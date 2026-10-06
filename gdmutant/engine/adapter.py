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
    """The language-specific callables the engine needs:

    - `generate_mutants(path, source, catalog)` → every mutant for the file (each already tagged
      with any ``# gdmutant: ignore`` reason).
    - `apply_mutant(mutant, source)` → ``(mutated_source, is_valid)``; an invalid mutant (NF-5 —
      the language couldn't parse the result) is tallied without ever running the suite.
    - `isolate_copy(copy_dir, token)` / `release_copy(token)` → the pair that makes ``--jobs``
      sound. See their own fields below.
    """

    generate_mutants: Callable[[str, str, tuple[Operator, ...]], list[Mutant]]
    apply_mutant: Callable[[Mutant, str], tuple[str, bool]]
    #: Give the project copy at `copy_dir` whatever state it would otherwise **share** with every
    #: other copy of the same project, under the unique `token` the engine assigns it.
    #:
    #: ``--jobs N`` runs N mutants at once by giving each worker its own copy of the project. That
    #: isolates every file a worker touches *inside* the project directory, and nothing outside it.
    #: A language whose test run writes to a machine-wide location — one derived from the project's
    #: name rather than its path, a fixed port, a directory in the user's profile — has N workers
    #: writing to one place, and a suite that trips over itself there goes red for a reason that has
    #: nothing to do with the mutant. A red suite is a KILL, so the run reports **fewer survivors
    #: than exist**: the one direction a mutation tool must never be wrong in.
    #:
    #: The engine cannot know what that state is, so it hands over the copy and a token and asks.
    #: An adapter whose test run genuinely shares nothing outside the project directory implements
    #: this as a no-op — but as a *considered* one, because a no-op here is a claim that parallel
    #: runs are sound, not an absence of opinion. Raise instead if a copy cannot be isolated: the
    #: engine would otherwise go on to run it in parallel, which is the defect this exists to stop.
    isolate_copy: Callable[[str, str], None]
    #: Undo `isolate_copy` for the copy identified by `token`, once its worker has finished.
    #:
    #: The engine deletes the project copy itself; this is for anything the isolation created
    #: *outside* it, which the engine has no way to name. Called exactly once per isolated copy,
    #: after that worker's last suite run, including when the run is failing. It must not raise: the
    #: mutants are already scored by then, and a leftover temporary directory is not a reason to
    #: turn a finished run into a failed one. That is a contract the engine relies on and does not
    #: check: it calls this from a `finally` and does not catch what comes out. (If `isolate_copy`
    #: itself raises, this is not called, on either path that isolates.)
    release_copy: Callable[[str], None]
