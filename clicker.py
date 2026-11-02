"""Maus-Klicker - Mausklick-Abläufe aufnehmen, bearbeiten und abspielen.

Jeder Ablauf wird als gut lesbare JSON-Datei gespeichert (Standard:
sequence.json neben diesem Skript) und nach jeder Änderung automatisch gesichert.

Schritte können an ein Programmfenster gebunden werden: Ihre Koordinaten werden
dann relativ zur oberen linken Ecke des Fensterinhalts gespeichert (in Pixeln
bei 100 % Skalierung). So trifft das Abspielen auch dann noch die richtige
Stelle, wenn das Fenster verschoben wurde oder auf einem anderen Bildschirm mit
anderer Skalierung liegt.

Globale Tastenkürzel (funktionieren auch, wenn ein anderes Programm aktiv ist):
    F8   aktuelle Mausposition als neuen Schritt hinzufügen (ohne Klick)
    F9   Abspielen starten
    Esc  Abspielen / Aufnahme stoppen
"""

import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk

# Make Tk and pynput use real screen pixels, otherwise coordinates are off
# when Windows display scaling is not 100 %.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

from pynput import keyboard, mouse  # noqa: E402  (must come after DPI setup)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(APP_DIR, "sequence.json")
FILE_VERSION = 2

BUTTONS = {
    "left": mouse.Button.left,
    "right": mouse.Button.right,
    "middle": mouse.Button.middle,
}

BUTTON_LABELS = {"left": "links", "right": "rechts", "middle": "Mitte"}

DEFAULT_SETTINGS = {
    "repeat": 1,              # 0 = repeat forever
    "start_delay": 3.0,       # seconds before replay starts
    "move_duration": 0.2,     # seconds for each mouse movement
    "default_delay": 0.5,     # wait before a step that has no recorded timing
    "record_timing": True,    # use real time between recorded clicks as delay
    "minimize_on_play": True,
    "bring_to_front": True,   # activate the program window before replay
}

COLUMNS = (
    # id, heading, width, anchor
    ("idx", "#", 40, "center"),
    ("enabled", "An", 40, "center"),
    ("label", "Bezeichnung", 200, "w"),
    ("coords", "Koordinaten", 90, "center"),
    ("x", "X", 65, "center"),
    ("y", "Y", 65, "center"),
    ("button", "Taste", 65, "center"),
    ("clicks", "Klicks", 55, "center"),
    ("delay", "Warten vorher (s)", 125, "center"),
)


# --------------------------------------------------------------------------- #
# Win32 window helpers
# --------------------------------------------------------------------------- #
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shcore = ctypes.WinDLL("shcore")
_dwmapi = ctypes.WinDLL("dwmapi")

HWND = wintypes.HWND
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)

_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = HWND
_user32.GetAncestor.argtypes = [HWND, wintypes.UINT]
_user32.GetAncestor.restype = HWND
_user32.GetWindowTextLengthW.argtypes = [HWND]
_user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindow.argtypes = [HWND]
_user32.IsWindowVisible.argtypes = [HWND]
_user32.IsIconic.argtypes = [HWND]
_user32.ClientToScreen.argtypes = [HWND, ctypes.POINTER(wintypes.POINT)]
_user32.MonitorFromWindow.argtypes = [HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.GetForegroundWindow.restype = HWND
_user32.SetForegroundWindow.argtypes = [HWND]
_user32.BringWindowToTop.argtypes = [HWND]
_user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.UINT]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_shcore.GetDpiForMonitor.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
_dwmapi.DwmGetWindowAttribute.argtypes = [HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

GA_ROOT = 2
SW_RESTORE = 9
MONITOR_DEFAULTTONEAREST = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_CLOAKED = 14
HWND_TOPMOST = HWND(-1)
HWND_NOTOPMOST = HWND(-2)
SWP_RAISE_FLAGS = 0x0001 | 0x0002 | 0x0040   # NOSIZE | NOMOVE | SHOWWINDOW


def root_window_at(x, y):
    """Top-level window under a screen point (or None)."""
    hwnd = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return _user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None


def window_title(hwnd):
    length = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid):
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def window_info(hwnd):
    """Data stored in the JSON to find the program window again later."""
    return {
        "process": process_name(window_pid(hwnd)),
        "title": window_title(hwnd),
        "class": window_class(hwnd),
    }


def is_alive(hwnd):
    return bool(hwnd) and bool(_user32.IsWindow(hwnd))


def _is_cloaked(hwnd):
    cloaked = wintypes.DWORD()
    _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return bool(cloaked.value)


def belongs_to_program(hwnd, program):
    if not (program and is_alive(hwnd)):
        return False
    if program.get("process"):
        return process_name(window_pid(hwnd)).lower() == program["process"].lower()
    return window_class(hwnd) == program.get("class")


def find_window(program, preferred=None):
    """Find the visible top-level window of a program.

    Matches by process name first, then prefers the same window class and the
    same title. Among equal matches the top-most window wins.
    """
    if not program:
        return None
    if (preferred and is_alive(preferred) and _user32.IsWindowVisible(preferred)
            and belongs_to_program(preferred, program)):
        return preferred

    own_pid = os.getpid()
    best, best_score = None, -1

    def callback(hwnd, _):
        nonlocal best, best_score
        if not _user32.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
            return True
        title = window_title(hwnd)
        if not title or window_pid(hwnd) == own_pid:
            return True
        score = 0
        if program.get("process"):
            if process_name(window_pid(hwnd)).lower() != program["process"].lower():
                return True
        if window_class(hwnd) == program.get("class"):
            score += 2
        if title == program.get("title"):
            score += 4
        if not program.get("process") and score == 0:
            return True
        if score > best_score:
            best, best_score = hwnd, score
        return True

    _user32.EnumWindows(WNDENUMPROC(callback), 0)
    return best


def client_origin(hwnd):
    """Screen position of the top-left corner of the window's content area."""
    pt = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y


def window_scale(hwnd):
    """Display scaling of the monitor the window is on (1.0 = 100 %)."""
    try:
        monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        _shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        return (dpi_x.value or 96) / 96
    except Exception:
        return 1.0


