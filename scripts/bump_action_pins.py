"""Point every documented `uses: kphutt/gdmutant@<sha> # vX.Y.Z` pin at the vX.Y.Z tag's commit.

This is step 10 of docs/releasing.md, done by a command instead of by hand. It has to run after
the release, never in the release commit: the SHA it writes does not exist until the tag does.
Until it runs, tests/test_action_pin.py fails on `main` and on every pull request, because the
docs still pin the previous release.

Usage, from the repo root, once `vX.Y.Z` is on origin:

  uv run python scripts/bump_action_pins.py            # the version in pyproject.toml
  uv run python scripts/bump_action_pins.py --version 0.1.3

It reads the tag's commit from origin, rewrites the SHA of every pin whose comment names that
version, and writes nothing at all unless every file checks out. It fails, and says why, when the
tag is not on origin yet, when a listed file has no pin, or when a pin's comment names a different
version, which means release step 1 missed it. Running it twice is harmless: the second run
reports every pin already current.

`PIN_FILES` is the one list of files that carry a pin. tests/test_action_pin.py checks the same
list and fails if any other file in the repo gains a pin, so a new doc cannot slip past both.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Every file that shows a SHA-pinned `uses: kphutt/gdmutant@...` line, relative to the repo root.
PIN_FILES = ("README.md", "action.yml", "docs/gdmutant-guide.md")

#: A pin with its version comment: the SHA and the version are the parts that matter.
PIN = re.compile(
    r"(kphutt/gdmutant@)(?P<sha>[0-9a-f]{40})(?P<gap>\s*#\s*v)(?P<version>\d+\.\d+\.\d+)"
)

#: Any SHA pin, commented or not. More of these than `PIN` matches means a pin with no comment.
ANY_PIN = re.compile(r"kphutt/gdmutant@[0-9a-f]{40}")


def packaged_version(root: Path) -> str:
    """The version `pyproject.toml` declares."""
    with (root / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def tag_commit(version: str, root: Path) -> str | None:
    """The commit `vVERSION` points at on origin, or None when the tag is not there yet.

    Read from origin rather than local refs, like tests/test_action_pin.py, so a clone that has
    not fetched the tag still gets the right answer. An annotated tag's `^{}` line is its commit;
    a lightweight tag (this repo's kind) has only the plain line, which already is the commit."""
    tag = f"refs/tags/v{version}"
    output = subprocess.run(
        ["git", "ls-remote", "origin", tag, f"{tag}^{{}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    shas = {ref: sha for sha, ref in (line.split("\t") for line in output.splitlines() if line)}
    return shas.get(f"{tag}^{{}}") or shas.get(tag)


def bump(text: str, sha: str, version: str) -> tuple[str, int, int, list[str]]:
    """Rewrite the SHA of every pin commented `vVERSION` to `sha`.

    Returns the new text, how many pins changed, how many were already current, and a problem for
    every pin whose comment names another version. Other SHA-pinned actions in the same file are
    never touched: only `kphutt/gdmutant@` pins match."""
    changed = current = 0
    problems: list[str] = []

    def replace(match: re.Match[str]) -> str:
        nonlocal changed, current
        if match["version"] != version:
            problems.append(
                f"a pin is commented v{match['version']}, not v{version}: release step 1 bumps "
                "every comment in the release commit, so this one was missed"
            )
            return match[0]
        if match["sha"] == sha:
            current += 1
            return match[0]
        changed += 1
        return f"{match[1]}{sha}{match['gap']}{match['version']}"

    new = PIN.sub(replace, text)
    bare = len(ANY_PIN.findall(text)) - len(PIN.findall(text))
    if bare:
        problems.append(
            f"{bare} pin(s) carry no `# vX.Y.Z` comment, so there is no way to tell which release "
            "they mean. Add the comment, then run this again"
        )
    return new, changed, current, problems


def main(argv: list[str] | None = None, root: Path = REPO) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--version", help="default: the version in pyproject.toml")
    args = parser.parse_args(argv)
    version = args.version or packaged_version(root)

    sha = tag_commit(version, root)
    if sha is None:
        print(
            f"bump_action_pins: v{version} is not on origin yet. Push the tag first: the pins can "
            "only name a commit the tag already points at.",
            file=sys.stderr,
        )
        return 1

    updates: dict[Path, str] = {}
    problems: list[str] = []
    changed = current = 0
    for name in PIN_FILES:
        path = root / name
        # Bytes, not read_text: that would turn a CRLF checkout into LF on the way back out.
        text = path.read_bytes().decode("utf-8")
        new, file_changed, file_current, file_problems = bump(text, sha, version)
        if file_changed + file_current + len(file_problems) == 0:
            problems.append(f"{name}: no `kphutt/gdmutant@<sha> # vX.Y.Z` pin found")
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
        f"bump_action_pins: v{version} is {sha[:7]}. Updated {changed} pin(s) in "
        f"{len(updates)} file(s), {current} already current."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
