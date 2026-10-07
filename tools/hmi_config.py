#!/usr/bin/env python3
"""Declarative project config (usart-hmi-project-v2): add, edit, delete and reorder pages, components and pictures.

python3 hmi_project.py export-config WORKSPACE          writes WORKSPACE/project.json + pages/*.json
python3 hmi_project.py validate WORKSPACE --config project.json
python3 hmi_project.py pack WORKSPACE OUT.HMI --config project.json

The config lists the *desired* ordered pages, pictures and fonts; an entry that is missing is deleted, a new one is
created, the order is the runtime id order. `origin` ties an entry to the workspace it was exported from and keeps every
stored byte the JSON does not show (attribute widths, unknown records, table words, trailers). Numbers inside exported
attributes and code are *baseline* ids; they are remapped once to the final ids. New references are written
symbolically: {"$ref": "picture:logo"} in attributes, ${page:key} in code. See README.md for the full contract.

Nothing here runs Wine, the editor or any other process. Pillow/fontTools are only imported when a picture/font
`source` is requested.
"""
import hashlib
import json
import re
import shutil
import struct
import uuid
from pathlib import Path

import hmi_factory as F
import hmi_image as IMG
import hmi_integrity as I
import hmi_model as M
import hmi_parse as H
import hmi_project as P
import hmi_refs as R

FORMAT = 'usart-hmi-project-v2'
CONFIG_NAME = 'project.json'
PAGES_DIR = 'pages'
NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
KEY_RE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.\-]*')
MANAGED = ('i', 'zi', 'pa')


class ConfigError(ValueError):
    pass


# ------------------------------------------------------------------ strict JSON helpers

def load_json(path, label='JSON file'):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise ConfigError('%s has the duplicate key %r' % (label, k))
            d[k] = v
        return d
    try:
        return json.loads(Path(path).read_text(encoding='utf8'), object_pairs_hook=pairs)
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError('Cannot read %s: %s' % (label, e)) from e
    except ValueError as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError('%s is not valid JSON: %s' % (label, e)) from e


def _dict(value, allowed, where, required=()):
    if not isinstance(value, dict):
        raise ConfigError('%s must be an object' % where)
    extra = set(value) - set(allowed)
    if extra:
        raise ConfigError('%s has unknown field(s): %s' % (where, ', '.join(sorted(extra))))
    for k in required:
        if k not in value:
            raise ConfigError('%s needs "%s"' % (where, k))
    return value


def _list(value, where):
    if not isinstance(value, list):
        raise ConfigError('%s must be a list' % where)
    return value


def _map(value, where):
    if not isinstance(value, dict):
        raise ConfigError('%s must be an object' % where)
    return value


def _key(value, where):
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise ConfigError('%s must be a key of letters, digits, _ . - (got %r)' % (where, value))
    return value


# ------------------------------------------------------------------ baseline

class Baseline:
    """The effective workspace (raw files + legacy page JSON edits + imported resources) in parsed form."""

    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.files, _ = P.load_workspace(directory)
        self.order = list(self.files)
        if 'main.HMI' not in self.files:
            raise ConfigError('The workspace has no main.HMI')
        self.index = M.parse_index(self.files['main.HMI'])
        pairs = self.index.pairs()
        self.page_files = [n for k, n in pairs if k == 'pa']
        self.pic_files = [n for k, n in pairs if k == 'i']
        self.font_files = [n for k, n in pairs if k == 'zi']
        listed = self.page_files + self.pic_files + self.font_files
        for n in listed:
            if n not in self.files:
                raise ConfigError('The index lists %s which is not in the workspace' % n)
        if len(set(listed)) != len(listed):
            raise ConfigError('The index lists the same resource file twice; structural editing needs unique entries')
        self.managed = set(listed) | {self.source_of(n) for n in self.pic_files}
        self.pages = {n: M.parse_page(self.files[n]) for n in self.page_files}
        self.page_id = {n: i for i, n in enumerate(self.page_files)}
        self.pic_id = {n: i for i, n in enumerate(self.pic_files)}
        self.font_id = {n: i for i, n in enumerate(self.font_files)}
        h = hashlib.sha256()
        for n in self.order:
            h.update(n.encode('latin1') + b'\0' + hashlib.sha256(self.files[n]).digest())
        self.snapshot = h.hexdigest()

    def source_of(self, picture_file):
        return picture_file[:-1] + 'is'


# ------------------------------------------------------------------ export

def _line_json(raw):
    try:
        return raw.decode('utf8')
    except UnicodeDecodeError:
        return {'hex': raw.hex()}


def _events_json(obj):
    return {e.label: [_line_json(x) for x in e.lines] for e in obj.events}


def _attrs_json(obj, skip):
    return {a.name: M.decode_value(a.name, a.value) for a in obj.attrs if a.name not in skip}


def _type_json(code):
    return H.TYPES.get(code, code)


def _source_png(source):
    """The PNG stored in a picture's .is wrapper, or None when there is none/it is not a plain PNG wrapper."""
    if source is None:
        return None
    try:
        IMG.validate_source(source)
    except ValueError:
        return None
    return bytes(source[27:])


def describe_page(page, file):
    """The editable JSON view of one baseline page (also the `raw` content mode)."""
    root = page.objects[0]
    used, objects = set(), []
    for i, o in enumerate(page.objects[1:], 1):
        name = o.get('objname').value.decode('utf8', 'replace') if o.get('objname') else 'obj%d' % i
        key = re.sub(r'[^A-Za-z0-9_.\-]', '_', name) or 'obj%d' % i
        base, n = key, 1
        while key in used:
            n += 1
            key = '%s_%d' % (base, n)
        used.add(key)
        objects.append({'key': key, 'origin': {'file': file, 'index': i}, 'type': _type_json(o.get('type').value[0]),
                        'attributes': _attrs_json(o, ('type', 'id')), 'events': _events_json(o)})
    return {'name': page.name, 'root': {'attributes': _attrs_json(root, ('type', 'id', 'objname')),
                                        'events': _events_json(root)}, 'objects': objects}


