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
checks that every documented pin names the release this commit should name, not just a version
that once existed.

A third: a pin whose `# vX.Y.Z` comment and SHA name different releases. 0.1.5 shipped its PyPI
page that way, with 0.1.4's commit commented `# v0.1.5`. The release commit now leaves every pin
alone, and both checks here hold the comment and the SHA to the same release.
"""

from __future__ import annotations

import functools
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

#: The one list of files carrying a pin, owned by the script that bumps them after a release. This
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
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout
    return dict(bump_action_pins.parse_ls_remote(output))


#: A `uses:` ref, plus the `# vX.Y.Z` comment that may follow it on the same line. The comment is
#: what a reader reads to tell which release they are copying, so it has to agree with the SHA.
_USES_WITH_LABEL = re.compile(
    r"kphutt/gdmutant@(?P<ref>[^\s\"'`]+)(?:[ \t]*#[ \t]*v(?P<label>\d+\.\d+\.\d+))?"
)


def _head_commit() -> str | None:
    """The commit this checkout is on, or None when git cannot say."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def expected_pin(version: str, refs: dict[str, str], head_sha: str | None) -> tuple[str, str]:
    """The release every documented pin must name on this commit, as (version, commit).

    The release commit never touches a pin. Once the tag exists, step 10's follow-up PR moves each
    pin's SHA and its `# vX.Y.Z` comment together. So the pins name the previous release in two
    windows, and `version` everywhere else:

    * before `vVERSION` is pushed, because there is no commit to name yet;
    * on `vVERSION`'s own tagged commit, the one `publish.yml`'s gate checks out and tests,
      because a commit cannot contain its own hash.

    Both windows are fully checked: the previous release's tag exists, so its SHA and its version
    can both be compared. On any other commit once the tag exists, `main` included, the pins must
    name `version`, and this fails until step 10 merges, which is the reminder working. When git
    cannot report HEAD (`head_sha` is None) that strict expectation still applies, so a missing
    answer never loosens the check.

    0.1.5 shipped the shape this rules out. Its release commit moved the comments to `# v0.1.5`
    while the SHAs still named 0.1.4, and PyPI froze that README into the 0.1.5 page."""
    tag_sha = bump_action_pins.commit_of_tag(version, refs)
    if tag_sha is not None and tag_sha != head_sha:
        return version, tag_sha
    previous = bump_action_pins.previous_release(version, refs)
    assert previous is not None, (
        f"no release older than v{version} is tagged on origin, so no documented pin can name a "
        "real release yet"
    )
    previous_sha = bump_action_pins.commit_of_tag(previous, refs)
    assert previous_sha is not None, f"v{previous} was listed but has no commit"
    return previous, previous_sha


def accepted_pins(
    version: str, refs: dict[str, str], head_sha: str | None, pins_dirty: bool
) -> list[tuple[str, str]]:
    """Every (version, commit) pair the pins may name here: `expected_pin`'s, and at most one more.

    The one extra case is step 10 before its commit. A maintainer runs `bump_action_pins.py` on a
    branch cut from the tagged commit, so HEAD is still that commit, and `expected_pin` asks for the
    previous release. The working tree already pins the new tag, and the suite run on it went red
    although nothing was wrong. So when HEAD is the tag's own commit and a pin file differs from
    HEAD, the new tag's pins are accepted too. A clean tree on the tagged commit, which is what
    `publish.yml`'s gate checks out, stays strict: only the previous release passes there."""
    accepted = [expected_pin(version, refs, head_sha)]
    tag_sha = bump_action_pins.commit_of_tag(version, refs)
    if pins_dirty and tag_sha is not None and tag_sha == head_sha:
        accepted.append((version, tag_sha))
    return accepted


def _pin_files_differ_from_head() -> bool:
    """Does any pin file in the working tree differ from HEAD? Anything git cannot answer counts
    as no, so a failed question keeps the check strict rather than loosening it."""
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", *bump_action_pins.PIN_FILES],
            cwd=REPO,
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 1


