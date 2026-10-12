"""Tests for the Bash/Zsh/Fish shell completion support.

Owned by the completion work. These tests run the real `adbsync` CLI in a
subprocess (via ``python -m BetterADBSync``) with the source tree on
PYTHONPATH, so they pass both from an installed package and from a plain
source checkout without any packaging changes.

Safety: no test here touches a real device or the user's adb; the remote-path
helper is exercised only with deliberately broken/hanging stubs or (once it
exists) the shared fake ADB from tests/fake_adb.py.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
COMPLETIONS_DIR = REPO_ROOT / "completions"

SHELLS = ("bash", "zsh", "fish")

CHECKED_IN_SNAPSHOTS = {
    "bash": COMPLETIONS_DIR / "adbsync.bash",
    "zsh": COMPLETIONS_DIR / "_adbsync",
    "fish": COMPLETIONS_DIR / "adbsync.fish",
}

# Tokens each generated script must contain. Fish spells options as
# `-l dry-run`/`-s n` rather than `--dry-run`/`-n`, so its list differs.
EXPECTED_TOKENS = {
    "bash": (
        "push", "pull", "completion", "completion-remote-paths",
        "-n", "--dry-run", "--adb-bin", "--adb-flag", "--adb-option",
        "--exclude", "--del", "complete -F _adbsync adbsync",
    ),
    "zsh": (
        "#compdef adbsync", "push", "pull", "completion",
        "-n", "--dry-run", "--adb-bin", "--adb-flag", "--adb-option",
        "--exclude", "--del", "_arguments",
    ),
    "fish": (
        "complete -c adbsync", "push", "pull", "completion",
        "-s n", "dry-run", "adb-bin", "adb-flag", "adb-option",
        "exclude", "-l del", "__fish_use_subcommand",
    ),
}


def _metadata_stub(tmp: Path) -> Path:
    """Minimal dist-info so `importlib.metadata.version` works for an
    uninstalled source checkout (the package reads __version__ at import)."""
    dist_info = tmp / "BetterADBSync-1.4.1.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: BetterADBSync\nVersion: 1.4.1\n",
        encoding="utf-8",
    )
    return tmp


def _adbsync_env(tmp: Path) -> dict:
    env = os.environ.copy()
    python_path_entries = [str(SRC_DIR), str(_metadata_stub(tmp))]
    if env.get("PYTHONPATH"):
        python_path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_entries)
    return env


def _run_adbsync(tmp: Path, *args: str, timeout: float = 60, env=None) -> subprocess.CompletedProcess:
    if env is None:
        env = _adbsync_env(tmp)
    return subprocess.run(
        [sys.executable, "-m", "BetterADBSync", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


# ---------------------------------------------------------------------------
# `adbsync completion <shell>` output
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell", SHELLS)
def test_completion_prints_nonempty_script(tmp_path, shell):
    result = _run_adbsync(tmp_path, "completion", shell)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), "completion script must not be empty"
    for token in EXPECTED_TOKENS[shell]:
        assert token in result.stdout, f"missing {token!r} in {shell} completion script"


@pytest.mark.parametrize("shell", SHELLS)
def test_checked_in_snapshot_matches_generated_output(tmp_path, shell):
    snapshot_path = CHECKED_IN_SNAPSHOTS[shell]
    assert snapshot_path.exists(), (
        f"missing checked-in snapshot {snapshot_path}; regenerate with "
        f"`adbsync completion {shell}`"
    )
    result = _run_adbsync(tmp_path, "completion", shell)
    assert result.returncode == 0, result.stderr
    assert result.stdout == snapshot_path.read_text(encoding="utf-8"), (
        f"completions snapshot for {shell} is stale; regenerate with "
        f"`adbsync completion {shell}`"
    )


def test_completion_unknown_shell_is_clean_argparse_error(tmp_path):
    result = _run_adbsync(tmp_path, "completion", "csh")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# Shell syntax checks (each skipped when its shell is not installed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell,argv", [
    ("bash", ["bash", "-n", str(CHECKED_IN_SNAPSHOTS["bash"])]),
    ("zsh", ["zsh", "-n", str(CHECKED_IN_SNAPSHOTS["zsh"])]),
    ("fish", ["fish", "--no-execute", str(CHECKED_IN_SNAPSHOTS["fish"])]),
])
def test_checked_in_snapshot_passes_shell_syntax_check(shell, argv):
    if shutil.which(argv[0]) is None:
        pytest.skip(f"{argv[0]} not installed")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_bash_completion_functional_subcommands():
    if shutil.which("bash") is None:
        pytest.skip("bash not installed")
    program = (
        f"source {CHECKED_IN_SNAPSHOTS['bash']}\n"
        'COMP_WORDS=(adbsync ""); COMP_CWORD=1\n'
        "_adbsync\n"
        'printf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", program], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert {"push", "pull", "completion"} <= set(result.stdout.split())


def test_fish_completion_functional_subcommands():
    if shutil.which("fish") is None:
        pytest.skip("fish not installed")
    program = (
        f"source {CHECKED_IN_SNAPSHOTS['fish']}; "
        "complete -C 'adbsync '"
    )
    result = subprocess.run(
        ["fish", "-c", program], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    completed = {line.split("\t")[0] for line in result.stdout.splitlines() if line}
    assert {"push", "pull", "completion"} <= completed


# ---------------------------------------------------------------------------
# Hidden remote-path helper: must never hang and must degrade gracefully
# ---------------------------------------------------------------------------

def test_remote_paths_helper_missing_adb_prints_nothing(tmp_path):
    """A nonexistent adb binary yields no suggestions and exit 0, silently."""
    result = _run_adbsync(
        tmp_path,
        "--adb-bin", str(tmp_path / "no-such-adb"),
        "completion-remote-paths", "/sdcard",
    )
    assert result.returncode == 0
    assert result.stdout == ""


def test_remote_paths_helper_times_out_never_hangs(tmp_path):
    """A hung adb is cut off by the helper's internal timeout (~1.25s)."""
    stub = tmp_path / "hanging-adb"
    stub.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    stub.chmod(0o755)
    start = time.monotonic()
    result = _run_adbsync(
        tmp_path,
        "--adb-bin", str(stub),
        "--adb-flag", "s",
        "--adb-option", "P", "5037",
        "completion-remote-paths", "/sdcard",
        timeout=30,
    )
    elapsed = time.monotonic() - start
    assert result.returncode == 0
    assert result.stdout == ""
    # Generous bound around REMOTE_PATHS_TIMEOUT_SECONDS (1.25s) plus
    # interpreter startup; the hard subprocess timeout above is the real
    # no-hang guarantee.
    assert elapsed < 15


