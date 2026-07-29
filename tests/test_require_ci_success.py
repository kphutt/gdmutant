"""Tests for the CI-status half of the release gate (`scripts/require_ci_success.py`).

This guard decides whether a commit is allowed to reach PyPI, and it only ever runs in CI — where
a bug in it shows up as either a bad release or a release that can never happen, not as a failing
test. Every fail-closed branch is pinned here, where it fails fast and locally.

The bias under test is deliberate and one-directional: anything the guard cannot positively prove
must be a refusal. A guard that opens on ambiguity is worse than no guard, because it reads as
protection while providing none.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "require_ci_success.py"
_spec = importlib.util.spec_from_file_location("require_ci_success", _SCRIPT)
assert _spec and _spec.loader
require_ci_success = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(require_ci_success)

REPO = "kphutt/gdmutant"
SHA = "a" * 40
CI = require_ci_success.CI_WORKFLOW


def run(**overrides: Any) -> dict:
    """A workflow-run payload that passes the gate, unless an override breaks it."""
    payload = {
        "path": CI,
        "head_sha": SHA,
        "event": "push",
        "repository": {"full_name": REPO},
        "status": "completed",
        "conclusion": "success",
        "run_started_at": "2026-07-29T00:00:00Z",
        "html_url": "https://example.invalid/run",
    }
    payload.update(overrides)
    return payload


# --- the accept path -------------------------------------------------------------------------


def test_a_completed_successful_push_run_passes() -> None:
    kept = require_ci_success.matching([run()], REPO, SHA)
    assert kept, "a genuine successful run must survive filtering"
    assert require_ci_success.verdict(kept, SHA) is None


# --- what must never satisfy the gate --------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        (
            "path",
            ".github/workflows/other.yml",
            "a different workflow must not stand in for ci.yml",
        ),
        ("head_sha", "b" * 40, "a run for another commit proves nothing about this one"),
        ("event", "pull_request", "a fork PR sharing this content must not satisfy the gate"),
        ("repository", {"full_name": "someone/else"}, "a run in another repo is not evidence"),
    ],
)
def test_impostor_runs_are_filtered_out(field: str, value: Any, why: str) -> None:
    assert require_ci_success.matching([run(**{field: value})], REPO, SHA) == [], why


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("completed", "failure"),
        ("completed", "cancelled"),  # the real incident: a deploy rode a cancelled run
        ("completed", "timed_out"),
        ("completed", "action_required"),
        ("completed", None),
        ("in_progress", None),  # not finished yet is not the same as passed
        ("queued", None),
        (None, None),
    ],
)
def test_only_a_completed_success_passes(status: str | None, conclusion: str | None) -> None:
    kept = require_ci_success.matching([run(status=status, conclusion=conclusion)], REPO, SHA)
    problem = require_ci_success.verdict(kept, SHA)
    assert problem is not None, f"status={status!r} conclusion={conclusion!r} must be refused"
    assert SHA in problem, "the message must name the commit, to be actionable"


def test_no_runs_at_all_is_a_refusal() -> None:
    problem = require_ci_success.verdict([], SHA)
    assert problem is not None, "a commit CI never ran on must not be publishable"
    assert "never verified" in problem


# --- the re-run ordering rule ------------------------------------------------------------------


def test_the_most_recent_run_decides_not_any_run() -> None:
    """An old success must not shadow a newer failure."""
    kept = require_ci_success.matching(
        [
            run(conclusion="success", run_started_at="2026-07-01T00:00:00Z"),
            run(conclusion="failure", run_started_at="2026-07-29T00:00:00Z"),
        ],
        REPO,
        SHA,
    )
    assert require_ci_success.verdict(kept, SHA) is not None, "the newer failure must win"


def test_a_since_fixed_rerun_is_allowed_to_pass() -> None:
    """The mirror case: an old failure must not permanently poison a commit."""
    kept = require_ci_success.matching(
        [
            run(conclusion="failure", run_started_at="2026-07-01T00:00:00Z"),
            run(conclusion="success", run_started_at="2026-07-29T00:00:00Z"),
        ],
        REPO,
        SHA,
    )
    assert require_ci_success.verdict(kept, SHA) is None, "the newer success must win"


def test_a_run_missing_its_timestamp_sorts_last_rather_than_crashing() -> None:
    kept = require_ci_success.matching(
        [run(run_started_at=None), run(conclusion="failure")], REPO, SHA
    )
    assert len(kept) == 2
    assert kept[0].get("run_started_at"), "a dated run must outrank an undated one"


# --- fetch_runs fails closed on every transport problem ----------------------------------------


def test_api_failure_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "gh: HTTP 503"),
    )
    runs, error = require_ci_success.fetch_runs(REPO, SHA)
    assert runs is None and "503" in error


@pytest.mark.parametrize("body", ["not json", "{}", "[]", "null"])
def test_a_malformed_response_is_a_refusal(body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, body, "")
    )
    runs, error = require_ci_success.fetch_runs(REPO, SHA)
    assert runs is None, f"{body!r} must not be read as an empty-but-valid run list"
    assert error


def test_the_query_pins_the_http_method(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh api` silently switches GET->POST when `-f` params are present unless -X GET is given."""
    seen: dict[str, list[str]] = {}

    def capture(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"workflow_runs": []}), "")

    monkeypatch.setattr(subprocess, "run", capture)
    require_ci_success.fetch_runs(REPO, SHA)
    assert "-X" in seen["cmd"] and seen["cmd"][seen["cmd"].index("-X") + 1] == "GET"
    assert f"head_sha={SHA}" in seen["cmd"]
    assert "event=push" in seen["cmd"]


# --- the exit code is what the workflow actually consumes --------------------------------------


def test_main_exits_nonzero_when_ci_did_not_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, json.dumps({"workflow_runs": [run(conclusion="failure")]}), ""
        ),
    )
    assert require_ci_success.main(["require_ci_success.py", "--repo", REPO, "--sha", SHA]) == 1


def test_main_exits_zero_on_a_clean_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, json.dumps({"workflow_runs": [run()]}), ""
        ),
    )
    assert require_ci_success.main(["require_ci_success.py", "--repo", REPO, "--sha", SHA]) == 0
