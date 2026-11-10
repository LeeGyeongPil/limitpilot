"""Build the LimitPilot icon set with the Python standard library only.

    python scripts/build_icons.py --out DIR            # write the icons to DIR
    python scripts/build_icons.py --out DIR --check assets/icon
                                                       # …and compare with the committed set

Design: a navy rounded square, a 270° teal gauge arc whose last 15 % is an amber redline,
and a white needle that stops just before the redline with a small chevron at its tip
("turn before the limit"). The geometry below is the single source; the SVG and every
raster size are rendered from it.

Rasterising: each shape has a signed distance function (SDF). A pixel's coverage is
``clamp(0.5 - distance_in_pixels)``, which gives one-pixel anti-aliasing without
super-sampling. PNG is written with zlib; ICNS and ICO are packed by hand. The output is
deterministic: the same script always produces the same bytes.
"""

import argparse
import math
import os
import struct
import sys
import zlib

# ---------------------------------------------------------------- 기하(1024 기준 좌표, y 아래로)

CANVAS = 1024.0
BODY = (100.0, 100.0, 924.0, 924.0)       # 둥근 사각형(macOS 아이콘 격자)
BODY_RADIUS = 185.0
CENTER = (512.0, 548.0)                    # 게이지 중심
ARC_RADIUS = 268.0
ARC_WIDTH = 66.0
ARC_START = 225.0                          # 도(수학 좌표, 반시계 +) — 왼쪽 아래에서 시작
ARC_SWEEP = 270.0                          # 시계 방향으로 270°
REDLINE_FROM = 0.85                        # 호의 마지막 15% 가 레드라인
NEEDLE_AT = 0.80                           # 바늘은 레드라인 바로 앞
NEEDLE_LEN = 214.0
NEEDLE_WIDTH = 40.0
HUB_RADIUS = 50.0
CHEVRON_SIZE = 46.0                        # 바늘 끝 셰브론(진로 표식)
CHEVRON_WIDTH = 30.0

NAVY_TOP = (0x0F, 0x1E, 0x33)
NAVY_BOTTOM = (0x17, 0x37, 0x5A)
TEAL = (0x2E, 0xC4, 0xB6)
AMBER = (0xFF, 0xB0, 0x20)
WHITE = (0xF5, 0xF7, 0xFA)

OUTPUTS = ("limitpilot.svg", "LimitPilot.icns", "LimitPilot.ico",
           "menubar-template.png", "menubar-template@2x.png")


def _hex(c):
    return "#%02X%02X%02X" % c


def _polar(frac, radius):
    """Point on the gauge at ``frac`` (0..1) of the sweep, in image coordinates."""
    ang = math.radians(ARC_START - ARC_SWEEP * frac)
    return (CENTER[0] + math.cos(ang) * radius, CENTER[1] - math.sin(ang) * radius)


def _needle_geometry():
    tip = _polar(NEEDLE_AT, NEEDLE_LEN)
    ang = math.radians(ARC_START - ARC_SWEEP * NEEDLE_AT)
    ux, uy = math.cos(ang), -math.sin(ang)          # 바늘 방향(이미지 좌표)
    px, py = -uy, ux                                  # 수직 방향
    apex = (tip[0] + ux * CHEVRON_SIZE * 0.55, tip[1] + uy * CHEVRON_SIZE * 0.55)
    wing1 = (apex[0] - ux * CHEVRON_SIZE + px * CHEVRON_SIZE * 0.8,
             apex[1] - uy * CHEVRON_SIZE + py * CHEVRON_SIZE * 0.8)
    wing2 = (apex[0] - ux * CHEVRON_SIZE - px * CHEVRON_SIZE * 0.8,
             apex[1] - uy * CHEVRON_SIZE - py * CHEVRON_SIZE * 0.8)
    return tip, apex, wing1, wing2


# ---------------------------------------------------------------- SVG (같은 기하)

