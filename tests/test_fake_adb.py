"""Contract tests for tests/fake_adb.py -- get these green first.

These tests exercise the fake adb directly (and through the product's real
AndroidFileSystem class) so that later transfer tests can rely on it.
"""

import calendar
import os
import shlex
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from BetterADBSync.FileSystems.Android import AndroidFileSystem

from conftest import FAKE_ADB, device_write, tree_digest, write_tree

SENTINEL = "ADBSYNC END OF COMMAND"
MINUTE = 60
# Fixed, recognizable source mtime (not minute-aligned on purpose).
FIXED_MTIME = 1_700_000_123


@pytest.fixture
def fs(harness):
    """A real AndroidFileSystem wired to the fake adb (persistent shell)."""
    filesystem = AndroidFileSystem([str(FAKE_ADB)], "UTF-8", False)
    yield filesystem
    # AndroidFileSystem.__del__ closes the shell's stdin and waits for it.
    del filesystem


# --- executable / protocol -------------------------------------------------


def test_fake_adb_is_executable_with_shebang(fake_adb_path):
    assert os.access(fake_adb_path, os.X_OK), "tests/fake_adb.py must have the executable bit"
    first_line = fake_adb_path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!")


def test_shell_sentinel_protocol_exact_bytes(harness):
    """The fake speaks exactly the stdin/stdout protocol Android.py expects."""
    proc = subprocess.Popen(
        [str(FAKE_ADB), "shell"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    def adb_shell(commands):
        # Mirrors AndroidFileSystem.adb_shell byte for byte.
        proc.stdin.write(shlex.join(commands).encode("UTF-8"))
        proc.stdin.write(" </dev/null\n".encode("UTF-8"))
        proc.stdin.write(shlex.join(["echo", SENTINEL]).encode("UTF-8"))
        proc.stdin.write(" </dev/null\n".encode("UTF-8"))
        proc.stdin.flush()
        lines = []
        while True:
            raw = proc.stdout.readline()
            assert raw, "fake shell closed before the sentinel"
            line = raw.decode("UTF-8").rstrip("\r\n")
            if line == SENTINEL:
                break
            lines.append(line)
        return lines

    # ':' must produce no output at all (test_connection relies on this).
    assert adb_shell([":"]) == []
    assert adb_shell(["echo", "hello world"]) == ["hello world"]
    assert adb_shell(["mkdir", "-p", "/sdcard/sentinel"]) == []
    listing = adb_shell(["ls", "-ladb", "/sdcard/sentinel"])
    assert len(listing) == 1
    assert listing[0].startswith("d")
    assert adb_shell(["no-such-command-xyz"])  # error surfaces as output, not a crash

    proc.stdin.close()
    # Like a shell, the session exits with the status of its last command
    # (here: 127 for the unknown command above). Android.py never checks it.
    assert proc.wait(timeout=10) == 127
    assert proc.stderr.read() == b""


def test_android_fs_connection_and_basic_commands(fs, harness):
    assert list(fs.adb_shell([":"])) == []

    fs.makedirs("/sdcard/work/sub")
    st = fs.lstat("/sdcard/work")
    assert stat.S_ISDIR(st.st_mode)

    # utime must honour Android's YYYYmmddHHMM (minute resolution) round trip.
    fs.utime("/sdcard/work/sub", (FIXED_MTIME - (FIXED_MTIME % 60),) * 2)
    st = fs.lstat("/sdcard/work/sub")
    assert st.st_mtime == FIXED_MTIME - (FIXED_MTIME % 60)

    assert fs.realpath("/sdcard/work") == "/sdcard/work"

    fs.rmdir("/sdcard/work/sub")
    with pytest.raises(FileNotFoundError):
        fs.lstat("/sdcard/work/sub")


def test_realpath_missing_raises_file_not_found(fs, harness):
    with pytest.raises(FileNotFoundError):
        fs.realpath("/sdcard/does-not-exist")


def test_lstat_and_ls_output_parse_with_android_regexes(fs, harness, tmp_path):
    """ls -ladb / ls -lab output must satisfy Android.RE_LS_TO_STAT exactly."""
    work = harness.sdcard / "work"
    write_tree(work, {
        "plain.txt": b"x" * 42,
        "with space.txt": b"y" * 7,
        "ünïcode-ファイル.txt": "雪の中で".encode("utf-8"),
        "zero.txt": b"",
        "sub/nested.txt": b"nested",
    })
    # Give one file a known non-minute-aligned mtime.
    os.utime(work / "plain.txt", (FIXED_MTIME, FIXED_MTIME))

    # Single-path lstat form: ls -ladb
    st = fs.lstat("/sdcard/work/plain.txt")
    assert stat.S_ISREG(st.st_mode)
    assert st.st_size == 42
    assert st.st_mtime == FIXED_MTIME - (FIXED_MTIME % 60)

    st_zero = fs.lstat("/sdcard/work/zero.txt")
    assert st_zero.st_size == 0

    st_dir = fs.lstat("/sdcard/work")
    assert stat.S_ISDIR(st_dir.st_mode)

    # Directory listing form: ls -lab (includes . and .. like ls -a does)
    entries = dict(fs.lstat_in_dir("/sdcard/work"))
    assert entries is not None
    assert entries["."] is not None and entries[".."] is not None
    assert entries["plain.txt"].st_size == 42
    assert entries["with space.txt"].st_size == 7  # ls -b escaping must be reverted
    assert entries["ünïcode-ファイル.txt"].st_size == 12
    assert entries["zero.txt"].st_size == 0

    with pytest.raises(FileNotFoundError):
        fs.lstat("/sdcard/work/missing.txt")


def test_get_files_tree_round_trip(fs, harness):
    work = harness.sdcard / "tree"
    write_tree(work, {
        "a.txt": b"a",
        "with space.txt": b"sp",
        "日本語 ファイル.txt": "データ".encode(),
        "sub/b.txt": b"b",
        "sub/deep/c.txt": b"c",
    })
    os.utime(work / "a.txt", (FIXED_MTIME, FIXED_MTIME))

    tree = fs.get_files_tree("/sdcard/tree")
    assert isinstance(tree, dict)
    assert set(tree) == {".", "a.txt", "with space.txt", "日本語 ファイル.txt", "sub"}
    assert set(tree["sub"]) == {".", "b.txt", "deep"}
    assert set(tree["sub"]["deep"]) == {".", "c.txt"}
    # Base.py stores minute-truncated (atime, mtime, size) tuples for files.
    expected_minute = FIXED_MTIME - (FIXED_MTIME % 60)
    assert tree["a.txt"] == (expected_minute, expected_minute, 1)


def test_missing_path_error_kinds(fs, harness):
    """Error lines must match the product's specific error regexes."""
    with pytest.raises(FileNotFoundError):
        fs.lstat("/sdcard/no/such/path")
    # A file where a directory component should be -> Not a directory line.
    write_tree(harness.sdcard, {"afile": b"iamafile"})
    with pytest.raises(NotADirectoryError):
        fs.lstat("/sdcard/afile/child")


# --- push / pull -----------------------------------------------------------


def test_push_copies_bytes_and_preserves_mtime(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"z" * 1000}) / "f.txt"
    os.utime(src, (FIXED_MTIME, FIXED_MTIME))

    result = harness.run_fake("push", str(src), "/sdcard/x/f.txt")
    assert result.returncode == 0, result.stderr

    dest = harness.device_path("sdcard/x/f.txt")
    assert dest.read_bytes() == b"z" * 1000
    assert dest.stat().st_mtime == pytest.approx(FIXED_MTIME, abs=0.001)


def test_pull_bytes_and_mtime_flags(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"q" * 500}) / "f.txt"
    os.utime(src, (FIXED_MTIME, FIXED_MTIME))
    harness.run_fake("push", str(src), "/sdcard/x/f.txt")

    plain = tmp_path / "pulled-plain.txt"
    result = harness.run_fake("pull", "/sdcard/x/f.txt", str(plain))
    assert result.returncode == 0, result.stderr
    assert plain.read_bytes() == b"q" * 500
    # Without -a real adb sets the current time (adbsync re-applies mtimes itself).
    assert abs(plain.stat().st_mtime - time.time()) < 60

    preserved = tmp_path / "pulled-a.txt"
    result = harness.run_fake("pull", "-a", "/sdcard/x/f.txt", str(preserved))
    assert result.returncode == 0, result.stderr
    assert preserved.stat().st_mtime == pytest.approx(FIXED_MTIME, abs=0.001)


