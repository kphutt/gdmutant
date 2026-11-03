# ruff: noqa
"""merge2.py SHARDS_DIR : per-key merge. Shard trees hold identical generated mutants; take each key's verdict from the one shard that ran it."""

import json, pathlib, sys

root = pathlib.Path(sys.argv[1])
shards = sorted(root.glob("meta-*"))
assert shards
base = {}
for sh in shards:
    for m in sh.rglob("*.meta"):
        rel = m.relative_to(sh)
        meta = json.loads(m.read_text())
        if rel not in base:
            base[rel] = json.loads(json.dumps(meta))
            for k in base[rel]["exit_code_by_key"]:
                base[rel]["exit_code_by_key"][k] = None
        b = base[rel]
        assert set(b["exit_code_by_key"]) == set(meta["exit_code_by_key"]), (
            f"generation differs for {rel}"
        )
        assert b["hash_by_function_name"] == meta["hash_by_function_name"], (
            f"function hashes differ for {rel}"
        )
        for k, v in meta["exit_code_by_key"].items():
            if v is None:
                continue
            assert b["exit_code_by_key"][k] is None, f"{k} run by two shards"
            b["exit_code_by_key"][k] = v
            for fld in (
                "durations_by_key",
                "estimated_durations_by_key",
                "type_check_error_by_key",
            ):
                if k in meta.get(fld, {}):
                    b.setdefault(fld, {})[k] = meta[fld][k]
n = 0
for rel, b in base.items():
    dst = pathlib.Path("mutants") / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(b, indent=4))
    n += sum(v is not None for v in b["exit_code_by_key"].values())
missing = sum(v is None for b in base.values() for v in b["exit_code_by_key"].values())
print("merged keys with verdicts", n, "still not run", missing)
assert missing == 0, "some mutants were run by no shard"
