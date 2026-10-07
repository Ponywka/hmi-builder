#!/usr/bin/env python3
"""Editable HMI workspaces with standalone Python packing; no Wine/editor is required.

python3 hmi_project.py unpack project.HMI workspace
python3 hmi_project.py pack workspace result.HMI --config project.json
python3 hmi_project.py import-picture workspace 0 image.png [--replace]
python3 hmi_project.py import-font workspace 0 font.ttf [--chars TEXT | --chars-file FILE] [--replace]
python3 hmi_project.py recover-import workspace
python3 hmi_project.py export-config workspace [--update-snapshot]
python3 hmi_project.py validate workspace --config project.json
python3 hmi_project.py pack workspace result.HMI --config project.json

Edit files/Program.s, raw files/*, or pages/*.json. For adding/deleting/reordering pages, components, pictures and
fonts use the structural config (export-config, see hmi_config.py and README.md). Explicit import commands compile a PNG picture pair or a
TTF/OTF font into an existing resource slot; ordinary pack never automatically re-encodes resources. This builds
.HMI projects, not .tft firmware. Python calculates page/index/container checksums, verifies the output, then
creates it exclusively without overwriting existing files.
The optional --backend editor uses an already running USART HMI agent only for development comparisons.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import time
import uuid

import hmi_parse as H
import hmi_integrity as I

ROOT = Path(__file__).resolve().parent
FORMAT = 'usart-hmi-workspace-v1'
TEXT_ATTRS = {'objname', 'txt', 'path'}
# Import transactions deliberately live beside, rather than inside, files/.  A
# marker is enough to make a half-published pair visible to every workspace
# reader without copying the resource bytes into the marker itself.
IMPORT_PENDING = '.import.pending'
IMPORT_TXN_DIR = '.import-transaction'
IMPORT_TXN_FORMAT = 'usart-hmi-import-v1'


def digest(b):
    return hashlib.sha256(b).hexdigest()


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf8')


def canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def safe_name(name):
    if not isinstance(name, str) or not name or '/' in name or '\\' in name or name in ('.', '..') or '\0' in name:
        raise ValueError('Invalid internal file name: %r' % name)
    if len(name.encode('latin1')) > 15:
        raise ValueError('Internal file name exceeds 15 bytes: ' + name)
    return name


def attr_value(name, raw):
    if name in TEXT_ATTRS:
        try:
            return raw.decode('utf8')
        except UnicodeDecodeError:
            pass
    if name not in TEXT_ATTRS and len(raw) in (1, 2, 4):
        signed = (name in H.SIGNED16 and len(raw) == 2) or (name in H.SIGNED32 and len(raw) == 4)
        return int.from_bytes(raw, 'little', signed=signed)
    return {'hex': raw.hex()}


def editable_page(b):
    header = H.page_header(b)
    objects = []
    for i in range(header['object_count']):
        offset, length, _ = struct.unpack_from('<III', b, header['table_addr'] + 12 * i)
        block = b[header['table_addr'] + offset:header['table_addr'] + offset + length]
        attrs, events, _ = H.parse_object(block, 0)
        objects.append({'attributes': {k: attr_value(k, v) for k, v in attrs.items()}, 'events': events})
    return {'name': header['name'], 'objects': objects}


def lp(b):
    return struct.pack('<I', len(b)) + b


def encode_value(name, value, original):
    if isinstance(value, dict) and set(value) == {'hex'}:
        return bytes.fromhex(value['hex'])
    if isinstance(value, str) and name in TEXT_ATTRS:
        return value.encode('utf8')
    if isinstance(value, int) and not isinstance(value, bool) and len(original) in (1, 2, 4):
        signed = (name in H.SIGNED16 and len(original) == 2) or (name in H.SIGNED32 and len(original) == 4)
        return value.to_bytes(len(original), 'little', signed=signed)
    raise ValueError('Invalid value for attribute %s; use a number, text, or {"hex": "..."}' % name)


def encode_page(original, edit):
    """Keep original widths, field order, unknown header bytes, and object trailing bytes. Rebuild sizes/offsets."""
    old = editable_page(original)
    if canonical(edit) == canonical(old):
        return original
    hd = H.page_header(original)
    if set(edit) != {'name', 'objects'} or len(edit['objects']) != hd['object_count']:
        raise ValueError('Adding/removing objects is not supported by the workspace encoder yet')
    name = edit['name'].encode('utf8')
    if not name or len(name) > 16 or b'\0' in name:
        raise ValueError('Page name must fit in 16 bytes')
    head = bytearray(original[:hd['table_addr']])
    head[24:40] = name.ljust(16, b'\0')
    offset = 12 * hd['object_count']
    table, blocks = [], []
    for i, obj in enumerate(edit['objects']):
        if set(obj) != {'attributes', 'events'}:
            raise ValueError('Each object needs attributes and events')
        old_off, length, extra = struct.unpack_from('<III', original, hd['table_addr'] + 12 * i)
        block = original[hd['table_addr'] + old_off:hd['table_addr'] + old_off + length]
        attrs, events, end = H.parse_object(block, 0)
        values = obj['attributes']
        if set(values) != set(attrs):
            raise ValueError('Attribute keys changed for object %d; adding/removing attributes is unsupported' % i)
        if i == 0 and values.get('objname') != edit['name']:
            raise ValueError('Page name and the first object objname must agree')
        if set(obj['events']) != set(events):
            raise ValueError('Event names changed for object %d; keep empty events instead of deleting them' % i)
        out = lp(('att-%d' % len(attrs)).encode('ascii'))
        for key, raw in attrs.items():
            value = encode_value(key, values[key], raw)
            if len(key.encode('ascii')) > 16:
                raise ValueError('Attribute name too long')
            out += struct.pack('<I', 16 + len(value)) + key.encode('ascii').ljust(16, b'\0') + value
        for event in events:
            lines = obj['events'][event]
            if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
                raise ValueError('Event %s must contain a list of code lines' % event)
            out += lp(('%s-%d' % (event, len(lines))).encode('ascii'))
            out += b''.join(lp(line.encode('utf8')) for line in lines)
        out += block[end:]   # terminator / any unrecognised trailing bytes
        table.append(struct.pack('<III', offset, len(out), extra))
        blocks.append(out)
        offset += len(out)
    struct.pack_into('<I', head, 4, hd['table_addr'] + offset)
    return bytes(head) + b''.join(table) + b''.join(blocks)


def _lexists(path):
    """Like Path.exists(), but also true for a broken symlink."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _reject_symlink(path, label, missing_ok=False):
    try:
        st = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise FileNotFoundError('%s does not exist: %s' % (label, path))
    if stat.S_ISLNK(st.st_mode):
        raise ValueError('%s must not be a symlink: %s' % (label, path))
    return st


