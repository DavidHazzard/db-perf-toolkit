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

Connections set `application_name=db-perf-toolkit`, so the tool is identifiable in `pg_stat_activity` to whoever is watching the server.

## Timeouts

The two timeouts guard different risks and are applied differently:

| | Read connections | Write connections |
|---|---|---|
| `statement_timeout` | **30s** | **unbounded** |
| `lock_timeout` | 10s | 10s |

`statement_timeout` bounds how long a statement may *run*. That is right for diagnostics — a slow catalog query over a large schema should never become someone's incident — and actively wrong for maintenance. It cancels `VACUUM` and `REINDEX CONCURRENTLY` like any other statement, and a cancelled `REINDEX CONCURRENTLY` leaves an `INVALID` index behind that has to be dropped by hand.

`lock_timeout` bounds how long we wait *to start*. That is the right guard for maintenance, and it applies to reads too — a query blocked behind DDL is just as stuck, and giving up beats joining the queue. It matters most for `ACCESS EXCLUSIVE` requests: once one is waiting, every lock request behind it waits too, including plain `SELECT`s. A maintenance command parked on a lock can take a table down before doing any work.
