"""``python3 -m hmi_emu PROJECT_DIR`` (run from the ``tools`` directory) or ``python3 tools/hmi_emulator.py``."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading

from .app import Emulator
from .bridge import PtyLink, SocketLink
from .project import ProjectError


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hmi_emulator", description="Emulator of a USART HMI display for a portable project.")
    ap.add_argument("project", help="portable project directory (project.json, pages/, pictures/, fonts/)")
    ap.add_argument("--pty", nargs="?", const="", metavar="LINK", help="open a pseudo terminal; LINK is an optional "
                    "symlink to it (e.g. /dev/ttyS1, needs root); the real path is printed")
    ap.add_argument("--tcp", metavar="[HOST:]PORT", help="serve the UART on a TCP port (socat can turn it into a pty)")
    ap.add_argument("--unix", metavar="PATH", help="serve the UART on a unix socket")
    ap.add_argument("--http", default="127.0.0.1:8765", metavar="[HOST:]PORT", help="web UI (default 127.0.0.1:8765, 'off' disables)")
    ap.add_argument("--state-dir", metavar="DIR", help="keep the EEPROM (wepo/repo) here between runs")
    ap.add_argument("--watch", action="store_true", help="reload the project when its files change (stays on the page)")
    ap.add_argument("--no-boot-frame", action="store_true", help="do not send 00 00 00 / 88 at power-up")
    ap.add_argument("--bkcmd", type=int, default=2, choices=(0, 1, 2, 3), help="initial return-code mode (default 2: errors only)")
    args = ap.parse_args(argv)

    links = []
    try:
        if args.pty is not None:
            links.append(PtyLink(args.pty or None))
        if args.tcp:
            host, _, port = args.tcp.rpartition(":")
            links.append(SocketLink((host or "127.0.0.1", int(port))))
        if args.unix:
            links.append(SocketLink(args.unix))
        emu = Emulator(args.project, links, state_dir=args.state_dir, boot_frame=not args.no_boot_frame, bkcmd=args.bkcmd)
    except (ProjectError, OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    http = None
    if args.http != "off":
        host, _, port = args.http.rpartition(":")
        http = (host or "127.0.0.1", int(port))
    emu.start(http, watch=args.watch)
    for link in links:
        print("UART:", link.name, flush=True)
    if http:
        print("Web UI: http://%s:%d/" % http, flush=True)
    done = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: done.set())
    signal.signal(signal.SIGTERM, lambda *a: done.set())
    done.wait()
    emu.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
