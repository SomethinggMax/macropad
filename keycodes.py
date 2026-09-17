"""US-layout HID keycode tables (USB HID Usage Page 0x07)."""

# Modifier bitmask for byte 0 of the report
MODIFIERS = {
    "ctrl": 0x01, "lctrl": 0x01, "shift": 0x02, "lshift": 0x02,
    "alt": 0x04, "lalt": 0x04, "gui": 0x08, "lgui": 0x08,
    "win": 0x08, "cmd": 0x08, "meta": 0x08,
    "rctrl": 0x10, "rshift": 0x20,
    "ralt": 0x40, "altgr": 0x40, "rgui": 0x80,
}

KEYS = {
    "a": 0x04, "b": 0x05, "c": 0x06, "d": 0x07, "e": 0x08, "f": 0x09,
    "g": 0x0A, "h": 0x0B, "i": 0x0C, "j": 0x0D, "k": 0x0E, "l": 0x0F,
    "m": 0x10, "n": 0x11, "o": 0x12, "p": 0x13, "q": 0x14, "r": 0x15,
    "s": 0x16, "t": 0x17, "u": 0x18, "v": 0x19, "w": 0x1A, "x": 0x1B,
    "y": 0x1C, "z": 0x1D,

    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,

    "enter": 0x28, "return": 0x28, "esc": 0x29, "escape": 0x29,
    "backspace": 0x2A, "bksp": 0x2A, "tab": 0x2B, "space": 0x2C,
    "minus": 0x2D, "equal": 0x2E, "leftbrace": 0x2F, "rightbrace": 0x30,
    "backslash": 0x31, "hash": 0x32, "semicolon": 0x33, "apostrophe": 0x34,
    "grave": 0x35, "comma": 0x36, "dot": 0x37, "period": 0x37, "slash": 0x38,
    "capslock": 0x39,

    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,

    "printscreen": 0x46, "sysrq": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "insert": 0x49, "home": 0x4A, "pageup": 0x4B, "delete": 0x4C, "del": 0x4C,
    "end": 0x4D, "pagedown": 0x4E,

    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,

    "numlock": 0x53, "kpslash": 0x54, "kpasterisk": 0x55, "kpminus": 0x56,
    "kpplus": 0x57, "kpenter": 0x58,
    "kp1": 0x59, "kp2": 0x5A, "kp3": 0x5B, "kp4": 0x5C, "kp5": 0x5D,
    "kp6": 0x5E, "kp7": 0x5F, "kp8": 0x60, "kp9": 0x61, "kp0": 0x62,
    "kpdot": 0x63,

    "menu": 0x65, "application": 0x65, "power": 0x66,

    "f13": 0x68, "f14": 0x69, "f15": 0x6A, "f16": 0x6B, "f17": 0x6C,
    "f18": 0x6D, "f19": 0x6E, "f20": 0x6F, "f21": 0x70, "f22": 0x71,
    "f23": 0x72, "f24": 0x73,
}

# Printable character -> (needs_shift, keycode)
_UNSHIFTED = {
    " ": "space", "\t": "tab", "\n": "enter",
    "-": "minus", "=": "equal", "[": "leftbrace", "]": "rightbrace",
    "\\": "backslash", ";": "semicolon", "'": "apostrophe", "`": "grave",
    ",": "comma", ".": "dot", "/": "slash",
}
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "minus", "+": "equal", "{": "leftbrace", "}": "rightbrace",
    "|": "backslash", ":": "semicolon", '"': "apostrophe", "~": "grave",
    "<": "comma", ">": "dot", "?": "slash",
}

CHARS = {}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    CHARS[_c] = (False, KEYS[_c])
for _c in "abcdefghijklmnopqrstuvwxyz":
    CHARS[_c.upper()] = (True, KEYS[_c])
for _c, _name in _UNSHIFTED.items():
    CHARS[_c] = (False, KEYS[_name])
for _c, _name in _SHIFTED.items():
    CHARS[_c] = (True, KEYS[_name])


# Keys that do not produce a single printable character, grouped for the IDE.
_GROUPS = [
    ("Modifiers", ["ctrl", "shift", "alt", "gui", "altgr", "rctrl", "rshift", "rgui"]),
    ("Navigation", ["up", "down", "left", "right", "home", "end",
                    "pageup", "pagedown"]),
    ("Editing", ["enter", "tab", "space", "backspace", "delete", "insert", "escape"]),
    ("Function", [f"f{n}" for n in range(1, 25)]),
    ("Locks & system", ["capslock", "numlock", "scrolllock", "printscreen",
                        "pause", "menu"]),
    ("Keypad", [f"kp{n}" for n in range(10)] +
               ["kpenter", "kpplus", "kpminus", "kpasterisk", "kpslash", "kpdot"]),
]

_ALIASES = {
    "ctrl": ["lctrl"], "shift": ["lshift"], "alt": ["lalt"],
    "gui": ["win", "cmd", "meta", "lgui"], "altgr": ["ralt"],
    "enter": ["return"], "escape": ["esc"], "delete": ["del"],
    "backspace": ["bksp"], "printscreen": ["sysrq"], "menu": ["application"],
}


def catalogue():
    """Grouped non-printable key names, with aliases, for the editor's picker."""
    groups = []
    for title, names in _GROUPS:
        entries = []
        for name in names:
            if name not in KEYS and name not in MODIFIERS:
                raise KeyError(f"catalogue lists unknown key {name!r}")
            entries.append({
                "name": name,
                "aliases": _ALIASES.get(name, []),
                "modifier": name in MODIFIERS,
            })
        groups.append({"title": title, "keys": entries})
    return groups
