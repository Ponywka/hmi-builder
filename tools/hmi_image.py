#!/usr/bin/env python3
"""Offline PNG -> USART HMI .i/.is encoder for qumo=13 palette pictures.

python3 hmi_image.py encode input.png output.i

Pillow is needed only to read PNG pixels. The binary codec and validators use the
standard library. RGB is truncated to RGB565; alpha is quantized to seven bits.
Outputs are created exclusively. This does not encode animations or .tft files.
"""
import argparse
import io
import json
from pathlib import Path
import struct
import zlib

import hmi_parse as H

HEADER = struct.Struct('<4BH2BIHHIi')
PAYLOAD_TAG = 0x05ddc33c
PNG = b'\x89PNG\r\n\x1a\n'
MAX_PIXELS = 4 * 1024 * 1024
PALETTE_LIMIT = 256


def _integer(value, low, high, label):
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError('%s must be an integer in %d..%d' % (label, low, high))
    return value


def _dimensions(w, h):
    _integer(w, 1, 65535, 'Width')
    _integer(h, 1, 65535, 'Height')
    if w * h > MAX_PIXELS:
        raise ValueError('Picture exceeds the %d-pixel encoder limit' % MAX_PIXELS)


def _header(data, source=False):
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < HEADER.size:
        raise ValueError('Truncated picture header')
    hd = dict(zip(H.PICTURE_HEADER, HEADER.unpack_from(data)))
    qumo, kind, address = (10, 1, 27) if source else (13, 4, 24)
    if (hd['qumo'], hd['picdatatype'], hd['dataaddr']) != (qumo, kind, address):
        raise ValueError('Unsupported picture profile: qumo=%d type=%d dataaddr=%d' %
                         (hd['qumo'], hd['picdatatype'], hd['dataaddr']))
    if hd['encodeen'] or hd['res1'] or hd['alphaen'] not in (0, 1) or not 1 <= hd['quality'] <= 100:
        raise ValueError('Unsupported picture encoding/flags/quality')
    _dimensions(hd['w'], hd['h'])
    if hd['imgbytesize'] != len(data) - address:
        raise ValueError('Picture imgbytesize does not match the stored payload')
    if hd['alphaaddr'] < 0 or (hd['alphaaddr'] and not hd['alphaen']):
        raise ValueError('Invalid picture alpha offset/flag')
    return hd


def _colour_stream(data, end, total):
    """Strict native-compatible decoding; unlike H._blocks, never substitute or truncate."""
    pos, base, pixels = 20, 0, []
    while len(pixels) < total:
        if pos + 5 > end:
            raise ValueError('Truncated palette block')
        zero, count, ib = struct.unpack_from('<HHB', data, pos)
        # The native decoder has only five- and six-bit token branches.
        if zero or ib not in (5, 6) or not 1 <= count <= min(2048, 1 << (2 * ib)):
            raise ValueError('Invalid/unsupported palette block')
        pos += 5
        if pos + 2 * count > end:
            raise ValueError('Truncated RGB565 palette')
        palette = struct.unpack_from('<%dH' % count, data, pos)
        pos += 2 * count
        mask, threshold = (1 << ib) - 1, ((1 << (8 - ib)) - 1) << ib
        block_pixels = len(pixels)
        while len(pixels) < total:
            if pos >= end:
                raise ValueError('Truncated colour stream')
            # The next block's leading zero word is the current stream delimiter.
            if data[pos:pos + 2] == b'\0\0':
                if len(pixels) == block_pixels:
                    raise ValueError('Empty palette block')
                break
            token = data[pos]
            pos += 1
            if token >= threshold:
                base = (token & mask) << ib
                continue
            run = token >> ib
            if not run:
                if pos >= end:
                    raise ValueError('Truncated extended colour run')
                run = data[pos]
                pos += 1
                if not run:
                    raise ValueError('Noncanonical zero-length colour run')
            index = base + (token & mask)
            if index >= count:
                raise ValueError('Colour token refers outside the palette')
            if len(pixels) + run > total:
                raise ValueError('Colour run exceeds picture dimensions')
            pixels.extend([palette[index]] * run)
    if pos != end:
        raise ValueError('Trailing bytes in colour stream')
    return pixels


