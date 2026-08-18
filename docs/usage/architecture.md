# Architecture

```
cli.py          Click commands, exit codes, JSON vs terminal output
render.py       Rich tables and JSON serialisation
models.py       Engine-agnostic result types
manifest.py     Rollback manifests for destructive runs
safety.py       Guards applied before anything destructive
backends/
  base.py       Backend ABC + CheckUnavailable
  postgres.py   All PostgreSQL catalog queries
```

Backends own their SQL outright and translate results into the shared models, because the introspection queries for different engines have nothing in common. The CLI and renderers depend only on `models.py`, so a second engine needs no changes above the backend layer.

## Planning is separate from execution

Maintenance commands do not run SQL directly. Each backend returns `Operation` objects carrying their SQL, whether they are destructive, and their own rollback statement. `--script`, dry-run and `--execute` then consume the same objects rather than reimplementing the SQL per mode and drifting apart.

That is also the seam the SQL Server backend will use: there an `Operation`'s SQL is an `EXEC dbo.IndexOptimize ...` rather than DDL of our own, and everything downstream is unchanged.

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
