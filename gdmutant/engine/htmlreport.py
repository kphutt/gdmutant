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
* **The narrative travels with it.** Each survivor's gap/risk/fix copy comes from `explain`, the
  same words the console prints, plus the operator reference inlined from `survivor_reference` so
  an offline reader can still learn what the operator means.

The rendered page keeps the full ``mutation-testing-elements`` report in a
``<script type="application/json">`` block, so the file stays machine-readable for other tooling
even though nothing in the page reads it back.

The view model (`report_view`) is built and typed here in Python, where it is testable; the inlined
script is a thin renderer over it and derives no verdicts of its own. That split is deliberate — an
earlier draft of this design let the view assert "every test still passed" from a template, which
printed it under all 18 mutants including the 11 a test had actually killed.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from gdmutant.engine.explain import DOC_BASE_URL, _display_col
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
_RARE: tuple[tuple[str, str], ...] = (
    ("timeout", "Timeout"),
    ("ignored", "Ignored"),
    ("compile errors", "CompileError"),
    ("runtime errors", "RuntimeError"),
)

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
    """One unit of work: a token, an operator, and every mutant that tried it."""

    fid: str
    line: int
    col: int
    colEnd: int  # noqa: N815 — matches the key the inlined renderer reads
    op: str
    func: str
    angles: list[Angle] = field(default_factory=list)
    gap: str = ""
    risk: str = ""
    fix: str = ""
    cls: str = ""
    tag: str = ""


@dataclass
class FileView:
    """One source file: its lines, its findings, and its own tally for the index."""

    path: str
    lines: list[str]
    findings: list[Finding]
    ops: list[tuple[str, int]]
    detected: int
    survived: int
    total: int
    score: float | None


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
    rare: list[tuple[str, int]]


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
    """A survivor's ``(gap, risk, fix)`` copy, read back out of the report's own fields.

    `report.stryker_report` writes `explain.survivor_report_fields` into ``description`` (the gap)
    and ``statusReason`` (the risk and the fix, blank-line separated). Reading it back keeps the
    page's words identical to the console's, which is the point — re-authoring report copy here is
    exactly the drift that once put "every test still passed" on a killed mutant.
    """
    parts = [p.strip() for p in str(mutant.get("statusReason", "")).split("\n\n") if p.strip()]
    return (
        str(mutant.get("description", "")),
        parts[0] if parts else "",
        parts[1] if len(parts) > 1 else "",
    )