def check_pins_name_one_of(texts: dict[str, str], accepted: list[tuple[str, str]]) -> None:
    """Pass if one `accepted` pair fits every pin in every file of `texts`, label to text.

    One pair across all the files, not one per file: a half-done bump, with one file on the new tag
    and another still on the previous release, must fail. Otherwise raise every file's failure
    against the first pair, since that pair is the strict expectation."""
    first: list[str] | None = None
    for version, sha in accepted:
        problems: list[str] = []
        for label, text in texts.items():
            try:
                check_pins_are_current(text, label, version, sha)
            except AssertionError as failure:
                problems.append(str(failure))
        if not problems:
            return
        if first is None:
            first = problems
    assert first is not None, "no accepted pair was given, so nothing could pass"
    raise AssertionError("\n".join(first))


def check_pins_are_current(text: str, label: str, version: str, sha: str) -> None:
    """Assert every pin in `text` reads `@SHA # vVERSION`, both halves, or raise `AssertionError`.

    Split out from the test below so every branch is reachable from a unit test with synthetic
    inputs. A SHA pin with no comment fails too: nothing could then tell a reader which release it
    is, and a check that skipped it would be this repo's recurring bug one."""
    pins = list(_USES_WITH_LABEL.finditer(text))
    assert pins, f"{label} shows no `uses:` line to check"
    for pin in pins:
        ref, tagged = pin["ref"], pin["label"]
        if _SHA.match(ref):
            assert tagged is not None, (
                f"{label} pins `@{ref}` with no `# vX.Y.Z` comment. Add `# v{version}`, so a "
                "reader can tell which release it is"
            )
            assert (ref, tagged) == (sha, version), (
                f"{label} pins `@{ref} # v{tagged}`, but on this commit every pin must read "
                f"`@{sha} # v{version}`. The release commit leaves the pins alone, and step 10 of "
                "docs/releasing.md (scripts/bump_action_pins.py) moves the SHA and the comment "
                "together once the tag is pushed"
            )
        elif match := re.fullmatch(r"v(?P<tagged>\d+\.\d+\.\d+)", ref):
            assert match.group("tagged") == version, (
                f"{label} pins `@{ref}`, not the release this commit should name, `v{version}`"
            )
        # Anything else (a branch name, an empty ref) isn't a version pin this check applies to.


def test_every_shipped_uses_line_pins_the_release_this_commit_should_name() -> None:
    # A `uses:` line that names a real, valid, non-floating ref still goes stale the moment a new
    # version ships: v0.1.0 stayed pinned here through 0.1.1 and 0.1.2, so a reader copying the
    # README installed a version with a bug 0.1.1 had already fixed, and got an action.yml with no
    # `command` input, one shipped later. `test_no_shipped_uses_line_pins_a_floating_tag` above
    # only checks the ref isn't unsatisfiable, not that it's current -- this closes that gap.
    version = check_release_tag.packaged_version()
    accepted = accepted_pins(
        version, _remote_tag_refs(), _head_commit(), _pin_files_differ_from_head()
    )
    texts = {
        str(path.relative_to(REPO)): path.read_text(encoding="utf-8")
        for path in DOCS_SHOWING_A_USES_LINE
    }
    check_pins_name_one_of(texts, accepted)


# --- the comment names the release its SHA belongs to, offline ----------------------------------


def pin_label_problems(text: str, label: str, refs: dict[str, str]) -> tuple[list[str], set[str]]:
    """Every SHA pin whose `# vX.Y.Z` comment names a release whose tag is another commit.

    Returns the problems, and the versions whose tags `refs` does not hold, so the caller can say
    which pins it could not judge rather than passing them quietly. A pin with no comment is not
    this function's business: the currency check above fails it."""
    problems: list[str] = []
    missing: set[str] = set()
    for pin in _USES_WITH_LABEL.finditer(text):
        ref, tagged = pin["ref"], pin["label"]
        if tagged is None or not _SHA.match(ref):
            continue
        tag_sha = bump_action_pins.commit_of_tag(tagged, refs)
        if tag_sha is None:
            missing.add(tagged)
        elif tag_sha != ref:
            problems.append(
                f"{label} pins `@{ref} # v{tagged}`, but v{tagged} is `{tag_sha}`. The comment "
                "and the SHA name different releases"
            )
    return problems, missing


