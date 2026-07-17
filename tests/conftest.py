"""Shared test fakes."""

from dataclasses import dataclass
from pathlib import Path

from gdmutant.engine.runner import SuiteResult


@dataclass
class MarkerRunner:
    """A fake suite that 'catches' (fails on) any mutation producing `kill_marker` in the target
    file. Reads the file each call, so it reacts to whatever the loop wrote to disk."""

    target: str
    kill_marker: str
    tests: int = 3

    def run(self, project_dir: str, timeout: float | None = None) -> SuiteResult:
        content = Path(self.target).read_text(encoding="utf-8")
        return SuiteResult(tests=self.tests, failures=int(self.kill_marker in content), errors=0)
