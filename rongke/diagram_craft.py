# -*- coding: utf-8 -*-
"""机制图的工艺审计（2026-11-14）。

为什么要单独一层：几何判据只能保证「装得下、不重叠」，保证不了「像期刊插图」。
配色按序号轮、长标签把节点撑成半张图、换行把「磷酸化」断成「磷酸／化」、
回边从图心斜穿过去 —— 这些是**每张图都会犯**的问题，靠人一张一张看是不
现实的（看一张改一张 = 工具本身没变好）。这一层把它们变成可量的规则：
引擎照着规则生成，审计照着规则回查，`diagram_svg` 每次出图都把结论与修法
一起还给 AI，AI 不必猜「这张行不行」。

如实边界：
· 本层是**几何 + 清单**口径，量的是结构，不是审美判断。留白呼吸感、箭头
  亲和度、色相观感仍要看图；接上 playwright 时由 svgq 的七道门补实测层。
· 规则只覆盖现在已经踩到过的坑；没量到的维度不冒充通过（见 SKIP 项）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

#: 尺度均衡的上限。一个 132px 的圆配一个 56px 的方框是合理的（圆是分子、
#: 方框是步骤）；但 3.4 倍以上就说明「某个标签把节点撑爆了」，得折行或拆开。
_MAX_W_RATIO = 3.4
_MAX_H_RATIO = 3.0
#: 末行占行宽的下限。低于这个比例就是孤行（「磷酸／化」），读起来像被切断。
_MIN_LAST_SHARE = 0.30

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/.%]*")


def _rect_hit(a, b, tol: float = 0.5) -> tuple[float, float]:
    """两矩形重叠量（宽, 高）；<=0 表示不相交。"""
    ox = min(a[2], b[2]) - max(a[0], b[0]) - 2 * tol
    oy = min(a[3], b[3]) - max(a[1], b[1]) - 2 * tol
    return ox, oy


def _node_rect(n: dict) -> tuple[float, float, float, float]:
    """节点 box 是 `[x, y, w, h]`，不是 `[x0,y0,x1,y1]`。两处口径混用会静默
    把判据变成「永远相交」或「永远不相交」——2026-11-14 就栽在这里。"""
    x, y, w, h = (float(v) for v in n["box"][:4])
    return x, y, x + w, y + h


def _seg_rect(p, q, r) -> bool:
    """线段与矩形是否相交（Liang–Barsky 裁剪，纯算术，零依赖）。"""
    x0, y0 = p
    dx, dy = q[0] - x0, q[1] - y0
    t0, t1 = 0.0, 1.0
    for pn, qn in ((-dx, x0 - r[0]), (dx, r[2] - x0),
                   (-dy, y0 - r[1]), (dy, r[3] - y0)):
        if pn == 0:
            if qn < 0:
                return False
        else:
            t = qn / pn
            if pn < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def audit(layout: str | Path | dict) -> dict:
    """读几何清单，逐条量工艺规则。返回 ok / items / metrics / must_fix。"""
    if isinstance(layout, dict):
        man = layout
    else:
        man = json.loads(Path(layout).read_text("utf-8"))
    if man.get("panels"):
        # 多 panel：每个 panel 自己那份清单都要过同一套规则。
        # 顶层没有节点几何，直接拿 nodes/edges 量会全空 —— 那是假绿。
        items: list[dict] = []
        for pn in man["panels"]:
            lp = pn.get("layout") or ""
            sub = (json.loads(Path(lp).read_text("utf-8"))
                   if lp and Path(lp).exists() else None)
            if sub is None:
                items.append({"id": "P0", "name": "panel 清单缺失",
                              "level": "FAIL",
                              "detail": f"panel {pn.get('id')} 的 layout 不在盘上",
                              "fix": "重跑 render，别删中间清单。"})
                continue
            for it in audit(sub)["items"]:
                it = dict(it)
                it["id"] = f"{pn['id']}/{it['id']}"
                items.append(it)
        bad = [i for i in items if i["level"] == "FAIL"]
        return {"ok": not bad, "items": items,
                "metrics": {"panels": len(man["panels"])},
                "must_fix": bad,
                "warns": [i for i in items if i["level"] == "WARN"]}
    nodes = man.get("nodes") or []
    edges = man.get("edges") or []
    comps = man.get("compartments") or []
    direction = str(man.get("direction") or "LR")
    items: list[dict] = []

    def add(cid: str, name: str, level: str, detail: str, fix: str = "") -> None:
        items.append({"id": cid, "name": name, "level": level,
                      "detail": detail, "fix": fix})

    # ── P1 配色语义：同区室同色、别按序号轮色 ───────────────────
    by_group: dict[str, set] = {}
    ungrouped: set = set()
    for n in nodes:
        if n.get("group"):
            by_group.setdefault(str(n["group"]), set()).add(str(n["stroke"]))
        else:
            ungrouped.add(str(n["stroke"]))
    bad_g = {g: s for g, s in by_group.items() if len(s) > 1}
    tints = {c for s in by_group.values() for c in s} | ungrouped
    # 上限：每个区室一色 + 判断框一抹 + 墨色；超过就是「按序号轮色」回来了。
    cap = len(by_group) + 2
    if bad_g:
        add("P1", "配色语义", "FAIL",
            f"同一区室出现了多种描边色：{bad_g}",
            "同区室必须同色 —— 颜色表达「在哪」，不表达「第几个」")
    elif len(tints) > cap:
        add("P1", "配色语义", "FAIL",
            f"{len(tints)} 种描边色 > 区室 {len(by_group)} 个 + 2（判断/墨色）",
            "去掉按序号轮色：未分组节点统一墨色")
    else:
        add("P1", "配色语义", "PASS",
            f"{len(by_group)} 个区室各一色（共 {len(tints)} 色），未分组回落墨色")

    # ── P2 尺度均衡：一个标签不许把节点撑成半张图 ────────────────
    if len(nodes) >= 2:
        ws = [float(n["box"][2]) for n in nodes]
        # 圆 / 菱形的高 = 宽（形状决定），不参与「方框高差」这条 ——
        # 混进来会把合理形状判成失衡。方框类才比高。
        hs = [float(n["box"][3]) for n in nodes
              if n["kind"] not in ("circle", "decision")]
        wr = max(ws) / min(ws)
        hr = (max(hs) / min(hs)) if len(hs) >= 2 else 1.0
        big = max(nodes, key=lambda n: float(n["box"][2]))
        if wr > _MAX_W_RATIO or hr > _MAX_H_RATIO:
            add("P2", "尺度均衡", "FAIL",
                f"节点宽相差 {wr:.1f} 倍、方框高相差 {hr:.1f} 倍（上限 "
                f"{_MAX_W_RATIO:g}/{_MAX_H_RATIO:g}）；最大的是 "
                f"{big['id']}「{big['label']}」{big['box'][2]:.0f}px",
                "折行或缩短这个标签；必要时拆成两个节点")
        else:
            add("P2", "尺度均衡", "PASS",
                f"宽 {min(ws):.0f}–{max(ws):.0f}px（{wr:.1f} 倍）、"
                f"方框高 {min(hs):.0f}–{max(hs):.0f}px（{hr:.1f} 倍）"
                if hs else f"宽 {min(ws):.0f}–{max(ws):.0f}px（{wr:.1f} 倍）")

    # ── P3 折行质量：不拆英文词、不留孤字行 ─────────────────────
    bad_wrap: list[str] = []
    for n in nodes:
        lines = [str(x) for x in (n.get("lines") or [])]
        label = str(n.get("label") or "")
        if len(lines) < 2:
            continue
        joined = "".join(lines)
        for w in set(_WORD.findall(label)):
            if w and w not in joined:      # 被拆开：拼回去也不连续
                bad_wrap.append(f"{n['id']} 的「{w}」被拆断")
        if len(lines[-1]) == 1 and len(label) > 1:
            bad_wrap.append(f"{n['id']} 末行孤字「{lines[-1]}」")
    if bad_wrap:
        add("P3", "折行质量", "FAIL", "；".join(bad_wrap[:4]),
            "换行要在词与短语边界断，末行不留孤字")
    else:
        add("P3", "折行质量", "PASS",
            f"{sum(1 for n in nodes if len(n.get('lines') or []) > 1)} 个节点折行，"
            "英文词完整、无孤字行")

    # ── P4 标签压线：边标签不许落在别的连线/节点上 ───────────────
    hits: list[str] = []
    for e in edges:
        lb = e.get("label_box")
        if not lb:
            continue
        for o in edges:
            if o["k"] == e["k"]:
                continue            # 自己的线由白底光晕负责可读性
            pts = o.get("points") or []
            if any(_seg_rect(tuple(pts[i]), tuple(pts[i + 1]), lb)
                   for i in range(len(pts) - 1)):
                hits.append(f"「{e['label']}」压在 {o['from']}→{o['to']} 的线上")
                break
        for n in nodes:
            ox, oy = _rect_hit(lb, _node_rect(n))
            if ox > 0 and oy > 0:
                hits.append(f"「{e['label']}」压进节点 {n['id']} 框")
                break
    # 标签**之间**也不许互压：svgq 的碰撞门只看「文字压图形／压线」，
    # 两个标签叠成一摞时七道门是 PASS 的（2026-11-14 实测：把标签宽度估成
    # 0.25 倍、三个标签叠在一起，碰撞门仍 PASS）。这一条补上那个洞。
    for i in range(len(edges)):
        a = edges[i].get("label_box")
        if not a:
            continue
        for j in range(i + 1, len(edges)):
            b = edges[j].get("label_box")
            if not b:
                continue
            ox, oy = _rect_hit(a, b, tol=1.0)
            if ox > 0 and oy > 0:
                hits.append(f"「{edges[i]['label']}」与「{edges[j]['label']}」标签互压 "
                            f"{ox:.0f}×{oy:.0f}px")
    if hits:
        add("P4", "标签压线", "FAIL", "；".join(hits[:4]),
            "缩短这条边标签，或把这条边挪到相邻空档")
    else:
        add("P4", "标签压线", "PASS",
            f"{sum(1 for e in edges if e.get('label_box'))} 个边标签都落在净空里")

    # ── P5 区室框不许互相套 ─────────────────────────────────────
    # 判的是**真互套**：重叠面积超过小框的 6%。两块虚线框在边界上蹭一两像素
    # 是正常排版（区室本来就该挨着），拿它判红只会让 AI 反复重画也过不了。
    nests: list[str] = []
    interleaved: list[str] = []
    glayers: dict[str, set] = {}
    for n in nodes:
        if n.get("group") and n.get("layer") is not None:
            glayers.setdefault(str(n["group"]), set()).add(int(n["layer"]))
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            a, b = comps[i]["box"], comps[j]["box"]
            ox, oy = _rect_hit(a, b, tol=1.0)
            if ox <= 0 or oy <= 0:
                continue
            small = min((a[2] - a[0]) * (a[3] - a[1]),
                        (b[2] - b[0]) * (b[3] - b[1]))
            if small <= 0 or ox * oy / small <= 0.06:
                continue
            ga, gb = comps[i]["id"], comps[j]["id"]
            # 两组的节点**占着同一层** ⇒ 框怎么裁都会叠，这是层序问题
            # （分子链的深度会把「胞质」排到「核」后面），不是裁框能修的。
            if glayers.get(ga, set()) & glayers.get(gb, set()):
                interleaved.append(f"{ga} 与 {gb} 共占第 "
                                   f"{sorted(glayers[ga] & glayers[gb])[0] + 1} 层"
                                   f"（重叠 {ox*oy/small:.0%}）")
            else:
                nests.append(f"{ga} 与 {gb} 重叠 {ox:.0f}×{oy:.0f}px"
                             f"（{ox*oy/small:.0%}）")
    if nests:
        add("P5", "区室不嵌套", "FAIL", "；".join(nests[:3]),
            "同层撞上的两块框要按成员边界裁到中缝（引擎已裁，裁不动说明成员本身压着）")
    elif interleaved:
        add("P5", "区室不嵌套", "WARN", "；".join(interleaved[:3]),
            "区室层序交错：把这两组的节点改成按物理先后排列（或拆成两张图），"
            "否则框只能叠着画")
    else:
        add("P5", "区室不嵌套", "PASS",
            f"{len(comps)} 个区室框无实质重叠")

    # ── P6 回边不许穿过节点框 ───────────────────────────────────
    # 以前量的是「折点是否落在内容带外」—— 那是个全局带，宽图/竖图必然误红。
    # 真正的缺陷只有一个：线穿过某个节点框（读者会把节点当成线路的一部分）。
    cross_hits: list[str] = []
    for e in edges:
        if not e.get("reversed"):
            continue
        pts = [tuple(p) for p in (e.get("points") or [])]
        for n in nodes:
            if n["id"] in (e["from"], e["to"]):
                continue
            if any(_seg_rect(pts[i], pts[i + 1], _node_rect(n))
                   for i in range(len(pts) - 1)):
                cross_hits.append(f"{e['from']}→{e['to']} 穿过节点 {n['id']}")
                break
    if cross_hits:
        add("P6", "回边不穿节点", "FAIL", "；".join(cross_hits[:3]),
            "回边默认走边侧；通道若仍撞节点，就给 route=top 或把节点挪开")
    elif any(e.get("reversed") for e in edges):
        add("P6", "回边不穿节点", "PASS",
            f"{sum(1 for e in edges if e.get('reversed'))} 条回边都不穿节点框")
    else:
        add("P6", "回边不穿节点", "SKIP", "没有回边，这一条无处可量")

    fails = [i for i in items if i["level"] == "FAIL"]
    metrics = {
        "colors": len(tints), "groups": len(by_group),
        "w_ratio": round(max(float(n["box"][2]) for n in nodes)
                         / min(float(n["box"][2]) for n in nodes), 2) if nodes else 1.0,
        "n_wrapped": sum(1 for n in nodes if len(n.get("lines") or []) > 1),
        "n_reversed": sum(1 for e in edges if e.get("reversed")),
    }
    return {"ok": not fails, "items": items,
            "must_fix": [i["fix"] for i in fails if i["fix"]],
            "metrics": metrics, "scope": "geometry",
            "honest_limit": "几何口径，未替代浏览器实拍与审美判断"}
