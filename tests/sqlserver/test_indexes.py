"""Index checks, against a real SQL Server.

Two rules govern everything in this file.

**Nothing here runs index DDL on `dbo.orders`.** The seeded database is
session-scoped and shared, and any CREATE INDEX, DROP INDEX or ALTER INDEX
REBUILD on a table discards that table's rows from
`sys.dm_db_missing_index_details` — measured: one real CREATE INDEX took the
count from 1 to 0. A test that rebuilt an index on `dbo.orders` would not fail;
it would quietly empty the missing-index evidence for every test that ran
afterwards. So every test that needs to create or drop an index does it on a
table of its own, via the `probe_table` fixture.

(`SET PARSEONLY ON` is not a workaround. Sent in the same batch as the
statement it is meant to suppress, it does not apply to that batch, and the
CREATE INDEX executes for real — which is how that measurement above came to
be taken by accident.)

**Most of these tests assert a refusal.** The PostgreSQL suite is shaped the
same way and for the same reason: a check that finds a dead index is useful,
and a check that offers a live one for deletion is worse than no check at all.
The tests that matter most here are the ones that prove an index in use, a
primary key, and a uniqueness-enforcing index are each kept out of the drop
path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any

import pytest

from db_perf_toolkit.backends.base import CheckUnavailable
from db_perf_toolkit.backends.sqlserver import indexes as index_checks
from db_perf_toolkit.backends.sqlserver.connection import connect
from db_perf_toolkit.backends.sqlserver.indexes import (
    ACTION_NONE,
    ACTION_REBUILD,
    ACTION_REORGANIZE,
    IndexChecks,
    recommended_action,
)
from db_perf_toolkit.models import Check
from db_perf_toolkit.safety import index_drop_refusal

#: What the seed builds, named once so a change to the scenario shows up here
#: as one edit rather than a scatter of string literals.
UNREAD_INDEX = "ix_orders_status"
UNIQUE_UNREAD_INDEX = "uq_orders_reference"
INDEX_IN_USE = "ix_orders_customer_id"
SEEDED_SEEKS = 30
FRAGMENTED_INDEX = "pk_line_items"


@pytest.fixture
def index_backend(seeded_sqlserver_dsn: str) -> Iterator[IndexChecks]:
    """The check mixin, bound to a real connection.

    `connect()` returns the plain backend; the composed class that will carry
    every mixin is assembled elsewhere. Wrapping the connection here tests the
    class this module actually ships, without depending on how the final
    backend is put together.
    """
    with connect(seeded_sqlserver_dsn) as backend:
        yield IndexChecks(backend._conn, database=backend.database, host=backend.host)


@pytest.fixture
def probe_table(mssql_connect: Callable[..., Any]) -> Iterator[Callable[..., str]]:
    """Create a scratch table shaped like `dbo.orders`, and drop it afterwards.

    Shaped like it on purpose: the missing-index pathology needs a selective
    unindexed column over enough rows that a seek would beat a scan, and
    reproducing that shape is what lets a test run generated DDL for real
    instead of eyeballing the string.

    20,000 rows is the measured floor — it produces the same suggestion as the
    seed's 200,000 and builds in about 0.15s.
    """
    conn = mssql_connect()
    cursor = conn.cursor()
    created: list[str] = []

    def _create(name: str, *, rows: int = 0) -> str:
        cursor.execute(f"DROP TABLE IF EXISTS dbo.{name}")
        cursor.execute(
            f"""
            CREATE TABLE dbo.{name} (
                id           int          NOT NULL CONSTRAINT pk_{name} PRIMARY KEY CLUSTERED,
                warehouse_id int          NOT NULL,
                total_cents  bigint       NOT NULL,
                status       varchar(20)  NOT NULL,
                reference    varchar(40)  NOT NULL,
                placed_at    datetime2(3) NOT NULL
            )
            """
        )
        created.append(name)
        if rows:
            cursor.execute(
                f"""
                WITH n AS (
                    SELECT TOP ({rows}) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS x
                    FROM sys.all_objects a CROSS JOIN sys.all_objects b
                )
                INSERT INTO dbo.{name}
                    (id, warehouse_id, total_cents, status, reference, placed_at)
                SELECT x, x % 40, (CAST(x AS bigint) * 37) % 250000,
                       CHOOSE(1 + x % 3, 'placed', 'shipped', 'cancelled'),
                       CONCAT('REF-', CAST(x AS varchar(12))),
                       DATEADD(minute, -x, CAST('2025-01-01T00:00:00' AS datetime2(3)))
                FROM n
                """
            )
            # Without this the optimiser still believes the table is empty, a
            # scan looks cheap, and it recommends nothing.
            cursor.execute(f"UPDATE STATISTICS dbo.{name} WITH FULLSCAN")
        return f"dbo.{name}"

    yield _create

    for name in reversed(created):
        with suppress(Exception):
            cursor.execute(f"DROP TABLE IF EXISTS dbo.{name}")


# ---------------------------------------------------------------------------
# What this backend claims
# ---------------------------------------------------------------------------


def test_supports_names_only_the_checks_implemented_here() -> None:
    assert IndexChecks.supports == frozenset(
        {Check.UNUSED_INDEXES, Check.MISSING_INDEXES, Check.FRAGMENTATION}
    )


def test_index_burden_is_refused_rather_than_filled_with_statement_counts(
    index_backend: IndexChecks,
) -> None:
    """`user_updates` counts statements, not rows — measured at 1 after 200,000.

    `TableIndexBurden.writes` is documented as row modifications, and the
    check's whole argument is per-row write amplification. Putting a statement
    count under that heading would be wrong by five orders of magnitude on a
    bulk load while looking entirely plausible, so this backend declines the
    check. The refusal is the feature.
    """
    assert Check.INDEX_BURDEN not in IndexChecks.supports

    with pytest.raises(CheckUnavailable) as exc:
        index_backend.index_burden(min_unused=2)
    assert "no equivalent" in exc.value.reason


# ---------------------------------------------------------------------------
# unused-indexes: what it finds
# ---------------------------------------------------------------------------


def test_unused_indexes_finds_the_index_nothing_reads(index_backend: IndexChecks) -> None:
    rows = index_backend.unused_indexes(max_scans=0)
    by_name = {row.index: row for row in rows}

    assert UNREAD_INDEX in by_name, "an index with zero reads was not reported"
    unread = by_name[UNREAD_INDEX]
    assert unread.table == "orders"
    assert unread.scans == 0
    assert unread.size_bytes > 0
    assert unread.size_pretty.endswith(("bytes", "kB", "MB", "GB", "TB"))
    # Written by every INSERT and read by nothing: the purest case this check
    # exists to find, and the reason `scans` excludes user_updates.
    assert index_drop_refusal(unread) is None


def test_unused_indexes_reports_an_index_the_dmv_has_never_recorded(
    index_backend: IndexChecks,
    probe_table: Callable[..., str],
    mssql_connect: Callable[..., Any],
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> None:
    """A nonclustered index with no usage row must still be reported as unused."""
    probe_table("probe_absent")
    mssql_connect().cursor().execute(
        "CREATE NONCLUSTERED INDEX ix_probe_absent_status ON dbo.probe_absent (status)"
    )

    absent = mssql_query(
        """
        SELECT u.object_id AS usage_row
        FROM sys.indexes AS i
        LEFT JOIN sys.dm_db_index_usage_stats AS u
               ON u.database_id = DB_ID()
              AND u.object_id = i.object_id
              AND u.index_id = i.index_id
        WHERE i.object_id = OBJECT_ID('dbo.probe_absent')
          AND i.name = 'ix_probe_absent_status'
        """
    )
    assert absent and absent[0]["usage_row"] is None, (
        "the index already has a row in sys.dm_db_index_usage_stats, so the "
        "LEFT JOIN this test exists to defend is not being exercised"
    )

    by_name = {row.index: row for row in index_backend.unused_indexes(max_scans=0)}
    assert "ix_probe_absent_status" in by_name, (
        "an index with no row in the usage DMV was not reported — this is the "
        "INNER JOIN failure: after a restart, every unused index disappears "
        "and the report reads like a clean bill of health"
    )
    assert by_name["ix_probe_absent_status"].scans == 0


# ---------------------------------------------------------------------------
# unused-indexes: what it refuses
# ---------------------------------------------------------------------------


def test_unused_indexes_does_not_offer_an_index_that_is_in_use(
    index_backend: IndexChecks,
    wait_for_sqlserver: Callable[..., None],
) -> None:
    """The refusal that matters most: 30 recorded seeks means hands off."""
    wait_for_sqlserver(
        lambda: any(
            row.index == INDEX_IN_USE and row.scans >= SEEDED_SEEKS
            for row in index_backend.unused_indexes(max_scans=10_000)
        )
    )

    offered = {row.index for row in index_backend.unused_indexes(max_scans=0)}
    assert INDEX_IN_USE not in offered, "an index with recorded seeks was offered for dropping"

    # And when the caller asks to see everything, it is there with its reads
    # intact — the check is filtering, not failing to see it.
    everything = {row.index: row for row in index_backend.unused_indexes(max_scans=10_000)}
    assert everything[INDEX_IN_USE].scans >= SEEDED_SEEKS
    assert index_drop_refusal(everything[INDEX_IN_USE]) is not None


def test_unused_indexes_never_offers_a_primary_key(index_backend: IndexChecks) -> None:
    """An unused primary key is still a primary key.

    Excluded in SQL rather than filtered afterwards, so that no caller of this
    check — including one that ignores the safety helpers — can ever see one.
    """
    names = {row.index for row in index_backend.unused_indexes(max_scans=10_000)}
    assert "pk_orders" not in names
    assert "pk_line_items" not in names


def test_unique_index_is_reported_but_refused_as_a_drop(index_backend: IndexChecks) -> None:
    """Reported because it is worth knowing; refused because it enforces something.

    `uq_orders_reference` is a bare CREATE UNIQUE INDEX, so SQL Server records
    `is_unique` without `is_unique_constraint` — the same split PostgreSQL has
    between a unique index and a unique constraint, and the reason
    `index_drop_refusal` tests both fields.
    """
    by_name = {row.index: row for row in index_backend.unused_indexes(max_scans=0)}

    assert UNIQUE_UNREAD_INDEX in by_name, "a unique index with no reads should still be reported"
    unique = by_name[UNIQUE_UNREAD_INDEX]
    assert unique.scans == 0
    assert unique.is_unique is True
    assert unique.enforces_constraint is False
    refusal = index_drop_refusal(unique)
    assert refusal is not None and "unique" in refusal


def test_constraint_backed_index_is_flagged_as_enforcing_a_constraint(
    index_backend: IndexChecks,
    probe_table: Callable[..., str],
    mssql_connect: Callable[..., Any],
) -> None:
    """A UNIQUE constraint sets the second flag as well, and is refused first.

    On its own table: adding a constraint to `dbo.orders` is index DDL and
    would wipe its missing-index rows.
    """
    probe_table("probe_constraint")
    mssql_connect().cursor().execute(
        "ALTER TABLE dbo.probe_constraint "
        "ADD CONSTRAINT uq_probe_constraint_reference UNIQUE (reference)"
    )

    by_name = {row.index: row for row in index_backend.unused_indexes(max_scans=0)}
    assert "uq_probe_constraint_reference" in by_name
    backed = by_name["uq_probe_constraint_reference"]
    assert backed.is_unique is True
    assert backed.enforces_constraint is True
    refusal = index_drop_refusal(backed)
    assert refusal is not None and "constraint" in refusal


# ---------------------------------------------------------------------------
# unused-indexes: the rollback statement
# ---------------------------------------------------------------------------


def test_definition_recreates_the_index_exactly(
    index_backend: IndexChecks,
    probe_table: Callable[..., str],
    mssql_connect: Callable[..., Any],
) -> None:
    """The manifest's rollback has to run, so run it.

    There is no pg_get_indexdef here — the statement is reassembled from the
    catalog — so asserting on its text would only prove the assembler agrees
    with itself. This drops the index and rebuilds it from the generated DDL,
    then regenerates the DDL from the rebuilt index: if anything were lost, the
    two strings would differ.

    The index is deliberately awkward — unique, two keys with a descending one,
    an included column, a filter and a non-default fill factor — because those
    are exactly the properties a naive reconstruction silently drops.
    """
    probe_table("probe_rollback", rows=100)
    cursor = mssql_connect().cursor()
    cursor.execute(
        """
        CREATE UNIQUE NONCLUSTERED INDEX ix_probe_rollback_awkward
            ON dbo.probe_rollback (warehouse_id ASC, placed_at DESC)
            INCLUDE (total_cents)
            WHERE status = 'placed'
            WITH (FILLFACTOR = 70)
        """
    )

    def definition() -> str:
        rows = index_backend.unused_indexes(max_scans=10_000)
        return next(row.definition for row in rows if row.index == "ix_probe_rollback_awkward")

    original = definition()
    assert original.startswith("CREATE UNIQUE NONCLUSTERED INDEX [ix_probe_rollback_awkward]")
    assert "[warehouse_id] ASC, [placed_at] DESC" in original
    assert "INCLUDE ([total_cents])" in original
    assert "FILLFACTOR = 70" in original
    assert "WHERE" in original

    cursor.execute("DROP INDEX ix_probe_rollback_awkward ON dbo.probe_rollback")
    cursor.execute(original)

    assert definition() == original, "the index rebuilt from the rollback DDL is not the same index"


# ---------------------------------------------------------------------------
# missing-indexes
# ---------------------------------------------------------------------------


def test_missing_indexes_finds_the_column_the_seed_left_unindexed(
    index_backend: IndexChecks,
) -> None:
    rows = [row for row in index_backend.missing_indexes(min_impact=0.0) if row.table == "orders"]

    assert rows, "the optimiser recorded no missing index for dbo.orders"
    assert len(rows) == 3, f"expected the seed's three suggestions, got {len(rows)}"
    assert any(row.equality_columns and "warehouse_id" in row.equality_columns for row in rows), (
        "the unindexed column the seed's workload filters on was not suggested"
    )
    assert all(row.schema == "dbo" for row in rows)
    assert all(row.impact_score > 0 for row in rows)
    assert all(row.seeks + row.scans > 0 for row in rows)
    assert all(row.last_seen is not None and row.last_seen.tzinfo is not None for row in rows)


def test_missing_indexes_rank_by_impact_and_honour_the_floor(
    index_backend: IndexChecks,
) -> None:
    """Ordering is the whole contract of a ranking signal."""
    rows = index_backend.missing_indexes(min_impact=0.0)
    scores = [row.impact_score for row in rows]
    assert scores == sorted(scores, reverse=True)

    floor = max(scores) / 2
    filtered = index_backend.missing_indexes(min_impact=floor)
    assert filtered, "the floor excluded everything, including the top suggestion"
    assert len(filtered) < len(rows)
    assert all(row.impact_score >= floor for row in filtered)


def test_missing_index_create_statement_actually_runs(
    index_backend: IndexChecks,
    probe_table: Callable[..., str],
    mssql_connect: Callable[..., Any],
    mssql_query: Callable[..., list[dict[str, Any]]],
    wait_for_sqlserver: Callable[..., None],
) -> None:
    """Generated DDL is only useful if the server accepts it.

    Run against a table of this test's own, because creating the index is what
    proves the statement — and creating it is also what would destroy
    `dbo.orders`' suggestions for every test that follows.
    """
    probe_table("probe_missing", rows=20_000)
    cursor = mssql_connect().cursor()
    # Two predicates and an ORDER BY: a single-predicate equality SELECT gets a
    # trivial plan, and trivial plans skip the missing-index feature entirely.
    cursor.execute(
        """
        SELECT TOP (100) p.id, p.reference, p.total_cents, p.placed_at
        FROM dbo.probe_missing AS p
        WHERE p.warehouse_id = 7 AND p.total_cents > 90000
        ORDER BY p.placed_at DESC
        """
    ).fetchall()

    wait_for_sqlserver(
        lambda: any(
            row.table == "probe_missing" for row in index_backend.missing_indexes(min_impact=0.0)
        )
    )
    suggestion = next(
        row for row in index_backend.missing_indexes(min_impact=0.0) if row.table == "probe_missing"
    )
    assert "[dbo].[probe_missing]" in suggestion.create_statement
    assert "warehouse_id" in suggestion.create_statement

    cursor.execute(suggestion.create_statement)

    created = mssql_query(
        """
        SELECT i.name AS index_name, COUNT(ic.column_id) AS key_columns
        FROM sys.indexes AS i
        JOIN sys.index_columns AS ic
          ON ic.object_id = i.object_id AND ic.index_id = i.index_id AND ic.key_ordinal > 0
        WHERE i.object_id = OBJECT_ID('dbo.probe_missing') AND i.is_primary_key = 0
        GROUP BY i.name
        """
    )
    assert len(created) == 1, "the generated statement did not create exactly one index"
    assert created[0]["key_columns"] >= 1


# ---------------------------------------------------------------------------
# fragmentation
# ---------------------------------------------------------------------------


def test_fragmentation_finds_the_index_the_seed_shredded(index_backend: IndexChecks) -> None:
    rows = index_backend.index_fragmentation(min_pct=5.0, min_pages=1000)
    by_name = {row.index: row for row in rows}

    assert FRAGMENTED_INDEX in by_name
    fragmented = by_name[FRAGMENTED_INDEX]
    assert fragmented.table == "line_items"
    # The seed's row-widening update is deterministic: 31.5% over 5,210 pages.
    assert 30.0 < fragmented.fragmentation_pct < 33.0
    assert 5_000 < fragmented.page_count < 5_500
    assert fragmented.recommended_action == ACTION_REBUILD


def test_limited_mode_reports_no_page_density_and_sampled_does(
    index_backend: IndexChecks,
) -> None:
    """The cost of the cheap reading, stated rather than faked.

    LIMITED reads only the parent level of the B-tree, so the server returns
    NULL for page density. Reporting a zero there would read as "no space
    used"; None says "not measured", which is what happened.
    """
    limited = index_backend.index_fragmentation(min_pct=5.0, min_pages=1000)
    assert limited and all(row.page_density_pct is None for row in limited)

    sampled = index_backend.index_fragmentation(min_pct=5.0, min_pages=1000, mode="SAMPLED")
    density = {row.index: row.page_density_pct for row in sampled}
    assert density[FRAGMENTED_INDEX] is not None
    assert 70.0 < float(density[FRAGMENTED_INDEX] or 0.0) < 85.0


def test_fragmentation_ignores_indexes_below_the_page_floor(index_backend: IndexChecks) -> None:
    """Fragmentation on a small index is noise, and rebuilding it buys nothing."""
    assert index_backend.index_fragmentation(min_pct=5.0, min_pages=1_000_000) == []

    # Lowering the floor is what makes the small indexes visible, so the floor
    # is doing the excluding rather than the query failing to see them.
    everything = index_backend.index_fragmentation(min_pct=0.0, min_pages=0)
    assert len(everything) > 1


def test_an_unknown_scan_mode_is_refused(index_backend: IndexChecks) -> None:
    """The mode is interpolated into the DMF call, so it is checked, not trusted."""
    with pytest.raises(ValueError, match="Unknown scan mode"):
        index_backend.index_fragmentation(min_pct=5.0, min_pages=0, mode="EVERYTHING")


@pytest.mark.parametrize(
    ("fragmentation_pct", "expected"),
    [
        (0.0, ACTION_NONE),
        (4.9, ACTION_NONE),
        (5.0, ACTION_REORGANIZE),
        (29.9, ACTION_REORGANIZE),
        (30.0, ACTION_REBUILD),
        (99.9, ACTION_REBUILD),
    ],
)
def test_recommended_action_uses_ola_hallengrens_thresholds(
    fragmentation_pct: float, expected: str
) -> None:
    """Reorganize above 5%, rebuild above 30% — his numbers, inclusive at the line."""
    assert recommended_action(fragmentation_pct) == expected


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_the_checks_work_as_plain_functions_on_any_backend(seeded_sqlserver_dsn: str) -> None:
    """Exposed as functions as well as methods, so composition order cannot matter."""
    with connect(seeded_sqlserver_dsn) as backend:
        assert index_checks.unused_indexes(backend, max_scans=0)
        assert index_checks.missing_indexes(backend, min_impact=0.0)
        assert index_checks.index_fragmentation(backend, min_pct=5.0, min_pages=1000)
