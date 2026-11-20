# -*- coding: utf-8 -*-
"""写作前技能简报：把本任务阶段技能的入口正文**确定性**送到模型眼前。

═══════════════════════════════════════════════════════════════════════
解决的是什么
═══════════════════════════════════════════════════════════════════════
`loop.py` 的 SYSTEM 只写「进阶段前先 `load_skill(name)` 读入口」——
这是**提示词里的行为要求**，不是内核保证。技能在不在模型眼前，取决于它
这一步想不想读、读几份、读没读入口文档。实测 D2 挂了 `paper-writing`
仍是模型自己连着调 `load_skill` 去拿规范，步数 26、输入 token 比不带预设
多 92%。

这里做的事很小：**开场装配时，由内核一次性把点名技能的入口摘要拼进
SYSTEM**，之后整场冻结（不每步重算，不破坏前缀缓存）。于是

  · 规范**确定性在场** ⇒ 一次写对的概率提高，少一次打回；
  · 多次 `load_skill` 往返收敛成一次装配 ⇒ 少几步、少几轮全量历史；
  · 深读仍走 `load_skill(name, file=…)` —— 渐进披露，不把 195 KB 全塞进前缀。

═══════════════════════════════════════════════════════════════════════
三条纪律
═══════════════════════════════════════════════════════════════════════
1. **baseline 逐字节等价**：不挂载能力 ⇒ 没有贡献 ⇒ 一个字都不加。
2. **缺件 ≠ 坏件**：本机没装 `nature-*` 时如实列出「未注入」，不抛错、
   不把预设判成 broken、也不假装注入过（拦沉默，不拦选择）。
3. **预算是硬的**：`max_chars` 必填无默认值，超了截断并如实说明，
   不让「前置」变成「每步多背一大段」。

⚠️ 权衡要写清：简报进入 SYSTEM 后每步仍在上下文里（全量历史逐步重发），
   净收益来自「少几次工具往返 + 少一次打回」，不是「上下文变短」。
   这一层要和 `loop.py` 的历史压缩一起看，不能单独宣称省钱。
"""
from __future__ import annotations

from typing import Any

from .stages import read_skill_text

BRIEF_TITLE = "【写作前必读 · 内核确定性前置】"


def build_brief(skills: list[str] | tuple[str, ...],
                max_chars: int) -> dict[str, Any]:
    """构建简报。返回 `{"text", "injected", "missing", "chars"}`。

    `text` 为空 = 一份都没注进去（此时 `missing` 必须非空，调用方要把
    这件事说出口）。绝不因为读不到技能就抛异常 —— 干净 clone 上没有
    `~/.agents/skills` 是常态，不是错误。
    """
    names = [str(s).strip() for s in (skills or []) if str(s).strip()]
    try:
        budget = int(max_chars)
    except (TypeError, ValueError) as e:
        raise ValueError(f"max_chars 不是整数：{max_chars!r}") from e
    if budget < 200:
        raise ValueError(f"max_chars 太小（{budget}），至少 200")
    empty = {"text": "", "injected": [], "missing": [], "chars": 0}
    if not names:
        return empty

    per = max(300, budget // len(names))
    parts: list[str] = []
    injected: list[str] = []
    missing: list[str] = []
    for name in names:
        txt = read_skill_text(name, limit=per)
        if not isinstance(txt, str) or not txt.strip() or txt.startswith("⛔"):
            missing.append(name)
            continue
        # ★ 内核原生技能（KERNEL_SKILLS）直接返回全文、**不理 limit**。
        #   不在这里兜一道，30 条踩坑清单会一口吃满整份预算，后面点名的
        #   技能全被整体截断掉 —— 「挂了却没到模型眼前」的静默失败。
        if len(txt) > per:
            txt = txt[:per] + "…（入口摘要，已截断；深读用 load_skill）"
        injected.append(name)
        parts.append(
            f"── {name}（入口摘要；深读仍用 load_skill(name, file=…)）──\n{txt}")

    if not parts:
        return {**empty, "missing": missing}

    note = ""
    if missing:
        note = ("\n\n（以下技能本机不可用，未注入，不算坏件："
                + "、".join(missing) + "）")
    text = BRIEF_TITLE + "\n" + "\n\n".join(parts) + note
    if len(text) > budget:
        # ★ 截断提示自己也要占预算：先留够 tail 的长度再截，
        #   否则「截断后」反而比预算更长（实测 4000 → 4028，假守门）。
        tail = "\n…（简报超预算，已截断；深读用 load_skill）"
        text = text[:max(0, budget - len(tail))] + tail
    return {"text": text, "injected": injected,
            "missing": missing, "chars": len(text)}
