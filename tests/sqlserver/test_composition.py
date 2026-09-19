"""The composed backend must answer everything it advertises.

`supports` exists so the CLI can skip checks an engine cannot do. That only
works if the two agree: a capability list that overstates itself is worse
than none, because the base class raises "SQL Server has no equivalent of
this check" for anything the mixins did not provide — so an overstated entry
turns into a confident denial of a check that was promised.

Composition is the seam where that can break. The check modules are written
independently and each declares only what it implements; nothing but this
file asserts that the assembled class delivers the union.
"""

from __future__ import annotations

import inspect

import pytest

from db_perf_toolkit.backends.base import Backend
from db_perf_toolkit.backends.sqlserver import (
    IndexChecks,
    MaintenanceOperations,
    QueryChecks,
    SqlServerBackend,
)
from db_perf_toolkit.models import Check

#: Check -> the method that answers it.
_METHOD_FOR = {
    Check.SLOW_QUERIES: "slow_queries",
    Check.SEQ_SCANS: "seq_scan_hotspots",
    Check.MISSING_INDEXES: "missing_indexes",
    Check.UNUSED_INDEXES: "unused_indexes",
    Check.INDEX_BURDEN: "index_burden",
    Check.BLOAT: "bloated_tables",
    Check.FREE_SPACE: "free_space",
    Check.FRAGMENTATION: "index_fragmentation",
    Check.BLOCKING: "blocking_chains",
}


def test_every_advertised_check_is_actually_implemented() -> None:
    """No entry in `supports` may resolve to the base class's refusal."""
    for check in SqlServerBackend.supports:
        method = getattr(SqlServerBackend, _METHOD_FOR[check])
        owner = method.__qualname__.split(".")[0]
        assert owner != "Backend", (
            f"{check} is advertised but still resolves to Backend.{_METHOD_FOR[check]}, "
            "which raises 'no equivalent'. A mixin is missing from the composition."
        )


def test_supports_is_exactly_the_union_of_the_mixins() -> None:
    """Set by hand, this would drift the moment a module gained a check."""
    expected = QueryChecks.supports | IndexChecks.supports | MaintenanceOperations.supports
    assert SqlServerBackend.supports == expected


def test_unadvertised_checks_still_refuse() -> None:
    """The other half of the contract, and the easier half to lose.

    INDEX_BURDEN is the live case: SQL Server can produce a number for it, but
    user_updates counts statements rather than rows, so the number would mean
    something other than the column it lands in.
    """
    unsupported = set(Check) - SqlServerBackend.supports
    assert Check.INDEX_BURDEN in unsupported

    for check in unsupported:
        method = getattr(SqlServerBackend, _METHOD_FOR[check])
        owner = method.__qualname__.split(".")[0]
        assert owner == "Backend", (
            f"{check} is not advertised, yet {owner} implements it. Either declare it "
            "in that mixin's supports or remove the implementation — a method that "
            "exists but is never reachable through `supports` is dead either way."
        )


def test_composition_covers_every_check_in_the_enum() -> None:
    """Guards this file itself: a new Check with no mapping would skip silently."""
    assert set(_METHOD_FOR) == set(Check)


def test_maintenance_is_declared_separately_from_checks() -> None:
    """supports_maintenance is a boolean, not a Check, because maintenance is
    not something `report` runs."""
    assert SqlServerBackend.supports_maintenance is True
    assert MaintenanceOperations.supports_maintenance is True


@pytest.mark.parametrize("name", sorted(set(_METHOD_FOR.values())))
def test_signatures_stay_compatible_with_the_base(name: str) -> None:
    """A mixin narrowing a signature would break the CLI, which holds a Backend.

    Extra keyword-only parameters with defaults are fine — index_fragmentation
    adds `mode` that way — so this compares the parameters the base declares
    rather than requiring the signatures be identical.
    """
    base_params = inspect.signature(getattr(Backend, name)).parameters
    composed_params = inspect.signature(getattr(SqlServerBackend, name)).parameters

    for param in base_params:
        assert param in composed_params, (
            f"{name} drops the base parameter {param!r}; the CLI calls this through "
            "a Backend reference and would fail at runtime."
        )
