# ruff: noqa
"""Give every source file that git says is unchanged since the cached commit an old mtime, so mutmut's mtime check skips regenerating it."""

import json, os, subprocess, pathlib

stats = json.loads(pathlib.Path("mutants/mutmut-stats.json").read_text())
base = stats["git_commit"]
changed = set(
    subprocess.run(
        ["git", "diff", "--name-only", base, "--"], capture_output=True, text=True, check=True
    ).stdout.split()
)
n = 0
for p in pathlib.Path("gdmutant").rglob("*.py"):
    if p.as_posix() not in changed:
        os.utime(p, (946684800, 946684800))
        n += 1
print(
    f"backdated {n} unchanged files; left {len(changed & {q.as_posix() for q in pathlib.Path('gdmutant').rglob('*.py')})} changed since {base[:8]}"
)
