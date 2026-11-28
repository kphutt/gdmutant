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


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("check_backlog_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_backlog = _load()

HEAD = "| # | Item | Who | Description | Next step | Done when |\n|---|---|---|---|---|---|\n"
BLOCKED_HEAD = (
    "| # | Item | Who | Description | Next step | Done when | Since |\n"
    "|---|---|---|---|---|---|---|\n"
)
ARCHIVE_HEAD = (
    "| # | Closed | Item | What wrapped up | Shipped in | Release |\n|---|---|---|---|---|---|\n"
)
MARKER = "<!-- backlog-format: 3 -->\n"
NOTES = MARKER + "- **One fact.** The reason.\n"


def orow(num: int, done: str = "it works", desc: str = "d") -> str:
    return f"| GM-{num} | a | agent | {desc} | n | {done} |\n"


def brow(num: int, done: str = "it works", since: str = "2026-11-27", desc: str = "d") -> str:
    return f"| GM-{num} | a | agent | {desc} | Waiting on: x | {done} | {since} |\n"


def arow(num: int, anchor: str | None = None, closed: str = "2026-11-01") -> str:
    a = f'<a id="{anchor if anchor is not None else f"gm-{num}"}"></a>'
    return f"| {a}GM-{num} | {closed} | done | w | s | r |\n"


READY_TWO = orow(2)
ARCHIVE_ONE = MARKER + ARCHIVE_HEAD + arow(1)


def backlog(
    next_id: str = "GM-3",
    progress: str = "",
    blocked: str = "",
    ready: str = READY_TWO,
    needs: str = "",
    head: str = HEAD,
) -> str:
    return (
        f"{MARKER}Next ID: {next_id}\n\n## In progress\n\n{head}{progress}\n"
        f"## Blocked\n\n{BLOCKED_HEAD}{blocked}\n"
        f"## Ready to build\n\n{head}{ready}\n## Needs definition\n\n{head}{needs}"
    )


def run(
    tmp_path: Path,
    text: str | None = None,
    archive: str = ARCHIVE_ONE,
    notes: str = NOTES,
    prefix: str = "GM",
    flags: tuple[str, ...] = (),
) -> int:
    (tmp_path / "p-backlog.md").write_text(backlog() if text is None else text, encoding="utf-8")
    (tmp_path / "p-archive.md").write_text(archive, encoding="utf-8")
    (tmp_path / "p-notes.md").write_text(notes, encoding="utf-8")
    return check_backlog.main(["check_backlog.py", "p", prefix, str(tmp_path), *flags])


def findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str | None = None, **kw
) -> str:  # type: ignore[no-untyped-def]
    """Run and require exit 1, then return what was printed."""
    assert run(tmp_path, text, **kw) == 1
    return capsys.readouterr().out


# --- clean files, arguments and missing files --------------------------------------------------


def test_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path) == 0
    assert capsys.readouterr().out.strip() == "clean"


def test_this_repos_own_backlog_files_are_clean() -> None:
    assert check_backlog.main(["check_backlog.py", "gdmutant", "GM", str(REPO)]) == 0


def test_no_arguments_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_backlog.main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_one_argument_prints_usage() -> None:
    assert check_backlog.main(["check_backlog.py", "p"]) == 2


@pytest.mark.parametrize("prefix", ["g", "G", "GMMM", "gm", "G1", ""])
def test_a_bad_prefix_is_refused(tmp_path: Path, prefix: str) -> None:
    assert run(tmp_path) == 0  # the files exist, so only the prefix is wrong
    assert check_backlog.main(["check_backlog.py", "p", prefix, str(tmp_path)]) == 2


@pytest.mark.parametrize("prefix", ["GM", "GMX"])
def test_two_and_three_letter_prefixes_are_accepted(tmp_path: Path, prefix: str) -> None:
    text = backlog().replace("GM-", f"{prefix}-")
    archive = ARCHIVE_ONE.replace("GM-", f"{prefix}-").replace("gm-", f"{prefix.lower()}-")
    assert run(tmp_path, text, archive=archive, prefix=prefix) == 0


def test_only_the_given_prefix_is_read(tmp_path: Path) -> None:
    other = "| " + "XY" + "-7 | a | agent | d | n | ok |\n"  # assembled: not a real id
    assert run(tmp_path, backlog(ready=READY_TWO + other)) == 1


@pytest.mark.parametrize("which", ["backlog", "archive", "notes"])
def test_missing_file_is_exit_two(tmp_path: Path, which: str) -> None:
    assert run(tmp_path) == 0
    (tmp_path / f"p-{which}.md").unlink()
    assert check_backlog.main(["check_backlog.py", "p", "GM", str(tmp_path)]) == 2


def test_missing_file_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_backlog.main(["check_backlog.py", "p", "GM", str(tmp_path)]) == 2
    assert "p-backlog.md" in capsys.readouterr().out


def test_a_file_that_is_not_utf8_is_exit_two(tmp_path: Path) -> None:
    assert run(tmp_path) == 0
    (tmp_path / "p-notes.md").write_bytes(b"\xff\xfe\x00bad")
    assert check_backlog.main(["check_backlog.py", "p", "GM", str(tmp_path)]) == 2


@pytest.mark.parametrize("which", ["backlog", "archive", "notes"])
def test_an_empty_file_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], which: str
) -> None:
    kw = {"backlog": "text", "archive": "archive", "notes": "notes"}[which]
    out = findings(tmp_path, capsys, **{kw: "  \n"})
    assert f"p-{which}.md is empty" in out


def test_root_defaults_to_the_current_directory(tmp_path: Path) -> None:
    """Run as a subprocess with its own cwd: a test must not chdir the pytest process itself."""
    assert run(tmp_path) == 0
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


# --- IDs and Next ID ---------------------------------------------------------------------------


def test_reused_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, backlog(ready=orow(1) + READY_TWO))
    assert "used twice" in out


def test_a_gap_in_the_sequence_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, backlog(next_id="GM-4", ready=orow(3)))
    assert "missing from both backlog and archive: [2]" in out


def test_next_id_equal_to_the_highest_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "not above" in findings(tmp_path, capsys, backlog(next_id="GM-2"))


def test_next_id_far_below_the_highest_id(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(next_id="GM-1")) == 1


def test_next_id_two_above_the_highest_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "should be 3" in findings(tmp_path, capsys, backlog(next_id="GM-4"))


def test_no_next_id_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, backlog().replace("Next ID: GM-3\n", ""))
    assert "no 'Next ID'" in out


def test_next_id_lines_disagree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "disagree" in findings(tmp_path, capsys, backlog() + "\nNext ID: GM-9\n")


