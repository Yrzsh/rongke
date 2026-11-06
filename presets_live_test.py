# -*- coding: utf-8 -*-
"""能力预设层的**常驻**验收探针（挂进 regress.py，不许叫 _probe_*.py）。

判据分三层，都不采信「自称成功」：
  ① 装载：`preset.list_presets()` 的 (good, broken) + `caps` 真实挂载结果；
  ② 判坏：6 组负控必须**打红**，且坏件照样留在 results 里；
  ③ 端到端：`POST /api/run` 的 preset 字段真的走到 `kernel.run(preset=…)`，
     且前端三处、后端三处、内核事件流都有接线 —— 防「只改前端＝静默丢弃的假功能」。
"""
from __future__ import annotations

import inspect
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
N = 0


def check(label: str, got, want=True) -> None:
    global N
    N += 1
    ok = bool(got) if want is True else got == want
    if not ok:
        FAILS.append(f"{label}：得到 {got!r}，期望 {want!r}")
        print(f"  [FAIL] {label}  got={got!r}")
    else:
        # ★ 必须打 `[PASS]`（不是 ✓ U+2713）：regress.py 的 `_RE_MARK`
        #   只认 `[PASS|FAIL|OK|BAD]` 或 ✔/✘。用别的符号 ⇒ 59 项断言被
        #   计成 1 项（只数到结尾那行 [OK]），判据自己成了盲区。
        print(f"  [PASS] {label}")


