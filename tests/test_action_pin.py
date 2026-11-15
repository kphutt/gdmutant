"""The documented action pins must name the packaged release's exact tag, and nothing else.

`action.yml` makes gdmutant consumable as a GitHub Action, and its header comment, the guide's
"GitHub Actions" section, and the README's own copy are where a consumer copies the `uses:` line
from. Each of those lines pins `kphutt/gdmutant@vX.Y.Z`, the full release tag. Three ways that can
go wrong, each checked here:

* A floating ref. The convenient-looking `@v1` is a tag this repo cannot produce:
  `scripts/check_release_tag.py` fails any tag that does not equal the version in
  `pyproject.toml`, so a `v1` tag would demand a packaged version of literally `1` (asserted below
  rather than described, so the reason stays true if the guard changes). A branch such as `main`
  moves with every merge, so it is no pin at all.
* A stale pin. The docs once shipped a `v0.1.0` pin through two more releases before anyone
  noticed. Every pin must name the version `pyproject.toml` declares, on every commit. The release
  commit sets both together (step 1 of docs/releasing.md, through scripts/bump_action_pins.py), so
  there is no window where they may differ.
* A pin outside the one list. A doc that shows a pin but is missing from `PIN_FILES` would never be
  checked or bumped, so every tracked file is scanned for one.

Tags are safe to show because they cannot move: the repo's tag ruleset blocks deleting or
re-pointing any tag, and releases from 0.1.8 on are also immutable. A consumer whose policy
requires commit IDs can still pin the release's commit, which the guide says.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import test_public_readiness
import yaml

REPO = Path(__file__).resolve().parent.parent

#: This module, referenced by name so a test can point `REPO` at a throwaway directory without
#: disturbing the real one -- `monkeypatch.setattr` needs the module object, not the bare name.
_MODULE = sys.modules[__name__]

_SCRIPT = REPO / "scripts" / "check_release_tag.py"
_spec = importlib.util.spec_from_file_location("check_release_tag_for_pin", _SCRIPT)
assert _spec and _spec.loader
check_release_tag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_release_tag)

#: Any `kphutt/gdmutant@<ref>` a reader could copy, wherever it appears.
_USES = re.compile(r"kphutt/gdmutant@(?P<ref>[^\s\"'`]+)")

#: A floating major/minor tag: `v1`, `v0`, `v1.2` — anything short of a full version.
_FLOATING = re.compile(r"^v\d+(\.\d+)?$")

_BUMP = REPO / "scripts" / "bump_action_pins.py"
_bump_spec = importlib.util.spec_from_file_location("bump_action_pins_for_pin", _BUMP)
assert _bump_spec and _bump_spec.loader
bump_action_pins = importlib.util.module_from_spec(_bump_spec)
_bump_spec.loader.exec_module(bump_action_pins)

#: The one list of files carrying a pin, owned by the script that bumps them at release time. This
#: test used to keep its own copy, and two lists of the same files drift: a doc added to one would
#: be checked but never bumped, or bumped but never checked.
DOCS_SHOWING_A_USES_LINE = [REPO / name for name in bump_action_pins.PIN_FILES]


@pytest.mark.parametrize("path", DOCS_SHOWING_A_USES_LINE, ids=lambda p: p.name)
def test_no_shipped_uses_line_pins_a_floating_tag(path: Path) -> None:
    refs = _USES.findall(path.read_text(encoding="utf-8"))
    assert refs, f"{path.relative_to(REPO)} shows no `uses:` line to check"
    for ref in refs:
        assert not _FLOATING.match(ref), (
            f"{path.relative_to(REPO)} tells consumers to pin `@{ref}`, a floating tag this repo "
            "cannot publish — pin the full release tag, `@vX.Y.Z`"
        )


def test_a_floating_major_tag_really_is_unsatisfiable() -> None:
    # Grounds the README's stated reason: the release guard rejects `v1` against the packaged
    # version, so the tag consumers would want cannot be created in the first place.
    assert check_release_tag.mismatch("v1", check_release_tag.packaged_version()) is not None


def test_the_guide_says_there_is_no_floating_tag() -> None:
    # The claim a consumer needs, in the place they will look for it.
    guide = (REPO / "docs" / "gdmutant-guide.md").read_text(encoding="utf-8")
    assert "## GitHub Actions" in guide
    assert "no major-version tags" in guide


def test_the_guide_says_the_tags_cannot_move_and_how_to_pin_a_commit_instead() -> None:
    # Showing a tag is only safe because it cannot be re-pointed. A reader whose policy requires
    # commit IDs needs to know that is still an option.
    guide = (REPO / "docs" / "gdmutant-guide.md").read_text(encoding="utf-8")
    assert "release tags cannot be moved" in guide
    assert "requires commit IDs" in guide


# --- every pin names the packaged release's tag -------------------------------------------------


#: A full 40-hex-char commit ID.
_SHA = re.compile(r"^[0-9a-f]{40}$")


def check_pins_are_current(text: str, label: str, version: str) -> None:
    """Assert every pin in `text` reads `@vVERSION` exactly, or raise `AssertionError`.

    A pin here is anything `bump_action_pins.PIN` matches, the same rule the script that writes
    the pins uses, so the writer and this check cannot disagree about what a pin is. A file with no
    pin at all fails too: a check that read nothing would be this repo's recurring bug one."""
    refs = bump_action_pins.pins(text)
    assert refs, f"{label} shows no `kphutt/gdmutant@<ref>` pin to check"
    wanted = f"v{version}"
    for ref in refs:
        if ref == wanted:
            continue
        if _SHA.match(ref):
            raise AssertionError(
                f"{label} pins the commit ID `@{ref}`. The docs pin the release tag, `@{wanted}`"
            )
        if bump_action_pins.RELEASE_TAG.fullmatch(ref):
            raise AssertionError(
                f"{label} pins `@{ref}`, but the packaged version is {version}, so every pin "
                f"must read `@{wanted}`. Run scripts/bump_action_pins.py, part of step 1 of "
                "docs/releasing.md"
            )
        raise AssertionError(
            f"{label} pins `@{ref}`, a ref that can move or does not exist (a branch, or a "
            f"major-only tag). Pin the exact release tag, `@{wanted}`"
        )


