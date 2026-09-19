# Continuous integration

Every push to `main` and every pull request runs [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) on GitHub-hosted `ubuntu-latest` runners. Four jobs run in parallel.

| Job | Blocking | What it does |
|---|---|---|
| **Lint** | yes | `uv lock --check`, `ruff check .`, `ruff format --check .`, and one structural check on where SQL Server tests live |
| **Type check** | yes | `mypy` in strict mode over `src` and `tests` |
| **Tests (PostgreSQL)** | yes | `pytest -m "not sqlserver"` against a real PostgreSQL 16 container |
| **Tests (SQL Server)** | yes | `pytest -m sqlserver` against a real SQL Server 2022 container |

## Status badge

Paste this under the title in `README.md`:

```markdown
[![CI](https://github.com/DavidHazzard/db-perf-toolkit/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/DavidHazzard/db-perf-toolkit/actions/workflows/ci.yml)
```

All four jobs block, so the badge means all four passed.

## The type-check job blocks, and the story of how it got there

`pyproject.toml` sets `strict = true`, and `mypy` passes cleanly across `src` and `tests`. The job blocks merges.

It was not written that way. When this workflow was first drafted, `uv run mypy` reported **22 errors across 6 files**, and the job was `continue-on-error` with a comment saying so — gating merges on a check that has never passed just means a red badge from day one, and the alternative of scattering `# type: ignore` until the number reaches zero is worse.

What made the difference is that 16 of the 22 were a single error repeated. The maintenance tests called `plan_drop_unused_indexes`, `plan_vacuum`, `execute` and `_conn` on a value typed as the `Backend` ABC, which did not declare any of them. That is not a typing nuisance; it is the type checker pointing out that the abstract interface stopped short of what every concrete backend actually offered. Widening the ABC to match reality cleared all 16 at once, the remaining handful went the same day, and the `continue-on-error` came off.

The only concession in the config is `ignore_missing_imports` for `pyodbc` and `testcontainers`, neither of which ships stubs — and `pyodbc` is an optional extra that may not be installed when mypy runs at all. `strict = true` and `files = ["src", "tests"]` are untouched; nothing was narrowed to make the number fall.

Deliberately, no baseline file and no error-count ratchet. Both are a second thing to maintain and a second thing to be wrong. If the job goes red, the fix is the type error.

## Expected duration

Both engines run on every push. That was a deliberate choice: a SQL Server backend that is only exercised nightly is a SQL Server backend that is broken most mornings.

The four jobs run concurrently, so wall time is the slowest job, not the sum. Times below are for a warm run — a `uv.lock` unchanged since the previous run, and the SQL Server image already in the Actions cache.

| Job | Warm | Where it goes |
|---|---|---|
| Lint | ~45 s | 15 s runner/checkout/uv setup, 5 s `uv sync`, ~5 s ruff and the boundary check, rest is job startup |
| Type check | ~1 min | 20 s setup and sync, ~25 s mypy on a cold cache |
| Tests (PostgreSQL) | ~1 min 15 s | 20 s setup, ~15 s pulling `postgres:16` (150 MB), then **11 s measured** for container boot and the whole suite — call it ~20 s on a runner |
| Tests (SQL Server) | ~2 min 30 s | 20 s setup, ~30 s ODBC driver install, ~40 s cache restore + `docker load`, then **19 s measured** for container boot and the whole suite — call it ~40 s on a runner |

The two test figures in bold are measured end-to-end on a developer machine with a warm image, not estimated. The runner figures beside them are those numbers scaled for slower hardware, and are the honest guess in this table.

They should also stay roughly true as the suites grow, because **the container start dominates and every test shares it.** Both suites use a session-scoped fixture, so the SQL Server container's ~9 s to a server that accepts logins is paid once per run, not once per test. Measured directly: going from 59 to 73 SQL Server tests moved the total from 16.5 s to 17.2 s — fourteen more tests against a real server for under a second. The figure to watch is not the test count; it is anything that adds a *second container*, which is why this job must not run under xdist.

