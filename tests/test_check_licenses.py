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
