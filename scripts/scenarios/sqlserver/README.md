# SQL Server scenarios

The SQL Server counterpart of [`scripts/scenarios/`](../). Same purpose — a
database with real problems in it, so that a diagnostic check can be shown to
find them rather than asserted to — but the machinery is different enough to be
worth writing down.

Run it by hand:

```bash
docker run -d --name dbperf-mssql -p 1433:1433 \
  -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='dbperf-Test-Passw0rd!' -e MSSQL_PID=Developer \
  mcr.microsoft.com/mssql/server:2022-latest

# See "Readiness" below. Do NOT skip this and do NOT replace it with a port check.
until sqlcmd -S localhost,1433 -U sa -P 'dbperf-Test-Passw0rd!' -C -d master \
      -Q "SELECT 1" >/dev/null 2>&1; do sleep 1; done

sqlcmd -S localhost,1433 -U sa -P 'dbperf-Test-Passw0rd!' -C -Q "CREATE DATABASE dbperf"
sqlcmd -S localhost,1433 -U sa -P 'dbperf-Test-Passw0rd!' -C -d dbperf -i small.sql
```

Or let the fixtures in [`tests/sqlserver/conftest.py`](../../../tests/sqlserver/conftest.py)
do it. They read this same `small.sql`, so the two paths cannot drift apart.

---

## What `small.sql` produces

Every number below was measured against `mcr.microsoft.com/mssql/server:2022-latest`
(SQL Server 2022 RTM-CU27, 16.0.4295.3), and is stable across repeat runs.

| Pathology | Where it shows up | Measured |
|---|---|---|
| Slow, repeated queries | `sys.dm_exec_query_stats` | 5 statements, `execution_count` 60 / 30 / 12 / 12 / 8 |
| Missing index | `sys.dm_db_missing_index_details` | 3 rows on `dbo.orders`, `avg_user_impact` 87.2 / 92.4 / 93.1 |
| Unused index, maintained | `sys.dm_db_index_usage_stats` | `ix_orders_status`: seeks/scans/lookups 0, updates 1 |
| Unused index, untouched | `sys.dm_db_index_usage_stats` | `uq_orders_reference`: all four user counters 0 |
| Index genuinely in use | `sys.dm_db_index_usage_stats` | `ix_orders_customer_id`: 30 seeks — must **not** be reported droppable |
| Fragmentation | `sys.dm_db_index_physical_stats` | `pk_line_items`: 31.5% fragmented, 76.6% page density, 5,210 pages |
| Persisted query history | `sys.query_store_runtime_stats` | 18 queries; `count_executions` 60 / 30 / 12 / 12 / 8 |
| Blocking chain | `sys.dm_exec_requests` | not in this file — see below |

Seed cost: ~4 s, ~90 MB of database.

---

## Readiness: the port lies, and here is by how much

`scripts/demo.sh` carries a comment about `pg_isready`: PostgreSQL's entrypoint
runs a throwaway server for `initdb` and then restarts, so a probe that fires
during that window is answering about a server that is about to disappear. SQL
Server has the same trap with different plumbing, and it is worse.

Measured three times, warm image, 24-core host:

```
docker run returned            :   0.52s
TCP 1433 accepts               :   0.53s   <-- a port check says "ready" here
master answers SELECT 1        :   7.66s   <-- it is actually ready here
  --> port-check lie window    :   7.13s
CREATE DATABASE returned       :   8.04s
dbperf answers SELECT 1        :   8.10s
```

**A port check is wrong by about seven seconds**, which is most of the startup.
And it is not a quiet seven seconds — polling `master` across that window
returns two different failures in sequence:

1. `[08001] Client unable to establish connection because an error was
   encountered during handshakes` — the listener is up, the engine is not
   serving.
2. `[28000] Login failed for user 'sa'. (18456)` — the engine is serving, but
   the entrypoint has not applied `MSSQL_SA_PASSWORD` yet.

Anything that retries only on connection-refused sails straight past both.

So the probe is **`SELECT 1` against the target database**, not the port and
not `master`. A session cannot be opened against a database that is not
`ONLINE`, which makes a successful round-trip proof rather than inference. The
fixture probes twice: `master`, to get far enough to issue `CREATE DATABASE`,
then `dbperf`, because `CREATE DATABASE` returns before the new database is
necessarily usable.

