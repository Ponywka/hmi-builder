"""Lossless page/index model: unchanged data round-trips byte for byte, edits touch only what they should."""
import os
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hmi_model as M
import hmi_parse as H
from test_hmi_project import example_page

HMI_FILE = os.environ.get('HMI_FILE')


def lp(b):
    return struct.pack('<I', len(b)) + b


def block(attrs, events=(), tail=b'\0\0\0\0', count=None):
    out = lp(('att-%d' % (len(attrs) if count is None else count)).encode())
    for name, value in attrs:
        out += struct.pack('<I', 16 + len(value)) + name.encode().ljust(16, b'\0') + value
    for label, lines in events:
        out += lp(('%s-%d' % (label, len(lines))).encode()) + b''.join(lp(x) for x in lines)
    return out + tail


def page_of(blocks, extras=None, name=b'p'):
    head = bytearray(56)
    head[24:40] = name.ljust(16, b'\0')
    head[44:56] = bytes(range(12))
    table, body, off = b'', b'', 12 * len(blocks)
    for i, b in enumerate(blocks):
        table += struct.pack('<III', off, len(b), (extras or {}).get(i, 0))
        body += b
        off += len(b)
    struct.pack_into('<4I', head, 0, 1, 56 + off, 56, len(blocks))
    return bytes(head) + table + body


class PageModelTest(unittest.TestCase):
    def test_example_round_trip(self):
        data = example_page()
        self.assertEqual(M.parse_page(data).to_bytes(), data)

    def test_opaque_data_survives(self):
        weird = block([('type', b'y'), ('id', b'\0'), ('objname', b'\xff\xfe'), ('dup', b'1'), ('dup', b'2')],
                      [('codesup', [b'\xff bad utf8', b''])], tail=b'\1\2\3\4\5')
        data = page_of([block([('type', b'y'), ('id', b'\0'), ('objname', b'p')]), weird], extras={0: 7, 1: 99})
        page = M.parse_page(data)
        self.assertEqual(page.to_bytes(), data)
        self.assertEqual([a.name for a in page.objects[1].attrs], ['type', 'id', 'objname', 'dup', 'dup'])
        self.assertEqual(page.objects[1].extra, 99)
        self.assertEqual(page.objects[1].tail, b'\1\2\3\4\5')
        self.assertEqual(page.objects[1].events[0].lines, [b'\xff bad utf8', b''])

    def test_editing_regenerates_only_counts(self):
        data = page_of([block([('type', b'y'), ('id', b'\0'), ('objname', b'p')],
                              [('codesup', [b'a'])])])
        page = M.parse_page(data)
        page.objects[0].attrs.append(M.Attr('extra', b'\1'))
        page.objects[0].events[0].lines.append(b'b')
        out = M.parse_page(page.to_bytes())
        self.assertEqual([a.name for a in out.objects[0].attrs][-1], 'extra')
        self.assertEqual(out.objects[0].events[0].lines, [b'a', b'b'])
        self.assertIn(b'codesup-2', page.to_bytes())
        self.assertIn(b'att-4', page.to_bytes())

    def test_page_name_and_limits(self):
        page = M.parse_page(example_page())
        page.name = 'новая'
        self.assertEqual(M.parse_page(page.to_bytes()).name, 'новая')
        for bad in ('', 'x' * 17, 'a\0b'):
            with self.assertRaises(M.ModelError):
                page.name = bad
        page.objects = []
        with self.assertRaises(M.ModelError):
            page.to_bytes()

    def test_malformed_pages_are_rejected(self):
        good = example_page()
        cases = [good[:40], good[:60], good + b'', bytearray(good)]
        bad = bytearray(good)
        struct.pack_into('<I', bad, 8, 60)                 # table address
        cases.append(bytes(bad))
        bad = bytearray(good)
        struct.pack_into('<I', bad, 12, 0)                 # no objects
        cases.append(bytes(bad))
        bad = bytearray(good)
        struct.pack_into('<I', bad, 56, 13)                # object not contiguous
        cases.append(bytes(bad))
        bad = bytearray(good)
        struct.pack_into('<I', bad, 4, len(good) + 5)      # size beyond data
        cases.append(bytes(bad))
        for i, data in enumerate(cases[:1] + cases[1:2] + cases[4:]):
            with self.subTest(case=i), self.assertRaises(M.ModelError):
                M.parse_page(data)

    def test_trailing_unaccounted_bytes_are_rejected(self):
        data = bytearray(example_page())
        struct.pack_into('<I', data, 4, len(data) + 0)
        data += b'junk'
        struct.pack_into('<I', data, 4, len(data))
        with self.assertRaises(M.ModelError):
            M.parse_page(bytes(data))

    def test_values(self):
        self.assertEqual(M.decode_value('x', (-5).to_bytes(2, 'little', signed=True)), -5)
        self.assertEqual(M.decode_value('val', (-5).to_bytes(4, 'little', signed=True)), -5)
        self.assertEqual(M.decode_value('objname', b'ab'), 'ab')
        self.assertEqual(M.decode_value('objname', b'\xff'), {'hex': 'ff'})
        self.assertEqual(M.decode_value('buff', bytes(3)), {'hex': '000000'})
        self.assertEqual(M.encode_value('x', -5, 2), (-5).to_bytes(2, 'little', signed=True))
        self.assertEqual(M.encode_value('txt', 'é'), 'é'.encode())
        self.assertEqual(M.encode_value('buff', {'hex': '0a0b'}), b'\n\x0b')
        for name, value, width in (('w', 65536, 2), ('w', -1, 2), ('w', True, 1), ('w', 'x', 2), ('txt', 3, None),
                                   ('w', 1.5, 2), ('buff', {'hex': 'zz'}, None), ('w', 1, 3)):
            with self.subTest(name=name, value=value), self.assertRaises(M.ModelError):
                M.encode_value(name, value, width)


