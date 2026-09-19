"""What this particular SQL Server can be asked.

On PostgreSQL a feature check is usually one question — is the extension
installed? SQL Server has no single answer of that shape. The same query can
be unavailable because the edition is Azure SQL Database, because the version
predates the DMV, because Query Store was never switched on for this database,
or because someone installed Ola Hallengren's procedures in master instead of
here. Each check would otherwise rediscover that itself, four times over and
slightly differently.

So detection happens once per connection and the checks branch on the result.
The detection queries are deliberately forgiving: a probe that fails is
evidence the feature is absent, not a reason to abort the run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyodbc

#: SERVERPROPERTY('EngineEdition'). Only the values worth branching on are
#: named; the rest arrive as integers and are compared numerically.
ENGINE_EDITION_ENTERPRISE = 3
ENGINE_EDITION_AZURE_SQL_DATABASE = 5
ENGINE_EDITION_AZURE_SYNAPSE = 6
ENGINE_EDITION_MANAGED_INSTANCE = 8

#: Query Store arrived in SQL Server 2016. The version test below is not the
#: whole story — see `ServerCapabilities.major_version`.
QUERY_STORE_MIN_MAJOR = 13

#: Online index rebuilds are an Enterprise feature on the box product, and are
#: always available on the Azure platforms. Developer edition reports
#: EngineEdition 3 as well, which is correct: it has the Enterprise feature set.
_ONLINE_REBUILD_EDITIONS = frozenset(
    {
        ENGINE_EDITION_ENTERPRISE,
        ENGINE_EDITION_AZURE_SQL_DATABASE,
        ENGINE_EDITION_MANAGED_INSTANCE,
    }
)

#: sys.database_query_store_options.actual_state: 0 OFF, 1 READ_ONLY,
#: 2 READ_WRITE, 3 ERROR. READ_ONLY still has history to read, so it counts as
#: usable — the check reads the captured statistics, it does not write them.
_QUERY_STORE_READABLE = frozenset({1, 2})

#: Ola Hallengren's object names, and what they must be. Matching on the name
#: alone would accept somebody's unrelated table called CommandLog.
_INDEX_OPTIMIZE = ("IndexOptimize", "P")
_COMMAND_LOG = ("CommandLog", "U")

#: A callable that runs one SELECT and returns rows keyed by column name. The
#: backend passes its own `_query`; taking the callable rather than a
#: connection keeps this module free of connection handling and testable with
#: a stub.
Query = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ServerCapabilities:
    """Everything the checks need to know about the target before they run."""

    engine_edition: int
    edition: str
    product_version: str
    product_level: str | None
    database: str
    """Major version from SERVERPROPERTY('ProductVersion').

    Do not gate a feature on this without checking the edition first. Azure SQL
    Database reports 12.0.x forever, regardless of the engine actually running,
    which is ahead of every boxed release — so a plain `major >= 13` test
    concludes that a server with Query Store enabled by default does not
    support Query Store.
    """
    major_version: int
    """None when sys.database_query_store_options could not be read at all,
    which means the view does not exist (pre-2016) or is not readable here."""
    query_store_state: str | None
    query_store_enabled: bool
    """Database each of Ola Hallengren's objects was found in, or None.

    Not a bool, because the default installation puts them in master rather
    than in the database being diagnosed, and an EXEC has to name the right
    one. Cross-database lookup is itself unavailable on Azure SQL Database, so
    None there means "not visible from here" rather than "not installed".
    """
    index_optimize_database: str | None
    command_log_database: str | None
    """Schema the procedures were found in. Usually dbo, but the installation
    script can be edited, and the generated EXEC must match."""
    ola_schema: str | None

    @property
    def is_azure_sql_database(self) -> bool:
        return self.engine_edition == ENGINE_EDITION_AZURE_SQL_DATABASE

    @property
    def is_managed_instance(self) -> bool:
        return self.engine_edition == ENGINE_EDITION_MANAGED_INSTANCE

    @property
    def is_azure(self) -> bool:
        return self.engine_edition in {
            ENGINE_EDITION_AZURE_SQL_DATABASE,
            ENGINE_EDITION_AZURE_SYNAPSE,
            ENGINE_EDITION_MANAGED_INSTANCE,
        }

    @property
    def has_index_optimize(self) -> bool:
        return self.index_optimize_database is not None

    @property
    def has_command_log(self) -> bool:
        return self.command_log_database is not None

    @property
    def supports_online_rebuild(self) -> bool:
        return self.engine_edition in _ONLINE_REBUILD_EDITIONS

    def describe(self) -> str:
        """One line naming the server, for the note above every report.

        @@VERSION is the obvious source and the wrong one: it is a four-line
        banner including the build date and the host OS, which a single-line
        header cannot carry. Edition and build number are what actually change
        the answers below.
        """
        level = f" {self.product_level}" if self.product_level else ""
        return f"Microsoft SQL Server {self.product_version}{level} — {self.edition}"


def detect_capabilities(query: Query) -> ServerCapabilities:
    """Ask the server what it is and what it has.

    Four short queries rather than one, because three of them are allowed to
    fail: only the SERVERPROPERTY call is available everywhere and needs no
    privilege beyond connecting.
    """
    identity = query(_IDENTITY_SQL)[0]

    engine_edition = int(identity["engine_edition"])
    product_version = str(identity["product_version"])
    major_version = _major_version(product_version)
    is_azure = engine_edition in {
        ENGINE_EDITION_AZURE_SQL_DATABASE,
        ENGINE_EDITION_AZURE_SYNAPSE,
        ENGINE_EDITION_MANAGED_INSTANCE,
    }

    state, enabled = _query_store(query, major_version=major_version, is_azure=is_azure)
    ola = _ola_hallengren(query, current_database=str(identity["database_name"]), is_azure=is_azure)

    return ServerCapabilities(
        engine_edition=engine_edition,
        edition=str(identity["edition"]),
        product_version=product_version,
        product_level=_opt_str(identity["product_level"]),
        database=str(identity["database_name"]),
        major_version=major_version,
        query_store_state=state,
        query_store_enabled=enabled,
        index_optimize_database=ola.index_optimize_database,
        command_log_database=ola.command_log_database,
        ola_schema=ola.schema,
    )


#: SERVERPROPERTY returns sql_variant, which pyodbc surfaces as a string for
#: EngineEdition unless it is cast, so every column is cast explicitly.
_IDENTITY_SQL = """
    SELECT
        CAST(SERVERPROPERTY('EngineEdition')   AS int)            AS engine_edition,
        CAST(SERVERPROPERTY('Edition')         AS nvarchar(256))  AS edition,
        CAST(SERVERPROPERTY('ProductVersion')  AS nvarchar(64))   AS product_version,
        CAST(SERVERPROPERTY('ProductLevel')    AS nvarchar(64))   AS product_level,
        DB_NAME()                                                 AS database_name
