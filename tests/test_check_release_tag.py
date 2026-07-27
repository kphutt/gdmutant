"""Tests for the release-tag guard (`scripts/check_release_tag.py`).

This guard is the last thing standing between a mistyped tag and an irreversible PyPI upload, and
it only ever runs in CI — where a bug in it shows up as a bad release, not a failing test. So the
mismatch logic is pinned here, where it fails fast and locally.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_release_tag.py"
_spec = importlib.util.spec_from_file_location("check_release_tag", _SCRIPT)
assert _spec and _spec.loader
check_release_tag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_release_tag)


def test_matching_tag_is_accepted() -> None:
    assert check_release_tag.mismatch("v1.2.3", "1.2.3") is None


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v0.2.0", "0.1.0"),  # the real hazard: tag ahead of the package
        ("v0.1.0", "0.2.0"),  # and behind it
        ("v1.2.3-hotfix", "1.2.3"),  # a suffix must not be truncated into a match
        ("v1.2", "1.2.3"),
        ("v1.2.3", "1.2.30"),  # no prefix matching
    ],
)
def test_mismatched_tag_is_rejected(tag: str, version: str) -> None:
    problem = check_release_tag.mismatch(tag, version)
    assert problem is not None, f"{tag!r} vs {version!r} must be rejected"
    assert tag in problem and version in problem, "the message must name both, to be actionable"


@pytest.mark.parametrize("tag", ["1.2.3", "release-1.2.3", "", "V1.2.3"])
def test_tags_without_the_v_prefix_are_rejected(tag: str) -> None:
    """The workflow triggers on `v*.*.*`, so anything else reaching this guard is a mistake.

    `V1.2.3` is included deliberately: the check is case-sensitive, and a capitalised tag would
    not have fired the workflow in the first place.
    """
    assert check_release_tag.mismatch(tag, "1.2.3") is not None


def test_reads_the_real_pyproject() -> None:
    """The guard must read the version from the actual packaged metadata, not a copy."""
    version = check_release_tag.packaged_version()
    assert version, "pyproject.toml must declare a [project] version"
    assert check_release_tag.mismatch(f"v{version}", version) is None


def test_main_reports_usage_without_a_tag() -> None:
    assert check_release_tag.main(["check_release_tag.py"]) == 2


def test_main_fails_on_a_mismatched_tag() -> None:
    assert check_release_tag.main(["check_release_tag.py", "v999.999.999"]) == 1


def test_main_succeeds_on_the_packaged_version() -> None:
    version = check_release_tag.packaged_version()
    assert check_release_tag.main(["check_release_tag.py", f"v{version}"]) == 0


# --- The "is this commit on main?" guard in the release workflow --------------------------------
# This guard lives in YAML, not Python, so nothing else in the suite would notice it being dropped,
# reordered, or defanged. It is pinned here because the failure it prevents is unrecoverable: once
# the Release is created, publish.yml uploads to PyPI, and a PyPI version number can never be
# reused. The ordering assertion is the load-bearing one -- a guard that runs after the Release is
# created is not a guard.

_RELEASE_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"


def _workflow() -> str:
    return _RELEASE_WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_checks_the_tag_is_an_ancestor_of_main() -> None:
    assert "merge-base --is-ancestor" in _workflow(), (
        "release.yml must refuse a tag whose commit is not an ancestor of main"
    )


def _ancestor_check_line() -> str:
    """The single line that runs the ancestor comparison."""
    lines = [ln for ln in _workflow().splitlines() if "merge-base --is-ancestor" in ln]
    assert len(lines) == 1, f"expected exactly one ancestor check, found {len(lines)}"
    return lines[0]


def test_the_ancestor_check_resolves_main_from_the_remote() -> None:
    """A local `main` may not exist on the runner; the check must compare against the remote.

    Asserted on the comparison line itself, not the whole file: `origin/main` also appears in the
    fetch refspec above it, so a file-wide substring check still passes if the comparison is
    quietly retargeted at a bare `main` that may not exist on the runner — where `merge-base`
    would fail open or error out instead of answering the question.
    """
    assert "origin/main" in _ancestor_check_line()


def test_the_ancestor_check_has_full_history_to_walk() -> None:
    """`git merge-base` needs main's history — a shallow clone has none of it."""
    assert "fetch-depth: 0" in _workflow()


def test_both_guards_run_before_the_release_is_created() -> None:
    """Creating the Release fires the PyPI upload, so every guard must precede it.

    Anchored on `run: gh release create`, not the bare command name: the workflow's header comment
    also mentions `gh release create --verify-tag` when it explains the design, and matching that
    prose instead of the step would make this test pass no matter where the guards actually sit.
    """
    workflow = _workflow()
    creates_release = workflow.index("run: gh release create")
    for guard in ("scripts/check_release_tag.py", "merge-base --is-ancestor"):
        assert workflow.index(guard) < creates_release, (
            f"{guard!r} must run before the Release is created — afterwards the upload has already "
            "happened and a PyPI version number can never be reused"
        )


