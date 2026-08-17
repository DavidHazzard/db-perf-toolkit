"""PostgreSQL backend.

Every query here is read-only against catalog and statistics views. The
connection is opened read-only at the server level, so the tool cannot write
even if a query were wrong — it is expected to be pointed at production.
"""

from __future__ import annotations

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from db_perf_toolkit.backends.base import Backend, CheckUnavailable
from db_perf_toolkit.models import (
    BloatedTable,
    BlockingChain,
    SeqScanHotspot,
    SlowQuery,
    StatsWindow,
    UnusedIndex,
)

APPLICATION_NAME = "db-perf-toolkit"

#: pg_stat_statements renamed its timing columns in PostgreSQL 13:
#: total_time -> total_exec_time, mean_time -> mean_exec_time. Querying the
#: wrong pair fails outright, so the names are chosen from the server version.
_PGSS_RENAME_VERSION = 13


class PostgresBackend(Backend):
    engine = "PostgreSQL"

    def __init__(self, conn: psycopg.Connection[dict[str, object]]) -> None:
        self._conn = conn
        self._server_version_num = conn.info.server_version

    @property
    def _major(self) -> int:
        return self._server_version_num // 10000

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def stats_window(self) -> StatsWindow:
        raw = self._conn.info.parameter_status("server_version") or str(self._major)
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT stats_reset FROM pg_stat_database WHERE datname = current_database()"
            )
            row = cur.fetchone()
        reset = row["stats_reset"] if row else None
        return StatsWindow(stats_reset=reset, server_version=raw)  # type: ignore[arg-type]

    def _has_extension(self, name: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (name,))
            return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    def slow_queries(self, limit: int) -> list[SlowQuery]:
        if not self._has_extension("pg_stat_statements"):
            raise CheckUnavailable(
                "slow-queries",
                "The pg_stat_statements extension is not installed on this database.",
                "Add it to shared_preload_libraries in postgresql.conf, restart the\n"
                "server, then run: CREATE EXTENSION pg_stat_statements;",
            )

        total_col = "total_exec_time" if self._major >= _PGSS_RENAME_VERSION else "total_time"
        mean_col = "mean_exec_time" if self._major >= _PGSS_RENAME_VERSION else "mean_time"

        query = sql.SQL("""
            SELECT
                query,
                calls,
                {total} AS total_ms,
                {mean}  AS mean_ms,
                rows,
                100.0 * {total} / NULLIF(SUM({total}) OVER (), 0) AS pct_total_time
            FROM pg_stat_statements
            WHERE query NOT ILIKE %s
            ORDER BY {total} DESC
            LIMIT %s
        """).format(total=sql.Identifier(total_col), mean=sql.Identifier(mean_col))

        try:
            with self._conn.cursor() as cur:
                cur.execute(query, ("%pg_stat_statements%", limit))
                rows = cur.fetchall()
        except psycopg.errors.InsufficientPrivilege as exc:
            raise CheckUnavailable(
                "slow-queries",
                "Not permitted to read pg_stat_statements.",
                "Grant pg_read_all_stats to this role:\n  GRANT pg_read_all_stats TO <role>;",
            ) from exc

        return [
            SlowQuery(
                query=_squash(str(row["query"])),
                calls=int(row["calls"]),  # type: ignore[call-overload]
                total_ms=float(row["total_ms"]),  # type: ignore[arg-type]
                mean_ms=float(row["mean_ms"]),  # type: ignore[arg-type]
                rows=int(row["rows"]),  # type: ignore[call-overload]
                pct_total_time=float(row["pct_total_time"] or 0.0),  # type: ignore[arg-type]
            )
            for row in rows
        ]

    def seq_scan_hotspots(self, min_seq_scans: int, min_rows: int) -> list[SeqScanHotspot]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    relname                                   AS table_name,
                    seq_scan                                  AS seq_scans,
                    COALESCE(idx_scan, 0)                     AS index_scans,
                    seq_tup_read                              AS seq_rows_read,
                    n_live_tup                                AS live_rows,
                    seq_tup_read::float8 / NULLIF(seq_scan, 0) AS avg_rows_per_scan,
                    pg_size_pretty(pg_total_relation_size(relid)) AS size_pretty
                FROM pg_stat_user_tables
                WHERE seq_scan >= %s
                  AND n_live_tup >= %s
                ORDER BY seq_tup_read DESC
                LIMIT 50
                """,
                (min_seq_scans, min_rows),
            )
            rows = cur.fetchall()

        return [
            SeqScanHotspot(
                table=str(row["table_name"]),
                seq_scans=int(row["seq_scans"]),  # type: ignore[call-overload]
                index_scans=int(row["index_scans"]),  # type: ignore[call-overload]
                seq_rows_read=int(row["seq_rows_read"]),  # type: ignore[call-overload]
                avg_rows_per_scan=float(row["avg_rows_per_scan"] or 0.0),  # type: ignore[arg-type]
                live_rows=int(row["live_rows"]),  # type: ignore[call-overload]
                size_pretty=str(row["size_pretty"]),
            )
            for row in rows
        ]

    def unused_indexes(self, max_scans: int) -> list[UnusedIndex]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.relname                              AS table_name,
                    s.indexrelname                         AS index_name,
                    s.idx_scan                             AS scans,
                    pg_relation_size(s.indexrelid)         AS size_bytes,
                    pg_size_pretty(pg_relation_size(s.indexrelid)) AS size_pretty,
                    pg_get_indexdef(s.indexrelid)          AS definition,
                    i.indisunique                          AS is_unique,
                    (c.conindid IS NOT NULL)               AS enforces_constraint
                FROM pg_stat_user_indexes s
                JOIN pg_index i ON i.indexrelid = s.indexrelid
                LEFT JOIN pg_constraint c ON c.conindid = s.indexrelid
                -- Primary keys are excluded outright: an unused primary key is
                -- still a primary key, and suggesting it as droppable would be
                -- actively dangerous advice.
                WHERE NOT i.indisprimary
                  AND s.idx_scan <= %s
                ORDER BY pg_relation_size(s.indexrelid) DESC
                """,
                (max_scans,),
            )
            rows = cur.fetchall()

        return [
            UnusedIndex(
                table=str(row["table_name"]),
                index=str(row["index_name"]),
                scans=int(row["scans"]),  # type: ignore[call-overload]
                size_bytes=int(row["size_bytes"]),  # type: ignore[call-overload]
                size_pretty=str(row["size_pretty"]),
                definition=str(row["definition"]),
                is_unique=bool(row["is_unique"]),
                enforces_constraint=bool(row["enforces_constraint"]),
            )
            for row in rows
        ]

    def bloated_tables(self, min_dead_pct: float, min_dead_rows: int) -> list[BloatedTable]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    relname     AS table_name,
                    n_live_tup  AS live_rows,
                    n_dead_tup  AS dead_rows,
                    100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0) AS dead_pct,
                    pg_size_pretty(pg_total_relation_size(relid)) AS size_pretty,
                    last_autovacuum,
                    last_vacuum
                FROM pg_stat_user_tables
                WHERE n_dead_tup >= %s
                  AND 100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0) >= %s
                ORDER BY dead_pct DESC
                """,
                (min_dead_rows, min_dead_pct),
            )
            rows = cur.fetchall()

        return [
            BloatedTable(
                table=str(row["table_name"]),
                live_rows=int(row["live_rows"]),  # type: ignore[call-overload]
                dead_rows=int(row["dead_rows"]),  # type: ignore[call-overload]
                dead_pct=float(row["dead_pct"] or 0.0),  # type: ignore[arg-type]
                size_pretty=str(row["size_pretty"]),
                last_autovacuum=row["last_autovacuum"],  # type: ignore[arg-type]
                last_vacuum=row["last_vacuum"],  # type: ignore[arg-type]
            )
            for row in rows
        ]

    def blocking_chains(self) -> list[BlockingChain]:
        # pg_blocking_pids() resolves the whole wait graph server-side, which
        # is both simpler and more accurate than joining pg_locks by hand —
        # that approach misses several lock modes.
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    blocked.pid       AS blocked_pid,
                    blocked.usename   AS blocked_user,
                    blocked.query     AS blocked_query,
                    EXTRACT(EPOCH FROM (now() - blocked.query_start))::float8 AS blocked_seconds,
                    blocker.pid       AS blocking_pid,
                    blocker.usename   AS blocking_user,
                    blocker.query     AS blocking_query,
                    blocker.state     AS blocking_state
                FROM pg_stat_activity AS blocked
                CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking(pid)
                JOIN pg_stat_activity AS blocker ON blocker.pid = blocking.pid
                WHERE blocked.pid <> pg_backend_pid()
                ORDER BY blocked_seconds DESC NULLS LAST
                """
            )
            rows = cur.fetchall()

        return [
            BlockingChain(
                blocked_pid=int(row["blocked_pid"]),  # type: ignore[call-overload]
                blocked_user=_opt_str(row["blocked_user"]),
                blocked_query=_squash(str(row["blocked_query"])),
                blocked_seconds=float(row["blocked_seconds"] or 0.0),  # type: ignore[arg-type]
                blocking_pid=int(row["blocking_pid"]),  # type: ignore[call-overload]
                blocking_user=_opt_str(row["blocking_user"]),
                blocking_query=_squash(str(row["blocking_query"])),
                blocking_state=_opt_str(row["blocking_state"]),
            )
            for row in rows
        ]


def _squash(text: str) -> str:
    """Collapse whitespace so multi-line SQL fits a terminal row."""
    return " ".join(text.split())


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def connect(dsn: str, *, connect_timeout: int = 10) -> PostgresBackend:
    """Open a connection the server itself will refuse writes on.

    This tool is pointed at production databases, so read-only must be
    enforced by the server rather than by the discipline of the queries above.

    `Connection.read_only` alone is NOT sufficient: it configures transactions
    that psycopg opens itself, and in autocommit mode psycopg opens none, so
    the attribute is silently inert and writes succeed. Setting the session
    default is what actually applies to the implicit transaction wrapping each
    statement. Both are set — the attribute keeps the intent visible to anyone
    reading the connection, the SET is what enforces it.

    `application_name` makes the tool identifiable in pg_stat_activity to
    whoever is watching the server.
    """
    conn = psycopg.connect(
        dsn,
        autocommit=True,
        connect_timeout=connect_timeout,
        application_name=APPLICATION_NAME,
        row_factory=dict_row,
    )
    conn.read_only = True
    conn.execute("SET SESSION default_transaction_read_only = on")
    return PostgresBackend(conn)
