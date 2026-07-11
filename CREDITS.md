# Credits & third-party licenses

Every third-party library shipped or adapted here is logged with its license, verified
permissive at the time of use. This keeps open-sourcing (and any future dual-licensing)
clean. **Re-verify each license at the moment of use** — licenses and repos change.

## Runtime dependencies (shipped)
| Library | Use | License | Source | Verified |
|---|---|---|---|---|
| gdtoolkit | The GDScript parser/formatter the adapter mutates | MIT | https://github.com/Scony/godot-gdscript-toolkit | 2026-07-10 |
| lark | Parser toolkit whose `Token`/`Tree` types the adapter uses directly (also gdtoolkit's parser) | MIT | https://github.com/lark-parser/lark | 2026-07-11 |

## Interoperability / formats
| Item | Use | License | Notes |
|---|---|---|---|
| `mutation-testing-elements` report schema (Stryker) | Report output format (renders in the shared HTML viewer) | Apache-2.0 | Adopting the schema *shape* is interoperability. If any schema files are ever vendored verbatim, add the Apache-2.0 NOTICE + attribution. |

## Build / dev tools (not shipped, recorded for completeness)
| Tool | Use | License |
|---|---|---|
| Godot Engine | Runs the GDScript test suites headlessly | MIT |
| Python | Runtime | PSF |
| uv | Dependency + environment manager | Apache-2.0 / MIT |
| ruff · mypy · pytest · pip-audit | Lint / typecheck / test / audit | MIT / MIT / MIT / Apache-2.0 |

## Prior art (studied for ideas only — no code copied)
- **hanse7962/GodotMutationTesting** — **unlicensed** (all rights reserved). Studied for
  ideas only; no code used or adapted.
- **mutmut** (BSD-3), **Stryker** / **PIT** (Apache-2.0), **infection** (BSD-3),
  **cargo-mutants** (MIT) — architecture and patterns studied. Patterns aren't copyrightable,
  and these licenses permit adaptation with attribution; none of their code is copied here.
