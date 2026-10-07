#!/usr/bin/env python3
"""Animation encoder: PNG frames -> compiled `.gmov` + source `.gmovs` (standard library; Pillow only to read the PNGs).

python3 hmi_anim.py encode --ms 500 frame0.png frame1.png ... out.gmov     # also writes out.gmovs

Layout (little endian), as found in USART HMI 1.68.1 projects:

  header, 76 bytes: 'GMOV', dire, quality, fps, 0 | dataaddr=76 | frames | w:u16 h:u16 | showpic=0 |
                    videodatasize (file size - 76) | wavaddr=0 | wavsize=0 | stim (total ms) |
                    extmessageaddr (videodatasize - 4096) | extmessagesize=4096 | 4 flag bytes | zeros
  frame table, 40 bytes per frame: flags | 0 | offset (from dataaddr) | w | h<<16 | length | alpha offset (compiled) or 0
                    (source) | duration ms | start ms | 0 | 0
  frames back to back; then the 4096-byte extension block 'hmic' + u32 4088 + zeros.
  .gmov  : header dire 13, quality 82, flags 01 01 00 00, table flags 0x0400000d; each frame is a picture payload
           (hmi_image.encode_payload: format 2, RGB565 palette blocks, alpha stream stored for every frame).
  .gmovs : header dire 10, quality 86, flags 00 01 00 00, table flags 0x0200000a; each frame is a .NET serialized
           System.Drawing.Bitmap holding the PNG: 156 constant bytes, u32 PNG length, 0x02, the PNG, 0x0b.

The source writer reproduces the editor's `.gmovs` byte for byte (checked on all 12 animations of the sample project).
The compiled frames use this repository's picture encoder, so they are not byte-identical to the editor's compressor.
"""
import argparse
import json
import struct
from pathlib import Path

import hmi_image as IMG
import hmi_parse as H

HEADER_SIZE, ENTRY_SIZE, EXT_SIZE = 76, 40, 4096
EXT_BLOCK = b'hmic' + struct.pack('<I', EXT_SIZE - 8) + bytes(EXT_SIZE - 8)
SOURCE_FIXED = bytes.fromhex(
    '0001000000ffffffff01000000000000000c020000005153797374656d2e44726177696e672c2056657273696f6e3d322e302e302e302c2043756c747572'
    '653d6e65757472616c2c205075626c69634b6579546f6b656e3d62303366356637663131643530613361050100000015'
    '53797374656d2e44726177696e672e4269746d617001000000044461746107020200000009030000000f03000000')
PNG_END = b'IEND\xaeB`\x82'
OPAQUE_FLAGS = b'\x00\x01\x00\x00'   # compiled .gmov without any alpha stream (seen in V4.4.24 anim_6)
MAX_FRAMES, MAX_PIXELS = 255, 4 * 1024 * 1024
KINDS = {'gmov': dict(dire=13, quality=82, flags=b'\x01\x01\x00\x00', table=0x0400000d),
         'gmovs': dict(dire=10, quality=86, flags=b'\x00\x01\x00\x00', table=0x0200000a)}


def _header(kind, fps, frames, w, h, total_size, total_ms, opaque=False):
    k = KINDS[kind]
    videodatasize = total_size - HEADER_SIZE
    head = (b'GMOV' + bytes((k['dire'], k['quality'], fps, 0)) +
            struct.pack('<IIHHIIIIIII', HEADER_SIZE, frames, w, h, 0, videodatasize, 0, 0, total_ms,
                        videodatasize - EXT_SIZE, EXT_SIZE) + (OPAQUE_FLAGS if opaque else k['flags']))
    return head.ljust(HEADER_SIZE, b'\0')


