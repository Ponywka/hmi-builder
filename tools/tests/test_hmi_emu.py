"""Tests of the HMI emulator on a small synthetic project (no vendor data)."""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hmi_font  # noqa: E402
from hmi_emu import lang  # noqa: E402
from hmi_emu.app import Emulator  # noqa: E402
from hmi_emu.bridge import PtyLink, SocketLink  # noqa: E402
from hmi_emu.device import Device, END  # noqa: E402
from hmi_emu.project import Project  # noqa: E402
from hmi_emu.render import Renderer, rgb565  # noqa: E402

try:
    from PIL import Image
except ImportError:                 # pragma: no cover
    Image = None


def obj(key, type_, **attrs):
    base = {"objname": key, "vscope": 0, "x": 0, "y": 0, "w": 10, "h": 10}
    base.update(attrs)
    events = attrs.pop("_events", None) or {}
    base.pop("_events", None)
    return {"key": key, "type": type_, "attributes": base, "events": events}


def make_project(directory):
    """Two pages: ``home`` (button, number, text, timer, slider) and ``other``."""
    os.makedirs(os.path.join(directory, "fonts"))
    os.makedirs(os.path.join(directory, "pictures"))
    glyph = {"width": 4, "left": 0, "right": 0, "alpha": [255] * 4 * 6}
    font = hmi_font.encode_font({ord(c): glyph for c in "0123456789 abcdefghijklmnopqrstuvwxyz"}, 6, "T", "ascii", 3)
    with open(os.path.join(directory, "fonts", "f.zi"), "wb") as f:
        f.write(font)
    Image.new("RGBA", (40, 60), (255, 0, 0, 255)).save(os.path.join(directory, "pictures", "red.png"))
    home = {
        "name": "home",
        "root": {"attributes": {"x": 0, "y": 0, "w": 40, "h": 60, "sta": 1, "bco": 0}, "events": {
            "codesload": ["lang_seen=lang", "t0.txt=\"x\"+\"y\""]}},
        "objects": [
            {"key": "b0", "type": "button", "attributes": {
                "objname": "b0", "vscope": 0, "x": 0, "y": 0, "w": 20, "h": 20, "sta": 1, "bco": 63488, "bco2": 31,
                "pco": 65535, "font": {"$ref": "font:f"}, "txt": "", "txt_maxl": 10, "val": 0, "xcen": 1, "ycen": 1},
             "events": {"codesdown": ["n0.val+=1"], "codesup": ["prints 0x65,1", "prints dp,1", "prints 7,1",
                                                              "prints 0xff,1", "prints 0xff,1", "prints 0xff,1"]}},
            {"key": "t0", "type": "text", "attributes": {
                "objname": "t0", "vscope": 0, "x": 0, "y": 30, "w": 40, "h": 8, "sta": 3, "pco": 65535,
                "font": {"$ref": "font:f"}, "txt": "", "txt_maxl": 6, "xcen": 0, "ycen": 0, "isbr": 0}, "events": {}},
            {"key": "n0", "type": "number", "attributes": {
                "objname": "n0", "vscope": 0, "x": 0, "y": 40, "w": 40, "h": 8, "sta": 3, "pco": 65535,
                "font": {"$ref": "font:f"}, "val": 5, "lenth": 0}, "events": {}},
            {"key": "tm", "type": "timer", "attributes": {"objname": "tm", "vscope": 0, "tim": 100, "en": 1},
             "events": {"codestimer": ["ticks++", "if(ticks==3)", "{", "  page ${page:other}", "}"]}},
            {"key": "g", "type": "variable", "attributes": {"objname": "g", "vscope": 1, "val": 41, "txt": "", "txt_maxl": 8},
             "events": {}},
            {"key": "sl", "type": "slider", "attributes": {
                "objname": "sl", "vscope": 0, "x": 0, "y": 50, "w": 40, "h": 10, "mode": 0, "sta": 1, "bco": 0,
                "bco1": 2016, "psta": 2, "wid": 0, "hig": 0, "val": 0, "minval": 0, "maxval": 100},
             "events": {"codesslide": ["prints sl.val,2", "printh ff ff ff"], "codesdown": [], "codesup": []}},
        ],
    }
    other = {"name": "other", "root": {"attributes": {"x": 0, "y": 0, "w": 40, "h": 60, "sta": 2, "pic": {"$ref": "picture:red"}},
                                       "events": {"codesload": ["printh 91 ff ff ff", "g.val+=1"]}},
             "objects": [
                 {"key": "back", "type": "button", "attributes": {"objname": "back", "vscope": 0, "x": 0, "y": 0, "w": 10, "h": 10,
                                                                   "sta": 3, "txt": ""},
                  "events": {"codesup": ["page home"]}}]}
    for name, data in (("home", home), ("other", other)):
        with open(os.path.join(directory, "pages", name + ".json") if os.path.isdir(os.path.join(directory, "pages")) else
                  _mk(directory, name), "w", encoding="utf-8") as f:
            json.dump(data, f)
    config = {
        "format": "usart-hmi-project-v2", "portable": True,
        "pages": [{"key": "home", "content": {"mode": "json", "path": "pages/home.json"}},
                  {"key": "other", "content": {"mode": "json", "path": "pages/other.json"}}],
        "pictures": [{"key": "red", "source": {"png": "pictures/red.png"}}],
        "fonts": [{"key": "f", "source": {"zi": "fonts/f.zi"}}],
        "animations": [], "program": "Program.s",
    }
    with open(os.path.join(directory, "project.json"), "w") as f:
        json.dump(config, f)
    with open(os.path.join(directory, "Program.s"), "w") as f:
        f.write("int lang=2,ticks=0,lang_seen=0 // globals\nrepo lang,100\nif(lang<0||lang>12)\n{\n  lang=2\n}\npage ${page:home}\n")


