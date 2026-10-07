"""Independent picture encoder tests, including cached native RGB/alpha outputs."""
import hashlib
import io
import json
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hmi_image as I
import hmi_parse as H


def png(w, h, rgba):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    rows = b''.join(b'\0' + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))
    return I.PNG + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b'')


def expand(alpha):
    return [255 if a == 255 else 2 * (a * 127 // 255) for a in alpha]


class PictureCodecTest(unittest.TestCase):
    def roundtrip(self, w, h, colours, alpha=None):
        encoded = I.encode_picture(w, h, colours, alpha)
        hd = I.validate_picture(encoded)
        decoded = H.decode_picture(encoded)
        expected = [c if alpha is None or alpha[i] * 127 // 255 else 0 for i, c in enumerate(colours)]
        self.assertEqual(decoded['rgb565'], expected)
        if alpha is not None and not all(a == 255 for a in alpha):
            self.assertEqual(decoded['alpha'], expand(alpha))
        else:
            self.assertIsNone(decoded['alpha'])
        self.assertEqual(hd['imgbytesize'], len(encoded) - 24)
        self.assertEqual(I.encode_picture(w, h, colours, alpha), encoded)
        return encoded

    def test_asymmetric_and_single_pixel_dimensions(self):
        for w, h in ((1, 1), (1, 257), (257, 1), (2, 3), (3, 2), (19, 7)):
            with self.subTest(w=w, h=h):
                self.roundtrip(w, h, list(range(w * h)))

    def test_rle_inline_extended_boundaries(self):
        for run in (1, 2, 6, 7, 31, 32, 254, 255, 256, 510, 511):
            with self.subTest(run=run):
                self.roundtrip(run, 1, [0xf81f] * run)

    def test_palette_banks_and_block_boundaries(self):
        for count in (1, 2, 31, 32, 33, 255, 256, 257, 300, 1024):
            colours = list(range(count)) + list(reversed(range(count)))
            with self.subTest(count=count):
                self.roundtrip(count, 2, colours)

    def test_all_rgb565_colours_are_preserved(self):
        colours = list(range(65536))
        encoded = self.roundtrip(256, 256, colours)
        vector = json.loads((ROOT / 'tests/fixtures/hmi_picture_vectors.json').read_text())['large_case']
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), vector['compiled_sha256'])
        stored = I._rotate(colours, 256, 256)
        raw = struct.pack('<%dH' % len(stored), *stored)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), vector['native_stored_rgb565_sha256'])

    def test_alpha_all_levels_and_runs(self):
        self.roundtrip(256, 1, [0x1234] * 256, list(range(256)))
        for run in (1, 2, 254, 255, 256, 511):
            with self.subTest(run=run):
                self.roundtrip(run, 3, [0xabcd] * (run * 3), [0] * run + [128] * run + [255] * run)

    def test_native_alpha_encoder_quantization_all_inputs(self):
        vector = json.loads((ROOT / 'tests/fixtures/hmi_alpha_vectors.json').read_text())
        alpha = list(bytes.fromhex(vector['input']))
        expected = list(bytes.fromhex(vector['decoded']))
        self.assertEqual(expand(alpha), expected)
        encoded = I.encode_picture(256, 1, [0x1234] * 256, alpha)
        self.assertEqual(H.decode_picture(encoded)['alpha'], expected)
        native = bytes.fromhex(vector['encoded'])
        self.assertEqual(I._alpha_stream(native, 0, len(alpha)), [a * 127 // 255 for a in alpha])

    def test_hidden_colours_and_fully_opaque_alpha(self):
        self.roundtrip(2, 2, [0xffff] * 4, [0, 1, 2, 255])
        self.roundtrip(2, 2, [0x1234] * 4, [254, 255, 254, 255])

    def test_seeded_random_rasters(self):
        rng = random.Random(1729)
        for w, h in ((7, 13), (32, 33), (64, 31)):
            self.roundtrip(w, h, [rng.randrange(65536) for _ in range(w * h)],
                           [rng.randrange(256) for _ in range(w * h)])

    def test_codecs_need_no_pillow_or_subprocess(self):
        real_import = __import__
        def guarded(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.'):
                raise AssertionError('Image library imported by the pure codec')
            return real_import(name, *args, **kwargs)
        with mock.patch('builtins.__import__', side_effect=guarded), \
                mock.patch('subprocess.run', side_effect=AssertionError('External program started')):
            self.roundtrip(2, 3, list(range(6)), [0, 64, 128, 192, 254, 255])
            I.validate_source(I.encode_source(png(1, 1, [1, 2, 3, 255])))

    def test_invalid_dimensions_pixels_and_profiles(self):
        for w, h in ((0, 1), (1, 0), (-1, 1), (65536, 1), (65535, 65535), (True, 1)):
            with self.subTest(w=w, h=h), self.assertRaises(ValueError):
                I.encode_picture(w, h, [])
        for colours, alpha in (([], None), ([65536], None), ([-1], None), ([True], None),
                               ([0], []), ([0], [256]), ([0], [-1]), ([0], [True])):
            with self.assertRaises(ValueError):
                I.encode_picture(1, 1, colours, alpha)
        template = I.encode_picture(1, 1, [0])
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            I.encode_picture(2, 1, [0, 0], template=template)
        for pos, value in ((0, 12), (3, 3), (6, 1), (7, 1), (8, 23)):
            bad = bytearray(template)
            bad[pos] = value
            with self.subTest(pos=pos), self.assertRaises(ValueError):
                I.validate_picture(bad)

    def test_strict_lengths_offsets_and_token_errors(self):
        original = I.encode_picture(3, 2, list(range(6)), [0, 30, 90, 150, 210, 255])
        for pos in (8, 12, 16, 20, 24, 28, 32, 36, 40):
            bad = bytearray(original)
            bad[pos] ^= 1
            with self.subTest(pos=pos), self.assertRaises(ValueError):
                I.validate_picture(bad)
        for length in (0, 23, 24, 43, len(original) - 1):
            with self.assertRaises(ValueError):
                I.validate_picture(original[:length])
        bad = bytearray(I.encode_picture(1, 1, [0]))
        bad[48] = 4  # Python's permissive decoder accepts ib=4; native does not.
        with self.assertRaisesRegex(ValueError, 'palette'):
            I.validate_picture(bad)
        bad = bytearray(I.encode_picture(1, 1, [0]))
        bad[-1] = 0x21  # palette index one in a one-entry palette
        with self.assertRaisesRegex(ValueError, 'palette'):
            I.validate_picture(bad)
        bad[-1] = 0x40  # run of two pixels in a one-pixel image
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            I.validate_picture(bad)

    def test_template_preserves_metadata_not_logical_id(self):
        template = I.encode_picture(3, 2, list(range(6)), picture_id=123)
        modified = I.encode_picture(3, 2, [42] * 6, template=template, picture_id=7)
        hd = I.validate_picture(modified)
        self.assertEqual(hd['pictureid'], 123)
        self.assertEqual(modified[:16], template[:16])

    def test_native_cached_picture_vectors(self):
        vectors = json.loads((ROOT / 'tests/fixtures/hmi_picture_vectors.json').read_text())
        for v in vectors['cases']:
            with self.subTest(case=v['case']):
                encoded = bytes.fromhex(v['compiled'])
                hd = I.validate_picture(encoded)
                d = H.decode_picture(encoded)
                stored = I._rotate(d['rgb565'], hd['w'], hd['h'])
                raw = struct.pack('<%dH' % len(stored), *stored)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), v['native_stored_rgb565_sha256'])
                if 'native_project_rgba' in v:
                    native = bytes.fromhex(v['native_project_rgba'])
                    colours = [((native[p] >> 3) << 11) | ((native[p + 1] >> 2) << 5) | (native[p + 2] >> 3)
                               for p in range(0, len(native), 4)]
                    self.assertEqual(d['rgb565'], colours)
                    self.assertEqual(d['alpha'] or [255] * len(colours), list(native[3::4]))
                if 'native_payload' in v:
                    payload = bytes.fromhex(v['native_payload'])
                    head = bytearray(encoded[:24])
                    struct.pack_into('<Ii', head, 16, len(payload), 0)
                    I.validate_picture(bytes(head) + payload)
                    self.assertEqual(H.decode_picture(bytes(head) + payload)['rgb565'], d['rgb565'])


class PngAdapterTest(unittest.TestCase):
    def test_source_header_and_exact_png_preservation(self):
        raw = png(2, 1, [0, 1, 2, 3, 254, 255, 1, 255])
        source = I.encode_source(raw, picture_id=12)
        hd = I.validate_source(source)
        self.assertEqual(source[24:27], b'png')
        self.assertEqual(source[27:], raw)
        self.assertEqual(hd['dataaddr'], 27)
        self.assertEqual(hd['imgbytesize'], len(raw))
        self.assertEqual(hd['pictureid'], 12)
        self.assertEqual(hd['qumo'], 10)

    def test_source_validation_rejects_bad_png_and_metadata(self):
        raw = png(1, 1, [0, 0, 0, 255])
        for data in (b'not png', raw[:-1], raw + b'trailing', raw[:40] + b'corrupted' + raw[40:]):
            with self.assertRaises(ValueError):
                I.encode_source(data)
        source = bytearray(I.encode_source(raw))
        source[12] = 2
        with self.assertRaisesRegex(ValueError, 'dimensions'):
            I.validate_source(source)

    def test_png_loading_quantization_and_alpha(self):
        try:
            import PIL.Image
        except ImportError:
            self.skipTest('Pillow not installed')
        values = [255, 127, 31, 128, 7, 3, 7, 255, 55, 66, 77, 0, 128, 255, 0, 255]
        raw = png(2, 2, values)
        with mock.patch('subprocess.run', side_effect=AssertionError('External process started')):
            compiled, source = I.encode_png(raw)
        d = H.decode_picture(compiled)
        expected = [((values[p] >> 3) << 11) | ((values[p + 1] >> 2) << 5) | (values[p + 2] >> 3)
                    if values[p + 3] * 127 // 255 else 0 for p in range(0, len(values), 4)]
        self.assertEqual(d['rgb565'], expected)
        self.assertEqual(d['alpha'], expand(values[3::4]))
        self.assertEqual(source[27:], raw)

    def test_png_modes_and_missing_dependency(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest('Pillow not installed')
        for mode in ('1', 'L', 'LA', 'RGB', 'RGBA', 'P'):
            with self.subTest(mode=mode):
                image = Image.new(mode, (3, 2))
                if mode == 'P':
                    image.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
                    image.putdata([0, 1] * 3)
                    image.info['transparency'] = 0
                buffer = io.BytesIO()
                image.save(buffer, format='PNG')
                compiled, source = I.encode_png(buffer.getvalue())
                I.validate_picture(compiled)
                I.validate_source(source)
        raw = png(1, 1, [0, 0, 0, 255])
        real_import = __import__
        def no_pillow(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.'):
                raise ImportError('blocked for test')
            return real_import(name, *args, **kwargs)
        with mock.patch('builtins.__import__', side_effect=no_pillow), self.assertRaisesRegex(ValueError, 'needs Pillow'):
            I.encode_png(raw)
        image = Image.new('I;16', (1, 1), 1024)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        with self.assertRaisesRegex(ValueError, '16-bit'):
            I.encode_png(buffer.getvalue())

    def test_exclusive_pair_outputs(self):
        compiled = I.encode_picture(1, 1, [0])
        source = I.encode_source(png(1, 1, [0, 0, 0, 255]))
        with tempfile.TemporaryDirectory(dir=ROOT / '.state') as directory:
            out = Path(directory) / 'sample.i'
            companion = out.with_suffix('.is')
            companion.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                I.write_pair(out, compiled, source)
            self.assertFalse(out.exists())
            self.assertEqual(companion.read_bytes(), b'keep')
            companion.unlink()
            I.write_pair(out, compiled, source)
            self.assertEqual(out.read_bytes(), compiled)
            self.assertEqual(companion.read_bytes(), source)
            with self.assertRaises(FileExistsError):
                I.write_pair(out, compiled, source)


@unittest.skipUnless(os.environ.get('HMI_FILE'), 'set HMI_FILE')
class RealPictureTest(unittest.TestCase):
    def test_all_existing_picture_and_source_profiles(self):
        files = H.read_container(Path(os.environ['HMI_FILE']).read_bytes())
        for name, data in files.items():
            if name.endswith('.i'):
                with self.subTest(file=name):
                    I.validate_picture(data)
            elif name.endswith('.is'):
                with self.subTest(file=name):
                    I.validate_source(data)


if __name__ == '__main__':
    unittest.main()
