"""Tests for the release-tag guard (`scripts/check_release_tag.py`).

This guard is the last thing standing between a mistyped tag and an irreversible PyPI upload, and
it only ever runs in CI — where a bug in it shows up as a bad release, not a failing test. So the
mismatch logic is pinned here, where it fails fast and locally.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release_tag.py"
_spec = importlib.util.spec_from_file_location("check_release_tag", _SCRIPT)
assert _spec and _spec.loader
check_release_tag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_release_tag)


def test_matching_tag_is_accepted() -> None:
    assert check_release_tag.mismatch("v1.2.3", "1.2.3") is None


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v0.2.0", "0.1.0"),  # the real hazard: tag ahead of the package
        ("v0.1.0", "0.2.0"),  # and behind it
        ("v1.2.3-hotfix", "1.2.3"),  # a suffix must not be truncated into a match
        ("v1.2", "1.2.3"),
        ("v1.2.3", "1.2.30"),  # no prefix matching
    ],
)
def test_mismatched_tag_is_rejected(tag: str, version: str) -> None:
    problem = check_release_tag.mismatch(tag, version)
    assert problem is not None, f"{tag!r} vs {version!r} must be rejected"
    assert tag in problem and version in problem, "the message must name both, to be actionable"


@pytest.mark.parametrize("tag", ["1.2.3", "release-1.2.3", "", "V1.2.3"])
def test_tags_without_the_v_prefix_are_rejected(tag: str) -> None:
    """The workflow triggers on `v*.*.*`, so anything else reaching this guard is a mistake.

    `V1.2.3` is included deliberately: the check is case-sensitive, and a capitalised tag would
    not have fired the workflow in the first place.
    """
    assert check_release_tag.mismatch(tag, "1.2.3") is not None


def test_reads_the_real_pyproject() -> None:
    """The guard must read the version from the actual packaged metadata, not a copy."""
    version = check_release_tag.packaged_version()
    assert version, "pyproject.toml must declare a [project] version"
    assert check_release_tag.mismatch(f"v{version}", version) is None


def test_main_reports_usage_without_a_tag() -> None:
    assert check_release_tag.main(["check_release_tag.py"]) == 2


def test_main_fails_on_a_mismatched_tag() -> None:
    assert check_release_tag.main(["check_release_tag.py", "v999.999.999"]) == 1


def test_main_succeeds_on_the_packaged_version() -> None:
    version = check_release_tag.packaged_version()
    assert check_release_tag.main(["check_release_tag.py", f"v{version}"]) == 0
