#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上下文经济探针 —— 先量，再改。

    python ctx_econ.py                # 扫 runs/2*/ 真归档，逐条判红线（人读报告）
    python ctx_econ.py --save         # 顺手把这份数存成 before 基线
    python ctx_econ.py --against      # 跟 before 基线比，报相对改善（验收用）
    python ctx_econ.py --offline      # 合成夹具 + 负对照（这条才是回归项）

## 为什么这个文件必须先写

它不改内核、不调模型、不写归档（除了 `--save` 那一份基线）。存在的理由只有一条：

**「A 压缩 + C 预算闸」的验收全是相对量（↓≥40%）。先改代码再补探针，before 值就永久
丢了** —— 那时任何"改善了"的说法都无法被反驳，也就无法被相信。

## 为什么它自带负对照

`regress.py` 的自检注释写过这件事：判决工具红了会有人管，**它自己却报"全绿"是最难
被发现的失败**。所以 `--offline` 里每个判据都配一份**故意做坏**的夹具，要求它必须
塌红。塌不了 ⇒ 这条判据不存在，只是一件装饰。

## 测不到 ≠ 通过

09-18 早上三场的 `llm_call` 还没有 `messages_chars` / `tools_schema_chars` 字段，
09-18 晚些的 `cache_read` 恒 0（探针只认 Anthropic 字段名，口径在 09-19 修过）。
这些**一律报 NA 并写明为什么**，绝不计入通过 —— 否则"字段还没埋"会被读成"这条过了"，
正是本项目反复记的那类静默失败。
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import sys
from pathlib import Path

# Windows 控制台默认 GBK，报告里的 `⇒` / `✔` 一打印就抛 UnicodeEncodeError ⇒
# **退出码 1，看着像"判据抓到了坏"，其实是探针自己崩了**。与 template_test.py 同一
# 处治法：把 stdout 换成 UTF-8。回归运行器不设 PYTHONIOENCODING，所以每个脚本自己换。
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = Path(__file__).resolve().parent
ARCHIVE_GLOB = "2*/session.json"          # 只认日期目录；runs/_* 是夹具，不是真跑
SNAP = ROOT / "runs" / "_ctx_econ" / "before.json"

# 台账摘要的识别锚。★ 从内核**导入**而不是在这里抄一遍 —— 抄一份就等于制造
# 第二个真相，内核改字的那一刻这条跨源核对会静默地永远判"没摘要"。
# 导入失败时退回本地钉死的副本，并如实标 degraded：探针要能测量内核，
# 就不该因为内核坏了而一起瘫掉（那正是"测量工具坏了、于是把失败读成成功"）。
try:
    sys.path.insert(0, str(ROOT))
    from rongke.context import BRIEF_MARK
    BRIEF_SRC = "rongke.context"
except Exception as _e:                                       # noqa: BLE001
    BRIEF_MARK = "【上下文出清·台账摘要】"
    BRIEF_SRC = f"本地副本（导入失败：{type(_e).__name__}）⇒ 跨源核对不可信"

# ── 红线阈值 ─────────────────────────────────────────────────
# 判"坏"的门槛。定 these 的原则：**现在的归档必须塌**，否则这条判据是在表扬现状。
GROWTH_RED_CHARS = 20_000        # 一场任务历史至少长出这么多，才算"实质增长"
COST_RED_INPUT_TOKENS = 400_000  # 单任务整场输入 token 红线（实测成功档在 37k~62k）
CACHE_FLOOR = 0.90               # 前缀缓存命中中位数下限（现 0.88~0.965）
COMPACT_MAX_PER_RUN = 1          # 压缩是急刹车：一场超过 1 次＝阈值定错了


# ── 提取 ─────────────────────────────────────────────────────
def _ev(session: dict, kind: str) -> list[dict]:
    return [e.get("data") or {} for e in (session.get("events") or [])
            if e.get("kind") == kind]


