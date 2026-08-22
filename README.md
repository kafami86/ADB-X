# ⚡ ADB-X

### The Android ADB Specialist

**ADB-X** is a professional Android-to-PC backup system built around **Android Debug Bridge (ADB)**.

It is designed for reliable **incremental backups**, intelligent file detection, multiple verification modes, automated jobs, resilient ADB transfers, and a polished terminal interface.

> **Master the connection. Control the data.**

---

## ✨ Features

### 📦 Incremental Backup

ADB-X does not blindly copy everything every time.

It compares the Android device against the existing PC backup and determines which files actually need to be transferred.

Typical states include:

* 🆕 New files
* ✅ Already backed up
* 🔄 Changed files
* ❌ Failed transfers

This makes repeated backups significantly more efficient than performing a full copy every time.

---

### 🧠 Multiple Verification Modes

ADB-X supports three verification modes:

| Mode     | Description                                                   |
| -------- | ------------------------------------------------------------- |
| `fast`   | Fast path/size/metadata-based verification                    |
| `normal` | Metadata-first verification with deeper checks when necessary |
| `strict` | Strict SHA-256 content identity verification                  |

#### FAST

Designed for frequent backups where speed is important.

```text
--mode fast
```

Uses lightweight file information to avoid unnecessary deep verification.

#### NORMAL

Balances performance and verification depth.

```text
--mode normal
```

Metadata is checked first and deeper verification is performed when required.

#### STRICT

Designed for maximum content identity verification.

```text
--mode strict
```

Uses SHA-256 content identity to determine whether files are truly identical.

---

## 🔐 File Identity

ADB-X is designed to avoid relying exclusively on filenames.

A file can be renamed on the Android device without necessarily becoming a completely new file from a content-identity perspective.

Depending on the selected verification mode, ADB-X can use combinations of:

* File path
* Filename
* File size
* Modification metadata
* Content identity
* SHA-256 hashing

This allows deeper verification modes to detect situations where filename-based comparison alone would fail.

> FAST mode prioritizes speed.
> STRICT mode prioritizes content identity.

---

## 📂 Recursive Folder Support

ADB-X works recursively with directories.

For example:

```text
/sdcard/DCIM/Camera
├── IMG_001.jpg
├── IMG_002.jpg
└── 2026
    ├── IMG_003.jpg
    └── IMG_004.jpg
```

The complete directory structure can be preserved in the PC backup.

---

# 🖥️ Requirements

## Windows

ADB-X requires:

* Windows 10/11
* Python 3.x
* Android Debug Bridge (`adb`)
* USB debugging enabled on the Android device
* ADB authorization granted on the phone

Check Python:

```bat
python --version
```

Check ADB:

```bat
adb version
```

Check connected devices:

```bat
adb devices
```

Expected:

```text
List of devices attached
AEEIRO7LRCMJIZKN    device
```

If the device shows:

```text
unauthorized
```

unlock the phone and accept the USB debugging authorization dialog.

---

# 📱 Android Setup

## 1. Enable Developer Options

On most Android devices:

```text
Settings
→ About phone
→ MIUI/HyperOS version
→ Tap several times
```

until Developer Options are enabled.

## 2. Enable USB Debugging

Go to:

```text
Settings
→ Developer options
→ USB debugging
```

Enable it.

## 3. Connect the Phone

Connect the Android device using USB.

Run:

```bat
adb devices
```

Authorize the computer on the phone if requested.

---

# 🚀 Installation

Clone the repository:

```bat
git clone <YOUR_REPOSITORY_URL>
cd ADB-X
```

If the project provides dependencies:

```bat
pip install -r requirements.txt
```

Verify the application:

```bat
python main.py --help
```

You should see:

