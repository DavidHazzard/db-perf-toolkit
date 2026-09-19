"""Index checks: what nothing reads, what the optimiser wants, what has drifted.

Three checks live here. Two of them have PostgreSQL counterparts and are
deliberately kept faithful to them; one has no counterpart at all and is here
because SQL Server genuinely knows something PostgreSQL does not.

WHAT IS NOT HERE, AND WHY: INDEX_BURDEN
---------------------------------------
`TableIndexBurden.writes` is documented as row modifications — PostgreSQL's
`n_tup_ins + n_tup_upd + n_tup_del`. The obvious SQL Server source is
`sys.dm_db_index_usage_stats.user_updates`, and it is the wrong number:
**it counts statements, not rows.** Measured on 2022 CU27 against the seeded
scenario, `user_updates` was **1** after an INSERT of 200,000 rows.

That is not a rounding difference, it is a different quantity. The entire
argument of index-burden is per-row write amplification — ten redundant
indexes on a hot table cost ten extra B-tree writes *per row* — and a column
headed "writes" holding 1 where PostgreSQL holds 200,000 would make the
ranking meaningless and the number actively misleading.

The row-level alternative was investigated and rejected:
`sys.dm_db_stats_properties.modification_counter` is genuinely per-row, but it
is per-*statistic* rather than per-table, and it **resets to zero every time
the statistic is updated**. Measured against the same scenario after
`UPDATE STATISTICS ... WITH FULLSCAN`, every statistic on `dbo.orders` read
`modification_counter = 0` — immediately after the 200,000-row load that
built the table. On any database with auto-update statistics on (the default)
the counter is reset by the very write volume it is being asked to measure, so
a table under heavy churn reports a *small* number. Ranking tables by it would
invert the answer on precisely the tables that matter.

So this backend does not claim INDEX_BURDEN. `Check.INDEX_BURDEN` is absent
from `IndexChecks.supports`, the base class refuses it with "SQL Server has no
equivalent of this check", and the operator reads an honest refusal instead of
a plausible table of wrong numbers. `TableIndexBurden.writes_unit` exists for
a backend that can do better; nothing here can, so nothing here pretends to.

THE TRAP IN sys.dm_db_index_usage_stats
---------------------------------------
An index that has not been touched since the service started **has no row in
that DMV at all**. Not a row of zeros — no row. Measured: immediately after
`CREATE TABLE` plus `CREATE INDEX`, both indexes were absent, and the row
appeared only on the first read or DML.

An INNER JOIN to that DMV therefore silently drops exactly the indexes this
check exists to find. `unused_indexes` LEFT JOINs from `sys.indexes` and reads
a missing row as zero usage, which is what it means.

This is the stats-window guard arriving by a second route. `stats_window()`
protects against a restart making every index *look* unused by reporting a
zero; this protects against a restart making an unused index *invisible* by
reporting nothing. Both failures are the same failure, and the second one is
worse because an empty result reads like a clean bill of health.

COMPOSITION
-----------
These checks are exposed twice: as module-level functions taking a backend,
and as `IndexChecks`, a `SqlServerBackend` subclass declaring only the checks
implemented here. The final backend class is composed from the check mixins
elsewhere, so `supports` is a union of what the mixins actually provide and
can never advertise a check the composed class cannot answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from db_perf_toolkit.backends.sqlserver.connection import SqlServerBackend
from db_perf_toolkit.models import Check, IndexFragmentation, MissingIndex, UnusedIndex
from db_perf_toolkit.thresholds import MIN_PAGES, REBUILD_ABOVE_PCT, REORGANIZE_ABOVE_PCT

#: Ola Hallengren's thresholds, and they are his numbers rather than ours:
#: IndexOptimize defaults to @FragmentationLevel1 = 5 and
#: @FragmentationLevel2 = 30, reorganizing between the two and rebuilding above
#: the upper one. Named after him because they are a widely-adopted convention
#: with a known provenance, not a measurement — the right threshold for a given
#: index depends on how it is read, and anyone who has measured their own
#: should pass their own.

#: Also Ola's default (@MinNumberOfPages = 1000). Below roughly eight pages an
#: index lives on mixed extents and avg_fragmentation_in_percent is noise
#: rather than a finding; well above that, a small index is cheap to rebuild
#: and rebuilding it buys nothing worth the log it writes.

ACTION_REBUILD = "rebuild"
ACTION_REORGANIZE = "reorganize"
ACTION_NONE = "none"

#: sys.dm_db_index_physical_stats scan modes, cheapest first.
#:
#: LIMITED is the default and reads only the parent level of the B-tree, which
#: is what makes this check safe to point at a production database. DETAILED
#: reads every page of every index; on a large database that is not a slow
#: query, it is an incident — the same trap `free_space` documents on the
#: PostgreSQL side, where pgstattuple scans every page and the approximate
#: variant exists precisely so the tool does not do that by default.
#:
#: The cost of LIMITED is that avg_page_space_used_in_percent comes back NULL,
#: so `page_density_pct` is None unless SAMPLED or DETAILED is asked for. That
#: is why the field is optional on the model: the cheap reading genuinely does
#: not know.
SCAN_MODE_LIMITED = "LIMITED"
SCAN_MODE_SAMPLED = "SAMPLED"
SCAN_MODE_DETAILED = "DETAILED"
SCAN_MODES = (SCAN_MODE_LIMITED, SCAN_MODE_SAMPLED, SCAN_MODE_DETAILED)

#: sysname. A generated index name longer than this is rejected by the server,
#: so the generated CREATE statement would not run.
MAX_IDENTIFIER_LENGTH = 128

_DMV_REMEDY = (
    "The index DMVs need the server-state permission:\n"
    "  GRANT VIEW SERVER STATE TO [<login>];\n"
    "On Azure SQL Database: GRANT VIEW DATABASE STATE TO [<user>];"
)


def unused_indexes(backend: SqlServerBackend, max_scans: int = 0) -> list[UnusedIndex]:
    """Nonclustered indexes nothing has read, with the DDL to put them back.

    The drop-safety semantics are the PostgreSQL backend's, unchanged:

    * **Primary keys are excluded outright.** An unused primary key is still a
      primary key, and offering one for deletion is dangerous advice however it
      is captioned. `sys.indexes.is_primary_key` is the filter.
    * **Unique and constraint-backed indexes are listed, and flagged unsafe.**
      They are reported because knowing a unique index is never read is worth
      knowing; they are flagged because dropping one changes what rows the
      table will accept, which is a schema change wearing a cleanup's clothing.
      `is_unique` and `is_unique_constraint` map onto the model's `is_unique`
      and `enforces_constraint`, and `safety.index_drop_refusal` then refuses
      them for exactly the reasons it refuses their PostgreSQL equivalents. A
      bare CREATE UNIQUE INDEX sets the first and not the second — the same
      split PostgreSQL has between a unique index and a unique constraint.

    Two SQL Server-specific exclusions, neither of which has a PostgreSQL
    counterpart:

    * **The clustered index is not a candidate.** It *is* the table: dropping
      it rewrites every row into a heap and frees nothing, so "unused" is not
      a meaningful reading of its counters.
    * **Only B-tree indexes are reported** (`sys.indexes.type` 1 and 2).
      Columnstore, XML, spatial and hash indexes each need a different CREATE
      statement, and this check will not offer a drop it cannot write a
      rollback for.

    `scans` is user_seeks + user_scans + user_lookups. `user_updates` is
    excluded deliberately: it is the maintenance cost of the index, not
    evidence anybody read it, and an index that is written but never read is
    the purest case this check exists to find.

    An index with no row in the usage DMV counts as zero scans — see the module
    docstring; that absence is the whole reason this query LEFT JOINs.
    """
    with backend._denied_as_unavailable("unused-indexes", _DMV_REMEDY):
        rows = backend._query(_UNUSED_INDEXES_SQL, max_scans)

    return [
        UnusedIndex(
            schema=str(row["schema_name"]),
            table=str(row["table_name"]),
            index=str(row["index_name"]),
            scans=int(row["scans"]),
            size_bytes=int(row["size_bytes"]),
            size_pretty=_size_pretty(int(row["size_bytes"])),
            definition=_create_index_statement(backend, row),
            is_unique=bool(row["is_unique"]),
            enforces_constraint=bool(row["is_unique_constraint"]),
        )
        for row in rows
    ]


def missing_indexes(backend: SqlServerBackend, min_impact: float = 0.0) -> list[MissingIndex]:
    """Indexes the optimiser says it wanted, ranked by the conventional score.

    This check has no PostgreSQL equivalent, and that asymmetry is the point:
    `sys.dm_db_missing_index_details` is a real server-side recommendation,
    written by the optimiser as it compiles plans. PostgreSQL's nearest
    offering is `seq-scans`, which reports candidates for EXPLAIN rather than
    recommendations, because inventing one would be a lie the tool cannot back
    up. See `MissingIndex`.

    `impact_score` is the conventional formula —
    `avg_total_user_cost * avg_user_impact * (user_seeks + user_scans)` — and
    it deserves to be read for what it is. `avg_user_impact` is the optimiser's
    own estimate of how much cheaper *the plans it already compiled* would have
    been, produced from the same cost model that produced those plans. It is
    not a measurement and it is not a promise: it takes no account of the write
    cost of the index it is asking for, of indexes that already almost cover
    the query, or of the fact that three of these rows often want three
    overlapping indexes where one would serve. It ranks candidates for
    investigation. Ranking is all it does.

    Two further things this DMV will not tell you, both worth knowing before
    acting on a row:

    * The suggestions are **cleared by any index DDL on the table** and by a
      service restart, so an empty result may mean "recently rebuilt" rather
      than "nothing wanted".
    * The equality and inequality columns arrive in no useful order — the DMV
      does not choose a key order, so the generated statement lists equality
      columns first and then inequality columns, which is the conventional
      rule and still not necessarily the best key order for this workload.

    The generated name is a placeholder built from the columns; rename it to
    whatever the local convention is before running it.
    """
    with backend._denied_as_unavailable("missing-indexes", _DMV_REMEDY):
        rows = backend._query(_MISSING_INDEXES_SQL, min_impact)

    return [
        MissingIndex(
            schema=str(row["schema_name"]),
            table=str(row["table_name"]),
            equality_columns=_opt_str(row["equality_columns"]),
            inequality_columns=_opt_str(row["inequality_columns"]),
            included_columns=_opt_str(row["included_columns"]),
            impact_score=float(row["impact_score"]),
            seeks=int(row["seeks"]),
            scans=int(row["scans"]),
            last_seen=_as_utc(row["last_seen_utc"]),
            create_statement=_missing_index_statement(backend, row),
        )
        for row in rows
    ]


def index_fragmentation(
    backend: SqlServerBackend,
    min_pct: float = REORGANIZE_ABOVE_PCT,
    min_pages: int = MIN_PAGES,
    *,
    mode: str = SCAN_MODE_LIMITED,
) -> list[IndexFragmentation]:
    """How far each index's physical page order has drifted from its logical one.

    SQL Server's nearest analogue of bloat, and not the same thing — see
    `IndexFragmentation`. There are no dead tuples here to vacuum; there are
    pages that split, and a scan that consequently walks the disk out of order.

    `mode` is the cost dial and it defaults to the cheap end. LIMITED reads
    only the parent level of the B-tree. DETAILED reads **every page of every
    index**, which on a large database is a performance incident rather than a
    slow query, and this check must never be the reason someone gets paged.
    SAMPLED reads 1% of pages and is the setting that gets you
    `page_density_pct`; under LIMITED that column is NULL at the server and
    None on the model, because the cheap reading genuinely does not know it.

    `recommended_action` uses Ola Hallengren's thresholds — reorganize above
    5%, rebuild above 30% — because his IndexOptimize is the de facto standard
    maintenance solution and matching it means the recommendation here and the
    action his procedure would take do not contradict each other. They are a
    convention with a known provenance, not a measurement.

    `min_pages` matters more than it looks. Fragmentation on a tiny index is
    noise — below about eight pages the index lives on mixed extents and the
    percentage means nothing at all — and rebuilding one costs log and buys
    nothing. The default is Ola's 1000.

    Rows are per index, not per partition: a partitioned index is aggregated
    with its fragmentation weighted by page count, so one badly fragmented
    partition of forty does not present as a badly fragmented index.
    """
    scan_mode = mode.upper()
    if scan_mode not in SCAN_MODES:
        raise ValueError(f"Unknown scan mode {mode!r}. Expected one of {', '.join(SCAN_MODES)}.")

    # The mode is interpolated because it is an argument to a table-valued
    # function rather than a value in a predicate. What makes that safe is the
    # membership test above, not the shape of the string.
    statement = _FRAGMENTATION_SQL.format(mode=scan_mode)

    with backend._denied_as_unavailable("fragmentation", _DMV_REMEDY):
        rows = backend._query(statement, min_pages, min_pct)

    return [
        IndexFragmentation(
            schema=str(row["schema_name"]),
            table=str(row["table_name"]),
            index=str(row["index_name"]),
            fragmentation_pct=float(row["fragmentation_pct"]),
            page_count=int(row["page_count"]),
            page_density_pct=(
                None if row["page_density_pct"] is None else float(row["page_density_pct"])
            ),
            recommended_action=recommended_action(float(row["fragmentation_pct"])),
        )
        for row in rows
    ]


def recommended_action(fragmentation_pct: float) -> str:
    """Ola Hallengren's thresholds, applied.

    Public because the maintenance planner has to agree with the report: a run
    that says "rebuild" and then reorganizes is worse than either.
    """
    if fragmentation_pct >= REBUILD_ABOVE_PCT:
        return ACTION_REBUILD
    if fragmentation_pct >= REORGANIZE_ABOVE_PCT:
        return ACTION_REORGANIZE
    return ACTION_NONE


class IndexChecks(SqlServerBackend):
    """The index checks, as a backend mixin.

    `supports` names only what this module implements. INDEX_BURDEN is absent
    on purpose and the module docstring says why — `user_updates` counts
    statements, not rows.
    """

    supports: frozenset[Check] = frozenset(
        {Check.UNUSED_INDEXES, Check.MISSING_INDEXES, Check.FRAGMENTATION}
    )

    def unused_indexes(self, max_scans: int) -> list[UnusedIndex]:
        return unused_indexes(self, max_scans)

    def missing_indexes(self, min_impact: float) -> list[MissingIndex]:
        return missing_indexes(self, min_impact)

    def index_fragmentation(
        self,
        min_pct: float = REORGANIZE_ABOVE_PCT,
        min_pages: int = MIN_PAGES,
        *,
        mode: str = SCAN_MODE_LIMITED,
    ) -> list[IndexFragmentation]:
        return index_fragmentation(self, min_pct, min_pages, mode=mode)


# ---------------------------------------------------------------------------
# Statement construction
# ---------------------------------------------------------------------------


def _create_index_statement(backend: SqlServerBackend, row: dict[str, Any]) -> str:
    """Reconstruct a runnable CREATE INDEX for an existing index.

    There is no `pg_get_indexdef` here, so this is assembled from the catalog
    by hand, and it has to be right: `plan_drop_unused_indexes` puts it in the
    rollback manifest, and "we dropped your index and you can work out how to
    rebuild it" is not a recoverable position.

    Everything that changes the resulting index is carried: key order and
    direction, included columns, the filter predicate, fill factor, padding,
    locking options, data compression, and the filegroup or partition scheme
    it lives on. Options left at their defaults are omitted rather than
    spelled out, because a statement someone has to read before running is
    better short.

    The one thing it does not reproduce is per-partition data compression: the
    first partition's setting is applied to the whole index. A partitioned
    index with mixed compression is rare and the alternative is a statement
    nobody can check by eye.
    """
    unique = "UNIQUE " if row["is_unique"] else ""
    statement = (
        f"CREATE {unique}{row['type_desc']} INDEX {backend._quote(str(row['index_name']))}"
        f" ON {backend._target(str(row['schema_name']), str(row['table_name']))}"
        f" ({row['key_columns']})"
    )
    if row["included_columns"]:
        statement += f" INCLUDE ({row['included_columns']})"
    if row["filter_definition"]:
        statement += f" WHERE {row['filter_definition']}"

    options = _index_options(row)
    if options:
        statement += f" WITH ({', '.join(options)})"

    data_space = backend._quote(str(row["data_space_name"]))
    if str(row["data_space_type"]).strip() == "PS" and row["partition_column"]:
        # A partition scheme needs the partitioning column, and getting this
        # wrong turns a rollback into an unpartitioned index on one filegroup.
        data_space += f"({row['partition_column']})"
    return f"{statement} ON {data_space};"


def _index_options(row: dict[str, Any]) -> list[str]:
    """Only the options that differ from what CREATE INDEX would default to."""
    options: list[str] = []
    if row["is_padded"]:
        options.append("PAD_INDEX = ON")
    if int(row["fill_factor"] or 0):
        options.append(f"FILLFACTOR = {int(row['fill_factor'])}")
    if row["ignore_dup_key"]:
        options.append("IGNORE_DUP_KEY = ON")
    if not row["allow_row_locks"]:
        options.append("ALLOW_ROW_LOCKS = OFF")
    if not row["allow_page_locks"]:
        options.append("ALLOW_PAGE_LOCKS = OFF")
    compression = str(row["data_compression_desc"] or "NONE")
    if compression != "NONE":
        options.append(f"DATA_COMPRESSION = {compression}")
    return options


def _missing_index_statement(backend: SqlServerBackend, row: dict[str, Any]) -> str:
    """Build the CREATE INDEX the DMV is asking for.

    The column lists arrive already bracket-quoted from the DMV, so they are
    used verbatim rather than re-quoted. Equality columns lead, inequality
    columns follow: that is the conventional key order and the one Microsoft's
    own missing-index template uses, and it is still a default rather than an
    answer — the DMV expresses no opinion about key order.
    """
    keys = ", ".join(
        str(part)
        for part in (row["equality_columns"], row["inequality_columns"])
        if part is not None
    )
    table = str(row["table_name"])
    name = _generated_index_name(table, keys)

    statement = (
        f"CREATE NONCLUSTERED INDEX {backend._quote(name)}"
        f" ON {backend._target(str(row['schema_name']), table)}"
        f" ({keys})"
    )
    if row["included_columns"]:
        statement += f" INCLUDE ({row['included_columns']})"
    return statement + ";"


def _generated_index_name(table: str, keys: str) -> str:
    """A placeholder name that is legal, readable and deterministic.

    Deterministic matters: the same suggestion produces the same name on every
    run, so two reports of the same database can be diffed.
    """
    columns = [column.strip().strip("[]") for column in keys.split(",")]
    raw = "_".join(["IX", table, *columns])
    safe = "".join(character if character.isalnum() else "_" for character in raw)
    return safe[:MAX_IDENTIFIER_LENGTH]


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _size_pretty(size_bytes: int) -> str:
    """Format a size the way pg_size_pretty does.

    Matched deliberately: the same report renders both engines, and a column
    that reads "5056 kB" for one and "4.94 MiB" for the other is a column that
    invites the reader to compare the wrong things.
    """
    for unit, size in (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("kB", 1024)):
        if size_bytes >= 10 * size:
            return f"{round(size_bytes / size)} {unit}"
    return f"{size_bytes} bytes"


def _as_utc(value: object) -> datetime | None:
    """Stamp UTC on a timestamp the query already converted.

    The conversion happens in SQL (see `_MISSING_INDEXES_SQL`) because the DMV
    column is the server's local time and the client may be anywhere. Aware
    rather than naive is not cosmetic — a naive datetime here compares
    TypeError-ily against every other timestamp in a report.
    """
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC)


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

#: LEFT JOIN, not JOIN. An index untouched since the service started has no row
#: in sys.dm_db_index_usage_stats at all, so an inner join drops exactly the
#: indexes this query is looking for. See the module docstring.
#:
#: The column lists are assembled with FOR XML PATH rather than STRING_AGG,
#: which would read better and would also make this query silently fail on
#: SQL Server 2016 and earlier. `.value('.', 'nvarchar(max)')` rather than a
#: bare FOR XML PATH('') because the latter entity-escapes a column name
#: containing & or <, and the generated DDL has to run.
_UNUSED_INDEXES_SQL = """
    SELECT
        sch.name                       AS schema_name,
        obj.name                       AS table_name,
        idx.name                       AS index_name,
        reads.scans                    AS scans,
        index_size.size_bytes          AS size_bytes,
        idx.type_desc                  AS type_desc,
        idx.is_unique                  AS is_unique,
        idx.is_unique_constraint       AS is_unique_constraint,
        idx.filter_definition          AS filter_definition,
        idx.fill_factor                AS fill_factor,
        idx.is_padded                  AS is_padded,
        idx.ignore_dup_key             AS ignore_dup_key,
        idx.allow_row_locks            AS allow_row_locks,
        idx.allow_page_locks           AS allow_page_locks,
        data_space.name                AS data_space_name,
        data_space.type                AS data_space_type,
        compression.data_compression_desc AS data_compression_desc,
        key_cols.columns               AS key_columns,
        included_cols.columns          AS included_columns,
        partition_col.column_name      AS partition_column
    FROM sys.indexes AS idx
    JOIN sys.objects AS obj
      ON obj.object_id = idx.object_id
    JOIN sys.schemas AS sch
      ON sch.schema_id = obj.schema_id
    JOIN sys.data_spaces AS data_space
      ON data_space.data_space_id = idx.data_space_id
    LEFT JOIN sys.dm_db_index_usage_stats AS usage_stats
      ON usage_stats.database_id = DB_ID()
     AND usage_stats.object_id   = idx.object_id
     AND usage_stats.index_id    = idx.index_id
    CROSS APPLY (
        SELECT COALESCE(usage_stats.user_seeks, 0)
             + COALESCE(usage_stats.user_scans, 0)
             + COALESCE(usage_stats.user_lookups, 0) AS scans
    ) AS reads
    CROSS APPLY (
        SELECT COALESCE(SUM(stats.used_page_count), 0) * 8192 AS size_bytes
        FROM sys.dm_db_partition_stats AS stats
        WHERE stats.object_id = idx.object_id
          AND stats.index_id  = idx.index_id
    ) AS index_size
    CROSS APPLY (
        SELECT TOP (1) part.data_compression_desc
        FROM sys.partitions AS part
        WHERE part.object_id = idx.object_id
          AND part.index_id  = idx.index_id
        ORDER BY part.partition_number
    ) AS compression
    CROSS APPLY (
        SELECT STUFF((
            SELECT ', ' + QUOTENAME(col.name)
                 + CASE WHEN ic.is_descending_key = 1 THEN ' DESC' ELSE ' ASC' END
            FROM sys.index_columns AS ic
            JOIN sys.columns AS col
              ON col.object_id = ic.object_id AND col.column_id = ic.column_id
            WHERE ic.object_id = idx.object_id
              AND ic.index_id  = idx.index_id
              AND ic.key_ordinal > 0
            ORDER BY ic.key_ordinal
            FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS columns
    ) AS key_cols
    CROSS APPLY (
        SELECT STUFF((
            SELECT ', ' + QUOTENAME(col.name)
            FROM sys.index_columns AS ic
            JOIN sys.columns AS col
              ON col.object_id = ic.object_id AND col.column_id = ic.column_id
            WHERE ic.object_id = idx.object_id
              AND ic.index_id  = idx.index_id
              AND ic.is_included_column = 1
            ORDER BY ic.index_column_id
            FOR XML PATH(''), TYPE).value('.', 'nvarchar(max)'), 1, 2, '') AS columns
    ) AS included_cols
    -- OUTER APPLY, not CROSS: a non-partitioned index has no row with
    -- partition_ordinal > 0, and CROSS APPLY would drop every such index from
    -- the result — which is to say, almost all of them.
    OUTER APPLY (
        SELECT TOP (1) QUOTENAME(col.name) AS column_name
        FROM sys.index_columns AS ic
        JOIN sys.columns AS col
          ON col.object_id = ic.object_id AND col.column_id = ic.column_id
        WHERE ic.object_id = idx.object_id
          AND ic.index_id  = idx.index_id
          AND ic.partition_ordinal > 0
        ORDER BY ic.partition_ordinal
    ) AS partition_col
    WHERE obj.type = 'U'
      AND obj.is_ms_shipped = 0
      -- Nonclustered B-trees only. The clustered index is the table, and the
      -- other index types need a CREATE statement this module cannot write.
      AND idx.type = 2
      -- An unused primary key is still a primary key.
      AND idx.is_primary_key = 0
      AND idx.is_disabled = 0
      AND idx.is_hypothetical = 0
      AND reads.scans <= ?
    ORDER BY index_size.size_bytes DESC, idx.name
"""

#: The three DMVs are separate because the optimiser records the *want*
#: (details) separately from how often it wanted it (group_stats), joined
#: through a group table that exists for a feature — multi-index groups — the
#: engine has never actually used: a group holds exactly one index today.
#:
#: The timestamp is converted to UTC in SQL for the same reason the server
#: start time is (see connection.py): the column is server-local, the client
#: is anywhere, and datetimeoffset is the one type pyodbc will not decode
#: without a registered output converter.
_MISSING_INDEXES_SQL = """
    SELECT
        sch.name                  AS schema_name,
        obj.name                  AS table_name,
        details.equality_columns  AS equality_columns,
        details.inequality_columns AS inequality_columns,
        details.included_columns  AS included_columns,
        group_stats.avg_total_user_cost
            * group_stats.avg_user_impact
            * (group_stats.user_seeks + group_stats.user_scans) AS impact_score,
        group_stats.user_seeks    AS seeks,
        group_stats.user_scans    AS scans,
        DATEADD(MINUTE, DATEDIFF(MINUTE, GETDATE(), GETUTCDATE()),
            CASE
                WHEN group_stats.last_user_seek IS NULL THEN group_stats.last_user_scan
                WHEN group_stats.last_user_scan IS NULL THEN group_stats.last_user_seek
                WHEN group_stats.last_user_seek > group_stats.last_user_scan
                    THEN group_stats.last_user_seek
                ELSE group_stats.last_user_scan
            END)                  AS last_seen_utc
    FROM sys.dm_db_missing_index_details AS details
    JOIN sys.dm_db_missing_index_groups AS groups
      ON groups.index_handle = details.index_handle
    JOIN sys.dm_db_missing_index_group_stats AS group_stats
      ON group_stats.group_handle = groups.index_group_handle
    JOIN sys.objects AS obj
      ON obj.object_id = details.object_id
    JOIN sys.schemas AS sch
      ON sch.schema_id = obj.schema_id
    WHERE details.database_id = DB_ID()
      AND group_stats.avg_total_user_cost
          * group_stats.avg_user_impact
          * (group_stats.user_seeks + group_stats.user_scans) >= ?
    ORDER BY impact_score DESC
"""

#: One row per index rather than per partition per allocation unit, which is
#: what the DMF returns. Fragmentation is averaged weighted by page count, so
#: a 40-partition index with one shredded partition is not reported as a
#: shredded index; page counts are summed, because the page floor is about the
#: cost of rebuilding the whole thing.
#:
#: IN_ROW_DATA only: LOB and row-overflow allocation units report NULL
#: fragmentation, and including them would drag the weighted average towards
#: nothing.
_FRAGMENTATION_SQL = """
    SELECT
        sch.name AS schema_name,
        obj.name AS table_name,
        idx.name AS index_name,
        SUM(physical.page_count) AS page_count,
        SUM(physical.avg_fragmentation_in_percent * physical.page_count)
            / NULLIF(SUM(physical.page_count), 0) AS fragmentation_pct,
        SUM(physical.avg_page_space_used_in_percent * physical.page_count)
            / NULLIF(SUM(physical.page_count), 0) AS page_density_pct
    FROM sys.dm_db_index_physical_stats(DB_ID(), NULL, NULL, NULL, '{mode}') AS physical
    JOIN sys.indexes AS idx
      ON idx.object_id = physical.object_id
     AND idx.index_id  = physical.index_id
    JOIN sys.objects AS obj
      ON obj.object_id = idx.object_id
    JOIN sys.schemas AS sch
      ON sch.schema_id = obj.schema_id
    WHERE obj.type = 'U'
      AND obj.is_ms_shipped = 0
      AND physical.alloc_unit_type_desc = 'IN_ROW_DATA'
      -- index_id 0 is a heap: no index to reorganize, and the remedy for a
      -- fragmented heap is a different operation entirely.
      AND physical.index_id > 0
      AND idx.type IN (1, 2)
    GROUP BY sch.name, obj.name, idx.name
    HAVING SUM(physical.page_count) >= ?
       AND SUM(physical.avg_fragmentation_in_percent * physical.page_count)
           / NULLIF(SUM(physical.page_count), 0) >= ?
    ORDER BY fragmentation_pct DESC
"""