def test_next_id_lines_that_agree_are_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog() + "\nNext ID: GM-3\n") == 0


@pytest.mark.parametrize(
    "line", ["next id: GM-3", "Next ID: GM-03", "Next ID:GM-3", "Next ID: GM-3."]
)
def test_a_malformed_next_id_line_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str
) -> None:
    out = findings(tmp_path, capsys, backlog().replace("Next ID: GM-3", line))
    assert "malformed Next ID" in out


def test_a_row_without_a_valid_id_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(
        tmp_path, capsys, backlog(ready=READY_TWO + "| TBD | a | agent | d | n | ok |\n")
    )
    assert "has no valid ID" in out


def test_fullwidth_digits_are_not_an_id(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO + "| GM-３ | a | agent | d | n | ok |\n")) == 1


def test_a_leading_zero_id_is_not_an_id(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO + "| GM-03 | a | agent | d | n | ok |\n")) == 1


def test_no_rows_at_all_with_next_id_one_is_clean(tmp_path: Path) -> None:
    empty = MARKER + "# t\n\n" + ARCHIVE_HEAD
    assert run(tmp_path, backlog(next_id="GM-1", ready=""), archive=empty) == 0


# --- sections and tables -----------------------------------------------------------------------

_SECTIONS = ["In progress", "Blocked", "Ready to build", "Needs definition"]


@pytest.mark.parametrize("section", _SECTIONS)
def test_a_renamed_required_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str
) -> None:
    out = findings(tmp_path, capsys, backlog().replace(f"## {section}", f"## {section} (old)"))
    assert f"required section '{section}' is missing or renamed" in out


@pytest.mark.parametrize("section", _SECTIONS)
def test_a_removed_required_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str
) -> None:
    out = findings(tmp_path, capsys, backlog().replace(f"## {section}\n", ""))
    assert f"required section '{section}' is missing or renamed" in out


def test_trailing_spaces_after_a_section_name_are_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog().replace("## Blocked\n", "## Blocked  \n")) == 0


def test_a_section_that_appears_twice_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog() + "\n## Blocked\n\n" + BLOCKED_HEAD)
    assert "appears 2 times" in out


def test_a_renamed_active_section_cannot_hide_a_row_without_done_when(tmp_path: Path) -> None:
    text = backlog(ready=orow(2, done="")).replace("## Ready to build", "## Ready")
    assert run(tmp_path, text) == 1


def test_an_optional_research_section_is_allowed(tmp_path: Path) -> None:
    assert run(tmp_path, backlog() + "\n## Research\n\n" + HEAD) == 0


def test_a_table_outside_the_open_sections_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "outside the open sections" in findings(
        tmp_path, capsys, backlog() + "\n## Misc\n\n" + HEAD
    )


def test_a_table_before_the_first_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, HEAD + "\n" + backlog())
    assert "before the first section" in out


def test_two_tables_in_one_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog(ready=READY_TWO + "\n" + HEAD))
    assert "has 2 tables, expected 1" in out


def test_a_section_with_no_table_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog(next_id="GM-1", ready="", needs="")
    text = text[: text.index("## Needs definition")] + "## Needs definition\n\nnothing here\n"
    assert "'Needs definition' has 0 tables" in findings(
        tmp_path, capsys, text, archive=MARKER + "# t\n\n" + ARCHIVE_HEAD
    )


def test_renamed_column(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, backlog().replace("Description", "Details", 1))
    assert "has columns" in out


def test_blocked_table_without_since_is_a_finding(tmp_path: Path) -> None:
    assert run(tmp_path, backlog().replace(" Since |", " When |")) == 1


def test_blocked_table_that_is_missing_its_since_column(tmp_path: Path) -> None:
    assert run(tmp_path, backlog().replace(BLOCKED_HEAD, HEAD)) == 1


def test_a_missing_separator_row_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog().replace(HEAD, HEAD.split("\n")[0] + "\n", 1))
    assert "no separator row" in out


def test_a_separator_row_of_the_wrong_width_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(
        tmp_path, capsys, backlog().replace("|---|---|---|---|---|---|---|\n", "|---|---|\n", 1)
    )
    assert "separator row has 2 cells" in out


# --- row shape ---------------------------------------------------------------------------------


@pytest.mark.parametrize("section", ["ready", "progress", "needs"])
def test_a_short_row_is_a_finding_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], section: str
) -> None:
    out = findings(tmp_path, capsys, backlog(**{section: "| GM-2 | a | agent | d |\n"}))
    assert "GM-2 in" in out and "has 4 cells, its header has 6" in out


def test_a_short_ready_row_does_not_read_the_wrong_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Five cells: counting from the end would read `n` as Done when and call this row fine.
    out = findings(tmp_path, capsys, backlog(ready="| GM-2 | a | agent | n | it works |\n"))
    assert "has 5 cells, its header has 6" in out


def test_a_short_blocked_row_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog(blocked="| GM-2 | a | agent | d | Waiting on: x | 2026-11-27 |\n", ready="")
    out = findings(tmp_path, capsys, text)
    assert "GM-2 in Blocked has 6 cells, its header has 7" in out


def test_a_long_row_is_a_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, backlog(ready="| GM-2 | a | agent | d | n | ok | extra |\n"))
    assert "has 7 cells" in out


def test_an_escaped_pipe_is_one_cell(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=orow(2, desc="a \\| b"))) == 0


@pytest.mark.parametrize("done", ["", "   ", "-", "..."])
def test_active_row_without_words_in_done_when(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], done: str
) -> None:
    out = findings(tmp_path, capsys, backlog(ready=orow(2, done=done)))
    assert "has no Done when" in out


def test_in_progress_row_without_done_when(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(progress=orow(2, done=""), ready="")) == 1


def test_blocked_row_without_done_when(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(blocked=brow(2, done=""), ready="")) == 1


def test_blocked_row_with_done_when_is_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(blocked=brow(2), ready="")) == 0


@pytest.mark.parametrize("since", ["", "soon", "2026-13-01", "27-11-2026"])
def test_a_blocked_row_needs_a_real_since_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], since: str
) -> None:
    out = findings(tmp_path, capsys, backlog(blocked=brow(2, since=since), ready=""))
    assert "has no Since date" in out


def test_needs_definition_row_may_lack_done_when(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready="", needs=orow(2, done=""))) == 0


def test_placeholder_rows_are_fine_in_their_own_sections(tmp_path: Path) -> None:
    progress = "| | Nothing in progress | | | | |\n"
    blocked = "| | Nothing blocked | | | Waiting on: ... | | |\n"
    assert run(tmp_path, backlog(progress=progress, blocked=blocked)) == 0


