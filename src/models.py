"""
Shared data structures used across the backup tool.
Kept dependency-free (stdlib only) so every other module can import freely
without risking circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class FileStatus(str, Enum):
    NEW = "new"
    CHANGED = "changed"
    MISSING = "missing"          # was backed up before, now absent from destination
    INCOMPLETE = "incomplete"    # a stale .part file was found
    ALREADY_BACKED_UP = "already_backed_up"
    EXTRA_ON_DEST = "extra_on_dest"  # exists on PC, no longer on phone (only used with --delete)
    NEEDS_VERIFICATION = "needs_verification"  # metadata ambiguous; deeper check required
    FAILED = "failed"


class VerificationMode(str, Enum):
    """The three selectable per-job / global verification modes.

    FAST   - path + size + mtime only. Never hashes. Default mode.
    NORMAL - metadata first; SHA-256 only when metadata is ambiguous, plus
             an optional hash-index lookup to catch obvious renames/moves
             without hashing the whole android filesystem.
    STRICT - content identity via SHA-256 is the ONLY thing that decides
             "already backed up". Filename/path never determines identity.
    """
    FAST = "fast"
    NORMAL = "normal"
    STRICT = "strict"

    @classmethod
    def from_str(cls, value: str) -> "VerificationMode":
        try:
            return cls(value.strip().lower())
        except ValueError as e:
            valid = ", ".join(m.value for m in cls)
            raise ValueError(f"Unknown verification mode '{value}'. Valid modes: {valid}") from e


@dataclass
class RemoteFileEntry:
    """A file as reported by the Android device."""
    rel_path: str        # posix-style, relative to the job's remote source root
    remote_path: str      # absolute path on the device
    size: int
    mtime: int            # epoch seconds, device clock


@dataclass
class LocalFileEntry:
    """A file as found on the PC destination."""
    rel_path: str          # posix-style, relative to the job's local destination root
    local_path: str        # absolute Windows path
    size: int
    mtime: float


@dataclass
class DiffEntry:
    rel_path: str
    status: FileStatus
    remote: Optional[RemoteFileEntry] = None
    local: Optional[LocalFileEntry] = None
    # Set when this entry was resolved to ALREADY_BACKED_UP via a content
    # hash match at a different path (rename/move detection), so callers
    # can report/log where the match was found.
    matched_at_rel_path: Optional[str] = None
    # Set when a SHA-256 was already computed on the android side during
    # comparison (STRICT mode, or NORMAL's escalation path) so the
    # transfer/verification step can reuse it instead of re-hashing.
    remote_sha256: Optional[str] = None


@dataclass
class CompareResult:
    new: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    incomplete: list = field(default_factory=list)
    already_backed_up: list = field(default_factory=list)
    extra_on_dest: list = field(default_factory=list)
    needs_verification: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    mode: "VerificationMode" = VerificationMode.FAST

    def to_transfer(self):
        """Every entry that needs an actual pull, in a stable order."""
        return self.new + self.changed + self.missing + self.incomplete

    def to_transfer_including_verification(self):
        """Same as to_transfer(), plus files whose status metadata alone
        couldn't resolve (NEEDS_VERIFICATION) -- these must be re-pulled
        and verified for real rather than silently skipped or silently
        counted as backed up. This is what actually drives the transfer
        phase; to_transfer() alone stays for callers that only want the
        clear-cut new/changed/missing/incomplete buckets."""
        return self.to_transfer() + self.needs_verification

    def total_bytes_to_transfer(self) -> int:
        return sum(
            (e.remote.size if e.remote else 0) for e in self.to_transfer_including_verification()
        )

    def counts(self) -> dict:
        return {
            "new": len(self.new),
            "changed": len(self.changed),
            "missing": len(self.missing),
            "incomplete": len(self.incomplete),
            "already_backed_up": len(self.already_backed_up),
            "extra_on_dest": len(self.extra_on_dest),
            "needs_verification": len(self.needs_verification),
            "failed": len(self.failed),
        }


@dataclass
class TransferOutcome:
    rel_path: str
    success: bool
    bytes_transferred: int = 0
    error: Optional[str] = None
    retries_used: int = 0
    worker_id: int = 0


@dataclass
class JobConfig:
    name: str
    source: str            # android path, e.g. /sdcard/DCIM/Camera
    destination: str       # windows path
    device_serial: Optional[str] = None   # overrides global default if set
    enabled: bool = True
    # Per-job override of the global verification mode. None = use global.
    verification_mode: Optional[str] = None


@dataclass
class GlobalSettings:
    adb_path: str = "adb"
    device_serial: Optional[str] = None
    retry_count: int = 3
    retry_backoff_seconds: float = 2.0
    verify_hash: bool = False
    hash_algorithm: str = "sha256"
    mtime_tolerance_seconds: int = 3
    workers: int = 4
    log_dir: str = "logs"
    state_db: str = "backup_state.sqlite3"
    # Default verification mode used by any job that doesn't override it.
    verification_mode: str = VerificationMode.FAST.value
    # Chunk size (bytes) used for streaming SHA-256 reads, both locally
    # and (implicitly) by the device's own sha256sum. Configurable per the
    # spec; large files are never loaded fully into RAM regardless.
    hash_chunk_size: int = 4 * 1024 * 1024
    # Path to the persistent hash-index DB used by NORMAL/STRICT modes.
    # Empty/None disables the index (comparator falls back to per-file
    # remote hashing without a cache, still correct, just slower).
    hash_index_db: str = "hash_index.sqlite3"
