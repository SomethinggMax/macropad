"""Exercise the agent's window matching without Windows."""

import ctypes
import importlib.util
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "agent" / "windows-agent.py"


class FakeFunc:
    def __init__(self, name):
        self.argtypes = self.restype = None
    def __call__(self, *a, **k):
        return 0


class FakeDLL:
    def __init__(self, *a, **k):
        self._f = {}
    def __getattr__(self, item):
        return self._f.setdefault(item, FakeFunc(item))


def load():
    ctypes.WinDLL = FakeDLL
    ctypes.windll = FakeDLL()
    if not hasattr(ctypes, "WINFUNCTYPE"):
        ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    spec = importlib.util.spec_from_file_location("winagent", AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def win(wid, title, exe, cls, z, w, h):
    return {"id": wid, "title": title, "exe": exe, "class": cls, "z": z,
            "rect": {"x": 0, "y": 0, "width": w, "height": h},
            "client": {"x": 0, "y": 0, "width": w, "height": h},
            "area": w * h}


# Max's real case: two MapleStory windows, same title AND same class, told
# apart only by size - the small one is the chat window and sits above the
# game in z-order, so topmost-first picked the wrong one.
WINDOWS = [
    win(986860, "MapleStory", "MapleStory.exe", "MapleStoryClass", 5, 410, 806),
    win(527280, "MapleStory", "MapleStory.exe", "MapleStoryClass", 6, 1928, 1111),
    win(103, "Some Game - Launcher", "launcher.exe", "Chrome_WidgetWin_1", 2, 800, 600),
    win(104, "Discord", "Discord.exe", "Chrome_WidgetWin_1", 3, 1200, 800),
]


def main():
    agent = load()
    agent.list_windows = lambda: [dict(w) for w in WINDOWS]

    cases = [
        ("MapleStory",          527280, "identical title+class -> biggest wins"),
        ("MapleStory#2",        986860, "#2 -> the smaller chat window"),
        ("527280",              527280, "explicit window id"),
        ("986860",              986860, "explicit window id, the small one"),
        ("exe:MapleStory.exe",  527280, "exe match still prefers the big window"),
        ("exe:launcher.exe",       103, "exe field match"),
        ("Discord",                104, "unrelated window"),
        ("Launcher",               103, "substring of a longer title"),
    ]
    failures = 0
    for target, expect, note in cases:
        got = agent.find_window(target)
        got_id = got["id"] if got else None
        ok = got_id == expect
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {target:24} -> {got_id}  ({note})")

    for target in ["nothing here", "MapleStory#9"]:
        got = agent.find_window(target)
        ok = got is None
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {target:24} -> {got} (should be None)")

    matches, _ = agent.match_windows("MapleStory")
    print("\n  'MapleStory' matches, in the order focus would try them:")
    for n, w in enumerate(matches):
        print(f"    #{n+1} {w['rect']['width']}x{w['rect']['height']} id={w['id']}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
