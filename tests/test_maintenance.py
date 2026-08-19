"""Write-mode tests.

The guards matter more than the happy path here. A tool that drops the wrong
index is worse than no tool, so most of these assert that something is
*refused*.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from db_perf_toolkit import manifest, safety
from db_perf_toolkit.backends import CheckUnavailable, connect
from db_perf_toolkit.models import StatsWindow

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_read_only_backend_refuses_to_execute(seeded_dsn: str) -> None:
    """The default connection must not be able to run maintenance at all."""
    with connect(seeded_dsn) as backend:
        indexes = backend.unused_indexes(max_scans=0)
        plan = backend.plan_drop_unused_indexes(indexes, min_size_bytes=0)
        with pytest.raises(RuntimeError, match="read-only"):
            backend.execute(plan.operations)


def test_plan_refuses_unique_and_constraint_backed_indexes(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        indexes = backend.unused_indexes(max_scans=0)
        plan = backend.plan_drop_unused_indexes(indexes, min_size_bytes=0)

    # Targets are quoted schema-qualified names, e.g. "public"."orders_pkey".
    def name_of(target: str) -> str:
        return target.rsplit(".", 1)[-1].strip('"')

    planned = {name_of(op.target) for op in plan.operations}
    refused = {name_of(t): r for t, r in plan.refused.items()}

    assert "orders_reference_key" not in planned
    assert "unique" in refused["orders_reference_key"]

    assert "orders_customer_slot_uq" not in planned
    assert "constraint" in refused["orders_customer_slot_uq"]

    # The one genuinely droppable index is still planned.
    assert "orders_status_idx" in planned


def test_size_floor_excludes_trivial_indexes(seeded_dsn: str) -> None:
    """Ola's MinNumberOfPages logic: maintenance on a tiny object is noise."""
    with connect(seeded_dsn) as backend:
        indexes = backend.unused_indexes(max_scans=0)
        generous = backend.plan_drop_unused_indexes(indexes, min_size_bytes=0)
        strict = backend.plan_drop_unused_indexes(indexes, min_size_bytes=10 * 1024**3)

    assert generous.operations, "expected at least one droppable index"
    assert not strict.operations, "a 10GB floor should exclude everything here"
    assert any("floor" in reason for reason in strict.refused.values())


def test_stats_window_guard_refuses_a_short_window() -> None:
    recent = StatsWindow(stats_reset=datetime.now(UTC) - timedelta(days=2), server_version="16")
    with pytest.raises(safety.RefusedError, match="too short a window"):
        safety.check_stats_window(recent, min_days=7)


def test_stats_window_guard_allows_a_long_window() -> None:
    old = StatsWindow(stats_reset=datetime.now(UTC) - timedelta(days=90), server_version="16")
    safety.check_stats_window(old, min_days=7)

    never_reset = StatsWindow(stats_reset=None, server_version="16")
    safety.check_stats_window(never_reset, min_days=7)


# ----------------------------------------------------------------------
# Execution and rollback
# ----------------------------------------------------------------------


