# Remediation

`vacuum --execute` then `drop-unused-indexes --execute`, against the same three databases. Wall times include ~1s of CLI startup.

| | Small | Realistic | Horror |
|---|---|---|---|
| Dead tuples before | 96,000 | 4,777,136 | 7,196,662 |
| Dead tuples after | **0** | **0** | **0** |
| VACUUM time | 1.5s | 13.1s | 10.1s |
| Indexes dropped | 1 | 35 | 33 |
| Index bytes | 105 MB → **47 MB** | 2.41 GB → **1.55 GB** | 1.91 GB → **1.51 GB** |
| Database size | 173 MB → **115 MB** | 6.26 GB → **5.40 GB** | 4.37 GB → **3.97 GB** |
| Restore | 1.8s (1 stmts) | 9.4s (35 stmts) | 5.0s (33 stmts) |

## VACUUM does not shrink the file

Every dead tuple was reclaimed — 7,196,662 on Horror alone — and heap size did not move: Small 61 MB → 61 MB, Realistic 3.84 GB → 3.84 GB, Horror 2.44 GB → 2.44 GB.

That is correct behaviour, not a failure. Plain `VACUUM` marks space reusable by future inserts; it does not return it to the operating system. Only `VACUUM FULL` does, and it rewrites the table under an `ACCESS EXCLUSIVE` lock — an outage on anything large.

**Every byte of on-disk reduction above came from dropping indexes, none from vacuuming.**

## CONCURRENTLY, verified under load

A second connection inserted continuously while 33 indexes were dropped from the Horror database. It committed **710 rows** with a worst-case stall of **0.6 ms** and 0 errors.

`DROP INDEX CONCURRENTLY` takes a `SHARE UPDATE EXCLUSIVE` lock rather than an `ACCESS EXCLUSIVE` one, so writes keep flowing. Worth measuring rather than repeating from the manual — it is the difference between a maintenance window and an incident.

## Round trip

Each drop was restored from its manifest alone. On Horror that included `"Mixed.Case.Index"`, `"index'with'quotes"` and an index on a table named `"user data old"` — all recreated exactly, which is what the `sql.Identifier` quoting is for.
