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
from collections.abc import Sequence

from gdmutant.engine.mutants import Mutant

#: One stable explainer per operator (the ShellCheck "one explainer per rule" model), each a section
#: anchor in the merged survivor reference. A single base URL, so swapping in a short vanity URL is
#: a one-line change. The anchor is the operator id verbatim: every
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

#: Reference sections that explain a survivor by **where it sits** rather than by which operator
#: produced it. Any operator can land on an `assert` line or inside an `enum`, and in both places
#: the operator's own advice is not merely unhelpful but impossible to follow — which is what makes
#: a whole report read as noise. Neither is an operator, so each is keyed by its own heading slug on
#: the reference page, exactly like the operator anchors.
ASSERT_SECTION = "assert"
ENUM_SECTION = "enum-member"

#: An `assert(` call opening on a source line. The `(?<![\w.])` lookbehind keeps it off `my_assert(`
#: and `helper.assert(`.
_ASSERT_CALL_RE = re.compile(r"(?<![\w.])assert\s*\(")
#: An `enum` declaration opening. Anchored at the start of the line, so a string or a comment
#: mentioning "enum" cannot match. `assert`, `enum` and `_enclosing_func`'s `func` are the only
#: language tokens the explainer knows; everything else it says is derived from the mutant alone.
_ENUM_START_RE = re.compile(r"^\s*enum\b")

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
_ENUM_EXPLAIN = (
    "This mutant changes an `enum` member's value, and your tests pass either way. Code that "
    "refers to the member by name moves with it — both sides of `cell == Cell.FLOOR` change "
    "together — so nothing your tests observe reads the number itself.",
    "Usually none: most enums are purely symbolic. It matters when the number is read AS a "
    "number — a bitflag enum combined with `|` or `&`, or a value written to a save file, sent "
    "over a network, or handed to a shader or another program. There this really is an uncaught "
    "bug.",
    "First decide whether the number matters at all. If it does, pin it — assert the concrete "
    "value, or round-trip it through whatever reads it as a number. If every use is by name this "
    "is an equivalent mutant: mark the line `# gdmutant: ignore` with your reason, or leave it. "
    "gdmutant reads one file at a time, so it cannot make that call for you.",
)

#: Section -> narrative, for the contexts above. Looked up only when `context_section` names one,
#: so an operator's own explanation is still the default for everything else.
_CONTEXT_EXPLAIN: dict[str, tuple[str, str, str]] = {
    ASSERT_SECTION: _ASSERT_EXPLAIN,
    ENUM_SECTION: _ENUM_EXPLAIN,
}


def doc_url(anchor: str) -> str:
    """The stable explainer URL for a reference section (clickable anywhere; ShellCheck model) — a
    section anchor into the merged survivor reference. Every ``## …`` heading on that page slugifies
    to its key (an operator id, or `ASSERT_SECTION`), so the key is the anchor verbatim."""
    return f"{DOC_BASE_URL}#{anchor}"


def _closing_paren(
    source_lines: Sequence[str], line_index: int, column_index: int
) -> tuple[int, int] | None:
    """The 1-based ``(line, column)`` of the ``)`` that closes the ``(`` at 0-based
    `line_index`/`column_index`, or ``None`` when nothing in the rest of the file closes it.

    Counting parens from the opening one is what gives a call a real *end*, and the walk runs to the
    end of the file because a call may span as many lines as its author wants.

    ``None`` means "this file does not read the way I assumed" — a paren inside a string or a
    comment, which a textual scan cannot see. Returning it, rather than running the span to the end
    of the file, keeps a misread from swallowing every mutant below it. In practice the source here
    has already parsed (it is what the mutants were generated from), so parens balance everywhere
    outside strings and comments; an unclosed one is therefore already the misread case.
    """
    depth = 0
    for index in range(line_index, len(source_lines)):
        text = source_lines[index]
        for offset in range(column_index if index == line_index else 0, len(text)):
            char = text[offset]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index + 1, offset + 1
    return None


