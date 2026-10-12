# Test environment plan

The upstream project currently has no test suite. Before transfer execution is changed, add a no-device test harness using `pytest`, `tmp_path`, and a fake executable supplied through `adbsync --adb-bin`.

See [`../docs/design/transfer-performance.md`](../docs/design/transfer-performance.md) for the fake-ADB protocol contract, failure injection, test matrix, benchmark measures, and safety rules.

## Planned layout

```text
tests/
  conftest.py                 # per-test fake device root and environment
  fake_adb.py                 # executable ADB shim; no real ADB calls
  test_fake_adb.py            # fake protocol/command contract
  test_transfer.py            # push/pull, fast path, batches, failures, paths
  test_completion.py          # bash/zsh/fish output and remote suggestions
  test_performance_counts.py  # invocation-count budgets; no flaky wall-clock gate
```

The fake-device root and host fixtures must be isolated per test. Simulated delay is for demonstrating reduced process/command overhead only; benchmark throughput on the real device separately with a small generated fixture if needed.

## Planned developer commands

After adding the test dependency/configuration:

```sh
uv run pytest
uv run pytest tests/test_transfer.py -q
uv run pytest tests/test_completion.py -q
```

Shell syntax checks should run where shells are available:

```sh
bash -n completions/adbsync.bash
zsh -n completions/_adbsync
fish --no-execute completions/adbsync.fish
```

These files describe the handoff and test contract only; the fake executable and product changes have not been implemented yet.
