# db-perf-toolkit

Point it at a PostgreSQL database and it surfaces slow queries, unused indexes, table bloat, sequential-scan hotspots, and lock contention — then, if you ask it to, fixes what it safely can. Read-only by default; every destructive change is previewed, guarded, and reversible.

```console
$ dbperf --dsn postgresql://user@host/shop report
```

```
16.14 (Debian 16.14-1.pgdg13+1)  ·  statistics never reset (counters cover full server uptime)

Slowest statements by total execution time
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┓
┃ Query                       ┃ Calls ┃    Total ┃     Mean ┃    Rows ┃ % time ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━┩
│ SELECT o.status, count(*)   │    15 │   1.01 s │  67.1 ms │      45 │  30.5% │
│ FROM orders o JOIN          │       │          │          │         │        │
│ audit_log a ON a.order_i…   │       │          │          │         │        │
│ SELECT count(*) FROM orders │    40 │ 466.9 ms │  11.7 ms │      40 │  14.2% │
│ WHERE total_cents > $1      │       │          │          │         │        │
└─────────────────────────────┴───────┴──────────┴──────────┴─────────┴────────┘

Sequential scan hotspots — candidates for EXPLAIN, not index recommendations
┏━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┓
┃ Table  ┃ Seq scans ┃ Idx scans ┃  Rows read ┃ Avg/scan ┃ Live rows ┃  Size ┃
┡━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━┩
│ orders │       140 │         0 │ 16,800,000 │  120,000 │   246,000 │ 51 MB │
└────────┴───────────┴───────────┴────────────┴──────────┴───────────┴───────┘

Indexes with no recorded scans
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Table  ┃ Index                ┃ Scans ┃    Size ┃ Safe to drop?     ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ orders │ orders_reference_key │     0 │   16 MB │ no — unique index │
│ orders │ orders_status_idx    │     0 │ 1912 kB │ likely            │
│ orders │ orders_placed_at_idx │     0 │ 1896 kB │ likely            │
└────────┴──────────────────────┴───────┴─────────┴───────────────────┘

Tables with a high dead tuple ratio
┏━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ Table  ┃    Live ┃   Dead ┃ Dead % ┃  Size ┃ Last autovacuum ┃
┡━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ orders │ 246,000 │ 54,000 │  18.0% │ 51 MB │ never           │
└────────┴─────────┴────────┴────────┴───────┴─────────────────┘

blocking: nothing found
```

## See it work

```bash
./scripts/demo.sh
```

Spins up a throwaway PostgreSQL container, builds a schema with real problems in it, diagnoses, remediates, and diagnoses again. Roughly 90 seconds, and it cleans up after itself (`--keep` to leave it running).

What one pass changes:

| | Before | After |
|---|---|---|
| `orders` table size | 157 MB | **99 MB** |
| Dead tuples | 96,000 (24.0%) | **none** |
| Unused indexes | 4 | 3 (only the safe one dropped) |

The interesting part is what it *declined* to do. Four indexes had zero recorded scans; exactly one was dropped:

```
  DROP  public.orders_notes_idx  (58 MB, 0 scans, on orders)
  skip  public.orders_reference_key — unique index (enforces uniqueness even without a constraint row)
  skip  public.orders_id_uq — backs a constraint (dropping it changes what the table accepts)
  skip  public.orders_status_idx — only 2552 kB — below the 8MB floor, so dropping it buys nothing
```

A fifth index, `orders_customer_idx`, never appeared at all — it is genuinely in use, so it was never a candidate.

The demo finishes by calling `pg_stat_reset()` and trying again, to show the guard hold:

```
Statistics were reset 0.0 days ago, which is too short a window to conclude an
index is unused (minimum 7 days).
An index serving a weekly or monthly query looks untouched most of the time.
Override with --min-stats-age-days if you are certain.
```

## Read-only by default; writes are opt-in and reversible

Diagnosis never writes. The connection is read-only **at the server**, not by the discipline of the queries:

```sql
SET SESSION default_transaction_read_only = on
```

That specific line matters. psycopg's `Connection.read_only` attribute governs only transactions the driver opens itself, and in autocommit mode it opens none — so the attribute is silently inert and writes succeed. The integration suite asserts a `CREATE TABLE` is actually rejected, which is how that was caught.

Maintenance commands (`vacuum`, `reindex`, `drop-unused-indexes`) are the only ones that can write, and they open a **separate** connection to do it. The rules:

- **Dry run is the default.** Nothing runs without `--execute`. This is the inverse of Ola Hallengren's `@Execute='Y'` default, which is a reasonable choice for a solution with fifteen years of hardening behind it and not for this one.
- **Every destructive run writes a rollback manifest first**, to local disk, before touching anything. A drop records its own `CREATE INDEX` statement — taken from `pg_get_indexdef` — so `restore-indexes` can put it back. An interrupted run is still recoverable.
- **Destructive operations require typing the database name.** `--yes` skips it for automation.
- **`--script` never connects for writes at all**, emitting SQL for a human to review.

Every statement is bounded by `statement_timeout` (default 30s). A diagnostic tool should never be the thing that pages someone. Connections set `application_name=db-perf-toolkit` so the tool is identifiable in `pg_stat_activity`.

### What it refuses to drop

| Refused | Why |
|---|---|
| Primary keys | Excluded from the query entirely |
| Constraint-backed indexes | Dropping changes what the table accepts |
| Unique indexes | Enforce uniqueness even with no `pg_constraint` row |
| Indexes with any recorded scans | Not unused |
| Indexes below 8MB | Below the floor, so the change buys nothing |
| Anything, if statistics were reset < 7 days ago | The window is too short to call an index unused |

That last one is the guard people skip. Counters are cumulative since the last reset, so an index serving a monthly report looks untouched for 29 days out of 30. Override with `--min-stats-age-days`.

## Relationship to Ola Hallengren's Maintenance Solution