def screen_to_rel(hwnd, x, y):
    ox, oy = client_origin(hwnd)
    scale = window_scale(hwnd)
    return round((x - ox) / scale), round((y - oy) / scale)


def rel_to_screen(hwnd, x, y):
    ox, oy = client_origin(hwnd)
    scale = window_scale(hwnd)
    return ox + round(x * scale), oy + round(y * scale)


def bring_to_front(hwnd):
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows only lets the foreground thread change the foreground window, so
    # briefly attach to its input queue.
    fg = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    own_thread = _kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != own_thread
                    and _user32.AttachThreadInput(own_thread, fg_thread, True))
    try:
        # Toggling "always on top" raises the window even when Windows refuses
        # to hand over the keyboard focus.
        _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_RAISE_FLAGS)
        _user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_RAISE_FLAGS)
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(own_thread, fg_thread, False)


def describe_program(program):
    if not program:
        return "keins (Koordinaten sind absolute Bildschirmpositionen)"
    title = program.get("title") or program.get("class") or "?"
    if len(title) > 45:
        title = title[:42] + "..."
    return f'{program.get("process") or "?"}  -  "{title}"'


# --------------------------------------------------------------------------- #
# Data helpers
# --------------------------------------------------------------------------- #
def make_event(**values):
    event = {
        "label": "",
        "x": 0,
        "y": 0,
        "relative": False,    # True = relative to the program window
        "button": "left",
        "clicks": 1,
        "delay": DEFAULT_SETTINGS["default_delay"],
        "enabled": True,
    }
    for key, value in values.items():
        if key in event:
            event[key] = coerce_field(key, value)
    return event


def coerce_field(name, value):
    """Convert a value (often a string typed by the user) to the field's type."""
    if name == "label":
        return str(value)
    if name in ("x", "y"):
        return int(round(float(value)))
    if name == "clicks":
        return max(1, min(3, int(float(value))))
    if name == "delay":
        return max(0.0, round(float(value), 3))
    if name == "button":
        value = str(value).strip().lower()
        value = {label.lower(): key for key, label in BUTTON_LABELS.items()}.get(value, value)
        if value not in BUTTONS:
            raise ValueError(f"Taste muss eine von {', '.join(BUTTON_LABELS.values())} sein")
        return value
    if name in ("enabled", "relative"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "✓")
        return bool(value)
    raise KeyError(name)


def load_sequence(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in data.get("settings", {}).items() if k in DEFAULT_SETTINGS})
    program = data.get("program") or None
    if program:
        program = {k: str(program.get(k, "")) for k in ("process", "title", "class")}
    events = [make_event(**ev) for ev in data.get("events", [])]
    return settings, program, events


def save_sequence(path, settings, program, events):
    data = {"version": FILE_VERSION, "settings": settings, "program": program, "events": events}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Edit dialog
# --------------------------------------------------------------------------- #
class EventDialog(tk.Toplevel):
    """Dialog for editing all fields of one step, incl. picking a position."""

    def __init__(self, app, event, title="Schritt bearbeiten"):
        parent = app.root
        super().__init__(parent)
        self.app = app
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self._countdown_job = None

        self.vars = {
            "label": tk.StringVar(value=event["label"]),
            "x": tk.StringVar(value=str(event["x"])),
            "y": tk.StringVar(value=str(event["y"])),
            "relative": tk.BooleanVar(value=event["relative"]),
            "button": tk.StringVar(value=BUTTON_LABELS[event["button"]]),
            "clicks": tk.StringVar(value=str(event["clicks"])),
            "delay": tk.StringVar(value=str(event["delay"])),
            "enabled": tk.BooleanVar(value=event["enabled"]),
        }

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        def row(r, text, widget):
            ttk.Label(body, text=text).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)

        label_entry = ttk.Entry(body, textvariable=self.vars["label"], width=32)
        row(0, "Bezeichnung", label_entry)

        ttk.Label(body, text="Position").grid(row=1, column=0, sticky="w", pady=3)
        pos = ttk.Frame(body)
        pos.grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(pos, text="X").pack(side="left")
        ttk.Entry(pos, textvariable=self.vars["x"], width=7).pack(side="left", padx=(2, 8))
        ttk.Label(pos, text="Y").pack(side="left")
        ttk.Entry(pos, textvariable=self.vars["y"], width=7).pack(side="left", padx=(2, 0))

        rel_check = ttk.Checkbutton(body, text="Relativ zum Programmfenster",
                                    variable=self.vars["relative"], command=self._toggle_relative)
        rel_check.grid(row=2, column=1, columnspan=2, sticky="w")
        if not app.program and not event["relative"]:
            rel_check.state(["disabled"])

        pick = ttk.Frame(body)
        pick.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(2, 6))
        self.pick_btn = ttk.Button(pick, text="Mausposition in 3 s übernehmen", command=self._start_capture)
        self.pick_btn.pack(side="left")
        ttk.Button(pick, text="Position zeigen", command=self._show_position).pack(side="left", padx=6)

        row(4, "Taste", ttk.Combobox(body, textvariable=self.vars["button"],
                                      values=list(BUTTON_LABELS.values()), state="readonly", width=10))
        row(5, "Klicks", ttk.Spinbox(body, textvariable=self.vars["clicks"], from_=1, to=3, width=10))
        row(6, "Warten vorher (s)", ttk.Spinbox(body, textvariable=self.vars["delay"],
                                              from_=0, to=3600, increment=0.1, width=10))
        ttk.Checkbutton(body, text="Aktiv", variable=self.vars["enabled"]).grid(
            row=7, column=1, sticky="w", pady=3)

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="Abbrechen", command=self._cancel).pack(side="left")

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        label_entry.focus_set()
        label_entry.select_range(0, "end")
        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 60}")
        self.grab_set()
        self.wait_window(self)

    def _xy(self):
        try:
            return int(float(self.vars["x"].get())), int(float(self.vars["y"].get()))
        except ValueError:
            messagebox.showerror("Ungültige Position", "X und Y müssen Zahlen sein.", parent=self)
            return None

    def _not_found(self):
        messagebox.showerror("Programm nicht gefunden",
                             "Das Programmfenster ist nicht geöffnet, daher kann die Position "
                             "nicht umgerechnet werden.",
                             parent=self)

    def _toggle_relative(self):
        relative = self.vars["relative"].get()
        xy = self._xy()
        converted = xy and self.app.convert_point(*xy, to_relative=relative)
        if not converted:
            self.vars["relative"].set(not relative)
            if xy:
                self._not_found()
            return
        self.vars["x"].set(str(converted[0]))
        self.vars["y"].set(str(converted[1]))

    def _start_capture(self, remaining=3):
        if remaining == 0:
            self.pick_btn.config(text="Mausposition in 3 s übernehmen", state="normal")
            xy = mouse.Controller().position
            if self.vars["relative"].get():
                xy = self.app.convert_point(*xy, to_relative=True)
                if not xy:
                    self._not_found()
                    return
            self.vars["x"].set(str(int(xy[0])))
            self.vars["y"].set(str(int(xy[1])))
            self.lift()
            return
        if remaining == 3:
            self.pick_btn.config(state="disabled")
        self.pick_btn.config(text=f"Maus aufs Ziel... {remaining}")
        self._countdown_job = self.after(1000, self._start_capture, remaining - 1)

    def _show_position(self):
        xy = self._xy()
        if not xy:
            return
        if self.vars["relative"].get():
            xy = self.app.convert_point(*xy, to_relative=False)
            if not xy:
                self._not_found()
                return
        mouse.Controller().position = xy

    def _ok(self):
        try:
            self.result = make_event(**{k: v.get() for k, v in self.vars.items()})
        except (ValueError, KeyError) as exc:
            messagebox.showerror("Ungültiger Wert", str(exc), parent=self)
            return
        self._close()

    def _cancel(self):
        self.result = None
        self._close()

    def _close(self):
        if self._countdown_job:
            self.after_cancel(self._countdown_job)
        self.grab_release()
        self.destroy()


