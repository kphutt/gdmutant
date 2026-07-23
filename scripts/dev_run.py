#!/usr/bin/env python3
"""Run a dev command through mise's pinned toolchain if mise is installed, otherwise run it
directly. CONTRIBUTING.md documents both paths as supported ("Prefer not to use mise? Install
uv yourself") -- lets the same pre-commit hooks work for either audience instead of hard-requiring
`mise exec --` (found live via Litmus review: a no-mise contributor got "mise: command not found"
on every pre-push hook).
"""

import shutil
import subprocess
import sys


def resolve(args: list[str]) -> list[str]:
    return ["mise", "exec", "--", *args] if shutil.which("mise") else args


def main() -> int:
    return subprocess.run(resolve(sys.argv[1:])).returncode


if __name__ == "__main__":
    sys.exit(main())