**Wall time: roughly 2½–3 minutes**, set by the SQL Server job. A cold run — first push after a dependency bump, or after the image cache is evicted — adds about a minute to that, mostly in the image pull.

Two things keep this honest rather than optimistic:

- `concurrency.cancel-in-progress` kills the previous run when you push again, so a rapid series of pushes costs one run's worth of minutes, not five.
- The SQL Server job probes for tests before doing any expensive setup (below). Now that the suite exists that no longer short-circuits anything, but it is what allowed this workflow to be merged before the tests it runs — and it still keeps the job from spending 2½ minutes to discover there is nothing to do.

## Caching the SQL Server image

`mcr.microsoft.com/mssql/server:2022-latest` is the single largest cost in the workflow. Measured, not estimated:

| | |
|---|---|
| Compressed layers in the registry (what `docker pull` downloads) | **626 MB** |
| Unpacked on disk | **1.69 GB** |
| `docker save` tar | **1.70 GB**, ~10 s to write |
| That tar after zstd −3 (what `actions/cache` stores and transfers) | **556 MB**, ~4.5 s to compress |

The workflow keys an `actions/cache` entry on the image ref, `docker load`s it on a hit, and on a miss pulls, `docker save`s, and lets the cache pick the tar up at the end of the job.

**What the cache actually saves.** On a hit it replaces a 626 MB fetch from Microsoft's registry plus decompression into 1.69 GB of layers with a 556 MB fetch from the Actions cache — same Azure region as the runner, so typically two to three times the throughput — plus a local `docker load`. In practice that is **roughly 30–50 seconds a run**. Worth having, and worth being clear that it is seconds rather than minutes: the image still has to reach the runner's disk either way, and no cache changes that.

The larger benefit is not the seconds. It is that Microsoft's registry leaves the critical path. A transient 5xx or a throttle from `mcr.microsoft.com` is otherwise a red badge caused by nothing in this repository, which is exactly the failure mode a public portfolio repo can least afford.

**What it costs.** A cache *miss* is slower than having no cache at all — the pull still happens, and then `docker save` (~10 s) and a 556 MB upload are added on top, call it 25–35 s of overhead. That is paid only when the key changes.

**Staleness.** The key pins a moving tag, so once `2022-latest` is cached the runner keeps that build until someone bumps `MSSQL_CACHE_EPOCH` in `ci.yml`. That is reproducibility rather than a bug, but it is a decision with a shelf life: bump the epoch when you want Microsoft's current image.

For reference, the container itself is cheap once the image is local. Measured on a developer machine with a warm image, and worth reading carefully because the obvious measurement is the wrong one:

| | |
|---|---|
| `docker run` returns | 0.5 s |
| TCP 1433 accepts connections | 0.5 s |
| Server logs *"ready for client connections"* | 5.4 s |
| `sa` can actually answer `SELECT 1` | **~8.1 s** |
| Seed applied, suite ready | ~12 s |

A port check calls this ready at half a second and the log line calls it ready at five, but neither can log in. **Only the last two numbers are real.**

The reason is worth keeping next to the number, because the log line is the intuitive thing to wait on and it looks like the right answer. The engine logs *"ready for client connections"* as soon as it can serve — but the image's entrypoint applies `MSSQL_SA_PASSWORD` *after* that. So in the window between 5.4 s and roughly 7.7 s the server is up and answering, and rejecting `sa` with:

```
[28000] Login failed for user 'sa'. (18456)
```

Which reads exactly like a wrong password. That is the trap: waiting on the port or on the log line does not fail as a timeout, it fails as a credentials bug, and sends you to debug the wrong thing entirely. The fixtures poll for a successful query instead, which is why they are correct where a `wait_for_port` would not be.

Either way the boot is seconds and the transfer is minutes. Only the transfer is worth caching.

## The SQL Server job tolerates the suite not existing

`pytest -m sqlserver` exits **5** — "no tests collected" — when nothing carries the marker, and GitHub Actions treats any non-zero exit as a failure. The job therefore probes first:

