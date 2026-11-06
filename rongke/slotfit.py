# -*- coding: utf-8 -*-
"""槽位容量模型 —— **全链唯一一份**。

一只文字框能装多少字 = 宽度能排几个"字宽" × 高度能排几行。
就这一句，但**"字宽"和"能排几行"以前都猜错了**，且错的方向是"判得比实际小"。

## 谁在用（三处，**都必须从这里取**）

| 用它的地方 | 用途 |
|---|---|
| `deck_template_extract.py` | 抽取时算随库的容量表（`slots.json`） |
| `rongke/deck.py::slot_capacity()` | 只读工具 / 开工清单 / 测试 |
| `deck_from_template` 的填充子脚本 | 真正落笔前再算一遍（含缩字号那一支） |

★ 2026-09-22 之前这里**有三份互相抄的公式**（抽取器一份、`deck.py` 一份、
  填充子脚本字符串里一份）。三份抄着抄着就会漂，而"表里说能装、执行体说装不下"
  正是本项目最贵的那类失败。⇒ 收成这一个模块，其余全部 `import` 它。

## 五条"修出来的事实"（每条都有量测，2026-09-22）

① **一行不是"几个字"，是"几个 em"。**
   一个汉字 ≈ 1em，一个英文字母/数字只占 ≈ 0.56em。旧模型整段按 1em/字算
   ⇒ 英文栏位被系统性判小。**实测实测模板 703 槽里 156 槽（22%）连模板
   自己的原文都判"装不下"**（`Contents` / `Research Background` / `2018.6` /
   `朱廷恒老师`）；浙工大模板 177 槽里 10 槽（5.6%）。
   一条会在**模板自己**身上报错的判据 ⇒ 设计定律 10（假告警教人无视整份报告）。

② **内边距是读出来的，不是假设的**：`bodyPr/@lIns @rIns @tIns @bIns`。
   PowerPoint 默认 0.1/0.1/0.05/0.05in，但**形状可以覆盖**（大量图形写
   `lIns=0 rIns=0`）⇒ 对这类框，假设 0.2in 会白扣 0.2in 可用宽度。

③ **`wrap="none"` 的框不换行** ⇒ 行数不由框高决定。

④ **作者原文是存在性证明。**
   模板作者在这只框里放下了 N 个 em、且渲得出 ⇒ "容量 < N" 就是错的。
   所以行数取 `max(几何算出的, 原文需要的)`。
   ★ 这条让"原文自己装不下"**由构造归零**（不是靠调系数逼近）——
     `deck_template_extract.py` 抽完当场断言，`template_test` §3b 常驻守着。

⑤ **浮点容差**：`1.45in / 18pt` 本该正好 5 字，浮点给出 `4.9999…`，
   `int()` 一取就成 4。⇒ 边界比较一律带 `EPS`。

## 一处刻意的保守

`char_em()` 的拉丁系数是**字体平均advance**，不是逐字体量出来的：
Arial/微软雅黑 的小写 ≈ 0.556em、大写 ≈ 0.667em、数字 0.556em、空格 0.278em。
⇒ 它**不追求逐字体精确**，只求"别把装得下的判成装不下"（④ 已兜住上界）。
真伪由渲染对照兜底（`deck_preview.py`）：判据不越界，也证明不了好看。
"""

from __future__ import annotations

import math
import unicodedata

# ── 长度单位 ──
#: 1 英寸的 EMU 数。**内边距一律以 EMU 传**（文件里就是这么存的整数）——
#: 换成英寸再传会引入浮点漂移，而"表里的数 == 执行体算的数"要求逐位相等。
EMU_IN = 914400

#: PowerPoint 的默认内边距（形状不写 `bodyPr` 那些属性时用这些），单位 **EMU**：
#: 左/右 0.1in、上/下 0.05in。
INSET_DEF_EMU = (91440, 91440, 45720, 45720)

#: `bodyPr/@vert` 里**真的是竖排**的那几个值。
#:
#: ⚠️ **必须排除 `"horz"`** —— 它是"**显式**横排"，合法且常见（实测模板里
#:    大量形状写着 `vert="horz"`）。把 `vert` 直接当布尔用 ⇒ 行列互换 ⇒
#:    实测 `正文-左长文右两点` 的页标题格容量从 **30 字被抬到 47 字**
#:    （表里承诺的比装得下的多，**危险方向**），而两条自检**全绿**：
#:    ① 表与执行体一致（一起错），② 原文装得下（被抬高的上限反而更容易满足）。
#:    ⇒ 教训：自检只证"一致"，证不了"对"。所以下面还有第三条 —— 上界。
VERT_MODES = ("vert", "vert270", "eaVert", "mongolianVert",
              "wordArtVert", "wordArtVertRtl")

#: 行高 ≈ 1.2em（与旧公式一致；改它会同时动两份槽位表，别顺手改）
LINE_SPACING = 1.2

#: 每行容量下限（em）。取 0 会让任何字都判"溢出"，取 0.05 让"极窄的框"仍能放 1 个字。
EM_FLOOR = 0.05

