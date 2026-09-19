# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is below 1.0.0, the CLI surface may change in a minor release;
breaking changes are called out under **Changed** with the word *breaking*.

## [Unreleased]

Nothing yet. Work lands here after v0.1.0 is tagged.

## [0.1.0] - unreleased

The first published release: PostgreSQL diagnosis and maintenance behind one CLI.
Everything below is already in `main`; the date is filled in when `v0.1.0` is
tagged. See [docs/usage/releasing.md](docs/usage/releasing.md).

### Added

- Engine-agnostic backend layer, as the foundation for a second engine. Checks are
  now declared rather than assumed: `Backend.supports` names what an engine can
  answer and everything else raises `CheckUnavailable` saying so, instead of
  forcing each engine to implement a check it cannot honestly support.
- `MissingIndex` and `IndexFragmentation` models, engine dispatch from the DSN
  scheme, and a clear install message when the `sqlserver` extra is absent rather
  than an `ImportError`.
- `StatsWindow.window_source`, because the two engines establish the statistics
  window differently — PostgreSQL reads `pg_stat_database.stats_reset`, while SQL
  Server has no equivalent column and is bounded by service start time. Ignoring
  that on SQL Server would mean recommending index drops across a failover, when
  every index looks unused.
- `scripts/docker-clean.sh`, scoped to this project's containers and their
  anonymous volumes. It deliberately never runs `docker system prune` or
  `docker volume prune`, which are global and reap other projects' work.

- **Diagnostics.** Read-only checks behind a Click CLI with Rich tables and JSON
  export: `slow-queries`, `seq-scans`, `unused-indexes`, `bloat`, `blocking`,
  `index-burden`, `free-space`, and `report` to run them together.
  - Read-only is enforced by the server rather than by the discipline of the
    queries, via `SET SESSION default_transaction_read_only`. psycopg's
    `read_only` attribute is not sufficient on its own: it governs only
    transactions the driver opens, and in autocommit mode it opens none.
  - Sequential scans are reported as `EXPLAIN` candidates, not index
    recommendations. PostgreSQL has no equivalent of SQL Server's missing-index
    DMV, so there is no server-side recommendation to relay.
  - Unused indexes carry a drop-safety verdict rather than a bare list. Primary
    keys are excluded outright; unique and constraint-backed indexes are listed
    but refused.
  - `pg_stat_statements` renamed its timing columns in PostgreSQL 13, so the
    column pair is selected from the server version rather than assumed.
  - A missing extension or insufficient privileges raise `CheckUnavailable` with a
    remedy, and never abort the other checks in a `report` run.
- **SQL Server.** The same CLI against SQL Server 2016+ and Azure SQL Database,
  behind the optional `[sqlserver]` extra, with the engine chosen from the DSN
  scheme. `slow-queries` reads Query Store where it exists and falls back to the
  plan cache; `unused-indexes` reaches the same drop-safety verdict and the same
  refusals; `blocking` checks permissions before reporting, because
  `sys.dm_exec_requests` does not deny an unprivileged login — it silently shows
  it only its own session.
  - `missing-indexes` and `fragmentation` have no PostgreSQL counterpart and are
    offered only where they are real, rather than approximated. A check its
    engine cannot answer refuses with the closest check that engine *can* answer
    and states how the two differ.
  - `index-maintenance` orchestrates Ola Hallengren's `IndexOptimize` rather
    than reimplementing it, through the same plan/dry-run/execute pipeline. If
    the procedure is not installed the command says so and stops.
  - `index-burden` is deliberately **not** offered on SQL Server.
    `user_updates` counts statements, not rows — it reads **1** after a
    200,000-row `INSERT` — and the plausible substitute is worse:
    `modification_counter` is per-row but resets on every statistics update, so
    with auto-update on it is zeroed by the very write volume it measures.
  - Read-only is not equally enforceable. PostgreSQL holds the guarantee at the
    server; SQL Server has no server-side equivalent outside a read-only
    replica, so there it is this tool's discipline. That asymmetry is stated
    rather than papered over.
