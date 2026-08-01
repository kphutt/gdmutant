"""The self-contained HTML report — gdmutant's own viewer, not a generic one.

``gdmutant run --html out.html`` writes **one file that needs nothing else**: no CDN, no fonts, no
images, no fetch. Open it on a plane, attach it to a CI run, mail it to a teammate. Every style,
every script and the mascot are inlined; the only ``http`` in the page is a documentation hyperlink
you may click, never a resource the page needs to render.

What it shows, and why it is not the stock viewer:

* **Findings, not mutants.** A mutant is a unit of *execution*; a finding is a unit of *work*.
  ``0 -> 1`` and ``0 -> -1`` on the same token are two angles on one gap ("this number is not
  pinned") that **one** test closes, so reporting them separately duplicates an identical narrative
  and inflates the to-do list. Findings group by ``(line, span, operator)`` and **never across
  operators**: on ``return 0`` the ``numeric`` span sits inside the ``statement-deletion`` span, yet
  they are genuinely different gaps (the value is not pinned / the whole line could vanish
  unnoticed).
* **The changed characters are marked**, not a marker beside them — the token itself carries the
  tint and the click target.
* **The narrative travels with it.** Each survivor's gap/risk/start copy comes from `explain`, the
  same words the console prints, plus the operator reference inlined from `survivor_reference` so
  an offline reader can still learn what the operator means.
* **Every finding has an address.** A finding's id is the tuple it was grouped by
  (``line:col:colEnd:operator``), which the page joins to the file path to get a key that is stable
  across regeneration — so a finding can be linked to (`location.hash`) and remembered
  (`localStorage`) instead of only looked at. An earlier positional id renumbered on every run,
  which made both impossible.
* Paths are shown relative to the project root. The report is made to travel (mailed, attached to
  a review, handed to a colleague), and an absolute path from the machine that produced it is noise
  to every reader but its author, whose username and directory layout it also carries. Given the
  project root (`project_dir`), a file inside it is displayed as its project-relative path. A file
  genuinely outside the project keeps its absolute one, because there is no shorter honest name for
  it. The displayed path is what the page addresses findings by, so a run that moves between
  machines keeps its links and its done-marks.

The rendered page keeps the full ``mutation-testing-elements`` report in a
``<script type="application/json">`` block, so the file stays machine-readable for other tooling.
A download button hands that block back as a ``.json`` file through a ``blob:`` URL, which is
built in the page from bytes the page already carries, with no request and no new data.

The view model (`report_view`) is built and typed here in Python, where it is testable; the inlined
script is a thin renderer over it and derives no verdicts of its own. That split is deliberate — an
earlier draft of this design let the view assert "every test still passed" from a template, which
printed it under all 18 mutants including the 11 a test had actually killed.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from gdmutant.engine.explain import DOC_BASE_URL, _display_col, context_section
from gdmutant.engine.survivor_reference import SURVIVOR_REFERENCE

#: Statuses that count as *caught* — the same set `MutationRun.detected` counts (a mutation that
#: hung the suite was observably caught, so a timeout is a kill).
_DETECTED = frozenset({"Killed", "Timeout"})

#: Schema status -> (tag word, colour class, the outcome phrase shown beside the change).
#: Every claim the page makes about a mutant is a finished string built **here**, from the run's
#: real status, because the view is not allowed to state a fact of its own.
_OUTCOME: dict[str, tuple[str, str, str]] = {
    "Survived": ("survived", "sv", "no test failed"),
    "Killed": ("caught", "kd", "a test failed"),
    "Timeout": ("caught", "kd", "the suite timed out, which counts as caught"),
    "Ignored": ("ignored", "ot", "suppressed by an ignore annotation, so it never ran"),
    "CompileError": ("invalid", "ot", "it did not parse, so it never ran"),
    "RuntimeError": ("error", "ot", "the run errored"),
}

#: The rare statuses, surfaced in the header only when non-zero. ``NoCoverage`` (never emitted) and
#: ``Undetected`` (identical to ``Survived`` here) are omitted entirely.
#:
#: Each header count is also a filter: the reader can click "204 runtime errors" and land on the
#: mutants behind it. Those three numbers are not one thing, and the page must not let them read as
#: one. A *timeout* is a kill and only ever a performance signal, a *compile error* is the re-parse
#: guard working and the mutant never ran, but a *runtime error* is a mutant that was valid, did
#: run, and whose harness then fell over. That last one measured nothing, so a big count there is a
#: blind spot in the score, and it is the one a reader most needs to be able to reach.
_RARE: tuple[tuple[str, str], ...] = (
    ("timeout", "Timeout"),
    ("ignored", "Ignored"),
    ("compile errors", "CompileError"),
    ("runtime errors", "RuntimeError"),
)

#: The statuses `_RARE` lets the reader filter by, as a set. Recorded per finding so the page can
#: answer "which findings hold a runtime error?" without shipping a status on every angle.
_RARE_STATUSES = frozenset(status for _, status in _RARE)

TAGLINE = "Mutation testing for GDScript and Godot — find the bugs your green tests would miss."

#: Frank, inlined. The repo's copy at ``.github/assets/frank.svg`` is a README asset and ships in no
#: distribution (see the sdist allowlist in ``pyproject.toml``), and a URL would put the page
#: back on the network — so the markup lives here. ``tests/test_htmlreport.py`` pins it to that
#: file, so the two cannot drift. Sized by CSS; the fixed width/height and the ``<title>`` are
#: dropped because the ``aria-label`` already names him.
FRANK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" class="frank" role="img"'
    ' aria-label="Frank the Mutant">'
    '<rect x="4" y="33" width="8" height="9" rx="2" fill="#8d9199"/>'
    '<rect x="52" y="33" width="8" height="9" rx="2" fill="#8d9199"/>'
    '<rect x="11" y="12" width="42" height="42" rx="4" fill="#6fbf5e"/>'
    '<path d="M11 16 a4 4 0 0 1 4-4 h34 a4 4 0 0 1 4 4 v8 H11 Z" fill="#20242b"/>'
    '<path d="M11 29 H53" stroke="#2f7a29" stroke-width="2.2" fill="none"/>'
    '<path d="M17 25.5 v7 M26 25.5 v7 M35 25.5 v7 M44 25.5 v7" stroke="#2f7a29"'
    ' stroke-width="2.2" stroke-linecap="round"/>'
    '<circle cx="24" cy="40" r="6.5" fill="#ffffff"/>'
    '<circle cx="24" cy="40" r="3.2" fill="#20242b"/>'
    '<circle cx="41" cy="41" r="4" fill="#ffffff"/>'
    '<circle cx="41" cy="41" r="2" fill="#20242b"/>'
    '<path d="M23 49 q9 5 18 -1" stroke="#20242b" stroke-width="2.5" stroke-linecap="round"'
    ' fill="none"/>'
    "</svg>"
)

#: The report's tab icon — Frank again, built from the very markup above so the two can never
#: diverge, and carried as a ``data:`` URI so the page still fetches nothing. This is the one
#: surface gdmutant controls where a favicon is actually consumed: someone comparing two runs has
#: several report tabs open, and the icon is what tells them apart.
#:
#: **Base64, not percent-encoding.** The art is full of ``#`` colour literals, and a bare ``#``
#: inside a ``data:`` URI starts the fragment — the icon then fails *silently*, with the markup
#: looking perfectly correct in view-source. Base64 cannot have that failure. It costs ~33% over
#: the raw bytes and still lands near 1.2 KB, which is a rounding error beside the report itself.
#: (The masthead art and this share ``.github/assets/frank.svg`` as their source; a change to
#: Frank's colours would reach both.)
FAVICON_HREF = "data:image/svg+xml;base64," + base64.b64encode(FRANK_SVG.encode()).decode("ascii")


# --------------------------------------------------------------------------------------------
# View model
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Angle:
    """One mutant inside a finding: what it changed, and how that attempt ended."""

    change: str
    tag: str
    cls: str
    outcome: str


@dataclass
class Finding:
    """One unit of work: a token, an operator, and every mutant that tried it.

    ``fid`` is the *stable* identity — the very tuple `_findings` groups by, rendered as
    ``line:col:colEnd:operator``. It is therefore unique within a file by construction, and it is
    the same string every time the report is regenerated from source that has not moved. Joined to
    the file's path (`finding_key`) it addresses a finding across the whole report, which is what
    lets a selection live in the URL and a done-mark live in `localStorage`.

    ``start`` is the console's ``start`` field verbatim: the missing *input* to add a test for.
    It is deliberately **not** called "fix" — `explain` only names the input, never the expected
    value, because that oracle is the reader's and a guess could codify a bug.
    """

    fid: str
    line: int
    col: int
    colEnd: int  # noqa: N815 — matches the key the inlined renderer reads
    op: str
    func: str
    #: The survivor-reference section this finding expands and links to — its operator, or
    #: `explain.ASSERT_SECTION` when the mutated token sits inside an `assert`. Stored per finding
    #: (unlike the doc URL, which the page builds) because only the generator can see the source
    #: line the rule reads, and a page that showed "what a comparison survivor means" beside an
    #: explanation about asserts would contradict itself.
    ref: str = ""
    #: The rare statuses (`_RARE_STATUSES`) among this finding's angles, deduped: what a clicked
    #: header count filters on. Empty for the overwhelming majority of findings, which is why it
    #: sits here rather than as a status on every `Angle`. One short list per finding costs a
    #: fraction of one extra string per mutant, and it is already the question the filter asks.
    rare: list[str] = field(default_factory=list)
    angles: list[Angle] = field(default_factory=list)
    gap: str = ""
    risk: str = ""
    start: str = ""
    cls: str = ""
    tag: str = ""


@dataclass
class FileView:
    """One source file: its lines, its findings, and its own tally for the index.

    ``stamp`` digests this file's findings **and their outcomes**. A done-mark records the stamp it
    was made under, so the page can tell "you marked this against the report you are looking at"
    from "you marked this against an older run of this file". Per file, not per report: adding a
    second file to a run must not cast doubt on marks made against the first.
    """

    path: str
    lines: list[str]
    findings: list[Finding]
    ops: list[tuple[str, int]]
    detected: int
    survived: int
    total: int
    score: float | None
    stamp: str


@dataclass
class ReportView:
    """Everything the inlined renderer reads. Totals are **mutant**-based, matching the mutation
    score's definition; only the work list is grouped into findings."""

    files: list[FileView]
    refs: dict[str, list[tuple[str, str]]]
    docBase: str
    detected: int
    survived: int
    total: int
    score: float | None
    #: ``(label, count, status)`` per non-zero rare status. The status rides along because the
    #: header renders each count as a filter button that has to name what it filters on.
    rare: list[tuple[str, int, str]]


