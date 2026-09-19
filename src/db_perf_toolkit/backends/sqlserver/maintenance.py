"""SQL Server maintenance: orchestration, not reimplementation.

Three things here differ from the PostgreSQL backend in ways a reader must not
have to infer.

1. INDEX MAINTENANCE IS OLA HALLENGREN'S, AND STAYS HIS.

   `IndexOptimize` from the `SQL Server Maintenance Solution
   <https://ola.hallengren.com>`_ (MIT) is the de-facto standard for SQL
   Server index maintenance, with fifteen years of production hardening behind
   its edge cases — LOB columns that cannot be rebuilt online, partitioned
   indexes, statistics updates, resumable operations. This tool drives it
   through the same plan / dry-run / execute pipeline as everything else and
   reimplements none of it. It does not vendor the procedures and does not
   install them: they are installed and updated by the user through his
   channels, and if they are absent the plan is refused with the URL.

   His default installation puts them in `master`, not in the database being
   diagnosed, so every generated `EXEC` is three-part qualified from
   `capabilities.index_optimize_database` rather than assuming `dbo.` in the
   current database. `@Databases` then names the database to work on, which is
   how his solution has always expected to be called.

2. `read_only=True` IS THIS TOOL'S SEATBELT. THE ENGINE IS NOT HOLDING IT.

   PostgreSQL opens its diagnostic session with
   `default_transaction_read_only = on`, so `execute()`'s refusal is a second
   line of defence behind one the server enforces. **SQL Server has no
   session-level equivalent**, and this is not a theoretical gap: a
   `CREATE TABLE` issued through a `read_only=True` connection of this
   backend's own making succeeds. `test_read_only_is_this_tools_guard_not_the_engines`
   asserts exactly that, because it is the fact most likely to be assumed away.

   So on SQL Server the check at the top of `execute()` is *the* thing
   stopping this process writing. Not a backstop — the wall. Code added below
   that reaches `self._conn` without passing it has removed the guarantee, and
   nothing in the engine will object. The real enforcement available on this
   platform is the login's permissions; `connection.py` says what to grant.

3. THERE IS NO `CONCURRENTLY`.

   PostgreSQL's maintenance path leans on `DROP INDEX CONCURRENTLY` and
   `REINDEX CONCURRENTLY` throughout, measured at a 0.6ms worst-case stall
   while indexes were dropped under a live commit load. SQL Server offers no
   equivalent for a drop, and its nearest analogue for a rebuild is
   edition-gated:

   * `DROP INDEX` takes a schema-modification (Sch-M) lock on the table. Sch-M
     conflicts with *everything*, including plain `SELECT`s. For a nonclustered
     index the work under that lock is metadata plus deallocation and is
     usually milliseconds, but "short" is not "concurrent", and on a table with
     a long-running reader the drop waits and everything arriving behind it
     waits too. `WITH (ONLINE = ON)` is not the escape hatch it looks like, and
     this was checked rather than assumed: against SQL Server 2022 Developer —
     which has the full Enterprise feature set — `DROP INDEX ... WITH (ONLINE =
     ON)` on a nonclustered index fails with error 3745, "Only a clustered
     index can be dropped online", and adding `WAIT_AT_LOW_PRIORITY` fails the
     same way because that option requires `ONLINE = ON`. The same statement
     against a *clustered* index succeeds, because dropping one is really a
     heap rebuild. So for the indexes this tool drops there is no online form
     on any edition. The connection's `SET LOCK_TIMEOUT` bounds how long we
     queue for the Sch-M lock, never how long we hold it, and that is the
     whole of the mitigation available.
   * `ALTER INDEX ... REBUILD WITH (ONLINE = ON)` is Enterprise, Developer,
     Azure SQL Database and Managed Instance only — `capabilities.supports_online_rebuild`.
     On Standard it is a runtime error, not a silent downgrade.
   * `ALTER INDEX ... REORGANIZE` is online on every edition. It is the only
     always-available non-blocking index operation SQL Server has.

   That shapes what this module asks IndexOptimize to do: see
   `_fragmentation_actions`. Where online rebuild is unavailable the answer is
   REORGANIZE rather than an offline rebuild, because an offline rebuild holds
   that Sch-M lock for the whole rebuild and this tool will not schedule that
   on someone's behalf. Ola's own defaults fall back to
   `INDEX_REBUILD_OFFLINE`; that is a reasonable default for a solution being
   run in a maintenance window, and the wrong one for a diagnostic tool being
   pointed at production mid-afternoon.
"""

from __future__ import annotations

import pyodbc