[Ola Hallengren's SQL Server Maintenance Solution](https://ola.hallengren.com/) (MIT) is the de-facto standard for SQL Server maintenance. This tool does not compete with it and does not vendor it.

**Borrowed as design, reimplemented for PostgreSQL:**

- `@Execute='N'` → `--script`, emitting reviewable SQL instead of running it
- `@MinNumberOfPages = 1000` → the 8MB index floor; 1000 PostgreSQL pages is 8MB, and the reasoning carries over
- `LogToTable` → the rollback manifest, written to local disk rather than the target database, since the tool must not need write access just to keep its own notes

**Planned for SQL Server:** orchestration, not reimplementation. `dbperf` will detect `IndexOptimize` and `CommandLog` in the target database and drive them through the same plan/dry-run/execute pipeline. His procedures stay his, installed and updated by you through his own channels.

The division is straightforward: **his solution maintains, this one diagnoses** — and on SQL Server, hands the maintenance to his.

## Install

```bash
uv tool install db-perf-toolkit     # or: pipx install db-perf-toolkit
```

From a clone:

```bash
uv sync
uv run dbperf --help
```

## Usage

The DSN can be passed with `--dsn` or read from `$DBPERF_DSN`.

```bash
export DBPERF_DSN=postgresql://user@host/dbname

dbperf report                       # every check, one pass
dbperf slow-queries --limit 20
dbperf seq-scans --min-scans 50 --min-rows 1000
dbperf unused-indexes
dbperf bloat --min-dead-pct 10
dbperf blocking
```

Add `--json` to any check for machine-readable output. Rich formatting is written to stderr in that mode, so stdout stays a clean pipe:

```bash
dbperf --json report | jq '.unused_indexes[] | select(.is_unique == false)'
```

### Maintenance

All of these are dry-run unless given `--execute`.

```bash
dbperf vacuum                            # preview
dbperf vacuum --execute                  # VACUUM (ANALYZE) bloated tables
dbperf reindex --execute                 # REINDEX INDEX CONCURRENTLY

dbperf drop-unused-indexes               # preview, with refusals explained
dbperf drop-unused-indexes --script      # emit SQL, connect for nothing else
dbperf drop-unused-indexes --execute     # prompts for the database name

dbperf restore-indexes --from ~/.db-perf-toolkit/rollbacks/shop-<stamp>.json --execute
```

`--script` output carries its own undo:

```sql
-- public.orders_status_idx: 1912 kB, 0 scans, on orders
-- rollback: CREATE INDEX orders_status_idx ON public.orders USING btree (status);
DROP INDEX CONCURRENTLY "public"."orders_status_idx";
```

`CONCURRENTLY` throughout — both `REINDEX` and `DROP INDEX` — so maintenance does not take a lock that blocks writes for its duration. That is the difference between a maintenance window and an outage.

## What it checks

| Check | Source | Notes |
|---|---|---|
| `slow-queries` | `pg_stat_statements` | Ranked by total execution time, with each statement's share of the whole. |
| `seq-scans` | `pg_stat_user_tables` | Tables absorbing heavy sequential scans. |
| `unused-indexes` | `pg_stat_user_indexes`, `pg_index`, `pg_constraint` | Never-scanned indexes, with a drop-safety verdict. |
| `bloat` | `pg_stat_user_tables` | Dead tuple ratio and last vacuum time. |
| `blocking` | `pg_blocking_pids()`, `pg_stat_activity` | Point-in-time snapshot of lock waits. |

### Two places this tool refuses to overclaim

**Sequential scans are not index recommendations.** PostgreSQL has no equivalent of SQL Server's `sys.dm_db_missing_index_details`, so there is no server-side recommendation to report. Reporting a heuristic as a recommendation would be advice the tool cannot back up. These are candidates to examine with `EXPLAIN`.

**An unused index is not automatically a droppable index.** Primary keys are excluded outright. Indexes that are `UNIQUE` or back a constraint are listed but marked unsafe — they enforce what data the table will accept, so dropping one is a schema change, not a cleanup. Note the difference the tool tracks: `CREATE UNIQUE INDEX` sets `is_unique` but creates no `pg_constraint` row, while a `UNIQUE` constraint sets both.

## Reading the numbers

Every cumulative check is meaningless without the window it covers, which is why the statistics reset time is printed above every report. An index that looks unused may simply have had its counters reset an hour ago. Check `stats_reset` before dropping anything.

Bloat figures come from the statistics collector and are estimates, not an exact on-disk measurement.

## Requirements

- PostgreSQL 9.6+ for most checks; `slow-queries` needs the `pg_stat_statements` extension
- Python 3.11+

`pg_stat_statements` must be loaded at server start — `CREATE EXTENSION` alone is not enough:

```
shared_preload_libraries = 'pg_stat_statements'
```

If it is missing, `dbperf` says so and explains the fix rather than failing obscurely. One unavailable check never aborts the rest of a `report` run.

Reading other users' statements requires `pg_read_all_stats` or superuser; without it PostgreSQL silently hides those rows.

## Architecture

```
cli.py          Click commands, exit codes, JSON vs terminal output
render.py       Rich tables and JSON serialisation
models.py       Engine-agnostic result types
backends/
  base.py       Backend ABC + CheckUnavailable
  postgres.py   All PostgreSQL catalog queries
```

Backends own their SQL outright and translate results into the shared models, because the introspection queries for different engines have nothing in common. The CLI and renderers depend only on `models.py`, so a second engine needs no changes above the backend layer.

`CheckUnavailable` distinguishes "this server cannot answer that question" from "this tool is broken". The first is reported with a remedy and exit code 3; the second is a bug.

## Tests

```bash
uv run pytest
```

13 integration tests run against a real PostgreSQL 16 container via testcontainers — no mocked cursors. The value of this tool is entirely in whether its catalog queries are correct, which only a live server can establish. The suite creates genuine dead tuples, drives real sequential scans, and opens a second connection to produce an actual lock wait.

Two production bugs were caught this way on the first run: the read-only enforcement described above, and the drop-safety classification for constraint-backed indexes.

The fixture disables autovacuum so dead tuples survive to be measured, and calls `pg_stat_force_next_flush()` after seeding — statistics are accumulated per-backend and flushed on a timer, so assertions made immediately after a workload otherwise fail intermittently.

## Roadmap

- SQL Server **diagnosis** (`sys.dm_exec_query_stats` / Query Store, `sys.dm_db_missing_index_details`, `sys.dm_db_index_usage_stats`, `sys.dm_exec_requests`) behind the same CLI. Query Store is 2016+, and `sys.dm_db_missing_index_details` differs on Azure SQL Database — the backend has to know which server it is talking to.
- SQL Server **maintenance** by orchestrating Ola Hallengren's `IndexOptimize` through the same plan/dry-run/execute pipeline.
- Object selection syntax borrowed from his `@Databases` parameter: `--tables 'public.%,-public.audit_%'`, wildcards with `-` for exclusion.
- Run history in local SQLite, so "unused across six runs spanning three months" replaces a single snapshot as grounds for dropping an index.
- Index bloat in addition to table bloat.

## Contributing

Issues and PRs welcome. `uv run ruff check .` and `uv run pytest` should pass; tests need a working Docker daemon.

## License

MIT
