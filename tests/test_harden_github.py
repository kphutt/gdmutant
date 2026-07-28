"""Tests for the branch-protection spec in `scripts/harden_github.py`.

The script is idempotent config-as-code: it PUTs the whole branch-protection object, so a wrong
required-check list is not a stale value that drifts harmlessly — re-running the script overwrites
the live setting with the wrong one. Two ways that goes bad, both fatal to the repo:

* requiring a context no job reports (a bare `Verify` when the matrix reports
  `Verify (ubuntu-24.04)` and `Verify (windows-2025)`) leaves every PR pending forever;
* silently shipping a shorter list than the live one quietly REDUCES the merge gate.

So these tests pin the derivation against the repo's real workflow files and pin the ratchet that
refuses to write a reduced set.

`scripts/` is outside the coverage source (`pyproject.toml` sets `source = ["gdmutant"]`), so this
file adds no coverage obligation; the logic earns a test on its own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "harden_github.py"
_spec = importlib.util.spec_from_file_location("harden_github", _SCRIPT)
assert _spec and _spec.loader
harden_github = importlib.util.module_from_spec(_spec)
# Register before exec: `@dataclass` resolves the defining module out of `sys.modules`, and a
# module loaded straight from a path is not there unless we put it there.
sys.modules["harden_github"] = harden_github
_spec.loader.exec_module(harden_github)


# The exact contexts GitHub reports for the required jobs, as read back from the live branch
# protection on `main`. Written out in full so a workflow rename that changes a context has to be
# reflected here deliberately, not absorbed silently.
EXPECTED_CONTEXTS = [
    "Verify (ubuntu-24.04)",
    "Verify (windows-2025)",
    "Secret scan (gitleaks)",
    "Self-test (real Godot)",
    "Self-test (real Godot + GUT)",
]


def test_required_contexts_match_the_live_protection_set() -> None:
    """The derived list is exactly what branch protection requires on `main` today."""
    assert harden_github.required_contexts() == EXPECTED_CONTEXTS


def test_matrix_job_never_yields_the_bare_job_name() -> None:
    """The regression guard: `verify` is a matrix job, so a bare `Verify` can never report.

    Requiring it would block every PR in the repo permanently.
    """
    contexts = harden_github.required_contexts()
    assert "Verify" not in contexts
    assert sum(c.startswith("Verify (") for c in contexts) == 2


def test_every_required_job_id_exists_and_always_runs() -> None:
    jobs = harden_github.all_jobs()
    for job_id in harden_github.REQUIRED_JOBS:
        assert job_id in jobs, f"{job_id!r} is required but defined in no workflow"
        assert not jobs[job_id].conditional, f"{job_id!r} is if:-gated, so it can skip and hang PRs"


def test_path_filtered_workers_are_not_required() -> None:
    """The heavy Godot workers are `if:`-gated, so they must stay out of the required set."""
    jobs = harden_github.all_jobs()
    for worker in ("selftest-godot-run", "selftest-gut-run"):
        assert jobs[worker].conditional
        assert worker not in harden_github.REQUIRED_JOBS
        for context in jobs[worker].contexts():
            assert context not in harden_github.required_contexts()


def test_publish_and_release_jobs_are_not_required() -> None:
    """`publish.yml` / `release.yml` never run on a pull request, so they can never be required."""
    contexts = harden_github.required_contexts()
    for never_on_a_pr in ("Publish to PyPI", "Create GitHub Release", "Build distributions"):
        assert never_on_a_pr not in contexts


def test_protection_payload_keeps_the_hardening_settings() -> None:
    payload = harden_github.protection_payload()
    assert payload["required_status_checks"]["contexts"] == EXPECTED_CONTEXTS
    assert payload["required_status_checks"]["strict"] is True
    assert payload["enforce_admins"] is True
    assert payload["required_conversation_resolution"] is True
    assert payload["allow_force_pushes"] is False
    assert payload["allow_deletions"] is False


# --------------------------------------------------------------------------------------------
# Parser behaviour, against synthetic workflows
# --------------------------------------------------------------------------------------------


def _workflow(tmp_path: Path, body: str, filename: str = "ci.yml") -> Path:
    directory = tmp_path / "workflows"
    directory.mkdir(exist_ok=True)
    (directory / filename).write_text(body, encoding="utf-8")
    return directory


INLINE_MATRIX = """\
name: CI
on:
  pull_request:
jobs:
  verify:
    name: Verify
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-24.04, windows-2025]
    runs-on: ${{ matrix.os }}
    steps:
      - name: Tests
        run: pytest
"""

BLOCK_MATRIX = """\
name: CI
on:
  pull_request:
jobs:
  verify:
    name: Verify
    strategy:
      matrix:
        os:
          - ubuntu-24.04
          - windows-2025
    runs-on: ${{ matrix.os }}
"""

TWO_DIMENSIONS = """\
name: CI
on:
  pull_request:
jobs:
  verify:
    name: Verify
    strategy:
      matrix:
        os: [ubuntu-24.04, windows-2025]
        python: ["3.12", "3.13"]
    runs-on: ${{ matrix.os }}
