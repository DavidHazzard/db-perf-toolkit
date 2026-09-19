"""Integration tests against a real PostgreSQL server."""

from __future__ import annotations

import contextlib
import json
import threading
import time

import psycopg
import pytest

from db_perf_toolkit.backends import CheckUnavailable, connect
from db_perf_toolkit.backends.postgres import connect as connect_postgres
from db_perf_toolkit.render import to_json

pytestmark = pytest.mark.integration


def test_connection_is_genuinely_read_only(seeded_dsn: str) -> None:
    """The read-only claim must hold at the server, not by convention.

    This tool is pointed at production databases, so a bug in a catalog query
    must not be able to write.
    """
    # The concrete backend, because this asserts on the real connection
    # object — which only the PostgreSQL backend exposes.
    with connect_postgres(seeded_dsn) as backend:
        conn = backend._conn
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE should_never_exist (id int)")


def test_stats_window_reports_server_version(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        window = backend.stats_window()
    assert window.server_version.startswith("16")


def test_slow_queries_ranks_by_total_time(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        rows = backend.slow_queries(limit=20)

    assert rows, "pg_stat_statements recorded nothing"
    # Ordering is the entire contract of this check.
    totals = [r.total_ms for r in rows]
    assert totals == sorted(totals, reverse=True)
    assert all(r.calls > 0 for r in rows)
    # Percentages are a share of the whole, so none may exceed 100.
    assert all(0.0 <= r.pct_total_time <= 100.0 for r in rows)


def test_slow_queries_finds_the_repeated_scan(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        rows = backend.slow_queries(limit=50)

    scans = [r for r in rows if "total_cents" in r.query and "count" in r.query.lower()]
    assert scans, "the seeded sequential-scan workload is missing from pg_stat_statements"
    assert scans[0].calls >= 60


def test_slow_queries_unavailable_without_the_extension(dsn: str) -> None:
    """A database without pg_stat_statements must produce guidance, not a crash."""
    admin = psycopg.connect(dsn, autocommit=True)
    with admin:
        admin.execute("DROP DATABASE IF EXISTS no_pgss")
        admin.execute("CREATE DATABASE no_pgss")

    bare = dsn.rsplit("/", 1)[0] + "/no_pgss"
    try:
        with connect(bare) as backend, pytest.raises(CheckUnavailable) as exc:
            backend.slow_queries(limit=5)
        assert "pg_stat_statements" in exc.value.reason
        assert exc.value.remedy is not None
        assert "shared_preload_libraries" in exc.value.remedy
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin2:
            admin2.execute("DROP DATABASE IF EXISTS no_pgss")


def test_unused_indexes_never_suggests_dropping_a_primary_key(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        rows = backend.unused_indexes(max_scans=0)

    names = {r.index for r in rows}
    assert "orders_pkey" not in names, "a primary key must never be reported as droppable"


def test_unused_indexes_flags_constraint_backed_indexes_as_unsafe(seeded_dsn: str) -> None:
    """A unique index is not merely a read optimisation.

    Dropping it changes what the table will accept, so reporting it as
    droppable would be actively harmful advice.
    """
    with connect(seeded_dsn) as backend:
        rows = backend.unused_indexes(max_scans=0)

    by_name = {r.index: r for r in rows}

    # A bare CREATE UNIQUE INDEX writes no pg_constraint row, so
    # enforces_constraint is legitimately False — but it still enforces
    # uniqueness, and is_unique is what stops it being called droppable.
    assert "orders_reference_key" in by_name
    bare_unique = by_name["orders_reference_key"]
    assert bare_unique.is_unique is True
    assert bare_unique.enforces_constraint is False

    # A UNIQUE constraint creates both, exercising the pg_constraint join.
    assert "orders_customer_slot_uq" in by_name
    constraint_backed = by_name["orders_customer_slot_uq"]
    assert constraint_backed.is_unique is True
    assert constraint_backed.enforces_constraint is True

    assert "orders_status_idx" in by_name
    plain_idx = by_name["orders_status_idx"]
    assert plain_idx.is_unique is False
    assert plain_idx.enforces_constraint is False
    assert plain_idx.size_bytes > 0


def test_bloat_detects_the_deleted_rows(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        rows = backend.bloated_tables(min_dead_pct=1.0, min_dead_rows=100)

    orders = [r for r in rows if r.table == "orders"]
    assert orders, "the deleted rows did not register as dead tuples"
    assert orders[0].dead_rows > 0
    assert 0.0 < orders[0].dead_pct <= 100.0


def test_seq_scan_hotspots_finds_the_scanned_table(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        rows = backend.seq_scan_hotspots(min_seq_scans=10, min_rows=100)

    orders = [r for r in rows if r.table == "orders"]
    assert orders, "sequential scans on orders were not recorded"
    assert orders[0].seq_scans >= 10
    assert orders[0].avg_rows_per_scan > 0


def test_blocking_detects_a_real_lock_wait(seeded_dsn: str) -> None:
    """Hold a lock in one session and collide with it in another."""
    started = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with psycopg.connect(seeded_dsn) as holder:
            holder.execute("BEGIN")
            holder.execute("LOCK TABLE orders IN ACCESS EXCLUSIVE MODE")
            started.set()
            release.wait(timeout=20)
            holder.rollback()

    holder_thread = threading.Thread(target=hold_lock, daemon=True)
    holder_thread.start()
    assert started.wait(timeout=10), "lock holder never acquired the lock"

    blocked = psycopg.connect(seeded_dsn, autocommit=True)

    def get_blocked() -> None:
        # This statement is expected to block and then fail when the holder
        # rolls back; the failure is the point, not an error to surface.
        with contextlib.suppress(Exception):
            blocked.execute("SELECT count(*) FROM orders")

    blocked_thread = threading.Thread(target=get_blocked, daemon=True)
    blocked_thread.start()

    try:
        chains = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with connect(seeded_dsn) as backend:
                chains = backend.blocking_chains()
            if chains:
                break
            time.sleep(0.3)

        assert chains, "a genuine lock wait was not detected"
        chain = chains[0]
        assert chain.blocked_pid != chain.blocking_pid
        assert chain.blocked_seconds >= 0
    finally:
        release.set()
        holder_thread.join(timeout=10)
        blocked_thread.join(timeout=10)
        blocked.close()


def test_report_collects_every_check(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        report = backend.report(limit=5)

    assert report.window.server_version
    assert report.slow_queries
    assert report.unused_indexes
    assert not report.skipped, f"unexpected skips: {report.skipped}"


def test_report_records_skips_instead_of_aborting(dsn: str) -> None:
    """One unavailable check must not take the rest of the run with it."""
    admin = psycopg.connect(dsn, autocommit=True)
    with admin:
        admin.execute("DROP DATABASE IF EXISTS partial_checks")
        admin.execute("CREATE DATABASE partial_checks")

    bare = dsn.rsplit("/", 1)[0] + "/partial_checks"
    try:
        with connect(bare) as backend:
            report = backend.report(limit=5)

        assert "slow-queries" in report.skipped
        # The checks that do not need the extension still ran.
        assert report.blocking_chains == []
        assert report.bloated_tables == []
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin2:
            admin2.execute("DROP DATABASE IF EXISTS partial_checks")


def test_json_export_is_valid_and_serialises_datetimes(seeded_dsn: str) -> None:
    with connect(seeded_dsn) as backend:
        report = backend.report(limit=3)

    payload = json.loads(to_json(report))
    assert "window" in payload
    assert isinstance(payload["unused_indexes"], list)
    # stats_reset is a datetime and must survive serialisation.
    assert "stats_reset" in payload["window"]