def export_config(directory):
    """Write project.json and pages/*.json describing the current effective workspace."""
    base = Baseline(directory)
    out = base.root / CONFIG_NAME
    pages_dir = base.root / PAGES_DIR
    for p in (out, pages_dir, base.root / 'pictures'):
        if P._lexists(p):
            hint = ' (legacy page JSON from `unpack --legacy-pages`; unpack without it to use project.json)' if p == pages_dir else ''
            raise FileExistsError('%s already exists%s; move it away to export again' % (p, hint))
    used = set()

    def unique(raw, fallback):
        key = re.sub(r'[^A-Za-z0-9_.\-]', '_', raw) or fallback
        base_key, n = key, 1
        while key in used:
            n += 1
            key = '%s_%d' % (base_key, n)
        used.add(key)
        return key

    pages, written = [], []
    for f in base.page_files:
        key = unique(base.pages[f].name, f)
        path = '%s/%s.json' % (PAGES_DIR, key)
        written.append((path, describe_page(base.pages[f], f)))
        pages.append({'key': key, 'origin': {'file': f}, 'content': {'mode': 'json', 'path': path}})
    used.clear()
    pictures, pngs = [], []
    for f in base.pic_files:
        entry = {'key': unique('pic_' + f[:-2], f), 'origin': {'file': f}}
        png = _source_png(base.files.get(base.source_of(f)))
        if png is not None:
            rel = 'pictures/%s.png' % entry['key']
            entry['source'] = {'png': rel}
            pngs.append((rel, png))
        pictures.append(entry)
    used.clear()
    fonts = [{'key': unique('font_' + f[:-3], f), 'origin': {'file': f}} for f in base.font_files]
    config = {'format': FORMAT, 'baseline': {'snapshot': base.snapshot}, 'id_policy': 'preserve', 'acknowledge': {},
              'pages': pages, 'pictures': pictures, 'fonts': fonts}
    try:
        (base.root / 'pictures').mkdir()
        for rel, png in pngs:
            with (base.root / rel).open('xb') as f:
                f.write(png)
        pages_dir.mkdir()
        for path, content in written:
            with (base.root / path).open('xb') as f:
                f.write(P.json_bytes(content))
        with out.open('xb') as f:
            f.write(P.json_bytes(config))
    except BaseException:
        for d in (pages_dir, base.root / 'pictures'):                  # only what this call created
            shutil.rmtree(d, ignore_errors=True)
        if out.exists() and not out.is_symlink():
            out.unlink()
        raise
    return {'config': str(out), 'pages': len(pages), 'pictures': len(pictures), 'fonts': len(fonts),
            'snapshot': base.snapshot}


def update_snapshot(directory):
    """Re-pin project.json to the current workspace after raw files were edited on purpose.

    Only the snapshot changes; origins keep meaning "the entity stored in this workspace file", so use it when the edits
    did not move resources or components."""
    base = Baseline(directory)
    path = P._safe_workspace_path(base.root, CONFIG_NAME, 'Config')
    cfg = load_json(path, 'config')
    old = cfg.setdefault('baseline', {}).get('snapshot')
    cfg['baseline']['snapshot'] = base.snapshot
    tmp = path.with_name('%s.%s.tmp' % (CONFIG_NAME, uuid.uuid4().hex))
    try:
        with tmp.open('xb') as f:                      # exclusive: never writes through a pre-made link
            f.write(P.json_bytes(cfg))
        tmp.replace(path)
    except BaseException:
        if tmp.exists() and not tmp.is_symlink():
            tmp.unlink()
        raise
    return {'config': str(path), 'snapshot': base.snapshot, 'changed': old != base.snapshot}


# ------------------------------------------------------------------ spec loading

def _event_lines(value, where):
    if not isinstance(value, list):
        raise ConfigError('%s must be a list of code lines' % where)
    out = []
    for line in value:
        if isinstance(line, dict) and set(line) == {'text'} and isinstance(line['text'], str):
            line = line['text']
        if isinstance(line, str):
            out.append(line.encode('utf8'))
        elif isinstance(line, dict) and set(line) == {'hex'}:
            try:
                out.append(bytes.fromhex(line['hex']))
            except (ValueError, TypeError) as e:
                raise ConfigError('%s has an invalid hex line' % where) from e
        else:
            raise ConfigError('%s: a code line is a string, {"text": ...} or {"hex": ...}' % where)
    return out


def _content(entry, base, where):
    mode = _dict(entry.get('content'), ('mode', 'path', 'name', 'root', 'objects'), where + '.content', ('mode',))['mode']
    if mode == 'raw':
        origin = entry.get('origin')
        if not origin:
            raise ConfigError('%s: mode "raw" needs an origin' % where)
        return describe_page(base.pages[origin['file']], origin['file'])
    if mode == 'json':
        c = entry['content']
        if 'path' not in c:
            raise ConfigError('%s: mode "json" needs "path"' % where)
        rel = c['path']
        path = P._safe_workspace_path(base.root, rel, 'Page content ' + str(rel))
        return load_json(path, str(rel))
    if mode == 'inline':
        c = entry['content']
        return {k: c[k] for k in ('name', 'root', 'objects') if k in c}
    raise ConfigError('%s: content mode must be raw, json or inline' % where)


def load_config(base, config_path):
    cfg = load_json(config_path, 'config')
    _dict(cfg, ('format', 'baseline', 'id_policy', 'acknowledge', 'pages', 'pictures', 'fonts'), 'config',
          ('format', 'pages', 'pictures', 'fonts'))
    if cfg['format'] != FORMAT:
        raise ConfigError('Unsupported config format %r (expected %s)' % (cfg['format'], FORMAT))
    snap = _dict(cfg.get('baseline', {}), ('snapshot',), 'baseline').get('snapshot')
    if snap is not None and snap != base.snapshot:
        raise ConfigError('The workspace changed since this config was exported (snapshot mismatch); '
                          'run export-config on the current workspace or re-unpack the project')
    if cfg.get('id_policy', 'preserve') not in ('preserve', 'remap'):
        raise ConfigError('id_policy must be "preserve" or "remap"')
    ack = _dict(cfg.get('acknowledge', {}), ('dynamic', 'external_ids'), 'acknowledge')
    for lst in ('pages', 'pictures', 'fonts'):
        if not isinstance(cfg[lst], list):
            raise ConfigError('"%s" must be a list' % lst)
    return cfg, ack


