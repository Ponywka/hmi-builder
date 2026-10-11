#!/usr/bin/env python3
"""Standalone encoder and validator for USART HMI ``.zi`` font files.

The module deliberately keeps the binary codec independent of Pillow and
fontTools.  Those packages are imported only by :func:`encode_ttf`, where
FreeType is used to rasterise a TTF/CFF font.  A glyph supplied to
:func:`encode_font` is a mapping with ``width``, ``left``, ``right`` and
row-major ``alpha`` fields.  ``width`` is the nominal advance; the other two
metrics are overhangs, not blank padding.

The encoder writes the two profiles found in the supported projects:
``ascii`` (state 0, 95 entries, byte offsets) and ``bmp`` (state 1, 65536
entries, eight-byte aligned offsets).  BMP output accepts only printable BMP
code points when a charset is explicitly supplied.  A template can preserve
its complete present-code set, including legacy control entries.

This is a basic single-glyph layout encoder.  It does not perform shaping,
kerning, or bidirectional layout.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
import os
from pathlib import Path
import re
import struct
import unicodedata


HEADER_FORMAT = "<12BIBBHIIBBBBBBHII"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
HEADER_FIELDS = (
    "Password", "codelT0", "codelDec", "qumo", "encode", "state", "w", "h",
    "codeh_star", "codeh_end", "codel_star", "codel_end", "qyt", "fontver",
    "zimoascbeg", "zimobinbeg", "datasize", "dataaddr", "codehT0", "codehDec",
    "Anti", "unequal_res", "encodenamebeg", "fontdataadd8byte", "res1",
    "trueziqty", "res3",
)

# Header values emitted by the editor for all three known source profiles.
_PROFILE_CONSTANTS = {
    "Password": 4,
    "codelT0": 255,
    "codelDec": 0,
    "qumo": 13,
    "encode": 24,
    "w": 0,
    "codeh_star": 255,
    "codeh_end": 255,
    "codel_star": 0,
    "codel_end": 255,
    "fontver": 6,
    "zimobinbeg": 0,
    "codehT0": 255,
    "codehDec": 0,
    "Anti": 1,
    "unequal_res": 1,
    "res1": 0,
    "res3": 0,
}

_ENCODING = b"utf-8"
_U24_MAX = (1 << 24) - 1


def _u24(value: int) -> bytes:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _U24_MAX:
        raise ValueError("font address does not fit in u24")
    return value.to_bytes(3, "little")


def _normalise_layout(layout) -> str:
    if layout is None:
        return "ascii"
    if isinstance(layout, int) and not isinstance(layout, bool):
        if layout == 0:
            return "ascii"
        if layout == 1:
            return "bmp"
    value = str(layout).strip().lower().replace("_", "-")
    if value in {"ascii", "state0", "state-0", "0", "95"}:
        return "ascii"
    if value in {"bmp", "unicode", "state1", "state-1", "1", "65536"}:
        return "bmp"
    raise ValueError("layout must be 'ascii' or 'bmp'")


def _normalise_bpp(bpp) -> int:
    if bpp is None:
        return 3
    if isinstance(bpp, bool):
        raise ValueError("bpp must be 1 or 3")
    try:
        value = int(bpp)
    except (TypeError, ValueError) as exc:
        raise ValueError("bpp must be 1 or 3") from exc
    if isinstance(bpp, float) and bpp != value:
        raise ValueError("bpp must be 1 or 3")
    if value not in (1, 3):
        raise ValueError("bpp must be 1 or 3")
    return value


def _flatten_alpha(alpha, width: int | None = None, height: int | None = None) -> tuple[list[int], int, int]:
    """Flatten a flat or rectangular alpha iterable and validate its dimensions."""
    if isinstance(alpha, (bytes, bytearray, memoryview)):
        values = list(bytes(alpha))
    else:
        try:
            outer = list(alpha)
        except TypeError as exc:
            raise ValueError("glyph alpha must be an iterable") from exc
        if outer and isinstance(outer[0], (list, tuple, bytes, bytearray, memoryview)):
            rows = [list(row) for row in outer]
            inferred_h = len(rows)
            inferred_w = len(rows[0]) if rows else 0
            if any(len(row) != inferred_w for row in rows):
                raise ValueError("glyph alpha rows have different widths")
            values = [v for row in rows for v in row]
            if height is None:
                height = inferred_h
            if width is None:
                width = inferred_w
        else:
            values = outer
    if width is None or height is None:
        raise ValueError("glyph alpha dimensions are required")
    if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
        raise ValueError("glyph alpha dimensions must be integers")
    if width < 0 or height < 0 or len(values) != width * height:
        raise ValueError("glyph alpha length does not match width * height")
    out = []
    for value in values:
        if isinstance(value, bool):
            value = 255 if value else 0
        if not isinstance(value, int) or not 0 <= value <= 255:
            raise ValueError("glyph alpha values must be integers in 0..255")
        out.append(value)
    return out, width, height


def _quantise_alpha(values: Iterable[int], bpp: int) -> list[int]:
    if bpp == 1:
        out = []
        for value in values:
            if value in (0, 1):
                out.append(0 if value == 0 else 255)
            elif value in (0, 255):
                out.append(value)
            else:
                raise ValueError("1-bit glyph alpha must contain only 0 or 255")
        return out
    # The decoder represents a 3-bit level as round(level * 255 / 7).
    return [round(value * 7 / 255) for value in values]


def _to_stored(values: list[int], width: int, height: int) -> list[int]:
    """Invert the decoder's row-major raster transform for qumo 13."""
    stored = [0] * (width * height)
    for y in range(height):
        row = y * width
        for x in range(width):
            stored[(width - 1 - x) * height + y] = values[row + x]
    return stored


