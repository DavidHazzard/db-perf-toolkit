# Installation

```bash
uv tool install db-perf-toolkit     # or: pipx install db-perf-toolkit
```

From a clone:

```bash
uv sync
uv run dbperf --help
```

## Requirements

- **PostgreSQL 9.6+** for most checks
- **Python 3.11+**

Two checks need extensions. Both ship with PostgreSQL as contrib modules; neither is required for the tool to run, and a missing one produces an explanation rather than a crash.

| Check | Needs | Install |
|---|---|---|
| `slow-queries` | `pg_stat_statements` | `shared_preload_libraries` + `CREATE EXTENSION` |
| `free-space` | `pgstattuple` | `CREATE EXTENSION pgstattuple;` |

`pg_stat_statements` must be loaded at server start — `CREATE EXTENSION` alone is not enough:

```
shared_preload_libraries = 'pg_stat_statements'
```

A missing extension is reported as a skipped check with the remedy, exit code 3. One unavailable check never aborts the rest of a `report` run.

## Permissions

Reading other users' statements requires `pg_read_all_stats` or superuser. Without it PostgreSQL silently hides those rows rather than erroring, so the tool checks explicitly and tells you.

```sql
GRANT pg_read_all_stats TO <role>;
```

Diagnosis needs no write privileges at all. See [maintenance](maintenance.md) for what the write commands need.

## Connecting

Pass `--dsn` or set `$DBPERF_DSN`:

```bash
export DBPERF_DSN=postgresql://user@host/dbname
dbperf report
```

Connections set `application_name=db-perf-toolkit`, so the tool is identifiable in `pg_stat_activity` to whoever is watching the server, and every statement is bounded by `statement_timeout` (default 30s).
