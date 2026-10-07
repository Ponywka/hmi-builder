"""Python-only integrity tests; native oracle outputs are recorded as small golden vectors."""
import hashlib
import json
import os
from pathlib import Path
import random
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hmi_integrity as I
import hmi_parse as H

FIXTURES = ROOT / 'tests/fixtures'


def bit_crc(data, initial=0xffffffff):
    c = initial
    for b in data:
        c ^= b
        for _ in range(32):
            c = ((c << 1) ^ (0x04c11db7 if c & 0x80000000 else 0)) & 0xffffffff
    return c


class ChecksumTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = json.loads((FIXTURES / 'hmi_sign_vectors.json').read_text())

    def test_crc_matches_bit_reference(self):
        for initial in (0, 0xffffffff, 0x12345678):
            for size in (0, 1, 2, 3, 4, 7, 8, 9, 31, 32, 33, 256):
                b = bytes(range(256))[:size]
                with self.subTest(initial=initial, size=size):
                    self.assertEqual(I.crc_bytes(b, initial), bit_crc(b, initial))

    def test_crc_incremental_updates(self):
        b = bytes(range(256))
        for split in (0, 1, 3, 127, 255, 256):
            self.assertEqual(I.crc_bytes(b[split:], I.crc_bytes(b[:split])), I.crc_bytes(b))

    def test_native_signing_vectors(self):
        for v in self.vectors['vectors']:
            with self.subTest(kind=v['kind'], case=v['case']):
                sign = I.sign_page if v['kind'] == 'page' else I.sign_index
                verify = I.verify_page if v['kind'] == 'page' else I.verify_index
                raw, expected = bytes.fromhex(v['input']), bytes.fromhex(v['signed'])
                self.assertEqual(sign(raw), expected)
                self.assertTrue(verify(expected))
                self.assertEqual(sign(expected), expected)

    def test_native_word_and_megabyte_boundaries(self):
        rng = random.Random(self.vectors['random_seed'])
        for v in self.vectors['boundary_vectors']:
            b = bytearray(rng.randbytes(v['length']))
            if v['kind'] == 'page':
                struct.pack_into('<III', b, 4, len(b), 56, 0)
                sign = I.sign_page
            else:
                struct.pack_into('<I', b, 4, 96)
                sign = I.sign_index
            with self.subTest(kind=v['kind'], length=len(b)):
                self.assertEqual(hashlib.sha256(b).hexdigest(), v['input_sha256'])
                self.assertEqual(hashlib.sha256(sign(b)).hexdigest(), v['signed_sha256'])

    def test_covered_byte_corruption_is_detected(self):
        for kind in ('page', 'index'):
            v = next(v for v in self.vectors['vectors'] if v['kind'] == kind and len(bytes.fromhex(v['signed'])) == 257)
            raw = bytes.fromhex(v['signed'])
            verify = I.verify_page if kind == 'page' else I.verify_index
            for pos in (0, 4, 10, 12, 14, 16, 20, 21, 22, 36, 40, 55, 96, len(raw) - 1):
                b = bytearray(raw)
                b[pos] ^= 1
                with self.subTest(kind=kind, pos=pos):
                    self.assertFalse(verify(b))

    def test_page_length_is_checked_and_normalized(self):
        v = next(v for v in self.vectors['vectors'] if v['kind'] == 'page')
        raw = bytes.fromhex(v['signed']) + b'old trailing data'
        self.assertFalse(I.verify_page(raw))
        signed = I.sign_page(raw)
        self.assertTrue(I.verify_page(signed))
        self.assertEqual(struct.unpack_from('<I', signed, 4)[0], len(signed))
        self.assertEqual(signed[-17:], b'old trailing data')

    def test_short_files_are_rejected(self):
        for size in (0, 1, 55):
            self.assertFalse(I.verify_page(bytes(size)))
            with self.assertRaises(ValueError):
                I.sign_page(bytes(size))
        for size in (0, 1, 95):
            self.assertFalse(I.verify_index(bytes(size)))
            with self.assertRaises(ValueError):
                I.sign_index(bytes(size))

    def test_version_normalization_and_validation(self):
        raw = bytes(56)
        result = I.sign_page(raw, version=(1, 65, 5))
        self.assertEqual(result[40:43], bytes((1, 65, 5)))
        self.assertTrue(I.verify_page(result))
        for version in ((1, 2), (1, 2, 256), (1, -1, 0), (1, 2, '3')):
            with self.assertRaises(ValueError):
                I.sign_page(raw, version=version)


