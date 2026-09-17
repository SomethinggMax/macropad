"""Macropad window agent - runs on the Windows PC, lets the Pi see and focus windows.

The Pi is a USB keyboard and cannot see what is running on this machine, so this
agent answers those questions for it. Standard library + ctypes only.

    python windows-agent.py

Then in the macro IDE on the Pi, pick a target window or use:  focus <title>
"""

import ctypes
import json
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8765
DWMWA_CLOAKED = 14
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
MONITORINFOF_PRIMARY = 1
CF_UNICODETEXT = 13
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


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]


MONITORPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                 ctypes.POINTER(RECT), wintypes.LPARAM)


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


def list_windows():
    found = []

    def collect(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd):
            name = _title(hwnd)
            if name:
                found.append({"id": int(hwnd), "title": name, "exe": _process(hwnd)})
        return True

    user32.EnumWindows(ENUMPROC(collect), 0)
    found.sort(key=lambda w: (w["exe"].lower(), w["title"].lower()))
    return found


def find_window(target):
    """Match by exact window id, else case-insensitive substring of title or exe."""
    windows = list_windows()
    if isinstance(target, int) or str(target).isdigit():
        for win in windows:
            if win["id"] == int(target):
                return win
        return None
    needle = str(target).lower()
    for key in ("title", "exe"):
        for win in windows:
            if needle in win[key].lower():
                return win
    return None


def focus(hwnd):
    """Raise a window, working around Windows' foreground-stealing protection."""
    hwnd = wintypes.HWND(int(hwnd))
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

    return int(user32.GetForegroundWindow() or 0) == int(hwnd.value)


def foreground_window():
    """The window that currently has focus, or None."""
    handle = user32.GetForegroundWindow()
    if not handle:
        return None
    return {"id": int(handle), "title": _title(handle), "exe": _process(handle)}


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
        if self.path.startswith("/foreground"):
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
            self._send({"ok": True, "agent": "windows", "version": 1})
        else:
            self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send({"ok": False, "error": "bad json"}, 400)

        if self.path.startswith("/clipboard"):
            return self._send({"ok": set_clipboard(str(data.get("text", "")))})
        if self.path.startswith("/foreground"):
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
        window = find_window(target)
        if window is None:
            return self._send({"ok": False, "error": f"no window matching {target!r}"}, 404)
        ok = focus(window["id"])
        self._send({"ok": ok, "window": window,
                    "error": None if ok else "window did not come to the foreground"})

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
          f"(windows, focus, screen, cursor, clipboard, pixel)")
    print(f"Found {len(list_windows())} windows. Leave this running.\n")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
