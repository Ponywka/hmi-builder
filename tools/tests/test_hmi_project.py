"""Workspace serialization tests; optional real-file tests use HMI_FILE. No editor is launched by this suite."""
import copy
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hmi_parse as H
import hmi_project as P
import hmi_integrity as I


def example_page():
    attrs = {'type': b'y', 'id': b'\0', 'objname': b'demo', 'x': (-179).to_bytes(2, 'little', signed=True),
             'txt': 'Пример'.encode('utf8'), 'txt_maxl': (150).to_bytes(2, 'little'),
             'path_m': (300).to_bytes(4, 'little'), 'buff': bytes(range(10))}
    block = P.lp(('att-%d' % len(attrs)).encode())
    for name, value in attrs.items():
        block += struct.pack('<I', 16 + len(value)) + name.encode().ljust(16, b'\0') + value
    block += P.lp(b'codesload-1') + P.lp(b'printh 91') + P.lp(b'codesup-0') + bytes(4)
    head = bytearray(56)
    struct.pack_into('<4I', head, 0, 123, 56 + 12 + len(block), 56, 1)
    head[24:40] = b'demo'.ljust(16, b'\0')
    head[44:56] = bytes(range(12))
    return bytes(head) + struct.pack('<III', 12, len(block), 42) + block


def example_picture(variant=0):
    """A tiny valid 2x2 picture accepted by hmi_parse.check()."""
    payload = (struct.pack('<5I', 2, 2, 2, 0, 1) + struct.pack('<HHB', 0, 1, 1) +
               struct.pack('<H', 0xffff) + bytes((8 + variant,)))
    return struct.pack('<4BH2BIHHIi', 13, 96, 1, 4, 0, 0, 0, 24, 2, 2, len(payload), 0) + payload


def indexed_workspace(directory, files, records, prefix='indexed-source'):
    head = bytearray(96)
    struct.pack_into('<I', head, 4, 96)
    struct.pack_into('<II', head, 24, 96, len(records))
    index = bytes(head)
    for kind, name in records:
        index += kind.encode().ljust(8, b'\0') + name.encode().ljust(8, b'\0')
    files = dict(files)
    files['main.HMI'] = I.sign_index(index)
    source = directory / (prefix + '.HMI')
    source.write_bytes(H.write_container(files, order=list(files)))
    workspace = directory / (prefix + '-workspace')
    P.unpack(source, workspace)
    return workspace, files


def picture_workspace(directory):
    files = {
        'main.HMI': b'',
        '10.i': example_picture(), '10.is': b'png-original-10',
        '2.i': example_picture(), '2.is': b'png-original-2',
    }
    return indexed_workspace(directory, files, [('i', '10.i'), ('i', '2.i')], 'picture-source')