"""


@pytest.mark.parametrize("body", [INLINE_MATRIX, BLOCK_MATRIX])
def test_matrix_expands_for_both_yaml_list_styles(tmp_path: Path, body: str) -> None:
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["verify"].contexts() == ["Verify (ubuntu-24.04)", "Verify (windows-2025)"]


def test_two_matrix_dimensions_expand_in_declaration_order(tmp_path: Path) -> None:
    """GitHub joins the values in the order the dimensions are declared, not alphabetically."""
    jobs = harden_github.all_jobs(_workflow(tmp_path, TWO_DIMENSIONS))
    assert jobs["verify"].contexts() == [
        "Verify (ubuntu-24.04, 3.12)",
        "Verify (ubuntu-24.04, 3.13)",
        "Verify (windows-2025, 3.12)",
        "Verify (windows-2025, 3.13)",
    ]


def test_a_step_name_is_not_mistaken_for_the_job_name(tmp_path: Path) -> None:
    """`name:` inside a step sits deeper than the job's own key; only the job's own counts."""
    jobs = harden_github.all_jobs(_workflow(tmp_path, INLINE_MATRIX))
    assert jobs["verify"].name == "Verify"


def test_job_without_a_name_reports_under_its_job_id(tmp_path: Path) -> None:
    body = "name: CI\non:\n  pull_request:\njobs:\n  lint:\n    runs-on: ubuntu-24.04\n"
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["lint"].contexts() == ["lint"]


def test_trailing_comment_is_stripped_from_a_job_name(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify # the fast gate\n    runs-on: ubuntu-24.04\n"
    )
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["verify"].contexts() == ["Verify"]


def test_matrix_include_is_rejected_rather_than_guessed(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify\n    strategy:\n      matrix:\n"
        "        os: [ubuntu-24.04]\n        include:\n          - os: macos-15\n"
    )
    with pytest.raises(ValueError, match="include"):
        harden_github.all_jobs(_workflow(tmp_path, body))


def test_templated_job_name_is_rejected(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  verify:\n    name: Verify ${{ matrix.os }}\n    strategy:\n      matrix:\n"
        "        os: [ubuntu-24.04]\n"
    )
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    with pytest.raises(ValueError, match="templated name"):
        jobs["verify"].contexts()


def test_if_always_still_counts_as_always_running(tmp_path: Path) -> None:
    body = (
        "name: CI\non:\n  pull_request:\njobs:\n"
        "  gate:\n    name: Gate\n    if: always()\n    runs-on: ubuntu-24.04\n"
        "  worker:\n    name: Worker\n    if: needs.changes.outputs.godot == 'true'\n"
        "    runs-on: ubuntu-24.04\n"
    )
    jobs = harden_github.all_jobs(_workflow(tmp_path, body))
    assert jobs["gate"].conditional is False
    assert jobs["worker"].conditional is True


def test_a_missing_required_job_raises_instead_of_dropping_the_check(tmp_path: Path) -> None:
    """A renamed job id must fail loudly. Silently dropping it would shrink the merge gate."""
    body = "name: CI\non:\n  pull_request:\njobs:\n  something-else:\n    name: Other\n"
    with pytest.raises(ValueError, match="secret-scan|verify|selftest"):
        harden_github.required_contexts(_workflow(tmp_path, body))


def test_an_if_gated_required_job_raises(tmp_path: Path) -> None:
    lines = ["name: CI", "on:", "  pull_request:", "jobs:"]
    for job_id in harden_github.REQUIRED_JOBS:
        lines += [f"  {job_id}:", f"    name: {job_id}", "    if: github.event_name == 'push'"]
    body = "\n".join(lines) + "\n"
    with pytest.raises(ValueError, match="if:-gated"):
        harden_github.required_contexts(_workflow(tmp_path, body))


# --------------------------------------------------------------------------------------------
# The ratchet: the script refuses to write a set smaller than the live one
# --------------------------------------------------------------------------------------------


class _FakeGh:
    """Stands in for the `gh` CLI: records every call, answers the protection read."""

    def __init__(self, live_contexts: list[str]) -> None:
        self.live_contexts = live_contexts
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, stdin: str | None = None) -> tuple[bool, str]:
        self.calls.append(args)
        joined = " ".join(args)
        if "branches/main/protection" in joined and "-X" not in args:
            return True, (
                '{"required_status_checks": {"strict": true, "contexts": '
                f"{self.live_contexts!r}".replace("'", '"')
                + "}}"
            )
        if joined.startswith("api repos/") and "-X" not in args:
            return True, "{}"
        return True, ""

    def wrote_protection(self) -> bool:
        return any(
            "branches/main/protection" in " ".join(call) and "PUT" in call for call in self.calls
        )


@pytest.fixture
def _has_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harden_github.shutil, "which", lambda _name: "/usr/bin/gh")


@pytest.mark.usefixtures("_has_gh")
def test_write_is_refused_when_it_would_drop_a_live_required_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGh([*EXPECTED_CONTEXTS, "Some check the spec forgot"])
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 1
    assert not fake.wrote_protection()


@pytest.mark.usefixtures("_has_gh")
def test_write_proceeds_when_the_spec_covers_every_live_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGh(list(EXPECTED_CONTEXTS))
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant"]) == 0
    assert fake.wrote_protection()


@pytest.mark.usefixtures("_has_gh")
def test_dry_run_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh(list(EXPECTED_CONTEXTS))
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant", "--dry-run"]) == 0
    assert all("-X" not in call for call in fake.calls), "a dry run must not send a write"


@pytest.mark.usefixtures("_has_gh")
def test_check_mode_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGh(list(EXPECTED_CONTEXTS))
    monkeypatch.setattr(harden_github, "_gh", fake)

    assert harden_github.main(["kphutt/gdmutant", "--check"]) == 0
    assert all("-X" not in call for call in fake.calls), "--check must not send a write"
