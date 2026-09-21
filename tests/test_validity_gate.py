"""The NF-5 validity gate must keep giving today's answer, on the reject side as well as the accept
side, whatever is done to make it faster.

`apply_mutant` re-parses every mutated file, and a mutant whose file does not parse is `INVALID`,
never run and never scored. That re-parse is most of gdmutant's own engine time, so it is the thing
worth speeding up, and a faster version has two ways to be wrong. Wrongly rejecting a valid mutant
only hides a mutant. Wrongly accepting a broken one is the dangerous direction: the mutant runs,
Godot fails to load it, and the report counts it as killed, which is exactly what NF-5 exists to
stop.

Real catalog mutants are almost all valid by design, so a check built only from them would pass a
gate that accepted everything. So these rows deliberately include broken files: a replacement that
breaks the syntax at every mutation site, the one rejection real code was found to produce
(`-float(x)` to `+float(x)`), and files that are fine inside one function but broken across the
file, which is what a gate re-parsing only part of the file could miss. The test refuses to pass
unless it saw rejections.

The reference is pinned here, not imported from the code under test: gdtoolkit's own parse of the
whole file with metadata, the gate's behaviour before any speed work. The answer under test goes
through `apply_mutant`, the exact call the engine makes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

from gdtoolkit.parser import parser as gdparser
from lark.exceptions import LarkError

from gdmutant.adapters.gdscript import apply_mutant, generate_mutants
from gdmutant.engine.mutants import Mutant
from gdmutant.engine.spans import Span

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "benchmark_for_gate", REPO / "scripts" / "benchmark.py"
)
assert _spec and _spec.loader
_benchmark = importlib.util.module_from_spec(_spec)
sys.modules["benchmark_for_gate"] = _benchmark
_spec.loader.exec_module(_benchmark)


def reference_accepts(source: str) -> bool:
    """The gate as it was before any speed work: gdtoolkit parses the whole file, with metadata."""
    try:
        gdparser.parse(source, gather_metadata=True)
    except LarkError:
        return False
    return True


#: A replacement that cannot be valid wherever it lands: an opening bracket that never closes.
_BREAKS = "("

#: Whole-file sources a local re-parse could wrongly accept, plus the one real rejection. Each is
#: written as (name, original source, a mutant's span as (line, col, end_line, end_col), and its
#: replacement), so it goes through `apply_mutant` like any catalog mutant.
_CROSS_DECLARATION = [
    (
        "the real rejection: unary minus to unary plus",
        "func f(x: int) -> float:\n\treturn -float(x)\n",
        (2, 9, 2, 10),
        "+",
    ),
    (
        "a bracket opened in one function and never closed, with more functions after it",
        "func f(x: int) -> int:\n\treturn x + 1\n\nfunc g(y: int) -> int:\n\treturn y\n",
        (2, 11, 2, 12),
        "+ (",
    ),
    (
        "a string opened in one function and never closed",
        'func f() -> String:\n\treturn "a" + "b"\n\nfunc g() -> int:\n\treturn 1\n',
        (2, 13, 2, 14),
        '+ "',
    ),
    (
        "a body line dedented out of its function, spilling into the file's top level",
        "func f(x: int) -> int:\n\tvar y := x\n\treturn y\n",
        (3, 1, 3, 2),
        "",
    ),
]


def _rows() -> list[tuple[str, Mutant, str]]:
    """Every (label, mutant, source) the gate is checked on."""
    sources = {
        "corpus/turn_order.gd": (REPO / "corpus" / "turn_order.gd").read_text(encoding="utf-8"),
        "synthetic-1": _benchmark.synthetic_source(1),
    }
    rows: list[tuple[str, Mutant, str]] = []
    for name, source in sources.items():
        mutants = generate_mutants(name, source)
        rows += [
            (f"{name}: catalog {m.operator_id} at {m.span.line}:{m.span.column}", m, source)
            for m in mutants
        ]
        seen = set()
        for m in mutants:
            if m.span in seen:
                continue
            seen.add(m.span)
            broken = dataclasses.replace(m, operator_id="broken", replacement=_BREAKS)
            rows.append((f"{name}: broken at {m.span.line}:{m.span.column}", broken, source))
    for label, source, (line, col, end_line, end_col), replacement in _CROSS_DECLARATION:
        span = Span(line, col, end_line, end_col)
        # Split on "\n" only, the way the engine's spans do (engine/spans.py), never splitlines().
        original = source.split("\n")[line - 1][col - 1 : end_col - 1]
        rows.append((label, Mutant("fixture.gd", span, "fixture", original, replacement), source))
    return rows


def test_the_gate_gives_the_reference_answer_on_both_sides() -> None:
    rows = _rows()
    disagreements = []
    accepted = rejected = 0
    for label, mutant, source in rows:
        mutated, gate = apply_mutant(mutant, source)
        reference = reference_accepts(mutated)
        accepted += reference
        rejected += not reference
        if gate != reference:
            side = "ACCEPTED a broken file" if gate else "rejected a valid file"
            disagreements.append(f"{label}: the gate {side}")
    assert not disagreements, "\n".join(disagreements)
    # A gate that says "valid" to everything passes the loop above if every row is valid, so the
    # rows must hold both answers. Checking only accepts would be a gate that checked nothing.
    assert accepted > 0, "no row was valid, so the accept side was never checked"
    assert rejected >= len(_CROSS_DECLARATION), (
        f"only {rejected} row(s) were invalid, fewer than the {len(_CROSS_DECLARATION)} fixtures "
        "built to be, so the reject side is not really being checked"
    )


def test_every_fixture_is_rejected_by_the_reference() -> None:
    # The fixtures exist to be rejections. If the grammar ever came to accept one, the test above
    # would quietly lose that row's worth of reject-side coverage, so say so here instead.
    for label, source, (line, col, end_line, end_col), replacement in _CROSS_DECLARATION:
        lines = source.split("\n")  # +1 below puts back the "\n" each line lost
        start = sum(len(x) + 1 for x in lines[: line - 1]) + col - 1
        end = sum(len(x) + 1 for x in lines[: end_line - 1]) + end_col - 1
        mutated = source[:start] + replacement + source[end:]
        assert reference_accepts(source), f"{label}: the original must parse"
        assert not reference_accepts(mutated), f"{label}: the fixture no longer breaks the file"
