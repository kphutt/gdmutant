# gdmutant — contributor & AI-assistant guide

A language-agnostic **mutation-testing** tool — the first *usable* one for GDScript/Godot. It
mutates a project's source (flip `>`↔`>=`, `and`↔`or`, bump a number, …), reruns the tests per
mutant, and reports **survivors** — lines a bug could live on that no test catches. This is the
fast-orientation guide for anyone (human or AI) working on the code; the product rationale is in
[`README.md`](README.md), and the authoritative design is in
[`docs/design/DESIGN.md`](docs/design/DESIGN.md).

> `gdmutant` is a provisional codename, not yet cleared for public use.

## Status

v0.1 built — the language-neutral engine (mutate → run → tally → score → report), the GDScript
adapter, the GdUnit4 runner, the Stryker reporter, and the `gdmutant run` CLI are all in and tested
(and the suite is mutation-tested against itself, see `docs/mutation-testing.md`). Two things remain
before a public launch (see [`ROADMAP.md`](ROADMAP.md)): **live CI validation** of the
`godot --headless` + GdUnit4 path, and the **statement-deletion operator** (the last DESIGN.md FG-2.1
mutation). The package stays version `0.0.0` until both land, then tags `0.1.0`.

## Setup

The toolchain is pinned, so setup is one command:

```sh
mise install          # installs the pinned Python + uv
uv sync --frozen      # installs the exact locked dependencies into .venv
```

Prefer not to use mise? Install [uv](https://docs.astral.sh/uv/) yourself, then `uv sync --frozen`.

## Build · test

```sh
uv run ruff check .            # lint
uv run ruff format --check .   # format
uv run mypy gdmutant           # type check (strict)
uv run pytest                  # tests + coverage
uv run pip-audit               # dependency audit
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

- **Every change lands via a reviewed PR;** `main` is protected. New behavior comes with tests —
  this is a testing tool, so we hold ourselves to it.
- **No AI co-author trailer** on commits/PRs.
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
- [`ROADMAP.md`](ROADMAP.md) — the backlog.
- `docs/decisions/NNNN-*.md` — append-only ADRs (`ls` is the index).
- [`CREDITS.md`](CREDITS.md) — third-party licenses.

Live docs open with YAML frontmatter (`type` / `status` / `created`); build only from `status: active`.

## Non-goals (v0.1)

Coverage-gated mutant selection, the HTML report, incremental/diff mode, the optional LLM-semantic
mutant mode, and any second-language adapter (TypeScript delegates to Stryker, or is skipped).
Finishing the GDScript path on a real fixture beats breadth.