from db_perf_toolkit.backends.base import CheckUnavailable
from db_perf_toolkit.backends.sqlserver.connection import SqlServerBackend
from db_perf_toolkit.models import BloatedTable, Operation, Plan, UnusedIndex
from db_perf_toolkit.safety import index_drop_refusal
from db_perf_toolkit.thresholds import INDEX_OPTIMIZE_LEVEL_1, INDEX_OPTIMIZE_LEVEL_2, MIN_PAGES

#: Ola Hallengren's own defaults, kept rather than re-derived. Below
#: @FragmentationLevel1 an index is left alone; between the two it is
#: reorganized; above @FragmentationLevel2 it is rebuilt.

#: @MinNumberOfPages: maintenance on an object smaller than this costs more
#: than it returns. His default, and the origin of the PostgreSQL backend's
#: 8MB floor — 1000 pages is 8MB on both engines, which is coincidence in
#: derivation and convenience in practice.

#: The drop floor, stated in bytes because that is what UnusedIndex carries.
#: Same reasoning, same number as the PostgreSQL backend: reclaiming 16KB is
#: not worth a schema change.
DEFAULT_MIN_INDEX_BYTES = 8 * 1024 * 1024

#: Where Ola's solution lives, for every message that has to mention it.
OLA_HALLENGREN_URL = "https://ola.hallengren.com"

#: Ola's @FragmentationHigh / @FragmentationMedium take an ordered list of
#: actions and use the first one applicable to each index. Neither list here
#: ends in INDEX_REBUILD_OFFLINE — see the module docstring, point 3.
_ACTIONS_ONLINE_REBUILD = "INDEX_REBUILD_ONLINE,INDEX_REORGANIZE"
_ACTIONS_REORGANIZE_ONLY = "INDEX_REORGANIZE"

#: Plan.refused is keyed by target; "*" is the key for something that applies
#: to the plan as a whole rather than to one object. Matches the PostgreSQL
#: backend's use of it in plan_reindex.
_WHOLE_PLAN = "*"


