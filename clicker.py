"""Maus-Klicker - Maus- und Tastatur-Abläufe aufnehmen, bearbeiten und abspielen.

Ein Schritt ist entweder ein Mausklick an einer Position, ein Text, der
getippt wird, oder eine Tastenkombination (z. B. Strg+S).

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

import collections
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

# As a PyInstaller exe, __file__ points into a temporary folder that is deleted
# on exit, so keep sequence.json next to the exe instead.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(APP_DIR, "sequence.json")
FILE_VERSION = 3

BUTTONS = {
    "left": mouse.Button.left,
    "right": mouse.Button.right,
    "middle": mouse.Button.middle,
}

BUTTON_LABELS = {"left": "links", "right": "rechts", "middle": "Mitte"}
CLICK_LABELS = {"left": "Linksklick", "right": "Rechtsklick", "middle": "Mittelklick"}

STEP_TYPES = {"click": "Klick", "text": "Text", "key": "Taste"}
# Fields stored in the JSON for each step type (besides label/type/delay/enabled).
STEP_FIELDS = {
    "click": ("x", "y", "relative", "button", "clicks"),
    "text": ("text",),
    "key": ("keys",),
}

DEFAULT_SETTINGS = {
    "repeat": 1,              # 0 = repeat forever
    "start_delay": 3.0,       # seconds before replay starts
    "move_duration": 0.2,     # seconds for each mouse movement
    "default_delay": 0.5,     # wait before every step (unless custom waits are on)
    "use_custom_delays": False,  # True = every step waits its own "delay"
    "type_delay": 0.03,       # seconds between typed characters
    "record_timing": True,    # use real time between recorded clicks as delay
    "record_keyboard": True,  # record typing / key combos in "record multiple"
    "minimize_on_play": True,
    "abort_on_input": True,   # stop replay when the user moves the mouse / types
    "bring_to_front": True,   # activate the program window before replay
}

COLUMNS = (
    # id, heading, width, anchor
    ("idx", "#", 40, "center"),
    ("enabled", "An", 40, "center"),
    ("type", "Typ", 55, "center"),
    ("label", "Bezeichnung", 180, "w"),
    ("coords", "Koordinaten", 90, "center"),
    ("x", "X", 60, "center"),
    ("y", "Y", 60, "center"),
    ("action", "Aktion", 160, "w"),
    ("delay", "Eigene Wartezeit (s)", 135, "center"),
)


# --------------------------------------------------------------------------- #
# Keyboard helpers
# --------------------------------------------------------------------------- #
# Key combos are stored with pynput's key names ("ctrl+shift+s", "enter").
# German names are shown in the GUI and accepted when typing a combo.
MODIFIERS = ("ctrl", "alt", "shift", "cmd")
KEY_DISPLAY = {
    "ctrl": "Strg", "alt": "Alt", "shift": "Umschalt", "cmd": "Win",
    "enter": "Enter", "tab": "Tab", "esc": "Esc", "space": "Leertaste",
    "backspace": "Rücktaste", "delete": "Entf", "insert": "Einfg",
    "home": "Pos1", "end": "Ende", "page_up": "Bild↑", "page_down": "Bild↓",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "caps_lock": "Feststell", "print_screen": "Druck", "menu": "Menü", "plus": "Plus",
}
KEY_ALIASES = {label.lower(): name for name, label in KEY_DISPLAY.items()}
KEY_ALIASES.update({
    "control": "ctrl", "umschalt": "shift", "win": "cmd", "windows": "cmd",
    "return": "enter", "eingabe": "enter", "escape": "esc", "leer": "space",
    "del": "delete", "ins": "insert", "pgup": "page_up", "pgdn": "page_down",
    "bild auf": "page_up", "bild ab": "page_down",
})


def parse_keys(text):
    """'Strg+Umschalt+S' -> 'ctrl+shift+s'. Raises ValueError for unknown keys."""
    parts = [p.strip().lower() for p in str(text).split("+")]
    names = []
    for part in parts:
        if not part:
            raise ValueError("Leerer Tastenname (für die Plus-Taste 'Plus' schreiben)")
        name = KEY_ALIASES.get(part, part)
        if name not in keyboard.Key.__members__ and len(name) != 1 and name != "plus":
            raise ValueError(f"Unbekannte Taste: {part}")
        if name not in names:
            names.append(name)
    others = [n for n in names if n not in MODIFIERS]
    if len(others) > 1:
        raise ValueError("Außer Strg/Alt/Umschalt/Win ist nur eine Taste pro Kombination erlaubt")
    return "+".join([m for m in MODIFIERS if m in names] + others)


def format_keys(keys):
    """'ctrl+shift+s' -> 'Strg+Umschalt+S'"""
    def label(name):
        if name in KEY_DISPLAY:
            return KEY_DISPLAY[name]
        if len(name) == 1 or (name[0] == "f" and name[1:].isdigit()):
            return name.upper()
        return name
    return "+".join(label(n) for n in keys.split("+")) if keys else ""


def key_object(name):
    """pynput key for a stored key name."""
    if name in keyboard.Key.__members__:
        return keyboard.Key[name]
    char = "+" if name == "plus" else name
    if char.isascii() and char.isalnum():
        # Virtual key codes of A-Z / 0-9 equal their upper-case ASCII codes;
        # sending these (instead of characters) makes Ctrl+S etc. work.
        return keyboard.KeyCode.from_vk(ord(char.upper()))
    return keyboard.KeyCode.from_char(char)


def recorded_key_name(key):
    """Stored key name for a key event from the listener (or None)."""
    if isinstance(key, keyboard.Key):
        for mod in MODIFIERS:          # ctrl_l, shift_r, alt_gr, cmd_l, ...
            if key.name.startswith(mod):
                return mod
        return key.name
    vk = getattr(key, "vk", None)
    if vk and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39):
        return chr(vk).lower()
    char = getattr(key, "char", None)
    if char and char.isprintable():
        return "plus" if char == "+" else char
    return None


def step_delay(ev, settings):
    """Seconds to wait before a step."""
    return ev["delay"] if settings["use_custom_delays"] else settings["default_delay"]


def estimate_duration(events, settings):
    """Rough length of one round in seconds (without the start delay)."""
    total = 0.0
    for ev in events:
        if not ev["enabled"]:
            continue
        total += step_delay(ev, settings)
        if ev["type"] == "click":
            total += settings["move_duration"]
        elif ev["type"] == "text":
            total += len(ev["text"]) * settings["type_delay"]
    return total


def format_duration(seconds):
    """3.25 -> '3,3 s', 75 -> '1:15 min', 3725 -> '1:02:05 h'"""
    if seconds < 60:
        return f"{seconds:.1f} s".replace(".", ",")
    seconds = round(seconds)
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d} min"
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d} h"


def action_text(ev):
    """What a step does, as shown in the 'Aktion' column."""
    if ev["type"] == "click":
        return click_action(ev["button"], ev["clicks"])
    if ev["type"] == "text":
        return f"„{ev['text']}“"
    return format_keys(ev["keys"])


def click_action(button, clicks):
    return CLICK_LABELS[button] + (f" ×{clicks}" if clicks > 1 else "")


CLICK_ACTIONS = {click_action(b, n): (b, n) for b in BUTTONS for n in (1, 2, 3)}


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
        "type": "click",      # click, text or key
        "x": 0,
        "y": 0,
        "relative": False,    # True = relative to the program window
        "button": "left",
        "clicks": 1,
        "text": "",           # text steps: typed character by character
        "keys": "",           # key steps: e.g. "ctrl+s"
        "delay": DEFAULT_SETTINGS["default_delay"],
        "enabled": True,
    }
    for key, value in values.items():
        if key in event:
            event[key] = coerce_field(key, value)
    return event


def coerce_field(name, value):
    """Convert a value (often a string typed by the user) to the field's type."""
    if name in ("label", "text"):
        return str(value)
    if name == "type":
        if value not in STEP_TYPES:
            raise ValueError(f"Unbekannter Schritt-Typ: {value}")
        return value
    if name == "keys":
        return parse_keys(value) if str(value).strip() else ""
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
            raise ValueError(f"Maustaste muss eine von {', '.join(BUTTON_LABELS.values())} sein")
        return value
    if name in ("enabled", "relative"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "✓")
        return bool(value)
    raise KeyError(name)


