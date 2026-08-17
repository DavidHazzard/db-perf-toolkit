#!/usr/bin/env bash
#
# End-to-end demonstration: build a database with real problems, diagnose it,
# remediate it, then diagnose it again.
#
#   ./scripts/demo.sh            run it
#   ./scripts/demo.sh --keep     leave the container up afterwards
#
# Requires Docker and psql.

set -euo pipefail

CONTAINER=dbperf-demo
PORT=55432
DSN="postgresql://postgres:demo@localhost:${PORT}/shop"
KEEP=${1:-}

export PGPASSWORD=demo
export DBPERF_DSN="$DSN"

cd "$(dirname "$0")/.."

rule() { printf '\n\033[1;36m%s\033[0m\n' "══ $* ══"; }
psql_q() { psql -h localhost -p "$PORT" -U postgres -d shop -q -v ON_ERROR_STOP=1 "$@"; }

cleanup() {
  if [[ "$KEEP" != "--keep" ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

rule "Starting PostgreSQL"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD=demo -e POSTGRES_DB=shop -p "${PORT}:5432" \
  postgres:16 \
  postgres -c shared_preload_libraries=pg_stat_statements \
           -c pg_stat_statements.track=all \
           -c autovacuum=off >/dev/null

# pg_isready is not sufficient: the entrypoint runs a temporary server for
# initdb and then restarts, so readiness must be proven with a real query.
for _ in $(seq 1 90); do
  psql -h localhost -p "$PORT" -U postgres -d shop -tAc "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done
echo "ready"

rule "Creating a schema with problems in it"
psql_q <<'SQL'
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

-- Every refusal category, plus one index that genuinely should go.
CREATE UNIQUE INDEX orders_reference_key ON orders (reference);          -- unique      -> refused
ALTER TABLE orders ADD CONSTRAINT orders_id_uq UNIQUE (id);              -- constraint  -> refused
CREATE INDEX orders_notes_idx  ON orders (notes);                        -- unused, big -> DROP
CREATE INDEX orders_status_idx ON orders (status);                       -- unused, tiny-> under floor
CREATE INDEX orders_customer_idx ON orders (customer_id);                -- actually used -> untouched

CREATE TABLE audit_log (
    id       bigserial PRIMARY KEY,
    order_id bigint,
    note     text
);

INSERT INTO orders (customer_id, reference, notes, total_cents, status)
SELECT i % 5000,
       'REF-' || i,
       'order notes for record number ' || i || ' with some padding text',
       (i * 37) % 250000,
       (ARRAY['placed','shipped','cancelled'])[1 + i % 3]
FROM generate_series(1, 400000) AS i;

INSERT INTO audit_log (order_id, note)
SELECT i, 'created' FROM generate_series(1, 120000) AS i;
SQL

rule "Applying a workload"
# Sequential scans on an unindexed column.
for _ in $(seq 1 40); do
  psql -h localhost -p "$PORT" -U postgres -d shop -tAc \
    "SELECT count(*) FROM orders WHERE total_cents > 100000;" >/dev/null
done
# An expensive join.
for _ in $(seq 1 12); do
  psql -h localhost -p "$PORT" -U postgres -d shop -tAc \
    "SELECT o.status, count(*) FROM orders o JOIN audit_log a ON a.order_id = o.id GROUP BY 1;" >/dev/null
done
# Keep one index genuinely in use, so the tool must leave it alone.
for _ in $(seq 1 30); do
  psql -h localhost -p "$PORT" -U postgres -d shop -tAc \
    "SELECT count(*) FROM orders WHERE customer_id = 42;" >/dev/null
done
# Dead tuples, with autovacuum off so they persist.
psql_q -c "DELETE FROM orders WHERE customer_id < 1200;" >/dev/null
psql_q -c "SELECT pg_stat_force_next_flush();" >/dev/null
echo "done"

rule "1. DIAGNOSE — before"
uv run dbperf report --limit 5

rule "2. REMEDIATE — what would happen (dry run)"
uv run dbperf drop-unused-indexes

rule "2b. REMEDIATE — the SQL, with rollback"
uv run dbperf drop-unused-indexes --script

rule "3. REMEDIATE — apply"
uv run dbperf vacuum --execute
uv run dbperf drop-unused-indexes --execute --yes

rule "4. DIAGNOSE — after"
psql_q -c "SELECT pg_stat_force_next_flush();" >/dev/null
uv run dbperf report --limit 5

rule "5. The stats-window guard"
echo "Resetting statistics, which makes every counter meaningless..."
psql_q -c "SELECT pg_stat_reset();" >/dev/null
uv run dbperf drop-unused-indexes --execute --yes || echo "(refused, as it should be — exit $?)"

if [[ "$KEEP" == "--keep" ]]; then
  rule "Container left running"
  echo "  export DBPERF_DSN='$DSN'"
  echo "  docker rm -f $CONTAINER   # when finished"
fi