def metrics(session: dict) -> dict:
    """从一份 session.json 抽出上下文经济的全部可测量。缺的字段留 None。

    返回里 `_na` 记下**哪些量测不到、为什么** —— 这份清单和数字同等重要：
    报告若只印数字，读的人会把"没有这一行"当成"没问题"。
    """
    res = session.get("result") or {}
    usage = res.get("usage") or {}
    na: list[str] = []

    calls = _ev(session, "llm_call")
    dones = _ev(session, "llm_done")
    mounted = _ev(session, "tools_mounted")

    msg_chars = [c.get("messages_chars") for c in calls]
    schema_chars = [c.get("tools_schema_chars") for c in calls]
    if any(v is None for v in msg_chars) or not msg_chars:
        msg_chars = []
        na.append("messages_chars：这批归档还没埋这个字段（09-18 早上格式）")
    if any(v is None for v in schema_chars) or not schema_chars:
        schema_chars = []
        na.append("tools_schema_chars：同上")

    prompt_chars = ([m + s for m, s in zip(msg_chars, schema_chars)]
                    if msg_chars and schema_chars else [])

    peak_msg = max(msg_chars) if msg_chars else None
    growth = (msg_chars[-1] - msg_chars[0]) if len(msg_chars) >= 2 else None
    slope = None
    if len(msg_chars) >= 2:
        slope = growth / (len(msg_chars) - 1)

    ins = [((d.get("usage") or {}).get("input_tokens")) for d in dones]
    ins = [v for v in ins if v is not None]
    peak_in = max(ins) if ins else None
    if not ins:
        na.append("逐步 input_tokens：没有 llm_done 事件")

    # floor_share = schema 在整场 prompt 字节里占的比例。**这是本轮推翻"裁 schema
    # 是头号成本"那条旧估计的度量**：旧口径量的是首步占比（82.9%），分母只有一步。
    floor_share = None
    if prompt_chars and schema_chars:
        tot = sum(prompt_chars)
        floor_share = (sum(schema_chars) / tot) if tot else None

    cache_series = [d.get("cache_ratio") for d in dones
                    if d.get("cache_ratio") is not None]
    cache_med = statistics.median(cache_series) if cache_series else None
    # 恒 0 的 cache_ratio 是**坏探针**（只认 Anthropic 字段名）而非"没命中"。
    # 把它当 0 来判红，等于用一把已知失灵的尺子下结论（09-19 的口径事故）。
    if cache_med is not None and max(cache_series) == 0:
        cache_med = None
        na.append("cache_ratio：全程恒 0 —— 这是旧探针的口径病，不是真没命中缓存")
    elif cache_med is None:
        na.append("cache_ratio：这批归档没有该字段")

    compact = len(_ev(session, "context_compact"))
    skipped = len(_ev(session, "context_compact_skipped"))
    warn = len(_ev(session, "context_warn"))
    overflow = len(_ev(session, "context_overflow"))
    budget_notes = len(_ev(session, "budget_note")) + len(_ev(session, "budget_limit"))
    hints = len(_ev(session, "wrapup_hint"))

    # ── 跨源核对：埋点与记账是不是同一个数 ─────────────────────
    # R1 只认 `context_compact` 这一个事件名。而**事件发不出去、代码却照样跑完**，
    # 于是 R1 会永远判绿 —— 一个永不触发的判据比没有判据更危险（它让人以为有人在看）。
    # 两个独立来源：① `result.compacted`（内核记的最后一次出清的事实）；
    # ② 消息栈尾部那条台账摘要（`BRIEF_MARK`，只有真出清过才会存在）。
    # 真归档里没有整份消息栈 ⇒ ① 才是主力，② 服务夹具与将来的全量归档。
    logged = res.get("compacted")
    has_logged = isinstance(logged, dict) and bool(logged)
    stack = (session.get("_snapshot") or {}).get("messages") or []
    has_brief = None
    if stack:
        has_brief = any(isinstance(m.get("content"), str) and
                        str(m.get("content")).startswith(BRIEF_MARK) for m in stack)
    mismatch = ""
    if has_logged and compact == 0:
        mismatch = ("result.compacted 记着出清发生过，却一次 context_compact 都没发 "
                    "⇒ 账上有、人看不到（前端与评测会双双漏计）")
    elif compact > 0 and not has_logged:
        mismatch = (f"发了 {compact} 次 context_compact，result.compacted 却是空的 "
                    "⇒ 事件与真出清脱钩（emit 了，但没记账）")
    elif has_brief is False and compact > 0:
        mismatch = f"发了 {compact} 次事件，消息栈里却找不到台账摘要 ⇒ 事件是空的"
    elif has_brief and compact == 0 and skipped == 0:
        mismatch = "消息栈里有台账摘要（出清远发生过），却一次事件都没发 ⇒ 没被记账"

    return {
        "run_id": session.get("id") or "",
        "goal": (session.get("goal") or "")[:70],
        "success": res.get("success"),
        "reason": str(res.get("reason") or ""),
        "steps": res.get("steps"),
        "total_input_tokens": usage.get("input_tokens"),
        "cache_ratio_final": res.get("cache_ratio"),
        "cache_ratio_median": cache_med,
        "tools_available": calls[0].get("tools_available") if calls else None,
        "mounted": (mounted[0].get("allow") if mounted else None),
        "mounted_total": (mounted[0].get("total") if mounted else None),
        "first_prompt_chars": prompt_chars[0] if prompt_chars else None,
        "schema_chars": schema_chars[0] if schema_chars else None,
        "peak_msg_chars": peak_msg,
        "msg_growth_chars": growth,
        "msg_growth_slope": slope,
        "floor_share": floor_share,
        "peak_input_tokens": peak_in,
        "compact_events": compact,
        "compact_skipped": skipped,
        "cross_check": mismatch,
        # 有几个**独立**来源可核。0 个就判 NA，绝不判"两侧一致"——
        # 现存 19 份真归档既没有 result.compacted 也没有消息栈，判绿就是假绿。
        "cross_sources": (1 if logged is not None else 0)
        + (1 if has_brief is not None else 0),
        "context_warn_events": warn,
        "overflow_events": overflow,
        "budget_note_events": budget_notes,
        "wrapup_hint_events": hints,
        "monotonic_growth": (bool(msg_chars) and all(
            b >= a for a, b in zip(msg_chars, msg_chars[1:]))),
        "_na": na,
        "_brief_src": BRIEF_SRC,
    }


