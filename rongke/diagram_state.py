# -*- coding: utf-8 -*-
"""机制图状态单源 —— 把 `diagram_svg` 从「打印机」升级成 AI 可摆弄的操作面。

## 设计裁决（2026-11-12）

与 `aideck/` 的 `画布.json` 同构：图元级 / 稳定 id / 可查询 / 可增量 / 状态持久。
但有一处**刻意不同**——

  画布里 AI 自己摆坐标；机制图的坐标**必须由工具算**（分层布局 + 七道质量门）。
  所以本状态里一个 x / y / bbox 都不存。AI「只改一个节点」改的是**语义**
  （label / kind / group / 边的端点与语义），几何在 render 时整体重算。
  手摆坐标会同时废掉自动布局和质量门，那是把「可增量」买回来、把「画得对」卖掉。

## 状态是事实源，spec 是投影

`project_spec()` 只输出 `svgdiagram._validate()` 认识的字段，把状态内部的
`eid` / `revision` / `schema` 全剥掉 ⇒ **后端零改动**；不传 action 的默认出图
路径**一个字节都不碰状态文件**（默认路径零副作用，有正负控）。

## 与准入六条对齐（`rongke/toolize.py`）

形态由工具算（几何在 svgdiagram）· 规格单源（`ACTION_GUIDE` 在插件里，
枚举真从 svgdiagram 读）· 质量可判定（七道门）· 写后读回（`save`）·
在册 schema 可见（`diagram_svg.action`）· 质量与放行分离
（`gaps` 只 advisory，不改 `ok` / `scope` / `checks`）。

纯标准库，不进 `/api/tools`（不是一个新工具），不改 SYSTEM，不加门。
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

STATE_NAME = "机制图.json"
SCHEMA_VERSION = 1

#: 动作集单源。工具描述读这一份，免得两处漂移。
OPS = ("new", "add_node", "add_edge", "add_group", "set_node", "set_edge",
       "set_canvas", "remove", "query", "render",
       # 2026-11-12 晚按 GitHub 上「AI-native 画布」接口模式补的**操作面协议**：
       # 分层读取 / 意图级编辑 / 事务 / 版本 / 按需披露。
       "describe", "inspect", "guide", "group", "relabel", "connect",
       "batch", "snapshot", "restore",
       # 布局意图（2026-11-12 夜）：AI 说「怎么摆」，工具算几何。
       "layout", "align", "route")

#: 复合 op —— 一条意图展开成一串原子 op，在一次事务里落盘。
COMPOUND_OPS = ("group", "relabel", "connect", "batch")

#: 只读 / 元操作 —— 不改场景状态。
META_OPS = ("describe", "inspect", "guide")

#: 状态里**不许出现**的键。几何是渲染产物，不是事实。
FORBIDDEN_GEOMETRY_KEYS = ("x", "y", "bbox", "box", "cx", "cy", "width_px",
                           "height_px", "left", "top", "pos", "coords",
                           "polys", "center", "size", "layer")

_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


class StateError(ValueError):
    """动作不合法。说清楚为什么，不静默做别的事。"""


def _result(ok: bool, **kw) -> dict:
    return dict({"ok": ok, "advisory": True}, **kw)


def empty() -> dict:
    """空状态。节点 / 边都带稳定 id，边另有 `eid` 供单条增删改。"""
    return {
        "schema": SCHEMA_VERSION,
        "title": "",
        "direction": "LR",
        "width_mm": 180.0,
        "nodes": [],
        "edges": [],
        "groups": [],
        "revision": 0,
        "checkpoints": [],
        "hints": {},
    }


def state_path(path: str | Path = STATE_NAME) -> Path:
    p = Path(path)
    return p if p.is_absolute() else Path.cwd() / p


def save(state: dict, path: str | Path = STATE_NAME) -> dict:
    """写后逐字节读回（同 `litpool.save`）。不认「写成功」这回事。"""
    p = state_path(path)
    payload = json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(payload, encoding="utf-8")
        back = p.read_text(encoding="utf-8")
    except OSError as ex:
        return _result(False, error=f"写/读回失败：{type(ex).__name__}: {ex}",
                       path=str(p))
    return _result(back == payload, path=str(p),
                   readback_equal=(back == payload), chars=len(payload))


def load(path: str | Path = STATE_NAME) -> dict:
    """读不到 / 版本不符 ⇒ 空状态。调用方用 `present` 区分「没有」和「空的」。"""
    p = state_path(path)
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(obj, dict) or obj.get("schema") != SCHEMA_VERSION:
        return empty()
    for k in ("nodes", "edges", "groups"):
        if not isinstance(obj.get(k), list):
            obj[k] = []
    for k, v in (("title", ""), ("direction", "LR"), ("width_mm", 180.0),
                 ("revision", 0), ("checkpoints", []), ("hints", {})):
        if k not in obj:
            obj[k] = v
    # 旧状态（11-12 早于操作面协议）没有 checkpoints，补空即可，
    # 不把整份状态判成 empty —— 身份不能被协议升级抹掉。
    return obj


def _new_id(prefix: str, used: set[str]) -> str:
    i = 1
    while f"{prefix}{i}" in used:
        i += 1
    return f"{prefix}{i}"


def _gap(level: str, code: str, detail: str, fix: str) -> dict:
    return {"level": level, "code": code, "detail": detail, "action": fix}


def gaps(state: dict) -> list[dict]:
    """结构缺口。`hard` = 会让 `_validate` 抛错；`soft` = 只提示，不拦动作。

    枚举**真从 svgdiagram 读**，不在本模块抄一份（规格单源）。
    """
    from rongke import svgdiagram as D

    out: list[dict] = []
    nodes = state.get("nodes") or []
    edges = state.get("edges") or []
    groups = state.get("groups") or []
    nids = [str(n.get("id") or "") for n in nodes]
    nid_set = set(nids)
    gids = {str(g.get("id") or "") for g in groups}

    if not nids:
        out.append(_gap("hard", "EMPTY_DIAGRAM", "一个节点都没有",
                        "先 add_node；机制图至少要有一个节点。"))
    for nid in sorted({x for x in nids if nids.count(x) > 1}):
        out.append(_gap("hard", "DUP_NODE_ID", f"节点 id 重复：{nid}",
                        "换一个 id，或用 set_node 改已有那个。"))
    eids = [str(e.get("eid") or "") for e in edges]
    for eid in sorted({x for x in eids if eids.count(x) > 1}):
        out.append(_gap("hard", "DUP_EDGE_ID", f"边 id 重复：{eid}",
                        "换一个 eid，或用 set_edge 改已有那条。"))
    gids_list = [str(g.get("id") or "") for g in groups]
    for gid in sorted({x for x in gids_list if gids_list.count(x) > 1}):
        out.append(_gap("hard", "DUP_GROUP_ID", f"区室 id 重复：{gid}",
                        "换一个 id，或删掉重复那条。"))
    for n in nodes:
        if str(n.get("kind") or "") not in D.NODE_KINDS:
            out.append(_gap("hard", "BAD_NODE_KIND",
                            f"节点 {n.get('id')} 的 kind={n.get('kind')}",
                            "改成 svgdiagram.NODE_KINDS 里的一种。"))
        if n.get("group") and str(n["group"]) not in gids:
            out.append(_gap("soft", "UNDECLARED_GROUP",
                            f"节点 {n.get('id')} 的区室 {n.get('group')} 未声明",
                            "add_group 声明一个；不声明后端也会按 id 自动补框。"))
    used: set[str] = set()
    for e in edges:
        f, t = str(e.get("from") or ""), str(e.get("to") or "")
        used.add(f)
        used.add(t)
        if f not in nid_set or t not in nid_set:
            out.append(_gap("hard", "DANGLING_EDGE",
                            f"边 {e.get('eid')} 指向不存在的节点：{f}→{t}",
                            "改端点，或 remove 这条边。"))
        elif f == t:
            out.append(_gap("hard", "SELF_LOOP", f"边 {e.get('eid')} 是自环",
                            "加一个中间节点把回路表达出来。"))
        if str(e.get("kind") or "") not in D.EDGE_KINDS:
            out.append(_gap("hard", "BAD_EDGE_KIND",
                            f"边 {e.get('eid')} 的 kind={e.get('kind')}",
                            "改成 svgdiagram.EDGE_KINDS 里的一种。"))
    for nid in nids:
        if nids and nid not in used:
            out.append(_gap("soft", "ISOLATED_NODE", f"节点 {nid} 没有连线",
                            "确认它是不是该单独存在；加边用 add_edge。"))
    if str(state.get("direction") or "").upper() not in D.DIRECTIONS:
        out.append(_gap("hard", "BAD_DIRECTION",
                        f"direction={state.get('direction')}",
                        "改成 LR 或 TB。"))
    try:
        if float(state.get("width_mm") or 0) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        out.append(_gap("hard", "BAD_WIDTH",
                        f"width_mm={state.get('width_mm')!r}",
                        "给一个正数（声明物理宽度，单位 mm）。"))
    return out


def hard_gaps(state: dict) -> list[dict]:
    return [g for g in gaps(state) if g["level"] == "hard"]


def project_spec(state: dict) -> dict:
    """状态 → spec。剥掉所有内部字段，只留 `_validate` 认识的那一组。"""
    return {
        "nodes": [{"id": str(n.get("id") or ""),
                   "label": str(n.get("label") or n.get("id") or ""),
                   "kind": str(n.get("kind") or "process"),
                   "group": str(n.get("group") or ""),
                   "icon": str(n.get("icon") or "")} for n in state["nodes"]],
        "edges": [{"from": str(e.get("from") or ""),
                   "to": str(e.get("to") or ""),
                   "label": str(e.get("label") or ""),
                   "kind": str(e.get("kind") or "arrow")} for e in state["edges"]],
        "groups": [{"id": str(g.get("id") or ""),
                    "label": str(g.get("label") or g.get("id") or ""),
                    "kind": str(g.get("kind") or "box")}
                   for g in state["groups"]],
        "direction": state.get("direction", "LR"),
        "title": state.get("title", ""),
        "width_mm": state.get("width_mm", 180.0),
    }


def assert_no_geometry(state: dict) -> None:
    """结构判据用：状态里不许长出任何几何键（不是拿字符串去文案里找）。"""
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in FORBIDDEN_GEOMETRY_KEYS:
                    raise StateError(f"状态里混进了几何字段 `{k}`：几何必须由工具算。")
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(state)


def status_line(state: dict) -> str:
    """advisory 一行。读数是下界，不是质量结论。"""
    gs = gaps(state)
    soft = [g["code"] for g in gs if g["level"] == "soft"]
    tail = f" · soft 缺口 {len(soft)} 处（{'、'.join(soft)}）" if soft else ""
    return (f"机制图状态：{len(state['nodes'])} 节点 / {len(state['edges'])} 边 / "
            f"{len(state['groups'])} 区室 · rev {state.get('revision', 0)}{tail}")


def _next_for(op: str, state: dict, gs: list[dict]) -> str:
    soft = [g["code"] for g in gs if g["level"] == "soft"]
    if op == "query":
        base = ("拿到 id 后用 set_node / set_edge / remove 只改一个图元；"
                "不用重写整份 spec。要出图就发 {\"op\":\"render\"}。")
    elif op == "render":
        base = ("spec 是状态的投影，几何由工具重算；不要手改 SVG 字节，"
                "也不要把坐标写回状态。要改一处就发 {\"op\":\"set_node\"} / "
                "{\"op\":\"set_edge\"} 只动一个图元，或先 {\"op\":\"query\"} "
                "看现在有什么；红项按 next 改语义再 render，别去压字号。")
    else:
        base = ("状态已存。改一处继续用 set_node / set_edge / add_* / remove；"
                "问「现在有什么」用 query；出图用 render。")
    if soft:
        base += f"\n⚠️ soft 缺口（不拦出图）：{'、'.join(soft)}。"
    return base


# ══════════════════════════════════════════════════════════════════
# 操作面协议（2026-11-12 晚，按 GitHub 调研落地）
# ══════════════════════════════════════════════════════════════════
# 参照 286486/kalamo《MCP 设计/绘图/画布类工具接口模式》、
# yashagw/excalidraw-boards/docs/mcp.md、QuekenouLn/excalidraw-mcp、
# tldraw Agent Starter Kit。四条硬原则：
#   ① 显式回执：每次写回 affected ids + field diff + revision，AI 拿回执
#      继续下一步，不靠自己记（Figma use_figma 把它写成协议级要求）。
#   ② 分层读取：describe 摘要 / inspect 选区 / query 全量，
#      别一上来把整份 JSON 塞进上下文（get_metadata 稀疏 XML 口径）。
#   ③ 意图级编辑：「把这三个归到胞质」= 一批原子 op 一次事务，
#      不是让 AI 循环发三次 set_node；失败整笔回滚（Canva 事务三段式）。
#   ④ 双通道反馈：结构化状态 + 语义摘要一起回；写后校验不是可选项。
# 反面参照：tldraw Make Real 是「截图→LLM→HTML→画布」单向闭环，
# 不是结构化读写；本模块不做那条路。

GUIDE = """机制图操作面（给 AI 用的场景编辑器，不是一次性出图器）：
读取：describe 一句话摘要 / inspect 按 ids 看细节 / query 全量。
写入：new / add_node / add_edge / add_group / set_node / set_edge /
      set_canvas / remove；意图级 group（批量归组）/ relabel（批量改标签）/
      connect（按名字连边）/ batch（一串 op 一次事务）。
