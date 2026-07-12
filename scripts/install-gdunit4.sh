#!/usr/bin/env bash
# Install the GdUnit4 addon into the corpus fixture for the live self-test (tests/test_selftest_live.py).
#
# GdUnit4 is intentionally NOT vendored (see corpus/project.godot, CREDITS.md). It is downloaded at a
# pinned commit and verified by a hash of the extracted addon *tree* — not the release tag (tags can
# be re-pointed) and not the GitHub source tarball (its gzip bytes can change under you; the 2023
# codeload recompression broke checksums ecosystem-wide). The tree hash is immune to both.
#
# Bumping GdUnit4 is a deliberate act: update PIN_SHA + PIN_TREE_HASH together, and the self-test
# then re-validates the new version end-to-end. Dependabot cannot bump this (by design).
#
# Usage: scripts/install-gdunit4.sh   (run from anywhere; installs into <repo>/corpus/addons/gdUnit4)
set -euo pipefail

# --- the single pin -----------------------------------------------------------------------------
PIN_VERSION="v6.1.3"
PIN_SHA="1579130d73f15f628fd0cfdbf7d60bdc39144a26"
PIN_TREE_HASH="ff4eb405025477efad0f1aeabf5247f3cdb27515fa46de583cc750d146c64e69"
# ------------------------------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/corpus/addons/gdUnit4"
URL="https://github.com/MikeSchulze/gdUnit4/archive/${PIN_SHA}.tar.gz"

# Portable sha256 command NAME (Linux CI has sha256sum; macOS has shasum) — same output format on
# both. It must be a command, not a shell function: xargs execs it and cannot see functions.
if command -v sha256sum >/dev/null 2>&1; then SHA256="sha256sum"; else SHA256="shasum -a 256"; fi

# Deterministic hash of a directory's contents: sha256 of the LC_ALL=C-sorted "sha256  relpath"
# lines. Independent of archive format, timestamps, and extraction order. $SHA256 is intentionally
# unquoted so "shasum -a 256" word-splits into command + args for xargs.
# shellcheck disable=SC2086
tree_hash() { ( cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 $SHA256 | $SHA256 | awk '{print $1}' ); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading GdUnit4 ${PIN_VERSION} (${PIN_SHA:0:12}) ..."
curl -fsSL --retry 5 --retry-all-errors "$URL" -o "$TMP/gdunit4.tar.gz"
tar -xzf "$TMP/gdunit4.tar.gz" -C "$TMP" "gdUnit4-${PIN_SHA}/addons/gdUnit4"
EXTRACTED="$TMP/gdUnit4-${PIN_SHA}/addons/gdUnit4"

GOT="$(tree_hash "$EXTRACTED")"
if [ "$GOT" != "$PIN_TREE_HASH" ]; then
  echo "ERROR: GdUnit4 addon hash mismatch." >&2
  echo "  expected: $PIN_TREE_HASH" >&2
  echo "  got:      $GOT" >&2
  echo "If you deliberately changed PIN_SHA/PIN_VERSION, update PIN_TREE_HASH to the value above." >&2
  echo "If you did NOT, the download may be corrupt or tampered — do not install." >&2
  exit 1
fi

rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$EXTRACTED" "$DEST"
echo "Installed GdUnit4 ${PIN_VERSION} -> corpus/addons/gdUnit4 (verified)."
