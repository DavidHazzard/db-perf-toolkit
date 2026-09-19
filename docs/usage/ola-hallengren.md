# Relationship to Ola Hallengren's Maintenance Solution

[Ola Hallengren's SQL Server Maintenance Solution](https://ola.hallengren.com/) (MIT) is the de-facto standard for SQL Server maintenance. This tool does not compete with it and does not vendor it.

The division is straightforward: **his solution maintains, this one diagnoses** — and on SQL Server, hands the maintenance to his.

## Borrowed as design, reimplemented for PostgreSQL

| His | Here |
|---|---|
| `@Execute='N'` | `--script`, emitting reviewable SQL instead of running it |
| `@MinNumberOfPages = 1000` | The 8MB index floor — 1000 PostgreSQL pages is 8MB |
| `LogToTable` | The rollback manifest, on local disk rather than the target database |

One borrow turned out to be wrong in translation. `@MinNumberOfPages` is sound for *maintenance* — rebuilding a tiny index really is pointless — and the wrong instrument for *dropping*, where the dominant cost is write amplification rather than bytes. That is what [`index-burden`](../benchmarks/findings.md) exists to cover.

## Deliberately not borrowed

**Execute-by-default.** `@Execute='Y'` is a reasonable default for a solution with fifteen years of production hardening. Dry-run is the right default here.

**The action model.** His solution does backups and `DBCC CHECKDB`; those are a different product.

## How SQL Server uses it

Orchestration, not reimplementation. `dbperf index-maintenance` detects `IndexOptimize` and `CommandLog` in the target database and drives them through the same plan/dry-run/execute pipeline as every other maintenance command. His procedures stay his, installed and updated through his own channels. If they are not installed, the command says so and stops — it does not fall back to something homegrown.

There are two independent dry runs, and they nest. Without `--execute`, `dbperf` prints the plan and runs nothing. The generated call also carries IndexOptimize's own `@Execute = 'N'` unless `--execute` is given, so even a hand-copied `EXEC` out of `--script` still only prints what it would do.

Version awareness is the part to design up front: Query Store is 2016+ with `sys.dm_exec_query_stats` as fallback, and `sys.dm_db_missing_index_details` differs on Azure SQL Database — which is why his solution ships a separate script for it.