# ── 判决 ─────────────────────────────────────────────────────
def judge(m: dict) -> list[tuple[str, str, str, str]]:
    """返回 [(编号, 名称, 结论, 依据)]；结论 ∈ {红, 绿, NA}。

    NA 不是通过。见文件头「测不到 ≠ 通过」。
    """
    out: list[tuple[str, str, str, str]] = []

    def add(rid, name, verdict, why):
        out.append((rid, name, verdict, why))

    g, c = m["msg_growth_chars"], m["compact_events"]
    # R1 —— 只增不减。这条是本轮改动要修的正主。
    if g is None:
        add("R1", "上下文只增不减", "NA", "无 messages_chars")
    elif g >= GROWTH_RED_CHARS and c == 0:
        add("R1", "上下文只增不减", "红",
            f"历史长了 {g:,} 字符，内核一次都没动过手（context_compact=0）")
    else:
        add("R1", "上下文只增不减", "绿", f"长了 {g:,} 字符 / 压缩 {c} 次")

    # R2 —— 溢出这条硬崩路径有没有被兜住。没有样本就如实说没有。
    if m["overflow_events"] == 0 and "执行中断" not in m["reason"]:
        add("R2", "溢出未兜底", "NA",
            "本轮无溢出样本（溢出=硬崩那条路径要靠夹具证，见 --offline）")
    elif "执行中断" in m["reason"] and m["compact_events"] == 0:
        add("R2", "溢出未兜底", "红", f"崩在「{m['reason'][:40]}」且无降级动作")
    else:
        add("R2", "溢出未兜底", "绿", f"overflow={m['overflow_events']}")

    # R3 —— 膨胀可见性：长出来了，却连一声都没吭。
    if g is None:
        add("R3", "膨胀不可见", "NA", "无 messages_chars")
    elif g >= GROWTH_RED_CHARS and (m["context_warn_events"] + c) == 0:
        add("R3", "膨胀不可见", "红",
            f"长了 {g:,} 字符，context_warn=0 且 compact=0（阈值默认关）")
    else:
        add("R3", "膨胀不可见", "绿",
            f"warn={m['context_warn_events']} compact={c}")

    # R4 —— 预算闸：烧过红线却没有任何拦阻信号。
    ti = m["total_input_tokens"]
    if ti is None:
        add("R4", "无预算闸", "NA", "无 usage.input_tokens")
    elif ti >= COST_RED_INPUT_TOKENS and m["budget_note_events"] == 0:
        add("R4", "无预算闸", "红",
            f"整场 {ti:,} 输入 token（红线 {COST_RED_INPUT_TOKENS:,}），"
            f"全程零道预算闸（budget_note=0）")
    else:
        add("R4", "无预算闸", "绿", f"{ti:,} token / budget_note={m['budget_note_events']}")

    # R5 —— 耗尽无收尾：撞满步数，模型事先没收到过一次"该收尾了"。
    exhausted = ("最大步数" in m["reason"]) or ("步数耗尽" in m["reason"])
    if m["steps"] is None:
        add("R5", "耗尽无收尾", "NA", "无 steps")
    elif exhausted and m["wrapup_hint_events"] == 0:
        add("R5", "耗尽无收尾", "红",
            f"{m['steps']} 步撞满、判「未完成」，期间没有任何收尾提示")
    else:
        add("R5", "耗尽无收尾", "绿",
            "未撞满步数" if not exhausted else f"hint={m['wrapup_hint_events']}")

    # R6 —— 跨源核对：埋点与记账是不是同一个数。
    # 没有这条，R1 就是**单源判据**：只要 `emit("context_compact")` 那行没执行
    # （改名、提前 return、被异常吞掉），R1 就永远判绿，而压缩代码看着在跑。
    if m["cross_check"]:
        add("R6", "出清已记账（跨源）", "红", m["cross_check"])
    elif m.get("_brief_src", "").startswith("本地副本"):
        add("R6", "出清已记账（跨源）", "NA", f"BRIEF_MARK 来自{m['_brief_src']}")
    elif not m["cross_sources"]:
        # 现存 19 份真归档既没有 result.compacted 也没有消息栈 ⇒ 无从核对。
        # 这里若判绿，就是拿"没有反证"当"证据"。
        add("R6", "出清已记账（跨源）", "NA", "这份归档里没有可独立核对的来源")
    elif m["compact_events"]:
        add("R6", "出清已记账（跨源）", "绿",
            f"{m['compact_events']} 次出清，事件与记录一致")
    else:
        add("R6", "出清已记账（跨源）", "绿", "无出清、无记录（一致）")

    # S1 —— 哨兵，不是判据：防"用 cache 命中率换字节数"的假改善。
    cm = m["cache_ratio_median"]
    if cm is None:
        add("S1", "哨兵·缓存命中", "NA", "见下方 NA 清单")
    else:
        add("S1", "哨兵·缓存命中", "绿" if cm >= CACHE_FLOOR else "红",
            f"中位 {cm:.3f}（下限 {CACHE_FLOOR}）")

    # S2 —— 哨兵：压缩超过 1 次＝把急刹车当例行打扫，每步一次 cache miss。
    # ★ 一条观察都没发生时判 NA。这里曾经判过绿（空归档"全绿"），那就是
    #   本项目最怕的形状：检查在，但它什么都不知道却报了好消息。
    if m["steps"] is None and g is None:
        add("S2", "哨兵·压缩不过频", "NA", "这场没有任何可观察的运行数据")
    elif c <= COMPACT_MAX_PER_RUN:
        add("S2", "哨兵·压缩不过频", "绿", f"context_compact={c}（上限 {COMPACT_MAX_PER_RUN}）")
    else:
        add("S2", "哨兵·压缩不过频", "红",
            f"context_compact={c} 超上限 {COMPACT_MAX_PER_RUN} —— 急刹车被当成了例行打扫")
    return out