def _alpha_stream(data, pos, total):
    values = []
    while len(values) < total:
        if pos >= len(data):
            raise ValueError('Truncated alpha stream')
        token = data[pos]
        pos += 1
        value, run = token & 127, 1
        if not token & 128:
            if pos >= len(data):
                raise ValueError('Truncated alpha run')
            run = data[pos]
            pos += 1
            if not run:
                raise ValueError('Zero-length alpha run')
        if len(values) + run > total:
            raise ValueError('Alpha run exceeds picture dimensions')
        values.extend([value] * run)
    if pos != len(data):
        raise ValueError('Trailing bytes in alpha stream')
    return values


def validate_picture(data):
    """Validate the complete supported .i profile; return its parsed header."""
    hd = _header(data)
    payload = data[24:]
    if len(payload) < 20:
        raise ValueError('Truncated picture payload')
    fmt, sh, sw, tag, length = struct.unpack_from('<5I', payload)
    if (fmt, sh, sw, tag) != (2, hd['h'], hd['w'], PAYLOAD_TAG):
        raise ValueError('Unsupported picture payload format/dimensions/tag')
    end = 20 + length
    if end > len(payload) or length < 8:
        raise ValueError('Invalid colour stream length')
    if hd['alphaaddr'] != (end if end < len(payload) else 0):
        raise ValueError('Alpha offset does not match the colour stream end')
    colours = _colour_stream(payload, end, hd['w'] * hd['h'])
    if hd['alphaaddr']:
        _alpha_stream(payload, end, len(colours))
    return hd


def _png_size(data):
    """Check PNG framing/chunk CRCs without importing an image library."""
    if not isinstance(data, (bytes, bytearray, memoryview)) or data[:8] != PNG:
        raise ValueError('Source must be a PNG image')
    pos, dimensions, have_data, ended = 8, None, False, False
    while pos < len(data):
        if pos + 12 > len(data):
            raise ValueError('Truncated PNG chunk')
        length = struct.unpack_from('>I', data, pos)[0]
        typ = bytes(data[pos + 4:pos + 8])
        end = pos + 12 + length
        if end > len(data):
            raise ValueError('Truncated PNG chunk data')
        body = data[pos + 8:end - 4]
        expected = struct.unpack_from('>I', data, end - 4)[0]
        if zlib.crc32(data[pos + 4:end - 4]) & 0xffffffff != expected:
            raise ValueError('PNG chunk checksum mismatch')
        if dimensions is None:
            if typ != b'IHDR' or length != 13:
                raise ValueError('PNG must start with one IHDR chunk')
            w, h, depth, colour, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
            _dimensions(w, h)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(colour, ()) or compression or filtering or interlace not in (0, 1):
                raise ValueError('Unsupported PNG header')
            dimensions = w, h
        elif typ == b'IHDR':
            raise ValueError('Duplicate PNG IHDR')
        if typ == b'acTL':
            raise ValueError('Animated PNGs are not supported by the picture encoder')
        if typ == b'IDAT':
            have_data = True
        if typ == b'IEND':
            if length or end != len(data) or not have_data:
                raise ValueError('Invalid PNG end/data')
            ended = True
            break
        pos = end
    if not ended:
        raise ValueError('PNG has no complete IEND')
    return dimensions


def validate_source(data):
    """Validate .is metadata and PNG chunks; pixel decompression is checked by encode_png."""
    hd = _header(data, source=True)
    if hd['alphaaddr'] or data[24:27] != b'png':
        raise ValueError('Unsupported picture source wrapper')
    if _png_size(data[27:]) != (hd['w'], hd['h']):
        raise ValueError('PNG dimensions differ from source metadata')
    return hd


def _rotate(values, w, h):
    return [values[y * w + x] for x in range(w - 1, -1, -1) for y in range(h)]