def change_note(operator_id: str, original: str, replacement: str) -> str:
    """A plain one-line rendering of what a mutant changed.

    Deletion operators have no meaningful replacement, so they read as a removal rather than an
    ``x -> y``: deriving the phrasing from the replacement alone produced "replaced it with pass"
    for a deleted line and a dangling "replaced it with " for a dropped ``not``.
    """
    if operator_id == "statement-deletion":
        return "This whole line was removed"
    if replacement == "":
        return f"Removed {original}"
    return f"Changed {original} to {replacement}"


def _adjust_col(line: str, column: int) -> int:
    """`column` (1-based, in source characters) as a column in the tab-expanded line the page shows.

    Reuses `explain`'s tab expansion so the HTML marker and the console caret can never land on
    different columns.
    """
    return _display_col(line[: column - 1]) + 1


def _enclosing_func(lines: list[str], line_no: int) -> str:
    """The name of the ``func`` enclosing 1-based `line_no`, or ``""``.

    Kept as its own scan rather than reusing `explain._enclosing_func`: that one reads the *raw*
    source lines, while the view holds tab-expanded copies, and it returns ``None`` where the view
    wants a plain empty string.
    """
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        match = re.match(r"\s*(?:static\s+)?func\s+(\w+)", lines[i])
        if match:
            return match.group(1)
    return ""


def _narrative(mutant: dict[str, Any]) -> tuple[str, str, str]:
    """A survivor's ``(gap, risk, start)`` copy, read back out of the report's own fields.

    `report.stryker_report` writes `explain.survivor_report_fields` into ``description`` (the gap)
    and ``statusReason`` (the risk and the start, blank-line separated). Reading it back keeps the
    page's words identical to the console's, which is the point — re-authoring report copy here is
    exactly the drift that once put "every test still passed" on a killed mutant.
    """
    parts = [p.strip() for p in str(mutant.get("statusReason", "")).split("\n\n") if p.strip()]
    return (
        str(mutant.get("description", "")),
        parts[0] if parts else "",
        parts[1] if len(parts) > 1 else "",
    )


def finding_key(path: str, fid: str) -> str:
    """A finding's address across the whole report: ``path:line:col:colEnd:operator``.

    The page builds this same string in JavaScript (one place, `keyOf`) rather than the generator
    storing it per finding — a full path repeated on each of a few thousand findings is pure weight
    in a file meant to be mailed. Parsing splits from the *right*, because a path may itself contain
    ``:`` (``res://scripts/a.gd``, ``C:/src/a.gd``) while the four trailing fields never can.
    """
    return f"{path}:{fid}"


def _stamp(path: str, findings: list[Finding]) -> str:
    """A short digest of a file's findings *and their outcomes*.

    Changes exactly when this file's result set changes — a finding appearing, vanishing, or
    flipping between surviving and caught. That is the question a done-mark has to answer to be
    trustworthy: "is this still the run I marked?" A timestamp would answer "no" on every
    regeneration, including one that changed nothing, so the digest is over content only and
    `render_html` stays deterministic.
    """
    body = "\n".join(f"{finding.fid}|{finding.cls}" for finding in findings)
    return hashlib.sha256(f"{path}\n{body}".encode()).hexdigest()[:12]


def _findings(
    mutants: list[dict[str, Any]], raw_lines: list[str], lines: list[str]
) -> list[Finding]:
    """Group `mutants` into findings, keyed by ``(line, span, operator)``.

    Never across operators: two operators may touch overlapping spans and still be different gaps.
    `raw_lines` are the source's own lines (whose tabs the report's columns are counted against),
    `lines` their tab-expanded twins (what the page draws).

    That grouping key *is* the finding's id, so no two findings in a file can share one and a
    regenerated report reproduces it exactly.
    """
    findings: list[Finding] = []
    by_key: dict[tuple[int, int, int, str], Finding] = {}
    for mutant in mutants:
        start, end = mutant["location"]["start"], mutant["location"]["end"]
        line_no = int(start["line"])
        on_file = 0 < line_no <= len(lines)
        src = lines[line_no - 1] if on_file else ""
        raw = raw_lines[line_no - 1] if on_file else ""
        col = _adjust_col(raw, int(start["column"]))
        col_end = _adjust_col(raw, int(end["column"]))
        a = max(0, col - 1)
        b = max(col_end - 1, a + 1)
        original = src[a:b]
        operator = str(mutant["mutatorName"])
        key = (line_no, col, col_end, operator)
        finding = by_key.get(key)
        if finding is None:
            finding = Finding(
                fid=f"{line_no}:{col}:{col_end}:{operator}",
                line=line_no,
                col=col,
                colEnd=col_end,
                op=operator,
                func=_enclosing_func(lines, line_no),
                # The *raw* lines and the report's own (raw) start column, so the shared rule
                # sees exactly what the console saw — the tab-expanded copies the page draws would
                # shift every column past a tab.
                ref=context_section(original, line_no, int(start["column"]), raw_lines) or operator,
            )
            by_key[key] = finding
            findings.append(finding)
        status = str(mutant["status"])
        # Recorded on the finding so a clicked header count can find the findings behind it. A
        # list deduped with `not in`, not a set, so the order is the mutants' own and the rendered
        # page is byte-identical run to run.
        if status in _RARE_STATUSES and status not in finding.rare:
            finding.rare.append(status)
        tag, cls, outcome = _OUTCOME.get(status, (status.lower(), "ot", status))
        finding.angles.append(
            Angle(
                change=change_note(operator, original, str(mutant.get("replacement", ""))),
                tag=tag,
                cls=cls,
                outcome=outcome,
            )
        )
        # The narrative comes from the first angle that *has* one — which is the first surviving
        # angle, since only survivors carry it. Taking it from the first angle unconditionally lost
        # the copy whenever a killed mutant happened to be grouped ahead of a surviving one on the
        # same token (`0 -> 1` killed, `0 -> -1` survived), leaving a finding tagged SURVIVED with
        # nothing to say.
        if not finding.gap:
            finding.gap, finding.risk, finding.start = _narrative(mutant)
    for finding in findings:
        classes = {a.cls for a in finding.angles}
        # Any surviving angle makes the whole finding actionable. All-caught is green. A finding
        # with a mix is the informative case: part of the boundary is tested and part is not.
        finding.cls = "sv" if "sv" in classes else ("kd" if classes == {"kd"} else "ot")
        finding.tag = finding.angles[0].tag if len(classes) == 1 else "mixed"
    return findings


def _score(detected: int, survived: int) -> float | None:
    """``detected / (detected + survived)`` as a percentage, or ``None`` with nothing killable —
    the same formula (and the same ``None``) as `MutationRun.mutation_score`."""
    scored = detected + survived
    return round(100 * detected / scored, 1) if scored else None


def _render_inline_markdown(text: str) -> str:
    """Escape `text`, then re-apply the only two inline markers the reference page uses."""
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def _display_path(path: str, project_dir: str | None) -> str:
    """`path` as the report shows it: relative to `project_dir` when it sits inside it.

    A report is made to travel. An absolute path is the author's own machine, their username and
    their directory layout, carried into every row of an artifact meant to be mailed or attached to
    a review, and it is noise to every reader but the one who produced it. It is also unreadable in
    bulk: sixty identical leading characters before the part that distinguishes one row from the
    next, in the one column a reader scans.

    A file outside the project keeps its absolute path, and that is deliberate rather than a gap.
    There is no shorter honest name for it, and a `..`-laden relative path would be worse on both
    counts. `loop.SourceOutsideProject` already refuses that arrangement for parallel runs, so the
    case is rare and is the one place a full path is genuinely the answer. Windows' separate drives
    land here too, because `relative_to` raises `ValueError` for them just as it does for a file one
    directory up.

    With no `project_dir` at all (a report rendered by something other than the CLI) the path is
    passed through unchanged, separators normalised.
    """
    if project_dir is not None:
        try:
            return Path(path).resolve().relative_to(Path(project_dir).resolve()).as_posix()
        except ValueError:
            pass
    return path.replace("\\", "/")


