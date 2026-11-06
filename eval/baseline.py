# -*- coding: utf-8 -*-
"""评测基线 —— 从归档算成功率，**并在样本不够时拒绝出数**。

为什么要有这个脚本，而不是每次临时统计：
  2026-09-19 外部评审第一条实质性指摘是「从来没有成功率分母」。
  分母不存在不是因为没人算，是因为**没有可信的计算口径**。
  口径写在人脑子里 = 没有口径（本项目 B 型缺陷：两处真相、注定漂移）。
  所以这里把口径写死成代码，**包括"什么时候不许出数"**。

三条口径（刻意写死，不许调用方放宽）：

  1. **分母 = 不同任务数，不是运行次数。**
     同一个目标跑 7 遍的成功率不是"任务成功率"，是"该任务的重试成功率"。
     把两者合在一起报，是拿一个数字掩盖两个含义 —— 那就是假数。
  2. **每个任务至少重复 3 次。** 单次成功/失败分不清是能力还是运气。
  3. **可报门槛 = ≥30 个任务 且 每任务 ≥3 次。**
     任一不满足 ⇒ 打印 `NOT REPORTABLE` 并**带退出码 2**。
     退出码 2 是刻意的：CI 与脚本不能把"没测"当成"通过"
     （本项目 M 型缺陷的同类 —— 别把"没检查"渲染成"合格"）。

退出码：0 = 可报；2 = NOT REPORTABLE；1 = 读不到归档。

归档读取口径（**只能读这些，不许去猜**）：
  `runs/<id>/session.json` 的 `goal` 与 `result.{success,steps,usage,lint,verdict}`。
  ⚠️ 归档里 **没有** 工具结果原文 —— `events[kind=tool]` 只有 `digest`（结果摘要哈希）。
  所以这个脚本**只算任务级成败**，绝不复算证据门第 4 条（验收覆盖）——
  它需要 `tc.result` 文本，而那个东西根本没落盘。见 eval/README.md「已知边界」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as TK                                                     # noqa: E402

# ★ 产品层**唯一**的「累计用量」实现。本文件里 `collect()` 是**逐场**视图（带
#   rubric/lint/contract），而"一共花了多少 token"只有那一个地方算 —— 不许在这边
#   再写一遍（同一件事两处真相 = B 型缺陷）。`self_test()` 里有一条断言盯着两者不
#   许分家。
from rongke.memory import archived_usage                               # noqa: E402

DEFAULT_RUNS = ROOT / "runs"
DEFAULT_TASKS = HERE / "tasks.jsonl"

# ── 口径常量：改这里等于改口径，必须同时改 README 与报告标题 ──
MIN_TASKS = 30          # 至少要多少个**不同任务**
MIN_REPEATS = 3         # 每个任务至少重复几次
TAIL_STEPS = 8          # 未收敛尾部阈值（沿用 2026-09-18 实测基线）

# 2026-09-19：`llm._usage_from` 修掉了缓存字段名（原先只认 Anthropic 的口径，
# 而本机网关发的是 prompt_tokens_details.cached_tokens ⇒ `cache_read` 恒为 0，
# 实测真实命中 95.8%）。**在这个日期之前落盘的归档里，那 0 是被坏探针写下的"当时值"。**
# 归档里的当时值是**证据**，不可重算 ⇒ 任何包含旧归档的报告，成本列一律不可用。
# 这也是 P 型纪律的机器化："每个字段都要能回答 —— 你代表现在，还是代表当时？"
CACHE_READ_FIXED_AT = "20260919"


def _norm_goal(s: str) -> str:
    """目标指纹：去空白/标点、折叠大小写。同一句话的不同排版算同一个任务。"""
    s = (s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。、；：！？,.;:!?\"'`（）()\[\]【】\-—]", "", s)
    return s


def goal_id(goal: str) -> str:
    return hashlib.sha1(_norm_goal(goal).encode("utf-8")).hexdigest()[:8]


def _rubric_key(items) -> tuple[str, ...]:
    """一套验收标准的指纹：逐条规范化后**排序**（顺序不算差异）。"""
    return tuple(sorted(_norm_goal(str(x)) for x in (items or []) if str(x).strip()))


# ══════════════════════ 采集 ══════════════════════
@dataclass
class RunRow:
    run_id: str
    goal: str
    ok: bool
    steps: int
    elapsed: float
    in_tok: int
    out_tok: int
    reasoning_tok: int
    cache_read: int
    cache_ratio: float | None
    score: int | None
    reason: str
    cost: str
    path: Path
    # 这套运行**当时用的验收标准**（规范化排序后的指纹）。
    # 空元组 = 归档里没有契约（早期归档）⇒ 判不了评分标准，别假装判了。
    rubric: tuple[str, ...] = ()


def collect(runs_dir: Path, since: str = "") -> tuple[list[RunRow], list[str]]:
    """读归档。返回 (rows, 读不动的目录说明)。坏归档**如实报告，不静默跳过**。"""
    rows: list[RunRow] = []
    bad: list[str] = []
    if not runs_dir.exists():
        return rows, [f"归档根不存在：{runs_dir}"]
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        if since and d.name[:8] < since:
            continue
        p = d / "session.json"
        if not p.exists():
            bad.append(f"{d.name}（没有 session.json —— 这个目录不是可回放的运行归档）")
            continue
        try:
            j = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            bad.append(f"{d.name}（session.json 解析失败：{type(e).__name__}）")
            continue
        r = j.get("result") or {}
        u = r.get("usage") or {}
        lint = r.get("lint") or {}
        # ⚠️ 容器口径：验收标准在 `result.contract.acceptance`（不是顶层、不是 events）。
        #    取不到就给空元组 —— 早期归档没有契约字段，那是"判不了"，不是"没有标准"。
        _ct = r.get("contract") or {}
        rows.append(RunRow(
            run_id=str(j.get("id") or d.name),
            goal=str(j.get("goal") or ""),
            ok=bool(r.get("success")),
            steps=int(r.get("steps") or 0),
            elapsed=float(j.get("elapsed") or 0.0),
            # ⚠️ 字段名口径：内核用 input_tokens/output_tokens，
            #    OpenAI 裸接口是 prompt_tokens/completion_tokens。
            #    只认内核那份（llm.Usage 已做过归一，落到归档的是归一后的名字）。
            in_tok=int(u.get("input_tokens") or 0),
            out_tok=int(u.get("output_tokens") or 0),
            reasoning_tok=int(u.get("reasoning_tokens") or 0),
            cache_read=int(u.get("cache_read") or 0),
            cache_ratio=r.get("cache_ratio"),
            score=(lint.get("score") if isinstance(lint.get("score"), int) else None),
            reason=str(r.get("reason") or "")[:60],
            cost=str(r.get("cost_display") or ""),
            path=p,
            rubric=_rubric_key(_ct.get("acceptance")),
        ))
    return rows, bad


# ══════════════════════ 统计 ══════════════════════
@dataclass
class TaskStat:
    gid: str
    goal: str
    runs: list[RunRow] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.runs)

    @property
    def ok(self) -> int:
        return sum(1 for x in self.runs if x.ok)

    @property
    def rate(self) -> float:
        return self.ok / self.n if self.n else 0.0


@dataclass
class Stats:
    rows: list[RunRow]
    tasks: list[TaskStat]
    bad: list[str]
    # 评分标准漂移：同一任务重复跑，用的却**不是同一套验收标准**的组数 + 逐组说明
    rubric_unstable: int = 0
    rubric_comparable: int = 0     # 有几个任务组"比得了"（重复>=2 且能取到契约）
    rubric_detail: list[str] = field(default_factory=list)

    @property
    def n_runs(self) -> int:
        return len(self.rows)

    @property
    def n_tasks(self) -> int:
        return len(self.tasks)

    @property
    def min_repeats(self) -> int:
        return min((t.n for t in self.tasks), default=0)


def rubric_drift(tasks: list[TaskStat]) -> tuple[int, int, list[str]]:
    """同一任务重复跑，**验收标准是不是同一把尺子**？

    返回 `(漂移组数, 可比组数, 逐组说明)`。

    为什么这条必须报（2026-09-19 实测）：契约里的 `acceptance` 是**起草模型自己写的**
    （`contract.DRAFT_SYSTEM`），而它同一道题每次写得都不一样 ——
    实测 5 场同一目标出了 **4 套不同标准**、另外 3 场出了 **3 套**；
    其中一套还**少了一条**（目标明说「论文引言」，那套标准里根本没有"体裁"这一项）。

    ⇒ 按 `goal_id` 分组算出来的"任务成功率"，**每次用的尺子并不是同一把**。
    这不是拒绝出数的理由（措辞天然会飘，硬卡字符串必误伤），但**必须写在报告里** ——
    否则那个数字会被当成"同一把尺子下的成功率"来读，而它是多把尺子混出来的。

    判据：规范化并排序后，**整套标准**去重计数 > 1 即算漂移（逐条顺序不算差异）。
    归档里没有契约（空指纹）时**跳过该组** —— 那是"判不了"，不是"没有标准"。
    因此**同时返回"可比组数"**：0 个可比组时的"0 组漂移"是"没比过"，不是"比过了、很稳"。
    """
    unstable = 0
    comparable = 0
    detail: list[str] = []
    for t in tasks:
        if t.n < 2:
            continue                      # 单次运行无从比较
        keys = {x.rubric for x in t.runs}
        keys.discard(())                  # 丢掉"取不到契约"的那些，不参与比较
        if not keys:
            continue                      # 全套都取不到契约 ⇒ 判不了，不进可比组
        comparable += 1
        if len(keys) > 1:
            unstable += 1
            detail.append(
                f"  - 「{t.goal[:34]}…」{t.n} 场里用了 **{len(keys)} 套**不同标准"
                f"（{t.gid}）")
    return unstable, comparable, detail


def summarize(rows: list[RunRow], bad: list[str]) -> Stats:
    by: dict[str, TaskStat] = {}
    for r in rows:
        gid = goal_id(r.goal)
        st = by.setdefault(gid, TaskStat(gid=gid, goal=r.goal))
        st.runs.append(r)
    # 稳定排序：任务内按时间（run_id 字典序即时间序）
    for st in by.values():
        st.runs.sort(key=lambda x: x.run_id)
    tasks = sorted(by.values(), key=lambda s: s.gid)
    n_unstable, n_cmp, detail = rubric_drift(tasks)
    return Stats(rows=rows, tasks=tasks, bad=bad,
                 rubric_unstable=n_unstable, rubric_comparable=n_cmp,
                 rubric_detail=detail)


def reportable(s: Stats, *, force: bool = False) -> tuple[bool, list[str]]:
    """可报门槛。`force` 只给承重自检用（证明这道门在承重）。"""
    reasons: list[str] = []
    if not force:
        if s.n_tasks < MIN_TASKS:
            reasons.append(f"不同任务数 {s.n_tasks} < {MIN_TASKS}")
        thin = [t for t in s.tasks if t.n < MIN_REPEATS]
        if thin:
            reasons.append(
                f"{len(thin)}/{s.n_tasks} 个任务重复次数 < {MIN_REPEATS}"
                f"（最少的只有 {s.min_repeats} 次）："
                + ", ".join(f"{t.gid}({t.n})" for t in thin[:6])
            )
    return (not reasons), reasons


# ══════════════════════ 渲染 ══════════════════════
def _span(vals: list[int], k: int = 1, sep: str = "-") -> str:
    """把一列数压成「中位(范围)」—— 列变宽时也不串行。

    为什么不用「原样全部列出」：一次任务跑 40 遍，那一行会有 40 个数字，
    列宽必然溢出、后面的目标文字被挤走，**读表的人只能靠猜哪一列是哪一列**。
    压缩成范围是唯一随样本量稳定的写法。
    """
    if not vals:
        return "-"
    vs = sorted(vals)
    mid = vs[len(vs) // 2]
    lo, hi = vs[0], vs[-1]
    def fmt(x: int) -> str:
        return f"{x / k:.0f}{'k' if k > 1 else ''}" if k > 1 else str(x)
    return f"{fmt(lo)}{sep}{fmt(hi)}(中{fmt(mid)})" if lo != hi else fmt(lo)


def cost_stats(rows: list[RunRow]) -> tuple[int, int, int]:
    """(能给出单价的, 明写 UNKNOWN 的, 更早归档里根本没这字段的)。

    **这是唯一一处成本口径定义。** 渲染与自检都调它，谁都不许再数一遍
    （本项目纪律：判据的第二份拷贝 = 一个注定漂移的独立真相）。
    """
    priced = sum(1 for x in rows
                 if x.cost.strip() and x.cost.strip().upper() != "UNKNOWN")
    explicit = sum(1 for x in rows if x.cost.strip().upper() == "UNKNOWN")
    return priced, explicit, len(rows) - priced - explicit


def render(s: Stats, tasks_file: Path | None = None) -> str:
    L: list[str] = []
    ok, why = reportable(s)
    L.append("═══ 荣珂评测基线 ═══")
    L.append(f"口径：可报门槛 = 任务数 ≥ {MIN_TASKS} 且 每任务重复 ≥ {MIN_REPEATS}；"
             f"分母 = **不同任务数**（不是运行次数）")
    L.append("")
    L.append(f"归档运行数  {s.n_runs}")
    L.append(f"不同任务数  {s.n_tasks}")
    L.append(f"每任务重复  min={s.min_repeats} "
             f"max={max((t.n for t in s.tasks), default=0)}")
    if s.bad:
        L.append(f"读不动的目录 {len(s.bad)} 个（如实列出，不静默跳过）")
        for b in s.bad:
            L.append(f"    · {b}")
    L.append("")
    L.append(f"{'任务':<10}{'次数':<6}{'成功':<6}{'步数':<14}{'输入tok':<20}{'cache':<7}目标")
    for t in s.tasks:
        L.append(f"{t.gid:<10}{t.n:<6}{t.ok:<6}"
                 f"{_span([x.steps for x in t.runs]):<14}"
                 f"{_span([x.in_tok for x in t.runs], k=1000):<20}"
                 f"{_span([x.cache_read for x in t.runs], sep='/'):<7}"
                 f"{t.goal[:36].replace(chr(10), ' ')}")
    L.append("")

    if ok:
        rate = sum(t.ok for t in s.tasks) / s.n_tasks
        L.append(f"任务级成功率  {rate:.1%}（{sum(t.ok for t in s.tasks)}/{s.n_tasks} 任务全过）")
        n_ok_runs = sum(1 for x in s.rows if x.ok)
        L.append(f"重试口径成功率 {n_ok_runs}/{s.n_runs} = {n_ok_runs / s.n_runs:.1%}"
                 f"（**仅供参考**，它被重复次数加权，不等于任务能力）")
    else:
        L.append("★ NOT REPORTABLE —— 样本不足以报成功率。缺什么：")
        for w in why:
            L.append(f"    · {w}")
        L.append("")
        L.append("  为什么宁可不报：一个只覆盖 2 个任务、其中一个只跑过 2 次的成功率，")
        L.append("  挂在任何汇报里都会被读成「能力指标」。**宁可不出数，不出假数。**")
        L.append(f"  现在能报的只有：运行数 {s.n_runs} / 任务数 {s.n_tasks}。")

    # ── 评分标准漂移：上面那个成功率是**几把尺子**混出来的？──
    #    放在成功率正下方是刻意的：它是一个**限定语**，不是附注。
    L.append("")
    if s.rubric_unstable:
        L.append(f"⚠ 评分标准漂移：**{s.rubric_unstable} 个任务组**的重复运行用了不同的验收标准"
                 f"（可比组 {s.rubric_comparable} 个）")
        for d in s.rubric_detail:
            L.append(d)
        L.append("  为什么重要：`acceptance` 是**起草模型自己写的**（同一个目标重跑，起草结果每次不同）。")
        L.append("  ⇒ 上面的成功率**不是同一把尺子量出来的**，读的时候必须带上这一点。")
        L.append("  修法：把评分标准**钉进任务定义**（`eval/tasks.jsonl` 的 `acceptance`），")
        L.append("        让同一任务的每次重复都用同一套标准来判。")
    else:
        L.append(f"评分标准漂移  0 组（可比组 {s.rubric_comparable} 个）"
                 + ("" if s.rubric_comparable else " ← **0 个可比组 = 没比过**，不是'比过了、很稳'"))

    # ── 尾部与健康度（这些与样本量无关，随时可报）──
    L.append("")
    L.append("── 与样本量无关、随时可报的观测 ──")
    tail = [x for x in s.rows if x.steps > TAIL_STEPS]
    L.append(f"未收敛尾部（步数 > {TAIL_STEPS}）  {len(tail)}/{s.n_runs} 场"
             + (f"  步数 {[x.steps for x in tail]}" if tail else ""))
    if s.rows:
        zero_cache = [x for x in s.rows if not x.cache_read and x.in_tok > 0]
        L.append(f"cache_read = 0 的运行            {len(zero_cache)}/{s.n_runs} 场")
        # ⚠️ 这条读数**曾经**被写成一个假结论。第一版这里印的是
        #    「⇒ 前缀缓存完全没生效」，而真相是解析器只认 Anthropic 的字段名、
        #    真实命中 95.8%（2026-09-19 实测，见 cache_probe.py）。
        #    **读数恒为 0 ≠ 能力不存在** —— 报告不许印出测量撑不住的结论。
        #    所以这里按"样本里有没有带毒归档"分岔说话：
        unverifiable = [x for x in zero_cache if x.run_id[:8] < CACHE_READ_FIXED_AT]
        if unverifiable:
            L.append("                                  ⚠ 这 **0 是不可信的**：这些归档落盘于口径修复之前，"
                     "读数由坏探针写下。")
            L.append("                                    **读数恒为 0 ≠ 缓存没生效。** "
                     "先跑 `python cache_probe.py` 看原始响应体里到底有没有那个字段。")
        else:
            L.append("                                  ⇒ 样本全部落盘于口径修复之后，"
                     "此处的 0 可以读作「这些步确实没有命中」")
        # ⚠️ 这里曾经写错过（2026-09-19，第一版）：把「6/9 场是 UNKNOWN」反推成
        #    「表已填」。**方向反了，而且数字也漏算** —— 更早的 3 场归档里
        #    `cost_display` 字段根本不存在（空串），被排除在计数之外，
        #    于是"9 场里只有 6 场是 UNKNOWN"读起来像"有 3 场给出了数字"。
        #    真相是 **9/9 场都没有可引用的单价**。这正是本项目 F 型缺陷
        #    （把"没有数据"渲染成"有数据"）—— 出数之前先问「这个数是怎么数出来的」。
        priced, explicit_unknown, missing = cost_stats(s.rows)
        L.append(f"成本可读的运行                    {priced}/{s.n_runs} 场"
                 f"（其中 {explicit_unknown} 场明写 UNKNOWN，{missing} 场更早的归档没有该字段）")
        if not priced:
            L.append("                                  ⇒ **单位经济模型仍不成立**："
                     "没有任何一场能给出单价。既不能宣称单价表齐全，也不能说成本是 0"
                     "—— 后者会让人以为免费（pricing.py 的刻意设计）。")
        L.append(f"归档保留 usage 的运行            "
                 f"{sum(1 for x in s.rows if x.in_tok > 0)}/{s.n_runs} 场"
                 f"（字段名 input_tokens/output_tokens —— 不是 prompt_tokens）")
    if tasks_file is not None:
        L += _coverage(s, tasks_file)
    # ── 带毒归档警告：这一项必须显眼，否则有人会拿旧归档算成本 ──
    poisoned = [x for x in s.rows if x.run_id[:8] < CACHE_READ_FIXED_AT]
    if poisoned:
        L += ["",
              f"⚠ 含 {len(poisoned)}/{s.n_runs} 场 {CACHE_READ_FIXED_AT} 之前的归档：",
              "  它们的 `usage.cache_read` 是被**坏探针**（只认 Anthropic 字段名）写下的 0，",
              "  而实测真实命中率 95.8%。**当时值不可重算** ⇒ 这些场的成本列一律不可用；",
              "  token 体量仍可用（input_tokens 那条路径一直是好的）。"]
    return "\n".join(L)


def load_tasks(tasks_file: Path) -> tuple[list[dict], list[str]]:
    """读任务集。**坏行报错不跳过** —— 一个静默坏行就是一个静默缩水的分母。"""
    out: list[dict] = []
    errs: list[str] = []
    if not tasks_file.exists():
        return out, [f"任务集不存在：{tasks_file}"]
    for i, line in enumerate(tasks_file.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            d = json.loads(line)
        except Exception as e:
            errs.append(f"第 {i} 行 JSON 坏了：{e}")
            continue
        for k in ("id", "bucket", "goal", "checks"):
            if k not in d:
                errs.append(f"第 {i} 行缺字段 {k}（id={d.get('id', '?')}）")
        if "checks" in d and not isinstance(d["checks"], list):
            errs.append(f"第 {i} 行 checks 不是数组")
        out.append(d)
    ids = [d.get("id") for d in out]
    dup = {x for x in ids if ids.count(x) > 1}
    if dup:
        errs.append(f"重复的任务 id：{sorted(dup)}")
    return out, errs


def _coverage(s: Stats, tasks_file: Path) -> list[str]:
    tasks, errs = load_tasks(tasks_file)
    L = ["", "── 任务集覆盖 ──"]
    for e in errs:
        L.append(f"  ✗ {e}")
    seen = {goal_id(t["goal"]) for t in tasks}
    ran = {t.gid for t in s.tasks}
    buckets: dict[str, int] = {}
    for t in tasks:
        buckets[t["bucket"]] = buckets.get(t["bucket"], 0) + 1
    L.append(f"  任务集共 {len(tasks)} 个：" +
             "、".join(f"{k} {v}" for k, v in sorted(buckets.items())))
    L.append(f"  归档里有运行过的：{len(seen & ran)}/{len(tasks)}"
             f"  ⇒ 未跑过的 {len(tasks) - len(seen & ran)} 个任务**没有数据**")
    stray = ran - seen
    if stray:
        L.append(f"  ⚠ 归档里有 {len(stray)} 个目标不在任务集里（探索性运行，不参与分母）："
                 + ", ".join(sorted(stray)))
    return L


# ══════════════════════ 自检（正控 / 负控 / 承重自检）══════════════════════
def _synth(root: Path, spec: list[tuple]) -> Path:
    """造一个假归档树：`spec = [(目标, 跑几次, 成功几次[, 评分标准轮换])]`。

    第 4 项可选：一个「验收标准列表」的列表，按运行次序**轮换** —— 用来造评分标准漂移。
    不传就写空 `acceptance`（模拟早期归档：判不了，不是"没有标准"）。
    """
    runs = root / "runs"
    if runs.exists():
        # ★ 挪走而不是 `rmtree`：本环境按**条目数**收删除费（见 `_tkit`）。
        #   `_synth` 造的是**假**归档树、一次自检要重建 8 次 ⇒ 8 棵被删。
        #   挪走的代价是一次 `os.replace`（零计费），落点自动避让序号。
        TK.stash(runs, "eval-synth-runs")
    runs.mkdir(parents=True)
    n = 0
    for item in spec:
        goal, times, oks = item[0], item[1], item[2]
        rotate = item[3] if len(item) > 3 else None
        for i in range(times):
            n += 1
            d = runs / f"20990101-{n:06d}"
            d.mkdir(parents=True, exist_ok=True)
            acc = rotate[i % len(rotate)] if rotate else []
            (d / "session.json").write_text(json.dumps({
                "id": d.name, "goal": goal, "elapsed": 10.0,
                "result": {"success": i < oks, "steps": 5,
                           "usage": {"input_tokens": 1000, "output_tokens": 100,
                                     "reasoning_tokens": 10, "cache_read": 0},
                           "cache_ratio": 0.0, "cost_display": "UNKNOWN",
                           "lint": {"score": 100}, "reason": "ok",
                           "contract": {"acceptance": acc}},
            }, ensure_ascii=False), encoding="utf-8")
    return runs


def self_test() -> int:
    fails: list[str] = []
    print("── baseline.py 自检 ──")

    # ★ 不用 `tempfile.TemporaryDirectory`：它在 `__exit__` 里**自动 `rmtree`**，
    #   而本环境按**条目数**收删除费 ⇒ 一次自检就有 **8 棵**假归档树被删掉
    #   （`_synth` 被调 8 次）。
    #   ⚠️ 我按 `rmtree / unlink / remove` 关键字扫全仓时**扫不到这一处** ——
    #      真正的删除发生在上下文管理器的 `__exit__` 里。教训：**关键字扫不出
    #      全部删除点**，藏身之处还有 `TemporaryDirectory`、`NamedTemporaryFile`
    #      这类"自带清理"的上下文管理器。
    #   落点改仓库内（与 `_trash` 同盘），清场改 `_tkit`（挪，不删）。
    td = TK.fresh_dir(ROOT / "runs" / "_regress" / "_probe_eval", "eval-selftest")
    tmp = td
    try:

        # 【正控】足够样本 + 已知真值 ⇒ 必须算对，且判定可报
        spec = [(f"任务{i}", 3, 2) for i in range(MIN_TASKS)]
        s = summarize(*collect(_synth(tmp, spec)))
        ok, why = reportable(s)
        want = 2 * MIN_TASKS / (3 * MIN_TASKS)
        exp = MIN_TASKS * (2 / 3) / MIN_TASKS
        got_runs = sum(1 for x in s.rows if x.ok) / s.n_runs
        print(f"  正控：n_tasks={s.n_tasks} n_runs={s.n_runs} "
              f"重试口径={got_runs:.4f}（期望 {want:.4f}）可报={ok}")
        if not ok:
            fails.append(f"正控：样本充足却被判不可报：{why}")
        if abs(got_runs - want) > 1e-9:
            fails.append(f"正控：重试口径算错 {got_runs} != {want}")
        if s.n_tasks != MIN_TASKS:
            fails.append(f"正控：任务数应为 {MIN_TASKS}，得到 {s.n_tasks}")

        # 【负控】任务数不够 ⇒ 必须判不可报，且**不许**在正文里出现任何百分比成功率
        s2 = summarize(*collect(_synth(tmp, [("只有两个任务A", 5, 4), ("只有两个任务B", 2, 2)])))
        ok2, why2 = reportable(s2)
        txt2 = render(s2)
        print(f"  负控：n_tasks={s2.n_tasks} 可报={ok2} 退出码应为 2")
        if ok2:
            fails.append("负控：2 个任务竟被判可报")
        if "NOT REPORTABLE" not in txt2:
            fails.append("负控：报告里没有 NOT REPORTABLE")
        if "任务级成功率" in txt2:
            fails.append("负控：报告里出现了「任务级成功率」—— 不可报时不许出数")
        if s2.min_repeats != 2:
            fails.append(f"负控：最小重复次数应为 2，得到 {s2.min_repeats}")

        # 【负控2】任务数够、但有一个任务只跑 1 次 ⇒ 仍不可报（重复次数这条必须独立承重）
        spec3 = [(f"任务{i}", 3, 3) for i in range(MIN_TASKS)] + [("只跑一次的任务", 1, 1)]
        s3 = summarize(*collect(_synth(tmp, spec3)))
        ok3, why3 = reportable(s3)
        print(f"  负控2：n_tasks={s3.n_tasks}（够）min_repeats={s3.min_repeats} 可报={ok3}")
        if ok3:
            fails.append("负控2：仅因任务数够就放行，忽略了个别任务重复不足")

        # 【承重自检】把可报门槛短路成 force=True ⇒ 负控2 必须当场失守
        #   不失守 ⇒ 上面那些"不可报"是别的什么在报，这道门根本没在干活（G 型）
        ok3f, _ = reportable(s3, force=True)
        print(f"  承重自检：force=True 后可报={ok3f}（必须 True，否则这门不承重）")
        if not ok3f:
            fails.append("承重自检：短路门槛后仍判不可报 ⇒ 门槛不是唯一判据")

        # ── 评分标准漂移（2026-09-19 新增）──────────────────────────
        # 【正控】同一目标重复跑，但每次用的是**不同**的验收标准 ⇒ 必须检出，
        #   且报告里必须出现限定语（它是成功率的一行限定，不是附注）
        R1 = ["字数约150字", "内容说明低温诱导的作用", "全文不含任何文献引用"]
        R2 = ["文本字数约150字（例如140–160字）", "内容围绕低温诱导的作用展开", "全文不含引用标记"]
        s5 = summarize(*collect(_synth(tmp, [("同一目标的任务", 3, 3, [R1, R2])])))
        txt5 = render(s5)
        print(f"  正控·漂移：unstable={s5.rubric_unstable} 可比组={s5.rubric_comparable}（期望 1/1）")
        if s5.rubric_unstable != 1:
            fails.append(f"正控·漂移：应检出 1 组漂移，得到 {s5.rubric_unstable}")
        if s5.rubric_comparable != 1:
            fails.append(f"正控·漂移：可比组应为 1，得到 {s5.rubric_comparable}")
        if "评分标准漂移" not in txt5:
            fails.append("正控·漂移：报告里没写漂移，成功率会被当成同一把尺子量的")

        # 【负控】同一套标准重复三次 ⇒ **不许**报漂移（误报比漏报更伤）
        s6 = summarize(*collect(_synth(tmp, [("标准不变的任务", 3, 3, [R1, R1, R1])])))
        txt6 = render(s6)
        print(f"  负控·漂移：unstable={s6.rubric_unstable}（期望 0）")
        if s6.rubric_unstable != 0:
            fails.append("负控·漂移：同一套标准被误报成漂移")
        if "0 组（可比组 1 个）" not in txt6:
            fails.append("负控·漂移：应写明「0 组（可比组 1 个）」—— 真比过才敢说不漂")

        # 【负控2】只是逐条**顺序**不同 ⇒ 不算漂移（判据必须先规范化再排序）
        s7 = summarize(*collect(_synth(tmp, [("顺序不同的任务", 2, 2,
                                              [R1, list(reversed(R1))])])))
        print(f"  负控·漂移2：unstable={s7.rubric_unstable}（期望 0，仅顺序不同）")
        if s7.rubric_unstable != 0:
            fails.append("负控·漂移2：仅条目顺序不同被算成漂移（应排序后比较）")

        # 【负控3】归档里取不到契约 ⇒ 判**判不了**（0/0），既不许算漂移，也不许暗示"很稳"
        s8 = summarize(*collect(_synth(tmp, [("没有契约的任务", 3, 3)])))
        txt8 = render(s8)
        print(f"  负控·漂移3：unstable={s8.rubric_unstable} 可比组={s8.rubric_comparable}（期望 0/0）")
        if s8.rubric_unstable != 0 or s8.rubric_comparable != 0:
            fails.append("负控·漂移3：取不到契约时应判「判不了」(0/0)")
        if "没比过" not in txt8:
            fails.append("负控·漂移3：0 个可比组时没写明「没比过」—— 这是 F 型假安慰")

        # 【承重自检】把漂移判据短路成"永远 0 漂移" ⇒ 上面的正控必须当场失守。
        #   不失守 ⇒ 那条"检出漂移"是别的什么在报，这道判据根本没在干活（G 型）。
        _orig_drift = globals()["rubric_drift"]
        globals()["rubric_drift"] = lambda tasks: (0, 0, [])
        try:
            s9 = summarize(*collect(_synth(tmp, [("同一目标的任务", 3, 3, [R1, R2])])))
            print(f"  承重自检·漂移：短路判据后 unstable={s9.rubric_unstable}（必须 0）")
            if s9.rubric_unstable != 0:
                fails.append("承重自检·漂移：短路后仍报漂移 ⇒ 漂移不是这条判据报的")
        finally:
            globals()["rubric_drift"] = _orig_drift

        # 【坏归档不许静默跳过】
        (tmp / "runs" / "20990101-999999").mkdir(parents=True, exist_ok=True)
        s4 = summarize(*collect(tmp / "runs"))
        print(f"  坏归档：bad={len(s4.bad)}（应为 1）")
        if len(s4.bad) != 1:
            fails.append("坏归档目录没被如实报告")

        # 【字段名口径】只认 input_tokens；给一个 prompt_tokens-only 的归档必须读成 0 而不是瞎猜
        d = tmp / "runs" / "20990102-000001"
        d.mkdir(parents=True, exist_ok=True)
        (d / "session.json").write_text(json.dumps({
            "id": d.name, "goal": "口径测试", "elapsed": 1.0,
            "result": {"success": True, "steps": 1,
                       "usage": {"prompt_tokens": 999, "completion_tokens": 9},
                       "lint": {}, "reason": ""},
        }, ensure_ascii=False), encoding="utf-8")
        s5 = summarize(*collect(tmp / "runs"))
        row = [x for x in s5.rows if x.run_id == d.name][0]
        print(f"  字段名口径：prompt_tokens=999 → 读成 in_tok={row.in_tok}（应为 0，不许猜）")
        if row.in_tok != 0:
            fails.append("字段名口径：把 prompt_tokens 当成 input_tokens 混读了")

        # 【正控/负控】成本口径 —— 这里真出过一个"方向反了"的假数（第一版把
        #   「6/9 场 UNKNOWN」反推成「表已填」），所以这条必须有控。
        #
        #   ⚠️ 第一版这条自检**自己也不承重**（G 型）：断言写的是
        #   `"表已填" in txt6`，而渲染出来的解释句里正好有「既不能说『表已填』」
        #   这五个字 —— 于是断言匹配的是**我写的解释文字**，不是结论。
        #   修法：断言**计算值**（cost_stats 的返回元组），不比对渲染文案。
        #   渲染是那个元组的纯函数，测计算才是测到根。
        s6 = summarize(*collect(_synth(tmp, [(f"任务{i}", 3, 3) for i in range(MIN_TASKS)])))
        c6 = cost_stats(s6.rows)
        print(f"  成本负控：cost_stats={c6}（应为 (0, 90, 0)：一场都没单价）")
        if c6 != (0, s6.n_runs, 0):
            fails.append(f"成本负控：9/9 无单价时 cost_stats 应为 (0,{s6.n_runs},0)，得到 {c6}")
        # 正控：真给一场单价 ⇒ 必须**不再**报「一场都没有」
        one = sorted((tmp / "runs").iterdir())[0] / "session.json"
        j = json.loads(one.read_text(encoding="utf-8"))
        j["result"]["cost_display"] = "0.0123 USD"
        one.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        c7 = cost_stats(summarize(*collect(tmp / "runs")).rows)
        print(f"  成本正控：cost_stats={c7}（应为 (1, 89, 0)）")
        if c7 != (1, s6.n_runs - 1, 0):
            fails.append(f"成本正控：1 场有单价时应为 (1,{s6.n_runs - 1},0)，得到 {c7}")

        # ── 用量口径：两份读取器**必须逐场一致** ──────────────────────
        # `collect()`（本文件的逐场视图）与 `rongke.memory.archived_usage()`
        # （产品层唯一实现）读的是同一批 `session.json` 的 `result.usage.input_tokens`。
        # 它们分家 = 同一件事两处真相，正是 B 型缺陷 —— 而两个数都会被人拿去
        # 算钱。所以这里让它们在同一棵假树上对账，而不是靠"记得同步"。
        spec_use = [("用量任务A", 3, 3), ("用量任务B", 2, 2)]
        _synth(tmp, spec_use)
        rdir = tmp / "runs"
        rows_u, _bad_u = collect(rdir)
        sum_rows = sum(x.in_tok for x in rows_u)
        au = archived_usage(rdir)
        print(f"  口径·一致：archived_usage={au['input_tokens']} vs Σcollect={sum_rows}"
              f"（应为 5×1000=5000）")
        if sum_rows != 5000:
            fails.append(f"口径真值：Σcollect 应为 5000，得到 {sum_rows}")
        if au["input_tokens"] != sum_rows:
            fails.append(f"两份读取器分家：archived_usage={au['input_tokens']} "
                         f"但 Σcollect.in_tok={sum_rows}")

        # 【判别力正控】往 `_` 前缀目录里塞一份**带用量**的副本（模拟真环境里的
        # `runs/_ui_resume`，它是真运行的逐字节副本）。两份读取器都该不数它；
        # 而"少跳一步 `_`"的天真实现会多算 77777 ⇒ 两个答案**分得开**，
        # 才证明「跳 `_`」这条规矩真的在承重，而不是写了等于没写。
        def _naive_usage(d: Path) -> int:
            """**故意写错**：顶层目录一律数、不跳 `_`。只当判别力正控。"""
            tot = 0
            for dd in sorted(Path(d).iterdir()):
                p = dd / "session.json"
                if not dd.is_dir() or not p.exists():
                    continue
                j = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                tot += int(((j.get("result") or {}).get("usage") or {})
                           .get("input_tokens") or 0)
            return tot

        fx = rdir / "_fixture" / "session.json"
        fx.parent.mkdir(parents=True, exist_ok=True)
        fx.write_text(json.dumps({
            "id": "_fixture", "goal": "用量任务A", "elapsed": 1.0,
            "result": {"run_id": "用量任务A", "success": True, "steps": 5,
                       "usage": {"input_tokens": 77777, "output_tokens": 7},
                       "cost_display": "UNKNOWN", "lint": {"score": 100},
                       "contract": {"acceptance": []}},
        }, ensure_ascii=False), encoding="utf-8")
        au_fx = archived_usage(rdir)
        rows_fx, _ = collect(rdir)
        sum_fx = sum(x.in_tok for x in rows_fx)
        naive_fx = _naive_usage(rdir)
        print(f"  口径·跳 `_`：archived={au_fx['input_tokens']} Σcollect={sum_fx}"
              f"；天真实现会读成 {naive_fx}（应为 5000 / 5000 / 82777）")
        if au_fx["input_tokens"] != 5000 or sum_fx != 5000:
            fails.append(f"`_` 前缀的建材被算进了总数：archived={au_fx['input_tokens']} "
                         f"Σcollect={sum_fx}（都应为 5000）")
        if naive_fx != 82777:
            fails.append(f"判别力正控失效：天真实现应读成 82777，得到 {naive_fx}"
                         f" ⇒ 这条检查分辨不出「跳 `_`」有没有生效")
        if au_fx["skipped"] != ["_fixture"]:
            fails.append(f"跳过清单：应为 ['_fixture']，得到 {au_fx['skipped']}")
    finally:
        # 挪走，不删（理由见本函数开头那段）。
        TK.stash(td, "eval-selftest")

    if fails:
        print("\n自检失败：")
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("\n自检全过（2 正控 + 5 负控 + 2 承重自检 + 5 口径）")
    return 0


# ══════════════════════ CLI ══════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂评测基线（口径写死，样本不足拒绝出数）")
    ap.add_argument("--runs", default=str(DEFAULT_RUNS), help="归档根（默认 runs/）")
    ap.add_argument("--tasks", default=str(DEFAULT_TASKS), help="任务集 jsonl")
    ap.add_argument("--since", default="", help="只看 YYYYMMDD 之后的归档")
    ap.add_argument("--json", action="store_true", help="额外输出机器读的 JSON")
    ap.add_argument("--self-test", action="store_true", help="正控/负控/承重自检")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    rows, bad = collect(Path(a.runs), since=a.since)
    s = summarize(rows, bad)
    if not rows:
        print(f"没有读到任何运行归档（{a.runs}）。")
        for b in bad[:5]:
            print(f"  · {b}")
        return 1
    print(render(s, tasks_file=Path(a.tasks)))
    if a.json:
        ok, why = reportable(s)
        print("\n" + json.dumps({
            "reportable": ok, "why_not": why,
            "n_runs": s.n_runs, "n_tasks": s.n_tasks, "min_repeats": s.min_repeats,
            "tasks": [{"gid": t.gid, "n": t.n, "ok": t.ok,
                       "steps": [x.steps for x in t.runs]} for t in s.tasks],
        }, ensure_ascii=False, indent=2))
    ok, _ = reportable(s)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