def _encode_colours(pixels):
    out, start = bytearray(), 0
    while start < len(pixels):
        palette, indexes, end = [], {}, start
        while end < len(pixels):
            colour = pixels[end]
            if colour not in indexes:
                if len(palette) == PALETTE_LIMIT:
                    break
                indexes[colour] = len(palette)
                palette.append(colour)
            end += 1
        out.extend(struct.pack('<HHB', 0, len(palette), 5))
        out.extend(struct.pack('<%dH' % len(palette), *palette))
        # Native retains the bank across blocks; Python's historical decoder resets it.
        # An explicit reset makes the stream correct for both implementations.
        out.append(0xe0)
        pos, base = start, 0
        while pos < end:
            colour, run = pixels[pos], 1
            while pos + run < end and run < 255 and pixels[pos + run] == colour:
                run += 1
            index = indexes[colour]
            bank = index >> 5
            if bank != base:
                out.append(0xe0 | bank)
                base = bank
            if run <= 6:
                out.append((run << 5) | (index & 31))
            else:
                out.extend((index & 31, run))
            pos += run
        start = end
    return bytes(out)


def _encode_alpha(values):
    out, pos = bytearray(), 0
    while pos < len(values):
        value, run = values[pos], 1
        while pos + run < len(values) and run < 255 and values[pos + run] == value:
            run += 1
        if run == 1:
            out.append(value | 128)
        else:
            out.extend((value, run))
        pos += run
    return bytes(out)


def _make_header(w, h, size, alpha, template, source=False, picture_id=0):
    hd = _header(template, source) if template is not None else dict(
        qumo=10 if source else 13, quality=100 if source else 96, alphaen=1,
        picdatatype=1 if source else 4, pictureid=picture_id, encodeen=0, res1=0,
        dataaddr=27 if source else 24, w=w, h=h, imgbytesize=size, alphaaddr=alpha)
    if template is not None and (hd['w'], hd['h']) != (w, h):
        raise ValueError('Replacement dimensions must match the existing picture (%dx%d)' % (hd['w'], hd['h']))
    hd.update(w=w, h=h, imgbytesize=size, alphaaddr=alpha)
    if alpha and not hd['alphaen']:
        raise ValueError('Existing picture profile disables alpha')
    return HEADER.pack(*(hd[k] for k in H.PICTURE_HEADER))


def encode_picture(w, h, rgb565, alpha=None, *, template=None, picture_id=0):
    """Encode row-major RGB565 and optional 8-bit alpha with the standard library.

    Input colours are not globally palette-quantized. A new palette block is used
    after 256 distinct colours. Alpha becomes floor(a * 127 / 255), matching the
    native encoder. Decoding uses level * 2, except 127 becomes 255. Hidden RGB is zero.
    """
    _dimensions(w, h)
    _integer(picture_id, 0, 65535, 'Picture metadata ID')
    colours = list(rgb565)
    if len(colours) != w * h:
        raise ValueError('Colour count differs from picture dimensions')
    for c in colours:
        _integer(c, 0, 65535, 'RGB565 colour')
    if template is not None:
        validate_picture(template)
    payload, alpha_off = encode_payload(w, h, colours, alpha)
    output = _make_header(w, h, len(payload), alpha_off, template, picture_id=picture_id) + payload
    validate_picture(output)
    return output


