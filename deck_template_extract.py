# -*- coding: utf-8 -*-
"""把一份**品牌模板**抽成"页型 + 槽位表"（可重跑）。

## 为什么要有它

品牌模板的价值是它的**全部装饰**，所以只能"在模板页上填字"，不能重绘。
而"哪一页是哪一页型、每页有哪些可填的槽、每个槽能装多少字"这三件事，
**手写会烂**（17 页 × 平均 8 个槽 ≈ 120 个条目，改一次模板就全废）。

⇒ 拆成两半：**页型 → 源页号**手工写（人审，那是判断）；
**槽位 + 容量**全部**自动抽**（那是事实，可以从几何与字号算出来）。

## ★ 一份模板一份 `kinds.json`（2026-09-22 通用化）

原先"页型 → 源页号"这 14 行**写死在代码里**，于是第二份模板（莫干山校区，
**72 页 / 54 种版式**）根本没有落点 —— 要么改代码，要么复制一份抽取器（H 型）。
现在那份判断住在 `docs/deck-templates/<名>/kinds.json` 里，与槽位表同居：

    docs/deck-templates/<名>/kinds.json   ← **判断**（人写：页型 → 源页号 + 该模板的文案线索）
    docs/deck-templates/<名>/slots.json   ← **事实**（本脚本抽：几何 + 容量 + 资产）

不传 `--kinds` 时退回内置的 `ZJUT_KINDS`（浙工大 17 页那套），
**输出与从前一字不差** —— 老模板的槽位表不需要因为这次通用化而重抽。

## ★ 容量是"算出来"的，不是"标出来"的 —— 且公式**只有一份**

模型住在 `rongke/slotfit.py`（本文件 `import` 它，`rongke/deck.py` 也 `import` 它，
填充子脚本还是 `import` 它）。**不许再抄第二份。**

    cpl = (W_in - lIns - rIns) / pt      ← **em**，不是"字"（英文只占 ~0.56em）
    lines = (H_in - tIns - bIns) / (pt * 1.2)
    lines = max(lines, 原文需要的行数)     ← 作者原文 = 存在性证明

⚠️ 2026-09-22 之前这条链上有**三份互相抄的公式**（本文件 / `deck.py` / 填充子脚本），
   而且**四条事实全是猜的**：整段按 1em/字、内边距固定 0.2in、每行容量取整、
   行数不看作者写了多少。后果实测：莫干山模板 **703 槽里 156 槽（22%）连模板
   自己的原文都判"装不下"**（`Contents` / `2018.6` / `朱廷恒老师`）——
   一条会在模板自己身上报错的判据（设计定律 10）。计量过程见 `rongke/slotfit.py`。

## ⛔→✅ 已修：容量算在**未取整**的几何上（2026-09-22）

**症状**：表里告诉模型的字数上限**比执行体实际装得下的多**，于是
「照着工具的建议写，反而被工具判失败」。实测老槽位表 **177 个可比槽里 5 处**
对不上：`chart i=16/17/18`（99/132/77 写成 90/120/70）、
`content-2item i=3/6`（136 写成 119）。

**根因**：算 `cap_*` 用的是**未取整**的 `w_in/h_in`，而存进表里的却是
`round(...,2)` 之后的几何；下游 `deck.py::_cap` 按**表里的几何**重算
⇒ `int()` 正好落在浮点边界上，被 floor 掉一位。

**修法**（一行级，但必须两处同时）：先把几何取整成**要存进去的那个值**，
再用它算容量。⇒ 「表里的数」与「执行体算的数」**由构造保证相等**，
而不是靠"两边都记得用同一个精度"这种约定。

**守卫**：`verify_capacity()` 在抽取末尾**按存储几何逐槽重算**，
对不上就**非零退出**（这不是警告 —— 表与执行体分家就是这张表的全部意义所在）。
`template_test.py` §3b 另有同一判据的常驻版本，守着随库的那份 `slots.json`。

**另一条常驻守卫**：抽取末尾还会断言「**模板自己写的原文，装得下**」。
它是上一条的**上界**——存储几何重算只证明"表与执行体一致"，
证明不了"这个数字对"；而作者原文能放下，是"数字没算小"的存在性证明。

用法：

    python deck_template_extract.py <模板.pptx> -o docs/deck-templates/<名>/slots.json
    python deck_template_extract.py <模板.pptx> -o …/slots.json --kinds …/kinds.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ★ 容量模型只有一份，住在 `rongke/slotfit.py`（零依赖模块，谁都能 import）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rongke.slotfit import (    # noqa: E402
    INSET_DEF_EMU, needed_lines, slot_capacity, as_paras,
)
# ★ **字号继承链也只有一份**，住在 `rongke/deck.py::PhChain`（同样是零依赖模块，只在
#   `zipfile` 上工作）。抽取器不许自己再解析一遍 master/layout 的 `txStyles` ——
#   2026-09-22 实测：不接它的时候，表对 64 个槽承诺过头（见 `_inherited_pt`）。
from rongke.deck import PhChain          # noqa: E402

EMU_IN = 914400

#: 视为"装饰锚点"的原文（保留原样，不可填）
ANCHOR_ROLES = ("anchor", "fixed", "empty")


def flat_shapes(shapes):
    """**深度优先摊平**：组合（`p:grpSp`）里的子形状也要拿出来。

    ★ 为什么必须有它（2026-09-22 量出来的）：`SlideShapes`（`slide.shapes`）
      **只给顶层**。实测这份模板有 **14 处文字住在组合里** ——
      其中 **8 处是四个章节页上各两个「请输入文本」**。抽取器看不见它们
      ⇒ 渲染出来就是页面上**明晃晃两个「请输入文本」**（我第一版当真了、
      还当"次要"放过，是覆盖率探针把它量出来的）。

    ⚠️ **顺序必须与 `rongke/deck.py` 的 `_TEMPLATE_SCRIPT._flat` 完全一致** ——
      两边的槽位索引就是**这个 DFS 序号**。两边都用 python-pptx 的
      `shapes` 迭代，所以顺序由同一个库保证；`template_test` 里有一条
      **带组合的合成模板**端到端钉住它（正控：组合里的字必须落在同一个索引上）。
    """
    out = []
    for sh in shapes:
        out.append(sh)
        sub = getattr(sh, "shapes", None)
        if sub is not None:
            try:
                out.extend(flat_shapes(sub))
            except Exception:
                pass            # 拿不到子形状不是错，只是这一层没有
    return out


def _inherited_pt(chain, src: int, sh, tf) -> float | None:
    """槽里的 run 没写 `sz` 时，问**继承链**要有效字号（单源：`rongke/deck.py::PhChain`）。

    ★ 为什么必须问链、不能退回文稿缺省值（2026-09-22 修，实测）：
      旧写法是 `pt = default_pt(prs)` —— `p:defaultTextStyle` 的 **18pt**。
      它**系统性低估**：moganshan **64 槽** / zjut **18 槽**的真实继承字号更大
      （18pt → 24 / 28pt），而且**全部往"承诺得更多"的方向错**。
      下游的门（`deck._sp_overflow`）走的是真继承链 ⇒
      **表承诺 30 字、真值只装得下约 9 字，模型照表写、反被门判超限** ——
      正是本文件开头那条"容量存在的唯一理由"要防的事。

    ⚠️ `src` 必须是**分片号**（部件名里的页码）：`PhChain.layout_of` 是**按部件名**建索引的
      （同一个坑当天在 `deck.probe_pptx` 里也踩过一次，两侧都是"拿甲页的 layout 解析乙页"）。
    判不了就返回 `None`（调用方退回缺省值）—— 宁可不出数，不出假数。
    """
    if chain is None:
        return None
    from pptx.oxml.ns import qn
    best: float | None = None
    try:
        for p in tf.paragraphs:
            lvl = 1
            ppr = p._p.find(qn("a:pPr"))
            if ppr is not None and ppr.get("lvl"):
                try:
                    lvl = int(ppr.get("lvl")) + 1     # OOXML 的 `lvl` 从 0 起
                except (TypeError, ValueError):
                    lvl = 1
            got = chain.pt_of(src, sh._element, lvl)
            if got and (best is None or got > best):
                best = got
    except Exception:
        return None
    return best


#: 内置的**浙工大 17 页**那套：**页型 → 源页号**（手工写的 14 行 = 判断，不是事实）。
#: 第二份模板起就不该改这里 —— 把判断写进 `docs/deck-templates/<名>/kinds.json`。
ZJUT_KINDS: list[tuple[str, str, list[int]]] = [
    ("cover", "封面", [1]),
    ("toc", "目录", [2]),
    ("section", "章节页", [3, 8, 11, 14]),
    ("chart", "图表页", [4]),
    ("content-2blk", "两栏块+图", [5]),
    ("content-quad", "四宫格", [6]),
    ("content-compare", "对比两栏", [7]),
    ("content-4col", "四栏+四段", [9]),
    ("content-ring", "圆环四段", [10]),
    ("content-5grid", "五宫格", [12]),
    ("content-2item", "两段编号", [13]),
    ("content-timeline", "时间轴（SmartArt）", [15]),
    ("content-3col", "三列要点", [16]),
    ("closing", "结尾页", [17]),
]

#: SmartArt 的**文字不在 slide 里** —— 它在 `ppt/diagrams/dataN.xml`。
#: 只读 slide 文字框的抽取器会给这种页寥寥几个槽，然后把三十来段「请输入文本」
#: 原样交出去 —— 正是「看起来能填、其实填不到」。所以这里单独扫一遍部件。
DIAGRAM_DATA_REL = ("http://schemas.openxmlformats.org/officeDocument/"
                    "2006/relationships/diagramData")


#: 模板**自带、而我们够不着**的非文字资产。
#:
#: ★ 为什么要量它（2026-09-22）：`deck_from_template` **只填字**。实测这份模板带着
#:   **40 张图片 / 1 个原生图表 / 0 表格 / 1 个 SmartArt / 1 个图片占位符**。
#:   装饰照片换不了无所谓；但 **p4 那个甜甜圈图是"销售额 / 第一二季度 / 20 : 80"
#:   的示例数据** —— 一旦这一页被用上，就会交出一页**带着假数字的图**，
#:   而文字槽还填得像模像样。**假数字进组会汇报，比少一张图严重得多。**
#:   ⇒ 必须把它**说出口**（拦沉默），而不是让模型自己去猜。
ASSET_NOTE = {
    "chart": "自带**示例图表**（数据在 xlsx 里，换不了）—— 用了它就会交出假数字",
    "table": "自带表格（内容换不了）",
    "smartart": "自带 SmartArt（文字**可以**填，见 `diagram_slots`）",
    "pic": "自带图片（换不了）",
}


def page_assets(zf, slide_no: int) -> dict:
    """清点这一页的非文字资产（按 XML 标签直数，只求"有没有、几个"）。"""
    try:
        x = zf.read(f"ppt/slides/slide{slide_no}.xml").decode("utf-8")
    except KeyError:
        return {}
    return {
        "pics": len(_re.findall(r"<p:pic>", x)),
        "charts": len(_re.findall(r'graphicData uri="[^"]*chart', x)),
        "tables": len(_re.findall(r"<a:tbl>", x)),
        "smartart": len(_re.findall(r'graphicData uri="[^"]*diagram', x)),
        "pic_ph": len(_re.findall(r'<p:ph[^/]*type="pic"', x)),
    }


def diagram_parts(zf, slide_no: int) -> list[str]:
    """这份 slide 挂着的 SmartArt **数据**部件名（`ppt/diagrams/data1.xml`）。"""
    import posixpath
    rels = f"ppt/slides/_rels/slide{slide_no}.xml.rels"
    try:
        x = zf.read(rels).decode("utf-8")
    except KeyError:
        return []
    out = []
    for m in _re.finditer(r"<Relationship [^>]*/>", x):
        tag = m.group(0)
        if f'Type="{DIAGRAM_DATA_REL}"' not in tag:
            continue
        tgt = _re.search(r'Target="([^"]+)"', tag)
        if tgt:
            out.append(posixpath.normpath(posixpath.join("ppt/slides",
                                                         tgt.group(1))))
    return out


def diagram_slots_from_parts(pairs) -> list[dict]:
    """把 `[(部件名, XML 文本)]` 编成 SmartArt 槽位 —— **编号规则的唯一实现处**。

    为什么要单拎出来（2026-09-22）：定位 SmartArt 部件有两条路，但**编号必须只有一份**，
    否则又是一处 H 型。

      · **模板侧** `diagram_slots(zf, slide_no)`：按 `ppt/slides/_rels/slideN.xml.rels`
        + zip 成员名解析 —— 只对**原封不动的模板**成立（那时源页号 = 成员名）。
      · **产物侧**（`deck_template_demo.py` 的读回复测）：按**部件自己的关系**
        （`reltype` 以 `/diagramData` 结尾）解析。**不能**用
        `slides[i].part.partname` 去拼成员名 —— 产物上它报的是顺序号 `slide1..slideN`，
        而 zip 成员名是**模板源页号**（实测 12 页产物：成员为
        slide1/2/3/4/7/13/16/29/42/46/59/61）。
        **根因**：`Presentation.slides` 是**带副作用的 `@lazyproperty`** ——
        光读一下就调 `PresentationPart.rename_slide_parts()`，把分片名**就地**改成
        `slide1..N`（**不是 `save()` 干的**）。见 `rongke/deck.py` 里那段血债与
        `_slides_by_rid()`。

    ⚠️ 容量给 `None`：SmartArt 的排版由 PowerPoint 自己算（字多了它自己缩），
    我们**量不出**「装几个字」—— 那就如实说量不出，而不是编一个数。
    """
    slots: list[dict] = []
    # ★ 槽键 `d<i>` 的序号必须**跨部件连续**（2026-09-22 修）。
    #   旧写法 `for i, m in enumerate(...)` 让**每个部件**都从 `d0` 重新开始
    #   ⇒ 一页挂两个 SmartArt 时键当场撞车：后一个部件的槽被前一个覆盖，
    #     清单上槽数看着是够的，**产物里却原样留着「请输入文本」**。
    #   这是"能填、其实没填"里最难看见的一种 —— 量的维度与漏的维度不同源。
    #   ⚠️ 但 `part_index` 必须**保持按部件计**：填充端靠 `(part, part_index)`
    #     去定位"改哪个部件的第几个 `<a:t>`"。键是全局的，定位是局部的。
    #   序号**包含空 `<a:t>`**（空的不出槽、但计数照走）—— 这样单部件页的
    #   编号与历史逐位一致，存量槽位表不需要重抽。
    gi = 0
    for part, x in pairs:
        for pi, m in enumerate(_re.finditer(r"<a:t>([^<]*)</a:t>", x)):
            i, gi = gi, gi + 1
            txt = m.group(1)
            if not txt.strip():
                continue
            slots.append({
                "i": f"d{i}", "name": f"{part}#{pi}", "x": None, "y": None,
                "w": None, "h": None, "pt": None, "pt_inherited": True,
                "role": "diagram", "orig_chars": len(txt),
                "cap_chars": None, "cap_cpl": None, "cap_lines": None,
                "sample": txt, "part": part, "part_index": pi,
            })
    return slots


def diagram_slots(zf, slide_no: int) -> list[dict]:
    """（**模板侧**）按源页号解析 SmartArt 部件，再交给编号的唯一实现。

    只对原封不动的模板成立：它拿 `slide_no` 直接当**成员名里的页码**用。
    """
    pairs = []
    for part in diagram_parts(zf, slide_no):
        try:
            pairs.append((part, zf.read(part).decode("utf-8")))
        except KeyError:
            continue
    return diagram_slots_from_parts(pairs)

#: 反推槽位语义角色的**文案线索**。它们与"页型 → 源页号"一样是**判断**，
#: 因此也随模板走（`kinds.json` 的 `rules`）—— 换一所学校改这两个字符串表就够了，
#: 不必动代码。
DEFAULT_RULES: dict = {
    #: 校训（装饰，保留原样）
    "motto": ["厚德健行", "取精用弘"],
    #: 校名 —— ⚠️ **不能一概当固定件**：封面标题恰好也叫「浙江工业大学…」
    #:    区分靠字号：右上角那枚校徽上的校名是小字（< 20pt），封面标题是 45pt。
    "school": "浙江工业大学",
    #: ★★ 含校名 **不等于** 固定件。实测两处误报（2026-09-22，两份模板各中一条）：
    #:   · **长段正文**里提到校名（莫干山「一段无图式」的 189 字校园介绍）——
    #:     判成固定件 ⇒ **模型碰不到它** ⇒ 产物原样交出模板自带的介绍文字。
    #:   · **模板自称**（浙工大页脚「浙江工业大学-通用PPT模板」）——
    #:     判成固定件 ⇒ **产物印着模板自己的产品名出厂**。
    #:   ⇒ 校徽那种「校名徽标」是**短的**（几个字），长的一律是正文。
    "school_pt_max": 20.0,
    "school_len_max": 24,
    #: ★ 模板自称的指纹：任何写着"模板/模版/template"的槽**一律可填** ——
    #:   交出去的稿子上印着「XX通用PPT模板」是错的，它必须被作者的内容替掉。
    "self_name_hints": ["模板", "模版", "template", "Template"],
    #: 明显的占位文案 —— 用来反推槽位的语义角色
    "title_hints": ["请输入标题", "请输入具", "标题一", "标题二", "具体内容"],
    "body_hints": ["请输入文本", "请输入内容", "这里请放入", "请输入具体内容"],
}
#: 纯编号形态（`01-` / `03` / `4`）—— 装饰性编号，保留原样
import re as _re
_NUM_ONLY = _re.compile(r"^\d{1,2}\s*[-.．、]?$")
#: **纯标点**（大引号 `“` / 破折号 / 省略号 / 项目符号）—— 装饰，保留原样。
#:
#: ★★ 为什么要正面定义"什么是锚点"（2026-09-22）：
#:   旧规则里有一条 `容量 ≤ 2 ⇒ 锚点`。它想说的是"那些压根不是槽的装饰件"，
#:   实测它抓对了 6 个大引号（119.5pt 的 `“`，容量 1 字），
#:   但同时**冻住了 4 个真槽** —— 最刺眼的是封面上的
#:   **「展示人 / 袁荣」**：那个名字框只有 0.70in 宽、18pt，
#:   算出来的容量**恰好 2 字** ⇒ 判成装饰 ⇒ 不可填
#:   ⇒ **产物会印着上一位作者的名字出厂**（和"页脚印着模板自己的名字"同一类静默泄漏）。
#:   ⇒ 改成正面定义：**锚点 = 编号 / 巨型数字 / 纯标点**，其余一律可填。
#:   这条判据的口径是**"拦沉默，不拦选择"**：冻错一个槽是静默的（交出别人的名字），
#:   多放一个槽是可修的（工具会要求填、模型会写）。
_PUNCT_ONLY = _re.compile(r"^[\s\u3000-\u303f\u2010-\u2027\u2030-\u205e"
                          r"\uff01-\uff65·•…]+$")


def slot_role(text: str, pt: float | None, y_in: float, h_in: float,
              page_h_in: float, cap: int | None, nchar: int,
              rules: dict | None = None) -> str:
    """给一个文字形状定"语义角色"。

    ⚠️ 角色**只做提示**（给人看懂、也给模型提示），**不参与判定** ——
    判定只看容量。所以即使角色判得不完美，也不会造成静默错误；
    而"填错槽"由 `sample`（原文样例）兜住。
    ⚠️ `cap` 仍收着，但**不再用来判锚点**（见 `_PUNCT_ONLY` 上面的血债）。
    """
    r = rules or DEFAULT_RULES
    t = text.replace("\n", "").strip()
    if not t:
        return "empty"
    # 校训 / 校徽上的校名（小字）⇒ 固定件
    if any(k in t for k in (r.get("motto") or ()) if k):
        return "fixed"
    # ★★ 含校名 ⇒ **还要满足两个条件**才算固定件（否则是长正文 / 模板自称）：
    #   · 短（≤ `school_len_max`）—— 校徽上的校名是几个字，正文是几百字；
    #   · 不自称是模板 —— 「浙江工业大学-通用PPT模板」是**模板的产品名**，
    #     它必须可填，否则产物会印着模板自己的名字出厂。
    _school = str(r.get("school") or "")
    _selfn = tuple(k for k in (r.get("self_name_hints") or ()) if k)
    if (_school and _school in t
            and (pt or 0) < float(r.get("school_pt_max", 20.0))
            and len(t) <= int(r.get("school_len_max", 24))
            and not any(k in t for k in _selfn)):
        return "fixed"
    # 锚点：装饰编号 / 巨型数字 / 纯标点（**不按容量判**）
    if (t.isdigit() and (pt or 0) >= 100) or _NUM_ONLY.match(t) \
            or _PUNCT_ONLY.match(t):
        return "anchor"
    if y_in + h_in / 2 >= page_h_in * 0.92:
        return "footer"
    if any(k in t for k in (r.get("body_hints") or ())) or nchar > 40:
        return "body"
    if any(k in t for k in (r.get("title_hints") or ())):
        return "heading"
    return "label"


def load_kinds(path: Path) -> tuple[list[tuple[str, str, list[int]]], dict, str]:
    """读一份 `kinds.json` ⇒ (页型表, 文案线索, 模板名)。

    格式（`_note` 之类的下划线键一律忽略，它们是给人看的）：

        {"name": "moganshan",
         "rules": {"motto": [...], "school": "...", ...},   # 可选，缺的用 DEFAULT_RULES
         "kinds": [{"kind": "封面", "label": "封面", "srcs": [1]}, ...]}

    ⚠️ **读不动就抛**，不许悄悄退回内置那套 —— 静默退回会让"我以为在抽新模板，
      实际抽的是浙工大"这种事故一路走到产出。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    kinds: list[tuple[str, str, list[int]]] = []
    for e in raw.get("kinds") or []:
        kind, srcs = str(e.get("kind") or "").strip(), list(e.get("srcs") or [])
        if not kind or not srcs:
            raise ValueError(f"kinds.json 里这一条不完整：{e!r}")
        kinds.append((kind, str(e.get("label") or kind), [int(x) for x in srcs]))
    if not kinds:
        raise ValueError(f"{path} 里一条页型都没有。")
    rules = dict(DEFAULT_RULES)
    rules.update({k: v for k, v in (raw.get("rules") or {}).items()
                  if not str(k).startswith("_")})
    return kinds, rules, str(raw.get("name") or Path(path).parent.name)


