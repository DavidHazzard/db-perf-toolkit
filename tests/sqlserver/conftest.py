"""Test fixtures backed by a real SQL Server instance.

The mirror of `tests/conftest.py`, and for the same reason: mocking a cursor
would only prove that the code calls a cursor. Whether a DMV query is *correct*
is a question about SQL Server's dynamic management views, and the only thing
that can answer it is SQL Server.

Three things make this harder than the PostgreSQL equivalent, and each is
solved below rather than hoped away.

1. STARTUP IS SLOW AND LIES ABOUT BEING FINISHED.

   `scripts/demo.sh` has a comment about `pg_isready`: PostgreSQL's entrypoint
   runs a throwaway server for initdb and then restarts, so a readiness probe
   that fires during that window is answering about a server that is about to
   go away. SQL Server has the same shape of trap with different plumbing.
   The engine binds 1433 early, while it is still creating and recovering
   master, model, msdb and tempdb; during that window a TCP connect succeeds,
   a login can fail with "Login failed for user 'sa'" because the entrypoint
   has not applied MSSQL_SA_PASSWORD yet, and even after login succeeds a
   just-created user database can still be RECOVERING.

   Measured on this image, three runs, warm:

       docker run returned      0.52s
       TCP 1433 accepts         0.53s   <- a port check says "ready" here
       master answers SELECT 1  7.66s   <- it is actually ready here
       dbperf answers SELECT 1  8.10s

   A port check is wrong by about seven seconds, and not quietly: polling
   across that window returns an 08001 TLS handshake failure first and then
   "Login failed for user 'sa'" (18456), because the entrypoint has not
   applied MSSQL_SA_PASSWORD yet. Anything retrying only on connection-refused
   sails past both.

   So the probe is `SELECT 1` **against the target database**, not a port
   check and not a `SELECT 1` against master. You cannot open a session
   against a database that is not ONLINE, which makes a successful round-trip
   proof rather than inference. `_wait_until_usable` runs it twice: once for
   master, to get far enough to issue CREATE DATABASE, and once for the
   application database afterwards - CREATE DATABASE returns before the new
   database is necessarily usable.

2. THE DIAGNOSTIC VIEWS ARE EMPTY UNTIL SOMETHING RUNS.

   `sys.dm_db_missing_index_details` is written by the query optimiser as a
   side effect of compiling a plan. Schema alone produces nothing; a fixture
   that only runs DDL yields a database that reports itself perfectly healthy,
   which is the failure mode to watch for here - it looks complete, it runs
   clean, and it asserts nothing. The workload lives in
   `scripts/scenarios/sqlserver/small.sql` so it is inspectable, and is
   replayed from here batch for batch, `GO n` included.

   Read that file's header and the README beside it before changing anything
   in it: the section order is load-bearing, and index DDL in the wrong place
   silently erases the missing-index pathology.

3. THE CONTAINER IS EXPENSIVE.

   One per test is not viable, so `sqlserver_instance` and
   `seeded_sqlserver_dsn` are session-scoped and shared. Note that pytest-xdist
   gives each worker its own session and therefore its own container: run this
   package single-process.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from testcontainers.core.container import DockerContainer

try:  # pragma: no cover - exercised by its own absence
    import pyodbc
except ImportError:  # pragma: no cover
    pyodbc = None


#: Overridable so the same fixtures can be pointed at 2017/2019 to check that a
#: DMV query has not quietly grown a version dependency.
IMAGE = os.environ.get("DBPERF_MSSQL_IMAGE", "mcr.microsoft.com/mssql/server:2022-latest")

#: SQL Server enforces its own password policy on the SA account and refuses to
#: finish starting if it is not met; the container then exits with a message
#: only visible in its logs.
SA_PASSWORD = os.environ.get("DBPERF_MSSQL_PASSWORD", "dbperf-Test-Passw0rd!")
SA_USERNAME = "sa"

DATABASE = "dbperf"

#: Generous because a cold image on a loaded host genuinely can take minutes;
#: the fixture reports what it actually took via `sqlserver_startup_seconds`.
STARTUP_TIMEOUT = float(os.environ.get("DBPERF_MSSQL_STARTUP_TIMEOUT", "300"))

SEED_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "scenarios" / "sqlserver" / "small.sql"
)

#: `GO` is a sqlcmd batch separator, not T-SQL, so the driver never sees it and
#: the script has to be split here. `GO 60` means "run this batch 60 times",
#: which is how the seed expresses a repeated workload without duplicating the
#: statements in Python.
_GO = re.compile(r"^\s*GO(?:[ \t]+(\d+))?[ \t]*(?:--.*)?$", re.IGNORECASE)

#: msodbcsql registers itself under a versioned name. Match any of them and
#: prefer the newest rather than hard-coding 18, so a runner with only 17
#: installed still works.
_DRIVER = re.compile(r"^ODBC Driver (\d+) for SQL Server$")

#: What the backend assumes when a DSN names no driver; anything else has to be
#: spelled out in the DSN.
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

#: Hosts the backend treats as local, and therefore trusts a self-signed
#: certificate from without being told to.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ".", "(local)"})


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything in this package `sqlserver`.

    A hook rather than a `pytestmark` in each module: the point of the marker
    is that `-m "not sqlserver"` reliably skips a 1.7GB download, and that
    guarantee should not depend on whoever adds the next test file remembering
    a decorator.
    """
    here = Path(__file__).parent
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and here in Path(str(path)).parents:
            item.add_marker(pytest.mark.sqlserver)


