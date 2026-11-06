# -*- coding: utf-8 -*-
"""上下文经济 —— 把"只增不减"改成"长了就出手"。

## 为什么必须有这个模块

主循环每步把**整个 `messages` 原样重发**（`loop.py` 的 `self.llm.chat(messages, ...)`），
而 `messages` 从建立起**只 append、从不删改**。全仓此前没有任何压缩：无摘要、无截断
历史、无滚动窗口。后果有两条，都实测过：

1. **成本**：19 份真归档里，历史每步长 3,715 字符（中位）；四场 PPT 任务整场输入
   token 分别 1,142,001 / 1,012,234 / 737,357 / 355,386，全部被人中途取消。
2. **唯一的硬崩路径**：真溢出时网关回 400 → `llm.LLMError` → 主循环不接 →
   整场判「执行中断」。**此前没有任何降级动作。**

## 为什么是"纯程序化"而不是"让模型写摘要"

前沿框架（Claude Code 的五层 compaction、Codex / OpenCode 的 auto-compact、
OpenHands 的 condenser）都靠一次 LLM 调用来重写历史。荣珂刻意不走这条路，三条理由：

- **摘要器保留什么是不可复算的自述。** 本项目的定律是"能算的就不许靠匹配"
  （见 `gate._kernel_verdict`）。在证据链中间插一个不可审计的裁判，正是
  `pitfalls` 开头那句"没有一条会报错"所描述的失败形状。
- **脱水不需要判断。** 下面的 stub 全部由台账字段**现算**
  （`seq` / `ok` / `ms` / `orig_chars` / `digest()` / `extract_anchors()`），
  `stub_for()` 的签名里**根本没有消息**这件事就是这条理由的可执行形式。
- **门禁根本不看消息栈。** `gate.check()` 读的是 `registry.trace`（`by_seq` 与
  `tc.result`），而台账截 12,000 字、回灌给模型截 6,000 字 —— **模型看到的本来
  就比台账少**。所以把旧消息脱水不触碰任何一道门的判定输入。这一点有回归测试正面
  锁死（`context_test.py`：同一份台账、压缩前后 `gate.check` 的 Verdict 逐字节相同）。

## 前缀稳定性：为什么"急刹车"而不是"例行打扫"

实测归档的前缀缓存命中率中位 **94.4%**。改写历史前段会打掉后续所有步的命中 ——
那笔交易是**亏**的。因此：

- 锚定段（`system` + 契约 + 历史经验）与最近 `KEEP_LIVE` 条**永不触碰**；
- 每一处替换都**实测净收益**，不足 `MIN_GAIN_CHARS` 就放弃（不靠估）；
- 够了就停，不做"顺手清干净"。

于是 `apply(apply(x)) == apply(x)`：第二次对着一份 stub 再算一遍，收益是负的 ⇒ 原样
放回。幂等是**量出来的**，不是靠记水位线碰出来的 —— 少一份会漂移的隐藏状态。

## 代价，如实记在这里

模型想回看 20 步前那份长检索结果时，拿到的是 240 字头部 + 锚点。锚点里带 DOI / URL /
数值，所以它可以**自己重发那一次调用**（`import_document` / `web_extract` 幂等），
而这一步比语义摘要便宜。这是刻意的取舍，不是遗漏。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .tools import extract_anchors

# ── 阈值 ─────────────────────────────────────────────────────
STUB_HEAD = 240           # 脱水后保留的原文头部字符数（错误行仍用它）
STUB_HEAD_HEAD = 80       # ★ W37：正常的替身只留 80 字头部（尾部另给 80）——
                          #   因为**给得起回读指针**了，不必再靠头部承载信息。
                          #   实测依据：240 字头部 × 150 条历史 = 36,000 字符，
                          #   而它们全是"看过但再也不会用"的残影。
SPILL_TAIL = 80           # 尾部预览字符数（结论/报错常落在末尾）
SPILL_DIR = "tool_results"  # 沉淀目录（相对任务工作区 = cwd）
ASSISTANT_HEAD = 400      # L4：旧 assistant 正文保留的头部
KEEP_LIVE = 6             # 兼容保留：最近这么多条消息**绝不**脱水
# ★ `KEEP_LIVE_STEPS` 的**唯一**定义在下面那段论证之后（约 100 行处）。
#   这里曾经还有一行 `KEEP_LIVE_STEPS = 4`，被下面那行 =2 **静默覆盖** ——
#   于是"源码里写着 4、运行时是 2"，读源码的人和读行为的人看到两个数。
#   一个常量只留一处定义：要改，改那唯一的一处。
MIN_GAIN_CHARS = 600      # 实测净收益不足这么多就不动手
FOLD_MIN_GAIN_CHARS = 200  # 仅 L3，且**仅当前缀已经在本下标之前断过**（见 `act`）
BRIEF_MARK = "【上下文出清·台账摘要】"   # L5 的识别锚，靠它保证不重复追加
LEDGER_BRIEF_N = 24       # L5 摘要里最多列多少次调用
REMIND_MARK = "【目标重申"    # `_reminder` 的开头，见 loop._reminder

# ══════════════ 为什么保护窗口必须按「步」算，不能按「条」算 ══════════════
# 2026-10-15 实测（run 20261015-193936，课题 hirsutella-sinensis-breeding）：
#
#   原注释写的是「KEEP_LIVE = 6 条消息 ≈ 最后两步」—— **这个换算是错的**。
#   实测该 run：27 步 / 78 次调用 ⇒ 平均每步 **2.89** 个调用
#   ⇒ 每步消息数 = 1（assistant）+ 2.89（结果）= **3.89 条**
#   ⇒ KEEP_LIVE=6 只护住 **1.54 步**，**不到两步**。
#
#   后果是致命的，而且症状极具误导性：模型第 N 步拿到一份长结果，
#   第 N+1 步**还没轮到它思考**，这份结果就已经可以被脱水成 240 字 stub。
#   于是模型看到的永远是"结果已出清"，**它从没真正读到过数据**，
#   只好把同一个调用再发一遍 —— 表现为「原地打转、候选池计数来回反复」。
#
#   实测证据：该 run 78 条 tool 结果里 **37 条是 stub**，
#   其中包括 20,465 字符的 kb_bibtex、9,701 字符的 kb_read —— 模型一次都没看见。
#   两轮跑废（第一轮 35 步 / 第二轮 23 步）都是同一个根因，
#   与"用哪个库工具"无关 ⇒ 我先加的 kb_index 修对了截断，却没修对这件事。
#
# ★ 纪律：**一个窗口的语义单位，必须与它要保护的东西的单位一致。**
#   要保护的是"模型还来得及读的那几步"，那就按步数，不能按消息条数 ——
#   每步的消息条数由模型决定（1~6 个并行调用），是个**变量**，
#   拿它当换算系数，等于把保护窗口交给模型去压缩。
#
# ★ 为什么是 2 而不是 4：
#   要挡住的事故只有一件 —— **第 N 步拿到的结果，在第 N+1 步还读得到**。
#   保护 2 步就够（当前步 + 上一步），这正是"模型能把它读进下一轮思考"的最小值。
#   取 4 是过度保护：实测同一个 9 步样本，窗口 4 步时只能压动 5/9 条结果，
#   出清省下的字节从 15%+ 掉到 5.2%，**把急刹车变成了压不动**。
#   窗口的单位要正确，宽度要够用就好 —— 宁可按需调大，不要一上来就宽到压不动。
KEEP_LIVE_STEPS = 2


def _step_cutoff(messages: list[dict], steps: int) -> int:
    """返回一个下标：**从它到最后**的消息属于最近 `steps` 步，必须保护。

    「一步」的边界 = 一条带 `tool_calls` 的 assistant 消息。
    从末尾往前数 `steps` 个这样的边界，取最靠前那个的下标。
    数不够 `steps` 个 ⇒ 返回 0（整段都保护，也就是不动）。
    """
    if steps <= 0:
        return len(messages)
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            seen += 1
            if seen >= steps:
                return i
    return 0

LEVELS = {
    1: "旧工具结果→由台账重算的 stub",
    2: "旧失败调用/无台账回灌→一行",
    3: "折叠重复的目标重申",
    4: "旧 assistant 正文截头（tool_calls 原样保留）",
    5: "兜底：追加一份逐工具台账摘要",
}


def messages_chars(messages: Iterable[dict]) -> int:
    """消息体字符数 —— 上下文占用的代理指标。

    ★ **单源**：`loop._messages_chars` 改成调这里。两份同口径的实现迟早漂移，
      而"压到多少字符"这件事的全部意义就是两个数说的是同一种东西。

    **刻意用字符数而不是 token**：内核零依赖、没有 tokenizer，任何"按字数估 token"
    的换算都会造出一个看起来精确的假数。字符数是实测的。真需要 token 口径的地方
    （预算闸）用网关回的 `usage.input_tokens` —— 那也是实测值，不是换算值。
    """
    n = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        for tc in (m.get("tool_calls") or []):
            try:
                n += len(json.dumps(tc, ensure_ascii=False, default=str))
            except Exception:
                pass
    return n


# ── 台账索引 ────────────────────────────────────────────────
def by_call_id(trace: list) -> dict[str, Any]:
    """`tool_call_id → ToolCall`。

    只认 `tc.id` 非空的。`deliver_task` / `report_blocker` 的回灌**不经过注册表**
    （那是门在说话，不是工具在说话），因此没有台账对应物 —— 那些条目走
    `_stub_orphan`，且**不假装自己有 digest**（给一条没有出处的记录配一个看起来
    可核对的指纹，等于给假证据化妆 —— `to_dict` 对失败调用不产锚点是同一条纪律）。
    """
    out: dict[str, Any] = {}
    for tc in trace or []:
        cid = getattr(tc, "id", "") or ""
        if cid:
            out[cid] = tc
    return out


def _anchors_text(tc: Any) -> str:
    try:
        a = extract_anchors(str(tc.result or ""))
    except Exception:
        return "锚点抽取失败"
    parts = []
    for k in ("doi", "url", "numbers"):
        v = a.get(k) or []
        if v:
            parts.append(f"{k}={','.join(str(x) for x in v[:6])}")
    return " ".join(parts) or "无锚点"


def spill(tc: Any) -> str:
    """把一条工具结果**落盘**，返回可回读的相对路径；失败返回空串。

    ★ W37 照抄 Kimi `toolResultTruncationService.ts` 的结构（见
      `00_总控/W19-三家内核机制对照报告.md` §2）：内容不丢，挪到盘上，
      上下文里只留**指针 + 首尾预览**。

      此前的 stub 只写"正文 N 字符已出清"，**没有取回路径** —— 模型看得见
      东西被删了，却没有办法把它读回来。那是把"没看全"渲染成"没了"。

    落点：`Path.cwd() / SPILL_DIR / #<seq>-<name>.txt`。

      为什么是 cwd：任务期间 `loop.run()` 会 chdir 进本次 `runs/<run_id>/`，
      ⇒ 沉淀物天然落在围栏内，与 `write_file` / `audit_deck` 同一口径。
      不写绝对路径、不写用户目录 —— 那是把围栏外的盘也当自己家。

    幂等：同名覆盖、原子换名。同一个 seq 重复沉淀得到同一份内容，
      因此 `apply(apply(x)) == apply(x)` 不会被这一步破坏。

    失败**不静默**：返回空串，调用方据此退回旧的"无指针"形态并如实说明。
    沉淀是附赠品，写不了也不许打断任务 —— 但也不许假装写成了。
    """
    seq = getattr(tc, "seq", None)
    name = str(getattr(tc, "name", "tool") or "tool")
    body = str(getattr(tc, "result", "") or "")
    if seq is None or not body:
        return ""
    # 工具名进文件名前先收敛成安全字符：它来自注册表，但仍不许路径分隔符落地。
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)[:40]
    try:
        d = Path.cwd() / SPILL_DIR
        d.mkdir(parents=True, exist_ok=True)
        fn = f"#{int(seq)}-{safe}.txt"
        target = d / fn
        tmp = d / (fn + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
        return f"{SPILL_DIR}/{fn}"
    except Exception:
        return ""


def _tail(body: str, n: int = SPILL_TAIL) -> str:
    """尾部预览。首尾都要给：只有头部的话，结论/报错恰恰常落在末尾。"""
    tail = body[-n:] if len(body) > n else body
    return tail.replace("\n", " ")


def _readback_line(tc: Any, orig: int) -> str:
    """`next_step` 那一行 —— **它不是给人看的**，是告诉模型怎么把内容取回来。

    照抄 Kimi 的 `next_step: Use Read with output_path ...` 判断：
    一个不带回读法的"已出清"等于一个死胡同。
    """
    path = spill(tc)
    if not path:
        return (f"⚠️ 落盘失败，本条全文**无法回读**；如需再用，"
                f"请用同样的参数重发 `{getattr(tc, 'name', '该工具')}`。")
    return (f"next_step: 全文已落盘 {orig} 字符 ⇒ "
            f"用 `read_file(path=\"{path}\")` 读回（可分段读）。")


def stub_for(tc: Any) -> str:
    """由**台账记录**确定性重算一条工具观察的替身。

    ★ 参数里只有 `tc`，没有消息 —— 因此"同一次调用在任何时候都得到同一个替身"
      是签名层面的事实，不是巧合。回归测试据此断言可复算性。

    ★ W37：形态从"已出清 + 240 字头部"改成"已出清 + 首尾预览 + 回读指针"。
      头部压到 `STUB_HEAD_HEAD`（80 字）、尾部给 `SPILL_TAIL`（80 字）。
      不是"少给点"，是**给了能自己拿回来的办法**，所以可以给得更少。
    """
    orig = getattr(tc, "orig_chars", -1)
    body = str(tc.result or "")
    if orig < 0:
        orig = len(body)
    head = body[:STUB_HEAD_HEAD].replace("\n", " ")
    # 短结果不值得落盘也不值得给指针：它本来就快说完了，直接给全文。
    if len(body) <= STUB_HEAD_HEAD + SPILL_TAIL:
        return (f"[#{getattr(tc, 'seq', '?')} {tc.name} "
                f"{'ok' if tc.ok else 'ERR'} {getattr(tc, 'ms', 0)}ms] "
                f"正文 {orig} 字符：{head}")
    return (
        f"[#{getattr(tc, 'seq', '?')} {tc.name} "
        f"{'ok' if tc.ok else 'ERR'} {getattr(tc, 'ms', 0)}ms] "
        f"正文 {orig} 字符已出清（digest={tc.digest()}，"
        f"指纹={str(getattr(tc, 'fingerprint', ''))[:8]}，{_anchors_text(tc)}）。\n"
        f"前 {STUB_HEAD_HEAD} 字：{head}\n"
        f"尾 {SPILL_TAIL} 字：{_tail(body)}\n"
        + _readback_line(tc, orig)
    )


def _err_line(tc: Any) -> str:
    err = str(getattr(tc, "error", "") or "")[:STUB_HEAD].replace("\n", " ")
    body = str(tc.result or "")
    orig = getattr(tc, "orig_chars", -1)
    if orig < 0:
        orig = len(body)
    if len(body) <= STUB_HEAD_HEAD + SPILL_TAIL:
        return (f"[#{tc.seq} {tc.name} ERR {getattr(tc, 'ms', 0)}ms] {err}"
                f"（正文 {orig} 字符；digest={tc.digest()}）")
    return (f"[#{tc.seq} {tc.name} ERR {getattr(tc, 'ms', 0)}ms] {err}"
            f"（正文 {orig} 字符已出清；digest={tc.digest()}）\n"
            f"尾 {SPILL_TAIL} 字：{_tail(body)}\n"
            + _readback_line(tc, orig))


def _stub_orphan(msg: dict) -> str:
    body = str(msg.get("content") or "")
    return (f"[门的一段回灌，**无台账对应记录**] 正文 {len(body)} 字符已出清，"
            f"无 digest 可给。前 {STUB_HEAD} 字："
            f"{body[:STUB_HEAD].replace(chr(10), ' ')}")


def ledger_brief(trace: list, n: int = LEDGER_BRIEF_N) -> str:
    """L5 兜底：从台账现算一份"到目前为止发生过什么"的事实清单。

    它**不是摘要** —— 摘要要判断"哪些重要"，这份只有"发生过哪些调用、各自成没成、
    结果开头是什么"。全部走 `ToolCall.brief()` 现算，因此可复算。
    """
    calls = list(trace or [])
    ok = sum(1 for tc in calls if tc.ok)
    head = calls[:n]
    lines = [f"{BRIEF_MARK} 更早的工具结果已出清（正文不在眼前，台账里都在）。"
             f"成功 {ok}/{len(calls)} 次调用。共 {len(calls)} 次，列前 {len(head)} 次："]
    for tc in head:
        lines.append("  " + tc.brief(140))
    if len(calls) > n:
        lines.append(f"  …另有 {len(calls) - n} 次，全部在台账里，可用 seq 引证。")
    return "\n".join(lines)


# ── 锚定段与可动区 ───────────────────────────────────────────
def anchored_head(messages: list[dict]) -> int:
    """锚定段的长度：**开头到第一个 assistant 之前**的所有消息。

    实际形状 = `initial_messages()` 的 `system` + `user(契约+工作区+课题)`，
    再可能加一条 `user(历史经验)`。判据用"第一个 assistant 之前"而**不写死下标**：
    注入历史经验是可选的（`loop.py` 里 `if hist:`），写死 2 或 3 都会在其中一种
    形状下把契约本身划进可动区。续跑的会话自动落进同一规则 —— 快照原样接回，
    前缀形状不变。
    """
    i = 0
    while i < len(messages) and messages[i].get("role") != "assistant":
        i += 1
    return i if i else min(2, len(messages))


# ── 计划 ────────────────────────────────────────────────────
def plan(messages: list[dict], trace_by_id: dict,
         keep_live: int = KEEP_LIVE,
         keep_live_steps: int = KEEP_LIVE_STEPS) -> dict[int, list[int]]:
    """纯函数：算出每一级**候选**的下标。不改数据、不判收益。

    收益判定留在 `apply` 里**实测**，不在这里估 —— 见 `_replacement` 的注释。
    所以本函数报的是"哪些地方*值得看一眼*"，不是"哪些地方*一定会动*"。
    """
    lo = anchored_head(messages)
    # ★ 合成件不占压缩预算。L5 那条台账摘要是内核自己 append 进去的，若把它算进
    #   "最近 keep_live 条"的计数里，`len - keep_live` 就每 append 一次往后挪一格 ——
    #   于是下一次 apply 会**多啃掉一条**，`apply(apply(x)) == apply(x)` 当场不成立。
    #   （这条不是推演出来的：冒烟测试里第二次 apply 又省了 5,637 字符，就是这么来的。）
    brief_adj = 1 if any(m.get("role") == "user" and
                         str(m.get("content") or "").startswith(BRIEF_MARK)
                         for m in messages) else 0
    # ★ 保护窗口按「步」算（见 KEEP_LIVE_STEPS 上方那段）。
    #   两个口径都要算，取**保护得更多**的那一个 —— 也就是 `hi` **更小**的那个。
    #
    # ★ 这里我错过一次，把方向写反了：`hi` 是**可动区的上界**，`hi` 越大＝
    #   可动的越多＝保护得越少。第一版写 `hi = max(by_count, by_steps)`，
    #   于是在密集形状下 `by_count`（38）一把压过 `by_steps`（16），
    #   步数口径**完全失效** —— 实测最近 4 步的 28 条结果里只护住 6 条，
    #   与原缺陷一模一样。方向应当是 `min`。
    #   承重自检记在 context_test.py 第 12 节（反证那条就是打这个的）。
    #
    # ★ 例外：`keep_live` 大到条数口径已覆盖整个消息栈时 = **调用方要关掉压缩**
    #   （测试与"只想脱水到某个级别"的场景这么用）。这时不能再让步数口径
    #   把它拽回来 —— 否则"关"这个动作就关不掉了。判据是"条数口径本身
    #   已经护住了全部"（`by_count <= 0`），而不是"keep_live 是个大数"：
    #   后者是猜调用方的意图，前者是可核对的事实。
    by_count = len(messages) - max(0, keep_live) - brief_adj
    if by_count <= 0:
        hi = 0                                                  # 条数口径已护住全部 ⇒ 不动
    else:
        by_steps = _step_cutoff(messages, keep_live_steps)
        hi = min(by_count, by_steps) if by_steps > 0 else by_count
        hi = max(0, min(hi, len(messages)))
    p: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
    if hi <= lo:
        return p
    for i in range(lo, hi):
        m = messages[i]
        role = m.get("role")
        body = m.get("content")
        if not isinstance(body, str):
            continue
        if role == "tool":
            p[1 if trace_by_id.get(m.get("tool_call_id") or "") is not None else 2].append(i)
        elif role == "user" and body.startswith(REMIND_MARK):
            p[3].append(i)
        elif role == "assistant":
            # ★ 只动 content。`tool_calls` 必须逐字节原样留着 —— 少了它，下一轮
            #   API 直接报错（`loop` 里"必须原样保留"那条纪律）。
            if len(body) > ASSISTANT_HEAD:
                p[4].append(i)
    # 重申折叠：判据要看**全局**最后一次重申在哪，而不是"可动区里有几条"。
    # 写错成后者的话（第一版就是这么错的）：旧重申落在可动区、新那条落在
    # keep_live 保护区 —— 这恰恰是最常见的形状 —— 结果一条都不折，白看着它每步重发。
    _all_remind = [i for i, m in enumerate(messages)
                   if m.get("role") == "user"
                   and str(m.get("content") or "").startswith(REMIND_MARK)]
    if _all_remind:
        _last = _all_remind[-1]
        p[3] = [i for i in p[3] if i < _last]
    else:
        p[3] = []
    return p


def _replacement(m: dict, lvl: int, tc: Any) -> str | None:
    if lvl == 1:
        return stub_for(tc) if tc.ok else _err_line(tc)
    if lvl == 2:
        return _stub_orphan(m)
    if lvl == 3:
        return (f"{REMIND_MARK}·已折叠】更早的一次重申与最近一次同文，"
                f"最近的那份仍在下面。")
    if lvl == 4:
        return str(m.get("content") or "")[:ASSISTANT_HEAD] + "…（旧正文已出清）"
    return None


# ── 执行 ────────────────────────────────────────────────────
def apply(messages: list[dict], trace: list, target_chars: int,
          keep_live: int = KEEP_LIVE,
          keep_live_steps: int = KEEP_LIVE_STEPS,
          only_levels: tuple[int, ...] = (1, 2, 3, 4, 5)) -> tuple[list[dict], dict]:
    """把 `messages` 压到 `target_chars` 以下；**够了就停**。

    返回 `(新消息表, 统计)`。统计里的每一项都是**算出来的**：
    `freed_chars = 压缩前实测字符 - 压缩后实测字符`。

    不改入参：被改的那些条走 `dict(m)` 浅拷；`tool_calls` 那个 list 按引用带走
    ⇒ 序列化结果逐字节不变 ⇒ 前缀稳定。
    """
    trace_by_id = by_call_id(trace)
    before = messages_chars(messages)
    out = [dict(m) for m in messages]
    p = plan(out, trace_by_id, keep_live, keep_live_steps)
    done: list[int] = []
    touched_idx: set[int] = set()       # 已被改写的下标 —— L3 的收益门要看它
    counts = {"stub": 0, "err_line": 0, "orphan": 0, "fold": 0, "trim": 0}
    stat = {"before_chars": before, "after_chars": before, "freed_chars": 0,
            "levels": [], "counts": counts, "brief": False,
            "target_chars": int(target_chars), "kept_live": max(0, keep_live),
            "kept": 0}

    def act(lvl: int) -> None:
        """对这一级的候选逐个试替换；**实测**没省够就放回原样。

        收益门为什么对 L3 放宽一档（`FOLD_MIN_GAIN_CHARS`）：折叠一条重申只省
        四五百字，按统一门槛永远不过 —— 但**前提**是 L1/L2 已经在更早的下标改写过，
        那时前缀缓存的账已经付过一次，本步不再额外付。反过来，若还没有任何东西
        动过（`touched_idx` 为空或都排在后面），为几百字断一次前缀是**亏**的，
        于是照旧按 `MIN_GAIN_CHARS` 拦下来。
        """
        touched = False
        for i in p.get(lvl) or []:
            m = out[i]
            old = m.get("content")
            if not isinstance(old, str):
                continue
            tc = trace_by_id.get(m.get("tool_call_id") or "") if lvl in (1, 2) else None
            if lvl == 1 and tc is None:
                continue
            new = _replacement(m, lvl, tc)
            if new is None:
                continue
            floor = MIN_GAIN_CHARS
            if lvl == 3 and touched_idx and min(touched_idx) < i:
                floor = FOLD_MIN_GAIN_CHARS
            if len(old) - len(new) < floor:
                continue
            m["content"] = new
            touched = True
            touched_idx.add(i)
            if lvl == 1:
                counts["stub" if tc.ok else "err_line"] += 1
            elif lvl == 2:
                counts["orphan"] += 1
            elif lvl == 3:
                counts["fold"] += 1
            else:
                counts["trim"] += 1
        if touched:
            done.append(lvl)
            now = messages_chars(out)
            stat["after_chars"] = now
            stat["freed_chars"] = max(0, before - now)

    for lvl in (1, 2, 3, 4):
        if lvl in only_levels:
            act(lvl)
            if stat["after_chars"] <= target_chars:
                break
    if 5 in only_levels and stat["after_chars"] > target_chars and done:
        out = _put_brief(out, trace)
        stat["brief"] = True
        done.append(5)
        now = messages_chars(out)
        stat["after_chars"] = now
        stat["freed_chars"] = max(0, before - now)

    stat["levels"] = sorted(set(done))
    stat["touched"] = sum(counts.values())
    return out, stat


def _put_brief(messages: list[dict], trace: list) -> list[dict]:
    """放（或就地更新）那条台账摘要。

    ★ 就地更新而不是再追加一条：追加会让 `apply(apply(x)) != apply(x)`，
      于是每次压缩都把历史推长一截 —— 正是这个模块要治的病。
    """
    txt = ledger_brief(trace)
    for i, m in enumerate(messages):
        if m.get("role") == "user" and str(m.get("content") or "").startswith(BRIEF_MARK):
            if m["content"] == txt:
                return messages
            out = list(messages)
            out[i] = dict(m, content=txt)
            return out
    return list(messages) + [{"role": "user", "content": txt}]


def force_brief(messages: list[dict], trace: list,
                keep_live_steps: int = 0) -> tuple[list[dict], dict]:
    """溢出兜底：走满整级阶梯。`llm.ContextOverflow` 那条路专用。

    ★ 为什么这里把 `keep_live_steps` 默认成 **0**（而不是沿用 4）：
      保护窗口与"救命"是**两个相反的目标**。
      · 常规出清：目标是"别打断模型的思路"，所以最近几步的结果要留着；
      · 溢出兜底：目标是"这一发必须发得出去"，否则整轮任务当场作废，
        留着那几步结果也读不成了。
      2026-10-15 实测：`KEEP_LIVE_STEPS=4` 生效后，溢出兜底在带密集并行的
      消息栈上**一条都压不动**（`eff=0 skipped=2`），
      重试那次收到的还是 25,572 字符 —— 等于兜底失效。
      所以这里显式放开步数保护，只留 `keep_live` 的条数下限。

    `keep_live_steps=0` ⇒ `plan` 里 `_step_cutoff` 返回 `len(messages)`；
    为 0 的分支会走 `by_count`，与改动前的行为一致。
    """
    return apply(messages, trace, target_chars=0, keep_live_steps=keep_live_steps,
                 only_levels=(1, 2, 3, 4, 5))
