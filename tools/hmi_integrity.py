#!/usr/bin/env python3
"""USART HMI integrity checks, independent of Wine and the editor.

The CRC register is non-reflected, polynomial 0x04c11db7, initially 0xffffffff,
with no final XOR. Each input BYTE is XORed into the low bits of the register
and followed by 32 shifts (four table updates), not the usual eight shifts.
Page/index checksums cover all stored bytes after the first four checksum
bytes, then selected header fields a second time in a format-specific order.
These are integrity checksums, not cryptographic signatures.
"""
import struct

MASK = 0xffffffff
FORMAT_VERSION = 0x21
FILE_ID = 0x55
WRITER_VERSION = (1, 68, 1)
DATA_BASE = 0x700000
BACKUP_BASE = 0x80000
ALT_TABLE_MARKER = 0x380000
CONTAINER_TAG = b'ver21234'
MAX_RECORDS = 15000  # larger native tables use a different layout, not implemented here


def _crc_table():
    table = []
    for byte in range(256):
        c = byte << 24
        for _ in range(8):
            c = ((c << 1) ^ (0x04c11db7 if c & 0x80000000 else 0)) & MASK
        table.append(c)
    return tuple(table)


CRC_TABLE = _crc_table()


def crc_bytes(data, initial=MASK):
    """Update the byte-wise HMI CRC register; consecutive calls concatenate data."""
    c = initial & MASK
    table = CRC_TABLE
    for byte in data:
        c ^= byte
        c = ((c << 8) & MASK) ^ table[c >> 24]
        c = ((c << 8) & MASK) ^ table[c >> 24]
        c = ((c << 8) & MASK) ^ table[c >> 24]
        c = ((c << 8) & MASK) ^ table[c >> 24]
    return c


def crc_words(data, initial=MASK):
    """Container CRC: inject each little-endian 32-bit word, then advance 32 bits."""
    if len(data) % 4:
        raise ValueError('Word CRC input length must be a multiple of four')
    c = initial & MASK
    table = CRC_TABLE
    for (word,) in struct.iter_unpack('<I', data):
        c ^= word
        c = ((c << 8) & MASK) ^ table[c >> 24]
        c = ((c << 8) & MASK) ^ table[c >> 24]
        c = ((c << 8) & MASK) ^ table[c >> 24]
        c = ((c << 8) & MASK) ^ table[c >> 24]
    return c


def _length(data, minimum):
    if not minimum <= len(data) <= MASK:
        raise ValueError('Signed file must have %d..%d bytes' % (minimum, MASK))


def page_checksum(data):
    _length(data, 56)
    c = crc_bytes(memoryview(data)[4:])
    for start, end in ((4, 8), (12, 16), (20, 21), (21, 22)):
        c = crc_bytes(data[start:end], c)
    return c


def index_checksum(data):
    _length(data, 96)
    c = crc_bytes(memoryview(data)[4:])
    for start, end in ((16, 20), (4, 8), (10, 11), (14, 15)):
        c = crc_bytes(data[start:end], c)
    return c


def verify_page(data):
    return (len(data) >= 56 and data[21] == FILE_ID and data[22] == FORMAT_VERSION
            and struct.unpack_from('<I', data, 4)[0] == len(data)
            and struct.unpack_from('<I', data)[0] == page_checksum(data))


def verify_index(data):
    return (len(data) >= 96 and data[14] == FILE_ID and data[10] == FORMAT_VERSION
            and struct.unpack_from('<I', data)[0] == index_checksum(data))


def _version(version):
    if len(version) != 3 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in version):
        raise ValueError('Writer version must contain three bytes')
    return bytes(version)


def sign_page(data, version=WRITER_VERSION):
    """Preserve valid historical files; otherwise normalize headers and recalculate."""
    _length(data, 56)
    if verify_page(data):
        return bytes(data)
    out = bytearray(data)
    out[21:23] = bytes((FILE_ID, FORMAT_VERSION))
    out[40:43] = _version(version)
    struct.pack_into('<I', out, 4, len(out))
    struct.pack_into('<I', out, 0, page_checksum(out))
    return bytes(out)


def sign_index(data, version=WRITER_VERSION):
    _length(data, 96)
    if verify_index(data):
        return bytes(data)
    out = bytearray(data)
    writer = _version(version)
    out[8:10] = writer[:2]
    out[36] = writer[2]
    out[10], out[14] = FORMAT_VERSION, FILE_ID
    struct.pack_into('<I', out, 0, index_checksum(out))
    return bytes(out)