def test_a_placeholder_beside_real_rows_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog(progress="| | Nothing in progress | | | | | |\n" + orow(2), ready="")
    assert "placeholder row beside real rows" in findings(tmp_path, capsys, text)


def test_a_placeholder_in_the_wrong_section_is_a_finding(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO + "| | Nothing in progress | | | | | |\n")) == 1


def test_a_placeholder_of_the_wrong_width_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog(progress="| | Nothing in progress | |\n"))
    assert "placeholder row in In progress has 3 cells" in out


# --- the archive -------------------------------------------------------------------------------


def test_bad_archive_anchor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, archive=ARCHIVE_HEAD + arow(1, anchor="gm-7"))
    assert "does not match" in out


def test_a_missing_archive_anchor_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(
        tmp_path, capsys, archive=ARCHIVE_HEAD + arow(1).replace('<a id="gm-1"></a>', "")
    )
    assert 'needs the anchor <a id="gm-1"></a>' in out


def test_a_backlog_row_may_carry_a_matching_anchor(tmp_path: Path) -> None:
    ready = READY_TWO.replace("| GM-2 |", '| <a id="gm-2"></a>GM-2 |')
    assert run(tmp_path, backlog(ready=ready)) == 0


def test_a_backlog_row_with_a_wrong_anchor_is_a_finding(tmp_path: Path) -> None:
    ready = READY_TWO.replace("| GM-2 |", '| <a id="gm-9"></a>GM-2 |')
    assert run(tmp_path, backlog(ready=ready)) == 1


def test_an_archive_row_needs_a_closed_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, archive=ARCHIVE_HEAD + arow(1, closed="yesterday"))
    assert "has no Closed date" in out


def test_the_archive_release_column_is_optional(tmp_path: Path) -> None:
    head = "| # | Closed | Item | What wrapped up | Shipped in |\n|---|---|---|---|---|\n"
    row = '| <a id="gm-1"></a>GM-1 | 2026-11-01 | done | w | s |\n'
    assert run(tmp_path, archive=MARKER + head + row) == 0


def test_an_archive_with_the_wrong_columns_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, archive=ARCHIVE_ONE.replace("Closed", "Date"))
    assert "p-archive.md has columns" in out


def test_an_archive_with_two_tables_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, archive=ARCHIVE_ONE + "\n" + ARCHIVE_HEAD)
    assert "has 2 tables, expected 1" in out


def test_an_archive_with_no_table_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog(next_id="GM-1", ready=""), archive="# archive\n")
    assert "has 0 tables, expected 1" in out


# --- Blocks and Blocked by ---------------------------------------------------------------------


def _pair(blocks: str = "Blocks GM-3", blocked: str = "Blocked by GM-2") -> str:
    return orow(2, desc=blocks) + orow(3, desc=blocked)


def test_blocks_with_blocked_by(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(next_id="GM-4", ready=_pair()), archive=ARCHIVE_ONE) == 0


def test_blocks_without_blocked_by(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog(next_id="GM-4", ready=_pair(blocked="d"))
    assert "no matching 'Blocked by GM-2'" in findings(tmp_path, capsys, text)


def test_blocked_by_without_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog(next_id="GM-4", ready=_pair(blocks="d"))
    assert "no matching 'Blocks GM-3'" in findings(tmp_path, capsys, text)


def test_blocks_naming_an_id_that_does_not_exist(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog(ready=orow(2, desc="Blocks GM-9"))
    assert "GM-9, which is not an open row" in findings(tmp_path, capsys, text)


def test_blocked_by_naming_an_archived_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog(ready=orow(2, desc="Blocked by GM-1"))
    assert "GM-1, which is not an open row" in findings(tmp_path, capsys, text)


def test_a_list_of_ids_is_checked_each_way(tmp_path: Path) -> None:
    ready = orow(2, desc="Blocks GM-3, GM-4") + orow(3, desc="Blocked by GM-2")
    ready += orow(4, desc="Blocked by GM-2")
    assert run(tmp_path, backlog(next_id="GM-5", ready=ready)) == 0
    assert (
        run(tmp_path, backlog(next_id="GM-5", ready=ready.replace("Blocked by GM-2", "d", 1))) == 1
    )


def test_a_link_clause_may_end_a_sentence_or_brackets(tmp_path: Path) -> None:
    ready = orow(2, desc="Needs it (Blocks GM-3). Later.") + orow(3, desc="Blocked by GM-2.")
    assert run(tmp_path, backlog(next_id="GM-4", ready=ready)) == 0


@pytest.mark.parametrize(
    "spelling",
    [
        "blocks GM-3",
        "Blocks: GM-3",
        "Blocks GM-3 and GM-4",
        "Blocked-by GM-3",
        "Blocked on GM-3",
        "Depends on GM-3",
        "Blocks GM-3 soon",
    ],
)
def test_a_near_miss_link_spelling_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    out = findings(
        tmp_path, capsys, backlog(next_id="GM-4", ready=orow(2, desc=spelling) + orow(3))
    )
    assert "is not 'Blocks <ID>, <ID>'" in out


def test_the_word_blocks_in_prose_is_not_a_link(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=orow(2, desc="Fixes the code blocks in the guide"))) == 0


def test_links_in_a_commented_out_row_do_not_count(tmp_path: Path) -> None:
    text = backlog() + "\n<!-- sample\n\n" + orow(2, desc="Blocks GM-9") + "\n-->\n"
    assert run(tmp_path, text) == 0


# --- HTML comments -----------------------------------------------------------------------------

COMMENT = "<!-- sample\n| GM-9 | x |\n\n-->\n"  # marked, so only touching a table can fault it


def _apart(opening: str) -> str:
    """A comment holding one table row, set apart from every table by blank lines."""
    return f"\n{opening}\n\n| GM-9 | x |\n\n-->\n"


def test_a_comment_that_hides_a_table_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog().replace(READY_TWO, "<!--\n" + READY_TWO + "-->\n")
    assert "without a leading" in findings(tmp_path, capsys, text)


def test_a_comment_touching_a_table_row_from_below(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready="\n" + COMMENT + READY_TWO)) == 1


def test_a_comment_touching_a_table_row_from_above(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO + COMMENT)) == 1


def test_a_comment_with_two_rows_side_by_side_and_no_neighbours(tmp_path: Path) -> None:
    archive = ARCHIVE_ONE + "\n<!-- sample\n| GM-8 | x |\n| GM-9 | y |\n-->\n\n"
    assert run(tmp_path, archive=archive) == 1


def test_a_row_on_the_last_line_of_a_comment(tmp_path: Path) -> None:
    archive = ARCHIVE_ONE + "\n<!-- sample\n| GM-9 | y | -->\n| GM-8 | z |\n"
    assert run(tmp_path, archive=archive) == 1


