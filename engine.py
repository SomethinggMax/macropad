"""Interpreter for macro scripts: typing, delays, loops, labels, jumps, randomness."""

import random
import re
import time

import agent_client
import layouts

from keyboard import Keyboard, NotConnectedError, parse_combo
from mouse import Mouse


class MacroError(Exception):
    pass


class MacroStopped(MacroError):
    """Raised when an emergency stop interrupts a running macro."""

    def __init__(self, message="stopped", log=None):
        super().__init__(message)
        self.log = log or []


class Instruction:
    __slots__ = ("op", "args", "line")

    def __init__(self, op, args, line):
        self.op, self.args, self.line = op, args, line

    def __repr__(self):
        return f"<{self.op} {self.args} @L{self.line}>"


NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COMPARISONS = ("<", "<=", ">", ">=", "==", "!=", "contains", "near")
COLOUR = re.compile(r"^#?([0-9a-fA-F]{6})$")
NEAR_TOLERANCE = 16  # per channel, for the "near" comparison
# Commands whose argument is literal text, where '#' must stay as typed.
TEXT_OPS = ("type", "paste", "set", "oneof")
COLOUR_TOKEN = re.compile(r"^#[0-9a-fA-F]{6}(?![0-9a-fA-F])")


def strip_inline_comment(text):
    """Drop a trailing '# ...' comment, but keep colour literals like #ff00aa."""
    for index, char in enumerate(text):
        if char == "#" and not COLOUR_TOKEN.match(text[index:]):
            return text[:index]
    return text
MOUSE_OPS = ("move", "moveto", "click", "mousedown", "mouseup",
             "scroll", "screen")
# ops whose x/y are measured from the anchored window, when one is set
ANCHORED_OPS = ("moveto", "getpixel", "waitfor")
# $$ is a literal '$'; $name is a reference. A bare '$' (as in "$5") stays literal.
VAR = re.compile(r"\$(\$|[A-Za-z_][A-Za-z0-9_]*)")


def expand(text, variables, line):
    """Substitute $name references; $$ becomes a literal '$'."""
    def swap(match):
        token = match.group(1)
        if token == "$":
            return "$"
        if token not in variables:
            raise MacroError(f"line {line}: undefined variable ${token}")
        return variables[token]
    return VAR.sub(swap, str(text))


def strip_vars(text):
    """Text with variable references removed, for layout checks at parse time."""
    return VAR.sub(lambda m: "" if m.group(1) != "$" else "$", str(text))


def as_number(value, variables, line, what):
    resolved = expand(value, variables, line)
    try:
        return int(str(resolved).strip())
    except ValueError:
        raise MacroError(
            f"line {line}: {what} must be a whole number, got {resolved!r}") from None


def _parse_ms(token, line):
    """'500' -> (500, 500); '100-300' -> a random range; '$w' defers to run time."""
    def one(part):
        part = part.strip()
        if "$" in part:
            return part  # resolved when the instruction executes
        try:
            return int(part)
        except ValueError:
            raise MacroError(f"line {line}: bad duration {token!r}") from None

    if "-" in token and not token.strip().startswith("-"):
        lo, hi = (one(p) for p in token.split("-", 1))
    else:
        lo = hi = one(token)
    if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
        raise MacroError(f"line {line}: bad duration {token!r} (min above max)")
    return lo, hi


