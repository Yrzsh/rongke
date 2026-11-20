# -*- coding: utf-8 -*-
"""生物图元库：把「细胞 / 细胞核 / 内质网 / 蛋白 / MHC」画成**图形**，不是框。

为什么需要它（2026-11-14）：用户给的参考图是 BioRender 式机制图 ——
节点是一个有立体感、有内部结构的**图元**，标签在图元下方；区室是一圈
细胞膜轮廓，不是矩形虚线框。`svgdiagram` 原来的「圆角矩形 + 文字」再调
参数也变不成那种图，缺的是这一层**图元**。

设计约束（与全仓一致）：
  · 零第三方依赖、零外部素材：所有图元都是手写的 path/ellipse + 径向渐变，
    不引用任何 PNG / iconfont / 网络资源（出图后离线也能打开）。
  · 图元在 100×100 的局部坐标里画，调用方给包围框，统一 `scale` 过去；
    描边用 `vector-effect="non-scaling-stroke"`，缩放不改变线宽。
  · 颜色只由 tint 决定（区室语义色），不在图元里各写一套配色。

这里**不是** BioRender 素材的复刻：形状是按公开细胞形态常识重新画的简化
示意图，用于「预览级机制图」，不冒充投稿级医学插画。
"""
from __future__ import annotations

import html
import math
import re

_GRAPHIC = re.compile(r'<(path|circle|ellipse|line|polygon|rect)\b')

#: 图元的配色：tint（区室色）→ 渐变 id。立体感的来源是**径向渐变**
#: （亮部偏左上、暗部偏右下），不是投影滤镜 —— 滤镜在部分查看端会丢。
_TINT_TO_GRAD = {
    "#B23A2E": "bioRed", "#2E5A63": "bioTeal", "#4C6B52": "bioGreen",
    "#7A5A8E": "bioPurple", "#3E6C8C": "bioBlue", "#B0813E": "bioGold",
}
_GRAD_COLORS = {
    "bioRed": ("#F6C9C2", "#D96A5C", "#A83228"),
    "bioTeal": ("#CDE7EA", "#7FB4BE", "#2E5A63"),
    "bioGreen": ("#D5E8D2", "#8FBC8A", "#3F6B46"),
    "bioPurple": ("#E4D6EE", "#B195C6", "#6B4A82"),
    "bioBlue": ("#D2E4F2", "#8FB6D6", "#3A6285"),
    "bioGold": ("#F3E3C2", "#DDBB7A", "#A87C33"),
}


def defs() -> str:
    """所有图元共用的渐变定义，一次注入 `<defs>`。"""
    out = []
    for gid, (hi, mid, lo) in _GRAD_COLORS.items():
        out.append(
            f'<radialGradient id="{gid}" cx="36%" cy="32%" r="72%">'
            f'<stop offset="0%" stop-color="{hi}"/>'
            f'<stop offset="55%" stop-color="{mid}"/>'
            f'<stop offset="100%" stop-color="{lo}"/></radialGradient>')
    # 细胞质：极浅的填充 + 膜是两层描边（外膜厚、内膜细），像参考图那样。
    out.append('<radialGradient id="bioCytosol" cx="50%" cy="42%" r="70%">'
               '<stop offset="0%" stop-color="#FFFFFF"/>'
               '<stop offset="100%" stop-color="#EAF4F6"/></radialGradient>')
    return "".join(out)


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _grad(tint: str) -> str:
    return _TINT_TO_GRAD.get(str(tint or "").upper(), "bioTeal")


def _wrap(x: float, y: float, w: float, h: float, inner: str) -> str:
    """把 100×100 局部坐标的图元放进目标包围框。

    非均匀缩放会把描边一起拉扁，所以给每个图元元素加
    `vector-effect="non-scaling-stroke"`：填充随框走，线宽不随缩放变。
    """
    inner = _GRAPHIC.sub(r'<\1 vector-effect="non-scaling-stroke"', inner)
    return (f'<g transform="translate({x:.2f},{y:.2f}) '
            f'scale({w / 100:.4f},{h / 100:.4f})" '
            f'stroke-linejoin="round" stroke-linecap="round">{inner}</g>')


