"""Tests for `scripts/check_backlog.py`, the structural check on the three backlog files.

The script sits outside the coverage and mutation gates (both cover only `gdmutant/`), so these
tests are the only thing pinning its logic. Each check in the script has a case here that fails
when that check is dropped or its comparison is flipped.

`scripts/` is deliberately left out of the sdist, so the whole module skips when it runs from an
unpacked source distribution.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_backlog.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason="scripts/ is not in the sdist, so there is nothing to load"
)

HEAD = (
    "| # | Item | Who | Effort | Description | Next step | Done when |\n"
    "|---|---|---|---|---|---|---|\n"
)
BLOCKED = (
    "| # | Item | Who | Effort | Description | Next step | Done when | Since |\n"
    "|---|---|---|---|---|---|---|---|\n"
)
ROW_TWO = "| GM-2 | a | agent | S | d | n | it works |\n"
COMMENT = "<!-- sample\n| GM-9 | x |\n\n-->\n"  # marked, so only touching a table can fault it
ARCHIVE = "| # | Item |\n|---|---|\n| GM-1 | done |\n"


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("check_backlog_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_backlog = _load()


def backlog(
    next_id: str = "GM-3", ready: str = "", blocked: str = "", progress: str = "", extra: str = ""
) -> str:
    return (
        f"Next ID: {next_id}\n\n## In progress\n\n{HEAD}{progress}\n"
        f"## Blocked\n\n{BLOCKED}{blocked}\n"
        f"## Ready to build\n\n{HEAD}{ready}\n## Needs definition\n\n{HEAD}{extra}"
    )


def run(tmp_path: Path, text: str, archive: str = ARCHIVE, prefix: str = "GM") -> int:
    (tmp_path / "p-backlog.md").write_text(text, encoding="utf-8")
    (tmp_path / "p-archive.md").write_text(archive, encoding="utf-8")
    (tmp_path / "p-notes.md").write_text("", encoding="utf-8")
    return check_backlog.main(["check_backlog.py", "p", prefix, str(tmp_path)])


def test_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO)) == 0
    assert capsys.readouterr().out.strip() == "clean"


def test_this_repos_own_backlog_files_are_clean() -> None:
    assert check_backlog.main(["check_backlog.py", "gdmutant", "GM", str(REPO)]) == 0


def test_reused_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, backlog(ready="| GM-1 | a | agent | S | d | n | ok |\n" + ROW_TWO)) == 1
    assert "used twice" in capsys.readouterr().out


def test_a_gap_in_the_sequence(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog(next_id="GM-4", ready="| GM-3 | a | agent | S | d | n | ok |\n")
    assert run(tmp_path, text) == 1
    assert "missing" in capsys.readouterr().out


def test_next_id_equal_to_the_highest_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, backlog(next_id="GM-2", ready=ROW_TWO)) == 1
    assert "not above" in capsys.readouterr().out


def test_next_id_far_below_the_highest_id(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(next_id="GM-1", ready=ROW_TWO)) == 1


def test_next_id_just_above_the_highest_id_is_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(next_id="GM-3", ready=ROW_TWO)) == 0


def test_no_next_id_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog(ready=ROW_TWO).replace("Next ID: GM-3\n", "")
    assert run(tmp_path, text) == 1
    assert "no 'Next ID'" in capsys.readouterr().out


def test_next_id_lines_disagree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO) + "\nNext ID: GM-9\n") == 1
    assert "disagree" in capsys.readouterr().out


def test_next_id_lines_that_agree_are_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO) + "\nNext ID: GM-3\n") == 0


def test_renamed_column(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO).replace("Effort", "Size", 1)) == 1
    assert "has columns" in capsys.readouterr().out


def test_blocked_table_without_since_is_a_finding(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO).replace(" Since |", " When |")) == 1


def test_blocked_table_that_is_missing_its_since_column(tmp_path: Path) -> None:
    text = backlog(ready=ROW_TWO).replace(BLOCKED, HEAD)
    assert run(tmp_path, text) == 1


def test_fewer_than_four_tables(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog(ready=ROW_TWO)
    text = text[: text.index("## Needs definition")]
    assert run(tmp_path, text) == 1
    assert "open tables" in capsys.readouterr().out


def test_bad_archive_anchor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = '| # | Item |\n|---|---|\n| <a id="gm-7"></a>GM-1 | done |\n'
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1
    assert "does not match" in capsys.readouterr().out


def test_good_archive_anchor(tmp_path: Path) -> None:
    archive = '| # | Item |\n|---|---|\n| <a id="gm-1"></a>GM-1 | done |\n'
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 0


@pytest.mark.parametrize("which", ["backlog", "archive", "notes"])
def test_missing_file_is_exit_two(tmp_path: Path, which: str) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO)) == 0
    (tmp_path / f"p-{which}.md").unlink()
    assert check_backlog.main(["check_backlog.py", "p", "GM", str(tmp_path)]) == 2


def test_missing_file_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_backlog.main(["check_backlog.py", "p", "GM", str(tmp_path)]) == 2
    assert "p-backlog.md" in capsys.readouterr().out


def test_blocks_without_blocked_by(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ready = (
        "| GM-2 | a | agent | S | Blocks GM-3 | n | ok |\n| GM-3 | b | agent | S | d | n | ok |\n"
    )
    assert run(tmp_path, backlog(next_id="GM-4", ready=ready)) == 1
    assert "Blocked by" in capsys.readouterr().out


def test_blocks_with_blocked_by(tmp_path: Path) -> None:
    ready = (
        "| GM-2 | a | agent | S | Blocks GM-3 | n | ok |\n"
        "| GM-3 | b | agent | S | Blocked by GM-2 | n | ok |\n"
    )
    assert run(tmp_path, backlog(next_id="GM-4", ready=ready)) == 0


def test_blocks_naming_an_id_that_does_not_exist(tmp_path: Path) -> None:
    ready = "| GM-2 | a | agent | S | Blocks GM-9 | n | ok |\n"
    assert run(tmp_path, backlog(ready=ready)) == 1


def test_active_row_without_done_when(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, backlog(ready="| GM-2 | a | agent | S | d | n |  |\n")) == 1
    assert "no Done when" in capsys.readouterr().out


def test_in_progress_row_without_done_when(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(progress="| GM-2 | a | agent | S | d | n |  |\n")) == 1


def test_blocked_row_without_done_when(tmp_path: Path) -> None:
    row = "| GM-2 | a | agent | S | d | Waiting on: x |  | 2026-11-27 |\n"
    assert run(tmp_path, backlog(blocked=row)) == 1


def test_blocked_row_with_done_when_is_fine(tmp_path: Path) -> None:
    row = "| GM-2 | a | agent | S | d | Waiting on: x | ok | 2026-11-27 |\n"
    assert run(tmp_path, backlog(blocked=row)) == 0


def test_needs_definition_row_may_lack_done_when(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(extra="| GM-2 | a | agent | ? | d | n |  |\n")) == 0


def test_a_comment_that_hides_a_table_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog(ready=ROW_TWO).replace(ROW_TWO, "<!--\n" + ROW_TWO + "-->\n")
    assert run(tmp_path, text) == 1
    assert "without a leading" in capsys.readouterr().out


def test_a_comment_touching_a_table_row_from_below(tmp_path: Path) -> None:
    text = backlog(ready=COMMENT + ROW_TWO)
    assert run(tmp_path, text) == 1


def test_a_comment_touching_a_table_row_from_above(tmp_path: Path) -> None:
    text = backlog(ready=ROW_TWO + COMMENT)
    assert run(tmp_path, text) == 1


def test_a_comment_hiding_a_row_in_the_archive(tmp_path: Path) -> None:
    archive = ARCHIVE + "<!--\n| GM-2 | x |\n| GM-3 | y |\n-->\n"
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1


def test_a_sample_row_set_apart_by_blank_lines_is_fine(tmp_path: Path) -> None:
    archive = ARCHIVE + "\n<!-- sample\nSample:\n\n| GM-9 | x |\n\n-->\n"
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 0


def test_ids_inside_comments_do_not_count(tmp_path: Path) -> None:
    archive = ARCHIVE + "\n<!-- sample\nSample:\n\n| GM-1 | x |\n\n-->\n"
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 0


def test_anchored_ids_are_read(tmp_path: Path) -> None:
    ready = '| <a id="gm-2"></a>GM-2 | a | agent | S | d | n | ok |\n'
    assert run(tmp_path, backlog(ready=ready)) == 0
    assert run(tmp_path, backlog(ready=ready + ROW_TWO)) == 1


def test_only_the_given_prefix_is_read(tmp_path: Path) -> None:
    other = "| " + "XY" + "-7 | a | agent | S | d | n | ok |\n"  # assembled: not a real id
    assert run(tmp_path, backlog(ready=ROW_TWO + other)) == 0


def test_no_arguments_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_backlog.main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_one_argument_prints_usage() -> None:
    assert check_backlog.main(["check_backlog.py", "p"]) == 2


@pytest.mark.parametrize("prefix", ["g", "G", "GMMM", "gm", "G1", ""])
def test_a_bad_prefix_is_refused(tmp_path: Path, prefix: str) -> None:
    assert (
        run(tmp_path, backlog(ready=ROW_TWO)) == 0
    )  # the files exist, so only the prefix is wrong
    assert check_backlog.main(["check_backlog.py", "p", prefix, str(tmp_path)]) == 2


@pytest.mark.parametrize("prefix", ["GM", "GMX"])
def test_two_and_three_letter_prefixes_are_accepted(tmp_path: Path, prefix: str) -> None:
    text = backlog(ready=ROW_TWO).replace("GM-", f"{prefix}-")
    archive = ARCHIVE.replace("GM-", f"{prefix}-")
    assert run(tmp_path, text, archive=archive, prefix=prefix) == 0


def test_root_defaults_to_the_current_directory(tmp_path: Path) -> None:
    """Run as a subprocess with its own cwd: a test must not chdir the pytest process itself."""
    assert run(tmp_path, backlog(ready=ROW_TWO)) == 0
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "p", "GM"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert (done.returncode, done.stdout.strip()) == (0, "clean")
    empty = tmp_path / "empty"
    empty.mkdir()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "p", "GM"],
        cwd=empty,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert done.returncode == 2


def test_the_gap_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog(next_id="GM-4", ready="| GM-3 | a | agent | S | d | n | ok |\n")
    assert run(tmp_path, text) == 1
    assert "[2]" in capsys.readouterr().out


def test_no_rows_at_all_with_next_id_one_is_clean(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(next_id="GM-1"), archive="") == 0


def test_a_line_that_is_not_a_table_row_is_not_checked_for_done_when(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO + "GM-5 |  |\n")) == 0


def test_a_comment_inside_the_first_line_of_a_file(tmp_path: Path) -> None:
    archive = "| GM-1 | <!-- x --> |\n| GM-2 | y |\n"
    assert run(tmp_path, backlog(next_id="GM-3"), archive=archive) == 1


def test_a_comment_touching_a_row_only_from_below(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready="\n" + COMMENT + ROW_TWO)) == 1


def test_a_comment_with_two_rows_side_by_side_and_no_neighbours(tmp_path: Path) -> None:
    archive = ARCHIVE + "\n<!-- sample\n| GM-8 | x |\n| GM-9 | y |\n-->\n\n"
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1


def test_a_row_on_the_last_line_of_a_comment(tmp_path: Path) -> None:
    archive = ARCHIVE + "\n<!-- sample\n| GM-9 | y | -->\n| GM-8 | z |\n"
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1


def test_the_hidden_row_is_reported_at_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = "| # |\n|---|\n<!--\n| GM-9 |\n-->\n"
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1
    assert "line 3" in capsys.readouterr().out


def _apart(opening: str) -> str:
    """A comment holding one table row, set apart from every table by blank lines."""
    return f"\n{opening}\n\n| GM-9 | x |\n\n-->\n"


def test_an_unmarked_comment_holding_a_row_is_a_finding_even_when_set_apart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = ARCHIVE + _apart("<!--\nNot a sample:")
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1
    assert "'<!-- sample' marker" in capsys.readouterr().out


def test_a_marked_sample_comment_that_touches_a_table_is_still_a_finding(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=ROW_TWO + COMMENT)) == 1


def test_the_word_sample_on_the_next_line_is_not_a_marker(tmp_path: Path) -> None:
    archive = ARCHIVE + _apart("<!--\nSample:")
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=archive) == 1


def test_the_sample_marker_ignores_case_and_needs_a_word_boundary(tmp_path: Path) -> None:
    ok = ARCHIVE + _apart("<!-- SAMPLE row")
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=ok) == 0
    bad = ARCHIVE + _apart("<!-- sampled")
    assert run(tmp_path, backlog(ready=ROW_TWO), archive=bad) == 1


def test_a_comment_finding_does_not_hide_the_other_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = ARCHIVE + _apart("<!--\nx")
    assert run(tmp_path, backlog(next_id="GM-1", ready=ROW_TWO), archive=archive) == 1
    out = capsys.readouterr().out
    assert "without a leading" in out and "not above" in out and "2 finding(s)" in out


def test_findings_in_both_files_are_each_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _apart("<!--\nx")
    assert run(tmp_path, backlog(ready=ROW_TWO) + bad, archive=ARCHIVE + bad) == 1
    out = capsys.readouterr().out
    assert "p-backlog.md" in out and "p-archive.md" in out


_SECTIONS = ["In progress", "Blocked", "Ready to build", "Needs definition"]


@pytest.mark.parametrize("section", _SECTIONS)
def test_a_renamed_required_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str
) -> None:
    text = backlog(ready=ROW_TWO).replace(f"## {section}", f"## {section} (old)")
    assert run(tmp_path, text) == 1
    assert f"required section '{section}' is missing or renamed" in capsys.readouterr().out


@pytest.mark.parametrize("section", _SECTIONS)
def test_a_removed_required_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str
) -> None:
    text = backlog(ready=ROW_TWO).replace(f"## {section}\n", "")
    assert run(tmp_path, text) == 1
    assert f"required section '{section}' is missing or renamed" in capsys.readouterr().out


def test_trailing_spaces_after_a_section_name_are_fine(tmp_path: Path) -> None:
    text = backlog(ready=ROW_TWO).replace("## Blocked\n", "## Blocked  \n")
    assert run(tmp_path, text) == 0


def test_a_renamed_active_section_cannot_hide_a_row_without_done_when(tmp_path: Path) -> None:
    text = backlog(ready="| GM-2 | a | agent | S | d | n |  |\n").replace(
        "## Ready to build", "## Ready"
    )
    assert run(tmp_path, text) == 1
