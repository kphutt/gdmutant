"""Tests for `scripts/run_gitleaks.py`, the pre-commit secret-scan wrapper.

Cloud CI's `Secret scan (gitleaks)` job is the real, unbypassable gate now (a required
branch-protection check on `main`, per ADR-0012's 2026-08-04 Correction) — but this local hook is
still the fast, catches-it-earlier layer run before a secret is even committed, so a machine
without `gitleaks` on PATH must never look indistinguishable from a clean scan. Reviewed live: it
used to (a quiet one-line note to stdout), which is exactly the "gate that passes without checking
anything" shape AGENTS.md calls out.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_gitleaks.py"
_spec = importlib.util.spec_from_file_location("run_gitleaks", _SCRIPT)
assert _spec and _spec.loader
run_gitleaks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_gitleaks)


def test_missing_gitleaks_still_exits_zero_but_warns_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exit 0 is deliberate (a hard failure here just gets bypassed with --no-verify, which skips
    # every hook — see the script's own docstring), but the warning must be unmissable, not a
    # one-liner easily mistaken for "scanned, clean".
    monkeypatch.setattr(run_gitleaks.shutil, "which", lambda _name: None)
    rc = run_gitleaks.main()
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOT" in err and "SCAN" in err  # loud, not a quiet aside
    assert "gitleaks" in err.lower()


def test_missing_gitleaks_warning_goes_to_stderr_not_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_gitleaks.shutil, "which", lambda _name: None)
    run_gitleaks.main()
    assert capsys.readouterr().out == ""


def test_present_gitleaks_runs_a_staged_pre_commit_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_gitleaks.shutil, "which", lambda _name: "/usr/bin/gitleaks")
    captured: dict[str, object] = {}

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0)

    monkeypatch.setattr(run_gitleaks.subprocess, "run", fake_run)
    rc = run_gitleaks.main()
    assert rc == 0
    assert captured["argv"] == [
        "/usr/bin/gitleaks",
        "git",
        "--pre-commit",
        "--redact",
        "--staged",
        "--verbose",
    ]


def test_a_real_leak_found_propagates_gitleaks_own_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_gitleaks.shutil, "which", lambda _name: "/usr/bin/gitleaks")
    monkeypatch.setattr(
        run_gitleaks.subprocess,
        "run",
        lambda argv: subprocess.CompletedProcess(argv, returncode=1),
    )
    assert run_gitleaks.main() == 1
