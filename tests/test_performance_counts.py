"""adb invocation-count budgets read from the fake adb JSONL log.

These tests pin the POST-optimization behavior:

1. **Fast path**: a first copy into a missing destination uses ONE recursive
   adb push/pull process — no per-file transfers and no remote inventory.
2. **Batched incremental**: changed files are transferred in one adb process
   per (destination parent directory, command-size chunk), never one per file.
3. **Budgets** from docs/design/transfer-performance.md: a small handful of
   processes for the 300-file initial copy and the 30-file incremental copy.

No wall-clock assertions here (no flaky timing gates) — timings live in
benchmarks/measure.py.

Note: rerun-sensitive pushes use a trailing slash on the source so that reruns
target the same destination path (without it, an existing destination would
make the second run nest a new source-basename directory, per adbsync's
trailing-slash semantics, and everything would be recopied).
"""

import os

import pytest

from conftest import write_tree

pytestmark = pytest.mark.performance

FIXED = 1_700_000_123
LATER = FIXED + 7200


def build_many_small(base, n_files, n_dirs=4):
    """n_files small files spread over n_dirs subdirectories; returns base."""
    spec = {}
    for i in range(n_files):
        d = i % n_dirs
        spec[f"dir{d}/file{i:04d}.txt"] = f"content of file {i}\n".encode()
    return write_tree(base, spec)


# --- fast path: first copy into a missing destination -----------------------


def test_initial_push_to_missing_destination_uses_one_recursive_transfer(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 40)

    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.count_transfers("push") == 1  # one recursive adb push
    assert harness.log.count_transfers("pull") == 0
    assert harness.log.count_processes("shell") == 1  # one persistent shell per run
    # No remote inventory: the missing destination is only probed
    # (path fixing + fast-path eligibility).
    assert harness.log.count_shell("ls") == 2
    assert harness.log.transferred_files() == 40


def test_initial_pull_to_missing_destination_uses_one_recursive_transfer(harness, tmp_path):
    from conftest import device_write

    for i in range(20):
        device_write(harness, f"/sdcard/src/file{i:02d}.txt", b"x" * 50, mtime=FIXED)

    harness.run_adbsync("pull", "/sdcard/src", str(tmp_path / "back"), expect=0)
    harness.log.settle()

    assert harness.log.count_transfers("pull") == 1  # one recursive adb pull
    assert harness.log.count_transfers("push") == 0
    assert harness.log.count_processes("shell") == 1
    # No remote inventory: a single lstat probe of the source directory.
    assert harness.log.count_shell("ls") == 1
    assert harness.log.transferred_files() == 20


# --- batched incremental copies ----------------------------------------------


def test_incremental_push_batches_modified_files_per_parent_directory(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 30)
    for path in src.rglob("*.txt"):
        os.utime(path, (FIXED, FIXED))
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)

    modified = sorted(src.rglob("*.txt"))[:5]  # all live in dir0/
    for path in modified:
        path.write_bytes(b"modified content")
        os.utime(path, (LATER, LATER))

    harness.reset_log()
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.count_transfers("push") == 1  # one batch for the shared parent
    assert harness.log.count_processes("shell") == 1
    assert harness.log.transferred_files() == 5


def test_noop_rerun_spawns_no_transfer_processes(harness, tmp_path):
    """A no-op rerun copies nothing.

    The run still performs one persistent inventory shell — a manifest cache
    is explicitly a non-goal of this branch — but it must spawn zero
    push/pull processes.
    """
    src = build_many_small(tmp_path / "host", 40)
    for path in src.rglob("*.txt"):
        os.utime(path, (FIXED, FIXED))
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)

    harness.reset_log()
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.count_transfers() == 0
    assert harness.log.count_processes("shell") == 1
    assert harness.log.transferred_bytes() == 0


# --- design-doc budgets -------------------------------------------------------


@pytest.mark.slow
def test_budget_initial_copy_of_300_small_files_uses_a_small_handful_of_processes(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 300, n_dirs=10)

    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.transferred_files() == 300  # all files must still arrive
    assert harness.log.count_transfers() <= 8  # one recursive transfer today


def test_budget_incremental_changes_transfer_in_bounded_batches(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 30, n_dirs=3)
    for path in src.rglob("*.txt"):
        os.utime(path, (FIXED, FIXED))
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)

    for path in sorted(src.rglob("*.txt")):
        path.write_bytes(b"changed")
        os.utime(path, (LATER, LATER))

    harness.reset_log()
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.transferred_files() == 30
    # 30 files across 3 parent dirs: one batch per parent directory.
    assert harness.log.count_transfers() <= 6