def _origin_file(entry, valid, where, label):
    o = entry.get('origin')
    if o is None:
        return None
    _dict(o, ('file',), where + '.origin', ('file',))
    if not isinstance(o['file'], str) or o['file'] not in valid:
        raise ConfigError('%s.origin.file %r is not a %s of the workspace' % (where, o['file'], label))
    return o['file']


def _page_specs(cfg, base):
    specs, keys = [], set()
    for n, entry in enumerate(cfg['pages']):
        where = 'pages[%d]' % n
        _dict(entry, ('key', 'origin', 'content'), where, ('key', 'content'))
        key = _key(entry['key'], where + '.key')
        if key in keys:
            raise ConfigError('Duplicate page key %r' % key)
        keys.add(key)
        origin = _origin_file(entry, base.page_files, where, 'page')
        c = _content(entry, base, where)
        _dict(c, ('name', 'root', 'objects'), where + ' content', ('name',))
        name = c['name']
        if not isinstance(name, str) or not name or len(name.encode('utf8')) > 16 or not NAME_RE.fullmatch(name):
            if origin is None or name != base.pages[origin].name:
                raise ConfigError('%s: page name must be a 1..16 byte identifier (got %r)' % (where, name))
        root = _dict(c.get('root', {}), ('attributes', 'events'), where + '.root')
        objects, okeys = [], set()
        for m, o in enumerate(_list(c.get('objects', []), where + '.objects')):
            w = '%s.objects[%d]' % (where, m)
            _dict(o, ('key', 'origin', 'type', 'attributes', 'events'), w, ('key', 'type'))
            if isinstance(o['type'], bool) or not isinstance(o['type'], (str, int)):
                raise ConfigError('%s.type must be a type name or number' % w)
            ok = _key(o['key'], w + '.key')
            if ok in okeys:
                raise ConfigError('%s: duplicate object key %r on page %r' % (w, ok, key))
            okeys.add(ok)
            oo = None
            if o.get('origin') is not None:
                _dict(o['origin'], ('file', 'index'), w + '.origin', ('file', 'index'))
                f, i = o['origin']['file'], o['origin']['index']
                if not isinstance(f, str) or f not in base.pages or isinstance(i, bool) or not isinstance(i, int) or not 1 <= i < len(base.pages[f].objects):
                    raise ConfigError('%s.origin does not name a component of the workspace' % w)
                oo = (f, i)
            objects.append({'key': ok, 'origin': oo, 'type': o['type'], 'attributes': dict(_map(o.get('attributes', {}), w + '.attributes')),
                            'events': dict(_map(o.get('events', {}), w + '.events'))})
        if name in {sp['name'] for sp in specs}:
            raise ConfigError('%s: duplicate page name %r' % (where, name))
        if len(objects) + 1 > M.MAX_OBJECTS:
            raise ConfigError('%s: a page holds at most %d components besides the page object' % (where, M.MAX_OBJECTS - 1))
        specs.append({'key': key, 'origin': origin, 'name': name,
                      'root_attrs': dict(_map(root.get('attributes', {}), where + '.root.attributes')),
                      'root_events': dict(_map(root.get('events', {}), where + '.root.events')), 'objects': objects, 'where': where})
    if not 1 <= len(specs) <= 255:
        raise ConfigError('A project needs 1..255 pages')
    return specs


# ------------------------------------------------------------------ maps

def _name_text(value, where='objname'):
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {'hex'} and isinstance(value['hex'], str):
        try:
            return bytes.fromhex(value['hex']).decode('utf8', 'replace')
        except ValueError:
            pass
    raise ConfigError('%s must be text or {"hex": ...}' % where)


def _obj_name(spec_obj, base):
    attrs = spec_obj['attributes']
    if 'objname' in attrs:
        return _name_text(attrs['objname'])
    if spec_obj['origin']:
        f, i = spec_obj['origin']
        return base.pages[f].objects[i].get('objname').value.decode('utf8', 'replace')
    return None


def _build_maps(base, pages, pictures, fonts):
    page_map = {i: None for i in range(len(base.page_files))}
    comp_map, comp_names_final = {}, {}
    for final_id, ps in enumerate(pages):
        f = ps['origin']
        if f is None:
            continue
        bid = base.page_id[f]
        if page_map[bid] is not None:
            continue                     # a copy of an already placed page
        page_map[bid] = final_id
        cm = {0: 0}
        names = {ps['name']}
        for n, so in enumerate(ps['objects'], 1):
            if so['origin'] and so['origin'][0] == f:
                cm.setdefault(so['origin'][1], n)
            nm = _obj_name(so, base)
            if nm:
                names.add(nm)
        for i in range(len(base.pages[f].objects)):
            cm.setdefault(i, None)
        comp_map[bid] = cm
        comp_names_final[bid] = names
    pic_map = {i: None for i in range(len(base.pic_files))}
    for final_id, e in enumerate(pictures):
        if e['origin'] and pic_map[base.pic_id[e['origin']]] is None:
            pic_map[base.pic_id[e['origin']]] = final_id
    font_map = {i: None for i in range(len(base.font_files))}
    for final_id, e in enumerate(fonts):
        if e['origin'] and font_map[base.font_id[e['origin']]] is None:
            font_map[base.font_id[e['origin']]] = final_id
    return page_map, comp_map, comp_names_final, pic_map, font_map


def _comp_base_names(base):
    out = {}
    for f, page in base.pages.items():
        names = {0: page.name}
        for i, o in enumerate(page.objects[1:], 1):
            if o.get('objname'):
                names[i] = o.get('objname').value.decode('utf8', 'replace')
        out[base.page_id[f]] = names
    return out


