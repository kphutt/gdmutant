"""Survivor explanations — turn a surviving mutant into a clear "here's the gap, here's why it
matters, here's where to start" narrative (LOD-215).

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

import textwrap

from gdmutant.engine.mutants import Mutant

#: One stable docs page per operator (the ShellCheck "one explainer per rule" model). A single base
#: so the launch swap to a short vanity URL is one line; the repo is private pre-launch, so this
#: 404s for non-collaborators until the flip — fine while only first-party users see it.
DOC_BASE_URL = "https://github.com/kphutt/gdmutant/blob/main/docs/survivors"

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


def doc_url(operator_id: str) -> str:
    """The stable explainer-page URL for an operator (clickable anywhere; ShellCheck model)."""
    return f"{DOC_BASE_URL}/{operator_id}.md"


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
    gap_t, risk_t, start_t = _EXPLAIN.get(op, _FALLBACK)
    fmt = {"a": a, "b": b}

    line_no, col = mutant.span.line, mutant.span.column
    func = None
    src = None
    if source_lines is not None and 1 <= line_no <= len(source_lines):
        src = source_lines[line_no - 1]
        func = _enclosing_func(source_lines, line_no)

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
    out += _block("gap", gap_t.format(**fmt))
    out.append("")
    out += _block("risk", risk_t.format(**fmt))
    out.append("")
    out += _block("start", start_t.format(**fmt))
    out.append("")
    out.append(f"  more   {doc_url(op)}")
    out.append("─" * _WIDTH)
    return out