```text
usage: backup [-h] [--config CONFIG] [--version]
              [--job NAME | --run-all | --list-devices | --list-jobs]
              [--auto]
              [--serial SERIAL]
              [--mode {fast,normal,strict}]
              [--delete]
              [--verify-hash]
              [--no-verify-hash]
              [--retry N]
              [--workers N]
              [--verbose]
              [--add-job NAME SOURCE DEST]
              [--remove-job NAME]
```

---

# 🎮 Interactive Mode

The easiest way to use ADB-X is the interactive interface:

```bat
python main.py
```

The application opens the main dashboard.

The general navigation flow is:

```text
MAIN MENU
   │
   ├── 📂 View Jobs
   │      │
   │      ├── 📷 Camera
   │      ├── 🖼️ Albums
   │      └── 🎙️ Recordings
   │
   ├── 🚀 Run All Jobs
   ├── 📊 Backup Status
   ├── ⚙️ Settings
   └── 🚪 Exit
```

Jobs are intentionally kept inside **View Jobs** so the main dashboard remains clean.

---

# 📂 Jobs

A job defines:

```text
Android source
      ↓
PC destination
      ↓
Backup configuration
```

Example:

```text
Camera
Source:
    /sdcard/DCIM/Camera

Destination:
    F:\Backups\Camera
```

Another example:

```text
Recordings
Source:
    /sdcard/Recordings

Destination:
    F:\Backups\Recordings
```

---

# 🧰 Command-Line Usage

ADB-X can also be controlled directly from the command line.

## Run a specific job

```bat
python main.py --job Camera
```

## Run all enabled jobs

```bat
python main.py --run-all
```

## List connected devices

```bat
python main.py --list-devices
```

## List configured jobs

```bat
python main.py --list-jobs
```

---

# ⚡ Verification Modes

The verification mode can be overridden for a single run.

### FAST

```bat
python main.py --job Camera --mode fast
```

### NORMAL

```bat
python main.py --job Camera --mode normal
```

### STRICT

```bat
python main.py --job Camera --mode strict
```

The command-line mode overrides the configured mode for that run.

---

# 🤖 Automatic Mode

ADB-X provides a non-interactive mode designed for:

* `.bat` scripts
* Windows Task Scheduler
* Scheduled backups
* Automated workflows

Use:

```bat
python main.py --job Camera --auto
```

`--auto` means:

* No Y/N confirmation
* No interactive job selection
* No interactive prompts
* Safe for automation

---

# 🔥 Automated Multi-Job Backup

For example, to automatically back up:

```text
Camera
Albums
Recordings
```

in sequence:

```bat
@echo off

cd /d "%~dp0"

echo.
echo ==========================================
echo              ADB-X AUTO BACKUP
echo ==========================================
echo.

python main.py --job Camera --mode fast --auto

if errorlevel 1 (
    echo Camera backup failed.
    exit /b %ERRORLEVEL%
)

python main.py --job Albums --mode fast --auto

if errorlevel 1 (
    echo Albums backup failed.
    exit /b %ERRORLEVEL%
)

python main.py --job Recordings --mode fast --auto

if errorlevel 1 (
    echo Recordings backup failed.
    exit /b %ERRORLEVEL%
)

echo.
echo ==========================================
echo        ALL BACKUPS COMPLETED
echo ==========================================
echo.

pause
```

This executes the jobs sequentially:

```text
Camera
   ↓
Albums
   ↓
Recordings
```

No Y/N confirmation is required because `--auto` is enabled.

---

# 🧪 Hash Verification

Force SHA-256 verification:

```bat
python main.py --job Camera --verify-hash
```

Disable SHA-256 verification:

```bat
python main.py --job Camera --no-verify-hash
```

These options override the normal verification behavior for that run.

---

# 🔄 Retry Control

Retry behavior can be overridden:

```bat
python main.py --job Camera --retry 3
```

This is useful when working with unstable USB connections or devices.

---

# 👥 Parallel Workers

ADB-X supports multiple transfer workers.

Override the configured value:

```bat
python main.py --job Camera --workers 4
```

