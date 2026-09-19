"""Slow-query and blocking checks, against a real SQL Server.

Nothing here is mocked, for the reason `tests/sqlserver/conftest.py` gives at
length: whether a DMV query is *correct* is a question about SQL Server's
dynamic management views, and a stubbed cursor can only prove that the code
calls a cursor.

Three of these tests exist specifically because the failures they guard
against ship green. A check that scopes the plan cache the obvious way returns
zero rows, a check read by an under-privileged login returns zero rows, and
both render as "no slow queries found" — a clean run, a healthy-looking
report, and nothing to investigate. Assertions that only say "it did not
crash" would pass in every one of those cases.

The assertions are on structure rather than on literals wherever the literal
is an artefact of timing: total times vary run to run, so what is asserted is
that the ordering is by total time, that the means are the totals divided by
the counts, and that the percentages are shares of a whole. Execution counts
are the exception and are asserted literally, because the seed script fixes
them — a statement that ran sixty times must not come back as sixty statements
that ran once.

**The seeded database is shared and read-only by contract.** Nothing here
creates or drops an index on `dbo.orders`; doing so discards that table's rows
in `sys.dm_db_missing_index_details` and breaks every test that runs
afterwards. The one thing these tests do create is a server login, which
touches no table and no index, and it is killed and dropped again on the way
out.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from db_perf_toolkit.backends.base import CheckUnavailable
from db_perf_toolkit.backends.sqlserver.connection import SqlServerBackend, connect
from db_perf_toolkit.backends.sqlserver.queries import (
    QueryChecks,
    blocking_chains,
    slow_queries,
)
from db_perf_toolkit.models import Check

from .conftest import SqlServerInstance

pyodbc = pytest.importorskip("pyodbc")

pytestmark = pytest.mark.sqlserver

#: A login with no grant beyond CONNECT, used to prove that a denial becomes a
#: skipped check with a remedy rather than a traceback — and, for blocking,
#: that it does not become a clean bill of health.
_NOPRIV_LOGIN = "dbperf_queries_nopriv"
_NOPRIV_PASSWORD = "dbperf-NoPriv-Passw0rd!"

#: `GO 60` in the seed script: the statement the workload runs most often. Its
#: execution count is fixed by the fixture, so it is safe to assert on.
_REPEATED_SCAN = "%total_cents > 90000%"

#: Execution counts the seed script produces, as documented in
#: scripts/scenarios/sqlserver/README.md (60/30/12/12/8). Only the two that
#: are unambiguous are asserted on: the twelves belong to two different
#: statements, and a plan that is recompiled mid-workload splits its count
#: across two cache entries. The point of the assertion is that repeated
#: executions are *accumulated* rather than reported as one call each, and
#: these two carry that on their own.
_SEEDED_EXECUTION_COUNTS = {60, 30}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(seeded_sqlserver_dsn: str) -> Iterator[SqlServerBackend]:
    """A backend on the seeded database, closed at the end of the test."""
    with connect(seeded_sqlserver_dsn) as opened:
        yield opened


@pytest.fixture
def plan_cache_backend(backend: SqlServerBackend) -> SqlServerBackend:
    """The same backend, forced onto the `sys.dm_exec_query_stats` path.

    The seeded database has Query Store on, so the fallback would otherwise
    never be exercised — and the fallback is the path every server without
    Query Store takes, which is most of them. Capabilities are resolved once
    and cached, so overwriting the cache is the whole mechanism; nothing on
    the server changes.
    """
    backend._capabilities = dataclasses.replace(
        backend.capabilities, query_store_enabled=False, query_store_state=None
    )
    return backend


@pytest.fixture
def unprivileged_dsn(
    sqlserver_instance: SqlServerInstance,
    mssql_connect: Callable[..., Any],
) -> Iterator[str]:
    """A DSN for a login holding nothing but CONNECT on the database.

    The sessions are killed before the login is dropped. pyodbc pools
    connections by default, so `Connection.close()` returns the session to the
    pool rather than ending it, and `DROP LOGIN` then fails with "the user is
    currently logged in" (15434) — which would leave the login behind for
    every later test in this session-scoped container.
    """
    conn = mssql_connect()
    cursor = conn.cursor()
    cursor.execute(
        f"IF SUSER_ID('{_NOPRIV_LOGIN}') IS NULL "
        f"CREATE LOGIN [{_NOPRIV_LOGIN}] "
        f"WITH PASSWORD = '{_NOPRIV_PASSWORD}', CHECK_POLICY = OFF"
    )
    cursor.execute(
        f"IF DATABASE_PRINCIPAL_ID('{_NOPRIV_LOGIN}') IS NULL "
        f"CREATE USER [{_NOPRIV_LOGIN}] FOR LOGIN [{_NOPRIV_LOGIN}]"
    )

    instance = dataclasses.replace(
        sqlserver_instance, username=_NOPRIV_LOGIN, password=_NOPRIV_PASSWORD
    )
    try:
        yield instance.url()
    finally:
        cursor.execute(
            "DECLARE @kill nvarchar(max) = N'';"
            "SELECT @kill += 'KILL ' + CAST(session_id AS varchar(10)) + ';' "
            "FROM sys.dm_exec_sessions WHERE login_name = ?;"
            "EXEC sys.sp_executesql @kill;",
            _NOPRIV_LOGIN,
        )
        cursor.execute(f"DROP USER IF EXISTS [{_NOPRIV_LOGIN}]")
        cursor.execute(f"IF SUSER_ID('{_NOPRIV_LOGIN}') IS NOT NULL DROP LOGIN [{_NOPRIV_LOGIN}]")


# ---------------------------------------------------------------------------
# slow_queries
# ---------------------------------------------------------------------------


def test_the_seeded_database_prefers_query_store(backend: SqlServerBackend) -> None:
    """The seed turns Query Store on, so the preferred path is the one taken.

    Asserted separately from the check itself: if this ever stops holding,
    every Query Store assertion below silently starts testing the plan cache
    instead and still passes.
    """
    assert backend.capabilities.query_store_enabled is True
    assert backend.capabilities.query_store_state == "READ_WRITE"


@pytest.mark.parametrize("path", ["query_store", "plan_cache"])
def test_slow_queries_returns_the_seeded_workload(
    request: pytest.FixtureRequest, path: str
) -> None:
    """Both sources find the workload, and agree on how often it ran.

    Parameterised over the two sources rather than written twice because the
    contract is that they are interchangeable: the same statements, the same
    execution counts, the same model. Only the window differs.
    """
    target: SqlServerBackend = request.getfixturevalue(
        "backend" if path == "query_store" else "plan_cache_backend"
    )

    rows = slow_queries(target, 50)

    assert rows, f"{path} returned no slow queries against a database with a seeded workload"
    assert len(rows) >= 5, f"{path} found {len(rows)} statements; the seed runs at least 5"

    counts = {row.calls for row in rows}
    assert counts >= _SEEDED_EXECUTION_COUNTS, (
        f"{path} lost seeded executions: expected {sorted(_SEEDED_EXECUTION_COUNTS)} "
        f"among {sorted(counts)}"
    )


@pytest.mark.parametrize("path", ["query_store", "plan_cache"])
def test_slow_queries_is_internally_consistent(request: pytest.FixtureRequest, path: str) -> None:
    """Ordering, arithmetic and units, checked against each other.

    The unit conversion is the reason for the upper bound on `total_ms`. SQL
    Server reports these durations in microseconds; forgetting to divide by
    1000 is invisible in a table of numbers but would put a 500ms statement at
    eight minutes, so anything claiming more wall-clock time than the seeded
    workload could possibly have taken is treated as the conversion having
    been dropped.
    """
    target: SqlServerBackend = request.getfixturevalue(
        "backend" if path == "query_store" else "plan_cache_backend"
    )

    rows = slow_queries(target, 50)
    assert rows

    totals = [row.total_ms for row in rows]
    assert totals == sorted(totals, reverse=True), "not ordered by total time"

    for row in rows:
        assert row.calls > 0
        assert row.total_ms >= 0.0
        assert row.total_ms < 600_000.0, (
            f"{row.total_ms}ms for a seeded statement suggests microseconds "
            f"were reported as milliseconds: {row.query[:60]}"
        )
        assert row.mean_ms == pytest.approx(row.total_ms / row.calls, rel=1e-6)
        assert row.rows >= 0
        assert 0.0 <= row.pct_total_time <= 100.0
        assert "\n" not in row.query, "query text should be squashed onto one line"

    # Shares of the whole population, so they sum to at most 100 and — since
    # this workload really does dominate the server's own chatter — to a good
    # deal more than nothing.
    assert sum(row.pct_total_time for row in rows) <= 100.0 + 1e-6
    assert sum(row.pct_total_time for row in rows) > 1.0


def test_slow_queries_respects_the_limit(backend: SqlServerBackend) -> None:
    assert len(slow_queries(backend, 3)) == 3


def test_percentages_are_of_the_whole_not_of_the_page(backend: SqlServerBackend) -> None:
    """A truncated list must not renormalise its own percentages.

    "This statement is 40% of the database's time" and "this statement is 40%
    of the three statements I chose to show you" are different claims, and only
    the first is worth acting on. The check computes the share before applying
    TOP, so the same statement reports the same percentage at any limit.

    The tolerance is a percentage point rather than a float epsilon because
    the denominator is live: statements this suite itself runs enter the plan
    cache between the two readings and move every share slightly. A
    renormalising bug would not move them slightly — it would put 100% between
    the top two rows, which is what the second assertion catches.
    """
    wide = {row.query: row.pct_total_time for row in slow_queries(backend, 50)}
    narrow = slow_queries(backend, 2)

    assert len(narrow) == 2
    for row in narrow:
        assert row.pct_total_time == pytest.approx(wide[row.query], abs=1.0)
    assert sum(row.pct_total_time for row in narrow) < 99.0


def test_the_null_dbid_trap_would_have_emptied_this_check(
    backend: SqlServerBackend,
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> None:
    """Regression guard for the filter that looks right and returns nothing.

    `sys.dm_exec_sql_text.dbid` is populated only for SQL inside a module, so
    it is NULL for every ad-hoc batch an application sends. Scoping the plan
    cache with `WHERE st.dbid = DB_ID()` therefore finds none of the workload
    — and reports a database under load as having no slow queries at all.

    This test runs both filters side by side so the failure is legible: the
    naive one must miss the seeded statement, and the plan-attribute one the
    check actually uses must find it.
    """
    naive = mssql_query(
        """
        SELECT COUNT_BIG(*) AS n
        FROM sys.dm_exec_query_stats AS qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
        WHERE st.dbid = DB_ID() AND st.text LIKE ?
        """,
        _REPEATED_SCAN,
    )
    correct = mssql_query(
        """
        SELECT COUNT_BIG(*) AS n
        FROM sys.dm_exec_query_stats AS qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
        CROSS APPLY (
            SELECT CAST(pa.value AS int) AS dbid
            FROM sys.dm_exec_plan_attributes(qs.plan_handle) AS pa
            WHERE pa.attribute = 'dbid'
        ) AS attr
        WHERE attr.dbid = DB_ID() AND st.text LIKE ?
        """,
        _REPEATED_SCAN,
    )

    assert naive[0]["n"] == 0, (
        "st.dbid now resolves for ad-hoc batches; if SQL Server has changed "
        "this, the comment in queries.py needs revisiting — but do not switch "
        "the filter back without measuring it"
    )
    assert correct[0]["n"] > 0

    found = [row for row in slow_queries(backend, 50) if "AS matched" in row.query]
    assert found, "the check itself lost the statement the naive filter loses"
    assert found[0].calls == 60


def test_self_exclusion_applies_to_the_statement_not_the_batch(
    plan_cache_backend: SqlServerBackend,
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> None:
    """The second way this check can empty itself out, found the hard way.

    `sys.dm_exec_sql_text` returns the whole submitted batch, comments and
    all. The check excludes its own statements the way the PostgreSQL backend
    excludes `%pg_stat_statements%` — but testing that pattern against the
    batch text throws away every statement in any batch that so much as
    mentions the DMV in a comment.

    Which the seed script does: section 7 opens by explaining that
    `sys.dm_exec_query_stats` is written by the execution engine, and that
    comment travels with all five workload statements. The exclusion, applied
    to the batch, deleted the busiest statement in the fixture and left a
    result that still looked entirely plausible.
    """
    batch = mssql_query(
        "SELECT COUNT_BIG(*) AS n "
        "FROM sys.dm_exec_query_stats AS qs "
        "CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st "
        "WHERE st.text LIKE ? AND st.text LIKE '%dm_exec_query_stats%'",
        "%AS matched%",
    )
    assert batch[0]["n"] > 0, (
        "the premise of this test is gone: the seeded workload's batch no "
        "longer mentions dm_exec_query_stats, so it can no longer catch a "
        "batch-wide exclusion. Find another batch that does, or delete this."
    )

    rows = slow_queries(plan_cache_backend, 50)
    matched = [row for row in rows if "AS matched" in row.query]
    assert matched, "the busiest seeded statement was excluded by its own batch's comments"
    assert matched[0].calls == 60
    # The exclusion still has to work on what it is aimed at.
    assert not any("dm_exec_query_stats" in row.query for row in rows)


def test_query_text_is_normalised_by_simple_parameterisation(
    plan_cache_backend: SqlServerBackend,
) -> None:
    """The text is what the engine recorded, not what the client sent.

    The seed sends `... WHERE customer_id = 42`. Simple parameterisation
    rewrites it before either view ever sees it, so it comes back bracket-
    quoted with the literal replaced by `@1`. Asserted rather than merely
    documented: anyone writing the next test here will reach for the text they
    sent, and this says plainly why that cannot work.
    """
    texts = [row.query for row in slow_queries(plan_cache_backend, 50)]
    parameterised = [text for text in texts if "COUNT_BIG(*) [n]" in text]

    assert parameterised, f"seeded statement (e) not found among {len(texts)} statements"
    statement = parameterised[0]
    assert "[customer_id]=@1" in statement
    assert "customer_id = 42" not in statement


# ---------------------------------------------------------------------------
# blocking_chains
# ---------------------------------------------------------------------------


def test_blocking_chains_reports_a_live_chain(
    backend: SqlServerBackend,
    blocking_chain: Callable[..., Any],
) -> None:
    """Two sessions, one lock, and the pair of session ids the fixture set up.

    `BlockingChain.blocked_pid` / `blocking_pid` are named for PostgreSQL
    backend pids; SQL Server session ids go in them unchanged, and this is
    where that mapping is pinned down.
    """
    with blocking_chain() as chain:
        rows = blocking_chains(backend)

        ours = [row for row in rows if row.blocked_pid == chain.blocked_spid]
        assert ours, (
            f"session {chain.blocked_spid} is blocked by {chain.blocker_spid} in "
            f"sys.dm_exec_requests, but the check reported {[r.blocked_pid for r in rows]}"
        )
        found = ours[0]
        assert found.blocking_pid == chain.blocker_spid
        assert found.blocked_user == "sa"
        assert found.blocking_user == "sa"
        assert "COUNT_BIG" in found.blocked_query
        assert found.blocked_seconds >= 0.0

        # The blocker is sleeping inside an open transaction — the classic
        # shape, and the one where sys.dm_exec_requests has no row for it at
        # all. Getting a state and a statement out of it is what the fallback
        # through sys.dm_exec_connections is for.
        assert found.blocking_state is not None
        assert "open transaction" in found.blocking_state
        assert found.blocking_query != ""


def test_blocking_chains_excludes_the_observers_own_session(
    backend: SqlServerBackend,
    blocking_chain: Callable[..., Any],
) -> None:
    ours = int(backend._scalar("SELECT @@SPID"))
    with blocking_chain() as chain:
        rows = blocking_chains(backend)
        # Sanity: the chain really was live while we looked.
        assert any(row.blocking_pid == chain.blocker_spid for row in rows)
        assert all(row.blocked_pid != ours for row in rows)
        assert all(row.blocked_pid != row.blocking_pid for row in rows)


def test_blocking_chains_is_empty_when_nothing_is_blocked(backend: SqlServerBackend) -> None:
    rows = blocking_chains(backend)
    assert isinstance(rows, list)
    assert all(row.blocking_pid != 0 for row in rows)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_slow_queries_denial_names_the_grant(unprivileged_dsn: str) -> None:
    """A missing grant is the operator's problem, so say which grant."""
    with connect(unprivileged_dsn) as denied, pytest.raises(CheckUnavailable) as raised:
        slow_queries(denied, 10)

    message = raised.value.full_message()
    assert raised.value.check == str(Check.SLOW_QUERIES)
    assert "GRANT" in message
    assert "STATE" in message


