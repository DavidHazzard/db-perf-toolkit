# Performance

Median of 5 runs, measured **in-process**. Milliseconds.

| Check | Small | Realistic | Horror | Growth |
|---|---|---|---|---|
| `slow-queries` | 6.6 | 7.0 | 6.4 | 1x |
| `seq-scans` | 0.7 | 1.0 | 1.0 | 1x |
| `unused-indexes` | 1.0 | 9.1 | 27.5 | 28x |
| `index-burden` | 1.0 | 8.2 | 19.0 | 19x |
| `bloat` | 0.6 | 1.9 | 3.2 | 5x |
| `blocking` | 0.6 | 0.7 | 0.6 | 1x |

## Why these are measured in-process

**CLI startup costs 1518 ms.** That is `uv run dbperf --version` — interpreter and import time before a single byte reaches PostgreSQL. It dominates end-to-end wall time at every scale.

A first pass at this benchmark reported ~1s per check and was measuring Python's import time, not the tool. The numbers above are the queries.

## Scaling

Catalog size grows **288x** from Small to Horror (7 to 2,017 indexes). The heaviest check grows about **28x** — sub-linear, because the statistics views are indexed and cost is dominated by `pg_relation_size()` calls per row returned.

`slow-queries` and `blocking` are flat: both read a fixed-size working set regardless of how many tables exist.

`free-space` is absent from this table because it reads table data rather than catalogs and is not comparable — it is also why it is excluded from `report`.