def _sphere(tint: str, nucleus: bool = True) -> str:
    """一个细胞球：渐变外膜 + 深色核 + 几颗胞质颗粒。"""
    g = _grad(tint)
    dots = "".join(
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#FFFFFF" opacity="0.28"/>'
        for cx, cy, r in ((28, 62, 3.0), (70, 34, 2.4), (66, 72, 2.0), (34, 30, 1.8)))
    nuc = ('<circle cx="54" cy="52" r="21" fill="#000000" opacity="0.22"/>'
           '<circle cx="50" cy="48" r="19" fill="#FFFFFF" opacity="0.10"/>') if nucleus else ""
    return (f'<circle cx="50" cy="50" r="46" fill="url(#{g})"/>'
            f'<circle cx="50" cy="50" r="46" fill="none" stroke="#00000022" '
            f'stroke-width="1.2"/>{nuc}{dots}')


def cell(x, y, w, h, tint): return _wrap(x, y, w, h, _sphere(tint))


def cell_t(x, y, w, h, tint): return _wrap(x, y, w, h, _sphere("#B23A2E"))


def cell_b(x, y, w, h, tint):
    """B 细胞：球 + 表面伸出的 Y 形抗体。"""
    ys = []
    for ang in (20, 80, 140, 200, 260, 320):
        a = math.radians(ang)
        cx, cy = 50 + 46 * math.cos(a), 50 + 46 * math.sin(a)
        tx, ty = 50 + 58 * math.cos(a), 50 + 58 * math.sin(a)
        # Y 的两臂朝外张开
        lx, ly = tx + 6 * math.cos(a + 0.7), ty + 6 * math.sin(a + 0.7)
        rx, ry = tx + 6 * math.cos(a - 0.7), ty + 6 * math.sin(a - 0.7)
        ys.append(f'<path d="M{cx:.1f},{cy:.1f} L{tx:.1f},{ty:.1f} '
                  f'M{tx:.1f},{ty:.1f} L{lx:.1f},{ly:.1f} '
                  f'M{tx:.1f},{ty:.1f} L{rx:.1f},{ry:.1f}" fill="none" '
                  f'stroke="#5A7C99" stroke-width="2.4"/>')
    return _wrap(x, y, w, h, _sphere("#3E6C8C") + "".join(ys))


def cell_cd4(x, y, w, h, tint): return _wrap(x, y, w, h, _sphere("#4C6B52"))


def nucleus(x, y, w, h, tint):
    """细胞核：核膜双圈 + 核仁 + 核孔点。"""
    pores = "".join(
        f'<circle cx="{50 + 40 * math.cos(a):.1f}" cy="{50 + 34 * math.sin(a):.1f}" '
        f'r="1.6" fill="#7FB4BE"/>'
        for a in [i * math.pi / 5 for i in range(10)])
    inner = ('<ellipse cx="50" cy="50" rx="42" ry="36" fill="url(#bioCytosol)" '
             'stroke="#7FB4BE" stroke-width="2"/>'
             '<ellipse cx="50" cy="50" rx="37" ry="31" fill="none" '
             'stroke="#A9CBD3" stroke-width="1"/>'
             '<ellipse cx="52" cy="48" rx="13" ry="11" fill="#E8D9B8" '
             'stroke="#C9B48A" stroke-width="1"/>'
             '<ellipse cx="50" cy="45" rx="7" ry="6" fill="#F4ECDD"/>' + pores)
    return _wrap(x, y, w, h, inner)


def er(x, y, w, h, tint):
    """内质网：一摞折叠片层，靠右呈弧形。"""
    paths = []
    for i in range(5):
        yy = 18 + i * 14
        paths.append(
            f'<path d="M18,{yy} C34,{yy - 8} 60,{yy + 10} 84,{yy - 2}" '
            f'fill="none" stroke="#9B86B8" stroke-width="5" opacity="0.85"/>')
        paths.append(
            f'<path d="M18,{yy} C34,{yy - 8} 60,{yy + 10} 84,{yy - 2}" '
            f'fill="none" stroke="#C7B6DC" stroke-width="1.6"/>')
    return _wrap(x, y, w, h, "".join(paths))


def golgi(x, y, w, h, tint):
    """高尔基体：几条叠在一起的扁弧 + 小囊泡。"""
    parts = []
    for i in range(4):
        yy = 30 + i * 12
        parts.append(f'<path d="M22,{yy} Q50,{yy - 16} 78,{yy - 4}" fill="none" '
                     f'stroke="#8E76AB" stroke-width="6"/>')
    parts += [f'<circle cx="{cx}" cy="{cy}" r="3.4" fill="#B9A3D0"/>'
              for cx, cy in ((18, 66), (84, 58), (70, 82))]
    return _wrap(x, y, w, h, "".join(parts))