def test_recursive_directory_transfer_both_directions(harness, tmp_path):
    src = write_tree(tmp_path / "host/tree", {
        "a.txt": b"a" * 10,
        "sub/b.txt": b"b" * 20,
        "sub/deep/c.txt": b"c" * 30,
        "with space.txt": b"s",
        "ünï.txt": "ü".encode(),
    })

    result = harness.run_fake("push", str(src), "/sdcard/tree")
    assert result.returncode == 0, result.stderr
    remote = harness.device_path("sdcard/tree")
    assert tree_digest(remote) == tree_digest(src)

    back = tmp_path / "back"
    result = harness.run_fake("pull", "/sdcard/tree", str(back))
    assert result.returncode == 0, result.stderr
    # pull into a missing destination copies the directory itself (cp-like)
    assert tree_digest(back) == tree_digest(src)


def test_multi_source_push_and_pull(harness, tmp_path):
    host = write_tree(tmp_path / "host", {"a.txt": b"aaa", "b.txt": b"bbb"})
    multi = harness.sdcard / "multi"
    multi.mkdir()

    result = harness.run_fake("push", str(host / "a.txt"), str(host / "b.txt"), "/sdcard/multi")
    assert result.returncode == 0, result.stderr
    assert (multi / "a.txt").read_bytes() == b"aaa"
    assert (multi / "b.txt").read_bytes() == b"bbb"

    dest = tmp_path / "multi-back"
    dest.mkdir()
    result = harness.run_fake("pull", "/sdcard/multi/a.txt", "/sdcard/multi/b.txt", str(dest))
    assert result.returncode == 0, result.stderr
    assert (dest / "a.txt").read_bytes() == b"aaa"
    assert (dest / "b.txt").read_bytes() == b"bbb"

    # Multiple sources without an existing directory destination must fail.
    result = harness.run_fake("push", str(host / "a.txt"), str(host / "b.txt"), "/sdcard/not-a-dir-target")
    assert result.returncode != 0


