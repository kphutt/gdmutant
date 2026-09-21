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

import functools
import importlib.util
import os
import re
import subprocess
import warnings
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

#: The `# vX.Y.Z` comment beside a pinned SHA, which is what a reader actually reads to tell which
#: release they are copying. Needs no network, so it is the half of the staleness check that can
#: still run while a release's tag is being cut.
_USES_VERSION_COMMENT = re.compile(r"kphutt/gdmutant@[0-9a-f]{40}\s*#\s*v(?P<tagged>\d+\.\d+\.\d+)")

_BUMP = REPO / "scripts" / "bump_action_pins.py"
_bump_spec = importlib.util.spec_from_file_location("bump_action_pins_for_pin", _BUMP)
assert _bump_spec and _bump_spec.loader
bump_action_pins = importlib.util.module_from_spec(_bump_spec)
_bump_spec.loader.exec_module(bump_action_pins)

#: The one list of files carrying a pin, owned by the script that bumps them after a release. This
#: test used to keep its own copy, and two lists of the same files drift: a doc added to one would
#: be checked but never bumped, or bumped but never checked.
DOCS_SHOWING_A_USES_LINE = [REPO / name for name in bump_action_pins.PIN_FILES]

#: Directories that are not the repository's own content: environments, caches, build output, the
#: downloaded Godot addons, and the copies mutation tools make of the tree.
_NOT_THE_REPO = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "htmlcov",
    "dist",
    "build",
    "mutants",
    ".benchmarks",
}


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


