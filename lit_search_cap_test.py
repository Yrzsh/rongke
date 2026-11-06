# -*- coding: utf-8 -*-
"""常驻判据：检索文献能力插件的四条真消费点 + 两个方向的负控。

只认读回的结果，不认「挂上了」：
  · 正控：预设点一行 ⇒ 段正文真进 system、检索工具链真被预算保住、
          门禁规则真用内核实测值判、写作节真保留
  · 负控：baseline / 其他预设逐字等价；配置不接活、缺工具不说、
          坏件不进 broken、索引越界 —— 都要被打红
  · 门禁规则必须**有真实分母**：`refs_n` 要能在 writing.lint 的 metrics 里读到，
    且在「正确的稿子」上不报错（验收标准没提检索就不判）

判据口径：逐条 `[PASS]`、结尾 `[DONE]`（`regress.py:_RE_MARK` 只认这些标记）。
"""
from __future__ import annotations

import json
import pathlib
import sys
from types import SimpleNamespace

_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

_N = 0
_FAIL = 0

EXT = "@external/lit-search"
SEG = "external-lit-search"
PRESET = "lit-search"


def check(name: str, cond, detail: str = "") -> bool:
    global _N, _FAIL
    _N += 1
    ok = bool(cond)
    if not ok:
        _FAIL += 1
        print(f"[FAIL] {name} {detail}")
    else:
        print(f"[PASS] {name}")
    return ok


class _FakeRegistry:
    """只给能力做可达性检查用的最小替身（duck typing：只需 .get）。"""

    def __init__(self, names):
        self._names = set(names)

    def get(self, name):
        return object() if name in self._names else None


