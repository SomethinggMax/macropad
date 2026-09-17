"""Import the Windows agent on any OS, with the Win32 layer stubbed.

The agent can only really run on Windows, but most of what breaks it is
import-time: a structure referenced before it is defined, a typo in a field
list, a bad argtypes entry. Those fail identically everywhere, so stub the
DLLs and import it. Catches the mistakes worth catching without a Windows box.
"""

import ctypes
import importlib.util
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "agent" / "windows-agent.py"


class FakeFunc:
    """Accepts argtypes/restype assignments and any call."""

    def __init__(self, name):
        self.name = name
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):
        return 0


class FakeDLL:
    def __init__(self, name, *args, **kwargs):
        self._name = name
        self._funcs = {}

    def __getattr__(self, item):
        return self._funcs.setdefault(item, FakeFunc(item))


def main():
    ctypes.WinDLL = FakeDLL
    ctypes.windll = FakeDLL("windll")
    if not hasattr(ctypes, "WINFUNCTYPE"):
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE

    spec = importlib.util.spec_from_file_location("winagent", AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # __name__ is not __main__, so no server starts

    required = ["list_windows", "find_window", "focus", "screen_info",
                "foreground_window", "cursor_position", "pixel_colour",
                "capture_region", "get_clipboard", "set_clipboard", "Handler"]
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        print(f"  FAIL: missing {', '.join(missing)}")
        return 1

    for structure in ["RECT", "MONITORINFO", "BITMAPINFOHEADER", "BITMAPINFO"]:
        ctypes.sizeof(getattr(module, structure))  # raises if a field list is bad

    routes = [r for r in ("/windows", "/focus", "/screen", "/cursor", "/clipboard",
                          "/pixel", "/region", "/foreground", "/window", "/ping")
              if r in Path(AGENT).read_text()]
    print(f"  imports cleanly, version {module.VERSION}")
    print(f"  {len(required)} required symbols present")
    print(f"  structures size-check OK")
    print(f"  endpoints referenced: {', '.join(routes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
