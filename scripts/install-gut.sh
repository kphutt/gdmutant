#!/usr/bin/env bash
# Install the GUT (Godot Unit Test) addon into the corpus fixture for the live self-test.
#
# GUT is intentionally NOT vendored (same policy as GdUnit4 — see corpus/project.godot, CREDITS.md).
# It is downloaded at a pinned commit and verified by a hash of the extracted addon *tree* — not the
# release tag (tags can be re-pointed) and not the GitHub source tarball (its gzip bytes can change
# under you). The tree hash is immune to both.
#
# Bumping GUT is a deliberate act: update PIN_SHA + PIN_TREE_HASH together, and the self-test then
# re-validates the new version end-to-end. Dependabot cannot bump this (by design).
#
# Usage: scripts/install-gut.sh   (run from anywhere; installs into <repo>/corpus/addons/gut)
set -euo pipefail

# --- the single pin -----------------------------------------------------------------------------
PIN_VERSION="v9.7.1"
PIN_SHA="aeb5d4f3f7f0a6c9b5e178876d6c99b791fda605"
PIN_TREE_HASH="f867f8a8e6e685e4f796c002b27be2265627d037a4ec23b1370aa7a080e3a523"
# ------------------------------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/corpus/addons/gut"
URL="https://github.com/bitwes/Gut/archive/${PIN_SHA}.tar.gz"

# Portable Python interpreter (both CI and the operator's machines pin Python via the toolchain).
if command -v python3 >/dev/null 2>&1; then PYTHON="python3"; else PYTHON="python"; fi

# Deterministic, CROSS-PLATFORM hash of a directory's contents: sha256 over the sorted
# "relpath\0sha256(file-bytes)" lines, computed in PYTHON. This deliberately does NOT use the shell's
# sha256sum (as install-gdunit4.sh still does): GUT's addon ships binary font files that contain CR
# bytes, and Git Bash's sha256sum defaults to TEXT mode on Windows — stripping CR before hashing — so
# the shell method yields a DIFFERENT digest on the operator's Windows machine than on Linux CI (the
# exact failure this replaces). Python reads bytes identically on every platform, so one pin verifies
# on Windows and Linux alike — the operator's "Python for logic, never bash" rule, applied to precisely
# the Git Bash gap it exists for. Independent of archive format, timestamps, and extraction order.
tree_hash() {
  "$PYTHON" - "$1" <<'PY'
import hashlib
import os
import sys

root = sys.argv[1]
entries = []
for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()  # deterministic walk (entries are sorted again below regardless)
    for name in filenames:
        full = os.path.join(dirpath, name)
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        with open(full, "rb") as handle:
            entries.append(rel + "\0" + hashlib.sha256(handle.read()).hexdigest())
entries.sort()
print(hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest())
PY
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading GUT ${PIN_VERSION} (${PIN_SHA:0:12}) ..."
curl -fsSL --retry 5 --retry-all-errors "$URL" -o "$TMP/gut.tar.gz"
tar -xzf "$TMP/gut.tar.gz" -C "$TMP" "Gut-${PIN_SHA}/addons/gut"
EXTRACTED="$TMP/Gut-${PIN_SHA}/addons/gut"

GOT="$(tree_hash "$EXTRACTED")"
if [ "$GOT" != "$PIN_TREE_HASH" ]; then
  echo "ERROR: GUT addon hash mismatch." >&2
  echo "  expected: $PIN_TREE_HASH" >&2
  echo "  got:      $GOT" >&2
  echo "If you deliberately changed PIN_SHA/PIN_VERSION, update PIN_TREE_HASH to the value above." >&2
  echo "If you did NOT, the download may be corrupt or tampered — do not install." >&2
  exit 1
fi

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$EXTRACTED" "$DEST"
echo "Installed GUT ${PIN_VERSION} -> corpus/addons/gut (verified)."