@pytest.mark.parametrize("path", DOCS_SHOWING_A_USES_LINE, ids=lambda p: p.name)
def test_every_shipped_pin_names_the_packaged_release(path: Path) -> None:
    # A `uses:` line that names a real, valid, non-floating ref still goes stale the moment a new
    # version ships: v0.1.0 stayed pinned here through 0.1.1 and 0.1.2, so a reader copying the
    # README installed a version with a bug 0.1.1 had already fixed. The release commit moves the
    # version and the pins together, so they agree on every commit, the tagged one included.
    check_pins_are_current(
        path.read_text(encoding="utf-8"),
        str(path.relative_to(REPO)),
        check_release_tag.packaged_version(),
    )


def _pin(ref: str) -> str:
    """A synthetic pin line. Built from `ref` so this file never holds a pin the scan would find."""
    return f"      - uses: kphutt/gdmutant@{ref}\n"


def test_a_pin_naming_the_packaged_release_passes() -> None:
    check_pins_are_current(_pin("v0.1.3") + "prose\n" + _pin("v0.1.3"), "fake.md", "0.1.3")


def test_a_stale_release_tag_fails() -> None:
    with pytest.raises(AssertionError, match=r"packaged version is 0\.1\.3"):
        check_pins_are_current(_pin("v0.1.2"), "fake.md", "0.1.3")


def test_a_later_release_tag_fails_too() -> None:
    with pytest.raises(AssertionError, match=r"must read `@v0\.1\.3`"):
        check_pins_are_current(_pin("v0.1.4"), "fake.md", "0.1.3")


def test_a_commit_id_pin_fails() -> None:
    with pytest.raises(AssertionError, match="commit ID"):
        check_pins_are_current(_pin("a" * 40), "fake.md", "0.1.3")


@pytest.mark.parametrize("ref", ["main", "v1", "v0", "v0.1", "v0.1.3-rc1", "latest"])
def test_a_ref_that_is_not_an_exact_release_tag_fails(ref: str) -> None:
    with pytest.raises(AssertionError, match="can move or does not exist"):
        check_pins_are_current(_pin(ref), "fake.md", "0.1.3")


def test_one_bad_pin_among_good_ones_fails() -> None:
    # A file with one good pin beside one bad pin must not pass on the strength of the good one.
    with pytest.raises(AssertionError, match="commit ID"):
        check_pins_are_current(_pin("v0.1.3") + _pin("b" * 40), "fake.md", "0.1.3")


def test_a_file_with_no_pin_fails() -> None:
    with pytest.raises(AssertionError, match="shows no"):
        check_pins_are_current("no pins here\n", "fake.md", "0.1.3")


