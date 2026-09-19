"""Slow-query and blocking checks for SQL Server.

Two checks live here because they read the same family of views — the
`sys.dm_exec_*` set, which is a live window onto the plan cache and the
scheduler — and because they share the same permission story and the same
trap about how SQL Server reports the database a statement belongs to.

THE THREE THINGS THAT MAKE THIS HARDER THAN THE POSTGRESQL EQUIVALENT
---------------------------------------------------------------------

1. There are two sources for query history, not one, and the better one is
   optional. `sys.dm_exec_query_stats` is always there and costs nothing, but
   it is the plan cache: evict the plan — memory pressure, a settings change,
   `DBCC FREEPROCCACHE`, a restart — and the history is gone with it. Query
   Store persists the same numbers in user tables and survives restarts, which
   is the only way to answer "was this query always slow?". So Query Store is
   preferred where `capabilities` reports it readable, and the plan cache is
   the fallback. The tool must never *require* Query Store: it is 2016+, off
   by default, and some deployments cannot afford its write cost.

2. `sys.dm_exec_sql_text.dbid` is NULL for ad-hoc batches. It is populated
   only for SQL that lives inside a module — a procedure, trigger or function
   — so scoping the plan cache with the obvious `WHERE st.dbid = DB_ID()`
   returns *zero rows* against a database with a live application workload,
   which is precisely the workload this check exists to find. Measured on the
   seeded test database: that filter returns 0 rows where the plan-attribute
   filter below returns 23. "No slow queries found" is the worst failure shape
   a check like this has, because it ships green. The database a plan was
   compiled against comes from `sys.dm_exec_plan_attributes` instead.

3. The text these views return is normalised, not verbatim. Simple
   parameterisation rewrites a statement before it is ever recorded, so a
   query sent as

       SELECT COUNT_BIG(*) AS n FROM dbo.orders WHERE customer_id = 42

   comes back as

       (@1 tinyint)SELECT COUNT_BIG(*) [n] FROM [dbo].[orders] WHERE [customer_id]=@1

   — bracket-quoted, the literal replaced by `@1`, and a parameter declaration
   prepended. That is a feature, since it is what lets `execution_count`
   accumulate across executions with different literals, but it means nothing
   may assert on the text it sent. (The two sources differ slightly even here:
   the plan-cache path slices out the statement using the offsets and so drops
   the `(@1 tinyint)` prefix, while Query Store keeps it.)

UNITS
-----

SQL Server reports these durations in **microseconds** — `total_elapsed_time`
and `total_worker_time` in the plan cache, `avg_duration` in Query Store — and
every one of them is divided by 1000 here, because `SlowQuery` is in
milliseconds and a three-orders-of-magnitude error in a "slow query" report is
not the kind of bug that announces itself.

Elapsed time is used rather than worker time. Worker time is CPU, and a query
that spends four minutes blocked on a lock burns almost none of it; PostgreSQL's
`pg_stat_statements.total_exec_time` is wall-clock, and matching it keeps the
two engines' reports comparable.
"""

from __future__ import annotations

from typing import Any

from db_perf_toolkit.backends.base import CheckUnavailable
from db_perf_toolkit.backends.sqlserver.connection import SqlServerBackend
from db_perf_toolkit.models import BlockingChain, Check, SlowQuery

#: What to tell someone whose login cannot read the execution DMVs. Both
#: grants are named because SQL Server 2022 split VIEW SERVER STATE into
#: narrower permissions, and a 2022 denial reads "VIEW SERVER PERFORMANCE
#: STATE permission was denied" — a message that does not mention the grant
#: most documentation tells you to use.
_SERVER_STATE_REMEDY = (
    "Grant the login permission to read the execution DMVs:\n"
    "  GRANT VIEW SERVER STATE TO [<login>];\n"
    "On SQL Server 2022 and later the narrower grant is enough:\n"
    "  GRANT VIEW SERVER PERFORMANCE STATE TO [<login>];\n"
    "On Azure SQL Database, where VIEW SERVER STATE is not grantable:\n"
    "  GRANT VIEW DATABASE STATE TO [<user>];"
)

#: Query Store lives in the user database rather than in the instance, so it
#: is refused by a different permission and needs its own sentence.
_QUERY_STORE_REMEDY = (
    "Grant the login permission to read this database's Query Store:\n"
    "  GRANT VIEW DATABASE STATE TO [<user>];\n"
    "On SQL Server 2022 and later:\n"
    "  GRANT VIEW DATABASE PERFORMANCE STATE TO [<user>];\n"
    "Or turn Query Store off for this database, and the plain plan-cache\n"
    "reading will be used instead."
)


