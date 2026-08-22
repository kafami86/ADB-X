# Android ADB Backup

A reliable, incremental Android → PC backup tool that runs over official
ADB. Unlike a simple `adb pull -a`, it never blindly re-copies everything:
every run scans both sides, figures out exactly what's new, changed,
missing, or left over from an interrupted transfer, and only moves those
files. It works for **any** folder on the device, not just photos/videos.

Backup is strictly one-way (Android → PC). Nothing on the phone is ever
modified or deleted, and root is never required.

## Verification modes

Every backup job runs in one of three selectable verification modes,
which change how the *decision algorithm itself* works, not just the UI:

- **⚡️ FAST** (default) — path + size + modification time only. Never
  hashes, never reads file contents. The right choice for daily backups
  of large folders (10k+ files).
- **⚖️ NORMAL** — same cheap metadata check first; escalates to a real
  SHA-256 comparison only for the specific file whose metadata looks
  ambiguous, and can use an optional local hash index to recognize an
  obvious rename/move without hashing everything.
- **🔬 STRICT / DEEP** — content identity via streamed SHA-256 is the
  only thing that decides "already backed up". Filename and path never
  determine identity, so this mode can detect files that were renamed,
  moved, or placed in a different backup subfolder — as long as the
  content is identical. Large files are always hashed in chunks
  (`hash_chunk_size` in `config.json`, default 4 MB) and never loaded
  into RAM.

Pick a mode per job from the interactive menu, set a job's default in
`config.json` (`"verification_mode": "fast" | "normal" | "strict"`), or
override it for a single run:

```bat
backup.exe --job Camera --mode fast
backup.exe --job Camera --mode normal
backup.exe --job Camera --mode strict
backup.exe --run-all --mode strict
```

With no `--mode` given, each job uses its own configured mode, falling
back to the global `settings.verification_mode` default (`fast`).

NORMAL and STRICT modes use an optional, supplementary local hash index
(`hash_index.sqlite3` by default) purely as a performance hint for
rename/move detection — it is never the source of truth. If it's
missing, deleted, or corrupted, it's rebuilt automatically and the
backup itself stays correct either way.

---

## 1. Requirements

