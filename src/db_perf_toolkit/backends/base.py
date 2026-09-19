"""Backend interface.

Each engine's introspection SQL shares nothing with the others, so backends
own their queries outright and translate results into the shared models. The
CLI and renderers depend only on this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType

from db_perf_toolkit.models import (
    BloatedTable,
    BlockingChain,
    Check,
    IndexFragmentation,
    MissingIndex,
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
        return CheckUnavailable(
            str(check),
            f"{self.engine} has no equivalent of this check.",
            f"Supported here: {', '.join(sorted(str(c) for c in self.supports))}",
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

    def free_space(self, **kwargs: object) -> list[TableFreeSpace]:
        raise self._unsupported(Check.FREE_SPACE)

    def index_fragmentation(self, min_pct: float, min_pages: int) -> list[IndexFragmentation]:
        raise self._unsupported(Check.FRAGMENTATION)

    def blocking_chains(self) -> list[BlockingChain]:
        raise self._unsupported(Check.BLOCKING)

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

    def __enter__(self) -> Backend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
