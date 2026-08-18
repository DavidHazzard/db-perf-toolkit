"""Write-mode tests.

The guards matter more than the happy path here. A tool that drops the wrong
index is worse than no tool, so most of these assert that something is
*refused*.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from db_perf_toolkit import manifest, safety
from db_perf_toolkit.backends import connect
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

    planned = {op.target.split(".")[-1] for op in plan.operations}
    refused = {t.split(".")[-1]: r for t, r in plan.refused.items()}

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
