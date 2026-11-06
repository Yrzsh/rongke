"""设计资产 —— 全项目唯一的「版面规范」单源。

## 为什么有这个模块（2026-09-21 的诊断，不是推想）

用户看完一份**五门全绿**的 13 页组会 deck，只说了一句「**太差了，不能直接上组会**」。
翻遍整条链路，**没有任何一处存着"设计资产"**：

  · 起手 `create_presentation()` → python-pptx 的**空白 4:3 默认母版**
    （`vendor/ppt-mcp/utils/presentation_utils.py`，白底、Office 默认主题）；
  · `style_deck` 的全部打扮 = 换 12 个色槽 + 换字体 + 背景填色
    + **一条 0.01 英寸的页脚线**；
  · `DECK_THEMES` 只有 `colors` + `fonts` 两个字段 —— 无字号、无版式、无几何；
  · 那份已装的 PPT 技能里，全目录数值规范只有 6 处，**零个 hex 色值**。

⇒ 全链最"具体"的设计数值是一条页脚线。**判据只能划定下限；产出上限来自资产本身。**
   别人"加个 SKILL 就好看"，加的其实是这套东西：把设计师的隐性判断
   （版式比例、色彩克制、字号对比、留白）**固化成可判定的数字**。

## 这个模块装了什么

  ① 画布与单位换算（px ↔ EMU ↔ pt，**精确整数**，无取整误差）
  ② 版面三区网格：A 标题带 / B 内容带 / C 页脚带 + 左右页边距
  ③ 字号阶梯：8 档（封面 96px → 页脚 16px），**pt 是唯一写进 XML 的档位**
  ④ 色彩角色：**只写「角色 → theme 槽名」的映射**，hex 不在这里
  ⑤ 六种页型的元素几何 + 装饰 + 通用页脚

## 两条单源纪律（违反即 H 型）

  · **色值不在这里复制**：这里的值只是槽名（`"accent1"`），真正的 hex 单源在
    `deck.DECK_THEMES` —— `patch_theme` 与视觉判据都读那一份。
  · **子进程引用不到本模块**（`deck._COMPOSE_SCRIPT` 跑在独立解释器里）
    ⇒ 整个 dict 经 payload **注入**，沿用 `GEO_ASYM_MAX` 的既有手法。
    **不是抄一份** —— 抄一份就是"判据那边 3.0、修复这边 3.0，改一处忘一处"。

## 单位换算为什么能精确

  16:9 = 13.3333in × 7.5in = 960pt × 540pt = 12192000 × 6858000 EMU。
  取 1280px 宽 ⇒ 12192000 / 1280 = **9525 EMU/px**（整数）；
  960pt / 1280 = **0.75 pt/px**（有限小数）。所以不引入任何浮点误差。
"""

from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════
# ① 画布与单位
# ══════════════════════════════════════════════════════════════════════

#: 设计坐标系（px）。**只用来读本模块的表**，不写进文件。
CANVAS_PX: tuple[int, int] = (1280, 720)

#: 1px = 9525 EMU（= 1/96 in），精确整数。
PX_EMU: int = 9525

#: 1px = 0.75pt，有限小数。
PX_PT: float = 0.75


def px_to_emu(v: float) -> int:
    """px → EMU（精确整数）。"""
    return int(round(v * PX_EMU))


def px_to_pt(v: float) -> float:
    """px → pt（保留两位；本表所有值换算后都是有限小数）。"""
    return round(v * PX_PT, 2)


def emu_to_px(v: float) -> float:
    """EMU → px（复测时用）。"""
    return v / PX_EMU


# ══════════════════════════════════════════════════════════════════════
# ② 版面三区网格
# ══════════════════════════════════════════════════════════════════════
#
#   ┌──────────────────────────────────────┐ 0
#   │ A · 标题带                            │
#   ├──────────────────────────────────────┤ 120
#   │ B · 内容带（唯一可变高度的那一块）      │
#   ├──────────────────────────────────────┤ 660
#   │ C · 页脚带                            │
#   └──────────────────────────────────────┘ 720
#
#: 三带的 (top, bottom)，px。
BANDS_PX: dict[str, tuple[int, int]] = {
    "A": (0, 120),
    "B": (120, 660),
    "C": (660, 720),
}