def _findings(
    mutants: list[dict[str, Any]], raw_lines: list[str], lines: list[str], prefix: str
) -> list[Finding]:
    """Group `mutants` into findings, keyed by ``(line, span, operator)``.

    Never across operators: two operators may touch overlapping spans and still be different gaps.
    `raw_lines` are the source's own lines (whose tabs the report's columns are counted against),
    `lines` their tab-expanded twins (what the page draws); `prefix` namespaces the finding ids so
    they stay unique across a multi-file report.
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
                fid=f"{prefix}{len(findings)}",
                line=line_no,
                col=col,
                colEnd=col_end,
                op=operator,
                func=_enclosing_func(lines, line_no),
            )
            by_key[key] = finding
            findings.append(finding)
        tag, cls, outcome = _OUTCOME.get(
            str(mutant["status"]), (str(mutant["status"]).lower(), "ot", str(mutant["status"]))
        )
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
            finding.gap, finding.risk, finding.fix = _narrative(mutant)
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


def report_view(report: dict[str, Any]) -> ReportView:
    """Build the typed view model the inlined renderer reads, from a ``mutation-testing-elements``
    report dict."""
    files: list[FileView] = []
    counts: dict[str, int] = {}
    for index, (path, entry) in enumerate(report.get("files", {}).items()):
        raw_lines = str(entry.get("source", "")).split("\n")
        lines = [line.expandtabs(4) for line in raw_lines]
        mutants = list(entry.get("mutants", []))
        findings = _findings(mutants, raw_lines, lines, f"f{index}-")
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
        files.append(
            FileView(
                path=path.replace("\\", "/"),
                lines=lines,
                findings=findings,
                ops=sorted(ops.items()),
                detected=detected,
                survived=survived,
                total=len(mutants),
                score=_score(detected, survived),
            )
        )
    # Most actionable first: the file with the most survivors is where the reader should start.
    files.sort(key=lambda f: (-f.survived, f.path))
    detected = sum(v for k, v in counts.items() if k in _DETECTED)
    survived = counts.get("Survived", 0)
    present = {f.op for file in files for f in file.findings}
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
        rare=[(label, counts[status]) for label, status in _RARE if counts.get(status)],
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
.mast .tbtn{margin-left:auto}

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
.fhead span:nth-child(n+3){text-align:right}

/* ---- controls ---- */
.bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{background:var(--surface);border:1px solid var(--border);color:var(--text-muted);
  border-radius:999px;padding:5px 12px;font:500 12px/1 var(--mono);cursor:pointer}
.chip[aria-pressed="true"]{background:var(--accent-soft);border-color:var(--accent-border);
  color:var(--accent)}
.stepper{margin-left:auto;display:flex;align-items:center;gap:6px;
  background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:4px 6px}
.stepper button{background:none;border:0;color:var(--text);cursor:pointer;font-size:17px;
  width:30px;height:26px;border-radius:999px}
.stepper button:hover:not(:disabled){background:var(--surface-2)}
.stepper button:disabled{opacity:.45;cursor:default}
.stepper .pos{font:600 12px/1 var(--mono);color:var(--text-muted);min-width:118px;text-align:center}

/* ---- legend: the marks mean nothing without it ---- */
.legend{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:14px;
  font-size:11.5px;color:var(--text-muted)}
.legend b{font-weight:400}
.legend .sw{font-family:var(--mono);border-radius:3px;padding:0 4px;margin-right:5px}
.legend .sw.sv{background:var(--danger-soft);box-shadow:0 0 0 1px var(--danger-border)}
.legend .sw.kd{background:var(--good-soft);box-shadow:0 0 0 1px var(--good)}
.legend .sw.ot{background:var(--surface-2);box-shadow:0 0 0 1px var(--border-strong)}
.legend .sw.multi{background:var(--danger-soft);outline:1px dashed var(--danger);outline-offset:1px}

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
  cursor:pointer;border-radius:3px;background:var(--tone-soft);
  box-shadow:0 0 0 1px var(--tone-border)}
.mark.kd{--tone:var(--good);--tone-soft:var(--good-soft);--tone-border:var(--good)}
/* Neither survived nor caught (ignored / invalid / errored): muted, never green. Green would claim
   a test caught something that was suppressed or never even parsed. */
.mark.ot{--tone:var(--text-muted);--tone-soft:var(--surface-2);--tone-border:var(--border-strong)}
/* Hover strengthens the mark's OWN tone. Fading every mark to neutral grey on hover briefly
   claimed the tone reserved for ignored/invalid. */
.mark:hover{box-shadow:0 0 0 2px var(--tone)}
/* A dashed outline (outside the box, clear of the baseline) = several findings on this token;
   clicking cycles them. */
.mark.multi{outline:1px dashed var(--tone);outline-offset:1px}
.mark.on{background:var(--accent-soft);box-shadow:0 0 0 2px var(--accent)}

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
.f.fix dt{color:var(--accent)}
.card a{color:var(--accent);font-size:12.5px;font-family:var(--mono)}
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
// What the source pane currently has painted. The pane is rebuilt only when this changes —
// selection moves repaint nothing but the few nodes that actually differ.
let painted = null;
let caretEl = null, hostEl = null;

const file = () => D.files[cur];
const isSv = f => f.cls === 'sv';
// The stepper walks exactly what is on screen. Making it walk survivors while the source showed
// everything meant selecting a caught mark reported "- of 6" — a live selection the stepper said
// did not exist.
const shown = () => file().findings.filter(f =>
  (filter === 'all' || (filter === 'survived' ? isSv(f) : f.cls === 'kd')) &&
  (op === 'all' || f.op === op));

// Escapes for BOTH text and attribute contexts: a quote inside a GDScript token would otherwise
// close the `title="..."` it lands in.
function esc(s){
  return (s == null ? '' : String(s)).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function plural(n, word){ return n + ' ' + word + (n === 1 ? '' : 's'); }
// Built here rather than stored per finding: the same anchor `explain.doc_url` builds, and one
// copy instead of one per finding (which cost ~240 KB on a 2851-mutant directory run).
function docUrl(f){ return D.docBase + '#' + f.op; }

// ---- detail card ---------------------------------------------------------------------------

function cardHTML(f){
  // Prints only what the generator computed. No outcome is derived here — that is exactly how
  // "every test still passed" once ended up under a tag reading CAUGHT. One narrative, then one
  // line per angle; no aggregate sentence is invented over the angle set.
  const angles = f.angles.map(a => `<b>${esc(a.change)}</b> — ${esc(a.outcome)}.`).join('\n');
  const many = f.angles.length > 1
    ? `<span class="tag op">${plural(f.angles.length, 'change')} here</span>` : '';
  return `<div class="card">
    <div class="who">
      <span class="tag ${f.cls}">${esc(f.tag)}</span>
      <span class="tag op">${esc(f.op)}</span>
      ${many}
      <span class="loc">${esc(file().path)}:${f.line}${
        f.func ? '  ·  func ' + esc(f.func) : ''}</span>
    </div>
    <div class="diff">${esc((file().lines[f.line - 1] || '').trim())}
${angles}</div>
    ${f.gap ? `<dl class="f"><dt>gap</dt><dd>${esc(f.gap)}</dd></dl>` : ''}
    ${f.risk ? `<dl class="f"><dt>risk</dt><dd>${esc(f.risk)}</dd></dl>` : ''}
    ${f.fix ? `<dl class="f fix"><dt>fix</dt><dd>${esc(f.fix)}</dd></dl>` : ''}
    ${refHTML(f)}
  </div>`;
}

// The per-operator reference, inlined — it expands in place instead of navigating away, and
// offline a GitHub link resolves to nothing at all. Shown only on a finding with a surviving
// angle: "what a numeric survivor means" beside a CAUGHT tag contradicts the tag.
function refHTML(f){
  if (!isSv(f)) return '';
  const secs = D.refs[f.op];
  if (!secs) return `<a href="${esc(docUrl(f))}">${esc(f.op)} reference &rarr;</a>`;
  const open = refOpen === f.fid;
  return `<button class="refbtn" data-ref="${esc(f.fid)}" aria-expanded="${open}">`
    + `${open ? '&#9662;' : '&#9656;'} what a ${esc(f.op)} survivor means</button>`
    + (open ? `<div class="ref">`
        + secs.map(([k, v]) => `<dl class="f"><dt>${esc(k)}</dt><dd>${v}</dd></dl>`).join('')
        + `<a href="${esc(docUrl(f))}">read it on GitHub &rarr;</a></div>` : '');
}

function wireRef(root){
  const b = root.querySelector('.refbtn');
  if (b) b.onclick = () => {
    refOpen = refOpen === b.dataset.ref ? null : b.dataset.ref;   // toggle
    paintSelection();
  };
}

// ---- the file index ------------------------------------------------------------------------

function paintFiles(){
  const q = query.trim().toLowerCase();
  const rows = D.files
    .map((f, i) => [f, i])
    .filter(([f]) => !q || f.path.toLowerCase().includes(q))
    .map(([f, i]) => `<button class="frow" data-file="${i}">
      <span class="fsc">${f.score === null ? '&ndash;'
        : f.score + '<span style="font-size:14px">%</span>'}</span>
      <span class="fpath">${esc(f.path)}</span>
      <span class="fn sv">${f.survived}</span>
      <span class="fn kd">${f.detected}</span>
      <span class="fn">${f.total}</span>
    </button>`).join('');
  $('#filelist').innerHTML =
    `<div class="fhead"><span>score</span><span>file</span><span>survived</span>`
    + `<span>caught</span><span>mutants</span></div>`
    + (rows || '<div class="empty">No file matches that filter.</div>');
}

function renderIndex(){
  const box = D.files.length >= SEARCH_FROM
    ? `<input id="q" class="qbox" type="search" placeholder="filter files…"
         aria-label="Filter files by path">` : '';
  $('#body').innerHTML = `<p class="note">Most survivors first — that is where to start.
      Choose a file to work through its findings.</p>
    ${box}<div class="files" id="filelist"></div>`;
  const input = $('#q');
  if (input) { input.value = query; input.oninput = () => { query = input.value; paintFiles(); }; }
  // Delegated: the row list is rebuilt on every keystroke in the filter box.
  $('#filelist').onclick = e => {
    const el = e.target.closest('.frow');
    if (!el) return;
    cur = +el.dataset.file; sel = null; refOpen = null; view = 'file'; renderFile();
  };
  paintFiles();
}

// ---- the source pane -----------------------------------------------------------------------

// What the painted source depends on. Selection is deliberately NOT part of it.
function sourceKey(){ return cur + '|' + filter + '|' + op; }

function paintSource(){
  const f = file();
  const vis = new Set(shown().map(x => x.fid));
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
      // A real <button>, so every finding is reachable by Tab and actionable by Enter/Space.
      body += `<button type="button" class="mark ${cls}${g.fs.length > 1 ? ' multi' : ''}"`
           +  ` data-ids="${g.fs.map(x => x.fid).join(',')}" aria-pressed="false"`
           +  ` title="${esc(tip)}${g.fs.length > 1 ? ' — click to cycle findings' : ''}">`
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
  painted = sourceKey();
}

// Selection is painted by touching only the nodes that change: the previously-marked buttons, the
// active row, and one caret row. Rebuilding the whole pane per click was invisible at 18 mutants
// and froze the renderer at 309 across 225 lines.
function paintSelection(){
  const src = $('#src');
  src.querySelectorAll('.mark.on').forEach(el => {
    el.classList.remove('on');
    el.setAttribute('aria-pressed', 'false');
  });
  const prev = src.querySelector('.row.active');
  if (prev) prev.classList.remove('active');
  if (caretEl) { caretEl.remove(); caretEl = null; }
  if (hostEl) { hostEl.remove(); hostEl = null; }

  const aside = $('#aside');
  if (!sel) {
    aside.innerHTML = '<div class="card empty">Choose a marked token in the source, or use the'
      + ' arrows, to see what it means.</div>';
    paintStepper();
    return;
  }
  src.querySelectorAll('.mark').forEach(el => {
    if (el.dataset.ids.split(',').indexOf(sel.fid) >= 0) {
      el.classList.add('on');
      el.setAttribute('aria-pressed', 'true');
    }
  });
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
      wireRef(hostEl);
    }
  }
  aside.innerHTML = NARROW.matches ? '' : cardHTML(sel);
  if (!NARROW.matches) wireRef(aside);
  paintStepper();
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
      mutants. Fixing a finding usually takes one test.</p>
    <div class="legend">
      <span><span class="sw sv">&nbsp;&nbsp;</span>survived — no test caught it</span>
      <span><span class="sw kd">&nbsp;&nbsp;</span>caught by a test</span>
      <span><span class="sw ot">&nbsp;&nbsp;</span>never run (ignored, invalid or errored)</span>
      <span><span class="sw multi">&nbsp;&nbsp;</span>more than one finding here — click to
        cycle</span>
    </div>
    <div class="panes">
      <div class="src"><div class="rows" id="src"></div></div>
      <div class="aside" id="aside"></div>
    </div>`;

  // One delegated handler for every mark in the pane, attached once — not one per mark, reattached
  // on every click.
  $('#src').onclick = e => {
    const el = e.target.closest('.mark');
    if (!el) return;
    const ids = el.dataset.ids.split(',');
    // One finding: select it. Several sharing this token: cycle findings on repeat clicks.
    const at = sel ? ids.indexOf(sel.fid) : -1;
    pick(f.findings.find(x => x.fid === ids[at < 0 ? 0 : (at + 1) % ids.length]));
  };
  document.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
    filter = b.dataset.filter; keepSelection(); refresh();
  });
  document.querySelectorAll('[data-op]').forEach(b => b.onclick = () => {
    op = b.dataset.op; keepSelection(); refresh();
  });
  if (MULTI) $('#back').onclick = toIndex;
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

function toIndex(){ view = 'index'; sel = null; refOpen = null; renderIndex(); }

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
});
NARROW.addEventListener('change', () => { if (view === 'file') paintSelection(); });
$('#theme').onclick = () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === 'dark' ? 'light' : 'dark';
};
view === 'index' ? renderIndex() : renderFile();
"""


