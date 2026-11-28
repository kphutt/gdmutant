"""Check a repo's three backlog files. Exit 0 clean, 1 on a finding, 2 if a file is missing.

Usage: check_backlog.py <project> <PREFIX> [root]

Every structural assumption the checks rely on is itself checked, so a missing, renamed or
misshapen part is a finding, never a silent skip: section names and counts, table headers and
separator rows, each row's cell count, each row's ID and anchor, the Next ID line, the
Blocks and Blocked by links, and the archive table. Columns are looked up by their header name,
never by counting from either end. Digits are ASCII only, and lines are split on "\\n" only.

`main` takes the full argv, program name first, so `main(sys.argv)` is the whole entry point and
tests can call it directly. The script is lint-clean under ruff's default rules plus E, B and E501,
and `ruff format --check`, all at 100 columns, so a repo can paste it in unchanged.
"""

import contextlib
import re
import sys
from datetime import date
from pathlib import Path

ID_NUMBER = r"[1-9][0-9]*"  # ASCII only: `\d` also matches fullwidth and other digits
ANCHORED_ID = r"(?:<a id=\"([^\"]*)\"></a>)?({prefix}-" + ID_NUMBER + ")"
OPEN_COLUMNS = ["#", "Item", "Who", "Effort", "Description", "Next step", "Done when"]
ARCHIVE_COLUMNS = ["#", "Closed", "Item", "What wrapped up", "Shipped in", "Release"]
ACTIVE = ("In progress", "Blocked", "Ready to build")
REQUIRED = (*ACTIVE, "Needs definition")
OPTIONAL = ("Research",)
# The template's empty-table rows: the only rows allowed without an ID, and only in their section.
PLACEHOLDERS = {"In progress": "Nothing in progress", "Blocked": "Nothing blocked"}
# `<!-->` and `<!--->` close themselves; a comment with no `-->` runs to the end of the file.
COMMENT = re.compile(r"<!--(?:-?>|.*?-->|.*)", re.S)
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.*?[^`])\1(?!`)")
# A delimiter row of one or more cells, with at least one pipe: `---|---`, `--- |`, `:-: |`.
DELIMITER_ROW = re.compile(r"(?=[^|]*-)(?=.*\|)[\s:|-]+")


def mask_code_spans(text: str) -> str:
    """Blank out inline code spans, keeping every offset, so their text is never read as HTML."""
    return "\n".join(
        CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line) for line in text.split("\n")
    )


def comment_spans(raw: str) -> list[tuple[int, int, bool]]:
    """Return (start, end, closed) for every HTML comment, skipping `<!--` inside a code span."""
    masked = mask_code_spans(raw)
    return [(m.start(), m.end(), m.group(0).endswith("-->")) for m in COMMENT.finditer(masked)]


def strip_comments(text: str) -> str:
    """Drop closed HTML comments: sample rows inside them don't count. Line numbers are kept."""
    out, pos = [], 0
    for start, end, closed in comment_spans(text):
        if closed:
            out += [text[pos:start], "\n" * text.count("\n", start, end)]
            pos = end
    return "".join([*out, text[pos:]])


def comments_hiding_rows(raw: str) -> list[int]:
    """Return the 1-based line of every HTML comment that could hide a table row.

    A comment that holds a table row must say it is a sample (it starts with `<!-- sample`), and
    even then it may not touch a table: a row on the line before or after it, or two rows side by
    side inside it. Otherwise a stray comment could hide a real row from readers.
    """
    lines = raw.split("\n")  # not splitlines(): it also splits on form feeds and U+2028

    def is_row(n: int) -> bool:
        return 0 <= n < len(lines) and lines[n].lstrip().startswith("|")

    found = []
    for start, end, closed in comment_spans(raw):
        if not closed:
            continue
        first = raw.count("\n", 0, start)
        last = raw.count("\n", 0, end)
        inside = [n for n in range(first, last + 1) if is_row(n)]
        touches = is_row(first - 1) or is_row(last + 1) or any(is_row(n + 1) for n in inside)
        marked = re.match(r"<!--[ \t]*sample\b", raw[start:end], re.I) is not None
        if inside and (touches or not marked):
            found.append(first + 1)
    return found


def unclosed_comment(raw: str) -> int | None:
    """Return the 1-based line of an HTML comment that never closes, or None."""
    for start, _, closed in comment_spans(raw):
        if not closed:
            return raw.count("\n", 0, start) + 1
    return None