def _emit_uniform(out: bytearray, value: int, count: int, *, bpp: int) -> None:
    """Emit a run of zeros or opaque pixels, in chunks representable by k=0."""
    if count < 0:
        raise ValueError("negative glyph run")
    while count:
        n = min(count, 31)
        if bpp == 1:
            out.append((0x20 if value else 0) | n)
        else:
            # Type 3's k=0 token uses bit 5 to distinguish opaque from zero.
            out.append((0x20 if value == 7 else 0) | n)
        count -= n


def _encode_tokens(levels: list[int], bpp: int) -> bytes:
    """Encode levels in decoder/stored order with non-empty canonical tokens."""
    out = bytearray()
    i = 0
    n = len(levels)
    while i < n:
        value = levels[i]
        if bpp == 1:
            if value not in (0, 255):
                raise ValueError("internal 1-bit level is not binary")
            # A zero run followed by opaque pixels is represented compactly by
            # the type-1 k=1/k=2/k=3 forms where that saves a token.
            if value == 0:
                z = i
                while i < n and levels[i] == 0:
                    i += 1
                zeros = i - z
                if i < n and levels[i] == 255:
                    o = i
                    while i < n and levels[i] == 255:
                        i += 1
                    opaque = i - o
                    # Leave at most 31 zeros for a combined token.
                    if zeros > 31:
                        leading = zeros - 31
                        _emit_uniform(out, 0, leading, bpp=bpp)
                        zeros = 31
                    if zeros <= 7 and 5 <= opaque <= 7:
                        out.append(0xC0 | (zeros << 3) | opaque)
                        continue
                    if 3 <= opaque <= 4:
                        out.append(0x80 | (0x20 if opaque == 4 else 0) | zeros)
                        continue
                    if 1 <= opaque <= 2:
                        out.append(0x40 | (0x20 if opaque == 2 else 0) | zeros)
                        continue
                    # No combined token covers all of a longer opaque run.
                    _emit_uniform(out, 0, zeros, bpp=bpp)
                    _emit_uniform(out, 255, opaque, bpp=bpp)
                    continue
                _emit_uniform(out, 0, zeros, bpp=bpp)
                continue
            # Opaque run not preceded by zeros.
            start = i
            while i < n and levels[i] == 255:
                i += 1
            _emit_uniform(out, 255, i - start, bpp=bpp)
            continue

        # Three-bit alpha.  Levels are integers 0..7.
        if value not in range(8):
            raise ValueError("internal 3-bit level is outside 0..7")
        if value == 0:
            z = i
            while i < n and levels[i] == 0:
                i += 1
            zeros = i - z
            if i < n and levels[i] not in (0, 7):
                # k=2: up to seven zero pixels followed by one antialiased pixel.
                while zeros > 7:
                    take = min(zeros, 31)
                    _emit_uniform(out, 0, take, bpp=bpp)
                    zeros -= take
                out.append(0x80 | (zeros << 3) | levels[i])
                i += 1
                continue
            if i < n and levels[i] == 7:
                # k=1 can combine a short zero run and one or two opaque pixels.
                o = i
                while i < n and levels[i] == 7:
                    i += 1
                opaque = i - o
                if 1 <= opaque <= 2 and zeros <= 31:
                    out.append(0x40 | (0x20 if opaque == 2 else 0) | zeros)
                    continue
                _emit_uniform(out, 0, zeros, bpp=bpp)
                _emit_uniform(out, 7, opaque, bpp=bpp)
                continue
            _emit_uniform(out, 0, zeros, bpp=bpp)
            continue
        if value == 7:
            start = i
            while i < n and levels[i] == 7:
                i += 1
            _emit_uniform(out, 7, i - start, bpp=bpp)
            continue
        # Pair arbitrary levels where possible; a lone level is k=2 with no
        # zero prefix.  Both forms always consume at least one pixel.
        if i + 1 < n and levels[i + 1] in range(1, 7):
            out.append(0xC0 | (value << 3) | levels[i + 1])
            i += 2
        else:
            out.append(0x80 | value)
            i += 1
    return bytes(out)


def encode_glyph(alpha, width: int | None = None, height: int | None = None, bpp: int = 3, *, stored: bool = False) -> bytes:
    """Encode one glyph bitmap and return ``type-byte + token stream``.

    ``alpha`` is normally visual row-major data (``y * width + x``).  Pass
    ``stored=True`` when the input is already in the decoder's stored order.
    ``width`` is the complete bitmap width, including left/right overhangs;
    metrics are handled by :func:`encode_font`.
    """
    bpp = _normalise_bpp(bpp)
    values, width, height = _flatten_alpha(alpha, width, height)
    if width <= 0 or height <= 0:
        raise ValueError("glyph bitmap dimensions must be positive")
    levels = _quantise_alpha(values, bpp)
    if not stored:
        levels = _to_stored(levels, width, height)
    return bytes((bpp,)) + _encode_tokens(levels, bpp)


