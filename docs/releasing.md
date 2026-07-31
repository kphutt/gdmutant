---
type: how-to
status: active
created: 2026-07-23
---

# Releasing gdmutant

How gdmutant's distributions reach PyPI. Publishing uses **Trusted Publishing (OIDC)** — no API
token is stored in the repo. Design and rationale:
[`docs/decisions/0010-pypi-trusted-publishing.md`](decisions/0010-pypi-trusted-publishing.md).

The workflow is [`.github/workflows/publish.yml`](../.github/workflows/publish.yml). It builds the
sdist + wheel with `uv build`, validates them with `twine check`, and uploads via
`pypa/gh-action-pypi-publish` using a short-lived OIDC credential.

## Prerequisite — the one-time manual seed (maintainer)
Every publish (dry-run or real) depends on a trusted publisher registered on each index. Registering
one is a web-login step only the account owner can do. Values (full table in the ADR):

- **https://pypi.org** and **https://test.pypi.org** -> Account settings -> Publishing -> add a
  GitHub publisher: Owner `kphutt`, Repository `gdmutant`, Workflow `publish.yml`, Environment `pypi`
  (on PyPI) / `testpypi` (on TestPyPI), PyPI Project Name `gdmutant`.
- Create the GitHub Environments `pypi` and `testpypi` under repo Settings -> Environments.

A publish that fails at the publish step with an OIDC-trust error means this registration is
missing or does not match the workflow, environment and repository it names.

## Dry-run -> TestPyPI (`workflow_dispatch`)
Rehearse the full OIDC + upload path against the throwaway index without cutting a release:

- GitHub -> **Actions** -> **Publish** -> **Run workflow** (on the branch/tag you want to build).
- Or from the CLI: `gh workflow run publish.yml`.

This runs the `build` job then `publish-testpypi` (uploading to `https://test.pypi.org/legacy/`).
Verify the result at https://test.pypi.org/p/gdmutant.

## Real release -> PyPI (`release: published`)
1. Set the release version in `pyproject.toml` (a separate change — see `CHANGELOG.md`). The tag
   must match it; `scripts/check_release_tag.py` fails the release if it doesn't.
2. Create and publish a **GitHub Release** (tag it, then Publish — a draft does not trigger the
   workflow; only a *published* release does).
3. Publishing the release runs the `build` job then `publish-pypi`, uploading to PyPI over OIDC.
4. Verify at https://pypi.org/p/gdmutant.

## Notes
- **Nothing is stored** — no token secret; the index mints a one-shot credential per upload.
- The build backend runs only in the low-privilege `build` job; the OIDC token is granted only to the
  publish jobs, each gated behind a GitHub Environment.
- To reproduce the build locally: `uv build` then `uv run --with twine twine check dist/*`.
