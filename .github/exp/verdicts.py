# ruff: noqa
"""verdicts.py OUT.json : dump {mutant key: exit code} from every mutants/**/*.meta; print counts."""

import json, sys, pathlib, collections

out = {}
for m in pathlib.Path("mutants").rglob("*.meta"):
    out.update(json.loads(m.read_text())["exit_code_by_key"])
pathlib.Path(sys.argv[1]).write_text(json.dumps(out))
names = {
    0: "survived",
    1: "killed",
    3: "killed",
    33: "no_tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "type_check",
    152: "timeout",
    24: "timeout",
    None: "NOT_RUN",
}
c = collections.Counter(out.values())
print(f"total={len(out)} raw={dict(c)}")
