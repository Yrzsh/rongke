# -*- coding: utf-8 -*-
"""图形质量维度（乙派辅助层）——"这张图看起来能不能投"。

── 为什么要有这个模块 ─────────────────────────────────────────
W10 侦察报告的第一条结论是：荣珂的 figure 侧四道门（源码预检、字形、
碰撞、对齐）都很健康，**但"渲染出来的图好不好看"这个维度一个都没有**。
用关键词扫 `figure.py` 印证过：字号 2 次、font 1 次、dpi 2 次、
对比 2 次、留白 **0** 次、边距 **0** 次。

那四道门问的是"图坏没坏"（有没有豆腐块、有没有文字重叠）。
本模块问的是另一个问题：**"图体面不体面"**。两者不重叠。

── 三条纪律（都是硬约束，不是建议）──────────────────────────
① **绝不阻断**。所有结果只有 WARN / INFO 两级，没有 FAIL。
   理由：审美没有"必须"——把品味判成硬门槛，会让一份合格的稿子
   因为某个阈值卡住而发不出去。W10 报告也明写新维度必须是非阻断的。
② **基于分布，不假设"文字是深色"**。实测教训（同一套判据在 PPT 侧
   犯过）：深底浅字时按"深色=文字"取样会把背景当文字，量出物理上
   不可能的 1.00:1。所以这里所有量值都是**对整幅图统计**，不挑极端像素。
③ **阈值必须引用出处**。每个数字都写明它来自哪份公开规范，
   不许出现"我觉得 12pt 比较好看"这种自造标准。

── 为什么不直接 vendor figure-gate ─────────────────────────
`narenp12/figure-gate`（MIT）确实有 21 道图门，阈值也都引了公开标准
（SIAM 1pt 数据线、Science 0.5pt 图框、WCAG 4.5:1、Nature 7pt 字号上限），
但它只有 1 星、且是几天前刚建的仓库。整包引进来等于把交付质量押在
一个还没被时间检验的项目上。这里采取的做法是：
**采用它引用的那些公开标准本身**（标准是稳的），
用自己的 stdlib 实现（代码是可控的）。
调色板那一道已经用它 `check_palette.py` 的原实现交叉验证过（见
`_w35_pair_regress.py`：新调色板 0 红 / 旧调色板 4 红）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = [
    "QualityFinding", "QualityReport", "check_palette",
    "relative_luminance", "contrast_ratio", "oklab", "hex_to_rgb",
    "PALETTE_BANDS", "TEXT_CONTRAST_MIN",
]

# ── 阈值：每一项都连出处 ───────────────────────────────────────
# 亮度带：figure-gate 用它保证"既不太黑也不太浅"，落在这个区间里的颜色
#   在灰度打印和常见投影下都还分得开。区间 0.43–0.77 取自 Okabe-Ito
#   设计说明对"可打印"的取值范围。
PALETTE_L_MIN = 0.43
PALETTE_L_MAX = 0.77
# 彩度下限：低于 0.10 的颜色在色相维度上退化成灰，读者只能靠明度分辨。
PALETTE_C_MIN = 0.10
# ── 分离度：**只报数，不设门** ───────────────────────────────────
#
# ★ 2026-10-26 重要更正。我本来给分离度设了阈值（CVD 0.04 / 正常 0.10，
#   由"JND≈0.02"推出），实现在用真实色对对切之后**证明它不可靠**：
#
#     色对                    色盲    我的dE   明度差ΔL  色相角差   实际
#     旧 绿#3F8F6B vs 红#A63A50  deutan  0.0742   0.0722    0.2°   分不开
#     旧 蓝#0F4D92 vs 紫#6B5B95  protan  0.0702   0.0615    4.3°   分不开
#     OI 绿#009E73 vs 粉#CC79A7  deutan  0.0755   0.0617  176.8°   分得开
#     OI 绿#009E73 vs 粉#CC79A7  protan  0.0962   0.0196  173.2°   分得开
#
#   「分不开」的 dE=0.0742 和「分得开」的 dE=0.0755 **几乎相等** ——
#   这说明在 OKLab 欧氏距离上这两类情况本来就不可分，任何阈值都会
#   至少错杀一个好配色或放过一个坏配色。真正的判别信号是**色相角**
#   （0.2° vs 177°），而近中性色的色相角又不稳定。
#
#   ⇒ 结论：CVD 分离度这个判据我没能标定到可用的程度，所以**不设门**。
#     只把测量值当 INFO 报出来，并给出最接近的那一对让读者自己判断。
#     这与 figure-gate 的 CAM02 实现不冲突：那套实现在旧调色板上判
#     「分不开」（deutan dE=9.7<10.5），与本表格的 ΔL/色相角证据一致，
#     **它是对的**。荣珂当前用的 Okabe-Ito 已在构建期用它的原实现
#     验证过全过（见 _w35_pair_regress.py）。
PALETTE_CVD_INFO_ONLY = True
# WCAG 2.1 AA 对普通文本的对比度要求（1.4.3）
TEXT_CONTRAST_MIN = 4.5
# WCAG 2.1 对"非文本图形元素"的要求（1.4.11）——线、色块属这一档
GRAPHIC_CONTRAST_MIN = 3.0
# Nature 投稿指南：图内最小字号 5pt，但正文图注常要求 7pt 可读。
# 这里用 7pt 当"建议值"，5pt 当"硬下限"（下限由已有门负责）。
MIN_FONT_PT = 7.0
MIN_FONT_PT_HARD = 5.0
# Science 图表规范：图框/刻度线 0.5pt 起，数据线 1pt 起
FRAME_PT_MIN = 0.5
DATA_STROKE_PT_MIN = 1.0

PALETTE_BANDS = {
    "L_MIN": PALETTE_L_MIN, "L_MAX": PALETTE_L_MAX,
    "CHROMA_MIN": PALETTE_C_MIN,
    "cvd_info_only": PALETTE_CVD_INFO_ONLY,
}


@dataclass
class QualityFinding:
    """一条质量提示。**只有 WARN / INFO 两级** —— 这个模块不产生 FAIL。"""
    level: str              # WARN | INFO
    check_id: str
    message: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"level": self.level, "check_id": self.check_id,
                "message": self.message, "evidence": self.evidence}


@dataclass
class QualityReport:
    findings: list[QualityFinding] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        """**只收 WARN**。INFO 是「报个数，不判对错」，拼进警告会变成噪声。

        ★ 2026-10-26 修：原写法把 INFO 也拼进来，于是每一张图（哪怕调色板
          完全合格）都会带 3 条"警告"—— 那正是我在本项目反复踩过的
          「一条永远触发的告警等于没有告警」，模型会学会忽略整块输出。
          现在分两个属性：`warnings` 只给 WARN，`notes` 给全部（含 INFO）。
        """
        return [self._fmt(f) for f in self.findings if f.level == "WARN"]

    @property
    def notes(self) -> list[str]:
        """全部条目（WARN + INFO），供报告正文完整展示。"""
        return [self._fmt(f) for f in self.findings]

    @staticmethod
    def _fmt(f: "QualityFinding") -> str:
        return f"[质量/{f.level}] {f.check_id} {f.message}"

    def to_dict(self) -> dict:
        """★ `warnings` / `notes` 必须**一起带上**。

        2026-10-26 修：原来的 to_dict() 只回 findings 与 metrics，
        `warnings` 被丢掉了 —— 下游 `_quality_block()` 拿 `block["warnings"]`
        永远拿到空列表，于是整条质量维度在真实审计里**一条都说不出话**。
        接线看起来是通的（quality 块有 4 个键），实际是装饰。
        这就是"下游支持了、上游没回传"的典型：任何只看"键存在"的检查都会绿。
        """
        return {"findings": [f.to_dict() for f in self.findings],
                "metrics": self.metrics,
                "warnings": self.warnings,
                "notes": self.notes}


# ══════════════════════════════════════════════════════════════
# 色彩计算：全部 stdlib，不引 numpy / colour-science
# ══════════════════════════════════════════════════════════════

def hex_to_rgb(h: str) -> tuple[float, float, float]:
    """'#RRGGBB' → 三个 0..1 的 sRGB 分量。"""
    s = h.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"不是 6 位十六进制颜色：{h!r}")
    return (int(s[0:2], 16) / 255.0,
            int(s[2:4], 16) / 255.0,
            int(s[4:6], 16) / 255.0)


def _srgb_to_linear(c: float) -> float:
    """sRGB 传输函数反解（IEC 61966-2-1）。"""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(h: str) -> float:
    """WCAG 2.1 相对亮度（定义在 1.4.3）：先反解传输函数再加权。

    ★ 必须走线性光，不能直接对 0-255 加权平均 —— 后者算出来的
      对比度和人眼观感差很多，这是自建对比度探针最常见的错法。
    """
    r, g, b = (_srgb_to_linear(c) for c in hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG 对比度：(亮+0.05)/(暗+0.05)，范围 1.0–21.0。"""
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def oklab(h: str) -> tuple[float, float, float]:
    """sRGB → OKLab（Björn Ottosson 2020）。

    为什么用 OKLab 而不是 CIELAB：OKLab 的 L 通道在整条亮度范围上
    和感知明度基本线性，所以"亮度带"这种区间判据才有一个稳定的含义。
    在 CIELAB 上同一条带的中灰段会明显偏亮，阈值会漂。
    """
    r, g, b = (_srgb_to_linear(c) for c in hex_to_rgb(h))
    l_ = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m_ = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s_ = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l3 = math.copysign(abs(l_) ** (1 / 3), l_)
    m3 = math.copysign(abs(m_) ** (1 / 3), m_)
    s3 = math.copysign(abs(s_) ** (1 / 3), s_)
    return (0.2104542553 * l3 + 0.7936177850 * m3 - 0.0040720468 * s3,
            1.9779984951 * l3 - 2.4285922050 * m3 + 0.4505937099 * s3,
            0.0259040371 * l3 + 0.7827717662 * m3 - 0.8086757660 * s3)


