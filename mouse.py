"""Mouse over the USB HID gadget (/dev/hidg1).

The gadget is a standard *relative* mouse, because Windows would not accept an
absolute-pointer report descriptor. Absolute moves are therefore synthesised:
slam the pointer into the top-left corner (where the desktop clamps it), then
step out to the target. That makes `moveto` self-correcting - it never drifts,
because every move re-establishes the corner as a known origin.
"""

import struct
import time

from keyboard import HidDevice

DEVICE = "/dev/hidg1"
BUTTONS = {"left": 0x01, "right": 0x02, "middle": 0x04}
STEP = 127  # a delta is one signed byte


class Mouse(HidDevice):
    def __init__(self, device=DEVICE, screen=(1920, 1080), origin=(0, 0),
                 click_delay=0.02, step_delay=0.002, locate=None, place=None):
        super().__init__(device)
        # Optional callable that puts the pointer somewhere exactly (the PC
        # agent). Preferred for absolute moves: HID deltas are distorted by
        # pointer acceleration and blocked by gaps between monitors, neither of
        # which can be corrected reliably from this side.
        self.place = place
        # Optional callable returning the pointer's real (x, y). With it, moves
        # are closed-loop: the true corner is measured rather than assumed, which
        # matters because the virtual desktop's corner may be dead space no
        # monitor covers, and because pointer acceleration distorts raw deltas.
        self.locate = locate
        self.screen = screen
        # Top-left of the virtual desktop in Windows coordinates. It is negative
        # when a monitor sits left of or above the primary one, and it is the
        # point the corner-slam actually lands on.
        self.origin = origin
        self.click_delay = click_delay
        self.step_delay = step_delay
        self._buttons = 0
        self._x, self._y = 0, 0

    @property
    def position(self):
        return self._x, self._y

    def _send(self, dx=0, dy=0, wheel=0):
        self._write(struct.pack("<Bbbb", self._buttons,
                                max(-STEP, min(STEP, int(dx))),
                                max(-STEP, min(STEP, int(dy))),
                                max(-STEP, min(STEP, int(wheel)))))

    def _travel(self, dx, dy):
        """Send a large movement as a series of single-byte steps."""
        while dx or dy:
            step_x = max(-STEP, min(STEP, dx))
            step_y = max(-STEP, min(STEP, dy))
            self._send(step_x, step_y)
            dx -= step_x
            dy -= step_y
            time.sleep(self.step_delay)

    def to_origin(self):
        """Pin the pointer at the top-left corner, which the desktop clamps.

        The step count is deliberately generous rather than derived from
        `screen`: the pointer clamps at the corner of the whole virtual desktop
        (all monitors), which can be far wider than the configured size. Too few
        steps would stop short and every absolute move would then be off.
        """
        steps = max(80, max(self.screen) // STEP + 2)
        for _ in range(steps):
            self._send(-STEP, -STEP)
            time.sleep(self.step_delay)
        self._x, self._y = self.origin

    def calibrate(self):
        """Slam to the corner and record where the pointer really stopped."""
        self.to_origin()
        if self.locate is None:
            return self.origin
        found = self.locate()
        self.origin = (found["x"], found["y"])
        self._x, self._y = self.origin
        return self.origin

    def move_to(self, x, y, corrections=6):
        """Move to an absolute position in Windows screen coordinates."""
        if self.place is not None:
            landed = self.place(int(x), int(y))
            self._x, self._y = landed["x"], landed["y"]
            time.sleep(self.click_delay)
            return self._x, self._y

        left, top = self.origin
        x = max(left, min(int(x), left + self.screen[0] - 1))
        y = max(top, min(int(y), top + self.screen[1] - 1))

        if self.locate is None:
            self.to_origin()
            self._travel(x - left, y - top)
            self._x, self._y = x, y
            time.sleep(self.click_delay)
            return self._x, self._y

        left, top = self.calibrate()
        self._travel(x - left, y - top)
        for _ in range(corrections):
            found = self.locate()
            self._x, self._y = found["x"], found["y"]
            dx, dy = x - self._x, y - self._y
            if dx == 0 and dy == 0:
                break
            self._travel(dx, dy)  # acceleration or dead space knocked us off
        found = self.locate()
        self._x, self._y = found["x"], found["y"]
        time.sleep(self.click_delay)
        return self._x, self._y

    def move(self, dx, dy):
        """Move relative to the current pointer position."""
        self._travel(int(dx), int(dy))
        left, top = self.origin
        self._x = max(left, min(self._x + int(dx), left + self.screen[0] - 1))
        self._y = max(top, min(self._y + int(dy), top + self.screen[1] - 1))
        time.sleep(self.click_delay)

    def button_down(self, button="left"):
        self._buttons |= self._bit(button)
        self._send()
        time.sleep(self.click_delay)

    def button_up(self, button=None):
        self._buttons = 0 if button is None else self._buttons & ~self._bit(button)
        self._send()
        time.sleep(self.click_delay)

    def click(self, button="left", count=1):
        for _ in range(max(1, int(count))):
            self.button_down(button)
            self.button_up(button)

    def scroll(self, amount):
        """Positive scrolls up, negative scrolls down."""
        remaining = int(amount)
        while remaining:
            step = max(-STEP, min(STEP, remaining))
            self._send(wheel=step)
            remaining -= step
            time.sleep(self.click_delay)

    def release_all(self):
        self._buttons = 0
        self._send()

    def close(self):
        if self._fd is not None:
            try:
                self.release_all()
            except Exception:
                pass
        super().close()

    @staticmethod
    def _bit(button):
        key = str(button).lower()
        if key not in BUTTONS:
            raise ValueError(
                f"unknown mouse button {button!r} (use {', '.join(BUTTONS)})")
        return BUTTONS[key]
