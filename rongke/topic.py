# -*- coding: utf-8 -*-
"""课题上下文（导师模式 P1/P2）：长生命的「课题」+ 卡点台账。

## 为什么需要它

荣珂原来只有**一条时间尺度：一次 run**。`TaskContract` 的字段跑完就归档；
`report_blocker` 是**终局**动作 —— 卡点被说出来一次，然后永远消失。
于是「导师」最核心的两句话做不出来：

  · 「**上次到哪了？**」 —— 没有对象可查（课题不是一等公民）
  · 「**这个坑你两周前卡过。**」 —— 没有台账，同一个坑换个说法会再踩一次

⇒ 本模块给两件东西，都住在 `<项目根>/topics/`：

    topics/
      CURRENT            # 一行文本：当前课题 slug
      <slug>/
        topic.json       # {slug, title, question, created, updated}
        PLAN.md          # 计划（模型自由写）
        READING.md       # 读书台账
        EVIDENCE.md      # 证据表
        BLOCKERS.jsonl   # 卡点台账（append-only 事件流）

## ★ 三条设计纪律（既有的实测教训，在这里复用而不是重造）

① **「未闭环」必须浮现，但内核不判断「相不相关」。**
   第一版设计是"按卡点与本次目标的**相似度**过滤" —— **推翻了**，理由是硬的：
   相似度需要一个阈值，而**真实卡点语料现在一条都不存在** ⇒ 任何阈值都是猜；
   本项目记过「**打红面 99.6% 与 0% 是同一种失败**」（前者是噪音器，后者是装饰品）。
   现在改成 **无阈值的全量浮现**：未闭环的按新→旧列出，超过上限**如实说还有几条**。
   ⇒ **拦沉默，不拦选择** —— 内核只负责"让它出现"，"相不相关"交给模型/人。
   ⇒ 附带好处：**这条机制没有可调的阈值**，因此不会烂、也不需要标定语料。

② **台账是 append-only 的事件流，不是"重写整份文件"。**
   `resolve_blocker` **不修改**已有行，而是**追加一条** `{"kind":"resolve", …}`；
   读取时按 id 折叠。理由：一次崩溃最多丢新写的那一条，不会毁掉整本账；
   而且"谁在什么时候把它闭环的"天然被保留下来（事后可查）。

③ **写完读回来核**（本仓老规矩：自称成功永不算证据）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

# ── 文档名（单源：工具参数名 → 文件名，别处不许再写一份）──────────
DOC_FILE = {"plan": "PLAN.md", "reading": "READING.md", "evidence": "EVIDENCE.md"}
# 课题工作阶段 ≠ stages.py 的任务技能路由；只能由用户明确选择。
WORK_STAGES = {"ideate": "构思评估", "design": "研究设计", "evidence": "文献与证据",
               "execute": "实验与写作", "submission": "投稿与回复"}
NEXT_STEP_MAX = 240

# 上下文里每份文档摘多少字。**截断必须如实说**（M 型：不许把"没给全"渲染成"就这么多"）。
DOC_HEAD = 1200
# 未闭环卡点最多列几条 —— 超出的**如实报数**，不是悄悄丢掉。
BLOCKER_SHOW = 5
# 按窗口读文档时的默认窗口（字符）。**这个数只有一处**：界面 `mentor.js` 传的
# `limit=12000` 与它同值 —— 两边各写一个数就是 H 型（改一处忘一处，窗口悄悄缩水）。
DOC_WINDOW = 12000
# 窗口上限。再大就不叫"窗口"了：一次把整份台账塞进响应，翻页机制会退化成摆设。
DOC_WINDOW_MAX = 40000
# `topic_status` 一次默认列多少条卡点（原先是 `loop.py` 里写死的 10，
# 现在这个数归台账模块**单源**持有 —— 状态折叠只在这里做一次）。
BLOCKER_PAGE = 10

_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-\.\u4e00-\u9fff]{1,48}$")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def topics_root(root: "Path | str | None" = None) -> Path:
    if root is not None:
        return Path(root)
    # 延迟导入：本模块要能被单测直接 import。
    # ★ 用 data_root 而不是 project_root：课题是**用户数据**，装成依赖后
    #   不能落在 site-packages 里（升级即丢）。源码树里两者同一个目录。
    from .config import data_root
    return data_root() / "topics"


# ── slug 安全：**模型给的字符串**，所以这是一条安全边界，不是格式检查 ──
def valid_slug(slug: str) -> bool:
    """只收 [字母/数字/下划线/短横/点/中文]，长度 ≤48，且不许出现 `..`。"""
    s = (slug or "").strip()
    if not _SLUG_RE.match(s):
        return False
    return s not in (".", "..") and ".." not in s


def slugify(name: str) -> str:
    """把人类可读的课题名压成 slug。空 ⇒ 空串（由调用方决定怎么办，不在这里编一个）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff\-\.]+", "-", (name or "").strip(), flags=re.U)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s[:48]


def _topic_dir(slug: str, root=None) -> Path | None:
    """解析 `topics/<slug>/`。**非法或越界一律返回 None**（不抛异常给上层猜）。"""
    if not valid_slug(slug):
        return None
    base = topics_root(root)
    p = base / slug.strip()
    try:
        real_base, real_p = base.resolve(), p.resolve()
    except Exception:
        return None
    if real_p != real_base and not str(real_p).startswith(str(real_base) + os.sep):
        return None
    return p


