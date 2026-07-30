---
type: decision
status: active
created: 2026-07-10
---

# Write the engine (and GDScript adapter) in Python, not GDScript

## Status
Accepted

## Context
gdmutant is a mutation-testing tool whose first target language is GDScript. The obvious
question — asked deliberately, not assumed away — is whether to write the tool *in* GDScript,
since **self-hosting is the norm** for mutation testers: StrykerJS is JS-for-JS, mutmut is
Python-for-Python, infection is PHP-for-PHP. A GDScript-for-GDScript tool would also carry
real ecosystem advantages (below), so it earned a genuine hearing.

Every self-hosting mutation tool, though, relies on one thing: a **native, driveable AST** it
can parse → mutate → re-emit. That is the expensive core of any mutation tester. The facts:

- **The parser only exists in Python.** [`gdtoolkit`](https://github.com/Scony/godot-gdscript-toolkit)
  ships a real GDScript parser + formatter as a **Python** library — the asset that makes this
  project cheap to build *now* (this repo's premise: "the AST work is nearly free"). Godot
  exposes **no** stable, public *parse-arbitrary-source → mutable-tree → unparse* API from
  within GDScript (its real parser is C++, internal to the editor). So a GDScript
  implementation would have to either (a) reimplement a GDScript parser from scratch — deleting
  the "AST is nearly free" premise the whole project rests on — or (b) shell out to gdtoolkit
  (Python) for parsing and marshal trees back into GDScript: two runtimes plus a boundary, the
  worst of both.
- **The goal is a language-*neutral* engine with per-language adapters.** A generic
  mutation engine that must orchestrate `pytest`, JSON reports, and eventually *delegate
  TypeScript to Stryker* is a poor fit for a game-scripting language and a normal fit for Python.
- **It ships as a standalone dev CLI.** `pipx install gdmutant`, PyPI, argparse/subprocess/JSON,
  and `pytest` for the engine's own tests are all mature in Python; standalone GDScript-as-CLI
  (`godot --headless --script`) is awkward to package and distribute.

The honest pulls *toward* GDScript, recorded so they are not forgotten: no second runtime for a
Godot dev (they already have Godot), a native in-process test runner, and AssetLib distribution.

## Decision
- **Write the engine and the GDScript adapter in Python.** Mutation is a direct `gdtoolkit`
  library call. Stack: **Python 3.12**, **uv** (hash-pinned `uv.lock`, `uv sync --frozen`),
  `pyproject.toml` (no executable `setup.py`); toolchain pinned via `uv` (`uv.lock`,
  `.python-version`).
- **The adapter runs the tests as a subprocess** — `godot --headless` + GdUnit4 (the chosen
  first test-runner) — parsing its machine-readable output. Godot is an unavoidable prerequisite in *any* design,
  because it is what runs the tests; only the orchestration + mutation lives in Python.
- **Keep the engine language-neutral.** No GDScript-specific assumptions leak into `engine/`;
  language specifics live only in `adapters/gdscript/`.

## Consequences
- **The one real cost: a second runtime for Godot devs.** Mitigation — keep prerequisites to
  exactly *"Godot (already installed) + `pipx install gdmutant`"*: one command, no venv juggling.
- Self-hosting's usual payoff (contributors write in the language they mutate) is forgone; we
  accept it because the parser that makes self-hosting possible elsewhere does not exist on the
  GDScript side.
- **What would flip this decision (the re-open trigger):** Godot shipping a public, driveable
  GDScript AST (parse → mutate → unparse), *or* a mature native-GDScript parser landing. Either
  makes a GDScript rewrite for tighter ecosystem fit worth revisiting. The adapter spike will
  sanity-check for any new Godot 4.x parser surface before the adapter shape is finalized; absent
  that, `gdtoolkit` (Python) is the known-good path.

Supersede this record (do not edit it) if the trigger fires.
This tool is the standalone realization of the "custom, gdtoolkit-AST-based mutation
harness" idea first sketched while planning a private Godot project — the game this
project was extracted from.
