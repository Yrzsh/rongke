# -*- coding: utf-8 -*-
"""稿面过程反馈：让模型**一次读回「还差什么」**，而不是交付时被打回。

═══════════════════════════════════════════════════════════════════════
为什么不是「再造一个工具」
═══════════════════════════════════════════════════════════════════════
`writing_metrics` 已经存在（`caps_builtin.py`），但它只回 5 个指标 + 一份
人话报告：模型得自己从 34 项里推算「我差多少字、引注有没有乱、套话有没有」，
于是 D2 实测调了 5 次。这里做的是**把那 5 次收敛成 1 次**：

  · 全量指标一次给齐（内核实测，不让模型外推）；
  · 只列**契约硬约束里没达标的差异**，其余启发式项标「建议、不阻断」；
  · 字数用 `Report.display_unit` 单一量纲，不再一行两个数。

═══════════════════════════════════════════════════════════════════════
与门的分工（这条不能反）
═══════════════════════════════════════════════════════════════════════
**这一层只给信息，不拦人。** 启发式项（句长、术语密度、尾段重复…）在这里
一律是「建议」，不进硬约束；门仍只判契约里那几类内核可实测的硬约束。
把更多指标搬进终审 = 多一次打回 = 更费力，与目标函数相反。

`lint()` 的 34 项是唯一数据源；这里不另算一遍，避免两处真相。
"""
from __future__ import annotations

from typing import Any

from .delivery_pipeline import CHARS_TOLERANCE
from .writing import lint

TITLE = "【稿面读回 · 还差什么】"


def build_feedback(text: str, *, target_chars: int | None = None,
                   caps=None, sources=None, read_docs=None,
                   searched_docs=None, min_score: float = 0.55) -> dict[str, Any]:
    """返回 `{"text", "metrics", "gaps", "advisory", "score", "unit", "chars",
    "alignment"}`。

    `gaps` 为空 ⇒ 硬约束全达标，`text` 会明说「可继续写」，不列一堆无关的
    「建议」把模型训练成无视反馈。

    ★ 引文溯源是**附加读回，不进 `gaps`、不改 `needs_revision`**：
    显式给了 `sources` / `read_docs` / `searched_docs` 才跑，什么都不传时
    返回体与 11-07 版本逐字节一致（baseline 等价，也不给不带预设的任务添噪声）。
    """
    try:
        rep = lint(text or "", target_chars=target_chars, caps=caps)
    except TypeError:                    # 兼容 lint 尚无 caps 形参的中间态
        rep = lint(text or "", target_chars=target_chars)
    m = dict(rep.metrics or {})
    unit = getattr(rep, "display_unit", "chars") or "chars"
    cur = m.get(unit, m.get("chars", 0))

    gaps: list[str] = []
    if target_chars:
        d = abs(float(m.get("chars_delta_pct") or 0.0))
        if d > CHARS_TOLERANCE:
            gaps.append(f"字数：{cur} / 目标 {target_chars}（偏差 {d:+.0f}%，"
                        f"容差 ±{CHARS_TOLERANCE:.0f}%）")
    if m.get("cliche_hits"):
        gaps.append(f"套话命中 {m['cliche_hits']} 处（契约常明令不用套话）")
    if m.get("placeholders"):
        gaps.append(f"占位符 {m['placeholders']} 处（TODO/待补/XX）")
    seq = m.get("cite_first_order") or []
    if seq and list(seq) != sorted(seq):
        gaps.append(f"引注首现序乱：{list(seq)}（应按 1,2,3… 首次出现）")
    if m.get("has_refs") and not m.get("refs_n"):
        gaps.append("检测到参考文献表但条数为 0（引注 ↔ 文献表对不上）")

    # 启发式硬问题只作建议，不冒充契约硬约束。
    advisory = [i.line() for i in rep.hard() if i.line() not in gaps]
    advisory += [i.line() for i in rep.soft()]

    lines = [TITLE]
    if gaps:
        lines.append("还差这些（改完再交）：")
        lines += [f"  · {g}" for g in gaps]
    else:
        lines.append("硬约束全达标，可继续写，不必反复量稿。")
    lines.append(f"当前字数（{unit}）：{cur}；稿面评分 {rep.score}/100；"
                 f"内核实测 {len(m)} 项指标（附后）。")
    if advisory:
        lines.append(f"另有 {len(advisory)} 条启发式建议，不阻断交付，按需参考。")

    alignment = None
    if sources is not None or read_docs is not None or searched_docs is not None:
        from .source_alignment import align as _align
        alignment = _align(text or "", sources=sources, read_docs=read_docs,
                           searched_docs=searched_docs, min_score=min_score)
        if alignment["has_alignment"]:
            lines.append("")
            lines.append(alignment["text"])

    return {
        "text": "\n".join(lines),
        "human": rep.format_for_model(),
        "metrics": m,
        "gaps": gaps,
        "advisory": advisory,
        "score": rep.score,
        "unit": unit,
        "chars": cur,
        "needs_revision": bool(gaps),
        "alignment": alignment,
    }
