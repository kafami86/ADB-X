"""
hash_index.py

An optional, persistent local index of (sha256 -> where we've already put
that content on the PC), used by NORMAL and STRICT verification modes to
recognize a file that was renamed or moved without re-transferring it.

CRITICAL DESIGN CONSTRAINT (from spec):
  "The hash index is an optimization, NOT the ultimate source of truth."
  "If the hash database is missing, corrupted, or stale -> rebuild it
   safely. Never trust a stale hash index blindly. The backup itself must
   remain correct even if the hash index is deleted."

So every lookup here is treated as a HINT only. Callers (comparator.py)
always double check that the PC file the hint points at still actually
exists on disk with the size/hash we expect before ever treating it as
"already backed up" -- a hint that fails that check is just discarded,
never trusted blindly, and the file falls through to a normal
new/changed decision instead.

If the underlying sqlite file is missing or corrupted, this class
transparently rebuilds an empty table -- "rebuild it safely" here means
"never raise, never block a backup on a broken cache file", not
"re-hash every PC file on every corruption" (that full rescan, if ever
wanted, is a separate explicit operation -- see rebuild_from_directory).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Iterable, Optional

from .logger import get_logger

log = get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS hash_index (
    job_name    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    rel_path    TEXT NOT NULL,
    indexed_utc REAL NOT NULL,
    PRIMARY KEY (job_name, sha256)
);
CREATE INDEX IF NOT EXISTS idx_hash_index_lookup ON hash_index(job_name, sha256);
"""


class HashIndex:
    """Thread-safe (same locking pattern as StateManager -- one shared
    connection guarded by a lock, since it's written from worker threads
    after successful transfers)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = self._open_safely()

    def _open_safely(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        try:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.executescript(SCHEMA)
            conn.commit()
            # Sanity probe -- catches "file exists but isn't a valid sqlite
            # db" (corruption) that connect() alone won't surface.
            conn.execute("SELECT COUNT(*) FROM hash_index").fetchone()
            return conn
        except sqlite3.DatabaseError as e:
            log.warning(
                "Hash index at %s is missing/corrupted (%s); rebuilding an "
                "empty index. This never blocks or endangers the backup -- "
                "it only means renamed/moved files may be re-verified by "
                "hash instead of matched instantly this run.",
                self.db_path, e,
            )
            try:
                conn.close()
            except Exception:
                pass
            backup_corrupt_path = self.db_path + f".corrupt-{int(time.time())}"
            try:
                os.replace(self.db_path, backup_corrupt_path)
            except OSError:
                pass
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.executescript(SCHEMA)
            conn.commit()
            return conn

    def close(self):
        with self._lock:
            self._conn.close()

    # -------------------------------------------------------------- write
    def record(self, job_name: str, sha256: str, size: int, rel_path: str):
        with self._lock:
            self._conn.execute(
                """INSERT INTO hash_index (job_name, sha256, size, rel_path, indexed_utc)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(job_name, sha256) DO UPDATE SET
                     size=excluded.size, rel_path=excluded.rel_path,
                     indexed_utc=excluded.indexed_utc""",
                (job_name, sha256, size, rel_path, time.time()),
            )
            self._conn.commit()

    # --------------------------------------------------------------- read
    def lookup(self, job_name: str, sha256: str) -> Optional[str]:
        """Returns the rel_path this content was last seen at, or None.
        This is a HINT ONLY -- caller must verify the file still exists
        with matching size/hash before trusting it (see module docstring)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT rel_path FROM hash_index WHERE job_name=? AND sha256=?",
                (job_name, sha256),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def forget(self, job_name: str, sha256: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM hash_index WHERE job_name=? AND sha256=?", (job_name, sha256)
            )
            self._conn.commit()

    def rebuild_from_entries(self, job_name: str, entries: Iterable):
        """Safe, explicit full rebuild: wipes this job's rows and
        re-inserts from an iterable of (sha256, size, rel_path) the
        caller has already computed. Never called implicitly -- corruption
        recovery above deliberately starts empty instead, since hashing
        every PC file on every corrupted-index run would silently turn a
        FAST/NORMAL backup into a full-directory hash pass, which the spec
        explicitly forbids."""
        with self._lock:
            self._conn.execute("DELETE FROM hash_index WHERE job_name=?", (job_name,))
            for sha256, size, rel_path in entries:
                self._conn.execute(
                    """INSERT INTO hash_index (job_name, sha256, size, rel_path, indexed_utc)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(job_name, sha256) DO UPDATE SET
                         size=excluded.size, rel_path=excluded.rel_path,
                         indexed_utc=excluded.indexed_utc""",
                    (job_name, sha256, size, rel_path, time.time()),
                )
            self._conn.commit()
