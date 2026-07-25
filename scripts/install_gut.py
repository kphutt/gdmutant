#!/usr/bin/env python3
"""Install the GUT (Godot Unit Test) addon into the corpus fixture for the live self-test.

GUT is intentionally NOT vendored (same policy as GdUnit4 — see corpus/project.godot, CREDITS.md).
It is downloaded at a pinned commit and verified by a hash of the extracted addon *tree* — see
scripts/_addon_install.py for why a tree hash (not the tag, not the source-tarball bytes) and how a
deliberate bump works.

Usage: python scripts/install_gut.py  (run from anywhere; installs into <repo>/corpus/addons/gut)
"""

import hashlib
import os
import sys
from pathlib import Path

from _addon_install import install_addon

# --- the single pin -----------------------------------------------------------------------------
PIN_VERSION = "v9.7.1"
PIN_SHA = "aeb5d4f3f7f0a6c9b5e178876d6c99b791fda605"
PIN_TREE_HASH = "f867f8a8e6e685e4f796c002b27be2265627d037a4ec23b1370aa7a080e3a523"
# ------------------------------------------------------------------------------------------------


def tree_hash(root: Path) -> str:
    """Deterministic, CROSS-PLATFORM sha256 of a directory's file contents.

    sha256 over the sorted ``"<relpath>\\0<file-sha256>"`` lines (POSIX ``/`` separators), joined by
    ``\\n`` — computed by reading raw file bytes in Python. This deliberately does NOT use the
    shell's ``sha256sum`` (as install_gdunit4.py's algorithm still mirrors): GUT's addon ships
    binary font files that contain CR bytes, and Git Bash's ``sha256sum`` defaults to TEXT mode on
    Windows — stripping CR before hashing — so the shell method yields a DIFFERENT digest on the
    operator's Windows machine than on Linux CI. Reading bytes is identical on every platform, so
    one pin verifies on Windows and Linux alike. Independent of archive format, timestamps, and
    extraction order.
    """
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()  # deterministic walk (entries are sorted again below regardless)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as handle:
                entries.append(rel + "\0" + hashlib.sha256(handle.read()).hexdigest())
    entries.sort()
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def main() -> int:
    return install_addon(
        name="GUT",
        version=PIN_VERSION,
        sha=PIN_SHA,
        repo="bitwes/Gut",
        archive_member=f"Gut-{PIN_SHA}/addons/gut",
        dest_relpath="corpus/addons/gut",
        tree_hash_pin=PIN_TREE_HASH,
        tree_hash=tree_hash,
    )


if __name__ == "__main__":
    sys.exit(main())
