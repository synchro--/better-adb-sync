# Test environment (no device, no real adb)

This is the implemented test harness for Better ADB Sync. All tests run
against `tests/fake_adb.py`, a directly executable fake `adb` passed to the
product through `adbsync --adb-bin`. No phone, Android Studio, or installed
`adb` binary is ever required or invoked, and every local path is confined to
per-test `tmp_path` directories.

See [`../docs/design/transfer-performance.md`](../docs/design/transfer-performance.md)
for the protocol contract, failure injection, test matrix, benchmark measures,
and safety rules this harness implements.

## Layout

```text
tests/
  conftest.py                 # fixtures: fake device root, host trees, adbsync
                              # runner, JSONL log analyzer (FakeAdbLog)
  fake_adb.py                 # executable ADB shim (shebang + +x); no real ADB calls
  test_fake_adb.py            # fake protocol/command contract (keep green first)
  test_transfer.py            # push/pull behavior of the current implementation
  test_performance_counts.py  # adb process-count budgets (fast path + batching)
  test_completion.py          # shell completion tests (owned by the completion work)
benchmarks/
  measure.py                  # synthetic benchmark harness (fake adb only)
  results/baseline.json|.md   # saved baseline numbers (before transfer optimization)
```

Run order matters when developing: `test_fake_adb.py` proves the fake's own
contract; the other suites build on it.

## Fake-adb environment contract

`tests/fake_adb.py` reads:

| Variable | Meaning |
|---|---|
| `BETTER_ADBSYNC_FAKE_ROOT` | **Required.** Temporary directory acting as the device filesystem. Android absolute paths (`/sdcard/...`) are mapped beneath it; anything resolving outside it (e.g. via symlinks) is rejected with exit code 1. |
| `BETTER_ADBSYNC_FAKE_LOG` | JSONL invocation log. One `kind=process` entry per fake process (`argv`, `command`, `exit_code`, `files`, `bytes`, `failed`, `duration_ms`, `pid`, `ts`) plus one `kind=shell_cmd` entry per command executed on the persistent `adb shell`. |
| `BETTER_ADBSYNC_FAKE_DELAY_MS` | Simulated latency, paid once per fake process (default 0). Models adb client startup + server connection setup. |
| `BETTER_ADBSYNC_FAKE_FAIL` | Comma-separated `op:path` rules (`op` ∈ `push pull ls realpath mkdir rm touch *`; `path` is an fnmatch pattern that also prefix-matches directories). Injected push/pull copies half the bytes and exits non-zero; injected shell commands print their standard error line, which the product treats as fatal. |
| `BETTER_ADBSYNC_FAKE_LOCAL_ROOT` | *Extension.* When set (the fixtures always set it to `tmp_path`), local paths used as push sources or pull destinations must resolve beneath it or the command fails. |
| `BETTER_ADBSYNC_FAKE_NO_DEVICE` | *Extension.* Non-empty: `devices` lists nothing, `get-state` and the `:` shell test fail like an unconnected host (used for completion/no-device behavior). |
| `BETTER_ADBSYNC_FAKE_TZ_OFFSET` | *Extension.* Device UTC offset in `date +%z` format (default `+0000`). Models a non-UTC device: `ls` renders mtimes as device-local wall clock (epoch + offset), `date +%z` reports the value, and `touch` keeps parsing with `calendar.timegm` (matching real toybox on the reference device). The product subtracts the offset when parsing `ls` output. |

Emulated surface: persistent `adb shell` speaking the exact
`ADBSYNC END OF COMMAND` sentinel protocol of `Android.py` (including the
` </dev/null` suffix on every command line), one-shot `adb shell CMD`,
`ls -ladb` / `ls -lab` (with `-b` escaping, `total` line, `.`/`..`, minute
timestamps), `realpath`, `mkdir -p`, `rm [-r]`, `touch -at/-mt`, `date +%z`,
`:`,
`push`/`pull` (single/multi source, recursive directories, cp-like target
resolution, `-a`/`-n`), plus `devices`/`get-state`/`version`/server stubs.
Global flags/options before the command (`-d`, `-s SERIAL`, `-P N`, ...) are
accepted. Exit codes: 0 success, 1 command/injection/escape failure, 2
configuration or usage error.

Deliberate deviations from real adb (all intentional, see also the module
docstring of `fake_adb.py`):

* `touch` never creates a missing file; it reports
  `touch: ...: No such file or directory`. Timestamp updates must not paper
  over a missing or partial transfer.
* ls/realpath timestamps are formatted in the fake device's timezone: UTC by
  default, or shifted by `BETTER_ADBSYNC_FAKE_TZ_OFFSET` to model a non-UTC
  device. The product parses them with `calendar.timegm`
  (`FileSystems/date_utils.py`) and subtracts the device offset (probed once
  via `date +%z`); with the default offset round trips stay exact.
* `touch` interprets its `[[CC]YY]MMDDhhmm` string with `calendar.timegm`
  (UTC wall clock) regardless of the fake timezone — matching real toybox on
  the reference device, where the stored epoch is the UTC interpretation of
  the string.
* File mode bits are not preserved; mtimes are (push always, pull with `-a`;
  adbsync re-applies mtimes itself afterwards anyway).
* The fake never execs any external command — not even `python` helpers — and
  refuses to run at all without an explicit `BETTER_ADBSYNC_FAKE_ROOT`.

## Commands

```sh
uv sync                 # once: creates .venv with pytest + editable adbsync
uv run pytest           # full suite
uv run pytest tests/test_fake_adb.py -q
uv run pytest tests/test_transfer.py -q
uv run pytest tests/test_performance_counts.py -q
uv run pytest -m "not slow" -q   # skips the 300-file budget test
```

Benchmarks (synthetic fixtures, fake adb only; ~30 s at the default 15 ms
simulated per-process latency):

```sh
uv run python benchmarks/measure.py --tag baseline
uv run python benchmarks/measure.py --delay-ms 0 --tag zero-delay
```

Results land in `benchmarks/results/<tag>.json` and `.md`. The saved
`baseline.*` files were captured on this branch **before** any transfer
optimization landed.

Shell syntax checks should run where shells are available:

```sh
bash -n completions/adbsync.bash
zsh -n completions/_adbsync
fish --no-execute completions/adbsync.fish
```

## Conventions and safety rules

* Tests use only generated files under pytest's `tmp_path`; local paths are
  fenced by `BETTER_ADBSYNC_FAKE_LOCAL_ROOT`.
* `--del` is never used in this suite. Deletion behavior stays untested here
  until a fixture-only test needs it (only ever inside `tmp_path`).
* Per-run adb process counts come from the JSONL log (`harness.log`); timing
  assertions live only in `benchmarks/measure.py`, never in pytest.
* Rerun-sensitive tests push with a **trailing slash** on the source (or to a
  full destination file path) so that adbsync's trailing-slash semantics don't
  make the second run nest a fresh directory and recopy everything.
* Former current-behavior gaps (pull exit code not checked; retries falsely
  skipped after a failed push; same-minute size changes invisible to the
  mtime-only diff) were flipped from `xfail` to ordinary passing assertions
  by the transfer work.
