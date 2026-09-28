# Claude Keep-Awake

A small Python script for Windows that keeps the computer from going to sleep **while Claude Code is working inside VS Code**, with a safety cap: a run that lasts longer than **2 hours** stops blocking sleep.

- Single file, standard library only (`ctypes`), no `pip install`
- No admin rights, no changes to power settings, no network access
- Read-only access to processes (`PROCESS_QUERY_LIMITED_INFORMATION`); the sleep lock is always released when the script exits
- Live metrics in the console and a summary on exit

---

## Quick start

```cmd
git clone https://github.com/<user>/claude-keep-awake.git
cd claude-keep-awake
python keep_awake.py
```

Press `Ctrl+C` to stop. The lock is released and a summary is printed. Closing the console window also releases the lock, because Windows clears it automatically when the process ends.

Requirements: Windows 10/11, Python 3.8+.

### Examples

```cmd
:: Defaults: 120-minute cap, 5 quiet minutes end a run
python keep_awake.py

:: Also keep the screen on
python keep_awake.py --keep-display

:: 3-hour cap, one output line per check, and log events to a file
python keep_awake.py --max-run 180 --verbose --log keep_awake.log
```

---

## How it works

```
 every 5 seconds
     │
     ▼
 find claude.exe whose path contains "anthropic.claude-code"   ← the VS Code extension
     │  + all descendant processes (shells, python, npm, tests that Claude runs)
     ▼
 measure CPU and I/O of the whole tree, and detect new processes
     │
     ▼
 activity? ── yes ──►  ACTIVE  ──►  SetThreadExecutionState(ES_SYSTEM_REQUIRED)
     │                   │
     │                   └── run exceeded --max-run ──►  CAPPED  (lock released)
     │
     └── no activity for --idle-grace minutes ──►  IDLE  (lock released, run closed)
```

**What a "run" is:** a run starts at the first activity and ends after `--idle-grace` consecutive minutes without activity. The grace period covers short pauses, for example while the model is thinking and little data is flowing.

**What counts as "activity"** (any one is enough):

| Signal | Default threshold |
|---|---|
| CPU of the process tree | ≥ 1.0% of one core |
| I/O of the process tree (files and network) | ≥ 2 KB/s |
| New process in the tree (Claude ran a tool) | any new process |

**What Windows receives:** only `ES_SYSTEM_REQUIRED`. The computer won't sleep, but the screen can still turn off and the lock screen still works. `--keep-display` adds `ES_DISPLAY_REQUIRED`.

> **Worth knowing:** closing a laptop lid or choosing Sleep manually **overrides** the lock. This is intended Windows behavior.

---

## Console metrics

A live line that updates in place:

```
[12:14:53] ACTIVE    | claude 1, procs   5 | cpu   4.1% | io   1.3 KB/s | run 00:00:03 | cap in 01:59:56 | awake 00:00:03 | AC 100%
```

| Field | Meaning |
|---|---|
| `ACTIVE / IDLE / CAPPED / NO CLAUDE` | Current state (`NO CLAUDE` = no Claude Code process found) |
| `claude N` | Number of Claude Code processes found (roughly one per window or conversation) |
| `procs N` | Tree size: Claude plus the tools it runs |
| `cpu` | CPU of the tree, as % of one core |
| `io` | I/O rate of the tree |
| `run` / `cap in` | Length of the current run and time left until the cap |
| `idle` | Time since the last activity |
| `awake` | Total time the lock has been held |
| `AC 100%` / `battery 64%` | Power source and battery level |

Events (run start, run end, cap reached, lock on/off) are printed as permanent lines and, with `--log`, also written to a file. On exit a summary is printed: monitor uptime, time kept awake, number of runs, runs that hit the cap, longest run, and peak CPU.

---

## Options

| Flag | Default | Description |
|---|---|---|
| `--interval` | 5 | Seconds between checks |
| `--max-run` | 120 | Minutes. A longer run stops blocking sleep |
| `--idle-grace` | 5 | Minutes without activity before a run is considered finished |
| `--cpu-threshold` | 1.0 | CPU threshold (% of one core) |
| `--io-threshold` | 2.0 | I/O threshold in KB/s |
| `--keep-display` | off | Also keep the screen on |
| `--match` | `anthropic.claude-code` | Substring of the `claude.exe` path. Repeatable |
| `--log FILE` | — | Append events to a file |
| `--verbose` | off | One output line per check instead of a live line |

### Calibration

The thresholds are based on real measurements: an idle Claude Code uses about 0% CPU and 0–700 B/s; while working it uses 0.5–5% CPU and more than 1 KB/s, plus new processes. Numbers may differ on other machines:

1. Run `python keep_awake.py --verbose` while Claude Code is open **and doing nothing**, and watch `cpu` and `io`.
2. If the state jumps to ACTIVE for no reason, raise `--io-threshold` or `--cpu-threshold`.
3. If long runs "drop" in the middle (while the model is thinking), increase `--idle-grace`.

