"""The emulated display: pages, components, instruction execution, timers, touch, the UART protocol."""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from . import lang
from .lang import HmiError, Num, Ref, Str
from .project import Project

END = b"\xff\xff\xff"
TEXT_ATTRS = {"txt", "path"}
SYSTEM = {"dp", "dim", "dims", "bkcmd", "sleep", "thsp", "thup", "usup", "bauds", "baud", "delay", "sendxy", "wup",
          "ussp", "rand", "loadpageid", "loadcmpid", "dp_"}
# attribute that holds "the value" of a component when only its name is written
VALUE_ATTR = {"text": "txt", "variable": None}
TWFILE_MAGIC = bytes([0x3A, 0xA1, 0xBB, 0x44, 0x7F, 0xFF, 0xFE])
MAX_DEPTH = 24


def _type_codes():
    try:
        from hmi_parse import TYPES
    except ImportError:         # pragma: no cover - the tools directory is on the path when the emulator runs
        return {}
    return {name: code for code, name in TYPES.items()}


_TYPE_CODES = _type_codes()


class _PageChanged(lang.Stop):
    pass


class RObj:
    """Runtime state of one component."""

    def __init__(self, defn):
        self.defn = defn
        self.attrs = dict(defn.attrs)
        self.visible = True
        self.touch = True
        self.pressed = False
        self.next_due = None
        self.started = time.monotonic()

    @property
    def type(self):
        return self.defn.type

    @property
    def name(self):
        return self.defn.name