#: 左右页边距（px）。= 0.75in，与 16:9 画布成整比。
PAD_PX: int = 72

#: 内容带里元素的实际起画点（px）。比 B 区顶多出 32px 呼吸，
#: 否则标题下的强调线会和大块正文贴在一起。
CONTENT_TOP_PX: int = 152

#: 内容可用宽度（px）：1280 − 2×72 = 1136。
CONTENT_W_PX: int = CANVAS_PX[0] - 2 * PAD_PX

#: 内容可用高度（px）：从 152 到 660 = 508；取 472 留 36px 下呼吸。
CONTENT_H_PX: int = BANDS_PX["B"][1] - CONTENT_TOP_PX - 36


# ══════════════════════════════════════════════════════════════════════
# ③ 字号阶梯
# ══════════════════════════════════════════════════════════════════════
#
# 纪律：**一页里只允许出现阶梯上的档位**，不许临时凑一个中间值 ——
#       字号对比是"层次感"的全部来源，随手取值会让层次糊掉。
#
# 换算后：96px→72pt｜80→60｜64→48｜**48→36**｜40→30｜**32→24**｜24→18｜16→12
#
# ★ 阶梯的下限被**客观判据**钉在 12pt —— 这不是审美取舍，是硬约束：
#   `deck.py::_gate_type` 的 `font_small` 拿 `facts.min_body_font_pt` 量
#   「正文体量」的**最小**字号（口径 = 单个 run ≥ `_BODY_TEXT_MIN_CHARS`=12 字），
#   低于 12pt 就 WARN。
#
#   ⚠️ 我第一版把页脚定在 14px(10.5pt)，理由是"与既有页脚页同值 ⇒ 零回归"。
#      **那个理由是假的**：实测旧那份 deck 的 `font_small` 根本没响，不是因为
#      10.5pt 合格，而是因为它的正文占位符**字号继承母版、量不出来** ⇒
#      判据走的是 SKIP 分支（"没查"）。**「没查」被我读成了「查了没问题」。**
#      一旦本工具把字号写实，10.5pt 立刻被抓 —— 页脚落款有 20 个字，
#      在判据眼里就是一段"正文"。
#   ⇒ 治法不是去放宽判据（那会把真问题一起放过去），是**让阶梯满足判据**：
#     全套文字 ≥ 12pt ⇒ `min_body_font_pt` 恒 ≥ 12 ⇒ 不需要任何豁免。
#
# ★★ 正文 22 → 24 → 28 → **26→32px**，走了四版。前三版都在**猜**，最后一版是**看**出来的：
#
#   第 1 版 22px(16.5pt)：渲染出来像注脚。
#   第 2–3 版 24/28px：解继承链后知道原件正文是 master 的 **32pt**
#     （`txStyles/bodyStyle/lvl1pPr/defRPr sz=3200`，titleStyle 4400=44pt），
#     于是往上抬。
#
#   ⚠️ **中间我推错过一次，错在把"行宽"当成了"字号"**：
#     我当时写「原件 32pt 摆在窄框（占宽 67.5%）里所以又大又密，
#     我摆在 1136px 全宽框里就显小」—— 于是只敢抬到 21pt。
#     **这个推理是错的**：同一个 pt 值，在两张等高（7.5in）的幻灯片上
#     **视觉高度完全一样**，跟画布宽度无关。宽度只决定**每行装几个字**。
#     ⇒ 要还原原件的"体量"，正文就该是 **32pt 那一档**，不是往下缩。
#
#   ★ 而**判据一条都查不出这一类**（不越界、不超字号、页面不溢出）：
#     只有把成品**渲染出来看**才发现"这页空得像没写完"。
#     ⇒ 这就是参考样例（`docs/deck-reference/`）存在的理由：
#       它把这个反馈回路固定下来 —— 排完要看，看完要改。
LADDER_PX: dict[str, int] = {
    "cover_title": 96,   # 封面主标题（72pt）
    "anchor": 80,        # 巨型数据锚点（60pt）
    "section": 64,       # 章节大字 / 收束金句（48pt）
    "page_title": 48,    # 页面主标题（36pt）
    "subtitle": 40,      # 副标题 / 卡片头（30pt）
    "body": 32,          # 正文（24pt）—— 与母版默认同档（原件正是 32pt）
    "caption": 24,       # 图注 / 落款（18pt）
    "footer": 16,        # 页脚 / 页码（12pt，**判据门槛下限**）
}

