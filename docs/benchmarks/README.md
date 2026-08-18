# Benchmarks

Every check run **5 times** against three PostgreSQL 16 databases of escalating awfulness, plus a full remediation pass on each.

| | Small | Realistic | Horror |
|---|---|---|---|
|  | one app table | a decade of growth | college project to startup to PE to offshore |
| Size | 173 MB | 6.26 GB | 4.37 GB |
| Indexes | 7 | 852 | 2,017 |
| Unused | 4 (57%) | 631 (74%) | 1,819 (90%) |
| Slowest check | 6.6 ms | 9.1 ms | 27.5 ms |

## Contents

- [Scenarios](scenarios.md) — what the three databases contain
- [Performance](performance.md) — query timings and how they scale
- [Findings](findings.md) — the measurement that changed the tool
- [Remediation](remediation.md) — what the write path actually did

## Reproducing

```bash
./scripts/bench.py --runs 5 --out bench.json
./scripts/bench_remediate.py --out remediate.json    # destructive
./scripts/bench_report.py bench.json docs/benchmarks remediate.json
```

Scenario schemas are in [`scripts/scenarios/`](../../scripts/scenarios/).

## Caveats

- Warm cache, single host, PostgreSQL 16 in Docker with `fsync=off`. These measure the tool's scaling, not your storage.
- `autovacuum=off` in every scenario, so bloat persists to be measured. Real servers reclaim continuously.
- Diagnostic figures describe **plans**, not executed changes. Only [remediation](remediation.md) wrote anything.
