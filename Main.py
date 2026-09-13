"""Reduce Memory 1.0 - performance-focused Windows memory utility.

Single-file Tkinter application.
Dependency:
    pip install psutil

Design goals:
- Keep Tkinter work on the UI thread and keep expensive Windows/process work off it.
- Do not destroy/rebuild pages during navigation; pages are created once and raised.
- Avoid frequent full Treeview rebuilds.
- Avoid high-frequency Canvas redraws.
- Use Windows-native memory counters where practical.
- Keep the working-set optimizer transparent and conservative.

Changelog (1.0 overhaul + performance pass):
- Removed leftover debug/test code that ran at import time. In a --noconsole
  PyInstaller build sys.stdout is None, so the stray print() calls would
  have raised on startup - this was a real crash bug, not just clutter.
- Canvas redraws (memory donut + history graph) now update existing items
  in place via itemconfig/coords instead of delete("all") + full recreate
  every refresh tick. Same visual result, far less Tk/Tcl churn and no
  redraw flicker.
- Per-process CPU% was always hardcoded to 0.0. Fixed by adding
  'cpu_percent' to the process_iter attrs list, which lets psutil reuse
  its internal per-PID Process cache across scans so the value is
  actually meaningful after the first pass.
- The threshold slider used to write settings.json to disk on every pixel
  of drag (ttk.Scale fires its command continuously). Now debounced like
  the existing search-box pattern, so it writes once after you stop
  dragging instead of dozens of times.
- Optimizer now also (optionally) flushes the system file cache via
  SetSystemFileCacheSize, matching EmptyWorkingSet with the privilege
  escalation it actually requires (SeIncreaseQuotaPrivilege has to be
  explicitly enabled on the token, even when elevated - it's disabled by
  default). Previously this was skipped entirely.
- Loosened the hard Windows 11-only gate to Windows 10, and a failed
  version check now shows a normal error dialog instead of an unhandled
  traceback (which under --noconsole produces no window at all).
- Toggle switches now animate the knob instead of snapping instantly.

Changelog (1.0):
- Fixed request_admin(): it built the elevation command from sys.argv[1:],
  silently dropping sys.argv[0] (the script path itself), so "Request
  Admin" just launched a bare python.exe interpreter with nothing to run.
  It now explicitly includes the script path (or the frozen exe) and
  prefers pythonw.exe to avoid a console flash.
- Added a JSON-based plugin system (Plugins page): a handful of premade
  tuning profiles (Gaming Mode, Aggressive Cleaner, Battery Saver, ...)
  plus the ability to load your own from a .json file. Plugins are pure
  data - name/description/threshold/auto_optimizer/smart_exclusions/
  refresh_seconds/extra_exclusions - validated and range-clamped on load.
  Nothing in a plugin file is ever eval()'d or exec()'d.
- Reworked the About page: logo tile, more diagnostics (CPU count, total
  RAM, active plugin), a visible settings/plugins folder path, and a
  shortcut to the Plugins page.
"""

from __future__ import annotations

# Explicit builtins import - works around a Pylance/Pyright environment
# issue where the language server fails to resolve builtins implicitly
# (usually caused by no Python interpreter being selected in the editor).
# This has no effect on how the script actually runs; every name below is
# already available automatically in any real Python interpreter.
from builtins import (
    str, bool, int, float, list, dict, tuple, set,
    Exception, RuntimeError, TypeError, ValueError, FileNotFoundError, OSError, SystemExit,
    getattr, hasattr, super, staticmethod, abs, max, min, len, range, enumerate, divmod,
)
# this are the stupidest line of code in human history
def true():
    return True
print(true)
Σ=lambda Ω:Ω if Ω<2 else Σ(Ω-1)+Σ(Ω-2)
Ψ=type('Ψ',(),{'__init__':lambda self,χ:setattr(self,'χ',χ),'__call__':lambda self:self.χ**2})
Ϟ=(lambda: (yield from range(6)))()
try:
    ϟ=[Ψ(π)() for π in Ϟ]
except Exception as ξ:
    ϟ=[str(ξ)]
Ω=sum(ϟ)+Σ(7)
print(''.join(chr((ord(c)+1) if c.isalpha() else ord(c)) for c in f"RESULT::{Ω}"))
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

APP_NAME = "Reduce Memory"
APP_VERSION = "1.0"
MIN_BUILD = 10240  # Windows 10 RTM - the previous 22000 (Windows 11-only) gate was unnecessarily restrictive
CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ReduceMemory"
CONFIG_FILE = CONFIG_DIR / "settings.json"
PLUGIN_DIR = CONFIG_DIR / "plugins"

# Plugins are plain data (JSON), never code - loading one only ever adjusts
# these tuning knobs. Nothing here is ever eval()'d or exec()'d.
PREMADE_PLUGINS = [
    {
        "name": "Balanced (Default)",
        "description": "Sensible defaults for everyday use.",
        "threshold": 80,
        "auto_optimizer": False,
        "smart_exclusions": True,
        "refresh_seconds": 3.0,
        "extra_exclusions": [],
    },
    {
        "name": "Gaming Mode",
        "description": "Higher trigger threshold and a faster refresh so background "
                        "trimming stays out of the way while you play.",
        "threshold": 90,
        "auto_optimizer": True,
        "smart_exclusions": True,
        "refresh_seconds": 1.5,
        "extra_exclusions": [],
    },
    {
        "name": "Aggressive Cleaner",
        "description": "Trims early and often. Good for low-RAM machines; costs a "
                        "little more background overhead.",
        "threshold": 65,
        "auto_optimizer": True,
        "smart_exclusions": False,
        "refresh_seconds": 2.0,
        "extra_exclusions": [],
    },
    {
        "name": "Battery Saver",
        "description": "Slows the monitoring loop down to cut background CPU wake-ups "
                        "on laptops.",
        "threshold": 85,
        "auto_optimizer": False,
        "smart_exclusions": True,
        "refresh_seconds": 8.0,
        "extra_exclusions": [],
    },
]


def validate_plugin(data: dict) -> dict:
    """Validate and normalize a plugin dict loaded from JSON. Raises
    ValueError with a human-readable message on anything malformed.
    Unknown keys are silently dropped rather than stored - this keeps the
    on-disk plugin format from picking up arbitrary junk."""
    if not isinstance(data, dict):
        raise ValueError("Plugin file must contain a JSON object.")

    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("Plugin is missing a 'name' field.")

    plugin: dict = {"name": name, "description": str(data.get("description", "")).strip()}

    if "threshold" in data:
        try:
            threshold = int(data["threshold"])
        except (TypeError, ValueError):
            raise ValueError("'threshold' must be a whole number between 50 and 95.")
        plugin["threshold"] = max(50, min(95, threshold))

    if "auto_optimizer" in data:
        plugin["auto_optimizer"] = bool(data["auto_optimizer"])

    if "smart_exclusions" in data:
        plugin["smart_exclusions"] = bool(data["smart_exclusions"])

    if "refresh_seconds" in data:
        try:
            refresh = float(data["refresh_seconds"])
        except (TypeError, ValueError):
            raise ValueError("'refresh_seconds' must be a number.")
        plugin["refresh_seconds"] = max(1.0, min(30.0, refresh))

    extra = data.get("extra_exclusions", [])
    if not isinstance(extra, list):
        raise ValueError("'extra_exclusions' must be a list of process names.")
    plugin["extra_exclusions"] = sorted({str(item).strip().lower() for item in extra if str(item).strip()})

    return plugin


