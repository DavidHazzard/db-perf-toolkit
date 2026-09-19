"""Command line interface."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import psycopg
from rich.console import Console

from db_perf_toolkit import __version__, manifest, render, safety
from db_perf_toolkit.backends import Backend, CheckUnavailable, UnknownEngineError, connect
from db_perf_toolkit.models import Operation, Plan

DSN_ENVVAR = "DBPERF_DSN"

#: Connection failures that deserve a message rather than a traceback.
#:
#: `ConnectionError` is the builtin, and it is what covers SQL Server:
#: SqlServerConnectionError subclasses it precisely so this module never has
#: to import pyodbc, which is an optional extra and absent on a
#: PostgreSQL-only install. UnknownEngineError is here because the most
#: common way to reach it is a DSN naming an engine whose extra is missing —
#: which is a connection problem from the user's side, not a usage error.
CONNECTION_ERRORS = (psycopg.OperationalError, ConnectionError, UnknownEngineError)

#: Ola Hallengren's published fragmentation thresholds: reorganize above 5%,
#: rebuild above 30%, ignore anything under 1000 pages. Restated here because
#: importing them would pull in the SQL Server backend, and with it pyodbc —
#: an optional extra that is absent on a PostgreSQL-only install. A test pins
#: these against the backend's own constants so the two cannot drift.
REORGANIZE_ABOVE_PCT = 5.0
REBUILD_ABOVE_PCT = 30.0
FRAGMENTATION_MIN_PAGES = 1000


class DbPerfGroup(click.Group):
    """Turns the two expected failure modes into messages, once, for every command.

    These were handled per-command, which meant every new command silently
    opted out: the maintenance commands were added later and tracebacked on a
    bad DSN because nobody remembered the `except`. Catching here makes the
    behaviour a property of the CLI rather than of whoever wrote the command.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except CheckUnavailable as exc:
            # Not a crash: the server cannot answer this question, and the
            # message says what would make it able to.
            click.secho(f"{exc.check} unavailable — {exc.reason}", fg="yellow", err=True)
            if exc.remedy:
                click.secho(exc.remedy, fg="yellow", err=True)
            sys.exit(3)
        except CONNECTION_ERRORS as exc:
            click.secho(f"Could not connect: {str(exc).strip()}", fg="red", err=True)
            sys.exit(2)


class Context:
    def __init__(self, dsn: str | None, as_json: bool, timeout: int) -> None:
        self.dsn = dsn
        self.as_json = as_json
        self.timeout = timeout
        self.host: str | None = None
        # JSON goes to stdout clean; Rich chrome must not contaminate a pipe.
        self.console = Console(stderr=as_json)

    def require_dsn(self) -> str:
        """Demand the connection string at the point of use, not at parse time.

        `--dsn` is deliberately not `required=True`. Click validates group
        options before dispatching to the subcommand, so a required one makes
        `dbperf bloat --help` fail with "Missing option '--dsn'" — the tool
        refusing to explain itself until you hand it a database.
        """
        if not self.dsn:
            raise click.UsageError(
                f"Missing option '--dsn'. Pass a connection string or set ${DSN_ENVVAR}."
            )
        return self.dsn

    def backend(self) -> Backend:
        """Always read-only. Writes require an explicit second connection."""
        return connect(self.require_dsn(), connect_timeout=self.timeout)


def _run(ctx: Context, check: str, fetch: Callable[[Backend], list[Any]], build: Any) -> None:
    """Shared body for the single-check commands."""
    with ctx.backend() as backend:
        window = backend.stats_window()
        rows = fetch(backend)

    if ctx.as_json:
        click.echo(render.to_json(rows))
        return

    ctx.console.print(render.stats_window_note(window))
    ctx.console.print()
    if rows:
        ctx.console.print(build(rows))
    else:
        ctx.console.print(f"[dim]{check}: nothing found[/dim]")


