"""Every gate script must fail on empty, missing or errored input, and on a bypass switch left on.

A gate that passes without checking anything is indistinguishable from a gate that passed. This
module feeds each gate script in `scripts/` the thing that is easiest to get wrong -- nothing, a
missing file, a sub-command that errors, a switch that turns the check off -- and requires a
non-zero exit (or the script's documented failing verdict).

Scope. Every case here is offline and fast: sub-commands are faked, no mutation run starts, and
nothing touches the network. Good-input cases live in each gate's own test module; this one only
asks "what does it do when there is nothing to check?". Gates that need the GitHub API are out of
scope here (`harden_github.py`'s live read and write paths); only its offline derivation and
argument handling are exercised.

Where a gate passes on empty input, the case is either a declared allowed-empty exception (a normal
test asserting the documented behaviour, with a comment naming where it is documented) or, if the
pass is not documented as intended, a strict xfail with a one-line reason. The strictness is the
point: the day such a gate is fixed, the xfail turns into a failure that says to delete the marker.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = REPO / "scripts"


def _load(name: str, register: bool = False):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(f"{name}_empty_input", _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    if register:  # `@dataclass` resolves its module out of sys.modules while the class body runs.
        sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


baseline = _load("check_mutation_baseline")
verify_local = _load("verify_local")
license_check_local = _load("license_check_local")
check_licenses = _load("check_licenses")
check_release_tag = _load("check_release_tag")
published = _load("check_published_package", register=True)
readme_images = _load("check_readme_images", register=True)
harden_github = _load("harden_github", register=True)
run_gitleaks = _load("run_gitleaks")


class _Done:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


# --- scripts/check_mutation_baseline.py (pre-commit manual hook, mutation.yml) -------------------


# Declared allowed-empty: a clean diff has nothing to mutate. Documented by the existing
# `test_no_files_changed_still_passes` in tests/test_check_mutation_baseline_cap.py.
def test_baseline_gate_passes_when_there_is_nothing_to_mutate() -> None:
    with mock.patch.object(baseline, "changed_gdmutant_files", return_value=[]):
        assert baseline.main([]) == 0


def test_baseline_gate_fails_when_counting_mutants_errors() -> None:
    boom = subprocess.CalledProcessError(1, ["poodle"], output="", stderr="boom")
    with (
        mock.patch.object(baseline, "changed_gdmutant_files", return_value=["gdmutant/x.py"]),
        mock.patch.object(baseline, "count_mutants_per_file", side_effect=boom),
    ):
        assert baseline.main([]) != 0


def test_baseline_gate_fails_when_the_mutation_run_itself_errors() -> None:
    with (
        mock.patch.object(baseline, "changed_gdmutant_files", return_value=["gdmutant/x.py"]),
        mock.patch.object(baseline, "count_mutants_per_file", return_value={"gdmutant/x.py": 3}),
        mock.patch.object(baseline.subprocess, "run", return_value=_Done(2, "usage error")),
    ):
        assert baseline.main([]) != 0


def test_baseline_gate_fails_when_the_mutant_count_is_missing_for_a_changed_file() -> None:
    # An empty count map: the file is changed but poodle reported nothing for it.
    with (
        mock.patch.object(baseline, "changed_gdmutant_files", return_value=["gdmutant/x.py"]),
        mock.patch.object(baseline, "count_mutants_per_file", return_value={}),
        mock.patch.object(baseline.subprocess, "run", return_value=_Done(0, "0 mutants")),
    ):
        assert baseline.main([]) != 0


@pytest.mark.parametrize("cap", ["0", "-1"])
def test_baseline_gate_fails_when_the_cap_env_var_leaves_nothing_inside_it(
    monkeypatch: pytest.MonkeyPatch, cap: str
) -> None:
    # The cap is a switch: set low enough it drops every file, which must not read as a pass.
    monkeypatch.setenv(baseline.MAX_MUTANTS_ENV_VAR, cap)
    with (
        mock.patch.object(baseline, "changed_gdmutant_files", return_value=["gdmutant/x.py"]),
        mock.patch.object(baseline, "count_mutants_per_file", return_value={"gdmutant/x.py": 3}),
        mock.patch.object(baseline.subprocess, "run", return_value=_Done(0, "clean")),
    ):
        assert baseline.main([]) != 0


def test_baseline_gate_does_not_swallow_a_failing_git_diff() -> None:
    # A git error must surface (non-zero via traceback), never read as "no files changed".
    boom = subprocess.CalledProcessError(128, ["git", "diff"])
    with (
        mock.patch.object(baseline.subprocess, "run", side_effect=boom),
        pytest.raises(subprocess.CalledProcessError),
    ):
        baseline.main([])


# --- scripts/verify_local.py (the one command that mirrors ci.yml's verify job) ------------------


def _write_workflow(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ci.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_verify_local_fails_when_the_workflow_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_local, "WORKFLOW", tmp_path / "absent.yml")
    with pytest.raises(SystemExit) as excinfo:
        verify_local.load_steps("verify")
    assert excinfo.value.code not in (0, None)


def test_verify_local_fails_when_the_workflow_file_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify_local, "WORKFLOW", _write_workflow(tmp_path, ""))
    with pytest.raises(SystemExit) as excinfo:
        verify_local.load_steps("verify")
    assert excinfo.value.code not in (0, None)


def test_verify_local_fails_when_the_job_has_no_steps_to_replay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A job made only of `uses:` steps has nothing replayable; it must not report success.
    body = "jobs:\n  verify:\n    steps:\n      - uses: actions/checkout@v4\n"
    monkeypatch.setattr(verify_local, "WORKFLOW", _write_workflow(tmp_path, body))
    with pytest.raises(SystemExit) as excinfo:
        verify_local.load_steps("verify")
    assert excinfo.value.code not in (0, None)


def test_verify_local_fails_when_the_job_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = "jobs:\n  other:\n    steps:\n      - run: echo hi\n"
    monkeypatch.setattr(verify_local, "WORKFLOW", _write_workflow(tmp_path, body))
    with pytest.raises(SystemExit) as excinfo:
        verify_local.load_steps("verify")
    assert excinfo.value.code not in (0, None)


def test_verify_local_command_line_fails_for_an_unknown_job() -> None:
    # Called the way the hook calls it: as a script, with --job.
    done = subprocess.run(
        [sys.executable, str(_SCRIPTS / "verify_local.py"), "--job", "no-such-job"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert done.returncode != 0


def test_verify_local_does_not_count_a_missing_bash_as_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_local.shutil, "which", lambda _name: None)
    assert verify_local.run_step({"name": "x", "run": "true", "shell": "bash"}) is False


def test_verify_local_exits_nonzero_when_any_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    steps = [{"name": "a", "run": "x", "shell": None}, {"name": "b", "run": "y", "shell": None}]
    monkeypatch.setattr(verify_local, "load_steps", lambda _job: steps)
    monkeypatch.setattr(verify_local, "run_step", lambda step: step["name"] != "b")
    monkeypatch.setattr(sys, "argv", ["verify_local.py"])
    assert verify_local.main() == 1


def test_verify_local_step_runner_reports_a_failing_command_as_failed() -> None:
    failing = f'"{sys.executable}" -c "import sys; sys.exit(3)"'
    assert verify_local.run_step({"name": "x", "run": failing, "shell": None}) is False


# --- scripts/license_check_local.py (pre-push hook wrapping verify_local's license job) ----------


def test_license_check_hook_propagates_a_failing_inner_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(license_check_local.subprocess, "run", lambda *a, **k: _Done(3))
    assert license_check_local.main() == 3


# --- scripts/check_licenses.py (ci.yml license-check job) ----------------------------------------


def test_license_gate_fails_when_pip_licenses_cannot_be_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("piplicenses")

    monkeypatch.setattr(check_licenses.subprocess, "run", missing)
    assert check_licenses.main() != 0


def test_license_gate_fails_when_pip_licenses_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def errored(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, ["piplicenses"])

    monkeypatch.setattr(check_licenses.subprocess, "run", errored)
    assert check_licenses.main() != 0


@pytest.mark.parametrize("license_text", ["", "UNKNOWN"])
def test_license_gate_fails_on_a_package_with_no_license_metadata(
    monkeypatch: pytest.MonkeyPatch, license_text: str
) -> None:
    listing = f'[{{"Name": "mystery", "License": "{license_text}"}}]'
    monkeypatch.setattr(check_licenses.subprocess, "run", lambda *a, **k: _Done(0, listing))
    assert check_licenses.main() != 0


# --- scripts/check_release_tag.py (publish.yml, release.yml) -------------------------------------


def test_release_tag_gate_fails_with_no_tag_argument() -> None:
    assert check_release_tag.main(["check_release_tag.py"]) != 0


def test_release_tag_gate_fails_on_an_empty_tag() -> None:
    assert check_release_tag.main(["check_release_tag.py", ""]) != 0


def test_release_tag_gate_fails_when_the_changelog_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    version = check_release_tag.packaged_version()
    monkeypatch.setattr(check_release_tag, "CHANGELOG", tmp_path / "absent.md")
    assert check_release_tag.main(["check_release_tag.py", f"v{version}"]) != 0


def test_release_tag_gate_fails_when_the_changelog_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    version = check_release_tag.packaged_version()
    empty = tmp_path / "CHANGELOG.md"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_release_tag, "CHANGELOG", empty)
    assert check_release_tag.main(["check_release_tag.py", f"v{version}"]) != 0


# --- scripts/check_published_package.py (publish.yml, after the upload) --------------------------


def _fake_run(answers: dict[str, object]):  # type: ignore[no-untyped-def]
    """A runner answering by argv fragment; anything unmatched exits 0 with no output."""

    def run(argv: Sequence[str], cwd: Path):  # type: ignore[no-untyped-def]
        joined = " ".join(str(part) for part in argv)
        for fragment, result in answers.items():
            if fragment in joined:
                return result
        return published.Result(0, "")

    return run


def test_published_check_fails_without_a_version() -> None:
    with pytest.raises(SystemExit) as excinfo:
        published.main(["check_published_package.py"])
    assert excinfo.value.code not in (0, None)


def test_published_check_fails_when_every_command_succeeds_but_prints_nothing(
    tmp_path: Path,
) -> None:
    # Empty --version output, empty import location, empty dry run: all "exit 0, said nothing".
    code, _message = published.check(
        "1.2.3", tmp_path, "https://index.test/simple", _fake_run({}), 1, 0.0, lambda _s: None
    )
    assert code != published.OK


def test_published_check_fails_when_no_install_attempt_is_made(tmp_path: Path) -> None:
    code, _message = published.check(
        "1.2.3", tmp_path, "https://index.test/simple", _fake_run({}), 0, 0.0, lambda _s: None
    )
    assert code != published.OK


def test_published_check_fails_when_the_install_errors(tmp_path: Path) -> None:
    runner = _fake_run({"pip install": published.Result(1, "ERROR: some resolver failure")})
    code, _message = published.check(
        "1.2.3", tmp_path, "https://index.test/simple", runner, 3, 0.0, lambda _s: None
    )
    assert code != published.OK


def test_published_check_main_exits_nonzero_when_the_environment_cannot_be_built(
    tmp_path: Path,
) -> None:
    runner = _fake_run({"-m venv": published.Result(1, "no venv module")})
    argv = ["check_published_package.py", "--version", "v1.2.3", "--workdir", str(tmp_path)]
    assert published.main(argv, run=runner, sleep=lambda _s: None) != 0


def test_published_version_check_fails_on_empty_output() -> None:
    assert published.version_problem(published.Result(0, ""), "1.2.3") is not None


def test_published_location_check_fails_on_empty_output(tmp_path: Path) -> None:
    assert published.location_problem(published.Result(0, ""), tmp_path / "venv") is not None


def test_published_smoke_check_fails_on_empty_output() -> None:
    assert published.smoke_problem(published.Result(0, "")) is not None


def test_published_example_check_fails_when_nothing_was_written(tmp_path: Path) -> None:
    assert published.example_problem(published.Result(0, ""), tmp_path / "absent.gd") is not None


# --- scripts/check_readme_images.py (publish.yml, before the upload) -----------------------------


def test_readme_image_gate_fails_when_the_dist_directory_is_missing(tmp_path: Path) -> None:
    argv = ["check_readme_images.py", "--dist-dir", str(tmp_path / "absent")]
    assert readme_images.main(argv) != 0


def test_readme_image_gate_fails_when_no_attempt_is_allowed(tmp_path: Path) -> None:
    # `--attempts 0` must not turn the network check into a no-op that reports success.
    wheel = tmp_path / "gdmutant-0.1.0-py3-none-any.whl"
    metadata = (
        "Metadata-Version: 2.4\nName: gdmutant\nVersion: 0.1.0\n"
        "Description-Content-Type: text/markdown\n\n"
        '<img src="https://example.test/x.svg">\n'
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("gdmutant-0.1.0.dist-info/METADATA", metadata)

    def fetch(_url: str):  # type: ignore[no-untyped-def]
        return readme_images.Response(status=200, content_type="image/svg+xml")

    argv = ["check_readme_images.py", "--dist-dir", str(tmp_path), "--attempts", "0"]
    assert readme_images.main(argv, fetch=fetch, sleep=lambda _s: None) != 0


# --- scripts/harden_github.py (offline parts only; live reads and writes need the API) -----------


def test_hardening_cannot_derive_required_checks_from_an_empty_workflow_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        harden_github.required_contexts(tmp_path)


def test_hardening_cannot_derive_required_checks_from_a_missing_workflow_directory(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        harden_github.required_contexts(tmp_path / "absent")


def test_hardening_cannot_derive_required_checks_from_empty_workflow_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "ci.yml").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        harden_github.required_contexts(tmp_path)


def test_hardening_exits_nonzero_without_the_gh_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harden_github.shutil, "which", lambda _name: None)
    assert harden_github.main([]) != 0


def test_hardening_exits_nonzero_when_no_required_check_can_be_derived(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harden_github.shutil, "which", lambda _name: "gh")
    monkeypatch.setattr(harden_github, "WORKFLOWS", tmp_path)
    assert harden_github.main(["owner/repo"]) != 0


# --- scripts/run_gitleaks.py (pre-commit hook) ---------------------------------------------------


# Declared allowed-empty: documented in the docstring of scripts/run_gitleaks.py (the required
# `Secret scan (gitleaks)` job in ci.yml is the real gate).
def test_gitleaks_hook_warns_and_passes_when_gitleaks_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run_gitleaks.shutil, "which", lambda _name: None)
    assert run_gitleaks.main() == 0
    assert "NOT SCANNED" in capsys.readouterr().err


def test_gitleaks_hook_propagates_a_failing_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_gitleaks.shutil, "which", lambda _name: "gitleaks")
    monkeypatch.setattr(run_gitleaks.subprocess, "run", lambda *a, **k: _Done(1))
    assert run_gitleaks.main() != 0
