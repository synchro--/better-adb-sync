"""Behavioral tests of the CURRENT adbsync implementation (no source changes).

Push and pull are exercised through the installed ``adbsync`` console script
with ``--adb-bin`` pointing at tests/fake_adb.py. Known current-behavior gaps
are marked ``xfail`` with an explicit reason so the transfer-optimization
work can flip them to passing tests.

``--del`` is deliberately never used in this suite.
"""

import os

import pytest

from conftest import device_write, tree_digest, write_tree

# Fixed, recognizable mtimes. FIXED is not minute-aligned on purpose so that
# minute-truncation in Base.py/Android.py is observable.
FIXED = 1_700_000_123
FIXED_MINUTE = FIXED - (FIXED % 60)
LATER = FIXED + 7200


# --- push: local -> fake device --------------------------------------------


def test_push_new_file_creates_destination_with_content_and_mtime(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"hello world"}) / "f.txt"
    os.utime(src, (FIXED, FIXED))

    harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt", expect=0)

    dest = harness.device_path("sdcard/dest/f.txt")
    assert dest.read_bytes() == b"hello world"
    # Android.py stamps the destination via touch with minute resolution.
    assert dest.stat().st_mtime == FIXED_MINUTE
    assert harness.log.count_transfers("push") == 1


def test_push_unchanged_file_performs_no_transfers(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"stable"}) / "f.txt"
    os.utime(src, (FIXED, FIXED))
    harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt", expect=0)

    harness.reset_log()
    result = harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt", expect=0)
    assert harness.log.count_transfers("push") == 0
    assert b"stable" == harness.device_path("sdcard/dest/f.txt").read_bytes()
    assert "Empty copy tree" in result.stderr


def test_push_modified_file_is_recopied(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"v1"}) / "f.txt"
    os.utime(src, (FIXED, FIXED))
    harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt", expect=0)

    src.write_bytes(b"v2-longer-content")
    os.utime(src, (LATER, LATER))

    harness.reset_log()
    harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt", expect=0)
    assert harness.log.count_transfers("push") == 1
    dest = harness.device_path("sdcard/dest/f.txt")
    assert dest.read_bytes() == b"v2-longer-content"
    assert dest.stat().st_mtime == LATER - (LATER % 60)


def test_push_into_missing_destination_directory(harness, tmp_path):
    src = write_tree(tmp_path / "host/srcdir", {
        "a.txt": b"a",
        "sub/b.txt": b"b",
    })

    harness.run_adbsync("push", str(src), "/sdcard/brand-new", expect=0)

    assert tree_digest(harness.device_path("sdcard/brand-new")) == tree_digest(src)
    assert harness.log.count_transfers("push") == 2


def test_push_nested_tree_content_parity(harness, tmp_path):
    src = write_tree(tmp_path / "host/tree", {
        "one.txt": b"1",
        "lvl1/two.txt": b"2",
        "lvl1/lvl2/three.txt": b"3",
        "lvl1/lvl2/lvl3/four.txt": b"4",
    })
    for path in src.rglob("*.txt"):
        os.utime(path, (FIXED, FIXED))

    harness.run_adbsync("push", str(src), "/sdcard/tree", expect=0)

    assert tree_digest(harness.device_path("sdcard/tree")) == tree_digest(src)
    assert harness.log.count_transfers("push") == 4


def test_push_filenames_with_spaces_and_unicode(harness, tmp_path):
    spec = {
        "with space.txt": b"spaces",
        "café ☃.txt": "unicode".encode(),
        "日本語 ファイル.txt": "データ".encode(),
        "sub dir/nested name.txt": b"nested",
        "empty-file.txt": b"",
    }
    src = write_tree(tmp_path / "host", spec)
    for path in src.rglob("*"):
        if path.is_file():
            os.utime(path, (FIXED, FIXED))

    harness.run_adbsync("push", str(src), "/sdcard/dest", expect=0)

    assert tree_digest(harness.device_path("sdcard/dest")) == tree_digest(src)
    assert harness.log.count_transfers("push") == len(spec)