#: 浮点边界容差（见模块头 ⑤）。
EPS = 1e-9

#: 给模型看的换算系数：同一个框里**英文字符数**约为**汉字数**的多少倍。
#: 由 `char_em` 的拉丁系数推出（1 / 0.556 ≈ 1.8），不是拍的。
ASCII_RATIO = 1.8


def char_em(ch: str) -> float:
    """一个字符占几个 em。**全角 1.0；英文数字 ≈ 0.56。**"""
    o = ord(ch)
    if (unicodedata.east_asian_width(ch) in ("W", "F")
            or 0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
            or 0xAC00 <= o <= 0xD7A3 or 0xFE30 <= o <= 0xFE6F):
        return 1.0
    if ch == " ":
        return 0.278
    if ch.isdigit():
        return 0.556
    if "A" <= ch <= "Z":
        return 0.667
    if "a" <= ch <= "z":
        return 0.556
    if ch in "/|:;.,'\"!()[]-–—~`\\":
        return 0.3
    return 0.6


def em_width(text: str) -> float:
    """一段文字占几个 em（不换行时的一行宽度）。"""
    return sum(char_em(c) for c in (text or ""))


def as_paras(text) -> list[str]:
    """把"要填进去的内容"归成段落列表（与 `deck_from_template` 的约定一致）。"""
    if text is None:
        return []
    if isinstance(text, str):
        return text.split("\n")
    return [str(x) for x in text]


def needed_lines(paras, cpl: float | None) -> int:
    """这些段落要几行才排得下。`cpl` = 每行 em 容量（**浮点**，见 ⑤）。"""
    if not paras or not cpl or cpl <= 0:
        return 0
    return sum(max(1, math.ceil((em_width(p) - EPS) / cpl)) for p in paras)


def slot_capacity(w_in: float, h_in: float, pt: float | None, *,
                  ins_emu: tuple = INSET_DEF_EMU, wrap: str = "square",
                  vert: str = "", min_lines: int = 0
                  ) -> tuple[float | None, int | None, float | None]:
    """(每行 em 容量, 行数, 总 em 容量)。

    `pt` 为空 ⇒ 三个都 `None`（**"读不到"不是"不符合"** —— 缺字号是查错了层，
    真身在 `p:defaultTextStyle`；连那儿都没有才叫真的读不到）。

    `ins_emu` = (左, 右, 上, 下) 内边距，**EMU 整数**（见 ②）。

    `min_lines` = **原文需要的行数**（存在性证明，见模块头 ④）。
    抽取器用 `needed_lines(原文段落, cpl)` 算出来存进表里；填充时原样传回来。

    `vert` = `bodyPr/@vert`（`"eaVert"` 等）。竖排时**长和宽互换**：
      「一行」沿高度走，「几行」横着排 —— 不换这一下，竖排框的容量会算成
      完全无关的数（实测模板 p48 的「关键词」就是竖排）。

    ⚠️ **不许再取整 `cpl`**。旧版 `int(...)` 把 `1.07em` 的容量砍成 `1em`，
      于是"能放 1.05em 的字"被判装不下（`01` / `Contents` 都栽在这上面）。
    """
    if not pt or pt <= 0:
        return None, None, None
    il, ir, it, ib = [float(x) / EMU_IN for x in (ins_emu or INSET_DEF_EMU)]
    if vert in VERT_MODES:              # 真竖排 ⇒ 行列互换（内边距也跟着换）
        w_in, h_in = h_in, w_in
        il, ir, it, ib = it, ib, il, ir
    # ⚠️ `+ EPS` 不是装饰：`(1.45 - 0.2) * 72 / 18` 用十进制是 5.0，
    #    浮点给 4.999999999999999 ⇒ `int(cap)` 一取就成 4
    #    （和本模块头 ⑤ 同一条病，只是换了个位置犯）。
    cpl = max(EM_FLOOR, (w_in - il - ir) * 72.0 / pt + EPS)
    mx_geo = max(1, math.floor((h_in - it - ib) * 72.0
                               / (pt * LINE_SPACING) + EPS))
    if wrap == "none":
        # 不换行 ⇒ 框高说了不算，作者写了几个"行"（= 几个段落）就几个
        mx = max(1, int(min_lines))
    else:
        mx = max(mx_geo, int(min_lines))
    return cpl, mx, cpl * mx


def fits(text, w_in: float, h_in: float, pt: float | None, **kw) -> bool:
    """这段文字放得下吗？（判据的判定面；测试与自检用同一条）"""
    paras = as_paras(text)
    cpl, mx, cap = slot_capacity(w_in, h_in, pt, **kw)
    if cpl is None:
        return True                     # 容量未知 ⇒ 拦不住，也不许假装拦得住
    return needed_lines(paras, cpl) <= mx and em_width("".join(paras)) <= cap + EPS


def cap_chars(cap_em: float | None) -> int | None:
    """给模型看的"几个汉字"（= em 数取整）。"""
    return None if cap_em is None else int(cap_em)
