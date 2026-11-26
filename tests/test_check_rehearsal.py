"""scripts/check_rehearsal.py: a release ships only a commit the release rehearsal passed on."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "check_rehearsal", Path(__file__).resolve().parent.parent / "scripts" / "check_rehearsal.py"
)
assert _spec and _spec.loader
check_rehearsal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_rehearsal)

SHA = "c" * 40


def _run(conclusion: str | None, status: str = "completed", sha: str = SHA) -> dict[str, Any]:
    return {"head_sha": sha, "conclusion": conclusion, "status": status}


def test_a_success_passes_even_beside_a_failed_or_cancelled_run() -> None:
    runs = [_run("failure"), _run("cancelled"), _run("success")]
    assert check_rehearsal.decide(runs, SHA)[0] == "pass"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", None])
def test_anything_but_a_finished_success_fails(conclusion: str | None) -> None:
    assert check_rehearsal.decide([_run(conclusion)], SHA)[0] == "fail"


def test_a_refusal_names_the_newest_run_to_re_run() -> None:
    runs = [{**_run("cancelled"), "id": 7}, {**_run("failure"), "id": 9}, {**_run(None), "id": 8}]
    reason = check_rehearsal.decide(runs, SHA)[1]
    assert "Run 9 ended failure" in reason and "`gh run rerun 9`" in reason


def test_with_no_run_it_says_to_rehearse_the_tag() -> None:
    reason = check_rehearsal.decide([], SHA)[1]
    assert "No run exists" in reason and "rehearse-release.yml --ref <tag>" in reason


def test_it_waits_while_running_then_passes(capsys: pytest.CaptureFixture[str]) -> None:
    answers = iter([[_run(None, "in_progress")], [_run("success")]])
    naps: list[float] = []
    code = check_rehearsal.check(
        SHA, "o/r", 5, fetch=lambda repo, sha: next(answers), sleep=naps.append, poll_seconds=7
    )
    assert code == 0 and naps == [7]
    assert "still running, checking again in 7s" in capsys.readouterr().out


def test_it_gives_up_once_the_wait_runs_out(capsys: pytest.CaptureFixture[str]) -> None:
    naps: list[float] = []
    code = check_rehearsal.check(
        SHA, "o/r", 0, fetch=lambda repo, sha: [_run(None, "queued")], sleep=naps.append
    )
    assert code == 1 and naps == []
    assert "::error::the release rehearsal on" in capsys.readouterr().out


def test_a_failure_stops_at_once_without_waiting() -> None:
    naps: list[float] = []
    fetch = lambda repo, sha: [_run("failure")]  # noqa: E731
    assert check_rehearsal.check(SHA, "o/r", 5, fetch=fetch, sleep=naps.append) == 1
    assert naps == []


def test_fetch_asks_for_this_commits_rehearsal_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"workflow_runs": [_run("success")]}))

    monkeypatch.setattr(check_rehearsal.subprocess, "run", fake)
    assert check_rehearsal.fetch_runs("o/r", SHA) == [_run("success")]
    assert seen == [
        [
            "gh",
            "api",
            f"repos/o/r/actions/workflows/rehearse-release.yml/runs?head_sha={SHA}&per_page=100",
        ]
    ]


@pytest.mark.parametrize("body", ['{"workflow_runs": {}}', "{}", "not json"])
def test_fetch_raises_on_an_answer_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    monkeypatch.setattr(
        check_rehearsal.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, body),
    )
    with pytest.raises((ValueError, KeyError)):
        check_rehearsal.fetch_runs("o/r", SHA)


def test_main_reads_the_repo_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, float]] = []
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(check_rehearsal, "check", lambda *args: calls.append(args) or 0)
    assert check_rehearsal.main([SHA, "--wait-minutes", "3"]) == 0
    assert calls == [(SHA, "o/r", 3.0)]


# --- Wiring: both release paths run the guard, before anything irreversible ----------------------
# Two paths that should agree (AGENTS.md's recurring bug two): release.yml drafts the Release, and
# publish.yml gates the upload even for a Release made in the web UI. Both have to run it.

_WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_GUARD_JOBS = {"release.yml": "release", "publish.yml": "provenance"}


def _job(workflow: str) -> dict[str, Any]:
    jobs = yaml.safe_load((_WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"]
    return dict(jobs[_GUARD_JOBS[workflow]])


@pytest.mark.parametrize("workflow", sorted(_GUARD_JOBS))
def test_both_release_paths_check_the_rehearsal_on_the_released_commit(workflow: str) -> None:
    job = _job(workflow)
    runs = [step.get("run", "") for step in job["steps"]]
    guard = [run for run in runs if "scripts/check_rehearsal.py" in run]
    assert guard == ['python3 scripts/check_rehearsal.py "$(git rev-parse HEAD)"']
    assert job["permissions"].get("actions") == "read"
    assert job["timeout-minutes"] > 20  # longer than the guard's own wait, or the job dies first


def test_the_release_is_drafted_only_after_the_rehearsal_guard() -> None:
    runs = [step.get("run", "") for step in _job("release.yml")["steps"]]
    guard = next(i for i, run in enumerate(runs) if "check_rehearsal.py" in run)
    draft = next(i for i, run in enumerate(runs) if run.startswith("gh release create"))
    assert guard < draft


def test_the_upload_needs_the_job_that_runs_the_rehearsal_guard() -> None:
    jobs = yaml.safe_load((_WORKFLOWS / "publish.yml").read_text(encoding="utf-8"))["jobs"]
    assert "provenance" in jobs["publish-pypi"]["needs"]