def test_push_trailing_slash_merges_into_existing_directory(harness, tmp_path):
    src = write_tree(tmp_path / "host/srcdir", {"a.txt": b"a", "sub/b.txt": b"b"})
    dest = harness.sdcard / "dest"
    dest.mkdir()

    # Source dir with trailing slash: contents go INTO the existing dest.
    harness.run_adbsync("push", str(src) + "/", "/sdcard/dest", expect=0)
    assert (dest / "a.txt").read_bytes() == b"a"
    assert (dest / "sub" / "b.txt").read_bytes() == b"b"
    assert not (dest / "srcdir").exists()


def test_push_without_trailing_slash_nests_under_existing_directory(harness, tmp_path):
    src = write_tree(tmp_path / "host/srcdir", {"a.txt": b"a"})
    dest = harness.sdcard / "dest"
    dest.mkdir()

    harness.run_adbsync("push", str(src), "/sdcard/dest", expect=0)
    assert (dest / "srcdir" / "a.txt").read_bytes() == b"a"


def test_push_file_into_existing_directory_uses_basename(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"file"}) / "f.txt"
    dest = harness.sdcard / "destdir"
    dest.mkdir()

    harness.run_adbsync("push", str(src), "/sdcard/destdir", expect=0)
    assert (dest / "f.txt").read_bytes() == b"file"


def test_push_dry_run_makes_no_changes(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"planned"})

    result = harness.run_adbsync("-n", "push", str(src), "/sdcard/dest", expect=0)

    assert not harness.device_path("sdcard/dest").exists()
    assert harness.log.count_transfers("push") == 0
    assert "f.txt" in result.stderr  # the plan is still logged


def test_push_exclude_pattern_is_honored(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"a.txt": b"a", "skip.log": b"log"})

    harness.run_adbsync("--exclude", "*.log", "push", str(src), "/sdcard/dest", expect=0)

    dest = harness.device_path("sdcard/dest")
    assert (dest / "a.txt").read_bytes() == b"a"
    assert not (dest / "skip.log").exists()
    assert harness.log.count_transfers("push") == 1


def test_push_empty_directory_tree(harness, tmp_path):
    src = tmp_path / "host"
    (src / "empty").mkdir(parents=True)

    harness.run_adbsync("push", str(src), "/sdcard/emptytest", expect=0)

    assert harness.device_path("sdcard/emptytest").is_dir()
    assert harness.device_path("sdcard/emptytest/empty").is_dir()
    assert harness.log.count_transfers("push") == 0


def test_push_missing_source_fails(harness, tmp_path):
    result = harness.run_adbsync("push", str(tmp_path / "nope.txt"), "/sdcard/x.txt")
    assert result.returncode != 0
    assert "No such file" in result.stderr
    assert not harness.device_path("sdcard/x.txt").exists()


def test_push_failure_injection_exits_nonzero_without_stamping_mtime(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"W" * 1000}) / "f.txt"
    os.utime(src, (FIXED, FIXED))

    harness.set_fail("push:/sdcard/dest/f.txt")
    result = harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt")

    assert result.returncode != 0
    assert "Non-zero exit code from adb push" in result.stderr
    dest = harness.device_path("sdcard/dest/f.txt")
    assert dest.exists()
    assert dest.stat().st_size == 500  # partial copy, visible to a retry
    # The destination must NOT be stamped as if the transfer had succeeded.
    assert dest.stat().st_mtime != FIXED_MINUTE


@pytest.mark.xfail(reason="pull exit code not checked yet — transfer work will fix")
def test_pull_failure_injection_surfaces_nonzero_exit(harness, tmp_path):
    device_write(harness, "/sdcard/src/f.txt", b"Q" * 1000, mtime=FIXED)
    dest = tmp_path / "pulled.txt"

    harness.set_fail("pull:/sdcard/src/f.txt")
    result = harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest))

    assert result.returncode != 0