def split_cells(line: str) -> list[str]:
    """Split a table line into cells: one outer pipe off each end, `\\|` is not a divider."""
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


def parse_tables(text: str, name: str, problems: list[str]) -> list[dict]:
    """Return every table in `text` as {line, header, rows}, each row as (line, cells).

    A table is a run of lines that start with `|`. A line the checks would otherwise skip is a
    finding: one indented before its `|`, a non-blank line right after a table (Markdown reads it
    as another row), a table with no leading pipes, an HTML table, a code fence, and a table with
    a missing or misshapen separator row.
    """
    tables: list[dict] = []
    current: list[tuple[int, str]] = []
    lines = text.split("\n")
    for i, line in enumerate([*lines, ""]):
        if re.match(r" {0,3}(?:```|~~~)", line):
            problems.append(
                f"{name}: line {i + 1} starts a code fence, which can hide sections and rows"
            )
        if re.search(r"<table\b", mask_code_spans(line), re.I):
            problems.append(f"{name}: line {i + 1} has an HTML table; use a Markdown table")
        is_table = line.lstrip().startswith("|")
        if is_table and not line.startswith("|"):
            problems.append(f"{name}: line {i + 1} starts with spaces before its '|'")
        if is_table:
            current.append((i + 1, line))
            continue
        if DELIMITER_ROW.fullmatch(line):
            problems.append(
                f"{name}: line {i + 1} is a table row that does not start with '|'; "
                "start every table line with '|'"
            )
        if current and line.strip():
            problems.append(
                f"{name}: line {i + 1} continues the table above it without a blank line"
            )
        if current:
            tables.append(parse_table(current, name, problems))
            current = []
    return tables


def parse_table(lines: list[tuple[int, str]], name: str, problems: list[str]) -> dict:
    start, header_line = lines[0]
    header = split_cells(header_line)
    body = lines[1:]
    sep = split_cells(body[0][1]) if body else []
    if not body or not all(re.fullmatch(r":?-+:?", c) for c in sep):
        problems.append(f"{name}: the table at line {start} has no separator row under its header")
    else:
        if len(sep) != len(header):
            problems.append(
                f"{name}: the table at line {start}'s separator row has {len(sep)} cells, "
                f"its header has {len(header)}"
            )
        body = body[1:]
    return {"line": start, "header": header, "rows": [(n, split_cells(t)) for n, t in body]}


def check_rows(table: dict, where: str, prefix: str, placeholder, need_anchor, problems) -> list:
    """Return (id, cells by column name) for each row with an ID; report every row that is off.

    A row whose cell count differs from its header comes back with None for its cells, since
    its columns cannot be trusted. An archive row needs the anchor `<a id="<lowercase id>"></a>`;
    a backlog row may carry one, and then it must match too.
    """
    header = table["header"]
    good = []
    placeholders = 0
    for line_no, cells in table["rows"]:
        id_col = header.index("#") if "#" in header else 0
        first = cells[id_col] if id_col < len(cells) else ""
        m = re.fullmatch(ANCHORED_ID.format(prefix=prefix), first)
        if m is None:
            item_col = header.index("Item") if "Item" in header else -1
            item = cells[item_col] if 0 <= item_col < len(cells) else None
            if first == "" and placeholder is not None and item == placeholder:
                placeholders += 1
                if len(cells) != len(header):
                    problems.append(
                        f"the placeholder row in {where} has {len(cells)} cells, "
                        f"its header has {len(header)}"
                    )
            else:
                problems.append(f"a row in {where} has no valid ID: {first!r} (line {line_no})")
            continue
        anchor, rid = m.group(1), m.group(2)
        if need_anchor and anchor != rid.lower():
            problems.append(f'{rid} in {where} needs the anchor <a id="{rid.lower()}"></a>')
        elif anchor is not None and anchor != rid.lower():
            problems.append(f"{rid} in {where} has the anchor {anchor!r}, expected {rid.lower()!r}")
        if len(cells) != len(header):
            problems.append(
                f"{rid} in {where} has {len(cells)} cells, its header has {len(header)}"
            )
            good.append((rid, None))
            continue
        good.append((rid, dict(zip(header, cells, strict=True))))
    if placeholders and len(table["rows"]) > 1:
        problems.append(f"{where} has a placeholder row beside real rows; delete the placeholder")
    return good


