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


# a game whose chat window shares its name, plus decoys
WINDOWS = [
    {"id": 101, "title": "Some Game", "exe": "game.exe",
     "class": "UnityWndClass", "z": 0},
    {"id": 102, "title": "Some Game", "exe": "game.exe",
     "class": "GameChatWindow", "z": 1},
    {"id": 103, "title": "Some Game - Launcher", "exe": "launcher.exe",
     "class": "Chrome_WidgetWin_1", "z": 2},
    {"id": 104, "title": "Discord", "exe": "Discord.exe",
     "class": "Chrome_WidgetWin_1", "z": 3},
]


def main():
    agent = load()
    agent.list_windows = lambda: [dict(w) for w in WINDOWS]

    cases = [
        ("Some Game",              101, "plain substring -> topmost match"),
        ("Some Game#2",            102, "#2 -> second match"),
        ("class:GameChatWindow",   102, "class picks the chat window"),
        ("class:UnityWndClass",    101, "class picks the game window"),
        ("exe:launcher.exe",       103, "exe field match"),
        ("102",                    102, "explicit window id"),
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

    for target in ["nothing here", "Some Game#9"]:
        got = agent.find_window(target)
        ok = got is None
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {target:24} -> {got} (should be None)")

    matches, _ = agent.match_windows("Some Game")
    print(f"\n  'Some Game' matches {len(matches)} windows: "
          + ", ".join(f"#{n+1} {w['class']}" for n, w in enumerate(matches)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
