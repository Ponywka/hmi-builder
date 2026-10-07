"""Structural JSON config: add/edit/delete/reorder pages, components, pictures and fonts, with reference remapping."""
import json
import os
from pathlib import Path
import shutil
import struct
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
import hmi_project as P
import structfix as X
from test_hmi_project import png_2x2

HMI_FILE = os.environ.get('HMI_FILE')
try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False
DEJAVU = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'


def standard_pages():
    return [
        ('main', [X.button('b0', pic=0),
                  X.button('b1', pic=2, picc=1, events={'codesup': ['page 1', 'b[2].txt="x"', 'pic 0,0,2', 'if(b0.pic==2)',
                                                                      'vis 1,1', 'page third']}),
                  X.text('t0', font=1)], {'left': 1, 'right': 2}),
        ('second', [X.button('b0', pic=2, events={'codesup': ['page 0', 'b[1].pic=2']}), X.text('n0', font=1)], {}),
        ('third', [X.button('b0'), X.button('b1'), X.button('b2')], {'up': 0}),
    ]


class Base(unittest.TestCase):
    pages = None
    kwargs = {}

    def setUp(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='config-test-')
        self.dir = Path(self.temp.name)
        self.ws, self.files = X.project(self.dir, self.pages or standard_pages(), fonts=2, **self.kwargs)
        self.export = C.export_config(self.ws)

    def tearDown(self):
        self.temp.cleanup()

    def compile(self):
        return C.compile_project(self.ws, 'project.json')

    def cfg(self, fn):
        X.edit_config(self.ws, fn)

    def page(self, key, fn):
        X.edit_page(self.ws, key, fn)

    def parse(self, files, name):
        return M.parse_page(files[name])

    def names(self, files):
        idx = M.parse_index(files['main.HMI'])
        return [n for k, n in idx.pairs() if k == 'pa']

    def lines(self, files, page_file, obj, event='codesup'):
        return [x.decode() for x in M.parse_page(files[page_file]).objects[obj].event(event).lines]

    def attr(self, files, page_file, obj, name):
        return M.decode_value(name, M.parse_page(files[page_file]).objects[obj].get(name).value)

    def remap(self):
        self.cfg(lambda c: c.update(id_policy='remap'))


class ExportAndNoopTest(Base):
    def test_export_layout(self):
        cfg = json.loads((self.ws / 'project.json').read_text())
        self.assertEqual(cfg['format'], C.FORMAT)
        self.assertEqual([p['key'] for p in cfg['pages']], ['main', 'second', 'third'])
        self.assertEqual([p['origin']['file'] for p in cfg['pictures']], ['10.i', '11.i', '12.i'])
        self.assertEqual(cfg['id_policy'], 'preserve')
        page = json.loads((self.ws / 'pages/main.json').read_text())
        self.assertEqual([o['key'] for o in page['objects']], ['b0', 'b1', 't0'])
        self.assertEqual(page['objects'][1]['origin'], {'file': '0.pa', 'index': 2})
        self.assertNotIn('id', page['objects'][0]['attributes'])
        self.assertEqual(page['root']['attributes']['left'], 1)

    def test_export_refuses_to_overwrite(self):
        with self.assertRaises(FileExistsError):
            C.export_config(self.ws)

    def test_noop_reproduces_every_file_and_the_order(self):
        cand = self.compile()
        self.assertEqual(list(cand.files), list(self.files))
        self.assertEqual(cand.files, self.files)
        self.assertEqual(cand.changed, set())
        self.assertEqual(cand.report['rewritten_code_lines'], 0)

    def test_noop_pack_matches_legacy_pack(self):
        out = self.dir / 'config.HMI'
        legacy = self.dir / 'legacy.HMI'
        P.pack_config(self.ws, out, 'project.json')
        P.pack(self.ws, legacy)
        self.assertEqual(out.read_bytes(), legacy.read_bytes())

    def test_raw_mode_is_the_same_as_json_mode(self):
        self.cfg(lambda c: c['pages'][0].update(content={'mode': 'raw'}))
        self.assertEqual(self.compile().files, self.files)

    def test_legacy_page_json_workspaces_cannot_be_exported_over(self):
        legacy = self.dir / 'legacy'
        P.unpack(self.dir / 'source.HMI', legacy, pages=True)
        with self.assertRaisesRegex(FileExistsError, 'legacy page JSON'):
            C.export_config(legacy)
        self.assertFalse((legacy / 'project.json').exists())

    def test_update_snapshot_after_a_deliberate_raw_edit(self):
        (self.ws / 'files/Program.s').write_bytes(b'int a=1\npage 0\n')
        result = C.update_snapshot(self.ws)
        self.assertTrue(result['changed'])
        self.assertEqual(self.compile().files['Program.s'], b'int a=1\npage 0\n')
        self.assertFalse(C.update_snapshot(self.ws)['changed'])

    def test_placeholders_work_in_program_s(self):
        (self.ws / 'files/Program.s').write_bytes(b'page ${page:second}\n')
        C.update_snapshot(self.ws)
        self.assertEqual(self.compile().files['Program.s'], b'page 1\n')

    def rebuild(self, files, name):
        shutil.rmtree(self.dir / name, ignore_errors=True)
        (self.dir / name).mkdir()
        source = self.dir / name / 's.HMI'
        source.write_bytes(H.write_container(files, order=list(files)))
        ws = self.dir / name / 'ws'
        P.unpack(source, ws, pages=False)
        return ws

    def test_binary_lines_and_orphan_files_survive_a_noop(self):
        files = dict(self.files)
        page = M.parse_page(files['0.pa'])
        page.objects[2].events[1].lines.append(b'\xff\xfe not utf8')
        files['0.pa'] = I.sign_page(page.to_bytes())
        files['999.i'] = X.picture(7)               # present in the container but not in the index
        files['999.is'] = b'orphan'
        ws = self.rebuild(files, 'binary')
        C.export_config(ws)
        cand = C.compile_project(ws, 'project.json')
        self.assertEqual(cand.files, files)
        self.assertEqual(list(cand.files), list(files))

    def test_duplicate_index_entries_are_refused(self):
        files = dict(self.files)
        idx = M.parse_index(files['main.HMI'])
        idx.records.append(idx.records[0])
        files['main.HMI'] = I.sign_index(idx.to_bytes())
        ws = self.rebuild(files, 'dup')
        with self.assertRaisesRegex(C.ConfigError, 'twice'):
            C.export_config(ws)

    def test_stale_snapshot_is_rejected(self):
        (self.ws / 'files/Program.s').write_bytes(b'page 1\n')
        with self.assertRaisesRegex(C.ConfigError, 'snapshot'):
            self.compile()


