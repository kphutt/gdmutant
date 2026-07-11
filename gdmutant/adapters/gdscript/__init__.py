"""GDScript adapter (the first adapter).

gdtoolkit AST -> apply operators -> re-emit; run `godot --headless` + GdUnit4 and
parse its JUnit XML into killed/survived. The mutation mechanism (unparse fidelity
vs. AST-guided source-span edits) is settled by a spike at the start of the v0.1 engine milestone.
"""
