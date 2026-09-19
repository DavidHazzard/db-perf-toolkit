"""SQL Server connection, and the read-only claim this backend can honestly make.

PostgreSQL's backend opens the session read-only at the server:
`default_transaction_read_only` means a write is refused by the engine no
matter what the tool does. **SQL Server has no equivalent, and this module
does not pretend otherwise.** There is no session-level `SET TRANSACTION READ
ONLY`; read-only is a property of a database (`ALTER DATABASE ... SET
READ_ONLY`) or of an availability replica, and setting either is a production
change a diagnostic tool has no business making.

What this connection actually does about it:

* `read_only=True` gates the maintenance path inside this process. That stops
  the tool writing; it stops nothing else. A seatbelt, not a wall.
* `ApplicationIntent=ReadOnly` is sent. Against an availability group listener
  it routes the session to a readable secondary, where writes *are* refused by
  the engine. Against a standalone instance it is accepted and ignored, and
  writes succeed — so it is a routing hint that sometimes has the side effect
  of enforcement, never an enforcement mechanism, and it is not reported as
  one anywhere in the output.
* Autocommit is on and every statement issued is a SELECT.

The enforcement that does exist is the login's permissions, and that is a
deployment decision rather than something a connection string can assert. A
login for this tool wants exactly:

    -- instance-wide DMVs: blocking, missing indexes, index usage stats
    GRANT VIEW SERVER STATE TO [dbperf];   -- VIEW DATABASE STATE on Azure SQL Database
    -- catalog views and object metadata
    ALTER ROLE db_datareader ADD MEMBER [dbperf];

and nothing more. SQL Server 2022 split VIEW SERVER STATE into granular
permissions — a denial there reads "VIEW SERVER PERFORMANCE STATE permission
was denied" — but the old grant still covers them, so one line remains the
whole story. That belongs here, beside the code someone would otherwise
assume was enforcing it, rather than in a README they may never open.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, unquote

import pyodbc

from db_perf_toolkit.backends.base import Backend, CheckUnavailable
from db_perf_toolkit.backends.sqlserver.capabilities import (
    ServerCapabilities,
    detect_capabilities,
)
from db_perf_toolkit.models import Check, StatsWindow

#: Identifies the session in sys.dm_exec_sessions.program_name. A DBA who
#: finds an unfamiliar session running DMV queries against production should be
#: able to name it without asking anyone.
APPLICATION_NAME = "db-perf-toolkit"

#: Driver 17 works, but 18 is the one that is still supported and the only one
#: that speaks TDS 8.0. Overridable per-DSN with ?driver=...
DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"

#: Diagnostics must never become the incident, so catalog queries are bounded.
#: Matches the PostgreSQL backend; see connect() for what the bound is worth.
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000

#: SET LOCK_TIMEOUT, in milliseconds. This one is an exact analogue of
#: PostgreSQL's lock_timeout: it bounds time spent queuing, not time spent
#: working, and the engine enforces it.
DEFAULT_LOCK_TIMEOUT_MS = 10_000

#: The window for every cumulative counter on SQL Server. Named for the column
#: it comes from so the report can say where the number came from.
WINDOW_SOURCE = "sqlserver_start_time"

#: Hosts where trusting a self-signed certificate costs nothing, because an
#: attacker positioned to intercept loopback traffic already owns the machine.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ".", "(local)"})

#: DSN query parameters mapped to their ODBC keyword. Anything not listed is
#: passed through verbatim: ODBC keywords are an open set, and a DSN is the
#: natural place to put MultiSubnetFailover or HostNameInCertificate without
#: this module needing to learn about each one.
_OPTION_ALIASES = {
    "driver": "Driver",
    "encrypt": "Encrypt",
    "trustservercertificate": "TrustServerCertificate",
    "applicationintent": "ApplicationIntent",
    "authentication": "Authentication",
    "trustedconnection": "Trusted_Connection",
    "multisubnetfailover": "MultiSubnetFailover",
    "hostnameincertificate": "HostNameInCertificate",
}

_SUPPORTED_SCHEMES = frozenset({"mssql", "sqlserver"})


class SqlServerConnectionError(ConnectionError):
    """Could not reach the server, with a message meant for a human.

    pyodbc's own errors are a SQLSTATE and a driver string, which is accurate
    and close to unreadable — "IM002 ... Data source name not found" is how a
    missing ODBC driver announces itself, and nothing in it mentions ODBC
    drivers. This carries the diagnosis instead.
    """


@dataclass(frozen=True, slots=True)
class Target:
    """A parsed DSN, before it becomes an ODBC connection string."""

    host: str
    port: int | None = None
    instance: str | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    options: dict[str, str] = field(default_factory=dict)

    @property
    def server(self) -> str:
        """The ODBC Server keyword: host, plus instance or port if given.

        Named instances and ports are alternatives rather than a pair — the
        instance name is what the SQL Browser service resolves *to* a port —
        so an instance takes precedence and the port is dropped.

        An IPv6 literal keeps its brackets, because the port separator here is
        a comma and the address is full of colons: `::1,1433` is ambiguous to
        the driver in a way that `[::1],1433` is not.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.instance:
            return f"{host}\\{self.instance}"
        return f"{host},{self.port}" if self.port else host

    @property
    def is_local(self) -> bool:
        return self.host.lower() in _LOOPBACK_HOSTS