def _name_bytes(name):
    if not isinstance(name, str) or name in ('.', '..') or '/' in name or '\\' in name:
        raise ValueError('Invalid internal file name: %r' % name)
    try:
        raw = name.encode('latin1')
    except UnicodeEncodeError as e:
        raise ValueError('Internal file name must be Latin-1: %r' % name) from e
    if not 1 <= len(raw) <= 15 or any(b < 32 or b == 127 for b in raw):
        raise ValueError('Internal file name must contain 1..15 non-control bytes: %r' % name)
    return raw


def table_checksum(table):
    """Word-wise CRC of count+28-byte records, followed by the four-byte salt ADEC."""
    return crc_words(b'ADEC', crc_words(table))


def build_container(files, order=None, extra=0):
    """Build the supported compact ver21234 container, preserving supplied resource bytes.

    `extra` is opaque per-record metadata, not a payload checksum. Native-reader
    acceptance was verified for zero and several nonzero values; default zero
    makes output deterministic. It can be set to reproduce native oracle files.
    Inner page/index checksums are the caller's responsibility.
    """
    names = list(files if order is None else order)
    if len(names) > MAX_RECORDS:
        raise ValueError('Containers with more than %d records use an unsupported layout' % MAX_RECORDS)
    if len(set(names)) != len(names) or set(names) != set(files):
        raise ValueError('Order must name every internal file exactly once')
    if not isinstance(extra, int) or not 0 <= extra <= MASK:
        raise ValueError('Entry metadata must be a u32')
    table = bytearray(struct.pack('<I', len(names)))
    offset, body = DATA_BASE, []
    for name in names:
        raw = _name_bytes(name)
        data = files[name]
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError('Internal file payload must be bytes: ' + name)
        data = bytes(data)
        if offset + len(data) > MASK:
            raise ValueError('Container exceeds the 32-bit file size limit')
        table += raw.ljust(16, b'\0') + struct.pack('<III', offset, len(data), extra)
        body.append(data)
        offset += len(data)
    checked_table = table + struct.pack('<I', table_checksum(table))
    out = bytearray(DATA_BASE)
    out[:len(checked_table)] = checked_table
    out[BACKUP_BASE:BACKUP_BASE + len(checked_table)] = checked_table
    struct.pack_into('<I', out, ALT_TABLE_MARKER, MASK)
    out[DATA_BASE - len(CONTAINER_TAG):DATA_BASE] = CONTAINER_TAG
    return bytes(out) + b''.join(body)


def container_errors(data, require_exact_body=False):
    """Validate both metadata copies and payload bounds. Payloads themselves have no outer CRC.

    Native recovery from one damaged table copy is intentionally not performed:
    damaged/unsupported containers are reported, not silently rewritten.
    Empty-name records are unused extents and may occur more than once.
    """
    if len(data) < DATA_BASE:
        return ['Container is shorter than its metadata area']
    count = struct.unpack_from('<I', data)[0]
    if count > MAX_RECORDS:
        return ['Unsupported container layout: more than %d records' % MAX_RECORDS]
    end = 4 + count * 28
    expected = struct.pack('<I', table_checksum(memoryview(data)[:end]))
    errors = []
    if data[end:end + 4] != expected:
        errors.append('Primary table checksum mismatch')
    if data[BACKUP_BASE:BACKUP_BASE + end] != data[:end]:
        errors.append('Backup table differs from primary table')
    if data[BACKUP_BASE + end:BACKUP_BASE + end + 4] != expected:
        errors.append('Backup table checksum mismatch')
    if data[DATA_BASE - len(CONTAINER_TAG):DATA_BASE] != CONTAINER_TAG:
        errors.append('Unsupported or damaged container format tag')
    if data[ALT_TABLE_MARKER:ALT_TABLE_MARKER + 4] != b'\xff' * 4:
        errors.append('Unexpected alternate-table marker')
    names, extents, last_end = set(), [], DATA_BASE
    for i in range(count):
        pos = 4 + 28 * i
        raw = data[pos:pos + 16]
        name = bytes(raw).split(b'\0', 1)[0].decode('latin1')
        offset, size, _ = struct.unpack_from('<III', data, pos + 16)
        if name:
            try:
                _name_bytes(name)
            except ValueError as e:
                errors.append(str(e))
            if name in names:
                errors.append('Duplicate internal file: ' + name)
            names.add(name)
        if offset < DATA_BASE or offset + size > len(data):
            errors.append('Record %d has a payload outside the container' % i)
        if size:
            extents.append((offset, offset + size, i))
        last_end = max(last_end, offset + size)
    extents.sort()
    previous_end = DATA_BASE
    for start, finish, i in extents:
        if start < previous_end:
            errors.append('Record %d overlaps another payload' % i)
        previous_end = max(previous_end, finish)
    if require_exact_body and last_end != len(data):
        errors.append('Trailing data outside all recorded extents')
    return errors


def verify_container(data, require_exact_body=False):
    return not container_errors(data, require_exact_body)