def _assert_spans(source_lines: Sequence[str]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Every ``assert(…)`` call in the file, as ``(position of the "(", position of its matching
    ")")`` — both 1-based ``(line, column)`` pairs.

    Whole-file and paren-balanced, not per-line, for the same reason `_in_enum` scans the file: an
    `assert` spans as many lines as its author writes, and the mutated line is then just the
    condition (``a == b``), which says nothing about itself. A per-line rule misses every multi-line
    assert — the expensive direction, because the reader is then handed the operator's advice ("add
    a test at the boundary"), which is impossible to follow for a check whose failure kills the
    whole process.

    The closing paren carries as much weight as the opening one. Without it the rule has no upper
    bound, so ordinary killable code *after* the call on the same physical line
    (``assert(a > b); return c > d``) inherits the assert's "no test can kill this" narrative — the
    opposite mistake, and the one that talks a reader out of a test they should write.

    Textual rather than an AST walk, for `_in_enum`'s reason: it only ever mis-scopes an
    *explanation* — it can never change a verdict, a score, or which mutants run — and an AST walk
    would put a language parse behind every reporting surface. It also keeps this engine module
    language-neutral, which an AST walk here would not.
    """
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for index, text in enumerate(source_lines):
        for match in _ASSERT_CALL_RE.finditer(text):
            closing = _closing_paren(source_lines, index, match.end() - 1)
            if closing is not None:
                spans.append(((index + 1, match.end()), closing))
    return spans


def _on_assert(original: str, line: int, column: int, source_lines: Sequence[str]) -> bool:
    """True when a mutant changes something an `assert` call guards.

    `original` is the text the mutant replaced and `line`/`column` its 1-based start position. Two
    shapes count:

    * a token **strictly between** an assert call's parens — which is why the answer needs the whole
      file and both ends of the call, not the mutated line and a lower bound (see `_assert_spans`);
    * a **deletion of the assert statement itself**, whose original text is the whole call, and so
      carries the answer without the file being consulted at all.

    Anything else that merely mentions "assert" — a lookalike identifier, a comment, a token that
    sits before the call opens or after it closes — is left alone.
    """
    if _ASSERT_CALL_RE.match(original):
        return True
    at = (line, column)
    return any(opened < at < closed for opened, closed in _assert_spans(source_lines))


def _in_enum(source_lines: Sequence[str], line_no: int) -> bool:
    """True when 1-based `line_no` falls inside an ``enum … { … }`` declaration.

    Whole-file, not per-line, because an enum body spans lines: the mutated line is usually just
    ``FLOOR = 1,``, which says nothing about itself. So the scan runs from the top of the file to
    `line_no`, tracking brace depth, and stops there.

    Brace counting is textual, so a ``{`` or ``}`` inside a comment or a string on an enum line
    would skew the depth. That only ever mis-scopes an *explanation* — it can never change a
    verdict, a score, or which mutants run — and the alternative (an AST walk) would put a
    GDScript parse behind every reporting surface.
    """
    depth = 0
    for index, text in enumerate(source_lines[:line_no], start=1):
        opening = depth == 0 and _ENUM_START_RE.match(text) is not None
        if depth == 0 and not opening:
            continue
        if index == line_no:  # the declaration's own line, or a line in its body
            return True
        depth += text.count("{") - text.count("}")
        depth = max(depth, 0)
    return False


def context_section(
    original: str, line: int, column: int, source_lines: Sequence[str] | None
) -> str | None:
    """The reference section that explains this mutant by **where it sits**, or ``None`` when its
    operator's own section is the right one.

    Primitives rather than a `Mutant`, so the HTML report — which works from the report *dict*, not
    the objects — asks the same question of the same rule instead of growing a second copy that can
    drift. `source_lines` is the whole file; without it (an unreadable source) there is nothing to
    read, and the operator narrative stands: accurate, just less specific.

    This names a *location*, never an equivalence. Every one of these mutants is still generated,
    still run, and still counted in the score exactly as before — what changes is only what
    gdmutant says about the ones that survive.
    """
    if source_lines is None:
        return None
    if _on_assert(original, line, column, source_lines):
        return ASSERT_SECTION
    if _in_enum(source_lines, line):
        return ENUM_SECTION
    return None


def reference_section(mutant: Mutant, source_lines: Sequence[str] | None) -> str:
    """The survivor-reference section that explains `mutant` — a `context_section` when one applies,
    else its operator id. The `more` link, the Markdown link and the HTML report's inline expansion
    all resolve through this, so no surface can send a reader to the page that contradicts the
    explanation printed beside it."""
    return context_section(mutant.original, mutant.span.line, mutant.span.column, source_lines) or (
        mutant.operator_id
    )


def _narrative(mutant: Mutant, source_lines: Sequence[str] | None = None) -> tuple[str, str, str]:
    """The ``(gap, risk, start)`` sentences for `mutant`, token-substituted. This is the single
    source of the survivor copy: the console block (`render_survivor`), the Markdown job summary and
    the report fields (`survivor_report_fields`) all read it, so the surfaces can never drift.

    `source_lines` is the mutated file when the caller has it. Given it, a mutant in one of the
    `context_section` places gets that context's narrative instead of its operator's, because there
    the operator's advice is impossible to follow: "add a test with two equal operands" for a check
    whose failure kills the process, or "add a test at the boundary this number sets" for an enum
    tag that has no boundary. Without the file (an unreadable source) the operator narrative still
    stands — accurate, just less specific.
    """
    section = context_section(mutant.original, mutant.span.line, mutant.span.column, source_lines)
    if section is not None:
        return _CONTEXT_EXPLAIN[section]
    gap_t, risk_t, start_t = _EXPLAIN.get(mutant.operator_id, _FALLBACK)
    fmt = {"a": mutant.original, "b": mutant.replacement}
    return gap_t.format(**fmt), risk_t.format(**fmt), start_t.format(**fmt)


def survivor_report_fields(
    mutant: Mutant, source_lines: Sequence[str] | None = None
) -> tuple[str, str]:
    """The survivor narrative trimmed for the HTML report's ``description`` and ``statusReason``
    fields — the same gap/risk/start copy `render_survivor` shows, minus the box-drawing, caret,
    and docs link the HTML viewer already draws for itself. ``description`` carries the gap (what
    the tests miss); ``statusReason`` carries the risk and the starting point (why it matters and
    where to begin), blank-line separated. Both are non-empty for every survivor. `source_lines` is
    passed to `_narrative` (see there: it is what lets a survivor explain where it sits)."""
    gap, risk, start = _narrative(mutant, source_lines)
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
    gap, risk, start = _narrative(mutant, source_lines)
    # An assert survivor's `more` link goes to the section that explains *that*, not to the
    # operator's — the operator's page would send a reader off to write the test this one cannot be
    # killed by. The header still names the operator: it is still what changed.
    anchor = reference_section(mutant, source_lines)

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
