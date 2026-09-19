# Maintenance

Diagnosis never writes. These are the only commands that can, and they open a **separate** connection to do it.

```bash
dbperf vacuum                            # preview
dbperf vacuum --execute                  # VACUUM (ANALYZE) bloated tables
dbperf reindex --execute                 # REINDEX INDEX CONCURRENTLY

dbperf drop-unused-indexes               # preview, with refusals explained
dbperf drop-unused-indexes --script      # emit SQL, connect for nothing else
dbperf drop-unused-indexes --execute     # prompts for the database name

dbperf restore-indexes --from ~/.db-perf-toolkit/rollbacks/<file>.json --execute
```

## The rules

- **Dry run is the default.** Nothing runs without `--execute`. This is the inverse of Ola Hallengren's `@Execute='Y'`, which is a reasonable default for a solution with fifteen years of hardening behind it and not for this one.
- **Every destructive run writes a rollback manifest first**, to local disk, before touching anything. A drop records its own `CREATE INDEX` statement — taken from `pg_get_indexdef` — so `restore-indexes` can put it back. An interrupted run is still recoverable.
- **Destructive operations require typing the database name.** `--yes` skips it for automation.
- **`--script` never connects for writes at all**, emitting SQL for a human to review.
- **`CONCURRENTLY` throughout**, on both `REINDEX` and `DROP INDEX`, so maintenance does not take a lock that blocks writes for its duration. [Verified under load](../benchmarks/remediation.md): 710 commits with a 0.6 ms worst-case stall while 33 indexes were dropped underneath.

## Timeouts

Write connections run with **no `statement_timeout`** and a **10s `lock_timeout`**. Maintenance takes as long as it takes; what is bounded is time spent waiting for a lock, not time spent working. See [installation](installation.md#timeouts) for why the two differ.

## What it refuses to drop

| Refused | Why |
|---|---|
| Primary keys | Excluded from the query entirely |
| Constraint-backed indexes | Dropping changes what the table accepts |
| Unique indexes | Enforce uniqueness even with no `pg_constraint` row |
| Indexes with any recorded scans | Not unused |
| Indexes below 8MB | Below the floor, so the change buys nothing |
| Anything, if statistics were reset < 7 days ago | The window is too short to call an index unused |

That last one is the guard people skip. Counters are cumulative since the last reset, so an index serving a monthly report looks untouched for 29 days out of 30. Override with `--min-stats-age-days`.

The 8MB floor has a known limitation on pathological schemas — see [findings](../benchmarks/findings.md). Use `index-burden` alongside it.

## Rollback

```
$ dbperf drop-unused-indexes --script

-- public.orders_status_idx: 1912 kB, 0 scans, on orders
-- rollback: CREATE INDEX orders_status_idx ON public.orders USING btree (status);
DROP INDEX CONCURRENTLY "public"."orders_status_idx";
```

Manifests live in `~/.db-perf-toolkit/rollbacks/`, on local disk rather than in the target database — the tool must not need write access somewhere just to keep its own notes.

Round trips are verified at scale, including indexes named `"Mixed.Case.Index"`, `"index'with'quotes"`, and one on a table called `"user data old"`.

## What VACUUM does not do

`VACUUM` marks space reusable by future inserts. It does **not** return it to the operating system, and the file does not shrink. Measured across three databases: every one of 12M dead tuples reclaimed, heap size unchanged to the byte.

Only `VACUUM FULL` returns space, and it rewrites the table under an `ACCESS EXCLUSIVE` lock — blocking reads as well as writes, for the duration. **The tool does not currently offer it.** See [free-space](diagnostics.md) for measuring what a rewrite would reclaim, and consider [`pg_repack`](https://github.com/reorg/pg_repack), which achieves the same result with only brief exclusive locks. Why neither is implemented yet: [roadmap](../roadmap.md#vacuum-full-and-pg_repack).

## SQL Server: `index-maintenance`

```bash
dbperf index-maintenance                 # dry run
dbperf index-maintenance --script        # print the EXEC, run nothing
dbperf index-maintenance --execute
```

This drives [Ola Hallengren's](ola-hallengren.md) `IndexOptimize` rather than reimplementing it, through the same plan/dry-run/execute pipeline as every other maintenance command. It requires the procedure to be installed; if it is not, the command says so and stops rather than substituting something homegrown.

The dry runs nest. Without `--execute` nothing runs, and the generated call also carries IndexOptimize's own `@Execute = 'N'`, so a hand-copied `EXEC` from `--script` still only prints what it would do.

There is no PostgreSQL equivalent, and mapping one would mean accepting arguments it ignores: `REINDEX` has no fragmentation thresholds to honour, so `--min-pages` and the reorganize/rebuild levels have nothing to act on. `reindex` is the honest command there.

## Not implemented

No rate limiting beyond the guards above. If maintenance needs scheduling, drive `--script` output through whatever already runs your migrations.
