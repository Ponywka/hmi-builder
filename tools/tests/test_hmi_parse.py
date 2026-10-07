"""Sanity tests for hmi_parse. Needs a project file: HMI_FILE=/path/to/PROJECT.HMI python3 -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import hmi_parse as H  # noqa: E402

PATH = os.environ.get('HMI_FILE')


@unittest.skipUnless(PATH and os.path.exists(PATH), 'set HMI_FILE')
class ParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH, 'rb') as f:
            cls.files = H.read_container(f.read())
        cls.proj = H.parse_project(cls.files)

    def test_check_clean(self):
        self.assertEqual(H.check(self.files), [])

    def test_counts_match_index(self):
        words, recs = H.parse_index(self.files['main.HMI'])
        self.assertEqual(len(self.proj['pages']), sum(1 for k, _ in recs if k == 'pa'))
        self.assertEqual(len(self.proj['pictures']), sum(1 for k, _ in recs if k == 'i'))

    def test_every_page_starts_with_page_object(self):
        for pg in self.proj['pages']:
            self.assertEqual(pg['objects'][0]['type_name'], 'page')
            self.assertEqual(pg['objects'][0]['objname'], pg['name'])

    def test_sources_are_png(self):
        for p in self.proj['pictures']:
            self.assertEqual(p.get('source_format'), 'png')

    def test_decode_all_pictures(self):
        _, recs = H.parse_index(self.files['main.HMI'])
        for k, n in recs:
            if k == 'i':
                pic = H.decode_picture(self.files[n])
                self.assertEqual(len(pic['rgb565']), pic['w'] * pic['h'])

    def test_decoded_matches_source_png(self):
        try:
            from PIL import Image
            import io
        except ImportError:
            self.skipTest('Pillow not installed')
        r565 = lambda p: ((p[0] >> 3) << 11) | ((p[1] >> 2) << 5) | (p[2] >> 3)
        _, recs = H.parse_index(self.files['main.HMI'])
        for k, n in recs:
            if k != 'i':
                continue
            src = self.files[n[:-2] + '.is']
            im = Image.open(io.BytesIO(src[src.find(b'\x89PNG'):]))
            if im.mode != 'RGB':       # alpha pictures: the encoder coarsens the colors of translucent pixels
                continue
            pic = H.decode_picture(self.files[n])
            pixels = getattr(im, 'get_flattened_data', im.getdata)()
            self.assertEqual(pic['rgb565'], [r565(p) for p in pixels], n)

    def test_decode_all_animations(self):
        for a in self.proj['animations']:
            frames = H.gmov_decode(self.files[a['file']])
            self.assertEqual(len(frames), a['frames'])
            for f in frames:
                self.assertEqual(len(f['rgb565']), f['w'] * f['h'])

    def test_fonts(self):
        for f in self.proj['fonts']:
            hd = f['header']
            self.assertEqual(f['glyphs'], hd['trueziqty'])
            b = self.files[f['file']]
            for code in (0x41, 0x30) if hd['state'] == 0 or hd['encode'] == 24 else ():
                g = H.font_glyph(b, code, hd)
                if g:
                    self.assertEqual(len(g['alpha']), g['w'] * g['h'])
                    self.assertTrue(any(g['alpha']), 'blank glyph %r' % chr(code))

    def test_named_headers(self):
        ph = self.proj['project_header']
        self.assertEqual(ph['resources_count'], len(self.proj['pictures']) + len(self.proj['pages']) +
                         len(self.proj['animations']) + len(self.proj['fonts']))
        self.assertEqual(self.proj['device']['crc'], ph['model_crc'])
        for pg in self.proj['pages']:
            self.assertEqual(pg['header']['name'], pg['name'])
            self.assertEqual(pg['header']['object_count'], len(pg['objects']))

    def test_animation_frames(self):
        for a in self.proj['animations']:
            self.assertEqual(a['frames'], a['source_frames'])


if __name__ == '__main__':
    unittest.main()
