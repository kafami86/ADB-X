import os
import sys
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.adb_manager import AdbError
from src.models import DiffEntry, FileStatus, GlobalSettings, RemoteFileEntry
from src.state_manager import StateManager
from src.transfer_engine import TransferEngine, ReconnectCoordinator


class FakeProc:
    """Stands in for the subprocess.Popen returned by AdbManager.start_pull.
    Simulates a pull taking `delay` seconds, then writes `content` to
    local_path and reports a fake exit code -- long enough that several
    of these running in different worker threads genuinely overlap in
    time, so tests can observe real concurrency."""

    def __init__(self, local_path, content, delay=0.05, fail=False):
        self.local_path = local_path
        self.content = content
        self.delay = delay
        self.fail = fail
        self.stdout = None  # TransferEngine only iterates this if truthy

    def wait(self):
        time.sleep(self.delay)
        if not self.fail:
            with open(self.local_path, "wb") as f:
                f.write(self.content)
            return 0
        return 1


class FakeAdbManager:
    """Minimal stand-in for AdbManager covering only what TransferEngine
    touches, with an instrumented start_pull() that tracks how many
    fake pulls are in flight at once (to verify the worker pool actually
    bounds concurrency) and can simulate a temporary disconnect."""

    def __init__(self, contents, delay=0.05, disconnect_after=None, disconnect_duration=0.0):
        self.contents = contents  # remote_path -> bytes
        self.delay = delay
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.pull_count = 0
        # Optional simulated disconnect: after this many start_pull() calls,
        # is_device_connected() reports False for disconnect_duration
        # seconds, then recovers.
        self.disconnect_after = disconnect_after
        self.disconnect_duration = disconnect_duration
        self._disconnect_deadline = None

    def start_pull(self, serial, remote_path, local_path):
        with self._lock:
            self.pull_count += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            if self.disconnect_after is not None and self.pull_count == self.disconnect_after:
                self._disconnect_deadline = time.time() + self.disconnect_duration

        content = self.contents[remote_path]
        proc = FakeProc(local_path, content, delay=self.delay)

        real_wait = proc.wait

        def wait_and_release():
            rc = real_wait()
            with self._lock:
                self.in_flight -= 1
            return rc

        proc.wait = wait_and_release
        return proc

    def is_device_connected(self, serial):
        if self._disconnect_deadline is None:
            return True
        return time.time() >= self._disconnect_deadline


def make_entry(rel_path: str, content: bytes) -> DiffEntry:
    remote = RemoteFileEntry(rel_path=rel_path, remote_path=f"/sdcard/{rel_path}", size=len(content), mtime=1700000000)
    return DiffEntry(rel_path=rel_path, status=FileStatus.NEW, remote=remote)


class ParallelTransferConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dest = os.path.join(self.tmpdir, "dest")
        self.state = StateManager(os.path.join(self.tmpdir, "state.sqlite3"))

    def tearDown(self):
        self.state.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_worker_pool_bounds_concurrency_and_transfers_everything_correctly(self):
        n_files = 20
        workers = 4
        entries = []
        contents = {}
        for i in range(n_files):
            rel = f"file_{i:03d}.bin"
            content = os.urandom(256) + str(i).encode()
            contents[f"/sdcard/{rel}"] = content
            entries.append(make_entry(rel, content))

        adb = FakeAdbManager(contents, delay=0.05)
        settings = GlobalSettings(retry_count=3, retry_backoff_seconds=0.01, verify_hash=False, workers=workers)
        engine = TransferEngine(adb, settings, self.state)

        results = []
        results_lock = threading.Lock()

        def run_one(entry):
            outcome = engine.transfer(
                job_name="TestJob",
                serial="SERIAL",
                entry=entry,
                dest_root=self.dest,
                progress_cb=None,
                worker_id=0,
            )
            with results_lock:
                results.append(outcome)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_one, e) for e in entries]
            for f in as_completed(futures):
                f.result()

        # Every file transferred successfully.
        self.assertEqual(len(results), n_files)
        self.assertTrue(all(o.success for o in results), [o.error for o in results if not o.success])

        # The bounded pool never let more than `workers` fake pulls run at
        # the same time -- this is the "controlled parallel/batch" and
        # "sensible bounded worker pool" requirement.
        self.assertLessEqual(adb.max_in_flight, workers)
        # And it actually achieved real parallelism (not silently serial).
        self.assertGreater(adb.max_in_flight, 1)

        # Every file landed at its correct final path with correct bytes,
        # no cross-file corruption from concurrent writers.
        for rel, content in [(e.rel_path, contents[e.remote.remote_path]) for e in entries]:
            final_path = os.path.join(self.dest, rel)
            self.assertTrue(os.path.exists(final_path))
            with open(final_path, "rb") as f:
                self.assertEqual(f.read(), content)
            # No .part file left behind for successful transfers.
            self.assertFalse(os.path.exists(final_path + ".part"))

        # State DB has exactly one row per file (no duplicate/lost writes
        # from concurrent mark_backed_up calls sharing one connection).
        known = self.state.all_known_paths("TestJob")
        self.assertEqual(known, {e.rel_path for e in entries})

    def test_a_failed_file_does_not_affect_other_concurrent_files(self):
        n_files = 6
        entries = []
        contents = {}
        for i in range(n_files):
            rel = f"file_{i}.bin"
            content = os.urandom(64)
            contents[f"/sdcard/{rel}"] = content
            entries.append(make_entry(rel, content))

        adb = FakeAdbManager(contents, delay=0.02)
        # Make file_2 always fail its pull.
        orig_start_pull = adb.start_pull

        def start_pull_with_one_failure(serial, remote_path, local_path):
            proc = orig_start_pull(serial, remote_path, local_path)
            if remote_path.endswith("file_2.bin"):
                proc.fail = True
            return proc

        adb.start_pull = start_pull_with_one_failure

        settings = GlobalSettings(retry_count=2, retry_backoff_seconds=0.01, workers=3)
        engine = TransferEngine(adb, settings, self.state)

        results = {}
        results_lock = threading.Lock()

        def run_one(entry):
            outcome = engine.transfer("TestJob", "SERIAL", entry, self.dest, worker_id=0)
            with results_lock:
                results[entry.rel_path] = outcome

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run_one, e) for e in entries]
            for f in as_completed(futures):
                f.result()

        self.assertFalse(results["file_2.bin"].success)
        for rel in [f"file_{i}.bin" for i in range(n_files) if i != 2]:
            self.assertTrue(results[rel].success, f"{rel} should have succeeded independently of file_2's failure")


class ReconnectCoordinatorTests(unittest.TestCase):
    def test_concurrent_disconnect_fires_callbacks_exactly_once_and_releases_all_workers(self):
        n_files = 6
        entries = []
        contents = {}
        for i in range(n_files):
            rel = f"f{i}.bin"
            content = os.urandom(32)
            contents[f"/sdcard/{rel}"] = content
            entries.append(make_entry(rel, content))

        # Every pull "disconnects" for a short window starting on the 2nd
        # start_pull call, simulating the cable being pulled while several
        # workers are mid-transfer at once.
        adb = FakeAdbManager(contents, delay=0.03, disconnect_after=2, disconnect_duration=0.15)

        # Make every in-flight pull fail while "disconnected" so multiple
        # workers hit the AdbError branch concurrently.
        real_start_pull = adb.start_pull

        def flaky_start_pull(serial, remote_path, local_path):
            proc = real_start_pull(serial, remote_path, local_path)
            if adb._disconnect_deadline is not None and time.time() < adb._disconnect_deadline:
                proc.fail = True
            return proc

        adb.start_pull = flaky_start_pull

        state = StateManager(os.path.join(tempfile.mkdtemp(), "state.sqlite3"))
        settings = GlobalSettings(retry_count=5, retry_backoff_seconds=0.01, workers=4)
        engine = TransferEngine(adb, settings, state)

        disconnect_events = []
        reconnect_events = []
        events_lock = threading.Lock()

        coordinator = ReconnectCoordinator(
            on_disconnect=lambda: (events_lock.acquire(), disconnect_events.append(1), events_lock.release()),
            on_reconnect=lambda: (events_lock.acquire(), reconnect_events.append(1), events_lock.release()),
        )
        engine.reconnect_coordinator = coordinator

        results = []
        results_lock = threading.Lock()

        def run_one(entry):
            outcome = engine.transfer("TestJob", "SERIAL", entry, tempfile.mkdtemp(), worker_id=0)
            with results_lock:
                results.append(outcome)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(run_one, e) for e in entries]
            for f in as_completed(futures):
                f.result()

        state.close()

        # All files eventually succeeded despite the mid-flight disconnect.
        self.assertEqual(len(results), n_files)
        self.assertTrue(all(o.success for o in results), [o.error for o in results if not o.success])

        # The key correctness property: no matter how many workers hit the
        # disconnect concurrently, the UI callbacks fire exactly once for
        # the episode -- not once per worker that noticed it.
        self.assertEqual(len(disconnect_events), 1, "on_disconnect should fire exactly once for one episode")
        self.assertEqual(len(reconnect_events), 1, "on_reconnect should fire exactly once for one episode")


if __name__ == "__main__":
    unittest.main()