def is_date(text: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def check_links(rows: dict, open_ids: set, prefix: str, problems: list[str]) -> None:
    """Check every `Blocks <ID>, <ID>` and `Blocked by <ID>, <ID>` both ways.

    A clause ends its cell, its sentence or its brackets (a period or `)` follows it). A link-like
    word followed by an ID in any other spelling, such as lowercase, a colon, "and", `Blocked-by`,
    `Blocked on` or `Depends on`, is a finding, so a link is never silently skipped. The word
    alone, as in "code blocks", is plain prose.
    """
    ref = rf"{prefix}-{ID_NUMBER}"
    clause = re.compile(rf"\b(Blocks|Blocked by) ({ref}(?:, {ref})*)(?=[.)]|$)")
    near_miss = re.compile(
        rf"\b(?:block(?:s|ed|ing)?|depends?)(?:[\s_-]+(?:by|on))?[\s:_(-]*{prefix}-", re.I
    )
    links: dict[str, dict[str, list[str]]] = {}
    for rid, cells in rows.items():
        found: dict[str, list[str]] = {"Blocks": [], "Blocked by": []}
        malformed = False
        for cell in cells.values():
            for m in clause.finditer(cell):
                found[m.group(1)] += m.group(2).split(", ")
            rest = clause.sub("", cell)
            malformed |= near_miss.search(rest) is not None
        if malformed:
            problems.append(
                f"{rid} has a Blocks or Blocked by that is not 'Blocks <ID>, <ID>' or "
                "'Blocked by <ID>, <ID>' at the end of a cell, sentence or brackets"
            )
        links[rid] = found
    for rid, found in links.items():
        for target in found["Blocks"]:
            if target not in open_ids:
                problems.append(f"{rid} says it blocks {target}, which is not an open row")
            elif target in links and rid not in links[target]["Blocked by"]:
                problems.append(
                    f"{rid} says it blocks {target}, which has no matching 'Blocked by {rid}'"
                )
        for source in found["Blocked by"]:
            if source not in open_ids:
                problems.append(f"{rid} says it is blocked by {source}, which is not an open row")
            elif source in links and rid not in links[source]["Blocks"]:
                problems.append(
                    f"{rid} says it is blocked by {source}, which has no matching 'Blocks {rid}'"
                )


def main(argv: list[str]) -> int:
    # Findings quote file text, which may hold characters a Windows console code page lacks.
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if len(argv) < 3:
        print("usage: check_backlog.py <project> <PREFIX> [root]")
        return 2
    project, prefix = argv[1], argv[2]
    if not re.fullmatch(r"[A-Z]{2,3}", prefix):
        print(f"prefix must be two or three capital letters, got {prefix!r}")
        return 2
    prefix = re.escape(prefix)
    root = Path(argv[3]) if len(argv) > 3 else Path(".")
    files = {k: root / f"{project}-{k}.md" for k in ("backlog", "archive", "notes")}
    missing = [str(p) for p in files.values() if not p.is_file()]
    if missing:
        print("missing:", ", ".join(missing))
        return 2

    raw = {}
    for k, p in files.items():
        try:
            raw[k] = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(f"{p.name} is not valid UTF-8")
            return 2

    problems: list[str] = []
    for k, p in files.items():
        if not raw[k].strip():
            problems.append(f"{p.name} is empty")
        line_no = unclosed_comment(raw[k])
        if line_no is not None:
            problems.append(
                f"{p.name}: the HTML comment at line {line_no} never closes, which hides "
                "everything after it"
            )
    for k in ("backlog", "archive"):
        for line_no in comments_hiding_rows(raw[k]):
            problems.append(
                f"{files[k].name}: the HTML comment at line {line_no} holds a table row without "
                "a leading '<!-- sample' marker, or touches a table, which can hide rows"
            )

    backlog = strip_comments(raw["backlog"])
    archive = strip_comments(raw["archive"])
    rows: list[tuple[str, dict | None]] = []  # (id, cells by column name) from every table

    # The backlog: each section by name, each table in the section it sits in.
    headings = [
        (i, line[3:].strip())
        for i, line in enumerate(backlog.split("\n"), start=1)
        if line.startswith("## ")
    ]
    names = [n for _, n in headings]
    for name in sorted(set(names)):
        if names.count(name) > 1:
            problems.append(f"section '{name}' appears {names.count(name)} times")
    for required in REQUIRED:
        if required not in names:
            problems.append(f"required section '{required}' is missing or renamed")
    by_section: dict[str, list[dict]] = {}
    for table in parse_tables(backlog, files["backlog"].name, problems):
        above = [n for i, n in headings if i < table["line"]]
        section = above[-1] if above else None
        if section not in (*REQUIRED, *OPTIONAL):
            place = f"in section '{section}'" if section else "before the first section"
            problems.append(f"a table {place} is outside the open sections (line {table['line']})")
            continue
        by_section.setdefault(section, []).append(table)
    for section, tables in by_section.items():
        if len(tables) > 1:
            problems.append(f"section '{section}' has {len(tables)} tables, expected 1")
        expected = [*OPEN_COLUMNS, "Since"] if section == "Blocked" else OPEN_COLUMNS
        for table in tables:
            if table["header"] != expected:
                problems.append(
                    f"section '{section}' has columns {table['header']}, expected {expected}"
                )
            placeholder = PLACEHOLDERS.get(section)
            found = check_rows(table, section, prefix, placeholder, False, problems)
            rows += found
            if section not in ACTIVE:
                continue
            # Every active row needs a Done when with words in it, and a Blocked row its Since.
            for rid, cells in found:
                if cells is None:
                    continue
                if not re.search(r"\w", cells.get("Done when", "x")):
                    problems.append(f"{rid} in {section} has no Done when")
                if section == "Blocked" and not is_date(cells.get("Since", "2000-01-01")):
                    problems.append(f"{rid} in Blocked has no Since date (YYYY-MM-DD)")
    for required in REQUIRED:
        if required in names and required not in by_section:
            problems.append(f"section '{required}' has 0 tables, expected 1")
    open_ids = {rid for rid, _ in rows}
    check_links({r: c for r, c in rows if c is not None}, open_ids, prefix, problems)

    # The archive: one table with the template's header (Release is optional), every row dated.
    archive_tables = parse_tables(archive, files["archive"].name, problems)
    if len(archive_tables) != 1:
        problems.append(f"{files['archive'].name} has {len(archive_tables)} tables, expected 1")
    for table in archive_tables:
        if table["header"] not in (ARCHIVE_COLUMNS, ARCHIVE_COLUMNS[:-1]):
            problems.append(
                f"{files['archive'].name} has columns {table['header']}, "
                f"expected {ARCHIVE_COLUMNS} (Release is optional)"
            )
        found = check_rows(table, files["archive"].name, prefix, None, True, problems)
        rows += found
        for rid, cells in found:
            if cells is not None and not is_date(cells.get("Closed", "2000-01-01")):
                problems.append(f"{rid} in {files['archive'].name} has no Closed date (YYYY-MM-DD)")

    ids = [rid for rid, _ in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"an ID is used twice: {dupes}")

    nums = sorted(int(i.split("-")[1]) for i in ids)
    if nums and set(nums) != set(range(1, nums[-1] + 1)):
        gaps = sorted(set(range(1, nums[-1] + 1)) - set(nums))
        problems.append(f"IDs missing from both backlog and archive: {gaps}")

    next_ids = []
    for line in backlog.split("\n"):
        if not re.match(r"[\W_]*next[\W_]*id\b", line, re.I):
            continue
        m = re.fullmatch(rf"Next ID: {prefix}-({ID_NUMBER})\s*", line)
        if m is None:
            problems.append(f"malformed Next ID line: {line.strip()!r}")
        else:
            next_ids.append(m.group(1))
    if not next_ids:
        problems.append("no 'Next ID' line")
    else:
        if len(set(next_ids)) > 1:
            problems.append(f"'Next ID' lines disagree: {next_ids}")
        highest = nums[-1] if nums else 0
        if int(next_ids[0]) <= highest:
            problems.append(f"Next ID {next_ids[0]} is not above the highest ID {highest}")
        elif int(next_ids[0]) != highest + 1:
            problems.append(
                f"Next ID {next_ids[0]} should be {highest + 1}, one above the highest ID {highest}"
            )

    for m in re.finditer(r'<a id="([^"]*)"></a>([A-Z]+-[0-9]+)', archive):
        if m.group(1) != m.group(2).lower():
            problems.append(f"archive anchor {m.group(1)!r} does not match {m.group(2)}")

    for p in problems:
        print(p)
    print("clean" if not problems else f"{len(problems)} finding(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