def report_view(report: dict[str, Any], project_dir: str | None = None) -> ReportView:
    """Build the typed view model the inlined renderer reads, from a ``mutation-testing-elements``
    report dict. `project_dir` is the project root the CLI ran against, used to shorten displayed
    paths (`_display_path`); without it paths are shown exactly as the report keys them."""
    files: list[FileView] = []
    counts: dict[str, int] = {}
    for path, entry in report.get("files", {}).items():
        raw_lines = str(entry.get("source", "")).split("\n")
        lines = [line.expandtabs(4) for line in raw_lines]
        mutants = list(entry.get("mutants", []))
        findings = _findings(mutants, raw_lines, lines)
        per_file: dict[str, int] = {}
        for mutant in mutants:
            status = str(mutant["status"])
            per_file[status] = per_file.get(status, 0) + 1
            counts[status] = counts.get(status, 0) + 1
        detected = sum(v for k, v in per_file.items() if k in _DETECTED)
        survived = per_file.get("Survived", 0)
        ops: dict[str, int] = {}
        for finding in findings:
            ops[finding.op] = ops.get(finding.op, 0) + 1
        shown_path = _display_path(path, project_dir)
        files.append(
            FileView(
                path=shown_path,
                lines=lines,
                findings=findings,
                ops=sorted(ops.items()),
                detected=detected,
                survived=survived,
                total=len(mutants),
                score=_score(detected, survived),
                # Stamped from the *displayed* path, the one done-marks are keyed by, so a Windows
                # run and a POSIX run of the same project agree.
                stamp=_stamp(shown_path, findings),
            )
        )
    # Most actionable first: the file with the most survivors is where the reader should start.
    files.sort(key=lambda f: (-f.survived, f.path))
    detected = sum(v for k, v in counts.items() if k in _DETECTED)
    survived = counts.get("Survived", 0)
    present = {f.ref for file in files for f in file.findings}
    return ReportView(
        files=files,
        docBase=DOC_BASE_URL,
        refs={
            op: [(label, _render_inline_markdown(body)) for label, body in sections]
            for op, sections in SURVIVOR_REFERENCE.items()
            if op in present
        },
        detected=detected,
        survived=survived,
        total=sum(file.total for file in files),
        score=_score(detected, survived),
        rare=[(label, counts[status], status) for label, status in _RARE if counts.get(status)],
    )


# --------------------------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------------------------

