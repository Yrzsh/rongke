# -*- coding: utf-8 -*-
"""常驻探针：SYSTEM 分节装配 / 通用贡献点 / 额外判据 slot 化 / 工具可见性回退。

2026-11-05 内核优化的正负控。**两个方向都量**：
  · 正控：装配真省了字、交付纪律真保住、规则真生效、回退真放开；
  · 负控：baseline 逐字节等价、不点名就不生效、拼错节名降级要出声、
          两个 system 骨架提供者必须 broken、真幻觉不许被编成工具。

判据口径：逐条 `[PASS]`，结尾 `[DONE]`（`regress.py:_RE_MARK` 只认这些标记）。
"""
from __future__ import annotations

import inspect
import json
import pathlib
import re
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

_N = 0
_FAIL = 0


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


class _Stub:
    """贡献点夹具：只带类属性，不注册到全局。"""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def main() -> int:
    from rongke import caps, system_sections as ss
    from rongke.loop import SYSTEM, initial_messages
    from rongke.contract import TaskContract
    from rongke.tools import Tool, ToolRegistry, scope_fallback

    print("== 1 分节：可逆是构造保证，不是测试碰运气 ==")
    secs = ss.split_sections(SYSTEM)
    check("SYSTEM 非空且够长", len(SYSTEM) > 6000, str(len(SYSTEM)))
    check("切成 7 个有序节", len(secs) == 7, str([s.id for s in secs]))
    check("每节文本非空", all(s.text for s in secs))
    check("★ 拼接逐字节等于原串（可逆）", "".join(s.text for s in secs) == SYSTEM)
    check("锚点位置严格递增",
          [SYSTEM.index(s.text[:12]) for s in secs[1:]] ==
          sorted(SYSTEM.index(s.text[:12]) for s in secs[1:]))
    by_id = {s.id: s for s in secs}
    check("delivery 是常驻节（跨任务交付纪律）", by_id["delivery"].always)
    check("deck 是可选节", not by_id["deck"].always)
    check("PPT 工具链确实长在 deck 节里", "compose_deck" in by_id["deck"].text)
    check("交稿纪律确实长在 delivery 节里（不在 deck 节）",
          "交稿时必须附上正文全文" in by_id["delivery"].text
          and "交稿时必须附上正文全文" not in by_id["deck"].text)
    check("minimal_system == 所有常驻节拼接",
          ss.minimal_system(SYSTEM) == "".join(s.text for s in secs if s.always))
    check("minimal_system 保住交稿纪律",
          "交稿时必须附上正文全文" in ss.minimal_system(SYSTEM))
    check("minimal_system 不再有 PPT 段",
          "compose_deck" not in ss.minimal_system(SYSTEM))

    print("== 2 baseline 等价：连拼接都不许做 ==")
    a0 = ss.assemble(SYSTEM)
    check("★ 不带 needed ⇒ 返回 SYSTEM 原对象", a0.text is SYSTEM)
    check("saved=0 / dropped 空", a0.saved == 0 and a0.dropped == ())
    check("sections 是全节", a0.sections == ss.ALL_IDS)
    check("不是 complete 覆盖", a0.complete is False)
    check("整场冻结标记", a0.frozen is True)
    check("全点名也返回原对象",
          ss.assemble(SYSTEM, needed=list(ss.ALL_IDS)).text is SYSTEM)
    check("装配事实可序列化", isinstance(json.dumps(a0.to_dict()), str))
    check("to_dict 带 frozen / saved",
          "frozen" in a0.to_dict() and "saved" in a0.to_dict())

    print("== 3 按任务装配：省了字，且没把交付纪律一起裁掉 ==")
    aw = ss.assemble(SYSTEM, needed=["writing"])
    check("★ 写作场 system 变短", aw.chars < aw.baseline_chars,
          f"{aw.chars}/{aw.baseline_chars}")
    check("★ 省下的量级真实（>3000 字符）", aw.saved > 3000, str(aw.saved))
    check("丢的是 figure + deck", aw.dropped == ("figure", "deck"), str(aw.dropped))
    check("保留 5 节（4 常驻 + writing）", len(aw.sections) == 5, str(aw.sections))
    check("★ 交稿纪律还在（没按 PPT 标题粗暴裁）",
          "交稿时必须附上正文全文" in aw.text)
    check("★ 写作姿态体检还在", "defensive_" in aw.text)
    check("★ PPT 段不在了", "compose_deck" not in aw.text)
    check("★ 配图段不在了", "audit_figure" not in aw.text)
    check("写作阶段纪律还在", "load_skill" in aw.text)
    af = ss.assemble(SYSTEM, needed=["figure"])
    check("配图场保留配图段", "audit_figure" in af.text)
    check("配图场丢掉写作段", "nature-polishing" not in af.text)
    check("配图场仍保住交付纪律", "交稿时必须附上正文全文" in af.text)
    check("全可选节点名 ⇒ 逐字节等价",
          ss.assemble(SYSTEM, needed=list(ss.OPTIONAL)).text is SYSTEM)

    print("== 4 负控：拼错 / 漂移不许静默 ==")
    try:
        ss.assemble(SYSTEM, needed=["不存在的节"])
        _raised = False
    except ValueError:
        _raised = True
    check("未知节名 ⇒ ValueError", _raised)
    try:
        ss.split_sections("写作任务：\n写作任务：\n配图任务：")
        _raised2 = False
    except (ValueError, AssertionError):
        _raised2 = True
    check("锚点重复 ⇒ raise（不许静默缺段）", _raised2)
    check("空串不炸，返回空 Assembly",
          ss.assemble("").text == "" and ss.assemble("").chars == 0)
    ac = ss.assemble(SYSTEM, needed=["writing"], complete_text="XYZ")
    check("complete_text 优先于 needed", ac.text == "XYZ" and ac.complete is True)
    check("complete 覆盖记全节被替换", ac.dropped == ss.ALL_IDS)

    print("== 5 Caps 通用贡献点：新行为 = 新能力，不改内核 ==")
    check("空 Caps 无任何贡献", caps.Caps.empty().contributions("SYSTEM_SECTIONS") == [])
    check("空 Caps 无额外规则", caps.Caps.empty().extra_rules() == [])
    check("空 Caps 不请求覆盖", caps.Caps.empty().complete_flag() is False)
    c1 = caps.Caps("x", by_id={
        "r1": (_Stub(SYSTEM_SECTIONS=["writing", "writing", "figure"]), {}),
        "r2": (_Stub(SYSTEM_SECTIONS="deck"), {}),
    })
    check("contributions 读到类/实例属性",
          len(c1.contributions("SYSTEM_SECTIONS")) == 2)
    check("system_sections 去重保序",
          c1.system_sections() == ["writing", "figure", "deck"],
          str(c1.system_sections()))
    c2 = caps.Caps("x", by_id={
        "r1": (_Stub(SYSTEM_COMPLETE="整份骨架"), {}),
        "prompt": (_Stub(), {"complete": True}),
        "r2": (_Stub(EXTRA_RULES=[("k", re.compile("k"), "eq0", "标签"),
                                  ("坏形状",)]), {}),
    })
    check("system_complete 读回全文", c2.system_complete() == "整份骨架")
    check("complete_flag 从 prompt 行 config 读", c2.complete_flag() is True)
    check("extra_rules 只收 4 元组",
          len(c2.extra_rules()) == 1 and c2.extra_rules()[0][3] == "标签")
    check("complete_flag 读的是 config 不是 sections 段名",
          caps.Caps("x", by_id={"prompt": (_Stub(), {
              "complete": False,
              "sections": ["identity", "tools", "runtime", "safety", "skills"]})
          }).complete_flag() is False)
    check("schema_discipline 仍不许 required 带 default",
          caps.schema_discipline() == [], str(caps.schema_discipline()))

    print("== 6 两个 system 骨架提供者 ⇒ 必须 broken（拦沉默）==")
    from rongke.caps import Capability, register
    _tmp_names = ("@test/complete-a", "@test/complete-b")

    class _CA(Capability):
        id, kind, version, config_schema = "ca", "prompt", "1", {}
        SYSTEM_COMPLETE = "骨架A"

        def mount(self, cfg, ctx):
            return []

    class _CB(Capability):
        id, kind, version, config_schema = "cb", "prompt", "1", {}
        SYSTEM_COMPLETE = "骨架B"

        def mount(self, cfg, ctx):
            return []

    try:
        caps._REGISTRY[_tmp_names[0]] = _CA
        caps._REGISTRY[_tmp_names[1]] = _CB
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td) / "presets"
            d.mkdir()
            (d / "dup.json").write_text(json.dumps({
                "id": "dup", "name": "双骨架", "description": "负控",
                "order": 1, "scenes": ["writing"],
                "plugins": [
                    {"id": "ca", "name": _tmp_names[0], "config": {}},
                    {"id": "cb", "name": _tmp_names[1], "config": {}},
                ]}), encoding="utf-8")
            _dup = caps.load_capability_preset(
                "dup", directory=d, home=pathlib.Path(td) / "home")
            check("★ 双骨架预设不健康", not _dup.healthy)
            check("★ 理由点名 SYSTEM_COMPLETE",
                  any("SYSTEM_COMPLETE" in r.error for r in _dup.broken),
                  str([r.error for r in _dup.broken]))
    finally:
        for n in _tmp_names:
            caps._REGISTRY.pop(n, None)

    print("== 7 initial_messages 接线：读回 system 正文，不看配置在场 ==")
    ctr = TaskContract(raw_goal="测试", needs_writing=False,
                       acceptance=[], deliverables=["x.md"], stages=[])
    m_plain = initial_messages(ctr, None)
    check("不带 caps ⇒ SYSTEM 原文本", m_plain[0]["content"] == SYSTEM)
    m_base = initial_messages(ctr, None, caps=caps.load_capability_preset("baseline"))
    check("★ baseline ⇒ 逐字节等价", m_base[0]["content"] == SYSTEM)
    fact: dict = {}
    m_lean = initial_messages(ctr, None,
                              caps=caps.load_capability_preset("lean-context"),
                              assembly_out=fact)
    check("★ lean-context 真省了字", len(m_lean[0]["content"]) < len(SYSTEM),
          str(len(m_lean[0]["content"])))
    check("★ 装配事实带 saved / sections / frozen",
          fact.get("saved", 0) > 0 and fact.get("sections") and fact.get("frozen"),
          str(fact))
    check("★ 装配事实带 preset 身份", fact.get("preset") == "lean-context")
    check("★ 裁掉 PPT 段", "compose_deck" not in m_lean[0]["content"])
    check("★ 保住交付纪律", "交稿时必须附上正文全文" in m_lean[0]["content"])
    m_strict = initial_messages(ctr, None,
                                caps=caps.load_capability_preset("strict-v2"))
    check("★ complete=true 真改了 system", len(m_strict[0]["content"]) < len(SYSTEM),
          str(len(m_strict[0]["content"])))
    check("★ 覆盖后仍留常驻节",
          "交稿时必须附上正文全文" in m_strict[0]["content"]
          and "compose_deck" not in m_strict[0]["content"])
    bad_caps = caps.Caps("bad", by_id={
        "r1": (_Stub(SYSTEM_SECTIONS=["拼错的节"]), {})})
    degraded: dict = {}
    m_bad = initial_messages(ctr, None, caps=bad_caps, assembly_out=degraded)
    check("★ 节名拼错不许打崩任务，退回全量", m_bad[0]["content"] == SYSTEM)
    check("★ 降级必须出声（degraded + error）",
          degraded.get("degraded") is True and degraded.get("error"),
          str(degraded))
    # ⚠️ initial_messages 的调用点在 `Rongke._loop`（:4778），不在
    #    `_run_serial`（:4442）。扫错函数就会得到「接线不存在」的假红 ——
    #    「读不到」不等于「不符合」，判据扫哪个函数必须写清楚。
    from rongke.loop import Rongke
    _src = inspect.getsource(Rongke._loop)
    check("_loop 真把 assembly_out 传下去了",
          "assembly_out=_asm_fact" in _src)
    check("_loop 真发了 system_assembled 事件",
          'emit("system_assembled"' in _src)
    check("_loop 真接了工具可见性回退",
          "scope_fallback(schemas" in _src)

    print("== 8 额外判据 slot 化：贡献即生效，不点名就不生效 ==")
    from rongke.gate import (CompletionGate, KERNEL_FAIL, KERNEL_UNKNOWN)
    _rule = ("ext_rule_hits", re.compile("外部规则"), "eq0", "外部规则命中")

    class _Rep:
        metrics = {"ext_rule_hits": 2}
        target_chars = 0

    g_plain = CompletionGate(ToolRegistry(), caps=caps.Caps.empty())
    st0, note0 = g_plain._kernel_verdict("不许有外部规则命中", _Rep())
    check("不贡献规则 ⇒ 该 key 不判（UNKNOWN）",
          st0 == KERNEL_UNKNOWN, f"{st0}/{note0}")
    g_new = CompletionGate(ToolRegistry(), caps=caps.Caps(
        "x", by_id={"r1": (_Stub(EXTRA_RULES=[_rule]), {})}))
    st1, note1 = g_new._kernel_verdict("不许有外部规则命中", _Rep())
    check("★ 贡献规则 ⇒ 真打红（不是只在配置里挂上）",
          st1 == KERNEL_FAIL, f"{st1}/{note1}")
    check("★ 打回理由用了能力自带的标签",
          "外部规则命中 2 处" in note1, note1)
    st2, _ = g_new._kernel_verdict("不许有外部规则命中",
                                   type("R2", (), {"metrics": {"ext_rule_hits": 0},
                                                   "target_chars": 0})())
    check("★ 值为 0 ⇒ 不打红（不在正确稿上报错）", st2 != KERNEL_FAIL, st2)
    check("内置 7 条规则一条没丢", len(__import__(
        "rongke.gate", fromlist=["_EXTRA_RULES"])._EXTRA_RULES) == 7)

    print("== 9 工具可见性回退：放开一次，真幻觉不许编工具 ==")
    reg = ToolRegistry()
    reg.register(Tool("alpha", "工具甲", {}, lambda: 1, domain="core"))
    reg.register(Tool("beta", "工具乙", {}, lambda: 2, domain="ppt"))
    schemas = reg.openai_schemas(["alpha"])
    check("裁剪只作用于「给模型看的 schema」", len(schemas) == 1)
    check("注册表里工具仍在（不是调不到）", reg.get("beta") is not None)
    check("已可见 ⇒ 不重复放开", scope_fallback(schemas, reg, "alpha") is None)
    fb = scope_fallback(schemas, reg, "beta")
    check("★ 未暴露但真实存在 ⇒ 放开", fb is not None
          and fb["function"]["name"] == "beta")
    check("★ 真查无此工具 ⇒ 不替模型编工具",
          scope_fallback(schemas, reg, "幻觉工具") is None)
    if fb is not None:
        schemas.append(fb)
    check("★ 放开后整场保持（再调不重复扩张）",
          scope_fallback(schemas, reg, "beta") is None)
    check("工具回退函数无全局状态（纯函数）",
          "registry.get" in inspect.getsource(scope_fallback))

    print("== 10 工具硬预算：死配置接活、可观测、能回退 ==")
    from rongke.tools import (ESSENTIAL_TOOLS, apply_schema_budget,
                              schema_chars)
    from rongke.caps_builtin import ToolMount
    from rongke.gate import CompletionGate
    # ★ 2026-11-05：第 6 条硬门要求装载阶段技能，工具预算曾把 load_skill
    #   裁掉 ⇒ strict-v2 真写作场 42s 后报阻塞。必留面必须覆盖硬门要求的
    #   工具，夹具里也得真的注册它们，否则「必留」只是空名单。
    check("★ 硬门要求的技能装载工具都在必留面里（预算不许造死门）",
          set(CompletionGate.LOADER_TOOLS) <= set(ESSENTIAL_TOOLS),
          str(set(CompletionGate.LOADER_TOOLS) - set(ESSENTIAL_TOOLS)))
    reg2 = ToolRegistry()
    reg2.register(Tool("deliver_task", "交稿", {}, lambda: 1, domain="core"))
    reg2.register(Tool("report_blocker", "诚实退出", {}, lambda: 1, domain="core"))
    for n in ("load_skill", "read_skill"):
        reg2.register(Tool(n, "装载技能", {}, lambda: 1, domain="core"))
    for i in range(14):
        reg2.register(Tool(f"extra_{i:02d}", f"额外工具{i}",
                           {"description": "x" * 300}, lambda: 1, domain="misc"))
    raw2 = reg2.openai_schemas(None)
    check("夹具里必留工具确实存在",
          all(any(s["function"]["name"] == n for s in raw2) for n in ESSENTIAL_TOOLS))
    _k0, _d0 = apply_schema_budget(raw2, 0)
    check("★ 预算=0（baseline 默认）⇒ 一个工具都不裁",
          len(_k0) == len(raw2) and _d0 == [])
    _kbig, _dbig = apply_schema_budget(raw2, 10 ** 7)
    check("预算远大于清单 ⇒ 也不裁", len(_kbig) == len(raw2) and _dbig == [])
    budget = 4000
    _k1, _d1 = apply_schema_budget(raw2, budget)
    _k2, _d2 = apply_schema_budget(raw2, budget)
    check("★ 小预算 ⇒ 真的裁了（不是配置在场）",
          0 < len(_k1) < len(raw2) and _d1, f"kept={len(_k1)}")
    check("★ 同一输入两次结果逐字相同（确定性 ⇒ 才能整场冻结）",
          _k1 == _k2 and _d1 == _d2)
    check("★ 严格在预算内（按事件同口径量，不是估算）",
          schema_chars(_k1) <= budget, str(schema_chars(_k1)))
    check("★ 裁预算不许把交付/诚实退出通道拔掉",
          all(any(s["function"]["name"] == n for s in _k1) for n in ESSENTIAL_TOOLS))
    check("裁掉的每个名字都原本真实存在",
          set(_d1) <= {s["function"]["name"] for s in raw2})
    check("确实裁掉了工具（回退负控的分母）", bool(_d1))
    check("★ 被裁的工具仍可由点名回退放开",
          bool(_d1) and scope_fallback(_k1, reg2, _d1[0]) is not None)

    m0 = ToolMount()
    m0.mount({"refresh": "once", "initial_domains": []}, {})
    s0 = m0.schemas_for(reg2, None, 0, "open")
    check("★ baseline 形态（无预算声明）⇒ 清单与注册表逐字等价",
          len(s0) == len(raw2) and m0.budget_report is None)
    check("config default 是「不限」而不是某次实测字符数",
          m0.config_schema["schema_chars_budget"]["default"] == 0)
    m1 = ToolMount()
    _prov = m1.mount({"refresh": "per_stage", "initial_domains": [],
                      "schema_chars_budget": 4000}, {})
    s1 = m1.schemas_for(reg2, None, 0, "open")
    r1 = m1.budget_report
    check("★ 显式写了预算 ⇒ 真裁且产出报告",
          r1 is not None and len(s1) < len(raw2), str(r1 and r1["after_tools"]))
    check("报告列出裁掉的每一个工具（不许静默丢）",
          r1 is not None and len(r1["dropped"]) == len(raw2) - len(s1))
    check("报告自报 within_budget 与实测一致",
          r1 is not None and r1["within_budget"] == (r1["after_chars"] <= 4000))
    check("报告标明整场冻结", r1 is not None and r1.get("frozen") is True)
    s1b = m1.schemas_for(reg2, None, 1, "writing")
    check("★ per_stage 重算仍是同一份清单（开场算一次，不每步变形）",
          s1b == s1)
    check("mount() 的 provides 如实说预算不是「不限」",
          any("4000" in x for x in _prov))
    _strict = json.loads((_ROOT / "rongke" / "presets" / "strict-v2.json")
                         .read_text(encoding="utf-8"))
    check("strict-v2 确实显式声明了预算（消费点接的是真配置）",
          any(p.get("config", {}).get("schema_chars_budget") == 18000
              for p in _strict["plugins"]))
    check("前端接了 tools_budget_applied（没被 default 静默丢）",
          'case "tools_budget_applied"' in
          (_ROOT / "rongke" / "web" / "static" / "workspace.js").read_text(
              encoding="utf-8"))
    check("内核真发了 tools_budget_applied 事件",
          'emit("tools_budget_applied"' in
          (_ROOT / "rongke" / "loop.py").read_text(encoding="utf-8"))
    # ★ 原来这里硬编码了 `"_build(registry, allow)"` 这个**参数串**：
    #   给 _build 加一个 essential 参数就假红。判据要量语义（谁调谁），
    #   不量某个调用的字符串形状；用 AST 还顺带避开注释里同名文字。
    import ast
    def _called_names(node):
        out = set()
        for c in ast.walk(node):
            if not isinstance(c, ast.Call):
                continue
            f = c.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        return out
    _tree = ast.parse((_ROOT / "rongke" / "caps_builtin.py").read_text(encoding="utf-8"))
    _cls = next(n for n in _tree.body
                if isinstance(n, ast.ClassDef) and n.name == "ToolMount")
    _methods = {n.name: n for n in _cls.body if isinstance(n, ast.FunctionDef)}
    _sf, _bd = _methods["schemas_for"], _methods["_build"]
    _loop_calls = _called_names(
        ast.parse((_ROOT / "rongke" / "loop.py").read_text(encoding="utf-8")))
    check("预算裁剪做在装配层（schemas_for → _build → apply_schema_budget）",
          "_build" in _called_names(_sf) and
          "apply_schema_budget" in _called_names(_bd))
    check("★ 调用处没有自己裁一次（裁剪只有一个入口，整场才冻得住）",
          "apply_schema_budget" not in _loop_calls)
    _build_calls = [c for c in ast.walk(_sf) if isinstance(c, ast.Call)
                    and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "_build"]
    check("★ 能力贡献的 essential 真传进了 _build（不是死参数）",
          "essential" in [a.arg for a in _sf.args.args] and
          any(isinstance(a, ast.Name) and a.id == "essential"
              for c in _build_calls for a in c.args))

    print("== 11 锚点漂移时，baseline 与降级路径都不许炸（静态审阅实踩）==")
    from unittest import mock
    _broken = "一份锚点全丢了的 SYSTEM：没有写作任务、没有配图任务、没有演示文稿段。\n"
    try:
        ss.split_sections(_broken)
        _raised = False
    except (ValueError, AssertionError):
        _raised = True
    check("夹具确实让锚点计数失败（负控分母有效）", _raised)
    check("★ 锚点漂移 ⇒ baseline 仍原样返回，不裁不炸",
          ss.assemble(_broken).text == _broken)
    check("★ 能力自带全文 ⇒ 结构性覆盖不依赖 SYSTEM 锚点",
          ss.assemble(_broken, complete_text="我自己写的 system").text
          == "我自己写的 system")
    with mock.patch.object(ss, "split_sections",
                           side_effect=ValueError("锚点漂移")):
        check("★ initial_messages 的「退回全量」降级恒能兜住",
              ss.assemble(SYSTEM).text is SYSTEM)
    check("负控：正常 SYSTEM 下 baseline 仍是原对象",
          ss.assemble(SYSTEM).text is SYSTEM)
    check("快速路径排在切分之前（源码结构钉子）",
          inspect.getsource(ss.assemble).index("if not needed:")
          < inspect.getsource(ss.assemble).index("secs = split_sections(s)"))

    print("== 12 预设与前端可见性 ==")
    health = caps.all_presets_health()
    check("★ 全部随库预设仍健康", health.get("healthy") is True,
          str(health.get("broken")))
    ids = [p["id"] for p in health.get("good", [])]
    check("lean-context 在预设清单里且健康",
          "lean-context" in ids
          and all(p.get("healthy") for p in health["good"]
                  if p["id"] == "lean-context"))
    check("baseline 仍健康（9 行值没被动过）",
          all(p.get("healthy") for p in health["good"] if p["id"] == "baseline"))
    check("@rongke/system-assembly 已注册",
          "@rongke/system-assembly" in caps.registered_names())
    js = (_ROOT / "rongke" / "web" / "static" / "workspace.js").read_text(
        encoding="utf-8")
    check("前端接了 system_assembled（没被 default 静默丢）",
          'case "system_assembled"' in js)
    check("前端接了 tool_scope_expanded", 'case "tool_scope_expanded"' in js)
    check("前端给了新预设中文名", '"lean-context"' in js)
    # index.html 住在 web/（不是 web/static/）。本轮硬约束是它一行不动：
    # 预设中文名与事件人话全部落在 workspace.js，这里不许被偷偷接胶囊。
    html = _ROOT / "rongke" / "web" / "index.html"
    check("index.html 存在（路径是 web/ 不是 web/static/）", html.is_file())
    check("★ index.html 一行未动（没有本轮新增的预设名）",
          "lean-context" not in html.read_text(encoding="utf-8"))

    print("== 13 收尾：complete 保留节 / 胶囊入口 / 两个 UI 测试 ==")
    # ① 通用贡献点：不是给 prompt 行开的后门，外部能力也能挂同名属性。
    c3 = caps.Caps("x", by_id={
        "r1": (_Stub(SYSTEM_KEEP_SECTIONS=["writing", "writing"]), {}),
        "r2": (_Stub(SYSTEM_KEEP_SECTIONS="figure"), {}),
    })
    check("keep_sections 从通用贡献点读且去重保序",
          c3.keep_sections() == ["writing", "figure"], str(c3.keep_sections()))
    check("空 Caps 不点名保留任何节", caps.Caps.empty().keep_sections() == [])

    # ② 第二个命名空间也要拦沉默：拼错节名 ⇒ 坏件，不许降级路径悄悄吞掉。
    from rongke.caps_builtin import PromptSections
    _ps = PromptSections()
    try:
        _ps.mount({"complete": True, "sections": ["identity"],
                   "keep_sections": ["writing"]}, {})
        _ok = True
    except Exception:
        _ok = False
    check("正控：合法 keep_sections 能挂载", _ok)
    check("挂载后贡献点真带上 writing", _ps.SYSTEM_KEEP_SECTIONS == ["writing"])
    try:
        PromptSections().mount({"complete": True, "sections": ["identity"],
                                "keep_sections": ["不存在的节"]}, {})
        _bad_ok = False
    except ValueError as e:
        _bad_ok = "keep_sections" in str(e)
    check("★ 负控：拼错的可选节名必须当场报错（进 broken）", _bad_ok)
    check("新字段非必填且无 default（schema 纪律不回退）",
          caps.schema_discipline() == [], str(caps.schema_discipline()))

    # ③ strict-v2：结构性覆盖仍在，但 scenes=["writing"] 的流水线不许被裁掉。
    fact2: dict = {}
    m2 = initial_messages(ctr, None,
                          caps=caps.load_capability_preset("strict-v2"),
                          assembly_out=fact2)
    check("★ strict-v2 的 system 含写作流水线 load_skill",
          "load_skill" in m2[0]["content"])
    check("★ strict-v2 仍裁掉 PPT 段", "compose_deck" not in m2[0]["content"])
    check("★ strict-v2 仍保住交稿纪律",
          "交稿时必须附上正文全文" in m2[0]["content"])
    check("★ 结构性覆盖如实记账（complete=False 也不许说没覆盖）",
          fact2.get("structural_override") is True, str(fact2))
    check("★ 点名保留的节写进装配事实",
          fact2.get("kept_sections") == ["writing"], str(fact2.get("kept_sections")))
    check("★ dropped 是真裁掉的节（不含 writing）",
          "writing" not in (fact2.get("dropped") or []), str(fact2.get("dropped")))
    check("★ 长度＝常驻节＋writing（不是全量，也不是 1893 骨架）",
          len(m2[0]["content"])
          == len(ss.minimal_system(SYSTEM)) + len(by_id["writing"].text),
          str(len(m2[0]["content"])))
    # ④ 没点名保留时仍是旧骨架（负控），baseline 完全不受影响。
    _no_keep = caps.Caps("x", by_id={"prompt": (_Stub(), {"complete": True})})
    _f3: dict = {}
    m3 = initial_messages(ctr, None, caps=_no_keep, assembly_out=_f3)
    check("★ 负控：complete 但零 keep ⇒ 仍是 4 常驻节骨架",
          m3[0]["content"] == ss.minimal_system(SYSTEM))
    check("零 keep 时 structural_override 仍为 True",
          _f3.get("structural_override") is True)
    _f0: dict = {}
    m0 = initial_messages(ctr, None, caps=caps.Caps.empty(), assembly_out=_f0)
    check("★ baseline 不受新字段影响（原对象＋无覆盖）",
          m0[0]["content"] is SYSTEM and _f0.get("structural_override") is False)
    check("前端显示改用 structural_override（不把 true 读成 false）",
          "d.structural_override || d.complete" in js)

    # ⑤ 第 8/9 份预设的前台入口：只补 DOM，index.html 文件 0 行。
    check("PILL_PRESETS 接了 lean-context", '"精简上下文": "lean-context"' in js)
    check("PILL_PRESETS 接了 ext-answer-first",
          '"先答后问": "ext-answer-first"' in js)
    check("胶囊入口由 workspace.js 动态补进折叠区",
          "function mountExtraPresets" in js and "#morePills" in js)
    check("★ 补 DOM 排在 .chip 事件绑定之前（否则点了没反应）",
          js.index("function mountExtraPresets")
          < js.index('document.querySelectorAll(".chip")'))
    check("新胶囊带可直填的 data-goal", "用最精简的上下文" in js
          and "b.dataset.goal = goal" in js)
    _html_text = html.read_text(encoding="utf-8")
    check("★ index.html 仍一行未动（新胶囊名不在 HTML 里）",
          "精简上下文" not in _html_text and "先答后问" not in _html_text)

    # ⑥ 两个 UI 测试的诊断脆弱性：不许「没测到」被异常冒充成「没问题」。
    bt = (_ROOT / "billing_ui_test.py").read_text(encoding="utf-8")
    us = (_ROOT / "ui_shot.py").read_text(encoding="utf-8")
    # ⚠️ 量具体**调用形式**，别量裸词：注释里也会提到这个 API，
    #    扫全文会把「修好了」判成「没修」（与源码 AST 判据同一类坑）。
    check("billing-ui 诊断分支不再有会抛的 eval_on_selector",
          "pg.eval_on_selector(" not in bt)
    check("billing-ui 复用热控制台 RONGKE_WEB（摘掉冷启动变量）",
          "RONGKE_WEB" in bt and "re.search" in bt)
    check("ui_shot 填 #kbFindQ 前先点「新增文献」tab 并等可见",
          "data-kbtab=" in us and 'state="visible"' in us)
    check("★ 切 tab 确实排在 fill 之前（顺序钉子）",
          us.index("data-kbtab=") < us.index('page.fill("#kbFindQ"'))

    print(f"[DONE] {_N} 项 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
