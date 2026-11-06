# -*- coding: utf-8 -*-
"""外部能力示例：把别的 agent 实踩过的「收尾纪律」吸收成一个提示段。

这不是内核代码。放进 `rongke/caps_ext/`、`~/.rongke/caps/`，或
`RONGKE_CAP_EXT` 指向的任意目录，预设里按注册名点一行就能生效；
删掉文件，一切退回原样。判健康仍只认 `MountReport.broken`。
"""
from __future__ import annotations

from rongke.caps import Capability, register

_SECTION = "external-answer-first"

# 出处：dsh persona 实踩（2026-11-03）——「每步反问我一下」是行为问题，
# 冷却只统计工具调用、正文提问不计入 ⇒ 必须写成硬约束 + 禁用句式清单。
_TEXT = """【答复纪律（外部能力 · answer-first）】
1. 先给结论，再给理由；不要用反问句开头，也不要在收尾再抛一个问题。
2. 本轮能直接做完的事就做完，不要用「要不要我……」「这样对吗？」把球踢回去。
3. 只有当两种读法会导致方向完全相反、且继续做下去代价明显更大时，才允许提问；
   一次只问一个，并说明你暂时按哪种读法继续。
4. 不确定时明说「这一步没验证」，不要用商量语气把没做的动作伪装成已完成。
"""


@register("@external/answer-first")
class AnswerFirst(Capability):
    """自带提示文本的能力：`SECTION_TEXTS` 是类属性，挂载期即可被校验。"""

    id = "answer-first"
    kind = "prompt"
    version = "1.0.0"
    # 必填项不给 default —— 与 dsh 的 sampleOverCapGlobResults 事故同一条纪律。
    config_schema = {"enabled": {"type": "bool", "required": True}}
    SECTION_TEXTS = {_SECTION: _TEXT}

    def mount(self, cfg, ctx):
        if not cfg["enabled"]:
            raise ValueError("enabled=false 的能力不该被点名；要关就从预设里删掉这一行")
        return [f"自带提示段 {_SECTION}（{len(_TEXT)} 字符）"]

    def provides(self) -> list[str]:
        return [f"prompt:{_SECTION}"]
