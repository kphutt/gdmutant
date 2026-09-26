"""Tests for the runner interface + JUnit-XML parsing."""

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest

from gdmutant.engine.runner import (
    CommandRunner,
    Runner,
    SuiteResult,
    SuiteTimeout,
    parse_junit_xml,
    suite_seconds,
    with_filename,
)


@dataclass
class FakeRunner:
    """A Runner that returns a preset result (used to drive engine tests without Godot)."""

    result: SuiteResult
    calls: list[str] = field(default_factory=list)

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        self.calls.append(project_dir)
        return self.result


def test_suite_result_passed_and_failed() -> None:
    assert SuiteResult(tests=3, failures=0, errors=0).passed is True
    assert SuiteResult(tests=3, failures=0, errors=0).failed is False
    assert SuiteResult(tests=3, failures=1, errors=0).failed is True
    assert SuiteResult(tests=3, failures=0, errors=2).failed is True


def test_parse_single_suite() -> None:
    r = parse_junit_xml('<testsuite name="s" tests="4" failures="1" errors="0" skipped="1"/>')
    assert (r.tests, r.failures, r.errors, r.skipped) == (4, 1, 0, 1)
    assert r.failed is True


def test_parse_nested_suites_are_summed() -> None:
    xml = (
        "<testsuites>"
        '<testsuite tests="2" failures="0" errors="0"/>'
        '<testsuite tests="3" failures="0" errors="1"/>'
        "</testsuites>"
    )
    r = parse_junit_xml(xml)
    assert (r.tests, r.failures, r.errors) == (5, 0, 1)
    assert r.failed is True


def test_parse_nested_testsuite_is_not_double_counted() -> None:
    # A <testsuite> may nest child <testsuite>s whose totals already roll up into the parent's
    # attributes; sum only the outer suite, don't descend (regression: no double-count).
    xml = '<testsuite tests="5" failures="1"><testsuite tests="2" failures="1"/></testsuite>'
    r = parse_junit_xml(xml)
    assert (r.tests, r.failures) == (5, 1)


def test_parse_all_green_passes() -> None:
    xml = '<testsuites><testsuite tests="5" failures="0" errors="0"/></testsuites>'
    assert parse_junit_xml(xml).passed is True


def test_parse_missing_attributes_default_to_zero() -> None:
    r = parse_junit_xml('<testsuite tests="2"/>')
    assert (r.failures, r.errors, r.skipped) == (0, 0, 0)
    assert r.passed is True


def test_parse_no_testsuite_raises() -> None:
    # Anchored so a mutant that wraps or re-cases the message ("...JUnit XML" -> "...junit xml")
    # is still caught, not just any string containing "no <testsuite>".
    with pytest.raises(ValueError, match=r"^no <testsuite> element in JUnit XML$"):
        parse_junit_xml("<other/>")


def test_parse_missing_count_attr_defaults_to_zero() -> None:
    # With the `tests` attribute absent, the "0" default must be used (not a crash): a mutant that
    # drops or corrupts that default (None, "XX0XX") would raise on int() instead.
    r = parse_junit_xml('<testsuite failures="0" errors="0"/>')
    assert (r.tests, r.failures, r.errors) == (0, 0, 0)
    assert r.passed is True


def test_parse_sums_every_field_across_suites() -> None:
    # Two suites each contributing to every field, with distinct values, so a mutant that assigns
    # (`x = ...`) instead of accumulating (`x += ...`) yields the last suite's value, not the sum.
    xml = (
        "<testsuites>"
        '<testsuite tests="1" failures="1" errors="1" skipped="1"/>'
        '<testsuite tests="1" failures="2" errors="3" skipped="4"/>'
        "</testsuites>"
    )
    r = parse_junit_xml(xml)
    assert (r.tests, r.failures, r.errors, r.skipped) == (2, 3, 4, 5)


def test_parse_malformed_xml_raises() -> None:
    with pytest.raises(ParseError):
        parse_junit_xml("<not closed")


