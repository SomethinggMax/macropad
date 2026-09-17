"""Macropad window agent - runs on the Windows PC, lets the Pi see and focus windows.

The Pi is a USB keyboard and cannot see what is running on this machine, so this
agent answers those questions for it. Standard library + ctypes only.

    python windows-agent.py

Then in the macro IDE on the Pi, pick a target window or use:  focus <title>
"""

import base64
import ctypes
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8765
VERSION = 7  # bumped whenever endpoints change, so the Pi can warn if stale
DWMWA_CLOAKED = 14
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
ERROR_ACCESS_DENIED = 5
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
MONITORINFOF_PRIMARY = 1
CF_UNICODETEXT = 13
SRCCOPY = 0x00CC0020
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000
HOTKEY_MODS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
               "shift": MOD_SHIFT, "win": MOD_WIN, "gui": MOD_WIN,
               "cmd": MOD_WIN, "meta": MOD_WIN}
VIRTUAL_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B,
    "escape": 0x1B, "backspace": 0x08, "insert": 0x2D, "delete": 0x2E,
    "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "scrolllock": 0x91, "pause": 0x13, "numlock": 0x90,
}
VIRTUAL_KEYS.update({f"f{n}": 0x70 + n - 1 for n in range(1, 25)})
VIRTUAL_KEYS.update({chr(c): c for c in range(0x41, 0x5B)})          # A-Z
VIRTUAL_KEYS.update({chr(c).lower(): c for c in range(0x41, 0x5B)})  # a-z
VIRTUAL_KEYS.update({str(n): 0x30 + n for n in range(10)})           # 0-9
DIB_RGB_COLORS = 0
GMEM_MOVEABLE = 0x0002

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

# Explicit signatures matter on 64-bit: without them ctypes truncates HWNDs to
# 32 bits and every window handle silently becomes wrong.
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                            wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p,
                            wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD)]

ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


MONITORPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                 ctypes.POINTER(RECT), wintypes.LPARAM)

user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def screen_info():
    """Virtual desktop bounds and each monitor's rectangle, in Windows pixels.

    The virtual desktop origin is negative when a monitor sits left of or above
    the primary one, so the Pi needs it to translate coordinates: its mouse can
    only find the top-left corner of the whole desktop, not of one screen.
    """
    metric = user32.GetSystemMetrics
    virtual = {"x": metric(SM_XVIRTUALSCREEN), "y": metric(SM_YVIRTUALSCREEN),
               "width": metric(SM_CXVIRTUALSCREEN),
               "height": metric(SM_CYVIRTUALSCREEN)}

    screens = []

    def collect(handle, _hdc, _rect, _param):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            box = info.rcMonitor
            screens.append({
                "x": box.left, "y": box.top,
                "width": box.right - box.left, "height": box.bottom - box.top,
                "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY)})
        return True

    user32.EnumDisplayMonitors(None, None, MONITORPROC(collect), 0)
    screens.sort(key=lambda s: (s["x"], s["y"]))
    return {"virtual": virtual, "monitors": screens}


def _title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _cloaked(hwnd):
    """UWP apps keep hidden 'cloaked' windows that should not be listed."""
    value = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value),
                                 ctypes.sizeof(value))
    return bool(value.value)


def _process(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _class_name(hwnd):
    """Window class: usually stable and differs between an app's own windows,
    which makes it the best way to tell a game from its chat window."""
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _protected(hwnd):
    """True when this window's process cannot be opened for full query.

    That is what an elevated (or anti-cheat protected) process looks like from
    a normal-privilege agent, and it is why SetForegroundWindow on it fails.
    """
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    ctypes.set_last_error(0)
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid.value)
    if handle:
        kernel32.CloseHandle(handle)
        return False
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


def _rects(hwnd):
    """Window box and client box, both in screen coordinates.

    The client box excludes the title bar and borders, so a position inside an
    application should be measured from there. For a borderless window the two
    are identical.
    """
    box = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(box))
    window = {"x": box.left, "y": box.top,
              "width": box.right - box.left, "height": box.bottom - box.top}

    inner = RECT()
    user32.GetClientRect(hwnd, ctypes.byref(inner))
    origin = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    client = {"x": origin.x, "y": origin.y,
              "width": inner.right - inner.left, "height": inner.bottom - inner.top}
    return window, client


def list_windows():
    found = []

    def collect(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd):
            name = _title(hwnd)
            if name:
                window, client = _rects(hwnd)
                found.append({"id": int(hwnd), "title": name,
                              "exe": _process(hwnd), "class": _class_name(hwnd),
                              "z": len(found),  # EnumWindows order: topmost first
                              "area": window["width"] * window["height"],
                              "protected": _protected(hwnd),
                              "rect": window, "client": client})
        return True

    user32.EnumWindows(ENUMPROC(collect), 0)
    return found


