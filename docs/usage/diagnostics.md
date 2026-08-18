# Diagnostics

Every command here opens a **read-only** connection and reads statistics and catalog views. Nothing is written. See [maintenance](maintenance.md) for the commands that can.

```bash
dbperf report                       # every cheap check, one pass
dbperf slow-queries --limit 20
dbperf seq-scans --min-scans 50 --min-rows 1000
dbperf unused-indexes
dbperf index-burden --min-unused 2
dbperf bloat --min-dead-pct 10
dbperf free-space                   # expensive; not in `report`
dbperf blocking
```

Add `--json` to any check for machine-readable output. Rich formatting goes to stderr in that mode, so stdout stays a clean pipe:

```bash
dbperf --json report | jq '.unused_indexes[] | select(.is_unique == false)'
```

Terminal tables are capped at 25 rows and always say how many were hidden. JSON output is never capped — a silently truncated list reads as "that is everything", which is worse than no list.

## What each check reads

| Check | Source | Notes |
|---|---|---|
| `slow-queries` | `pg_stat_statements` | Ranked by total execution time, with each statement's share of the whole. |
| `seq-scans` | `pg_stat_user_tables` | Tables absorbing heavy sequential scans. |
| `unused-indexes` | `pg_stat_user_indexes`, `pg_index`, `pg_constraint` | Never-scanned indexes, with a drop-safety verdict. |
| `index-burden` | `pg_stat_user_indexes`, `pg_stat_user_tables` | Per-table index cost, ranked by write amplification. |
| `bloat` | `pg_stat_user_tables` | Dead tuple ratio and last vacuum time. |
| `free-space` | `pgstattuple` | Space a rewrite would return to the OS. |
| `blocking` | `pg_blocking_pids()`, `pg_stat_activity` | Point-in-time snapshot of lock waits. |

## Three distinctions the tool insists on

### Sequential scans are not index recommendations

PostgreSQL has no equivalent of SQL Server's `sys.dm_db_missing_index_details`, so there is no server-side recommendation to report. `seq-scans` is a heuristic pointing at tables worth examining with `EXPLAIN`. Presenting it as a recommendation would be advice the tool cannot back up.

### An unused index is not automatically a droppable index

Primary keys are excluded outright. Indexes that are `UNIQUE` or back a constraint are listed but marked unsafe — they govern what data the table accepts, so dropping one is a schema change wearing a cleanup's clothing.

Note the distinction the tool tracks: `CREATE UNIQUE INDEX` sets `is_unique` but writes no `pg_constraint` row, while a `UNIQUE` constraint sets both. The safety check has to catch either.

### `bloat` and `free-space` answer different questions

`bloat` counts **dead tuples** — churn awaiting a vacuum. `free-space` measures **actual free space in the file**.

They diverge exactly where it matters. After a vacuum, dead tuples read zero while the file stays the same size, because plain `VACUUM` marks space reusable rather than returning it to the operating system. On the benchmark's worst database, `bloat` reports **zero tables** while `free-space` finds **31 holding 1.03 GB** — every one of them at 0% dead tuples.

`free-space` is the only check that reads table data rather than catalogs, so it is the only one that can be slow: `pgstattuple` scans every page. Tables above `--approx-above-mb` use `pgstattuple_approx`, which consults the visibility map instead and reports what fraction it actually read. It is deliberately **not** part of `report` — a report that sometimes takes seconds and sometimes ten minutes is a worse tool than one that makes you ask.

## Why `index-burden` is separate from `unused-indexes`

`unused-indexes` answers "which indexes are unread". `index-burden` answers "which tables are paying for them".

The dominant cost of a redundant index is not the disk it occupies — it is the B-tree write that every `INSERT`, `UPDATE` and `DELETE` pays into it. Ten useless 16kB indexes on a hot table are individually trivial and collectively expensive, and a per-index size floor is structurally unable to see that.

This is measured, not hypothetical: see [the benchmark finding](../benchmarks/findings.md).

## Reading the numbers

Every cumulative check is meaningless without the window it covers, which is why the statistics reset time is printed above every report. An index that looks unused may simply have had its counters reset an hour ago. Check `stats_reset` before dropping anything.

Bloat figures come from the statistics collector and are estimates. `free-space` figures are measured, exactly or by sampling — the table says which.
