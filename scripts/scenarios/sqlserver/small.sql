-- Scenario 1 of 3 (SQL Server): a small, ordinary application database.
--
-- The SQL Server counterpart of scripts/scenarios/small.sql. Same idea — one
-- application table with the everyday problems — but the pathologies are built
-- differently, because SQL Server's diagnostic views are populated by different
-- machinery than PostgreSQL's.
--
-- Run it by hand:
--     sqlcmd -S localhost,1433 -U sa -P '...' -C -d dbperf -i small.sql
--
-- or let tests/sqlserver/conftest.py apply it to a throwaway container.
--
-- ---------------------------------------------------------------------------
-- WHAT THIS PRODUCES, AND WHERE TO SEE IT
-- ---------------------------------------------------------------------------
--
--   sys.dm_exec_query_stats            three query shapes, executed enough
--                                      times that execution_count and
--                                      total_worker_time are meaningful.
--
--   sys.dm_db_missing_index_details    dbo.orders has no index on
--                                      warehouse_id. Section 7 runs the
--                                      queries that make the optimiser say so.
--
--   sys.dm_db_index_usage_stats        two flavours of "unused", measured:
--                                        ix_orders_status — user_seeks,
--                                          user_scans and user_lookups all 0,
--                                          user_updates 1. Created BEFORE the
--                                          load, so DML maintains it.
--                                        uq_orders_reference — all four user
--                                          counters 0. Created AFTER the load
--                                          and read by nothing.
--                                      A third case exists and this file
--                                      cannot produce it: an index with NO
--                                      ROW AT ALL. Measured on 2022 CU27,
--                                      CREATE TABLE + CREATE INDEX alone
--                                      leaves both indexes absent from this
--                                      DMV; the row appears on the first DML
--                                      or read. So a freshly restored or
--                                      freshly restarted database reports its
--                                      untouched indexes by omission, and a
--                                      check that INNER JOINs to this DMV
--                                      silently misses exactly the indexes it
--                                      is looking for. LEFT JOIN from
--                                      sys.indexes.
--
--                                      Also note user_updates = 1 after a
--                                      200,000-row INSERT: this DMV counts
--                                      statements, not rows.
--
--   sys.dm_db_index_physical_stats     pk_line_items is driven to high
--                                      avg_fragmentation_in_percent by a
--                                      deterministic row-widening update.
--
--   (blocking)                         NOT here — a lock chain needs two
--                                      concurrent sessions, which a single
--                                      script cannot hold open. See the
--                                      `blocking_chain` fixture in
--                                      tests/sqlserver/conftest.py.
--
-- ---------------------------------------------------------------------------
-- THE ORDERING IS LOAD-BEARING. DO NOT SHUFFLE THESE SECTIONS.
-- ---------------------------------------------------------------------------
--
--  * Any CREATE INDEX / DROP INDEX / ALTER INDEX REBUILD on a table discards
--    that table's rows in sys.dm_db_missing_index_details. So every piece of
--    index DDL on dbo.orders happens in section 3, long before the workload in
--    section 7. Add an index at the bottom of this file and the missing-index
--    pathology silently disappears.
--
--  * ix_orders_status must be created BEFORE section 4 loads rows, or it gets
--    no sys.dm_db_index_usage_stats row at all and stops being an example of
--    "maintained but never read".
--
--  * uq_orders_reference must be created AFTER the load, so that it is never
--    written to either and every one of its user counters stays at 0.
--
--  * Statistics are refreshed in section 6, before the workload. The
--    missing-index suggestion is an optimiser cost estimate; with stale row
--    counts the optimiser may not think an index would help enough to mention.

SET NOCOUNT ON;
GO

-- ---------------------------------------------------------------------------
-- 1. Database settings the pathologies depend on
-- ---------------------------------------------------------------------------

-- READ_COMMITTED_SNAPSHOT must be OFF, which is the default, but say so out
-- loud: with RCSI ON, readers take row versions instead of shared locks and
-- the blocking chain the tests reproduce simply never forms. This is the
-- single setting most likely to make the lock tests "flake" on someone
-- else's instance.
ALTER DATABASE CURRENT SET READ_COMMITTED_SNAPSHOT OFF WITH ROLLBACK IMMEDIATE;
GO