class MaintenanceOperations(SqlServerBackend):
    """Maintenance planning and execution for SQL Server.

    Planning never touches the server, exactly as on PostgreSQL: `--script`,
    `--dry-run` and `--execute` consume the same `Operation` objects, so the
    SQL that is printed is the SQL that would run.

    A `SqlServerBackend` subclass declaring only what this module provides, to
    be composed with the check mixins elsewhere — the same arrangement as
    `IndexChecks` and `QueryChecks`. It adds no `Check`: maintenance is not a
    diagnostic, so `supports` stays whatever the check mixins union to, and
    `supports_maintenance` is what this one claims.
    """

    supports_maintenance = True

    # ------------------------------------------------------------------
    # The two PostgreSQL verbs, answered rather than left to the base class
    #
    # `supports_maintenance = True` makes the base class's "maintenance is not
    # implemented in this backend" the wrong sentence for these: maintenance
    # is implemented, and these two specifically are not how SQL Server does
    # it. Saying which one to use instead costs four lines.
    # ------------------------------------------------------------------

    def plan_vacuum(self, tables: list[BloatedTable], *, analyze: bool = True) -> Plan:
        raise CheckUnavailable(
            "vacuum",
            "SQL Server has no VACUUM. It has no MVCC dead tuples to reclaim: "
            "row versions live in tempdb's version store and are cleaned up by "
            "a background task.",
            "The nearest thing to bloat here is index fragmentation — run "
            "`fragmentation`, then index maintenance.",
        )

    def plan_reindex(self, indexes: list[UnusedIndex]) -> Plan:
        raise CheckUnavailable(
            "reindex",
            "SQL Server index rebuilds are not reimplemented here.",
            "Use index maintenance, which drives Ola Hallengren's IndexOptimize: "
            "it chooses reorganize or rebuild per index, handles the cases a "
            "one-line ALTER INDEX does not, and is the de-facto standard.\n"
            f"  {OLA_HALLENGREN_URL}",
        )

    # ------------------------------------------------------------------
    # Index maintenance, via IndexOptimize
    # ------------------------------------------------------------------

    def plan_index_maintenance(
        self,
        *,
        databases: str | None = None,
        fragmentation_level_1: int = INDEX_OPTIMIZE_LEVEL_1,
        fragmentation_level_2: int = INDEX_OPTIMIZE_LEVEL_2,
        min_number_of_pages: int = MIN_PAGES,
        execute: bool = False,
    ) -> Plan:
        """Build the `EXEC dbo.IndexOptimize` that maintains this database.

        `execute=False` emits `@Execute = 'N'`, which is IndexOptimize's own
        dry run: it prints the commands it would issue and issues none. That
        is a *second* layer of dry-run underneath this tool's, and the two are
        independent — `plan_index_maintenance(execute=True)` still returns a
        plan and still runs nothing until `execute()` is called with it.

        Raises:
            CheckUnavailable: IndexOptimize is not installed, or is not
                reachable from this connection.
        """
        capabilities = self.capabilities
        if not capabilities.has_index_optimize:
            raise CheckUnavailable("index-maintenance", *_not_installed(capabilities.is_azure))

        schema = capabilities.ola_schema or "dbo"
        procedure = (
            f"{self._quote(str(capabilities.index_optimize_database))}"
            f".{self._quote(schema)}.{self._quote('IndexOptimize')}"
        )

        plan = Plan(engine=self.engine, database=self.database)

        # @LogToTable writes into CommandLog, which lives beside the procedure.
        # Asking for it when the table is absent is not a degraded run, it is
        # an error out of IndexOptimize itself, so it is gated on detection
        # rather than hoped for.
        log_to_table = capabilities.has_command_log
        if not log_to_table:
            plan.refused["@LogToTable"] = (
                f"{schema}.CommandLog was not found beside IndexOptimize, so the run "
                "cannot record what it did. Install it from the same script."
            )

        online = capabilities.supports_online_rebuild
        if not online:
            plan.refused[_WHOLE_PLAN] = (
                f"{capabilities.edition} has no online index rebuild, so heavily "
                "fragmented indexes are reorganized instead of rebuilt. A rebuild "
                "here would hold a schema-modification lock on the table for its "
                "whole duration, which this tool will not schedule for you."
            )

        arguments = [
            ("@Databases", _literal(databases or self.database)),
            ("@FragmentationLevel1", str(int(fragmentation_level_1))),
            ("@FragmentationLevel2", str(int(fragmentation_level_2))),
            ("@FragmentationMedium", _literal(_ACTIONS_REORGANIZE_ONLY)),
            ("@FragmentationHigh", _literal(_fragmentation_actions(online))),
            ("@MinNumberOfPages", str(int(min_number_of_pages))),
            ("@LogToTable", _literal("Y" if log_to_table else "N")),
            ("@Execute", _literal("Y" if execute else "N")),
        ]
        statement = f"EXEC {procedure}\n" + ",\n".join(
            f"    {name} = {value}" for name, value in arguments
        )

        action = "rebuild/reorganize" if execute else "report what it would do"
        plan.operations.append(
            Operation(
                target=procedure,
                description=(
                    f"IndexOptimize on {databases or self.database}: {action} for indexes "
                    f"above {fragmentation_level_1}% fragmentation and "
                    f"{min_number_of_pages} pages"
                ),
                sql=statement + ";",
                # Nothing is dropped and nothing is lost, so there is no
                # rollback to record and no manifest to write. `destructive`
                # governs exactly that, not "does this write".
                destructive=False,
            )
        )
        return plan

    # ------------------------------------------------------------------
    # Dropping unused indexes
    # ------------------------------------------------------------------

    def plan_drop_unused_indexes(
        self,
        indexes: list[UnusedIndex],
        *,
        min_size_bytes: int = DEFAULT_MIN_INDEX_BYTES,
    ) -> Plan:
        """Drop indexes confirmed unused, refusing anything load-bearing.

        The refusals are `safety.index_drop_refusal`'s, shared with the
        PostgreSQL backend rather than restated here: an index that enforces a
        constraint is not a read optimisation that happens to be unused, and
        that is true on both engines for the same reason.

        One guard is additional, and only because SQL Server makes it
        possible to be missing what PostgreSQL always has. `pg_get_indexdef`
        cannot fail; a SQL Server index definition has to be reassembled from
        `sys.index_columns`, and a drop whose `CREATE INDEX` could not be
        reassembled is not a drop, it is a deletion. Those are refused.

        Note the lock this takes, because there is no `CONCURRENTLY` to soften
        it — see the module docstring, point 3.
        """
        plan = Plan(engine=self.engine, database=self.database)

        for idx in indexes:
            target = self._index_target(idx)

            refusal = index_drop_refusal(idx)
            if refusal is not None:
                plan.refused[target] = refusal
                continue

            if idx.size_bytes < min_size_bytes:
                plan.refused[target] = (
                    f"only {idx.size_pretty} — below the {min_size_bytes // 1024 // 1024}MB "
                    "floor, so dropping it buys nothing"
                )
                continue

            rollback = _rollback_for(idx)
            if rollback is None:
                plan.refused[target] = (
                    "no CREATE INDEX statement was captured for it, so the drop could "
                    "not be undone from the rollback manifest"
                )
                continue

            plan.operations.append(
                Operation(
                    target=target,
                    description=f"{idx.size_pretty}, {idx.scans} scans, on {idx.table}",
                    # An index name is unique per table, not per schema, so
                    # the table is part of the statement rather than optional
                    # decoration.
                    sql=(
                        f"DROP INDEX {self._quote(idx.index)} "
                        f"ON {self._target(idx.schema, idx.table)};"
                    ),
                    destructive=True,
                    rollback_sql=rollback,
                )
            )
        return plan

    def _index_target(self, index: UnusedIndex) -> str:
        """Quoted three-part name: schema, table, index.

        Three parts rather than the PostgreSQL backend's two because SQL
        Server scopes index names to their table — `dbo.orders.ix_status` and
        `dbo.customers.ix_status` are different objects with the same name,
        and the manifest is an audit trail that has to tell them apart.
        """
        return f"{self._target(index.schema, index.table)}.{self._quote(index.index)}"

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(self, operations: list[Operation]) -> list[tuple[Operation, str | None]]:
        """Run operations, returning (operation, error) for each.

        One failure does not abort the rest, matching PostgreSQL: a partially
        applied maintenance run is normal, and the manifest already records
        how to undo whatever landed.

        The `read_only` refusal below is load-bearing in a way its PostgreSQL
        twin is not. There, the server would refuse the write anyway. Here,
        this `if` is the only thing between the tool and a production schema
        change — see the module docstring, point 2.
        """
        if self.read_only:
            raise RuntimeError(
                "This backend is read-only. Reconnect with read_only=False to execute."
            )

        results: list[tuple[Operation, str | None]] = []
        for op in operations:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(op.sql)
                    # IndexOptimize with @Execute='N' prints its commands as
                    # messages rather than rows, but a proc is free to return
                    # result sets and an unread one can leave the connection
                    # unusable for the next operation.
                    _drain(cur)
                results.append((op, None))
            except pyodbc.Error as exc:
                results.append((op, _message(exc)))
        return results