def test_a_comment_inside_the_first_line_of_a_file(tmp_path: Path) -> None:
    assert run(tmp_path, archive="| GM-1 | <!-- x --> |\n| GM-2 | y |\n") == 1


def test_the_hidden_row_is_reported_at_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, archive=ARCHIVE_ONE + "<!--\n| GM-9 |\n-->\n")
    assert "line 5" in out


def test_a_sample_row_set_apart_by_blank_lines_is_fine(tmp_path: Path) -> None:
    assert run(tmp_path, archive=ARCHIVE_ONE + _apart("<!-- sample\nSample:")) == 0


def test_ids_inside_comments_do_not_count(tmp_path: Path) -> None:
    archive = ARCHIVE_ONE + "\n<!-- sample\nSample:\n\n| GM-1 | x |\n\n-->\n"
    assert run(tmp_path, archive=archive) == 0


def test_an_unmarked_comment_holding_a_row_is_a_finding_even_when_set_apart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, archive=ARCHIVE_ONE + _apart("<!--\nNot a sample:"))
    assert "'<!-- sample' marker" in out


def test_the_word_sample_on_the_next_line_is_not_a_marker(tmp_path: Path) -> None:
    assert run(tmp_path, archive=ARCHIVE_ONE + _apart("<!--\nSample:")) == 1


def test_the_sample_marker_ignores_case_and_needs_a_word_boundary(tmp_path: Path) -> None:
    assert run(tmp_path, archive=ARCHIVE_ONE + _apart("<!-- SAMPLE row")) == 0
    assert run(tmp_path, archive=ARCHIVE_ONE + _apart("<!-- sampled")) == 1


def test_a_marked_sample_comment_that_touches_a_table_is_still_a_finding(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO + COMMENT)) == 1


def test_a_comment_finding_does_not_hide_the_other_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(
        tmp_path, capsys, backlog(next_id="GM-1"), archive=ARCHIVE_ONE + _apart("<!--\nx")
    )
    assert "without a leading" in out and "not above" in out and "2 finding(s)" in out


def test_findings_in_both_files_are_each_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _apart("<!--\nx")
    out = findings(tmp_path, capsys, backlog() + bad, archive=ARCHIVE_ONE + bad)
    assert "p-backlog.md" in out and "p-archive.md" in out


def test_an_unclosed_comment_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog() + "\n<!-- never closed\n")
    assert "never closes" in out


@pytest.mark.parametrize("opener", ["<!-->", "<!--->"])
def test_a_comment_that_closes_itself_is_not_unclosed(tmp_path: Path, opener: str) -> None:
    assert run(tmp_path, backlog() + f"\n{opener}\n") == 0


def test_a_comment_opener_inside_a_code_span_is_not_a_comment(tmp_path: Path) -> None:
    assert run(tmp_path, backlog() + "\nWrite `<!--` to open a comment.\n") == 0


def test_a_notes_file_with_an_unclosed_comment_is_a_finding(tmp_path: Path) -> None:
    assert run(tmp_path, notes=NOTES + "<!-- oops\n") == 1


# --- table shapes the checks would otherwise skip ----------------------------------------------


def test_an_indented_table_row_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(
        tmp_path, capsys, backlog(ready=READY_TWO + " " + orow(2).replace("GM-2", "GM-3"))
    )
    assert "starts with spaces before its '|'" in out


def test_a_table_without_leading_pipes_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog() + "\na | b\n--- | ---\n1 | 2\n")
    assert "does not start with '|'" in out


def test_a_code_fence_is_a_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = findings(tmp_path, capsys, backlog() + "\n```\n## Blocked\n```\n")
    assert "starts a code fence" in out


def test_an_html_table_is_a_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "has an HTML table" in findings(
        tmp_path, capsys, backlog() + "\n<table><tr></tr></table>\n"
    )


def test_a_line_right_after_a_table_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog(ready=READY_TWO + "stray text\n"))
    assert "continues the table above it" in out


def test_a_blank_looking_line_after_a_table_is_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO + "   \n")) == 0


def test_a_row_with_trailing_spaces_after_its_last_pipe_is_fine(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready=READY_TWO.rstrip("\n") + "   \n")) == 0


def test_a_row_without_its_closing_pipe_keeps_its_last_cell(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready="| GM-2 | a | agent | d | n | x\n")) == 0


# --- line numbers in findings ------------------------------------------------------------------


def _line_of(text: str, wanted: str) -> int:
    return text.split("\n").index(wanted) + 1


def test_findings_name_the_right_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = backlog() + "\n## Misc\n\n" + HEAD
    out = findings(tmp_path, capsys, text)
    assert f"outside the open sections (line {_line_of(text, '## Misc') + 2})" in out
    text = backlog() + "\n<table><tr></tr></table>\n"
    out = findings(tmp_path, capsys, text)
    assert f"line {_line_of(text, '<table><tr></tr></table>')} has an HTML table" in out
    indented = " " + orow(3).rstrip("\n")
    text = backlog(ready=READY_TWO + indented + "\n", next_id="GM-4")
    out = findings(tmp_path, capsys, text)
    assert f"line {_line_of(text, indented)} starts with spaces" in out
    text = backlog().replace(HEAD, HEAD.split("\n")[0] + "\n", 1)
    out = findings(tmp_path, capsys, text)
    assert f"the table at line {_line_of(text, HEAD.split(chr(10))[0])} has no separator" in out


def test_an_unclosed_comment_is_reported_at_its_line() -> None:
    assert check_backlog.unclosed_comment("a\nb\n<!-- x") == 3
    assert check_backlog.unclosed_comment("a\n<!-- ok -->\n") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<!-- note -->\n| a |\n", []),
        ("| a |\n<!-- note -->\n", []),
        ("x\n\n<!-- sample\n\n| a |\n\n-->\n\ny\n", []),
        ("<!-- sample\n\n| a |\n\n-->\n| b |\n", [1]),
        ("| b |\n<!-- sample\n\n| a |\n\n-->\n", [2]),
        ("<!-- sample\n\n| a |\n\n-->", []),
        ("| a | <!-- x -->\n| b |\n", [1]),
        ("x\n<!-- y\n| a |\n\n-->\n", [2]),
    ],
)
def test_comments_hiding_rows_reports_exact_lines(raw: str, expected: list[int]) -> None:
    assert check_backlog.comments_hiding_rows(raw) == expected


# --- no crash on odd shapes --------------------------------------------------------------------


def test_a_row_with_an_empty_first_cell_in_an_open_section_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog(ready=READY_TWO + "| | a | agent | d | n | ok |\n"))
    assert "has no valid ID" in out