def _assemble(kind, fps, w, h, frame_data, alpha_offsets, durations, opaque=False):
    n = len(frame_data)
    table, offset, start = b'', ENTRY_SIZE * n, 0
    for data, alpha, ms in zip(frame_data, alpha_offsets, durations):
        table += struct.pack('<10I', KINDS[kind]['table'], 0, offset, w | h << 16, len(data), alpha, ms, start, 0, 0)
        offset += len(data)
        start += ms
    body = table + b''.join(frame_data) + EXT_BLOCK
    return _header(kind, fps, n, w, h, HEADER_SIZE + len(body), start, opaque) + body


def _check(frames, durations, fps):
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ValueError('An animation needs 1..%d frames' % MAX_FRAMES)
    if isinstance(durations, int) and not isinstance(durations, bool):
        durations = [durations] * len(frames)
    durations = list(durations)
    if len(durations) != len(frames):
        raise ValueError('One duration per frame is needed')
    for ms in durations:
        if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= 0xffff:
            raise ValueError('Frame durations are integers 1..65535 ms')
    if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 255:
        raise ValueError('fps is an integer 1..255')
    return durations


def build_source(pngs, durations, fps=10):
    """The `.gmovs` source animation for PNG frames of equal size."""
    durations = _check(pngs, durations, fps)
    sizes = {IMG._png_size(p) for p in pngs}
    if len(sizes) != 1:
        raise ValueError('All frames must have the same size')
    (w, h), = sizes
    data = [SOURCE_FIXED + struct.pack('<I', len(p)) + b'\x02' + bytes(p) + b'\x0b' for p in pngs]
    return _assemble('gmovs', fps, w, h, data, [0] * len(pngs), durations)


def encode_animation(pngs, durations, fps=10, opaque=False):
    """Return (gmov, gmovs) for PNG frames. Pillow is needed to read the pixels.

    `opaque` writes the variant without alpha streams (header flags 00 01 00 00); every pixel must then be opaque."""
    durations = _check(pngs, durations, fps)
    frames = [IMG.load_png(p) for p in pngs]
    if len({(f[0], f[1]) for f in frames}) != 1:
        raise ValueError('All frames must have the same size')
    w, h = frames[0][0], frames[0][1]
    if w * h > MAX_PIXELS:
        raise ValueError('Frames are too large')
    if opaque and any(a != 255 for f in frames if f[3] is not None for a in f[3]):
        raise ValueError('An opaque animation cannot have transparent pixels')
    payloads = [IMG.encode_payload(w, h, f[2], None if opaque else f[3], always_alpha=not opaque) for f in frames]
    gmov = _assemble('gmov', fps, w, h, [p for p, _ in payloads], [a for _, a in payloads], durations, opaque)
    gmovs = build_source(pngs, durations, fps)
    validate_animation(gmov)
    validate_source(gmovs)
    return gmov, gmovs


def _parse(data, kind):
    data = bytes(data)
    if len(data) < HEADER_SIZE + ENTRY_SIZE + EXT_SIZE or data[:4] != b'GMOV':
        raise ValueError('Not a %s animation' % kind)
    k = KINDS[kind]
    dire, quality, fps, vtype = data[4:8]
    dataaddr, n, w, h, showpic, size, wav, wavsize, stim, extaddr, extsize = struct.unpack_from('<IIHHIIIIIII', data, 8)
    if (dire, quality, vtype, dataaddr, showpic, wav, wavsize, extsize) != (k['dire'], k['quality'], 0, HEADER_SIZE, 0, 0, 0, EXT_SIZE):
        raise ValueError('Unsupported animation header profile')
    opaque = kind == 'gmov' and data[48:52] == OPAQUE_FLAGS
    if (data[48:52] != k['flags'] and not opaque) or data[52:HEADER_SIZE] != bytes(24):
        raise ValueError('Unsupported animation header flags')
    if not 1 <= n <= MAX_FRAMES or size != len(data) - HEADER_SIZE or extaddr != size - EXT_SIZE:
        raise ValueError('Inconsistent animation header')
    if data[-EXT_SIZE:] != EXT_BLOCK:
        raise ValueError('Unsupported animation extension block')
    frames, expect_off, start = [], ENTRY_SIZE * n, 0
    for i in range(n):
        e = struct.unpack_from('<10I', data, HEADER_SIZE + ENTRY_SIZE * i)
        flags, zero, off, wh, length, alpha, ms, st, z1, z2 = e
        if flags != k['table'] or (zero, z1, z2) != (0, 0, 0) or wh != (w | h << 16) or off != expect_off or st != start:
            raise ValueError('Frame %d has an unsupported table entry' % i)
        pos = HEADER_SIZE + off
        if pos + length > len(data) - EXT_SIZE:
            raise ValueError('Frame %d is outside the animation' % i)
        frames.append(dict(data=data[pos:pos + length], alpha_offset=alpha, duration_ms=ms))
        expect_off += length
        start += ms
    if HEADER_SIZE + expect_off != len(data) - EXT_SIZE or stim != start:
        raise ValueError('Frames do not fill the animation exactly')
    return dict(fps=fps, w=w, h=h, frames=frames, opaque=opaque)