class AddTest(Base):
    def add_page(self, c):
        c['pages'].append({'key': 'settings', 'content': {'mode': 'inline', 'name': 'settings', 'root': {
            'attributes': {'sta': 1, 'bco': 63519}}, 'objects': [
            {'key': 'title', 'type': 'text', 'attributes': {'objname': 'title', 'x': 20, 'y': 20, 'w': 220, 'h': 30,
                                                           'txt': 'Settings', 'font': {'$ref': 'font:font_1'}}},
            {'key': 'back', 'type': 'button', 'attributes': {'objname': 'back', 'x': 20, 'y': 70, 'w': 120, 'h': 36,
                                                            'txt': 'Back', 'pic': {'$ref': 'picture:pic_11'}},
             'events': {'codesup': ['page ${page:main}', 'b0.pic=${picture:pic_12}']}}]}})

    def test_new_page_with_fresh_components(self):
        self.cfg(self.add_page)
        cand = self.compile()
        self.assertEqual(cand.report['added'], ['3.pa'])
        self.assertEqual(cand.report['modified'], ['main.HMI'])
        self.assertEqual(self.names(cand.files)[-1], '3.pa')
        page = self.parse(cand.files, '3.pa')
        self.assertEqual(page.name, 'settings')
        self.assertEqual([o.get('objname').value for o in page.objects], [b'settings', b'title', b'back'])
        self.assertEqual([o.get('id').value[0] for o in page.objects], [0, 1, 2])
        self.assertEqual(self.attr(cand.files, '3.pa', 0, 'w'), 272)              # size taken from the existing pages
        self.assertEqual(self.attr(cand.files, '3.pa', 1, 'font'), 1)
        self.assertEqual(self.attr(cand.files, '3.pa', 2, 'pic'), 1)
        self.assertEqual(self.attr(cand.files, '3.pa', 2, 'endx'), 139)
        self.assertEqual(self.lines(cand.files, '3.pa', 2), ['page 0', 'b0.pic=2'])
        self.assertEqual(H.check(cand.files), [])
        for name in cand.report['added']:
            self.assertNotIn(name, self.files)
        # every unrelated file is untouched
        self.assertTrue(all(cand.files[n] == self.files[n] for n in self.files if n != 'main.HMI'))

    def test_new_page_builds_with_the_standalone_packer(self):
        self.cfg(self.add_page)
        out = self.dir / 'new.HMI'
        result = P.pack_config(self.ws, out, 'project.json')
        self.assertIn('3.pa', result['changed_files'])
        files = H.read_container(out.read_bytes())
        self.assertTrue(I.verify_page(files['3.pa']))
        self.assertTrue(I.verify_index(files['main.HMI']))
        self.assertEqual(H.check(files), [])

    @unittest.skipUnless(HAVE_PIL, 'Pillow is needed to read PNG sources')
    def test_new_picture_from_png(self):
        (self.ws / 'assets').mkdir(exist_ok=True)
        (self.ws / 'assets/logo.png').write_bytes(png_2x2())
        self.cfg(lambda c: c['pictures'].append({'key': 'logo', 'source': {'png': 'assets/logo.png'}}))
        self.cfg(self.add_page)
        self.page_with_logo()
        cand = self.compile()
        self.assertEqual(cand.report['added'], ['13.i', '13.is', '3.pa'])
        self.assertEqual(cand.files['13.is'][27:], png_2x2())
        self.assertEqual(H.decode_picture(cand.files['13.i'])['w'], 2)
        idx = M.parse_index(cand.files['main.HMI'])
        self.assertEqual([k for k, _ in idx.pairs()], ['i'] * 4 + ['zi'] * 2 + ['pa'] * 4)
        self.assertEqual(idx.pairs()[3], ('i', '13.i'))
        self.assertEqual(self.attr(cand.files, '3.pa', 2, 'pic'), 3)

    def page_with_logo(self):
        def fn(c):
            c['pages'][-1]['content']['objects'][1]['attributes']['pic'] = {'$ref': 'picture:logo'}
        self.cfg(fn)

    def test_append_keeps_every_existing_id(self):
        self.cfg(self.add_page)
        cand = self.compile()
        self.assertEqual(cand.report['page_id_changes'], {'settings': [None, 3]})
        self.assertEqual(X.resolved_pictures(cand.files)['main'], X.resolved_pictures(self.files)['main'])

    def test_inserting_in_the_middle_needs_remap(self):
        self.cfg(self.add_page)
        self.cfg(lambda c: c['pages'].insert(1, c['pages'].pop()))
        with self.assertRaisesRegex(C.ConfigError, 'id_policy'):
            self.compile()
        self.remap()
        cand = self.compile()
        self.assertEqual(cand.report['page_id_changes']['settings'], [None, 1])
        self.assertEqual(cand.report['page_id_changes']['second'], [1, 2])
        self.assertEqual(self.lines(cand.files, '1.pa', 1), ['page 0', 'b[1].pic=2'])    # page 0 unchanged
        self.assertEqual(self.lines(cand.files, '0.pa', 2)[0], 'page 2')                  # was page 1 = second
        self.assertEqual(self.attr(cand.files, '0.pa', 0, 'left'), 2)

    def test_new_objects_in_an_existing_page(self):
        def fn(doc):
            doc['objects'].append({'key': 'extra', 'type': 'button', 'attributes': {
                'objname': 'extra', 'x': 5, 'y': 6, 'w': 7, 'h': 8, 'txt': 'E'}, 'events': {'codesup': ['b[3].pic=1']}})
        self.page('third', fn)
        cand = self.compile()
        page = self.parse(cand.files, '2.pa')
        self.assertEqual(len(page.objects), 5)
        self.assertEqual(page.objects[4].get('id').value[0], 4)
        self.assertEqual(self.lines(cand.files, '2.pa', 4), ['b[3].pic=1'])
        self.assertEqual(cand.report['modified'], ['2.pa'])

    def test_object_and_name_placeholders(self):
        def fn(doc):
            doc['objects'][0]['events'] = {'codesup': ['click ${obj:main/b1},1', 'p[${page:third}].b[${obj:third/b2}].val=1',
                                                       '${objname:main/t0}.txt="${pagename:second}"']}
        self.page('third', fn)
        cand = self.compile()
        self.assertEqual(self.lines(cand.files, '2.pa', 1),
                         ['click 2,1', 'p[2].b[3].val=1', 't0.txt="second"'])

    def test_binary_and_non_utf8_code_lines_survive(self):
        self.page('third', lambda d: d['objects'][0]['events'].update(codesup=[{'hex': 'ff fe'.replace(' ', '')}, 'x=1']))
        cand = self.compile()
        self.assertEqual(self.parse(cand.files, '2.pa').objects[1].event('codesup').lines, [b'\xff\xfe', b'x=1'])

    def test_fresh_timer_and_variable(self):
        def fn(doc):
            doc['objects'] += [{'key': 'tm', 'type': 'timer', 'attributes': {'objname': 'tm0', 'tim': 500}},
                               {'key': 'v', 'type': 'variable', 'attributes': {'objname': 'v0', 'val': -3}}]
        self.page('second', fn)
        cand = self.compile()
        self.assertEqual(self.attr(cand.files, '1.pa', 3, 'tim'), 500)
        self.assertEqual(self.attr(cand.files, '1.pa', 4, 'val'), -3)

    @unittest.skipUnless(os.path.exists(DEJAVU), 'DejaVu font not installed')
    def test_new_font_from_ttf(self):
        try:
            import fontTools  # noqa: F401
        except ImportError:
            self.skipTest('fontTools is not installed')
        (self.ws / 'assets').mkdir(exist_ok=True)
        shutil.copy(DEJAVU, self.ws / 'assets/f.ttf')
        self.cfg(lambda c: c['fonts'].append({'key': 'new', 'source': {'ttf': 'assets/f.ttf', 'height': 16,
                                                                      'chars': 'AB', 'layout': 'ascii', 'bpp': 3}}))
        cand = self.compile()
        self.assertEqual(cand.report['added'], ['2.zi'])
        header = H.font_header(cand.files['2.zi'])
        self.assertEqual(header['h'], 16)
        self.assertEqual([k for k, _ in M.parse_index(cand.files['main.HMI']).pairs()].count('zi'), 3)

    def test_replace_picture_keeps_its_slot(self):
        if not HAVE_PIL:
            self.skipTest('Pillow is needed to read PNG sources')
        (self.ws / 'assets').mkdir(exist_ok=True)
        (self.ws / 'assets/new.png').write_bytes(png_2x2())
        self.cfg(lambda c: c['pictures'][1].update(source={'png': 'assets/new.png'}))
        cand = self.compile()
        self.assertEqual(sorted(cand.report['modified']), ['11.i', '11.is'])
        self.assertEqual(cand.files['11.is'][27:], png_2x2())
        self.assertEqual(cand.files['10.i'], self.files['10.i'])


