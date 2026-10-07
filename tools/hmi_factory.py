#!/usr/bin/env python3
"""Creation profiles for brand-new pages and components (standard library only).

hmi_schema.json lists the per-type attributes and their default bytes, but not the common head that precedes them, and
its `length` field is not always the stored width (button `txt` has length 4 but the default is six bytes). The
profiles here combine

  * the common head (order, widths and neutral defaults) read from the project the editor itself saved:
    every object of every type in MATE_272_480.HMI shares one head layout;
  * the per-type attributes, order, widths and defaults from the schema, whose per-type layout equals the saved
    objects of the same type (checked in tests/test_hmi_factory.py against the real project when HMI_FILE is set);
  * event blocks `codes<label>` in schema order, empty by default.

Supported for creation: page(121), button(98), text(116), picture(112), timer(51), variable(52). The first five have
been seen (or are schema-identical to seen) in saved projects; picture(112) has no saved sample and is taken from the
schema alone, so it is marked `donor: false`. Any other type can still be edited, moved and copied through an
`origin`, which keeps its stored records.
"""
import hmi_model as M
import hmi_parse as H

# (name, width, default); width None = variable text, default None = caller must provide
VISUAL_HEAD = (('type', 1, None), ('id', 1, 0), ('objname', None, None), ('vscope', 1, 0), ('drag', 1, 0),
               ('sendkey', 1, 0), ('aph', 1, 127), ('movex', 2, 0), ('movey', 2, 0), ('x', 2, None), ('y', 2, None),
               ('w', 2, None), ('h', 2, None), ('endx', 2, None), ('endy', 2, None), ('effect', 1, 0),
               ('first', 1, 0), ('time', 2, 300), ('lockobj', 1, 0), ('groupid0', 4, 0), ('groupid1', 4, 0))
REDUCED_HEAD = (('type', 1, None), ('id', 1, 0), ('objname', None, None), ('vscope', 1, 0), ('lockobj', 1, 0),
                ('groupid0', 4, 0), ('groupid1', 4, 0))

CREATABLE = {121: ('page', VISUAL_HEAD, True), 98: ('button', VISUAL_HEAD, True), 116: ('text', VISUAL_HEAD, True),
             112: ('picture', VISUAL_HEAD, False), 51: ('timer', REDUCED_HEAD, True), 52: ('variable', REDUCED_HEAD, True)}
NAME_TO_TYPE = {name: code for code, (name, _, _) in CREATABLE.items()}
TEXT_FIELDS = ('objname', 'txt')


def type_code(value):
    """Accept a numeric type or one of the creatable type names."""
    if isinstance(value, bool):
        raise M.ModelError('Invalid component type: %r' % (value,))
    if isinstance(value, int):
        code = value
    elif isinstance(value, str) and value in NAME_TO_TYPE:
        code = NAME_TO_TYPE[value]
    else:
        raise M.ModelError('Unknown component type %r; creatable types: %s' % (value, ', '.join(sorted(NAME_TO_TYPE))))
    if code not in CREATABLE:
        raise M.ModelError('Type %d cannot be created from defaults; copy an existing object with `origin`' % code)
    return code


def profile(code):
    """Ordered [(name, width, default_bytes_or_None, schema_attr_or_None)] and the event labels for a type."""
    name, head, _donor = CREATABLE[code]
    schema = H.SCHEMA.get(str(code))
    if not schema:
        raise M.ModelError('hmi_schema.json has no definition for type %d' % code)
    attrs = [(n, w, None if d is None else M.encode_value(n, d, w), None) for n, w, d in head]
    for a in schema['attributes']:
        raw = bytes(a['default'])
        if a['name'] in TEXT_FIELDS:
            attrs.append((a['name'], None, raw, a))
        else:
            if len(raw) not in (1, 2, 4):
                raise M.ModelError('Unsupported default width for %s.%s' % (name, a['name']))
            attrs.append((a['name'], len(raw), raw, a))
    labels = ['codes' + e['label'] for e in schema['events']]
    return attrs, labels


def _range_check(code, name, value, width, schema_attr):
    bits = 8 * width
    signed = (name in M.SIGNED16 and width == 2) or (name in M.SIGNED32 and width == 4)
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if schema_attr is not None:
        lo, hi = schema_attr.get('min', 0), schema_attr.get('max', 0)
        # Native min/max sometimes exceed the stored width (crop offsets); only honour ranges that fit.
        if lo < hi and low <= lo and hi <= high:
            low, high = lo, hi
    if not low <= value <= high:
        raise M.ModelError('%s.%s=%r is outside %d..%d' % (CREATABLE[code][0], name, value, low, high))


def make_object(code, values, events=None, object_id=0, page_size=None):
    """Build a new object. `values` maps attribute names to ints/strings (references must already be resolved).

    Required: objname (not for page), x/y/w/h for visual components (a page takes the size of `page_size`).
    Derived: type, endx/endy (inclusive end = start + size - 1).
    """
    code = type_code(code)
    attrs, labels = profile(code)
    known = {n for n, _, _, _ in attrs}
    values = dict(values)
    for k in values:
        if k not in known:
            raise M.ModelError('Unknown attribute %r for %s' % (k, CREATABLE[code][0]))
    if 'type' in values and values['type'] != code:
        raise M.ModelError('The component type cannot be changed through attributes')
    values['type'] = code
    values['id'] = object_id
    visual = 'w' in known
    if visual and code == 121:
        values.setdefault('x', 0)
        values.setdefault('y', 0)
        if page_size:
            values.setdefault('w', page_size[0])
            values.setdefault('h', page_size[1])
    if visual:
        for k in ('x', 'y', 'w', 'h'):
            if k not in values:
                raise M.ModelError('%s needs %r' % (CREATABLE[code][0], k))
            if isinstance(values[k], bool) or not isinstance(values[k], int):
                raise M.ModelError('%s must be an integer' % k)
        if values['w'] < 1 or values['h'] < 1:
            raise M.ModelError('Width and height must be positive')
        values.setdefault('endx', values['x'] + values['w'] - 1)
        values.setdefault('endy', values['y'] + values['h'] - 1)
    out = []
    for name, width, default, schema_attr in attrs:
        if name in values:
            v = values[name]
            if name in TEXT_FIELDS:
                if not isinstance(v, str):
                    raise M.ModelError('%s must be text' % name)
                raw = v.encode('utf8')
            else:
                if isinstance(v, bool) or not isinstance(v, int):
                    raise M.ModelError('%s must be an integer' % name)
                _range_check(code, name, v, width, schema_attr)
                raw = M.encode_value(name, v, width)
        elif default is not None:
            raw = default
        else:
            raise M.ModelError('%s needs %r' % (CREATABLE[code][0], name))
        out.append(M.Attr(name, raw))
    obj = M.Obj(out, [], b'\0\0\0\0', 0)
    names = [n for n, _, _, _ in attrs]
    if 'txt' in names and 'txt_maxl' in names:
        maxl = int.from_bytes(obj.get('txt_maxl').value, 'little')
        if len(obj.get('txt').value) > maxl:
            raise M.ModelError('txt is longer than txt_maxl (%d)' % maxl)
    events = dict(events or {})
    for label in events:
        if label not in labels:
            raise M.ModelError('Event %r is not defined for %s; valid: %s' % (label, CREATABLE[code][0], ', '.join(labels)))
    for label in labels:
        lines = events.get(label, [])
        obj.events.append(M.Event(label, [x if isinstance(x, bytes) else x.encode('utf8') for x in lines]))
    return obj