# 公开别名：`rongke/lab.py` 要往课题目录里写 LAB.jsonl。
# 让它自己拼 `topics_root()/slug` 就是**第二处路径真相** —— 白名单与越界校验
# 会各说一套（`lab` 那边迟早漏掉 `..` 拦截）。所以只开一个只读入口，
# 校验仍然只有这里一处。
def topic_dir(slug: str, root=None) -> Path | None:
    return _topic_dir(slug, root)


# ── 当前课题 ──────────────────────────────────────────────
def current(root=None) -> str:
    """当前课题 slug；没设过 / 已失效 ⇒ 空串（**不猜、不编**）。"""
    f = topics_root(root) / "CURRENT"
    try:
        s = f.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return s if _topic_dir(s, root) is not None else ""


def set_current(slug: str, root=None) -> str:
    """切到某个课题。**必须先存在** —— 「切到一个不存在的课题」会造出一个隐形空壳。"""
    d = _topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}（只收字母/数字/下划线/短横/点/中文，≤48 字）")
    if not (d / "topic.json").exists():
        raise ValueError(f"课题不存在: {slug!r} —— 先用 topic_use 创建它")
    base = topics_root(root)
    base.mkdir(parents=True, exist_ok=True)
    f = base / "CURRENT"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(slug.strip() + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, f)                      # 原子：要么完整、要么什么都没有
    return slug.strip()


# ── 课题本身 ──────────────────────────────────────────────
def read_topic(slug: str, root=None) -> dict | None:
    d = _topic_dir(slug, root)
    if d is None:
        return None
    f = d / "topic.json"
    if not f.exists():
        return None
    try:
        meta = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    meta.setdefault("slug", slug.strip())
    if meta.get("stage") not in WORK_STAGES:
        meta["stage"] = "ideate"  # 老课题兼容：不代表任何完成度
    meta.setdefault("next_step", "")
    raw_library = meta.get("library", [])
    meta["library"] = list(dict.fromkeys(str(x).strip() for x in raw_library
                                         if isinstance(x, str) and x.strip())) if isinstance(raw_library, list) else []
    meta["dir"] = str(d)
    meta["docs"] = {k: (d / v).exists() for k, v in DOC_FILE.items()}
    meta["open_blockers"] = len(open_blockers(slug, root=root))
    return meta


def list_topics(root=None) -> list[dict]:
    base = topics_root(root)
    out: list[dict] = []
    if not base.exists():
        return out
    for c in sorted(base.iterdir()):
        if not c.is_dir():
            continue
        m = read_topic(c.name, root=root)
        if m:
            out.append(m)
    return out


def ensure_topic(slug: str, title: str = "", question: str = "", root=None) -> dict:
    """创建（已存在则**原样返回**，绝不覆盖 —— 静默覆盖用户的一手内容是数据事故）。"""
    d = _topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}（只收字母/数字/下划线/短横/点/中文，≤48 字）")
    f = d / "topic.json"
    if f.exists():
        return read_topic(slug, root=root) or {}
    d.mkdir(parents=True, exist_ok=True)
    meta = {"slug": slug.strip(), "title": (title or slug).strip(),
            "question": (question or "").strip(),
            "created": _now(), "updated": _now()}
    f.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                 encoding="utf-8", newline="\n")
    return read_topic(slug, root=root) or {}


def update_topic(slug: str, title: str, question: str, root=None,
                 stage: str | None = None, next_step: str | None = None) -> dict:
    """显式编辑课题元数据；关联、计划、卡点及未知字段原样保留。"""
    if len(title) > 160 or len(question) > 1200:
        raise ValueError("课题名称最多 160 字，研究问题最多 1200 字")
    if stage is not None and stage not in WORK_STAGES:
        raise ValueError("课题阶段必须是：" + "、".join(WORK_STAGES))
    if next_step is not None and len(next_step) > NEXT_STEP_MAX:
        raise ValueError(f"下一步最多 {NEXT_STEP_MAX} 字")
    d = _topic_dir(slug, root)
    old = read_topic(slug, root=root)
    if d is None or old is None:
        raise KeyError("课题不存在或台账不可读")
    old.pop("dir", None); old.pop("docs", None); old.pop("open_blockers", None)
    old.update(title=title.strip(), question=question.strip(), updated=_now())
    if stage is not None and stage != old.get("stage"):
        old["stage_updated"] = _now()
    if stage is not None:
        old["stage"] = stage
    if next_step is not None:
        old["next_step"] = next_step.strip()
    tmp = d / "topic.json.tmp"
    tmp.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    os.replace(tmp, d / "topic.json")
    return read_topic(slug, root=root) or {}


