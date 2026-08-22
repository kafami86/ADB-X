"""
backup_job.py

Orchestrates a single backup job end-to-end: scan both sides, compare,
(optionally ask for confirmation), transfer, and produce a summary.
Framework-agnostic -- it takes callback hooks so both the rich interactive
UI and the silent --auto mode can drive it identically.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .adb_manager import AdbManager, AdbError
from .comparator import compare
from .hash_index import HashIndex
from .models import CompareResult, GlobalSettings, JobConfig, TransferOutcome, VerificationMode, DiffEntry, FileStatus
from .scanner import scan_destination, scan_source
from .state_manager import StateManager
from .transfer_engine import TransferEngine, ProgressUpdate, ReconnectCoordinator
from .logger import get_logger

log = get_logger()

DisconnectCallback = Callable[[], None]
ReconnectCallback = Callable[[], None]


@dataclass
class JobSummary:
    job_name: str
    compare_result: Optional[CompareResult] = None
    outcomes: List[TransferOutcome] = field(default_factory=list)
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    mode: VerificationMode = VerificationMode.FAST

    @property
    def transferred(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def failed(self) -> int:
        return sum(1 for o in self.outcomes if not o.success)

    @property
    def skipped(self) -> int:
        return len(self.compare_result.already_backed_up) if self.compare_result else 0

    @property
    def had_nothing_to_do(self) -> bool:
        return self.compare_result is not None and len(self.compare_result.to_transfer_including_verification()) == 0


ConfirmCallback = Callable[[CompareResult], bool]
ProgressCallback = Callable[[ProgressUpdate], None]
FileDoneCallback = Callable[[TransferOutcome, int, int], None]  # outcome, index, total


class BackupJob:
    def __init__(
        self,
        job_config: JobConfig,
        settings: GlobalSettings,
        adb: AdbManager,
        state: StateManager,
        hash_index: Optional[HashIndex] = None,
    ):
        self.config = job_config
        self.settings = settings
        self.adb = adb
        self.state = state
        # Shared across jobs run in the same process; None disables the
        # rename/move-detection optimization but never affects correctness
        # (comparator.py treats every hash-index lookup as a hint only).
        self.hash_index = hash_index
        self.engine = TransferEngine(adb, settings, state)
        # Gives each pool thread a stable, unique small integer id (0..N-1)
        # the first time it picks up a task, so the progress UI can key
        # per-worker slots consistently across that thread's whole
        # lifetime in the pool -- rather than guessing a slot from
        # submission order, which doesn't reliably match which thread
        # actually executes which task.
        self._worker_local = threading.local()
        self._next_worker_id = 0
        self._worker_id_lock = threading.Lock()

    def _worker_id(self) -> int:
        if not hasattr(self._worker_local, "id"):
            with self._worker_id_lock:
                self._worker_local.id = self._next_worker_id
                self._next_worker_id += 1
        return self._worker_local.id

    def _effective_mode(self, override: Optional[VerificationMode]) -> VerificationMode:
        """Resolution order: explicit run-time override (e.g. CLI --mode)
        > per-job config > global default."""
        if override is not None:
            return override
        if self.config.verification_mode:
            return VerificationMode.from_str(self.config.verification_mode)
        return VerificationMode.from_str(self.settings.verification_mode)

    def run(
        self,
        serial: str,
        auto_confirm: bool = False,
        confirm_cb: Optional[ConfirmCallback] = None,
        progress_cb: Optional[ProgressCallback] = None,
        file_done_cb: Optional[FileDoneCallback] = None,
        allow_delete: bool = False,
        on_disconnect: Optional[DisconnectCallback] = None,
        on_reconnect: Optional[ReconnectCallback] = None,
        cancel_flag=None,
        mode: Optional[VerificationMode] = None,
    ) -> JobSummary:
        effective_mode = self._effective_mode(mode)
        summary = JobSummary(job_name=self.config.name)
        self.engine.on_disconnect = on_disconnect
        self.engine.on_reconnect = on_reconnect
        self.engine.cancel_flag = cancel_flag
        # A fresh coordinator per run: several workers can hit the same
        # physical disconnect at once, and this makes sure on_disconnect/
        # on_reconnect fire exactly once for the episode instead of once
        # per worker that noticed it. The (single-threaded) scan phase
        # above doesn't use this -- only the parallel transfer phase does.
        reconnect_coordinator = ReconnectCoordinator(on_disconnect=on_disconnect, on_reconnect=on_reconnect)
        self.engine.reconnect_coordinator = reconnect_coordinator

        try:
            log.info("[%s] Scanning android source: %s", self.config.name, self.config.source)
            source = self._scan_source_with_reconnect(serial, on_disconnect, on_reconnect, cancel_flag)

            log.info("[%s] Scanning destination: %s", self.config.name, self.config.destination)
            dest_finished, dest_parts = scan_destination(self.config.destination)

            result = compare(
                job_name=self.config.name,
                source=source,
                dest_finished=dest_finished,
                dest_part_paths=dest_parts,
                state=self.state,
                mtime_tolerance_seconds=self.settings.mtime_tolerance_seconds,
                include_extra=allow_delete,
                mode=effective_mode,
                adb=self.adb,
                serial=serial,
                hash_index=self.hash_index,
                hash_chunk_size=self.settings.hash_chunk_size,
            )
            summary.compare_result = result
            summary.mode = effective_mode

        except Exception as e:  # noqa: BLE001 - surfaced to caller/summary
            log.exception("[%s] Scan/compare failed", self.config.name)
            summary.error = str(e)
            summary.finished_at = time.time()
            return summary

        # NEEDS_VERIFICATION entries (e.g. device had no sha256sum in a
        # mode that requires hashing) can't be resolved by metadata alone
        # and must never be silently counted as backed up -- the safe
        # resolution is to re-pull and let the normal transfer-time
        # verification (size, and hash when enabled) settle it for real.
        to_transfer = result.to_transfer_including_verification()

        if not to_transfer and not (allow_delete and result.extra_on_dest):
            summary.finished_at = time.time()
            return summary

        if not auto_confirm and confirm_cb is not None:
            if not confirm_cb(result):
                summary.error = "Cancelled by user."
                summary.finished_at = time.time()
                return summary

        total = len(to_transfer)
        # Bounded worker pool: at most `workers` files are ever mid-pull at
        # once (so at most `workers` adb processes running concurrently),
        # regardless of how large to_transfer is. Clamp to a sane range so
        # a bad config value can't spawn an unreasonable number of
        # concurrent adb pulls against one USB device.
        worker_count = max(1, min(self.settings.workers or 1, 16))

        def _transfer_one(entry):
            return entry, self.engine.transfer(
                job_name=self.config.name,
                serial=serial,
                entry=entry,
                dest_root=self.config.destination,
                progress_cb=progress_cb,
                cancel_flag=cancel_flag,
                worker_id=self._worker_id(),
            )

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix=f"xfer-{self.config.name}") as executor:
            futures = [executor.submit(_transfer_one, entry) for entry in to_transfer]
            completed = 0
            for future in as_completed(futures):
                entry, outcome = future.result()
                completed += 1
                summary.outcomes.append(outcome)
                if file_done_cb:
                    # `completed` here is COMPLETED / TOTAL for the whole
                    # job -- a simple count of finished futures so far, so
                    # it's correct regardless of which file finishes in
                    # which order under parallel execution.
                    file_done_cb(outcome, completed, total)
                if outcome.success:
                    log.info("[%s] OK  %s (%d bytes, %d retries)", self.config.name, entry.rel_path, outcome.bytes_transferred, outcome.retries_used)
                else:
                    log.error("[%s] FAIL %s: %s", self.config.name, entry.rel_path, outcome.error)

        if allow_delete and result.extra_on_dest:
            for diff_entry in result.extra_on_dest:
                try:
                    os.remove(diff_entry.local.local_path)
                    self.state.forget(self.config.name, diff_entry.rel_path)
                    log.info("[%s] DELETED (no longer on device) %s", self.config.name, diff_entry.rel_path)
                except OSError as e:
                    log.warning("[%s] Could not delete %s: %s", self.config.name, diff_entry.rel_path, e)

        summary.finished_at = time.time()
        return summary

    def _scan_source_with_reconnect(self, serial, on_disconnect, on_reconnect, cancel_flag):
        """Runs the (unbounded) android inventory scan. If the device is
        actually unplugged, the underlying adb shell call raises AdbError --
        that is the only signal treated as a disconnect. A scan that simply
        takes a long time keeps running inside scan_source() and never
        reaches this except-block, so a slow scan can never be mistaken for
        a disconnection."""
        while True:
            try:
                return scan_source(self.adb, serial, self.config.source)
            except AdbError:
                if self.adb.is_device_connected(serial):
                    # Failed for some other reason (e.g. permission error),
                    # not a disconnect -- surface the real error.
                    raise
                log.warning("[%s] Device disconnected during scan; waiting for reconnect.", self.config.name)
                self.adb.wait_for_reconnect(
                    serial,
                    on_waiting=on_disconnect,
                    on_reconnected=on_reconnect,
                    cancel_flag=cancel_flag,
                )
                # loop and retry the scan from scratch now that it's back
