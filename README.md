# db-perf-toolkit

[![CI](https://github.com/DavidHazzard/db-perf-toolkit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/DavidHazzard/db-perf-toolkit/actions/workflows/ci.yml)

Point it at a PostgreSQL or SQL Server database and it surfaces slow queries, unused indexes, missing indexes, bloat, fragmentation and lock contention. Then, if you ask it to, it fixes the ones it can prove are safe to fix — and tells you why it will not touch the rest.

![dbperf refusing to drop three of the four indexes a naive query would have dropped](https://raw.githubusercontent.com/DavidHazzard/db-perf-toolkit/main/demo/out/refusal.gif)

Two commands, 23 seconds, no install required to watch it.

The first is the query everyone reaches for: `idx_scan = 0`. It returns **six** indexes. Two are primary keys, one is a unique index, one backs a constraint, one is below the size floor, and one is genuinely dead weight. Running that list through `DROP INDEX` drops two primary keys and two constraints.

The second is `dbperf drop-unused-indexes` against the same database. It drops **one** — and names the reason beside each of the three it refuses: unique, constraint-backed, below the 8 MB floor. The two primary keys never appear, because they are excluded from the query that finds candidates rather than filtered out afterwards. Then `--script` prints the `CREATE INDEX` rollback above the `DROP`, which is the line a DBA checks for before trusting anything.

## Everything in the list below reported success

Ten times in this repository, across layers with nothing in common, something returned a success signal that was not derived from its outcome. None of them raised. Every one was found by checking the result rather than the return code.

| Reported | Actually |
|---|---|
| psycopg's `conn.read_only = True` — a read-only connection | Writes **succeeded**. The attribute governs only transactions the driver opens, and in autocommit it opens none. |
| `statement_timeout` as a safety guard on every connection | It cancels `VACUUM` and `REINDEX CONCURRENTLY` like any other statement — and a cancelled `REINDEX CONCURRENTLY` leaves an `INVALID` index to drop by hand. The guard was making cleanup work. |
| `WHERE st.dbid = DB_ID()` — scope the plan cache to this database | `sys.dm_exec_sql_text.dbid` is `NULL` for ad-hoc batches. Zero rows against a live workload, where the correct filter returns 23. |
| `JOIN sys.dm_db_index_usage_stats` — read each index's usage | An index never touched has **no row at all** in that DMV. Not a row of zeros. The inner join silently drops exactly the indexes the check exists to find. |
| `sys.dm_exec_requests` came back empty — nothing is blocked | It does not deny an unprivileged login. It succeeds, showing only that login's own session. A gridlocked server reports clean. |
| A filter excluding the tool's own statements from its own report | It matched the whole batch, comments included, and silently deleted the busiest statement in the fixture. |
| A green SQL Server CI job | `pyodbc` is an optional extra and the fixtures skip rather than fail. The job could exit 0 having tested nothing. |
| A pytest hook that applies the `sqlserver` marker by path | It fails open. Marks nothing, everything deselects, pytest exits 5, run is green and wrong — and no assertion inside the suite can tell that apart from having nothing to mark. |
| `vhs demo.tape` exited 0 | VHS 0.12.0 captured zero frames and wrote no file. |
| A commit succeeded with a message describing the change | The `git add` named a path that does not exist. `git add` fails atomically on an unmatched pathspec, so it staged nothing, and `stderr` went to `/dev/null`. |

The tell is identical every time: **a success signal not derived from the outcome.**

The discipline that caught all ten is also the same every time — check the result, not the return code — and it is why the rest of the repository looks the way it does:

- **The CI job executes every `run:` block**, extracted from the parsed YAML and run locally, rather than eyeballing the workflow. That caught a heredoc fence that broke under command substitution, and a `grep` under `set -e` that would have turned the build red the moment the repo became clean.
- **The tests run against real PostgreSQL and SQL Server containers**, never mocked cursors. The only thing worth being right about here is whether the catalog SQL returns the correct answer, and a mock returns whatever you told it to.
- **The fixtures wait for a successful `SELECT 1`, not an open port**, because the port lie was measured rather than assumed. TCP 1433 accepts at 0.5 s; the engine answers at 7.7 s. In between, the failure is `Login failed for user 'sa'` — so waiting on the port does not fail as a timeout. It fails as a credentials bug, and sends you to debug the wrong thing entirely.

Most of the tests in this repository assert that something is **refused**. A tool that drops the wrong index is worse than no tool.

## Read-only is not symmetric, and this page will not pretend it is

On **PostgreSQL**, read-only is enforced by the server, not by the discipline of the queries:

```sql
SET SESSION default_transaction_read_only = on
```

The integration suite asserts that a `CREATE TABLE` through a diagnostic connection is actually rejected. That assertion is how the psycopg trap in the first row of the table above was caught.

On **SQL Server it is not enforced at all.** Tested, not assumed: a `CREATE TABLE` through a `read_only=True` connection succeeded. There is no session-level `SET TRANSACTION READ ONLY`, and `ApplicationIntent=ReadOnly` is accepted and ignored on a standalone instance — against an availability group listener it routes to a readable secondary where writes *are* refused, which makes it a routing hint that sometimes has enforcement as a side effect, never an enforcement mechanism.

So on SQL Server `read_only` gates the maintenance path inside this process and nothing more. A seatbelt, not a wall. **The enforcement is the grant**, which is a deployment decision no connection string can assert:

```sql
GRANT VIEW SERVER STATE TO [dbperf];   -- VIEW DATABASE STATE on Azure SQL Database
ALTER ROLE db_datareader ADD MEMBER [dbperf];
```

An overclaimed safety property is worse than an absent one.

## Two engines that deliberately diverge

SQL Server has `sys.dm_db_missing_index_details` — a genuine server-side recommendation the optimiser built from its own activity. PostgreSQL has no equivalent, so it gets `seq-scans`, which reports candidates for `EXPLAIN` and is named so that nobody mistakes it for a recommendation. Neither engine fakes the other's checks. A check an engine cannot answer is refused with the reason and a pointer to the nearest thing, never left silently blank.

| | PostgreSQL | SQL Server |
|---|---|---|
| `slow-queries` | `pg_stat_statements` | Query Store, falling back to the plan cache |
| `unused-indexes` | With a per-index drop-safety verdict | Same verdict, same refusals |
| `blocking` | `pg_blocking_pids()` | `sys.dm_exec_requests`, permission-checked first |
| `seq-scans` | Tables worth an `EXPLAIN` | — the missing-index DMV answers this directly |
| `missing-indexes` | — no server-side recommendation exists to relay | `sys.dm_db_missing_index_details` |
| `bloat`, `free-space` | Dead tuples, and reclaimable space | — no MVCC dead tuples; row versions live in `tempdb` |
| `fragmentation` | — the analogue is `free-space` | Index page-order drift |
| `index-burden` | Ranked by row modifications | — `user_updates` counts *statements*: **1** after a 200,000-row `INSERT` |
| Maintenance | `VACUUM`, `REINDEX CONCURRENTLY`, `DROP INDEX CONCURRENTLY` | Orchestrates Ola Hallengren's `IndexOptimize` rather than reimplementing it |

The `index-burden` gap is the one worth reading twice, because the plausible substitute is worse than the absence. `sys.dm_db_stats_properties.modification_counter` really is per-row — and it resets on every statistics update, so with auto-update on it is zeroed by the very write volume it is meant to measure. Ranking by it would invert the answer on exactly the tables that matter. See [the SQL Server notes](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/ola-hallengren.md) and [the benchmark finding](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/benchmarks/findings.md).

A check its engine cannot answer refuses with the nearest thing that engine *can* answer, and says how the two differ — `dbperf fragmentation` against PostgreSQL points at `free-space` rather than printing a list of unrelated check names. `fragmentation` and `free-space` are both excluded from `dbperf report`: they read table and index data rather than catalogs, and a report that sometimes takes ten minutes is worse than one that makes you ask.

## Install

Not on PyPI yet — the first `v0.1.0` tag has not been pushed. This is what works today:

```bash
git clone https://github.com/DavidHazzard/db-perf-toolkit
cd db-perf-toolkit
uv sync
uv run dbperf --dsn postgresql://user@host/shop report
```

This is what will work after that tag lands, and not before:

```bash
uv tool install db-perf-toolkit
export DBPERF_DSN=postgresql://user@host/shop
dbperf report
```

SQL Server needs the optional extra plus Microsoft's ODBC driver, which pip cannot install for you: `uv sync --all-extras`, then [`msodbcsql18`](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server). Connect with an `mssql://` DSN; the engine is chosen from the scheme, because a connection string already says what it connects to.

Requirements, extensions, permissions and timeouts: **[installation](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/installation.md)**.

## Run it against a database with real problems

```bash
./scripts/demo.sh
```

Spins up a throwaway PostgreSQL container, builds a schema with genuine pathologies, diagnoses, remediates, and diagnoses again. About 90 seconds, and it cleans up after itself. One pass takes the demo table from 157 MB to 99 MB and clears 96,000 dead tuples. It closes by calling `pg_stat_reset()` and trying again, so you can watch the stats-window guard refuse rather than read about it.

The GIF above is regenerable the same way — `./scripts/record-demo.sh` renders it from a 72-line [VHS tape](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/demo/demo.tape) under version control, so a recording cannot drift from the tool it records.

## Two things worth knowing before pointing this at production

**`VACUUM` does not shrink the file.** All 12M dead tuples across the three benchmark databases were reclaimed and heap size did not move by a byte: 61 MB, 3.84 GB and 2.44 GB, before and after. Plain `VACUUM` marks space reusable by future inserts; only `VACUUM FULL` returns it to the OS, and that rewrites the table under an `ACCESS EXCLUSIVE` lock. Every byte of on-disk reduction in those runs came from dropping indexes.

**`bloat` and `free-space` answer different questions.** Dead tuples read zero after a vacuum while the file stays exactly as large. On the worst benchmark database, `bloat` reports **zero tables** while `free-space` finds **31 holding 1.03 GB** — every one of them at 0% dead tuples.

## Documentation

**Usage**

| | |
|---|---|
| [Installation](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/installation.md) | Requirements, extensions, permissions, connecting, timeouts |
| [Diagnostics](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/diagnostics.md) | The read-only checks and how to read them |
| [Maintenance](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/maintenance.md) | Write mode, guards, refusals, rollback |
| [Architecture](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/architecture.md) | Internals, backend design, tests |
| [Ola Hallengren](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/ola-hallengren.md) | What was borrowed, and what deliberately was not |
| [CI](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/ci.md) | Four blocking jobs, and the two guards against a green run that tested nothing |
| [Releasing](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/releasing.md) | Tag-driven, Trusted Publishing, no PyPI token to leak |
| [Roadmap](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/roadmap.md) | What is next, what was deferred, and what was deliberately not built |

**Benchmarks**

| | |
|---|---|
| [Overview](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/benchmarks/README.md) | Headline results across three databases |
| [Scenarios](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/benchmarks/scenarios.md) | The three test databases, up to 2,017 indexes across 202 tables |
| [Performance](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/benchmarks/performance.md) | Query timings and how they scale |
| [Findings](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/benchmarks/findings.md) | The measurement that changed the tool |
| [Remediation](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/benchmarks/remediation.md) | What the write path actually did |

## Tests

**124 tests: 106 integration — 33 PostgreSQL, 73 SQL Server — every one against a real engine in a container, plus 18 CLI tests that need no database.** No mocked cursors anywhere: the value of this tool is entirely in whether its catalog SQL is correct, and a mock cannot establish that. The suites create genuine dead tuples, drive real sequential scans, produce a confirmed `LCK_M_IS` block from a second connection, and build 31.52% fragmentation deterministically by page splits rather than by `NEWID()`.

Everything CI runs, runs locally — there is no `make ci` indirection and no step that exists only on the runner:

```bash
uv sync --all-extras --dev
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q -m "not sqlserver"      # needs Docker
uv run pytest -q -rs -m sqlserver        # needs Docker and msodbcsql18
```

## Contributing

Issues and PRs welcome. The four CI jobs all block, so run the commands above before opening one.

## License

MIT. Ola Hallengren's Maintenance Solution is his, also MIT, and is [orchestrated rather than vendored](https://github.com/DavidHazzard/db-perf-toolkit/blob/main/docs/usage/ola-hallengren.md).
