# Releasing

Releases are tag-driven. Pushing a tag matching `v*` runs
[`.github/workflows/release.yml`](../../.github/workflows/release.yml), which
lints, tests, builds, verifies, and publishes to PyPI.

There are no API tokens in this repository and none should ever be created. The
workflow authenticates to PyPI with [Trusted
Publishing](https://docs.pypi.org/trusted-publishers/) — GitHub mints a
short-lived OIDC token for that one workflow run, and PyPI exchanges it for an
upload token that expires in fifteen minutes. A leaked repository secret is not a
risk you can have if there is no secret.

The one-time setup below has to be done **before the first tag is pushed**.

---

## One-time setup on pypi.org

Do this once, by hand, as the account that will own the project. It takes about
five minutes.

### 1. A PyPI account with 2FA

- Register at <https://pypi.org/account/register/> if you have not.
- PyPI requires two-factor authentication for publishing. Enable it under
  <https://pypi.org/manage/account/> — an authenticator app (TOTP) or a security
  key. Save the recovery codes somewhere that is not this laptop.

### 2. Create a *pending* publisher

`db-perf-toolkit` does not exist on PyPI yet, so there is no project to attach a
publisher to. PyPI's answer is a **pending publisher**: the configuration exists
first, and the project is created by the first successful upload that matches it.

1. Go to <https://pypi.org/manage/account/publishing/>.
2. Scroll to **Add a new pending publisher** and choose the **GitHub** tab.
3. Fill in exactly these five fields:

   | Field | Value | Why |
   |---|---|---|
   | **PyPI Project Name** | `db-perf-toolkit` | Must equal `name` in `pyproject.toml`, character for character. |
   | **Owner** | `DavidHazzard` | The GitHub user or organisation that owns the repository. Not a display name. |
   | **Repository name** | `db-perf-toolkit` | Just the repository, with no owner prefix and no `.git`. |
   | **Workflow name** | `release.yml` | The **filename** of the workflow, not the `name:` inside it. Not a path — `release.yml`, not `.github/workflows/release.yml`. |
   | **Environment name** | `pypi` | Must match `environment: name: pypi` in the publish job. Optional to PyPI, and you want it — see below. |

4. Click **Add**. The pending publisher is now listed on that page.

> **The environment name is the field people get wrong, and it is the one that
> matters most.** Without it, *any* workflow run in the repository that can reach
> `release.yml` can publish. With it, PyPI additionally requires the run to be
> executing inside the GitHub environment named `pypi`, which you can put
> approval rules on (step 3). Leave it blank in PyPI and it must be blank in the
> workflow too; the two are compared literally, and a mismatch fails the upload
> with `invalid-publisher`.

### 3. Create the matching GitHub environment

1. In the GitHub repository: **Settings → Environments → New environment**.
2. Name it `pypi` — same string as above, lowercase.
3. Optional and recommended: under **Deployment protection rules**, tick
   **Required reviewers** and add yourself. A tag push then pauses at the publish
   job until you approve it in the Actions tab. Everything before that point —
   lint, tests, build, `twine check`, the wheel smoke test — has already run, so
   what you are approving is an artifact you can see the verification for.
4. Also optional: under **Deployment branches and tags**, restrict to tags
   matching `v*`, so the environment cannot be entered from an arbitrary branch.

### 4. After the first successful release

The pending publisher is consumed and becomes a normal trusted publisher attached
to the project. You can see it at
<https://pypi.org/manage/project/db-perf-toolkit/settings/publishing/>, and that
is where you would add another one later (a second workflow, or a fork you
control). Nothing needs to change in this repository.

---

## Cutting a release

1. **Pick the version.** Pre-1.0, a new check or CLI flag is a minor bump
   (`0.2.0`); a fix that changes no interface is a patch bump (`0.1.1`). A change
   that breaks an existing command's output or flags is also a minor bump while
   the leading digit is `0`, and it is called out in the changelog with the word
   *breaking*.

2. **Bump `__version__`.** One line, in
   [`src/db_perf_toolkit/__init__.py`](../../src/db_perf_toolkit/__init__.py):

   ```python
   __version__ = "0.2.0"
   ```

   That is the only place a version number is written. `pyproject.toml` declares
   `dynamic = ["version"]` and reads it from there, so the wheel metadata and
   `dbperf --version` cannot disagree.

3. **Update `CHANGELOG.md`.** Rename the `## [Unreleased]` heading to
   `## [0.2.0] - YYYY-MM-DD`, open a fresh empty `## [Unreleased]` above it, and
   update the two link definitions at the bottom of the file.

4. **Commit, then tag.** The tag must be the version with a `v` prefix:

   ```bash
   git commit -am "Release 0.2.0"
   git push
   git tag -a v0.2.0 -m "v0.2.0"
   git push origin v0.2.0
   ```

5. **Watch the run** in the Actions tab. If you enabled required reviewers, it
   will wait for you at **Publish to PyPI**.

The workflow refuses to publish if the tag and `__version__` disagree: it checks
that the build produced `db_perf_toolkit-<tag without v>-py3-none-any.whl`, and
fails with a message naming both if it did not. Delete the tag, fix the version,
tag again.

## What the workflow does, and why each step is there

| Step | Why it is a release gate |
|---|---|
| `ruff check` | Cheap, and a release is the wrong time to discover lint drift. |
| `pytest` | The integration suite needs a Docker daemon, which `ubuntu-latest` has. Publishing untested code is worse than a release that takes three more minutes. |
| `uv build` | Produces the sdist and the wheel from a clean checkout, not from your working tree. |
| Tag/version check | A PyPI version number can never be reused or reassigned. Catching a mismatch before upload is the whole game. |
| `twine check --strict` | Catches a README that PyPI will not render, which is otherwise only visible after the upload is permanent. |
| Wheel smoke test | Installs the built wheel into a clean virtualenv and runs `dbperf --help` and `--version`. Proves the entry point resolves and the version string matches the tag. |
| `pypa/gh-action-pypi-publish` | Uploads with the OIDC identity. It also generates [PEP 740 attestations](https://docs.pypi.org/attestations/), which is why the publish job holds `id-token: write`. |
| `gh release create` | Attaches the same artifacts to a GitHub release, so the tag has something readable next to it. |

There is deliberately **no mypy step**. Type checking belongs on every push, in
CI, not on the one path that has to stay runnable when you need to ship a fix.

## When something goes wrong

**The upload failed with `invalid-publisher`.** PyPI compared the OIDC claims
against the publisher configuration and one field did not match. The error page
on PyPI prints the claims it actually received — compare them to the five fields
in step 2, in this order: environment name (most common), workflow filename,
repository name, owner, project name.

**The upload failed, and I want to retry.** A rejected upload does not consume
the version number. Fix the cause and push the tag again (delete and re-push it:
`git tag -d v0.2.0 && git push --delete origin v0.2.0`, then re-tag).

**The upload succeeded and the release is broken.** The version number is spent
permanently — PyPI does not allow re-uploading a file, even after deletion. Fix
forward with a patch release, and
[yank](https://pypi.org/help/#yanked) the bad version from the project's
**Manage → Releases** page. Yanking leaves it installable for anyone who pins it
exactly, and hides it from resolution for everyone else, which is what you want.

**A dry run against TestPyPI.** TestPyPI is a separate instance with separate
accounts and separate publishers. To rehearse: register at
<https://test.pypi.org/>, create the same pending publisher there with the
environment name `testpypi`, and add a temporary job to the workflow that is a
copy of `publish` with `environment: name: testpypi` and

```yaml
      - uses: pypa/gh-action-pypi-publish@release/v1
        with:
          repository-url: https://test.pypi.org/legacy/
```

Tag something like `v0.1.0rc1`, confirm it lands at
<https://test.pypi.org/project/db-perf-toolkit/>, then remove the job. Worth
doing once, before the first real release, because it exercises the OIDC exchange
end to end against a registry where a burnt version number does not matter.

## Versioning: why the number lives in `__init__.py`

The alternative was deriving the version from the git tag with `hatch-vcs`, and
it was rejected for one concrete reason: `dbperf --version` reads
`db_perf_toolkit.__version__`, and this project prints that version into a
rollback manifest that exists to be read back after something has gone wrong.
Under `hatch-vcs` the version is materialised at build time, so an editable or
in-tree run reports whatever the last tag plus a dev suffix says, and a checkout
without git metadata — an unpacked sdist, a Docker build with a shallow copy —
has no version at all. A tool whose audit trail can say `0.0.0` is worse than a
tool that asks you to edit one line.

So: **one literal, in `src/db_perf_toolkit/__init__.py`, read by the build
backend.** `pyproject.toml` no longer carries a version of its own, which removes
the drift that existed before — the two files each said `0.1.0` independently,
and nothing checked that they agreed.

The tag is then an assertion about that literal rather than the source of it, and
the workflow verifies the assertion before anything is published. That is the
property that matters: the version in the metadata, the version the CLI prints,
and the version in the tag are the same number, checked by a machine.

Revisit this at 1.0, if releases become frequent enough that editing a line is
friction worth automating.
