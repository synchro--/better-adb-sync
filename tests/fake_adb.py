#!/usr/bin/env python3
"""Fake `adb` executable for the Better ADB Sync no-device test harness.

This program NEVER invokes a real adb binary and NEVER talks to a device.
It is passed to the product under test via ``adbsync --adb-bin <this file>``
and emulates exactly the adb surface that ``src/BetterADBSync`` uses:

* ``adb shell`` (no extra args) -- a persistent stdin/stdout process speaking
  the sentinel protocol of ``FileSystems/Android.py``: every command line is
  suffixed with `` </dev/null`` and followed by a line
  ``echo 'ADBSYNC END OF COMMAND'``; the fake prints command output lines and
  then exactly one sentinel line for the echo line.
* One-shot ``adb shell CMD...`` (useful for completion / diagnostics).
* ``adb push SRC... DST`` and ``adb pull SRC... DST`` including multiple
  sources, recursive directory copies, cp-like target resolution and the
  ``-a``/``-n`` sync flags. Android absolute paths are mapped beneath
  ``BETTER_ADBSYNC_FAKE_ROOT``; local paths are used as given (and are
  restricted to ``BETTER_ADBSYNC_FAKE_LOCAL_ROOT`` when that is set).
* ``adb devices``, ``adb get-state``, ``adb version``, ``adb start-server``,
  ``adb kill-server``, ``adb wait-for-device`` -- harmless stubs.

Environment variables (the fake-device contract):

``BETTER_ADBSYNC_FAKE_ROOT`` (required)
    Temporary directory standing in for the device filesystem. Every Android
    absolute path is mapped beneath it. Any path that resolves outside this
    root (e.g. via a symlink) is rejected with exit code 1 and nothing is
    read or written outside of it.
``BETTER_ADBSYNC_FAKE_LOG`` (optional)
    JSONL invocation log. One ``{"kind": "process" ...}`` entry per fake
    process (argv, command, exit_code, files, bytes, duration_ms, pid, ts)
    plus one ``{"kind": "shell_cmd" ...}`` entry per command executed inside
    a persistent ``adb shell`` session.
``BETTER_ADBSYNC_FAKE_DELAY_MS`` (optional, default 0)
    Simulated per-process latency: the fake sleeps this long at process
    start, modelling adb client startup and server connection setup.
``BETTER_ADBSYNC_FAKE_FAIL`` (optional)
    Comma-separated ``op:path`` rules for deterministic failure injection.
    ``op`` is one of ``push pull ls realpath mkdir rm touch *``; ``path`` is
    an fnmatch pattern that also prefix-matches directories (``/sdcard/x``
    matches ``/sdcard/x/f.txt``). Injected push/pull failures copy only half
    the bytes and exit non-zero; injected shell commands print their standard
    error line (which the product treats as a fatal protocol line).
``BETTER_ADBSYNC_FAKE_LOCAL_ROOT`` (optional)
    When set, local host paths used as push sources or pull destinations
    must resolve beneath it, otherwise the command fails with exit code 1.
``BETTER_ADBSYNC_FAKE_TZ_OFFSET`` (optional, default ``+0000``)
    Device UTC offset in ``date +%z`` format (``+HHMM``/``-HHMM``). Models a
    device whose timezone is not UTC: ``ls`` renders mtimes as device-local
    wall clock (epoch + offset, printed as-is), ``date +%z`` reports this
    value, and ``touch`` keeps parsing its time string with
    ``calendar.timegm`` (matching real toybox, which stores the UTC epoch the
    string denotes). The product must subtract the offset when parsing ls
    output to recover true epochs.
``BETTER_ADBSYNC_FAKE_TZ`` (optional)
    IANA zone name (e.g. ``Europe/Rome``). Takes precedence over
    ``BETTER_ADBSYNC_FAKE_TZ_OFFSET`` and models a real DST device
    faithfully: ``ls`` renders each mtime with the DST offset in effect at
    that file's own epoch, ``touch`` parses its time string as local
    *standard* time (stored = timegm(string) - standard_offset, measured on
    the reference device for winter and summer strings alike),
    ``getprop persist.sys.timezone`` reports the zone name and ``date +%z``
    the offset in effect right now. Requires host tzdata for the zone.
``BETTER_ADBSYNC_FAKE_NO_DEVICE`` (optional)
    When set to a non-empty value the fake pretends no device is attached:
    ``devices`` lists nothing, ``get-state`` fails and the ``:`` shell test
    prints ``adb: no devices/emulators found``.

Exit codes: 0 success, 1 command failure (including injected failures and
path-escape rejection), 2 configuration/usage error.

Deliberate deviations from real adb (documented in tests/README.md):
* ``touch`` never creates a missing file; it reports
  ``touch: ...: No such file or directory`` so that timestamp updates cannot
  silently paper over a missing/partial transfer.
* Mode bits are not preserved; push/pull preserve file mtimes (push always,
  pull only with ``-a``; adbsync re-applies mtimes itself afterwards).
* ls timestamps are rendered in the fake device's timezone: UTC by default,
  shifted by ``BETTER_ADBSYNC_FAKE_TZ_OFFSET`` to model a non-UTC device.
  The product parses them with ``calendar.timegm``
  (date_utils.datetime_string_to_posix_timestamp) and must subtract the
  device offset to recover the true epoch; with the default offset nothing
  changes and minute-truncated round trips stay exact.
* ``touch`` interprets its ``[[CC]YY]MMDDhhmm`` string with
  ``calendar.timegm`` (UTC wall clock) regardless of the fake timezone --
  matching real toybox on the reference device, where the stored epoch is
  the UTC interpretation of the string. (In ``BETTER_ADBSYNC_FAKE_TZ`` zone
  mode ``touch`` instead parses as local *standard* time, which is what the
  reference device was measured to do.)
"""

