# -*- coding: utf-8 -*-
"""机制图原生 SVG 后端（零第三方依赖）。

## 边界：AI 声明规格，工具算几何

模型给的是 `{nodes, edges, groups}` —— **谁激活谁、谁抑制谁、谁在哪个区室**。
坐标、换行、层序、连线走向、箭头三角形、区室框全由本模块算。模型不写一个
SVG 字节，因此「字画出框外」「两套坐标系」「箭头朝向反了」这些形态在结构上
就没有入口。算完立刻读回，交 `svgq` 七道门量。

## 布局：自己实现的分层法（Sugiyama  lite），不租框架

四步：拆环 → 最长路径分层 → 跨层边插虚节点 → 重心法排序减少交叉。
为什么不用现成的：grandalf 是 GPLv2/EPLv1 双授权（与本仓许可不兼容），
networkx 的 `multipartite_layout` 在函数体里 import numpy 且不减少交叉。
本模块只用标准库，与本仓「零依赖出图」那条路线一致。

★ **虚节点是质量的一部分，不是装饰**：跨多层的边如果不插虚节点，就会一条
  直线穿过中间层的节点框 —— 而现有七道门量的是文字，**量不到线穿框**。
  插了虚节点之后，每一段线只走在层与层之间的空档里，空档里没有节点，
  「线穿框」在结构上不可能发生。这跟把图例挪出绘图区是同一种保证方式：
  不靠门去抓，靠布局让它压根不发生。

## 物理量纲

节点标签 10px、边标签 8.5px。画布宽 `w = max(自然宽, width_mm × 96/25.4)`，
几何清单里 `mm_per_px = width_mm / w` —— 声明宽度摆进版面后字到底有几 pt，
由字号门算。图画不下时**不缩字**，而是把画布撑宽并如实报：此时按声明宽度
缩放会把字压到 5pt 以下，门会判红，模型该做的是缩短标签或改声明宽度。
"""
from __future__ import annotations

import json
import math
import re
from collections import deque
from pathlib import Path
from typing import Any

from .deck import text_advance_em
from .svgplot import (FONT_STACK, _AXIS, _Canvas, _esc, _INK, _MM_TO_PX,
                      _PALETTE, _PAPER, inspect_svg)

#: 节点形状口径的**单源**：校验、几何、绘制、工具描述都读这一份。
#: 2026-11-11 P0 扩充的 6 种取自 Mermaid flowchart 形状目录（MIT），
#: 只用名称与形状语义这些事实，几何全部自己用标准库算，没有搬一行上游代码。
NODE_KINDS = (
    "process", "decision", "terminator", "entity",
    "database", "circle", "hexagon", "subroutine", "stadium", "cylinder",
)
NODE_SHAPE_DESC = {
    "process": "圆角矩形（步骤）",
    "decision": "菱形（判断）",
    "terminator": "胶囊（起止）",
    "entity": "直角矩形（实体）",
    "database": "竖立圆柱（数据集 / 数据库）",
    "circle": "正圆（分子 / 实体）",
    "hexagon": "六边形（预处理）",
    "subroutine": "双竖线矩形（子流程）",
    "stadium": "跑道形双框（阶段）",
    "cylinder": "横放圆柱（样本 / 存储）",
}
EDGE_KINDS = ("arrow", "inhibit", "dashed", "phospho", "bind", "cleave",
              "secrete", "degrade")
EDGE_KIND_DESC = {
    "arrow": "激活 / 流向（实心小三角）",
    "inhibit": "抑制（T 形横杠）",
    "dashed": "间接 / 假设（虚线 + 三角）",
    "phospho": "磷酸化 / 修饰（三角 + 实心圆点）",
    "bind": "结合 / 相互作用（双向三角）",
    "cleave": "剪切 / 加工（三角 + 双横杠）",
    "secrete": "分泌 / 释放（三角 + 囊泡点）",
    "degrade": "降解 / 清除（叉号）",
}
DIRECTIONS = ("LR", "TB")

NODE_SIZE = 10.0
EDGE_SIZE = 8.5
GROUP_SIZE = 8.5
TITLE_SIZE = 11.0
LEGEND_SIZE = 9.0        # 图例字号：比边标签大一档，读图例不该费劲
LEGEND_H = 24.0          # 图例一行的高度（图元/色块与文字共用这一条基线带）
LEGEND_GAP = 18.0        # 图例与内容之间的空档

#: 描边与箭头规格（2026-11-14 视觉语法重做）：期刊插图是**细线 + 小箭头**，
#: 不是网页流程图那种 1.6px 粗框配 9×8 大三角。整图观感一半由这两个数决定。
_NODE_STROKE_W = 1.2
_EDGE_STROKE_W = 1.1

_PAD_X = 12.0            # 节点左右内边距
_PAD_Y = 8.0             # 节点上下内边距
#: 行高 / 字号。1.35 时 10px 字的行距 13.5px 小于浏览器实测字盒 14.2px，
#: 折行后两行会**真的**相压 0.5px（2026-11-14 实测抓到的 WARN）。1.45 留出余量。
_LINE_H = 1.45
_NODE_GAP = 20.0         # 同层节点间距
#: 层间空档下限。要同时装下：源侧区室底边留白、靶侧区室标题带、边标签本身。
#: 早先取 56 —— 竖排通路图上每一段空档都多出十几像素，整图被拉成一条过长的带。
#: 48 仍能装下 8.5px 的边标签与区室标题带。
_MIN_LAYER_GAP = 48.0
_EDGE_PAD = 8.0          # 边标签沿 flow 轴的左右留白
#: 边标签沿 cross 轴让位时额外留的空隙。
_LABEL_PAD = 4.0
#: cross 轴字盒宽的**安全系数**。0.55/1.0 的 em 口径量的是前进宽度，浏览器
#: getBBox 还含侧 bearings，实测每字约多 0.03~0.04em —— 误差随字数线性涨，
#: 固定像素留白压不住长标签。乘一个略大于实测误差的系数才与字数无关地安全。
_WIDTH_SAFETY = 1.06


def _ascii_share(text: str) -> float:
    """非空白字符里 ASCII 占多少。"""
    t = [c for c in str(text) if not c.isspace()]
    if not t:
        return 0.0
    return sum(1 for c in t if c.isascii()) / len(t)


def _label_em(text: str) -> float:
    """边标签的**字盒**宽（em）：前进宽度 + 两侧 bearings。

    `text_advance_em` 量的是前进宽度；浏览器 getBBox 把 bearings 也算进去。
    实测连续大写西文「SMAD7」的 盒/前进宽度 = **1.32**，中文只有 ~1.0 ——
    固定的 1.06 对中文够、对西文差一截，标签就会让到相邻连线上（2026-11-14
    跑 D3 负控时量出来的）。按 ASCII 占比加权补回来：纯西文 ×1.25，
    叠加 1.06 后正好覆盖实测的 1.32。
    """
    return text_advance_em(text) * (1.0 + 0.25 * _ascii_share(text))


_MARGIN = 18.0
_MAX_NODE_W = 170.0      # 超过就换行，不让节点无限宽
#: 箭头：实心小三角。9×4 在 1.6px 粗线上还算协调，细线配大箭头就变成「网页
#: 流程图」。7×3.2 与 1.1px 线宽成对。
_ARROW_LEN = 7.0
_ARROW_HALF = 3.2
_GROUP_PAD = 12.0
#: 区室标题带占掉的 flow 厚度（一行组名 + 上留白）。区室框在 run 的 flow 起点
#: 要让出这么多，边标签就不能进这一段。
_GROUP_HEAD = GROUP_SIZE * 1.5 + _GROUP_PAD

# ══════════════════════════════════════════════════════════════════
# 语义配色（2026-11-14）
# ══════════════════════════════════════════════════════════════════
# 以前按图元序号轮 `_PALETTE`：受体红、靶基因橙、SMAD7 蓝 —— 颜色与语义无关，
# 读图的人会以为同色 = 同一条通路。期刊插图的口径是**颜色表示「在哪」**：
# 同一区室同色、跨区室靠色区分、没有区室的回落墨色；判断框单独一抹砖红。
# 判据侧的对比度门量的是**文字与所在底色**，描边配色不参与，故本条不改门。
_GROUP_TINTS = ("#2E5A63", "#4C6B52", "#7A5A8E", "#3E6C8C", "#B0813E")
_NODE_INK = "#3C4A52"
_DECISION_TINT = "#B23A2E"


def _soften(hex_col: str, f: float = 0.9) -> str:
    """把色相淡化成底色：区室靠**色相**区分，不靠饱和度抢节点。

    期刊的区室是浅色背景块，节点是白底深描边；虚线框紧贴节点那种画法
    会让「框」变成第二个节点（2026-11-14 用户原话：「谁家科研绘图长这样」）。
    """
    h = hex_col.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    f = max(0.0, min(0.96, f))
    return "#%02X%02X%02X" % (round(r + (255 - r) * f),
                             round(g + (255 - g) * f),
                             round(b + (255 - b) * f))

# ══════════════════════════════════════════════════════════════════
# 形状尺度上限（2026-11-14）
# ══════════════════════════════════════════════════════════════════
# 圆的内接框只有直径的 1/√2、六边形只有宽度的 0.56。按**整行**字宽直接反解
# 外形，一个长标签就能把单个节点撑到 180px 以上（2026-11-14 真产物：两个正圆
# 直径 182px，整图 1234px，摆进 180mm 版面字号掉到 4pt）。所以先按上限收窄
# 折行、再反解外形：宁可标签两行，也不让一个节点吃掉半张图。
#: 期刊图里「分子」与「步骤」是同一量级：方框高 30、圆直径 100 已经是上限。
#: 超过它，读者第一眼看到的是两个大圆，而不是通路。
_CIRCLE_MAX_D = 104.0
_HEX_MAX_W = 150.0
#: 菱形（判断框）的内接框只有外框一半宽，字号又必须 ≥ 7.5pt ⇒ 外框必须给足。
#: 190 会让「是否发生胞外释放？」这类 9 字问句折成两行、还可能把「胞外释放」
#: 切两半；208 装得下一行 9 字，判断框在期刊里本来也比方框宽。
_DEC_MAX_W = 208.0
#: 折行候选宽度（px）。从小到大试，第一个「反解出的外形 ≤ 上限」的胜出；
#: 全都超限就用最后一档（此时仍按真实尺寸画，绝不偷偷把字缩到装不下）。
_TEXT_LIMITS = (170.0, 140.0, 120.0, 100.0, 84.0, 70.0, 58.0)

#: 边标签避让的候选档位（0 = 原位，其余为 cross 轴的 ±k 档）。
#: 提取成常量是为了让判据能「关掉避让」做负控：否则让位与避让两道防线
#: 会互相兜底，碰撞门永远不红，判据失掉分辨率。
_AVOID_STEPS = (0, 1, -1, 2, -2, 3, -3)


class DiagramError(ValueError):
    """规格不合法 / 本后端画不了。**说清楚为什么**，不静默画个别的东西。"""


# ══════════════════════════════════════════════════════════════════
# 规格校验
# ══════════════════════════════════════════════════════════════════

