"""Survivor explanations — turn a surviving mutant into a clear "here's the gap, here's why it
matters, here's where to start" narrative.

Every mutation tool only *locates* a survivor and shows the diff; the untested-gap narrative is
what makes it actionable. The copy follows the diagnostics gold standard (rustc/Clippy/Ruff/
ShellCheck):

  * talk about the *test suite*, not the mutation — "your tests pass whether X or Y" (green != run);
  * the ``risk`` is a *concrete failure* (a refactor slipping through green), not a restatement;
  * ``start`` names the missing *input* only — never an expected/assertion value (that oracle is
    the developer's; a guess could codify a bug), matching rustc ``HasPlaceholders`` / ESLint
    ``hasSuggestions``.

This module is deterministic (L1): it states only what the AST + the two test runs prove. The
code-aware domain guess and a drafted test are the opt-in LLM mode (L2), and live elsewhere.
"""

from __future__ import annotations

import re
import textwrap

from gdmutant.engine.mutants import Mutant

#: One stable explainer per operator (the ShellCheck "one explainer per rule" model), each a section
#: anchor in the merged survivor reference. A single base URL so the launch swap to a short vanity
#: URL is one line; the repo is private pre-launch, so this 404s for non-collaborators until the
#: flip — fine while only first-party users see it. The anchor is the operator id verbatim: every
#: ``## …`` heading in that page slugifies to its operator id (spaces → hyphens), so ``doc_url``
#: needs no slug transform — keep the two in lockstep when adding an operator.
DOC_BASE_URL = "https://github.com/kphutt/gdmutant/blob/main/docs/survivors/README.md"

_WIDTH = 74  # rule width + prose wrap target

#: Per operator id: (gap, risk, start). ``{a}`` = original token, ``{b}`` = replacement.
#: Type-neutral wording (an operator may act on numbers *or* strings), so nothing over-claims.
_EXPLAIN: dict[str, tuple[str, str, str]] = {
    "comparison": (
        "Your tests pass whether this says `{a}` or `{b}`. They run this line, but never the one "
        "input where the two disagree — equal operands. That case is untested.",
        "Passing here is false confidence, not proof. A later refactor or merge that changes the "
        "equal case slips through green. If the equal case has a right answer, no test guards it.",
        "Add a test that reaches this line with two equal operands (a value compared to "
        "itself) and assert the result you expect. Only you know that result — gdmutant "
        "reports the gap, not it.",
    ),
    "boolean": (
        "Your tests pass whether this needs both sides (`and`) or just one (`or`). No test covers "
        "the case that tells them apart: the operands disagreeing (one true, one false).",
        "Your tests can't tell 'needs both' from 'needs either.' A change that loosens or tightens "
        "this guard would pass every test.",
        "Add a test where exactly one side is true and the other false, and assert the outcome.",
    ),
    "arithmetic": (
        "Your tests pass whether this uses `{a}` or `{b}`. Nothing pins the exact result, so the "
        "two operators look the same to your tests.",
        "Your tests would accept the wrong result here. A typo or refactor that swaps the operator "
        "ships a wrong value that every test calls fine.",
        "Add a test with concrete inputs and assert the exact expected result.",
    ),
    "constant": (
        "Your tests pass whether this is `{a}` or `{b}`. Nothing they assert depends on the value, "
        "so it is invisible to your tests.",
        "This could be flipped — by accident or a bad merge — and nothing would fail. Whatever it "
        "controls is effectively untested.",
        "Add a test that exercises the behavior this value controls and assert it matches.",
    ),
    "numeric": (
        "Your tests pass whether this number is `{a}` or `{b}`. No test pins the exact "
        "value or the boundary it sets.",
        "An off-by-one here — a bad edit or a wrong assumption — would pass every test.",
        "Add a test at the boundary this number sets (one input on each side) and assert "
        "which side each lands on.",
    ),
    "logical-not": (
        "Your tests pass whether this condition is negated or not. No test runs this branch with "
        "the condition both ways.",
        "The guard could be inverted and nothing would fail — the wrong branch runs unchecked.",
        "Add a test that makes the condition true and another that makes it false, then "
        "assert which branch runs each time.",
    ),
    "compound-assign": (
        "Your tests pass whether this update is `{a}` or `{b}`. Nothing pins the "
        "accumulated value, so the two look the same.",
        "The wrong update here — a typo or a bad merge — would pass every test.",
        "Add a test that drives several updates and asserts the exact accumulated value.",
    ),
    "modulo": (
        "Your tests pass whether this uses `{a}` or `{b}`. Every test input is a clean multiple, "
        "where `%`, `*`, and `/` can look alike.",
        "A swapped operator here would pass every test that only uses clean multiples.",
        "Add a test with a non-multiple input (one that leaves a remainder) and assert the result.",
    ),
    "statement-deletion": (
        "Your tests pass with this line removed entirely. Nothing they assert depends on it "
        "running, so its whole effect is unchecked.",
        "This line could be dropped in a refactor and no test would notice. Anything "
        "relying on its effect is unguarded.",
        "Add a test that asserts this line's effect — something that fails if the line is gone.",
    ),
}
_FALLBACK = (
    "Your tests pass with this change applied — nothing distinguishes it.",
    "A change here would pass every test.",
    "Add a test that fails under this exact change.",
)

