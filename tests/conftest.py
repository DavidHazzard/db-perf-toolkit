"""Test fixtures backed by a real PostgreSQL server.

Mocked cursors would prove only that the code calls a cursor. The value of
this tool is in whether its catalog queries are correct, which can only be
established by running them against a live server.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg.rows import dict_row
from testcontainers.community.postgres import PostgresContainer

IMAGE = "postgres:16"

#: pg_stat_statements must be loaded at server start; CREATE EXTENSION alone
#: is not enough. track=all also records statements inside functions.
SERVER_COMMAND = (
    "postgres"
    " -c shared_preload_libraries=pg_stat_statements"
    " -c pg_stat_statements.track=all"
    # Keep autovacuum from tidying away the dead tuples the bloat test needs.
    " -c autovacuum=off"
)


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer(IMAGE, driver=None).with_command(SERVER_COMMAND)
    with container as running:
        yield running


@pytest.fixture(scope="session")
def dsn(pg_container: PostgresContainer) -> str:
    return pg_container.get_connection_url()


@pytest.fixture(scope="session")
def seeded_dsn(dsn: str) -> str:
    """A database with pg_stat_statements enabled and a workload applied."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

        conn.execute("""
            CREATE TABLE orders (
                id          bigserial PRIMARY KEY,
                customer_id bigint NOT NULL,
                reference   text NOT NULL,
                total_cents bigint NOT NULL,
                status      text NOT NULL
            )
        """)
        # Bare UNIQUE INDEX: enforces uniqueness but creates no pg_constraint
        # row, so it must report is_unique=True, enforces_constraint=False —
        # and still be refused as droppable on the strength of is_unique.
        conn.execute("CREATE UNIQUE INDEX orders_reference_key ON orders (reference)")
        # UNIQUE CONSTRAINT: creates both a constraint and its backing index,
        # so this one exercises the pg_constraint join.
        conn.execute("ALTER TABLE orders ADD CONSTRAINT orders_customer_slot_uq UNIQUE (id)")
        # Plain redundant index: genuinely droppable.
        conn.execute("CREATE INDEX orders_status_idx ON orders (status)")

        conn.execute("""
            INSERT INTO orders (customer_id, reference, total_cents, status)
            SELECT i % 500, 'REF-' || i, (i * 37) % 100000, 'placed'
            FROM generate_series(1, 20000) AS i
        """)

        # Sequential scans on an unindexed column.
        for _ in range(60):
            conn.execute("SELECT count(*) FROM orders WHERE total_cents > 50000")

        # Dead tuples, with autovacuum off so they persist.
        conn.execute("DELETE FROM orders WHERE customer_id < 100")

        _flush_stats(conn)

    return dsn


def _flush_stats(conn: psycopg.Connection[dict[str, object]]) -> None:
    """Push this backend's pending statistics to the collector.

    Statistics are accumulated per-backend and flushed on a timer, so a query
    issued immediately after a workload can legitimately see stale counters.
    Without this the table-level assertions are intermittently wrong — a
    genuinely flaky test rather than a real failure.
    """
    try:
        conn.execute("SELECT pg_stat_force_next_flush()")
    except psycopg.errors.UndefinedFunction:
        # Added in PostgreSQL 15; older servers flush on their own schedule.
        conn.rollback()
        time.sleep(1.0)


@pytest.fixture
def wait_for() -> Callable[[Callable[[], bool]], None]:
    """Poll until a statistics-dependent condition holds."""

    def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.25)
        raise AssertionError(f"condition never became true within {timeout}s")

    return _wait
