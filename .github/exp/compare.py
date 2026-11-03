# ruff: noqa
"""compare.py BASELINE.json NEW.json : report differing verdicts. Exit codes are compared raw."""

import json, sys, os

a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
if "--ran-only" in sys.argv:
    b = {k: v for k, v in b.items() if v is not None}
common = a.keys() & b.keys()
diff = sorted(k for k in common if a[k] != b[k])
msg = [
    f"baseline={len(a)} new={len(b)} only_in_baseline={len(a.keys() - b.keys())} only_in_new={len(b.keys() - a.keys())} differing={len(diff)}"
]
for k in diff[:25]:
    msg.append(f"  {k}: {a[k]} -> {b[k]}")
t = "\n".join(msg)
print(t)
sf = os.environ.get("GITHUB_STEP_SUMMARY")
if sf:
    open(sf, "a").write("### verdict comparison vs baseline\n```\n" + t + "\n```\n")