Workers are an internal transfer-performance mechanism.

They are intentionally not required to be displayed in the user-facing UI.

---

# 📊 Live Backup Interface

During a backup, ADB-X provides a live terminal interface.

The important information is displayed in a compact format:

```text
📦 BACKUP [30/14000] IMG_20260822_080531_very_long_name_...
⚡ 92.4 MB/s │ 📊 4.82/25.3 GB │ ⏱ ETA 00:03:48
```

### `[30/14000]`

Means:

```text
30     = current file
14000  = total files
```

The interface does not intentionally spam the terminal with one permanent line per file.

The current file is updated inside the live interface.

---

# ⚡ Transfer Speed

ADB-X reports the actual transfer rate in:

```text
MB/s
```

Example:

```text
⚡ 92.4 MB/s
```

The value is calculated from transferred data and elapsed time rather than being a static placeholder.

---

# 📄 Long Filenames

Long filenames are automatically shortened for display.

Example:

```text
📦 BACKUP [30/14000] IMG_20260822_080531_very_long_filename_...
```

Short filenames remain complete:

```text
📦 BACKUP [31/14000] IMG_20260822_080532.mp4
```

The displayed `...` does **not** rename or modify the actual file.

The real filename remains unchanged on disk.

---

# 🔌 Device Reconnection

ADB-X is designed to handle temporary ADB disconnections.

If the cable is disconnected during a backup, the interface can report:

```text
🔌 ADB CONNECTION LOST
⏳ Waiting for device to reconnect...
```

After the device becomes available:

```text
🟢 ADB CONNECTION RESTORED
🔄 Resuming backup...
```

This prevents a temporary USB interruption from silently looking like a frozen application.

---

# 🛡️ Safe Incremental Behavior

ADB-X is designed around a simple principle:

```text
Android
   ↓
Compare
   ↓
Existing PC backup?
   ├── YES → Skip
   └── NO  → Transfer
```

The goal is to avoid retransferring files that have already been backed up.

For deeper verification, additional file identity information can be used.

---

# ⚠️ Important: Do Not Modify Backup Files Manually

For the most reliable incremental behavior:

**Do not manually rename, move or modify files inside the backup destination unless you understand how the selected verification mode identifies them.**

The backup system maintains its own understanding of previously transferred data.

If files are manually changed outside ADB-X, the program may correctly interpret them as missing, changed, or requiring verification.

---

# 🗑️ Delete Mode

ADB-X also supports:

```bat
--delete
```

Example:

```bat
python main.py --job Camera --delete
```

This enables removal of PC files previously written by that job when they are no longer present on the Android source.

### ⚠️ Use carefully

Deletion is fundamentally different from normal incremental backup.

Normal backup:

```text
Phone → PC
```

Delete mode can additionally remove files from:

```text
PC
```

when they are determined to no longer exist on the device.

---

# ➕ Adding Jobs

A job can be added from the command line:

```bat
python main.py --add-job Camera /sdcard/DCIM/Camera "F:\Backups\Camera"
```

Example:

```bat
python main.py --add-job Recordings /sdcard/Recordings "F:\Backups\Recordings"
```

---

# ➖ Removing Jobs

Remove a configured job:

```bat
python main.py --remove-job Camera
```

---

# 📋 Configuration

The default configuration file is:

```text
config.json
```

ADB-X can use a custom configuration file:

```bat
python main.py --config my-config.json
```

This is useful when maintaining multiple backup profiles.

---

# 🧩 Custom Configuration

The project is designed around configurable jobs rather than hard-coded
backup locations.

A typical job contains information conceptually similar to:

```text
Job
├── Name
├── Android Source
├── PC Destination
├── Enabled
└── Verification / Backup Settings
```

Use the application's job management interface whenever possible rather
than manually editing configuration values without understanding their
meaning.

---

# 📝 Logging

ADB-X maintains logs for backup operations.

