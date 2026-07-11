"""End-to-end: gdmutant mutates the real corpus GDScript module and reports survivors.

Uses a marker fake runner (no Godot) to prove the full pipeline — find_sites -> generate -> apply
-> run -> tally -> report — on real GDScript. The live Godot/GdUnit4 runner is validated in CI.
"""

from dataclasses import dataclass
from pathlib import Path

from gdmutant.adapters.gdscript import generate_mutants
from gdmutant.engine.loop import run
from gdmutant.engine.report import console_summary, stryker_report
from gdmutant.engine.runner import SuiteResult

CORPUS = Path(__file__).resolve().parent.parent / "corpus" / "turn_order.gd"


@dataclass
class MarkerRunner:
    """A fake suite that 'catches' any mutation producing `kill_marker` in the file."""

    target: str
    kill_marker: str

    def run(self, project_dir: str) -> SuiteResult:
        content = Path(self.target).read_text(encoding="utf-8")
        return SuiteResult(tests=3, failures=int(self.kill_marker in content), errors=0)


def test_corpus_generates_many_real_mutants() -> None:
    source = CORPUS.read_text(encoding="utf-8")
    mutants = generate_mutants(str(CORPUS), source)
    assert len(mutants) >= 8
    assert {"comparison", "arithmetic", "numeric"} <= {m.operator_id for m in mutants}


def test_end_to_end_mutate_run_and_report(tmp_path: Path) -> None:
    source = CORPUS.read_text(encoding="utf-8")
    target = tmp_path / "turn_order.gd"  # copy so on-disk mutation never touches the repo file
    target.write_text(source, encoding="utf-8")

    # The "test" catches any '>' -> '>=' mutation (the marker); everything else survives.
    result = run(str(tmp_path), str(target), source, MarkerRunner(str(target), ">="))

    assert result.killed >= 1 and result.survived >= 1
    assert result.mutation_score is not None
    assert "Survivors" in console_summary(result)
    assert stryker_report(result, str(target), source)["files"][str(target)]["mutants"]
    assert target.read_text(encoding="utf-8") == source  # restored
