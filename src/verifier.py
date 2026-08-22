"""
verifier.py

Post-transfer verification. Size check is always performed (cheap, and
catches truncated/interrupted transfers). SHA-256 is optional (config
verify_hash=true) since it requires reading the whole file back off disk
and, for a true match, re-hashing on the device too -- slower but the
strongest guarantee.

Hashing is always done in fixed-size chunks so multi-GB video files never
get loaded into RAM.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional

from .adb_manager import AdbManager
from .logger import get_logger

log = get_logger()

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB default; callers may pass a different size


def local_sha256(path: str, chunk_size: int = CHUNK_SIZE) -> str:
    """Streaming/chunked SHA-256 -- never loads the whole file into RAM,
    regardless of file size (multi-GB video files included)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def verify_size(local_path: str, expected_size: int) -> bool:
    try:
        actual = os.path.getsize(local_path)
    except OSError:
        return False
    return actual == expected_size


def verify_hash(adb: AdbManager, serial: str, remote_path: str, local_path: str) -> Optional[bool]:
    """
    Returns True/False if verification could be performed, or None if the
    device has no sha256sum binary (caller should fall back to size-only
    and log a one-time warning).
    """
    remote_digest = adb.remote_sha256(serial, remote_path)
    if remote_digest is None:
        return None
    local_digest = local_sha256(local_path)
    return remote_digest == local_digest