def _safe_workspace_path(root, relative, label, *, regular=True):
    """Resolve a workspace-relative path without following symlink components."""
    try:
        rel = Path(relative)
    except TypeError as e:
        raise ValueError('%s is not a relative path: %r' % (label, relative)) from e
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('%s escapes the workspace: %s' % (label, relative))
    root = Path(root).resolve()
    current = root
    for part in rel.parts:
        current = current / part
        st = _reject_symlink(current, label, missing_ok=True)
        if st is not None and regular and not stat.S_ISREG(st.st_mode):
            # The final path must be a file; intermediate directories are
            # checked below and are allowed to be directories.
            if current != root / rel:
                if not stat.S_ISDIR(st.st_mode):
                    raise ValueError('%s has a non-directory parent: %s' % (label, current))
                continue
            raise ValueError('%s is not a regular file: %s' % (label, current))
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise ValueError('%s escapes the workspace: %s' % (label, relative)) from e
    if regular:
        _reject_symlink(current, label)
        if not current.is_file():
            raise ValueError('%s is not a regular file: %s' % (label, current))
    return current


def _pending_message(directory):
    return ('Pending import transaction in %s; run `python3 %s recover-import %s` '
            'before loading or packing this workspace' %
            (directory, Path(__file__).resolve(), directory))


def _ensure_no_pending(directory):
    directory = Path(directory)
    marker = directory / IMPORT_PENDING
    txn_root = directory / IMPORT_TXN_DIR
    if _lexists(marker):
        if marker.is_symlink():
            raise ValueError('Import transaction marker is a symlink: %s' % marker)
        raise ValueError(_pending_message(directory))
    if _lexists(txn_root):
        if txn_root.is_symlink():
            raise ValueError('Import transaction directory is a symlink: %s' % txn_root)
        raise ValueError('Import transaction directory has no marker; run `python3 %s recover-import %s`'
                         % (Path(__file__).resolve(), directory))


def _read_import_manifest(directory):
    """Validate paths used by an import and return (root, manifest)."""
    directory = Path(directory)
    st = _reject_symlink(directory, 'Workspace')
    if not stat.S_ISDIR(st.st_mode):
        raise ValueError('Workspace is not a directory: %s' % directory)
    root = directory.resolve()
    manifest_path = root / 'manifest.json'
    _reject_symlink(manifest_path, 'Workspace manifest')
    files_dir = root / 'files'
    _reject_symlink(files_dir, 'Workspace files directory')
    if not files_dir.is_dir():
        raise ValueError('Workspace files directory is missing: %s' % files_dir)
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf8'))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValueError('Invalid workspace manifest: %s' % e) from e
    if not isinstance(manifest, dict) or manifest.get('format') != FORMAT:
        raise ValueError('Unsupported workspace format')
    records = manifest.get('files')
    if not isinstance(records, list):
        raise ValueError('Workspace manifest files must be a list')
    names = set()
    for rec in records:
        if not isinstance(rec, dict):
            raise ValueError('Invalid workspace manifest file record')
        name = safe_name(rec.get('name'))
        if name in names:
            raise ValueError('Duplicate internal file: ' + name)
        names.add(name)
        _safe_workspace_path(root, Path('files') / name, 'Raw file ' + name)
        if 'page' in rec:
            page = rec.get('page')
            _safe_workspace_path(root, page, 'Page JSON for ' + name)
    # A symlink anywhere in the two editable trees could otherwise redirect a
    # later read or replacement after the manifest has been checked.
    for tree_name in ('files', 'pages'):
        tree = root / tree_name
        _reject_symlink(tree, tree_name + ' directory', missing_ok=True)
        if tree.is_dir():
            for child in tree.rglob('*'):
                if child.is_symlink():
                    raise ValueError('Workspace path must not be a symlink: %s' % child)
    return root, manifest


def _manifest_records(manifest):
    out = {}
    for rec in manifest.get('files', []):
        name = safe_name(rec.get('name'))
        out[name] = rec
    return out


def _strict_id(value, label='resource id'):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('%s must be a non-negative integer' % label)
    return value


def _strict_optional_index(value):
    return _strict_id(value, 'font index')


def _validate_font_options(height, bpp, size):
    if height is not None and (isinstance(height, bool) or not isinstance(height, int) or not 1 <= height <= 255):
        raise ValueError('height must be an integer in 1..255')
    if bpp is not None and (isinstance(bpp, bool) or not isinstance(bpp, int) or bpp not in (1, 3)):
        raise ValueError('bpp must be 1 or 3')
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        raise ValueError('size must be a positive integer')