def _escape_for_script(payload: str) -> str:
    """``</`` -> ``<\\/`` so nothing inside embedded source can close the ``<script>`` early.

    Valid JSON (``\\/`` escapes ``/``), so it round-trips on parse.
    """
    return payload.replace("</", "<\\/")


def _head_stats(view: ReportView) -> str:
    """The header's stat blocks: caught, survived, total, then any non-zero rare status."""
    blocks = [
        ("kd", view.detected, "caught"),
        ("sv", view.survived, "survived"),
        ("", view.total, "mutants"),
        *[("", count, label) for label, count in view.rare],
    ]
    return "".join(
        f'<div class="stat {cls}"><b>{count}</b><i>{html.escape(label)}</i></div>'
        for cls, count, label in blocks
    )


def render_html(report: dict[str, Any]) -> str:
    """Render `report` (a ``mutation-testing-elements`` dict) as the self-contained HTML page."""
    view = report_view(report)
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
<style>{_CSS}</style></head>
<body>
<div class="wrap">
  <div class="mast">
    {FRANK_SVG}
    <div><h1>gdmutant</h1><p>{html.escape(TAGLINE)}</p></div>
    <button class="tbtn" id="theme" title="Toggle light / dark"
      aria-label="Toggle light / dark">&#9680;</button>
  </div>
  <div class="head">
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