#: 同上，换算成 pt（写进 XML 用这个）。由 `LADDER_PX` 派生 ⇒ 单源。
LADDER_PT: dict[str, float] = {k: px_to_pt(v) for k, v in LADDER_PX.items()}

#: 复测用的白名单：**允许出现在文件里的字号**（pt）。
ALLOWED_PT: frozenset[float] = frozenset(LADDER_PT.values())

#: 哪些档位加粗。
BOLD_KEYS: frozenset[str] = frozenset(
    {"cover_title", "anchor", "section", "page_title", "subtitle"})


# ══════════════════════════════════════════════════════════════════════
# ③b 一行字占多高 —— 文本框的**最小高度**从哪来
# ══════════════════════════════════════════════════════════════════════
#
# ★ 为什么这一节必须存在（2026-09-21 实跑抓到）：
#   我把 `subtitle` 从 32px 抬到 40px，**忘了封面副标题的框还是 44px 高**。
#   ⇒ 复测当场报 `text_overflows_box`（1.395 > 1.30）。
#   渲染出来看**并没有被裁掉**（PowerPoint 默认不裁剪），所以它不是"画面坏了"——
#   但它是**真的缺陷**：框装不下一行 ⇒ 那个文本框的字一换行，
#   第二行就会**压到下面的元素上**（封面署名恰好在它下方 70px 处）。
#   ⇒ 这一类的病根不是"判据太严"，是**资产自己没算过"一行字要占多高"**。
#
# 纪律：**这几个数不许在这里另写一份**。
#   它们是 `deck.py` 里溢出估算器用的同一组物理量 ——
#   那边要用同一个值去判断"有没有溢出"。
#   两处各写一个 1.2 就是 H 型：改了估算器、资产跟着失效，而没有任何东西会响。
#   ⇒ 单源做法：**定义在这里**，`deck.py` 反过来从本模块取
#     （`design.py` 零依赖、不 import 任何项目内模块 ⇒ 不会成环）。

#: 典型单倍行距（取值偏保守：宁高不低）。`deck.py::_LINE_SPACING` 取同一对象。
LINE_SPACING: float = 1.2

#: DrawingML 默认文本框内边距：左右 0.1 英寸、上下 0.05 英寸（EMU）。
#: `deck.py::_INS_LR` / `_INS_TB` 取同一对象。
TEXT_INSET_LR_EMU: int = 91440
TEXT_INSET_TB_EMU: int = 45720


def min_box_h_px(size_key: str) -> float:
    """某个字号档位下，**装得下一行字**所需的最小框高（px）。

        px(一行) = LADDER_PX[档位] × LINE_SPACING
        再加上下内边距

    ⚠️ 这是**下限**，不是"理想高度"。它只回答一个问题：
       **这个框会不会连一行都装不下。**
       至于"两三行的标题该给多高"，那要靠渲染看（见 `PAGE_KINDS` 里
       封面标题那条注释：第一版只给 140px，两行 96px 字直接压到副标题上）。
    """
    return round(LADDER_PX[size_key] * LINE_SPACING
                 + 2 * TEXT_INSET_TB_EMU / PX_EMU, 2)


