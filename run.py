#!/usr/bin/env python3
"""Run a macro script against the USB HID gadget."""

import argparse
import sys

from engine import MacroError, run
from keyboard import NotConnectedError, host_state


def main():
    ap = argparse.ArgumentParser(description="Run a macro script.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("file", nargs="?", help="macro file to run")
    src.add_argument("-c", "--command", help="inline macro source")
    ap.add_argument("-n", "--dry-run", action="store_true", help="parse and trace only")
    ap.add_argument("-d", "--delay", type=float, default=0.0,
                    help="seconds to wait before starting (focus your window)")
    ap.add_argument("--max-steps", type=int, default=100_000)
    ap.add_argument("--max-seconds", type=float, default=60.0)
    args = ap.parse_args()

    source = args.command if args.command else open(args.file).read()

    if not args.dry_run:
        state = host_state()
        if state != "configured":
            print(f"host not ready (UDC state={state}); is the USB-C cable connected?",
                  file=sys.stderr)
            return 2
        if args.delay:
            import time
            time.sleep(args.delay)

    try:
        log = run(source, dry_run=args.dry_run,
                  max_steps=args.max_steps, max_seconds=args.max_seconds)
    except MacroError as exc:
        print(f"macro error: {exc}", file=sys.stderr)
        return 1
    except NotConnectedError as exc:
        print(f"connection lost: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for entry in log:
            print(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
