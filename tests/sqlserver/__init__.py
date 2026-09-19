"""Integration tests backed by a real SQL Server instance.

A package rather than a bare directory so that `tests/sqlserver/conftest.py`
gets an unambiguous module name of its own. `tests/` itself has no
`__init__.py`, so without this the two conftests would both want to be
imported as top-level `conftest` under pytest's default `prepend` import mode.
"""
