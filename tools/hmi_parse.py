#!/usr/bin/env python3
"""Parser for USART HMI / Nextion Editor project files (*.HMI), reverse engineered.

Container
  u32 count, then count * 28-byte entries: name[16] u32 offset, u32 size, u32 extra.
  Named entries contain live files. Empty names mark unused extents left by older saves; they are not continuations.
  The table CRC is word-wise over count+entries followed by ADEC; table+CRC is mirrored at 0x80000.
  Normal containers (up to 15000 records) have FFFFFFFF at 0x380000 and ver21234 at 0x6ffff8.
  write_container() creates valid compact outer containers on Python; inner page/index signing is handled by hmi_project.py.

main.HMI  (project index)
  0x60-byte header (u32 words: [7] = number of index records), then 16-byte records
  ext[8] name[8]: 'i' pictures (list order == picture ID), 'pa' pages (list order == page ID),
  'gmov' animations, 'zi' fonts; the rest is stale data left by incremental saves.

N.pa  (page)
  0x38-byte header: [0]=crc [1]=size of the live data [2]=0x38 [3]=object count, page name at 0x18;
  then 12-byte table entries (offset, size, 0) per object (offset is relative to 0x38) and the objects.
  Everything after `size` is stale (deleted objects, old thumbnails) and must be ignored.
  object = u32 len + "att-N", N attributes, then "codes<event>-M" blocks with M code lines
  (u32 len + text each). attribute = u32 (0x10 + value_len), name[16], value.

N.i   compiled picture. File header (24 bytes): magic 0x04016 00d, 0, 0x18, w, h (project orientation, u16 each), data
      length, offset of the alpha block (0 = opaque). Data: u32 format (2), u32 stored height 480, u32 stored width 272 (the
      LCD is landscape, the raster is the project image rotated 90 degrees CCW, i.e. 480 columns x 272 rows), u32 constant,
      u32 stream length, u16 0, u16 palette size, u8 ib, palette (RGB565 LE), then an RLE stream of tokens:
      byte t >= E0 (E0 = ((1<<(8-ib))-1)<<ib): base = (t & mask) << ib (high bits of the palette index, sets the base
      for the following tokens); otherwise count = t >> ib, index = (t & mask) + base, count 0 = the next byte is the
      count. An optional alpha block (at the offset in the file header) is a byte stream: bit7 set = one pixel with
      the 7-bit alpha level in the low bits, bit7 clear = run (value, then a count byte). Native alpha encoding uses
      floor(alpha * 127 / 255); decoding doubles the level, except 127 becomes 255. Hidden RGB can remain in old files.
      Bytes after the stream are stale remnants of older saves. N.is has a 24-byte source header, 'png', then PNG at 27.
N.gmov  compiled animation: 'GMOV', u32 version, u32 0x4c, u32 frame count, u16 w, u16 h, ... then 40-byte frame entries
        (u32 flags 0x0400000d, 0, offset, u16 w + u16 h, u32 frame data length, u32 offset of the alpha block inside the frame
        data, u32 duration ms, u32 start ms, 0, 0), then the frames back to back; each frame is a picture payload as in N.i
        (the palette is limited to 256 colors per block, so frames consist of several blocks).
N.gmovs source animation (header + one PNG per frame), Program.s global code (UTF-8).
N.zi  font. 44-byte header = packed struct zimoxinxi (see ZI_FIELDS), then the font name (encodenamebeg bytes) and the
      encoding name, up to base = dataaddr + zimoascbeg. state 0: glyph index entry (code-32) for ASCII 32..126;
      state 1 with encode 24 (UTF-8): one entry per UTF-16 code (qyt = 65536), entries with size 0 are absent glyphs.
      Entry (10 bytes): u16 code, u8 width, u8 left, u8 right, u24 address, u16 size; the glyph data is at
      base + (address << 3 if fontdataadd8byte else address). Glyph data: u8 type (1 = 1-bit, 3 = 3-bit alpha) followed
      by tokens that expand to (left + width + right) * height alpha values, stored column-major (qumo 13 = rotate90+flip):
        type 3  11 aaa bbb: two pixels; 10 nnn lll: nnn zeros + one pixel; 01 s zzzzz: z zeros + (1 + s) opaque pixels;
                001 nnnnn: n opaque pixels; 000 nnnnn: n zeros   (pixel = level * 255 / 7)
        type 1  11 zzz ooo: z zeros + o opaque; 10 s zzzzz: z zeros + (3 + s) opaque; 01 s zzzzz as above; 00x as above.

Usage
  hmi_parse.py FILE.HMI --list
  hmi_parse.py FILE.HMI --check                           container/page/index integrity and structural self-test
  hmi_parse.py FILE.HMI --project [-o project.json]      pages, objects, events, index, resources
  hmi_parse.py FILE.HMI --extract DIR                    raw container files
  hmi_parse.py FILE.HMI --images DIR                     source PNGs named by picture ID (000.png ...)
  hmi_parse.py FILE.HMI --decode DIR                     pictures and animation frames decoded from the compiled data
  hmi_parse.py FILE.HMI --font 2.zi --text 'Hello 0123'   ASCII preview of a font
  hmi_parse.py FILE.HMI --animations DIR                 animation frames (N_00.png ...)
"""
import argparse
import json
import re
import struct
import sys

