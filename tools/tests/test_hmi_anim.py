"""Animation encoder: PNG frames -> .gmov / .gmovs."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hmi_anim as A
import hmi_parse as H
from test_hmi_image import png as make_png

HMI_FILE = os.environ.get('HMI_FILE')
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def frame(w, h, seed):
    px = bytearray()
    for y in range(h):
        for x in range(w):
            px += bytes(((x * 40 + seed * 50) % 256, (y * 60 + seed * 20) % 256, (x * y + seed) % 256, 255 if (x + y + seed) % 3 else 100))
    return make_png(w, h, bytes(px))


class SourceTest(unittest.TestCase):
    def test_layout_and_round_trip(self):
        pngs = [frame(5, 4, i) for i in range(3)]
        data = A.build_source(pngs, [100, 250, 50], fps=12)
        info = A.validate_source(data)
        self.assertEqual((info['fps'], info['w'], info['h']), (12, 5, 4))
        self.assertEqual(info['pngs'], pngs)
        self.assertEqual([f['duration_ms'] for f in info['frames']], [100, 250, 50])
        self.assertEqual(struct.unpack_from('<I', data, 36)[0], 400)                    # total ms
        self.assertEqual(struct.unpack_from('<I', data, 8 + 4 + 4 + 4)[0], 0)           # showpic
        self.assertEqual(data[-4096:][:8], b'hmic\xf8\x0f\x00\x00')
        self.assertEqual(struct.unpack_from('<I', data, 40)[0], len(data) - 76 - 4096)  # extmessageaddr

    def test_single_duration_and_limits(self):
        data = A.build_source([frame(2, 2, 0)] * 2, 40)
        self.assertEqual([f['duration_ms'] for f in A.validate_source(data)['frames']], [40, 40])
        for args in (([], 10), ([frame(2, 2, 0)], [10, 20]), ([frame(2, 2, 0)], [0]), ([frame(2, 2, 0)], [70000]),
                     ([frame(2, 2, 0)], [True]), ([frame(2, 2, 0), frame(3, 2, 0)], 10), ([frame(2, 2, 0)] * 256, 10)):
            with self.subTest(frames=len(args[0]), durations=args[1]), self.assertRaises(ValueError):
                A.build_source(*args)
        with self.assertRaises(ValueError):
            A.build_source([frame(2, 2, 0)], 10, fps=0)

    def test_validator_rejects_damage(self):
        good = A.build_source([frame(4, 4, 0), frame(4, 4, 1)], 10)
        cases = []
        for pos in (0, 4, 5, 20, 48, 60):
            b = bytearray(good)
            b[pos] ^= 1
            cases.append(bytes(b))
        cases += [good[:-1], good[:100], good + b'x', good[:-4096] + bytes(4096)]
        b = bytearray(good)
        struct.pack_into('<I', b, 76 + 8, 99)                                         # frame offset
        cases.append(bytes(b))
        b = bytearray(good)
        b[good.find(b'\x89PNG') - 1] ^= 1                                              # PNG length byte
        cases.append(bytes(b))
        for i, bad in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(ValueError):
                A.validate_source(bad)


@unittest.skipUnless(HAVE_PIL, 'Pillow is needed to read PNG frames')
class CompiledTest(unittest.TestCase):
    def test_frames_decode_to_the_source_pixels(self):
        pngs = [frame(9, 7, i) for i in range(4)]
        gmov, gmovs = A.encode_animation(pngs, [100, 100, 300, 50])
        info = A.validate_animation(gmov)
        self.assertEqual((info['w'], info['h'], len(info['frames'])), (9, 7, 4))
        self.assertEqual(A.validate_source(gmovs)['pngs'], pngs)
        decoded = H.gmov_decode(gmov)
        self.assertEqual([f['duration_ms'] for f in decoded], [100, 100, 300, 50])
        self.assertEqual([f['start_ms'] for f in decoded], [0, 100, 200, 500])
        import hmi_image
        for png, d in zip(pngs, decoded):
            w, h, colours, alpha = hmi_image.load_png(png)
            self.assertEqual(d['rgb565'], [c if a * 127 // 255 else 0 for c, a in zip(colours, alpha)])
            self.assertEqual(d['alpha'], [255 if a * 127 // 255 == 127 else (a * 127 // 255) * 2 for a in alpha])

    def test_opaque_frames_still_carry_an_alpha_block(self):
        png = make_png(3, 3, bytes((10, 20, 30, 255)) * 9)
        gmov, _ = A.encode_animation([png], 10)
        f = A.validate_animation(gmov)['frames'][0]
        self.assertGreater(f['alpha_offset'], 0)
        self.assertEqual(set(H.gmov_decode(gmov)[0]['alpha']), {255})

    def test_opaque_variant_has_no_alpha_block(self):
        pngs = [make_png(3, 3, bytes((10 * i, 20, 30, 255)) * 9) for i in range(2)]
        gmov, _ = A.encode_animation(pngs, 50, opaque=True)
        info = A.validate_animation(gmov)
        self.assertTrue(info['opaque'])
        self.assertEqual([f['alpha_offset'] for f in info['frames']], [0, 0])
        self.assertEqual(gmov[48:52], A.OPAQUE_FLAGS)
        self.assertFalse(A.validate_animation(A.encode_animation(pngs, 50)[0])['opaque'])
        with self.assertRaises(ValueError):
            A.encode_animation([frame(4, 4, 0)], 10, opaque=True)

    def test_validator_rejects_damage(self):
        gmov, _ = A.encode_animation([frame(4, 4, 0), frame(4, 4, 1)], 10)
        for i, bad in enumerate([gmov[:-1], gmov[:-5000], gmov + b'x', b'XXXX' + gmov[4:], gmov[:50] + b'\1' + gmov[51:],
                                 gmov[:76 + 20] + struct.pack('<I', 0) + gmov[76 + 24:]]):
            with self.subTest(case=i), self.assertRaises(ValueError):
                A.validate_animation(bad)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for i in range(2):
                (d / ('f%d.png' % i)).write_bytes(frame(4, 3, i))
            out = d / 'a.gmov'
            cmd = [sys.executable, str(ROOT / 'hmi_anim.py'), 'encode', '--ms', '70', str(d / 'f0.png'), str(d / 'f1.png'), str(out)]
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            r = subprocess.run(cmd, capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(A.validate_animation(out.read_bytes())['frames']), 2)
            self.assertTrue(out.with_suffix('.gmovs').exists())
            self.assertNotEqual(subprocess.run(cmd, capture_output=True, text=True, env=env).returncode, 0)   # no overwrite


@unittest.skipUnless(HMI_FILE, 'set HMI_FILE for the real animations')
class RealProjectTest(unittest.TestCase):
    def test_source_writer_reproduces_the_editor_files_byte_for_byte(self):
        files = H.read_container(Path(HMI_FILE).read_bytes())
        names = [n for n in files if n.endswith('.gmov')]
        self.assertGreaterEqual(len(names), 12)
        for n in names:
            info = A.validate_source(files[n + 's'])
            self.assertEqual(A.build_source(info['pngs'], [f['duration_ms'] for f in info['frames']], info['fps']), files[n + 's'], n)
            A._parse(files[n], 'gmov')

    @unittest.skipUnless(HAVE_PIL, 'Pillow is needed to encode')
    def test_encoded_animations_match_the_originals_structurally(self):
        files = H.read_container(Path(HMI_FILE).read_bytes())
        for n in [n for n in files if n.endswith('.gmov')][:4]:
            info = A.validate_source(files[n + 's'])
            ms = [f['duration_ms'] for f in info['frames']]
            new, _ = A.encode_animation(info['pngs'], ms, info['fps'])
            old_f, new_f = H.gmov_decode(files[n]), H.gmov_decode(new)
            for a, b in zip(old_f, new_f):
                self.assertEqual((a['w'], a['h'], a['alpha'], a['duration_ms'], a['start_ms']),
                                 (b['w'], b['h'], b['alpha'], b['duration_ms'], b['start_ms']))
                self.assertTrue(all(a['alpha'][i] != 255 or a['rgb565'][i] == b['rgb565'][i] for i in range(len(a['alpha']))))
            self.assertEqual(files[n][:24], new[:24])
            self.assertEqual(files[n][26:40], new[26:40])


if __name__ == '__main__':
    unittest.main()
