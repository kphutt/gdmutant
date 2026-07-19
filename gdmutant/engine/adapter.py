"""The **Adapter** seam (DESIGN.md NF-3).

The engine loop is language-neutral: select → mutate → run tests → tally. Only two operations are
language-specific — generating a file's mutants, and applying one to produce mutated source. Those
are injected as an `Adapter` (the way `runner` and `catalog` are), so the engine never imports a
language adapter and a new language requires no change to the engine. The GDScript implementation is
`gdmutant.adapters.gdscript.ADAPTER`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gdmutant.engine.mutants import Mutant
from gdmutant.engine.operators import Operator


@dataclass(frozen=True)
class Adapter:
    """The two language-specific callables the engine needs:

    - `generate_mutants(path, source, catalog)` → every mutant for the file (each already tagged
      with any ``# gdmutant: ignore`` reason).
    - `apply_mutant(mutant, source)` → ``(mutated_source, is_valid)``; an invalid mutant (NF-5 —
      the language couldn't parse the result) is tallied without ever running the suite.
    """

    generate_mutants: Callable[[str, str, tuple[Operator, ...]], list[Mutant]]
    apply_mutant: Callable[[Mutant, str], tuple[str, bool]]
