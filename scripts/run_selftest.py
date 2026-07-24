#!/usr/bin/env python3
"""Manual-stage hook: install the pinned GdUnit4 addon, then run the live self-test against a
real Godot binary. Mirrors ci.yml's selftest-godot job. Requires a `godot` binary on PATH.
"""

import os
import subprocess
import sys


def main() -> int:
    install = subprocess.run(["sh", "scripts/install-gdunit4.sh"])
    if install.returncode != 0:
        return install.returncode
    env = os.environ.copy()
    env["GDMUTANT_GODOT"] = "godot"
    cmd = ["uv", "run", "pytest", "tests/test_selftest_live.py", "-v", "--no-cov"]
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