def default_pt(prs) -> float | None:
    """这份演示文稿的**缺省字号**（`p:defaultTextStyle` lvl1 的 `defRPr/@sz`）。

    ★ 为什么必须有它：模板里大量文本框的 run **不写 sz**（`<a:rPr …>` 里没有），
    看着像"字号读不到"。但"读不到"是**查错了层** —— 真身在
    `ppt/presentation.xml` 的 `defaultTextStyle`（实测本模板 = **18pt**）。
    ⇒ 拿不到它就等于**算不出容量**，而算不出容量就会让"填长了溢出"重新变成
      静默失败（本项目第七个缺口的同一个病）。
    """
    from pptx.oxml.ns import qn
    el = prs.part._element.find(qn("p:defaultTextStyle"))
    if el is None:
        return None
    lvl = el.find(qn("a:lvl1pPr"))
    dr = lvl.find(qn("a:defRPr")) if lvl is not None else None
    if dr is None or not dr.get("sz"):
        return None
    return int(dr.get("sz")) / 100.0


def _bodyPr(tf):
    """这只文字框的 `a:bodyPr` 元素（`wrap` / `vert` 这些属性只住在这里）。"""
    from pptx.oxml.ns import qn
    return tf._txBody.find(qn("a:bodyPr"))


def margins_emu(tf) -> tuple[int, int, int, int]:
    """真实内边距 (左, 右, 上, 下)，单位 **EMU 整数**（文件里就是这个单位）。

    ★ 为什么"读"而不是"假设 0.2in"：PowerPoint 的默认确是 0.1+0.1in，
      但**形状可以覆盖**（大量图形写 `lIns="0" rIns="0"`）⇒ 对那类框，
      假设 0.2in 会白扣掉 0.2in 可用宽度，而 0.2in 在 12pt 下就是 1.2 个字。
      python-pptx 的 `margin_*` 属性已经带正确的缺省值，直接用。

    ⚠️ **别在这里换成英寸再存**：0.1in 换成 4 位小数会漂，
      而"表里的数 == 执行体算的数"要求逐位相等（实测漂出 42 处假告警）。
    """
    return (int(tf.margin_left), int(tf.margin_right),
            int(tf.margin_top), int(tf.margin_bottom))


