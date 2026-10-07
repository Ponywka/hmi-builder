"""Portable source-only projects: export from a .HMI, rebuild from assets, insert/delete without renumbering."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hmi_config as C
import hmi_integrity as I
import hmi_model as M
import hmi_parse as H
import hmi_portable as PT
import hmi_project as P
import structfix as X
from test_hmi_project import png_2x2

HMI_FILE = os.environ.get('HMI_FILE')
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def pages():
    return [
        ('main', [X.button('b0', pic=0), X.button('b1', pic=1, events={'codesup': [
            'page 1', 'b[2].txt="x"', 'vis 1,1', 'if(b0.pic==1)', 'pic 0,0,0', 't0.font=1', 'page third', 'prints 0x65,1']}),
            X.text('t0', font=1)], {'left': 1, 'right': 2}),
        ('second', [X.button('b0', pic=1, events={'codesup': ['page 0', 'p[0].b[1].pic=1']}), X.text('n0', font=1)], {}),
        ('third', [X.button('b0'), X.button('b1')], {'up': 0}),
    ]


class Base(unittest.TestCase):
    pictures = 0
    kwargs = {}

    def setUp(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='portable-test-')
        self.dir = Path(self.temp.name)
        ws, files = X.project(self.dir, pages(), pictures=self.pictures, fonts=2, extras=True, **self.kwargs)
        self.files = dict(files)
        self.fixup()
        self.source = self.dir / 'source.HMI'
        self.source.write_bytes(H.write_container(self.files, order=list(self.files)))
        self.out = self.dir / 'portable'
        PT.export_portable(self.source, self.out)

    def fixup(self):
        if self.pictures:
            import hmi_image
            for i in range(self.pictures):
                n = 10 + i
                self.files['%d.i' % n], self.files['%d.is' % n] = hmi_image.encode_png(png_2x2())

    def tearDown(self):
        self.temp.cleanup()

    def compile(self):
        return C.compile_project(self.out, 'project.json')

    def cfg(self, fn):
        X.edit_config(self.out, fn)

    def page_doc(self, key, fn):
        path = self.out / 'pages' / (key + '.json')
        doc = json.loads(path.read_text())
        fn(doc)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1))

    def parse(self, files, index):
        idx = M.parse_index(files['main.HMI'])
        names = [n for k, n in idx.pairs() if k == 'pa']
        return M.parse_page(files[names[index]])

    def lines(self, files, page, obj, event='codesup'):
        return [x.decode() for x in self.parse(files, page).objects[obj].event(event).lines]


class ExportTest(Base):
    def test_layout_of_the_directory(self):
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ['Program.s', 'animations', 'fonts', 'pages', 'pictures', 'project.json'])
        cfg = json.loads((self.out / 'project.json').read_text())
        self.assertTrue(cfg['portable'])
        self.assertEqual([p['key'] for p in cfg['pages']], ['main', 'second', 'third'])
        self.assertEqual(cfg['project']['index_order'], ['zi', 'pa'])
        self.assertEqual(sorted(cfg['layouts']), ['116', '121', '98'])
        self.assertFalse((self.out / 'files').exists() or (self.out / 'manifest.json').exists())

    def test_no_numeric_ids_are_left_in_references(self):
        doc = json.loads((self.out / 'pages/main.json').read_text())
        self.assertEqual(doc['root']['attributes']['left'], {'$ref': 'page:second'})
        self.assertEqual(doc['root']['attributes']['right'], {'$ref': 'page:third'})
        self.assertEqual(doc['objects'][2]['attributes']['font'], {'$ref': 'font:font_1'})
        lines = doc['objects'][1]['events']['codesup']
        self.assertEqual(lines, ['page ${page:second}', 'b[${obj:main/b1}].txt="x"', 'vis ${obj:main/b0},1',
                                 'if(b0.pic==1)', 'pic 0,0,0', 't0.font=${font:font_1}', 'page third', 'prints 0x65,1'])
        self.assertEqual((self.out / 'Program.s').read_bytes(), b'page ${page:main}\n')
        self.assertIn('p[${page:main}].b[${obj:main/b0}].pic=1', json.dumps(
            json.loads((self.out / 'pages/second.json').read_text())))

    def test_derived_and_default_fields_are_omitted(self):
        attrs = json.loads((self.out / 'pages/main.json').read_text())['objects'][0]['attributes']
        self.assertNotIn('endx', attrs)
        self.assertNotIn('type', attrs)
        self.assertNotIn('id', attrs)
        doc = json.loads((self.out / 'pages/main.json').read_text())
        self.assertEqual(doc['objects'][0]['extra'], 101)           # distinct table word survives
        self.assertEqual(doc['objects'][0]['tail'], '01000007')

    def test_destination_must_not_exist_and_failures_clean_up(self):
        with self.assertRaises(FileExistsError):
            PT.export_portable(self.source, self.out)
        target = self.dir / 'again'
        real = P.json_bytes
        with mock.patch.object(P, 'json_bytes', side_effect=OSError('disk full')), self.assertRaises(OSError):
            PT.export_portable(self.source, target)
        self.assertFalse(target.exists())
        PT.export_portable(self.source, target)

    def test_unsupported_projects_are_refused(self):
        files = dict(self.files)
        files['9.zzz'] = b'x'
        src = self.dir / 'extra.HMI'
        src.write_bytes(H.write_container(files, order=list(files)))
        with self.assertRaisesRegex(C.ConfigError, 'outside the project index'):
            PT.export_portable(src, self.dir / 'x')
        self.assertFalse((self.dir / 'x').exists())


class BuildTest(Base):
    def test_unedited_project_rebuilds_identical_pages_and_script(self):
        cand = self.compile()
        for kind in ('pa', 'zi'):
            a = [n for k, n in M.parse_index(self.files['main.HMI']).pairs() if k == kind]
            b = [n for k, n in M.parse_index(cand.files['main.HMI']).pairs() if k == kind]
            self.assertEqual([self.files[n] for n in a], [cand.files[n] for n in b], kind)
        self.assertEqual(cand.files['Program.s'], self.files['Program.s'])
        self.assertEqual(H.check(cand.files), [])

    def test_build_is_deterministic_and_packs(self):
        a, b = self.compile(), self.compile()
        self.assertEqual(a.files, b.files)
        out = self.dir / 'o.HMI'
        P.pack_config(self.out, out, 'project.json')
        self.assertEqual(H.check(H.read_container(out.read_bytes())), [])

    def test_insert_a_page_in_the_middle_without_renumbering_anything_by_hand(self):
        self.cfg(lambda c: c['pages'].insert(1, {'key': 'new', 'content': {'mode': 'inline', 'name': 'new', 'objects': [
            {'key': 'go', 'type': 'button', 'attributes': {'objname': 'go', 'x': 1, 'y': 2, 'w': 30, 'h': 10},
             'events': {'codesup': ['page ${page:third}']}}]}}))
        cand = self.compile()
        self.assertEqual([self.parse(cand.files, i).name for i in range(4)], ['main', 'new', 'second', 'third'])
        main = self.parse(cand.files, 0)
        self.assertEqual(M.decode_value('left', main.objects[0].get('left').value), 2)       # 'second' moved to id 2
        self.assertEqual(M.decode_value('right', main.objects[0].get('right').value), 3)
        self.assertEqual(self.lines(cand.files, 0, 2)[0], 'page 2')
        self.assertEqual(self.lines(cand.files, 1, 1), ['page 3'])
        self.assertEqual(cand.files['Program.s'], b'page 0\n')

    def test_delete_a_component_in_the_middle(self):
        self.page_doc('main', lambda d: d['objects'].pop(0))
        # b0 is referenced by 'vis ${obj:main/b0}' -> unknown reference, not a silently wrong id
        with self.assertRaisesRegex(C.ConfigError, 'Unknown reference'):
            self.compile()
        self.page_doc('main', lambda d: d['objects'][0]['events'].update(codesup=['b[${obj:main/b1}].txt="x"']))
        with self.assertRaisesRegex(C.ConfigError, 'obj:main/b0'):
            self.compile()                      # page 'second' still points at the deleted component
        self.page_doc('second', lambda d: d['objects'][0]['events'].update(codesup=['page ${page:main}']))
        cand = self.compile()
        self.assertEqual(self.lines(cand.files, 0, 1), ['b[1].txt="x"'])

    def test_deleting_a_referenced_page_is_an_unknown_reference(self):
        self.cfg(lambda c: c['pages'].pop(1))
        with self.assertRaisesRegex(C.ConfigError, 'Unknown|unknown'):
            self.compile()

    def test_reordering_pages_moves_every_reference(self):
        self.cfg(lambda c: c['pages'].reverse())
        cand = self.compile()
        self.assertEqual([self.parse(cand.files, i).name for i in range(3)], ['third', 'second', 'main'])
        self.assertEqual(self.lines(cand.files, 2, 2)[0], 'page 1')
        self.assertEqual(cand.files['Program.s'], b'page 2\n')

    def test_plain_numbers_are_rejected(self):
        self.page_doc('main', lambda d: d['objects'][1]['events'].update(codesup=['page 1']))
        with self.assertRaisesRegex(C.ConfigError, r'\$\{page:key\}'):
            self.compile()
        self.page_doc('main', lambda d: d['objects'][1]['events'].update(codesup=['page ${page:second}']))
        self.page_doc('main', lambda d: d['objects'][2]['attributes'].update(font=1))
        with self.assertRaisesRegex(C.ConfigError, r'\$ref'):
            self.compile()
        self.page_doc('main', lambda d: d['objects'][2]['attributes'].update(font=9))     # not a font id: stays a number
        self.compile()

    def test_fresh_component_needs_only_the_essentials(self):
        self.page_doc('third', lambda d: d['objects'].append({'key': 'x', 'type': 'button', 'attributes': {
            'objname': 'x', 'x': 10, 'y': 20, 'w': 30, 'h': 40, 'txt': 'Go'}}))
        cand = self.compile()
        o = self.parse(cand.files, 2).objects[3]
        self.assertEqual(M.decode_value('endy', o.get('endy').value), 59)
        self.assertEqual(M.decode_value('txt', o.get('txt').value), 'Go')
        self.assertEqual([e.label for e in o.events], ['codesdown', 'codesup'])

    def test_validation_errors(self):
        cases = [
            (lambda d: d['objects'][0]['attributes'].update(nothing=1), 'unknown attribute'),
            (lambda d: d['objects'][0]['attributes'].pop('objname'), 'missing'),
            (lambda d: d['objects'][0].update(key='b1'), 'duplicate object key'),
            (lambda d: d['objects'][0]['events'].update(up=[]), 'start with'),
            (lambda d: d['objects'][0].update(extra=-1), 'extra'),
            (lambda d: d['objects'][0].update(tail='zz'), 'tail'),
            (lambda d: d['objects'][0].update(type='slider'), 'layout'),
            (lambda d: d['objects'][0]['attributes'].update(w=70000), 'does not fit'),
            (lambda d: d['objects'][0]['attributes'].update(pic={'$ref': 'font:font_0'}), 'picture reference'),
            (lambda d: d.update(name='x' * 17), 'invalid page name'),
        ]
        original = (self.out / 'pages/third.json').read_text()
        for fn, pattern in cases:
            with self.subTest(pattern=pattern):
                (self.out / 'pages/third.json').write_text(original)
                self.page_doc('third', fn)
                with self.assertRaisesRegex(C.ConfigError, pattern):
                    self.compile()

    def test_config_errors(self):
        original = (self.out / 'project.json').read_text()
        for fn, pattern in [(lambda c: c.update(extra=1), 'unknown field'),
                            (lambda c: c['project'].update(header='zz'), 'hex'),
                            (lambda c: c['project'].update(header='00'), '96 bytes'),
                            (lambda c: c['pages'][0].update(key=c['pages'][1]['key']), 'Duplicate page key'),
                            (lambda c: c['pages'][0]['content'].update(path='../x.json'), 'escapes'),
                            (lambda c: c['fonts'][0]['source'].update(zi='../x'), 'escapes'),
                            (lambda c: c['pages'].clear(), '1..255 pages'),
                            (lambda c: c.update(pages=5), 'must be a list')]:
            with self.subTest(pattern=pattern):
                (self.out / 'project.json').write_text(original)
                self.cfg(fn)
                with self.assertRaisesRegex(Exception, pattern):
                    self.compile()

    def test_cli_and_no_optional_packages_without_pictures(self):
        out = self.dir / 'cli.HMI'
        code = ("import sys; sys.modules['PIL']=None; sys.modules['fontTools']=None; sys.path.insert(0, %r);"
                "import hmi_project as P; P.pack_config(%r, %r, 'project.json')" % (str(ROOT), str(self.out), str(out)))
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(H.check(H.read_container(out.read_bytes())), [])


@unittest.skipUnless(HAVE_PIL, 'Pillow is needed to encode the PNG pictures')
class PictureTest(Base):
    pictures = 3

    def test_pictures_are_png_assets_and_ids_follow_the_list(self):
        pngs = sorted(p.name for p in (self.out / 'pictures').iterdir())
        self.assertEqual(pngs, ['pic_10.png', 'pic_11.png', 'pic_12.png'])
        self.assertEqual((self.out / 'pictures/pic_10.png').read_bytes(), png_2x2())
        doc = json.loads((self.out / 'pages/main.json').read_text())
        self.assertEqual(doc['objects'][0]['attributes']['pic'], {'$ref': 'picture:pic_10'})
        cand = self.compile()
        self.assertEqual(X.resolved_pictures(cand.files)['main']['b0']['pic'], '0.i')

    def test_removing_a_picture_in_the_middle_needs_no_renumbering(self):
        self.cfg(lambda c: c['pictures'].pop(0))
        with self.assertRaisesRegex(C.ConfigError, 'unknown|Unknown'):
            self.compile()                      # still used by main/b0
        self.page_doc('main', lambda d: d['objects'][0]['attributes'].update(pic=65535))
        self.page_doc('main', lambda d: d['objects'][1]['events'].update(codesup=['page ${page:second}', 'pic 0,0,${picture:pic_11}']))
        cand = self.compile()
        self.assertEqual(self.lines(cand.files, 0, 2)[1], 'pic 0,0,0')
        self.assertEqual(sum(1 for n in cand.files if n.endswith('.i')), 2)

    def test_edited_png_is_picked_up_and_new_picture_added(self):
        from PIL import Image
        Image.new('RGBA', (5, 3), (10, 200, 30, 255)).save(self.out / 'pictures/pic_11.png')
        shutil.copy(self.out / 'pictures/pic_10.png', self.out / 'pictures/extra.png')
        self.cfg(lambda c: c['pictures'].insert(1, {'key': 'extra', 'source': {'png': 'pictures/extra.png'}}))
        cand = self.compile()
        names = [n for k, n in M.parse_index(cand.files['main.HMI']).pairs() if k == 'i']
        self.assertEqual(len(names), 4)
        self.assertEqual([H.decode_picture(cand.files[n])['w'] for n in names], [2, 2, 5, 2])


@unittest.skipUnless(HAVE_PIL, 'Pillow is needed to encode animations')
class AnimationTest(Base):
    kwargs = {'animations': 2}

    def fixup(self):
        import hmi_factory as F
        page = M.parse_page(self.files['0.pa'])
        o = F.make_object(116, dict(objname='a0', x=0, y=0, w=10, h=10), object_id=4)
        head = [a for a in o.attrs][:21]
        head[0].value = bytes((2,))
        obj = M.Obj(head + [M.Attr('vid', (1).to_bytes(2, 'little'))], [M.Event('codesdown', []), M.Event('codesup', []),
                                                                        M.Event('codesplayend', [])])
        page.objects.append(obj)
        self.files['0.pa'] = I.sign_page(page.to_bytes())

    def test_frames_are_png_files_and_vid_is_symbolic(self):
        cfg = json.loads((self.out / 'project.json').read_text())
        self.assertEqual([a['key'] for a in cfg['animations']], ['anim_20', 'anim_21'])
        self.assertEqual(cfg['animations'][1]['frames'], [{'png': 'animations/anim_21/000.png', 'ms': 101},
                                                          {'png': 'animations/anim_21/001.png', 'ms': 200}])
        self.assertEqual((self.out / 'animations/anim_20/000.png').read_bytes(), png_2x2())
        doc = json.loads((self.out / 'pages/main.json').read_text())
        self.assertEqual(doc['objects'][-1]['attributes']['vid'], {'$ref': 'animation:anim_21'})

    def test_rebuild_and_reorder(self):
        import hmi_anim
        cand = self.compile()
        names = [n for k, n in M.parse_index(cand.files['main.HMI']).pairs() if k == 'gmov']
        self.assertEqual(len(names), 2)
        info = hmi_anim.validate_animation(cand.files[names[1]])
        self.assertEqual([f['duration_ms'] for f in info['frames']], [101, 200])
        self.assertEqual(M.decode_value('vid', self.parse(cand.files, 0).objects[-1].get('vid').value), 1)
        self.cfg(lambda c: c['animations'].reverse())
        cand = self.compile()
        self.assertEqual(M.decode_value('vid', self.parse(cand.files, 0).objects[-1].get('vid').value), 0)
        self.assertEqual(M.decode_value('x', self.parse(cand.files, 0).objects[-1].get('x').value), 0)

    def test_frames_can_be_edited_added_and_removed(self):
        from PIL import Image
        shutil.copy(self.out / 'animations/anim_20/000.png', self.out / 'animations/anim_20/002.png')
        self.cfg(lambda c: c['animations'][0]['frames'].append({'png': 'animations/anim_20/002.png', 'ms': 30}))
        Image.new('RGBA', (2, 2), (255, 0, 0, 255)).save(self.out / 'animations/anim_20/000.png')
        cand = self.compile()
        name = [n for k, n in M.parse_index(cand.files['main.HMI']).pairs() if k == 'gmov'][0]
        frames = H.gmov_decode(cand.files[name])
        self.assertEqual([f['duration_ms'] for f in frames], [100, 200, 30])
        self.assertEqual(frames[0]['rgb565'][0], 0xf800)

    def test_deleting_a_used_animation_is_an_unknown_reference(self):
        self.cfg(lambda c: c['animations'].pop(1))
        with self.assertRaisesRegex(C.ConfigError, 'unknown animation'):
            self.compile()

    def test_binary_fallback_without_source(self):
        files = dict(self.files)
        del files['21.gmovs']
        src = self.dir / 'nosrc.HMI'
        src.write_bytes(H.write_container(files, order=list(files)))
        out = self.dir / 'nosrc'
        PT.export_portable(src, out)
        cfg = json.loads((out / 'project.json').read_text())
        self.assertEqual(cfg['animations'][1], {'key': 'anim_21', 'gmov': 'animations/anim_21.gmov'})
        self.assertEqual(cfg['animations'][0]['frames'][0]['ms'], 100)
        cand = C.compile_project(out, 'project.json')
        self.assertEqual(len([n for n in cand.files if n.endswith('.gmov')]), 2)


@unittest.skipUnless(HMI_FILE and HAVE_PIL, 'set HMI_FILE (and install Pillow) for the real-project round trip')
class RealProjectTest(unittest.TestCase):
    def test_round_trip_of_the_sample_project(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='portable-real-') as d:
            out = Path(d) / 'p'
            PT.export_portable(HMI_FILE, out)
            self.assertFalse((out / 'files').exists())
            cand = C.compile_project(out, 'project.json')
            a = H.read_container(Path(HMI_FILE).read_bytes())
            ia, ib = M.parse_index(a['main.HMI']).pairs(), M.parse_index(cand.files['main.HMI']).pairs()
            self.assertEqual([k for k, _ in ia], [k for k, _ in ib])
            for kind in ('pa', 'zi'):
                fa = [n for k, n in ia if k == kind]
                fb = [n for k, n in ib if k == kind]
                self.assertTrue(all(a[x] == cand.files[y] for x, y in zip(fa, fb)), kind)
            for x, y in zip([n for k, n in ia if k == 'gmov'], [n for k, n in ib if k == 'gmov']):
                self.assertEqual(a[x + 's'], cand.files[y + 's'])
            self.assertEqual(cand.files['Program.s'], a['Program.s'])
            for x, y in zip([n for k, n in ia if k == 'gmov'], [n for k, n in ib if k == 'gmov']):
                self.assertEqual(a[x + 's'], cand.files[y + 's'])         # editor source files reproduced exactly
                for fa_, fb_ in zip(H.gmov_decode(a[x]), H.gmov_decode(cand.files[y])):
                    self.assertEqual((fa_['w'], fa_['h'], fa_['alpha'], fa_['duration_ms']),
                                     (fb_['w'], fb_['h'], fb_['alpha'], fb_['duration_ms']))
            for x, y in zip([n for k, n in ia if k == 'i'], [n for k, n in ib if k == 'i']):
                self.assertEqual(a[x + 's'][27:], cand.files[y + 's'][27:])
                da, db = H.decode_picture(a[x]), H.decode_picture(cand.files[y])
                self.assertEqual((da['w'], da['h']), (db['w'], db['h']))
            self.assertEqual(H.check(cand.files), [])
            # inserting a page in the middle needs no hand renumbering and keeps the project valid
            X.edit_config(out, lambda c: c['pages'].insert(40, {'key': 'extra', 'content': {'mode': 'inline', 'name': 'extra', 'objects': []}}))
            cand2 = C.compile_project(out, 'project.json')
            self.assertEqual(H.check(cand2.files), [])
            self.assertEqual(sum(1 for n in cand2.files if n.endswith('.pa')), 95)


if __name__ == '__main__':
    unittest.main()