_CSS = """
*{box-sizing:border-box}
:root{
  --bg:#f6f5f1; --bg-elevated:#fefefd; --surface:#efeee8; --surface-2:#e6e4db;
  --border:#dcdccf; --border-strong:#b4b3a1;
  --text:#1b1c1a; --text-muted:#4f5249; --text-faint:#66695f;
  --accent:#5b21b6; --accent-soft:#efe7fb; --accent-border:#cdb8f2;
  --danger:#a4152f; --danger-soft:#fbe6ea; --danger-border:#eeb9c4;
  --good:#14532d; --good-soft:#e3f2e7;
  --mono: ui-monospace,"Cascadia Code","JetBrains Mono",Consolas,"Liberation Mono",Menlo,monospace;
  --sans: -apple-system,BlinkMacSystemFont,"Segoe UI Variable","Segoe UI",Roboto,Helvetica,
    Arial,sans-serif;
  --serif: "Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  --radius:12px; color-scheme:light dark;
}
[data-theme="dark"]{
  --bg:#08090b; --bg-elevated:#0d0f12; --surface:#121418; --surface-2:#171a1f;
  --border:#22262c; --border-strong:#454c56;
  --text:#e7e9ed; --text-muted:#a6aebc; --text-faint:#949cab;
  --accent:#c4b5fd; --accent-soft:rgba(167,139,250,.16); --accent-border:rgba(167,139,250,.45);
  --danger:#fda4af; --danger-soft:rgba(251,113,133,.12); --danger-border:rgba(251,113,133,.42);
  --good:#6ee7b7; --good-soft:rgba(52,211,153,.12);
}
/* Ligatures OFF DOCUMENT-WIDE, inherited by everything. Scoping this to the code pane was a real
   bug: the detail card's diff rendered the replacement `<=` as a single glyph, hiding the very
   operator the mutation is about. Nothing in a mutation report may ever ligature. */
html,body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
  font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0,"dlig" 0}
code,pre,.code,.mono{font-family:var(--mono)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1400px;margin:0 auto;padding:20px 24px 80px}

/* ---- masthead ---- */
.mast{display:flex;align-items:center;gap:14px;margin-bottom:18px}
.frank{width:44px;height:44px;flex:none}
.mast h1{font:600 19px/1.2 var(--sans);margin:0;letter-spacing:-.01em}
.mast p{margin:3px 0 0;font-size:12.5px;color:var(--text-muted);line-height:1.45}
.mast .acts{margin-left:auto;display:flex;align-items:center;gap:8px}

/* ---- header: sparse by design. No score bar. ---- */
.head{display:flex;align-items:flex-end;gap:34px;flex-wrap:wrap;
  padding-bottom:18px;border-bottom:1px solid var(--border);margin-bottom:18px}
.score{font-family:var(--serif);font-size:64px;font-weight:500;line-height:1;letter-spacing:-.02em}
.score .pct{font-size:30px;color:var(--text-muted);margin-left:2px}
.score .cap{display:block;font:400 12px/1.5 var(--sans);color:var(--text-muted);
  letter-spacing:.02em;margin-top:8px}
.stat{display:flex;flex-direction:column;gap:3px}
.stat b{font-family:var(--serif);font-size:30px;font-weight:500;line-height:1}
.stat i{font-style:normal;font-size:11px;color:var(--text-muted);text-transform:uppercase;
  letter-spacing:.1em}
.stat.sv b{color:var(--danger)} .stat.kd b{color:var(--good)}
/* A rare-status count that is a FILTER, not a label. It has to keep the stat block's exact look.
   The header is a row of numbers, and one of them suddenly wearing a chip's chrome would read as a
   different kind of thing, so the button is reset to nothing and earns its affordance from the
   pointer, the hover, and the pressed state. */
.stat.jump{background:none;border:0;padding:0;margin:0;font-family:inherit;text-align:left;
  cursor:pointer;border-bottom:1px dashed var(--border-strong);align-self:stretch}
.stat.jump:hover{border-bottom-color:var(--accent)}
.stat.jump:hover i{color:var(--text)}
.stat.jump[aria-pressed="true"]{border-bottom:2px solid var(--accent)}
.stat.jump[aria-pressed="true"] b,.stat.jump[aria-pressed="true"] i{color:var(--accent)}
.tbtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  border-radius:8px;width:32px;height:32px;cursor:pointer;font-size:14px;flex:none}

/* ---- file index ---- */
.crumb{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.crumb code{font-size:13px;color:var(--text-muted)}
.back{background:var(--surface);border:1px solid var(--border);color:var(--text);
  border-radius:999px;padding:5px 12px;font:500 12px/1 var(--mono);cursor:pointer}
.back:hover{background:var(--surface-2)}
.files{background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius);
  overflow:hidden}
.frow{display:grid;grid-template-columns:96px 1fr repeat(3,72px);align-items:center;gap:10px;
  width:100%;text-align:left;background:none;border:0;border-top:1px solid var(--border);
  padding:12px 16px;cursor:pointer;color:var(--text);font-family:inherit}
.frow:first-child{border-top:0}
.frow:hover{background:var(--surface)}
.frow .fsc{font-family:var(--serif);font-size:26px;line-height:1}
.frow .fpath{font:500 13px/1.4 var(--mono);word-break:break-all}
.frow .fn{font:500 13px/1 var(--mono);text-align:right}
.frow .fn.sv{color:var(--danger)} .frow .fn.kd{color:var(--good)}
.fhead{display:grid;grid-template-columns:96px 1fr repeat(3,72px);gap:10px;padding:10px 16px;
  background:var(--surface);font:600 10.5px/1 var(--mono);text-transform:uppercase;
  letter-spacing:.1em;color:var(--text-muted)}
/* Every heading is a real <button>, so re-sorting is reachable by Tab and Enter and not only by
   mouse. Reset to inherit the row's own type, because a heading that changes size or weight when
   it becomes clickable makes the table look like it moved. */
.fhead button{background:none;border:0;padding:0;margin:0;cursor:pointer;font:inherit;
  letter-spacing:inherit;text-transform:inherit;color:inherit;text-align:left}
.fhead button:nth-child(n+3){text-align:right}
.fhead button:hover{color:var(--text)}
.fhead button[aria-pressed="true"]{color:var(--accent)}
/* The direction marker. Only the sorted column has one, so the arrow itself says which column is
   in charge, with no second highlight needed to carry that. */
.fhead .dir{margin-left:4px}

/* ---- controls ---- */
.bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:var(--surface);border:1px solid var(--border);color:var(--text-muted);
  border-radius:999px;padding:5px 12px;font:500 12px/1 var(--mono);cursor:pointer}
.chip[aria-pressed="true"]{background:var(--accent-soft);border-color:var(--accent-border);
  color:var(--accent)}
.stepper{margin-left:8px;display:flex;align-items:center;gap:6px;
  background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:4px 6px}
.stepper button{background:none;border:0;color:var(--text);cursor:pointer;font-size:17px;
  width:30px;height:26px;border-radius:999px}
.stepper button:hover:not(:disabled){background:var(--surface-2)}
.stepper button:disabled{opacity:.45;cursor:default}
.stepper .pos{font:600 12px/1 var(--mono);color:var(--text-muted);min-width:118px;text-align:center}
/* Progress through the list, kept clear of the stepper's position so the two counts are never
   read as one number. It takes over the `margin-left:auto` that used to push the stepper right. */
.done{margin-left:auto;font:500 11.5px/1 var(--mono);color:var(--text-muted)}

/* ---- legend: the marks mean nothing without it ---- */
.legend{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:14px;
  font-size:11.5px;color:var(--text-muted)}
.legend b{font-weight:400}
.legend .sw{font-family:var(--mono);border-radius:3px;padding:0 4px;margin-right:5px}
.legend .sw.sv{background:var(--danger-soft);box-shadow:0 0 0 1px var(--danger-border)}
.legend .sw.kd{background:var(--good-soft);box-shadow:0 0 0 1px var(--good)}
.legend .sw.ot{background:var(--surface-2);box-shadow:0 0 0 1px var(--border-strong)}
.legend .sw.multi{background:var(--danger-soft);outline:2px dashed var(--danger);outline-offset:0}
.legend code{font-size:11px;background:var(--surface-2);border-radius:3px;padding:0 3px}

/* ---- source ---- */
.panes{display:grid;gap:20px;grid-template-columns:minmax(0,1.55fr) minmax(320px,1fr)}
.src{background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius);
  overflow:auto}
/* ONE scroll container for the whole pane, never one per line: per-row `overflow-x` let the lines
   shear independently, so the caret row slid out from under the token it points at. */
.rows{min-width:max-content}
.row{display:grid;grid-template-columns:52px 1fr;font-family:var(--mono);font-size:13px;
  line-height:22px}
.row:hover{background:var(--surface)}
.ln{color:var(--text-faint);text-align:right;padding-right:12px;user-select:none;
  border-right:1px solid var(--border);font-size:11.5px}
.ln.hit{color:var(--text);font-weight:700}
.code{position:relative;padding-left:12px;white-space:pre}
/* The MARKED CHARACTER is the target: an overlaid dot covers the very token it points at, so the
   mutated span itself gets the tint and the click handler. Zero horizontal padding — any would
   shift the monospace grid the caret row aligns against.
   BACKGROUND ONLY, never an underline: an underline tight beneath `<` or `>` fuses with the glyph
   and reads as `<=` / `>=`, disguising the exact operator the mutation is about. Rings and
   outlines sit outside the glyph box, so they are safe; anything touching the baseline is not. */
.mark{--tone:var(--danger);--tone-soft:var(--danger-soft);--tone-border:var(--danger-border);
  font:inherit;color:inherit;line-height:inherit;white-space:pre;border:0;margin:0;padding:0;
  cursor:pointer;border-radius:3px;background:var(--tone-soft);position:relative;
  box-shadow:0 0 0 1px var(--tone-border)}
.mark.kd{--tone:var(--good);--tone-soft:var(--good-soft);--tone-border:var(--good)}
/* Neither survived nor caught (ignored / invalid / errored): muted, never green. Green would claim
   a test caught something that was suppressed or never even parsed. */
.mark.ot{--tone:var(--text-muted);--tone-soft:var(--surface-2);--tone-border:var(--border-strong)}
/* Hover strengthens the mark's OWN tone. Fading every mark to neutral grey on hover briefly
   claimed the tone reserved for ignored/invalid. */
.mark:hover{box-shadow:0 0 0 2px var(--tone)}
/* SEVERAL findings on one token. The previous treatment — a 1px dashed outline at 1px offset —
   was invisible in practice, and measured so in Chrome on the corpus fixture's one multi mark
   (`return 0`, `numeric` nested inside `statement-deletion`): the outline computed to
   `1px dashed rgb(20,83,45)` sitting immediately outside a solid ring of the *identical* colour,
   so the two read as one slightly thick edge. A 1px dash is barely a dash at that size anyway.
   The replacement states the number instead of hinting at it. The ring goes dashed and doubles in
   weight, REPLACING the solid one rather than sitting beside it, and a small count badge rides the
   corner. The ring is an outline and the badge is absolutely positioned, so neither takes layout
   space, shifts the monospace grid, or touches the baseline the caret row aligns to. */
.mark.multi{box-shadow:none;outline:2px dashed var(--tone);outline-offset:0}
.mark.multi::after{content:attr(data-n);position:absolute;top:-5px;right:-6px;
  font:700 9px/1 var(--sans);color:var(--bg-elevated);background:var(--tone);
  border-radius:999px;padding:1.5px 3.5px;pointer-events:none}
.mark.on{background:var(--accent-soft);box-shadow:0 0 0 2px var(--accent)}
/* Handled: faded, so the eye skips it — but never hidden, and never recoloured to green, which
   would claim a test now catches it. Opacity only; a strikethrough would touch the baseline and
   fuse with `<` / `>`, the exact thing this page must never do to an operator. */
.mark.done{opacity:.45}
/* Marked done under an EARLIER run and still surviving. Loud on purpose: a stale tick hiding a
   live survivor is the one failure these marks could introduce, so it is never quiet. Dotted, and
   outside the glyph box like the other outlines here. */
.mark.recheck{opacity:1;outline:2px dotted var(--danger);outline-offset:2px}

/* The caret row: a triangle that pops out under the exact token, the HTML echo of the `^` the
   console prints. Explicit height: both cells hold only absolutely-positioned children, so without
   it the grid row collapses to 0 and the triangle lands on the next line of code. */
.caret-row{display:grid;grid-template-columns:52px 1fr;font-family:var(--mono);font-size:13px;
  line-height:20px;background:var(--accent-soft)}
.caret-row .ln{border-right:1px solid var(--border)}
.caret-row .car{position:relative;padding-left:12px;white-space:pre;color:var(--accent)}
/* No margin offset: the triangle glyph is centred inside its own 1ch advance, so putting its left
   edge on the character cell's left edge already centres it under the token. */
.caret-row .tri{position:absolute;top:-1px;font-size:13px;line-height:20px}
/* MUST stay at the code's 13px: `left` is in `ch`, and `ch` resolves against the element's OWN
   font-size. At 11.5px its `ch` is narrower than the code grid's, so the text crept left by an
   amount that grew with the column. The smaller text lives in a nested span instead. */
.caret-row .say{position:absolute;top:0;font-size:13px;color:var(--text-muted);white-space:nowrap}
.caret-row .say i{font-style:normal;font-size:11.5px;line-height:20px;display:inline-block}
.caret-row .say em{font-style:normal;color:var(--text-muted);letter-spacing:.06em;font-size:11px}
.row.active{background:var(--accent-soft)}

/* ---- detail card ---- */
.aside{align-self:start;position:sticky;top:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 18px}
.card .who{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:12px}
.tag{font:600 10.5px/1 var(--mono);text-transform:uppercase;letter-spacing:.1em;
  padding:4px 8px;border-radius:6px;border:1px solid}
.tag.sv{color:var(--danger);background:var(--danger-soft);border-color:var(--danger-border)}
.tag.kd{color:var(--good);background:var(--good-soft);border-color:transparent}
.tag.ot{color:var(--text-muted);background:var(--surface-2);border-color:var(--border-strong)}
.tag.op{color:var(--text-muted);background:var(--bg-elevated);border-color:var(--border)}
.who .loc{font:500 12px/1 var(--mono);color:var(--text-muted)}
.diff{font-family:var(--mono);font-size:12.5px;background:var(--bg-elevated);
  border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:14px;
  white-space:pre-wrap;color:var(--text-muted)}
.diff b{color:var(--text);font-weight:700}
.f{display:grid;grid-template-columns:44px 1fr;gap:10px;margin-bottom:11px}
.f dt{font:700 10.5px/1.7 var(--mono);text-transform:uppercase;letter-spacing:.1em;
  color:var(--text-muted)}
.f dd{margin:0;font-size:13.5px;line-height:1.62;color:var(--text)}
.f.start dt{color:var(--accent)}
.card a{color:var(--accent);font-size:12.5px;font-family:var(--mono)}
.tag.re{color:var(--danger);background:var(--danger-soft);border-color:var(--danger-border)}
/* The done toggle. Quiet until pressed; loud only in the carried-over state. */
.donebtn{display:block;width:100%;text-align:left;margin:0 0 10px;cursor:pointer;
  background:var(--bg-elevated);border:1px solid var(--border);color:var(--text-muted);
  border-radius:8px;padding:7px 10px;font:500 12px/1.35 var(--mono)}
.donebtn:hover{background:var(--surface-2)}
.donebtn.done{background:var(--good-soft);border-color:var(--good);color:var(--good)}
.donebtn.recheck{background:var(--danger-soft);border-color:var(--danger-border);
  color:var(--danger)}
/* Inline reference: expands in place rather than navigating away mid-triage. */
.refbtn{background:none;border:0;padding:0;cursor:pointer;color:var(--accent);
  font:500 12.5px var(--mono);text-align:left}
.refbtn:hover{text-decoration:underline}
.ref{margin-top:10px;padding:12px 14px;background:var(--bg-elevated);
  border:1px solid var(--border);border-radius:8px}
.ref .f{grid-template-columns:112px 1fr;margin-bottom:8px}
.ref .f dt{color:var(--text-muted);text-transform:none;letter-spacing:0;font-size:11px}
.ref .f dd{font-size:12.5px;line-height:1.55;color:var(--text-muted)}
.ref code{background:var(--surface-2);border-radius:3px;padding:0 3px;font-size:11.5px}
.ref strong{color:var(--text)}
.inline-host{grid-column:1 / -1;padding:4px 12px 16px 12px}
.empty{color:var(--text-muted);font-size:13px;padding:18px}
.note{font-size:11.5px;color:var(--text-muted);margin:0 0 14px;line-height:1.5}

/* Below this the side panel has no room. Rather than stacking it far below the fold — where a
   click gives no visible feedback at all — the detail opens INLINE under the active line, and the
   side pane is dropped. Same renderer, one layout switch. */
@media (max-width:900px){
  .panes{grid-template-columns:1fr}
  .aside{display:none}
}
"""

