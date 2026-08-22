"""
Unit tests for comparator.py -- the logic that decides new/changed/
missing/incomplete/already-backed-up. Uses a throwaway SQLite state DB
per test, no adb/device required.

Run with:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.comparator import compare
from src.models import RemoteFileEntry, LocalFileEntry, FileStatus
from src.state_manager import StateManager


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "state.sqlite3")
        self.state = StateManager(self.db_path)

    def tearDown(self):
        self.state.close()
        self.tmpdir.cleanup()

    def _remote(self, rel, size=100, mtime=1000):
        return RemoteFileEntry(rel_path=rel, remote_path=f"/sdcard/Camera/{rel}", size=size, mtime=mtime)

    def _local(self, rel, size=100, mtime=1000.0):
        return LocalFileEntry(rel_path=rel, local_path=f"/dest/{rel}", size=size, mtime=mtime)

    def test_new_file(self):
        source = {"a.jpg": self._remote("a.jpg")}
        result = compare("job1", source, {}, set(), self.state)
        self.assertEqual(len(result.new), 1)
        self.assertEqual(result.new[0].status, FileStatus.NEW)

    def test_already_backed_up_when_sizes_match(self):
        source = {"a.jpg": self._remote("a.jpg", size=100)}
        dest = {"a.jpg": self._local("a.jpg", size=100)}
        result = compare("job1", source, dest, set(), self.state)
        self.assertEqual(len(result.already_backed_up), 1)
        self.assertEqual(len(result.new), 0)

    def test_changed_when_size_differs(self):
        source = {"a.jpg": self._remote("a.jpg", size=200)}
        dest = {"a.jpg": self._local("a.jpg", size=100)}
        result = compare("job1", source, dest, set(), self.state)
        self.assertEqual(len(result.changed), 1)

    def test_incomplete_when_only_part_file_present(self):
        source = {"a.jpg": self._remote("a.jpg")}
        result = compare("job1", source, {}, {"a.jpg"}, self.state)
        self.assertEqual(len(result.incomplete), 1)
        self.assertEqual(len(result.new), 0)

    def test_missing_when_previously_backed_up_but_now_absent(self):
        self.state.mark_backed_up("job1", "a.jpg", 100, 1000)
        source = {"a.jpg": self._remote("a.jpg")}
        # not in dest_finished, not in dest_parts -> was tracked before -> "missing"
        result = compare("job1", source, {}, set(), self.state)
        self.assertEqual(len(result.missing), 1)
        self.assertEqual(len(result.new), 0)

    def test_interrupted_transfer_scenario(self):
        """
        Simulates the exact scenario from the spec:
          Phone: 1000 files
          PC: 600 complete files
          File 601 was mid-transfer when the cable was pulled (.part only)
          Files 602-1000 were never started
        Expect: 600 already_backed_up, 1 incomplete, 399 new, nothing lost.
        """
        source = {}
        dest = {}
        parts = set()
        for i in range(1000):
            name = f"file_{i:04d}.jpg"
            source[name] = self._remote(name, size=100)
            if i < 600:
                dest[name] = self._local(name, size=100)
            elif i == 600:
                parts.add(name)  # stale .part only, no finished file
            # 602..999 -> nothing on PC at all

        result = compare("job1", source, dest, parts, self.state)
        self.assertEqual(len(result.already_backed_up), 600)
        self.assertEqual(len(result.incomplete), 1)
        self.assertEqual(len(result.new), 399)
        self.assertEqual(len(result.to_transfer()), 400)

    def test_extra_on_dest_only_flags_previously_tracked_files(self):
        # File was backed up before by this tool, but is gone from the phone now.
        self.state.mark_backed_up("job1", "old.jpg", 100, 1000)
        dest = {"old.jpg": self._local("old.jpg"), "unrelated.txt": self._local("unrelated.txt")}
        result = compare("job1", {}, dest, set(), self.state, include_extra=True)
        rel_paths = {e.rel_path for e in result.extra_on_dest}
        self.assertIn("old.jpg", rel_paths)
        self.assertNotIn("unrelated.txt", rel_paths)  # never touch untracked files


if __name__ == "__main__":
    unittest.main()
