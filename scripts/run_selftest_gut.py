#!/usr/bin/env python3
"""Manual-stage hook: install the pinned GUT addon, then run the GUT leg of the live self-test
against a real Godot binary. Mirrors ci.yml's selftest-gut job. Requires a `godot` binary on PATH.
"""

import os
import subprocess
import sys


def main() -> int:
    install = subprocess.run([sys.executable, "scripts/install_gut.py"])
    if install.returncode != 0:
        return install.returncode
    env = os.environ.copy()
    env["GDMUTANT_GODOT"] = "godot"
    cmd = ["uv", "run", "pytest", "tests/test_selftest_live.py", "-k", "gut", "-v", "--no-cov"]
    return subprocess.run(cmd, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
