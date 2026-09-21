"""The GDScript marker run's Godot-free parts: marking a copy, and each runner's `run_markers`.

Godot itself is faked here (subprocess mocked). The same paths against real Godot are in
`tests/test_selftest_live.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import gdmutant.adapters.gdscript.marker_run as marker_mod
import gdmutant.adapters.gdscript.runner as runner_mod
import gdmutant.engine.runner as engine_runner
from gdmutant.adapters.gdscript import generate_mutants
from gdmutant.adapters.gdscript.marker_run import (
    HITS_FILE,
    RECORDER_DIR,
    WRITER_AUTOLOAD,
    GDScriptMarker,
    _with_writer_autoload,
)
from gdmutant.adapters.gdscript.markers import MARKER_AUTOLOAD
from gdmutant.adapters.gdscript.runner import GdUnit4Runner, GutRunner
from gdmutant.engine.runner import (
    CommandRunner,
    MarkerRunnable,
    SuiteResult,
    script_error_excerpt,
)

_SOURCE = """extends RefCounted
const LIMIT = 3


func a(x: int) -> bool:
\treturn x > 1
"""
_SETTINGS = 'config_version=5\n\n[application]\n\nconfig/name="p"\n'


def _copy(tmp_path: Path, settings: str | None = _SETTINGS) -> Path:
    copy = tmp_path / "copy"
    copy.mkdir(parents=True)
    if settings is not None:
        (copy / "project.godot").write_text(settings, encoding="utf-8")
    (copy / "a.gd").write_text(_SOURCE, encoding="utf-8")
    return copy


def _registers(calls: list[list[str]], *, register: bool = True, output: str = "") -> Any:
    """A fake `subprocess.run` for the import scan: records the command and, like Godot, writes
    the class list, naming the recorder class only if `register`."""

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert (kwargs["capture_output"], kwargs["text"], kwargs["check"]) == (True, True, False)
        cache = Path(kwargs["cwd"]) / ".godot" / "global_script_class_cache.cfg"
        cache.parent.mkdir(exist_ok=True)
        name = MARKER_AUTOLOAD if register else "Other"
        cache.write_text(f'list=[{{\n"class": &"{name}",\n}}]\n', encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, output, "")

    return fake_run


def _files(copy: Path, rel: str = "a.gd") -> dict[str, tuple[str, list[Any]]]:
    return {rel: (_SOURCE, generate_mutants(str(copy / rel), _SOURCE))}


def test_mark_writes_the_marked_source_the_recorder_and_the_autoload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers(calls))
    marked = GDScriptMarker(godot="my-godot").mark(str(copy), _files(copy))

    text = (copy / "a.gd").read_text(encoding="utf-8")
    assert "\treturn x > 1" not in text
    assert f"\t{MARKER_AUTOLOAD}.hit(0); return x > 1" in text
    assert "\r\n" not in (copy / "a.gd").read_bytes().decode("utf-8")
    recorder = (copy / RECORDER_DIR / "marks.gd").read_text(encoding="utf-8")
    assert recorder.startswith(f"class_name {MARKER_AUTOLOAD}\n")
    assert "static func hit(spot: int) -> void:" in recorder
    writer = (copy / RECORDER_DIR / "writer.gd").read_text(encoding="utf-8")
    assert f'FileAccess.open("res://{HITS_FILE}"' in writer
    assert "NOTIFICATION_PREDELETE" in writer
    assert f'{WRITER_AUTOLOAD}="*res://{RECORDER_DIR}/writer.gd"' in (
        copy / "project.godot"
    ).read_text(encoding="utf-8")
    assert calls == [["my-godot", "--headless", "--path", str(copy.resolve()), "--import"]]
    assert marked.hits_path == str(copy / HITS_FILE)
    # The const's mutants have no marker (run everything). The return's share spot 0.
    placements = marked.placements["a.gd"]
    mutants = _files(copy)["a.gd"][1]
    for mutant, placement in zip(mutants, placements, strict=True):
        assert placement == (None if mutant.span.line == 2 else 0)
    assert None in placements


def test_spot_ids_are_unique_across_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = _copy(tmp_path)
    (copy / "b.gd").write_text(_SOURCE, encoding="utf-8")
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    (copy / "c.gd").write_text(_SOURCE, encoding="utf-8")
    files = {**_files(copy), **_files(copy, "b.gd"), **_files(copy, "c.gd")}
    marked = GDScriptMarker().mark(str(copy), files)
    assert {p for p in marked.placements["a.gd"] if p is not None} == {0}
    assert {p for p in marked.placements["b.gd"] if p is not None} == {1}
    assert {p for p in marked.placements["c.gd"] if p is not None} == {2}
    assert f"{MARKER_AUTOLOAD}.hit(1); return" in (copy / "b.gd").read_text(encoding="utf-8")


def test_the_writer_is_registered_first_in_an_existing_autoload_section() -> None:
    settings = '[autoload]\n\nGame="*res://game.gd"\n'
    assert _with_writer_autoload(settings) == (
        f'[autoload]\n{WRITER_AUTOLOAD}="*res://{RECORDER_DIR}/writer.gd"\n\nGame="*res://game.gd"\n'
    )


def test_a_project_with_no_autoloads_gains_a_section() -> None:
    assert _with_writer_autoload("config_version=5\n\n") == (
        f'config_version=5\n\n[autoload]\n\n{WRITER_AUTOLOAD}="*res://{RECORDER_DIR}/writer.gd"\n'
    )


def test_a_copy_with_no_project_file_is_refused(tmp_path: Path) -> None:
    copy = _copy(tmp_path, settings=None)
    with pytest.raises(RuntimeError, match="there is no project.godot"):
        GDScriptMarker().mark(str(copy), _files(copy))


def test_an_existing_recorder_directory_is_refused(tmp_path: Path) -> None:
    copy = _copy(tmp_path)
    (copy / RECORDER_DIR).mkdir()
    with pytest.raises(RuntimeError, match=f"already has a {RECORDER_DIR}/ directory"):
        GDScriptMarker().mark(str(copy), _files(copy))


@pytest.mark.parametrize("name", [MARKER_AUTOLOAD, WRITER_AUTOLOAD])
def test_an_autoload_already_using_a_recorder_name_is_refused(tmp_path: Path, name: str) -> None:
    copy = _copy(tmp_path, _SETTINGS + f'\n[autoload]\n\n{name}="*res://x.gd"\n')
    with pytest.raises(RuntimeError, match=f"autoload named {name}"):
        GDScriptMarker().mark(str(copy), _files(copy))


def test_a_script_already_declaring_the_recorder_class_is_refused(tmp_path: Path) -> None:
    copy = _copy(tmp_path)
    (copy / "sub").mkdir()
    (copy / "sub" / "x.gd").write_text(f"class_name {MARKER_AUTOLOAD}\n", encoding="utf-8")
    with pytest.raises(
        RuntimeError, match=f"sub/x.gd already declares class_name {MARKER_AUTOLOAD}"
    ):
        GDScriptMarker().mark(str(copy), _files(copy))


def test_a_similar_name_is_not_mistaken_for_the_recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path, _SETTINGS + f'\n[autoload]\n\n{MARKER_AUTOLOAD}Extra="*res://x.gd"\n')
    (copy / "x.gd").write_text(f"class_name {MARKER_AUTOLOAD}Extra\n", encoding="utf-8")
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    GDScriptMarker().mark(str(copy), _files(copy))  # no refusal


def test_a_missing_godot_names_the_flag_that_fixes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path)

    def missing(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError(2, "not found")

    monkeypatch.setattr(marker_mod.subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="Pass the Godot executable with --godot") as caught:
        GDScriptMarker(godot="nope").mark(str(copy), _files(copy))
    assert "'nope'" in str(caught.value)
    assert "--runner command included" in str(caught.value)
    assert caught.value.__cause__ is None  # not a FileNotFoundError, which names --command instead


def test_a_hung_import_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = _copy(tmp_path)

    def hangs(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["timeout"] == 300.0
        raise subprocess.TimeoutExpired(command, 300.0)

    monkeypatch.setattr(marker_mod.subprocess, "run", hangs)
    with pytest.raises(RuntimeError, match="took longer than 300s"):
        GDScriptMarker().mark(str(copy), _files(copy))


def test_an_import_that_did_not_register_the_recorder_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path)
    monkeypatch.setattr(
        marker_mod.subprocess, "run", _registers([], register=False, output="ERROR: boom")
    )
    with pytest.raises(RuntimeError, match="did not register the _GdmMarks") as caught:
        GDScriptMarker().mark(str(copy), _files(copy))
    assert caught.value.args[0].endswith("Godot said:\nERROR: boom")


def test_an_import_that_wrote_no_class_list_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path)
    monkeypatch.setattr(
        marker_mod.subprocess,
        "run",
        lambda command, **_k: subprocess.CompletedProcess(command, 0, "", ""),
    )
    with pytest.raises(RuntimeError, match="did not register"):
        GDScriptMarker().mark(str(copy), _files(copy))


# --- each runner's marker run ---------------------------------------------------------------------


def test_script_error_excerpt_is_the_first_error_and_what_follows() -> None:
    output = "ok\nSCRIPT ERROR: one\n  at: a.gd:3\n  x\n  y\n  z\nSCRIPT ERROR: two\n"
    assert script_error_excerpt(output) == "SCRIPT ERROR: one\n  at: a.gd:3\n  x\n  y"
    assert script_error_excerpt("all fine") == ""


def test_every_runner_can_do_the_marker_run() -> None:
    for runner in (CommandRunner(["x"]), GdUnit4Runner(), GutRunner()):
        assert isinstance(runner, MarkerRunnable)


def _completed(stdout: str, code: int = 0) -> Any:
    return lambda *a, **k: subprocess.CompletedProcess([], code, stdout, "")


def test_the_command_runner_marks_a_script_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_runner.subprocess, "run", _completed("SCRIPT ERROR: x\n at y"))
    result = CommandRunner(["h"]).run_markers(".")
    assert result.runtime_error == "SCRIPT ERROR: x\n at y"
    assert result.errors == 1


def test_the_command_runner_marker_run_is_its_normal_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_runner.subprocess, "run", _completed("fine"))
    assert CommandRunner(["h"]).run_markers(".", timeout=5) == SuiteResult(1, 0, 0)


def test_gdunit4_keeps_going_after_a_failure_only_in_the_marker_run() -> None:
    runner = GdUnit4Runner()
    assert runner.command("p", markers=True) == [*runner.command("p"), "-c"]
    assert "-c" not in runner.command("p")


def test_gut_needs_no_extra_switch_for_the_marker_run() -> None:
    runner = GutRunner()
    assert runner.command("p", markers=True) == runner.command("p")


_XML = '<testsuites><testsuite tests="2" failures="0" errors="0"/></testsuites>'


def _writes_report(project: Path, report: str, stdout: str) -> Any:
    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "--import" not in command:
            path = project / report
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_XML, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return fake_run


@pytest.mark.parametrize(
    ("runner", "report"),
    [
        (GdUnit4Runner(), runner_mod.DEFAULT_REPORT_PATH),
        (GutRunner(), runner_mod.DEFAULT_GUT_REPORT_PATH),
    ],
)
def test_a_junit_marker_run_reports_a_script_error_outside_every_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: Any, report: str
) -> None:
    out = "Run tests\nSCRIPT ERROR: Invalid access\n   at: res://a.gd:4\nAll passed\n"
    monkeypatch.setattr(runner_mod.subprocess, "run", _writes_report(tmp_path, report, out))
    marked = runner.run_markers(str(tmp_path))
    assert (marked.tests, marked.failures, marked.errors) == (2, 0, 0)
    assert marked.runtime_error == "SCRIPT ERROR: Invalid access\n   at: res://a.gd:4\nAll passed"
    # A mutant run of the same output is unchanged: the scan is the marker run's alone.
    assert runner.run(str(tmp_path)).runtime_error == ""


def test_a_junit_marker_run_passes_its_own_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    fake = _writes_report(tmp_path, runner_mod.DEFAULT_REPORT_PATH, "")

    def recording(command: list[str], **kwargs: Any) -> Any:
        seen.append(command)
        return fake(command, **kwargs)

    monkeypatch.setattr(runner_mod.subprocess, "run", recording)
    runner = GdUnit4Runner()
    runner.run_markers(str(tmp_path), timeout=7)
    assert seen[-1][-1] == "-c"
    runner.run(str(tmp_path))
    assert seen[-1][-1] == "--ignoreHeadlessMode"


def test_a_script_that_is_not_utf8_does_not_stop_the_name_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path)
    (copy / "latin.gd").write_bytes(b"# caf\xe9\nextends Node\n")
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    GDScriptMarker().mark(str(copy), _files(copy))  # no UnicodeDecodeError


def test_the_default_godot_is_the_one_on_path() -> None:
    assert GDScriptMarker().godot == "godot"


def test_the_command_runner_marker_run_gets_the_runners_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[object] = []

    def fake(*a: Any, **k: Any) -> Any:
        seen.append(k["timeout"])
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(engine_runner.subprocess, "run", fake)
    CommandRunner(["h"], timeout=77.0).run_markers(".")
    assert seen == [77.0]


@pytest.mark.parametrize(
    ("runner", "report"),
    [
        (GdUnit4Runner(timeout=66.0), runner_mod.DEFAULT_REPORT_PATH),
        (GutRunner(timeout=66.0), runner_mod.DEFAULT_GUT_REPORT_PATH),
    ],
)
def test_a_junit_marker_run_gets_the_runners_own_budget_and_scans_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: Any, report: str
) -> None:
    seen: list[object] = []
    fake = _writes_report(tmp_path, report, "")

    def recording(command: list[str], **kwargs: Any) -> Any:
        if "--import" not in command:
            seen.append(kwargs["timeout"])
        fake(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, "fine", "SCRIPT ERROR: late\n  at: x.gd:2")

    monkeypatch.setattr(runner_mod.subprocess, "run", recording)
    assert runner.run_markers(str(tmp_path)).runtime_error == "SCRIPT ERROR: late\n  at: x.gd:2"
    assert seen == [66.0]


# Exact words, and the edges of the name checks, pinned after a mutation run.


def test_the_taken_name_messages_in_full(tmp_path: Path) -> None:
    copy = _copy(tmp_path)
    (copy / RECORDER_DIR).mkdir()
    with pytest.raises(RuntimeError) as directory:
        GDScriptMarker().mark(str(copy), _files(copy))
    assert str(directory.value) == (
        f"the project already has a {RECORDER_DIR}/ directory, which coverage analysis needs for "
        "its recorder. Rename it to use coverage analysis"
    )
    other = _copy(tmp_path / "o", _SETTINGS + f"\n[autoload]\n\n{WRITER_AUTOLOAD}=1\n")
    with pytest.raises(RuntimeError) as autoload:
        GDScriptMarker().mark(str(other), _files(other))
    assert str(autoload.value) == (
        f"project.godot already registers an autoload named {WRITER_AUTOLOAD}, a name coverage "
        "analysis needs for its recorder. Rename it to use coverage analysis"
    )
    third = _copy(tmp_path / "t")
    # A walk meets z.gd before it descends into a/, so only sorting names a/b.gd first.
    (third / "a").mkdir()
    (third / "a" / "b.gd").write_text(f"class_name {MARKER_AUTOLOAD}\n", encoding="utf-8")
    (third / "z.gd").write_text(f"class_name {MARKER_AUTOLOAD}\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as script:
        GDScriptMarker().mark(str(third), _files(third))
    # Sorted, so the same project always names the same file.
    assert str(script.value) == (
        f"a/b.gd already declares class_name {MARKER_AUTOLOAD}, the name coverage analysis needs "
        "for its recorder. Rename it to use coverage analysis"
    )


def test_an_indented_autoload_with_spaces_around_the_equals_is_still_caught(
    tmp_path: Path,
) -> None:
    copy = _copy(tmp_path, _SETTINGS + f"\n[autoload]\n\n  {MARKER_AUTOLOAD} = 1\n")
    with pytest.raises(RuntimeError, match=f"autoload named {MARKER_AUTOLOAD}"):
        GDScriptMarker().mark(str(copy), _files(copy))


def test_a_name_only_mentioned_in_a_value_is_not_an_autoload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = _copy(tmp_path, _SETTINGS + f'\nconfig/description="uses {MARKER_AUTOLOAD}=x"\n')
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    GDScriptMarker().mark(str(copy), _files(copy))


@pytest.mark.parametrize(
    "script",
    [
        f"extends Node\nclass_name  {MARKER_AUTOLOAD}\n",  # extra spacing
        f"\tclass_name {MARKER_AUTOLOAD}\n",  # indented
        f"class_name {MARKER_AUTOLOAD} extends Object\n",  # one-line form, GDScript 4
        f"@tool class_name {MARKER_AUTOLOAD}\n",  # an annotation on the same line
        f'@icon("res://i.svg") @tool class_name {MARKER_AUTOLOAD} extends Node\n',
    ],
)
def test_a_real_declaration_of_the_recorder_class_is_refused(tmp_path: Path, script: str) -> None:
    copy = _copy(tmp_path)
    (copy / "x.gd").write_text(script, encoding="utf-8")
    with pytest.raises(RuntimeError, match="already declares class_name"):
        GDScriptMarker().mark(str(copy), _files(copy))


@pytest.mark.parametrize(
    "script",
    [
        f"extends Node\n# class_name {MARKER_AUTOLOAD}\n",  # a comment on its own line
        f"extends Node\nvar x := 1  # class_name {MARKER_AUTOLOAD}\n",  # a trailing comment
        f'extends Node\nvar s := "class_name {MARKER_AUTOLOAD}"\n',  # a string literal
        f"extends Node\n## Do not write class_name {MARKER_AUTOLOAD} here.\n",  # a doc comment
    ],
)
def test_the_recorder_class_named_in_a_comment_or_a_string_is_not_a_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    copy = _copy(tmp_path)
    (copy / "x.gd").write_text(script, encoding="utf-8")
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    GDScriptMarker().mark(str(copy), _files(copy))  # no refusal


@pytest.mark.parametrize("name", [MARKER_AUTOLOAD, WRITER_AUTOLOAD])
def test_a_commented_out_autoload_entry_is_not_a_taken_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    copy = _copy(tmp_path, _SETTINGS + f'\n[autoload]\n\n;{name}="*res://x.gd"\n')
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    GDScriptMarker().mark(str(copy), _files(copy))  # no refusal


def test_trailing_blank_lines_are_trimmed_but_nothing_else() -> None:
    assert _with_writer_autoload("k=VX\n\n\n").startswith("k=VX\n\n[autoload]")
    assert _with_writer_autoload("k=V  \n").startswith("k=V  \n\n[autoload]")


def test_a_project_file_with_non_ascii_text_is_read_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "Á" is the bytes C3 81 in UTF-8, and 0x81 is undefined in Windows' cp1252, so reading this
    # project.godot with the platform's default encoding fails there. Godot writes UTF-8.
    copy = _copy(tmp_path, 'config_version=5\n\n[application]\n\nconfig/name="Árbol"\n')
    monkeypatch.setattr(marker_mod.subprocess, "run", _registers([]))
    GDScriptMarker().mark(str(copy), _files(copy))
