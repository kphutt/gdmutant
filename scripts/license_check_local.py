#!/usr/bin/env python3
"""Run ci.yml's `license-check` job locally, in an isolated venv.

That job's own first step is `uv sync --frozen --no-dev` — fine in CI, where the runner is thrown
away afterward, but not on a dev machine: running it against the *shared* `.venv` uninstalls
pre-commit itself mid-hook, which on Windows fails outright ("failed to remove file
...pre-commit.exe: The process cannot access the file because it is being used by another
process" — found live, running this as a pre-push hook). Pointing `UV_PROJECT_ENVIRONMENT` at a
separate directory (`.venv-license-check`, gitignored) gives the job its own venv to strip down to
no-dev, so the shared one — and whatever's currently running out of it — is never touched.

Usage, from the repo root:

    uv run python scripts/license_check_local.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ISOLATED_VENV = REPO_ROOT / ".venv-license-check"


def main() -> int:
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(ISOLATED_VENV)

    # `uv run` auto-syncs its target environment (full deps, including PyYAML) before running the
    # command, so verify_local.py's own `import yaml` succeeds before the license-check job's
    # first step (`uv sync --frozen --no-dev`, run as a subprocess inside it) narrows that SAME
    # isolated venv down to shipped-only deps. Both calls share this env via inheritance.
    check = subprocess.run(
        ["uv", "run", "python", "scripts/verify_local.py", "--job", "license-check"],
        cwd=REPO_ROOT,
        env=env,
    )
    return check.returncode


if __name__ == "__main__":
    sys.exit(main())