def test_fake_runner_satisfies_the_protocol() -> None:
    fake = FakeRunner(SuiteResult(tests=1, failures=0, errors=0))
    assert isinstance(fake, Runner)
    runner: Runner = fake  # static conformance
    assert runner.run("some/dir").passed
    assert fake.calls == ["some/dir"]


def _exits(code: int) -> list[str]:
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def test_command_runner_exit_zero_is_a_passing_suite(tmp_path: Path) -> None:
    result = CommandRunner(_exits(0)).run(str(tmp_path))
    assert result.passed is True
    assert (result.tests, result.failures, result.errors) == (1, 0, 0)


def test_command_runner_nonzero_exit_is_a_failing_suite(tmp_path: Path) -> None:
    # Any non-zero exit — not just 1 — means the suite failed (a mutant was killed).
    for code in (1, 2, 127):
        result = CommandRunner(_exits(code)).run(str(tmp_path))
        assert result.failed is True, f"exit {code} should be a failure"
        assert (result.tests, result.failures) == (1, 1)


def test_command_runner_runs_in_the_project_dir(tmp_path: Path) -> None:
    # cwd must be the project dir: the command exits 0 only if it sees a marker file in cwd.
    (tmp_path / "marker").write_text("x", encoding="utf-8")
    cmd = [sys.executable, "-c", "import os, sys; sys.exit(0 if os.path.exists('marker') else 1)"]
    runner = CommandRunner(cmd)
    assert runner.run(str(tmp_path)).passed is True
    other = tmp_path / "other"
    other.mkdir()
    assert runner.run(str(other)).failed is True  # marker not visible from a different cwd


def test_command_runner_satisfies_the_protocol() -> None:
    assert isinstance(CommandRunner(_exits(0)), Runner)


def test_command_runner_timeout_raises_suite_timeout(tmp_path: Path) -> None:
    # A command that outruns its budget raises SuiteTimeout (not a leaked TimeoutExpired), so the
    # engine can tally it as a Timeout detection rather than a generic error.
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(SuiteTimeout):
        CommandRunner(slow, timeout=0.2).run(str(tmp_path))


def test_command_runner_failure_captures_output_as_detail(tmp_path: Path) -> None:
    # A failed run keeps the command's own output (so a baseline misconfiguration is debuggable,
    # not silently swallowed). Prefers stderr.
    cmd = [
        sys.executable,
        "-c",
        "import sys; print('boom-on-stderr', file=sys.stderr); sys.exit(1)",
    ]
    result = CommandRunner(cmd).run(str(tmp_path))
    assert result.failed is True
    assert "boom-on-stderr" in result.detail


def test_command_runner_success_has_no_detail(tmp_path: Path) -> None:
    assert CommandRunner(_exits(0)).run(str(tmp_path)).detail == ""


def _prints_then_exits(text: str, code: int, *, stderr: bool = False) -> list[str]:
    stream = "sys.stderr" if stderr else "sys.stdout"
    script = f"import sys; print({text!r}, file={stream}); sys.exit({code})"
    return [sys.executable, "-c", script]


def test_command_runner_script_error_in_output_is_an_error_even_on_exit_zero(
    tmp_path: Path,
) -> None:
    # The exact case a bare exit-code check can't catch (docs/decisions/0015): GDScript has no
    # exceptions, so a runtime error can leave a half-executed test still exiting 0.
    cmd = _prints_then_exits("SCRIPT ERROR: Nonexistent function 'foo'", 0)
    result = CommandRunner(cmd).run(str(tmp_path))
    assert result.failed is True
    assert (result.tests, result.failures, result.errors) == (1, 0, 1)
    assert "SCRIPT ERROR" in result.detail
    assert "GDScript has no exceptions" in result.detail


def test_command_runner_script_error_on_stderr_is_also_caught(tmp_path: Path) -> None:
    cmd = _prints_then_exits("SCRIPT ERROR: boom", 0, stderr=True)
    result = CommandRunner(cmd).run(str(tmp_path))
    assert (result.failures, result.errors) == (0, 1)


