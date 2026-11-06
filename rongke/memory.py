# -*- coding: utf-8 -*-
"""荣珂记忆：会话存档 + 跨任务经验。

两层：
  * **会话层**（`memory/runs.jsonl`）—— 每次任务一条完整记录（目标/契约/步骤/
    工具调用/结果）。用途：可追溯、可复盘、给下次任务做相似召回。
  * **经验层**（`memory/lessons.md`）—— 人可读的长期经验，跨任务累积，
    可注入后续任务的上下文。

为什么要它：没有记忆的 agent 每次都在从零试错。有了它，上次踩过的坑
（哪个工具常失败、哪条路走不通）能变成下次的先验。

一条纪律：**历史只作参考，事实必须重新用工具核实**。注入文本里写死了这句，
因为"拿上次的结论当这次的证据"是这类系统最危险的退化方式。
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _shingles(s: str, n: int = 2) -> set[str]:
    """字符 n-gram —— 中文没空格，bigram 比按空格分词稳。"""
    s = re.sub(r"\s+", "", (s or "").lower())
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ── 召回排序的三个乘数（2026-09-18 新增）────────────────────
# 原实现只有"相似度"一维，实测暴露两个病：
#   ① **打平时永远召回最旧的**：`sorted(..., key=-score)` 是**稳定排序**，
#      而输入 `recent_runs()` 是按时间正序 ⇒ 同分者保持原顺序 ⇒ 最新的经验
#      排在最前面被 `[:k]` 切掉。最近踩的坑反而进不了上下文。
#   ② **同一个目标召回 3 次**：没有任何去重，同一个目标字符串的相似度都是 1.0，
#      于是注入文本里同一句话重复 3 遍（实测：「写一段约120字的论文引言草稿…」
#      在注入里出现 3 次）—— 白占 3 倍字符，而字符是每步重发的。
_HALF_LIFE_DAYS = 14.0        # 新近度半衰期：14 天前的记录权重减半
_DEDUP_GOAL_SIM = 0.90        # 目标相似度超过它即视为"同一个任务"，只留分最高的


def _recency_weight(ts: float, now: float,
                    half_life_days: float = _HALF_LIFE_DAYS) -> float:
    """时间衰减，返回 (0, 1]。

    `ts <= 0`（没有时间戳的旧记录）返回 **0.5** 而不是 0 ——
    把"不知道多旧"当成"很旧"会让老经验永久沉默；当成"很新"又会挤掉真新的。
    中间值 = 不惩罚也不优待，这是唯一诚实的选项。
    """
    if ts <= 0:
        return 0.5
    age_days = max(0.0, (now - ts) / 86400.0)
    return 0.5 ** (age_days / max(0.5, half_life_days))


def _importance(d: dict) -> float:
    """这条记录携带「可迁移教训」的量，返回 (0, 1]。

    判据刻意只用**记录自带、机器可算**的字段，不去猜内容质量：
      * 未完成 + 写明了 `reason`  → **1.0**：它是"这条路走不通"的直接证词，
        比一次顺利的成功更该被下次看到（踩坑清单存在的全部理由）。
      * 成功                     → 0.9：有可复用的路径。
      * 未完成 + 没给 reason      → **0.6**：多半只是撞了步数上限，信息量最低。
    """
    if not bool(d.get("success")):
        return 1.0 if str(d.get("reason") or "").strip() else 0.6
    return 0.9


@dataclass
class RunRecord:
    goal: str
    success: bool
    steps: int
    elapsed: float
    tools_used: list[str] = field(default_factory=list)
    final: str = ""
    reason: str = ""
    workspace: str = ""
    # 本次任务的归档 id —— 用来把台账与 runs/<id>/session.json 对上，避免历史页重复列同一任务。
    # 旧记录没有这个字段，读到的是 ""，按"不可回放"处理即可，向后兼容。
    run_id: str = ""
    # ── 用量与成本（2026-09-18 新增）────────────────────────
    # 旧记录没有这几个字段 ⇒ 读取时按默认值处理，不假装它们存在。
    # **成本默认 "UNKNOWN" 而不是 0**：0 是"这次不要钱"的断言，
    # UNKNOWN 是"我不知道"的事实。把后者渲染成前者就是假安慰。
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cost_usd: object = "UNKNOWN"
    # 失败的工具名（含重复 = 失败多次）。
    # 为什么单独记：`tools_used` 只有名字、**没有成败** ⇒ 台账算不出工具失败率，
    # 蒸馏层就没法说出"哪个工具常坏"这种最该传给下次的先验。
    # 一个名字一条 = 一次失败；与 `tools_used` 对齐即可算出比例。
    tools_failed: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    def line(self) -> str:
        """给**模型**看的一行：只保留对决策有用的信息。

        ⚠️ 这个方法**只被 `inject_text` 调用** —— 它输出的每一个字符
        都会在后续每一步重发（一份注入文本 × N 步 = N 倍成本）。
        所以它必须克制：**步数与工具名对模型有决策价值**（暗示这类任务要几轮、
        通常走哪条路），而"上次花了多少钱"是**给人看**的，对模型纯属噪声。
        给人看的版本是 `line_detail()`。
        """
        mark = "成功" if self.success else "未完成"
        tools = ", ".join(sorted(set(self.tools_used))) or "无"
        return f"  · 「{self.goal[:60]}」→ {mark}（{self.steps} 步 / {self.elapsed:.0f}s；工具：{tools}）"

    def line_detail(self) -> str:
        """给**人**看的一行（终端列表、界面），带用量与成本。**不要用它做注入。**"""
        cost = "UNKNOWN"
        if self.cost_usd != "UNKNOWN" and self.cost_usd is not None:
            try:
                cost = f"${float(self.cost_usd):.4f}"
            except Exception:
                cost = "UNKNOWN"
        tok = (f"{self.input_tokens}+{self.output_tokens} tok"
               if (self.input_tokens or self.output_tokens) else "用量未记录")
        return f"{self.line()}  [{tok} · 成本 {cost}]"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts_iso"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))
        return d


# ── 归档口径的用量统计 ──────────────────────────────────────────
# 这里是全项目**唯一**的「总共花了多少 token」实现。CLI 与 Web 都读它。
# ⚠️ 不要另外写一份。`eval/baseline.py::collect()` 也读同一批文件，但它要的是
#    **逐场**的行（rubric/lint/contract），不是总额 —— 总额只在下面这一个地方算，
#    `baseline.py --self-test` 里有条断言盯着两者不许分家。

class ArchivedUsageCache:
    """按归档文件签名缓存用量；只存小型汇总，不持有稿件正文。"""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[tuple[int, int], dict | None]] = {}


def archived_usage(runs_dir: Path, since: str = "", cache: ArchivedUsageCache | None = None) -> dict:
    """**已归档运行的真实用量。** 读 `runs/<id>/session.json` 的 `result.usage`。

    ── 为什么以归档为准，而不是台账（`memory/runs.jsonl`）──────────
    两处**同源**（都来自内核的 `result.usage`），但**覆盖不同**。
    实测（2026-09-19，`runs/` 下 21 个子目录）分三段：

    | 段 | 场次 | 归档 | 台账 |
    |---|---|---|---|
    | 09-18 17:15–18:09 | 7 | **没有**（目录里没 session.json） | 没有用量字段 |
    | 09-18 19:30–20:38 | 3 | **有，且完整** | 两行写了 0、一行根本没进台账 |
    | 09-18 21:46 起 | 11 | 有 | 有（**逐场一致，分毫不差**） |

    合计：归档 **1,065,746** 输入 / 台账 **708,820** 输入 ⇒ 台账 **66.5%**。
    根因是一次内核部署：`llm_done` 事件与台账用量字段**同时**出现，而在那之前
    内核**已经在累加** `result.usage` 了。所以漏掉的那 356,926 全是**最贵**的几场
    （两个 14 步的 Origin 任务，单场 15 万+ 输入）。
    ⇒ **拿台账当"历史累计"会少报 33.5%，且漏的恰恰是大头。**

    ⚠️ 判据：顶层目录名以 `_` 开头的一律**不数**。它们不是任务、是建材
    （测试夹具 / 回归日志 / 回收站）。`_ui_resume/session.json` 尤其是地雷：
    它是真运行 `20260918-203437` 的**逐字节副本**（连 run_id 都一样），
    数字是真的，但把它算进来 = **同一场计两遍**。
    （`cmd_pricing` 扫「响应侧模型」那段跳 `_` 也是同一条规矩 —— 不过那里
    实测**目前无差别**，因为那份副本出自 `llm_done` 事件诞生之前、一条都没有。
    它是防御，不是修一个已看到的错。）

    ⚠️ `without_usage` / `no_archive` 都要**如实报出来**，不许吞掉 —— 这个数是
    **下界**，不是总额。把"不可知"渲染成"没有"就是 F 型缺陷。
    """
    from . import pricing as _pricing
    out: dict = {
        "runs": 0, "input_tokens": 0, "output_tokens": 0,
        "reasoning_tokens": 0, "cache_read": 0, "cache_ratio": 0.0,
        "cost_usd": _pricing.UNKNOWN, "cost_display": _pricing.format_cost(_pricing.UNKNOWN),
        "uncosted": 0, "without_usage": [], "no_archive": [], "skipped": [],
        "runs_dir": str(runs_dir),
    }
    runs_dir = Path(runs_dir)
    if not runs_dir.exists():
        return out

    total_cost = 0.0
    uncosted = 0
    for d in sorted(runs_dir.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith("_"):
            out["skipped"].append(d.name)      # 建材，不是任务
            continue
        if since and d.name[:8] < since:
            continue
        p = d / "session.json"
        if not p.exists():
            # 早期运行还没归档 —— 它的用量**不可知**，不是 0。
            out["no_archive"].append(d.name)
            continue
        try:
            st = p.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            out["no_archive"].append(d.name)
            continue
        cached = None
        if cache is not None:
            with cache._lock:
                hit = cache._rows.get(str(p))
                cached = hit[1] if hit is not None and hit[0] == sig else None
        if cached is None:
            try:
                j = json.loads(p.read_text(encoding="utf-8", errors="replace"))
                r = j.get("result") or {}
                u = r.get("usage") or {}
                cached = {"usage": {k: int(u.get(k) or 0) for k in
                                    ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read")},
                          "cost": r.get("cost_usd", _pricing.UNKNOWN)}
            except Exception:
                cached = {"invalid": True}
            if cache is not None:
                with cache._lock:
                    # 防止实例被长期用于大量临时工作区时无界增长。
                    if len(cache._rows) >= 1024 and str(p) not in cache._rows:
                        cache._rows.pop(next(iter(cache._rows)), None)
                    cache._rows[str(p)] = (sig, cached)
        if cached.get("invalid"):
            out["no_archive"].append(d.name)   # 解析不了 = 读不到，同样不可知
            continue
        u = cached["usage"]
        i = int(u.get("input_tokens") or 0)
        o = int(u.get("output_tokens") or 0)
        if not i and not o:
            out["without_usage"].append(d.name)
            continue
        out["runs"] += 1
        out["input_tokens"] += i
        out["output_tokens"] += o
        out["reasoning_tokens"] += int(u.get("reasoning_tokens") or 0)
        out["cache_read"] += int(u.get("cache_read") or 0)
        c = cached["cost"]
        if c == _pricing.UNKNOWN or c is None:
            uncosted += 1
        else:
            try:
                total_cost = round(total_cost + float(c), 8)
            except Exception:
                uncosted += 1

    out["uncosted"] = uncosted
    if out["input_tokens"]:
        out["cache_ratio"] = round(out["cache_read"] / out["input_tokens"], 4)
    # 成本只在**每一场**都能算时才给总额；否则如实 UNKNOWN。
    # 「场次为 0」也给 UNKNOWN —— 0 场不是"免费"，是"没数据"。
    if out["runs"] and not uncosted:
        out["cost_usd"] = total_cost
        out["cost_display"] = _pricing.format_cost(total_cost)
    return out


def default_root(cfg=None):
    """记忆根目录的**唯一**算法。

    为什么要有这个函数：此前 `Memory(project_root() / "memory")` 散在 **5 处**
    （loop / cli ×2 / web.server ×2），于是"把记忆定向到别处"这件事做不到 ——
    2026-09-26 实测咬人：四臂实验一天往共享台账追加 39 条，`web_test.py` 的
    历史正控（读真台账、取前 50）被挤出窗口，**回归自己变红**。测试/实验要能
    换一个目录，判据才不随产品使用量漂移。

    取值优先级：显式 `cfg.runtime.memory_dir` → 环境变量 `RONGKE_MEMORY_DIR`
    （已在 Config.load 里并入 runtime）→ `用户数据根/memory`。

    ★ 末档从 `project_root()` 改成 `data_root()`：装成依赖后 `project_root()`
      指向 site-packages，记忆会被写进库目录（升级即丢、只读环境直接炸）。
      源码树里两者**是同一个目录**，所以现有行为一字不变。
    """
    md = getattr(getattr(cfg, "runtime", None), "memory_dir", None)
    if md:
        return Path(md)
    import os
    env = os.environ.get("RONGKE_MEMORY_DIR")
    if env:
        return Path(env)
    from .config import data_root
    return data_root() / "memory"


class Memory:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.runs_path = self.root / "runs.jsonl"
        self.lessons_path = self.root / "lessons.md"
        self.root.mkdir(parents=True, exist_ok=True)

    # ── 写 ──────────────────────────────────────────────────
    def save_run(self, goal: str, result, elapsed: float = 0.0) -> RunRecord:
        usage = getattr(result, "usage", None)
        cost = getattr(result, "cost", "UNKNOWN")
        trace = list(getattr(result, "trace", []) or [])
        rec = RunRecord(
            goal=goal,
            success=bool(getattr(result, "success", False)),
            steps=int(getattr(result, "steps", 0)),
            elapsed=round(float(elapsed), 1),
            tools_used=[c.name for c in trace],
            tools_failed=[c.name for c in trace if not getattr(c, "ok", False)],
            final=(getattr(result, "final", "") or "")[:300],
            reason=(getattr(result, "reason", "") or "")[:300],
            workspace=getattr(result, "workspace", "") or "",
            run_id=str(getattr(result, "run_id", "") or ""),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read=int(getattr(usage, "cache_read", 0) or 0),
            cost_usd=cost if cost is not None else "UNKNOWN",
        )
        with self.runs_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        return rec

    def note(self, text: str) -> None:
        """追加一条长期经验（人可读）。**手工渠道**，与下面的自动蒸馏并存。

        为什么两条渠道都要：自动层只能说出"数字里读得出来的话"，
        而人有时知道数字里没有的东西（某工具上游坏了、某个口径变了）。
        注入时两者会带不同标签，模型能分辨哪条是算出来的、哪条是人写的。
        """
        stamp = time.strftime("%Y-%m-%d")
        with self.lessons_path.open("a", encoding="utf-8") as f:
            f.write(f"- [{stamp}] {text.strip()}\n")

    # ── 蒸馏：把台账算成经验（**零 LLM**）────────────────────
    # 分工：这一层只输出**能从数字里直接读出来**的结论 ⇒ 它不可能编造。
    # 需要"理解一次任务为什么失败"的那部分留给 LLM 层（`distill_run`），
    # 且必须带 provenance 校验 —— 对不上原文的教训一律丢弃。
    _MIN_SAMPLES = 5          # 少于这么多条任务记录，一条经验都不出
    _MIN_TOOL_CALLS = 3       # 一个工具至少被调过这么多次，才谈得上"失败率"
    _TOOL_FAIL_RATE = 0.3     # 失败率阈值

    def distill_stats(self, n: int = 200) -> list[str]:
        """从台账里**算**出可注入的经验条目。**零 LLM、零猜测。**

        样本不足就**返回空**，不给勉强的结论 —— 出"经验"比出"数字"更需要样本。
        一个只见过 2 次就总结出的"规律"，注入给模型就是一条很像真的**假先验**，
        而假先验比没有先验更危险（它会改变模型的决策，且无从证伪）。

        这是本轮补上的**生产者**：在此之前 `lessons.md` 没有任何写入方、
        文件根本不存在，`inject_text` 里的"积累的经验"分支恒为空 ——
        文档写着"两层记忆"，实际只有会话层是活的（与上一轮那个
        从未被实例化的 `AuditLog` 同型）。
        """
        runs = self.recent_runs(n)
        if len(runs) < self._MIN_SAMPLES:
            return []
        out: list[str] = []

        # ① 常失败的工具 —— 让它下次先想好备选路径，而不是在同一个坑里反复试
        totals: dict[str, int] = {}
        fails: dict[str, int] = {}
        for r in runs:
            for name in (r.get("tools_used") or []):
                totals[name] = totals.get(name, 0) + 1
            for name in (r.get("tools_failed") or []):
                fails[name] = fails.get(name, 0) + 1
        bad = [(fails.get(nm, 0) / t, nm, fails.get(nm, 0), t)
               for nm, t in totals.items() if t >= self._MIN_TOOL_CALLS]
        bad = [b for b in bad if b[0] >= self._TOOL_FAIL_RATE]
        bad.sort(key=lambda b: (-b[0], -b[3]))
        for rate, nm, f, t in bad[:3]:
            out.append(f"工具 `{nm}` 失败偏多（{f}/{t} ≈ {rate:.0%}）——"
                       f"用到它时先想好备选路径，别在同一处反复试探。")

        # ② 成功任务的步数带宽 —— 给"我是不是在打转"一个参照系
        steps = sorted(int(r.get("steps") or 0) for r in runs
                       if r.get("success") and int(r.get("steps") or 0) > 0)
        if len(steps) >= self._MIN_SAMPLES:
            med = steps[len(steps) // 2]
            out.append(f"历史成功任务的步数落在 {steps[0]}–{steps[-1]} 之间（中位 {med} 步）——"
                       f"步数明显超出时，先回头检查是不是在反复试探同一个问题。")

        # ③ 未完成比例 —— 提醒"撞上限 ≠ 做错了"
        bad_runs = [r for r in runs if not r.get("success")]
        if bad_runs and len(bad_runs) / len(runs) >= self._TOOL_FAIL_RATE:
            out.append(f"最近 {len(runs)} 次任务里有 {len(bad_runs)} 次未完成 ——"
                       f"多半是撞了步数上限而非做错了；早点收敛比穷尽试探划算。")
        return out

    # ── 读 ──────────────────────────────────────────────────
    def recent_runs(self, n: int = 50) -> list[dict]:
        """只读末尾 n 条。追加式台账不应让每次召回重读整个历史。"""
        n = max(0, int(n))
        if not n or not self.runs_path.exists():
            return []
        try:
            out: deque[dict] = deque(maxlen=n)
            examined = 0
            with self.runs_path.open("rb") as f:
                f.seek(0, 2)
                pos = f.tell()
                while pos > 0 and len(out) < n and examined < n * 2:
                    prev = buf = b""
                    # 找到上一条换行；每次只向前扩窗口，不解码整份文件。
                    while True:
                        start = max(0, pos - 65536)
                        f.seek(start)
                        buf = f.read(pos - start) + prev
                        at = buf.rfind(b"\n")
                        if at >= 0 or start == 0:
                            break
                        prev = buf
                        pos = start
                        if len(buf) > 1024 * 1024:
                            break
                    if len(buf) > 1024 * 1024:
                        break
                    raw = buf[at + 1:] if at >= 0 else buf
                    pos = start + at if at >= 0 else 0
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    # 空行不占坏行预算；坏行最多容忍 n 条。
                    examined += 1
                    try:
                        out.appendleft(json.loads(line))
                    except Exception:
                        continue
                    if len(out) == n:
                        break
            return list(out)
        except OSError:
            return []

    def lessons(self, n: int = 20) -> list[str]:
        if not self.lessons_path.exists():
            return []
        lines = [l.strip() for l in
                 self.lessons_path.read_text(encoding="utf-8", errors="replace").splitlines()]
        return [l for l in lines if l.startswith("-")][-n:]

    def recall(self, goal: str, k: int = 3, min_score: float = 0.06,
               now: float | None = None) -> list[RunRecord]:
        """按「相似度 × 重要性 × 新近度」召回历史任务，并对**近乎相同的目标去重**。

        三个乘数各自治一个实测过的病，见模块里 `_recency_weight` / `_importance`
        上面那段注释；去重治的是"同一目标注入 3 遍"。

        为什么要去重而不是"靠调 k"：k 是**给模型看几条不同经验**的预算，
        不是"允许几条重复"的预算。重复条目把预算吃掉，等于把 k 变小了。
        """
        now = time.time() if now is None else float(now)
        scored: list[tuple[float, dict]] = []
        for d in self.recent_runs(200):
            g = str(d.get("goal", ""))
            s = _similarity(goal, g)
            if s < min_score:
                continue
            scored.append((s * _importance(d)
                           * _recency_weight(float(d.get("ts") or 0), now), d))
        # 同分时用 ts 兜底（**新的优先**）。少了这个兜底，稳定排序会让新经验
        # 永远排在旧经验后面、被 [:k] 切掉 —— 那正是原来的病。
        scored.sort(key=lambda x: (-x[0], -float(x[1].get("ts") or 0)))

        out: list[RunRecord] = []
        seen_goals: list[str] = []
        for _, d in scored:
            g = str(d.get("goal", ""))
            if any(_similarity(g, prev) >= _DEDUP_GOAL_SIM for prev in seen_goals):
                continue                    # 与已选中的某条是同一个任务，跳过
            seen_goals.append(g)
            out.append(RunRecord(
                goal=str(d.get("goal", "")), success=bool(d.get("success")),
                steps=int(d.get("steps") or 0), elapsed=float(d.get("elapsed") or 0),
                tools_used=list(d.get("tools_used") or []),
                tools_failed=list(d.get("tools_failed") or []),
                final=str(d.get("final") or ""), reason=str(d.get("reason") or ""),
                workspace=str(d.get("workspace") or ""), ts=float(d.get("ts") or 0),
                input_tokens=int(d.get("input_tokens") or 0),
                output_tokens=int(d.get("output_tokens") or 0),
                cache_read=int(d.get("cache_read") or 0),
                # 旧记录没有 cost_usd ⇒ 按 UNKNOWN（"我不知道"），不是 0（"免费"）
                cost_usd=d.get("cost_usd", "UNKNOWN"),
                run_id=str(d.get("run_id") or ""),
            ))
            if len(out) >= k:
                break
        return out

    # ── 注入 ────────────────────────────────────────────────
    @staticmethod
    def _render(past: list[RunRecord], lessons: list[tuple[str, str]]) -> str:
        """`lessons` 是 (来源标签, 正文) 的列表 —— 标签必须带，见 `all_lessons`。"""
        parts = ["【历史参考 —— 仅供参考，不要照搬】"]
        if past:
            parts.append("你之前做过相似任务：")
            parts += [r.line() for r in past]
        if lessons:
            parts.append("积累的经验：")
            parts += [f"  [{tag}] {text}" for tag, text in lessons]
        parts.append(
            "⚠️ 以上只是提示，**不是证据**。本次任务的事实必须重新用工具核实，"
            "不得引用历史结论充当本次证据。"
        )
        return "\n".join(parts)

    def all_lessons(self, n_manual: int = 8) -> list[tuple[str, str]]:
        """合并两条来源，**各带标签**：`统计` = 从台账算出来的；`人工` = 人写的。

        为什么要标签：这两类断言的**可信度性质不同** ——
        "统计"是这台机器自己的历史频率（可复算），"人工"是某个人的判断（不可复算）。
        混在一起会让模型把后者当成前者。注入文本里每个字都该能回答"你从哪来"。

        顺序上**自动在前**：它贴合这台机器的实际行为，而人工条目是泛化的。
        """
        out: list[tuple[str, str]] = [("统计", s) for s in self.distill_stats()]
        # `lessons()` 返回的是 `lessons.md` 的原始行（自带 `- ` 前缀）——
        # 注入时已经有 `[人工]` 标签，再留一个破折号就是双重项目符号、
        # 既难看又白占 2 个字符（每步重发的东西，每个字符都要有理由）。
        out += [("人工", l[2:].strip() if l.startswith("- ") else l.strip())
                for l in self.lessons(n_manual)]
        return out

    def inject_text(self, goal: str, k: int = 3, max_chars: int = 1400) -> str:
        """拼出要注入的"历史参考"块。**字符预算是硬约束。**

        为什么是硬约束：这段文本会在后续**每一步**重发（一份注入 × N 步 = N 倍成本）。
        实测首步 messages 只有 3,201 字符，这段就占一块。

        超预算时按**退化阶梯**逐级砍：先砍经验条数（`all → 4 → 0`），
        再砍历史条数（`k → … → 1`）。顺序有理由 ——
        "谁做过相似任务"比"泛泛的经验条目"更贴这次的具体目标。

        **装不下就什么都不注入**（返回 `""`），绝不截断到半句话：
        半截的教训比没有教训更危险 —— 模型会自己把它补全成错的。
        """
        lessons_all = self.all_lessons()
        past_all = self.recall(goal, k=max(1, k))
        if not lessons_all and not past_all:
            return ""
        for n_lessons in (len(lessons_all), 4, 0):
            for n_past in range(len(past_all), 0, -1):
                text = self._render(past_all[:n_past], lessons_all[:n_lessons])
                if len(text) <= max_chars:
                    return text
        return ""

    def stats(self, runs_dir: Path | None = None) -> dict:
        """统计。**两个口径分开放，别混着读。**

        ⚠️ 2026-09-19 改过形状，原因值得记：
        原先 `input_tokens` / `output_tokens` / `cache_read` / `cache_ratio` /
        `cost_usd` / `cost_display` 直接挂在顶层 ⇒ 调用方（`cli.py --pricing`）
        很自然地把它读成了「历史累计用量」。但这个是**台账**口径，而台账的
        用量字段是**后加的**，只覆盖 21 场里的 11 场 ⇒ **少报 33.5%**。
        现在：
          · 顶层只留**计数**（runs / success / lessons*）—— 计数没有两种口径；
          · `ledger`   = 台账口径（**会漏记**，别拿来报"累计用量"）；
          · `archived` = 归档口径（**完整的那把秤**；不传 `runs_dir` 就是 `None`
            —— 是"没查"，不是"查出来是 0"）。
        """
        runs = self.recent_runs(1000)
        ok = [r for r in runs if r.get("success")]
        in_tok = sum(int(r.get("input_tokens") or 0) for r in runs)
        out_tok = sum(int(r.get("output_tokens") or 0) for r in runs)
        cache = sum(int(r.get("cache_read") or 0) for r in runs)
        # 成本只在**所有**记录都可算时给出总额；混杂未知就如实报 UNKNOWN。
        # （见 pricing.add_cost：任一侧未知 ⇒ 结果未知，不许把未知吞成已知。）
        from . import pricing as _pricing
        total_cost: object = 0.0
        priced = 0
        for r in runs:
            c = r.get("cost_usd", "UNKNOWN")
            if c == _pricing.UNKNOWN or c is None:
                continue
            try:
                total_cost = round(float(total_cost) + float(c), 8)
                priced += 1
            except Exception:
                continue
        if not priced or priced < len([r for r in runs if int(r.get("input_tokens") or 0)]):
            total_cost = _pricing.UNKNOWN
        # 经验计数必须**含自动蒸馏的那部分** —— 只数 `lessons.md` 会让界面
        # 显示"经验 0"而注入里其实有话，那是"显示与现实不一致"。
        n_auto = len(self.distill_stats())
        n_manual = len(self.lessons(1000))
        return {
            # ── 计数：没有两种口径，留顶层 ────────────────────────
            "runs": len(runs),
            "success": len(ok),
            "lessons": n_auto + n_manual,
            "lessons_auto": n_auto,
            "lessons_manual": n_manual,
            # ── 台账口径：**会漏记**（见 docstring）。报"累计用量"别用这个 ──
            "ledger": {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read": cache,
                "cache_ratio": round(cache / in_tok, 4) if in_tok else 0.0,
                "cost_usd": total_cost,
                "cost_display": _pricing.format_cost(total_cost),
            },
            # ── 归档口径：完整的那把秤。没传 runs_dir ⇒ None（"没查"，不是 0）──
            "archived": archived_usage(runs_dir) if runs_dir is not None else None,
            "path": str(self.root),
        }
