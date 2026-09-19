# Architecture

```
cli.py          Click commands, exit codes, JSON vs terminal output
render.py       Rich tables and JSON serialisation
models.py       Engine-agnostic result types
manifest.py     Rollback manifests for destructive runs
safety.py       Guards applied before anything destructive
backends/
  base.py       Backend ABC + CheckUnavailable; engine dispatch from the DSN
  postgres.py   All PostgreSQL catalog queries
  sqlserver/    Split by concern, because one file was not going to hold it
    connection.py   DSN parsing, ODBC string, connection errors worth reading
    capabilities.py Edition and version probing: what this server can answer
    queries.py      Query Store and plan-cache checks
    indexes.py      Missing, unused, fragmented; index burden's refusal
    maintenance.py  IndexOptimize orchestration
```

`SqlServerBackend` composes those modules as mixins, and each declares the checks it implements. Nothing hand-maintains the union: `supports` is built from the parts, and a test asserts both directions — nothing advertised may fall through to the base class's refusal, and nothing implemented may go unadvertised. That test exists because the gap it catches is silent, and the tool's whole value is that it does not quietly claim things.

Backends own their SQL outright and translate results into the shared models, because the introspection queries for different engines have nothing in common. The CLI and renderers depend only on `models.py`, so a second engine needs no changes above the backend layer.

## Planning is separate from execution

Maintenance commands do not run SQL directly. Each backend returns `Operation` objects carrying their SQL, whether they are destructive, and their own rollback statement. `--script`, dry-run and `--execute` then consume the same objects rather than reimplementing the SQL per mode and drifting apart.

The SQL Server backend uses the same seam: there an `Operation`'s SQL is an `EXEC dbo.IndexOptimize ...` rather than DDL of our own, and everything downstream — `--script`, dry run, `--execute`, the rollback manifest — is unchanged.

Every planning method is declared on `Backend`, refusing by default, rather than only on the concrete backends. That is what lets the CLI hold a `Backend` and stay type-checked. It is not a formality: `plan_index_maintenance` was implemented on SQL Server and left off the seam, and the result was that the orchestration could not be reached from any command at all.

## CheckUnavailable

`CheckUnavailable` distinguishes "this server cannot answer that question" from "this tool is broken". The first is reported with a remedy and exit code 3; the second is a bug. A `report` run records unavailable checks as skipped and continues — a server without `pg_stat_statements` can still report bloat and blocking.

## Read-only enforcement

Read-only is enforced by the server, not by the discipline of the queries:

```sql
SET SESSION default_transaction_read_only = on
```

That specific line matters. psycopg's `Connection.read_only` attribute governs only transactions the driver opens itself, and in autocommit mode it opens none — so the attribute is silently inert and writes succeed. The integration suite asserts a `CREATE TABLE` is actually rejected, which is how that was caught.

## Identifier quoting

All generated DDL goes through `psycopg.sql.Identifier`. `Operation.target` is a quoted qualified name for the same reason — an unquoted `schema.name` cannot be parsed back apart once a name contains a dot, and the manifest is the audit trail for destructive work.

## Tests

```bash
uv run pytest
```

Integration tests run against a real PostgreSQL 16 container via testcontainers — no mocked cursors. The value of this tool is entirely in whether its catalog queries are correct, which only a live server can establish. The suite creates genuine dead tuples, drives real sequential scans, and opens a second connection to produce an actual lock wait.

Most of the maintenance tests assert that something is **refused**. A tool that drops the wrong index is worse than no tool.

The fixture disables autovacuum so dead tuples survive to be measured, and calls `pg_stat_force_next_flush()` after seeding — statistics are accumulated per-backend and flushed on a timer, so assertions made immediately after a workload otherwise fail intermittently.
