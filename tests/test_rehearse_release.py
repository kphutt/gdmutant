"""scripts/rehearse_release.py: the pure half, which decides what every runbook edit writes and what
the rehearsal's exit code says.

The impure half (a throwaway clone, git, uv) is exercised by running the script itself, which is
what `.github/workflows/rehearse-release.yml` does on every merge. These tests pin the decisions:
which version gets rehearsed, that each runbook edit refuses a tree that has drifted from the
runbook, and that a step which did not run can never add up to a pass.
"""

from __future__ import annotations

import importlib.util
import io
import re
import subprocess
import sys
import typing
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
PUBLISH_WORKFLOW = REPO / ".github" / "workflows" / "publish.yml"

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


def test_a_failed_verify_job_names_which_of_its_steps_went_red() -> None:
    """verify_local.py indents its per-step verdicts, so an unindented match reported only
    `FAILED (1/6)` -- a count, with the one thing the reader needs left out of the report."""
    out = (
        "[5/6] Tests + coverage\n  ok\n\n[6/6] Supply-chain audit (pip-audit)\n"
        "  FAILED: Supply-chain audit (pip-audit)\n\n"
        "------\nFAILED (1/6):\n  - Supply-chain audit (pip-audit)"
    )
    shown = rehearse_release.evidence(_result(out))
    assert "FAILED: Supply-chain audit (pip-audit)" in shown
    assert "FAILED (1/6):" in shown


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


def test_the_exit_codes_are_the_numbers_the_workflow_and_the_runbook_promise() -> None:
    """The one thing about this script that another program reads.

    `.github/workflows/rehearse-release.yml` passes or fails the job on the process exit status,
    and the module docstring and docs/releasing.md each tell a reader what the three numbers mean.
    Every other test in this section compares a verdict against these constants, so all of them
    would still pass with the values swapped or shifted. A mutation sweep found exactly that:
    three surviving mutants, one on each of these three lines, and nothing else in the pure half.
    Pinned to the literals here, because the literals are what the contract is.
    """
    assert (rehearse_release.OK, rehearse_release.FAILED, rehearse_release.NOT_RUN) == (0, 1, 2)
    # The half CI acts on: exactly one of the three is a passing exit status.
    assert [rehearse_release.OK, rehearse_release.FAILED, rehearse_release.NOT_RUN].count(0) == 1


def test_a_missing_tool_stops_before_anything_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rehearse_release.shutil, "which", lambda tool: None)
    rehearsal = rehearse_release.rehearse(quick=True, keep=False)
    assert rehearsal.exit_code() == rehearse_release.NOT_RUN
    assert rehearsal.steps[0].detail == "`git` is not on PATH"


