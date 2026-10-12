# Better ADB Sync benchmark results (fake adb)

* Tag: `baseline`
* Generated: 2026-10-12T19:12:36+00:00
* Git: commit `7e6f279` (dirty)
* Simulated latency: **15.0 ms per fake-adb process** (models adb client startup + server connection; orchestration overhead only, not USB bandwidth)
* Fixtures: 300 small files (nested), 4 x 4 MiB, incremental modifies 10 files
* Python 3.14.8 on macOS-27.0.1-arm64-arm-64bit-Mach-O

| workload | direction | phase | wall (s) | adb procs | transfer procs | shell reqs | files copied | bytes copied | files/s | tree files |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| many_small | push | initial | 14.16 | 301 | 300 | 344 | 300 | 8100 | 21.2 | 300 |
| many_small | push | noop | 0.12 | 1 | 0 | 44 | 0 | 0 | 0.0 | 300 |
| many_small | push | incremental | 0.56 | 11 | 10 | 64 | 10 | 350 | 17.8 | 300 |
| many_small | pull | initial | 14.11 | 301 | 300 | 43 | 300 | 8100 | 21.3 | 300 |
| many_small | pull | noop | 0.12 | 1 | 0 | 44 | 0 | 0 | 0.0 | 300 |
| many_small | pull | incremental | 0.58 | 11 | 10 | 44 | 10 | 350 | 17.1 | 300 |
| few_large | push | initial | 0.30 | 5 | 4 | 8 | 4 | 16777216 | 13.3 | 4 |
| few_large | push | noop | 0.09 | 1 | 0 | 4 | 0 | 0 | 0.0 | 4 |
| few_large | pull | initial | 0.30 | 5 | 4 | 3 | 4 | 16777216 | 13.3 | 4 |
| few_large | pull | noop | 0.09 | 1 | 0 | 4 | 0 | 0 | 0.0 | 4 |

`adb procs` counts fake-adb process invocations (one persistent `adb shell` plus one process per push/pull); `shell reqs` counts individual commands on the persistent shell.
