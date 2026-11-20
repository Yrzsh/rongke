# -*- coding: utf-8 -*-
"""文献池所有权层判据 —— 接线是真的、只读是不写盘的、缺席是不静默的。

量两件事：① `litpool_guard` 的行为（回包入池 / 引导 / 越界 / 状态）；
② 主循环的**接线结构**（AST，不用字符串包含当结构判据）。
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import litpool as L                              # noqa: E402
from rongke import litpool_guard as G                        # noqa: E402

_fail = 0


def ok(m):
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def eq(got, want, label):
    ok(label) if got == want else bad(f"{label}：期望 {want!r}，实际 {got!r}")


def truth(got, label):
    ok(label) if got else bad(label)


A, B, C = "kbabc123", "kbdef456", "kbghi789"
OUTLINE = ("# 大纲\n## 第1章 背景\nkbabc123 与 10.1000/xyz\n"
           "## 第2章 方法\nkbdef456\n## 附录\nkbghi789\n")
HITS = json.dumps({"hits": [{"doc_id": A, "title": "A", "doi": "10.1000/a"},
                            {"doc_id": B, "title": "B"}]}, ensure_ascii=False)


def ws(outline: str = OUTLINE) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="rk_lp_guard_"))
    (d / "outline.md").write_text(outline, encoding="utf-8")
    return d


def hits_call(d, query="被毛孢 育种", payload=HITS, tool="literature_search"):
    return G.ingest_call(tool, {"query": query}, payload, d)


print("── 1. 回包自动入池：去重、全文、写后读回 ──")
d = ws()
r = hits_call(d)
eq(r["applied"], True, "literature_search 回包自动入池")
eq((r["n_new"], r["n_dup"]), (2, 0), "首轮两条全新增")
eq(r["readback_equal"], True, "★ 落盘后逐字节读回相等")
truth((d / G.STATE_NAME).is_file(), "文献池.json 真在盘上")
r2 = hits_call(d, "菌株", json.dumps({"hits": [{"doc_id": A}]}))
eq((r2["n_new"], r2["n_dup"]), (0, 1), "重复回包按 doc_id 去重")
ft = G.ingest_call("literature_fetch", {"dois": [A]},
                   json.dumps({"results": [{"doi": A, "ok": True,
                                            "chars": 5231}]}), d)
eq(ft["n_fulltext"], 1, "取全文回包登记全文状态（不编正文）")
eq(L.load(d / G.STATE_NAME)["entries"][0]["fulltext"]["text"], "",
   "★ 没有正文就不编正文（note_fulltext 不造假）")
eq(L.load(d / G.STATE_NAME)["entries"][0]["fulltext"]["chars"], 5231,
   "全文字符数来自回包回读")

print("\n── 2. 解析不了 / 形状不认识 / 工具失败：一律明说 ──")
eq(G.ingest_call("kb_search", {}, "{oops", d)["reason"], "parse_failed",
   "★ JSON 被截断 ⇒ 明说 parse_failed，不静默跳过")
eq(G.ingest_call("kb_search", {}, '{"foo": 1}', d)["reason"], "shape_unknown",
   "★ 不认识的回包形状 ⇒ 明说没测到")
eq(G.ingest_call("kb_search", {}, "", d)["applied"], False, "空回包不入池")
eq(G.ingest_call("write_file", {}, "{}", d)["reason"], "not_lit_tool",
   "非文献工具不碰池")
eq(G.ingest_call("literature_search", {}, '{"hits": []}', d)["reason"],
   "no_entries", "0 条候选如实记，不造空池")
eq("回包解析不了" in G.ingest_call("kb_search", {}, "{oops", d)["detail"],
   True, "失败回包带可执行的说明")
eq(G.ingest_call("kb_search", {}, HITS, d)["advisory"], True,
   "回包是 advisory，不拦调用")

print("\n── 3. 引导建池：含「模型没用检索工具」的分母 ──")
d = ws()
b = G.bootstrap_from_outline(d)
eq(b["applied"], True, "★ 模型一次检索都没调，也从可见大纲建池")
eq(len(b["created"]), 4, "创建 4 条（含 DOI 形态），不编标题")
eq(b["readback_equal"], True, "引导后写后读回")
st = L.load(d / G.STATE_NAME)
eq(all(e["screening"]["verdict"] == "unscreened" for e in st["entries"]),
   True, "★ 引导条目未判相关性（工具不替人判）")
eq(all(not e["cards"] for e in st["entries"]), True,
   "★ 引导条目不编精读卡")
eq(all(not e["title"] for e in st["entries"]), True, "引导条目不编标题")
b2 = G.bootstrap_from_outline(d)
eq(b2["applied"], False, "第二次幂等，不重复建池")
eq(b2["reason"], "already_in_pool", "幂等理由说出口")
eq(G.bootstrap_from_outline(ws("# 大纲\n没有 doc_id\n"))["reason"],
   "no_docid_in_outline", "大纲没 doc_id 时不造池")
eq(G.bootstrap_from_outline(pathlib.Path(tempfile.mkdtemp()))["reason"],
   "no_outline", "没有 outline.md 时明说，不静默")

print("\n── 4. 章-文献映射：附录不并入上一章 + 只读不写盘 ──")
d = ws()
bd = G.outline_bindings(d)
eq(bd.get("2"), [B], "★ 附录里的 kbghi789 不并进第 2 章（11-09 P1 同型假红）")
eq("3" in bd, False, "★ 附录不占章号（不制造第 3 章）")
truth("10.1000/xyz" in bd.get("1", []), "DOI 形态也算可见 doc_id")
# 两层防护是独立的：所有标题行都当边界 ⇒ 内容不串章；_SKIP_CH 只管不占章号。
# 负控因此量「附录会不会被算成一章」，而不是「内容会不会漏进上一章」。
_o = G._SKIP_CH
try:
    G._SKIP_CH = __import__("re").compile(r"(?!x)x")
    bd2 = G.outline_bindings(d)
    truth("3" in bd2 and C in bd2.get("3", []),
          "★ 短路跳过规则 ⇒ 附录当场被算成第 3 章（判据有牙）")
    eq(bd2.get("2"), [B], "内容仍不串章，证明两层防护独立")
finally:
    G._SKIP_CH = _o
st = L.load(d / G.STATE_NAME) if (d / G.STATE_NAME).exists() else L.empty()
before = json.dumps(st, sort_keys=True, ensure_ascii=False)
cb = L.check_bindings(st, bd)
eq(cb["read_only"], True, "check_bindings 是只读版")
eq(json.dumps(st, sort_keys=True, ensure_ascii=False), before,
   "★ 只读核对不改 state 一个字节")
truth(bool(cb["out_of_pool"]), "池外 doc_id 被点名")
d2 = ws()
hits_call(d2)
eq(G.check_outline(d2)["ok"], False, "干净回包未判相关性 ⇒ 越界/未纳入被点名")
eq(G.check_outline(d2)["read_only"], True, "check_outline 只读")
eq(G.check_outline(ws())["status"], "absent", "没有池时明说 absent")

print("\n── 5. 交付态：缺席不静默、缺口点名、状态行进答复 ──")
s = G.status(ws())
eq(s["status"], "absent", "★ 没有 文献池.json ⇒ absent，不冒充通过")
eq(s["gap_codes"], ["LITPOOL_ABSENT"], "缺席单独成 code")
eq(s["advisory"], True, "缺席结论是 advisory")
truth("缺席" in G.status_line(s), "status_line 明说缺席")
d = ws()
hits_call(d)
s = G.status(d)
truth("CRITERIA_UNDECLARED" in s["gap_codes"], "未声明准则被点名")
truth("UNSCREENED" in s["gap_codes"], "逐篇未判被点名")
truth("已判定" in G.status_line(s), "状态行带过程读数")
truth("advisory" in G.status_line(s), "状态行明说 advisory")
truth("文献池" in G.guard_line(hits_call(ws())), "guard_line 给模型看的一句话")
eq(G.guard_line({"applied": False}), "", "没做事就不往 obs 塞废话")
truth("unknown" in G.status_line(None), "没拿到结论时说 unknown")

print("\n── 6. 写后 / 交付前整理：确定性、可幂等 ──")
d = ws()
r = G.reconcile_after_write(d)
eq(r["applied"], True, "写后整理跑通")
eq(r["advisory"], True, "整理是 advisory")
truth(bool((r.get("bootstrap") or {}).get("created")), "写后触发引导建池")
rd = G.reconcile_before_delivery(d, "见〔库·kbabc123 p.3〕。")
eq(rd["status"]["advisory"], True, "交付前结论 advisory")
truth("gap_codes" in rd["status"], "交付前结论带 gap_codes")

print("\n── 7. 主循环接线（AST 结构判据，不是字符串包含）──")
loop_src = (ROOT / "rongke/loop.py").read_text(encoding="utf-8")
TREE = ast.parse(loop_src)
rd_src = (ROOT / "rongke/readiness.py").read_text(encoding="utf-8")
RDTREE = ast.parse(rd_src)


def fn_of(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def names_in(node):
    return {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}


def calls_in(node):
    return {x.func.id for x in ast.walk(node)
            if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)}


imports = {a.name for n in ast.walk(TREE)
           if isinstance(n, ast.ImportFrom) for a in n.names}
truth("litpool_guard" in imports, "loop.py 真 import 了所有权层")
loop_fn = fn_of(TREE, "_loop")
truth(loop_fn is not None, "_loop 找得到")
names = names_in(loop_fn)
truth("_litpool_ingest" in names, "★ 统一收口接上了 _litpool_ingest")
truth("_litpool_after_write" in names, "★ 写后接上了 _litpool_after_write")
truth("_litpool_before_delivery" in names,
      "★ 交付前接上了 _litpool_before_delivery")
truth("_lit_status" in names, "交付态变量真被用上")
args_loop = {a.arg for a in loop_fn.args.args} | set(loop_fn.args.kwonlyargs)
truth({"recall", "unattended"} <= args_loop,
      "★ 请求级参数链没被改（R6 NameError 的防线仍在）")
compose = fn_of(TREE, "_compose_final")
truth("litpool_status" in {a.arg for a in compose.args.args},
      "★ _compose_final 收 litpool_status")
# deliver_attempt 的 dict 里必须同时有 figure_registry 与 litpool 两个键
deliver_keys = set()
for n in ast.walk(TREE):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
            and n.func.id == "emit" and n.args \
            and isinstance(n.args[0], ast.Constant) \
            and n.args[0].value == "deliver_attempt":
        for a in n.args[1:]:
            if isinstance(a, ast.Dict):
                deliver_keys |= {k.value for k in a.keys
                                 if isinstance(k, ast.Constant)}
truth({"figure_registry", "litpool"} <= deliver_keys,
      "★ deliver_attempt 事件里真带 litpool 字段")
# final 调用处真把 litpool_status 递下去
final_kw = set()
for n in ast.walk(TREE):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
            and n.func.id == "_compose_final":
        final_kw |= {k.arg for k in n.keywords if k.arg}
truth("litpool_status" in final_kw, "★ final 真把 litpool_status 传下去")
# 硬门没被碰
gate_checks = [n for n in ast.walk(TREE) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "check"
               and isinstance(n.func.value, ast.Attribute)
               and n.func.value.attr == "gate"]
eq(len(gate_checks), 1, "★ self.gate.check( 仍只 1 处，没加硬门")
# readiness 的三条 advisory
lp_gaps = set()
for n in ast.walk(RDTREE):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
            and n.func.id == "gap" and n.args \
            and isinstance(n.args[0], ast.Constant) \
            and str(n.args[0].value).startswith("LITPOOL_"):
        lp_gaps.add(n.args[0].value)
eq(lp_gaps, {"LITPOOL_ABSENT", "LITPOOL_GAPS", "LITPOOL_OUTLINE_BINDINGS"},
   "★ readiness 三条 advisory 齐全")
truth(all("stage_free" in {k.arg for k in n.keywords}
          for n in ast.walk(RDTREE) if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Name) and n.func.id == "gap"
          and n.args and isinstance(n.args[0], ast.Constant)
          and str(n.args[0].value).startswith("LITPOOL_")),
      "三条都是 stage_free（过程反馈，不进阶段硬门）")

print("\n── 7b. 运行时冒烟：真工具注册表 → 主循环接线 → 盘上池 ──")
# AST 只证明「线焊上了」；这一段真调一次工具，量「电通不通」。
# 不依赖 LLM（本机 CODEBUDDY_API_KEY 缺席），但走的是生产同一条 registry.call 路径。
import os                                                    # noqa: E402
from types import SimpleNamespace                              # noqa: E402
from rongke import loop as LOOP                                # noqa: E402
from rongke.tools import Tool, ToolRegistry                    # noqa: E402

_d = ws()
_old_cwd = os.getcwd()
os.chdir(_d)
try:
    reg = ToolRegistry()
    reg.register(Tool(
        name="literature_search", description="冒烟用",
        parameters={"type": "object", "properties": {
            "query": {"type": "string"}}},
        fn=lambda **kw: HITS, domain="core", source="builtin"))
    call = reg.call("literature_search", {"query": "被毛孢 育种"})
    eq(call.ok, True, "真工具注册表调用成功（result 是字符串）")
    events: list[tuple[str, dict]] = []
    rep = LOOP._litpool_ingest("literature_search",
                               {"query": "被毛孢 育种"}, call,
                               lambda k, v: events.append((k, v)))
    eq(rep["applied"], True, "★ 主循环接线在运行时真把回包入池")
    eq(rep["readback_equal"], True, "运行时落盘写后读回")
    truth((_d / G.STATE_NAME).is_file(), "盘上真有 文献池.json")
    truth(any(k == "litpool_ingested" for k, _ in events),
          "emit 真发出 litpool_ingested 事件")
    rep2 = LOOP._litpool_after_write(
        lambda k, v: events.append((k, v)))
    eq(rep2["applied"], True, "写后整理真跑通")
    truth("litpool_bootstrap" in [k for k, _ in events]
          or (rep2.get("bootstrap") or {}).get("reason") == "already_in_pool",
          "写后引导真触发或已幂等")
    st = LOOP._litpool_before_delivery(
        lambda k, v: events.append((k, v)))
    eq(bool(st.get("advisory")), True, "交付前结论 advisory")
    truth("litpool_status" in [k for k, _ in events],
          "交付前 emit 真发出 litpool_status")
    final = LOOP._compose_final(
        SimpleNamespace(summary="交付说明"), "正文" * 100,
        files=["draft.md"],
        figure_status={"status": "ok", "n_entries": 0, "ok": True,
                       "gaps": []},
        litpool_status=st)
    truth("【文献池】" in final, "★ final 真带文献池状态行")
    truth("图表登记" in final, "既有的图表状态行没被挤掉")
finally:
    os.chdir(_old_cwd)

print("\n── 8. 判据登记与边界 ──")
rg = (ROOT / "regress.py").read_text(encoding="utf-8")
truth('T("litpool-guard"' in rg, "本判据已挂进 regress.py 的 TESTS")
eq(G.ALL_TOOLS & {"write_file", "deliver_task"}, set(),
   "文献工具集合不含写文件/交付，不乱接线")
truth("文献池" in G.status_line(G.status(ws())), "缺席状态行说得出是哪一层")

print()
if _fail:
    print(f"[FAIL] 失败 {_fail} 项")
    raise SystemExit(1)
print("[DONE] 文献池所有权层判据全部通过")