def _fragmentation_actions(supports_online_rebuild: bool) -> str:
    """What IndexOptimize should do above @FragmentationLevel2.

    Gated on the detected edition rather than left to fail at runtime:
    `ALTER INDEX ... REBUILD WITH (ONLINE = ON)` on Standard is error 40536 /
    "online index operations can only be performed in Enterprise edition",
    raised in the middle of a maintenance run rather than before it starts.
    """
    return _ACTIONS_ONLINE_REBUILD if supports_online_rebuild else _ACTIONS_REORGANIZE_ONLY


def _not_installed(is_azure: bool) -> tuple[str, str]:
    """(reason, remedy) for a server without IndexOptimize.

    Stated plainly, because the alternative reading — that the tool will
    install it — would be a tool writing procedures into master on a
    production instance without being asked.
    """
    azure_note = (
        "\nOn Azure SQL Database cross-database calls are not possible, so the\n"
        "procedures must be installed in this database; his site ships a separate\n"
        "script for it.\n"
        if is_azure
        else ""
    )
    return (
        "Ola Hallengren's IndexOptimize was not found in this database or in master.",
        f"SQL Server index maintenance here is his solution, driven by this tool.\n"
        f"This tool does not install it, does not bundle it, and will not write\n"
        f"procedures into your instance. Install it yourself from:\n"
        f"  {OLA_HALLENGREN_URL}\n"
        f"{azure_note}"
        f"Then re-run: detection finds IndexOptimize and CommandLog in whichever\n"
        f"database they were installed into.",
    )


def _rollback_for(index: UnusedIndex) -> str | None:
    """The index's own CREATE statement, or None if there is not one.

    `UnusedIndex.definition` is whatever the backend's index query could
    reassemble. Anything that is not a CREATE statement — an empty string, a
    column list, a placeholder — cannot be run to put the index back, and
    saying so is better than writing it into a manifest that will be trusted
    later.
    """
    definition = (index.definition or "").strip()
    if not definition.upper().startswith("CREATE"):
        return None
    return definition.rstrip(";") + ";"


def _literal(value: str) -> str:
    """A T-SQL string literal. Single quotes double, as in every dialect."""
    return "'" + value.replace("'", "''") + "'"


def _drain(cursor: pyodbc.Cursor) -> None:
    """Consume every result set the statement produced."""
    while True:
        if cursor.description is not None:
            cursor.fetchall()
        if not cursor.nextset():
            return


def _message(exc: pyodbc.Error) -> str:
    """The server's message, without pyodbc's tuple repr around it."""
    if len(exc.args) > 1 and isinstance(exc.args[1], str):
        return exc.args[1].strip()
    return str(exc).strip()
