"""Shared fixtures for the Better ADB Sync no-device test harness.

Every test gets:

* a private fake-device root and host trees under ``tmp_path``;
* environment wiring for ``tests/fake_adb.py`` (see the module docstring of
  that file for the full contract);
* ``harness.run_adbsync(...)`` which invokes the installed ``adbsync``
  console script as a subprocess with ``--adb-bin`` pointing at the fake;
* ``harness.run_fake(...)`` which invokes the fake adb directly;
* ``harness.log`` -- a JSONL analyzer over the fake's invocation log.

No test ever touches a real adb binary or a real device, and all local paths
are confined to the per-test ``tmp_path`` via ``BETTER_ADBSYNC_FAKE_LOCAL_ROOT``.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
FAKE_ADB = TESTS_DIR / "fake_adb.py"


class FakeAdbLog:
    """Analyzer for the JSONL invocation log written by tests/fake_adb.py."""

    def __init__(self, path):
        self.path = Path(path)

    def entries(self):
        """All log entries (re-read from disk on every call)."""
        if not self.path.exists():
            return []
        entries = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:  # pragma: no cover - defensive
                    continue
        return entries

    def processes(self, command=None):
        return [
            e
            for e in self.entries()
            if e.get("kind") == "process" and (command is None or e.get("command") == command)
        ]

    def count_processes(self, command=None):
        return len(self.processes(command))

    def transfer_processes(self):
        """Every push/pull process invocation (the batching budget unit)."""
        return [e for e in self.processes() if e.get("command") in ("push", "pull")]

    def count_transfers(self, command=None):
        if command is None:
            return len(self.transfer_processes())
        return self.count_processes(command)

    def shell_commands(self, command=None):
        return [
            e
            for e in self.entries()
            if e.get("kind") == "shell_cmd" and (command is None or e.get("command") == command)
        ]

    def count_shell(self, command=None):
        return len(self.shell_commands(command))

    def transferred_files(self):
        return sum(e.get("files", 0) for e in self.transfer_processes())

    def transferred_bytes(self):
        return sum(e.get("bytes", 0) for e in self.transfer_processes())

    def settle(self, timeout=5.0):
        """Wait until the log stops growing (covers late shell-session entries)."""
        deadline = time.monotonic() + timeout
        previous = -1
        while time.monotonic() < deadline:
            current = len(self.entries())
            if current == previous:
                return current
            previous = current
            time.sleep(0.05)
        return previous


class Harness:
    def __init__(self, tmp_path, root, log_path, monkeypatch):
        self.tmp_path = Path(tmp_path)
        self.root = Path(root)
        self.log_path = Path(log_path)
        self.monkeypatch = monkeypatch
        self.log = FakeAdbLog(log_path)

    @property
    def sdcard(self):
        """The fake /sdcard directory (created on demand)."""
        path = self.root / "sdcard"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def device_path(self, *parts):
        """Host-side path of an Android path like /sdcard/foo (for assertions)."""
        return self.root.joinpath(*[p.strip("/") for p in parts if p.strip("/")])

    def set_delay_ms(self, ms):
        self.monkeypatch.setenv("BETTER_ADBSYNC_FAKE_DELAY_MS", str(ms))

    def set_fail(self, rule):
        self.monkeypatch.setenv("BETTER_ADBSYNC_FAKE_FAIL", rule)

    def clear_fail(self):
        self.monkeypatch.delenv("BETTER_ADBSYNC_FAKE_FAIL", raising=False)

    def reset_log(self):
        """Start a fresh invocation log (for per-run counts)."""
        if self.log_path.exists():
            self.log_path.unlink()

    def set_no_device(self, value="1"):
        self.monkeypatch.setenv("BETTER_ADBSYNC_FAKE_NO_DEVICE", value)

    def set_tz_offset(self, value):
        """Model a device whose timezone is not UTC (date +%z format)."""
        self.monkeypatch.setenv("BETTER_ADBSYNC_FAKE_TZ_OFFSET", value)

    def set_tz_zone(self, name):
        """Model a device in IANA zone `name` (per-epoch DST, std-time touch).

        Takes precedence over set_tz_offset in the fake. Requires host
        tzdata for `name`; use require_zone() to skip cleanly without it.
        """
        self.monkeypatch.setenv("BETTER_ADBSYNC_FAKE_TZ", name)

    def run_fake(self, *args, input=None, timeout=60):
        """Invoke the fake adb directly (as a fresh process)."""
        return subprocess.run(
            [str(FAKE_ADB), *args],
            input=input,
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=timeout,
        )

    def run_adbsync(self, *args, expect=None, timeout=180):
        """Invoke the installed adbsync CLI against the fake adb.

        Global adbsync options (including --adb-bin) are inserted before any
        subcommand, as argparse requires.
        """
        cmd = [find_adbsync(), "--adb-bin", str(FAKE_ADB), *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=dict(os.environ),
            timeout=timeout,
        )
        if expect is not None and result.returncode != expect:
            raise AssertionError(
                f"adbsync {' '.join(args)!r} exited {result.returncode}, expected {expect}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        return result


def find_adbsync():
    """Locate the adbsync console script belonging to the running interpreter."""
    sibling = Path(sys.executable).with_name("adbsync")
    if sibling.exists() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("adbsync")
    if found:
        return found
    raise RuntimeError(
        "adbsync executable not found next to the current Python or on PATH; "
        "run `uv sync` (or pip install -e .) in the repository first"
    )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "device"
    root.mkdir()
    log_path = tmp_path / "adb.jsonl"
    monkeypatch.setenv("BETTER_ADBSYNC_FAKE_ROOT", str(root))
    monkeypatch.setenv("BETTER_ADBSYNC_FAKE_LOG", str(log_path))
    monkeypatch.setenv("BETTER_ADBSYNC_FAKE_DELAY_MS", "0")
    # Safety net: no local path outside this test's tmp_path may be touched.
    monkeypatch.setenv("BETTER_ADBSYNC_FAKE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.delenv("BETTER_ADBSYNC_FAKE_FAIL", raising=False)
    monkeypatch.delenv("BETTER_ADBSYNC_FAKE_NO_DEVICE", raising=False)
    monkeypatch.delenv("BETTER_ADBSYNC_FAKE_TZ_OFFSET", raising=False)
    monkeypatch.delenv("BETTER_ADBSYNC_FAKE_TZ", raising=False)
    return Harness(tmp_path, root, log_path, monkeypatch)


def require_zone(name="Europe/Rome"):
    """Return ZoneInfo(name), or skip the test if host tzdata lacks it."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        pytest.skip(f"host tzdata does not provide {name}")


@pytest.fixture
def fake_adb_path():
    return FAKE_ADB


def write_tree(base, spec):
    """Create files from {relative_path: content}; returns `base`."""
    base = Path(base)
    for rel, content in spec.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        path.write_bytes(content)
    return base


def device_write(harness, apath, content, mtime=None):
    """Create a file directly inside the fake device filesystem.

    Used to build pull fixtures without involving `adb push`.
    """
    parts = [p for p in apath.split("/") if p]
    path = harness.root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def tree_digest(base):
    """{relative_path: (size, sha256)} for content-parity assertions."""
    import hashlib

    base = Path(base)
    digest = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            digest[str(path.relative_to(base))] = ("symlink", os.readlink(path))
        elif path.is_dir():
            digest[str(path.relative_to(base))] = ("dir", None)
        else:
            data = path.read_bytes()
            digest[str(path.relative_to(base))] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    return digest
