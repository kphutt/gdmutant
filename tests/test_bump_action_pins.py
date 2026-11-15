"""Tests for `scripts/bump_action_pins.py`, the pin edit in release step 1 as a command.

The script runs once a release, in the release commit, so a mistake in it is found on release day,
the worst time. These tests pin every refusal and the one write. The script reads no network, so
none of them needs one.
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


def _pin(ref: str) -> str:
    """A pin line, built from `ref` so this file never holds a pin the repo-wide scan would find."""
    return f"      - uses: kphutt/gdmutant@{ref}\n"


def _tag(version: str) -> str:
    return _pin(f"v{version}")


# --- bump ---------------------------------------------------------------------------------------


def test_bump_points_every_pin_at_the_new_tag() -> None:
    text = _tag("0.1.2") + "prose\n" + _tag("0.1.2")
    new, changed, current, problems = bump_action_pins.bump(text, "0.1.3")
    assert new == _tag("0.1.3") + "prose\n" + _tag("0.1.3")
    assert (changed, current, problems) == (2, 0, [])


def test_bump_counts_a_pin_already_on_the_tag_as_current() -> None:
    new, changed, current, problems = bump_action_pins.bump(_tag("0.1.3"), "0.1.3")
    assert new == _tag("0.1.3")
    assert (changed, current, problems) == (0, 1, [])


def test_bump_never_touches_another_actions_pin() -> None:
    other = "      - uses: actions/checkout@v0.1.2\n"
    new, changed, _, _ = bump_action_pins.bump(other + _tag("0.1.2"), "0.1.3")
    assert new == other + _tag("0.1.3")
    assert changed == 1


def test_bump_leaves_a_templated_ref_in_code_alone() -> None:
    code = 'return f"git+https://github.com/kphutt/gdmutant@{action_ref}"\n'
    new, changed, current, problems = bump_action_pins.bump(code + _tag("0.1.2"), "0.1.3")
    assert new == code + _tag("0.1.3")
    assert (changed, current, problems) == (1, 0, [])


def test_bump_refuses_to_move_a_pin_backwards() -> None:
    new, changed, _, problems = bump_action_pins.bump(_tag("0.1.4"), "0.1.3")
    assert new == _tag("0.1.4")  # left alone, not guessed at
    assert changed == 0
    assert len(problems) == 1 and "`@v0.1.4`, later than v0.1.3" in problems[0]


def test_bump_compares_versions_as_numbers_not_strings() -> None:
    # As strings "0.1.10" < "0.1.9", which would refuse a correct bump to 0.1.10.
    new, changed, _, problems = bump_action_pins.bump(_tag("0.1.9"), "0.1.10")
    assert (new, changed, problems) == (_tag("0.1.10"), 1, [])


@pytest.mark.parametrize("ref", ["a" * 40, "main", "v1", "v0.1", "v0.1.3-rc1"])
def test_bump_refuses_a_pin_that_is_not_an_exact_release_tag(ref: str) -> None:
    # A commit ID, a branch or a floating tag is never rewritten into a tag: the script cannot tell
    # whether that pin was deliberate, so it says so and leaves the edit to a person.
    new, changed, current, problems = bump_action_pins.bump(_pin(ref), "0.1.3")
    assert (new, changed, current) == (_pin(ref), 0, 0)
    assert len(problems) == 1 and "not an exact release tag" in problems[0]


def test_one_bad_pin_is_reported_beside_good_ones() -> None:
    _, changed, _, problems = bump_action_pins.bump(_tag("0.1.2") + _pin("main"), "0.1.3")
    assert changed == 1 and len(problems) == 1


# --- main ---------------------------------------------------------------------------------------


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.3"\n', encoding="utf-8")
    for name in bump_action_pins.PIN_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files.get(name, _tag("0.1.2")).encode("utf-8"))
    return tmp_path


def _contents(root: Path) -> dict[str, bytes]:
    return {name: (root / name).read_bytes() for name in bump_action_pins.PIN_FILES}


def test_main_points_every_listed_file_at_the_packaged_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {})
    assert bump_action_pins.main([], root=root) == 0
    for text in _contents(root).values():
        assert text.decode("utf-8") == _tag("0.1.3")
    assert "Updated 3 pin(s) in 3 file(s), 0 already current" in capsys.readouterr().out


def test_a_second_run_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repo(tmp_path, {})
    assert bump_action_pins.main([], root=root) == 0
    after = _contents(root)
    capsys.readouterr()
    assert bump_action_pins.main([], root=root) == 0
    assert _contents(root) == after
    assert "Updated 0 pin(s) in 0 file(s), 3 already current" in capsys.readouterr().out


def test_one_bad_file_stops_every_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A half-bumped set of docs is worse than none: the pin test then fails on some files and
    # passes on others, and the one left behind is easy to miss. So a single problem anywhere
    # means nothing is written, including to the files that were fine.
    root = _repo(tmp_path, {"action.yml": _tag("0.1.4")})
    before = _contents(root)
    assert bump_action_pins.main([], root=root) == 1
    assert _contents(root) == before
    assert "action.yml: a pin reads `@v0.1.4`" in capsys.readouterr().err


def test_a_commit_id_pin_stops_every_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": _pin("a" * 40)})
    before = _contents(root)
    assert bump_action_pins.main([], root=root) == 1
    assert _contents(root) == before
    assert "README.md: a pin reads" in capsys.readouterr().err


def test_a_listed_file_with_no_pin_is_a_failure_not_a_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {"README.md": "no pins here any more\n"})
    before = _contents(root)
    assert bump_action_pins.main([], root=root) == 1
    assert _contents(root) == before
    assert "README.md: no `kphutt/gdmutant@vX.Y.Z` pin found" in capsys.readouterr().err


def test_main_keeps_crlf_line_endings(tmp_path: Path) -> None:
    crlf = _tag("0.1.2").replace("\n", "\r\n")
    root = _repo(tmp_path, dict.fromkeys(bump_action_pins.PIN_FILES, crlf))
    assert bump_action_pins.main([], root=root) == 0
    expected = _tag("0.1.3").replace("\n", "\r\n").encode("utf-8")
    assert all(text == expected for text in _contents(root).values())


def test_an_explicit_version_overrides_pyproject(tmp_path: Path) -> None:
    root = _repo(tmp_path, {})
    assert bump_action_pins.main(["--version", "0.2.0"], root=root) == 0
    assert all(text.decode("utf-8") == _tag("0.2.0") for text in _contents(root).values())


@pytest.mark.parametrize("version", ["v0.2.0", "0.2", "0.2.0rc1", ""])
def test_a_version_that_is_not_x_y_z_is_refused(
    tmp_path: Path, version: str, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, {})
    before = _contents(root)
    assert bump_action_pins.main(["--version", version], root=root) == 1
    assert _contents(root) == before
    assert "is not an X.Y.Z version" in capsys.readouterr().err
