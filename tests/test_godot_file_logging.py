"""Godot's file log in ``--jobs`` worker copies: off unless ``--godot-file-logging`` asks for it."""

from pathlib import Path

import pytest
from conftest import MarkerRunner

import gdmutant.cli as cli
from gdmutant.adapters.gdscript import ADAPTER, ADAPTER_WITH_GODOT_FILE_LOGGING
from gdmutant.adapters.gdscript.project_settings import isolate_user_dir
from gdmutant.engine.adapter import Adapter

_OFF = (
    "file_logging/enable_file_logging=false",
    "file_logging/enable_file_logging.pc=false",
)
_SOURCE = "func f(a):\n\treturn a > 1\n"


def _project(tmp_path: Path, settings: str = "") -> Path:
    project = tmp_path / "copy"
    project.mkdir(parents=True)
    (project / "project.godot").write_text(
        f'config_version=5\n\n[application]\n\nconfig/name="demo"\n{settings}', encoding="utf-8"
    )
    return project


def _settings(project: Path) -> str:
    return (project / "project.godot").read_text(encoding="utf-8")


def test_a_worker_copy_writes_no_godot_log_by_default(tmp_path: Path) -> None:
    project = _project(tmp_path)
    isolate_user_dir(str(project), "run-w0")
    text = _settings(project)
    assert "[debug]" in text
    assert all(line in text.splitlines() for line in _OFF)


def test_a_project_that_turned_its_log_on_is_overruled(tmp_path: Path) -> None:
    # The `.pc` override wins over the plain key on a desktop, so both have to be set.
    project = _project(
        tmp_path,
        "\n[debug]\n\nfile_logging/enable_file_logging=true\n"
        "file_logging/enable_file_logging.pc=true\n",
    )
    isolate_user_dir(str(project), "run-w0")
    text = _settings(project)
    assert all(line in text.splitlines() for line in _OFF)
    assert "=true\n" not in text.split("[debug]")[1]


def test_an_os_override_ahead_of_pc_is_overruled_too(tmp_path: Path) -> None:
    # Godot uses the first matching override, so `.windows=true` above `.pc` would still log.
    project = _project(
        tmp_path,
        "\n[debug]\n\nfile_logging/enable_file_logging.windows=true\n"
        "file_logging/enable_file_logging.pc=true\n",
    )
    isolate_user_dir(str(project), "run-w0")
    debug = _settings(project).split("[debug]")[1]
    assert "file_logging/enable_file_logging.windows=false" in debug.splitlines()
    assert "=true" not in debug


@pytest.mark.parametrize(
    "line",
    [
        "  file_logging/enable_file_logging.linux.x86_64 = true",
        "file_logging/enable_file_logging.my-tag=true",
        "\tfile_logging/enable_file_logging.windows\t=true",
    ],
)
def test_an_override_however_it_is_written_is_overruled(tmp_path: Path, line: str) -> None:
    project = _project(tmp_path, f"\n[debug]\n\n{line}\n")
    isolate_user_dir(str(project), "run-w0")
    assert "true" not in _settings(project).split("[debug]")[1]


def test_asking_for_the_log_leaves_the_projects_own_setting(tmp_path: Path) -> None:
    project = _project(tmp_path, "\n[debug]\n\nfile_logging/enable_file_logging.pc=true\n")
    isolate_user_dir(str(project), "run-w0", file_logging=True)
    text = _settings(project)
    assert "file_logging/enable_file_logging.pc=true" in text
    assert not any(line in text for line in _OFF)


def test_the_logging_adapter_differs_only_in_how_it_isolates_a_copy(tmp_path: Path) -> None:
    assert ADAPTER_WITH_GODOT_FILE_LOGGING.generate_mutants is ADAPTER.generate_mutants
    assert ADAPTER_WITH_GODOT_FILE_LOGGING.apply_mutant is ADAPTER.apply_mutant
    assert ADAPTER_WITH_GODOT_FILE_LOGGING.release_copy is ADAPTER.release_copy
    project = _project(tmp_path)
    ADAPTER_WITH_GODOT_FILE_LOGGING.isolate_copy(str(project), "run-w0")
    assert "[debug]" not in _settings(project)


class _Ran(Exception):
    """Stops a run at the engine, carrying the adapter the CLI handed it."""

    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter


def _stop_at_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def engine(project_dir: str, target: object, *rest: object, **kwargs: object) -> None:
        adapter = rest[2] if isinstance(target, str) else rest[1]
        assert isinstance(adapter, Adapter)
        raise _Ran(adapter)

    monkeypatch.setattr(cli, "run", engine)
    monkeypatch.setattr(cli, "run_paths", engine)


@pytest.mark.parametrize(("flag", "expected"), [([], ADAPTER), (["--godot-file-logging"], None)])
@pytest.mark.parametrize("files", [1, 2])
def test_both_entry_points_pick_the_adapter_the_flag_asks_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: list[str],
    expected: Adapter | None,
    files: int,
) -> None:
    sources = []
    for index in range(files):
        path = tmp_path / f"f{index}.gd"
        path.write_text(_SOURCE, encoding="utf-8")
        sources.append(str(path))
    _stop_at_the_engine(monkeypatch)
    monkeypatch.setattr(cli, "GdUnit4Runner", lambda **kwargs: MarkerRunner(sources[0], "zzz"))
    with pytest.raises(_Ran) as ran:
        cli.main(
            ["run", *sources, "--project", str(tmp_path), "--runner", "gdunit4", "-j", "2", *flag]
        )
    assert ran.value.adapter is (expected or ADAPTER_WITH_GODOT_FILE_LOGGING)
    # What a worker copy then gets: no log unless the flag asked for one.
    project = _project(tmp_path / "worker")
    ran.value.adapter.isolate_copy(str(project), "run-w0")
    assert ("[debug]" in _settings(project)) is not bool(flag)


def test_changed_since_keeps_the_log_choice() -> None:
    adapter = cli._adapter({"f.gd": {1}}, godot_file_logging=True)
    assert adapter.isolate_copy is ADAPTER_WITH_GODOT_FILE_LOGGING.isolate_copy
    assert cli._adapter({"f.gd": {1}}, godot_file_logging=False).isolate_copy is isolate_user_dir
