# Better ADB Sync

An [rsync](https://wiki.archlinux.org/title/rsync)-like program to sync files between a computer and an Android device

## Installation

Preparation: Make sure BetterADBSync version from PyPi.org is no longer installed:
```
$ pip uninstall BetterADBSync
```

Download and install BetterADBSync fork from GitHub 

```
$ git clone https://github.com/jpstotz/better-adb-sync.git
$ pip install better-adb-sync/
```

## How to use

To push from your computer to your phone use
```
$ adbsync push LOCAL ANDROID
```

To pull from your phone to your computer use
```
$ adbsync pull ANDROID LOCAL
```

Full help is available with `$ adbsync --help`:

```
adbsync --help
usage: adbsync [-h] [--version] [--no-color] [-v | -q] [-n] [-L] [--exclude EXCLUDE] [--exclude-from EXCLUDE_FROM] [--del] [--delete-excluded] [--force] [--show-progress]
               [--adb-encoding ADB_ENCODING] [--adb-bin ADB_BIN] [--adb-flag ADB_FLAG] [--adb-option OPTION VALUE]
               {push,pull,completion,completion-remote-paths} ...

Sync files between a computer and an Android device

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
  -n, --dry-run         Perform a dry run; do not actually copy and delete etc
  -L, --copy-links      Follow symlinks and copy their referent file / directory
  --exclude EXCLUDE     fnmatch pattern to ignore relative to source (reusable)
  --exclude-from EXCLUDE_FROM
                        Filename of file containing fnmatch patterns to ignore relative to source (reusable)
  --del                 Delete files at the destination that are not in the source
  --delete-excluded     Delete files at the destination that are excluded
  --force               Allows files to overwrite folders and folders to overwrite files. This is false by default to prevent large scale accidents
  --show-progress       Show progress from 'adb push' and 'adb pull' commands
  --adb-encoding ADB_ENCODING
                        Which encoding to use when talking to adb. Defaults to UTF-8. Relevant to GitHub issue #22

logging:
  --no-color            Disable colored logging (Linux only)
  -v, --verbose         Increase logging verbosity: -v for debug
  -q, --quiet           Decrease logging verbosity: -q for warning, -qq for error, -qqq for critical, -qqqq for no logging messages

ADB arguments:
  By default ADB works for me without touching any of these, but if you have any specific demands then go ahead. See 'adb --help' for a full list of adb flags and options

  --adb-bin ADB_BIN     Use the given adb binary. Defaults to 'adb' ie whatever is on path
  --adb-flag ADB_FLAG   Add a flag to call adb with, eg '--adb-flag d' for adb -d, that is return an error if more than one device is connected
  --adb-option OPTION VALUE
                        Add an option to call adb with, eg '--adb-option P 5037' for adb -P 5037, that is use port 5037 for the adb server

direction:
  {push,pull,completion,completion-remote-paths}
    push                Push from computer to phone
    pull                Pull from phone to computer
    completion          Print a shell completion script (bash, zsh or fish) to
                        stdout
```

## Performance

Compared with upstream (`jpstotz/better-adb-sync` master), measured on a
OnePlus Nord over USB (adb 37.0.1). Both sides transfer identical bytes;
only the number of adb processes and the planning differ.

| workload | upstream | this branch | difference |
|---|---|---|---|
| pull 19,763 thumbnails (869 MB) | 906 s, 19,764 procs | 150 s, 2 procs | **83% less time (6×)**, 99.99% fewer procs |
| pull 1,051 photos (5.3 GB) | 224 s, 1,052 procs | 168 s, 2 procs | **25% less time**, 99.8% fewer procs |
| push 300 small files | ~24 s, 301 procs | ~7 s, 2 procs | **71% less time**, 99.3% fewer procs |
| pull 300 small files | ~7.4 s, 301 procs | ~0.9 s, 2 procs | **88% less time**, 99.3% fewer procs |
| no-op rerun (up to 19,763 files) | 1 proc | 1 proc | unchanged, ~2 s at 19k files |

Why it is faster: every `adb` process costs ~30–40 ms of startup/handshake
before moving a single byte. Upstream spawns **one process per file**; this
branch copies a whole tree with **one recursive `adb push`/`pull`** (fast
path) and groups incremental changes into **batched multi-source pushes**,
while `ls`/`mkdir`/`touch` ride a single persistent shell. File count, not
total size, decides the win: thousands of small files (caches, thumbnails,
chat media) improve the most; big media files are USB-bandwidth-bound, so
the gain there is the removed per-file overhead (~25% on 5 GB of photos).

Also fixed along the way (correctness, not speed): transfer exit codes are
now checked (a failed copy is no longer reported as successful), the diff
compares size plus mtime (same-minute changes and partial files are no
longer invisible), and device mtimes are resolved with per-epoch DST via
the probed IANA zone (files dated in the other DST season are neither
re-copied forever nor silently skipped).

## Shell completion

Tab completion for **Bash, Zsh and Fish** covers the subcommands (`push`,
`pull`, `completion`), all long/short options and flags, local path arguments,
and remote (Android) path arguments.

Completion is strictly opt-in: **adbsync never modifies your shell startup
files automatically**. You choose whether and how to enable it — the scripts
are only printed by `adbsync completion <shell>`.

### Bash

Add this to your `~/.bashrc`:

```bash
source <(adbsync completion bash)
```

Alternatively, save the script where your bash-completion setup finds it:

```bash
adbsync completion bash > ~/.local/share/bash-completion/completions/adbsync
```

### Zsh

Save the script as `_adbsync` in a directory on your `fpath` (this must happen
before `compinit` runs, e.g. from `~/.zshrc`):

```zsh
adbsync completion zsh > "${fpath[1]}/_adbsync"
```

Or source it directly from `~/.zshrc`:

```zsh
source <(adbsync completion zsh)
```

### Fish

```fish
adbsync completion fish > ~/.config/fish/completions/adbsync.fish
```

Fish loads completions from that directory automatically.

### What gets completed

- Local path arguments use your shell's native file completion.
- Remote (Android) path arguments list the **immediate children** of the
  remote parent directory only (never a recursive scan). Directories are
  suggested with a trailing `/`. The query honors `--adb-bin`, `--adb-flag`
  and `--adb-option` if you have typed them already, enforces an internal
  timeout of about one second, and suggests nothing when no device is
  connected — completion can never hang your shell.

The scripts in [`completions/`](completions/) are checked-in snapshots of
`adbsync completion <shell>`; the test suite verifies they are up to date.
After changing the CLI, regenerate them with:

```sh
adbsync completion bash > completions/adbsync.bash
adbsync completion zsh  > completions/_adbsync
adbsync completion fish > completions/adbsync.fish
```

## Intro

This is a (pretty much from scratch) rewrite of Google's [adbsync](https://github.com/google/adb-sync) repo.

The reason for the rewrite is to

1. Update the repo to Python 3 codestyle (strings are by default UTF-8, no more `b""` and `u""`, classes don't need to inherit from object, 4 space indentation etc)
2. Add in support for `--exclude`, `--exclude-from`, `--del`, `--delete-excluded` like `rsync` has (this required a complete rewrite of the diffing algorithm)

## Additions

- `--del` will delete files and folders on the destination end that are not present on the source end. This does not include excluded files.
- `--delete-excluded` will delete excluded files and folders on the destination end.
- `--exclude` can be used many times. Each should be a `fnmatch` pattern relative to the source. These patterns will be ignored unless `--delete-excluded` is specified.
- `--exclude-from` can be used many times. Each should be a filename of a file containing `fnmatch` patterns relative to the source.

## Possible future TODOs

I am satisfied with my code so far, however a few things could be added if they are ever needed

- `--backup` and `--backup-dir-local` or `--backup-dir-android` to move outdated / to-delete files to another folder instead of deleting

---

## Related Projects

Before getting used to this, please review this list of projects that are
somehow related to adb-sync and may fulfill your needs better:

* [rsync](https://rsync.samba.org/) is a file synchronization tool for local
  (including FUSE) file systems or SSH connections. 
* [adbfs-rootless](https://github.com/spion/adbfs-rootless) is a fork of adbfs
  that requires no root on the device. Does not play very well with rsync.
* [go-mtpfs](https://github.com/hanwen/go-mtpfs) is a FUSE file system to
  connect to Android devices via MTP. Due to MTP's restrictions, only a certain
  set of file extensions is supported. To store unsupported files, just add
  .txt! Requires no USB debugging mode.

# Setup

## Android Side

First you need to enable USB debugging mode. This allows authorized computers
(on Android before 4.4.3 all computers) to perform possibly dangerous
operations on your device. If you do not accept this risk, do not proceed and
try using [go-mtpfs](https://github.com/hanwen/go-mtpfs) instead!

On your Android device:

* Go to the Settings app.
* If there is no "Developer Options" menu:
  * Select "About".
  * Tap "Build Number" seven times.
  * Go back.
* Go to "Developer Options".
* Enable "USB Debugging".

## PC Side

* Install the [Android SDK](https://developer.android.com/studio) which is included in Android Studio. Android SDK will
  be installed upon first start of Android Studio.    
  If you don't want to install a Android Studio you can try to download 
  [command-line-tools](https://developer.android.com/studio/index.html#command-line-tools-only).
  Some Linux distributions come with a package named like "android-tools-adb"
  that contains the required tool.
* Make sure `adb` is in your PATH. If you use a package from your Linux
  distribution, this should already be the case; if you used the SDK, you
  probably will have to add an entry to PATH in your ~/.profile file, log out
  and log back in.
* `git clone https://github.com/jpstotz/better-adb-sync.git
* `pip install better-adb-sync/`

On modern Android device the device need to be unlocked (display on and lock screen is 
not active) when connecting with an USB cable to the PC.   

## Usage

To get a full help, type:

```
adbsync --help
```

### Usage examples

To synchronize your music files from ~/Music to your device, type one of:

```
adbsync push ~/Music /sdcard
adbsync push ~/Music/ /sdcard/Music
```

---
To synchronize your music files from ~/Music to your device, deleting files you
removed from your PC, type one of:

```
adbsync push --del ~/Music /sdcard
adbsync push --del ~/Music/ /sdcard/Music
```

---
To copy all downloads from your device to your PC, type:

```
adbsync pull /sdcard/Download/ ~/Downloads
```

---
Backup all data from external storage to your PC:
```
adbsync pull -L --show-progress pull /sdcard/ ./BackupAndroid
```
* `-L` is required to follow the symlink `/sdcard` 
* `--show-progress` is optional but recommended as depending on the data on the phone the operation can take quite a while