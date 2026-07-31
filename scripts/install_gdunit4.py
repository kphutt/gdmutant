#!/usr/bin/env python3
"""Install the GdUnit4 addon into the corpus fixture for the live self-test.

GdUnit4 is intentionally NOT vendored (see corpus/project.godot, docs/credits.md). It is
downloaded at a pinned commit and verified by a hash of the extracted addon *tree* — see
scripts/_addon_install.py for why a tree hash (not the tag, not the source-tarball bytes) and how a
deliberate bump works.

Usage: python scripts/install_gdunit4.py  (installs into <repo>/corpus/addons/gdUnit4)
"""

import hashlib
import sys
from pathlib import Path

from _addon_install import install_addon

# --- the single pin -----------------------------------------------------------------------------
PIN_VERSION = "v6.1.3"
PIN_SHA = "1579130d73f15f628fd0cfdbf7d60bdc39144a26"
PIN_TREE_HASH = "ff4eb405025477efad0f1aeabf5247f3cdb27515fa46de583cc750d146c64e69"
# ------------------------------------------------------------------------------------------------


def tree_hash(root: Path) -> str:
    """Deterministic sha256 of a directory's file contents.

    Byte-for-byte identical to the (first field of the) shell pipeline this replaced:
        find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum
    i.e. sha256 over, for each regular file (not a symlink, matching ``find -type f``) in
    LC_ALL=C (byte-order) sorted relative-path order, the line ``"<file-sha256>  ./<relpath>\\n"``
    — two spaces, a leading ``./``, POSIX ``/`` separators. Reading raw bytes matches Linux CI's
    ``sha256sum`` (the platform the pin was computed on). Independent of archive format, timestamps,
    and extraction order.
    """
    files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    files.sort(key=lambda path: ("./" + path.relative_to(root).as_posix()).encode("utf-8"))
    outer = hashlib.sha256()
    for path in files:
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        rel = "./" + path.relative_to(root).as_posix()
        outer.update(f"{file_hash}  {rel}\n".encode())
    return outer.hexdigest()


def main() -> int:
    return install_addon(
        name="GdUnit4",
        version=PIN_VERSION,
        sha=PIN_SHA,
        repo="godot-gdunit-labs/gdUnit4",
        archive_member=f"gdUnit4-{PIN_SHA}/addons/gdUnit4",
        dest_relpath="corpus/addons/gdUnit4",
        tree_hash_pin=PIN_TREE_HASH,
        tree_hash=tree_hash,
    )


if __name__ == "__main__":
    sys.exit(main())