def test_push_pull_into_existing_directory_uses_basename(harness, tmp_path):
    host = write_tree(tmp_path / "host", {"f.txt": b"data"})
    target = harness.sdcard / "destdir"
    target.mkdir()

    result = harness.run_fake("push", str(host / "f.txt"), "/sdcard/destdir")
    assert result.returncode == 0, result.stderr
    assert (target / "f.txt").read_bytes() == b"data"

    back = tmp_path / "backdir"
    back.mkdir()
    result = harness.run_fake("pull", "/sdcard/destdir/f.txt", str(back))
    assert result.returncode == 0, result.stderr
    assert (back / "f.txt").read_bytes() == b"data"


def test_missing_source_fails_without_side_effects(harness, tmp_path):
    result = harness.run_fake("push", str(tmp_path / "nope.txt"), "/sdcard/x.txt")
    assert result.returncode == 1
    assert not harness.device_path("sdcard/x.txt").exists()

    result = harness.run_fake("pull", "/sdcard/nope.txt", str(tmp_path / "out.txt"))
    assert result.returncode == 1
    assert not (tmp_path / "out.txt").exists()


# --- failure injection -----------------------------------------------------


def test_failure_injection_partial_push_and_pull(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"W" * 1000}) / "f.txt"
    os.utime(src, (FIXED_MTIME, FIXED_MTIME))

    harness.set_fail("push:/sdcard/f.txt")
    result = harness.run_fake("push", str(src), "/sdcard/f.txt")
    assert result.returncode == 1
    dest = harness.device_path("sdcard/f.txt")
    assert dest.exists()
    assert dest.stat().st_size == 500  # partial copy: exactly half the bytes

    process_entries = [e for e in harness.log.entries() if e.get("kind") == "process"]
    assert process_entries and process_entries[-1]["failed"] is True

    harness.clear_fail()
    # Complete the file, then prove pull injection halves *it*.
    assert harness.run_fake("push", str(src), "/sdcard/f.txt").returncode == 0
    harness.set_fail("pull:/sdcard/f.txt")
    back = tmp_path / "back.txt"
    result = harness.run_fake("pull", "/sdcard/f.txt", str(back))
    assert result.returncode == 1
    assert back.exists() and back.stat().st_size == 500

    # Glob rules work too.
    harness.set_fail("push:/sdcard/*.log")
    assert harness.run_fake("push", str(src), "/sdcard/other.log").returncode == 1
    assert harness.run_fake("push", str(src), "/sdcard/other.txt").returncode == 0


