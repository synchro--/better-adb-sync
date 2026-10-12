"""adb invocation-count budgets read from the fake adb JSONL log.

Two kinds of tests live here:

1. **Documented baseline** (passing): the CURRENT implementation spawns one
   transfer process per changed file and exactly one persistent `adb shell`
   process per run. These pin the pre-optimization behavior so any change to
   process counts is visible.

2. **Post-optimization budgets** (marked xfail): the desired budgets from
   docs/design/transfer-performance.md. They fail today by design; the
   transfer work should turn them into passing tests by flipping the marker.

No wall-clock assertions here (no flaky timing gates) — timings live in
benchmarks/measure.py.

Note: all pushes use a trailing slash on the source so that reruns target the
same destination path (without it, an existing destination would make the
second run nest a new source-basename directory, per adbsync's trailing-slash
semantics, and everything would be recopied).
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


# --- documented current baseline (passing) ---------------------------------


def test_baseline_initial_push_spawns_one_transfer_process_per_file(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 40)

    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.count_transfers("push") == 40  # baseline: one process per file
    assert harness.log.count_transfers("pull") == 0
    assert harness.log.count_processes("shell") == 1  # one persistent shell per run
    # The source tree is listed locally; only the (missing) destination is
    # probed twice over the shell (path fixing + tree inventory).
    assert harness.log.count_shell("ls") == 2
    assert harness.log.transferred_files() == 40


def test_baseline_incremental_push_spawns_one_transfer_process_per_modified_file(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 30)
    for path in src.rglob("*.txt"):
        os.utime(path, (FIXED, FIXED))
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)

    modified = sorted(src.rglob("*.txt"))[:5]
    for path in modified:
        path.write_bytes(b"modified content")
        os.utime(path, (LATER, LATER))

    harness.reset_log()
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.count_transfers("push") == 5  # baseline: one per changed file
    assert harness.log.count_processes("shell") == 1


def test_baseline_pull_spawns_one_transfer_process_per_file(harness, tmp_path):
    from conftest import device_write

    for i in range(20):
        device_write(harness, f"/sdcard/src/file{i:02d}.txt", b"x" * 50, mtime=FIXED)

    harness.run_adbsync("pull", "/sdcard/src", str(tmp_path / "back"), expect=0)
    harness.log.settle()

    assert harness.log.count_transfers("pull") == 20  # baseline: one per file
    assert harness.log.count_transfers("push") == 0
    assert harness.log.count_processes("shell") == 1


def test_budget_noop_run_spawns_no_transfer_processes(harness, tmp_path):
    """Desired budget (already met today): a no-op rerun copies nothing.

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


# --- desired post-optimization budgets (xfail until the transfer work) -----


@pytest.mark.slow
@pytest.mark.xfail(
    reason="baseline spawns one push process per file (300); desired budget after "
    "optimization is a small handful of adb processes (batched or one recursive "
    "transfer) — transfer work will flip this"
)
def test_budget_initial_copy_of_300_small_files_uses_a_small_handful_of_processes(harness, tmp_path):
    src = build_many_small(tmp_path / "host", 300, n_dirs=10)

    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    harness.log.settle()

    assert harness.log.transferred_files() == 300  # all files must still arrive
    assert harness.log.count_transfers() <= 8


@pytest.mark.xfail(
    reason="baseline spawns one push process per modified file (30); desired budget "
    "after optimization is a bounded batch per parent directory — transfer work "
    "will flip this"
)
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
    # 30 files across 3 parent dirs: a handful of batched processes, not 30.
    assert harness.log.count_transfers() <= 6
