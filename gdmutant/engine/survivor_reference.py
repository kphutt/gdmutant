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
keep the page's inline markers, which `htmlreport` turns into real markup: backticked code spans
become ``<code>``, and doubled-asterisk bold would become ``<strong>``. The page carries no bold
today, so only the code spans appear below.
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
            "add a test with concrete inputs and assert the exact expected result.",
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
            "`and` and `or` return the same result except when the two operands disagree (one "
            "true, one false). No test exercised that case, so the connective is unchecked.",
        ),
        (
            "How to kill it",
            "add a test where exactly one side is true and the other false, and assert the "
            "outcome. That is the only input that distinguishes `and` from `or`.",
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
            "which pair was swapped decides what your tests are missing. `>` against `>=`, or `<` "
            "against `<=`, differ on exactly one input, when the two sides are equal, and your "
            "tests run this line but never with equal operands. `==` against `!=` is the other "
            "case: those two are opposites and disagree on every input, so a survivor there "
            "means nothing your tests assert reads this comparison at all.",
        ),
        (
            "How to kill it",
            "add a test that reaches this line with two equal operands (a value compared to "
            "itself) and assert the result you intend. Equal operands separate every swap this "
            "operator makes, the boundary pair and the equality pair alike, so that one test "
            "kills any of them.",
        ),
        (
            "Equivalent mutant?",
            "Rare here. A boundary swap is one when the equal case is genuinely unreachable (e.g. "
            "the two operands can never be equal by construction). An `==` against `!=` swap "
            "disagrees on every input, so it is one only when nothing observable depends on the "
            "comparison at all.",
        ),
    ),
    "compound-assign": (
        (
            "The change",
            "gdmutant swapped a compound-assignment operator (e.g. `+=` → `-=`). A `+=` that "
            "appends a string literal gets no compound-assign mutant: `String` has no `-=`, so the "
            "swap would be invalid code rather than a test gap. That line can still get a "
            "statement-deletion mutant, which is a real gap when it survives.",
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
            "legitimate. Consider removing the constant.",
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
            "nothing pins the exact result, so the two operators produce values your tests treat "
            "the same. A clean multiple is not the cause: `6 % 3` is 0 where `6 * 3` is 18 and "
            "`6 / 3` is 2, so once a test asserts the number, even a clean multiple tells the two "
            "apart.",
        ),
        (
            "How to kill it",
            "add a test with concrete inputs and assert the exact result. An input that leaves a "
            "remainder makes the gap between the operators widest, but any input does once the "
            "result is pinned.",
        ),
        (
            "Equivalent mutant?",
            "Rare. It needs a left operand that is always 0, where `0 % n`, `0 * n` and `0 / n` "
            "all come out the same. An operand that is merely always a multiple does not make the "
            "mutant equivalent.",
        ),
    ),
    "numeric": (
        (
            "The change",
            "gdmutant changed a number by one unit of its last written digit (e.g. `0` → `1`, "
            "`0.5` → `0.6`, `0xFF` → `0x100`, bumped a bound). Every literal form is covered: "
            "integers, floats, hex, binary, and separated forms like `1_000`. A float moves by one "
            "unit of the precision you wrote rather than by a whole `1.0`, so the mutant is the "
            "off-by-a-bit value the constant plausibly has, not one any test would reject on "
            "sight.",
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
            "add a test that asserts the effect of this line (a signal emitted, a field set, a "
            "call made), something that fails if the line is gone.",
        ),
        (
            "Equivalent mutant?",
            "Legitimate if the statement genuinely has no observable effect (dead code). Consider "
            "removing it. The commonest shape by far is a redundant initializer: `_cells = "
            "PackedByteArray()` when the declaration `var _cells: PackedByteArray` already "
            "default-initialises it, or an assignment that just restates the declaration's own `=` "
            "value. Confirm one by checking that nothing can write to the variable before this "
            "line. The same statement inside a `reset()` that runs repeatedly is not redundant, "
            "and a test failing to catch its removal is a real gap.",
        ),
    ),
    "assert": (
        (
            "The change",
            "gdmutant changed a token inside an `assert(...)` call (a comparison, a connective, a "
            "number) or removed the assert statement outright.",
        ),
        (
            "Why it survived",
            "a mutated assertion only behaves differently on an input the original would have "
            "rejected, and a failed `assert` aborts the whole Godot process. A test running inside "
            "that process cannot observe the abort as anything but its own death, so no test can "
            "pass on the original and fail on the mutant. The survivor is structural. It is not a "
            "gap in your suite.",
        ),
        (
            "How to kill it",
            "from an in-process harness you cannot, and it is not worth trying. To take it out of "
            "the report, mark the line `# gdmutant: ignore`. It stays visible as `ignored` and "
            "leaves the score. gdmutant does not skip assert lines for you. A tool that quietly "
            "drops code from its own report is telling you a smaller truth than it knows, and "
            "which of your asserts are load-bearing is your call, not its.",
        ),
        (
            "Equivalent mutant?",
            "Effectively yes, and it is the common case rather than the exception: on defensive "
            "code, assert lines can hold most of a file's survivors and leave a healthy-looking "
            "score with nothing actionable behind it. If the condition is one real callers can "
            "actually violate, that is worth knowing, but the fix is to move the check into a "
            "branch that returns or emits an error, where a test can reach it, not to write a test "
            "that expects a crash.",
        ),
    ),
    "enum-member": (
        (
            "The change",
            "gdmutant changed a member of an `enum` declaration: usually the number assigned to "
            "it, and on a computed value like `A = 1 + 0` the arithmetic operator instead.",
        ),
        (
            "Why it survived",
            "most enums are used symbolically. `if cell == Cell.FLOOR` compares a name to a name, "
            "and both sides move together when the number behind it changes, so nothing your tests "
            "observe reads the number at all.",
        ),
        (
            "How to kill it",
            "only worth doing if the number is genuinely read as a number. Two cases where it is: "
            "a bitflag enum (`1, 2, 4`, combined with `|` and `&`), and a value that leaves the "
            "program: written to a save file, sent over a network, handed to a shader or another "
            "tool. There a changed number is a real bug, and a test that pins the concrete value "
            "(or round-trips it through whatever reads it) kills the mutant.",
        ),
        (
            "Equivalent mutant?",
            "Very often, and gdmutant cannot tell. It analyses one file at a time, so a numeric "
            "use in another file, in a save format, or in engine code is outside what it can see, "
            "and suppressing these by default would hide exactly the bitflag and serialisation "
            "bugs that matter most. So they are reported and the call is yours: `# gdmutant: "
            "ignore` on the line, with your reason, once you have checked.",
        ),
    ),
}