def _local_tag_refs() -> dict[str, str]:
    """This clone's own tags, mapped to their SHAs, with no network.

    `git show-ref --tags -d` prints the same refs `git ls-remote` does, `^{}` lines included, but
    space-separated. It exits 1 when there are no tags at all, which is an empty answer, not an
    error. Anything else git cannot answer raises, and the caller turns that into a skip that says
    so."""
    result = subprocess.run(
        ["git", "show-ref", "--tags", "-d"],
        cwd=REPO,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode not in (0, 1):
        raise OSError(f"`git show-ref` exited {result.returncode}: {result.stderr.strip()}")
    return {
        ref: sha for sha, ref in (line.split(" ", 1) for line in result.stdout.splitlines() if line)
    }


@pytest.mark.parametrize("path", DOCS_SHOWING_A_USES_LINE, ids=lambda p: p.name)
def test_every_pins_comment_names_the_release_its_sha_belongs_to(path: Path) -> None:
    """The guard for the 0.1.5 defect that needs no network: each `# vX.Y.Z` comment must name the
    release whose tag is the SHA beside it. It reads this clone's own tags. Where a tag a pin names
    has not been fetched (CI's shallow checkout fetches none), it skips and names the missing tags,
    rather than passing; the currency check above, which asks origin, still covers that pin."""
    label = str(path.relative_to(REPO))
    try:
        refs = _local_tag_refs()
    except OSError as exc:
        pytest.skip(f"NOTCHECKED: git could not list this clone's tags ({exc})")
    problems, missing = pin_label_problems(path.read_text(encoding="utf-8"), label, refs)
    assert not problems, "\n".join(problems)
    if missing:
        names = ", ".join(f"v{version}" for version in sorted(missing))
        pytest.skip(
            f"NOTCHECKED: {label} names {names}, which this clone has not fetched, so those pins "
            "could not be compared with their tags. Run `git fetch --tags` to check them."
        )


#: The SHAs the 0.1.5 defect involved, written out so the proof below needs no tags at all.
_V014 = "f1178ea01bc06a1e4ed2f8415e6351ddd34af608"
_V015 = "bf611f70d52d985aa217cabcef53afaa29c5b0e7"
_REAL_REFS = {"refs/tags/v0.1.4": _V014, "refs/tags/v0.1.5": _V015}


def test_the_pin_0_1_5_shipped_on_pypi_is_caught() -> None:
    # The exact line in the README 0.1.5 shipped: 0.1.4's commit, commented v0.1.5.
    shipped = f"      - uses: kphutt/gdmutant@{_V014} # v0.1.5\n"
    problems, missing = pin_label_problems(shipped, "README.md", _REAL_REFS)
    assert missing == set()
    assert len(problems) == 1 and "name different releases" in problems[0]


def test_a_pin_whose_comment_matches_its_sha_passes() -> None:
    for sha, version in ((_V014, "0.1.4"), (_V015, "0.1.5")):
        assert pin_label_problems(_pin(version, sha), "README.md", _REAL_REFS) == ([], set())


def test_a_pin_naming_an_unfetched_tag_is_reported_missing_not_passed() -> None:
    problems, missing = pin_label_problems(_pin("0.1.9"), "README.md", _REAL_REFS)
    assert (problems, missing) == ([], {"0.1.9"})


def test_the_label_check_reads_an_annotated_tags_commit() -> None:
    refs = {"refs/tags/v0.1.5": "c" * 40, "refs/tags/v0.1.5^{}": _V015}
    assert pin_label_problems(_pin("0.1.5", _V015), "x", refs) == ([], set())
    problems, _ = pin_label_problems(_pin("0.1.5", "c" * 40), "x", refs)
    assert len(problems) == 1


def test_local_tag_refs_answers_empty_in_a_repo_with_no_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    assert _local_tag_refs() == {}


def test_local_tag_refs_raises_where_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_MODULE, "REPO", tmp_path / "does-not-exist")
    with pytest.raises(OSError):
        _local_tag_refs()


