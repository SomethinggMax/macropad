"""Talks to the window agent running on the PC.

The PC's address is not configured anywhere: the Flask app records the address
the browser connects from, which is the same machine the agent runs on.
"""

import fcntl
import json
from pathlib import Path
from urllib.parse import quote
import re
import socket
import struct
import subprocess
import urllib.error
import urllib.request

PORT = 8765
TIMEOUT = 4.0
WANT_VERSION = 5
# Remembered across restarts: the host is normally learned from the browser, so
# without this every service restart would break agent features until someone
# reloaded the IDE.
STATE = Path(__file__).resolve().parent / ".agent-host"


def _load_host():
    try:
        saved = STATE.read_text().strip()
    except OSError:
        return None
    return saved or None


_host = _load_host()


class AgentError(RuntimeError):
    """The agent is unreachable or refused the request."""


SIOCGIFADDR = 0x8915


def _own_addresses():
    """Every IPv4 address on this Pi - a request from ourselves is not the PC.

    getaddrinfo() only reports loopback, and SIOCGIFADDR only reports the first
    address per interface, so secondaries would slip through. Ask iproute2.
    """
    found = {"127.0.0.1", "::1", "localhost"}
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=3).stdout
        found.update(re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out))
        return found
    except (OSError, subprocess.SubprocessError):
        pass
    try:  # fallback if iproute2 is unavailable
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return found
    try:
        for _, name in socket.if_nameindex():
            try:
                packed = fcntl.ioctl(sock.fileno(), SIOCGIFADDR,
                                     struct.pack("256s", name[:15].encode()))
                found.add(socket.inet_ntoa(packed[20:24]))
            except OSError:
                pass
    finally:
        sock.close()
    return found


def set_host(host):
    """Remember where the PC is, learned from the browser's own connection."""
    global _host
    if host and host not in _own_addresses() and host != _host:
        _host = host
        try:
            STATE.write_text(host)
        except OSError:
            pass  # remembering is a convenience, not a requirement


def get_host():
    return _host


def _call(path, payload=None, timeout=TIMEOUT):
    if not _host:
        raise AgentError(
            "no PC agent known yet - open the macro IDE in a browser on the PC "
            "so the Pi learns its address, and make sure windows-agent.py is running")
    url = f"http://{_host}:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except ValueError:
            raise AgentError(f"agent returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AgentError(
            f"cannot reach the agent at {_host}:{PORT} - is windows-agent.py "
            f"running on the PC? ({exc})") from None


def ping():
    return _call("/ping")


def screen():
    """Virtual desktop geometry from the PC, or None when the agent is absent."""
    try:
        info = _call("/screen", timeout=1.5)  # never stall a macro on this
    except AgentError:
        return None
    return info if info.get("ok") else None


def cursor():
    """Where the pointer actually is, in Windows coordinates."""
    result = _call("/cursor", timeout=2.0)
    if not result.get("ok"):
        raise AgentError(
            result.get("error") or "agent has no /cursor - re-download it")
    return result


def set_cursor(x, y):
    """Place the pointer exactly, bypassing acceleration and monitor gaps."""
    result = _call("/cursor", {"x": int(x), "y": int(y)}, timeout=2.0)
    if not result.get("ok"):
        raise AgentError(result.get("error") or "agent could not move the cursor")
    return result


def foreground():
    result = _call("/foreground", timeout=2.0)
    if "window" not in result:
        raise AgentError(
            result.get("error") or "agent has no /foreground - re-download it")
    return result["window"]


def get_clipboard():
    result = _call("/clipboard", timeout=3.0)
    if not result.get("ok"):
        raise AgentError(
            result.get("error") or "agent cannot read the clipboard - re-download it")
    return result.get("text", "")


def set_clipboard(text):
    result = _call("/clipboard", {"text": str(text)}, timeout=3.0)
    if not result.get("ok"):
        raise AgentError("the PC refused to set the clipboard")
    return True


def version():
    """Agent protocol version, or 0 when it is too old to report one."""
    try:
        return int(ping().get("version") or 0)
    except (AgentError, TypeError, ValueError):
        return 0


def window(target):
    """Resolve a window by title or exe, with its screen and client rectangles."""
    result = _call(f"/window?target={quote(str(target))}", timeout=3.0)
    if not result.get("ok") or not result.get("window"):
        raise AgentError(result.get("error") or f"no window matching {target!r}")
    return result["window"]


def region(x, y, w=32, h=32):
    """A block of screen pixels as base64 RGB, centred on demand by the caller."""
    result = _call(f"/region?x={int(x)}&y={int(y)}&w={int(w)}&h={int(h)}", timeout=4.0)
    if not result.get("ok"):
        raise AgentError(result.get("error") or "agent has no /region - re-download it")
    return result


def pixel(x, y):
    result = _call(f"/pixel?x={int(x)}&y={int(y)}", timeout=2.0)
    if not result.get("ok"):
        raise AgentError(result.get("error") or "could not read that pixel")
    return result["colour"]


def windows():
    return _call("/windows").get("windows", [])


def focus(target):
    result = _call("/focus", {"target": target})
    if not result.get("ok"):
        message = result.get("error") or f"could not focus {target!r}"
        others = result.get("ambiguous") or []
        if len(others) > 1:
            choices = "; ".join(
                f"#{o['n']} {o.get('size', '?')} {o['exe']}" for o in others)
            message += f" - {len(others)} windows match, disambiguate with {choices}"
        raise AgentError(message)
    return result.get("window", {})