def short_box_slots() -> list[str]:
    """**自检**：列出框高装不下一行字的 slot / 页脚项。

    返回形如 `["cover.subtitle(44<57.6)", "footer.label(28<28.8)"]`；
    全合规返回 `[]`。`deck_test` §23 拿它做断言 —— **资产自己盯住自己**。

    ★ 为什么做成函数而不是靠人眼：我给阶梯换过一次档（`subtitle` 32→40px），
      六个框当场就不够高了，而**渲染看不出来**（不裁剪）—— 只有算式看得出来。
    """
    bad: list[str] = []
    for kind, pk in PAGE_KINDS.items():
        for slot in pk.get("slots", []):
            key = slot.get("size")
            if not key:
                continue                      # 图/表：不动字号 ⇒ 不适用
            h = float(slot["box"][3])
            need = min_box_h_px(str(key))
            if h < need:
                bad.append(f"{kind}.{slot.get('role')}({h:g}<{need:g})")
    for nm, item in (("footer.rule", FOOTER_RULE),
                     ("footer.label", FOOTER_LABEL),
                     ("footer.page", FOOTER_PAGE)):
        key = item.get("size")
        if not key:
            continue
        h = float(item["box"][3])
        need = min_box_h_px(str(key))
        if h < need:
            bad.append(f"{nm}({h:g}<{need:g})")
    return bad


# ══════════════════════════════════════════════════════════════════════
# ④ 色彩角色 → theme 槽名
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ **这里只写槽名，不写 hex。** 真正的色值单源在 `deck.DECK_THEMES`，
#    由 `patch_theme` 写进 theme1.xml、由视觉判据读回核对。
#    两处都写 hex 就是 H 型：改一处忘一处，静默漂移。
#
# 面积纪律（来自成熟模板体系的共同约定，`compose_deck` 复测可量）：
#   主色/区块 ≤ 60%｜辅色 ≤ 30%｜**强调色 ≤ 10%**（封面页可放宽到 20%）。
#   强调色超配额就会脏 —— 这是"看起来廉价"最常见的单一成因。
COLOR_ROLES: dict[str, object] = {
    "bg": "lt1",        # 页面底色
    "block": "lt2",     # 区块 / 卡片底
    "ink": "dk1",       # 标题与正文（墨迹）
    "ink2": "dk2",      # 次级文字 / 图注
    "accent": "accent1",  # 强调（朱砂）—— 用量受配额约束
    "rule": "accent2",  # 分隔线 / 页脚
    "data": "accent4",  # 数据锚点
    "series": ["accent1", "accent5", "accent3", "accent6"],  # 图表系列
}

#: 强调色的面积配额（分母 = 页面面积）。复测用。
ACCENT_QUOTA = 0.10
ACCENT_QUOTA_HERO = 0.20

#: 文本的**缺省水平对齐**。设计语言通篇左对齐（左页边距 72px + 朱砂短线）。
#:
#: ★ 为什么必须**显式写进 XML**（2026-09-21 实测）：`a:pPr/@algn` 缺省时
#:   PowerPoint 会去**母版/版式**取值 ⇒ "正文到底左对齐还是居中"由**这份
#:   deck 的来路**决定，而不是由我们决定。实测那份 13 页 deck 的 `algn`
#:   **全是 None** ⇒ 收束页正文被版式居中，和左侧强调整线打架。
#:   ⇒ 版面规范必须**显式拥有**对齐。
#:
#: 单源：`_COMPOSE_SCRIPT`（经 payload 注入）与 `_align_report`（读回复测）
#: 用**同一个值** —— 两处各写一个字面量就是 H 型。
ALIGN_DEFAULT: str = "left"