def _check_policy(policy, base, page_map, comp_map, pic_map, font_map):
    problems = []
    for label, mapping in (('page', page_map), ('picture', pic_map), ('font', font_map)):
        for old, new in mapping.items():
            if new is not None and new != old:
                problems.append('%s id %d would become %d' % (label, old, new))
    for pid, cm in comp_map.items():
        for old, new in cm.items():
            if new is not None and new != old:
                problems.append('component %d of page id %d would become %d' % (old, pid, new))
    if problems and policy != 'remap':
        raise ConfigError('The edit shifts the ids of surviving entities (%s%s).\n'
                          'Append new entries at the end, or set "id_policy": "remap" to renumber them (references '
                          'are then rewritten and checked).' % ('; '.join(problems[:6]), ' ...' if len(problems) > 6 else ''))
    return problems


# ------------------------------------------------------------------ building pages

def copy_obj(o):
    return M.Obj([M.Attr(None, a.value, a.field) for a in o.attrs], [M.Event(None, e.lines, e.header) for e in o.events],
                 o.tail, o.extra, o.count_header)


def _type_any(value, where):
    if isinstance(value, bool):
        raise ConfigError('%s: invalid type %r' % (where, value))
    if isinstance(value, int):
        return value
    rev = {v: k for k, v in H.TYPES.items()}
    if value in rev:
        return rev[value]
    raise ConfigError('%s: unknown component type %r' % (where, value))


def _sha(text):
    return hashlib.sha256(text.encode('utf8', 'surrogateescape')).hexdigest()