@click.group(cls=DbPerfGroup, context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="dbperf")
@click.option(
    "--dsn",
    envvar=DSN_ENVVAR,
    help=(
        "Connection string; the scheme picks the engine "
        f"(postgresql:// or mssql://). Reads ${DSN_ENVVAR} if not given."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout instead of a table.")
@click.option("--timeout", default=10, show_default=True, help="Connection timeout in seconds.")
@click.pass_context
def main(ctx: click.Context, dsn: str | None, as_json: bool, timeout: int) -> None:
    """Surface performance problems in a PostgreSQL or SQL Server database.

    Every command opens a read-only connection and reads only statistics and
    catalog views. Nothing is written, and no query is run against your data.

    Read-only is not equally enforceable on both engines. PostgreSQL holds the
    guarantee at the server, so a bug in this tool cannot write. SQL Server has
    no server-side equivalent outside a read-only replica, so there the
    guarantee is this tool's discipline — see the README.

    Not every check exists on both engines, because some have no honest
    counterpart: `dbperf <check> --help` says which engines answer it.
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


@main.command("index-burden")
@click.option(
    "--min-unused", default=2, show_default=True, help="Only tables with at least this many."
)
@click.pass_obj
def index_burden(ctx: Context, min_unused: int) -> None:
    """Per-table index cost, ranked by write amplification.

    `unused-indexes` answers "which indexes are unread"; this answers "which
    tables are paying for them". The distinction matters because the dominant
    cost of a redundant index is not disk, it is the B-tree write that every
    INSERT, UPDATE and DELETE pays into it. A size floor cannot see that: ten
    useless 16kB indexes on a hot table are individually trivial and
    collectively expensive.
    """
    _run(ctx, "index-burden", lambda b: b.index_burden(min_unused), render.index_burden_table)


@main.command("missing-indexes", short_help="SQL Server: indexes the optimiser says it wanted.")
@click.option(
    "--min-impact",
    default=0.0,
    show_default=True,
    help="Drop suggestions scoring below this.",
)
@click.pass_obj
def missing_indexes(ctx: Context, min_impact: float) -> None:
    """Indexes the optimiser says it wanted. SQL Server only.

    The absence of a PostgreSQL equivalent is real, not an omission:
    sys.dm_db_missing_index_details is written by the optimiser itself as it
    compiles plans, and PostgreSQL keeps no such record. `seq-scans` is the
    nearest thing it can offer, and it answers a weaker question.

    Treat the output as a ranking, not a worklist. The DMV emits one row per
    plan, so the same index arrives several times in slightly different
    shapes; creating them in order builds a pile of near-duplicates.
    """
    _run(
        ctx,
        "missing-indexes",
        lambda b: b.missing_indexes(min_impact),
        render.missing_indexes_table,
    )


@main.command("fragmentation", short_help="SQL Server: indexes whose page order has drifted.")
@click.option(
    "--min-pct",
    default=REORGANIZE_ABOVE_PCT,
    show_default=True,
    help="Ignore indexes below this fragmentation.",
)
@click.option(
    "--min-pages",
    default=FRAGMENTATION_MIN_PAGES,
    show_default=True,
    help="Ignore indexes smaller than this. Below ~1000 pages the number is noise.",
)
@click.pass_obj
def fragmentation(ctx: Context, min_pct: float, min_pages: int) -> None:
    """Indexes whose page order has drifted. SQL Server only.

    Not the same question as `bloat`, which counts dead tuples — something
    SQL Server does not have. Recommended actions follow Ola Hallengren's
    thresholds: reorganize above 5%, rebuild above 30%.

    Uses the LIMITED scan mode, which reads index metadata rather than the
    pages themselves. DETAILED is more accurate and reads every page, which
    is not something to point at a busy production server by default.
    """
    _run(
        ctx,
        "fragmentation",
        lambda b: b.index_fragmentation(min_pct, min_pages),
        render.fragmentation_table,
    )


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


@main.command("free-space")
@click.option("--min-free-pct", default=20.0, show_default=True)
@click.option("--min-size-mb", default=50, show_default=True, help="Ignore tables below this.")
@click.option(
    "--approx-above-mb",
    default=1024,
    show_default=True,
    help="Use pgstattuple_approx above this size.",
)
@click.option("--exact", is_flag=True, help="Force a full scan of every candidate.")
@click.pass_obj
def free_space(
    ctx: Context, min_free_pct: float, min_size_mb: int, approx_above_mb: int, exact: bool
) -> None:
    """Space a table rewrite would return to the operating system.

    Not the same question as `bloat`. That counts dead tuples — churn waiting
    for a vacuum — and they read zero once vacuumed, while the file stays
    exactly as large. This is the measurement that survives a vacuum.

    Unlike every other check, this reads table data rather than catalogs and
    can therefore be slow: pgstattuple scans every page. Tables above
    --approx-above-mb use the visibility map instead.

    Requires the pgstattuple extension.
    """
    _run(
        ctx,
        "free-space",
        lambda b: b.free_space(
            min_free_pct=min_free_pct,
            min_table_bytes=min_size_mb * 1024 * 1024,
            approx_above_bytes=approx_above_mb * 1024 * 1024,
            exact=exact,
        ),
        render.free_space_table,
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
    with ctx.backend() as backend:
        result = backend.report(limit)

    if ctx.as_json:
        click.echo(render.to_json(result))
        return

    render.render_report(result, ctx.console)


# ----------------------------------------------------------------------
# Maintenance
# ----------------------------------------------------------------------


def _show_plan(plan: Plan) -> None:
    for op in plan.operations:
        marker = click.style("DROP", fg="red") if op.destructive else click.style("run ", fg="cyan")
        click.echo(f"  {marker}  {op.target}  ({op.description})")
    for target, reason in plan.refused.items():
        click.secho(f"  skip  {target} — {reason}", fg="yellow")


def _emit_script(plan: Plan) -> None:
    click.echo("-- Generated by db-perf-toolkit. Review before running.")
    for op in plan.operations:
        click.echo(f"\n-- {op.target}: {op.description}")
        if op.rollback_sql:
            click.echo(f"-- rollback: {op.rollback_sql}")
        click.echo(op.sql)


def _apply(ctx: Context, plan: Plan, *, execute: bool, script: bool, assume_yes: bool) -> None:
    """Shared tail for every maintenance command."""
    if script:
        _emit_script(plan)
        return

    if not plan.operations:
        click.echo("Nothing to do.")
        _show_plan(plan)
        return

    _show_plan(plan)

    if not execute:
        click.secho(
            f"\nDry run — {len(plan.operations)} operation(s) planned. Pass --execute to apply.",
            fg="cyan",
        )
        return

    destructive = plan.destructive_operations
    manifest_path: Path | None = None
    if destructive:
        # Written before anything runs, so an interrupted run is still
        # recoverable from whatever did land.
        manifest_path = manifest.default_path(plan.database)
        manifest.write(plan, manifest_path, host=ctx.host)
        click.secho(f"\nRollback written to {manifest_path}", fg="green")

        if not assume_yes:
            click.secho(
                f"\nAbout to run {len(destructive)} DESTRUCTIVE operation(s) on {plan.database}.",
                fg="red",
                bold=True,
            )
            typed = click.prompt(
                "Type the database name to confirm", default="", show_default=False
            )
            if typed != plan.database:
                click.secho("Name did not match — nothing was run.", fg="yellow")
                sys.exit(4)

    with connect(ctx.require_dsn(), connect_timeout=ctx.timeout, read_only=False) as backend:
        results = backend.execute(plan.operations)

    failures = [(op, err) for op, err in results if err]
    for op, err in results:
        if err:
            click.secho(f"  FAILED  {op.target} — {err}", fg="red")
        else:
            click.secho(f"  ok      {op.target}", fg="green")

    if manifest_path:
        click.echo(f"\nUndo with: dbperf restore-indexes --from {manifest_path}")
    if failures:
        sys.exit(5)


@main.command("vacuum")
@click.option("--min-dead-pct", default=10.0, show_default=True)
@click.option("--min-dead-rows", default=1000, show_default=True)
@click.option("--no-analyze", is_flag=True, help="VACUUM without ANALYZE.")
@click.option("--execute", is_flag=True, help="Actually run it. Off by default.")
@click.option("--script", is_flag=True, help="Print the SQL instead of running it.")
@click.pass_obj
def vacuum(
    ctx: Context,
    min_dead_pct: float,
    min_dead_rows: int,
    no_analyze: bool,
    execute: bool,
    script: bool,
) -> None:
    """VACUUM tables carrying dead tuples. Non-destructive."""
    with ctx.backend() as backend:
        tables = backend.bloated_tables(min_dead_pct=min_dead_pct, min_dead_rows=min_dead_rows)
        plan = backend.plan_vacuum(tables, analyze=not no_analyze)
    _apply(ctx, plan, execute=execute, script=script, assume_yes=True)


@main.command("reindex")
@click.option("--max-scans", default=0, show_default=True)
@click.option("--execute", is_flag=True, help="Actually run it. Off by default.")
@click.option("--script", is_flag=True, help="Print the SQL instead of running it.")
@click.pass_obj
def reindex(ctx: Context, max_scans: int, execute: bool, script: bool) -> None:
    """Rebuild indexes with REINDEX INDEX CONCURRENTLY. Non-destructive.

    CONCURRENTLY avoids the lock that would block writes for the duration.
    """
    with ctx.backend() as backend:
        indexes = backend.unused_indexes(max_scans=max_scans)
        plan = backend.plan_reindex(indexes)
    _apply(ctx, plan, execute=execute, script=script, assume_yes=True)


@main.command("drop-unused-indexes")
@click.option("--min-size-mb", default=8, show_default=True, help="Ignore indexes below this size.")
@click.option(
    "--min-stats-age-days",
    default=safety.MIN_STATS_AGE_DAYS,
    show_default=True,
    help="Refuse if statistics were reset more recently than this.",
)
@click.option("--execute", is_flag=True, help="Actually drop. Off by default.")
@click.option("--script", is_flag=True, help="Print the SQL instead of running it.")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip the typed confirmation.")
@click.pass_obj
def drop_unused_indexes(
    ctx: Context,
    min_size_mb: int,
    min_stats_age_days: int,
    execute: bool,
    script: bool,
    assume_yes: bool,
) -> None:
    """Drop indexes confirmed unused. DESTRUCTIVE.

    Primary keys, unique indexes and constraint-backed indexes are always
    refused. Every drop is recorded with its own CREATE statement first, so
    the change can be undone with `restore-indexes`.
    """
    with ctx.backend() as backend:
        window = backend.stats_window()
        if execute:
            try:
                safety.check_stats_window(window, min_days=min_stats_age_days)
            except safety.RefusedError as exc:
                click.secho(str(exc), fg="red", err=True)
                sys.exit(4)
        indexes = backend.unused_indexes(max_scans=0)
        plan = backend.plan_drop_unused_indexes(indexes, min_size_bytes=min_size_mb * 1024 * 1024)

    ctx.console.print(render.stats_window_note(window))
    _apply(ctx, plan, execute=execute, script=script, assume_yes=assume_yes)


@main.command(
    "index-maintenance", short_help="SQL Server: reorganize or rebuild fragmented indexes."
)
@click.option("--databases", default=None, help="IndexOptimize @Databases. Defaults to this one.")
@click.option("--min-pages", default=FRAGMENTATION_MIN_PAGES, show_default=True)
@click.option("--execute", is_flag=True, help="Actually run it. Off by default.")
@click.option("--script", is_flag=True, help="Print the SQL instead of running it.")
@click.pass_obj
def index_maintenance(
    ctx: Context,
    databases: str | None,
    min_pages: int,
    execute: bool,
    script: bool,
) -> None:
    """Reorganize or rebuild fragmented indexes. SQL Server only.

    Orchestrates Ola Hallengren's IndexOptimize rather than reimplementing
    it. That procedure is mature and widely deployed, and a second-hand copy
    would be strictly worse: unfamiliar to every DBA who already runs it, and
    without its years of accumulated edge cases.

    Requires IndexOptimize to be installed. If it is not, this reports that
    and stops rather than falling back to something homegrown.

    There are two independent dry runs here. Without --execute this prints
    the plan and runs nothing. The generated call also carries IndexOptimize's
    own @Execute = 'N' unless --execute is given, so even a hand-run copy of
    the printed SQL only prints what it would do.
    """
    with ctx.backend() as backend:
        plan = backend.plan_index_maintenance(
            databases=databases,
            min_number_of_pages=min_pages,
            execute=execute,
        )
    _apply(ctx, plan, execute=execute, script=script, assume_yes=True)


@main.command("restore-indexes")
@click.option(
    "--from",
    "manifest_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Rollback manifest written by a previous drop.",
)
@click.option("--execute", is_flag=True, help="Actually recreate. Off by default.")
@click.pass_obj
def restore_indexes(ctx: Context, manifest_path: Path, execute: bool) -> None:
    """Recreate indexes dropped by an earlier run, from its manifest."""
    data = manifest.read(manifest_path)
    statements = manifest.rollback_statements(data)
    if not statements:
        click.echo("Manifest contains no rollback statements.")
        return

    click.echo(f"From {manifest_path} ({data.get('created_at')}):")
    for target, stmt in statements:
        click.echo(f"  {target}")
        click.echo(f"    {stmt}")

    if not execute:
        click.secho(
            f"\nDry run — {len(statements)} index(es) would be recreated. Pass --execute to apply.",
            fg="cyan",
        )
        return

    ops = [
        Operation(target=target, description="restore", sql=stmt, destructive=False)
        for target, stmt in statements
    ]
    with connect(ctx.require_dsn(), connect_timeout=ctx.timeout, read_only=False) as backend:
        results = backend.execute(ops)

    for op, err in results:
        if err:
            click.secho(f"  FAILED  {op.target} — {err}", fg="red")
        else:
            click.secho(f"  restored  {op.target}", fg="green")


if __name__ == "__main__":
    main()