def index_bytes(records, tail=b'', addr=96):
    head = bytearray(96)
    struct.pack_into('<I', head, 4, 96)
    struct.pack_into('<II', head, 24, addr, len(records))
    return bytes(head) + b''.join(M.Index.make_record(k, n) for k, n in records) + tail


class IndexModelTest(unittest.TestCase):
    def test_round_trip_and_edit(self):
        data = index_bytes([('i', '10.i'), ('zi', '0.zi'), ('pa', '3.pa')], tail=b'tail')
        idx = M.parse_index(data)
        self.assertEqual(idx.to_bytes(), data)
        self.assertEqual(idx.pairs(), [('i', '10.i'), ('zi', '0.zi'), ('pa', '3.pa')])
        idx.records.append(M.Index.make_record('pa', '4.pa'))
        out = M.parse_index(idx.to_bytes())
        self.assertEqual(len(out.records), 4)
        self.assertEqual(out.tail, b'tail')
        self.assertEqual(struct.unpack_from('<I', idx.to_bytes(), 4)[0], 96)      # datasize is not the file length

    def test_record_validation(self):
        for kind, name in (('', 'a'), ('i', ''), ('i', 'too_long_name'), ('i', 'a/b'), ('i', 'ä'), ('toolongkind', 'a')):
            with self.subTest(kind=kind, name=name), self.assertRaises(M.ModelError):
                M.Index.make_record(kind, name)

    def test_malformed_indexes(self):
        good = index_bytes([('i', '1.i'), ('pa', '2.pa')])
        for data in (good[:50], good[:100], index_bytes([('i', '1.i')], addr=100),
                     good[:96] + b'\0' * 32, good[:96] + b'1\0\0\0\0\0\0\0' + b'x' * 8 + good[112:]):
            with self.subTest(len=len(data)), self.assertRaises(M.ModelError):
                M.parse_index(data)


@unittest.skipUnless(HMI_FILE, 'set HMI_FILE for real-project round trips')
class RealProjectModelTest(unittest.TestCase):
    def test_all_pages_and_index_round_trip(self):
        files = H.read_container(Path(HMI_FILE).read_bytes())
        pages = 0
        for name, data in files.items():
            if name.endswith('.pa'):
                size = struct.unpack_from('<I', data, 4)[0]
                self.assertEqual(M.parse_page(data).to_bytes(), data[:size], name)
                pages += 1
        self.assertEqual(M.parse_index(files['main.HMI']).to_bytes(), files['main.HMI'])
        self.assertGreater(pages, 1)


if __name__ == '__main__':
    unittest.main()
