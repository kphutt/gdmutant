#!/usr/bin/env python3
"""One discoverable entry point for the everyday dev loop: lint, test, build.

Each subcommand is a thin dispatcher over the real tools, never a reimplementation, so it can
never drift from what those tools actually do:

    lint  -> uv run ruff check . / uv run ruff format --check . / uv run mypy gdmutant
    test  -> uv run pytest
    build -> uv build

This is a convenience shortcut for the fast inner loop, not a replacement for
`scripts/verify_local.py` (the full ci.yml `verify` job, parsed from the workflow itself so it
cannot drift) or `pre-commit run --all-files` (the same commands as git hooks). Run one of those
two before opening a pull request; run `dev.py` while you're still writing the change and want a
quick, named command instead of remembering the individual `uv run ...` invocations.

Usage, from the repo root:

    uv run python scripts/dev.py lint
    uv run python scripts/dev.py test
    uv run python scripts/dev.py build

Exit code is 0 only if every command in the chosen subcommand's chain passed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Each subcommand's chain of real commands, run in order. A later command still runs even if an
#: earlier one fails (mirrors verify_local.py: report everything broken in one pass, not just the
#: first thing).
COMMANDS: dict[str, list[list[str]]] = {
    "lint": [
        ["uv", "run", "ruff", "check", "."],
        ["uv", "run", "ruff", "format", "--check", "."],
        ["uv", "run", "mypy", "gdmutant"],
    ],
    "test": [
        ["uv", "run", "pytest"],
    ],
    "build": [
        ["uv", "build"],
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("task", choices=sorted(COMMANDS), help="the dev task to run")
    args = parser.parse_args()

    commands = COMMANDS[args.task]
    failed: list[str] = []
    for command in commands:
        printed = " ".join(command)
        print(f"$ {printed}")
        proc = subprocess.run(command, cwd=REPO_ROOT)
        if proc.returncode != 0:
            failed.append(printed)
            print(f"  FAILED: {printed}\n")
        else:
            print("  ok\n")

    print("-" * 60)
    if failed:
        print(f"{args.task} FAILED ({len(failed)}/{len(commands)}):")
        for command in failed:
            print(f"  - {command}")
        return 1

    print(f"{args.task}: all {len(commands)} command(s) passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