"""

_QUERY_STORE_SQL = """
    SELECT actual_state, actual_state_desc
    FROM sys.database_query_store_options
"""

#: Joined to sys.schemas because the EXEC has to name the schema, and matched
#: on object type so an unrelated table named CommandLog is not mistaken for
#: the logging table the maintenance path writes to.
_OLA_SQL = """
    SELECT s.name AS schema_name, o.name AS object_name, o.type AS object_type
    FROM {database}sys.objects AS o
    JOIN {database}sys.schemas AS s ON s.schema_id = o.schema_id
    WHERE o.name IN ('IndexOptimize', 'CommandLog')
"""


@dataclass(frozen=True, slots=True)
class _Ola:
    index_optimize_database: str | None
    command_log_database: str | None
    schema: str | None


def _query_store(query: Query, *, major_version: int, is_azure: bool) -> tuple[str | None, bool]:
    """Read Query Store's state for the current database.

    The version guard is an optimisation, not the safety net: the try/except
    is, because the view is also absent on Azure Synapse and unreadable
    without VIEW DATABASE STATE. Anything that fails here is reported as
    "no Query Store", which is the conservative answer — the caller then falls
    back to sys.dm_exec_query_stats, whose window is the plan cache rather
    than a retention policy.
    """
    if major_version < QUERY_STORE_MIN_MAJOR and not is_azure:
        return None, False

    try:
        rows = query(_QUERY_STORE_SQL)
    except pyodbc.Error:
        return None, False

    if not rows:
        return None, False
    return str(rows[0]["actual_state_desc"]), int(rows[0]["actual_state"]) in _QUERY_STORE_READABLE


def _ola_hallengren(query: Query, *, current_database: str, is_azure: bool) -> _Ola:
    """Locate IndexOptimize and CommandLog, here or in master.

    The default installation puts both in master, so looking only in the
    database under diagnosis would report "not installed" on the majority of
    servers that do have it. The current database is checked first because a
    local copy is the one a deliberate installation chose.

    Azure SQL Database is skipped for master entirely: cross-database queries
    are not supported there, so the probe would fail rather than return
    nothing, and Ola's solution ships a separate Azure script for that reason.
    """
    found = _ola_objects(query, database=None, label=current_database)
    if found.index_optimize_database and found.command_log_database:
        return found

    if is_azure:
        return found

    in_master = _ola_objects(query, database="master", label="master")
    return _Ola(
        index_optimize_database=found.index_optimize_database or in_master.index_optimize_database,
        command_log_database=found.command_log_database or in_master.command_log_database,
        schema=found.schema or in_master.schema,
    )


def _ola_objects(query: Query, *, database: str | None, label: str) -> _Ola:
    prefix = f"{database}." if database else ""
    try:
        rows = query(_OLA_SQL.format(database=prefix))
    except pyodbc.Error:
        # A denied or impossible cross-database read is indistinguishable from
        # an empty one for our purposes: either way we cannot EXEC it.
        return _Ola(None, None, None)

    index_optimize: str | None = None
    command_log: str | None = None
    schema: str | None = None
    for row in rows:
        name, object_type = str(row["object_name"]), str(row["object_type"]).strip()
        if (name, object_type) == _INDEX_OPTIMIZE:
            index_optimize, schema = label, str(row["schema_name"])
        elif (name, object_type) == _COMMAND_LOG:
            command_log = label
            schema = schema or str(row["schema_name"])
    return _Ola(index_optimize, command_log, schema)


def _major_version(product_version: str) -> int:
    major, _, _ = product_version.partition(".")
    return int(major) if major.isdigit() else 0


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