import os
import hmi_integrity as Integrity

try:
    _SJ = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hmi_schema.json')))
    SCHEMA, MODELS, SERIES = _SJ['types'], _SJ.get('models', []), _SJ.get('series', {})
except (OSError, ValueError, KeyError):
    SCHEMA, MODELS, SERIES = {}, [], {}

# Component type ids and names from the editor's own series table (hmi_schema.json, series X2); 70 = ColPic, 69 = Printer3D.
TYPES = {0: 'waveform', 1: 'slider', 2: 'animation', 3: 'video', 4: 'audio', 5: 'touch_capture', 51: 'timer', 52: 'variable',
         53: 'dual_state_button', 54: 'number', 55: 'scrolling_text', 56: 'checkbox', 57: 'radio_button', 58: 'qr_code',
         59: 'virtual_float', 60: 'external_picture', 61: 'combo_box', 62: 'sliding_text', 63: 'file_stream',
         64: 'file_directory', 65: 'file_browser', 66: 'data_record', 67: 'state_switch', 68: 'select_text',
         69: 'printer_3d', 70: 'col_pic', 98: 'button', 110: 'touch_upload_vp',
         106: 'progress_bar', 109: 'touch_hotspot', 112: 'picture', 113: 'crop_picture', 116: 'text', 121: 'page',
         122: 'pointer', 125: 'ascii_text_input_vp', 126: 'variable_input_vp', 127: 'artistic_text_vp', 128: 'text_vp',
         129: 'data_variable_vp', 130: 'progress_bar_vp', 131: 'picture_vp', 132: 'button_vp', 133: 'qr_code_vp',
         134: 'basic_touch_vp', 135: 'popup_menu_vp', 136: 'icon_carousel_vp', 137: 'curve_vp', 138: 'jpeg_vp', 139: 'dra_vp'}
# Type ids not confirmed by the editor's component table.
GUESSED_TYPES = set()   # every type used by the project is in the editor's series table (hmi_schema.json)
COLOR_ATTRS = ('bco', 'bco1', 'bco2', 'pco', 'pco2', 'borderc')       # RGB565
PIC_ATTRS = ('pic', 'pic1', 'pic2', 'picc', 'picc1', 'picc2', 'bpic', 'ppic')  # picture IDs, 65535 = none
PAGE_ATTRS = ('up', 'down', 'left', 'right')                            # swipe target page IDs, 255 = none
ENUMS = {  # confirmed by the editor's own attribute descriptions / Nextion docs
    'vscope': {0: 'private', 1: 'global'},
    'xcen': {0: 'left', 1: 'center', 2: 'right'},
    'ycen': {0: 'top', 1: 'center', 2: 'bottom'},
}
PAGE_STA = {0: 'none', 1: 'solid_color', 2: 'picture'}
HEAD_ATTRS = ('type', 'id', 'objname', 'vscope', 'drag', 'sendkey', 'aph', 'movex', 'movey', 'x', 'y', 'w', 'h', 'endx',
              'endy', 'effect', 'first', 'time', 'lockobj', 'groupid0', 'groupid1')
STRING_ATTRS = {'objname', 'txt', 'path', 'path_m', 'buff'}
SIGNED16 = {'x', 'y', 'endx', 'endy', 'movex', 'movey', 'spax', 'spay'}
SIGNED32 = {'val', 'minval', 'maxval'}


DATA_BASE = Integrity.DATA_BASE  # data starts after tables, checksums, and format metadata


def read_entries(d):
    """List of container entries (name, offset, size, extra); unnamed entries are unused extents."""
    n = struct.unpack_from('<I', d, 0)[0]
    out = []
    for i in range(n):
        raw = d[4 + i * 28:4 + (i + 1) * 28]
        out.append((raw[:16].split(b'\0')[0].decode('latin1'),) + struct.unpack('<III', raw[16:]))
    return out