def aggregate(ms: list[dict]) -> dict:
    """把多份归档的度量折成一组中位数 —— `compare()` 的两个输入都是它的产物。

    只收"可测的那些"：某份归档缺 `floor_share`，它就不参与 floor_share 的中位数，
    而不是被当成 0 拉低均值。**缺位与零是两个事实**（这条在 09-19 的用量少报上栽过）。
    """
    def med(key: str):
        vals = [m[key] for m in ms if isinstance(m.get(key), (int, float))]
        return statistics.median(vals) if vals else None

    return {
        "runs": len(ms),
        "success": sum(1 for m in ms if m.get("success")),
        "steps_median": med("steps"),
        "total_input_median": med("total_input_tokens"),
        "first_prompt_median": med("first_prompt_chars"),
        "floor_share_median": med("floor_share"),
        "peak_input_median": med("peak_input_tokens"),
        "growth_slope_median": med("msg_growth_slope"),
        "cache_median": med("cache_ratio_median"),
        "compact_total": sum(m.get("compact_events") or 0 for m in ms),
        "red_total": 0,          # 由调用方填
    }


# ── 相对验收（改动后跑 --against） ────────────────────────────
def compare(base: dict, now: dict) -> list[tuple[str, str, str]]:
    """把验收§3 的六条相对判据算出来。返回 [(名称, 结论, 依据)]。

    before/now 都是 aggregate() 的产物。**任一侧缺数 ⇒ NA，不许蒙判通过。**
    """
    out = []

    def down(name, key, need_pct, sentinel=False):
        b, n = base.get(key), now.get(key)
        if not isinstance(b, (int, float)) or not isinstance(n, (int, float)) or not b:
            out.append((name, "NA", f"before={b} after={n}"))
            return
        d = (b - n) / b * 100.0
        if sentinel:
            out.append((name, "绿" if d >= 0 else "红",
                        f"{b:,.0f}→{n:,.0f}（{'降' if d>=0 else '升'} {abs(d):.1f}%，哨兵：不许升）"))
        else:
            out.append((name, "绿" if d >= need_pct else "红",
                        f"{b:,.0f}→{n:,.0f} 降 {d:.1f}%（要求 ≥{need_pct}%）"))

    down("峰值 prompt token ↓", "peak_input_median", 40)
    down("每步新增字符 ↓", "growth_slope_median", 40)
    down("首步 prompt 不升", "first_prompt_median", 0)
    b, n = base.get("steps_median"), now.get("steps_median")
    if not isinstance(b, (int, float)) or not isinstance(n, (int, float)):
        out.append(("步数不升 >+1", "NA", f"before={b} after={n}"))
    else:
        out.append(("步数不升 >+1", "绿" if n <= b + 1 else "红", f"{b}→{n}"))
    b, n = base.get("cache_median"), now.get("cache_median")
    if not isinstance(b, (int, float)) or not isinstance(n, (int, float)):
        out.append(("缓存命中哨兵", "NA", "有一侧不可测（09-18 旧探针口径不可用）"))
    else:
        out.append(("缓存命中哨兵", "绿" if n >= CACHE_FLOOR else "红",
                    f"{b:.3f}→{n:.3f}（下限 {CACHE_FLOOR}）"))
    b, n = base.get("success"), now.get("success")
    out.append(("成功率不得转红", "NA" if n is None else ("绿" if n >= (b or 0) else "红"),
                f"{b}→{n}"))
    return out


