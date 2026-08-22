import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.adb_manager import AdbManager


class InventoryScanNeverTimesOutTests(unittest.TestCase):
    """Regression test: the inventory scan (find_files) must always pass
    timeout=None down to subprocess.run, regardless of the AdbManager's
    configured default timeout. A slow scan must never be aborted."""

    def test_find_files_uses_unbounded_timeout(self):
        adb = AdbManager(adb_path="adb", timeout=5)  # tiny default on purpose
        captured_timeouts = []

        def fake_run(cmd, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            class R:
                returncode = 0
                stdout = "100|1700000000|/sdcard/x/a.jpg\n"
                stderr = ""
            return R()

        with mock.patch("subprocess.run", fake_run):
            adb.find_files("SERIAL", "/sdcard/x")

        self.assertTrue(captured_timeouts, "expected at least one adb call")
        self.assertTrue(
            all(t is None for t in captured_timeouts),
            f"inventory scan must use timeout=None for every call, got {captured_timeouts}",
        )

    def test_short_commands_still_use_bounded_default(self):
        adb = AdbManager(adb_path="adb", timeout=5)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            class R:
                returncode = 0
                stdout = "Pixel\n"
                stderr = ""
            return R()

        with mock.patch("subprocess.run", fake_run):
            adb.get_device_model("SERIAL")

        self.assertEqual(captured["timeout"], 5)


class WaitForReconnectTests(unittest.TestCase):
    def test_calls_waiting_then_reconnected_and_blocks_until_connected(self):
        adb = AdbManager(adb_path="adb")
        states = [False, False, False, True]

        def fake_connected(serial):
            return states.pop(0)

        adb.is_device_connected = fake_connected
        events = []
        adb.wait_for_reconnect(
            "SERIAL",
            poll_interval=0.001,
            on_waiting=lambda: events.append("waiting"),
            on_reconnected=lambda: events.append("reconnected"),
        )
        self.assertEqual(events, ["waiting", "reconnected"])
        self.assertEqual(states, [])  # polled exactly until True

    def test_does_not_call_reconnected_if_never_reconnects_and_cancelled(self):
        import threading
        adb = AdbManager(adb_path="adb")
        adb.is_device_connected = lambda serial: False
        cancel = threading.Event()
        cancel.set()
        events = []
        with self.assertRaises(Exception):
            adb.wait_for_reconnect(
                "SERIAL",
                poll_interval=0.001,
                on_waiting=lambda: events.append("waiting"),
                on_reconnected=lambda: events.append("reconnected"),
                cancel_flag=cancel,
            )
        self.assertEqual(events, ["waiting"])


if __name__ == "__main__":
    unittest.main()