def test_a_one_cell_row_is_a_finding_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "has no valid ID" in findings(tmp_path, capsys, backlog(ready=READY_TWO + "| |\n"))


def test_a_short_row_under_a_header_with_the_id_column_last_is_not_a_crash(tmp_path: Path) -> None:
    head = (
        "| Item | Who | Effort | Description | Next step | Done when | # |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    assert run(tmp_path, backlog(ready="| a | b | c | d | e | f |\n", head=head)) == 1


def test_an_iso_date_without_dashes_is_not_a_date(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(blocked=brow(2, since="20261127"), ready="")) == 1
    assert run(tmp_path, archive=ARCHIVE_HEAD + arow(1, closed="20261101")) == 1


# --- private pointers and arguments -----------------------------------------------------------
# Hosts and IDs below are made up. The real private ID pattern is only ever passed on the command
# line from outside the repo, so nothing here names it.

# Assembled, so the repo-wide tracker-reference scan never sees an ID-shaped literal.
ZZ_ID = "ZZ" + "-" + "7"
PRIV_ID = "PRIV" + "-" + "12"
POINTER = "points at a private tracker item"


def saying(text: str) -> str:
    return backlog(ready=orow(2, desc=text))


@pytest.mark.parametrize(
    ("link", "host"),
    [
        ("https://linear.app/team/issue/" + ZZ_ID, "linear.app"),
        ("https://example.atlassian.net/browse/" + ZZ_ID, "example.atlassian.net"),
        ("https://team.notion.site/page", "team.notion.site"),
    ],
)
def test_a_tracker_url_in_a_row_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], link: str, host: str
) -> None:
    out = findings(tmp_path, capsys, saying(f"Tracked at {link}"))
    assert f"{POINTER} ({host!r})" in out


def test_a_tracker_url_is_a_finding_with_no_flags_given(tmp_path: Path) -> None:
    """The CI call passes no flags, and the built-in host list must still run there."""
    assert run(tmp_path, saying("See https://linear.app/x")) == 1


@pytest.mark.parametrize("link", ["https://github.com/o/r/issues/12", "https://notlinear.app/x"])
def test_other_hosts_are_fine(tmp_path: Path, link: str) -> None:
    assert run(tmp_path, saying(f"See {link}")) == 0


def test_allow_host_lets_a_named_public_host_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = saying("Docs at https://team.notion.site/page")
    assert run(tmp_path, text) == 1
    assert run(tmp_path, text, flags=("--allow-host", "team.notion.site")) == 0
    assert run(tmp_path, text, flags=("--allow-host=notion.site",)) == 0
    other = saying("At https://linear.app/x and https://team.notion.site/p")
    capsys.readouterr()
    assert run(tmp_path, other, flags=("--allow-host", "notion.site")) == 1
    out = capsys.readouterr().out
    assert f"{POINTER} ('linear.app')" in out
    assert "team.notion.site" not in out


def test_private_host_adds_a_host(tmp_path: Path) -> None:
    text = saying("See https://tickets.corp.example/" + ZZ_ID)
    assert run(tmp_path, text) == 0
    assert run(tmp_path, text, flags=("--private-host", "corp.example")) == 1


def test_private_id_is_a_finding_only_when_given(tmp_path: Path) -> None:
    text = saying("Follows " + PRIV_ID)
    assert run(tmp_path, text) == 0
    assert run(tmp_path, text, flags=("--private-id", "PRIV-[0-9]+")) == 1


@pytest.mark.parametrize("arg", ["--private_id", "-x", "--helpme", "--unknown=1"])
def test_an_unknown_argument_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], arg: str
) -> None:
    assert run(tmp_path) == 0  # valid files first, so exit 2 can only come from the argument
    capsys.readouterr()
    assert run(tmp_path, flags=(arg,)) == 2
    assert f"unknown argument: {arg}" in capsys.readouterr().out


def test_an_extra_positional_argument_exits_two(tmp_path: Path) -> None:
    assert run(tmp_path, flags=("extra",)) == 2


def test_a_tracker_host_inside_a_path_is_not_a_host(tmp_path: Path) -> None:
    assert run(tmp_path, saying("See https://example.org/linear.app/x")) == 0
    assert run(tmp_path, saying("See https://example.org/a/linear.app")) == 0
    assert run(tmp_path, saying("See https://linear.app/x")) == 1


def test_a_host_is_matched_whatever_its_case(tmp_path: Path) -> None:
    assert run(tmp_path, saying("See https://LINEAR.app/x")) == 1
    assert run(tmp_path, saying("See https://tickets.Corp.Example/x")) == 0
    flags = ("--private-host", "CORP.example")
    assert run(tmp_path, saying("See https://tickets.corp.example/x"), flags=flags) == 1
    allow = ("--allow-host", "LINEAR.app")
    assert run(tmp_path, saying("See https://linear.app/x"), flags=allow) == 0


def test_a_url_in_a_comment_is_still_a_finding(tmp_path: Path) -> None:
    """The file is public as written, so a comment hides nothing from a reader of the raw file."""
    text = backlog(ready=READY_TWO + "\n<!-- see https://linear.app/team/issue/" + ZZ_ID + " -->\n")
    assert run(tmp_path, text) == 1


def test_a_private_id_in_a_comment_is_still_a_finding(tmp_path: Path) -> None:
    text = backlog(ready=READY_TWO + "\n<!-- Follows " + PRIV_ID + " -->\n")
    assert run(tmp_path, text, flags=("--private-id", "PRIV-[0-9]+")) == 1


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (("--private-id",), "--private-id needs a valid regular expression"),
        (("--private-id", "("), "--private-id #1 needs a valid regular expression"),
        (("--private-id=",), "--private-id #1 needs a valid regular expression"),
        (("--private-host",), "--private-host needs a host name"),
        (("--private-host=https://x.example/",), "--private-host #1 needs a host name"),
        (("--private-host", "a b"), "--private-host #1 needs a host name"),
        (("--private-host", "x.example/path"), "--private-host #1 needs a host name"),
        (("--allow-host=x.example/path",), "--allow-host needs a host name"),
        (("--allow-host",), "--allow-host needs a host name"),
        (("--allow-host=",), "--allow-host needs a host name"),
    ],
)
def test_a_flag_with_a_bad_value_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: tuple[str, ...], message: str
) -> None:
    assert run(tmp_path) == 0
    capsys.readouterr()
    assert run(tmp_path, flags=flags) == 2
    assert message in capsys.readouterr().out


