"""Check a repo's three backlog files. Exit 0 clean, 1 on a finding, 2 on a usage or input error.

Usage: check_backlog.py <project> <PREFIX> [root] [--private-id REGEX] [--private-host HOST]
       [--allow-host HOST] [--private-file PATH]

Each option may repeat, and `--option=value` works too. Any other argument is an error (exit 2).
Every input is checked against the one form it may take, never against a list of bad forms, so
a malformed value exits 2 instead of loading and then matching nothing. `<project>` is ASCII
letters, digits, `.`, `_` and `-`, starting with a letter or digit; `<PREFIX>` is two or three
capital letters; `[root]` is an existing directory. A HOST is two or more dot-separated labels of
ASCII letters, digits and hyphens, with no hyphen at either end of a label; case is folded. A
REGEX must compile, have no space at either end, never match empty text, and hold no anchor
(`^`, `$`, `\\A`, `\\Z`), since it is searched inside each line. A private host that an
`--allow-host` covers is an error, since it could never match. `-h` or `--help` prints help
only as the lone argument, and a flag whose next argument starts with `-` is missing its value:
write such a value as `--option=-value`.

`--private-file PATH` reads private patterns from a file kept outside the repo, so a public repo
never has to write them down. One entry per line, `id:REGEX` or `host:HOST`; blank lines and
lines starting with `#` are skipped. The file is read as UTF-8, a BOM and Windows line endings
are ignored, and a space before or after an entry's kind or value is an error. The
`CHECK_BACKLOG_PRIVATE_FILE` environment variable names one more such file; set but empty, it
is an error. Every file named, by the variable or by the option, adds to `--private-id` and
`--private-host`, and a path named twice is read once. A file inside the checked repo is
refused. An unreadable file or a bad line exits 2. A finding from a file entry names the file
line and never prints the pattern or the text it matched.

Every structural assumption the checks rely on is itself checked, so a missing, renamed or
misshapen part is a finding, never a silent skip: section names and counts, table headers and
separator rows, each row's cell count, each row's ID and anchor, the Next ID line, the
Blocks and Blocked by links, and the archive table. No file may point at a private tracker: the
raw text, HTML comments included, is scanned for text matching `--private-id`, and for a URL on
`--private-host` or a built-in hosted-tracker host (minus `--allow-host`). Those are findings. A
phrase such as "see the private ticket" only prints a `check:` note and never changes the exit
code. Columns are looked up by their header name, never by counting from either end. Digits
are ASCII only, and lines are split on "\\n" only.

`main` takes the full argv, program name first, so `main(sys.argv)` is the whole entry point and
tests can call it directly. The script is lint-clean under ruff's default rules plus E, B and E501,
and `ruff format --check`, all at 100 columns, so a repo can paste it in unchanged.
"""

import contextlib
import os
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
# Hosted trackers whose URLs are findings unless `--allow-host` exempts them. Not exhaustive:
# `--private-host` adds more, and `--private-id` catches an ID whatever the host.
TRACKER_HOSTS = (
    "linear.app",
    "atlassian.net",
    "app.asana.com",
    "shortcut.com",
    "clickup.com",
    "monday.com",
    "trello.com",
    "notion.so",
    "notion.site",
)
# A dotted name, with or without a scheme. The lookbehind stops `notlinear.app` from reading as
# `linear.app`; a name just after a single `/` (a path, not `//host`) is skipped in the code.
HOSTLIKE = re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+[a-z0-9-]+", re.I)
# Before the host scan, as a browser reads a host: the IDNA dots (fullwidth, ideographic and
# halfwidth) become `.`, and zero-width characters and the soft hyphen are dropped.
HOST_SPELLING = str.maketrans(
    {"\uff0e": ".", "\u3002": ".", "\uff61": "."}
    | dict.fromkeys(["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"])
)
# `https:/linear.app`: one slash after a scheme still reads as a URL, unlike a path segment.
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:/\Z")
# The one accepted form of a `--private-host`, `--allow-host` or `host:` value: a name HOSTLIKE
# can read, with real DNS label rules. ASCII only, and checked before the value is lowercased,
# since `str.lower()` turns the Kelvin sign into `k`. Case is then folded, as HOSTLIKE ignores it.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
HOST_VALUE = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
# A project name: the stem of the three file names, so never a path.
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EMPTY_MATCH = "matches empty text; it must match the ID itself"
# A phrase that reads like a pointer to a private item. Only a prompt to look, never a finding.
# It spans line breaks and hyphens. Each form needs a lead word or an article, so "skip private
# items" and "private vulnerability reporting" are not hits.
_ARTICLE = r"(?:the|a|an|our)[\s-]+"
_PRIVATE = r"(?:(?:related|linked)[\s-]+)?(?:private|internal)[\s-]+(?:[A-Za-z]+[\s-]+)?"
PRIVATE_POINTER = re.compile(
    r"\b(?:"
    rf"(?:see|per|via|in|at|related|linked|tracked)[\s-]+(?:{_ARTICLE})?{_PRIVATE}"
    r"(?:ticket|tracker|issue|finding|item)s?"
    rf"|{_ARTICLE}{_PRIVATE}(?:ticket|tracker|issue|finding|item|repository|repo)s?"
    rf"|{_ARTICLE}(?:linear|jira|asana|shortcut|clickup|trello|notion)[\s-]+(?:ticket|issue|item)s?"
    r")\b",
    re.I,
)


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


