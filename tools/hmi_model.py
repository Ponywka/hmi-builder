#!/usr/bin/env python3
"""Lossless page and project-index models for USART HMI files (standard library only).

The readers keep every byte that the format carries: ordered attribute records, ordered event blocks (code lines as
bytes), per-object table words and trailing bytes, the unparsed page header, and the raw 16-byte index records.
Serializing an unmodified model reproduces the original live bytes exactly, so a structural edit changes only the
records the caller touched. Checksums are not handled here; sign with hmi_integrity.sign_page / sign_index.

Page layout (live part):   56-byte header | N x (u32 offset, u32 size, u32 extra) | object blocks
  header: crc u32, datasize u32, table_addr u32 (0x38), count u32, ..., name[16] at 0x18
  object block: lp('att-N') + N x (u32 16+len, name[16], value) + event blocks + opaque tail
  event block:  lp('codes<label>-M') + M x lp(line)
Index layout: 96-byte header | records of kind[8] + name[8]; header word 7 is the record count.
"""
import re
import struct

PAGE_HEADER_SIZE = 0x38
INDEX_HEADER_SIZE = 96
RECORD_SIZE = 16
MAX_OBJECTS = 255          # local object ids are one byte wide; 255 is reserved for "all" in some commands
MAX_RECORDS = 15000

SIGNED16 = {'x', 'y', 'endx', 'endy', 'movex', 'movey', 'spax', 'spay'}
SIGNED32 = {'val', 'minval', 'maxval'}
TEXT_ATTRS = {'objname', 'txt', 'path'}


class ModelError(ValueError):
    pass


def lp(b):
    return struct.pack('<I', len(b)) + bytes(b)


class Attr:
    """One attribute record; `field` is the raw 16-byte name field, so odd padding survives a round trip."""
    __slots__ = ('field', 'value')

    def __init__(self, name, value, field=None):
        if field is None:
            try:
                raw = name.encode('ascii') if isinstance(name, str) else bytes(name)
            except UnicodeEncodeError as e:
                raise ModelError('Attribute name must be ASCII: %r' % (name,)) from e
            if not raw or len(raw) > 16 or b'\0' in raw:
                raise ModelError('Invalid attribute name: %r' % name)
            field = raw.ljust(16, b'\0')
        self.field = bytes(field)
        self.value = bytes(value)

    @property
    def name(self):
        return self.field.split(b'\0', 1)[0].decode('ascii', 'replace')

    def to_bytes(self):
        return struct.pack('<I', 16 + len(self.value)) + self.field + self.value


class Event:
    """One `codes<label>-M` block. `header` is kept raw while the line count is unchanged."""
    __slots__ = ('header', 'lines', '_count')

    def __init__(self, label, lines, header=None):
        self.lines = [bytes(x) for x in lines]
        if header is None:
            if not label.startswith('codes'):
                raise ModelError('Event label must start with "codes": %r' % label)
            header = ('%s-%d' % (label, len(self.lines))).encode('ascii')
            self._count = len(self.lines)
        else:
            self._count = len(self.lines)
        self.header = bytes(header)

    @property
    def label(self):
        return self.header.rsplit(b'-', 1)[0].decode('ascii', 'replace')

    def to_bytes(self):
        header = self.header
        if len(self.lines) != self._count:
            header = ('%s-%d' % (self.label, len(self.lines))).encode('ascii')
        return lp(header) + b''.join(lp(x) for x in self.lines)


class Obj:
    """A page object (the first one is the page root, type 121)."""
    __slots__ = ('attrs', 'events', 'tail', 'extra', 'count_header', '_count')

    def __init__(self, attrs, events, tail=b'\0\0\0\0', extra=0, count_header=None):
        self.attrs = list(attrs)
        self.events = list(events)
        self.tail = bytes(tail)
        self.extra = extra
        self._count = len(self.attrs)
        self.count_header = count_header

    def get(self, name):
        for a in self.attrs:
            if a.name == name:
                return a
        return None

    def event(self, label):
        for e in self.events:
            if e.label == label:
                return e
        return None

    def to_bytes(self):
        header = self.count_header
        if header is None or len(self.attrs) != self._count:
            header = ('att-%d' % len(self.attrs)).encode('ascii')
        out = [lp(header)]
        out += [a.to_bytes() for a in self.attrs]
        out += [e.to_bytes() for e in self.events]
        out.append(self.tail)
        return b''.join(out)