def mito(x, y, w, h, tint):
    """线粒体：椭圆外膜 + 内部嵴（折线）。"""
    inner = ('<ellipse cx="50" cy="50" rx="42" ry="26" fill="#F2C9CB" '
             'stroke="#C77E86" stroke-width="1.6"/>'
             '<path d="M14,50 C24,34 30,66 40,50 C50,34 56,66 66,50 C76,34 82,60 88,48" '
             'fill="none" stroke="#C77E86" stroke-width="3.4"/>')
    return _wrap(x, y, w, h, inner)


def protein(x, y, w, h, tint):
    """蛋白团：几个叠在一起的圆角块，读作「一团蛋白」，不是几何圆。"""
    inner = ('<path d="M30,58 a16,16 0 0 1 4,-30 a18,18 0 0 1 30,-6 '
             'a15,15 0 0 1 12,26 a14,14 0 0 1 -18,22 a15,15 0 0 1 -28,-12 Z" '
             'fill="#CBB6DC" stroke="#8E76AB" stroke-width="1.6"/>'
             '<circle cx="46" cy="44" r="6" fill="#E4D6EE"/>')
    return _wrap(x, y, w, h, inner)


def dna(x, y, w, h, tint):
    """DNA：环状双链（两条正弦 + 横档），参考图里那个圆圈分子。"""
    pts1, pts2 = [], []
    for i in range(49):
        a = i / 48 * 2 * math.pi
        r1 = 40 + 5 * math.sin(8 * a)
        r2 = 33 + 5 * math.sin(8 * a + math.pi)
        pts1.append((50 + r1 * math.cos(a), 50 + r1 * math.sin(a)))
        pts2.append((50 + r2 * math.cos(a), 50 + r2 * math.sin(a)))
    d1 = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts1) + " Z"
    d2 = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts2) + " Z"
    inner = (f'<path d="{d1}" fill="none" stroke="#C9A24B" stroke-width="2.2"/>'
             f'<path d="{d2}" fill="none" stroke="#E0C88A" stroke-width="2.2"/>')
    return _wrap(x, y, w, h, inner)


def mrna(x, y, w, h, tint):
    """mRNA：一条波浪单链 + 5' 帽标记。"""
    pts = [(8 + i * 84 / 24, 50 + 12 * math.sin(i / 24 * 3 * math.pi))
           for i in range(25)]
    d = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    inner = (f'<path d="{d}" fill="none" stroke="#C96F6F" stroke-width="3"/>'
             '<circle cx="10" cy="50" r="4" fill="#C96F6F"/>')
    return _wrap(x, y, w, h, inner)


def ribosome(x, y, w, h, tint):
    inner = ('<ellipse cx="50" cy="60" rx="26" ry="18" fill="#9FBF9A" '
             'stroke="#6E8F6A" stroke-width="1.4"/>'
             '<ellipse cx="50" cy="36" rx="16" ry="12" fill="#C2DABE" '
             'stroke="#6E8F6A" stroke-width="1.4"/>')
    return _wrap(x, y, w, h, inner)


def antibody(x, y, w, h, tint):
    inner = ('<path d="M50,86 L50,46 M50,46 L26,18 M50,46 L74,18" fill="none" '
             'stroke="#5A7C99" stroke-width="7"/>'
             '<path d="M26,18 L22,10 M74,18 L78,10" stroke="#8FB6D6" '
             'stroke-width="7"/>')
    return _wrap(x, y, w, h, inner)


def mhc(x, y, w, h, tint):
    """MHC：两根跨膜肽柱 + 顶部的抗原结合槽（一个小凹槽）。"""
    inner = ('<rect x="30" y="26" width="12" height="58" rx="5" fill="#C9A24B"/>'
             '<rect x="58" y="26" width="12" height="58" rx="5" fill="#7FB4BE"/>'
             '<path d="M28,26 Q50,4 72,26" fill="none" stroke="#7A5A8E" '
             'stroke-width="7"/>'
             '<path d="M38,18 Q50,10 62,18" fill="none" stroke="#B23A2E" '
             'stroke-width="5"/>')
    return _wrap(x, y, w, h, inner)


