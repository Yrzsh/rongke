# -*- coding: utf-8 -*-
"""「哪些能力值得做成给 AI 的产出工具」准入单源（2026-11-11）。

## 用户实证（本文件的前提，不是推测）

数据图 / PPT / 排版三套专门工具做出来以后，**成品明显更好**，属于根源性提升。
用户 2026-11-11 22:25 明确纠正过一次因果链：这说的是**产出质量**，
不是「工具有没有被模型调用」。把 R1/R8 的 0 次调用拿来反驳"别再扩工具"
是认错了轴 —— 那只说明**提升覆盖不到某些稿子**，不否定提升本身。

先把「根源」认准：这三套都是**产出侧工具**——替模型生成产出物的形态。
`writing_metrics`（量字数与引注）、`source_alignment`（核引文出处）
虽然也是工具，却是**查询/测量侧**：只回读数，成品仍由模型自由写，
所以不会带来"成品更好"。项目里早就"把别的能力做成工具"了，
差别在这里，不在数量。

## 2026-11-12 补层：事实源先于产出面

用户接着提出「写综述的查文献 / 文献池 / 精读 / 敲定大纲能不能做成工具」。
核实后：检索侧工具**早就有**（`kb_coverage` / `literature_search` /
`literature_fetch` / `kb_search` / `kb_read` / `verify_citation`），
11-09 六环节综述工作流也跑通过。真正缺的是两件：
①「不全面 / 相关性低」**没有任何可判定基准**（`coverage.py` 判的是库覆盖，
`_kb_rank` 直接取上游 score，全仓无纳入排除准则）；
② 元数据/全文、精读笔记、大纲、正文引注、检索回包**五处互不互证**。

⇒ 上一轮的三层落位少了一层：**事实源层**。产物存在之前，先得有池子。
本目录因此加 `FACT_SOURCE` / `WORKFLOW` / `JUDGEMENT_SPLIT` 三件，
配套实现 `rongke/litpool.py`（`文献池.json`）。
⚠️ 相关性**语义判断仍归人**：工具只保证「准则先声明、逐篇判过、
理由与页码留痕、能回溯全文」。别造一个假的相关性分数。

## 边界（不粉饰）

  · 开发期资产：**不进 `/api/tools`**、不改 SYSTEM、不加硬门、不碰现有枚举。
  · `VALIDATED` / `QUERY_CONTRAST` 是**源码读回**（AST，带命中符号）；
    `CANDIDATES` 是**声明式**：还没实现，`basis="declared"` 明写未实测。
  · C1 coverage 是次要维度，只用于「扩的时候顺手补接线」，**不参与准入否决**。
  · 判据只量能量到的结构事实；"基准是否独立于被测对象"这类语义
    由配套判据的承重自检负责，本目录不假装量过。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READBACK_DATE = "2026-11-11"

#: 产出侧准入六条。前四条是「成品会变好」的必要条件，后两条是接入纪律。
CRITERIA: dict[str, dict[str, str]] = {
    "deterministic_form": {
        "name": "形态由工具算",
        "question": "模型只声明规格，不写输出格式字节吗？",
        "why": "三套工具的共同本体：坐标/版式/页序/编号由确定性代码算，"
               "模型手写就必然撞框、错页序、错编号。",
    },
    "spec_single_source": {
        "name": "规格或体例单源",
        "question": "有一份 SPEC_GUIDE / STYLES / SUPPORTED_KINDS 被多处复用吗？",
        "why": "描述、参数、提示段三处抄写必然漂移；单源才可判定、才可加种类。",
    },
    "quality_measurable": {
        "name": "成品质量可判定",
        "question": "有没有对**成品**跑的门/审计函数，而不只是流程证据？",
        "why": "六道图面门、svgq 七道门、typeset 逐块读回 —— 质量不是靠门抓，"
               "但门必须量得到成品。",
    },
    "readback": {
        "name": "写后按结构读回",
        "question": "落盘后逐块/逐图元/逐页读回，而不是只信写入成功？",
        "why": "证据只认读回；「写成功」/外层 exit code 都不算。",
    },
    "schema_visible": {
        "name": "在册且 schema 可见",
        "question": "工具真注册了、描述里带规格口径吗？",
        "why": "挂上了 ≠ 够得着；常驻插件不进 SYSTEM，靠工具表与回包。",
    },
    "advisory": {
        "name": "质量与放行分离",
        "question": "质量走 quality/next、不改 ok/scope、不加硬门吗？",
        "why": "advisory 才能既说出口又不拦选择。",
    },
}

#: 覆盖维度：不否决准入，只标出"收益能覆盖多少稿子"。
COVERAGE = {
    "id": "coverage",
    "name": "默认路径覆盖",
    "why": "R1/R4/R8/R9：`visual_renumber` 默认话术 0 次；"
           "R9 真正补回图块的是 `figure_guard` 自动 reconcile。"
           "这是**调度**问题，不推翻「用了就更好」，只说明扩工具时该顺手补接线。",
}

#: 硬否决：这类能力做成工具只会造出「有牙的假判据」。
VETO = {
    "domain_judgement": "需要领域判断（论证是否成立 / 文献是否恰当 / 结论是否创新 / "
                        "该用哪种统计检验）—— writing.py:19-20、eval/README.md:7-9 明写不判",
    "no_form_spec": "产出物没有确定形态规范（正文叙述、语言润色）—— "
                    "工具算不了形态，只会把自由文本搬进 schema",
}

#: 三套已被用户实证「成品明显更好」的产出侧工具。
#: 每条给后端源码与证据，判据从这里读回，不手填 bool。
VALIDATED: dict[str, dict] = {
    "data-figure": dict(
        name="数据图", backend="rongke/svgplot.py",
        extra=("rongke/figure.py", "rongke/analysis.py"),
        tools=("plot_svg",),
        gates=("_gate_source", "_gate_collision", "_gate_glyph_font",
               "svgplot_test.py", "svgq_test.py"),
        quality_keys=("checks", "gates", "quality", "hard", "blocking"),
        evidence="svgplot.py:63 SUPPORTED_KINDS 14 图型单源、:383 render_svg；"
                 "figure.py:1315-1556 六道图面门；analysis.recommend 按列形状选图型",
        note="AI 给数据与图型，工具算坐标/轴/图例并过门"),
    "ppt": dict(
        name="PPT", backend="rongke/deck.py",
        extra=("rongke/caps_ext/deck_desk.py",),
        tools=("deck_make", "deck_slide", "deck_plan", "deck_desk"),
        gates=("audit_quality", "check_slide", "deck_desk_test.py",
               "deck_test.py"),
        quality_keys=("checks", "gates", "quality", "hard", "soft", "hint",
                      "action", "next"),
        evidence="deck_desk.py:66-67 deck_make(spec) 一次成稿、deck_slide(index) 改单页；"
                 ":583 全通过才替换交付件 + spec sidecar；:755 next 给规格模板",
        note="AI 给页内容，工具算版式/页序/讲稿备注，增量改单页"),
    "typeset": dict(
        name="格式排版", backend="rongke/typeset.py",
        extra=("rongke/caps_ext/typeset_desk.py",),
        tools=("typeset", "typeset_export"),
        gates=("audit_quality", "_numbering_ok", "_caption_order_ok",
               "typeset_test.py"),
        quality_keys=("quality", "next", "checks", "hard", "soft"),
        evidence="typeset.py:80 ROLES / STYLES 三档体例单源、:619-664 逐块读回；"
                 "HTML/docx/tex 三路 + audit_quality 复用三套判据",
        note="AI 给 blocks，工具算版式并三路导出，逐块读回"),
}

#: 反例：**已经做成工具、却不是产出侧**，因此不带来成品提升。
QUERY_CONTRAST: dict[str, dict] = {
    "writing_metrics": dict(
        file="rongke/caps_builtin.py",
        evidence="caps_builtin.py:279-298 注册进 registered_tools（预设撤销清单）；"
                 "控制台中文名「量稿件字数与引注」；F 场 0 次调用",
        note="只回字数/引注读数，成品仍由模型写"),
    "source_alignment": dict(
        file="rongke/caps_builtin.py",
        evidence="caps_builtin.py:336-369 预设层；"
                 ".workbuddy/memory/MEMORY.md:28「注册了仍 0 次调用」",
        note="只回引文—原文对齐三态，不替换产出物"),
}

#: 2026-11-12 补层：写作工作流讨论暴露出**事实源层先于产出面**。
#: 上一轮只分了「加种类 / 新开面 / 否决」，那是产物已经存在之后的事；
#: 综述链路的病是「产物各自持有事实、谁也不读谁」——先要有池子。
FACT_SOURCE = {
    "principle": "稳定 id 扛身份、其余都是投影；写后读回；多环互证 "
                 "（同 figure_registry 的五连、journal 的 append-only）",
    "existing": (
        {"id": "shore-journal", "state": "entries.jsonl",
         "module": "rongke.journal", "note": "手记唯一事实源，逐日 .md 全是视图"},
        {"id": "figure-registry", "state": "图表登记.json",
         "module": "rongke.figure_registry", "note": "稳定 fig:/tab: id，编号降级为渲染产物"},
        {"id": "litpool", "state": "文献池.json",
         "module": "rongke.litpool",
         "note": "2026-11-12 新增：检索台账 / 判定留痕 / 精读卡 / 章映射 / 五连读回"},
    ),
}

#: 「判断归人，留痕归工具」——相关性语义不能工具化，但「判没判」可以。
JUDGEMENT_SPLIT = {
    "human": "这篇文献相不相关 / 论证是否成立 / 结论是否创新",
    "tool": "准则是否先声明、是否逐篇判过、理由与页码有没有留痕、"
            "能否回溯到全文、大纲引用是否 ⊆ 已纳入且已精读的池",
    "why": "writing.py:19-20 不评判文献恰当与结论创新；"
           "给相关性造一个假分数只会重演「判据有牙却没基准」。",
}

#: 用户 2026-11-12 提的写作工作流四段，逐段对账：早有工具 ≠ 早有事实源。
WORKFLOW: tuple[dict, ...] = (
    {"stage": "查文献", "holders": 2, "existing": "kb_coverage / literature_search / "
     "literature_fetch / kb_search / kb_read / verify_citation",
     "gap": "召回与相关性**无可判定基准**：coverage.py 只判库覆盖，"
            "_kb_rank 直接取上游 score；无纳入/排除准则、无结果级去重",
     "now": "litpool.record_query 记召回台账（n_returned/n_dup/n_new）"
            "——只是下界读数，不是质量结论",
     "next": "接 literature_search 真实回包 + 把 query 扩展从库内搬到学术检索侧"},
    {"stage": "文献池", "holders": 5, "existing": "无；元数据在外部 litkb、精读在 READING.md/"
     "EVIDENCE.md、大纲在 outline.md、引注在稿件，五处互不互证",
     "gap": "没有唯一事实源；证据表「只能手抄」"
            "（docs/mentor-mode-20260922.md:161）",
     "now": "litpool 文献池.json：稳定 doc_id + 写后逐字节读回",
     "next": "让 kb_add / convert_pdf / kb_read 都往池里写，别再各存各的"},
    {"stage": "精细化阅读", "holders": 3,
     "existing": "kb_read 按页定位 / convert_pdf 转 Markdown",
     "gap": "逐篇读吃掉步数（11-09 B 场 kb_read 73 次、A 场 40 步耗尽）；"
            "读完的结论不落结构化对象，下次重写还得再读",
     "now": "litpool.add_card 精读卡 claim/page/quote，且必须能回溯全文",
     "next": "接成批量精读：一次多卡、按章聚合，别逐篇调工具"},
    {"stage": "基于池敲定大纲", "holders": 2,
     "existing": "outline.md 自由文本 + gate 第 10 条大纲门",
     "gap": "门只核「每章有没有 ≥1 个 doc_id」，不核「对不对」；"
            "「每章引用 ⊆ 映射清单」只是提示词（综述流程方案:414 待办）",
     "now": "litpool.assign_chapters 挡池外/未纳入/未精读；"
            "verify 五连读回把大纲与正文引注拉回同一份池",
     "next": "把越界检查接成 advisory 简报，不改硬门"},
)


#: 候选产出能力。`basis="declared"` = 没实测，别当成已验证。
#: form_spec / measurable / producer 只是在问「有没有确定形态规范 / 独立基准 /
#: 是不是替模型产出」，不冒充「已经做成了」。
#: attach 的默认去处：**往已有产出面里加种类**，不是每个能力新开一个工具。
#: 三个产出面复用同一套规格单源 / 质量门 / 写后读回，边际成本最低；
#: 只有跨全文身份、放不进任何单一产物的事实源才 `new-surface`。
ATTACH = {
    "typeset": "加种类：格式排版面",
    "data-figure": "加种类：数据图面",
    "ppt": "加种类：PPT 面",
    "new-surface": "新开产出面（须走满六条准入）",
    "none": "不工具化",
}


def _c(name, owner, evidence, form_spec, measurable, producer,
       batch, note, veto="", attach="typeset"):
    return dict(name=name, owner=owner, evidence=evidence,
                form_spec=form_spec, measurable=measurable, producer=producer,
                batch=batch, veto=veto, attach=attach,
                basis="declared", note=note)


CANDIDATES: tuple[dict, ...] = (
    _c("引注与参考文献表编排", "模型自由写 [n] / 〔n〕 + 文献表",
       "docs/质量与效率卡点定位-2026-11-04.md Q4:162-171：A 场 13 处全角引注"
       "被 writing.py:97 判 citations=0 / cite_numbers=0 / has_refs=0（假绿）；"
       "writing.py:244 citation_structure 已有台账；图表登记单源已解决同类问题",
       True, True, True, "P0",
       "跨正文与文献表的身份，放不进单一产物；与 figure_registry 同构："
       "doc_id 扛身份、编号降级为渲染产物、五连读回",
       attach="new-surface"),
    _c("论文表格（三线表 / 跨页 / 表题）", "模型手写 Markdown 表格",
       "typeset.py 已有 table role 与 Table Grid 导出，但没有登记单源；"
       "docs/排版质量提升-2026-11-11.md 记录 docx 表格读回 8 项 checks",
       True, True, True, "P0",
       "表题/单位/脚注/跨页续表都有确定形态；模型手写必错位"),
    _c("公式与编号 / 交叉引用", "模型自由写 LaTeX 行",
       "typeset.py 的 tex 路已含 fancyhdr + \\thepage；"
       "公式编号与图表编号同属「编号降级为渲染产物」",
       True, True, True, "P1",
       "零依赖渲染范围要谨慎：先做编号与引用登记，不做排版引擎"),
    _c("投稿包裹（封面 / 投稿信 / 声明页）", "模型自由写文本",
       "typeset.py STYLES 三档体例（journal-cn/thesis/preprint）已单源；"
       "docs/GitHub资源地图-工具种类扩充-2026-11-11.md 记 CSL 是 CC BY-SA 只取事实",
       True, True, True, "P1",
       "期刊模板差异是确定形态；只做版式，不做内容判断"),
    _c("附录与补充材料清单", "模型自由列文件",
       "paper_desk 只读汇总写/画/排/讲/投，但无补充材料登记",
       True, True, True, "P2",
       "与图表登记同族：文件清单 ↔ 正文引用 ↔ 成品附件三连读回",
       attach="new-surface"),
    _c("审稿意见 point-by-point 回应表", "模型自由写段落",
       "docs/评审回应-2026-09-19.md 已有人工版本可作事实来源",
       True, True, True, "P2",
       "意见编号 ↔ 回应 ↔ 改动位置是稳定结构；但要先有真稿 diff"),
    _c("正文论证与语言润色", "模型自由写文本",
       "rongke/writing.py:19-20 明写不评判论证/文献恰当/结论创新；"
       "eval/README.md:7-9 判据不测科学结论正确性",
       False, False, False, "VETO",
       "没有确定形态规范，工具算不了",
       veto="no_form_spec", attach="none"),
    _c("图注统计检验方法 / 结论创新性", "人或模型自由填",
       "docs/产品说明.md:320-321：统计方法不能由程序代填，留 ___ 占位",
       False, False, False, "VETO",
       "需要领域判断，工具化会造出假牙",
       veto="domain_judgement", attach="none"),
)

_SPEC_NAMES = ("SPEC_GUIDE", "STYLES", "DESIGN", "STATE_NAME",
               "VOICE", "ROLES", "SUPPORTED_KINDS", "NODE_KINDS")
_OUTPUT_BYTES = ("<svg", "<!DOCTYPE html", "w:document",
                 "\\documentclass", "<w:document")
_READBACK_NAMES = ("read_text", "readback_equal", "state_readback_equal",
                   "every_block_present", "_write_back", "render_markdown")


def _src(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _tree(src: str) -> ast.Module:
    try:
        return ast.parse(src)
    except SyntaxError:
        return ast.parse("")


def _names(tree: ast.Module) -> list[str]:
    """AST 名字，不是字符串包含：注释 / docstring 里的词不算命中。"""
    out: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.append(n.id)
        elif isinstance(n, ast.Attribute):
            out.append(n.attr)
        elif isinstance(n, ast.alias):
            out.append(n.name)
    return out


def _str_keys(tree: ast.Module) -> set[str]:
    """只收 dict 键 / 下标 / 关键字参数名 —— 排除 docstring 造成的假命中。"""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Dict):
            for k in n.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out.add(k.value)
        elif isinstance(n, ast.Subscript):
            s = n.slice
            if isinstance(s, ast.Constant) and isinstance(s.value, str):
                out.add(s.value)
        elif isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg:
                    out.add(kw.arg)
    return out


def _str_literals(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
    return out


def _assigns(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            tg = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in tg:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def _func_names(tree: ast.Module) -> set[str]:
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _attr_calls(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if isinstance(v, ast.Name):
                out.add(f"{v.id}.{n.func.attr}")
    return out


def inspect_validated(key: str) -> dict:
    """三套已验证工具：六条准入逐条从源码 AST 读回。"""
    meta = VALIDATED.get(key)
    if not meta:
        return {"id": key, "verdict": "unknown", "error": "未登记"}
    paths = (meta["backend"],) + meta["extra"]
    trees = [(p, _tree(_src(p))) for p in paths]
    names = [n for _, t in trees for n in _names(t)]
    assigns = set().union(*[_assigns(t) for _, t in trees])
    keys = set().union(*[_str_keys(t) for _, t in trees])
    funcs = set().union(*[_func_names(t) for _, t in trees])
    literals = [s for _, t in trees for s in _str_literals(t)]
    ev: dict[str, str] = {}
    ck: dict[str, bool] = {}

    # 1 形态由工具算：暴露给 AI 的插件层零输出字节字面 + 后端确有 render/export
    #    （后端当然要写字节——那是工具在写，不是模型在写）
    plugin_src = "".join(_src(p) for p in meta["extra"]
                         if "caps_ext" in p)
    byte_hit = sorted({b for b in _OUTPUT_BYTES
                       if any(b in s for s in literals) and b in plugin_src})
    renderers = sorted(f for f in funcs
                       if f.startswith(("render", "export", "make_", "build_")))
    ck["deterministic_form"] = bool(renderers) and not byte_hit
    ev["deterministic_form"] = (
        f"后端 render/export 函数 {renderers[:6] or '无'}；"
        f"插件层输出字节字面 {byte_hit or '0 处'}")

    # 2 单源：顶层声明的规格常量，且名字在 AST 里被复用 ≥2 次
    declared = sorted(n for n in _SPEC_NAMES if n in assigns)
    reused = {n: names.count(n) for n in declared if names.count(n) >= 2}
    ck["spec_single_source"] = bool(reused)
    ev["spec_single_source"] = (f"顶层声明 {declared or '无'}；"
                                f"复用 {reused or '无'}（≥2 次才算单源）")

    # 3 成品质量可判定：门函数 / 专属判据文件**读回确实在**（≥2 个）。
    #    基准是 VALIDATED.gates 这份事实源清单，不是猜函数名前缀 ——
    #    typeset 复用三套外部判据、figure.py 的门是类名下方法，前缀猜不到。
    rg_src = _src("regress.py")
    hits = []
    for g in meta["gates"]:
        if g.endswith(".py"):
            present = (ROOT / g).exists() and g in rg_src
        else:
            present = g in funcs or any(g in s for s in literals)
        if present:
            hits.append(g)
    ck["quality_measurable"] = len(hits) >= 2
    ev["quality_measurable"] = (f"门/判据读回 {len(hits)}/{len(meta['gates'])}："
                                f"{hits or '无'}")

    # 4 写后按结构读回
    rb = sorted(n for n in _READBACK_NAMES if n in names)
    ck["readback"] = bool(rb)
    ev["readback"] = f"读回符号 {rb or '无'}"

    # 5 在册且 schema 可见：工具名出现在工具表，描述/参数带规格口径
    tools_js = _src("rongke/web/static/workspace.js")
    listed = sum(1 for t in meta["tools"] if f'"{t}"' in tools_js
                 or f"{t}:" in tools_js or f'"{t}"' in
                 "".join(_src(p) for p in paths))
    ck["schema_visible"] = listed > 0
    ev["schema_visible"] = f"工具名在册 {listed}/{len(meta['tools'])}"

    # 6 质量与放行分离：回包有 ok（结构自检）+ 至少一个质量/指引字段。
    #    不认死 quality/next 一种写法：数据图走 checks/gates、PPT 走 hint/action。
    quality_hits = sorted(keys & set(meta["quality_keys"]))
    ck["advisory"] = "ok" in keys and bool(quality_hits)
    ev["advisory"] = (f"ok 键={'ok' in keys}；质量字段 {quality_hits or '无'}"
                      f"（dict 键/下标，非字符串包含）")

    # 覆盖：loop.py 里对该能力有确定性属性调用（不否决准入）
    loop_calls = _attr_calls(_tree(_src("rongke/loop.py")))
    wired = sorted(c for c in loop_calls
                   if any(k in c for k in meta["tools"]))
    cov = bool(wired)
    return {"id": key, "name": meta["name"], "tools": list(meta["tools"]),
            "note": meta["note"], "evidence_user": meta["evidence"],
            "checks": ck, "evidence": ev, "coverage": cov,
            "coverage_evidence": f"loop.py 确定性调用 {wired or '无'}",
            "verdict": _verdict(ck), "basis": "readback"}


def inspect_query(tool: str) -> dict:
    """反例：查询/测量侧工具，不替换产出物 ⇒ 不是同一类，别算进「三套」。"""
    meta = QUERY_CONTRAST.get(tool)
    if not meta:
        return {"id": tool, "verdict": "unknown"}
    src = _src(meta["file"])
    t = _tree(src)
    # 字符串常量才是证据（"registered_tools" 是 setdefault 的实参，不是 Name）
    in_registry = any("registered_tools" in s for s in _str_literals(t))
    tool_declared = any(tool in s for s in _str_literals(t))
    return {"id": tool, "tools": [tool], "note": meta["note"],
            "in_registry": in_registry, "tool_declared": tool_declared,
            "checks": {"deterministic_form": False, "spec_single_source": False,
                       "quality_measurable": False, "readback": False,
                       "schema_visible": tool_declared, "advisory": False},
            "evidence": {"preset": meta["evidence"],
                         "registered_tools": f"预设撤销清单={in_registry}",
                         "producer": "只回读数，不生成产出物"},
            "verdict": "query_not_producer" if in_registry and tool_declared
            else "unknown",
            "basis": "readback"}


def _verdict(ck: dict[str, bool]) -> str:
    core = ("deterministic_form", "spec_single_source",
            "quality_measurable", "readback")
    if all(ck.get(k) for k in core):
        return "exemplar" if all(ck.values()) else "partial"
    return "gap"


def candidate_report() -> list[dict]:
    """候选排序：先否决，再看是不是「有形态规范的产出侧」。"""
    out = []
    for c in CANDIDATES:
        if c["veto"]:
            v, why = "reject", VETO[c["veto"]]
        elif c["form_spec"] and c["measurable"] and c["producer"]:
            v, why = "qualify", "有确定形态规范 + 可判定基准 + 替模型产出 ⇒ 同构可复制"
        else:
            v, why = "blocked", "缺形态规范或独立基准，先补事实源再谈工具"
        out.append(dict(c, verdict=v, reason=why))
    return out


def placement() -> dict[str, list[dict]]:
    """第一批该往哪落：加种类 / 新开产出面 / 否决。

    默认去处是**加种类**——三个产出面已有规格单源、质量门、写后读回，
    复用它们的边际成本远低于再开一个常驻工具（也不会再制造一个
    「注册了但默认路径 0 次调用」的入口）。
    """
    out: dict[str, list[dict]] = {"add_kind": [], "new_surface": [],
                                  "reject": [], "blocked": []}
    for c in candidate_report():
        if c["verdict"] == "reject":
            out["reject"].append(c)
        elif c["verdict"] == "blocked":
            out["blocked"].append(c)
        elif c["attach"] in ("typeset", "data-figure", "ppt"):
            out["add_kind"].append(c)
        elif c["attach"] == "new-surface":
            out["new_surface"].append(c)
        else:
            out["blocked"].append(c)
    return out


def workflow() -> dict:
    """写作工作流四段对账：哪些早有工具、哪些缺事实源、本轮补了什么。"""
    return {
        "stages": len(WORKFLOW),
        "with_existing_tools": sum(1 for w in WORKFLOW if w["existing"]),
        "with_gap": sum(1 for w in WORKFLOW if w["gap"]),
        "with_new_source": sum(1 for w in WORKFLOW if w["now"]),
        "max_holders": max(w["holders"] for w in WORKFLOW),
        "all_have_next": all(w["next"] for w in WORKFLOW),
        "rows": [dict(w) for w in WORKFLOW],
    }


def assess_all() -> list[dict]:
    rows = [inspect_validated(k) for k in VALIDATED]
    rows += [inspect_query(k) for k in QUERY_CONTRAST]
    return rows


def summary() -> dict:
    rows = assess_all()
    return {
        "readback_date": READBACK_DATE,
        "validated": len(VALIDATED),
        "query_contrast": len(QUERY_CONTRAST),
        "verdicts": {v: sum(1 for r in rows if r["verdict"] == v)
                     for v in sorted({r["verdict"] for r in rows})},
        "coverage_true": sum(1 for r in rows if r.get("coverage")),
        "candidates": {v: sum(1 for c in candidate_report() if c["verdict"] == v)
                       for v in ("qualify", "blocked", "reject")},
        "placement": {k: len(v) for k, v in placement().items()},
        "fact_sources": len(FACT_SOURCE["existing"]),
        "workflow": {k: v for k, v in workflow().items() if k != "rows"},
        "batches": {b: sum(1 for c in CANDIDATES if c["batch"] == b)
                    for b in ("P0", "P1", "P2", "VETO")},
        "basis_note": "VALIDATED/QUERY_CONTRAST 为源码读回；CANDIDATES 为声明式，未实测",
    }