def test_local_tag_refs_raises_outside_a_repository_rather_than_answering_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git exits 128 here. Only exit 1 means "no tags", so this must not come back as an empty map.
    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    with pytest.raises(OSError, match="exited 128"):
        _local_tag_refs()


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
_PREV_SHA = "b" * 40
_LATER_SHA = "c" * 40


def _bare_pin(sha: str = _FAKE_SHA) -> str:
    """A pin with no `# vX.Y.Z` comment: the shape no check can tie to a release."""
    return f"      - uses: kphutt/gdmutant@{sha}\n"


def _pin(version: str, sha: str = _FAKE_SHA) -> str:
    return f"      - uses: kphutt/gdmutant@{sha} # v{version}\n"


def _refs(**tags: str) -> dict[str, str]:
    """Tag refs from keyword pairs: `_refs(v0_1_2="b"*40)` is `refs/tags/v0.1.2`."""
    return {f"refs/tags/{name.replace('_', '.')}": sha for name, sha in tags.items()}


# --- which release the pins must name -----------------------------------------------------------


def test_before_the_tag_the_pins_name_the_previous_release() -> None:
    # The release window that used to deadlock: version bumped, tag not pushed. It is now a strict
    # check, both halves, because the previous release's tag exists.
    refs = _refs(v0_1_2=_PREV_SHA)
    assert expected_pin("0.1.3", refs, _LATER_SHA) == ("0.1.2", _PREV_SHA)


def test_on_the_tagged_commit_the_pins_still_name_the_previous_release() -> None:
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    assert expected_pin("0.1.3", refs, _FAKE_SHA) == ("0.1.2", _PREV_SHA)


def test_any_other_commit_after_the_tag_names_the_new_release() -> None:
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    assert expected_pin("0.1.3", refs, _LATER_SHA) == ("0.1.3", _FAKE_SHA)


def test_an_unknown_head_still_expects_the_new_release_rather_than_loosening_the_check() -> None:
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    assert expected_pin("0.1.3", refs, None) == ("0.1.3", _FAKE_SHA)


def test_with_no_earlier_release_there_is_nothing_a_pin_could_name() -> None:
    with pytest.raises(AssertionError, match="no release older than v0.1.0"):
        expected_pin("0.1.0", {}, _LATER_SHA)


# --- the pins against that release --------------------------------------------------------------


def test_a_pin_naming_the_expected_release_at_its_commit_passes() -> None:
    check_pins_are_current(_pin("0.1.3") + _pin("0.1.3"), "fake.md", "0.1.3", _FAKE_SHA)


def test_a_release_commit_that_bumps_the_comment_alone_fails() -> None:
    # The 0.1.5 defect, as the release window sees it: comment moved to the version being cut,
    # SHA still the previous release's. The suite now fails the release pull request that does it.
    with pytest.raises(AssertionError, match="leaves the pins alone"):
        check_pins_are_current(_pin("0.1.3", _PREV_SHA), "fake.md", "0.1.2", _PREV_SHA)


def test_a_stale_pin_fails_once_the_tag_exists() -> None:
    with pytest.raises(AssertionError, match=r"must read `@a{40} # v0\.1\.3`"):
        check_pins_are_current(_pin("0.1.2", _PREV_SHA), "fake.md", "0.1.3", _FAKE_SHA)


def test_a_right_sha_under_the_wrong_comment_fails() -> None:
    with pytest.raises(AssertionError, match="must read"):
        check_pins_are_current(_pin("0.1.2", _FAKE_SHA), "fake.md", "0.1.3", _FAKE_SHA)


def test_a_pin_with_no_version_comment_fails() -> None:
    with pytest.raises(AssertionError, match="no `# vX.Y.Z` comment"):
        check_pins_are_current(_bare_pin(), "fake.md", "0.1.3", _FAKE_SHA)


def test_a_mix_of_commented_and_bare_pins_fails() -> None:
    # A file with one good pin beside one bare pin must not pass on the strength of the good one.
    with pytest.raises(AssertionError, match="no `# vX.Y.Z` comment"):
        check_pins_are_current(_pin("0.1.3") + _bare_pin(), "fake.md", "0.1.3", _FAKE_SHA)


