# -*- coding: utf-8 -*-
"""外部能力插件：把「检索文献」这条工作流做成可挂载的一行。

## 为什么放在 caps_ext/ 而不是内核

改前，「检索文献」是**硬编码**的：SYSTEM 里写着第 9 步才动笔、
`contract.py` 里写着 `literature_fetch` 的三条路、`loop.py` 无条件注册
`verify_citation` / `literature_fetch`。预设里就算选了「文献检索」胶囊，
也只是切到 `lit-verify` 改几个门禁指标 —— 检索纪律本身既不能换，也不能关，
更不能按任务换一套工具箱。

本文件把这件事收成**一个能力行**：预设点一行就有，删掉这个 `.py` 就
整条退回原样。内核零新增特判，只消费已有的通用贡献点：

    SECTION_TEXTS      → 检索纪律正文（prompt.sections 点名才拼进 system）
    ESSENTIAL_TOOLS    → 本场工具预算裁剪时**不许裁掉**的检索工具面
    SYSTEM_SECTIONS    → 保留 writing 节（检索是写作流水线的一段）
    EXTRA_RULES        → 有真实分母的门禁规则（refs_n，来自 writing.lint）

四条都是「能力贡献、内核遍历」，不是「内核按 id 问某个插件在不在」。

## 三条不许破

1. **工具名先查 `/api/tools` 再写**：`literature_search(query/limit/sources)`、
   `literature_fetch`、`fetch_doi`、`verify_citation`、`kb_add`、`kb_search`、
   `build_references` 都在册。提示段里点一个不存在的工具 = 给模型一条
   永远走不通的路。
2. **配置必须接活**：`sources` 会真的拼进提示段正文，
   `require_citations` 会真的决定门禁规则在不在。只躺在 JSON 里的值不算生效。
3. **工具不可达要如实说出口**：`literature_search` 来自外部 workbench，
   客户机没连 MCP 时它不在 registry。这不是坏件（能力本身没坏），
   但也不许假装「检索插件已全量生效」—— `provides()` 里必须点名缺了谁。
"""
from __future__ import annotations

import re

from rongke.caps import Capability, register

_SECTION = "external-lit-search"

#: 检索工具链。**必留**与**可达性检查**读同一份名单，不许两处各抄一遍。
#: `deliver_task` / `report_blocker` 是内核自己的交付通道，由 tools.py 管，
#: 不在这份名单里（插件不该抢内核的职责）。
TOOL_CHAIN = (
    "kb_search",            # ① 先查本地库
    "literature_search",    # ② 学术四源找候选
    "literature_fetch",     # ③ 取全文（Europe PMC / Unpaywall / doi.org）
    "fetch_doi",            # ④ 只取元数据
    "verify_citation",      # ⑤ 逐条核验存在性 / 撤稿 / 元数据一致
    "kb_add",               # ⑥ 入库
    "build_references",     # ⑦ 整理并核验参考文献表
)

_TEXT = """【文献检索纪律（外部能力 · lit-search）】
1. 先查本地库：`kb_search(query=…)`。库里没有，先明说「库里没有」，再联网补，
   不要默认全网都是空的，也不要把库空说成「世上没有相关研究」。
2. 找候选只用 `literature_search(query=…, limit=…, sources=…)`；网页/灰色文献
   才用 `web_search`。两个工具返回的东西不同，不许互相冒充。
3. 候选必须留下标题 / 年份 / DOI / 出处。**不许凭记忆补 DOI、作者或年份**：
   没拿到的字段就写「未取得」，编一条看起来对的参考文献比没有更糟。
4. 要全文用 `literature_fetch(dois=[…])`；它没取到就如实说「这几条路这次没成」，
   不许把标题或摘要当全文用。`fetch_doi` 只给元数据，不判撤稿、也不算核验。
5. 引用前逐条调 `verify_citation`：DOI 是否真实、是否已被撤稿、元数据与稿件
   是否一致。已撤稿的文献不许进参考文献表，也不许拿来支撑结论。
6. 取回的全文用 `kb_add` / `kb_ingest` 入库，参考文献表用 `build_references`
   整理核验。写稿时标清哪句来自哪篇、哪句来自外网，两者不要混成一段。
7. 任一检索工具本场不可用时，明说「这一步没验证 / 没检索」，不要用商量语气
   把没做的动作写成已完成；也不要自己手搓爬虫去撞反爬页面。
"""


