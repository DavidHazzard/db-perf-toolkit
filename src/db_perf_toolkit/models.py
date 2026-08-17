"""Engine-agnostic result types.

Backends translate their own catalog views into these, so the renderers and
the JSON export never learn anything about a specific database engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


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

    table: str
    seq_scans: int
    index_scans: int
    seq_rows_read: int
    avg_rows_per_scan: float
    live_rows: int
    size_pretty: str


@dataclass(frozen=True, slots=True)
class UnusedIndex:
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
    table: str
    live_rows: int
    dead_rows: int
    dead_pct: float
    size_pretty: str
    last_autovacuum: datetime | None
    last_vacuum: datetime | None


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


@dataclass(slots=True)
class Report:
    """Everything a full run collected, for `dbperf report` and JSON export."""

    window: StatsWindow
    slow_queries: list[SlowQuery] = field(default_factory=list)
    seq_scan_hotspots: list[SeqScanHotspot] = field(default_factory=list)
    unused_indexes: list[UnusedIndex] = field(default_factory=list)
    bloated_tables: list[BloatedTable] = field(default_factory=list)
    blocking_chains: list[BlockingChain] = field(default_factory=list)
    """Checks that could not run, mapped to why — e.g. a missing extension."""
    skipped: dict[str, str] = field(default_factory=dict)
