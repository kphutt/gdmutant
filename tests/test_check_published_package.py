"""The post-upload check that the published package installs and runs.

Every command is injected, so the suite never installs anything, never reaches an index, and never
creates a virtual environment. The wiring is pinned separately: this one must run *after* the
upload (nothing else can see what the index serves) and must not be mistaken for a gate.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

_SCRIPT = REPO / "scripts" / "check_published_package.py"
_spec = importlib.util.spec_from_file_location("check_published_package_under_test", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
# Registered before execution, not after: `@dataclass` resolves its own module out of `sys.modules`
# while the class body runs, and a module missing from there fails to import with an AttributeError
# that says nothing about the real cause.
sys.modules[_spec.name] = check
_spec.loader.exec_module(check)


class FakeRunner:
    """Answers each command by the first matching fragment in its argv, and records every call.

    The real ``gdmutant example`` writes a file to `cwd` as a side effect that `example_problem`
    then checks for on disk -- a plain Result can report success without a fake having actually
    done that. `side_effects` lets a fragment run a callback against `cwd` alongside its Result, so
    a fake command can be faithful about what it claims to have done.
    """

    def __init__(
        self,
        answers: dict[str, check.Result],
        default: check.Result | None = None,
        side_effects: dict[str, Callable[[Path], None]] | None = None,
    ):
        self.answers = answers
        self.default = default or check.Result(0, "")
        self.side_effects = side_effects or {}
        self.calls: list[list[str]] = []
        self.cwds: list[Path] = []

    def __call__(self, argv: Sequence[str], cwd: Path) -> check.Result:
        self.calls.append(list(argv))
        self.cwds.append(cwd)
        joined = " ".join(str(part) for part in argv)
        for fragment, effect in self.side_effects.items():
            if fragment in joined:
                effect(cwd)
        for fragment, result in self.answers.items():
            if fragment in joined:
                return result
        return self.default


def _healthy(venv_dir: Path, version: str = "0.1.0") -> dict[str, check.Result]:
    """The answers a working published package gives."""
    site = venv_dir / "lib" / "site-packages" / "gdmutant" / "__init__.py"
    return {
        "--version": check.Result(0, f"gdmutant {version}\n"),
        "gdmutant.__file__": check.Result(0, str(site)),
        "--dry-run": check.Result(0, "3 mutants for smoke.gd:\n  smoke.gd:4:12  comparison\n"),
        "example": check.Result(0, f"Wrote {check.EXAMPLE_NAME}\n"),
    }


def _healthy_side_effects() -> dict[str, Callable[[Path], None]]:
    """Paired with `_healthy`'s answers: the one fake command with a real filesystem side effect."""
    return {"example": lambda cwd: (cwd / check.EXAMPLE_NAME).write_text("stub", encoding="utf-8")}


def _main(tmp_path: Path, runner: FakeRunner, version: str = "v0.1.0", attempts: int = 3) -> int:
    return check.main(
        [
            "check_published_package.py",
            "--version",
            version,
            "--workdir",
            str(tmp_path),
            "--attempts",
            str(attempts),
            "--delay",
            "0",
        ],
        run=runner,
        sleep=lambda _: None,
    )


# --- Small rules --------------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "expected"), [("v0.1.0", "0.1.0"), ("0.1.0", "0.1.0")])
def test_a_tag_or_a_bare_version_both_work(given: str, expected: str) -> None:
    """The workflow has the tag to hand, and guard 1 has already forced the tag to equal the
    packaged version -- so accepting either spelling removes a place for the two to disagree."""
    assert check.normalize_version(given) == expected


def test_the_venv_paths_are_built_from_path_parts() -> None:
    """Never a string with a hardcoded separator: Windows is a deployment target here."""
    python, console_script = check.venv_paths(Path("root") / "venv")
    assert python.parent == console_script.parent
    assert python.parent.parent == Path("root") / "venv"
    assert console_script.stem == "gdmutant"


@pytest.mark.parametrize(
    "output",
    [
        "ERROR: No matching distribution found for gdmutant==0.1.0",
        "ERROR: Could not find a version that satisfies the requirement gdmutant==0.1.0",
    ],
)
def test_pip_not_finding_the_version_reads_as_propagation(output: str) -> None:
    assert check.looks_like_propagation_lag(output)


def test_a_real_install_failure_does_not_read_as_propagation() -> None:
    assert not check.looks_like_propagation_lag("ERROR: Failed building wheel for gdtoolkit")