#: The reference section for a survivor inside an `assert`. Not an operator — any operator can land
#: on an assert line — so it is keyed by the heading's own slug, exactly like the operator anchors.
ASSERT_SECTION = "assert"

#: An `assert(` call opening on a source line. The `(?<![\w.])` lookbehind keeps it off `my_assert(`
#: and `helper.assert(`. This and `_enclosing_func`'s ``func`` are the only two language tokens the
#: explainer knows; everything else it says is derived from the mutant alone.
_ASSERT_CALL_RE = re.compile(r"(?<![\w.])assert\s*\(")

#: The narrative for a survivor inside an `assert` — the one place the generic "add a test that
#: distinguishes them" advice is not merely unhelpful but impossible to follow, so saying it anyway
#: is what makes a whole report read as noise.
_ASSERT_EXPLAIN = (
    "This mutant sits inside an `assert`, and your tests pass either way. A weakened assertion "
    "only behaves differently on an input the original would have rejected — and a failed assert "
    "aborts the whole Godot process, which a test running inside that process cannot observe as "
    "anything but its own death. So no in-process test can kill this one.",
    "Low, and it is not a gap in your tests. The assert guards a condition your callers are "
    "supposed to already satisfy; the real risk is reading a score built from mutants like this "
    "one as if every survivor were actionable.",
    "Treat it as a legitimate survivor. If you want it out of the report, mark the line with "
    "`# gdmutant: ignore` — it stays visible as `ignored` and drops out of the score. Only reach "
    "for a test if the condition is one real callers can actually violate, in which case the "
    "check belongs in a branch that returns or emits an error, not in an assert.",
)


def doc_url(anchor: str) -> str:
    """The stable explainer URL for a reference section (clickable anywhere; ShellCheck model) — a
    section anchor into the merged survivor reference. Every ``## …`` heading on that page slugifies
    to its key (an operator id, or `ASSERT_SECTION`), so the key is the anchor verbatim."""
    return f"{DOC_BASE_URL}#{anchor}"


def source_line(mutant: Mutant, source_lines: list[str] | None) -> str | None:
    """The raw text of the line `mutant` sits on, or ``None`` when the file's lines are unavailable
    (unreadable) or the line is off the end of them (a file that moved or shrank since the run).

    The reporting surfaces all need this same lookup before they can ask for a narrative, so it
    lives once, here, next to the narrative that consumes it. (`render_survivor` keeps its own
    inline form: it needs the *list* in the same breath, for the enclosing-function scan.)
    """
    line_no = mutant.span.line
    if source_lines is None or not (1 <= line_no <= len(source_lines)):
        return None
    return source_lines[line_no - 1]


def on_assert(original: str, column: int, source_line: str | None) -> bool:
    """True when a mutant changes something an `assert` call guards.

    `original` is the text the mutant replaced, `column` its 1-based start column, and `source_line`
    the raw text of the line it sits on (``None`` when the source is unreadable). Primitives rather
    than a `Mutant`, so the HTML report — which works from the report *dict*, not the objects — asks
    the same question of the same rule instead of growing a second copy of it that can drift.

    Two shapes count, and the column is what separates them from a false positive:

    * a token **inside** the call — the assert's ``(`` closes at or before the mutated column, so a
      trailing ``# assert(...)`` comment further along the line can never match;
    * a **deletion of the assert statement itself**, whose original text is the whole call.

    Anything else on a line that merely mentions "assert" is left alone."""
    if _ASSERT_CALL_RE.match(original):
        return True
    if source_line is None:
        return False
    match = _ASSERT_CALL_RE.search(source_line)
    return match is not None and match.end() <= column - 1


def reference_section(mutant: Mutant, source_line: str | None) -> str:
    """The survivor-reference section that explains `mutant` — its operator id, or `ASSERT_SECTION`
    when the mutant sits inside an `assert`. The `more` link, the Markdown link and the HTML
    report's inline expansion all resolve through this, so no surface can send a reader to the page
    that contradicts the explanation printed beside it."""
    if on_assert(mutant.original, mutant.span.column, source_line):
        return ASSERT_SECTION
    return mutant.operator_id


