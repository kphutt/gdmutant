#!/usr/bin/env python3
"""Manual-stage hook: run mutmut in report mode (advisory), mirroring mutation.yml's
baseline-failure guard. A surviving mutant is fine (this is advisory) -- but a baseline that
won't run clean unmutated means the score would be meaningless, so that case fails this hook
instead of silently reporting a green run.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dev_run import resolve  # noqa: E402


def main() -> int:
    log_path = Path("mutmut-run.log")
    with log_path.open("w", encoding="utf-8") as log:
        subprocess.run(
            resolve(["uv", "run", "mutmut", "run"]), stdout=log, stderr=subprocess.STDOUT
        )
    log_text = log_path.read_text(encoding="utf-8")
    print(log_text)
    if "failed to collect stats" in log_text.lower():
        print(
            "check_mutation_baseline: mutmut baseline failed -- could not collect stats "
            "(the suite does not run cleanly unmutated); any advisory score would be meaningless."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