def _strict_decode_stream(data: bytes, total: int) -> list[int]:
    """Decode one stream exactly; unlike the display decoder, never pads."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("glyph stream is not bytes")
    data = bytes(data)
    if len(data) < 2:
        raise ValueError("glyph stream is truncated")
    typ = data[0]
    if typ not in (1, 3):
        raise ValueError("unsupported glyph codec type %d" % typ)
    out: list[int] = []
    for token in data[1:]:
        before = len(out)
        k = token >> 6
        if typ == 3:
            if k == 3:
                out.extend(((token >> 3) & 7, token & 7))
            elif k == 2:
                out.extend([0] * ((token >> 3) & 7))
                out.append(token & 7)
            elif k == 1:
                out.extend([0] * (token & 31))
                out.extend([7] * (2 if token & 32 else 1))
            else:
                out.extend([7 if token & 32 else 0] * (token & 31))
        else:
            if k == 3:
                out.extend([0] * ((token >> 3) & 7))
                out.extend([1] * (token & 7))
            elif k == 2:
                out.extend([0] * (token & 31))
                out.extend([1] * (3 + ((token >> 5) & 1)))
            elif k == 1:
                out.extend([0] * (token & 31))
                out.extend([1] * (2 if token & 32 else 1))
            else:
                out.extend([1 if token & 32 else 0] * (token & 31))
        if len(out) == before:
            raise ValueError("glyph stream contains an empty token")
        if len(out) > total:
            raise ValueError("glyph stream expands beyond bitmap")
    if len(out) != total:
        raise ValueError("glyph stream expands to %d pixels, expected %d" % (len(out), total))
    return out


def _coerce_glyph(value, height: int) -> tuple[int, int, int, list[int]]:
    """Return nominal width, left, right, and visual row-major alpha."""
    if isinstance(value, Mapping):
        alpha = value.get("alpha", value.get("bitmap"))
        if alpha is None:
            raise ValueError("glyph mapping needs alpha or bitmap")
        width = value.get("width")
        left = value.get("left", 0)
        right = value.get("right", 0)
        if width is None and "w" in value:
            width = int(value["w"]) - int(left) - int(right)
    elif isinstance(value, tuple) and len(value) == 4:
        width, left, right, alpha = value
    else:
        raise ValueError("glyph must be a mapping or (width, left, right, alpha) tuple")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (width, left, right)):
        raise ValueError("glyph metrics must be integers")
    if width < 0 or left < 0 or right < 0 or width + left + right <= 0:
        raise ValueError("glyph metrics must be non-negative and have a bitmap width")
    total_width = width + left + right
    values, actual_width, actual_height = _flatten_alpha(alpha, total_width, height)
    if actual_width != total_width or actual_height != height:
        raise ValueError("glyph alpha dimensions do not match metrics and font height")
    for metric in (width, left, right):
        if metric > 255:
            raise ValueError("glyph metric does not fit in u8")
    return width, left, right, values


def _glyph_map(glyphs) -> dict[int, object]:
    if isinstance(glyphs, Mapping):
        items = glyphs.items()
    else:
        try:
            sequence = list(glyphs)
        except TypeError as exc:
            raise ValueError("glyphs must be a mapping or iterable of mappings") from exc
        items = []
        for item in sequence:
            if isinstance(item, Mapping):
                if "code" not in item:
                    raise ValueError("glyph mapping needs a code")
                items.append((item["code"], item))
            else:
                try:
                    code, glyph = item
                except (TypeError, ValueError) as exc:
                    raise ValueError("glyph iterable items must be mappings or (code, glyph) pairs") from exc
                items.append((code, glyph))
    result: dict[int, object] = {}
    for code, glyph in items:
        if isinstance(code, bool) or not isinstance(code, int):
            raise ValueError("glyph code points must be integers")
        if code in result:
            raise ValueError("duplicate glyph code U+%04X" % code)
        result[code] = glyph
    return result


def _is_control(code: int) -> bool:
    return unicodedata.category(chr(code)).startswith("C")


def _validate_code(code: int, layout: str, *, allow_controls: bool = False) -> None:
    if layout == "ascii":
        if not 32 <= code <= 126:
            raise ValueError("ASCII font code must be in U+0020..U+007E")
        return
    if code < 0 or code > 0xFFFF:
        raise ValueError("BMP font cannot contain non-BMP code U+%X" % code)
    if 0xD800 <= code <= 0xDFFF:
        raise ValueError("BMP font cannot contain surrogate U+%04X" % code)
    if not allow_controls and _is_control(code):
        raise ValueError("BMP font cannot contain control U+%04X" % code)


def _name_bytes(name: str) -> tuple[bytes, bytes]:
    if not isinstance(name, str) or not name:
        raise ValueError("font name must be a non-empty string")
    if "\x00" in name:
        raise ValueError("font name cannot contain NUL")
    raw = name.encode("utf-8")
    if len(raw) > 250 or len(raw) + len(_ENCODING) > 255:
        raise ValueError("font name is too long for a .zi header")
    return raw, _ENCODING


def encode_font(glyphs, height: int, name: str, layout="ascii", bpp: int = 3, *, _allow_controls: bool = False) -> bytes:
    """Build a validated ``.zi`` from visual row-major glyph mappings.

    ``glyphs`` maps Unicode code points to ``{'width', 'left', 'right',
    'alpha'}`` dictionaries.  Missing table entries are encoded as absent
    glyphs.  ``layout`` is ``'ascii'`` or ``'bmp'`` and ``bpp`` is 1 or 3.
    """
    layout = _normalise_layout(layout)
    bpp = _normalise_bpp(bpp)
    if isinstance(height, bool) or not isinstance(height, int) or not 1 <= height <= 255:
        raise ValueError("height must be an integer in 1..255")
    name_raw, encoding_raw = _name_bytes(name)
    code_map = _glyph_map(glyphs)
    for code in code_map:
        _validate_code(code, layout, allow_controls=_allow_controls)

    # Canonical table dimensions and profile flags.
    state = 0 if layout == "ascii" else 1
    qyt = 95 if state == 0 else 65536
    table_bytes = 10 * qyt
    prefix = name_raw + encoding_raw
    zimoascbeg = len(prefix)
    if zimoascbeg > 255:
        raise ValueError("font name and encoding do not fit zimoascbeg")
    base = HEADER_SIZE + zimoascbeg
    payload_start = base + table_bytes + (2 if state == 0 else 0)

    entries = [bytearray(10) for _ in range(qyt)]
    payload = bytearray()
    dedup: dict[tuple[bytes, int, int, int], int] = {}
    present = 0
    for code in sorted(code_map):
        width, left, right, alpha = _coerce_glyph(code_map[code], height)
        stream = encode_glyph(alpha, width + left + right, height, bpp)
        key = (stream, width, left, right)
        if key in dedup:
            absolute = dedup[key]
        else:
            if state == 1:
                # The stored address is relative to ``base`` in eight-byte
                # units.  ``base`` itself need not be eight-byte aligned.
                relative = table_bytes + len(payload)
                aligned = (relative + 7) & ~7
                payload.extend(b"\0" * (aligned - relative))
                absolute = base + aligned
            else:
                absolute = payload_start + len(payload)
            payload.extend(stream)
            dedup[key] = absolute
        rel_bytes = absolute - base
        if state == 1:
            if rel_bytes % 8:
                raise RuntimeError("internal BMP payload alignment error")
            address = rel_bytes // 8
        else:
            address = rel_bytes
        _u24(address)
        index = code - 32 if state == 0 else code
        struct.pack_into("<HBBB", entries[index], 0, code, width, left, right)
        entries[index][5:8] = _u24(address)
        struct.pack_into("<H", entries[index], 8, len(stream))
        present += 1

    total_size = payload_start + len(payload)
    if total_size - HEADER_SIZE > 0xFFFFFFFF:
        raise ValueError("font is too large")
    values = dict(_PROFILE_CONSTANTS)
    values.update(
        state=state,
        h=height,
        qyt=qyt,
        zimoascbeg=zimoascbeg,
        datasize=total_size - HEADER_SIZE,
        dataaddr=HEADER_SIZE,
        encodenamebeg=len(name_raw),
        fontdataadd8byte=state,
        trueziqty=present,
    )
    try:
        header = struct.pack(HEADER_FORMAT, *(values[f] for f in HEADER_FIELDS))
    except struct.error as exc:
        raise ValueError("font header field does not fit") from exc
    out = bytearray(header)
    out.extend(prefix)
    for entry in entries:
        out.extend(entry)
    if state == 0:
        out.extend(b"\0\0")
    out.extend(payload)
    result = bytes(out)
    # Keep this public primitive strict: malformed output must never escape.
    validate_font(result)
    return result


def _read_template(template) -> bytes:
    if isinstance(template, (bytes, bytearray, memoryview)):
        return bytes(template)
    try:
        path = os.fspath(template)
    except TypeError as exc:
        raise ValueError("template must be .zi bytes or a path") from exc
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as exc:
        raise ValueError("cannot read template: %s" % exc) from exc


def _entry_info(data: bytes, header: Mapping[str, int], code: int):
    base = HEADER_SIZE + header["zimoascbeg"]
    index = code - 32 if header["state"] == 0 else code
    if not 0 <= index < header["qyt"]:
        return None
    code_stored, width, left, right, address_raw, size = struct.unpack_from("<HBBB3sH", data, base + 10 * index)
    if not size:
        return None
    shift = 3 if header["fontdataadd8byte"] else 0
    absolute = base + (int.from_bytes(address_raw, "little") << shift)
    return {
        "code": code_stored, "width": width, "left": left, "right": right,
        "address": absolute, "size": size,
    }


def _present_codes(data: bytes, header: Mapping[str, int]) -> list[int]:
    if header["state"] == 0:
        codes = range(32, 127)
    else:
        codes = range(65536)
    out = []
    for code in codes:
        if _entry_info(data, header, code) is not None:
            out.append(code)
    return out


def _header_from_bytes(data: bytes) -> dict:
    if len(data) < HEADER_SIZE:
        raise ValueError("truncated .zi header")
    try:
        values = struct.unpack_from(HEADER_FORMAT, data, 0)
    except struct.error as exc:
        raise ValueError("truncated .zi header") from exc
    return dict(zip(HEADER_FIELDS, values))


def validate_font(data) -> dict:
    """Validate a ``.zi`` and return its named header dictionary.

    Validation includes exact table bounds, profile flags, code/index
    agreement for present entries, payload overlap/alignment, shared payloads,
    and exact token expansion (no stream truncation or padding).
    """
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError("font data must be bytes")
    data = bytes(data)
    header = _header_from_bytes(data)
    if header["datasize"] != len(data) - HEADER_SIZE:
        raise ValueError("font datasize does not match file length")
    for field, expected in _PROFILE_CONSTANTS.items():
        if header[field] != expected:
            raise ValueError("unsupported .zi header field %s=%r" % (field, header[field]))
    if header["state"] not in (0, 1):
        raise ValueError("unsupported .zi state")
    expected_qyt = 95 if header["state"] == 0 else 65536
    if header["qyt"] != expected_qyt:
        raise ValueError("unsupported .zi glyph table size")
    if header["h"] == 0:
        raise ValueError("font height is zero")
    if header["fontdataadd8byte"] != header["state"]:
        raise ValueError("unsupported font address mode")
    if header["dataaddr"] != HEADER_SIZE or header["zimobinbeg"] != 0:
        raise ValueError("unsupported .zi data address")
    if header["encodenamebeg"] == 0:
        raise ValueError("font name is empty")
    name_end = HEADER_SIZE + header["encodenamebeg"]
    base = HEADER_SIZE + header["zimoascbeg"]
    if name_end > base or base > len(data):
        raise ValueError("font name/encoding bounds are malformed")
    try:
        name = data[HEADER_SIZE:name_end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("font name is not UTF-8") from exc
    if not name or b"\0" in data[HEADER_SIZE:name_end]:
        raise ValueError("font name is malformed")
    encoding = data[name_end:base]
    if encoding != _ENCODING:
        raise ValueError("unsupported font encoding name")
    table_end = base + 10 * expected_qyt
    payload_start = table_end + (2 if header["state"] == 0 else 0)
    if payload_start > len(data):
        raise ValueError("truncated glyph table")
    if any(data[table_end:payload_start]):
        raise ValueError("non-zero initial table padding")
    if header["zimoascbeg"] != header["encodenamebeg"] + len(_ENCODING):
        raise ValueError("font name and encoding are not contiguous")

    present = 0
    ranges: dict[tuple[int, int], tuple[int, int]] = {}
    all_ranges: list[tuple[int, int, int]] = []
    shift = 3 if header["fontdataadd8byte"] else 0
    for index in range(expected_qyt):
        p = base + 10 * index
        code, width, left, right, address_raw, size = struct.unpack_from("<HBBB3sH", data, p)
        if not size:
            # Historical absent entries may retain a default width; only the
            # size and address need be empty.
            if address_raw != b"\0\0\0":
                raise ValueError("absent glyph %d has a payload address" % index)
            continue
        expected_code = index + 32 if header["state"] == 0 else index
        if code != expected_code:
            raise ValueError("present glyph index %d stores code U+%04X" % (index, code))
        total_width = width + left + right
        if total_width <= 0:
            raise ValueError("present glyph U+%04X has zero bitmap width" % code)
        absolute = base + (int.from_bytes(address_raw, "little") << shift)
        if header["fontdataadd8byte"] and (absolute - base) % 8:
            raise ValueError("BMP glyph U+%04X has an unaligned relative address" % code)
        end = absolute + size
        if absolute < payload_start or end > len(data):
            raise ValueError("glyph U+%04X payload is out of bounds" % code)
        stream = data[absolute:end]
        try:
            _strict_decode_stream(stream, total_width * header["h"])
        except ValueError as exc:
            raise ValueError("glyph U+%04X: %s" % (code, exc)) from exc
        key = (absolute, size)
        previous = ranges.get(key)
        dimensions = (total_width, header["h"])
        if previous is not None and previous != dimensions:
            raise ValueError("shared payload has incompatible glyph dimensions")
        ranges[key] = dimensions
        all_ranges.append((absolute, end, code))
        present += 1

    if present != header["trueziqty"]:
        raise ValueError("trueziqty does not match present glyph count")
    if all_ranges:
        unique = sorted((start, end) for start, end, _ in all_ranges)
        # Shared entries collapse to the same range.  Distinct ranges may be
        # separated by zero alignment padding but may not overlap.
        merged: list[tuple[int, int]] = []
        for start, end in unique:
            if merged and start < merged[-1][1]:
                if start == merged[-1][0] and end == merged[-1][1]:
                    continue
                raise ValueError("glyph payloads overlap")
            if merged and start > merged[-1][1]:
                gap = data[merged[-1][1]:start]
                if any(gap):
                    raise ValueError("non-zero glyph payload padding")
            merged.append((start, end))
        if merged[0][0] != payload_start:
            gap = data[payload_start:merged[0][0]]
            if any(gap):
                raise ValueError("non-zero initial glyph payload padding")
        if merged[-1][1] != len(data):
            raise ValueError("trailing bytes after glyph payloads")
    elif len(data) != payload_start:
        raise ValueError("font with no glyphs has trailing data")

    header["name"] = name
    header["encoding"] = encoding.decode("ascii")
    return header


def parse_chars(value) -> list[int]:
    """Parse a literal charset or simple code-point/range specification.

    Literal strings retain spaces and punctuation.  Specifications containing
    ``U+XXXX``, ``0xXXXX`` or decimal/range tokens are also accepted, e.g.
    ``U+0020-U+007E,U+03A9``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value
        # Treat explicit code-point syntax as a specification.  Plain text is
        # deliberately literal so a requested space cannot disappear.
        if re.search(r"(?:U\+|u\+|0[xX])[0-9A-Fa-f]+", text) or re.search(r"[0-9]+\s*[-:]\s*[0-9]+", text):
            tokens = [t for t in re.split(r"[,;\s]+", text.strip()) if t]
            result: list[int] = []
            for token in tokens:
                token = token.replace("U+", "0x").replace("u+", "0x")
                if "-" in token or ":" in token:
                    parts = re.split("[-:]", token, maxsplit=1)
                    if len(parts) != 2:
                        raise ValueError("invalid charset range %r" % token)
                    a, b = (_parse_codepoint(x) for x in parts)
                    if b < a:
                        raise ValueError("descending charset range %r" % token)
                    result.extend(range(a, b + 1))
                else:
                    result.append(_parse_codepoint(token))
            return _unique_codes(result)
        return _unique_codes(map(ord, text))
    try:
        return _unique_codes(value)
    except TypeError as exc:
        raise ValueError("chars must be text or an iterable of code points") from exc