def png_2x2():
    import zlib
    rows = b'\0' + bytes((255, 0, 0, 255, 0, 255, 0, 255))
    rows += b'\0' + bytes((0, 0, 255, 255, 255, 255, 255, 255))
    def chunk(kind, body):
        return (struct.pack('>I', len(body)) + kind + body +
                struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff))
    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 6, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='workspace-test-')
        self.directory = Path(self.temp.name)
        self.page = example_page()

    def tearDown(self):
        self.temp.cleanup()

    def test_unchanged_page_is_identical(self):
        self.assertEqual(P.encode_page(self.page, P.editable_page(self.page)), self.page)

    def test_text_and_event_length_changes(self):
        edit = P.editable_page(self.page)
        edit['objects'][0]['attributes']['txt'] = 'Saving Wi-Fi — longer UTF-8 text'
        edit['objects'][0]['events']['codesload'].insert(0, '// 中文 comment')
        out = P.encode_page(self.page, edit)
        self.assertEqual(P.editable_page(out), edit)
        self.assertEqual(out[44:56], self.page[44:56])
        self.assertEqual(struct.unpack_from('<III', out, 56)[2], 42)
        self.assertEqual(H.page_header(out)['datasize'], len(out))
        self.assertEqual(out[-4:], bytes(4))

    def test_numeric_width_and_binary_values(self):
        edit = P.editable_page(self.page)
        attrs = edit['objects'][0]['attributes']
        self.assertEqual(attrs['path_m'], 300)
        attrs['x'] = -12
        attrs['path_m'] = 301
        attrs['buff'] = {'hex': 'abcdef'}
        out = P.encode_page(self.page, edit)
        self.assertEqual(P.editable_page(out), edit)

    def test_numeric_overflow_is_rejected(self):
        edit = P.editable_page(self.page)
        edit['objects'][0]['attributes']['id'] = 256
        with self.assertRaises(OverflowError):
            P.encode_page(self.page, edit)

    def test_changed_object_or_event_keys_rejected(self):
        edit = P.editable_page(self.page)
        edit['objects'][0]['events']['new_event'] = []
        with self.assertRaises(ValueError):
            P.encode_page(self.page, edit)
        edit = P.editable_page(self.page)
        edit['objects'].append(copy.deepcopy(edit['objects'][0]))
        with self.assertRaises(ValueError):
            P.encode_page(self.page, edit)

    def test_name_mismatch_rejected(self):
        edit = P.editable_page(self.page)
        edit['name'] = 'renamed'
        with self.assertRaises(ValueError):
            P.encode_page(self.page, edit)

    def test_unpack_load_roundtrip(self):
        # Outer metadata is valid; the synthetic page is deliberately unsigned for parser tests.
        files = {'0.pa': self.page, 'Program.s': b'page 0\r\n'}
        source = self.directory / 'input.HMI'
        source.write_bytes(H.write_container(files))
        ws = self.directory / 'workspace'
        P.unpack(source, ws)
        self.assertEqual(P.load_workspace(ws), (files, []))
        with self.assertRaises(FileExistsError):
            P.unpack(source, ws)

    def test_raw_and_json_edit_conflict(self):
        source = self.directory / 'input.HMI'
        source.write_bytes(H.write_container({'0.pa': self.page}))
        ws = self.directory / 'workspace'
        P.unpack(source, ws)
        path = ws / 'pages/0.json'
        edit = json.loads(path.read_text())
        edit['objects'][0]['attributes']['txt'] = 'Changed in JSON'
        path.write_text(json.dumps(edit))
        raw = ws / 'files/0.pa'
        raw.write_bytes(raw.read_bytes() + b'raw change')
        with self.assertRaisesRegex(ValueError, 'both as raw bytes and page JSON'):
            P.load_workspace(ws)

    def test_load_and_pack_reject_symlinked_manifest_raw_and_page_paths(self):
        source = self.directory / 'symlink-input.HMI'
        source.write_bytes(H.write_container({'0.pa': self.page, 'Program.s': b'page demo\\r\\n'}))
        ws = self.directory / 'symlink-workspace'
        P.unpack(source, ws)
        outside = self.directory / 'outside'
        outside.write_bytes(b'outside')
        raw = ws / 'files/Program.s'
        raw.unlink()
        raw.symlink_to(outside)
        with self.assertRaises(ValueError):
            P.load_workspace(ws)
        with self.assertRaises(ValueError):
            P.pack(ws, self.directory / 'raw-symlink.HMI')

        raw.unlink()
        raw.write_bytes(b'page demo\\r\\n')
        page = ws / 'pages/0.json'
        page.unlink()
        page.symlink_to(outside)
        with self.assertRaises(ValueError):
            P.load_workspace(ws)
        page.unlink()
        page.write_bytes((ws / 'manifest.json').read_bytes())
        manifest = ws / 'manifest.json'
        manifest.unlink()
        manifest.symlink_to(outside)
        with self.assertRaises(ValueError):
            P.load_workspace(ws)
        with self.assertRaises(ValueError):
            P.pack(ws, self.directory / 'manifest-symlink.HMI')

    def test_internal_names_cannot_escape_workspace(self):
        for name in ('../outside', '/outside', '..', 'a\\b', 'a/b'):
            with self.assertRaises(ValueError):
                P.safe_name(name)


class ResourceImportTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='resource-import-test-')
        self.directory = Path(self.temp.name)
        self.workspace, self.original = picture_workspace(self.directory)
        self.source = self.directory / 'input.png'
        self.source.write_bytes(b'fake png input')

    def tearDown(self):
        self.temp.cleanup()

    def test_picture_uses_filtered_index_order_and_preserves_manifest(self):
        original_manifest = (self.workspace / 'manifest.json').read_bytes()
        image = types.SimpleNamespace(
            encode_png=mock.Mock(return_value=(example_picture(1), b'png-replacement')),
            validate_picture=lambda data: H.picture_header(data),
            validate_source=lambda data: {'format': 'png'},
        )
        with mock.patch.dict(sys.modules, {'hmi_image': image}):
            report = P.import_picture(self.workspace, 0, self.source)
        self.assertEqual(report['compiled_name'], '10.i')
        self.assertEqual(report['source_name'], '10.is')
        self.assertEqual((self.workspace / 'files/10.i').read_bytes(), example_picture(1))
        self.assertEqual((self.workspace / 'files/2.i').read_bytes(), self.original['2.i'])
        self.assertEqual((self.workspace / 'files/2.is').read_bytes(), self.original['2.is'])
        self.assertEqual((self.workspace / 'manifest.json').read_bytes(), original_manifest)
        self.assertFalse((self.workspace / P.IMPORT_PENDING).exists())
        self.assertFalse((self.workspace / P.IMPORT_TXN_DIR).exists())
        kwargs = image.encode_png.call_args.kwargs
        self.assertEqual(kwargs['template'], self.original['10.i'])
        self.assertEqual(kwargs['source_template'], self.original['10.is'])
        self.assertEqual(kwargs['picture_id'], 0)

    def test_changed_pair_requires_replace(self):
        path = self.workspace / 'files/10.i'
        path.write_bytes(path.read_bytes() + b'changed')
        image = types.SimpleNamespace(
            encode_png=mock.Mock(), validate_picture=lambda data: H.picture_header(data),
            validate_source=lambda data: {},
        )
        with mock.patch.dict(sys.modules, {'hmi_image': image}):
            with self.assertRaisesRegex(ValueError, 'use --replace'):
                P.import_picture(self.workspace, 0, self.source)
        image.encode_png.assert_not_called()

    def test_replace_allows_changed_target_without_manifest_edit(self):
        original_manifest = (self.workspace / 'manifest.json').read_bytes()
        target = self.workspace / 'files/10.i'
        target.write_bytes(example_picture(1))
        image = types.SimpleNamespace(
            encode_png=mock.Mock(return_value=(example_picture(), b'png-replacement')),
            validate_picture=lambda data: H.picture_header(data),
            validate_source=lambda data: {},
        )
        with mock.patch.dict(sys.modules, {'hmi_image': image}):
            P.import_picture(self.workspace, 0, self.source, replace=True)
        self.assertEqual(target.read_bytes(), example_picture())
        self.assertEqual((self.workspace / 'manifest.json').read_bytes(), original_manifest)

    def test_invalid_ids_and_font_options_fail_before_encoder(self):
        with self.assertRaises(ValueError):
            P.import_picture(self.workspace, True, self.source)
        with self.assertRaises(ValueError):
            P.import_picture(self.workspace, -1, self.source)
        with self.assertRaises(ValueError):
            P.import_font(self.workspace, 0, self.source, height=0)
        with self.assertRaises(ValueError):
            P.import_font(self.workspace, 0, self.source, bpp=2)
        with self.assertRaises(ValueError):
            P.import_font(self.workspace, 0, self.source, size=0)

    def test_symlink_and_manifest_escape_are_rejected(self):
        outside = self.directory / 'outside'
        outside.write_bytes(b'outside')
        target = self.workspace / 'files/10.i'
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaises(ValueError):
            P.import_picture(self.workspace, 0, self.source)

        target.unlink()
        target.write_bytes(self.original['10.i'])
        manifest_path = self.workspace / 'manifest.json'
        real_manifest = self.directory / 'manifest-real.json'
        real_manifest.write_bytes(manifest_path.read_bytes())
        manifest_path.unlink()
        manifest_path.symlink_to(real_manifest)
        with self.assertRaises(ValueError):
            P.import_picture(self.workspace, 0, self.source)

    def test_pair_publish_failure_rolls_back_both_files(self):
        old_i = (self.workspace / 'files/10.i').read_bytes()
        old_is = (self.workspace / 'files/10.is').read_bytes()
        image = types.SimpleNamespace(
            encode_png=mock.Mock(return_value=(example_picture(1), b'png-replacement')),
            validate_picture=lambda data: H.picture_header(data),
            validate_source=lambda data: {},
        )
        original_check = P.H.check
        original_replace = P.os.replace
        calls = {'count': 0}
        def fail_second_target(source, destination):
            calls['count'] += 1
            if calls['count'] == 3:  # marker, first target, second target
                raise OSError('injected publish failure')
            return original_replace(source, destination)
        P.H.check = lambda files: []
        P.os.replace = fail_second_target
        try:
            with mock.patch.dict(sys.modules, {'hmi_image': image}):
                with self.assertRaises(OSError):
                    P.import_picture(self.workspace, 0, self.source)
        finally:
            P.H.check = original_check
            P.os.replace = original_replace
        self.assertEqual((self.workspace / 'files/10.i').read_bytes(), old_i)
        self.assertEqual((self.workspace / 'files/10.is').read_bytes(), old_is)
        self.assertFalse((self.workspace / P.IMPORT_PENDING).exists())

    def test_interrupted_pair_is_blocked_then_recovered(self):
        old_i = (self.workspace / 'files/10.i').read_bytes()
        old_is = (self.workspace / 'files/10.is').read_bytes()
        image = types.SimpleNamespace(
            encode_png=mock.Mock(return_value=(example_picture(1), b'png-replacement')),
            validate_picture=lambda data: H.picture_header(data),
            validate_source=lambda data: {},
        )
        original_check, original_replace = P.H.check, P.os.replace
        calls = {'count': 0}
        def fail_rollback(source, destination):
            calls['count'] += 1
            if calls['count'] in (3, 4):
                raise OSError('injected publish/rollback failure')
            return original_replace(source, destination)
        P.H.check = lambda files: []
        P.os.replace = fail_rollback
        try:
            with mock.patch.dict(sys.modules, {'hmi_image': image}):
                with self.assertRaises(RuntimeError):
                    P.import_picture(self.workspace, 0, self.source)
        finally:
            P.H.check, P.os.replace = original_check, original_replace
        with self.assertRaisesRegex(ValueError, 'recover-import'):
            P.load_workspace(self.workspace)
        report = P.recover_import(self.workspace)
        self.assertEqual(report['restored'], ['files/10.i'])
        self.assertEqual((self.workspace / 'files/10.i').read_bytes(), old_i)
        self.assertEqual((self.workspace / 'files/10.is').read_bytes(), old_is)

    def test_recovery_retries_after_partial_rollback_without_consuming_backup(self):
        root = self.directory / 'partial-recovery'
        (root / 'files').mkdir(parents=True)
        (root / 'files/a').write_bytes(b'old-a')
        (root / 'files/b').write_bytes(b'old-b')
        old = {'a': b'old-a', 'b': b'old-b'}
        new = {'a': b'new-a', 'b': b'new-b'}
        real_assert, real_replace = P._assert_target_hash, P.os.replace
        published = {'count': 0}
        calls = {'count': 0}
        def fail_after_publish(path, expected, label):
            if label == 'Published picture target':
                published['count'] += 1
                if published['count'] == 2:
                    raise OSError('injected post-publish validation failure')
            return real_assert(path, expected, label)
        def fail_second_rollback(source, destination):
            calls['count'] += 1
            if calls['count'] == 5:  # marker, two publishes, b rollback, a rollback
                raise OSError('injected second rollback failure')
            return real_replace(source, destination)
        P._assert_target_hash, P.os.replace = fail_after_publish, fail_second_rollback
        try:
            with self.assertRaises(RuntimeError):
                P._picture_pair_transaction(root, old, new, ('a', 'b'))
        finally:
            P._assert_target_hash, P.os.replace = real_assert, real_replace
        self.assertEqual((root / 'files/a').read_bytes(), b'new-a')
        self.assertEqual((root / 'files/b').read_bytes(), b'old-b')
        self.assertTrue((root / P.IMPORT_PENDING).exists())
        report = P.recover_import(root)
        self.assertEqual(report['restored'], ['files/a'])
        self.assertEqual((root / 'files/a').read_bytes(), b'old-a')
        self.assertEqual((root / 'files/b').read_bytes(), b'old-b')
        self.assertFalse((root / P.IMPORT_PENDING).exists())


