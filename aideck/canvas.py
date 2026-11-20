# -*- coding: utf-8 -*-
"""AI 画布 —— 给 AI 用的图元级幻灯片编辑器内核（零第三方依赖）。

## 它和「一次性成稿」不是一类东西

Origin 之所以是工具，不是因为它能把一张表变成一张图，而是因为**人能在
上面反复摆弄**：加一条曲线、拖一下图例、问「这条线的线宽多少」、看着
画布改。状态一直在那儿，每一步都能看见结果。

AI 做 PPT 缺的正是这个。以前的做法是「模型写一份完整 spec → 导出 pptx」：
中间没有画布、没有状态、没有查询，模型只能靠上下文记住自己放过什么，
一旦要改一个框就得整份重出。**那不是工具，是打印机。**

这里换成一个**持久化的图元文档**：

    页 → 图元（text / bullets / rect / line / chart / table / image）
    每个图元有稳定 id + bbox + 样式 + 数据

AI 用原子动作操作它（add / set / move / remove / align / duplicate），
每一步都：

    改内存 → 落盘 → **读回** → 跑几何质检 → 返回「现在这页长什么样」

`query()` 是给 AI 的眼睛：它不必记住状态，随时问「第 2 页有什么」。
`audit()` 是给 AI 的尺子：越界、重叠、字号太小、密度过高、离网格，
当场以结构化缺口返回。**工具替 AI 看画布**，就像 Origin 替人把画布画出来。

## 边界

  · 内核纯标准库；导出 .pptx 才外包给装了 python-pptx 的 3.12 子进程。
  · 几何由 `rongke/design.py` 的 1280×720 网格给定，但**不替 AI 排版**：
    图元坐标就是最终坐标，AI 自己摆。这是与 `compose_deck` 的根本区别。
  · 质检只给信息（`advisory`），不拦动作、不替 AI 决定。
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

#: 与 `rongke/design.py` 同一套画布（16:9 = 1280×720px，1px = 9525 EMU）。
CANVAS_PX = (1280, 720)
PX_EMU = 9525
MARGIN = 72
CONTENT_TOP = 140

#: 图元类型白名单。认不出就拒，**不猜**。
TYPES = ("text", "bullets", "rect", "line", "chart", "table", "image")
#: 带文字的图元（质检/读回/导出要单独处理）。
TEXT_TYPES = ("text", "bullets", "table")
#: 视觉类图元（导出 pptx 时是真图表/图片/表格，不是文本框）。
VISUAL_TYPES = ("chart", "image", "table")

STATE_NAME = "画布.json"
HTML_NAME = "画布.html"
DECK_NAME = "deck.pptx"

#: 绢青陈墨（用户视觉体系）。色值只放这里一份。
PALETTE = {
    "paper": "#f6f3ec", "ink": "#22303a", "ink2": "#5a6a72",
    "rule": "#d8d2c4", "accent": "#a8442f", "accent2": "#3f6b6a",
}


def _box(v) -> list[int]:
    """归一 [x,y,w,h]；非法一律抛，不静默修成「差不多」。"""
    if isinstance(v, str):
        v = [float(x) for x in re.split(r"[,\s]+", v.strip()) if x]
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise ValueError(f"box 必须是 [x,y,w,h]，收到 {v!r}")
    b = [int(round(float(x))) for x in v]
    if b[2] <= 0 or b[3] <= 0:
        raise ValueError(f"box 的宽高必须为正：{b}")
    return b


def _color(v) -> str:
    s = str(v or "ink").strip()
    return PALETTE.get(s, s if s.startswith("#") else PALETTE["ink"])


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:6]


class CanvasError(ValueError):
    """动作被拒。带明确原因，让 AI 知道该改哪个参数。"""


class Canvas:
    """一份可持久化、可查询、可增量修改的幻灯片画布。

    `root` 是工作目录；状态落在 `root/画布.json`。
    """

    def __init__(self, root: "Path | str", title: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / STATE_NAME
        self.doc = self._load() or {
            "version": 1, "title": str(title or "未命名画布"),
            "canvas": {"w": CANVAS_PX[0], "h": CANVAS_PX[1]},
            "pages": [],
        }

    # ── 落盘 / 读回 ──────────────────────────────────────────────
    def _load(self):
        if not self.state_path.is_file():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def save(self) -> bool:
        """写入后**立刻读回比对**；不一致就是失败，不靠「写成功」自述。"""
        payload = json.dumps(self.doc, ensure_ascii=False, indent=2)
        try:
            self.state_path.write_text(payload, encoding="utf-8", newline="\n")
            return self.state_path.read_text(encoding="utf-8") == payload
        except OSError:
            return False

    # ── 查询：AI 的眼睛 ───────────────────────────────────────────
    def _page(self, pid: str | None):
        pages = self.doc["pages"]
        if pid is None:
            return pages[-1] if pages else None
        return next((p for p in pages if p["id"] == pid), None)

    def _find(self, eid: str):
        for p in self.doc["pages"]:
            for e in p["elements"]:
                if e["id"] == eid:
                    return p, e
        return None, None

    def query(self, page: str = "", type: str = "") -> dict:
        """「现在画布上有什么」—— 返回逐页逐图元的事实，不要求 AI 记状态。"""
        pages = self.doc["pages"]
        if page:
            pages = [p for p in pages if p["id"] == page]
        out = []
        for p in pages:
            els = p["elements"]
            if type:
                els = [e for e in els if e["type"] == type]
            out.append({"id": p["id"], "kind": p["kind"],
                        "title": p.get("title", ""), "elements": [
                            {"id": e["id"], "type": e["type"],
                             "box": e["box"], "text": _plain(e)[:80]}
                            for e in els]})
        return {"ok": True, "pages": len(self.doc["pages"]),
                "shown": len(out), "state": out,
                "canvas": self.doc["canvas"], "advisory": True}

    # ── 几何质检：AI 的尺子 ───────────────────────────────────────
    def audit(self) -> dict:
        """量出来的事实。只报不拦 —— 「拦选择」不归工具管。"""
        w, h = self.doc["canvas"]["w"], self.doc["canvas"]["h"]
        gaps: list[dict] = []
        counts = {"pages": len(self.doc["pages"]), "elements": 0,
                  "visuals": 0, "chars": 0, "out_of_bounds": 0, "overlaps": 0}
        for pi, p in enumerate(self.doc["pages"], 1):
            els = p["elements"]
            counts["elements"] += len(els)
            if not els:
                # ⚠️ 必须是 soft：刚 new_page 出来的空页是**正常中间状态**。
                #    判 hard 会让「建页」这个合法动作回 ok=False（假红），
                #    等于训练 AI 无视回包。空页要**说出口**，但不该判动作失败。
                gaps.append({"code": "EMPTY_PAGE", "level": "soft", "page": pi,
                             "detail": f"第 {pi} 页一个图元都没有",
                             "action": "用 add 放一个 text/bullets 图元"})
            boxes = []
            for e in els:
                x, y, ew, eh = e["box"]
                counts["chars"] += len(_plain(e))
                if e["type"] in VISUAL_TYPES:
                    counts["visuals"] += 1
                if x < 0 or y < 0 or x + ew > w or y + eh > h:
                    counts["out_of_bounds"] += 1
                    gaps.append({"code": "OUT_OF_BOUNDS", "level": "hard", "page": pi,
                                 "detail": f"{e['id']}（{e['type']}）box={e['box']} 越出 {w}×{h} 画布",
                                 "action": f"用 move({e['id']}, box=...) 挪回画布内"})
                if e["type"] in TEXT_TYPES:
                    boxes.append((e["id"], e["box"], int(e.get("size") or 24)))
                    if int(e.get("size") or 24) < 16:
                        gaps.append({"code": "TOO_SMALL", "level": "soft", "page": pi,
                                     "detail": f"{e['id']} 字号 {e.get('size')}px < 16px",
                                     "action": "用 set 把 size 调到 ≥16"})
                    if x not in (MARGIN, 356, 656, 840, 1088):
                        gaps.append({"code": "OFF_GRID", "level": "soft", "page": pi,
                                     "detail": f"{e['id']} 左边缘 x={x} 不在设计网格列上",
                                     "action": "对齐到 72 / 356 / 656 / 840 / 1088"})
            # 文字图元两两重叠（装饰矩形/线条不参与，它们本来就压在文字底下）
            for i, (ai, ab, _) in enumerate(boxes):
                for bi, bb, _ in boxes[i + 1:]:
                    ox = max(0, min(ab[0] + ab[2], bb[0] + bb[2]) - max(ab[0], bb[0]))
                    oy = max(0, min(ab[1] + ab[3], bb[1] + bb[3]) - max(ab[1], bb[1]))
                    area = ox * oy
                    small = min(ab[2] * ab[3], bb[2] * bb[3])
                    if small and area / small > 0.20:
                        counts["overlaps"] += 1
                        gaps.append({"code": "OVERLAP", "level": "hard", "page": pi,
                                     "detail": f"{ai} 与 {bi} 重叠 {area / small:.0%}",
                                     "action": "用 move 挪开其中一个，或调 align"})
            if counts["chars"] > 900:
                gaps.append({"code": "DENSE_PAGE", "level": "soft", "page": pi,
                             "detail": f"第 {pi} 页文字偏多（{counts['chars']} 字符累计）",
                             "action": "拆成两页或精简要点"})
        return {"ok": not any(g["level"] == "hard" for g in gaps),
                "counts": counts, "gaps": gaps, "advisory": True}

    # ── 原子动作（每个都写后读回 + 返回当前事实）──────────────────
    def _commit(self, action: str, **extra) -> dict:
        written = self.save()
        a = self.audit()
        rep = {"ok": written and a["ok"], "action": action,
               # `ok=False` 只表示「落盘后有 hard 缺口」，**不表示动作没执行**。
               # 像 Origin 把图拖出画布：东西确实在那儿，工具负责把越界说出来，
               # 由 AI 决定挪不挪。两个事实分开报，别让 AI 以为要重发动作。
               "applied": True,
               "written": written, "readback_equal": written,
               "gaps": a["gaps"], "counts": a["counts"], "advisory": True}
        rep.update(extra)
        # 关键：ok 只说明「落盘 + 无 hard 缺口」。soft 缺口照样如实列出，
        # 不因为「能写」就把版面问题吞掉。
        if not written:
            rep["error"] = "落盘后读回不一致（状态没写进去）"
        return rep

    def new_page(self, kind: str = "content", title: str = "") -> dict:
        kind = str(kind or "content")
        p = {"id": _new_id("p"), "kind": kind, "title": str(title or ""),
             "elements": []}
        # 新页自带一个标题框 —— 但不替 AI 写内容（text 留空，audit 会报空页）。
        self.doc["pages"].append(p)
        return self._commit("new_page", page=p["id"], page_state=p)

    def add(self, type: str, box, *, page: str = "", text: str = "",
            items=None, size: int = 26, color: str = "ink",
            fill: str = "accent", chart: dict | None = None,
            src: str = "", rows=None) -> dict:
        t = str(type or "").strip().lower()
        if t not in TYPES:
            raise CanvasError(f"图元类型 {type!r} 认不出；可用 {list(TYPES)}")
        p = self._page(page or None)
        if p is None:
            p = self.new_page().get("page_state")  # 空画布自动开一页
            p = self.doc["pages"][-1]
        e: dict = {"id": _new_id("e"), "type": t, "box": _box(box),
                   "color": _color(color)}
        if t in ("text", "bullets"):
            e["size"] = int(size)
            if t == "text":
                e["text"] = str(text or "")
            else:
                e["items"] = ([str(x) for x in items] if isinstance(items, (list, tuple))
                              else [x.strip() for x in str(text or "").splitlines() if x.strip()])
        elif t == "rect":
            e["fill"] = _color(fill)
        elif t == "line":
            e["fill"] = _color(fill)
        elif t == "chart":
            e["chart"] = chart or {}
            if not (e["chart"].get("categories") and e["chart"].get("series")):
                raise CanvasError("chart 图元需要 chart.categories / chart.series（内核不编数据）")
        elif t == "image":
            e["src"] = str(src or "")
        elif t == "table":
            e["rows"] = rows or []
            e["size"] = int(size)
        p["elements"].append(e)
        return self._commit("add", element=e, page=p["id"],
                            hint=f"用 query(page='{p['id']}') 看当前整页；"
                                 f"用 move('{e['id']}', box=...) 调位置")

    def set(self, eid: str, **patch) -> dict:
        p, e = self._find(str(eid))
        if e is None:
            raise CanvasError(f"找不到图元 {eid!r}；用 query() 看当前 id 清单")
        allowed = {"box", "text", "items", "size", "color", "fill",
                   "chart", "src", "rows"}
        for k, v in patch.items():
            if k not in allowed:
                raise CanvasError(f"不许改 {k!r}；可改 {sorted(allowed)}")
            if k == "box":
                e[k] = _box(v)
            elif k in ("color", "fill"):
                e[k] = _color(v)
            elif k == "size":
                e[k] = int(v)
            else:
                e[k] = v
        return self._commit("set", element=e, page=p["id"])

    def move(self, eid: str, box=None, *, dx: int = 0, dy: int = 0) -> dict:
        p, e = self._find(str(eid))
        if e is None:
            raise CanvasError(f"找不到图元 {eid!r}")
        b = _box(box) if box is not None else list(e["box"])
        b[0] += int(dx)
        b[1] += int(dy)
        e["box"] = b
        return self._commit("move", element=e, page=p["id"])

    def remove(self, eid: str) -> dict:
        p, e = self._find(str(eid))
        if e is None:
            raise CanvasError(f"找不到图元 {eid!r}")
        p["elements"].remove(e)
        return self._commit("remove", removed=eid, page=p["id"])

    def duplicate(self, eid: str, *, dx: int = 24, dy: int = 24) -> dict:
        p, e = self._find(str(eid))
        if e is None:
            raise CanvasError(f"找不到图元 {eid!r}")
        import copy
        c = copy.deepcopy(e)
        c["id"] = _new_id("e")
        c["box"][0] += int(dx)
        c["box"][1] += int(dy)
        p["elements"].append(c)
        return self._commit("duplicate", element=c, page=p["id"], source=eid)

    def align(self, eids, mode: str = "left") -> dict:
        """把一批图元按同一条边对齐（mode = left/right/top/bottom）。"""
        if mode not in ("left", "right", "top", "bottom"):
            raise CanvasError(f"mode {mode!r} 认不出")
        ids = [eids] if isinstance(eids, str) else list(eids)
        els = [self._find(i)[1] for i in ids]
        if any(e is None for e in els) or len(els) < 2:
            raise CanvasError("align 需要 ≥2 个在场图元 id")
        if mode in ("left", "right"):
            idx, off = (0, 0) if mode == "left" else (0, 2)
            target = (els[0]["box"][0] if mode == "left"
                      else els[0]["box"][0] + els[0]["box"][2])
            for e in els[1:]:
                e["box"][idx] = (target if mode == "left"
                                 else target - e["box"][2])
        else:
            target = (els[0]["box"][1] if mode == "top"
                      else els[0]["box"][1] + els[0]["box"][3])
            for e in els[1:]:
                e["box"][1] = (target if mode == "top"
                               else target - e["box"][3])
        return self._commit("align", aligned=ids, mode=mode,
                            elements=[dict(e) for e in els])

    def apply(self, action: dict) -> dict:
        """统一入口：AI 只发一个动作 dict，工具负责分发 + 读回。

        这样工具层只需要极少的具名工具，动作种类却能一直长。
        """
        if not isinstance(action, dict):
            return {"ok": False, "error": "action 必须是对象"}
        a = dict(action)
        op = str(a.pop("op", "")).strip().lower()
        try:
            if op == "query":
                return self.query(**{k: v for k, v in a.items()
                                     if k in ("page", "type")})
            if op == "audit":
                return self.audit()
            if op in ("new_page", "add", "set", "move", "remove",
                      "duplicate", "align"):
                return getattr(self, op)(**a)
            return {"ok": False,
                    "error": f"动作 {op!r} 认不出；可用 query/audit/new_page/add/set/move/remove/duplicate/align"}
        except (CanvasError, ValueError, TypeError) as e:
            # 失败时**原状态一字不动**：动作改到一半异常也要回滚。
            self.doc = self._load() or self.doc
            return {"ok": False, "action": op,
                    "error": f"{type(e).__name__}: {e}",
                    "hint": "用 query() 看重试前画布上真实有哪些 id"}


def _plain(e: dict) -> str:
    """图元里的纯文字（给 query / audit / 读回比对用）。"""
    if e["type"] == "text":
        return str(e.get("text") or "")
    if e["type"] == "bullets":
        return " ".join(str(x) for x in (e.get("items") or []))
    if e["type"] == "table":
        return " ".join(" ".join(str(c) for c in r)
                        for r in (e.get("rows") or []) if isinstance(r, (list, tuple)))
    if e["type"] == "chart":
        return str((e.get("chart") or {}).get("title") or "")
    return ""
