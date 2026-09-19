"""Terminal and JSON rendering.

Renderers take the shared models only, so adding a second engine needs no
changes here.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from db_perf_toolkit.models import (
    BloatedTable,
    BlockingChain,
    Report,
    SeqScanHotspot,
    SlowQuery,
    StatsWindow,
    TableFreeSpace,
    TableIndexBurden,
    UnusedIndex,
)

QUERY_PREVIEW_CHARS = 70

#: Terminal tables are capped; JSON export never is. On a real database
#: `unused-indexes` can return hundreds of rows, and a 600-line wall of text
#: is not a report. The cap is always stated — a silently truncated list
#: reads as "that is everything", which is worse than no list.
DEFAULT_ROW_CAP = 25


def _capped(rows: list[Any], cap: int | None) -> tuple[list[Any], int]:
    """Return (visible rows, number hidden)."""
    if cap is None or len(rows) <= cap:
        return rows, 0
    return rows[:cap], len(rows) - cap


def _note_hidden(table: Table, hidden: int, columns: int, hint: str) -> None:
    if hidden:
        table.add_section()
        note = Text(f"… and {hidden:,} more — {hint}", style="yellow")
        table.add_row(note, *[""] * (columns - 1))


def _truncate(text: str, width: int = QUERY_PREVIEW_CHARS) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _bytes(n: int) -> str:
    for unit, size in (("GB", 1024**3), ("MB", 1024**2), ("kB", 1024)):
        if n >= size:
            return f"{n / size:.0f} {unit}"
    return f"{n} B"


def _count(n: int) -> str:
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:,}"


def _ms(value: float) -> str:
    if value >= 60_000:
        return f"{value / 60_000:.1f} min"
    if value >= 1_000:
        return f"{value / 1_000:.2f} s"
    return f"{value:.1f} ms"


def stats_window_note(window: StatsWindow) -> Text:
    """Cumulative counters mean nothing without the window they cover.

    An index can look unused simply because the statistics were reset an hour
    ago, so the reset time is shown alongside every cumulative check rather
    than buried in documentation.
    """
    note = Text()
    note.append(f"{window.server_version}", style="bold")
    if window.stats_reset is None:
        note.append("  ·  statistics never reset (counters cover full server uptime)", style="dim")
    else:
        age = datetime.now(window.stats_reset.tzinfo) - window.stats_reset
        days = age.days
        span = f"{days}d" if days else f"{age.seconds // 3600}h"
        note.append(
            f"  ·  statistics reset {window.stats_reset:%Y-%m-%d %H:%M} ({span} of data)",
            style="dim",
        )
    return note


def slow_queries_table(rows: list[SlowQuery]) -> Table:
    table = Table(title="Slowest statements by total execution time", title_justify="left")
    table.add_column("Query", overflow="ellipsis", max_width=QUERY_PREVIEW_CHARS)
    table.add_column("Calls", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Mean", justify="right")
    table.add_column("Rows", justify="right")
    table.add_column("% time", justify="right")

    for r in rows:
        table.add_row(
            _truncate(r.query),
            f"{r.calls:,}",
            _ms(r.total_ms),
            _ms(r.mean_ms),
            f"{r.rows:,}",
            f"{r.pct_total_time:.1f}%",
        )
    return table


def seq_scan_table(rows: list[SeqScanHotspot], cap: int | None = DEFAULT_ROW_CAP) -> Table:
    visible, hidden = _capped(rows, cap)
    table = Table(
        title="Sequential scan hotspots — candidates for EXPLAIN, not index recommendations",
        title_justify="left",
    )
    table.add_column("Table")
    table.add_column("Seq scans", justify="right")
    table.add_column("Idx scans", justify="right")
    table.add_column("Rows read", justify="right")
    table.add_column("Avg/scan", justify="right")
    table.add_column("Live rows", justify="right")
    table.add_column("Size", justify="right")

    for r in visible:
        table.add_row(
            r.table,
            f"{r.seq_scans:,}",
            f"{r.index_scans:,}",
            f"{r.seq_rows_read:,}",
            f"{r.avg_rows_per_scan:,.0f}",
            f"{r.live_rows:,}",
            r.size_pretty,
        )
    _note_hidden(table, hidden, 7, "use --json for the full list")
    return table


def unused_indexes_table(rows: list[UnusedIndex], cap: int | None = DEFAULT_ROW_CAP) -> Table:
    visible, hidden = _capped(rows, cap)
    total = f" ({len(rows):,} found)" if hidden else ""
    table = Table(title=f"Indexes with no recorded scans{total}", title_justify="left")
    table.add_column("Table")
    table.add_column("Index")
    table.add_column("Scans", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Safe to drop?")

    for r in visible:
        if r.enforces_constraint:
            verdict = Text("no — backs a constraint", style="red")
        elif r.is_unique:
            verdict = Text("no — unique index", style="red")
        else:
            verdict = Text("likely", style="green")
        table.add_row(r.table, r.index, f"{r.scans:,}", r.size_pretty, verdict)
    _note_hidden(table, hidden, 5, "use --json for the full list")
    return table


def index_burden_table(rows: list[TableIndexBurden], cap: int | None = DEFAULT_ROW_CAP) -> Table:
    """Per-table index cost.

    Ranked by redundant writes rather than size, because that is the cost a
    per-index size floor cannot see: an unused 16kB index on a hot table is
    paid for on every insert, forever.
    """
    visible, hidden = _capped(rows, cap)
    total = f" ({len(rows):,} tables)" if hidden else ""
    table = Table(
        title=f"Index burden — write cost of indexes nothing reads{total}",
        title_justify="left",
    )
    table.add_column("Table")
    table.add_column("Indexes", justify="right")
    table.add_column("Unused", justify="right")
    table.add_column("Wasted", justify="right")
    table.add_column("Idx/heap", justify="right")
    # The header names the unit rather than assuming rows: a backend that can
    # only report statements must not have that read as a row count.
    unit = visible[0].writes_unit if visible else "rows"
    table.add_column(
        f"{unit.capitalize()[:-1] if unit.endswith('s') else unit} writes", justify="right"
    )
    table.add_column("Redundant idx writes", justify="right")

    for r in visible:
        unused = Text(
            f"{r.unused_count}/{r.index_count}",
            style="red" if r.unused_count >= r.index_count - 1 else "yellow",
        )
        ratio = Text(
            f"{r.index_to_heap_pct:.0f}%",
            style="red" if r.index_to_heap_pct >= 100 else "",
        )
        table.add_row(
            r.table,
            str(r.index_count),
            unused,
            _bytes(r.unused_bytes),
            ratio,
            _count(r.writes),
            _count(r.redundant_writes),
        )
    _note_hidden(table, hidden, 7, "use --json for the full list")
    return table


def free_space_table(rows: list[TableFreeSpace], cap: int | None = DEFAULT_ROW_CAP) -> Table:
    """Space a rewrite would return to the operating system.

    Not the same question as `bloat`, which counts dead tuples. After a
    vacuum those read zero while the file stays the same size.
    """
    visible, hidden = _capped(rows, cap)
    total = f" ({len(rows):,} tables)" if hidden else ""
    table = Table(
        title=f"Reclaimable space — what a rewrite would return{total}",
        title_justify="left",
    )
    table.add_column("Table")
    table.add_column("Size", justify="right")
    table.add_column("Live", justify="right")
    table.add_column("Dead", justify="right")
    table.add_column("Free", justify="right")
    table.add_column("Reclaimable", justify="right")
    table.add_column("Measured")

    for r in visible:
        free = Text(f"{r.free_pct:.0f}%", style="red" if r.free_pct >= 40 else "yellow")
        how = r.method
        if r.method == "approx" and r.scanned_pct is not None:
            how = f"approx ({r.scanned_pct:.0f}% scanned)"
        table.add_row(
            r.table,
            _bytes(r.table_bytes),
            f"{r.live_pct:.0f}%",
            f"{r.dead_pct:.0f}%",
            free,
            _bytes(r.free_bytes),
            how,
        )
    _note_hidden(table, hidden, 7, "use --json for the full list")
    return table


def bloat_table(rows: list[BloatedTable], cap: int | None = DEFAULT_ROW_CAP) -> Table:
    visible, hidden = _capped(rows, cap)
    total = f" ({len(rows):,} found)" if hidden else ""
    table = Table(title=f"Tables with a high dead tuple ratio{total}", title_justify="left")
    table.add_column("Table")
    table.add_column("Live", justify="right")
    table.add_column("Dead", justify="right")
    table.add_column("Dead %", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Last autovacuum")

    for r in visible:
        pct = Text(f"{r.dead_pct:.1f}%", style="red" if r.dead_pct >= 20 else "yellow")
        last = r.last_autovacuum or r.last_vacuum
        table.add_row(
            r.table,
            f"{r.live_rows:,}",
            f"{r.dead_rows:,}",
            pct,
            r.size_pretty,
            f"{last:%Y-%m-%d %H:%M}" if last else "never",
        )
    _note_hidden(table, hidden, 6, "use --json for the full list")
    return table


def blocking_table(rows: list[BlockingChain]) -> Table:
    table = Table(title="Sessions currently blocked", title_justify="left")
    table.add_column("Blocked", justify="right")
    table.add_column("Waiting")
    table.add_column("Blocked query", overflow="ellipsis", max_width=40)
    table.add_column("Blocked by", justify="right")
    table.add_column("Blocker state")
    table.add_column("Blocker query", overflow="ellipsis", max_width=40)

    for r in rows:
        table.add_row(
            str(r.blocked_pid),
            f"{r.blocked_seconds:.1f}s",
            _truncate(r.blocked_query, 40),
            str(r.blocking_pid),
            r.blocking_state or "—",
            _truncate(r.blocking_query, 40),
        )
    return table


def render_report(report: Report, console: Console) -> None:
    console.print(stats_window_note(report.window))
    console.print()

    sections: list[tuple[str, list[Any], Any]] = [
        ("slow-queries", report.slow_queries, slow_queries_table),
        ("seq-scans", report.seq_scan_hotspots, seq_scan_table),
        ("unused-indexes", report.unused_indexes, unused_indexes_table),
        ("index-burden", report.index_burden, index_burden_table),
        ("bloat", report.bloated_tables, bloat_table),
        ("blocking", report.blocking_chains, blocking_table),
    ]

    for name, rows, builder in sections:
        if name in report.skipped:
            continue
        if rows:
            console.print(builder(rows))
        else:
            console.print(Text(f"{name}: nothing found", style="dim"))
        console.print()

    for name, reason in report.skipped.items():
        console.print(Text(f"{name} skipped — {reason}", style="yellow"))


def _default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"not JSON serialisable: {type(obj).__name__}")


def to_json(payload: object) -> str:
    """Serialise models for piping into dashboards or diffing between runs."""
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        data: Any = dataclasses.asdict(payload)
    elif isinstance(payload, list):
        data = [
            dataclasses.asdict(item)
            if dataclasses.is_dataclass(item) and not isinstance(item, type)
            else item
            for item in payload
        ]
    else:
        data = payload
    return json.dumps(data, indent=2, default=_default)
