"""Check a repo's three backlog files. Exit 0 clean, 1 on a finding, 2 if a file is missing.

Usage: check_backlog.py <project> <PREFIX> [root]

`main` takes the full argv, program name first, so `main(sys.argv)` is the whole entry point and
tests can call it directly. The script is lint-clean under ruff's default rules plus E, B and E501,
and `ruff format --check`, all at 100 columns, so a repo can paste it in unchanged.
"""

import re
import sys
from pathlib import Path

ANCHOR = r"(?:<a id=\"[^\"]*\"></a>)?"


def strip_comments(text: str) -> str:
    """Drop HTML comments: sample rows inside them don't count."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def comments_hiding_rows(raw: str) -> list[int]:
    """Return the 1-based line of every HTML comment that could hide a table row.

    A comment that holds a table row must say it is a sample (it starts with `<!-- sample`), and
    even then it may not touch a table: a row on the line before or after it, or two rows side by
    side inside it. Otherwise a stray comment could hide a real row from readers.
    """
    lines = raw.splitlines()

    def is_row(n: int) -> bool:
        return 0 <= n < len(lines) and lines[n].lstrip().startswith("|")

    found = []
    for m in re.finditer(r"<!--.*?-->", raw, re.S):
        first = raw.count("\n", 0, m.start())
        last = raw.count("\n", 0, m.end())
        inside = [n for n in range(first, last + 1) if is_row(n)]
        touches = is_row(first - 1) or is_row(last + 1) or any(is_row(n + 1) for n in inside)
        marked = re.match(r"<!--[ \t]*sample\b", m.group(0), re.I) is not None
        if inside and (touches or not marked):
            found.append(first + 1)
    return found


def main(argv: list[str]) -> int:
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

    problems = []
    for k in ("backlog", "archive"):
        for line_no in comments_hiding_rows(files[k].read_text(encoding="utf-8")):
            problems.append(
                f"{files[k].name}: the HTML comment at line {line_no} holds a table row without "
                "a leading '<!-- sample' marker, or touches a table, which can hide rows"
            )

    backlog = strip_comments(files["backlog"].read_text(encoding="utf-8"))
    archive = strip_comments(files["archive"].read_text(encoding="utf-8"))
    row_id = re.compile(rf"^\|\s*{ANCHOR}({prefix}-(\d+))\s*\|", re.M)
    ids = [m.group(1) for text in (backlog, archive) for m in row_id.finditer(text)]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append(f"an ID is used twice: {dupes}")

    nums = sorted(int(i.split("-")[1]) for i in ids)
    if nums and set(nums) != set(range(1, nums[-1] + 1)):
        gaps = sorted(set(range(1, nums[-1] + 1)) - set(nums))
        problems.append(f"IDs missing from both backlog and archive: {gaps}")

    next_ids = re.findall(rf"^Next ID: {prefix}-(\d+)\s*$", backlog, re.M)
    if not next_ids:
        problems.append("no 'Next ID' line")
    else:
        if len(set(next_ids)) > 1:
            problems.append(f"'Next ID' lines disagree: {next_ids}")
        highest = nums[-1] if nums else 0
        if int(next_ids[0]) <= highest:
            problems.append(f"Next ID {next_ids[0]} is not above the highest ID {highest}")

    # Every active row needs a Done when (the last column, or the one before Since in Blocked).
    active = ("In progress", "Blocked", "Ready to build")
    sections = re.split(r"^## ", backlog, flags=re.M)[1:]
    present = {sec.split("\n", 1)[0].strip() for sec in sections}
    for required in (*active, "Needs definition"):
        if required not in present:
            problems.append(f"required section '{required}' is missing or renamed")
    for sec in sections:
        name = sec.split("\n", 1)[0].strip()
        if name not in active:
            continue
        for line in sec.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not line.startswith("|") or not re.fullmatch(rf"{ANCHOR}{prefix}-\d+", cells[0]):
                continue
            done_when = cells[-2] if name == "Blocked" else cells[-1]
            if not done_when:
                problems.append(f"{cells[0]} in {name} has no Done when")

    # Every "Blocks X" needs a matching "Blocked by" on X.
    rows = {
        m.group(1): m.group(0)
        for m in re.finditer(rf"^\|\s*{ANCHOR}({prefix}-\d+)\s*\|.*$", backlog, re.M)
    }
    for rid, line in rows.items():
        for target in re.findall(rf"\bBlocks ({prefix}-\d+)", line):
            if not re.search(rf"\bBlocked by [^|]*\b{rid}\b", rows.get(target, "")):
                problems.append(
                    f"{rid} says it blocks {target}, which has no matching 'Blocked by {rid}'"
                )

    # Every open table has the same header row (Blocked adds Since at the end).
    headers = re.findall(r"^## (.+)\n\n(?:(?!## ).*\n)*?(\|.*\|)\n\|[-| ]+\|", backlog, re.M)
    if len(headers) < 4:
        problems.append(f"found {len(headers)} open tables, expected at least 4")
    base = None
    for section, header in headers:
        cols = [c.strip() for c in header.strip("|").split("|")]
        if section.strip() == "Blocked":
            cols = cols[:-1] if cols[-1] == "Since" else [*cols, "<no Since>"]
        if base is None:
            base = cols
        elif cols != base:
            problems.append(f"section '{section}' has columns {cols}, expected {base}")

    for m in re.finditer(r'<a id="([^"]+)"></a>([A-Z]+-\d+)', archive):
        if m.group(1) != m.group(2).lower():
            problems.append(f"archive anchor {m.group(1)} does not match {m.group(2)}")

    for p in problems:
        print(p)
    print("clean" if not problems else f"{len(problems)} finding(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