# --- The assertions about the installed package --------------------------------------------------


def test_a_matching_version_passes() -> None:
    assert check.version_problem(check.Result(0, "gdmutant 0.1.0\n"), "0.1.0") is None


def test_a_mismatched_version_is_reported_with_both_numbers() -> None:
    problem = check.version_problem(check.Result(0, "gdmutant 0.0.9\n"), "0.1.0")
    assert problem is not None
    assert "0.1.0" in problem and "0.0.9" in problem


def test_a_crashing_version_flag_is_a_failure() -> None:
    problem = check.version_problem(check.Result(1, "Traceback ..."), "0.1.0")
    assert problem is not None and "exited 1" in problem


def test_a_package_imported_from_inside_the_venv_passes(tmp_path: Path) -> None:
    venv_dir = tmp_path / "venv"
    inside = venv_dir / "lib" / "site-packages" / "gdmutant" / "__init__.py"
    assert check.location_problem(check.Result(0, str(inside)), venv_dir) is None


def test_a_shadowing_source_tree_fails_the_check(tmp_path: Path) -> None:
    """THE ISOLATION ASSERTION. If the checkout can satisfy `import gdmutant`, the run proves
    nothing about what was published -- it would stay green against an empty wheel."""
    venv_dir = tmp_path / "venv"
    checkout = tmp_path / "checkout" / "gdmutant" / "__init__.py"
    problem = check.location_problem(check.Result(0, str(checkout)), venv_dir)
    assert problem is not None
    assert "shadowed" in problem


def test_an_import_that_fails_outright_is_reported(tmp_path: Path) -> None:
    problem = check.location_problem(check.Result(1, "ModuleNotFoundError"), tmp_path / "venv")
    assert problem is not None


def test_a_dry_run_listing_mutants_passes() -> None:
    assert check.smoke_problem(check.Result(0, "3 mutants for smoke.gd:\n")) is None


def test_a_dry_run_that_lists_nothing_fails() -> None:
    """Exit 0 with no mutants would mean the parser never saw the file -- a green run proving
    nothing, which is the failure mode a bare `--version` check has by design."""
    problem = check.smoke_problem(check.Result(0, ""))
    assert problem is not None and "no mutants" in problem


def test_a_dry_run_that_counts_zero_mutants_fails() -> None:
    """`gdmutant run --dry-run` prints its count line and exits 0 even when it found nothing, so a
    run that produced no mutants satisfies both the exit code and the shape of the output. Reading
    the count is what tells the two apart, and it is the whole claim this check makes: a mutant
    means the parser loaded and parsed real source."""
    problem = check.smoke_problem(check.Result(0, "0 mutants for smoke.gd:\n"))
    assert problem is not None and "no mutants" in problem


def test_a_mutant_count_ending_in_zero_passes() -> None:
    """The count is a number, not a prefix. Testing for the substring "0 mutants for " instead
    would call every multiple of ten a broken release, which is the loudest possible false alarm on
    the most ordinary result."""
    for output in ("10 mutants for smoke.gd:\n", "20 mutants for smoke.gd:\n"):
        assert check.smoke_problem(check.Result(0, output)) is None


def test_a_dry_run_whose_output_does_not_start_with_a_count_fails() -> None:
    """If the CLI's output format changes, this check stops being able to answer its question. It
    says so and fails, rather than raising a ValueError that reads as a bug in the check itself."""
    problem = check.smoke_problem(check.Result(0, "listed 3 mutants for smoke.gd:\n"))
    assert problem is not None and "'listed'" in problem


def test_a_crashing_dry_run_fails() -> None:
    problem = check.smoke_problem(
        check.Result(1, "ModuleNotFoundError: No module named 'gdtoolkit'")
    )
    assert problem is not None and "exited 1" in problem


def test_example_writing_its_file_passes(tmp_path: Path) -> None:
    written = tmp_path / check.EXAMPLE_NAME
    written.write_text("stub", encoding="utf-8")
    assert check.example_problem(check.Result(0, f"Wrote {written}\n"), written) is None


def test_example_exiting_nonzero_fails(tmp_path: Path) -> None:
    problem = check.example_problem(check.Result(1, "Traceback ..."), tmp_path / check.EXAMPLE_NAME)
    assert problem is not None and "exited 1" in problem


