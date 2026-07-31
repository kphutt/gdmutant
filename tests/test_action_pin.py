"""The distributed action must never advertise a `@v1`-style floating tag.

`action.yml` makes gdmutant consumable as a GitHub Action, and its header comment plus the README's
"GitHub Action" section are where a consumer copies the `uses:` line from. Both used to be able to
drift toward the convenient-looking `kphutt/gdmutant@v1`, which this repo cannot produce:

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

DOCS_SHOWING_A_USES_LINE = [REPO / "action.yml", REPO / "README.md"]


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


def test_the_readme_says_there_is_no_floating_tag() -> None:
    # The claim a consumer needs, in the place they will look for it.
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "## GitHub Action" in readme
    assert "There is no `@v1` or `@v0`" in readme