Logs are useful for investigating:

* Failed transfers
* ADB disconnects
* Verification failures
* Retry attempts
* Unexpected device behavior
* Interrupted backups

When reporting a problem, include the relevant log information rather
than only the final error message.

---

# 🔍 Troubleshooting

## Device not detected

Run:

```bat
adb devices
```

If nothing appears:

1. Check the USB cable.
2. Check USB connection mode.
3. Make sure USB Debugging is enabled.
4. Unlock the Android device.
5. Accept the RSA authorization prompt.
6. Restart ADB if necessary.

Try:

```bat
adb kill-server
adb start-server
adb devices
```

---

## Device shows `unauthorized`

Unlock the phone and accept:

```text
Allow USB debugging?
```

Then run:

```bat
adb devices
```

again.

---

## Backup appears slow

Large directories containing thousands of files can require significant
time to scan.

This is expected because the application must inspect enough information
to determine what actually requires backup.

Performance depends on:

* Number of files
* USB connection
* Android filesystem performance
* ADB performance
* Storage speed
* Selected verification mode
* File sizes

FAST mode is intended for frequent backups where minimizing verification
overhead is important.

STRICT mode performs deeper content verification and can therefore take
significantly longer.

---

## Why is scanning slower with many small files?

A directory containing:

```text
14,000 files
```

can be slower to inspect than a directory containing:

```text
100 large files
```

even if the total storage size is similar.

The bottleneck is often file metadata enumeration and ADB filesystem
operations rather than raw USB transfer speed.

---

# 🏗️ Architecture

Conceptually, ADB-X is divided into several responsibilities:

```text
┌─────────────────────────────┐
│       Interactive UI        │
├─────────────────────────────┤
│        Job Manager          │
├─────────────────────────────┤
│   Incremental Detection     │
├─────────────────────────────┤
│ Verification / Hash Engine  │
├─────────────────────────────┤
│      Transfer Engine        │
├─────────────────────────────┤
│          ADB Layer          │
└─────────────────────────────┘
              │
              ▼
        Android Device
```

This separation allows the terminal interface to evolve without
unnecessarily changing the underlying backup engine.

---

# 🎯 Design Philosophy

ADB-X follows several principles:

### 1. Don't copy what already exists

Incremental backup should avoid unnecessary transfers.

### 2. Speed when possible

FAST mode should minimize unnecessary deep verification.

### 3. Identity when necessary

Deeper verification modes can use stronger file identity mechanisms.

### 4. Automation

A backup tool should work both interactively and unattended.

### 5. Visibility

The user should always know:

```text
What is happening?
Which file is being processed?
How much is done?
How fast is it?
How much remains?
```

### 6. Clean UI

A professional backup tool should not flood the terminal with thousands
of repetitive lines.

---

# 🗺️ Roadmap

Potential future improvements:

* [ ] Automatic scheduled backup profiles
* [ ] Backup history dashboard
* [ ] More advanced transfer statistics
* [ ] Resume interrupted individual files
* [ ] Backup snapshots
* [ ] Storage health information
* [ ] Multiple connected Android devices
* [ ] Profile-based backup configurations
* [ ] Richer verification reports
* [ ] Backup integrity auditing
* [ ] Optional encrypted backup archives

---

# ⚠️ Disclaimer

ADB-X is a backup utility built around Android Debug Bridge.

Always maintain an independent backup of important data.

No backup system should be considered the only copy of irreplaceable
files.

Before using destructive options such as:

```text
--delete
```

verify your source and destination paths carefully.

---

# 📜 License

Choose and add an appropriate open-source license for this project.

Recommended options include:

* MIT
* Apache-2.0
* GPL-3.0

Example:

```text
MIT License
```

---

# ⚡ ADB-X

**The Android ADB Specialist.**

```text
CONNECT.
SCAN.
VERIFY.
BACK UP.
```

> **Master the connectionf. Control the data.**
