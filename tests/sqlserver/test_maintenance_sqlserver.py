"""SQL Server write-mode tests.

Same emphasis as `tests/test_maintenance.py`: the guards matter more than the
happy path, so most of what follows asserts that something is *refused*.

Two things are specific to this engine and are asserted rather than assumed.

* `read_only=True` is not enforced by SQL Server. There is no test here that a
  write is refused at the server, because there is no mechanism that would
  refuse it — `test_read_only_is_this_tools_guard_not_the_engines` pins the
  opposite, on purpose. A future reader who adds a server-side read-only
  assertion and watches it fail should find that test first.

* The seeded database is shared and read-only by contract: creating or
  dropping an index on `dbo.orders` discards that table's rows from
  `sys.dm_db_missing_index_details` for every test that runs afterwards. So
  anything here that mutates does it to a table it created itself, named for
  this module, and drops it again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyodbc
import pytest

from db_perf_toolkit import manifest
from db_perf_toolkit.backends.base import CheckUnavailable
from db_perf_toolkit.backends.sqlserver.capabilities import (
    ENGINE_EDITION_ENTERPRISE,
    ServerCapabilities,
)
from db_perf_toolkit.backends.sqlserver.connection import odbc_connection_string, parse_dsn
from db_perf_toolkit.backends.sqlserver.maintenance import (
    OLA_HALLENGREN_URL,
    MaintenanceOperations,
)
from db_perf_toolkit.models import Operation, UnusedIndex

#: Tables and indexes this module creates. Prefixed so that anything left
#: behind by an interrupted run is obviously ours.
_TABLE = "dbperf_maint_probe"
_INDEX = "ix_dbperf_maint_probe_value"


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _unused_index(**overrides: Any) -> UnusedIndex:
    """A droppable index, before any override makes it undroppable.

    Built by hand rather than read from the server so that each refusal can be
    provoked in isolation; `tests/sqlserver/test_indexes_sqlserver.py` is where
    the shape of a real one is checked.
    """
    index = UnusedIndex(
        schema="dbo",
        table="orders",
        index="ix_orders_status",
        scans=0,
        size_bytes=64 * 1024 * 1024,
        size_pretty="64 MB",
        definition="CREATE NONCLUSTERED INDEX [ix_orders_status] ON [dbo].[orders] ([status])",
        is_unique=False,
        enforces_constraint=False,
    )
    return replace(index, **overrides)


def _capabilities(**overrides: Any) -> ServerCapabilities:
    """Detection results, stubbed.

    IndexOptimize cannot be installed in the fixture container — it is
    Ola's script, fetched from his site, and this tool deliberately does not
    carry a copy. So the presence path is exercised against stubbed detection
    and the *absence* path against the real server, which is genuinely without
    it. What the real server still proves about the generated SQL is
    `test_generated_exec_parses_on_the_server`.
    """
    capabilities = ServerCapabilities(
        engine_edition=ENGINE_EDITION_ENTERPRISE,
        edition="Developer Edition (64-bit)",
        product_version="16.0.4215.2",
        product_level="RTM",
        database="dbperf",
        major_version=16,
        query_store_state="READ_WRITE",
        query_store_enabled=True,
        index_optimize_database="master",
        command_log_database="master",
        ola_schema="dbo",
    )
    return replace(capabilities, **overrides)


@pytest.fixture
def backend_factory(
    sqlserver_instance: Any,
    mssql_connect: Callable[..., Any],
) -> Callable[..., MaintenanceOperations]:
    """Build a maintenance backend over a connection the fixture will close."""

    def _build(*, read_only: bool = True) -> MaintenanceOperations:
        return MaintenanceOperations(
            mssql_connect(),
            database=sqlserver_instance.database,
            host=sqlserver_instance.host,
            read_only=read_only,
        )

    return _build


@pytest.fixture
def probe_table(mssql_connect: Callable[..., Any]) -> Iterator[Callable[[], None]]:
    """Our own table with one nonclustered index on it, dropped afterwards.

    Never `dbo.orders`: index DDL against the seeded table wipes its rows out
    of the missing-index DMV for the rest of the session.
    """
    conn = mssql_connect()
    cursor = conn.cursor()

    def create() -> None:
        cursor.execute(f"DROP TABLE IF EXISTS dbo.[{_TABLE}]")
        cursor.execute(f"CREATE TABLE dbo.[{_TABLE}] (id int NOT NULL, value int NOT NULL)")
        cursor.execute(
            f"INSERT INTO dbo.[{_TABLE}] (id, value) "
            "SELECT TOP (500) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)), 1 "
            "FROM sys.all_objects"
        )
        cursor.execute(f"CREATE NONCLUSTERED INDEX [{_INDEX}] ON dbo.[{_TABLE}] (value)")

    create()
    yield create
    cursor.execute(f"DROP TABLE IF EXISTS dbo.[{_TABLE}]")


def _index_exists(query: Callable[..., list[dict[str, Any]]], name: str) -> bool:
    return bool(
        query(
            "SELECT 1 AS present FROM sys.indexes "
            "WHERE name = ? AND object_id = OBJECT_ID('dbo.' + ?)",
            name,
            _TABLE,
        )
    )


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_read_only_backend_refuses_to_execute(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """The default connection must not be able to run maintenance at all."""
    backend = backend_factory()
    plan = backend.plan_drop_unused_indexes([_unused_index()], min_size_bytes=0)

    assert plan.operations, "expected the sample index to be droppable"
    with pytest.raises(RuntimeError, match="read-only"):
        backend.execute(plan.operations)


def test_plan_refuses_unique_and_constraint_backed_indexes(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """The same categories as PostgreSQL, from the same shared guard."""
    backend = backend_factory()
    candidates = [
        _unused_index(index="ix_unique", is_unique=True),
        _unused_index(index="ix_constraint", enforces_constraint=True),
        _unused_index(index="ix_used", scans=17),
        _unused_index(index="ix_droppable"),
    ]
    plan = backend.plan_drop_unused_indexes(candidates, min_size_bytes=0)

    def name_of(target: str) -> str:
        return target.rsplit(".", 1)[-1].strip("[]")

    planned = {name_of(op.target) for op in plan.operations}
    refused = {name_of(target): reason for target, reason in plan.refused.items()}

    assert "ix_unique" not in planned
    assert "unique" in refused["ix_unique"]

    assert "ix_constraint" not in planned
    assert "constraint" in refused["ix_constraint"]

    assert "ix_used" not in planned
    assert "17 recorded scans" in refused["ix_used"]

    assert planned == {"ix_droppable"}


def test_size_floor_excludes_trivial_indexes(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """Ola's MinNumberOfPages logic: maintenance on a tiny object is noise."""
    backend = backend_factory()
    candidates = [_unused_index(size_bytes=16 * 1024, size_pretty="16 KB")]

    generous = backend.plan_drop_unused_indexes(candidates, min_size_bytes=0)
    strict = backend.plan_drop_unused_indexes(candidates)

    assert generous.operations
    assert not strict.operations
    assert any("floor" in reason for reason in strict.refused.values())


