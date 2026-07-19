"""Packaging-hygiene guards ([ticket]).

These assert the distribution *metadata* stays correct without building an artifact: the PEP 561
`py.typed` marker ships, the strict-typing signal is declared, and the sdist ships from an explicit
allowlist so no local, untracked, or dot-prefixed state (agent/editor dirs, CI config) can leak
into a release.
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


def test_sdist_ships_from_an_allowlist_that_omits_local_and_ci_state() -> None:
    # The sdist ships an explicit allowlist, so nothing untracked or dot-prefixed can leak into a
    # release. Assert the load-bearing paths are shipped and that no dot-dir (agent/editor state) or
    # CI/dev tooling directory is in the list — an allowlist naming none of them is the robust,
    # tool-agnostic fix for hatchling otherwise bundling local cruft.
    include = _PYPROJECT["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    for shipped in ("/gdmutant", "/tests", "/docs", "/pyproject.toml"):
        assert shipped in include, f"{shipped} must ship in the sdist"
    for entry in include:
        base = entry.lstrip("/")
        assert not base.startswith("."), f"no dot-prefixed state should be shipped: {entry}"
        assert base not in ("scripts", "github"), f"CI/dev tooling must not ship: {entry}"
