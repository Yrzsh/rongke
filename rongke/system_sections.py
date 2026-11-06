# -*- coding: utf-8 -*-
"""SYSTEM 分节装配：把「每场全量注入」改成「开场按任务装配一次、整场冻结」。

## 学的是谁，学了什么，没学什么

| 来源 | 主张 | 这里怎么用 |
|---|---|---|
| Anthropic Agent Skills | 三级渐进披露：元数据常驻、指令触发才读、资源按需 | 把 SYSTEM 切成语义节，只装配本任务用得上的节 |
| ITR（arXiv:2602.17046） | 每步检索最小 system 片段 + 最小工具子集，带置信度门控回退 | 借「按任务最小化」，**不借每步动态** |
| prompt caching | 缓存前缀必须逐字节稳定；改一个字符整段 miss | 装配只在开场发生一次，之后整场不改 ⇒ 前缀仍可缓存 |
| dsh `complete:true` | 只发一份完整段，升级新增的段再也进不来 | 能力自带 `SYSTEM_COMPLETE` 即可结构性替换 |

⚠️ **ITR 与 prompt caching 直接冲突**：每步重算 system 会把缓存打穿。
`loop.py:4960` 自己就写着「工具 schema 外加每轮原样重发的 SYSTEM 是前缀缓存
最值钱的对象」。所以本模块的纪律是**一次装配、整场冻结**，不是「每步检索」；
省的是「本任务根本用不着的段」，不是「每一步都重新算一遍」。

## 三条硬约束（判据会逐条量）

1. **可逆**：按构造，所有节文本按原序拼接 == 输入 system 逐字节。
   锚点必须在输入串里恰好出现一次且位置严格递增，否则 raise ——
   改了 SYSTEM 让锚点漂移时，宁可当场炸，也不许静默产出一份缺段的 system。
2. **baseline 等价**：不声明 `needed` 也不给 `complete_text` ⇒ 返回**输入对象
   本身**，连拼接都不做。旧路径一个字节都不变。
3. **交付纪律不许被裁掉**：`- **交稿时必须附上正文全文**` 起的那一节
   长在「做演示文稿」标题之下，但它是**所有写作任务**的交付门。
   按标题粗暴裁 PPT 段会把它一起带走（2026-11-04 实测过的真实缺口），
   所以单列成 `always` 节。
"""
from __future__ import annotations

from dataclasses import dataclass

#: (id, 起始锚点, 是否常驻, 说明)。`None` = 从开头起。
#: 锚点必须是 SYSTEM 里**唯一**的子串；这里不存行号 —— 行号会随注释漂移，
#: 语义锚点不会。
ANCHORS: tuple[tuple[str, str | None, bool, str], ...] = (
    ("identity", None, True, "身份 / 证据门 / 提问分支 / 文献核验"),
    ("writing", "写作任务：", False, "写作流水线 / 阶段技能 / 引注与动笔纪律"),
    ("figure", "配图任务：", False, "配图静默失败 / audit_figure"),
    ("deck", "做演示文稿（组会 PPT / 汇报幻灯片）任务：", False,
     "PPT 工具链 / 版面精排 / 讲稿备注"),
    ("delivery", "- **交稿时必须附上正文全文**", True,
     "交付正文全文 / 稿面体检 / 写作姿态（跨任务，不许跟着 PPT 一起被裁）"),
    ("workspace", "工作区纪律：", True, "工作区围栏 / 相对路径"),
    ("tools", "工具使用：", True, "参数 schema / 读输入认对门"),
)

ALWAYS: tuple[str, ...] = tuple(a[0] for a in ANCHORS if a[2])
OPTIONAL: tuple[str, ...] = tuple(a[0] for a in ANCHORS if not a[2])
ALL_IDS: tuple[str, ...] = tuple(a[0] for a in ANCHORS)


@dataclass(frozen=True)
class Section:
    id: str
    text: str
    always: bool
    note: str


@dataclass(frozen=True)
class Assembly:
    """一次装配的**事实**。发出去给人看，也进判据。"""

    text: str
    sections: tuple[str, ...]
    dropped: tuple[str, ...]
    chars: int
    baseline_chars: int
    complete: bool
    frozen: bool = True

    @property
    def saved(self) -> int:
        return self.baseline_chars - self.chars

    def to_dict(self) -> dict:
        return {"sections": list(self.sections), "dropped": list(self.dropped),
                "chars": self.chars, "baseline_chars": self.baseline_chars,
                "saved": self.saved, "complete": self.complete,
                "frozen": self.frozen}


