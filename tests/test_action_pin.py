"""The distributed action must never advertise a `@v1`-style floating tag.

`action.yml` makes gdmutant consumable as a GitHub Action, and its header comment, the guide's
"GitHub Actions" section, and the README's own copy are where a consumer copies the `uses:` line
from. Any of them could drift toward the convenient-looking `kphutt/gdmutant@v1`, which this repo
cannot produce:

* `scripts/check_release_tag.py` fails any tag that does not equal the version in `pyproject.toml`,
  so a `v1` tag would demand a packaged version of literally `1` — asserted below rather than
  described, so the reason stays true if the guard changes.
* The tag ruleset on the repo blocks deletion and non-fast-forward updates on every ref for anyone
  acting normally, so an existing tag cannot be moved to a later commit through ordinary use either
  (a repo admin can still disable the ruleset itself as a rare, deliberate override -- see
  docs/releasing.md -- but that's not something a consumer's pinned tag needs to worry about).

Consumers therefore pin a commit SHA (or a full `vX.Y.Z` tag) and take bumps from Dependabot. This
test keeps the shipped copy honest about that.

A second, separate way to go wrong: a real, valid, non-floating pin that simply falls behind. The
docs shipped a `v0.1.0` SHA through two more releases before anyone noticed. This module also
checks that every documented pin names the version currently in `pyproject.toml`, not just a
version that once existed.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
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
    REPO / "README.md",
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


#: A full 40-hex-char commit SHA, the documented, recommended way to pin (see the guide's
#: Pinning section).
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _latest_tag_commit(version: str) -> str:
    """The commit `vVERSION` points at, read straight from `origin` rather than local refs.

    `ci.yml`'s `verify` job checks out with no `fetch-depth` set, the actions/checkout default of
    1, a single commit and no tags at all. `git rev-parse vX.Y.Z^{commit}` only works if that tag
    ref exists locally, so it fails there with exit 128, "unknown revision", even though the tag
    is real and published. `git ls-remote` asks the remote directly and needs no local history at
    any depth. Prefer the `^{}`-dereferenced line, which is what an *annotated* tag's own commit
    resolves to. A lightweight tag (this repo's kind, as of writing) has no such line, and the
    plain ref is already the commit."""
    tag = f"v{version}"
    output = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    shas = {ref: sha for sha, ref in (line.split("\t") for line in output.splitlines() if line)}
    assert shas, f"origin has no tag named {tag!r} at all, the release may not be pushed yet"
    return shas.get(f"refs/tags/{tag}^{{}}", shas[f"refs/tags/{tag}"])


@pytest.mark.parametrize("path", DOCS_SHOWING_A_USES_LINE, ids=lambda p: p.name)
def test_every_shipped_uses_line_pins_the_latest_released_version(path: Path) -> None:
    # A `uses:` line that names a real, valid, non-floating ref still goes stale the moment a new
    # version ships: v0.1.0 stayed pinned here through 0.1.1 and 0.1.2, so a reader copying the
    # README installed a version with a bug 0.1.1 had already fixed, and got an action.yml with no
    # `command` input, one shipped later. `test_no_shipped_uses_line_pins_a_floating_tag` above
    # only checks the ref isn't unsatisfiable, not that it's current -- this closes that gap.
    version = check_release_tag.packaged_version()
    latest_tag_sha = _latest_tag_commit(version)

    refs = _USES.findall(path.read_text(encoding="utf-8"))
    assert refs, f"{path.relative_to(REPO)} shows no `uses:` line to check"
    for ref in refs:
        if _SHA.match(ref):
            assert ref == latest_tag_sha, (
                f"{path.relative_to(REPO)} pins `@{ref}`, which is not v{version}'s commit "
                f"(`{latest_tag_sha}`) -- the documented ref has gone stale, bump it to the "
                "latest release"
            )
        elif match := re.fullmatch(r"v(?P<tagged>\d+\.\d+\.\d+)", ref):
            assert match.group("tagged") == version, (
                f"{path.relative_to(REPO)} pins `@{ref}`, not the current release `v{version}` "
                "-- bump it"
            )
        # Anything else (a branch name, an empty ref) isn't a version pin this check applies to.


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
