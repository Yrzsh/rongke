# -*- coding: utf-8 -*-
"""「技能门值不值」对照实验 —— 用数据回答"约束会不会把模型套死"。

## 为什么这是现在最该测的一件事

外部证据说这件事**默认是亏的**：
  · arXiv 2608.23067（31 技能 × 50 项目 × 1000 任务，**带长度匹配无关对照**）：
    注入"对的技能"使 mean Pass@2 **降 1.3–4.2%**、token **+72%~394%**，
    只有 17–36% 的〈技能-任务-模型〉组合获益。
  · arXiv 2608.11888：307 例"技能致败"，最大来源是**过度流程 / 过度验证**。

荣珂的写作任务正好落在这个形状里：契约推断出阶段 ⇒ 事前注入装载协议 ⇒
交付时硬门要求 `load_skill` 成功。方向上"宁可漏，别让纪律变成税"的收窄做过，
但**从没和一次长度匹配的对照比过**。没比过，就不知道这门是资产还是税。

## 四臂（唯一的差别只有两处：`contract.stages` 与那一段注入文本）

| 臂 | 阶段/门 | 额外注入 | 它在回答什么 |
|---|---|---|---|
| `full` | 原样（协议注入 + 技能硬门） | 无 | 现状 |
| `no_skill` | `stages=[]` ⇒ 协议不注入、门不要求 | 无 | 把门和协议**一起**去掉，总效应 |
| `text_only` | `stages=[]` ⇒ 门不要求 | **技能全文直接喂进上下文** | 内容的价值（去掉"自己去拿"这个动作） |
| `placebo` | `stages=[]` ⇒ 门不要求 | **等长的无关文本** | 长度匹配对照：多一段字本身值多少 |

三组差分，只有一个才是答案：
    full − no_skill   = 门 + 协议 的净效应（含 token 代价）
    full − placebo    = **扣掉"多一段文本"之后**的净效应 ⇒ 这条说了算
    text_only − full  = "喂给它" 与 "让它自己去装载" 谁更好

## 消融是怎么做的：**不碰门禁一行代码**

`gate.py:448` 的技能门整块包在 `if contract.stages:` 里，
而 `contract.required_skills` 与 `stages.plan_text()` 也都只认 `stages`。
所以"清空 stages"就是天然的开关 —— 它是**契约里的数据**，不是新加的语义。
本实验因此不改动任何一道门，也就不承担"改门禁语义"的代价。

`--self-check` 里专门有一条**开关自己的负控**：同一个 registry、同一份交付声明，
`stages=["draft"]` 必须出现"专属技能还没装载"，`stages=[]` 必须**不出现**。
这条不成立 ⇒ 消融是假的 ⇒ 全部结论作废，脚本直接拒绝开跑。

## 通道：四臂都走同一条拼装路

`Rongke.run(resume_from=...)` 是内核既有的"跨进程续跑"入口
（`_loop` 见 `resume["messages"]` 就不自己装配）。四臂**一律**从它进去，
消息栈由 `loop.initial_messages(contract, ws)` 现算 ——
`full` 臂的栈因而与生产路径逐字节相同（self-check 第 4 条钉着）。
副作用是四臂都不带跨任务经验注入 —— **四臂同向**，且当前经验层只有 2 条人工条目。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import baseline as B                       # noqa: E402
import run_tasks as R                      # noqa: E402  判据引擎复用，不另立
from rongke.contract import TaskContract   # noqa: E402
from rongke.gate import CompletionClaim, CompletionGate   # noqa: E402
from rongke.stages import STAGE_BY_ID, available, read_skill_text   # noqa: E402
from rongke.tools import ToolRegistry      # noqa: E402
from rongke.writing import extract_target_chars   # noqa: E402

ARMS = ("full", "no_skill", "text_only", "placebo")
CONTROL_ARMS = ("no_skill", "text_only", "placebo")
# placebo 取材锚点：README 中段（讲仓库卫生 / _trash 那一片），与"写一段论文引言"无关。
PLACEBO_ANCHOR = 20000


# ═══════════════════════ 注入文本：按**实测成本**等长 ═══════════════════════
def skill_blobs(stage_ids: list[str], cap: int) -> tuple[str, list[str]]:
    """把该任务**被要求装载**的技能全文拼成一段（每条按 `max_observation_chars` 截）。

    为什么按 `cap` 截：模型在 `full` 臂里看到的是工具回灌，而 `_observation`
    会截到 `cfg.loop.max_observation_chars`。对照文本要匹配的是**模型实际吃到的长度**，
    不是磁盘上的长度 —— 匹配错了，实验就变成"长文本 vs 短文本"。
    """
    out, names = [], []
    for sid in stage_ids:
        st = STAGE_BY_ID.get(sid)
        if st is None or st.skill in names:
            continue
        names.append(st.skill)
        txt = read_skill_text(st.skill) or ""
        out.append(txt[:cap])
    return "\n\n".join(out), names


def placebo_of(n_chars: int) -> str:
    """从 README 中段取 n 个字符（不够就循环铺满），保证**恰好等长**。"""
    if n_chars <= 0:
        return ""
    src = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
    body = (src[PLACEBO_ANCHOR:] + src) * (n_chars // max(len(src), 1) + 2)
    return body[:n_chars]


def wrap_skill(name_list: list[str], blob: str) -> str:
    return "【内核已为你装载的阶段技能全文，按它写】\n" + "\n".join(
        f"—— {n} ——" for n in name_list) + "\n" + blob


PLACEBO_HEAD = "【参考材料（与本任务无直接关系，按需忽略）】\n"


def make_placebo(exp: int) -> tuple[str, str]:
    """造一段**总长恰好等于 exp** 的无关文本。返回 (注入全文, 正文哈希)。

    等长是设计成立的前提：不等长就变成"长上下文 vs 短上下文"的实验，
    测不到技能内容本身。这里宁可砍正文，也不让总长差一个字符。
    """
    body = placebo_of(max(0, exp - len(PLACEBO_HEAD)))
    text = PLACEBO_HEAD + body
    if len(text) < exp:                      # 正文不够长时补到等长（README 循环铺满）
        text += "·" * (exp - len(text))
    return text[:exp], hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


# ═══════════════════════════ 一臂的计划 ═══════════════════════════
def bare(c: TaskContract) -> TaskContract:
    """同一条契约，但 `stages` 清空。

    ★ 为什么不能 `replace(c, stages=[])`：`TaskContract.__post_init__`（contract.py:332）
      看到 stages 为空就**从目标文本重新推断**，写作任务还会兜底成 `draft` ——
      构造器根本造不出一份"没有阶段"的契约。所以只能**构造之后再改**。
      这条不写清楚，下一个人一定会用 `replace(..., stages=[])` 然后把"门没关掉"
      当成"门没用"。
    """
    x = replace(c)
    x.stages = []
    return x


@dataclass
class ArmPlan:
    arm: str
    contract: TaskContract
    extra: str = ""
    skills: list[str] = field(default_factory=list)

    @property
    def stage_ids(self) -> list[str]:
        return list(self.contract.stages or [])


def make_plan(arm: str, base: TaskContract, cap: int) -> ArmPlan:
    """从**同一份**冻结契约派生四臂。派生只做声明的那两件事。

    用 `dataclasses.replace` 而不是 `to_dict()/from_dict()`：后者要过一轮
    JSON 与字段过滤，多一条会悄悄改东西的路 —— 实验里"没改的东西"必须
    是**结构上不可能改**的，而不是"我看代码觉得没改"。
    """
    if arm == "full":
        return ArmPlan(arm, base, "",
                       [STAGE_BY_ID[s].skill for s in base.stages if s in STAGE_BY_ID])
    c = bare(base)                                   # ← 唯一的开关：协议 + 硬门
    if arm == "no_skill":
        return ArmPlan(arm, c, "")
    blob, names = skill_blobs(base.stages, cap)      # 长度按 full 臂该吃的量算
    if arm == "text_only":
        return ArmPlan(arm, c, wrap_skill(names, blob), names)
    text, h = make_placebo(len(wrap_skill(names, blob)))
    return ArmPlan(arm, c, text, [f"placebo#{h}"])


# ═════════════════════ 自检：开关有效 + 设计成立（0 次任务运行）═════════════════
def _stub_claim() -> CompletionClaim:
    return CompletionClaim(summary="stub")


def self_check(base: TaskContract, cap: int) -> int:
    """设计不成立就不许开跑。**每条断言都针对一种"实验白做"的死法**。

      ① 别的字段被改了        ⇒ 测的就不是消融，是换任务
      ② 开关其实没换（门照旧）⇒ 四臂长得一样，结论全是噪声
      ③ 长度没匹配            ⇒ 实验退化成"长上下文 vs 短上下文"
      ④ 自己另起一套拼装      ⇒ 测的是我对生产路径的想象
    """
    bad: list[str] = []
    print("═══ 实验设计自检（不跑任务；只花一次契约起草调用）═══")

    if not base.stages:
        bad.append(f"这个 goal 推不出任何阶段（stages=[]）⇒ 没有门可以消融。"
                   f"换一个会被判成写作任务的 goal")
    plans = {a: make_plan(a, base, cap) for a in ARMS}

    # ① 只有声明的两处不同
    for a in CONTROL_ARMS:
        x = plans[a].contract
        for f_ in ("raw_goal", "goal", "deliverables", "acceptance", "constraints",
                   "forbidden", "needs_writing", "needs_figure", "target_chars"):
            if getattr(x, f_, None) != getattr(base, f_, None):
                bad.append(f"① {a} 臂的 `{f_}` 被改了：{getattr(base, f_, None)!r} → "
                           f"{getattr(x, f_, None)!r} —— 那就不是消融")
        if x.stages != []:
            bad.append(f"① {a} 臂的 stages 没清空：{x.stages}")
    print(f"  {'✘' if bad else '✔'} ① 四臂只差在 stages 与那一段注入文本")

    # ② 开关自己的负控：技能门必须在 full 上出现、在清空后消失
    have = available()
    need = [STAGE_BY_ID[s].skill for s in base.stages
            if s in STAGE_BY_ID and STAGE_BY_ID[s].skill in have]
    if not base.stages:
        pass
    elif not need:
        bad.append(f"②本机找不到这些阶段的技能"
                   f"（{[STAGE_BY_ID[s].skill for s in base.stages if s in STAGE_BY_ID]}）"
                   f"⇒ 门只会 warn 不会硬拦，对照无意义")
    else:
        def gate_reasons(stages: list[str]) -> str:
            reg = ToolRegistry()
            reg.start_task()
            cc = replace(base)
            cc.stages = list(stages)          # 构造后改，理由见 `bare()`
            v = CompletionGate(reg).check(_stub_claim(), cc)
            return "\n".join(list(v.reasons) + list(v.warnings))
        KEY = "专属技能"
        on, off = gate_reasons(list(base.stages)), gate_reasons([])
        if KEY not in on:
            bad.append(f"②full 臂的门**没提**技能缺失 ⇒ 这门当前根本没生效，测不到东西")
        if KEY in off:
            bad.append(f"②清空 stages 后门**仍然**要求技能 ⇒ 消融开关无效，全部结论作废")
        print(f"  {'✔' if KEY in on and KEY not in off else '✘'} "
              f"② 门在 full 上硬拦、在其余三臂上消失（要装：{need}）")

    # ③ 长度匹配
    blob, names = skill_blobs(base.stages, cap)
    exp = len(wrap_skill(names, blob))
    got = len(plans["placebo"].extra)
    if not blob.strip():
        bad.append("③技能全文读出来是空的 ⇒ 没有东西可对照")
    elif exp != got:
        bad.append(f"③长度没对上：技能注入 {exp} 字 vs placebo {got} 字")
    else:
        print(f"  ✔ ③ 等长匹配：注入与 placebo 均 {exp} 字（差 0）")

    # ④ 拼装同一条路
    from rongke.loop import initial_messages
    ws = HERE
    j = lambda m: json.dumps(m, ensure_ascii=False, default=str, sort_keys=True)
    if j(initial_messages(base, ws)) != j(initial_messages(plans["full"].contract, ws)):
        bad.append("④full 臂的消息栈与 `loop.initial_messages(起草契约)` 不再逐字节相同"
                   " ⇒ 拼装漂移，测的是想象")
    else:
        print(f"  ✔ ④ full 臂复用 loop.initial_messages，逐字节相同（{len(initial_messages(base, ws))} 条）")

    if bad:
        for e in bad:
            print(f"  ✘ {e}")
        print("\n✘ 设计不成立，**拒绝开跑**。")
        return 3
    print("\n═══ 设计成立：开关有效、长度等配、拼装同源 ✔ ═══")
    return 0


# ═══════════════════════════ 跑一臂 ═══════════════════════════
class InfraFlake(RuntimeError):
    """网关掉线 / 执行中断 —— **不是实验结果，是采集失败**，必须重采。

    为什么不与"任务没过"混在一起：`RemoteDisconnected` 落的样本会让某一臂
    凭空少一个观测，四臂就不配对了；而它看起来和"这一臂真的差"一模一样。
    """


def _tok_from_events(events: list[dict]) -> dict:
    """从事件里**把 token 账现算回来**。

    实测依据：`text_only` 那场被 `RemoteDisconnected` 打断，`result.usage` 记 0，
    但它的事件流里有 2 次真 `llm_done`（input 15,680 + …）。
    崩溃路径把已经烧掉的量记成 0 ⇒ 成本账偏低 —— 与 README 里那次
    「历史累计用量少报 33.5%」同型。这里不动内核，只在测量端按事实重算。
    """
    out = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0, "calls": 0}
    for e in events:
        if e.get("kind") != "llm_done":
            continue
        u = (e.get("data") or {}).get("usage") or {}
        out["calls"] += 1
        for k in ("input_tokens", "output_tokens", "reasoning_tokens"):
            out[k] += int(u.get(k) or 0)
    return out


def _split_checks(arm: str, checks: list[dict]) -> tuple[list[dict], list[dict]]:
    """把判据分成「质量」与「过程」两堆。

    ★ 这条是 pilot 抓出来的**我自己的判据缺陷**：`w1-short-plain` 的判据里含
      `tool_ok load_skill≥1`。对照组是被**故意**拿掉装载义务的，用它来判对照组
      = 同义反复，四臂会永远显示"门有用"。所以凡是被消融机制本身的判据，
      在非 `full` 臂上不进通过判定，只单独列出来给人看。
    """
    loader = set(CompletionGate.LOADER_TOOLS)
    q, p = [], []
    for c in checks:
        if arm != "full" and c.get("kind") == "tool_ok" and c.get("tool") in loader:
            p.append(c)
        else:
            q.append(c)
    return q, p


def run_one(runner, plan: ArmPlan, ws: Path, goal: str, checks: list[dict],
            max_steps: int) -> dict:
    from rongke.loop import initial_messages
    messages = initial_messages(plan.contract, ws)
    if plan.extra:
        messages.append({"role": "user", "content": plan.extra})
    events: list[dict] = []
    t0 = time.time()
    res = runner.run(goal, contract=plan.contract, max_steps=max_steps,
                     on_event=lambda k, d: events.append({"kind": k, "data": d}),
                     workspace=ws,
                     resume_from={"goal": goal, "step": 0, "messages": messages,
                                  "contract": plan.contract.to_dict(),
                                  "t": time.time(), "compacted": {}})
    reason = str(getattr(res, "reason", "") or "")
    if "执行中断" in reason or "LLMError" in reason:
        raise InfraFlake(reason[:160])
    fact = R.Fact.from_result(res, events)
    q_checks, p_checks = _split_checks(plan.arm, checks)
    qrows, meta = R.evaluate(q_checks, fact)
    prows, _ = R.evaluate(p_checks, fact)
    # 注入成本要算**模型真吃到的**：契约文本 + 那一段注入 + 技能工具的回灌
    injected = len(json.dumps(messages, ensure_ascii=False))
    loaded = [t for t in fact.trace if t.get("name") in ("load_skill", "read_skill")]
    tok = _tok_from_events(events)
    return {
        "arm": plan.arm, "task": "", "run_id": res.run_id,
        "passed": R.run_passed(qrows),
        "verdicts": [{"kind": x.kind, "v": x.verdict, "d": x.detail} for x in qrows],
        "process": [{"kind": x.kind, "v": x.verdict, "d": x.detail} for x in prows],
        "outcome": meta["outcome"], "steps": res.steps,
        "input_tokens": tok["input_tokens"], "output_tokens": tok["output_tokens"],
        "reasoning_tokens": tok["reasoning_tokens"], "llm_calls": tok["calls"],
        "usage_as_reported": int((fact.usage or {}).get("input_tokens") or 0),
        "score": (res.lint or {}).get("score") if res.lint else None,
        "draft_chars": len(fact.draft or ""),
        "gate_rejects": sum(1 for p_ in fact.deliver_attempts if p_ is False),
        "skill_loads": len([t for t in loaded if t.get("ok")]),
        "extra_chars": len(plan.extra), "injected_chars": injected,
        "elapsed": round(time.time() - t0, 1),
        "n_tools": len(fact.trace), "reason": reason[:120],
    }


# ═══════════════════════════ 统计：配对 + bootstrap ═══════════════════════════
def boot_ci(diffs: list[float], iters: int = 4000, seed: int = 20260926) -> tuple[float, float]:
    """均值的 bootstrap 95% CI。n 很小 ⇒ 区间会宽得很，**这是要显出来的，不是藏的**。"""
    if not diffs:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    n = len(diffs)
    means = sorted(statistics.fmean(rnd.choices(diffs, k=n)) for _ in range(iters))
    return (round(means[int(0.025 * iters)], 3), round(means[int(0.975 * iters) - 1], 3))


def mcnemar(a: list[bool], b: list[bool]) -> tuple[int, int, float]:
    """配对二项检验（McNemar 精确式）：只数**不一致的那些对**。"""
    n01 = sum(1 for x, y in zip(a, b) if x and not y)     # 实验组过、对照不过
    n10 = sum(1 for x, y in zip(a, b) if not x and y)     # 反过来
    n = n01 + n10
    if n == 0:
        return (n01, n10, 1.0)
    k = min(n01, n10)
    p = sum(math.comb(n, i) for i in range(k + 1)) * 2 / (2 ** n)
    return (n01, n10, round(min(1.0, p), 4))


METRICS = ("steps", "input_tokens", "gate_rejects", "score", "draft_chars")


def compare(recs: list[dict], flakes: dict | None = None) -> list[str]:
    L: list[str] = []
    by: dict[str, list[dict]] = {}
    for r in recs:
        by.setdefault(r["arm"], []).append(r)
    base = by.get("full") or []
    if not base:
        return ["没有 full 臂的样本，无从配对。"]
    L.append("过率 = 只按**质量判据**算（被消融那条机制的判据不进判定，见 `_split_checks`）")
    L.append("臂           n  过率  中位步  中位输入tok  体检分  打回  装载次数  注入字符  中断重采")
    for a in ARMS:
        rs = by.get(a) or []
        if not rs:
            continue
        L.append(f"{a:11s} {len(rs):3d} {sum(1 for r in rs if r['passed'])/len(rs):5.2f}"
                 f"{statistics.median([r['steps'] for r in rs]):8.1f} "
                 f"{statistics.median([r['input_tokens'] for r in rs]):11,.0f} "
                 f"{_med_score(rs):>6}  "
                 f"{statistics.median([r['gate_rejects'] for r in rs]):4.1f}  "
                 f"{statistics.median([r['skill_loads'] for r in rs]):7.1f}  "
                 f"{statistics.median([r['injected_chars'] for r in rs]):8,.0f}"
                 f"      {(flakes or {}).get(a, 0)}")
    L.append("")
    for a in CONTROL_ARMS:
        rs = by.get(a) or []
        if not rs or len(rs) != len(base):
            L.append(f"[full − {a}] 配对不齐（{len(base)} vs {len(rs)}）⇒ 只报均值差，不给检验")
        m = min(len(base), len(rs))
        if m == 0:
            continue
        for k in ("steps", "input_tokens", "injected_chars"):
            d = [base[i][k] - rs[i][k] for i in range(m)]
            lo, hi = boot_ci([float(x) for x in d])
            L.append(f"  {k:14s} 均值差 {statistics.fmean(d):+12,.0f}  "
                     f"bootstrap95%CI [{lo:+,.0f}, {hi:+,.0f}]  n={m}")
        a_p = [base[i]["passed"] for i in range(m)]
        b_p = [rs[i]["passed"] for i in range(m)]
        n01, n10, p = mcnemar(a_p, b_p)
        verdict = ("**分不出来**" if p > 0.05 else
                   ("full 更好" if n01 > n10 else "full 更差"))
        L.append(f"  pass 配对 McNemar：full独过 {n01} / 对照独过 {n10}  p={p}  ⇒ {verdict}"
                 + (f"  ⚠️ n={m} 太小，这个 p 值只能当**方向**看" if m < 12 else ""))
        L.append("")
    L.append("读法：full 的代价是**正值**（多步/多 tok/多打回），收益只有 pass 率；")
    L.append("      而 `placebo` 已经把「多一段字」本身的代价扣掉了 —— 看那一行。")
    return L


def _med_score(rs: list[dict]) -> str:
    v = [r["score"] for r in rs if isinstance(r.get("score"), (int, float))]
    return f"{statistics.median(v):.0f}" if v else "—"


# ⚠️ 一条实测教训（2026-09-26，我把他们绿灯跑红过）：实验若用默认的
#   `Rongke(cfg)`，每次运行都会往产品的共享台账 `memory/runs.jsonl` 追加一条。
#   一天攒下 39 条之后，`web_test.py` 的历史正控被挤出了 `history(50)` 的窗口
#   —— **测试读的是真台账**，于是"用户多跑了几场实验"就能让回归变红。
#   实验属于建材，必须走 `use_memory=False`（内核 loop.py:1482 早就留了这个口子）。

# ════════════════════════════ 入口 ════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂「技能门值不值」四臂对照实验")
    ap.add_argument("--self-check", action="store_true",
                    help="只验设计：开关有效性 + 等长匹配 + 拼装同源（0 次模型调用）")
    ap.add_argument("--pilot", action="store_true", help="1 任务 × 4 臂 × 1 次，量真实成本")
    ap.add_argument("--sweep", action="store_true", help="完整实验（花钱，需 --yes）")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--goal", default="", help="覆盖任务 goal（默认取任务集第一条 write-short）")
    ap.add_argument("--task-id", default="w1-short-plain")
    ap.add_argument("--reps", type=int, default=6, help="每臂重复次数")
    ap.add_argument("--retries", type=int, default=2,
                    help="网关掉线时的重采次数（中断不算实验结果）")
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--stages", default="",
                    help="离线验设计用：合成契约并指定阶段（如 `draft` 或 `discipline,draft`）")
    a = ap.parse_args(argv)

    from rongke.config import Config
    cfg = Config.load()
    tasks_file = HERE / "tasks.jsonl"
    ts, _errs = B.load_tasks(tasks_file)
    task = next((t for t in ts if t.get("id") == a.task_id), None)
    if task is None:
        print(f"✘ 任务集里没有 {a.task_id}")
        return 1
    goal = a.goal or str(task["goal"])
    checks = [dict(c) for c in (task.get("checks") or [])]

    if a.stages:
        # 合成契约：让**设计自检**不依赖网关（真跑仍必须走真起草器，见下面）。
        # 为什么允许：①②③④ 四条断言判的是"消融开关换没换、长度配没配、拼装同不同源"，
        # 与契约内容无关。但报告必须显出来它是合成的 —— 拿合成契约的结论当真跑结论，
        # 就是本项目最反感的那种"把没测的说成测过了"。
        base = TaskContract.fallback(goal)
        base.stages = [x.strip() for x in a.stages.split(",") if x.strip()]
        base.target_chars = __import__("rongke.writing", fromlist=["x"]) \
            .extract_target_chars(goal)
        print(f"⚠️ 契约是**合成**的（fallback + 指定阶段 {base.stages}）——"
              f"只够验设计，不够出结论。真跑请去掉 --stages。")
    elif not cfg.provider.ready:
        print("✘ 网关未就绪，且没给 --stages ⇒ 连契约都起草不了。"
              "要离线验设计：`--stages draft`。")
        return 1
    else:
        from rongke.llm import LLM
        llm = LLM(cfg.provider)
        # 契约**只起草一次**，四臂共享同一个对象。起草两遍就会得到两份不完全一样的
        # 验收标准，那才是本项目最反感的那种"实验自己把自己的尺子换了"。
        base = TaskContract.draft(goal, llm)
    print(f"契约：stages={base.stages}  needs_writing={base.needs_writing}  "
          f"target_chars={base.target_chars}  验收 {len(base.acceptance)} 条")

    cap = int(getattr(cfg.loop, "max_observation_chars", 6000))
    rc = self_check(base, cap)
    if rc != 0 or not (a.pilot or a.sweep):
        return rc

    arms = [x.strip() for x in a.arms.split(",") if x.strip() in ARMS]
    reps = 1 if a.pilot else a.reps
    plans = {arm: make_plan(arm, base, cap) for arm in arms}

    est_runs = len(arms) * reps
    med = 54_000          # 实测 w1-short-plain 7 次的输入 token 中位（见 eval/BASELINE）
    print(f"计划：{len(arms)} 臂 × {reps} 次 = {est_runs} 场；"
          f"按实测中位 {med:,} tok/场 ⇒ 约 {est_runs * med / 1e6:.1f}M input tokens"
          f"（前缀缓存命中会把实付压到远低于这个数，但**本仓没有价格表，算不出美元**）")
    if a.sweep and not a.yes:
        print("✘ 加 --yes 才真跑。先看设计：--self-check（不花钱）")
        return 1

    from rongke.loop import Rongke
    runner = Rongke(cfg, use_memory=False)   # ★ 实验不写 memory/runs.jsonl，见下注
    recs: list[dict] = []
    flakes: dict[str, int] = {}
    for rep in range(1, reps + 1):
        for arm in arms:
            r = None
            for attempt in range(1, a.retries + 2):     # 掉线重采，最多 --retries 次
                ws = runner.make_workspace(tag=f"ablate-{arm}"
                                           + (f"-r{attempt}" if attempt > 1 else ""))
                try:
                    r = run_one(runner, plans[arm], ws, goal, checks,
                                int(task.get("max_steps") or 0) or None)
                    break
                except InfraFlake as e:
                    flakes[arm] = flakes.get(arm, 0) + 1
                    print(f"  · {arm} #{rep} 中断重采（第 {attempt} 次尝试）：{e}")
                except Exception as e:
                    print(f"  · {arm} #{rep} ERROR {type(e).__name__}: {e}")
                    break
            if r is None:
                print(f"  · {arm} #{rep} **放弃**：重采 {a.retries} 次仍被打断 ⇒ "
                      f"这一臂少一个样本，四臂不再配对")
                continue
            r["rep"] = rep
            r["task"] = task["id"]
            recs.append(r)
            (ws / "eval_arm.json").write_text(json.dumps(
                {"arm": arm, "task_id": task["id"], "rep": rep,
                 "injected_chars": r["injected_chars"], "extra_chars": r["extra_chars"],
                 "contract": plans[arm].contract.to_dict()}, ensure_ascii=False),
                encoding="utf-8", newline="\n")
            print(f"  · {arm:10s} #{rep} {'过' if r['passed'] else '不过'} "
                  f"步{r['steps']:3d} 输入{r['input_tokens']:8,d}tok "
                  f"体检{r['score'] or '—'} 打回{r['gate_rejects']} "
                  f"装载{r['skill_loads']} 注入{r['injected_chars']:,}字符")

    text = "\n".join([
        f"═══ 技能门对照实验 · {task['id']} · {time.strftime('%Y-%m-%d %H:%M:%S')} ═══",
        f"goal：{goal}",
        f"四臂差在哪：stages（协议注入 + 硬门）与一段等长文本",
        ""] + compare(recs, flakes))
    print("\n" + text)
    out = HERE / "reports" / f"ablate-{task['id']}-{time.strftime('%Y%m%d-%H%M%S')}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".md").write_text(text + "\n", encoding="utf-8", newline="\n")
    out.with_suffix(".json").write_text(
        json.dumps({"task": task["id"], "goal": goal, "runs": recs}, ensure_ascii=False,
                   indent=1), encoding="utf-8", newline="\n")
    print(f"\n报告：{out}.md / .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