def compact_event(ev):
    """Only the fields that matter for the step's type, for a readable JSON."""
    out = {"label": ev["label"], "type": ev["type"]}
    out.update({k: ev[k] for k in STEP_FIELDS[ev["type"]]})
    out.update(delay=ev["delay"], enabled=ev["enabled"])
    return out


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
    data = {"version": FILE_VERSION, "settings": settings, "program": program,
            "events": [compact_event(ev) for ev in events]}
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
        self.event = event
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self._countdown_job = None

        self.vars = {
            "label": tk.StringVar(value=event["label"]),
            "type": tk.StringVar(value=event["type"]),
            "x": tk.StringVar(value=str(event["x"])),
            "y": tk.StringVar(value=str(event["y"])),
            "relative": tk.BooleanVar(value=event["relative"]),
            "button": tk.StringVar(value=BUTTON_LABELS[event["button"]]),
            "clicks": tk.StringVar(value=str(event["clicks"])),
            "text": tk.StringVar(value=event["text"]),
            "keys": tk.StringVar(value=format_keys(event["keys"])),
            "delay": tk.StringVar(value=str(event["delay"])),
            "enabled": tk.BooleanVar(value=event["enabled"]),
        }

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        def row(parent, r, text, widget):
            ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, columnspan=2, sticky="ew", pady=3)

        label_entry = ttk.Entry(body, textvariable=self.vars["label"], width=36)
        row(body, 0, "Bezeichnung", label_entry)

        types = ttk.Frame(body)
        for value, text in (("click", "Mausklick"), ("text", "Text tippen"), ("key", "Tastenkombination")):
            ttk.Radiobutton(types, text=text, value=value, variable=self.vars["type"],
                            command=self._show_type).pack(side="left", padx=(0, 10))
        row(body, 1, "Typ", types)

        # Mouse click
        self.frames = {"click": ttk.Frame(body), "text": ttk.Frame(body), "key": ttk.Frame(body)}
        click = self.frames["click"]
        ttk.Label(click, text="Position").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 8))
        pos = ttk.Frame(click)
        pos.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(pos, text="X").pack(side="left")
        ttk.Entry(pos, textvariable=self.vars["x"], width=7).pack(side="left", padx=(2, 8))
        ttk.Label(pos, text="Y").pack(side="left")
        ttk.Entry(pos, textvariable=self.vars["y"], width=7).pack(side="left", padx=(2, 0))

        rel_check = ttk.Checkbutton(click, text="Relativ zum Programmfenster",
                                    variable=self.vars["relative"], command=self._toggle_relative)
        rel_check.grid(row=1, column=1, columnspan=2, sticky="w")
        if not app.program and not event["relative"]:
            rel_check.state(["disabled"])

        pick = ttk.Frame(click)
        pick.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(2, 6))
        self.pick_btn = ttk.Button(pick, text="Mausposition in 3 s übernehmen", command=self._start_capture)
        self.pick_btn.pack(side="left")
        ttk.Button(pick, text="Position zeigen", command=self._show_position).pack(side="left", padx=6)

        row(click, 3, "Maustaste", ttk.Combobox(click, textvariable=self.vars["button"],
                                                values=list(BUTTON_LABELS.values()), state="readonly", width=10))
        row(click, 4, "Klicks", ttk.Spinbox(click, textvariable=self.vars["clicks"], from_=1, to=3, width=10))

        # Typed text
        text = self.frames["text"]
        self.text_entry = ttk.Entry(text, textvariable=self.vars["text"], width=36)
        row(text, 0, "Text", self.text_entry)
        ttk.Label(text, text="Wird Zeichen für Zeichen in das aktive Fenster getippt.",
                  foreground="#666").grid(row=1, column=1, sticky="w")

        # Key combination
        key = self.frames["key"]
        self.key_entry = ttk.Entry(key, textvariable=self.vars["keys"], width=36)
        row(key, 0, "Tasten", self.key_entry)
        ttk.Label(key, text="Mit + verbinden, z. B. Enter, Tab, F5, Strg+S,\n"
                            "Alt+F4, Umschalt+Tab, Strg+Umschalt+Esc, Win+D",
                  foreground="#666", justify="left").grid(row=1, column=1, sticky="w")

        for frame in self.frames.values():
            frame.grid(row=2, column=0, columnspan=3, sticky="ew")
            frame.columnconfigure(0, minsize=110)

        row(body, 3, "Warten vorher (s)", ttk.Spinbox(body, textvariable=self.vars["delay"],
                                                     from_=0, to=3600, increment=0.1, width=10))
        ttk.Checkbutton(body, text="Aktiv", variable=self.vars["enabled"]).grid(
            row=4, column=1, sticky="w", pady=3)
        body.columnconfigure(0, minsize=110)

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="left", padx=4)
        ttk.Button(buttons, text="Abbrechen", command=self._cancel).pack(side="left")

        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._show_type(focus=False)
        focus = {"text": self.text_entry, "key": self.key_entry}.get(event["type"], label_entry)
        focus.focus_set()
        focus.select_range(0, "end")
        self.update_idletasks()
        self.geometry(f"+{parent.winfo_rootx() + 60}+{parent.winfo_rooty() + 60}")
        self.grab_set()
        self.wait_window(self)

    def _show_type(self, focus=True):
        current = self.vars["type"].get()
        for name, frame in self.frames.items():
            if name == current:
                frame.grid()
            else:
                frame.grid_remove()
        if focus and current in ("text", "key"):
            (self.text_entry if current == "text" else self.key_entry).focus_set()

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
        values = {k: v.get() for k, v in self.vars.items()}
        step_type = values["type"]
        # Keep the fields of the other step types as they were, so switching
        # the type back and forth doesn't lose anything.
        for other, fields in STEP_FIELDS.items():
            if other != step_type:
                for field in fields:
                    values[field] = self.event[field]
        try:
            if step_type == "key" and not values["keys"].strip():
                raise ValueError("Bitte mindestens eine Taste angeben.")
            if step_type == "text" and not values["text"]:
                raise ValueError("Bitte einen Text angeben.")
            self.result = make_event(**values)
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
        self._rec_keyboard = False      # copy of the setting for the listener thread
        self._rec_text = None           # text step that typed characters are added to
        self._held = set()              # modifier keys currently held (listener thread)

        self.playing = False
        self.stop_event = threading.Event()
        self.play_thread = None
        self._play_error = None
        self._playing_iid = None
        self._guard_armed = False       # replay is running and watching for user input
        self._guard_listener = None
        self._abort_reason = None       # what user input aborted the replay
        self._own_moves = collections.deque(maxlen=64)   # positions the replay moved to

        self._editor = None             # inline cell editor widget
        self._drag_from = None
        self._dragged = False
        self._loading = False

        self._build_ui()
        self._load(path, create_if_missing=True)

        self.key_listener = keyboard.Listener(on_press=self._on_global_key,
                                              on_release=self._on_global_key_release)
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
        root.geometry(f"{round(1060 * s)}x{round(800 * s)}")
        root.minsize(round(920 * s), round(680 * s))

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
        rec_box = ttk.LabelFrame(root, text="Aufnahme", padding=8)
        rec_box.pack(fill="x", padx=8, pady=(8, 0))
        rec = ttk.Frame(rec_box)
        rec.pack(fill="x")
        self.rec_single_btn = ttk.Button(rec, text="● Klick aufnehmen", command=self.record_single)
        self.rec_single_btn.pack(side="left")
        self.rec_multi_btn = ttk.Button(rec, text="●● Mehrere aufnehmen", command=self.toggle_record_multi)
        self.rec_multi_btn.pack(side="left", padx=4)

        self.v_record_timing = tk.BooleanVar(value=DEFAULT_SETTINGS["record_timing"])
        ttk.Checkbutton(rec, text="Zeit zwischen Schritten aufnehmen",
                        variable=self.v_record_timing).pack(side="left", padx=(12, 0))
        self.v_record_keyboard = tk.BooleanVar(value=DEFAULT_SETTINGS["record_keyboard"])
        ttk.Checkbutton(rec, text="Tastatur mitaufnehmen",
                        variable=self.v_record_keyboard).pack(side="left", padx=(12, 0))

        add = ttk.Frame(rec_box)
        add.pack(fill="x", pady=(6, 0))
        ttk.Button(add, text="+ Mausposition (F8)", command=self.add_current_position).pack(side="left")
        ttk.Button(add, text="+ Klick...", command=self.add_manual).pack(side="left", padx=4)
        ttk.Button(add, text="+ Text...", command=lambda: self.add_manual("text")).pack(side="left")
        ttk.Button(add, text="+ Taste...", command=lambda: self.add_manual("key")).pack(side="left", padx=4)

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
            ("Wartezeit = Standard", self.delays_to_standard),
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
        play_box = ttk.LabelFrame(root, text="Abspielen", padding=8)
        play_box.pack(fill="x", padx=8)
        play = ttk.Frame(play_box)
        play.pack(fill="x")
        waits = ttk.Frame(play_box)
        waits.pack(fill="x", pady=(6, 0))

        self.v_repeat = tk.StringVar(value=str(DEFAULT_SETTINGS["repeat"]))
        self.v_start_delay = tk.StringVar(value=str(DEFAULT_SETTINGS["start_delay"]))
        self.v_move_duration = tk.StringVar(value=str(DEFAULT_SETTINGS["move_duration"]))
        self.v_default_delay = tk.StringVar(value=str(DEFAULT_SETTINGS["default_delay"]))
        self.v_type_delay = tk.StringVar(value=str(DEFAULT_SETTINGS["type_delay"]))
        self.v_custom_delays = tk.BooleanVar(value=DEFAULT_SETTINGS["use_custom_delays"])
        self.v_minimize = tk.BooleanVar(value=DEFAULT_SETTINGS["minimize_on_play"])
        self.v_abort_on_input = tk.BooleanVar(value=DEFAULT_SETTINGS["abort_on_input"])

        def spin(parent, text, var, frm, to, inc, width=6):
            ttk.Label(parent, text=text).pack(side="left")
            ttk.Spinbox(parent, textvariable=var, from_=frm, to=to, increment=inc,
                        width=width).pack(side="left", padx=(2, 10))

        spin(play, "Wiederholen (0 = ∞)", self.v_repeat, 0, 100000, 1)
        spin(play, "Startverzögerung (s)", self.v_start_delay, 0, 60, 0.5)
        spin(play, "Bewegungszeit (s)", self.v_move_duration, 0, 5, 0.05)
        spin(play, "Tippverzögerung (s)", self.v_type_delay, 0, 2, 0.01)
        ttk.Checkbutton(play, text="Beim Abspielen minimieren", variable=self.v_minimize).pack(side="left")
        ttk.Checkbutton(play, text="Bei Eingabe abbrechen",
                        variable=self.v_abort_on_input).pack(side="left", padx=(10, 0))

        spin(waits, "Standard-Wartezeit (s)", self.v_default_delay, 0, 60, 0.1)
        ttk.Checkbutton(waits, text="Eigene Wartezeiten der Schritte verwenden "
                                    "(aus = jeder Schritt wartet die Standard-Wartezeit)",
                        variable=self.v_custom_delays).pack(side="left")

        bottom = ttk.Frame(root, padding=8)
        bottom.pack(fill="x")
        self.play_btn = ttk.Button(bottom, text="▶ Abspielen (F9)", style="Accent.TButton", command=self.play)
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(bottom, text="■ Stopp (Esc)", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.count_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.count_var, foreground="#555").pack(side="right")
        self.status_var = tk.StringVar(value="Bereit.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=10)

        for var in (self.v_repeat, self.v_start_delay, self.v_move_duration, self.v_default_delay,
                    self.v_type_delay, self.v_minimize, self.v_abort_on_input, self.v_record_timing,
                    self.v_record_keyboard, self.v_bring_to_front):
            var.trace_add("write", lambda *_: self._settings_changed())
        # Switching custom waits on/off changes how the wait column is shown.
        self.v_custom_delays.trace_add("write", lambda *_: self._settings_changed(refresh=True))

    def set_status(self, text):
        self.status_var.set(text)

    def _settings_changed(self, refresh=False):
        if self._loading:
            return
        if refresh:
            self._refresh(select=self.selected_indices())
        else:
            self._update_summary()
        self._save()

    def _update_summary(self):
        """Step count and expected duration next to the play button."""
        n = len(self.events)
        settings = self.get_settings()
        one_round = estimate_duration(self.events, settings)
        text = f"{n} Schritt{'e' if n != 1 else ''}  ·  Dauer ≈ {format_duration(one_round)}"
        repeat = settings["repeat"]
        if repeat == 0:
            text += " pro Durchlauf (endlos)"
        elif repeat > 1:
            text += f" × {repeat} = {format_duration(one_round * repeat)}"
        self.count_var.set(text)

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
            "type_delay": num(self.v_type_delay, float, "type_delay"),
            "use_custom_delays": bool(self.v_custom_delays.get()),
            "record_timing": bool(self.v_record_timing.get()),
            "record_keyboard": bool(self.v_record_keyboard.get()),
            "minimize_on_play": bool(self.v_minimize.get()),
            "abort_on_input": bool(self.v_abort_on_input.get()),
            "bring_to_front": bool(self.v_bring_to_front.get()),
        }

    def _apply_settings(self, s):
        self._loading = True
        try:
            self.v_repeat.set(str(s["repeat"]))
            self.v_start_delay.set(str(s["start_delay"]))
            self.v_move_duration.set(str(s["move_duration"]))
            self.v_default_delay.set(str(s["default_delay"]))
            self.v_type_delay.set(str(s["type_delay"]))
            self.v_custom_delays.set(bool(s["use_custom_delays"]))
            self.v_record_timing.set(bool(s["record_timing"]))
            self.v_record_keyboard.set(bool(s["record_keyboard"]))
            self.v_minimize.set(bool(s["minimize_on_play"]))
            self.v_abort_on_input.set(bool(s["abort_on_input"]))
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
        screen_steps = [i for i, ev in enumerate(self.events)
                        if ev["type"] == "click" and not ev["relative"]]
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
            if ev["type"] != "click" or ev["relative"] == to_relative:
                continue
            convert = screen_to_rel if to_relative else rel_to_screen
            ev["x"], ev["y"] = convert(hwnd, ev["x"], ev["y"])
            ev["relative"] = to_relative
        self._changed(select=indices)
        return True

    def toggle_relative_selected(self):
        sel = [i for i in self.selected_indices() if self.events[i]["type"] == "click"]
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
        # Custom waits that are switched off are shown in brackets (not used).
        delay_fmt = "{:.2f}" if self.v_custom_delays.get() else "({:.2f})"
        for i, ev in enumerate(self.events):
            if ev["type"] == "click":
                where = ("Fenster" if ev["relative"] else "Bildschirm", ev["x"], ev["y"])
            else:
                where = ("", "", "")
            self.tree.insert("", "end", iid=str(i), tags=() if ev["enabled"] else ("disabled",), values=(
                i + 1, "✓" if ev["enabled"] else "", STEP_TYPES[ev["type"]], ev["label"],
                *where, action_text(ev), delay_fmt.format(ev["delay"])))
        if select:
            iids = [str(i) for i in select if 0 <= i < len(self.events)]
            if iids:
                self.tree.selection_set(iids)
                self.tree.focus(iids[-1])
                self.tree.see(iids[-1])
        self._update_summary()

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

    def add_manual(self, step_type="click"):
        sx, sy = mouse.Controller().position
        x, y, relative = self._point_for_step(sx, sy, root_window_at(sx, sy))
        dlg = EventDialog(self, make_event(label=self._next_label(), type=step_type,
                                           x=x, y=y, relative=relative,
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

    def delays_to_standard(self):
        """Set the custom wait of the selected steps (or all) to the standard wait."""
        sel = self.selected_indices()
        if not sel:
            if not self.events or not messagebox.askyesno(
                    "Wartezeiten zurücksetzen",
                    "Kein Schritt markiert. Die eigene Wartezeit aller Schritte auf die "
                    "Standard-Wartezeit setzen?"):
                return
            sel = list(range(len(self.events)))
        std = self.get_settings()["default_delay"]
        for i in sel:
            self.events[i]["delay"] = std
        self._changed(select=sel)
        self.set_status(f"Wartezeit von {len(sel)} Schritt(en) auf {std:.2f} s gesetzt.")

    def clear_all(self):
        if self.events and messagebox.askyesno("Alles löschen", "Alle Schritte dieses Ablaufs löschen?"):
            self.events.clear()
            self._changed()

    def show_selected_position(self):
        sel = self.selected_indices()
        if not sel:
            return
        ev = self.events[sel[0]]
        if ev["type"] != "click":
            return
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
        is_click = self.events[idx]["type"] == "click"
        if name == "enabled":
            self.events[idx]["enabled"] = not self.events[idx]["enabled"]
            self._changed(select=[idx])
        elif name == "coords" and is_click:
            self.tree.selection_set(iid)
            self.toggle_relative_selected()
        elif name in ("idx", "type") or (name in ("coords", "x", "y") and not is_click):
            self.tree.selection_set(iid)
            self.edit_selected()
        else:
            self._start_inline_edit(idx, col, name)
        return "break"

    def _edit_selected_label(self):
        sel = self.selected_indices()
        if sel:
            self._start_inline_edit(sel[0], "label", "label")

    def _start_inline_edit(self, idx, col, name):
        self._commit_editor()
        iid = str(idx)
        self.tree.see(iid)
        self.tree.update_idletasks()
        bbox = self.tree.bbox(iid, col)
        if not bbox:
            return
        x, y, w, h = bbox
        ev = self.events[idx]
        if name != "action":
            current = ev[name]
        elif ev["type"] == "text":
            current = ev["text"]
        else:
            current = format_keys(ev["keys"])

        if name == "action" and ev["type"] == "click":
            widget = ttk.Combobox(self.tree, values=list(CLICK_ACTIONS), state="readonly")
            widget.set(click_action(ev["button"], ev["clicks"]))
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
            self._apply_cell(self.events[idx], name, value)
        except (ValueError, IndexError, KeyError) as exc:
            self.set_status(f"Ungültiger Wert ({name}): {exc}")
            return
        self._changed(select=[idx])
        self.tree.focus_set()
        if name == "delay" and not self.v_custom_delays.get():
            self.set_status("Hinweis: Eigene Wartezeiten sind aus - eingeschaltet unter Abspielen.")

    @staticmethod
    def _apply_cell(ev, name, value):
        if name != "action":
            ev[name] = coerce_field(name, value)
        elif ev["type"] == "click":
            ev["button"], ev["clicks"] = CLICK_ACTIONS[value]
        elif ev["type"] == "text":
            if not value:
                raise ValueError("Text darf nicht leer sein")
            ev["text"] = value
        else:
            if not value.strip():
                raise ValueError("Keine Taste angegeben")
            ev["keys"] = parse_keys(value)

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
        what = "Klicks und Tastatur" if self._rec_keyboard else "Klicks"
        self.set_status(f"{what} werden aufgenommen... Esc oder 'Aufnahme stoppen' zum Beenden.")

    def _start_recording(self, mode):
        self.recording = mode
        self.last_click_time = None
        self._rec_text = None
        self._rec_keyboard = mode == "multi" and self.get_settings()["record_keyboard"]
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

    def _on_global_click(self, x, y, button, pressed, injected=False):
        # Runs in the listener thread. The window under the click is looked up
        # right away (before it can close), then everything is handed over to
        # the Tk thread.
        if pressed and not injected:
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
        delay = self._recorded_delay(t)
        x, y, relative = self._point_for_step(sx, sy, root_hwnd)

        # A fast second click at the same spot becomes a double-click.
        sel = self.selected_indices()
        if sel and self.recording == "multi" and delay < 0.4:
            prev = self.events[sel[-1]]
            if (prev["type"] == "click" and prev["button"] == name and prev["clicks"] < 3 and prev["relative"] == relative
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

    def _recorded_delay(self, t):
        """Wait time for a newly recorded step at time t."""
        settings = self.get_settings()
        if settings["record_timing"] and self.last_click_time is not None:
            delay = t - self.last_click_time
        else:
            delay = settings["default_delay"]
        self.last_click_time = t
        return delay

    def _handle_recorded_key(self, name, char, held, t):
        if self.recording != "multi":
            return
        command_mods = held & {"ctrl", "alt", "cmd"}
        altgr = {"ctrl", "alt"} <= held      # AltGr arrives as Ctrl+Alt (e.g. @, €, {)
        if name == "space" and not command_mods:
            char = " "
        is_char = bool(char) and char.isprintable() and (not command_mods or altgr)

        # Typed characters (and Backspace) extend the text step being recorded.
        sel = self.selected_indices()
        text_ev = self._rec_text if sel and self.events[sel[-1]] is self._rec_text else None
        if text_ev and (is_char or (name == "backspace" and not held)):
            self.last_click_time = t
            if is_char:
                text_ev["text"] += char
            else:
                text_ev["text"] = text_ev["text"][:-1]
                if not text_ev["text"]:
                    del self.events[sel[-1]]
                    self._rec_text = None
                    self._changed(select=[sel[-1] - 1])
                    return
            self._changed(select=[sel[-1]])
            self.set_status(f"Text: {action_text(text_ev)}")
            return

        delay = self._recorded_delay(t)
        if is_char:
            ev = make_event(label=self._next_label(), type="text", text=char, delay=delay)
            self.insert_event(ev)
            self._rec_text = ev
            self.set_status(f"Text: {action_text(ev)}")
            return
        keys = "+".join([m for m in MODIFIERS if m in held] + [name])
        idx = self.insert_event(make_event(label=self._next_label(), type="key", keys=keys, delay=delay))
        self.set_status(f"Schritt {idx + 1} aufgenommen: Taste {format_keys(keys)}.")

    # ------------------------------------------------------------ hotkeys --
    def _on_global_key(self, key, injected=False):
        # Runs in the listener thread. Keys sent by the replay are ignored.
        if injected or key is None:
            return
        name = recorded_key_name(key)
        if self._guard_armed and key != keyboard.Key.esc:
            self._user_input(f"Tastendruck ({format_keys(name) if name else 'Taste'})")
        if name in MODIFIERS:
            self._held.add(name)
        elif key == keyboard.Key.esc:
            self.queue.put(("stop",))
        elif key == keyboard.Key.f9:
            self.queue.put(("play",))
        elif key == keyboard.Key.f8:
            self.queue.put(("add_pos",))
        elif (name and self.recording == "multi" and self._rec_keyboard
              and window_pid(_user32.GetForegroundWindow()) != os.getpid()):
            self.queue.put(("key", name, getattr(key, "char", None), frozenset(self._held),
                            time.perf_counter()))

    def _on_global_key_release(self, key, injected=False):
        if not injected and key is not None:
            self._held.discard(recorded_key_name(key))

    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "click":
                    self._handle_recorded_click(*msg[1:])
                elif kind == "key":
                    self._handle_recorded_key(*msg[1:])
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
        self._abort_reason = None
        self._guard_armed = False
        self._own_moves.clear()
        if settings["abort_on_input"]:
            self._guard_listener = mouse.Listener(on_move=self._guard_move, on_click=self._guard_click,
                                                  on_scroll=self._guard_scroll)
            self._guard_listener.daemon = True
            self._guard_listener.start()
        program = dict(self.program) if self.program else None
        self.play_thread = threading.Thread(target=self._play_worker,
                                            args=(steps, settings, program, self._hwnd), daemon=True)
        self.play_thread.start()

    def stop(self):
        self.stop_event.set()

    # The guard runs in listener threads while the replay is armed (after the
    # start countdown, so you can still switch windows then). Input sent by
    # the replay itself is flagged "injected" by Windows; cursor positions the
    # replay set are remembered too, in case a move event isn't flagged.
    def _user_input(self, reason):
        if self._guard_armed and not self.stop_event.is_set():
            self._abort_reason = reason
            self.stop_event.set()

    def _guard_move(self, x, y, injected=False):
        if not injected and self._guard_armed and not any(
                abs(x - px) <= 2 and abs(y - py) <= 2 for px, py in list(self._own_moves)):
            self._user_input(f"Mausbewegung bei ({int(x)}, {int(y)})")

    def _guard_click(self, x, y, button, pressed, injected=False):
        if not injected and pressed:
            self._user_input(f"Mausklick ({CLICK_LABELS.get(button.name, button.name)})")

    def _guard_scroll(self, x, y, dx, dy, injected=False):
        if not injected:
            self._user_input("Mausrad")

    def _set_cursor(self, ctrl, x, y):
        self._own_moves.append((x, y))
        ctrl.position = (x, y)
        # Windows may clamp the position (e.g. at the screen edge).
        self._own_moves.append(tuple(int(v) for v in ctrl.position))

    def _guard_poll(self):
        """Second check besides the listener: every 50 ms, compare where the
        cursor is with where the replay put it."""
        ctrl = mouse.Controller()
        while self._guard_armed and not self.stop_event.wait(0.05):
            x, y = ctrl.position
            self._guard_move(x, y)

    def _play_worker(self, steps, settings, program, hwnd):
        ctrl = mouse.Controller()
        kb = keyboard.Controller()
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
            self._own_moves.append(tuple(int(v) for v in ctrl.position))
            self._guard_armed = bool(self._guard_listener)
            if self._guard_armed:
                threading.Thread(target=self._guard_poll, daemon=True).start()
            repeat = settings["repeat"]
            while repeat == 0 or loops_done < repeat:
                for n, (idx, ev) in enumerate(steps, 1):
                    self.queue.put(("progress", idx, loops_done + 1, repeat, n, len(steps)))
                    if stop.wait(step_delay(ev, settings)):
                        return
                    if ev["type"] != "click":
                        if program:
                            hwnd = find_window(program, hwnd)
                            if not hwnd:
                                self._play_error = "Gestoppt: Das Programmfenster wurde geschlossen."
                                return
                        if not self._keyboard_target_ok(program, hwnd):
                            self._play_error = (
                                "Gestoppt: Das Programmfenster ließ sich nicht in den Vordergrund holen."
                                if program else
                                "Gestoppt: Die Tastatureingabe würde im Klicker landen "
                                "(Programm wählen oder 'Beim Abspielen minimieren' einschalten).")
                            return
                        if ev["type"] == "text":
                            if not self._type_text(kb, ev["text"], settings["type_delay"]):
                                return
                        else:
                            self._press_keys(kb, ev["keys"])
                        continue
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

    def _keyboard_target_ok(self, program, hwnd):
        """Make sure key presses go to the program (or at least not to the Clicker)."""
        for _ in range(2):
            fg = _user32.GetForegroundWindow()
            if fg and window_pid(fg) != os.getpid() and (not program or belongs_to_program(fg, program)):
                return True
            if not hwnd:
                return False
            bring_to_front(hwnd)
            time.sleep(0.2)
        return False

    def _type_text(self, kb, text, delay):
        """Type text character by character. Returns False if playback was stopped."""
        for char in text:
            if self.stop_event.is_set():
                return False
            kb.type(char)
            if delay > 0 and self.stop_event.wait(delay):
                return False
        return True

    @staticmethod
    def _press_keys(kb, keys):
        """Press a combo like 'ctrl+shift+s': modifiers down, key, all up again."""
        if not keys:
            return
        pressed = []
        try:
            for name in keys.split("+"):
                key = key_object(name)
                kb.press(key)
                pressed.append(key)
        finally:
            for key in reversed(pressed):
                kb.release(key)

    def _move(self, ctrl, x, y, duration):
        """Glide the mouse to (x, y). Returns False if playback was stopped."""
        if duration <= 0:
            self._set_cursor(ctrl, x, y)
            return not self.stop_event.is_set()
        sx, sy = ctrl.position
        steps = max(1, int(duration / 0.01))
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in/out
            self._set_cursor(ctrl, round(sx + (x - sx) * t), round(sy + (y - sy) * t))
            if self.stop_event.wait(duration / steps):
                return False
        self._set_cursor(ctrl, x, y)
        return not self.stop_event.is_set()

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
        self._guard_armed = False
        if self._guard_listener:
            self._guard_listener.stop()
            self._guard_listener = None
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
        if self._abort_reason:
            msg = (f"Abgebrochen: Benutzereingabe erkannt - {self._abort_reason}. "
                   f"Abgeschlossene Durchläufe: {loops_done}.")
            self.set_status(msg)
            print(f"[Maus-Klicker] {msg}", flush=True)
            self._show_abort_message(msg)
        elif self._play_error:
            self.set_status(f"{self._play_error} (abgeschlossene Durchläufe: {loops_done})")
        else:
            stopped = self.stop_event.is_set()
            self.set_status(f"{'Gestoppt' if stopped else 'Fertig'} - abgeschlossene Durchläufe: {loops_done}.")

    def _show_abort_message(self, msg):
        messagebox.showwarning(
            "Abspielen abgebrochen",
            f"{msg}\n\nDas Abspielen wurde zur Sicherheit gestoppt, weil Maus oder Tastatur "
            "benutzt wurden. Abschalten unter 'Abspielen' → 'Bei Eingabe abbrechen'.",
            parent=self.root)

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