def test_plan_refuses_an_index_it_could_not_recreate(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """A drop that cannot be undone is a deletion, so it is not planned.

    PostgreSQL cannot reach this: pg_get_indexdef always returns a statement.
    A SQL Server definition is reassembled from sys.index_columns and can come
    back empty, and the manifest is only worth trusting if nothing writes an
    unusable entry into it.
    """
    backend = backend_factory()
    plan = backend.plan_drop_unused_indexes(
        [_unused_index(definition=""), _unused_index(index="ix_columns", definition="(status)")],
        min_size_bytes=0,
    )

    assert not plan.operations
    assert all("could not be undone" in reason for reason in plan.refused.values())


def test_index_maintenance_refused_without_ola_installed(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """The fixture container genuinely has no IndexOptimize, which is the point.

    The remedy has to send the user to Ola's site and has to say that this
    tool will not install it for them — a maintenance tool that quietly
    creates procedures in master is a worse outcome than a refusal.
    """
    backend = backend_factory()
    assert backend.capabilities.has_index_optimize is False

    with pytest.raises(CheckUnavailable) as raised:
        backend.plan_index_maintenance()

    message = raised.value.full_message()
    assert OLA_HALLENGREN_URL in message
    assert "does not install it" in message


def test_postgresql_shaped_verbs_say_what_to_use_instead(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """`vacuum` and `reindex` are refused with a reason, not a shrug.

    The backend declares `supports_maintenance = True`, so the base class's
    "maintenance is not implemented in this backend" would be actively wrong
    for these two.
    """
    backend = backend_factory()

    with pytest.raises(CheckUnavailable) as vacuum:
        backend.plan_vacuum([])
    assert "no VACUUM" in vacuum.value.full_message()

    with pytest.raises(CheckUnavailable) as reindex:
        backend.plan_reindex([])
    assert OLA_HALLENGREN_URL in reindex.value.full_message()


# ----------------------------------------------------------------------
# The generated EXEC
# ----------------------------------------------------------------------


def test_index_maintenance_qualifies_the_procedure_by_database(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """Ola's default install is in master, not in the database under diagnosis.

    An unqualified `EXEC dbo.IndexOptimize` would fail on the majority of
    installations, so the database detection reported is what the EXEC names,
    and @Databases carries the database to actually work on.
    """
    backend = backend_factory()
    backend._capabilities = _capabilities()

    plan = backend.plan_index_maintenance()
    statement = plan.operations[0].sql

    assert "EXEC [master].[dbo].[IndexOptimize]" in statement
    assert "@Databases = 'dbperf'" in statement
    assert "@FragmentationLevel1 = 5" in statement
    assert "@FragmentationLevel2 = 30" in statement
    assert "@MinNumberOfPages = 1000" in statement
    assert "@LogToTable = 'Y'" in statement
    assert "@Execute = 'N'" in statement
    assert plan.operations[0].target == "[master].[dbo].[IndexOptimize]"


def test_index_maintenance_uses_a_local_install_when_there_is_one(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """A deliberate local install wins over master, schema included.

    The installation script can be edited, so the schema is whatever detection
    found rather than a hard-coded `dbo`.
    """
    backend = backend_factory()
    backend._capabilities = _capabilities(
        index_optimize_database="dbperf", command_log_database="dbperf", ola_schema="maint"
    )

    statement = backend.plan_index_maintenance().operations[0].sql

    assert "EXEC [dbperf].[maint].[IndexOptimize]" in statement
    assert "[master]" not in statement


def test_index_maintenance_execute_flag(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """`--execute` reaches IndexOptimize's own @Execute, and only then."""
    backend = backend_factory()
    backend._capabilities = _capabilities()

    assert "@Execute = 'N'" in backend.plan_index_maintenance().operations[0].sql
    assert "@Execute = 'Y'" in backend.plan_index_maintenance(execute=True).operations[0].sql


def test_log_to_table_is_gated_on_commandlog_existing(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """@LogToTable='Y' without CommandLog is an error out of IndexOptimize."""
    backend = backend_factory()
    backend._capabilities = _capabilities(command_log_database=None)

    plan = backend.plan_index_maintenance()

    assert "@LogToTable = 'N'" in plan.operations[0].sql
    assert "CommandLog" in plan.refused["@LogToTable"]


def test_online_rebuild_is_gated_on_the_edition(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """ONLINE = ON is Enterprise-only, and is decided before the run, not during it.

    On an edition without it the answer is REORGANIZE — online everywhere —
    rather than an offline rebuild holding a schema-modification lock for its
    duration. Ola's own defaults fall back to INDEX_REBUILD_OFFLINE; that is
    right for a maintenance window and wrong for a tool pointed at production.
    """
    backend = backend_factory()

    backend._capabilities = _capabilities()
    enterprise = backend.plan_index_maintenance()
    assert (
        "@FragmentationHigh = 'INDEX_REBUILD_ONLINE,INDEX_REORGANIZE'"
        in enterprise.operations[0].sql
    )
    assert "*" not in enterprise.refused

    backend._capabilities = _capabilities(engine_edition=2, edition="Standard Edition (64-bit)")
    standard = backend.plan_index_maintenance()
    assert "@FragmentationHigh = 'INDEX_REORGANIZE'" in standard.operations[0].sql
    assert "INDEX_REBUILD_ONLINE" not in standard.operations[0].sql
    assert "online index rebuild" in standard.refused["*"]


def test_generated_exec_parses_on_the_server(
    backend_factory: Callable[..., MaintenanceOperations],
) -> None:
    """The EXEC is real T-SQL, checked by the engine rather than by eye.

    IndexOptimize is not installed, so the statement cannot run — but SQL
    Server parses a batch before it resolves object names, so reaching
    "Could not find stored procedure" is proof the syntax and the
    three-part name are well formed. This also exercises execute()'s error
    path: the failure is returned, not raised.
    """
    backend = backend_factory(read_only=False)
    backend._capabilities = _capabilities()
    plan = backend.plan_index_maintenance()

    results = backend.execute(plan.operations)

    (_, error) = results[0]
    assert error is not None
    assert "Could not find stored procedure" in error


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def test_read_only_is_this_tools_guard_not_the_engines(
    sqlserver_instance: Any,
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> None:
    """SQL Server does not enforce this backend's read_only flag. Deliberately asserted.

    PostgreSQL sets `default_transaction_read_only`, so a write through a
    read-only backend is refused by the server. The SQL Server connection
    string's closest equivalent, ApplicationIntent=ReadOnly, is a routing hint
    for availability groups; against a standalone instance it is accepted and
    ignored. A write through it succeeds, which is what this asserts.

    Do not "fix" this into an assertion that the write is refused. It would
    fail, correctly. The consequence is that `execute()`'s read_only check is
    the only barrier this tool has, which is why it is tested on its own above.
    """
    target = parse_dsn(sqlserver_instance.url())
    conn = pyodbc.connect(odbc_connection_string(target, read_only=True), autocommit=True)
    table = f"{_TABLE}_readonly_probe"
    try:
        # read_only=False here is the whole demonstration: the flag lives in
        # this process, and the connection underneath it was opened exactly as
        # a read-only one would be.
        backend = MaintenanceOperations(conn, database=sqlserver_instance.database, read_only=False)
        results = backend.execute(
            [
                Operation(
                    target=f"[dbo].[{table}]",
                    description="write through a ReadOnly-intent connection",
                    sql=f"CREATE TABLE dbo.[{table}] (id int NOT NULL)",
                    destructive=False,
                )
            ]
        )
        assert results[0][1] is None, "ApplicationIntent=ReadOnly is not enforcement"
        assert mssql_query("SELECT 1 AS present FROM sys.tables WHERE name = ?", table), (
            "the write reached the database"
        )
    finally:
        with pyodbc.connect(odbc_connection_string(target), autocommit=True) as cleanup:
            cleanup.cursor().execute(f"DROP TABLE IF EXISTS dbo.[{table}]")
        conn.close()


def test_one_failure_does_not_abort_the_rest(
    backend_factory: Callable[..., MaintenanceOperations],
    probe_table: Callable[[], None],
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> None:
    """A partially applied run is normal; an aborted one hides what landed."""
    backend = backend_factory(read_only=False)
    operations = [
        Operation(
            target="[dbo].[no_such_table]",
            description="fails",
            sql="DROP INDEX [nope] ON dbo.[dbperf_no_such_table];",
            destructive=True,
        ),
        Operation(
            target=f"[dbo].[{_TABLE}].[{_INDEX}]",
            description="succeeds",
            sql=f"DROP INDEX [{_INDEX}] ON dbo.[{_TABLE}];",
            destructive=True,
        ),
    ]

    results = backend.execute(operations)

    assert results[0][1] is not None
    assert results[1][1] is None
    assert not _index_exists(mssql_query, _INDEX)


def test_drop_then_restore_round_trip(
    backend_factory: Callable[..., MaintenanceOperations],
    probe_table: Callable[[], None],
    mssql_query: Callable[..., list[dict[str, Any]]],
    tmp_path: Path,
) -> None:
    """A drop must be undoable from its manifest alone, exactly as on PostgreSQL.

    `restore-indexes` reads the manifest and nothing else, so the CREATE
    statement recorded has to be runnable T-SQL against the same server — not
    a description of the index, and not PostgreSQL's spelling of one.
    """
    assert _index_exists(mssql_query, _INDEX)

    planner = backend_factory()
    index = _unused_index(
        table=_TABLE,
        index=_INDEX,
        definition=f"CREATE NONCLUSTERED INDEX [{_INDEX}] ON [dbo].[{_TABLE}] ([value])",
    )
    plan = planner.plan_drop_unused_indexes([index], min_size_bytes=0)

    assert len(plan.operations) == 1
    operation = plan.operations[0]
    assert operation.destructive is True
    assert operation.target == f"[dbo].[{_TABLE}].[{_INDEX}]"
    assert operation.sql == f"DROP INDEX [{_INDEX}] ON [dbo].[{_TABLE}];"

    path = manifest.write(plan, tmp_path / "rollback.json")

    writer = backend_factory(read_only=False)
    assert all(error is None for _, error in writer.execute(plan.operations))
    assert not _index_exists(mssql_query, _INDEX)

    restored = manifest.rollback_statements(manifest.read(path))
    assert len(restored) == 1
    results = writer.execute(
        [
            Operation(target=target, description="restore", sql=sql, destructive=False)
            for target, sql in restored
        ]
    )
    assert all(error is None for _, error in results)
    assert _index_exists(mssql_query, _INDEX)


def test_bracket_quoting_survives_an_awkward_name(
    backend_factory: Callable[..., MaintenanceOperations],
    mssql_connect: Callable[..., Any],
    mssql_query: Callable[..., list[dict[str, Any]]],
) -> None:
    """A `]` in an identifier must double, or the generated DDL hits the wrong object.

    Checked against the server rather than against a string, because the
    question is whether SQL Server accepts what was generated.
    """
    table = "dbperf]maint]odd"
    index = "ix]odd"
    cursor = mssql_connect().cursor()
    quoted_table = "[" + table.replace("]", "]]") + "]"
    cursor.execute(f"DROP TABLE IF EXISTS dbo.{quoted_table}")
    cursor.execute(f"CREATE TABLE dbo.{quoted_table} (value int NOT NULL)")
    cursor.execute(
        f"CREATE NONCLUSTERED INDEX [{index.replace(']', ']]')}] ON dbo.{quoted_table} (value)"
    )
    try:
        planner = backend_factory()
        plan = planner.plan_drop_unused_indexes(
            [
                _unused_index(
                    table=table,
                    index=index,
                    definition=(
                        f"CREATE NONCLUSTERED INDEX [{index.replace(']', ']]')}] "
                        f"ON [dbo].{quoted_table} ([value])"
                    ),
                )
            ],
            min_size_bytes=0,
        )

        assert plan.operations[0].target == f"[dbo].{quoted_table}.[ix]]odd]"

        writer = backend_factory(read_only=False)
        assert all(error is None for _, error in writer.execute(plan.operations))
        assert not mssql_query(
            "SELECT 1 AS present FROM sys.indexes WHERE name = ? AND object_id = OBJECT_ID(?)",
            index,
            f"dbo.{quoted_table}",
        )
    finally:
        cursor.execute(f"DROP TABLE IF EXISTS dbo.{quoted_table}")
