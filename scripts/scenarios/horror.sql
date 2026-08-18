-- Ten years of decisions nobody was around to defend.
--
-- Era 1  college project      — everything text, no constraints, `data`/`temp`/`test1`
-- Era 2  startup              — copy-paste partitioning, 3am indexes never removed
-- Era 3  PE acquisition       — audit tables, soft deletes, nothing ever really deleted
-- Era 4  offshore rewrite     — index every column, backup tables, _final_v2_new

\timing on
SET maintenance_work_mem = '1GB';
SET synchronous_commit = off;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ======================================================================
-- ERA 1: the college project. 2014. Everything is text.
-- ======================================================================
CREATE TABLE "Users" (
    user_id   text,            -- no primary key
    email     text,
    created   text,            -- '2014-03-01', sometimes '03/01/2014'
    balance   text,            -- money. as text.
    is_active text             -- 'true' / 'TRUE' / '1' / 'yes'
);
INSERT INTO "Users" (user_id, email, created, balance, is_active)
SELECT g::text, 'user' || g || '@example.com',
       CASE WHEN g % 3 = 0 THEN '2014-03-01' ELSE '03/01/2014' END,
       ((g * 17) % 90000)::text,
       (ARRAY['true','TRUE','1','yes'])[1 + g % 4]
FROM generate_series(1, 300000) g;

CREATE TABLE data   (id text, stuff text, more_stuff text);
CREATE TABLE temp   (id text, val text);
CREATE TABLE test1  (a text, b text, c text);
INSERT INTO data  SELECT g::text, repeat('x', 60), repeat('y', 60) FROM generate_series(1, 200000) g;
INSERT INTO temp  SELECT g::text, repeat('z', 40) FROM generate_series(1, 150000) g;
INSERT INTO test1 SELECT g::text, g::text, g::text FROM generate_series(1, 100000) g;

-- ======================================================================
-- ERA 2: the startup. Partitioning by copy-paste; a table per year.
-- ======================================================================
DO $$
DECLARE y int;
BEGIN
  FOR y IN 2017..2023 LOOP
    EXECUTE format($f$
      CREATE TABLE orders_%1$s (
        id bigserial PRIMARY KEY, customer_id bigint, ref text, payload text,
        amount_text text, status text, region text, settings jsonb,
        created_at timestamptz DEFAULT now()
      )$f$, y);
    EXECUTE format($f$
      INSERT INTO orders_%1$s (customer_id, ref, payload, amount_text, status, region, settings)
      SELECT g %% 50000, 'R-%1$s-' || g, repeat('pad', 40), ((g*7) %% 100000)::text,
             (ARRAY['new','paid','void'])[1 + g %% 3],
             (ARRAY['us','eu','apac'])[1 + g %% 3],
             jsonb_build_object('prefs', jsonb_build_object('theme','dark','n', g %% 7))
      FROM generate_series(1, 350000) g$f$, y);
  END LOOP;
END $$;

-- ======================================================================
-- ERA 3: private equity. Audit everything, delete nothing.
-- ======================================================================
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..24 LOOP
    EXECUTE format($f$
      CREATE TABLE audit_%1$s (
        id bigserial PRIMARY KEY, entity text, entity_id bigint,
        before_json jsonb, after_json jsonb, actor text,
        is_deleted boolean DEFAULT false,          -- soft delete, never reclaimed
        changed_at timestamptz DEFAULT now()
      )$f$, i);
    EXECUTE format($f$
      INSERT INTO audit_%1$s (entity, entity_id, before_json, after_json, actor)
      SELECT 'orders', g, jsonb_build_object('s','new'), jsonb_build_object('s','paid'),
             'svc-account-' || (g %% 12)
      FROM generate_series(1, 250000) g$f$, i);
    -- Soft-delete most of it. The rows stay. The space stays. Forever.
    EXECUTE format('UPDATE audit_%1$s SET is_deleted = true WHERE id %% 10 <> 0', i);
  END LOOP;
END $$;

-- ======================================================================
-- ERA 4: the offshore rewrite. Backups, versions, index everything.
-- ======================================================================
CREATE TABLE users_backup_20240312 AS SELECT * FROM "Users";
CREATE TABLE users_bak             AS SELECT * FROM "Users";
CREATE TABLE users_old             AS SELECT * FROM "Users";
CREATE TABLE "users_FINAL"         AS SELECT * FROM "Users";
CREATE TABLE users_final_v2        AS SELECT * FROM "Users";
CREATE TABLE users_final_v2_new    AS SELECT * FROM "Users";
CREATE TABLE "user data old"       AS SELECT * FROM "Users";   -- spaces. in a table name.

DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..160 LOOP
    EXECUTE format($f$
      CREATE TABLE svc_config_%1$s (
        id bigserial PRIMARY KEY, k text, v text, grp bigint,
        enabled boolean DEFAULT true, deprecated boolean DEFAULT false,
        legacy boolean DEFAULT false, migrated boolean DEFAULT false,
        tier text, owner text
      )$f$, i);
    EXECUTE format($f$
      INSERT INTO svc_config_%1$s (k, v, grp, tier, owner)
      SELECT 'k'||g, repeat('v', 30), g %% 200, 'tier'||(g%%4), 'team'||(g%%9)
      FROM generate_series(1, 9000) g$f$, i);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------
-- Index hygiene: an index for every column, and then some
-- ----------------------------------------------------------------------

-- Yearly orders: every permutation somebody ever needed, kept forever.
DO $$
DECLARE y int;
BEGIN
  FOR y IN 2017..2023 LOOP
    EXECUTE format('CREATE INDEX ord_%1$s_c      ON orders_%1$s (customer_id)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_c_dup  ON orders_%1$s (customer_id)', y);  -- exact dup
    EXECUTE format('CREATE INDEX ord_%1$s_cs     ON orders_%1$s (customer_id, status)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_sc     ON orders_%1$s (status, customer_id)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_csr    ON orders_%1$s (customer_id, status, region)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_crs    ON orders_%1$s (customer_id, region, status)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_pay    ON orders_%1$s (payload)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_amt    ON orders_%1$s (amount_text)', y);
    EXECUTE format('CREATE INDEX ord_%1$s_reg    ON orders_%1$s (region)', y);
    EXECUTE format('CREATE UNIQUE INDEX ord_%1$s_ref ON orders_%1$s (ref)', y);
    EXECUTE format('ALTER TABLE orders_%1$s ADD CONSTRAINT ord_%1$s_id_uq UNIQUE (id)', y);
  END LOOP;
END $$;

-- Audit tables: indexed on booleans, which is close to useless.
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..24 LOOP
    EXECUTE format('CREATE INDEX aud_%1$s_ent    ON audit_%1$s (entity)', i);
    EXECUTE format('CREATE INDEX aud_%1$s_eid    ON audit_%1$s (entity_id)', i);
    EXECUTE format('CREATE INDEX aud_%1$s_del    ON audit_%1$s (is_deleted)', i);
    EXECUTE format('CREATE INDEX aud_%1$s_actor  ON audit_%1$s (actor)', i);
    EXECUTE format('CREATE INDEX aud_%1$s_when   ON audit_%1$s (changed_at)', i);
    EXECUTE format('CREATE INDEX aud_%1$s_ent2   ON audit_%1$s (entity, entity_id)', i);
  END LOOP;
END $$;

-- svc_config: an index on every column, including four booleans.
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..160 LOOP
    EXECUTE format('CREATE INDEX cfg_%1$s_k    ON svc_config_%1$s (k)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_v    ON svc_config_%1$s (v)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_g    ON svc_config_%1$s (grp)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_en   ON svc_config_%1$s (enabled)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_dep  ON svc_config_%1$s (deprecated)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_leg  ON svc_config_%1$s (legacy)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_mig  ON svc_config_%1$s (migrated)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_tier ON svc_config_%1$s (tier)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_own  ON svc_config_%1$s (owner)', i);
    EXECUTE format('CREATE INDEX cfg_%1$s_kv   ON svc_config_%1$s (k, v)', i);
  END LOOP;
END $$;

-- The identifiers that break naive tooling.
CREATE INDEX "Users email idx"        ON "Users" (email);
CREATE INDEX "users FINAL email idx"  ON "users_FINAL" (email);
CREATE INDEX "user data old idx"      ON "user data old" (email);
CREATE INDEX "Mixed.Case.Index"       ON users_bak (email);
CREATE INDEX "index'with'quotes"      ON users_old (email);

-- ----------------------------------------------------------------------
-- Churn: the reason nothing ever gets smaller
-- ----------------------------------------------------------------------
DO $$
DECLARE y int;
BEGIN
  FOR y IN 2017..2023 LOOP
    EXECUTE format('UPDATE orders_%1$s SET status = ''void'' WHERE id %% 3 = 0', y);
    EXECUTE format('DELETE FROM orders_%1$s WHERE customer_id < 20000', y);
  END LOOP;
END $$;

SELECT pg_size_pretty(pg_database_size(current_database())) AS total_size;
SELECT count(*) AS tables  FROM pg_stat_user_tables;
SELECT count(*) AS indexes FROM pg_stat_user_indexes;
