"""Point every documented `uses: kphutt/gdmutant@vX.Y.Z` pin at the packaged version's tag.

This is part of step 1 of docs/releasing.md, done by a command instead of by hand. It runs in the
release commit itself, next to the version bump: the docs name the release tag, `vX.Y.Z`, and the
tag's name is known before the tag exists, so the commit that cuts a release can already name it.
The tag cannot move once pushed (a tag ruleset blocks deleting or re-pointing it, and releases are
immutable), so a reader who copies the line gets exactly the release it names.

Usage, from the repo root:

  uv run python scripts/bump_action_pins.py            # the version in pyproject.toml
  uv run python scripts/bump_action_pins.py --version 0.1.3

It reads no network. It rewrites every pin in the files `PIN_FILES` lists to `@vVERSION`, and
writes nothing at all unless every file checks out. It fails, and says why, when a listed file has
no pin, when a pin is not an exact release tag (a commit ID, a branch such as `main`, a major-only
tag such as `v1`), or when a pin names a later version than the one asked for, which would move it
backwards. Running it twice is harmless: the second run reports every pin already current.

`PIN_FILES` is the one list of files that carry a pin. tests/test_action_pin.py checks the same
list and fails if any other file in the repo gains a pin, so a new doc cannot slip past both.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Every file that shows a `uses: kphutt/gdmutant@...` line, relative to the repo root.
PIN_FILES = ("README.md", "action.yml", "docs/gdmutant-guide.md")

#: Any `kphutt/gdmutant@<ref>` a reader could copy, wherever it appears in a pin file. The ref
#: must start with a letter or digit, so a templated ref in code (`@{action_ref}`) is not a pin.
#: The one rule for what counts as a pin, shared with tests/test_action_pin.py so the two cannot
#: drift.
PIN = re.compile(r"(?P<head>kphutt/gdmutant@)(?P<ref>[A-Za-z0-9][^\s\"'`]*)")

#: The one shape a documented pin may take: an exact release tag.
RELEASE_TAG = re.compile(r"v(?P<version>\d+\.\d+\.\d+)")


def packaged_version(root: Path) -> str:
    """The version `pyproject.toml` declares."""
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def version_key(version: str) -> tuple[int, int, int]:
    """`0.1.10` as `(0, 1, 10)`, so versions sort as numbers rather than as strings."""
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def pins(text: str) -> list[str]:
    """Every pin's ref in `text`, in order."""
    return [match["ref"] for match in PIN.finditer(text)]


def bump(text: str, version: str) -> tuple[str, int, int, list[str]]:
    """Point every pin at `vVERSION`.

    Returns the new text, how many pins changed, how many were already current, and a problem for
    every pin that cannot be moved safely: one that is not an exact release tag, and one that names
    a later version (bumping it would move it backwards). Other actions' pins in the same file are
    never touched: only `kphutt/gdmutant@` pins match."""
    changed = current = 0
    problems: list[str] = []

    def replace(match: re.Match[str]) -> str:
        nonlocal changed, current
        ref = match["ref"]
        tag = RELEASE_TAG.fullmatch(ref)
        if tag is None:
            problems.append(
                f"a pin reads `@{ref}`, which is not an exact release tag. Docs pin `@vX.Y.Z`, "
                "never a commit ID, a branch or a major-only tag. Rewrite it by hand, then run "
                "this again"
            )
            return match[0]
        if version_key(tag["version"]) > version_key(version):
            problems.append(
                f"a pin reads `@{ref}`, later than v{version}: bumping it would move it "
                "backwards, so check which version you meant"
            )
            return match[0]
        if tag["version"] == version:
            current += 1
            return match[0]
        changed += 1
        return f"{match['head']}v{version}"

    return PIN.sub(replace, text), changed, current, problems


def main(argv: list[str] | None = None, root: Path = REPO) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--version", help="default: the version in pyproject.toml")
    args = parser.parse_args(argv)
    # `is None`, not `or`: an empty `--version ""` must be refused, never read as "use the default".
    version = packaged_version(root) if args.version is None else args.version
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print(f"bump_action_pins: {version!r} is not an X.Y.Z version", file=sys.stderr)
        return 1

    updates: dict[Path, str] = {}
    problems: list[str] = []
    changed = current = 0
    for name in PIN_FILES:
        path = root / name
        # Bytes, not read_text: that would turn a CRLF checkout into LF on the way back out.
        text = path.read_bytes().decode("utf-8")
        new, file_changed, file_current, file_problems = bump(text, version)
        if file_changed + file_current + len(file_problems) == 0:
            problems.append(f"{name}: no `kphutt/gdmutant@vX.Y.Z` pin found")
        problems += [f"{name}: {p}" for p in file_problems]
        changed += file_changed
        current += file_current
        if new != text:
            updates[path] = new

    if problems:
        print("bump_action_pins: wrote nothing, because:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    for path, new in updates.items():
        path.write_bytes(new.encode("utf-8"))
    print(
        f"bump_action_pins: pinned v{version}. Updated {changed} pin(s) in "
        f"{len(updates)} file(s), {current} already current."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
