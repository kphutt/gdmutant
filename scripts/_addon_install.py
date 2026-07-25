#!/usr/bin/env python3
"""Shared installer for a pinned, NOT-vendored Godot addon downloaded into the corpus fixture.

Both scripts/install_gdunit4.py and scripts/install_gut.py are thin pin-carriers over this: each
passes its single pin (version + commit SHA + tree-hash), its archive layout, and its own tree-hash
function; this module does the download + extract + verification + install.

Why a hash of the extracted addon *tree* (not the release tag, not the source tarball's bytes): tags
can be re-pointed, and GitHub's source-tarball gzip bytes can change under you (the 2023 codeload
recompression broke checksums ecosystem-wide). A tree hash is immune to both.

The two addons deliberately use DIFFERENT tree-hash functions (each script supplies its own): their
pins were computed with different methods and must each verify byte-for-byte — see the two callers.
This module is agnostic to which one it's handed.
"""

import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

_DOWNLOAD_RETRIES = 5


def _download(url: str, dest: Path) -> None:
    """Fetch ``url`` into ``dest``, retrying transient failures.

    Mirrors the old ``curl -fsSL --retry 5 --retry-all-errors``: up to five attempts over any error.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "gdmutant-addon-installer"})
    last_error: Exception | None = None
    for attempt in range(1, _DOWNLOAD_RETRIES + 1):
        try:
            with urllib.request.urlopen(request) as response:
                dest.write_bytes(response.read())
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            if attempt == _DOWNLOAD_RETRIES:
                break
    raise RuntimeError(f"download failed after {_DOWNLOAD_RETRIES} attempts: {url}") from last_error


def install_addon(
    *,
    name: str,
    version: str,
    sha: str,
    repo: str,
    archive_member: str,
    dest_relpath: str,
    tree_hash_pin: str,
    tree_hash: Callable[[Path], str],
) -> int:
    """Download the pinned addon, verify its tree hash, and install it into the repo.

    ``archive_member`` is the path INSIDE the GitHub source tarball to extract (e.g.
    ``gdUnit4-<sha>/addons/gdUnit4``). ``dest_relpath`` is where it lands, relative to the repo root
    (e.g. ``corpus/addons/gdUnit4``). ``tree_hash`` computes the addon's deterministic content hash;
    it must match ``tree_hash_pin`` or nothing is installed. Returns a process exit code.
    """
    repo_root = Path(__file__).resolve().parent.parent
    dest = repo_root / dest_relpath
    url = f"https://github.com/{repo}/archive/{sha}.tar.gz"

    print(f"Downloading {name} {version} ({sha[:12]}) ...")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        archive = tmp / "addon.tar.gz"
        _download(url, archive)

        with tarfile.open(archive, "r:gz") as tar:
            members = [
                member
                for member in tar.getmembers()
                if member.name == archive_member or member.name.startswith(archive_member + "/")
            ]
            tar.extractall(tmp, members=members, filter="data")
        extracted = tmp / archive_member

        got = tree_hash(extracted)
        if got != tree_hash_pin:
            print(f"ERROR: {name} addon hash mismatch.", file=sys.stderr)
            print(f"  expected: {tree_hash_pin}", file=sys.stderr)
            print(f"  got:      {got}", file=sys.stderr)
            print(
                "If you deliberately changed PIN_SHA/PIN_VERSION, update PIN_TREE_HASH to the "
                "value above.",
                file=sys.stderr,
            )
            print(
                "If you did NOT, the download may be corrupt or tampered — do not install.",
                file=sys.stderr,
            )
            return 1

        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(extracted, dest)

    print(f"Installed {name} {version} -> {dest_relpath} (verified).")
    return 0
