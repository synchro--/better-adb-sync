# Better ADB Sync benchmark results (fake adb)

* Tag: `after`
* Generated: 2026-10-12T19:38:03+00:00
* Git: commit `f579140` (dirty)
* Simulated latency: **15.0 ms per fake-adb process** (models adb client startup + server connection; orchestration overhead only, not USB bandwidth)
* Fixtures: 300 small files (nested), 4 x 4 MiB, incremental modifies 10 files
* Python 3.14.8 on macOS-27.0.1-arm64-arm-64bit-Mach-O

| workload | direction | phase | wall (s) | adb procs | transfer procs | shell reqs | files copied | bytes copied | files/s | tree files |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| many_small | push | initial | 0.19 | 2 | 1 | 3 | 300 | 8100 | 1583.5 | 300 |
| many_small | push | noop | 0.10 | 1 | 0 | 45 | 0 | 0 | 0.0 | 300 |
| many_small | push | incremental | 0.14 | 2 | 1 | 56 | 10 | 350 | 68.9 | 300 |
| many_small | pull | initial | 0.17 | 2 | 1 | 2 | 300 | 8100 | 1725.8 | 300 |
| many_small | pull | noop | 0.10 | 1 | 0 | 45 | 0 | 0 | 0.0 | 300 |
| many_small | pull | incremental | 0.15 | 2 | 1 | 45 | 10 | 350 | 68.6 | 300 |
| few_large | push | initial | 0.14 | 2 | 1 | 3 | 4 | 16777216 | 28.8 | 4 |
| few_large | push | noop | 0.08 | 1 | 0 | 5 | 0 | 0 | 0.0 | 4 |
| few_large | pull | initial | 0.14 | 2 | 1 | 2 | 4 | 16777216 | 29.4 | 4 |
| few_large | pull | noop | 0.09 | 1 | 0 | 5 | 0 | 0 | 0.0 | 4 |

`adb procs` counts fake-adb process invocations (one persistent `adb shell` plus one process per push/pull); `shell reqs` counts individual commands on the persistent shell.
