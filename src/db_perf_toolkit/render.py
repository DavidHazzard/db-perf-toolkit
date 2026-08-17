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
    UnusedIndex,
)

QUERY_PREVIEW_CHARS = 70


def _truncate(text: str, width: int = QUERY_PREVIEW_CHARS) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


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


def seq_scan_table(rows: list[SeqScanHotspot]) -> Table:
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

    for r in rows:
        table.add_row(
            r.table,
            f"{r.seq_scans:,}",
            f"{r.index_scans:,}",
            f"{r.seq_rows_read:,}",
            f"{r.avg_rows_per_scan:,.0f}",
            f"{r.live_rows:,}",
            r.size_pretty,
        )
    return table


def unused_indexes_table(rows: list[UnusedIndex]) -> Table:
    table = Table(title="Indexes with no recorded scans", title_justify="left")
    table.add_column("Table")
    table.add_column("Index")
    table.add_column("Scans", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Safe to drop?")

    for r in rows:
        if r.enforces_constraint:
            verdict = Text("no — backs a constraint", style="red")
        elif r.is_unique:
            verdict = Text("no — unique index", style="red")
        else:
            verdict = Text("likely", style="green")
        table.add_row(r.table, r.index, f"{r.scans:,}", r.size_pretty, verdict)
    return table


def bloat_table(rows: list[BloatedTable]) -> Table:
    table = Table(title="Tables with a high dead tuple ratio", title_justify="left")
    table.add_column("Table")
    table.add_column("Live", justify="right")
    table.add_column("Dead", justify="right")
    table.add_column("Dead %", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Last autovacuum")

    for r in rows:
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
