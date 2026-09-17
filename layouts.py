"""Keyboard layout profiles.

HID sends physical key positions, not characters - the host decides what
character a scancode produces. A layout maps a character to the sequence of
key presses the *host* needs to produce it. On US-International the keys
" ' ` ~ ^ are dead: they wait for a following character to compose an accent,
so a literal one is typed as dead-key + space.
"""

from keycodes import CHARS, KEYS, MODIFIERS

SHIFT = MODIFIERS["shift"]
SPACE = (0, KEYS["space"])

# char -> (modifiers, keycode) for the dead key itself
_DEAD = {
    '"': (SHIFT, KEYS["apostrophe"]),
    "'": (0, KEYS["apostrophe"]),
    "~": (SHIFT, KEYS["grave"]),
    "`": (0, KEYS["grave"]),
    "^": (SHIFT, KEYS["6"]),
}

# dead key -> base letter -> composed character
_ACCENTS = {
    '"': {"a": "ä", "e": "ë", "i": "ï", "o": "ö", "u": "ü", "y": "ÿ"},
    "'": {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "y": "ý", "c": "ç"},
    "`": {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù"},
    "~": {"a": "ã", "n": "ñ", "o": "õ"},
    "^": {"a": "â", "e": "ê", "i": "î", "o": "ô", "u": "û"},
}


def _plain():
    """Direct character -> single keypress, as on a plain US layout."""
    return {c: ((SHIFT if shifted else 0, key),) for c, (shifted, key) in CHARS.items()}


def _us_international():
    table = _plain()
    for char, press in _DEAD.items():
        # literal dead character: press it, then space to release the composition
        table[char] = (press, SPACE)
    for dead, mapping in _ACCENTS.items():
        press = _DEAD[dead]
        for base, composed in mapping.items():
            table[composed] = (press, (0, KEYS[base]))
            table[composed.upper()] = (press, (SHIFT, KEYS[base]))
    return table


LAYOUTS = {
    "us": _plain(),
    "us-intl": _us_international(),
}
DEFAULT = "us-intl"


def get(name):
    try:
        return LAYOUTS[name]
    except KeyError:
        raise ValueError(
            f"unknown layout {name!r}; available: {', '.join(sorted(LAYOUTS))}"
        ) from None
