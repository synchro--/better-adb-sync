# Transfer performance and shell completion design

## Goal and constraints

Improve real transfer time for large file sets, especially directories containing many small files, without changing the language or requiring software on the phone. Add tab completion for Bash, Zsh, and Fish. Keep the existing CLI conflict, filtering, and delete behavior unless a deliberate change is separately reviewed.

The current branch starts at upstream `master` commit `8ad077f` (BetterADBSync 1.4.1). This document is a staged design; it does not claim the optimizations are implemented or benchmarked.

## Findings from the existing code

| Priority | Observation | Likely effect |
|---|---|---|
| High | `FileSystems/Base.py::push_tree_here` descends to each copied file. `Local.py` invokes `adb pull` per file; `Android.py` invokes `adb push` per file. | Repeated client/process and sync-connection setup is costly for many small files. The installed ADB CLI accepts multiple paths in one pull/push invocation. |
| High | A missing destination still causes a full inventory/diff and then per-file transfers. | A new folder can instead be copied recursively by a single native ADB CLI invocation when options/semantics permit. |
| High | `__init__.py` logs whole source/destination/operation trees at default INFO, sorts five diff trees, and logs each copied path. | Large/no-op jobs can spend substantial time formatting, sorting, and writing terminal output. |
| Correctness prerequisite | The normal pull path in `Local.py` uses `subprocess.run` without checking `returncode`; the `CalledProcessError` handler does not catch that failure. `Base.py` can then apply timestamps anyway. | A failed or partial transfer may appear complete and not be retried. |
| Correctness prerequisite | Android file size is parsed in `Android.py`, but `Base.py` drops it. The diff compares only mtimes rounded down to a minute. | Changed-size or partial files with equal/older timestamps can be skipped. |
| Medium, measure first | Remote inventory uses `ls -lab` per directory over a persistent `adb shell`; local and remote trees are held recursively in memory. | Deep/wide trees may spend time in metadata scans and Python tree construction. Profile before replacing this backend. |
| Medium, measure first | `Base.py` calls `utime` after each copied file; Android `utime` makes a separate shell request. | Per-file metadata requests may erase some batching gains on push. |
| Test gap | Upstream has no test suite or fake ADB fixture. CLI parsing is `argparse` and there are no completion scripts. | Changes need a deterministic no-device test harness and explicit shell-completion checks. |

These are code-inspection hypotheses, not timing measurements.

## Design

### 1. Keep the planner and introduce a batched executor

Keep Python and initially retain `FileSyncer.diff_trees` semantics. Flatten the copy portion of the existing plan into small `TransferBatch` values grouped by source and destination parent. Execute with argument arrays using ADB's multi-path form (`pull REMOTE... LOCAL`, `push LOCAL... REMOTE`). Chunk by a conservative command-byte/argument limit. Parent grouping preserves paths and prevents same-basename collisions across directories.

Do not batch deletion operations. Keep `--del` opt-in and execute its existing safety checks separately. Advanced options should use the existing/custom path unless parity tests prove the batched path equivalent.

### 2. Fast path for a new destination tree

For a normal directory source and a missing destination root, consider one recursive native `adb pull` or `adb push` before building either full file tree. Initially enable only when all behavior is understood and no option requires the custom planner: not dry-run, excludes, delete/delete-excluded, copy-links, or skip-permission-denied. Preserve source/destination trailing-slash semantics and use appropriate timestamp handling (`adb pull -a` is available). Verify symlink, mode, timestamp, partial-failure, and path-layout behavior before enabling by default. Fall back to the normal planner for unsupported cases.

This is the most valuable first-copy path: it can remove both the Python full-tree diff and the one-ADB-process-per-file overhead.

### 3. Make the default run quiet and cheaper

Default to a summary (counts/bytes copied, skipped, failed, elapsed time). Keep full trees and individual operations behind verbose/dry-run output. Do not sort whole source/destination/excluded trees solely to format logs; sort displayed plans or transfer batches only where deterministic output is useful. Keep `--show-progress` as an explicit progress mode.

### 4. Preserve transfer correctness

- Check every transfer process result and include direction, path/batch, and useful stderr in failures.
- Never update destination timestamps after a failed transfer.
- Extend metadata comparisons to include size plus mtime, with tests for minute-resolution collisions and partial files. Preserve the current source-vs-destination timestamp conflict rule unless an explicit behavior change is approved.
- Batch timestamp restoration where possible; only remove/replace it with ADB-native metadata handling after tests prove equivalent behavior.
- Do not hash all files by default; that would read every byte on each run.

`adb push --sync` may be measured as a comparison, but is not a drop-in: its metadata/skip behavior differs from the current source-newer policy and can send a source file when the destination timestamp is newer. Do not silently change conflict semantics. There is no general `adb pull --sync`; `adb sync` is for build partitions, not arbitrary media folders.

### 5. Bash, Zsh, and Fish completion

Expose one stable interface, for example `adbsync completion bash|zsh|fish`, which writes the selected completion script to stdout. Keep three shell-native adapters in `completions/` and derive static options/subcommands from the existing argparse definition where practical. Do not migrate to Click solely for completions and do not have installation modify `.bashrc`, `.zshrc`, or Fish config automatically.