def wrap_of(tf) -> str:
    """`bodyPr/@wrap`：`"square"`（默认，换行）/ `"none"`（不换行）。"""
    bp = _bodyPr(tf)
    return (bp.get("wrap") or "square") if bp is not None else "square"


def vert_of(tf) -> str:
    """`bodyPr/@vert`：空串 = 横排；`"eaVert"` 等 = 竖排（行列互换）。"""
    bp = _bodyPr(tf)
    return (bp.get("vert") or "") if bp is not None else ""


def extract(path: Path, kinds: list[tuple[str, str, list[int]]] | None = None,
            rules: dict | None = None) -> dict:
    """抽一份槽位表。`kinds` 为空 ⇒ 用内置的浙工大那套（**与从前一字不差**）。"""
    import zipfile
    from pptx import Presentation  # 只有这条路需要 python-pptx

    kinds = kinds or ZJUT_KINDS
    rules = rules or DEFAULT_RULES
    # 另开一份只读包，专门扫 SmartArt 部件（python-pptx 不给这个口子）
    zf = zipfile.ZipFile(str(path))
    prs = Presentation(str(path))
    page_h_in = prs.slide_height / EMU_IN
    dflt = default_pt(prs)
    # 字号继承链：**建一次、逐页复用**（与门同一份实现）。建不起来 ⇒ `None`，
    # 调用方退回 `dflt`，但 `pt_inherited` 仍如实标着（不静默、不假装知道）。
    try:
        chain = PhChain(zf)
    except Exception:
        chain = None
    out: dict = {
        "source": str(path),
        "slide_size": [prs.slide_width, prs.slide_height],
        "aspect": round(prs.slide_width / prs.slide_height, 4),
        "default_pt": dflt,
        "pages": {},
    }

    for kind, label, srcs in kinds:
        entries = []
        for n, src in enumerate(srcs, 1):
            s = prs.slides[src - 1]
            shapes = flat_shapes(s.shapes)      # ★ 含组合里的子形状
            slots = []
            for i, sh in enumerate(shapes):
                if not sh.has_text_frame:
                    continue
                tf = sh.text_frame
                txt = tf.text
                if not txt.strip():
                    continue
                if None in (sh.left, sh.top, sh.width, sh.height):
                    continue
                pts = [r.font.size.pt for p in tf.paragraphs
                       for r in p.runs if r.font.size]
                inherited = not pts
                if pts:
                    pt = max(pts)
                else:
                    # ★ 问**继承链**，别退回整个文稿的缺省值（血债见 `_inherited_pt`）。
                    pt = _inherited_pt(chain, src, sh, tf) or dflt
                # ⛔ 容量必须算在**要存进去的那个几何**上。
                #   旧版用未取整的 w_in/h_in 算 cap、却存 round(...,2) 的几何
                #   ⇒ 下游按表里的几何重算就少一位（实测 177 槽里 5 处），
                #   方向还是危险的那一侧：**表里承诺的比装得下的多**。
                #   先取整、再算 ⇒ 「表里的数 == 执行体算的数」由构造保证。
                w_in = round(sh.width / EMU_IN, 2)
                h_in = round(sh.height / EMU_IN, 2)
                y_in = sh.top / EMU_IN
                nchar = len(txt.replace("\n", ""))
                # ── 容量 ────────────────────────────────────────────────
                # 五个输入**全是读出来的**：要存进去的几何（见下面那条血债）、
                # 真实内边距、换行方式、竖排、以及**作者原文需要几行**。
                # 公式本身住在 `rongke/slotfit.py`，这里不许抄。
                ins = margins_emu(tf)
                vrt = vert_of(tf)
                wrap = wrap_of(tf)
                kw = dict(ins_emu=ins, vert=vrt, wrap=wrap)
                # 先只求"每行 em 容量"，用它量出作者原文需要几行（存在性证明）
                _c0, _, _ = slot_capacity(w_in, h_in, pt, **kw)
                min_lines = needed_lines(as_paras(txt), _c0)
                cpl, lines, cap = slot_capacity(w_in, h_in, pt, **kw,
                                                min_lines=min_lines)
                role = slot_role(txt, pt, y_in, h_in, page_h_in, cap, nchar, rules)
                slots.append({
                    "i": i, "name": sh.name,
                    "x": round(sh.left / EMU_IN, 2), "y": round(y_in, 2),
                    "w": w_in, "h": h_in,
                    "pt": pt, "pt_inherited": inherited, "role": role,
                    "orig_chars": nchar,
                    "ins_emu": list(ins), "wrap": wrap, "vert": vrt,
                    "min_lines": min_lines,
                    "cap_chars": (int(cap) if cap is not None else None),
                    "cap_cpl": cpl, "cap_lines": lines,
                    "sample": txt.replace("\n", "⏎")[:40],
                })
            # 每页把**字号最大的可填槽**提为 `heading`（确定性规则，可复核）。
            # 单靠原文关键词判"谁是标题"会漏（封面标题叫「浙江工业大学…」）。
            fillable = [s for s in slots
                        if s["role"] not in ("anchor", "fixed", "empty")]
            # SmartArt：文字**不在 slide 里**，得单独扫部件（`diagram_slots`）。
            # 不补这一步的后果是"这一页看着只有 3 个槽，其余三十来段占位文本
            # 原样交出去"，而渲染出来才发现 —— 典型的"能填、其实没填"。
            slots += diagram_slots(zf, src)
            fillable = [s for s in slots
                        if s["role"] not in ("anchor", "fixed", "empty")]
            # 提 `heading` 时要**排除 diagram**（它的 pt 是 None，会把 max 抢走）
            sized = [s for s in fillable if s["role"] != "diagram"]
            if sized:
                top = max(sized, key=lambda s: (s["pt"] or 0))
                if top["role"] != "footer":
                    top["role"] = "heading"
            entries.append({"id": kind if len(srcs) == 1 else f"{kind}-{n}",
                            "src": src, "assets": page_assets(zf, src),
                            "slots": slots})
        out["pages"][kind] = {"label": label, "entries": entries}
    return out