def match_windows(target):
    """Every window matching a target, best match first.

    Targets can be a window id, a plain substring of the title or exe, a
    field-qualified form (`exe:`, `title:`, `class:`), and may end in `#2` to
    take the second match. Candidates are ordered topmost-first so `#1` is the
    most recently active one, which is what a person means by "the" window.
    """
    windows = list_windows()
    target = str(target).strip()

    index = 1
    if "#" in target:
        head, _, tail = target.rpartition("#")
        if tail.isdigit() and head.strip():
            target, index = head.strip(), int(tail)

    if target.isdigit():
        exact = [w for w in windows if w["id"] == int(target)]
        return exact, index

    field = None
    for name in ("exe", "title", "class"):
        if target.lower().startswith(name + ":"):
            field, target = name, target[len(name) + 1:].strip()
            break

    needle = target.lower()
    fields = [field] if field else ["title", "exe", "class"]

    ranked = []
    for rank, (key, exact_only) in enumerate(
            [(f, True) for f in fields] + [(f, False) for f in fields]):
        for win in windows:
            value = str(win.get(key, "")).lower()
            hit = value == needle if exact_only else needle in value
            if hit and not any(w["id"] == win["id"] for _, w in ranked):
                ranked.append((rank, win))
    # rank, then biggest window first, then topmost. A main window is almost
    # always larger than the app's own side windows.
    ranked.sort(key=lambda pair: (pair[0], -pair[1].get("area", 0), pair[1]["z"]))
    return [win for _, win in ranked], index


def find_window(target):
    matches, index = match_windows(target)
    if not matches or index > len(matches):
        return None
    return matches[index - 1]


def focus(hwnd):
    """Raise a window, working around Windows' foreground-stealing protection.

    Focus changes are applied asynchronously, so confirmation has to be polled;
    checking immediately reports failure for a switch that is about to succeed.
    """
    wanted = int(hwnd)
    hwnd = wintypes.HWND(wanted)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    foreground = user32.GetForegroundWindow()
    target_thread = user32.GetWindowThreadProcessId(foreground, None)
    this_thread = kernel32.GetCurrentThreadId()

    user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    attached = user32.AttachThreadInput(this_thread, target_thread, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, target_thread, False)

    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        current = int(user32.GetForegroundWindow() or 0)
        if current == wanted:
            return True
        time.sleep(0.03)

    # The app may have handed focus to one of its own windows (a game moving
    # focus to its chat box, say). That is a success for macro purposes, but
    # say which window actually holds it.
    current = int(user32.GetForegroundWindow() or 0)
    if current:
        ours, theirs = wintypes.DWORD(), wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(ours))
        user32.GetWindowThreadProcessId(wintypes.HWND(current), ctypes.byref(theirs))
        if ours.value and ours.value == theirs.value:
            return True
    return False


def foreground_window():
    """The window that currently has focus, or None."""
    handle = user32.GetForegroundWindow()
    if not handle:
        return None
    return {"id": int(handle), "title": _title(handle), "exe": _process(handle)}


def capture_region(x, y, width, height):
    """Grab a block of screen pixels as base64 RGB, one blit rather than
    width*height GetPixel calls (which would take seconds)."""
    screen = user32.GetDC(None)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        if not gdi32.BitBlt(memory, 0, 0, width, height, screen, x, y, SRCCOPY):
            return None
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative: rows top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        buffer = (ctypes.c_char * (width * height * 4))()
        if not gdi32.GetDIBits(memory, bitmap, 0, height, buffer,
                               ctypes.byref(info), DIB_RGB_COLORS):
            return None
        raw = bytes(buffer)
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(None, screen)

    rgb = bytearray(width * height * 3)
    for index in range(width * height):
        blue, green, red = raw[index * 4], raw[index * 4 + 1], raw[index * 4 + 2]
        rgb[index * 3] = red
        rgb[index * 3 + 1] = green
        rgb[index * 3 + 2] = blue
    return base64.b64encode(bytes(rgb)).decode()


def parse_hotkey(combo):
    """'ctrl+f11' -> (modifier flags, virtual key code)."""
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    flags = 0
    for part in parts[:-1]:
        if part not in HOTKEY_MODS:
            if part in VIRTUAL_KEYS:
                raise ValueError(
                    f"{part!r} is a key, not a modifier - write it last, "
                    f"e.g. ctrl+{part}")
            raise ValueError(
                f"unknown modifier {part!r} (use ctrl, alt, shift or win)")
        flags |= HOTKEY_MODS[part]
    key = VIRTUAL_KEYS.get(parts[-1])
    if key is None:
        raise ValueError(f"cannot use {parts[-1]!r} as a hotkey")
    # NOREPEAT so holding the combo fires once, not continuously
    return flags | MOD_NOREPEAT, key