def read_container(d, with_stale=False):
    """Return {name: bytes}. The live data of a file is its first (named) entry; the following unnamed entries are
    stale remnants of older, longer versions of the file (the editor appends in place and never cleans up).
    With with_stale=True return (files, stale), grouping unused chunks by the preceding name (None before any file)."""
    files, stale, last = {}, {}, None
    for name, off, size, _extra in read_entries(d):
        if name:
            last = name
            files[last] = bytes(d[off:off + size])
            stale[last] = []
        else:
            stale.setdefault(last, []).append(bytes(d[off:off + size]))
    return (files, stale) if with_stale else files


def write_container(files, order=None):
    """Valid compact outer container, without modifying supplied inner resources.
    For page/index checksum repair and a complete editable-project build, use hmi_project.pack()."""
    return Integrity.build_container(files, order)


def parse_object(b, p):
    """Parse one object whose 'att-N' length prefix is at p."""
    ln = struct.unpack_from('<I', b, p)[0]
    cnt = int(b[p + 4:p + 4 + ln].decode().split('-')[1])
    p += 4 + ln
    attrs = {}
    for _ in range(cnt):
        h = struct.unpack_from('<I', b, p)[0]
        name = b[p + 4:p + 20].split(b'\0')[0].decode()
        n = h - 0x10
        attrs[name] = b[p + 20:p + 20 + n]
        p += 20 + n
    codes = {}
    while p + 4 <= len(b):
        ln = struct.unpack_from('<I', b, p)[0]
        if not 0 < ln < 40 or b[p + 4:p + 9] != b'codes':
            break
        ev, c = b[p + 4:p + 4 + ln].decode().rsplit('-', 1)
        p += 4 + ln
        lines = []
        for _ in range(int(c)):
            l = struct.unpack_from('<I', b, p)[0]
            lines.append(b[p + 4:p + 4 + l].decode('utf8', 'replace'))
            p += 4 + l
        codes[ev] = lines
    return attrs, codes, p


def decode_attr(name, v):
    if name in STRING_ATTRS:
        return v.decode('utf8', 'replace')
    signed = (name in SIGNED16 and len(v) == 2) or (name in SIGNED32 and len(v) == 4)
    return int.from_bytes(v, 'little', signed=signed)


def parse_page(b):
    crc, size, hdr, nobj = struct.unpack_from('<4I', b, 0)
    live = b[:size]
    objs = []
    for i in range(nobj):
        off, sz, _ = struct.unpack_from('<III', live, 0x38 + 12 * i)
        attrs, codes, end = parse_object(live, 0x38 + off)
        o = {k: decode_attr(k, v) for k, v in attrs.items()}
        o['type_name'] = TYPES.get(o['type'], 'type%d' % o['type'])
        o['events'] = {k: v for k, v in codes.items() if v}
        objs.append(o)
    return {'name': b[0x18:0x28].split(b'\0')[0].decode(), 'header': page_header(b), 'objects': objs,
            'stale_bytes': len(b) - size}


PROJECT_HEADER = ('crc datasize upver0 upver1 filever xiliemark guidire encode hmiffid otp model_crc password resources_addr '
                  'resources_count memory_filesystem_len upver2 ram1_open resourcescancel_font resourcescancel_pic '
                  'appmedata0 appmedata1 appmedata2 appmedata3 appmedata4 appmedata5 tt_asp100_tc picencodever res1 '
                  'res15 res16 res17 res18 res19 res20 res21').split()
PAGE_HEADER = ('crc datasize table_addr object_count lock_password page_lock hmiffid filever pagelei').split()


def project_header(m):
    """main.HMI header = hmitype.hmifilehead (96 bytes); model_crc selects the device model."""
    return dict(zip(PROJECT_HEADER, struct.unpack_from('<II8BIIIIi4B6I2BH7I', m, 0)))


def page_header(b):
    """N.pa header = hmitype.hmipagehead (56 bytes)."""
    h = dict(zip(PAGE_HEADER, struct.unpack_from('<5I4B', b, 0)))
    h['name'] = b[24:40].split(b'\0')[0].decode()
    h['upver0'], h['upver1'], h['upver2'], h['encodeid'] = b[40], b[41], b[42], b[43]
    return h


