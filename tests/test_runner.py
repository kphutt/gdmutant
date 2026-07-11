"""Tests for the runner interface + JUnit-XML parsing."""

from dataclasses import dataclass, field
from xml.etree.ElementTree import ParseError

import pytest

from gdmutant.engine.runner import Runner, SuiteResult, parse_junit_xml


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
    # attributes; sum only the outer suite, don't descend ([internal-tool] P3).
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
    with pytest.raises(ValueError, match="no <testsuite>"):
        parse_junit_xml("<other/>")


def test_parse_malformed_xml_raises() -> None:
    with pytest.raises(ParseError):
        parse_junit_xml("<not closed")


def test_fake_runner_satisfies_the_protocol() -> None:
    fake = FakeRunner(SuiteResult(tests=1, failures=0, errors=0))
    assert isinstance(fake, Runner)
    runner: Runner = fake  # static conformance
    assert runner.run("some/dir").passed
    assert fake.calls == ["some/dir"]
