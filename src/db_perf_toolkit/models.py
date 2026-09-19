"""Engine-agnostic result types.

Backends translate their own catalog views into these, so the renderers and
the JSON export never learn anything about a specific database engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Check(StrEnum):
    """Every check the CLI can run.

    Backends declare which of these they support. A check a backend does not
    support is refused with an explanation, never silently absent — "this
    engine has no equivalent" is information, and a blank section is not.
    """

    SLOW_QUERIES = "slow-queries"
    SEQ_SCANS = "seq-scans"
    MISSING_INDEXES = "missing-indexes"
    UNUSED_INDEXES = "unused-indexes"
    INDEX_BURDEN = "index-burden"
    BLOAT = "bloat"
    FREE_SPACE = "free-space"
    FRAGMENTATION = "fragmentation"
    BLOCKING = "blocking"


@dataclass(frozen=True, slots=True)
class MissingIndex:
    """An index the server itself says is missing.

    SQL Server only, and the asymmetry is deliberate. `sys.dm_db_missing_index_details`
    is a genuine server-side recommendation built from optimiser activity.
    PostgreSQL has no equivalent, which is why its nearest check is called
    `seq-scans` and reports candidates for EXPLAIN rather than recommendations.
    Forcing the two engines to expose the same check would mean inventing one.
    """

    schema: str
    table: str
    equality_columns: str | None
    inequality_columns: str | None
    included_columns: str | None
    """Optimiser's own estimate of the percentage improvement, times seeks.
    Not a promise — it is a ranking signal, and the README says so."""
    impact_score: float
    seeks: int
    scans: int
    last_seen: datetime | None
    create_statement: str


@dataclass(frozen=True, slots=True)
class IndexFragmentation:
    """SQL Server's analogue of bloat, at the index rather than table level.

    Not interchangeable with `BloatedTable`: that counts dead tuples awaiting
    a vacuum, a concept SQL Server does not have. This measures how far an
    index's physical page order has drifted from its logical order.
    """

    schema: str
    table: str
    index: str
    fragmentation_pct: float
    page_count: int
    page_density_pct: float | None
    """Ola Hallengren's thresholds: reorganize above 5%, rebuild above 30%."""
    recommended_action: str


@dataclass(frozen=True, slots=True)
class SlowQuery:
    query: str
    calls: int
    total_ms: float
    mean_ms: float
    rows: int
    """Share of total execution time across all recorded statements."""
    pct_total_time: float


@dataclass(frozen=True, slots=True)
class SeqScanHotspot:
    """A table taking heavy sequential scans.

    Deliberately not called a "missing index". PostgreSQL has no equivalent of
    SQL Server's `sys.dm_db_missing_index_details`, so there is no server-side
    recommendation to report. This is a heuristic pointing at tables worth
    examining with EXPLAIN — presenting it as a recommendation would be a lie
    the tool cannot back up.
    """

    schema: str
    table: str
    seq_scans: int
    index_scans: int
    seq_rows_read: int
    avg_rows_per_scan: float
    live_rows: int
    size_pretty: str


@dataclass(frozen=True, slots=True)
class UnusedIndex:
    schema: str
    table: str
    index: str
    scans: int
    size_bytes: int
    size_pretty: str
    definition: str
    is_unique: bool
    """True when the index backs a UNIQUE or EXCLUDE constraint.

    Such an index cannot simply be dropped — it enforces the constraint, so
    dropping it changes what data the table will accept.
    """
    enforces_constraint: bool


@dataclass(frozen=True, slots=True)
class BloatedTable:
    schema: str
    table: str
    live_rows: int
    dead_rows: int
    dead_pct: float
    size_pretty: str
    last_autovacuum: datetime | None
    last_vacuum: datetime | None


@dataclass(frozen=True, slots=True)
class TableIndexBurden:
    """Per-table index cost, which per-index reporting cannot express.

    A size floor is the right heuristic for *maintenance* — rebuilding a tiny
    index really is pointless — but it is the wrong one for *dropping*. The
    dominant cost of a redundant index is not the disk it occupies, it is the
    B-tree write every INSERT, UPDATE and DELETE pays into it forever. Ten
    useless 16KB indexes on a hot table cost ten extra writes per row, and
    every one of them looks individually harmless.
    """

    schema: str
    table: str
    index_count: int
    unused_count: int
    unused_bytes: int
    index_bytes: int
    heap_bytes: int
    """Modifications recorded against the table. See `writes_unit` — the two
    engines do not count the same thing, and this figure is only comparable
    within one engine."""
    writes: int

    """What `writes` counts: "rows" or "statements".

    PostgreSQL's n_tup_ins/upd/del are per-row. SQL Server's
    sys.dm_db_index_usage_stats.user_updates is per-STATEMENT — measured at 1
    after a 200,000-row INSERT. Putting a statement count under a field
    documented as rows would be wrong by five orders of magnitude on a bulk
    load, and this check's whole argument is per-row write amplification.
    A backend that cannot supply row counts should say so here rather than
    substitute a number that reads the same and means something else.
    """
    writes_unit: str = "rows"

    @property
    def redundant_writes(self) -> int:
        """Index writes that bought nothing, in `writes_unit` units."""
        return self.unused_count * self.writes

    @property
    def index_to_heap_pct(self) -> float:
        if self.heap_bytes == 0:
            return 0.0
        return 100.0 * self.index_bytes / self.heap_bytes