def host_problem(host: str) -> str | None:
    """Why `host` is not a host name HOSTLIKE could read from a file, or None. Never quotes it.

    The only accepted form is two or more labels joined by dots, each label ASCII letters, digits
    and hyphens, with no hyphen at either end of a label. Anything else could never match, so it
    is refused. The reason names a common slip, as a hint; the form itself is the rule.
    """
    if HOST_VALUE.fullmatch(host):
        return None
    if "://" in host:
        return "not a scheme"
    if "*" in host:
        return "not a wildcard (a name already matches its subdomains)"
    if ":" in host:
        return "not a port"
    return (
        "two or more dot-separated labels of ASCII letters, digits and hyphens, "
        "with no dot or hyphen at either end of a label"
    )


def id_problem(value: str) -> str | None:
    """Why `value` is not a usable private ID pattern, or None. Never quotes it.

    It must be a regular expression that compiles, has no space at either end, and cannot match
    empty text (a pattern that does would point at every line, or at none).
    """
    if not value:
        return "needs a valid regular expression"
    if value != value.strip():
        return "has a space at its start or end; write \\s or [ ] inside the pattern instead"
    try:
        rule = re.compile(value)
    except re.error:
        return "needs a valid regular expression"
    if rule.search("") is not None:
        return EMPTY_MATCH
    if has_anchor(value):
        return (
            "has an anchor (^, $, \\A or \\Z); patterns are searched inside each line, "
            "so an anchor would miss an ID with text around it"
        )
    return None


def has_anchor(pattern: str) -> bool:
    """True if `pattern` holds `^`, `$`, `\\A`, `\\Z` or `\\z` outside a character class.

    An escaped `\\^` or `\\$` and a `^` or `$` inside `[...]` are literal text, so they pass.
    `\\A` and `\\Z` do not compile inside a class, so any one found is an anchor.
    """
    i, in_class = 0, False
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if pattern[i + 1 : i + 2] in ("A", "Z", "z"):
                return True
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            i += 1
            i += pattern.startswith("^", i)  # a negated class
            i += pattern.startswith("]", i)  # a `]` first in a class is literal
            in_class = True
            continue
        elif c in "^$":
            return True
        i += 1
    return False


def empty_match_line(rule: re.Pattern, raw: dict) -> str | None:
    """Where `rule` matches empty text in the files, as 'file line N', or None.

    A pattern such as `\\b` passes the load-time check, since it needs text around it, so the
    scan looks for an empty match in the files themselves.
    """
    for name, text in raw.items():
        for line_no, line in enumerate(text.split("\n"), start=1):
            if any(m.start() == m.end() for m in rule.finditer(line)):
                return f"{name} line {line_no}"
    return None


