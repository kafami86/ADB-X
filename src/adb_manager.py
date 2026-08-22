"""
adb_manager.py

Thin, careful wrapper around adb.exe. Every subprocess call goes through
here so quoting/encoding/error-handling is consistent and centralized.

Design notes:
- We always invoke adb with an argument LIST (never shell=True on our side),
  so Windows never re-tokenizes paths with spaces/parentheses/unicode.
- The single string we build for the remote `find ... -exec stat ...`
  command is a POSIX-shell string that the *device's* /system/bin/sh will
  parse -- so remote paths are single-quoted with '\'' escaping, which is
  unrelated to (and safe from) Windows quoting rules.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional

from .models import RemoteFileEntry
from .logger import get_logger

log = get_logger()


class AdbError(RuntimeError):
    pass


class AdbNotFoundError(AdbError):
    pass


class DeviceNotFoundError(AdbError):
    pass


class DeviceDisconnectedError(AdbError):
    """Raised when a device that was previously connected is no longer
    reachable (USB unplugged, phone locked out of debugging, etc). This is
    distinct from a slow-but-alive command, which simply keeps running."""
    pass


@dataclass
class DeviceInfo:
    serial: str
    state: str          # "device", "unauthorized", "offline"
    model: Optional[str] = None


def _posix_quote(path: str) -> str:
    """Single-quote a path for the device's POSIX shell."""
    return "'" + path.replace("'", "'\\''") + "'"