# --------------------------------------------------------------------------- #
# Main application
# --------------------------------------------------------------------------- #
class ClickerApp:
    def __init__(self, root, path=DEFAULT_FILE):
        self.root = root
        self.path = path
        self.events = []
        self.program = None             # {"process", "title", "class"} or None
        self._hwnd = None               # cached window handle of the program
        self.queue = queue.Queue()      # messages from listener / player threads

        self.recording = None           # None, "single", "multi" or "pick"
        self.mouse_listener = None
        self.last_click_time = None

        self.playing = False
        self.stop_event = threading.Event()
        self.play_thread = None
        self._play_error = None
        self._playing_iid = None

        self._editor = None             # inline cell editor widget
        self._drag_from = None
        self._dragged = False
        self._loading = False

        self._build_ui()
        self._load(path, create_if_missing=True)

        self.key_listener = keyboard.Listener(on_press=self._on_global_key)
        self.key_listener.daemon = True
        self.key_listener.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(30, self._poll_queue)
        self.root.after(500, self._poll_program)

    # ----------------------------------------------------------------- UI --
    def _build_ui(self):
        root = self.root
        root.title("Maus-Klicker")
        # Sizes below are for 100 % display scaling; Tk already scales the
        # fonts, so scale window size, column widths and row height to match.
        s = self.ui_scale = root.winfo_fpixels("1i") / 96
        root.geometry(f"{round(1060 * s)}x{round(680 * s)}")
        root.minsize(round(920 * s), round(560 * s))

        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=round(24 * s))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        # File bar
        filebar = ttk.Frame(root, padding=(8, 8, 8, 0))
        filebar.pack(fill="x")
        ttk.Button(filebar, text="Neu...", command=self.new_file).pack(side="left")
        ttk.Button(filebar, text="Öffnen...", command=self.open_file).pack(side="left", padx=4)
        ttk.Button(filebar, text="Speichern unter...", command=self.save_as).pack(side="left")
        self.file_var = tk.StringVar()
        ttk.Label(filebar, textvariable=self.file_var, foreground="#555").pack(side="left", padx=10)

        # Program bar
        prog = ttk.LabelFrame(root, text="Programmfenster", padding=8)
        prog.pack(fill="x", padx=8, pady=(8, 0))
        self.pick_btn = ttk.Button(prog, text="🎯 Programm wählen", command=self.pick_program)
        self.pick_btn.pack(side="left")
        ttk.Button(prog, text="Entfernen", command=self.clear_program).pack(side="left", padx=4)
        self.program_var = tk.StringVar()
        ttk.Label(prog, textvariable=self.program_var).pack(side="left", padx=(8, 0))
        self.program_state_var = tk.StringVar()
        self.program_state_lbl = ttk.Label(prog, textvariable=self.program_state_var)
        self.program_state_lbl.pack(side="left", padx=8)
        self.v_bring_to_front = tk.BooleanVar(value=DEFAULT_SETTINGS["bring_to_front"])
        ttk.Checkbutton(prog, text="Beim Abspielen in den Vordergrund",
                        variable=self.v_bring_to_front).pack(side="right")

        # Record bar
        rec = ttk.LabelFrame(root, text="Aufnahme", padding=8)
        rec.pack(fill="x", padx=8, pady=(8, 0))
        self.rec_single_btn = ttk.Button(rec, text="● Klick aufnehmen", command=self.record_single)
        self.rec_single_btn.pack(side="left")
        self.rec_multi_btn = ttk.Button(rec, text="●● Mehrere aufnehmen", command=self.toggle_record_multi)
        self.rec_multi_btn.pack(side="left", padx=4)
        ttk.Button(rec, text="+ Mausposition hinzufügen (F8)", command=self.add_current_position).pack(side="left")
        ttk.Button(rec, text="+ Manuell hinzufügen...", command=self.add_manual).pack(side="left", padx=4)

        self.v_record_timing = tk.BooleanVar(value=DEFAULT_SETTINGS["record_timing"])
        ttk.Checkbutton(rec, text="Zeit zwischen Klicks aufnehmen",
                        variable=self.v_record_timing).pack(side="left", padx=(12, 0))

        # Sequence list
        mid = ttk.LabelFrame(root, text="Ablauf", padding=8)
        mid.pack(fill="both", expand=True, padx=8, pady=8)

        tree_frame = ttk.Frame(mid)
        tree_frame.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="extended")
        for col, heading, width, anchor in COLUMNS:
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=round(width * s), anchor=anchor, stretch=(col == "label"))
        self.tree.tag_configure("disabled", foreground="#999")
        self.tree.tag_configure("playing", background="#cde8ff")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        self.tree.bind("<ButtonPress-1>", self._on_tree_press)
        self.tree.bind("<B1-Motion>", self._on_tree_drag)
        self.tree.bind("<ButtonRelease-1>", self._on_tree_release)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Return>", lambda e: self.edit_selected())
        self.tree.bind("<F2>", lambda e: self._edit_selected_label())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<space>", lambda e: self.toggle_enabled())
        self.tree.bind("<Control-Up>", lambda e: self.move_selected(-1) or "break")
        self.tree.bind("<Control-Down>", lambda e: self.move_selected(1) or "break")
        self.tree.bind("<Control-d>", lambda e: self.duplicate_selected())
        self.tree.bind("<Control-a>", lambda e: self.tree.selection_set(self.tree.get_children()) or "break")
        self.tree.bind("<MouseWheel>", lambda e: self._commit_editor(), add="+")

        side = ttk.Frame(mid, padding=(8, 0, 0, 0))
        side.pack(side="left", fill="y")
        for text, cmd in (
            ("Bearbeiten...", self.edit_selected),
            ("Duplizieren", self.duplicate_selected),
            ("Löschen", self.delete_selected),
            ("An / Aus", self.toggle_enabled),
            (None, None),
            ("▲ Nach oben", lambda: self.move_selected(-1)),
            ("▼ Nach unten", lambda: self.move_selected(1)),
            (None, None),
            ("Fenster / Bildschirm", self.toggle_relative_selected),
            ("Position zeigen", self.show_selected_position),
            ("Alles löschen", self.clear_all),
        ):
            if text is None:
                ttk.Separator(side).pack(fill="x", pady=6)
            else:
                ttk.Button(side, text=text, command=cmd, width=19).pack(fill="x", pady=2)
        ttk.Label(side, text="Doppelklick auf Zelle:\nbearbeiten.\nZeilen ziehen:\nsortieren.",
                  foreground="#666", justify="left").pack(side="bottom", anchor="w")

        # Playback bar
        play = ttk.LabelFrame(root, text="Abspielen", padding=8)
        play.pack(fill="x", padx=8)

        self.v_repeat = tk.StringVar(value=str(DEFAULT_SETTINGS["repeat"]))
        self.v_start_delay = tk.StringVar(value=str(DEFAULT_SETTINGS["start_delay"]))
        self.v_move_duration = tk.StringVar(value=str(DEFAULT_SETTINGS["move_duration"]))
        self.v_default_delay = tk.StringVar(value=str(DEFAULT_SETTINGS["default_delay"]))
        self.v_minimize = tk.BooleanVar(value=DEFAULT_SETTINGS["minimize_on_play"])

        def spin(text, var, frm, to, inc, width=6):
            ttk.Label(play, text=text).pack(side="left")
            ttk.Spinbox(play, textvariable=var, from_=frm, to=to, increment=inc,
                        width=width).pack(side="left", padx=(2, 10))

        spin("Wiederholen (0 = ∞)", self.v_repeat, 0, 100000, 1)
        spin("Startverzögerung (s)", self.v_start_delay, 0, 60, 0.5)
        spin("Bewegungszeit (s)", self.v_move_duration, 0, 5, 0.05)
        spin("Standard-Wartezeit (s)", self.v_default_delay, 0, 60, 0.1)
        ttk.Checkbutton(play, text="Beim Abspielen minimieren", variable=self.v_minimize).pack(side="left")

        bottom = ttk.Frame(root, padding=8)
        bottom.pack(fill="x")
        self.play_btn = ttk.Button(bottom, text="▶ Abspielen (F9)", style="Accent.TButton", command=self.play)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(bottom, text="■ Stopp (Esc)", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=10)
        self.count_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.count_var, foreground="#555").pack(side="right")

        for var in (self.v_repeat, self.v_start_delay, self.v_move_duration, self.v_default_delay,
                    self.v_minimize, self.v_record_timing, self.v_bring_to_front):
            var.trace_add("write", lambda *_: self._save())

    def set_status(self, text):
        self.status_var.set(text)

    # ------------------------------------------------------- settings/file --
    def get_settings(self):
        def num(var, cast, key, minimum=0):
            try:
                return max(minimum, cast(float(var.get())))
            except (ValueError, tk.TclError):
                return DEFAULT_SETTINGS[key]

        return {
            "repeat": num(self.v_repeat, int, "repeat"),
            "start_delay": num(self.v_start_delay, float, "start_delay"),
            "move_duration": num(self.v_move_duration, float, "move_duration"),
            "default_delay": num(self.v_default_delay, float, "default_delay"),
            "record_timing": bool(self.v_record_timing.get()),
            "minimize_on_play": bool(self.v_minimize.get()),
            "bring_to_front": bool(self.v_bring_to_front.get()),
        }

    def _apply_settings(self, s):
        self._loading = True
        try:
            self.v_repeat.set(str(s["repeat"]))
            self.v_start_delay.set(str(s["start_delay"]))
            self.v_move_duration.set(str(s["move_duration"]))
            self.v_default_delay.set(str(s["default_delay"]))
            self.v_record_timing.set(bool(s["record_timing"]))
            self.v_minimize.set(bool(s["minimize_on_play"]))
            self.v_bring_to_front.set(bool(s["bring_to_front"]))
        finally:
            self._loading = False

    def _load(self, path, create_if_missing=False):
        if not os.path.exists(path) and create_if_missing:
            settings, program, events = dict(DEFAULT_SETTINGS), None, []
        else:
            try:
                settings, program, events = load_sequence(path)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                messagebox.showerror("Datei konnte nicht geöffnet werden", f"{path}\n\n{exc}")
                return False
        self.path = path
        self.events = events
        self._set_program(program, None)
        self._apply_settings(settings)
        self._refresh()
        self._save()
        self.file_var.set(f"Datei: {path}  (automatisch gespeichert)")
        self.set_status(f"{len(events)} Schritte geladen.")
        return True

    def _save(self):
        if self._loading:
            return
        try:
            save_sequence(self.path, self.get_settings(), self.program, self.events)
        except OSError as exc:
            self.set_status(f"Speichern fehlgeschlagen: {exc}")

    def new_file(self):
        path = filedialog.asksaveasfilename(title="Neuer Ablauf", defaultextension=".json",
                                            filetypes=[("JSON", "*.json")], initialdir=APP_DIR)
        if path:
            self.events = []
            self.path = path
            self._set_program(None, None)
            self._apply_settings(DEFAULT_SETTINGS)
            self._refresh()
            self._save()
            self.file_var.set(f"Datei: {path}  (automatisch gespeichert)")
            self.set_status("Neuer Ablauf erstellt.")

    def open_file(self):
        path = filedialog.askopenfilename(title="Ablauf öffnen", filetypes=[("JSON", "*.json")],
                                          initialdir=os.path.dirname(self.path))
        if path:
            self._load(path)

    def save_as(self):
        path = filedialog.asksaveasfilename(title="Ablauf speichern unter", defaultextension=".json",
                                            filetypes=[("JSON", "*.json")],
                                            initialdir=os.path.dirname(self.path))
        if path:
            self.path = path
            self._save()
            self.file_var.set(f"Datei: {path}  (automatisch gespeichert)")
            self.set_status("Gespeichert.")

    # ------------------------------------------------------ program window --
    def pick_program(self):
        if self.recording or self.playing:
            return
        self._start_recording("pick")
        self.set_status("Klicke auf das Programmfenster, das gesteuert werden soll... (Esc bricht ab)")

    def _set_program(self, program, hwnd):
        self.program = program
        self._hwnd = hwnd
        self.program_var.set(f"Programm: {describe_program(program)}")
        self._update_program_state()

    def _program_picked(self, hwnd):
        if not is_alive(hwnd):
            self.set_status("An dieser Stelle wurde kein Fenster gefunden.")
            return
        old_program = self.program
        self._set_program(window_info(hwnd), hwnd)
        self.set_status(f"Programm gesetzt: {describe_program(self.program)}")
        screen_steps = [i for i, ev in enumerate(self.events) if not ev["relative"]]
        if screen_steps and old_program is None and messagebox.askyesno(
                "Schritte umrechnen",
                f"Die {len(screen_steps)} vorhandenen Schritt(e) mit Bildschirmposition in Positionen "
                "relativ zu diesem Programmfenster umrechnen?\n\nDas Fenster sollte dafür dort "
                "liegen, wo es bei der Aufnahme war."):
            self._convert_steps(screen_steps, to_relative=True)
        self._save()

    def clear_program(self):
        if not self.program:
            return
        rel_steps = [i for i, ev in enumerate(self.events) if ev["relative"]]
        if rel_steps:
            if self.program_hwnd():
                if not messagebox.askyesno(
                        "Programm entfernen",
                        f"{len(rel_steps)} Schritt(e) sind relativ zum Programmfenster. In absolute "
                        "Bildschirmpositionen umrechnen (mit der aktuellen Fensterposition)?"):
                    return
                self._convert_steps(rel_steps, to_relative=False)
            elif not messagebox.askyesno(
                    "Programm entfernen",
                    f"Das Programmfenster ist nicht geöffnet. {len(rel_steps)} fensterrelative(r) "
                    "Schritt(e) können nicht umgerechnet werden und werden erst wieder abgespielt, "
                    "wenn ein Programm gewählt ist. Fortfahren?"):
                return
        self._set_program(None, None)
        self._save()
        self.set_status("Programm entfernt.")

    def program_hwnd(self):
        """Handle of the program window right now, or None if it isn't open."""
        hwnd = find_window(self.program, self._hwnd)
        self._hwnd = hwnd
        return hwnd

    def convert_point(self, x, y, to_relative):
        hwnd = self.program_hwnd()
        if not hwnd:
            return None
        return screen_to_rel(hwnd, x, y) if to_relative else rel_to_screen(hwnd, x, y)

    def _convert_steps(self, indices, to_relative):
        hwnd = self.program_hwnd()
        if not hwnd:
            self.set_status("Programmfenster nicht gefunden - Koordinaten können nicht umgerechnet werden.")
            return False
        for i in indices:
            ev = self.events[i]
            if ev["relative"] == to_relative:
                continue
            convert = screen_to_rel if to_relative else rel_to_screen
            ev["x"], ev["y"] = convert(hwnd, ev["x"], ev["y"])
            ev["relative"] = to_relative
        self._changed(select=indices)
        return True

    def toggle_relative_selected(self):
        sel = self.selected_indices()
        if not sel:
            return
        if not self.program:
            self.set_status("Bitte zuerst ein Programm wählen, um fensterrelative Koordinaten zu nutzen.")
            return
        to_relative = not all(self.events[i]["relative"] for i in sel)
        if self._convert_steps(sel, to_relative):
            self.set_status(f"{len(sel)} Schritt(e) in {'Fenster' if to_relative else 'Bildschirm'}-Koordinaten umgerechnet.")

    def _point_for_step(self, x, y, root_hwnd):
        """Turn a screen point into (x, y, relative) for a new step."""
        if self.program and belongs_to_program(root_hwnd, self.program):
            # Clicks in another window of the program (e.g. a dialog) are
            # anchored to the main window; with several main windows use the
            # one that was clicked.
            if window_class(root_hwnd) == self.program.get("class"):
                self._hwnd = root_hwnd
            hwnd = self.program_hwnd()
            if hwnd:
                rx, ry = screen_to_rel(hwnd, x, y)
                return rx, ry, True
        return x, y, False

    def _update_program_state(self):
        if not self.program:
            self.program_state_var.set("")
            return
        hwnd = self.program_hwnd()
        if hwnd:
            ox, oy = client_origin(hwnd)
            text = f"● gefunden - Anker ({ox}, {oy}), Skalierung {window_scale(hwnd):.0%}"
            if _user32.IsIconic(hwnd):
                text = "● gefunden (minimiert)"
            self.program_state_var.set(text)
            self.program_state_lbl.config(foreground="#1a7f37")
        else:
            self.program_state_var.set("● nicht geöffnet")
            self.program_state_lbl.config(foreground="#c62828")

    def _poll_program(self):
        if not self.playing:
            self._update_program_state()
        self.root.after(1000, self._poll_program)

    # -------------------------------------------------------------- list --
    def _refresh(self, select=None):
        self._close_editor()
        self._playing_iid = None
        self.tree.delete(*self.tree.get_children())
        for i, ev in enumerate(self.events):
            self.tree.insert("", "end", iid=str(i), tags=() if ev["enabled"] else ("disabled",), values=(
                i + 1, "✓" if ev["enabled"] else "", ev["label"],
                "Fenster" if ev["relative"] else "Bildschirm", ev["x"], ev["y"],
                BUTTON_LABELS[ev["button"]], ev["clicks"], f"{ev['delay']:.2f}"))
        if select:
            iids = [str(i) for i in select if 0 <= i < len(self.events)]
            if iids:
                self.tree.selection_set(iids)
                self.tree.focus(iids[-1])
                self.tree.see(iids[-1])
        n = len(self.events)
        self.count_var.set(f"{n} Schritt{'e' if n != 1 else ''}")

    def _changed(self, select=None):
        self._refresh(select)
        self._save()

    def selected_indices(self):
        return sorted(int(i) for i in self.tree.selection())

    def _insert_index(self):
        sel = self.selected_indices()
        return sel[-1] + 1 if sel else len(self.events)

    def insert_event(self, event):
        idx = self._insert_index()
        self.events.insert(idx, event)
        self._changed(select=[idx])
        return idx

    def _next_label(self):
        return f"Schritt {len(self.events) + 1}"

    def add_current_position(self):
        sx, sy = mouse.Controller().position
        x, y, relative = self._point_for_step(sx, sy, root_window_at(sx, sy))
        idx = self.insert_event(make_event(label=self._next_label(), x=x, y=y, relative=relative,
                                           delay=self.get_settings()["default_delay"]))
        self.set_status(f"Schritt {idx + 1} bei ({x}, {y}) {'im Fenster' if relative else 'auf dem Bildschirm'} hinzugefügt.")

    def add_manual(self):
        sx, sy = mouse.Controller().position
        x, y, relative = self._point_for_step(sx, sy, root_window_at(sx, sy))
        dlg = EventDialog(self, make_event(label=self._next_label(), x=x, y=y, relative=relative,
                                           delay=self.get_settings()["default_delay"]),
                          title="Schritt hinzufügen")
        if dlg.result:
            self.insert_event(dlg.result)

    def edit_selected(self):
        sel = self.selected_indices()
        if not sel:
            return
        idx = sel[0]
        dlg = EventDialog(self, self.events[idx])
        if dlg.result:
            self.events[idx] = dlg.result
            self._changed(select=[idx])

    def duplicate_selected(self):
        sel = self.selected_indices()
        if not sel:
            return
        copies = [dict(self.events[i]) for i in sel]
        pos = sel[-1] + 1
        self.events[pos:pos] = copies
        self._changed(select=range(pos, pos + len(copies)))

    def delete_selected(self):
        sel = self.selected_indices()
        if not sel:
            return
        for i in reversed(sel):
            del self.events[i]
        self._changed(select=[min(sel[0], len(self.events) - 1)])
        self.set_status(f"{len(sel)} Schritt(e) gelöscht.")

    def toggle_enabled(self):
        sel = self.selected_indices()
        if not sel:
            return
        new_state = not all(self.events[i]["enabled"] for i in sel)
        for i in sel:
            self.events[i]["enabled"] = new_state
        self._changed(select=sel)

    def move_selected(self, direction):
        sel = self.selected_indices()
        if not sel:
            return
        if (direction < 0 and sel[0] == 0) or (direction > 0 and sel[-1] == len(self.events) - 1):
            return
        order = sel if direction < 0 else list(reversed(sel))
        for i in order:
            j = i + direction
            self.events[i], self.events[j] = self.events[j], self.events[i]
        self._changed(select=[i + direction for i in sel])

    def clear_all(self):
        if self.events and messagebox.askyesno("Alles löschen", "Alle Schritte dieses Ablaufs löschen?"):
            self.events.clear()
            self._changed()

    def show_selected_position(self):
        sel = self.selected_indices()
        if not sel:
            return
        ev = self.events[sel[0]]
        xy = (ev["x"], ev["y"])
        if ev["relative"]:
            xy = self.convert_point(*xy, to_relative=False)
            if not xy:
                self.set_status("Programmfenster nicht gefunden.")
                return
        mouse.Controller().position = xy

    # ------------------------------------------------- inline cell editing --
    def _on_tree_double_click(self, e):
        if self.tree.identify_region(e.x, e.y) != "cell":
            return
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        col = self.tree.identify_column(e.x)
        name = self.tree.column(col, "id")
        idx = int(iid)
        if name == "enabled":
            self.events[idx]["enabled"] = not self.events[idx]["enabled"]
            self._changed(select=[idx])
        elif name == "coords":
            self.tree.selection_set(iid)
            self.toggle_relative_selected()
        elif name == "idx":
            self.edit_selected()
        else:
            self._start_inline_edit(idx, col, name)
        return "break"

    def _edit_selected_label(self):
        sel = self.selected_indices()
        if sel:
            self._start_inline_edit(sel[0], "#3", "label")

    def _start_inline_edit(self, idx, col, name):
        self._commit_editor()
        iid = str(idx)
        self.tree.see(iid)
        self.tree.update_idletasks()
        bbox = self.tree.bbox(iid, col)
        if not bbox:
            return
        x, y, w, h = bbox
        current = self.events[idx][name]

        if name == "button":
            widget = ttk.Combobox(self.tree, values=list(BUTTON_LABELS.values()), state="readonly")
            widget.set(BUTTON_LABELS[current])
            widget.bind("<<ComboboxSelected>>", lambda e: self._commit_editor())
        else:
            widget = ttk.Entry(self.tree)
            widget.insert(0, str(current))
            widget.select_range(0, "end")
            widget.bind("<FocusOut>", lambda e: self._commit_editor())
        widget.bind("<Return>", lambda e: self._commit_editor() or "break")
        widget.bind("<KP_Enter>", lambda e: self._commit_editor() or "break")
        widget.bind("<Escape>", lambda e: self._close_editor() or "break")
        widget.place(x=x, y=y, width=w, height=h)
        widget.focus_set()
        self._editor = (widget, idx, name)

    def _commit_editor(self):
        if not self._editor:
            return
        widget, idx, name = self._editor
        value = widget.get()
        self._close_editor()
        try:
            self.events[idx][name] = coerce_field(name, value)
        except (ValueError, IndexError) as exc:
            self.set_status(f"Ungültiger Wert ({name}): {exc}")
            return
        self._changed(select=[idx])
        self.tree.focus_set()

    def _close_editor(self):
        if self._editor:
            widget = self._editor[0]
            self._editor = None
            widget.destroy()

    # ---------------------------------------------------- drag to reorder --
    def _on_tree_press(self, e):
        self._commit_editor()
        self._dragged = False
        region = self.tree.identify_region(e.x, e.y)
        self._drag_from = self.tree.identify_row(e.y) if region == "cell" else None

    def _on_tree_drag(self, e):
        if self._drag_from is None or len(self.tree.selection()) > 1:
            return
        target = self.tree.identify_row(e.y)
        if target and target != self._drag_from:
            i, j = int(self._drag_from), int(target)
            self.events.insert(j, self.events.pop(i))
            self._refresh(select=[j])
            self._drag_from = target
            self._dragged = True

    def _on_tree_release(self, e):
        if self._dragged:
            self._save()
        self._drag_from = None
        self._dragged = False

    # ---------------------------------------------------------- recording --
    def record_single(self):
        if self.recording or self.playing:
            return
        self._start_recording("single")
        self.set_status("Klicke irgendwo außerhalb dieses Fensters, um einen Schritt aufzunehmen... (Esc bricht ab)")

    def toggle_record_multi(self):
        if self.recording:
            self.stop_recording()
            return
        if self.playing:
            return
        self._start_recording("multi")
        self.set_status("Klicks werden aufgenommen... klicke in deinem Programm. Esc oder 'Aufnahme stoppen' zum Beenden.")

    def _start_recording(self, mode):
        self.recording = mode
        self.last_click_time = None
        if mode == "multi":
            self.rec_multi_btn.config(text="■ Aufnahme stoppen")
        else:
            self.rec_multi_btn.config(state="disabled")
        self.rec_single_btn.config(state="disabled")
        self.pick_btn.config(state="disabled")
        self.play_btn.config(state="disabled")
        self.mouse_listener = mouse.Listener(on_click=self._on_global_click)
        self.mouse_listener.daemon = True
        self.mouse_listener.start()

    def stop_recording(self):
        if not self.recording:
            return
        self.recording = None
        if self.mouse_listener:
            self.mouse_listener.stop()
            self.mouse_listener = None
        self.rec_multi_btn.config(text="●● Mehrere aufnehmen", state="normal")
        self.rec_single_btn.config(state="normal")
        self.pick_btn.config(state="normal")
        self.play_btn.config(state="normal")
        self.set_status("Aufnahme gestoppt.")

    def _on_global_click(self, x, y, button, pressed):
        # Runs in the listener thread. The window under the click is looked up
        # right away (before it can close), then everything is handed over to
        # the Tk thread.
        if pressed:
            x, y = int(x), int(y)
            self.queue.put(("click", x, y, button, time.perf_counter(), root_window_at(x, y)))

    def _handle_recorded_click(self, sx, sy, button, t, root_hwnd):
        if not self.recording or (root_hwnd and window_pid(root_hwnd) == os.getpid()):
            return  # ignore clicks on the Clicker itself
        if self.recording == "pick":
            self.stop_recording()
            self._program_picked(root_hwnd)
            return
        name = next((n for n, b in BUTTONS.items() if b == button), None)
        if name is None:
            return
        settings = self.get_settings()
        if settings["record_timing"] and self.last_click_time is not None:
            delay = t - self.last_click_time
        else:
            delay = settings["default_delay"]
        self.last_click_time = t
        x, y, relative = self._point_for_step(sx, sy, root_hwnd)

        # A fast second click at the same spot becomes a double-click.
        sel = self.selected_indices()
        if sel and self.recording == "multi" and delay < 0.4:
            prev = self.events[sel[-1]]
            if (prev["button"] == name and prev["clicks"] < 3 and prev["relative"] == relative
                    and abs(prev["x"] - x) <= 4 and abs(prev["y"] - y) <= 4):
                prev["clicks"] += 1
                self._changed(select=[sel[-1]])
                return

        idx = self.insert_event(make_event(label=self._next_label(), x=x, y=y, relative=relative,
                                           button=name, delay=delay))
        where = "im Programmfenster" if relative else "auf dem Bildschirm"
        msg = f"Schritt {idx + 1} aufgenommen: Klick {BUTTON_LABELS[name]} bei ({x}, {y}) {where}."
        if self.recording == "single":
            self.stop_recording()
        self.set_status(msg)

    # ------------------------------------------------------------ hotkeys --
    def _on_global_key(self, key):
        if key == keyboard.Key.esc:
            self.queue.put(("stop",))
        elif key == keyboard.Key.f9:
            self.queue.put(("play",))
        elif key == keyboard.Key.f8:
            self.queue.put(("add_pos",))

    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "click":
                    self._handle_recorded_click(*msg[1:])
                elif kind == "stop":
                    if self.recording:
                        self.stop_recording()
                    if self.playing:
                        self.stop()
                elif kind == "play":
                    self.play()
                elif kind == "add_pos":
                    if not self.playing:
                        self.add_current_position()
                elif kind == "status":
                    self.set_status(msg[1])
                elif kind == "progress":
                    self._show_progress(*msg[1:])
                elif kind == "play_done":
                    self._on_play_done(msg[1])
        except queue.Empty:
            pass
        self.root.after(30, self._poll_queue)

    # ----------------------------------------------------------- playback --
    def play(self):
        if self.playing or self.recording:
            return
        self._commit_editor()
        steps = [(i, dict(ev)) for i, ev in enumerate(self.events) if ev["enabled"]]
        if not steps:
            self.set_status("Nichts abzuspielen - der Ablauf hat keine aktiven Schritte.")
            return
        if any(ev["relative"] for _, ev in steps):
            if not self.program:
                self.set_status("Einige Schritte sind relativ zu einem Programmfenster - bitte zuerst ein Programm wählen.")
                return
            if not self.program_hwnd():
                self.set_status(f"Programmfenster nicht geöffnet: {describe_program(self.program)}")
                return
        settings = self.get_settings()
        self.playing = True
        self._play_error = None
        self.stop_event.clear()
        self.play_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.rec_single_btn.config(state="disabled")
        self.rec_multi_btn.config(state="disabled")
        self.pick_btn.config(state="disabled")
        if settings["minimize_on_play"]:
            self.root.iconify()
        program = dict(self.program) if self.program else None
        self.play_thread = threading.Thread(target=self._play_worker,
                                            args=(steps, settings, program, self._hwnd), daemon=True)
        self.play_thread.start()

    def stop(self):
        self.stop_event.set()

    def _play_worker(self, steps, settings, program, hwnd):
        ctrl = mouse.Controller()
        stop = self.stop_event
        loops_done = 0
        try:
            hwnd = find_window(program, hwnd)
            start = settings["start_delay"]
            while start > 0:
                self.queue.put(("status", f"Start in {start:.0f} s... (Esc zum Stoppen)"))
                if stop.wait(min(1.0, start)):
                    return
                start -= 1.0
            if hwnd and settings["bring_to_front"]:
                bring_to_front(hwnd)
                if stop.wait(0.3):
                    return
            repeat = settings["repeat"]
            while repeat == 0 or loops_done < repeat:
                for n, (idx, ev) in enumerate(steps, 1):
                    self.queue.put(("progress", idx, loops_done + 1, repeat, n, len(steps)))
                    if stop.wait(ev["delay"]):
                        return
                    x, y = ev["x"], ev["y"]
                    if ev["relative"]:
                        # Look the window up again before every step, so moving
                        # it during replay is fine too.
                        hwnd = find_window(program, hwnd)
                        if not hwnd:
                            self._play_error = "Gestoppt: Das Programmfenster wurde geschlossen."
                            return
                        if _user32.IsIconic(hwnd):
                            bring_to_front(hwnd)
                            if stop.wait(0.3):
                                return
                        x, y = rel_to_screen(hwnd, x, y)
                    if not self._move(ctrl, x, y, settings["move_duration"]):
                        return
                    if not self._clear_own_window(x, y, hwnd):
                        self._play_error = ("Gestoppt: Das Klicker-Fenster verdeckt die Klickposition "
                                            "('Beim Abspielen minimieren' einschalten).")
                        return
                    ctrl.click(BUTTONS[ev["button"]], ev["clicks"])
                loops_done += 1
        except Exception as exc:  # keep the GUI alive if something goes wrong
            self._play_error = f"Fehler beim Abspielen: {exc}"
        finally:
            self.queue.put(("play_done", loops_done))

    def _clear_own_window(self, x, y, hwnd):
        """Make sure a click at (x, y) won't land on the Clicker itself."""
        own_pid = os.getpid()
        for _ in range(2):
            under = root_window_at(x, y)
            if not under or window_pid(under) != own_pid:
                return True
            if not hwnd:
                return False
            bring_to_front(hwnd)
            time.sleep(0.2)
        return False

    def _move(self, ctrl, x, y, duration):
        """Glide the mouse to (x, y). Returns False if playback was stopped."""
        if duration <= 0:
            ctrl.position = (x, y)
            return not self.stop_event.is_set()
        sx, sy = ctrl.position
        steps = max(1, int(duration / 0.01))
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in/out
            ctrl.position = (round(sx + (x - sx) * t), round(sy + (y - sy) * t))
            if self.stop_event.wait(duration / steps):
                return False
        ctrl.position = (x, y)
        return True

    def _show_progress(self, idx, loop, repeat, n, total):
        if self._playing_iid and self.tree.exists(self._playing_iid):
            ev = self.events[int(self._playing_iid)] if int(self._playing_iid) < len(self.events) else None
            self.tree.item(self._playing_iid, tags=() if ev and ev["enabled"] else ("disabled",))
        iid = str(idx)
        if self.tree.exists(iid):
            self.tree.item(iid, tags=("playing",))
            self.tree.see(iid)
            self._playing_iid = iid
        loops = "∞" if repeat == 0 else str(repeat)
        self.set_status(f"Spiele Schritt {n}/{total}, Durchlauf {loop}/{loops}... (Esc zum Stoppen)")

    def _on_play_done(self, loops_done):
        self.playing = False
        self.play_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.rec_single_btn.config(state="normal")
        self.rec_multi_btn.config(state="normal")
        self.pick_btn.config(state="normal")
        sel = self.selected_indices()
        self._refresh(select=sel)
        if self.root.state() == "iconic":
            self.root.deiconify()
        self.root.lift()
        if self._play_error:
            self.set_status(f"{self._play_error} (abgeschlossene Durchläufe: {loops_done})")
        else:
            stopped = self.stop_event.is_set()
            self.set_status(f"{'Gestoppt' if stopped else 'Fertig'} - abgeschlossene Durchläufe: {loops_done}.")

    # -------------------------------------------------------------- close --
    def _on_close(self):
        self.stop_event.set()
        self.stop_recording()
        self._commit_editor()
        self._save()
        self.key_listener.stop()
        self.root.destroy()


def main():
    path = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FILE
    root = tk.Tk()
    ClickerApp(root, path)
    root.mainloop()


if __name__ == "__main__":
    main()