class Page:
    __slots__ = ('header', 'objects')

    def __init__(self, header, objects):
        if len(header) != PAGE_HEADER_SIZE:
            raise ModelError('Page header must have %d bytes' % PAGE_HEADER_SIZE)
        self.header = bytearray(header)
        self.objects = list(objects)

    @property
    def name(self):
        return bytes(self.header[24:40]).split(b'\0', 1)[0].decode('utf8', 'replace')

    @name.setter
    def name(self, value):
        raw = value.encode('utf8')
        if not raw or len(raw) > 16 or b'\0' in raw:
            raise ModelError('Page name must be 1..16 UTF-8 bytes without NUL')
        self.header[24:40] = raw.ljust(16, b'\0')

    def to_bytes(self):
        if not 1 <= len(self.objects) <= MAX_OBJECTS:
            raise ModelError('A page needs 1..%d objects' % MAX_OBJECTS)
        blocks = [o.to_bytes() for o in self.objects]
        offset, table = 12 * len(blocks), []
        for o, b in zip(self.objects, blocks):
            table.append(struct.pack('<III', offset, len(b), o.extra))
            offset += len(b)
        head = bytearray(self.header)
        struct.pack_into('<I', head, 4, PAGE_HEADER_SIZE + offset)
        struct.pack_into('<I', head, 8, PAGE_HEADER_SIZE)
        struct.pack_into('<I', head, 12, len(blocks))
        return bytes(head) + b''.join(table) + b''.join(blocks)


def _parse_object(block, extra):
    if len(block) < 4:
        raise ModelError('Object block is truncated')
    n = struct.unpack_from('<I', block)[0]
    m = re.fullmatch(rb'att-(\d+)', block[4:4 + n])
    if not m or 4 + n > len(block):
        raise ModelError('Object block does not start with an att-N header')
    count_header, count, p = block[4:4 + n], int(m.group(1)), 4 + n
    attrs = []
    for _ in range(count):
        if p + 20 > len(block):
            raise ModelError('Attribute record is truncated')
        h = struct.unpack_from('<I', block, p)[0]
        if h < 16 or p + 4 + h > len(block):
            raise ModelError('Attribute record has an invalid length')
        attrs.append(Attr(None, block[p + 20:p + 4 + h], block[p + 4:p + 20]))
        p += 4 + h
    events = []
    while p + 4 <= len(block):
        ln = struct.unpack_from('<I', block, p)[0]
        if not 0 < ln < 40 or block[p + 4:p + 9] != b'codes' or p + 4 + ln > len(block):
            break
        header = block[p + 4:p + 4 + ln]
        m = re.fullmatch(rb'(codes.+)-(\d+)', header)
        if not m:
            break
        q, lines = p + 4 + ln, []
        for _ in range(int(m.group(2))):
            if q + 4 > len(block):
                raise ModelError('Event line is truncated')
            ll = struct.unpack_from('<I', block, q)[0]
            if q + 4 + ll > len(block):
                raise ModelError('Event line is truncated')
            lines.append(block[q + 4:q + 4 + ll])
            q += 4 + ll
        events.append(Event(None, lines, header))
        p = q
    o = Obj(attrs, events, block[p:], extra, count_header)
    return o


def parse_page(data):
    """Strict reader for the supported page layout; unchanged to_bytes() equals data[:datasize]."""
    data = bytes(data)
    if len(data) < PAGE_HEADER_SIZE:
        raise ModelError('Page is shorter than its header')
    size, table, count = struct.unpack_from('<III', data, 4)
    if table != PAGE_HEADER_SIZE:
        raise ModelError('Unsupported page table address: %d' % table)
    if not 1 <= count <= MAX_OBJECTS or size > len(data) or size < table + 12 * count:
        raise ModelError('Invalid page object count or size')
    live = data[:size]
    objects, expected = [], 12 * count
    for i in range(count):
        off, ln, extra = struct.unpack_from('<III', live, table + 12 * i)
        if off != expected or table + off + ln > size:
            raise ModelError('Object %d is not stored contiguously inside the page' % i)
        objects.append(_parse_object(live[table + off:table + off + ln], extra))
        expected += ln
    if table + expected != size:
        raise ModelError('Page has unaccounted bytes between objects and its size')
    page = Page(live[:PAGE_HEADER_SIZE], objects)
    return page


# ---------------------------------------------------------------- attribute values

