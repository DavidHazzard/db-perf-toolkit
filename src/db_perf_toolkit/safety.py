"""Guards applied before anything destructive runs.

These are deliberately conservative. A tool that drops the wrong index on a
large table at 3am is worse than no tool, and the cost of a false refusal is
a command-line flag.
"""

from __future__ import annotations

from datetime import UTC, datetime

from db_perf_toolkit.models import StatsWindow, UnusedIndex

#: Below this, "unused" is an artefact of the measurement window rather than a
#: finding. PostgreSQL counters are cumulative since the last reset, so an
#: index on a monthly report query looks untouched for 29 days out of 30.
MIN_STATS_AGE_DAYS = 7


class RefusedError(Exception):
    """A guard blocked the operation. The message explains the override."""


def stats_window_age_days(window: StatsWindow) -> float | None:
    if window.stats_reset is None:
        # Never reset: counters cover full server uptime, which is the
        # strongest window available.
        return None
    now = datetime.now(window.stats_reset.tzinfo or UTC)
    return (now - window.stats_reset).total_seconds() / 86400


def check_stats_window(window: StatsWindow, *, min_days: int = MIN_STATS_AGE_DAYS) -> None:
    age = stats_window_age_days(window)
    if age is None:
        return
    if age < min_days:
        raise RefusedError(
            f"Statistics were reset {age:.1f} days ago, which is too short a window to "
            f"conclude an index is unused (minimum {min_days} days).\n"
            f"An index serving a weekly or monthly query looks untouched most of the time.\n"
            f"Override with --min-stats-age-days if you are certain."
        )


def index_drop_refusal(index: UnusedIndex) -> str | None:
    """Why this index must not be dropped, or None if it may be.

    Constraint enforcement is the line. A unique index is not a read
    optimisation that happens to be unused — it governs what the table will
    accept, so dropping it is a schema change wearing a cleanup's clothing.
    """
    if index.enforces_constraint:
        return "backs a constraint (dropping it changes what the table accepts)"
    if index.is_unique:
        return "unique index (enforces uniqueness even without a constraint row)"
    if index.scans > 0:
        return f"has {index.scans} recorded scans"
    return None
