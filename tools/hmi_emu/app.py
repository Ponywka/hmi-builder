"""Ties the pieces together: device + renderer + links + ticker + web server."""

from __future__ import annotations

import os
import threading
import time

from .bridge import Link
from .device import Device
from .project import Project
from .render import Renderer
from .web import describe, make_server


class Emulator:
    def __init__(self, project_dir, links=(), state_dir=None, boot_frame=True, bkcmd=2):
        self.project = Project(project_dir)
        self.renderer = Renderer(self.project)
        self.links = list(links)
        self._boot_frame = boot_frame
        self.seq = 0
        self.entries = []                       # (seq, dir, text)
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.device = Device(self.project, self._to_links, state_dir=state_dir, boot_frame=boot_frame, bkcmd=bkcmd, start=False)
        self.device.on_event = self._event
        for link in self.links:
            link.attach(self.device)
        self.device.power_on()
        self._running = False
        self.web = None

    # -- traffic ---------------------------------------------------------------------------------------------------

    def _to_links(self, data):
        for link in self.links:
            link.send(data)

    def send_to_host(self, data):
        self.device._out(data)

    def _event(self, kind, payload):
        if kind == "uart":
            direction, data = payload
            self._add("rx" if direction == "rx" else "tx", describe(data))
        elif kind == "error":
            self._add("err", payload)

    def _add(self, direction, text):
        with self._lock:
            self.seq += 1
            self.entries.append((self.seq, direction, text))
            if len(self.entries) > 3000:
                del self.entries[:1000]

    def state(self, since):
        with self._lock:
            log = [{"dir": d, "text": t} for s, d, t in self.entries if s > since]
            seq = self.seq
        dev = self.device
        with dev.lock:
            page = dev.page
            animating = any(o.type == "animation" and o.attrs.get("en") for o in dev.objects()) if page is not None else False
            return {
                "seq": seq, "log": log, "version": dev.version, "page": page,
                "pagename": self.project.pages[page].key if page is not None else "",
                "pages": [p.key for p in self.project.pages], "dim": dev.sysvars["dim"], "sleep": dev.sysvars["sleep"],
                "animating": animating, "links": [l.name for l in self.links],
                "errors": dev.error_count, "last_error": dev.last_error or "",
            }

    def variables(self):
        dev = self.device
        with dev.lock:
            objs = {}
            if dev.page is not None:
                for o in dev.objects()[1:]:
                    a = o.attrs
                    if o.type in ("text", "button", "number"):
                        v = a.get("txt") if o.type != "number" else a.get("val")
                        if v not in (None, ""):
                            objs[o.name] = v
                    elif o.type in ("variable", "slider", "progress_bar"):
                        objs[o.name] = a.get("val", a.get("txt"))
                    elif o.type == "timer":
                        objs[o.name] = "en=%s tim=%s" % (a.get("en"), a.get("tim"))
            return {"globals": dict(dev.globals), "objects": objs}

    # -- reload (after the project files changed) ------------------------------------------------------------------

    def reload(self):
        """Re-read the project directory; stays on the same page and keeps the global variables."""
        project = Project(self.project.dir)          # raises on a broken edit: the old project keeps running
        dev = self.device
        with dev.lock:
            key = self.project.pages[dev.page].key if dev.page is not None else None
            saved = dict(dev.globals)
            self.project = project
            self.renderer = Renderer(project)
            dev.project = project
            dev.boot_frame = False
            dev.power_on()
            dev.boot_frame = self._boot_frame
            for name, value in saved.items():
                if name in dev.globals:
                    dev.globals[name] = value
            if key in project.page_by_key and project.page_by_key[key].index != dev.page:
                dev._switch(project.page_by_key[key].index)
        self._add("err", "project reloaded")

    def _fingerprint(self):
        stamp = []
        base = self.project.dir
        for root, _, files in os.walk(base):
            for name in files:
                if name.endswith((".json", ".s", ".png", ".zi")):
                    path = os.path.join(root, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    stamp.append((path, st.st_mtime_ns, st.st_size))
        return hash(tuple(sorted(stamp)))

    def _watcher(self):
        last = self._fingerprint()
        while self._running:
            time.sleep(1.0)
            now = self._fingerprint()
            if now != last:
                time.sleep(0.4)                       # let an editor / build finish writing
                last = self._fingerprint()
                try:
                    self.reload()
                except Exception as exc:              # a half-written project: keep the old one, try on the next change
                    self._add("err", "reload failed: %s" % exc)

    # -- run -------------------------------------------------------------------------------------------------------

    def start(self, http=None, watch=False):
        self._running = True
        if watch:
            threading.Thread(target=self._watcher, daemon=True, name="watch").start()
        for link in self.links:
            link.start()
        threading.Thread(target=self._ticker, daemon=True, name="timers").start()
        if http:
            self.web = make_server(self, http[0], http[1])
            threading.Thread(target=self.web.serve_forever, daemon=True, name="http").start()
        return self

    def _ticker(self):
        while self._running:
            self.device.tick()
            time.sleep(0.01)

    def stop(self):
        self._running = False
        if self.web:
            self.web.shutdown()
        for link in self.links:
            link.stop()