def verify_capacity(data: dict) -> tuple[int, list[str]]:
    """按**存储的几何**逐槽重算容量，与表里写的比。返回 (可比槽数, 对不上的描述)。

    ★ 为什么这条判据必须常驻：这张表的**全部意义**就是"把执行体会怎么算
      提前告诉模型"。表与执行体分家 ⇒ 模型照着表写、工具说装不下
      ⇒ **照工具的建议做反而被工具判失败**（本项目最贵的那类失败）。
      ⚠️ 它不是"风格检查"：对不上就**非零退出**，因为一旦不成立，
        整张表的每一个数字都不可信。

    ⚠️ 分母要分开报：SmartArt 槽量不出容量（`cap_chars is None`），
      **那不算"不比"** —— 它从来不进分母。见 `deck.slot_capacity`。
    """
    bad: list[str] = []
    n = 0
    for kind, blk in (data.get("pages") or {}).items():
        for e in blk.get("entries") or []:
            for s in e.get("slots") or []:
                if s.get("role") == "diagram" or s.get("cap_chars") is None:
                    continue
                n += 1
                ins = s.get("ins_emu") or list(INSET_DEF_EMU)
                cpl, lines, cap = slot_capacity(
                    s["w"], s["h"], s["pt"], ins_emu=tuple(ins),
                    wrap=s.get("wrap") or "square", vert=s.get("vert") or "",
                    min_lines=s.get("min_lines") or 0)
                if (cpl, lines, int(cap)) != (s["cap_cpl"], s["cap_lines"],
                                              s["cap_chars"]):
                    bad.append(f"{e.get('id')} 槽 {s['i']}：表里 "
                               f"{s['cap_cpl']}×{s['cap_lines']}={s['cap_chars']}，"
                               f"按存储几何重算是 "
                               f"{cpl}×{lines}={int(cap)}")
    return n, bad


