"""Creation profiles: fresh objects must match the layout the editor itself saved."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hmi_factory as F
import hmi_model as M
import hmi_parse as H

HMI_FILE = os.environ.get('HMI_FILE')
VISUAL = dict(objname='x1', x=1, y=2, w=3, h=4)


def layout(obj):
    return [(a.name, len(a.value) if a.name not in ('objname', 'txt') else 'text') for a in obj.attrs]


class FactoryTest(unittest.TestCase):
    def test_defaults_and_derived_fields(self):
        o = F.make_object(98, dict(VISUAL, txt='OK'), {'codesup': ['page 0']}, object_id=5)
        v = {a.name: M.decode_value(a.name, a.value) for a in o.attrs}
        self.assertEqual((v['type'], v['id'], v['endx'], v['endy']), (98, 5, 3, 5))
        self.assertEqual((v['aph'], v['time'], v['val'], v['txt'], v['txt_maxl']), (127, 300, 0, 'OK', 10))
        self.assertEqual([e.label for e in o.events], ['codesdown', 'codesup'])
        self.assertEqual(o.events[1].lines, [b'page 0'])
        self.assertEqual((o.extra, o.tail), (0, b'\0\0\0\0'))

    def test_page_takes_the_given_size_and_all_events(self):
        o = F.make_object(121, {'objname': 'p'}, page_size=(272, 480))
        v = {a.name: M.decode_value(a.name, a.value) for a in o.attrs}
        self.assertEqual((v['w'], v['h'], v['endx'], v['endy'], v['up'], v['pic']), (272, 480, 271, 479, 255, 65535))
        self.assertEqual([e.label for e in o.events], ['codesload', 'codesloadend', 'codesdown', 'codesup', 'codesunload'])

    def test_reduced_head_for_timer_and_variable(self):
        t = F.make_object(51, {'objname': 'tm0', 'tim': 1000})
        self.assertEqual([a.name for a in t.attrs], ['type', 'id', 'objname', 'vscope', 'lockobj', 'groupid0', 'groupid1', 'tim', 'en'])
        self.assertEqual([e.label for e in t.events], ['codestimer'])
        v = F.make_object('variable', {'objname': 'v0', 'val': -5})
        self.assertEqual(M.decode_value('val', v.get('val').value), -5)
        self.assertEqual(v.events, [])

    def test_type_names(self):
        for name, code in (('page', 121), ('button', 98), ('text', 116), ('picture', 112), ('timer', 51), ('variable', 52)):
            self.assertEqual(F.type_code(name), code)
            self.assertEqual(F.type_code(code), code)
        for bad in ('slider', 113, 0, True, None, 1.5):
            with self.subTest(bad=bad), self.assertRaises(M.ModelError):
                F.type_code(bad)

    def test_rejects_invalid_input(self):
        cases = [
            (98, dict(VISUAL, nope=1)), (98, {'objname': 'b'}), (98, dict(VISUAL, w=0)), (98, dict(VISUAL, x=40000)),
            (98, dict(VISUAL, style=300)), (98, dict(VISUAL, txt=5)), (98, dict(VISUAL, font=True)),
            (98, dict(VISUAL, txt='x' * 11)), (98, dict(VISUAL, type=116)), (51, {'tim': 10, 'objname': 't'}),
            (98, dict(VISUAL, w='3')), (98, dict(x=1, y=1, w=1, h=1)),
        ]
        for code, values in cases:
            with self.subTest(values=values), self.assertRaises(M.ModelError):
                F.make_object(code, values)
        with self.assertRaises(M.ModelError):
            F.make_object(98, VISUAL, {'codesload': []})

    def test_page_round_trips_through_the_independent_parser(self):
        page = M.Page(bytes(56), [F.make_object(121, {'objname': 'n', 'w': 272, 'h': 480}),
                                  F.make_object(116, dict(VISUAL, txt='Привет', txt_maxl=20), object_id=1)])
        page.name = 'n'
        parsed = H.parse_page(page.to_bytes())
        self.assertEqual([o['type_name'] for o in parsed['objects']], ['page', 'text'])
        self.assertEqual(parsed['objects'][1]['txt'], 'Привет')


@unittest.skipUnless(HMI_FILE, 'set HMI_FILE to compare with objects saved by the editor')
class SavedLayoutTest(unittest.TestCase):
    def test_fresh_objects_match_saved_objects(self):
        files = H.read_container(Path(HMI_FILE).read_bytes())
        saved = {}
        for name, data in files.items():
            if name.endswith('.pa'):
                for o in M.parse_page(data).objects:
                    saved.setdefault(o.get('type').value[0], o)
        checked = 0
        for code in F.CREATABLE:
            if code not in saved:
                continue
            values = {'objname': 'n'}
            if code in (121, 98, 116, 112):
                values.update(x=1, y=2, w=3, h=4)
            fresh = F.make_object(code, values)
            self.assertEqual(layout(fresh), layout(saved[code]), 'type %d' % code)
            self.assertEqual([e.label for e in fresh.events], [e.label for e in saved[code].events])
            self.assertEqual((fresh.extra, fresh.tail), (saved[code].extra, saved[code].tail))
            checked += 1
        self.assertGreaterEqual(checked, 5)


if __name__ == '__main__':
    unittest.main()