def liposome(x, y, w, h, tint):
    """脂质体：双层磷脂环（一圈小球 + 尾巴）包着一条 mRNA。"""
    parts = []
    for i in range(20):
        a = i / 20 * 2 * math.pi
        cx, cy = 50 + 40 * math.cos(a), 50 + 40 * math.sin(a)
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="#E3C56F" '
                     f'stroke="#C9A24B" stroke-width="1"/>')
    parts.append('<circle cx="50" cy="50" r="30" fill="#F7EFDA"/>')
    for k in range(3):
        yy = 38 + k * 11
        parts.append(f'<path d="M34,{yy} q8,-7 16,0 t16,0" fill="none" '
                     f'stroke="#C96F6F" stroke-width="2.6"/>')
    return _wrap(x, y, w, h, "".join(parts))


def virus(x, y, w, h, tint):
    parts = []
    for i in range(12):
        a = i / 12 * 2 * math.pi
        x1, y1 = 50 + 30 * math.cos(a), 50 + 30 * math.sin(a)
        x2, y2 = 50 + 44 * math.cos(a), 50 + 44 * math.sin(a)
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="#6E9AA8" stroke-width="3"/>'
                     f'<circle cx="{x2:.1f}" cy="{y2:.1f}" r="3.4" fill="#6E9AA8"/>')
    parts.append('<circle cx="50" cy="50" r="30" fill="#CFE3E8" stroke="#6E9AA8" '
                 'stroke-width="1.6"/>')
    parts.append('<path d="M36,50 q7,-8 14,0 t14,0" fill="none" stroke="#6E9AA8" '
                 'stroke-width="2.4"/>')
    return _wrap(x, y, w, h, "".join(parts))


def lysate(x, y, w, h, tint):
    """肿瘤裂解物：几块不规则碎片。"""
    parts = []
    for cx, cy, r in ((36, 42, 16), (58, 34, 13), (62, 58, 15), (40, 64, 11)):
        pts = []
        for i in range(7):
            a = i / 7 * 2 * math.pi
            rr = r * (0.75 + 0.25 * math.sin(3 * a + cx))
            pts.append(f"{cx + rr * math.cos(a):.1f},{cy + rr * math.sin(a):.1f}")
        parts.append(f'<polygon points="{" ".join(pts)}" fill="#8FB6AD" opacity="0.85"/>')
    return _wrap(x, y, w, h, "".join(parts))


def dendritic(x, y, w, h, tint):
    """树突状细胞：球体 + 一圈分叉的短突起（DC 的标志形态）。"""
    parts = []
    for ang in range(10, 370, 45):
        a = math.radians(ang)
        x1, y1 = 50 + 30 * math.cos(a), 50 + 30 * math.sin(a)
        mx, my = 50 + 40 * math.cos(a + 0.18), 50 + 40 * math.sin(a + 0.18)
        tx, ty = 50 + 46 * math.cos(a - 0.08), 50 + 46 * math.sin(a - 0.08)
        f1 = (tx + 7 * math.cos(a + 0.7), ty + 7 * math.sin(a + 0.7))
        f2 = (tx + 7 * math.cos(a - 0.7), ty + 7 * math.sin(a - 0.7))
        parts.append(
            f'<path d="M{x1:.1f},{y1:.1f} L{mx:.1f},{my:.1f} L{tx:.1f},{ty:.1f} '
            f'M{tx:.1f},{ty:.1f} L{f1[0]:.1f},{f1[1]:.1f} '
            f'M{tx:.1f},{ty:.1f} L{f2[0]:.1f},{f2[1]:.1f}" fill="none" '
            f'stroke="#8FB6D6" stroke-width="2.4"/>')
    parts.append('<circle cx="50" cy="50" r="30" fill="url(#bioTeal)"/>'
                 '<circle cx="48" cy="49" r="14" fill="#00000033"/>'
                 '<circle cx="46" cy="47" r="12" fill="#FFFFFF18"/>')
    return _wrap(x, y, w, h, "".join(parts))


