"""Draws the current state of the emulated display with Pillow (fonts are the project's own ``.zi`` files)."""

from __future__ import annotations

import io
import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:            # pragma: no cover - the emulator needs Pillow only for drawing
    Image = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hmi_font  # noqa: E402


def rgb565(v):
    v = int(v) & 0xFFFF
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


class Renderer:
    def __init__(self, project):
        if Image is None:
            raise RuntimeError("Pillow is required (pip install Pillow)")
        self.project = project
        self._pics = {}
        self._fonts = {}
        self._masks = {}
        self._frames = {}
        first = project.pages[0].root.attrs if project.pages else {}
        self.size = (int(first.get("w", 272)), int(first.get("h", 480)))

    # -- resources -------------------------------------------------------------------------------------------------

    def picture(self, ident):
        if not isinstance(ident, int) or not 0 <= ident < len(self.project.picture_keys):
            return None
        img = self._pics.get(ident)
        if img is None and ident not in self._pics:
            rel = self.project.pictures.get(self.project.picture_keys[ident])
            try:
                img = Image.open(self.project.path(rel)).convert("RGBA")
            except (OSError, TypeError):
                img = None
            self._pics[ident] = img
        return img

    def font(self, ident):
        if not isinstance(ident, int) or not 0 <= ident < len(self.project.font_keys):
            return None
        if ident not in self._fonts:
            rel = self.project.fonts.get(self.project.font_keys[ident])
            try:
                with open(self.project.path(rel), "rb") as f:
                    self._fonts[ident] = hmi_font.FontReader(f.read())
            except (OSError, ValueError, TypeError):
                self._fonts[ident] = None
        return self._fonts[ident]

    def mask(self, ident, code):
        key = (ident, code)
        if key not in self._masks:
            g = self.font(ident).glyph(code)
            if g is None:
                self._masks[key] = None
            else:
                rows = g["alpha"]
                h, w = len(rows), len(rows[0]) if rows else 0
                self._masks[key] = (Image.frombytes("L", (w, h), bytes(v for r in rows for v in r)), g)
        return self._masks[key]

    def frame(self, anim_key, index):
        files = self.project.animations.get(anim_key) or []
        if not files:
            return None
        key = (anim_key, index % len(files))
        if key not in self._frames:
            try:
                self._frames[key] = Image.open(self.project.path(files[key[1]])).convert("RGBA")
            except (OSError, TypeError):
                self._frames[key] = None
        return self._frames[key]

    # -- drawing ---------------------------------------------------------------------------------------------------

    def render(self, device):
        with device.lock:
            return self._render(device)

    def _render(self, device):
        img = Image.new("RGBA", self.size, (0, 0, 0, 255))
        if device.page is None:
            return img.convert("RGB")
        if device.sysvars.get("sleep"):
            return img.convert("RGB")
        objs = device.objects()
        self._background(img, objs[0])
        self._canvas(img, device.canvas)
        for o in objs[1:]:
            if not o.visible:
                continue
            try:
                self._component(img, o, device)
            except (ValueError, OSError, KeyError, TypeError):
                pass
        out = img.convert("RGB")
        dim = int(device.sysvars.get("dim", 100))
        if dim < 100:
            out = Image.blend(Image.new("RGB", out.size, (0, 0, 0)), out, max(0, dim) / 100.0)
        return out

    def _background(self, img, page_obj):
        a = page_obj.attrs
        sta = a.get("sta", 0)
        if sta == 1:
            img.paste(rgb565(a.get("bco", 0)), (0, 0, img.width, img.height))
        elif sta == 2:
            self._paste(img, self.picture(a.get("pic")), 0, 0)

    def _paste(self, img, pic, x, y):
        if pic is not None:
            img.alpha_composite(pic, (0, 0), (0, 0)) if (x, y) == (0, 0) and pic.size == img.size else self._blit(img, pic, x, y)

    @staticmethod
    def _blit(img, pic, x, y):
        sx, sy = max(0, -x), max(0, -y)
        dx, dy = max(0, x), max(0, y)
        w, h = min(pic.width - sx, img.width - dx), min(pic.height - sy, img.height - dy)
        if w > 0 and h > 0:
            img.alpha_composite(pic, (dx, dy), (sx, sy, sx + w, sy + h))

    def _crop(self, img, ident, a):
        pic = self.picture(ident)
        if pic is None:
            return False
        x, y, w, h = a["x"], a["y"], a["w"], a["h"]
        part = pic.crop((x, y, x + w, y + h))
        self._blit(img, part, x, y)
        return True

    def _fill(self, img, a, color, box=None):
        x, y, w, h = box or (a["x"], a["y"], a["w"], a["h"])
        if w > 0 and h > 0:
            ImageDraw.Draw(img).rectangle((x, y, x + w - 1, y + h - 1), fill=rgb565(color) + (255,))

    def _component(self, img, o, device):
        a, t = o.attrs, o.type
        if t in ("timer", "variable", "touch_capture", "touch_hotspot") or "x" not in a:
            return
        if t in ("button", "text", "number"):
            pressed = t == "button" and o.pressed
            sta = a.get("sta", 3)
            suffix = "2" if pressed else ""
            if sta == 0:
                self._crop(img, a.get("picc" + suffix, a.get("picc")), a)
            elif sta == 1:
                self._fill(img, a, a.get("bco" + suffix, a.get("bco", 0)))
            elif sta == 2:
                self.paste_at(img, self.picture(a.get("pic" + suffix, a.get("pic"))), a)
            if a.get("style") == 1 and a.get("borderw"):
                ImageDraw.Draw(img).rectangle((a["x"], a["y"], a["x"] + a["w"] - 1, a["y"] + a["h"] - 1),
                                              outline=rgb565(a.get("borderc", 0)) + (255,), width=int(a["borderw"]))
            text = a.get("txt", "")
            if t == "number":
                text = str(a.get("val", 0)).rjust(int(a.get("lenth", 0) or 0), "0")
            if text != "":
                self._text(img, a, text, a.get("pco2" if pressed else "pco", a.get("pco", 0)))
        elif t == "crop_picture":
            self._crop(img, a.get("picc"), a)
        elif t == "progress_bar":
            self._progress(img, a)
        elif t == "slider":
            self._slider(img, a)
        elif t == "animation":
            self._animation(img, o, device)
        elif t == "external_picture":
            data = device.files.get(a.get("path", ""))
            if data:
                try:
                    pic = Image.open(io.BytesIO(data)).convert("RGBA").resize((a["w"], a["h"]))
                    self._blit(img, pic, a["x"], a["y"])
                    return
                except OSError:
                    pass
            self._fill(img, a, 0x2104)
        elif t == "col_pic":
            self._fill(img, a, 0x2104)
            ImageDraw.Draw(img).rectangle((a["x"], a["y"], a["x"] + a["w"] - 1, a["y"] + a["h"] - 1), outline=(90, 90, 90, 255))

    def paste_at(self, img, pic, a):
        if pic is not None:
            self._blit(img, pic, a["x"], a["y"])

    def _progress(self, img, a):
        val = max(0, min(100, int(a.get("val", 0))))
        direction = a.get("dez", 0)
        x, y, w, h = a["x"], a["y"], a["w"], a["h"]
        if a.get("sta", 0) == 0:
            self._fill(img, a, a.get("bco", 0))
        else:
            self._crop(img, a.get("bpic"), a)
        if direction == 0:
            part = (x, y, w * val // 100, h)
        elif direction == 1:
            n = w * val // 100
            part = (x + w - n, y, n, h)
        elif direction == 2:
            n = h * val // 100
            part = (x, y + h - n, w, n)
        else:
            part = (x, y, w, h * val // 100)
        if part[2] <= 0 or part[3] <= 0:
            return
        if a.get("sta", 0) == 0:
            self._fill(img, a, a.get("pco", 0), part)
        else:
            pic = self.picture(a.get("ppic"))
            if pic is not None:
                self._blit(img, pic.crop((part[0], part[1], part[0] + part[2], part[1] + part[3])), part[0], part[1])

    def _slider(self, img, a):
        lo, hi = a.get("minval", 0), a.get("maxval", 100)
        val = max(lo, min(hi, a.get("val", lo)))
        frac = (val - lo) / float(hi - lo or 1)
        x, y, w, h = a["x"], a["y"], a["w"], a["h"]
        if a.get("sta", 0) == 0:
            self._crop(img, a.get("picc"), a)
        elif a.get("sta") == 1:
            self._fill(img, a, a.get("bco", 0))
        vertical = a.get("mode", 0) == 1
        n = int((h if vertical else w) * frac)
        part = (x, y + h - n, w, n) if vertical else (x, y, n, h)
        if n > 0:
            pic = self.picture(a.get("picc1"))
            if pic is not None and a.get("sta", 0) == 0:
                self._blit(img, pic.crop((part[0], part[1], part[0] + part[2], part[1] + part[3])), part[0], part[1])
            else:
                self._fill(img, a, a.get("bco1", 0), part)
        psta = a.get("psta", 2)
        wid, hig = int(a.get("wid", 0)), int(a.get("hig", 0))
        cx = x + w // 2 if vertical else x + n
        cy = y + h - n if vertical else y + h // 2
        if psta == 2 and wid > 0 and hig > 0:
            self._fill(img, a, a.get("pco", 0), (cx - wid // 2, cy - hig // 2, wid, hig))
        elif psta == 1:
            handle = self.picture(a.get("pic2"))
            if handle is not None:
                self._blit(img, handle, cx - handle.width // 2, cy - handle.height // 2)

    def _animation(self, img, o, device):
        a = o.attrs
        key = None
        keys = self.project.animation_keys
        if isinstance(a.get("vid"), int) and 0 <= a["vid"] < len(keys):
            key = keys[a["vid"]]
        if key is None:
            return
        files = self.project.animations.get(key) or []
        index = int(a.get("from", 0))
        if a.get("en") and files:
            ms = self.project.animation_ms.get(key) or [50]
            elapsed = int((device.clock() - o.started) * 1000)
            total = sum(ms) or 1
            elapsed = elapsed % total if a.get("loop", 1) else min(elapsed, total - 1)
            for i, m in enumerate(ms):
                if elapsed < m:
                    index = i
                    break
                elapsed -= m
        frame = self.frame(key, index)
        if frame is not None:
            self._blit(img, frame, a["x"], a["y"])

    # -- text ------------------------------------------------------------------------------------------------------

    def _advance(self, font_id, ch):
        m = self.mask(font_id, ord(ch))
        if m is None:
            return None
        return m[1]["width"]

    def _wrap(self, font_id, text, width, spax, wrap):
        lines = []
        for para in text.replace("\r\n", "\r").replace("\n", "\r").split("\r"):
            if not wrap:
                lines.append(para)
                continue
            cur, cur_w, last_space = "", 0, -1
            for ch in para:
                adv = self._advance(font_id, ch)
                adv = (adv if adv is not None else 4) + spax
                if cur_w + adv > width and cur:
                    if last_space >= 0 and ch != " ":
                        lines.append(cur[:last_space])
                        cur = cur[last_space + 1:]
                    else:
                        lines.append(cur)
                        cur = ""
                    cur_w = sum((self._advance(font_id, c) or 4) + spax for c in cur)
                    last_space = -1
                if ch == " ":
                    last_space = len(cur)
                cur += ch
                cur_w += adv
            lines.append(cur)
        return lines

    def _text(self, img, a, text, color):
        font_id = a.get("font")
        font = self.font(font_id)
        if font is None:
            return
        spax, spay = int(a.get("spax", 0)), int(a.get("spay", 0))
        lines = self._wrap(font_id, str(text), a["w"], spax, bool(a.get("isbr", 0)))
        line_h = font.height + spay
        total_h = line_h * len(lines) - spay
        ycen = a.get("ycen", 0)
        y0 = a["y"] + (0 if ycen == 0 else (a["h"] - total_h) // 2 if ycen == 1 else a["h"] - total_h)
        rgb = rgb565(color)
        for n, line in enumerate(lines):
            width = sum((self._advance(font_id, c) or 0) + spax for c in line) - (spax if line else 0)
            xcen = a.get("xcen", 0)
            x = a["x"] + (0 if xcen == 0 else (a["w"] - width) // 2 if xcen == 1 else a["w"] - width)
            y = y0 + n * line_h
            for ch in line:
                m = self.mask(font_id, ord(ch))
                if m is None:
                    x += 4 + spax
                    continue
                mask, g = m
                gx, gy = x - g["left"], y
                if mask.width and mask.height:
                    img.paste(Image.new("RGBA", mask.size, rgb + (255,)), (gx, gy), mask)
                x += g["width"] + spax

    # -- drawing instructions (cls, fill, line...) -----------------------------------------------------------------

    def _canvas(self, img, ops):
        d = ImageDraw.Draw(img)
        for name, a in ops:
            try:
                if name == "cls":
                    img.paste(rgb565(a[0]) + (255,), (0, 0, img.width, img.height))
                elif name == "fill":
                    d.rectangle((a[0], a[1], a[0] + a[2] - 1, a[1] + a[3] - 1), fill=rgb565(a[4]) + (255,))
                elif name == "draw":
                    d.rectangle((a[0], a[1], a[2], a[3]), outline=rgb565(a[4]) + (255,))
                elif name == "line":
                    d.line((a[0], a[1], a[2], a[3]), fill=rgb565(a[4]) + (255,))
                elif name in ("cir", "cirs"):
                    r = a[2]
                    box = (a[0] - r, a[1] - r, a[0] + r, a[1] + r)
                    if name == "cirs":
                        d.ellipse(box, fill=rgb565(a[3]) + (255,))
                    else:
                        d.ellipse(box, outline=rgb565(a[3]) + (255,))
                elif name == "pic":
                    self._blit(img, self.picture(a[2]), a[0], a[1])
                elif name == "picq":
                    pic = self.picture(a[4])
                    if pic is not None:
                        self._blit(img, pic.crop((a[0], a[1], a[0] + a[2], a[1] + a[3])), a[0], a[1])
            except (IndexError, TypeError, ValueError):
                continue