-- Query Store: ON, deliberately, with non-default settings.
--
-- The tradeoff. sys.dm_exec_query_stats is free and immediate but it is a
-- window onto the plan cache: evict the plan (memory pressure, DBCC
-- FREEPROCCACHE, a settings change, a restart) and the history is gone. Query
-- Store persists the same numbers in user tables, survives restarts, and is
-- the only way to answer "was this query always slow?" — which is the question
-- a performance tool actually gets asked.
--
-- What it costs, and why each default is overridden here:
--
--   QUERY_CAPTURE_MODE: the 2019+ default is AUTO, which discards queries that
--     are infrequent or cheap. A seeded test workload is exactly that, so AUTO
--     would leave the catalog views plausibly, silently empty. ALL captures
--     everything — correct for a test fixture, wrong for production, where ALL
--     on an ad-hoc-heavy workload can bloat the store fast.
--
--   INTERVAL_LENGTH_MINUTES: runtime stats are bucketed, and the default
--     bucket is 60 minutes. Nothing aggregates into sys.query_store_runtime_stats
--     until a bucket exists. 1 is the minimum and the only usable value for a
--     fixture that lives for seconds.
--
--   DATA_FLUSH_INTERVAL_SECONDS: collected data sits in memory for 900 seconds
--     by default before it is written where the catalog views can see it. This
--     is the Query Store analogue of PostgreSQL's stats collector lag, and it
--     is why section 8 calls sp_query_store_flush_db: without that call a test
--     that queries Query Store immediately after the workload reads an empty
--     store and fails for reasons that have nothing to do with the code under
--     test.
--
-- Overall: enabled because a slow-query check that only reads the plan cache
-- gives different answers on Tuesday than on Monday, and because the cost of
-- being wrong here is a tool that quietly under-reports. If a deployment
-- cannot afford it, dm_exec_query_stats still works — the toolkit must not
-- *require* Query Store, only prefer it.
ALTER DATABASE CURRENT SET QUERY_STORE = ON;
GO

ALTER DATABASE CURRENT SET QUERY_STORE (
    OPERATION_MODE               = READ_WRITE,
    QUERY_CAPTURE_MODE           = ALL,
    INTERVAL_LENGTH_MINUTES      = 1,
    DATA_FLUSH_INTERVAL_SECONDS  = 60,
    MAX_STORAGE_SIZE_MB          = 100,
    MAX_PLANS_PER_QUERY          = 200
);
GO

-- ---------------------------------------------------------------------------
-- 2. Idempotent teardown, so the file can be re-run against a live database
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS dbo.line_items;
DROP TABLE IF EXISTS dbo.orders;
GO

-- ---------------------------------------------------------------------------
-- 3. Schema and indexes  (all index DDL on dbo.orders lives here — see header)
-- ---------------------------------------------------------------------------

CREATE TABLE dbo.orders (
    id            int           NOT NULL,
    customer_id   int           NOT NULL,
    warehouse_id  int           NOT NULL,
    reference     varchar(40)   NOT NULL,
    total_cents   bigint        NOT NULL,
    status        varchar(20)   NOT NULL,
    placed_at     datetime2(3)  NOT NULL,
    CONSTRAINT pk_orders PRIMARY KEY CLUSTERED (id)
);
GO

-- Created BEFORE the load on purpose: the INSERT in section 4 maintains it,
-- so sys.dm_db_index_usage_stats gets a row with user_updates > 0 and
-- user_seeks = user_scans = user_lookups = 0. This is the honest shape of a
-- dead index on a table that is still being written to, and the case a naive
-- "WHERE user_seeks = 0" query gets right.
CREATE NONCLUSTERED INDEX ix_orders_status ON dbo.orders (status);
GO

-- Also created before the load, and this one IS read by the workload, so it
-- must never be reported as droppable.
CREATE NONCLUSTERED INDEX ix_orders_customer_id ON dbo.orders (customer_id);
GO

-- Deliberately NOT indexed: warehouse_id. Section 7's queries filter on it,
-- which is what puts dbo.orders into sys.dm_db_missing_index_details.

CREATE TABLE dbo.line_items (
    id        int           NOT NULL,
    order_id  int           NOT NULL,
    sku       varchar(32)   NOT NULL,
    -- varchar, not char: the padding column has to be able to GROW, because
    -- growth is what splits pages. A fixed-width column cannot fragment this
    -- way; it would just reserve the space up front.
    padding   varchar(3000) NOT NULL,
    CONSTRAINT pk_line_items PRIMARY KEY CLUSTERED (id) WITH (FILLFACTOR = 100)
);
GO

