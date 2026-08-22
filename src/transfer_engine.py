"""
transfer_engine.py

Executes the actual copy for one file:

  1. Pull the remote file into "<final_name>.part" (never directly into
     the final name -- so a half-copied file can never be mistaken for a
     complete one, even if we crash mid-write).
  2. While the pull runs, poll the growing .part file's size on a
     background thread to drive live progress/speed/ETA -- this does not
     depend on parsing adb's own progress text, whose format has changed
     across adb versions.
  3. On process exit, verify size (always) and hash (optional).
  4. Only on success: atomically os.replace() .part -> final name, and
     record it in the state DB.
  5. On failure: leave the .part file in place (or remove it, depending
     on the failure type) and retry up to retry_count times with backoff.

A stale .part left over from a previous, interrupted run is ALWAYS
discarded and re-pulled from scratch -- adb pull cannot resume a partial
file, so trusting partial bytes would risk silently keeping corrupt data.
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from .adb_manager import AdbManager, AdbError
from .models import DiffEntry, TransferOutcome, GlobalSettings
from .state_manager import StateManager
from . import verifier
from .logger import get_logger

log = get_logger()

PART_SUFFIX = ".part"


@dataclass
class ProgressUpdate:
    rel_path: str
    bytes_done: int
    bytes_total: int
    speed_bps: float
    attempt: int
    worker_id: int = 0


ProgressCallback = Callable[[ProgressUpdate], None]


class TransferEngine:
    def __init__(self, adb: AdbManager, settings: GlobalSettings, state: StateManager):
        self.adb = adb
        self.settings = settings
        self.state = state
        self._hash_unsupported_warned = False
        self._warn_lock = threading.Lock()
        self.on_disconnect = None
        self.on_reconnect = None
        # Set by BackupJob to a ReconnectCoordinator when running with a
        # worker pool, so that N concurrently-disconnected workers wait
        # together and fire on_disconnect/on_reconnect exactly once
        # between them, instead of once each. None (default) preserves
        # today's single-caller behavior (used by tests / single-worker
        # runs), falling back to self.adb.wait_for_reconnect directly.
        self.reconnect_coordinator = None

    def transfer(
        self,
        job_name: str,
        serial: str,
        entry: DiffEntry,
        dest_root: str,
        progress_cb: Optional[ProgressCallback] = None,
        cancel_flag: Optional[threading.Event] = None,
        worker_id: int = 0,
    ) -> TransferOutcome:
        remote = entry.remote
        rel_path = entry.rel_path
        final_path = os.path.join(dest_root, rel_path.replace("/", os.sep))
        part_path = final_path + PART_SUFFIX

        os.makedirs(os.path.dirname(final_path) or dest_root, exist_ok=True)

        # Always discard a stale partial before starting a fresh attempt --
        # adb pull can't resume, so partial bytes are never trustworthy.
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError as e:
                log.warning("Could not remove stale part file %s: %s", part_path, e)

        last_error = None
        attempt = 1
        while attempt <= self.settings.retry_count:
            if cancel_flag is not None and cancel_flag.is_set():
                return TransferOutcome(rel_path, False, error="cancelled", retries_used=attempt - 1, worker_id=worker_id)

            try:
                self._pull_with_progress(serial, remote.remote_path, part_path, remote.size, rel_path, attempt, progress_cb, worker_id)
            except AdbError as e:
                if not self.adb.is_device_connected(serial):
                    # Not an ordinary transient failure -- the cable came
                    # out. Wait here and retry the SAME attempt number once
                    # reconnected, rather than burning a retry slot while
                    # the phone is physically disconnected.
                    #
                    # With a worker pool, several workers can hit this at
                    # once. If a coordinator is set, route through it so
                    # on_disconnect/on_reconnect fire exactly once for the
                    # whole batch and every worker waits on the same
                    # event, instead of each one independently polling and
                    # each one calling the UI callbacks.
                    log.warning("Device disconnected mid-transfer (%s); waiting for reconnect.", rel_path)
                    if self.reconnect_coordinator is not None:
                        self.reconnect_coordinator.wait_for_reconnect(
                            self.adb, serial, cancel_flag=cancel_flag
                        )
                    else:
                        self.adb.wait_for_reconnect(
                            serial,
                            on_waiting=self.on_disconnect,
                            on_reconnected=self.on_reconnect,
                            cancel_flag=cancel_flag,
                        )
                    if os.path.exists(part_path):
                        os.remove(part_path)  # stale partial from the interrupted pull
                    continue  # do NOT increment attempt -- this wasn't a real failure
                last_error = str(e)
                log.warning("Pull failed (attempt %d/%d) for %s: %s", attempt, self.settings.retry_count, rel_path, e)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            if not verifier.verify_size(part_path, remote.size):
                last_error = "size mismatch after transfer (interrupted or truncated)"
                log.warning("%s: %s", rel_path, last_error)
                if os.path.exists(part_path):
                    os.remove(part_path)
                self._sleep_backoff(attempt)
                attempt += 1
                continue

            digest = None
            if self.settings.verify_hash:
                hash_ok = verifier.verify_hash(self.adb, serial, remote.remote_path, part_path)
                if hash_ok is None:
                    with self._warn_lock:
                        if not self._hash_unsupported_warned:
                            log.warning("Device has no sha256sum binary; falling back to size-only verification.")
                            self._hash_unsupported_warned = True
                elif hash_ok is False:
                    last_error = "hash mismatch after transfer"
                    log.warning("%s: %s", rel_path, last_error)
                    if os.path.exists(part_path):
                        os.remove(part_path)
                    self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                else:
                    digest = verifier.local_sha256(part_path)

            # Success: atomic rename into place.
            os.replace(part_path, final_path)
            self.state.mark_backed_up(job_name, rel_path, remote.size, remote.mtime, digest)
            return TransferOutcome(rel_path, True, bytes_transferred=remote.size, retries_used=attempt - 1, worker_id=worker_id)

        return TransferOutcome(rel_path, False, error=last_error or "unknown error", retries_used=self.settings.retry_count, worker_id=worker_id)

    # ------------------------------------------------------------ internals
    def _pull_with_progress(self, serial, remote_path, part_path, expected_size, rel_path, attempt, progress_cb, worker_id=0):
        proc = self.adb.start_pull(serial, remote_path, part_path)

        stop = threading.Event()
        poller = threading.Thread(
            target=self._poll_progress,
            args=(part_path, expected_size, rel_path, attempt, progress_cb, stop, worker_id),
            daemon=True,
        )
        poller.start()

        # Drain adb's own stdout/stderr so the pipe never fills and blocks it.
        if proc.stdout:
            for _ in proc.stdout:
                pass

        returncode = proc.wait()
        stop.set()
        poller.join(timeout=2)

        if returncode != 0:
            raise AdbError(f"adb pull exited with code {returncode} for {remote_path}")

    @staticmethod
    def _poll_progress(part_path, expected_size, rel_path, attempt, progress_cb, stop_event, worker_id=0):
        # Each in-flight transfer runs its own poller thread against its
        # own .part file (a unique path per worker), so there is no shared
        # state here to race on -- this only ever touches this one file
        # and calls progress_cb with this worker's own slot id, which the
        # UI aggregator keys progress by.
        if progress_cb is None:
            return
        last_bytes = 0
        last_time = time.time()
        while not stop_event.is_set():
            time.sleep(0.25)
            try:
                current = os.path.getsize(part_path)
            except OSError:
                current = 0
            now = time.time()
            elapsed = max(now - last_time, 1e-6)
            speed = (current - last_bytes) / elapsed
            progress_cb(ProgressUpdate(rel_path, current, expected_size, max(speed, 0), attempt, worker_id))
            last_bytes, last_time = current, now
        # final snapshot
        try:
            current = os.path.getsize(part_path)
        except OSError:
            current = last_bytes
        progress_cb(ProgressUpdate(rel_path, current, expected_size, 0, attempt, worker_id))

    def _sleep_backoff(self, attempt: int):
        delay = self.settings.retry_backoff_seconds * attempt
        time.sleep(delay)


class ReconnectCoordinator:
    """Shared across all worker threads in one parallel backup run.

    Without this, if the USB cable is pulled while N workers are each
    mid-pull, every worker independently notices the disconnect at
    roughly the same moment and would each: print a disconnect banner,
    poll `adb devices` on its own, and print a reconnect banner -- N of
    each, racing/interleaving in the console.

    With this, the first worker to notice becomes the "leader" for this
    disconnect episode: it fires on_disconnect() once, then polls for
    reconnection while every other worker that hits the same episode just
    blocks on a threading.Event. When the device comes back, the leader
    fires on_reconnected() once and releases everyone.

    Thread safety: state transitions are guarded by self._lock. The
    "waiting" flag and event are only ever flipped by the leader while
    holding the lock; followers only ever read them (to decide whether to
    become leader) under the lock, then release it before blocking on the
    event -- so no follower can block on a stale/already-set event from a
    previous episode.
    """

    def __init__(self, on_disconnect=None, on_reconnect=None):
        self.on_disconnect = on_disconnect
        self.on_reconnect = on_reconnect
        self._lock = threading.Lock()
        self._waiting = False
        self._reconnected_event = threading.Event()

    def wait_for_reconnect(self, adb: AdbManager, serial: str, poll_interval: float = 2.0, cancel_flag=None):
        with self._lock:
            if self._waiting:
                # Someone else is already the leader for this disconnect
                # episode -- just wait for them to signal reconnection.
                is_leader = False
            else:
                is_leader = True
                self._waiting = True
                self._reconnected_event.clear()

        if not is_leader:
            self._reconnected_event.wait()
            return

        # Leader path. A straggler worker can reach here after the device
        # has already reconnected (e.g. it was slower to notice the
        # original failure) -- in that case there's no real episode to
        # report, so skip the banners entirely rather than printing a
        # disconnect/reconnect pair for a "disconnect" that's already
        # over.
        if adb.is_device_connected(serial):
            with self._lock:
                self._waiting = False
                self._reconnected_event.set()
            return

        if self.on_disconnect:
            self.on_disconnect()
        while not adb.is_device_connected(serial):
            if cancel_flag is not None and cancel_flag.is_set():
                with self._lock:
                    self._waiting = False
                    self._reconnected_event.set()  # release any followers too
                raise AdbError("Cancelled while waiting for device reconnection.")
            time.sleep(poll_interval)

        if self.on_reconnect:
            self.on_reconnect()
        with self._lock:
            self._waiting = False
            self._reconnected_event.set()