def test_a_full_version_tag_ref_must_name_the_expected_release() -> None:
    check_pins_are_current("uses: kphutt/gdmutant@v0.1.3\n", "fake.md", "0.1.3", _FAKE_SHA)
    with pytest.raises(AssertionError, match="not the release this commit should name"):
        check_pins_are_current("uses: kphutt/gdmutant@v0.1.2\n", "fake.md", "0.1.3", _FAKE_SHA)


def test_a_file_with_no_uses_line_fails() -> None:
    with pytest.raises(AssertionError, match="shows no `uses:` line"):
        check_pins_are_current("no pins here\n", "fake.md", "0.1.3", _FAKE_SHA)


def test_the_release_gate_on_the_tagged_commit_passes_with_the_previous_releases_pins() -> None:
    """End to end through both functions. On the tagged commit the docs pin the previous release,
    SHA and comment alike. That has to pass, or `publish.yml` can never publish. The same pins on
    a later commit must fail, so the stale-pin protection is intact."""
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    docs = _pin("0.1.2", _PREV_SHA)
    check_pins_are_current(docs, "fake.md", *expected_pin("0.1.3", refs, _FAKE_SHA))
    with pytest.raises(AssertionError, match="must read"):
        check_pins_are_current(docs, "fake.md", *expected_pin("0.1.3", refs, _LATER_SHA))


