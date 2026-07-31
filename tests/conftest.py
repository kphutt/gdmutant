"""Shared test fakes + session-wide test isolation."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from gdmutant.engine.runner import SuiteResult

# Location vars git exports into a hook's environment (e.g. pre-push). If pytest is spawned from
# such a hook, inheriting them makes EVERY git call — the test helpers AND gdmutant's production git
# calls (`_git_backup`, `_changed_lines` in cli.py, exercised by the --since /
# require-clean tests) — operate on the hook's repo instead of each test's throwaway tmp repo,
# failing those tests only under the hook. Scrub them once, session-wide (generalizes the per-helper
# scrub in test_cli.py so the production paths are isolated too). Tests never depend on ambient git
# env, so removing these is always safe.
_GIT_ENV_LEAKS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: scrub inherited GIT_* location vars so any git subprocess a test drives (production
    code included) acts on its own tmp repo, never the repo of a hook that spawned pytest."""
    for var in _GIT_ENV_LEAKS:
        monkeypatch.delenv(var, raising=False)


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