def _validate(spec: dict) -> tuple[list[dict], list[dict], list[dict], str, str, float]:
    if not isinstance(spec, dict):
        raise DiagramError("规格必须是一个对象（含 nodes / edges）。")
    raw_nodes = spec.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise DiagramError("nodes 不能为空：机制图至少要有一个节点。")
    nodes: list[dict] = []
    seen: set[str] = set()
    for i, n in enumerate(raw_nodes):
        if not isinstance(n, dict):
            raise DiagramError(f"nodes[{i}] 不是对象。")
        nid = str(n.get("id") or "").strip()
        if not nid:
            raise DiagramError(f"nodes[{i}] 缺 id。边要靠 id 指过来，不能没有。")
        if nid in seen:
            raise DiagramError(f"节点 id 重复：`{nid}`。重复会让边指到错的节点上。")
        seen.add(nid)
        kind = str(n.get("kind") or "process")
        if kind not in NODE_KINDS:
            raise DiagramError(
                f"节点 `{nid}` 的 kind=`{kind}` 不支持（支持：{'、'.join(NODE_KINDS)}）。"
                "本后端不会把它假装画成别的形状。")
        nodes.append({"id": nid, "label": str(n["label"]) if "label" in n else nid,
                      "kind": kind, "group": str(n.get("group") or "").strip(),
                      "icon": str(n.get("icon") or "").strip()})
        if nodes[-1]["icon"]:
            from rongke import svgbio as _bio
            if nodes[-1]["icon"] not in _bio.ICONS:
                raise DiagramError(
                    f"节点 `{nid}` 的 icon=`{nodes[-1]['icon']}` 不在图元库里"
                    f"（可选：{'、'.join(sorted(_bio.ICONS))}）。"
                    "不写 icon 就走流程图形状，写错名字不该静默变成别的图形。")
    edges: list[dict] = []
    for i, e in enumerate(spec.get("edges") or []):
        if not isinstance(e, dict):
            raise DiagramError(f"edges[{i}] 不是对象。")
        f, t = str(e.get("from") or "").strip(), str(e.get("to") or "").strip()
        if f not in seen or t not in seen:
            raise DiagramError(
                f"edges[{i}] 是 `{f}`→`{t}`，但有不存在的节点。"
                f"现有 id：{'、'.join(sorted(seen))}")
        if f == t:
            raise DiagramError(
                f"edges[{i}] 是自环（`{f}`→`{f}`）。本后端不画自环 —— "
                "加一个中间节点把回路表达出来，别让它静默消失。")
        kind = str(e.get("kind") or "arrow")
        if kind not in EDGE_KINDS:
            raise DiagramError(
                f"edges[{i}] 的 kind=`{kind}` 不支持（支持：{'、'.join(EDGE_KINDS)}）。")
        edges.append({"from": f, "to": t, "label": str(e.get("label") or ""),
                      "kind": kind})
    groups: list[dict] = []
    gseen: set[str] = set()
    for g in spec.get("groups") or []:
        gid = str((g or {}).get("id") if isinstance(g, dict) else g or "").strip()
        if gid and gid not in gseen:
            gseen.add(gid)
            groups.append({"id": gid,
                           "label": str(g.get("label", "")) if isinstance(g, dict)
                           else gid,
                           "kind": str((g or {}).get("kind") or "box")
                           if isinstance(g, dict) else "box"})
    # 节点上写了 group 但没在 groups 里声明的，自动补一个（label 就用 id）
    for n in nodes:
        if n["group"] and n["group"] not in gseen:
            gseen.add(n["group"])
            groups.append({"id": n["group"], "label": n["group"], "kind": "box"})
    direction = str(spec.get("direction") or "LR").upper()
    if direction not in DIRECTIONS:
        raise DiagramError(f"direction=`{direction}` 不支持（支持：LR、TB）。")
    title = str(spec.get("title") or "")
    # 不能用 `or 180.0`：那会把**显式的 0 / 负数**当成「没填」静默换成默认值，
    # 非法宽度就漏过去了。只有 None / 空串才算没填。
    raw_w = spec.get("width_mm")
    if raw_w is None or raw_w == "":
        width_mm = 180.0
    else:
        try:
            width_mm = float(raw_w)
        except (TypeError, ValueError):
            raise DiagramError(f"width_mm 不是数：{raw_w!r}")
    if width_mm <= 0:
        raise DiagramError("width_mm 必须为正。")
    return nodes, edges, groups, direction, title, width_mm


# ══════════════════════════════════════════════════════════════════
# 文本与节点尺寸
# ══════════════════════════════════════════════════════════════════

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokens(para: str) -> list[str]:
    """切成**可断的最小单位**：ASCII 词整体、中文连续字整体、其余逐字。

    按字硬断会把 `SMAD2/3` 拆成 `SMAD2/` + `3`、把「磷酸化」拆成「磷／酸化」——
    读图的人会把断口当成两个概念（2026-11-14 真产物里两处都出现了）。
    """
    toks: list[str] = []
    i, n = 0, len(para)
    while i < n:
        ch = para[i]
        if ch.isascii() and (ch.isalnum() or ch in "-_/.%"):
            j = i
            while j < n and para[j].isascii() and (
                    para[j].isalnum() or para[j] in "-_/.%"):
                j += 1
            toks.append(para[i:j])
            i = j
        elif _CJK.match(ch):
            j = i
            while j < n and _CJK.match(para[j]):
                j += 1
            toks.append(para[i:j])
            i = j
        else:
            toks.append(ch)
            i += 1
    return toks


def _em(s: str) -> float:
    return sum(text_advance_em(c) for c in s)


def _wrap_para(para: str, max_em: float) -> list[str]:
    toks = _tokens(para)
    lines: list[str] = []
    cur, em = "", 0.0
    for tk in toks:
        w = _em(tk)
        if cur and em + w > max_em:
            lines.append(cur.rstrip())
            cur, em = "", 0.0
            if tk == " ":
                continue
        if w > max_em and cur == "":         # 单个超长词：退化成按字断
            piece = ""
            for ch in tk:
                if piece and _em(piece) + _em(ch) > max_em:
                    lines.append(piece)
                    piece = ""
                piece += ch
            cur, em = piece, _em(piece)
            continue
        cur += tk
        em += w
    lines.append(cur.rstrip())
    # 孤行回补：末行只剩一两个字就把上一行末尾挪下来。
    # 中文整串在 `_tokens` 里是一个 token（为了不断开词），所以这里按**字**退；
    # ASCII 词仍整体走，不许从词中间切（SMAD2/3 被拆成 SMAD2／/3 就是这种错）。
    if len(lines) >= 2:
        prev = lines[-2]
        if lines[-1] and _em(lines[-1]) < max_em * 0.35 and len(prev) > 1:
            for cut in range(len(prev) - 1, 0, -1):
                rest, moved = prev[:cut].rstrip(), prev[cut:].lstrip()
                if not rest or not moved:
                    continue
                a, b = prev[cut - 1], prev[cut]
                if a.isascii() and b.isascii() and (a.isalnum() or a in "/.%-+") \
                        and (b.isalnum() or b in "/.%-+"):
                    continue                  # 不许把 ASCII 词切两半
                if _em(rest) <= max_em and _em(moved + lines[-1]) <= max_em:
                    lines[-2] = rest
                    lines[-1] = (moved + lines[-1]).strip()
                    break
            # 中文两行还要**均分**：贪心断成「9 字 + 2 字」虽然没了孤字，
            # 仍然是歪的；按字数对半切才像期刊里那种居中排的字块。
            a, b = lines[-2], lines[-1]
            if len(lines) == 2 and " " not in a and " " not in b \
                    and len(a) >= len(b) + 2:
                whole = a + b
                cut = (len(whole) + 1) // 2
                if _em(whole[:cut]) <= max_em and _em(whole[cut:]) <= max_em:
                    lines[-2], lines[-1] = whole[:cut], whole[cut:]
    return [l for l in lines if l] or [""]


def _wrap(label: str, max_em: float) -> list[str]:
    """按 em 宽度贪心换行。中英混排共用 `deck.text_advance_em` 那一把尺子。

    只在**可断处**断：ASCII 词整体走（不拆 SMAD2/3），中文按字断，末行不留孤字。
    """
    out: list[str] = []
    for para in str(label).split("\n"):
        out.extend(_wrap_para(para, max_em))
    return out or [""]


#: 浏览器实测（2026-11-12，12 个中英混排样本 / 10px 字号）：
#: `deck.text_advance_em` 对中文混排低估 2~10%，连续大写西文最多 19.5%。
#: 内核零依赖、出图时**不跑浏览器** ⇒ 只能取保守上界，宁可框略大，
#: 也不许文字压出形状。纯大写长标签由 `_wrap` 的 170px 上限折行兜住。
_TEXT_EM_SAFETY = 1.12

#: `_text_box` 的几何内缩 —— 尺寸计算与容器判据**共用一份**，不许两处各抄
#: 0.22 / 0.146 然后悄悄漂移。
_HEX_INSET = 0.22
_CIRCLE_K = (1.0 - 2 ** -0.5) / 2
_CIRCLE_INNER = 1.0 - 2 * _CIRCLE_K          # = 1/√2 ≈ 0.7071
_HEX_INNER = 1.0 - 2 * _HEX_INSET            # = 0.56


def _shape_box(tw: float, th: float, kind: str) -> tuple[float, float]:
    """由文字块（最长行宽 tw、总行高 th）反解外框。

    内缩系数与 `_text_box` **成对**：这里算「应当装得下」的外框，那边量
    「文字实际被装进去的框」，两处各抄一份就会漂移。
    """
    if kind == "circle":
        # 圆的内接正方形边长 = d·_CIRCLE_INNER，必须 ≥ 文字宽 × 安全系数。
        # 原式 √(tw²+th²) 把文字当成「旋转 45° 的矩形」，对轴对齐文字系统性
        # 偏小 ⇒ 2026-11-12 真产出里「SMAD2/3 磷酸化」实测压出圆弧 5.5px，
        # 而质量门却把实测判成 NOT AUDITABLE。几何公式错，不是字体玄学。
        need = max(tw * _TEXT_EM_SAFETY, th)
        d = need / _CIRCLE_INNER + _PAD_X * 2
        return max(d, 56.0), max(d, 56.0)
    if kind == "hexagon":
        # 内接框 = [0.22w, 0.78w] × [y+2, y+h-2]。宽度只按内接框反解；
        # 旧式额外 `+ h*0.9` 把六边形撑成一条扁箭头（2026-11-14 真产物）——
        # 内接框比斜边更保守，那段补偿只会放大外形，不会增加任何安全。
        h = max(th + 10.0, 30.0)
        w = (tw * _TEXT_EM_SAFETY + _PAD_X * 2) / _HEX_INNER
        return max(w, 56.0), h
    if kind == "decision":
        # 菱形的内接矩形只有外框的一半宽、一半高 ⇒ 外框翻倍才装得下字。
        return tw * 2 + _PAD_X * 2, th * 2 + _PAD_Y * 2
    h = max(th + _PAD_Y * 2, 30.0)
    w = tw + _PAD_X * 2
    if kind in ("terminator", "stadium"):
        w += h                      # 两端半圆各吃掉 h/2，字只能躺在中间那段
    elif kind == "cylinder":
        w += h * 0.5                # 横放圆柱的左右端盖
    elif kind == "subroutine":
        w += 18.0                   # 双竖线各占 9px
    elif kind == "database":
        h += h * 0.28               # 顶 / 底椭圆各吃掉一段高度
    return max(w, 56.0), h


