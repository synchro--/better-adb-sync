#!/usr/bin/env python3
"""Synthetic benchmark harness for Better ADB Sync — fake adb only, no device.

Measures the CURRENT implementation before/after transfer optimizations using
the same fake-device contract as the test suite (tests/fake_adb.py). It never
invokes a real adb binary and only touches generated files inside a private
temporary workspace.

Workloads (docs/design/transfer-performance.md, "Test and fake-ADB
environment"):

* many_small  — ~300 small files in a nested directory tree
* few_large   — a few multi-MiB files
* phases      — initial copy into an empty/missing destination, a no-op
                rerun, and an incremental rerun (~10 modified files)

Each workload runs in both directions (push and pull).

Simulated latency: BETTER_ADBSYNC_FAKE_DELAY_MS defaults to **15 ms**, paid
once per fake-adb process. This models warm adb client process startup plus
server connection setup (a first local `adb shell` round trip commonly lands
in the 10–30 ms range), so batching/fewer-process savings are visible while
remaining conservative. It measures orchestration/process-launch overhead
only — it does not model USB bandwidth (see the design doc).

Usage:

    uv run python benchmarks/measure.py [--delay-ms 15] [--tag baseline]

Outputs machine-readable JSON and a readable markdown table under
benchmarks/results/.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_ADB = REPO_ROOT / "tests" / "fake_adb.py"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "results"

ENV_ROOT = "BETTER_ADBSYNC_FAKE_ROOT"
ENV_LOG = "BETTER_ADBSYNC_FAKE_LOG"
ENV_DELAY = "BETTER_ADBSYNC_FAKE_DELAY_MS"
ENV_LOCAL_ROOT = "BETTER_ADBSYNC_FAKE_LOCAL_ROOT"


def find_adbsync():
    sibling = Path(sys.executable).with_name("adbsync")
    if sibling.exists() and os.access(sibling, os.X_OK):
        return str(sibling)
    found = shutil.which("adbsync")
    if found:
        return found
    raise SystemExit("adbsync not found; run `uv sync` (or pip install -e .) first")


def build_tree(base: Path, n_files: int, n_dirs: int = 10, n_subdirs: int = 3):
    """Nested tree: n_dirs/n_subdirs directories, ~n_files small files total."""
    per_dir = max(1, n_files // (n_dirs * n_subdirs))
    written = 0
    for d in range(n_dirs):
        for s in range(n_subdirs):
            dirpath = base / f"dir{d:02d}" / f"sub{s}"
            dirpath.mkdir(parents=True, exist_ok=True)
            for i in range(per_dir):
                rel = dirpath / f"file{i:02d}.txt"
                rel.write_bytes(f"synthetic small file {d}/{s}/{i}\n".encode())
                written += 1
    return written


def build_large(base: Path, n_files: int, size_mib: int):
    base.mkdir(parents=True, exist_ok=True)
    block = bytes(range(256)) * (1024 * 1024 // 256)  # 1 MiB, deterministic
    for i in range(n_files):
        (base / f"large{i}.bin").write_bytes((block * size_mib)[: size_mib * 1024 * 1024])


def tree_stats(base: Path):
    files = 0
    total_bytes = 0
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*")):
        if path.is_dir():
            digest.update(b"d:" + str(path.relative_to(base)).encode() + b"\n")
        elif path.is_file():
            data = path.read_bytes()
            files += 1
            total_bytes += len(data)
            digest.update(b"f:" + str(path.relative_to(base)).encode())
            digest.update(hashlib.sha256(data).digest())
    return files, total_bytes, digest.hexdigest()


def modify_files(base: Path, n: int):
    """Rewrite n files with new content and a future mtime; returns modified paths."""
    candidates = sorted(p for p in base.rglob("*") if p.is_file())[:n]
    future = time.time() + 86400
    for path in candidates:
        path.write_bytes(path.read_bytes() + b"MODIFIED")
        os.utime(path, (future, future))
    return candidates


def read_log(path: Path):
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


def log_metrics(path: Path):
    entries = read_log(path)
    processes = [e for e in entries if e.get("kind") == "process"]
    transfers = [e for e in processes if e.get("command") in ("push", "pull")]
    shell_reqs = [e for e in entries if e.get("kind") == "shell_cmd"]
    durations = [e.get("duration_ms", 0.0) for e in transfers]
    return {
        "adb_processes": len(processes),
        "transfer_processes": len(transfers),
        "shell_requests": len(shell_reqs),
        "files_copied": sum(e.get("files", 0) for e in transfers),
        "bytes_copied": sum(e.get("bytes", 0) for e in transfers),
        "transfer_avg_ms": (sum(durations) / len(durations)) if durations else 0.0,
    }


def invoke(env, adbsync, fake, args, log_path):
    run_env = dict(env)
    run_env[ENV_LOG] = str(log_path)
    cmd = [adbsync, "--adb-bin", str(fake), *args]
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=run_env, timeout=3600)
    wall = time.perf_counter() - started
    if proc.returncode != 0:
        raise RuntimeError(
            f"adbsync failed ({proc.returncode}): {' '.join(args)}\n"
            f"--- stderr ---\n{proc.stderr[-4000:]}"
        )
    return wall, proc


def git_info():
    def run(*argv):
        try:
            return subprocess.run(
                ["git", *argv], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except OSError:  # pragma: no cover
            return ""

    commit = run("rev-parse", "--short", "HEAD")
    dirty = bool(run("status", "--porcelain"))
    return {"commit": commit, "dirty": dirty}


def emit_markdown(result, out_path: Path):
    lines = []
    lines.append("# Better ADB Sync benchmark results (fake adb)")
    lines.append("")
    lines.append(f"* Tag: `{result['tag']}`")
    lines.append(f"* Generated: {result['generated_at']}")
    lines.append(f"* Git: commit `{result['git']['commit']}`"
                 f"{' (dirty)' if result['git']['dirty'] else ''}")
    cfg = result["config"]
    lines.append(f"* Simulated latency: **{cfg['delay_ms']} ms per fake-adb process**"
                 " (models adb client startup + server connection; orchestration"
                 " overhead only, not USB bandwidth)")
    lines.append(f"* Fixtures: {cfg['small_files']} small files (nested), "
                 f"{cfg['large_files']} x {cfg['large_size_mib']} MiB, "
                 f"incremental modifies {cfg['incremental_files']} files")
    lines.append(f"* Python {result['environment']['python']} on "
                 f"{result['environment']['platform']}")
    lines.append("")
    lines.append("| workload | direction | phase | wall (s) | adb procs | transfer procs"
                 " | shell reqs | files copied | bytes copied | files/s | tree files |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for run in result["runs"]:
        lines.append(
            "| {workload} | {direction} | {phase} | {wall_s:.2f} | {adb_processes}"
            " | {transfer_processes} | {shell_requests} | {files_copied}"
            " | {bytes_copied} | {files_per_s:.1f} | {tree_files} |".format(**run)
        )
    lines.append("")
    lines.append("`adb procs` counts fake-adb process invocations (one persistent"
                 " `adb shell` plus one process per push/pull); `shell reqs` counts"
                 " individual commands on the persistent shell.")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delay-ms", type=float, default=15.0,
                        help="simulated latency per fake-adb process (default: 15)")
    parser.add_argument("--small-files", type=int, default=300)
    parser.add_argument("--large-files", type=int, default=4)
    parser.add_argument("--large-size-mib", type=int, default=4)
    parser.add_argument("--incremental-files", type=int, default=10)
    parser.add_argument("--tag", default="baseline")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--workload", action="append",
                        choices=["many_small", "few_large"],
                        help="run only the given workload (repeatable)")
    args = parser.parse_args()

    workloads = args.workload or ["many_small", "few_large"]
    if not FAKE_ADB.exists():
        raise SystemExit(f"fake adb not found: {FAKE_ADB}")
    adbsync = find_adbsync()

    workspace = Path(tempfile.mkdtemp(prefix="adbsync-bench-"))
    device_root = workspace / "device"
    device_root.mkdir()
    host_root = workspace / "host"
    logs_dir = workspace / "logs"
    logs_dir.mkdir()

    env = dict(os.environ)
    env[ENV_ROOT] = str(device_root)
    env[ENV_DELAY] = str(args.delay_ms)
    env[ENV_LOCAL_ROOT] = str(workspace)
    env.pop("BETTER_ADBSYNC_FAKE_FAIL", None)
    env.pop("BETTER_ADBSYNC_FAKE_NO_DEVICE", None)

    print(f"workspace: {workspace}")
    print(f"adbsync:   {adbsync}")
    print(f"delay:     {args.delay_ms} ms per fake-adb process")

    # --- fixtures ---------------------------------------------------------
    print("building fixtures ...")
    many_small = host_root / "many_small"
    small_tree_files = build_tree(many_small, args.small_files)
    few_large = host_root / "few_large"
    build_large(few_large, args.large_files, args.large_size_mib)

    # Pull fixtures live on the fake device (populated directly, not via adb,
    # so measured runs start with an empty log).
    pull_sources = {
        "many_small": device_root / "sdcard" / "bench" / "pull" / "many_small",
        "few_large": device_root / "sdcard" / "bench" / "pull" / "few_large",
    }
    shutil.copytree(many_small, pull_sources["many_small"])
    shutil.copytree(few_large, pull_sources["few_large"])

    push_dests = {
        "many_small": "/sdcard/bench/push/many_small",
        "few_large": "/sdcard/bench/push/few_large",
    }

    def tree_files(workload):
        if workload == "many_small":
            n, total, _ = tree_stats(many_small)
        else:
            n, total, _ = tree_stats(few_large)
        return n, total

    # --- runs -------------------------------------------------------------
    runs = []
    counter = 0
    for workload in workloads:
        phases = ["initial", "noop", "incremental"] if workload == "many_small" \
            else ["initial", "noop"]
        for direction in ("push", "pull"):
            for phase in phases:
                counter += 1
                if direction == "push":
                    sync_args = ["push", str(many_small if workload == "many_small" else few_large) + "/",
                                 push_dests[workload]]
                    source_tree = many_small if workload == "many_small" else few_large
                    dest_tree = device_root / "sdcard" / "bench" / "push" / workload
                else:
                    src_apath = f"/sdcard/bench/pull/{workload}"
                    dest_dir = workspace / "back" / workload
                    sync_args = ["pull", src_apath + "/", str(dest_dir)]
                    source_tree = pull_sources[workload]
                    dest_tree = dest_dir

                if phase == "incremental":
                    modify_files(source_tree, args.incremental_files)

                log_path = logs_dir / f"{counter:02d}-{workload}-{direction}-{phase}.jsonl"
                label = f"[{workload}/{direction}/{phase}]"
                print(f"{label} running ...", flush=True)
                wall, _ = invoke(env, adbsync, FAKE_ADB, sync_args, log_path)
                metrics = log_metrics(log_path)

                # Integrity: after a copy phase the trees must match exactly.
                if phase in ("initial", "incremental"):
                    n_src, b_src, digest_src = tree_stats(source_tree)
                    n_dst, b_dst, digest_dst = tree_stats(dest_tree)
                    if digest_src != digest_dst:
                        raise RuntimeError(
                            f"{label} content mismatch: src {n_src} files/{b_src} B vs "
                            f"dst {n_dst} files/{b_dst} B"
                        )

                n_tree, b_tree = tree_files(workload)
                run_record = {
                    "workload": workload,
                    "direction": direction,
                    "phase": phase,
                    "wall_s": round(wall, 3),
                    "tree_files": n_tree,
                    "tree_bytes": b_tree,
                    "files_per_s": round(metrics["files_copied"] / wall, 2) if wall else 0.0,
                    "scan_files_per_s": round(n_tree / wall, 2) if wall else 0.0,
                    **{k: (round(v, 3) if isinstance(v, float) else v)
                       for k, v in metrics.items()},
                }
                runs.append(run_record)
                print(
                    f"{label} wall {wall:6.2f}s  adbsync-procs {metrics['adb_processes']:4d}  "
                    f"transfers {metrics['transfer_processes']:4d}  "
                    f"shell-reqs {metrics['shell_requests']:4d}  "
                    f"files {metrics['files_copied']:4d}"
                )

    result = {
        "tag": args.tag,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": git_info(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "config": {
            "delay_ms": args.delay_ms,
            "small_files": args.small_files,
            "small_files_actual": small_tree_files,
            "large_files": args.large_files,
            "large_size_mib": args.large_size_mib,
            "incremental_files": args.incremental_files,
            "workspace": str(workspace),
        },
        "runs": runs,
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{args.tag}.json"
    md_path = out_dir / f"{args.tag}.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    emit_markdown(result, md_path)
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")
    print(f"logs kept in {logs_dir}")


if __name__ == "__main__":
    main()