-- ---------------------------------------------------------------------------
-- 4. Data
-- ---------------------------------------------------------------------------

-- A tally driven off sys.all_objects rather than GENERATE_SERIES, which is
-- SQL Server 2022 only. This file has to work on 2017 and 2019 too.
WITH n AS (
    SELECT TOP (200000)
           ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS x
    FROM sys.all_objects a CROSS JOIN sys.all_objects b
)
INSERT INTO dbo.orders (id, customer_id, warehouse_id, reference, total_cents, status, placed_at)
SELECT x,
       x % 5000,
       -- 40 warehouses over 200k rows: 5000 rows each, selective enough that
       -- an index seek would clearly beat the clustered scan the optimiser is
       -- otherwise forced into. Too many distinct values and the seek wins by
       -- so little that no suggestion is emitted; too few and a scan really is
       -- the right plan.
       x % 40,
       CONCAT('REF-', CAST(x AS varchar(12))),
       (CAST(x AS bigint) * 37) % 250000,
       CHOOSE(1 + x % 3, 'placed', 'shipped', 'cancelled'),
       DATEADD(minute, -x, CAST('2025-01-01T00:00:00' AS datetime2(3)))
FROM n;
GO

-- Created AFTER the load, and read by nothing: its sys.dm_db_index_usage_stats
-- row has user_seeks, user_scans, user_lookups AND user_updates all at 0. It
-- is also UNIQUE, so it enforces a constraint and must be refused as droppable
-- on those grounds even once it has been correctly identified as unread.
CREATE UNIQUE NONCLUSTERED INDEX uq_orders_reference ON dbo.orders (reference);
GO

WITH n AS (
    SELECT TOP (30000)
           ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS x
    FROM sys.all_objects a CROSS JOIN sys.all_objects b
)
INSERT INTO dbo.line_items (id, order_id, sku, padding)
SELECT x,
       ((x * 7) % 200000) + 1,
       CONCAT('SKU-', CAST(x % 900 AS varchar(8))),
       REPLICATE('a', 40)
FROM n;
GO

-- ---------------------------------------------------------------------------
-- 5. Fragmentation
-- ---------------------------------------------------------------------------
--
-- The usual demo clusters on NEWID() and lets random keys shred the index.
-- That works, but the result is different every run and cannot be asserted on
-- tightly. This is the deterministic version and it produces the same
-- fragmentation every time:
--
--   Rows went in above in key order at FILLFACTOR 100, so every page is packed
--   solid and in perfect physical order — 0% fragmentation, ~100% page
--   density. Now widen every second row from 40 bytes of padding to 1200.
--   There is no free space on any page, so each widened row forces a split:
--   SQL Server allocates a page from wherever the allocation bitmap has one,
--   moves half the rows across, and patches the linked list. The logical order
--   of the leaf level stops matching the physical order, which is precisely
--   what avg_fragmentation_in_percent measures.
--
--   Widening every second row rather than every row matters — it guarantees
--   the split lands mid-page rather than at the end, where SQL Server's
--   ascending-key special case would append a fresh page instead of splitting.
--
-- Measure it with:
--   SELECT index_id, avg_fragmentation_in_percent, avg_page_space_used_in_percent, page_count
--   FROM sys.dm_db_index_physical_stats(DB_ID(), OBJECT_ID('dbo.line_items'), NULL, NULL, 'SAMPLED');
--
-- Note the page_count: below ~8 pages an index lives on mixed extents and
-- avg_fragmentation_in_percent is noise that should be ignored, not a finding.
-- This table lands in the thousands of pages, well clear of that floor.
UPDATE dbo.line_items
SET padding = REPLICATE('b', 1200)
WHERE id % 2 = 0;
GO

-- Second wave. The first pass alone measures ~23% fragmentation, which is
-- above the conventional 10% "reorganize" line but below the 30% "rebuild"
-- one, and a fixture that sits between two thresholds is a fixture that will
-- argue with whatever threshold the tool picks. This pass widens a further
-- sixth of the rows - all of them rows the first pass left narrow, so they
-- are spread across pages that are already half empty and already split -
-- and pushes the measured figure clear of both lines.
UPDATE dbo.line_items
SET padding = REPLICATE('c', 2600)
WHERE id % 6 = 3;
GO

