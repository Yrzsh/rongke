# -*- coding: utf-8 -*-
"""方法台账（method）＋ 创新点台账（patent）：把「方法从哪来 → 怎么做 → 会不会被问住 → 能不能报专利」钉成一条链。

## 它补的是哪个洞

`lab_method` 现在只会做一件事：把**库里已有**的三张资产卡（染色 / 身份 / 定量）
渲染出来。它假设「方法在文献里」，而真实做实验时更长的那一段是：

  · 库里没有人这么做过 —— 你得**自己设计**一个方法；
  · 做出来了，参数改了三次 —— 你得**记住这次到底按哪一版做的**；
  · 改完之后比原文好 —— 这可能**够报一个专利**，但你事后想不起来区别在哪。

这三件事今天全落在散漫的聊天记录里。`lab.py` 的台账有 strain/measure/observe…
但没有「一个方法」这个对象 —— 于是「这个数是按 v2 还是 v3 跑出来的」永远追不回去，
而这正是实验记录最容易被追责的一处（也是专利新颖性最容易自己毁掉的一处）。

## ★ 五条设计纪律（每条都对着一种真实失败）

① **参数只从原文来，不从常识来。** `params` 每一项必须带 `src=`：`kb:<doc_id> p.<页>`
   （库里文献）、`user`（他这次实测/口头定的）、`design`（他自己设计、尚待验证）。
   **不许留空** —— 没有出处的参数，模型下一轮就会当成自己记得的事实去复述。
   这与导师模型那条 `source=` 纪律同源。

② **每一版独立成条，永不覆盖。** 方法会迭代（酶浓度、时长、温度），
   而「上一版怎么做」在复现失败时是唯一线索。所以是 append-only 事件流，
   不是一份会被覆盖的 md。

③ **差异必须显式写出来。** `differs_from` 不为空时，`what_changed` 不能为空。
   「我这版更好」不算差异 —— 要说清改了哪个参数、从多少改到多少。

④ **专利线索不评判新颖性 —— 只登记「可能有点像」。** 新型性判断是专利代理人的事，
   模型替用户下「这能申请专利」的结论是有害的（他会真去报，然后被驳回，
   或者更糟：先发论文自毁新颖性）。所以这里只做三件可复算的事：
     · 记下「哪几条参数/步骤是你自己的，不在任何库内文献里」（`own_params`）；
     · 记下**时间线**（`first_public` 有没有填、比 `disclosed_at` 早还是晚）；
     · 提醒「发论文/开会之前先看这个」。
   `verdict` 只有三档，且措辞刻意保守：`worth_asking`（值得问代理人）/
   `check_first`（先补齐再问）/ `weak`（可能不够）。

⑤ **不做「方法库」的第二个副本。** 库里的方法仍然只有 `kb_*` 一条路；
   这里只存**关于你手上这个方法**的事实（你改了什么、你这版怎么跑）。

## 落盘

    topics/<slug>/METHODS.jsonl      # append-only 事件流（方法 + 版本 + 专利线索）
    topics/<slug>/METHODS.md         # 由事件流确定性渲染的视图（别手改）

为什么是 append-only 而不是一份 json：与 `lab.py` 同一条理由 ——
「上一次到底怎么做的」这个信息一旦被覆盖就永远找不回来，
而它恰恰是复现失败时唯一的线索。作废走 `retract` 事件，不改旧行。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from . import topic as _T

METHODS_FILE = "METHODS.jsonl"
METHODS_MD = "METHODS.md"

#: 事件类型。新增方法 / 新版本 / 观察 / 作废 / 专利线索。
KINDS = ("method", "version", "observe", "retract", "patent")

#: 方法从哪来。**这个字段是整件事的支点** —— 它决定「创新点」能不能算出来。
ORIGINS = {
    "literature": "照库里的文献做（参数应全部来自 kb 锚点）",
    "adapted": "库里方法 + 我改了参数（改了哪几个必须写）",
    "own": "自己设计的（库里没有现成做法）",
    "standard": "行业标准/试剂盒说明书（能引标准号或说明书版本）",
}

#: 参数出处前缀。**只收这四种** —— 收得越宽，编造越容易藏。
SRC_RE = re.compile(r"^(kb:[A-Za-z0-9]{4,32}( p\.[0-9ivxlcIVXLC\-]+)?|user|design|std:.{1,60})$")

#: 专利线索的三档判定。措辞刻意保守 —— 见模块头纪律④。
VERDICTS = {
    "worth_asking": "值得拿去问专利代理人（有自己的参数 + 有区别 + 未公开）",
    # ★ 2026-11-16 ：这句话原来把三种缺口写死在一个括号里。
    #   真实使用里出现了只差 first_public 的情形（自己的参数 7 条、差异也写了），
    #   结果给用户的一句话里仍写着「缺自己的参数」—— **与 gaps 列表自相矛盾**。
    #   一句话摘要必须从 gaps 算出来，不能把「可能的缺口清单」当成「当前的缺口」。
    "check_first": "先把缺口补齐再问（具体缺哪几项见 gaps）",
    "weak": "可能不够（改动太小或全是文献里的）",
    "published": "已经公开过 —— 新颖性可能已受影响，问代理人时务必先说这一条",
}

MAX_ID = 48
MAX_TEXT = 3000
MAX_PARAMS = 40
MAX_STEPS = 60
MAX_METHODS = 200

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 路径 ────────────────────────────────────────────────────
def methods_path(slug: str, root=None) -> Path:
    """`topics/<slug>/METHODS.jsonl`。非法 / 不存在 ⇒ **抛错**（与 `lab.lab_path` 同纪律）。"""
    d = _T.topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}")
    if not (Path(d) / "topic.json").exists():
        raise ValueError(f"课题不存在: {slug!r} —— 先用 topic_use 创建它")
    return Path(d) / METHODS_FILE


def events(slug: str, root=None) -> list:
    """原始事件流。坏行跳过但不扔（原文还在盘上）。"""
    p = methods_path(slug, root)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:                                   # noqa: BLE001
            continue
        if isinstance(e, dict) and e.get("kind"):
            out.append(e)
    return out


# ── 形状校验 ────────────────────────────────────────────────
def check_id(s: str, what: str = "方法 id") -> str:
    t = str(s or "").strip()
    if not _ID_RE.match(t):
        raise ValueError(f"{what} 只能用字母/数字/下划线/短横（≤48）：{t!r}。"
                         "中文名放 title，不要进 id（id 要进文件名与专利对照表）")
    return t


def _clean_params(params) -> list:
    """参数表。每一项 `{name, value, src}`，src 必须对上 `SRC_RE`。

    ★ 为什么 src 必填而不是可省：省掉之后，「我按 2% 溶壁酶做的」这句话
      在三个月后无法分辨是抄的、自己调的还是模型记错了。专利对比更要命。
    """
    if params is None:
        return []
    if not isinstance(params, list):
        raise ValueError("params 必须是数组（每项 {name, value, src}）")
    if len(params) > MAX_PARAMS:
        raise ValueError(f"参数最多 {MAX_PARAMS} 项")
    out = []
    for i, it in enumerate(params):
        if not isinstance(it, dict):
            raise ValueError(f"第 {i + 1} 个参数不是对象")
        name = str(it.get("name") or "").strip()
        val = str(it.get("value") or "").strip()
        src = str(it.get("src") or "").strip()
        if not name:
            raise ValueError(f"第 {i + 1} 个参数缺 name")
        if not val:
            raise ValueError(f"参数 {name} 缺 value（写成什么值？含单位）")
        if not src:
            raise ValueError(
                f"参数 {name} 缺 src —— **每个参数都必须写出处**："
                "`kb:<doc_id> p.<页>`（库里文献）/ `user`（你定的/实测的）/ "
                "`design`（自己设计、待验证）/ `std:<标准号或说明书版本>`。"
                "没有出处的参数下一轮就会被当成事实复述。")
        if not SRC_RE.match(src):
            raise ValueError(
                f"参数 {name} 的 src 写法不认：{src!r}。"
                "只收 `kb:<doc_id> p.<页>` / `user` / `design` / `std:<标准号或版本>`")
        out.append({"name": name[:80], "value": val[:200], "src": src[:80]})
    return out


def _clean_steps(steps) -> list:
    if steps is None:
        return []
    if not isinstance(steps, list):
        raise ValueError("steps 必须是字符串数组（一步一句，带量与时长）")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"步骤最多 {MAX_STEPS} 步")
    return [str(s).strip()[:500] for s in steps if str(s).strip()]


def _clean_pitfalls(p) -> list:
    if p is None:
        return []
    if not isinstance(p, list):
        raise ValueError("pitfalls 必须是字符串数组（每条一句：会怎么失败、怎么识别）")
    return [str(s).strip()[:500] for s in p if str(s).strip()][:20]


# ── 写一条 ──────────────────────────────────────────────────
def record(slug: str, kind: str, root=None, **fields) -> dict:
    """追加一条事件，**写完读回**再返回。`verified` 是读回结果，不是自称。"""
    k = (kind or "").strip().lower()
    if k not in KINDS:
        raise ValueError(f"kind 只能是 {'/'.join(KINDS)}（收到 {kind!r}）")
    p = methods_path(slug, root)
    evs = events(slug, root)

    fields = dict(fields)
    # ⚠️ `seq` 这个词有两个意思：**事件自己的序号**与**retract 要作废的序号**。
    #   绝不能共用一个键 —— 否则一次作废会把「自己第几号」写成目标号，台账从此错位而
    #   任何校验都不会报错。所以调用方的 `seq` 一律先改名成 `target_seq`。
    #   （这条纪律是从 `lab.record` 抄来的，那边踩过一次。）
    if "seq" in fields:
        if k != "retract":
            raise ValueError("seq 只能用于 kind='retract'（它指的是要作废的那一条）")
        fields["target_seq"] = fields.pop("seq")
    ev = {"kind": k, "at": _now(), "seq": len(evs) + 1}
    for key, val in fields.items():
        if val is None or val == "":
            continue
        ev[key] = val

    ev = _validate(ev, evs)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    back = events(slug, root)
    ok = bool(back) and back[-1].get("seq") == ev["seq"]
    return {**ev, "ok": ok, "verified": ok,
            "file": METHODS_FILE, "slug": slug}


def _validate(ev: dict, evs: list) -> dict:
    """形状校验。**不合格一律抛错，不进盘。**"""
    k = ev["kind"]
    known = {e.get("id") for e in evs if e.get("kind") == "method"}

    if k in ("method", "version"):
        mid = check_id(ev.get("id"))
        ev["id"] = mid
        title = str(ev.get("title") or "").strip()
        if k == "method" and not title:
            raise ValueError(f"方法 {mid} 缺 title（这个方法叫什么）")
        if k == "version" and mid not in known:
            raise ValueError(f"方法 {mid} 还不存在 —— 先用 kind='method' 建它再记版本")
        if k == "version":
            ver = ev.get("version")
            if not isinstance(ver, int) or ver < 1:
                raise ValueError("version 必须是 ≥1 的整数（第几版）")
            prev = [e for e in evs if e.get("kind") == "version" and e.get("id") == mid]
            # ★ 首建那条 `method` 事件本身就是 v1（`_fold` 里把它折成了 v1）。
            #   这里原来只数 `version` 事件，于是再报一次 v1 会被当成合法写进去 ——
            #   盘上就有两层 v1，而「上一版到底怎么做」从此无法回答。
            seen = [e["version"] for e in prev]
            if any(e.get("kind") == "method" and e.get("id") == mid for e in evs):
                seen.append(1)
            if ver in seen:
                raise ValueError(
                    f"方法 {mid} 已经有 v{ver} 了 —— **版本号不能复用**。"
                    f"已用：{sorted(seen)}。上一版的样子要能随时追回去，覆盖它就追不回了")
        else:
            if mid in known:
                raise ValueError(
                    f"方法 {mid} 已存在 —— 要改内容用 kind='version' 记新版本，"
                    "不要用 kind='method' 覆盖（覆盖之后「上一版怎么做」就没了）")
            if len(known) >= MAX_METHODS:
                raise ValueError(f"方法数已到上限 {MAX_METHODS}")
            origin = str(ev.get("origin") or "").strip()
            if origin not in ORIGINS:
                raise ValueError(
                    f"方法 {mid} 的 origin 必须是 {'/'.join(ORIGINS)}（收到 {origin!r}）。"
                    "**这个字段决定创新点能不能算出来，不能空着**")
            ev["origin"] = origin
            ev["title"] = title[:120]
            differs = [str(x).strip() for x in (ev.get("differs_from") or []) if str(x).strip()]
            ev["differs_from"] = differs[:10]
        ev["params"] = _clean_params(ev.get("params"))
        ev["steps"] = _clean_steps(ev.get("steps"))
        ev["pitfalls"] = _clean_pitfalls(ev.get("pitfalls"))
        # ★ 纪律③：说「不一样」就必须说清哪里不一样。
        if ev.get("differs_from") and not str(ev.get("what_changed") or "").strip():
            raise ValueError(
                f"方法 {mid} 写了 differs_from={ev['differs_from']} 但没写 what_changed —— "
                "「我这版更好」不算差异，要说清改了哪个参数、从多少改到多少")
        if str(ev.get("what_changed") or "").strip():
            ev["what_changed"] = str(ev["what_changed"]).strip()[:MAX_TEXT]
        if str(ev.get("note") or "").strip():
            ev["note"] = str(ev["note"]).strip()[:MAX_TEXT]
        for key in ("instrument", "consumable", "yield"):
            if str(ev.get(key) or "").strip():
                ev[key] = str(ev[key]).strip()[:300]

    elif k == "observe":
        mid = check_id(ev.get("id"))
        ev["id"] = mid
        if mid not in known:
            raise ValueError(f"方法 {mid} 不存在")
        text = str(ev.get("text") or "").strip()
        if not text:
            raise ValueError("observe 要写 text（这次做的时候发生了什么）")
        ev["text"] = text[:MAX_TEXT]
        if str(ev.get("version") or "").strip():
            ev["version"] = str(ev["version"]).strip()[:16]
        for key in ("worked", "failed"):
            if key in ev:
                ev[key] = bool(ev[key])

    elif k == "retract":
        tgt = ev.get("target_seq")
        if not isinstance(tgt, int) or tgt < 1:
            raise ValueError("retract 要给 target_seq（要作废的那一条的 seq）")
        if not any(e.get("seq") == tgt for e in evs):
            raise ValueError(f"没有 seq={tgt} 这一条")
        if not str(ev.get("why") or "").strip():
            raise ValueError("retract 要给 why（为什么作废）—— 没有理由的作废等于篡改")
        ev["why"] = str(ev["why"]).strip()[:500]

    elif k == "patent":
        mid = check_id(ev.get("id"))
        ev["id"] = mid
        if mid not in known:
            raise ValueError(f"方法 {mid} 不存在 —— 专利线索必须挂在某个具体方法上")
        if str(ev.get("disclosed_at") or "").strip():
            ev["disclosed_at"] = str(ev["disclosed_at"]).strip()[:32]
        if str(ev.get("first_public") or "").strip():
            ev["first_public"] = str(ev["first_public"]).strip()[:32]
        ev["claims_basis"] = [str(x).strip()[:400]
                              for x in (ev.get("claims_basis") or []) if str(x).strip()][:20]
    return ev


# ── 折叠成状态 ──────────────────────────────────────────────
def _fold(evs: list) -> dict:
    """事件流 → 当前状态。作废的整条不再参与（人仍然能在 jsonl 里看到它）。"""
    dead = {e.get("target_seq") for e in evs if e.get("kind") == "retract"}
    alive = [e for e in evs if e.get("seq") not in dead]
    methods: dict = {}
    for e in alive:
        k = e["kind"]
        if k == "method":
            methods[e["id"]] = {"id": e["id"], "seq": e.get("seq"),
                                "title": e.get("title", ""),
                                "origin": e.get("origin", ""),
                                "differs_from": list(e.get("differs_from") or []),
                                "versions": [], "observes": [], "patent": None,
                                "at": e.get("at", ""), "_first": e}
        elif k == "version" and e["id"] in methods:
            methods[e["id"]]["versions"].append(e)
        elif k == "observe" and e["id"] in methods:
            methods[e["id"]]["observes"].append(e)
        elif k == "patent" and e["id"] in methods:
            methods[e["id"]]["patent"] = e
    for m in methods.values():
        # ★ 2026-11-15：最初 `method` 这条事件本身就记了第一版参数，
        #   但 `_fold` 只把 `version` 事件收进 `versions`，于是**首建的参数永远不会
        #   出现在 `latest` 里** —— 判据当场抓到（3 条参数算出 0 条）。
        #   语义上它确实是 v1：方法建起来的那一刻就是第一版。
        first = m.pop("_first", None)
        if first and not any((v.get("version") or 0) <= 1 for v in m["versions"]):
            v1 = {"kind": "version", "id": m["id"], "version": 1,
                  "at": first.get("at", ""), "params": first.get("params") or [],
                  "steps": first.get("steps") or [],
                  "pitfalls": first.get("pitfalls") or [],
                  "what_changed": first.get("what_changed", "")}
            for extra in ("instrument", "consumable", "yield", "note"):
                if first.get(extra):
                    v1[extra] = first[extra]
            m["versions"].append(v1)
        m["versions"].sort(key=lambda x: x.get("version") or 0)
        m["latest"] = m["versions"][-1] if m["versions"] else None
    return {"methods": methods, "alive": alive, "n_retracted": len(dead)}


def state(slug: str, root=None) -> dict:
    return _fold(events(slug, root=root))


# ── 创新点判定（只做可复算的三件事）──────────────────────────
def _own_params(ver: dict) -> list:
    """这一版里**不在裤内文献**的参数（`user` / `design` / `std`）。

    ★ 这是判断「有没有你自己的东西」的**唯一**可复算口径。
      不靠模型感觉，靠每一条参数的 `src` 前缀。
    """
    if not ver:
        return []
    return [p for p in (ver.get("params") or [])
            if not str(p.get("src", "")).startswith("kb:")]


def patent_view(slug: str, mid: str, root=None) -> dict:
    """一个方法的创新点视图。**不下新颖性判决，只说三件能复算的事。**"""
    st = state(slug, root=root)
    m = st["methods"].get(mid)
    if m is None:
        raise ValueError(f"方法不存在：{mid!r}。已有的：{sorted(st['methods'])}")
    ver = m["latest"]
    own = _own_params(ver)
    pat = m.get("patent") or {}
    disclosed = str(pat.get("disclosed_at") or "").strip()
    public = str(pat.get("first_public") or "").strip()

    gaps = []
    if not own:
        gaps.append("最新一版里**没有任何一条参数是你自己的**（全部 src=kb:*）"
                    "—— 照文献做的改动，一般不属于可申请的创新")
    if m.get("origin") == "literature":
        gaps.append("origin=literature（完全是文献做法）")
    if ver and not str(ver.get("what_changed") or "").strip():
        gaps.append("最新一版**没写跟之前/跟文献差在哪**（what_changed 为空）")
    if not pat:
        gaps.append("还没有登记专利线索（kind='patent'）—— 说明还没梳理过")
    # ★★ 2026-11-15 真实使用补：模型实际录入的参数里有一半是
    #   「得率 = 高于原文献（用户口述，未给具体数值）」这种**定性说法**。
    #   它 src=user，于是被算进「你自己的参数」—— 但专利代理人拿到「高于原文献」这句
    #   是无法写进权利要求的：权利要求要的是数值范围。所以单独列一条缺口，
    #   **但不把它们从 own_params 里扣掉** —— 那会让「我有自己的东西」这个事实被误报。
    soft = [p for p in own
            if not re.search(r"\d", str(p.get("value", "")))]
    if soft:
        gaps.append(
            "有 %d 条参数是**定性说法、没有数值**（%s）—— "
            "专利权利要求要的是数值范围，这几条先补成具体数"
            % (len(soft), "、".join(p.get("name", "?") for p in soft[:4])))
    if pat and not public:
        gaps.append("没填 first_public（论文投稿/公开发布的日期）—— "
                    "**这个日期决定新颖性还在不在，必须填**")

    verdict = "worth_asking"
    if public:
        verdict = "published"
    elif not own or m.get("origin") == "literature":
        verdict = "weak"
    elif gaps:
        verdict = "check_first"

    return {
        "ok": True, "slug": slug, "id": mid, "title": m.get("title", ""),
        "origin": m.get("origin", ""), "n_versions": len(m["versions"]),
        "own_params": own,
        # ★ `latest` 在只建了方法、还没记过版本时是 None（首次使用就会走到）。
        #   这里原来直接 `ver.get(...)`，一调就 AttributeError —— 判据里的「应当空而不崩」那条就是为这个写的。
        "kb_params": [p for p in ((ver or {}).get("params") or [])
                      if str(p.get("src", "")).startswith("kb:")],
        "has_version": ver is not None,
        "what_changed": (ver or {}).get("what_changed", ""),
        "disclosed_at": disclosed, "first_public": public,
        "claims_basis": list(pat.get("claims_basis") or []),
        "soft_params": soft,
        "gaps": gaps, "verdict": verdict, "verdict_cn": VERDICTS[verdict],
        "note": "★ 这**不是**新颖性结论。它只算三件事：你有几条自己的参数、"
                "改动写没写清、公开日期填没填。能不能报，要拿去问专利代理人；"
                "**发表/开会之前务必先看 first_public**。",
    }


# ── 渲染 ────────────────────────────────────────────────────
def _fmt_params(ps: list) -> str:
    if not ps:
        return "（没有参数 —— 一个方法没有参数，别人复不了）"
    return "\n".join(f"  - {p['name']}：{p['value']}  `{p['src']}`" for p in ps)


def render_md(slug: str, root=None) -> str:
    """确定性渲染：同一事件流 ⇒ 逐字节相同。"""
    st = state(slug, root=root)
    L = [f"# 方法台账 · {slug}", "",
         "> 由 `METHODS.jsonl` 渲染，别手改（作废走 retract 事件，不改旧行）。", ""]
    ms = st["methods"]
    if not ms:
        L += ["（还没有记录任何方法）", ""]
        return "\n".join(L)
    for mid in sorted(ms):
        m = ms[mid]
        L += [f"## `{mid}` {m['title']}", "",
              f"- 来源：{ORIGINS.get(m['origin'], m['origin'])}",
              f"- 版本数：{len(m['versions'])}"]
        if m.get("differs_from"):
            L.append(f"- 区别于：{'、'.join(m['differs_from'])}")
        L += [""]
        for ver in m["versions"]:
            L += [f"### v{ver.get('version')}", ""]
            if ver.get("what_changed"):
                L += [f"**跟上一版/文献的差异**：{ver['what_changed']}", ""]
            L.append("**参数**：")
            L.append(_fmt_params(ver.get("params") or []))
            L.append("")
            if ver.get("steps"):
                L.append("**步骤**：")
                L += [f"{i + 1}. {s}" for i, s in enumerate(ver["steps"])]
                L.append("")
            if ver.get("pitfalls"):
                L.append("**坑（会怎么失败 / 怎么认出来）**：")
                L += [f"- {s}" for s in ver["pitfalls"]]
                L.append("")
        if m["observes"]:
            L.append("**做的时候的记录**：")
            for o in m["observes"]:
                mark = "✔" if o.get("worked") else ("✘" if o.get("failed") else "·")
                v = f" v{o['version']}" if o.get("version") else ""
                L.append(f"- {mark}{v} {o.get('text', '')}  （{o.get('at', '')}）")
            L.append("")
        if m.get("patent"):
            pv = patent_view(slug, mid, root=root)
            L += [f"**专利线索**：{pv['verdict_cn']}",
                  f"- 我自己的参数：{len(pv['own_params'])} 条"
                  f"（文献参数 {len(pv['kb_params'])} 条）"]
            if pv["first_public"]:
                L.append(f"- 首次公开：{pv['first_public']}")
            for g in pv["gaps"]:
                L.append(f"- 缺口：{g}")
            L.append("")
    return "\n".join(L)


def write_md(slug: str, root=None) -> dict:
    """渲染并落盘 `METHODS.md`，**写完读回比对**。"""
    d = _T.topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}")
    md = render_md(slug, root=root)
    p = Path(d) / METHODS_MD
    tmp = p.with_name("." + p.name + ".tmp")
    tmp.write_text(md, encoding="utf-8", newline="\n")
    os.replace(tmp, p)
    back = p.read_text(encoding="utf-8", errors="replace")
    return {"ok": back == md, "verified": back == md, "file": METHODS_MD,
            "dir": str(d), "md": md, "md_chars": len(md)}