# ---------------------------------------------------------------------------
# Connection plumbing
# ---------------------------------------------------------------------------


def _best_odbc_driver() -> str | None:
    best: str | None = None
    best_version = -1
    for name in pyodbc.drivers():
        match = _DRIVER.match(name)
        if match and int(match.group(1)) > best_version:
            best, best_version = name, int(match.group(1))
    return best


@dataclass(frozen=True)
class SqlServerInstance:
    """Everything a test needs to reach the container."""

    host: str
    port: int
    driver: str
    username: str = SA_USERNAME
    password: str = SA_PASSWORD
    database: str = DATABASE
    startup_seconds: float = 0.0

    def odbc(self, database: str | None = None, *, login_timeout: int = 5) -> str:
        # TrustServerCertificate is not laziness. Driver 18 flipped the default
        # to Encrypt=yes, and the container presents a self-signed certificate,
        # so without this every connection fails on certificate validation
        # rather than on anything to do with the test.
        return (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={database or self.database};"
            f"UID={self.username};PWD={self.password};"
            "Encrypt=yes;TrustServerCertificate=yes;"
            f"Connection Timeout={login_timeout};"
        )

    def url(self, database: str | None = None) -> str:
        """The DSN form the backend's `connect()` takes.

        Kept as bare as it can be. The backend applies TrustServerCertificate
        itself for loopback hosts, and testcontainers publishes to localhost,
        so spelling it out here would mean the same ODBC keyword arriving
        twice. Query parameters are only added for the cases the backend
        cannot infer: a non-default driver, or a host it will not recognise as
        local.
        """
        quoted = urllib.parse.quote(self.password, safe="")
        url = (
            f"mssql://{self.username}:{quoted}@{self.host}:{self.port}/{database or self.database}"
        )
        params: dict[str, str] = {}
        if self.driver != DEFAULT_DRIVER:
            params["driver"] = self.driver
        if self.host.lower() not in _LOOPBACK_HOSTS:
            params["trust_server_certificate"] = "yes"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url