def slow_queries(backend: SqlServerBackend, limit: int) -> list[SlowQuery]:
    """The `limit` statements that have spent the most wall-clock time.

    Reads Query Store where it is enabled and the plan cache where it is not,
    and says which in neither case — the two have genuinely different windows
    (a retention policy versus whatever is still cached) and the report's
    stats-window note is what carries that distinction.

    The percentages are shares of the whole filtered population, not of the
    `limit` rows returned, so they answer "how much of this database's time
    does this statement account for?" rather than "how much of the top ten?".
    They therefore sum to less than 100 whenever anything was truncated.

    Both paths exclude this tool's own statements, the way the PostgreSQL
    backend excludes `%pg_stat_statements%`: a diagnostic query that ranks
    itself is noise, and on a quiet server it ranks high. On the plan-cache
    path that exclusion has to be applied to the extracted statement rather
    than to the batch it came from — see `_PLAN_CACHE_SQL`, where getting it
    wrong silently deleted the busiest statement in the test fixture.
    """
    if backend.capabilities.query_store_enabled:
        with backend._denied_as_unavailable(str(Check.SLOW_QUERIES), _QUERY_STORE_REMEDY):
            rows = backend._query(_QUERY_STORE_SQL, limit)
    else:
        with backend._denied_as_unavailable(str(Check.SLOW_QUERIES), _SERVER_STATE_REMEDY):
            rows = backend._query(_PLAN_CACHE_SQL, limit)

    return [
        SlowQuery(
            query=_squash(str(row["query"])),
            calls=int(row["calls"] or 0),
            total_ms=float(row["total_ms"] or 0.0),
            mean_ms=float(row["mean_ms"] or 0.0),
            rows=int(row["rows"] or 0),
            pct_total_time=float(row["pct_total_time"] or 0.0),
        )
        for row in rows
    ]


def blocking_chains(backend: SqlServerBackend) -> list[BlockingChain]:
    """Sessions currently waiting on a lock held by another session.

    Much simpler than the PostgreSQL version, which has to resolve the wait
    graph with `pg_blocking_pids()`: `sys.dm_exec_requests.blocking_session_id`
    is the head of the chain, already resolved by the engine. The joins are
    only there to turn two session ids into something a human can act on.

    `BlockingChain.blocked_pid` and `blocking_pid` carry SQL Server session
    ids. The model is named for PostgreSQL backend pids and is deliberately
    not renamed — one shared shape is the point of it — but a "pid" here is a
    `session_id` (what `@@SPID` returns), not an operating-system process.

    Two asymmetries with PostgreSQL are worth knowing:

    * The blocker usually has no request at all. The classic chain is a
      session that ran something, left its transaction open, and went idle —
      it holds the lock while `sys.dm_exec_requests` has no row for it. So the
      blocker's statement comes from `sys.dm_exec_connections.most_recent_
      sql_handle`, which is the last batch that connection submitted. That is
      the same best-effort PostgreSQL makes when it shows `query` for an idle
      backend, and it is why `blocking_state` spells out "(open transaction)":
      an idle session holding locks is the finding, and its last statement is
      only the clue.

    * These are instance-wide views, so a chain in another database on the
      same instance appears here too. That matches `pg_stat_activity`, which
      is also not scoped to the current database, and it is the right answer:
      the two sessions are contending for the same server.
    """
    # Without VIEW SERVER STATE this query does not fail — it succeeds and
    # returns only the caller's own session, which this check then filters
    # out, leaving a clean "nothing is blocked". That is the same green-ship
    # failure as the dbid trap above, so visibility is established first and
    # a blind connection refuses the check instead of passing it.
    if not backend._scalar(_CAN_SEE_OTHER_SESSIONS_SQL):
        raise CheckUnavailable(
            str(Check.BLOCKING),
            "This login can only see its own session, so it cannot observe blocking.",
            _SERVER_STATE_REMEDY,
        )

    with backend._denied_as_unavailable(str(Check.BLOCKING), _SERVER_STATE_REMEDY):
        rows = backend._query(_BLOCKING_SQL)

    return [
        BlockingChain(
            blocked_pid=int(row["blocked_pid"]),
            blocked_user=_opt_str(row["blocked_user"]),
            blocked_query=_squash(str(row["blocked_query"] or "")),
            blocked_seconds=float(row["blocked_seconds"] or 0.0),
            blocking_pid=int(row["blocking_pid"]),
            blocking_user=_opt_str(row["blocking_user"]),
            blocking_query=_squash(str(row["blocking_query"] or "")),
            blocking_state=_opt_str(row["blocking_state"]),
        )
        for row in rows
    ]


