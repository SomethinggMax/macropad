#!/usr/bin/env python3
"""Web UI for writing, storing and firing macros."""

import re
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import agent_client
from engine import MacroError, MacroStopped, run
from keyboard import Keyboard, NotConnectedError, host_state
from keycodes import catalogue

MACRO_DIR = Path(__file__).resolve().parent / "macros"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
SUFFIX = ".macro"

app = Flask(__name__)
run_lock = threading.Lock()
stop_event = threading.Event()
# Latest source line the runner is on, polled by the UI to highlight it.
progress = {"line": None, "op": None, "running": False}


def macro_path(name):
    """Resolve a macro name to a path, refusing anything outside MACRO_DIR."""
    if not SAFE_NAME.match(name or ""):
        raise ValueError("name must be letters, digits, spaces, _ or - (max 64)")
    path = (MACRO_DIR / (name + SUFFIX)).resolve()
    if path.parent != MACRO_DIR.resolve():
        raise ValueError("invalid name")
    return path


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    agent_client.set_host(request.remote_addr)
    state = host_state()
    found = agent_client.version() if agent_client.get_host() else 0
    return jsonify(state=state, connected=(state == "configured"),
                   agent_host=agent_client.get_host(),
                   agent_version=found, agent_wants=agent_client.WANT_VERSION,
                   agent_stale=bool(found and found < agent_client.WANT_VERSION))


@app.route("/api/probe")
def probe():
    """Where the pointer is and what colour is under it, for the pixel picker."""
    agent_client.set_host(request.remote_addr)
    try:
        at = agent_client.cursor()
        return jsonify(ok=True, x=at["x"], y=at["y"],
                       colour=agent_client.pixel(at["x"], at["y"]))
    except agent_client.AgentError as exc:
        return jsonify(ok=False, error=str(exc)), 503


@app.route("/api/sample", methods=["POST"])
def sample():
    """Read the colour where the pointer is, without the pointer in the way.

    GetPixel captures the cursor graphic as well as the screen, so a naive read
    returns the arrow's own black outline or white body instead of the pixel
    underneath. Park the pointer, read, then put it back.
    """
    agent_client.set_host(request.remote_addr)
    try:
        at = agent_client.cursor()
        x, y = at["x"], at["y"]

        parked = None
        for offset in (220, -220):
            moved = agent_client.set_cursor(x + offset, y + offset)
            if abs(moved["x"] - x) > 60 or abs(moved["y"] - y) > 60:
                parked = moved
                break
        if parked is None:  # nowhere to go, e.g. a tiny screen
            return jsonify(ok=False,
                           error="could not move the pointer clear of the sample"), 409

        time.sleep(0.12)  # let the screen redraw without the cursor over it
        colour = agent_client.pixel(x, y)
        agent_client.set_cursor(x, y)
        return jsonify(ok=True, x=x, y=y, colour=colour)
    except agent_client.AgentError as exc:
        return jsonify(ok=False, error=str(exc)), 503


@app.route("/api/region")
def region():
    """Pixels around the pointer, so the UI can show a zoomed grid to click.

    A grid is more reliable than reading the pixel under the cursor: the cursor
    graphic is captured too, and on an elevated window the pointer cannot be
    moved out of the way at all.
    """
    agent_client.set_host(request.remote_addr)
    size = max(8, min(int(request.args.get("size", 32)), 128))
    try:
        at = agent_client.cursor()
        left, top = at["x"] - size // 2, at["y"] - size // 2
        block = agent_client.region(left, top, size, size)
        return jsonify(ok=True, cursor=at, left=left, top=top,
                       size=size, rgb=block["rgb"])
    except agent_client.AgentError as exc:
        return jsonify(ok=False, error=str(exc)), 503


@app.route("/api/keys")
def keys():
    return jsonify(groups=catalogue())


@app.route("/api/windows")
def windows():
    agent_client.set_host(request.remote_addr)
    try:
        return jsonify(ok=True, windows=agent_client.windows())
    except agent_client.AgentError as exc:
        return jsonify(ok=False, error=str(exc), windows=[]), 503