import calendar
import datetime
import fnmatch
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import sys
import time

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None

SENTINEL = "ADBSYNC END OF COMMAND"
STDIN_REDIRECT_SUFFIX = " </dev/null"

ENV_ROOT = "BETTER_ADBSYNC_FAKE_ROOT"
ENV_LOG = "BETTER_ADBSYNC_FAKE_LOG"
ENV_DELAY_MS = "BETTER_ADBSYNC_FAKE_DELAY_MS"
ENV_FAIL = "BETTER_ADBSYNC_FAKE_FAIL"
ENV_LOCAL_ROOT = "BETTER_ADBSYNC_FAKE_LOCAL_ROOT"
ENV_NO_DEVICE = "BETTER_ADBSYNC_FAKE_NO_DEVICE"
ENV_TZ_OFFSET = "BETTER_ADBSYNC_FAKE_TZ_OFFSET"
ENV_TZ = "BETTER_ADBSYNC_FAKE_TZ"

# adb global options that consume a separate value.
GLOBAL_VALUE_OPTS = ("-s", "-t", "-H", "-P", "-L")
# adb sync options accepted for push/pull; only -a/-n change behaviour.
SYNC_OPTS = "anpqrz"

ALLOWED_FAIL_OPS = {"push", "pull", "ls", "realpath", "mkdir", "rm", "touch", "*"}