# --- Executing the guard for real ---------------------------------------------------------------
# Everything above reads the workflow as text, which pins that the guard is present and correctly
# ordered but not that it is correctly *wired*: an inverted condition (`if` where `if !` belongs)
# passes every substring assertion while doing the exact opposite — publishing off-main commits and
# blocking the legitimate ones. So the step's shell body is lifted out of the YAML and run against a
# throwaway repo, once for a commit on main and once for a commit off it.


def _guard_script() -> str:
    """The shell body of the `Verify the tagged commit is on main` step, dedented."""
    lines = _workflow().splitlines()
    anchor = next(i for i, ln in enumerate(lines) if "merge-base --is-ancestor" in ln)
    start = max(i for i in range(anchor) if lines[i].strip() == "run: |")
    indent = len(lines[start + 1]) - len(lines[start + 1].lstrip())
    body = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:] if line.strip() else "")
    return "\n".join(body)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _scratch_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone whose `origin` has one commit on main, plus a second commit that never reached it.

    Returns the working clone and the two commit SHAs (on-main, off-main).
    """
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )

    work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
    _git("config", "user.email", "t@example.invalid", cwd=work)
    _git("config", "user.name", "test", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / "f.txt").write_text("on main\n", encoding="utf-8")
    _git("add", "f.txt", cwd=work)
    _git("commit", "-m", "on main", cwd=work)
    _git("push", "origin", "main", cwd=work)
    on_main = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()

    # A commit that exists only on a side branch — exactly the shape of a tag pushed at an
    # unreviewed commit, which is what the guard has to refuse.
    _git("checkout", "-b", "side", cwd=work)
    (work / "f.txt").write_text("off main\n", encoding="utf-8")
    _git("commit", "-am", "off main", cwd=work)
    off_main = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()

    return work, on_main, off_main


def _usable_bash() -> str | None:
    """The path to a bash that can actually run a script, or None.

    ``shutil.which("bash")`` alone is not proof. On GitHub's windows-2025 runners it resolves to the
    WSL launcher stub at ``C:\\Windows\\System32\\bash.exe``, which exists but has no distribution
    installed: it prints a UTF-16 "install a distro" notice and exits 1 — a failure that looks just
    like the guard rejecting a commit, which would turn these tests into noise. So probe the
    candidate rather than trusting its name. A Windows machine with Git Bash on PATH still runs
    these tests; one with only the WSL stub skips them.
    """
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - platform-dependent
        return None
    try:
        probe = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", "printf ok"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform-dependent
        return None
    return bash if probe.stdout.strip() == "ok" else None


_BASH = _usable_bash()
_NEEDS_BASH = pytest.mark.skipif(_BASH is None, reason="no usable bash to run the guard's shell")


def test_a_posix_platform_always_has_a_usable_bash() -> None:
    """The two tests below are the guard's only behavioural coverage.

    A silent skip on Linux — where the release job actually runs (`runs-on: ubuntu-24.04`) — would
    be indistinguishable from them passing, so pin that the probe finds bash there. Windows is
    exempt: the guard never executes on a Windows runner.
    """
    if sys.platform != "win32":
        assert _BASH is not None, "bash must be usable here; the guard's real tests need it"


def _run_guard(work: Path, commit: str) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    _git("checkout", "--detach", commit, cwd=work)
    return subprocess.run(
        # GitHub's default shell for a `run:` block on Linux, so the step behaves here as it does
        # on the runner. The script arrives on stdin to keep Windows path translation out of it.
        [_BASH, "--noprofile", "--norc", "-eo", "pipefail", "-s"],
        input=_guard_script(),
        cwd=work,
        capture_output=True,
        text=True,
        env={**os.environ, "GITHUB_REF_NAME": "v9.9.9"},
    )


@_NEEDS_BASH
def test_the_guard_accepts_a_commit_that_is_on_main(tmp_path: Path) -> None:
    work, on_main, _ = _scratch_repo(tmp_path)
    result = _run_guard(work, on_main)
    assert result.returncode == 0, f"a commit on main must be releasable:\n{result.stderr}"


@_NEEDS_BASH
def test_the_guard_refuses_a_commit_that_never_reached_main(tmp_path: Path) -> None:
    """The whole point: a version tag pushed at an unreviewed commit must not reach PyPI."""
    work, _, off_main = _scratch_repo(tmp_path)
    result = _run_guard(work, off_main)
    assert result.returncode != 0, "a commit that is not an ancestor of main must be refused"
    assert "not an ancestor" in result.stdout + result.stderr, (
        "the failure must say why, so the maintainer can act on it"
    )
