# -*- coding: utf-8 -*-
"""遥测（rongke/telemetry.py）与计价（rongke/pricing.py）单测 —— 纯函数，不需要 LLM、网络。

覆盖：
  1. 词汇表白名单 —— 白名单外必报（正控）/ 白名单内必闭嘴（负控）
  2. 结构判据     —— 有开始无结束必报（正控）
                     **零耗时按 kind 分**：chat/invoke_agent 必报（正控）
                     **execute_tool 耗时为 0 不许报**（负控，防误报）
  3. 层级与枚举   —— 父节点悬空 / span_id 重复 / kind / status / operation.name 两处真相
  4. 记录器端到端 —— 一条干净的 trace 必须**零问题**（负控）
  5. 时间账       —— 只用顶层 span 求和（父子不重复计数）+ 未埋点缺口
  6. 计价         —— 未知模型返回字面 UNKNOWN（正控）/ 已知模型 > 0
                     任一侧未知 ⇒ 合计未知（不许把未知吞成已知）
  7. 缓存口径     —— cache_read 是否含在 input 里，两种口径给出不同的数
  8. 上限与导出   —— MAX_SPANS 截断 / jsonl 可逐行解析
  9. 承重自检     —— 把白名单换成恒真替身、把"未闭合"谓词换成恒假、
                     把零耗时名单清空 / 塞满，对应的正控或负控必须**当场失守**
 10. 用量字段口径 —— `llm._usage_from` 必须认得**每一个真实存在的缓存字段名**
                     （OpenAI / DeepSeek / Anthropic 三家的都不一样）；
                     读到 0 ≠ 没有缓存，也可能是我们没读对名字（实测踩过）

一条刻意的记法：**每个断言都成对出现**。只有正控 = 可能报的是别的原因；
只有负控 = 可能这条检查根本没在干活。本项目实测过这两种翻车。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import telemetry as _T                      # noqa: E402
from rongke import pricing as _P                        # noqa: E402
from rongke import llm as _L                            # noqa: E402
from rongke.telemetry import Span, TraceRecorder, check_spans, span_problems  # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def has_problem(problems, needle):
    return any(needle in p for p in problems)


def mk_span(**kw):
    base = dict(name="chat", kind="chat", trace_id="t1", parent_id="",
                start=100.0, end=100.5, status="ok", attrs={})
    base.update(kw)
    return Span(**base)


class _Usage:
    def __init__(self, i=0, o=0, r=0, cr=0, cw=0):
        self.input_tokens, self.output_tokens = i, o
        self.reasoning_tokens, self.cache_read, self.cache_write = r, cr, cw


print("── 1. 词汇表白名单 ──")
good_span = mk_span(attrs={"gen_ai.operation.name": "chat",
                           "gen_ai.usage.input_tokens": 10,
                           "agent.latency_ms": 500.0})
bad_span = mk_span(attrs={"gen_ai.operation.name": "chat",
                          "我自己编的字段": 1})
check("白名单内的键 → 不报（负控）", _T.vocabulary_problems(good_span), [])
check("白名单外的键 → 报出来（正控）", _T.vocabulary_problems(bad_span), ["我自己编的字段"])
check("白名单外 ⇒ span_problems 也报", has_problem(span_problems(bad_span), "不在词汇表白名单"), True)

print("\n── 2. 结构判据（正控 / 负控 成对）──")
check("缺结束时间戳 → 必报（正控）",
      has_problem(span_problems(mk_span(end=0.0)), "缺少结束时间戳"), True)
check("end 早于 start → 必报（正控）",
      has_problem(span_problems(mk_span(start=200.0, end=100.0)), "end 早于 start"), True)
check("start 非正 → 必报（正控）",
      has_problem(span_problems(mk_span(start=0.0)), "start 非正"), True)

# ★ 这一对是 2026-09-18 的教训固化：原本我把「start==end」一律当异常，
#   结果 3 个**合法的**子毫秒工具（load_skill / writing_brief）被误判成坏。
#   `record_tool` 是用 `start = end - ms/1000` 反推起点的，注册表把毫秒取整
#   ⇒ 读本地文件真的会在 1ms 内返回、ms 截断成 0 ⇒ 该 span **必然** end==start。
#   这不是缺陷，是记录方式的必然产物。判据因此**按 kind 分**。
check("★ chat span 耗时恰好为 0 → 必报（正控）",
      has_problem(span_problems(mk_span(kind="chat", start=100.0, end=100.0)),
                  "耗时**恰好为 0**"), True)
check("★ invoke_agent 耗时恰好为 0 → 必报（正控）",
      has_problem(span_problems(mk_span(kind="invoke_agent", start=100.0, end=100.0)),
                  "耗时**恰好为 0**"), True)
check("★ execute_tool 耗时 0（子毫秒工具）→ 不许报（负控，防误报）",
      span_problems(mk_span(name="load_skill", kind="execute_tool",
                            start=100.0, end=100.0,
                            attrs={"gen_ai.operation.name": "execute_tool",
                                   "gen_ai.tool.name": "load_skill"})), [])
check("★ 判据单一定义处：span_problems 与 timing_problems 结论一致（防两处真相）",
      has_problem(span_problems(mk_span(kind="chat", start=100.0, end=100.0)),
                  "耗时**恰好为 0**")
      == bool(_T.timing_problems(mk_span(kind="chat", start=100.0, end=100.0))), True)
check("正常闭合的 span → 零问题（负控）", span_problems(good_span), [])
check("正常闭合的 execute_tool（有耗时有属性）→ 零问题（负控）",
      span_problems(mk_span(name="search", kind="execute_tool",
                            start=100.0, end=100.4,
                            attrs={"gen_ai.operation.name": "execute_tool",
                                   "gen_ai.tool.name": "search"})), [])

print("\n── 3. 层级与枚举（两处真相）──")
check("kind 越界 → 必报", has_problem(span_problems(mk_span(kind="啥也不是")), "kind="), True)
check("status 越界 → 必报", has_problem(span_problems(mk_span(status="maybe")), "status="), True)
check("kind 与 operation.name 不一致 → 必报（两处真相）",
      has_problem(span_problems(mk_span(kind="chat",
                                        attrs={"gen_ai.operation.name": "execute_tool"})),
                  "不一致"), True)
check("父节点悬空 → check_spans 报",
      has_problem(check_spans([mk_span(parent_id="不存在的id")]), "悬空父节点"), True)
dup = mk_span(span_id="same")
check("span_id 重复 → check_spans 报",
      has_problem(check_spans([dup, mk_span(span_id="same")]), "span_id 重复"), True)

print("\n── 4. 记录器端到端：一条干净的 trace 必须零问题（负控）──")
# ⚠️ 这个 sleep 不是凑数，是**夹具必须让被观测的对象真的耗掉一格时钟**：
#    本机实测 `time.time()` 步进 ≈ 1 ms（连调 2000 次**全部精确相等**），
#    而 start_run() → finish() 之间只隔几条语句（微秒级）
#    ⇒ end == start **精确相等** ⇒ 零耗时判据（invoke_agent 必须 > 0）会在一份
#    **完全健康**的 trace 上报错，本套件因此长期是红的（2026-09-19 定位）。
#    **这是判据与时钟粒度的冲突，不是判据的缺陷** —— 生产里一次 invoke_agent 是秒级，
#    永远读不到 0，所以判据照留；要修的是这个"零工作量"的夹具。
#    并加一条**先决条件断言**（见下面 finish 之后那条），免得有人删掉 sleep
#    之后测试又"通过"了 —— 那时它测的是夹具，不是记录器。
CLOCK_TICK_S = 0.004
rec = TraceRecorder(agent_name="rongke", model="m1", provider="p1")
root = rec.start_run()
time.sleep(CLOCK_TICK_S)
check("根 span 已开且未闭合", _T.open_spans(rec.spans), [rec.spans[0]])
check("根 span 的 kind", rec.spans[0].kind, "invoke_agent")
rec.record_chat(1, 100.0, 100.8, _Usage(i=1000, o=200, r=50, cr=400),
                finish_reason="tool_calls", messages_chars=5000,
                tools_schema_chars=9000, cost=0.0012)
check("chat span 挂到了根下", rec.spans[1].parent_id, root)
check("chat span 带结束时间戳（本次修复的核心）", rec.spans[1].closed, True)
check("chat span 记下 finish_reason",
      rec.spans[1].attrs.get("gen_ai.response.finish_reason"), "tool_calls")
check("chat span 记下缓存命中", rec.spans[1].attrs.get("gen_ai.usage.cache_read"), 400)
rec.finish(cost=0.0012, iteration_count=1)
check("finish 之后根 span 闭合", _T.open_spans(rec.spans), [])
check("★ 先决条件：根 span 的墙钟跨度真的 > 0（否则下面的负控其实在测夹具）",
      rec.spans[0].duration_ms > 0, True)
check("★ 干净的 trace → check_spans 零问题（负控）", check_spans(rec.spans), [])
check("根 span 带上总迭代次数", rec.spans[0].attrs.get("agent.iteration_count"), 1)

print("\n── 5. 工具 span（含被拦下的那一类）──")
try:
    from rongke.tools import ToolCall
    rec2 = TraceRecorder(model="m1")
    rec2.start_run()
    time.sleep(CLOCK_TICK_S)   # 同上：让时钟走一格（否则 ^ 根 span 读成 0 耗时）
    tc_ok = ToolCall(seq=1, id="a", name="fetch_doi", args={}, ok=True, ms=120,
                     result="x" * 50, domain="lit", mutating=False, ts=1000.0)
    rec2.after_call(tc_ok)
    sp = rec2.spans[-1]
    check("工具 span 的 kind", sp.kind, "execute_tool")
    check("工具 span 的时长取自台账的 ms", round(sp.duration_ms), 120)
    check("工具 span 的 tool.name", sp.attrs.get("gen_ai.tool.name"), "fetch_doi")

    tc_blocked = ToolCall(seq=2, id="b", name="origin_plot_xy", args={}, ok=False, ms=0,
                          result="", error="被围栏拦截", domain="figure",
                          mutating=True, blocked_by="workspace_fence", ts=2000.0)
    rec2.after_call(tc_blocked)
    sp2 = rec2.spans[-1]
    check("被拦下的调用 status=error", sp2.status, "error")
    check("被拦下的调用记下 blocked_by", sp2.attrs.get("agent.blocked_by"), "workspace_fence")
    check("★ ms=0 的工具 span 不产生问题（防误报负控）", span_problems(sp2), [])
    rec2.finish()
    check("含工具调用的 trace 零问题", check_spans(rec2.spans), [])
except ImportError as e:
    print(f"  [SKIP] rongke.tools 不可导入：{e}")

print("\n── 6. 时间账：只用顶层 span 求和 ──")
t_root = mk_span(name="invoke_agent", kind="invoke_agent", span_id="R",
                 parent_id="", start=100.0, end=110.0)
t_child = mk_span(name="chat", kind="chat", span_id="C", parent_id="R",
                  start=102.0, end=104.0)
rep = _T.timing_report([t_root, t_child], 12.0)
check("顶层只算根（子不重复计数）", rep["top_level_spans"], 1)
check("span_total_ms 不含子（否则会大于墙钟 = 假数）", rep["span_total_ms"], 10000.0)
check("覆盖时长", rep["span_covered_ms"], 10000.0)
check("墙钟", rep["elapsed_ms"], 12000.0)
check("未埋点缺口", rep["uncovered_ms"], 2000.0)
check("未埋点占比", rep["uncovered_pct"], 16.7)

print("\n── 7. 计价：未知模型返回 UNKNOWN，不是 0 ──")
empty = {"models": {}, "default": None, "cache_read_included_in_input": True}
u = _Usage(i=1_000_000, o=1_000_000, cr=200_000)
check("★ 未知模型 → 字面 UNKNOWN（正控）",
      _P.estimate_cost(u, "谁也没听过的模型", empty), "UNKNOWN")
check("★ UNKNOWN 不是 0（0 会让人以为免费）",
      _P.estimate_cost(u, "谁也没听过的模型", empty) != 0, True)

table = {"models": {"m1": {"input": 1.0, "output": 2.0, "cache_read": 0.1}},
         "default": None, "cache_read_included_in_input": True}
check("已知模型 → 算得出钱（>0）", _P.estimate_cost(u, "m1", table), 2.82)

print("\n── 8. 缓存口径：两种口径必须给出不同的数 ──")
table_b = dict(table)
table_b["cache_read_included_in_input"] = False
a = _P.estimate_cost(u, "m1", table)
b = _P.estimate_cost(u, "m1", table_b)
check("口径不同 ⇒ 结果不同（不许悄悄统一成一种）", a != b, True)
check("含在 input 内时更便宜（缓存部分按缓存价计）", a < b, True)
check("并列口径 = 全额 input + 缓存 + 输出", b, 3.02)

print("\n── 9. 成本相加：未知不许被吞成已知 ──")
check("UNKNOWN + 1.0 → UNKNOWN", _P.add_cost(_P.UNKNOWN, 1.0), "UNKNOWN")
check("1.0 + UNKNOWN → UNKNOWN", _P.add_cost(1.0, _P.UNKNOWN), "UNKNOWN")
check("1.25 + 2.75 → 4.0", _P.add_cost(1.25, 2.75), 4.0)
check("None + 1.0 → 1.0（缺省不是未知）", _P.add_cost(None, 1.0), 1.0)
check("format_cost(UNKNOWN)", _P.format_cost(_P.UNKNOWN), "UNKNOWN")
check("format_cost(None)", _P.format_cost(None), "UNKNOWN")
check("format_cost(0) → 明确的 0（价格表里真写了 0 才可能）", _P.format_cost(0), "$0")
check("format_cost(0.0123456)", _P.format_cost(0.0123456), "$0.0123")
check("format_cost(极小值) → 不四舍五入成 0", _P.format_cost(0.00001), "<$0.0001")
check("template 列出模型名骨架",
      sorted(_P.template(["b", "a", "a"])["models"]), ["a", "b"])

print("\n── 10. 上限与导出 ──")
rec3 = TraceRecorder(model="m1")
rec3.start_run()
for i in range(5):
    rec3.record_chat(i + 1, 100.0 + i, 100.5 + i, _Usage(i=10, o=2))
rec3.finish()
lines = [x for x in rec3.jsonl().splitlines() if x.strip()]
check("jsonl 行数 == span 数", len(lines), len(rec3.spans))
check("每行都是合法 JSON", all(isinstance(json.loads(x), dict) for x in lines), True)
check("jsonl 里带 duration_ms", "duration_ms" in json.loads(lines[0]), True)

_old_max = _T.MAX_SPANS
_T.MAX_SPANS = 3
try:
    rec4 = TraceRecorder(model="m1")
    rec4.start_run()
    for i in range(10):
        rec4.record_chat(i + 1, 100.0 + i, 100.5 + i, _Usage(i=1, o=1))
    overflow = len(rec4.spans)
finally:
    _T.MAX_SPANS = _old_max           # 一定要还原，否则污染后面所有断言
check("超过 MAX_SPANS 就停止记录（防归档膨胀）", overflow, 3)
check("MAX_SPANS 已还原", _T.MAX_SPANS, _old_max)

print("\n── 11. RunResult 的成本字段（界面/归档靠它）──")


def _check_runresult():
    from rongke.loop import RunResult
    from rongke.llm import Usage
    r = RunResult(success=True, reason="ok", steps=3, usage=Usage(100, 20, 5, 0, 0))
    d = r.to_dict()
    check("RunResult 默认成本是 UNKNOWN 而不是 0", d["cost_usd"], "UNKNOWN")
    check("RunResult 的 cost_display 如实写 UNKNOWN", d["cost_display"], "UNKNOWN")
    check("RunResult 暴露缓存命中率", "cache_ratio" in d, True)
    check("usage.cache_ratio：无输入时为 0（不除零）", Usage().cache_ratio, 0.0)
    check("usage.cache_ratio：400/1000", Usage(1000, 0, 0, 400, 0).cache_ratio, 0.4)


try:
    _check_runresult()
except Exception as e:
    print(f"  [SKIP] 无法导入 rongke.loop（{type(e).__name__}: {e}）")

print("\n── 12. 承重自检（两条：词汇表 + 未闭合判据）──")
# 12.1 词汇表：把白名单换成**恒真**的替身 —— 等价于 re.compile(r"(?!)") 的"永不拒绝"
probe_vocab = mk_span(attrs={"gen_ai.operation.name": "chat", "瞎编的键": 1})
live = bool(_T.vocabulary_problems(probe_vocab))
_old_allowed = _T.ALLOWED_ATTRS


class _AcceptEverything(frozenset):
    def __contains__(self, item):        # noqa: D105
        return True


_T.ALLOWED_ATTRS = _AcceptEverything()
try:
    dead = bool(_T.vocabulary_problems(probe_vocab))
finally:
    _T.ALLOWED_ATTRS = _old_allowed
check("消掉白名单 ⇒ 越界键不再报（证明词汇表检查承重）", (live, dead), (True, False))
check("ALLOWED_ATTRS 已还原", _T.ALLOWED_ATTRS is _old_allowed, True)

# 12.2 未闭合判据：把谓词换成**恒假** —— 正控必须当场失守
probe_unclosed = mk_span(end=0.0)
live2 = has_problem(span_problems(probe_unclosed), "缺少结束时间戳")
_old_pred = _T._is_unclosed
_T._is_unclosed = lambda s: False        # 永不判"未闭合"
try:
    dead2 = has_problem(span_problems(probe_unclosed), "缺少结束时间戳")
finally:
    _T._is_unclosed = _old_pred
check("消掉未闭合判据 ⇒ 缺 end 不再报（证明这条检查承重）", (live2, dead2), (True, False))
check("_is_unclosed 已还原", _T._is_unclosed is _old_pred, True)

# 12.3 新的"按 kind 分"的零耗时判据：两个方向都要承重
#      ① 名单清空 ⇒ chat 的正控必须失守
#      ② 名单塞进 execute_tool ⇒ 子毫秒工具的负控必须失守（证明它不是碰巧不报）
probe_zero = mk_span(kind="chat", start=100.0, end=100.0)
probe_tool0 = mk_span(name="load_skill", kind="execute_tool", start=100.0, end=100.0,
                      attrs={"gen_ai.operation.name": "execute_tool"})
live3 = has_problem(span_problems(probe_zero), "耗时**恰好为 0**")
live3n = span_problems(probe_tool0)
_old_need = _T._NEEDS_POSITIVE_DURATION
_T._NEEDS_POSITIVE_DURATION = ()
try:
    dead3 = has_problem(span_problems(probe_zero), "耗时**恰好为 0**")
finally:
    _T._NEEDS_POSITIVE_DURATION = _old_need
_T._NEEDS_POSITIVE_DURATION = ("chat", "invoke_agent", "execute_tool")
try:
    dead3n = span_problems(probe_tool0)
finally:
    _T._NEEDS_POSITIVE_DURATION = _old_need
check("清空名单 ⇒ chat 零耗时不再报（正控承重）", (live3, dead3), (True, False))
check("★ 名单塞进 execute_tool ⇒ 子毫秒工具**会**报（证明负控不是碰巧过的）",
      (live3n, bool(dead3n)), ([], True))
check("_NEEDS_POSITIVE_DURATION 已还原",
      _T._NEEDS_POSITIVE_DURATION, ("chat", "invoke_agent"))

print("\n── 13. 用量字段口径（llm._usage_from）：读数不能恒为 0 ──")
# 动因（2026-09-19 实测事故）：解析器原先**只读 Anthropic 的 cache_read_input_tokens**，
# 而本机网关发的命中数在 prompt_tokens_details.cached_tokens ⇒ 读数恒为 0 ⇒
# 「前缀缓存完全没生效」被写进优化靶。实测反证：同前缀第二次 cached_tokens=5120/5342（95.8%）。
# **这是"量不到"被读成"不存在"**。所以这一节测的不是"能不能算对"，
# 而是"**每一个真实存在的字段名都读得出来**"。

# ⚠️ 用**实测抓到的真实原始体**，不是我编的形状（含 Anthropic 残桩 cache_creation_input_tokens）
REAL_COLD = {"prompt_tokens": 5342, "completion_tokens": 2, "total_tokens": 5344,
             "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0, "video_tokens": 0},
             "completion_tokens_details": {"reasoning_tokens": 0, "image_tokens": 0},
             "cache_creation_input_tokens": 0}
REAL_WARM = dict(REAL_COLD)
REAL_WARM["prompt_tokens_details"] = {"cached_tokens": 5120, "audio_tokens": 0, "video_tokens": 0}

check("实测冷调用：无命中 → cache_read=0（负控，不许瞎猜）",
      _L._usage_from({"usage": REAL_COLD}).cache_read, 0)
check("★ 实测热调用：cached_tokens=5120 必须读出来（正控）",
      _L._usage_from({"usage": REAL_WARM}).cache_read, 5120)
check("★ 同一份实测体：input_tokens 口径不受影响（5342 不许串味）",
      _L._usage_from({"usage": REAL_WARM}).input_tokens, 5342)
check("★ 命中比例算得出来（5120/5342）",
      _L._usage_from({"usage": REAL_WARM}).cache_ratio, 0.9584)

for _name, _u, _want in [
    ("Anthropic cache_read_input_tokens",
     {"input_tokens": 2000, "cache_read_input_tokens": 1792}, 1792),
    ("DeepSeek 原生 prompt_cache_hit_tokens",
     {"prompt_tokens": 2000, "prompt_cache_hit_tokens": 1792}, 1792),
    ("扁平写法 cache_hit_tokens",
     {"prompt_tokens": 2000, "cache_hit_tokens": 1792}, 1792),
    ("OpenAI 标准 prompt_tokens_details.cached_tokens",
     {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 1792}}, 1792),
    # 关键负控：Anthropic 那一侧的**值为 0 的残桩**不许盖掉真命中数。
    # 为什么必须有这条：该网关实测就在发 cache_creation_input_tokens=0，
    # 若哪天补一个 cache_read_input_tokens=0，"第一个命中的就返回"会把 5120 静默改回 0。
    ("★ 有 0 值残桩时仍取真命中数（不许被先命中者覆盖）",
     {"prompt_tokens": 2000, "cache_read_input_tokens": 0,
      "prompt_tokens_details": {"cached_tokens": 1792}}, 1792),
    ("一个缓存字段都没有 → 0（负控）", {"prompt_tokens": 2000}, 0),
]:
    check(_name, _L._usage_from({"usage": _u}).cache_read, _want)

# 承重自检：把缓存分支短路成"永远 0" ⇒ 上面那批正控必须**当场失守**
_live_probe = _L._usage_from({"usage": REAL_WARM}).cache_read
_old_cachefn = _L._cache_read_from
_L._cache_read_from = lambda u: 0
try:
    _dead_probe = _L._usage_from({"usage": REAL_WARM}).cache_read
finally:
    _L._cache_read_from = _old_cachefn
check("消掉缓存字段分支 ⇒ 命中数读不回（证明这一节在承重）",
      (_live_probe, _dead_probe), (5120, 0))
check("_cache_read_from 已还原", _L._cache_read_from is _old_cachefn, True)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