def _node_size(label: str, kind: str, icon: str = "") -> tuple[float, float, list[str]]:
    """先折行再反解外框，外框不超过形状上限（长标签不许把节点撑成半张图）。

    `icon` 命中生物图元库时，节点 = **图元 + 下方标签**：尺寸由图元默认大小
    决定（细胞大、分子小），不再由文字反解。文字只决定要不要把框撑宽一点。
    """
    if icon:
        from rongke import svgbio as _bio
        iw, ih = _bio.ICON_SIZE.get(icon, (48.0, 48.0))
        lines = _wrap(label, max(iw, 96.0) / NODE_SIZE) if label.strip() else []
        tw = max((text_advance_em(x) for x in lines), default=0.0) * NODE_SIZE
        th = len(lines) * NODE_SIZE * _LINE_H
        # 图元在上、标签在下：标签与图元之间留 6px，标签占 th。
        # 没写标签的图元（细胞器）不占标签位，也不画空文字。
        return max(iw, tw + 8.0), ih + (6.0 + th if lines else 0.0), lines or [""]
    cap = {"circle": _CIRCLE_MAX_D, "hexagon": _HEX_MAX_W,
           "decision": _DEC_MAX_W}.get(kind)
    lines = _wrap(label, _MAX_NODE_W / NODE_SIZE)
    box = _shape_box(max(text_advance_em(x) for x in lines) * NODE_SIZE,
                     len(lines) * NODE_SIZE * _LINE_H, kind)
    if cap is None:
        return box[0], box[1], lines
    for limit in _TEXT_LIMITS:
        lines = _wrap(label, limit / NODE_SIZE)
        tw = max(text_advance_em(x) for x in lines) * NODE_SIZE
        th = len(lines) * NODE_SIZE * _LINE_H
        box = _shape_box(tw, th, kind)
        if box[0] <= cap and (kind != "circle" or box[1] <= cap):
            return box[0], box[1], lines
    # 全档都超限：按最后一档的真实尺寸画，字不缩（缩了就是「装不下还硬装」）。
    return box[0], box[1], lines


def _text_box(x: float, y: float, w: float, h: float,
              kind: str) -> tuple[float, float, float, float]:
    """节点标签**应当被装进去**的框。交给容纳门当判据。"""
    if kind == "decision":
        return x + w / 4, y + h / 4, x + w * 3 / 4, y + h * 3 / 4
    if kind in ("terminator", "stadium"):
        return x + h / 2, y + 2.0, x + w - h / 2, y + h - 2.0
    if kind == "circle":
        return (x + w * _CIRCLE_K, y + h * _CIRCLE_K,
                x + w * (1 - _CIRCLE_K), y + h * (1 - _CIRCLE_K))
    if kind == "hexagon":
        return (x + w * _HEX_INSET, y + 2.0,
                x + w * (1 - _HEX_INSET), y + h - 2.0)
    if kind == "cylinder":
        return x + h * 0.26, y + 2.0, x + w - h * 0.26, y + h - 2.0
    if kind == "subroutine":
        return x + 12.0, y + 2.0, x + w - 12.0, y + h - 2.0
    if kind == "database":
        return x + 4.0, y + h * 0.14, x + w - 4.0, y + h * 0.86
    return x + 3.0, y + 2.0, x + w - 3.0, y + h - 2.0


# ══════════════════════════════════════════════════════════════════
# 分层布局
# ══════════════════════════════════════════════════════════════════

def _break_cycles(ids: list[str], edges: list[dict]) -> set[int]:
    """迭代 DFS 找回边（指向仍在栈上的节点）。返回要临时反转的边下标。

    反转只用于**布局**；画的时候箭头仍指向原 target，所以反馈回路的方向
    不会被画反。用迭代而不是递归：节点一多递归会炸，而炸了就是整张图出不来。
    """
    adj: dict[str, list[tuple[str, int]]] = {i: [] for i in ids}
    for k, e in enumerate(edges):
        adj[e["from"]].append((e["to"], k))
    state = dict.fromkeys(ids, 0)
    back: set[int] = set()
    for root in ids:
        if state[root]:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            u, pi = stack[-1]
            if pi < len(adj[u]):
                stack[-1] = (u, pi + 1)
                v, k = adj[u][pi]
                if state[v] == 1:
                    back.add(k)
                elif state[v] == 0:
                    state[v] = 1
                    stack.append((v, 0))
            else:
                state[u] = 2
                stack.pop()
    return back


def _layout(nodes: list[dict], edges: list[dict], direction: str,
            edge_label_em: dict[int, float],
            hints: dict | None = None) -> dict[str, Any]:
    """算出每个节点/虚节点的 (flow, cross) 中心与尺寸，以及每条边的折点。

    `hints` 是 AI 声明的**布局意图**（不是坐标）：
      · route  {边序号: "top"|"bottom"} —— 回边虚节点在层内排到哪一侧；
      · align  [[id, id, ...], ...] —— 一组节点在层内相邻、按给定顺序排。
    两者都只改排序权重，不碰几何公式，所以质量门与写后读回全部照旧。
    """
    route = (hints or {}).get("route") or {}
    align_groups = (hints or {}).get("align") or []
    ids = [n["id"] for n in nodes]
    back = _break_cycles(ids, edges)
    # 布局方向的边表（回边临时反转）
    dag: list[tuple[str, str, int]] = []
    for k, e in enumerate(edges):
        f, t = ((e["to"], e["from"]) if k in back else (e["from"], e["to"]))
        dag.append((f, t, k))
    indeg = dict.fromkeys(ids, 0)
    out: dict[str, list[str]] = {i: [] for i in ids}
    for f, t, _k in dag:
        out[f].append(t)
        indeg[t] += 1
    q = deque(i for i in ids if indeg[i] == 0)
    layer = dict.fromkeys(ids, 0)
    topo: list[str] = []
    while q:
        u = q.popleft()
        topo.append(u)
        for v in out[u]:
            layer[v] = max(layer[v], layer[u] + 1)
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(topo) != len(ids):
        raise DiagramError("环没拆干净（这是本后端的缺陷，不是规格的错）。")

    # 说明：「区室按声明顺序落层」试过一版（把胞质整块推到核之上），区室确实
    # 变成连续色带，但把「诱导表达 tf→smad7」压成了逆向边 —— 箭头方向反了，
    # 语义错。图形的正确性优先于色带整齐，故本版不做层序重排；
    # 同层共占的框叠由 `_settle_compartments` 裁中缝，仍裁不开的淡到近乎白。

    # 跨层边插虚节点：每段线只走在层间空档里 ⇒ 结构上不可能穿过节点框
    size: dict[str, tuple[float, float]] = {}
    lines: dict[str, list[str]] = {}
    for n in nodes:
        w, h, ls = _node_size(n["label"], n["kind"], n.get("icon", ""))
        size[n["id"]] = (w, h)
        lines[n["id"]] = ls
    lchain: dict[int, list[str]] = {}
    chain: dict[int, list[str]] = {}
    dummy_layer: dict[str, int] = {}
    dummy_owner: dict[str, int] = {}
    for f, t, k in dag:
        span = layer[t] - layer[f]
        ch = [f]
        for j in range(1, span):
            did = f"__d{k}_{j}"
            dummy_owner[did] = k          # 虚节点属于哪条边，route 意图靠它定位
            dummy_layer[did] = layer[f] + j
            size[did] = (0.0, 12.0)
            ch.append(did)
        ch.append(t)
        lchain[k] = ch
        chain[k] = ch[::-1] if k in back else ch

    all_ids = ids + list(dummy_layer)
    lay_of = dict(layer)
    lay_of.update(dummy_layer)
    layers: dict[int, list[str]] = {}
    for i in all_ids:
        layers.setdefault(lay_of[i], []).append(i)
    preds: dict[str, list[str]] = {i: [] for i in all_ids}
    succs: dict[str, list[str]] = {i: [] for i in all_ids}
    for ch in lchain.values():
        for a, b in zip(ch, ch[1:]):
            succs[a].append(b)
            preds[b].append(a)

    # 重心法排序：上下扫几遍，减少连线交叉
    order = {L: list(v) for L, v in layers.items()}
    pos: dict[str, int] = {}
    for L in sorted(order):
        for j, i in enumerate(order[L]):
            pos[i] = j
    for it in range(8):
        down = it % 2 == 0
        nb = preds if down else succs
        for L in (sorted(order) if down else sorted(order, reverse=True)):
            def bary(i: str) -> float:
                ns = nb[i]
                return sum(pos[v] for v in ns) / len(ns) if ns else float(pos[i])

            def rank(i: str) -> float:
                # 回边的虚节点挤到层的一端 —— 这是「route 意图真的改变几何」
                # 的落点，不是存个字段装样子。**默认也走边侧**：摆在重心位
                # 会让回边从整张图中间斜穿过去，压住层间标签与区室框
                # （2026-11-14 真产物肉眼可见「负反馈」线穿过图心）。
                k = dummy_owner.get(i)
                if k is not None and k in back:
                    r = route.get(str(k)) or route.get(k)
                    if r == "top":
                        return -1e9
                    return 1e9          # bottom 与「没声明」同侧
                return bary(i)

            order[L] = sorted(order[L], key=lambda i: (rank(i), pos[i]))
            for j, i in enumerate(order[L]):
                pos[i] = j

    # 意图级对齐：把一组节点挪到层内相邻、按声明顺序排。
    # 必须放在重心法**之后**，否则下一轮排序又会把它们冲散。
    for grp in align_groups:
        want = [str(x) for x in grp]
        for L in sorted(order):
            present = [i for i in order[L] if i in want]
            if len(present) < 2:
                continue
            anchor = min(order[L].index(i) for i in present)
            rest = [i for i in order[L] if i not in want]
            before_n = sum(1 for i in order[L][:anchor] if i not in want)
            block = [i for i in want if i in order[L]]
            order[L] = rest[:before_n] + block + rest[before_n:]
    for L in sorted(order):
        for j, i in enumerate(order[L]):
            pos[i] = j

    lr = direction == "LR"
    # flow = 层推进方向，cross = 层内排列方向
    def fs(i: str) -> float:
        return size[i][0 if lr else 1]

    def cs(i: str) -> float:
        return size[i][1 if lr else 0]

    maxL = max(order)
    colw = {L: max((fs(i) for i in order[L]), default=0.0) for L in order}
    # 层间空档要装得下边标签**沿 flow 轴**的那一维：LR 是字宽，TB 是行高。
    # 拿字宽去撑 TB 的空档只会白白拉高画布，装不下的那一维反倒没量。
    gap_need: dict[int, float] = {}
    for k, ch in lchain.items():
        L = lay_of[ch[0]]
        need = (edge_label_em.get(k, 0.0) * EDGE_SIZE if lr
                else EDGE_SIZE * _LINE_H)
        gap_need[L] = max(gap_need.get(L, 0.0), need)
    # 区室框在 flow 两端各探进空档一点：终点侧是底边留白，起点侧 LR 也是留白、
    # TB 则是标题带（组名画在框顶）。边标签只能落在两者之间的安全窗里。
    res_lo = _GROUP_PAD + 2.0
    res_hi = (_GROUP_PAD if lr else _GROUP_HEAD) + 2.0
    gaps: dict[int, float] = {}
    for L in range(maxL):
        # 空档要同时容下两端区室探进来的部分 + 边标签本身。只按边标签量空档，
        # 区室边框/标题带就会压到边标签上（线穿字，文字门量不到）。
        need = gap_need.get(L, 0.0) + res_lo + res_hi + _EDGE_PAD
        gaps[L] = max(_MIN_LAYER_GAP, need)
    flow0: dict[int, float] = {}
    cur = _MARGIN
    for L in range(maxL + 1):
        flow0[L] = cur
        cur += colw[L] + gaps.get(L, 0.0)
    total_flow = cur - gaps.get(maxL, 0.0) + _MARGIN

    colh = {L: (sum(cs(i) for i in order[L]) + _NODE_GAP * (len(order[L]) - 1))
            for L in order}
    max_cross = max(colh.values())
    center: dict[str, tuple[float, float]] = {}
    for L in sorted(order):
        c = _MARGIN + (max_cross - colh[L]) / 2
        for i in order[L]:
            center[i] = (flow0[L] + colw[L] / 2, c + cs(i) / 2)
            c += cs(i) + _NODE_GAP
    total_cross = max_cross + _MARGIN * 2

    # 折点（原方向）：起点贴源节点边框，终点贴靶节点边框，中间过虚节点中心
    polys: dict[int, list[tuple[float, float]]] = {}
    for k, ch in chain.items():
        pts: list[tuple[float, float]] = []
        for j, i in enumerate(ch):
            fc, cc = center[i]
            if j == 0:
                pts.append((fc + fs(i) / 2, cc) if lr else (cc, fc + fs(i) / 2))
            elif j == len(ch) - 1:
                pts.append((fc - fs(i) / 2, cc) if lr else (cc, fc - fs(i) / 2))
            else:
                pts.append((fc, cc) if lr else (cc, fc))
        polys[k] = pts

    w = total_flow if lr else total_cross
    h = total_cross if lr else total_flow
    return {"center": center, "size": size, "lines": lines, "polys": polys,
            "chain": chain, "layer": lay_of, "flow0": flow0, "colw": colw,
            "gaps": gaps, "res_lo": res_lo, "res_hi": res_hi,
            "natural_w": w, "natural_h": h, "lr": lr,
            "back": back, "nodes": ids}


