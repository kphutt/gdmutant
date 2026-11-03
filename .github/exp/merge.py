# ruff: noqa
"""merge.py PLAN_N_JSON SHARDS_DIR : overlay each shard's own .meta files onto mutants/ (already holds shard 0 tree)."""

import json, shutil, sys, pathlib

inc = json.loads(sys.argv[1])["include"]
root = pathlib.Path(sys.argv[2])
for s in inc:
    d = root / f"meta-{s['shard']}"
    for src in s["files"].split(","):
        rel = pathlib.Path(src + ".meta")
        srcf = d / rel
        assert srcf.is_file(), f"shard {s['shard']} did not produce {rel}"
        dst = pathlib.Path("mutants") / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(srcf, dst)
print("merged", sum(len(s["files"].split(",")) for s in inc), "meta files from", len(inc), "shards")