def parse(source):
    """Parse macro source into a list of Instructions plus a label map."""
    instrs, labels, open_loops = [], {}, []

    for lineno, raw in enumerate(source.splitlines(), 1):
        # Only whole-line comments, so '#' stays typeable inside text.
        body = raw.rstrip("\r").lstrip()
        if not body.strip() or body.startswith("#"):
            continue
        op, _, remainder = body.partition(" ")
        op = op.lower()
        if op not in TEXT_OPS:
            remainder = strip_inline_comment(remainder)
        rest = remainder.strip()

        if op == "type":
            # Verbatim: leading/trailing spaces in typed text are significant.
            instrs.append(Instruction("type", [remainder], lineno))
        elif op == "key":
            parts = rest.split()
            if not parts:
                raise MacroError(f"line {lineno}: 'key' needs a combo")
            hold = int(parts[1]) / 1000 if len(parts) > 1 else 0.0
            if "$" not in parts[0]:  # a variable combo is checked when it runs
                try:
                    parse_combo(parts[0])
                except ValueError as exc:
                    raise MacroError(f"line {lineno}: {exc}") from None
            instrs.append(Instruction("key", [parts[0], hold], lineno))
        elif op in ("hold", "release"):
            if op == "hold" and not rest:
                raise MacroError(f"line {lineno}: 'hold' needs a key or combo")
            if rest and "$" not in rest:
                try:
                    parse_combo(rest)
                except ValueError as exc:
                    raise MacroError(f"line {lineno}: {exc}") from None
            instrs.append(Instruction(op, [rest or None], lineno))
        elif op in ("moveto", "move", "screen"):
            parts = rest.split()
            if len(parts) != 2:
                raise MacroError(f"line {lineno}: {op!r} needs two numbers")
            instrs.append(Instruction(op, parts, lineno))
        elif op == "scroll":
            if not rest:
                raise MacroError(f"line {lineno}: 'scroll' needs an amount")
            instrs.append(Instruction("scroll", [rest], lineno))
        elif op in ("mousedown", "mouseup"):
            if op == "mousedown" and not rest:
                raise MacroError(f"line {lineno}: 'mousedown' needs a button")
            instrs.append(Instruction(op, [rest or None], lineno))
        elif op == "click":
            parts = rest.split()
            button = parts[0] if parts else "left"
            count = parts[1] if len(parts) > 1 else "1"
            if len(parts) > 2:
                raise MacroError(f"line {lineno}: 'click' takes <button> <count>")
            instrs.append(Instruction("click", [button, count], lineno))
        elif op == "anchor":
            if not rest:
                raise MacroError(
                    f"line {lineno}: 'anchor' needs a window, or 'none'")
            instrs.append(Instruction("anchor", [remainder.strip()], lineno))
        elif op == "paste":
            if not remainder.strip():
                raise MacroError(f"line {lineno}: 'paste' needs some text")
            instrs.append(Instruction("paste", [remainder], lineno))
        elif op == "clip":
            if not NAME.match(rest or ""):
                raise MacroError(f"line {lineno}: 'clip' needs a variable name")
            instrs.append(Instruction("clip", [rest], lineno))
        elif op == "getpixel":
            parts = rest.split()
            if len(parts) != 3 or not NAME.match(parts[2]):
                raise MacroError(
                    f"line {lineno}: 'getpixel' needs <x> <y> <variable>")
            instrs.append(Instruction("getpixel", parts, lineno))
        elif op == "waitfor":
            parts = rest.split()
            if not parts:
                raise MacroError(f"line {lineno}: 'waitfor' needs 'window' or 'pixel'")
            kind = parts[0].lower()
            rest_parts = parts[1:]
            timeout = "10000"
            if len(rest_parts) > (3 if kind == "pixel" else 1) and \
                    rest_parts[-1].isdigit():
                timeout = rest_parts.pop()
            if kind == "window":
                if not rest_parts:
                    raise MacroError(f"line {lineno}: 'waitfor window' needs a title")
                instrs.append(Instruction(
                    "waitfor", ["window", " ".join(rest_parts), timeout], lineno))
            elif kind == "pixel":
                if len(rest_parts) != 3:
                    raise MacroError(
                        f"line {lineno}: 'waitfor pixel' needs <x> <y> <#rrggbb>")
                instrs.append(Instruction(
                    "waitfor", ["pixel", rest_parts, timeout], lineno))
            else:
                raise MacroError(
                    f"line {lineno}: waitfor {kind!r} - use 'window' or 'pixel'")
        elif op == "jumpif":
            parts = rest.split()
            if len(parts) < 4:
                raise MacroError(
                    f"line {lineno}: jumpif needs <variable> <op> <value> <label>")
            variable, comparison, target = parts[0], parts[1], parts[-1]
            value = " ".join(parts[2:-1])
            if not NAME.match(variable):
                raise MacroError(
                    f"line {lineno}: jumpif needs a variable name, got {variable!r}")
            if comparison not in COMPARISONS:
                raise MacroError(
                    f"line {lineno}: unknown comparison {comparison!r} "
                    f"(use {', '.join(COMPARISONS)})")
            instrs.append(
                Instruction("jumpif", [variable, comparison, value, target], lineno))
        elif op == "focus":
            if not rest:
                raise MacroError(
                    f"line {lineno}: 'focus' needs a window title, exe name or id")
            instrs.append(Instruction("focus", [remainder.strip()], lineno))
        elif op in ("set", "add"):
            name, _, value = rest.partition(" ")
            if not NAME.match(name or ""):
                raise MacroError(
                    f"line {lineno}: {op!r} needs a variable name "
                    f"(letters, digits, underscore; not starting with a digit)")
            value = value.strip()
            if not value:
                raise MacroError(f"line {lineno}: {op!r} {name} needs a value")
            if op == "add" and "$" not in value:
                try:
                    int(value)
                except ValueError:
                    raise MacroError(
                        f"line {lineno}: 'add' amount must be a number, got {value!r}"
                    ) from None
            instrs.append(Instruction(op, [name, value], lineno))
        elif op == "delay":
            instrs.append(Instruction("delay", list(_parse_ms(rest, lineno)), lineno))
        elif op == "label":
            if not rest:
                raise MacroError(f"line {lineno}: 'label' needs a name")
            if rest in labels:
                raise MacroError(f"line {lineno}: duplicate label {rest!r}")
            labels[rest] = len(instrs)
            instrs.append(Instruction("nop", [], lineno))
        elif op == "goto":
            instrs.append(Instruction("goto", [rest], lineno))
        elif op == "repeat":
            if rest == "forever":
                count = -1
            elif "$" in rest:
                count = rest  # resolved when the loop starts
            else:
                try:
                    count = int(rest)
                except ValueError:
                    raise MacroError(f"line {lineno}: bad repeat count {rest!r}") from None
            open_loops.append(len(instrs))
            instrs.append(Instruction("repeat", [count, None], lineno))
        elif op == "end":
            if not open_loops:
                raise MacroError(f"line {lineno}: 'end' without 'repeat'")
            start = open_loops.pop()
            instrs[start].args[1] = len(instrs)
            instrs.append(Instruction("end", [start], lineno))
        elif op == "chance":
            parts = rest.split(None, 1)
            if len(parts) != 2:
                raise MacroError(f"line {lineno}: 'chance <pct> <instruction>'")
            pct = parts[0] if "$" in parts[0] else int(parts[0])
            inner = parse(parts[1])[0]
            if len(inner) != 1:
                raise MacroError(f"line {lineno}: 'chance' needs one simple instruction")
            instrs.append(Instruction("chance", [pct, inner[0]], lineno))
        elif op == "oneof":
            choices = [c.strip() for c in rest.split("|") if c.strip()]
            if not choices:
                raise MacroError(f"line {lineno}: 'oneof' needs choices split by |")
            instrs.append(Instruction("oneof", [choices], lineno))
        else:
            raise MacroError(f"line {lineno}: unknown command {op!r}")

    if open_loops:
        bad = instrs[open_loops[-1]].line
        raise MacroError(f"line {bad}: 'repeat' without matching 'end'")
    for instr in instrs:
        if instr.op == "goto" and instr.args[0] not in labels:
            raise MacroError(f"line {instr.line}: goto unknown label {instr.args[0]!r}")
        if instr.op == "jumpif" and instr.args[3] not in labels:
            raise MacroError(
                f"line {instr.line}: jumpif unknown label {instr.args[3]!r}")

    # every op that defines a variable, and where its name sits in args
    assigned = {i.args[0] for i in instrs if i.op in ("set", "add", "clip")}
    assigned |= {i.args[2] for i in instrs if i.op == "getpixel"}
    for instr in instrs:
        if instr.op == "jumpif" and instr.args[0] not in assigned:
            raise MacroError(
                f"line {instr.line}: jumpif tests {instr.args[0]!r}, which is never set")
    for instr in instrs:
        for name in _referenced(instr):
            if name not in assigned:
                raise MacroError(
                    f"line {instr.line}: ${name} is never set anywhere in this macro")
    return instrs, labels