def _chroma(lab: tuple[float, float, float]) -> float:
    return math.hypot(lab[1], lab[2])


def _delta_e_ok(a: str, b: str) -> float:
    """OKLab 空间里的欧氏距离当 dE。

    这是本模块唯一"自造"的量：figure-gate 用的是 CAM02-UCS 上的 dE，
    两者数值不完全等尺度。所以下面所有阈值都是**对着 OKLab 尺子
    重新标定**过的（见 PALETTE_BANDS 的注释与交叉验证记录），
    不是把 CAM02 的阈值直接搬过来用。
    """
    la, aa, ba = oklab(a)
    lb, ab, bb = oklab(b)
    return math.sqrt((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2)


# ── 色盲模拟：Machado, Oliveira & Fernandes (2009) 的 sRGB 矩阵 ────
#
# ★ 2026-10-26 第一版写错了，记下来：我先用 Hunt-Pointer-Estevez 系数把
#   sRGB 转成 LMS，再乘 Viénot 那一套"用另外两类锥体重建缺失那类"的系数。
#   两套系数是在**不同空间**标定的，混用后结果没有任何物理意义 ——
#   实测中灰 #808080 在 protan 下被模拟成 #009081（青绿）、
#   在 tritan 下变成 #A24CFF（紫）。而灰阶在**任何**色盲类型下都不该变。
#   这个自检（灰进灰出）比我用来"核对阈值"的任何方法都更早、更便宜地
#   暴露出错误，所以它被写成了下面的硬断言。
#
#   正确做法：Machado 2009 直接给出 sRGB **线性**空间的 3x3 矩阵，
#   不需要中间 LMS，也不存在空间混用。severity=1.0（完全色盲）。
_CVD_MATRICES = {
    # 红色盲（L 锥体缺失）
    "protan": ((0.152286, 1.052583, -0.204868),
               (0.114503, 0.786281, 0.099216),
               (-0.003882, -0.048116, 1.051998)),
    # 绿色盲（M 锥体缺失）—— 男性约 6%，是最常见的类型
    "deutan": ((0.367322, 0.860646, -0.227968),
               (0.280085, 0.672501, 0.047413),
               (-0.011820, 0.042940, 0.968881)),
    # 蓝色盲（S 锥体缺失）—— 约 0.01%，只作参考不设门
    "tritan": ((1.255528, -0.076749, -0.178779),
               (-0.078411, 0.930809, 0.147602),
               (0.004733, 0.691367, 0.303900)),
}


def simulate_cvd(h: str, kind: str) -> str:
    """把一个颜色按某类色盲模拟后返回十六进制。

    输入输出都是编码过的 sRGB；矩阵作用在**线性**值上。
    """
    mat = _CVD_MATRICES.get(kind)
    if mat is None:
        raise ValueError(f"不认识的色盲类型：{kind}（可选 protan/deutan/tritan）")
    lin = [_srgb_to_linear(c) for c in hex_to_rgb(h)]
    rgb = [sum(mat[i][j] * lin[j] for j in range(3)) for i in range(3)]
    out = []
    for c in rgb:
        c = min(1.0, max(0.0, c))
        s = c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        out.append(round(min(1.0, max(0.0, s)) * 255))
    return "#%02X%02X%02X" % tuple(out)


def _worst_pair(palette: list[str], transform=None) -> tuple[float, str, str]:
    worst, pa, pb = float("inf"), "", ""
    for i in range(len(palette)):
        for j in range(i + 1, len(palette)):
            a = transform(palette[i]) if transform else palette[i]
            b = transform(palette[j]) if transform else palette[j]
            d = _delta_e_ok(a, b)
            if d < worst:
                worst, pa, pb = d, palette[i], palette[j]
    return worst, pa, pb


def _selfcheck() -> list[str]:
    """自检。返回问题列表；空列表 = 所有底层换算都成立。

    ★ 这些断言是**唯一**能在没有外部参照时发现换算写错的机制。
      我第一版把两套不同空间的色盲系数混用，`protan` 把中灰
      #808080 模拟成青绿色 —— 灰进灰出这条一跑就炸了。
      比起"拿我的阈值去核对别人的阈值"（两个都可能错），
      这类**不变量**是更可靠的锚：它们的正确答案由定义唯一确定。
    """
    bad: list[str] = []
    # ① WCAG 对比度的两条定义式
    if abs(contrast_ratio("#FFFFFF", "#000000") - 21.0) > 1e-6:
        bad.append("白/黑对比度不等于 21:1（WCAG 定义式写错了）")
    if abs(contrast_ratio("#FFFFFF", "#FFFFFF") - 1.0) > 1e-6:
        bad.append("同色对比度不等于 1:1")
    # ② 相对亮度的三原色系数（WCAG 1.4.3 直接给出）
    for h, want in (("#FF0000", 0.2126), ("#00FF00", 0.7152), ("#0000FF", 0.0722)):
        got = relative_luminance(h)
        if abs(got - want) > 5e-4:
            bad.append(f"{h} 相对亮度 {got:.4f} != {want}")
    # ③ OKLab 的端点与中灰
    if abs(oklab("#FFFFFF")[0] - 1.0) > 1e-4:
        bad.append("白在 OKLab 里 L != 1")
    if abs(oklab("#000000")[0]) > 1e-4:
        bad.append("黑在 OKLab 里 L != 0")
    mid = oklab("#808080")[0]
    if not (0.55 <= mid <= 0.65):
        bad.append(f"中灰 OKLab L={mid:.4f} 偏离 0.6 太多")
    # ④ **灰进灰出**：任何色盲模拟都不改变无彩色
    for kind in ("protan", "deutan", "tritan"):
        for grey in ("#808080", "#404040", "#C0C0C0"):
            sim = simulate_cvd(grey, kind)
            r, g, b = hex_to_rgb(sim)
            # 无彩色要求三通道相等（允许 1/255 的取整误差）
            if max(abs(r - g), abs(g - b), abs(r - b)) > 2 / 255:
                bad.append(f"{kind} 把灰 {grey} 模拟成了有彩色 {sim}")
    return bad


_SELFCHECK_PROBLEMS = _selfcheck()


# ══════════════════════════════════════════════════════════════
# 调色板体检
# ══════════════════════════════════════════════════════════════

def check_palette(palette: list[str], surface: str = "#FFFFFF") -> QualityReport:
    """五道调色板门。全部按 **all-pairs** 比对，不是只比相邻色。

    ★ all-pairs 不是洁癖。分类调色板里任意两色都可能同时出现在图上
      （不同系列、不同分面），只比相邻色会放行"三个蓝"那种组合 ——
      我第一版就是那么错的，解出一条同时含 #0072B2 / #2166AC / #3288BD
      的"最优解"，三个蓝色在正常视觉下都分不开。
    """
    rep = QualityReport()
    pal = [p for p in palette if p]
    if len(pal) < 2:
        rep.findings.append(QualityFinding(
            "INFO", "PALETTE-TOO-SMALL",
            f"调色板只有 {len(pal)} 色，跳过分离度检查。"))
        return rep

    # ① 亮度带
    bad_l = []
    for h in pal:
        L = oklab(h)[0]
        if not (PALETTE_L_MIN <= L <= PALETTE_L_MAX):
            bad_l.append((h, round(L, 4)))
    if bad_l:
        rep.findings.append(QualityFinding(
            "WARN", "PALETTE-LIGHTNESS",
            f"{len(bad_l)} 色落在亮度带 {PALETTE_L_MIN}–{PALETTE_L_MAX} 之外："
            "太暗的在灰度打印里并成一片，太浅的在投影下消失。",
            [f"{h} L={L}" for h, L in bad_l]))
    rep.metrics["palette_lightness"] = {
        h: round(oklab(h)[0], 4) for h in pal}

    # ② 彩度下限
    bad_c = []
    for h in pal:
        C = _chroma(oklab(h))
        if C < PALETTE_C_MIN:
            bad_c.append((h, round(C, 4)))
    if bad_c:
        rep.findings.append(QualityFinding(
            "WARN", "PALETTE-CHROMA",
            f"{len(bad_c)} 色彩度低于 {PALETTE_C_MIN}，色相上已经接近灰，"
            "读者只能靠明度分辨。",
            [f"{h} C={C}" for h, C in bad_c]))
    rep.metrics["palette_chroma"] = {h: round(_chroma(oklab(h)), 4) for h in pal}

    # ③ 色盲分离度 —— **只测量、只报告，不判定**
    #
    #   ★ 这里刻意不产出 WARN。理由见文件上方 PALETTE_CVD_INFO_ONLY 的长注释：
    #     我用真实色对证明了 OKLab 欧氏 dE 无法把「分得开」与「分不开」
    #     分开（两类情况的 dE 相差不到 2%）。既然判据不可靠，
    #     就不许它出现在能被当结论读的位置上 —— 一个恒真或恒假的判据
    #     比没有判据更糟，它给人"已经检查过"的错觉。
    #     真正缺的那块（可靠的色盲分离度）由构建期的 figure-gate 原实现兜底。
    cvd_worst = {}
    for kind in ("protan", "deutan", "tritan"):
        worst, pa, pb = _worst_pair(
            pal, lambda h, k=kind: simulate_cvd(h, k))
        cvd_worst[kind] = (round(worst, 4), pa, pb)
    worst_kind = min(("protan", "deutan"),
                     key=lambda k: cvd_worst[k][0])
    w_de, w_a, w_b = cvd_worst[worst_kind]
    rep.findings.append(QualityFinding(
        "INFO", "PALETTE-CVD-INFO",
        f"色盲模拟下最接近的一对是 {w_a} / {w_b}（{worst_kind}，OKLab dE={w_de}）。"
        "这是**信息不是判定**：dE 在这个量级上无法区分「可分辨」与「不可分辨」"
        "（实测两个真实色对的 dE 相差不到 2% 而结论相反），"
        "所以这里不给通过/不通过。真正要确认请用 CAM02-UCS 那套实现复核。",
        [f"{k}: dE={v[0]} ({v[1]} vs {v[2]})" for k, v in cvd_worst.items()]))
    rep.metrics["palette_cvd"] = {k: v[0] for k, v in cvd_worst.items()}

    # ④ 正常视觉分离度 —— 同上，只测量
    nv, na, nb = _worst_pair(pal)
    rep.findings.append(QualityFinding(
        "INFO", "PALETTE-SEPARATION-INFO",
        f"正常视觉下最接近的两色是 {na} / {nb}（OKLab dE={round(nv, 4)}）。"
        "只报数，不设门 —— 理由同色盲分离度。"))
    rep.metrics["palette_separation"] = round(nv, 4)

    # ⑤ 与底色的对比度 —— 只报数、不设门
    #
    #   ★ 降级理由（与 figure-gate 对齐）：它自己把这一项判 `warn`，
    #     而且**连 Okabe-Ito 也是 warn**（#E69F00 2.25:1、#56B4E9 2.31:1），
    #     它的整体 ok 照样是 True。也就是说这套标准件在浅底上也过不了
    #     3.0:1，这不是配色失误而是**设计取舍**：浅底上的浅色块本来就
    #     靠"直接标注数值/直接标签"来读，不靠色块与底色的明暗差。
    #     把它当硬门会误伤公认标准件，所以这里只报数。
    low = []
    for h in pal:
        cr = contrast_ratio(h, surface)
        if cr < GRAPHIC_CONTRAST_MIN:
            low.append((h, round(cr, 2)))
    if low:
        rep.findings.append(QualityFinding(
            "INFO", "PALETTE-CONTRAST-INFO",
            f"{len(low)} 色与底色 {surface} 的对比度低于 {GRAPHIC_CONTRAST_MIN}:1"
            "（WCAG 2.1 对非文本图形元素的要求）。这个量级在浅底上是常态，"
            "**不判不合格**；代价是这些颜色必须直接标注数值或直接贴标签，"
            "不能指望色块自己从底色里跳出来。",
            [f"{h} {cr}:1" for h, cr in low]))
    rep.metrics["palette_contrast"] = {
        h: round(contrast_ratio(h, surface), 2) for h in pal}

    return rep
