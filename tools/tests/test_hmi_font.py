"""Synthetic tests for the standalone .zi font encoder."""

from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hmi_font as F  # noqa: E402
import hmi_parse as H  # noqa: E402


class GlyphCodecTests(unittest.TestCase):
    def _roundtrip(self, alpha, width, height, bpp):
        stream = F.encode_glyph(alpha, width, height, bpp)
        self.assertEqual(stream[0], bpp)
        levels = H.glyph_decode(stream, width * height)
        visual = H._rotate90_flip(levels, height, width)
        expected = F._quantise_alpha(alpha, bpp)
        if bpp == 3:
            expected = [round(v * 255 / 7) for v in expected]
        self.assertEqual(visual, expected)
        self.assertEqual(F._strict_decode_stream(stream, width * height).__len__(), width * height)

    def test_one_bit_exact_binary_stream(self):
        alpha = [0] * 40 + [255] * 35 + [0] * 3 + [255] * 2
        self._roundtrip(alpha, 10, 8, 1)
        self.assertNotIn(b"\x00", F.encode_glyph(alpha, 10, 8, 1)[1:])

    def test_three_bit_levels_and_arbitrary_alpha(self):
        levels = [0, 36, 73, 109, 146, 182, 219, 255]
        self._roundtrip(levels * 4, 8, 4, 3)
        self._roundtrip([0, 17, 80, 127, 201, 255] * 5, 5, 6, 3)

    def test_codec_rejects_truncation_and_padding(self):
        stream = F.encode_glyph([0, 255, 0, 255], 2, 2, 1)
        with self.assertRaises(ValueError):
            F._strict_decode_stream(stream[:-1], 4)
        with self.assertRaises(ValueError):
            F._strict_decode_stream(stream + b"\x00", 4)


class NativeGlyphVectorTests(unittest.TestCase):
    def test_cached_native_alpha_vectors(self):
        fixture = Path(__file__).with_name("fixtures") / "hmi_glyph_vectors.json"
        with fixture.open(encoding="utf-8") as f:
            cases = json.load(f)["cases"]
        for case in cases:
            with self.subTest(case=case["case"]):
                stream = bytes.fromhex(case["input"])
                pixels = case["pixels"]
                native = bytes.fromhex(case["native_alpha"])
                self.assertEqual(len(native), pixels)
                self.assertEqual(len(F._strict_decode_stream(stream, pixels)), pixels)
                levels = F._strict_decode_stream(stream, pixels)
                if stream[0] == 1:
                    decoded = bytes(255 if level else 0 for level in levels)
                    bpp = 1
                else:
                    decoded = bytes(round(level * 255 / 7) for level in levels)
                    bpp = 3
                self.assertEqual(decoded, native)
                # The pure encoder need not select the same token family, but
                # must preserve the native stored-order alpha exactly.
                rebuilt = F.encode_glyph(list(native), pixels, 1, bpp, stored=True)
                rebuilt_levels = F._strict_decode_stream(rebuilt, pixels)
                if bpp == 1:
                    rebuilt_alpha = bytes(255 if level else 0 for level in rebuilt_levels)
                else:
                    rebuilt_alpha = bytes(round(level * 255 / 7) for level in rebuilt_levels)
                self.assertEqual(rebuilt_alpha, native)


class FontProfileTests(unittest.TestCase):
    @staticmethod
    def glyph(width, height, value=255, left=0, right=0):
        return {"width": width, "left": left, "right": right,
                "alpha": [value] * ((width + left + right) * height)}

    def test_ascii_profile_and_metrics(self):
        data = F.encode_font({32: self.glyph(5, 4, 0), 65: self.glyph(3, 4, 255, 1, 2)},
                             4, "Synthetic", "ascii", 1)
        header = F.validate_font(data)
        self.assertEqual((header["state"], header["qyt"], header["h"], header["trueziqty"]), (0, 95, 4, 2))
        self.assertEqual(header["name"], "Synthetic")
        base = 44 + header["zimoascbeg"]
        code, width, left, right, address, size = struct.unpack_from("<HBBB3sH", data, base + 10 * (65 - 32))
        self.assertEqual((code, width, left, right), (65, 3, 1, 2))
        self.assertEqual(int.from_bytes(address, "little"), 952 + 2)
        self.assertGreater(size, 1)

    def test_bmp_profile_alignment_and_unicode(self):
        data = F.encode_font({0x41: self.glyph(4, 3), 0x3A9: self.glyph(2, 3, 255)},
                             3, "BMP", "bmp", 3)
        header = F.validate_font(data)
        self.assertEqual((header["state"], header["qyt"], header["trueziqty"]), (1, 65536, 2))
        base = 44 + header["zimoascbeg"]
        for code in (0x41, 0x3A9):
            entry = struct.unpack_from("<HBBB3sH", data, base + 10 * code)
            self.assertEqual(entry[0], code)
            self.assertEqual((int.from_bytes(entry[4], "little") * 8) % 8, 0)

    def test_shared_payload_is_valid(self):
        glyph = self.glyph(3, 2, 255)
        data = F.encode_font({65: glyph, 66: glyph}, 2, "Shared", "ascii", 1)
        h = F.validate_font(data)
        base = 44 + h["zimoascbeg"]
        a = struct.unpack_from("<HBBB3sH", data, base + 10 * (65 - 32))
        b = struct.unpack_from("<HBBB3sH", data, base + 10 * (66 - 32))
        self.assertEqual(a[4:], b[4:])

    def test_bmp_rejects_controls_surrogates_and_nonbmp(self):
        g = self.glyph(1, 1)
        for code in (0, 0x7F, 0xD800, 0x10000):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    F.encode_font({code: g}, 1, "x", "bmp", 1)

    def test_charset_parser_keeps_literal_space(self):
        self.assertEqual(F.parse_chars(" A"), [32, 65])
        self.assertEqual(F.parse_chars("0123"), [48, 49, 50, 51])
        self.assertEqual(F.parse_chars("U+0041-U+0043,U+03A9"), [65, 66, 67, 0x3A9])

    def test_validate_rejects_trailing_bytes(self):
        data = F.encode_font({65: self.glyph(1, 1)}, 1, "x", "ascii", 1)
        with self.assertRaises(ValueError):
            F.validate_font(data + b"\0")


FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
)
LOCAL_FONT = next((p for p in FONT_CANDIDATES if os.path.exists(p)), None)


@unittest.skipUnless(LOCAL_FONT, "no local TrueType font")
class DecodeTests(unittest.TestCase):
    def test_decode_roundtrip_is_pixel_exact(self):
        # asymmetric bitmap so a transposed or mirrored decode is detected
        alpha = [[0, 36, 73, 255], [182, 0, 255, 0], [255, 109, 0, 36]]
        glyphs = {65: {"width": 2, "left": 1, "right": 1, "alpha": alpha}}
        for layout in ("ascii", "bmp"):
            data = F.encode_font(glyphs, 3, "Dec", layout, 3)
            font = F.decode_font(data)
            self.assertEqual((font["name"], font["height"], font["layout"]), ("Dec", 3, layout))
            g = font["glyphs"][65]
            self.assertEqual((g["width"], g["left"], g["right"]), (2, 1, 1))
            self.assertEqual(g["alpha"], [[round(round(v * 7 / 255) * 255 / 7) for v in r] for r in alpha])
            again = F.encode_font(font["glyphs"], 3, font["name"], layout, font["bpp"])
            self.assertEqual(F.decode_font(again)["glyphs"], font["glyphs"])

    def test_decode_one_bit_and_rejects_bad_data(self):
        data = F.encode_font({66: {"width": 2, "left": 0, "right": 0, "alpha": [0, 255, 255, 0]}}, 2, "One", "ascii", 1)
        self.assertEqual(F.decode_font(data)["glyphs"][66]["alpha"], [[0, 255], [255, 0]])
        with self.assertRaises(ValueError):
            F.decode_font(data[:-1])


class RasterisationTests(unittest.TestCase):
    def test_ttf_ascii_space_and_missing_cmap(self):
        data = F.encode_ttf(LOCAL_FONT, height=16, chars=" A", bpp=1)
        h = F.validate_font(data)
        self.assertEqual(h["state"], 0)
        self.assertEqual(h["trueziqty"], 2)
        with self.assertRaises(ValueError):
            F.encode_ttf(LOCAL_FONT, height=16, chars=[0x10FFFF], bpp=1)

    def test_ttf_bmp_unicode_and_template_defaults(self):
        template = F.encode_font({65: {"width": 6, "left": 0, "right": 0, "alpha": [255] * (6 * 12)}},
                                 12, "Template", "bmp", 3)
        data = F.encode_ttf(LOCAL_FONT, template=template, chars="A", bpp=1)
        h = F.validate_font(data)
        self.assertEqual((h["name"], h["h"], h["state"]), ("Template", 12, 1))
        self.assertEqual(h["trueziqty"], 1)
        data = F.encode_ttf(LOCAL_FONT, height=18, chars=[0x03A9], layout="bmp", bpp=3)
        self.assertEqual(F.validate_font(data)["trueziqty"], 1)

    def test_explicit_size_is_validated_for_empty_charset(self):
        for size in (0, -1, True, 1.5, "bad"):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    F.encode_ttf(LOCAL_FONT, height=12, chars=[], size=size)

    def test_missing_path(self):
        with self.assertRaises(ValueError):
            F.encode_ttf("/does/not/exist.ttf", height=12)


if __name__ == "__main__":
    unittest.main()