def host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def check_private_pointers(raw: str, name: str, opts: dict, problems: list[str], notes: list[str]):
    """Scan the raw text, comments included, since the file is public as written.

    A match for `--private-id`, or a URL on a private or built-in tracker host that is not
    allowed, is a finding. A pointer-like phrase is only a note.
    """
    hosts = [*TRACKER_HOSTS, *opts["private_host"]]
    for line_no, line in enumerate(raw.split("\n"), start=1):
        found = [m.group(0) for r in opts["private_id"] for m in r.finditer(line)]
        # A rule from the private file is named by its line there, never by what it matched.
        found += [f"private file line {n}" for r, n in opts["file_id"] if r.search(line)]
        seen = line.translate(HOST_SPELLING)  # what a browser would read as the host
        for m in HOSTLIKE.finditer(seen):
            before = seen[: m.start()]
            if before.endswith("/") and not before.endswith("//") and not SCHEME.search(before):
                continue  # `x/linear.app`: a path segment, not a host
            host = m.group(0).lower()
            if host_in(host, opts["allow_host"]):
                continue
            if host_in(host, hosts):
                found.append(host)
            found += [f"private file line {n}" for h, n in opts["file_host"] if host_in(host, [h])]
        for what in found:
            problems.append(
                f"{name}: line {line_no} points at a private tracker item ({what!r}); "
                "a public row must stand on its own"
            )
    for m in PRIVATE_POINTER.finditer(raw):
        line_no = raw.count("\n", 0, m.start()) + 1
        notes.append(
            f"check: {name}: line {line_no} reads like a pointer to a private item "
            f"({' '.join(m.group(0).split())!r}); confirm the row stands on its own"
        )


def repo_top(root: Path) -> Path | None:
    """The git work tree holding `root` (a `.git` entry in it or a parent), or None."""
    here = root.resolve()
    return next((d for d in (here, *here.parents) if (d / ".git").exists()), None)