@app.route("/api/focus", methods=["POST"])
def focus_window():
    agent_client.set_host(request.remote_addr)
    target = (request.json or {}).get("target")
    try:
        return jsonify(ok=True, window=agent_client.focus(target))
    except agent_client.AgentError as exc:
        return jsonify(ok=False, error=str(exc)), 503


@app.route("/agent")
def download_agent():
    path = Path(__file__).resolve().parent / "agent" / "windows-agent.py"
    return app.response_class(
        path.read_text(), mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=windows-agent.py"})


@app.route("/api/macros", methods=["GET"])
def list_macros():
    MACRO_DIR.mkdir(exist_ok=True)
    names = sorted(p.stem for p in MACRO_DIR.glob("*" + SUFFIX))
    return jsonify(macros=names)


@app.route("/api/macros/<name>", methods=["GET", "PUT", "DELETE"])
def macro(name):
    try:
        path = macro_path(name)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    if request.method == "GET":
        if not path.exists():
            return jsonify(error="not found"), 404
        return jsonify(name=name, source=path.read_text())

    if request.method == "PUT":
        source = (request.json or {}).get("source", "")
        try:
            run(source, dry_run=True, max_steps=2000)
        except (MacroError, ValueError) as exc:
            return jsonify(error=f"not saved - {exc}"), 400
        MACRO_DIR.mkdir(exist_ok=True)
        path.write_text(source)
        return jsonify(ok=True, name=name)

    if path.exists():
        path.unlink()
    return jsonify(ok=True)


@app.route("/api/check", methods=["POST"])
def check():
    source = (request.json or {}).get("source", "")
    try:
        return jsonify(ok=True, log=run(source, dry_run=True, max_steps=5000))
    except (MacroError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route("/api/run", methods=["POST"])
def run_macro():
    data = request.json or {}
    source = data.get("source", "")
    delay = min(max(float(data.get("delay", 3)), 0), 300)

    if not run_lock.acquire(blocking=False):
        return jsonify(ok=False, error="a macro is already running"), 409
    try:
        state = host_state()
        if state != "configured":
            return jsonify(
                ok=False, error=f"host not connected (USB state: {state})"), 409

        stop_event.clear()
        target = data.get("target")
        focused = None
        if target:
            try:
                focused = agent_client.focus(target)
            except agent_client.AgentError as exc:
                return jsonify(ok=False, error=str(exc)), 503
            # let the window manager settle before the first keystroke
            if stop_event.wait(0.25):
                return jsonify(ok=True, stopped=True, log=["stopped before start"])
        if stop_event.wait(delay):  # stoppable during the countdown too
            return jsonify(ok=True, stopped=True, log=["stopped before start"])

        def track(line, op):
            progress["line"], progress["op"] = line, op

        progress.update(line=None, op=None, running=True)
        log = run(source, max_steps=200_000, max_seconds=300, stop=stop_event,
                  on_step=track)
        if focused:
            log.insert(0, f"focused {focused.get('title')!r}")
        return jsonify(ok=True, log=log)
    except MacroStopped as exc:  # must precede MacroError - it is a subclass
        return jsonify(ok=True, stopped=True, log=exc.log + ["** STOPPED **"])
    except (MacroError, ValueError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except NotConnectedError as exc:
        return jsonify(ok=False, error=f"connection lost: {exc}"), 409
    finally:
        progress.update(line=None, op=None, running=False)
        run_lock.release()


@app.route("/api/progress")
def get_progress():
    return jsonify(**progress)


@app.route("/api/stop", methods=["POST"])
def stop_macro():
    """Emergency stop: abort any running macro and drop every held key."""
    stop_event.set()
    released = False
    if run_lock.acquire(blocking=False):
        # Nothing is running, so clear any keys a previous crash left down.
        try:
            with Keyboard() as kb:
                kb.release_all()
            released = True
        except NotConnectedError:
            pass
        finally:
            run_lock.release()
    return jsonify(ok=True, released=released)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