#: **哪些 slot 的 role 是"列表"**（要项目符号），其余一律显式写 `a:buNone`。
#:
#: ★ 为什么这是个**设计资产**问题，而不是"润色"（2026-09-21 **渲染出来才看见**）：
#:   参考样例的**章节页副标题**带上了项目符号 —— 变成一个孤零零的「•」。
#:   根因：那一页的副标题槽吃的是 `body` 占位符，而 `body` 在版式的
#:   `lstStyle` 里是带点的列表。**换一个没有列表样式的版式来建，点就没了。**
#:   ⇒ "这行前面有没有点"由**这份 deck 的来路**决定，而不是由我们决定。
#:     这与 `ALIGN_DEFAULT` 是**同一个病**：版面规范没有显式拥有它。
#:
#: 口径按 **role**（语义位），**不按形状出身** —— 这正是设计资产的意思：
#: 同一个 `body` 占位符，落进 `content` 的正文槽就该有点，
#: 落进 `section` 的副标题槽就不该有。
BULLET_ROLES: frozenset[str] = frozenset({"body", "body_left", "body_right"})


# ══════════════════════════════════════════════════════════════════════
# ⑤ 六种页型的元素几何
# ══════════════════════════════════════════════════════════════════════
#
# 每条 slot：
#   role   —— 语义名（也是复测里 `rk-slot-<role>` 标记的取值）
#   from   —— 接收哪些**来源形状**。取值为占位符类型名
#             （`title`/`ctrTitle`/`subTitle`/`body`）、`visual`（图/表/图表）、
#             `other`（普通文本框）。按列表顺序优先匹配。
#   box    —— (left, top, width, height)，px
#   size   —— `LADDER_PX` 的键；None ⇒ 不动它的字号（图/表）
#   color  —— `COLOR_ROLES` 的键；None ⇒ 不动它的颜色
#   align  —— "left" | "center" | "right"；缺省 left
#
# 每条 decor：由 `compose_deck` **新画**的装饰元素。
#   name 必须稳定 —— 幂等靠"先按名删、再画"实现。

_RULE = {"shape": "rect", "box": (72, 116, 96, 6), "color": "accent",
         "name": "rk-decor-rule"}
_RULE_SHORT = {"shape": "rect", "box": (72, 128, 96, 6), "color": "accent",
               "name": "rk-decor-rule"}

