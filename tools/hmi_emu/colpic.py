"""Decoder of the ColPic thumbnail format that a host streams into a col_pic component with cpN.write("...").

The text carries 6 bits per character (offset '0', '~' stands for a backslash). The binary form is a 32-byte header
(version 3, width, height, mark 0x05DDC33C, palette size and run data size), a palette of RGB565 colours and a run
length coded list of palette indexes: 0xE0|bank selects the bank of 32 colours, count<<5|index (count 1..6) is a short
run, index followed by a count byte is a long run.
"""

import struct

MARK = 0x05DDC33C


def text_to_bytes(data):
    """Reverses the 6-bit text coding; a trailing partial group is dropped."""
    out = bytearray()
    vals = [(0x5C if c == 0x7E else c) - 48 for c in data]
    for i in range(0, len(vals) - 3, 4):
        a, b, c, d = vals[i:i + 4]
        out.append(((a << 2) | (b >> 4)) & 255)
        out.append((((b & 15) << 4) | (c >> 2)) & 255)
        out.append((((c & 3) << 6) | d) & 255)
    return bytes(out)


def decode(data):
    """ColPic text (bytes/str) -> (width, height, list of RGB565 values); raises ValueError when it is not ColPic."""
    if isinstance(data, str):
        data = data.encode("latin-1", "replace")
    raw = text_to_bytes(data.rstrip(b"\x00"))
    if len(raw) < 32 or raw[0] != 3 or struct.unpack_from("<I", raw, 12)[0] != MARK:
        raise ValueError("not ColPic data")
    width, height = struct.unpack_from("<II", raw, 4)
    palette_size, run_size = struct.unpack_from("<II", raw, 16)
    if not (0 < width * height <= 1 << 22) or palette_size % 2 or 32 + palette_size + run_size > len(raw):
        raise ValueError("bad ColPic header")
    palette = struct.unpack_from("<%dH" % (palette_size // 2), raw, 32)
    runs = raw[32 + palette_size:32 + palette_size + run_size]
    pixels = []
    bank = 0
    i = 0
    total = width * height
    while i < len(runs) and len(pixels) < total:
        b = runs[i]
        i += 1
        hi, idx = b >> 5, b & 31
        if hi == 7:
            bank = idx
            continue
        count = hi
        if hi == 0:
            if i >= len(runs):
                break
            count = runs[i]
            i += 1
        n = bank * 32 + idx
        pixels.extend([palette[n] if n < len(palette) else 0] * count)
    pixels.extend([0] * (total - len(pixels)))
    return width, height, pixels[:total]