class Device:
    def __init__(self, project: Project, send=None, state_dir=None, boot_frame=True, sleep=time.sleep,
                 clock=time.monotonic, bkcmd=2, start=True):
        self.project = project
        self._send = send or (lambda data: None)
        self.state_dir = state_dir
        self.boot_frame = boot_frame
        self._sleep = sleep
        self.clock = clock
        self.lock = threading.RLock()
        self.log = []                       # (direction, text) for the UI, newest last
        self.on_event = None                # callback(kind, text)
        self.default_bkcmd = bkcmd
        self.version = 0
        if start:
            self.power_on()

    # -- state ---------------------------------------------------------------------------------------------------

    def power_on(self):
        with self.lock:
            self.globals = {}
            self.sysvars = {"dim": 100, "dims": 100, "bkcmd": self.default_bkcmd, "sleep": 0, "thsp": 0, "thup": 0,
                            "usup": 0, "bauds": 9600, "baud": 9600, "delay": 0, "sendxy": 0, "wup": 255, "ussp": 0,
                            "rand": 0, "loadpageid": 0, "loadcmpid": 0}
            self.eeprom = self._load_eeprom()
            self.files = {}                 # the display's SD / RAM files written with twfile
            self.states = {}                # page index -> {objname: RObj}
            self.page = None
            self.canvas = []                # drawing instructions of the current page
            self.cache = {}
            self.rx = bytearray()
            self.mode = "cmd"
            self.mode_info = {}
            self.depth = 0
            self.pressed = None
            self.exec_page = None
            self.source_id = 0
            self.error_count = 0
            self.last_error = None
            self.written = {}               # col_pic / cp data per component name
            self._bump()
            if self.boot_frame:
                self._out(b"\x00\x00\x00" + END)
            try:
                lines = self.project.program_lines
                self._exec_lines(lines, None, source="Program.s")
            except _PageChanged:
                pass
            except HmiError as exc:
                self._error("Program.s: %s" % exc)
            if self.page is None and self.project.pages:
                self._switch(0)
            if self.boot_frame:
                self._out(b"\x88" + END)

    def _load_eeprom(self):
        if self.state_dir:
            try:
                with open(os.path.join(self.state_dir, "eeprom.json"), encoding="utf-8") as f:
                    return {int(k): int(v) for k, v in json.load(f).items()}
            except (OSError, ValueError):
                pass
        return {}

    def _save_eeprom(self):
        if self.state_dir:
            try:
                os.makedirs(self.state_dir, exist_ok=True)
                with open(os.path.join(self.state_dir, "eeprom.json"), "w", encoding="utf-8") as f:
                    json.dump(self.eeprom, f)
            except OSError:
                pass

    def _bump(self):
        self.version += 1

    # -- output --------------------------------------------------------------------------------------------------

    def _out(self, data):
        data = bytes(data)
        self._note("tx", data)
        self._send(data)

    def _note(self, direction, data):
        self.log.append((direction, bytes(data), time.time()))
        if len(self.log) > 2000:
            del self.log[:500]
        if self.on_event:
            self.on_event("uart", (direction, bytes(data)))

    def _error(self, text):
        self.error_count += 1
        self.last_error = text
        self.log.append(("err", text.encode("utf-8", "replace"), time.time()))
        if self.on_event:
            self.on_event("error", text)

    # -- pages and components --------------------------------------------------------------------------------------

    def page_def(self):
        return self.project.pages[self.page] if self.page is not None else None

    def _state(self, index):
        st = self.states.get(index)
        if st is None:
            page = self.project.pages[index]
            st = {o.name: RObj(o) for o in page.all}
            st["(page)"] = st[page.root.name]
            self.states[index] = st
        return st

    def _reset_page(self, index):
        page = self.project.pages[index]
        old = self.states.get(index)
        st = {}
        for o in page.all:
            if old is not None and o.attrs.get("vscope") == 1 and o.name in old and o.type != "page":
                st[o.name] = old[o.name]                      # a global component keeps its state
            else:
                st[o.name] = RObj(o)
        st["(page)"] = st[page.root.name]
        self.states[index] = st
        return st

    def objects(self, index=None):
        """Runtime components of a page in id order (the page object first)."""
        index = self.page if index is None else index
        st = self._state(index)
        return [st[o.name] for o in self.project.pages[index].all]

    def _find_global(self, name):
        for idx, st in self.states.items():
            o = st.get(name)
            if o is not None and o.attrs.get("vscope") == 1:
                return o
        for page in self.project.pages:
            d = page.by_name.get(name)
            if d is not None and d.attrs.get("vscope") == 1:
                return self._state(page.index)[name]
        return None

    def _switch(self, index):
        if not 0 <= index < len(self.project.pages):
            raise HmiError("invalid page id %d" % index, 0x03)
        if self.depth > MAX_DEPTH:
            raise HmiError("page chain too deep", 0x1b)
        self.depth += 1
        try:
            if self.page is not None:
                try:
                    self.fire(self.objects()[0], "codesunload")
                except _PageChanged:
                    pass
            if self.page is not None:
                # the page that opened this one and the component whose event did it (the keyboards read both)
                self.sysvars["loadpageid"] = self.page
                self.sysvars["loadcmpid"] = self.source_id
            self.page = index
            self.canvas = []
            self.pressed = None
            self._reset_page(index)
            for o in self.objects():
                if o.type == "timer":
                    o.next_due = self.clock() + max(50, int(o.attrs.get("tim", 100))) / 1000.0
            self._bump()
            try:
                self.fire(self.objects()[0], "codesload")
            except _PageChanged:
                return
            self._bump()
            try:
                self.fire(self.objects()[0], "codesloadend")
            except _PageChanged:
                return
        finally:
            self.depth -= 1

    # -- code execution --------------------------------------------------------------------------------------------

    def _parsed(self, key, lines):
        prog = self.cache.get(key)
        if prog is None:
            try:
                prog = lang.parse(lines)
            except HmiError as exc:
                raise HmiError("%s: %s" % (key[0] if isinstance(key, tuple) else key, exc), 0x00)
            self.cache[key] = prog
        return prog

    def _exec_lines(self, lines, page, source):
        prog = self._parsed(("lines", source, id(lines)), lines)
        saved = self.exec_page
        self.exec_page = self.page if page is None else page
        try:
            lang.run(prog, self)
        finally:
            self.exec_page = saved

    def fire(self, robj, event):
        lines = robj.defn.events.get(event)
        if not lines:
            return
        key = (robj.defn.page, robj.defn.id, event)
        saved = self.exec_page
        saved_source = self.source_id
        self.exec_page = self.page
        if event in ("codesdown", "codesup", "codestimer", "codesslide"):
            self.source_id = robj.defn.id
        try:
            prog = self._parsed(key, lines)
            lang.run(prog, self)
        except _PageChanged:
            raise
        except HmiError as exc:
            self._error("%s.%s %s: %s" % (robj.defn.page, robj.name, event, exc))
            self._reply_error(exc.code)
        finally:
            self.exec_page = saved
            self.source_id = saved_source
        self._bump()

    def value(self, node):
        return lang.evaluate(node, self)

    # -- name resolution -------------------------------------------------------------------------------------------

    def _component(self, page_index, ident):
        objs = self.objects(page_index)
        if not 0 <= ident < len(objs):
            raise HmiError("invalid component id %d" % ident, 0x02)
        return objs[ident]

    def _locate(self, ref):
        """-> ("obj", RObj, attr|None) | ("sys", name) | ("glob", name)"""
        parts = list(ref.parts)
        cur = self.exec_page if self.exec_page is not None else self.page
        first = parts[0]
        if first == "p" and len(parts) >= 2 and not isinstance(parts[1], str):
            pid = int(self.value(parts[1]))
            if not 0 <= pid < len(self.project.pages):
                raise HmiError("invalid page id", 0x03)
            rest = parts[2:]
            if rest and rest[0] == "b" and len(rest) >= 2 and not isinstance(rest[1], str):
                obj = self._component(pid, int(self.value(rest[1])))
                rest = rest[2:]
            else:
                obj = self._component(pid, 0)
            return ("obj", obj, rest[0] if rest else None)
        if first == "b" and len(parts) >= 2 and not isinstance(parts[1], str):
            obj = self._component(cur, int(self.value(parts[1])))
            return ("obj", obj, parts[2] if len(parts) > 2 else None)
        if first in self.project.page_by_name and len(parts) >= 2:
            page = self.project.page_by_name[first]
            if parts[1] in page.by_name:
                obj = self._state(page.index)[parts[1]]
                return ("obj", obj, parts[2] if len(parts) > 2 else None)
            return ("obj", self._state(page.index)[page.root.name], parts[1])
        if cur is not None:
            st = self._state(cur)
            if first in st and first != "(page)":
                return ("obj", st[first], parts[1] if len(parts) > 1 else None)
            if first == self.project.pages[cur].name:           # the page object by its name
                return ("obj", st["(page)"], parts[1] if len(parts) > 1 else None)
        if first in self.globals:
            return ("glob", first)
        if first in self.sysvars or first == "dp":
            return ("sys", first)
        g = self._find_global(first)
        if g is not None:
            return ("obj", g, parts[1] if len(parts) > 1 else None)
        raise HmiError("invalid variable name %s" % first, 0x1a)

    def read(self, ref):
        kind, *rest = self._locate(ref)
        if kind == "glob":
            return self.globals[rest[0]]
        if kind == "sys":
            return self.page if rest[0] == "dp" else self.sysvars[rest[0]]
        obj, attr = rest
        if attr is None:
            attr = "txt" if obj.type == "text" and "txt" in obj.attrs else "val"
        if attr not in obj.attrs:
            if attr == "val" and "txt" in obj.attrs and obj.type == "variable":
                return obj.attrs["txt"]
            if attr == "type":
                return _TYPE_CODES.get(obj.type, 0)
            if attr == "id":
                return obj.defn.id
            raise HmiError("invalid attribute %s.%s" % (obj.name, attr), 0x1a)
        return obj.attrs[attr]

    def write(self, ref, value):
        kind, *rest = self._locate(ref)
        if kind == "glob":
            if isinstance(value, str):
                raise HmiError("a text cannot be stored in an int", 0x1b)
            self.globals[rest[0]] = _wrap32(value)
        elif kind == "sys":
            name = rest[0]
            if name == "dp":
                raise HmiError("dp is read only", 0x1b)
            if isinstance(value, str):
                raise HmiError("a text cannot be stored in %s" % name, 0x1b)
            self.sysvars[name] = _wrap32(value)
            if name == "delay":
                self.delay(max(0, self.sysvars["delay"]))
            if name == "dim":
                self.sysvars["dim"] = max(0, min(100, self.sysvars["dim"]))
        else:
            obj, attr = rest
            if attr is None:
                attr = "txt" if "txt" in obj.attrs and obj.type in ("text", "variable") else "val"
            self._set_attr(obj, attr, value)
        self._bump()

    def _set_attr(self, obj, attr, value):
        if attr not in obj.attrs:
            if attr == "val" and obj.type == "variable" and "txt" in obj.attrs:
                attr = "txt"
            else:
                raise HmiError("invalid attribute %s.%s" % (obj.name, attr), 0x1a)
        old = obj.attrs[attr]
        if isinstance(old, str) or attr in TEXT_ATTRS:
            value = str(value)
            cap = obj.attrs.get("txt_maxl")
            if attr == "txt" and isinstance(cap, int) and cap > 0:
                raw = value.encode("utf-8")
                if len(raw) > cap:
                    value = raw[:cap].decode("utf-8", "ignore")
        else:
            if isinstance(value, str):
                raise HmiError("a text cannot be stored in %s.%s" % (obj.name, attr), 0x1b)
            value = int(value)
        obj.attrs[attr] = value
        if obj.type == "timer" and attr in ("en", "tim"):
            obj.next_due = self.clock() + max(50, int(obj.attrs.get("tim", 100))) / 1000.0
        if obj.type == "animation" and attr == "en":
            obj.started = self.clock()

    # -- commands --------------------------------------------------------------------------------------------------

    def _obj_arg(self, node):
        """A component named or numbered by an instruction argument."""
        cur = self.exec_page if self.exec_page is not None else self.page
        if isinstance(node, Ref) and len(node.parts) == 1 and isinstance(node.parts[0], str):
            st = self._state(cur)
            if node.parts[0] in st:
                return st[node.parts[0]]
            g = self._find_global(node.parts[0])
            if g is not None:
                return g
            raise HmiError("invalid component %s" % node.parts[0], 0x02)
        if isinstance(node, Ref):
            kind, *rest = self._locate(node)
            if kind == "obj" and rest[1] is None:
                return rest[0]
        ident = self.value(node)
        if isinstance(ident, str):
            raise HmiError("invalid component", 0x02)
        if ident == 255:
            return None
        return self._component(cur, ident)

    def _ints(self, args, n):
        vals = [self.value(a) for a in args[:n]]
        return vals

    def command(self, name, args, raw):
        if name == "int":
            for decl in args:
                key = decl.target.parts[0]
                self.globals[key] = _wrap32(self.value(decl.expr)) if decl.expr is not None else 0
            return
        if name == ".call":
            obj = self._obj_arg(args[0])
            method = args[1].value
            if method == "close":
                self.written.pop(obj.name, None)
            elif method == "write":
                v = self.value(args[2]) if len(args) > 2 else ""
                self.written[obj.name] = v.encode("utf-8") if isinstance(v, str) else bytes([int(v) & 255])
            else:
                raise HmiError("unknown method %s" % method, 0x00)
            self._bump()
            return
        if name == "page":
            if len(args) != 1:
                raise HmiError("page needs one argument")
            arg = args[0]
            if isinstance(arg, Ref) and len(arg.parts) == 1 and arg.parts[0] in self.project.page_by_name:
                idx = self.project.page_by_name[arg.parts[0]].index
            else:
                idx = self.value(arg)
                if isinstance(idx, str):
                    if idx not in self.project.page_by_name:
                        raise HmiError("invalid page %s" % idx, 0x03)
                    idx = self.project.page_by_name[idx].index
            self._switch(int(idx))
            raise _PageChanged()
        if name in ("vis", "tsw"):
            obj = self._obj_arg(args[0])
            state = int(self.value(args[1]))
            targets = self.objects()[1:] if obj is None else [obj]
            for o in targets:
                if name == "vis":
                    o.visible = bool(state)
                else:
                    o.touch = bool(state)
            self._bump()
            return
        if name == "click":
            obj = self._obj_arg(args[0])
            event = "codesdown" if int(self.value(args[1])) else "codesup"
            if obj is not None:
                if self.depth > MAX_DEPTH:
                    raise HmiError("click chain too deep", 0x1b)
                self.depth += 1
                try:
                    self.fire(obj, event)
                finally:
                    self.depth -= 1
            return
        if name in ("ref", "ref_stop", "ref_star", "refresh", "doevents", "com_star", "com_stop", "randset", "setlayer",
                    "addt", "cle", "play", "stop", "sendxy"):
            if name == "cle" and args:
                obj = self._obj_arg(args[0])
                self.written.pop(obj.name, None) if obj is not None else None
            self._bump()
            return
        if name == "prints":
            self._prints(args)
            return
        if name == "print":
            v = self.value(args[0])
            self._out(v.encode("utf-8") if isinstance(v, str) else struct.pack("<i", _wrap32(v)))
            return
        if name == "printh":
            self._out(bytes.fromhex("".join(re.findall(r"[0-9A-Fa-f]{2}", raw.split(None, 1)[1] if " " in raw else ""))))
            return
        if name in ("wepo", "wept"):
            val = self.value(args[0])
            addr = int(self.value(args[1]))
            self.eeprom[addr] = _wrap32(val) if not isinstance(val, str) else val
            self._save_eeprom()
            return
        if name in ("repo", "rept"):
            addr = int(self.value(args[1]))
            self.write(args[0], self.eeprom.get(addr, -1))
            return
        if name in ("covx", "cov"):
            src = self.value(args[0])
            length = int(self.value(args[2])) if len(args) > 2 else 0
            if isinstance(src, str):
                try:
                    out = int(src.strip() or "0", 0) if src.strip().lstrip("-").isdigit() or src.strip().lower().startswith("0x") else int(float(src))
                except ValueError:
                    out = 0
            else:
                out = str(src).rjust(length, "0") if length else str(src)
            self.write(args[1], out)
            return
        if name in ("btlen", "strlen"):
            v = self.value(args[0])
            self.write(args[1], len(v.encode("utf-8")) if isinstance(v, str) else len(str(v)))
            return
        if name == "substr":
            s = self.value(args[0])
            start, ln = int(self.value(args[2])), int(self.value(args[3]))
            raw_bytes = s.encode("utf-8")
            self.write(args[1], raw_bytes[start:start + ln].decode("utf-8", "ignore"))
            return
        if name == "spstr":
            s, delim, idx = self.value(args[0]), self.value(args[2]), int(self.value(args[3]))
            pieces = s.split(delim) if delim else [s]
            self.write(args[1], pieces[idx] if 0 <= idx < len(pieces) else "")
            return
        if name == "get":
            v = self.value(args[0])
            if isinstance(v, str):
                self._out(b"\x70" + v.encode("utf-8") + END)
            else:
                self._out(b"\x71" + struct.pack("<i", _wrap32(v)) + END)
            return
        if name == "sendme":
            self._out(bytes([0x66, self.page or 0]) + END)
            return
        if name == "rest":
            raise _Reset()
        if name in ("cls", "pic", "picq", "xpic", "xstr", "fill", "line", "draw", "cir", "cirs"):
            self.canvas.append((name, [self.value(a) for a in args]))
            self._bump()
            return
        if name in ("twfile", "delfile", "whmi-wri"):
            raise HmiError("%s is a host instruction" % name)
        raise HmiError("unknown instruction %s" % name, 0x00)

    def _prints(self, args):
        v = self.value(args[0])
        n = int(self.value(args[1])) if len(args) > 1 else 0
        if isinstance(v, str):
            self._out(v.encode("utf-8"))
        else:
            n = n or 4
            self._out((_wrap32(v) & 0xFFFFFFFF).to_bytes(4, "little")[:max(1, min(4, n))])

    # -- UART from the host ----------------------------------------------------------------------------------------

    def feed(self, data):
        with self.lock:
            self._note("rx", data)
            self.rx += data
            self._process()

    def _process(self):
        while True:
            if self.mode == "cmd":
                pos = self.rx.find(END)
                if pos < 0:
                    return
                raw = bytes(self.rx[:pos])
                del self.rx[:pos + 3]
                self._instruction(raw)
            elif self.mode == "twfile":
                info = self.mode_info
                if len(self.rx) < 12:
                    return
                if bytes(self.rx[:7]) != TWFILE_MAGIC:
                    self._error("twfile: unexpected data")
                    self.mode = "cmd"
                    continue
                length = self.rx[10] | (self.rx[11] << 8)
                if self.rx[7] == 0x00 and bytes(self.rx[8:10]) == b"\xff\xff":
                    del self.rx[:12]
                    self.mode = "cmd"
                    continue
                if len(self.rx) < 12 + length:
                    return
                payload = bytes(self.rx[12:12 + length])
                del self.rx[:12 + length]
                info["data"] += payload[:-2]
                self._out(b"\x05" + END)
                if len(info["data"]) >= info["size"]:
                    self.files[info["path"]] = bytes(info["data"][:info["size"]])
                    self.mode = "cmd"
                    self._bump()
            elif self.mode == "download":
                info = self.mode_info
                if not self.rx:
                    return
                need = min(info["next"], info["size"]) - info["received"]
                take = self.rx[:need]
                del self.rx[:len(take)]
                info["received"] += len(take)
                if info["received"] >= info["size"]:
                    self.mode = "cmd"
                    self._error("firmware download ignored (%d bytes): the emulator runs the project directory" % info["size"])
                    self._out(b"\x05")
                elif info["received"] >= info["next"]:
                    info["next"] += 4096
                    self._out(b"\x05")

    def _instruction(self, raw):
        text = raw.decode("utf-8", "surrogateescape").strip()
        if not text:
            return
        try:
            if text.startswith("twfile "):
                path, size = text[7:].rsplit(",", 1)
                self.mode = "twfile"
                self.mode_info = {"path": path.strip().strip('"'), "size": int(size), "data": bytearray()}
                return
            if text.startswith("whmi-wri "):
                self.mode = "download"
                self.mode_info = {"size": int(text[9:].split(",")[0]), "received": 0, "next": 4096}
                self._out(b"\x05")
                return
            if text.startswith("delfile "):
                self.files.pop(text[8:].strip().strip('"'), None)
                self._reply_ok()
                return
            m = re.match(r"^([A-Za-z_][\w]*)\.write\(\"(.*)\"\)$", text, re.S)
            if m:
                self.written[m.group(1)] = raw[raw.index(b'.write("') + 8:-2]
                self._bump()
                return
            m = re.match(r"^([A-Za-z_][\w]*)\.close\(\)$", text)
            if m:
                self._bump()
                return
            if self.page is None:
                raise HmiError("no page", 0x00)
            prog = lang.parse([text])
            saved = self.exec_page
            self.exec_page = self.page
            try:
                lang.run(prog, self)
            except _PageChanged:
                pass
            finally:
                self.exec_page = saved
            self._reply_ok()
        except _Reset:
            self.power_on()
        except HmiError as exc:
            self._error("%s: %s" % (text, exc))
            self._reply_error(exc.code)
        except (ValueError, IndexError) as exc:
            self._error("%s: %s" % (text, exc))
            self._reply_error(0x00)
        self._bump()

    def _reply_ok(self):
        if self.sysvars["bkcmd"] in (1, 3):
            self._out(b"\x01" + END)

    def _reply_error(self, code):
        if self.sysvars["bkcmd"] in (2, 3):
            self._out(bytes([code & 0xFF]) + END)

    # -- touch and time --------------------------------------------------------------------------------------------

    def _hit(self, x, y):
        best = None
        for o in self.objects()[1:]:
            a = o.attrs
            if o.type in ("timer", "variable", "touch_capture") or not o.visible or not o.touch:
                continue
            if "x" not in a or "w" not in a:
                continue
            if not (a["x"] <= x < a["x"] + a["w"] and a["y"] <= y < a["y"] + a["h"]):
                continue
            ev = o.defn.events
            if not (ev.get("codesdown") or ev.get("codesup") or ev.get("codesslide") or o.type == "slider"):
                continue
            best = o
        return best

    def touch(self, x, y, action):
        """``action`` is "down", "move" or "up"."""
        with self.lock:
            if self.page is None or self.sysvars["sleep"]:
                if action == "down" and self.sysvars["sleep"] and self.sysvars["thup"]:
                    self.sysvars["sleep"] = 0
                    self._bump()
                return
            try:
                self._touch(x, y, action)
            except _PageChanged:
                pass
            self._bump()

    def _touch(self, x, y, action):
        objs = self.objects()
        captures = [o for o in objs if o.type == "touch_capture"]
        if action == "down":
            self.pressed = self._hit(x, y) or objs[0]
            self.pressed.pressed = True
            for c in captures:
                self.fire(c, "codesdown")
            if self.pressed.type == "slider":
                self._slide(self.pressed, x, y)
            self.fire(self.pressed, "codesdown")
        elif action == "move":
            if self.pressed is not None and self.pressed.type == "slider":
                self._slide(self.pressed, x, y)
        elif action == "up":
            target, self.pressed = self.pressed, None
            if target is not None:
                target.pressed = False
                if target.type == "slider":
                    self._slide(target, x, y, fire=False)
                for c in captures:
                    self.fire(c, "codesup")
                self.fire(target, "codesup")

    def _slide(self, o, x, y, fire=True):
        a = o.attrs
        lo, hi = a.get("minval", 0), a.get("maxval", 100)
        if a.get("mode", 0) == 1:
            frac = 1 - (y - a["y"]) / max(1, a["h"] - 1)
        else:
            frac = (x - a["x"]) / max(1, a["w"] - 1)
        val = int(round(lo + max(0.0, min(1.0, frac)) * (hi - lo)))
        if val != a.get("val"):
            a["val"] = val
            if fire:
                self.fire(o, "codesslide")

    def tick(self):
        """Run the timers that are due (call every ~10 ms)."""
        with self.lock:
            if self.page is None:
                return
            now = self.clock()
            st = self.states.get(self.page, {})
            for o in list(st.values()):
                if o.type == "timer" and o.attrs.get("en") and o.next_due is not None and now >= o.next_due:
                    o.next_due = now + max(50, int(o.attrs.get("tim", 100))) / 1000.0
                    try:
                        self.fire(o, "codestimer")
                    except _PageChanged:
                        break

    def delay(self, ms):
        self.lock.release()
        try:
            self._sleep(ms / 1000.0)
        finally:
            self.lock.acquire()

    # -- convenience for tests and the UI --------------------------------------------------------------------------

    def tap(self, x, y):
        self.touch(x, y, "down")
        self.touch(x, y, "up")

    def center(self, name, page=None):
        o = self._state(self.page if page is None else page)[name]
        a = o.attrs
        return a["x"] + a["w"] // 2, a["y"] + a["h"] // 2

    def attr(self, name, attr, page=None):
        return self._state(self.page if page is None else page)[name].attrs[attr]


class _Reset(Exception):
    pass


def _wrap32(v):
    v = int(v) & 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v