@unittest.skipUnless(HAVE_PIL, 'Pillow is needed to encode PNG sources')
class PngWorkspaceTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='config-png-')
        self.dir = Path(self.temp.name)
        import hmi_image
        compiled, source = hmi_image.encode_png(png_2x2())
        self.pair = (compiled, source)
        ws, files = X.project(self.dir, [('main', [X.button('b0', pic=0)], {})], pictures=2, fonts=1)
        files = dict(files)
        files['10.i'], files['10.is'] = compiled, source
        shutil.rmtree(self.dir / 'w2', ignore_errors=True)
        (self.dir / 'w2').mkdir()
        src = self.dir / 'w2/s.HMI'
        src.write_bytes(H.write_container(files, order=list(files)))
        self.ws = self.dir / 'w2/ws'
        P.unpack(src, self.ws, pages=False)
        self.files = files

    def tearDown(self):
        self.temp.cleanup()

    def test_pictures_are_exported_as_png_files(self):
        C.export_config(self.ws)
        self.assertEqual((self.ws / 'pictures/pic_10.png').read_bytes(), png_2x2())
        cfg = json.loads((self.ws / 'project.json').read_text())
        self.assertEqual(cfg['pictures'][0]['source'], {'png': 'pictures/pic_10.png'})
        self.assertNotIn('source', cfg['pictures'][1])             # its .is is not a PNG wrapper
        self.assertEqual(C.compile_project(self.ws, 'project.json').files, self.files)   # untouched PNG: no re-encode

    def test_editing_a_png_rebuilds_only_that_picture(self):
        C.export_config(self.ws)
        from PIL import Image
        Image.new('RGBA', (5, 3), (10, 200, 30, 255)).save(self.ws / 'pictures/pic_10.png')
        cand = C.compile_project(self.ws, 'project.json')
        self.assertEqual(sorted(cand.report['modified']), ['10.i', '10.is'])
        self.assertEqual(H.decode_picture(cand.files['10.i'])['w'], 5)

    def test_failed_export_removes_the_assets_it_made(self):
        real = P.json_bytes
        n = []

        def flaky(obj):
            n.append(1)
            if len(n) == 2:
                raise OSError('disk full')
            return real(obj)
        with mock.patch.object(P, 'json_bytes', flaky), self.assertRaises(OSError):
            C.export_config(self.ws)
        self.assertFalse((self.ws / 'pictures').exists())
        self.assertFalse((self.ws / 'pages').exists())


class DeleteTest(Base):
    def test_delete_middle_picture_remaps_attributes_and_code(self):
        self.page('main', lambda d: d['objects'][1]['attributes'].update(picc=65535))
        self.cfg(lambda c: c['pictures'].pop(1))
        with self.assertRaisesRegex(C.ConfigError, 'id_policy'):
            self.compile()
        self.remap()
        cand = self.compile()
        self.assertEqual(sorted(cand.report['removed']), ['11.i', '11.is'])
        self.assertNotIn('11.i', cand.files)
        before, after = X.resolved_pictures(self.files), X.resolved_pictures(cand.files)
        before['main']['b1']['picc'] = 'none'
        self.assertEqual(after, before)
        self.assertEqual(self.lines(cand.files, '0.pa', 2),
                         ['page 1', 'b[2].txt="x"', 'pic 0,0,1', 'if(b0.pic==1)', 'vis 1,1', 'page third'])
        self.assertEqual(self.lines(cand.files, '1.pa', 1)[1], 'b[1].pic=1')
        self.assertEqual(cand.report['picture_id_changes'], {'pic_12': [2, 1]})
        self.assertEqual(H.check(cand.files), [])

    def test_deleting_a_referenced_picture_is_a_dangling_error(self):
        self.remap()
        self.cfg(lambda c: c['pictures'].pop(2))
        with self.assertRaisesRegex(C.ConfigError, 'deleted picture'):
            self.compile()

    def test_deleting_the_last_picture_keeps_ids(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(pic=65535))
        self.page('main', lambda d: d['objects'][1]['attributes'].update(pic=65535))
        self.page('second', lambda d: d['objects'][0]['attributes'].update(pic=65535))
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['page 1']))
        self.page('second', lambda d: d['objects'][0]['events'].update(codesup=['page 0']))
        self.cfg(lambda c: c['pictures'].pop())
        cand = self.compile()          # preserve policy is fine: no survivor moves
        self.assertEqual(cand.report['removed'], ['12.i', '12.is'])

    def test_delete_middle_page(self):
        self.remap()
        self.page('main', lambda d: d['objects'][1]['events']['codesup'].__setitem__(0, 'page 2'))
        self.cfg(lambda c: c['pages'].pop(1))
        with self.assertRaisesRegex(C.ConfigError, 'deleted'):
            self.compile()                                    # swipe-right/left still point at 'second'

    def test_delete_page_after_fixing_references(self):
        self.remap()
        self.page('main', lambda d: d['root']['attributes'].update(left=255))
        self.page('main', lambda d: d['objects'][1]['events'].update(
            codesup=['b[2].txt="x"', 'pic 0,0,2', 'if(b0.pic==2)', 'vis 1,1', 'page third']))
        self.cfg(lambda c: c['pages'].pop(1))
        cand = self.compile()
        self.assertEqual(cand.report['removed'], ['1.pa'])
        self.assertEqual(self.names(cand.files), ['0.pa', '2.pa'])
        self.assertEqual(self.attr(cand.files, '0.pa', 0, 'right'), 1)            # third is now page 1
        self.assertEqual(self.lines(cand.files, '0.pa', 2)[-1], 'page third')
        self.assertEqual(H.check(cand.files), [])

    def test_page_ids_follow_the_final_order(self):
        self.remap()
        self.cfg(lambda c: c['pages'].reverse())
        cand = self.compile()
        self.assertEqual(self.names(cand.files), ['2.pa', '1.pa', '0.pa'])
        self.assertEqual(self.attr(cand.files, '0.pa', 0, 'left'), 1)             # 'second' stays second page: id 1
        self.assertEqual(self.attr(cand.files, '0.pa', 0, 'right'), 0)            # 'third' is now page 0
        self.assertEqual(self.lines(cand.files, '1.pa', 1)[0], 'page 2')          # 'main' is now page 2
        self.assertEqual(self.lines(cand.files, '0.pa', 2)[0], 'page 1')
        self.assertEqual(self.attr(cand.files, '2.pa', 0, 'up'), 2)

    def test_program_s_follows_page_zero(self):
        self.remap()
        self.cfg(lambda c: c['pages'].reverse())
        cand = self.compile()
        self.assertEqual(cand.files['Program.s'], b'page 2\n')

    def test_deleting_the_startup_page_is_dangling(self):
        self.remap()
        self.page('third', lambda d: d['root']['attributes'].update(up=255))
        self.page('second', lambda d: d['objects'][0]['events'].update(codesup=['b[1].pic=2']))
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['page 1']))
        self.page('main', lambda d: d['root']['attributes'].update(left=255, right=255))
        self.cfg(lambda c: c['pages'].pop(0))
        with self.assertRaisesRegex(C.ConfigError, 'Program.s'):
            self.compile()

    def test_delete_objects_and_renumber_component_ids(self):
        self.remap()
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['page 1', 'pic 0,0,2', 'page third']))
        self.page('main', lambda d: d['objects'].pop(0))        # b0 removed; 'if(b0.pic==2)' is gone with the line
        cand = self.compile()
        page = self.parse(cand.files, '0.pa')
        self.assertEqual([o.get('objname').value for o in page.objects], [b'main', b'b1', b't0'])
        self.assertEqual([o.get('id').value[0] for o in page.objects], [0, 1, 2])

    def test_name_references_to_removed_components_are_dangling(self):
        self.remap()
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['b0.txt="x"']))
        self.page('main', lambda d: d['objects'].pop(0))
        with self.assertRaisesRegex(C.ConfigError, 'b0 was deleted'):
            self.compile()


