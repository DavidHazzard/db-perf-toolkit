# Roadmap

What is next, why each item is not done yet, and — the more useful half — what was deliberately not built.

Nothing here is a commitment to a date. This is a pre-1.0 tool that has never been tagged; see [releasing](usage/releasing.md).

## Next

### `VACUUM FULL` and `pg_repack`

[`free-space`](usage/diagnostics.md) was the prerequisite, and it exists now: recommending a table rewrite without being able to say what it returns is a guess, not a recommendation. On the worst benchmark database it finds [31 tables holding 1.03 GB](benchmarks/findings.md) that `bloat` reports as clean.

Deferred because the execution side is the hard half, not the measurement. `VACUUM FULL` holds an `ACCESS EXCLUSIVE` lock for the whole rewrite — blocking reads as well as writes — which is the opposite of the `CONCURRENTLY`-throughout posture the rest of the write path takes. Shipping it alone would give the tool one command whose safe use depends on a maintenance window it has no way to know about.

[`pg_repack`](https://github.com/reorg/pg_repack) is the right answer and is a larger change than it looks: it is a server extension *and* a client binary, so it is neither SQL this backend can emit into an `Operation` nor something capability detection can settle by querying `pg_extension` alone.

### Object selection

```
--tables 'public.%,-public.audit_%'
```

Borrowed in shape from Ola Hallengren's `@Databases`, where a leading `-` excludes and the list is evaluated in order. See [what else was borrowed](usage/ola-hallengren.md).

Deferred because it touches every command's signature and every backend's SQL at once, and because getting it wrong lands squarely in this repository's favourite failure class: a pattern that matches nothing makes `drop-unused-indexes` print "nothing to do" against a database full of work, and reads exactly like a clean bill of health. The design decision that has to come first is that a selector matching zero objects must be an error, not an empty result — which is a behaviour to get right rather than a flag to add.

### Run history in local SQLite

So that "unused across six runs, six weeks apart" replaces "unused in this one snapshot".

The single snapshot is the weakest input the drop decision has. It is guarded today by refusing to drop anything when statistics were reset less than seven days ago — an index serving a monthly report looks untouched for 29 days out of 30 — but a guard against a short window is not the same as a long one.

Deferred because it introduces the first persistent state this tool owns, and therefore a schema, a location, and a migration story for both. The rollback manifests already live in `~/.db-perf-toolkit/`, so the location is settled; what is not settled is what happens when two people run against the same server, or when the same database is reached through two different DSNs. The existing guard works, so this is an upgrade rather than a gap.

### Index bloat

Distinct from table bloat and from `free-space`: a B-tree that has been through enough churn holds pages that are mostly empty.

Deferred because both routes are unsatisfying today. `pgstattuple` per index is a full scan, and `free-space` already demonstrated what that costs — it is the only check excluded from `report` for exactly that reason. The widely-copied estimation queries are estimates with known failure modes on some column types, and this repository has already been bitten once by presenting an estimate where a reader expected a measurement. It lands when it can be measured cheaply, or when the estimate can be labelled honestly enough to be worth printing.


## Deliberately not built

### A hosted browser sandbox

A live database per visitor is a recurring bill and a standing attack surface, and it would be the most expensive thing in this repository to keep alive — therefore the first thing to go stale, at which point a stranger's first impression of the tool is a 502.

What a stranger actually needs to see is a refusal, and that is 23 seconds of terminal output. The [recorded demo](../demo/README.md) costs 183 KB, renders in every browser with no JavaScript, needs no uptime, and regenerates from a 72-line tape under version control. Anyone who wants to run it against a real database has `scripts/demo.sh`, which builds and tears down its own container.

### Recorded DMV fixtures instead of real engines

Recording `sys.dm_*` and `pg_stat_*` output once and replaying it would make the suite fast and Docker-free. It would also make it useless, because a recorded fixture returns exactly what the person recording it expected.

Four of the ten failures listed in the [README](../README.md) were behaviours nobody would have thought to record: an index with **no row at all** in `sys.dm_db_index_usage_stats` rather than a row of zeros; `sys.dm_exec_sql_text.dbid` arriving `NULL` for ad-hoc batches; `sys.dm_exec_requests` succeeding for an unprivileged login while showing only its own session; usage counters resetting with the service. A fixture built from assumptions cannot contradict them, and contradicting them is the entire job.

The price is smaller than it looks, because container start dominates and every test shares it. Both suites use a session-scoped fixture: going from 59 to 73 SQL Server tests moved the total from 16.5 s to 17.2 s. Details, including why this job must not run under xdist, are in [CI](usage/ci.md).

### `index-burden` on SQL Server

Not deferred — declined, on a measurement rather than a hunch.

`sys.dm_db_index_usage_stats.user_updates` counts *statements*, not rows: it reads **1** after a 200,000-row `INSERT`. The obvious substitute, `sys.dm_db_stats_properties.modification_counter`, genuinely is per-row — and it resets on every statistics update, so with auto-update statistics on (the default) it is zeroed by the very write volume it is being asked to measure. The busiest tables report the smallest numbers, and ranking by it would invert the answer on precisely the tables that matter.

So `Check.INDEX_BURDEN` is absent from the SQL Server backend's `supports`, and the base class refuses it by name. `TableIndexBurden.writes_unit` exists so a backend that *can* supply row counts says which unit it is using, rather than filling one field from two sources whose numbers differ by five orders of magnitude under the same column heading.

This is reconsidered if SQL Server grows a per-row write counter that survives a statistics update. Until then, an honest refusal beats a plausible table of wrong numbers.

### A coverage gate

Every test here is an integration test against a live server, and most of the maintenance tests assert that something is *refused*. A coverage percentage cannot tell you whether a refusal was correct. The reasoning, and the only version of this gate that would be worth having, is in [CI](usage/ci.md#why-there-is-no-coverage-report).