def test_plan_cache_denial_names_view_server_state(unprivileged_dsn: str) -> None:
    """The fallback path is refused by a different permission than Query Store.

    Query Store is a set of catalog views in the user database and is refused
    by VIEW DATABASE STATE; the plan cache is instance-wide and needs VIEW
    SERVER STATE. Telling someone to grant the wrong one is worse than saying
    nothing, because they will grant it and come back no better off.
    """
    with connect(unprivileged_dsn) as denied:
        denied._capabilities = dataclasses.replace(
            denied.capabilities, query_store_enabled=False, query_store_state=None
        )
        with pytest.raises(CheckUnavailable) as raised:
            slow_queries(denied, 10)

    assert "VIEW SERVER STATE" in raised.value.full_message()


def test_blocking_refuses_rather_than_reporting_a_clean_bill_of_health(
    unprivileged_dsn: str,
) -> None:
    """The failure this guards against is the one that looks like good news.

    `sys.dm_exec_requests` does not deny an under-privileged login — it
    succeeds and shows it only its own session. The check filters its own
    session out, so a blind connection would come back with an empty list and
    render as "nothing is blocked", on a server that may be gridlocked. It has
    to refuse instead.
    """
    with connect(unprivileged_dsn) as denied, pytest.raises(CheckUnavailable) as raised:
        blocking_chains(denied)

    assert raised.value.check == str(Check.BLOCKING)
    assert "VIEW SERVER STATE" in raised.value.full_message()