PAGE_KINDS: dict[str, dict] = {
    # ── 封面 ──────────────────────────────────────────────────────
    "cover": {
        "label": "封面页",
        "chrome": False,          # 不加通用页脚（封面带页脚很业余）
        "slots": [
            # ⚠️ 标题框高度必须容得下**两三行** 96px 字（96×2×1.2 ≈ 230px）。
            #    第一版只给 140px ⇒ 两行标题直接压到副标题上。
            #    **这一条判据查不出来** —— 只有渲染出来才看得见
            #    （「判据能证明不越界，证明不了好看」的活例）。
            {"role": "title", "from": ("title", "ctrTitle", "other"),
             "box": (72, 240, 1136, 240), "size": "cover_title", "color": "ink"},
            # ⚠️ 高 44→64：`subtitle` 抬到 40px 后，44px 装不下一行（需要 57.6）。
            #    渲染看**不出来**（不裁剪），但**字一换行就会压到下面那行署名**上。
            {"role": "subtitle", "from": ("subTitle", "body"),
             "box": (72, 508, 1000, 64), "size": "subtitle", "color": "ink2"},
            # ⚠️ 高 32→40：同上（caption 24px 需要 38.4）。
            {"role": "signature", "from": ("other",),
             "box": (72, 578, 700, 40), "size": "caption", "color": "rule"},
        ],
        "decor": [
            {"shape": "rect", "box": (72, 232, 120, 8), "color": "accent",
             "name": "rk-decor-rule"},
        ],
    },
    # ── 章节 ──────────────────────────────────────────────────────
    "section": {
        "label": "章节页",
        "chrome": False,
        "slots": [
            # ⚠️ 标题 80→88、副标题 36→48：`section` 48px 需 86.4、`body` 32px 需 48.0。
            #    这两个框原先都装不下自己那一行（同封面那两处，同一个病）。
            {"role": "title", "from": ("title", "ctrTitle", "other"),
             "box": (356, 264, 852, 88), "size": "section", "color": "ink"},
            {"role": "subtitle", "from": ("subTitle", "body"),
             "box": (356, 360, 852, 48), "size": "body", "color": "ink2"},
            {"role": "anchor", "from": ("other",),
             "box": (72, 228, 252, 192), "size": "anchor", "color": "accent"},
        ],
        "decor": [],
    },
    # ── 内容 ──────────────────────────────────────────────────────
    "content": {
        "label": "内容页",
        "chrome": True,
        "slots": [
            {"role": "title", "from": ("title", "ctrTitle", "other"),
             "box": (72, 36, 1136, 72), "size": "page_title", "color": "ink"},
            # ⚠️ 这里**故意用 top 而不是 middle**（2026-09-21 渲染对照后改）：
            #    `middle` 把三条要点吊在 472px 内容带的正中 ⇒ 上下各一大片空，
            #    读起来像"走丢了"。而成熟学术 deck 的版面纪律是
            #    **上锚点（标题）→ 中部主体 → 下锚点（页脚/落款带）**，
            #    空白应当**聚在底部**（读作留白），不该被上下切开（读作失误）。
            #    参考文献 `nature-paper2ppt/references/design-and-layout.md`
            #    的 Page fullness rule。
            #    ★ 这一条**判据查不出来**（"居中"不越界、不超字号），
            #      只有渲染出来并列对比才看得见。
            {"role": "body", "from": ("body", "subTitle", "other"),
             "box": (72, CONTENT_TOP_PX, 1136, CONTENT_H_PX),
             "size": "body", "color": "ink", "anchor": "top"},
        ],
        "decor": [_RULE],
    },
    # ── 双栏 ──────────────────────────────────────────────────────
    "two-col": {
        "label": "双栏页",
        "chrome": True,
        "slots": [
            {"role": "title", "from": ("title", "ctrTitle", "other"),
             "box": (72, 36, 1136, 72), "size": "page_title", "color": "ink"},
            {"role": "body_left", "from": ("body", "subTitle", "other"),
             "box": (72, CONTENT_TOP_PX, 552, CONTENT_H_PX),
             "size": "body", "color": "ink", "anchor": "middle"},
            {"role": "body_right", "from": ("body", "subTitle", "other"),
             "box": (656, CONTENT_TOP_PX, 552, CONTENT_H_PX),
             "size": "body", "color": "ink", "anchor": "middle"},
        ],
        "decor": [_RULE],
    },
    # ── 图表 ──────────────────────────────────────────────────────
    "chart": {
        "label": "图表页",
        "chrome": True,
        "slots": [
            {"role": "title", "from": ("title", "ctrTitle", "other"),
             "box": (72, 36, 1136, 72), "size": "page_title", "color": "ink"},
            # 主视觉吃 B 区的 >60% 宽 —— "让证据自己占满一页"是学术 deck 的硬要求
            {"role": "visual", "from": ("visual",),
             "box": (72, CONTENT_TOP_PX, 736, CONTENT_H_PX),
             "size": None, "color": None},
            {"role": "body", "from": ("body", "subTitle", "other"),
             "box": (840, CONTENT_TOP_PX, 368, CONTENT_H_PX),
             "size": "body", "color": "ink", "anchor": "middle"},
        ],
        "decor": [_RULE],
    },
    # ── 收束 ──────────────────────────────────────────────────────
    "closing": {
        "label": "收束页",
        "chrome": False,
        "slots": [
            {"role": "title", "from": ("title", "ctrTitle", "other"),
             "box": (72, 232, 1136, 120), "size": "section", "color": "ink"},
            # ⚠️ 第一版正文框只有 36px 高 ⇒ 装不下「下一步计划」那三条要点，
            #    渲染时整块溢出到框外（同样是判据查不出的那一类）。给到 220px。
            {"role": "body", "from": ("body", "subTitle", "other"),
             "box": (72, 380, 1136, 220), "size": "body", "color": "ink2",
             "anchor": "middle"},
        ],
        "decor": [
            {"shape": "rect", "box": (72, 236, 96, 6), "color": "accent",
             "name": "rk-decor-rule"},
        ],
    },
}