def test_a_templated_ref_in_code_is_not_a_pin() -> None:
    # action.yml builds install URLs as `kphutt/gdmutant@{action_ref}` in Python. Those are code,
    # not a line a reader copies, so they must not count as pins, good or bad.
    text = 'return f"git+https://github.com/kphutt/gdmutant@{action_ref}"\n'
    assert bump_action_pins.pins(text) == []


def test_what_the_bump_writes_is_what_this_check_accepts() -> None:
    # Recurring bug two: the writer and the checker are a pair. A stale file run through the bump
    # must come out passing, and the same file before the bump must fail.
    stale = _pin("v0.1.2") + "prose\n" + _pin("v0.1.2")
    with pytest.raises(AssertionError):
        check_pins_are_current(stale, "fake.md", "0.1.3")
    bumped, changed, _, problems = bump_action_pins.bump(stale, "0.1.3")
    assert (changed, problems) == (2, [])
    check_pins_are_current(bumped, "fake.md", "0.1.3")


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


def _tracked_files() -> list[Path]:
    """Every file git tracks in this checkout, as absolute paths.

    `git ls-files`, not a directory walk. A walk descends into anything sitting under the working
    tree whether git knows about it or not -- including a git worktree, or any other untracked
    copy of the repo, parked under a gitignored directory such as `.claude/worktrees/`. That copy
    carries its own README.md and action.yml, each pinning the action again, so the walk found the
    same pin a second time at a path nothing lists, and failed. `git ls-files` only ever names what
    git actually tracks, so a nested copy it does not track -- ignored or merely untracked alike --
    is invisible to it, on any machine that happens to have one sitting under the tree.

    Raises `test_public_readiness._GitCannotAnswer` rather than returning nothing when git itself
    could not be asked: "no answer" and "no files" are different facts, and only `_scan_targets`
    below knows which of them is safe to treat as expected.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise test_public_readiness._GitCannotAnswer(str(exc)) from exc
    return [REPO / name for name in out.split("\0") if name]


def _scan_targets() -> list[Path]:
    """The tracked files to read, a skip where nothing is tracked for a named reason, or a
    failure everywhere else.

    Mirrors `test_public_readiness._scan_targets` exactly, because this scan and that one answer
    the same question of a different root: is this a real checkout, or one of the copies a test
    suite also legitimately runs from (mutmut's `mutants/`, a poodle run, an unpacked sdist)? That
    question -- and the empty answer being expected there, not a failure -- is shared with
    `test_public_readiness._why_this_tree_tracks_nothing`, so it is asked once, not answered twice.
    An empty or unreadable answer everywhere else must not let the assertions below pass over zero
    files and call the tree clean, which is the "gate that passes without checking anything" shape
    AGENTS.md warns about.
    """
    reason = test_public_readiness._why_this_tree_tracks_nothing(REPO)
    try:
        files = _tracked_files()
    except test_public_readiness._GitCannotAnswer as exc:
        if reason is not None:
            pytest.skip(f"{reason}, and git cannot answer here either ({exc})")
        pytest.fail(
            f"git could not list this tree's tracked files ({exc}), so this scan read nothing "
            "at all. It fails rather than passing: a scan that reports clean because it never ran "
            "is the one outcome this module must never produce. Run the suite from a git checkout."
        )
    if not files:
        if reason is not None:
            pytest.skip(f"{reason}, so git tracks nothing here and there is nothing to scan")
        pytest.fail(
            "git tracks no files in this tree, so this scan read nothing at all and would have "
            "passed without seeing a byte. It fails rather than passing. Run the suite from a git "
            "checkout."
        )
    return files


#: A pin a reader could copy, wherever it appears: a version-shaped tag (`@v0.1.7`, or a floating
#: `@v1`) or a full commit ID. A placeholder such as `@vX.Y.Z` or a templated `@{ref}` is not one.
_PIN = re.compile(rb"kphutt/gdmutant@(?:v\d|[0-9a-f]{40})")


def _files_pinning_the_action() -> set[str]:
    """Every tracked file's path (relative to `REPO`, forward-slashed) that carries a pin."""
    return {
        path.relative_to(REPO).as_posix()
        for path in _scan_targets()
        if _PIN.search(path.read_bytes())
    }


def test_every_file_that_pins_the_action_is_on_the_one_list() -> None:
    """A new doc showing a pinned `uses:` line must join `PIN_FILES`, or nothing would keep it
    current: the staleness check above would never read it, and the release's pin bump would never
    rewrite it. So scan every tracked file for pins and compare the files found with the list.
    """
    found = _files_pinning_the_action()
    listed = set(bump_action_pins.PIN_FILES)
    assert found, "found no pinned `uses:` line anywhere, so this scan read nothing useful"
    assert found <= listed, (
        f"these files pin the action but are not in scripts/bump_action_pins.py's PIN_FILES: "
        f"{sorted(found - listed)}. Add them there, so the release bumps them and this test "
        "checks them"
    )
    assert listed <= found, f"PIN_FILES names files with no pin in them: {sorted(listed - found)}"


def _pin_line(ref: str) -> str:
    return f"      - uses: kphutt/gdmutant@{ref}\n"


def _init_git_repo(root: Path) -> None:
    """A fresh, throwaway git repo at `root`, with an identity so a commit can be made."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)


def _commit_all(root: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=root, check=True)


def test_an_ignored_copy_of_a_pinning_file_does_not_trip_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape that used to fail: an untracked copy of the repo -- a git worktree, or any
    other gitignored tree -- parked under the working tree, holding its own copy of a pinning
    file. `git ls-files` must never see it, so it must never show up in the scan."""
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(_pin_line("v0.1.0"), encoding="utf-8")
    _commit_all(tmp_path)

    copy = tmp_path / "ignored" / "README.md"
    copy.parent.mkdir()
    copy.write_text(_pin_line("v0.0.9"), encoding="utf-8")
    # Sanity: prove the fixture is actually ignored, not merely untracked -- the distinction this
    # test exists to pin, since `git ls-files` (unlike a walk that skips `.gitignore` entries by
    # name) leaves out both alike.
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(copy)], cwd=tmp_path, check=False
    ).returncode
    assert ignored == 0, "the planted copy was not actually gitignored, so this test proves nothing"

    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    assert _files_pinning_the_action() == {"README.md"}


