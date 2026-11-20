# -*- coding: utf-8 -*-
"""开题大纲（outline）：把「计划做什么实验」和「给论文哪一节填什么数据」钉成一份可对账的东西。

## 它补的是哪个洞

课题台账（`topic.py`）有三份自由文本：PLAN / READING / EVIDENCE。它们能装下计划，
但**没有任何一处强制回答"这句话靠哪个数据成立"**。于是真实使用里长出来的是：
大纲是散文，实验记录是另一份散文，论文正文是第三份散文 —— 三者对不上时
没有任何东西会报错，直到开题答辩被问「这一段的数据在哪」。

本模块只做一件事：**让「要写什么」和「要做什么」之间必须有一条连线。**

    topics/<slug>/
      OUTLINE.md        # 给人读的大纲（自动生成，别手改 —— 改了会被下一次覆盖）
      outline.json      # 给机器对账的结构（单源，OUTLINE.md 由它渲染）

## ★ 四条设计纪律

① **数据需求必须指到章节，实验必须指到数据需求。**
   一个孤儿实验（`feeds=[]`）意味着"做了但不知道写进哪"，一个孤儿数据需求
   （没有任何实验供数）意味着"这一节现在写不出来"。两者都不许隐藏。

② **`status` 是声明，不是事实。** 实验到底做没做，事实在实验台账（`lab.py`）里，
   不在大纲里。所以 `planned→done` 不许由大纲自己说 —— 对账（`reconcile`）会拿
   台账里**真有的**测量去核，核不上就标 `unverified`，并在回包里说出来。
   这与导师模型那条 `source=` 纪律同源：**能核的必须核，核不了的要标出来**。

③ **不留孤儿数据。** 用户删一章，挂在它下面的数据需求不能凭空消失 ——
   那正是「论文少了一节但没人发现」。`save` 保留它们并标记 `orphan=True`，
   由对账说清楚，让用户决定是删还是改挂。

④ **不做模板填空题。** 四类章节（引言/方法/结果/讨论）的骨架只用来
   **检查"有没有漏掉一整类"**，不用来自动填充 —— 自动填满等于替用户编大纲，
   与导师模型那次「模板样例被当成导师原话」是同一种错。

## 为什么不是主题目里的一行 JSON

`topic.json` 已经用「未知字段原样保留」支持扩展，理论上也能塞进去。
但那会让每次 `read_topic` 都把整份大纲搬进内存，而大纲最多几十节、
每节带若干条数据需求 —— 它有自己的读写节奏（起草时反复改，定稿后基本不动）。
单开一份文件，`topic.json` 的语义不变，老课题不用迁移。

## 为什么 OUTLINE.md 是生成的

人要看它、要粘进开题报告；机器要对账。两份各自维护必然漂移，
而漂移之后**没有任何东西会报错**（本仓最贵的那类失效）。
所以结构是单源，Markdown 只是视图：改结构 → 重渲染。
手改 OUTLINE.md 会在下一次 `save` 被覆盖 —— 文件头写明了。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import topic as _T

#: 章节类型。只用来**查漏**（四类里有没有整类缺失），不用来填内容。
SECTION_KINDS = {
    "intro": "引言 / 研究背景",
    "method": "方法",
    "result": "结果",
    "discussion": "讨论",
}

#: 数据形态：这一节要的是**什么形状**的东西。
DATA_KINDS = {
    "table": "表格（多组可对比）",
    "figure": "图（趋势/分布/结构）",
    "stat": "数字（一个能写进正文的量）",
    "image": "图像（显微/电泳/形态）",
    "text": "定性描述（可以没有数，但要有出处）",
}

#: 数据需求的状态。★ `done` 必须经对账认可，不能由大纲自己声明。
DATA_STATUS = {
    "missing": "还没有",
    "planned": "已排进实验",
    "have": "已有数据（未核对）",
    "done": "已核对：台账里真有",
    "unverified": "自称已有，但台账里查不到",
}

#: 实验状态。同上：`done` 只是**声明的**完成，事实看台账。
EXP_STATUS = {"planned": "计划中", "running": "进行中", "done": "已做完（声明）",
              "dropped": "放弃"}

#: 大纲规模上限。不是审美 —— 超过之后模型与人都不会再读它，
#: 那份大纲就退化成"存着但没人看"，与本仓反复记过的空壳同型。
MAX_SECTIONS = 60
MAX_EXPERIMENTS = 80
MAX_NEEDS = 12            # 单节的数据需求条数
MAX_TITLE = 160
MAX_TEXT = 2000
MAX_ID = 64

OUTLINE_FILE = "outline.json"
OUTLINE_MD = "OUTLINE.md"
SCHEMA_VERSION = 1

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _d(slug: str, root=None) -> Path | None:
    """课题目录；非法或越界一律 None（与 `topic._topic_dir` 同一纪律）。"""
    if not _T.valid_slug(str(slug or "")):
        return None
    try:
        d = _T.topic_dir(str(slug), root=root)
    except Exception:                                   # noqa: BLE001
        return None
    base = Path(_T.topics_root(root)).resolve()
    try:
        p = Path(d).resolve()
    except Exception:                                   # noqa: BLE001
        return None
    if p == base or base not in p.parents:
        return None
    return p


def check_id(s: str, what: str = "id") -> str:
    """内部 id 只收字母/数字/下划线/短横。中文标题放 `title`，不进 id。

    为什么要限制：id 会进 Markdown 锚点、进对账输出、进实验台账的 `feeds`，
    放宽到任意字符之后，任何一个空格或全角括号都会让两处对不上，
    而对不上时**不会报错，只会静默判成"没有实验供数"**。
    """
    t = str(s or "").strip()
    if not _ID_RE.match(t):
        raise ValueError(f"{what} 只能用字母/数字/下划线/短横（≤64）：{t!r}")
    return t


# ── 结构校验 ────────────────────────────────────────────────
def _clean_need(n: dict, where: str) -> dict:
    if not isinstance(n, dict):
        raise ValueError(f"{where}：数据需求必须是对象")
    kind = str(n.get("kind") or "").strip()
    if kind not in DATA_KINDS:
        raise ValueError(f"{where}：数据形态必须是 {'/'.join(DATA_KINDS)}，收到 {kind!r}")
    what = str(n.get("what") or "").strip()
    if not what:
        raise ValueError(f"{where}：必须写清「这一节要的是什么数据」（what 不能为空）")
    if len(what) > MAX_TEXT:
        raise ValueError(f"{where}：what 超过 {MAX_TEXT} 字")
    st = str(n.get("status") or "missing").strip()
    if st not in DATA_STATUS:
        raise ValueError(f"{where}：status 必须是 {'/'.join(DATA_STATUS)}，收到 {st!r}")
    out = {"kind": kind, "what": what, "status": st,
           "unit": str(n.get("unit") or "").strip()[:64],
           "note": str(n.get("note") or "").strip()[:MAX_TEXT]}
    # 「用哪个数算出来的」—— 论文里最容易缺的就是这一句。
    if n.get("source"):
        out["source"] = str(n["source"]).strip()[:MAX_TEXT]
    return out


def _clean_section(s: dict, i: int) -> dict:
    if not isinstance(s, dict):
        raise ValueError(f"第 {i + 1} 节不是对象")
    sid = check_id(s.get("id"), f"第 {i + 1} 节的 id")
    title = str(s.get("title") or "").strip()
    if not title:
        raise ValueError(f"节 {sid}：title 不能为空")
    if len(title) > MAX_TITLE:
        raise ValueError(f"节 {sid}：title 超过 {MAX_TITLE} 字")
    kind = str(s.get("kind") or "result").strip()
    if kind not in SECTION_KINDS:
        raise ValueError(f"节 {sid}：kind 必须是 {'/'.join(SECTION_KINDS)}，收到 {kind!r}")
    needs = s.get("needs") or []
    if not isinstance(needs, list):
        raise ValueError(f"节 {sid}：needs 必须是数组")
    if len(needs) > MAX_NEEDS:
        raise ValueError(f"节 {sid}：数据需求最多 {MAX_NEEDS} 条")
    return {"id": sid, "title": title, "kind": kind,
            "intent": str(s.get("intent") or "").strip()[:MAX_TEXT],
            "needs": [_clean_need(n, f"节 {sid} 第 {j + 1} 条") for j, n in enumerate(needs)],
            "orphan": bool(s.get("orphan"))}


def _clean_exp(x: dict, i: int) -> dict:
    if not isinstance(x, dict):
        raise ValueError(f"第 {i + 1} 个实验不是对象")
    xid = check_id(x.get("id"), f"第 {i + 1} 个实验的 id")
    title = str(x.get("title") or "").strip()
    if not title:
        raise ValueError(f"实验 {xid}：title 不能为空")
    if len(title) > MAX_TITLE:
        raise ValueError(f"实验 {xid}：title 超过 {MAX_TITLE} 字")
    st = str(x.get("status") or "planned").strip()
    if st not in EXP_STATUS:
        raise ValueError(f"实验 {xid}：status 必须是 {'/'.join(EXP_STATUS)}，收到 {st!r}")
    feeds = x.get("feeds") or []
    if not isinstance(feeds, list):
        raise ValueError(f"实验 {xid}：feeds 必须是数组")
    feeds = [str(f).strip() for f in feeds if str(f).strip()]
    if len(feeds) > 24:
        raise ValueError(f"实验 {xid}：一个实验最多供 {24} 处数据")
    return {"id": xid, "title": title, "status": st, "feeds": feeds,
            "method": str(x.get("method") or "").strip()[:MAX_TEXT],
            "controls": str(x.get("controls") or "").strip()[:MAX_TEXT],
            "n": str(x.get("n") or "").strip()[:64],
            "days": x.get("days") if isinstance(x.get("days"), int) else None,
            "depends_on": [str(d).strip() for d in (x.get("depends_on") or [])
                           if str(d).strip()][:12],
            "note": str(x.get("note") or "").strip()[:MAX_TEXT]}


def build(*, title: str = "", question: str = "", thesis: str = "",
          sections=None, experiments=None, keep=None) -> dict:
    """把原始输入清洗成一颗可落盘的结构。**任何形状错误在这里抛，不进盘。**"""
    sections = sections or []
    experiments = experiments or []
    keep = keep or []
    if not isinstance(sections, list):
        raise ValueError("sections 必须是数组")
    if not isinstance(experiments, list):
        raise ValueError("experiments 必须是数组")
    if len(sections) > MAX_SECTIONS:
        raise ValueError(f"章节最多 {MAX_SECTIONS} 节（再多就没人读了）")
    if len(experiments) > MAX_EXPERIMENTS:
        raise ValueError(f"实验最多 {MAX_EXPERIMENTS} 个")
    secs = [_clean_section(s, i) for i, s in enumerate(sections)]
    # ★ keep：保留下来的孤儿节。它们 **不计入上限**，但必须先于
    #   “节必须存在”的校验就在场 —— 否则“删一节但还有实验指向它”这个
    #   **最常见的真实情形**会被当成“typo”拒掉。
    for i, s in enumerate(keep or []):
        if s.get("id") and s["id"] not in {x["id"] for x in secs}:
            secs.append(_clean_section(dict(s, orphan=True), len(sections) + i))
    exps = [_clean_exp(x, i) for i, x in enumerate(experiments)]

    dup = [k for k, n in _counts([s["id"] for s in secs]).items() if n > 1]
    if dup:
        raise ValueError(f"节 id 重复：{dup}（对账靠 id 连线，重了就连错）")
    dup = [k for k, n in _counts([x["id"] for x in exps]).items() if n > 1]
    if dup:
        raise ValueError(f"实验 id 重复：{dup}")

    # 连线的两端都必须真实存在。允许的是「实验指向一个已被删掉的节」吗？
    # 不允许 —— 那是把 typo 记成孤儿，永远查不出来。删节时 save 会把
    # 孤儿需求标 orphan=True 保留下来，id 仍在，所以这里能查到。
    known = {s["id"] for s in secs}
    bad = sorted({f for x in exps for f in x["feeds"] if f not in known})
    if bad:
        raise ValueError(
            f"实验 feeds 指向不存在的节：{bad}。已知节：{sorted(known)}。"
            "（如果那节确实要删，先删掉对应的 feed —— 否则对账会静默判成「有孤儿数据」。）")
    dep = sorted({d for x in exps for d in x["depends_on"]}
                 - {x["id"] for x in exps})
    if dep:
        raise ValueError(f"depends_on 指向不存在的实验：{dep}")

    return {"schema_version": SCHEMA_VERSION,
            "title": str(title or "").strip()[:MAX_TITLE],
            "question": str(question or "").strip()[:MAX_TEXT],
            "thesis": str(thesis or "").strip()[:MAX_TEXT],
            "sections": secs, "experiments": exps, "updated": _now()}


def _counts(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


# ── 读写 ────────────────────────────────────────────────────
def path_of(slug: str, root=None) -> Path | None:
    """大纲文件路径。★ **课题本身不存在就返 None**。

    为什么不能只算路径：`_d()` 只验证形式（合法 slug、不越界）。
    于是一个从未建过的课题也会得到一个看着正常的路径；
    调用方拿它去 `write_text` 就会凭空造出一个目录，而回包看起来像“已保存”。
    "路径能算出来" 与 "目标能写” 是两回事 —— 这里要的是后者。
    """
    d = _d(slug, root)
    if d is None:
        return None
    if _T.read_topic(slug, root=root) is None:
        return None
    return d / OUTLINE_FILE


def load(slug: str, root=None) -> dict | None:
    """读回大纲。**不存在返回 None，损坏也返回 None 但单独报**（见 `diagnose`）。"""
    p = path_of(slug, root)
    if p is None or not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    raw.setdefault("schema_version", SCHEMA_VERSION)
    raw.setdefault("sections", [])
    raw.setdefault("experiments", [])
    raw.setdefault("title", "")
    raw.setdefault("question", "")
    raw.setdefault("thesis", "")
    raw.setdefault("updated", "")
    return raw


def diagnose(slug: str, root=None) -> str:
    """为什么读不到。"" ⇒ 正常；否则一句人话（**不许静默当成"没写过大纲"**）。"""
    p = path_of(slug, root)
    if p is None:
        # ★ 这里要分两种：**课题不存在**与**课题在、只是没写过大纲**。
        #   前者是错误路径，后者是正常空缺。合成一句话会让用户去查一个根本没错的课题。
        if _d(slug, root) is not None and _T.read_topic(slug, root=root) is not None:
            return ""
        return "课题不存在或路径越界"
    if not p.exists():
        return ""
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                              # noqa: BLE001
        return f"{OUTLINE_FILE} 存在但读不成 JSON：{type(e).__name__}: {e}"
    return ""


def _atomic_write(p: Path, text: str) -> None:
    """固定停车位 + `os.replace`（委托 `state_store.atomic_write_text` 单源实现）。"""
    from .state_store import atomic_write_text
    atomic_write_text(p, text)


def save(slug: str, *, title: str = "", question: str = "", thesis: str = "",
         sections=None, experiments=None, root=None) -> dict:
    """全量替换式保存 + 读回校验。

    ★ 为什么是全量替换而不是按节 patch：大纲是一个**整体结构**（实验与章节靠 id
      互相指向）。按节增量合并之后，「我删了一节」与「我漏传了一节」在盘上长得
      一模一样 —— 那正是要防的漂移。模型手上永远有完整一份（`outline_status`
      会整份回给它），改完整体交回来。
    ★ 但**孤儿数据不跟着消失**：新结构里没有的旧节，若挂着数据需求，
      会被保留并标 `orphan=True` —— 见模块头纪律③。
    """
    d = _d(slug, root)
    if d is None:
        raise ValueError(f"非法课题 slug：{slug!r}")
    if _T.read_topic(slug, root=root) is None:
        raise KeyError(f"课题不存在：{slug!r}。先 topic_use 建/切过去。")

    old = load(slug, root=root) or {}
    # ── 孤儿保护：旧节里有数据需求、新结构里没有它 ⇒ 保留并标记 ──
    #   先算、再 build：因为实验的 feeds 可能还指向它，校验必须能看见它。
    keep_ids = {s.get("id") for s in sections or []}
    carried = [dict(s, orphan=True) for s in old.get("sections", [])
               if s.get("id") not in keep_ids and s.get("needs")]
    new = build(title=title or old.get("title", ""),
                question=question or old.get("question", ""),
                thesis=thesis or old.get("thesis", ""),
                sections=sections, experiments=experiments, keep=carried)

    # 旧实验若指向被带过来的节，连线仍然有效；指向真被删掉的节则断开并记录。
    known = {s["id"] for s in new["sections"]}
    for x in new["experiments"]:
        drop = [f for f in x.get("feeds", []) if f not in known]
        if drop:
            x["feeds"] = [f for f in x.get("feeds", []) if f in known]
            x["note"] = (x.get("note", "") + f"（原 feeds {drop} 已断：那些节被删且没有数据需求）").strip()

    p = d / OUTLINE_FILE
    _atomic_write(p, json.dumps(new, ensure_ascii=False, indent=1) + "\n")
    md = render_md(slug, new)
    _atomic_write(d / OUTLINE_MD, md)

    back = load(slug, root=root)
    ok = bool(back) and back.get("sections") == new["sections"] \
        and back.get("experiments") == new["experiments"]
    return {"ok": ok, "verified": ok, "slug": slug,
            "file": str(p), "md_file": str(d / OUTLINE_MD),
            "sections": len(back.get("sections", [])) if back else 0,
            "experiments": len(back.get("experiments", [])) if back else 0,
            "needs": sum(len(s.get("needs", [])) for s in (back or {}).get("sections", [])),
            "carried_orphan_sections": [s["id"] for s in carried],
            "md_chars": len(md)}


# ── 对账 ────────────────────────────────────────────────────
@dataclass
class Line:
    """一条连线：某节要的某样数据，由哪些实验供。"""
    section: str
    section_title: str
    what: str
    kind: str
    status: str
    feeders: list = field(default_factory=list)

    @property
    def section_id(self) -> str:
        return self.section


def reconcile(slug: str, root=None) -> dict:
    """把大纲与实验台账对一遍。**全部结论都可由这两份东西复算**，没有猜测。

    核的是什么（这是本模块存在的理由）：
      · 每一节要的数据，有没有至少一个实验在供 ⇒ 没有就是「这一节现在写不出来」；
      · 每个实验，供了几处数据 ⇒ 一处都不供就是「做了但不知道写进哪」；
      · 自称 `have`/`done` 的数据，台账里那条课题目录**真有**测量吗
        ⇒ 查不到就降为 `unverified`，**不许沿用自称**。
      · 四类章节里整类缺失 ⇒ 说出来（只是查漏，不是要求一定四类齐全）。

    台账那一侧用的是 `lab.py` 的 `measures_of` / `events`，
    与 `lab_report` 读的是同一份盘 —— 不另建一份"大纲以为的台账"。
    """
    from . import lab as _L

    ol = load(slug, root=root)
    if ol is None:
        return {"ok": False, "error": "还没写过大纲",
                "why": diagnose(slug, root=root) or f"{OUTLINE_FILE} 不存在",
                "slug": slug, "sections": 0, "experiments": 0,
                "starved": [], "orphan_experiments": [], "unverified": [],
                "empty_sections": [], "missing_kinds": [], "lines": []}

    # 台账里**真有**的测量：按指标名建集合。只认 measure 事件（定性观察不算数）。
    have_metrics: set = set()
    try:
        for ev in _L.events(slug, root=root):
            if ev.get("kind") == "measure" and ev.get("metric"):
                have_metrics.add(str(ev["metric"]).strip())
    except Exception:                                   # noqa: BLE001
        have_metrics = set()

    by_section = {s["id"]: s for s in ol.get("sections", [])}
    feed_map: dict = {}
    for x in ol.get("experiments", []):
        for f in x.get("feeds", []):
            feed_map.setdefault(f, []).append(x["id"])

    lines: list = []
    starved: list = []
    unverified: list = []
    for s in ol.get("sections", []):
        for n in s.get("needs", []):
            feeders = feed_map.get(s["id"], [])
            st = n.get("status", "missing")
            # ★ 自称已有的，去台账核一遍。核不上就降级 —— 不许沿用自称。
            if st in ("have", "done"):
                hit = _match_metric(n, have_metrics)
                if not hit:
                    st = "unverified"
                    unverified.append({"section": s["id"], "what": n["what"],
                                       "claimed": n.get("status"),
                                       "why": "台账里没有对应的测量（measures 为空或指标名对不上）"})
                else:
                    st = "done"
            ln = Line(section=s["id"], section_title=s.get("title", ""),
                      what=n.get("what", ""), kind=n.get("kind", ""),
                      status=st, feeders=feeders)
            lines.append(ln)
            if not feeders:
                starved.append({"section": s["id"], "title": s.get("title", ""),
                                "what": n.get("what", ""), "kind": n.get("kind", ""),
                                "status": st})

    orphan_exps = [{"id": x["id"], "title": x.get("title", ""), "status": x.get("status")}
                   for x in ol.get("experiments", []) if not x.get("feeds")]

    # ★★ 2026-11-15 真实使用补：一节**一条数据需求都写**，也是「这一节现在写不出来」。
    #   真实跑出来的大纲里有 8 节（全部引言 + 结论）needs 为空，而 `ok` 仍算 True ——
    #   因为它只看“要了数据却没人供”，没看“根本没要”。于是一份有八节空白的
    #   大纲拿到了一个绿灯。引言不需要实验数据，但它**必須声明它靠什么成立**（引哪几篇文献）。
    empty_sections = [{"id": s.get("id"), "title": s.get("title", ""),
                       "kind": s.get("kind", ""), "orphan": bool(s.get("orphan"))}
                      for s in ol.get("sections", []) if not s.get("needs")]

    kinds_present = {s.get("kind") for s in ol.get("sections", [])}
    missing_kinds = [{"kind": k, "label": v} for k, v in SECTION_KINDS.items()
                     if k not in kinds_present]

    return {"ok": not (starved or orphan_exps or unverified or empty_sections),
            "slug": slug,
            "title": ol.get("title", ""),
            "sections": len(ol.get("sections", [])),
            "experiments": len(ol.get("experiments", [])),
            "needs": len(lines),
            "have_metrics": sorted(have_metrics),
            "starved": starved,            # 要数据、没有实验供 ⇒ 这一节写不出来
            "orphan_experiments": orphan_exps,   # 做了实验、不知写进哪
            "unverified": unverified,      # 自称已有、台账核不上
            "missing_kinds": missing_kinds,
            "empty_sections": empty_sections,     # 一条数据需求都没写 ⇒ 也算写不出来
            "orphan_sections": [s["id"] for s in ol.get("sections", []) if s.get("orphan")],
            "lines": [{"section": l.section, "section_title": l.section_title,
                       "what": l.what, "kind": l.kind, "status": l.status,
                       "feeders": l.feeders} for l in lines]}


def _match_metric(need: dict, metrics: set) -> bool:
    """这条数据需求，在台账里有没有对应的测量。

    ★ 判据故意**宽松**：只要台账里有一个测量指标名，和这条需求的说法
      互相包含（需求里的词出现在指标名里，或反过来），就算核上。
      为什么宽松：指标名是用户自己起的（「转化率」「融合率%」「原生质体再生率」），
      要求逐字相等会把真做过的实验判成没做 —— 那是假红，比假绿更伤信任
      （用户会去改一个本来正确的大纲）。
      反过来的风险（把没做判成做了）由 `status` 一栏兜着：对账只把它标
      `done`，**不删也不改用户的原话**，用户在面板上仍看得见 `what` 原文。
    """
    w = str(need.get("what") or "").strip().lower()
    if not w:
        return False
    u = str(need.get("unit") or "").strip().lower()
    cands = {w}
    # 「X 的转化率」这类说法：把后缀拆出来单独比
    for sep in ("的", "：", ":", "（", "(", " "):
        if sep in w:
            cands.update(p.strip() for p in w.split(sep) if p.strip())
    for m in metrics:
        ml = m.strip().lower()
        if not ml:
            continue
        for c in cands:
            if c and (c in ml or ml in c):
                return True
        if u and u in ml:
            return True
    return False


# ── 供数计划 → 实验台账 ───────────────────────────────────
def todo_items(slug: str, root=None) -> list:
    """把「要什么数据」翻译成实验台账能吃的 todo 条目。

    只造 **todo**，不造 measure —— 这是红线的落点：大纲说什么数据该有，
    不能顺手在大纲里"造"出那条测量。真有数据必须来自 `lab_record(kind="measure")`。
    """
    rep = reconcile(slug, root=root)
    out = []
    for i, s in enumerate(rep.get("starved", [])):
        if s["status"] == "done":
            continue
        out.append({"what": f"[{s['title']}] 取数据：{s['what']}",
                    "due": "", "how": f"数据形态：{DATA_KINDS.get(s['kind'], s['kind'])}；"
                                    f"出自大纲第 {s['section']} 节",
                    "_section": s["section"], "_idx": i})
    return out


def plan_to_lab(slug: str, root=None, *, only_missing: bool = True) -> dict:
    """把大纲里还没有数据的需求，写成实验台账的 todo（幂等：已存在同名的跳过）。"""
    from . import lab as _L

    items = todo_items(slug, root=root)
    if not only_missing:
        items = [i for i in items]
    added, skipped = [], []
    existing = set()
    try:
        for ev in _L.events(slug, root=root):
            if ev.get("kind") == "todo":
                existing.add(str(ev.get("what", "")).strip())
    except Exception:                                   # noqa: BLE001
        pass
    # ★ todo 要 id（台账的硬规矩）。用大纲节 id 派生，**幂等可推导**：
    #   同一节同一条数据需求永远得到同一个 id，所以重跑不会写二条。
    #   不能用自增序号 —— 那样每次排都是新 id，幂等性就靠名字字符串碰巧了。
    for it in items:
        if it["what"] in existing:
            skipped.append(it["what"])
            continue
        try:
            _L.record(slug, "todo", root=root,
                      id=f"OL-{it['_section']}-{it['_idx']}",
                      what=it["what"], due=it.get("due", ""),
                      how=it.get("how", ""))
            added.append(it["what"])
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"写 todo 失败：{e}") from None
    return {"ok": True, "slug": slug, "added": added, "skipped": skipped,
            "total_added": len(added)}


# ── Markdown 视图 ──────────────────────────────────────────
def render_md(slug: str, ol: dict) -> str:
    """从结构渲染给人看的大纲。**别手改这份文件** —— 下一次保存会整份覆盖。"""
    L = [f"# 大纲 · {ol.get('title') or slug}", "",
         "> 本文件由 `outline.json` 自动渲染，手改会被下一次保存覆盖。",
         f"> 更新时间：{ol.get('updated', '')}", ""]
    if ol.get("question"):
        L += [f"**研究问题**：{ol['question']}", ""]
    if ol.get("thesis"):
        L += [f"**一句话主张**：{ol['thesis']}", ""]

    feed_map: dict = {}
    for x in ol.get("experiments", []):
        for f in x.get("feeds", []):
            feed_map.setdefault(f, []).append(x["id"])

    L += ["## 章节与数据需求", ""]
    for s in ol.get("sections", []):
        flag = " ⚠️孤儿（这一节已从结构里删掉，但还挂着数据需求）" if s.get("orphan") else ""
        L += [f"### {s.get('title', '')} `{s.get('id')}`"
              f" · {SECTION_KINDS.get(s.get('kind'), s.get('kind'))}{flag}", ""]
        if s.get("intent"):
            L += [f"{s['intent']}", ""]
        needs = s.get("needs", [])
        if not needs:
            L += ["- （这一节还没写数据需求 —— 意味着它现在没有数据来源）", ""]
            continue
        L += ["| 要什么数据 | 形态 | 状态 | 谁供 |", "| --- | --- | --- | --- |"]
        for n in needs:
            who = "、".join(feed_map.get(s["id"], [])) or "⚠️ **无实验供应**"
            L.append(f"| {n.get('what', '')} | {DATA_KINDS.get(n.get('kind'), n.get('kind'))} "
                     f"| {DATA_STATUS.get(n.get('status'), n.get('status'))} | {who} |")
        L += [""]

    L += ["## 实验计划", ""]
    exps = ol.get("experiments", [])
    if not exps:
        L += ["- （还没有实验）", ""]
    else:
        L += ["| 实验 | 状态 | 供哪几节 | 方法 | 对照 | n | 天数 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for x in exps:
            L.append(f"| `{x.get('id')}` {x.get('title', '')} "
                     f"| {EXP_STATUS.get(x.get('status'), x.get('status'))} "
                     f"| {'、'.join(x.get('feeds', [])) or '**无（做了不知道写进哪）**'} "
                     f"| {x.get('method', '')} | {x.get('controls', '')} "
                     f"| {x.get('n', '')} | {x.get('days') if x.get('days') is not None else ''} |")
        L += [""]

    L += ["> 对账（哪一节缺数、哪个实验没去处）请用 `outline_status`，"
          "那是按实验台账现算的，比这份快照新。", ""]
    return "\n".join(L)
