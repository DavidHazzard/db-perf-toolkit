-- Scenario 1 of 3: a small, ordinary application database.
--
-- One table with the everyday problems: a few unused indexes covering each
-- refusal category, some dead tuples, and a sequential-scan hotspot.

\timing on
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE orders (
    id           bigserial PRIMARY KEY,
    customer_id  bigint NOT NULL,
    reference    text   NOT NULL,
    notes        text   NOT NULL,
    total_cents  bigint NOT NULL,
    status       text   NOT NULL,
    placed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX orders_reference_key ON orders (reference);   -- unique       -> refused
ALTER TABLE orders ADD CONSTRAINT orders_id_uq UNIQUE (id);       -- constraint   -> refused
CREATE INDEX orders_notes_idx    ON orders (notes);               -- unused, big  -> DROP
CREATE INDEX orders_status_idx   ON orders (status);              -- unused, tiny -> under floor
CREATE INDEX orders_customer_idx ON orders (customer_id);         -- in use       -> untouched

CREATE TABLE audit_log (id bigserial PRIMARY KEY, order_id bigint, note text);

INSERT INTO orders (customer_id, reference, notes, total_cents, status)
SELECT i % 5000,
       'REF-' || i,
       'order notes for record number ' || i || ' with some padding text',
       (i * 37) % 250000,
       (ARRAY['placed','shipped','cancelled'])[1 + i % 3]
FROM generate_series(1, 400000) AS i;

INSERT INTO audit_log (order_id, note)
SELECT i, 'created' FROM generate_series(1, 120000) AS i;

DELETE FROM orders WHERE customer_id < 1200;
SELECT pg_stat_force_next_flush();
