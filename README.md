# db-perf-toolkit

Point it at a PostgreSQL database and it surfaces slow queries, unused indexes, table bloat, sequential-scan hotspots, and lock contention. A lightweight, read-only alternative to expensive DBA tooling.

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

## It cannot write to your database

This is designed to be pointed at production, so read-only is enforced by the server rather than by the discipline of the queries:

```sql
SET SESSION default_transaction_read_only = on
```

Every statement runs inside a read-only transaction. A bug in a catalog query cannot become a write.

That specific line matters. psycopg's `Connection.read_only` attribute governs only transactions the driver opens itself, and in autocommit mode it opens none — so the attribute is silently inert and writes succeed. The integration suite asserts a `CREATE TABLE` is actually rejected, which is how that was caught.

Connections also set `application_name=db-perf-toolkit`, so the tool is identifiable in `pg_stat_activity` to whoever is watching the server.

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

Add `--json` to any command for machine-readable output. Rich formatting is written to stderr in that mode, so stdout stays a clean pipe:

```bash
dbperf --json report | jq '.unused_indexes[] | select(.is_unique == false)'
```

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

- SQL Server backend (`sys.dm_exec_query_stats`, `sys.dm_db_missing_index_details`, `sys.dm_db_index_usage_stats`, `sys.dm_exec_requests`) behind the same CLI
- Index bloat in addition to table bloat
- `--since` filtering using `pg_stat_statements_reset()` checkpoints

## Contributing

Issues and PRs welcome. `uv run ruff check .` and `uv run pytest` should pass; tests need a working Docker daemon.

## License

MIT
