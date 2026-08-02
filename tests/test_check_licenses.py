"""Tests for the license-compliance gate (`scripts/check_licenses.py`).

The gate is a regex over free-text license metadata, which is the kind of logic that fails
*silently* and in the safe-looking direction: a missed pattern lets a license through and the
build still goes green. An early version of this matcher anchored both ends of the family name,
so `LGPLv3`, `GPLv2` and `AGPLv3` all passed the gate — a trailing `\\b` cannot match before the
`v`. That is the bug this table exists to keep out.

`scripts/` is outside the coverage source (`pyproject.toml` sets `source = ["gdmutant"]`), so
this file adds no coverage obligation; it is here because the logic earns a test on its own.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_licenses.py"
_spec = importlib.util.spec_from_file_location("check_licenses", _SCRIPT)
assert _spec and _spec.loader
check_licenses = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_licenses)


# Every spelling seen across gdmutant's real dependency closure, plus the common variants a
# dependency bump could introduce. A false positive here blocks a legal dependency.
PERMISSIVE = [
    "MIT",
    "MIT License",
    "Apache-2.0",
    "Apache Software License",
    "BSD License",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0 AND CNRI-Python",
    "Apache-2.0 OR BSD-2-Clause",
    "Mozilla Public License 2.0 (MPL 2.0)",
    "Python Software Foundation License",
    "PSF-2.0",
    "ISC",
    "The Unlicense (Unlicense)",
]

# Licenses gdmutant cannot ship inside an MIT distribution, in the spellings that actually occur.
# The `v`-suffixed forms are the regression guard.
COPYLEFT = [
    ("GPL", "GPL"),
    ("GPLv2", "GPL"),
    ("GPL-3.0-or-later", "GPL"),
    ("GNU General Public License v2", "GNU General Public License"),
    ("LGPL", "LGPL"),
    ("LGPLv3", "LGPL"),
    ("LGPL-2.1-only", "LGPL"),
    ("AGPL", "AGPL"),
    ("AGPLv3", "AGPL"),
    ("GNU Affero General Public License", "Affero"),
    ("SSPL-1.0", "SSPL"),
    ("Server Side Public License", "Server Side Public License"),
    ("Commons Clause", "Commons Clause"),
    ("BUSL-1.1", "BUSL"),
]


@pytest.mark.parametrize("license_text", PERMISSIVE)
def test_permissive_licenses_pass(license_text: str) -> None:
    assert check_licenses.denied_reason(license_text) is None, (
        f"{license_text!r} is permissive and must not fail the gate"
    )


@pytest.mark.parametrize(("license_text", "family"), COPYLEFT)
def test_copyleft_licenses_are_denied(license_text: str, family: str) -> None:
    assert check_licenses.denied_reason(license_text) == family, (
        f"{license_text!r} must be denied, and reported as {family!r}"
    )


def test_lgpl_is_not_reported_as_gpl() -> None:
    """The leading word boundary keeps each family under its own name.

    Without it `GPL` matches inside `LGPLv3`, and the failure message names the wrong licence —
    which sends a reader looking for a dependency that isn't there.
    """
    assert check_licenses.denied_reason("LGPLv3") == "LGPL"


def test_matching_is_case_insensitive() -> None:
    assert check_licenses.denied_reason("gplv3") == "GPL"


@pytest.mark.parametrize("blank", ["", "   ", "UNKNOWN"])
def test_blank_metadata_is_not_silently_allowed(blank: str) -> None:
    """`denied_reason` only answers the copyleft question; absent metadata is handled by `main`,
    which treats it as its own failure. Guard the split so neither side quietly drops the case."""
    assert check_licenses.denied_reason(blank) is None
    assert "UNKNOWN" in check_licenses.UNKNOWN


class _FakeCompletedProcess:
    """Stands in for `subprocess.run(...).stdout` -- only `.stdout` is ever read."""

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_main_fails_when_pip_licenses_reports_zero_packages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`main`'s gate logic is `if problems: return 1`, which is vacuously true-less on an empty
    list -- a `pip-licenses` call returning `[]` (a broken `--no-dev` sync, or a change to its
    output shape) produced no problems and therefore a green "License gate passed" having checked
    nothing. Demonstrated by making `pip-licenses` return an empty package list.
    """
    monkeypatch.setattr(
        check_licenses.subprocess, "run", lambda *a, **k: _FakeCompletedProcess("[]")
    )

    assert check_licenses.main() == 1
    lines = capsys.readouterr().err.splitlines()
    # Exact-line equality, not a substring check: a mutation that only pollutes the string's edges
    # (e.g. wrapping the whole literal) still contains "zero shipped packages" as a substring, so
    # only pinning the line verbatim closes that gap.
    assert "License gate FAILED: pip-licenses reported zero shipped packages." in lines
    assert (
        "A gate that checks nothing is not a passing gate. This usually means a broken "
        "`--no-dev` sync or a change to pip-licenses' output shape -- investigate before "
        "merging; do not treat this as a clean run."
    ) in lines


def test_main_passes_when_packages_are_present_and_permissive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The zero-packages guard must not fire on a normal, non-empty, clean run."""
    payload = json.dumps([{"Name": "gdtoolkit", "License": "MIT"}])
    monkeypatch.setattr(
        check_licenses.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(payload)
    )

    assert check_licenses.main() == 0
    out = capsys.readouterr().out
    assert "License gate passed" in out


def test_main_still_fails_on_a_real_copyleft_hit_alongside_other_packages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new zero-packages guard must not swallow the pre-existing copyleft failure path."""
    payload = json.dumps(
        [{"Name": "gdtoolkit", "License": "MIT"}, {"Name": "copyleft-dep", "License": "GPL-3.0"}]
    )
    monkeypatch.setattr(
        check_licenses.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(payload)
    )

    assert check_licenses.main() == 1
    err = capsys.readouterr().err
    assert "License gate FAILED" in err
    assert "copyleft-dep" in err
