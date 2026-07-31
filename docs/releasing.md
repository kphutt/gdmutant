---
type: how-to
status: active
created: 2026-07-23
---

# Releasing gdmutant

How gdmutant's distributions reach PyPI. Publishing uses **Trusted Publishing (OIDC)** — no API
token is stored in the repo. Design and rationale:
[`docs/decisions/0010-pypi-trusted-publishing.md`](decisions/0010-pypi-trusted-publishing.md).

Two workflows split the work, with a human standing between them:

- [`.github/workflows/release.yml`](../.github/workflows/release.yml) triggers on a pushed version
  tag (`v*.*.*`). It checks the tag against the version in `pyproject.toml` and the tagged commit
  against `main`, then creates a **draft** GitHub Release with auto-generated notes. It never creates
  or moves a tag (`gh release create --verify-tag` fails if the tag is missing), and it uploads
  nothing.
- [`.github/workflows/publish.yml`](../.github/workflows/publish.yml) triggers on a **published**
  Release. It builds the sdist + wheel with `uv build`, validates them with `twine check`, runs the
  full release gate, and uploads via `pypa/gh-action-pypi-publish` using a short-lived OIDC
  credential.

A draft Release does not fire `release: published`, so **pushing a tag stages a release and stops
there**. The upload waits for a maintainer to open the draft on GitHub and press **Publish** — the
deliberate, auditable human act ADR-0010 gates a real upload on.

## Prerequisite — the one-time manual seed (maintainer)
Every publish (dry-run or real) depends on a trusted publisher registered on each index. Registering
one is a web-login step only the account owner can do. Values (full table in the ADR):

- **https://pypi.org** and **https://test.pypi.org** -> Account settings -> Publishing -> add a
  GitHub publisher: Owner `kphutt`, Repository `gdmutant`, Workflow `publish.yml`, Environment `pypi`
  (on PyPI) / `testpypi` (on TestPyPI), PyPI Project Name `gdmutant`.
- Create the GitHub Environments `pypi` and `testpypi` under repo Settings -> Environments.

A publish that fails at the publish step with an OIDC-trust error means this registration is
missing or does not match the workflow, environment and repository it names.

### The publisher moves after an index's first upload
An index that has never received a `gdmutant` upload has no project to hang a publisher on, so the
registration above is a **pending** publisher, held against the *account*. The first successful
upload creates the project and [converts the pending publisher into a normal
one](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/), which then lives
under the **project's** Settings -> Publishing and is gone from the account-level pending list.

That relocation is the trap. After a release the account page that used to list the publisher is
empty, and adding a pending publisher there again does nothing, because the project exists now.
Look at the *project's* publishing settings instead, and confirm the entry is there. If it isn't,
add it there with the same five values — otherwise the *next* release fails at the OIDC step with a
trust error whose real cause, an earlier upload that succeeded, appears nowhere in the log.

## Dry-run -> TestPyPI (`workflow_dispatch`)
Rehearse the full OIDC + upload path against the throwaway index without cutting a release:

- GitHub -> **Actions** -> **Publish** -> **Run workflow** (on the branch/tag you want to build).
- Or from the CLI: `gh workflow run publish.yml`.

This runs the `build` job then `publish-testpypi` (uploading to `https://test.pypi.org/legacy/`).
Verify the result at https://test.pypi.org/p/gdmutant.

## Real release -> PyPI (push a tag, then press Publish)

1. **Set the version** in `pyproject.toml`. The tag must match it exactly;
   `scripts/check_release_tag.py` fails the release if it doesn't.
2. **Date the changelog.** Change this version's heading in `CHANGELOG.md` from
   `## [X.Y.Z] — unreleased` to `## [X.Y.Z] — YYYY-MM-DD`, using the date you expect to publish.
   Nothing automates this and no check enforces it, and it has to happen before the tag: the tag
   ships the commit it points at, so tagging first publishes a changelog that calls the shipped
   version unreleased.
3. **Merge both to `main`** through the usual PR. A tag whose commit is not an ancestor of `main` is
   refused.
4. **Push the tag**: `git tag vX.Y.Z <commit>` then `git push origin vX.Y.Z`. Get it right the first
   time — the repo's tag ruleset blocks deleting and re-pointing tags, so a tag naming the wrong
   version or the wrong commit cannot be fixed in place, and the recovery is to burn the version
   number and cut a new one.
5. *Automatic.* `release.yml` runs its two guards — the tag matches the packaged version, and the
   tagged commit is on `main` — and then stages a **draft** Release with generated notes. A guard
   that fails leaves no Release at all, so nothing has shipped.
6. **Review the draft on GitHub and press Publish.** The generated notes are a raw commit list; edit
   them into something worth reading, with this version's `CHANGELOG.md` entry as the source.
   Publishing is what fires `publish.yml`, and only then does anything reach PyPI.
7. *Automatic.* `publish.yml` re-runs the two provenance guards and adds five more — a full-history
   secret scan, `verify` on Linux *and* Windows, the license gate, and both Godot self-tests — every
   one of them before the upload job is granted its OIDC token ([ADR-0012](decisions/0012-merge-time-local-ship-time-cloud.md)).
   The Godot legs make this a slow run, not a quick one. A guard that fails stops the upload while
   leaving the Release published; fix the cause and re-run the failed jobs from the Actions tab
   (a re-run replays the same `release: published` event), or cut a new version if the fix needs a
   code change.
8. Verify at https://pypi.org/p/gdmutant.

## Notes
- **Nothing is stored** — no token secret; the index mints a one-shot credential per upload.
- The build backend runs only in the low-privilege `build` job; the OIDC token is granted only to the
  publish jobs, each gated behind a GitHub Environment.
- Creating *and* publishing a Release straight from the GitHub web UI skips `release.yml` entirely.
  `publish.yml`'s gate still runs, which is why it repeats the provenance guards rather than trusting
  that the tag path already ran them.
- To reproduce the build locally: `uv build` then `uv run --with twine twine check dist/*`.
