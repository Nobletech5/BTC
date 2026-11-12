#!/usr/bin/env python3
"""Cutest picture: a hand-drawn kitten face, rendered in pure Python stdlib.

The program draws the picture itself (no bundled image, no network, no
third-party libraries) and writes it deterministically to
$MAKEFUN_OUTPUT_DIR/entry.png (default /output/entry.png).

Technique: a small scanline rasteriser draws filled ellipses, polygons and
thick lines into a 2x supersampled framebuffer, which is then box-downsampled
to 512x512 for smooth edges. The PNG is encoded by hand with zlib+struct.
"""

import math
import os
import struct
import zlib

W = H = 512
SS = 2  # supersampling factor
SW, SH = W * SS, H * SS

# Framebuffer: RGB bytes at supersampled resolution.
buf = bytearray(SW * SH * 3)


def _blend(x, y, r, g, b, a):
    """Blend one supersampled pixel. a is 0..255."""
    if a <= 0 or x < 0 or y < 0 or x >= SW or y >= SH:
        return
    i = (y * SW + x) * 3
    if a >= 255:
        buf[i] = r
        buf[i + 1] = g
        buf[i + 2] = b
    else:
        buf[i] = (r * a + buf[i] * (255 - a)) // 255
        buf[i + 1] = (g * a + buf[i + 1] * (255 - a)) // 255
        buf[i + 2] = (b * a + buf[i + 2] * (255 - a)) // 255


def _span(x0, x1, y, r, g, b, a):
    """Fill a horizontal run of supersampled pixels (inclusive)."""
    if y < 0 or y >= SH or x1 < 0 or x0 >= SW:
        return
    x0 = max(0, x0)
    x1 = min(SW - 1, x1)
    if x0 > x1:
        return
    i0 = (y * SW + x0) * 3
    i1 = (y * SW + x1 + 1) * 3
    if a >= 255:
        buf[i0:i1] = bytes((r, g, b)) * (x1 - x0 + 1)
    else:
        for x in range(x0, x1 + 1):
            _blend(x, y, r, g, b, a)


def fill_ellipse(cx, cy, rx, ry, color, alpha=255):
    """Filled ellipse, logical (512-space) coordinates."""
    cx, cy, rx, ry = cx * SS, cy * SS, rx * SS, ry * SS
    r, g, b = color
    y0 = max(0, int(math.ceil(cy - ry)))
    y1 = min(SH - 1, int(math.floor(cy + ry)))
    for y in range(y0, y1 + 1):
        dy = (y + 0.5 - cy) / ry
        t = 1.0 - dy * dy
        if t <= 0:
            continue
        dx = rx * math.sqrt(t)
        _span(int(math.ceil(cx - dx)), int(math.floor(cx + dx)), y, r, g, b, alpha)


def fill_circle(cx, cy, rad, color, alpha=255):
    fill_ellipse(cx, cy, rad, rad, color, alpha)


def fill_poly(points, color, alpha=255):
    """Filled polygon, logical coordinates. Points: list of (x, y)."""
    pts = [(x * SS, y * SS) for x, y in points]
    r, g, b = color
    ys = [p[1] for p in pts]
    y0 = max(0, int(math.ceil(min(ys))))
    y1 = min(SH - 1, int(math.floor(max(ys))))
    n = len(pts)
    for y in range(y0, y1 + 1):
        yc = y + 0.5
        xs = []
        for i in range(n):
            x1, y1p = pts[i]
            x2, y2p = pts[(i + 1) % n]
            if (y1p <= yc < y2p) or (y2p <= yc < y1p):
                xs.append(x1 + (yc - y1p) * (x2 - x1) / (y2p - y1p))
        if len(xs) < 2:
            continue
        xs.sort()
        for k in range(0, len(xs) - 1, 2):
            _span(int(math.ceil(xs[k])), int(math.floor(xs[k + 1])), y, r, g, b, alpha)


def _stamp(cx, cy, rad, r, g, b, alpha):
    fill_ellipse(cx / SS, cy / SS, rad / SS, rad / SS, (r, g, b), alpha)


def thick_line(x0, y0, x1, y1, color, width, alpha=255):
    """A line drawn as a chain of small discs (round caps, smooth)."""
    x0, y0, x1, y1 = x0 * SS, y0 * SS, x1 * SS, y1 * SS
    r, g, b = color
    rad = width * SS / 2.0
    dist = math.hypot(x1 - x0, y1 - y0)
    steps = max(1, int(dist / max(1.0, rad * 0.6)) + 1)
    for i in range(steps + 1):
        t = i / steps
        _stamp(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, rad, r, g, b, alpha)


def arc(cx, cy, rad, a0, a1, color, width, alpha=255):
    """An arc (angles in degrees, 0 = east, increasing clockwise in screen space)."""
    r, g, b = color
    steps = max(4, int(abs(a1 - a0) / 6) + 1)
    prev = None
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        px = cx + rad * math.cos(a)
        py = cy + rad * math.sin(a)
        if prev is not None:
            thick_line(prev[0], prev[1], px, py, color, width, alpha)
        prev = (px, py)


