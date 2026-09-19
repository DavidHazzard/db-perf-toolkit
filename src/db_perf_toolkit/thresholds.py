"""Ola Hallengren's published index-maintenance thresholds.

These live in a module of their own because four copies of them had
accumulated: `backends/sqlserver/indexes.py` used them to classify
fragmentation, `backends/sqlserver/maintenance.py` restated them as
IndexOptimize parameters, and `cli.py` restated them again for its `--help`
defaults. The CLI could not import either of the others without pulling in
pyodbc, which is an optional extra absent on a PostgreSQL-only install.

Nothing here imports a driver, so every one of those callers can share a
single definition rather than agreeing to stay in step.

Source: https://ola.hallengren.com/sql-server-index-and-statistics-maintenance.html
"""

from __future__ import annotations

#: Reorganize between this and REBUILD_ABOVE_PCT; rebuild above that.
REORGANIZE_ABOVE_PCT = 5.0
REBUILD_ABOVE_PCT = 30.0

#: Below roughly this many pages, fragmentation is noise: a small index
#: occupies few enough extents that page order barely affects reads, and
#: rebuilding it costs more than it returns.
MIN_PAGES = 1000

#: IndexOptimize's own parameters are whole percentages, so they are derived
#: from the values above rather than written out again.
INDEX_OPTIMIZE_LEVEL_1 = int(REORGANIZE_ABOVE_PCT)
INDEX_OPTIMIZE_LEVEL_2 = int(REBUILD_ABOVE_PCT)
