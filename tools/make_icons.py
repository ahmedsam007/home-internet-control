"""Draws the home-screen icons (routerctl/web/icon-*.png) without any image library.

    python tools/make_icons.py
"""

import os
import struct
import zlib

BLUE, WHITE = (47, 111, 221), (255, 255, 255)
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "routerctl", "web")


def inside(u, v):
    """Shape in a 0..1 square: white signal bars on blue. Returns colour or None (transparent)."""
    bars = [(0.20, 0.62), (0.36, 0.48), (0.52, 0.34), (0.68, 0.20)]   # (left, top) of 4 bars
    for left, top in bars:
        if left <= u <= left + 0.12 and top <= v <= 0.80:
            return WHITE
    return BLUE


def png(size, path, rounded):
    ss = 4
    rows = []
    r = 0.22
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    u, v = (x + (sx + .5) / ss) / size, (y + (sy + .5) / ss) / size
                    if rounded:
                        cx = min(max(u, r), 1 - r)
                        cy = min(max(v, r), 1 - r)
                        if (u - cx) ** 2 + (v - cy) ** 2 > r * r:
                            continue
                    c = inside(u, v)
                    acc[0] += c[0]
                    acc[1] += c[1]
                    acc[2] += c[2]
                    acc[3] += 255
            n = ss * ss
            a = acc[3] // n
            if a:
                row += bytes([acc[0] * 255 // acc[3], acc[1] * 255 // acc[3], acc[2] * 255 // acc[3], a])
            else:
                row += bytes(4)
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)


if __name__ == "__main__":
    png(180, os.path.join(OUT, "icon-180.png"), rounded=False)   # iPhone rounds the corners itself
    png(192, os.path.join(OUT, "icon-192.png"), rounded=True)
    png(512, os.path.join(OUT, "icon-512.png"), rounded=True)
    png(512, os.path.join(OUT, "icon-maskable.png"), rounded=False)  # Android crops it to its own shape
    print("icons written to", OUT)
