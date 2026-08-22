"""
ui.py

Rich-based interactive console UI: main menu, job list, live transfer
progress, scan/compare summaries, and simple wizards for adding/editing
jobs and settings. Kept separate from backup_job.py so the same job logic
can be driven headlessly by --auto/--run-all without any of this.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress_bar import ProgressBar
from rich.prompt import Prompt, Confirm, IntPrompt

from .adb_manager import AdbManager, AdbError, DeviceNotFoundError
from .backup_job import BackupJob, JobSummary
from .config_manager import ConfigManager, ConfigError
from .device_manager import DeviceManager
from .hash_index import HashIndex
from .models import CompareResult, JobConfig, TransferOutcome, VerificationMode
from .state_manager import StateManager
from .transfer_engine import ProgressUpdate

console = Console()

MODE_LABELS = {
    VerificationMode.FAST: "⚡️ FAST",
    VerificationMode.NORMAL: "⚖️ NORMAL",
    VerificationMode.STRICT: "🔬 STRICT / DEEP",
}

MODE_DESCRIPTIONS = {
    VerificationMode.FAST: "Fastest daily backup.\nPath + size + metadata.",
    VerificationMode.NORMAL: "Balanced speed and reliability.\nMetadata first, hash only when needed.",
    VerificationMode.STRICT: "Maximum verification.\nSHA-256 content identity.",
}


def human_size(n: int) -> str:
    n = float(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class LiveTransferReporter:
    """Aggregates progress from N concurrently-running transfer workers
    into ONE panel that redraws IN PLACE -- never a new block per file,
    never one panel per worker, never a permanent line per filename.

    Two callbacks feed this from worker threads:
      - on_progress(update): fired ~4x/sec per in-flight file by that
        file's own poller thread (see transfer_engine._poll_progress).
      - on_file_done(outcome, idx, total): fired once per finished file,
        from the thread draining as_completed() in BackupJob.run().

    Both callbacks, and the render they trigger, happen under self._lock.
    That's the only synchronization this needs: all state this panel
    displays lives in this one object, and nothing here is ever read or
    written without the lock, so concurrent workers can never produce a
    torn/inconsistent frame.

    The current-file line is COMPACT and ALWAYS ONE LINE:
        [30/14000] IMG_20260822_080531.mp4              92.4 MB/s
    A long filename is truncated with "..." rather than ever wrapping --
    the "[N/TOTAL]" counter is always shown in full; the filename is what
    gives way when space is tight. This holds the same whether there are
    10 files or 100,000: only the CURRENT file is ever shown, never a
    running history of every filename seen so far.
    """

    def __init__(self, console: Console, job_name: str, total: int, workers: int, total_bytes: int = 0, mode: Optional[VerificationMode] = None):
        self.console = console
        self.job_name = job_name
        self.total = total
        self.workers = workers
        self.total_bytes = total_bytes
        self.mode = mode

        self._lock = threading.Lock()
        self.completed = 0
        self.failed = 0
        self._active = {}  # worker_id -> {rel_path, bytes_done, bytes_total, speed}
        self._last_active_worker = None
        self._last_failure = None  # (rel_path, error) of the most recent failure, if any
        self._completed_bytes = 0
        self._live = Live(self._render(), console=console, refresh_per_second=8, transient=False)

    def __enter__(self):
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._live.__exit__(exc_type, exc, tb)

    # ---------------------------------------------------------- callbacks
    def on_progress(self, update: ProgressUpdate):
        with self._lock:
            self._active[update.worker_id] = {
                "rel_path": update.rel_path,
                "bytes_done": update.bytes_done,
                "bytes_total": update.bytes_total,
                "speed": update.speed_bps,
            }
            self._last_active_worker = update.worker_id
            self._live.update(self._render())

    def on_file_done(self, outcome: TransferOutcome, idx: int, total: int):
        with self._lock:
            self.completed = idx
            self.total = total
            if outcome.success:
                self._completed_bytes += outcome.bytes_transferred
            else:
                self.failed += 1
                self._last_failure = (outcome.rel_path, outcome.error)
            # Clear this worker's slot -- it's picking up a new file next,
            # which will repopulate the slot via on_progress.
            self._active.pop(outcome.worker_id, None)
            self._live.update(self._render())

    # ------------------------------------------------------------ render
    def _aggregate_speed(self) -> float:
        return sum(w["speed"] for w in self._active.values())

    def _current_line_width(self) -> int:
        # Panel adds ~4 chars of border/padding on each side; be
        # conservative so the line never overflows and wraps.
        width = self.console.size.width if self.console and self.console.size else 100
        return max(width - 8, 20)

    def _render(self):
        remaining = max(self.total - self.completed, 0)
        bar = ProgressBar(total=max(self.total, 1), completed=self.completed, width=None)

        lines = [bar]

        current = self._active.get(self._last_active_worker) if self._last_active_worker is not None else None
        speed = self._aggregate_speed()

        counter = f"[{self.completed + (1 if current else 0)}/{self.total}]"
        lines.append(_compact_progress_line(counter, current, speed, self._current_line_width()))

        if self.total_bytes and speed > 0:
            bytes_done_est = self._completed_bytes + sum(w["bytes_done"] for w in self._active.values())
            remaining_bytes = max(self.total_bytes - bytes_done_est, 0)
            eta_seconds = remaining_bytes / speed
            lines.append(Text(f"ETA: {_format_eta(eta_seconds)}"))

        lines.append(Text(""))
        lines.append(Text(f"Completed: {self.completed}    Remaining: {remaining}    Failed: {self.failed}"))
        if self._last_failure:
            fail_path, fail_err = self._last_failure
            lines.append(Text(f"❌ Failed: {_short_name(fail_path)} ({fail_err})", style="red"))

        if self.workers > 1:
            lines.append(Text(f"Workers: {len(self._active)} active", style="dim"))

        title = f"📦 {self.job_name}"
        if self.mode is not None:
            title += f"  [{MODE_LABELS.get(self.mode, self.mode)}]"
        return Panel(Group(*lines), title=title, border_style="cyan")


class InteractiveApp:
    def __init__(self, cfg: ConfigManager, adb: AdbManager):
        self.cfg = cfg
        self.adb = adb
        self.device_manager = DeviceManager(adb)
        self.state = StateManager(self.cfg.settings.state_db)
        self.hash_index = HashIndex(self.cfg.settings.hash_index_db) if self.cfg.settings.hash_index_db else None

    # --------------------------------------------------------------- menu
    def main_loop(self):
        while True:
            console.rule("[bold cyan]Android ADB Backup")
            self._print_device_status()
            console.print()

            table = Table(show_header=False, box=None, padding=(0, 1))
            for i, job in enumerate(self.cfg.jobs, start=1):
                status = "" if job.enabled else " [dim](disabled)[/dim]"
                table.add_row(f"{i}.", f"{job.name}{status}")
            n = len(self.cfg.jobs)
            table.add_row(f"{n + 1}.", "Add new backup job")
            table.add_row(f"{n + 2}.", "Run all jobs")
            table.add_row(f"{n + 3}.", "Manage jobs (edit/remove)")
            table.add_row(f"{n + 4}.", "Settings")
            table.add_row(f"{n + 5}.", "Exit")
            console.print(Panel(table, title="Backup Jobs", border_style="cyan"))

            choice = Prompt.ask("Select an option", default=str(n + 5))
            if not choice.isdigit():
                continue
            choice = int(choice)

            if 1 <= choice <= n:
                self._run_job_flow(self.cfg.jobs[choice - 1])
            elif choice == n + 1:
                self._add_job_wizard()
            elif choice == n + 2:
                self._run_all_flow()
            elif choice == n + 3:
                self._manage_jobs_menu()
            elif choice == n + 4:
                self._settings_menu()
            elif choice == n + 5:
                console.print("Goodbye.")
                break
            else:
                console.print("[red]Invalid choice.[/red]")

    def _print_device_status(self):
        try:
            devices = self.device_manager.list_ready_devices()
        except AdbError as e:
            console.print(f"[red]ADB error:[/red] {e}")
            return
        if not devices:
            console.print("Device: [red]Not connected[/red]")
            return
        preferred = self.cfg.settings.device_serial
        chosen = None
        if preferred:
            chosen = next((d for d in devices if d.serial == preferred), None)
        if chosen is None and len(devices) == 1:
            chosen = devices[0]
        if chosen:
            model = self.adb.get_device_model(chosen.serial)
            console.print(f"Device: [green]{model}[/green]  ({chosen.serial})")
            console.print("Status: [green]Connected[/green]")
        else:
            console.print(f"Multiple devices connected: {[d.serial for d in devices]}")
            console.print("[yellow]Set a default device_serial in Settings.[/yellow]")

    # ---------------------------------------------------------- job flows
    def _resolve_device_or_warn(self, job: Optional[JobConfig] = None) -> Optional[str]:
        preferred = (job.device_serial if job and job.device_serial else self.cfg.settings.device_serial)
        try:
            device = self.device_manager.resolve(preferred_serial=preferred)
            return device.serial
        except DeviceNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            ready = self.device_manager.list_ready_devices()
            if len(ready) > 1 and not preferred:
                table = Table()
                table.add_column("#")
                table.add_column("Serial")
                table.add_column("Model")
                for i, d in enumerate(ready, start=1):
                    table.add_row(str(i), d.serial, self.adb.get_device_model(d.serial))
                console.print(table)
                idx = IntPrompt.ask("Select device #", default=1)
                if 1 <= idx <= len(ready):
                    return ready[idx - 1].serial
            return None

    def _select_mode(self, job: JobConfig) -> VerificationMode:
        """Verification Mode selection screen. Remembers the choice on the
        job's config so future runs (interactive or --auto) default to it
        until changed again."""
        default_mode = VerificationMode.from_str(
            job.verification_mode or self.cfg.settings.verification_mode
        )
        console.print()
        console.print("[bold]Verification Mode[/bold]")
        order = [VerificationMode.FAST, VerificationMode.NORMAL, VerificationMode.STRICT]
        for i, m in enumerate(order, start=1):
            marker = ">" if m == default_mode else " "
            console.print(f"{marker} {i}. {MODE_LABELS[m]}")
            for line in MODE_DESCRIPTIONS[m].splitlines():
                console.print(f"     {line}")
        choice = Prompt.ask(
            "Select mode (blank = keep current)",
            choices=["1", "2", "3", ""],
            default="",
            show_choices=False,
        )
        if not choice:
            return default_mode
        chosen = order[int(choice) - 1]
        if chosen != default_mode:
            self.cfg.update_job(job.name, verification_mode=chosen.value)
        return chosen

    def _run_job_flow(self, job: JobConfig, mode: Optional[VerificationMode] = None):
        serial = self._resolve_device_or_warn(job)
        if not serial:
            return
        if mode is None:
            mode = self._select_mode(job)
        bj = BackupJob(job, self.cfg.settings, self.adb, self.state, hash_index=self.hash_index)

        console.print(f"\n[bold]Scanning Android...[/bold]")
        console.print(f"[bold]Scanning destination...[/bold]\n")

        reporter_holder = {}

        def confirm_and_prepare(result: CompareResult) -> bool:
            proceed = self._confirm_summary(result)
            if proceed:
                to_transfer = result.to_transfer_including_verification()
                workers = max(1, min(self.cfg.settings.workers or 1, 16))
                reporter = LiveTransferReporter(
                    console, job.name, total=len(to_transfer), workers=workers,
                    total_bytes=result.total_bytes_to_transfer(), mode=result.mode,
                )
                reporter_holder["reporter"] = reporter
                reporter.__enter__()
            return proceed

        def progress_cb(update: ProgressUpdate):
            reporter = reporter_holder.get("reporter")
            if reporter:
                reporter.on_progress(update)

        def file_done_cb(outcome: TransferOutcome, idx: int, total: int):
            reporter = reporter_holder.get("reporter")
            if reporter:
                reporter.on_file_done(outcome, idx, total)

        try:
            summary = bj.run(
                serial=serial,
                auto_confirm=False,
                confirm_cb=confirm_and_prepare,
                progress_cb=progress_cb,
                file_done_cb=file_done_cb,
                on_disconnect=self._on_disconnect,
                on_reconnect=self._on_reconnect,
                mode=mode,
            )
        finally:
            reporter = reporter_holder.get("reporter")
            if reporter:
                reporter.__exit__(None, None, None)

        self._print_final_summary(summary)
        self._wait_for_back()

    def _wait_for_back(self):
        """Every result/status screen (completed, cancelled, nothing-to-
        backup, failed, etc.) must give the user explicit control back
        rather than silently redrawing the main menu underneath them --
        the user must never feel like the screen they were reading just
        got yanked away. Any keypress or blank Enter continues; 'b' works
        too, for anyone used to typing it."""
        console.print()
        Prompt.ask("[B] Back to Main Menu", default="", show_default=False)

    def _on_disconnect(self):
        console.print()
        console.print("[bold red]🔌 ADB DEVICE DISCONNECTED[/bold red]")
        console.print("[yellow]⏳ Waiting for the phone to reconnect...[/yellow]")
        console.print("[yellow]📱 Please reconnect the USB cable and unlock your phone.[/yellow]")

    def _on_reconnect(self):
        console.print("[bold green]✅ DEVICE RECONNECTED[/bold green]")
        console.print("[green]🔄 Resuming backup...[/green]")
        console.print()

    def _run_all_flow(self):
        for job in self.cfg.jobs:
            if not job.enabled:
                continue
            console.rule(f"[bold]{job.name}[/bold]")
            self._run_job_flow(job)

    def _confirm_summary(self, result: CompareResult) -> bool:
        counts = result.counts()
        mode_label = MODE_LABELS.get(result.mode, str(result.mode))
        table = Table(show_header=False, box=None)
        table.add_row("Mode:", mode_label)
        table.add_row("New files:", str(counts["new"]))
        table.add_row("Already backed up:", str(counts["already_backed_up"]))
        table.add_row("Changed files:", str(counts["changed"]))
        table.add_row("Missing files:", str(counts["missing"]))
        table.add_row("Incomplete files:", str(counts["incomplete"]))
        if counts["needs_verification"]:
            table.add_row("Needs verification:", str(counts["needs_verification"]))
        table.add_row("Total to transfer:", str(len(result.to_transfer_including_verification())))
        table.add_row("Total size:", human_size(result.total_bytes_to_transfer()))
        console.print(Panel(table, title="Backup Summary", border_style="cyan"))

        if not result.to_transfer_including_verification():
            console.print("[green]Everything is already backed up.[/green]")
            console.print("[green]No new or changed files found.[/green]")
            return False

        return Confirm.ask("Proceed?", default=True)

    def _print_final_summary(self, summary: JobSummary):
        if summary.error and summary.compare_result is None:
            console.print(f"[red]Job failed:[/red] {summary.error}")
            return
        if summary.compare_result and not summary.compare_result.to_transfer_including_verification():
            return  # already printed by _confirm_summary
        if summary.error:
            console.print(f"[yellow]{summary.error}[/yellow]")
            return

        table = Table(show_header=False, box=None)
        table.add_row("Transferred:", str(summary.transferred))
        table.add_row("Skipped:", str(summary.skipped))
        table.add_row("Failed:", str(summary.failed))
        table.add_row("Verified:", str(summary.transferred))
        total = summary.transferred + summary.skipped + summary.failed
        table.add_row("Total:", str(total))
        style = "green" if summary.failed == 0 else "yellow"
        console.print(Panel(table, title="Backup completed", border_style=style))

    # --------------------------------------------------------- management
    def _add_job_wizard(self):
        console.rule("Add new backup job")
        name = Prompt.ask("Job name (e.g. Camera)").strip()
        if self.cfg.get_job(name):
            console.print("[red]A job with that name already exists.[/red]")
            return
        source = Prompt.ask("Android source path (e.g. /sdcard/DCIM/Camera)").strip()
        destination = Prompt.ask("PC destination folder (e.g. F:\\Backups\\Camera)").strip()
        job = JobConfig(name=name, source=source, destination=destination)
        self.cfg.add_job(job)
        console.print(f"[green]Added job '{name}'.[/green]")

    def _manage_jobs_menu(self):
        if not self.cfg.jobs:
            console.print("[yellow]No jobs configured yet.[/yellow]")
            return
        table = Table()
        table.add_column("#")
        table.add_column("Name")
        table.add_column("Source")
        table.add_column("Destination")
        table.add_column("Enabled")
        for i, j in enumerate(self.cfg.jobs, start=1):
            table.add_row(str(i), j.name, j.source, j.destination, "yes" if j.enabled else "no")
        console.print(table)

        idx = IntPrompt.ask("Select job # to edit/remove (0 to cancel)", default=0)
        if idx == 0 or idx > len(self.cfg.jobs):
            return
        job = self.cfg.jobs[idx - 1]
        action = Prompt.ask(
            "Action", choices=["edit", "remove", "toggle", "cancel"], default="cancel"
        )
        if action == "remove":
            if Confirm.ask(f"Remove job '{job.name}'? (destination files are NOT deleted)", default=False):
                self.cfg.remove_job(job.name)
                console.print("[green]Removed.[/green]")
        elif action == "edit":
            new_source = Prompt.ask("Source", default=job.source)
            new_dest = Prompt.ask("Destination", default=job.destination)
            self.cfg.update_job(job.name, source=new_source, destination=new_dest)
            console.print("[green]Updated.[/green]")
        elif action == "toggle":
            self.cfg.update_job(job.name, enabled=not job.enabled)
            console.print("[green]Toggled.[/green]")

    def _settings_menu(self):
        s = self.cfg.settings
        console.rule("Settings")
        console.print(f"1. adb path: {s.adb_path}")
        console.print(f"2. default device serial: {s.device_serial or '(auto)'}")
        console.print(f"3. retry count: {s.retry_count}")
        console.print(f"4. verify hash (sha256, legacy post-transfer check): {s.verify_hash}")
        console.print(f"5. parallel transfer workers: {s.workers}")
        console.print(f"6. default verification mode: {MODE_LABELS.get(VerificationMode.from_str(s.verification_mode), s.verification_mode)}")
        console.print("7. back")
        choice = Prompt.ask("Select", default="7")
        if choice == "1":
            s.adb_path = Prompt.ask("adb path", default=s.adb_path)
        elif choice == "2":
            devices = self.device_manager.list_ready_devices()
            if devices:
                table = Table()
                table.add_column("#")
                table.add_column("Serial")
                for i, d in enumerate(devices, start=1):
                    table.add_row(str(i), d.serial)
                console.print(table)
            val = Prompt.ask("Serial (blank = auto-detect)", default=s.device_serial or "")
            s.device_serial = val or None
        elif choice == "3":
            s.retry_count = IntPrompt.ask("Retry count", default=s.retry_count)
        elif choice == "4":
            s.verify_hash = Confirm.ask("Verify with SHA-256 (slower, strongest guarantee)?", default=s.verify_hash)
        elif choice == "5":
            s.workers = max(1, min(IntPrompt.ask("Parallel transfer workers (1-16)", default=s.workers), 16))
        elif choice == "6":
            order = [VerificationMode.FAST, VerificationMode.NORMAL, VerificationMode.STRICT]
            for i, m in enumerate(order, start=1):
                console.print(f"  {i}. {MODE_LABELS[m]}")
            idx = IntPrompt.ask("Select default mode", default=order.index(VerificationMode.from_str(s.verification_mode)) + 1)
            if 1 <= idx <= len(order):
                s.verification_mode = order[idx - 1].value
        else:
            return
        self.cfg.save()
        console.print("[green]Saved.[/green]")


def _short_name(rel_path: str, max_len: int = 40) -> str:
    if len(rel_path) <= max_len:
        return rel_path
    return "..." + rel_path[-(max_len - 3):]


def _truncate_filename(name: str, max_len: int) -> str:
    """Truncates from the END with '...' -- never wraps. Preserves the
    front of the name (usually the most identifying part) and always
    leaves room for the ellipsis itself."""
    if max_len <= 3:
        return "..."[:max_len]
    if len(name) <= max_len:
        return name
    return name[: max_len - 3] + "..."


def _compact_progress_line(counter: str, current: Optional[dict], speed: float, width: int) -> Text:
    """Builds the single-line "[N/TOTAL] filename ... speed" display.
    The counter is ALWAYS shown in full; the filename is truncated (never
    wrapped) to whatever space is left, and the speed suffix is dropped
    first if space is extremely tight -- the counter is what must never
    be sacrificed."""
    if current is None:
        return Text(f"{counter} -")

    name = current["rel_path"].replace("\\", "/").rsplit("/", 1)[-1]
    speed_suffix = f"  {human_size(speed)}/s" if speed and speed > 0 else ""

    # Reserve space for "counter" + one space + speed_suffix; whatever's
    # left goes to the filename (truncated with "..." if needed).
    reserved = len(counter) + 1 + len(speed_suffix)
    name_budget = max(width - reserved, 8)

    if len(name) > name_budget:
        name = _truncate_filename(name, name_budget)
        # Filename was truncated -- if we're still somehow over width
        # (extremely narrow terminal), drop the speed suffix entirely
        # rather than let the line wrap.
        if len(counter) + 1 + len(name) + len(speed_suffix) > width:
            speed_suffix = ""

    return Text(f"{counter} {name}{speed_suffix}")


def _format_eta(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