def _read_user_file(path, label):
    try:
        path = Path(path)
    except TypeError as e:
        raise ValueError('%s is not a path: %r' % (label, path)) from e
    st = _reject_symlink(path, label)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('%s is not a regular file: %s' % (label, path))
    return path.read_bytes()


def _resource_record(files, kind, resource_id):
    """Resolve IDs from filtered index order, never from a filename."""
    resource_id = _strict_id(resource_id)
    try:
        _words, records = H.parse_index(files['main.HMI'])
    except (KeyError, ValueError, struct.error) as e:
        raise ValueError('Cannot resolve resource ID from main.HMI: %s' % e) from e
    selected = [name for entry_kind, name in records if entry_kind == kind]
    if resource_id >= len(selected):
        raise ValueError('%s id %d is out of range (available: %d)' %
                         ('picture' if kind == 'i' else 'font', resource_id, len(selected)))
    name = selected[resource_id]
    if kind == 'i':
        if not name.endswith('.i'):
            raise ValueError('Unsupported picture target in index: ' + name)
        pair = name[:-2] + '.is'
        if name not in files or pair not in files:
            raise ValueError('Picture %d has no complete .i/.is pair (%s)' % (resource_id, name))
        return name, pair
    if kind == 'zi':
        if not name.endswith('.zi') or name not in files:
            raise ValueError('Unsupported font target in index: ' + name)
        return name
    raise ValueError('Unsupported import resource kind: ' + kind)


def _lazy_encoder(module_name):
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise RuntimeError('%s is required for this import operation' % module_name) from e


def _check_candidate(files):
    try:
        problems = H.check(files)
    except Exception as e:
        raise ValueError('Invalid candidate workspace: %s' % e) from e
    if problems:
        raise ValueError('Invalid candidate workspace:\n' + '\n'.join(problems))


def _same_dimensions(old_header, new_header):
    for key in ('w', 'width'):
        if key in old_header and key in new_header and old_header[key] != new_header[key]:
            raise ValueError('Imported picture width differs from the existing slot')
    for key in ('h', 'height'):
        if key in old_header and key in new_header and old_header[key] != new_header[key]:
            raise ValueError('Imported picture height differs from the existing slot')


def _new_file_bytes(value, label):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError('%s encoder did not return bytes' % label)
    return bytes(value)


def _changed_slot(files, records, name):
    rec = records.get(name)
    if not rec or not isinstance(rec.get('sha256'), str):
        raise ValueError('Manifest has no original hash for ' + name)
    return digest(files[name]) != rec['sha256']


def _import_workspace(directory):
    _ensure_no_pending(directory)
    root, manifest = _read_import_manifest(directory)
    # load_workspace retains the established page/raw conflict behavior and
    # page JSON representation; the path pass above prevents symlink escapes.
    files, changed = load_workspace(root)
    return root, manifest, files, changed


def _safe_txn_relative(root, relative, label, *, regular=False):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('%s escapes workspace: %s' % (label, relative))
    return _safe_workspace_path(root, rel, label, regular=regular)