def _parse_codepoint(token: str) -> int:
    try:
        return int(token, 0)
    except ValueError:
        if token.isdigit():
            return int(token, 10)
        raise ValueError("invalid code point %r" % token)


def _unique_codes(values) -> list[int]:
    out = []
    seen = set()
    for value in values:
        if isinstance(value, str):
            if len(value) != 1:
                raise ValueError("charset string items must be one character")
            value = ord(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0x10FFFF:
            raise ValueError("invalid Unicode code point %r" % (value,))
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _font_family(tt) -> str | None:
    if "name" not in tt:
        return None
    candidates = []
    for record in tt["name"].names:
        if record.nameID not in (1, 4, 6):
            continue
        try:
            value = record.toUnicode()
        except Exception:
            continue
        if value:
            candidates.append((0 if record.nameID == 1 else 1, value))
    return min(candidates)[1] if candidates else None


def _load_font_modules():
    try:
        from fontTools.ttLib import TTFont
    except ImportError as exc:
        raise RuntimeError("fontTools is required for TTF/OTF encoding") from exc
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Pillow is required for TTF/OTF encoding") from exc
    return TTFont, Image, ImageDraw, ImageFont


def _check_font_kind(tt) -> None:
    if "fvar" in tt:
        raise ValueError("variable fonts are not supported")
    unsupported = {"COLR", "CPAL", "CBDT", "CBLC", "sbix", "SVG "}
    found = unsupported.intersection(tt.keys())
    if found:
        raise ValueError("color/bitmap fonts are not supported: %s" % ", ".join(sorted(found)))
    if "cmap" not in tt:
        raise ValueError("font has no cmap")


def _fit_size(path, font_index: int, height: int, ImageFont) -> tuple[object, int]:
    def load(size):
        try:
            return ImageFont.truetype(path, size=size, index=font_index)
        except (OSError, ValueError) as exc:
            raise ValueError("cannot load font with FreeType: %s" % exc) from exc

    def fits(font):
        asc, desc = font.getmetrics()
        return asc + desc <= height

    best = None
    size = 1
    while size <= max(64, height * 8 + 32):
        font = load(size)
        if not fits(font):
            break
        best = (font, size)
        size *= 2
    if best is None:
        raise ValueError("font height is too small for FreeType metrics")
    lo = best[1]
    hi = size
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        font = load(mid)
        if fits(font):
            best = (font, mid)
            lo = mid
        else:
            hi = mid
    return best


def _rasterize_char(ch: str, font, height: int, bpp: int, Image, ImageDraw) -> dict:
    try:
        advance = font.getlength(ch)
        bbox = font.getbbox(ch, anchor="ls")
    except (OSError, ValueError) as exc:
        raise ValueError("cannot rasterize U+%04X: %s" % ord(ch,)) from exc
    width = int(round(advance))
    if width < 0 or width > 255:
        raise ValueError("advance for U+%04X does not fit u8" % ord(ch))
    if width == 0:
        raise ValueError("zero advance for U+%04X" % ord(ch))
    if bbox is None:
        bbox = (0, 0, width, 0)
    x0, y0, x1, y1 = (int(v) for v in bbox)
    left = max(0, -x0)
    right = max(0, x1 - width)
    if left > 255 or right > 255 or width + left + right > 255:
        raise ValueError("horizontal metrics for U+%04X do not fit u8" % ord(ch))
    asc, desc = font.getmetrics()
    line_height = asc + desc
    top_pad = (height - line_height) // 2
    baseline = top_pad + asc
    total_width = width + left + right
    # Render with a guard border.  ImageDraw otherwise clips silently, which
    # would turn a bad size/metric choice into an apparently valid tofu glyph.
    margin = max(8, getattr(font, "size", 16) * 2)
    canvas = Image.new("L", (total_width + 2 * margin, height + 2 * margin), 0)
    draw = ImageDraw.Draw(canvas)
    draw.text((margin + left, margin + baseline), ch, font=font, fill=255, anchor="ls")
    used = canvas.getbbox()
    if used is not None:
        if used[0] < margin or used[2] > margin + total_width or used[1] < margin or used[3] > margin + height:
            raise ValueError("raster for U+%04X would be clipped" % ord(ch))
    crop = canvas.crop((margin, margin, margin + total_width, margin + height))
    get_flattened_data = getattr(crop, "get_flattened_data", None)
    raw = list(get_flattened_data() if get_flattened_data is not None else crop.getdata())
    if bpp == 1:
        alpha = [255 if value >= 128 else 0 for value in raw]
    else:
        alpha = [round(round(value * 7 / 255) * 255 / 7) for value in raw]
    return {"width": width, "left": left, "right": right, "alpha": alpha}


def encode_ttf(path, *, height=None, chars=None, name=None, template=None, bpp=None,
               size=None, font_index=0, layout=None) -> bytes:
    """Rasterise a TTF/CFF OTF/TTC and return a validated ``.zi`` font.

    ``template`` may be raw ``.zi`` bytes or a path.  When supplied, omitted
    height/name/layout/bpp values inherit the supported original profile.  If
    ``chars`` is omitted with a template, every present original code is
    requested and a missing cmap entry is an error.  Without a template the
    default charset is printable ASCII and height defaults to 24.
    """
    try:
        font_path = os.fspath(path)
    except TypeError as exc:
        raise ValueError("font path is not path-like") from exc
    if not os.path.isfile(font_path):
        raise ValueError("font path does not exist: %s" % font_path)
    if isinstance(font_index, bool) or not isinstance(font_index, int) or font_index < 0:
        raise ValueError("font_index must be a non-negative integer")

    template_data = None
    template_header = None
    if template is not None:
        template_data = _read_template(template)
        template_header = validate_font(template_data)
    if height is None:
        height = template_header["h"] if template_header is not None else 24
    if name is None:
        name = template_header["name"] if template_header is not None else None
    if layout is None:
        layout = ("bmp" if template_header["state"] else "ascii") if template_header is not None else "ascii"
    if bpp is None:
        if template_data is not None:
            # Existing profiles use both codecs.  Prefer the antialiased codec
            # if any present stream uses it; all generated streams are uniform.
            h = template_header
            bpp = 1
            for code in _present_codes(template_data, h):
                entry = _entry_info(template_data, h, code)
                if entry and template_data[entry["address"]] == 3:
                    bpp = 3
                    break
        else:
            bpp = 3
    layout = _normalise_layout(layout)
    bpp = _normalise_bpp(bpp)
    if isinstance(height, bool) or not isinstance(height, int) or not 1 <= height <= 255:
        raise ValueError("height must be an integer in 1..255")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        raise ValueError("size must be a positive integer")

    if chars is None:
        if template_data is not None:
            selected = _present_codes(template_data, template_header)
            allow_controls = layout == "bmp"
        else:
            selected = list(range(32, 127))
            allow_controls = False
    else:
        selected = parse_chars(chars)
        allow_controls = False
    for code in selected:
        _validate_code(code, layout, allow_controls=allow_controls)
    TTFont, Image, ImageDraw, ImageFont = _load_font_modules()
    try:
        try:
            tt = TTFont(font_path, fontNumber=font_index, lazy=False)
        except (OSError, ValueError, IndexError) as exc:
            raise ValueError("cannot open font: %s" % exc) from exc
        try:
            _check_font_kind(tt)
            cmap = tt.getBestCmap() or {}
            family = _font_family(tt)
        finally:
            tt.close()
        if not selected:
            # Still load/rasterise no glyphs only to make sure the font is a
            # valid supported font; family remains useful for default naming.
            font = None
            chosen_size = size
        elif size is None:
            font, chosen_size = _fit_size(font_path, font_index, height, ImageFont)
        else:
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError("size must be a positive integer")
            try:
                font = ImageFont.truetype(font_path, size=size, index=font_index)
            except (OSError, ValueError) as exc:
                raise ValueError("cannot load font with FreeType: %s" % exc) from exc
            asc, desc = font.getmetrics()
            if asc + desc > height:
                raise ValueError("explicit FreeType size does not fit requested height")
            chosen_size = size
        if name is None:
            name = family or "Custom"
        if not isinstance(name, str) or not name:
            name = "Custom"

        result = {}
        for code in selected:
            glyph_name = cmap.get(code)
            if glyph_name is None or glyph_name == ".notdef":
                raise ValueError("font cmap is missing U+%04X" % code)
            ch = chr(code)
            # Keep explicit size checking useful even for a zero-glyph charset.
            if font is None:
                continue
            result[code] = _rasterize_char(ch, font, height, bpp, Image, ImageDraw)
        return encode_font(result, height, name, layout, bpp, _allow_controls=allow_controls)
    finally:
        # TTFont is closed above.  Pillow's FreeTypeFont owns a small native
        # handle but has no explicit close API on all supported Pillow builds.
        pass


def _read_chars_file(path: str) -> list[int]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("cannot read charset file: %s" % exc) from exc
    return parse_chars(text.replace("\r", "").replace("\n", ""))


def decode_glyph(stream: bytes, width: int, height: int) -> tuple[list[list[int]], int]:
    """Decode one ``type-byte + token stream`` into visual rows of 0..255 alpha.

    ``width`` is the complete bitmap width (advance + left + right).  Returns
    ``(rows, bpp)``.  Inverse of :func:`encode_glyph`.
    """
    levels = _strict_decode_stream(stream, width * height)
    bpp = bytes(stream)[0]
    scale = (lambda v: round(v * 255 / 7)) if bpp == 3 else (lambda v: 255 if v else 0)
    rows = [[scale(levels[(width - 1 - x) * height + y]) for x in range(width)] for y in range(height)]
    return rows, bpp


def decode_font(data) -> dict:
    """Decode a ``.zi`` into ``{name, height, layout, bpp, glyphs}``.

    ``glyphs`` maps code points to ``{'width', 'left', 'right', 'alpha'}`` with
    visual row-major alpha rows, i.e. the input accepted by :func:`encode_font`.
    The font is validated first.
    """
    data = bytes(data)
    header = validate_font(data)
    glyphs = {}
    bpps = set()
    for code in _present_codes(data, header):
        info = _entry_info(data, header, code)
        total = info["width"] + info["left"] + info["right"]
        stream = data[info["address"]:info["address"] + info["size"]]
        rows, bpp = decode_glyph(stream, total, header["h"])
        bpps.add(bpp)
        glyphs[code] = {"width": info["width"], "left": info["left"], "right": info["right"], "alpha": rows}
    return {
        "name": header["name"], "height": header["h"],
        "layout": "ascii" if header["state"] == 0 else "bmp",
        "bpp": 3 if 3 in bpps or not bpps else 1, "glyphs": glyphs,
    }


class FontReader:
    """Lazy reader of a ``.zi``: glyphs are decoded on first use (no full validation).

    ``glyph(code)`` returns ``{'width', 'left', 'right', 'alpha'}`` (visual rows of 0..255) or None when the font has no
    such character.  Meant for renderers (the HMI emulator); use :func:`decode_font` to read the whole font.
    """

    def __init__(self, data):
        self.data = bytes(data)
        self.header = _header_from_bytes(self.data)
        if self.header["state"] not in (0, 1) or self.header["h"] == 0:
            raise ValueError("unsupported .zi header")
        self.height = self.header["h"]
        self._cache = {}

    def glyph(self, code):
        if code in self._cache:
            return self._cache[code]
        info = _entry_info(self.data, self.header, code)
        g = None
        if info is not None:
            total = info["width"] + info["left"] + info["right"]
            stream = self.data[info["address"]:info["address"] + info["size"]]
            try:
                rows, _ = decode_glyph(stream, total, self.height)
                g = {"width": info["width"], "left": info["left"], "right": info["right"], "alpha": rows}
            except ValueError:
                g = None
        self._cache[code] = g
        return g


def export_font(data, directory) -> int:
    """Write ``font.json`` and one ``U+XXXX.png`` per glyph; needs Pillow."""
    import json
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required to write glyph PNGs") from exc
    font = decode_font(data)
    out = Path(directory)
    (out / "glyphs").mkdir(parents=True, exist_ok=True)
    meta = {k: font[k] for k in ("name", "height", "layout", "bpp")}
    meta["glyphs"] = {}
    for code, g in sorted(font["glyphs"].items()):
        name = "U+%04X.png" % code
        w = g["width"] + g["left"] + g["right"]
        img = Image.frombytes("L", (w, font["height"]), bytes(v for row in g["alpha"] for v in row))
        img.save(out / "glyphs" / name)
        meta["glyphs"]["%04X" % code] = {"width": g["width"], "left": g["left"], "right": g["right"], "file": "glyphs/" + name}
    (out / "font.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return len(font["glyphs"])


def import_font(directory) -> bytes:
    """Rebuild a ``.zi`` from a directory written by :func:`export_font`."""
    import json
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError("Pillow is required to read glyph PNGs") from exc
    out = Path(directory)
    meta = json.loads((out / "font.json").read_text(encoding="utf-8"))
    glyphs = {}
    for hexcode, g in meta["glyphs"].items():
        img = Image.open(out / g["file"]).convert("L")
        w, h = img.size
        pix = list(img.tobytes())
        glyphs[int(hexcode, 16)] = {
            "width": g["width"], "left": g["left"], "right": g["right"],
            "alpha": [pix[y * w:(y + 1) * w] for y in range(h)],
        }
    return encode_font(glyphs, meta["height"], meta["name"], meta["layout"], meta["bpp"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Encode USART HMI .zi fonts without Wine")
    sub = parser.add_subparsers(dest="command", required=True)
    enc = sub.add_parser("encode", help="encode a TTF/OTF/TTC into a .zi font")
    enc.add_argument("input")
    enc.add_argument("output")
    enc.add_argument("--height", type=int)
    chars = enc.add_mutually_exclusive_group()
    chars.add_argument("--chars")
    chars.add_argument("--chars-file")
    enc.add_argument("--layout", choices=("ascii", "bmp"))
    enc.add_argument("--bpp", type=int, choices=(1, 3))
    enc.add_argument("--size", type=int)
    enc.add_argument("--font-index", type=int, default=0)
    enc.add_argument("--name")
    dec = sub.add_parser("decode", help="decode a .zi into font.json + glyph PNGs")
    dec.add_argument("input")
    dec.add_argument("directory")
    rebuild = sub.add_parser("rebuild", help="build a .zi from a directory written by decode")
    rebuild.add_argument("directory")
    rebuild.add_argument("output")
    args = parser.parse_args(argv)
    if args.command == "decode":
        try:
            print("%d glyphs" % export_font(Path(args.input).read_bytes(), args.directory))
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "rebuild":
        output = Path(args.output)
        if output.exists():
            parser.error("refusing to overwrite existing output: %s" % output)
        try:
            data = import_font(args.directory)
            with output.open("xb") as f:
                f.write(data)
        except (OSError, ValueError, KeyError) as exc:
            parser.error(str(exc))
        return 0
    if args.command == "encode":
        output = Path(args.output)
        if output.exists():
            parser.error("refusing to overwrite existing output: %s" % output)
        try:
            selected = _read_chars_file(args.chars_file) if args.chars_file else args.chars
            data = encode_ttf(
                args.input, height=args.height, chars=selected, name=args.name,
                bpp=args.bpp, size=args.size, font_index=args.font_index,
                layout=args.layout,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as f:
                f.write(data)
        except (OSError, ValueError, RuntimeError) as exc:
            parser.error(str(exc))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