def _referenced(instr):
    """Every $name a single instruction refers to."""
    found = []
    for arg in instr.args:
        items = arg if isinstance(arg, list) else [arg]
        for item in items:
            if isinstance(item, Instruction):
                found += _referenced(item)
            elif isinstance(item, str):
                found += [m.group(1) for m in VAR.finditer(item) if m.group(1) != "$"]
    return found


def validate(instrs, table, layout_name):
    """Reject text that the host layout cannot produce, before anything is sent."""
    for instr in instrs:
        if instr.op == "type":
            texts = [instr.args[0]]
        elif instr.op == "oneof":
            texts = instr.args[0]
        elif instr.op == "chance" and instr.args[1].op in ("type", "oneof"):
            inner = instr.args[1]
            texts = [inner.args[0]] if inner.op == "type" else inner.args[0]
        else:
            continue
        for text in texts:
            for char in strip_vars(text):
                if char not in table:
                    raise MacroError(
                        f"line {instr.line}: {char!r} cannot be typed on layout "
                        f"{layout_name!r}")


def run(source, kb=None, max_steps=100_000, max_seconds=60.0, dry_run=False,
        layout=None, stop=None, on_step=None):
    """Execute a macro. Guard rails stop runaway loops from spamming the host."""
    instrs, labels = parse(source)

    layout_name = kb.layout_name if kb is not None else (layout or layouts.DEFAULT)
    table = layouts.get(layout_name)
    validate(instrs, table, layout_name)

    owns_kb = kb is None and not dry_run
    if owns_kb:
        kb = Keyboard().open()
    mouse = None
    if not dry_run and any(i.op in MOUSE_OPS for i in instrs):
        mouse = Mouse().open()
        layout_info = agent_client.screen()  # ask the PC how big the desktop is
        if layout_info:
            area = layout_info["virtual"]
            mouse.screen = (area["width"], area["height"])
            mouse.origin = (area["x"], area["y"])
            mouse.locate = agent_client.cursor
            mouse.place = agent_client.set_cursor

    counters, variables, steps = {}, {}, 0
    anchor = {"origin": None, "name": None}
    started = time.monotonic()
    log = []
    try:
        pc = 0
        while pc < len(instrs):
            steps += 1
            if steps > max_steps:
                raise MacroError(f"aborted: exceeded {max_steps} steps (runaway loop?)")
            if time.monotonic() - started > max_seconds:
                raise MacroError(f"aborted: exceeded {max_seconds}s runtime")

            if stop is not None and stop.is_set():
                raise MacroStopped("stopped", log)

            instr = instrs[pc]
            if on_step is not None:
                on_step(instr.line, instr.op)
            pc = _step(instr, pc, kb, labels, counters, log, dry_run, stop,
                       variables, table, mouse, anchor)
    except MacroStopped as exc:
        exc.log = log
        raise
    finally:
        # A macro that errors mid-hold must not leave keys stuck down.
        if kb is not None and not dry_run:
            try:
                kb.release_all()
            except NotConnectedError:
                pass
        if mouse is not None:
            mouse.close()
        if owns_kb:
            kb.close()
    return log