class Hotkeys:
    """Registers global hotkeys and calls the Pi back when one is pressed.

    RegisterHotKey binds to the calling thread's message queue, so everything
    happens on one worker thread: configuration arrives through a queue and
    presses are read with PeekMessage.
    """

    def __init__(self):
        self.requests = queue.Queue()
        self.registered = []
        self.last_error = None
        self._bound = {}
        self._callback = None
        threading.Thread(target=self._run, daemon=True).start()

    def configure(self, hotkeys, callback):
        self.requests.put((list(hotkeys), callback))

    def _unbind_all(self):
        for hotkey_id in list(self._bound):
            user32.UnregisterHotKey(None, hotkey_id)
            del self._bound[hotkey_id]

    def _bind(self, hotkeys, callback):
        self._unbind_all()
        self._callback = callback
        self.registered, problems = [], []
        for index, item in enumerate(hotkeys, start=1):
            combo = item.get("combo", "")
            try:
                flags, key = parse_hotkey(combo)
            except ValueError as exc:
                problems.append(f"{combo}: {exc}")
                continue
            if not user32.RegisterHotKey(None, index, flags, key):
                problems.append(f"{combo}: already taken by another program")
                continue
            self._bound[index] = item
            self.registered.append({"combo": combo, "macro": item.get("macro")})
        self.last_error = "; ".join(problems) or None

    def _fire(self, item):
        if not self._callback:
            return
        # a panic hotkey goes straight to the stop endpoint, so it works no
        # matter which macro is running - or if none is
        if item.get("action") == "stop":
            target, payload = self._callback + "/stop", {}
        else:
            target = self._callback + "/run"
            payload = {"name": item.get("macro"), "delay": 0, "hotkey": True}
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            target, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10).read()
        except (urllib.error.URLError, OSError) as exc:
            print(f"  hotkey callback failed: {exc}")

    def _run(self):
        message = MSG()
        while True:
            try:
                while True:
                    hotkeys, callback = self.requests.get_nowait()
                    self._bind(hotkeys, callback)
            except queue.Empty:
                pass
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == WM_HOTKEY:
                    item = self._bound.get(int(message.wParam))
                    if item:
                        print(f"  hotkey {item.get('combo')} -> "
                              f"{item.get('action') or item.get('macro')}")
                        threading.Thread(target=self._fire, args=(item,),
                                         daemon=True).start()
            time.sleep(0.03)


HOTKEYS = Hotkeys()


def cursor_position():
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return {"x": point.x, "y": point.y}


