import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scanner import scan_destination
from src.adb_manager import AdbManager


class ScanDestinationTests(unittest.TestCase):
    def test_separates_finished_and_part_files(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "sub folder (2)")
            os.makedirs(sub)
            with open(os.path.join(d, "a.jpg"), "wb") as f:
                f.write(b"x" * 10)
            with open(os.path.join(sub, "b.mp4.part"), "wb") as f:
                f.write(b"x" * 5)

            finished, parts = scan_destination(d)

            self.assertIn("a.jpg", finished)
            self.assertEqual(finished["a.jpg"].size, 10)
            self.assertIn("sub folder (2)/b.mp4", parts)

    def test_creates_missing_destination_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "does_not_exist_yet")
            finished, parts = scan_destination(target)
            self.assertEqual(finished, {})
            self.assertEqual(parts, set())
            self.assertTrue(os.path.isdir(target))


class AdbFindParseTests(unittest.TestCase):
    def test_parses_stat_output_with_unicode_and_spaces(self):
        raw = (
            "12345|1700000000|/sdcard/DCIM/Camera/IMG (1).jpg\n"
            "999|1700000001|/sdcard/DCIM/Camera/\u0639\u06a9\u0633.jpg\n"
        )
        entries = AdbManager._parse_find_output(raw, "/sdcard/DCIM/Camera")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].rel_path, "IMG (1).jpg")
        self.assertEqual(entries[0].size, 12345)
        self.assertEqual(entries[1].rel_path, "\u0639\u06a9\u0633.jpg")

    def test_ignores_blank_and_malformed_lines(self):
        raw = "\n12345|1700000000|/sdcard/Camera/a.jpg\nnot a valid line\n"
        entries = AdbManager._parse_find_output(raw, "/sdcard/Camera")
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