def macrophage(x, y, w, h, tint):
    """巨噬细胞：不规则阿米巴轮廓 + 伪足 + 吞噬颗粒。"""
    pts = []
    for i in range(14):
        a = i / 14 * 2 * math.pi
        r = 40 + 5 * math.sin(3 * a + 0.6) + 3 * math.sin(5 * a)
        pts.append(f"{50 + r * math.cos(a):.1f},{50 + r * math.sin(a):.1f}")
    parts = [f'<polygon points="{" ".join(pts)}" fill="url(#bioPurple)" '
             f'stroke="#6B4A82" stroke-width="1.4" opacity="0.92"/>']
    for cx, cy, r in ((34, 40, 4.2), (58, 62, 3.4), (66, 38, 2.6)):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#FFFFFF" opacity="0.3"/>')
    parts.append('<ellipse cx="46" cy="52" rx="15" ry="12" fill="#00000033"/>')
    return _wrap(x, y, w, h, "".join(parts))


def tumor(x, y, w, h, tint):
    """肿瘤细胞：不规则大细胞（多核 + 异形轮廓），一眼能区别于正常细胞。"""
    pts = []
    for i in range(16):
        a = i / 16 * 2 * math.pi
        r = 44 + 4 * math.sin(4 * a + 1.2)
        pts.append(f"{50 + r * math.cos(a):.1f},{50 + r * math.sin(a):.1f}")
    parts = [f'<polygon points="{" ".join(pts)}" fill="url(#bioRed)" '
             f'stroke="#A83228" stroke-width="1.3"/>']
    for cx, cy, rx, ry in ((42, 44, 12, 10), (62, 56, 11, 9), (48, 66, 8, 7)):
        parts.append(f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" '
                     f'fill="#00000038"/><ellipse cx="{cx - 1}" cy="{cy - 1}" '
                     f'rx="{rx * 0.6:.1f}" ry="{ry * 0.55:.1f}" fill="#FFFFFF16"/>')
    return _wrap(x, y, w, h, "".join(parts))


def tissue(x, y, w, h, tint):
    """组织切片：一排紧密排列的上皮样细胞，每格一个核。"""
    parts = []
    cols, rows = 4, 3
    cw, ch = 96 / cols, 96 / rows
    for r in range(rows):
        for c in range(cols):
            cx = 2 + cw * (c + 0.5)
            cy = 2 + ch * (r + 0.5)
            rw, rh = cw * 0.42, ch * 0.42
            pts = []
            for i in range(6):
                a = i / 6 * 2 * math.pi + math.pi / 6
                pts.append(f"{cx + rw * math.cos(a):.1f},{cy + rh * math.sin(a):.1f}")
            parts.append(f'<polygon points="{" ".join(pts)}" fill="#E7F0E4" '
                         f'stroke="#8FAF8A" stroke-width="1.2"/>'
                         f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{min(rw, rh) * 0.34:.1f}" '
                         f'fill="#9FB79A"/>')
    return _wrap(x, y, w, h, "".join(parts))


def organ(x, y, w, h, tint):
    """器官（抽象叶形，可用作肝/脾/瘤块）：带血管点的实体。"""
    parts = ['<path d="M22,30 C18,58 34,86 60,88 C82,89 92,70 88,48 '
             'C85,30 70,14 50,14 C36,14 25,18 22,30 Z" fill="url(#bioGold)" '
             'stroke="#A87C33" stroke-width="1.4"/>',
             '<path d="M50,20 C46,40 54,62 48,84" fill="none" stroke="#A87C33" '
             'stroke-width="1.6" opacity="0.7"/>']
    for cx, cy, r in ((34, 40, 3.0), (66, 34, 2.4), (62, 64, 2.8), (38, 70, 2.2)):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="#B23A2E" opacity="0.6"/>')
    return _wrap(x, y, w, h, "".join(parts))


def mouse(x, y, w, h, tint):
    """实验小鼠（侧视）：身体 + 头 + 耳 + 尾。"""
    inner = ('<ellipse cx="50" cy="60" rx="34" ry="21" fill="url(#bioGold)" '
             'stroke="#A87C33" stroke-width="1.2"/>'
             '<circle cx="82" cy="46" r="13" fill="#E7D3AA" stroke="#A87C33" stroke-width="1.2"/>'
             '<circle cx="86" cy="30" r="8" fill="#E7D3AA" stroke="#A87C33" stroke-width="1.2"/>'
             '<circle cx="74" cy="30" r="7" fill="#E7D3AA" stroke="#A87C33" stroke-width="1.2"/>'
             '<circle cx="90" cy="45" r="2.2" fill="#3A2E1E"/>'
             '<path d="M18,62 C6,66 4,80 14,84" fill="none" stroke="#A87C33" stroke-width="2.2"/>')
    return _wrap(x, y, w, h, inner)