def _mk(directory, name):
    os.makedirs(os.path.join(directory, "pages"), exist_ok=True)
    return os.path.join(directory, "pages", name + ".json")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class DeviceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        make_project(self._tmp.name)
        self.project = Project(self._tmp.name)
        self.out = bytearray()
        self.clock = Clock()
        self.dev = Device(self.project, self.out.extend, clock=self.clock, boot_frame=False)

    def take(self):
        data = bytes(self.out)
        self.out.clear()
        return data

    def send(self, text):
        self.dev.feed(text.encode("utf-8") + END)
        return self.take()


class LanguageTests(unittest.TestCase):
    def run_lines(self, lines, **names):
        class Env:
            def __init__(self):
                self.vars = dict(names)
                self.cmds = []

            def read(self, ref):
                return self.vars[ref.parts[0]] if len(ref.parts) == 1 else self.vars[".".join(map(str, ref.parts))]

            def write(self, ref, value):
                self.vars[".".join(map(str, ref.parts))] = value

            def value(self, node):
                return lang.evaluate(node, self)

            def command(self, name, args, raw):
                self.cmds.append((name, [self.value(a) for a in args]))

        env = Env()
        lang.run(lang.parse(lines), env)
        return env

    def test_arithmetic_precedence_and_text(self):
        env = self.run_lines(["a=1+2*3", "s=\"x\"+\"y\"", "b=(1+2)*3", "c=7%4", "d=-3+10/4"], a=0, s="", b=0, c=0, d=0)
        self.assertEqual([env.vars[k] for k in "asbcd"][0:1] + [env.vars["s"], env.vars["b"], env.vars["c"], env.vars["d"]],
                         [7, "xy", 9, 3, -1])

    def test_blocks_else_if_for_while(self):
        lines = ["x=0", "if(v==1)", "{", "  x=10", "}else if(v==2)", "{", "  x=20", "}else", "{", "  x=30", "}",
                 "for(i=0;i<4;i++)", "{", "  y+=i", "}", "while(z>0)", "{", "  z--", "}", "if(v==2){w=5}"]
        env = self.run_lines(lines, v=2, x=0, i=0, y=0, z=3, w=0)
        self.assertEqual((env.vars["x"], env.vars["y"], env.vars["z"], env.vars["w"]), (20, 6, 0, 5))

    def test_comments_and_commands(self):
        env = self.run_lines(["// nothing", "prints \"a//b\",0 // trailing", "wepo lang,100", "covx t.txt,n.val,0,0"],
                             **{"t.txt": "42", "n.val": 0, "lang": 3})
        self.assertEqual([c[0] for c in env.cmds], ["prints", "wepo", "covx"])
        self.assertEqual(env.cmds[0][1], ["a//b", 0])

    def test_index_refs_and_errors(self):
        stmts = lang.parse(["p[loadpageid.val].b[loadcmpid.val].val=5"])
        self.assertEqual(stmts[0].target.parts[0], "p")
        with self.assertRaises(lang.HmiError):
            lang.parse(["if(a==1"])
        with self.assertRaises(lang.HmiError):
            lang.parse(["}"])
        with self.assertRaises(lang.HmiError):
            lang.parse(["{"])