def test_example_exiting_zero_but_writing_nothing_fails(tmp_path: Path) -> None:
    # The one shape a bare exit code can't catch: a command that claims success without doing the
    # one thing it exists to do. Package data missing from the wheel looks exactly like this.
    problem = check.example_problem(check.Result(0, "Wrote it\n"), tmp_path / check.EXAMPLE_NAME)
    assert problem is not None and "did not write" in problem


# --- Installing, with the index catching up ------------------------------------------------------


def test_index_lag_is_retried_then_reported_as_lag_not_as_a_broken_package(tmp_path: Path) -> None:
    """A fresh upload is briefly invisible. Calling that "the package is broken" would send whoever
    reads the log hunting a defect that does not exist."""
    runner = FakeRunner(
        {"pip": check.Result(1, "ERROR: No matching distribution found for gdmutant==0.1.0")}
    )
    assert _main(tmp_path, runner, attempts=4) == check.NOT_PROPAGATED
    assert sum("pip" in " ".join(call) for call in runner.calls) == 4


def test_the_wait_is_bounded(tmp_path: Path) -> None:
    """It gives up and says so rather than hanging until the job's timeout kills it with no
    message at all."""
    waits: list[float] = []
    runner = FakeRunner({"pip": check.Result(1, "ERROR: No matching distribution found")})
    code = check.main(
        [
            "check_published_package.py",
            "--version",
            "v0.1.0",
            "--workdir",
            str(tmp_path),
            "--attempts",
            "3",
            "--delay",
            "30",
        ],
        run=runner,
        sleep=waits.append,
    )
    assert code == check.NOT_PROPAGATED
    assert waits == [30.0, 30.0], "it waits between attempts and then stops"


def test_an_install_that_succeeds_on_a_later_attempt_continues(tmp_path: Path) -> None:
    answers = _healthy(tmp_path / "venv")
    effects = _healthy_side_effects()
    attempts = {"n": 0}

    def runner(argv: Sequence[str], cwd: Path) -> check.Result:
        joined = " ".join(str(part) for part in argv)
        if "pip" in joined:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return check.Result(1, "ERROR: No matching distribution found")
            return check.Result(0, "Successfully installed gdmutant-0.1.0")
        for fragment, effect in effects.items():
            if fragment in joined:
                effect(cwd)
        for fragment, result in answers.items():
            if fragment in joined:
                return result
        return check.Result(0, "")

    assert _main(tmp_path, runner) == check.OK  # type: ignore[arg-type]
    assert attempts["n"] == 2


def test_a_real_install_error_fails_immediately(tmp_path: Path) -> None:
    """Repeating a dependency-resolution error for ten minutes buries the message that matters."""
    runner = FakeRunner({"pip": check.Result(1, "ERROR: Failed building wheel for gdtoolkit")})
    assert _main(tmp_path, runner, attempts=5) == check.BROKEN
    assert sum("pip" in " ".join(call) for call in runner.calls) == 1


def test_it_installs_from_the_named_index(tmp_path: Path) -> None:
    runner = FakeRunner(_healthy(tmp_path / "venv"), side_effects=_healthy_side_effects())
    assert _main(tmp_path, runner) == check.OK
    pip_call = next(call for call in runner.calls if "pip" in " ".join(call))
    assert "--index-url" in pip_call
    assert check.DEFAULT_INDEX in pip_call
    assert "gdmutant==0.1.0" in pip_call


def test_a_venv_that_cannot_be_created_is_reported(tmp_path: Path) -> None:
    runner = FakeRunner({"venv": check.Result(1, "Error: Command failed")})
    assert _main(tmp_path, runner) == check.BROKEN


# --- End to end, with everything faked ------------------------------------------------------------


def test_a_healthy_published_package_passes(tmp_path: Path) -> None:
    assert (
        _main(
            tmp_path, FakeRunner(_healthy(tmp_path / "venv"), side_effects=_healthy_side_effects())
        )
        == check.OK
    )


def test_a_wrong_version_on_the_index_fails(tmp_path: Path) -> None:
    answers = _healthy(tmp_path / "venv")
    answers["--version"] = check.Result(0, "gdmutant 0.0.9\n")
    assert _main(tmp_path, FakeRunner(answers)) == check.BROKEN


def test_a_missing_runtime_dependency_fails(tmp_path: Path) -> None:
    """The exact failure a `--version` check would sail past: the entry point works, the parser
    never arrived. This is why the dry run is here."""
    answers = _healthy(tmp_path / "venv")
    answers["--dry-run"] = check.Result(1, "ModuleNotFoundError: No module named 'gdtoolkit'")
    assert _main(tmp_path, FakeRunner(answers)) == check.BROKEN