def pixel_colour(x, y):
    """Colour at a screen point as #rrggbb (GetPixel returns BGR)."""
    dc = user32.GetDC(None)
    try:
        value = gdi32.GetPixel(dc, int(x), int(y))
    finally:
        user32.ReleaseDC(None, dc)
    if value == 0xFFFFFFFF:
        return None  # CLR_INVALID: point is not on any monitor
    return "#%02x%02x%02x" % (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def _open_clipboard(attempts=10):
    """Another app may hold the clipboard briefly; retry rather than fail."""
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard():
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.c_wchar_p(pointer).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard(text):
    buffer = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(pointer, buffer, size)
    kernel32.GlobalUnlock(handle)

    if not _open_clipboard():
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True  # the clipboard owns the memory now
    finally:
        user32.CloseClipboard()


class Handler(BaseHTTPRequestHandler):
    server_version = "MacropadAgent/1.0"

    def _send(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({"ok": True})

    def do_GET(self):
        if self.path.startswith("/window?") or self.path == "/window":
            query = parse_qs(urlparse(self.path).query)
            target = (query.get("target") or [""])[0]
            found = find_window(target) if target else None
            self._send({"ok": found is not None, "window": found,
                        "error": None if found else f"no window matching {target!r}"})
        elif self.path.startswith("/region"):
            query = parse_qs(urlparse(self.path).query)
            try:
                x = int(query["x"][0]); y = int(query["y"][0])
                width = max(1, min(int(query.get("w", ["32"])[0]), 128))
                height = max(1, min(int(query.get("h", ["32"])[0]), 128))
            except (KeyError, ValueError, IndexError):
                return self._send({"ok": False, "error": "need x and y"}, 400)
            data = capture_region(x, y, width, height)
            self._send({"ok": data is not None, "x": x, "y": y,
                        "w": width, "h": height, "rgb": data,
                        "error": None if data else "screen capture failed"})
        elif self.path.startswith("/foreground"):
            current = foreground_window()
            self._send({"ok": current is not None, "window": current})
        elif self.path.startswith("/cursor"):
            self._send({"ok": True, **cursor_position()})
        elif self.path.startswith("/clipboard"):
            text = get_clipboard()
            self._send({"ok": text is not None, "text": text or ""})
        elif self.path.startswith("/pixel"):
            query = parse_qs(urlparse(self.path).query)
            try:
                x = int(query.get("x", ["?"])[0]); y = int(query.get("y", ["?"])[0])
            except ValueError:
                return self._send({"ok": False, "error": "x and y must be numbers"}, 400)
            colour = pixel_colour(x, y)
            self._send({"ok": colour is not None, "colour": colour,
                        "error": None if colour else "point is not on any monitor"})
        elif self.path.startswith("/screen"):
            self._send({"ok": True, **screen_info()})
        elif self.path.startswith("/windows"):
            self._send({"ok": True, "windows": list_windows()})
        elif self.path.startswith("/ping"):
            self._send({"ok": True, "agent": "windows", "version": VERSION,
                        "hotkeys": HOTKEYS.registered})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send({"ok": False, "error": "bad json"}, 400)

        if self.path.startswith("/hotkeys"):
            port = int(data.get("port") or 8080)
            callback = f"http://{self.client_address[0]}:{port}/api"
            HOTKEYS.configure(data.get("hotkeys") or [], callback)
            time.sleep(0.2)  # let the worker apply them before reporting back
            return self._send({"ok": HOTKEYS.last_error is None,
                               "registered": HOTKEYS.registered,
                               "error": HOTKEYS.last_error})
        if self.path.startswith("/clipboard"):
            return self._send({"ok": set_clipboard(str(data.get("text", "")))})
        if self.path.startswith("/window?") or self.path == "/window":
            query = parse_qs(urlparse(self.path).query)
            target = (query.get("target") or [""])[0]
            found = find_window(target) if target else None
            self._send({"ok": found is not None, "window": found,
                        "error": None if found else f"no window matching {target!r}"})
        elif self.path.startswith("/region"):
            query = parse_qs(urlparse(self.path).query)
            try:
                x = int(query["x"][0]); y = int(query["y"][0])
                width = max(1, min(int(query.get("w", ["32"])[0]), 128))
                height = max(1, min(int(query.get("h", ["32"])[0]), 128))
            except (KeyError, ValueError, IndexError):
                return self._send({"ok": False, "error": "need x and y"}, 400)
            data = capture_region(x, y, width, height)
            self._send({"ok": data is not None, "x": x, "y": y,
                        "w": width, "h": height, "rgb": data,
                        "error": None if data else "screen capture failed"})
        elif self.path.startswith("/foreground"):
            current = foreground_window()
            self._send({"ok": current is not None, "window": current})
        elif self.path.startswith("/cursor"):
            try:
                user32.SetCursorPos(int(data["x"]), int(data["y"]))
            except (KeyError, ValueError, TypeError):
                return self._send({"ok": False, "error": "need x and y"}, 400)
            return self._send({"ok": True, **cursor_position()})
        if not self.path.startswith("/focus"):
            return self._send({"ok": False, "error": "not found"}, 404)

        target = data.get("target")
        if target in (None, ""):
            return self._send({"ok": False, "error": "no target given"}, 400)
        matches, index = match_windows(target)
        if not matches:
            return self._send({"ok": False,
                               "error": f"no window matching {target!r}"}, 404)
        if index > len(matches):
            return self._send({"ok": False, "error":
                               f"{target!r} has only {len(matches)} match(es)"}, 404)
        window = matches[index - 1]
        ok = focus(window["id"])
        problem = None
        if not ok:
            problem = "window did not come to the foreground"
            if window.get("protected"):
                problem += (" - that process is elevated or protected, so a "
                            "normal-privilege agent cannot focus it. Run this "
                            "agent as administrator, or click the window with "
                            "the HID mouse instead (a real click always works)")
        payload = {"ok": ok, "window": window, "error": problem}
        if len(matches) > 1:
            payload["ambiguous"] = [
                {"n": n + 1, "title": w["title"], "exe": w["exe"],
                 "class": w["class"], "id": w["id"],
                 "size": f"{w['rect']['width']}x{w['rect']['height']}"}
                for n, w in enumerate(matches)]
        self._send(payload)

    def log_message(self, fmt, *args):
        print("  " + fmt % args)


if __name__ == "__main__":
    # Without this, a scaled display reports virtualised pixels and every
    # coordinate the Pi is told would be wrong.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()

    info = screen_info()
    v = info["virtual"]
    print(f"Desktop {v['width']}x{v['height']} at ({v['x']},{v['y']}), "
          f"{len(info['monitors'])} monitor(s)")
    for m in info["monitors"]:
        print(f"  {m['width']}x{m['height']} at ({m['x']},{m['y']})"
              f"{' [primary]' if m['primary'] else ''}")
    print(f"Macropad agent listening on port {PORT}  "
          f"(windows, focus, screen, cursor, clipboard, pixel, region, hotkeys)")
    print(f"Found {len(list_windows())} windows. Leave this running.\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
