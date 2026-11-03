# ruff: noqa
"""plan2.py : per-function LPT split from a full run's per-mutant durations."""

import collections, fnmatch, json, os, pathlib

funcs = {}
allkeys = {}
for m in sorted(pathlib.Path("mutants").rglob("*.meta")):
    meta = json.loads(m.read_text())
    for k, code in meta["exit_code_by_key"].items():
        stem = k.rsplit("__mutmut_", 1)[0]
        f = funcs.setdefault(stem, dict(n=0, dur=0.0))
        f["n"] += 1
        f["dur"] += meta["durations_by_key"].get(k) or 0.0
        allkeys[k] = code
print(
    "functions",
    len(funcs),
    "mutants",
    len(allkeys),
    "total_dur_s",
    round(sum(f["dur"] for f in funcs.values())),
)
top = sorted(funcs.items(), key=lambda x: -x[1]["dur"])[:8]
for s, f in top:
    print(f"  top {s} n={f['n']} dur={f['dur']:.0f}")


def pack(n):
    bins = [dict(shard=i, dur=0.0, cnt=0, globs=[]) for i in range(n)]
    for s, f in sorted(funcs.items(), key=lambda x: -x[1]["dur"]):
        b = min(bins, key=lambda b: b["dur"])
        b["dur"] += f["dur"]
        b["cnt"] += f["n"]
        b["globs"].append(s + "__mutmut_*")
    return [b for b in bins if b["globs"]]


res = {}
for n in (8, 12, 16):
    bins = pack(n)
    res[str(n)] = dict(
        include=[dict(shard=b["shard"], globs=" ".join(f'"{g}"' for g in b["globs"])) for b in bins]
    )
    print(n, "est(s):", [round(b["dur"]) for b in bins])
    # disjointness
    for k in list(allkeys)[::50]:
        hits = sum(any(fnmatch.fnmatch(k, g) for g in b["globs"]) for b in bins)
        assert hits == 1, (k, hits)
pathlib.Path("baseline2").mkdir(exist_ok=True)
pathlib.Path("baseline2/baseline-verdicts.json").write_text(json.dumps(allkeys))
with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
    for n in res:
        fh.write(f"m{n}=" + json.dumps(res[n]) + chr(10))
