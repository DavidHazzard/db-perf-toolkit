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
    SqlServerBackend,
    SqlServerConnectionError,
    Target,
    connect,
    odbc_connection_string,
    parse_dsn,
)

__all__ = [
    "APPLICATION_NAME",
    "ServerCapabilities",
    "SqlServerBackend",
    "SqlServerConnectionError",
    "Target",
    "connect",
    "detect_capabilities",
    "odbc_connection_string",
    "parse_dsn",
]
