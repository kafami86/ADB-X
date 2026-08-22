#!/usr/bin/env python3
"""
Android ADB Backup
==================

Entry point. Supports:
  - No arguments -> interactive menu (see src/ui.py)
  - --job NAME --auto        -> run one job non-interactively
  - --run-all                -> run every enabled job non-interactively
  - --list-devices           -> print connected devices and exit
  - --add-job / --remove-job -> manage jobs from the command line

Exit codes (important for .bat files / Task Scheduler):
  0 = success, nothing failed
  1 = completed but at least one file failed to transfer
  2 = configuration or device error (nothing was attempted)
  3 = unexpected/fatal error
"""

from __future__ import annotations

import argparse
import shutil
import sys
from typing import Optional

from src.adb_manager import AdbManager, AdbError, DeviceNotFoundError, AdbNotFoundError
from src.backup_job import BackupJob, JobSummary
from src.config_manager import ConfigManager, ConfigError
from src.device_manager import DeviceManager
from src.hash_index import HashIndex
from src.logger import setup_logger
from src.models import JobConfig, VerificationMode
from src.state_manager import StateManager

APP_NAME = "Android ADB Backup"
APP_VERSION = "1.0.0"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backup",
        description=f"{APP_NAME} v{APP_VERSION} -- incremental Android-to-PC backup over ADB.",
    )
    p.add_argument("--config", default="config.json", help="Path to config.json (default: ./config.json)")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--job", metavar="NAME", help="Run a single job by name")
    mode.add_argument("--run-all", action="store_true", help="Run every enabled job")
    mode.add_argument("--list-devices", action="store_true", help="List connected ADB devices and exit")
    mode.add_argument("--list-jobs", action="store_true", help="List configured jobs and exit")

    p.add_argument("--auto", action="store_true", help="Non-interactive: no prompts, safe for .bat/Task Scheduler")
    p.add_argument("--serial", metavar="SERIAL", help="Override the device serial for this run")
    p.add_argument(
        "--mode", choices=["fast", "normal", "strict"], default=None,
        help="Verification mode for this run: fast (path+size+metadata, default), "
             "normal (metadata first, hash when needed), strict (SHA-256 content identity). "
             "Overrides the per-job and global default for this run only.",
    )
    p.add_argument("--delete", action="store_true", help="Also remove PC files this job previously wrote that are no longer on the device")
    p.add_argument("--verify-hash", action="store_true", help="Force SHA-256 verification for this run")
    p.add_argument("--no-verify-hash", action="store_true", help="Disable SHA-256 verification for this run")
    p.add_argument("--retry", type=int, metavar="N", help="Override retry count for this run")
    p.add_argument("--workers", type=int, metavar="N", help="Override number of parallel transfer workers for this run (default: config value, normally 4)")
    p.add_argument("--verbose", action="store_true", help="Verbose console output (in addition to the log file)")

    p.add_argument("--add-job", nargs=3, metavar=("NAME", "SOURCE", "DEST"), help="Add a job and exit, e.g. --add-job Camera /sdcard/DCIM/Camera F:\\Backups\\Camera")
    p.add_argument("--remove-job", metavar="NAME", help="Remove a job by name and exit")
    return p


def cmd_list_devices(adb: AdbManager) -> int:
    try:
        devices = adb.list_devices()
    except (AdbError, AdbNotFoundError) as e:
        print(f"ERROR: {e}")
        return 2
    if not devices:
        print("No devices found.")
        return 0
    for d in devices:
        model = ""
        if d.state == "device":
            try:
                model = f"  ({adb.get_device_model(d.serial)})"
            except AdbError:
                pass
        print(f"{d.serial}\t{d.state}{model}")
    return 0


def cmd_list_jobs(cfg: ConfigManager) -> int:
    if not cfg.jobs:
        print("No jobs configured.")
        return 0
    for j in cfg.jobs:
        flag = "" if j.enabled else " (disabled)"
        print(f"{j.name}{flag}: {j.source}  ->  {j.destination}")
    return 0


def _terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def _truncate_filename(name: str, max_len: int) -> str:
    if max_len <= 3:
        return "..."[:max_len]
    if len(name) <= max_len:
        return name
    return name[: max_len - 3] + "..."


def run_job_headless(bj: BackupJob, serial: str, allow_delete: bool, log, mode: Optional[VerificationMode] = None) -> JobSummary:
    def on_progress(update):
        pass  # per-poll byte progress isn't printed headlessly; only per-file lines below

    def on_file_done(outcome, idx, total):
        # COMPACT SINGLE-LINE progress: carriage-return in place, never a
        # new permanent line per file, never wraps regardless of filename
        # length. Only the CURRENT file is shown -- no history is printed.
        status = "OK" if outcome.success else f"FAIL ({outcome.error})"
        counter = f"[{idx}/{total}]"
        name = outcome.rel_path.replace("\\", "/").rsplit("/", 1)[-1]
        width = _terminal_width()
        budget = max(width - len(counter) - len(status) - 4, 8)
        name = _truncate_filename(name, budget)
        line = f"{counter} {name}  {status}"
        # Pad to overwrite any leftover characters from a longer previous
        # line, then carriage-return (no newline) so the next update
        # redraws in place.
        sys.stdout.write("\r" + line.ljust(width)[:width])
        sys.stdout.flush()
        if idx == total or not outcome.success:
            # Leave a real newline after the very last file, or on a
            # failure (so the FAIL detail isn't overwritten and lost).
            sys.stdout.write("\n")
            sys.stdout.flush()

    def on_disconnect():
        print()
        print("🔌 ADB DEVICE DISCONNECTED")
        print("⏳ Waiting for the phone to reconnect...")
        print("📱 Please reconnect the USB cable and unlock your phone.")

    def on_reconnect():
        print("✅ DEVICE RECONNECTED")
        print("🔄 Resuming backup...")

    summary = bj.run(
        serial=serial,
        auto_confirm=True,
        confirm_cb=None,
        progress_cb=on_progress,
        file_done_cb=on_file_done,
        allow_delete=allow_delete,
        on_disconnect=on_disconnect,
        on_reconnect=on_reconnect,
        mode=mode,
    )
    return summary


