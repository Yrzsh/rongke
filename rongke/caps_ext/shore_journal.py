# -*- coding: utf-8 -*-
"""外部能力：《岸记》记忆插件 —— 把实验记录与规划写成有文体的日记。

这是**插件**，不是内核：放进 `rongke/caps_ext/` 由 `caps.ensure_external_loaded()`
自动发现，预设 `journal.json` 点两行即生效；删掉这个文件，整条退回原样。
健康仍只认 `MountReport.broken`；预设绑单次 `run()`，`finally` 按登记清单撤销工具。

两个能力：
  * `@external/shore-journal-voice`（prompt）：把体例与「岸律」注入 system。
  * `@external/shore-journal`（tool）：五个工具，包住 `rongke.journal.JournalBook`。

纪律：必填 config 不给 default；同名工具属于别人 ⇒ 不覆盖、也不登记撤销；
校验失败**直接抛异常**（`ToolRegistry.call` 会捕获并置 `ok=False`），
绝不返回一句漂亮话把没写入伪装成写入。
"""
from __future__ import annotations

import json
from pathlib import Path

from rongke.caps import Capability, register
from rongke.journal import WATERS, KINDS, THEMES, JournalBook

_SECTION = "external-shore-journal"

_VOICE_TEXT = """【岸记 · 实验手记（外部能力）】
你手边有一册《岸记》。它不是 changelog，也不是验收报告；它是这一渡的航记。
一、什么时候必须记（别等用户提醒）：
  1. 做完一场有读回结果的实验 —— 记「验」：问什么、读回什么、判词一句。
  2. 出现翻案、假红、自己判断错了 —— 记「朱批」，只记这些，不记大路消息。
  3. 收尾 —— 记「收」，并**必须**另写一条「不宣称」：本轮没验证到什么。
  4. 定下一步 —— 记「明日」，并附可核对的验收判据；它会自动出现在规划看板。
二、六段固定：起 / 验 / 朱批 / 收 / 明日 / 锚。开页时先定「水位」：
  潮＝推进且全绿，平＝稳态整理，雾＝读不到或判据没测到，礁＝打红或翻案。
三、硬约束（写之前逐条对）：
  · 「验」的读回必须是**读回来的数字或产物**，带出处；不许写「验证通过」四个字。
  · 判词不超过一句，是压缩不是省略；数字与证据锚一个都不能因为文体被删掉。
  · 「读不到」≠「不符合」；没测到就写没测到，不许算进通过。
  · 每条「明日」都要能被第三方判「做没做」，不允许「后续优化」这种无法验收的话。
  · 日记是视图，台账 `entries.jsonl` 才是单源；**不手改**渲染出来的 .md / .html。
四、禁用句式（报告腔，出现即不合格）：
  「今天完成了以下工作」「综上所述」「已生成文件」「整体效果良好」「后续持续优化」。
  用第一人称写：我今天想弄清什么、动手前撞见了什么、哪一刻我发现自己错了。
五、岸律：证据只认读回 · 读不到 ≠ 不符合 · 判词是压缩，不是省略。
"""


def _root(cfg: dict) -> Path:
    d = Path(str(cfg["dir"]))
    if not d.is_absolute():
        d = Path(__file__).resolve().parents[2] / d
    return d


