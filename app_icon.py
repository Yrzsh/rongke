# -*- coding: utf-8 -*-
"""生成桌面图标 `rongke/web/app.ico` —— 纯标准库，可重复再生。

    python app_icon.py            # 写出并自检
    python app_icon.py --check    # 只比对：磁盘上那份和现在画出来的是否一致

## 为什么不用 PIL / 网上找个 png

图标是**派生物**，派生物就该能从代码重算出来 —— 和 `deck.py` 里 `DECK_THEMES`
只写一份、`design.py` 只写槽名是同一条纪律。为一个 16 KB 的文件引入 pillow
（以及它的二进制轮子）去换"能重算"这个性质，不划算：荣珂的内核零依赖是定律 23。

## 颜色不是审美选择，是抄来的

三个值全部取自控制台的真令牌，见 `rongke/web/index.html` 的设计令牌段：

| 用途 | 令牌 | 值 |
|---|---|---|
| 卡片底 | `--ink` (`--b-1000`) | `#0d0d0d` |
| 主体 | `--biz` (`--dk-400`) | `#0b7fff` |
| 高光 | `--on-ink` | `#ffffff` |

（2026-09-29 设计令牌整体对齐 ZCode —— zai-org/ZCode，Apache-2.0 —— 后按新值重画。）

⇒ 图标和窗口里的界面**同一个蓝**。改令牌时记得一起 `python app_icon.py`。

## 画的是"珂"

珂 = 像玉的石头。所以是一张切面宝石：五边形冠部 + 桌面 + 腰棱，
不是字母、不是笑脸。细节按尺寸分级 —— 16px 上画切面只会糊成一团脏点。
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "rongke" / "web" / "app.ico"

INK = (0x0D, 0x0D, 0x0D)        # --ink / --b-1000（Zai Light 的 foreground-strong）
BIZ = (0x0B, 0x7F, 0xFF)        # --biz / --dk-400（ZCode icon-blue）
WHITE = (0xFF, 0xFF, 0xFF)      # --on-ink

SIZES = (16, 24, 32, 48, 256)


# ── 几何：一律用 0..1 归一坐标，逐尺寸独立重画（不是把大图缩下来）──────────
def _rounded_rect(inset: float, radius: float) -> list[tuple[float, float]]:
    """圆角矩形，按面积展开成多边形（角上各 6 段）。"""
    a, b = inset, 1.0 - inset
    r = radius
    pts: list[tuple[float, float]] = []
    for (cx, cy, s0, s1) in ((b - r, b - r, 0.0, 0.5), (a + r, b - r, 0.5, 1.0),
                             (a + r, a + r, 1.0, 1.5), (b - r, a + r, 1.5, 2.0)):
        for i in range(7):
            ang = (s0 + (s1 - s0) * i / 6.0) * 3.141592653589793
            pts.append((cx + r * _cos(ang), cy + r * _sin(ang)))
    return pts


def _cos(x: float) -> float:
    import math
    return math.cos(x)


def _sin(x: float) -> float:
    import math
    return math.sin(x)


def _gem() -> list[tuple[float, float]]:
    """宝石轮廓：台面两条上棱 + 腰 + 尖底。"""
    return [(0.26, 0.30), (0.74, 0.30), (0.86, 0.48), (0.50, 0.84), (0.14, 0.48)]


def _diamond(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    return [(cx, cy - r), (cx + r * 0.42, cy), (cx, cy + r), (cx - r * 0.42, cy)]


def _quad(p0, p1, w: float) -> list[tuple[float, float]]:
    """把一条线段粗化成四边形，用于切面线。"""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    ln = (dx * dx + dy * dy) ** 0.5 or 1.0
    ox, oy = -dy / ln * w, dx / ln * w
    return [(p0[0] + ox, p0[1] + oy), (p1[0] + ox, p1[1] + oy),
            (p1[0] - ox, p1[1] - oy), (p0[0] - ox, p0[1] - oy)]


# ── 光栅：扫描线填充 + 4x 超采样降来 ⇒ 边缘是平滑的，不是锯的 ─────────────
def _fill(shape: list[tuple[float, float]], n: int, ss: int = 4) -> bytearray:
    m = n * ss
    cov = bytearray(m * m)
    for y in range(m):
        yy = (y + 0.5) / m
        xs: list[float] = []
        k = len(shape)
        for i in range(k):
            (x0, y0), (x1, y1) = shape[i], shape[(i + 1) % k]
            if (y0 <= yy < y1) or (y1 <= yy < y0):
                xs.append(x0 + (yy - y0) * (x1 - x0) / (y1 - y0))
        xs.sort()
        row = y * m
        for i in range(0, len(xs) - 1, 2):
            lo = max(int(xs[i] * m + 0.5), 0)
            hi = min(int(xs[i + 1] * m + 0.5), m)
            if hi > lo:
                cov[row + lo:row + hi] = b"\x01" * (hi - lo)
    out = bytearray(n * n)
    a = ss * ss
    for y in range(n):
        for x in range(n):
            s = 0
            for dy in range(ss):
                base = (y * ss + dy) * m + x * ss
                s += sum(cov[base:base + ss])
            out[y * n + x] = round(s * 255 / a)
    return out


def _paint(buf: list[list[int]], shape, n: int, rgb, alpha: int = 255) -> None:
    """按覆盖率把一层合成进 RGBA 缓冲（后者覆盖前者，边界做 alpha 混合）。"""
    cov = _fill(shape, n)
    for i in range(n * n):
        c = cov[i]
        if not c:
            continue
        a = c * alpha // 255
        px = buf[i]
        na = px[3] + (255 - px[3]) * a // 255
        if not na:
            continue
        for j in range(3):
            px[j] = (px[j] * px[3] * (255 - a) + rgb[j] * a * 255) // (na * 255 or 1) \
                if na else px[j]
        px[3] = na


def render(n: int) -> bytes:
    """画一个 n×n 的 RGBA 位图。"""
    buf = [[0, 0, 0, 0] for _ in range(n * n)]
    detail = n >= 32
    _paint(buf, _rounded_rect(0.045, 0.235 if detail else 0.16), n, INK)
    _paint(buf, _gem(), n, BIZ)
    if detail:
        # 台面：冠部上方那条亮面
        _paint(buf, _quad((0.34, 0.30), (0.66, 0.30), 0.012), n, WHITE, 210)
        _paint(buf, _quad((0.26, 0.30), (0.14, 0.48), 0.010), n, INK, 150)
        _paint(buf, _quad((0.74, 0.30), (0.86, 0.48), 0.010), n, INK, 150)
        _paint(buf, _quad((0.34, 0.30), (0.50, 0.84), 0.008), n, INK, 120)
        _paint(buf, _quad((0.66, 0.30), (0.50, 0.84), 0.008), n, INK, 120)
        _paint(buf, _diamond(0.80, 0.19, 0.075), n, WHITE)
    return b"".join(bytes(px) for px in buf)


# ── 容器编码：256 走 PNG（现代 shell），16/24/32/48 走 32 位 DIB（老壳稳）──────
def _png(rgba: bytes, n: int) -> bytes:
    raw = b"".join(b"\x00" + rgba[y * n * 4:(y + 1) * n * 4] for y in range(n))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _dib(rgba: bytes, n: int) -> bytes:
    px = bytearray()
    for y in range(n - 1, -1, -1):                    # BMP 自底向上
        row = rgba[y * n * 4:(y + 1) * n * 4]
        px += bytes(x for i in range(0, len(row), 4)
                    for x in (row[i + 2], row[i + 1], row[i], row[i + 3]))
    # AND 掩码：1 bit/像素，每行补到 32 位 —— 少算一个 `* n` 会让 shell 读到半截掩码
    and_mask = b"\x00" * (((n + 31) // 32) * 4 * n)
    hdr = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0,
                      len(px) + len(and_mask), 0, 0, 0, 0)
    return hdr + bytes(px) + and_mask


def build() -> bytes:
    images = []
    for s in SIZES:
        rgba = render(s)
        images.append((s, _png(rgba, s) if s >= 256 else _dib(rgba, s)))
    n = len(images)
    off = 6 + 16 * n
    out = bytearray(struct.pack("<HHH", 0, 1, n))
    body = bytearray()
    for s, data in images:
        w = 0 if s >= 256 else s
        h = 0 if s >= 256 else s
        planes, bitcount = (1, 32)
        out += struct.pack("<BBBBHHII", w, h, 0, 0, planes, bitcount,
                           len(data), off + len(body))
        body += data
    return bytes(out) + bytes(body)


def _read_sizes(blob: bytes) -> list[tuple[int, int, int]]:
    """从 .ico 里读回 (宽, 高, 字节数)，用于自检（不靠"我写了"）。"""
    reserved, typ, count = struct.unpack_from("<HHH", blob, 0)
    assert reserved == 0 and typ == 1, "不是合法的 ICO 头"
    got = []
    for i in range(count):
        w, h, _, _, planes, bit, size, off = struct.unpack_from("<BBBBHHII", blob, 6 + 16 * i)
        body = blob[off:off + size]
        assert len(body) == size, f"条目 {i} 声明 {size} 字节，实际越界"
        assert body[:8] == b"\x89PNG\r\n\x1a\n" or \
            struct.unpack_from("<I", body, 0)[0] == 40, \
            f"条目 {i} 既不是 PNG 也不是 BITMAPINFOHEADER(40)"
        assert planes == 1 and bit == 32, "每个条目都得是 1 平面 32 位"
        got.append(((w or 256), (h or 256), size))
    return got


def main(argv: list[str]) -> int:
    blob = build()
    sizes = _read_sizes(blob)
    bad = [s for i, s in enumerate(SIZES) if sizes[i][0] != s]
    if bad:
        print(f"[FAIL] 尺寸对不上：{bad}")
        return 1
    print(f"  画了 {len(sizes)} 档：{[f'{a}x{b}' for a, b, _ in sizes]}"
          f"  合计 {len(blob) / 1024:.1f} KB")
    if "--check" in argv:
        cur = OUT.read_bytes() if OUT.exists() else b""
        same = cur == blob
        print(f"  [{'PASS' if same else 'FAIL'}] 磁盘上那份与代码重算的结果"
              f"{'一致' if same else '不一致 ⇒ 跑 python app_icon.py 再生'}]")
        return 0 if same else 1
    OUT.write_bytes(blob)
    print(f"  [{'PASS'}] 写出 {OUT.relative_to(HERE)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