def svg() -> bytes:
    x0, y0, x1, y1 = BODY
    s0 = _polar(0.0, ARC_RADIUS)
    s1 = _polar(REDLINE_FROM, ARC_RADIUS)
    s2 = _polar(1.0, ARC_RADIUS)
    tip, apex, wing1, wing2 = _needle_geometry()

    def f(v):
        return f"{v:.1f}"

    large1 = 1 if ARC_SWEEP * REDLINE_FROM > 180 else 0
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">',
        "  <title>LimitPilot</title>",
        '  <defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{_hex(NAVY_TOP)}"/>'
        f'<stop offset="1" stop-color="{_hex(NAVY_BOTTOM)}"/></linearGradient></defs>',
        f'  <rect x="{f(x0)}" y="{f(y0)}" width="{f(x1 - x0)}" height="{f(y1 - y0)}" '
        f'rx="{f(BODY_RADIUS)}" fill="url(#bg)"/>',
        f'  <path d="M {f(s0[0])} {f(s0[1])} A {f(ARC_RADIUS)} {f(ARC_RADIUS)} 0 {large1} 1 '
        f'{f(s1[0])} {f(s1[1])}" fill="none" stroke="{_hex(TEAL)}" '
        f'stroke-width="{f(ARC_WIDTH)}" stroke-linecap="round"/>',
        f'  <path d="M {f(s1[0])} {f(s1[1])} A {f(ARC_RADIUS)} {f(ARC_RADIUS)} 0 0 1 '
        f'{f(s2[0])} {f(s2[1])}" fill="none" stroke="{_hex(AMBER)}" '
        f'stroke-width="{f(ARC_WIDTH)}" stroke-linecap="round"/>',
        f'  <line x1="{f(CENTER[0])}" y1="{f(CENTER[1])}" x2="{f(tip[0])}" y2="{f(tip[1])}" '
        f'stroke="{_hex(WHITE)}" stroke-width="{f(NEEDLE_WIDTH)}" stroke-linecap="round"/>',
        f'  <polyline points="{f(wing1[0])},{f(wing1[1])} {f(apex[0])},{f(apex[1])} '
        f'{f(wing2[0])},{f(wing2[1])}" fill="none" stroke="{_hex(WHITE)}" '
        f'stroke-width="{f(CHEVRON_WIDTH)}" stroke-linecap="round" stroke-linejoin="round"/>',
        f'  <circle cx="{f(CENTER[0])}" cy="{f(CENTER[1])}" r="{f(HUB_RADIUS)}" '
        f'fill="{_hex(WHITE)}"/>',
        "</svg>",
        "",
    ]
    return "\n".join(parts).encode("utf-8")


# ---------------------------------------------------------------- SDF 래스터라이저

def _sd_round_rect(x, y):
    x0, y0, x1, y1 = BODY
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - BODY_RADIUS, (y1 - y0) / 2 - BODY_RADIUS
    qx, qy = abs(x - cx) - hx, abs(y - cy) - hy
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - BODY_RADIUS


def _sd_capsule(x, y, a, b, half_width):
    ax, ay = a
    bx, by = b
    pax, pay = x - ax, y - ay
    bax, bay = bx - ax, by - ay
    h = max(0.0, min(1.0, (pax * bax + pay * bay) / (bax * bax + bay * bay)))
    return math.hypot(pax - bax * h, pay - bay * h) - half_width


_ARC_ENDS = None


def _arc(x, y):
    """(distance to the gauge arc, position along the sweep 0..1)."""
    global _ARC_ENDS
    if _ARC_ENDS is None:
        _ARC_ENDS = (_polar(0.0, ARC_RADIUS), _polar(1.0, ARC_RADIUS))
    dx, dy = x - CENTER[0], CENTER[1] - y           # 수학 좌표(y 위)
    phi = math.degrees(math.atan2(dy, dx))
    g = ((ARC_START - phi) % 360.0) / ARC_SWEEP
    if g <= 1.0:
        return abs(math.hypot(dx, dy) - ARC_RADIUS) - ARC_WIDTH / 2, g
    e0, e1 = _ARC_ENDS
    d0 = math.hypot(x - e0[0], y - e0[1])
    d1 = math.hypot(x - e1[0], y - e1[1])
    return min(d0, d1) - ARC_WIDTH / 2, (0.0 if d0 < d1 else 1.0)


def _cov(d_units, scale):
    return max(0.0, min(1.0, 0.5 - d_units * scale))


def _mix(c, s, a):
    return (c[0] + (s[0] - c[0]) * a, c[1] + (s[1] - c[1]) * a, c[2] + (s[2] - c[2]) * a)


def render_app_icon(size: int):
    """RGBA rows for the full-colour app icon at ``size`` × ``size``."""
    scale = size / CANVAS                          # 단위 → 픽셀
    inv = CANVAS / size
    tip, apex, wing1, wing2 = _needle_geometry()
    rows = []
    for py in range(size):
        y = (py + 0.5) * inv
        t = (y - BODY[1]) / (BODY[3] - BODY[1])
        t = max(0.0, min(1.0, t))
        bg = _mix(NAVY_TOP, NAVY_BOTTOM, t)
        row = bytearray()
        for px in range(size):
            x = (px + 0.5) * inv
            a_body = _cov(_sd_round_rect(x, y), scale)
            if a_body <= 0.0:
                row += b"\0\0\0\0"
                continue
            c = bg
            d_arc, g = _arc(x, y)
            a_arc = _cov(d_arc, scale)
            if a_arc > 0.0:
                # 청록 → 앰버 경계는 약 1픽셀 폭으로 섞는다
                width = 1.0 / (ARC_RADIUS * scale * math.radians(ARC_SWEEP))
                k = max(0.0, min(1.0, (g - REDLINE_FROM) / width + 0.5))
                c = _mix(c, _mix(TEAL, AMBER, k), a_arc)
            d_needle = min(_sd_capsule(x, y, CENTER, tip, NEEDLE_WIDTH / 2),
                           _sd_capsule(x, y, wing1, apex, CHEVRON_WIDTH / 2),
                           _sd_capsule(x, y, wing2, apex, CHEVRON_WIDTH / 2),
                           math.hypot(x - CENTER[0], y - CENTER[1]) - HUB_RADIUS)
            a_needle = _cov(d_needle, scale)
            if a_needle > 0.0:
                c = _mix(c, WHITE, a_needle)
            row += bytes((int(c[0] + 0.5), int(c[1] + 0.5), int(c[2] + 0.5),
                          int(a_body * 255 + 0.5)))
        rows.append(bytes(row))
    return rows