```bash
uv run pytest -m sqlserver --collect-only -q > /dev/null 2>&1
```

Exit 5 sets a step output of `found=false`, writes a line to the run summary, and every subsequent step in the job is skipped — no ODBC driver install, no 1.7 GB image, no container. Exit 0 proceeds normally. Any *other* exit code is a real collection error and fails the job, so a broken `conftest.py` is not quietly mistaken for an empty suite.

This matters beyond the current moment: it means the SQL Server job can be merged before the SQL Server tests are, and it means deleting the suite would never leave a job failing for want of something to run.

**But absent is not the same as fine**, and "zero tests collected" has two causes that deserve opposite treatment. The job distinguishes them:

| State | Treatment |
|---|---|
| No `tests/sqlserver/test_*.py` at all | Tolerated. `::warning::` annotation on the run and the PR, **Nothing ran** in the job summary, build stays green. |
| Test modules exist, marker selects nothing | **Fails the job**, with an error annotation naming the likely cause. |

The second case is a regression — the marker renamed, or the auto-marking hook in `tests/sqlserver/conftest.py` no longer matching — and its symptom is that the entire suite silently disappears.

That hook is worth understanding, because it is why this check has to live in the workflow rather than in the test suite. `tests/sqlserver/conftest.py` applies the `sqlserver` marker in `pytest_collection_modifyitems` by comparing each item's path against the package directory, so no one has to remember a decorator. It is the right design, and it **fails open**: move the conftest, restructure the directory, or let a future pytest change what `item.path` returns, and the hook does not raise. It marks nothing, every test deselects, and the run reports zero selected with exit 5 — green, and wrong.

No assertion inside the conftest can catch that, because a hook that marks nothing is indistinguishable from a hook with nothing to mark. Telling the two apart needs an observer outside the suite that knows whether test files exist on disk, which is exactly what this step is. The error annotation names both causes deliberately: "marker renamed" is what people will guess, and "the hook stopped matching" is what will actually happen, because it survives a refactor that looks entirely safe.

The other useful property is that this needs no maintenance. There is no flag to flip and nobody has to remember: the guard reads the filesystem, so the moment `tests/sqlserver/test_*.py` lands, tolerance for an empty collection ends by itself.

### The hook's guarantee has a boundary, and the lint job patrols it

Because the marker is applied by path, what the hook actually guarantees is *"everything under `tests/sqlserver/`"* — not *"every SQL Server test"*. Those come apart the moment someone writes a SQL Server test somewhere else. It would go unmarked, `-m "not sqlserver"` would select it, and **the PostgreSQL job would quietly pull a 1.7 GB image and start a SQL Server container** — slow, confusing, and green.

Nothing inside the conftest can prevent that, because the file it would need to notice is outside the package it governs. So the lint job checks the boundary from above:

```bash
grep -rl 'pyodbc' tests --include='*.py' | grep -v '^tests/sqlserver/'
```

Any hit fails the build and names the files.

**Why `pyodbc` and not a filename pattern.** Pytest scopes conftest fixtures to their own directory subtree, so a stray test cannot borrow the SQL Server fixtures — asking for one from `tests/` root is an immediate `fixture 'seeded_sqlserver_dsn' not found` at setup, which is loud and harmless. The dangerous file is therefore necessarily *self-contained*: it has to build its own connection, because it cannot borrow one. And a self-contained SQL Server test has to import a driver. That makes `pyodbc` close to a necessary condition for the only form this failure can take, rather than a guess at what a SQL Server test looks like — which is why this is tighter than matching `*sqlserver*` in a filename.

The residual gap, recorded so nobody mistakes it for an oversight: a self-contained test that reaches SQL Server without importing `pyodbc` — driving `sqlcmd` through `subprocess`, say. It is not worth widening the grep for. A check that fires on real cases and is understood beats one that fires on imagined ones and gets disabled the first time it is wrong.

