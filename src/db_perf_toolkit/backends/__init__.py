"""Backend registry and engine dispatch.

The engine is chosen from the DSN scheme rather than a required flag: a
connection string already says what it connects to, and making the user
repeat it is a papercut on every invocation.
"""

from __future__ import annotations

from urllib.parse import urlparse

from db_perf_toolkit.backends.base import Backend, CheckUnavailable
from db_perf_toolkit.backends.postgres import PostgresBackend
from db_perf_toolkit.backends.postgres import connect as connect_postgres

__all__ = [
    "Backend",
    "CheckUnavailable",
    "PostgresBackend",
    "UnknownEngineError",
    "connect",
    "connect_postgres",
    "engine_for_dsn",
]

#: DSN scheme -> engine key. SQLAlchemy-style "+driver" suffixes are stripped
#: before lookup, so mssql+pyodbc:// and mssql:// both resolve.
_SCHEMES = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "postgresql+psycopg": "postgres",
    "mssql": "sqlserver",
    "sqlserver": "sqlserver",
    "mssql+pyodbc": "sqlserver",
}


class UnknownEngineError(ValueError):
    """The DSN names an engine this tool does not speak."""


def engine_for_dsn(dsn: str) -> str:
    scheme = urlparse(dsn).scheme.lower()
    if not scheme:
        raise UnknownEngineError(
            f"No scheme in {dsn!r}. Expected something like "
            "postgresql://user@host/db or mssql://user@host/db"
        )
    key = _SCHEMES.get(scheme) or _SCHEMES.get(scheme.split("+", 1)[0])
    if key is None:
        supported = ", ".join(sorted(set(_SCHEMES)))
        raise UnknownEngineError(f"Unsupported scheme {scheme!r}. Supported: {supported}")
    return key


def connect(dsn: str, **kwargs: object) -> Backend:
    """Open a backend for whichever engine the DSN names."""
    engine = engine_for_dsn(dsn)

    if engine == "postgres":
        return connect_postgres(dsn, **kwargs)  # type: ignore[arg-type]

    if engine == "sqlserver":
        try:
            from db_perf_toolkit.backends.sqlserver import connect as connect_sqlserver
        except ImportError as exc:
            raise UnknownEngineError(
                "SQL Server support is not installed.\n"
                "Install the extra and the Microsoft ODBC driver:\n"
                "  pip install 'db-perf-toolkit[sqlserver]'\n"
                "  https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server"
            ) from exc
        return connect_sqlserver(dsn, **kwargs)  # type: ignore[arg-type,no-any-return]

    raise UnknownEngineError(f"No backend registered for {engine!r}")