def _write_exclusive(path, data):
    path = Path(path)
    _reject_symlink(path, 'Output staging path', missing_ok=True)
    with path.open('xb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _remove_transaction(root, txn_dir, marker=None):
    """Remove only a transaction tree we have proved contains no symlink."""
    txn_dir = Path(txn_dir)
    if _lexists(txn_dir):
        _reject_symlink(txn_dir, 'Import transaction directory')
        if not txn_dir.is_dir():
            raise ValueError('Import transaction path is not a directory: %s' % txn_dir)
        for child in txn_dir.rglob('*'):
            if child.is_symlink():
                raise ValueError('Import transaction path is a symlink: %s' % child)
        shutil.rmtree(txn_dir)
        parent = txn_dir.parent
        if parent.name == IMPORT_TXN_DIR and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    if marker is not None and _lexists(marker):
        _reject_symlink(marker, 'Import transaction marker')
        marker.unlink()


def _write_pending_marker(root, txn_dir, entries):
    root, txn_dir = Path(root), Path(txn_dir)
    marker = root / IMPORT_PENDING
    if _lexists(marker):
        raise ValueError('Import transaction marker already exists: %s' % marker)
    marker_data = {
        'format': IMPORT_TXN_FORMAT,
        'operation': 'picture',
        'transaction_dir': str(txn_dir.relative_to(root)),
        'entries': entries,
    }
    tmp = root / (IMPORT_PENDING + '.' + uuid.uuid4().hex + '.tmp')
    try:
        _write_exclusive(tmp, json_bytes(marker_data))
        os.replace(tmp, marker)
    except BaseException:
        if _lexists(tmp) and not tmp.is_symlink():
            tmp.unlink()
        raise


def _assert_target_hash(path, expected, label):
    st = _reject_symlink(path, label)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('%s is not a regular file: %s' % (label, path))
    actual = digest(path.read_bytes())
    if actual != expected:
        raise RuntimeError('%s changed during import; refusing to overwrite it' % label)


def _picture_pair_transaction(root, old_files, new_files, pair):
    """Publish two picture files with a recoverable marker and rollback."""
    root = Path(root)
    _ensure_no_pending(root)
    txn_name = 'picture-' + uuid.uuid4().hex
    txn_dir = root / IMPORT_TXN_DIR / txn_name
    txn_root = root / IMPORT_TXN_DIR
    _reject_symlink(txn_root, 'Import transaction root', missing_ok=True)
    if _lexists(txn_root) and not txn_root.is_dir():
        raise ValueError('Import transaction root is not a directory: %s' % txn_root)
    txn_root.mkdir(exist_ok=True)
    _reject_symlink(txn_root, 'Import transaction root')
    txn_dir.mkdir()
    entries = []
    marker = root / IMPORT_PENDING
    replaced = []
    try:
        for index, name in enumerate(pair):
            target = _safe_workspace_path(root, Path('files') / name, 'Picture target ' + name)
            old = _new_file_bytes(old_files[name], name)
            new = _new_file_bytes(new_files[name], name)
            stage = txn_dir / ('stage-%d' % index)
            backup = txn_dir / ('backup-%d' % index)
            _write_exclusive(stage, new)
            _write_exclusive(backup, old)
            entries.append({
                'target': str(Path('files') / name),
                'stage': str(stage.relative_to(root)),
                'backup': str(backup.relative_to(root)),
                'original_sha256': digest(old),
                'new_sha256': digest(new),
            })
        _write_pending_marker(root, txn_dir, entries)
        # The marker exists before the first replace. It makes an interrupted
        # process observable to every workspace reader.
        for entry in entries:
            target = _safe_workspace_path(root, entry['target'], 'Picture target')
            _assert_target_hash(target, entry['original_sha256'], 'Picture target')
            stage = _safe_txn_relative(root, entry['stage'], 'Picture staging path', regular=True)
            _assert_target_hash(stage, entry['new_sha256'], 'Picture staging file')
            os.replace(stage, target)
            replaced.append(entry)
        for entry in entries:
            target = _safe_workspace_path(root, entry['target'], 'Picture target')
            _assert_target_hash(target, entry['new_sha256'], 'Published picture target')
        _remove_transaction(root, txn_dir, marker)
    except BaseException as error:
        rollback_error = None
        try:
            # Determine the state of every target first. This also handles an
            # os.replace shim that reports failure after moving one file.
            to_restore = []
            for entry in entries:
                target = _safe_workspace_path(root, entry['target'], 'Picture target')
                current = digest(target.read_bytes())
                if current == entry['original_sha256']:
                    continue
                if current != entry['new_sha256']:
                    raise RuntimeError('Picture target changed outside this transaction: %s' % target)
                to_restore.append(entry)
            for entry in reversed(to_restore):
                target = _safe_workspace_path(root, entry['target'], 'Picture target')
                _assert_target_hash(target, entry['new_sha256'], 'Published picture target')
                backup = _safe_txn_relative(root, entry['backup'], 'Picture backup path', regular=True)
                _assert_target_hash(backup, entry['original_sha256'], 'Picture backup file')
                os.replace(backup, target)
            _remove_transaction(root, txn_dir, marker)
        except BaseException as e:
            rollback_error = e
        if rollback_error is not None:
            raise RuntimeError('%s; transaction retained, run `python3 %s recover-import %s`'
                               % (error, Path(__file__).resolve(), root)) from rollback_error
        raise


def _validate_picture_output(image, old_i, new_i, new_is, picture_id):
    try:
        old_header = H.picture_header(old_i)
    except Exception as e:
        raise ValueError('Existing picture slot is invalid: %s' % e) from e
    new_header = image.validate_picture(new_i)
    source_header = image.validate_source(new_is)
    if not isinstance(new_header, dict) or not isinstance(source_header, dict):
        raise ValueError('Picture validators did not return headers')
    _same_dimensions(old_header, new_header)
    _same_dimensions(old_header, source_header)
    _same_dimensions(new_header, source_header)
    # The on-disk pictureid is a template/profile field in existing projects;
    # logical IDs come from filtered main.HMI index order and need not match it.
    return new_header


def import_picture(directory, picture_id, source, replace=False):
    """Replace one existing indexed picture slot, preserving the HMI layout."""
    picture_id = _strict_id(picture_id, 'picture id')
    root, manifest, files, _changed = _import_workspace(directory)
    records = _manifest_records(manifest)
    compiled_name, source_name = _resource_record(files, 'i', picture_id)
    for name in (compiled_name, source_name):
        if _changed_slot(files, records, name) and not replace:
            raise ValueError('%s already differs from the unpacked workspace; use --replace' % name)
    image = _lazy_encoder('hmi_image')
    old_i, old_is = files[compiled_name], files[source_name]
    png = _read_user_file(source, 'Picture source')
    try:
        encoded = image.encode_png(png, template=old_i, source_template=old_is, picture_id=picture_id)
    except Exception:
        # Encoding is deliberately before any staging or target write.
        raise
    if not isinstance(encoded, (tuple, list)) or len(encoded) != 2:
        raise ValueError('hmi_image.encode_png must return (compiled_i, source_is)')
    new_i = _new_file_bytes(encoded[0], 'compiled picture')
    new_is = _new_file_bytes(encoded[1], 'picture source')
    header = _validate_picture_output(image, old_i, new_i, new_is, picture_id)
    candidate = dict(files)
    candidate[compiled_name], candidate[source_name] = new_i, new_is
    _check_candidate(candidate)
    _picture_pair_transaction(root, files, candidate, (compiled_name, source_name))
    return {'operation': 'import-picture', 'picture_id': picture_id,
            'compiled_name': compiled_name, 'source_name': source_name,
            'width': header.get('w', header.get('width')),
            'height': header.get('h', header.get('height'))}


def _parse_chars_file(path):
    text = _read_user_file(path, 'Character file').decode('utf8')
    # Keep printable Unicode, while treating either line ending as formatting.
    return ''.join(c for c in text if c not in '\r\n' and c.isprintable())


def _font_report(data, header, font_id, name):
    try:
        codes = H.font_codes(data, header)
    except Exception:
        codes = []
    coverage = {}
    for script, low, high in H.SCRIPTS:
        count = sum(1 for code in codes if low <= code <= high)
        if count:
            coverage[script] = count
    return {'operation': 'import-font', 'font_id': font_id,
            'font_name': header.get('name', name), 'name': header.get('name', name),
            'target_name': name, 'glyphs': header.get('trueziqty', len(codes)),
            'coverage': coverage}


def import_font(directory, font_id, source, chars=None, height=None, bpp=None, size=None,
                font_index=0, replace=False):
    """Replace one existing indexed font slot atomically."""
    font_id = _strict_id(font_id, 'font id')
    font_index = _strict_optional_index(font_index)
    _validate_font_options(height, bpp, size)
    try:
        font_source = Path(source)
    except TypeError as e:
        raise ValueError('Font source is not a path: %r' % (source,)) from e
    source_st = _reject_symlink(font_source, 'Font source')
    if not stat.S_ISREG(source_st.st_mode):
        raise ValueError('Font source is not a regular file: %s' % font_source)
    root, manifest, files, _changed = _import_workspace(directory)
    records = _manifest_records(manifest)
    name = _resource_record(files, 'zi', font_id)
    if _changed_slot(files, records, name) and not replace:
        raise ValueError('%s already differs from the unpacked workspace; use --replace' % name)
    font = _lazy_encoder('hmi_font')
    old = files[name]
    try:
        old_header = H.font_header(old)
    except Exception as e:
        raise ValueError('Existing font slot is invalid: %s' % e) from e
    # The template carries encoding/orientation and other target-specific
    # fields. Explicit defaults for height/name make the API behavior clear.
    try:
        encoded = font.encode_ttf(source, height=old_header.get('h') if height is None else height,
                                  chars=chars, name=old_header.get('name'), template=old,
                                  bpp=bpp, size=size, font_index=font_index)
    except Exception as e:
        raise ValueError('Font encoding failed: %s' % e) from e
    new = _new_file_bytes(encoded, 'font')
    new_header = font.validate_font(new)
    if not isinstance(new_header, dict):
        raise ValueError('Font validator did not return a header')
    candidate = dict(files)
    candidate[name] = new
    _check_candidate(candidate)
    _atomic_font_replace(root, name, old, new)
    return _font_report(new, new_header, font_id, name)


def _atomic_font_replace(root, name, old, new):
    target = _safe_workspace_path(root, Path('files') / name, 'Font target ' + name)
    txn_root = root / IMPORT_TXN_DIR
    _reject_symlink(txn_root, 'Import transaction root', missing_ok=True)
    if _lexists(txn_root) and not txn_root.is_dir():
        raise ValueError('Import transaction root is not a directory: %s' % txn_root)
    txn_root.mkdir(exist_ok=True)
    _reject_symlink(txn_root, 'Import transaction root')
    temp = txn_root / ('font-' + uuid.uuid4().hex + '.tmp')
    _write_exclusive(temp, new)
    try:
        _assert_target_hash(target, digest(old), 'Font target')
        _assert_target_hash(temp, digest(new), 'Font staging file')
        os.replace(temp, target)
    finally:
        try:
            if _lexists(temp) and not temp.is_symlink():
                temp.unlink()
        finally:
            if txn_root.is_dir() and not any(txn_root.iterdir()):
                txn_root.rmdir()


def _validate_marker(root):
    marker = root / IMPORT_PENDING
    _reject_symlink(marker, 'Import transaction marker')
    try:
        marker_data = json.loads(marker.read_text(encoding='utf8'))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise ValueError('Corrupt import transaction marker: %s' % e) from e
    if (not isinstance(marker_data, dict) or marker_data.get('format') != IMPORT_TXN_FORMAT or
            marker_data.get('operation') != 'picture'):
        raise ValueError('Corrupt import transaction marker format')
    txn_rel = marker_data.get('transaction_dir')
    txn = _safe_txn_relative(root, txn_rel, 'Transaction directory', regular=False)
    if txn.parent != root / IMPORT_TXN_DIR:
        raise ValueError('Corrupt import transaction directory path')
    _reject_symlink(txn, 'Transaction directory')
    if not txn.is_dir():
        raise ValueError('Corrupt import transaction directory: %s' % txn)
    entries = marker_data.get('entries')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Corrupt import transaction entries')
    checked = []
    targets = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Corrupt import transaction entry')
        target_rel, stage_rel, backup_rel = (entry.get(k) for k in ('target', 'stage', 'backup'))
        target = _safe_txn_relative(root, target_rel, 'Transaction target', regular=True)
        # os.replace() removes a stage file when that side of the pair has
        # already been published, so a missing stage is valid during recovery.
        stage = _safe_txn_relative(root, stage_rel, 'Transaction staging path', regular=False)
        _reject_symlink(stage, 'Transaction staging path', missing_ok=True)
        if _lexists(stage):
            st = stage.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError('Corrupt import transaction staging path')
        # Backups are intentionally retained while recovery replaces targets.
        # A missing backup is recoverable only if its target is already proven
        # to contain the original bytes (checked in recover_import()).
        backup = _safe_txn_relative(root, backup_rel, 'Transaction backup path', regular=False)
        _reject_symlink(backup, 'Transaction backup path', missing_ok=True)
        if _lexists(backup):
            st = backup.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError('Corrupt import transaction backup path')
        try:
            target.relative_to(root / 'files')
            stage.relative_to(txn)
            backup.relative_to(txn)
        except ValueError as e:
            raise ValueError('Corrupt import transaction path') from e
        if target in targets:
            raise ValueError('Corrupt import transaction has duplicate target')
        targets.add(target)
        old_hash, new_hash = entry.get('original_sha256'), entry.get('new_sha256')
        if (not isinstance(old_hash, str) or len(old_hash) != 64 or
                not isinstance(new_hash, str) or len(new_hash) != 64):
            raise ValueError('Corrupt import transaction hashes')
        if _lexists(backup) and digest(backup.read_bytes()) != old_hash:
            raise ValueError('Corrupt import transaction backup: %s' % backup)
        if _lexists(stage) and digest(stage.read_bytes()) != new_hash:
            raise ValueError('Corrupt import transaction staging file: %s' % stage)
        checked.append((entry, target, stage, backup))
    return marker, txn, checked


def recover_import(directory):
    """Safely roll back a pending picture pair transaction."""
    directory = Path(directory)
    _reject_symlink(directory, 'Workspace')
    root = directory.resolve()
    marker = root / IMPORT_PENDING
    txn_root = root / IMPORT_TXN_DIR
    if not _lexists(marker):
        if _lexists(txn_root):
            raise ValueError('Import transaction directory has no marker; refusing recovery')
        return {'operation': 'recover-import', 'restored': [], 'workspace': str(root)}
    marker, txn, checked = _validate_marker(root)
    # Validate every current target before touching any of them. This guards
    # recovery against third-party modifications without partial rollback.
    to_restore = []
    for entry, target, stage, backup in checked:
        current = digest(target.read_bytes())
        old_hash, new_hash = entry['original_sha256'], entry['new_sha256']
        if current == old_hash:
            # A previous recovery may already have restored this target. A
            # consumed backup is safe only in this state; any new target still
            # needs an intact backup below.
            continue
        if current != new_hash:
            raise RuntimeError('Refusing recovery: target changed outside this transaction: %s' % target)
        if not _lexists(backup):
            raise RuntimeError('Cannot recover published target; backup is missing: %s' % backup)
        to_restore.append((entry, target, backup))
    restored = []
    try:
        for index, (entry, target, backup) in enumerate(to_restore):
            _assert_target_hash(target, entry['new_sha256'], 'Pending import target')
            _assert_target_hash(backup, entry['original_sha256'], 'Pending import backup')
            # Do not consume the original backup. A fresh staged copy makes a
            # failed second restore retryable and keeps the marker idempotent.
            restore_stage = txn / ('restore-%d-%s' % (index, uuid.uuid4().hex))
            _write_exclusive(restore_stage, backup.read_bytes())
            _assert_target_hash(restore_stage, entry['original_sha256'], 'Recovery staging file')
            os.replace(restore_stage, target)
            restored.append(str(target.relative_to(root)))
        _remove_transaction(root, txn, marker)
    except BaseException:
        # Already restored targets are recognized as old on the next attempt.
        raise
    return {'operation': 'recover-import', 'restored': restored, 'workspace': str(root)}


def unpack(source, destination, pages=True):
    """Write files/ + manifest.json (+ legacy pages/*.json when `pages`). The CLI defaults to no legacy pages."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise FileExistsError('Workspace already exists: ' + str(destination))
    data = source.read_bytes()
    files, stale = H.read_container(data, with_stale=True)
    destination.mkdir(parents=True)
    (destination / 'files').mkdir()
    if pages:
        (destination / 'pages').mkdir()
    records = []
    for name, b in files.items():
        safe_name(name)
        (destination / 'files' / name).write_bytes(b)
        rec = {'name': name, 'sha256': digest(b), 'size': len(b)}
        if pages and name.endswith('.pa'):
            page = editable_page(b)
            # Internal numeric file name remains stable even if the page name changes.
            path = 'pages/' + name[:-3] + '.json'
            (destination / path).write_bytes(json_bytes(page))
            rec.update(page=path, page_sha256=digest(canonical(page).encode('utf8')))
        records.append(rec)
    manifest = {'format': FORMAT, 'source': source.name, 'source_sha256': digest(data), 'files': records,
                'discarded_stale_bytes': sum(len(c) for chunks in stale.values() for c in chunks)}
    (destination / 'manifest.json').write_bytes(json_bytes(manifest))
    return manifest


def load_workspace(directory):
    # Use the same component-by-component checks for ordinary pack/load as for
    # imports.  Otherwise a manifest or raw/page path symlink could make pack
    # silently embed bytes from outside the workspace.
    _ensure_no_pending(directory)
    directory, manifest = _read_import_manifest(directory)
    files, changed = {}, []
    for rec in manifest['files']:
        name = safe_name(rec['name'])
        if name in files:
            raise ValueError('Duplicate internal file: ' + name)
        raw_path = _safe_workspace_path(directory, Path('files') / name, 'Raw file ' + name)
        b = raw_path.read_bytes()
        if 'page' in rec:
            page_path = _safe_workspace_path(directory, rec['page'], 'Page JSON for ' + name)
            edit = json.loads(page_path.read_text(encoding='utf8'))
            edited = digest(canonical(edit).encode('utf8')) != rec['page_sha256']
            if edited:
                if digest(b) != rec['sha256']:
                    raise ValueError('%s changed both as raw bytes and page JSON; choose one representation' % name)
                b = encode_page(b, edit)
        files[name] = b
        if digest(b) != rec['sha256']:
            changed.append(name)
    expected = set(files)
    actual = {p.name for p in (directory / 'files').iterdir() if p.is_file()}
    if actual != expected:
        raise ValueError('Raw file list differs from manifest: %r' % (actual ^ expected))
    return files, changed


def win_path(path):
    return 'Z:' + str(Path(path).resolve()).replace('/', '\\')


def signature_neutral(name, b):
    """Only bytes rewritten by HmiSafe. The body and reserved bytes must remain identical."""
    b = bytearray(b)
    if name.endswith('.pa'):
        b[:8] = bytes(8)         # checksum and size
        b[21:23] = bytes(2)      # file identification/version
        b[40:43] = bytes(3)      # writer version
    elif name == 'main.HMI':
        b[:4] = bytes(4)
        b[8:10] = bytes(2)
        b[36] = 0
    return bytes(b)


def pack(directory, output, agent_dir=None, prefix=None, timeout=180, backend='python'):
    """Build locally by default; the editor backend is an explicitly selected development oracle."""
    if backend == 'editor':
        return pack_editor(directory, output, agent_dir or ROOT / '.state/packer-agent', prefix, timeout)
    if backend != 'python':
        raise ValueError('Unknown pack backend: ' + str(backend))
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Output already exists: ' + str(output))
    files, changed = load_workspace(directory)
    return publish(files, changed, output)


def publish(files, changed, output):
    """Check, sign, assemble and verify a complete file map, then create `output` exclusively.

    `changed` names the files that were edited or created; only those may have their checksums repaired."""
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Output already exists: ' + str(output))
    problems = H.check(files)
    if problems:
        raise ValueError('Invalid workspace:\n' + '\n'.join(problems))
    signed, repaired = {}, []
    for name, data in files.items():
        result = I.sign_page(data) if name.endswith('.pa') else I.sign_index(data) if name == 'main.HMI' else data
        if result != data:
            if name not in changed:
                raise ValueError('Invalid checksum in an unedited workspace file: ' + name)
            if signature_neutral(name, result) != signature_neutral(name, data):
                raise RuntimeError('Unexpected change beyond the checksum: ' + name)
            repaired.append(name)
        signed[name] = result
    assembled = I.build_container(signed)
    problems = I.container_errors(assembled, require_exact_body=True)
    if problems:
        raise RuntimeError('Generated container failed its integrity check:\n' + '\n'.join(problems))
    rebuilt = H.read_container(assembled)
    if list(rebuilt) != list(files):
        raise RuntimeError('Writer changed the internal file list/order')
    for name, data in rebuilt.items():
        if data != signed[name]:
            raise RuntimeError('Writer changed an internal file: ' + name)
        if (name.endswith('.pa') and not I.verify_page(data)) or (name == 'main.HMI' and not I.verify_index(data)):
            raise RuntimeError('Generated file failed its checksum check: ' + name)
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with output.open('xb') as f:
            created = True
            f.write(assembled)
    except BaseException:
        if created:
            try:
                output.unlink()
            except OSError:
                pass
        raise
    return {'output': str(output), 'bytes': len(assembled), 'files': len(files), 'changed_files': list(changed),
            'signed_files': repaired, 'backend': 'python'}


def pack_editor(directory, output, agent_dir, prefix=None, timeout=180):
    output, agent_dir = Path(output).resolve(), Path(agent_dir).resolve()
    if output.exists():
        raise FileExistsError('Output already exists: ' + str(output))
    if not agent_dir.is_dir():
        raise ValueError('Start the editor once: AGENT_DIR=%s ./run.sh' % agent_dir)
    files, changed = load_workspace(directory)
    problems = H.check(files)
    if problems:
        raise ValueError('Invalid workspace:\n' + '\n'.join(problems))
    request = 'pack_' + uuid.uuid4().hex
    job = agent_dir / request
    (job / 'files').mkdir(parents=True)
    (job / 'names.txt').write_text('\n'.join(files), encoding='utf8')
    for name, b in files.items():
        (job / 'files' / name).write_bytes(b)
    env = {**os.environ, 'WINEPREFIX': str(Path(prefix or ROOT / 'pfx').resolve()), 'WINEDEBUG': '-all'}
    # csc derives the assembly name from the output basename. Publish the DLL atomically so the agent
    # never tries to load a half-written binary; the request folder matches that assembly name.
    assembly_file = job / (request + '.pending')
    command = ['wine', r'C:\windows\Microsoft.NET\Framework\v4.0.30319\csc.exe', '/nologo', '/unsafe',
               '/target:library', '/out:' + win_path(assembly_file), win_path(ROOT / 'agent_plugins' / 'hmi_build.cs')]
    proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 or not assembly_file.exists():
        raise RuntimeError('Builder compilation failed:\n' + proc.stdout + proc.stderr)
    os.replace(assembly_file, agent_dir / (request + '.dll'))
    result = agent_dir / (request + '.out')
    deadline = time.monotonic() + timeout
    while not result.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('No reply from editor agent. Request retained at %s. Start with AGENT_DIR=%s ./run.sh' % (job, agent_dir))
        time.sleep(.2)
    report = result.read_text(encoding='utf8')
    if not report.startswith('OK\n'):
        raise RuntimeError('Editor rejected the build:\n' + report)
    assembled = (job / 'out.HMI').read_bytes()
    rebuilt = H.read_container(assembled)
    if list(rebuilt) != list(files):
        raise RuntimeError('Editor changed the internal file list/order')
    for name, b in files.items():
        if rebuilt[name] != b:
            if name not in changed:
                raise RuntimeError('Editor changed an unedited internal file: ' + name)
            if signature_neutral(name, rebuilt[name]) != signature_neutral(name, b):
                raise RuntimeError('Unexpected editor change beyond the signature: ' + name)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents racing another process that creates output during the build.
    with output.open('xb') as f:
        f.write(assembled)
    return {'output': str(output), 'bytes': len(assembled), 'files': len(files), 'changed_files': changed,
            'editor_report': report, 'request': str(job), 'backend': 'editor'}


def _arg_nonnegative(value):
    try:
        result = int(value, 10)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError('must be a non-negative integer') from e
    if result < 0:
        raise argparse.ArgumentTypeError('must be a non-negative integer')
    return result


def _config():
    return importlib.import_module('hmi_config')


def _summarize(report, files, limit=30):
    out = {'files': files}
    for k, v in report.items():
        if isinstance(v, list) and len(v) > limit:
            out[k] = v[:limit] + ['... %d more' % (len(v) - limit)]
        else:
            out[k] = v
    return out


def pack_config(directory, output, config):
    """Compile a structural config in memory and build the .HMI (standard library only unless new sources are used)."""
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Output already exists: ' + str(output))
    cand = _config().compile_project(directory, config)
    result = publish(cand.files, cand.changed, output)
    result.update(_summarize(cand.report, len(cand.files)))
    result['changed_files'] = result['changed_files'][:30]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    u = sub.add_parser('unpack')
    u.add_argument('source')
    u.add_argument('directory')
    u.add_argument('--portable', action='store_true',
                   help='self-contained source-only project (PNG pictures, JSON pages, no original files) for Git')
    u.add_argument('--legacy-pages', action='store_true',
                   help='also write pages/*.json (in-place edits only, packed without --config); default: project.json + config/')
    p = sub.add_parser('pack')
    p.add_argument('directory')
    p.add_argument('output')
    p.add_argument('--backend', choices=('python', 'editor'), default='python',
                   help='python: standalone packing (default); editor: optional development oracle')
    p.add_argument('--agent-dir', default=str(ROOT / '.state' / 'packer-agent'), help='editor backend only')
    p.add_argument('--prefix', help='Wine prefix; editor backend only')
    p.add_argument('--timeout', type=float, default=180, help='agent timeout; editor backend only')
    p.add_argument('--config', help='structural project config (usart-hmi-project-v2), relative to the workspace')
    p.add_argument('--legacy', action='store_true',
                   help='ignore an existing project.json and pack the plain workspace files')
    ex = sub.add_parser('export-config', help='write project.json + pages/*.json for structural editing')
    ex.add_argument('directory')
    ex.add_argument('--update-snapshot', action='store_true',
                    help='keep project.json, only re-pin it to the workspace files as they are now')
    va = sub.add_parser('validate', help='compile a structural config in memory and report, without writing a .HMI')
    va.add_argument('directory')
    va.add_argument('--config', default='project.json')
    ip = sub.add_parser('import-picture')
    ip.add_argument('directory')
    ip.add_argument('picture_id', type=_arg_nonnegative)
    ip.add_argument('source')
    ip.add_argument('--replace', action='store_true')
    inf = sub.add_parser('import-font')
    inf.add_argument('directory')
    inf.add_argument('font_id', type=_arg_nonnegative)
    inf.add_argument('source')
    chars = inf.add_mutually_exclusive_group()
    chars.add_argument('--chars')
    chars.add_argument('--chars-file')
    inf.add_argument('--height', type=int)
    inf.add_argument('--bpp', type=int, choices=(1, 3))
    inf.add_argument('--size', type=int)
    inf.add_argument('--font-index', type=_arg_nonnegative, default=0)
    inf.add_argument('--replace', action='store_true')
    ri = sub.add_parser('recover-import')
    ri.add_argument('directory')
    args = parser.parse_args()
    try:
        if args.action == 'unpack' and args.portable:
            print(json.dumps(importlib.import_module('hmi_portable').export_portable(args.source, args.directory),
                             ensure_ascii=False, indent=2))
        elif args.action == 'unpack':
            m = unpack(args.source, args.directory, pages=args.legacy_pages)
            print('Unpacked %d files; discarded %d stale bytes -> %s' % (len(m['files']), m['discarded_stale_bytes'], args.directory))
            if not args.legacy_pages:
                print('Editable: %s/project.json and %s/pages/*.json' % (args.directory, args.directory))
                _config().export_config(args.directory)
        elif args.action == 'pack':
            if args.config:
                if args.backend != 'python':
                    raise ValueError('--config builds only with the python backend')
                result = pack_config(args.directory, args.output, args.config)
            else:
                if (Path(args.directory) / 'project.json').exists() and not args.legacy:
                    raise ValueError('%s/project.json exists: pack it with `--config project.json`, or use --legacy to '
                                     'ignore it and pack the plain workspace files' % args.directory)
                result = pack(args.directory, args.output, args.agent_dir, args.prefix, args.timeout, args.backend)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.action == 'export-config':
            cfg = _config()
            result = cfg.update_snapshot(args.directory) if args.update_snapshot else cfg.export_config(args.directory)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.action == 'validate':
            cand = _config().compile_project(args.directory, args.config)
            print(json.dumps(_summarize(cand.report, len(cand.files)), ensure_ascii=False, indent=2))
        elif args.action == 'import-picture':
            print(json.dumps(import_picture(args.directory, args.picture_id, args.source, args.replace),
                             ensure_ascii=False, indent=2))
        elif args.action == 'import-font':
            font_chars = _parse_chars_file(args.chars_file) if args.chars_file else args.chars
            print(json.dumps(import_font(args.directory, args.font_id, args.source, font_chars,
                                         args.height, args.bpp, args.size, args.font_index, args.replace),
                             ensure_ascii=False, indent=2))
        elif args.action == 'recover-import':
            print(json.dumps(recover_import(args.directory), ensure_ascii=False, indent=2))
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as e:
        parser.exit(1, 'Error: %s\n' % e)


if __name__ == '__main__':
    main()
