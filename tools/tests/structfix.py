"""Synthetic project builder for structural tests (no vendor data, no editor)."""
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hmi_factory as F
import hmi_font as FONT
import hmi_integrity as I
import hmi_model as M
import hmi_parse as H
import hmi_project as P


def picture(variant=0):
    """Tiny valid 2x2 compiled picture; `variant` makes the bytes distinguishable."""
    payload = (struct.pack('<5I', 2, 2, 2, 0, 1) + struct.pack('<HHB', 0, 1, 1) +
               struct.pack('<H', 0xffff) + bytes((8 + variant,)))
    return struct.pack('<4BH2BIHHIi', 13, 96, 1, 4, 0, 0, 0, 24, 2, 2, len(payload), 0) + payload


def font(name='tiny'):
    g = {'width': 2, 'left': 0, 'right': 0, 'alpha': [255] * 4}
    return FONT.encode_font({65: g}, 2, name, 'ascii', 1)


def page_bytes(name, objects, extras=False, **root_values):
    """objects: [(type, values, events)] -> signed page bytes. Root geometry defaults to 272x480.
    extras=True gives every object a distinct table word and trailer so tests can see them follow their owner."""
    values = dict(objname=name, x=0, y=0, w=272, h=480)
    values.update(root_values)
    objs = [F.make_object(121, values)]
    for n, (t, vals, events) in enumerate(objects, 1):
        objs.append(F.make_object(t, vals, events, n))
    if extras:
        for n, o in enumerate(objs):
            o.extra, o.tail = 100 + n, bytes((n, 0, 0, 7))
    page = M.Page(bytes(56), objs)
    page.name = name
    return I.sign_page(page.to_bytes())


def project(directory, pages, pictures=3, fonts=1, program='page 0\n', extras=False, animations=0):
    """Create a workspace under `directory`; pages = [(name, [(type, values, events)], root_values)].

    Pictures are N.i/N.is with N = 10, 11, ... and fonts N.zi, pages N.pa (N = 0, 1, ...), so that ids and filenames differ.
    """
    directory = Path(directory)
    files, records = {}, []
    for i in range(pictures):
        n = 10 + i
        files['%d.i' % n] = picture(i)
        files['%d.is' % n] = b'png-%d' % n
        records.append(('i', '%d.i' % n))
    for i in range(fonts):
        files['%d.zi' % i] = font('f%d' % i)
        records.append(('zi', '%d.zi' % i))
    if animations:
        import hmi_anim
        from test_hmi_project import png_2x2
        for i in range(animations):
            n = 20 + i
            files['%d.gmov' % n], files['%d.gmovs' % n] = hmi_anim.encode_animation([png_2x2(), png_2x2()], [100 + i, 200], 10)
            records.append(('gmov', '%d.gmov' % n))
    for i, spec in enumerate(pages):
        name, objects = spec[0], spec[1]
        root = spec[2] if len(spec) > 2 else {}
        files['%d.pa' % i] = page_bytes(name, objects, extras, **root)
        records.append(('pa', '%d.pa' % i))
    files['Program.s'] = program.encode('utf8')
    head = bytearray(96)
    struct.pack_into('<I', head, 4, 96)
    struct.pack_into('<II', head, 24, 96, len(records))
    index = bytes(head) + b''.join(M.Index.make_record(k, n) for k, n in records)
    files['main.HMI'] = I.sign_index(index)
    source = directory / 'source.HMI'
    source.write_bytes(H.write_container(files, order=list(files)))
    workspace = directory / 'workspace'
    P.unpack(source, workspace, pages=False)
    return workspace, files


def button(name, x=0, y=0, **extra):
    vals = dict(objname=name, x=x, y=y, w=50, h=20)
    events = extra.pop('events', {})
    vals.update(extra)
    return (98, vals, events)


def text(name, x=0, y=0, **extra):
    vals = dict(objname=name, x=x, y=y, w=50, h=20)
    events = extra.pop('events', {})
    vals.update(extra)
    return (116, vals, events)


def edit_config(workspace, fn):
    path = Path(workspace) / 'project.json'
    cfg = json.loads(path.read_text(encoding='utf8'))
    fn(cfg)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf8')


def edit_page(workspace, key, fn):
    cfg = json.loads((Path(workspace) / 'project.json').read_text(encoding='utf8'))
    entry = next(p for p in cfg['pages'] if p['key'] == key)
    path = Path(workspace) / entry['content']['path']
    doc = json.loads(path.read_text(encoding='utf8'))
    fn(doc)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf8')


def resolved_pictures(files):
    """{page name: {object name: {attr: picture file or 'none'}}} from a file map, following the index order."""
    idx = M.parse_index(files['main.HMI'])
    pics = [n for k, n in idx.pairs() if k == 'i']
    out = {}
    for k, n in idx.pairs():
        if k != 'pa':
            continue
        page = M.parse_page(files[n])
        d = {}
        for o in page.objects:
            r = {}
            for a in o.attrs:
                if a.name in ('pic', 'pic1', 'pic2', 'picc', 'picc1', 'picc2', 'bpic', 'ppic'):
                    v = int.from_bytes(a.value, 'little')
                    r[a.name] = 'none' if v == 65535 else pics[v]
            d[o.get('objname').value.decode()] = r
        out[page.name] = d
    return out