It costs a fraction of a second, runs in the job that needs no Docker, and turns an invariant that held by convention into one that holds because it is checked.

The marker partition is exact today: `-m sqlserver` and `-m "not sqlserver"` sum to the full collection, with no test in both and none in neither. This step is what keeps that true.

## Two guards against a green run that tested nothing

The SQL Server fixtures `skip` rather than fail when they cannot connect. That is the right behaviour for a developer without SQL Server installed, and exactly the wrong behaviour on CI: a job that skips its entire suite exits 0 and shows a green check. There are two ways to fall into that, and the workflow blocks both.

**Before the suite runs**, it asks the question through the same interpreter pytest will use:

```python
# Raises ImportError if the sqlserver extra never arrived.
import pyodbc

# Empty if pyodbc is installed but no driver is registered.
drivers = [d for d in pyodbc.drivers() if "for SQL Server" in d]
if not drivers:
    sys.exit(...)
```

This catches both failure modes in one check. `pyodbc` lives in an optional extra, so any `uv run` that forgets `--all-extras` leaves it uninstalled — every job here runs a single `uv sync --all-extras --dev` up front precisely so the extra cannot go missing between steps. The `msodbcsql18` install itself is skipped when a driver is already registered, which is true of some runner images.

**After the suite runs**, the job checks that something actually passed:

```bash
case "$summary" in
  *" passed"*) ;;
  *) echo "::error::No SQL Server test actually ran — every test skipped."; exit 1 ;;
esac
```

Deliberately "did anything pass", not "did anything skip". A suite with a couple of legitimately skipped tests is fine; a suite where *nothing* ran is not. The job also passes `-rs`, so every skip prints its reason rather than hiding behind a dot.

One operational note: do not add `-n auto` to this job. Each xdist worker gets its own pytest session and therefore its own 1.7 GB container.

## Why there is no coverage report

There is no coverage job, deliberately.

Every test in this suite is an integration test against a live server. Coverage of such a suite measures how much of the code a handful of end-to-end paths happen to touch, which is a large number that means very little — and the things this project actually needs to be right about are whether the catalog queries return correct answers and whether the destructive paths refuse what they should. Neither is a line-coverage question. Most of the maintenance tests assert that something is *refused*; a coverage percentage cannot tell you whether the refusal was correct.

Adding `pytest-cov` would also mean a dependency in the lockfile that exists only to produce a number nobody acts on. If a gate is ever wanted, the honest one is a branch-coverage floor on `safety.py` and `manifest.py` specifically, not a repository-wide percentage.

What the jobs *do* publish to each run's summary is the part that carries information: the pass/fail line for each suite, and — when the SQL Server suite has not landed yet — a line saying so, rather than a job that silently did nothing.

## Dependency updates

[`.github/dependabot.yml`](../../.github/dependabot.yml) opens PRs weekly for both `pip` and `github-actions`. Development tooling (`pytest*`, `ruff`, `mypy`, `testcontainers`) is grouped into a single PR so a routine week is one review, not four.

The lint job runs `uv lock --check`, so a Dependabot PR that edits `pyproject.toml` without regenerating `uv.lock` fails immediately rather than leaving the lockfile quietly out of step with the manifest.

## Action versions

Actions are pinned to major tags — `actions/checkout@v4`, `actions/cache@v4`, `astral-sh/setup-uv@v6` — so patch and minor fixes arrive without a PR, and a major bump arrives as a Dependabot PR you can read.

## Running the same checks locally

Everything CI runs, runs here. There is no `make ci` indirection and no step that exists only on the runner:

```bash
uv sync --all-extras --dev
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -m "not sqlserver"
uv run pytest -q -rs -m sqlserver
```

All of these need a working Docker daemon for the test steps, and the SQL Server step additionally needs `msodbcsql18` installed locally. Run the `uv sync` first and keep it: if you reach for `uv run --extra sqlserver pytest` instead, be consistent about it across every command in the shell, or an earlier plain `uv run` can leave `pyodbc` uninstalled and the whole SQL Server package will quietly skip.
