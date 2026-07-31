"""The per-operator survivor reference, inlined into the HTML report.

The HTML report is **self-contained** — it has to explain a survivor with no network. A link to
GitHub is exactly what an offline reader cannot follow, and sending someone out to a browser tab
mid-triage breaks the loop they are in. The whole reference is a few kilobytes, so the report
carries it rather than pointing at it (the canonical link still rides along, below the expansion).

This table is the **shipped** copy: `docs/` is not part of the wheel, so the renderer cannot read
`docs/survivors/README.md` at runtime. To keep one home for the fact,
`tests/test_survivor_reference.py` parses that page and asserts it matches this table
section-for-section — edit the page and the suite fails, pointing here.

Each value is the ordered ``(label, body)`` pairs of one operator's ``## …`` section, keyed by the
operator id (which is also that heading's GitHub slug, the anchor `explain.doc_url` builds). Bodies
keep the page's two inline markers -- backticked code spans and doubled-asterisk bold -- which
`htmlreport` turns into ``<code>`` and ``<strong>``.
"""

from __future__ import annotations

#: Operator id -> the ordered ``(label, body)`` sections of its reference entry.
SURVIVOR_REFERENCE: dict[str, tuple[tuple[str, str], ...]] = {
    "arithmetic": (
        (
            "The change",
            "gdmutant swapped an arithmetic operator (e.g. `+` → `-`, `*` → `/`). Note `+` may "
            "also concatenate strings.",
        ),
        (
            "Why it survived",
            "nothing pins the exact result, so the two operators produce values your tests treat "
            'the same (they check the sign, or "non-zero", but not the number).',
        ),
        (
            "How to kill it",
            "add a test with concrete inputs and assert the **exact** expected result.",
        ),
        (
            "Equivalent mutant?",
            "Possible when the operands make both operators yield the same value (e.g. `x * 1` vs "
            "`x / 1`, or `x + 0`).",
        ),
    ),
    "boolean": (
        (
            "The change",
            "gdmutant swapped `and` ↔ `or`.",
        ),
        (
            "Why it survived",
            "`and` and `or` return the same result **except** when the two operands disagree (one "
            "true, one false). No test exercised that case, so the connective is unchecked.",
        ),
        (
            "How to kill it",
            "add a test where exactly one side is true and the other false, and assert the outcome "
            "— that is the only input that distinguishes `and` from `or`.",
        ),
        (
            "Equivalent mutant?",
            "If one operand can never be false (or never true) at this point, `and` and `or` are "
            "equivalent and the survivor is legitimate.",
        ),
    ),
    "comparison": (
        (
            "The change",
            "gdmutant swapped a comparison operator (e.g. `>` → `>=`, `<` → `<=`, `==` → `!=`).",
        ),
        (
            "Why it survived",
            "`>` and `>=` (and their kin) differ on exactly one input — when the two sides are "
            "**equal**. Your tests run this line but never with equal operands, so the boundary is "
            "untested.",
        ),
        (
            "How to kill it",
            "add a test that reaches this line with two equal operands (a value compared to "
            "itself) and assert the result you intend. That case fails under the mutant.",
        ),
        (
            "Equivalent mutant?",
            "Rare here, but possible if the equal case is genuinely unreachable (e.g. the two "
            "operands can never be equal by construction). If so, the survivor is legitimate.",
        ),
    ),
    "compound-assign": (
        (
            "The change",
            "gdmutant swapped a compound-assignment operator (e.g. `+=` → `-=`).",
        ),
        (
            "Why it survived",
            "nothing pins the accumulated value, so the two updates look the same to your tests.",
        ),
        (
            "How to kill it",
            "add a test that drives several updates through this line and asserts the exact "
            "accumulated value.",
        ),
        (
            "Equivalent mutant?",
            "Possible when the accumulator is never observed, or the update amount is zero.",
        ),
    ),
    "constant": (
        (
            "The change",
            "gdmutant flipped a boolean literal (`true` ↔ `false`).",
        ),
        (
            "Why it survived",
            "nothing your tests assert depends on this value, so its actual value is invisible to "
            "the suite.",
        ),
        (
            "How to kill it",
            "add a test that exercises the behavior this flag/value controls and assert it matches "
            "the value.",
        ),
        (
            "Equivalent mutant?",
            "If the value never affects observable behavior (dead flag), the survivor is "
            "legitimate — consider removing the constant.",
        ),
    ),
    "logical-not": (
        (
            "The change",
            "gdmutant removed a `not`, inverting a condition.",
        ),
        (
            "Why it survived",
            "no test runs this branch with the condition both ways, so the inversion changes "
            "nothing your tests observe.",
        ),
        (
            "How to kill it",
            "add a test that makes the condition true and another that makes it false, and assert "
            "which branch runs each time.",
        ),
        (
            "Equivalent mutant?",
            "If the guarded branch has no observable effect, the survivor is legitimate.",
        ),
    ),
    "modulo": (
        (
            "The change",
            "gdmutant swapped `%` with another operator (e.g. `*`, `/`).",
        ),
        (
            "Why it survived",
            "every test input is a clean multiple, where `%`, `*`, and `/` can produce "
            "indistinguishable results.",
        ),
        (
            "How to kill it",
            "add a test with a **non-multiple** input (one that leaves a remainder) and assert the "
            "exact result.",
        ),
        (
            "Equivalent mutant?",
            "Rare; possible if the operand is always a multiple by construction.",
        ),
    ),
    "numeric": (
        (
            "The change",
            "gdmutant changed a numeric literal (e.g. `0` → `1`, bumped a bound).",
        ),
        (
            "Why it survived",
            "no test pins the exact value or the boundary this number sets.",
        ),
        (
            "How to kill it",
            "add tests on each side of the boundary this number controls, and assert which side "
            "each input lands on.",
        ),
        (
            "Equivalent mutant?",
            "If the literal only affects an internal value that never changes an observable "
            "outcome, the survivor may be legitimate.",
        ),
    ),
    "statement-deletion": (
        (
            "The change",
            "gdmutant removed a whole statement (replaced it with `pass`).",
        ),
        (
            "Why it survived",
            "nothing your tests assert depends on this statement running, so its entire effect is "
            "unchecked.",
        ),
        (
            "How to kill it",
            "add a test that asserts the effect of this line — a signal emitted, a field set, a "
            "call made — something that fails if the line is gone.",
        ),
        (
            "Equivalent mutant?",
            "Legitimate if the statement genuinely has no observable effect (dead code) — consider "
            "removing it. The commonest shape by far is a **redundant initializer**: `_cells = "
            "PackedByteArray()` when the declaration `var _cells: PackedByteArray` already "
            "default-initialises it, or an assignment that just restates the declaration's own `=` "
            "value. Confirm one by checking that nothing can write to the variable before this "
            "line — the same statement inside a `reset()` that runs repeatedly is **not** "
            "redundant, and a test failing to catch its removal is a real gap.",
        ),
    ),
    "assert": (
        (
            "The change",
            "gdmutant changed a token inside an `assert(...)` call — a comparison, a connective, a "
            "number — or removed the assert statement outright.",
        ),
        (
            "Why it survived",
            "a mutated assertion only behaves differently on an input the original would have "
            "**rejected**, and a failed `assert` aborts the whole Godot process. A test running "
            "inside that process cannot observe the abort as anything but its own death, so no "
            "test can pass on the original and fail on the mutant. The survivor is structural — it "
            "is not a gap in your suite.",
        ),
        (
            "How to kill it",
            "from an in-process harness you cannot, and it is not worth trying. To take it out of "
            "the report, mark the line `# gdmutant: ignore`; it stays visible as `ignored` and "
            "leaves the score. gdmutant does **not** skip assert lines for you — a tool that "
            "quietly drops code from its own report is telling you a smaller truth than it knows, "
            "and which of your asserts are load-bearing is your call, not its.",
        ),
        (
            "Equivalent mutant?",
            "Effectively yes, and it is the common case rather than the exception: on defensive "
            "code, assert lines can hold most of a file's survivors and leave a healthy-looking "
            "score with nothing actionable behind it. If the condition is one real callers can "
            "actually violate, that is worth knowing — but the fix is to move the check into a "
            "branch that returns or emits an error, where a test can reach it, not to write a test "
            "that expects a crash.",
        ),
    ),
    "enum-member": (
        (
            "The change",
            "gdmutant changed the number assigned to a member of an `enum` declaration.",
        ),
        (
            "Why it survived",
            "most enums are used symbolically. `if cell == Cell.FLOOR` compares a name to a name, "
            "and both sides move together when the number behind it changes, so nothing your tests "
            "observe reads the number at all.",
        ),
        (
            "How to kill it",
            "only worth doing if the number is genuinely read **as a number**. Two cases where it "
            "is: a **bitflag** enum (`1, 2, 4`, combined with `|` and `&`), and a value that "
            "leaves the program — written to a save file, sent over a network, handed to a shader "
            "or another tool. There a changed number is a real bug, and a test that pins the "
            "concrete value (or round-trips it through whatever reads it) kills the mutant.",
        ),
        (
            "Equivalent mutant?",
            "Very often, and **gdmutant cannot tell**. It analyses one file at a time, so a "
            "numeric use in another file, in a save format, or in engine code is outside what it "
            "can see — and suppressing these by default would hide exactly the bitflag and "
            "serialisation bugs that matter most. So they are reported and the call is yours: `# "
            "gdmutant: ignore` on the line, with your reason, once you have checked.",
        ),
    ),
}
