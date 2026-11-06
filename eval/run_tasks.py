# -*- coding: utf-8 -*-
"""任务执行器 —— 把 `eval/tasks.jsonl` 的判据从「写着」变成「跑过」。

## 为什么缺这一层（2026-09-26 实测，不是推测）

`eval/tasks.jsonl` 有 33 个任务、8 种判据、共 74 条 check。而全仓
`grep tasks.jsonl` 只命中 `eval/baseline.py` 一处，且它只校验这些行的
**JSON 形状**（字段在不在、`checks` 是不是数组，见 baseline.py:421-425）——
**没有任何代码执行过 `chars_range` / `tool_ok` / `numeric_anchor` 其中任何一条。**
`baseline.py` 报的「成功」取自归档里的 `result.success`，而那正是**门自己的自述**。

所以在此之前：
  · 「论文写得好不好」不是测不准，是**没有测**；
  · 每道门在真任务上的命中率与**误报率**无从回答 ——
    「约束会不会把模型套死」这个真问题因此只能辩论，不能收口。

## 判据词汇：PASS / FAIL / NOT AUDITABLE

沿用 `figure.py` 那条 —— **`NOT AUDITABLE`（拿不到事实）≠ `PASS`（判过了）**。
用一个布尔把两者合并就是造一份假证据（M 型缺陷）。
典型场景：工作区已被清场 ⇒ `file_exists` 判不了 ⇒ 报 NA，不许报「文件在」。

## 退出码：把「没测」与「通过」分开

    0 = 样本够，且所有已判 check 全过
    1 = 读不到东西 / 执行器自身出错 / 网关未就绪
    2 = NOT REPORTABLE（任务数或重复数不够 ⇒ 拒绝出数，与 baseline.py 同一口径）
    3 = **仪器自检失败**（负控判不出「坏」⇒ 仪器不可信，此时谈被测对象没有意义）
    4 = 样本够，但有 check 判 FAIL

⚠️ 3 与 4 必须分开：仪器坏了时把一堆 FAIL 报成「通过」，是这类东西最坏的一种死法。

## 三条纪律

1. **口径不许有第二套。** 门槛常量与任务集/归档读取器从 `baseline.py` import；
   字数用 `writing.count_chars`（中文字符 + 西文单词）；数值锚点用
   `tools.extract_anchors`（连它「单位数不作数」的取舍一起继承）。
   **本文件不自己数任何东西。**
2. **昂贵动作必须显式。** 真跑模型要 `--run --yes`，且先报任务数与重复数。
   `--self-test` 与 `--replay` 全程零模型调用 —— 先用它们把仪器跑通。
3. **先证明它能判出「坏」。** `--self-test` 读 `eval/negatives.jsonl`：
   一份已知不合格的交付若被判 PASS ⇒ 退出码 3。另含**承重自检**
   （把判据核心抽掉后自检必须变红 ⇒ 证明那些断言不是恒真）。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ★ 本机控制台默认 GBK，而报告里全是中文与 `·` —— 不换 UTF-8 会**在打印时**抛
#   UnicodeEncodeError：判据全算完了、报告行都生成了，最后死在输出上，
#   看起来像「执行器没跑」。测量工具自己挂掉比被测对象挂掉更糟（同 regress.py）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import baseline as B                              # noqa: E402  口径单源
from rongke.tools import extract_anchors          # noqa: E402
from rongke.writing import count_chars            # noqa: E402

PASS, FAIL, NA = "PASS", "FAIL", "NOT AUDITABLE"
NEGATIVES = HERE / "negatives.jsonl"
REPORT_DIR = HERE / "reports"
SOURCE_EXT = {".csv", ".txt", ".md", ".json", ".tsv", ".tab"}


# ═════════════════════ 一次交付的可判事实 ═════════════════════
@dataclass
class Fact:
    """判据的**唯一输入**。字段必须都是算得出来的事实，不许有模型自己的说法。

    `missing` 记下「哪些事实拿不到」，让每一个 NA 都有出处 ——
    一个不解释的 NA 和一个不解释的 PASS 一样可疑。
    """
    run_id: str = ""
    goal: str = ""
    draft: str = ""
    trace: list[dict] = field(default_factory=list)
    verdict: dict | None = None
    usage: dict | None = None
    workspace: str = ""
    success: bool = False
    reason: str = ""
    deliver_attempts: list[bool] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def no_data(self) -> bool:
        return "result" in self.missing

    # ── 两个入口：从落盘归档 / 从内存里的 RunResult ──
    @classmethod
    def from_session(cls, j: dict) -> "Fact":
        r = j.get("result") or {}
        ev = j.get("events") or []
        f = cls(
            run_id=str(j.get("id") or r.get("run_id") or ""),
            goal=str(j.get("goal") or ""),
            draft=str(r.get("draft") or ""),
            trace=list(r.get("trace") or []),
            verdict=r.get("verdict"),
            usage=r.get("usage"),
            workspace=str(r.get("workspace") or j.get("workspace") or ""),
            success=bool(r.get("success")),
            reason=str(r.get("reason") or ""),
            deliver_attempts=[bool((e.get("data") or {}).get("passed"))
                              for e in ev if e.get("kind") == "deliver_attempt"],
        )
        if not r:
            f.missing.append("result")
        return f

    @classmethod
    def from_result(cls, res, events: list[dict]) -> "Fact":
        u, v = getattr(res, "usage", None), getattr(res, "verdict", None)
        c = getattr(res, "contract", None)
        return cls(
            run_id=str(getattr(res, "run_id", "") or ""),
            goal=str(getattr(c, "raw_goal", "") or getattr(res, "goal", "") or ""),
            draft=str(getattr(res, "draft", "") or ""),
            trace=[t.to_dict() for t in (getattr(res, "trace", []) or [])],
            verdict=v.to_dict() if hasattr(v, "to_dict") else None,
            usage=u.to_dict() if hasattr(u, "to_dict") else None,
            workspace=str(getattr(res, "workspace", "") or ""),
            success=bool(getattr(res, "success", False)),
            reason=str(getattr(res, "reason", "") or ""),
            deliver_attempts=[bool((e.get("data") or {}).get("passed"))
                              for e in events if e.get("kind") == "deliver_attempt"],
        )

    @property
    def outcome(self) -> str:
        """这一场**是怎么收场的** —— 与「判据过没过」分开报。

        为什么必须分开：`gate expect=block` 的任务里，「被门拦下」与
        「步数耗尽根本没走到门口」在 `success=False` 这一个比特上长得一模一样，
        而它们各自说明的东西完全不同。
        """
        if self.success:
            return "delivered_pass"
        if any(p is False for p in self.deliver_attempts):
            return "delivered_blocked"
        if "阻塞" in self.reason:
            return "blocker"
        if "最大步数" in self.reason:
            return "exhausted"
        if "取消" in self.reason or "中断" in self.reason:
            return "cancelled"
        return "unfinished"


# ═════════════════ 工作区解析（清场是「挪」不是「删」）═════════════════
def resolve_workspace(fact: Fact) -> tuple[Path | None, str]:
    """返回 (可读的工作区 | None, 它从哪儿来)。

    先看原路径，再看 `runs/_trash/` —— 本项目的清场纪律是**挪走**
    （README「清场为什么是挪不是删」），所以历史产物大概率还在那儿。
    两处都不在 ⇒ 交 None，让判据报 NA，**不许挑一个路径就当它过了**。
    """
    raw = (fact.workspace or "").strip()
    if not raw:
        fact.missing.append("workspace(空)")
        return None, "空"
    p = Path(raw)
    if p.is_dir():
        return p, "原位"
    cand = ROOT / "runs" / "_trash" / p.name
    if cand.is_dir():
        return cand, "已清场，在 _trash"
    fact.missing.append(f"workspace(不可读:{p.name})")
    return None, "不可读"


# ═══════════════════════ 判据：一种一个纯函数 ═══════════════════════
# 约定：返回 (verdict, detail)。detail 必须带**现场值** ——
#       只说「不合格」的判据等于没判：模型和人都不知道该改哪儿。

def _c_chars_range(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    if not f.draft.strip():
        return NA, "无交付正文（未交付 ≠ 字数不合格）"
    n = count_chars(f.draft)                      # 口径在 writing.py，这里不重数
    lo, hi = int(c.get("min", 0)), int(c.get("max", 10 ** 9))
    return (PASS if lo <= n <= hi else FAIL), f"{n} 字，要求 [{lo}, {hi}]"


def _tool_counts(f: Fact, name: str) -> tuple[int, int]:
    ok = sum(1 for t in f.trace if t.get("name") == name and t.get("ok"))
    return ok, sum(1 for t in f.trace if t.get("name") == name)


def _c_tool_ok(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    if f.no_data:
        return NA, "归档里没有 result ⇒ 台账不可读"
    name, need = str(c.get("tool", "")), int(c.get("min", 1))
    ok, allc = _tool_counts(f, name)
    got = f"{name} 成功 {ok} 次" + (f"（另 {allc - ok} 次失败）" if allc > ok else "")
    return (PASS if ok >= need else FAIL), f"{got}，要求 ≥{need}"


def _c_tool_absent(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    name = str(c.get("tool", ""))
    ok, allc = _tool_counts(f, name)
    if ok:
        return FAIL, f"{name} 成功了 {ok} 次，而契约禁止它"
    if allc:
        # 试了但没成：没做成被禁的事 ⇒ PASS，但**这件事必须能被看见**（它不干净）。
        return PASS, f"{name} 零成功（试过 {allc} 次均失败）"
    return PASS, f"{name} 未被调用"


def _c_gate(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    exp = str(c.get("expect", "pass"))
    passed = bool((f.verdict or {}).get("passed"))
    if exp == "pass":
        if not passed:
            return FAIL, f"未通过完成门（verdict.passed={passed}，收场={f.outcome}）"
        return PASS, "交付通过了完成门"
    if exp == "block":
        if passed:
            return FAIL, "该拦没拦：交付通过了完成门"
        if f.deliver_attempts:
            return PASS, f"{len(f.deliver_attempts)} 次交付尝试全被打回"
        if f.outcome == "blocker":
            return PASS, "诚实通道：走了 report_blocker 且未宣称完成"
        # 「没交付」不等于「门把它拦下了」。耗尽/取消时门根本没被敲过 ——
        # 在这里判 PASS 就是拿「没检查」渲染成「合格」（M 型）。
        return NA, f"门未被敲过，无从判定它拦不拦得住（收场={f.outcome}）"
    return NA, f"未知 expect：{exp}"


def _c_file_exists(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    ws, src = ctx["ws"], ctx["ws_src"]
    if ws is None:
        return NA, f"工作区{src} ⇒ 文件在不在无从核对"
    rel = str(c.get("path", "")).replace("\\", "/").lstrip("/")
    if (ws / rel).exists():
        return PASS, f"{rel} 在（{src}）"
    return FAIL, f"{rel} 不在 {ws}"


def _nums_of(text: str) -> list[str]:
    return list((extract_anchors(text) or {}).get("num") or [])


def _c_numeric_anchor(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    """正文里**每个被量出来的数**都要能在源数据里逐字命中。

    刻意复用 `tools.extract_anchors` 而不是在这里重写一遍数值正则：
    它自带那条实测教训 —— **单位数不作数**（否则「1..6」的编号清单会把判据
    退化成恒真，而一个拦不住东西的检查比没有检查更糟）。
    """
    if not f.draft.strip():
        return NA, "无交付正文"
    rel = str(c.get("source", "")).strip("/\\").replace("\\", "/")
    ws = ctx["ws"]
    d = (ws / rel) if (ws is not None and rel) else None
    if d is None or not d.is_dir():
        return NA, f"源数据目录不可读：{rel or '(check 里没给 source)'}"
    src, nfiles = set(), 0
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.suffix.lower() in SOURCE_EXT:
            nfiles += 1
            src |= set(_nums_of(p.read_text(encoding="utf-8", errors="replace")))
    want = set(_nums_of(f.draft))
    if not want:
        return NA, "正文里没有可核对的数值锚点（单位数不计，口径见 extract_anchors）"
    if not src:
        return NA, f"源数据 {nfiles} 个文件里一个数值都没有 ⇒ 判不了，不是「免检」"
    miss = sorted(want - src)
    return ((PASS if not miss else FAIL),
            f"正文数值 {len(want)} 个、源自 {nfiles} 个文件"
            + (f"；追不到证据的：{miss[:8]}" if miss else "；全部可追"))


def _c_content_absent(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    """**字面子串，不是正则**（任务 a6 要考的就是别把 DOI 改写成路径）。"""
    needle = str(c.get("contains", ""))
    if not needle:
        return NA, "check 里没有 contains"
    if not f.draft.strip():
        return NA, "无交付正文"
    hit = needle in f.draft
    return ((FAIL if hit else PASS),
            f"字面串 {needle!r} {'出现在正文里' if hit else '未出现在正文里'}")


def _c_input_tokens_max(c: dict, f: Fact, ctx: dict) -> tuple[str, str]:
    if f.usage is None:
        return NA, "归档里没有 usage"
    got = int((f.usage or {}).get("input_tokens") or 0)
    if not got:
        return NA, "网关未回 input_tokens ⇒ 判不了，不是「免检」"
    n = int(c.get("n", 0) or 0)
    return ((PASS if got <= n else FAIL), f"整场输入 {got} tokens，上限 {n}")


CHECKS: dict = {
    "chars_range": _c_chars_range,
    "tool_ok": _c_tool_ok,
    "tool_absent": _c_tool_absent,
    "gate": _c_gate,
    "file_exists": _c_file_exists,
    "numeric_anchor": _c_numeric_anchor,
    "content_absent": _c_content_absent,
    "input_tokens_max": _c_input_tokens_max,
}
_CHECKS0 = dict(CHECKS)          # 承重自检要在事后原样装回去


@dataclass
class Row:
    kind: str
    spec: dict
    verdict: str
    detail: str


def evaluate(checks: list[dict], fact: Fact) -> tuple[list[Row], dict]:
    ws, src = resolve_workspace(fact)
    ctx = {"ws": ws, "ws_src": src}
    rows: list[Row] = []
    for c in checks or []:
        kind = str(c.get("kind", ""))
        fn = CHECKS.get(kind)
        if fn is None:
            # 未知判据**不许混进 PASS** —— 那会把「没实现」渲染成「已核对」。
            v, d = NA, "执行器没有这条判据的实现（不许当已通过）"
        else:
            v, d = fn(c, fact, ctx)
        rows.append(Row(kind, c, v, d))
    tally = {k: sum(1 for r in rows if r.verdict == k) for k in (PASS, FAIL, NA)}
    return rows, {"tally": tally, "ws_src": src, "outcome": fact.outcome}


def run_passed(rows: list[Row]) -> bool:
    """一次运行算「过」当且仅当：**没有 FAIL 也没有 NA**。

    NA 不算过 —— 这条是整套仪器的立身之处：把「没检查」渲染成「合格」
    就是本项目最反感的 M 型缺陷。代价是报告里会显出一堆「没测到」，那是诚实。
    """
    return bool(rows) and all(r.verdict == PASS for r in rows)


# ════════════════════ 任务 ↔ 归档 的认领 ════════════════════
def link_tasks(tasks_file: Path) -> dict:
    """规范化 goal → 任务。**只认精确串**，对不上就不认领。

    为什么不 fuzzy：把一次无关的运行认领成某任务的样本，等于**凭空造分母**，
    而分母正是这套仪器的全部意义。宁可漏，不可错。
    """
    ts, errs = B.load_tasks(tasks_file)
    for e in errs:
        print(f"⚠️ 任务集：{e}")
    return {B._norm_goal(str(t.get("goal") or "")): t for t in ts if t.get("goal")}


# ════════════════════ 模式一：离线回放（零成本）════════════════════
def replay(runs_dir: Path, tasks_file: Path, since: str = "") -> tuple[list, list]:
    recs, unmatched = [], []
    by_goal = link_tasks(tasks_file)
    by_id = {str(t.get("id")): t for t in by_goal.values()}
    if not runs_dir.is_dir():
        return recs, [f"归档根不存在：{runs_dir}"]
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir() and not p.name.startswith("_")):
        if since and d.name[:8] < since:
            continue
        f = d / "session.json"
        if not f.exists():
            unmatched.append(f"{d.name}：没有 session.json（不是可回放的归档）")
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            unmatched.append(f"{d.name}：session.json 读不动（{type(e).__name__}）")
            continue
        # 认领优先看 sidecar（--run 落下的凭据），再看 goal 精确匹配
        tid = ""
        sd = d / "eval_task.json"
        if sd.exists():
            try:
                tid = str(json.loads(sd.read_text(encoding="utf-8")).get("task_id") or "")
            except Exception:
                tid = ""
        t = by_goal.get(B._norm_goal(str(j.get("goal") or "")))
        task = by_id.get(tid) or t
        if task is None:
            unmatched.append(f"{d.name}：goal 对不上任务集 ⇒ 不认领、不猜、不并入分母")
            continue
        if tid and t and tid != t.get("id"):
            unmatched.append(f"{d.name}：sidecar 写 {tid} 而 goal 对到 {t.get('id')} ⇒ 以 sidecar 为准")
        fact = Fact.from_session(j)
        rows, meta = evaluate([dict(c) for c in (task.get("checks") or [])], fact)
        recs.append({"run_id": d.name, "task_id": str(task.get("id")), "rows": rows,
                     "meta": meta, "fact": fact,
                     "outcome_expected": _expect_outcome(task)})
    return recs, unmatched


def _expect_outcome(task: dict) -> str:
    """任务里有没有 `gate expect=block` —— 用来识别「 adversarial 任务」。"""
    return "block" if any(str(c.get("kind")) == "gate" and str(c.get("expect")) == "block"
                          for c in (task.get("checks") or [])) else "pass"


# ════════════════════ 模式二：真跑（贵，两次确认）════════════════════
def prepare_ws(fixture_root: Path, task_id: str) -> tuple[Path | None, str]:
    src = fixture_root / task_id
    if not src.is_dir():
        return None, f"缺材料目录 eval/fixtures/{task_id}/"
    ws = Path(tempfile.mkdtemp(prefix=f"rongke-eval-{task_id}-"))
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        dst = ws / p.relative_to(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(p.read_bytes())
    return ws, "材料已就位"


def run_tasks(tasks: list[dict], repeat: int, cfg, fixture_root: Path) -> list:
    from rongke.loop import Rongke          # 延迟 import：离线模式不该把内核拉起来
    recs = []
    for t in tasks:
        checks = [dict(c) for c in (t.get("checks") or [])]
        for k in range(1, repeat + 1):
            if t.get("status") == "needs-fixture":
                ws, why = prepare_ws(fixture_root, t["id"])
                if ws is None:
                    print(f"  · {t['id']} #{k} SKIPPED：{why}（明着跳，不静默）")
                    continue
            else:
                ws = None
            events: list[dict] = []
            try:
                res = Rongke(cfg, use_memory=False).run(
                    str(t.get("goal") or ""),
                    max_steps=int(t.get("max_steps") or 0) or None,
                    on_event=lambda kind, data: events.append({"kind": kind, "data": data}),
                    workspace=ws)
            except Exception as e:
                print(f"  · {t['id']} #{k} ERROR：{type(e).__name__}: {e}")
                continue
            fact = Fact.from_result(res, events)
            rows, meta = evaluate(checks, fact)
            _write_sidecar(res, t["id"], k, checks)
            print(f"  · {t['id']} #{k} {meta['outcome']:17s} "
                  f"P{sum(1 for r in rows if r.verdict == PASS)}"
                  f"/F{sum(1 for r in rows if r.verdict == FAIL)}"
                  f"/N{sum(1 for r in rows if r.verdict == NA)}"
                  f"  步{res.steps}  输入{int((fact.usage or {}).get('input_tokens') or 0)}tok")
            recs.append({"run_id": res.run_id, "task_id": t["id"], "rows": rows,
                         "meta": meta, "fact": fact,
                         "outcome_expected": _expect_outcome(t)})
    return recs


def _write_sidecar(res, task_id: str, k: int, checks: list[dict]) -> None:
    """把「这一场属于哪个任务」落成盘上的凭据。

    为什么不能只靠 goal 字符串：任务集会改，改了之后旧归档就再也对不上；
    而 --replay 要能**离线重算**，就得有一份不随任务集漂移的认领记录。
    """
    try:
        d = Path(getattr(res, "workspace", "") or "")
        if not d.is_dir():
            return
        (d / "eval_task.json").write_text(
            json.dumps({"task_id": task_id, "repeat": k, "checks": checks,
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False),
            encoding="utf-8", newline="\n")
    except Exception:
        pass


# ═════════════════════ pass^k：同任务次次过才算过 ═════════════════════
def pass_k(recs: list) -> dict:
    by: dict[str, list[bool]] = {}
    for r in recs:
        by.setdefault(r["task_id"], []).append(run_passed(r["rows"]))
    n = len(recs)
    full = {k: v for k, v in by.items() if len(v) >= B.MIN_REPEATS}
    kmin = min((len(v) for v in full.values()), default=0)
    return {"n_runs": n, "n_tasks": len(by),
            "pass@1": round(sum(x for v in by.values() for x in v) / n, 3) if n else None,
            "k": kmin,
            "pass^k": (round(sum(1 for v in full.values() if all(v)) / len(full), 3)
                       if full else None),
            "per_task": {t: [int(x) for x in v] for t, v in sorted(by.items())}}


# ═════════════════ 模式三：仪器自检（正控/负控/承重）═════════════════
def load_negatives(path: Path) -> tuple[list[dict], list[str]]:
    out, errs = [], []
    if not path.exists():
        return out, [f"负控集不存在：{path}"]
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            d = json.loads(line)
        except Exception as e:
            errs.append(f"negatives 第 {i} 行坏了：{e}")
            continue
        for k in ("id", "expect", "check", "fact"):
            if k not in d:
                errs.append(f"negatives 第 {i} 行缺字段 {k}")
        out.append(d)
    return out, errs


def _fact_from_spec(spec: dict, root: Path) -> Fact:
    """把负控条目落成 Fact。文件类判据**真的要落盘**再判 ——
    对着内存里的假设判 `file_exists`，判的是我的想象不是代码。"""
    f = Fact(
        run_id=str(spec.get("run_id", "neg")), goal=str(spec.get("goal", "")),
        draft=str(spec.get("draft", "")), trace=list(spec.get("trace") or []),
        verdict=spec.get("verdict"), usage=spec.get("usage"),
        success=bool(spec.get("success")), reason=str(spec.get("reason", "")),
        deliver_attempts=list(spec.get("deliver_attempts") or []),
    )
    ws = spec.get("workspace")
    if ws == "TEMP":
        p = root / str(spec.get("_dir", "w"))
        p.mkdir(parents=True, exist_ok=True)
        for rel, body in (spec.get("files") or {}).items():
            q = p / rel
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text(str(body), encoding="utf-8", newline="\n")
        f.workspace = str(p)
    elif ws == "GONE":
        f.workspace = str(root / "does-not-exist")
    else:
        f.workspace = str(ws or "")
    return f


def _sweep(negs: list[dict]) -> tuple[bool, list[str]]:
    """把负控集全判一遍。返回 (是否全中, 不中的说明)。"""
    miss = []
    with tempfile.TemporaryDirectory(prefix="rongke-eval-selftest-") as td:
        root = Path(td)
        for i, n in enumerate(negs):
            spec = dict(n["fact"])
            spec["_dir"] = f"d{i}"
            rows, _ = evaluate([dict(n["check"])], _fact_from_spec(spec, root))
            got = rows[0].verdict if rows else NA
            if got != n["expect"]:
                miss.append(f"{n['id']}：期望 {n['expect']} / 实得 {got}"
                            f"（{rows[0].detail if rows else '无输出'}）")
    return (not miss), miss


def self_test(verbose: bool = True) -> int:
    """**退出码 3 = 仪器不可信。** 四类断言缺一不可：

      · 正控：已知合格的必须判 PASS —— 判成 FAIL 就是误报，而误报会教人无视报告
      · 负控：已知不合格的必须判 FAIL/NA —— 判成 PASS 就是瞎
      · 边界：拿不到事实时必须报 NA，不许滑成 PASS
      · 承重：把判据核心抽掉后自检**必须变红** —— 否则上面那些绿勾是恒真换来的
    """
    negs, errs = load_negatives(NEGATIVES)
    bad = list(errs)
    if not negs:
        print("✘ 负控集为空 ⇒ 无法自检，按仪器不可信处理")
        return 3
    covered = {str(n["check"].get("kind")) for n in negs}
    un = sorted(set(CHECKS) - covered)
    if un:
        bad.append(f"这些判据一条负控都没有，等于没测过：{un}")

    if verbose:
        for n in negs:
            print(f"  · [{n['id']}] 期望 {n['expect']:14s} {n.get('why', '')}")
    hit, miss = _sweep(negs)
    print(f"正控/负控/边界：{'全部命中 ✔' if hit else '有 %d 条不中 ✘' % len(miss)}"
          f"（共 {len(negs)} 条）")
    for m in miss:
        print(f"      ✘ {m}")
    if not hit:
        bad.append(f"{len(miss)} 条自检断言不中 ⇒ 判据与预期不符")

    # ── 承重自检：逐个抽掉判据核心，上面的断言必须变红 ──
    killers = {
        "chars_range 恒真": lambda c, f, ctx: (PASS, "被抽空"),
        "gate 把「没走到门口」当拦住": lambda c, f, ctx: (PASS, "被抽空"),
        "数值锚点全收": lambda c, f, ctx: (PASS, "被抽空"),
        "file_exists 不看盘": lambda c, f, ctx: (PASS, "被抽空"),
    }
    for name, fn in killers.items():
        CHECKS.clear()
        CHECKS.update(_CHECKS0)
        CHECKS[fn_key(name)] = fn
        try:
            still_green, _ = _sweep(negs)
        finally:
            CHECKS.clear()
            CHECKS.update(_CHECKS0)
        if still_green:
            bad.append(f"承重自检失败：抽掉「{name}」后自检**照样全绿** ⇒ 那条断言是恒真")
        elif verbose:
            print(f"  ✔ 承重：抽掉「{name}」→ 自检变红（断言真的依赖实现）")

    if (B.MIN_TASKS, B.MIN_REPEATS) != (30, 3):
        bad.append(f"口径常量被改动：MIN_TASKS={B.MIN_TASKS} MIN_REPEATS={B.MIN_REPEATS}")
    print(f"口径：分母=不同任务数 ≥{B.MIN_TASKS}、每任务 ≥{B.MIN_REPEATS} 次"
          f"（从 baseline.py import，这里不另立一套）")

    if bad:
        print("\n═══ 自检未通过：仪器不可信（退出码 3）═══")
        for e in bad:
            print(f"  ✘ {e}")
        return 3
    print("\n═══ 仪器自检通过 ✔ 判据能判出「坏」，且断言有承重 ═══")
    return 0


_KILL_KEYS = {"chars_range 恒真": "chars_range",
              "gate 把「没走到门口」当拦住": "gate",
              "数值锚点全收": "numeric_anchor",
              "file_exists 不看盘": "file_exists"}


def fn_key(name: str) -> str:
    return _KILL_KEYS[name]


# ════════════════════════════ 报告 ════════════════════════════
def render(recs: list, unmatched: list[str], mode: str, pk: dict) -> str:
    L = [f"═══ 荣珂任务执行器 · {mode} · {time.strftime('%Y-%m-%d %H:%M:%S')} ═══",
         f"口径：分母=不同任务数 ≥{B.MIN_TASKS} 且每任务 ≥{B.MIN_REPEATS} 次（与 baseline.py 同源）",
         "判定：PASS / FAIL / NOT AUDITABLE —— **NA 不算通过**（拿不到事实 ≠ 已核对）", ""]
    if not recs:
        L.append("没有任何可评的运行。**这不是「全过」，是「没测」。**")
        if unmatched:
            L.append(f"未认领的归档 {len(unmatched)} 个：")
            L += [f"  · {u}" for u in unmatched[:15]]
        return "\n".join(L)

    by_task: dict[str, list] = {}
    for r in recs:
        by_task.setdefault(r["task_id"], []).append(r)
    tally = {k: 0 for k in (PASS, FAIL, NA)}
    n_bad = 0
    for tid in sorted(by_task):
        rs = by_task[tid]
        ok = [run_passed(r["rows"]) for r in rs]
        n_bad += (not all(ok))
        L.append(f"[{tid}] {len(rs)} 次 · 次次全过 {sum(ok)}/{len(rs)} · "
                 f"收场 {sorted({r['meta']['outcome'] for r in rs})}")
        for r in rs:
            for row in r["rows"]:
                tally[row.verdict] = tally.get(row.verdict, 0) + 1
                if row.verdict != PASS:
                    spec = json.dumps({k: v for k, v in row.spec.items() if k != "kind"},
                                      ensure_ascii=False)
                    L.append(f"    {r['run_id']} {row.verdict:14s} "
                             f"{row.kind}{{{spec}}} → {row.detail}")
            if r["fact"].missing:
                L.append(f"    {r['run_id']} 拿不到的事实：{r['fact'].missing}")
        L.append("")
    L.append(f"check 级合计：PASS {tally[PASS]} / FAIL {tally[FAIL]} / NA {tally[NA]}")
    L.append(f"任务级：{len(by_task)} 个任务里 {n_bad} 个没做到次次全过")
    L.append("pass@1 = %s ；k=%s ⇒ pass^k = %s"
             % (pk["pass@1"], pk["k"],
                pk["pass^k"] if pk["pass^k"] is not None
                else f"不可报（没有任务重复满 {B.MIN_REPEATS} 次）"))
    if unmatched:
        L.append(f"未认领的归档 {len(unmatched)} 个（不猜、不并入分母）：")
        L += [f"  · {u}" for u in unmatched[:15]]
    return "\n".join(L)


def save_report(text: str, mode: str, obj: dict | None = None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    p.write_text(text + "\n", encoding="utf-8", newline="\n")   # ★ LF，见 .gitattributes
    if obj is not None:
        (p.with_suffix(".json")).write_text(
            json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return p


# ⚠️ 一条实测教训（2026-09-26，我把他们绿灯跑红过）：实验若用默认的
#   `Rongke(cfg)`，每次运行都会往产品的共享台账 `memory/runs.jsonl` 追加一条。
#   一天攒下 39 条之后，`web_test.py` 的历史正控被挤出了 `history(50)` 的窗口
#   —— **测试读的是真台账**，于是"用户多跑了几场实验"就能让回归变红。
#   实验属于建材，必须走 `use_memory=False`（内核 loop.py:1482 早就留了这个口子）。

# ════════════════════════════ 入口 ════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂任务执行器：把 eval/tasks.jsonl 的判据跑起来")
    ap.add_argument("--self-test", action="store_true",
                    help="离线自检（正控/负控/边界/承重），不花钱 —— 先跑这个")
    ap.add_argument("--replay", action="store_true", help="用既有归档离线重算判据（零模型调用）")
    ap.add_argument("--run", action="store_true", help="真跑任务（要花钱，需再配 --yes）")
    ap.add_argument("--yes", action="store_true", help="确认真跑会花钱")
    ap.add_argument("--only", default="", help="只跑这些任务 id（逗号分隔）")
    ap.add_argument("--repeat", type=int, default=B.MIN_REPEATS,
                    help="每任务重复次数（默认取 baseline.MIN_REPEATS=%d）" % B.MIN_REPEATS)
    ap.add_argument("--ready-only", action="store_true",
                    help="只跑 status=ready 的任务（needs-fixture 明着跳过，不静默）")
    ap.add_argument("--since", default="", help="--replay 只看某天（YYYYMMDD）之后的归档")
    ap.add_argument("--tasks", default=str(HERE / "tasks.jsonl"))
    ap.add_argument("--quiet", action="store_true", help="自检只报不中项")
    ap.add_argument("--json", action="store_true", help="报告旁边再落一份机器读的 JSON")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test(verbose=not a.quiet)

    if not (a.replay or a.run):
        print("选一个模式：--self-test（自检）· --replay（离线重算）· --run --yes（真跑）")
        return 1

    tasks_file = Path(a.tasks)
    ts, errs = B.load_tasks(tasks_file)
    for e in errs:
        print(f"⚠️ 任务集：{e}")

    from rongke.config import Config
    cfg = Config.load()

    recs: list = []
    unmatched: list[str] = []
    if a.replay:
        recs, unmatched = replay(cfg.runtime.workspace_dir, tasks_file, since=a.since)
        mode = "replay"
    if a.run:
        if not cfg.provider.ready:
            print("✘ 网关未就绪（base_url / model / API Key 环境变量不全），跑不了。"
                  "离线请用 --replay 或 --self-test。")
            return 1
        sel = [t for t in ts if not a.only or t.get("id") in {x.strip() for x in a.only.split(",")}]
        if a.ready_only:
            sk = [t["id"] for t in sel if t.get("status") != "ready"]
            sel = [t for t in sel if t.get("status") == "ready"]
            if sk:
                print(f"明着跳过 {len(sk)} 个待备材料的任务：{sk}")
        if not a.yes:
            print(f"✘ 真跑要花钱：{len(sel)} 个任务 × {a.repeat} 次，"
                  f"步数上限合计 ≈{sum(int(t.get('max_steps') or 40) for t in sel) * a.repeat}。"
                  f"确认后再加 --yes。（离线不花钱的路子：--replay / --self-test）")
            return 1
        mode = "run+replay" if a.replay else "run"
        print(f"真跑：{len(sel)} 个任务 × {a.repeat} 次")
        recs += run_tasks(sel, a.repeat, cfg, HERE / "fixtures")
        if not recs and not a.replay:
            print("一场都没跑成 ⇒ 拒绝出数。")
            return 1

    pk = pass_k(recs)
    text = render(recs, unmatched, mode, pk)
    print("\n" + text)
    obj = {"mode": mode, "pass_k": pk, "unmatched": unmatched,
           "runs": [{"run_id": r["run_id"], "task_id": r["task_id"],
                     "outcome": r["meta"]["outcome"], "passed": run_passed(r["rows"]),
                     "missing": r["fact"].missing,
                     "checks": [{"kind": x.kind, "spec": x.spec, "verdict": x.verdict,
                                 "detail": x.detail} for x in r["rows"]]}
                    for r in recs]}
    p = save_report(text, mode, obj if a.json else None)
    print(f"\n报告：{p}" + (f"\nJSON ：{p.with_suffix('.json')}" if a.json else ""))

    n_tasks = len({r["task_id"] for r in recs})
    enough = n_tasks >= B.MIN_TASKS and pk["k"] >= B.MIN_REPEATS
    any_fail = any(not run_passed(r["rows"]) for r in recs)
    if not recs:
        print("没有可评的运行 ⇒ 拒绝出数（2）")
        return 2
    if not enough:
        print(f"\n样本不够：{n_tasks} 个任务、k={pk['k']}"
              f"（门槛 ≥{B.MIN_TASKS} / ≥{B.MIN_REPEATS}）⇒ **NOT REPORTABLE（2）**。"
              f"上面的数字只能当个案回放看。")
        return 2
    print("\n═══ 可报（样本达到门槛）═══")
    return 4 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