def test_command_runner_script_error_takes_precedence_over_a_nonzero_exit(tmp_path: Path) -> None:
    # A command that both errors AND exits non-zero is still reported via `errors`, not `failures`:
    # the SCRIPT ERROR is the more specific, more actionable diagnosis of the two.
    cmd = _prints_then_exits("SCRIPT ERROR: boom", 1)
    result = CommandRunner(cmd).run(str(tmp_path))
    assert (result.failures, result.errors) == (0, 1)


def test_command_runner_without_script_error_is_unaffected(tmp_path: Path) -> None:
    # A non-Godot command (or a Godot one that never hits this) is untouched: ordinary exit-code
    # pass/fail, no `errors`.
    result = CommandRunner(_exits(0)).run(str(tmp_path))
    assert (result.failures, result.errors) == (0, 0)


def test_command_runner_missing_executable_raises(tmp_path: Path) -> None:
    # A command that can't be executed at all raises (the engine tallies it as ERROR / the CLI
    # surfaces the not-found hint) — it is never silently treated as a passing or failing suite.
    with pytest.raises(FileNotFoundError):
        CommandRunner(["gdmutant-no-such-binary-xyz"]).run(str(tmp_path))


def test_command_runner_names_the_attempted_command_when_the_os_omits_the_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CommandRunner is the third (framework-neutral, exit-code) call site into
    # subprocess.run for a "godot"/test-runner binary. Like the two GdUnit4/GUT JUnit call
    # sites, it must patch a Windows CreateProcess FileNotFoundError (.filename == None) back
    # in with the attempted command, so the CLI's missing-executable hint
    # (`_missing_executable` in cli.py) can name the actual bad path instead of falling back
    # to a generic placeholder.
    import gdmutant.engine.runner as runner_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(runner_mod.subprocess, "run", boom)
    with pytest.raises(FileNotFoundError) as excinfo:
        CommandRunner(["/nonexistent/bad-runner", "--flag"]).run(str(tmp_path))
    assert excinfo.value.filename == "/nonexistent/bad-runner"


def test_with_filename_leaves_an_already_named_error_alone() -> None:
    # POSIX already sets .filename on a missing-executable FileNotFoundError — don't touch it.
    error = FileNotFoundError(2, "No such file or directory", "/already/set")
    assert with_filename(error, "/attempted/godot") is error


def test_with_filename_patches_a_filename_less_error() -> None:
    # Windows' CreateProcess failure leaves .filename None (verified live) — the CLI's
    # missing-executable hint needs a name to show the user, so patch one in.
    error = FileNotFoundError(2, "The system cannot find the file specified")
    assert error.filename is None
    patched = with_filename(error, "/attempted/godot")
    assert patched.filename == "/attempted/godot"
    assert patched.errno == 2


def test_a_suites_reported_duration_is_read_and_added_up() -> None:
    # The measurement the per-mutant budget rests on. A mutant can make the tests slower and can
    # do nothing at all about the framework's startup, so the two have to be told apart, and the
    # report is what tells them apart for free.
    result = parse_junit_xml(
        "<testsuites>"
        '<testsuite name="a" tests="2" failures="0" time="1.25"/>'
        '<testsuite name="b" tests="3" failures="0" time="0.75"/>'
        "</testsuites>"
    )
    assert [suite.time for suite in result.suites] == [1.25, 0.75]
    assert result.reported_time == 2.0


def test_a_report_with_no_durations_reports_no_test_time() -> None:
    # Not an error and not a zero measurement: it is "this report did not say". The engine reads
    # that one answer and falls back to multiplying the whole wall-clock, which is the safe
    # direction. A report that named zero and one that named nothing must look the same here,
    # because nothing downstream could tell them apart anyway.
    result = parse_junit_xml('<testsuite name="a" tests="1" failures="0"/>')
    assert result.reported_time == 0.0
    assert result.suites[0].time == 0.0