def heart(cx, cy, s, color, alpha=255):
    """A small heart centred near (cx, cy), size s."""
    fill_circle(cx - s * 0.38, cy - s * 0.18, s * 0.40, color, alpha)
    fill_circle(cx + s * 0.38, cy - s * 0.18, s * 0.40, color, alpha)
    fill_poly(
        [(cx - s * 0.72, cy - s * 0.05), (cx + s * 0.72, cy - s * 0.05),
         (cx, cy + s * 0.85)],
        color, alpha,
    )


def _lcg(seed):
    x = seed
    while True:
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        yield x


def draw_background():
    # Vertical pastel gradient: soft pink at the top, warm cream at the bottom.
    top = (255, 234, 241)
    bottom = (255, 247, 233)
    for y in range(SH):
        t = y / (SH - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        i0 = y * SW * 3
        buf[i0:(y + 1) * SW * 3] = bytes((r, g, b)) * SW
    # Faint white dots, fixed positions from a seeded generator (deterministic).
    rng = _lcg(20261113)
    for _ in range(16):
        x = 24 + next(rng) % 464
        y = 24 + next(rng) % 464
        rad = 5 + next(rng) % 9
        fill_circle(x, y, rad, (255, 255, 255), 70)


def draw_kitten():
    cream = (255, 244, 227)
    outline = (232, 180, 140)
    pink = (242, 125, 150)
    dark = (67, 48, 43)
    blush = (255, 159, 178)
    whisker = (217, 160, 107)
    mouth_c = (178, 106, 90)

    # Soft ground shadow.
    fill_ellipse(256, 470, 122, 13, (243, 201, 168), 90)

    # Ears (behind the head so the head overlaps their bases).
    fill_poly([(104, 200), (150, 94), (208, 180)], outline)
    fill_poly([(408, 200), (362, 94), (304, 180)], outline)
    fill_poly([(122, 184), (152, 118), (192, 174)], pink)
    fill_poly([(390, 184), (360, 118), (320, 174)], pink)

    # Head: warm outline, then the cream face.
    fill_circle(256, 278, 172, outline)
    fill_circle(256, 278, 164, cream)

    # Little bow on the right ear.
    bow = (242, 102, 139)
    bow_d = (225, 78, 122)
    fill_poly([(322, 188), (284, 164), (290, 210)], bow)
    fill_poly([(322, 188), (360, 164), (354, 210)], bow)
    fill_circle(322, 188, 10, bow_d)

    # Big glossy eyes: dark outline, warm iris, dark pupil, white highlights.
    for ex in (188, 324):
        fill_ellipse(ex, 268, 38, 48, dark)
        fill_ellipse(ex, 270, 27, 35, (124, 74, 33))
        fill_ellipse(ex, 270, 17, 25, dark)
        fill_circle(ex - 12, 250, 13, (255, 255, 255))
        fill_circle(ex + 13, 286, 6, (255, 255, 255))
        # Two tiny lash flicks on the outer top edge.
        thick_line(ex - 31, 234, ex - 41, 222, dark, 3)
        thick_line(ex - 23, 228, ex - 31, 214, dark, 3)

    # Blush cheeks.
    fill_ellipse(146, 344, 30, 17, blush, 140)
    fill_ellipse(366, 344, 30, 17, blush, 140)

    # Tiny pink nose and a little "w" mouth.
    fill_poly([(238, 316), (274, 316), (256, 338)], pink)
    fill_circle(256, 334, 7, pink)
    arc(240, 340, 14, 15, 165, mouth_c, 3)
    arc(272, 340, 14, 15, 165, mouth_c, 3)

    # Whiskers.
    for sx in (-1, 1):
        thick_line(256 + sx * 106, 300, 256 + sx * 162, 288, whisker, 2.5, 200)
        thick_line(256 + sx * 108, 322, 256 + sx * 162, 326, whisker, 2.5, 200)
        thick_line(256 + sx * 106, 344, 256 + sx * 158, 362, whisker, 2.5, 200)

    # Paws at the bottom, with toe lines.
    for px in (196, 316):
        fill_ellipse(px, 446, 38, 22, outline)
        fill_ellipse(px, 444, 34, 19, cream)
        thick_line(px - 10, 434, px - 10, 452, outline, 2)
        thick_line(px + 10, 434, px + 10, 452, outline, 2)

    # Two floating hearts.
    heart(428, 84, 22, (255, 143, 171), 230)
    heart(84, 118, 15, (255, 143, 171), 200)


def downsample():
    """Box-average the 2x framebuffer down to 512x512 RGB bytes."""
    out = bytearray(W * H * 3)
    for y in range(H):
        for x in range(W):
            r = g = b = 0
            for dy in (0, 1):
                base = ((y * SS + dy) * SW + x * SS) * 3
                for dx in (0, 1):
                    i = base + dx * 3
                    r += buf[i]
                    g += buf[i + 1]
                    b += buf[i + 2]
            o = (y * W + x) * 3
            out[o] = (r + 2) // 4
            out[o + 1] = (g + 2) // 4
            out[o + 2] = (b + 2) // 4
    return bytes(out)


def png_bytes(rgb, w, h):
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    draw_background()
    draw_kitten()
    rgb = downsample()
    data = png_bytes(rgb, W, H)
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "entry.png"), "wb") as f:
        f.write(data)


if __name__ == "__main__":
    main()