def _index_exists(dsn: str, name: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        cur = conn.execute("SELECT 1 FROM pg_class WHERE relname = %s AND relkind = 'i'", (name,))
        return cur.fetchone() is not None


def test_drop_then_restore_round_trip(seeded_dsn: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A drop must be undoable from its manifest alone.

    This is the whole reason each operation carries its own CREATE statement:
    "we dropped it, work out how to rebuild it" is not a recoverable position.
    """
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute("DROP INDEX IF EXISTS orders_roundtrip_idx")
        setup.execute("CREATE INDEX orders_roundtrip_idx ON orders (total_cents)")

    assert _index_exists(seeded_dsn, "orders_roundtrip_idx")

    # Plan against a read-only connection, as the CLI does.
    with connect(seeded_dsn) as ro:
        indexes = [i for i in ro.unused_indexes(max_scans=0) if i.index == "orders_roundtrip_idx"]
        assert indexes, "the new index should register as unused"
        plan = ro.plan_drop_unused_indexes(indexes, min_size_bytes=0)

    assert len(plan.operations) == 1
    op = plan.operations[0]
    assert op.destructive is True
    assert op.rollback_sql is not None
    assert "CREATE INDEX" in op.rollback_sql

    path = tmp_path / "rollback.json"
    manifest.write(plan, path, host="localhost")

    with connect(seeded_dsn, read_only=False) as rw:
        results = rw.execute(plan.operations)
    assert all(err is None for _, err in results), results
    assert not _index_exists(seeded_dsn, "orders_roundtrip_idx"), "index should be gone"

    # Now restore from the manifest with no other information.
    data = manifest.read(path)
    statements = manifest.rollback_statements(data)
    assert len(statements) == 1

    with psycopg.connect(seeded_dsn, autocommit=True) as restore:
        restore.execute(statements[0][1])

    assert _index_exists(seeded_dsn, "orders_roundtrip_idx"), "restore failed"

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute("DROP INDEX IF EXISTS orders_roundtrip_idx")


def test_manifest_records_only_destructive_operations(seeded_dsn: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with connect(seeded_dsn) as backend:
        tables = backend.bloated_tables(min_dead_pct=0.0, min_dead_rows=0)
        vacuum_plan = backend.plan_vacuum(tables)

    path = tmp_path / "vacuum.json"
    manifest.write(vacuum_plan, path)
    data = manifest.read(path)

    assert vacuum_plan.operations, "expected a vacuum plan"
    assert all(not op.destructive for op in vacuum_plan.operations)
    assert data["operations"] == [], "non-destructive work needs no rollback"


def test_vacuum_executes_and_clears_dead_tuples(seeded_dsn: str) -> None:
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute("DROP TABLE IF EXISTS vacuum_target")
        setup.execute("CREATE TABLE vacuum_target AS SELECT i FROM generate_series(1, 5000) i")
        setup.execute("DELETE FROM vacuum_target WHERE i < 3000")
        setup.execute("SELECT pg_stat_force_next_flush()")

    with connect(seeded_dsn) as ro:
        tables = [
            t
            for t in ro.bloated_tables(min_dead_pct=1.0, min_dead_rows=10)
            if t.table == "vacuum_target"
        ]
        assert tables, "expected dead tuples before vacuuming"
        plan = ro.plan_vacuum(tables)

    assert "VACUUM (ANALYZE)" in plan.operations[0].sql

    with connect(seeded_dsn, read_only=False) as rw:
        results = rw.execute(plan.operations)
    assert all(err is None for _, err in results), results

    with psycopg.connect(seeded_dsn, autocommit=True) as check:
        check.execute("SELECT pg_stat_force_next_flush()")
        cur = check.execute(
            "SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'vacuum_target'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 0, "vacuum did not reclaim the dead tuples"

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute("DROP TABLE IF EXISTS vacuum_target")


def test_generated_sql_quotes_identifiers(seeded_dsn: str) -> None:
    """Identifiers come from the catalog, but must still be quoted properly."""
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute('DROP INDEX IF EXISTS "weird-Index name"')
        setup.execute('CREATE INDEX "weird-Index name" ON orders (status, customer_id)')

    with connect(seeded_dsn) as backend:
        indexes = [i for i in backend.unused_indexes(max_scans=0) if i.index == "weird-Index name"]
        assert indexes
        plan = backend.plan_drop_unused_indexes(indexes, min_size_bytes=0)

    assert '"weird-Index name"' in plan.operations[0].sql

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute('DROP INDEX IF EXISTS "weird-Index name"')


# ----------------------------------------------------------------------
# Index burden
# ----------------------------------------------------------------------


def test_index_burden_counts_unused_per_table(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        rows = backend.index_burden(min_unused=1)

    orders = [r for r in rows if r.table == "orders"]
    assert orders, "orders carries unused indexes and should appear"
    r = orders[0]
    assert r.index_count >= r.unused_count >= 1
    assert r.unused_bytes > 0
    assert r.writes > 0


def test_index_burden_ranks_by_write_amplification_not_size(seeded_dsn: str) -> None:
    """Ranking must follow write cost, not bytes.

    Two tables identical in shape and size, differing only in how much they
    are written to. A size-based ranking cannot tell them apart; that is the
    blind spot this check exists to cover.
    """
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute("DROP TABLE IF EXISTS burden_hot, burden_cold")
        for name in ("burden_hot", "burden_cold"):
            setup.execute(
                f"CREATE TABLE {name} (id serial PRIMARY KEY, a int, b int, c int, d int)"
            )
            for col in "abcd":
                setup.execute(f"CREATE INDEX {name}_{col}_idx ON {name} ({col})")
            setup.execute(
                f"INSERT INTO {name} (a, b, c, d) SELECT i, i, i, i FROM generate_series(1, 5000) i"
            )
        # Only the hot one takes ongoing churn.
        for _ in range(12):
            setup.execute("UPDATE burden_hot SET a = a + 1")
        setup.execute("VACUUM burden_hot")
        setup.execute("SELECT pg_stat_force_next_flush()")

    with connect(seeded_dsn) as backend:
        rows = backend.index_burden(min_unused=1)

    by_table = {r.table: r for r in rows}
    assert "burden_hot" in by_table and "burden_cold" in by_table
    hot, cold = by_table["burden_hot"], by_table["burden_cold"]

    assert hot.unused_count == cold.unused_count, "same shape, so same unused count"
    assert hot.writes > cold.writes, "the hot table should record more row writes"
    assert hot.redundant_writes > cold.redundant_writes

    order = [r.table for r in rows]
    assert order.index("burden_hot") < order.index("burden_cold")

    # And the whole result set is ordered by write cost, not size.
    costs = [r.redundant_writes for r in rows]
    assert costs == sorted(costs, reverse=True)

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute("DROP TABLE IF EXISTS burden_hot, burden_cold")


def test_index_burden_included_in_report(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        report = backend.report(limit=3)
    assert report.index_burden, "report should include index burden"
    assert "index-burden" not in report.skipped


def test_operation_target_is_quoted_and_parseable(seeded_dsn: str) -> None:
    """The manifest is the audit trail for destructive work.

    An unquoted "schema.name" cannot be parsed back apart once a name contains
    a dot, which is exactly the case in schemas that need this tool most.
    """
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute('DROP INDEX IF EXISTS "Odd.Dotted.Name"')
        setup.execute('CREATE INDEX "Odd.Dotted.Name" ON orders (status, total_cents)')

    with connect(seeded_dsn) as backend:
        indexes = [i for i in backend.unused_indexes(max_scans=0) if i.index == "Odd.Dotted.Name"]
        assert indexes
        plan = backend.plan_drop_unused_indexes(indexes, min_size_bytes=0)

    op = plan.operations[0]
    assert op.target == '"public"."Odd.Dotted.Name"'
    assert '"public"."Odd.Dotted.Name"' in op.sql
    assert op.rollback_sql is not None and '"Odd.Dotted.Name"' in op.rollback_sql

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute('DROP INDEX IF EXISTS "Odd.Dotted.Name"')


# ----------------------------------------------------------------------
# Free space
# ----------------------------------------------------------------------


def test_free_space_unavailable_without_pgstattuple(dsn: str) -> None:
    """A missing contrib extension must explain itself, not crash."""
    admin = psycopg.connect(dsn, autocommit=True)
    with admin:
        admin.execute("DROP DATABASE IF EXISTS no_pgstattuple")
        admin.execute("CREATE DATABASE no_pgstattuple")

    bare = dsn.rsplit("/", 1)[0] + "/no_pgstattuple"
    try:
        with connect(bare) as backend, pytest.raises(CheckUnavailable) as exc:
            backend.free_space()
        assert "pgstattuple" in exc.value.reason
        assert exc.value.remedy is not None and "CREATE EXTENSION" in exc.value.remedy
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin2:
            admin2.execute("DROP DATABASE IF EXISTS no_pgstattuple")


def test_free_space_sees_what_dead_tuples_cannot(seeded_dsn: str) -> None:
    """The whole reason this check exists.

    After a vacuum, dead tuples read zero while the file stays the same size.
    `bloat` therefore calls the table clean; `free-space` still sees the hole.
    """
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
        setup.execute("DROP TABLE IF EXISTS hollow")
        setup.execute(
            "CREATE TABLE hollow AS "
            "SELECT i, repeat('padding', 40) AS pad FROM generate_series(1, 200000) i"
        )
        setup.execute("DELETE FROM hollow WHERE i % 10 <> 0")

    # VACUUM cannot remove tuples still visible to any open snapshot, and a
    # pooled connection from an earlier test can hold one. Retry until the
    # horizon advances rather than assuming the first pass reclaims.
    deadline = time.monotonic() + 20
    dead = -1
    while time.monotonic() < deadline:
        with psycopg.connect(seeded_dsn, autocommit=True) as vac:
            vac.execute("VACUUM hollow")
            vac.execute("SELECT pg_stat_force_next_flush()")
            row = vac.execute(
                "SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'hollow'"
            ).fetchone()
        dead = int(row[0]) if row else -1
        if dead == 0:
            break
        time.sleep(0.5)
    assert dead == 0, f"vacuum never reclaimed the dead tuples ({dead} left)"

    with connect(seeded_dsn) as backend:
        bloat = [
            t
            for t in backend.bloated_tables(min_dead_pct=1.0, min_dead_rows=1)
            if t.table == "hollow"
        ]
        free = [
            f
            for f in backend.free_space(min_free_pct=10.0, min_table_bytes=1024 * 1024)
            if f.table == "hollow"
        ]

    assert not bloat, "dead tuples are gone after the vacuum, so bloat sees nothing"
    assert free, "but the file is still mostly empty and free-space must see it"
    assert free[0].free_pct > 50
    assert free[0].free_bytes > 0
    assert free[0].method == "exact"

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute("DROP TABLE IF EXISTS hollow")


def test_free_space_skips_tables_below_the_size_floor(seeded_dsn: str) -> None:
    """Scanning every small table costs more than rewriting them would save."""
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")

    with connect(seeded_dsn) as backend:
        everything = backend.free_space(min_free_pct=0.0, min_table_bytes=0)
        big_only = backend.free_space(min_free_pct=0.0, min_table_bytes=10 * 1024**3)

    assert everything, "expected at least one table with no floor"
    assert not big_only, "a 10GB floor should exclude every table here"


# ----------------------------------------------------------------------
# Timeouts
# ----------------------------------------------------------------------


def _guc(backend, name: str) -> str:  # type: ignore[no-untyped-def]
    with backend._conn.cursor() as cur:
        cur.execute(f"SHOW {name}")
        row = cur.fetchone()
    return str(next(iter(row.values())))


def test_read_connections_bound_statement_time(seeded_dsn: str) -> None:
    """A diagnostic must never become the incident it was called to find."""
    with connect(seeded_dsn) as backend:
        assert _guc(backend, "statement_timeout") == "30s"
        assert _guc(backend, "lock_timeout") == "10s"


def test_write_connections_do_not_bound_statement_time(seeded_dsn: str) -> None:
    """Maintenance runs as long as it takes.

    statement_timeout cancels VACUUM and REINDEX CONCURRENTLY like any other
    statement. A cancelled REINDEX CONCURRENTLY leaves an INVALID index that
    has to be dropped by hand, so bounding the *work* is the wrong guard here.
    """
    with connect(seeded_dsn, read_only=False) as backend:
        assert _guc(backend, "statement_timeout") == "0"
        # Waiting to start is still bounded — that is the risk worth capping.
        assert _guc(backend, "lock_timeout") == "10s"


def test_statement_timeout_would_cancel_a_vacuum(seeded_dsn: str) -> None:
    """Demonstrates why the split exists, by reintroducing the old behaviour."""
    from db_perf_toolkit.models import Operation

    # A private table: vacuuming a shared one would clear dead tuples that
    # other tests against this session-scoped database rely on.
    with psycopg.connect(seeded_dsn, autocommit=True) as setup:
        setup.execute("DROP TABLE IF EXISTS timeout_probe")
        setup.execute(
            "CREATE TABLE timeout_probe AS SELECT i, repeat('x', 100) AS pad "
            "FROM generate_series(1, 80000) i"
        )
        setup.execute("DELETE FROM timeout_probe WHERE i % 2 = 0")

    op = Operation(
        target="timeout_probe",
        description="probe",
        sql='VACUUM (ANALYZE) "public"."timeout_probe";',
        destructive=False,
    )
    with connect(seeded_dsn, read_only=False, statement_timeout_ms=1) as backend:
        results = backend.execute([op])
    assert results[0][1] is not None
    assert "timeout" in results[0][1].lower()

    # With the default, the same operation completes.
    with connect(seeded_dsn, read_only=False) as backend:
        results = backend.execute([op])
    assert results[0][1] is None, results

    with psycopg.connect(seeded_dsn, autocommit=True) as cleanup:
        cleanup.execute("DROP TABLE IF EXISTS timeout_probe")


def test_lock_timeout_gives_up_rather_than_queueing(seeded_dsn: str) -> None:
    """Blocked maintenance should fail fast, not join the queue.

    An ACCESS EXCLUSIVE request that waits blocks every lock request behind
    it, including plain SELECTs, so a maintenance command parked on a lock
    takes the table down before doing any work.
    """
    from db_perf_toolkit.models import Operation

    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with psycopg.connect(seeded_dsn) as holder:
            holder.execute("BEGIN")
            holder.execute("LOCK TABLE orders IN ACCESS EXCLUSIVE MODE")
            holder_ready.set()
            release.wait(timeout=30)
            holder.rollback()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert holder_ready.wait(timeout=10)

    try:
        op = Operation(
            target="orders",
            description="probe",
            sql='REINDEX INDEX CONCURRENTLY "public"."orders_status_idx";',
            destructive=False,
        )
        started = time.monotonic()
        with connect(seeded_dsn, read_only=False, lock_timeout_ms=750) as backend:
            results = backend.execute([op])
        waited = time.monotonic() - started

        assert results[0][1] is not None, "expected the lock wait to be given up on"
        assert "lock" in results[0][1].lower() or "timeout" in results[0][1].lower()
        assert waited < 15, f"gave up after {waited:.1f}s, which is not failing fast"
    finally:
        release.set()
        thread.join(timeout=15)