@register("@external/lit-search")
class LitSearch(Capability):
    """检索文献 = 一个可挂载的能力行（提示段 + 工具面 + 门禁规则）。"""

    id = "lit-search"
    kind = "tool"
    version = "1.0.0"

    # 必填项一律不给 default —— 与 dsh 的 sampleOverCapGlobResults 事故同族：
    # 装不上的预设必须当场 broken，不许悄悄拿一个默认值糊过去。
    config_schema = {
        "enabled": {"type": "bool", "required": True},
        "sources": {"type": "list", "item_type": "str", "required": True},
        "require_citations": {"type": "bool", "required": True},
    }

    # 类属性：挂载期就能被 available_sections() 看到（段名进白名单）。
    SECTION_TEXTS = {_SECTION: _TEXT}
    SYSTEM_SECTIONS = ("writing",)
    ESSENTIAL_TOOLS = TOOL_CHAIN

    def mount(self, cfg, ctx):
        if not cfg["enabled"]:
            raise ValueError("enabled=false 的能力不该被点名；要关就从预设里删掉这一行")
        if not cfg["sources"]:
            raise ValueError("sources 不能为空：提示段里点名的检索源必须说得出名字")
        self.cfg = cfg
        # ★ 配置接活：sources 真拼进正文，改配置 ⇒ 段正文逐字变化。
        #   只把值存进 self 而不出现在 system 里，就是又一次「自称成功」。
        src_line = ("\n本场检索源（按此顺序，工具不支持时明说）："
                    + "、".join(str(s) for s in cfg["sources"]) + "\n")
        self.SECTION_TEXTS = {_SECTION: _TEXT + src_line}
        # ★ require_citations 接活：它决定门禁规则在不在。
        #   分母是 writing.lint 的 refs_n（参考文献表条目数），是内核实测值，
        #   不是模型的自我陈述。触发词同时要求「检索」与「文献表」，
        #   免得「随便聊两句」这种稿子被一条本不该管的规则打红。
        self.EXTRA_RULES = (
            (("refs_n", re.compile(r"(检索|查).{0,8}文献.{0,16}"
                                   r"(参考文献表|文献表|bibliography)", re.I),
              "gt0", "检索后文献表条目"),)
            if cfg["require_citations"] else ()
        )
        # ★ 可达性检查：没有 registry（--presets --live 的只读挂载检查）
        #   ⇒ 如实说「没验」，不许说「已验证全在」。有 registry 而缺工具
        #   ⇒ 点名缺谁。都不算坏件，但都要在 provides 里说出口。
        registry = ctx.get("registry")
        if registry is None:
            self.tool_status = "unchecked"
            self.missing_tools = ()
        else:
            self.missing_tools = tuple(
                n for n in TOOL_CHAIN if registry.get(n) is None)
            self.tool_status = ("missing" if self.missing_tools else "ok")
        return [f"检索纪律 {_SECTION}（{len(self.SECTION_TEXTS[_SECTION])} 字符）",
                f"必留工具 {len(TOOL_CHAIN)} 个",
                f"门禁规则 {len(self.EXTRA_RULES)} 条",
                f"工具可达性={self.tool_status}"]

    def provides(self) -> list[str]:
        out = [f"tool:{self.id}", f"prompt:{_SECTION}",
               f"system-sections:writing",
               f"essential-tools:{len(TOOL_CHAIN)}",
               f"extra-rules:{len(getattr(self, 'EXTRA_RULES', ()))}"]
        status = getattr(self, "tool_status", "unchecked")
        missing = list(getattr(self, "missing_tools", ()) or ())
        if status == "unchecked":
            out.append("[未验证] 本轮没有 registry，检索工具可达性没测")
        elif missing:
            # 拦沉默：环境缺件不是能力坏，但「挂上了」不等于「全量生效」。
            out.append("[缺件] 本场检索工具不可达：" + "、".join(missing))
        else:
            out.append("检索工具链 7/7 可达")
        return out