@pytest.mark.parametrize(
    "raw",
    [
        None,  # the attribute is absent
        "",  # present and empty
        "not-a-number",
        "-1.5",  # a negative duration is not a duration
        "0",  # zero says nothing about how long the tests took
        "nan",  # float() accepts these, and they poison every comparison downstream
        "inf",
        "-inf",
    ],
)
def test_an_unusable_duration_reads_as_no_measurement(raw: str | None) -> None:
    # `nan` is the one that matters most and is the easiest to miss. `float("nan")` succeeds, and
    # a NaN budget compares False against both the floor and the cap, so it would come out of the
    # bounding untouched and every mutant would be ruled a hang the instant it started.
    assert suite_seconds(raw) == 0.0


def test_an_unusable_duration_comes_back_as_a_positive_zero() -> None:
    # The boundary is "greater than zero", not "at least zero", and the difference is visible on
    # a negative zero: `float("-0.0")` is finite and compares equal to zero, so a rule that
    # admitted it would let a signed zero through into the budget arithmetic. Nothing here should
    # ever hand its caller a duration with a sign on it.
    assert math.copysign(1.0, suite_seconds("-0.0")) == 1.0
    assert math.copysign(1.0, suite_seconds("-3.0")) == 1.0
    assert math.copysign(1.0, suite_seconds("0")) == 1.0


def test_a_suite_carries_the_file_its_runner_says_it_belongs_to() -> None:
    # The engine hands these strings straight back to the runner and never parses one, so only the
    # runner may spell them. `file_of` is how it does the spelling, from the two attributes JUnit
    # gives it.
    result = parse_junit_xml(
        "<testsuites>"
        '<testsuite name="test_x" package="test/deep" tests="1" failures="0" time="2.0"/>'
        "</testsuites>",
        file_of=lambda name, package: f"res://{package}/{name}.gd",
    )
    assert result.suites[0].file == "res://test/deep/test_x.gd"
    assert result.file_times == {"res://test/deep/test_x.gd": 2.0}


def test_a_suite_with_no_package_is_handed_an_empty_one() -> None:
    # A report that names no `package` must hand the runner an empty string, not a placeholder.
    # The GdUnit4 spelling turns an empty package into an empty file, which keeps that suite out
    # of the per-file durations and falls back to the whole suite's time. Any other default would
    # build a path that looks real, points nowhere, and no selection would ever ask for.
    seen: list[tuple[str, str]] = []

    def record(name: str, package: str) -> str:
        seen.append((name, package))
        return f"res://{package}/{name}.gd" if package else ""

    result = parse_junit_xml(
        '<testsuite name="lonely" tests="1" failures="0" time="1.0"/>', file_of=record
    )
    assert seen == [("lonely", "")]
    assert result.suites[0].file == ""
    assert result.file_times == {}


def test_without_a_file_of_no_suite_claims_a_file() -> None:
    # The default, and what the exit-code runner and any future runner without the knowledge get.
    # An empty file keeps that suite out of the per-file durations entirely, so a selected mutant
    # falls back to the whole suite's time rather than being budgeted for zero seconds.
    result = parse_junit_xml('<testsuite name="a" tests="1" failures="0" time="3.0"/>')
    assert result.suites[0].file == ""
    assert result.file_times == {}
    assert result.reported_time == 3.0  # the whole-suite figure is still there


def test_several_suites_in_one_file_have_their_times_added_not_overwritten() -> None:
    # A framework may report one file as several suites (an inner class is one). Keeping only the
    # last would budget a mutant for a fraction of the time its file really takes, which is the
    # tight-budget failure this tool refuses.
    result = parse_junit_xml(
        "<testsuites>"
        '<testsuite name="a" package="t" tests="1" failures="0" time="1.0"/>'
        '<testsuite name="a" package="t" tests="1" failures="0" time="2.5"/>'
        '<testsuite name="b" package="t" tests="1" failures="0" time="0.5"/>'
        "</testsuites>",
        file_of=lambda name, package: f"res://{package}/{name}.gd",
    )
    assert result.file_times == {"res://t/a.gd": 3.5, "res://t/b.gd": 0.5}