def decode_value(name, raw):
    """int / str / {'hex': ...} view of one attribute value (width taken from the stored bytes)."""
    if name in TEXT_ATTRS:
        try:
            return raw.decode('utf8')
        except UnicodeDecodeError:
            return {'hex': raw.hex()}
    if len(raw) in (1, 2, 4):
        signed = (name in SIGNED16 and len(raw) == 2) or (name in SIGNED32 and len(raw) == 4)
        return int.from_bytes(raw, 'little', signed=signed)
    return {'hex': raw.hex()}


def encode_value(name, value, width=None):
    """Encode a JSON value; `width` is the fixed byte width of numeric attributes (None for text/hex)."""
    if isinstance(value, dict) and set(value) == {'hex'}:
        try:
            return bytes.fromhex(value['hex'])
        except (ValueError, TypeError) as e:
            raise ModelError('Invalid hex value for %s' % name) from e
    if isinstance(value, str):
        if name not in TEXT_ATTRS:
            raise ModelError('Attribute %s is not a text attribute' % name)
        return value.encode('utf8')
    if isinstance(value, int) and not isinstance(value, bool):
        if width not in (1, 2, 4):
            raise ModelError('Attribute %s has no numeric width' % name)
        signed = (name in SIGNED16 and width == 2) or (name in SIGNED32 and width == 4)
        try:
            return value.to_bytes(width, 'little', signed=signed)
        except OverflowError as e:
            raise ModelError('Value %r does not fit attribute %s (%d byte%s)' % (value, name, width, 's' * (width > 1))) from e
    raise ModelError('Invalid value for attribute %s: %r' % (name, value))


# ---------------------------------------------------------------- project index

class Index:
    """main.HMI: 96-byte header plus 16-byte records (kind[8] + name[8]); raw records survive unchanged."""
    __slots__ = ('header', 'records', 'tail')

    def __init__(self, header, records, tail=b''):
        if len(header) != INDEX_HEADER_SIZE:
            raise ModelError('Index header must have %d bytes' % INDEX_HEADER_SIZE)
        self.header = bytearray(header)
        self.records = [bytes(r) for r in records]
        self.tail = bytes(tail)

    @staticmethod
    def make_record(kind, name):
        for label, v in (('kind', kind), ('name', name)):
            try:
                raw = v.encode('ascii') if isinstance(v, str) else bytes(v)
            except UnicodeEncodeError as e:
                raise ModelError('Index %s must be ASCII: %r' % (label, v)) from e
            if not raw or len(raw) > 8 or b'\0' in raw or not re.fullmatch(rb'[\w.\-]+', raw):
                raise ModelError('Index %s must be 1..8 characters of [A-Za-z0-9_.-]: %r' % (label, v))
        return kind.encode('ascii').ljust(8, b'\0') + name.encode('ascii').ljust(8, b'\0')

    @staticmethod
    def split(record):
        return (record[:8].split(b'\0', 1)[0].decode('ascii', 'replace'),
                record[8:].split(b'\0', 1)[0].decode('ascii', 'replace'))

    def pairs(self):
        return [self.split(r) for r in self.records]

    def to_bytes(self):
        if len(self.records) > MAX_RECORDS:
            raise ModelError('Too many index records')
        head = bytearray(self.header)
        struct.pack_into('<I', head, 24, INDEX_HEADER_SIZE)
        struct.pack_into('<I', head, 28, len(self.records))
        return bytes(head) + b''.join(self.records) + self.tail


def parse_index(data):
    data = bytes(data)
    if len(data) < INDEX_HEADER_SIZE:
        raise ModelError('Index is shorter than its header')
    addr, count = struct.unpack_from('<II', data, 24)
    if addr != INDEX_HEADER_SIZE:
        raise ModelError('Unsupported resource table address: %d' % addr)
    end = INDEX_HEADER_SIZE + RECORD_SIZE * count
    if count > MAX_RECORDS or end > len(data):
        raise ModelError('Index record count exceeds the file')
    records = [data[INDEX_HEADER_SIZE + RECORD_SIZE * i:INDEX_HEADER_SIZE + RECORD_SIZE * (i + 1)] for i in range(count)]
    for i, r in enumerate(records):
        if not re.fullmatch(rb'[a-z]+\0+[\w.\-]+\0*', r):
            raise ModelError('Index record %d is malformed' % i)
    return Index(data[:INDEX_HEADER_SIZE], records, data[end:])