# ── 合成夹具（--offline 的地基） ──────────────────────────────
# 每个夹具产一份**形状与真归档一致**的 session dict。之所以是 dict 而不是落盘文件：
# 探针不该在自己的测试里往 runs/ 堆影子目录（09-21 踩过：27 份 _probe_loop_*.py）。
def _session(steps, msg0, msg_step, schema_chars, tools, in_tok, cache,
             *, success=True, reason="证据门通过。", kinds=(), brief=None,
             compacted="auto"):
    """造一份形状与真归档一致的 session。

    `brief` / `compacted` = 两个**独立**的记账来源。默认都跟着 `kinds` 里有没有
    `context_compact` 走 —— 夹具的默认职责是"自洽"，让 R6 安静；要单测 R6 时
    才显式把某一侧掰开（见 `event_not_real` / `record_not_emitted`）。
    """
    ev: list[dict] = [
        {"kind": "tools_mounted", "data": {"allow": tools, "total": tools}, "t": "0"},
    ]
    chars = msg0
    tok = in_tok
    stack = [{"role": "system", "content": "S" * 200},
             {"role": "user", "content": "契约" * 300}]
    for i in range(1, steps + 1):
        ev.append({"kind": "llm_call", "data": {
            "n": i, "tools_available": tools, "messages": 2 + i * 3,
            "messages_chars": int(chars), "tools_schema_chars": schema_chars}, "t": "0"})
        ev.append({"kind": "llm_done", "data": {
            "n": i, "ms": 100.0, "cache_ratio": cache, "finish_reason": "tool_calls",
            "usage": {"input_tokens": int(tok), "output_tokens": 50,
                      "reasoning_tokens": 0, "cache_read": int(tok * cache),
                      "cache_write": 0}}, "t": "0"})
        stack.append({"role": "assistant", "content": "a" * 900, "tool_calls": []})
        stack.append({"role": "tool", "tool_call_id": "c%d" % i, "content": "x" * 4000})
        chars += msg_step
        tok = int(tok * 1.06)
    for k in kinds:
        ev.append({"kind": k, "data": {}, "t": "0"})
    fired = any(k == "context_compact" for k in kinds)
    if brief is None:
        brief = fired
    if brief:
        stack.append({"role": "user", "content": BRIEF_MARK + " 一长串台账事实。"})
    if compacted == "auto":
        compacted = {"levels": [1], "freed_chars": 4000, "n": sum(
            1 for k in kinds if k == "context_compact")} if fired else None
    tot_in = 0
    for e in ev:
        if e["kind"] == "llm_done":
            tot_in += e["data"]["usage"]["input_tokens"]
    ev.append({"kind": "step", "data": {"n": steps}, "t": "0"})
    res = {"success": success, "reason": reason, "steps": steps,
           "usage": {"input_tokens": tot_in, "output_tokens": 50 * steps},
           "cache_ratio": cache}
    if compacted:
        res["compacted"] = compacted
    return {"id": "fixture", "goal": "夹具", "events": ev,
            "_snapshot": {"messages": stack}, "result": res}


