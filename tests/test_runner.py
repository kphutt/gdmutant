"""Tests for the runner interface + JUnit-XML parsing."""

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pytest

from gdmutant.engine.runner import CommandRunner, Runner, SuiteResult, parse_junit_xml


@dataclass
class FakeRunner:
    """A Runner that returns a preset result (used to drive engine tests without Godot)."""

    result: SuiteResult
    calls: list[str] = field(default_factory=list)

    def run(self, project_dir: str) -> SuiteResult:
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


def test_command_runner_honours_timeout(tmp_path: Path) -> None:
    slow = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(subprocess.TimeoutExpired):
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


def test_command_runner_missing_executable_raises(tmp_path: Path) -> None:
    # A command that can't be executed at all raises (the engine tallies it as ERROR / the CLI
    # surfaces the not-found hint) — it is never silently treated as a passing or failing suite.
    with pytest.raises(FileNotFoundError):
        CommandRunner(["gdmutant-no-such-binary-xyz"]).run(str(tmp_path))