_JS = r"""
const D = DATA_JSON;
const $ = s => document.querySelector(s);
// Below this width the detail card renders inline under the active line instead of in the side
// pane; the query is live, so a resize re-lays-out into the right shape.
const NARROW = window.matchMedia('(max-width: 900px)');
const MULTI = D.files.length > 1;
// Above this many files the index gets a filter box. Below it, scanning beats typing.
const SEARCH_FROM = 8;

let cur = 0, sel = null, filter = 'survived', op = 'all', refOpen = null, query = '';
let view = MULTI ? 'index' : 'file';
// The file index's order. The default is the generator's own, most survivors first, and it stays
// the default deliberately: it is the only order that answers "where do I start". Score would be
// worse, because 1 survivor in 5 mutants and 100 in 500 both read 80%.
let sortBy = 'survived', sortDesc = true;
// What the source pane currently has painted. The pane is rebuilt only when this changes —
// selection moves repaint nothing but the few nodes that actually differ.
let painted = null;
let caretEl = null, hostEl = null;

const file = () => D.files[cur];
const isSv = f => f.cls === 'sv';

// ---- stable finding identity -----------------------------------------------------------------
//
// `f.fid` is the tuple the generator grouped by — `line:col:colEnd:operator` — so it is unique
// within a file and identical every time the report is regenerated from source that has not moved.
// Joined to the path it addresses a finding across the whole report. That one primitive is what
// both the URL fragment and the done-marks are built on; nothing else here needs a new id.
const keyOf = f => file().path + ':' + f.fid;
// Split from the RIGHT: a path may contain `:` (`res://a.gd`, `C:/src/a.gd`), the four trailing
// fields never can. `.*` is greedy, so the last legal split wins.
const KEY_RE = /^(.*):(\d+:\d+:\d+:[^:]+)$/;
// A `rare:<Status>` filter comes from a header count: "show me the 204 runtime errors". It is the
// one filter that is a question about the WHOLE REPORT rather than about the file on screen, which
// is why clicking one may move you to another file (see `onClick`).
const RARE = 'rare:';
function matches(f){
  const ok = filter === 'all' ? true
    : filter.indexOf(RARE) === 0 ? f.rare.indexOf(filter.slice(RARE.length)) >= 0
    : filter === 'survived' ? isSv(f)
    : f.cls === 'kd';
  return ok && (op === 'all' || f.op === op);
}
// Takes the file explicitly, so "does any OTHER file match?" is answerable without moving `cur`
// there first, which is what a header count clicked from the index has to ask.
const shownIn = f => f.findings.filter(matches);
// The stepper walks exactly what is on screen. Making it walk survivors while the source showed
// everything meant selecting a caught mark reported "- of 6" — a live selection the stepper said
// did not exist.
const shown = () => shownIn(file());

// Escapes for BOTH text and attribute contexts: a quote inside a GDScript token would otherwise
// close the `title="..."` it lands in.
function esc(s){
  return (s == null ? '' : String(s)).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function plural(n, word){ return n + ' ' + word + (n === 1 ? '' : 's'); }
// Built here rather than stored per finding: the same anchor `explain.doc_url` builds, and one
// copy instead of one per finding (which cost ~240 KB on a 2851-mutant directory run).
function docUrl(f){ return D.docBase + '#' + f.ref; }

// ---- the address bar -------------------------------------------------------------------------
//
// The selected (file, finding) is mirrored into `location.hash`, so a reload keeps your place and
// "look at this survivor" is a link you can paste into a review. Only the three characters that
// would actually break a fragment are escaped — the hash stays readable, which is the point of
// having one.
function encFrag(s){ return s.replace(/%/g, '%25').replace(/#/g, '%23').replace(/ /g, '%20'); }
function decFrag(s){ try { return decodeURIComponent(s); } catch (e) { return s; } }

// Resolve the current hash against THIS report. Everything here can fail — the link may name a
// file this run did not cover, or a finding whose line has moved since — and every failure returns
// null, which the caller turns into the ordinary default view. A stale link never errors and never
// lands on the wrong finding: an id that does not match exactly does not match at all.
function fromHash(){
  const raw = decFrag(String(location.hash || '').replace(/^#/, ''));
  if (!raw) return null;
  const m = KEY_RE.exec(raw);
  const at = D.files.findIndex(f => f.path === (m ? m[1] : raw));
  if (at < 0) return null;
  const found = m ? D.files[at].findings.find(f => f.fid === m[2]) : null;
  // A hash that names a finding the report no longer has resolves to the FILE it named, not to
  // nothing: the file is still the best answer to "where was I".
  return { at, finding: found || null };
}

let lastHash = null;    // what WE last put in the address bar, as the browser reports it back

// PUSH ON STRUCTURAL MOVES, REPLACE ON STEPPING. That is the whole of this page's history policy.
//
// Opening a file from the index, and coming back from it, are the moves a reader means when they
// reach for the browser's back button; those get a real history entry, so back means "back to the
// file list" and agrees with the page's own `all files` button. Stepping between findings does
// not, because 197 findings would otherwise become 197 back-presses standing between the reader
// and wherever they came from. A report that hijacks the back button is worse than one that
// ignores it, which is why every move used to replace.
//
// Set by the moves that are structural (`.frow`, `toIndex`, `toFirstMatch`) and consumed by the
// very next `syncHash`. A single-file report has no index and therefore no structural move at all,
// so nothing there ever sets it and its history behaves exactly as it did before.
let pushEntry = false;

function syncHash(){
  const want = view === 'file' ? '#' + encFrag(sel ? keyOf(sel) : file().path) : '';
  const push = pushEntry;
  pushEntry = false;             // consumed here whether or not the address actually moves
  // No change of address, no entry: pushing a duplicate would give the reader a back press that
  // visibly does nothing.
  if (want === lastHash) return;
  lastHash = want;
  // Some browsers refuse either call on a `file://` page, so fall back to the assignment. That
  // pushes, which is right for the structural case and the same compromise as before for the other.
  try {
    const url = want || location.pathname + location.search;
    if (push) history.pushState(null, '', url); else history.replaceState(null, '', url);
  }
  catch (e) { location.hash = want; }
  // Browsers may normalise what they stored. Remember THAT, so the hashchange guard compares like
  // with like rather than re-opening the page against its own write.
  lastHash = String(location.hash || '');
}

// ---- done marks ------------------------------------------------------------------------------
//
// SCOPED PER REPORT FILE, not per run. The triage loop is "mark a batch done, write tests, re-run,
// regenerate over the same --html path", so marks that evaporated on every regeneration would
// vanish exactly when they start being worth something. Keying on the report's own location gets
// that for free, and gets the other half too: a copy that travels — mailed, downloaded, archived —
// opens on a different path and therefore opens UNMARKED, rather than showing a stranger's
// progress. Losing marks is the safe failure here; inheriting someone else's is not.
//
// The danger this is designed against is a stale tick hiding a live survivor. So each mark records
// the file's `stamp` (a digest of its findings and their outcomes). A mark made under an older
// stamp on a finding that is STILL SURVIVING is shown as "re-check", styled apart, and is NOT
// counted as done — the count never claims progress the run does not support. Marks never filter
// or hide a finding, and the header's survivor total is always the run's own number.
const STORE = 'gdmutant.done.v1:' + String(location.href || '').split('#')[0].split('?')[0];
let marks = {};
function loadMarks(){
  // localStorage can be absent, disabled, full, or hold something another version wrote. None of
  // that is worth an error on a report someone opened to read: fall back to unmarked.
  try { marks = JSON.parse(localStorage.getItem(STORE) || '{}'); } catch (e) { marks = null; }
  if (!marks || typeof marks !== 'object' || Array.isArray(marks)) marks = {};
}
function saveMarks(){
  try { localStorage.setItem(STORE, JSON.stringify(marks)); } catch (e) { /* session only */ }
}
// '' · 'done' (marked under the stamp on screen) · 'recheck' (marked earlier, still surviving).
function markState(f){
  const at = marks[keyOf(f)];
  if (at === undefined) return '';
  return (at !== file().stamp && isSv(f)) ? 'recheck' : 'done';
}
function toggleDone(f){
  if (!f) return;
  // Toggling a carried-over mark re-affirms it against the run on screen; toggling again clears
  // it. So acknowledging a re-check is one keypress, and never a silent no-op.
  if (markState(f) === 'done') delete marks[keyOf(f)];
  else marks[keyOf(f)] = file().stamp;
  saveMarks();
  paintSelection();
}
function markSets(){
  const done = new Set(), recheck = new Set();
  file().findings.forEach(f => {
    const s = markState(f);
    if (s === 'done') done.add(f.fid); else if (s === 'recheck') recheck.add(f.fid);
  });
  return [done, recheck];
}

// ---- detail card ---------------------------------------------------------------------------

function cardHTML(f){
  // Prints only what the generator computed. No outcome is derived here — that is exactly how
  // "every test still passed" once ended up under a tag reading CAUGHT. One narrative, then one
  // line per angle; no aggregate sentence is invented over the angle set.
  const angles = f.angles.map(a => `<b>${esc(a.change)}</b> — ${esc(a.outcome)}.`).join('\n');
  const many = f.angles.length > 1
    ? `<span class="tag op">${plural(f.angles.length, 'change')} here</span>` : '';
  const state = markState(f);
  return `<div class="card">
    <div class="who">
      <span class="tag ${f.cls}">${esc(f.tag)}</span>
      <span class="tag op">${esc(f.op)}</span>
      ${many}
      ${state === 'recheck' ? '<span class="tag re">re-check</span>' : ''}
      <span class="loc">${esc(file().path)}:${f.line}${
        f.func ? '  ·  func ' + esc(f.func) : ''}</span>
    </div>
    <div class="diff">${esc((file().lines[f.line - 1] || '').trim())}
${angles}</div>
    ${f.gap ? `<dl class="f"><dt>gap</dt><dd>${esc(f.gap)}</dd></dl>` : ''}
    ${f.risk ? `<dl class="f"><dt>risk</dt><dd>${esc(f.risk)}</dd></dl>` : ''}
    ${// `start`, never "fix": `explain` names the missing INPUT and stops there, because the value
      // it should assert is the reader's to decide — a guess would codify a bug. Labelling it "fix"
      // promised a remedy the tool does not have.
      f.start ? `<dl class="f start"><dt>start</dt><dd>${esc(f.start)}</dd></dl>` : ''}
    ${doneHTML(f, state)}
    ${refHTML(f)}
  </div>`;
}

// The done control. A mark is progress through a list, not a verdict on the code, so it never
// changes the finding's tag and never removes it from anything.
function doneHTML(f, state){
  const label = state === 'done' ? '&#10003; done'
    : state === 'recheck' ? '&#10003; done earlier — still surviving, re-check'
    : 'mark done';
  return `<button class="donebtn ${state}" data-done="1" aria-pressed="${state === 'done'}"`
    + ` title="Toggle done (d)">${label}</button>`;
}

// The per-operator reference, inlined — it expands in place instead of navigating away, and
// offline a GitHub link resolves to nothing at all. Shown only on a finding with a surviving
// angle: "what a numeric survivor means" beside a CAUGHT tag contradicts the tag.
function refHTML(f){
  if (!isSv(f)) return '';
  const secs = D.refs[f.ref];
  if (!secs) return `<a href="${esc(docUrl(f))}">${esc(f.ref)} reference &rarr;</a>`;
  const open = refOpen === f.fid;
  return `<button class="refbtn" data-ref="${esc(f.fid)}" aria-expanded="${open}">`
    + `${open ? '&#9662;' : '&#9656;'} what this ${esc(f.ref)} survivor means</button>`
    + (open ? `<div class="ref">`
        + secs.map(([k, v]) => `<dl class="f"><dt>${esc(k)}</dt><dd>${v}</dd></dl>`).join('')
        + `<a href="${esc(docUrl(f))}">read it on GitHub &rarr;</a></div>` : '');
}

// ---- clicks ----------------------------------------------------------------------------------
//
// ONE delegated handler for every control in either view, wired ONCE to `#body`. `#body` is part
// of the page shell — the views replace its innerHTML, never the element — so nothing here is
// re-wired on a view switch, a filter change, or a repaint.
//
// This is not only about the 309-finding file (though per-element listeners on thousands of marks
// were already the wrong shape). It is about the failure that one-listener-per-control invites:
// the `#prev` / `#next` arrows shipped DRAWN, labelled, with their disabled state faithfully
// maintained by `paintStepper`, and never connected to `step()`. They were decorative. The
// keyboard path worked, so the harness — which pressed keys — saw nothing wrong. A control that
// exists in the markup now reaches its behaviour through this one function or not at all.
function onClick(e){
  const at = s => e.target.closest(s);

  const row = at('.frow');
  if (row) {
    cur = +row.dataset.file; sel = null; refOpen = null; view = 'file';
    pushEntry = true;                 // structural: the browser's back button returns to the index
    renderFile();
    return;
  }
  if (at('#back')) { toIndex(); return; }
  // A disabled <button> fires no click at all, so the clamp needs no guard here.
  if (at('#prev')) { step(-1); return; }
  if (at('#next')) { step(1); return; }

  // A column heading on the file index. The same one again flips the direction; a different one
  // takes over and starts at the end worth looking at: biggest numbers first for the counts, A to Z
  // for the path, which is the only column where "largest" means nothing.
  const head = at('[data-sort]');
  if (head) {
    const key = head.dataset.sort;
    if (sortBy === key) sortDesc = !sortDesc;
    else { sortBy = key; sortDesc = key !== 'file'; }
    paintFiles();
    return;
  }

  const chip = at('[data-filter]') || at('[data-op]');
  if (chip) {
    if (chip.dataset.filter !== undefined) filter = chip.dataset.filter;
    else op = chip.dataset.op;
    // A `rare:` filter came from a header count, which sits above BOTH views and counts the whole
    // report, so it can be clicked from the index, or on a file holding none of what it counts.
    // Either way it has to land where those mutants actually are. The in-file chips never move
    // you: filtering the file you are reading must not carry you off to a different one.
    const fromHeader = filter.indexOf(RARE) === 0;
    if (fromHeader && (view !== 'file' || !shown().length)) { toFirstMatch(); return; }
    keepSelection();
    refresh();
    // A header count is a request to SEE those mutants, so it opens one. Filtering to a list with
    // nothing selected and leaving the reader to press an arrow answers a question they did not
    // ask. The chips keep their own behaviour: they narrow what is already being read.
    if (fromHeader && !sel) step(1);
    return;
  }

  const ref = at('.refbtn');
  if (ref) {
    refOpen = refOpen === ref.dataset.ref ? null : ref.dataset.ref;   // toggle
    paintSelection();
    return;
  }
  if (at('.donebtn')) { toggleDone(sel); return; }

  const mark = at('.mark');
  if (mark) {
    const ids = mark.dataset.ids.split(',');
    // One finding: select it. Several sharing this token: cycle findings on repeat clicks.
    const i = sel ? ids.indexOf(sel.fid) : -1;
    pick(file().findings.find(x => x.fid === ids[i < 0 ? 0 : (i + 1) % ids.length]));
  }
}

// ---- the file index ------------------------------------------------------------------------

// The columns, in the order they are drawn, each with the value it sorts on. A file that could
// not be scored sorts below 0%: it is not a good result, it is an absent one, and floating it to
// the top of a score sort would put the least informative rows where the worst ones belong.
const COLS = [
  ['score', 'score', f => (f.score === null ? -1 : f.score)],
  ['file', 'file', f => f.path],
  ['survived', 'survived', f => f.survived],
  ['caught', 'caught', f => f.detected],
  ['mutants', 'mutants', f => f.total],
];

// `[file, its index in D.files]` pairs in the reader's chosen order. The index is carried through
// rather than recomputed, because it is what a clicked row reports back as `data-file`.
function sortedFiles(){
  const key = COLS.find(c => c[0] === sortBy)[2];
  const rows = D.files.map((f, i) => [f, i]);
  rows.sort(([a], [b]) => {
    const x = key(a), y = key(b);
    const c = x < y ? -1 : x > y ? 1 : 0;
    // The direction applies to the CHOSEN column only. Ties always break on the path ascending,
    // the same tie-break the generator uses, so the default state here reproduces the order the
    // report arrived in exactly, and equal rows never swap places between repaints.
    if (c) return sortDesc ? -c : c;
    return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
  });
  return rows;
}

function paintFiles(){
  const q = query.trim().toLowerCase();
  const rows = sortedFiles()
    .filter(([f]) => !q || f.path.toLowerCase().includes(q))
    .map(([f, i]) => `<button class="frow" data-file="${i}">
      <span class="fsc">${f.score === null ? '&ndash;'
        : f.score + '<span style="font-size:14px">%</span>'}</span>
      <span class="fpath">${esc(f.path)}</span>
      <span class="fn sv">${f.survived}</span>
      <span class="fn kd">${f.detected}</span>
      <span class="fn">${f.total}</span>
    </button>`).join('');
  const heads = COLS.map(([key, label]) => {
    const on = sortBy === key;
    return `<button type="button" data-sort="${key}" aria-pressed="${on}"`
      + ` title="Sort by ${label}">${label}`
      + (on ? `<span class="dir">${sortDesc ? '&#9662;' : '&#9652;'}</span>` : '')
      + `</button>`;
  }).join('');
  $('#filelist').innerHTML = `<div class="fhead">${heads}</div>`
    + (rows || '<div class="empty">No file matches that filter.</div>');
}

function renderIndex(){
  const box = D.files.length >= SEARCH_FROM
    ? `<input id="q" class="qbox" type="search" placeholder="filter files…"
         aria-label="Filter files by path">` : '';
  $('#body').innerHTML = `<p class="note">Most survivors first — that is where to start.
      Choose a file to work through its findings, or click a column heading to re-sort.</p>
    ${box}<div class="files" id="filelist"></div>`;
  const input = $('#q');
  if (input) { input.value = query; input.oninput = () => { query = input.value; paintFiles(); }; }
  paintFiles();
  // The header's rare-status counts are filters and live above this view too, so their pressed
  // state is painted here as well as in the file view.
  paintChips();
  syncHash();   // the index has no address of its own; drop whatever finding the URL still names
}

// Open the first file that holds something this filter shows. A header count is a claim about the
// whole report ("204 runtime errors"), so the click has to reach them wherever they are; with no
// file matching at all, stay put and let the file view say "no findings" rather than move for
// nothing.
function toFirstMatch(){
  const hit = D.files.findIndex(f => shownIn(f).length);
  // Structural only where it really moves you: out of the index, or across to a different file.
  // Re-filtering the file already on screen is not a navigation and must not cost a back press,
  // which is what keeps a single-file report's history exactly as it was.
  if (view !== 'file' || (hit >= 0 && hit !== cur)) pushEntry = true;
  if (hit >= 0) cur = hit;
  sel = null; refOpen = null; view = 'file';
  renderFile();
}

// ---- the source pane -----------------------------------------------------------------------

// What the painted source depends on. Selection is deliberately NOT part of it.
function sourceKey(){ return cur + '|' + filter + '|' + op; }

// What the pane last drew, so the legend can explain the marks that are on screen and nothing
// else. Reset on every repaint, filled while the marks are built.
let seen = {};

function paintSource(){
  const f = file();
  const vis = new Set(shown().map(x => x.fid));
  seen = { unscored: {} };
  let out = '';
  f.lines.forEach((text, i) => {
    const n = i + 1;
    const here = f.findings.filter(x => x.line === n && vis.has(x.fid));
    // A mark is a character range and may host more than one FINDING, when different operators
    // touch overlapping spans — on `return 0`, `numeric` sits inside `statement-deletion`.
    // Clicking cycles findings, never raw mutants, which would re-split a grouped finding.
    const groups = [];
    here.forEach(x => {
      const a = Math.max(0, x.col - 1), b = Math.max(a + 1, x.colEnd - 1);
      const hit = groups.find(g => !(b <= g.a || a >= g.b));
      if (hit) { hit.a = Math.min(hit.a, a); hit.b = Math.max(hit.b, b); hit.fs.push(x); }
      else groups.push({ a, b, fs: [x] });
    });
    groups.sort((x, y) => x.a - y.a);

    let body = '', pos = 0;
    groups.forEach(g => {
      if (g.a < pos) return;                      // pathological overlap: skip, never corrupt text
      body += esc(text.slice(pos, g.a));
      const cls = g.fs.some(isSv) ? 'sv' : (g.fs.every(x => x.cls === 'kd') ? 'kd' : 'ot');
      const tip = g.fs.map(x => x.op + ': '
        + x.angles.map(a => a.change + ' (' + a.tag + ')').join(', ')).join(' | ');
      // Say the real number and the real action. "click to cycle" was vague, and on the common
      // case — exactly two — "cycle" is simply the wrong word for what the click does.
      const many = g.fs.length > 1
        ? ` — ${g.fs.length} findings here, click to switch between them` : '';
      seen[cls] = true;
      if (g.fs.length > 1) seen.multi = true;
      // Only a mark actually DRAWN grey teaches the grey swatch, and it teaches the states it
      // really holds — collected here, from the angles, rather than assumed from the palette.
      if (cls === 'ot') {
        g.fs.forEach(x => x.angles.forEach(a => { if (a.cls === 'ot') seen.unscored[a.tag] = 1; }));
      }
      // A real <button>, so every finding is reachable by Tab and actionable by Enter/Space.
      body += `<button type="button" class="mark ${cls}${g.fs.length > 1 ? ' multi' : ''}"`
           +  ` data-ids="${g.fs.map(x => x.fid).join(',')}" data-n="${g.fs.length}"`
           +  ` aria-pressed="false" title="${esc(tip)}${esc(many)}">`
           +  (esc(text.slice(g.a, g.b)) || '&nbsp;') + `</button>`;
      pos = g.b;
    });
    body += esc(text.slice(pos));

    out += `<div class="row" data-line="${n}">`
        +  `<div class="ln${here.length ? ' hit' : ''}">${n}</div>`
        +  `<div class="code">${body || ' '}</div></div>`;
  });
  $('#src').innerHTML = out;
  caretEl = hostEl = null;      // both lived inside the markup just replaced
  paintLegend();
  painted = sourceKey();
}

// The three states that are neither survived nor caught. They are NOT one thing, and the legend
// used to call all three "never run", which is wrong for the third:
//   * ignored — a `# gdmutant: ignore` annotation suppresses it. Generated for the report, never
//     run at all (no validity check, no suite run), excluded from the score.
//   * invalid — the mutation did not survive the re-parse guard, so it was discarded before any
//     test ran. Also never run.
//   * error  — the runner FAILED WHILE EXECUTING IT (a Godot crash, say). This one was run, or at
//     least attempted; calling it "never run" states the opposite of what happened.
// (Definitions taken from `Verdict` in `gdmutant/engine/loop.py`, which is where they are decided.)
// They share one grey, because the page draws them one grey — so they share one entry, and the
// entry names exactly the ones this pane contains.
const UNSCORED = {
  ignored: 'ignored — a <code># gdmutant: ignore</code> annotation, so it never ran',
  invalid: 'invalid — the mutation did not parse, so it never ran',
  error: 'errored — the runner failed while running it',
};
const UNSCORED_ORDER = ['ignored', 'invalid', 'error'];

// Built from the marks just drawn, never from the full palette. An entry for a colour this report
// does not contain teaches a reader a shade they will never see and cannot recognise — and on the
// corpus fixture, where ignored/invalid/errored are all zero, that was three quarters of the grey
// entry's copy describing nothing. It narrows with the filter for the same reason: the legend is a
// key to the marks on screen, not a glossary of the tool.
function paintLegend(){
  const bits = [];
  const row = (cls, text) => `<span><span class="sw ${cls}">&nbsp;&nbsp;</span>${text}</span>`;
  if (seen.sv) bits.push(row('sv', 'survived — no test caught it'));
  if (seen.kd) bits.push(row('kd', 'caught by a test'));
  const un = UNSCORED_ORDER.filter(k => seen.unscored[k]);
  if (un.length) bits.push(row('ot', un.map(k => UNSCORED[k]).join(' &middot; ')));
  if (seen.multi) {
    bits.push(row('multi', 'more than one finding on this token — the badge is how many;'
      + ' click to switch between them'));
  }
  $('#legend').innerHTML = bits.join('');
}

// Selection is painted by touching only the nodes that change: the previously-marked buttons, the
// active row, and one caret row. Rebuilding the whole pane per click was invisible at 18 mutants
// and froze the renderer at 309 across 225 lines.
function paintSelection(){
  const src = $('#src');
  const prev = src.querySelector('.row.active');
  if (prev) prev.classList.remove('active');
  if (caretEl) { caretEl.remove(); caretEl = null; }
  if (hostEl) { hostEl.remove(); hostEl = null; }

  // ONE pass over the marks carries both the selection and the done state — the pane is walked
  // once per selection change either way, and splitting them doubled that walk for nothing.
  const [done, recheck] = markSets();
  src.querySelectorAll('.mark').forEach(el => {
    const ids = el.dataset.ids.split(',');
    const on = !!sel && ids.indexOf(sel.fid) >= 0;
    el.classList.toggle('on', on);
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
    // A token may host several findings; it reads as handled only when every one of them is.
    el.classList.toggle('done', ids.every(i => done.has(i)));
    el.classList.toggle('recheck', ids.some(i => recheck.has(i)));
  });

  const aside = $('#aside');
  if (!sel) {
    aside.innerHTML = '<div class="card empty">Choose a marked token in the source, or use the'
      + ' arrows, to see what it means.</div>';
    paintStepper();
    syncHash();
    return;
  }
  const row = src.querySelector('.row[data-line="' + sel.line + '"]');
  if (row) {
    row.classList.add('active');
    // Caret row: the triangle pops out under the exact column, mirroring the console's `^`. One
    // line PER ANGLE, stacked, with the triangle on the first only. A bare count ("2 angles") hid
    // the very thing the reader wants; showing the first change plus "+1 more" put two numbers
    // side by side meaning different things. Each line carries its own outcome, so a finding
    // whose angles disagree shows that here rather than flattening it.
    const lines = sel.angles.map(a =>
      `${esc(a.change)} &middot; <em>${esc(a.tag)}</em>`).join('<br>');
    caretEl = document.createElement('div');
    caretEl.className = 'caret-row';
    caretEl.style.height = (sel.angles.length * 20) + 'px';
    caretEl.innerHTML = `<div class="ln"></div><div class="car">`
      + `<span class="tri" style="left:calc(12px + ${sel.col - 1}ch)">&#9650;</span>`
      // Anchored to the token's START, the same origin as the triangle, so the gap between them
      // is a constant 2ch. Anchoring to `colEnd` made the gap grow with token length.
      + `<span class="say" style="left:calc(12px + ${sel.col + 1}ch)"><i>${lines}</i></span>`
      + `</div>`;
    row.after(caretEl);
    if (NARROW.matches) {
      hostEl = document.createElement('div');
      hostEl.className = 'inline-host';
      hostEl.innerHTML = cardHTML(sel);
      row.append(hostEl);
    }
  }
  aside.innerHTML = NARROW.matches ? '' : cardHTML(sel);
  paintStepper();
  syncHash();
}

function paintStepper(){
  const list = shown();
  const idx = sel ? list.findIndex(x => x.fid === sel.fid) : -1;
  // Say "findings", because the header counts MUTANTS — two different units on one screen, and an
  // unlabelled "1 of 6" beside "18 mutants" invites the reader to reconcile them wrongly.
  $('#pos').textContent = list.length
    ? `${idx < 0 ? '–' : idx + 1} of ${plural(list.length, 'finding')}`
    : 'no findings';
  $('#prev').disabled = !list.length || idx === 0;
  $('#next').disabled = !list.length || idx === list.length - 1;

  // Counted over what is on screen, exactly like the stepper — a mark for a finding this filter
  // (or this run) does not show contributes nothing, so the number can never claim progress the
  // page cannot back up. Carried-over marks are called out separately rather than folded in.
  let done = 0, recheck = 0;
  list.forEach(f => {
    const s = markState(f);
    if (s === 'done') done++; else if (s === 'recheck') recheck++;
  });
  $('#done').textContent = list.length
    ? `${done} of ${list.length} done` + (recheck ? ` · ${recheck} to re-check` : '')
    : '';
}

// ---- the file view -------------------------------------------------------------------------

function renderFile(){
  const f = file();
  const chips = f.ops.map(([o, n]) =>
    `<button class="chip" data-op="${esc(o)}">${esc(o)}(${n})</button>`).join('');
  $('#body').innerHTML = `
    <div class="crumb">
      ${MULTI ? '<button class="back" id="back">&larr; all files</button>' : ''}
      <code>${esc(f.path)}</code>
    </div>
    <div class="bar">
      <button class="chip" data-filter="survived">survived</button>
      <button class="chip" data-filter="caught">caught</button>
      <button class="chip" data-filter="all">all</button>
      <span style="width:10px"></span>
      <button class="chip" data-op="all">every mutator</button>
      ${chips}
      <span class="done" id="done" aria-live="polite"></span>
      <div class="stepper">
        <button id="prev" title="Previous finding (left arrow)"
          aria-label="Previous finding">&larr;</button>
        <span class="pos" id="pos"></span>
        <button id="next" title="Next finding (right arrow)"
          aria-label="Next finding">&rarr;</button>
      </div>
    </div>
    <p class="note">A <b>finding</b> is one spot in the code under one mutator. gdmutant may try
      several changes at that spot — each change is a <b>mutant</b>, and the numbers above count
      mutants. Fixing a finding usually takes one test. Arrow keys step; <b>d</b> marks one done —
      done marks stay in this browser, for this report file, and never hide a survivor.</p>
    <div class="legend" id="legend"></div>
    <div class="panes">
      <div class="src"><div class="rows" id="src"></div></div>
      <div class="aside" id="aside"></div>
    </div>`;

  // Every control in this markup reaches its behaviour through the one delegated handler on
  // `#body` (see `onClick`), so there is nothing to wire here and nothing to forget to wire.
  painted = null;
  refresh();
  // Land on the first finding rather than an empty panel: the file was opened to read one.
  if (!sel) step(1);
}

// Keep the selection when it survives the new filter, so switching to "all" for context does not
// throw away the finding being read.
function keepSelection(){
  if (sel && !shown().some(x => x.fid === sel.fid)) { sel = null; refOpen = null; }
}

function paintChips(){
  document.querySelectorAll('[data-filter]').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.filter === filter));
  document.querySelectorAll('[data-op]').forEach(b =>
    b.setAttribute('aria-pressed', b.dataset.op === op));
}

function refresh(){
  if (painted !== sourceKey()) paintSource();
  paintChips();
  paintSelection();
}

function pick(f){ sel = f || null; refOpen = null; paintSelection(); }

// Only ever reached on a multi-file report: the `all files` button is drawn only when `MULTI`, and
// the Escape key that also lands here is guarded on it. So the entry this pushes always has an
// index to go back to.
function toIndex(){
  view = 'index'; sel = null; refOpen = null;
  pushEntry = true;                   // structural: back returns to the file you came from
  renderIndex();
}

// Clamp, never wrap — and never scrollIntoView: the page must not move under you.
function step(d){
  if (view !== 'file') return;
  const list = shown();
  if (!list.length) return;
  // Compare by `fid`. Findings carry no `id`, and comparing one made `undefined === undefined`
  // match element 0, so every step recomputed the index as 0 and navigation froze after one move.
  const at = sel ? list.findIndex(x => x.fid === sel.fid) : -1;
  const next = at < 0 ? (d > 0 ? 0 : list.length - 1) : at + d;
  if (next < 0 || next >= list.length) return;
  pick(list[next]);
}

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') step(-1);
  else if (e.key === 'ArrowRight') step(1);
  else if (e.key === 'Escape' && MULTI && view === 'file') toIndex();
  else if ((e.key === 'd' || e.key === 'D') && view === 'file') toggleDone(sel);
});
NARROW.addEventListener('change', () => { if (view === 'file') paintSelection(); });
$('#theme').onclick = () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === 'dark' ? 'light' : 'dark';
};

// ---- getting the JSON back out ---------------------------------------------------------------
//
// The full mutation-testing-elements report is already in the page, in the `application/json`
// block, and has been since the file was first written, but nothing said so, so the only way to
// reach it was View Source. This hands it back as a real download.
//
// It adds no data and touches no network: the bytes are the ones the page is already made of, and
// the `blob:` URL is minted in the browser from them. Self-containment is exactly as it was.
$('#dl').onclick = () => {
  const url = URL.createObjectURL(
    new Blob([$('#mutation-test-report').textContent], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  // A fixed name, never the report's own path: the file is meant to travel, and naming it after
  // somebody's directory layout is the very thing the displayed paths stopped doing.
  a.download = 'gdmutant-report.json';
  a.click();
  // The blob is held alive by its URL until this runs, and a reader may download several times.
  // Revoking in the same tick can pull the URL out from under a download that has only just
  // started, so it waits for the click to finish being handled first.
  setTimeout(() => URL.revokeObjectURL(url), 0);
};

// ---- boot ------------------------------------------------------------------------------------

// Open on whatever the URL names, falling back to the ordinary default view whenever it names
// nothing this report has. A link that no longer resolves costs you the deep link, never the page.
function open_(){
  const hit = fromHash();
  if (!hit) { lastHash = null; view = MULTI ? 'index' : 'file'; cur = 0; sel = null; }
  else {
    cur = hit.at; view = 'file'; sel = hit.finding;
    // The default filter is `survived`. A link to a caught finding would otherwise resolve
    // correctly and then land on a pane that does not show it, which reads as a broken link.
    if (sel && !shown().some(x => x.fid === sel.fid)) { filter = 'all'; op = 'all'; }
  }
  painted = null;
  view === 'index' ? renderIndex() : renderFile();
}

// Someone pasting a link into the address bar of an already-open report is the same-document case,
// so nothing reloads — without this the URL would change and the page would not.
//
// This is ALSO the browser's back and forward buttons, and it is the only listener they need. Both
// events fire on a history move between two entries that differ by fragment, and every entry this
// page creates differs by fragment: a file's is `#path:line:col:colEnd:operator`, the index's is
// no fragment at all. So a `popstate` listener beside this one would not add a case; it would
// double-handle every one of them. (`replaceState` fires neither event, which is why stepping
// stays invisible here.) The guard is what keeps the page from re-opening against its own write.
window.addEventListener('hashchange', () => {
  if (String(location.hash || '') === String(lastHash || '')) return;   // our own write
  open_();
});

// `#body` belongs to the page shell and is never replaced, so this outlives every view switch.
$('#body').onclick = onClick;
// The header sits OUTSIDE `#body` (it spans both views), and its rare-status counts are filters.
// Same handler, so a header count reaches the same filter path a chip does rather than a private
// one of its own.
$('#head').onclick = onClick;
loadMarks();
open_();
"""