class ComponentReferenceTest(Base):
    pages = [('main', [X.button('b0'), X.button('b1', events={'codesup': [
        'b[2].txt="x"', 'vis 3,1', 'click 2,1', 'tsw 255,0', 'ref 0', 'p[0].b[3].pic=1']}), X.text('t0')], {})]

    def test_component_references_follow_the_final_ids(self):
        self.remap()
        self.page('main', lambda d: d['objects'].pop(0))        # b0 (id 1) removed: b1 -> 1, t0 -> 2
        cand = self.compile()
        self.assertEqual(self.lines(cand.files, '0.pa', 1),
                         ['b[1].txt="x"', 'vis 2,1', 'click 1,1', 'tsw 255,0', 'ref 0', 'p[0].b[2].pic=1'])

    def test_swapping_components_swaps_each_reference_once(self):
        self.remap()
        self.page('main', lambda d: d['objects'].__setitem__(slice(0, 2), [d['objects'][1], d['objects'][0]]))
        cand = self.compile()
        # b0 <-> b1 swap their ids (1 <-> 2); t0 keeps 3. The moved b1 now owns id 1.
        self.assertEqual(self.lines(cand.files, '0.pa', 1)[:3], ['b[1].txt="x"', 'vis 3,1', 'click 1,1'])
        self.assertEqual(self.lines(cand.files, '0.pa', 1)[5], 'p[0].b[3].pic=1')


class ReorderTest(Base):
    pages = [('main', [X.button('b0', x=1), X.button('b1', x=2), X.text('t0', x=3), X.button('b2', x=4)], {})]
    kwargs = {'extras': True}

    def test_metadata_follows_its_object(self):
        self.remap()
        self.page('main', lambda d: d['objects'].reverse())
        cand = self.compile()
        old = self.parse(self.files, '0.pa')
        new = self.parse(cand.files, '0.pa')
        self.assertEqual([o.get('objname').value for o in new.objects], [b'main', b'b2', b't0', b'b1', b'b0'])
        for new_index, old_index in ((1, 4), (2, 3), (3, 2), (4, 1)):
            self.assertEqual((new.objects[new_index].extra, new.objects[new_index].tail),
                             (old.objects[old_index].extra, old.objects[old_index].tail))
            self.assertEqual(new.objects[new_index].get('id').value[0], new_index)
        self.assertEqual((new.objects[0].extra, new.objects[0].tail), (old.objects[0].extra, old.objects[0].tail))

    def test_unchanged_blocks_keep_their_bytes(self):
        self.remap()
        self.page('main', lambda d: d['objects'].reverse())
        cand = self.compile()
        old = self.parse(self.files, '0.pa')
        new = self.parse(cand.files, '0.pa')
        # same records except the id attribute
        a, b = old.objects[4], new.objects[1]
        self.assertEqual([(x.name, x.value) for x in a.attrs if x.name != 'id'],
                         [(x.name, x.value) for x in b.attrs if x.name != 'id'])

    def test_geometry_edit_updates_the_end_coordinates(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(x=100, w=60))
        cand = self.compile()
        self.assertEqual(self.attr(cand.files, '0.pa', 1, 'endx'), 159)
        self.assertEqual(self.attr(cand.files, '0.pa', 1, 'endy'), 19)

    def test_copying_an_object_inside_the_page(self):
        def fn(doc):
            copy = json.loads(json.dumps(doc['objects'][0]))
            copy['key'] = 'b0_copy'
            copy['attributes']['objname'] = 'b0c'
            doc['objects'].append(copy)
        self.page('main', fn)
        cand = self.compile()
        page = self.parse(cand.files, '0.pa')
        self.assertEqual(page.objects[5].get('objname').value, b'b0c')
        self.assertEqual(page.objects[5].extra, page.objects[1].extra)           # inherited from the shared origin

    def test_object_limit(self):
        def fn(doc):
            for n in range(260):
                doc['objects'].append({'key': 'n%d' % n, 'type': 'text', 'attributes': {'objname': 'n%d' % n, 'x': 0, 'y': 0, 'w': 1, 'h': 1}})
        self.page('main', fn)
        with self.assertRaisesRegex(C.ConfigError, 'at most'):
            self.compile()


