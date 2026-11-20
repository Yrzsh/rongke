# -*- coding: utf-8 -*-
"""九类内置能力 —— 每一类都对应 2026-11-04 实测里的一个真实卡点。

★ 要点：baseline 与 strict-v2 / 五份任务预设用的是**同一批类**，只有 config
  不同。修卡点从「改内核源码」变成「改预设里的一行」。

所有 `mount()` 都只把 config 存进实例（不碰全局状态）；唯一的例外是
`WritingMetricsTool`：`expose=true` 且 ctx 里给了 registry 时才注册工具，
`expose=false`（baseline）时**一个工具都不加** ⇒ 工具清单逐字不变。
"""
from __future__ import annotations

import re

from .caps import Capability, register


@register("@rongke/metric-citation")
class CitationMetric(Capability):
    """Q4：引注正则只认 [1]，〔1〕式全族失明。"""
    id = "metric-citation"
    kind = "metric"
    version = "1.0.0"
    config_schema = {
        "bracket_pairs": {"type": "list", "item_type": "str", "required": True,
                          "hint": "必填且无默认值，如 [\"[n]\"] 或 [\"[n]\",\"〔n〕\"]"},
        "count_ref_table": {"type": "bool", "required": True},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        return ["count_citations", f"brackets={cfg['bracket_pairs']}"]

    def patterns(self) -> list:
        out = []
        for pair in self.cfg["bracket_pairs"]:
            if len(pair) < 2:
                continue
            l, r = pair[0], pair[-1]
            out.append(re.compile(re.escape(l) + r"\d{1,3}(?:\s*[-,，～~至]\s*\d{1,3})*" + re.escape(r)))
        return out

    def count(self, text: str) -> int:
        return sum(len(p.findall(text or "")) for p in self.patterns())


@register("@rongke/metric-chars")
class CharsMetric(Capability):
    """Q3：偏差按 chars_eq 算、展示却按 chars，一行里两个字数。"""
    id = "metric-chars"
    kind = "metric"
    version = "1.0.0"
    config_schema = {
        "unit": {"type": "str", "required": True,
                 "choices": ["chars", "chars_eq", "chars_excl_refs"]},
        "display_unit": {"type": "str", "required": True,
                         "choices": ["chars", "chars_eq", "chars_excl_refs"]},
        "en_word_factor": {"type": "float", "min": 1.0, "max": 3.0, "default": 1.6},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        return ["measure_chars", f"unit={cfg['unit']}",
                "口径一致" if cfg["unit"] == cfg["display_unit"] else "⚠ 量纲漂移"]

    def measure(self, text: str) -> dict:
        """真调 `writing.lint()`，不再用手搓的玩具计数。"""
        from .writing import lint
        m = lint(text or "").metrics
        vals = {"chars": m.get("chars", 0), "chars_eq": m.get("chars_eq", 0),
                "chars_excl_refs": m.get("chars_excl_refs", 0)}
        return {"measure": vals[self.cfg["unit"]],
                "display": vals[self.cfg["display_unit"]],
                "consistent": self.cfg["unit"] == self.cfg["display_unit"],
                "all": vals}


@register("@rongke/gate-kernel")
class KernelGate(Capability):
    """Q1/Q2：34 个指标只认 6 个；其余靠模型自述措辞，抄原文即放行。"""
    id = "gate-kernel"
    kind = "gate"
    version = "1.0.0"
    BASE_METRICS = ("chars", "chars_delta_pct", "paragraphs", "sentences",
                    "citations", "has_refs")
    config_schema = {
        "measured_metrics": {"type": "list", "item_type": "str", "required": True},
        "evidence_fallback": {"type": "bool", "required": True,
                              "hint": "true = 判不了就退回措辞匹配（Q1 的成因）"},
        "min_score": {"type": "int", "min": 0, "max": 100, "default": 70},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        return [f"kernel_metrics={len(cfg['measured_metrics'])}",
                "允许措辞兜底" if cfg["evidence_fallback"] else "禁措辞兜底"]

    def extra_metrics(self) -> list[str]:
        """baseline 的 6 项恰好对应 `_kernel_verdict` 原有四段 ⇒ 返回空，零变化。"""
        return [x for x in self.cfg["measured_metrics"] if x not in self.BASE_METRICS]


@register("@rongke/contract-followup")
class FollowupContract(Capability):
    """Q5：追问原样继承父轮契约，新指令零判据。"""
    id = "contract-followup"
    kind = "contract"
    version = "1.0.0"
    config_schema = {
        "merge_new_constraints": {"type": "bool", "required": True},
        "keep_parent": {"type": "bool", "required": True},
        "redraft_target": {"type": "bool", "required": True},
    }
    # 只认**能当验收标准**的祈使/否定句；问句、陈述、寒暄一律不收。
    _IMPERATIVE = re.compile(
        r"不要|别|不许|不得|切勿|压成|压缩到|改成|删掉|去掉|保留|贴(上|回|出)?全文|"
        r"附全文|控制在|限(在|于)|必须是|一律|逐条|不加|不补充|不引入|不出现")

    def mount(self, cfg, ctx):
        self.cfg = cfg
        return ["追问合并新约束" if cfg["merge_new_constraints"] else "原样沿用"]

    def split_constraints(self, text: str, existing: list[str] | None = None) -> list[str]:
        if not self.cfg["merge_new_constraints"]:
            return []
        parts = re.split(r"[。；;！!\n]+", str(text or ""))
        have = {re.sub(r"\W+", "", x) for x in (existing or [])}
        out: list[str] = []
        for p in parts:
            p = re.sub(r"^\s*【[^】]*】\s*", "", p).strip(" ，,、:：-")
            if not (4 <= len(p) <= 60) or "?" in p or "？" in p:
                continue
            if not self._IMPERATIVE.search(p):
                continue
            if re.sub(r"\W+", "", p) in have:
                continue
            out.append(p)
            if len(out) >= 6:
                break
        return out


@register("@rongke/mount-tools")
class ToolMount(Capability):
    """E1：59 个工具 schema 开局算死，整场不收缩。"""
    id = "mount-tools"
    kind = "mount"
    version = "1.0.0"
    config_schema = {
        "refresh": {"type": "str", "required": True,
                    "choices": ["once", "per_stage", "per_step"]},
        "initial_domains": {"type": "list", "item_type": "str", "required": True},
        # ★ 2026-11-05：这个字段原先是**死配置**（strict-v2 写了 18000，
        #   全代码没有消费点），和 `complete:true` 自称生效是同一种假成功。
        #   default=0 明确表示「不限」；旧值 42753 是某次实测字符数，一旦
        #   工具增长超过它，baseline 会被悄悄裁掉 ⇒ 那才是真回归。
        "schema_chars_budget": {"type": "int", "min": 0, "max": 200000, "default": 0},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        self._cache = None
        self._last_stage = None
        self.budget_report = None
        _b = int(cfg.get("schema_chars_budget") or 0)
        return [f"refresh={cfg['refresh']}", f"domains={len(cfg['initial_domains'])}",
                f"schema预算={'不限' if _b <= 0 else str(_b)}"]

    def domains(self) -> list[str]:
        """本场预设声明的初始工具挂载域清单（去重保序）。"""
        out: list[str] = []
        for d in (self.cfg.get("initial_domains") or []):
            s = str(d or "").strip()
            if s and s not in out:
                out.append(s)
        return out

    @property
    def initial_domains(self) -> list[str]:
        return self.domains()

    def schemas_for(self, registry, allow, step: int = 0, stage: str = "",
                    essential=()):
        """★ 可刷新接口：`once` 时每步返回**同一个 list 对象**，零额外开销，
        `tools_schema_chars` 与改前逐字相同（不把 schemas 整段移进循环）。

        预算裁剪放在**生成缓存时**，不是调用处：这样 `per_stage` / `per_step`
        重算出来的也是同一份清单（同一输入 + 确定性顺序 = 同一结果），
        才谈得上「整场冻结」。在调用处裁会出现开场裁了、第 3 步又全量的怪事。

        `essential` 由 `Caps.essential_tools()` 传入（本次挂载的能力贡献）。
        空元组 ⇒ 与旧版逐字等价；它也进缓存的输入语义，所以同一场不会
        出现「开场保住检索工具、第 3 步又裁掉」这种分叉。
        """
        if self.cfg["refresh"] == "once":
            if self._cache is None:
                self._cache = self._build(registry, allow, essential)
            return self._cache
        key = (stage, step) if self.cfg["refresh"] == "per_step" else stage
        if self._cache is None or key != self._last_stage:
            self._cache = self._build(registry, allow, essential)
            self._last_stage = key
        return self._cache

    def _build(self, registry, allow, essential=()):
        from .tools import ESSENTIAL_TOOLS, apply_schema_budget, schema_chars
        raw = registry.openai_schemas(allow)
        budget = int(self.cfg.get("schema_chars_budget") or 0)
        if budget <= 0:
            self.budget_report = None
            return raw
        # 内核交付通道永远在；其余必留项来自**本次挂载的能力**，去重保序。
        _must = list(ESSENTIAL_TOOLS)
        for n in essential:
            if n and n not in _must:
                _must.append(str(n))
        kept, dropped = apply_schema_budget(raw, budget, essential=tuple(_must))
        _names = [s.get("function", {}).get("name", "") for s in kept]
        self.budget_report = {
            "budget": budget,
            "before_tools": len(raw), "after_tools": len(kept),
            "before_chars": schema_chars(raw), "after_chars": schema_chars(kept),
            "within_budget": schema_chars(kept) <= budget,
            "dropped": dropped,
            # ★ 原来这里把名字写死成两个交付通道，插件保住的工具就
            #   「生效了但事件不说」—— 另一种形式的自称成功。改成读真集合。
            "essential": [n for n in _names if n in _must],
            "essential_from_caps": len(essential),
            "essential_over_budget": schema_chars(kept) > budget,
            "frozen": True,
            "reason": "按任务预算裁剪给模型看的 schema；被点名的工具仍可回退放开",
        }
        return kept


@register("@rongke/context-compact")
class ContextCompact(Capability):
    """E2：追问背着父轮完整历史，第 3 步才压。"""
    id = "context-compact"
    kind = "context"
    version = "1.0.0"
    BASELINE_TARGET_CHARS = 120000
    BASELINE_KEEP_LIVE = 6
    config_schema = {
        "target_chars": {"type": "int", "min": 10000, "max": 500000, "required": True},
        "compact_before_followup": {"type": "bool", "required": True},
        "keep_live": {"type": "int", "min": 1, "max": 50, "default": 6},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        return [f"target={cfg['target_chars']}",
                "追问先脱水" if cfg["compact_before_followup"] else "追问先发后压"]

    def effective_target_chars(self, cfg_default: int, *, preset_id: str = "") -> int:
        """解析最终压缩阈值：baseline 未改写时尊重用户 `config.json`（含 `0` 关闭）。

        非 baseline 预设（如 `lit-verify`=80000 / `strict-v2`=60000）或用户经个人层
        把 `baseline` 改成非基线值时，才覆盖 `cfg_default`。
        """
        val = int(self.cfg["target_chars"])
        if str(preset_id or "").strip() in ("", "baseline") and val == self.BASELINE_TARGET_CHARS:
            return int(cfg_default)
        return val

    def effective_keep_live(self, cfg_default: int, *, preset_id: str = "") -> int:
        """解析压缩保留条数：仅在预设显式偏离默认值时覆盖 `cfg_default`。"""
        val = int(self.cfg.get("keep_live", self.BASELINE_KEEP_LIVE))
        if val == self.BASELINE_KEEP_LIVE:
            return int(cfg_default)
        return val


@register("@rongke/tool-writing-metrics")
class WritingMetricsTool(Capability):
    """E4：66 个工具里没有量字数的，模型只能拿比值外推。"""
    id = "tool-writing-metrics"
    kind = "tool"
    version = "1.0.0"
    config_schema = {
        "expose": {"type": "bool", "required": True},
        "metrics": {"type": "list", "item_type": "str", "required": True},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        if not cfg["expose"]:
            return ["未暴露（模型继续手算）"]
        registry = ctx.get("registry")
        if registry is None:
            return ["已声明暴露，但本轮没有 registry（只读挂载检查）"]
        from .tools import Tool
        from .process_feedback import build_feedback

        def _fn(text: str = "", target_chars: int | None = None, **_kw) -> str:
            # ★ 11-07：一次回灌「还差什么」+ 全量指标，而不是只给 5 个数
            #   让模型自己外推（D2 实测它为此调了 5 次）。数据源仍是 lint()。
            fb = build_feedback(text, target_chars=target_chars,
                                caps=ctx.get("caps"))
            m = fb["metrics"]
            # 预设点名的指标排前面，其余 34 项一次给齐。
            picked = {k: m[k] for k in cfg["metrics"] if k in m}
            rest = {k: v for k, v in m.items() if k not in picked}
            return (fb["text"] + "\n\n"
                    + "【全量指标 · 内核实测】\n"
                    + "\n".join(f"{k}={v}" for k, v in {**picked, **rest}.items())
                    + "\n\n【人话报告（启发式项仅供参考，不阻断交付）】\n"
                    + fb["human"])

        # ★ 原本就有同名工具 ⇒ 它属于别人（第三方插件 / 内核开机预设）。
        #   既不覆盖、也不登记撤销：否则 run() 收尾会把别人的工具撤掉。
        if registry.get("writing_metrics") is not None:
            return ["writing_metrics 已由其他来源注册（不覆盖、不撤销）"]
        try:
            registry.register(Tool(
                name="writing_metrics",
                description=("一次读回当前稿件的全量实测指标与「还差什么」的差异摘要。"
                             "贴入稿件全文即可；内核自己算，不要手算字数或比值，"
                             "也不必反复调用。"),
                parameters={"type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "稿件全文"},
                                "target_chars": {"type": "integer", "description": "目标字数（可选）"}},
                            "required": ["text"]},
                fn=_fn, domain="core", source="builtin"))
        except Exception as e:                                  # noqa: BLE001
            raise RuntimeError(f"注册 writing_metrics 失败：{type(e).__name__}: {e}") from e
        # ★ 只登记「这次才新加的」：load_capability_preset 会把它挂到 Caps 上，
        #   Rongke.run() 的 finally 按这份清单撤销，预设就真的只管这一场。
        ctx.setdefault("registered_tools", []).append("writing_metrics")
        return [f"writing_metrics({','.join(cfg['metrics'])})"]


@register("@rongke/tool-source-alignment")
class SourceAlignmentTool(Capability):
    """把「这条引注有没有出处」做成**写之中**能主动查的读回。

    与门的分工：只给事实、不下判决。`source_alignment` 的三态里
    `unknown` 绝不等于「你编了」；查完不满意模型自己改稿，不产生打回。
    """
    id = "tool-source-alignment"
    kind = "tool"
    version = "1.0.0"
    config_schema = {
        "expose": {"type": "bool", "required": True},
        "min_score": {"type": "float", "required": True, "min": 0.0, "max": 1.0},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        if not cfg["expose"]:
            return ["未暴露（引文对齐只留内核 API）"]
        registry = ctx.get("registry")
        if registry is None:
            return ["已声明暴露，但本轮没有 registry（只读挂载检查）"]
        from .tools import Tool
        from .source_alignment import align

        def _fn(text: str = "", sources: str = "", read_docs=None,
                searched_docs=None, **_kw) -> str:
            import json
            r = align(text or "", sources=sources or None,
                      read_docs=read_docs, searched_docs=searched_docs,
                      min_score=cfg["min_score"])
            return (r["text"] + "\n\n【结构化台账 · 内核实测】\n"
                    + json.dumps({"summary": r["summary"], "items": r["items"]},
                                 ensure_ascii=False, indent=1))

        if registry.get("source_alignment") is not None:
            return ["source_alignment 已由其他来源注册（不覆盖、不撤销）"]
        try:
            registry.register(Tool(
                name="source_alignment",
                description=(
                    "核对稿件里每条引注的出处：① 本次有没有真读过该文献 "
                    "② 拿不拿得到那一页原文 ③ 稿件句能否在原文里找到支撑句。"
                    "贴入稿件全文；把 kb_read / kb_search 的回包 JSON 贴进 sources "
                    "即可做内容级比对，留空则只核出处记录。"
                    "⚠️ 三态：判不了 ≠ 不符合；对齐只证明有文本可支撑，"
                    "不判结论对错。本读回不阻断交付，不必为它反复调用。"),
                parameters={"type": "object",
                            "properties": {
                                "text": {"type": "string",
                                         "description": "稿件全文（含参考文献表）"},
                                "sources": {"type": "string",
                                            "description": "库内原文的 JSON 字符串："
                                                           "{doc_id: 全文} 或 "
                                                           "[{doc_id,page_no,text}]；留空只核出处"},
                                "read_docs": {"type": "array",
                                              "items": {"type": "string"},
                                              "description": "本次 kb_read 真读过的 doc_id"},
                                "searched_docs": {"type": "array",
                                                  "items": {"type": "string"},
                                                  "description": "仅 kb_search 命中过的 doc_id"},
                            },
                            "required": ["text"]},
                fn=_fn, domain="core", source="builtin"))
        except Exception as e:                                  # noqa: BLE001
            raise RuntimeError(
                f"注册 source_alignment 失败：{type(e).__name__}: {e}") from e
        ctx.setdefault("registered_tools", []).append("source_alignment")
        return [f"source_alignment(min_score={cfg['min_score']})"]


@register("@rongke/prompt-sections")
class PromptSections(Capability):
    """对应 dsh 的 complete: true —— 结构性覆盖提示段。"""
    id = "prompt-sections"
    kind = "prompt"
    version = "1.0.0"
    config_schema = {
        "complete": {"type": "bool", "required": True},
        "sections": {"type": "list", "item_type": "str", "required": True},
        # 非必填、无 default：没写就是「可选节全丢」，与接活前的旧行为一致。
        # 这里的 id 属于 **system_sections.OPTIONAL**，和上面 `sections` 的
        # dsh 风格提示段名是两个命名空间，绝不混用。
        "keep_sections": {"type": "list", "item_type": "str", "required": False,
                          "hint": "complete=true 时仍要保留的 SYSTEM 可选节，如 [\"writing\"]"},
    }

    #: 通用贡献点：结构性覆盖时仍要保留的语义节。外部能力也能挂同名属性。
    SYSTEM_KEEP_SECTIONS: list[str] = []

    def mount(self, cfg, ctx):
        from .caps import PROMPT_SECTION_IDS, available_sections
        from .system_sections import OPTIONAL
        known = set(PROMPT_SECTION_IDS) | set(available_sections())
        unknown = [s for s in cfg["sections"] if s not in known]
        if unknown:
            # 「声明了」不等于「装得出来」。原来这里无条件回一句「追加 N 段」，
            # 于是预设里写个拼错的名字也照样全绿 —— 死配置伪装成挂载成功。
            # 外部能力自带段也算 known；但「点名了却没挂该能力」由
            # mount_preset 的交叉校验单独打红，不在这里糊过去。
            raise ValueError(
                f"prompt.sections 里有不存在的提示段 {unknown!r}"
                f"（可用 {sorted(known)}）")
        keep = [str(s) for s in (cfg.get("keep_sections") or [])]
        bad_keep = [s for s in keep if s not in OPTIONAL]
        if bad_keep:
            # 第二命名空间也要拦沉默：写个不存在的节名，装配器会抛、
            # 任务会被降级成全量，等于「点名保留」悄悄失效。
            raise ValueError(
                f"prompt.keep_sections 里有不存在的可选 SYSTEM 节 {bad_keep!r}"
                f"（只有 {list(OPTIONAL)}）")
        self.cfg = cfg
        self.SYSTEM_KEEP_SECTIONS = keep
        out = [f"追加 {len(cfg['sections'])} 段"]
        if cfg["complete"]:
            # ★ 2026-11-05 已接线（原来这里写「[未接线]」）：
            #   `initial_messages` 读到本行的 complete=true 后，只发常驻节
            #   ＋ keep_sections 点名的可选节 —— 结构性覆盖仍在（日后 SYSTEM
            #   里再长出新段也不会漏进来），但不再把写作流水线一刀切掉。
            #   能力若自带 `SYSTEM_COMPLETE` 全文，则优先用那份全文。
            out.append("complete=true 已接线：system 骨架＝常驻节"
                       + (f"＋保留 {keep}" if keep else "（可选节全丢）"))
        return out


@register("@rongke/stage-brief")
class StageBrief(Capability):
    """写作前把阶段技能入口正文**确定性**前置，替代模型自己多次 load_skill。

    ⚠️ 与 `stage-skill-gate` 的分工要分清：那道门按用户要求只做**软警告**
    （W36：装技能是行为要求，没做不该被拒交付）。这里不做门、不加严，
    只负责「写之前规范已经在眼前」—— 省的是往返与打回，不是拦人。
    """

    id = "stage-brief"
    kind = "prompt"
    version = "1.0.0"
    config_schema = {
        # 必填、无默认值：不点名技能 = 不许悄悄等价于「全给」；
        # 预算也没有默认值，避免有人以为「前置」没有成本。
        "skills": {"type": "list", "item_type": "str", "required": True,
                   "hint": "本任务阶段技能的入口文档名，如 nature-writing"},
        "max_chars": {"type": "int", "required": True,
                      "hint": "整份简报的字符预算（含标题与缺件说明）"},
    }

    #: 通用消费点。`loop.initial_messages` 读 contributions("STAGE_BRIEF")。
    #: 空 dict / text 为空 ⇒ 不往 system 里加一个字（baseline 逐字节等价）。
    STAGE_BRIEF: dict = {}

    def mount(self, cfg, ctx):
        from .stage_brief import build_brief
        rep = build_brief(cfg["skills"], cfg["max_chars"])
        self.STAGE_BRIEF = rep
        out = [f"写作前置简报：注入 {len(rep['injected'])} 份 / "
               f"{rep['chars']} 字符"]
        if rep["missing"]:
            out.append("缺件（未注入、不判坏件）：" + "、".join(rep["missing"]))
        return out


@register("@rongke/stage-skill-gate")
class StageSkillGate(Capability):
    """Q7：阶段技能是软警告，恒亮告警制造噪音。"""
    id = "stage-skill-gate"
    kind = "stage"
    version = "1.0.0"
    config_schema = {
        "hard": {"type": "bool", "required": True},
        "warn_on_missing": {"type": "bool", "required": True},
        "dedupe_warnings": {"type": "bool", "required": True},
    }

    def mount(self, cfg, ctx):
        self.cfg = cfg
        return ["技能硬门" if cfg["hard"] else "仅警告"]


@register("@rongke/system-assembly")
class SystemAssembly(Capability):
    """按任务装配 SYSTEM：声明这一场要保留哪些**可选节**。

    学自 Anthropic Agent Skills 的渐进披露（只用得上的指令才进上下文），
    但不学 ITR 的「每步重算」—— prompt caching 要求前缀逐字节稳定，
    所以装配只在开场发生一次、整场冻结（见 `system_sections.py`）。

    ★ 这里只贡献「要哪些节」，不自带 system 散文：提示文本住代码里，
      预设点名不写正文（与 `preset.py` 同一条纪律）。要结构性替换整份
      system，由外部能力用类属性 `SYSTEM_COMPLETE` 提供全文。
    """

    id = "system-assembly"
    kind = "prompt"
    version = "1.0.0"
    config_schema = {
        # 必填、无默认值 —— 与 dsh 的 sampleOverCapGlobResults 事故同一条纪律：
        # 「没声明要哪些节」不能悄悄等价于「全给」，那会让裁剪永远不生效。
        "sections": {"type": "list", "item_type": "str", "required": True,
                     "hint": "要保留的可选节，如 [\"writing\"]；可选集见 system_sections.OPTIONAL"},
    }

    def mount(self, cfg, ctx):
        from .system_sections import OPTIONAL
        unknown = [s for s in cfg["sections"] if s not in OPTIONAL]
        if unknown:
            # 拼错节名必须当场打红，不许被装配器静默吞掉。
            raise ValueError(
                f"system-assembly.sections 里有不认得的可选节 {unknown!r}"
                f"（只有 {list(OPTIONAL)}）")
        self.SYSTEM_SECTIONS = list(cfg["sections"])
        return [f"按任务装配 SYSTEM：保留 {self.SYSTEM_SECTIONS}（开场一次、整场冻结）"]