def _repo_with_private_file(tmp_path: Path, lines: str) -> tuple[Path, Path]:
    """A fake checkout (it has a `.git`) plus a private file kept outside it, as the gate needs."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    private = tmp_path / "private.txt"
    private.write_text(lines, encoding="utf-8")
    return repo, private


def test_private_file_adds_an_id_and_a_host(tmp_path: Path) -> None:
    repo, private = _repo_with_private_file(
        tmp_path, "# a comment\nid:PRIV-[0-9]+\nhost:tickets.corp.example\n"
    )
    flags = ("--private-file", str(private))
    assert run(repo, saying("Follows " + PRIV_ID), flags=flags) == 1
    assert run(repo, saying("See https://tickets.corp.example/x"), flags=flags) == 1
    assert run(repo, saying("Nothing private here"), flags=flags) == 0
    assert run(repo, saying("Follows " + PRIV_ID)) == 0  # only when the file is given


def test_private_file_works_for_a_directory_that_is_not_a_checkout(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    private = tmp_path / "private.txt"
    private.write_text("id:PRIV-[0-9]+\n", encoding="utf-8")
    assert run(plain, saying("Follows " + PRIV_ID), flags=("--private-file", str(private))) == 1


def test_private_file_may_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, private = _repo_with_private_file(tmp_path, "id:PRIV-[0-9]+\n")
    monkeypatch.setenv("CHECK_BACKLOG_PRIVATE_FILE", str(private))
    assert run(repo, saying("Follows " + PRIV_ID)) == 1
    assert run(repo, saying("Nothing private here")) == 0


def test_a_private_file_inside_the_repo_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = _repo_with_private_file(tmp_path, "")
    inside = repo / "private.txt"
    inside.write_text("id:PRIV-[0-9]+\n", encoding="utf-8")
    assert run(repo, flags=("--private-file", str(inside))) == 2
    assert "must be outside the repository" in capsys.readouterr().out


@pytest.mark.parametrize(
    "lines", [PRIV_ID + "\n", "id:(PRIV\n", "host:a/PRIV\n", "id:\n", "tracker:PRIV\n"]
)
def test_a_bad_private_file_line_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lines: str
) -> None:
    repo, private = _repo_with_private_file(tmp_path, lines)
    assert run(repo, flags=("--private-file", str(private))) == 2
    out = capsys.readouterr().out
    assert "--private-file #1 line 1" in out
    assert "PRIV" not in out  # the line is the private pattern, so it is never echoed


def test_a_missing_private_file_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, private = _repo_with_private_file(tmp_path, "")
    assert run(repo, flags=("--private-file", str(private) + ".gone")) == 2
    assert "could not be read" in capsys.readouterr().out


@pytest.mark.parametrize("lines", ["", "\n\n", "# only a comment\n", "# one\n\n  \n# two\n"])
def test_an_empty_or_comment_only_private_file_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], lines: str
) -> None:
    repo, private = _repo_with_private_file(tmp_path, lines.replace("one", PRIV_ID))
    assert run(repo, flags=("--private-file", str(private))) == 2
    out = capsys.readouterr().out
    assert "has no rules" in out
    assert "PRIV" not in out


def test_an_empty_private_file_from_the_environment_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, private = _repo_with_private_file(tmp_path, "# nothing yet\n")
    monkeypatch.setenv("CHECK_BACKLOG_PRIVATE_FILE", str(private))
    assert run(repo) == 2


_BAD_HOSTS = [
    ("tracker.example.com:8443", "not a port"),
    ("*.example.com", "not a wildcard"),
    ("https://tracker.example.com", "not a scheme"),
]


@pytest.mark.parametrize(("host", "why"), _BAD_HOSTS)
@pytest.mark.parametrize("flag", ["--private-host", "--allow-host"])
def test_a_host_that_can_never_match_is_refused_by_a_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, host: str, why: str
) -> None:
    assert run(tmp_path, flags=(flag, host)) == 2
    out = capsys.readouterr().out
    ordinal = " #1" if flag == "--private-host" else ""  # only a private rule is numbered
    assert f"{flag}{ordinal} needs a host name" in out
    assert why in out
    assert host not in out


@pytest.mark.parametrize(("host", "why"), _BAD_HOSTS)
def test_a_host_that_can_never_match_is_refused_in_the_private_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str, why: str
) -> None:
    repo, private = _repo_with_private_file(tmp_path, f"host:{host}\n")
    assert run(repo, flags=("--private-file", str(private))) == 2
    out = capsys.readouterr().out
    assert "--private-file #1 line 1 needs a host name" in out
    assert why in out
    assert host not in out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_lists_every_option_and_the_environment_variable(
    capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    assert check_backlog.main(["check_backlog.py", flag]) == 0
    out = capsys.readouterr().out
    for word in (
        "--private-id REGEX",
        "--private-host HOST",
        "--allow-host HOST",
        "--private-file PATH",
        "CHECK_BACKLOG_PRIVATE_FILE",
    ):
        assert word in out


_MALFORMED_HOSTS = [
    "tracker.example.com.",
    "user" + "@" + "tracker.example.com",  # assembled: not a real address
    "tracker.example.com?x=1",
    "tracker_x.example.com",
]


@pytest.mark.parametrize("host", _MALFORMED_HOSTS)
@pytest.mark.parametrize("flag", ["--private-host", "--allow-host"])
def test_a_malformed_host_is_refused_by_a_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, host: str
) -> None:
    assert run(tmp_path) == 0
    capsys.readouterr()
    assert run(tmp_path, flags=(flag, host)) == 2
    out = capsys.readouterr().out
    assert flag in out
    assert host not in out


@pytest.mark.parametrize("host", _MALFORMED_HOSTS)
def test_a_malformed_host_is_refused_in_the_private_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    repo, private = _repo_with_private_file(tmp_path, f"host:{host}\n")
    assert run(repo, flags=("--private-file", str(private))) == 2
    out = capsys.readouterr().out
    assert "--private-file #1 line 1" in out
    assert host not in out


@pytest.mark.parametrize("argv", [["--private-id", "-h"], ["--bogus", "-h"], ["-h", "--bogus"]])
def test_help_is_not_read_as_a_value_or_beside_other_arguments(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    assert check_backlog.main(["check_backlog.py", *argv]) != 0
    assert "CHECK_BACKLOG_PRIVATE_FILE" not in capsys.readouterr().out


def test_help_as_the_only_argument_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_backlog.main(["check_backlog.py", "-h"]) == 0
    assert "CHECK_BACKLOG_PRIVATE_FILE" in capsys.readouterr().out


@pytest.mark.parametrize("pattern", ["^PRIV-[0-9]+", "PRIV-[0-9]+$", "(PRIV)?", " PRIV-[0-9]+"])
def test_a_private_id_that_is_anchored_empty_matching_or_padded_exits_two(
    tmp_path: Path, pattern: str
) -> None:
    assert run(tmp_path, flags=(f"--private-id={pattern}",)) == 2


def test_a_private_host_that_an_allowed_host_covers_exits_two(tmp_path: Path) -> None:
    flags = ("--private-host", "tickets.corp.example", "--allow-host", "corp.example")
    assert run(tmp_path, flags=flags) == 2


def test_an_empty_private_file_variable_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHECK_BACKLOG_PRIVATE_FILE", "")
    assert run(tmp_path) == 2


def test_the_variable_and_the_option_load_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, from_env = _repo_with_private_file(tmp_path, "id:PRIV-[0-9]+\n")
    other = tmp_path / "other.txt"
    other.write_text("host:tickets.corp.example\n", encoding="utf-8")
    monkeypatch.setenv("CHECK_BACKLOG_PRIVATE_FILE", str(from_env))
    flags = ("--private-file", str(other))
    assert run(repo, saying("Follows " + PRIV_ID), flags=flags) == 1
    assert run(repo, saying("See https://tickets.corp.example/x"), flags=flags) == 1


@pytest.mark.parametrize(
    "line", ["See https://tickets.corp.example/x", "See https:/tickets.corp.example/x"]
)
def test_a_private_host_is_found_in_the_odd_spellings_of_a_url(tmp_path: Path, line: str) -> None:
    flags = ("--private-host", "corp.example")
    assert run(tmp_path, saying(line), flags=flags) == 1
    assert run(tmp_path, saying(line.replace(".", "．")), flags=flags) == 1
    assert run(tmp_path, saying(line.replace("corp", "co​rp")), flags=flags) == 1


@pytest.mark.parametrize(
    "host", ["-x.example.com", "x-.example.com", "localhost", "a..example.com"]
)
def test_a_host_with_a_bad_label_or_one_label_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], host: str
) -> None:
    assert (
        run(tmp_path, flags=(f"--private-host={host}",)) == 2
    )  # `=` form: a value may start with "-"
    assert "--private-host #1 needs a host name" in capsys.readouterr().out
    repo, private = _repo_with_private_file(tmp_path / "f", f"host:{host}\n")
    assert run(repo, flags=("--private-file", str(private))) == 2


@pytest.mark.parametrize("dot", ["．", "。", "｡"])
def test_every_idna_dot_reads_as_a_dot(tmp_path: Path, dot: str) -> None:
    text = saying("See https://tickets.corp.example/x".replace(".", dot))
    assert run(tmp_path, text, flags=("--private-host", "corp.example")) == 1


@pytest.mark.parametrize("hidden", ["​", "‌", "‍", "⁠", "﻿", "­"])
def test_every_hidden_character_is_dropped_before_the_host_scan(
    tmp_path: Path, hidden: str
) -> None:
    text = saying(f"See https://tickets.corp{hidden}.example/x")
    assert run(tmp_path, text, flags=("--private-host", "corp.example")) == 1


@pytest.mark.parametrize(
    "flag", ["--private-host", "--private-id", "--allow-host", "--private-file"]
)
@pytest.mark.parametrize("next_arg", ["--allow-host", "-x"])
def test_a_flag_followed_by_another_flag_is_missing_its_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, next_arg: str
) -> None:
    assert run(tmp_path, flags=(flag, next_arg, "x.example")) == 2
    assert "needs" in capsys.readouterr().out


def test_a_pattern_that_matches_empty_text_is_refused_at_load_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, flags=("--private-id", "(PRIV)?")) == 2
    out = capsys.readouterr().out
    assert "matches empty text" in out
    assert "it does at" not in out


def test_a_pattern_that_only_matches_empty_in_the_files_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, flags=("--private-id", r"\b")) == 2
    assert "it does at" in capsys.readouterr().out


def test_a_private_file_host_that_an_allowed_host_covers_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, private = _repo_with_private_file(tmp_path, "host:tickets.corp.example\n")
    flags = ("--private-file", str(private), "--allow-host", "corp.example")
    assert run(repo, flags=flags) == 2
    assert "--private-file #1 line 1 names a host" in capsys.readouterr().out


@pytest.mark.parametrize("root", ["", "no-such-directory"])
def test_a_root_that_is_not_a_directory_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], root: str
) -> None:
    assert run(tmp_path) == 0
    capsys.readouterr()
    assert (
        check_backlog.main(["check_backlog.py", "p", "GM", str(tmp_path / root) if root else ""])
        == 2
    )
    assert "root must be an existing directory" in capsys.readouterr().out


@pytest.mark.parametrize("project", ["../p", ".p", "p/q", "p q", ""])
def test_a_project_name_that_is_not_a_plain_name_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], project: str
) -> None:
    assert run(tmp_path) == 0
    capsys.readouterr()
    assert check_backlog.main(["check_backlog.py", project, "GM", str(tmp_path)]) == 2
    assert "project must be" in capsys.readouterr().out


# --- backlog format 3: no Effort column, the format marker, the row bar, no PR in Item ---------


def _marker_case(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, first_line: str | None
) -> str:
    """Replace the marker line of one file (`None` removes it), run, and return what was printed."""
    files = {"text": backlog(), "archive": ARCHIVE_ONE, "notes": NOTES}
    key = "text" if kind == "backlog" else kind
    rest = files[key].replace(MARKER, "", 1)
    files[key] = rest if first_line is None else first_line + "\n" + rest
    assert run(tmp_path, **files) == 1
    return capsys.readouterr().out


def test_a_table_that_still_has_an_effort_column_is_a_finding_in_every_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old_head = HEAD.replace("| Who |", "| Who | Effort |").replace("|---|", "|---|---|", 1)
    old_blocked = BLOCKED_HEAD.replace("| Who |", "| Who | Effort |").replace(
        "|---|", "|---|---|", 1
    )
    text = backlog().replace(HEAD, old_head).replace(BLOCKED_HEAD, old_blocked)
    out = findings(tmp_path, capsys, text)
    assert out.count("has columns") == 4 and out.count("'Effort'") == 4


def test_an_effort_cell_in_a_row_of_the_new_layout_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = findings(tmp_path, capsys, backlog(ready="| GM-2 | a | agent | S | d | n | ok |\n"))
    assert "GM-2 in Ready to build has 7 cells, its header has 6" in out


@pytest.mark.parametrize("kind", ["backlog", "archive", "notes"])
def test_a_missing_format_marker_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    out = _marker_case(tmp_path, capsys, kind, None)
    assert f"p-{kind}.md: backlog-format marker missing: add `<!-- backlog-format: 3 -->`" in out
    assert "1 finding(s)" in out


@pytest.mark.parametrize("kind", ["backlog", "archive", "notes"])
@pytest.mark.parametrize("version", ["1", "2", "4", "20"])
def test_an_unknown_format_version_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, version: str
) -> None:
    out = _marker_case(tmp_path, capsys, kind, f"<!-- backlog-format: {version} -->")
    assert f"p-{kind}.md: unknown backlog format {version}" in out


@pytest.mark.parametrize(
    "bad",
    [
        "<!-- backlog-format: two -->",
        "<!-- backlog-format: -->",
        "<!-- backlog-format: 2.0 -->",
        "<!-- backlog-format: 02 -->",
        "<!-- backlog-format: 0 -->",
        "<!--backlog-format: 3-->",
        "<!-- backlog-format 3 -->",
        "<!-- Backlog-Format: 3 -->",
        "<!--  backlog-format: 3 -->",
        "<!-- backlog-format: 3 --> trailing",
    ],
)
def test_a_malformed_format_marker_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    assert "p-backlog.md: malformed backlog-format marker" in _marker_case(
        tmp_path, capsys, "backlog", bad
    )


def test_two_format_markers_are_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _marker_case(tmp_path, capsys, "backlog", MARKER.strip() + "\n" + MARKER.strip())
    assert "p-backlog.md: 2 backlog-format markers, expected exactly 1" in out


def test_a_format_marker_past_the_fifth_line_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "a\nb\nc\nd\n" + backlog()) == 0  # the marker lands on line 5
    assert run(tmp_path, "a\nb\nc\nd\ne\n" + backlog()) == 1
    assert "backlog-format marker is on line 6" in capsys.readouterr().out


def test_a_bom_before_the_format_marker_is_clean(tmp_path: Path) -> None:
    assert run(tmp_path, "﻿" + backlog()) == 0


def test_a_marker_shown_in_a_code_span_is_not_a_second_marker(tmp_path: Path) -> None:
    notes = NOTES + "\nWrite `<!-- backlog-format: 3 -->` first.\n"
    assert run(tmp_path, notes=notes) == 0


def test_a_marker_only_inside_a_code_span_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = backlog().replace(MARKER, "`" + MARKER.strip() + "`\n", 1)
    assert "backlog-format marker missing" in findings(tmp_path, capsys, text)


def test_help_prints_the_kit_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_backlog.main(["check_backlog.py", "--help"]) == 0
    assert capsys.readouterr().out.startswith("backlogs kit, backlog format 3")


_LEAD = "A long lead-in that keeps this row over the length gate " + "word " * 40


def _described(text: str, section: str = "ready") -> str:
    return backlog(**{section: f"| GM-2 | a | agent | {text} | n | ok |\n"})


@pytest.mark.parametrize(
    "text",
    [
        "One sentence",
        "What it is. Why it matters.",
        "Why? Because.",
        "Use e.g. Foo or bar. Then stop",
        "Bump to v1.2.3 now. It is old",
        "Read [the ADR](https://example.com/a.b/C) first. Then go",
        "Run `a. B. C. D` first. Then go",
        "Ship at 3 p.m. Monday. Then stop.",
        "Fix the build. Needs Prof. Lee to review.",
        "Fix the build. Due Jan. 5 per Mr. Lee.",
        "One. Two. Three.",
        "",
    ],
)
def test_a_description_of_at_most_two_sentences_is_clean(tmp_path: Path, text: str) -> None:
    assert run(tmp_path, _described(text)) == 0


@pytest.mark.parametrize(
    "text",
    [
        "One. Two. Three.",
        "One. Two! Three? Four",
        "One. Two.  Three",
        "Fix the bug. Test the fix. Ship the fix",
    ],
)
def test_a_description_of_three_sentences_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    out = findings(tmp_path, capsys, _described(_LEAD + text))
    assert "GM-2 in Ready to build has a Description of more than 2 sentences" in out


def test_the_sentence_cap_covers_blocked_and_needs_definition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _LEAD + "One. Two. Three."
    blocked = f"| GM-2 | a | agent | {text} | Waiting on: x | ok | 2026-01-02 |\n"
    out = findings(tmp_path, capsys, backlog(blocked=blocked, ready=""))
    assert "GM-2 in Blocked has a Description of more than 2 sentences" in out
    out = findings(
        tmp_path, capsys, backlog(needs=f"| GM-2 | a | agent | {text} | n | ok |\n", ready="")
    )
    assert "GM-2 in Needs definition has a Description of more than 2" in out


def test_a_closing_bracket_after_a_full_stop_still_ends_the_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _LEAD + "It is done (see the doc.) Then more. Then stop"
    assert "more than 2 sentences" in findings(tmp_path, capsys, _described(text))


def test_a_short_row_is_never_flagged_whatever_its_punctuation(tmp_path: Path) -> None:
    assert run(tmp_path, _described("One. Two. Three. Four. Five.")) == 0


def test_the_length_gate_is_a_strict_greater_than(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = "One. Two. Three."
    at_gate = base + "x" * (200 - len(base))
    assert len(at_gate) == 200 and run(tmp_path, _described(at_gate)) == 0
    assert "more than 2 sentences" in findings(tmp_path, capsys, _described(at_gate + "x"))


def _in_progress(item: str) -> str:
    return backlog(progress=f"| GM-2 | {item} | agent | d | n | ok |\n", ready="")


@pytest.mark.parametrize(
    "item",
    [
        "Fix it (PR #41)",
        "PR #7 for the fix",
        "Fix it (pr #4)",
        "Fix it (PR#4)",
        "Fix it (PR 4)",
        "Fix it (PR: #4)",
        "Fix it (PRs #4, #5)",
        "Fix it (Pull Request 4)",
        "Fix it (pull requests #4 and #5)",
        "Fix it https://github.com/o/r/pull/4",
        "Fix it o/r/pull/4/files",
    ],
)
def test_an_in_progress_row_citing_a_pr_is_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], item: str
) -> None:
    out = findings(tmp_path, capsys, _in_progress(item))
    assert "GM-2 in In progress cites a PR in its Item cell" in out


@pytest.mark.parametrize(
    "item",
    [
        "Fix it",
        "Fix it for issue #41",
        "Fix the PRNG",
        "#41",
        "Pull 4 levers",
        "Rework PR 2FA flow",
        "Add pull request 3-way merge",
    ],
)
def test_an_in_progress_row_without_a_pr_citation_is_clean(tmp_path: Path, item: str) -> None:
    assert run(tmp_path, _in_progress(item)) == 0


def test_a_pr_citation_outside_in_progress_is_left_alone(tmp_path: Path) -> None:
    assert run(tmp_path, backlog(ready="| GM-2 | Fix it (PR #41) | agent | d | n | ok |\n")) == 0