class ReferenceGateTest(Base):
    def dynamic_page(self, d):
        d['objects'][0]['events']['codesup'] = ['p[v.val].b[1].txt="x"', 'page cur.val']

    def test_deleting_a_picture_does_not_touch_other_domains(self):
        self.remap()
        self.page('main', lambda d: d['objects'][1]['attributes'].update(picc=65535))
        self.page('second', self.dynamic_page)
        self.cfg(lambda c: c['pictures'].pop(1))
        self.compile()

    def test_computed_page_references_need_an_acknowledgement(self):
        self.remap()
        self.page('second', self.dynamic_page)
        self.cfg(lambda c: c['pages'].reverse())
        with self.assertRaises(C.ConfigError) as cm:
            self.compile()
        message = str(cm.exception)
        self.assertIn('second/b0/codesup#1', message)
        self.assertIn('"sha256"', message)
        import re
        entries = [json.loads(x) for x in re.findall(r'acknowledge with: (\{.*?\})\n', message + '\n')]
        self.assertEqual(len(entries), 2)
        self.cfg(lambda c: c.update(acknowledge={'dynamic': entries}))
        cand = self.compile()
        self.assertEqual(cand.report['acknowledged']['dynamic'], 2)

    def test_acknowledgement_is_tied_to_the_exact_line(self):
        self.remap()
        self.page('second', self.dynamic_page)
        self.cfg(lambda c: c['pages'].reverse())
        self.cfg(lambda c: c.update(acknowledge={'dynamic': [{'site': 'second/b0/codesup#1', 'sha256': '0' * 64}]}))
        with self.assertRaisesRegex(C.ConfigError, 'acknowledge with'):
            self.compile()

    def test_stale_acknowledgements_are_rejected(self):
        self.cfg(lambda c: c.update(acknowledge={'dynamic': [{'site': 'main/b0/codesup#1', 'sha256': '0' * 64}]}))
        with self.assertRaisesRegex(C.ConfigError, 'stale'):
            self.compile()

    def test_external_ids_need_an_acknowledgement(self):
        self.remap()
        self.page('main', lambda d: d['objects'][0]['events'].update(codesup=['prints 0x65,1', 'prints dp,1']))
        self.page('main', lambda d: d['root']['attributes'].update(left=255, right=255))
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['page third']))
        self.page('second', lambda d: d['objects'][0]['events'].update(codesup=['page 0']))
        self.cfg(lambda c: c['pages'].pop(1))
        with self.assertRaisesRegex(C.ConfigError, 'visible outside the project'):
            self.compile()
        self.cfg(lambda c: c.update(acknowledge={'external_ids': ['*']}))
        cand = self.compile()
        self.assertEqual(cand.report['acknowledged']['external_ids'], ['third'])
        # raw UART constants are never rewritten
        self.assertEqual(self.lines(cand.files, '0.pa', 1), ['prints 0x65,1', 'prints dp,1'])

    def test_scoped_external_acknowledgement(self):
        self.remap()
        self.page('main', lambda d: d['objects'][0]['events'].update(codesup=['prints dp,1']))
        self.page('main', lambda d: d['root']['attributes'].update(left=255, right=255))
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['page third']))
        self.page('second', lambda d: d['objects'][0]['events'].update(codesup=['page 0']))
        self.cfg(lambda c: c['pages'].pop(1))
        self.cfg(lambda c: c.update(acknowledge={'external_ids': ['main']}))
        with self.assertRaisesRegex(C.ConfigError, 'third'):
            self.compile()
        self.cfg(lambda c: c.update(acknowledge={'external_ids': ['third']}))
        self.compile()

    def test_new_numeric_references_are_rejected_when_ids_move(self):
        self.remap()
        self.cfg(lambda c: c['pictures'].pop(1))
        self.page('main', lambda d: d['objects'][1]['attributes'].update(picc=65535))
        self.page('main', lambda d: d['objects'][0]['events'].update(codesup=['b0.pic=2']))
        with self.assertRaisesRegex(C.ConfigError, r'\$\{picture:key\}'):
            self.compile()
        self.page('main', lambda d: d['objects'][0]['events'].update(codesup=['b0.pic=${picture:pic_12}']))
        cand = self.compile()
        self.assertEqual(self.lines(cand.files, '0.pa', 1), ['b0.pic=1'])

    def test_unchanged_baseline_lines_are_remapped_but_new_ones_are_not_guessed(self):
        self.remap()
        self.page('main', lambda d: d['objects'][1]['attributes'].update(picc=65535))
        self.cfg(lambda c: c['pictures'].pop(1))
        self.page('second', lambda d: d['objects'][0]['events'].update(codesup=['page 0', 'b[1].pic=2', 'b0.pic=9']))
        cand = self.compile()       # 9 is not a known picture id: left alone, not an error
        self.assertEqual(self.lines(cand.files, '1.pa', 1), ['page 0', 'b[1].pic=1', 'b0.pic=9'])

    def test_new_numeric_attribute_values_need_a_reference_when_ids_move(self):
        self.remap()
        self.page('main', lambda d: d['objects'][1]['attributes'].update(picc=65535))
        self.cfg(lambda c: c['pictures'].pop(1))
        self.page('main', lambda d: d['objects'][0]['attributes'].update(pic=2))
        with self.assertRaisesRegex(C.ConfigError, r'\$ref'):
            self.compile()
        self.page('main', lambda d: d['objects'][0]['attributes'].update(pic={'$id': 2, 'namespace': 'baseline'}))
        self.assertEqual(self.attr(self.compile().files, '0.pa', 1, 'pic'), 1)
        self.page('main', lambda d: d['objects'][0]['attributes'].update(pic={'$id': 7, 'namespace': 'final'}))
        self.assertEqual(self.attr(self.compile().files, '0.pa', 1, 'pic'), 7)

    def test_unmodelled_component_types_block_renumbering(self):
        # a picture-VP component's picture list is not modelled: refuse to renumber pictures
        files = dict(self.files)
        page = M.parse_page(files['0.pa'])
        page.objects[1].get('type').value = bytes((131,))
        files['0.pa'] = I.sign_page(page.to_bytes())
        shutil.rmtree(self.dir / 'again', ignore_errors=True)
        (self.dir / 'again').mkdir()
        source = self.dir / 'again' / 's.HMI'
        source.write_bytes(H.write_container(files, order=list(files)))
        ws = self.dir / 'again' / 'ws'
        P.unpack(source, ws, pages=False)
        C.export_config(ws)
        X.edit_config(ws, lambda c: (c.update(id_policy='remap'), c['pictures'].pop(1)))
        with self.assertRaisesRegex(C.ConfigError, 'not modelled'):
            C.compile_project(ws, 'project.json')


class FontTest(Base):
    def test_delete_font_remaps_references(self):
        self.remap()

        def use_font_1(key):
            def fn(doc):
                for o in doc['objects']:
                    if 'font' in o['attributes']:
                        o['attributes']['font'] = {'$ref': 'font:font_1'}
            self.page(key, fn)
        for key in ('main', 'second', 'third'):
            use_font_1(key)
        self.cfg(lambda c: c['fonts'].pop(0))
        cand = self.compile()
        self.assertEqual(self.attr(cand.files, '0.pa', 3, 'font'), 0)
        self.assertEqual(self.attr(cand.files, '0.pa', 1, 'font'), 0)
        self.assertEqual(cand.report['removed'], ['0.zi'])
        self.assertEqual(cand.report['font_id_changes'], {'font_1': [1, 0]})

    def test_deleting_a_used_font_is_dangling(self):
        self.remap()
        self.cfg(lambda c: c['fonts'].pop(1))
        with self.assertRaisesRegex(C.ConfigError, 'deleted font'):
            self.compile()


