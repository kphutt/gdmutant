# gdmutant — Conventions

A language-agnostic **mutation-testing** tool; the first *usable* one for GDScript/Godot.
This file is the AI/contributor entry point; `README.md` is the product rationale, and the
authoritative design lives in `docs/design/DESIGN.md` (the design gate — written next).

> **`gdmutant` is a provisional codename.** Clear it (registries + a trademark sense-check)
> before any public launch. It lives in one place: this repo's name + README.

## Status

**Bootstrapped — hardening, toolchain, and the docs spine are in place; the engine is not
built yet.** The next step is the `DESIGN.md` gate (reviewed), *then* the engine loop +
GDScript adapter. Do not build past the step the plan/ROADMAP is calling for.

## Design goals (do not lose these)

- **Ship fast.** A working v0.1 that mutates one real module and prints survivors beats a
  perfect framework.
- **Standalone, no AI required.** A normal CLI a developer installs and runs, exactly like
  Stryker. This is the #1 constraint: usable from the README alone, with no AI in the loop.
- **Generic engine, per-language adapters.** The loop (mutate → run → killed/survived →
  report) is language-neutral; only two bits are language-specific — mutating the AST and
  running that language's tests. Build the loop once; a new language = one small adapter.
- **Deterministic core.** The mutation-operator engine is reproducible — the mode a CI
  check can trust. Any future LLM-semantic mode stays *out* of that gate (nondeterministic).

## Tech stack (decided — do not re-litigate)

- **Language: Python 3.12.** The engine and the GDScript adapter are Python because the AST
  work rides on `gdtoolkit` (a Python GDScript parser/formatter). Full rationale +
  re-open trigger: `docs/decisions/0001`.
- **Deps: uv** with a hash-pinned `uv.lock` (`uv sync --frozen`); `pyproject.toml`, no
  executable `setup.py`. Toolchain pinned in `mise.toml` (Python + uv).
- **First test-runner adapter: GdUnit4** (machine-readable JUnit XML). Runs via
  `godot --headless` as a subprocess.
- **Report format:** Stryker's `mutation-testing-elements` JSON schema (renders in the
  existing HTML viewer for free).

## Cross-machine (developed on macOS + Windows)

- **One-command setup:** `mise install` (installs pinned Python + uv) → `uv sync --frozen`.
- **LF line endings everywhere** (`.gitattributes`) so diffs/hashes/secret-scans match.
- **No hardcoded OS paths;** nothing machine-specific committed (per-machine overrides go in
  the git-ignored `.claude/settings.local.json`).

## How changes land

- **Every change lands via a reviewed PR; `main` is protected.** The bootstrap commit is the
  one exception (you can't PR into an empty repo).
- **No AI co-author trailer** on commits/PRs (`.claude/settings.json` → `includeCoAuthoredBy:
  false`). Never add a `Co-Authored-By` line.
- **SHA-pin every GitHub Action to the commit**, never the tag; Dependabot bumps them.
- **CI gate:** `ruff` (lint + format) + `mypy` + `pytest` (coverage) + `pip-audit`, plus a
  gitleaks secret-scan. Green CI is required; the design/intent review is advisory.
- **Sensitive paths** (CI, scripts, toolchain, and — once they exist — the mutation-operator
  catalog + the GDScript adapter) are in `CODEOWNERS` and stay human-reviewed. The adapter is
  the real technical risk: a wrong mutant means a silently wrong survivor report.

## Docs — where things live

`docs/design/DESIGN.md` is the authoritative design (goals + FG/NF requirements +
architecture + build plan). Convention: `ROADMAP.md` (the backlog, big rocks),
`docs/decisions/NNNN-*.md` (**append-only** ADRs — never edited; supersede with a new record;
`ls` is the index), `docs/design/{initiative}/` for working notes. Live docs open with YAML
frontmatter (`type` / `status` / `created`); build only from `status: active`. `CREDITS.md`
logs every third-party library's license (keeps open-sourcing clean).

## Non-goals (v0.1 — do NOT build yet)

Coverage-gated mutant selection (the later #1 speedup), the HTML report, incremental/diff mode,
the optional LLM-semantic mutant mode, and any second-language adapter (TypeScript delegates to
Stryker, or is skipped). Finishing the GDScript path on a real fixture beats breadth.
