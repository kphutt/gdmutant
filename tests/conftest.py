"""Shared test fakes + session-wide test isolation."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from gdmutant.engine.runner import SuiteResult


def pytest_report_header() -> str:
    """Say, before the first test runs, whether the private-vocabulary half of the public-readiness
    guard is going to run at all.

    This is the loud half of `tests/test_public_readiness.py`'s three-state contract. That guard has
    two halves: shape rules, which name nothing and run on every machine, and a vocabulary rule,
    which needs a private word list that cannot ship with a repository meant to go public. On a CI
    runner or a fresh clone there is no list, and the vocabulary rule legitimately does not run.

    The hazard is not that it does not run. It is that a green suite then *looks* like a fully
    checked one, and somebody flips the repository public on the strength of it. pytest prints this
    line at the top of every run on every machine, so the state is on screen in the CI log and in
    the terminal, whether or not anybody reads a skip reason at the bottom.

    Deliberately not a failure: making CI fail for lacking a file it must never be given would just
    get the guard deleted. Announced, not enforced.
    """
    # Imported here rather than at module scope: a collection-time error in the guard should be
    # reported as that test module failing to import, not as conftest taking the whole run down.
    from tests.test_public_readiness import vocabulary_state

    state, said = vocabulary_state()
    return f"public-readiness vocabulary [{state}]: {said}"


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
