"""
scanner.py

Builds file listings for both sides of a backup job:
- the Android source (one adb shell round trip via AdbManager.find_files)
- the PC destination (os.walk; never reads file contents here)

Returns plain dicts keyed by posix-style relative path for O(1) lookups
in the comparator.
"""

from __future__ import annotations

import os
from typing import Dict, Set, Tuple

from .adb_manager import AdbManager
from .models import RemoteFileEntry, LocalFileEntry
from .logger import get_logger

log = get_logger()

PART_SUFFIX = ".part"


def scan_source(adb: AdbManager, serial: str, remote_root: str) -> Dict[str, RemoteFileEntry]:
    entries = adb.find_files(serial, remote_root)
    result = {e.rel_path.replace("\\", "/"): e for e in entries}
    log.debug("Scanned android source %s -> %d files", remote_root, len(result))
    return result


def scan_destination(dest_root: str) -> Tuple[Dict[str, LocalFileEntry], Set[str]]:
    """
    Returns (finished_files, part_rel_paths):
      finished_files: rel_path -> LocalFileEntry for completed (non-.part) files
      part_rel_paths: set of rel_paths (without the .part suffix) that only
                       exist as a stale .part leftover on disk
    """
    finished: Dict[str, LocalFileEntry] = {}
    part_paths: Set[str] = set()

    if not os.path.isdir(dest_root):
        os.makedirs(dest_root, exist_ok=True)
        return finished, part_paths

    for dirpath, _dirnames, filenames in os.walk(dest_root):
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, dest_root).replace(os.sep, "/")

            if fname.endswith(PART_SUFFIX):
                rel_final = rel[: -len(PART_SUFFIX)]
                part_paths.add(rel_final)
                continue

            try:
                st = os.stat(full)
            except OSError as e:
                log.warning("Could not stat %s: %s", full, e)
                continue

            finished[rel] = LocalFileEntry(
                rel_path=rel, local_path=full, size=st.st_size, mtime=st.st_mtime
            )

    log.debug("Scanned destination %s -> %d complete, %d stale .part", dest_root, len(finished), len(part_paths))
    return finished, part_paths
