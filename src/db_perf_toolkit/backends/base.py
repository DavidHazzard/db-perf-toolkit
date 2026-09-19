"""Backend interface.

Each engine's introspection SQL shares nothing with the others, so backends
own their queries outright and translate results into the shared models. The
CLI and renderers depend only on this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Self

from db_perf_toolkit.models import (
    BloatedTable,
    BlockingChain,
    Check,
    IndexFragmentation,
    MissingIndex,
    Operation,
    Plan,
    Report,
    SeqScanHotspot,
    SlowQuery,
    StatsWindow,
    TableFreeSpace,
    TableIndexBurden,
    UnusedIndex,
)


class CheckUnavailable(Exception):
    """A check cannot run on this server, with a reason worth showing.

    Raised for missing extensions or insufficient privileges — conditions the
    user can act on. It is not an error in the tool, so the CLI reports it as
    a skipped check rather than a crash.
    """

    def __init__(self, check: str, reason: str, remedy: str | None = None) -> None:
        self.check = check
        self.reason = reason
        self.remedy = remedy
        super().__init__(f"{check}: {reason}")

    def full_message(self) -> str:
        return f"{self.reason}\n{self.remedy}" if self.remedy else self.reason


#: The nearest check on the *other* engine, for checks one engine cannot answer.
#:
#: These are neighbours, not equivalents, and the wording says so on purpose.
#: `free-space` and `fragmentation` both describe physical storage drift but
#: measure different things, and `seq-scans` is a weaker signal than the
#: optimiser's own recommendations — presenting either pair as a substitution
#: would be the kind of false equivalence this tool exists to avoid. The point
#: is to hand someone the next thing to try rather than a list of eight
#: unrelated check names.
_NEAREST_EQUIVALENT: dict[Check, tuple[Check, str]] = {
    Check.FRAGMENTATION: (
        Check.FREE_SPACE,
        "measures space a rewrite would reclaim, not page-order drift",
    ),
    Check.FREE_SPACE: (
        Check.FRAGMENTATION,
        "measures page-order drift, not space a rewrite would reclaim",
    ),
    Check.MISSING_INDEXES: (
        Check.SEQ_SCANS,
        "reports tables worth an EXPLAIN, not recommendations from the optimiser",
    ),
    Check.BLOAT: (
        Check.FRAGMENTATION,
        "measures page-order drift; this engine has no dead tuples to count",
    ),
}


class Backend(ABC):
    """Read-only access to one database server's performance statistics.

    Checks are declared, not assumed. `supports` says which of them this
    engine can answer; everything else raises CheckUnavailable explaining
    that the engine has no equivalent. That is a deliberate choice over
    forcing parity: SQL Server has a real missing-index DMV and PostgreSQL
    does not, and pretending otherwise would mean inventing a recommendation
    one of them cannot support.
    """

    #: Human-readable engine name, e.g. "PostgreSQL".
    engine: str

    #: Checks this backend can answer. Anything absent is refused.
    supports: frozenset[Check] = frozenset()

    # ------------------------------------------------------------------
    # Required of every backend
    # ------------------------------------------------------------------

    @abstractmethod
    def stats_window(self) -> StatsWindow: ...

    @abstractmethod
    def close(self) -> None: ...

    # ------------------------------------------------------------------
    # Checks. Overridden where supported; refused where not.
    # ------------------------------------------------------------------

    def _unsupported(self, check: Check) -> CheckUnavailable:
        lines = []
        neighbour = _NEAREST_EQUIVALENT.get(check)
        if neighbour and neighbour[0] in self.supports:
            lines.append(f"Closest here: `dbperf {neighbour[0]}` — {neighbour[1]}.")
        lines.append(f"Supported here: {', '.join(sorted(str(c) for c in self.supports))}")
        return CheckUnavailable(
            str(check),
            f"{self.engine} has no equivalent of this check.",
            "\n".join(lines),
        )

    def slow_queries(self, limit: int) -> list[SlowQuery]:
        raise self._unsupported(Check.SLOW_QUERIES)

    def seq_scan_hotspots(self, min_seq_scans: int, min_rows: int) -> list[SeqScanHotspot]:
        raise self._unsupported(Check.SEQ_SCANS)

    def missing_indexes(self, min_impact: float) -> list[MissingIndex]:
        raise self._unsupported(Check.MISSING_INDEXES)

    def unused_indexes(self, max_scans: int) -> list[UnusedIndex]:
        raise self._unsupported(Check.UNUSED_INDEXES)

    def index_burden(self, min_unused: int) -> list[TableIndexBurden]:
        raise self._unsupported(Check.INDEX_BURDEN)

    def bloated_tables(self, min_dead_pct: float, min_dead_rows: int) -> list[BloatedTable]:
        raise self._unsupported(Check.BLOAT)

    def free_space(
        self,
        *,
        min_free_pct: float = 20.0,
        min_table_bytes: int = 0,
        approx_above_bytes: int = 0,
        exact: bool = False,
    ) -> list[TableFreeSpace]:
        raise self._unsupported(Check.FREE_SPACE)

    def index_fragmentation(self, min_pct: float, min_pages: int) -> list[IndexFragmentation]:
        raise self._unsupported(Check.FRAGMENTATION)

    def blocking_chains(self) -> list[BlockingChain]:
        raise self._unsupported(Check.BLOCKING)

    # ------------------------------------------------------------------
    # Maintenance
    #
    # Declared here rather than only on the concrete backends so the CLI can
    # hold a Backend and still be type-checked. Engines that cannot perform an
    # operation refuse it exactly as an unsupported check does; nothing here
    # touches the server, since planning is separate from execution.
    # ------------------------------------------------------------------

    #: Whether this backend can run maintenance at all.
    supports_maintenance: bool = False

    def _no_maintenance(self, what: str) -> CheckUnavailable:
        return CheckUnavailable(
            what,
            f"{self.engine} maintenance is not implemented in this backend.",
            None,
        )

    def plan_vacuum(self, tables: list[BloatedTable], *, analyze: bool = True) -> Plan:
        raise self._no_maintenance("vacuum")

    def plan_reindex(self, indexes: list[UnusedIndex]) -> Plan:
        raise self._no_maintenance("reindex")

    def plan_drop_unused_indexes(
        self, indexes: list[UnusedIndex], *, min_size_bytes: int = 0
    ) -> Plan:
        raise self._no_maintenance("drop-unused-indexes")

    def plan_index_maintenance(
        self,
        *,
        databases: str | None = None,
        fragmentation_level_1: int = 5,
        fragmentation_level_2: int = 30,
        min_number_of_pages: int = 1000,
        execute: bool = False,
    ) -> Plan:
        """Plan a reorganize/rebuild pass over this database's indexes.

        Declared on the seam for the same reason as the rest of this section:
        without it the CLI cannot reach the operation while holding a Backend,
        and SQL Server's IndexOptimize orchestration was unreachable for
        exactly that reason until this was added.

        PostgreSQL refuses it rather than approximating. Its REINDEX has no
        fragmentation thresholds to honour, so mapping these parameters onto
        it would mean accepting arguments it then ignores — `reindex` is the
        honest command there.
        """
        raise self._no_maintenance("index-maintenance")

    def execute(self, operations: list[Operation]) -> list[tuple[Operation, str | None]]:
        raise self._no_maintenance("execute")

    # ------------------------------------------------------------------
    # Combined run
    # ------------------------------------------------------------------

    def report(self, limit: int) -> Report:
        """Run every supported check.

        One unavailable check must not abort the rest — a server without
        pg_stat_statements can still report bloat and blocking — so
        CheckUnavailable is recorded and the run continues.

        free-space and fragmentation are deliberately excluded: both read
        table or index data rather than catalogs and can take minutes on a
        large database. A report that sometimes takes seconds and sometimes
        ten minutes is a worse tool than one that makes you ask.
        """
        report = Report(window=self.stats_window())

        runnable: list[tuple[Check, str, object]] = [
            (Check.SLOW_QUERIES, "slow_queries", lambda: self.slow_queries(limit)),
            (
                Check.SEQ_SCANS,
                "seq_scan_hotspots",
                lambda: self.seq_scan_hotspots(min_seq_scans=50, min_rows=1000),
            ),
            (
                Check.MISSING_INDEXES,
                "missing_indexes",
                lambda: self.missing_indexes(min_impact=0.0),
            ),
            (Check.UNUSED_INDEXES, "unused_indexes", lambda: self.unused_indexes(max_scans=0)),
            (Check.INDEX_BURDEN, "index_burden", lambda: self.index_burden(min_unused=2)),
            (
                Check.BLOAT,
                "bloated_tables",
                lambda: self.bloated_tables(min_dead_pct=10.0, min_dead_rows=1000),
            ),
            (Check.BLOCKING, "blocking_chains", lambda: self.blocking_chains()),
        ]

        for check, attr, run in runnable:
            if check not in self.supports:
                continue
            try:
                setattr(report, attr, run())  # type: ignore[operator]
            except CheckUnavailable as exc:
                report.skipped[str(check)] = exc.full_message()

        return report

    def __enter__(self) -> Self:
        """Self, not Backend: `with connect_postgres(...) as b` must keep the
        concrete type, or every PostgreSQL-only attribute becomes invisible to
        the type checker the moment it passes through a `with`."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
