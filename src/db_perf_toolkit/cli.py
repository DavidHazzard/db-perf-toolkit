"""Command line interface."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import click
import psycopg
from rich.console import Console

from db_perf_toolkit import __version__, render
from db_perf_toolkit.backends import Backend, CheckUnavailable, connect

DSN_ENVVAR = "DBPERF_DSN"


class Context:
    def __init__(self, dsn: str, as_json: bool, timeout: int) -> None:
        self.dsn = dsn
        self.as_json = as_json
        self.timeout = timeout
        # JSON goes to stdout clean; Rich chrome must not contaminate a pipe.
        self.console = Console(stderr=as_json)

    def backend(self) -> Backend:
        return connect(self.dsn, connect_timeout=self.timeout)


def _run(ctx: Context, check: str, fetch: Callable[[Backend], list[Any]], build: Any) -> None:
    """Shared body for the single-check commands."""
    try:
        with ctx.backend() as backend:
            window = backend.stats_window()
            rows = fetch(backend)
    except CheckUnavailable as exc:
        # Not a crash: the server simply cannot answer this question yet, and
        # the message says what to do about it.
        click.secho(f"{check} unavailable — {exc.reason}", fg="yellow", err=True)
        if exc.remedy:
            click.secho(exc.remedy, fg="yellow", err=True)
        sys.exit(3)
    except psycopg.OperationalError as exc:
        click.secho(f"Could not connect: {str(exc).strip()}", fg="red", err=True)
        sys.exit(2)

    if ctx.as_json:
        click.echo(render.to_json(rows))
        return

    ctx.console.print(render.stats_window_note(window))
    ctx.console.print()
    if rows:
        ctx.console.print(build(rows))
    else:
        ctx.console.print(f"[dim]{check}: nothing found[/dim]")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="dbperf")
@click.option(
    "--dsn",
    required=True,
    envvar=DSN_ENVVAR,
    help=f"PostgreSQL connection string. Reads ${DSN_ENVVAR} if not given.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout instead of a table.")
@click.option("--timeout", default=10, show_default=True, help="Connection timeout in seconds.")
@click.pass_context
def main(ctx: click.Context, dsn: str, as_json: bool, timeout: int) -> None:
    """Surface performance problems in a PostgreSQL database.

    Every command opens a read-only connection and reads only statistics and
    catalog views. Nothing is written, and no query is run against your data.
    """
    ctx.obj = Context(dsn=dsn, as_json=as_json, timeout=timeout)


@main.command("slow-queries")
@click.option("--limit", default=20, show_default=True, help="How many statements to return.")
@click.pass_obj
def slow_queries(ctx: Context, limit: int) -> None:
    """Statements ranked by total execution time.

    Requires the pg_stat_statements extension.
    """
    _run(ctx, "slow-queries", lambda b: b.slow_queries(limit), render.slow_queries_table)


@main.command("seq-scans")
@click.option("--min-scans", default=50, show_default=True)
@click.option("--min-rows", default=1000, show_default=True, help="Ignore small tables.")
@click.pass_obj
def seq_scans(ctx: Context, min_scans: int, min_rows: int) -> None:
    """Tables taking heavy sequential scans.

    PostgreSQL has no equivalent of SQL Server's missing-index DMV, so these
    are candidates to investigate with EXPLAIN — not index recommendations.
    """
    _run(
        ctx,
        "seq-scans",
        lambda b: b.seq_scan_hotspots(min_seq_scans=min_scans, min_rows=min_rows),
        render.seq_scan_table,
    )


@main.command("unused-indexes")
@click.option(
    "--max-scans", default=0, show_default=True, help="Treat <= this many scans as unused."
)
@click.pass_obj
def unused_indexes(ctx: Context, max_scans: int) -> None:
    """Indexes that are never scanned but still cost write throughput.

    Primary keys are excluded. Indexes backing UNIQUE or EXCLUDE constraints
    are listed but flagged as unsafe to drop, since they enforce the
    constraint rather than merely serving reads.
    """
    _run(ctx, "unused-indexes", lambda b: b.unused_indexes(max_scans), render.unused_indexes_table)


@main.command("bloat")
@click.option("--min-dead-pct", default=10.0, show_default=True)
@click.option("--min-dead-rows", default=1000, show_default=True)
@click.pass_obj
def bloat(ctx: Context, min_dead_pct: float, min_dead_rows: int) -> None:
    """Tables carrying a high proportion of dead tuples.

    Counts come from the statistics collector and are estimates, not an exact
    on-disk measurement.
    """
    _run(
        ctx,
        "bloat",
        lambda b: b.bloated_tables(min_dead_pct=min_dead_pct, min_dead_rows=min_dead_rows),
        render.bloat_table,
    )


@main.command("blocking")
@click.pass_obj
def blocking(ctx: Context) -> None:
    """Sessions currently blocked, and what is blocking them.

    A point-in-time snapshot — this is not a monitor.
    """
    _run(ctx, "blocking", lambda b: b.blocking_chains(), render.blocking_table)


@main.command("report")
@click.option("--limit", default=10, show_default=True, help="Slow queries to include.")
@click.pass_obj
def report(ctx: Context, limit: int) -> None:
    """Run every check and print one combined report.

    Checks the server cannot answer are reported as skipped rather than
    aborting the run.
    """
    try:
        with ctx.backend() as backend:
            result = backend.report(limit)
    except psycopg.OperationalError as exc:
        click.secho(f"Could not connect: {str(exc).strip()}", fg="red", err=True)
        sys.exit(2)

    if ctx.as_json:
        click.echo(render.to_json(result))
        return

    render.render_report(result, ctx.console)


if __name__ == "__main__":
    main()