def render_template(size: int):
    """Black + alpha menu bar template: the arc and the needle only, no background."""
    # 템플릿은 몸체 없이 게이지만 크게 — 게이지 외곽을 캔버스에 맞춘다
    reach = ARC_RADIUS + ARC_WIDTH / 2 + 8.0
    left, top = CENTER[0] - reach, CENTER[1] - reach
    span = reach * 2
    scale = size / span
    inv = span / size
    tip, apex, wing1, wing2 = _needle_geometry()
    rows = []
    for py in range(size):
        y = top + (py + 0.5) * inv
        row = bytearray()
        for px in range(size):
            x = left + (px + 0.5) * inv
            d_arc, _ = _arc(x, y)
            d_needle = min(_sd_capsule(x, y, CENTER, tip, NEEDLE_WIDTH / 2),
                           math.hypot(x - CENTER[0], y - CENTER[1]) - HUB_RADIUS)
            a = max(_cov(d_arc, scale), _cov(d_needle, scale))
            row += bytes((0, 0, 0, int(a * 255 + 0.5)))
        rows.append(bytes(row))
    return rows


# ---------------------------------------------------------------- 파일 형식

def png(rows, size: int) -> bytes:
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\0" + r for r in rows)          # 필터 0(None) — 결정적
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def icns(pngs: dict) -> bytes:
    # (형식 코드, 픽셀 크기) — 모두 PNG 데이터(macOS 10.7+)
    order = [(b"icp4", 16), (b"icp5", 32), (b"ic11", 32), (b"icp6", 64), (b"ic12", 64),
             (b"ic07", 128), (b"ic13", 256), (b"ic08", 256), (b"ic14", 512),
             (b"ic09", 512), (b"ic10", 1024)]
    body = b"".join(code + struct.pack(">I", len(pngs[s]) + 8) + pngs[s] for code, s in order)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def _dib(rows, size: int) -> bytes:
    """32-bit BMP (BITMAPINFOHEADER + BGRA bottom-up + empty AND mask) for small ICO frames."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    pixels = bytearray()
    for r in reversed(rows):
        for i in range(0, len(r), 4):
            pixels += bytes((r[i + 2], r[i + 1], r[i], r[i + 3]))
    mask_row = ((size + 31) // 32) * 4
    return header + bytes(pixels) + b"\0" * (mask_row * size)


def ico(frames: dict, pngs: dict) -> bytes:
    sizes = sorted(frames)
    images = [pngs[s] if s >= 256 else _dib(frames[s], s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    for s, data in zip(sizes, images):
        dim = 0 if s >= 256 else s
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


# ---------------------------------------------------------------- 실행

def build_all() -> dict:
    icns_sizes = (16, 32, 64, 128, 256, 512, 1024)
    ico_sizes = (16, 20, 24, 32, 40, 48, 64, 256)
    rows = {}
    for s in sorted(set(icns_sizes) | set(ico_sizes)):
        rows[s] = render_app_icon(s)
    pngs = {s: png(rows[s], s) for s in rows}
    return {
        "limitpilot.svg": svg(),
        "LimitPilot.icns": icns(pngs),
        "LimitPilot.ico": ico({s: rows[s] for s in ico_sizes}, pngs),
        "menubar-template.png": png(render_template(18), 18),
        "menubar-template@2x.png": png(render_template(36), 36),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--out", required=True, help="folder to write the icons to")
    p.add_argument("--check", help="committed icon folder to compare with (byte for byte)")
    args = p.parse_args(argv)
    files = build_all()
    os.makedirs(args.out, exist_ok=True)
    for name in OUTPUTS:
        with open(os.path.join(args.out, name), "wb") as handle:
            handle.write(files[name])
    if args.check:
        different = []
        for name in OUTPUTS:
            try:
                with open(os.path.join(args.check, name), "rb") as handle:
                    if handle.read() != files[name]:
                        different.append(name)
            except OSError:
                different.append(name)
        if different:
            print("icons differ: " + " ".join(different), file=sys.stderr)
            return 1
        print("icons ok: " + " ".join(OUTPUTS) + " (identical)")
        return 0
    print("icons written: " + " ".join(os.path.join(args.out, n) for n in OUTPUTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
