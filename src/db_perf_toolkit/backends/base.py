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
    Report,
    SeqScanHotspot,
    SlowQuery,
    StatsWindow,
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
    """Read-only access to one database server's performance statistics."""

    #: Human-readable engine name, e.g. "PostgreSQL".
    engine: str

    @abstractmethod
    def stats_window(self) -> StatsWindow: ...

    @abstractmethod
    def slow_queries(self, limit: int) -> list[SlowQuery]: ...

    @abstractmethod
    def seq_scan_hotspots(self, min_seq_scans: int, min_rows: int) -> list[SeqScanHotspot]: ...

    @abstractmethod
    def unused_indexes(self, max_scans: int) -> list[UnusedIndex]: ...

    @abstractmethod
    def index_burden(self, min_unused: int) -> list[TableIndexBurden]: ...

    @abstractmethod
    def bloated_tables(self, min_dead_pct: float, min_dead_rows: int) -> list[BloatedTable]: ...

    @abstractmethod
    def blocking_chains(self) -> list[BlockingChain]: ...

    @abstractmethod
    def close(self) -> None: ...

    def report(self, limit: int) -> Report:
        """Run every check.

        One unavailable check must not abort the rest — a server without
        pg_stat_statements can still report bloat and blocking — so
        CheckUnavailable is recorded and the run continues.
        """
        report = Report(window=self.stats_window())

        try:
            report.slow_queries = self.slow_queries(limit)
        except CheckUnavailable as exc:
            report.skipped["slow-queries"] = exc.full_message()

        try:
            report.seq_scan_hotspots = self.seq_scan_hotspots(min_seq_scans=50, min_rows=1000)
        except CheckUnavailable as exc:
            report.skipped["seq-scans"] = exc.full_message()

        try:
            report.unused_indexes = self.unused_indexes(max_scans=0)
        except CheckUnavailable as exc:
            report.skipped["unused-indexes"] = exc.full_message()

        try:
            report.index_burden = self.index_burden(min_unused=2)
        except CheckUnavailable as exc:
            report.skipped["index-burden"] = exc.full_message()

        try:
            report.bloated_tables = self.bloated_tables(min_dead_pct=10.0, min_dead_rows=1000)
        except CheckUnavailable as exc:
            report.skipped["bloat"] = exc.full_message()

        # free-space is deliberately absent. It reads table data rather than
        # catalogs and can take minutes on a large database; a `report` that
        # sometimes takes seconds and sometimes takes ten minutes is a worse
        # tool than one that makes you ask for the expensive check.

        try:
            report.blocking_chains = self.blocking_chains()
        except CheckUnavailable as exc:
            report.skipped["blocking"] = exc.full_message()

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