- **Windows 10/11**
- **Python 3.9+** ([python.org](https://www.python.org/downloads/windows/) — check "Add python.exe to PATH" during install)
- **Android Platform Tools** (`adb.exe`) — download from
  https://developer.android.com/tools/releases/platform-tools and either:
  - add the extracted folder to your Windows `PATH`, **or**
  - put `adb.exe` (and `AdbWinApi.dll`, `AdbWinUsbApi.dll`) next to `main.py`, **or**
  - point `adb_path` in `config.json` at the full path to `adb.exe`.
- On the phone: **Settings → About phone → tap "Build number" 7 times** to
  enable Developer Options, then **Settings → Developer options → USB
  debugging → ON**. Connect via USB and accept the "Allow USB debugging?"
  prompt (tick "Always allow from this computer" so unattended `.bat` runs
  don't hang waiting for a tap).

## 2. Install

```bat
cd android_backup
pip install -r requirements.txt
```

Copy `config.example.json` to `config.json` and edit the `jobs` list —
each job is just a name, an Android source path, and a Windows
destination folder:

```json
{
  "name": "Camera",
  "source": "/sdcard/DCIM/Camera",
  "destination": "F:\\Backups\\Camera"
}
```

You can also manage jobs from the command line or the interactive menu
instead of hand-editing JSON (see below).

## 3. Interactive use

```bat
python main.py
```

or double-click `interactive.bat`. You'll get a menu:

```
Android ADB Backup
────────────────────────────────────────
Device: POCO M4 Pro
Status: Connected

Backup Jobs
1. Camera
2. Albums
3. Recordings
4. Add new backup job
5. Run all jobs
6. Manage jobs (edit/remove)
7. Settings
8. Exit
```

Selecting a job scans both sides, shows a summary, and asks for
confirmation before transferring:

```
Backup Summary
────────────────────────────────────────
New files:          23
Already backed up:  1842
Changed files:       2
Missing files:       0
Incomplete files:    1
Total to transfer:  26
Total size:         4.7 GB

Proceed? [Y/N]
```

During transfer you get a live progress bar per file (current file,
bytes done, speed, ETA), followed by a final summary:

```
Backup completed
────────────────────────────────────────
Transferred: 26
Skipped: 1842
Failed: 0
Verified: 26
Total: 1868
```

If there's nothing to do, it says so and doesn't touch anything:

```
Everything is already backed up.
No new or changed files found.
```

## 4. Fully automatic mode (for a one-click .bat file)

This is the mode meant for `run_all.bat` / Task Scheduler — **no
keyboard interaction, ever**:

```bat
python main.py --run-all --auto
python main.py --job Camera --auto
python main.py --job "Camera" --auto --verify-hash
```

`run_all.bat` (included) does exactly this and reports success/failure
via its exit code:

```bat
@echo off
cd /d "%~dp0"
python main.py --run-all --auto
pause
```

Exit codes (useful in Task Scheduler or your own scripts):

| Code | Meaning |
|------|---------|
| 0 | Success, nothing failed |
| 1 | Completed, but at least one file failed to transfer (see log) |
| 2 | Configuration or device error — nothing was attempted |
| 3 | Unexpected/fatal error |

In `--auto` mode, if the configured device isn't found (wrong phone
plugged in, cable unplugged, `device_serial` mismatch), the tool exits
with code 2 and logs why — it will **not** guess and back up the wrong
device.

## 5. Command-line reference

```
python main.py [options]

  --job NAME              Run a single job by name
  --run-all               Run every enabled job
  --auto                  Non-interactive: no prompts (required for .bat use)
  --serial SERIAL         Use this device serial for this run
  --delete                Also remove PC files this tool previously wrote
                           that are no longer present on the device
  --verify-hash           Force SHA-256 verification for this run
  --no-verify-hash        Disable SHA-256 verification for this run
  --retry N                Override retry count for this run
  --config PATH            Path to config.json (default: ./config.json)
  --list-devices           List connected ADB devices and exit
  --list-jobs              List configured jobs and exit
  --add-job NAME SRC DEST  Add a job and exit
  --remove-job NAME        Remove a job and exit
  --verbose                Verbose console output
  --version                Show version
  --help                   Show this help
```

Every option also has a `--help` entry: `python main.py --help`.

## 6. How incremental sync works

1. **Scan the Android source** in a single `adb shell` round trip
   (`find … -exec stat -c '%s|%Y|%n' …`), returning size + mtime for
   every file under the configured path.
2. **Scan the PC destination** with a plain filesystem walk (no file
   contents are read at this stage).
3. **Compare** and classify every remote file as:
   - `already_backed_up` — same size on both sides → skipped
   - `new` — not on the PC yet
   - `changed` — size differs from the PC copy
   - `missing` — was backed up before (tracked in the state DB) but the
     local file is gone now (e.g. you deleted it) → re-copied
   - `incomplete` — only a stale `.part` file exists on the PC → discarded
     and re-copied from scratch
4. **Transfer** each file into `<name>.<ext>.part`, verify (size always;
   optional SHA-256 with `verify_hash: true` / `--verify-hash`), then
   atomically rename to the final name. A file is only ever considered
   backed up **after** this verification succeeds.
5. A small SQLite database (`backup_state.sqlite3`) caches this history
   per job to make future runs faster and to distinguish "new" from
   "missing" — but it is never trusted on its own. Every run re-checks
   the real Android and PC filesystems before doing anything.

**Interrupted-transfer example** (exactly the scenario this tool is
built to handle safely):

```
Phone: 1000 files
PC:    600 complete files
Transfer starts on file #601 → USB cable disconnects mid-copy

Next run:
  files 1–600   → already complete, skipped
  file  601     → only a .part exists → discarded, re-copied
  files 602–1000 → missing, copied
```

Nothing is ever marked "backed up" until the corresponding file
physically exists, complete and verified, in the destination folder.

## 7. Safety

- Backup is one-way: Android → PC only. The app never writes to,
  modifies, or deletes anything on the phone.
- PC destination files are **never** deleted automatically. Passing
  `--delete` only removes files *this job previously wrote* that have
  since disappeared from the phone — it will never touch unrelated files
  you've placed in the destination folder, and always logs what it
  removed.
- No root required.

## 8. Multiple devices

```bat
python main.py --list-devices
```

If more than one device is plugged in, either pass `--serial <serial>`,
set a per-job `"device_serial"` in `config.json`, or set the global
default `device_serial` under Settings in the interactive menu. In
`--auto` mode, an ambiguous or mismatched device causes a clean exit
(code 2) rather than guessing.

## 9. Filenames with spaces / Unicode / Persian / parentheses / brackets

Handled throughout: the Android-side listing command single-quotes paths
for the device shell, transfers are invoked with proper argument lists
(never a naive shell string on the Windows side), and all tests include
Unicode/space/parenthesis cases (see `tests/test_scanner.py`).

## 10. Testing

Unit tests cover the comparator (the core sync-decision logic) and the
scanner/parsing logic, including the exact "USB unplugged mid-transfer"
scenario from the spec. No device or `adb` install is required to run
them:

```bat
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

To test against a real device without touching your actual backups,
create a throwaway job pointed at a small test folder first, e.g.:

```bat
python main.py --add-job Test /sdcard/Download/test_folder .\test_backup_out
python main.py --job Test --auto --verbose
```

Then unplug the cable mid-transfer on a larger folder once and re-run
the same job to confirm it resumes correctly instead of re-copying
everything.

## 11. Project layout

```
android_backup/
  main.py                  CLI entry point (interactive + --auto)
  config.example.json      Copy to config.json and edit
  requirements.txt
  run_all.bat               One-click: run every enabled job
  run_job_example.bat       One-click: run a single named job
  interactive.bat            One-click: open the interactive menu
  src/
    adb_manager.py          All adb.exe subprocess calls
    device_manager.py       Device selection/verification
    scanner.py               Source (Android) + destination (PC) listing
    comparator.py             Classifies files: new/changed/missing/incomplete
    transfer_engine.py        .part transfer, progress, verify, retry, rename
    verifier.py                Size + streamed SHA-256 verification
    backup_job.py              Orchestrates one job: scan→compare→transfer
    config_manager.py          JSON config load/save/job CRUD
    state_manager.py            Supplementary SQLite history cache
    logger.py                   Rotating file log + console
    ui.py                        Rich-based interactive terminal UI
    models.py                    Shared dataclasses/enums
  tests/
    test_comparator.py
    test_scanner.py
  logs/
    backup.log                Rotating log (created on first run)
```

## 12. Troubleshooting

- **"Could not find 'adb'"** → install platform-tools and add it to
  `PATH`, or set `adb_path` in `config.json`.
- **"No authorized Android device found"** → check the cable, that USB
  debugging is on, and accept the on-phone authorization prompt.
- **Device found but files not appearing** → double-check the `source`
  path exists on the phone exactly as typed (case-sensitive); use
  `adb shell ls /sdcard/...` to confirm.
- **Hash verification says "device has no sha256sum binary"** → some
  minimal ROMs lack it; the tool automatically falls back to size-only
  verification and logs a one-time warning. Size verification alone is
  sufficient to catch truncated/interrupted transfers.