版本：snapshot 存检查点 / restore 回滚；写 op 可带 expected_revision 做乐观锁。
布局意图：layout {direction?,order?} / align {ids} / route {id,via:top|bottom}
      —— 状态里只存意图，几何仍由工具算；route 让回边走上方/下方通道，
      align 让一组节点在层内相邻。发 describe 看现状，再决定怎么摆。
出图：render 从状态投影 spec + hints，几何与七道质量门由工具算，状态里不存坐标。
每个写回包都带 affected（受影响 id）、diff（逐字段 before/after）、
revision、describe（语义摘要）、next（下一步）：拿回执继续，别凭记忆。
记不住就发 {"op":"guide"} 重新拿这份口径，不必把全文塞进工具描述。"""


def describe_scene(state: dict, *, detailed: bool = False) -> str:
    """语义摘要。借鉴 describe_scene / get_metadata：**分层读**的第一层。"""
    nodes = state.get("nodes") or []
    edges = state.get("edges") or []
    groups = state.get("groups") or []
    title = state.get("title") or "未命名机制图"
    soft = sorted({g["code"] for g in gaps(state) if g["level"] == "soft"})
    head = (f"{title} · {state.get('direction', 'LR')} · "
            f"rev {state.get('revision', 0)} · "
            f"{len(nodes)} 节点 / {len(edges)} 边 / {len(groups)} 区室")
    if soft:
        head += f" · {len(soft)} 处 soft 缺口（{'、'.join(soft)}）"
    if not detailed:
        return head
    by_group: dict[str, list[str]] = {}
    for n in nodes:
        by_group.setdefault(str(n.get("group") or "未分组"), []).append(
            f"{n.get('id')}={n.get('label')}")
    lines = [head]
    for g, names in by_group.items():
        lines.append(f"  [{g}] " + "；".join(names[:6])
                     + ("…" if len(names) > 6 else ""))
    if edges:
        lines.append("  边: " + "；".join(
            f"{e.get('from')}→{e.get('to')}"
            f"({e.get('label') or e.get('kind')})" for e in edges[:8])
            + ("…" if len(edges) > 8 else ""))
    return "\n".join(lines)


def _scene_view(state: dict) -> dict:
    """双通道里的**结构化通道**：ids + 语义摘要，不含几何坐标。

    借鉴 Framelink「服务端先精简再喂模型」：AI 要继续操作只需要 id 和状态，
    不需要整份 JSON；坐标由出图链算，永远不进上下文。
    """
    return {
        "summary": describe_scene(state),
        "node_ids": [str(n.get("id")) for n in state.get("nodes", [])],
        "edge_ids": [str(e.get("eid")) for e in state.get("edges", [])],
        "group_ids": [str(g.get("id")) for g in state.get("groups", [])],
        "revision": int(state.get("revision", 0)),
    }


def compile_hints(state: dict) -> dict:
    """把状态里的**布局意图**编译成出图链能吃的 hints（eid → 边序号）。

    状态里存的是 AI 能理解的稳定 id；`svgdiagram._layout` 只认边在 spec
    里的序号。这层翻译刻意放在投影侧，不让 AI 去记内部索引。
    找不到的边进 `unresolved`，明说，不静默丢。
    """
    h = state.get("hints") or {}
    idx = {str(e.get("eid")): k
           for k, e in enumerate(state.get("edges") or [])}
    route: dict[str, str] = {}
    unresolved: list[str] = []
    for eid, via in (h.get("route") or {}).items():
        eid = str(eid)
        if eid not in idx or via not in ("top", "bottom"):
            unresolved.append(eid)
            continue
        route[str(idx[eid])] = via
    align = [list(g) for g in (h.get("align") or [])]
    order = [str(x) for x in (h.get("order") or [])]
    if order:
        align.insert(0, order)     # layout.order = 按给定顺序相邻
    return {"route": route, "align": align,
            "route_by_eid": dict(h.get("route") or {}),
            "unresolved": unresolved}


def _diff(before: dict, after: dict) -> list[dict]:
    """逐字段 diff。回包给模型的是**变了什么**，不是整份状态。"""
    out: list[dict] = []
    for bucket, key in (("nodes", "id"), ("edges", "eid"), ("groups", "id")):
        b = {str(o.get(key, "")): o for o in (before.get(bucket) or [])}
        a = {str(o.get(key, "")): o for o in (after.get(bucket) or [])}
        for k in sorted(set(b) | set(a)):
            if k not in b:
                out.append({"type": bucket, "id": k, "change": "added",
                            "after": a[k]})
            elif k not in a:
                out.append({"type": bucket, "id": k, "change": "removed",
                            "before": b[k]})
            else:
                for f in sorted(set(b[k]) | set(a[k])):
                    if f in ("id", "eid"):
                        continue
                    if b[k].get(f) != a[k].get(f):
                        out.append({"type": bucket, "id": k, "field": f,
                                    "before": b[k].get(f),
                                    "after": a[k].get(f)})
    for f in ("title", "direction", "width_mm", "hints"):
        if before.get(f) != after.get(f):
            out.append({"type": "canvas", "id": "canvas", "field": f,
                        "before": before.get(f), "after": after.get(f)})
    return out


def _affected(diff: list[dict]) -> dict:
    out: dict[str, list[str]] = {"nodes": [], "edges": [],
                                 "groups": [], "canvas": []}
    for d in diff:
        k = d.get("type")
        if k in out and d.get("id") not in out[k]:
            out[k].append(d["id"])
    return out


def _expand(a: dict) -> list[dict] | None:
    """意图级 op → 原子 op 序列。None 表示它本身是原子 op。"""
    op = a.get("op")
    if op == "batch":
        ops = a.get("ops")
        if not isinstance(ops, list) or not ops:
            raise StateError("batch 要给非空 ops 数组。")
        return [dict(o) for o in ops]
    if op == "group":
        ids, gid = a.get("ids"), str(a.get("group") or "").strip()
        if not isinstance(ids, list) or not ids or not gid:
            raise StateError("group 要给 ids 数组与 group 名。")
        return [{"op": "set_node", "id": str(i), "group": gid} for i in ids]
    if op == "relabel":
        targets = a.get("targets")
        if not isinstance(targets, dict) or not targets:
            raise StateError("relabel 要给 targets 对象（id → 新标签）。")
        return [{"op": "set_node", "id": str(k), "label": v}
                for k, v in targets.items()]
    if op == "connect":
        return [{"op": "add_edge", "from": a.get("from"), "to": a.get("to"),
                 "label": a.get("label"), "kind": a.get("kind") or "arrow"}]
    return None


def _parse(action) -> dict:
    if isinstance(action, str):
        try:
            action = json.loads(action)
        except ValueError as ex:
            raise StateError(f"action 不是合法 JSON：{ex}") from ex
    if not isinstance(action, dict):
        raise StateError("action 必须是对象或 JSON 字符串。")
    return copy.deepcopy(action)


def apply(action, path: str | Path = STATE_NAME) -> dict:
    """一个动作 = 一次可读回的状态变更。`query` / `render` 不写盘。

    事务性：先在副本上改 → 量 hard gaps → 有红就**整笔不落盘**，
    绝不留下半截状态。`render` 回包带 `spec`，由工具层接着跑出图 + 七道门。
    """
    p = state_path(path)
    try:
        a = _parse(action)
    except StateError as ex:
        return _result(False, error=str(ex), next="按 ACTION_GUIDE 重发一个动作。")
    op = str(a.pop("op") or "").strip()
    if op not in OPS:
        return _result(False, error=f"未知动作 `{op}`（支持：{'、'.join(OPS)}）",
                       ops=list(OPS), next="按 ACTION_GUIDE 重发一个动作。")
    # state_path 允许动作自带，方便测试与多图并存；不写进状态本身。
    if a.get("state_path"):
        p = state_path(a.pop("state_path"))
    else:
        a.pop("state_path", None)

    present = p.exists()
    state = load(p)

    # ── 乐观锁（Excalidraw MCP 的 expectedRevision）────────────────
    # AI 可能拿着过期的 describe 回执发写操作；不拦就会把别人的改动覆盖掉。
    exp = a.pop("expected_revision", None)
    if exp is not None and present:
        try:
            exp_i = int(exp)
        except (TypeError, ValueError):
            return _result(False, op=op, error="expected_revision 不是整数",
                           revision=state.get("revision", 0))
        if exp_i != int(state.get("revision", 0)):
            return _result(False, op=op,
                           error="REVISION_CONFLICT：状态已被别的动作改过",
                           current_revision=state.get("revision", 0),
                           expected_revision=exp_i,
                           next="先 describe / inspect 重新看当前状态，再按新 revision 重发。")

    # ── 分层读取 / 按需披露（只读，不落盘）────────────────────────
    if op == "guide":
        return _result(True, op=op, guide=GUIDE, ops=list(OPS),
                       compound_ops=list(COMPOUND_OPS),
                       revision=state.get("revision", 0),
                       next="挑一个 op 发；口径记不住随时再发 guide，不必塞进上下文。")
    if op == "describe":
        return _result(True, op=op, present=present,
                       revision=state.get("revision", 0),
                       summary=describe_scene(state),
                       detail=describe_scene(state, detailed=True),
                       gaps=gaps(state),
                       next="看单个元素发 inspect（带 ids）；要全量发 query；改完发 render。")
    if op == "inspect":
        ids = {str(x) for x in (a.get("ids") or [])}
        if not ids:
            return _result(False, op=op,
                           error="inspect 要给 ids 数组（先 describe 看有哪些）")
        elems = []
        for bucket, key in (("nodes", "id"), ("edges", "eid"),
                            ("groups", "id")):
            for o in state.get(bucket, []):
                if str(o.get(key)) in ids:
                    elems.append({"type": bucket[:-1], "id": o.get(key),
                                  "revision": state.get("revision", 0),
                                  "data": copy.deepcopy(o)})
        found = {str(e["id"]) for e in elems}
        return _result(True, op=op, elements=elems,
                       missing=sorted(ids - found),
                       revision=state.get("revision", 0),
                       next="拿到 id 后 set_node/set_edge/remove 只改一个；批量用 group/relabel/batch。")

    # ── 快照 / 回滚（版本，撤销分组）──────────────────────────────
    if op == "snapshot":
        label = str(a.get("label") or
                   f"cp{len(state.get('checkpoints') or []) + 1}")
        draft = copy.deepcopy(state)
        draft["checkpoints"] = list(draft.get("checkpoints") or [])[-4:] + [
            {"id": label, "at_revision": int(draft.get("revision", 0)),
             "state": {k: copy.deepcopy(v) for k, v in draft.items()
                       if k != "checkpoints"}}]
        draft["revision"] = int(state.get("revision", 0)) + 1
        rep = save(draft, p)
        if not rep.get("readback_equal"):
            return _result(False, op=op, error="快照写后读回不一致")
        back = load(p)
        return _result(True, op=op, checkpoint=label,
                       revision=back.get("revision", 0),
                       checkpoints=[c["id"] for c in back["checkpoints"]],
                       summary=describe_scene(back),
                       next="改坏了发 restore 带 id 回滚；或继续 describe 看现状。")
    if op == "restore":
        cid = str(a.get("id") or "").strip()
        cps = state.get("checkpoints") or []
        hit = next((c for c in cps if str(c.get("id")) == cid), None)
        if hit is None:
            return _result(False, op=op, error=f"找不到检查点 `{cid}`",
                           checkpoints=[c.get("id") for c in cps])
        base = copy.deepcopy(hit.get("state") or empty())
        base["checkpoints"] = copy.deepcopy(cps)
        base["revision"] = int(state.get("revision", 0)) + 1
        rep = save(base, p)
        if not rep.get("readback_equal"):
            return _result(False, op=op, error="回滚写后读回不一致")
        back = load(p)
        d = _diff(state, back)
        return _result(True, op=op, restored=cid,
                       revision=back.get("revision", 0),
                       summary=describe_scene(back), diff=d,
                       affected=_affected(d),
                       next="已回滚；发 describe 确认现状。")

    # ── 意图级 / 事务：一条指令展开成一批原子 op，全成才落盘 ──────
    # 参照 Canva 的 start/perform/commit 三段式与 Excalidraw 的
    # allowedElementIds：失败整笔回滚，不留半截状态，也不产生一串撤销记录。
    if op in COMPOUND_OPS:
        try:
            subs = _expand({"op": op, **a}) or []
        except StateError as ex:
            return _result(False, op=op, error=str(ex),
                           next="发 guide 拿口径后重发。")
        base = copy.deepcopy(state)
        # ★ 真原子：整批在**旁路的临时状态**上跑，成功才一次性写回 p。
        # 逐个 sub 直接写 p 会在盘上留下半截中间态（另一个 describe 会读到），
        # 那是假事务；而且一次意图会产生 N 个 revision，回执对不上。
        tmp = p.with_name(p.stem + ".tx" + p.suffix)
        parking = p.with_name(p.stem + ".tx.last" + p.suffix)
        try:
            save(base, tmp)
            for sub in subs:
                r = apply(sub, tmp)
                if not r.get("ok"):
                    return _result(False, op=op, error="事务失败，p 未被改动",
                                   failed_op=sub.get("op"), reason=r.get("error"),
                                   rolled_back=True,
                                   revision=base.get("revision", 0),
                                   next="按 guide 修掉失败的那条 op 后重发整批。")
            final = load(tmp)
            final["revision"] = int(base.get("revision", 0)) + 1
            w = save(final, tmp)                 # 先在旁路写后读回
            if not w.get("readback_equal"):
                return _result(False, op=op, error="事务提交前读回不一致",
                               revision=base.get("revision", 0))
            # ★ 原子提交 = `os.replace` **改名**（hygiene 口径：挪，不删）。
            # 内核里一个 .unlink/.remove 都不许有；改名同盘瞬时，也不留临时文件。
            os.replace(tmp, p)
            back = load(p)                       # 提交后再读回一次
            if back != final:
                return _result(False, op=op, error="事务提交后读回与预期不一致",
                               revision=base.get("revision", 0))
        finally:
            # 异常兜底同样只挪不删；固定停车位同名覆盖，不会无限堆积。
            if tmp.exists():
                try:
                    os.replace(tmp, parking)
                except OSError:
                    pass
        d = _diff(base, back)
        return _result(True, op=op, revision=back.get("revision", 0),
                       ops_executed=len(subs), affected=_affected(d),
                       diff=d, summary=describe_scene(back),
                       gaps=gaps(back),
                       next="事务已提交（一次 revision）；describe 看现状 / render 出图。")

    if op == "query":
        gs = gaps(state)
        fmt = str(a.pop("response_format", "concise") or "concise")
        nxt = _next_for(op, state, gs)
        if not present:
            nxt += ("\n⚠️ 盘上还没有状态文件：发 {\"op\":\"new\"} 建，"
                    "或直接 add_node / add_edge 自动建（不会静默当成已有图）。")
        return _result(True, op=op, present=present, state=copy.deepcopy(state),
                       state_path=str(p), revision=state.get("revision", 0),
                       gaps=gs, status=status_line(state),
                       summary=describe_scene(state, detailed=(fmt == "detailed")),
                       response_format=fmt,
                       scene=_scene_view(state), next=nxt)
    if op == "render":
        gs = gaps(state)
        if hard_gaps(state):
            return _result(False, op=op, present=present, state_path=str(p),
                           gaps=gs, next="先按 gaps[].action 修掉 hard 缺口再 render。",
                           error="状态有 hard 缺口，不投影 spec，更不出图。")
        return _result(True, op=op, present=present, state_path=str(p),
                       revision=state.get("revision", 0), gaps=gs,
                       spec=project_spec(state), scene=_scene_view(state),
                       hints=compile_hints(state),
                       # 双通道：spec 给出图链，scene 给模型继续操作的结构化状态；
                       # 视觉通道（SVG/PNG 路径 + 写后校验）由工具层补 visual。
                       feedback={"structured": "scene / spec / gaps",
                                 "visual": "由工具层在出图后补 SVG/PNG 路径与七道门结论"},
                       status=status_line(state),
                       next=_next_for(op, state, gs))

    bootstrapped = False
    if op == "new":
        draft = empty()
        for k in ("title", "direction", "width_mm"):
            if k in a:
                draft[k] = a[k]
        bootstrapped = True
    else:
        draft = copy.deepcopy(state)
        if not present:
            # 降低调用阻力：第一个动作就是 add_* 也能开工（同「零参数拿模板」）。
            bootstrapped = True

    cascade: list[str] = []
    try:
        if op == "add_node":
            used = {str(n.get("id")) for n in draft["nodes"]}
            nid = str(a.get("id") or "").strip() or _new_id("n", used)
            if not _ID_RE.match(nid):
                raise StateError(f"节点 id `{nid}` 只许字母数字 _ . -")
            icon = str(a.get("icon") or "").strip()
            if icon:
                from rongke import svgbio as _bio
                if icon not in _bio.ICONS:
                    raise StateError(
                        f"icon=`{icon}` 不在图元库里"
                        f"（可选：{'、'.join(sorted(_bio.ICONS))}）。")
            draft["nodes"].append({"id": nid,
                                   "label": str(a.get("label") or nid),
                                   "kind": str(a.get("kind") or "process"),
                                   "group": str(a.get("group") or ""),
                                   "icon": icon})
            a["id"] = nid
        elif op == "add_edge":
            used = {str(e.get("eid")) for e in draft["edges"]}
            eid = str(a.get("eid") or "").strip() or _new_id("e", used)
            draft["edges"].append({"eid": eid,
                                   "from": str(a.get("from") or "").strip(),
                                   "to": str(a.get("to") or "").strip(),
                                   "label": str(a.get("label") or ""),
                                   "kind": str(a.get("kind") or "arrow")})
            a["eid"] = eid
        elif op == "add_group":
            gid = str(a.get("id") or "").strip()
            if not gid:
                raise StateError("add_group 要给 id。")
            draft["groups"].append({"id": gid,
                                    "label": str(a.get("label") or gid),
                                    "kind": str(a.get("kind") or "box")})
        elif op in ("set_node", "set_edge"):
            key, bucket, allowed = (
                ("id", draft["nodes"], ("label", "kind", "group", "icon"))
                if op == "set_node" else
                ("eid", draft["edges"], ("from", "to", "label", "kind")))
            target = str(a.get(key) or "").strip()
            hit = next((o for o in bucket if str(o.get(key)) == target), None)
            if hit is None:
                return _result(False, op=op, error=f"找不到 {key}=`{target}`",
                               present=present, state_path=str(p),
                               next="先 query 拿到真实 id 再改。")
            for k in allowed:
                if k in a:
                    if k == "icon" and str(a[k] or "").strip():
                        from rongke import svgbio as _bio
                        if str(a[k]).strip() not in _bio.ICONS:
                            raise StateError(
                                f"icon=`{a[k]}` 不在图元库里"
                                f"（可选：{'、'.join(sorted(_bio.ICONS))}）。")
                    hit[k] = a[k]
        elif op == "set_canvas":
            for k in ("title", "direction", "width_mm"):
                if k in a:
                    draft[k] = a[k]
        elif op == "layout":
            # 「怎么摆」的意图，不是坐标。direction 直接生效，order 由
            # compile_hints 编成层内相邻约束。
            if "direction" in a:
                draft["direction"] = str(a["direction"]).upper()
            hints = dict(draft.get("hints") or {})
            if "order" in a:
                if not isinstance(a["order"], list):
                    raise StateError("layout.order 要给 id 数组")
                hints["order"] = [str(x) for x in a["order"]]
            draft["hints"] = hints
        elif op == "align":
            ids = a.get("ids")
            if not isinstance(ids, list) or len(ids) < 2:
                raise StateError("align 要给至少两个 ids")
            hints = dict(draft.get("hints") or {})
            groups = [list(g) for g in (hints.get("align") or [])]
            groups.append([str(x) for x in ids])
            hints["align"] = groups
            draft["hints"] = hints
        elif op == "route":
            eid = str(a.get("id") or a.get("eid") or "").strip()
            via = str(a.get("via") or "").strip()
            if not eid or via not in ("top", "bottom"):
                raise StateError("route 要给 id（边 eid）与 via ∈ top/bottom")
            if not any(str(e.get("eid")) == eid for e in draft["edges"]):
                raise StateError(
                    f"route 指向不存在的边 `{eid}`；先 inspect 拿真实 eid")
            hints = dict(draft.get("hints") or {})
            rt = dict(hints.get("route") or {})
            rt[eid] = via
            hints["route"] = rt
            draft["hints"] = hints
        elif op == "remove":
            what = str(a.get("target") or "").strip()
            key, bucket = {"node": ("id", draft["nodes"]),
                           "edge": ("eid", draft["edges"]),
                           "group": ("id", draft["groups"])}.get(what, ("", []))
            if not key:
                raise StateError("remove 要给 target ∈ node / edge / group。")
            # 边用 eid 建、用 eid 改，删的时候也允许直接带 eid ——
            # 只认 id 会让「query 拿到的键」和「remove 要给的键」对不上，
            # 是 AI 真会踩的摩擦（E4 判据当场抓到）。
            target = str(a.get("id") or a.get("eid") or "").strip()
            kept_bucket = [o for o in bucket if str(o.get(key)) != target]
            if len(kept_bucket) == len(bucket):
                return _result(False, op=op, error=f"找不到 {what} id=`{target}`",
                               present=present, state_path=str(p),
                               next="先 query 拿到真实 id 再删。")
            draft[{"node": "nodes", "edge": "edges",
                   "group": "groups"}[what]] = kept_bucket
            if what == "node":
                cascade = [e["eid"] for e in draft["edges"]
                           if target in (e["from"], e["to"])]
                draft["edges"] = [e for e in draft["edges"]
                                  if target not in (e["from"], e["to"])]
            elif what == "group":
                for n in draft["nodes"]:
                    if n.get("group") == target:
                        n["group"] = ""
    except StateError as ex:
        return _result(False, op=op, error=str(ex), present=present,
                       state_path=str(p), next="按 ACTION_GUIDE 改动作再发一次。")

    # 状态层允许「还没画完」：EMPTY_DIAGRAM 是合法的中间态，只由 render 拦
    # （出图必须有节点）。DUP_* / DANGLING_EDGE / SELF_LOOP / BAD_* 是真硬伤，
    # 一个都不许落盘。否则 AI 每建一步都被「空图」拒一次，增量就名存实亡。
    gs = gaps(draft)
    blockers = [g for g in gs
                if g["level"] == "hard" and g["code"] != "EMPTY_DIAGRAM"]
    if blockers:
        return _result(False, op=op, error="动作会造成 hard 结构缺口，整笔未落盘",
                       gaps=gs, present=present, state_path=str(p),
                       revision=state.get("revision", 0),
                       next="状态没被写坏；按 gaps[].action 修完再发一次。")
    assert_no_geometry(draft)
    draft["revision"] = int(state.get("revision", 0)) + 1 if present else 1
    rep = save(draft, p)
    if not rep["ok"] or not rep.get("readback_equal"):
        return _result(False, op=op, error="状态写后读回不一致，已判失败",
                       present=present, state_path=str(p), save=rep,
                       next="检查磁盘/编码；别拿没读回的状态继续出图。")
    back = load(p)
    # 显式回执（Figma use_figma 的 {createdNodeIds, mutatedNodeIds} 口径）：
    # AI 拿 affected / diff / revision 继续下一步，不靠自己记住刚才改了什么。
    d = _diff(state, back)
    return _result(True, op=op, present=present, bootstrapped=bootstrapped,
                   state_path=str(p), revision=back.get("revision", 0),
                   state=back, gaps=gs, cascade_edges=cascade,
                   readback_equal=True, status=status_line(back),
                   affected=_affected(d), diff=d,
                   summary=describe_scene(back), scene=_scene_view(back),
                   next=_next_for(op, back, gs))
