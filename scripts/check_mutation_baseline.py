#!/usr/bin/env python3
"""Manual-stage hook: run poodle (report mode, advisory) against gdmutant/**.py files changed vs
origin/main. See docs/decisions/0013-windows-local-mutation-testing.md for why this is poodle
and not mutmut (native Windows support) and why it's diff-scoped, not a full-package sweep (a
full sweep is well over an hour; this is a pre-push hook).

A surviving mutant is fine (this is advisory) -- but a baseline that won't run clean unmutated
means the score would be meaningless, so that case fails this hook instead of silently reporting
a green run.
"""

import subprocess
import sys
from pathlib import Path


def changed_gdmutant_files(base_ref: str = "origin/main") -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD", "--", "gdmutant"],
        capture_output=True,
        text=True,
        check=True,
    )
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [f for f in files if f.endswith(".py") and Path(f).is_file()]


def main() -> int:
    files = changed_gdmutant_files()
    if not files:
        print("no gdmutant/ files changed vs origin/main -- nothing to mutate")
        return 0

    print(f"check_mutation_baseline: mutating {len(files)} changed file(s): {', '.join(files)}")

    only_args: list[str] = []
    for f in files:
        only_args += ["--only", f]

    result = subprocess.run(
        ["uv", "run", "poodle", "-c", "poodle.toml", *only_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        # poodle's own fail_under is unset, so a clean run with surviving mutants still exits 0 --
        # any nonzero here means it didn't produce a meaningful score at all (clean-run failure, no
        # mutants found, a usage error, ...), not just "some mutants survived."
        print(
            f"check_mutation_baseline: poodle exited {result.returncode} -- did not produce a "
            "meaningful score (see output above)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