def parse_dsn(dsn: str) -> Target:
    """Parse mssql:// and mssql+pyodbc:// URLs.

    urlparse is not used. It lowercases the host, which silently mangles the
    `host\\INSTANCE` form that a percent-encoded backslash is meant to carry,
    and the credentials it returns are still percent-encoded — a password
    containing an @ or a : is not exotic.
    """
    scheme, separator, rest = dsn.partition("://")
    if not separator:
        raise ValueError(f"Not a connection URL: {dsn!r}. Expected mssql://user@host/database")

    # SQLAlchemy-style "+driver" suffixes name the Python driver, which is
    # already decided here; mssql+pyodbc:// and mssql:// are the same thing.
    base_scheme = scheme.lower().partition("+")[0]
    if base_scheme not in _SUPPORTED_SCHEMES:
        raise ValueError(
            f"Not a SQL Server DSN: scheme {scheme!r}. Expected mssql:// or sqlserver://"
        )

    rest, _, query = rest.partition("?")
    netloc, _, path = rest.partition("/")
    credentials, _, hostspec = netloc.rpartition("@")

    username, _, password = credentials.partition(":")
    host, port = _split_host(hostspec)

    options: dict[str, str] = {}
    instance: str | None = None
    for key, value in parse_qsl(query, keep_blank_values=True):
        normalised = key.lower().replace("_", "").replace(" ", "")
        if normalised == "instance":
            instance = value
            continue
        options[_OPTION_ALIASES.get(normalised, key)] = value

    if "\\" in host:
        host, _, instance_from_host = host.partition("\\")
        instance = instance or instance_from_host

    if not host:
        raise ValueError(f"No host in {dsn!r}. Expected mssql://user@host/database")

    return Target(
        host=host,
        port=port,
        instance=instance,
        database=unquote(path) or None,
        username=unquote(username) or None,
        password=unquote(password) or None,
        options=options,
    )


def odbc_connection_string(target: Target, *, read_only: bool = True) -> str:
    """Build the ODBC connection string for a parsed DSN.

    Encrypt is the thing to get right. Driver 18 flipped the default to
    Encrypt=yes, which is correct and which breaks every local SQL Server
    container on first contact: the instance presents a self-signed
    certificate and the driver refuses the chain. The tempting fix is to send
    TrustServerCertificate=yes always, and that would quietly disable
    certificate validation against production servers on the far side of a
    network. So it is defaulted on for loopback only, where there is no
    network to be between us, and left alone everywhere else — a remote server
    with a self-signed certificate must say so in its DSN.
    """
    settings: dict[str, str] = {
        "Driver": DEFAULT_DRIVER,
        "Server": target.server,
    }
    if target.database:
        settings["Database"] = target.database

    if target.username:
        settings["UID"] = target.username
        if target.password is not None:
            settings["PWD"] = target.password
    else:
        # No user in the DSN means integrated authentication: Windows on the
        # box product, Kerberos through the Linux driver.
        settings["Trusted_Connection"] = "yes"

    if target.is_local:
        settings["TrustServerCertificate"] = "yes"

    if read_only:
        settings["ApplicationIntent"] = "ReadOnly"

    # The DSN wins over every default above: the person typing the connection
    # string knows things this module does not.
    settings.update(target.options)

    # APP does not, and is applied last. An unidentifiable session running
    # DMV queries on production is exactly the thing a DBA has to interrupt
    # someone to ask about.
    settings["APP"] = APPLICATION_NAME

    return ";".join(f"{key}={_escape(value)}" for key, value in settings.items()) + ";"


