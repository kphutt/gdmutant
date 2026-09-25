#!/usr/bin/env python3
"""Fail unless a release tag matches the version packaged in ``pyproject.toml``, and unless
``CHANGELOG.md`` has been dated for that same version.

This guard runs *before* the GitHub Release is created, because everything downstream is
irreversible: creating the Release fires ``publish.yml``, which uploads to PyPI — and a PyPI
version number can never be reused or overwritten. A tag of ``v0.2.0`` against a ``pyproject.toml``
still saying ``0.1.0`` would publish ``0.1.0`` under a release labelled ``0.2.0``, and the only
remedy is yanking and burning a version number.

The changelog check exists for the same reason. ``docs/releasing.md`` says to rename
``CHANGELOG.md``'s ``## [Unreleased]`` heading to ``## [X.Y.Z] - YYYY-MM-DD`` before tagging, in the
same commit as the version bump. Nothing used to enforce that: miss it and the tag ships a commit
whose own changelog still calls the shipped version unreleased. It is checked here, in the same
script both release.yml and publish.yml already call with the tag as their one argument, so this
reaches both call sites without a workflow-file change.

Usage::

    python3 scripts/check_release_tag.py v1.2.3
"""

import re
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

#: `v` followed by the version. Anchored so a stray suffix (`v1.2.3-hotfix`) is rejected rather
#: than silently truncated to something that happens to match.
TAG = re.compile(r"^v(?P<version>.+)$")

#: The changelog's top-of-file heading: `## [Unreleased]` or `## [X.Y.Z] - YYYY-MM-DD`. The date
#: half is optional in the pattern itself so a missing or malformed date can be reported as its own
#: distinct problem, rather than as "no heading found at all".
CHANGELOG_HEADING = re.compile(
    r"^## \[(?P<label>[^\]]+)\](?:\s*-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)


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


def changelog_problem(version: str, changelog: Path = CHANGELOG) -> str | None:
    """An error message if `changelog`'s top heading is not a dated release of `version`, else None.

    Four ways this must fail, and one it must not:
      - the file cannot be read at all: fail loudly, never silently pass
      - there is no ``## [...]`` heading to check
      - the top heading is still ``## [Unreleased]``
      - the top heading names some other version
      - the top heading has no date, or the date is not ``YYYY-MM-DD``
    """
    try:
        text = changelog.read_text(encoding="utf-8")
    except OSError as exc:
        return f"could not read {changelog} to check it was dated: {exc}"

    heading = next((ln.strip() for ln in text.splitlines() if ln.strip().startswith("## [")), None)
    if heading is None:
        return f"{changelog} has no '## [...]' heading to check was dated for {version!r}"

    match = CHANGELOG_HEADING.match(heading)
    if not match:
        return (
            f"{changelog}'s top heading {heading!r} is not of the form "
            "'## [X.Y.Z] - YYYY-MM-DD' (or the date is not that ISO shape)"
        )

    label = match.group("label")
    if label == "Unreleased":
        return (
            f"{changelog}'s top heading is still '## [Unreleased]'. Date it "
            f"'## [{version}] - YYYY-MM-DD' before tagging: the tag ships the commit it points at, "
            "so tagging first publishes a changelog that calls the shipped version unreleased."
        )
    if label != version:
        return (
            f"{changelog}'s top heading names version {label!r}, but the tag names {version!r}. "
            "Fix one of the two before tagging."
        )
    if not match.group("date"):
        return (
            f"{changelog}'s top heading for {version} has no date. Use "
            f"'## [{version}] - YYYY-MM-DD'."
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
    changelog_issue = changelog_problem(version, CHANGELOG)
    if changelog_issue:
        print(f"error: {changelog_issue}", file=sys.stderr)
        return 1
    print(f"tag {argv[1]} matches the packaged version {version}, and CHANGELOG.md is dated for it")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