def load_private_file(path: str, opts: dict, root: Path) -> str | None:
    """Add the entries of a private file to `opts`, or return the error to print (exit 2).

    A file inside the checked repo is refused, since a committed pattern file is the leak this
    option exists to prevent. Errors name the file line and never quote it: the line is the
    private pattern.
    """
    top = repo_top(root)
    try:
        if top is not None and Path(path).resolve().is_relative_to(top):
            return "--private-file must be outside the repository, so the list is never committed"
    except (OSError, ValueError):
        return "--private-file could not be read"
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError
        return "--private-file could not be read"
    before = len(opts["file_id"]) + len(opts["file_host"])
    for line_no, line in enumerate(text.split("\n"), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        kind, _, value = line.partition(":")
        where = f"--private-file line {line_no}"
        if kind not in ("id", "host"):
            return f"{where} must be id:REGEX or host:HOST, with nothing before or after it"
        if kind == "id":
            if problem := id_problem(value):
                return f"{where} {problem}"
            opts["file_id"].append((re.compile(value), line_no))
        elif problem := host_problem(value):
            return f"{where} needs a host name such as example.com, {problem}"
        else:
            opts["file_host"].append((value.lower(), line_no))
    if len(opts["file_id"]) + len(opts["file_host"]) == before:
        return "--private-file has no rules, so it would check nothing; add id: or host: lines"
    return None


HELP = """usage: check_backlog.py <project> <PREFIX> [root] [options]

Checks <project>-backlog.md, <project>-archive.md and <project>-notes.md in [root] (default .).
Exit 0 when clean, 1 on findings, 2 on a usage or input error.

arguments:
  <project>            ASCII letters, digits, '.', '_' and '-', starting with a letter or digit
  <PREFIX>             two or three capital letters A-Z
  [root]               an existing directory

options (each may repeat; --option=value works too):
  -h, --help           print this help and exit 0; only on its own, with no other argument
  --private-id REGEX   fail on text matching REGEX anywhere in a line: a valid pattern with
                       no anchor (^, $, \\A, \\Z), no space at either end, and no match on
                       empty text
  --private-host HOST  fail on a URL on HOST or a subdomain of it; no --allow-host may cover it
  --allow-host HOST    exempt a public page on HOST or a subdomain of it
  --private-file PATH  read id:REGEX and host:HOST lines from a file outside the repository;
                       a file with no rules is an error; adds to CHECK_BACKLOG_PRIVATE_FILE

A HOST is two or more labels joined by dots, such as example.com. Each label is ASCII letters,
digits and hyphens, with no hyphen at either end. Case does not matter. Anything else, such as
a scheme, a port, a path, a wildcard, a trailing dot or a space, is an error. A one-word
intranet host such as jira is not a HOST: match it with --private-id instead.

A flag's value is the next argument unless that starts with '-'; then the value is missing,
which is an error. Write a value that starts with '-' as --option=value.

environment:
  CHECK_BACKLOG_PRIVATE_FILE  one more --private-file, read as well as every flag;
                              set but empty is an error"""
USAGE = (
    "usage: check_backlog.py <project> <PREFIX> [root] [--private-id REGEX] "
    "[--private-host HOST] [--allow-host HOST] [--private-file PATH]"
)
OPTIONS = {
    "--private-id": "private_id",
    "--private-host": "private_host",
    "--allow-host": "allow_host",
    "--private-file": "private_file",
}
VALUE_OF = {
    "--private-id": "a valid regular expression",
    "--private-host": "a host name such as example.com",
    "--allow-host": "a host name such as example.com",
    "--private-file": "a path",
}


def parse_args(argv: list[str]) -> tuple[list[str], dict] | str:
    """Split argv into positionals and options, or return the text to print.

    Each argument is read once, in order: a flag's value is never also read as a flag, so a
    value of `-h` can never turn into a request for help. HELP comes back only for a lone
    `-h` or `--help`; anything else returned as a string is an error (exit 2).
    """
    opts: dict = {key: [] for key in (*OPTIONS.values(), "file_id", "file_host")}
    positional = [argv[0]] if argv else []
    rest = iter(argv[1:])
    for arg in rest:
        if arg in ("-h", "--help"):
            if len(argv) != 2:
                return f"{arg} prints help only on its own; drop the other arguments, or drop {arg}"
            return HELP
        if not arg.startswith("-"):
            positional.append(arg)
            continue
        flag, has_value, value = arg.partition("=")
        if flag not in OPTIONS:
            return f"unknown argument: {arg}"
        if not has_value:
            value = next(rest, None)
            if value is None or value.startswith("-"):
                return (
                    f"{flag} needs {VALUE_OF[flag]}; write one that starts with '-' as {flag}=VALUE"
                )
        key = OPTIONS[flag]
        if key == "private_id":
            if problem := id_problem(value):
                return f"--private-id {problem}"
            opts[key].append(re.compile(value))
        elif key == "private_file":
            if not value:
                return "--private-file needs a path"
            opts[key].append(value)
        elif problem := host_problem(value):
            return f"{flag} needs a host name such as example.com, {problem}"
        else:
            opts[key].append(value.lower())
    return positional, opts


def main(argv: list[str]) -> int:
    # Findings quote file text, which may hold characters a Windows console code page lacks.
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parsed = parse_args(argv)
    if parsed is HELP:
        print(HELP)
        return 0
    if isinstance(parsed, str):
        print(parsed)
        return 2
    args, opts = parsed
    if not 3 <= len(args) <= 4:
        print(USAGE)
        return 2
    project, prefix = args[1], args[2]
    if not PROJECT.fullmatch(project):
        print(
            "project must be ASCII letters, digits, '.', '_' and '-', starting with a letter "
            f"or digit, got {project!r}"
        )
        return 2
    if not re.fullmatch(r"[A-Z]{2,3}", prefix):
        print(f"prefix must be two or three capital letters, got {prefix!r}")
        return 2
    prefix = re.escape(prefix)
    root_arg = args[3] if len(args) > 3 else "."
    root = Path(root_arg)
    if not root_arg or not root.is_dir():
        print(f"root must be an existing directory, got {root_arg!r}")
        return 2
    env_file = os.environ.get("CHECK_BACKLOG_PRIVATE_FILE")
    if env_file == "":
        print("CHECK_BACKLOG_PRIVATE_FILE is set but empty; unset it or name a file")
        return 2
    # The variable and every --private-file all apply; a path named twice is read once.
    paths = [*([env_file] if env_file else []), *opts["private_file"]]
    for path in dict.fromkeys(str(Path(x).resolve()) for x in paths):
        error = load_private_file(path, opts, root)
        if error:
            print(error)
            return 2
    # A private host inside an allowed one is exempted before it is checked, so it never matches.
    pairs = [("a --private-host value", h) for h in opts["private_host"]]
    pairs += [(f"--private-file line {n}", h) for h, n in opts["file_host"]]
    for where, host in pairs:
        if host_in(host, opts["allow_host"]):
            print(f"{where} names a host an --allow-host covers, so it could never match")
            return 2
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
    rules = [("--private-id", r) for r in opts["private_id"]]
    rules += [(f"--private-file line {n}", r) for r, n in opts["file_id"]]
    for where, rule in rules:
        if spot := empty_match_line(rule, {files[k].name: raw[k] for k in raw}):
            print(f"{where} {EMPTY_MATCH} (it does at {spot})")
            return 2

    problems: list[str] = []
    notes: list[str] = []
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
    for k, p in files.items():
        check_private_pointers(raw[k], p.name, opts, problems, notes)
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

    for p in [*problems, *notes]:
        print(p)
    print("clean" if not problems else f"{len(problems)} finding(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