def validate_animation(data):
    """Strict check of a compiled `.gmov`; every frame must decode to exactly w*h pixels."""
    info = _parse(data, 'gmov')
    for i, f in enumerate(info['frames']):
        if info['opaque']:
            if f['alpha_offset']:
                raise ValueError('Frame %d of an opaque animation has an alpha block' % i)
        elif not 0 < f['alpha_offset'] < len(f['data']):
            raise ValueError('Frame %d has no alpha block' % i)
        px = H.decode_payload(f['data'], info['w'], info['h'], f['alpha_offset'])
        if len(px['rgb565']) != info['w'] * info['h']:
            raise ValueError('Frame %d does not decode to the frame size' % i)
    return info


def validate_source(data):
    """Strict check of a `.gmovs`; returns fps, size, durations and the PNG frames."""
    info = _parse(data, 'gmovs')
    pngs = []
    for i, f in enumerate(info['frames']):
        d = f['data']
        n = len(SOURCE_FIXED)
        if f['alpha_offset'] or d[:n] != SOURCE_FIXED or d[-1:] != b'\x0b' or d[n + 4:n + 5] != b'\x02':
            raise ValueError('Frame %d is not a serialized PNG bitmap' % i)
        (plen,) = struct.unpack_from('<I', d, n)
        png = d[n + 5:-1]
        if plen != len(png) or IMG._png_size(png) != (info['w'], info['h']) or not png.endswith(PNG_END):
            raise ValueError('Frame %d has an invalid PNG' % i)
        pngs.append(png)
    info['pngs'] = pngs
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='action', required=True)
    enc = sub.add_parser('encode', help='PNG frames -> OUT.gmov and OUT.gmovs')
    enc.add_argument('frames', nargs='+', help='PNG frames followed by the output .gmov path')
    enc.add_argument('--ms', type=int, default=500, help='duration of every frame')
    enc.add_argument('--fps', type=int, default=10)
    args = ap.parse_args()
    try:
        *pngs, out = args.frames
        if not pngs or not out.endswith('.gmov'):
            raise ValueError('Give at least one PNG frame and an output path ending in .gmov')
        out = Path(out)
        companion = out.with_suffix('.gmovs')
        for p in (out, companion):
            if p.exists() or p.is_symlink():
                raise FileExistsError('Output already exists: %s' % p)
        gmov, gmovs = encode_animation([Path(p).read_bytes() for p in pngs], args.ms, args.fps)
        with out.open('xb') as f:
            f.write(gmov)
        with companion.open('xb') as f:
            f.write(gmovs)
        print(json.dumps({'gmov': str(out), 'gmovs': str(companion), 'frames': len(pngs), 'bytes': len(gmov)}, indent=2))
    except (OSError, ValueError) as e:
        ap.exit(1, 'Error: %s\n' % e)


if __name__ == '__main__':
    main()