**Budget for CI: 8-9 s from `docker run` to a usable database**, excluding
image transfer. The image is 1.69 GB on disk from ~626 MB of compressed layers,
so on a cold runner the pull dominates the job and is worth caching. Paid once
per pytest session, not per test.

---

## Why DDL alone is not enough

`sys.dm_db_missing_index_details` is written by the **query optimiser**, as a
side effect of compiling a plan. A database that has only ever had
`CREATE TABLE` run against it reports itself perfectly healthy. This is the
failure mode to watch for: a fixture that looks complete, runs clean, and
asserts nothing.

Measured minimum workload to populate it: **one compilation** of a qualifying
query. The row is written at compile time, not execution time — running it
sixty times does not create more rows, it only raises `user_seeks` and
`avg_total_user_cost` in `sys.dm_db_missing_index_group_stats`. `small.sql`
repeats the queries anyway so those columns hold numbers worth ranking on.

"Qualifying" is doing real work in that sentence. Three things will silently
produce no suggestion:

- **Trivial plans.** A single-predicate equality `SELECT` can be optimised
  trivially, and trivial plans skip the missing-index feature entirely. This is
  the commonest reason the DMV comes back empty when someone expected a row.
  The seed uses two predicates plus an `ORDER BY`, and a second `GROUP BY`
  shape as insurance.
- **Bad cardinality estimates.** The suggestion is a costing decision. If the
  optimiser still believes the table holds the handful of rows it had at
  `CREATE TABLE` time, a scan looks cheap. The seed runs
  `UPDATE STATISTICS ... WITH FULLSCAN` before the workload.
- **Later index DDL.** Any `CREATE INDEX`, `DROP INDEX` or
  `ALTER INDEX ... REBUILD` on a table **discards that table's rows in
  `sys.dm_db_missing_index_details`**. All index DDL on `dbo.orders` therefore
  happens near the top of the file. Add an index at the bottom and the
  pathology disappears without a word.

That last one has a consequence for tests too: the seeded database is
session-scoped and shared, so a test that creates or drops an index on
`dbo.orders` breaks every test that runs after it.

---

## Query Store: enabled, deliberately, with the defaults overridden

**Enabled.** `sys.dm_exec_query_stats` is free and immediate, but it is a
window onto the plan cache: evict the plan — memory pressure, `DBCC
FREEPROCCACHE`, a settings change, a restart — and the history is gone. Query
Store persists the same numbers in user tables and survives restarts, which is
the only way to answer *"was this query always slow?"*. A slow-query check that
reads only the plan cache gives different answers on Tuesday than it gave on
Monday, and under-reports silently.

**The cost.** Three defaults make Query Store look broken in a short-lived
fixture, and all three are overridden in `small.sql`:

| Setting | Default | Here | Why |
|---|---|---|---|
| `QUERY_CAPTURE_MODE` | `AUTO` (2019+) | `ALL` | `AUTO` discards infrequent or cheap queries. A seeded test workload is exactly that, so `AUTO` leaves the catalog views plausibly, silently empty. `ALL` is right for a fixture and wrong for production, where it can bloat the store on an ad-hoc-heavy workload. |
| `INTERVAL_LENGTH_MINUTES` | 60 | 1 | Runtime stats are bucketed and nothing aggregates into `sys.query_store_runtime_stats` until a bucket exists. 1 is the minimum. |
| `DATA_FLUSH_INTERVAL_SECONDS` | 900 | 60 | Collected data sits in memory before it is written where the catalog views can see it. |

Even at 60 seconds the flush interval outlives the fixture, so `small.sql` ends
with `EXEC sys.sp_query_store_flush_db`. This is the Query Store analogue of
`pg_stat_force_next_flush()` in the PostgreSQL fixture, and without it a test
that reads Query Store straight after the workload sees an empty store and
fails for reasons that have nothing to do with the code under test. The
`dm_exec_*` views need no equivalent — they are read live off the cache.

**The tool must not require it.** Query Store is 2016+, is off by default, and
some deployments cannot afford it. Prefer it where it is on; fall back to
`dm_exec_query_stats` where it is not.

---

## Two traps in reading these DMVs

Both cost real time here, so they are written down rather than rediscovered.