class SqlServerBackend(Backend):
    """Read-only access to one SQL Server instance.

    Checks live in sibling modules and register themselves in `supports`; the
    base class refuses everything not listed there, so a half-built backend
    says "SQL Server has no equivalent of this check" rather than returning an
    empty table that reads like a clean bill of health.
    """

    engine = "SQL Server"

    #: Empty until the check modules add their own. Nothing is claimed here
    #: that is not implemented somewhere.
    supports: frozenset[Check] = frozenset()

    def __init__(
        self,
        conn: pyodbc.Connection,
        *,
        database: str,
        host: str | None = None,
        read_only: bool = True,
    ) -> None:
        self._conn = conn
        self._database = database
        self._host = host
        self.read_only = read_only
        self._capabilities: ServerCapabilities | None = None

    @property
    def database(self) -> str:
        return self._database

    @property
    def host(self) -> str | None:
        return self._host

    @property
    def capabilities(self) -> ServerCapabilities:
        """Edition, version and feature detection, resolved once.

        Cached for the life of the connection because none of it can change
        underneath us — a failover that changed the answers would take the
        connection with it.
        """
        if self._capabilities is None:
            self._capabilities = detect_capabilities(self._query)
        return self._capabilities

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def stats_window(self) -> StatsWindow:
        """When SQL Server's cumulative counters last started from zero.

        There is no pg_stat_database.stats_reset here. Index usage lives in
        sys.dm_db_index_usage_stats, which is memory-resident and starts empty
        when the service starts — so the window is bounded by
        sqlserver_start_time, and every cumulative check is only as
        trustworthy as that number is old.

        The consequence is worse than on PostgreSQL, not merely different.
        After a failover, a patch, or an Azure platform-initiated restart, the
        counters are seconds old and *every index on the server reads as
        unused*. A tool that skipped this guard would, on the morning after a
        failover, cheerfully recommend dropping a correct production index
        set. That is the whole reason this method exists.

        If the start time cannot be read, the check is refused rather than
        reported as unknown. A missing window is not a benign default: the
        stats-age guard treats "no reset recorded" as the strongest possible
        window, which is the right reading of PostgreSQL's NULL and precisely
        the wrong one here.
        """
        start = self._server_start_utc()
        if start is None:
            raise CheckUnavailable(
                "stats-window",
                "Could not read the server start time, so the age of the usage "
                "counters is unknown.",
                "Every cumulative check depends on it — an index looks unused after a\n"
                "restart whether or not it is. Grant the DMV permission:\n"
                "  GRANT VIEW SERVER STATE TO [<login>];\n"
                "On Azure SQL Database: GRANT VIEW DATABASE STATE TO [<user>];",
            )
        return StatsWindow(
            stats_reset=start,
            server_version=self.capabilities.describe(),
            window_source=WINDOW_SOURCE,
        )

    def _server_start_utc(self) -> datetime | None:
        """Server start time, as an aware UTC datetime, or None if unreadable.

        Timezone conversion happens in SQL rather than in Python because the
        column is the server's local time and the client may be anywhere. The
        offset applied is the one in force *now*, so a server that started on
        the other side of a daylight-saving transition is reported an hour
        out; against a window measured in days that is noise, and the
        alternative needs a timezone database the server may not have.

        Aware rather than naive is not cosmetic: safety.stats_window_age_days
        subtracts this from an aware `now`, and mixing the two raises
        TypeError in the middle of the guard that protects index drops.
        """
        for statement in (_SERVER_START_SQL, _SESSION_ONE_START_SQL):
            try:
                row = self._query_one(statement)
            except pyodbc.Error:
                # Both readings need VIEW SERVER STATE; the second is tried
                # anyway because a denied DMV and an unsupported one look
                # alike from here, and Azure SQL Database restricts them
                # differently.
                continue
            if row and row["start_time_utc"] is not None:
                started: datetime = row["start_time_utc"]
                return started.replace(tzinfo=UTC)
        return None

    # ------------------------------------------------------------------
    # Helpers for the check modules
    # ------------------------------------------------------------------

    def _query(self, statement: str, *params: object) -> list[dict[str, Any]]:
        """Run one SELECT and return rows keyed by column name.

        pyodbc yields positional Row tuples. Reading by name instead costs one
        dict per row and means that reordering a long SELECT list cannot
        silently move a value into the wrong field — these queries have
        fifteen-column projections where two adjacent columns are both counts.
        """
        with self._conn.cursor() as cur:
            if params:
                cur.execute(statement, params)
            else:
                cur.execute(statement)
            if cur.description is None:
                return []
            columns = [column[0] for column in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    def _query_one(self, statement: str, *params: object) -> dict[str, Any] | None:
        rows = self._query(statement, *params)
        return rows[0] if rows else None

    def _scalar(self, statement: str, *params: object) -> Any:
        row = self._query_one(statement, *params)
        return next(iter(row.values())) if row else None

    @staticmethod
    def _quote(identifier: str) -> str:
        """Bracket-quote one identifier, doubling any closing bracket.

        pyodbc has nothing like psycopg.sql.Identifier, so this is it. The
        names come from catalog views rather than from a user, but generated
        DDL is written into the rollback manifest and is meant to be read and
        pasted by a human, and `[Sales].[Orders 2024]` has to survive that trip
        intact.
        """
        return "[" + identifier.replace("]", "]]") + "]"

    def _target(self, schema: str, name: str) -> str:
        """Quoted two-part name, for display and for the manifest."""
        return f"{self._quote(schema)}.{self._quote(name)}"

    @contextmanager
    def _denied_as_unavailable(self, check: str, remedy: str) -> Iterator[None]:
        """Turn a permission failure into a skipped check, not a crash.

        A missing grant is something the operator can fix, which is the whole
        distinction CheckUnavailable exists to draw. Anything else — a syntax
        error, a DMV that does not exist on this edition — is left to
        propagate, because that is a bug in this tool and hiding it as
        "unavailable" would make it invisible.
        """
        try:
            yield
        except pyodbc.Error as exc:
            if not _is_permission_error(exc):
                raise
            raise CheckUnavailable(check, "Permission denied reading this DMV.", remedy) from exc


#: The offset is computed in SQL and the result stamped UTC in Python, because
#: the alternative — datetimeoffset — is the one SQL Server type pyodbc will
#: not decode without a registered output converter.
_SERVER_START_SQL = """
    SELECT DATEADD(MINUTE, DATEDIFF(MINUTE, GETDATE(), GETUTCDATE()), sqlserver_start_time)
               AS start_time_utc
    FROM sys.dm_os_sys_info
"""

#: Session 1 is the first session the engine opens at startup, so its login
#: time is the service start time by another route. Kept as a fallback because
#: sys.dm_os_sys_info is restricted on some Azure tiers where
#: sys.dm_exec_sessions is not.
_SESSION_ONE_START_SQL = """
    SELECT DATEADD(MINUTE, DATEDIFF(MINUTE, GETDATE(), GETUTCDATE()), login_time)
               AS start_time_utc
    FROM sys.dm_exec_sessions
    WHERE session_id = 1
"""

_CURRENT_DATABASE_SQL = "SELECT DB_NAME() AS database_name"


def connect(
    dsn: str,
    *,
    connect_timeout: int = 10,
    read_only: bool = True,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    backend_cls: type[SqlServerBackend] | None = None,
) -> SqlServerBackend:
    """Open a connection with timeouts appropriate to what it will do.

    Signature-compatible with the PostgreSQL connect() so the registry can
    dispatch on the DSN scheme alone, but two of the four arguments mean
    something weaker here, and the difference is worth stating:

    `read_only` does not reach the server. See the module docstring — SQL
    Server has no session-level read-only setting, so this gates the tool's
    own maintenance path and asks for ReadOnly routing, and the real guarantee
    has to come from the login's permissions.

    `statement_timeout_ms` is a client-side bound, not a server-side one.
    PostgreSQL's statement_timeout is enforced by the backend running the
    query; ODBC's query timeout is enforced by the driver, which sends an
    attention signal when the interval elapses and then waits for the server
    to notice. The work started either way. It is also expressed in whole
    seconds, so the value is rounded up — rounding 500ms down to 0 would mean
    "no timeout at all", which is the opposite of what was asked for.

    `lock_timeout_ms` maps exactly: SET LOCK_TIMEOUT bounds how long we wait to
    *start*, enforced by the engine, and giving up is better than joining the
    queue behind someone's schema change.

    Autocommit is on. An open read transaction here would hold its locks and
    pin the version store for as long as the process runs, which is precisely
    the condition this tool exists to find in other people's sessions.
    """
    target = parse_dsn(dsn)

    try:
        # pyodbc's timeout= sets the ODBC login timeout, which is what bounds
        # the connect itself; Connection.timeout below governs statements.
        conn = pyodbc.connect(
            odbc_connection_string(target, read_only=read_only),
            timeout=connect_timeout,
            autocommit=True,
        )
    except pyodbc.Error as exc:
        raise _connection_failure(target, exc) from exc

    if statement_timeout_ms is None:
        statement_timeout_ms = DEFAULT_STATEMENT_TIMEOUT_MS if read_only else 0
    conn.timeout = _ceil_seconds(statement_timeout_ms)

    with conn.cursor() as cur:
        # SET takes no bind parameters, so the value is interpolated — via
        # int(), which is what makes that safe rather than the shape of it.
        cur.execute(f"SET LOCK_TIMEOUT {int(lock_timeout_ms)}")
        cur.execute(_CURRENT_DATABASE_SQL)
        # The DSN's database may be absent, in which case the login's default
        # database is what we are actually pointed at. Reporting the one we
        # asked for would mislabel every result.
        database = str(cur.fetchone()[0])

    # backend_cls is how the package composes the check mixins onto this base.
    # Defaulting to the bare class keeps connection.py independent of them, so
    # importing it never drags in every check module.
    cls = backend_cls or SqlServerBackend
    return cls(conn, database=database, host=target.host, read_only=read_only)


def _split_host(hostspec: str) -> tuple[str, int | None]:
    """Separate host from port, leaving bracketed IPv6 literals intact."""
    if hostspec.startswith("["):
        literal, _, tail = hostspec.partition("]")
        port = tail.lstrip(":")
        return literal[1:], int(port) if port.isdigit() else None

    head, colon, tail = hostspec.rpartition(":")
    if colon and tail.isdigit():
        return unquote(head), int(tail)
    return unquote(hostspec), None


def _escape(value: str) -> str:
    """Brace-quote an ODBC value that would otherwise end the keyword early.

    A password containing a semicolon is the common case, and without this it
    truncates the connection string into something that either fails to parse
    or, worse, connects with different settings than were asked for.
    """
    if any(character in value for character in ";{}=") or value.strip() != value:
        return "{" + value.replace("}", "}}") + "}"
    return value


def _ceil_seconds(milliseconds: int) -> int:
    return -(-int(milliseconds) // 1000)


def _is_permission_error(exc: pyodbc.Error) -> bool:
    """True for "you may not read that", false for every other 42000.

    SQLSTATE 42000 is SQL Server's catch-all for syntax and access errors
    alike, so the state on its own cannot distinguish a denied DMV from a
    typo. The message text is the only signal available; both wordings the
    engine uses for this — error 297's "does not have permission" and error
    300's "permission was denied" — contain the word.
    """
    state = str(exc.args[0]) if exc.args else ""
    return state in {"42000", "42501"} and "permission" in str(exc).lower()


def _connection_failure(target: Target, exc: pyodbc.Error) -> SqlServerConnectionError:
    """Translate pyodbc's SQLSTATE into something actionable."""
    state = str(exc.args[0]) if exc.args else ""
    detail = str(exc).strip()

    if state == "IM002":
        return SqlServerConnectionError(
            f"The ODBC driver {DEFAULT_DRIVER!r} is not installed.\n"
            "Install it, or name an installed one with ?driver=...\n"
            "  https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server\n"
            "Installed drivers: " + (", ".join(pyodbc.drivers()) or "none")
        )

    # Driver 18 encrypts by default, so this is the first thing a local
    # container does, and the driver's own wording does not suggest a fix.
    if "certificate" in detail.lower():
        return SqlServerConnectionError(
            f"TLS handshake with {target.server} failed: {detail}\n"
            "The server is presenting a certificate this machine does not trust, which\n"
            "is what a self-signed development certificate looks like. If you know the\n"
            "server, append ?trust_server_certificate=yes to the DSN — that disables\n"
            "certificate validation, so do not do it across an untrusted network."
        )

    return SqlServerConnectionError(f"Could not connect to {target.server}: {detail}")