-- ---------------------------------------------------------------------------
-- 6. Statistics
-- ---------------------------------------------------------------------------
-- Refreshed before the workload, not after. A missing-index suggestion is a
-- costing decision; if the optimiser still believes dbo.orders holds the
-- handful of rows it had at CREATE TABLE time, a scan looks cheap and it
-- recommends nothing.
UPDATE STATISTICS dbo.orders WITH FULLSCAN;
UPDATE STATISTICS dbo.line_items WITH FULLSCAN;
GO

-- ---------------------------------------------------------------------------
-- 7. Workload
-- ---------------------------------------------------------------------------
--
-- DDL alone produces none of this. sys.dm_db_missing_index_details is written
-- by the optimiser as a side effect of compiling a plan, and
-- sys.dm_exec_query_stats is written by the execution engine. An empty
-- database that has merely been CREATEd reports no problems at all, which is
-- the trap: a fixture can look complete, run clean, and assert nothing.
--
-- `GO n` is sqlcmd's "run this batch n times". Each repetition is a separate
-- round-trip and a separate execution against the same cached ad-hoc plan, so
-- execution_count accumulates the way it would under a real client. The Python
-- fixture understands `GO n` too and replays these batches identically, so
-- this file and the test fixture cannot drift apart.
--
-- Minimum viable workload, measured rather than guessed (see README.md):
--   * missing index    — ONE compilation of the warehouse_id query is enough;
--                        the row is written at compile time, not at execution
--                        time. It is repeated below only so the accompanying
--                        avg_total_user_cost and user_seeks in
--                        sys.dm_db_missing_index_group_stats are non-trivial.
--   * query stats      — one execution is enough to create the row; the
--                        repetition is what makes total_worker_time large
--                        enough to rank above the server's own background
--                        chatter.

-- (a) The repeated scan. No index on total_cents, 200k rows, executed enough
--     times to dominate the server's total_worker_time.
SELECT COUNT_BIG(*) AS matched FROM dbo.orders WHERE total_cents > 90000;
GO 60

-- (b) The missing-index query. Two predicates and an ORDER BY, so the
--     optimiser runs full cost-based optimisation. A single-predicate
--     equality SELECT would get a TRIVIAL plan, and trivial plans skip the
--     missing-index feature entirely — the commonest reason this DMV comes
--     back empty when someone expects a row.
SELECT TOP (100) o.id, o.reference, o.total_cents, o.placed_at
FROM dbo.orders AS o
WHERE o.warehouse_id = 7
  AND o.total_cents  > 90000
ORDER BY o.placed_at DESC;
GO 12

-- (c) A second shape over the same missing index, this time an aggregate.
--     Belt and braces: if (b) ever starts qualifying for a trivial plan on
--     some future version, the GROUP BY here still will not.
SELECT o.customer_id, SUM(o.total_cents) AS total_cents
FROM dbo.orders AS o
WHERE o.warehouse_id = 11
GROUP BY o.customer_id;
GO 12

-- (d) An expensive join, to give the slow-query ranking something to sort.
--
--     Note what it selects and does NOT select. The obvious version of this
--     query groups by o.status - and measurably breaks the unused-index
--     pathology, because ix_orders_status is the narrowest index covering
--     (status, id) and the optimiser scans it instead of the clustered index.
--     ix_orders_status then has user_scans = 8 and is no longer an example of
--     an index nobody reads. Every column referenced here lives only in the
--     clustered index, which forces a clustered scan and leaves the
--     nonclustered indexes untouched.
SELECT TOP (20) o.id, o.placed_at, COUNT_BIG(*) AS lines
FROM dbo.orders AS o
JOIN dbo.line_items AS li ON li.order_id = o.id
WHERE o.total_cents > 200000
GROUP BY o.id, o.placed_at
ORDER BY lines DESC;
GO 8

-- (e) Reads through ix_orders_customer_id, so that index has real user_seeks
--     and the tool must leave it alone. Without this the "don't drop indexes
--     that are in use" path is never exercised — every index in the database
--     would be unused and the check could pass by saying yes to everything.
SELECT COUNT_BIG(*) AS n FROM dbo.orders WHERE customer_id = 42;
GO 30

-- ---------------------------------------------------------------------------
-- 8. Flush Query Store
-- ---------------------------------------------------------------------------
-- DATA_FLUSH_INTERVAL_SECONDS is 60 even after section 1 lowered it, so the
-- workload above is still sitting in memory. Anything reading
-- sys.query_store_runtime_stats before this call sees an empty store.
-- The dm_exec_* views need no equivalent — they are read live off the cache.
EXEC sys.sp_query_store_flush_db;
GO