def split_sections(system_text: str) -> list[Section]:
    """按语义锚点把 system 切成**有序、无损、可逆**的节。

    这是纯函数：不 import `loop`，不碰全局状态 ⇒ 可独立测，也不会形成
    `loop → system_sections → loop` 的循环导入。
    """
    s = str(system_text or "")
    if not s:
        return []
    marks: list[tuple[str, int, bool, str]] = []
    for sid, anchor, always, note in ANCHORS:
        if anchor is None:
            pos = 0
        else:
            n = s.count(anchor)
            if n != 1:
                raise ValueError(
                    f"SYSTEM 分节锚点 {sid!r}（{anchor!r}）命中 {n} 次，必须恰好 1 次")
            pos = s.index(anchor)
        marks.append((sid, pos, always, note))
    for i in range(1, len(marks)):
        if marks[i][1] <= marks[i - 1][1]:
            raise ValueError(
                f"SYSTEM 分节锚点顺序错乱：{marks[i-1][0]}@{marks[i-1][1]} "
                f"→ {marks[i][0]}@{marks[i][1]}")
    out: list[Section] = []
    for i, (sid, pos, always, note) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(s)
        out.append(Section(sid, s[pos:end], always, note))
    # 构造性可逆断言：可逆不是「测试碰巧过了」，是这里拼起来必须相等。
    if "".join(x.text for x in out) != s:
        raise AssertionError("SYSTEM 分节不可逆：拼接结果与原串不一致")
    return out


def minimal_system(system_text: str) -> str:
    """`complete:true` 但能力没自带全文时的骨架 = 所有常驻节。

    不是空串：身份、证据门、交付纪律、工作区、工具纪律缺一不可。
    """
    return "".join(x.text for x in split_sections(system_text) if x.always)


def assemble(system_text: str, needed=None, complete_text=None) -> Assembly:
    """开场装配一次。

    * `needed` 为 None/空 且无 `complete_text` ⇒ **返回输入对象本身**（baseline）。
    * `needed` 声明了可选节 ⇒ 常驻节 ∪ 点名节，按原序拼接；未点名的可选节丢掉。
    * `complete_text` 非空 ⇒ 结构性覆盖（dsh `completeSection` 同款），
      system 完全由能力/预设提供，内核一个字都不追加。

    未知节名**raise**，不许被静默吞掉 —— 拼错一个节名就等于悄悄少给一段纪律。
    """
    s = str(system_text or "")
    base = len(s)
    # ★ 两条快速路径必须排在 `split_sections` **之前**（2026-11-05 静态审阅实踩）：
    #   ① 能力自带全文 ⇒ 结构性覆盖与 SYSTEM 锚点无关；
    #   ② baseline ⇒ 连分节都不做，返回输入对象本身。
    #   否则 SYSTEM 锚点一旦漂移，baseline 和 `initial_messages` 里那句
    #   「退回全量」的异常降级**会一起抛错** —— 注释承诺「不许把任务打崩」
    #   就成了一句空话。降级路径现在恒能拿到完整的原 system。
    if complete_text:
        text = str(complete_text)
        return Assembly(text, (), ALL_IDS, len(text), base, True)
    if not needed:
        # 全量，仍然返回原对象，保证逐字节等价（连一次切分都不做）。
        return Assembly(s, ALL_IDS, (), base, base, False)
    secs = split_sections(s)
    if not secs:
        return Assembly(s, (), (), base, base, False)
    want = {str(x) for x in needed}
    unknown = sorted(want - set(ALL_IDS))
    if unknown:
        raise ValueError(f"未知的 SYSTEM 节 {unknown!r}（可用 {list(ALL_IDS)}）")
    if want >= set(OPTIONAL):
        # 全点名 = 全量，仍然返回原对象，保证逐字节等价。
        return Assembly(s, ALL_IDS, (), base, base, False)
    active = [x for x in secs if x.always or x.id in want]
    active_ids = {x.id for x in active}
    text = "".join(x.text for x in active)
    return Assembly(text, tuple(x.id for x in active),
                    tuple(x.id for x in secs if x.id not in active_ids),
                    len(text), base, False)
