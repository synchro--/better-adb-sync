# Better ADB Sync benchmark results (fake adb)

* Tag: `zoneinfo-check`
* Generated: 2026-10-13T10:53:35+00:00
* Git: commit `688eab3` (dirty)
* Simulated latency: **15.0 ms per fake-adb process** (models adb client startup + server connection; orchestration overhead only, not USB bandwidth)
* Fixtures: 300 small files (nested), 4 x 4 MiB, incremental modifies 10 files
* Python 3.14.8 on macOS-27.0.1-arm64-arm-64bit-Mach-O

| workload | direction | phase | wall (s) | adb procs | transfer procs | shell reqs | files copied | bytes copied | files/s | tree files |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| many_small | push | initial | 0.21 | 2 | 1 | 5 | 300 | 8100 | 1458.1 | 300 |
| many_small | push | noop | 0.10 | 1 | 0 | 47 | 0 | 0 | 0.0 | 300 |
| many_small | push | incremental | 0.15 | 2 | 1 | 58 | 10 | 350 | 66.3 | 300 |
| many_small | pull | initial | 0.18 | 2 | 1 | 4 | 300 | 8100 | 1684.8 | 300 |
| many_small | pull | noop | 0.10 | 1 | 0 | 47 | 0 | 0 | 0.0 | 300 |
| many_small | pull | incremental | 0.15 | 2 | 1 | 47 | 10 | 350 | 65.6 | 300 |
| few_large | push | initial | 0.14 | 2 | 1 | 5 | 4 | 16777216 | 27.8 | 4 |
| few_large | push | noop | 0.09 | 1 | 0 | 7 | 0 | 0 | 0.0 | 4 |
| few_large | pull | initial | 0.15 | 2 | 1 | 4 | 4 | 16777216 | 26.0 | 4 |
| few_large | pull | noop | 0.09 | 1 | 0 | 7 | 0 | 0 | 0.0 | 4 |

`adb procs` counts fake-adb process invocations (one persistent `adb shell` plus one process per push/pull); `shell reqs` counts individual commands on the persistent shell.
