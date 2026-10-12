# Agent instructions

## Project and task scope

- This is the Python 3.10+ Better ADB Sync project. Runtime sources live in `src/BetterADBSync/`; the command is `adbsync`.
- This branch is for measured ADB transfer improvements and shell completion for **Bash, Zsh, and Fish**. Keep the data plane on the existing ADB CLI; do not port the project to Rust or require an app/root access on the Android device.
- Follow the design and acceptance criteria in `docs/design/transfer-performance.md`.

## Safety and compatibility

- Preserve existing CLI/path semantics. Deletion stays opt-in (`--del`); tests must never use it against real user data.
- Use subprocess argument lists. Do not interpolate user paths into shell command strings.
- Check transfer exit codes. A failed copy must not be reported as successful or have timestamps updated as if it succeeded.
- Use only generated temporary files and a fake ADB executable in automated tests. Never access the user's personal phone folders or photo library from tests/benchmarks.
- Keep a fast path's eligibility narrow; unsupported options and semantics must fall back to the existing planner.

## Testing and performance claims

- Add tests before changing transfer execution. Use the fake-device contract in `tests/README.md` so tests require no connected phone or installed `adb`.
- Cover both push and pull, dry-run, filters, trailing-slash/path layout, spaces/Unicode, errors/partial copies, and all three shell completion outputs.
- Measure many-small-file, few-large-file, initial-copy, and no-op workloads against the same fixtures before and after. Report process counts and timings; do not claim speedups without measurements.
- Keep completion generation separate from transfer execution. Do not modify a developer's shell startup files automatically.

## Suggested ownership when delegating

- Transfer implementation: `src/BetterADBSync/__init__.py` and `src/BetterADBSync/FileSystems/`.
- Completion implementation: `src/BetterADBSync/argparsing.py`, `completions/`, and completion documentation.
- Test/fake-ADB implementation: `tests/` and `pyproject.toml` test configuration.
- Keep ownership distinct; integration/review should verify that all advanced-option paths retain their old behavior.