PICTURE_HEADER = 'qumo quality alphaen picdatatype pictureid encodeen res1 dataaddr w h imgbytesize alphaaddr'.split()
VIDEO_HEADER = ('ffid dire quality fps videodatatype dataaddr frameqyt w h showpic videodatasize wavaddr wavsize stim '
                'extmessageaddr extmessagesize alphaframhave alphaen encodeen res1 res2 res3 res4 res5 res6 res7').split()


def picture_header(b):
    """N.i file header = hmitype.Picturexinxi (24 bytes)."""
    return dict(zip(PICTURE_HEADER, struct.unpack_from('<4BH2BIHHIi', b, 0)))


def video_header(b):
    """N.gmov header = hmitype.Videoxinxi (76 bytes)."""
    return dict(zip(VIDEO_HEADER, struct.unpack_from('<i4BIIHHIIIIIIII4BIIIIII', b, 0)))


def parse_index(m):
    words = struct.unpack_from('<24I', m, 0)
    recs = []
    p = 0x60
    for _ in range(words[7]):
        r = m[p:p + 16]
        mm = re.match(rb'^([a-z]+)\x00+([\w.\-]+)\x00*$', r)
        if not mm:
            break
        recs.append((mm.group(1).decode(), mm.group(2).decode()))
        p += 16
    return words, recs


def _blocks(d, p, total):
    """Decode palette blocks starting at p until `total` pixels are produced. A block is
    u16 0, u16 palette size, u8 ib, palette (RGB565 LE) and an RLE stream that ends with a zero-length run (00 00),
    which doubles as the leading u16 0 of the next block (frames of animations have palettes limited to 256 colors,
    so they consist of several blocks). Returns (rgb565 list, position after the last token)."""
    px = []
    while len(px) < total and p + 5 <= len(d):
        pc = struct.unpack_from('<H', d, p + 2)[0]
        ib = d[p + 4]
        pal = [struct.unpack_from('<H', d, p + 5 + 2 * j)[0] for j in range(pc)]
        p += 5 + 2 * pc
        e0 = ((1 << (8 - ib)) - 1) << ib
        mask = (1 << ib) - 1
        base = 0
        while len(px) < total and p < len(d):
            t = d[p]
            if t >= e0:
                base = (t & mask) << ib
                p += 1
                continue
            cnt = t >> ib
            if cnt == 0:
                if p + 1 < len(d) and d[p + 1] == 0:      # 00 00: end of this block's stream
                    break
                cnt = d[p + 1]
                p += 1
            p += 1
            i = (t & mask) + base
            px += [pal[i] if i < pc else 0] * cnt
    return px[:total], p


def _alpha(d, p, total):
    a = []
    while len(a) < total and p < len(d):
        v = d[p]
        p += 1
        if v & 0x80:
            a.append(v & 0x7f)
        else:
            a += [v] * d[p]
            p += 1
    return [255 if v == 127 else v << 1 for v in a[:total]]


def _unrotate(px, w, h, alpha=None):
    """The stored raster has w rows x h columns (the LCD is landscape): project (x, y) = stored[w-1-x][y]."""
    out = [px[(w - 1 - x) * h + y] for y in range(h) for x in range(w)]
    al = [alpha[(w - 1 - x) * h + y] for y in range(h) for x in range(w)] if alpha else None
    return out, al


def decode_payload(d, w, h, alpha_off=0):
    """Decode one picture payload (u32 format 2, u32 h, u32 w, u32 const, u32 stream length, blocks...)."""
    fmt, sh, sw, _k, _slen = struct.unpack_from('<5I', d, 0)
    px, _ = _blocks(d, 20, sh * sw)
    if len(px) != sh * sw:
        raise ValueError('truncated picture stream')
    al = _alpha(d, alpha_off, sh * sw) if alpha_off else None
    px, al = _unrotate(px, w, h, al)
    return {'w': w, 'h': h, 'rgb565': px, 'alpha': al}


def decode_picture(b):
    """Decode a compiled N.i. Returns dict(w, h, rgb565=[...], alpha=[0..255]|None) in project orientation (row-major)."""
    w, h, _length, alpha_off = struct.unpack_from('<HHII', b, 12)
    return decode_payload(b[24:], w, h, alpha_off)


def gmov_decode(b):
    """Decode a compiled N.gmov: list of dict(w, h, rgb565, alpha, duration_ms, start_ms)."""
    frames, w, h = struct.unpack_from('<IHH', b, 12)
    ents = [struct.unpack_from('<10I', b, 0x4c + 40 * i) for i in range(frames)]
    pos = 0x4c + 40 * frames
    out = []
    for e in ents:
        fw, fh = e[3] & 0xffff, e[3] >> 16
        f = decode_payload(b[pos:pos + e[4]], fw, fh, e[5])
        f.update(duration_ms=e[6], start_ms=e[7])
        out.append(f)
        pos += e[4]
    return out


