# ruff: noqa
import json, pathlib, sys, collections

pats = sys.argv[1:]
out = collections.Counter()
for m in pathlib.Path("mutants").rglob("*.meta"):
    for k, v in json.loads(m.read_text())["exit_code_by_key"].items():
        if any(p in k for p in pats):
            out[(k.rsplit("__mutmut_", 1)[0], v)] += 1
for (k, v), n in sorted(out.items()):
    print(f"{k} exit={v} n={n}")
