"""CLI surface tests.

These need no database. That is the point: every defect they cover was
user-facing and none of it required a server to find — stale help text naming
one engine after two shipped, checks implemented on a backend but reachable
from no command, and a bad DSN producing a driver traceback instead of a
message. The CLI is the whole user-facing surface of this tool and had no
tests, so these are the cheapest ones in the repo.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from db_perf_toolkit import cli
from db_perf_toolkit.backends import Backend, CheckUnavailable
from db_perf_toolkit.models import BloatedTable, Check, StatsWindow


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_every_check_has_a_command(runner: CliRunner) -> None:
    """A check nobody can invoke is not a feature.

    `missing-indexes` and `fragmentation` were implemented, tested against a
    real server, and documented — and had no command, so no user could run
    either. The Check enum is the register of what this tool claims to
    answer, so it is what the CLI is measured against.
    """
    commands = set(cli.main.commands)
    missing = {c.value for c in Check} - commands
    assert not missing, f"checks with no CLI command: {sorted(missing)}"


def test_every_command_has_working_help(runner: CliRunner) -> None:
    for name in cli.main.commands:
        result = runner.invoke(cli.main, [name, "--help"])
        assert result.exit_code == 0, f"{name} --help failed:\n{result.output}"
        assert result.output.strip(), f"{name} has no help text"


def test_group_help_names_both_engines(runner: CliRunner) -> None:
    """Regression: this said "a PostgreSQL database" after SQL Server shipped.

    Help text is documentation that ships inside the binary, and it is the
    copy most likely to be read and least likely to be reviewed.
    """
    result = runner.invoke(cli.main, ["--help"])
    assert result.exit_code == 0
    assert "SQL Server" in result.output
    assert "PostgreSQL" in result.output


def test_help_works_without_a_dsn(runner: CliRunner) -> None:
    """A tool must be able to explain itself before it is configured.

    `--dsn` was `required=True` on the group, and Click validates group
    options before dispatch — so every `dbperf <command> --help` died with
    "Missing option '--dsn'" instead of printing help.
    """
    result = runner.invoke(cli.main, ["bloat", "--help"], env={"DBPERF_DSN": ""})
    assert result.exit_code == 0, result.output
    assert "--min-dead-pct" in result.output


def test_missing_dsn_is_a_usage_error_not_a_crash(runner: CliRunner) -> None:
    result = runner.invoke(cli.main, ["bloat"], env={"DBPERF_DSN": ""})
    assert result.exit_code == 2
    assert "--dsn" in result.output


def test_engine_only_commands_say_so(runner: CliRunner) -> None:
    """A SQL Server check must not read as universal.

    Someone pointing this at PostgreSQL should learn from --help that the
    check cannot answer there, rather than from an error after connecting.
    """
    for name in ("missing-indexes", "fragmentation", "index-maintenance"):
        result = runner.invoke(cli.main, [name, "--help"])
        assert "SQL Server only" in result.output, f"{name} does not state its engine"


def test_unknown_engine_exits_cleanly(runner: CliRunner) -> None:
    result = runner.invoke(cli.main, ["--dsn", "mysql://host/db", "bloat"])
    assert result.exit_code == 2
    assert "Could not connect" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_unreachable_host_exits_cleanly(runner: CliRunner) -> None:
    """Exit 2 and a message — not a psycopg traceback."""
    dsn = "postgresql://nobody@127.0.0.1:1/nothing"
    result = runner.invoke(cli.main, ["--dsn", dsn, "--timeout", "2", "bloat"])
    assert result.exit_code == 2
    assert "Could not connect" in result.output


@pytest.mark.parametrize(
    "command",
    ["bloat", "blocking", "report", "vacuum", "drop-unused-indexes", "index-maintenance"],
)
def test_connection_failure_is_handled_on_every_command(runner: CliRunner, command: str) -> None:
    """The handler lives on the group precisely so this holds for all of them.

    It used to live in two command bodies, which meant the maintenance
    commands — added later — tracebacked on a DSN that did not resolve.
    """
    dsn = "postgresql://nobody@127.0.0.1:1/nothing"
    result = runner.invoke(cli.main, ["--dsn", dsn, "--timeout", "2", command])
    assert result.exit_code == 2, f"{command} did not report the connection failure"
    assert "Could not connect" in result.output


def test_unavailable_check_exits_3_with_the_remedy(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unavailable(Backend):
        engine = "postgres"

        def stats_window(self) -> StatsWindow:
            return StatsWindow(server_version="PostgreSQL 16.0", stats_reset=None)

        def bloated_tables(self, min_dead_pct: float, min_dead_rows: int) -> list[BloatedTable]:
            raise CheckUnavailable(
                "bloat", "pgstattuple is not installed.", "CREATE EXTENSION pgstattuple;"
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "connect", lambda *a, **k: Unavailable())
    result = runner.invoke(cli.main, ["--dsn", "postgresql://x/y", "bloat"])

    assert result.exit_code == 3
    assert "pgstattuple is not installed" in result.output
    assert "CREATE EXTENSION pgstattuple;" in result.output


@pytest.mark.parametrize(
    ("call", "expected_pointer"),
    [
        (lambda b: b.index_fragmentation(0.0, 0), "free-space"),
        (lambda b: b.missing_indexes(0.0), "seq-scans"),
    ],
)
def test_check_missing_on_this_engine_points_at_its_neighbour(
    call: object, expected_pointer: str
) -> None:
    """Refusing is not enough; the refusal has to be worth reading.

    A user who runs `dbperf fragmentation` against PostgreSQL has a real
    question, and the answer is not a list of eight unrelated check names. It
    is "that concept does not exist here, and `free-space` is the closest
    thing that does" — with the difference stated, because the two are
    neighbours rather than substitutes.
    """
    from db_perf_toolkit.backends.postgres import PostgresBackend

    backend = PostgresBackend.__new__(PostgresBackend)
    with pytest.raises(CheckUnavailable) as exc:
        call(backend)  # type: ignore[operator]

    remedy = exc.value.remedy or ""
    assert f"dbperf {expected_pointer}" in remedy
    # The pointer must not read as an equivalence.
    assert "not" in remedy.split("Supported here")[0]