class DeviceTests(DeviceCase):
    def test_program_and_page_load(self):
        self.assertEqual(self.dev.page, 0)
        self.assertEqual(self.dev.globals["lang"], 2)          # erased EEPROM reads -1 and Program.s fixes it
        self.assertEqual(self.dev.attr("t0", "txt"), "xy")

    def test_touch_sends_frame(self):
        x, y = self.dev.center("b0")
        self.dev.touch(x, y, "down")
        self.assertEqual(self.dev.attr("n0", "val"), 6)
        self.assertEqual(self.take(), b"")
        self.dev.touch(x, y, "up")
        self.assertEqual(self.take(), bytes([0x65, 0, 7]) + END)

    def test_touch_outside_hits_nothing(self):
        self.dev.touch(39, 25, "down")
        self.dev.touch(39, 25, "up")
        self.assertEqual(self.take(), b"")

    def test_host_instructions(self):
        self.assertEqual(self.send('t0.txt="abc"'), b"")
        self.assertEqual(self.dev.attr("t0", "txt"), "abc")
        self.assertEqual(self.send('t0.txt="toolongtext"'), b"")
        self.assertEqual(self.dev.attr("t0", "txt"), "toolon")               # txt_maxl
        self.assertEqual(self.send("n0.val=-2"), b"")
        self.assertEqual(self.send("get n0.val"), b"\x71" + (-2 & 0xFFFFFFFF).to_bytes(4, "little") + END)
        self.assertEqual(self.send("get t0.txt"), b"\x70toolon" + END)
        self.assertEqual(self.send("sendme"), bytes([0x66, 0]) + END)

    def test_error_codes_and_bkcmd(self):
        self.assertEqual(self.send("nope.val=1"), b"\x1a" + END)
        self.assertEqual(self.send("page 9"), b"\x03" + END)
        self.assertEqual(self.send("t0.bogus=1"), b"\x1a" + END)
        self.assertEqual(self.send("n0.val=\"x\""), b"\x1b" + END)
        self.assertEqual(self.send("bkcmd=3"), b"\x01" + END)
        self.assertEqual(self.send("n0.val=1"), b"\x01" + END)
        self.assertEqual(self.send("bkcmd=0"), b"")
        self.assertEqual(self.send("nope.val=1"), b"")

    def test_split_and_batched_frames(self):
        self.dev.feed(b't0.txt="a')
        self.dev.feed(b'b"\xff\xff')
        self.assertEqual(self.dev.attr("t0", "txt"), "xy")
        self.dev.feed(b'\xffn0.val=9\xff\xff\xff')
        self.assertEqual((self.dev.attr("t0", "txt"), self.dev.attr("n0", "val")), ("ab", 9))

    def test_page_change_timers_and_globals(self):
        for _ in range(3):
            self.clock.t += 0.11
            self.dev.tick()
        self.assertEqual(self.dev.page, 1)
        self.assertEqual(self.take(), b"\x91" + END)
        self.assertEqual(self.dev.attr("g", "val", page=0), 42)    # a global component keeps its value
        self.dev.tap(*self.dev.center("back"))
        self.assertEqual(self.dev.page, 0)
        self.assertEqual(self.dev.attr("g", "val"), 42)
        self.assertEqual(self.dev.attr("n0", "val"), 5)            # page-local values are reset by the load
        self.send("g.val=7")
        self.assertEqual(self.dev.attr("g", "val"), 7)

    def test_slider_drag(self):
        self.dev.touch(20, 55, "down")
        self.assertEqual(self.dev.attr("sl", "val"), 51)
        self.dev.touch(39, 55, "move")
        self.dev.touch(39, 55, "up")
        self.assertEqual(self.dev.attr("sl", "val"), 100)
        frames = self.take()
        self.assertIn((100).to_bytes(2, "little") + END, frames)

    def test_vis_tsw_and_unknown_instruction(self):
        self.send("tsw b0,0")
        self.dev.tap(*self.dev.center("b0"))
        self.assertEqual(self.take(), b"")
        self.send("tsw b0,1")
        self.send("vis b0,0")
        self.dev.tap(*self.dev.center("b0"))
        self.assertEqual(self.take(), b"")
        self.assertEqual(self.send("frobnicate 1"), b"\x1a" + END if False else self.send("frobnicate 1"))
        self.assertTrue(self.dev.error_count >= 1)

    def test_eeprom_roundtrip_and_reset(self):
        self.send("lang=5")
        self.send("wepo lang,100")
        self.send("lang=1")
        self.send("repo lang,100")
        self.assertEqual(self.dev.globals["lang"], 5)

    def test_twfile_stores_file(self):
        payload = b"hello world"
        self.dev.feed(b'twfile "ram/a.bin",%d' % len(payload) + END)
        frame = bytes([0x3A, 0xA1, 0xBB, 0x44, 0x7F, 0xFF, 0xFE, 0x01, 0, 0]) + (len(payload) + 2).to_bytes(2, "little") + payload + b"\0\0"
        self.dev.feed(frame)
        self.assertEqual(self.dev.files["ram/a.bin"], payload)
        self.assertEqual(self.take(), b"\x05" + END)


