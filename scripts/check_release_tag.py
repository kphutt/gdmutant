#!/usr/bin/env python3
"""Fail unless a release tag matches the version packaged in ``pyproject.toml``.

This guard runs *before* the GitHub Release is created, because everything downstream is
irreversible: creating the Release fires ``publish.yml``, which uploads to PyPI — and a PyPI
version number can never be reused or overwritten. A tag of ``v0.2.0`` against a ``pyproject.toml``
still saying ``0.1.0`` would publish ``0.1.0`` under a release labelled ``0.2.0``, and the only
remedy is yanking and burning a version number.

Usage::

    python3 scripts/check_release_tag.py v1.2.3
"""

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: `v` followed by the version. Anchored so a stray suffix (`v1.2.3-hotfix`) is rejected rather
#: than silently truncated to something that happens to match.
TAG = re.compile(r"^v(?P<version>.+)$")


def packaged_version(pyproject: Path = PYPROJECT) -> str:
    """The version string declared in ``[project]``."""
    with pyproject.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def mismatch(tag: str, version: str) -> str | None:
    """An error message if `tag` does not name `version`, else None."""
    match = TAG.match(tag)
    if not match:
        return f"tag {tag!r} is not of the form vX.Y.Z"
    tagged = match.group("version")
    if tagged != version:
        return (
            # ASCII only: this string is printed, and gdmutant already shipped a Windows bug where
            # console output crashed under the legacy cp1252 code page. A guard that crashes
            # instead of reporting a mismatch is worse than no guard.
            f"tag {tag!r} declares version {tagged!r}, but pyproject.toml packages {version!r}. "
            "Publishing is irreversible on PyPI - fix one of the two and re-tag."
        )
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0] if argv else 'check_release_tag.py'} vX.Y.Z", file=sys.stderr)
        return 2
    version = packaged_version()
    problem = mismatch(argv[1], version)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1
    print(f"tag {argv[1]} matches the packaged version {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