class ContainerTest(unittest.TestCase):
    def test_word_crc_against_bit_reference(self):
        b = bytes(range(64))
        for initial in (0, 0xffffffff, 0x12345678):
            for size in (0, 4, 8, 12, 32, 64):
                c = initial
                for (word,) in struct.iter_unpack('<I', b[:size]):
                    c ^= word
                    for _ in range(32):
                        c = ((c << 1) ^ (0x04c11db7 if c & 0x80000000 else 0)) & 0xffffffff
                self.assertEqual(I.crc_words(b[:size], initial), c)
        with self.assertRaises(ValueError):
            I.crc_words(b'123')

    def test_native_container_vectors(self):
        fixtures = json.loads((FIXTURES / 'hmi_container_vectors.json').read_text())
        for v in fixtures['cases']:
            with self.subTest(case=v['name']):
                files = {f['name']: bytes.fromhex(f['hex']) for f in v['files']}
                data = I.build_container(files, extra=v['extra'])
                end = 4 + 28 * len(files)
                self.assertEqual(struct.unpack_from('<I', data, end)[0], int(v['table_crc'], 16))
                self.assertEqual(len(data), v['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), v['sha256'])
                self.assertEqual(I.container_errors(data, require_exact_body=True), [])
                self.assertEqual(H.read_container(data), files)

    def test_order_and_determinism(self):
        files = {'a': b'abc', 'b': b'def'}
        data = I.build_container(files, order=['b', 'a'])
        self.assertEqual(list(H.read_container(data)), ['b', 'a'])
        self.assertEqual(I.build_container(files), I.build_container(files))
        for order in ([], ['a'], ['a', 'a'], ['a', 'missing']):
            with self.assertRaises(ValueError):
                I.build_container(files, order=order)

    def test_names_payloads_and_metadata_are_validated(self):
        for name in ('', '.', '..', '../escape', 'a/b', 'a\\b', 'x' * 16, '\0', '\x1f', '中文'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                I.build_container({name: b''})
        for extra in (-1, 0x100000000, '0'):
            with self.assertRaises(ValueError):
                I.build_container({'a': b''}, extra=extra)
        with self.assertRaises(ValueError):
            I.build_container({'a': 100})

    def test_supported_count_boundary(self):
        files = {'f%05d' % i: b'' for i in range(I.MAX_RECORDS)}
        self.assertTrue(I.verify_container(I.build_container(files), require_exact_body=True))
        files['extra'] = b''
        with self.assertRaises(ValueError):
            I.build_container(files)

    def test_corrupted_metadata_and_truncation(self):
        raw = I.build_container({'a': b'abc', 'b': b'def'})
        end = 4 + 28 * 2
        for pos in (0, 4, end, I.BACKUP_BASE, I.BACKUP_BASE + end, I.ALT_TABLE_MARKER, I.DATA_BASE - 1):
            b = bytearray(raw)
            b[pos] ^= 1
            with self.subTest(pos=pos):
                self.assertFalse(I.verify_container(b))
        for size in (0, 3, 4, I.DATA_BASE - 1, len(raw) - 1):
            self.assertFalse(I.verify_container(raw[:size]))

    @staticmethod
    def repair_table(data):
        count = struct.unpack_from('<I', data)[0]
        end = 4 + 28 * count
        struct.pack_into('<I', data, end, I.table_checksum(data[:end]))
        data[I.BACKUP_BASE:I.BACKUP_BASE + end + 4] = data[:end + 4]
        return data

    def test_bounds_duplicates_and_overlap_after_valid_checksum(self):
        raw = I.build_container({'a': b'abc', 'b': b'def'})
        b = bytearray(raw)
        struct.pack_into('<I', b, 4 + 28 + 16, I.DATA_BASE + 1)
        self.assertFalse(I.verify_container(self.repair_table(b)))
        b = bytearray(raw)
        b[4 + 28:4 + 28 + 16] = b[4:4 + 16]
        self.assertFalse(I.verify_container(self.repair_table(b)))
        b = bytearray(raw)
        struct.pack_into('<I', b, 4 + 16, I.DATA_BASE - 1)
        self.assertFalse(I.verify_container(self.repair_table(b)))
        b = bytearray(raw)
        struct.pack_into('<I', b, 4 + 20, len(raw))
        self.assertFalse(I.verify_container(self.repair_table(b)))

    def test_repeated_unused_extents_are_not_duplicate_live_files(self):
        raw = bytearray(I.build_container({'a': b'ab', 'u': b'cd', 'v': b'ef'}))
        for i in (1, 2):
            raw[4 + 28 * i:4 + 28 * i + 16] = bytes(16)
        self.repair_table(raw)
        self.assertTrue(I.verify_container(raw, require_exact_body=True))
        files, unused = H.read_container(raw, with_stale=True)
        self.assertEqual(files, {'a': b'ab'})
        self.assertEqual(unused, {'a': [b'cd', b'ef']})
        raw[4:20] = bytes(16)
        raw[4 + 28:4 + 28 + 16] = b'u'.ljust(16, b'\0')
        self.repair_table(raw)
        self.assertTrue(I.verify_container(raw))
        files, unused = H.read_container(raw, with_stale=True)
        self.assertEqual(files, {'u': b'cd'})
        self.assertEqual(unused, {None: [b'ab'], 'u': [b'ef']})

    def test_outer_checksum_does_not_cover_resource_payload(self):
        raw = bytearray(I.build_container({'a': b'abc'}))
        raw[-1] ^= 1
        self.assertTrue(I.verify_container(raw))
        self.assertTrue(I.verify_container(raw + b'trailing'))
        self.assertFalse(I.verify_container(raw + b'trailing', require_exact_body=True))


@unittest.skipUnless(os.environ.get('HMI_FILE'), 'set HMI_FILE')
class RealProjectIntegrityTest(unittest.TestCase):
    def test_original_container_checksums(self):
        self.assertEqual(I.container_errors(Path(os.environ['HMI_FILE']).read_bytes()), [])

    def test_original_page_and_index_checksums(self):
        files = H.read_container(Path(os.environ['HMI_FILE']).read_bytes())
        checked = 0
        for name, data in files.items():
            if not name.endswith('.pa') and name != 'main.HMI':
                continue
            verify = I.verify_page if name.endswith('.pa') else I.verify_index
            sign = I.sign_page if name.endswith('.pa') else I.sign_index
            with self.subTest(file=name):
                self.assertTrue(verify(data))
                self.assertEqual(sign(data), data)
            checked += 1
        self.assertGreater(checked, 1)


if __name__ == '__main__':
    unittest.main()