def fixtures() -> list[tuple[str, str, dict, set[str]]]:
    """[(名字, 说明, session, 期望判红的编号集合)]。

    "健康"夹具是**正控**：它必须一条红线都不塌。它若塌了，说明判据在表扬错东西。
    其余每个都是**负控**：只破坏一件事，且必须只把那一件判出来。
    """
    return [
        ("healthy", "改动后应有的样子：长出来了、压过一次、烧得不多、没撞步数",
         _session(9, 8_000, 6_000, 30_000, 69, 18_000, 0.94,
                  kinds=("context_compact", "context_warn", "wrapup_hint",
                         "budget_note")),
         set()),
        ("no_action", "历史长了 84k 字符，内核一次没动手",
         _session(15, 8_000, 6_000, 52_000, 69, 11_000, 0.94,
                  kinds=("budget_note",)),
         {"R1", "R3"}),
        ("warn_only", "只发了一次告警，然后照样什么都不做",
         _session(15, 8_000, 6_000, 52_000, 69, 11_000, 0.94,
                  kinds=("context_warn", "budget_note")),
         {"R1"}),
        ("overspend", "整场烧过 40 万 token，全程没有预算闸",
         _session(12, 8_000, 1_500, 30_000, 69, 60_000, 0.94,
                  kinds=("context_compact",)),
         {"R4"}),
        ("exhausted", "撞满步数判「未完成」，期间没有一次收尾提示",
         _session(14, 8_000, 500, 30_000, 69, 12_000, 0.94,
                  success=False, reason="达到最大步数仍未通过证据门。",
                  kinds=("context_compact",)),
         {"R5"}),
        ("cache_dead", "前缀缓存被打掉了（每步改 SYSTEM 字节就是这个形状）",
         _session(9, 8_000, 1_500, 30_000, 69, 18_000, 0.20,
                  kinds=("context_compact",)),
         {"S1"}),
        ("over_compact", "每步都在压 —— 急刹车被当成例行打扫",
         _session(9, 8_000, 1_500, 30_000, 69, 18_000, 0.94,
                  kinds=("context_compact",) * 4),
         {"S2"}),
        # ── R6 的两把负控：故意把"事件"与"记账"掰开 ──────────────
        # 这是本轮唯一能证明"跨源核对真的在核对"的东西。少了它们，R6 就是一句
        # 写在报告里的装饰 —— 而本项目对"检查在，却永不触发"（G 型）的解药只有负控。
        ("event_not_real", "发了出清事件，但 result.compacted 与消息栈都没有",
         _session(9, 8_000, 1_500, 30_000, 69, 18_000, 0.94,
                  kinds=("context_compact",), brief=False, compacted=None),
         {"R6"}),
        ("record_not_emitted", "账上记着出清，事件却一次都没发",
         _session(9, 8_000, 1_500, 30_000, 69, 18_000, 0.94,
                  kinds=(), brief=True, compacted={"levels": [1], "freed_chars": 4000}),
         {"R6"}),
        ("zero_cache_field", "cache_ratio 恒 0（旧探针病）⇒ 必须 NA 不许判红也不许判绿",
         _session(9, 8_000, 1_500, 30_000, 69, 18_000, 0.0,
                  kinds=("context_compact",)),
         set()),
    ]