def main() -> int:
    from rongke import caps
    from rongke.caps import (CapPreset, CapRow, mount_preset,
                             schema_discipline)
    from rongke.contract import TaskContract
    from rongke.loop import SYSTEM, initial_messages
    from rongke.writing import lint
    from rongke import system_sections
    from rongke.preset import list_presets, presets_dir

    def system_of(c) -> str:
        return initial_messages(TaskContract(raw_goal="测试"), None,
                                caps=c)[0]["content"]

    print("== ① 自动发现与挂载健康 ==")
    errs = caps.ensure_external_loaded()
    check("随库 caps_ext 导入无错", errs == [], str(errs))
    check("插件不用手工 import 就能 resolve", caps.resolve(EXT) is not None)
    check("注册名出现在 registered_names()", EXT in caps.registered_names())
    check("自带段名进 available_sections() 白名单",
          SEG in caps.available_sections())
    check("必填 config 没有偷偷给 default", schema_discipline() == [],
          str(schema_discipline()))

    base = caps.load_capability_preset("baseline")
    lit = caps.load_capability_preset(PRESET)
    check("baseline 健康（新增插件没污染）", base.healthy,
          str([r.error for r in base.broken]))
    check("lit-search 预设健康", lit.healthy,
          str([r.error for r in lit.broken]))
    check("lit-search 无坏件", lit.broken == [],
          str([r.error for r in lit.broken]))
    # 不绑死总数：只验「比 baseline 多出这一行」，日后加功能不假红。
    _bids = {r.id for r in base.report.results}
    _lids = {r.id for r in lit.report.results}
    check("插件行真进了挂载报告", "lit-search" in _lids - _bids,
          str(sorted(_lids - _bids)))
    check("baseline 原有九行一个不少", _bids <= _lids,
          str(sorted(_bids - _lids)))
    _row = next((r for r in lit.report.results if r.id == "lit-search"), None)
    _prov = list((_row.provides if _row else []) or [])
    check("挂载报告如实列出必留工具 / 门禁 / 可达性",
          any("必留工具" in p for p in _prov)
          and any("门禁规则" in p for p in _prov)
          and any("可达性" in p for p in _prov), str(_prov))

    print("== ② 提示段真进 system，配置必须接活 ==")
    s_lit = system_of(lit)
    s_base = system_of(base)
    check("baseline system 逐字节 == SYSTEM（原对象）", s_base is SYSTEM)
    check("lit-search 的 system 真含检索纪律",
          "文献检索纪律（外部能力" in s_lit)
    check("提示段点名的工具都真实存在（先查 /api/tools 口径）",
          all(t in s_lit for t in ("kb_search", "literature_search",
                                   "literature_fetch", "fetch_doi",
                                   "verify_citation", "kb_add",
                                   "build_references")))
    check("★ sources 配置真拼进正文（不是躺在 JSON 里）",
          "OpenAlex" in s_lit and "PubMed" in s_lit)
    check("baseline 不含检索段（旧行为一字不动）",
          "文献检索纪律（外部能力" not in s_base)
    check("别的预设不串味（stats-review 不含检索段）",
          "文献检索纪律（外部能力" not in
          system_of(caps.load_capability_preset("stats-review")))
    check("lit-verify 也不串味（插件与旧预设互不牵连）",
          "文献检索纪律（外部能力" not in
          system_of(caps.load_capability_preset("lit-verify")))
    # 配置接活的两个方向：换 sources 正文变；关掉 require_citations 规则消失。
    _cls = caps.resolve(EXT)
    a = _cls()
    a.mount({"enabled": True, "sources": ["ONLY-X"],
             "require_citations": True}, {"registry": None})
    b = _cls()
    b.mount({"enabled": True, "sources": ["OpenAlex"],
             "require_citations": False}, {"registry": None})
    check("换 sources ⇒ 段正文逐字变化",
          a.SECTION_TEXTS[SEG] != b.SECTION_TEXTS[SEG])
    check("换 sources ⇒ 新源名进正文、旧源名消失",
          "ONLY-X" in a.SECTION_TEXTS[SEG]
          and "OpenAlex" not in a.SECTION_TEXTS[SEG])
    check("require_citations=false ⇒ 门禁规则真的没了",
          list(getattr(b, "EXTRA_RULES", ())) == [])
    check("require_citations=true ⇒ 门禁规则真的在",
          len(getattr(a, "EXTRA_RULES", ())) == 1)

    print("== ③ SYSTEM 写作节保留（第四个消费点）==")
    check("插件贡献 SYSTEM_SECTIONS=writing",
          lit.system_sections() == ["writing"], str(lit.system_sections()))
    check("baseline 不贡献任何 SYSTEM 节", base.system_sections() == [])
    check("lit-search 的 system 保住写作流水线 load_skill",
          "load_skill" in s_lit)
    check("SYSTEM 分节拼接仍逐字节可逆",
          "".join(x.text for x in system_sections.split_sections(SYSTEM)) == SYSTEM)

    print("== ④ 工具必留面：通用贡献点 + 真预算正负控 ==")
    from rongke.tools import ESSENTIAL_TOOLS, apply_schema_budget
    # ⚠️ 不许 `from rongke.caps_ext.lit_search import TOOL_CHAIN`：外部能力是
    #   由 ensure_external_loaded() 动态加载的，再 import 一次会重复注册同名
    #   能力并抛 ValueError。常量只从注册表里的类属性读（插件只认注册表）。
    TOOL_CHAIN = tuple(_cls.ESSENTIAL_TOOLS)
    ess = lit.essential_tools()
    check("baseline 不贡献必留工具", base.essential_tools() == ())
    check("lit-search 贡献整条检索链（集合包含，不绑死个数）",
          set(TOOL_CHAIN) <= set(ess), str(ess))
    check("必留名单不含内核交付通道（插件不抢内核职责）",
          not (set(ESSENTIAL_TOOLS) & set(ess)))
    # noise 名字排在检索链前面，且塞进内核自己的两个交付通道 ——
    # 否则「不传 essential 时 literature_search 会被裁」这条负控是空判据。
    _names = sorted(set(TOOL_CHAIN) | set(ESSENTIAL_TOOLS)
                    | {f"a_noise_{i:02d}" for i in range(30)})
    raw = [{"function": {"name": n, "description": "x" * 220}}
           for n in _names]
    budget = 1800
    kept0, _ = apply_schema_budget(raw, budget)
    # ★ 与内核真路径同口径：ToolMount._build 会把「内核交付通道」与
    #   「能力贡献」合并后再传进来。只传 ess 等于量了一条内核不走的假路径。
    _must = tuple(ESSENTIAL_TOOLS) + tuple(
        n for n in ess if n not in ESSENTIAL_TOOLS)
    kept1, _ = apply_schema_budget(raw, budget, essential=_must)
    n0 = {s["function"]["name"] for s in kept0}
    n1 = {s["function"]["name"] for s in kept1}
    check("负控：不传插件贡献时，检索工具确实会被裁掉",
          "literature_search" not in n0, str(sorted(n0)))
    check("★ 正控：传了插件贡献，检索链一个不丢",
          set(TOOL_CHAIN) <= n1, str(sorted(set(TOOL_CHAIN) - n1)))
    check("必留通道比预算还大时仍不许拆交付门",
          set(ESSENTIAL_TOOLS) <= n1)
    from rongke.caps_builtin import ToolMount

    class _Reg:
        def openai_schemas(self, allow=None):
            return raw

    tm = ToolMount()
    tm.mount({"refresh": "once", "initial_domains": [],
              "schema_chars_budget": budget}, {})
    tm.schemas_for(_Reg(), None, 0, "open", essential=ess)
    br = tm.budget_report or {}
    check("budget_report.essential 真的列出被保住的检索工具",
          set(TOOL_CHAIN) <= set(br.get("essential") or []),
          str(br.get("essential")))
    check("budget_report 如实写出贡献来自能力（不是硬编码两个通道）",
          br.get("essential_from_caps") == len(ess), str(br))
    check("必留通道吃掉预算时要说出口（不许假装达标）",
          isinstance(br.get("essential_over_budget"), bool))
    tm2 = ToolMount()
    tm2.mount({"refresh": "once", "initial_domains": [],
               "schema_chars_budget": budget}, {})
    tm2.schemas_for(_Reg(), None, 0, "open")
    br2 = tm2.budget_report or {}
    check("baseline 路径：不传 essential ⇒ 必留名单只来自内核表（不绑死个数）",
          set(br2.get("essential") or []) <= set(ESSENTIAL_TOOLS),
          str(br2.get("essential")))
    check("baseline 路径：检索工具一个都没被保住（插件没点名就不生效）",
          not (set(TOOL_CHAIN) & set(br2.get("essential") or [])))
    check("baseline 路径：essential_from_caps=0",
          br2.get("essential_from_caps") == 0, str(br2))

    print("== ⑤ 门禁规则必须有真实分母（正负两个方向）==")
    _metrics = lint("正文一句。\n\n参考文献\n[1] A. Author, A Title, 2020.").metrics
    rules = lit.extra_rules()
    check("插件贡献 1 条规则", len(rules) == 1, str(rules))
    check("baseline 不贡献任何规则", base.extra_rules() == [])
    _key, _pat, _mode, _label = rules[0]
    check("★ 规则 key 在 writing.lint 的真实指标里（分母存在）",
          _key in _metrics, str(sorted(_metrics)[:20]))
    check("规则是 gt0（有文献表条目才通过）", _mode == "gt0")
    check("触发词正控：'检索文献并列出参考文献表' 命中",
          bool(_pat.search("检索文献并列出参考文献表")))
    check("触发词负控：'随便聊聊' 不命中", not _pat.search("随便聊聊"))
    check("触发词负控：'只做速断不要引用' 不命中",
          not _pat.search("只做速断不要引用"))
    from rongke.gate import (CompletionGate, KERNEL_FAIL, KERNEL_PASS,
                             KERNEL_UNKNOWN, _EXTRA_RULES)
    from rongke.tools import ToolRegistry
    g_lit = CompletionGate(ToolRegistry(), caps=lit)
    g_base = CompletionGate(ToolRegistry(), caps=base)
    acc = "检索文献并列出参考文献表"
    v1, t1 = g_lit._kernel_verdict(acc, SimpleNamespace(
        metrics={"refs_n": 2}, target_chars=0))
    v2, t2 = g_lit._kernel_verdict(acc, SimpleNamespace(
        metrics={"refs_n": 0}, target_chars=0))
    v3, _ = g_lit._kernel_verdict("随便聊聊即可", SimpleNamespace(
        metrics={"refs_n": 0}, target_chars=0))
    v4, _ = g_base._kernel_verdict(acc, SimpleNamespace(
        metrics={"refs_n": 0}, target_chars=0))
    check("★ 正控：检索了且有文献表 ⇒ PASS，说明点名该规则",
          v1 == KERNEL_PASS and "检索后文献表条目" in t1, f"{v1} {t1}")
    check("★ 负控：检索了却没有文献表 ⇒ FAIL（不采信自述）",
          v2 == KERNEL_FAIL and "检索后文献表条目" in t2, f"{v2} {t2}")
    check("★ 正确的稿子不误伤：标准没提检索 ⇒ 不判（UNKNOWN）",
          v3 == KERNEL_UNKNOWN, v3)
    check("★ 贡献即点名：baseline 同一标准 ⇒ 不判（UNKNOWN）",
          v4 == KERNEL_UNKNOWN, v4)
    _old_same = [r for r in _EXTRA_RULES if r[0] == _key]
    check("同 key 的旧规则不会重复记账（触发词不重叠）",
          all(not r[1].search(acc) for r in _old_same),
          str([r[3] for r in _old_same]))
    check("说明文字里该规则只出现一次（没被判两遍）",
          t1.count("检索后文献表条目") == 1, t1)

    print("== ⑥ 工具可达性不静默（环境缺件 ≠ 能力坏）==")
    _all = _FakeRegistry(TOOL_CHAIN)
    _miss = _FakeRegistry(set(TOOL_CHAIN) - {"literature_search"})
    c_all = _cls()
    c_all.mount({"enabled": True, "sources": ["X"],
                 "require_citations": True}, {"registry": _all})
    c_none = _cls()
    c_none.mount({"enabled": True, "sources": ["X"],
                  "require_citations": True}, {})
    c_miss = _cls()
    c_miss.mount({"enabled": True, "sources": ["X"],
                  "require_citations": True}, {"registry": _miss})
    check("全在 ⇒ 明说 7/7 可达",
          any("可达" in p for p in c_all.provides()), str(c_all.provides()))
    check("★ 没 registry ⇒ 明说未验证（不许自称已验证）",
          any("[未验证]" in p for p in c_none.provides()),
          str(c_none.provides()))
    check("★ 缺工具 ⇒ 点名缺谁（不是静默全绿）",
          any("[缺件]" in p and "literature_search" in p
              for p in c_miss.provides()), str(c_miss.provides()))
    check("缺工具不算坏件（能力本身没坏）",
          c_miss.tool_status == "missing")

    print("== ⑦ 坏件与交叉校验（拦沉默，不拦选择）==")
    def _mount_with(config, sections=None, with_cap=True):
        rows = []
        if with_cap:
            rows.append(CapRow("lit-search", EXT, config))
        if sections is not None:
            rows.append(CapRow("prompt", "@rongke/prompt-sections",
                               {"complete": False, "sections": sections}))
        return mount_preset(CapPreset(PRESET, rows=rows),
                            ctx={"registry": _all})

    bad_cfg = {"enabled": False, "sources": ["X"], "require_citations": True}
    check("enabled=false ⇒ broken",
          any("enabled=false" in r.error for r in
              _mount_with(bad_cfg).broken))
    check("sources 为空 ⇒ broken",
          any("sources 不能为空" in r.error for r in
              _mount_with({"enabled": True, "sources": [],
                           "require_citations": True}).broken))
    check("缺必填 config ⇒ broken（不许拿默认值糊过去）",
          bool(_mount_with({"enabled": True}).broken))
    check("★ 点名外部段却没挂能力 ⇒ broken",
          any("没有任何能力提供" in r.error for r in
              _mount_with(None, [SEG], with_cap=False).broken),
          str([r.error for r in
               _mount_with(None, [SEG], with_cap=False).broken]))
    check("拼错段名 ⇒ 白名单打红",
          bool(_mount_with({"enabled": True, "sources": ["X"],
                            "require_citations": True},
                           ["external-lit-serach"]).broken))

    print("== ⑧ 硬约束：index.html 0 行 / baseline 9 行不动 / 胶囊入口 ==")
    html = (_ROOT / "rongke" / "web" / "index.html").read_text(
        encoding="utf-8")
    check("index.html 存在且没写进任何插件 id（0 行改动）",
          "lit-search" not in html and EXT not in html)
    bl = json.loads((_ROOT / "rongke" / "presets" / "baseline.json").read_text(
        encoding="utf-8"))
    check("baseline 仍是九行、id 未变",
          bl.get("id") == "baseline" and len(bl.get("plugins") or []) == 9,
          str(len(bl.get("plugins") or [])))
    _p = next(r for r in bl["plugins"] if r["id"] == "prompt")
    check("baseline 的 prompt.sections 五个内置段未动",
          _p["config"]["sections"] == ["identity", "tools", "runtime",
                                       "safety", "skills"])
    js = (_ROOT / "rongke" / "web" / "static" / "workspace.js").read_text(
        encoding="utf-8")
    check("★「文献检索」胶囊真指向插件预设",
          '"文献检索": "lit-search"' in js)
    check("★ 其余四个文献类胶囊仍指 lit-verify（没被挤坏）",
          '"引用核验": "lit-verify"' in js
          and '"查证说法": "lit-verify"' in js
          and '"深入精读": "lit-verify"' in js
          and '"通俗读懂": "lit-verify"' in js)
    check("界面说中文（PRESET_LABELS 有插件中文名）",
          '"lit-search": "文献检索 · 插件"' in js)
    _good, _bad = list_presets(presets_dir())
    check("第 10 份预设真能被预设校验器读到（不是只有文件在）",
          PRESET in {p.id for p in _good} and not _bad,
          str(([p.id for p in _good], _bad)))

    print(f"[DONE] {_N} 项 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
