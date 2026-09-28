"""
keep_awake.py - Prevent Windows from sleeping while Claude Code (VS Code) is working.

Usage (from CMD):
    python keep_awake.py
    python keep_awake.py --max-run 120 --idle-grace 5 --keep-display

How it works
------------
1. Every --interval seconds, find the Claude Code processes started by the VS Code
   extension (claude.exe under ...\\anthropic.claude-code-*\\) and all their descendants
   (tools Claude runs: shells, python, npm, tests...).
2. Measure the CPU and I/O rate of that process tree. Above the thresholds = "activity".
3. While a run is active, call SetThreadExecutionState(ES_SYSTEM_REQUIRED) so the
   system does not go to sleep. The screen may still turn off unless --keep-display.
4. A run ends after --idle-grace minutes without activity.
5. If a single run lasts longer than --max-run minutes (default 120), the lock is
   released and the computer may sleep normally, until the run ends.

Standard library only (ctypes). Windows only. Python 3.8+.
"""
from __future__ import annotations

import argparse
import ctypes
import shutil
import sys
import time
from ctypes import wintypes as wt

if sys.platform != "win32":
    sys.exit("keep_awake.py works on Windows only.")

# --------------------------------------------------------------------------- Win32 API

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = wt.HANDLE(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wt.BYTE),
        ("BatteryFlag", wt.BYTE),
        ("BatteryLifePercent", wt.BYTE),
        ("SystemStatusFlag", wt.BYTE),
        ("BatteryLifeTime", wt.DWORD),
        ("BatteryFullLifeTime", wt.DWORD),
    ]


# Exact signatures, so 64-bit handles and pointers are never truncated to a 32-bit int.
_P = ctypes.POINTER
for _name, _restype, _argtypes in [
    ("CreateToolhelp32Snapshot", wt.HANDLE, [wt.DWORD, wt.DWORD]),
    ("Process32FirstW", wt.BOOL, [wt.HANDLE, _P(PROCESSENTRY32W)]),
    ("Process32NextW", wt.BOOL, [wt.HANDLE, _P(PROCESSENTRY32W)]),
    ("OpenProcess", wt.HANDLE, [wt.DWORD, wt.BOOL, wt.DWORD]),
    ("CloseHandle", wt.BOOL, [wt.HANDLE]),
    ("QueryFullProcessImageNameW", wt.BOOL, [wt.HANDLE, wt.DWORD, wt.LPWSTR, _P(wt.DWORD)]),
    ("GetProcessTimes", wt.BOOL, [wt.HANDLE] + [_P(wt.FILETIME)] * 4),
    ("GetProcessIoCounters", wt.BOOL, [wt.HANDLE, _P(IO_COUNTERS)]),
    ("GetSystemPowerStatus", wt.BOOL, [_P(SYSTEM_POWER_STATUS)]),
    ("SetThreadExecutionState", wt.DWORD, [wt.DWORD]),
]:
    _fn = getattr(kernel32, _name)
    _fn.restype, _fn.argtypes = _restype, _argtypes