class Compiler:
    def __init__(self, base, cfg, ack):
        self.base, self.cfg, self.ack = base, cfg, ack
        self.policy = cfg.get('id_policy', 'preserve')
        self.findings = []            # (site, text, Finding)
        self.rewritten = 0
        self.pages = _page_specs(cfg, base)
        self.pictures = self._resource_specs('pictures', base.pic_files, 'picture')
        self.fonts = self._resource_specs('fonts', base.font_files, 'font')
        self.page_ids = {p['key']: i for i, p in enumerate(self.pages)}
        self.pic_ids = {e['key']: i for i, e in enumerate(self.pictures)}
        self.font_ids = {e['key']: i for i, e in enumerate(self.fonts)}
        if len(self.pictures) > 65535 or len(self.fonts) > 256:
            raise ConfigError('Too many pictures (max 65535) or fonts (max 256)')
        self.obj_ids = {p['key']: {o['key']: n for n, o in enumerate(p['objects'], 1)} for p in self.pages}
        self._per_page_maps()
        (self.page_map, self.comp_map, self.comp_names_final, self.pic_map,
         self.font_map) = _build_maps(base, self.pages, self.pictures, self.fonts)
        self._check_unmodelled_and_policy()
        self.page_size = self._page_size()
        self.maps_obj = self.maps()

    # -- specs
    def _resource_specs(self, section, valid, label):
        specs, keys = [], set()
        for n, e in enumerate(self.cfg[section]):
            where = '%s[%d]' % (section, n)
            _dict(e, ('key', 'origin', 'source'), where, ('key',))
            key = _key(e['key'], where + '.key')
            if key in keys:
                raise ConfigError('Duplicate %s key %r' % (label, key))
            keys.add(key)
            origin = _origin_file(e, valid, where, label)
            source = e.get('source')
            if origin is None and source is None:
                raise ConfigError('%s needs an origin or a source' % where)
            specs.append({'key': key, 'origin': origin, 'source': source, 'where': where})
        return specs

    def _per_page_maps(self):
        # per output page: baseline component index -> final index and final names (used for that page's own code)
        for ps in self.pages:
            ps['names'] = {ps['name']} | {nm for nm in (_obj_name(o, self.base) for o in ps['objects']) if nm}
            ps['comp'] = {}
            if ps['origin']:
                ps['comp'] = {0: 0}
                for i in range(1, len(self.base.pages[ps['origin']].objects)):
                    ps['comp'][i] = None
                for n, so in enumerate(ps['objects'], 1):
                    if so['origin'] and so['origin'][0] == ps['origin'] and ps['comp'].get(so['origin'][1]) is None:
                        ps['comp'][so['origin'][1]] = n

    def _page_size(self):
        for f in self.base.page_files:
            root = self.base.pages[f].objects[0]
            if root.get('w') and root.get('h'):
                return (int.from_bytes(root.get('w').value, 'little'), int.from_bytes(root.get('h').value, 'little'))
        return None

    def _check_unmodelled_and_policy(self):
        problems = _check_policy(self.policy, self.base, self.page_map, self.comp_map, self.pic_map, self.font_map)
        self.id_changes = problems
        deleted_pics = any(v is None for v in self.pic_map.values())
        deleted_pages = any(v is None for v in self.page_map.values())
        if any(v != k for k, v in self.pic_map.items()) or any(v != k for k, v in self.page_map.items()) or deleted_pics or deleted_pages:
            for f, page in self.base.pages.items():
                for o in page.objects:
                    t = o.get('type').value[0]
                    if t in R.UNMODELLED_TYPES:
                        raise ConfigError('Page %s contains a %s component whose picture/page references are not modelled; '
                                          'renumbering or deleting pages/pictures is refused for this project' % (f, R.UNMODELLED_TYPES[t]))

    # -- reference expansion
    def resolve_placeholder(self, kind, key):
        try:
            if kind == 'page':
                return self.page_ids[key]
            if kind == 'pagename':
                return self.pages[self.page_ids[key]]['name']
            if kind == 'picture':
                return self.pic_ids[key]
            if kind == 'font':
                return self.font_ids[key]
            pk, _, ok = key.partition('/')
            if kind == 'obj':
                return 0 if ok in ('', '(page)') else self.obj_ids[pk][ok]
            if kind == 'objname':
                ps = self.pages[self.page_ids[pk]]
                if ok in ('', '(page)'):
                    return ps['name']
                return _obj_name(ps['objects'][self.obj_ids[pk][ok] - 1], self.base) or ok
        except KeyError as e:
            raise ConfigError('Unknown reference ${%s:%s}' % (kind, key)) from e
        raise ConfigError('Unknown reference kind ${%s:%s}' % (kind, key))

    def maps(self):
        base = self.base
        page_names_base = {base.page_id[f]: base.pages[f].name for f in base.page_files}
        glob = set()
        for ps in self.pages:
            for so in ps['objects']:
                if so['attributes'].get('vscope') == 1 or (so['origin'] and self._origin_attr(so, 'vscope') == 1):
                    nm = _obj_name(so, base)
                    if nm:
                        glob.add(nm)
        program = base.files.get('Program.s', b'').decode('utf8', 'surrogateescape')
        for mt in re.finditer(r'(?m)^[ \t]*int[ \t]+([^/\n]*)', program):
            for part in mt.group(1).split(','):
                nm = part.split('=')[0].strip()
                if nm:
                    glob.add(nm)
        return R.Maps(self.page_map, self.pic_map, self.font_map, self.comp_map, page_names_base,
                      [p['name'] for p in self.pages], _comp_base_names(base), self.comp_names_final, glob)

    def _origin_attr(self, so, name):
        f, i = so['origin']
        a = self.base.pages[f].objects[i].get(name)
        return int.from_bytes(a.value, 'little') if a else None

    # -- attributes
    def _ref(self, value, domain, where):
        """Resolve an explicit reference object to a final id; None if `value` is not one."""
        if not isinstance(value, dict):
            return None
        label = {'pic': 'picture'}.get(domain, domain)
        if domain == 'anim':
            raise ConfigError('%s: animations cannot be referenced symbolically here (use a portable project)' % where)
        table = {'page': self.page_ids, 'pic': self.pic_ids, 'font': self.font_ids}[domain]
        if set(value) == {'$ref'}:
            kind, _, key = str(value['$ref']).partition(':')
            if kind != label:
                raise ConfigError('%s: this attribute takes a %s reference, not %r' % (where, label, value['$ref']))
            if key not in table:
                raise ConfigError('%s: unknown %s key %r' % (where, label, key))
            return table[key]
        if set(value) == {'$id', 'namespace'}:
            ns, v = value['namespace'], value['$id']
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError('%s: $id must be an integer' % where)
            if ns == 'final':
                return v
            if ns == 'baseline':
                mapping = {'page': self.page_map, 'pic': self.pic_map, 'font': self.font_map}[domain]
                if mapping.get(v, v) is None:
                    raise ConfigError('%s: %s %d was deleted' % (where, label, v))
                return mapping.get(v, v)
        return None

    def _map_value(self, domain, value, where):
        if domain == 'anim':
            return value                       # animations are not renumbered by this mode
        mapping = {'page': self.page_map, 'pic': self.pic_map, 'font': self.font_map}[domain]
        label = {'pic': 'picture'}.get(domain, domain)
        if domain == 'pic' and value == R.PIC_NONE or domain == 'page' and value == R.PAGE_NONE:
            return value
        if value in mapping:
            new = mapping[value]
            if new is None:
                raise ConfigError('%s refers to a deleted %s (id %d)' % (where, label, value))
            return new
        return value

    def _apply_overrides(self, obj, overrides, code, where):
        """Apply JSON attribute values onto a copied origin object (widths come from the stored records)."""
        explicit, changed = set(), set()
        for name, value in overrides.items():
            if name in ('type', 'id'):
                if name == 'type' and _type_any(value, where) != code:
                    raise ConfigError('%s: the component type cannot be changed' % where)
                continue
            cur = obj.get(name)
            if cur is None:
                raise ConfigError('%s: the component has no attribute %r (attributes cannot be added to an existing object)' % (where, name))
            dom = R.attr_domain(code, name)
            width = len(cur.value)
            if dom and not (isinstance(value, dict) and set(value) == {'hex'}):
                dname = dom[0]
                ref = self._ref(value, dname, '%s.%s' % (where, name))
                if ref is not None:
                    new = M.encode_value(name, ref, width)
                    if new != cur.value:
                        cur.value = new
                        changed.add(name)
                    explicit.add(name)
                    continue
                if isinstance(value, int) and not isinstance(value, bool):
                    if value == M.decode_value(name, cur.value):
                        continue         # unchanged baseline value: remapped below
                    if value != dom[1] and dname != 'anim' and self.maps_obj.changed(dname):
                        raise ConfigError('%s.%s: ids change in this edit, so a new %s must be written as '
                                          '{"$ref": "%s:key"}' % (where, name, dname, {'pic': 'picture'}.get(dname, dname)))
            raw = M.encode_value(name, value, width if name not in M.TEXT_ATTRS else None)
            if name == 'objname' and raw != cur.value and not (isinstance(value, str) and NAME_RE.fullmatch(value)):
                raise ConfigError('%s: invalid component name %r' % (where, value))
            if raw != cur.value:
                cur.value = raw
                changed.add(name)
            explicit.add(name)
        return explicit, changed

    def _remap_attrs(self, obj, code, explicit, where):
        for a in obj.attrs:
            dom = R.attr_domain(code, a.name)
            if not dom or a.name in explicit or len(a.value) not in (1, 2):
                continue
            old = int.from_bytes(a.value, 'little')
            new = self._map_value(dom[0], old, '%s.%s' % (where, a.name))
            if new != old:
                a.value = new.to_bytes(len(a.value), 'little')

    def _derive_geometry(self, obj, changed):
        for a_, b_, end in (('x', 'w', 'endx'), ('y', 'h', 'endy')):
            if end in changed or not (a_ in changed or b_ in changed):
                continue
            if obj.get(a_) and obj.get(b_) and obj.get(end):
                start = int.from_bytes(obj.get(a_).value, 'little', signed=True)
                size = int.from_bytes(obj.get(b_).value, 'little')
                obj.get(end).value = M.encode_value(end, start + size - 1, len(obj.get(end).value))

    def _fresh(self, so, ps, where, object_id):
        code = F.type_code(so['type'])
        values = {}
        for name, value in so['attributes'].items():
            dom = R.attr_domain(code, name)
            if dom and not (isinstance(value, dict) and set(value) == {'hex'}):
                ref = self._ref(value, dom[0], '%s.%s' % (where, name))
                if ref is not None:
                    value = ref
                elif isinstance(value, int) and not isinstance(value, bool) and value != dom[1] and dom[0] != 'anim' \
                        and self.maps_obj.changed(dom[0]):
                    raise ConfigError('%s.%s: ids change in this edit, so write {"$ref": "%s:key"}' % (
                        where, name, {'pic': 'picture'}.get(dom[0], dom[0])))
            if isinstance(value, dict):
                raise ConfigError('%s.%s: unsupported value %r' % (where, name, value))
            values[name] = value
        if code != 121 and not isinstance(values.get('objname'), str):
            raise ConfigError('%s needs a text "objname"' % where)
        if 'objname' in values and not NAME_RE.fullmatch(values['objname']):
            raise ConfigError('%s: invalid component name %r' % (where, values['objname']))
        try:
            return F.make_object(code, values, {k: _event_lines(v, where + '.events.' + k) for k, v in so['events'].items()},
                                 object_id, self.page_size)
        except M.ModelError as e:
            raise ConfigError('%s: %s' % (where, e)) from e

    # -- code
    def _line(self, raw, strict, page_base_id, own_comp, site, own_names=None):
        text = raw.decode('utf8', 'surrogateescape')
        rw = R.Rewriter(self.maps_obj, page_base_id, own_comp, strict, own_names)
        new = rw.run(text)
        for f in rw.findings:
            self.findings.append((site, text, f))
        if new != text:
            self.rewritten += 1
        new = R.expand(new, self.resolve_placeholder)
        return new.encode('utf8', 'surrogateescape')

    def _code(self, obj, base_lines, strict_all, page_base_id, own_comp, site_prefix, own_names=None):
        for ev in obj.events:
            for n, line in enumerate(ev.lines):
                strict = strict_all or line not in base_lines
                ev.lines[n] = self._line(line, strict, page_base_id, own_comp, '%s/%s#%d' % (site_prefix, ev.label, n + 1),
                                         own_names)

    # -- one page
    def build_page(self, ps):
        base, where = self.base, ps['where']
        origin = ps['origin']
        src_page = base.pages[origin] if origin else None
        header = bytes(src_page.header) if src_page else bytes(M.PAGE_HEADER_SIZE)
        page_base = base.page_id[origin] if origin else None
        own = ps['comp'] if origin else {}
        objs = []
        # root
        if origin:
            root = copy_obj(src_page.objects[0])
            explicit, changed = self._apply_overrides(root, {k: v for k, v in ps['root_attrs'].items() if k != 'objname'},
                                                      121, where + '.root')
            if ps['name'] != src_page.name:
                root.get('objname').value = ps['name'].encode('utf8')
            self._remap_attrs(root, 121, explicit, where + '.root')
            base_lines = {x for e in src_page.objects[0].events for x in e.lines}
            for label, lines in ps['root_events'].items():
                e = root.event(label)
                if e is None:
                    raise ConfigError('%s.root has no event %r' % (where, label))
                e.lines = _event_lines(lines, '%s.root.events.%s' % (where, label))
        else:
            attrs = {k: v for k, v in ps['root_attrs'].items() if k != 'objname'}
            attrs['objname'] = ps['name']
            fake = {'type': 121, 'attributes': attrs, 'events': ps['root_events']}
            root = self._fresh(fake, ps, where + '.root', 0)
            base_lines = set()
        self._code(root, base_lines, origin is None, page_base, own, '%s/(page)' % ps['key'], ps['names'] if origin else None)
        objs.append(root)
        for n, so in enumerate(ps['objects'], 1):
            w = '%s.objects[%d](%s)' % (where, n - 1, so['key'])
            if so['origin']:
                f, i = so['origin']
                src = base.pages[f].objects[i]
                code = src.get('type').value[0]
                if _type_any(so['type'], w) != code:
                    raise ConfigError('%s: the component type does not match its origin' % w)
                o = copy_obj(src)
                explicit, changed = self._apply_overrides(o, so['attributes'], code, w)
                self._remap_attrs(o, code, explicit, w)
                self._derive_geometry(o, changed)
                if 'txt' in changed and o.get('txt_maxl') and len(o.get('txt').value) > int.from_bytes(o.get('txt_maxl').value, 'little'):
                    raise ConfigError('%s: txt is longer than txt_maxl' % w)
                for label, lines in so['events'].items():
                    e = o.event(label)
                    if e is None:
                        raise ConfigError('%s: the component has no event %r' % (w, label))
                    e.lines = _event_lines(lines, '%s.events.%s' % (w, label))
                base_lines = {x for e in src.events for x in e.lines}
                same_page = origin is not None and f == origin
                self._code(o, base_lines, False, base.page_id[f] if same_page else None, own if same_page else None,
                           '%s/%s' % (ps['key'], so['key']), ps['names'] if same_page else None)
            else:
                o = self._fresh(so, ps, w, n)
                self._code(o, set(), True, None, {}, '%s/%s' % (ps['key'], so['key']))
            idattr = o.get('id')
            if idattr is None:
                raise ConfigError('%s: the component has no id attribute' % w)
            idattr.value = bytes([n])
            objs.append(o)
        # names
        names = set()
        for o in objs[1:]:
            nm = o.get('objname').value.decode('utf8', 'replace') if o.get('objname') else None
            if nm in names:
                raise ConfigError('%s: duplicate component name %r' % (where, nm))
            names.add(nm)
        page = M.Page(header, objs)
        if page.name != ps['name']:
            try:
                page.name = ps['name']
            except M.ModelError as e:
                raise ConfigError('%s: %s' % (where, e)) from e
        roots = objs[0].get('objname')
        if (origin is None or ps['name'] != src_page.name) and roots is not None:
            roots.value = ps['name'].encode('utf8')
        if len(objs) > M.MAX_OBJECTS:
            raise ConfigError('%s: a page holds at most %d objects' % (where, M.MAX_OBJECTS - 1))
        return page

    # -- resources
    def _alloc(self):
        used = {}
        for n in self.base.files:
            m = re.fullmatch(r'(\d+)\.(\w+)', n)
            if m:
                fam = {'is': 'i'}.get(m.group(2), m.group(2))
                used[fam] = max(used.get(fam, -1), int(m.group(1)))
        counter = dict(used)

        def alloc(kind):
            counter[kind] = counter.get(kind, -1) + 1
            return counter[kind]
        return alloc

    def _source_bytes(self, rel, label):
        path = P._safe_workspace_path(self.base.root, rel, label + ' ' + str(rel))
        return path.read_bytes()

    def build_resources(self):
        """Returns (new_files {name: bytes}, final names per kind, files kept/replaced/removed)."""
        base, alloc = self.base, self._alloc()
        files, order, final = {}, [], {'i': [], 'zi': [], 'pa': []}
        placed = set()

        def name_for(origin, kind, stem_ext):
            if origin is not None and origin not in placed:
                placed.add(origin)
                return origin
            return '%d.%s' % (alloc(kind), stem_ext)

        for e in self.pictures:
            origin, src = e['origin'], e['source']
            name = name_for(origin, 'i', 'i')
            companion = name + 's'
            if src is not None:
                png = _dict(src, ('png',), e['where'] + '.source', ('png',))['png']
                data = self._source_bytes(png, 'Picture source')
                baseline = _source_png(base.files.get(base.source_of(origin))) if origin else None
                if baseline is not None and baseline == data:
                    compiled, source = base.files[origin], base.files[base.source_of(origin)]   # PNG unchanged
                else:
                    try:
                        compiled, source = IMG.encode_png(data)
                    except ValueError as exc:
                        raise ConfigError('%s: %s' % (e['where'], exc)) from exc
            else:
                compiled = base.files[origin]
                source = base.files.get(base.source_of(origin))
            files[name] = compiled
            if source is not None:
                files[companion] = source
            final['i'].append(name)
        for e in self.fonts:
            origin, src = e['origin'], e['source']
            name = name_for(origin, 'zi', 'zi')
            if src is not None:
                _dict(src, ('ttf', 'height', 'chars', 'chars_file', 'layout', 'bpp', 'name', 'size', 'font_index'),
                      e['where'] + '.source', ('ttf',))
                if 'chars' in src and 'chars_file' in src:
                    raise ConfigError('%s.source: use either chars or chars_file' % e['where'])
                chars = src.get('chars')
                if 'chars_file' in src:
                    path = P._safe_workspace_path(base.root, src['chars_file'], 'Character file')
                    chars = P._parse_chars_file(path)
                ttf = P._safe_workspace_path(base.root, src['ttf'], 'Font source')
                from hmi_font import encode_ttf
                try:
                    data = encode_ttf(str(ttf), height=src.get('height'), chars=chars, name=src.get('name'),
                                      template=base.files[origin] if origin else None, bpp=src.get('bpp'),
                                      size=src.get('size'), font_index=src.get('font_index', 0), layout=src.get('layout'))
                except ValueError as exc:
                    raise ConfigError('%s: %s' % (e['where'], exc)) from exc
            else:
                data = base.files[origin]
            files[name] = data
            final['zi'].append(name)
        return files, final, placed, alloc

    # -- everything
    def compile(self):
        base = self.base
        new_files, final, placed_res, alloc = self.build_resources()
        page_files, placed = {}, set()
        for ps in self.pages:
            page = self.build_page(ps)
            data = page.to_bytes()
            origin = ps['origin']
            if origin is not None and origin not in placed:
                placed.add(origin)
                name = origin
                if data == base.files[origin]:
                    data = base.files[origin]
            else:
                name = '%d.pa' % alloc('pa')
            page_files[name] = data
            final['pa'].append(name)
        # Program.s
        program = base.files.get('Program.s')
        if program is not None:
            lines_old = {x for x in program.decode('utf8', 'surrogateescape').splitlines(True)}
            out = []
            text = program.decode('utf8', 'surrogateescape')
            for n, line in enumerate(text.splitlines(True), 1):
                body = line.rstrip('\r\n')
                eol = line[len(body):]
                new = self._line(body.encode('utf8', 'surrogateescape'), line not in lines_old, None, None,
                                 'Program.s#%d' % n).decode('utf8', 'surrogateescape')
                out.append(new + eol)
            new_program = ''.join(out).encode('utf8', 'surrogateescape')
        else:
            new_program = None
        self._resolve_findings()
        # index records: keep the original kind pattern, refill each managed kind
        index = M.Index(base.index.header, [], base.index.tail)
        records, remaining = [], {k: list(v) for k, v in final.items()}
        for kind, name in base.index.pairs():
            if kind in MANAGED:
                if remaining[kind]:
                    records.append((kind, remaining[kind].pop(0)))
            else:
                records.append((kind, name))
        for kind in MANAGED:
            if remaining[kind]:
                pos = max([i for i, (k, _) in enumerate(records) if k == kind], default=len(records) - 1) + 1
                for off, name in enumerate(remaining[kind]):
                    records.insert(pos + off, (kind, name))
        try:
            index.records = [M.Index.make_record(k, n) for k, n in records]
            index_bytes = index.to_bytes()
        except M.ModelError as e:
            raise ConfigError(str(e)) from e
        if index_bytes == base.files['main.HMI']:
            index_bytes = base.files['main.HMI']
        # assemble in baseline order; additions at the end
        produced = dict(new_files)
        produced.update(page_files)
        kept_names = set(produced)
        out_files, removed = {}, []
        for name in base.order:
            if name == 'main.HMI':
                out_files[name] = index_bytes
            elif name == 'Program.s' and new_program is not None:
                out_files[name] = new_program
            elif name in base.managed:
                if name in kept_names:
                    out_files[name] = produced[name]
                else:
                    removed.append(name)
            else:
                out_files[name] = base.files[name]
        added = []
        order_new = []
        for kind in ('i', 'zi', 'pa'):
            for name in final[kind]:
                if kind == 'i':
                    order_new += [name, name + 's']
                else:
                    order_new.append(name)
        for name in order_new:
            if name not in out_files and name in produced:
                out_files[name] = produced[name]
                added.append(name)
        if len(out_files) > I.MAX_RECORDS:
            raise ConfigError('The project would exceed %d files' % I.MAX_RECORDS)
        modified = [n for n in out_files if n in base.files and out_files[n] != base.files[n]]
        def id_changes(entries, old_of):
            out = {}
            for new_id, e in enumerate(entries):
                old = old_of(e)
                if old != new_id:
                    out[e['key']] = [old, new_id]
            return out
        return Candidate(out_files, set(modified) | set(added), {
            'added': added, 'removed': removed, 'modified': modified, 'rewritten_code_lines': self.rewritten,
            'page_id_changes': id_changes(self.pages, lambda e: base.page_id.get(e['origin'])),
            'picture_id_changes': id_changes(self.pictures, lambda e: base.pic_id.get(e['origin'])),
            'font_id_changes': id_changes(self.fonts, lambda e: base.font_id.get(e['origin'])),
            'acknowledged': self.acked})

    # -- findings
    def _resolve_findings(self):
        dyn = {}
        for entry in _list(self.ack.get('dynamic', []), 'acknowledge.dynamic'):
            _dict(entry, ('site', 'sha256'), 'acknowledge.dynamic[]', ('site', 'sha256'))
            if not isinstance(entry['site'], str) or not isinstance(entry['sha256'], str):
                raise ConfigError('acknowledge.dynamic[] site and sha256 must be strings')
            dyn[(entry['site'], entry['sha256'])] = False
        hard, need_dyn, external = [], [], []
        for site, text, f in self.findings:
            if f.kind in ('dangling', 'newref'):
                hard.append('%s: %s\n    %s' % (site, f.message, text.strip()))
            elif f.kind in ('dynamic', 'ordered'):
                key = (site, _sha(text))
                if key in dyn:
                    dyn[key] = True
                else:
                    need_dyn.append((site, text, f))
            elif f.kind == 'external':
                external.append((site, text, f))
        if hard:
            raise ConfigError('Reference problems:\n  ' + '\n  '.join(hard[:20]) + ('\n  ...' if len(hard) > 20 else ''))
        if need_dyn:
            lines = ['  %s: %s\n      %s\n      acknowledge with: {"site": %s, "sha256": "%s"}' % (
                s, f.message, t.strip(), json.dumps(s), _sha(t)) for s, t, f in need_dyn[:12]]
            raise ConfigError('Computed references could be invalidated by the id changes of this edit. Check each site '
                              '(make the producer use the final id, or keep that id unchanged) and list it under '
                              '"acknowledge": {"dynamic": [...]}:\n' + '\n'.join(lines) + ('\n  ...' if len(need_dyn) > 12 else ''))
        stale = [k for k, used in dyn.items() if not used]
        if stale:
            raise ConfigError('Unused/stale dynamic acknowledgement(s): %s' % ', '.join(s for s, _ in stale))
        ext_ids = self.ack.get('external_ids', [])
        if not isinstance(ext_ids, list) or any(not isinstance(x, str) for x in ext_ids):
            raise ConfigError('acknowledge.external_ids must be a list of strings')
        needed = []
        for ps in self.pages:
            if ps['origin']:
                bid = self.base.page_id[ps['origin']]
                if self.page_map.get(bid) not in (None, bid) and any(
                        o.get('sendkey') and o.get('sendkey').value != b'\0' for o in self.base.pages[ps['origin']].objects[1:]):
                    needed.append(ps['key'])        # touch events of this page report its page id
        if self.maps_obj.changed('page') and external:
            needed += [p['key'] for p in self.pages if p['origin'] and self.page_map.get(self.base.page_id[p['origin']]) not in (None, self.base.page_id[p['origin']])]
        for ps in self.pages:
            if ps['origin'] and self.comp_map.get(self.base.page_id[ps['origin']]) and any(
                    v not in (None, k) for k, v in ps['comp'].items()):
                src = self.base.pages[ps['origin']]
                if any(o.get('sendkey') and o.get('sendkey').value != b'\0' for o in src.objects[1:]):
                    needed.append(ps['key'] + '/*')
        needed = sorted(set(needed))
        missing = [k for k in needed if '*' not in ext_ids and k not in ext_ids]
        self.acked = {'dynamic': sum(1 for u in dyn.values() if u), 'external_ids': needed}
        if missing:
            raise ConfigError('This edit changes ids that are visible outside the project (UART frames such as `prints dp` '
                              'or touch events): %s. External firmware/controller code is not changed by this tool. '
                              'List them under "acknowledge": {"external_ids": [...]} (or ["*"]) after checking it.' % ', '.join(missing[:10]))


class Candidate:
    def __init__(self, files, changed, report):
        self.files, self.changed, self.report = files, changed, report


def compile_project(directory, config_path):
    root = Path(directory).resolve()
    path = Path(config_path)
    if not path.is_absolute():
        path = P._safe_workspace_path(root, path, 'Config')
    head = load_json(path, 'config')
    if isinstance(head, dict) and head.get('portable'):
        import hmi_portable
        return hmi_portable.compile_portable(root, head)
    base = Baseline(directory)
    try:
        cfg, ack = load_config(base, path)
        return Compiler(base, cfg, ack).compile()
    except ConfigError:
        raise
    except (M.ModelError, ValueError) as e:
        raise ConfigError(str(e)) from e
    except (TypeError, AttributeError, KeyError, IndexError) as e:
        raise ConfigError('Malformed config (%s: %s)' % (type(e).__name__, e)) from e