def verify_orig_fits(data: dict, reader=None) -> tuple[int, list[str]]:
    """**作者原文装得下吗？** 返回 (可比槽数, 装不下的描述)。

    ★ 这是 `verify_capacity()` 的**上界**，两条缺一不可：
      · `verify_capacity` 只证明"表里的数 == 按存储几何重算的数" —— 两边可以
        **一起错**（旧版就一起错了：整段按 1em/字 ⇒ 英文栏位全判小）；
      · 本条证明"**这个数字没有算小**"：模板作者在这只框里放下了 N 个 em
        且渲得出 ⇒ 容量 < N 就是错的。**这是存在性证明，不是风格偏好。**

    ⚠️ 只对**非装饰槽**要求。装饰锚点（大引号 `“`、巨型编号 `01`）本来就是
      贴着边放的艺术字，它们超框是设计意图，不是在证明容量。
      这条口径与 `slot_role` 的锚点正面定义同源：**拦沉默，不拦选择**。
    """
    bad: list[str] = []
    n = 0
    for kind, blk in (data.get("pages") or {}).items():
        for e in blk.get("entries") or []:
            for s in e.get("slots") or []:
                if s.get("role") in ("diagram", "anchor", "empty"):
                    continue
                if s.get("cap_cpl") is None:
                    continue
                n += 1
                # 原文只存了 40 字（模型看的），所以这里用**抽出来的摘要**判：
                # 采样被截断时只可能判"需要更少行"，所以它偏宽松、不会造成误报。
                sample = (s.get("sample") or "").replace("⏎", "\n")
                if needed_lines(as_paras(sample),
                                s["cap_cpl"]) > s["cap_lines"]:
                    bad.append(f"{e.get('id')} 槽 {s['i']}（{s.get('name')}）："
                               f"上限 {s['cap_cpl']:.2f}em/行 × {s['cap_lines']}行，"
                               f"但原文 {sample!r} 就要更多行")
    return n, bad