class RealResourceImportTest(unittest.TestCase):
    def test_generated_picture_and_font_slots_pack_without_external_process(self):
        try:
            import hmi_image
            import hmi_font
        except ImportError as e:
            self.skipTest('resource encoder dependency unavailable: %s' % e)
        font_path = next((Path(p) for p in (
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf') if Path(p).is_file()), None)
        if font_path is None:
            self.skipTest('no test TTF installed')
        try:
            compiled, source = hmi_image.encode_png(png_2x2())
            font_data = hmi_font.encode_ttf(font_path, height=24, chars='AB')
        except (ImportError, OSError, ValueError) as e:
            self.skipTest('resource encoder integration unavailable: %s' % e)
        with tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='real-resource-import-') as td:
            directory = Path(td)
            files = {'Screen.i': compiled, 'Screen.is': source, 'Font.zi': font_data}
            workspace, _original = indexed_workspace(directory, files,
                                                     [('i', 'Screen.i'), ('zi', 'Font.zi')], 'real-source')
            png = directory / 'replacement.png'
            png.write_bytes(png_2x2())
            font_source = directory / 'replacement.ttf'
            font_source.write_bytes(font_path.read_bytes())
            picture_report = P.import_picture(workspace, 0, png)
            font_report = P.import_font(workspace, 0, font_source, chars='AB')
            self.assertEqual((picture_report['width'], picture_report['height']), (2, 2))
            self.assertEqual(font_report['glyphs'], 2)
            output = directory / 'packed.HMI'
            with mock.patch('subprocess.run', side_effect=AssertionError('external process started')):
                result = P.pack(workspace, output)
            self.assertEqual(result['backend'], 'python')
            self.assertTrue(I.verify_container(output.read_bytes(), require_exact_body=True))

    def test_font_atomic_write_failure_leaves_target_and_no_temp(self):
        try:
            import hmi_font
        except ImportError as e:
            self.skipTest('font encoder dependency unavailable: %s' % e)
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if not font_path.is_file():
            self.skipTest('no test TTF installed')
        try:
            old_font = hmi_font.encode_ttf(font_path, height=24, chars='AB')
        except (ImportError, OSError, ValueError) as e:
            self.skipTest('font encoder integration unavailable: %s' % e)
        with tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='font-atomic-failure-') as td:
            directory = Path(td)
            workspace, _ = indexed_workspace(directory, {'Font.zi': old_font},
                                             [('zi', 'Font.zi')], 'font-source')
            source = directory / 'replacement.ttf'
            source.write_bytes(font_path.read_bytes())
            target = workspace / 'files/Font.zi'
            original = target.read_bytes()
            real_replace = P.os.replace
            P.os.replace = mock.Mock(side_effect=OSError('injected atomic failure'))
            try:
                with self.assertRaises(OSError):
                    P.import_font(workspace, 0, source, chars='AB')
            finally:
                P.os.replace = real_replace
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse((workspace / P.IMPORT_TXN_DIR).exists())

    def test_font_source_target_collision_fails_before_replacement(self):
        try:
            import hmi_font
        except ImportError as e:
            self.skipTest('font encoder dependency unavailable: %s' % e)
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if not font_path.is_file():
            self.skipTest('no test TTF installed')
        try:
            old_font = hmi_font.encode_ttf(font_path, height=24, chars='AB')
        except (ImportError, OSError, ValueError) as e:
            self.skipTest('font encoder integration unavailable: %s' % e)
        with tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='font-path-collision-') as td:
            directory = Path(td)
            workspace, _ = indexed_workspace(directory, {'Font.zi': old_font},
                                             [('zi', 'Font.zi')], 'font-collision-source')
            target = workspace / 'files/Font.zi'
            original = target.read_bytes()
            with self.assertRaises(ValueError):
                P.import_font(workspace, 0, target, chars='AB')
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse((workspace / P.IMPORT_TXN_DIR).exists())