def _escape_for_script(payload: str) -> str:
    """``</`` -> ``<\\/`` so nothing inside embedded source can close the ``<script>`` early.

    Valid JSON (``\\/`` escapes ``/``), so it round-trips on parse.
    """
    return payload.replace("</", "<\\/")


def _head_stats(view: ReportView) -> str:
    """The header's stat blocks: caught, survived, total, then any non-zero rare status.

    The rare ones are buttons, not text. They were the only numbers on the page with nothing
    behind them: a reader saw "204 runtime errors" and had no way to reach the 204, mutants that
    were valid, that ran, and that measured nothing because the harness fell over. Each carries a
    ``data-filter``, so it reaches the mutants through the filter the file view already has rather
    than through a second view built to show the same thing.
    """
    plain = [
        ("kd", view.detected, "caught"),
        ("sv", view.survived, "survived"),
        ("", view.total, "mutants"),
    ]
    out = "".join(
        f'<div class="stat {cls}"><b>{count}</b><i>{html.escape(label)}</i></div>'
        for cls, count, label in plain
    )
    return out + "".join(
        f'<button type="button" class="stat jump" data-filter="rare:{status}"'
        f' aria-pressed="false" title="Show the {label} in the source">'
        f"<b>{count}</b><i>{html.escape(label)}</i></button>"
        for label, count, status in view.rare
    )


