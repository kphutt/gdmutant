"""The distributed action must never advertise a `@v1`-style floating tag.

`action.yml` makes gdmutant consumable as a GitHub Action, and its header comment plus the CLI
reference's "GitHub Actions" section are where a consumer copies the `uses:` line from. Both used
to be able to drift toward the convenient-looking `kphutt/gdmutant@v1`, which this repo cannot
produce:

* `scripts/check_release_tag.py` fails any tag that does not equal the version in `pyproject.toml`,
  so a `v1` tag would demand a packaged version of literally `1` — asserted below rather than
  described, so the reason stays true if the guard changes.
* The tag ruleset on the repo blocks deletion and non-fast-forward updates on every ref, so an
  existing tag cannot be moved to a later commit either.

Consumers therefore pin a commit SHA (or a full `vX.Y.Z` tag) and take bumps from Dependabot. This
test keeps the shipped copy honest about that.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

_SCRIPT = REPO / "scripts" / "check_release_tag.py"
_spec = importlib.util.spec_from_file_location("check_release_tag_for_pin", _SCRIPT)
assert _spec and _spec.loader
check_release_tag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_release_tag)

#: Any `kphutt/gdmutant@<ref>` a reader could copy, wherever it appears.
_USES = re.compile(r"kphutt/gdmutant@(?P<ref>[^\s\"'`]+)")

#: A floating major/minor tag: `v1`, `v0`, `v1.2` — anything short of a full version.
_FLOATING = re.compile(r"^v\d+(\.\d+)?$")

DOCS_SHOWING_A_USES_LINE = [
    REPO / "action.yml",
    REPO / "docs" / "gdmutant-guide.md",
]


@pytest.mark.parametrize("path", DOCS_SHOWING_A_USES_LINE, ids=lambda p: p.name)
def test_no_shipped_uses_line_pins_a_floating_tag(path: Path) -> None:
    refs = _USES.findall(path.read_text(encoding="utf-8"))
    assert refs, f"{path.relative_to(REPO)} shows no `uses:` line to check"
    for ref in refs:
        assert not _FLOATING.match(ref), (
            f"{path.relative_to(REPO)} tells consumers to pin `@{ref}`, a floating tag this repo "
            "cannot publish — pin a commit SHA or a full version tag"
        )


def test_a_floating_major_tag_really_is_unsatisfiable() -> None:
    # Grounds the README's stated reason: the release guard rejects `v1` against the packaged
    # version, so the tag consumers would want cannot be created in the first place.
    assert check_release_tag.mismatch("v1", check_release_tag.packaged_version()) is not None


def test_the_guide_says_there_is_no_floating_tag() -> None:
    # The claim a consumer needs, in the place they will look for it.
    guide = (REPO / "docs" / "gdmutant-guide.md").read_text(encoding="utf-8")
    assert "## GitHub Actions" in guide
    assert "`@v1` or `@v0`" in guide


def test_the_ref_inputs_own_default_is_not_a_floating_tag() -> None:
    # The bug this test exists to catch shipped in exactly this input's `default:` value (a literal
    # `v1`, a ref this repo can never produce) and was invisible to every check above: those only
    # scan `kphutt/gdmutant@<ref>` strings in prose/docs, never an input's own default. A consumer
    # who never overrides `ref` gets whatever this default resolves to, so it must never itself be a
    # floating tag — an empty string (falls back to `github.action_ref`, always real at
    # invocation-time for a remote `uses: owner/repo@ref`), a real branch, or a full version tag are
    # all fine; a bare `v1`/`v0`/`v1.2` is exactly the unsatisfiable shape this repo cannot produce.
    action = yaml.safe_load((REPO / "action.yml").read_text(encoding="utf-8"))
    default = action["inputs"]["ref"]["default"]
    assert not _FLOATING.match(default), (
        f"action.yml's 'ref' input defaults to `{default}`, a floating tag this repo cannot "
        "publish — a consumer who never overrides `ref` would get an install that 404s"
    )