class OfflinePackTest(unittest.TestCase):
    def setUp(self):
        (ROOT / '.state').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.state', prefix='offline-pack-test-')
        self.directory = Path(self.temp.name)
        page = I.sign_page(example_page(), version=(1, 65, 5))
        head = bytearray(96)
        struct.pack_into('<I', head, 4, 96)
        struct.pack_into('<I', head, 16, 0x38a388a8)
        struct.pack_into('<II', head, 24, 96, 1)
        index = I.sign_index(bytes(head) + b'pa'.ljust(8, b'\0') + b'0.pa'.ljust(8, b'\0'), version=(1, 65, 5))
        self.files = {'main.HMI': index, '0.pa': page, 'Program.s': b'page demo\r\n'}
        source = self.directory / 'source.HMI'
        source.write_bytes(H.write_container(self.files))
        self.workspace = self.directory / 'workspace'
        P.unpack(source, self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_edit_pack_needs_no_wine_or_agent(self):
        output = self.directory / 'result.HMI'
        with mock.patch('subprocess.run', side_effect=AssertionError('External process started')), \
                mock.patch.object(P, 'pack_editor', side_effect=AssertionError('Editor backend used')), \
                mock.patch.dict(os.environ, {'PATH': '/no/wine', 'WINEPREFIX': '/no/prefix', 'AGENT_DIR': '/no/agent'}):
            result = P.pack(self.workspace, output)
        self.assertEqual(result['backend'], 'python')
        self.assertEqual(result['changed_files'], [])
        self.assertEqual(result['signed_files'], [])
        self.assertEqual(H.read_container(output.read_bytes()), self.files)
        self.assertTrue(I.verify_container(output.read_bytes(), require_exact_body=True))

    def test_edited_text_event_and_program(self):
        json_path = self.workspace / 'pages/0.json'
        edit = json.loads(json_path.read_text())
        edit['objects'][0]['attributes']['txt'] = 'Saving Wi-Fi — автономно'
        edit['objects'][0]['events']['codesload'].insert(0, '// Standalone pack test')
        json_path.write_text(json.dumps(edit, ensure_ascii=False))
        program = self.workspace / 'files/Program.s'
        program.write_bytes(program.read_bytes() + b'// Standalone pack test\r\n')
        expected, changed = P.load_workspace(self.workspace)
        output = self.directory / 'edited.HMI'
        result = P.pack(self.workspace, output)
        rebuilt = H.read_container(output.read_bytes())
        self.assertEqual(result['changed_files'], changed)
        self.assertEqual(result['signed_files'], ['0.pa'])
        self.assertEqual(list(rebuilt), list(self.files))
        self.assertEqual(rebuilt['main.HMI'], self.files['main.HMI'])
        self.assertEqual(rebuilt['Program.s'], expected['Program.s'])
        self.assertEqual(P.editable_page(rebuilt['0.pa']), edit)
        self.assertTrue(I.verify_page(rebuilt['0.pa']))
        self.assertEqual(P.signature_neutral('0.pa', rebuilt['0.pa']), P.signature_neutral('0.pa', expected['0.pa']))

    def test_existing_output_is_preserved(self):
        output = self.directory / 'keep.HMI'
        output.write_bytes(b'keep me')
        with self.assertRaises(FileExistsError):
            P.pack(self.workspace, output)
        self.assertEqual(output.read_bytes(), b'keep me')

    def test_invalid_unedited_checksum_is_not_silently_repaired(self):
        raw = bytearray(self.files['0.pa'])
        raw[0] ^= 1
        self.files['0.pa'] = bytes(raw)
        source = self.directory / 'invalid.HMI'
        source.write_bytes(H.write_container(self.files))
        workspace = self.directory / 'invalid-workspace'
        P.unpack(source, workspace)
        with self.assertRaisesRegex(ValueError, 'unedited workspace file'):
            P.pack(workspace, self.directory / 'not-created.HMI')
        self.assertFalse((self.directory / 'not-created.HMI').exists())

    def test_editor_backend_is_explicit_and_unknown_backend_rejected(self):
        with mock.patch.object(P, 'pack_editor', return_value={'backend': 'editor'}) as editor:
            self.assertEqual(P.pack(self.workspace, self.directory / 'oracle.HMI', backend='editor'), {'backend': 'editor'})
            editor.assert_called_once()
        with self.assertRaises(ValueError):
            P.pack(self.workspace, self.directory / 'unknown.HMI', backend='unknown')


@unittest.skipUnless(os.environ.get('HMI_FILE'), 'set HMI_FILE')
class RealProjectSerializationTest(unittest.TestCase):
    def test_all_pages_roundtrip_and_edit(self):
        files = H.read_container(Path(os.environ['HMI_FILE']).read_bytes())
        for name, b in files.items():
            if not name.endswith('.pa'):
                continue
            with self.subTest(page=name):
                edit = P.editable_page(b)
                self.assertEqual(P.encode_page(b, edit), b)
                edit['objects'][0]['events']['codesload'].insert(0, '// Workspace test')
                modified = P.encode_page(b, edit)
                self.assertEqual(P.editable_page(modified), edit)
                self.assertEqual(H.page_header(modified)['datasize'], len(modified))


if __name__ == '__main__':
    unittest.main()