def write_png(path, w, h, rgb565, alpha=None):
    import zlib
    rows = bytearray()
    ch = 4 if alpha else 3
    for y in range(h):
        rows.append(0)
        for x in range(w):
            v = rgb565[y * w + x]
            r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
            rows += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))
            if alpha:
                rows.append(alpha[y * w + x])

    def chunk(t, data):
        c = struct.pack('>I', len(data)) + t + data
        return c + struct.pack('>I', zlib.crc32(t + data) & 0xffffffff)
    open(path, 'wb').write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6 if alpha else 2, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(bytes(rows), 6)) + chunk(b'IEND', b''))


ZI_FIELDS = ('Password codelT0 codelDec qumo encode state w h codeh_star codeh_end codel_star codel_end qyt fontver '
             'zimoascbeg zimobinbeg datasize dataaddr codehT0 codehDec Anti unequal_res encodenamebeg fontdataadd8byte '
             'res1 trueziqty res3').split()


def font_header(b):
    d = dict(zip(ZI_FIELDS, struct.unpack_from('<12BIBBHIIBBBBBBHII', b, 0)))
    d['name'] = b[44:44 + d['encodenamebeg']].decode('utf8', 'replace')
    d['encoding'] = b[44 + d['encodenamebeg']:d['dataaddr'] + d['zimoascbeg']].decode('ascii', 'replace')
    return d


def _font_entry(b, hd, code):
    base = hd['dataaddr'] + hd['zimoascbeg']
    if hd['state'] == 0:
        i = code - 32
    elif hd['state'] == 1 and hd['encode'] == 24:
        i = code
    else:
        raise NotImplementedError('font state %d / encode %d' % (hd['state'], hd['encode']))
    if not 0 <= i < hd['qyt']:
        return None
    c, w, l, r, a, size = struct.unpack_from('<HBBB3sH', b, base + 10 * i)
    if size < 2:
        return None
    off = base + (int.from_bytes(a, 'little') << (3 if hd['fontdataadd8byte'] else 0))
    return {'code': code, 'width': w, 'left': l, 'right': r, 'size': size, 'data': b[off:off + size]}


def glyph_decode(data, total):
    """Expand glyph data to `total` alpha values (0..255), in stored order."""
    typ, out = data[0], []
    for t in data[1:]:
        k = t >> 6
        if typ == 3:
            if k == 3:
                out += [round(((t >> 3) & 7) * 255 / 7), round((t & 7) * 255 / 7)]
            elif k == 2:
                out += [0] * ((t >> 3) & 7) + [round((t & 7) * 255 / 7)]
            elif k == 1:
                out += [0] * (t & 31) + [255] * (2 if t & 32 else 1)
            else:
                out += [255 if t & 32 else 0] * (t & 31)
        elif typ == 1:
            if k == 3:
                out += [0] * ((t >> 3) & 7) + [255] * (t & 7)
            elif k == 2:
                out += [0] * (t & 31) + [255] * (3 + ((t >> 5) & 1))
            elif k == 1:
                out += [0] * (t & 31) + [255] * (2 if t & 32 else 1)
            else:
                out += [255 if t & 32 else 0] * (t & 31)
        else:
            raise ValueError('unknown glyph type %d' % typ)
    return (out + [0] * total)[:total]


def _rotate90_flip(a, wid, hig):
    """Appdrawfont.QumoBytesRotate90Flip: the stored glyph is column-major (wid = font height columns)."""
    n = len(a)
    if wid * hig != n:
        return a
    out, c, p = [], wid - 1, n - wid
    for _ in range(n):
        out.append(a[p])
        p -= wid
        if p < 0:
            p = n - c
            c -= 1
    return out


def font_glyph(b, code, hd=None):
    """Return dict(code, width, left, right, w, h, alpha=[row-major 0..255]) or None if the font has no such glyph."""
    hd = hd or font_header(b)
    e = _font_entry(b, hd, code)
    if e is None:
        return None
    total_w, h = e['left'] + e['width'] + e['right'], hd['h']
    a = glyph_decode(e['data'], total_w * h)
    if hd['qumo'] == 13:
        a = _rotate90_flip(a, h, total_w)
    elif hd['qumo'] not in (0,):
        raise NotImplementedError('qumo %d' % hd['qumo'])
    return {'code': code, 'width': e['width'], 'left': e['left'], 'right': e['right'], 'w': total_w, 'h': h, 'alpha': a}