def test_every_command_runs_in_the_workdir_never_a_checkout(tmp_path: Path) -> None:
    """`.gdmutant.toml` is read from the working directory and `sys.path` starts there, so a
    subprocess launched from a checkout could be answering about the wrong copy of gdmutant."""
    runner = FakeRunner(_healthy(tmp_path / "venv"), side_effects=_healthy_side_effects())
    assert _main(tmp_path, runner) == check.OK
    assert set(runner.cwds) == {tmp_path}


def test_the_scratch_source_is_real_gdscript_the_adapter_can_mutate(tmp_path: Path) -> None:
    """Written as a file the CLI parses for real in the job -- so it has to be valid, tab-indented
    GDScript with something mutable in it, not a placeholder."""
    assert "\t" in check.SMOKE_SOURCE
    assert ">" in check.SMOKE_SOURCE
    runner = FakeRunner(_healthy(tmp_path / "venv"), side_effects=_healthy_side_effects())
    _main(tmp_path, runner)
    assert (tmp_path / "smoke.gd").read_text(encoding="utf-8") == check.SMOKE_SOURCE


def test_a_temporary_workdir_is_cleaned_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[Path] = []

    def fake_mkdtemp(prefix: str = "") -> str:
        directory = tmp_path / "temporary"
        directory.mkdir()
        made.append(directory)
        return str(directory)

    monkeypatch.setattr(check.tempfile, "mkdtemp", fake_mkdtemp)
    runner = FakeRunner(
        _healthy(tmp_path / "temporary" / "venv"), side_effects=_healthy_side_effects()
    )
    check.main(
        ["check_published_package.py", "--version", "v0.1.0", "--attempts", "1"],
        run=runner,
        sleep=lambda _: None,
    )
    assert made and not made[0].exists()


def test_it_never_runs_a_command_that_could_publish_anything(tmp_path: Path) -> None:
    """It runs after a real upload against the real index, so it must be incapable of changing
    anything: no upload, no tag, no write to the index."""
    runner = FakeRunner(_healthy(tmp_path / "venv"), side_effects=_healthy_side_effects())
    _main(tmp_path, runner)
    for call in runner.calls:
        joined = " ".join(call)
        for forbidden in ("twine", "upload", "git ", "gh ", "publish"):
            assert forbidden not in joined, joined


# --- The wiring ---------------------------------------------------------------------------------

_PUBLISH = REPO / ".github" / "workflows" / "publish.yml"


def _publish_workflow() -> dict:
    return yaml.safe_load(_PUBLISH.read_text(encoding="utf-8"))


def test_the_published_check_runs_in_the_publish_workflow() -> None:
    assert "verify-published" in _publish_workflow()["jobs"]


def test_it_runs_after_the_upload() -> None:
    """There is no earlier point where the question is answerable: only a real upload puts
    something on the index to install."""
    assert "publish-pypi" in _publish_workflow()["jobs"]["verify-published"]["needs"]


def test_it_is_not_wired_as_a_gate() -> None:
    """Putting a post-upload job in `publish-pypi`'s `needs:` would deadlock the workflow, and no
    ordering would let it prevent a bad upload anyway -- the version number is spent by then."""
    assert "verify-published" not in _publish_workflow()["jobs"]["publish-pypi"]["needs"]


def test_it_holds_no_publishing_privilege() -> None:
    job = _publish_workflow()["jobs"]["verify-published"]
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job, "an environment is for a job that publishes; this one reads"


def test_the_checkout_is_not_the_working_directory() -> None:
    """`path:` keeps the source tree out of the directory the install and the CLI run in -- if the
    checkout were the working directory, `import gdmutant` would find the repo's own copy."""
    steps = _publish_workflow()["jobs"]["verify-published"]["steps"]
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["path"] == "release-gate"


def test_it_checks_the_version_that_was_actually_released() -> None:
    """The release tag, not a hand-typed number: guard 1 has already forced the tag to equal the
    packaged version, so there is no second source for the two to drift apart."""
    steps = _publish_workflow()["jobs"]["verify-published"]["steps"]
    step = next(s for s in steps if "check_published_package.py" in s.get("run", ""))
    assert "$TAG_NAME" in step["run"]
    assert step["env"]["TAG_NAME"] == "${{ github.event.release.tag_name }}"
