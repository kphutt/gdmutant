"""Language-neutral mutation loop: select -> mutate -> run -> tally -> report.

No language-specific assumptions live here (docs/decisions/0001). Coverage-gated
selection is a later speedup; v0.1 runs the full suite per mutant. Implemented in
the v0.1 engine milestone behind the approved DESIGN.md.
"""