def main() -> int:
    from rongke import caps
    from rongke.preset import list_presets, validate

    print("== ① 装载 ==")
    good, broken = list_presets()
    check("随库预设能读到 baseline", any(p.id == "baseline" for p in good))
    check("预设目录没有坏件", broken, [])
    base = caps.load_capability_preset("baseline")
    check("baseline 挂载健康", base.healthy)
    check("baseline 挂了 9 行", len(base.report.results), 9)
    check("baseline 全绿", len(base.report.broken), 0)
    check("baseline 值 = 旧常量（引注只认 [n]）", base.bracket_pairs(), ["[n]"])
    check("baseline 值 = 旧常量（门禁 6 项）", len(base.metrics()), 6)
    # 旧行为：偏差按 chars_eq 折算量算，报告却显示 chars ⇒ 量纲漂移（Q3）
    check("baseline 值 = 旧常量（测量单位 chars_eq）",
          base.flag("chars", "unit", ""), "chars_eq")
    check("baseline 值 = 旧常量（展示单位 chars）",
          base.flag("chars", "display_unit", ""), "chars")
    check("空 Caps 取 default ⇒ 旧行为等价",
          caps.Caps.empty().flag("gate", "evidence_fallback", True), True)
    check("必填 config 不许带 default", caps.schema_discipline(), [])
    # 注册表是**增量**的：外部能力从 caps_ext/ 自动加载，不该挤掉内置。
    # ⚠️ 别绑「恰好 N 个」：2026-11-04 加外部能力、2026-11-05 加第 10 个
    #    内置能力（system-assembly），两次都让「恰好 9」假红。
    #    改问「源码里声明的内置都在注册表里」—— 从 caps_builtin.py 的 AST
    #    读声明名单（单源、不随新增过期、也不扫注释里的字样）。
    import ast as _ast
    _rn = caps.registered_names()
    _builtin_src = (Path(__file__).parent / "rongke" / "caps_builtin.py").read_text(
        encoding="utf-8")
    _declared = {
        str(n.args[0].value)
        for n in _ast.walk(_ast.parse(_builtin_src))
        if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
        and n.func.id == "register" and n.args
        and isinstance(n.args[0], _ast.Constant)
    }
    # ⚠️ 本文件的 check 是 (name, got, expected)，不是 (name, cond, detail)。
    check("内置能力声明名单非空（防止注册表被清空还全绿）",
          len(_declared) >= 9, True)
    check("★ 源码声明的内置能力全在注册表（不绑总数）",
          sorted(_declared - set(_rn)), [])
    check("外部能力被自动发现并进注册表",
          "@external/answer-first" in _rn)

    print("== ② 负控（必须打红）==")
    P, R = caps.CapPreset, caps.CapRow
    rep = caps.mount_preset(P("x", rows=[R("c", "@rongke/metric-citation",
                                          {"count_ref_table": False})]))
    check("缺必填 bracket_pairs ⇒ broken", len(rep.broken), 1)
    check("理由点名缺的字段", "bracket_pairs" in (rep.broken[0].error if rep.broken else ""))
    check("坏件照样留在 results", len(rep.results), 1)

    rep = caps.mount_preset(P("x", rows=[R("z", "@nope/not-registered", {})]))
    check("模块 resolve 不到 ⇒ broken", len(rep.broken), 1)

    _, conflicts = caps.assemble([
        {"source": "a", "plugins": [{"id": "dup", "name": "@rongke/gate-kernel"}]},
        {"source": "b", "plugins": [{"id": "dup", "name": "@rongke/gate-kernel"}]}])
    check("重复 id ⇒ 层栈冲突", any("重复" in c for c in conflicts))
    rep2 = caps.mount_preset(P("x", rows=[]), conflicts)
    check("层栈冲突也进 broken（唯一判据）", len(rep2.broken), len(conflicts))

    _, conflicts = caps.assemble([
        {"source": "a", "plugins": [{"id": "p", "name": "@rongke/gate-kernel"}]},
        {"source": "b", "plugins": [{"id": "p", "op": "replace",
                                     "name": "@rongke/gate-kernel"}]}])
    check("replace 未 override ⇒ 冲突", any("replace" in c for c in conflicts))

    # 三条各由一种缺陷造成：非法枚举 / 缺必填（display_unit）/ 未知键
    errs = caps.schema_check(caps.resolve("@rongke/metric-chars").config_schema,
                             {"unit": "nope", "extra": 1})
    check("未知键 + 非法枚举 + 缺必填 ⇒ 报错", len(errs) >= 3, True)
    check("报了未知键", any("不认得的键" in e for e in errs))

    # 个人层覆盖：合并之后才校验，非法值必须被挡住
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        cfg = home / ".rongke"
        cfg.mkdir()
        (cfg / "config.json").write_text(
            json.dumps({"presets": {"baseline": {"plugins": [
                {"op": "patch", "id": "chars",
                 "config": {"unit": "not-a-unit"}}]}}},
                       ensure_ascii=False), encoding="utf-8")
        bad = caps.load_capability_preset("baseline", home=home)
        check("个人层非法枚举 ⇒ broken", len(bad.broken) >= 1, True)
        check("坏件理由带 config.unit",
              any("unit" in r.error for r in bad.broken))
    check("未知顶层键整份拒", bool(validate({"id": "x", "scenes": ["writing"],
                                            "skillz": []})))

    print("== ③ 端到端接线（防静默丢弃）==")
    from rongke.loop import Rongke
    from rongke.web import server as S

    check("Rongke.run 有 preset 形参", "preset" in inspect.signature(Rongke.run).parameters)
    check("Run 有 preset 字段", "preset" in S.Run.__dataclass_fields__)
    check("Run.preset 默认空串", S.Run(id="x", goal="g").preset, "")

    # 真跑一次 _exec（kernel 换成记录调用的替身，不烧模型）
    calls: list[dict] = []

    class FakeResult:
        @staticmethod
        def to_dict():
            return {"ok": True}

    class FakeKernel:
        def run(self, *a, **kw):
            calls.append(dict(kw))
            return FakeResult()

    mgr = S.Manager.__new__(S.Manager)
    mgr.kernel = FakeKernel()
    mgr._lock = threading.Lock()
    mgr.active = "probe"
    mgr.runs = {}
    r = S.Run(id="probe", goal="g", preset="paper-writing")
    mgr._exec(r)
    check("_exec 真的调了 kernel.run", len(calls), 1)
    check("preset 真的到了 kernel.run", calls[0].get("preset") if calls else None,
          "paper-writing")

    js = (ROOT / "rongke" / "web" / "static" / "workspace.js").read_text(encoding="utf-8")
    srv = (ROOT / "rongke" / "web" / "server.py").read_text(encoding="utf-8")
    loop = (ROOT / "rongke" / "loop.py").read_text(encoding="utf-8")
    html = (ROOT / "rongke" / "web" / "index.html").read_text(encoding="utf-8")
    check("前端有胶囊→预设映射表", "PILL_PRESETS" in js)
    check("前端预审带 preset", "max_steps: MAXSTEPS, mode: MODE, preset: PRESET" in js)
    check("前端 /api/run 带 preset", "body.preset = PRESET;" in js)
    check("前端 /api/follow 带 preset",
          "id: CUR.id, text, kb_only: !!MENTOR, mode: MODE, preset: PRESET" in js)
    check("新对话复位预设（防串味）", "PRESET = \"\";" in js)
    check("后端 /api/run 读 preset", 'body.get("preset")' in srv)
    check("后端 /api/presets 路由", "/api/presets" in srv)
    check("内核 emit 挂载事件", 'emit("preset_mounted"' in loop)
    check("index.html 一字未动（无 preset 字段）", "data-preset" not in html)
    check("ui_check 锁的调用处字面量仍在",
          "pillFormOpen(c.dataset.form || \"\", c)" in js)

    print("== ③b 前端可见性（内核发了事件，界面不许一片安静）==")
    # onEvent 的 default: break 会静默丢掉未列出的 kind —— 判据要钉在
    # 「case 存在」上，而不是「内核 emit 了」上（后者证明不了用户看得见）。
    for _k in ("preset_mounted", "preset_error", "followup_constraints",
               "followup_precompact"):
        check(f"onEvent 有 case {_k}", f'case "{_k}"' in js)
    check("预设 id 有中文短名", "PRESET_LABELS" in js and '"paper-writing": "论文写作"' in js)
    check("点胶囊时表单抬头说清用的是哪套要求",
          'PRESET_LABELS[PRESET] || PRESET' in js and '"要求"' in js)
    # W18 的分母是开机常驻注册表：临时工具写进 TOOLHUMAN 主表会被判成
    # 「内核没注册的旧名」。复刻 W18 的正则口径来钉这条边界。
    import re as _re
    _m = _re.search(r"const TOOLHUMAN = \{(.*?)\n\};", js, _re.S)
    check("W18 扫得到 TOOLHUMAN 主表", _m is not None)
    check("临时工具不进主表（W18 stale 负控）",
          bool(_m) and "writing_metrics" not in _m.group(1))
    check("humanTool 对临时工具仍说中文",
          "PRESET_TOOLHUMAN[name]" in js and
          'writing_metrics: "量稿件字数与引注"' in js)
    check("临时工具在过程组里不落裸「已调用工具」",
          'name === "writing_metrics") return "search"' in js)

    print("== ③c Q1 端到端：真跑 gate.check()，两个方向都量 ==")
    # D 场（2026-11-04 真人话术 + paper-writing）的死门：只量
    # `_echo_of()` 这个函数「认不认得复述」证明不了门在正确稿上放不放行。
    # 这里直接喂真证据链，只换证据内容，其他条件逐字相同。
    from types import SimpleNamespace
    from rongke.gate import CompletionGate, CompletionClaim, EvidenceRef
    from rongke.contract import TaskContract
    from rongke.tools import ToolRegistry as _TR
    _ACC = "全文不出现已确认融合子的结论性断言"

    def _q1_uncovered(pid: str, shows: str, result: str) -> bool:
        reg = _TR()
        reg.trace.append(SimpleNamespace(seq=1, name="read_file", ok=True,
                                         result=result, error=""))
        c = TaskContract(raw_goal="写讨论节", needs_writing=False,
                         acceptance=[_ACC], deliverables=["讨论.md"])
        claim = CompletionClaim(
            deliverables=["讨论.md"],
            evidence=[EvidenceRef(tool="read_file", call_seq=1,
                                  shows=shows, acceptance=_ACC)],
            summary="讨论.md 已写好")
        v = CompletionGate(reg, caps=caps.load_capability_preset(pid)).check(claim, c)
        txt = "\n".join(list(getattr(v, "reasons", []) or []) +
                        list(getattr(v, "warnings", []) or []))
        return "没有任何证据支撑" in txt

    check("Q1 正控：绑定字段=标准原文＋实质证据 ⇒ 不判「无证据」",
          _q1_uncovered("paper-writing",
                        "读回稿件，全篇只称候选再生菌株，无结论性断言",
                        "讨论.md 全文：尚不能确定其遗传来源，身份仍然存疑"), False)
    check("Q1 负控：shows 与回包都只抄标准原文 ⇒ 判「无证据」",
          _q1_uncovered("paper-writing", _ACC, _ACC), True)
    check("baseline 仍允许纯复述放行（旧行为不回归）",
          _q1_uncovered("baseline", _ACC, _ACC), False)

    print("== ④ 工具生命周期（预设只管这一场，不许泄漏给下一场）==")
    from rongke.tools import Tool, ToolRegistry

    reg = ToolRegistry()
    n0 = len(reg)
    b = caps.load_capability_preset("baseline", ctx={"registry": reg})
    check("baseline 不注册任何工具", len(reg), n0)
    check("baseline 撤销清单为空", b.registered_tools, [])
    p = caps.load_capability_preset("paper-writing", ctx={"registry": reg})
    check("paper-writing 挂上 writing_metrics", reg.get("writing_metrics") is not None)
    check("登记了本次新增工具", p.registered_tools, ["writing_metrics"])
    check("挂载事件里看得到新增清单",
          p.to_dict().get("registered_tools"), ["writing_metrics"])
    for _t in p.registered_tools:
        reg.unregister(_t)
    b.invalidate_mount_cache()
    check("撤销后工具总数回落", len(reg), n0)
    check("撤销后 writing_metrics 不在", reg.get("writing_metrics") is None)

    # 负控：原本就有的同名工具既不许被覆盖，也不许进撤销清单
    own = Tool(name="writing_metrics", description="第三方工具",
               parameters={"type": "object", "properties": {}},
               fn=lambda **kw: "third", domain="core", source="external")
    reg2 = ToolRegistry()
    reg2.register(own)
    p2 = caps.load_capability_preset("paper-writing", ctx={"registry": reg2})
    check("已存在的同名工具不被覆盖", reg2.get("writing_metrics") is own)
    check("别人的工具不进撤销清单", p2.registered_tools, [])

    # 真跑 Rongke.run() 的入口与收尾（只把 _run_serial 换成替身，不烧模型）
    class FakeGate:
        def __init__(self, c):
            self.caps = c

    k = Rongke.__new__(Rongke)
    k.registry = ToolRegistry()
    k._baseline_caps = caps.load_capability_preset(
        "baseline", ctx={"registry": k.registry})
    k.caps = k._baseline_caps
    k.gate = FakeGate(k.caps)
    seen: list[dict] = []

    def _fake_serial(*_a, **_kw):
        seen.append({"has_tool": k.registry.get("writing_metrics") is not None,
                     "caps": k.caps.preset_id, "gate": k.gate.caps.preset_id})
        return None

    k._run_serial = _fake_serial
    with tempfile.TemporaryDirectory() as td:
        k.license_root = Path(td)
        k.run("g", preset="paper-writing")
        check("run 期间工具真的挂上", bool(seen) and seen[0]["has_tool"], True)
        check("run 期间内核用本次预设", seen[0]["caps"], "paper-writing")
        check("run 期间 gate 用本次预设", seen[0]["gate"], "paper-writing")
        check("run 收尾撤掉工具", k.registry.get("writing_metrics") is None)
        check("run 收尾恢复 self.caps", k.caps.preset_id, "baseline")
        check("run 收尾恢复 gate.caps", k.gate.caps.preset_id, "baseline")
        seen.clear()
        k.run("g")
        check("下一场不带预设 ⇒ 工具不串味", seen[0]["has_tool"], False)
        check("下一场不带预设 ⇒ 回到 baseline", seen[0]["caps"], "baseline")

    print("== ⑤ 按任务挂不同 skill（预设工序 → 契约 → 门，两个方向都量）==")
    # 用户的判断：「每种任务所需的 skill 不一样」。判据不能只证明「配置在场」，
    # 要真调合并函数、真读回 contract.stages、真跑一次 gate.check()。
    from rongke.stages import STAGE_BY_ID, STAGE_ORDER
    from rongke.preset import presets_dir

    _TASK_PIDS = ("paper-writing", "lit-verify", "stats-review", "figure", "deck")
    _decl = {}
    for _pid in _TASK_PIDS:
        _c = caps.load_capability_preset(_pid)
        _decl[_pid] = list(_c.preset_stages)
        check(f"{_pid} 声明了本任务专属工序", bool(_c.preset_stages))
        check(f"{_pid} 的声明都查得到对应 skill",
              all(s in STAGE_BY_ID for s in _c.preset_stages))
        _raw = json.loads((presets_dir() / f"{_pid}.json").read_text(encoding="utf-8"))
        check(f"{_pid} 的 stages 真写进预设文件",
              list(_raw.get("stages") or []), _c.preset_stages)
        check(f"{_pid} 的挂载事件带出工序", _c.to_dict().get("stages"),
              _c.preset_stages)
    _skill_sets = [tuple(STAGE_BY_ID[s].skill for s in _decl[p]) for p in _TASK_PIDS]
    check("5 类任务的专属 skill 集合两两不同（不是同一套全量挂载）",
          len(set(_skill_sets)), len(_TASK_PIDS))
    check("baseline 不声明工序 ⇒ 旧行为逐字等价",
          caps.load_capability_preset("baseline").preset_stages, [])

    from types import SimpleNamespace as _NS
    from rongke.caps import merge_preset_stages
    _fake = _NS(stages=["draft"])
    _m = merge_preset_stages(_fake, ["stats", "units", "draft"])
    check("合并只加不删：原话推出的 draft 还在", "draft" in _fake.stages)
    check("合并只加不删：重复声明不重复插入",
          _fake.stages.count("draft"), 1)
    check("合并的 added 只报真补进去的", sorted(_m["added"]), ["stats", "units"])
    check("合并按流水线顺序归位",
          [STAGE_ORDER[s] for s in _fake.stages],
          sorted(STAGE_ORDER[s] for s in _fake.stages))
    check("合并返回的技能名与工序一一对应",
          len(_m["skills"]), len(_fake.stages))
    check("空声明 ⇒ 一个字都不改（baseline 路径不动契约）",
          merge_preset_stages(_NS(stages=["draft"]), [])["changed"], False)
    check("非法工序名被 preset.validate 打红（整份预设拒绝）",
          "stages 里有不认得的阶段" in validate(
              {"id": "x", "scenes": ["writing"],
               "stages": ["no_such_stage"]}, stem="x"))
    check("非法工序名不会混进合并结果",
          "no_such_stage" in merge_preset_stages(
              _NS(stages=[]), ["no_such_stage"])["stages"], False)

    # ★ 接线点在 `_loop`（契约定稿与 initial_messages 都在那儿），不是
    #   `_run_serial`。扫错函数会报"没接"，哪怕功能真的通 —— 判据读什么
    #   就得对准什么，和「读不到 ≠ 不符合」同一族。
    _loop_src = inspect.getsource(Rongke._loop)
    check("内核真调合并函数（不是只 emit 一个事件）",
          "merge_preset_stages(contract, _declared)" in _loop_src)
    check("内核发 preset_stages 事件",
          'emit("preset_stages"' in _loop_src)
    check("前端给 preset_stages 留了 case（不许被 default 静默丢掉）",
          'case "preset_stages"' in js)

    print("== ⑤b 挂载健康判据不说谎（skill 读得到 / 提示段真存在）==")
    import unittest.mock as _mock

    # ① 提示段白名单：声明一个不存在的段名必须当场 broken，而不是「追加 N 段」
    check("提示段白名单覆盖 baseline 的 5 个别名",
          {"identity", "tools", "runtime", "safety", "skills"}
          <= set(caps.PROMPT_SECTION_IDS))
    check("stats-checklist 是唯一可注入段",
          set(caps.INJECTABLE_SECTIONS), {"stats-checklist"})
    _bad = caps.mount_preset(caps.CapPreset("x", rows=[caps.CapRow(
        "prompt", "@rongke/prompt-sections",
        {"complete": False, "sections": ["stats-checklist", "拼错的段"]})]))
    check("负控：声明不存在的提示段 ⇒ broken", len(_bad.broken), 1)
    check("理由点名那个拼错的段",
          "拼错的段" in (_bad.broken[0].error if _bad.broken else ""))
    _good = caps.mount_preset(caps.CapPreset("x", rows=[caps.CapRow(
        "prompt", "@rongke/prompt-sections",
        {"complete": True, "sections": ["identity", "tool-policy"]})]))
    check("正控：合法段名 ⇒ 挂载成功", len(_good.broken), 0)
    # 2026-11-05：complete=true 已接线（原先这里判的是「不许自称已接线」）。
    # 产品行为真的变了 ⇒ 判据要跟着变，而不是把新实现判红。改得更严：
    # 除了 provides 不许再说「未接线」，下面 ③ 还要**真量 system 正文**。
    check("complete=true 已接线（provides 不再自称未接线）",
          any("已接线" in p for p in (_good.ok[0].provides if _good.ok else []))
          and not any("未接线" in p for p in (_good.ok[0].provides if _good.ok else [])),
          True)

    # ② 工序 id 是代码常量，它指向的 skill 是磁盘文件：只验前者会漏掉缺件。
    with tempfile.TemporaryDirectory() as _td:
        _d = Path(_td) / "presets"
        _d.mkdir()
        (_d / "ghost-skill.json").write_text(json.dumps({
            "id": "ghost-skill", "name": "缺件夹具", "description": "负控",
            "order": 1, "scenes": ["writing"], "stages": ["draft"],
            "plugins": []}, ensure_ascii=False), encoding="utf-8")
        _home = Path(_td) / "nohome"
        _normal = caps.load_capability_preset(
            "ghost-skill", directory=_d, home=_home)
        check("正控：技能读得到 ⇒ 这份预设健康", _normal.healthy)
        with _mock.patch("rongke.stages.available", return_value=set()):
            _blind = caps.load_capability_preset(
                "ghost-skill", directory=_d, home=_home)
        check("负控：技能读不到 ⇒ broken（不是全绿）", len(_blind.broken), 1)
        _why = _blind.broken[0].error if _blind.broken else ""
        check("理由点名工序与技能名（可操作，不是一句『加载失败』）",
              "draft" in _why and "nature-writing" in _why)
    _all = ("baseline", "strict-v2", "paper-writing", "lit-verify",
            "stats-review", "figure", "deck")
    check("7 份随库预设仍全绿（新增校验没误伤）",
          [p for p in _all if not caps.load_capability_preset(p).healthy], [])

    # ③ 提示段必须真进 system，而不是只在 JSON 里躺着一个名字。
    from rongke.loop import SYSTEM, STATS_CHECKLIST, initial_messages
    from rongke.contract import TaskContract as _TC
    with tempfile.TemporaryDirectory() as _td2:
        _c = _TC(raw_goal="统计审查", needs_writing=False,
                 acceptance=[], deliverables=["x.md"], stages=[])
        _m0 = initial_messages(_c, topics_root=Path(_td2))
        _mb = initial_messages(_c, topics_root=Path(_td2),
                               caps=caps.load_capability_preset("baseline"))
        _ms = initial_messages(_c, topics_root=Path(_td2),
                               caps=caps.load_capability_preset("stats-review"))
        _mc = initial_messages(_c, topics_root=Path(_td2),
                               caps=caps.load_capability_preset("strict-v2"))
        _ml = initial_messages(_c, topics_root=Path(_td2),
                               caps=caps.load_capability_preset("lean-context"))
    check("不带预设 ⇒ system 就是 SYSTEM 原文本", _m0[0]["content"], SYSTEM)
    check("baseline ⇒ 逐字节等价（一个字都不加）", _mb[0]["content"], SYSTEM)
    check("stats-review ⇒ 清单真进了 system", "统计审查清单" in _ms[0]["content"])
    check("追加的是同一份单源文本（没有第二份手抄）",
          _ms[0]["content"], SYSTEM + "\n\n" + STATS_CHECKLIST)
    # ★ complete=true 的接线判据：不读 provides 的自述，**量 system 正文**。
    check("★ complete=true 真改了 system（不是只在 provides 里改口）",
          _mc[0]["content"] != SYSTEM and len(_mc[0]["content"]) < len(SYSTEM), True)
    check("★ 覆盖后仍留跨任务交付纪律（没按 PPT 标题粗暴裁掉）",
          "交稿时必须附上正文全文" in _mc[0]["content"], True)
    check("★ 结构性覆盖真的丢了可选节", "compose_deck" not in _mc[0]["content"], True)
    # ★ 按任务装配：写作场只留 writing，PPT/配图段不在，交付纪律仍在。
    check("★ lean-context 装配后 system 变短", len(_ml[0]["content"]) < len(SYSTEM), True)
    check("★ lean-context 保住交付纪律", "交稿时必须附上正文全文" in _ml[0]["content"], True)
    check("★ lean-context 裁掉 PPT 段", "compose_deck" not in _ml[0]["content"], True)
    check("★ lean-context 保住写作纪律", "load_skill" in _ml[0]["content"], True)
    check("清单不是空壳（逐条可判的动作都在）",
          all(k in STATS_CHECKLIST for k in
              ("占位符", "量纲", "误差棒", "结尾不复述")))
    check("内核真把本场预设视图传给 initial_messages",
          'caps=getattr(self, "caps", None)' in _loop_src)

    # ★ 门的两个方向：正确的稿子不许被打红，缺必需 skill 才拦。
    #   D 场死门就是「加判据前没问会不会在正确稿上报错」的代价。
    from rongke.gate import CompletionGate, CompletionClaim, EvidenceRef
    from rongke.contract import TaskContract
    from rongke.tools import ToolRegistry as _TR2

    def _hard_reasons(pid: str, stages: list[str], loaded: list[str],
                      answer_only: bool = False, warnings: bool = False) -> str:
        reg = _TR2()
        seq = 1
        for nm in loaded:
            reg.trace.append(_NS(seq=seq, name="load_skill", ok=True,
                                 args={"name": nm, "file": ""},
                                 result="技能协议正文", error=""))
            seq += 1
        reg.trace.append(_NS(seq=seq, name="read_file", ok=True, args={},
                             result="审查.md 已核对", error=""))
        c = TaskContract(raw_goal="统计审查", needs_writing=False,
                         acceptance=[], deliverables=["审查.md"], stages=stages,
                         answer_only=answer_only)
        claim = CompletionClaim(
            deliverables=["审查.md"],
            evidence=[EvidenceRef(tool="read_file", call_seq=seq,
                                  shows="审查.md 已核对", acceptance="")],
            summary="审查.md 已写好")
        v = CompletionGate(reg, caps=caps.load_capability_preset(pid)).check(claim, c)
        key = "warnings" if warnings else "reasons"
        return "\n".join(list(getattr(v, key, []) or []))

    _need = [STAGE_BY_ID[s].skill for s in _decl["stats-review"]]
    check("硬门正控：必需 skill 全装载 ⇒ 不拦交付",
          "阶段技能·硬门" in _hard_reasons("stats-review", _decl["stats-review"],
                                           _need), False)
    check("硬门负控：少装一个必需 skill ⇒ 真的拦",
          "阶段技能·硬门" in _hard_reasons("stats-review", _decl["stats-review"],
                                           _need[:1]), True)
    check("不误伤：预设没点名的工序缺 skill ⇒ 不进硬门 reasons",
          "阶段技能·硬门" in _hard_reasons(
              "stats-review", _decl["stats-review"] + ["response"], _need), False)

    # ★ 真 HTTP 抓到的复发（run 03a69c03c26a）：选了 stats-review 胶囊，
    #   用户只问「为什么核算字数上限要剔除文献表」，契约判 answer_only /
    #   mode:"chat"，回答已写好却被硬门因没装载 nature-statistics 拒掉。
    #   判据要两个方向都量：问话不拦（但留提醒），真任务照旧拦。
    # 夹具必须复刻真跑：一个 load_skill 都没调（_need[:0]），否则 missing
    # 本就是空的，「没拦」和「没提醒」会同时成立，正的那一面量不到。
    check("问话契约：回答写好 ⇒ 硬门不拒（不复发 D 场死门）",
          "阶段技能·硬门" in _hard_reasons(
              "stats-review", _decl["stats-review"], _need[:0],
              answer_only=True), False)
    check("问话契约：仍留提醒（拦沉默，不拦选择）",
          "[阶段技能]" in _hard_reasons(
              "stats-review", _decl["stats-review"], _need[:0],
              answer_only=True, warnings=True))
    check("硬门分支真按 answer_only 分流（不是只写在注释里）",
          "if answer_only:" in inspect.getsource(CompletionGate.check))
    check("真任务负控仍在：answer_only=False 少装 skill 照样拦",
          "阶段技能·硬门" in _hard_reasons(
              "stats-review", _decl["stats-review"], _need[:1],
              answer_only=False))

    check("run 有 finally 收尾", "finally:" in inspect.getsource(Rongke.run))
    check("收尾按清单撤销工具",
          "self.registry.unregister(_t)" in inspect.getsource(Rongke.run))
    check("收尾作废 schema 缓存", "invalidate_mount_cache()" in loop)

    # ★ 夹具自检（resume_test / mentor_workspace_test）是**正则扫源码字面量**的：
    #   连注释里写一次属性名的点号形式，都会被当成「真实代码要这个协作对象」，
    #   当场打红 3 套。2026-11-04 实踩：注释里写了 self.caps ⇒ 19 条红灯里
    #   有 3 条是本轮自己造成的。这里复刻它的读/写口径，钉住别再犯。
    srcs = [inspect.getsource(f) for f in (Rongke._run_serial, Rongke._loop)]
    reads, writes = set(), set()
    for s in srcs:
        reads |= set(re.findall(r"self\.([A-Za-z_]\w*)", s))
        writes |= set(re.findall(r"self\.([A-Za-z_]\w*)\s*=(?!=)", s))
    check("主循环没给夹具自检添新依赖（caps 不在 reads-writes 里）",
          "caps" not in (reads - writes))

    # 结尾用 `[DONE]`：`[OK]` 会被 `_RE_MARK` 当成第 60 条 PASS，
    # 让回归计数比真实断言数多 1。
    print(f"\n[DONE] {'有失败' if FAILS else '全部通过'} · {N} 项 / {len(FAILS)} 失败")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