# ══════════════════════════════════════════════════════════════════
# 渲染
# ══════════════════════════════════════════════════════════════════

def _arrowhead(pts: list[tuple[float, float]], kind: str) -> list[tuple[float, float]]:
    """按最后一段的方向算箭头三角形 / 抑制横杠的顶点。"""
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    px, py = -uy, ux
    if kind == "inhibit":
        # T 形横杠：垂直于来向的一根粗短线，用窄矩形当填充多边形
        bx, by = x1 - ux * 2.0, y1 - uy * 2.0
        return [(bx + px * _ARROW_HALF, by + py * _ARROW_HALF),
                (bx - px * _ARROW_HALF, by - py * _ARROW_HALF),
                (bx - px * _ARROW_HALF - ux * 2.0, by - py * _ARROW_HALF - uy * 2.0),
                (bx + px * _ARROW_HALF - ux * 2.0, by + py * _ARROW_HALF - uy * 2.0)]
    bx, by = x1 - ux * _ARROW_LEN, y1 - uy * _ARROW_LEN
    return [(x1, y1), (bx + px * _ARROW_HALF, by + py * _ARROW_HALF),
            (bx - px * _ARROW_HALF, by - py * _ARROW_HALF)]


def _legend_width(items: list[dict]) -> float:
    """图例一行的自然宽度（不含左右 margin）。图元/色块 + 文字 + 项间距。"""
    total = 0.0
    for it in items:
        total += (LEGEND_H + 6.0) if (it.get("icon") or it.get("color")) else 0.0
        total += text_advance_em(str(it.get("label") or "")) * LEGEND_SIZE + 20.0
    return total


def _draw_legend(C, items: list[dict], x: float, y: float) -> None:
    """画图例：每一项是「小图元或色块 + 名称」，一行排开。

    图例是机制图的**读图契约**：细胞膜、外泌体、激活/抑制这些符号不解释，
    读者只能猜。给 AI 一个显式字段，比要求它「自己画个图例在角落」可靠得多。
    """
    from rongke import svgbio as _bio
    if any(it.get("icon") for it in items):
        C.add_def(_bio.defs())
    cursor = x
    for it in items:
        icon = it.get("icon")
        if icon:
            iw, ih = _bio.ICON_SIZE.get(icon, (LEGEND_H, LEGEND_H))
            k = LEGEND_H / max(iw, ih)
            C.fragment(_bio.draw_icon(
                icon, cursor + (LEGEND_H - iw * k) / 2,
                y + (LEGEND_H - ih * k) / 2, iw * k, ih * k, ""))
            cursor += LEGEND_H + 6.0
        elif it.get("color"):
            C.rect(cursor, y + 5.0, 17.0, LEGEND_H - 10.0, _PAPER,
                   stroke=str(it["color"]), stroke_width=_NODE_STROKE_W)
            cursor += 17.0 + 6.0
        label = str(it.get("label") or "")
        C.text(cursor, y + LEGEND_H / 2 + LEGEND_SIZE * 0.32, label,
               size=LEGEND_SIZE, anchor="start", fill=_INK)
        cursor += text_advance_em(label) * LEGEND_SIZE + 20.0


def _draw_edge_end(C, pts: list[tuple[float, float]], kind: str, tip: str) -> None:
    """画边末端语义标记：机制图里「怎么作用」靠末端图形说，不能只有三角。

    八种边共用一条 polyline，末端各画各的：抑制 T 杠、磷酸化圆点、结合双向、
    剪切双杠、分泌囊泡、降解叉号。全部用几何原语（多边形 / 圆），
    零 `<marker>`、零 path —— 显式坐标才能被 inspect_svg 逐点量到。
    """
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n          # 指向终点
    px, py = -uy, ux                 # 终点处的横向

    def bar(bx: float, by: float, half: float, w: float) -> list:
        """以 (bx,by) 为中心、沿横向的细长矩形（用 4 点，不用 rect 免描边）。"""
        return [(bx + px * half, by + py * half), (bx + px * half + ux * w,
                by + py * half + uy * w), (bx - px * half + ux * w,
                by - py * half + uy * w), (bx - px * half, by - py * half)]

    if kind == "inhibit":
        C.polygon(_arrowhead(pts, "inhibit"), _AXIS, cls="arrow", tip=tip)
        return
    if kind == "degrade":
        # 叉号：以终点前 4px 为中心，两条对角线交叉
        bx, by = x1 - ux * 4.0, y1 - uy * 4.0
        nx, ny = (ux - px) / 1.4142, (uy - py) / 1.4142
        for sg in (1, -1):
            a = (bx + sg * (px * 3.4 + ux * 3.4), by + sg * (py * 3.4 + uy * 3.4))
            b = (bx - sg * (px * 3.4 + ux * 3.4), by - sg * (py * 3.4 + uy * 3.4))
            C.polygon([(a[0] + nx * 1.0, a[1] + ny * 1.0),
                       (b[0] + nx * 1.0, b[1] + ny * 1.0),
                       (b[0] - nx * 1.0, b[1] - ny * 1.0),
                       (a[0] - nx * 1.0, a[1] - ny * 1.0)],
                      _AXIS, cls="arrow", tip=tip)
        return
    if kind == "bind":
        # 双向：终点三角 + 起点反向三角（结合/相互作用没有方向）
        C.polygon(_arrowhead(pts, "arrow"), _AXIS, cls="arrow", tip=tip)
        C.polygon(_arrowhead(list(reversed(pts)), "arrow"), _AXIS,
                  cls="arrow", tip=tip)
        return

    C.polygon(_arrowhead(pts, "arrow"), _AXIS, cls="arrow", tip=tip)
    if kind == "phospho":
        C.circle(x1 - ux * 9.0, y1 - uy * 9.0, 3.0, _AXIS, cls="arrow", tip=tip)
    elif kind == "cleave":
        for off in (6.0, 10.0):
            C.polygon(bar(x1 - ux * off, y1 - uy * off, 4.2, 1.4),
                      _AXIS, cls="arrow", tip=tip)
    elif kind == "secrete":
        for sg in (1, -1):
            C.circle(x1 - ux * 7.0 + px * 5.4 * sg,
                     y1 - uy * 7.0 + py * 5.4 * sg, 2.1, _AXIS, cls="arrow", tip=tip)


def _node_color(nodes: list[dict], groups: list[dict]) -> dict[str, str]:
    """节点描边色 = 它所在区室的色；没区室的回落墨色；判断框单独砖红。

    颜色只表达「在哪」，不表达「第几个」—— 按序号轮色会让读图的人把
    一红一绿的无关图元当成同一条通路（2026-11-14 真产物就是彩虹图）。
    """
    tint = {g["id"]: _GROUP_TINTS[i % len(_GROUP_TINTS)]
            for i, g in enumerate(groups)}
    return {n["id"]: (_DECISION_TINT if n["kind"] == "decision"
                      else tint.get(n["group"], _NODE_INK)) for n in nodes}


def _seg_hit(p, q, r) -> bool:
    """线段与矩形是否相交（Liang–Barsky，纯算术，零依赖）。"""
    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pn, qn in ((-dx, x0 - r[0]), (dx, r[2] - x0),
                   (-dy, y0 - r[1]), (dy, r[3] - y0)):
        if pn == 0:
            if qn < 0:
                return False
            continue
        t = qn / pn
        if pn < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _rect_ov(a, b) -> bool:
    return (min(a[2], b[2]) - max(a[0], b[0]) > 0
            and min(a[3], b[3]) - max(a[1], b[1]) > 0)


_ORGANELLE_PAD = 22.0
#: 细胞膜的最小高宽比：低于这个比例，膜读成「香肠」而不是细胞。
_CELL_MIN_RATIO = 0.56

#: 「区室 = 一个细胞」时，哪些图元**本身就是细胞**（细胞级尺度）。
#: 组里只有这些图元时，膜不另画（膜 = 那个细胞的轮廓）。
_CELL_SCALE_ICONS = ("cell", "cell_t", "cell_b", "cell_cd4", "dendritic",
                     "macrophage", "tumor", "tissue", "organ", "mouse")
#: 胞内散布的锚点角序：先斜角（离开核的正上/正下），再两侧，最后正上正下。
#: 参考图里细胞器都散在核周围，正上方往往留给「转录」标签。
_ORGANELLE_ANGLES = (-140, -40, 140, 40, 180, 0, -90, 90, -160, -20, 160, 20)