def test_an_unprivileged_login_really_can_read_dm_exec_requests(
    unprivileged_dsn: str,
) -> None:
    """The premise of the test above, asserted rather than assumed.

    If a future release starts denying this view outright, the permission
    probe in `blocking_chains` becomes redundant belt-and-braces rather than
    the thing standing between a blind connection and a false all-clear — and
    whoever notices should know which it is before deleting it.
    """
    with connect(unprivileged_dsn) as denied:
        visible = denied._query("SELECT session_id FROM sys.dm_exec_requests")
        assert visible, "expected an unprivileged login to still see its own request"
        assert denied._scalar("SELECT COUNT_BIG(*) FROM sys.dm_exec_sessions") == 1


@pytest.mark.parametrize("check", ["slow_queries", "blocking_chains"])
def test_a_real_failure_is_not_disguised_as_a_missing_grant(
    backend: SqlServerBackend, monkeypatch: pytest.MonkeyPatch, check: str
) -> None:
    """Only permission errors become CheckUnavailable; bugs still crash.

    SQLSTATE 42000 is SQL Server's catch-all for access *and* syntax errors,
    so a helper that swallowed the state rather than the wording would turn
    every typo in this module into a polite "grant yourself VIEW SERVER STATE"
    and make the bug invisible.
    """
    backend.capabilities  # resolve and cache before _query is replaced  # noqa: B018

    def explode(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        raise pyodbc.ProgrammingError(
            "42000",
            "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
            "Invalid column name 'no_such_column'. (207) (SQLExecDirectW)",
        )

    monkeypatch.setattr(backend, "_query", explode)

    with pytest.raises(pyodbc.ProgrammingError):
        if check == "slow_queries":
            slow_queries(backend, 10)
        else:
            blocking_chains(backend)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_the_mixin_claims_only_what_it_implements(backend: SqlServerBackend) -> None:
    """`supports` is the promise the base class refuses everything else on.

    Declared on the mixin rather than edited into `connection.py` so that the
    composed backend's `supports` is the union of what its mixins actually
    provide — a set that cannot drift into advertising a check no method
    answers.
    """
    assert QueryChecks.supports == frozenset({Check.SLOW_QUERIES, Check.BLOCKING})

    composed = QueryChecks(
        backend._conn, database=backend.database, host=backend.host, read_only=True
    )
    assert isinstance(composed, SqlServerBackend)
    # Same statements in the same order. Not the same objects: `total_ms` and
    # the percentages move between two readings of a live plan cache, so
    # comparing the dataclasses whole would fail for a reason that has nothing
    # to do with whether the delegation works.
    assert [row.query for row in composed.slow_queries(3)] == [
        row.query for row in slow_queries(backend, 3)
    ]
    assert composed.blocking_chains() == blocking_chains(backend)