Completion should support `push`/`pull`, flags/options, local path arguments, and immediate Android path children for remote arguments if responsive. Android completion must query only the requested parent (never recursively scan), honor the configured ADB binary/options where available, and fail quickly with no suggestions if no device is connected. Test spaces/Unicode and the shell-specific escaping behavior. Provide copy/source/install instructions for each shell in README.

## Step-by-step build plan

1. **Baseline:** add test tooling and the fake ADB fixture described in `tests/README.md`. Capture current invocation counts and timings on synthetic trees before changing code.
2. **Failure and metadata guardrails:** check pull exit status, preserve diagnostics, prevent post-failure timestamp updates, and add size/mtime comparison tests. Keep old behavior for conflicts, filters, and deletes.
3. **Reporting hot path:** summary-only default; full trees/per-file output only when requested; remove display-only sorting from the normal path.
4. **New-destination fast path:** add the recursive native directory transfer behind strict eligibility checks; validate both directions, trailing-slash behavior, and fallback cases.
5. **Existing-destination batching:** group changed files by compatible parent; bound batch argv size; batch timestamp work if measurements show it is still a bottleneck.
6. **Three-shell completion:** add Bash, Zsh, and Fish scripts plus `adbsync completion <shell>` output; document opt-in setup without editing shell startup files.
7. **Compare and review:** run automated tests, shell syntax/completion tests, then optional small temporary-device smoke tests. Publish measured before/after results and document which fast paths are enabled.

## Test and fake-ADB environment

Automated tests must not need a phone, Android Studio, or a real ADB binary. Implement a fake executable passed to the existing `--adb-bin` option. Each test gets a `tmp_path` host tree and a separate temporary fake-device root.

Suggested fake interface:

```text
BETTER_ADBSYNC_FAKE_ROOT=<temporary device filesystem>
BETTER_ADBSYNC_FAKE_LOG=<JSONL invocation log>
BETTER_ADBSYNC_FAKE_DELAY_MS=0       # configurable process/command latency for batching tests
BETTER_ADBSYNC_FAKE_FAIL=<operation:path>  # deterministic error/partial-copy injection
```

Fake executable requirements:

- Emulate `adb shell` as a persistent stdin/stdout process because `AndroidFileSystem` keeps one shell open. Support `:`, `ls -ladb`, `ls -lab`, `realpath`, `mkdir -p`, `rm`, `touch`, and the command sentinel. Produce `ls` lines compatible with the existing parser, including file size and minute-resolution timestamps.
- Map Android absolute paths beneath `BETTER_ADBSYNC_FAKE_ROOT`; reject traversal outside that root.
- Emulate push/pull file and recursive-directory transfers, including multiple source paths and the relevant ADB options. Log each process invocation, argv, result, files, and bytes so tests can assert batching.
- Support one-shot/non-zero failures and partial-copy injection. This proves the CLI surfaces errors and does not mark failed work complete.
- Never call host `rm` or a real ADB binary. Tests use temporary directories and delete only their own fixtures.

Automated test matrix:

- Push and pull: new file, existing unchanged file, modified/new file, missing destination directory, nested tree.
- Many small files in one directory: content matches; after batching, one invocation per batch rather than one per file. Test command-size chunking.
- Large synthetic file: exact content hash before/after; ensure no material large-file throughput regression in optional benchmark.
- Error injection: non-zero ADB exit, missing source, partial transfer; command exits non-zero and retry is not falsely skipped.
- Behavior: spaces/Unicode paths, trailing slash, empty directories, symlinks/fallback, mtime/size collisions, dry-run, excludes, permission-denied mode, and `--del` only within a fixture root.
- Performance: initial missing-destination copy, incremental many-small-file copy, and no-op/deep-tree scan. Record elapsed time, files/sec, bytes/sec, number of ADB processes/remote shell requests, and peak memory. Fake delay measures orchestration/process-launch savings only; it does not claim to simulate USB bandwidth.
- Completion: check output for all three shells; run `bash -n`, `zsh -n`, and `fish --no-execute` where available; integration tests use the fake ADB device for remote-path suggestions and verify no-device timeout behavior.

Add a small optional real-device smoke test using a unique temporary folder and generated data only. Never use the user's DCIM/MyAlbums, and never run `--del` against real phone data.

## Acceptance criteria

- A compatible first copy uses one recursive host-side ADB invocation rather than one per file.
- Incremental same-parent transfers use bounded batches; tests prove path/content parity and lower process counts.
- Default no-op output is concise and does not render complete trees.
- Failed ADB commands are visible and cannot be mistaken for completed transfers.
- All three completion formats install/source correctly and complete commands/options/local paths; remote path completion is bounded and graceful without a device.
- Performance report shows many-small-file gains and no large-file regression beyond measurement noise. No fixed speedup is promised before measurement.

## Non-goals for this branch

No Rust rewrite, default compression, multiple concurrent ADB processes, full-content hashing, phone-side rsync/Termux/root, persistent manifest/cache, or custom ADB wire-protocol client unless measured results later justify a separate proposal. These add complexity and are not currently supported by evidence.