def verify_cap_geometry(data: dict) -> tuple[int, list[str]]:
    """**几何自洽**：`cap_cpl × pt ÷ 72` 必须等于「去掉内边距后的可用宽度」。

    ★★ 这条为什么必须存在（2026-09-22 实测踩出来的）：
      `verify_capacity` 是"**用同一个函数再算一遍**" —— 函数本身错了它会跟着错，
      两条自检会**一起绿**。而 `verify_orig_fits`（原文装得下）在"上限被算大"时
      **反而更容易满足**，也绿。实测 `bodyPr/@vert="horz"` 被误当竖排（行列互换）：
      标题格容量从 30 字抬到 47 字（**危险方向：承诺的比装得下的多**），
      而 ①② **全绿** ⇒ 只有这一条会当场报出来。

      ⇒ 它的算式里**没有 `slot_capacity`**：只问「这个每行容量，配得上表里那只框吗」，
        而且**横排、竖排两种读法里必须至少有一种成立**（顺带把 `vert` 的方向钉住）。
        —— 自检只证"一致"，证不了"对"；**要不一致地证，就得换一把尺子。**

    ⚠️ `EM_FLOOR` 兜底的槽（框窄到 0.05em 以下）不参与：那种槽的数是**刻意**
      取得下限，本来就不该等于几何值。
    """
    from rongke.slotfit import EM_FLOOR
    bad: list[str] = []
    n = 0
    for kind, blk in (data.get("pages") or {}).items():
        for e in blk.get("entries") or []:
            for s in e.get("slots") or []:
                cpl = s.get("cap_cpl")
                if cpl is None or abs(cpl - EM_FLOOR) < 1e-12 or not s.get("pt"):
                    continue
                n += 1
                il, ir, it, ib = [v / EMU_IN for v in
                                  (s.get("ins_emu") or list(INSET_DEF_EMU))]
                got = cpl * s["pt"] / 72.0
                cands = (s["w"] - il - ir, s["h"] - it - ib)
                if not any(abs(got - c) <= 1e-6 for c in cands):
                    bad.append(f"{e.get('id')} 槽 {s['i']}（{s.get('name')}）："
                               f"每行容量 {cpl:.4f}em × {s['pt']}pt = "
                               f"{got:.4f}in 可用宽，但表里那只框横排是 "
                               f"{cands[0]:.4f}in、竖排是 {cands[1]:.4f}in "
                               f"（vert={s.get('vert')!r}）")
    return n, bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--kinds", default="",
                    help="页型表（docs/deck-templates/<名>/kinds.json）；"
                         "不传则用内置的浙工大那套")
    a = ap.parse_args()
    kinds = rules = None
    tname = ""
    if a.kinds:
        kinds, rules, tname = load_kinds(Path(a.kinds))
    data = extract(Path(a.template), kinds, rules)
    if tname:
        data["template_name"] = tname
    # ★ 出生即自检 ①：表里的容量必须与"按存储几何重算"逐槽相等（见 `verify_capacity`）
    n_ok, bad = verify_capacity(data)
    if bad:
        print(f"[FAIL] 容量自检不过：{n_ok} 个可比槽里有 {len(bad)} 处与执行体分家")
        for x in bad[:12]:
            print("   ·", x)
        print("   ⇒ 表里承诺的会比实际装得下的多（或反之），**这张表不可用**。")
        return 3
    # ★ 出生即自检 ②：**模板自己的原文必须装得下**（存在性证明，见 `verify_orig_fits`）。
    #   ① 只证明"两边一致"，证明不了"这个数对"；② 才证明"没算小"。
    n_fit, unfit = verify_orig_fits(data)
    if unfit:
        print(f"[FAIL] 「原文装得下」不成立：{n_fit} 个可比槽里有 {len(unfit)} 处"
              f"—— 判据比模板作者自己写的文案还紧")
        for x in unfit[:12]:
            print("   ·", x)
        print("   ⇒ 模型照着这张表写会长年被判短（或反过来把英文栏位挤扁），"
              "**先修 `rongke/slotfit.py` 的模型**。")
        return 4
    # ★ 出生即自检 ③：**几何自洽**（换一把尺子）。①②都只证"一致"，可以一起错；
    #   ③ 的算式里没有 `slot_capacity`，专治"上限被算大"这种危险方向。
    n_geo, bad_geo = verify_cap_geometry(data)
    if bad_geo:
        print(f"[FAIL] 几何自洽不过：{n_geo} 个可比槽里有 {len(bad_geo)} 处"
              f"每行容量配不上框的可用宽度")
        for x in bad_geo[:12]:
            print("   ·", x)
        print("   ⇒ 最可能是**方向判错**（如把 `vert=\"horz\"` 当竖排）或内边距读错。")
        return 5
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # LF + UTF-8（与仓库换行策略一致）
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8", newline="\n")

    print(f"[OK] {out}")
    print(f"  画布 {data['aspect']} ｜ 页型 {len(data['pages'])} 种 ｜ "
          f"容量自检 {n_ok} 槽全对 ｜ 原文装得下 {n_fit} 槽全对 ｜ "
          f"几何自洽 {n_geo} 槽全对")
    for kind, blk in data["pages"].items():
        for e in blk["entries"]:
            fillable = [s for s in e["slots"]
                        if s["role"] not in ("anchor", "fixed", "empty")]
            caps = [s["cap_chars"] for s in fillable if s["cap_chars"]]
            a = e.get("assets") or {}
            warn = ""
            if a.get("charts"):
                warn = "  ⚠️ 自带示例图表"
            print(f"  {e['id']:18s} p{e['src']:<3d} {blk['label']:10s} "
                  f"槽 {len(fillable):>2d} 个"
                  f"（中文 {min(caps) if caps else '?'}–{max(caps) if caps else '?'} 字）"
                  f"  图{a.get('pics', 0)} 表{a.get('tables', 0)} "
                  f"字图{a.get('smartart', 0)}{warn}"
                  f"  角色 {sorted({s['role'] for s in fillable})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