@pytest.mark.xfail(
    reason="failed push leaves a fresh-mtime partial destination and the retry is "
    "skipped; transfer work will fix (check results/sizes so retries are not falsely skipped)"
)
def test_push_retry_after_failure_is_not_falsely_skipped(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"R" * 1000}) / "f.txt"
    os.utime(src, (FIXED, FIXED))

    harness.set_fail("push:/sdcard/dest/f.txt")
    first = harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt")
    assert first.returncode != 0

    harness.clear_fail()
    harness.reset_log()
    harness.run_adbsync("push", str(src), "/sdcard/dest/f.txt", expect=0)

    assert harness.log.count_transfers("push") >= 1
    dest = harness.device_path("sdcard/dest/f.txt")
    assert dest.read_bytes() == b"R" * 1000


@pytest.mark.xfail(
    reason="diff compares minute-truncated mtimes only; a size change within the "
    "same minute is skipped — metadata work will fix"
)
def test_push_same_minute_size_change_is_detected(harness, tmp_path):
    src = write_tree(tmp_path / "host", {"f.txt": b"original"})
    os.utime(src, (FIXED, FIXED))
    harness.run_adbsync("push", str(src), "/sdcard/dest", expect=0)

    # Same mtime, different size: the current planner cannot see this change.
    (src / "f.txt").write_bytes(b"completely different and longer content")
    os.utime(src / "f.txt", (FIXED, FIXED))

    harness.reset_log()
    harness.run_adbsync("push", str(src), "/sdcard/dest", expect=0)

    dest = harness.device_path("sdcard/dest/f.txt")
    assert dest.read_bytes() == b"completely different and longer content"


# --- pull: fake device -> local --------------------------------------------


def test_pull_new_file_creates_destination_with_content_and_mtime(harness, tmp_path):
    device_write(harness, "/sdcard/src/f.txt", b"pull me", mtime=FIXED)
    dest = tmp_path / "out" / "f.txt"
    dest.parent.mkdir()

    harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest), expect=0)

    assert dest.read_bytes() == b"pull me"
    st = dest.stat()
    assert st.st_mtime == FIXED_MINUTE
    assert st.st_atime == FIXED_MINUTE
    assert harness.log.count_transfers("pull") == 1


def test_pull_unchanged_file_performs_no_transfers(harness, tmp_path):
    device_write(harness, "/sdcard/src/f.txt", b"stable", mtime=FIXED)
    dest = tmp_path / "out" / "f.txt"
    dest.parent.mkdir()
    harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest), expect=0)

    harness.reset_log()
    harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest), expect=0)
    assert harness.log.count_transfers("pull") == 0
    assert dest.read_bytes() == b"stable"


def test_pull_modified_file_is_recopied(harness, tmp_path):
    device_write(harness, "/sdcard/src/f.txt", b"v1", mtime=FIXED)
    dest = tmp_path / "f.txt"
    harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest), expect=0)

    device_write(harness, "/sdcard/src/f.txt", b"v2-changed", mtime=LATER)

    harness.reset_log()
    harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest), expect=0)
    assert harness.log.count_transfers("pull") == 1
    assert dest.read_bytes() == b"v2-changed"
    assert dest.stat().st_mtime == LATER - (LATER % 60)


def test_pull_into_missing_destination_directory(harness, tmp_path):
    device_write(harness, "/sdcard/srcdir/a.txt", b"a", mtime=FIXED)
    device_write(harness, "/sdcard/srcdir/sub/b.txt", b"b", mtime=FIXED)
    dest = tmp_path / "newdest"

    harness.run_adbsync("pull", "/sdcard/srcdir", str(dest), expect=0)

    assert (dest / "a.txt").read_bytes() == b"a"
    assert (dest / "sub" / "b.txt").read_bytes() == b"b"
    assert harness.log.count_transfers("pull") == 2


def test_pull_nested_tree_content_parity(harness, tmp_path):
    for rel, content in [("one.txt", b"1"), ("lvl1/two.txt", b"2"),
                         ("lvl1/lvl2/three.txt", b"3"), ("lvl1/lvl2/deep/four.txt", b"4")]:
        device_write(harness, f"/sdcard/tree/{rel}", content, mtime=FIXED)
    dest = tmp_path / "tree-back"

    harness.run_adbsync("pull", "/sdcard/tree", str(dest), expect=0)

    assert tree_digest(dest) == tree_digest(harness.device_path("sdcard/tree"))
    assert harness.log.count_transfers("pull") == 4