def render_html(report: dict[str, Any], project_dir: str | None = None) -> str:
    """Render `report` (a ``mutation-testing-elements`` dict) as the self-contained HTML page.

    `project_dir` is the project root the run was made against; paths inside it are displayed
    relative to it (`_display_path`). Omitting it renders the paths the report is keyed by.
    """
    view = report_view(report, project_dir)
    score = "n/a" if view.score is None else f"{view.score}"
    pct = "" if view.score is None else '<span class="pct">%</span>'
    caption = (
        "no mutants could be scored"
        if view.score is None
        else f"mutation score &middot; {view.detected} of {view.detected + view.survived} caught"
    )
    script = _JS.replace("DATA_JSON", _escape_for_script(json.dumps(asdict(view))))
    data = _escape_for_script(json.dumps(report))
    return f"""<!doctype html>
<html lang="en" data-theme="light">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>gdmutant mutation report</title>
<link rel="icon" href="{FAVICON_HREF}">
<style>{_CSS}</style></head>
<body>
<div class="wrap">
  <div class="mast">
    {FRANK_SVG}
    <div><h1>gdmutant</h1><p>{html.escape(TAGLINE)}</p></div>
    <div class="acts">
      <button class="tbtn" id="dl" title="Download the full report as JSON"
        aria-label="Download the full report as JSON">&#11015;</button>
      <button class="tbtn" id="theme" title="Toggle light / dark"
        aria-label="Toggle light / dark">&#9680;</button>
    </div>
  </div>
  <div class="head" id="head">
    <div class="score">{score}{pct}<span class="cap">{caption}</span></div>
    {_head_stats(view)}
  </div>
  <div id="body"></div>
</div>
<script type="application/json" id="mutation-test-report">{data}</script>
<script>{script}</script>
</body>
</html>
"""