def test_a_tracked_pinning_file_not_on_the_list_still_trips_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: a file git genuinely tracks, holding a pin, is not something the fix should
    ever make invisible -- only the untracked/ignored copies from the bug above."""
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text(_pin_line("v0.1.0"), encoding="utf-8")
    extra = tmp_path / "docs" / "extra.md"
    extra.parent.mkdir()
    extra.write_text(_pin_line("c" * 40), encoding="utf-8")
    _commit_all(tmp_path)

    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    found = _files_pinning_the_action()
    assert found == {"README.md", "docs/extra.md"}
    # This is exactly what makes the real test fail loudly for a file that shows a pin but was
    # never added to PIN_FILES, rather than silently missing it: `docs/extra.md` is found, and a
    # `PIN_FILES` list that does not name it would not be a superset of `found`.
    assert not found <= {"README.md"}


def _outcome_of_scanning() -> tuple[str, str]:
    """What `_scan_targets` does here, as a word, plus what it said about it.

    Mirrors `test_public_readiness._outcome_of_scanning`, and for the same reason: a skip raised
    inside a test is reported as a skip rather than a failure, so asserting on a raised exception
    directly would make this go quiet in exactly the case it exists to make noisy.
    """
    try:
        _scan_targets()
    except pytest.fail.Exception as failure:
        return "fail", str(failure)
    except pytest.skip.Exception as skipped:
        return "skip", str(skipped)
    return "scan", ""


def test_a_mutation_tools_copy_of_the_tree_still_skips_the_pin_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape Litmus caught: mutmut's `mutants/` copy has no `.git` of its own, so a `git
    ls-files` run there walks up to the enclosing real checkout and succeeds -- but answers empty,
    because nothing under a directory the real repo itself ignores is tracked. An unconditional
    `assert files` read that as "found nothing useful" and aborted mutmut's baseline entirely
    (deleting the dogfood score, not lowering it) on every run, since this scan never got the
    chance to say why. This must skip, and name the reason, instead."""
    monkeypatch.setattr(_MODULE, "REPO", tmp_path / "mutants")
    monkeypatch.setattr(_MODULE, "_tracked_files", list)
    outcome, said = _outcome_of_scanning()
    assert (outcome, "mutmut" in said) == ("skip", True), said


def test_a_tree_that_tracks_nothing_and_is_not_a_known_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same fix: an empty answer is only ever excused for a *named* reason.
    An ordinary directory that merely happens to track nothing is still a broken scan -- "git said
    nothing" is not itself a reason -- and must fail rather than quietly pass."""
    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    monkeypatch.setattr(_MODULE, "_tracked_files", list)
    outcome, said = _outcome_of_scanning()
    assert outcome == "fail", f"an unexplained empty tree came back as {outcome!r}: {said}"
    assert "tracks no files" in said
