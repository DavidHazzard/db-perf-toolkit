"""SQL Server backend.

A package rather than a single module, unlike postgres.py, because the SQL
Server surface splits along real seams: connection and read-only policy here,
feature detection in capabilities, the diagnostic queries and the maintenance
orchestration in their own modules. Feature detection in particular is not a
detail of one check — the edition, the version and whether Query Store is on
change the answers for several of them at once.

pyodbc is imported eagerly on purpose. The backend registry catches the
ImportError and turns it into installation instructions, so a user without the
optional dependency gets a sentence rather than a stack trace.
"""

from __future__ import annotations

from db_perf_toolkit.backends.sqlserver.capabilities import (
    ServerCapabilities,
    detect_capabilities,
)
from db_perf_toolkit.backends.sqlserver.connection import (
    APPLICATION_NAME,
    SqlServerConnectionError,
    Target,
    odbc_connection_string,
    parse_dsn,
)
from db_perf_toolkit.backends.sqlserver.connection import (
    SqlServerBackend as _SqlServerBase,
)
from db_perf_toolkit.backends.sqlserver.connection import (
    connect as _connect_base,
)
from db_perf_toolkit.backends.sqlserver.indexes import IndexChecks
from db_perf_toolkit.backends.sqlserver.maintenance import MaintenanceOperations
from db_perf_toolkit.backends.sqlserver.queries import QueryChecks
from db_perf_toolkit.models import Check


class SqlServerBackend(QueryChecks, IndexChecks, MaintenanceOperations, _SqlServerBase):
    """The backend callers actually get, assembled from the check modules.

    Each module owns its own SQL and declares only what it implements. The
    union below is what makes `supports` honest: it is built from the mixins
    that provide the methods, so it cannot name a check this class would then
    refuse. Setting it by hand, or on the base class, would allow exactly that
    — and a capability list that overstates itself is worse than none, because
    the base class raises "SQL Server has no equivalent of this check" for
    anything it does not find.

    INDEX_BURDEN is absent from every mixin and therefore from the union.
    sys.dm_db_index_usage_stats.user_updates counts statements rather than
    rows, and sys.dm_db_stats_properties.modification_counter resets on every
    statistics update — so the busiest tables would report the smallest
    numbers and the ranking would invert. indexes.py carries the measurements.
    """

    supports = frozenset[Check]().union(
        QueryChecks.supports,
        IndexChecks.supports,
        MaintenanceOperations.supports,
    )


def connect(
    dsn: str,
    *,
    connect_timeout: int = 10,
    read_only: bool = True,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int = 10_000,
) -> SqlServerBackend:
    """Open a composed SQL Server backend.

    Shadows connection.connect deliberately: that one returns the bare base,
    which refuses every check. Anything reaching this package through the
    registry should get the class that can answer them.
    """
    backend = _connect_base(
        dsn,
        connect_timeout=connect_timeout,
        read_only=read_only,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        backend_cls=SqlServerBackend,
    )
    assert isinstance(backend, SqlServerBackend)
    return backend


__all__ = [
    "APPLICATION_NAME",
    "IndexChecks",
    "MaintenanceOperations",
    "QueryChecks",
    "ServerCapabilities",
    "SqlServerBackend",
    "SqlServerConnectionError",
    "Target",
    "connect",
    "detect_capabilities",
    "odbc_connection_string",
    "parse_dsn",
]
