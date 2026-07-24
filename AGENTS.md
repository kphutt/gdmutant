# gdmutant — contributor & AI-assistant guide

A language-agnostic **mutation-testing** tool — the first *usable* one for GDScript/Godot. It
mutates a project's source (flip `>`↔`>=`, `and`↔`or`, bump a number, …), reruns the tests per
mutant, and reports **survivors** — lines a bug could live on that no test catches. This is the
fast-orientation guide for anyone (human or AI) working on the code; the product rationale is in
[`README.md`](README.md), and the authoritative design is in
[`docs/design/DESIGN.md`](docs/design/DESIGN.md).

> `gdmutant` is a provisional codename.

## Setup

gdmutant is a [uv](https://docs.astral.sh/uv/) project — install uv, then:

```sh
uv sync --frozen      # fetches the pinned Python (via .python-version) + locked deps into .venv
```

Run commands with `uv run`. Optional fully-pinned path via [mise](https://mise.jdx.dev) (pins uv
itself): `mise install`, then activate mise or prefix commands with `mise exec --`.

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

- **Language:** Python 3.12, managed with **uv** (hash-pinned `uv.lock`); toolchain pinned in
  `mise.toml`. Rationale for Python over GDScript:
  [`docs/decisions/0001`](docs/decisions/0001-write-the-engine-in-python-not-gdscript.md).
- **Runtime dependency:** [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit) — the
  GDScript parser the adapter mutates.
- **First test-runner adapter:** GdUnit4 (run via `godot --headless`, JUnit-XML output).
- **Report format:** Stryker's `mutation-testing-elements` JSON schema (renders in its HTML viewer).

## Conventions

- **`main` is protected; new behavior comes with tests** — this is a testing tool, so we hold
  ourselves to it.
- **CI gate:** `ruff` + `mypy` + `pytest` + `pip-audit`, plus a gitleaks secret scan. GitHub Actions
  are SHA-pinned (Dependabot bumps them).
- **Keep the engine language-neutral:** no GDScript-specific assumptions in `gdmutant/engine/`;
  language specifics live only in `gdmutant/adapters/<lang>/`.
- **The mutation-operator core is deterministic** — the reproducible mode a CI check can trust; any
  future LLM-semantic mode stays out of it.
- **Sensitive paths** (CI, scripts, toolchain, the mutation-operator catalog, and the GDScript
  adapter) are in `CODEOWNERS` and stay human-reviewed. The adapter is the real
  technical risk: a wrong mutant means a silently wrong survivor report.

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
- [`CREDITS.md`](CREDITS.md) — third-party licenses.

Live docs open with YAML frontmatter (`type` / `status` / `created`); build only from `status: active`.

## Non-goals (v0.1)

Coverage-gated mutant selection, the optional LLM-semantic mutant mode, and any second-language
adapter (TypeScript delegates to Stryker, or is skipped).
Finishing the GDScript path on a real fixture beats breadth.