class AdbManager:
    def __init__(self, adb_path: str = "adb", timeout: int = 60):
        self.adb_path = adb_path
        self.timeout = timeout

    # ---------------------------------------------------------------- core
    def _run(self, args: List[str], timeout, use_default: bool = False) -> subprocess.CompletedProcess:
        """timeout=None means "wait forever" and is honored as such --
        callers doing inventory scans rely on this. use_default is only set
        internally when the caller genuinely wants self.timeout."""
        cmd = [self.adb_path] + args
        effective_timeout = self.timeout if use_default else timeout
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
            )
        except FileNotFoundError as e:
            raise AdbNotFoundError(
                f"Could not find '{self.adb_path}'. Install platform-tools and/or "
                f"set the correct adb_path in config.json."
            ) from e
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb command timed out: {' '.join(cmd)}") from e
        return result

    # ------------------------------------------------------------ devices
    def list_devices(self) -> List[DeviceInfo]:
        result = self._run(["devices", "-l"], timeout=None, use_default=True)
        if result.returncode != 0:
            raise AdbError(f"'adb devices' failed: {result.stderr.strip()}")

        devices: List[DeviceInfo] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            model = None
            for tok in parts[2:]:
                if tok.startswith("model:"):
                    model = tok.split(":", 1)[1]
            devices.append(DeviceInfo(serial=serial, state=state, model=model))
        return devices

    def get_device_model(self, serial: str) -> str:
        out = self.shell(serial, "getprop ro.product.model")
        return out.strip() or serial

    # -------------------------------------------------------------- shell
    def shell(self, serial: str, command: str, timeout: Optional[int] = "__default__") -> str:
        """Run a single shell command string on the device and return stdout.
        timeout=None means wait forever (used for inventory scans); leaving
        timeout unset uses self.timeout (fine for short commands like
        getprop or existence checks)."""
        if timeout == "__default__":
            result = self._run(["-s", serial, "shell", command], timeout=None, use_default=True)
        else:
            result = self._run(["-s", serial, "shell", command], timeout=timeout)
        if result.returncode != 0 and result.stderr.strip():
            log.debug("adb shell stderr (serial=%s): %s", serial, result.stderr.strip())
        return result.stdout

    # ------------------------------------------------------------- listing
    def find_files(self, serial: str, remote_root: str) -> List[RemoteFileEntry]:
        """
        Recursively list files under remote_root using a single adb shell
        round-trip. Returns [] if the path doesn't exist.
        Tries a toybox/BusyBox 'stat -c' form first, falls back to a
        GNU-findutils '-printf' form for the rare device that has it instead.

        IMPORTANT: inventory scans must never time out (a large folder can
        legitimately take minutes or hours), so every shell() call here
        passes timeout=None explicitly rather than falling back to
        self.timeout.
        """
        root_q = _posix_quote(remote_root.rstrip("/") or "/")

        primary = f"find {root_q} -type f -exec stat -c '%s|%Y|%n' {{}} \\;"
        fallback = f"find {root_q} -type f -printf '%s|%T@|%p\\n'"

        entries = self._parse_find_output(self.shell(serial, primary, timeout=None), remote_root)
        if entries:
            return entries

        # Check whether the root simply doesn't exist / is empty, vs. the
        # command syntax being unsupported on this device.
        exists_check = self.shell(serial, f"[ -e {root_q} ] && echo EXISTS || echo MISSING", timeout=None)
        if "MISSING" in exists_check:
            return []

        # Try fallback syntax before giving up.
        entries = self._parse_find_output(self.shell(serial, fallback, timeout=None), remote_root, mtime_float=True)
        return entries

    # ---------------------------------------------------------- connectivity
    def is_device_connected(self, serial: str) -> bool:
        """Cheap, bounded check (does NOT use the unbounded scan timeout)
        used only to detect disconnection, never during a live scan."""
        try:
            devices = self.list_devices()
        except AdbError:
            return False
        return any(d.serial == serial and d.state == "device" for d in devices)

    def wait_for_reconnect(
        self,
        serial: str,
        poll_interval: float = 2.0,
        on_waiting: Optional[callable] = None,
        on_reconnected: Optional[callable] = None,
        cancel_flag=None,
    ) -> None:
        """Blocks until `serial` is connected and ready again. Calls
        on_waiting() once when the wait begins and on_reconnected() once it
        succeeds, so the UI layer can print the required disconnect/
        reconnect messages. Never gives up on its own -- only a cancel_flag
        (e.g. Ctrl+C) can interrupt it."""
        if on_waiting:
            on_waiting()
        while not self.is_device_connected(serial):
            if cancel_flag is not None and cancel_flag.is_set():
                raise AdbError("Cancelled while waiting for device reconnection.")
            time.sleep(poll_interval)
        if on_reconnected:
            on_reconnected()

    @staticmethod
    def _parse_find_output(raw: str, remote_root: str, mtime_float: bool = False) -> List[RemoteFileEntry]:
        entries: List[RemoteFileEntry] = []
        root = remote_root.rstrip("/")
        for line in raw.splitlines():
            line = line.rstrip("\r")
            if not line or "|" not in line:
                continue
            try:
                size_s, mtime_s, remote_path = line.split("|", 2)
                size = int(size_s)
                mtime = int(float(mtime_s)) if mtime_float else int(mtime_s)
            except (ValueError, IndexError):
                log.debug("Skipping unparseable find line: %r", line)
                continue

            if remote_path.startswith(root + "/"):
                rel = remote_path[len(root) + 1:]
            elif remote_path == root:
                rel = remote_path.rsplit("/", 1)[-1]
            else:
                rel = remote_path.lstrip("/")

            entries.append(
                RemoteFileEntry(rel_path=rel, remote_path=remote_path, size=size, mtime=mtime)
            )
        return entries

    def remote_sha256(self, serial: str, remote_path: str) -> Optional[str]:
        """Returns the sha256 hex digest computed ON the device, or None if
        the device has no sha256sum binary."""
        out = self.shell(serial, f"sha256sum {_posix_quote(remote_path)}")
        out = out.strip()
        if not out or "not found" in out.lower() or "permission denied" in out.lower():
            return None
        digest = out.split()[0]
        if len(digest) == 64:
            return digest.lower()
        return None

    # ------------------------------------------------------------- pull
    def start_pull(self, serial: str, remote_path: str, local_path: str) -> subprocess.Popen:
        """Starts `adb pull` asynchronously so the caller can poll progress
        by watching local_path grow on disk."""
        cmd = [self.adb_path, "-s", serial, "pull", remote_path, local_path]
        try:
            return subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as e:
            raise AdbNotFoundError(f"Could not find '{self.adb_path}'.") from e