**`sys.dm_exec_sql_text.dbid` is `NULL` for ad-hoc batches.** It is only
populated for SQL that lives in a module — a procedure, trigger or function. A
check that scopes `sys.dm_exec_query_stats` with `WHERE st.dbid = DB_ID()`
returns *zero rows* for an ordinary application workload, which is precisely
the workload it is meant to find. Get the database from the plan instead:

```sql
CROSS APPLY (
    SELECT CAST(pa.value AS int) AS dbid
    FROM sys.dm_exec_plan_attributes(qs.plan_handle) AS pa
    WHERE pa.attribute = 'dbid'
) AS attr
WHERE attr.dbid = DB_ID()
```

**Simple parameterisation rewrites the query text.** Workload statement (e) is
sent as `SELECT COUNT_BIG(*) AS n FROM dbo.orders WHERE customer_id = 42` and
comes back out of both DMVs as:

```
(@1 tinyint)SELECT COUNT_BIG(*) [n] FROM [dbo].[orders] WHERE [customer_id]=@1
```

Bracket-quoted, literal replaced by `@1`, with a parameter declaration
prepended. A test that asserts on the text it sent will fail. This is a feature
— it is what lets `execution_count` accumulate across executions with different
literals — but it means query text from these views is normalised, not
verbatim.

---

## Fragmentation without randomness

The usual demo clusters on `NEWID()` and lets random keys shred the index. It
works, but the result differs every run and cannot be asserted on tightly.

`small.sql` does it deterministically instead. Rows go in in key order at
`FILLFACTOR = 100`, so every page is packed solid and in perfect physical
order. Then two passes widen selected rows in place. There is no free space on
any page, so each widened row forces a split: SQL Server takes a page from
wherever the allocation bitmap has one, moves half the rows across, and patches
the linked list. Logical order stops matching physical order, which is exactly
what `avg_fragmentation_in_percent` measures.

Widening *every second* row matters — it puts the split mid-page, rather than
at the end where SQL Server's ascending-key special case appends a fresh page
instead of splitting.

One pass alone measures ~23%, which sits between the conventional 10%
"reorganize" line and the 30% "rebuild" line — a fixture that will argue with
whichever threshold the tool picks. The second pass takes it to **31.5%**,
clear of both. Page density lands at 76.6% over 5,210 pages.

Note the page count. Below ~8 pages an index lives on mixed extents and
`avg_fragmentation_in_percent` is noise that should be ignored rather than
reported; this table is three orders of magnitude clear of that floor. Both
`small.sql` and the fixture's verification query filter on `page_count > 8`.

A clustered index is also required: widening a row in a **heap** produces a
forwarded record, not a page split, and a different set of counters entirely.

---

## Blocking is not in this file

A lock chain needs two sessions alive simultaneously, and a script is one
session. `small.sql` sets up the one thing it can — it asserts
`READ_COMMITTED_SNAPSHOT OFF` explicitly — and the chain itself is the
`blocking_chain` fixture in `tests/sqlserver/conftest.py`: one session holds
`TABLOCKX, HOLDLOCK` inside an open transaction while another collides with it
on a plain `SELECT`, and the fixture waits for
`sys.dm_exec_requests.blocking_session_id` to confirm the chain before handing
it to the test. Measured wait type: `LCK_M_IS`.

Two things make this work, and the absence of either makes it hang instead of
fail clearly:

- **`READ_COMMITTED_SNAPSHOT` must be OFF.** Under RCSI the reader takes a row
  version rather than a shared lock, nothing blocks, and the fixture waits out
  its timeout for a reason nothing in the test suggests. It is the default, but
  the seed sets it explicitly because it is the single setting most likely to
  make these tests "flake" on someone else's instance.
- **The holding connection must have `autocommit` off.** With autocommit on,
  SQL Server releases the `TABLOCKX` the instant the `SELECT` completes and
  there is nothing left to block against.

---

## Not yet ported

The PostgreSQL side has three scenarios; this is the first. `realistic.sql` and
`horror.sql` have no SQL Server equivalent yet. The horror scenario's awkward
identifiers translate directly and are worth keeping — `[Mixed.Case.Index]`,
`[index'with'quotes]`, `[user data old]` — since bracket quoting breaks naive
tooling in the same way double quoting does in PostgreSQL.