def offline_selftest() -> int:
    """负对照总验收。返回进程退出码。

    ★ 这里打的是 [PASS]/[FAIL] —— 每个 FAIL 的含义是**判据失灵**，不是"归档不好"。
      两种语义不许混：真归档那份报告用「红/绿/NA」，不用行首标记（见 regress.py
      的 _RE_MARK：行首有标记才算一条断言）。
    """
    bad = 0
    print("── 负对照：每个判据都得证明它能判出「坏」 ──")
    for name, desc, ses, want_red in fixtures():
        m = metrics(ses)
        j = judge(m)
        reds = {rid for rid, _, verdict, _ in j if verdict == "红"}
        ok = (reds == want_red)
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {desc}")
        if not ok:
            print(f"          期望判红 {sorted(want_red) or '（无）'}，实得 {sorted(reds) or '（无）'}")
            bad += 1
        # 「测不到」不许混进通过：恒 0 的 cache_ratio 是坏探针，不是没命中缓存
        if name == "zero_cache_field":
            s1 = [v for rid, _, v, _ in j if rid == "S1"]
            if s1 != ["NA"]:
                print(f"  [FAIL] zero_cache_field/S1 应判 NA，实得 {s1}")
                bad += 1
            else:
                print("  [PASS] zero_cache_field/S1 判 NA 而非通过")

    # 承重自检 A：把「测不到」伪装成「通过」的通道必须堵死
    m = metrics({"id": "empty", "goal": "", "events": [], "result": {}})
    j = judge(m)
    verd = {v for _, _, v, _ in j}
    if "绿" in verd:
        print("  [FAIL] 空归档竟然有判据判绿 —— 缺数被当成通过了")
        bad += 1
    else:
        print(f"  [PASS] 空归档全判 NA（{len(j)} 条，无一判绿）")

    # 承重自检 B：floor_share 得对"少挂工具"有反应 —— 这就是推翻旧估计的那把尺
    full = metrics(_session(9, 8_000, 6_000, 52_000, 69, 18_000, 0.94))
    trim = metrics(_session(9, 8_000, 6_000, 9_000, 12, 18_000, 0.94))
    if not (full["floor_share"] and trim["floor_share"]
            and trim["floor_share"] < full["floor_share"] - 0.15):
        print(f"  [FAIL] floor_share 对少挂工具不敏感："
              f"{full['floor_share']} vs {trim['floor_share']}")
        bad += 1
    else:
        print(f"  [PASS] floor_share 随挂载数下降："
              f"{full['floor_share']:.3f}（69 个）→ {trim['floor_share']:.3f}（12 个）")

    # 承重自检 C：交付推后 3 步，compare 的步数哨兵必须塌
    base = aggregate([full])
    later = aggregate([metrics(_session(12, 8_000, 6_000, 52_000, 69, 18_000, 0.94))])
    got = [r for nm, r, _ in compare(base, later) if nm == "步数不升 >+1"]
    if got != ["红"]:
        print(f"  [FAIL] 交付推后 3 步，步数哨兵没塌：{got}")
        bad += 1
    else:
        print(f"  [PASS] 交付推后 3 步（{base['steps_median']}→{later['steps_median']}）步数哨兵塌红")

    # 承重自检 D：改了 schema 地板，compare 必须报得出降幅 —— 否则"省了多少"无从谈起
    got2 = [r for nm, r, _ in compare(base, trim_and_aggregate(full)) if nm == "峰值 prompt token ↓"]
    if got2 != ["绿"]:
        print(f"  [FAIL] 真降了 40% 以上却没判出绿：{got2}")
        bad += 1
    else:
        print("  [PASS] compare 的降幅判据认得出真改善")

    print(("  [DONE] 上下文经济探针全部通过" if bad == 0
           else f"  [DONE] 上下文经济探针：{bad} 处判据失灵"))
    return 0 if bad == 0 else 1


def trim_and_aggregate(base_m: dict) -> dict:
    """造一份"每步峰值降 50%"的聚合数，专门喂 compare 的降幅判据（承重自检 D）。"""
    a = aggregate([base_m])
    a["peak_input_median"] = (a["peak_input_median"] or 0) * 0.4
    return a


