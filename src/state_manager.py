"""
state_manager.py

A small SQLite database that remembers what this tool has previously
backed up per job. This is explicitly SUPPLEMENTARY:

  "If a state/metadata database is used, it must be treated as
   supplementary information, not the ultimate source of truth."

Its only jobs are:
  1. Let the comparator distinguish "new" (never seen before) from
     "missing" (we backed it up before, but it's not on the PC now --
     e.g. the user deleted the local copy) for nicer reporting.
  2. Cache a known-good sha256 so we don't rehash unchanged files on
     every single run when --verify-hash is on.
  3. Support --delete: only ever remove PC files that THIS job actually
     wrote in the past, never arbitrary files sitting in the folder.

Every run still re-verifies against the real filesystem before trusting
anything in here (see comparator.py).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Optional

from .logger import get_logger

log = get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    job_name    TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       INTEGER,
    sha256      TEXT,
    last_verified_utc REAL NOT NULL,
    PRIMARY KEY (job_name, rel_path)
);
"""


class StateManager:
    """NOTE ON THREAD SAFETY: with the parallel transfer engine, multiple
    worker threads call mark_backed_up() concurrently (one per completed
    file). A single sqlite3 connection is not safe to use from multiple
    threads without serialization, so every public method here takes
    self._lock for the duration of its DB access. Calls are individually
    short (one small write per finished file), so this lock is never a
    throughput bottleneck compared to the ADB pull itself."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # check_same_thread=False because we deliberately share this one
        # connection across worker threads, guarded by self._lock below --
        # sqlite3's own thread-affinity check would otherwise reject that.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(SCHEMA)
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    def mark_backed_up(self, job_name: str, rel_path: str, size: int, mtime: int, sha256: Optional[str] = None):
        with self._lock:
            self._conn.execute(
                """INSERT INTO files (job_name, rel_path, size, mtime, sha256, last_verified_utc)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_name, rel_path) DO UPDATE SET
                     size=excluded.size, mtime=excluded.mtime,
                     sha256=excluded.sha256, last_verified_utc=excluded.last_verified_utc""",
                (job_name, rel_path, size, mtime, sha256, time.time()),
            )
            self._conn.commit()

    def was_previously_backed_up(self, job_name: str, rel_path: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM files WHERE job_name=? AND rel_path=?", (job_name, rel_path)
            )
            return cur.fetchone() is not None

    def get_cached_hash(self, job_name: str, rel_path: str, size: int, mtime: int) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT sha256, size, mtime FROM files WHERE job_name=? AND rel_path=?",
                (job_name, rel_path),
            )
            row = cur.fetchone()
        if row and row[0] and row[1] == size and row[2] == mtime:
            return row[0]
        return None

    def all_known_paths(self, job_name: str):
        with self._lock:
            cur = self._conn.execute("SELECT rel_path FROM files WHERE job_name=?", (job_name,))
            return {r[0] for r in cur.fetchall()}

    def forget(self, job_name: str, rel_path: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM files WHERE job_name=? AND rel_path=?", (job_name, rel_path)
            )
            self._conn.commit()
