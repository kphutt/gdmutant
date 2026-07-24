# PyPI publishing via Trusted Publishing (OIDC), not a stored API token

## Status
Accepted

## Context
gdmutant ships a standalone CLI (`gdmutant = "gdmutant.cli:main"`) and is packaged as a normal
Python distribution (sdist + wheel via hatchling). To let anyone `pip install gdmutant`, the built
distributions have to be uploaded to the Python Package Index. The repo is private today and will go
public before the first tagged release; this ADR records the publishing mechanism as launch-prep,
ahead of any actual upload.

There are two ways to authenticate an upload to PyPI:

- **(a) A long-lived API token** stored as a GitHub Actions secret. Simple, but it is a standing
  bearer credential: anyone (or any compromised workflow/action) that can read the secret can publish
  as us, indefinitely, until it is manually rotated. It is exactly the kind of long-lived secret the
  maintainer's supply-chain baseline exists to avoid, and it must be seeded, stored, and rotated by
  hand.
- **(b) Trusted Publishing (OIDC).** PyPI and TestPyPI implement the PyPA "trusted publisher" model:
  the index is told, once, to trust a specific *repository + workflow filename + environment*. At
  publish time GitHub Actions mints a short-lived OIDC token for that exact identity, the index
  verifies it and issues a one-shot, minutes-long API token scoped to that upload. **No secret is ever
  stored in the repo**, nothing to rotate, and a leaked workflow log contains no reusable credential.

## Decision
Adopt **Trusted Publishing (OIDC)** via `.github/workflows/publish.yml`, using
`pypa/gh-action-pypi-publish`. No PyPI/TestPyPI API token is stored as a repo or org secret.

**Trigger design (two paths in one workflow):**
- **`workflow_dispatch` -> TestPyPI** — the deliberate dry-run. Fired by hand from the Actions tab;
  builds and publishes to `https://test.pypi.org/legacy/`. Lets us rehearse the real thing against the
  throwaway index without cutting a release.
- **`release: types: [published]` -> PyPI** — a real release. Publishing a GitHub Release runs the
  PyPI path. Tag/release management stays the human, auditable act that gates a real upload.

**Least-privilege shape:**
- Workflow-level `permissions: contents: read`. The **build** job (which runs the build backend's
  code via `uv build`) inherits only that — it has no OIDC.
- `id-token: write` is granted **only** on the two publish jobs, and each publish job is bound to a
  GitHub **Environment** (`testpypi` / `pypi`) so it can be protection-gated (required reviewers,
  branch/tag constraints) from the repo settings.
- Build once, publish from an artifact: the OIDC-bearing publish jobs do nothing but download the
  vetted `dist/` artifact and upload it — the build backend never executes in a job that can mint a
  credential.
- Every action is SHA-pinned to a full commit with a `# vX.Y.Z` comment (repo convention; Dependabot
  bumps them).

## The one-time manual seed (maintainer only — required before either path works)
Trusted Publishing requires a *pending publisher* to be registered on each index **before** the first
upload. This is a web-login action only the account owner can do; it is the single irreducible manual
step. Register the **same** identity on both indexes:

On **https://pypi.org** and **https://test.pypi.org** -> Account settings -> **Publishing** ->
"Add a new pending publisher" (GitHub) with these exact values:

| Field               | Value on PyPI        | Value on TestPyPI    |
|---------------------|----------------------|----------------------|
| PyPI Project Name   | `gdmutant`           | `gdmutant`           |
| Owner               | `kphutt`             | `kphutt`             |
| Repository name     | `gdmutant`           | `gdmutant`           |
| Workflow filename   | `publish.yml`        | `publish.yml`        |
| Environment name    | `pypi`               | `testpypi`           |

Note the environment name is the only field that differs between the two indexes — it must match the
`environment.name` of the corresponding publish job in `publish.yml`. Also create the two GitHub
Environments (`pypi`, `testpypi`) under repo Settings -> Environments so they exist to be gated.

Because the project does not yet exist on either index, these are *pending* publishers: the first
successful upload creates the project and converts the pending publisher into a normal one.

## Consequences
- **No long-lived publishing secret in the repo, ever** — nothing to leak or rotate; a compromised
  workflow log yields no reusable credential.
- **The seed is a hard prerequisite.** Until the maintainer registers the pending publishers (above),
  both the dry-run and the real release will fail at the publish step with an OIDC-trust error. This
  is by design — the trust is established out-of-band, on the index, by a human.
- **TestPyPI is a genuine rehearsal.** `workflow_dispatch` exercises the real OIDC + upload path
  against the throwaway index, so the first real PyPI release is not the first time the mechanism runs
  end-to-end.
- **Environments enable future gating.** Required reviewers or a tag filter can be added to the `pypi`
  environment later without touching the workflow.
- **Version management is out of scope here.** This ADR is the publishing *mechanism*; the
  `version` field in `pyproject.toml` stays `0.0.0` until a separate version-cut, and the workflow
  publishes whatever version the built distribution declares.
