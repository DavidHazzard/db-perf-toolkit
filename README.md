# db-perf-toolkit

Point it at a PostgreSQL database and it surfaces slow queries, unused indexes, table bloat, sequential-scan hotspots, and lock contention — then, if you ask it to, fixes what it safely can.

**Read-only by default.** Every destructive change is previewed, guarded, and reversible.

```console
$ dbperf --dsn postgresql://user@host/shop report
```

```
16.14 (Debian 16.14-1.pgdg13+1)  ·  statistics never reset (counters cover full server uptime)

Slowest statements by total execution time
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┓
┃ Query                       ┃ Calls ┃    Total ┃     Mean ┃    Rows ┃ % time ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━┩
│ SELECT o.status, count(*)   │    15 │   1.01 s │  67.1 ms │      45 │  30.5% │
│ FROM orders o JOIN          │       │          │          │         │        │
│ audit_log a ON a.order_i…   │       │          │          │         │        │
└─────────────────────────────┴───────┴──────────┴──────────┴─────────┴────────┘

Indexes with no recorded scans
┏━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Table  ┃ Index                ┃ Scans ┃    Size ┃ Safe to drop?     ┃
┡━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ orders │ orders_reference_key │     0 │   16 MB │ no — unique index │
│ orders │ orders_status_idx    │     0 │ 1912 kB │ likely            │
└────────┴──────────────────────┴───────┴─────────┴───────────────────┘
```

## Install

```bash
uv tool install db-perf-toolkit
export DBPERF_DSN=postgresql://user@host/dbname
dbperf report
```

Full requirements, extensions and permissions: **[docs/usage/installation.md](docs/usage/installation.md)**

## See it work

```bash
./scripts/demo.sh
```

Spins up a throwaway PostgreSQL container, builds a schema with real problems in it, diagnoses, remediates, and diagnoses again. Roughly 90 seconds, and it cleans up after itself.

One pass takes the demo table from 157 MB to 99 MB and clears 96,000 dead tuples. The interesting part is what it *declines* to do — four indexes had zero recorded scans and exactly one was dropped.

## Documentation

**Usage**

| | |
|---|---|
| [Installation](docs/usage/installation.md) | Requirements, extensions, permissions, connecting |
| [Diagnostics](docs/usage/diagnostics.md) | The read-only checks and how to read them |
| [Maintenance](docs/usage/maintenance.md) | Write mode, guards, rollback |
| [Architecture](docs/usage/architecture.md) | Internals, backend design, tests |
| [Ola Hallengren](docs/usage/ola-hallengren.md) | What was borrowed, and what deliberately was not |

**Benchmarks**

| | |
|---|---|
| [Overview](docs/benchmarks/README.md) | Headline results across three databases |
| [Scenarios](docs/benchmarks/scenarios.md) | The three test databases |
| [Performance](docs/benchmarks/performance.md) | Query timings and how they scale |
| [Findings](docs/benchmarks/findings.md) | The measurement that changed the tool |
| [Remediation](docs/benchmarks/remediation.md) | What the write path actually did |

## Two things worth knowing before pointing this at production

**`VACUUM` does not shrink the file.** All 12M dead tuples across the benchmark databases were reclaimed and heap size did not move by a byte. Plain `VACUUM` marks space reusable; only `VACUUM FULL` returns it to the OS, and that rewrites the table under an `ACCESS EXCLUSIVE` lock. Every byte of on-disk reduction in those runs came from dropping indexes.

**`bloat` and `free-space` are different questions.** Dead tuples read zero after a vacuum while the file stays large. On the worst benchmark database, `bloat` reports nothing while `free-space` finds 31 tables holding 1.03 GB.

## Roadmap

- SQL Server diagnosis behind the same CLI
- SQL Server maintenance by orchestrating Ola Hallengren's `IndexOptimize`
- Object selection syntax: `--tables 'public.%,-public.audit_%'`
- Run history in local SQLite, so "unused across six runs" replaces a single snapshot
- `VACUUM FULL` / `pg_repack` support, now that `free-space` can measure what it would return

## Contributing

Issues and PRs welcome. `uv run ruff check .` and `uv run pytest` should pass; tests need a working Docker daemon.

## License

MIT