def encode_payload(w, h, rgb565, alpha=None, *, always_alpha=False):
    """The picture payload (format 2) without the 24-byte file header: (payload, alpha_offset).

    Alpha is stored when any pixel is not opaque (or always with `always_alpha`, as animation frames do); an opaque
    picture has alpha offset 0. Used for pictures and for the frames of compiled animations."""
    colours = list(rgb565)
    if len(colours) != w * h:
        raise ValueError('Colour count differs from picture dimensions')
    levels = None
    if alpha is not None:
        values = list(alpha)
        if len(values) != len(colours):
            raise ValueError('Alpha count differs from picture dimensions')
        for a in values:
            _integer(a, 0, 255, 'Alpha')
        levels = [a * 127 // 255 for a in values]
        colours = [c if a else 0 for c, a in zip(colours, levels)]
        if all(a == 127 for a in levels) and not always_alpha:
            levels = None
    elif always_alpha:
        levels = [127] * len(colours)
    body = _encode_colours(_rotate(colours, w, h))
    payload = struct.pack('<5I', 2, h, w, PAYLOAD_TAG, len(body)) + body
    alpha_off = len(payload) if levels is not None else 0
    if levels is not None:
        payload += _encode_alpha(_rotate(levels, w, h))
    return payload, alpha_off


def encode_source(png_bytes, *, template=None, picture_id=0):
    """Wrap an existing single PNG as a .is, retaining the exact PNG bytes."""
    _integer(picture_id, 0, 65535, 'Picture metadata ID')
    w, h = _png_size(png_bytes)
    if template is not None:
        validate_source(template)
    output = _make_header(w, h, len(png_bytes), 0, template, source=True, picture_id=picture_id) + b'png' + bytes(png_bytes)
    validate_source(output)
    return output


def load_png(png_bytes):
    """(w, h, rgb565 list, alpha list) of a single 8-bit-or-less PNG. Only this function needs Pillow."""
    w, h = _png_size(png_bytes)
    if png_bytes[24] == 16:
        raise ValueError('16-bit PNG pixel conversion is not supported; convert the source to 8-bit PNG')
    try:
        from PIL import Image
    except ImportError as e:
        raise ValueError('PNG encoding needs Pillow: python3 -m pip install Pillow') from e
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Source must be a single PNG image')
            image.load()
            rgba = image.convert('RGBA').tobytes()
    except (OSError, SyntaxError) as e:
        raise ValueError('Cannot decode PNG pixels: ' + str(e)) from e
    colours = [((rgba[p] >> 3) << 11) | ((rgba[p + 1] >> 2) << 5) | (rgba[p + 2] >> 3)
               for p in range(0, len(rgba), 4)]
    return w, h, colours, list(rgba[3::4])


def encode_png(png_bytes, *, template=None, source_template=None, picture_id=0):
    """Return the matching (.i, .is) pair. Only the pixel-loading adapter needs Pillow."""
    w, h, colours, alpha = load_png(png_bytes)
    compiled = encode_picture(w, h, colours, alpha, template=template, picture_id=picture_id)
    source = encode_source(png_bytes, template=source_template, picture_id=picture_id)
    return compiled, source


def write_pair(output, compiled, source):
    """Publish new standalone files; never overwrite an existing output or companion."""
    output = Path(output)
    if output.suffix.lower() != '.i':
        raise ValueError('Compiled output must have the .i extension (source companion: .is)')
    companion = output.with_suffix('.is')
    for p in (output, companion):
        if p.exists() or p.is_symlink():
            raise FileExistsError('Output already exists: ' + str(p))
    hd, src = validate_picture(compiled), validate_source(source)
    if (hd['w'], hd['h']) != (src['w'], src['h']):
        raise ValueError('Compiled/source picture dimensions differ')
    created = []
    try:
        for p, data in ((output, compiled), (companion, source)):
            with p.open('xb') as f:
                created.append(p)
                f.write(data)
    except BaseException:
        for p in reversed(created):
            p.unlink()
        raise
    return output, companion


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)
    enc = sub.add_parser('encode', help='encode a single PNG as new .i and .is files')
    enc.add_argument('source')
    enc.add_argument('output')
    enc.add_argument('--picture-id', type=int, default=0, help='header metadata, not the project index ID')
    args = parser.parse_args()
    try:
        compiled, source = encode_png(Path(args.source).read_bytes(), picture_id=args.picture_id)
        output, companion = write_pair(args.output, compiled, source)
        hd = validate_picture(compiled)
        print(json.dumps({'compiled': str(output), 'source': str(companion), 'w': hd['w'], 'h': hd['h'],
                          'compiled_bytes': len(compiled), 'source_bytes': len(source)}, indent=2))
    except (OSError, ValueError) as e:
        parser.exit(1, 'Error: %s\n' % e)


if __name__ == '__main__':
    main()