def test_a_crash_deciding_what_to_rehearse_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Working out the version used to happen outside the step machinery, so a tree it could not
    read exited 1 with a traceback and no report at all -- the one exit code the docstring
    reserves for "a step ran and found something wrong"."""

    def boom(repo: Path = REPO) -> rehearse_release.Plan:
        raise RuntimeError("pyproject.toml is unreadable")

    monkeypatch.setattr(rehearse_release, "plan_rehearsal", boom)
    rehearsal = rehearse_release.rehearse(quick=True, keep=False)
    assert [step.name for step in rehearsal.steps] == ["preconditions"]
    assert rehearsal.steps[0].state == "FAIL"
    assert "RuntimeError" in rehearsal.steps[0].detail
    assert rehearsal.exit_code() == rehearse_release.FAILED


# --- Refusing to start, rather than rehearsing something that is not the release path -----------


def _repo(path: Path, version: str = "0.1.3") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=path, check=True)
    (path / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", *rehearse_release.COMMITTER, "commit", "--quiet", "-m", "first"],
        cwd=path,
        check=True,
    )
    return path


def test_a_checkout_with_tags_plans_the_next_release(tmp_path: Path) -> None:
    # The positive control: the two refusals below mean nothing if the happy path never runs.
    work = _repo(tmp_path / "ok")
    subprocess.run(["git", "tag", "v0.1.3"], cwd=work, check=True)
    plan = rehearse_release.plan_rehearsal(work)
    assert (plan.version, plan.last, plan.tag) == ("0.1.4", "0.1.3", "v0.1.4")


def test_a_tagless_checkout_refuses_rather_than_rehearsing_a_shipped_version(
    tmp_path: Path,
) -> None:
    """A tagless fetch is indistinguishable from a repository before its first release, and the
    guess it invites is the quiet one: every edit becomes a no-op and the rehearsal passes having
    walked nothing at all."""
    work = _repo(tmp_path / "tagless")
    with pytest.raises(StepNotRun, match="fetch-depth: 0"):
        rehearse_release.plan_rehearsal(work)


def test_an_unpacked_copy_with_no_git_at_all_refuses_to_start(tmp_path: Path) -> None:
    """An unpacked sdist is a tree with no `.git` anywhere above it. `git rev-parse HEAD` there
    used to raise straight out of the rehearsal, past every step the report is made of."""
    copy = tmp_path / "gdmutant-0.1.3"
    copy.mkdir()
    (copy / "pyproject.toml").write_text('[project]\nversion = "0.1.3"\n', encoding="utf-8")
    with pytest.raises(StepNotRun, match="not the root of a git checkout"):
        rehearse_release.plan_rehearsal(copy)


def test_a_mutation_tools_copy_inside_the_checkout_refuses_too(tmp_path: Path) -> None:
    """The case `--is-inside-work-tree` got wrong, and the reason this asks for the toplevel
    instead: mutmut's `mutants/` and a poodle run's temp directory live INSIDE the repository, so
    they are inside a work tree. They are still copies, with a `pyproject.toml` of their own and
    no commit of their own, and a rehearsal there would rehearse the copy."""
    checkout = _repo(tmp_path / "checkout")
    subprocess.run(["git", "tag", "v0.1.3"], cwd=checkout, check=True)
    copy = checkout / "mutants"
    copy.mkdir()
    (copy / "pyproject.toml").write_text('[project]\nversion = "0.1.3"\n', encoding="utf-8")
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=copy,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    assert inside.stdout.strip() == "true", "the premise of this test: the copy IS inside the tree"
    with pytest.raises(StepNotRun, match="not the root of a git checkout"):
        rehearse_release.plan_rehearsal(copy)


# --- The rehearsal's own commits ----------------------------------------------------------------


def test_the_committer_is_passed_per_command_and_never_stored(tmp_path: Path) -> None:
    """The bug this pins turned the suite step red on every single run.

    tests/test_public_readiness.py scans every tracked file for the clone's LOCAL git identity,
    and the rehearsal's own name is in the tree, so an identity written into the clone with
    `git config` made that test find the rehearsal's own source. Reproduced on the branch that
    added this script: four hits, one of them the `git config` line itself.
    """
    work = _repo(tmp_path / "work")
    (work / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(rehearse_release.commit_args("second"), cwd=work, check=True)
    stored = subprocess.run(
        ["git", "config", "--local", "--get", "user.name"],
        cwd=work,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert stored.stdout.strip() == "", (
        "the rehearsal stored a git identity in the clone, which the suite it runs will find"
    )
    # Named, not merely non-empty: this machine has an ambient identity, so a `commit_args` that
    # had dropped the per-command committer would still have produced a commit with an author.
    author = subprocess.run(
        ["git", "log", "-1", "--format=%an"],
        cwd=work,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    wanted = rehearse_release.COMMITTER[1].removeprefix("user.name=")
    assert author.stdout.strip() == wanted


def test_a_clone_that_stored_an_identity_is_refused_with_the_reason() -> None:
    rehearse_release.refuse_a_stored_identity("")  # the shape the clone must have
    with pytest.raises(StepFailed, match="scans every tracked file"):
        rehearse_release.refuse_a_stored_identity("a release dry run")


@pytest.mark.parametrize(
    ("dirty", "last"),
    [(True, "0.1.3"), (True, None), (False, None)],
    ids=["bumped", "bumped-mid-release", "release-in-progress"],
)
def test_an_unchanged_version_bump_is_fine_only_mid_release(dirty: bool, last: str | None) -> None:
    rehearse_release.refuse_an_unchanged_version_bump(dirty, last, "v0.1.4")


def test_a_version_bump_that_changed_nothing_when_it_had_to_fails() -> None:
    with pytest.raises(StepFailed, match="changed nothing at all"):
        rehearse_release.refuse_an_unchanged_version_bump(False, "0.1.3", "v0.1.4")


def test_only_the_version_bump_commit_may_be_empty() -> None:
    """A release already in progress has had every step 1-2 edit made by its release pull request,
    so that commit is genuinely empty and `git commit` exits 1 on it. The step 10 pin bump gets no
    such allowance: an empty commit there means bump_action_pins.py did nothing."""
    assert "--allow-empty" in rehearse_release.commit_args("m", allow_empty=True)
    assert "--allow-empty" not in rehearse_release.commit_args("m")


# --- What the tagged commit is checked with, against what publish.yml checks it with ------------


def _publish_run_steps(job: str) -> list[str]:
    jobs = yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return [step["run"].strip() for step in jobs[job]["steps"] if "run" in step]


@pytest.mark.parametrize(
    ("job", "command"),
    [("verify-ubuntu", rehearse_release.VERIFY), ("license-check", rehearse_release.LICENSE_CHECK)],
    ids=["verify", "license-check"],
)
def test_the_tagged_commit_runs_what_publish_yml_gates_the_upload_on(
    job: str, command: tuple[str, ...]
) -> None:
    """Recurring bug two, pinned: the rehearsal and the real gate are a pair, and the rehearsal is
    the half that can quietly check less. It ran bare `pytest` while publish.yml ran the whole
    `verify` job, so ruff, gdlint, mypy, pip-audit and the license gate were never rehearsed --
    and pip-audit is the one a release can newly fail on its own, because a release regenerates
    `uv.lock`. Both halves now go through scripts/verify_local.py, which reads ci.yml.
    """
    wanted = " ".join(command[command.index("python") :])
    assert any(wanted in step for step in _publish_run_steps(job)), (
        f"publish.yml's {job} job no longer runs `{wanted}`, so the rehearsal is now checking "
        "something other than the gate it exists to rehearse"
    )


def test_the_quick_run_never_claims_the_gate_it_skipped() -> None:
    quick = rehearse_release.gate_args(quick=True)
    assert "scripts/verify_local.py" not in quick and "--no-cov" in quick


def test_step_summary_names_how_many_verify_steps_ran() -> None:
    out = "verify - Linux\n[1/6] Lint\n  ok\nAll 6 steps of jobs.verify passed on Linux.\nNote: CI"
    assert (
        rehearse_release.step_summary(_result(out)) == "All 6 steps of jobs.verify passed on Linux."
    )


def test_step_summary_falls_back_to_pytests_own_last_line() -> None:
    assert rehearse_release.step_summary(_result("a\n5 passed\n")) == "5 passed"


# --- Windows: a report that cannot be printed is not a report -----------------------------------


def test_the_report_prints_on_a_console_that_cannot_encode_it() -> None:
    """gdmutant already shipped a Windows bug where console output crashed under the legacy cp1252
    code page. This is the far end of a chain that starts one function away: `run` decodes a child
    with ``errors="replace"``, which turns a byte it cannot read into U+FFFD -- a character cp1252
    cannot encode. Printing the report would then raise instead of printing the verdict.

    An em dash is not the test for this. cp1252 encodes that one fine (0x97), which is why the
    first version of this test passed against a `print` that would still have crashed.
    """
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    rehearse_release.say("a step said � this", console)
    console.flush()
    written = typing.cast(io.BytesIO, console.buffer).getvalue()
    assert written.replace(b"\r\n", b"\n") == b"a step said ? this\n"


def test_every_child_runs_in_utf8_and_without_the_machines_git_hooks() -> None:
    """Two measured traps, both already fixed one script over in check_mutation_baseline.py: a
    child writing cp1252 bytes that this script decodes as UTF-8, and a global `core.hooksPath`
    firing the operator's own pre-commit gate on every throwaway commit the suite makes."""
    assert rehearse_release.CHILD_ENV["PYTHONUTF8"] == "1"
    assert rehearse_release.CHILD_ENV["PYTHONIOENCODING"] == "utf-8"
    assert rehearse_release.CHILD_ENV["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert rehearse_release.CHILD_ENV["GIT_CONFIG_VALUE_0"] == ""
    assert rehearse_release.CHILD_ENV["GIT_CONFIG_COUNT"] == "1"


def test_a_child_whose_output_is_not_utf8_is_read_rather_than_raising(tmp_path: Path) -> None:
    script = tmp_path / "noise.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(b'before\\xff after')\n", encoding="utf-8"
    )
    result = rehearse_release.run([sys.executable, str(script)], tmp_path)
    assert "before" in result.stdout and "after" in result.stdout
