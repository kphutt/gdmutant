# gdmutant — contributor & AI-assistant guide

A language-agnostic **mutation-testing** tool for GDScript/Godot. It
mutates a project's source (flip `>`↔`>=`, `and`↔`or`, bump a number, …), reruns the tests per
mutant, and reports **survivors** — lines a bug could live on that no test catches. This is the
fast-orientation guide for anyone (human or AI) *contributing to* gdmutant's own source; the
product rationale is in [`README.md`](README.md), and the authoritative design is in
[`docs/design/DESIGN.md`](docs/design/DESIGN.md). Driving gdmutant as a tool — invoking the CLI
from an AI agent, not editing its code — is a different job: see
[`docs/using-with-an-ai-agent.md`](docs/using-with-an-ai-agent.md) instead.

## Setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project — install uv, then:

```sh
uv sync --frozen      # fetches the pinned Python (via .python-version) + locked deps into .venv
```

Run commands with `uv run`.

The live self-test suites below need a real Godot binary, which `uv sync` does not provide (`uv`
owns only Python here). `mise.toml` pins the same Godot release CI runs against — install
[mise](https://mise.jdx.dev/), then `mise install` fetches it; `mise which godot` prints the path
to pass as `GDMUTANT_GODOT`. Never add Python to `mise.toml` — that stays `uv`'s alone.

## Build · test

```sh
uv run ruff check .            # lint
uv run ruff format --check .   # format
uv run mypy gdmutant           # type check (strict)
uv run pytest                  # tests + coverage
uv run pip-audit               # dependency audit
```

Two suites are **env-gated** and auto-skip in a plain `uv run pytest` (so `verify` stays
Godot-free); run them when touching the adapter, runners, or CLI file-handling:

```sh
# Live self-test: drive the shipped CLI against a real Godot on the corpus.
GDMUTANT_GODOT=godot uv run pytest tests/test_selftest_live.py
# Dogfood harness: run gdmutant against a real GdUnit4 checkout — parse coverage + the
# whole-directory regression guard (Godot-free, ~5s). Point it at any GdUnit4 clone:
GDMUTANT_GDUNIT4_CLONE=<path-to-a-gdUnit4-checkout> uv run pytest tests/test_dogfood_gdunit4.py
```

## Tech stack (decided)

- **Language:** Python 3.12 (pinned via `.python-version`), managed with **uv** (hash-pinned
  `uv.lock`; uv itself floored in `pyproject.toml`'s `[tool.uv]`). Rationale for Python over GDScript:
  [`docs/decisions/0001`](docs/decisions/0001-write-the-engine-in-python-not-gdscript.md).
- **Runtime dependency:** [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit) — the
  GDScript parser the adapter mutates.
- **Test-runner adapters:** GdUnit4 and GUT are **peer JUnit-XML adapters** over one runner contract
  (both run via `godot --headless`; neither is privileged in the engine), plus the framework-neutral
  **exit-code command runner** for any harness without JUnit output. See
  [`docs/decisions/0011`](docs/decisions/0011-runner-agnostic-adapter-seam.md).
- **Report format:** the `mutation-testing-elements` JSON schema (renders in its HTML viewer).

## Conventions

- **`main` is protected; new behavior comes with tests** — this is a testing tool, so we hold
  ourselves to it.
- **CI gate:** `ruff` + `mypy` + `pytest` + `pip-audit`, plus a gitleaks secret scan. GitHub Actions
  are SHA-pinned (Dependabot bumps them).
- **Windows is a deployment target, not just a dev machine** — gdmutant is a cross-platform Python
  CLI people run on Windows, so test on Windows for real, not just Linux. Two concrete traps to
  watch for: console output can crash under the legacy `cp1252` code page, and `python3` can resolve
  to a *different* interpreter than `python` (see `.pre-commit-config.yaml`'s header for the guard).
- **`ci.yml` does not run automatically** (why: [ADR-0012](docs/decisions/0012-merge-time-local-ship-time-cloud.md)).
  The unbypassable gate is `publish.yml`'s release-time run of `verify` on both Linux and Windows,
  before every real release. Run the same checks locally any time:

  ```
  uv run python scripts/verify_local.py
  uv run python scripts/verify_local.py --job license-check   # or any other ci.yml job
  uv run python scripts/verify_local.py --list                # show a job's commands without running them
  ```

  This script reads its commands out of `ci.yml` rather than restating them, so local, CI, and the
  release gate can't drift apart. To restore `ci.yml` running automatically (worth doing once
  gdmutant is public), see ADR-0012's Decision section for the exact steps.

  **If every tool fails with `uv trampoline failed to canonicalize script path`,** the venv's
  launcher shims are stale — usually after a `uv` version bump. Fix: `uv sync --frozen --reinstall`.
- **Keep the engine language-neutral:** no GDScript-specific assumptions in `gdmutant/engine/`;
  language specifics live only in `gdmutant/adapters/<lang>/`.
- **The mutation-operator core is deterministic** — the reproducible mode a CI check can trust; any
  future LLM-semantic mode stays out of it.
- **Sensitive paths** — CI, scripts, toolchain, the mutation-operator catalog, and the GDScript
  adapter — are listed in `CODEOWNERS` for documentation only; it enforces no review (a sole
  maintainer can't approve their own PR). Nothing else mechanically blocks a bad change to them
  either: `main` requires a PR but no status checks, and merge-time checks are local discipline, not
  a gate (ADR-0012). Read changes to these paths carefully before merging — the GDScript adapter is
  the real technical risk, since a wrong mutant means a silently wrong survivor report.

## Design goals (keep these in mind)

- **Ship fast** — a working v0.1 that mutates one real module and prints survivors beats a framework.
- **Standalone, no AI required** — a normal CLI, usable from the README alone.
- **Generic engine, per-language adapters** — build the loop once; a new language = one small adapter.

## Docs — where things live

- [`README.md`](README.md) — what it is and why.
- [`docs/design/DESIGN.md`](docs/design/DESIGN.md) — authoritative design (goals, FG/NF requirements, architecture).
- [`CHANGELOG.md`](CHANGELOG.md) — what's landed and in progress (scope / non-goals live in `DESIGN.md`).
- `docs/mutation-testing.md` — the suite is mutation-tested against itself.
- `docs/decisions/NNNN-*.md` — append-only ADRs (`ls` is the index).
- [`docs/credits.md`](docs/credits.md) — third-party licenses.

Live docs open with YAML frontmatter (`type` / `status` / `created`); build only from `status: active`.

## Non-goals (v0.1)

Coverage-gated mutant selection, the optional LLM-semantic mutant mode, and any second-language
adapter (TypeScript is out of scope for v0.1).
Finishing the GDScript path on a real fixture beats breadth.