def _reply(label: str, payload: dict) -> str:
    return (f"{label}\n\n【岸记 · 结构化台账】\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))


@register("@external/shore-journal-voice")
class ShoreJournalVoice(Capability):
    """体例本身是一种能力：不靠模型临场发挥，靠注入的硬约束。"""

    id = "shore-journal-voice"
    kind = "prompt"
    version = "1.0.0"
    config_schema = {"enabled": {"type": "bool", "required": True}}
    SECTION_TEXTS = {_SECTION: _VOICE_TEXT}

    def mount(self, cfg, ctx):
        if not cfg["enabled"]:
            raise ValueError("enabled=false 的能力不该被点名；要关就从预设里删掉这一行")
        return [f"自带提示段 {_SECTION}（{len(_VOICE_TEXT)} 字符）"]

    def provides(self) -> list[str]:
        return [f"prompt:{_SECTION}"]


@register("@external/shore-journal")
class ShoreJournalTool(Capability):
    """五个工具：开页 / 续写 / 读回 / 结案 / 重渲染。"""

    id = "shore-journal"
    kind = "tool"
    version = "1.0.0"
    # 必填项不给 default —— 与 dsh 那次 zod 事故同一条纪律。
    config_schema = {
        "dir": {"type": "str", "required": True},
        "expose": {"type": "bool", "required": True},
        # 可选主题：非必填才允许给 default；必填项依旧一个 default 都没有。
        "theme": {"type": "str", "required": False, "default": "手记"},
    }
    TOOL_NAMES = ("journal_open", "journal_note", "journal_read",
                  "journal_done", "journal_render")

    def provides(self) -> list[str]:
        return [f"tool:{n}" for n in self.TOOL_NAMES]

    def mount(self, cfg, ctx):
        self.cfg = cfg
        if not cfg["expose"]:
            return ["未暴露（岸记只留内核 API）"]
        registry = ctx.get("registry")
        if registry is None:
            return ["已声明暴露，但本轮没有 registry（只读挂载检查）"]
        from rongke.tools import Tool

        theme = cfg.get("theme", "手记")
        if theme not in THEMES:
            raise ValueError(f"岸记主题只认 {list(THEMES)}，得到 {theme!r}")
        book = JournalBook(_root(cfg), theme=theme)

        def _open(date: str = "", water: str = "", question: str = "", **_kw) -> str:
            ev = book.open_day(date, water, question)
            return _reply(f"〔岸记 · 开页〕{ev['date']} 水位「{ev['water']}」：{ev['question']}", ev)

        def _note(kind: str = "", text: str = "", evidence: str = "",
                  verdict: str = "", boundary: str = "", date: str = "", **_kw) -> str:
            ev = book.note(kind, text, evidence=evidence, verdict=verdict,
                           boundary=boundary, day=date)
            return _reply(f"〔岸记 · 续一笔〕{ev['date']} · {ev['kind']}", ev)

        def _read(date: str = "", **_kw) -> str:
            r = book.read_day(date)
            return (f"〔岸记 · 读回〕第 {r['crossing']} 渡 · 水位 {r['water']} · "
                    f"落盘一致={r['same']}\n\n{r['markdown']}"
                    f"\n【岸记 · 结构化台账】\n"
                    + json.dumps({"date": r["date"], "water": r["water"],
                                  "question": r["question"],
                                  "count": len(r["notes"]), "same": r["same"]},
                                 ensure_ascii=False, indent=1))

        def _done(item: str = "", date: str = "", **_kw) -> str:
            ev = book.done(item, day=date)
            return _reply(f"〔岸记 · 已渡〕{item}（原「明日」不删）", ev)

        def _render(**_kw) -> str:
            r = book.render_all()
            return _reply(f"〔岸记 · 重渲染〕{r['events']} 事件 / {r['days']} 渡", r)

        specs = [
            ("journal_open", "开一页《岸记》：定日期、水位、今日问。同日已有页会拒绝覆盖。",
             {"date": {"type": "string", "description": "YYYY-MM-DD"},
              "water": {"type": "string", "description": "潮 / 平 / 雾 / 礁 四选一",
                        "enum": list(WATERS)},
              "question": {"type": "string", "description": "今天想弄清的一件事，不许空"}},
             ["date", "water", "question"], _open, True),
            ("journal_note", "续写一段日记。段落固定六选一；「明日」会同源长进规划看板。",
             {"kind": {"type": "string", "enum": list(KINDS),
                       "description": "起=记、验=实验、朱批=翻案、收=结论、明日=下一步、锚=证据路径"},
              "text": {"type": "string", "description": "正文；「验」写问什么"},
              "evidence": {"type": "string", "description": "读回的数字/产物与出处；「明日」用它写验收判据"},
              "verdict": {"type": "string", "description": "判词，一句；只用于「验」"},
              "boundary": {"type": "string", "description": "不宣称：本轮没验证到什么"},
              "date": {"type": "string", "description": "YYYY-MM-DD；不填续最近一渡"}},
             ["kind", "text"], _note, True),
            ("journal_read", "读回某一渡的全文（含落盘一致性），别凭记忆复述日记。",
             {"date": {"type": "string", "description": "YYYY-MM-DD"}},
             ["date"], _read, False),
            ("journal_done", "规划条目结案。只追加一条 done，原「明日」不删。",
             {"item": {"type": "string", "description": "「明日」原文片段，命中多条要给 date"},
              "date": {"type": "string", "description": "可选：指定哪一渡"}},
             ["item"], _done, True),
            ("journal_render", "按台账重渲染全部日记、规划看板与 HTML 日记本（视图永不手改）。",
             {}, [], _render, True),
        ]
        tools = [Tool(name=n, description=d,
                      parameters={"type": "object", "properties": p, "required": r},
                      fn=f, domain="core", mutating=m, source="plugin")
                 for n, d, p, r, f, m in specs]

        added: list[str] = []
        for t in tools:
            # ★ 同名工具已存在 ⇒ 属于别人，既不覆盖、也不登记撤销。
            if registry.get(t.name) is not None:
                continue
            try:
                registry.register(t)
            except Exception as e:
                raise RuntimeError(f"注册 {t.name} 失败：{type(e).__name__}: {e}") from e
            added.append(t.name)
        ctx.setdefault("registered_tools", []).extend(added)
        return [f"岸记工具 {len(added)}/{len(tools)} 个：{', '.join(added) or '全部已被占用'}"]
