"""Packaging-hygiene guards (LOD-102).

These assert the distribution *metadata* stays correct without building an artifact: the PEP 561
`py.typed` marker ships, the strict-typing signal is declared, and the sdist keeps dev/CI/machine
cruft out — especially local agent state (`.claude/`), which hatchling bundles into the sdist even
though it's gitignored, so only an explicit exclude keeps it out of a release.
"""

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_py_typed_marker_ships() -> None:
    # PEP 561: downstream type-checkers only honour our types if this marker is in the package.
    assert (_ROOT / "gdmutant" / "py.typed").is_file()


def test_typed_classifier_declared() -> None:
    # The marker and the `Typing :: Typed` classifier must agree — one without the other misleads.
    assert "Typing :: Typed" in _PYPROJECT["project"]["classifiers"]


def test_sdist_trims_dev_and_agent_cruft() -> None:
    # The sdist must exclude local agent state and CI/dev config. `.claude/` is the load-bearing
    # one: it's gitignored yet hatchling would still bundle it, so the explicit exclude is the only
    # thing keeping local agent settings out of a published release.
    excluded = _PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]
    for cruft in ("/.claude", "/.github", "/scripts"):
        assert cruft in excluded, f"{cruft} must stay excluded from the sdist"
