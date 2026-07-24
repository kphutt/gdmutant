#!/usr/bin/env python3
"""Run gitleaks's pre-commit secret scan if gitleaks is installed; otherwise skip gracefully
(exit 0) with a pointer to install it, instead of hard-failing the whole pre-commit run (found
live in review: a fresh clone with no local gitleaks binary crashed the pre-commit-stage
hook entirely).
"""

import shutil
import subprocess
import sys

GITLEAKS_INSTALL_DOCS = "https://github.com/gitleaks/gitleaks#installing"


def main() -> int:
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        print(
            "gitleaks not found on PATH -- skipping secret scan "
            f"(install: {GITLEAKS_INSTALL_DOCS}, then re-run "
            '"pre-commit run gitleaks" to confirm it works)'
        )
        return 0
    return subprocess.run(
        [gitleaks, "git", "--pre-commit", "--redact", "--staged", "--verbose"]
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
