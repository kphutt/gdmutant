"""Tests for the GDScript adapter's ``project.godot`` handling, and the ``user://`` isolation
a ``--jobs`` worker's copy needs (`gdmutant.adapters.gdscript.project_settings`).

What makes this worth a module of its own: the isolation is the difference between a parallel run
that reports the same survivors as a serial one and a parallel run that reports fewer. The live
gate (`tests/test_selftest_parallel_determinism.py`) proves it end to end against real Godot; these
are the cheap, every-platform checks of the settings it writes and the directory it removes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gdmutant.adapters.gdscript import project_settings
from gdmutant.adapters.gdscript.project_settings import (
    godot_data_path,
    isolate_user_dir,
    release_user_dir,
    with_setting,
    worker_user_dir_name,
    worker_user_dirs_root,
)

_PLAIN = 'config_version=5\n\n[application]\n\nconfig/name="demo"\n'


def _settings(tmp_path: Path, text: str = _PLAIN) -> Path:
    project = tmp_path / "copy"
    project.mkdir()
    settings = project / "project.godot"
    settings.write_text(text, encoding="utf-8", newline="")
    return settings


def _read(settings: Path) -> str:
    return settings.read_text(encoding="utf-8")


def test_an_isolated_copy_points_user_dir_at_its_own_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    isolate_user_dir(str(settings.parent), "run-w0")
    text = _read(settings)
    assert "config/use_custom_user_dir=true" in text
    assert f'config/custom_user_dir_name="{worker_user_dir_name("run-w0")}"' in text
    # Everything the project already said is still there: this runs on a copy of somebody's real
    # project, and a setting quietly dropped here is a suite that behaves differently per worker.
    assert 'config/name="demo"' in text
    assert text.startswith("config_version=5")


def test_an_isolated_copy_is_written_with_one_line_ending(tmp_path: Path) -> None:
    """A CRLF `project.godot` comes back LF-only, and no CRLF survives anywhere in it.

    `read_text` reads with universal newlines, so the CRLF is already gone before
    `isolate_user_dir` edits anything, and `newline=""` on the write puts back exactly what it
    holds. That is the behaviour, not an accident, and the comment beside that write used to claim
    the opposite (that the file's own endings survived), so this pins which one is true. What
    matters either way is that a Windows run cannot produce a copy whose every line changed:
    without `newline=""`, `write_text` would turn each line feed into CRLF.
    """
    crlf = _PLAIN.replace("\n", "\r\n")
    settings = _settings(tmp_path, crlf)
    assert settings.read_bytes().count(b"\r\n") == _PLAIN.count("\n")  # the fixture really is CRLF

    isolate_user_dir(str(settings.parent), "run-w0")

    assert b"\r" not in settings.read_bytes()
    text = _read(settings)
    assert "config/use_custom_user_dir=true" in text
    assert 'config/name="demo"' in text  # reading CRLF did not mangle what was already there


def test_two_tokens_never_land_in_the_same_user_dir() -> None:
    assert worker_user_dir_name("run-w0") != worker_user_dir_name("run-w1")
    # Nested under one parent, so a run that is killed before it can clean up leaves its
    # directories somewhere obvious instead of beside the user's own projects.
    assert (
        Path(worker_user_dir_name("run-w0")).parent == Path(worker_user_dir_name("run-w1")).parent
    )


def test_the_user_dir_name_nests_the_token_under_the_directory_cleanup_watches() -> None:
    # The value written into project.godot and the path `release_user_dir` deletes have to agree,
    # or a run isolates one directory and cleans up another. One shape, two readers: the token
    # directly inside the root, and nothing else in between.
    assert Path(worker_user_dir_name("run-w0")).parts == (worker_user_dirs_root().name, "run-w0")


def test_the_leftovers_of_an_interrupted_run_are_named_after_the_tool() -> None:
    # A run killed before it can release leaves this directory behind, in the same place the user's
    # own Godot projects keep their data. Whoever finds it has to be able to tell what put it there
    # and that deleting it is safe, so the name is part of the contract, not an implementation
    # detail.
    assert worker_user_dirs_root().name == "gdmutant"


def test_a_project_that_already_set_these_keys_is_overruled(tmp_path: Path) -> None:
    # The dangerous shape: a project that explicitly turns the custom user directory OFF, or names
    # one of its own. Adding the keys only when they are absent would leave every worker of such a
    # project pointed at one shared directory -- isolated in appearance only.
    settings = _settings(
        tmp_path,
        'config_version=5\n\n[application]\n\nconfig/name="demo"\n'
        "config/use_custom_user_dir=false\n"
        'config/custom_user_dir_name="theirs"\n',
    )
    isolate_user_dir(str(settings.parent), "run-w0")
    text = _read(settings)
    assert "config/use_custom_user_dir=false" not in text
    assert '"theirs"' not in text
    assert f'config/custom_user_dir_name="{worker_user_dir_name("run-w0")}"' in text


@pytest.mark.parametrize(
    "unnamed",
    [
        "config_version=5\n",
        "config_version=5\n\n[application]\n\nconfig/features=PackedStringArray()\n",
        'config_version=5\n\n[application]\n\nconfig/name=""\n',
        "config_version=5\n\n[application]\n\nconfig/name_localized={}\n",
    ],
    ids=["no-section", "no-name", "empty-name", "only-a-localized-name"],
)
def test_a_project_with_no_usable_name_is_given_one(tmp_path: Path, unnamed: str) -> None:
    # Godot reads the project's name FIRST and ignores both custom-user-dir settings when it is
    # empty, falling back to one "[unnamed project]" directory that every nameless project on the
    # machine shares (verified against Godot 4.7). So a nameless project cannot be isolated by the
    # custom-user-dir keys alone, and the fix for the defect would be a no-op on exactly the
    # projects that look least likely to notice.
    settings = _settings(tmp_path, unnamed)
    isolate_user_dir(str(settings.parent), "run-w0")
    assert 'config/name="run-w0"' in _read(settings)


def test_a_project_that_already_has_a_name_keeps_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    isolate_user_dir(str(settings.parent), "run-w0")
    text = _read(settings)
    assert 'config/name="demo"' in text
    assert 'config/name="run-w0"' not in text


def test_a_directory_that_is_not_a_godot_project_is_left_alone(tmp_path: Path) -> None:
    # Reachable with the framework-neutral command runner (ADR-0005) pointed at something that is
    # not a Godot project. There is no project.godot, so there is no user:// to share and nothing
    # here could run a Godot suite anyway: a no-op, not a silent failure.
    copy = tmp_path / "copy"
    copy.mkdir()
    isolate_user_dir(str(copy), "run-w0")
    assert list(copy.iterdir()) == []


def test_releasing_a_copy_removes_the_user_dir_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_settings, "godot_data_path", lambda: tmp_path)
    mine = worker_user_dirs_root() / "run-w0"
    theirs = worker_user_dirs_root() / "run-w1"
    for directory in (mine, theirs):
        (directory / "logs").mkdir(parents=True)
        (directory / "logs" / "godot.log").write_text("x", encoding="utf-8")
    release_user_dir("run-w0")
    assert not mine.exists()
    assert theirs.exists(), "releasing one worker's directory must not touch another's"


def test_releasing_a_copy_that_left_nothing_behind_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cleanup runs when the mutants are already scored, so nothing it finds (or fails to find) may
    # turn a finished run into a failed one.
    monkeypatch.setattr(project_settings, "godot_data_path", lambda: tmp_path / "never-created")
    release_user_dir("run-w0")


def test_the_data_path_on_windows_is_the_roaming_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stand-in directory rather than a written-out user profile: this repository refuses an
    # absolute home path in a tracked file, and the mapping under test only joins what it is given.
    roaming = tmp_path / "roaming"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(roaming))
    assert godot_data_path() == roaming


def test_the_data_path_on_windows_without_a_profile_falls_back_like_godot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Godot's own fallback when APPDATA is unset is the current directory, and matching it is the
    # point: this path exists to find the directory Godot created, not to pick a better one.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    assert godot_data_path() == Path(".")


def test_the_data_path_on_macos_is_application_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "somebodys-home"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert godot_data_path() == home / "Library" / "Application Support"


def test_the_data_path_on_linux_follows_an_absolute_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A POSIX-absolute literal, not tmp_path: Godot takes XDG_DATA_HOME only when it begins with
    # "/", and on a Windows machine tmp_path begins with a drive letter, so the case under test
    # would never be the case running.
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/srv/elsewhere/share")
    assert godot_data_path() == Path("/srv/elsewhere/share")


@pytest.mark.parametrize("xdg", ["", "relative/share"], ids=["unset", "relative"])
def test_the_data_path_on_linux_ignores_an_xdg_data_home_godot_would_ignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str
) -> None:
    # Godot uses XDG_DATA_HOME only when it is an absolute path and warns otherwise, so a relative
    # one has to fall back here too or the cleanup looks for the directory in the wrong place.
    home = tmp_path / "somebodys-home"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", xdg)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert godot_data_path() == home / ".local" / "share"


def test_a_setting_is_replaced_where_it_stands(tmp_path: Path) -> None:
    # `with_setting` is shared by the coverage marker run and the user-dir isolation, so its
    # behaviour is pinned here rather than through either caller.
    before = '[application]\nconfig/name="demo"\nconfig/features=PackedStringArray()\n'
    after = with_setting(before, "application", "config/name", '"other"')
    assert after == '[application]\nconfig/name="other"\nconfig/features=PackedStringArray()\n'


def test_a_missing_section_is_added_at_the_end() -> None:
    assert with_setting("config_version=5\n", "application", "config/name", '"demo"') == (
        'config_version=5\n\n[application]\n\nconfig/name="demo"\n'
    )