def _wait_until_usable(
    instance: SqlServerInstance, database: str, *, timeout: float
) -> tuple[float, int]:
    """Poll `SELECT 1` against `database` until it answers.

    Returns (seconds waited, attempts). Deliberately does NOT probe the port:
    1433 is listening long before the instance can serve a query, so a port
    check reports ready during the exact window this function exists to wait
    out.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    attempts = 0
    last: Exception | None = None

    while time.monotonic() < deadline:
        attempts += 1
        try:
            conn = pyodbc.connect(instance.odbc(database), timeout=5, autocommit=True)
            try:
                if conn.cursor().execute("SELECT 1").fetchval() == 1:
                    return time.monotonic() - started, attempts
            finally:
                conn.close()
        except Exception as exc:  # every failure here means "not ready yet"
            last = exc
        time.sleep(0.5)

    raise RuntimeError(
        f"SQL Server did not serve a query against [{database}] within {timeout:.0f}s "
        f"({attempts} attempts). Last error: {last}"
    )


def split_batches(script: str) -> list[tuple[str, int]]:
    """Split a sqlcmd script into (batch, repeat-count) pairs.

    Public because the seed script is the specification of the scenario, and a
    test that wants to assert on what it contains should read it the same way
    the fixture executes it.
    """
    batches: list[tuple[str, int]] = []
    buffer: list[str] = []

    def flush(repeat: int) -> None:
        text = "\n".join(buffer).strip()
        if text:
            batches.append((text, repeat))
        buffer.clear()

    for line in script.splitlines():
        match = _GO.match(line)
        if match:
            flush(int(match.group(1) or 1))
        else:
            buffer.append(line)
    flush(1)
    return batches


def _run_batch(cursor: Any, sql: str) -> None:
    cursor.execute(sql)
    # Drain every result set. Not cosmetic: leaving rows unfetched means the
    # server may not have finished producing them, so the "workload" would be
    # cheaper than it looks and the query-stats numbers would understate it.
    while True:
        with suppress(pyodbc.ProgrammingError):
            cursor.fetchall()
        if not cursor.nextset():
            break


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sqlserver_instance() -> Iterator[SqlServerInstance]:
    """A running SQL Server with an empty `dbperf` database.

    Session-scoped: the image is ~1.7GB and startup measures 8-9s warm, so
    one container is shared by every test in this package.
    """
    if pyodbc is None:
        pytest.skip("pyodbc is not installed; install the project's sqlserver extra")

    driver = _best_odbc_driver()
    if driver is None:
        pytest.skip(
            "no 'ODBC Driver NN for SQL Server' is registered with unixODBC. "
            "pyodbc alone is not enough - install Microsoft's msodbcsql18. "
            f"Drivers seen: {pyodbc.drivers()}"
        )

    container = (
        DockerContainer(IMAGE)
        .with_env("ACCEPT_EULA", "Y")
        .with_env("MSSQL_SA_PASSWORD", SA_PASSWORD)
        # The pre-2019 spelling. Harmless on 2022 and keeps DBPERF_MSSQL_IMAGE
        # usable for checking a DMV query against an older server.
        .with_env("SA_PASSWORD", SA_PASSWORD)
        .with_env("MSSQL_PID", "Developer")
        .with_env("MSSQL_AGENT_ENABLED", "false")
        .with_exposed_ports(1433)
    )

    began = time.monotonic()
    with container as running:
        bare = SqlServerInstance(
            host=running.get_container_host_ip(),
            port=int(running.get_exposed_port(1433)),
            driver=driver,
            database="master",
        )

        try:
            _wait_until_usable(bare, "master", timeout=STARTUP_TIMEOUT)
        except RuntimeError as exc:  # pragma: no cover - diagnostics only
            raise RuntimeError(f"{exc}\n--- container logs ---\n{_logs(running)}") from exc

        with pyodbc.connect(bare.odbc("master"), autocommit=True) as conn:
            conn.cursor().execute(f"IF DB_ID('{DATABASE}') IS NULL CREATE DATABASE [{DATABASE}]")

        instance = SqlServerInstance(
            host=bare.host, port=bare.port, driver=driver, database=DATABASE
        )
        # The second probe is the one that matters. CREATE DATABASE returns
        # before the database is necessarily ONLINE, so this is where the
        # "accepts connections before it is usable" problem actually bites.
        _wait_until_usable(instance, DATABASE, timeout=STARTUP_TIMEOUT)

        yield SqlServerInstance(
            host=bare.host,
            port=bare.port,
            driver=driver,
            database=DATABASE,
            startup_seconds=time.monotonic() - began,
        )


def _logs(container: DockerContainer) -> str:  # pragma: no cover - diagnostics only
    try:
        stdout, stderr = container.get_logs()
        return (stdout + stderr).decode("utf-8", "replace")[-4000:]
    except Exception as exc:
        return f"(could not read container logs: {exc})"


@pytest.fixture(scope="session")
def sqlserver_startup_seconds(sqlserver_instance: SqlServerInstance) -> float:
    """How long the container took from `docker run` to serving a real query.

    Exposed as a fixture so CI can record it rather than have someone guess a
    timeout.
    """
    return sqlserver_instance.startup_seconds


@pytest.fixture(scope="session")
def sqlserver_dsn(sqlserver_instance: SqlServerInstance) -> str:
    """An empty database. Use `seeded_sqlserver_dsn` unless testing the empty case."""
    return sqlserver_instance.url()


@pytest.fixture(scope="session")
def seeded_sqlserver_dsn(sqlserver_instance: SqlServerInstance) -> str:
    """`dbperf` with the small scenario applied and its workload replayed.

    Session-scoped and therefore applied exactly once. Tests must treat it as
    read-only: anything that creates or drops an index on `dbo.orders` wipes
    that table's rows out of `sys.dm_db_missing_index_details` for every test
    that runs afterwards.
    """
    script = SEED_SCRIPT.read_text(encoding="utf-8")
    with pyodbc.connect(sqlserver_instance.odbc(), autocommit=True) as conn:
        cursor = conn.cursor()
        for sql, repeat in split_batches(script):
            for _ in range(repeat):
                _run_batch(cursor, sql)
    return sqlserver_instance.url()


@pytest.fixture
def mssql_connect(
    sqlserver_instance: SqlServerInstance,
) -> Iterator[Callable[..., Any]]:
    """Factory for extra connections, closed for you at the end of the test."""
    opened: list[Any] = []

    def _connect(database: str | None = None, *, autocommit: bool = True) -> Any:
        conn = pyodbc.connect(sqlserver_instance.odbc(database), autocommit=autocommit)
        opened.append(conn)
        return conn

    yield _connect

    for conn in opened:
        with suppress(Exception):
            conn.close()


@pytest.fixture
def mssql_query(mssql_connect: Callable[..., Any]) -> Callable[..., list[dict[str, Any]]]:
    """Run a query and get dicts back, for asserting directly on a DMV.

    Requires a statement that returns a result set; use `mssql_connect` for
    anything that does not.

    Two traps worth knowing before writing a DMV assertion, both of which cost
    real time to find (the README beside `small.sql` has the detail):

      * `sys.dm_exec_sql_text.dbid` is NULL for ad-hoc batches, so scoping
        `sys.dm_exec_query_stats` with `WHERE st.dbid = DB_ID()` returns zero
        rows for an ordinary workload. Read `dbid` from
        `sys.dm_exec_plan_attributes(qs.plan_handle)` instead.
      * Simple parameterisation rewrites statement text, so a query sent as
        `WHERE customer_id = 42` comes back as
        `(@1 tinyint)SELECT ... WHERE [customer_id]=@1`. Text from these views
        is normalised, not verbatim.
    """
    conn = mssql_connect()

    def _query(sql: str, *params: Any) -> list[dict[str, Any]]:
        cursor = conn.cursor().execute(sql, *params)
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    return _query


@dataclass(frozen=True)
class BlockingChain:
    blocker_spid: int
    blocked_spid: int


@pytest.fixture
def blocking_chain(
    mssql_connect: Callable[..., Any],
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> Callable[..., Any]:
    """Hold an exclusive table lock in one session and collide with it in another.

    This is the one pathology the seed script cannot produce: a lock chain
    needs two sessions alive at the same time, and a script is one session.

    Depends on READ_COMMITTED_SNAPSHOT being OFF - the seed sets it explicitly
    for exactly this reason. Under RCSI the reader takes a row version instead
    of a shared lock, nothing blocks, and this fixture would wait out its
    timeout for no reason anyone could see from the test.
    """

    @contextmanager
    def _chain(table: str = "dbo.orders", timeout: float = 20.0) -> Iterator[BlockingChain]:
        # autocommit=False so the lock outlives the statement that took it.
        # With autocommit on, SQL Server releases the TABLOCKX the instant the
        # SELECT finishes and there is nothing to block against.
        holder = mssql_connect(autocommit=False)
        holder_cursor = holder.cursor()
        holder_cursor.execute(f"SELECT TOP (1) * FROM {table} WITH (TABLOCKX, HOLDLOCK)")
        holder_cursor.fetchall()
        blocker_spid = int(holder.cursor().execute("SELECT @@SPID").fetchval())

        victim = mssql_connect()
        blocked_spid = int(victim.cursor().execute("SELECT @@SPID").fetchval())
        collided = threading.Event()

        def collide() -> None:
            collided.set()
            # Expected to sit blocked until the holder rolls back; a failure
            # here is the teardown racing us, not a problem to surface.
            with suppress(Exception):
                victim.cursor().execute(f"SELECT COUNT_BIG(*) FROM {table}").fetchall()

        thread = threading.Thread(target=collide, daemon=True)
        thread.start()
        collided.wait(timeout=5)

        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                waiting = mssql_query(
                    "SELECT session_id, blocking_session_id "
                    "FROM sys.dm_exec_requests "
                    "WHERE blocking_session_id <> 0 AND session_id = ?",
                    blocked_spid,
                )
                if waiting and waiting[0]["blocking_session_id"] == blocker_spid:
                    break
                time.sleep(0.25)
            else:
                raise AssertionError(
                    f"session {blocked_spid} never showed as blocked by {blocker_spid} "
                    f"in sys.dm_exec_requests within {timeout:.0f}s"
                )
            yield BlockingChain(blocker_spid=blocker_spid, blocked_spid=blocked_spid)
        finally:
            with suppress(Exception):
                holder.rollback()
            thread.join(timeout=10)

    return _chain


@pytest.fixture
def wait_for_sqlserver() -> Callable[..., None]:
    """Poll until a DMV-dependent condition holds.

    The SQL Server counterpart of `wait_for` in `tests/conftest.py`. Less of
    the lag is asynchronous here - the dm_exec_* views are read live off the
    plan cache - but Query Store and index usage stats are both written by
    background tasks, so a bare assertion straight after a workload can be
    legitimately early.
    """

    def _wait(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.5)
        raise AssertionError(f"condition never became true within {timeout}s")

    return _wait