def _narrative(mutant: Mutant, source_line: str | None = None) -> tuple[str, str, str]:
    """The ``(gap, risk, start)`` sentences for `mutant`, token-substituted. This is the single
    source of the survivor copy: the console block (`render_survivor`), the Markdown job summary and
    the report fields (`survivor_report_fields`) all read it, so the surfaces can never drift.

    `source_line` is the raw text of the mutated line when the caller has it. Given it, a mutant
    inside an `assert` gets the assert narrative instead of its operator's: on defensive code
    asserts can be most of a file's survivors, and telling someone to "add a test with two equal
    operands" for a check whose failure kills the process is advice nobody can act on. Without the
    line (an unreadable source) the operator narrative still stands — accurate, just less specific.
    """
    if on_assert(mutant.original, mutant.span.column, source_line):
        return _ASSERT_EXPLAIN
    gap_t, risk_t, start_t = _EXPLAIN.get(mutant.operator_id, _FALLBACK)
    fmt = {"a": mutant.original, "b": mutant.replacement}
    return gap_t.format(**fmt), risk_t.format(**fmt), start_t.format(**fmt)


def survivor_report_fields(mutant: Mutant, source_line: str | None = None) -> tuple[str, str]:
    """The survivor narrative trimmed for the HTML report's ``description`` and ``statusReason``
    fields — the same gap/risk/start copy `render_survivor` shows, minus the box-drawing, caret,
    and docs link the HTML viewer already draws for itself. ``description`` carries the gap (what
    the tests miss); ``statusReason`` carries the risk and the starting point (why it matters and
    where to begin), blank-line separated. Both are non-empty for every survivor. `source_line` is
    passed to `_narrative` (see there: it is what makes an assert survivor explain itself)."""
    gap, risk, start = _narrative(mutant, source_line)
    return gap, f"{risk}\n\n{start}"


def _display_col(prefix: str, tabsize: int = 4) -> int:
    """Display column of `prefix` with tabs expanded — so a caret lands under a tabbed token."""
    col = 0
    for ch in prefix:
        col += tabsize - (col % tabsize) if ch == "\t" else 1
    return col


def _enclosing_func(lines: list[str], line_no: int) -> str | None:
    """The name of the ``func`` (or ``static func``) enclosing 1-based `line_no`, if any."""
    for i in range(min(line_no, len(lines)) - 1, -1, -1):
        stripped = lines[i].strip()
        for kw in ("func ", "static func "):
            if stripped.startswith(kw):
                return stripped[len(kw) :].split("(", 1)[0].strip() or None
    return None


def _block(label: str, text: str) -> list[str]:
    """A wrapped, hanging-indented ``label   text …`` paragraph."""
    head = "  " + label.ljust(7)
    indent = " " * len(head)
    wrapped = textwrap.wrap(text, width=_WIDTH - len(head)) or [""]
    return [head + wrapped[0]] + [indent + line for line in wrapped[1:]]


def render_survivor(mutant: Mutant, source_lines: list[str] | None) -> list[str]:
    """Render one survivor as the locked 7-slot block. `source_lines` is the file's lines (for the
    code + caret + enclosing function); when ``None`` (unreadable), those slots are omitted and the
    narrative still stands. Returns the block's lines (no trailing blank)."""
    op = mutant.operator_id
    a = mutant.original
    b = mutant.replacement  # only rendered for non-deletion operators (deletions use the code line)

    line_no, col = mutant.span.line, mutant.span.column
    func = None
    src = None
    if source_lines is not None and 1 <= line_no <= len(source_lines):
        src = source_lines[line_no - 1]
        func = _enclosing_func(source_lines, line_no)
    gap, risk, start = _narrative(mutant, src)
    # An assert survivor's `more` link goes to the section that explains *that*, not to the
    # operator's — the operator's page would send a reader off to write the test this one cannot be
    # killed by. The header still names the operator: it is still what changed.
    anchor = reference_section(mutant, src)

    prefix, suffix = "──── survived ", f" {op} ────"
    # A negative repeat count is already "" in Python, so no clamp is needed for a long operator id.
    out = [prefix + "─" * (_WIDTH - len(prefix) - len(suffix)) + suffix, ""]
    # The full path (as given, editors linkify ``path:line``) — unambiguous across a multi-file run.
    out.append(f"  {mutant.path}:{line_no}" + (f"   func {func}" if func else ""))
    out.append("")
    if src is not None:
        out.append(f"   {line_no:>4} | {src.expandtabs(4)}")
        caret_at = _display_col(src[: col - 1])
        if op == "statement-deletion":
            change = "this whole line was removed"
        elif mutant.replacement == "":
            change = f"removed  {a}"  # e.g. a `not` token dropped (logical-not)
        else:
            change = f"changed  {a}  to  {b}"
        out.append(f"        | {' ' * caret_at}^  {change} — every test still passed")
        out.append("")
    out += _block("gap", gap)
    out.append("")
    out += _block("risk", risk)
    out.append("")
    out += _block("start", start)
    out.append("")
    out.append(f"  more   {doc_url(anchor)}")
    out.append("─" * _WIDTH)
    return out
