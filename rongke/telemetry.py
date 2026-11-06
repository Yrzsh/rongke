# -*- coding: utf-8 -*-
"""荣珂遥测 —— 零依赖的 span 记录，词汇表对齐 OpenTelemetry GenAI 语义约定。

## 它治的那个病

主循环从第 1 步跑到第 40 步，内核**闭着眼**：它不知道这一趟花了多少钱、
上下文胀到多大、模型调用耗了多久、失败集中在哪。
具体地：`Usage` 只累加不落盘；LLM 调用**只有开始事件、没有结束时间戳**；
证据台账是**扁平的 seq，没有层级**（看不出"这一步花了 40 秒"是模型慢还是工具慢）。

## 为什么抄词汇表而不是引 SDK

OTel 的 GenAI 语义约定在 2026 年 6 月被移出主规范，独立到
`open-telemetry/semantic-conventions-genai`，而那个仓库**零 tagged release** ——
也就是说它至今没有可 pin 的稳定版本。把它作为依赖引进来，= 把地基压在一条还在移动的线上。

荣珂已经把「依赖分裂」这条教训贯彻到 MCP 与图面审计上：
**需要特定依赖的是被调用的那一侧**，由配置指路。所以这里同理：

> **抄词汇表，不抄实现。**

字段名严格沿用 `gen_ai.*` / `agent.*`。将来若要导出到任何 OTLP 后端，
只需写一个薄映射器，**不必回头改内核**。

## 一条纪律

`check_spans()` 的判据**全部是机器可算的**，没有一条依赖语义判断 ——
这是本项目的反复教训（"字面匹配当硬门必误伤"）。
同理，时间戳只判 `end >= start > 0`，不判"这个耗时是否合理"：
`ms=0` 是合法状态（被围栏拦下的调用根本没执行），
把它当异常报出来就是**误报**，而误报会教人学会无视整份报告。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ── 词汇表（对齐 OTel GenAI 语义约定）────────────────────────
# 只有列在这里的键才允许出现在 span.attrs 上。
# **刻意做成显式白名单**：新增字段时要显式加进来，而不是悄悄溜进去 ——
# 否则"对齐 OTel"就会在几个月内退化成一个口号。
GEN_AI_ATTRS: tuple[str, ...] = (
    "gen_ai.operation.name",        # invoke_agent | chat | execute_tool
    "gen_ai.provider.name",         # 服务方标识（OTel 里 gen_ai.system 已废弃）
    "gen_ai.request.model",
    "gen_ai.response.model",        # 与 request.model 不一致时值得警惕（网关换后端）
    "gen_ai.request.temperature",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.reasoning_tokens",
    "gen_ai.usage.cache_read",
    "gen_ai.usage.cache_write",
    "gen_ai.response.finish_reason",  # stop | tool_calls | length …
    "gen_ai.tool.name",
)

AGENT_ATTRS: tuple[str, ...] = (
    "agent.name",
    "agent.id",
    "agent.step",                   # 本 span 属于主循环的第几步
    "agent.iteration_count",        # 一次 run 里模型被调用的总次数
    "agent.latency_ms",
    "agent.cost_usd",               # ⚠️ 未知模型时是字面 "UNKNOWN"，不是 0（见 pricing.py）
    "agent.messages_chars",         # 发出去的消息体量（上下文占用的代理指标）
    "agent.tools_schema_chars",     # 工具 schema 体量（前缀缓存的稳定段）
    "agent.blocked_by",             # 被哪个钩子拦下
    "agent.tool_domain",
    "agent.tool_mutating",
    "agent.result_digest",
    "agent.result_chars",
)

ALLOWED_ATTRS: frozenset[str] = frozenset(GEN_AI_ATTRS + AGENT_ATTRS)

# span 种类 = OTel agent 约定里的 gen_ai.operation.name
KINDS: tuple[str, ...] = ("invoke_agent", "chat", "execute_tool")
# OTel StatusCode
STATUSES: tuple[str, ...] = ("unset", "ok", "error")

# 落盘上限：一次超长任务不该把 trace.jsonl 写成几十兆
# （与 loop._MAX_ARCHIVE_EVENTS 同一个理由）
MAX_SPANS = 2000


# ── span ────────────────────────────────────────────────────
@dataclass
class Span:
    """一个工作单元。层级是 OTel 那套：根 `invoke_agent` → 子 `chat` / `execute_tool`。

    字段刻意与 OTel 对齐（trace_id / span_id / parent_id / start / end / status），
    这样导出映射器就是一次字段改名，不需要重新推演语义。
    """

    name: str
    kind: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    trace_id: str = ""
    parent_id: str = ""
    start: float = field(default_factory=time.time)
    end: float = 0.0                       # 0 = 还没结束（未闭合）
    status: str = "unset"
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if not self.end or self.end < self.start:
            return 0.0
        return round((self.end - self.start) * 1000, 3)

    @property
    def closed(self) -> bool:
        return self.end > 0

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "kind": self.kind,
            "start": round(self.start, 6),
            "end": round(self.end, 6),
            "status": self.status,
            "duration_ms": self.duration_ms,
            "attrs": dict(self.attrs),
            "closed": self.closed,
        }


# ── 校验（全部机器可算）──────────────────────────────────────
def _is_unclosed(span: Span) -> bool:
    """span 是否缺结束时间戳。

    **抽成具名函数是为了让承重自检能替换它** —— 把谓词换成一个恒假的实现，
    "有开始无结束"的正控必须当场失守。不这样做的话，那条正控可能
    其实是**别的原因**在报（G 型缺陷：检查不承重，有正控但不承重）。
    """
    return not span.end


def vocabulary_problems(span: Span) -> list[str]:
    """attrs 里有没有词汇表之外的键。

    读模块级 `ALLOWED_ATTRS` 而不是在函数里捕获它 ——
    这样承重自检可以把白名单换成一个**恒真的**替身
    （等价于把正则换成 `re.compile(r"(?!)")`），验证这条检查真的在承重。
    """
    allowed = ALLOWED_ATTRS
    return [k for k in span.attrs if k not in allowed]


# 这些 kind 的耗时**必须为正**：它们背后是一次网络往返（或整段 agent 执行），
# 不可能在 0 毫秒内完成。恰好等于 0 ⇒ 时间戳没被记上。
#
# 为什么 `execute_tool` **不在**这个名单里（2026-09-18 实测教训）：
# `record_tool` 是用 `start = end - ms/1000` **反推**起点的，而注册表把毫秒
# 取整存进台账 ⇒ 读一个本地文件（`load_skill` / `writing_brief`）真的会在
# 1ms 内返回、`ms` 截断成 0 ⇒ 该 span 必然 `end == start`。
# 这不是缺陷，**是记录方式的必然产物**。把它判成"异常"就是误报，
# 而误报会教人学会无视整份体检报告（本项目最贵的一条教训）。
# 注：这条规则只回答"耗时是不是 0"，**不**回答"这个工具该不该花这么久"。
_NEEDS_POSITIVE_DURATION: tuple[str, ...] = ("chat", "invoke_agent")


def timing_problems(span: Span) -> list[str]:
    """时间戳判据 —— **按 kind 分类，且只在这里定义一处**。

    单一定义处的理由：我曾在一份**外部验收脚本**里另写了一遍同样的判据、
    而且写宽了（对工具也要求 `end > start > 0`），于是 3 个子毫秒工具被误判成坏。
    判据一旦有第二份拷贝，它就会漂移 —— 那是 B 型缺陷（两处真相）。
    """
    if span.start <= 0:
        return [f"span {span.span_id}({span.name}) 的 start 非正"]
    if _is_unclosed(span):
        return [f"span {span.span_id}({span.name}) **缺少结束时间戳**（有开始无结束）"]
    if span.end < span.start:
        return [f"span {span.span_id}({span.name}) 的 end 早于 start"]
    if span.kind in _NEEDS_POSITIVE_DURATION and span.end == span.start:
        return [f"span {span.span_id}({span.name}) 的 {span.kind} 耗时**恰好为 0**"
                f" —— 一次网络往返不可能是瞬时的，时间戳八成没记上"]
    return []


def span_problems(span: Span) -> list[str]:
    """单个 span 的结构性问题。返回问题清单（空 = 干净）。

    判据刻意保守 —— 每一条都对应一个**具体的真实现象**，没有"看着不对"这类：
      * `start <= 0`         —— start 没被赋值
      * `end == 0`           —— **有开始无结束**（正是要修的那个病）
      * `end < start`        —— 时间倒流
      * `end == start`       —— **按 kind 分**：chat / invoke_agent 必报
                                （见 `_NEEDS_POSITIVE_DURATION`）；
                                execute_tool 不报（子毫秒工具合法）
      * kind / status 越界   —— 打错枚举值
      * operation.name 与 kind 不一致 —— 两处真相

    时间戳那几条**全部委托给 `timing_problems`**，此处不重写。
    """
    out: list[str] = list(timing_problems(span))
    if span.kind not in KINDS:
        out.append(f"span {span.span_id} 的 kind={span.kind!r} 不在 {KINDS}")
    if span.status not in STATUSES:
        out.append(f"span {span.span_id} 的 status={span.status!r} 不在 {STATUSES}")
    op = span.attrs.get("gen_ai.operation.name")
    if op is not None and op != span.kind:
        out.append(f"span {span.span_id} 的 gen_ai.operation.name={op!r} 与 kind={span.kind!r} 不一致")
    for k in vocabulary_problems(span):
        out.append(f"span {span.span_id} 的属性 {k!r} 不在词汇表白名单里")
    return out


def check_spans(spans: list[Span]) -> list[str]:
    """整条 trace 的问题清单（空 = 干净）。给测试与体检用。"""
    out: list[str] = []
    ids: set[str] = set()
    for s in spans:
        if s.span_id in ids:
            out.append(f"span_id 重复：{s.span_id}")
        ids.add(s.span_id)
        out.extend(span_problems(s))
    for s in spans:
        if s.parent_id and s.parent_id not in ids:
            out.append(f"span {s.span_id} 的 parent_id={s.parent_id} 指向不存在的 span（悬空父节点）")
    return out


def open_spans(spans: list[Span]) -> list[Span]:
    """还没闭合的 span。任何非空结果都是缺陷 —— 它意味着"有开始无结束"。"""
    return [s for s in spans if not s.closed]


# ── 时间账（"声明 ≠ 事实"的桥）───────────────────────────────
def timing_report(spans: list[Span], elapsed_s: float) -> dict:
    """把 span 时间与墙钟时间对起来。

    为什么要它：span 时长是**声明**，墙钟是**事实**。若一条 trace 号称覆盖了
    30 秒而整个任务跑了 200 秒，说明有 170 秒的活动**根本没被埋点** ——
    这种"看不见的时间"必须能被发现，否则可观测性就是自欺。

    **只用顶层 span 求和**：子 span 嵌在父 span 里，直接全量相加会重复计数，
    造出一个比墙钟还大的"总耗时"，那是假数。
    """
    top = [s for s in spans if not s.parent_id]
    span_total = round(sum(s.duration_ms for s in top), 3)
    elapsed_ms = round(float(elapsed_s) * 1000, 3)
    if top:
        covered = round((max(s.end for s in top) - min(s.start for s in top)) * 1000, 3)
    else:
        covered = 0.0
    # 覆盖缺口：墙钟里没被任何顶层 span 覆盖的部分
    gap = round(max(0.0, elapsed_ms - covered), 3)
    return {
        "spans": len(spans),
        "top_level_spans": len(top),
        "span_total_ms": span_total,
        "span_covered_ms": covered,
        "elapsed_ms": elapsed_ms,
        "uncovered_ms": gap,
        "uncovered_pct": round(gap / elapsed_ms * 100, 1) if elapsed_ms > 0 else 0.0,
        "oneline": (f"span 覆盖 {covered:.0f}ms / 墙钟 {elapsed_ms:.0f}ms"
                    f"（未埋点 {gap:.0f}ms, {round(gap / elapsed_ms * 100, 1) if elapsed_ms > 0 else 0.0}%）"),
    }


# ── 记录器 ──────────────────────────────────────────────────
class TraceRecorder:
    """收集 span。既实现钩子接口（记工具调用），也提供显式的起止（记模型调用）。

    为什么它同时是 Hook：本仓库原本已经有一个 `AuditLog` 钩子（hooks.py），
    定义了 `after_call` 却**从未被实例化** —— 是一段死代码。
    把那个位置转正，好过另起一套日志系统（"平行机制 = 自加维护面"）。
    """

    name = "telemetry"

    def __init__(self, agent_name: str = "rongke", model: str = "",
                 provider: str = "", trace_id: str | None = None) -> None:
        self.trace_id = trace_id or uuid.uuid4().hex[:16]
        self.agent_name = agent_name
        self.model = model
        self.provider = provider
        self.spans: list[Span] = []
        self._open: dict[str, Span] = {}
        self._root_id = ""

    # ── 起止 ────────────────────────────────────────────────
    def start(self, name: str, kind: str, attrs: dict | None = None,
              parent: str | None = None) -> str:
        if len(self.spans) >= MAX_SPANS:
            return ""
        pid = self._root_id if parent is None else parent
        sp = Span(name=name, kind=kind, trace_id=self.trace_id, parent_id=pid,
                  attrs=dict(attrs or {}))
        self.spans.append(sp)
        self._open[sp.span_id] = sp
        if kind == "invoke_agent" and not self._root_id:
            self._root_id = sp.span_id
        return sp.span_id

    def end(self, span_id: str, attrs: dict | None = None,
            status: str = "ok", end: float | None = None) -> Span | None:
        sp = self._open.pop(span_id, None)
        if sp is None:
            return None
        if attrs:
            sp.attrs.update(attrs)
        sp.end = end if end is not None else time.time()
        sp.status = status if status in STATUSES else "unset"
        return sp

    def fail(self, span_id: str, attrs: dict | None = None, end: float | None = None) -> Span | None:
        return self.end(span_id, attrs=attrs, status="error", end=end)

    # ── 语义化包装 ──────────────────────────────────────────
    def start_run(self, goal_chars: int = 0) -> str:
        return self.start("invoke_agent", "invoke_agent",
                          {"gen_ai.operation.name": "invoke_agent",
                           "agent.name": self.agent_name,
                           "gen_ai.provider.name": self.provider,
                           "gen_ai.request.model": self.model,
                           "agent.messages_chars": goal_chars})

    def record_chat(self, step: int, start: float, end: float, usage: Any,
                    finish_reason: str = "", messages_chars: int = 0,
                    tools_schema_chars: int = 0, cost: Any = None,
                    status: str = "ok") -> None:
        """把一个模型调用记成 chat span（含**结束时间戳**）。

        这是本次修复的核心：原实现只在调用前广播 `llm_call`，
        调用**没有结束时间戳** ⇒ 模型耗时完全不可见。
        """
        if len(self.spans) >= MAX_SPANS:
            return
        u = usage
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self.provider,
            "gen_ai.request.model": self.model,
            "agent.step": step,
            "agent.latency_ms": round((end - start) * 1000, 3),
            "agent.messages_chars": messages_chars,
            "agent.tools_schema_chars": tools_schema_chars,
        }
        if u is not None:
            attrs.update({
                "gen_ai.usage.input_tokens": int(getattr(u, "input_tokens", 0) or 0),
                "gen_ai.usage.output_tokens": int(getattr(u, "output_tokens", 0) or 0),
                "gen_ai.usage.reasoning_tokens": int(getattr(u, "reasoning_tokens", 0) or 0),
                "gen_ai.usage.cache_read": int(getattr(u, "cache_read", 0) or 0),
                "gen_ai.usage.cache_write": int(getattr(u, "cache_write", 0) or 0),
            })
        if finish_reason:
            attrs["gen_ai.response.finish_reason"] = finish_reason
        if cost is not None:
            attrs["agent.cost_usd"] = cost
        sp = Span(name="chat", kind="chat", trace_id=self.trace_id,
                  parent_id=self._root_id, start=start, end=end,
                  status=status if status in STATUSES else "unset", attrs=attrs)
        self.spans.append(sp)

    def record_tool(self, call: Any) -> Span | None:
        """把一个工具调用记成 execute_tool span。

        耗时直接取台账的 `ms`（注册表已经量过了，不重复计时），
        结束时刻取 `ts`（ToolCall 构造时刻）—— 两者相减即 start，
        不需要动注册表。
        """
        if len(self.spans) >= MAX_SPANS:
            return None
        end = float(getattr(call, "ts", 0) or 0) or time.time()
        ms = float(getattr(call, "ms", 0) or 0)
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": str(getattr(call, "name", "")),
            "agent.tool_domain": str(getattr(call, "domain", "")),
            "agent.tool_mutating": bool(getattr(call, "mutating", False)),
            "agent.result_chars": len(str(getattr(call, "result", "") or "")),
        }
        blocked = str(getattr(call, "blocked_by", "") or "")
        if blocked:
            attrs["agent.blocked_by"] = blocked
        try:
            attrs["agent.result_digest"] = call.digest()
        except Exception:
            pass
        sp = Span(name=str(getattr(call, "name", "tool")), kind="execute_tool",
                  trace_id=self.trace_id, parent_id=self._root_id,
                  start=end - ms / 1000.0, end=end,
                  status="ok" if getattr(call, "ok", False) else "error",
                  attrs=attrs)
        self.spans.append(sp)
        return sp

    def finish(self, cost: Any = None, iteration_count: int = 0,
               status: str = "ok") -> None:
        """闭合根 span（把总账挂上去）。"""
        if not self._root_id:
            return
        attrs: dict[str, Any] = {"agent.iteration_count": iteration_count}
        if cost is not None:
            attrs["agent.cost_usd"] = cost
        self.end(self._root_id, attrs=attrs, status=status)

    # ── 钩子接口（工具调用自动留痕）─────────────────────────
    def before_call(self, tool: Any, args: dict):
        from .hooks import HookDecision
        return HookDecision.ok()

    def after_call(self, call: Any) -> None:
        try:
            self.record_tool(call)
        except Exception:
            pass          # 遥测失败绝不许影响任务本身

    # ── 导出 ────────────────────────────────────────────────
    def jsonl(self, limit: int = MAX_SPANS) -> str:
        rows = self.spans[:limit]
        return "\n".join(json.dumps(s.to_dict(), ensure_ascii=False, default=str)
                         for s in rows)

    def summary(self, elapsed_s: float, cost: Any = None,
                llm_calls: int = 0) -> dict:
        """给 session.json / 控制台用的摘要。"""
        rep = timing_report(self.spans, elapsed_s)
        chats = [s for s in self.spans if s.kind == "chat"]
        tools = [s for s in self.spans if s.kind == "execute_tool"]
        return {
            **rep,
            "llm_calls": llm_calls or len(chats),
            "tool_spans": len(tools),
            "llm_ms_total": round(sum(s.duration_ms for s in chats), 3),
            "tool_ms_total": round(sum(s.duration_ms for s in tools), 3),
            "tool_errors": len([s for s in tools if s.status == "error"]),
            "blocked": len([s for s in self.spans
                            if s.attrs.get("agent.blocked_by")]),
            "cost_usd": cost if cost is not None else "UNKNOWN",
            "open_spans": len(open_spans(self.spans)),
            "problems": check_spans(self.spans),
        }