class FakeError(Exception):
    """Command failure -> exit code 1."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class ConfigError(FakeError):
    """Configuration/usage error -> exit code 2."""

    def __init__(self, message):
        super().__init__(message, code=2)


class Stats:
    def __init__(self):
        self.files = 0
        self.bytes = 0
        self.failed = False
        self.skipped = 0


def parse_tz_offset(value):
    """Parse a `date +%z` style offset (+HHMM/-HHMM) to (string, seconds)."""
    match = re.fullmatch(r"([+-])(\d{2})(\d{2})", value.strip())
    if not match:
        raise ConfigError(f"{ENV_TZ_OFFSET}: expected +HHMM or -HHMM, got {value!r}")
    sign = 1 if match.group(1) == "+" else -1
    seconds = sign * (int(match.group(2)) * 3600 + int(match.group(3)) * 60)
    return f"{match.group(1)}{match.group(2)}{match.group(3)}", seconds


class Context:
    def __init__(self, environ):
        self.environ = environ
        root = environ.get(ENV_ROOT)
        if not root:
            raise ConfigError(f"{ENV_ROOT} is not set; refusing to run without a fake device root")
        if not os.path.isdir(root):
            raise ConfigError(f"{ENV_ROOT} does not exist: {root}")
        self.root = os.path.realpath(root)
        self.local_root = environ.get(ENV_LOCAL_ROOT)
        if self.local_root:
            self.local_root = os.path.realpath(self.local_root)
        self.log_path = environ.get(ENV_LOG)
        self.no_device = bool(environ.get(ENV_NO_DEVICE))
        self.rules = self._parse_fail_spec(environ.get(ENV_FAIL, ""))
        self.tz_offset_str, self.tz_offset = parse_tz_offset(
            environ.get(ENV_TZ_OFFSET, "+0000") or "+0000")
        self.tz_zone_name = (environ.get(ENV_TZ) or "").strip()
        self.tzinfo = None
        self.tz_std_offset = 0
        if self.tz_zone_name:
            if ZoneInfo is None:
                raise ConfigError(f"{ENV_TZ}: zoneinfo is unavailable on this Python")
            try:
                self.tzinfo = ZoneInfo(self.tz_zone_name)
            except (KeyError, ValueError):
                raise ConfigError(f"{ENV_TZ}: unknown zone {self.tz_zone_name!r}")
            # Standard offset = utcoffset - dst (constant across the year);
            # this is what the reference device's `touch` parses with.
            # (Convert via astimezone: zone.utcoffset() on a foreign-tz
            # datetime is not portable near DST transitions.)
            probe = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
            probe = probe.astimezone(self.tzinfo)
            off = probe.utcoffset()
            dst = probe.dst() or datetime.timedelta(0)
            self.tz_std_offset = int((off - dst).total_seconds())

    @staticmethod
    def _parse_fail_spec(spec):
        rules = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            op, sep, pattern = part.partition(":")
            if not sep or not pattern:
                raise ConfigError(f"{ENV_FAIL}: rule must be op:path, got {part!r}")
            if op not in ALLOWED_FAIL_OPS:
                raise ConfigError(f"{ENV_FAIL}: unknown operation {op!r} (allowed: {sorted(ALLOWED_FAIL_OPS)})")
            rules.append((op, pattern))
        return rules

    # --- path mapping -----------------------------------------------------

    def map_remote(self, raw):
        """Map an Android path to a host path beneath the fake device root."""
        normalized = posixpath.normpath(raw if raw.startswith("/") else "/" + raw)
        joined = os.path.join(self.root, normalized.lstrip("/"))
        self._require_within(joined, self.root, f"remote path escapes fake device root: {raw!r}")
        return joined

    def map_local(self, raw):
        """Validate a local host path (push source / pull destination)."""
        if not self.local_root:
            return raw
        self._require_within(raw, self.local_root, f"local path escapes allowed local root: {raw!r}")
        return raw

    @staticmethod
    def _require_within(path, root, message):
        real = os.path.realpath(path)
        if real != root and not real.startswith(root + os.sep):
            raise FakeError(message)

    def host_to_android(self, host_path):
        real = os.path.realpath(host_path)
        self._require_within(real, self.root, f"remote path escapes fake device root: {host_path!r}")
        rel = os.path.relpath(real, self.root)
        if rel == ".":
            return "/"
        return "/" + rel.replace(os.sep, "/")

    # --- failure injection ------------------------------------------------

    def injected(self, op, candidates):
        """True if any FAIL rule with operation `op` matches any candidate path."""
        if not self.rules:
            return False
        for rule_op, pattern in self.rules:
            if rule_op not in (op, "*"):
                continue
            for path in candidates:
                if path is None:
                    continue
                if fnmatch.fnmatchcase(path, pattern):
                    return True
                if path.startswith(pattern.rstrip("/") + "/"):
                    return True
        return False


# --- logging ---------------------------------------------------------------


def offset_for_mtime(ctx, mtime):
    """Device UTC offset (seconds east of UTC) in effect at epoch `mtime`.

    Zone mode resolves the offset per epoch via tzdata (DST-aware);
    otherwise the configured constant offset applies. NOTE: the conversion
    must go through astimezone -- calling zone.utcoffset() directly on a
    datetime whose tzinfo is datetime.timezone.utc reinterprets the UTC
    wall fields as zone-local fields on some interpreters (wrong answers
    near DST transitions).
    """
    if ctx.tzinfo is not None:
        moment = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc)
        return int(moment.astimezone(ctx.tzinfo).utcoffset().total_seconds())
    return ctx.tz_offset


def append_log(entry):
    path = os.environ.get(ENV_LOG)
    if not path:
        return
    data = (json.dumps(entry, ensure_ascii=True) + "\n").encode("utf-8")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - non-POSIX platforms
            pass
        os.write(fd, data)
    finally:
        os.close(fd)


def log_process(argv, command, exit_code, stats, started_ts, duration_ms):
    append_log({
        "kind": "process",
        "ts": started_ts,
        "pid": os.getpid(),
        "argv": list(argv),
        "command": command,
        "exit_code": exit_code,
        "files": stats.files,
        "bytes": stats.bytes,
        "failed": stats.failed,
        "duration_ms": round(duration_ms, 3),
    })


def log_shell_cmd(tokens, exit_code, output_lines, duration_ms):    append_log({
        "kind": "shell_cmd",
        "ts": time.time(),
        "pid": os.getpid(),
        "argv": list(tokens),
        "command": tokens[0] if tokens else "",
        "exit_code": exit_code,
        "files": 0,
        "bytes": 0,
        "output_lines": output_lines,
        "duration_ms": round(duration_ms, 3),
    })


# --- ls output formatting --------------------------------------------------


def _perm_string(mode):
    """Render st_mode as the ls permission string accepted by Android.RE_LS_TO_STAT."""
    out = []
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 0o7
        out.append("r" if bits & 0o4 else "-")
        out.append("w" if bits & 0o2 else "-")
        out.append("x" if bits & 0o1 else "-")
    return "".join(out)


def _escape_b(name):
    """ls -b style escaping (the product only reverts '\\ ' back to space)."""
    out = []
    for ch in name:
        if ch == "\\":
            out.append("\\\\")
        elif ch == " ":
            out.append("\\ ")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append("\\%03o" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def ls_line(display_name, st, escape, tz_offset=0):
    """One `ls -l` style line that matches Android.RE_LS_TO_STAT exactly.

    ``tz_offset`` shifts the rendered mtime into device-local wall clock
    (seconds east of UTC), modelling a device whose timezone is not UTC.
    """
    if stat.S_ISDIR(st.st_mode):
        typechar, size_field = "d", str(st.st_size)
    elif stat.S_ISREG(st.st_mode):
        typechar, size_field = "-", str(st.st_size)
    elif stat.S_ISLNK(st.st_mode):
        typechar, size_field = "l", str(st.st_size)
    elif stat.S_ISCHR(st.st_mode):
        typechar, size_field = "c", f"{st.st_rdev} 0"
    elif stat.S_ISBLK(st.st_mode):
        typechar, size_field = "b", f"{st.st_rdev} 0"
    elif stat.S_ISFIFO(st.st_mode):
        typechar, size_field = "p", None
    elif stat.S_ISSOCK(st.st_mode):
        typechar, size_field = "s", None
    else:  # pragma: no cover - not reachable with generated fixtures
        typechar, size_field = "-", str(st.st_size)

    tm = time.gmtime(st.st_mtime + tz_offset)
    stamp = "%04d-%02d-%02d %02d:%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min)
    nlink = st.st_nlink if st.st_nlink > 0 else 1

    parts = [typechar + _perm_string(st.st_mode), str(nlink), "root", "root"]
    if size_field is not None:
        parts.append(size_field)
    parts.append(stamp)
    name = _escape_b(display_name) if escape else display_name
    parts.append(name)
    return " ".join(parts)


# --- shell command implementations ----------------------------------------


def parse_time_string(value):
    """Parse touch's [[CC]YY]MMDDhhmm[.ss] format to a POSIX timestamp (UTC).

    The format is selected by string length: strptime's per-field regexes
    accept single-digit values, so trying ``%Y%m%d%H%M%S`` first would
    happily mis-parse a 12-digit ``%Y%m%d%H%M`` value as 22:01:5 for 22:15.
    """
    raw = value.split(".")[0]
    fmts = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M", 10: "%Y%m%d%H", 8: "%Y%m%d"}
    fmt = fmts.get(len(raw))
    if fmt is None:
        raise FakeError(f"touch: invalid date format {value!r}")
    try:
        return calendar.timegm(time.strptime(raw, fmt))
    except ValueError:
        raise FakeError(f"touch: invalid date format {value!r}")


def missing_path_error(prefix, raw, ctx):
    """Classify a missing path the way ls/realpath do (Not a directory vs No such)."""
    apath = posixpath.normpath(raw if raw.startswith("/") else "/" + raw)
    current = ctx.root
    for part in [p for p in apath.split("/") if p]:
        current = os.path.join(current, part)
        if os.path.lexists(current):
            if not os.path.isdir(current):
                return f"{prefix}: {raw}: Not a directory"
        else:
            break
    return f"{prefix}: {raw}: No such file or directory"


def cmd_ls(tokens, ctx):
    flags = set()
    paths = []
    for arg in tokens[1:]:
        if arg.startswith("-") and len(arg) > 1 and not paths:
            flags.update(arg[1:])
        else:
            paths.append(arg)
    if not paths:
        return ["ls: missing operand"], 1

    escape = "b" in flags
    lines = []
    status = 0
    for raw in paths:
        if ctx.injected("ls", [raw]):
            lines.append(f"ls: {raw}: Permission denied")
            status = 1
            continue
        host = ctx.map_remote(raw)
        if not os.path.lexists(host):
            lines.append(missing_path_error("ls", raw, ctx))
            status = 1
            continue
        st = os.lstat(host)
        if (not stat.S_ISDIR(st.st_mode)) or ("d" in flags):
            lines.append(ls_line(raw, st, escape, offset_for_mtime(ctx, st.st_mtime)))
            continue
        # Directory listing without -d: 'total N' header plus entries.
        try:
            names = sorted(os.listdir(host))
        except OSError as e:
            lines.append(f"ls: {raw}: {e.strerror}")
            status = 1
            continue
        entries = ([".", ".."] + names) if "a" in flags else names
        lines.append("total %d" % max(1, len(entries)))
        for name in entries:
            child = host if name == "." else os.path.join(host, ".." if name == ".." else name)
            try:
                child_st = os.lstat(child)
            except OSError as e:
                lines.append(f"ls: {raw}: {e.strerror}")
                status = 1
                continue
            lines.append(ls_line(name, child_st, escape, offset_for_mtime(ctx, child_st.st_mtime)))
    return lines, status


def cmd_realpath(tokens, ctx):
    paths = [a for a in tokens[1:] if not a.startswith("-")]
    if not paths:
        return ["realpath: missing operand"], 1
    raw = paths[0]
    if ctx.injected("realpath", [raw]):
        return [f"realpath: {raw}: No such file or directory"], 1
    host = ctx.map_remote(raw)
    if not os.path.lexists(host):
        return [missing_path_error("realpath", raw, ctx)], 1
    return [ctx.host_to_android(host)], 0


def cmd_mkdir(tokens, ctx):
    flags = set()
    paths = []
    for arg in tokens[1:]:
        if arg.startswith("-") and len(arg) > 1 and not paths:
            flags.update(arg[1:])
        else:
            paths.append(arg)
    if not paths:
        return ["mkdir: missing operand"], 1
    status = 0
    lines = []
    for raw in paths:
        if ctx.injected("mkdir", [raw]):
            lines.append(f"mkdir: cannot create directory '{raw}': Permission denied")
            status = 1
            continue
        host = ctx.map_remote(raw)
        try:
            if "p" in flags:
                os.makedirs(host, exist_ok=True)
            else:
                os.mkdir(host)
        except OSError as e:
            lines.append(f"mkdir: cannot create directory '{raw}': {e.strerror}")
            status = 1
    return lines, status


def cmd_rm(tokens, ctx):
    flags = set()
    paths = []
    for arg in tokens[1:]:
        if arg.startswith("-") and len(arg) > 1 and not paths:
            flags.update(arg[1:])
        else:
            paths.append(arg)
    if not paths:
        return ["rm: missing operand"], 1
    status = 0
    lines = []
    for raw in paths:
        host = ctx.map_remote(raw)
        if not os.path.lexists(host):
            if "f" in flags:
                continue
            lines.append(f"rm: cannot remove '{raw}': No such file or directory")
            status = 1
            continue
        if ctx.injected("rm", [raw]):
            lines.append(f"rm: cannot remove '{raw}': Permission denied")
            status = 1
            continue
        try:
            if os.path.isdir(host) and not os.path.islink(host):
                if "r" in flags:
                    shutil.rmtree(host)
                else:
                    lines.append(f"rm: cannot remove '{raw}': Is a directory")
                    status = 1
            else:
                os.remove(host)
        except OSError as e:
            lines.append(f"rm: cannot remove '{raw}': {e.strerror}")
            status = 1
    return lines, status


def cmd_touch(tokens, ctx):
    # Android.py sends: touch -at YYYYmmddHHMM -mt YYYYmmddHHMM PATH
    atime = mtime = None
    want_atime = want_mtime = False
    paths = []
    args = tokens[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-at", "-mt", "-t") and i + 1 < len(args):
            value = parse_time_string(args[i + 1])
            if ctx.tzinfo is not None:
                # Zone mode: the reference device parses the string as
                # local *standard* time (measured, both DST seasons).
                value -= ctx.tz_std_offset
            if arg in ("-at", "-t"):
                atime, want_atime = value, True
            if arg in ("-mt", "-t"):
                mtime, want_mtime = value, True
            i += 2
            continue
        if arg == "-a":
            want_atime = True
        elif arg == "-m":
            want_mtime = True
        elif arg.startswith("-") and len(arg) > 1 and not paths:
            pass  # tolerate other single-letter touch flags
        else:
            paths.append(arg)
        i += 1

    if not paths:
        return ["touch: missing file operand"], 1
    if not want_atime and not want_mtime:
        want_atime = want_mtime = True
    now = time.time()
    if want_atime and atime is None:
        atime = now
    if want_mtime and mtime is None:
        mtime = now

    status = 0
    lines = []
    for raw in paths:
        host = ctx.map_remote(raw)
        # Deliberate deviation: never create missing files (see module docstring).
        if not os.path.exists(host):
            lines.append(f"touch: cannot touch '{raw}': No such file or directory")
            status = 1
            continue
        if ctx.injected("touch", [raw]):
            lines.append(f"touch: cannot touch '{raw}': Permission denied")
            status = 1
            continue
        try:
            st = os.stat(host)
            os.utime(
                host,
                (atime if want_atime else st.st_atime, mtime if want_mtime else st.st_mtime),
            )
        except OSError as e:
            lines.append(f"touch: cannot touch '{raw}': {e.strerror}")
            status = 1
    return lines, status


def cmd_date(tokens, ctx):
    # The product probes the device UTC offset with `date +%z` once per run
    # (fallback when no usable IANA zone was reported by `getprop`).
    if tokens[1:] == ["+%z"]:
        if ctx.tzinfo is not None:
            seconds = offset_for_mtime(ctx, time.time())
            sign = "+" if seconds >= 0 else "-"
            return ["%s%02d%02d" % (sign, abs(seconds) // 3600, (abs(seconds) % 3600) // 60)], 0
        return [ctx.tz_offset_str], 0
    return ["date: only the +%z format is supported by fake adb"], 1


def cmd_getprop(tokens, ctx):
    # The product probes the device IANA zone for per-epoch DST handling.
    if tokens[1:] == ["persist.sys.timezone"]:
        return [ctx.tz_zone_name], 0
    return [""], 0


def execute_shell_command(tokens, ctx):
    """Run one shell command; return (output_lines, exit_status)."""
    if not tokens:
        return [], 0
    cmd = tokens[0]
    if cmd == ":":
        if ctx.no_device:
            return ["adb: no devices/emulators found"], 1
        return [], 0
    if cmd == "echo":
        return [" ".join(tokens[1:])], 0
    if cmd == "date":
        return cmd_date(tokens, ctx)
    if cmd == "getprop":
        return cmd_getprop(tokens, ctx)
    if cmd == "ls":
        return cmd_ls(tokens, ctx)
    if cmd == "realpath":
        return cmd_realpath(tokens, ctx)
    if cmd == "mkdir":
        return cmd_mkdir(tokens, ctx)
    if cmd == "rm":
        return cmd_rm(tokens, ctx)
    if cmd == "touch":
        return cmd_touch(tokens, ctx)
    return [f"sh: {cmd}: unknown command"], 127


# --- adb shell sessions ----------------------------------------------------


def strip_redirect(line):
    if line.endswith(STDIN_REDIRECT_SUFFIX):
        return line[: -len(STDIN_REDIRECT_SUFFIX)]
    return line


def emit(text, stdout_buffer):
    stdout_buffer.write(text.encode("utf-8", "surrogateescape") + b"\n")
    stdout_buffer.flush()


def run_shell_session(ctx):
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    status = 0
    while True:
        raw = stdin.readline()
        if not raw:
            break
        line = strip_redirect(raw.decode("utf-8", "surrogateescape").rstrip("\r\n"))
        if not line.strip():
            continue
        try:
            tokens = shlex.split(line, posix=True)
        except ValueError:
            tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "echo" and len(tokens) == 2 and tokens[1] == SENTINEL:
            # Protocol framing written by AndroidFileSystem.adb_shell.
            emit(SENTINEL, stdout)
            continue
        started = time.monotonic()
        try:
            # Everything must go to stdout: Android.py merges stderr into stdout.
            output, status = execute_shell_command(tokens, ctx)
        except FakeError as e:
            output, status = [str(e)], e.code
        log_shell_cmd(tokens, status, len(output), (time.monotonic() - started) * 1000)
        for out_line in output:
            emit(out_line, stdout)
    return status


def run_shell_once(tokens, ctx):
    output, status = execute_shell_command(tokens, ctx)
    for line in output:
        print(line)
    return status


# --- push / pull -----------------------------------------------------------


def parse_sync_args(command, rest):
    opts = set()
    paths = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            paths.extend(rest[i + 1 :])
            break
        if arg.startswith("-") and len(arg) > 1 and not paths:
            for ch in arg[1:]:
                if ch in SYNC_OPTS:
                    opts.add(ch)
                else:
                    raise FakeError(f"adb {command}: unknown option -{ch}")
            i += 1
            continue
        paths.append(arg)
        i += 1
    if len(paths) < 2:
        raise FakeError(f"usage: adb {command} [<options>] <source>... <destination>")
    return opts, paths


def resolve_target(src_name, dst_host, dst_apath):
    """cp-like target resolution: an existing directory receives basename(src).

    ``dst_apath`` is the Android-namespace destination path for push (None for
    pull, where the destination is a local path with no Android mapping).
    """
    if os.path.isdir(dst_host):
        name = src_name
        host = os.path.join(dst_host, name)
        apath = posixpath.join(dst_apath, name) if dst_apath else None
        return host, apath
    return dst_host, dst_apath


def copy_file(src_host, dst_host, src_apath, dst_apath, is_push, opts, ctx, stats):
    op = "push" if is_push else "pull"
    candidates = [src_apath, dst_apath, src_host, dst_host]
    injected = ctx.injected(op, candidates)
    try:
        data = open(src_host, "rb").read()
    except OSError as e:
        raise FakeError(f"{op}: cannot read {src_host!r}: {e.strerror}")
    write_data = data
    if injected:
        write_data = data[: max(1, len(data) // 2)] if len(data) > 1 else data
        stats.failed = True
    parent = os.path.dirname(dst_host)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            raise FakeError(f"{op}: cannot create {parent!r}: {e.strerror}")
    try:
        with open(dst_host, "wb") as fh:
            fh.write(write_data)
    except OSError as e:
        raise FakeError(f"{op}: cannot write {dst_host!r}: {e.strerror}")
    try:
        src_st = os.stat(src_host)
        if injected:
            mtime = time.time()
        elif is_push or "a" in opts:
            mtime = src_st.st_mtime
        else:
            mtime = time.time()
        os.utime(dst_host, (mtime, mtime))
    except OSError:
        pass
    stats.files += 1
    stats.bytes += len(write_data)


def copy_path(src_host, dst_host, src_apath, dst_apath, is_push, opts, ctx, stats):
    if os.path.islink(src_host):
        if os.path.isdir(src_host):
            # Do not recurse into symlinked directories (adbsync passes -L separately).
            stats.skipped += 1
            return
    if os.path.isdir(src_host):
        try:
            os.makedirs(dst_host, exist_ok=True)
        except OSError as e:
            raise FakeError(f"cannot create directory {dst_host!r}: {e.strerror}")
        for name in sorted(os.listdir(src_host)):
            child_src = os.path.join(src_host, name)
            child_dst = os.path.join(dst_host, name)
            child_src_apath = posixpath.join(src_apath, name) if src_apath else None
            child_dst_apath = posixpath.join(dst_apath, name) if dst_apath else None
            copy_path(child_src, child_dst, child_src_apath, child_dst_apath, is_push, opts, ctx, stats)
        return
    copy_file(src_host, dst_host, src_apath, dst_apath, is_push, opts, ctx, stats)


def run_sync(command, opts, paths, ctx):
    """Execute push or pull. Returns (exit_code, Stats)."""
    stats = Stats()
    if "n" in opts:
        print(f"{command}: dry run, no files transferred")
        return 0, stats

    is_push = command == "push"
    source_raws, dest_raw = paths[:-1], paths[-1]

    if is_push:
        dest_host = ctx.map_remote(dest_raw)
        dest_apath = posixpath.normpath(dest_raw if dest_raw.startswith("/") else "/" + dest_raw)
        if len(source_raws) > 1 and not os.path.isdir(dest_host):
            raise FakeError(f"push: multiple sources require an existing directory destination: {dest_raw}")
        for src_raw in source_raws:
            src_host = ctx.map_local(src_raw)
            if not os.path.exists(src_host):
                raise FakeError(f"push: cannot stat {src_raw!r}: No such file or directory")
            src_name = posixpath.basename(posixpath.normpath(src_raw.replace(os.sep, "/")))
            target_host, target_apath = resolve_target(src_name, dest_host, dest_apath)
            copy_path(src_host, target_host, None, target_apath, True, opts, ctx, stats)
    else:
        dest_host = ctx.map_local(dest_raw)
        if len(source_raws) > 1 and not os.path.isdir(dest_host):
            raise FakeError(f"pull: multiple sources require an existing directory destination: {dest_raw}")
        for src_raw in source_raws:
            src_apath = posixpath.normpath(src_raw if src_raw.startswith("/") else "/" + src_raw)
            src_host = ctx.map_remote(src_raw)
            if not os.path.exists(src_host):
                raise FakeError(f"pull: cannot read {src_raw!r}: No such file or directory")
            src_name = posixpath.basename(src_apath)
            target_host, target_apath = resolve_target(src_name, dest_host, None)
            copy_path(src_host, target_host, src_apath, target_apath, False, opts, ctx, stats)

    print(
        f"{stats.files} file{'s' if stats.files != 1 else ''} {command}ed, {stats.bytes} bytes"
        + (f" ({stats.skipped} skipped)" if stats.skipped else "")
    )
    return (1 if stats.failed else 0), stats


# --- adb global command stubs ---------------------------------------------


def run_devices(_rest, _ctx, stats):
    print("List of devices attached")
    if _ctx.no_device:
        print("")
    else:
        print("FAKE01\tdevice")
    return 0, stats


def run_get_state(_rest, ctx, stats):
    if ctx.no_device:
        raise FakeError("adb: no devices/emulators found")
    print("device")
    return 0, stats


def run_version(_rest, _ctx, stats):
    print("Android Debug Bridge version 1.0.41 (Better ADB Sync fake test harness)")
    print("Version embedded in Better ADB Sync tests/fake_adb.py")
    return 0, stats


def run_noop(_rest, _ctx, stats):
    return 0, stats


# --- main ------------------------------------------------------------------


def split_global_args(argv):
    """Split `[-gflag | -g value ...] command rest...` like adb's getopt."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-"):
            break
        if any(arg.startswith(v) for v in GLOBAL_VALUE_OPTS) and len(arg) > 2:
            i += 1  # inline value form, e.g. -semulator-5554
            continue
        if arg in GLOBAL_VALUE_OPTS:
            i += 2  # separate value form, e.g. -s emulator-5554
            continue
        i += 1  # plain flag, e.g. -d
    command = argv[i] if i < len(argv) else ""
    rest = argv[i + 1 :] if i < len(argv) else []
    return command, rest


