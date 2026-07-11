"""End-to-end: gdmutant mutates the real corpus GDScript module and reports survivors.

Uses a marker fake runner (no Godot) to prove the full pipeline — find_sites -> generate -> apply
-> run -> tally -> report — on real GDScript. The live Godot/GdUnit4 runner is validated in CI.
"""

from collections import Counter
from pathlib import Path

from conftest import MarkerRunner

from gdmutant.adapters.gdscript import generate_mutants
from gdmutant.engine.loop import run
from gdmutant.engine.report import console_summary, stryker_report

CORPUS = Path(__file__).resolve().parent.parent / "corpus" / "turn_order.gd"


def test_corpus_generates_the_exact_deterministic_mutant_set() -> None:
    # The corpus is fixed, so its mutant set is fully deterministic — pin it exactly. A loose `>=`
    # would hide an under-generating adapter (e.g. one that dropped duplicate `>`/`0`/`-` tokens),
    # and all five catalog operators are exercised through the real parser here.
    source = CORPUS.read_text(encoding="utf-8")
    mutants = generate_mutants(str(CORPUS), source)
    assert len(mutants) == 15
    assert Counter(m.operator_id for m in mutants) == {
        "comparison": 4,
        "numeric": 6,
        "arithmetic": 3,
        "boolean": 1,
        "constant": 1,
    }


def test_end_to_end_mutate_run_and_report(tmp_path: Path) -> None:
    source = CORPUS.read_text(encoding="utf-8")
    target = tmp_path / "turn_order.gd"  # copy so on-disk mutation never touches the repo file
    target.write_text(source, encoding="utf-8")

    # The "test" catches any '>' -> '>=' mutation (the marker); everything else survives.
    result = run(str(tmp_path), str(target), source, MarkerRunner(str(target), ">="))

    assert result.killed >= 1 and result.survived >= 1
    assert result.mutation_score is not None
    assert "Survivors" in console_summary(result)
    assert stryker_report(result, str(target), source, "gdscript")["files"][str(target)]["mutants"]
    assert target.read_text(encoding="utf-8") == source  # restored