def _scatter_organelles(center: dict[str, tuple[float, float]],
                        size: dict[str, tuple[float, float]],
                        nodes: list[dict], groups: list[dict],
                        edges: list[dict]) -> None:
    """把细胞里**没有连边**的图元（细胞器）散布到膜内侧，DAG 不管它们。

    DAG 只认边：er / golgi / mito 这些孤立图元会被排到 layer 0，与疫苗并列，
    细胞膜因此被拉成一长条、胞内一大片空白（2026-11-14 插图模式样图实测）。
    这里按「贴膜内侧」的锚点重摆，碰撞了换下一个角度；全撞了就原位不动，
    交给 craft 审计如实报。只动孤立图元，有边的成员一个都不挪（语义优先）。
    """
    deg = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e["from"] in deg:
            deg[e["from"]] += 1
        if e["to"] in deg:
            deg[e["to"]] += 1
    icon_of = {n["id"]: n.get("icon", "") for n in nodes}

    def box4(i: str) -> tuple[float, float, float, float]:
        fc, cc = center[i]
        sw, sh = size[i]
        return (fc - sw / 2, cc - sh / 2, fc + sw / 2, cc + sh / 2)

    for g in groups:
        if str(g.get("kind") or "") != "cell":
            continue
        mem = [n["id"] for n in nodes if n["group"] == g["id"]]
        free = [i for i in mem if deg[i] == 0 and icon_of.get(i)]
        anchor = [i for i in mem if i not in free]
        if not free or not anchor:
            continue
        rb = [box4(i) for i in anchor]
        x0 = min(r[0] for r in rb)
        y0 = min(r[1] for r in rb)
        x1 = max(r[2] for r in rb)
        y1 = max(r[3] for r in rb)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = (x1 - x0) / 2 + _ORGANELLE_PAD, (y1 - y0) / 2 + _ORGANELLE_PAD
        # 细胞不是胶囊：DAG 把成员排成一行时 ry 会小到只剩一条缝，细胞器
        # 全挤在一条横带上。给竖径一个下限，锚点才有上下可放。
        ry = max(ry, rx * _CELL_MIN_RATIO)
        blocked = [box4(i) for i in center if i not in free]
        # 边的中点附近是**边标签的落位区**（标签沿 flow 轴钉在空档正中）。
        # 细胞器散过去就是「图元压字」——craft P4 量的是标签框，不量图元，
        # 但人眼一眼能看到。这里按连线中点预留一条带，宁可少散一个位置。
        for e in edges:
            a, b = center.get(e["from"]), center.get(e["to"])
            if not a or not b:
                continue
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            blocked.append((mx - 40.0, my - 14.0, mx + 40.0, my + 14.0))
        placed: list[tuple[float, float, float, float]] = []
        for i in free:
            sw, sh = size[i]
            got = None
            for ang in _ORGANELLE_ANGLES:
                th = math.radians(ang)
                px = cx + rx * 0.74 * math.cos(th) - sw / 2
                py = cy + ry * 0.74 * math.sin(th) - sh / 2
                cand = (px, py, px + sw, py + sh)
                if any(_rect_ov(cand, b) for b in blocked):
                    continue
                if any(_rect_ov(cand, b) for b in placed):
                    continue
                got = cand
                break
            if got is None:
                continue
            center[i] = ((got[0] + got[2]) / 2, (got[1] + got[3]) / 2)
            placed.append(got)


def _settle_compartments(boxes: list[dict], lr: bool) -> None:
    """区室框互叠时，把后画的那块沿 cross 轴裁到两组成员的中间缝。

    层号来自图深度，同一层可能同时站着两个区室的节点 ⇒ 整组框必然叠
    （2026-11-14 真产物量到 311×76px，肉眼就是「胞质框套着核框」）。
    裁的边界取两组成员的间隔：裁得掉就裁，裁不掉（成员本身已经压在一起）
    就保持原样 —— 交给 craft 审计如实报红，不在这里假装解决。
    """
    lo_i, hi_i = (1, 3) if lr else (0, 2)      # cross 轴的低 / 高边
    fl = (0, 2) if lr else (1, 3)              # flow 轴

    def span(r) -> tuple[float, float]:
        return (r[1], r[1] + r[3]) if lr else (r[0], r[0] + r[2])

    for i, b in enumerate(boxes):
        for a in boxes[:i]:
            if min(b["rect"][fl[1]], a["rect"][fl[1]]) - max(
                    b["rect"][fl[0]], a["rect"][fl[0]]) <= 1.0:
                continue
            if min(b["rect"][hi_i], a["rect"][hi_i]) - max(
                    b["rect"][lo_i], a["rect"][lo_i]) <= 1.0:
                continue
            bl = [span(r) for r in b["members"]]
            al = [span(r) for r in a["members"]]
            alo, ahi = min(p[0] for p in al), max(p[1] for p in al)
            blo, bhi = min(p[0] for p in bl), max(p[1] for p in bl)
            if min(bhi, ahi) - max(blo, alo) > -2.0:
                continue                       # 成员本身就压着，裁不开
            # 两边的**边**都裁到两组成员的中缝：只裁一块的话，另一块自己的
            # padding 仍会探进来（真产物就卡在这 15px 上）。
            if blo >= ahi:                     # b 在下/右
                cut = (ahi + blo) / 2
                a["rect"][hi_i] = min(a["rect"][hi_i], cut - 0.5)
                b["rect"][lo_i] = max(b["rect"][lo_i], cut + 0.5)
                b["cut_lo"] = True             # 标题带被裁，改画在高边
                a["rect"][hi_i] = max(a["rect"][hi_i], ahi)
                b["rect"][lo_i] = min(b["rect"][lo_i], blo)
            elif bhi <= alo:                   # b 在上/左
                cut = (bhi + alo) / 2
                b["rect"][hi_i] = min(b["rect"][hi_i], cut - 0.5)
                a["rect"][lo_i] = max(a["rect"][lo_i], cut + 0.5)
                b["rect"][hi_i] = max(b["rect"][hi_i], bhi)
                a["rect"][lo_i] = min(a["rect"][lo_i], alo)


def _fit_report(nodes: list[dict], edges: list[dict], direction: str,
                label_em: dict[int, float], hints: dict | None,
                width_mm: float, L: dict) -> dict:
    """版面账：按声明宽度摆放时正文折合几 pt，换个方向要多少 mm。

    以前只丢一句「改 TB」，AI 不知道改了到底能不能装下 —— 只能试两次
    再放弃。这里把两个方向的 1:1 物理尺寸与字号都算出来，选择变成算术。
    """
    def pack(lay: dict, wmm: float) -> dict:
        # 画布已贴合内容（不撑宽）⇒ 摆进 wmm 版面时整体缩放，正文按同一比例走
        w = lay["natural_w"]
        h = lay["natural_h"]
        pt = NODE_SIZE * (wmm / w) * 2.8346
        return {"required_1to1_mm": [round(lay["natural_w"] * 25.4 / 96.0, 1),
                                     round(h * 25.4 / 96.0, 1)],
                "body_pt_if_placed": round(pt, 2),
                "width_px": round(w, 1)}
    other = "TB" if direction == "LR" else "LR"
    try:
        LO = _layout(nodes, edges, other, label_em, hints)
    except DiagramError:                     # 另一方向画不出来就如实不报
        LO = None
    out = {"direction": direction, "declared_width_mm": width_mm,
           "placed": pack(L, width_mm), "alternatives": []}
    if LO is not None:
        out["alternatives"].append(dict(pack(LO, width_mm), direction=other))
    return out


def _render_panels(spec: dict, out: str | Path) -> dict:
    """多 panel 组图：每个 panel 是一张独立的机制图，拼成 A/B/C 一张大图。

    参考图（BioRender 式）的完整故事往往是「几个 panel 各讲一段」：机制图、
    细胞互作、临床表型各占一格。没有这个能力，AI 只能一张图讲一件事。
    实现上不做「把多份节点塞进一套坐标」——每个 panel 走自己的完整
    布局/自检，再把 SVG 片段平移拼起来；panel 之间互不干扰，
    单图的判据（craft / 结构自检）照旧逐 panel 跑。
    """
    out = Path(out)
    panels = spec.get("panels")
    if not isinstance(panels, list) or not panels:
        raise DiagramError("panels 要给非空数组：[{id?, label?, spec:{...}}]。")
    if not all(isinstance(pn, dict) and isinstance(pn.get("spec"), dict)
               for pn in panels):
        raise DiagramError("每个 panel 必须是 {id?, label?, spec:{...}}，spec 为对象。")
    cols = int(spec.get("cols") or (2 if len(panels) > 1 else 1))
    cols = max(1, min(cols, len(panels)))
    rows = math.ceil(len(panels) / cols)
    direction = str(spec.get("direction") or "LR").upper()
    title = str(spec.get("title") or "")
    width_mm = float(spec.get("width_mm") or 180.0)
    tmpdir = out.parent / f".{out.stem}_panels"
    tmpdir.mkdir(parents=True, exist_ok=True)

    subs: list[dict] = []
    for i, pn in enumerate(panels):
        pid = str(pn.get("id") or chr(65 + i)).strip() or chr(65 + i)
        sub = dict(pn["spec"])
        sub.setdefault("direction", direction)
        sub["title"] = ""              # 子图不重复大标题；panel 自己带字母 + 小标题
        sub.setdefault("width_mm", width_mm / cols)
        tmp = tmpdir / f"panel_{i}_{pid}.svg"
        rep = render_diagram(sub, tmp)
        txt = tmp.read_text(encoding="utf-8")
        m = re.search(r"<svg\b[^>]*>(.*)</svg>", txt, re.S)
        inner = m.group(1) if m else ""
        # 梯度 / clip 的 id 是全局的，panel 之间会撞名（撞了就是整片图元变黑）。
        # 统一加 panel 前缀，连 `url(#x)` 的引用一起改。
        for _id in set(re.findall(r'id="([^"]+)"', inner)):
            inner = (inner.replace(f'id="{_id}"', f'id="{pid}_{_id}"')
                          .replace(f'url(#{_id})', f'url(#{pid}_{_id})'))
        subs.append({"id": pid, "label": str(pn.get("label") or ""),
                     "inner": inner, "w": float(rep["width_px"]),
                     "h": float(rep["height_px"]), "rep": rep,
                     "layout": str(tmp.with_suffix(".layout.json")),
                     "ok": bool(rep.get("ok")), "checks": rep.get("checks")})

    gap = 26.0
    band = 16.0                        # 每个 panel 顶部的字母带
    top = (TITLE_SIZE + 10.0) if title else 0.0
    legend = [x for x in (spec.get("legend") or []) if isinstance(x, dict)
              and str(x.get("label") or "").strip()]
    legend_w = _legend_width(legend) if legend else 0.0
    legend_h = (LEGEND_H + LEGEND_GAP) if legend else 0.0
    cell_w = [max(s["w"] for s in subs[c::cols]) for c in range(cols)]
    cell_h = [max(s["h"] for s in subs[r * cols:(r + 1) * cols])
              for r in range(rows)]
    W = max(2 * _MARGIN + sum(cell_w) + gap * (cols - 1),
            2 * _MARGIN + legend_w)
    H = (2 * _MARGIN + top + sum(cell_h) + band * rows + gap * (rows - 1)
         + legend_h)
    C = _Canvas(W, H)
    if title:
        C.text(W / 2, TITLE_SIZE + 2, title, size=TITLE_SIZE, weight="bold")
    row_y: list[float] = []
    yy = _MARGIN + top
    for r in range(rows):
        row_y.append(yy)
        yy += band + cell_h[r] + gap
    boxes: list[list[float]] = []
    for idx, s in enumerate(subs):
        r, c = divmod(idx, cols)
        col_x = _MARGIN + sum(cell_w[:c]) + gap * c
        x = col_x + (cell_w[c] - s["w"]) / 2
        # 内容**顶部对齐**（不是垂直居中）：同一行的两格图区从同一条线开始，
        # 字母也在同一条线上，读起来才是一张组图，不是四张截图。
        y = row_y[r] + band
        C.text(col_x + 2, row_y[r] + TITLE_SIZE, s["id"], size=TITLE_SIZE,
               weight="bold", anchor="start")
        if s["label"]:
            C.text(col_x + 2 + TITLE_SIZE * 0.92, row_y[r] + TITLE_SIZE,
                   s["label"], size=GROUP_SIZE, anchor="start", fill=_AXIS)
        C.fragment(f'<g transform="translate({x:.2f},{y:.2f})">'
                   f'{s["inner"]}</g>')
        boxes.append([round(x, 2), round(y, 2),
                      round(x + s["w"], 2), round(y + s["h"], 2)])

    if legend:
        _draw_legend(C, legend, _MARGIN, H - _MARGIN - LEGEND_H)

    desc = (f"荣珂原生 SVG 后端（零第三方依赖）| 机制图多 panel {cols}×{rows} | "
            f"panel {len(subs)} 个 | 预览级产物")
    svg = C.to_svg(title or "机制图（多 panel）", desc)
    p = out
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(svg, encoding="utf-8", newline="\n")

    checks = inspect_svg(p)
    checks["has_text"] = C.n_text >= len(subs)
    checks["readback_nonempty"] = len(svg) > 400
    checks["panels_ok"] = all(s["ok"] for s in subs)
    ok = all(bool(v) for v in checks.values())
    manifest = {
        "schema_version": 1, "backend": "native-svg-diagram",
        "kind": "diagram-panels", "direction": direction,
        "figure": {"width_mm": width_mm, "height_mm": H * 25.4 / 96.0,
                   "width_px": W, "height_px": H,
                   "mm_per_px": width_mm / W,
                   "intrinsic_width_mm": W * 25.4 / 96.0,
                   "placed_width_mm": width_mm, "clamped": False},
        "cols": cols, "rows": rows,
        "panels": [{"id": s["id"], "label": s["label"], "box": boxes[i],
                    "layout": s["layout"], "ok": s["ok"],
                    "checks": s["checks"],
                    "n_nodes": s["rep"].get("n_nodes", 0),
                    "n_edges": s["rep"].get("n_edges", 0)}
                   for i, s in enumerate(subs)],
        "nodes": [], "edges": [], "compartments": [],
    }
    mp = p.with_suffix(".layout.json")
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                  encoding="utf-8", newline="\n")
    warnings = [f"panel {s['id']}：{rep_w}" for s in subs
                for rep_w in (s["rep"].get("warnings") or [])]
    return {
        "ok": ok, "svg": str(p), "layout": str(mp), "bytes": p.stat().st_size,
        "backend": "native-svg-diagram", "scope": "preview",
        "kind": "diagram-panels", "direction": direction,
        "n_nodes": sum(s["rep"].get("n_nodes", 0) for s in subs),
        "n_edges": sum(s["rep"].get("n_edges", 0) for s in subs),
        "n_groups": sum(s["rep"].get("n_groups", 0) for s in subs),
        "n_marks": C.n_mark, "n_text": C.n_text,
        "width_px": W, "height_px": H,
        "width_mm": width_mm, "height_mm": H * 25.4 / 96.0,
        "layout_px_per_mm": W / width_mm,
        "checks": checks, "warnings": warnings,
        "panels": [{"id": s["id"], "box": boxes[i], "layout": s["layout"]}
                   for i, s in enumerate(subs)],
        "note": "" if ok else "结构自检有未通过项（见 checks）—— 文件保留在盘上供核查。",
        "svg_text": None,
    }