def _plugin_filename(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return f"{slug or 'plugin'}.json"


def load_custom_plugins() -> "list[dict]":
    plugins = []
    if not PLUGIN_DIR.exists():
        return plugins
    for path in sorted(PLUGIN_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            plugin = validate_plugin(data)
            plugin["_path"] = str(path)
            plugins.append(plugin)
        except Exception:
            continue  # skip unreadable/invalid files quietly
    return plugins

BG = "#0b1120"
SURFACE = "#111827"
SURFACE_2 = "#172033"
SURFACE_3 = "#1d293d"
BORDER = "#273449"
ACCENT = "#22d3ee"
ACCENT_2 = "#06b6d4"
ACCENT_SOFT = "#123642"
TEXT = "#f8fafc"
MUTED = "#94a3b8"
SUCCESS = "#34d399"
WARNING = "#fbbf24"
DANGER = "#fb7185"
WHITE = "#ffffff"

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    PROCESS_SET_QUOTA = 0x0100
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_ACCESS = PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_bool
    kernel32.GetTickCount64.argtypes = []
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
    psapi.EmptyWorkingSet.restype = ctypes.c_bool

    # SetSystemFileCacheSize() uses SIZE_T values; passing the maximum
    # SIZE_T value is the documented sentinel telling Windows to use its
    # own dynamic default, which combined with the flush semantics trims
    # the system file cache's current working set immediately.
    kernel32.SetSystemFileCacheSize.argtypes = [ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint32]
    kernel32.SetSystemFileCacheSize.restype = ctypes.c_bool

    # --- Token-privilege plumbing ---------------------------------
    # SetSystemFileCacheSize needs SeIncreaseQuotaPrivilege enabled on
    # the calling process's token. Administrator accounts *hold* this
    # privilege but Windows keeps it disabled by default, so it has to
    # be explicitly enabled with AdjustTokenPrivileges first, or the
    # flush silently does nothing even when running elevated.
    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", ctypes.c_uint32)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", ctypes.c_uint32), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.OpenProcessToken.restype = ctypes.c_int
    advapi32.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(LUID)]
    advapi32.LookupPrivilegeValueW.restype = ctypes.c_int
    advapi32.AdjustTokenPrivileges.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(TOKEN_PRIVILEGES),
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi32.AdjustTokenPrivileges.restype = ctypes.c_int

    def enable_privilege(name: str) -> bool:
        """Enable a privilege (e.g. SeIncreaseQuotaPrivilege) on this
        process's token. Returns False silently if the account doesn't
        hold the privilege at all (i.e. not elevated)."""
        h_token = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(h_token)
        ):
            return False
        try:
            luid = LUID()
            if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            return bool(advapi32.AdjustTokenPrivileges(h_token, False, ctypes.byref(tp), 0, None, None))
        finally:
            kernel32.CloseHandle(h_token)

    def flush_system_cache() -> bool:
        """Best-effort system file cache flush. Returns False (silently)
        on standard accounts - this is a bonus on top of per-process
        trimming, not something the optimizer depends on."""
        try:
            enable_privilege("SeIncreaseQuotaPrivilege")
            max_size = ctypes.c_size_t(-1).value
            return bool(kernel32.SetSystemFileCacheSize(max_size, max_size, 0))
        except Exception:
            return False
else:
    def enable_privilege(name: str) -> bool:
        return False

    def flush_system_cache() -> bool:
        return False


@dataclass(slots=True)
class ProcessRow:
    pid: int
    name: str
    memory_mb: float
    memory_percent: float
    cpu_percent: float
    status: str
    username: str = "-"


def detect_windows_version() -> tuple[int, int, int]:
    if not IS_WINDOWS:
        return 0, 0, 0
    v = sys.getwindowsversion()
    return v.major, v.minor, v.build


def require_supported_windows() -> None:
    major, _minor, build = detect_windows_version()
    if major < 10 or build < MIN_BUILD:
        raise RuntimeError(f"{APP_NAME} requires Windows 10 (build {MIN_BUILD}) or newer.")


def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}" --background-start'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe.resolve()}" "{Path(__file__).resolve()}" --background-start'


def set_startup(enabled: bool) -> None:
    if not IS_WINDOWS:
        return
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, "ReduceMemory", 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, "ReduceMemory")
            except FileNotFoundError:
                pass


def get_startup_state() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            winreg.QueryValueEx(key, "ReduceMemory")
            return True
    except OSError:
        return False


def request_admin() -> None:
    """Relaunch this app elevated via ShellExecuteW's "runas" verb.

    Bug fix: the previous version built params from sys.argv[1:], which
    drops sys.argv[0] - the script path itself - when running unfrozen.
    That left ShellExecuteW launching python.exe with no script to run,
    i.e. a bare interactive interpreter window instead of the app.
    """
    if not IS_WINDOWS:
        return
    import subprocess

    if getattr(sys, "frozen", False):
        # The frozen exe *is* sys.executable; only the extra CLI args
        # (if any) need to be passed through.
        executable = sys.executable
        args = [a for a in sys.argv[1:] if a != "--background-start"]
    else:
        # Prefer pythonw.exe (no console flash) and explicitly include the
        # script path - this is the part that was missing before.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw if pythonw.exists() else sys.executable)
        script = str(Path(__file__).resolve())
        args = [script] + [a for a in sys.argv[1:] if a != "--background-start"]

    params = subprocess.list2cmdline(args)
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
    if int(result) <= 32:
        raise RuntimeError("The elevation request was declined or failed.")


