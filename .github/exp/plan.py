# ruff: noqa
"""plan.py : read the restored baseline cache, emit per-file cost and N-way balanced globs."""

import fnmatch, json, pathlib, sys, os

files = {}
for m in sorted(pathlib.Path("mutants").rglob("*.meta")):
    meta = json.loads(m.read_text())
    keys = list(meta["exit_code_by_key"])
    if not keys:
        continue
    src = str(m.relative_to("mutants"))[:-5].replace("\\", "/")
    dur = sum(v for v in meta["durations_by_key"].values() if v)
    glob = keys[0].rsplit(".", 1)[0] + ".*"
    globs = [glob]
    if src.endswith("/__init__.py"):
        globs = sorted({k.rsplit("__mutmut_", 1)[0] + "__mutmut_*" for k in keys})
    files[src] = dict(n=len(keys), dur=dur, glob=glob, globs=globs, keys=keys)
allkeys = [k for f in files.values() for k in f["keys"]]
for f in files.values():
    for k in f["keys"]:
        assert any(fnmatch.fnmatch(k, g) for g in f["globs"]), (k, f["globs"])
for k in allkeys:
    hits = [s for s, f in files.items() if any(fnmatch.fnmatch(k, g) for g in f["globs"])]
    assert len(hits) == 1, (k, hits)
print(
    "files",
    len(files),
    "mutants",
    len(allkeys),
    "total_dur_s",
    round(sum(f["dur"] for f in files.values())),
)
for s, f in sorted(files.items(), key=lambda x: -x[1]["dur"]):
    print(f"  {s:55} n={f['n']:5} dur={f['dur']:8.0f}s glob={f['glob']}")


def pack(n):
    bins = [dict(shard=i, dur=0.0, cnt=0, globs=[], files=[]) for i in range(n)]
    for s, f in sorted(files.items(), key=lambda x: -x[1]["dur"]):
        b = min(bins, key=lambda b: b["dur"])
        b["dur"] += f["dur"]
        b["cnt"] += f["n"]
        b["globs"].extend(f["globs"])
        b["files"].append(s)
    return bins


res = {}
for n in (4, 8, 16):
    bins = pack(n)
    res[str(n)] = dict(
        include=[
            dict(
                shard=b["shard"],
                globs=" ".join(f'"{g}"' for g in b["globs"]),
                files=",".join(b["files"]),
            )
            for b in bins
        ]
    )
    print(
        n,
        "shards est dur(s):",
        [round(b["dur"]) for b in bins],
        "counts:",
        [b["cnt"] for b in bins],
    )
pathlib.Path("plan.json").write_text(json.dumps(res))
base = {}
for f in files.values():
    pass
for m in pathlib.Path("mutants").rglob("*.meta"):
    base.update(json.loads(m.read_text())["exit_code_by_key"])
pathlib.Path("baseline-verdicts.json").write_text(json.dumps(base))
smallest = min(files.values(), key=lambda f: f["n"])
pathlib.Path("prep-glob.txt").write_text(smallest["globs"][0])
go = os.environ["GITHUB_OUTPUT"]
with open(go, "a") as fh:
    fh.write("m4=" + json.dumps(res["4"]) + "\n")
    fh.write("m8=" + json.dumps(res["8"]) + "\n")
    fh.write("m16=" + json.dumps(res["16"]) + chr(10))
    fh.write("prep=" + smallest["globs"][0] + "\n")
