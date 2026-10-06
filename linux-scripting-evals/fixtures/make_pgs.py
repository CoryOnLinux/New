#!/usr/bin/env python3
"""Write a minimal Blu-ray PGS subtitle stream (.sup) with N captions.

ffmpeg has no PGS encoder, so fixtures that need picture-based subtitles start
from this file: mux it into an MKV as hdmv_pgs_subtitle, or transcode it to
dvd_subtitle / dvb_subtitle with ffmpeg.

Usage: make_pgs.py OUT.sup [WIDTH HEIGHT] [CAPTIONS]
"""
import struct
import sys


def segment(kind, pts, payload):
    # "PG", PTS and DTS on the 90 kHz clock, segment type, payload size
    return b"PG" + struct.pack(">IIBH", pts, 0, kind, len(payload)) + payload


def rle_box(w, h, color=1):
    """Run-length encode a w x h box filled with palette entry `color`."""
    line = bytes([0x00, 0xC0 | (w >> 8), w & 0xFF, color]) if w >= 64 else bytes([0x00, 0x80 | w, color])
    return (line + b"\x00\x00") * h


def display_set(pts, comp_no, width, height, show):
    ow, oh, x, y = 200, 40, (width - 200) // 2, height - 80
    window = struct.pack(">BBHHHH", 1, 0, x, y, ow, oh)
    if not show:
        pcs = struct.pack(">HHBHBBBB", width, height, 0x10, comp_no, 0x00, 0, 0, 0)
        return segment(0x16, pts, pcs) + segment(0x17, pts, window) + segment(0x80, pts, b"")
    pcs = struct.pack(">HHBHBBBB", width, height, 0x10, comp_no, 0x80, 0, 0, 1)
    pcs += struct.pack(">HBBHH", 0, 0, 0x00, x, y)
    # palette 0: entry 0 transparent, entry 1 opaque white (Y, Cr, Cb, alpha)
    pds = struct.pack(">BB", 0, 0) + bytes([0, 16, 128, 128, 0]) + bytes([1, 235, 128, 128, 255])
    data = rle_box(ow, oh)
    ods = struct.pack(">HBB", 0, 0, 0xC0) + (len(data) + 4).to_bytes(3, "big") + struct.pack(">HH", ow, oh) + data
    return (segment(0x16, pts, pcs) + segment(0x17, pts, window) + segment(0x14, pts, pds)
            + segment(0x15, pts, ods) + segment(0x80, pts, b""))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = sys.argv[1]
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 320
    height = int(sys.argv[3]) if len(sys.argv) > 3 else 240
    captions = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    stream = b""
    for i in range(captions):
        start = int((0.2 + i * 1.2) * 90000)
        stream += display_set(start, 2 * i, width, height, True)
        stream += display_set(start + 72000, 2 * i + 1, width, height, False)
    with open(out, "wb") as f:
        f.write(stream)


if __name__ == "__main__":
    main()