def test_failure_injection_shell_operation(harness, tmp_path):
    harness.sdcard.mkdir(parents=True, exist_ok=True)
    harness.set_fail("mkdir:/sdcard/blocked")
    result = harness.run_fake("shell", "mkdir", "-p", "/sdcard/blocked")
    assert result.returncode == 1
    assert "Permission denied" in result.stdout
    assert not harness.device_path("sdcard/blocked").exists()


# --- safety: path escapes --------------------------------------------------


def test_remote_path_escape_via_symlink_is_rejected(harness, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    sdcard = harness.sdcard
    os.symlink(outside, sdcard / "evil")

    dest = tmp_path / "pulled.txt"
    result = harness.run_fake("pull", "/sdcard/evil/secret.txt", str(dest))
    assert result.returncode == 1
    assert "escapes" in result.stderr
    assert not dest.exists()

    result = harness.run_fake("push", str(dest), "/sdcard/evil/injected.txt")
    assert result.returncode == 1
    assert not (outside / "injected.txt").exists()


def test_local_path_escape_is_rejected(harness, tmp_path):
    """BETTER_ADBSYNC_FAKE_LOCAL_ROOT confines local paths to the test dir."""
    sdcard = harness.sdcard
    (sdcard / "f.txt").write_bytes(b"device file")

    outside_source = Path("/etc/hosts")  # must never be read
    result = harness.run_fake("push", str(outside_source), "/sdcard/f.txt")
    assert result.returncode == 1
    assert "escapes" in result.stderr
    assert (sdcard / "f.txt").read_bytes() == b"device file"

    outside_dest = Path("/etc/adbsync-must-not-write-here")
    result = harness.run_fake("pull", "/sdcard/f.txt", str(outside_dest))
    assert result.returncode == 1
    assert "escapes" in result.stderr


# --- adb global options and stubs ------------------------------------------


def test_global_flags_before_command(harness, tmp_path):
    host = write_tree(tmp_path / "host", {"f.txt": b"g"})

    result = harness.run_fake("-s", "emulator-5554", "push", str(host / "f.txt"), "/sdcard/f.txt")
    assert result.returncode == 0, result.stderr
    assert harness.device_path("sdcard/f.txt").read_bytes() == b"g"

    result = harness.run_fake("-P", "5037", "shell", "echo", "hi")
    assert result.returncode == 0
    assert result.stdout.strip() == "hi"

    result = harness.run_fake("-d", "version")
    assert result.returncode == 0
    assert "Android Debug Bridge" in result.stdout


def test_device_stub_commands(harness):
    result = harness.run_fake("devices")
    assert result.returncode == 0
    assert "List of devices attached" in result.stdout
    assert "FAKE01" in result.stdout

    result = harness.run_fake("get-state")
    assert result.returncode == 0
    assert result.stdout.strip() == "device"

    harness.set_no_device()
    result = harness.run_fake("devices")
    assert result.returncode == 0
    assert "FAKE01" not in result.stdout
    result = harness.run_fake("get-state")
    assert result.returncode == 1
    result = harness.run_fake("shell", ":")
    assert result.returncode == 1
    assert "no devices/emulators found" in result.stdout

    # The product's connection test must fail fast when no device is present.
    filesystem = AndroidFileSystem([str(FAKE_ADB)], "UTF-8", False)
    with pytest.raises(BrokenPipeError):
        filesystem.test_connection()
    del filesystem

    result = harness.run_fake("banana")
    assert result.returncode == 1
    assert "unknown command" in result.stderr


# --- logging / delay -------------------------------------------------------


def test_jsonl_log_schema(harness, tmp_path):
    host = write_tree(tmp_path / "host", {"f.txt": b"log-me"})
    harness.run_fake("push", str(host / "f.txt"), "/sdcard/f.txt")

    processes = harness.log.processes()
    assert len(processes) == 1
    entry = processes[0]
    for key in ("kind", "ts", "pid", "argv", "command", "exit_code", "files", "bytes", "duration_ms"):
        assert key in entry
    assert entry["command"] == "push"
    assert entry["exit_code"] == 0
    assert entry["files"] == 1
    assert entry["bytes"] == len(b"log-me")
    assert entry["duration_ms"] >= 0
    assert entry["argv"][0].endswith("fake_adb.py")
    assert entry["argv"][1] == "push"

    # A persistent shell logs one shell_cmd entry per executed command.
    fs = AndroidFileSystem([str(FAKE_ADB)], "UTF-8", False)
    fs.makedirs("/sdcard/logged")
    del fs
    shell_cmds = harness.log.shell_commands("mkdir")
    assert len(shell_cmds) == 1
    assert shell_cmds[0]["kind"] == "shell_cmd"
    assert shell_cmds[0]["exit_code"] == 0
    assert harness.log.count_processes("shell") == 1
    harness.log.settle()
    assert harness.log.count_processes("shell") == 1


def test_delay_is_applied_per_process(harness, tmp_path):
    harness.set_delay_ms(300)
    start = time.monotonic()
    result = harness.run_fake("version", timeout=30)
    wall = time.monotonic() - start
    assert result.returncode == 0
    assert wall >= 0.25
    entry = harness.log.processes("version")[-1]
    assert entry["duration_ms"] >= 250


def test_fake_never_needs_a_real_adb_on_path(harness, tmp_path):
    """With PATH containing nothing but python3, push must still work."""
    empty_bin = tmp_path / "only-python"
    empty_bin.mkdir()
    python3 = Path(shutil.which("python3"))
    os.symlink(python3, empty_bin / "python3")
    harness.monkeypatch.setenv("PATH", str(empty_bin))

    host = write_tree(tmp_path / "host", {"f.txt": b"no-adb-needed"})
    result = harness.run_fake("push", str(host / "f.txt"), "/sdcard/f.txt")
    assert result.returncode == 0, result.stderr
    assert harness.device_path("sdcard/f.txt").read_bytes() == b"no-adb-needed"


# --- timezone modelling (BETTER_ADBSYNC_FAKE_TZ_OFFSET) --------------------


def test_date_percent_z_reports_configured_utc_offset(harness):
    result = harness.run_fake("shell", "date", "+%z")
    assert result.returncode == 0
    assert result.stdout.strip() == "+0000"

    harness.set_tz_offset("+0100")
    result = harness.run_fake("shell", "date", "+%z")
    assert result.returncode == 0
    assert result.stdout.strip() == "+0100"


def test_date_rejects_invalid_offset_configuration(harness):
    harness.set_tz_offset("0100")  # missing sign
    result = harness.run_fake("shell", "date", "+%z")
    assert result.returncode == 2
    assert "expected +HHMM" in result.stderr


def test_ls_renders_mtime_as_device_local_wall_clock(harness):
    device_write(harness, "/sdcard/tz/f.txt", b"x", mtime=FIXED_MTIME)

    harness.set_tz_offset("+0100")
    result = harness.run_fake("shell", "ls", "-lab", "/sdcard/tz")
    assert result.returncode == 0
    tm = time.gmtime(FIXED_MTIME + 3600)
    stamp = "%04d-%02d-%02d %02d:%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min)
    assert stamp in result.stdout

    # Negative offsets shift the other way (e.g. -0530 = UTC-5:30).
    harness.set_tz_offset("-0530")
    result = harness.run_fake("shell", "ls", "-lab", "/sdcard/tz")
    assert result.returncode == 0
    tm = time.gmtime(FIXED_MTIME - 19800)
    stamp = "%04d-%02d-%02d %02d:%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min)
    assert stamp in result.stdout


def test_touch_stores_utc_interpreted_epoch_despite_offset(harness):
    device_file = device_write(harness, "/sdcard/tz/f.txt", b"x", mtime=FIXED_MTIME)
    harness.set_tz_offset("+0100")

    # The same string the product formats from a source epoch (naive UTC).
    tm = time.gmtime(FIXED_MTIME)
    stamp = "%04d%02d%02d%02d%02d" % (tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour, tm.tm_min)
    result = harness.run_fake("shell", "touch", "-mt", stamp, "/sdcard/tz/f.txt")
    assert result.returncode == 0
    expected = calendar.timegm(time.strptime(stamp, "%Y%m%d%H%M"))
    assert device_file.stat().st_mtime == expected


def test_android_fs_probes_date_offset_once_before_listings(fs, harness):
    """The product probes `date +%z` once per run, before any ls iteration."""
    harness.sdcard  # ensure /sdcard exists
    fs.test_connection()
    fs.lstat_in_dir("/sdcard")
    fs.lstat("/sdcard")
    fs.lstat_in_dir("/sdcard")  # memoized: no second probe

    dates = harness.log.shell_commands("date")
    assert len(dates) == 1
