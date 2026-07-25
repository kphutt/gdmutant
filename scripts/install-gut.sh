#!/usr/bin/env bash
# Install the GUT (Godot Unit Test) addon into the corpus fixture for the live self-test.
#
# SPIKE ARTIFACT (spike/gut-runner): seeds a first-class GUT runner. GUT is intentionally NOT
# vendored (same policy as GdUnit4 — see corpus/project.godot, CREDITS.md). It is downloaded at a
# pinned commit and verified by a hash of the extracted addon *tree* — not the release tag (tags can
# be re-pointed) and not the GitHub source tarball (its gzip bytes can change under you). The tree
# hash is immune to both.
#
# Bumping GUT is a deliberate act: update PIN_SHA + PIN_TREE_HASH together, and the self-test then
# re-validates the new version end-to-end. Dependabot cannot bump this (by design).
#
# Usage: scripts/install-gut.sh   (run from anywhere; installs into <repo>/corpus/addons/gut)
set -euo pipefail

# --- the single pin -----------------------------------------------------------------------------
PIN_VERSION="v9.7.1"
PIN_SHA="aeb5d4f3f7f0a6c9b5e178876d6c99b791fda605"
PIN_TREE_HASH="70db9b9adb89c4c7c5bb42a06bd639d5cd25cc05fd9fb704f50af2d057674a2c"
# ------------------------------------------------------------------------------------------------

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/corpus/addons/gut"
URL="https://github.com/bitwes/Gut/archive/${PIN_SHA}.tar.gz"

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