def render_diagram(spec: dict, out: str | Path,
                   hints: dict | None = None) -> dict:
    """按规格画一张机制图，写完读回自检。

    `hints` 是 AI 声明的布局意图（route / align），**独立于 spec**：
    不给就走原来的默认布局，一字不改；给了才在 `_layout` 的排序权重上生效。
    """
    if "panels" in spec:
        return _render_panels(spec, out)
    nodes, edges, groups, direction, title, width_mm = _validate(spec)
    label_em = {k: _label_em(e["label"])
                for k, e in enumerate(edges) if e["label"]}
    L = _layout(nodes, edges, direction, label_em, hints)
    lr = L["lr"]
    center, size, lines = L["center"], L["size"], L["lines"]

    target_w = width_mm * _MM_TO_PX
    # 图例（legend）：期刊组图几乎必带的一行——「这圈膜/这个形状代表什么」。
    # 没有它，读图的人只能靠猜；AI 画组图时也没地方声明「橙色=激活」。
    legend = [x for x in (spec.get("legend") or []) if isinstance(x, dict)
              and str(x.get("label") or "").strip()]
    legend_w = _legend_width(legend) if legend else 0.0
    legend_h = (LEGEND_H + LEGEND_GAP) if legend else 0.0
    # 画布**贴合内容**，不再撑到声明的版面宽。撑宽只会在两侧留出几百像素空白，
    # 整图看上去像一张没排版的草图（2026-11-14 用户原话：「谁家科研绘图长这样」）。
    # 声明宽度仍然决定「按版面摆放时字号多大」，所以 font_size_floor 门照旧有效。
    w = max(L["natural_w"], 2 * _MARGIN + legend_w)
    h = L["natural_h"] + (TITLE_SIZE * 1.6 if title else 0.0) + legend_h
    top = TITLE_SIZE * 1.6 if title else 0.0
    dx = 0.0
    dy = top
    C = _Canvas(w, h)
    warnings: list[str] = []
    fit = _fit_report(nodes, edges, direction, label_em, hints, width_mm, L)
    if L["natural_w"] > target_w + 0.5:
        # 给**可执行**的修法，不给三个模糊选项：字不能缩（5pt 物理下限），
        # 所以物理宽度只能跟着自然宽度长。把建议值算出来，AI 一步就能重画。
        suggest = width_mm * (L["natural_w"] / target_w) if target_w > 0 else width_mm
        alt = (fit["alternatives"] or [{}])[0]
        alt_txt = ""
        if alt:
            alt_txt = (
                f"换 direction={alt['direction']} 后 1:1 物理尺寸 "
                f"{alt['required_1to1_mm'][0]:g}×{alt['required_1to1_mm'][1]:g}mm，"
                f"按 {width_mm:g}mm 宽摆放正文 ≈{alt['body_pt_if_placed']:g}pt。")
        warnings.append(
            f"图按 1:1 物理宽度 {w * 25.4 / 96.0:.0f}mm 出（声明 {width_mm:g}mm，"
            f"内容 {L['natural_w']:.0f}px 装不下）。摆进 {width_mm:g}mm 版面时整体缩小，"
            f"正文折合 {fit['placed']['body_pt_if_placed']:g}pt"
            f"（font_size_floor 门会如实报红）。{alt_txt}"
            f"一步修法：把 spec 的 width_mm 改成 ≥{suggest:.0f}mm 再重画；"
            f"若版面只有 {width_mm:g}mm，则改 direction、缩短节点/边标签，"
            f"或把图拆成两张。别靠缩小字号压宽度 —— 红项只会从宽度挪到字号门。")
    elif L["natural_w"] < target_w - 0.5:
        warnings.append(
            f"图按内容宽度 {w * 25.4 / 96.0:.0f}mm 出（声明 {width_mm:g}mm）："
            f"再撑宽只会是两侧空白。要铺满版面就加节点 / 加一层 / 两个分支并排；"
            f"或把 width_mm 改成 ≈{w * 25.4 / 96.0:.0f}mm，让声明与实际一致。")

    def P(i: str) -> tuple[float, float, float, float]:
        fc, cc = center[i]
        sw, sh = size[i]
        x = (fc - sw / 2 + dx) if lr else (cc - sw / 2 + dx)
        y = (cc - sh / 2 + dy) if lr else (fc - sh / 2 + dy)
        return x, y, sw, sh

    # ── 胞内散布：无连边的细胞器不该被 DAG 挤成最左一列 ──
    # 参考图（BioRender 式）里细胞器是**散布在核周围**的。DAG 只认边，
    # 没有边的 er/golgi/mito 会被排到 layer 0 与疫苗并列，膜因此被拉成一长条、
    # 胞内一大片空白。这里把它们按「贴膜内侧」的锚点重摆一次，只动孤立图元。
    _scatter_organelles(center, size, nodes, groups, edges)

    # ── 区室框（画在最底层）──
    # 一个区室的成员未必落在相邻层（SMAD7 属胞质却被排到最后一层）。整组画一个
    # 框会把夹在中间的别的区室整个吞进去 —— 截图上就是「胞质框套着细胞核框」。
    # 按**相邻层 run** 拆框：同一区室允许出现多块，语义反而更准（胞质本就到处都在）。
    # 拆完仍可能同层撞上（两个区室在同一层各有节点）⇒ 再按 cross 轴把后画的
    # 那块裁到两组成员的中间缝里（`_settle_compartments`）。
    boxes: list[dict] = []
    gbox: dict[str, tuple[float, float, float, float]] = {}
    for g in groups:
        mem_nodes = [n for n in nodes if n["group"] == g["id"]]
        mem = [n["id"] for n in mem_nodes]
        if not mem:
            continue
        if g.get("kind") == "cell":
            # 组内**只有细胞级图元**时，区室的轮廓就是那个细胞本身：
            # 再套一圈膜就是「细胞套细胞」的双层圈（截图上一眼就假）。
            # 判据看 icon：膜只在「细胞里装着东西」（核/细胞器/分子）时才画。
            _inner = [n.get("icon") for n in mem_nodes if n.get("icon")]
            if _inner and all(ic in _CELL_SCALE_ICONS for ic in _inner):
                continue
            # 区室 = **一个细胞**：一圈膜（外粗内细 + 极浅胞质），成员在膜内。
            # 这是参考图（BioRender 式）的区室形态，不是矩形背景块。
            from rongke import svgbio as _bio
            rects = [P(i) for i in mem]
            x0 = min(r[0] for r in rects) - 18.0
            y0 = min(r[1] for r in rects) - 18.0
            x1 = max(r[0] + r[2] for r in rects) + 18.0
            y1 = max(r[1] + r[3] for r in rects) + 18.0
            # 膜也守同一个高宽比：DAG 把成员排成一行时，膜会扁成一条胶囊。
            # 只在不压到膜外节点的前提下往上下长；撞上了就保持原样。
            hh, ww = y1 - y0, x1 - x0
            if hh < ww * _CELL_MIN_RATIO:
                cy = (y0 + y1) / 2
                grow = (ww * _CELL_MIN_RATIO - hh) / 2
                ny0 = max(cy - hh / 2 - grow, top + 3.0)
                ny1 = cy + hh / 2 + grow
                probe = (x0, ny0, x1, ny1)
                outsider = [P(n["id"]) for n in nodes
                            if n["group"] != g["id"]]
                if not any(_rect_ov(probe, (r[0], r[1], r[0] + r[2],
                                            r[1] + r[3])) for r in outsider):
                    y0, y1 = ny0, ny1
            C.add_def(_bio.defs())
            tint = _GROUP_TINTS[groups.index(g) % len(_GROUP_TINTS)]
            C.fragment(_bio.membrane(x0, y0, x1 - x0, y1 - y0, tint))
            gbox[g["id"]] = (x0, y0, x1, y1)
            if g["label"]:
                ly = y0 + GROUP_SIZE + 4.0
                C.text(x0 + 12.0, ly, g["label"], size=GROUP_SIZE,
                       anchor="start", fill=tint,
                       box=(x0 + 4, y0 + 2, x1 - 4, ly + 4))
            continue
        runs: list[list[int]] = []
        for lv in sorted({L["layer"][i] for i in mem}):
            if runs and lv == runs[-1][-1] + 1:
                runs[-1].append(lv)
            else:
                runs.append([lv])
        for ri, run in enumerate(runs):
            rects = [P(i) for i in mem if L["layer"][i] in run]
            x0 = min(r[0] for r in rects) - _GROUP_PAD
            # 标题带是 `top`，区室框顶不能顶进去 —— 否则框线与标题文字重叠
            # （2026-11-12 真产出肉眼可见：标题被「细胞质」虚线框横穿）。
            y0 = max(min(r[1] for r in rects) - _GROUP_PAD - GROUP_SIZE * 1.5,
                     top + 2.0)
            x1 = max(r[0] + r[2] for r in rects) + _GROUP_PAD
            y1 = max(r[1] + r[3] for r in rects) + _GROUP_PAD
            boxes.append({"gid": g["id"], "label": g["label"], "ri": ri,
                          "rect": [x0, y0, x1, y1], "members": rects})
    _settle_compartments(boxes, lr)
    tint_of = {g["id"]: _GROUP_TINTS[i % len(_GROUP_TINTS)]
               for i, g in enumerate(groups)}
    # 区室成员被别的区室夹在中间时（同一层各占一块），这块不能画成实色：
    # 实色会变成「蓝底里贴了一块紫补丁」。改成近乎白的极浅色 —— 仍能看出
    # 「这组还继续到这里」，但不会读成第二个区室。
    for b in boxes:
        others = [P(n["id"]) for n in nodes
                  if n["group"] != b["gid"] and n["group"]]
        b["light"] = any(_rect_ov(b["rect"], (r[0], r[1], r[0] + r[2],
                                             r[1] + r[3])) for r in others)
    for b in boxes:
        x0, y0, x1, y1 = b["rect"]
        # 区室是**背景色块**，不是贴着节点的虚线框。浅色相 + 无边框 + 圆角，
        # 节点才是唯一的「深色图形」—— 这是期刊插图的层次关系。
        C.rect(x0, y0, x1 - x0, y1 - y0,
               _soften(tint_of.get(b["gid"], _NODE_INK),
                       0.94 if b["light"] else 0.88),
               stroke="none", cls="compartment", rx=8, tip=b["gid"])
        # 同一区室拆成多块框时，**只在第一块写名字**。每块都写会在图上
        # 冒出两个「细胞质」标题（2026-11-12 真产出肉眼可见）。
        # 框的归属改由 `<title>` 里的 group id 承载 —— 结构身份，
        # 不靠「rect 后面跟的那个 text」猜，人和判据都读得到。
        if b["ri"] == 0:
            if b.get("cut_lo") and lr:
                # 顶边被裁掉了 ⇒ 名字改贴底边，仍在框内
                ly = y1 - 3.0
                C.text(x0 + 8, ly, b["label"], size=GROUP_SIZE,
                       anchor="start", fill=tint_of.get(b["gid"], _AXIS),
                       box=(x0 + 2, ly - GROUP_SIZE * 1.5, x1 - 2, ly + 2))
            else:
                C.text(x0 + 8, y0 + GROUP_SIZE + 3, b["label"], size=GROUP_SIZE,
                       anchor="start", fill=tint_of.get(b["gid"], _AXIS),
                       box=(x0 + 2, y0, x1 - 2, y0 + GROUP_SIZE * 1.5 + 3))
        if b["gid"] in gbox:
            ox, oy, px, py = gbox[b["gid"]]
            gbox[b["gid"]] = (min(ox, x0), min(oy, y0), max(px, x1), max(py, y1))
        else:
            gbox[b["gid"]] = (x0, y0, x1, y1)

    # ── 连线（在节点之下）──
    label_slots: dict[int, list[tuple[int, float]]] = {}
    polys: list[list[tuple[float, float]]] = []
    for k, e in enumerate(edges):
        pts = [(x + dx, y + dy) for x, y in L["polys"][k]]
        polys.append(pts)
        dash = "5,4" if e["kind"] == "dashed" else ""
        C.polyline(pts, _AXIS, _EDGE_STROKE_W, dash=dash)
        tip = f'{e["from"]} → {e["to"]}（{e["kind"]}）'
        _draw_edge_end(C, pts, e["kind"], tip)
        if e["label"]:
            ch = L["chain"][k]
            # 这段线走在哪两层之间，标签就进哪个空档。回边在布局里被反转过，
            # 取两端层号的**较小值**才是空档下标；取 chain[0] 会指到上一层去。
            gi = min(L["layer"][ch[0]], L["layer"][ch[1]])
            label_slots.setdefault(gi, []).append(
                (k, (pts[0][1] + pts[1][1]) / 2 if lr
                 else (pts[0][0] + pts[1][0]) / 2))

    # ── 节点 ──
    node_kind = {n["id"]: n["kind"] for n in nodes}
    strokes = _node_color(nodes, groups)
    for idx, n in enumerate(nodes):
        i = n["id"]
        x, y, sw, sh = P(i)
        col = strokes[i]
        kind = n["kind"]
        tip = (f'{i}：{n["label"]}（{NODE_SHAPE_DESC.get(kind, kind)}）'
               + (f'｜区室 {n["group"]}' if n["group"] else ""))
        if n.get("icon"):
            # 插图模式：节点是一个**图元**，标签在图元下方，不再套框。
            # 记账上仍是一个 mark（用不可见外框），保真门 / 容纳门口径不变。
            from rongke import svgbio as _bio
            iw, ih = _bio.ICON_SIZE.get(n["icon"], (48.0, 48.0))
            C.add_def(_bio.defs())
            C.rect(x, y, sw, sh, "none", tip=tip)
            C.fragment(_bio.draw_icon(n["icon"], x + (sw - iw) / 2, y, iw, ih, col))
            ly = y + ih + 6.0
            ls = lines[i] if n["label"].strip() else []
            lh = NODE_SIZE * _LINE_H
            for j, ln in enumerate(ls):
                C.text(x + sw / 2, ly + lh * (j + 0.5) + NODE_SIZE * 0.2, ln,
                       size=NODE_SIZE, fill=_INK,
                       box=(x, ly, x + sw, ly + len(ls) * lh))
            continue
        # ── 形状全部用 canvas 既有原语组合，绝不用 <path>/<marker>：
        #    显式顶点才能被 `inspect_svg` 的 marks_in_viewbox 逐点量到。
        if kind == "decision":
            C.polygon([(x + sw / 2, y), (x + sw, y + sh / 2),
                       (x + sw / 2, y + sh), (x, y + sh / 2)],
                      _PAPER, stroke=col, tip=tip, stroke_width=_NODE_STROKE_W)
        elif kind == "terminator":
            C.rect(x, y, sw, sh, _PAPER, stroke=col, tip=tip, rx=sh / 2,
                   stroke_width=_NODE_STROKE_W)
        elif kind == "circle":
            r = min(sw, sh) / 2
            # 深色外圈只作描边（frame），浅色内盘才是 mark：文字压在纸色上，
            # contrast 门量的是**文字实际所在的那个盘**，不是装饰边框。
            C.circle(x + sw / 2, y + sh / 2, r, col, cls="frame")
            C.circle(x + sw / 2, y + sh / 2, max(r - 1.6, 1.0), _PAPER,
                     tip=tip)
        elif kind == "hexagon":
            C.polygon([(x + sw * 0.22, y), (x + sw * 0.78, y),
                       (x + sw, y + sh / 2), (x + sw * 0.78, y + sh),
                       (x + sw * 0.22, y + sh), (x, y + sh / 2)],
                      _PAPER, stroke=col, tip=tip, stroke_width=_NODE_STROKE_W)
        elif kind == "database":
            # 竖立圆柱：主体矩形 + 6 点近似的顶 / 底椭圆盖。
            eh = sh * 0.14
            C.rect(x, y + eh / 2, sw, sh - eh, _PAPER, stroke=col,
                   tip=tip, stroke_width=_NODE_STROKE_W)
            C.polygon([(x, y + eh), (x + sw * 0.25, y),
                       (x + sw * 0.75, y), (x + sw, y + eh),
                      (x + sw * 0.75, y + eh * 2),
                      (x + sw * 0.25, y + eh * 2)],
                      _PAPER, stroke=col, cls="frame", stroke_width=_NODE_STROKE_W)
            C.polygon([(x, y + sh - eh), (x + sw * 0.25, y + sh - eh * 2),
                       (x + sw * 0.75, y + sh - eh * 2),
                       (x + sw, y + sh - eh),
                       (x + sw * 0.75, y + sh), (x + sw * 0.25, y + sh)],
                      _PAPER, stroke=col, cls="frame", stroke_width=_NODE_STROKE_W)
        elif kind == "cylinder":
            # 横放圆柱：4 点菱形端盖（与 database 的 6 点椭圆盖刻意不同）。
            ew = min(sh * 0.22, sw * 0.18)
            C.rect(x + ew / 2, y, sw - ew, sh, _PAPER, stroke=col,
                   tip=tip, stroke_width=_NODE_STROKE_W)
            C.polygon([(x, y + sh / 2), (x + ew, y),
                       (x + ew * 2, y + sh / 2), (x + ew, y + sh)],
                      _PAPER, stroke=col, cls="frame", stroke_width=_NODE_STROKE_W)
            C.polygon([(x + sw, y + sh / 2), (x + sw - ew, y),
                       (x + sw - ew * 2, y + sh / 2),
                       (x + sw - ew, y + sh)],
                      _PAPER, stroke=col, cls="frame", stroke_width=_NODE_STROKE_W)
        elif kind == "subroutine":
            C.rect(x, y, sw, sh, _PAPER, stroke=col, tip=tip,
                   stroke_width=_NODE_STROKE_W)
            C.line(x + 7, y + 3, x + 7, y + sh - 3, col, 1.2)
            C.line(x + sw - 7, y + 3, x + sw - 7, y + sh - 3, col, 1.2)
        elif kind == "stadium":
            C.rect(x, y, sw, sh, _PAPER, stroke=col, tip=tip, rx=sh / 2,
                   stroke_width=_NODE_STROKE_W)
            C.rect(x + 3, y + 3, sw - 6, sh - 6, "none", stroke=col,
                   rx=max(sh / 2 - 3, 1), cls="frame", stroke_width=0.8)
        else:
            C.rect(x, y, sw, sh, _PAPER, stroke=col, tip=tip,
                   rx=4.0 if kind == "process" else 0.0, stroke_width=_NODE_STROKE_W)
        bx0, by0, bx1, by1 = _text_box(x, y, sw, sh, kind)
        ls = lines[i]
        lh = NODE_SIZE * _LINE_H
        ybase = y + sh / 2 - (len(ls) - 1) * lh / 2 + NODE_SIZE * 0.35
        for j, ln in enumerate(ls):
            C.text(x + sw / 2, ybase + j * lh, ln, size=NODE_SIZE,
                   fill=_INK, box=(bx0, by0, bx1, by1))

    # ── 边标签：同一空档里沿 cross 轴贪心让位，结构上不互相压 ──
    # 让位间距必须按**这一段方向上真实的字盒尺寸**算：LR 的 cross 是竖直方向，
    # 字盒高矮都一样（一行行高）；TB 的 cross 是水平方向，字盒宽随字数变。
    # 早先两个方向共用一个行高当间距，TB 下宽标签必然互相压 —— 门实测抓到了。
    def half(k: int) -> float:
        return (EDGE_SIZE * _LINE_H / 2 if lr
                else label_em[k] * EDGE_SIZE * _WIDTH_SAFETY / 2)

    bound = h if lr else w
    label_boxes: dict[int, tuple[float, float, float, float]] = {}
    for gi, items in label_slots.items():
        items.sort(key=lambda t: t[1])
        off = L["flow0"][gi] + L["colw"][gi] + (dx if lr else dy)
        span = L["gaps"][gi]
        # 安全窗：去掉两端区室探进空档的部分，标签钉在窗正中。
        mid = off + L["res_lo"] + (span - L["res_lo"] - L["res_hi"]) / 2
        placed: list[tuple[int, float]] = []
        taken: list[tuple[float, float, float, float]] = []
        prev = -1e9

        def lab_box(k: int, c: float,
                    moff: float = 0.0) -> tuple[float, float, float, float]:
            mx, my = (mid + moff, c) if lr else (c, mid + moff)
            w_l = label_em.get(k, 0.0) * EDGE_SIZE * _WIDTH_SAFETY
            if lr:
                w_l = min(w_l, max(span - 2 * _LABEL_PAD, 8.0))
            return (mx - (w_l + 4.0) / 2, my - 5.0,
                    mx + (w_l + 4.0) / 2, my - 5.0 + EDGE_SIZE * _LINE_H + 2.0)

        def avoid(k: int, c: float) -> tuple[float, float]:
            """候选位置压到别的连线 / 已放的标签就沿 cross 轴就近挪一档。

            TB 的 cross 是水平方向：两条斜边交叉时，**怎么横挪都还在另一条线上**
            （2026-11-14 插图模式 TB 实测：「辅助」压在 cd8→efft 上）。所以 TB
            额外允许沿 flow 轴在空档内上下微调几像素；LR 不需要这个维度。
            """
            step = (EDGE_SIZE * _LINE_H + 6.0) if lr else (
                label_em.get(k, 0.0) * EDGE_SIZE * _WIDTH_SAFETY + 10.0)
            main_steps = (0.0,) if lr else (0.0, -7.0, 7.0, -14.0, 14.0)
            for moff in main_steps:
                for m in _AVOID_STEPS:
                    cc = c + m * step
                    b = lab_box(k, cc, moff)
                    # 出界检查要沿 **cross 轴**：LR 的 cross 是 y（对画布高），
                    # TB 的 cross 是 x（对画布宽）。早先一律拿 y 对 bound，
                    # TB 下 bound=w 而 y 早已超过它 ⇒ 所有候选都被判出界，
                    # 让位等于没开（插图模式 TB 的「辅助」压线就是这么来的）。
                    if lr:
                        if b[1] < 2.0 or b[3] > bound - 2.0:
                            continue
                    elif b[0] < 2.0 or b[2] > bound - 2.0:
                        continue
                    hit = any(_seg_hit(pts[i], pts[i + 1], b)
                              for kk, pts in enumerate(polys) if kk != k
                              for i in range(len(pts) - 1))
                    if hit or any(_rect_ov(b, tb) for tb in taken):
                        continue
                    return cc, moff
            return c, 0.0               # 挪不开就原位画，工艺审计会如实报出来

        for k, cross in items:
            c = max(cross, prev + half(k) + _LABEL_PAD)
            c, moff = avoid(k, c)
            placed.append((k, c, moff))
            taken.append(lab_box(k, c, moff))
            prev = c + half(k)
        # 整摞超出画布就整体上移（间距不变）；上移后仍探出另一边，
        # 说明这个空档里的边标签实在太多 —— 如实报警，不缩字、不叠字。
        over = placed[-1][1] + half(placed[-1][0]) + 4.0 - bound
        if over > 0:
            placed = [(k, c - over, mo) for k, c, mo in placed]
            if placed[0][1] - half(placed[0][0]) < 4.0:
                warnings.append(
                    f"第 {gi + 1} 个层间空档里有 {len(placed)} 条带标签的边，"
                    f"沿{'竖直' if lr else '水平'}方向排不开"
                    f"（画布{'高' if lr else '宽'}只有 {bound:.0f}px）。"
                    "请缩短这些边标签、减少这一段的边，或把 direction 改成 "
                    f"{'TB' if lr else 'LR'}；再不行就把图拆成两张。")
        for k, cross, moff in placed:
            # 沿 flow 轴钉在空档正中（TB 允许 ±14px 微调避开交叉斜线）：
            # 空档尺寸本来就是按这段标签量出来的，
            # 钉正中才保证两边各留得下 _EDGE_PAD，不会探进邻层节点框。
            mx, my = (mid + moff, cross) if lr else (cross, mid + moff)
            box = ((off, 0.0, off + span, h) if lr
                   else (0.0, off, w, off + span))
            # 白底光晕：标签压在虚线区室框 / 回边连线上时仍可读
            # （2026-11-14 真产物「负反馈」被边框与虚线同时穿过，字糊成一团）。
            # 光晕走 cls="frame"，不计入 mark 数 ⇒ 数据保真门不受影响。
            lab_w = label_em.get(k, 0.0) * EDGE_SIZE * _WIDTH_SAFETY
            if lr:
                lab_w = min(lab_w, max(span - 2 * _LABEL_PAD, 8.0))
            C.rect(mx - (lab_w + 4.0) / 2, my - 5.0, lab_w + 4.0,
                   EDGE_SIZE * _LINE_H + 2.0, _PAPER, cls="frame")
            label_boxes[k] = (mx - (lab_w + 4.0) / 2, my - 5.0,
                              mx + (lab_w + 4.0) / 2, my - 5.0 + EDGE_SIZE * _LINE_H + 2.0)
            C.text(mx, my + EDGE_SIZE * 0.35, edges[k]["label"], size=EDGE_SIZE,
                   fill=_AXIS, box=box)

    if title:
        C.text(w / 2, TITLE_SIZE + 2, title, size=TITLE_SIZE, weight="bold")

    if legend:
        _draw_legend(C, legend, _MARGIN, h - _MARGIN - LEGEND_H)

    desc = (f"荣珂原生 SVG 后端（零第三方依赖）| 机制图 {direction} | "
            f"节点 {len(nodes)} 边 {len(edges)} 区室 {len(gbox)} | "
            "预览级产物，未跑 PDF 六道渲染后审计门")
    svg = C.to_svg(title or "机制图", desc)
    p = Path(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(svg, encoding="utf-8", newline="\n")

    n_marks = len(nodes) + len(edges)      # 节点框 + 连线；箭头是 class="arrow"
    manifest = {
        "schema_version": 1, "backend": "native-svg-diagram",
        "hints": dict(hints or {}),
        "figure": {"width_mm": width_mm, "height_mm": h * 25.4 / 96.0,
                   "width_px": w, "height_px": h, "mm_per_px": width_mm / w,
                   "intrinsic_width_mm": w * 25.4 / 96.0,
                   "placed_width_mm": width_mm,
                   "clamped": False},
        "kind": "diagram", "direction": direction,
        "n_nodes": len(nodes), "n_edges": len(edges), "n_groups": len(gbox),
        "n_marks": n_marks, "n_text": C.n_text,
        "texts": C.text_specs, "marks": [list(b) for b in C.mark_boxes],
        "reversed_for_layout": sorted(L["back"]),
        # 判据要能「自己量」，就不能只给人眼看的 SVG：把节点外框、描边色、
        # 连线折点、区室框都写进清单。craft 审计与测试共用这一份，不各自解析。
        "nodes": [{"id": n["id"], "kind": n["kind"], "label": n["label"],
                   "group": n["group"], "box": [round(v, 2) for v in P(n["id"])],
                   "lines": lines[n["id"]], "layer": L["layer"][n["id"]],
                   "stroke": strokes[n["id"]]} for n in nodes],
        "edges": [{"k": k, "from": e["from"], "to": e["to"],
                   "kind": e["kind"], "label": e["label"],
                   "reversed": k in L["back"],
                   "label_box": [round(v, 2) for v in label_boxes[k]]
                   if k in label_boxes else None,
                   "points": [[round(x, 2), round(y, 2)] for x, y in polys[k]]}
                  for k, e in enumerate(edges)],
        # 区室框按**实际画出的每一块**列，不按区室取并集：并集会把它自己
        # 的多段框（中间隔着别的区室）连成一个套住对方的大框，判据当场误红。
        "compartments": [{"id": b["gid"], "run": b["ri"],
                          "box": [round(v, 2) for v in b["rect"]]}
                         for b in boxes],
        "fit": fit,
    }
    mp = p.with_suffix(".layout.json")
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                  encoding="utf-8", newline="\n")

    readback = p.read_text(encoding="utf-8")
    checks = inspect_svg(p)
    # `inspect_svg` 的 has_text 要求 ≥5 段，那是**数据图**的启发式。
    # 机制图可能只有两三个节点，换成更强的判据：每个节点的标签都必须真上屏。
    checks["has_text"] = C.n_text >= len(nodes)
    checks["readback_nonempty"] = len(readback) > 400
    checks["cjk_preserved"] = "荣珂原生 SVG 后端" in readback
    checks["marks_rendered"] = C.n_mark >= n_marks
    checks["every_node_labelled"] = all(
        any(t["text"] == ls[0] for t in C.text_specs)
        for n in nodes for ls in [lines[n["id"]]] if n["label"].strip())
    ok = all(bool(v) for v in checks.values())
    return {
        "ok": ok, "svg": str(p), "layout": str(mp), "bytes": p.stat().st_size,
        "backend": "native-svg-diagram", "scope": "preview", "kind": "diagram",
        "direction": direction, "n_nodes": len(nodes), "n_edges": len(edges),
        "n_groups": len(gbox), "n_marks": C.n_mark, "n_text": C.n_text,
        "width_px": w, "height_px": h, "width_mm": width_mm,
        # 画布 = 内容自然尺寸（2026-11-14 起不再撑到声明版面宽）；两者分开报，
        # 判据与 AI 都能自己算「这张图 1:1 是多大、摆进版面缩到几 pt」。
        "natural_w": round(L["natural_w"], 1), "natural_h": round(L["natural_h"], 1),
        "font_stack": FONT_STACK, "checks": checks, "warnings": warnings,
        "fit": fit,
        "reversed_for_layout": sorted(L["back"]),
        "audit_scope": ("未跑 PDF 六道渲染后审计门（那是投稿路线，审的是 PDF）。"
                        "本产物是预览级矢量机制图，质量由 SVG 七道门量。"),
        "publish_note": ('投稿级请改装 pip install "rongke[figure]" 后走 '
                         "plot_data + audit_figure；本产物只作预览 / 网页 / 插图。"),
        "note": "" if ok else "结构自检有未通过项（见 checks）—— 文件保留在盘上供核查，不算成功。",
    }