### Verifying the lock

In an **elevated** command prompt:

```cmd
powercfg /requests
```

`python.exe` should appear under `SYSTEM:` while the state is ACTIVE.

---

## Known limitations

- **Detection is heuristic.** The script infers a run from CPU and I/O; it doesn't know for sure whether Claude is mid-task. As a result, a run may end early if a pause is longer than `--idle-grace`, and background noise may look like a short run. Hooks would solve this (see the roadmap).
- **Claude Code CLI** installed via npm runs as `node.exe`, so it is not detected by default. The native build (`~/.local/bin/claude.exe`) is supported with `--match .local\bin\claude.exe`.
- **Windows only.**
- **All VS Code windows are measured together.** Work in one window keeps the computer awake even if another window is idle. This is intentional.

---

## Roadmap

### 1. Accuracy: a reliable run signal *(highest priority)*

A keep-awake tool is judged by two failures: **the computer sleeps in the middle of a task**, or **the computer stays awake for nothing** (battery, heat, wear). The CPU/I/O heuristic is a good compromise, but not exact.

**The proper solution is [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks).** Claude Code can run a command on defined events:

| Hook | Action |
|---|---|
| `UserPromptSubmit` | Write `busy` to a state file, e.g. `%LOCALAPPDATA%\claude-keep-awake\<session_id>` |
| `Stop` / `SessionEnd` | Delete the file |

The script reads the state file and uses CPU/I/O only as a fallback. This gives exact run start and end times, per-conversation tracking, and correct detection during long silent thinking. A **timeout for stale state files** is essential (e.g. if VS Code crashed and `Stop` never ran), otherwise the computer would stay awake forever.

### 2. Testable architecture

Everything is in one file today, which suits a simple tool. A larger version would separate the layers:

```
claude-keep-awake/
├── pyproject.toml              # metadata, entry point: keep-awake = claude_keep_awake.cli:main
├── src/claude_keep_awake/
│   ├── win32.py                # all ctypes calls, and nothing else (thin OS layer)
│   ├── probes.py               # ProcessTreeProbe, HookStateProbe: return a Sample
│   ├── state.py                # IDLE→ACTIVE→CAPPED state machine, pure function of (state, sample, time)
│   ├── report.py               # console output, log, summary
│   └── cli.py                  # argparse and config file
└── tests/
    ├── test_state.py           # every transition, including cap and grace, with a fake clock
    └── test_probes.py          # win32 mocked
```

With an injected clock, a test like "a 121-minute run releases the lock" runs in milliseconds instead of two hours.

### 3. Tests and CI

| Type | What to test |
|---|---|
| Unit: state machine | Run start, end after grace, hitting the cap, new run after the cap, Claude closed mid-run, clock jumps / resume from sleep |
| Unit: deltas | Process exiting between samples, PID reuse (hence the `(pid, creation_time)` key) |
| Integration (Windows only) | `SetThreadExecutionState` called with the right flags; `powercfg /requests` shows the process |
| Smoke | `python keep_awake.py --help` and a 10-second run without exceptions |

CI: GitHub Actions on `windows-latest` with a Python 3.8–3.13 matrix, plus ruff and mypy.

### 4. Safety: when *not* to keep the computer awake

- **Low battery:** release the lock below X% (e.g. `--min-battery 20`). The script already reads `GetSystemPowerStatus`, so this is a small change.
- **On battery at all:** an `--ac-only` option, useful for a laptop in a bag.
- **Absolute cap:** in addition to the per-run cap, a maximum awake time per day.

### 5. User experience and distribution

| Item | Result |
|---|---|
| Install | `pipx install .` providing a `keep-awake` command |
| Autostart | Task Scheduler at logon with `pythonw.exe` (no window) |
| Standalone EXE | PyInstaller `--onefile` for users without Python |
| Tray icon | Color by state; menu with pause, release and exit |
| Notifications | Windows toast when a run hits the cap |
| Config | `config.toml` so flags don't need retyping |
| Single instance | Named mutex so the script can't run twice |
| Statistics | JSON-lines event log and a `--stats` command (runs per day, average length, cap hits) to tune `--max-run` from real usage |

---

## Alternatives

| Alternative | Strength | Drawback for this use case |
|---|---|---|
| PowerToys Awake | Stable, graphical UI | Doesn't know whether Claude is working; only "always" or a timer |
| `powercfg /requestsoverride` | Built in | Requires admin, persistent, not activity-based |
| Simulated mouse movement | Works everywhere | Fakes user activity, interferes with the lock screen and other apps' idle detection |
| **This tool** | Claude-aware, with a cap and metrics, no admin | Heuristic (until hooks are added), Windows only |

---

## License

[MIT](LICENSE)