def test_pull_filenames_with_spaces_and_unicode(harness, tmp_path):
    spec = {
        "with space.txt": b"spaces",
        "café ☃.txt": "unicode".encode(),
        "日本語 ファイル.txt": "データ".encode(),
        "sub dir/nested name.txt": b"nested",
        "empty-file.txt": b"",
    }
    for rel, content in spec.items():
        device_write(harness, f"/sdcard/src/{rel}", content, mtime=FIXED)
    dest = tmp_path / "pulled"

    harness.run_adbsync("pull", "/sdcard/src", str(dest), expect=0)

    assert tree_digest(dest) == tree_digest(harness.device_path("sdcard/src"))
    assert harness.log.count_transfers("pull") == len(spec)


def test_pull_trailing_slash_merges_into_existing_directory(harness, tmp_path):
    device_write(harness, "/sdcard/srcdir/a.txt", b"a", mtime=FIXED)
    device_write(harness, "/sdcard/srcdir/sub/b.txt", b"b", mtime=FIXED)
    dest = tmp_path / "dest"
    dest.mkdir()

    harness.run_adbsync("pull", "/sdcard/srcdir/", str(dest), expect=0)
    assert (dest / "a.txt").read_bytes() == b"a"
    assert (dest / "sub" / "b.txt").read_bytes() == b"b"
    assert not (dest / "srcdir").exists()


def test_pull_without_trailing_slash_nests_under_existing_directory(harness, tmp_path):
    device_write(harness, "/sdcard/srcdir/a.txt", b"a", mtime=FIXED)
    dest = tmp_path / "dest"
    dest.mkdir()

    harness.run_adbsync("pull", "/sdcard/srcdir", str(dest), expect=0)
    assert (dest / "srcdir" / "a.txt").read_bytes() == b"a"


def test_pull_file_into_existing_directory_uses_basename(harness, tmp_path):
    device_write(harness, "/sdcard/src/f.txt", b"file", mtime=FIXED)
    dest = tmp_path / "destdir"
    dest.mkdir()

    harness.run_adbsync("pull", "/sdcard/src/f.txt", str(dest), expect=0)
    assert (dest / "f.txt").read_bytes() == b"file"


def test_pull_dry_run_makes_no_changes(harness, tmp_path):
    device_write(harness, "/sdcard/src/f.txt", b"planned", mtime=FIXED)
    dest = tmp_path / "dest"

    result = harness.run_adbsync("-n", "pull", "/sdcard/src/f.txt", str(dest), expect=0)

    assert not dest.exists()
    assert harness.log.count_transfers("pull") == 0
    assert "f.txt" in result.stderr


def test_pull_exclude_pattern_is_honored(harness, tmp_path):
    device_write(harness, "/sdcard/src/a.txt", b"a", mtime=FIXED)
    device_write(harness, "/sdcard/src/skip.log", b"log", mtime=FIXED)
    dest = tmp_path / "dest"

    harness.run_adbsync("--exclude", "*.log", "pull", "/sdcard/src", str(dest), expect=0)

    assert (dest / "a.txt").read_bytes() == b"a"
    assert not (dest / "skip.log").exists()
    assert harness.log.count_transfers("pull") == 1


def test_pull_empty_directory_tree(harness, tmp_path):
    harness.device_path("sdcard/empty/empty").mkdir(parents=True)
    dest = tmp_path / "emptyback"

    harness.run_adbsync("pull", "/sdcard/empty", str(dest), expect=0)

    assert dest.is_dir()
    assert (dest / "empty").is_dir()
    assert harness.log.count_transfers("pull") == 0


def test_pull_missing_source_fails(harness, tmp_path):
    result = harness.run_adbsync("pull", "/sdcard/nope.txt", str(tmp_path / "out.txt"))
    assert result.returncode != 0
    assert "/sdcard/nope.txt" in result.stderr
    assert not (tmp_path / "out.txt").exists()
