-- A deliberately awful 200-table schema.
--
-- Shaped like a real database that grew for a decade without a DBA: a few
-- whales, a medium tier, a long tail of small tables, and index hygiene that
-- nobody ever revisited.

\timing on
SET maintenance_work_mem = '1GB';
SET synchronous_commit = off;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ----------------------------------------------------------------------
-- 4 whales, ~500MB each
-- ----------------------------------------------------------------------
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..4 LOOP
    EXECUTE format($f$
      CREATE TABLE whale_%1$s (
        id          bigserial PRIMARY KEY,
        customer_id bigint NOT NULL,
        ref         text   NOT NULL,
        payload     text   NOT NULL,
        amount      bigint NOT NULL,
        status      text   NOT NULL,
        region      text   NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
      )$f$, i);

    EXECUTE format($f$
      INSERT INTO whale_%1$s (customer_id, ref, payload, amount, status, region)
      SELECT g %% 100000,
             'REF-%1$s-' || g,
             repeat('padding', 26),
             (g * 37) %% 1000000,
             (ARRAY['placed','shipped','cancelled','held'])[1 + g %% 4],
             (ARRAY['us-east','us-west','eu','apac'])[1 + g %% 4]
      FROM generate_series(1, 2000000) g$f$, i);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------
-- 16 medium tables, ~90MB each
-- ----------------------------------------------------------------------
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..16 LOOP
    EXECUTE format($f$
      CREATE TABLE ledger_%1$s (
        id        bigserial PRIMARY KEY,
        account   bigint NOT NULL,
        code      text   NOT NULL,
        memo      text   NOT NULL,
        cents     bigint NOT NULL,
        posted_at timestamptz NOT NULL DEFAULT now()
      )$f$, i);

    EXECUTE format($f$
      INSERT INTO ledger_%1$s (account, code, memo, cents)
      SELECT g %% 20000, 'CODE-' || (g %% 900), repeat('memo text ', 12), (g * 13) %% 500000
      FROM generate_series(1, 400000) g$f$, i);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------
-- 180 small tables — the long tail nobody remembers creating
-- ----------------------------------------------------------------------
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..180 LOOP
    EXECUTE format($f$
      CREATE TABLE lookup_%1$s (
        id    bigserial PRIMARY KEY,
        k     text NOT NULL,
        v     text NOT NULL,
        grp   bigint NOT NULL,
        flag  boolean NOT NULL DEFAULT false
      )$f$, i);

    EXECUTE format($f$
      INSERT INTO lookup_%1$s (k, v, grp)
      SELECT 'key-' || g, repeat('value ', 8), g %% 500
      FROM generate_series(1, 12000) g$f$, i);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------
-- Index hygiene: none
-- ----------------------------------------------------------------------

-- Whales: redundant prefixes, an exact duplicate, unique indexes nobody uses.
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..4 LOOP
    EXECUTE format('CREATE INDEX whale_%1$s_cust_idx        ON whale_%1$s (customer_id)', i);
    EXECUTE format('CREATE INDEX whale_%1$s_cust_status_idx ON whale_%1$s (customer_id, status)', i);
    EXECUTE format('CREATE INDEX whale_%1$s_cust_status_amt ON whale_%1$s (customer_id, status, amount)', i);
    -- Byte-for-byte duplicate of the first, under a different name.
    EXECUTE format('CREATE INDEX whale_%1$s_cust_copy_idx   ON whale_%1$s (customer_id)', i);
    EXECUTE format('CREATE INDEX whale_%1$s_payload_idx     ON whale_%1$s (payload)', i);
    EXECUTE format('CREATE UNIQUE INDEX whale_%1$s_ref_key  ON whale_%1$s (ref)', i);
    EXECUTE format('ALTER TABLE whale_%1$s ADD CONSTRAINT whale_%1$s_id_uq UNIQUE (id)', i);
    EXECUTE format('CREATE INDEX whale_%1$s_region_idx      ON whale_%1$s (region)', i);
  END LOOP;
END $$;

-- Medium tier: over-indexed.
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..16 LOOP
    EXECUTE format('CREATE INDEX ledger_%1$s_account_idx ON ledger_%1$s (account)', i);
    EXECUTE format('CREATE INDEX ledger_%1$s_code_idx    ON ledger_%1$s (code)', i);
    EXECUTE format('CREATE INDEX ledger_%1$s_memo_idx    ON ledger_%1$s (memo)', i);
    EXECUTE format('CREATE INDEX ledger_%1$s_cents_idx   ON ledger_%1$s (cents)', i);
    EXECUTE format('CREATE UNIQUE INDEX ledger_%1$s_ck   ON ledger_%1$s (account, id)', i);
  END LOOP;
END $$;

-- Long tail: three indexes each on a table nobody queries.
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..180 LOOP
    EXECUTE format('CREATE INDEX lookup_%1$s_k_idx   ON lookup_%1$s (k)', i);
    EXECUTE format('CREATE INDEX lookup_%1$s_grp_idx ON lookup_%1$s (grp)', i);
    EXECUTE format('CREATE INDEX lookup_%1$s_v_idx   ON lookup_%1$s (v)', i);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------
-- Bloat: mass deletes and updates, autovacuum is off
-- ----------------------------------------------------------------------
DO $$
DECLARE i int;
BEGIN
  FOR i IN 1..4 LOOP
    EXECUTE format('DELETE FROM whale_%1$s WHERE customer_id < 25000', i);
    EXECUTE format('UPDATE whale_%1$s SET status = ''revised'' WHERE amount %% 7 = 0', i);
  END LOOP;
  FOR i IN 1..16 LOOP
    EXECUTE format('DELETE FROM ledger_%1$s WHERE account < 6000', i);
  END LOOP;
END $$;

SELECT pg_size_pretty(pg_database_size(current_database())) AS total_size;
SELECT count(*) AS tables FROM pg_stat_user_tables;
SELECT count(*) AS indexes FROM pg_stat_user_indexes;