def set_library(slug: str, doc_id: str, linked: bool, root=None) -> dict:
    """只改课题关联，不复制或删除共享库文献。"""
    doc_id = (doc_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", doc_id):
        raise ValueError("文献ID格式不正确")
    d = _topic_dir(slug, root)
    if d is None:
        raise ValueError("非法的课题名")
    old = read_topic(slug, root=root)
    if old is None:
        raise KeyError("课题不存在或台账不可读")
    ids = list(old.get("library", []))
    if linked and doc_id not in ids:
        ids.append(doc_id)
    elif not linked and doc_id not in ids:
        raise ValueError("这篇文献本来就没有关联到本课题")
    elif not linked:
        ids.remove(doc_id)
    old.pop("dir", None); old.pop("docs", None); old.pop("open_blockers", None)
    old.update(library=ids, updated=_now())
    tmp = d / "topic.json.tmp"
    tmp.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    os.replace(tmp, d / "topic.json")
    return {"ok": True, "slug": slug, "doc_id": doc_id, "linked": linked, "library": ids}


# ── 卡点台账（append-only 事件流 + 按 id 折叠）──────────────
def _blockers_path(slug: str, root=None) -> Path | None:
    d = _topic_dir(slug, root)
    return None if d is None else d / "BLOCKERS.jsonl"


def _events(slug: str, root=None) -> list[dict]:
    p = _blockers_path(slug, root)
    if p is None or not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue                      # 坏行**跳过但不扔**（原文还在盘上，可人工看）
        if isinstance(e, dict) and e.get("id"):
            out.append(e)
    return out


def blockers(slug: str, status: str = "", root=None) -> list[dict]:
    """按 id 折叠事件流，返回卡点列表（新→旧）。`status` 空 = 全部。"""
    folded: dict[str, dict] = {}
    order: list[str] = []
    for e in _events(slug, root):
        bid = str(e["id"])
        if bid not in folded:
            folded[bid] = {"id": bid, "status": "open", "opened_at": "",
                           "what": "", "tried": [], "where": "",
                           "resolved_at": "", "resolved_how": ""}
            order.append(bid)
        b = folded[bid]
        if e.get("kind") == "resolve":
            b["status"] = "resolved"
            b["resolved_at"] = e.get("at", "")
            b["resolved_how"] = e.get("how", "")
        else:                              # kind == "open"（缺省也算开）
            b["opened_at"] = e.get("at", "")
            b["what"] = e.get("what", "")
            b["tried"] = list(e.get("tried") or [])
            b["where"] = e.get("where", "")
    items = [folded[i] for i in order]
    items.reverse()                        # 新的在前
    if status:
        items = [b for b in items if b["status"] == status]
    return items


def open_blockers(slug: str, root=None) -> list[dict]:
    return blockers(slug, status="open", root=root)


def blocker_page(slug: str, status: str = "open", offset: int = 0,
                 limit: int = BLOCKER_PAGE, root=None) -> dict:
    """卡点的**分页视图**（Web 面板与 `topic_status` 共用这一处折叠）。

    为什么要有它：`blockers()` 是"全给"，而面板要按状态筛、要能翻页。
    把这套过滤/分页写在 `web/server.py` 里就是**第二处真相** ——
    两边对"open 到底包含哪些"迟早会各说一套。所以折叠（`blockers()`）
    与分页都留在本模块，服务端只做传输。

    `status` ∈ `open` / `resolved` / `all`（空 = `all`）。
    返回里 `items` 与 `blockers` **同名两把**：前者给新界面，后者是
    `topic_status` 一直在用的旧名 —— 改名会让已有调用方静默拿不到数据。
    """
    key = (status or "").strip().lower() or "all"
    if key not in ("open", "resolved", "all"):
        raise ValueError(f"status 只能是 open / resolved / all（收到 {status!r}）")
    allb = blockers(slug, root=root)                     # 折叠处仍只有 `blockers()` 一处
    picked = allb if key == "all" else [b for b in allb if b["status"] == key]
    off = max(0, int(offset or 0))
    lim = max(1, int(limit or BLOCKER_PAGE))
    page = picked[off:off + lim]
    return {
        "ok": True, "slug": (slug or "").strip(), "status": key,
        "items": page, "blockers": page,
        "total": len(picked), "offset": off, "limit": lim,
        "truncated": off + len(page) < len(picked),
        # 未闭环数**始终是全量的事实**，与当前筛选/翻页无关 ——
        # 否则「open_total 随筛选变化」会让界面上的红色角标时有时无。
        "open_total": sum(1 for b in allb if b["status"] == "open"),
    }


# ── 课题文档的**只读窗口**（Web 面板读台账的唯一入口）────────────
def _doc_path(slug: str, doc: str, root=None) -> tuple[Path | None, str]:
    """解析 `topics/<slug>/<文档>`。**非法 slug / 非法 doc 一律 `(None, "")`**。

    这是本模块的第二条安全边界：`doc` 是**外面（含 Web 查询串）给的字符串**，
    所以它只能从 `DOC_FILE` 那张表里查，**不许**拼进任何路径 ——
    拼路径那一版里 `doc=../../config.json` 就是一个任意文件读取洞。
    """
    d = _topic_dir(slug, root)
    key = (doc or "").strip().lower()
    if d is None or key not in DOC_FILE:
        return None, key
    return d / DOC_FILE[key], key


def doc_index(slug: str, root=None) -> list[dict]:
    """三份活文档的**有界索引**：有没有、多少字、头部截了多少。

    只报事实，不塞正文 —— 正文按窗口用 `read_doc()` 取。
    这样"看课题到哪了"这件事的载荷是有界的，不会因为台账写了十万字
    就把 `topic_status` 的返回撑爆（那会把上下文一次烧光）。
    """
    d = _topic_dir(slug, root)
    out: list[dict] = []
    for key, name in DOC_FILE.items():
        f = (d / name) if d is not None else None
        exists = bool(f is not None and f.is_file())
        total = 0
        if exists:
            try:
                total = len(f.read_text(encoding="utf-8", errors="replace").strip())
            except Exception:
                total = 0
        out.append({
            "doc": key, "file": name, "exists": exists,
            "chars": total, "total": total,
            "empty": (not exists) or total == 0,
            # 头 N 字是**上下文里给过的那一段**；超过就是"没给全"，必须说出来。
            "head_chars": min(total, DOC_HEAD),
            "truncated": total > DOC_HEAD,
        })
    return out


def read_doc(slug: str, doc: str, offset: int = 0, limit: int = DOC_WINDOW,
             root=None) -> dict:
    """按**字符窗口**读一份课题活文档。只读，永远不写。

    ★ 为什么是字符窗口而不是行号：台账是模型随手追加的自由文本，
      一行可能几千字、也可能只有一个标题 —— 行号分页在这里没有意义。
    ★ 越界/缺失**如实说**，绝不编造，也绝不返回"成功但空"的假象：
      · 非法 slug / 非法 doc  ⇒ **raise**（这是安全边界，不是业务状态）
      · 课题不存在/文档没写过 ⇒ `not_found` / `empty`
      · offset 超出全文       ⇒ `text=""` + `out_of_range=True`
    """
    p, key = _doc_path(slug, doc, root)
    if p is None:
        raise ValueError(f"非法的课题名或文档名: slug={slug!r} doc={doc!r}"
                         f"（doc 只能是 {' / '.join(DOC_FILE)}）")
    s = (slug or "").strip()
    if not p.parent.is_dir() or not (p.parent / "topic.json").exists():
        return {"ok": False, "not_found": True, "reason": "no_topic", "slug": s,
                "doc": key, "file": DOC_FILE[key], "offset": 0,
                "text": "", "chars": 0, "total": 0, "total_chars": 0,
                "truncated": False, "empty": True,
                "error": f"课题不存在: {s!r}"}
    if not p.is_file():
        return {"ok": True, "not_found": False, "reason": "no_file", "slug": s,
                "doc": key, "file": DOC_FILE[key], "offset": 0,
                "text": "", "chars": 0, "total": 0, "total_chars": 0,
                "truncated": False, "empty": True}
    try:
        body = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as e:
        return {"ok": False, "not_found": False, "reason": "unreadable", "slug": s,
                "doc": key, "file": DOC_FILE[key], "offset": 0,
                "text": "", "chars": 0, "total": 0, "total_chars": 0,
                "truncated": False, "empty": True,
                "error": f"读不动这份台账：{type(e).__name__}: {e}"}
    total = len(body)
    off = max(0, int(offset or 0))
    lim = max(1, min(int(limit or DOC_WINDOW), DOC_WINDOW_MAX))
    window = body[off:off + lim]
    return {"ok": True, "not_found": False, "reason": "", "slug": s,
            "doc": key, "file": DOC_FILE[key], "offset": off, "limit": lim,
            "text": window, "chars": len(window),
            "total": total, "total_chars": total,
            "truncated": off + len(window) < total,
            "empty": total == 0,
            "out_of_range": off >= total and total > 0}


# ── 实验台账的两个只读视图（懒导入 lab，避免与 lab→topic 的依赖成环）──
# 依赖方向是定死的：`lab` 可以在顶层 import `topic`（它要用 topic_dir 的路径校验），
# `topic` **只能**在函数内 import `lab`。反了就是循环导入，而 Python 的表现是
# 一个莫名其妙的 ImportError，不是"依赖方向错了"。
_LAB_ZERO = {"events": 0, "strains": 0, "batches": 0, "running": 0,
             "measures": 0, "observes": 0, "gaps_missing": 0,
             "gaps_few": 0, "gaps_no_control": 0, "todos_open": 0}


def lab_counts(slug: str, root=None) -> dict:
    """台账计数块。**空台账回全 0，不回空字典** —— 下游会直接取键。"""
    out = dict(_LAB_ZERO)
    try:
        from . import lab as L
        st = L.state(slug, root=root)
        gp = L.gaps(slug, root=root)
    except Exception:                     # noqa: BLE001
        # 台账读坏了不该让整个状态视图崩 —— 但它**必须留痕**，
        # 所以带上 `error`，而不是静默返回全 0 假装"没有台账"。
        out["error"] = "台账不可读"
        return out
    out["events"] = st.get("n_events", 0)
    out["strains"] = len(st.get("strains") or {})
    out["batches"] = len(st.get("batches") or {})
    out["running"] = len([b for b in (st.get("batches") or {}).values()
                          if b.get("status") != "done"])
    out["measures"] = len(st.get("measures") or [])
    out["observes"] = len(st.get("observes") or [])
    out["gaps_missing"] = len([g for g in gp if g.get("kind") == "missing"])
    out["gaps_few"] = len([g for g in gp if g.get("kind") == "few_reps"])
    out["gaps_no_control"] = len([g for g in gp if g.get("kind") == "no_control"])
    out["todos_open"] = len([t for t in (st.get("todos") or {}).values()
                             if t.get("status") != "closed"])
    return out


def lab_next(slug: str, limit: int = 3, root=None) -> list[dict]:
    """机械排序的下一步（**只有三条**：上下文装不下更多，也给不了更多）。"""
    try:
        from . import lab as L
        return L.next_actions(slug, limit=limit, root=root)
    except Exception:                     # noqa: BLE001
        return []


LAB_DIGEST_MAX = 700


def lab_digest(slug: str, root=None) -> str:
    """给提示词用的台账摘要。**空台账 ⇒ 空串**（不许注入空壳块）。"""
    try:
        from . import lab as L
        return L.digest(slug, max_chars=LAB_DIGEST_MAX, root=root)
    except Exception:                     # noqa: BLE001
        return ""


# ── 状态折叠（**单源**：模型工具与 Web 面板都从这里取）──────────
def status_view(slug: str = "", root=None, status: str = "",
                offset: int = 0, limit: int = BLOCKER_PAGE,
                doc: str = "", doc_offset: int = 0,
                doc_limit: int = DOC_WINDOW) -> dict:
    """一份课题的**完整状态**：元信息 + 有界文档索引 + 卡点分页（可按需多给一窗正文）。

    存在的理由（此前这段逻辑住在 `loop.py` 的 `topic_status` 闭包里）：
    Web 面板要的是**同一件事**的另一半（文档全文、卡点按状态筛），
    于是这两半要么共用一个函数，要么各自实现一遍过滤/截断/计数 ——
    后者就是 H 型缺陷：面板说"还有 3 条"、模型看到"还有 5 条"，
    而两边都对天发誓自己没算错。现在折叠只在这里发生一次。

    `doc` 非空 ⇒ 顺带返回那一份的窗口正文（键 `document`），
    这样"索引 + 一窗正文"是一次调用，而不是让调用方拼两次请求。
    """
    s = (slug or "").strip() or current(root)
    if not s:
        # 没有当前课题**不是错误**：如实说空，并给下一步该做什么。
        return {"ok": True, "current": "", "slug": "",
                "topics": [m["slug"] for m in list_topics(root=root)],
                "topic_details": list_topics(root=root),
                "docs": dict.fromkeys(DOC_FILE, False), "doc_index": [],
                "blockers": [], "items": [], "blockers_total": 0,
                "open_total": 0, "offset": 0, "limit": max(1, int(limit or BLOCKER_PAGE)),
                "status": (status or "").strip().lower() or "all",
                "hint": "还没有当前课题 —— 用 topic_use 建一个再开工。"}
    m = read_topic(s, root=root)
    if not m:
        known = ", ".join(x["slug"] for x in list_topics(root=root)) or "（一个都没有）"
        raise ValueError(f"课题不存在: {s!r}。已知课题：{known}")
    page = blocker_page(s, status=status, offset=offset, limit=limit, root=root)
    out = {"ok": True, "current": current(root=root), "slug": s,
           "title": m.get("title"), "question": m.get("question"),
           "created": m.get("created"), "updated": m.get("updated"),
           # `docs` 保持**布尔表**（旧调用方按它判"写没写过"），
           # 有界的字数/截断信息另开 `doc_index` —— 加字段不改旧键。
           "docs": m.get("docs") or {},
           "doc_index": doc_index(s, root=root),
           "topics": [x["slug"] for x in list_topics(root=root)],
           "topic_details": list_topics(root=root), "library": m.get("library", []),
           # ★ 2026-11-02：把**阶段 / 下一步 / 实验台账**也交出来。
           #   此前 `status_view` 不返回 stage 与 next_step —— 模型中途看不到
           #   "这个课题到哪一步了、用户记的下一步是什么"，只能靠开场提示词那一次注入。
           #   而实验台账更关键：用户的原话是「老师说我没数据」，
           #   "有没有数据"必须能被读到，否则模型只能凭聊天记忆安慰他。
           #   ★ 空台账回**全 0** 而不是空字典：空字典会让下游
           #   `lab["measures"]` 直接 KeyError（或者更糟，被 `or 0` 掩盖成"没这一块"）。
           "stage": m.get("stage"), "stage_updated": m.get("stage_updated", ""),
           "next_step": m.get("next_step", ""),
           "lab": lab_counts(s, root=root), "lab_next": lab_next(s, root=root),
           "blockers": page["items"], "items": page["items"],
           "blockers_total": page["total"],
           "open_total": page["open_total"],
           "offset": page["offset"], "limit": page["limit"],
           "status": page["status"]}
    if page["truncated"]:
        # 超上限**如实报数**（M 型：别把"没列出来"渲染成"不存在"）。
        # 句子沿用旧口径（"还有 N 条没列出来"）—— 它是模型读得懂的那一句，
        # 换成"本次窗口 [a,b)"只会让它在推理里算错。
        out["note"] = (f"卡点（{page['status']}）共 {page['total']} 条，"
                       f"还有 {page['total'] - page['offset'] - len(page['items'])} 条"
                       f"没列出来（不是不存在）")
    if (doc or "").strip():
        out["document"] = read_doc(s, doc, offset=doc_offset, limit=doc_limit,
                                  root=root)
    return out


def add_blocker(slug: str, what: str, tried=None, where: str = "", root=None) -> dict:
    """记一条卡点。**这是记账，不是收工** —— 与终局的 `report_blocker` 分工明确。"""
    p = _blockers_path(slug, root)
    if p is None:
        raise ValueError(f"非法或不存在的课题: {slug!r}")
    if not (p.parent / "topic.json").exists():
        raise ValueError(f"课题不存在: {slug!r} —— 先用 topic_use 创建它")
    what = (what or "").strip()
    if not what:
        raise ValueError("what 为空：卡点必须写清『卡在哪』")
    tried = [str(x).strip() for x in (tried or []) if str(x).strip()]
    used = {str(e["id"]) for e in _events(slug, root)}
    n = len(used) + 1
    while f"B{n:03d}" in used:             # id 不许撞（append-only 下这是唯一的冲突面）
        n += 1
    bid = f"B{n:03d}"
    ev = {"id": bid, "kind": "open", "at": _now(), "status": "open",
          "what": what[:1000], "tried": tried[:12], "where": (where or "").strip()[:300]}
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    back = [b for b in blockers(slug, root=root) if b["id"] == bid]
    ok = bool(back) and back[0]["what"] == what[:1000]
    return {"id": bid, "ok": ok, "verified": ok, "open_total": len(open_blockers(slug, root=root))}


def resolve_blocker(slug: str, blocker_id: str, how: str = "", root=None) -> dict:
    """把一个卡点闭环。**追加一条 resolve 事件**，不改已有行（纪律②）。"""
    p = _blockers_path(slug, root)
    if p is None:
        raise ValueError(f"非法或不存在的课题: {slug!r}")
    bid = (blocker_id or "").strip()
    mine = [b for b in blockers(slug, root=root) if b["id"] == bid]
    if not mine:
        ids = ", ".join(b["id"] for b in blockers(slug, root=root)) or "（台账是空的）"
        raise ValueError(f"没有这个卡点 id：{bid!r}。现有：{ids} —— 不要猜 id，先看 topic_status")
    if mine[0]["status"] == "resolved":
        # 幂等分支：**本次没有写盘**。这里的 `verified` 核的是**读回来的状态**
        # （上面 `mine` 刚从账上读的），不是"写入成功" —— 所以它是如实的。
        # ⚠️ 这个键**必须给**：`loop.py` 的包装直接读 `r["verified"]`，
        #    少一个键就是 KeyError（实测踩过：幂等闭环那一次直接崩）。
        return {"id": bid, "ok": True, "verified": True, "already": True,
                "open_total": len(open_blockers(slug, root=root))}
    ev = {"id": bid, "kind": "resolve", "at": _now(), "status": "resolved",
          "how": (how or "").strip()[:1000]}
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    back = [b for b in blockers(slug, root=root) if b["id"] == bid]
    ok = bool(back) and back[0]["status"] == "resolved"
    return {"id": bid, "ok": ok, "verified": ok, "open_total": len(open_blockers(slug, root=root))}


# ── 课题文档 ──────────────────────────────────────────────
def write_doc(slug: str, doc: str, text: str, mode: str = "append", root=None) -> dict:
    """写 PLAN / READING / EVIDENCE。`mode` ∈ {append, replace}。

    **内核此前没有任何"写任意文本文件"的基元**（只有 read_file 读、import_document 转格式），
    所以这是补一条真缺口，不是给已有能力加壳。
    """
    d = _topic_dir(slug, root)
    if d is None:
        raise ValueError(f"非法的课题名: {slug!r}")
    if not (d / "topic.json").exists():
        raise ValueError(f"课题不存在: {slug!r} —— 先用 topic_use 创建它")
    key = (doc or "").strip().lower()
    if key not in DOC_FILE:
        raise ValueError(f"doc 只能是 {' / '.join(DOC_FILE)}（收到 {doc!r}）")
    body = text or ""
    if not body.strip():
        raise ValueError("text 为空 —— 空写没有意义；要读请用 topic_status")
    f = d / DOC_FILE[key]
    if mode == "append" and f.exists():
        old = f.read_text(encoding="utf-8", errors="replace").rstrip("\n")
        body = old + "\n\n" + body.strip() + "\n"
    f.write_text(body, encoding="utf-8", newline="\n")
    back = f.read_text(encoding="utf-8", errors="replace")
    ok = back.strip().endswith(body.strip()[-200:].strip()) if body.strip() else False
    return {"doc": DOC_FILE[key], "chars": len(back), "ok": bool(ok), "verified": bool(ok)}


def _doc_head(slug: str, doc: str, root=None) -> tuple[str, int]:
    d = _topic_dir(slug, root)
    if d is None:
        return "", 0
    f = d / DOC_FILE[doc]
    if not f.exists():
        return "", 0
    try:
        t = f.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return "", 0
    return t[:DOC_HEAD], len(t)


#: 出现这些词 ⇒ 用户**明确在问台账文档**，那就把正文展开。
#: 判据刻意收得很窄：宁可少展开一次（模型会自己调 `topic_status` 读），
#: 也不要因为一个「案」字就把 2000 字倾泻进「你好」这种问话里。
_DOC_WORDS = (
    "开题", "计划", "方案", "研究计划", "立项",
    "读书记录", "读了什么", "文献笔记", "笔记",
    "证据表", "证据", "引注", "参考文献", "综述",
    "台账", "进展", "PLAN", "READING", "EVIDENCE",
    "plan", "reading", "evidence",
)


def _needs_docs(goal: str) -> bool:
    """这句话是在问台账文档，还是一句用不上的寒暄/短问？

    ★ 为什么要这条判据（2026-11-03）：三份文档正文占了上下文的大头，
      而绝大多数问话（「你好」「在吗」「染色怎么做」）根本用不上它们。
      全量注入的结果是 **一个字的问题配 5800 字的背景** —— 模型于是
      拿背景当"当前任务"。这是上下文层面的病，改提示词治不了。

    宁可判"不展开"：不展开只是少一段背景，模型随时能自己 `topic_status` 读；
    误判成"展开"则是把无关正文塞进每一句话的对话里。
    """
    g = str(goal or "")
    if not g.strip():
        return False
    # 短句一律不展开：一句话里塞不下真正的文档需求。
    if len(g) <= 6:
        return False
    return any(w in g for w in _DOC_WORDS)


# ── 注入首条消息的上下文 ───────────────────────────────────
def context_text(root=None, goal: str = "", limit: int = BLOCKER_SHOW,
                 slug: str = "") -> str:
    """课题上下文。**没有当前课题就返回空串** —— 不注入空壳。

    为什么"没有就闭嘴"是一条纪律：空壳大纲/空壳段落比没有更糟 ——
    它**承诺了**有内容，却什么都给不出来（本仓在稿件台那处踩过同一个坑）。

    `goal` 目前只用于**如实标注**（本次目标与卡点台账无关时也不隐藏它们），
    不参与任何过滤 —— 见模块头纪律①。

    ★ W28 `slug`：由调用方指定「这一场会话用哪个课题」。为空才退回 `current()`。
      为什么必须能指定：`current()` 是**跨会话**的残留，同一刻可能已经被别人切走；
      而这场会话的身份在开跑那一刻就定下了（`mentor_mode` 事件与快照里都是它）。
      两处各读一次 `current()` ⇒ 提示词说的课题与事件/快照记的课题**可能是两个** ——
      那种"首条上下文与回放对不上"的失效没有任何报错，只有翻历史时才发现。
    """
    slug = str(slug or "").strip() or current(root)
    if not slug:
        return ""
    meta = read_topic(slug, root=root)
    if not meta:
        return ""
    lines = [
        "═══ 课题上下文（背景资料，不是本轮任务）═══",
        f"当前课题：{slug} —— {meta.get('title') or slug}",
    ]
    # ★ W27：把 slug **明写成「写台账时要传的那个值」**。
    #   起因是一次真实事故：模型调 `topic_write` 不带 slug，`current()` 又是
    #   上一轮残留的**另一个**课题，于是一整轮证据被静默写进了别的课题。
    #   现在 `topic_write` 缺 slug 会报错，但**报错是兜底、不是设计** ——
    #   设计是让模型在第一眼就知道该传什么，而不是撞一次错再回头找。
    lines.append(f'※ 写台账（topic_write / raise_blocker / resolve_blocker）时 '
                 f'**必须显式传 slug="{slug}"**，不要依赖「当前课题」这个默认值 —— '
                 f'它是跨会话残留的，可能与你本轮要做的事不是同一个课题。')
    lines.append(f"工作阶段：{WORK_STAGES.get(meta.get('stage'), '构思评估')}（人工选择，不代表完成度）")
    if meta.get("next_step"):
        lines.append(f"用户记录的下一步：{meta['next_step']}")
    if meta.get("question"):
        lines.append(f"科学问题：{meta['question']}")
    if meta.get("created"):
        lines.append(f"建立于：{meta['created']}" + (f"　最近改动：{meta['updated']}" if meta.get("updated") else ""))

    # ── 文档正文：**只在问到的时候注入**（2026-11-03 改）────────────────
    #
    # 用户原话：「不要写那么多用户不需要看的，本质上还是个 agent，
    #   跟其他的 agent 对话应该是一样的，只是多加了一些工具和 skill 调用」。
    #
    # 实测：这份上下文 5826 字，其中 PLAN/READING/EVIDENCE 三份文档占了大头
    # （分别从第 470 / 1707 / 2947 字开始）。于是输入「你好」两个字，
    # 首条消息里 5800 字是课题台账、80% 的内容与这句话无关 ——
    # 模型自然拿它当"当前要办的事"，回一段组会点名。
    # **提示词怎么改都治不了这个**：那是 2104 字 vs 5826 字的对抗，上下文赢。
    #
    # 新口径（同"工作区目录列表"那条老定律的反面运用）：
    #   · 台账**元数据**（课题名、下一步、科学问题、卡点、实验摘要）恒注入 ——
    #     它们是"我现在在哪"，答任何一句都需要；
    #   · 三份**文档正文**改成**按需**：用户这句话明显是在问文档内容时才注入。
    #     不注入不等于不存在 —— 下面留一句指向 `topic_doc`，模型要读随时能读。
    #     这正是 agent 的正常形态：文件在那里，用到才读。
    #
    # 判据（`topic_test.py` 与 `_rk1_probe/verify_fix6.py`）：打招呼/短句 ⇒ 不注入正文；
    # 说「我的开题报告」/「证据表」⇒ 必须注入。
    want_docs = _needs_docs(goal)
    if want_docs:
        for key in ("plan", "reading", "evidence"):
            head, total = _doc_head(slug, key, root=root)
            name = DOC_FILE[key]
            if not total:
                continue
            cut = f"（前 {DOC_HEAD} 字，全文 {total} 字）" if total > DOC_HEAD else ""
            lines += ["", f"── {name}{cut} ──", head or "（空）"]
    else:
        # 只报**有哪些、多长**，不倾泻正文：既不让模型以为"没有文档"，
        # 也不让它拿无关正文当上下文。
        have = []
        for key in ("plan", "reading", "evidence"):
            _, total = _doc_head(slug, key, root=root)
            if total:
                have.append(f"{DOC_FILE[key]}（{total} 字）")
        if have:
            lines += ["", "── 本课题已有文档 ──",
                      "  " + "、".join(have) +
                      "。**本轮没有自动展开** —— 要读用 "
                      "`topic_status(doc=\"plan\"|\"reading\"|\"evidence\")`，"
                      "它支持 `offset`/`limit` 取窗口。"]

    # ★ 未闭环卡点：全量浮现（有上限，超了如实报数）
    open_b = open_blockers(slug, root=root)
    if open_b:
        shown = open_b[:max(1, limit)]
        lines += ["", f"── 未闭环卡点 {len(open_b)} 条（新 → 旧，列 {len(shown)} 条）──"]
        for b in shown:
            tried = "；".join(f"（{i}）{t}" for i, t in enumerate(b["tried"], 1)) or "（**没写试过什么**）"
            lines.append(f"  [{b['id']}] {b['opened_at']} ｜ 卡在哪：{b['what']}")
            lines.append(f"        已经试过：{tried}" + (f"　位置：{b['where']}" if b["where"] else ""))
        if len(open_b) > len(shown):
            lines.append(f"  …… 还有 {len(open_b) - len(shown)} 条**没列出来**（不是不存在）——"
                         f" 要看全用 topic_status")
        lines += [
            "",
            "⚠️ 上面这些是**还没解决**的问题。开工前先扫一眼有没有撞上同一个坑；"
            "**这次如果解决了，用 `resolve_blocker` 把它闭环** —— 否则它会一直挂着，"
            "下次还会再提醒你一遍。",
        ]
    else:
        lines.append("")
        lines.append("（课题台账里**没有未闭环卡点**。）")

    # ★ 实验台账摘要（2026-11-02）。为什么必须**注入**，而不是等模型想起来查：
    #   用户的问题不是「给我看台账」，是「我下一步该干什么」「老师说我没数据」。
    #   要答这两句，模型先得**看见处境**；等它想起来调 `lab_report`，
    #   这一轮通常已经吐了一堆泛泛建议（这正是他抱怨的那种回答）。
    #   空台账 ⇒ 一个字都不输出 —— 遵守本函数顶上那条"不注入空壳"的纪律。
    #   硬上限 700 字（`LAB_DIGEST_MAX`）：这里多 200 字就是从 PLAN 头部抢 200 字。
    _dig = lab_digest(slug, root=root)
    if _dig:
        # 这一行是**指路**，不是摘要：台账只说"你现在在哪"，
        # 「该怎么做」在方法卡里（资产渲染，参数带页码）。没有这一行，
        # 模型会拿台账自己推方法 —— 那正是他抱怨的"泛泛而谈"。
        lines += ["", _dig, "",
                  "（方法/参数 → `lab_method`：staining 染色参数与失败分诊 / "
                  "identity 融合子身份证据链 / quant 定量指标与标尺；"
                  "一页汇报 → `lab_report`。**参数照抄卡里的，别凭常识补。**）",
                  "※ 这块**只在用户问进展/数据/下一步时才用得上** —— "
                  "没问到就别拿它当开场白。"]

    # ★ READING / EVIDENCE 引导（P3 的地基，W25）：
    #   台账里没有"读过什么"的记录，开题报告的证据表就只能靠回忆手抄 ——
    #   设计文档 §3.5 说的那个缺口。这里把"怎么记"以**动作**点名
    #   （模型执行的是它读到的动作：contract.py 那笔血债），每场任务都带上 ——
    #   代价约 150 字符，换来的是课题台账真正会长出 READING.md。
    lines += [
        "",
        "※ 每读一份材料（库里的或联网补的），顺手用 `topic_write(doc=\"reading\", …)` 记一笔："
        "doc_id + 从它拿到什么 + 和课题什么关系。",
        "  要下结论了用 `topic_write(doc=\"evidence\", …)` 攒**证据表** —— 每条都带"
        "〔库·doc_id p.页码〕或〔外网·来源〕锚，开题/写综述时才能直接整理成文，不用回头翻聊天记录。",
    ]
    return "\n".join(lines) + "\n"
