"""Send keystrokes to the host PC via the USB HID gadget device."""

import errno
import os
import select
import time

import layouts
from keycodes import CHARS, KEYS, MODIFIERS

DEVICE = "/dev/hidg0"
UDC_STATE = "/sys/class/udc/fe980000.usb/state"
RELEASE = b"\x00" * 8
MAX_KEYS = 6  # a boot-protocol report carries at most 6 simultaneous keycodes


class NotConnectedError(RuntimeError):
    """Host is not enumerating the gadget (cable unplugged, PC asleep, etc.)."""


def host_state():
    try:
        with open(UDC_STATE) as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


def parse_combo(combo):
    """'ctrl+alt+delete' -> (modifier_bits, keycode). Keycode is 0 for mods only."""
    mods, key = 0, 0
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combo")
    for part in parts[:-1]:
        if part not in MODIFIERS:
            if part in KEYS:
                raise ValueError(
                    f"{part!r} is a key, not a modifier - modifiers come first "
                    f"in a combo, so write e.g. ctrl+{part}")
            raise ValueError(
                f"unknown modifier: {part!r} (use "
                f"{', '.join(sorted({'ctrl', 'shift', 'alt', 'gui', 'altgr'}))})")
        mods |= MODIFIERS[part]
    last = parts[-1]
    if last in KEYS:
        key = KEYS[last]
    elif last in MODIFIERS:
        mods |= MODIFIERS[last]
    elif len(last) == 1 and last in CHARS:
        shifted, key = CHARS[last]
        if shifted:
            mods |= MODIFIERS["shift"]
    else:
        raise ValueError(f"unknown key: {last!r}")
    return mods, key


class HidDevice:
    """Shared plumbing for writing reports to a /dev/hidgN gadget node."""

    # The gadget driver blocks a write until the host polls that endpoint. A
    # blocking write therefore hangs forever if the host never does, so writes
    # are non-blocking with a deadline instead.
    write_timeout = 2.0

    def __init__(self, device):
        self.device = device
        self._fd = None

    def open(self):
        if self._fd is None:
            try:
                self._fd = os.open(self.device, os.O_WRONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                raise NotConnectedError(
                    f"{self.device} missing - is usb-gadget.service running?"
                ) from None
            except PermissionError:
                raise NotConnectedError(
                    f"no permission for {self.device} - need the 'hidgadget' group"
                ) from None
        return self

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _write(self, report):
        if self._fd is None:
            self.open()
        deadline = time.monotonic() + self.write_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NotConnectedError(
                    f"{self.device}: the PC is not reading reports from this "
                    f"endpoint (state={host_state()}). Try unplugging and "
                    f"replugging the USB-C cable so the PC re-enumerates it.")
            try:
                select.select([], [self._fd], [], min(remaining, 0.25))
                os.write(self._fd, report)
                return
            except BlockingIOError:
                time.sleep(0.005)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    time.sleep(0.005)
                    continue
                if exc.errno in (errno.ESHUTDOWN, errno.ENODEV, errno.EPIPE):
                    raise NotConnectedError(
                        f"host not reachable (state={host_state()}): {exc.strerror}"
                    ) from None
                raise


class Keyboard(HidDevice):
    """Types via /dev/hidg0, tracking which keys are being held down.

    Held keys persist across calls, so a macro can hold shift, type several
    things, then release - every report merges the held state with whatever
    the current call is sending.
    """

    def __init__(self, device=DEVICE, key_delay=0.012,
                 press_delay=0.012, mod_delay=0.012, layout=layouts.DEFAULT):
        super().__init__(device)
        self.layout_name = layout
        self.layout = layouts.get(layout)
        self.key_delay = key_delay      # after releasing a key
        self.press_delay = press_delay  # how long a key stays down
        self.mod_delay = mod_delay      # after a modifier-only transition
        self._held_mods = 0
        self._held_keys = []

    def close(self):
        if self._fd is not None:
            self._held_mods, self._held_keys = 0, []
            try:
                self._write(RELEASE)  # never leave a key stuck down on the host
            except NotConnectedError:
                pass
        super().close()

    def _emit(self, mods=0, keys=()):
        """Write one report merging held state with transient mods/keys."""
        codes = self._held_keys + [k for k in keys if k and k not in self._held_keys]
        codes = codes[:MAX_KEYS]
        self._write(bytes([self._held_mods | mods, 0]
                          + codes + [0] * (MAX_KEYS - len(codes))))

    def send(self, mods=0, keys=()):
        """Send one raw report (held state is still merged in)."""
        self._emit(mods, keys)

    # ---- held state ------------------------------------------------------
    @property
    def held(self):
        """Names of what is currently held down, for diagnostics."""
        names = [n for n, bit in MODIFIERS.items()
                 if bit & self._held_mods and n in ("ctrl", "shift", "alt", "gui")]
        names += [n for n, code in KEYS.items() if code in self._held_keys]
        return names

    def key_down(self, combo):
        """Press and hold; stays down until released."""
        mods, key = parse_combo(combo)
        self._held_mods |= mods
        if key and key not in self._held_keys:
            if len(self._held_keys) >= MAX_KEYS:
                raise ValueError(
                    f"cannot hold more than {MAX_KEYS} keys at once "
                    f"(already holding {', '.join(self.held)})")
            self._held_keys.append(key)
        self._emit()
        time.sleep(self.key_delay)

    def key_up(self, combo=None):
        """Release one combo, or everything when combo is None."""
        if combo is None:
            self._held_mods, self._held_keys = 0, []
        else:
            mods, key = parse_combo(combo)
            self._held_mods &= ~mods
            if key in self._held_keys:
                self._held_keys.remove(key)
        self._emit()
        time.sleep(self.key_delay)

    def release_all(self):
        self.key_up(None)

    # ---- typing ----------------------------------------------------------
    def tap(self, combo, hold=0.0):
        """Press a combo the way a real keyboard does: modifiers first, then the key."""
        mods, key = parse_combo(combo)
        if mods:
            self._emit(mods)
            # A modifier-only combo (a game skill bound to shift, say) is the
            # press itself, so any requested hold has to apply here. Holding
            # for only mod_delay can fall between two of the host's input
            # polls and be missed entirely.
            time.sleep(self.mod_delay if key else (hold or self.press_delay))
        if key:
            self._emit(mods, [key])
            time.sleep(hold or self.press_delay)
            self._emit(mods)
        self._emit()
        time.sleep(self.key_delay)

    def type_string(self, text, skip_unknown=False, should_stop=None):
        """Type text via the active layout, holding modifiers across runs.

        A character may need more than one press (on US-International a literal
        '"' is the dead key followed by space).
        """
        applied = 0
        try:
            for char in text:
                if should_stop is not None and should_stop():
                    break
                presses = self.layout.get(char)
                if presses is None:
                    if skip_unknown:
                        continue
                    raise ValueError(
                        f"{char!r} is not typeable on layout {self.layout_name!r}")
                for mods, key in presses:
                    if mods != applied:
                        # Modifier changes get their own report so the host
                        # cannot sample the keycode before the modifier lands.
                        self._emit(mods)
                        time.sleep(self.mod_delay)
                        applied = mods
                    self._emit(applied, [key])
                    time.sleep(self.press_delay)
                    self._emit(applied)
                    time.sleep(self.key_delay)
        finally:
            if applied:
                self._emit()