@dataclass(frozen=True, slots=True)
class TableFreeSpace:
    """Space a table would return to the operating system if rewritten.

    Distinct from `BloatedTable`, which counts dead tuples — churn awaiting a
    vacuum. Once vacuumed, dead tuples read zero while the file stays exactly
    as large, because plain VACUUM marks space reusable rather than returning
    it. This is the measurement that survives a vacuum, and the only basis on
    which recommending a rewrite is honest rather than a guess.
    """

    schema: str
    table: str
    table_bytes: int
    live_pct: float
    dead_pct: float
    free_bytes: int
    free_pct: float
    """How the figure was obtained: "exact" scans every page, "approx" uses
    the visibility map and reports what fraction it actually read."""
    method: str
    scanned_pct: float | None = None


@dataclass(frozen=True, slots=True)
class BlockingChain:
    blocked_pid: int
    blocked_user: str | None
    blocked_query: str
    blocked_seconds: float
    blocking_pid: int
    blocking_user: str | None
    blocking_query: str
    blocking_state: str | None


@dataclass(frozen=True, slots=True)
class StatsWindow:
    """When the statistics counters were last reset.

    Every cumulative check — unused indexes especially — is meaningless
    without this. An index that looks unused may simply have had its counters
    reset an hour ago.
    """

    stats_reset: datetime | None
    server_version: str
    """How the window was established, for the note printed above every report.

    PostgreSQL reads pg_stat_database.stats_reset. SQL Server has no such
    column: its usage counters reset when the service restarts, so the window
    is bounded by sqlserver_start_time. Same guard, different mechanism, and
    the consequence of ignoring it is worse on SQL Server — after a failover
    every index looks unused.
    """
    window_source: str = "stats_reset"


@dataclass(frozen=True, slots=True)
class Operation:
    """One maintenance action, planned but not yet run.

    Planning and execution are separated so `--script`, `--dry-run` and
    `--execute` all consume the same objects. It is also what lets the
    SQL Server backend orchestrate Ola Hallengren's procedures through the
    identical pipeline — there the `sql` is an EXEC of IndexOptimize rather
    than DDL of our own.
    """

    target: str
    description: str
    sql: str
    destructive: bool
    """SQL that undoes this operation, where an undo exists.

    For a dropped index this is its full CREATE statement, captured from
    pg_get_indexdef *before* the drop. Without it, "we deleted the index and
    you can work out how to rebuild it" is not a recoverable position.
    """
    rollback_sql: str | None = None


@dataclass(slots=True)
class Plan:
    """A set of operations plus why each one is or is not included."""

    engine: str
    database: str
    operations: list[Operation] = field(default_factory=list)
    """Candidates deliberately excluded, mapped to the reason."""
    refused: dict[str, str] = field(default_factory=dict)

    @property
    def destructive_operations(self) -> list[Operation]:
        return [op for op in self.operations if op.destructive]


@dataclass(slots=True)
class Report:
    """Everything a full run collected, for `dbperf report` and JSON export."""

    window: StatsWindow
    slow_queries: list[SlowQuery] = field(default_factory=list)
    seq_scan_hotspots: list[SeqScanHotspot] = field(default_factory=list)
    missing_indexes: list[MissingIndex] = field(default_factory=list)
    index_fragmentation: list[IndexFragmentation] = field(default_factory=list)
    unused_indexes: list[UnusedIndex] = field(default_factory=list)
    index_burden: list[TableIndexBurden] = field(default_factory=list)
    bloated_tables: list[BloatedTable] = field(default_factory=list)
    free_space: list[TableFreeSpace] = field(default_factory=list)
    blocking_chains: list[BlockingChain] = field(default_factory=list)
    """Checks that could not run, mapped to why — e.g. a missing extension."""
    skipped: dict[str, str] = field(default_factory=dict)
