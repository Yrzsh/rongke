# -*- coding: utf-8 -*-
"""图表登记单源：给每张图/表一个**稳定身份**，把编号降级为渲染产物。

## 为什么

改造前，`图 1` 这个字符串同时扛身份、出现顺序、交叉引用、题注编号和文件名
后缀；`next_number()` 是「已有题注编号的最大值 +1」。于是删一张图会永久断号、
重排章节后全篇「见图 N」不会跟着变，而 `scan_visuals`（扫题注正则）、
`typeset`（从图片链接反推块）、`paper_desk`（扫盘数 *.svg）三套事实源互不互证。

这里把图表做成一等结构化对象：

  · `图表登记.json` 是唯一事实源，每条带稳定 `id`（与编号、章节顺序无关）；
  · 正文只放锚点：图片 alt = `fig:xxx`，题注行尾 = `<!--rk:fig:xxx-->`；
  · 编号由工具按「锚点在正文里首次出现的顺序」算，渲染时才写进 Markdown；
  · 删图 / 重排章节 ⇒ 调 `renumber()` 一次，全篇题注与交叉引用自动重编；
  · 每步写后读回，`verify()` 做五连核对，缺哪一环就点名哪一环。

## 边界

  · 纯标准库、纯只读为主；写登记只在 `save()`，且写完立刻读回比对。
  · **向后兼容**：不给 `anchor_id` 时 `visual_place` 完全走旧文本路径，
    现有 `visual_desk_test` 一字不动。锚点用图片 alt + HTML 注释，
    Markdown 查看器忽略注释，现有两条 `_RE_CAPTION` 都是前缀匹配，认得出题注。
  · `verify()` 是 advisory 汇总，**不加门、不改交付门语义**。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

STATE_NAME = "图表登记.json"
SCHEMA_VERSION = 1
KINDS = ("figure", "table")
TIERS = ("preview", "publish")
ID_PREFIX = {"figure": "fig:", "table": "tab:"}
ZH = {"figure": "图", "table": "表"}

#: 规格口径单源：工具描述与报错指引共用。
SPEC_GUIDE = (
    "图表登记：entries=[{id, kind, caption, chapter, claim, source, tier, qa}]。"
    "id 用 fig:/tab: 前缀且全场唯一，与编号无关；tier=preview(SVG)/publish(投稿级)。"
    "正文只放锚点（图片 alt 写 id、题注行尾写 <!--rk:id-->），"
    "编号与题注位置由工具算；删图或重排章节后调 renumber 自动重编全篇引用。"
)

#: 题注行尾的身份锚点（HTML 注释，Markdown 渲染器忽略）。
ANCHOR_RE = re.compile(r"<!--\s*rk:((?:fig|tab):[A-Za-z0-9_.:-]+)\s*-->")
#: 图片 alt 位放 id —— 现有 `_RE_IMAGE` 只取路径，天然兼容。
IMG_ID_RE = re.compile(r"!\[((?:fig|tab):[A-Za-z0-9_.:-]+)\]\(([^)]+)\)")
#: 题注行（前缀匹配即可，行尾注释不影响识别）+ 它的身份锚点。
CAP_WITH_ID_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,4}[ \t]*)?(?:\*\*)?(图|表)[ \t]*(\d+)"
    r"[ \t]*[　\s:：、.．].*?<!--\s*rk:((?:fig|tab):[A-Za-z0-9_.:-]+)\s*-->[ \t]*$",
    re.M)
#: 任意图片 / 任意题注行。★ 2026-11-11 R4 真任务坐实：模型整篇
#: `write_file` 重写时会把工具写的 `<!--rk:id-->` 隐藏注释删掉（8 次改写
#: 全部不含锚点），于是登记变孤儿。身份不能再依赖**模型维护的隐藏标记**，
#: 改为靠可见内容特征（图片 source + 题注文本）定位；注释降级为可选增强。
IMG_ANY_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
CAP_ANY_RE = re.compile(
    r"^[ \t]{0,3}(?:#{1,4}[ \t]*)?(?:\*\*)?(图|表)[ \t]*(\d+)"
    r"[ \t]*[　\s:：、.．](.+?)[ \t]*$", re.M)
#: 重编号用：保留原分隔符（`图 1` / `图　1` / `图1` 都原样保留）。
NUMBER_RE = re.compile(r"(图|表)([ \t　]*)(\d+)")


def _clean_src(s: str) -> str:
    return str(s or "").split("#")[0].strip()


def _clean_caption(s: str) -> str:
    return re.sub(r"<!--.*?-->", "", str(s or "")).strip()

REQUIRED = ("id", "kind", "caption", "chapter", "source")


class RegistryError(ValueError):
    """登记表不能用。消息直接给调用方看，不静默造一份空的。"""


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "entries": []}


def validate(reg: dict) -> tuple[dict, list[str]]:
    """校验并归一：字段齐、id 唯一且前缀对、kind/tier 合法。"""
    if not isinstance(reg, dict):
        raise RegistryError("登记表必须是一个对象。")
    raw = reg.get("entries")
    if not isinstance(raw, list):
        raise RegistryError("登记表缺 entries 数组。")
    out, seen, errors = [], set(), []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            errors.append(f"entries[{i}] 不是对象")
            continue
        miss = [k for k in REQUIRED if not str(e.get(k) or "").strip()]
        if miss:
            errors.append(f"entries[{i}] 缺字段：{', '.join(miss)}")
            continue
        eid = str(e["id"]).strip()
        kind = str(e["kind"]).strip()
        if kind not in KINDS:
            errors.append(f"{eid}: kind 只能是 figure/table")
            continue
        if not eid.startswith(ID_PREFIX[kind]):
            errors.append(f"{eid}: id 前缀应为 {ID_PREFIX[kind]}")
            continue
        if eid in seen:
            errors.append(f"{eid}: id 重复")
            continue
        seen.add(eid)
        tier = str(e.get("tier") or "preview").strip()
        if tier not in TIERS:
            errors.append(f"{eid}: tier 只能是 preview/publish")
            continue
        out.append({"id": eid, "kind": kind,
                    "caption": str(e["caption"]).strip(),
                    "chapter": str(e["chapter"]).strip(),
                    "claim": str(e.get("claim") or "").strip(),
                    "source": str(e["source"]).strip(),
                    # 表格正文（markdown）。**模型自己写的内容**，所有权层
                    # 整篇覆盖后据此把表补回，不编造单元格。figure 恒为空。
                    "body": str(e.get("body") or "").strip(),
                    "tier": tier, "qa": dict(e.get("qa") or {})})
    return {"schema_version": SCHEMA_VERSION, "entries": out}, errors


def load(root: "Path | str") -> tuple[dict, str]:
    """读登记。缺席/损坏都如实说出口，**绝不静默造一份空表当通过**。"""
    p = Path(root) / STATE_NAME
    if not p.is_file():
        return _empty(), "absent"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        reg, errors = validate(raw)
    except Exception as ex:                                   # noqa: BLE001
        return _empty(), f"error:{type(ex).__name__}:{ex}"
    return reg, ("ok" if not errors else "error:" + ";".join(errors[:3]))


def save(root: "Path | str", reg: dict) -> dict:
    """写登记 + 写后读回逐字节等价比对。失败不改盘上的既有登记。"""
    norm, errors = validate(reg)
    if errors:
        return {"ok": False, "error": "登记校验未过：" + "；".join(errors[:4])}
    p = Path(root) / STATE_NAME
    payload = json.dumps(norm, ensure_ascii=False, indent=2)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(payload, encoding="utf-8", newline="\n")
        back = json.loads(p.read_text(encoding="utf-8"))
    except Exception as ex:                                   # noqa: BLE001
        return {"ok": False, "error": f"登记写盘/读回失败：{type(ex).__name__}: {ex}"}
    return {"ok": back == norm, "path": str(p), "bytes": p.stat().st_size,
            "readback_equal": back == norm, "n_entries": len(norm["entries"])}


def upsert(root: "Path | str", entry: dict) -> dict:
    """新增或按 id 覆盖一条，随后重算正文编号（同 id 幂等）。"""
    reg, status = load(root)
    if status.startswith("error:"):
        return {"ok": False, "error": f"现有登记表读不回来：{status}"}
    one, errors = validate({"entries": [entry]})
    if errors:
        return {"ok": False, "error": "；".join(errors)}
    item = one["entries"][0]
    rest = [e for e in reg["entries"] if e["id"] != item["id"]]
    rest.append(item)
    rep = save(root, {"entries": rest})
    rep["id"] = item["id"]
    rep["reused"] = any(e["id"] == item["id"] for e in reg["entries"])
    return rep


def delete(root: "Path | str", eid: str) -> dict:
    reg, status = load(root)
    if status.startswith("error:"):
        return {"ok": False, "error": f"现有登记表读不回来：{status}"}
    left = [e for e in reg["entries"] if e["id"] != str(eid)]
    if len(left) == len(reg["entries"]):
        return {"ok": False, "error": f"登记里没有 {eid}，没动盘上文件"}
    rep = save(root, {"entries": left})
    rep["removed"] = str(eid)
    return rep


def figure_locs(text: str, reg: dict) -> tuple[dict[str, int], dict[str, str]]:
    """在正文里给每条登记找**首次出现位置**与定位依据。

    四级证据，优先级按位置取最早：
      `comment`（隐藏注释，可选） → `img_id`（alt 放 id）
      → `img_src`（图片路径 == 登记的 source） → `caption`（题注文本含 caption）

    后两级是**可见内容**，模型整篇重写时通常会保留，所以覆盖不再让身份丢失。
    返回 `(pos, how)`；`how` 写进回包，方便人核对「这条是靠什么认出来的」。
    """
    t = str(text or "")
    pos: dict[str, int] = {}
    how: dict[str, str] = {}
    by_id = {e["id"]: e for e in reg["entries"]}

    def put(eid: str, p: int, kind: str) -> None:
        if eid not in pos or p < pos[eid]:
            pos[eid], how[eid] = p, kind

    for m in ANCHOR_RE.finditer(t):
        put(m.group(1), m.start(), "comment")
    for m in IMG_ID_RE.finditer(t):
        put(m.group(1), m.start(), "img_id")
    for m in IMG_ANY_RE.finditer(t):
        src = _clean_src(m.group(2))
        if not src:
            continue
        for eid, e in by_id.items():
            if src == _clean_src(e["source"]):
                put(eid, m.start(), "img_src")
    for m in CAP_ANY_RE.finditer(t):
        cap = _clean_caption(m.group(3))
        if not cap:
            continue
        for eid, e in by_id.items():
            if e["caption"] and e["caption"] in cap:
                put(eid, m.start(), "caption")
    return pos, how


def anchor_order(text: str, reg: dict) -> list[str]:
    """按图在正文里**首次出现的位置**排序 —— 章节重排后顺序自然跟着变。

    定位不到就排在最后（会被 `verify` 报 REGISTRY_ANCHOR_MISSING），不猜位置。
    """
    pos, _ = figure_locs(str(text or ""), reg)
    ids = [e["id"] for e in reg["entries"]]
    return sorted(ids, key=lambda i: (pos.get(i, 10 ** 9), ids.index(i)))


def numbering(text: str, reg: dict) -> dict[str, int]:
    """图 / 表各自按锚点顺序编号 1..N。**不持久化**，天然不会两处漂移。"""
    out, counters = {}, {"figure": 0, "table": 0}
    by_id = {e["id"]: e for e in reg["entries"]}
    for i in anchor_order(text, reg):
        e = by_id.get(i)
        if e is None:
            continue
        counters[e["kind"]] += 1
        out[i] = counters[e["kind"]]
    return out


def orphan_ids(text: str, reg: dict) -> list[str]:
    """正文里有锚点、登记里却没有的 id —— 「删了登记没删正文」必须说出口。"""
    t = str(text or "")
    known = {e["id"] for e in reg["entries"]}
    found = {m.group(1) for m in ANCHOR_RE.finditer(t)}
    found |= {m.group(1) for m in IMG_ID_RE.finditer(t)}
    return sorted(found - known)


def renumber(text: str, reg: dict) -> tuple[str, dict]:
    """删图 / 重排章节后重编全篇题注与交叉引用。

    ★ 一次性 `re.sub` 查表替换，不是逐条 sed：避免 3→2、2→1 的链式冲突。
    """
    t = str(text or "")
    nums = numbering(t, reg)
    by_id = {e["id"]: e for e in reg["entries"]}
    old: dict[str, int] = {}
    for m in CAP_WITH_ID_RE.finditer(t):
        old[m.group(3)] = int(m.group(2))
    # ★ 无隐藏注释的题注按**可见题注文本**反查登记。R4 之后这是常态：
    #   模型整篇重写会删注释，但题注文字和图片链接会留下。
    for m in CAP_ANY_RE.finditer(t):
        cap = _clean_caption(m.group(3))
        if not cap:
            continue
        for eid, e in by_id.items():
            if eid not in old and e["caption"] and e["caption"] in cap:
                old[eid] = int(m.group(2))
    orphans = orphan_ids(t, reg)
    trans: dict[tuple[str, int], int] = {}
    for eid, new_n in nums.items():
        e = by_id.get(eid)
        if e and eid in old:
            trans[(e["kind"], old[eid])] = new_n

    def sub(m: re.Match) -> str:
        k = "figure" if m.group(1) == "图" else "table"
        n = int(m.group(3))
        return f"{m.group(1)}{m.group(2)}{trans.get((k, n), n)}"

    # ⚠️ 混合稿（既有带锚点题注、也有老式无锚点题注）不能全篇替换编号：
    #    老式稿的「图 2」和登记里的「图 2」撞号时会被误改。只重编带锚点
    #    的题注行，并**明说**「老式引用没跟着变」，不假装已全篇重编。
    # mixed 的判据不是「有没有注释」，而是「有没有**认不出来**的老式题注」：
    # 无注释但能按 caption 反查到登记的题注属于本工具管的稿，照样全篇重编。
    unmapped = [m for m in CAP_ANY_RE.finditer(t)
                if not any(e["caption"] and
                           e["caption"] in _clean_caption(m.group(3))
                           for e in by_id.values())]
    mixed = bool(unmapped)
    if mixed:
        def cap_sub(m: re.Match) -> str:
            want = nums.get(m.group(3))
            if want is None:
                return m.group(0)
            return NUMBER_RE.sub(
                lambda x: f"{x.group(1)}{x.group(2)}{want}", m.group(0), count=1)
        new = CAP_WITH_ID_RE.sub(cap_sub, t)
    else:
        new = NUMBER_RE.sub(sub, t)

    # 写后读回：每条题注行的编号必须等于登记算出来的编号。
    seen = {m.group(3): int(m.group(2)) for m in CAP_WITH_ID_RE.finditer(new)}
    for m in CAP_ANY_RE.finditer(new):
        cap = _clean_caption(m.group(3))
        for eid, e in by_id.items():
            if eid not in seen and e["caption"] and e["caption"] in cap:
                seen[eid] = int(m.group(2))
    drift = sorted(eid for eid, want in nums.items()
                   if eid in seen and seen[eid] != want)
    note = ""
    if orphans:
        note = "正文里还有未登记的锚点，编号没敢全改：" + "、".join(orphans[:4])
    elif mixed:
        note = "混合稿：只重编了带锚点的题注行，老式题注与引用未改（不冒充全篇重编）"
    return new, {"ok": not drift and not orphans, "changed": new != t,
                 "numbering": nums, "old_numbering": old,
                 "drift": drift, "orphan_ids": orphans,
                 "n_replaced": len(trans), "mixed_mode": mixed,
                 "note": note}


def render_markdown(text: str, reg: dict) -> tuple[str, dict]:
    """把锚点稿渲染成**标准 Markdown**（给 typeset / scan_visuals 吃）。

    图片 alt 换成 `图 N`、题注行尾的 HTML 注释去掉；编号仍由登记算，
    模型一个编号都不用写。
    """
    t = str(text or "")
    nums = numbering(t, reg)
    by_id = {e["id"]: e for e in reg["entries"]}

    unknown: list[str] = []

    def img(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        eid2, e = alt, by_id.get(alt)
        if e is None:
            # 模型整篇重写后 alt 往往变回「图 1」——按可见的 source 反查。
            src_c = _clean_src(src)
            for cand_id, cand in by_id.items():
                if src_c and src_c == _clean_src(cand["source"]):
                    eid2, e = cand_id, cand
                    break
        if e is None:
            # 只有「alt 长得像 id 却查不到」才算异常；普通图片保持原样。
            if str(alt).startswith(("fig:", "tab:")):
                unknown.append(alt)
            return m.group(0)
        return f"![{ZH[e['kind']]} {nums.get(eid2, 0)}]({src})"

    new = IMG_ANY_RE.sub(img, t)
    new = ANCHOR_RE.sub("", new)
    return new, {"ok": not unknown, "n_anchors": len(nums),
                 "numbering": nums, "unknown_ids": unknown,
                 "bytes": len(new.encode("utf-8")),
                 "note": ("有锚点不在登记里，未渲染：" + "、".join(unknown[:4]))
                         if unknown else ""}


def _asset_ok(root: Path, rel: str) -> bool:
    s = str(rel or "").split("#")[0].strip()
    # 正文内 markdown 表没有独立产物文件；显式标记，不假装有一条路径。
    if s == "(inline)" or s.startswith(("http://", "https://", "data:")):
        return True                      # 外链由别的门管，这里只量本地产物
    try:
        q = (root / s).resolve()
        base = root.resolve()
        return (base in q.parents or q == base) and q.is_file()
    except Exception:                                        # noqa: BLE001
        return False


def verify(root: "Path | str", text: str, reg: dict,
           rendered: str = "") -> dict:
    """★ 五连读回：登记 → 盘 → 锚点 → 渲染成品 → 编号一致。

    缺哪一环就报哪一环的 code；`unknown`（没跑到的环节）不算通过。
    """
    t = str(text or "")
    norm, errors = validate(reg)
    gaps: list[dict] = []
    for msg in errors:
        gaps.append({"code": "REGISTRY_INVALID", "level": "hard",
                     "detail": msg, "action": "修登记字段后重跑"})

    nums = numbering(t, norm)
    locs, how = figure_locs(t, norm)
    cap_n: dict[str, int] = {}
    for m in CAP_WITH_ID_RE.finditer(t):
        cap_n[m.group(3)] = int(m.group(2))
    for m in CAP_ANY_RE.finditer(t):
        cap = _clean_caption(m.group(3))
        for e in norm["entries"]:
            if (e["id"] not in cap_n and e["caption"]
                    and e["caption"] in cap):
                cap_n[e["id"]] = int(m.group(2))

    for e in norm["entries"]:
        eid = e["id"]
        if not _asset_ok(Path(root), e["source"]):
            gaps.append({"code": "REGISTRY_ASSET_MISSING", "level": "hard",
                         "detail": f"{eid} 的产物 {e['source']} 不在盘上",
                         "action": "重新出图，或修 source 路径"})
        if eid not in locs:
            gaps.append({"code": "REGISTRY_ANCHOR_MISSING", "level": "hard",
                         "detail": f"{eid} 在正文里找不到图块或题注"
                                   "（按 id / 图片路径 / 题注文本都没定位到）",
                         "action": "重新 visual_place 把图块与题注补回正文"})
        if eid in cap_n and cap_n[eid] != nums.get(eid):
            gaps.append({"code": "REGISTRY_NUMBER_DRIFT", "level": "hard",
                         "detail": f"{eid} 题注写的是 {cap_n[eid]}，登记算的是 {nums.get(eid)}",
                         "action": "调 renumber() 重编全篇，别手改编号"})
        if rendered:
            z, n = ZH[e["kind"]], nums.get(eid)
            need_src = e["source"] != "(inline)"
            if (f"{z} {n}" not in rendered
                    or (need_src and e["source"] not in rendered)):
                gaps.append({"code": "REGISTRY_RENDER_MISSING", "level": "hard",
                             "detail": f"{eid} 没真嵌进排版成品（编号或产物路径缺席）",
                             "action": "让排版台从登记取图元，别从正文另猜一遍"})

    # ④b 正文里有锚点、登记里却没有 ⇒ 「删了登记没删正文」不能算过。
    for oid in orphan_ids(t, norm):
        gaps.append({"code": "REGISTRY_ORPHAN_ANCHOR", "level": "hard",
                     "detail": f"正文还留着 {oid} 的锚点，但登记里已无此条",
                     "action": "把正文里这张图的整块删掉，或重新 upsert 回登记"})
    # ⑤b 每张图都得在正文里被引用（题注行不算）—— 结构化版的
    #    VISUAL_NO_BODY_REF，靠 id→编号而不是靠扫描已有题注。
    body = "\n".join(ln for ln in t.splitlines()
                     if not CAP_ANY_RE.match(ln))
    refs = {(m.group(1), int(m.group(3)))
            for m in NUMBER_RE.finditer(body)}
    for e in norm["entries"]:
        n = nums.get(e["id"])
        if n is not None and (ZH[e["kind"]], n) not in refs:
            gaps.append({"code": "REGISTRY_NO_BODY_REF", "level": "hard",
                         "detail": f"{e['id']}（{ZH[e['kind']]} {n}）只有题注，正文没有呼应",
                         "action": "补一句这张图支撑了什么论证"})

    hard = [g for g in gaps if g["level"] == "hard"]
    return {"ok": not hard, "gaps": gaps, "n_entries": len(norm["entries"]),
            "numbering": nums, "locators": how, "advisory": True,
            "scope": "figure-registry",
            "checked": ("entries", "assets", "anchors", "render", "numbering",
                        "orphans", "body_refs"),
            "note": "" if not hard else "五连读回有缺口，见 gaps（advisory，不加门）"}