class PageTest(Base):
    def test_rename_page_changes_the_header_and_root_name(self):
        self.page('main', lambda d: d['objects'][1]['events'].update(codesup=['page 1']))
        self.page('third', lambda d: d.update(name='fourth'))
        cand = self.compile()
        page = self.parse(cand.files, '2.pa')
        self.assertEqual((page.name, page.objects[0].get('objname').value), ('fourth', b'fourth'))

    def test_rename_breaks_name_references(self):
        self.page('third', lambda d: d.update(name='fourth'))
        with self.assertRaisesRegex(C.ConfigError, 'third was deleted or renamed'):
            self.compile()

    def test_copy_a_page_through_its_origin(self):
        def fn(c):
            entry = json.loads(json.dumps(c['pages'][2]))
            entry['key'] = 'copy'
            entry['content'] = {'mode': 'inline', 'name': 'copy', 'objects': [
                {'key': o['key'], 'origin': o['origin'], 'type': o['type'], 'attributes': o['attributes'], 'events': o['events']}
                for o in json.loads((self.ws / 'pages/third.json').read_text())['objects']]}
            c['pages'].append(entry)
        self.cfg(fn)
        cand = self.compile()
        self.assertEqual(cand.report['added'], ['3.pa'])
        copy, original = self.parse(cand.files, '3.pa'), self.parse(cand.files, '2.pa')
        self.assertEqual(copy.name, 'copy')
        self.assertEqual([o.to_bytes() for o in copy.objects[1:]], [o.to_bytes() for o in original.objects[1:]])

    def test_duplicate_page_names_and_keys(self):
        self.page('third', lambda d: d.update(name='main'))
        with self.assertRaisesRegex(C.ConfigError, 'duplicate page name'):
            self.compile()

    def test_object_moved_from_another_page(self):
        def fn(doc):
            doc['objects'].append({'key': 'moved', 'type': 'text', 'origin': {'file': '0.pa', 'index': 3},
                                   'attributes': {'objname': 'moved'}})
        self.page('third', fn)
        cand = self.compile()
        page = self.parse(cand.files, '2.pa')
        self.assertEqual(page.objects[4].get('objname').value, b'moved')
        self.assertEqual(page.objects[4].get('id').value[0], 4)
        self.assertEqual(self.attr(cand.files, '2.pa', 4, 'font'), 1)


class ValidationTest(Base):
    def assertConfigError(self, pattern):
        with self.assertRaisesRegex(C.ConfigError, pattern):
            self.compile()

    def test_unknown_and_missing_fields(self):
        self.cfg(lambda c: c.update(extra=1))
        self.assertConfigError('unknown field')
        self.cfg(lambda c: (c.pop('extra'), c.pop('pictures')))
        self.assertConfigError('needs "pictures"')

    def test_wrong_format_and_policy(self):
        self.cfg(lambda c: c.update(format='other'))
        self.assertConfigError('Unsupported config format')
        self.cfg(lambda c: c.update(format=C.FORMAT, id_policy='yes'))
        self.assertConfigError('id_policy')

    def test_duplicate_json_keys_are_rejected(self):
        text = (self.ws / 'project.json').read_text()
        (self.ws / 'project.json').write_text(text.replace('"id_policy"', '"id_policy": "remap", "id_policy"', 1))
        self.assertConfigError('duplicate key')

    def test_keys_must_be_unique_and_well_formed(self):
        self.cfg(lambda c: c['pages'][1].update(key='main'))
        self.assertConfigError('Duplicate page key')
        self.cfg(lambda c: c['pages'][1].update(key='bad key'))
        self.assertConfigError('must be a key')
        self.cfg(lambda c: (c['pages'][1].update(key='second'), c['pictures'][1].update(key='pic_10')))
        self.assertConfigError('Duplicate picture key')

    def test_bad_origins(self):
        self.cfg(lambda c: c['pages'][0].update(origin={'file': '99.pa'}))
        self.assertConfigError('not a page')
        self.cfg(lambda c: c['pages'][0].update(origin={'file': '0.pa'}))
        self.cfg(lambda c: c['pictures'][0].update(origin={'file': '0.pa'}))
        self.assertConfigError('not a picture')
        self.cfg(lambda c: c['pictures'][0].update(origin={'file': '10.i'}))
        self.page('main', lambda d: d['objects'][0].update(origin={'file': '0.pa', 'index': 40}))
        self.assertConfigError('does not name a component')

    def test_entry_without_origin_or_source(self):
        self.cfg(lambda c: c['pictures'][0].pop('origin'))
        self.assertConfigError('needs an origin or a source')

    def test_object_errors(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(nothing=1))
        self.assertConfigError('no attribute')
        self.page('main', lambda d: d['objects'][0]['attributes'].pop('nothing'))
        self.page('main', lambda d: d['objects'][0].update(type='text'))
        self.assertConfigError('does not match its origin')
        self.page('main', lambda d: d['objects'][0].update(type='button'))
        self.page('main', lambda d: d['objects'][0]['events'].update(codesnope=[]))
        self.assertConfigError('no event')
        self.page('main', lambda d: d['objects'][0]['events'].pop('codesnope'))
        self.page('main', lambda d: d['objects'][0]['attributes'].update(w=70000))
        self.assertConfigError('does not fit')

    def test_text_longer_than_capacity(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(txt='x' * 30))
        self.assertConfigError('txt_maxl')

    def test_duplicate_component_names(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(objname='t0'))
        self.assertConfigError("duplicate component name 't0'")

    def test_invalid_component_names(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(objname='1bad'))
        self.assertConfigError('invalid component name')

    def test_new_object_errors(self):
        def add(**values):
            def fn(doc):
                doc['objects'].append({'key': 'n', 'type': 'button', 'attributes': values})
            return fn
        cases = [({'objname': 'n'}, "needs 'x'"), ({'x': 1, 'y': 1, 'w': 1, 'h': 1}, 'objname'),
                 ({'objname': 'n', 'x': 1, 'y': 1, 'w': 1, 'h': 1, 'pic': {'$ref': 'font:font_0'}}, 'picture reference'),
                 ({'objname': 'n', 'x': 1, 'y': 1, 'w': 1, 'h': 1, 'pic': {'$ref': 'picture:nope'}}, 'unknown picture key'),
                 ({'objname': 'n', 'x': 1, 'y': 1, 'w': 1, 'h': 1, 'bogus': 3}, 'Unknown attribute')]
        for values, pattern in cases:
            with self.subTest(values=values):
                self.page('main', lambda d: d['objects'].__delitem__(slice(3, None)))
                self.page('main', add(**values))
                self.assertConfigError(pattern)

    def test_unknown_placeholder(self):
        self.page('main', lambda d: d['objects'][0]['events'].update(codesup=['page ${page:nope}']))
        self.assertConfigError('Unknown reference')

    def test_page_name_rules(self):
        self.page('third', lambda d: d.update(name='a' * 17))
        self.assertConfigError('page name')
        self.page('third', lambda d: d.update(name='not valid'))
        self.assertConfigError('page name')

    def test_cannot_remove_all_pages(self):
        self.cfg(lambda c: c['pages'].clear())
        self.assertConfigError('1..255 pages')

    def test_path_escape_and_symlink_are_rejected(self):
        self.cfg(lambda c: c['pages'][0]['content'].update(path='../outside.json'))
        self.assertConfigError('escapes')
        outside = self.dir / 'outside.json'
        outside.write_text('{}')
        (self.ws / 'extra').mkdir()
        os.symlink(outside, self.ws / 'extra/link.json')
        self.cfg(lambda c: c['pages'][0]['content'].update(path='extra/link.json'))
        self.assertConfigError('symlink')

    def test_picture_source_path_safety(self):
        self.cfg(lambda c: c['pictures'].append({'key': 'x', 'source': {'png': '../x.png'}}))
        self.assertConfigError('escapes')

    def test_pending_import_blocks_the_build(self):
        (self.ws / P.IMPORT_PENDING).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Pending import'):
            self.compile()

    def test_inputs_are_not_modified(self):
        self.page('third', lambda d: d.update(name='fourth'))
        self.page('main', lambda d: d['objects'].pop())
        before = {str(p.relative_to(self.ws)): p.read_bytes() for p in self.ws.rglob('*') if p.is_file()}
        try:
            self.compile()
        except C.ConfigError:
            pass
        after = {str(p.relative_to(self.ws)): p.read_bytes() for p in self.ws.rglob('*') if p.is_file()}
        self.assertEqual(before, after)