def print_summary_line(summary: JobSummary):
    if summary.error and summary.compare_result is None:
        print(f"[{summary.job_name}] ERROR: {summary.error}")
        return
    if summary.had_nothing_to_do:
        print(f"[{summary.job_name}] Everything is already backed up. No new or changed files found.")
        return
    print(
        f"[{summary.job_name}] Transferred: {summary.transferred}  "
        f"Skipped: {summary.skipped}  Failed: {summary.failed}"
    )


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Console log level is driven ONLY by --verbose. It used to also turn
    # on for every non-headless run ("not args.auto"), i.e. every normal
    # interactive session -- which meant the per-file "[job] OK <file>"
    # INFO log line (backup_job.py, fired once per transferred file) was
    # printed straight to stdout on its own permanent line, underneath
    # and independent of the Rich Live progress panel in ui.py. That's
    # what caused "a new line per file" even though the Live panel itself
    # was already correct. The file log (always DEBUG, rotating) is
    # unaffected either way -- this only controls what echoes to the
    # console outside of the Live UI.
    log = setup_logger(verbose_console=args.verbose)

    try:
        cfg = ConfigManager(args.config).load()
    except ConfigError as e:
        print(f"Configuration error: {e}")
        return 2

    if args.retry is not None:
        cfg.settings.retry_count = args.retry
    if args.workers is not None:
        cfg.settings.workers = max(1, min(args.workers, 16))
    if args.verify_hash:
        cfg.settings.verify_hash = True
    if args.no_verify_hash:
        cfg.settings.verify_hash = False

    run_mode: Optional[VerificationMode] = None
    if args.mode is not None:
        run_mode = VerificationMode.from_str(args.mode)

    adb = AdbManager(adb_path=cfg.settings.adb_path)

    if args.add_job:
        name, source, dest = args.add_job
        try:
            job = JobConfig(name=name, source=source, destination=dest)
            if run_mode is not None:
                job.verification_mode = run_mode.value
            cfg.add_job(job)
            print(f"Added job '{name}'.")
            return 0
        except ConfigError as e:
            print(f"ERROR: {e}")
            return 2

    if args.remove_job:
        if cfg.remove_job(args.remove_job):
            print(f"Removed job '{args.remove_job}'.")
            return 0
        print(f"No such job: {args.remove_job}")
        return 2

    if args.list_devices:
        return cmd_list_devices(adb)

    if args.list_jobs:
        return cmd_list_jobs(cfg)

    # ---- Determine mode ----
    if not args.job and not args.run_all:
        # Interactive mode.
        from src.ui import InteractiveApp
        try:
            app = InteractiveApp(cfg, adb)
            app.main_loop()
            return 0
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 0
        except AdbNotFoundError as e:
            print(f"ERROR: {e}")
            return 2

    # ---- Non-interactive (--job / --run-all) ----
    device_manager = DeviceManager(adb)
    try:
        device = device_manager.resolve(preferred_serial=args.serial or cfg.settings.device_serial)
    except (DeviceNotFoundError, AdbNotFoundError) as e:
        print(f"ERROR: {e}")
        log.error(str(e))
        return 2

    state = StateManager(cfg.settings.state_db)
    hash_index = HashIndex(cfg.settings.hash_index_db) if cfg.settings.hash_index_db else None

    jobs_to_run = []
    if args.job:
        job = cfg.get_job(args.job)
        if not job:
            print(f"ERROR: no such job '{args.job}'. Use --list-jobs to see configured jobs.")
            return 2
        jobs_to_run = [job]
    else:  # --run-all
        jobs_to_run = [j for j in cfg.jobs if j.enabled]
        if not jobs_to_run:
            print("No enabled jobs configured.")
            return 0

    overall_failed = False
    for job_cfg in jobs_to_run:
        serial = job_cfg.device_serial or device.serial
        try:
            device_for_job = device_manager.resolve(preferred_serial=serial)
        except DeviceNotFoundError as e:
            print(f"[{job_cfg.name}] ERROR: {e}")
            overall_failed = True
            continue

        bj = BackupJob(job_cfg, cfg.settings, adb, state, hash_index=hash_index)
        effective_mode = run_mode or VerificationMode.from_str(job_cfg.verification_mode or cfg.settings.verification_mode)
        print(f"Running job '{job_cfg.name}' ({job_cfg.source} -> {job_cfg.destination}, mode={effective_mode.value}, workers={cfg.settings.workers}) ...")
        summary = run_job_headless(bj, device_for_job.serial, allow_delete=args.delete, log=log, mode=run_mode)
        print_summary_line(summary)
        if summary.failed > 0 or (summary.error and summary.compare_result is None):
            overall_failed = True

    state.close()
    if hash_index is not None:
        hash_index.close()
    return 1 if overall_failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 - last-resort fatal handler
        print(f"FATAL: {e}")
        sys.exit(3)