@functools.cache
def _remote_tag_refs() -> dict[str, str]:
    """Every tag ref on `origin`, mapped to its SHA, asked for once per test run.

    `_latest_tag_commit` is called five times a run, and each call used to be its own
    `git ls-remote` over the network. One listing of all tags answers every call, and the tags
    on `origin` do not change during one run. That matters most in a mutation sweep, which runs
    this whole suite once per surviving mutant."""
    output = subprocess.run(
        ["git", "ls-remote", "--tags", "origin"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return dict(bump_action_pins.parse_ls_remote(output))


def _latest_tag_commit(version: str) -> str | None:
    """The commit `vVERSION` points at, read straight from `origin` rather than local refs.

    `ci.yml`'s `verify` job checks out with no `fetch-depth` set, the actions/checkout default of
    1, a single commit and no tags at all. `git rev-parse vX.Y.Z^{commit}` only works if that tag
    ref exists locally, so it fails there with exit 128, "unknown revision", even though the tag
    is real and published. `git ls-remote` asks the remote directly and needs no local history at
    any depth. Prefer the `^{}`-dereferenced line, which is what an *annotated* tag's own commit
    resolves to. A lightweight tag (this repo's kind, as of writing) has no such line, and the
    plain ref is already the commit."""
    sha = bump_action_pins.commit_of_tag(version, _remote_tag_refs())
    if sha is None:
        # The release window: `pyproject.toml` already names the version being cut, but its tag is
        # not pushed yet, because the tag has to point at a commit that is already on `main`. This
        # used to be an assert, which deadlocked the release it was meant to protect: bumping the
        # version turned this test red, `Verify` is a required check, so the bump could not merge,
        # so the tag could never be pushed to make it green. 0.1.2 was cut before this function
        # existed, so nothing caught it. Returning None instead lets the caller fall back to a
        # check that needs no tag, rather than blocking or passing silently.
        return None
    return sha


def _head_commit() -> str | None:
    """The commit this checkout is on, or None when git cannot say."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def comparable_tag_commit(
    version: str, tag_sha: str | None, head_sha: str | None
) -> tuple[str | None, str | None]:
    """The commit the documented pins must name, or None and the reason that cannot be checked.

    Two windows make the SHA half impossible, and both are part of every release. Before the tag is
    pushed there is no commit to compare against (`tag_sha` is None). And on the tagged commit
    itself, the one `publish.yml`'s gate checks out and tests, the pins cannot name that commit,
    because a commit cannot contain its own hash. Treating the second window as stale made the
    release gate fail on every release, since the tagged commit can only ever pin the previous one:
    reproduced by running this test on the v0.1.2 tagged commit. Everywhere else, including `main`
    after the release, the pins must name the tag, and the follow-up PR that bumps them is what
    turns that back green. When git cannot report HEAD (`head_sha` is None) the strict comparison
    still runs, so a missing answer never loosens the check."""
    if tag_sha is None:
        return None, None
    if tag_sha == head_sha:
        return None, f"this checkout is v{version}'s own tagged commit, which cannot pin itself"
    return tag_sha, None


def check_pins_are_current(
    text: str,
    label: str,
    version: str,
    latest_tag_sha: str | None,
    unchecked_because: str | None = None,
) -> None:
    """Assert `text`'s shipped pins name `version`, raising `AssertionError` with `label` if not.

    Split out from the test below so every branch is reachable from a unit test with synthetic
    inputs. `latest_tag_sha is None` means the SHA half cannot run, in either of the two release
    windows `comparable_tag_commit` describes: before the tag is pushed, and on the tagged commit
    itself. Both only occur on a real release and both used to deadlock it, which is why they are
    unit-tested here. `unchecked_because` is the reason the warning gives, and defaults to the
    first window's.
    """
    if latest_tag_sha is None:
        # The SHA half of this check cannot run (see the docstring for when). Say so out
        # loud rather than skipping quietly, and still check the half that needs no tag: the `#
        # vX.Y.Z` comment beside each pin. That comment is what actually went stale the time this
        # mattered, when it read `# v0.1.0` through two later releases, so the protection this test
        # exists for stays live during the release window instead of lapsing exactly then.
        because = unchecked_because or f"v{version} is not on origin yet"
        warnings.warn(
            f"NOTCHECKED: {because}, so the pinned SHA could not be compared against its tag "
            f"in {label}. Checked the version comment instead.",
            stacklevel=2,
        )
        # Count the SHA pins first, and require a comment on every one of them. Collecting only the
        # commented pins and asserting that set is non-empty would let a file carrying one commented
        # pin and one bare pin through untouched: the bare one is the very thing this branch cannot
        # otherwise verify, so skipping it is the shape this repo calls recurring bug one, here in
        # the gate built to close another instance of it. Caught in review of this PR.
        sha_pins = [ref for ref in _USES.findall(text) if _SHA.match(ref)]
        commented = _USES_VERSION_COMMENT.findall(text)
        assert sha_pins, f"{label} shows no SHA-pinned `uses:` line to check"
        assert len(commented) == len(sha_pins), (
            f"{label} has {len(sha_pins)} SHA-pinned `uses:` line(s) but only {len(commented)} "
            "carry a `# vX.Y.Z` comment. With no tag pushed yet, a pin without that comment cannot "
            "be checked by either half, so add the comment rather than leaving it unverifiable."
        )
        for tagged in commented:
            assert tagged == version, (
                f"{label} pins a ref commented `# v{tagged}`, not the current "
                f"release `v{version}` -- bump the pin and its comment together"
            )
        return

    refs = _USES.findall(text)
    assert refs, f"{label} shows no `uses:` line to check"
    for ref in refs:
        if _SHA.match(ref):
            assert ref == latest_tag_sha, (
                f"{label} pins `@{ref}`, which is not v{version}'s commit "
                f"(`{latest_tag_sha}`) -- the documented ref has gone stale, bump it to the "
                "latest release"
            )
        elif match := re.fullmatch(r"v(?P<tagged>\d+\.\d+\.\d+)", ref):
            assert match.group("tagged") == version, (
                f"{label} pins `@{ref}`, not the current release `v{version}` -- bump it"
            )
        # Anything else (a branch name, an empty ref) isn't a version pin this check applies to.


@pytest.mark.parametrize("path", DOCS_SHOWING_A_USES_LINE, ids=lambda p: p.name)
def test_every_shipped_uses_line_pins_the_latest_released_version(path: Path) -> None:
    # A `uses:` line that names a real, valid, non-floating ref still goes stale the moment a new
    # version ships: v0.1.0 stayed pinned here through 0.1.1 and 0.1.2, so a reader copying the
    # README installed a version with a bug 0.1.1 had already fixed, and got an action.yml with no
    # `command` input, one shipped later. `test_no_shipped_uses_line_pins_a_floating_tag` above
    # only checks the ref isn't unsatisfiable, not that it's current -- this closes that gap.
    version = check_release_tag.packaged_version()
    tag_sha, because = comparable_tag_commit(version, _latest_tag_commit(version), _head_commit())
    check_pins_are_current(
        path.read_text(encoding="utf-8"),
        str(path.relative_to(REPO)),
        version,
        tag_sha,
        because,
    )


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


#: A synthetic pin line, the shape every shipped doc uses: a full SHA plus a `# vX.Y.Z` comment.
_FAKE_SHA = "a" * 40


def _bare_pin(sha: str = _FAKE_SHA) -> str:
    """A pin with no `# vX.Y.Z` comment: the shape neither half of the check can verify."""
    return f"      - uses: kphutt/gdmutant@{sha}\n"


def _pin(version: str, sha: str = _FAKE_SHA) -> str:
    return f"      - uses: kphutt/gdmutant@{sha} # v{version}\n"


def test_the_release_window_passes_and_says_which_half_did_not_run() -> None:
    """The state that used to deadlock: version bumped, tag not pushed, SHA necessarily stale.

    `_latest_tag_commit` returns None there. This must pass, because the tag cannot exist until
    the bump has merged, and it must say out loud that the SHA half did not run rather than going
    quiet, which is this repo's recurring bug one.
    """
    with pytest.warns(UserWarning, match="NOTCHECKED"):
        check_pins_are_current(_pin("0.1.3"), "fake.md", "0.1.3", None)


def test_the_release_window_still_catches_a_stale_pin_comment() -> None:
    # The protection must not lapse during the window. A comment naming an older release is exactly
    # the bug this whole module exists for: the docs shipped `# v0.1.0` through two later releases.
    with (
        pytest.warns(UserWarning, match="NOTCHECKED"),
        pytest.raises(AssertionError, match="not the current"),
    ):
        check_pins_are_current(_pin("0.1.0"), "fake.md", "0.1.3", None)


def test_the_release_window_rejects_a_mix_of_commented_and_bare_pins() -> None:
    # The hole a reviewer found in the first version of this fallback: it gathered the pins that DID
    # carry a comment and asserted that set was non-empty, so a file with one commented pin beside
    # one bare pin passed, leaving the bare pin -- the only one it could not verify -- unchecked.
    mixed = _pin("0.1.3") + _bare_pin("b" * 40)
    with (
        pytest.warns(UserWarning, match="NOTCHECKED"),
        pytest.raises(AssertionError, match="carry a `# vX.Y.Z` comment"),
    ):
        check_pins_are_current(mixed, "fake.md", "0.1.3", None)


def test_the_release_window_rejects_a_pin_with_no_version_comment() -> None:
    # With no tag AND no comment, neither half can tell whether the pin is current. Refuse rather
    # than pass, or the window becomes a hole a stale pin can walk through.
    bare = _bare_pin()
    with (
        pytest.warns(UserWarning, match="NOTCHECKED"),
        pytest.raises(AssertionError, match="carry a `# vX.Y.Z` comment"),
    ):
        check_pins_are_current(bare, "fake.md", "0.1.3", None)


def test_once_the_tag_exists_the_sha_is_compared_again() -> None:
    # After the tag is pushed the full check resumes: the SHA must equal the tag's commit, and a
    # pin left at the previous release fails, which is the reminder to bump it working as intended.
    check_pins_are_current(_pin("0.1.3"), "fake.md", "0.1.3", _FAKE_SHA)
    with pytest.raises(AssertionError, match="has gone stale"):
        check_pins_are_current(_pin("0.1.3", sha="b" * 40), "fake.md", "0.1.3", _FAKE_SHA)


def test_an_unreleased_version_resolves_to_none_rather_than_raising() -> None:
    """The deadlock fix itself, at its source.

    This used to `assert shas`, so a packaged version with no tag yet raised instead of returning,
    turning a required check red and making the release that would create the tag unmergeable. The
    tests above cover what the caller does with None. This one covers producing it at all.
    """
    assert _latest_tag_commit("99.99.99") is None
    # A version that really is tagged still resolves, so the None path cannot swallow everything.
    assert _latest_tag_commit("0.1.2") == "284f185f1495f2d79150781cf2e6de618ed11327"


# --- the tagged-commit window -------------------------------------------------------------------


def test_no_tag_yet_skips_the_sha_half_with_the_default_reason() -> None:
    assert comparable_tag_commit("0.1.3", None, "a" * 40) == (None, None)


def test_the_tagged_commit_itself_skips_the_sha_half_and_says_why() -> None:
    sha, because = comparable_tag_commit("0.1.3", _FAKE_SHA, _FAKE_SHA)
    assert sha is None
    assert because is not None and "own tagged commit" in because


def test_any_other_commit_after_the_tag_still_compares_the_sha() -> None:
    assert comparable_tag_commit("0.1.3", _FAKE_SHA, "c" * 40) == (_FAKE_SHA, None)


def test_an_unknown_head_still_compares_the_sha_rather_than_loosening_the_check() -> None:
    assert comparable_tag_commit("0.1.3", _FAKE_SHA, None) == (_FAKE_SHA, None)


def test_the_release_gate_on_the_tagged_commit_passes_with_the_previous_releases_pins() -> None:
    """The deadlock this window exists for, end to end through both functions.

    On the tagged commit the docs still pin the previous release's SHA, and must, while their
    comments already name the release being cut. That has to pass, or `publish.yml` can never
    publish. The same pins on a later commit must fail, so the stale-pin protection is intact."""
    previous = "b" * 40
    docs = _pin("0.1.3", sha=previous)
    sha, because = comparable_tag_commit("0.1.3", _FAKE_SHA, _FAKE_SHA)
    with pytest.warns(UserWarning, match="own tagged commit"):
        check_pins_are_current(docs, "fake.md", "0.1.3", sha, because)
    sha, because = comparable_tag_commit("0.1.3", _FAKE_SHA, "c" * 40)
    with pytest.raises(AssertionError, match="gone stale"):
        check_pins_are_current(docs, "fake.md", "0.1.3", sha, because)


def test_every_file_that_pins_the_action_is_on_the_one_list() -> None:
    """A new doc showing a pinned `uses:` line must join `PIN_FILES`, or nothing would keep it
    current: the staleness check above would never read it, and the release's pin bump would never
    rewrite it. So walk the repository for SHA pins and compare the files found with the list."""
    pin = re.compile(rb"kphutt/gdmutant@[0-9a-f]{40}")
    found = set()
    for dirpath, dirnames, filenames in os.walk(REPO):
        here = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _NOT_THE_REPO
            and not d.startswith((".venv-", ".poodle-temp"))
            and here / d != REPO / "corpus" / "addons"
        ]
        for filename in filenames:
            path = here / filename
            if pin.search(path.read_bytes()):
                found.add(path.relative_to(REPO).as_posix())
    listed = set(bump_action_pins.PIN_FILES)
    assert found, "found no pinned `uses:` line anywhere, so this scan read nothing useful"
    assert found <= listed, (
        f"these files pin the action but are not in scripts/bump_action_pins.py's PIN_FILES: "
        f"{sorted(found - listed)}. Add them there, so the release bumps them and this test "
        "checks them"
    )
    assert listed <= found, f"PIN_FILES names files with no pin in them: {sorted(listed - found)}"