class PublishTest(Base):
    def test_output_exists(self):
        out = self.dir / 'o.HMI'
        out.write_bytes(b'x')
        with self.assertRaises(FileExistsError):
            P.pack_config(self.ws, out, 'project.json')
        self.assertEqual(out.read_bytes(), b'x')

    def test_failed_write_removes_only_its_own_partial_file(self):
        out = self.dir / 'o.HMI'
        real = Path.open

        def flaky(path, *args, **kw):
            f = real(path, *args, **kw)
            if Path(path) == out:
                f.write = mock.Mock(side_effect=OSError('disk full'))
            return f
        with mock.patch.object(Path, 'open', flaky), self.assertRaises(OSError):
            P.pack_config(self.ws, out, 'project.json')
        self.assertFalse(out.exists())

    def test_cli_round_trip(self):
        cli = [sys.executable, str(ROOT / 'hmi_project.py')]
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        out = self.dir / 'cli.HMI'
        run = lambda *a: subprocess.run(cli + list(a), capture_output=True, text=True, env=env)
        r = run('validate', str(self.ws))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['files'], len(self.files))
        r = run('pack', str(self.ws), str(out))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--config', r.stderr)
        r = run('pack', str(self.ws), str(out), '--legacy')
        self.assertEqual(r.returncode, 0, r.stderr)
        out.unlink()
        r = run('pack', str(self.ws), str(out), '--config', 'project.json')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(H.check(H.read_container(out.read_bytes())), [])
        r = run('export-config', str(self.ws))
        self.assertNotEqual(r.returncode, 0)

    def test_does_not_import_optional_packages_without_new_sources(self):
        code = ("import sys; sys.modules['PIL']=None; sys.modules['fontTools']=None; sys.path.insert(0, %r);"
                "import hmi_project as P; P.pack_config(%r, %r, 'project.json')" % (str(ROOT), str(self.ws), str(self.dir / 'p.HMI')))
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(r.returncode, 0, r.stderr)


class SendkeyPageTest(Base):
    pages = [('main', [X.button('b0')], {}), ('touch', [X.button('b0', sendkey=1)], {}), ('last', [X.button('b0')], {})]
    kwargs = {'program': 'int a=0\n'}

    def test_moving_a_page_with_touch_reports_needs_acknowledgement(self):
        self.remap()
        self.page('main', lambda d: None)
        self.cfg(lambda c: c['pages'].pop(0))
        with self.assertRaisesRegex(C.ConfigError, 'touch'):
            self.compile()
        self.cfg(lambda c: c.update(acknowledge={'external_ids': ['touch']}))
        self.assertEqual(self.compile().report['acknowledged']['external_ids'], ['touch'])


class CopiedPageTest(Base):
    pages = [('main', [X.button('b0'), X.button('b1', events={'codesup': ['b2.txt="z"']}),
                       X.button('b2', events={'codesup': ['b[sys0].txt="x"']})], {})]

    def copy_main(self, drop=None, reverse=False):
        def fn(c):
            entry = json.loads(json.dumps(c['pages'][0]))
            entry['key'] = 'copy'
            doc = json.loads((self.ws / 'pages/main.json').read_text())
            doc['name'] = 'copy'
            if drop:
                doc['objects'] = [o for o in doc['objects'] if o['key'] != drop]
            if reverse:
                doc['objects'].reverse()
            entry['content'] = {'mode': 'inline', **doc}
            c['pages'].append(entry)
            c['id_policy'] = 'remap'
        self.cfg(fn)

    def test_reordered_copy_needs_the_same_acknowledgement_as_the_original(self):
        self.copy_main(reverse=True)
        with self.assertRaisesRegex(C.ConfigError, 'copy/b2/codesup#1'):
            self.compile()

    def test_deleted_component_is_dangling_in_the_copy_too(self):
        self.copy_main(drop='b2')
        with self.assertRaisesRegex(C.ConfigError, 'b2 was deleted'):
            self.compile()


class MalformedConfigTest(Base):
    def test_wrong_json_types_give_clean_errors(self):
        def page_obj(obj):
            return lambda c: c['pages'][0].update(content={'mode': 'inline', 'name': 'main', 'objects': [obj]})
        geometry = {'x': 1, 'y': 1, 'w': 1, 'h': 1}
        cases = {
            'objects': lambda c: c['pages'][0].update(content={'mode': 'inline', 'name': 'main', 'objects': 5}),
            'attributes': page_obj({'key': 'a', 'type': 'button', 'attributes': [1]}),
            'events': page_obj({'key': 'a', 'type': 'button', 'events': 5}),
            'type list': page_obj({'key': 'a', 'type': [1]}),
            'origin file': page_obj({'key': 'a', 'type': 'button', 'origin': {'file': [1], 'index': 1}}),
            'ack list': lambda c: c.update(acknowledge={'dynamic': 5}),
            'ack site': lambda c: c.update(acknowledge={'dynamic': [{'site': [1], 'sha256': 'x'}]}),
            'external ids': lambda c: c.update(acknowledge={'external_ids': 5}),
            'objname value': page_obj({'key': 'a', 'type': 'button', 'attributes': dict(geometry, objname=['x'])}),
        }
        original = (self.ws / 'project.json').read_text()
        for name, edit in cases.items():
            with self.subTest(name):
                (self.ws / 'project.json').write_text(original)
                self.cfg(edit)
                try:
                    self.compile()
                except C.ConfigError:
                    pass
                except Exception as e:  # noqa: BLE001
                    self.fail('%s raised %r instead of ConfigError' % (name, e))
                else:
                    self.fail('%s was accepted' % name)

    def test_non_text_object_name_in_an_origin_object(self):
        self.page('main', lambda d: d['objects'][0]['attributes'].update(objname=['b0']))
        with self.assertRaises(C.ConfigError):
            self.compile()

    def test_exported_hex_names_survive_a_noop(self):
        files = dict(self.files)
        page = M.parse_page(files['0.pa'])
        page.objects[1].get('objname').value = b'b\xff'
        files['0.pa'] = I.sign_page(page.to_bytes())
        shutil.rmtree(self.dir / 'hexname', ignore_errors=True)
        (self.dir / 'hexname').mkdir()
        source = self.dir / 'hexname/s.HMI'
        source.write_bytes(H.write_container(files, order=list(files)))
        ws = self.dir / 'hexname/ws'
        P.unpack(source, ws, pages=False)
        C.export_config(ws)
        self.assertEqual(C.compile_project(ws, 'project.json').files, files)

    def test_root_object_name_is_managed(self):
        self.page('main', lambda d: d['root']['attributes'].update(objname='other'))
        cand = self.compile()
        self.assertEqual(self.parse(cand.files, '0.pa').objects[0].get('objname').value, b'main')


