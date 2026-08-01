---
type: record
status: active
created: 2026-07-10
---

# Credits & third-party licenses

Every third-party library shipped or adapted here is logged with its license, verified
permissive at the time of use. This keeps open-sourcing (and any future dual-licensing)
clean. Re-verify each license at the moment of use. Licenses and repos change.

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

Scope here is anything the repository invokes, not only what `pyproject.toml` declares. A tool
pinned in a workflow or a hook is as real a dependency as a locked one, which is why the external
binaries and the `uvx` / `uv run --with` invocations are listed alongside the declared packages.

| Tool | Use | License |
|---|---|---|
| Godot Engine | Runs the GDScript test suites headlessly | MIT |
| [GdUnit4](https://github.com/godot-gdunit-labs/gdUnit4) (v6.1.3) | GDScript test framework. Its addon is downloaded (not vendored) into the corpus fixture for the live self-test (`scripts/install_gdunit4.py`) | MIT |
| [GUT](https://github.com/bitwes/Gut) (v9.7.1) | GDScript test framework (peer JUnit adapter). Its addon is downloaded, not vendored, into the corpus fixture for the live self-test (`scripts/install_gut.py`) | MIT |
| Python | Runtime | PSF |
| uv | Dependency + environment manager | Apache-2.0 / MIT |
| [mise](https://mise.jdx.dev) | Pins the Godot release the live self-test runs against (`mise.toml`) | MIT |
| [hatchling](https://github.com/pypa/hatch/tree/master/backend) | Build backend that produces the wheel and sdist | MIT |
| [hatch-fancy-pypi-readme](https://github.com/hynek/hatch-fancy-pypi-readme) | Build-backend metadata hook (`build-system.requires`) that rewrites the README banner into a tag-pinned absolute URL in every built distribution, which is what `publish.yml`'s long-description image check exists to verify | MIT |
| [twine](https://github.com/pypa/twine) | Validates the built distributions' metadata before the upload (`twine check`, via `uv run --with`) | Apache-2.0 |
| [zizmor](https://docs.zizmor.sh) | Static-analysis security lint for the GitHub Actions workflows (`zizmor.yml`, via `uvx` at a pinned version) | MIT |
| ruff · mypy · pytest · pip-audit | Lint / typecheck / test / audit | MIT / MIT / MIT / Apache-2.0 |
| [pytest-cov](https://github.com/pytest-dev/pytest-cov) | Coverage measurement during `pytest`, and the 100% line+branch floor | MIT |
| [mutmut](https://github.com/boxed/mutmut) | Mutation-tests gdmutant's own Python suite (the dogfood check) | BSD-3-Clause |
| [poodle](https://github.com/WiredNerd/poodle) | Mutation-tests the class-method bodies mutmut 3.x does not reach (`poodle.toml`) | MIT |
| [pip-licenses](https://github.com/raimon49/pip-licenses) | Reads dependency license metadata for the license gate (`scripts/check_licenses.py`) | MIT |
| [pre-commit](https://pre-commit.com) | Manages the local git hooks that run the checks in `.pre-commit-config.yaml` | MIT |
| gitleaks | Secret scan, local (pre-commit stage) and in CI | MIT |

## Prior art (studied for ideas only, no code copied)
- hanse7962/GodotMutationTesting: unlicensed (all rights reserved). Studied for
  ideas only, no code used or adapted.
- mutmut (BSD-3), Stryker / PIT (Apache-2.0), infection (BSD-3),
  cargo-mutants (MIT): architecture and patterns studied. Patterns aren't copyrightable,
  and these licenses permit adaptation with attribution. None of their code is copied here.

  mutmut appears twice on purpose. It is listed here for the ideas studied, and in the
  build/dev tools table above because gdmutant also *runs* it as a dev dependency. Neither
  entry implies any of its code was copied.

## Adapted documents
| Document | Source | License |
|---|---|---|
| [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) | [Contributor Covenant 3.0](https://www.contributor-covenant.org/version/3/0/) (reporting and enforcement sections filled in for this project, as that version instructs) | CC BY-SA 4.0 |