def _anchored(x, y, anchor):
    """Shift window-relative coordinates into screen coordinates."""
    if not anchor or not anchor.get("origin"):
        return x, y
    left, top = anchor["origin"]
    return x + left, y + top


def _channels(value):
    """#rrggbb -> (r, g, b), or None when it is not a colour."""
    found = COLOUR.match(str(value).strip())
    if not found:
        return None
    digits = found.group(1)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def _compare(left, right, comparison, line):
    if comparison == "contains":
        return str(right).lower() in str(left).lower()

    first_rgb, second_rgb = _channels(left), _channels(right)
    if comparison == "near":
        if first_rgb is None or second_rgb is None:
            raise MacroError(
                f"line {line}: 'near' compares colours, got {left!r} and {right!r}")
        return all(abs(a - b) <= NEAR_TOLERANCE
                   for a, b in zip(first_rgb, second_rgb))
    if first_rgb is not None and second_rgb is not None:
        # colours are case-insensitive: getpixel returns lowercase hex
        left, right = str(left).strip().lower(), str(right).strip().lower()
    try:
        first, second = int(str(left).strip()), int(str(right).strip())
    except ValueError:
        if comparison in ("<", "<=", ">", ">="):
            raise MacroError(
                f"line {line}: cannot compare {left!r} {comparison} {right!r} "
                f"as numbers") from None
        first, second = str(left), str(right)
    return {"<": first < second, "<=": first <= second,
            ">": first > second, ">=": first >= second,
            "==": first == second, "!=": first != second}[comparison]


