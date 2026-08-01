#!/usr/bin/env python3
"""License-compliance gate: fail if a *shipped* dependency carries a license gdmutant cannot
redistribute under MIT.

Two deliberate scoping choices:

**Runtime dependencies only.** Only what a user installs alongside gdmutant affects distribution
compliance. A copyleft *dev* tool (a linter, a test runner) is perfectly legal to use and must not
fail this gate, so the caller installs with ``uv sync --no-dev`` and this script reads whatever is
in the active environment. Today that closure is ten packages, all permissive.

**A deny-list of copyleft families, plus a hard failure on UNKNOWN — not an allow-list of
permissive names.** Package metadata spells the same license many ways: ``MIT`` and
``MIT License``; ``Apache-2.0`` and ``Apache Software License``; ``BSD License`` and
``BSD-3-Clause``. An allow-list has to enumerate every spelling and breaks the first time a
dependency bump changes its wording — a gate that fails for a reason unrelated to the risk it
exists to catch. Matching the copyleft families is stable, because those names are distinctive and
do not appear inside permissive ones. ``UNKNOWN`` is treated as a failure in its own right: an
unvetted license is exactly the case a human needs to look at.
"""

import json
import re
import subprocess
import sys

#: Families gdmutant cannot ship inside an MIT distribution. Matched case-insensitively as whole
#: words, so "GPL" catches "GPL-3.0-or-later" and "GNU General Public License" without also firing
#: on unrelated text. LGPL and AGPL are listed explicitly rather than relying on a "GPL" substring,
#: so the reason a build failed is legible in the output.
DENIED = (
    "AGPL",
    "Affero",
    "LGPL",
    "GPL",
    "GNU General Public License",
    "SSPL",
    "Server Side Public License",
    "Commons Clause",
    "Business Source License",
    "BUSL",
)

#: Missing or unresolvable license metadata. Not a copyleft hit, but never silently allowed.
UNKNOWN = ("UNKNOWN", "", None)

#: The gate's own tooling. CI installs the shipped dependencies with ``uv sync --no-dev`` and then
#: layers pip-licenses on top to do the scanning, so these three would otherwise be counted as
#: things gdmutant ships. They are not; excluding them keeps the report honest about what a user
#: actually installs. (All three are permissive, so this is accuracy, not a carve-out.)
TOOLING = ("pip-licenses", "prettytable", "wcwidth")


def denied_reason(license_text: str) -> str | None:
    """The denied family this license string matches, or None if it is acceptable.

    Leading word boundary only, deliberately. A *trailing* ``\\b`` requires a non-word character
    after the name, and versions are written ``LGPLv3`` / ``GPLv2`` / ``AGPLv3`` -- so anchoring
    both ends let every ``v``-suffixed spelling through the gate. The leading ``\\b`` still does
    the work that matters: it stops ``GPL`` from firing inside ``LGPLv3``, so each family is
    reported under its own name rather than the first one that happened to match.
    """
    for family in DENIED:
        # re.escape so punctuation in a family name can never be read as regex syntax.
        if re.search(rf"\b{re.escape(family)}", license_text, re.IGNORECASE):
            return family
    return None


def main() -> int:
    try:
        raw = subprocess.run(
            [
                sys.executable,
                "-m",
                "piplicenses",
                "--format=json",
                "--ignore-packages",
                *TOOLING,
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print(f"error: could not run pip-licenses: {error}", file=sys.stderr)
        return 2

    packages = json.loads(raw)
    problems: list[tuple[str, str, str]] = []
    for pkg in packages:
        name, license_text = pkg.get("Name", "?"), (pkg.get("License") or "").strip()
        if license_text in UNKNOWN:
            problems.append((name, license_text or "(none)", "no license metadata"))
        elif (family := denied_reason(license_text)) is not None:
            problems.append((name, license_text, f"matches {family}"))

    print(f"Checked {len(packages)} shipped packages:")
    for pkg in sorted(packages, key=lambda p: p.get("Name", "").lower()):
        print(f"  {pkg.get('Name', '?'):<26} {pkg.get('License', '?')}")

    if not packages:
        # `if problems: return 1` is vacuously true-less on an empty list, so a `pip-licenses` call
        # that returns `[]` (a broken `--no-dev` sync, or an output-shape change) would otherwise
        # print "License gate passed" having checked nothing. A gate that measures zero packages
        # has not passed -- it hasn't run.
        print(
            "\nLicense gate FAILED: pip-licenses reported zero shipped packages.", file=sys.stderr
        )
        print(
            "A gate that checks nothing is not a passing gate. This usually means a broken "
            "`--no-dev` sync or a change to pip-licenses' output shape -- investigate before "
            "merging; do not treat this as a clean run.",
            file=sys.stderr,
        )
        return 1

    if problems:
        print("\nLicense gate FAILED:", file=sys.stderr)
        for name, license_text, why in problems:
            print(f"  {name}: {license_text} -- {why}", file=sys.stderr)
        print(
            "\ngdmutant ships under MIT. A dependency under one of these licenses cannot be "
            "redistributed with it without a human decision -- review before merging.",
            file=sys.stderr,
        )
        return 1

    print("\nLicense gate passed: every shipped dependency is permissive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
