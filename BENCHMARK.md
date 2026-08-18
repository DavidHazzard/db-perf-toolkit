# Benchmark

Every check run **5 times** against three PostgreSQL 16 databases of escalating awfulness. Regenerate with:

```bash
./scripts/bench.py --runs 5 --out bench.json
./scripts/bench_report.py bench.json BENCHMARK.md
```

Scenario schemas live in [`scripts/scenarios/`](scripts/scenarios/).

## The three databases

| | Small | Realistic | Horror |
|---|---|---|---|
| | one app table | a decade of growth | college project → startup → PE → offshore |
| Size | 173 MB | 6.26 GB | 4.37 GB |
| Tables | 2 | 200 | 202 |
| Indexes | 7 | 852 | 2,017 |
| Heap | 61 MB | 3.84 GB | 2.44 GB |
| Index bytes | 105 MB | 2.41 GB | 1.91 GB |
| Index-to-heap | **172%** | **63%** | **78%** |

## Query time

Median of 5 runs, measured in-process. Milliseconds.

| Check | Small | Realistic | Horror | Scaling |
|---|---|---|---|---|
| `slow-queries` | 6.6 | 7.0 | 6.4 | 1× |
| `seq-scans` | 0.7 | 1.0 | 1.0 | 1× |
| `unused-indexes` | 1.0 | 9.1 | 27.5 | 28× |
| `index-burden` | 1.0 | 8.2 | 19.0 | 19× |
| `bloat` | 0.6 | 1.9 | 3.2 | 5× |
| `blocking` | 0.6 | 0.7 | 0.6 | 1× |

**CLI startup floor: 1518 ms.** That is `uv run dbperf --version` — interpreter and import cost before a single byte reaches PostgreSQL. It dominates end-to-end wall time at every scale, so the table above measures the queries rather than the launcher. A first pass at this benchmark reported ~1s per check and was measuring Python startup.

Catalog size grows **288x** from Small to Horror (7 to 2,017 indexes); the heaviest check grows about 28x. Sub-linear, because the statistics views are indexed and the cost is dominated by `pg_relation_size()` calls per row returned.

## What each database is guilty of

| | Small | Realistic | Horror |
|---|---|---|---|
| Unused indexes | 4 of 7 (57%) | 631 of 852 (74%) | 1,819 of 2,017 (90%) |
| Tables carrying unused indexes | 1 | 200 | 191 |
| Worst table | `orders` (5/6 unused) | `whale_1` (8/9 unused) | `orders_2020` (11/12 unused) |
| Redundant index writes | 2,480,000 | 134,382,804 | 142,353,282 |

## The finding that changed the tool

`drop-unused-indexes` applies an 8MB floor, borrowed from Ola Hallengren's `@MinNumberOfPages = 1000`. Running all three scenarios showed where that borrow breaks down.

| | Small | Realistic | Horror |
|---|---|---|---|
| Above floor — would drop | 1 → **58 MB** | 35 → **882 MB** | 33 → **414 MB** |
| Below floor — refused | 1 → 2 MB | 572 → 201 MB | 1,772 → 841 MB |

On the realistic database the floor works exactly as intended: the indexes it surfaces hold 4.4× more than everything it dismisses. **On the horror database it inverts** — the 1,772 indexes dismissed as too small to bother with hold 2.0× *more* than the 33 it would act on.

And bytes are the lesser cost. `orders_2020` carries 12 indexes, 11 unread; every INSERT pays 11 B-tree writes that serve no query, whether those indexes are 16kB or 16MB. A per-index size floor is structurally unable to see that.

That is what the `index-burden` check exists for: it ranks tables by `unused indexes × row modifications` rather than by size. The floor was a sound borrow for *maintenance* — rebuilding a tiny index really is pointless — and the wrong instrument for *dropping*.

## Remediation

`vacuum --execute` then `drop-unused-indexes --execute`, measured against the same three databases. Wall times include ~1s of CLI startup.

| | Small | Realistic | Horror |
|---|---|---|---|
| Dead tuples before | 96,000 | 4,777,136 | 7,196,662 |
| Dead tuples after | **0** | **0** | **0** |
| VACUUM time | 1.5s | 13.1s | 10.1s |
| Indexes dropped | 1 | 35 | 33 |
| Index bytes | 105 MB → **47 MB** | 2.41 GB → **1.55 GB** | 1.91 GB → **1.51 GB** |
| Database size | 173 MB → **115 MB** | 6.26 GB → **5.40 GB** | 4.37 GB → **3.97 GB** |
| Restore time | 1.8s (1 stmts) | 9.4s (35 stmts) | 5.0s (33 stmts) |

### VACUUM does not shrink the file

Every dead tuple was reclaimed — 7,196,662 on Horror alone — and heap size did not move: Small 61 MB → 61 MB, Realistic 3.84 GB → 3.84 GB, Horror 2.44 GB → 2.44 GB.

That is correct behaviour, not a failure. Plain `VACUUM` marks space reusable by future inserts; it does not return it to the operating system. Only `VACUUM FULL` does that, and it rewrites the table under an ACCESS EXCLUSIVE lock — an outage on anything large. **Every byte of on-disk reduction above came from dropping indexes, none from vacuuming.**

### CONCURRENTLY, verified under load

A second connection inserted continuously while 33 indexes were dropped from the Horror database. It committed **710 rows** with a worst-case stall of **0.6ms** and 0 errors.

`DROP INDEX CONCURRENTLY` takes a SHARE UPDATE EXCLUSIVE lock rather than an ACCESS EXCLUSIVE one, so writes keep flowing. Worth measuring rather than repeating from the manual — it is the difference between a maintenance window and an incident.

### Round trip

Each drop was restored from its manifest alone. On Horror that included `"Mixed.Case.Index"`, `"index'with'quotes"` and an index on a table named `"user data old"` — all recreated exactly, which is what the `sql.Identifier` quoting is for.

## Caveats

- Warm cache, single host, PostgreSQL 16 in Docker with `fsync=off`. These measure the tool's scaling, not your storage.
- `autovacuum=off` in every scenario, so bloat persists to be measured. Real servers reclaim continuously.
- Row counts are from the first run; timings from all runs.
- The diagnostic figures above are pre-remediation: they describe plans, not executed changes. The Remediation section is the only part where anything was written.