def _check_typeable(text, table, line):
    """Validate an expanded string before any of it is sent to the host."""
    if table is None:
        return
    for char in text:
        if char not in table:
            raise MacroError(
                f"line {line}: {char!r} cannot be typed on this layout")


def _check_combo(combo, line):
    try:
        parse_combo(combo)
    except ValueError as exc:
        raise MacroError(f"line {line}: {exc}") from None


def _step(instr, pc, kb, labels, counters, log, dry_run, stop=None,
          variables=None, table=None, mouse=None, anchor=None):
    variables = {} if variables is None else variables
    anchor = {"origin": None, "name": None} if anchor is None else anchor
    op, args = instr.op, instr.args

    if op == "nop":
        return pc + 1
    if op in MOUSE_OPS:
        numeric = ("screen", "moveto", "move", "scroll")
        values = ([as_number(a, variables, instr.line, op) for a in args]
                  if op in numeric else None)
        if op == "screen":
            log.append(f"screen {values[0]}x{values[1]}")
            if mouse is not None:
                mouse.screen = (values[0], values[1])
        elif op == "moveto":
            ax, ay = _anchored(values[0], values[1], anchor)
            log.append(f"moveto {ax},{ay}" +
                       (f" (in {anchor['name']!r})" if anchor["origin"] else ""))
            if mouse is not None:
                mouse.move_to(ax, ay)
        elif op == "move":
            log.append(f"move {values[0]},{values[1]}")
            if mouse is not None:
                mouse.move(values[0], values[1])
        elif op == "scroll":
            log.append(f"scroll {values[0]}")
            if mouse is not None:
                mouse.scroll(values[0])
        elif op == "click":
            button = expand(args[0], variables, instr.line)
            count = as_number(args[1], variables, instr.line, "click count")
            log.append(f"click {button} x{count}")
            if mouse is not None:
                try:
                    mouse.click(button, count)
                except ValueError as exc:
                    raise MacroError(f"line {instr.line}: {exc}") from None
        else:  # mousedown / mouseup
            button = expand(args[0], variables, instr.line) if args[0] else None
            log.append(f"{op} {button or 'all'}")
            if mouse is not None:
                try:
                    (mouse.button_down(button) if op == "mousedown"
                     else mouse.button_up(button))
                except ValueError as exc:
                    raise MacroError(f"line {instr.line}: {exc}") from None
        return pc + 1
    if op == "jumpif":
        variable, comparison, value, target = args
        if variable not in variables:
            raise MacroError(f"line {instr.line}: ${variable} is not set yet")
        left = variables[variable]
        right = expand(value, variables, instr.line)
        hit = _compare(left, right, comparison, instr.line)
        log.append(f"jumpif {variable}={left!r} {comparison} {right!r} -> "
                   f"{'jump to ' + target if hit else 'continue'}")
        return labels[target] if hit else pc + 1
    if op == "anchor":
        target = expand(args[0], variables, instr.line)
        if target.lower() in ("none", "off", "screen"):
            anchor["origin"], anchor["name"] = None, None
            log.append("anchor cleared - coordinates are absolute again")
            return pc + 1
        if dry_run:
            anchor["origin"], anchor["name"] = (0, 0), target
            log.append(f"anchor {target!r} (dry run: origin assumed 0,0)")
            return pc + 1
        try:
            found = agent_client.window(target)
        except agent_client.AgentError as exc:
            raise MacroError(f"line {instr.line}: {exc}") from None
        box = found["client"]
        anchor["origin"], anchor["name"] = (box["x"], box["y"]), found["title"]
        log.append(f"anchor {found['title']!r} client origin "
                   f"{box['x']},{box['y']} size {box['width']}x{box['height']}")
        return pc + 1
    if op == "paste":
        text = expand(args[0], variables, instr.line)
        log.append(f"paste {text[:40]!r}{'...' if len(text) > 40 else ''}")
        if not dry_run:
            try:
                agent_client.set_clipboard(text)
            except agent_client.AgentError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
            kb.tap("ctrl+v")
        return pc + 1
    if op == "clip":
        if not dry_run:
            try:
                variables[args[0]] = agent_client.get_clipboard()
            except agent_client.AgentError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
        else:
            variables.setdefault(args[0], "")
        log.append(f"clip -> {args[0]} = {variables[args[0]][:40]!r}")
        return pc + 1
    if op == "getpixel":
        x, y = _anchored(as_number(args[0], variables, instr.line, "x"),
                         as_number(args[1], variables, instr.line, "y"), anchor)
        if dry_run:
            variables.setdefault(args[2], "#000000")
        else:
            try:
                variables[args[2]] = agent_client.pixel(x, y)
            except agent_client.AgentError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
        log.append(f"getpixel {x},{y} -> {args[2]} = {variables[args[2]]}")
        return pc + 1
    if op == "waitfor":
        kind, spec, timeout_raw = args
        limit = as_number(timeout_raw, variables, instr.line, "timeout") / 1000
        if kind == "window":
            wanted = expand(spec, variables, instr.line).lower()
            describe = f"window {wanted!r}"
        else:
            x, y = _anchored(as_number(spec[0], variables, instr.line, "x"),
                             as_number(spec[1], variables, instr.line, "y"), anchor)
            wanted = expand(spec[2], variables, instr.line).lower()
            describe = f"pixel {x},{y} == {wanted}"
        if dry_run:
            log.append(f"waitfor {describe} (up to {limit:g}s)")
            return pc + 1
        deadline = time.monotonic() + limit
        while True:
            try:
                if kind == "window":
                    current = agent_client.foreground() or {}
                    hit = wanted in (current.get("title", "") + " "
                                     + current.get("exe", "")).lower()
                else:
                    hit = (agent_client.pixel(x, y) or "").lower() == wanted
            except agent_client.AgentError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
            if hit:
                log.append(f"waitfor {describe} -> ready")
                return pc + 1
            if time.monotonic() > deadline:
                raise MacroError(
                    f"line {instr.line}: timed out after {limit:g}s waiting for "
                    f"{describe}")
            if stop is not None and stop.wait(0.2):
                raise MacroStopped("stopped while waiting", log)
            elif stop is None:
                time.sleep(0.2)
    if op == "focus":
        target = expand(args[0], variables, instr.line)
        log.append(f"focus {target!r}")
        if not dry_run:
            try:
                window = agent_client.focus(target)
            except agent_client.AgentError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
            log[-1] = f"focus -> {window.get('title', target)!r}"
        return pc + 1
    if op == "set":
        variables[args[0]] = expand(args[1], variables, instr.line)
        log.append(f"set {args[0]} = {variables[args[0]]!r}")
        return pc + 1
    if op == "add":
        # an unset counter starts at zero, so 'add n 1' works without priming it
        total = as_number(variables.get(args[0], "0"), variables, instr.line,
                          f"${args[0]}")
        total += as_number(args[1], variables, instr.line, "amount")
        variables[args[0]] = str(total)
        log.append(f"add {args[0]} -> {total}")
        return pc + 1
    if op == "type":
        text = expand(args[0], variables, instr.line)
        _check_typeable(text, table, instr.line)
        log.append(f"type {text!r}")
        if not dry_run:
            try:
                kb.type_string(text, should_stop=(stop.is_set if stop else None))
            except ValueError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
            if stop is not None and stop.is_set():
                raise MacroStopped("stopped while typing", log)
        return pc + 1
    if op == "key":
        combo = expand(args[0], variables, instr.line)
        _check_combo(combo, instr.line)
        log.append(f"key {combo}")
        if not dry_run:
            try:
                kb.tap(combo, hold=args[1])
            except ValueError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
        return pc + 1
    if op in ("hold", "release"):
        combo = expand(args[0], variables, instr.line) if args[0] else None
        if combo:
            _check_combo(combo, instr.line)
        log.append(f"{op} {combo or 'all'}")
        if not dry_run:
            try:
                kb.key_down(combo) if op == "hold" else kb.key_up(combo)
            except ValueError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
        return pc + 1
    if op == "delay":
        lo, hi = (a if isinstance(a, int)
                  else as_number(a, variables, instr.line, "delay") for a in args)
        if lo > hi:
            raise MacroError(f"line {instr.line}: delay min {lo} is above max {hi}")
        ms = random.randint(lo, hi)
        log.append(f"delay {ms}ms")
        if not dry_run:
            if stop is not None:
                if stop.wait(ms / 1000):
                    raise MacroStopped("stopped during delay", log)
            else:
                time.sleep(ms / 1000)
        return pc + 1
    if op == "goto":
        return labels[args[0]]
    if op == "repeat":
        count, end_idx = args
        if pc not in counters:
            counters[pc] = (count if isinstance(count, int)
                            else as_number(count, variables, instr.line, "repeat count"))
        if counters[pc] == 0:
            del counters[pc]
            return end_idx + 1
        return pc + 1
    if op == "end":
        start = args[0]
        if counters.get(start, 0) > 0:
            counters[start] -= 1
        if counters.get(start, -1) == 0:
            del counters[start]
            return pc + 1
        return start + 1
    if op == "chance":
        pct, inner = args
        if not isinstance(pct, int):
            pct = as_number(pct, variables, instr.line, "chance percent")
        if random.randint(1, 100) <= pct:
            return _step(inner, pc, kb, labels, counters, log, dry_run, stop,
                         variables, table, mouse, anchor)
        log.append(f"chance {pct}% skipped")
        return pc + 1
    if op == "oneof":
        choice = expand(random.choice(args[0]), variables, instr.line)
        _check_typeable(choice, table, instr.line)
        log.append(f"oneof -> {choice!r}")
        if not dry_run:
            try:
                kb.type_string(choice, should_stop=(stop.is_set if stop else None))
            except ValueError as exc:
                raise MacroError(f"line {instr.line}: {exc}") from None
        return pc + 1
    raise MacroError(f"line {instr.line}: cannot execute {op!r}")