def font_codes(b, hd=None):
    """All codes that have a glyph."""
    hd = hd or font_header(b)
    return [c for c in (range(32, 32 + hd['qyt']) if hd['state'] == 0 else range(hd['qyt'])) if _font_entry(b, hd, c)]


def font_render_ascii(b, text, hd=None):
    """Debug helper: render text as ASCII art using the font's own glyphs."""
    hd = hd or font_header(b)
    gl = [font_glyph(b, ord(c), hd) for c in text]
    rows = []
    for y in range(hd['h']):
        rows.append(''.join(''.join(' .:-=+*#%@'[min(9, v * 10 // 256)] for v in g['alpha'][y * g['w']:(y + 1) * g['w']])
                            if g else '' for g in gl))
    return '\n'.join(rows)


def picture_info(files, name):
    n = name[:-2]
    i_, is_ = files.get(n + '.i'), files.get(n + '.is')
    info = {'file': name}
    if i_:
        w, h, ln = struct.unpack_from('<HHI', i_, 12)
        info.update(w=w, h=h, compiled_size=len(i_))
    if is_:
        k = is_.find(b'\x89PNG')
        info.update(source_format='png' if k >= 0 else 'unknown', source_size=len(is_) - k if k >= 0 else len(is_))
    return info


def gmov_info(files, name):
    b = files.get(name)
    info = {'file': name}
    if b and b[:4] == b'GMOV':
        frames, w, h = struct.unpack_from('<IHH', b, 12)
        info.update(frames=frames, w=w, h=h, size=len(b),
                    frame_ms=[struct.unpack_from('<I', b, 0x4c + 40 * i + 24)[0] for i in range(frames)])
        src = files.get(name + 's')
        if src:
            info['source_frames'] = len(gmov_frames(src))
    return info


def gmov_frames(b):
    """Source animation frames (PNG) from an N.gmovs file."""
    out = []
    for m in re.finditer(b'\x89PNG\r\n\x1a\n', b):
        e = b.find(b'IEND\xaeB`\x82', m.start())
        if e >= 0:
            out.append(b[m.start():e + 8])
    return out


SCRIPTS = (('ascii', 0x20, 0x7f), ('latin', 0x80, 0x24f), ('greek', 0x370, 0x3ff), ('cyrillic', 0x400, 0x52f),
           ('hebrew', 0x590, 0x5ff), ('arabic', 0x600, 0x6ff), ('thai', 0xe00, 0xe7f), ('hangul_jamo', 0x1100, 0x11ff),
           ('punctuation_symbols', 0x2000, 0x2bff), ('cjk_symbols', 0x3000, 0x303f), ('hiragana', 0x3040, 0x309f),
           ('katakana', 0x30a0, 0x30ff), ('cjk', 0x4e00, 0x9fff), ('hangul', 0xac00, 0xd7af), ('fullwidth', 0xff00, 0xffef))


def font_info(files, name):
    b = files[name]
    hd = font_header(b)
    info = {'file': name, 'size': len(b), 'header': hd}
    try:
        codes = font_codes(b, hd)
    except NotImplementedError:
        return info
    cov = {k: sum(1 for c in codes if lo <= c <= hi) for k, lo, hi in SCRIPTS}
    info['glyphs'] = len(codes)
    info['coverage'] = {k: v for k, v in cov.items() if v}
    return info


def rgb565(v):
    r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
    return '#%02x%02x%02x' % ((r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31)


CODE_PIC = re.compile(r'\b(?:pic|picq|xpic)\s+[^,\n]+,[^,\n]+,\s*(\d+)|\.(?:pic|pic1|pic2|picc|picc1|picc2|bpic|ppic)\s*=\s*(\d+)')
CODE_PAGE = re.compile(r'(?<![\w.])page\s+([A-Za-z_]\w*|\d+)')


def parse_prints(lines):
    """If an event is only `prints expr,len` lines, return the UART frame as a token list.
    Each token is the expression as written (e.g. '0x65', 'dp', '3'); len is the byte count (1 = single byte,
    anything else is kept as 'expr*len')."""
    out = []
    for l in lines:
        m = re.fullmatch(r'\s*prints\s+(.+?)\s*,\s*(\d+)\s*', l)
        if not m:
            return None
        out.append(m.group(1) if m.group(2) == '1' else '%s*%s' % (m.group(1), m.group(2)))
    return out


def resolve(project):
    """Add derived info: colors, picture/font/page references, enum names, UART frames, navigation, unused pictures."""
    pics, fonts = project['pictures'], project['fonts']
    names = [pg['name'] for pg in project['pages']]
    used = set()
    nav = {}
    for pg in project['pages']:
        targets = set()
        for o in pg['objects']:
            r = {}
            for k in COLOR_ATTRS:
                if isinstance(o.get(k), int):
                    r[k] = rgb565(o[k])
            for k in PIC_ATTRS:
                v = o.get(k)
                if isinstance(v, int) and v < len(pics):
                    r[k] = v
                    used.add(v)
            if o['type_name'] != 'page' and isinstance(o.get('font'), int) and o['font'] < len(fonts):
                r['font'] = fonts[o['font']]['header']['name']
            sch = {a['name']: a for a in SCHEMA.get(str(o['type']), {}).get('attributes', [])}
            for k, v in o.items():
                en = sch.get(k, {}).get('enum')
                if en and isinstance(v, int) and str(v) in en:
                    r[k] = en[str(v)]
            for k, m in ENUMS.items():
                if k not in r and o.get(k) in m:
                    r[k] = m[o[k]]
            if o['type_name'] == 'page':
                if o.get('sta') in PAGE_STA:
                    r['sta'] = PAGE_STA[o['sta']]
                for k in PAGE_ATTRS:
                    if isinstance(o.get(k), int) and o[k] < len(names):
                        r[k] = names[o[k]]
                        targets.add(names[o[k]])
            o['type_confidence'] = 'unknown' if o['type'] in GUESSED_TYPES or (SCHEMA and str(o['type']) not in SCHEMA and o['type'] != 121) else 'known'
            for lines in o['events'].values():
                text = '\n'.join(lines)
                for m in CODE_PIC.finditer(text):
                    used.add(int(m.group(1) or m.group(2)))
                for m in CODE_PAGE.finditer(text):
                    t = m.group(1)
                    targets.add(names[int(t)] if t.isdigit() and int(t) < len(names) else t)
            if r:
                o['resolved'] = r
            tx = {ev: frame for ev, lines in o['events'].items() for frame in [parse_prints(lines)] if frame}
            if tx:
                o['tx'] = tx
        nav[pg['name']] = sorted(targets)
    for m in CODE_PIC.finditer(project.get('program_s') or ''):
        used.add(int(m.group(1) or m.group(2)))
    project['navigation'] = nav
    project['pictures_unreferenced_in_hmi'] = [p['id'] for p in pics if p['id'] not in used]
    return project


def check(files):
    """Structural self-test; returns a list of problems (empty == OK)."""
    bad = []
    words, recs = parse_index(files['main.HMI'])
    ph = project_header(files['main.HMI'])
    if len(recs) != words[7] or ph['resources_count'] != len(recs):
        bad.append('index records %d != %d' % (len(recs), words[7]))
    for n, b in files.items():
        if not n.endswith('.pa'):
            continue
        ph_ = page_header(b)
        size, nobj = ph_['datasize'], ph_['object_count']
        if ph_['table_addr'] != 0x38 or size > len(b):
            bad.append('%s: bad header' % n)
            continue
        end = 0x38 + max(struct.unpack_from('<I', b, 0x38 + 12 * i)[0] + struct.unpack_from('<I', b, 0x38 + 12 * i + 4)[0]
                         for i in range(nobj))
        if end != size:
            bad.append('%s: table end %d != size %d' % (n, end, size))
        pg = parse_page(b)
        if len(pg['objects']) != nobj or pg['objects'][0]['type'] != 121:
            bad.append('%s: object count/first object' % n)
        if SCHEMA:
            for o in pg['objects']:
                t = SCHEMA.get(str(o['type']))
                if t and t.get('attributes') and o['type'] != 121:
                    names = [a['name'] for a in t['attributes']]
                    extra = [k for k in o if k not in HEAD_ATTRS and k not in ('type_name', 'events') and k not in names]
                    if extra:
                        bad.append('%s/%s: attributes not in schema: %s' % (n, o['objname'], extra))
    for k, n in recs:
        base = n.rsplit('.', 1)[0]
        if n not in files and k != 'i' and (base + '.' + k) not in files:
            bad.append('index entry %s missing' % n)
        if k == 'i' and (n not in files or base + '.is' not in files):
            bad.append('picture %s missing .i/.is' % n)
    for k, n in recs:
        if k == 'i':
            try:
                decode_picture(files[n])
            except Exception as e:
                bad.append('%s: %s' % (n, e))
        elif k == 'gmov':
            try:
                gmov_decode(files[n])
            except Exception as e:
                bad.append('%s: %s' % (n, e))
        elif k == 'zi':
            hd = font_header(files[n])
            if hd['trueziqty'] != len(font_codes(files[n], hd)):
                bad.append('%s: glyph count mismatch' % n)
    return bad


def parse_project(files):
    words, recs = parse_index(files['main.HMI'])
    pages_by_file = {n: parse_page(b) for n, b in files.items() if n.endswith('.pa')}
    pages = []
    for pid, (k, n) in enumerate(r for r in recs if r[0] == 'pa'):
        pg = pages_by_file[n]
        pages.append({'id': pid, 'file': n, **pg})
    pics = [dict(id=i, **picture_info(files, n)) for i, (k, n) in enumerate(r for r in recs if r[0] == 'i')]
    return resolve({
        'device': next((m for m in MODELS if m['crc'] == words[4]), {'crc': words[4]}),
        'series': SERIES.get('xiliename'),
        'project_header': project_header(files['main.HMI']),
        'program_s': files['Program.s'].decode('utf8', 'replace') if 'Program.s' in files else None,
        'pages': pages,
        'pictures': pics,
        'animations': [gmov_info(files, n) for k, n in recs if k == 'gmov'],
        'fonts': [font_info(files, n) for k, n in recs if k == 'zi'],
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('file')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--project', action='store_true')
    ap.add_argument('--extract')
    ap.add_argument('--images')
    ap.add_argument('--animations')
    ap.add_argument('--decode')
    ap.add_argument('--font')
    ap.add_argument('--text')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('-o')
    a = ap.parse_args()
    with open(a.file, 'rb') as f:
        data = f.read()
    if a.check:
        problems = Integrity.container_errors(data)
        if problems:
            print('\n'.join(problems))
            return 1
    files = read_container(data)
    if a.list:
        for n, b in files.items():
            print(f'{len(b):10d}  {n}')
    if a.extract:
        import os
        os.makedirs(a.extract, exist_ok=True)
        for n, b in files.items():
            open(os.path.join(a.extract, n), 'wb').write(b)
    if a.images:
        import os
        os.makedirs(a.images, exist_ok=True)
        _, recs = parse_index(files['main.HMI'])
        for pid, (k, n) in enumerate(r for r in recs if r[0] == 'i'):
            b = files.get(n[:-2] + '.is', b'')
            i = b.find(b'\x89PNG')
            if i >= 0:
                open(os.path.join(a.images, '%03d.png' % pid), 'wb').write(b[i:])
    if a.check:
        problems = []
        for name, b in files.items():
            if name.endswith('.pa') and not Integrity.verify_page(b):
                problems.append(name + ': page checksum/header mismatch')
            elif name == 'main.HMI' and not Integrity.verify_index(b):
                problems.append(name + ': index checksum/header mismatch')
        if not problems:
            problems = check(files)
        print('OK' if not problems else '\n'.join(problems))
        if problems:
            return 1
    if a.font:
        print(font_render_ascii(files[a.font], a.text or 'Hello 0123'))
    if a.decode:
        import os
        os.makedirs(a.decode, exist_ok=True)
        _, recs = parse_index(files['main.HMI'])
        for pid, (k, n) in enumerate(r for r in recs if r[0] == 'i'):
            pic = decode_picture(files[n])
            write_png(os.path.join(a.decode, '%03d.png' % pid), pic['w'], pic['h'], pic['rgb565'], pic['alpha'])
    if a.decode:
        import os
        for n, b in files.items():
            if n.endswith('.gmov'):
                for i, f in enumerate(gmov_decode(b)):
                    write_png(os.path.join(a.decode, 'anim_%s_%02d.png' % (n[:-5], i)), f['w'], f['h'], f['rgb565'], f['alpha'])
    if a.animations:
        import os
        os.makedirs(a.animations, exist_ok=True)
        for n, b in files.items():
            if n.endswith('.gmovs'):
                for i, f in enumerate(gmov_frames(b)):
                    open(os.path.join(a.animations, '%s_%02d.png' % (n[:-6], i)), 'wb').write(f)
    if a.project:
        s = json.dumps(parse_project(files), ensure_ascii=False, indent=1)
        open(a.o, 'w').write(s) if a.o else print(s)


if __name__ == '__main__':
    sys.exit(main())