def test_an_unreleased_version_has_no_tag_on_origin_and_a_released_one_does() -> None:
    assert bump_action_pins.commit_of_tag("99.99.99", _remote_tag_refs()) is None
    # A version that really is tagged still resolves, so the None path cannot swallow everything.
    assert (
        bump_action_pins.commit_of_tag("0.1.2", _remote_tag_refs())
        == "284f185f1495f2d79150781cf2e6de618ed11327"
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


#: Any SHA-pinned `uses:` line, wherever it appears -- the shape the scan below looks for.
_PIN = re.compile(rb"kphutt/gdmutant@[0-9a-f]{40}")


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
    rewrite it. So scan every tracked file for SHA pins and compare the files found with the list.
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


def _pin_line(sha: str) -> str:
    return f"      - uses: kphutt/gdmutant@{sha} # v0.1.0\n"


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
    (tmp_path / "README.md").write_text(_pin_line("a" * 40), encoding="utf-8")
    _commit_all(tmp_path)

    copy = tmp_path / "ignored" / "README.md"
    copy.parent.mkdir()
    copy.write_text(_pin_line("b" * 40), encoding="utf-8")
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
    (tmp_path / "README.md").write_text(_pin_line("a" * 40), encoding="utf-8")
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


# --- step 10 before its commit ------------------------------------------------------------------


def test_step_10_before_its_commit_accepts_the_new_tags_pins() -> None:
    # HEAD is the tagged commit and the pin files are edited: the bump has run, not yet committed.
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    accepted = accepted_pins("0.1.3", refs, _FAKE_SHA, pins_dirty=True)
    assert accepted == [("0.1.2", _PREV_SHA), ("0.1.3", _FAKE_SHA)]
    check_pins_name_one_of({"fake.md": _pin("0.1.3", _FAKE_SHA)}, accepted)
    check_pins_name_one_of({"fake.md": _pin("0.1.2", _PREV_SHA)}, accepted)


def test_a_clean_tagged_commit_still_accepts_only_the_previous_release() -> None:
    # The tree publish.yml's gate checks out. The new tag's pins cannot be there, so they must fail.
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    accepted = accepted_pins("0.1.3", refs, _FAKE_SHA, pins_dirty=False)
    assert accepted == [("0.1.2", _PREV_SHA)]
    with pytest.raises(AssertionError, match=r"must read `@b{40} # v0\.1\.2`"):
        check_pins_name_one_of({"fake.md": _pin("0.1.3", _FAKE_SHA)}, accepted)


def test_edited_pin_files_loosen_nothing_away_from_the_tagged_commit() -> None:
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    assert accepted_pins("0.1.3", refs, _LATER_SHA, pins_dirty=True) == [("0.1.3", _FAKE_SHA)]
    assert accepted_pins("0.1.3", _refs(v0_1_2=_PREV_SHA), _LATER_SHA, pins_dirty=True) == [
        ("0.1.2", _PREV_SHA)
    ]


def test_the_extra_pair_never_lets_a_file_mix_two_releases() -> None:
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    accepted = accepted_pins("0.1.3", refs, _FAKE_SHA, pins_dirty=True)
    mixed = _pin("0.1.2", _PREV_SHA) + _pin("0.1.3", _FAKE_SHA)
    # Both pairs fail, and the message is the strict pair's: the previous release.
    with pytest.raises(AssertionError, match=r"must read `@b{40} # v0\.1\.2`"):
        check_pins_name_one_of({"fake.md": mixed}, accepted)


def _repo_with_a_pin_file(root: Path) -> None:
    _init_git_repo(root)
    for name in bump_action_pins.PIN_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(_pin("0.1.2", _PREV_SHA), encoding="utf-8")
    (root / "unrelated.txt").write_text("tracked, but not a pin file\n", encoding="utf-8")
    _commit_all(root)


def test_pin_files_differ_from_head_sees_an_edit_and_only_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo_with_a_pin_file(tmp_path)
    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    assert _pin_files_differ_from_head() is False
    # A tracked file that is not a pin file, edited: still no.
    (tmp_path / "unrelated.txt").write_text("edited\n", encoding="utf-8")
    assert _pin_files_differ_from_head() is False
    (tmp_path / "README.md").write_text(_pin("0.1.3"), encoding="utf-8")
    assert _pin_files_differ_from_head() is True


def test_pin_files_differ_from_head_is_false_where_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Outside a repository git exits 128 or 129, and with no directory it cannot start at all.
    # Either way the answer must be "no", which keeps the check strict.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    monkeypatch.setattr(_MODULE, "REPO", tmp_path)
    assert _pin_files_differ_from_head() is False
    monkeypatch.setattr(_MODULE, "REPO", tmp_path / "does-not-exist")
    assert _pin_files_differ_from_head() is False


def test_a_half_done_bump_across_two_files_fails() -> None:
    # Step 10 before its commit, with one file bumped and another left behind. Each file alone
    # names an accepted pair, but not the same one, so the pins as a set are wrong.
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    accepted = accepted_pins("0.1.3", refs, _FAKE_SHA, pins_dirty=True)
    texts = {"README.md": _pin("0.1.3", _FAKE_SHA), "action.yml": _pin("0.1.2", _PREV_SHA)}
    with pytest.raises(AssertionError) as raised:
        check_pins_name_one_of(texts, accepted)
    # The report is the strict pair's, and names the file that is off it.
    assert "README.md pins" in str(raised.value)
    assert "action.yml" not in str(raised.value)


def test_a_complete_bump_across_every_file_passes() -> None:
    refs = _refs(v0_1_2=_PREV_SHA, v0_1_3=_FAKE_SHA)
    accepted = accepted_pins("0.1.3", refs, _FAKE_SHA, pins_dirty=True)
    texts = {"README.md": _pin("0.1.3", _FAKE_SHA), "action.yml": _pin("0.1.3", _FAKE_SHA)}
    check_pins_name_one_of(texts, accepted)


def test_the_strict_report_names_every_file_that_is_off() -> None:
    accepted = [("0.1.2", _PREV_SHA)]
    texts = {
        "README.md": _pin("0.1.3"),
        "action.yml": _pin("0.1.3"),
        "guide.md": _pin("0.1.2", _PREV_SHA),
    }
    with pytest.raises(AssertionError) as raised:
        check_pins_name_one_of(texts, accepted)
    said = str(raised.value)
    assert "README.md pins" in said and "action.yml pins" in said and "guide.md" not in said


def test_no_accepted_pair_fails_rather_than_passing() -> None:
    with pytest.raises(AssertionError, match="no accepted pair"):
        check_pins_name_one_of({"fake.md": _pin("0.1.3")}, [])