def bacteria(x, y, w, h, tint):
    """细菌：杆状菌体 + 鞭毛 + 质粒点。"""
    parts = ['<rect x="18" y="30" width="58" height="34" rx="17" fill="url(#bioGreen)" '
             'stroke="#3F6B46" stroke-width="1.4"/>',
             '<path d="M76,47 C86,40 88,56 96,50" fill="none" stroke="#6E8F6A" '
             'stroke-width="2"/>']
    for cx, cy in ((34, 47), (52, 40), (62, 55)):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="3.2" fill="#FFFFFF" opacity="0.35"/>')
    return _wrap(x, y, w, h, "".join(parts))


def exosome(x, y, w, h, tint):
    """外泌体：小膜泡 + 表面四个膜蛋白 + 腔内货物点。"""
    parts = ['<circle cx="50" cy="50" r="30" fill="url(#bioCytosol)" '
             'stroke="#7FB4BE" stroke-width="2"/>']
    for ang in (30, 120, 210, 300):
        a = math.radians(ang)
        parts.append(f'<rect x="{50 + 30 * math.cos(a) - 3:.1f}" '
                     f'y="{50 + 30 * math.sin(a) - 3:.1f}" width="6" height="6" '
                     f'rx="1.4" fill="#C9A24B"/>')
    for cx, cy in ((42, 44), (58, 52), (46, 60)):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="3.4" fill="#8FB6D6"/>')
    return _wrap(x, y, w, h, "".join(parts))


def membrane(x, y, w, h, tint):
    """细胞膜区室：一圈膜（外粗内细）+ 极浅胞质填充。用于 `groups[].kind="cell"`。"""
    inner = (f'<ellipse cx="50" cy="50" rx="48" ry="47" fill="url(#bioCytosol)"/>'
             f'<ellipse cx="50" cy="50" rx="48" ry="47" fill="none" '
             f'stroke="#9CC6CE" stroke-width="2.4"/>'
             f'<ellipse cx="50" cy="50" rx="45.5" ry="44.5" fill="none" '
             f'stroke="#C4DFE5" stroke-width="1"/>')
    return _wrap(x, y, w, h, inner)


#: 节点 `icon` 的可选值。写错名字要报错，不许静默换成一个通用圆。
ICONS = {
    "cell": cell, "cell_t": cell_t, "cell_b": cell_b, "cell_cd4": cell_cd4,
    "nucleus": nucleus, "er": er, "golgi": golgi, "mito": mito,
    "protein": protein, "dna": dna, "mrna": mrna, "ribosome": ribosome,
    "antibody": antibody, "mhc": mhc, "liposome": liposome, "virus": virus,
    "lysate": lysate,
    "dendritic": dendritic, "macrophage": macrophage, "tumor": tumor,
    "tissue": tissue, "organ": organ, "mouse": mouse, "bacteria": bacteria,
    "exosome": exosome,
}

#: 每种图元的**默认包围框**（px）。细胞球要大、分子要小 —— 参考图里细胞
#: 与分子本就不是一个量级，统一尺寸会把分子画得和细胞一样大。
ICON_SIZE = {
    "cell": (78, 78), "cell_t": (78, 78), "cell_b": (74, 74), "cell_cd4": (74, 74),
    "nucleus": (74, 64), "er": (66, 60), "golgi": (60, 54), "mito": (52, 36),
    "protein": (40, 40), "dna": (42, 42), "mrna": (58, 26), "ribosome": (36, 34),
    "antibody": (28, 34), "mhc": (34, 52), "liposome": (62, 62), "virus": (54, 54),
    "lysate": (58, 54),
    "dendritic": (82, 82), "macrophage": (80, 80), "tumor": (82, 82),
    "tissue": (76, 76), "organ": (66, 66), "mouse": (80, 54),
    "bacteria": (58, 34), "exosome": (38, 38),
}


def draw_icon(name: str, x: float, y: float, w: float, h: float,
              tint: str = "") -> str:
    """按名字画图元。名字不在表里就抛错（写错 icon 不该静默变成别的图形）。"""
    fn = ICONS.get(name)
    if fn is None:
        raise KeyError(f"没有名为 `{name}` 的生物图元（可选：{'、'.join(ICONS)}）")
    return fn(x, y, w, h, tint)