class QueryChecks(SqlServerBackend):
    """The two execution-DMV checks, as a mixin the final backend composes.

    A subclass of `SqlServerBackend` rather than a bare mixin so that
    `self._query`, `self.capabilities` and `self._denied_as_unavailable`
    type-check without a block of `if TYPE_CHECKING` attribute stubs
    restating the base class's interface. The sibling check modules are the
    same shape, and the composed backend inherits from all of them; Python
    linearises that without complaint because they share one base.

    `supports` names only what this module implements. The composed class
    unions the mixins' sets, so `supports` can never advertise a check that
    nothing in the class can answer — which is the guarantee `Backend` was
    built around, and the reason this is declared here rather than edited
    into `connection.py` by three modules at once.
    """

    supports = frozenset({Check.SLOW_QUERIES, Check.BLOCKING})

    def slow_queries(self, limit: int) -> list[SlowQuery]:
        return slow_queries(self, limit)

    def blocking_chains(self) -> list[BlockingChain]:
        return blocking_chains(self)


#: The plan-cache reading.
#:
#: `attr` is the whole point of this query's shape. `st.dbid` sits right there
#: on the same row and is NULL for every ad-hoc batch, so filtering on it
#: returns nothing at all for an ordinary workload; the database a plan was
#: compiled against is only reliably available as a plan attribute.
#:
#: The SUBSTRING is SQL Server's standard incantation for pulling one
#: statement out of the batch text that contains it. The offsets are byte
#: offsets into an nvarchar, hence the halving, and -1 means "to the end".
#: Without it a batch of twenty statements reports all twenty as the text of
#: each one.
#:
#: And it is the reason for the CTE. `sys.dm_exec_sql_text` returns the whole
#: submitted batch — comments included — so excluding this tool's own
#: statements by testing `st.text` discards every statement in any batch that
#: merely *mentions* the DMV. That is not hypothetical: this repository's own
#: seed script introduces its workload with a comment explaining what
#: sys.dm_exec_query_stats records, and testing the batch text dropped the
#: most-executed statement in the fixture. The exclusion therefore applies to
#: the extracted statement, which an alias cannot do inside the same WHERE —
#: hence two levels. The outer WHERE still runs before the window function, so
#: the percentages remain shares of the population being reported on.
_PLAN_CACHE_SQL = """
    WITH statements AS (
        SELECT
            SUBSTRING(
                st.text,
                (qs.statement_start_offset / 2) + 1,
                ((CASE qs.statement_end_offset
                      WHEN -1 THEN DATALENGTH(st.text)
                      ELSE qs.statement_end_offset
                  END - qs.statement_start_offset) / 2) + 1
            )                                                      AS query,
            qs.execution_count                                     AS execution_count,
            qs.total_elapsed_time                                  AS total_us,
            qs.total_rows                                          AS total_rows
        FROM sys.dm_exec_query_stats AS qs
        CROSS APPLY sys.dm_exec_sql_text(qs.sql_handle) AS st
        CROSS APPLY (
            SELECT CAST(pa.value AS int) AS dbid
            FROM sys.dm_exec_plan_attributes(qs.plan_handle) AS pa
            WHERE pa.attribute = 'dbid'
        ) AS attr
        WHERE attr.dbid = DB_ID()
          AND st.text IS NOT NULL
    )
    SELECT TOP (?)
        s.query                                                    AS query,
        s.execution_count                                          AS calls,
        CAST(s.total_us / 1000.0 AS float)                         AS total_ms,
        CAST(s.total_us / 1000.0
             / NULLIF(s.execution_count, 0) AS float)              AS mean_ms,
        s.total_rows                                               AS [rows],
        CAST(100.0 * s.total_us
             / NULLIF(SUM(s.total_us) OVER (), 0) AS float)        AS pct_total_time
    FROM statements AS s
    WHERE s.query NOT LIKE '%dm_exec_query_stats%'
    ORDER BY s.total_us DESC
"""

