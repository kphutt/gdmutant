---
type: how-to
status: active
created: 2026-07-11
---

# gdmutant: contributor & AI-assistant guide

A language-agnostic mutation-testing tool for GDScript/Godot. It
mutates a project's source (flip `>`↔`>=`, `and`↔`or`, bump a number, …), reruns the tests per
mutant, and reports survivors: lines a bug could live on that no test catches. This is the
fast-orientation guide for anyone (human or AI) *contributing to* gdmutant's own source. The
product rationale is in [`README.md`](README.md), and the authoritative design is in
[`docs/design/DESIGN.md`](docs/design/DESIGN.md). Driving gdmutant as a tool (invoking the CLI,
not editing its code) is a different job: see
[`docs/gdmutant-guide.md`](docs/gdmutant-guide.md) instead.

## Setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project. Install uv, then:

```sh
uv sync --frozen      # fetches the pinned Python (via .python-version) + locked deps into .venv
```

Run commands with `uv run`.

The live self-test suites below need a real Godot binary, which `uv sync` does not provide (`uv`
owns only Python here). `mise.toml` pins the same Godot release CI runs against. Install
[mise](https://mise.jdx.dev/), then `mise install` fetches it. `mise which godot` prints the path
to pass as `GDMUTANT_GODOT`. Never add Python to `mise.toml`. That stays `uv`'s alone.

## Build · test

```sh
uv run ruff check .            # lint
uv run ruff format --check .   # format
uv run mypy gdmutant           # type check (strict)
uv run pytest                  # tests + coverage
uv run pip-audit               # dependency audit
```

`scripts/dev.py` groups the everyday subset of those into one discoverable command, for the fast
inner loop while you're still writing a change (a dispatcher over the same commands above, so it
can't drift from them):

```sh
uv run python scripts/dev.py lint    # ruff check + ruff format --check + mypy
uv run python scripts/dev.py test    # pytest
uv run python scripts/dev.py build   # uv build
```

It is not a replacement for `scripts/verify_local.py` below, which is the one command that mirrors
the full CI `verify` job and is what you run before opening a pull request.

Two suites are env-gated and auto-skip in a plain `uv run pytest` (so `verify` stays
Godot-free). Run them when touching the adapter, runners, or CLI file-handling:

```sh
# Live self-test: drive the shipped CLI against a real Godot on the corpus.
# Run `mise install` first: an unset or empty GDMUTANT_GODOT skips this whole file silently.
GDMUTANT_GODOT=$(mise which godot) uv run pytest tests/test_selftest_live.py
# Dogfood harness: run gdmutant against a real GdUnit4 checkout. Two checks: parse coverage,
# and the whole-directory regression guard (Godot-free, ~5s). Point it at any GdUnit4 clone:
GDMUTANT_GDUNIT4_CLONE=<path-to-a-gdUnit4-checkout> uv run pytest tests/test_dogfood_gdunit4.py
```

## Tech stack (decided)

- Language: Python 3.12 (pinned via `.python-version`), managed with uv (hash-pinned
  `uv.lock`, with uv itself floored in `pyproject.toml`'s `[tool.uv]`). Rationale for Python over GDScript:
  [`docs/decisions/0001`](docs/decisions/0001-write-the-engine-in-python-not-gdscript.md).
- Runtime dependency: [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit), the
  GDScript parser the adapter mutates.
- Test-runner adapters: GdUnit4 and GUT are peer JUnit-XML adapters over one runner contract
  (both run via `godot --headless`, and neither is privileged in the engine), plus the framework-neutral
  exit-code command runner for any harness without JUnit output. See
  [`docs/decisions/0011`](docs/decisions/0011-runner-agnostic-adapter-seam.md).
- Report format: the `mutation-testing-elements` JSON schema (renders in its HTML viewer).

## Conventions

- `main` is protected, and new behavior comes with tests: this is a testing tool, so we hold
  ourselves to it.
- CI gate: `ruff` + `mypy` + `pytest` + `pip-audit`, plus a gitleaks secret scan and zizmor
  (workflow security). GitHub Actions are SHA-pinned (Dependabot bumps them).
- Windows is a deployment target, not just a dev machine. gdmutant is a cross-platform Python
  CLI people run on Windows, so test on Windows for real, not just Linux. Two concrete traps to
  watch for: console output can crash under the legacy `cp1252` code page, and `python3` can resolve
  to a *different* interpreter than `python` (see `.pre-commit-config.yaml`'s header for the guard).
- `ci.yml` runs automatically on every pull request and push to `main`, restored 2026-08-04 ahead
  of going public. [ADR-0012](docs/decisions/0012-merge-time-local-ship-time-cloud.md) covers why
  it was ever manual-only, and why it's back. The unbypassable gate is
  still `publish.yml`'s release-time run of `verify` on both Linux and Windows, before every real
  release. Run the same checks locally any time:

  ```
  uv run python scripts/verify_local.py
  uv run python scripts/verify_local.py --job license-check   # or any other ci.yml job
  uv run python scripts/verify_local.py --list                # show a job's commands without running them
  ```

  This script reads its commands out of `ci.yml` rather than restating them, so local, CI, and the
  release gate can't drift apart.

  If every tool fails with `uv trampoline failed to canonicalize script path`, the venv's
  launcher shims are stale, usually after a `uv` version bump. Fix: `uv sync --frozen --reinstall`.
- Keep the engine language-neutral: no GDScript-specific assumptions in `gdmutant/engine/`.
  Language specifics live only in `gdmutant/adapters/<lang>/`. One narrow, deliberate exception:
  `CommandRunner` (docs/decisions/0005) also checks its output for a GDScript-specific marker
  string (docs/decisions/0015). That check costs nothing for a non-GDScript command (the string
  never appears), so it was kept there rather than behind a new adapter-level runner and CLI flag.
- The mutation-operator core is deterministic, the reproducible mode a CI check can trust. Any
  future LLM-semantic mode stays out of it.
- **Recurring bug one: a gate that passes without checking anything.** Seen five times. A test that
  skipped when `git` was missing. A mutation hook that ran zero mutants. A license check over an
  empty package list. A script that exited 0 after all its writes failed. A mutation run scored
  against an already-broken baseline, where every mutant "died" so the 100% meant nothing.
  This is a list of shapes, not a list of things already fixed. Assume at least one is live in the
  tree right now, because that has been true every time anyone checked.
  Ask of any gate: what happens when its input is missing, empty, or unreadable? Silence is the
  wrong answer. Make it fail, or make it say out loud that it did not run.
- **Recurring bug two: two paths that should agree, and one checks less.** A second entry point
  running fewer checks than the first. A message fixed in one place but not its twin. Two things
  writing to stdout at once. Coverage and mutation testing cannot catch these. Both ask "is this
  path correct?", and the bug is "do these two paths match?". So when you change one of a pair, say
  what every member of the pair does now, including the ones already right.
- Sensitive paths (CI, scripts, toolchain, the mutation-operator catalog, and the GDScript
  adapter) are listed in `CODEOWNERS` for documentation only. It enforces no review (a sole
  maintainer can't approve their own PR). `main` requires a pull request and, alongside
  `Workflow security (zizmor)` (which reads the workflow files and nothing else), the checks
  `ci.yml`'s now-restored triggers make possible: `Verify` on both platforms, `Secret scan
  (gitleaks)`, and both `Self-test` gates. See `scripts/harden_github.py`'s `REQUIRED_JOBS` for
  the exact, current list of workflow-derived checks. Socket Security (a GitHub App, not a
  workflow job) adds two more, listed in that same script's `REQUIRED_APP_CHECKS`.
  `.github/CODEOWNERS` carries the full list of what branch protection enforces. Read changes to
  these paths carefully before merging. The GDScript adapter is the real technical risk, since a
  wrong mutant means a silently wrong survivor report.

## Design goals (keep these in mind)

- Ship fast: a working v0.1 that mutates one real module and prints survivors beats a framework.
- Standalone, no AI required: a normal CLI, usable from the README alone.
- Generic engine, per-language adapters: build the loop once. A new language = one small adapter.

## Docs: where things live

- [`README.md`](README.md): what it is and why.
- [`docs/design/DESIGN.md`](docs/design/DESIGN.md): authoritative design (goals, FG/NF requirements, architecture).
- [`CHANGELOG.md`](CHANGELOG.md): what's landed and in progress (scope / non-goals live in `DESIGN.md`).
- `docs/mutation-testing.md`: the suite is mutation-tested against itself.
- `docs/decisions/NNNN-*.md`: append-only ADRs (`ls` is the index).
- [`docs/releasing.md`](docs/releasing.md): the maintainer runbook for cutting a release to PyPI.
- [`docs/credits.md`](docs/credits.md): third-party licenses.

Live docs open with YAML frontmatter (`type` / `status` / `created`). Build only from
`status: active`. Two files are deliberately exempt, because they are rendered somewhere that has
no frontmatter support and would print it as a visible heading: `README.md` (it is the package's
PyPI description) and `.github/PULL_REQUEST_TEMPLATE.md` (it is pasted verbatim into every new pull
request's body). `tests/test_docs_frontmatter.py` pins both halves of that rule.

## Non-goals (v0.1)

Coverage-gated mutant selection, the optional LLM-semantic mutant mode, and any second-language
adapter (TypeScript is out of scope for v0.1).
Finishing the GDScript path on a real fixture beats breadth.
