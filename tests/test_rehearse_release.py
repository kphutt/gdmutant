"""scripts/rehearse_release.py: the pure half, which decides what every runbook edit writes and what
the rehearsal's exit code says.

The impure half (a throwaway clone, git, uv) is exercised by running the script itself, which is
what `.github/workflows/rehearse-release.yml` does on every merge. These tests pin the decisions:
which version gets rehearsed, that each runbook edit refuses a tree that has drifted from the
runbook, and that a step which did not run can never add up to a pass.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "rehearse_release", REPO / "scripts" / "rehearse_release.py"
)
assert _spec and _spec.loader
rehearse_release = importlib.util.module_from_spec(_spec)
sys.modules["rehearse_release"] = rehearse_release
_spec.loader.exec_module(rehearse_release)

_bump_spec = importlib.util.spec_from_file_location(
    "bump_action_pins_for_rehearsal", REPO / "scripts" / "bump_action_pins.py"
)
assert _bump_spec and _bump_spec.loader
bump_action_pins = importlib.util.module_from_spec(_bump_spec)
_bump_spec.loader.exec_module(bump_action_pins)

StepFailed = rehearse_release.StepFailed
StepNotRun = rehearse_release.StepNotRun
SHA_A = "a" * 40
SHA_B = "b" * 40


# --- Which version gets rehearsed ---------------------------------------------------------------


def test_next_patch_bumps_only_the_patch_number() -> None:
    assert rehearse_release.next_patch("0.1.3") == "0.1.4"
    assert rehearse_release.next_patch("1.9.9") == "1.9.10"


@pytest.mark.parametrize("version", ["0.1", "0.1.3.4", "0.1.3rc1", "v0.1.3", ""])
def test_next_patch_refuses_anything_but_three_integers(version: str) -> None:
    with pytest.raises(StepFailed, match="X.Y.Z"):
        rehearse_release.next_patch(version)


def test_a_tagged_version_rehearses_the_next_patch() -> None:
    assert rehearse_release.rehearsal_version("0.1.3", {"v0.1.2", "v0.1.3"}) == "0.1.4"


def test_an_untagged_version_is_a_release_in_progress_and_is_rehearsed_as_is() -> None:
    assert rehearse_release.rehearsal_version("0.1.4", {"v0.1.2", "v0.1.3"}) == "0.1.4"


# --- Runbook step 1: the version ---------------------------------------------------------------


def test_the_pyproject_version_line_is_rewritten_and_its_comment_kept() -> None:
    text = '[project]\nname = "gdmutant"\nversion = "0.1.3"   # the packaged version\n'
    new = rehearse_release.set_pyproject_version(text, "0.1.4")
    assert new == '[project]\nname = "gdmutant"\nversion = "0.1.4"   # the packaged version\n'


@pytest.mark.parametrize(
    "text",
    ['name = "gdmutant"\n', 'version = "0.1.3"\n[tool.x]\nversion = "9"\n'],
    ids=["none", "two"],
)
def test_the_pyproject_edit_needs_exactly_one_version_line(text: str) -> None:
    with pytest.raises(StepFailed, match="Runbook step 1"):
        rehearse_release.set_pyproject_version(text, "0.1.4")


def test_the_real_pyproject_has_the_one_line_the_runbook_edits() -> None:
    # The edit above is only worth something if the real file still has the shape it expects.
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "0.0.1" in rehearse_release.set_pyproject_version(text, "0.0.1")


# --- Runbook step 1: the pin comments ----------------------------------------------------------


def _pin(sha: str, version: str) -> str:
    return f"uses: kphutt/gdmutant@{sha} # v{version}"


def test_every_pin_comment_moves_to_the_new_version_and_every_sha_stays() -> None:
    text = f"{_pin(SHA_A, '0.1.3')}\nprose v0.1.3 stays\n{_pin(SHA_B, '0.1.3')}\n"
    new = rehearse_release.bump_pin_comments(text, bump_action_pins.PIN, "0.1.3", "0.1.4", "x")
    assert new == f"{_pin(SHA_A, '0.1.4')}\nprose v0.1.3 stays\n{_pin(SHA_B, '0.1.4')}\n"


def test_a_pin_already_on_the_new_version_is_left_alone() -> None:
    text = f"{_pin(SHA_A, '0.1.4')}\n{_pin(SHA_B, '0.1.3')}\n"
    new = rehearse_release.bump_pin_comments(text, bump_action_pins.PIN, "0.1.3", "0.1.4", "x")
    assert new == f"{_pin(SHA_A, '0.1.4')}\n{_pin(SHA_B, '0.1.4')}\n"


def test_a_pin_an_earlier_release_missed_fails_and_names_its_version() -> None:
    text = f"{_pin(SHA_A, '0.1.3')}\n{_pin(SHA_B, '0.1.1')}\n"
    with pytest.raises(StepFailed, match=r"README\.md has a pin comment naming 0\.1\.1"):
        rehearse_release.bump_pin_comments(
            text, bump_action_pins.PIN, "0.1.3", "0.1.4", "README.md"
        )


def test_a_pin_file_with_no_pin_fails() -> None:
    with pytest.raises(StepFailed, match="carries no"):
        rehearse_release.bump_pin_comments("no pins", bump_action_pins.PIN, "0.1.3", "0.1.4", "x")


# --- Runbook step 2: the changelog -------------------------------------------------------------


def test_the_unreleased_heading_is_dated_and_nothing_else_moves() -> None:
    text = "# Changelog\n\n## [Unreleased]\n\n- a\n\n## [0.1.3] - 2026-09-01\n"
    new = rehearse_release.date_changelog(text, "0.1.4", "2026-09-25")
    assert new == "# Changelog\n\n## [0.1.4] - 2026-09-25\n\n- a\n\n## [0.1.3] - 2026-09-01\n"


def test_a_changelog_already_dated_for_this_version_is_left_alone() -> None:
    text = "## [0.1.4] - 2026-09-20\n\n- a\n"
    assert rehearse_release.date_changelog(text, "0.1.4", "2026-09-25") == text


def test_a_changelog_whose_top_is_the_last_release_fails() -> None:
    with pytest.raises(StepFailed, match="Add an Unreleased section"):
        rehearse_release.date_changelog("## [0.1.3] - 2026-09-01\n", "0.1.4", "2026-09-25")


def test_a_changelog_with_no_heading_fails() -> None:
    with pytest.raises(StepFailed, match="no `## \\[...\\]` heading"):
        rehearse_release.date_changelog("# Changelog\n", "0.1.4", "2026-09-25")


def test_the_dated_changelog_passes_the_real_release_guard() -> None:
    # The two halves must agree: what the rehearsal writes is what check_release_tag.py accepts.
    spec = importlib.util.spec_from_file_location(
        "check_release_tag_for_rehearsal", REPO / "scripts" / "check_release_tag.py"
    )
    assert spec and spec.loader
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    dated = rehearse_release.date_changelog("## [Unreleased]\n", "0.1.4", "2026-09-25")
    heading = guard.CHANGELOG_HEADING.match(dated.strip())
    assert heading and heading.group("label") == "0.1.4" and heading.group("date") == "2026-09-25"


# --- The banner URL, fetched at the rehearsed commit -------------------------------------------


def test_the_tag_pinned_banner_url_is_fetched_at_the_commit_instead() -> None:
    url = "https://raw.githubusercontent.com/kphutt/gdmutant/v0.1.4/.github/assets/banner.png"
    expected = (
        f"https://raw.githubusercontent.com/kphutt/gdmutant/{SHA_A}/.github/assets/banner.png"
    )
    assert rehearse_release.rewrite_tag_url(url, "0.1.4", SHA_A) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://raw.githubusercontent.com/kphutt/gdmutant/v0.1.3/.github/assets/banner.png",
        "https://img.shields.io/pypi/v/gdmutant",
    ],
    ids=["another-tag", "a-badge"],
)
def test_every_other_url_is_fetched_as_written(url: str) -> None:
    assert rehearse_release.rewrite_tag_url(url, "0.1.4", SHA_A) == url


def test_the_rewrite_matches_what_the_build_writes() -> None:
    # TAGGED_RAW has to track pyproject.toml's substitution, or the rewrite silently never fires
    # and the banner is fetched at a tag that does not exist.
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    written = re.search(r"replacement = 'src=\"(?P<url>[^']+?)\.github/assets/'", pyproject)
    assert written, "pyproject.toml no longer rewrites the banner src"
    built = written.group("url").replace("$HFPR_VERSION", "0.1.4")
    assert built == rehearse_release.TAGGED_RAW.format(version="0.1.4")


# --- pytest's command line ---------------------------------------------------------------------


def test_the_full_run_keeps_the_coverage_floor() -> None:
    args = rehearse_release.pytest_args(quick=False)
    assert "--no-cov" not in args and args[-1] == "no:cacheprovider"


def test_the_quick_run_drops_coverage_and_names_only_existing_tests() -> None:
    args = rehearse_release.pytest_args(quick=True)
    assert "--no-cov" in args
    for name in rehearse_release.QUICK_TESTS:
        assert name in args and (REPO / name).is_file(), name


def test_pytest_prints_the_warning_lines_step_10_reads() -> None:
    flags = next(arg for arg in rehearse_release.PYTEST if arg.startswith("-r"))
    assert "w" in flags and "f" in flags


# --- Why a failed command failed ---------------------------------------------------------------


def _result(stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout, stderr)


def test_pytest_failures_are_named_rather_than_the_coverage_table() -> None:
    out = "FAILED tests/a.py::t1\nERROR tests/b.py::t2\nTOTAL 100%\n1 failed, 1 error"
    assert rehearse_release.evidence(_result(out)) == (
        "FAILED tests/a.py::t1\nERROR tests/b.py::t2\n1 failed, 1 error"
    )


def test_other_failures_show_the_end_of_the_output() -> None:
    out = "\n".join(f"line {n}" for n in range(30))
    shown = rehearse_release.evidence(_result(out, "boom")).splitlines()
    assert shown[-1] == "boom" and len(shown) == 15


def test_pytest_summary_is_the_last_nonblank_line() -> None:
    assert rehearse_release.pytest_summary(_result("a\n5 passed\n\n")) == "5 passed"
    assert rehearse_release.pytest_summary(_result("")) == "pytest printed nothing"


# --- The verdict: nothing that did not run adds up to a pass ------------------------------------


def _passes() -> str:
    return "fine"


def _fails() -> str:
    raise StepFailed("broken")


def _cannot() -> str:
    raise StepNotRun("no network")


def _crashes() -> str:
    raise KeyError("x")


def test_all_passing_steps_exit_zero() -> None:
    rehearsal = rehearse_release.Rehearsal()
    assert rehearsal.run("a", _passes) and rehearsal.run("b", _passes)
    assert rehearsal.exit_code() == rehearse_release.OK
    assert rehearsal.report().endswith("the release path is clear")


def test_a_failed_step_stops_the_chain_and_exits_one() -> None:
    rehearsal = rehearse_release.Rehearsal()
    assert rehearsal.run("a", _passes)
    assert not rehearsal.run("b", _fails)
    assert rehearsal.exit_code() == rehearse_release.FAILED
    assert "FAIL   b" in rehearsal.report() and "broken" in rehearsal.report()


def test_a_step_that_could_not_run_lets_the_chain_go_on_but_is_never_a_pass() -> None:
    rehearsal = rehearse_release.Rehearsal()
    assert rehearsal.run("a", _cannot)
    assert rehearsal.run("b", _passes)
    assert rehearsal.exit_code() == rehearse_release.NOT_RUN
    assert rehearsal.report().endswith("so this is not a pass")


def test_a_failure_outranks_a_step_that_did_not_run() -> None:
    rehearsal = rehearse_release.Rehearsal()
    rehearsal.run("a", _cannot)
    rehearsal.run("b", _fails)
    assert rehearsal.exit_code() == rehearse_release.FAILED


def test_a_crash_is_a_failure_not_a_traceback() -> None:
    rehearsal = rehearse_release.Rehearsal()
    assert not rehearsal.run("a", _crashes)
    assert rehearsal.steps[0].state == "FAIL" and "KeyError" in rehearsal.steps[0].detail


def test_skipped_steps_are_reported_with_the_reason() -> None:
    rehearsal = rehearse_release.Rehearsal()
    rehearsal.run("a", _fails)
    rehearsal.skip_rest(["b", "c"], "an earlier step failed (a)")
    assert [s.state for s in rehearsal.steps] == ["FAIL", "NOTRUN", "NOTRUN"]
    assert rehearsal.steps[2].detail == "an earlier step failed (a)"


def test_an_empty_rehearsal_is_not_a_pass() -> None:
    assert rehearse_release.Rehearsal().exit_code() == rehearse_release.NOT_RUN


def test_a_missing_tool_stops_before_anything_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rehearse_release.shutil, "which", lambda tool: None)
    rehearsal = rehearse_release.rehearse(quick=True, keep=False)
    assert rehearsal.exit_code() == rehearse_release.NOT_RUN
    assert rehearsal.steps[0].detail == "`git` is not on PATH"