- **Opt-in write mode.** `vacuum`, `reindex` and `drop-unused-indexes`, with
  `restore-indexes` to undo a previous drop. Planning is separated from execution:
  each backend returns `Operation` objects carrying their SQL, whether they are
  destructive, and their own rollback statement, which `--script`, dry run and
  `--execute` all consume.
  - Dry run unless `--execute`.
  - A rollback manifest is written to local disk before anything runs, holding
    each dropped index's own `CREATE` statement from `pg_get_indexdef`, so an
    interrupted run is still recoverable.
  - Typed database-name confirmation on destructive operations.
  - Refusals for primary keys, constraint-backed and unique indexes, indexes with
    any recorded scans, and indexes below an 8 MB floor.
  - Refusal when statistics were reset under 7 days ago, since counters are
    cumulative and an index serving a monthly report looks untouched for 29 days
    out of 30.
- **`index-burden`**, which answers the question a per-index view cannot: which
  tables are paying for indexes nothing reads. It ranks by unused indexes
  multiplied by row modifications rather than by size. Found by stress testing —
  on a pathological database the 8 MB floor inverts, and 1,772 indexes written off
  as too small hold twice as much as the 33 the tool would act on.
- **`free-space`**, the space a table rewrite would return to the operating
  system, and the prerequisite for ever offering `VACUUM FULL`. It is the only
  check that reads table data rather than catalogs: large tables use
  `pgstattuple_approx`, tables under a size floor are not examined at all, and it
  is deliberately excluded from `report`.
- **`scripts/demo.sh`**, an end-to-end demonstration that builds a throwaway
  database with genuine problems, diagnoses, remediates and diagnoses again. Of
  four indexes with zero recorded scans, exactly one is dropped; one pass takes
  the demo table from 157 MB to 99 MB and clears 96,000 dead tuples. It closes by
  calling `pg_stat_reset()` and retrying, so the stats-window guard can be seen
  refusing rather than merely described.
- **Benchmarks.** `scripts/bench.py`, `scripts/bench_remediate.py` and
  `scripts/bench_report.py`, three scenario schemas, and `docs/benchmarks/`
  generated from five runs of each check against each database. Check timings are
  1.0 ms to 27.5 ms, growing about 28x while the catalog grows 288x.
- **33 integration tests** against a real `postgres:16` container, with no mocked
  cursors — the value is entirely in whether the catalog SQL is correct, which
  only a live server can establish.

### Changed

- **Timeouts now differ by connection kind.** `statement_timeout` bounds how long
  a statement may *run*, which is right for diagnostics (30 s) and wrong for
  maintenance, where write connections run unbounded unless a caller insists.
  `lock_timeout` (10 s) bounds how long we wait *to start*, which is the right
  guard for maintenance and applies to reads as well.
- Terminal tables are capped at 25 rows, and the cap is always stated. JSON export
  stays uncapped. `unused-indexes` printed 643 lines against the realistic
  database, which is not a report.
- Table and index models carry schema qualification, since generating DDL against
  an unqualified name is wrong the moment two schemas collide.
- Documentation restructured: the README is an overview that links out, usage
  lives in `docs/usage/`, and `BENCHMARK.md` became the five generated files in
  `docs/benchmarks/`.

### Fixed

- `connect()` set `statement_timeout` unconditionally, including on write
  connections, so `dbperf vacuum --execute` aborted after 30 seconds on any table
  large enough to need longer. The worse case was `REINDEX CONCURRENTLY`: a
  cancelled one leaves an `INVALID` index behind that has to be dropped by hand,
  so the guard intended to make the tool safe was creating cleanup work.
- `Operation.target` was an unquoted `schema.name`, which cannot be parsed apart
  once a name contains a dot — and the manifest is the audit trail for destructive
  work. It is now a quoted qualified identifier. The generated SQL was always
  correct; only the display and manifest key were ambiguous.

### Measured, and worth knowing

- **`VACUUM` does not shrink the file.** All 12M dead tuples across the three
  benchmark databases were reclaimed and heap size did not move: 61 MB, 3.84 GB
  and 2.44 GB before and after. Every byte of on-disk reduction came from dropping
  indexes.
- **`CONCURRENTLY` holds under write load.** A second connection committed 710
  rows with a 0.6 ms worst-case stall and no errors while 33 indexes were dropped
  beneath it.
- **`bloat` and `free-space` are different questions.** On the worst benchmark
  database, `bloat` reports nothing while `free-space` finds 31 tables holding
  1.03 GB.

[Unreleased]: https://github.com/DavidHazzard/db-perf-toolkit/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DavidHazzard/db-perf-toolkit/releases/tag/v0.1.0