def _filetime_seconds(ft: wt.FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def list_processes() -> list[tuple[int, int, str]]:
    """Return [(pid, parent_pid, exe_name)] for every process."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    procs = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            procs.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return procs


def process_info(pid: int):
    """Return (image_path, created, cpu_seconds, io_bytes) or None if not accessible."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        path = buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""

        created, exited, kernel, user = (wt.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        io = IO_COUNTERS()
        io_bytes = 0
        if kernel32.GetProcessIoCounters(handle, ctypes.byref(io)):
            io_bytes = io.ReadTransferCount + io.WriteTransferCount + io.OtherTransferCount
        cpu = _filetime_seconds(kernel) + _filetime_seconds(user)
        return path, _filetime_seconds(created), cpu, io_bytes
    finally:
        kernel32.CloseHandle(handle)


def set_awake(awake: bool, keep_display: bool) -> None:
    flags = ES_CONTINUOUS
    if awake:
        flags |= ES_SYSTEM_REQUIRED
        if keep_display:
            flags |= ES_DISPLAY_REQUIRED
    kernel32.SetThreadExecutionState(flags)


def power_source() -> str:
    status = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return "?"
    source = {0: "battery", 1: "AC"}.get(status.ACLineStatus, "?")
    pct = status.BatteryLifePercent
    return source if pct == 255 else "%s %d%%" % (source, pct)


# --------------------------------------------------------------------------- monitoring

def sample_tree(match: list[str]) -> tuple[int, dict]:
    """Measure the Claude Code processes and every process they started.

    Returns (number of Claude Code processes,
             {(pid, creation_time): (cpu_seconds, io_bytes)} for the whole tree).
    """
    procs = list_processes()
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in procs:
        children.setdefault(ppid, []).append(pid)

    stack = []
    for pid, _, name in procs:
        if name.lower() == "claude.exe":
            info = process_info(pid)
            if info and any(m in info[0].lower() for m in match):
                stack.append((pid, info))
    roots = len(stack)

    counters = {}
    while stack:
        pid, (_, created, cpu, io) = stack.pop()
        if (pid, created) in counters:
            continue
        counters[(pid, created)] = (cpu, io)
        for child in children.get(pid, ()):
            info = process_info(child)
            # A "child" older than its parent is orphaned: its real parent died and
            # Windows reused the PID. It does not belong to the tree.
            if info and info[1] >= created:
                stack.append((child, info))
    return roots, counters


def fmt_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def fmt_rate(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s"):
        if bps < 1024:
            return "%.1f %s" % (bps, unit)
        bps /= 1024
    return "%.1f GB/s" % bps


def line_width() -> int:
    return shutil.get_terminal_size().columns - 1


class Monitor:
    IDLE, ACTIVE, CAPPED, NO_CLAUDE = "IDLE", "ACTIVE", "CAPPED", "NO CLAUDE"

    def __init__(self, args):
        self.args = args
        self.state = self.IDLE         # the sleep lock is held only in ACTIVE
        self.start = time.monotonic()
        self.run_start = None          # monotonic time the current run began
        self.last_activity = None      # monotonic time of the last detected activity
        self.prev_counters = {}
        self.prev_t = None
        self.awake_total = 0.0         # seconds the sleep lock was held
        self.runs = 0
        self.capped_runs = 0
        self.longest_run = 0.0
        self.peak_cpu = 0.0

    def event(self, message: str) -> None:
        print("\r[%s] %s" % (time.strftime("%H:%M:%S"), message.ljust(line_width() - 11)))
        if self.args.log:
            self.args.log.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
            self.args.log.flush()

    def measure(self, counters: dict, t: float) -> tuple[float, float, int]:
        """Return (cpu %, io bytes/s, new processes) since the previous check."""
        if self.prev_t is None:
            return 0.0, 0.0, 0
        elapsed = max(t - self.prev_t, 1e-6)
        if self.state == self.ACTIVE:
            # Capped so that a forced sleep (lid closed) is not counted as awake time.
            self.awake_total += min(elapsed, 2 * self.args.interval)
        # Per-process deltas: processes that exited don't make the totals drop,
        # and a new process contributes everything it used since it started.
        d_cpu = d_io = 0.0
        for key, (cpu, io) in counters.items():
            old_cpu, old_io = self.prev_counters.get(key, (0.0, 0))
            d_cpu += max(cpu - old_cpu, 0)
            d_io += max(io - old_io, 0)
        new_procs = len(counters.keys() - self.prev_counters.keys())
        return d_cpu / elapsed * 100, d_io / elapsed, new_procs

    def tick(self) -> None:
        t = time.monotonic()
        roots, counters = sample_tree(self.args.match)
        cpu_pct, io_rate, new_procs = self.measure(counters, t)
        self.prev_counters, self.prev_t = counters, t
        self.peak_cpu = max(self.peak_cpu, cpu_pct)

        if roots and (cpu_pct >= self.args.cpu_threshold
                      or io_rate >= self.args.io_threshold * 1024
                      or new_procs):
            self.last_activity = t

        # A run lasts from the first activity until --idle-grace minutes without any.
        in_run = self.last_activity is not None and t - self.last_activity <= self.args.idle_grace * 60
        if in_run and self.run_start is None:
            self.run_start = t
            self.runs += 1
            self.event("Run #%d started (cpu %.1f%%, io %s, new procs %d)"
                       % (self.runs, cpu_pct, fmt_rate(io_rate), new_procs))
        elif not in_run and self.run_start is not None:
            length = self.last_activity - self.run_start
            self.longest_run = max(self.longest_run, length)
            self.event("Run #%d finished - duration %s" % (self.runs, fmt_duration(length)))
            self.run_start = None

        run_len = t - self.run_start if self.run_start is not None else 0.0
        if not roots:
            state = self.NO_CLAUDE
        elif self.run_start is None:
            state = self.IDLE
        elif run_len > self.args.max_run * 60:
            state = self.CAPPED
            if self.state != self.CAPPED:
                self.capped_runs += 1
                self.event("Run #%d exceeded %g min - releasing lock, computer may sleep"
                           % (self.runs, self.args.max_run))
        else:
            state = self.ACTIVE

        if (state == self.ACTIVE) != (self.state == self.ACTIVE):
            set_awake(state == self.ACTIVE, self.args.keep_display)
            self.event("Sleep lock ON  - computer will stay awake" if state == self.ACTIVE
                       else "Sleep lock OFF - normal sleep settings apply")
        self.state = state
        self.status(roots, len(counters), cpu_pct, io_rate, run_len, t)

    def status(self, roots: int, procs: int, cpu_pct: float, io_rate: float,
               run_len: float, t: float) -> None:
        if self.state == self.ACTIVE:
            extra = "run %s | cap in %s" % (fmt_duration(run_len),
                                          fmt_duration(self.args.max_run * 60 - run_len))
        elif self.state == self.CAPPED:
            extra = "run %s | capped" % fmt_duration(run_len)
        elif self.last_activity is not None:
            extra = "idle %s" % fmt_duration(t - self.last_activity)
        else:
            extra = "waiting for activity"
        line = "[%s] %-9s | claude %d, procs %3d | cpu %5.1f%% | io %10s | %s | awake %s | %s" % (
            time.strftime("%H:%M:%S"), self.state, roots, procs, cpu_pct, fmt_rate(io_rate),
            extra, fmt_duration(self.awake_total), power_source())
        if self.args.verbose:
            print(line)
        else:
            width = line_width()
            print("\r" + line[:width].ljust(width), end="", flush=True)

    def summary(self) -> None:
        if self.run_start is not None:
            self.longest_run = max(self.longest_run, time.monotonic() - self.run_start)
        rows = [
            ("Monitor uptime", fmt_duration(time.monotonic() - self.start)),
            ("Time kept awake", fmt_duration(self.awake_total)),
            ("Runs detected", str(self.runs)),
            ("Runs that hit the cap", str(self.capped_runs)),
            ("Longest run", fmt_duration(self.longest_run)),
            ("Peak CPU (tree, % of 1 core)", "%.1f%%" % self.peak_cpu),
        ]
        print("\n" + "=" * 60 + "\n Session summary\n" + "=" * 60)
        for name, value in rows:
            print(" %-30s %s" % (name, value))
        print("=" * 60)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Keep Windows awake while Claude Code in VS Code is working (max run length cap).")
    p.add_argument("--interval", type=float, default=5, help="seconds between checks (default 5)")
    p.add_argument("--max-run", type=float, default=120,
                   help="minutes; a run longer than this no longer blocks sleep (default 120)")
    p.add_argument("--idle-grace", type=float, default=5,
                   help="minutes without activity before a run counts as finished (default 5)")
    p.add_argument("--cpu-threshold", type=float, default=1.0,
                   help="CPU %% of one core for the tree to count as active (default 1.0)")
    p.add_argument("--io-threshold", type=float, default=2.0,
                   help="KB/s of I/O for the tree to count as active (default 2.0)")
    p.add_argument("--keep-display", action="store_true", help="also keep the screen on")
    p.add_argument("--match", action="append", default=None,
                   help="substring of claude.exe path to watch (repeatable). "
                        "Default: anthropic.claude-code (VS Code / Cursor extension)")
    p.add_argument("--log", metavar="FILE", type=argparse.FileType("a", encoding="utf-8"),
                   help="append events to a log file")
    p.add_argument("--verbose", action="store_true", help="print one line per check instead of a live line")
    args = p.parse_args(argv)
    args.match = [m.lower() for m in (args.match or ["anthropic.claude-code"])]
    if args.interval <= 0 or args.max_run <= 0 or args.idle_grace < 0:
        p.error("--interval and --max-run must be > 0, --idle-grace must be >= 0")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    monitor = Monitor(args)
    print("Claude Code keep-awake monitor  (Ctrl+C to stop)")
    print("  interval %ss | max run %g min | idle grace %s min | cpu >= %.1f%% | io >= %.1f KB/s | display %s"
          % (args.interval, args.max_run, args.idle_grace, args.cpu_threshold,
             args.io_threshold, "on" if args.keep_display else "may turn off"))
    print("  watching claude.exe paths containing: %s\n" % ", ".join(args.match))
    try:
        while True:
            monitor.tick()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        set_awake(False, False)
        monitor.summary()
        if args.log:
            args.log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
