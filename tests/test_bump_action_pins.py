"""Tests for `scripts/bump_action_pins.py`, release step 10 as a command.

The script runs once a release, right after the tag, so a mistake in it is found on release day,
the worst time. These tests pin every refusal and the one write, with a fake `tag_commit` so no
test reaches the network. The real command was also replayed against the v0.1.3 release commit
and produced files byte-identical to the hand-made pin bump that shipped.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "bump_action_pins.py"
_spec = importlib.util.spec_from_file_location("bump_action_pins", _SCRIPT)
assert _spec and _spec.loader
bump_action_pins = importlib.util.module_from_spec(_spec)
sys.modules["bump_action_pins"] = bump_action_pins
_spec.loader.exec_module(bump_action_pins)

OLD = "a" * 40
NEW = "b" * 40


def _pin(sha: str, version: str) -> str:
    return f"      - uses: kphutt/gdmutant@{sha} # v{version}\n"


# --- bump ---------------------------------------------------------------------------------------


def test_bump_rewrites_every_pin_commented_with_the_version() -> None:
    text = _pin(OLD, "0.1.3") + "prose\n" + _pin(OLD, "0.1.3")
    new, changed, current, problems = bump_action_pins.bump(text, NEW, "0.1.3")
    assert new == _pin(NEW, "0.1.3") + "prose\n" + _pin(NEW, "0.1.3")
    assert (changed, current, problems) == (2, 0, [])


def test_bump_counts_a_pin_already_on_the_tag_as_current() -> None:
    new, changed, current, problems = bump_action_pins.bump(_pin(NEW, "0.1.3"), NEW, "0.1.3")
    assert new == _pin(NEW, "0.1.3")
    assert (changed, current, problems) == (0, 1, [])


def test_bump_never_touches_another_actions_pin() -> None:
    other = "      - uses: actions/checkout@" + OLD + " # v0.1.3\n"
    new, changed, _, _ = bump_action_pins.bump(other + _pin(OLD, "0.1.3"), NEW, "0.1.3")
    assert new == other + _pin(NEW, "0.1.3")
    assert changed == 1


def test_bump_reports_a_comment_step_one_missed() -> None:
    new, changed, _, problems = bump_action_pins.bump(_pin(OLD, "0.1.2"), NEW, "0.1.3")
    assert new == _pin(OLD, "0.1.2")  # left alone, not guessed at
    assert changed == 0
    assert len(problems) == 1 and "commented v0.1.2" in problems[0]


def test_bump_reports_a_pin_with_no_comment() -> None:
    bare = f"      - uses: kphutt/gdmutant@{OLD}\n"
    _, _, _, problems = bump_action_pins.bump(bare + _pin(OLD, "0.1.3"), NEW, "0.1.3")
    assert len(problems) == 1 and "no `# vX.Y.Z` comment" in problems[0]


# --- main ---------------------------------------------------------------------------------------


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.3"\n', encoding="utf-8")
    for name in bump_action_pins.PIN_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files.get(name, _pin(OLD, "0.1.3")).encode("utf-8"))
    return tmp_path


def _contents(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in bump_action_pins.PIN_FILES}


def test_main_points_every_listed_file_at_the_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, {})
    monkeypatch.setattr(bump_action_pins, "tag_commit", lambda version, root: NEW)
    assert bump_action_pins.main([], root=root) == 0
    for text in _contents(root).values():
        assert text.decode("utf-8") == _pin(NEW, "0.1.3")


def test_main_refuses_before_the_tag_exists_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {})
    before = _contents(root)
    monkeypatch.setattr(bump_action_pins, "tag_commit", lambda version, root: None)
    assert bump_action_pins.main([], root=root) == 1
    assert _contents(root) == before
    assert "not on origin yet" in capsys.readouterr().err


def test_one_bad_file_stops_every_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A half-bumped set of docs is worse than none: the pin test then fails on some files and
    # passes on others, and the one left behind is easy to miss. So a single problem anywhere
    # means nothing is written, including to the files that were fine.
    root = _repo(tmp_path, {"action.yml": _pin(OLD, "0.1.2")})
    before = _contents(root)
    monkeypatch.setattr(bump_action_pins, "tag_commit", lambda version, root: NEW)
    assert bump_action_pins.main([], root=root) == 1
    assert _contents(root) == before
    assert "action.yml: a pin is commented v0.1.2" in capsys.readouterr().err


def test_a_listed_file_with_no_pin_is_a_failure_not_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "no pins here any more\n"})
    monkeypatch.setattr(bump_action_pins, "tag_commit", lambda version, root: NEW)
    assert bump_action_pins.main([], root=root) == 1
    assert "README.md: no `kphutt/gdmutant@<sha> # vX.Y.Z` pin found" in capsys.readouterr().err


def test_main_keeps_crlf_line_endings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    crlf = _pin(OLD, "0.1.3").replace("\n", "\r\n")
    root = _repo(tmp_path, dict.fromkeys(bump_action_pins.PIN_FILES, crlf))
    monkeypatch.setattr(bump_action_pins, "tag_commit", lambda version, root: NEW)
    assert bump_action_pins.main([], root=root) == 0
    expected = _pin(NEW, "0.1.3").replace("\n", "\r\n").encode("utf-8")
    assert all(text == expected for text in _contents(root).values())


def test_an_explicit_version_overrides_pyproject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path, dict.fromkeys(bump_action_pins.PIN_FILES, _pin(OLD, "0.2.0")))
    asked: list[str] = []

    def tag(version: str, root: Path) -> str:
        asked.append(version)
        return NEW

    monkeypatch.setattr(bump_action_pins, "tag_commit", tag)
    assert bump_action_pins.main(["--version", "0.2.0"], root=root) == 0
    assert asked == ["0.2.0"]