def main(argv):
    started_ts = time.time()
    t0 = time.monotonic()
    argv_full = [sys.argv[0]] + list(argv)

    delay_ms = float(os.environ.get(ENV_DELAY_MS, "0") or "0")
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)

    stats = Stats()
    command = ""
    code = 0
    try:
        ctx = Context(os.environ)
        command, rest = split_global_args(argv)
        if not command:
            raise ConfigError("no command given (fake adb; real adb is never invoked)")
        if command == "shell":
            if rest:
                code = run_shell_once(rest, ctx)
            else:
                code = run_shell_session(ctx)
        elif command in ("push", "pull"):
            opts, paths = parse_sync_args(command, rest)
            code, stats = run_sync(command, opts, paths, ctx)
        elif command == "devices":
            code, stats = run_devices(rest, ctx, stats)
        elif command == "get-state":
            code, stats = run_get_state(rest, ctx, stats)
        elif command == "version":
            code, stats = run_version(rest, ctx, stats)
        elif command in ("start-server", "kill-server", "wait-for-device", "reconnect"):
            code, stats = run_noop(rest, ctx, stats)
        else:
            raise FakeError(f"unknown command {command!r} (fake adb; real adb is never invoked)")
    except FakeError as e:
        print(str(e), file=sys.stderr)
        code = e.code

    duration_ms = (time.monotonic() - t0) * 1000
    # For the persistent shell the process entry is written on session end.
    try:
        log_process(argv_full, command or "?", code, stats, started_ts, duration_ms)
    except OSError:  # pragma: no cover - logging must never break the fake
        pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