#: 合法页型（`compose_deck` 的入参白名单）。
KIND_NAMES: tuple[str, ...] = tuple(PAGE_KINDS)

#: 页型别名：让模型不必背准拼写。键是"能被接受的说法"，值是标准名。
KIND_ALIASES: dict[str, str] = {
    "cover": "cover", "封面": "cover", "title": "cover", "titlepage": "cover",
    "section": "section", "章节": "section", "chapter": "section", "divider": "section",
    "content": "content", "内容": "content", "body": "content", "text": "content",
    "bullet": "content", "list": "content",
    "two-col": "two-col", "two_col": "two-col", "twocol": "two-col",
    "双栏": "two-col", "columns": "two-col", "compare": "two-col",
    "chart": "chart", "图表": "chart", "figure": "chart", "data": "chart",
    "result": "chart", "graph": "chart",
    "closing": "closing", "收束": "closing", "结尾": "closing",
    "end": "closing", "conclusion": "closing", "thanks": "closing",
}


# ══════════════════════════════════════════════════════════════════════
# ⑥ 通用页脚（除 cover / section / closing 之外的页型都加）
# ══════════════════════════════════════════════════════════════════════
#
# ⚠️ 其中两个名字（`juanqing-footer-line` / `juanqing-footer-page`）
#    是 `style_deck` 的既有命名，`deck_test` 有断言按**名字**数页脚形状。
#    **名字不能改**，几何可以改到设计网格上。
#: 页脚线：贴 C 带顶部（668px），通栏。
FOOTER_RULE = {"shape": "rect", "box": (72, 668, 1136, 1),
               "color": "rule", "name": "juanqing-footer-line"}
#: 页脚左：项目/汇报名（文本由调用方给，缺省留空 ⇒ 只画线不画字）。
#: ⚠️ 高 28→30：`footer` 16px 需 28.8 —— 28 差 0.8px 也是"装不下一行"。
FOOTER_LABEL = {"shape": "text", "box": (72, 678, 820, 30),
                "size": "footer", "color": "rule",
                "name": "juanqing-footer-label"}
#: 页脚右：页码（文本自动填页序号）。
FOOTER_PAGE = {"shape": "text", "box": (1088, 678, 120, 30),
               "size": "footer", "color": "rule", "align": "right",
               "name": "juanqing-footer-page"}


def resolve_kind(raw: str) -> str | None:
    """把模型给的说法归一成标准页型名；认不出返回 None（**不许猜**）。"""
    k = str(raw or "").strip().lower().replace(" ", "").replace("-", "-")
    if k in PAGE_KINDS:
        return k
    return KIND_ALIASES.get(k) or KIND_ALIASES.get(k.replace("_", "-"))


def as_payload() -> dict:
    """打包给子进程的**完整**设计资产 dict。

    子进程是独立解释器，`import rongke.design` 到不了 ⇒ 必须整体注入。
    这也是**单源**的实现方式：注入的是本模块的活对象，不是抄本。
    """
    return {
        "canvas_px": list(CANVAS_PX),
        "px_emu": PX_EMU,
        "bands_px": {k: list(v) for k, v in BANDS_PX.items()},
        "pad_px": PAD_PX,
        "ladder_px": dict(LADDER_PX),
        "ladder_pt": dict(LADDER_PT),
        "bold_keys": sorted(BOLD_KEYS),
        "align_default": ALIGN_DEFAULT,
        "bullet_roles": sorted(BULLET_ROLES),
        "color_roles": COLOR_ROLES,
        "page_kinds": PAGE_KINDS,
        "footer": {"rule": FOOTER_RULE, "label": FOOTER_LABEL,
                   "page": FOOTER_PAGE},
    }