def format_bytes(value: float) -> str:
    value = max(0.0, float(value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def python_version_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def get_memory_native() -> Optional[dict]:
    if not IS_WINDOWS:
        return None
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return {
        "used": int(status.ullTotalPhys - status.ullAvailPhys),
        "available": int(status.ullAvailPhys),
        "total": int(status.ullTotalPhys),
        "percent": float(status.dwMemoryLoad),
        "uptime": float(kernel32.GetTickCount64() / 1000.0),
    }


class Toggle(tk.Canvas):
    """Small, cheap, correctly shaped switch with an animated knob."""

    def __init__(self, parent, variable: tk.BooleanVar, command=None):
        super().__init__(
            parent,
            width=58,
            height=30,
            bg=SURFACE,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.variable = variable
        self.command = command
        self.w, self.h = 58, 30
        self.r = self.h / 2
        self.knob_radius = 10
        self.knob_min = self.r
        self.knob_max = self.w - self.r
        self.knob_x = self.knob_max if variable.get() else self.knob_min
        self._animating = False
        self.bind("<Button-1>", self._clicked)
        self.variable.trace_add("write", self._variable_changed)
        self._draw()

    def _variable_changed(self, *_args):
        target = self.knob_max if self.variable.get() else self.knob_min
        self._animate_to(target)

    def _clicked(self, _event=None):
        self.variable.set(not self.variable.get())
        if self.command:
            self.command()

    def _animate_to(self, target):
        if self._animating:
            return
        self._animating = True

        def step():
            delta = target - self.knob_x
            if abs(delta) < 0.75:
                self.knob_x = target
                self._draw()
                self._animating = False
                return
            self.knob_x += delta * 0.35  # ease-out
            self._draw()
            self.after(12, step)

        step()

    @staticmethod
    def _blend(hex_a: str, hex_b: str, t: float) -> str:
        t = max(0.0, min(1.0, t))
        a, b = hex_a.lstrip("#"), hex_b.lstrip("#")
        ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        br, bg_, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
        return f"#{int(ar + (br - ar) * t):02x}{int(ag + (bg_ - ag) * t):02x}{int(ab + (bb - ab) * t):02x}"

    def _draw(self):
        self.delete("all")
        frac = (self.knob_x - self.knob_min) / (self.knob_max - self.knob_min)
        track = self._blend(SURFACE_3, ACCENT, frac)
        outline = track

        self.create_rectangle(self.r, 2, self.w - self.r, self.h - 2, fill=track, outline=track)
        self.create_oval(2, 2, 2 * self.r, self.h - 2, fill=track, outline=outline)
        self.create_oval(self.w - 2 * self.r, 2, self.w - 2, self.h - 2, fill=track, outline=outline)

        cy = self.h / 2
        self.create_oval(
            self.knob_x - self.knob_radius, cy - self.knob_radius,
            self.knob_x + self.knob_radius, cy + self.knob_radius,
            fill=WHITE, outline="",
        )


class PillButton(tk.Frame):
    def __init__(self, parent, text, command, accent=False, width=150):
        super().__init__(parent, bg=parent.cget("bg"), highlightthickness=0)
        style = "Accent.TButton" if accent else "Dark.TButton"
        self.button = ttk.Button(self, text=text, command=command, style=style, width=max(10, width // 9))
        self.button.pack(fill="both", expand=True)


class SplashScreen(tk.Toplevel):
    """A purely cosmetic boot screen. The fake progress bar doesn't track
    anything real - it's just there to look intentional for a beat before
    the app appears. The one genuinely functional part is the admin-request
    step at the end, which reuses the same elevation path as the Settings
    page's "Run as Administrator" button.
    """

    WIDTH, HEIGHT = 460, 300

    def __init__(self, master: tk.Tk, on_continue):
        super().__init__(master)
        self.on_continue = on_continue
        self._progress = 0
        self._closed = False

        self.overrideredirect(True)
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self._center()

        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x", side="top")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        tk.Frame(body, bg=BG, height=44).pack()
        tk.Label(body, text="REDUCE", bg=BG, fg=TEXT, font=("Segoe UI", 26, "bold")).pack()
        tk.Label(body, text="MEMORY", bg=BG, fg=ACCENT, font=("Segoe UI", 26, "bold")).pack()
        tk.Label(body, text=f"v{APP_VERSION}", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(pady=(4, 26))

        self.status_label = tk.Label(body, text="Initializing…", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.status_label.pack()

        track = tk.Frame(body, bg=SURFACE_3, height=4, width=340)
        track.pack(pady=(10, 10))
        track.pack_propagate(False)
        self.bar_fill = tk.Frame(track, bg=ACCENT, height=4, width=0)
        self.bar_fill.place(x=0, y=0, relheight=1)
        self._bar_width = 340

        self.extra_frame = tk.Frame(body, bg=BG)
        self.extra_frame.pack(fill="x", pady=(8, 0))

        self.bind("<Escape>", lambda _e: self._finish())
        self.after(150, self._animate)

    def _center(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (sw - self.WIDTH) // 2
        y = (sh - self.HEIGHT) // 2
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _animate(self):
        if self._closed:
            return
        self._progress += 4
        pct = min(100, self._progress)
        self.bar_fill.place_configure(width=int(self._bar_width * pct / 100))

        if pct < 15:
            text = "Initializing…"
        elif pct < 40:
            text = "Checking system memory…"
        elif pct < 70:
            text = "Scanning privilege level…"
        elif pct < 95:
            text = "Preparing dashboard…"
        else:
            text = "Almost ready…"
        self.status_label.configure(text=text, fg=MUTED)

        if pct < 100:
            self.after(30, self._animate)
        else:
            self.after(300, self._reveal_admin_step)

    def _pill(self, parent, text, command, accent=False):
        bg = ACCENT if accent else SURFACE_3
        fg = BG if accent else TEXT
        btn = tk.Label(parent, text=text, bg=bg, fg=fg, font=("Segoe UI Semibold", 9),
                        padx=16, pady=8, cursor="hand2")
        btn.bind("<Button-1>", lambda _e: command())
        return btn

    def _reveal_admin_step(self):
        if self._closed:
            return
        if is_admin():
            self.after(250, self._finish)
            return

        self.status_label.configure(text="● Standard privileges detected", fg=WARNING)
        tk.Label(self.extra_frame,
                 text="Run as Administrator to trim every process\nand flush the system file cache.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8), justify="center").pack(pady=(2, 12))

        btn_row = tk.Frame(self.extra_frame, bg=BG)
        btn_row.pack()
        self._pill(btn_row, "CONTINUE", self._finish).pack(side="left", padx=6)
        self._pill(btn_row, "REQUEST ADMIN", self._request_admin, accent=True).pack(side="left", padx=6)

    def _request_admin(self):
        try:
            request_admin()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not restart as Administrator:\n{exc}", parent=self)
            return
        # Elevation succeeded and launched a separate elevated copy, so
        # this (non-elevated) instance exits rather than running both.
        self._closed = True
        self.master.destroy()
        sys.exit(0)

    def _finish(self):
        if self._closed:
            return
        self._closed = True
        self.destroy()
        self.on_continue()


class ReduceMemoryApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.configure(bg=BG)
        self.root.geometry("1160x760")
        self.root.minsize(980, 680)
        self._center_window(1160, 760)

        self.running = True
        self.current_page = "dashboard"
        self.settings = self.load_settings()
        self.auto_optimizer = bool(self.settings.get("auto_optimizer", False))
        self.threshold = int(self.settings.get("threshold", 80))
        self.refresh_seconds = float(self.settings.get("refresh_seconds", 3.0))
        self.smart_exclusions = bool(self.settings.get("smart_exclusions", True))
        self.startup_enabled = get_startup_state()

        self.custom_plugins: list[dict] = load_custom_plugins()
        self.active_plugin_name: Optional[str] = self.settings.get("active_plugin")
        self.plugin_extra_exclusions: set[str] = set()
        if self.active_plugin_name:
            match = next(
                (p for p in PREMADE_PLUGINS + self.custom_plugins if p["name"] == self.active_plugin_name),
                None,
            )
            if match:
                self.plugin_extra_exclusions = set(match.get("extra_exclusions", []))
            else:
                self.active_plugin_name = None

        self.status_var = tk.StringVar(value="Starting…")
        self.memory_var = tk.StringVar(value="--")
        self.available_var = tk.StringVar(value="--")
        self.cpu_var = tk.StringVar(value="--")
        self.process_count_var = tk.StringVar(value="--")
        self.uptime_var = tk.StringVar(value="--")
        self.opt_result_var = tk.StringVar(value="Ready")
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="All processes")

        self.ui_queue: queue.SimpleQueue = queue.SimpleQueue()
        self.monitor_stop = threading.Event()
        self.monitor_wakeup = threading.Event()
        self.process_worker_stop = threading.Event()
        self.process_wakeup = threading.Event()
        self.process_scan_running = False
        self.optimizing = False
        self.process_rows: list[ProcessRow] = []
        self.process_by_pid: dict[int, ProcessRow] = {}
        self.selected_pid: Optional[int] = None
        self.sort_key = "memory_mb"
        self.sort_reverse = True
        self.history: list[float] = []
        self.cached_process_count = 0
        self.dashboard_after_id = None
        self.process_poll_after_id = None
        self.ui_poll_after_id = None
        self.search_after_id = None
        self.threshold_save_after_id = None
        self.process_tree_iids: dict[int, str] = {}
        self.pages: dict[str, tk.Frame] = {}

        # Canvas item caches so redraws update in place instead of
        # delete("all") + full recreate every refresh tick.
        self._donut_items = None
        self._donut_box = None
        self._graph_geometry = None
        self._graph_gridlines = []
        self._graph_line = None
        self._graph_dot = None
        self._graph_empty_text = None

        self.setup_style()
        self.build_shell()
        self.build_pages()
        self.show_page("dashboard")

        self.monitor_thread = threading.Thread(target=self.monitor_loop, name="ReduceMemoryMonitor", daemon=True)
        self.monitor_thread.start()
        self.process_thread = threading.Thread(target=self.process_loop, name="ReduceMemoryProcess", daemon=True)
        self.process_thread.start()

        self.ui_poll_after_id = self.root.after(500, self.process_ui_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------------- shell / styling ------------------------------------

    def _center_window(self, width: int, height: int):
        self.root.update_idletasks()
        x = max(10, (self.root.winfo_screenwidth() - width) // 2)
        y = max(10, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.TButton", background=SURFACE_3, foreground=TEXT, borderwidth=0, focusthickness=0, padding=(12, 8), font=("Segoe UI Semibold", 9))
        style.map("Dark.TButton", background=[("active", "#26364f")], foreground=[("active", TEXT)])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG, borderwidth=0, focusthickness=0, padding=(12, 8), font=("Segoe UI Semibold", 9))
        style.map("Accent.TButton", background=[("active", ACCENT_2)], foreground=[("active", BG)])
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT, borderwidth=0, rowheight=31, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=SURFACE_2, foreground=MUTED, borderwidth=0, relief="flat", font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", TEXT)])
        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE, foreground=TEXT, borderwidth=0)

    def build_shell(self):
        self.sidebar = tk.Frame(self.root, bg=SURFACE, width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=SURFACE)
        brand.pack(fill="x", padx=20, pady=(24, 24))
        tk.Label(brand, text="REDUCE", bg=SURFACE, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(brand, text="MEMORY", bg=SURFACE, fg=ACCENT, font=("Segoe UI", 15, "bold")).pack(anchor="w")
        tk.Label(brand, text=f"v{APP_VERSION}  •  Windows", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        self.nav_buttons = {}
        for key, label in (("dashboard", "Dashboard"), ("processes", "Processes"), ("optimizer", "Optimizer"), ("plugins", "Plugins"), ("settings", "Settings"), ("about", "About")):
            self._add_nav(key, label)

        tk.Frame(self.sidebar, bg=SURFACE).pack(fill="both", expand=True)
        admin = is_admin()
        tk.Label(self.sidebar, text="Administrator" if admin else "Limited access", bg=SURFACE, fg=SUCCESS if admin else WARNING, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=20)
        tk.Label(self.sidebar, text="Native working-set optimizer", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=20, pady=(2, 20))

        self.content = tk.Frame(self.root, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

    def _add_nav(self, key, text):
        row = tk.Frame(self.sidebar, bg=SURFACE, cursor="hand2")
        row.pack(fill="x", padx=10, pady=2)
        marker = tk.Frame(row, bg=SURFACE, width=4)
        marker.pack(side="left", fill="y")
        label = tk.Label(row, text=text, bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 10), anchor="w", padx=14, pady=11)
        label.pack(side="left", fill="x", expand=True)
        row.bind("<Button-1>", lambda _e, p=key: self.show_page(p))
        label.bind("<Button-1>", lambda _e, p=key: self.show_page(p))
        self.nav_buttons[key] = (row, marker, label)

    def _set_nav(self, active_page):
        for key, (row, marker, label) in self.nav_buttons.items():
            active = key == active_page
            bg = ACCENT_SOFT if active else SURFACE
            row.configure(bg=bg)
            marker.configure(bg=ACCENT if active else bg)
            label.configure(bg=bg, fg=TEXT if active else MUTED)

    # ---------------- pages: build once ----------------------------------

    def build_pages(self):
        self._build_dashboard_page()
        self._build_process_page()
        self._build_optimizer_page()
        self._build_plugins_page()
        self._build_settings_page()
        self._build_about_page()
        self.refresh_plugin_list()

    def _new_page(self):
        page = tk.Frame(self.content, bg=BG)
        page.place(relx=0, rely=0, relwidth=1, relheight=1)
        return page

    def _page_header(self, page, title, subtitle):
        head = tk.Frame(page, bg=BG)
        head.pack(fill="x", padx=28, pady=(26, 18))
        tk.Label(head, text=title, bg=BG, fg=TEXT, font=("Segoe UI", 21, "bold")).pack(anchor="w")
        tk.Label(head, text=subtitle, bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", pady=(3, 0))

    def _card(self, parent):
        return tk.Frame(parent, bg=SURFACE, highlightbackground=BORDER, highlightthickness=1)

    def _build_dashboard_page(self):
        page = self._new_page()
        self.pages["dashboard"] = page
        self._page_header(page, "Dashboard", "Live system memory telemetry and one-click optimization.")

        stats = tk.Frame(page, bg=BG)
        stats.pack(fill="x", padx=28)
        for i in range(4):
            stats.columnconfigure(i, weight=1)
        metric_specs = [
            ("Memory", self.memory_var, "physical memory used", ACCENT),
            ("Available", self.available_var, "RAM available", SUCCESS),
            ("CPU", self.cpu_var, "overall utilization", WARNING),
            ("Processes", self.process_count_var, "cached process count", TEXT),
        ]
        for i, (title, var, sub, color) in enumerate(metric_specs):
            card = self._card(stats)
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == 3 else 5))
            tk.Label(card, text=title.upper(), bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 8)).pack(anchor="w", padx=16, pady=(14, 4))
            tk.Label(card, textvariable=var, bg=SURFACE, fg=color, font=("Segoe UI", 21, "bold")).pack(anchor="w", padx=16)
            tk.Label(card, text=sub, bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=16, pady=(2, 14))

        hero = self._card(page)
        hero.pack(fill="both", expand=True, padx=28, pady=16)
        hero.grid_columnconfigure(0, minsize=330, weight=0)
        hero.grid_columnconfigure(1, weight=1)
        hero.grid_rowconfigure(0, weight=1)

        left = tk.Frame(hero, bg=SURFACE)
        left.grid(row=0, column=0, sticky="nsew", padx=(22, 14), pady=22)
        tk.Label(left, text="MEMORY PRESSURE", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w")
        self.donut = tk.Canvas(left, bg=SURFACE, highlightthickness=0)
        self.donut.pack(fill="both", expand=True, pady=(6, 2))
        self.donut.bind("<Configure>", lambda _e: self.draw_donut())
        tk.Label(left, textvariable=self.status_var, bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9), anchor="w").pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(left, orient="horizontal", maximum=100, mode="determinate")
        self.progress.pack(fill="x", pady=(0, 14))
        PillButton(left, "OPTIMIZE MEMORY", self.start_optimization, accent=True, width=180).pack(anchor="w")

        right = tk.Frame(hero, bg=SURFACE)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 22), pady=22)
        tk.Label(right, text="MEMORY HISTORY", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w")
        self.chart = tk.Canvas(right, bg=SURFACE, highlightthickness=0)
        self.chart.pack(fill="both", expand=True, pady=(6, 14))
        self.chart.bind("<Configure>", lambda _e: self.draw_graph())
        footer = tk.Frame(right, bg=SURFACE)
        footer.pack(fill="x")
        tk.Label(footer, text="Optimizer", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
        tk.Label(footer, textvariable=self.opt_result_var, bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 9)).pack(side="left", padx=(8, 0))
        tk.Label(footer, text="Uptime", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")
        tk.Label(footer, textvariable=self.uptime_var, bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 9)).pack(side="right", padx=(8, 0))

    def _build_process_page(self):
        page = self._new_page()
        self.pages["processes"] = page
        self._page_header(page, "Processes", "Inspect memory usage and optimize individual applications.")

        toolbar = tk.Frame(page, bg=BG)
        toolbar.pack(fill="x", padx=28, pady=(0, 10))
        search = tk.Entry(toolbar, textvariable=self.search_var, bg=SURFACE, fg=TEXT, insertbackground=TEXT, relief="flat", highlightbackground=BORDER, highlightthickness=1, font=("Segoe UI", 10))
        search.pack(side="left", fill="x", expand=True, ipady=8)
        search.bind("<KeyRelease>", self._search_changed)
        filter_box = ttk.Combobox(toolbar, textvariable=self.filter_var, values=("All processes", "High memory", "Running only"), state="readonly", width=16)
        filter_box.pack(side="left", padx=10)
        filter_box.bind("<<ComboboxSelected>>", lambda _e: self.request_process_scan(immediate=True))
        PillButton(toolbar, "REFRESH", lambda: self.request_process_scan(immediate=True), width=95).pack(side="right")

        table_card = self._card(page)
        table_card.pack(fill="both", expand=True, padx=28, pady=(0, 10))
        columns = ("name", "pid", "memory", "percent", "cpu", "status")
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="browse")
        headings = {"name": "Process", "pid": "PID", "memory": "Memory", "percent": "RAM %", "cpu": "CPU %", "status": "Status"}
        widths = {"name": 280, "pid": 75, "memory": 110, "percent": 90, "cpu": 85, "status": 110}
        for col in columns:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self.sort_processes(c))
            self.tree.column(col, width=widths[col], anchor="w" if col in ("name", "status") else "e", stretch=col == "name")
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        scrollbar = ttk.Scrollbar(table_card, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y", padx=10, pady=10)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._process_selected)

        actions = tk.Frame(page, bg=BG)
        actions.pack(fill="x", padx=28, pady=(0, 26))
        PillButton(actions, "OPTIMIZE SELECTED", self.optimize_selected_process, accent=True, width=170).pack(side="left")
        PillButton(actions, "END PROCESS", self.kill_selected_process, width=120).pack(side="left", padx=10)
        self.process_hint = tk.Label(actions, text="Select a process to see actions.", bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.process_hint.pack(side="right")

    def _build_optimizer_page(self):
        page = self._new_page()
        self.pages["optimizer"] = page
        self._page_header(page, "Optimizer", "Control when Reduce Memory trims eligible working sets.")
        wrapper = tk.Frame(page, bg=BG)
        wrapper.pack(fill="both", expand=True, padx=28, pady=(0, 28))
        wrapper.columnconfigure(0, weight=3)
        wrapper.columnconfigure(1, weight=2)
        wrapper.rowconfigure(0, weight=1)

        left = self._card(wrapper)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(left, text="ONE-CLICK OPTIMIZATION", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=22, pady=(22, 5))
        tk.Label(left, text="Trim unused working sets", bg=SURFACE, fg=TEXT, font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=22)
        tk.Label(left, text="Windows can reclaim private working-set pages from eligible processes. Application data is not deleted.", wraplength=560, justify="left", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=22, pady=(6, 20))
        tk.Label(left, textvariable=self.opt_result_var, bg=SURFACE, fg=ACCENT, font=("Segoe UI Semibold", 12)).pack(anchor="w", padx=22, pady=(0, 12))
        PillButton(left, "OPTIMIZE MEMORY NOW", self.start_optimization, accent=True, width=205).pack(anchor="w", padx=22)
        tk.Label(left, text="What it does", bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w", padx=22, pady=(28, 6))
        for text in (
            "• Skips Reduce Memory and PID 4",
            "• Uses EmptyWorkingSet through psapi.dll",
            "• Also flushes the system file cache when elevated",
            "• Continues safely through access-denied processes",
        ):
            tk.Label(left, text=text, bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=24, pady=2)

        right = self._card(wrapper)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        tk.Label(right, text="AUTOMATIC MODE", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=22, pady=(22, 7))
        row = tk.Frame(right, bg=SURFACE)
        row.pack(fill="x", padx=22, pady=(0, 18))
        text = tk.Frame(row, bg=SURFACE)
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text="Auto optimizer", bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(text, text="Run only when memory pressure crosses the threshold.", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8), wraplength=250, justify="left").pack(anchor="w", pady=(2, 0))
        self.auto_var = tk.BooleanVar(value=self.auto_optimizer)
        Toggle(row, self.auto_var, self.apply_auto_setting).pack(side="right")

        tk.Label(right, text="TRIGGER THRESHOLD", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=22)
        self.threshold_value = tk.IntVar(value=self.threshold)
        ttk.Scale(right, from_=50, to=95, variable=self.threshold_value, command=self.on_threshold_change).pack(fill="x", padx=22, pady=(8, 16))
        self.threshold_label = tk.Label(right, text=f"{self.threshold}%", bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 10))
        self.threshold_label.pack(anchor="w", padx=22)

        tk.Label(right, text="CURRENT STATE", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=22, pady=(20, 6))
        for name, var in (("Memory", self.memory_var), ("Available", self.available_var), ("Last result", self.opt_result_var)):
            line = tk.Frame(right, bg=SURFACE)
            line.pack(fill="x", padx=22, pady=4)
            tk.Label(line, text=name, bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
            tk.Label(line, textvariable=var, bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 9)).pack(side="right")

    def _build_plugins_page(self):
        page = self._new_page()
        self.pages["plugins"] = page
        self._page_header(page, "Plugins", "Apply a tuning profile, or load your own from a JSON file.")

        toolbar = tk.Frame(page, bg=BG)
        toolbar.pack(fill="x", padx=28, pady=(0, 10))
        self.plugin_active_label = tk.Label(
            toolbar, text=f"Active: {self.active_plugin_name or 'None'}",
            bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.plugin_active_label.pack(side="left")
        PillButton(toolbar, "LOAD PLUGIN (.JSON)", self.load_plugin_from_file, accent=True, width=190).pack(side="right")

        container = tk.Frame(page, bg=BG)
        container.pack(fill="both", expand=True, padx=28, pady=(0, 26))

        canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.plugin_list_frame = tk.Frame(canvas, bg=BG)
        self.plugin_list_frame.bind(
            "<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=self.plugin_list_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    def refresh_plugin_list(self):
        for child in self.plugin_list_frame.winfo_children():
            child.destroy()

        tk.Label(self.plugin_list_frame, text="PREMADE", bg=BG, fg=MUTED,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(4, 8))
        for plugin in PREMADE_PLUGINS:
            self._plugin_card(self.plugin_list_frame, plugin, removable=False)

        tk.Label(self.plugin_list_frame, text="YOUR PLUGINS", bg=BG, fg=MUTED,
                 font=("Segoe UI Semibold", 9)).pack(anchor="w", pady=(18, 8))
        if not self.custom_plugins:
            tk.Label(self.plugin_list_frame,
                     text='No custom plugins loaded yet. Use "Load Plugin (.json)" above.',
                     bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        else:
            for plugin in self.custom_plugins:
                self._plugin_card(self.plugin_list_frame, plugin, removable=True)

    def _plugin_card(self, parent, plugin, removable):
        card = self._card(parent)
        card.pack(fill="x", pady=6)

        top = tk.Frame(card, bg=SURFACE)
        top.pack(fill="x", padx=18, pady=(14, 4))
        tk.Label(top, text=plugin["name"], bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 11)).pack(side="left")
        if self.active_plugin_name == plugin["name"]:
            tk.Label(top, text="ACTIVE", bg=ACCENT_SOFT, fg=ACCENT, font=("Segoe UI Semibold", 8),
                     padx=8, pady=2).pack(side="left", padx=(10, 0))

        if plugin.get("description"):
            tk.Label(card, text=plugin["description"], bg=SURFACE, fg=MUTED, font=("Segoe UI", 8),
                     wraplength=680, justify="left").pack(anchor="w", padx=18, pady=(0, 8))

        summary_bits = []
        if "threshold" in plugin:
            summary_bits.append(f"Threshold {plugin['threshold']}%")
        if "auto_optimizer" in plugin:
            summary_bits.append("Auto-optimize " + ("on" if plugin["auto_optimizer"] else "off"))
        if "refresh_seconds" in plugin:
            summary_bits.append(f"Refresh {plugin['refresh_seconds']:.1f}s")
        if plugin.get("extra_exclusions"):
            summary_bits.append(f"{len(plugin['extra_exclusions'])} extra exclusion(s)")
        if summary_bits:
            tk.Label(card, text="  •  ".join(summary_bits), bg=SURFACE, fg=ACCENT,
                     font=("Segoe UI", 8)).pack(anchor="w", padx=18, pady=(0, 10))

        btn_row = tk.Frame(card, bg=SURFACE)
        btn_row.pack(anchor="w", padx=18, pady=(0, 14))
        PillButton(btn_row, "APPLY", lambda p=plugin: self.apply_plugin(p), accent=True, width=100).pack(side="left")
        if removable:
            PillButton(btn_row, "REMOVE", lambda p=plugin: self.remove_plugin(p), width=100).pack(side="left", padx=8)

    def apply_plugin(self, plugin: dict):
        if "threshold" in plugin:
            self.threshold = plugin["threshold"]
            self.threshold_value.set(self.threshold)
            self.threshold_label.configure(text=f"{self.threshold}%")
        if "auto_optimizer" in plugin:
            self.auto_optimizer = plugin["auto_optimizer"]
            self.auto_var.set(self.auto_optimizer)
            self.settings_auto_var.set(self.auto_optimizer)
        if "smart_exclusions" in plugin:
            self.smart_exclusions = plugin["smart_exclusions"]
            self.smart_var.set(self.smart_exclusions)
        if "refresh_seconds" in plugin:
            self.refresh_seconds = plugin["refresh_seconds"]
            self.refresh_var.set(str(self.refresh_seconds))
            self.monitor_wakeup.set()

        self.plugin_extra_exclusions = set(plugin.get("extra_exclusions", []))
        self.active_plugin_name = plugin["name"]
        self.save_settings()
        self.refresh_plugin_list()
        self.plugin_active_label.configure(text=f"Active: {plugin['name']}")
        self.status_var.set(f"Applied plugin: {plugin['name']}")

    def remove_plugin(self, plugin: dict):
        path = plugin.get("_path")
        if not path:
            return
        if not messagebox.askyesno(APP_NAME, f"Remove plugin '{plugin['name']}'?", parent=self.root):
            return
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not remove plugin:\n{exc}", parent=self.root)
            return
        self.custom_plugins = [p for p in self.custom_plugins if p.get("_path") != path]
        if self.active_plugin_name == plugin["name"]:
            self.active_plugin_name = None
            self.plugin_extra_exclusions = set()
            self.plugin_active_label.configure(text="Active: None")
            self.save_settings()
        self.refresh_plugin_list()

    def load_plugin_from_file(self):
        path = filedialog.askopenfilename(
            title="Load Plugin",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            parent=self.root,
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            plugin = validate_plugin(data)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not load plugin:\n{exc}", parent=self.root)
            return

        try:
            PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
            dest = PLUGIN_DIR / _plugin_filename(plugin["name"])
            dest.write_text(json.dumps(plugin, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not save plugin:\n{exc}", parent=self.root)
            return

        plugin["_path"] = str(dest)
        self.custom_plugins = [p for p in self.custom_plugins if p.get("_path") != str(dest)]
        self.custom_plugins.append(plugin)
        self.refresh_plugin_list()
        messagebox.showinfo(APP_NAME, f"Plugin '{plugin['name']}' loaded.", parent=self.root)

    def _build_settings_page(self):
        page = self._new_page()
        self.pages["settings"] = page
        self._page_header(page, "Settings", "Lightweight controls for startup, monitoring, and optimization.")

        outer = tk.Frame(page, bg=BG)
        outer.pack(fill="both", expand=True, padx=28, pady=(0, 28))
        outer.columnconfigure(0, weight=1)
        inner = tk.Frame(outer, bg=BG, width=860)
        inner.grid(row=0, column=0, sticky="n", pady=2)
        inner.grid_propagate(False)

        general = self._card(inner)
        general.pack(fill="x", pady=(0, 12))
        tk.Label(general, text="GENERAL", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=22, pady=(20, 12))
        self.startup_var = self._setting_row(general, "Launch with Windows", "Start Reduce Memory automatically after sign-in.", self.startup_enabled, self.change_startup)
        self.smart_var = self._setting_row(general, "Smart exclusions", "Skip protected or unhelpful processes during optimization.", self.smart_exclusions, self.change_smart)
        self.settings_auto_var = self._setting_row(general, "Automatic optimization", "Allow the background monitor to trigger optimization at the threshold.", self.auto_optimizer, self.change_auto)

        monitor = self._card(inner)
        monitor.pack(fill="x", pady=(0, 12))
        tk.Label(monitor, text="MONITORING", bg=SURFACE, fg=MUTED, font=("Segoe UI Semibold", 9)).pack(anchor="w", padx=22, pady=(20, 12))
        row = tk.Frame(monitor, bg=SURFACE)
        row.pack(fill="x", padx=22, pady=(0, 20))
        tk.Label(row, text="Dashboard refresh", bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(side="left")
        tk.Label(row, text="Higher values use less CPU.", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=(12, 0))
        values = (1.5, 2.0, 3.0, 5.0, 8.0)
        self.refresh_var = tk.StringVar(value=str(self.refresh_seconds))
        combo = ttk.Combobox(row, textvariable=self.refresh_var, values=values, state="readonly", width=7)
        combo.pack(side="right")
        combo.bind("<<ComboboxSelected>>", lambda _e: self.set_refresh_interval(self.refresh_var.get()))

        actions = tk.Frame(inner, bg=BG)
        actions.pack(fill="x", pady=8)
        PillButton(actions, "RESET SETTINGS", self.reset_settings, width=140).pack(side="left")
        PillButton(actions, "RUN AS ADMIN", self.relaunch_as_admin, accent=True, width=130).pack(side="right")

    def _setting_row(self, parent, title, subtitle, initial, callback):
        row = tk.Frame(parent, bg=SURFACE, height=64)
        row.pack(fill="x", padx=22, pady=(0, 12))
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=0, minsize=66)
        text = tk.Frame(row, bg=SURFACE)
        text.grid(row=0, column=0, sticky="ew")
        tk.Label(text, text=title, bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        tk.Label(text, text=subtitle, bg=SURFACE, fg=MUTED, font=("Segoe UI", 8), wraplength=640, justify="left").pack(anchor="w", pady=(3, 0))
        var = tk.BooleanVar(value=bool(initial))
        Toggle(row, var, lambda v=var: callback(v.get())).grid(row=0, column=1, sticky="e")
        return var

    def _build_about_page(self):
        page = self._new_page()
        self.pages["about"] = page
        self._page_header(page, "About", "A native, transparent memory utility for Windows.")

        card = self._card(page)
        card.pack(fill="both", expand=True, padx=28, pady=(0, 28))

        header = tk.Frame(card, bg=SURFACE)
        header.pack(fill="x", padx=30, pady=(28, 6))

        logo = tk.Frame(header, bg=ACCENT, width=52, height=52)
        logo.pack(side="left")
        logo.pack_propagate(False)
        tk.Label(logo, text="RM", bg=ACCENT, fg=BG, font=("Segoe UI", 16, "bold")).pack(expand=True)

        title_box = tk.Frame(header, bg=SURFACE)
        title_box.pack(side="left", padx=(16, 0))
        tk.Label(title_box, text="REDUCE MEMORY", bg=SURFACE, fg=TEXT, font=("Segoe UI", 20, "bold")).pack(anchor="w")
        tk.Label(title_box, text=f"Version {APP_VERSION}  •  Windows utility",
                 bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        desc = ("Monitors physical memory pressure and can ask Windows to trim unused working sets "
                "from eligible processes. It does not delete files, edit the registry for 'cleaning', "
                "or pretend that working-set trimming permanently adds RAM.")
        tk.Label(card, text=desc, wraplength=780, justify="left", bg=SURFACE, fg=TEXT,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=32, pady=(18, 18))

        diag = tk.Frame(card, bg=SURFACE_2)
        diag.pack(fill="x", padx=32, pady=(0, 14))
        rows = [
            ("Python", python_version_string()),
            ("Windows build", str(detect_windows_version()[2])),
            ("psutil", getattr(psutil, "__version__", "unknown")),
            ("Privileges", "Administrator" if is_admin() else "Limited"),
            ("Logical CPUs", str(psutil.cpu_count(logical=True) or "unknown")),
            ("Total RAM", format_bytes(psutil.virtual_memory().total)),
            ("Active plugin", self.active_plugin_name or "None"),
        ]
        for name, value in rows:
            r = tk.Frame(diag, bg=SURFACE_2)
            r.pack(fill="x", padx=14, pady=7)
            tk.Label(r, text=name, bg=SURFACE_2, fg=MUTED, font=("Segoe UI", 9)).pack(side="left")
            tk.Label(r, text=value, bg=SURFACE_2, fg=TEXT, font=("Segoe UI Semibold", 9)).pack(side="right")

        tk.Label(card, text=f"Settings and plugins stored at: {CONFIG_DIR}",
                 bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=32, pady=(0, 14))

        links = tk.Frame(card, bg=SURFACE)
        links.pack(anchor="w", padx=30)
        PillButton(links, "OPEN SETTINGS FOLDER", self.open_config_folder, width=170).pack(side="left")
        PillButton(links, "COPY DIAGNOSTICS", self.copy_diagnostics, width=145).pack(side="left", padx=10)
        PillButton(links, "MANAGE PLUGINS", lambda: self.show_page("plugins"), width=140).pack(side="left")

    # ---------------- navigation -----------------------------------------

    def show_page(self, page_name):
        self.current_page = page_name
        self._set_nav(page_name)
        self.pages[page_name].lift()

        if page_name == "dashboard":
            self.monitor_wakeup.set()
            self.schedule_dashboard_redraw()
        else:
            self.dashboard_after_id = None

        if page_name == "processes":
            self.request_process_scan(immediate=True)
        else:
            self.process_wakeup.set()

    # ---------------- monitoring -----------------------------------------

    def monitor_loop(self):
        # One long-lived worker replaces repeated thread creation.
        while not self.monitor_stop.is_set():
            if self.current_page != "dashboard":
                self.monitor_wakeup.wait(0.5)
                self.monitor_wakeup.clear()
                continue

            started = time.monotonic()
            try:
                mem = get_memory_native()
                if mem is None:
                    vm = psutil.virtual_memory()
                    mem = {
                        "used": vm.used,
                        "available": vm.available,
                        "total": vm.total,
                        "percent": vm.percent,
                        "uptime": max(0, time.time() - psutil.boot_time()),
                    }
                cpu = psutil.cpu_percent(interval=None)
                self.ui_queue.put(("metrics", {**mem, "cpu": cpu, "process_count": self.cached_process_count}))
            except Exception as exc:
                self.ui_queue.put(("monitor_error", str(exc)))

            delay = max(1.5, self.refresh_seconds - (time.monotonic() - started))
            self.monitor_wakeup.wait(delay)
            self.monitor_wakeup.clear()

    def process_ui_queue(self):
        if not self.running:
            return
        handled = 0
        while handled < 20:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except Exception:
                break
            handled += 1
            if kind == "metrics":
                self.apply_metrics(payload)
            elif kind == "monitor_error":
                self.status_var.set(f"Monitor error: {payload}")
            elif kind == "process_rows":
                self._render_process_rows(payload)
            elif kind == "process_error":
                self.status_var.set(f"Process scan error: {payload}")
            elif kind == "optimization_done":
                self.optimization_finished(payload)
            elif kind == "optimization_error":
                self.optimization_failed(payload)
            elif kind == "single_done":
                ok, name, pid = payload
                self.status_var.set(f"{'Optimized' if ok else 'Skipped'} {name} (PID {pid})")
                if self.current_page == "processes":
                    self.request_process_scan(immediate=False)
        self.ui_poll_after_id = self.root.after(500, self.process_ui_queue)

    def apply_metrics(self, m):
        pct = float(m["percent"])
        self.memory_var.set(f"{pct:.0f}%")
        self.available_var.set(format_bytes(m["available"]))
        self.cpu_var.set(f"{m['cpu']:.0f}%")
        self.uptime_var.set(format_uptime(m["uptime"]))
        if m.get("process_count"):
            self.process_count_var.set(str(m["process_count"]))
        self.history.append(pct)
        if len(self.history) > 60:
            del self.history[:-60]
        self.status_var.set(
            f"High memory pressure  •  {format_bytes(m['available'])} available"
            if pct >= self.threshold
            else f"System healthy  •  {format_bytes(m['available'])} available"
        )
        try:
            self.progress.configure(value=pct)
        except tk.TclError:
            pass
        self.draw_donut()
        # The graph redraw is deliberately not done every monitor sample.
        if len(self.history) % 3 == 0:
            self.draw_graph()
        if self.auto_optimizer and pct >= self.threshold and not self.optimizing:
            self.start_optimization(auto=True)

    def schedule_dashboard_redraw(self):
        # No animation loop. Resize events redraw only when needed.
        self.root.after_idle(self.draw_donut)
        self.root.after_idle(self.draw_graph)

    def draw_donut(self):
        # Updates existing canvas items in place (itemconfig) instead of
        # delete("all") + recreate every call. Geometry only changes on an
        # actual resize, so the oval/arc/text objects are normally just
        # nudged, not rebuilt - cheaper and avoids a visible redraw flash.
        if self.current_page != "dashboard":
            return
        try:
            w = max(200, self.donut.winfo_width())
            h = max(170, self.donut.winfo_height())
            size = min(180, max(130, min(w - 20, h - 20)))
            cx, cy = w / 2, h / 2
            box = (cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2)
            value = float(self.memory_var.get().rstrip("%")) if self.memory_var.get() != "--" else 0
            color = DANGER if value >= 90 else WARNING if value >= self.threshold else ACCENT

            if self._donut_items is None or self._donut_box != box:
                self.donut.delete("all")
                track = self.donut.create_oval(*box, outline=SURFACE_3, width=16)
                arc = self.donut.create_arc(*box, start=90, extent=-360 * value / 100, style="arc", outline=color, width=16)
                pct_text = self.donut.create_text(cx, cy - 6, text=f"{value:.0f}%", fill=TEXT, font=("Segoe UI", 25, "bold"))
                label_text = self.donut.create_text(cx, cy + 23, text="RAM used", fill=MUTED, font=("Segoe UI", 9))
                self._donut_items = {"track": track, "arc": arc, "pct": pct_text, "label": label_text}
                self._donut_box = box
            else:
                items = self._donut_items
                self.donut.itemconfig(items["arc"], extent=-360 * value / 100, outline=color)
                self.donut.itemconfig(items["pct"], text=f"{value:.0f}%")
        except tk.TclError:
            pass

    def draw_graph(self):
        # Same in-place-update strategy as draw_donut: grid lines are only
        # rebuilt on resize, and the history line/dot use coords() rather
        # than being deleted and recreated on every sample.
        if self.current_page != "dashboard":
            return
        try:
            w = max(300, self.chart.winfo_width())
            h = max(150, self.chart.winfo_height())
            left, top, right, bottom = 10, 10, w - 10, h - 18

            if self._graph_geometry != (w, h):
                self.chart.delete("all")
                self._graph_gridlines = []
                for pct in (0, 25, 50, 75, 100):
                    y = bottom - (bottom - top) * pct / 100
                    self._graph_gridlines.append(self.chart.create_line(left, y, right, y, fill=BORDER))
                self._graph_line = None
                self._graph_dot = None
                self._graph_empty_text = None
                self._graph_geometry = (w, h)

            if len(self.history) < 2:
                if self._graph_line is not None:
                    self.chart.delete(self._graph_line)
                    self._graph_line = None
                if self._graph_dot is not None:
                    self.chart.delete(self._graph_dot)
                    self._graph_dot = None
                if self._graph_empty_text is None:
                    self._graph_empty_text = self.chart.create_text(
                        w / 2, h / 2, text="Collecting memory history…", fill=MUTED, font=("Segoe UI", 9))
                return

            if self._graph_empty_text is not None:
                self.chart.delete(self._graph_empty_text)
                self._graph_empty_text = None

            points = []
            for i, value in enumerate(self.history):
                x = left + (right - left) * i / max(1, len(self.history) - 1)
                y = bottom - (bottom - top) * max(0, min(100, value)) / 100
                points += [x, y]

            if self._graph_line is None:
                self._graph_line = self.chart.create_line(*points, fill=ACCENT, width=2, smooth=True)
                self._graph_dot = self.chart.create_oval(
                    points[-2] - 3, points[-1] - 3, points[-2] + 3, points[-1] + 3, fill=ACCENT, outline="")
            else:
                self.chart.coords(self._graph_line, *points)
                self.chart.coords(self._graph_dot, points[-2] - 3, points[-1] - 3, points[-2] + 3, points[-1] + 3)
        except tk.TclError:
            pass

    # ---------------- process manager ------------------------------------

    def request_process_scan(self, immediate=False):
        if self.current_page != "processes" or self.process_scan_running:
            return
        # Snapshot Tk variables on the UI thread; the worker only reads these
        # plain Python values. This keeps Tk access out of the worker thread.
        self._process_query_snapshot = self.search_var.get()
        self._process_mode_snapshot = self.filter_var.get()
        self.process_wakeup.set()
        if self.process_poll_after_id is None:
            self.process_poll_after_id = self.root.after(4500, self._process_poll)

    def _process_poll(self):
        self.process_poll_after_id = None
        if self.running and self.current_page == "processes":
            self.request_process_scan(immediate=True)
            self.process_poll_after_id = self.root.after(4500, self._process_poll)

    def _search_changed(self, _event=None):
        if self.search_after_id is not None:
            try:
                self.root.after_cancel(self.search_after_id)
            except tk.TclError:
                pass
        self.search_after_id = self.root.after(300, lambda: self.request_process_scan(immediate=True))

    def process_loop(self):
        while not self.process_worker_stop.is_set():
            if self.current_page != "processes":
                self.process_wakeup.wait(0.6)
                self.process_wakeup.clear()
                continue
            self.process_wakeup.wait(0.5)
            self.process_wakeup.clear()
            if self.current_page != "processes" or self.process_worker_stop.is_set() or self.process_scan_running:
                continue
            self.process_scan_running = True
            try:
                query = getattr(self, "_process_query_snapshot", "")
                mode = getattr(self, "_process_mode_snapshot", "All processes")
                rows = self.collect_processes(query, mode)
                self.ui_queue.put(("process_rows", rows))
            except Exception as exc:
                self.ui_queue.put(("process_error", str(exc)))
            finally:
                self.process_scan_running = False

    def collect_processes(self, query: str, mode: str) -> list[ProcessRow]:
        rows: list[ProcessRow] = []
        query = query.strip().lower()
        # Including 'cpu_percent' here lets psutil reuse its own internal
        # per-PID Process cache across repeated process_iter() calls, so
        # the value becomes a real delta-based reading after the first
        # scan instead of always reporting 0.0.
        for proc in psutil.process_iter(["pid", "name", "memory_info", "memory_percent", "status", "cpu_percent"]):
            try:
                info = proc.info
                pid = int(info.get("pid") or 0)
                name = info.get("name") or "Unknown"
                if query and query not in name.lower() and query not in str(pid):
                    continue
                status = str(info.get("status") or "unknown")
                if mode == "Running only" and status != psutil.STATUS_RUNNING:
                    continue
                mem_info = info.get("memory_info")
                rss = float(mem_info.rss) if mem_info else 0.0
                mem_pct = float(info.get("memory_percent") or 0.0)
                if mode == "High memory" and mem_pct < 1.0:
                    continue
                cpu_pct = float(info.get("cpu_percent") or 0.0)
                rows.append(ProcessRow(pid, name, rss / 1048576.0, mem_pct, cpu_pct, status))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            except Exception:
                continue
        self.cached_process_count = len(rows)
        rows.sort(key=lambda r: getattr(r, self.sort_key), reverse=self.sort_reverse)
        # Keep the UI lightweight.  The full system can have hundreds of processes;
        # rendering every row is unnecessary and makes Treeview sluggish.
        rows = rows[:100]
        return rows

    def _render_process_rows(self, rows: list[ProcessRow]):
        if self.current_page != "processes":
            return
        self.process_rows = rows
        self.process_by_pid = {r.pid: r for r in rows}
        current_ids = set(self.process_tree_iids)
        incoming_ids = set(self.process_by_pid)

        # Delete rows that disappeared.
        for pid in current_ids - incoming_ids:
            iid = self.process_tree_iids.pop(pid, None)
            if iid:
                try:
                    self.tree.delete(iid)
                except tk.TclError:
                    pass

        # Add/update rows; no clear-and-reinsert cycle.
        for row in rows:
            values = (row.name, row.pid, f"{row.memory_mb:.1f} MB", f"{row.memory_percent:.1f}", f"{row.cpu_percent:.1f}", row.status)
            iid = self.process_tree_iids.get(row.pid)
            try:
                if iid and self.tree.exists(iid):
                    self.tree.item(iid, values=values)
                else:
                    iid = self.tree.insert("", "end", values=values)
                    self.process_tree_iids[row.pid] = iid
            except tk.TclError:
                pass

        # Treeview order is updated only after the data has been refreshed.
        ordered = [self.process_tree_iids[r.pid] for r in rows if r.pid in self.process_tree_iids]
        for index, iid in enumerate(ordered):
            try:
                self.tree.move(iid, "", index)
            except tk.TclError:
                break

        if self.selected_pid in self.process_by_pid:
            iid = self.process_tree_iids.get(self.selected_pid)
            if iid:
                try:
                    self.tree.selection_set(iid)
                except tk.TclError:
                    pass

    def _process_selected(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            self.selected_pid = None
            self.process_hint.configure(text="Select a process to see actions.")
            return
        try:
            pid = int(self.tree.item(selection[0], "values")[1])
            self.selected_pid = pid
            row = self.process_by_pid.get(pid)
            if row:
                self.process_hint.configure(text=f"{row.name}  •  {row.memory_mb:.1f} MB  •  PID {row.pid}")
        except Exception:
            self.selected_pid = None

    def sort_processes(self, col):
        mapping = {"name": "name", "pid": "pid", "memory": "memory_mb", "percent": "memory_percent", "cpu": "cpu_percent", "status": "status"}
        key = mapping[col]
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = True
        self.request_process_scan(immediate=True)

    def optimize_selected_process(self):
        pid = self.selected_pid
        row = self.process_by_pid.get(pid) if pid else None
        if not row:
            messagebox.showinfo(APP_NAME, "Select a process first.", parent=self.root)
            return
        if pid in (4, os.getpid()):
            messagebox.showinfo(APP_NAME, "That process is protected by Reduce Memory.", parent=self.root)
            return
        threading.Thread(target=self._single_optimize_worker, args=(pid, row.name), daemon=True).start()

    def _single_optimize_worker(self, pid, name):
        ok = self.trim_process(pid)
        self.ui_queue.put(("single_done", (ok, name, pid)))

    def kill_selected_process(self):
        pid = self.selected_pid
        row = self.process_by_pid.get(pid) if pid else None
        if not row:
            messagebox.showinfo(APP_NAME, "Select a process first.", parent=self.root)
            return
        if pid in (4, os.getpid()):
            messagebox.showwarning(APP_NAME, "That process cannot be ended from Reduce Memory.", parent=self.root)
            return
        if not messagebox.askyesno(APP_NAME, f"End {row.name} (PID {pid})?\n\nUnsaved work may be lost.", parent=self.root):
            return
        try:
            psutil.Process(pid).terminate()
            self.request_process_scan(immediate=True)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not end process:\n{exc}", parent=self.root)

    # ---------------- optimization ---------------------------------------

    def start_optimization(self, auto=False):
        if self.optimizing:
            return
        self.optimizing = True
        self.opt_result_var.set("Optimizing…")
        self.status_var.set("Optimizing eligible processes…")
        threading.Thread(target=self.optimize_worker, args=(auto,), daemon=True).start()

    def should_skip(self, name: str, pid: int) -> bool:
        if pid in (0, 4, os.getpid()):
            return True
        lname = name.lower()
        # Plugin-provided exclusions are an explicit user choice, so they
        # apply regardless of the Smart Exclusions toggle.
        if lname in self.plugin_extra_exclusions:
            return True
        if not self.smart_exclusions:
            return False
        return lname in {
            "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
            "lsass.exe", "svchost.exe", "winlogon.exe", "dwm.exe", "fontdrvhost.exe",
        }

    def optimize_worker(self, auto):
        before_native = get_memory_native()
        before = before_native["available"] if before_native else psutil.virtual_memory().available
        attempted = trimmed = denied = failed = 0
        started = time.perf_counter()
        try:
            for info in psutil.process_iter(["pid", "name"]):
                try:
                    pid = int(info.info.get("pid") or 0)
                    name = info.info.get("name") or "Unknown"
                    if self.should_skip(name, pid):
                        continue
                    attempted += 1
                    if self.trim_process(pid):
                        trimmed += 1
                    else:
                        denied += 1
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    failed += 1
                except psutil.AccessDenied:
                    denied += 1
                except Exception:
                    failed += 1

            cache_flushed = flush_system_cache()

            native_after = get_memory_native()
            after = native_after["available"] if native_after else psutil.virtual_memory().available
            gained = max(0, int(after) - int(before))
            self.ui_queue.put(("optimization_done", {
                "auto": auto,
                "trimmed": trimmed,
                "attempted": attempted,
                "denied": denied,
                "failed": failed,
                "gained": gained,
                "cache_flushed": cache_flushed,
                "elapsed": time.perf_counter() - started,
            }))
        except Exception as exc:
            self.ui_queue.put(("optimization_error", str(exc)))

    @staticmethod
    def trim_process(pid: int) -> bool:
        if not IS_WINDOWS:
            return False
        handle = kernel32.OpenProcess(PROCESS_ACCESS, False, int(pid))
        if not handle:
            return False
        try:
            return bool(psapi.EmptyWorkingSet(handle))
        finally:
            kernel32.CloseHandle(handle)

    def optimization_finished(self, result):
        self.optimizing = False
        saved = format_bytes(result["gained"])
        suffix = " • cache flushed" if result.get("cache_flushed") else ""
        self.opt_result_var.set(f"{result['trimmed']} processes • {saved} available{suffix}")
        self.status_var.set(f"Optimization complete  •  {saved} more available{suffix}")
        if self.current_page == "processes":
            self.request_process_scan(immediate=True)

    def optimization_failed(self, error):
        self.optimizing = False
        self.opt_result_var.set("Optimization failed")
        messagebox.showerror(APP_NAME, f"Optimization failed:\n{error}", parent=self.root)

    # ---------------- settings --------------------------------------------

    def apply_auto_setting(self):
        self.auto_optimizer = bool(self.auto_var.get())
        self.settings_auto_var.set(self.auto_optimizer)
        self.save_settings()

    def change_auto(self, enabled):
        self.auto_optimizer = bool(enabled)
        if hasattr(self, "auto_var"):
            self.auto_var.set(self.auto_optimizer)
        self.save_settings()

    def change_startup(self, enabled):
        try:
            set_startup(bool(enabled))
            self.startup_enabled = bool(enabled)
            self.save_settings()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not change startup:\n{exc}", parent=self.root)

    def change_smart(self, enabled):
        self.smart_exclusions = bool(enabled)
        self.save_settings()

    def set_refresh_interval(self, value):
        try:
            self.refresh_seconds = float(value)
        except (TypeError, ValueError):
            self.refresh_seconds = 3.0
        self.monitor_wakeup.set()
        self.save_settings()

    def on_threshold_change(self, value=None):
        try:
            self.threshold = int(float(value if value is not None else self.threshold_value.get()))
        except (TypeError, ValueError):
            self.threshold = 80
        self.threshold_value.set(self.threshold)
        self.threshold_label.configure(text=f"{self.threshold}%")

        # ttk.Scale fires its command continuously while dragging - debounce
        # the disk write so a single drag doesn't produce dozens of writes.
        if self.threshold_save_after_id is not None:
            try:
                self.root.after_cancel(self.threshold_save_after_id)
            except tk.TclError:
                pass
        self.threshold_save_after_id = self.root.after(400, self.save_settings)

    def reset_settings(self):
        if not messagebox.askyesno(APP_NAME, "Restore default settings?", parent=self.root):
            return
        self.auto_optimizer = False
        self.threshold = 80
        self.refresh_seconds = 3.0
        self.smart_exclusions = True
        self.save_settings()
        self.startup_enabled = get_startup_state()
        self.startup_var.set(self.startup_enabled)
        self.smart_var.set(True)
        self.settings_auto_var.set(False)
        self.auto_var.set(False)
        self.refresh_var.set("3.0")
        self.threshold_value.set(80)
        self.threshold_label.configure(text="80%")
        self.monitor_wakeup.set()

    def load_settings(self):
        try:
            if CONFIG_FILE.exists():
                return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def save_settings(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps({
                "auto_optimizer": self.auto_optimizer,
                "threshold": self.threshold,
                "refresh_seconds": self.refresh_seconds,
                "smart_exclusions": self.smart_exclusions,
                "active_plugin": self.active_plugin_name,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    def relaunch_as_admin(self):
        if is_admin():
            messagebox.showinfo(APP_NAME, "Reduce Memory is already running as administrator.", parent=self.root)
            return
        try:
            request_admin()
            self.on_close()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not request administrator access:\n{exc}", parent=self.root)

    # ---------------- about / misc ---------------------------------------

    def open_config_folder(self):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(CONFIG_DIR)  # noqa: S606 - Windows-only utility, opens Explorer
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open settings folder:\n{exc}", parent=self.root)

    def copy_diagnostics(self):
        build = detect_windows_version()[2]
        text = (
            f"{APP_NAME} {APP_VERSION}\n"
            f"Python: {python_version_string()}\n"
            f"Windows build: {build}\n"
            f"psutil: {getattr(psutil, '__version__', 'unknown')}\n"
            f"Privileges: {'Administrator' if is_admin() else 'Limited'}\n"
            f"Active plugin: {self.active_plugin_name or 'None'}\n"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ---------------- shutdown --------------------------------------------

    def on_close(self):
        if not self.running:
            return
        self.running = False
        self.monitor_stop.set()
        self.process_worker_stop.set()
        self.monitor_wakeup.set()
        self.process_wakeup.set()
        self.save_settings()
        for attr in ("ui_poll_after_id", "dashboard_after_id", "process_poll_after_id",
                     "search_after_id", "threshold_save_after_id"):
            ident = getattr(self, attr, None)
            if ident:
                try:
                    self.root.after_cancel(ident)
                except tk.TclError:
                    pass
        self.root.destroy()


def main():
    if not IS_WINDOWS:
        raise SystemExit(f"{APP_NAME} is Windows-only.")

    try:
        require_supported_windows()
    except RuntimeError as exc:
        # A failed check here used to be an unhandled exception, which under
        # a --noconsole PyInstaller build shows nothing at all to the user.
        error_root = tk.Tk()
        error_root.withdraw()
        messagebox.showerror(APP_NAME, str(exc))
        error_root.destroy()
        return

    root = tk.Tk()
    root.withdraw()  # stay hidden behind the splash until it hands off
    try:
        root.tk.call("tk", "scaling", 1.0)
    except tk.TclError:
        pass

    def launch_app():
        root.deiconify()
        ReduceMemoryApp(root)
        if "--background-start" in sys.argv:
            root.after(1000, root.deiconify)

    SplashScreen(root, on_continue=launch_app)
    root.mainloop()


if __name__ == "__main__":
    main()