# -*- coding: utf-8 -*-
"""实验台账：把「手上有几株、谁快谁慢、发酵到第几天」变成**能算**的东西。

## 为什么要这个模块

用户（做中国被毛孢 × 蝙蝠蛾拟青霉原生质体融合）说的两件事，根子是同一件：

- 「老师说我没数据」——他不是没做实验，是**做的全是定性观察**：
  「三株 23℃ 适应但长得慢」「其他几株看起来长得快，但还没测定指标」。
  这两句话在代码里的形态就是 `len(measures)==0 and len(observes)>0`。
- 「下一步该干什么」——只要上面那个式子成立，答案就是机械的：
  **把已有观察转成一个带单位、带 n 的数字**。不需要模型发挥。

所以本模块只做两件事：① 让「定性」与「定量」**必须是两个 kind**（不是一个布尔位），
于是"没数据"能被代码算出来；② 让"下一步"由台账**机械排出来**，并逐条注明
`source`（台账哪一类 / 哪条资产），模型只负责转述，不负责发明。

## 存储

`topics/<slug>/LAB.jsonl` —— append-only 事件流 + 按 id 折叠，与
`topic.py` 的 `BLOCKERS.jsonl` 是**同一个范式**（那个已过生产验证）。
理由：实验记录最怕"改历史"。追加不覆盖 ⇒ 出错能看出是哪一步写错的，
而不是只留下一个被覆盖过的终值。

⚠️ **故意不放进 `topic.DOC_FILE`**：那张表是自由文本白名单，
`topic_write` 能整篇 replace —— 一次操作就能把结构化台账搅成散文，
而"搅成散文"正是本位此前所有问题的来源（四份 EVIDENCE.md 四种形状）。

## 刻意不做的事

- **不自动把定性观察转成数字**（`observe` 不会偷偷变成 `measure`）。
  转这一步必须由人给数字，工具只负责说"这里缺一个数字"。
- **不算"融合成功"**（没有那种判据）。它只报"身份证据到第几环"。
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

from . import lab_assets as A
from . import topic

LAB_FILE = "LAB.jsonl"

# 8 种事件。`observe`（定性）与 `measure`（定量）**必须是两个 kind** ——
# 合成一个 kind 加个 is_quant 布尔位，"没数据"就得靠人读，代码算不出来。
#
# `retract`（作废）为什么必须是第 8 个 kind（2026-11-03 补）：
#   第一版没有它。于是在建本课题台账时脚本写重了 5 条 observe，唯一的处理办法是
#   **手工去改 `LAB.jsonl`** —— 而"手工改台账"正是这个文件开头反对的那件事
#   （本文件全部设计就是为了"出错能看出是哪一步写错的"）。
#   一个 append-only 的账，若没有"作废"这一笔，它遇到错误就只有两种下场：
#   要么被手工改写（丢失"当时写错了"这件事），要么永远错着。
#   所以补上：**作废也是一次追加**，作废记录本身不可再作废（要撤销就重记一条新的）。
KINDS = ("strain", "batch", "measure", "observe", "metric", "todo", "update", "close",
         "retract")

_MAX_NOTE = 600          # 单条自由文本上限：台账不是日记，写太长就没人读

# 定性判断的落库值 → 表里显示的中文。**落库保留英文枚举**（代码要按它排序/过滤），
# 只在渲染时翻译 —— 反过来的话，用户改一次叫法就得改一遍代码。
_GROWTH_CN = {"slow": "慢", "fast": "快", "normal": "正常",
              "none": "不长", "dead": "已死"}


# ══ 路径与读写 ═══════════════════════════════════════════════
def lab_path(slug: str, root=None) -> Path:
    """`topics/<slug>/LAB.jsonl`。非法 slug / 课题不存在 ⇒ **抛错**（不返回 None）。

    与 `topic._blockers_path` 的区别：卡点台账在课题页里是可选附件，缺了就不显示；
    实验台账是本模块的**唯一**数据源，路径解析不出来就必须当场说，
    否则会出现"静默写到别处"或"静默建了个空台账"。
    """
    d = topic.topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}（只收字母/数字/下划线/短横/点/中文，≤48 字）")
    if not (d / "topic.json").exists():
        raise ValueError(f"课题不存在: {slug!r} —— 先用 topic_use 创建它")
    return d / LAB_FILE


def events(slug: str, root=None) -> list[dict]:
    """原始事件流（按写入顺序）。坏行**跳过但不扔** —— 原文还在盘上，可人工看。"""
    p = lab_path(slug, root)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if isinstance(e, dict) and e.get("kind"):
            out.append(e)
    return out


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return date.today().isoformat()


# ══ 一次记录 ═════════════════════════════════════════════════
def record(slug: str, kind: str, root=None, **fields) -> dict:
    """追加一条事件，**写完读回**再返回。`verified` 是读回来的结果，不是自称。

    为什么要 `verified`：本仓有条老债 —— 「自称成功」永不算证据。
    所以这里写完必重读文件、按 id 找到这条，确认它在盘上。
    """
    k = (kind or "").strip().lower()
    if k not in KINDS:
        raise ValueError(f"kind 只能是 {'/'.join(KINDS)}（收到 {kind!r}）")
    p = lab_path(slug, root)
    evs = events(slug, root)
    st = _fold(evs)
    # ⚠️ `seq` 这个词有两个意思：**事件自己的序号**与**retract 要作废的序号**。
    #   绝不能共用一个键 —— 否则一次作废会把"自己第几号"写成目标号，
    #   台账从此错位而任何校验都不会报错。所以调用方的 `seq` 一律先改名成
    #   `target_seq`，然后再写自己的 `seq`。
    fields = dict(fields)
    if "seq" in fields:
        if k != "retract":
            raise ValueError("seq 只能用于 kind='retract'（它指的是要作废的那一条）")
        fields["target_seq"] = fields.pop("seq")
    ev = {"kind": k, "at": _now(), "seq": len(evs) + 1}    # seq 落盘即固化，供 retract 指认
    for key, val in fields.items():
        if val is None or val == "":
            continue
        ev[key] = val

    ev = _validate(ev, st)
    if k == "retract":
        ev = _resolve_retract(ev, evs)

    line = json.dumps(ev, ensure_ascii=False)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")

    # 读回校验：按「最后一条事件的 (kind, 主键) 」定位，确认真在盘上
    back = events(slug, root)
    got = back[-1] if back else {}
    ok = bool(got) and got.get("kind") == k and line == json.dumps(got, ensure_ascii=False)
    st2 = _fold(back)
    return {"ok": ok, "verified": ok, "kind": k, "event": got,
            "file": LAB_FILE, "total_events": len(back),
            "live_events": st2["n_events"], "retracted": st2["n_retracted"]}


def _validate(ev: dict, st: dict) -> dict:
    """形状校验。**不合格一律抛错** —— 台账里的脏数据比没有数据更糟：
    它会让人以为"我有 5 条定量"，而实际那 5 条是「长得快」这种字。

    ★ 最要紧的一条：`measure.value` 必须是数字。
      把「慢」写进 value 是这条链上最容易发生的静默失败 —— 它不报错，
      但 `matrix()` 会得到一个字符串，`gaps()` 会以为这格已经填了。
    """
    k = ev["kind"]
    if k in ("strain", "measure", "observe"):
        code = str(ev.get("code") or "").strip()
        if not code:
            raise ValueError(f"{k} 必须给 code（菌株编号，如 S01）")
        ev["code"] = code
    if k == "strain":
        ev.setdefault("role", "candidate")
        if ev["role"] not in ("parent", "fusant", "candidate"):
            raise ValueError("strain.role 只能是 parent / fusant / candidate")
        # `placeholder`（占位编号）：用户还没给真实株号时（S01/S02…），必须**显式标出来**。
        # 不标的话，汇报页上 S01–S05 长得和真株号一模一样，模型会照着念给导师听 ——
        # 那是一次"看着像事实的编造"，而且错在源头，事后没法靠读的人发现。
        if "placeholder" in ev:
            ev["placeholder"] = bool(ev["placeholder"])
    if k == "batch":
        bid = str(ev.get("id") or "").strip()
        if not bid:
            raise ValueError("batch 必须给 id（批次号，如 B01）")
        ev["id"] = bid
        if not ev.get("started"):
            ev["started"] = _today()
    if k == "measure":
        if not ev.get("metric"):
            raise ValueError("measure 必须给 metric（指标 key）")
        mkey = str(ev["metric"]).strip().lower()
        ev["metric"] = mkey
        known = _metrics_map(st)
        if mkey not in known:
            raise ValueError(
                f"未注册的指标 {mkey!r} —— 已知: {', '.join(sorted(known))}。"
                "要加新指标，先记一条 kind='metric'（key/label/unit），"
                "否则缺口表会永远算不对")
        md = known[mkey]
        v = ev.get("value")
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(
                f"measure.value 必须是**数字**（收到 {v!r}）。"
                "「长得慢」「快」这类判断是定性观察，用 kind='observe' 记；"
                "台账里定量与定性**不许混**，混了就算不出缺口")
        u = str(ev.get("unit") or md.get("unit") or "").strip()
        if md.get("unit") and u and u != md["unit"]:
            raise ValueError(
                f"单位不一致：指标 {mkey} 用 {md['unit']!r}，你给 {u!r}。"
                "单位混着记，跨株比较就会错（mm/d 与 cm/d 差 10 倍）")
        ev["unit"] = u or md.get("unit", "")
        if ev.get("n") is not None:
            try:
                ev["n"] = int(ev["n"])
            except Exception:
                raise ValueError(f"measure.n 必须是整数（收到 {ev.get('n')!r}）")
    if k == "metric":
        key = str(ev.get("key") or "").strip().lower()
        if not key:
            raise ValueError("metric 必须给 key")
        ev["key"] = key
        if not ev.get("unit"):
            raise ValueError("metric 必须给 unit —— 没有单位的指标不是指标")
    if k == "todo":
        tid = str(ev.get("id") or "").strip()
        if not tid:
            raise ValueError("todo 必须给 id（如 T01）")
        ev["id"] = tid
        ev.setdefault("status", "open")
    if k in ("update", "close"):
        ident = str(ev.get("id") or "").strip()
        if not ident:
            raise ValueError(f"{k} 必须给 id（要更新的菌株编号/批次号/待办号）")
        ev["id"] = ident
        if k == "update" and not isinstance(ev.get("fields"), dict):
            raise ValueError("update 必须给 fields（要改的字段字典），否则是条空更新")
    if k == "retract":
        # 作废必须**说得清为什么**。没有理由的作废＝又一次"改历史不留痕"，
        # 而这恰恰是本文件通篇要避免的（见开头「刻意不做的事」）。
        if not str(ev.get("reason") or "").strip():
            raise ValueError(
                "retract 必须给 reason —— 例如 reason='脚本重复写入，同一条 observe 记了 5 遍'。"
                "作废不写理由，三个月后没人知道这条为什么被划掉")
        if ev.get("target_seq") is None and not isinstance(ev.get("target"), dict):
            raise ValueError(
                "retract 必须给 seq（要作废第几条，#seq 见事件序号）"
                "或 target（按字段匹配，如 {'kind':'observe','code':'S01'}）")
    for key in ("note", "text", "how", "what", "method"):
        if isinstance(ev.get(key), str) and len(ev[key]) > _MAX_NOTE:
            ev[key] = ev[key][:_MAX_NOTE] + "…"
    return ev


# ══ 作废（retract）分辨率 ═══════════════════════════════════
def _seqs(evs: list[dict]) -> list[int]:
    """每条事件的序号。**有 `seq` 就认 `seq`，没有就按位置**（1 起）。

    为什么两者都认：2026-11-02 之前落盘的事件没有 `seq` 字段，
    而台账是 append-only、不许回头补写。按位置推虽然怕手工改文件，
    但它对老账是唯一可用的口径；新账一律带 `seq`，从源头就不再依赖位置。
    """
    return [e["seq"] if isinstance(e.get("seq"), int) else i
            for i, e in enumerate(evs, 1)]


def _dead_seqs(evs: list[dict]) -> set[int]:
    """被作废掉的序号集合。**只认 `target_seq`** —— 作废是显式的一笔，不靠模糊推断。"""
    out: set[int] = set()
    for e in evs:
        if e.get("kind") != "retract":
            continue
        try:
            out.add(int(e.get("target_seq")))
        except (TypeError, ValueError):
            continue
    return out


def _sig(e: dict, seq=None) -> str:
    """一条事件的一句话签名（报错时用来指认「你指的是哪条」）。"""
    kind = e.get("kind")
    n = seq if isinstance(seq, int) else (e.get("seq") if isinstance(e.get("seq"), int) else "?")
    bits = [f"#{n}", str(kind)]
    for kk in ("code", "id", "metric", "value", "unit", "growth", "what"):
        if e.get(kk) not in (None, ""):
            bits.append(f"{kk}={e.get(kk)}")
    return " ".join(bits)[:96] + f"（{e.get('at','')}）"


def _match(e: dict, sel: dict) -> bool:
    """字段匹配：`sel` 里每个键都必须在事件里、且值相等（字符串化比较，容忍 1 vs "1"）。"""
    for kk, vv in sel.items():
        if kk not in e or str(e.get(kk)) != str(vv):
            return False
    return True


def _resolve_retract(ev: dict, evs: list[dict]) -> dict:
    """把 retract 解析成**唯一一条**目标事件，写死 `target_seq`。

    ★ 为什么不允许"匹配到几条就全作废"：作废是删除的反面 —— 它必须**精确**。
      批量作废的正确姿势是调用方自己点名（seq），而不是让工具替他猜范围。
      本课题第一版台账就是脚本一次写重了 5 条 observe，那时若有"批量作废"，
      一条手滑的调用就能把 5 条真观察一起抹掉。
    ★ 匹配失败时**把候选事件列出来**：只回「没找到」，模型下一句只能瞎猜 seq，
      而它手上根本没有列事件的工具。报错里带签名，等于把"下一步怎么问"一起给了。
    """
    seqs = _seqs(evs)
    dead = _dead_seqs(evs)
    want = ev.get("target_seq")

    if want is not None:
        try:
            want = int(want)
        except (TypeError, ValueError):
            raise ValueError(f"seq 必须是整数（收到 {ev.get('target_seq')!r}）") from None
        if want not in seqs:
            raise ValueError(f"没有 #{want} 这条事件（本台账共 {len(seqs)} 条，"
                             f"序号 1–{len(seqs) or 0}）")
        idx = seqs.index(want)
        tgt = evs[idx]
        if tgt.get("kind") == "retract":
            raise ValueError(
                f"#{want} 本身是一条作废记录，不能作废。"
                "要撤销一次作废，就重新记一条正确的事件（台账不删行，只叠加）")
        if want in dead:
            raise ValueError(f"#{want} 已经作废过了，不必重复作废")
        ev["target_seq"] = want
        ev.setdefault("target_kind", tgt.get("kind"))
        return ev

    sel = ev.get("target")
    if not isinstance(sel, dict) or not sel:
        raise ValueError("retract 的 target 必须是非空字典，如 {'kind':'observe','code':'S01'}")
    hits = [i for i, e in enumerate(evs)
            if seqs[i] not in dead and e.get("kind") != "retract" and _match(e, sel)]
    if len(hits) == 1:
        idx = hits[0]
        ev["target_seq"] = seqs[idx]
        ev.setdefault("target_kind", evs[idx].get("kind"))
        return ev
    if not hits:
        raise ValueError(
            f"没有一条事件同时满足 target={sel}。要作废的那条长什么样？"
            "请换用 seq=（见下方事件签名），或放宽 target 的字段。\n最近的几条：\n  "
            + "\n  ".join(_sig(e) for e in evs[-8:]))
    raise ValueError(
        f"target={sel} 同时匹配 {len(hits)} 条，**不会**一起作废（作废必须精确）。"
        f"请改用 seq= 指定其中一条：\n  "
        + "\n  ".join(_sig(evs[i]) for i in hits[:10]))


# ══ 折叠 ═════════════════════════════════════════════════════
def _metrics_map(st: dict) -> dict:
    """内置指标 + 本课题自定义指标。内置在前（同名以内置为准，单源）。"""
    m: dict[str, dict] = {x["key"]: dict(x) for x in A.TARGET_METRICS}
    for key, v in (st.get("metrics") or {}).items():
        m.setdefault(key, dict(v))
    return m


def _fold(evs: list[dict]) -> dict:
    """把**事件列表**折叠成当前状态。**不改盘、不改事件** —— 纯函数式折叠。

    抽成"吃列表"的理由不只是好测：`retract` 要判断"目标还在不在、是不是已经作废过"，
    而那必须看**同一份**事件流。若折叠自己再读一次文件，两次读取之间就可能不一致
    （写入与折叠之间插进一条），作废会指向错的行 —— 这类错很难复现。
    """
    seqs = _seqs(evs)
    dead = _dead_seqs(evs)
    why_of: dict[int, dict] = {}
    for e in evs:
        if e.get("kind") == "retract":
            try:
                why_of[int(e.get("target_seq"))] = e
            except (TypeError, ValueError):
                pass

    st = {"strains": {}, "batches": {}, "measures": [], "observes": [],
          "metrics": {}, "todos": {}, "closed": {}, "n_events": 0,
          "n_raw_events": len(evs), "n_retracted": 0, "retracted": []}
    for pos, e in zip(seqs, evs):
        k = e.get("kind")
        if k == "retract":
            continue                       # 作废记录本身不进状态，只在下面记一笔
        if pos in dead:
            st["n_retracted"] += 1
            st["retracted"].append({
                "seq": pos, "kind": k, "sig": _sig(e, pos),
                "reason": str((why_of.get(pos) or {}).get("reason") or "")[:200]})
            continue
        st["n_events"] += 1
        if k == "strain":
            code = e.get("code")
            cur = st["strains"].get(code, {"code": code})
            cur.update({kk: vv for kk, vv in e.items() if kk not in ("kind", "at")})
            st["strains"][code] = cur
        elif k == "batch":
            bid = e.get("id")
            cur = st["batches"].get(bid, {"id": bid})
            cur.update({kk: vv for kk, vv in e.items() if kk not in ("kind", "at")})
            st["batches"][bid] = cur
        elif k == "metric":
            st["metrics"][str(e.get("key"))] = {
                "key": e.get("key"), "label": e.get("label") or e.get("key"),
                "unit": e.get("unit", ""), "n_min": e.get("n_min", 3),
                "method": e.get("method", ""), "anchor": e.get("anchor", ""),
                "reference": e.get("reference", ""), "custom": True}
        elif k == "measure":
            st["measures"].append(e)
            # 有定量记录的菌株，即使没先建 strain 也要出现在矩阵里
            st["strains"].setdefault(e.get("code"), {"code": e.get("code")})
        elif k == "observe":
            st["observes"].append(e)
            st["strains"].setdefault(e.get("code"), {"code": e.get("code")})
        elif k == "todo":
            st["todos"][e.get("id")] = dict(e)
        elif k == "update":
            # ⚠️ 这里**不许用 `break`**。循环是 `for pos, e in zip(seqs, evs)`，
            #   在分支顶层写 `break` 退出的是**事件循环**，不是分支 ——
            #   表现是"这条 update 之后的所有事件全部丢失"，而且只有
            #   "后面的事件没生效"这种间接症状（第一版就是这样：
            #   一个 update 把后面的 close 与改名全吞了，事件数却还是对的）。
            ident = e.get("id")
            flds = e.get("fields") or {}
            if ident in st["strains"]:
                st["strains"][ident].update(flds)
                # 菌株**改名**（占位编号 → 真实株号）：只改 strain 的 key 不够，
                # 历史测量/观察上挂的 `code` 也必须一起搬，否则那几条会"消失"
                # （矩阵按 code 取 measures）。这是唯一允许"改主键"的地方，
                # 因为它不删任何事件 —— 只改折叠视图。
                new = str(flds.get("code") or "").strip()
                if new and new != ident:
                    st["strains"][new] = st["strains"].pop(ident)
                    for m in st["measures"] + st["observes"]:
                        if m.get("code") == ident:
                            m["code"] = new
                    for b in st["batches"].values():
                        if b.get("code") == ident:
                            b["code"] = new
            elif ident in st["batches"]:
                st["batches"][ident].update(flds)
            elif ident in st["todos"]:
                st["todos"][ident].update(flds)
        elif k == "close":
            ident = e.get("id")
            st["closed"][ident] = {"id": ident, "how": e.get("how", ""), "at": e.get("at", "")}
            if ident in st["todos"]:
                st["todos"][ident]["status"] = "closed"
            if ident in st["batches"]:
                st["batches"][ident]["status"] = "done"
    return st


def _state(slug: str, root=None) -> dict:
    """读盘 + 折叠。**所有视图都从这里取状态**，`_fold` 只给需要"同一份事件流"的
    调用方（`record` 写前校验、`_resolve_retract` 定位目标）直接用。"""
    return _fold(events(slug, root))


def state(slug: str, root=None) -> dict:
    """公开的状态视图（给 `topic.status_view` 与工具用）。"""
    return _state(slug, root)


# ══ 视图 ═════════════════════════════════════════════════════
def measures_of(st: dict, code: str, metric: str = "") -> list[dict]:
    return [m for m in st["measures"]
            if m.get("code") == code and (not metric or m.get("metric") == metric)]


def latest(st: dict, code: str, metric: str) -> dict | None:
    """同一格测过多次 ⇒ 取**最后一条**（台账是追加的，后面的覆盖前面的语义）。"""
    got = measures_of(st, code, metric)
    return got[-1] if got else None


def matrix(slug: str, root=None) -> dict:
    """菌株 × 指标矩阵。空格是 `None`（渲染时显示 `—`，**留白会让"没测"看起来像"没这项"**）。

    列 = 所有内置指标 + 任何已有测量值的自定义指标。
    行 = 所有出现过的菌株（含亲本，`role` 标出来 —— 亲本是"快慢"这件事的参照系，
    没有亲本同一条件下的数据，"某株长得快"这句话本身不成立）。
    """
    st = _state(slug, root)
    mm = _metrics_map(st)
    used = {m.get("metric") for m in st["measures"]}
    cols = [k for k in mm if k in used or mm[k].get("custom") is not True]
    for k in sorted(used):
        if k and k not in cols:
            cols.append(k)
            mm.setdefault(k, {"key": k, "label": k, "unit": "", "n_min": 3})
    rows = []
    for code in sorted(st["strains"]):
        s = st["strains"][code]
        cells = {}
        for c in cols:
            m = latest(st, code, c)
            cells[c] = ({"value": m.get("value"), "unit": m.get("unit", ""),
                         "n": m.get("n"), "sd": m.get("sd"),
                         "method": m.get("method", "")} if m else None)
        obs = [o for o in st["observes"] if o.get("code") == code]
        _g = (obs[-1].get("growth") if obs else s.get("growth")) or ""
        rows.append({
            "code": code, "species": s.get("species", ""), "role": s.get("role", ""),
            # 占位编号（S01/S02… 用户还没给真实株号）。**必须一路传到渲染层**：
            # 汇报页上它和真株号长得一模一样，读的人无从分辨。
            "placeholder": bool(s.get("placeholder")),
            "temp_c": (obs[-1].get("temp_c") if obs else s.get("temp_c")),
            # slow/fast 翻成中文：这是**给导师看的表格**，不是给程序读的枚举。
            # 保留原始值只会让一页中文里冒出两个英文词。
            "growth": _GROWTH_CN.get(str(_g), str(_g)),
            "observes": len(obs), "cells": cells,
        })
    return {"cols": [{"key": c, "label": mm[c].get("label", c),
                      "unit": mm[c].get("unit", "")} for c in cols],
            "rows": rows, "metrics": mm}


def gaps(slug: str, root=None) -> list[dict]:
    """缺口 = 「该有但没有」或「有但不够」。

    三类，**逐条说明为什么算缺口**，因为「缺 9 格」本身不指导任何行动：
      ① 无测量 —— 只有定性观察（这就是"没数据"）；
      ② n 不足 —— 测了但 n=1，不算重复；
      ③ 无亲本对照 —— 亲本在同条件下没数，快慢没有参照系。
    """
    st = _state(slug, root)
    mm = _metrics_map(st)
    out: list[dict] = []
    cands = [c for c in st["strains"]
             if st["strains"][c].get("role", "candidate") != "parent"]
    parents = [c for c in st["strains"] if st["strains"][c].get("role") == "parent"]

    for code in sorted(cands):
        for key in mm:
            md = mm[key]
            m = latest(st, code, key)
            if m is None:
                out.append({"code": code, "metric": key, "label": md.get("label", key),
                            "unit": md.get("unit", ""), "kind": "missing",
                            "why": f"没有任何测量值（只有定性观察 {len([o for o in st['observes'] if o.get('code')==code])} 条）"
                                   if [o for o in st["observes"] if o.get("code") == code]
                                   else "没有任何测量值",
                            "n_min": md.get("n_min", 3),
                            "method": md.get("method", ""),
                            "reference": md.get("reference", ""),
                            "anchor": md.get("anchor", "")})
            elif (m.get("n") or 0) < (md.get("n_min") or 0):
                out.append({"code": code, "metric": key, "label": md.get("label", key),
                            "unit": md.get("unit", ""), "kind": "few_reps",
                            "why": f"n={m.get('n') or 0} < 要求 {md.get('n_min')}（n=1 是观察，不是重复）",
                            "n_min": md.get("n_min", 3),
                            "method": md.get("method", ""),
                            "reference": md.get("reference", ""),
                            "anchor": md.get("anchor", "")})

    # 亲本对照：候选株有某个指标的数，亲本却没有 ⇒ 这个"快/慢"没有参照
    if cands:
        for key in mm:
            if not any(latest(st, c, key) for c in cands):
                continue
            if parents and not any(latest(st, c, key) for c in parents):
                out.append({"code": ",".join(parents), "metric": key,
                            "label": mm[key].get("label", key), "unit": mm[key].get("unit", ""),
                            "kind": "no_control",
                            "why": "候选株有数、亲本没有 ⇒ 「快/慢」缺少同条件参照",
                            "n_min": mm[key].get("n_min", 3), "method": mm[key].get("method", ""),
                            "reference": mm[key].get("reference", ""),
                            "anchor": mm[key].get("anchor", "")})
    return out


def _days_between(started: str, asof: str) -> int:
    try:
        d0 = date.fromisoformat(str(started)[:10])
        d1 = date.fromisoformat(str(asof)[:10]) if asof else date.today()
        return (d1 - d0).days
    except Exception:
        return -1


def next_actions(slug: str, asof: str = "", limit: int = 6, gap_cap: int = 3,
                 root=None) -> list[dict]:
    """机械排序的「下一步」。**每条都带 `source`**，模型只需转述，不许发明。

    排序依据（不是"重要性"，是**可执行性 + 时效**）：
      ① 发酵批次到点了 ⇒ 今天就能取样，且样品有时效（过夜就废）；
      ② 逾期待办 ⇒ 已经答应了没做；
      ③ 身份证据缺第一环 ⇒ 它是后面所有工作**成立的前提**（核相不对，测什么都不用做）；
      ④ 只有定性没有定量的株 ⇒ 补一个数字，这是"没数据"的唯一解。

    ★ ③ 必须排在 ④ 之前，否则会被 ④ 挤掉：5 株候选各缺 5 个指标，
      只按"缺口"排会一口气填满 limit，而最该先做的那件事根本没露头
      （第一版就是这个 bug，靠 `lab_test.py` §7 的正控逮到）。
    """
    st = _state(slug, root)
    asof = asof or _today()
    mm = _metrics_map(st)

    # ① 批次到点
    batch_acts: list[dict] = []
    for bid in sorted(st["batches"]):
        b = st["batches"][bid]
        if b.get("status") == "done":
            continue
        days = _days_between(b.get("started", ""), asof)
        exp = b.get("expect_days")
        try:
            exp_i = int(exp) if exp is not None else 0
        except Exception:
            exp_i = 0
        if days >= 0 and exp_i and days >= exp_i:
            batch_acts.append({
                "what": f"批次 {bid}（{b.get('code','?')}）已 {days} 天 ≥ 预计 {exp_i} 天 ⇒ "
                        f"取样测菌粉得率（g/100mL，n≥3）",
                "why": "批次到点后不取样，菌体自溶会让得率偏低，这一批就废了",
                "source": "台账·batch", "due": asof, "id": bid,
            })
        elif days >= 2 and not exp_i:
            # ★ 没填预计周期时**不许替用户编一个**（编出来就是"看着像事实的猜"），
            #   但也不能就此一声不响 —— 那是"正在发酵"的人最需要一句话的时刻：
            #   第五天该不该取样、这一批到底算几天，全悬着。
            #   所以给的是**用数据定周期**的做法：每日取样画产率曲线。
            #   这既绕开了猜，又顺手补上 `ferment_period` 这个指标本身需要的观察序列
            #   （单点测不出周期，这是该指标的物理事实，不是我们的口味）。
            cyc = A.FERMENT_CYCLE
            batch_acts.append({
                "what": f"批次 {bid}（{b.get('code','?')}）已第 {days} 天，**没填预计周期** ⇒ "
                        f"从今天起每 24 h 取样称重（g/100mL），把产率曲线画出来定周期",
                "why": f"台账里这条 batch 没有 expect_days，工具不会替你猜一个天数；"
                       f"库内同种参照：{cyc['reference']}（{cyc['anchor']}）。"
                       f"你的融合子落在哪一段要自己量 —— 单点取样测不出周期",
                "source": "台账·batch＋资产·周期", "due": asof, "id": bid,
            })

    # ② 待办：逾期在前，未到期的也列（**不列就等于这条待办不存在**）。
    #    第一版只收逾期的，于是"摸染色条件（截止 11-06）"在 11-02 的汇报页上
    #    一个字都不出现 —— 而那恰恰是用户**正在做**的那件事。
    overdue_acts: list[dict] = []
    upcoming_acts: list[dict] = []
    for tid in sorted(st["todos"]):
        t = st["todos"][tid]
        if t.get("status") == "closed":
            continue
        due = str(t.get("due") or "")
        if due and due[:10] < asof:
            overdue_acts.append({"what": f"{t.get('what','?')}（待办 {tid}）",
                                 "why": f"已过截止 {due}",
                                 "source": "台账·todo", "due": due, "id": tid})
        else:
            left = _days_between(asof, due) if due else -1
            when = (f"截止 {due}（还 {left} 天）" if left >= 0
                    else f"截止 {due}" if due else "未定截止日")
            upcoming_acts.append({"what": f"{t.get('what','?')}（待办 {tid}）",
                                  "why": f"在办：{when}",
                                  "source": "台账·todo", "due": due, "id": tid})
    todo_acts = overdue_acts + upcoming_acts

    # ③ 身份证据缺第一环
    #    ⚠️ 这是**关键词判据**（台账里没有"身份证据"这个 kind）：
    #    只要待办/测量/观察里出现过核相相关词，就认为这条已经安排上了。
    #    它可能低估（写了别的叫法就识别不到），所以只在**完全没提过**时才提示，
    #    并明说依据 —— 不许把"没提到"说成"没做"。
    ident_acts: list[dict] = []
    if st["strains"] and not identity_mentioned(st):
        first = next((x for x in A.IDENTITY_CHAIN if x.get("why_first")), A.IDENTITY_CHAIN[0])
        ident_acts.append({
            "what": f"做{first['label']}：{first['how'][:60]}…",
            "why": f"台账里没提到任何身份证据 ⇒ 这几株是否融合子还没有证据（{first['cost']}）",
            "source": "资产·IDENTITY_CHAIN",
            "due": "",
            "id": first["key"],
        })

    # ④ 只有定性、没有定量 —— **按指标聚合，不按株**
    #    ★ 为什么按指标而不是按株：第一版是"每株一条"，5 株 × 5 指标的结果是
    #      三条一模一样的「S01/S02/S03 补测菌落生长速率」。那不是指导，是噪音。
    #      实验现实是**一次上平板就测完所有株**，所以正确的动作单位是
    #      「把这个指标在所有株上做掉」，一条顶五条。
    measured = {(m.get("code"), m.get("metric")) for m in st["measures"]}
    cand_codes = [r["code"] for r in matrix(slug, root)["rows"]
                  if r["role"] != "parent" and r["observes"]]
    gap_acts: list[dict] = []
    for key in mm:
        md = mm[key]
        lacking = [c for c in cand_codes if (c, key) not in measured]
        if not lacking:
            continue
        who = "、".join(lacking[:6]) + ("等" if len(lacking) > 6 else "")
        gap_acts.append({
            "what": f"把 {who} 的{md.get('label', key)}一次测完"
                    f"（{md.get('unit','')}，n≥{md.get('n_min', 3)}）",
            "why": (f"{len(lacking)} 株全缺这一个数字 —— "
                    f"同批上平板就一次做完，分株做等于重复开实验"
                    + (f"；参照 {md['reference']}" if md.get("reference") else "")),
            "source": "台账·缺口",
            "due": "",
            "id": key,
        })

    return (batch_acts + todo_acts + ident_acts + gap_acts[:gap_cap])[:limit]


def digest(slug: str, asof: str = "", max_chars: int = 700, root=None) -> str:
    """给**上下文注入**用的台账摘要。空台账 ⇒ `""`（不输出空壳块）。

    硬约束：≤`max_chars`，且**按行截**（截出的半行比不截更误导）。
    上限不是审美问题 —— `context_text` 还要装三份文档的头部，
    这里多 200 字就是从别处抢 200 字。
    """
    st = _state(slug, root)
    if not st["n_events"]:
        return ""
    rows = matrix(slug, root)["rows"]
    gp = gaps(slug, root)
    acts = next_actions(slug, asof=asof, limit=3, root=root)
    running = [b for b in st["batches"].values() if b.get("status") != "done"]
    n_missing = len([g for g in gp if g["kind"] == "missing"])
    n_few = len([g for g in gp if g["kind"] == "few_reps"])
    open_todos = [t for t in st["todos"].values() if t.get("status") != "closed"]

    lines = [f"── 实验台账（{LAB_FILE}，{st['n_events']} 条事件）──",
             f"菌株 {len(st['strains'])} ｜ 批次 {len(st['batches'])}（进行中 {len(running)}）｜ "
             f"定量 {len(st['measures'])} 条 ｜ 缺口 {len(gp)}（无值 {n_missing}／n不足 {n_few}）｜ "
             f"待办 {len(open_todos)}"]
    # 作废：不是"没发生过"，而是"记过、且被划掉了"。摘要里不写，模型就会拿作废的
    # 那几条照样汇报（它看得见事件原文，看不见折叠过程）。
    if st["n_retracted"]:
        lines.append(f"（另有 {st['n_retracted']} 条已作废，不计入上面任何数字）")
    _ph = [r["code"] for r in rows if r.get("placeholder")]
    if _ph:
        lines.append(f"⚠️ {len(_ph)} 株的编号是**占位**（{'、'.join(_ph[:6])}），不是真实株号 —— "
                     "转述时要说清，别把它们念成株名。")
    if st["measures"] and not st["observes"]:
        pass
    if n_missing and not st["measures"]:
        lines.append(f"⚠️ {len(rows)} 株只有定性观察、0 条定量 —— 这就是「没数据」在台账里的样子。")
    elif n_missing:
        lines.append(f"⚠️ 仍有 {n_missing} 格无值 —— 这些格子在汇报里只能空着。")
    if acts:
        lines.append("下一步（机械排序，不含模型判断）：")
        for i, a in enumerate(acts, 1):
            lines.append(f"  {i}. {a['what']}［{a['source']}］")
    out: list[str] = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > max_chars:
            out.append("…（台账摘要已截断，完整内容用 `lab_report` 出）")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


# ══ 汇报产物 ═══════════════════════════════════════════════
def _fmt(v, sd=None) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        s = ("%g" % v)
    else:
        s = str(v)
    if sd not in (None, ""):
        s += f" ± {sd}"
    return s


def render_md(slug: str, now: str = "", asof: str = "", root=None) -> str:
    """组会可用的「一页」。`now` / `asof` **可注入** ⇒ 同输入逐字节可复现，
    才能当基线回归（否则每次跑都因为时间戳不同而"看起来变了"）。
    """
    st = _state(slug, root)
    meta = topic.read_topic(slug, root) or {}
    mx = matrix(slug, root)
    gp = gaps(slug, root)
    acts = next_actions(slug, asof=asof, limit=8, root=root)
    now = now or _now()
    asof = asof or _today()

    L: list[str] = []
    L.append(f"# 实验台账 · {meta.get('title') or slug}")
    L.append("")
    L.append(f"> 课题 `{slug}` ｜ 生成 {now} ｜ 事件 {st['n_events']} 条 ｜ "
             f"阶段 {meta.get('stage') or '—'}"
             + (f" ｜ **已作废 {st['n_retracted']} 条**" if st["n_retracted"] else ""))
    if meta.get("question"):
        L.append(f"> 研究问题：{str(meta['question'])[:120]}")
    L.append("")

    # ── 一、菌株 × 指标
    L.append("## 一、菌株 × 指标（空格 = 还没测，不是「没这项」）")
    L.append("")
    head = ["菌株", "物种/角色", "温度", "生长"]
    head += [f"{c['label']}（{c['unit']}）" if c["unit"] else c["label"] for c in mx["cols"]]
    L.append("| " + " | ".join(head) + " |")
    L.append("|" + "|".join(["---"] * len(head)) + "|")
    for r in mx["rows"]:
        cells = []
        for c in mx["cols"]:
            v = r["cells"].get(c["key"])
            if not v:
                cells.append("—")
            else:
                s = _fmt(v["value"], v.get("sd"))
                if v.get("n"):
                    s += f" (n={v['n']})"
                cells.append(s)
        role = {"parent": "亲本", "fusant": "融合子", "candidate": "候选"}.get(r["role"], r["role"] or "—")
        # 占位编号加 `*`：这张表是要拿去组会的，S01 与真实株号在纸面上无法分辨，
        # 唯一的补救是在渲染层留痕（不写进 code 本身 —— code 是主键，不能带装饰）。
        L.append("| " + " | ".join(
            [r["code"] + ("\\*" if r.get("placeholder") else ""),
             f"{r['species'] or '—'}/{role}",
             f"{r['temp_c']}℃" if r.get("temp_c") not in (None, "") else "—",
             str(r.get("growth") or "—")] + cells) + " |")
    L.append("")
    _ph = [r["code"] for r in mx["rows"] if r.get("placeholder")]
    if _ph:
        L.append(f"> `\\*` = **占位编号**（{('、'.join(_ph))}）：你还没给我真实株号，"
                 "这几行的名字是我起的。拿到真名后用 "
                 "`lab_record(kind=\"update\", id=\"S0x\", fields={\"code\":\"真名\", "
                 "\"placeholder\":false})` 改名即可，历史观察会自动跟过去。")
        L.append("")
    if not mx["rows"]:
        L.append("（台账里还没有菌株记录 —— 先用 `lab_record(kind=\"strain\")` 建株。）")
        L.append("")

    # ── 二、缺口
    L.append("## 二、缺口（这就是「没数据」的具体位置）")
    L.append("")
    if not gp:
        L.append("（没有缺口。）")
    else:
        # **按指标聚合，不逐格列。** 7 株 × 5 指标的逐格写法是 25 行几乎完全
        # 相同的句子 —— 那不是"完整"，那是读不下去。人要看的是
        # 「哪个指标一株都没测」，而这件事在逐格列表里恰恰看不清。
        by_m: dict[str, list] = {}
        for g in gp:
            by_m.setdefault(g["metric"], []).append(g)
        for c in mx["cols"]:
            gs = by_m.get(c["key"])
            if not gs:
                continue
            missing = [g["code"] for g in gs if g["kind"] == "missing"]
            few = [g["code"] for g in gs if g["kind"] == "few_reps"]
            ctrl = [g for g in gs if g["kind"] == "no_control"]
            bits = []
            if missing:
                bits.append(f"{len(missing)} 株**没有任何测量值**"
                            f"（{'、'.join(missing[:6])}{'等' if len(missing) > 6 else ''}）")
            if few:
                bits.append(f"{len(few)} 株 n 不足（{'、'.join(few[:4])}）")
            if ctrl:
                bits.append("亲本缺同条件数据 ⇒ 快慢没有参照")
            unit = f"（{c['unit']}）" if c.get("unit") else ""
            line = f"- **{c['label']}{unit}**：" + "；".join(bits)
            md_ = mx["metrics"].get(c["key"], {})
            if md_.get("reference"):
                line += f"　参照 {md_['reference']}"
            if md_.get("anchor"):
                line += f"　〔{md_['anchor']}〕"
            L.append(line)
    L.append("")

    # ── 三、批次
    if st["batches"]:
        L.append("## 三、发酵批次")
        L.append("")
        L.append("| 批次 | 菌株 | 起始 | 已天数 | 预计 | 状态 | 条件 |")
        L.append("|---|---|---|---|---|---|---|")
        for bid in sorted(st["batches"]):
            b = st["batches"][bid]
            days = _days_between(b.get("started", ""), asof)
            cond = "，".join(x for x in [
                str(b.get("vessel") or ""), str(b.get("medium") or ""),
                (f"{b['temp_c']}℃" if b.get("temp_c") not in (None, "") else "")] if x)
            L.append(f"| {bid} | {b.get('code','—')} | {b.get('started','—')} | "
                     f"{days if days >= 0 else '—'} | {b.get('expect_days','—')} | "
                     f"{b.get('status','running')} | {cond or '—'} |")
        L.append("")

    # ── 四、下一步
    L.append("## 四、下一步（机械排序；来源逐条标注）")
    L.append("")
    if not acts:
        L.append("（台账里没有可排的行动。）")
    else:
        for i, a in enumerate(acts, 1):
            L.append(f"{i}. **{a['what']}**")
            L.append(f"   - 依据：{a['why']}　〔来源：{a['source']}〕")
    L.append("")

    # ── 五、方法锚点
    L.append("## 五、方法来源（每条可在文献库按 doc_id + 页码反查）")
    L.append("")
    L.append("**定量指标**")
    for m in A.TARGET_METRICS:
        ref = f"；参照 {m['reference']}" if m.get("reference") else ""
        L.append(f"- {m['label']}（{m['unit']}，n≥{m['n_min']}）：{m['method']}　〔{m['anchor']}{ref}〕")
    L.append("")
    L.append("**融合子身份证据链（按成本排）**")
    for x in A.IDENTITY_CHAIN:
        L.append(f"- {x['label']}：{x['how'][:70]}　〔{x['anchor']}〕（{x['cost']}）")
    L.append("")

    # ── 附、作废清单
    # 为什么要在"给导师看的一页"上留这个：作废不是删除，它是**一次更正**。
    # 让读的人看见"这里改过、改成什么、为什么改"，比让它看起来完美更可信；
    # 而且汇报数字若与上周不同，这一节就是那个差异的解释。
    if st["n_retracted"]:
        L.append("## 附、已作废的记录（原始行仍在 LAB.jsonl 里，可查）")
        L.append("")
        for r in st["retracted"]:
            L.append(f"- #{r['seq']} `{r['sig']}` —— {r['reason'] or '（未写理由）'}")
        L.append("")
    return "\n".join(L) + "\n"


# ══ 方法卡：把 lab_assets 渲染成模型能直接用的文本 ═══════════════
# ★ 为什么要有这一层：资产在 `lab_assets` 里躺了整整一轮，**没有任何工具读它** ——
#   测试引用过它，所以它「通过」了；但模型永远看不见。这是本仓那条定律的第二次复发：
#   **能力存在 ≠ 模型够得着**。资产不进工具面，等于没写。
#   所以这里的每个函数都只做一件事：**确定性渲染**。模型负责解释与排期，
#   不许自己编参数 —— 参数只来自资产，资产只来自文献页码。
_METHOD_KEYS = ("staining", "identity", "quant")

_METHOD_ALIASES = {
    "staining": ("染色", "染核", "核质", "核相", "dapi", "hoechst", "荧光", "fb28",
                 "calcofluor", "cfw", "镜检", "封片", "激发"),
    "identity": ("身份", "融合子", "融合株", "杂种", "异核体", "双核", "证明", "鉴定",
                 "拮抗", "同工酶", "酯酶", "issr", "分子标记", "是不是融合"),
    "quant": ("定量", "指标", "数据", "得率", "含量", "速率", "测定", "测什么",
              "标尺", "参照", "对照值", "发酵周期", "腺苷", "甘露醇"),
}

# 每一环「台账里提没提过」的判据词。⚠️ 这是**关键词判据**，只用来区分
# 「已记录」与「未记录」；**未记录 ≠ 没做**，输出里必须这么写。
_IDENTITY_HINTS = {
    "nuclear": ("核相", "dapi", "染色", "核质", "荧光"),
    "antagonism": ("拮抗",),
    "isozyme": ("同工酶", "酯酶"),
    "marker": ("issr", "分子标记", "交配型", "测序"),
}


def identity_mentioned(st: dict, key: str = "") -> bool:
    """台账里是否提过身份证据（`key` 为空 ⇒ 任何一环）。单源：供下一步与卡片共用。

    只读 todos / measures / observes 的自由文本 —— 台账没有「身份证据」这个 kind，
    再开一个 kind 等于让用户多学一套字段，不划算。
    """
    blob = " ".join(str(x) for x in
                    list(st["todos"].values()) + st["observes"] + st["measures"]).lower()
    keys = [key] if key else list(_IDENTITY_HINTS)
    return any(w in blob for k in keys for w in _IDENTITY_HINTS.get(k, ()))


def method_need(text: str) -> tuple[str, list[str]]:
    """把「核质染色怎么做」这类说法映射到三类卡。返回 (主类, 顺带命中的其他类)。

    为什么要映射而不是让模型传枚举：模型经常直接传用户原话。
    映射不到就**抛错并列出可用说法** —— 静默回一张不相干的卡，比报错坏得多。
    """
    t = (text or "").strip().lower()
    if t in _METHOD_KEYS:
        return t, []
    if t:
        hits = [k for k in _METHOD_KEYS
                if any(a in t for a in _METHOD_ALIASES[k])]
        if hits:
            return hits[0], hits[1:]
    raise ValueError(
        f"不认识的「方法需求」：{text!r}。内置三张卡 —— "
        "`staining`（核质染色/DAPI/荧光）、`identity`（怎么证明这几株是融合子）、"
        "`quant`（该测哪些定量指标、标尺是多少）。"
        "三张卡里都没有的方法，用 `kb_search` / `kb_ask` 去库里查原文。")


_PROTO_FIELDS = [("sample", "样本"), ("fix", "固定"), ("centrifuge", "离心"),
                 ("wall_stain", "壁染"), ("nuclear_stain", "核染"), ("stain_time", "染色时长"),
                 ("mount", "封片"), ("excitation", "激发波长"),
                 ("wall_channel", "壁染通道"),
                 ("objective", "物镜"), ("strength", "优点"), ("weakness", "短板")]


def _render_staining(st: dict) -> str:
    from . import lab_assets as A
    L: list[str] = []
    L.append("# 核质染色方法卡（DAPI ／ 荧光增白剂）")
    L.append("")
    L.append("> 本卡由 `rongke/lab_assets.py` 的 `DAPI_PROTOCOLS` 直接渲染，"
             "**不是模型写的**；每个参数都能按 `doc_id` + 页码回库核对。")
    L.append("")

    L.append("## 一、先说最要紧的：两篇正式发表物的 DAPI 浓度差 1000 倍")
    L.append("")
    L.append("| 字段 | 方案 A | 方案 B | 比值 |")
    L.append("|---|---|---|---|")
    for c in A.PROTOCOL_CONFLICTS:
        ratio = f"{c['ratio']}×" if c.get("ratio") else "—（是非题，不是倍数）"
        L.append(f"| {c['field']} | {c['a']} | {c['b']} | {ratio} |")
    L.append("")
    for c in A.PROTOCOL_CONFLICTS:
        L.append(f"- **{c['field']}**：{c['advice']}")
    L.append("")

    for i, p in enumerate(A.DAPI_PROTOCOLS):
        num = "二三四五"[i] if i < 5 else str(i + 2)
        L.append(f"## {num}、{p['label']}")
        L.append("")
        L.append(f"出处：`{p['source']}`")
        L.append("")
        L.append("| 项 | 参数 |")
        L.append("|---|---|")
        for k, lab in _PROTO_FIELDS:
            if p.get(k):
                L.append(f"| {lab} | {p[k]} |")
        if p.get("source_quote"):
            L.append("")
            L.append(f"原文（照抄，可回库核对）：{p['source_quote']}")
        L.append("")

    L.append("## 五、你的样本不是它们用的样本")
    L.append("")
    cands = [c for c in st["strains"] if st["strains"][c].get("role", "candidate") != "parent"]
    parents = [c for c in st["strains"] if st["strains"][c].get("role") == "parent"]
    L.append(f"- 方案 A 用**芽生孢子**，方案 B 用**原生质体**，"
             f"而你要染的是**融合子菌丝**。")
    L.append("- 浓度与波长可以照抄，**前处理不能照抄**：菌丝比孢子厚，"
             "直接滴染常常只染出轮廓、看不到核 —— 必须先固定，必要时加轻微酶解透化。")
    if cands:
        L.append(f"- 台账里现有候选株 {len(cands)} 株（{', '.join(sorted(cands))}）"
                 + (f"，亲本 {len(parents)} 株（{', '.join(sorted(parents))}）" if parents else
                    "，**还没有登记亲本**"))
        if not parents:
            L.append("  ⚠️ 没有亲本就无法做阳性对照，也无法排除双培养 —— 先把两个亲本记进台账。")
        else:
            L.append("  ⇒ 阳性对照就用亲本菌丝：它必然有核，染不上一定是方法或设备的问题。")
    else:
        L.append("- 台账里还没有菌株。先 `lab_record(kind=\"strain\", …)` 登记，"
                 "这张卡才能把参数落到你的具体株上。")
    L.append("")

    L.append("## 六、失败分诊（三支互斥 —— 先对症状，再改配方）")
    L.append("")
    L.append("| 你看到的 | 最可能的原因 | 下一步怎么改 |")
    L.append("|---|---|---|")
    for f in A.STAIN_FAILURE_BRANCHES:
        L.append(f"| **{f['symptom']}** | {f['likely']} | {f['next']} |")
    L.append("")
    L.append("> 三支的原因互斥、改法也互斥。**同时改两个变量，失败后分不清是哪一步** ——"
             "这是这类实验最常见的返工来源。")
    L.append("")

    L.append("## 七、第一版怎么下手（每步只改一个变量）")
    L.append("")
    for s in A.STAIN_FIRST_RUN:
        L.append(f"**第 {s['step']} 步 · {s['label']}**　〔{s['anchor']}〕")
        L.append("")
        L.append(f"- 做什么：{s['what']}")
        L.append(f"- 怎么读结果：{s['readout']}")
        L.append(f"- 为什么排在这：{s['why']}")
        L.append("")

    L.append("## 附、推断部分（不是原文 —— 别拿去当引用）")
    L.append("")
    L.append(f"> 依据：{A.STAIN_JUDGEMENT['basis']}")
    L.append("")
    L.append("| 环节 | 建议 | 为什么这么推 |")
    L.append("|---|---|---|")
    for it in A.STAIN_JUDGEMENT["items"]:
        L.append(f"| {it['what']} | {it['how']} | {it['why']} |")
    L.append("")
    L.append(f"⚠️ {A.STAIN_JUDGEMENT['note']}")
    L.append("")

    L.append("## 八、出处（逐条可反查）")
    L.append("")
    for p in A.DAPI_PROTOCOLS:
        L.append(f"- `{p['source']}` —— {p['label']}")
    for f in A.STAIN_FAILURE_BRANCHES:
        L.append(f"- `{f['anchor']}` —— {f['symptom']}")
    L.append("")
    return "\n".join(L) + "\n"


def _render_identity(st: dict) -> str:
    from . import lab_assets as A
    L: list[str] = []
    L.append("# 融合子身份证据链（按成本从低到高）")
    L.append("")
    L.append("> 由 `lab_assets.IDENTITY_CHAIN` 渲染。**每一环都必须与两个亲本同时对照** ——"
             "只是「长得像/长得慢」不构成身份证据：双培养、亲本自交、异核不稳都能长得像。")
    L.append("")
    L.append("| # | 证据 | 怎么做 | 成本 | 出处 | 台账里 |")
    L.append("|---|---|---|---|---|---|")
    for i, x in enumerate(A.IDENTITY_CHAIN, 1):
        has = identity_mentioned(st, x["key"])
        mark = "已记录" if has else "**未记录**"
        L.append(f"| {i} | {x['label']} | {x['how']} | {x['cost']} | `{x['anchor']}` | {mark} |")
    L.append("")
    L.append("「未记录」≠「没做」：台账只收你主动记进去的东西，"
             "做过了没记就会显示未记录。")
    L.append("")

    first = next((x for x in A.IDENTITY_CHAIN if x.get("why_first")), A.IDENTITY_CHAIN[0])
    L.append(f"## 建议从「{first['label']}」开始")
    L.append("")
    L.append(f"{first['why_first']}　（{first['cost']}）")
    L.append("")

    cands = [c for c in st["strains"] if st["strains"][c].get("role", "candidate") != "parent"]
    parents = [c for c in st["strains"] if st["strains"][c].get("role") == "parent"]
    L.append("## 台账现状")
    L.append("")
    L.append(f"- 候选株 {len(cands)} 株：{', '.join(sorted(cands)) or '（无）'}")
    L.append(f"- 亲本 {len(parents)} 株：{', '.join(sorted(parents)) or '（无 —— 没有对照，任何身份证据都不成立）'}")
    L.append(f"- 定量测量 {len(st['measures'])} 条；定性观察 {len(st['observes'])} 条")
    L.append("")
    return "\n".join(L) + "\n"


def _render_quant(slug: str, st: dict, root=None) -> str:
    from . import lab_assets as A
    L: list[str] = []
    L.append("# 定量指标与标尺（「没数据」到底缺哪个数字）")
    L.append("")
    L.append("> 由 `lab_assets.TARGET_METRICS` 渲染。参照值全部来自库里的对照实验，"
             "不是通用经验值 —— 按 `doc_id` + 页码可反查。")
    L.append("")
    L.append("| 指标 | 单位 | 怎么测 | n≥ | 参照值 | 出处 |")
    L.append("|---|---|---|---|---|---|")
    for m in A.TARGET_METRICS:
        L.append(f"| **{m['label']}** | {m['unit']} | {m['method']} | {m['n_min']} | "
                 f"{m.get('reference') or '—'} | `{m['anchor']}` |")
    L.append("")
    L.append("**定性观察不算数据**：`长得慢` 是这一组指标的定性版本，"
             "缺的正是数字。台账里 `measures` 为 0 就是「没数据」的准确含义。")
    L.append("")

    # 缺口按**指标**聚合，不按株逐格列 —— 实验现实是一次上平板测全部株，
    # 分株做等于把同一个实验重复开五遍。
    gp = gaps(slug, root)
    by_metric: dict[str, list[dict]] = {}
    for g in gp:
        by_metric.setdefault(g["metric"], []).append(g)
    L.append("## 你现在的缺口（按指标聚合）")
    L.append("")
    if not gp:
        L.append("（没有缺口：每株每个指标都有 n≥3 的测量。）")
    else:
        for key, items in by_metric.items():
            md = A.metric(key) or {}
            lab = md.get("label", key)
            unit = md.get("unit", "")
            codes = sorted({c for g in items for c in str(g["code"]).split(",")})
            kinds = sorted({g["kind"] for g in items})
            why = "；".join(sorted({g["why"] for g in items}))[:160]
            L.append(f"- **{lab}（{unit}）**：{len(codes)} 株待补 —— {', '.join(codes)}")
            L.append(f"  　缺口类型：{', '.join(kinds)}。{why}")
    L.append("")
    L.append("## 怎么用这张表")
    L.append("")
    L.append("- 先补**一个**指标、把所有株一次测完，而不是一株测满五项 ——"
             "前者的可比性来自同批同条件，后者没有对照。")
    L.append("- 亲本必须与候选株同条件同批测，否则「快/慢」没有参照系"
             "（台账会把这类缺口记成 `no_control`）。")
    L.append("- 报数时带 n 与 sd：只报均值等于没有重复。")
    L.append("")
    return "\n".join(L) + "\n"


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n].rstrip("，。；、 ") + "…"


def say_method(need: str) -> str:
    """方法卡的**对话版**：≤ 500 字，能直接念给人听。

    为什么必须有它：卡是一份 Markdown，模型拿到它最省事的回法就是
    「已生成 方法卡-staining.md」——用户要的是「我该怎么染」，不是「文件在哪」。
    所以这里把同一批资产压成 3-5 行动作，形态上**不需要打开任何文件**。

    内容只取 `lab_assets` 的既有字段，不新增任何未在原文核过的参数。
    """
    key, _ = method_need(need)
    L: list[str] = []
    if key == "staining":
        # 「方案 A」在卡里有定义，但在对话里用户没看过卡 —— 必须把参数就地写出来，
        # 否则每一步都在引用一个他没见过的名词。
        p = A.DAPI_PROTOCOLS[0] if A.DAPI_PROTOCOLS else {}
        L.append("染色按这个顺序走，一次只改一个变量：")
        if p:
            L.append(f"0. 先做阳性对照 —— 取**未融合的亲本菌丝**，按「{p.get('fix','')} → "
                     f"{p.get('wall_stain','')} + {p.get('nuclear_stain','')}，"
                     f"{p.get('stain_time','')} → {p.get('objective','')}，"
                     f"激发 {p.get('excitation','')}」染，和你的片子同一次上机。"
                     f"看得见核 ⇒ 问题在材料或前处理；全黑 ⇒ 先查滤块与染液，别再调配方。"
                     f"〔{p.get('source','')}〕")
        for s in A.STAIN_FIRST_RUN[1:]:
            L.append(f"{s['step']}. {s['label']} —— "
                     f"{_clip(s['what'], 58).replace('方案 A', '第 0 步的配方')}")
        if A.PROTOCOL_CONFLICTS:
            c = A.PROTOCOL_CONFLICTS[0]
            L.append(f"⚠️ {c['field']} 两篇差 {c['ratio']} 倍（{c['a']} vs {c['b']}）："
                     f"前者染固定后的芽生孢子、后者染活的原生质体。"
                     f"你染的是融合子菌丝 —— **浓度可照抄，前处理不能照抄**。")
    elif key == "identity":
        L.append("融合子身份要过四环，按成本从低到高：")
        for i, x in enumerate(A.IDENTITY_CHAIN, 1):
            L.append(f"{i}. {x['label']} —— {_clip(x['how'], 52)}〔{x['anchor']}〕（{x['cost']}）")
    else:
        L.append("能拿去汇报的只有定量，这几个指标是标尺：")
        for m in A.TARGET_METRICS:
            L.append(f"- {m['label']}（{m['unit']}，n≥{m['n_min']}）："
                     f"{_clip(m['method'], 50)}〔{m['anchor']}〕")
    return "\n".join(L)


def say_report(slug: str, asof: str = "", limit: int = 3, root=None) -> str:
    """台账的**对话版**：把「下一步」压成不超过 3 条、能直接说的话。

    刻意**不含**统计表 —— 报数字（菌株几株、缺口几格）是 `render_md` 的事，
    对话里只需要「今天做什么」。台账为空 ⇒ 返回 `""`，不编。
    """
    st = _state(slug, root)
    if not st["n_events"]:
        return ""
    rows = matrix(slug, root)["rows"]
    acts = next_actions(slug, asof=asof, limit=limit, root=root)
    L: list[str] = []
    if not st["measures"] and rows:
        L.append(f"你现在的定量数据是 0 条（{len(rows)} 株都只有「长得快/慢」这种观察），"
                 f"导师催的就是这个 —— 先把一个数字测出来，比再筛一株有用。")
    if acts:
        L.append("下一步：")
        for i, a in enumerate(acts, 1):
            L.append(f"{i}. {a['what']}〔{a['source']}〕")
    else:
        L.append("台账里没有到点的事 —— 这说明记录还不全，不是没事可做。")
    ph = [r["code"] for r in rows if r.get("placeholder")]
    if ph:
        L.append(f"（{'、'.join(ph[:6])} 是临时编号，转述时别说成真实株号。）")
    return "\n".join(L)


def render_method_card(need: str, slug: str, root=None) -> dict:
    """渲染方法卡（**确定性**：同一台账 + 同一资产 ⇒ 逐字节相同）。

    三个 need 对应三张卡，每个参数都来自 `lab_assets` 并带 `doc_id` + 页码。
    模型只负责解释与排期；卡里没有的参数不许它补。
    """
    key, also = method_need(need)
    st = _state(slug, root)
    if key == "staining":
        md = _render_staining(st)
        title = "核质染色方法卡"
    elif key == "identity":
        md = _render_identity(st)
        title = "融合子身份证据链"
    else:
        md = _render_quant(slug, st, root)
        title = "定量指标与标尺"
    return {"ok": True, "need": key, "also_matched": also, "title": title,
            "md": md, "md_chars": len(md), "say": say_method(key)}


def write_method_card(slug: str, need: str, root=None) -> dict:
    """把方法卡写进课题目录（`方法卡-<key>.md`），**写完读回比对**。

    与 `write_report` 同一条路：slug 白名单 + `topic_dir()`，不复制围栏逻辑。
    整篇覆盖 —— 它是资产视图，不是台账（台账在 `LAB.jsonl`，那份才 append-only）。
    """
    d = topic.topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}")
    r = render_method_card(need, slug, root)
    p = d / f"方法卡-{r['need']}.md"
    p.write_text(r["md"], encoding="utf-8", newline="\n")
    back = p.read_text(encoding="utf-8", errors="replace")
    return {**r, "ok": back == r["md"], "verified": back == r["md"],
            "file": p.name, "dir": str(d), "md": r["md"]}


def render_csv(slug: str, root=None) -> str:
    """tidy 长表：一行一个观测值。**定性行也在**（`evidence_type=qual`），
    否则拿去画图时会误以为"这一株什么都没做"。"""
    st = _state(slug, root)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["code", "species", "role", "batch", "metric", "label", "value", "unit",
                "n", "sd", "method", "evidence_type", "at", "note"])
    known = _metrics_map(st)
    codes = sorted(st["strains"])
    for code in codes:
        s = st["strains"][code]
        obs = [o for o in st["observes"] if o.get("code") == code]
        for o in obs:
            w.writerow([code, s.get("species", ""), s.get("role", ""), "", "", "observe",
                        o.get("growth", ""), "",
                        "", "", "", "qual", o.get("at", ""),
                        (o.get("text") or "")[:120]])
        for m in measures_of(st, code):
            md = known.get(m.get("metric"), {})
            w.writerow([code, s.get("species", ""), s.get("role", ""), m.get("batch", ""),
                        m.get("metric", ""), md.get("label", m.get("metric", "")),
                        m.get("value", ""), m.get("unit", ""), m.get("n", ""),
                        m.get("sd", ""), m.get("method", ""), "quant", m.get("at", ""),
                        (m.get("note") or "")[:120]])
    return buf.getvalue()


_REPORT_MD = "LAB.md"
_REPORT_CSV = "LAB.csv"


def write_report(slug: str, now: str = "", asof: str = "", root=None) -> dict:
    """把 `LAB.md` 与 `LAB.csv` 写进课题目录，**写完读回比对**。

    ⚠️ 就地写 `topics/<slug>/`，与 `topic_write` 同一条路（slug 白名单 + `topic_dir()`），
    不复制围栏逻辑。两个文件都**整篇覆盖** —— 它们是从台账渲染出来的视图，
    不是台账本身（台账在 `LAB.jsonl`，那份才 append-only）。
    """
    d = topic.topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}")
    md = render_md(slug, now=now, asof=asof, root=root)
    cs = render_csv(slug, root=root)
    pm, pc = d / _REPORT_MD, d / _REPORT_CSV
    pm.write_text(md, encoding="utf-8", newline="\n")
    pc.write_text(cs, encoding="utf-8", newline="\n")
    back_md = pm.read_text(encoding="utf-8", errors="replace")
    back_cs = pc.read_text(encoding="utf-8", errors="replace")
    return {"ok": back_md == md and back_cs == cs,
            "verified": back_md == md and back_cs == cs,
            "say": say_report(slug, asof=asof, root=root),
            "md_file": _REPORT_MD, "csv_file": _REPORT_CSV,
            "md_chars": len(back_md), "csv_rows": max(0, back_cs.count("\n") - 1),
            "md_text": md, "csv_text": cs,
            "dir": str(d)}
