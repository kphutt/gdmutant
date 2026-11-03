# ruff: noqa
"""Run a command, echo its output, and print elapsed seconds at each mutmut phase marker."""

import os, subprocess, sys, time

KEYS = (
    "Generating mutants",
    "Listing all tests",
    "Running stats",
    "Running clean tests",
    "Running mutation testing",
    "done",
    "mutations/second",
    "Found ",
    "Failed",
)
t0 = time.time()
p = subprocess.Popen(sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
last = ""
marks = []
buf = b""


def feed(line):
    global last
    s = line.decode("utf8", "replace").strip()
    s = s.lstrip("⠀⠁⠂⠃⠄⠅⠆⠇⠈⠉⠊⠋⠌⠍⠎⠏⠐⠑⠒⠓⠔⠕⠖⠗⠘⠙⠚⠛⠜⠝⠞⠟⠠⠡⠢⠣⠤⠥⠦⠧⠨⠩⠪⠫⠬⠭⠮⠯⠰⠱⠲⠳⠴⠵⠶⠷⠸⠹⠺⠻⠼⠽⠾⠿ ").strip()
    if any(k in s for k in KEYS) and s != last:
        marks.append((round(time.time() - t0), s[:120]))
        last = s


while True:
    ch = p.stdout.read1(65536)
    if not ch:
        break
    sys.stdout.buffer.write(ch)
    sys.stdout.buffer.flush()
    buf += ch
    parts = buf.replace(b"\r", b"\n").split(b"\n")
    buf = parts.pop()
    for ln in parts:
        feed(ln)
rc = p.wait()
total = round(time.time() - t0)
out = ["", f"### phase marks (seconds since start of `{' '.join(sys.argv[1:])[:60]}`)", "```"]
out += [f"{t:>5}s  {m}" for t, m in marks] + [f"{total:>5}s  EXIT {rc}", "```"]
txt = "\n".join(out)
print(txt)
sf = os.environ.get("GITHUB_STEP_SUMMARY")
if sf:
    open(sf, "a").write(txt + "\n")
sys.exit(0)
