"""
Unit tests for the mode-aware comparator (FAST / NORMAL / STRICT) and the
hash index's rename/move detection.

FAST is exercised implicitly by test_comparator.py already (it's the
default `mode=` value there). This file focuses on what changes in
NORMAL and STRICT, plus the hash index's own safety guarantees.

Run with:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.comparator import compare
from src.hash_index import HashIndex
from src.models import RemoteFileEntry, LocalFileEntry, FileStatus, VerificationMode
from src.state_manager import StateManager


class ModeAwareComparatorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "state.sqlite3")
        self.state = StateManager(self.db_path)
        self.hash_db_path = os.path.join(self.tmpdir.name, "hashes.sqlite3")
        self.hash_index = HashIndex(self.hash_db_path)
        self.adb = MagicMock()

    def tearDown(self):
        self.state.close()
        self.hash_index.close()
        self.tmpdir.cleanup()

    def _remote(self, rel, size=100, mtime=1000):
        return RemoteFileEntry(rel_path=rel, remote_path=f"/sdcard/Camera/{rel}", size=size, mtime=mtime)

    def _local(self, rel, size=100, mtime=1000.0):
        # Use a real temp file so verifier.local_sha256 can actually read it.
        path = os.path.join(self.tmpdir.name, rel.replace("/", "_"))
        with open(path, "wb") as f:
            f.write(b"x" * size)
        return LocalFileEntry(rel_path=rel, local_path=path, size=size, mtime=mtime)

    # ---------------------------------------------------------------- FAST
    def test_fast_never_calls_adb_hash(self):
        """The core performance guarantee: FAST must never hash, even when
        metadata looks a little suspicious."""
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=99999)}
        dest = {"a.jpg": self._local("a.jpg", size=100, mtime=1.0)}
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.FAST, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.assertEqual(len(result.already_backed_up), 1)
        self.adb.remote_sha256.assert_not_called()
        self.assertEqual(result.mode, VerificationMode.FAST)

    def test_fast_large_directory_never_hashes(self):
        """Regression guard for the spec's explicit perf requirement:
        14,000 files must not become 14,000 hash operations in FAST."""
        source = {}
        dest = {}
        for i in range(500):  # smaller N for test speed; same principle
            name = f"file_{i:04d}.jpg"
            source[name] = self._remote(name, size=100)
            dest[name] = self._local(name, size=100)
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.FAST, adb=self.adb, serial="SER1",
        )
        self.assertEqual(len(result.already_backed_up), 500)
        self.adb.remote_sha256.assert_not_called()

    # -------------------------------------------------------------- NORMAL
    def test_normal_trusts_clean_metadata_without_hashing(self):
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=1000)}
        dest = {"a.jpg": self._local("a.jpg", size=100, mtime=1000.0)}
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.NORMAL, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.assertEqual(len(result.already_backed_up), 1)
        self.adb.remote_sha256.assert_not_called()

    def test_normal_size_mismatch_is_changed_without_hashing(self):
        """Size mismatch is unambiguous -- NORMAL shouldn't need to hash
        to confirm what's already certain."""
        source = {"a.jpg": self._remote("a.jpg", size=200, mtime=1000)}
        dest = {"a.jpg": self._local("a.jpg", size=100, mtime=1000.0)}
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.NORMAL, adb=self.adb, serial="SER1",
        )
        self.assertEqual(len(result.changed), 1)
        self.adb.remote_sha256.assert_not_called()

    def test_normal_escalates_on_suspicious_newer_mtime(self):
        """Same size, but android mtime is clearly newer than what we
        recorded -- NORMAL should escalate to a real hash comparison
        rather than trust size alone."""
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=50000)}
        dest = {"a.jpg": self._local("a.jpg", size=100, mtime=1000.0)}
        self.adb.remote_sha256.return_value = "deadbeef" * 8
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.NORMAL, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.adb.remote_sha256.assert_called_once()
        # local content is "x"*100 -- its real sha256 won't equal the
        # mocked remote digest, so this should resolve as CHANGED.
        self.assertEqual(len(result.changed), 1)

    def test_normal_escalation_matches_when_hash_agrees(self):
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=50000)}
        local = self._local("a.jpg", size=100, mtime=1000.0)
        dest = {"a.jpg": local}
        from src import verifier
        real_digest = verifier.local_sha256(local.local_path)
        self.adb.remote_sha256.return_value = real_digest
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.NORMAL, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.assertEqual(len(result.already_backed_up), 1)
        self.assertEqual(len(result.changed), 0)

    # -------------------------------------------------------------- STRICT
    def test_strict_always_hashes_even_with_clean_metadata(self):
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=1000)}
        local = self._local("a.jpg", size=100, mtime=1000.0)
        dest = {"a.jpg": local}
        from src import verifier
        real_digest = verifier.local_sha256(local.local_path)
        self.adb.remote_sha256.return_value = real_digest
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.STRICT, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.adb.remote_sha256.assert_called_once()
        self.assertEqual(len(result.already_backed_up), 1)

    def test_strict_content_mismatch_flags_changed_despite_matching_metadata(self):
        """Filename/path must NOT determine identity in STRICT -- even
        same path + same size + same mtime should be re-verified, and a
        genuine content difference must be caught."""
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=1000)}
        local = self._local("a.jpg", size=100, mtime=1000.0)
        dest = {"a.jpg": local}
        self.adb.remote_sha256.return_value = "0" * 64  # won't match real content hash
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.STRICT, adb=self.adb, serial="SER1",
        )
        self.assertEqual(len(result.changed), 1)
        self.assertEqual(len(result.already_backed_up), 0)

    def test_strict_detects_renamed_moved_file_via_hash_index(self):
        """The spec's central STRICT scenario: DCIM/Camera/IMG001.jpg on
        android matches content already backed up as
        OldPhotos/MyPhoto.jpg on the PC -- must be recognized as already
        backed up and NOT re-transferred, purely by content."""
        local = self._local("OldPhotos/MyPhoto.jpg", size=100, mtime=1000.0)
        from src import verifier
        real_digest = verifier.local_sha256(local.local_path)
        self.hash_index.record("job1", real_digest, local.size, "OldPhotos/MyPhoto.jpg")

        source = {"DCIM/Camera/IMG001.jpg": self._remote("DCIM/Camera/IMG001.jpg", size=100)}
        dest = {"OldPhotos/MyPhoto.jpg": local}  # NOT at the same rel_path as source
        self.adb.remote_sha256.return_value = real_digest

        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.STRICT, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.assertEqual(len(result.already_backed_up), 1)
        self.assertEqual(result.already_backed_up[0].matched_at_rel_path, "OldPhotos/MyPhoto.jpg")
        self.assertEqual(len(result.new), 0)

    def test_strict_detects_rename_with_no_prior_hash_index_entry(self):
        """The spec's MANDATORY DEEP TEST (section 6): a file backed up
        by a plain FAST run (which never hashes/records anything) is then
        renamed on the PC. The very next STRICT run must still recognize
        it as already backed up via content -- it must NOT re-copy it,
        even though the hash index has no entry for it yet."""
        # FAST backup already happened; PC file exists under its OLD name
        # and the hash index is completely empty (as FAST never writes to it).
        local = self._local("A.m4a", size=100, mtime=1000.0)
        from src import verifier
        real_digest = verifier.local_sha256(local.local_path)

        # PC file gets renamed -- same bytes, new name, same directory.
        renamed_path = os.path.join(self.tmpdir.name, "My_Completely_Different_Name.m4a")
        os.replace(local.local_path, renamed_path)
        renamed_local = LocalFileEntry(
            rel_path="My_Completely_Different_Name.m4a", local_path=renamed_path,
            size=100, mtime=1000.0,
        )

        source = {"A.m4a": self._remote("A.m4a", size=100)}
        dest = {"My_Completely_Different_Name.m4a": renamed_local}
        self.adb.remote_sha256.return_value = real_digest

        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.STRICT, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        self.assertEqual(len(result.already_backed_up), 1)
        self.assertEqual(result.already_backed_up[0].matched_at_rel_path,
                          "My_Completely_Different_Name.m4a")
        self.assertEqual(len(result.new), 0)
        self.assertEqual(len(result.changed), 0)

        # And the match got persisted, so a THIRD run wouldn't even need
        # to re-hash the PC side to find it again.
        self.assertEqual(
            self.hash_index.lookup("job1", real_digest),
            "My_Completely_Different_Name.m4a",
        )

    def test_strict_does_not_trust_stale_hash_index_hint(self):
        """If the index points at a PC file that no longer exists (or no
        longer matches), it must be discarded, never trusted blindly --
        the file should fall through to ordinary new/changed handling."""
        # Index claims some content lives at a path that isn't in dest_finished at all.
        self.hash_index.record("job1", "cafebabe" * 8, 100, "Gone/vanished.jpg")

        source = {"DCIM/Camera/IMG002.jpg": self._remote("DCIM/Camera/IMG002.jpg", size=100)}
        dest = {}  # nothing on the PC
        self.adb.remote_sha256.return_value = "cafebabe" * 8

        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.STRICT, adb=self.adb, serial="SER1",
            hash_index=self.hash_index,
        )
        # Hint was stale -> falls through to a normal "new" classification.
        self.assertEqual(len(result.new), 1)
        self.assertEqual(len(result.already_backed_up), 0)

    def test_strict_needs_verification_when_device_has_no_sha256sum(self):
        """A device with no sha256sum binary can't fulfill STRICT's
        promise -- must surface as NEEDS_VERIFICATION, never silently
        treated as already-backed-up or silently downgraded to FAST."""
        source = {"a.jpg": self._remote("a.jpg", size=100, mtime=1000)}
        dest = {"a.jpg": self._local("a.jpg", size=100, mtime=1000.0)}
        self.adb.remote_sha256.return_value = None
        result = compare(
            "job1", source, dest, set(), self.state,
            mode=VerificationMode.STRICT, adb=self.adb, serial="SER1",
        )
        self.assertEqual(len(result.needs_verification), 1)
        self.assertEqual(len(result.already_backed_up), 0)
        self.assertEqual(len(result.changed), 0)


class HashIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "hashes.sqlite3")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_record_and_lookup_roundtrip(self):
        idx = HashIndex(self.db_path)
        idx.record("job1", "abc123", 100, "some/path.jpg")
        self.assertEqual(idx.lookup("job1", "abc123"), "some/path.jpg")
        idx.close()

    def test_lookup_missing_returns_none(self):
        idx = HashIndex(self.db_path)
        self.assertIsNone(idx.lookup("job1", "nope"))
        idx.close()

    def test_forget_removes_entry(self):
        idx = HashIndex(self.db_path)
        idx.record("job1", "abc123", 100, "some/path.jpg")
        idx.forget("job1", "abc123")
        self.assertIsNone(idx.lookup("job1", "abc123"))
        idx.close()

    def test_survives_corrupted_db_file(self):
        """'If the hash database is missing, corrupted, or stale ->
        rebuild it safely.' -- must never raise, must never block the
        backup, just quietly starts fresh."""
        with open(self.db_path, "wb") as f:
            f.write(b"this is not a sqlite database, just garbage bytes")
        idx = HashIndex(self.db_path)  # must not raise
        idx.record("job1", "abc123", 100, "some/path.jpg")
        self.assertEqual(idx.lookup("job1", "abc123"), "some/path.jpg")
        idx.close()

    def test_rebuild_from_entries_replaces_jobs_rows_only(self):
        idx = HashIndex(self.db_path)
        idx.record("job1", "aaa", 10, "old1.jpg")
        idx.record("job2", "bbb", 20, "keep_me.jpg")
        idx.rebuild_from_entries("job1", [("ccc", 30, "new1.jpg")])
        self.assertIsNone(idx.lookup("job1", "aaa"))
        self.assertEqual(idx.lookup("job1", "ccc"), "new1.jpg")
        self.assertEqual(idx.lookup("job2", "bbb"), "keep_me.jpg")  # untouched
        idx.close()


if __name__ == "__main__":
    unittest.main()