class ExportSafetyTest(Base):
    def test_update_snapshot_refuses_a_symlinked_config(self):
        outside = self.dir / 'outside.json'
        outside.write_text('{}')
        (self.ws / 'project.json').unlink()
        os.symlink(outside, self.ws / 'project.json')
        with self.assertRaises(ValueError):
            C.update_snapshot(self.ws)
        self.assertEqual(outside.read_text(), '{}')

    def test_update_snapshot_never_writes_through_a_prepared_temp_name(self):
        outside = self.dir / 'victim.txt'
        outside.write_text('keep')
        os.symlink(outside, self.ws / 'project.json.tmp')
        C.update_snapshot(self.ws)
        self.assertEqual(outside.read_text(), 'keep')
        self.assertFalse((self.ws / 'project.json').is_symlink())

    def test_failed_export_leaves_nothing_behind(self):
        shutil.rmtree(self.ws / 'pages')
        shutil.rmtree(self.ws / 'pictures')
        (self.ws / 'project.json').unlink()
        real = P.json_bytes
        calls = []

        def flaky(obj):
            calls.append(1)
            if len(calls) == 2:
                raise OSError('disk full')
            return real(obj)
        with mock.patch.object(P, 'json_bytes', flaky), self.assertRaises(OSError):
            C.export_config(self.ws)
        self.assertFalse((self.ws / 'pages').exists())
        self.assertFalse((self.ws / 'project.json').exists())
        C.export_config(self.ws)


class SchemaTest(Base):
    def test_exported_files_use_only_documented_fields(self):
        schema = json.loads((ROOT / 'hmi_project.schema.json').read_text())
        top = set(schema['properties'])
        defs = schema['$defs']
        cfg = json.loads((self.ws / 'project.json').read_text())
        self.assertLessEqual(set(cfg), top)
        self.assertEqual(cfg['format'], schema['properties']['format']['const'])
        for entry in cfg['pages']:
            self.assertLessEqual(set(entry), set(defs['page']['properties']))
        for entry in cfg['pictures']:
            self.assertLessEqual(set(entry), set(defs['picture']['properties']))
        for entry in cfg['fonts']:
            self.assertLessEqual(set(entry), set(defs['font']['properties']))
        page = json.loads((self.ws / 'pages/main.json').read_text())
        self.assertLessEqual(set(page), set(defs['page_definition']['properties']))
        self.assertLessEqual(set(page['root']), set(defs['root']['properties']))
        for o in page['objects']:
            self.assertLessEqual(set(o), set(defs['object']['properties']))

    def test_schema_matches_the_compiler_limits(self):
        schema = json.loads((ROOT / 'hmi_project.schema.json').read_text())
        self.assertEqual(schema['properties']['pages']['maxItems'], 255)
        self.assertEqual(schema['$defs']['page']['properties']['content']['oneOf'][2]['properties']['objects']['maxItems'], M.MAX_OBJECTS - 1)
        self.assertEqual(sorted(schema['properties']['id_policy']['enum']), ['preserve', 'remap'])


@unittest.skipUnless(HMI_FILE, 'set HMI_FILE for real-project structural tests')
class RealProjectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / '.state').mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='config-real-')
        cls.ws = Path(cls.temp.name) / 'ws'
        P.unpack(HMI_FILE, cls.ws, pages=False)
        C.export_config(cls.ws)
        cls.base = C.Baseline(cls.ws)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.copy = Path(self.temp.name) / ('c-' + self.id().split('.')[-1])
        shutil.copytree(self.ws, self.copy)

    def test_noop_is_identical(self):
        cand = C.compile_project(self.copy, 'project.json')
        self.assertEqual(list(cand.files), self.base.order)
        self.assertEqual(cand.files, self.base.files)

    def test_delete_middle_picture_keeps_every_reference(self):
        X.edit_config(self.copy, lambda c: (c.update(id_policy='remap'), c['pictures'].pop(100)))
        cand = C.compile_project(self.copy, 'project.json')
        self.assertEqual(len(cand.report['removed']), 2)
        gone = self.base.pic_files[100]
        before, after = X.resolved_pictures(self.base.files), X.resolved_pictures(cand.files)
        self.assertEqual(before, after)
        self.assertNotIn(gone, cand.files)
        self.assertEqual(H.check(cand.files), [])

    def test_fresh_page_builds_and_checks(self):
        def add(c):
            c['pages'].append({'key': 'settings', 'content': {'mode': 'inline', 'name': 'settings', 'objects': [
                {'key': 'b', 'type': 'button', 'attributes': {'objname': 'b', 'x': 1, 'y': 2, 'w': 100, 'h': 30,
                                                             'font': {'$ref': 'font:font_1'}, 'txt': 'OK'},
                 'events': {'codesup': ['page ${page:main}']}}]}})
        X.edit_config(self.copy, add)
        out = Path(self.temp.name) / 'fresh.HMI'
        P.pack_config(self.copy, out, 'project.json')
        files = H.read_container(out.read_bytes())
        self.assertEqual(H.check(files), [])
        self.assertEqual(sum(1 for n in files if n.endswith('.pa')), 95)
        self.assertEqual(H.parse_project(files)['pages'][-1]['name'], 'settings')

    def test_every_picture_is_exported_as_png_and_edits_rebuild_one(self):
        pngs = sorted((self.copy / 'pictures').glob('*.png'))
        self.assertEqual(len(pngs), 270)
        cand = C.compile_project(self.copy, 'project.json')
        self.assertEqual(cand.files, self.base.files)
        from PIL import Image
        target = self.copy / 'pictures/pic_10.png'
        im = Image.open(target).convert('RGBA')
        im.putpixel((0, 0), (1, 2, 3, 255))
        im.save(target)
        cand = C.compile_project(self.copy, 'project.json')
        self.assertEqual(sorted(cand.report['modified']), ['10.i', '10.is'])

    def test_deleting_a_page_needs_the_documented_acknowledgements(self):
        X.edit_config(self.copy, lambda c: (c.update(id_policy='remap'), c['pages'].pop(76)))
        with self.assertRaises(C.ConfigError) as cm:
            C.compile_project(self.copy, 'project.json')
        self.assertIn('acknowledge', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