# ---------------------------------------------------------------------------
# Hidden remote-path helper against the shared fake ADB (tests/fake_adb.py)
# ---------------------------------------------------------------------------

def test_remote_paths_helper_lists_immediate_children(harness):
    """Immediate children only: dirs get "/", files are plain, spaces and
    Unicode survive ls -b escaping, and nothing is scanned recursively."""
    (harness.sdcard / "Download").mkdir()
    (harness.sdcard / "Download" / "deep.txt").write_text("deep", encoding="utf-8")
    (harness.sdcard / "notes.txt").write_text("notes", encoding="utf-8")
    (harness.sdcard / "my file.txt").write_text("spaced", encoding="utf-8")
    (harness.sdcard / "写真").mkdir()

    result = harness.run_adbsync("completion-remote-paths", "/sdcard", expect=0)
    lines = result.stdout.splitlines()

    assert "Download/" in lines
    assert "写真/" in lines
    assert "notes.txt" in lines
    assert "my file.txt" in lines
    # Self/parent entries are never suggested.
    assert "./" not in lines
    assert "../" not in lines
    # Never recursive: children of /sdcard only.
    assert "deep.txt" not in lines


def test_remote_paths_helper_adb_failure_prints_nothing(harness):
    """An injected adb ls failure degrades to no suggestions, exit 0.

    (True no-device behavior on a real adb surfaces as a broken adb shell
    pipe, which takes the same exception path in the helper.)
    """
    (harness.sdcard / "Download").mkdir()
    harness.set_fail("ls:*")
    result = harness.run_adbsync("completion-remote-paths", "/sdcard", expect=0)
    assert result.stdout == ""


def test_remote_paths_helper_missing_remote_dir_prints_nothing(harness):
    """A remote path that does not exist degrades to no suggestions."""
    result = harness.run_adbsync("completion-remote-paths", "/no/such/dir", expect=0)
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# Existing CLI behavior must be unchanged
# ---------------------------------------------------------------------------

def test_help_output_lists_directions(tmp_path):
    result = _run_adbsync(tmp_path, "--help")
    assert result.returncode == 0
    for token in ("push", "pull", "completion"):
        assert token in result.stdout


def test_push_requires_local_and_android(tmp_path):
    result = _run_adbsync(tmp_path, "push")
    assert result.returncode == 2
    assert "the following arguments are required: LOCAL, ANDROID" in result.stderr


def test_pull_requires_android_and_local(tmp_path):
    result = _run_adbsync(tmp_path, "pull")
    assert result.returncode == 2
    assert "the following arguments are required: ANDROID, LOCAL" in result.stderr
