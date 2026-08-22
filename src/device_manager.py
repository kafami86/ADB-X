"""
device_manager.py

Resolves which physical Android device to talk to, for both the
interactive menu and unattended (--auto) runs. Never assumes "the only
device plugged in" is correct if a specific serial was configured --
in auto mode that must match exactly, otherwise we abort loudly rather
than silently backing up the wrong phone.
"""

from __future__ import annotations

from typing import List, Optional

from .adb_manager import AdbManager, DeviceInfo, DeviceNotFoundError, AdbError
from .logger import get_logger

log = get_logger()


class DeviceManager:
    def __init__(self, adb: AdbManager):
        self.adb = adb

    def list_ready_devices(self) -> List[DeviceInfo]:
        devices = self.adb.list_devices()
        return [d for d in devices if d.state == "device"]

    def resolve(self, preferred_serial: Optional[str] = None, interactive: bool = True) -> DeviceInfo:
        """
        Returns the DeviceInfo to use, or raises DeviceNotFoundError.

        - If preferred_serial is given, it MUST match a connected, ready
          device (this is what --auto / .bat runs rely on for safety).
        - If not given and exactly one ready device is connected, use it.
        - If not given and multiple are connected: in interactive mode the
          caller (ui.py) should prompt; here we just raise so the caller
          can present a choice.
        """
        all_devices = self.adb.list_devices()
        ready = [d for d in all_devices if d.state == "device"]

        if preferred_serial:
            for d in ready:
                if d.serial == preferred_serial:
                    return d
            unauth = [d for d in all_devices if d.serial == preferred_serial]
            if unauth:
                raise DeviceNotFoundError(
                    f"Device '{preferred_serial}' is connected but in state "
                    f"'{unauth[0].state}' (check for an on-screen USB debugging "
                    f"authorization prompt on the phone)."
                )
            raise DeviceNotFoundError(
                f"Configured device serial '{preferred_serial}' is not connected. "
                f"Connected ready devices: {[d.serial for d in ready] or 'none'}"
            )

        if len(ready) == 1:
            return ready[0]

        if len(ready) == 0:
            raise DeviceNotFoundError(
                "No authorized Android device found. Check the USB cable, that "
                "'File Transfer' / USB debugging is enabled, and accept the "
                "authorization prompt on the phone."
            )

        # multiple devices, no preference given
        raise DeviceNotFoundError(
            f"Multiple devices connected ({[d.serial for d in ready]}) and no "
            f"device_serial configured. Set 'device_serial' in config.json, or "
            f"pass --serial on the command line."
        )