# ── 真归档报告 ────────────────────────────────────────────────
def scan_archives() -> list[dict]:
    ms = []
    for p in sorted((ROOT / "runs").glob(ARCHIVE_GLOB)):
        try:
            ses = json.loads(p.read_text(encoding="utf-8"))
            # ★ 把对话快照挂进去，R6 才有第二个来源可核。
            #   `session.json` 里只有 `result.trace`（每次调用的摘要），**没有消息栈**，
            #   所以"真出清过没有"这件事在纯 session 里根本看不出来 —— 只靠
            #   `context_compact` 事件的话，R6 就成了一个自己造自己验的单源判据。
            #   快照跑成功后会被改名成 `conversation.done.json`（挪走代替删除），
            #   两个名字都要试：只试一个的话，**每一场成功的任务都会被判 NA**。
            for nm in ("conversation.json", "conversation.done.json"):
                q = p.parent / nm
                if q.is_file():
                    try:
                        ses["_snapshot"] = json.loads(q.read_text(encoding="utf-8"))
                    except Exception:
                        pass          # 坏快照：宁可 R6 判 NA，也不许拖垮整份报告
                    break
            ms.append(metrics(ses))
        except Exception as e:                    # 坏归档不许静默跳过
            print(f"  !! {p.parent.name} 读不动：{type(e).__name__}: {e}")
    return ms


def report(ms: list[dict]) -> tuple[int, dict]:
    """打印人读报告，返回 (判红条数, 聚合数)。

    聚合数**只在这里算一次**并交回调用方 —— `--save` 曾经另算一遍，于是报告说
    "判红 32"、基线里存的是 `red_total: 0`。两份真相迟早对不上，就别留第二份。
    """
    reds = 0
    for m in ms:
        j = judge(m)
        r = [x for x in j if x[2] == "红"]
        reds += len(r)
        print(f"\n=== {m['run_id']}  {m['steps']} 步  "
              f"{'成功' if m['success'] else '未成功'}  "
              f"in={m['total_input_tokens'] or 0:,} ===")
        print(f"    目标：{m['goal']}")
        for rid, cn, verdict, why in j:
            print(f"    {rid} {cn:14s} {verdict:2s}  {why}")
        for x in m["_na"]:
            print(f"    · 测不到：{x}")
    agg = aggregate(ms)
    agg["red_total"] = reds
    print("\n──── 汇总 ────")
    print(f"    归档 {agg['runs']} 份 / 成功 {agg['success']} / 判红 {reds} 条")
    for k in ("steps_median", "total_input_median", "first_prompt_median",
              "floor_share_median", "peak_input_median", "growth_slope_median",
              "cache_median"):
        v = agg[k]
        print(f"    {k:22s} {v:,.3f}" if isinstance(v, float) else f"    {k:22s} {v}")
    print(f"    context_compact 总计 {agg['compact_total']} —— 改动前应当是 0")
    return reds, agg


def main() -> int:
    ap = argparse.ArgumentParser(description="上下文经济度量探针（只读）")
    ap.add_argument("--offline", action="store_true", help="跑合成夹具 + 负对照（回归项）")
    ap.add_argument("--save", action="store_true", help="把真归档的这份数存成 before 基线")
    ap.add_argument("--against", action="store_true", help="跟 before 基线比，报相对改善")
    a = ap.parse_args()

    if a.offline:
        return offline_selftest()

    ms = scan_archives()
    if not ms:
        print("没有任何真跑归档可测 —— 探针无事可做（这不是通过）")
        return 1
    _reds, agg = report(ms)

    if a.save:
        SNAP.parent.mkdir(parents=True, exist_ok=True)
        SNAP.write_text(json.dumps({"aggregate": agg,
                                    "runs": {m["run_id"]: m for m in ms}},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已存 before 基线：{SNAP}（判红 {agg['red_total']} 条）")
    if a.against:
        if not SNAP.is_file():
            print("\n没有 before 基线（先 --save）—— 无从比较")
            return 1
        base = json.loads(SNAP.read_text(encoding="utf-8"))["aggregate"]
        print("\n──── 对基线的相对判据 ────")
        reg = 0
        for nm, verdict, why in compare(base, agg):
            reg += verdict == "红"
            print(f"    {nm:22s} {verdict:2s}  {why}")
        return 1 if reg else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