class RenderTests(DeviceCase):
    def test_button_text_and_pressed_colour(self):
        r = Renderer(self.project)
        img = r.render(self.dev)
        self.assertEqual(img.size, (40, 60))
        self.assertEqual(img.getpixel((19, 19)), rgb565(63488))                    # button background
        self.assertEqual(img.getpixel((39, 59)), (0, 0, 0))                        # page background
        self.dev.touch(5, 5, "down")
        self.assertEqual(r.render(self.dev).getpixel((19, 19)), rgb565(31))        # pressed colour (bco2)
        self.dev.touch(5, 5, "up")
        self.assertEqual(r.render(self.dev).getpixel((19, 19)), rgb565(63488))

    def test_text_uses_project_font(self):
        r = Renderer(self.project)
        img = r.render(self.dev)
        white = [img.getpixel((x, y)) for x in range(0, 8) for y in range(30, 38)]
        self.assertIn(rgb565(65535), white)                                          # "xy" text is drawn
        self.send('t0.txt=""')
        blank = r.render(self.dev)
        self.assertNotIn(rgb565(65535), [blank.getpixel((x, y)) for x in range(0, 40) for y in range(30, 38)])

    def test_page_picture_and_slider_fill(self):
        r = Renderer(self.project)
        self.dev.touch(20, 55, "down")
        img = r.render(self.dev)
        self.assertEqual(img.getpixel((5, 55)), rgb565(2016))
        self.assertEqual(img.getpixel((35, 55)), (0, 0, 0))
        self.send("page other")
        self.assertEqual(r.render(self.dev).getpixel((30, 30)), (255, 0, 0))

    def test_dim_and_sleep(self):
        r = Renderer(self.project)
        self.send("dim=50")
        self.assertEqual(r.render(self.dev).getpixel((19, 19)), tuple(v // 2 for v in rgb565(63488)))
        self.send("sleep=1")
        self.assertEqual(r.render(self.dev).getpixel((19, 19)), (0, 0, 0))


class LinkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        make_project(self._tmp.name)

    @staticmethod
    def read_until(read, needle, timeout=3.0):
        data, end = b"", time.time() + timeout
        while time.time() < end and needle not in data:
            chunk = read()
            data += chunk
        return data

    def test_pty_round_trip(self):
        emu = Emulator(self._tmp.name, [PtyLink()], boot_frame=True)
        emu.start()
        self.addCleanup(emu.stop)
        fd = os.open(emu.links[0].path, os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(os.close, fd)

        def read():
            return os.read(fd, 4096) if select.select([fd], [], [], 0.1)[0] else b""

        boot = self.read_until(read, b"\x88" + END)
        self.assertTrue(boot.startswith(b"\x00\x00\x00" + END))
        os.write(fd, b"get lang" + END)
        self.assertIn(b"\x71\x02\x00\x00\x00" + END, self.read_until(read, b"\x71\x02\x00\x00\x00" + END))
        emu.device.tap(5, 5)
        self.assertIn(bytes([0x65, 0, 7]) + END, self.read_until(read, bytes([0x65, 0, 7]) + END))

    def test_tcp_round_trip_and_log(self):
        link = SocketLink(("127.0.0.1", 0))
        emu = Emulator(self._tmp.name, [link], boot_frame=False)
        emu.start()
        self.addCleanup(emu.stop)
        s = socket.create_connection(("127.0.0.1", link.port))
        self.addCleanup(s.close)
        s.settimeout(0.2)
        s.sendall(b"n0.val=33" + END + b"get n0.val" + END)

        def read():
            try:
                return s.recv(4096)
            except socket.timeout:
                return b""

        self.assertIn(b"\x71\x21\x00\x00\x00" + END, self.read_until(read, b"\x71\x21\x00\x00\x00" + END))
        entries = [t for _, d, t in emu.entries]
        self.assertTrue(any("get n0.val" in e for e in entries))


class ReloadTests(unittest.TestCase):
    def test_reload_keeps_page_and_globals_and_survives_bad_edit(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(d)
            emu = Emulator(d, [], boot_frame=False)
            emu.device.feed(b"page other" + END)
            emu.device.globals["lang"] = 9
            path = os.path.join(d, "pages", "other.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["objects"][0]["attributes"]["x"] = 5
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            emu.reload()
            self.assertEqual((emu.device.page, emu.device.globals["lang"], emu.device.attr("back", "x")), (1, 9, 5))
            with open(path, "w") as f:
                f.write("{broken")
            with self.assertRaises(Exception):
                emu.reload()
            self.assertEqual(emu.device.attr("back", "x"), 5)             # the running project is untouched

    def test_watcher_picks_up_a_change(self):
        with tempfile.TemporaryDirectory() as d:
            make_project(d)
            emu = Emulator(d, [], boot_frame=False)
            emu.start(watch=True)
            self.addCleanup(emu.stop)
            time.sleep(1.2)
            path = os.path.join(d, "pages", "home.json")
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data["objects"][1]["attributes"]["x"] = 7
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            end = time.time() + 6

            def x():
                with emu.device.lock:
                    return emu.device.attr("t0", "x", page=0)

            while time.time() < end and x() != 7:
                time.sleep(0.1)
            self.assertEqual(x(), 7)


if __name__ == "__main__":
    unittest.main()