#: The Query Store reading.
#:
#: Aggregation is unavoidable here, and is the one real complication. Query
#: Store keeps runtime statistics per (plan, time interval), so one query
#: executed across three one-minute buckets with two plans is six rows; the
#: report wants one line per statement. Summing `avg_duration *
#: count_executions` reconstructs the total the plan cache would have given.
#:
#: The GROUP BY is on `query_text_id` rather than on the text itself because
#: `query_sql_text` is nvarchar(max), which SQL Server refuses to group by —
#: hence the join back to `sys.query_store_query_text` in the outer query.
#: Grouping on the text id also merges the several `query_id`s that one
#: statement acquires when it is run under different SET options.
#:
#: No database filter: these are catalog views in the database being
#: diagnosed, so they are already scoped. The plan cache's trap does not
#: exist here.
_QUERY_STORE_SQL = """
    WITH totals AS (
        SELECT
            qq.query_text_id                                  AS query_text_id,
            SUM(rs.count_executions)                          AS calls,
            SUM(rs.avg_duration * rs.count_executions)        AS total_us,
            SUM(rs.avg_rowcount * rs.count_executions)        AS total_rows
        FROM sys.query_store_runtime_stats AS rs
        JOIN sys.query_store_plan  AS p  ON p.plan_id   = rs.plan_id
        JOIN sys.query_store_query AS qq ON qq.query_id = p.query_id
        GROUP BY qq.query_text_id
    )
    SELECT TOP (?)
        qt.query_sql_text                                     AS query,
        t.calls                                               AS calls,
        CAST(t.total_us / 1000.0 AS float)                    AS total_ms,
        CAST(t.total_us / 1000.0
             / NULLIF(t.calls, 0) AS float)                   AS mean_ms,
        CAST(t.total_rows AS bigint)                          AS [rows],
        CAST(100.0 * t.total_us
             / NULLIF(SUM(t.total_us) OVER (), 0) AS float)   AS pct_total_time
    FROM totals AS t
    JOIN sys.query_store_query_text AS qt ON qt.query_text_id = t.query_text_id
    WHERE qt.query_sql_text NOT LIKE '%query_store_runtime_stats%'
    ORDER BY t.total_us DESC
"""

#: Can this login see anything but itself?
#:
#: Three permissions are probed because the answer is spelled differently on
#: every platform: VIEW SERVER STATE on the box product, the narrower VIEW
#: SERVER PERFORMANCE STATE that SQL Server 2022 split out of it, and VIEW
#: DATABASE STATE on Azure SQL Database, where the server-level permission is
#: not grantable at all. HAS_PERMS_BY_NAME returns NULL rather than raising
#: for a permission name the server does not recognise, which is what makes
#: naming a 2022-only permission safe to send to a 2017 instance.
_CAN_SEE_OTHER_SESSIONS_SQL = """
    SELECT CASE
        WHEN COALESCE(HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE'), 0) = 1
          OR COALESCE(HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER PERFORMANCE STATE'), 0) = 1
          OR COALESCE(HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'VIEW DATABASE STATE'), 0) = 1
        THEN 1 ELSE 0
    END AS can_see_other_sessions
"""

#: The blocking chain.
#:
#: `blocked_seconds` is `wait_time`, which is how long this request has been
#: on its current wait — the column the report prints under "Waiting". It is
#: not the age of the statement: a request that briefly came off the wait list
#: and went back on reports the shorter, current wait. That is the honest
#: reading of what the engine records, and the alternative (now - start_time)
#: overstates it for anything that has been doing real work in between.
#:
#: Every join to the blocker is LEFT or OUTER APPLY, because the common case
#: is a blocker with no active request and, on a busy instance, a blocker that
#: disconnected between the two reads of the DMV. Losing the whole row when
#: the blocker's details cannot be resolved would hide the blocking itself.
_BLOCKING_SQL = """
    SELECT
        r.session_id                                                  AS blocked_pid,
        s.login_name                                                  AS blocked_user,
        SUBSTRING(
            st.text,
            (r.statement_start_offset / 2) + 1,
            ((CASE r.statement_end_offset
                  WHEN -1 THEN DATALENGTH(st.text)
                  ELSE r.statement_end_offset
              END - r.statement_start_offset) / 2) + 1
        )                                                             AS blocked_query,
        CAST(r.wait_time / 1000.0 AS float)                           AS blocked_seconds,
        r.blocking_session_id                                         AS blocking_pid,
        bs.login_name                                                 AS blocking_user,
        bst.text                                                      AS blocking_query,
        CASE
            WHEN br.status IS NOT NULL THEN br.status
            WHEN bs.open_transaction_count > 0
                THEN CONCAT(bs.status, ' (open transaction)')
            ELSE bs.status
        END                                                           AS blocking_state
    FROM sys.dm_exec_requests AS r
    JOIN sys.dm_exec_sessions AS s  ON s.session_id  = r.session_id
    LEFT JOIN sys.dm_exec_sessions AS bs ON bs.session_id = r.blocking_session_id
    LEFT JOIN sys.dm_exec_requests AS br ON br.session_id = r.blocking_session_id
    OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) AS st
    OUTER APPLY (
        SELECT TOP (1) c.most_recent_sql_handle
        FROM sys.dm_exec_connections AS c
        WHERE c.session_id = r.blocking_session_id
    ) AS bc
    OUTER APPLY sys.dm_exec_sql_text(
        COALESCE(br.sql_handle, bc.most_recent_sql_handle)
    ) AS bst
    WHERE r.blocking_session_id <> 0
      AND r.session_id <> @@SPID
    ORDER BY r.wait_time DESC
"""


def _squash(text: str) -> str:
    """Collapse whitespace so multi-line SQL fits a terminal row."""
    return " ".join(text.split())


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
