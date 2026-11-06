# -*- coding: utf-8 -*-
"""导师模式的**工作台**半边（W28）：有界文档索引 / 一窗正文 / 卡点分页 / 会话课题身份。

## 为什么单开这一套

`topic_test.py` 锁的是「台账本身对不对」（建/切/卡点/文档/注入），
`mentor_test.py` 锁的是「提示词与三条出处判据」。两者都**不碰**一个东西：

    界面上的工作台**要读台账**——读一份 PLAN 的全文、看已闭环的卡点、
    翻到第 2 页。而内核此前只有「摘要头 1200 字」这一种读法。

这半边有三个特有的静默失效方式，每一个都只能靠行为级判据抓：

| 机制 | 静默失效的样子 |
|---|---|
| 一窗正文 | 界面上"更多"按钮翻着翻着**回到开头**（offset 没被采用），正文看着完全正常 |
| 文档索引 | 一份**没写过**的文档被报成"写了 0 字"而不是"没有"；或反之，写了却被报成没有 |
| 会话课题身份 | 追问轮悄悄读到**别人刚切过去**的课题 —— 每一轮都成功，数据进了另一个课题 |

最后一条是 `topic_write` 那次实测事故（一整轮证据写进了 `cordyceps-cultivation`）
的同一条根因，所以它在这里必须双向成立：**CURRENT 变了，这场会话的课题不许变**。

## 一条刻意的记法

**每个断言都成对出现**（沿用 `topic_test.py`）：只测"翻页能翻"会漏掉"翻页翻反了"，
只测"按 open 筛出 2 条"会漏掉"其实筛了个寂寞"（总数恒等于全量）。
所以下面每处筛选都同时断言 `all` / `open` / `resolved` 三个数，且 `open_total`
必须**与筛选无关**地恒等于未闭环数。

## 写盘纪律

所有写操作落在 `runs/_regress/` 下的临时根里（`_tkit.fresh_dir` 走"挪走"而不是删，
见它开头那段）。真账 `<项目根>/topics/` 一个字节都不碰 —— 那是学生几个月的计划与卡点。

⚠️ 夹具的口径：`mentor_mode` 事件里的课题来自 `topic.current()`，测试**必须**把它
换成哨兵值，否则这条断言会去读用户真账上的 `CURRENT`（绿得毫无意义，还依赖别人
机器上的状态）。所以第 6/7 节全程 monkeypatch，并在 `finally` 里还原。
"""
from __future__ import annotations

import inspect
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _tkit as _tk                                            # noqa: E402
from rongke import loop as L                                   # noqa: E402
from rongke import topic as _T                                 # noqa: E402
from rongke.contract import TaskContract                       # noqa: E402
from rongke.gate import Verdict                                # noqa: E402
from rongke.llm import Usage                                   # noqa: E402
from rongke.loop import Rongke, _make_topic_tools              # noqa: E402
from rongke.tools import Tool, ToolRegistry                    # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}\n         实得 {got!r}")


def has(label, got, needle):
    check(label, needle in got, True)


def hasnt(label, got, needle):
    check(label, needle in got, False)


def raises(label, fn):
    global ok_all
    try:
        fn()
    except Exception as e:
        print(f"  [PASS] {label}（{type(e).__name__}: {str(e)[:60]}）")
        return e
    ok_all = False
    print(f"  [FAIL] {label} —— 没抛异常（失败被静默吞掉了）")
    return None


def _j(s: str) -> dict:
    return json.loads(s)


TMP = _tk.fresh_dir(ROOT / "runs" / "_regress" / "_mw_topics", "mw-topics")
_SCRATCH = ROOT / "runs" / "_regress"
_tf = _make_topic_tools(root=TMP)

print(f"临时台账根：{TMP}")

# ═══ 1. 有界文档索引：三份文档「有没有 / 多少字」必须**量出来** ══════════
print("\n── 1. 文档索引：有界、量化、不塞正文 ──")
_tf["topic_use"](slug="mw-a", title="工作台课题 A", question="Q-A？")
_idx = _j(_tf["topic_status"](slug="mw-a"))["doc_index"]
check("三份活文档各有条目（plan/reading/evidence 一个不少）",
      [x["doc"] for x in _idx], ["plan", "reading", "evidence"])
check("刚建好的课题：三份都没写过（exists 全 False）",
      [x["exists"] for x in _idx], [False, False, False])
check("没写过 ⇒ empty 全 True（不许报成「写了 0 字」）",
      [x["empty"] for x in _idx], [True, True, True])
check("没写过 ⇒ chars/total 都是 0", [(x["chars"], x["total"]) for x in _idx],
      [(0, 0), (0, 0), (0, 0)])
# ★ 有界：索引里**不许**带正文（带了就等于把整份台账塞进每次状态查询）
check("★ 索引里没有正文键（有界：正文只能按窗口取）",
      [k for x in _idx for k in ("text", "content", "body") if k in x], [])

_S = "".join(str(i % 10) for i in range(400))        # 恰好 400 字，无空白
_tf["topic_write"](doc="reading", text=_S, slug="mw-a")
_idx2 = {x["doc"]: x for x in _j(_tf["topic_status"](slug="mw-a"))["doc_index"]}
check("写过之后 reading 的 exists=True", _idx2["reading"]["exists"], True)
check("★ 字数按**真文件**量出来（400）", _idx2["reading"]["total"], 400)
# 400 < DOC_HEAD ⇒ 上下文里**给全了**，这时候喊 truncated 就是假警报。
check("负控：比头窗口短 ⇒ truncated=False（没截断就不许喊截断）",
      _idx2["reading"]["truncated"], False)
check("head_chars = 真给出的字数（短文档就是全文）",
      _idx2["reading"]["head_chars"], 400)
# 正控：写一份**超过头窗口**的，两处同时翻面 —— 这条与上面那条成对。
_LONG = "很长的一行。" * 400                     # 2400 字 > DOC_HEAD
_tf["topic_write"](doc="evidence", text=_LONG, slug="mw-a")
_idx3 = {x["doc"]: x for x in _j(_tf["topic_status"](slug="mw-a"))["doc_index"]}
check("★ 超过头窗口 ⇒ truncated=True（不把「没看全」渲染成「就这么多」）",
      _idx3["evidence"]["truncated"], True)
check("★ 而且说得出头窗口到底给了多少字（DOC_HEAD）",
      _idx3["evidence"]["head_chars"], _T.DOC_HEAD)
check("★ total 仍是全文（截断的是上下文摘录，不是文件本身）",
      _idx3["evidence"]["total"], len(_LONG))
check("负控：另外两份仍然 exists=False/True 各自成立（索引是逐份量出来的，不是抄一份）",
      (_idx3["plan"]["exists"], _idx3["reading"]["exists"]), (False, True))
check("★ `docs` 布尔表与索引同源（两处必须说同一件事）",
      _j(_tf["topic_status"](slug="mw-a"))["docs"],
      {"plan": False, "reading": True, "evidence": True})

# ═══ 2. 一窗正文：offset/limit 是字符窗口，越界要如实说 ════════════════
print("\n── 2. 一窗正文：窗口算得准、越界如实 ──")
_r = _j(_tf["topic_status"](slug="mw-a", doc="reading", offset=100, limit=50))["document"]
check("★ 窗口取到的是第 100–150 个字符（逐字相等）", _r["text"], _S[100:150])
check("chars = 本窗字数", _r["chars"], 50)
check("offset 原样回传（界面靠它算下一次从哪开始）", _r["offset"], 100)
# 前端读的是 `total_chars`，首版契约里也叫过 `total` —— 两个都必须在。
check("★ 全文长度：`total_chars` 与 `total` **同时**给出（前端两个名字都读过）",
      (_r["total_chars"], _r["total"]), (400, 400))
check("★ 后面还有 ⇒ truncated=True（界面据此画「更多」）", _r["truncated"], True)
check("★ 这就是 `mentor.js` 读的那组键，一个不缺",
      sorted(k for k in ("text", "offset", "total_chars", "total", "truncated",
                         "not_found", "empty", "chars") if k in _r),
      ["chars", "empty", "not_found", "offset", "text", "total", "total_chars",
       "truncated"])

_r2 = _j(_tf["topic_status"](slug="mw-a", doc="reading", offset=350, limit=12000))["document"]
check("★ 尾窗：从 350 到末尾（只有 50 字）", _r2["text"], _S[350:])
check("★ 到尾了 ⇒ truncated=False（两个方向都要成立，否则「更多」永远亮着）",
      _r2["truncated"], False)
_r3 = _j(_tf["topic_status"](slug="mw-a", doc="reading", offset=1000))["document"]
check("★ 越界：text 是空串（不编、不回绕到开头）", _r3["text"], "")
check("★ 越界：但 total 仍是真实全文长度（如实报「文件还在，只是这一窗空了」）",
      _r3["total_chars"], 400)
check("★ 越界：明确标 out_of_range=True（静默返回空串会被读成「文档是空的」）",
      _r3["out_of_range"], True)

# ═══ 3. 负控：非法输入要**拒**，缺失要**如实说** ═══════════════════════
print("\n── 3. 负控：非法 slug / 非法 doc / 没写过的一份 / 课题不存在 ──")
for _bad in ("../evil", "a/b", "x" * 49, ".", ".."):
    raises(f"非法 slug {_bad!r} 被拒（目录穿越不许得逞）",
           lambda b=_bad: _tf["topic_status"](slug=b, doc="plan"))
# 空串**不是**非法 slug：它的语义是「当前课题」（schema 里写着的那个默认值）。
# 拿它当"拒"来测，测的其实是另一条约定。
check("★ 空 slug = 当前课题（CURRENT 指向 mw-a），照旧读得到",
      _j(_tf["topic_status"](slug="", doc="plan"))["slug"], "mw-a")
for _bad in ("nope", "../../config.json", "PLAN.md", "plan.md.bak"):
    raises(f"★ 非法 doc {_bad!r} 被拒（doc 只能查 DOC_FILE 那张表，不许拼进路径）",
           lambda b=_bad: _tf["topic_status"](slug="mw-a", doc=b))
check("被拒之后临时根里只有 mw-a（没在根外面留下东西）",
      sorted(p.name for p in TMP.iterdir() if p.is_dir()), ["mw-a"])

_miss = _j(_tf["topic_status"](slug="mw-a", doc="plan"))["document"]
check("★ 没写过的那份：empty=True（诚实说没有）", _miss["empty"], True)
check("★ 没写过的那份：text 是空串、字数为 0", (_miss["text"], _miss["total_chars"]),
      ("", 0))
check("★ 课题在、文档没写过 ⇒ not_found=False，reason 说得出是「哪一步没有」",
      (_miss["not_found"], _miss["reason"]), (False, "no_file"))
_e_nf = raises("课题不存在 ⇒ 报错（不许回一份「空状态」冒充该课题）",
               lambda: _tf["topic_status"](slug="no-such-topic", doc="plan"))
# 报错必须点名已知课题：只说「不存在」等于没说（人得知道该改成哪个名字）。
has("★ 报错里列出已知课题（人才改得对）", str(_e_nf or ""), "mw-a")
check("★ 台账层的 `read_doc` 对不存在的课题另有诚实回包（两条路都要能读）",
      _T.read_doc("no-such-topic", "plan", root=TMP)["not_found"], True)

# ═══ 4. 没有当前课题：**空着说**，不猜不编 ════════════════════════════
print("\n── 4. 负控：没有当前课题时，状态如实空着 ──")
_EMPTY = _tk.fresh_dir(ROOT / "runs" / "_regress" / "_mw_empty", "mw-empty")
_ef = _make_topic_tools(root=_EMPTY)
_e = _j(_ef["topic_status"]())
check("空台账：current 是空串（不猜）", _e["current"], "")
check("空台账：依然 ok=True（没课题不是错误）", _e["ok"], True)
check("空台账：文档索引为空（不编三份假的出来）", _e["doc_index"], [])
check("空台账：卡点列表为空", (_e["blockers"], _e["blockers_total"]), ([], 0))
check("空台账：带 hint 告诉下一步该做什么", "topic_use" in _e.get("hint", ""), True)
# 空 slug 在 `read_doc` 这一层是**安全边界**（不是业务状态）：它没有「当前课题」
# 这层兜底 —— 兜底需要一次 `CURRENT` 读，而那正是 `topic_status` 干的活。
# 所以这里要的是"当场拒"，不是"回一份空正文"（后者会被读成"文档是空的"）。
raises("空台账：`read_doc` 拿空 slug 直接拒（空正文会被误读成「文档是空的」）",
       lambda: _T.read_doc("", "plan", root=_EMPTY))

# ═══ 5. 卡点：按状态筛 + 分页，`open_total` 与筛选**无关** ══════════════
print("\n── 5. 卡点分页：open / resolved / all 三个数必须各自成立 ──")
for _w in ("卡点一", "卡点二", "卡点三"):
    _tf["raise_blocker"](what=_w + "_MW", slug="mw-a")
_tf["resolve_blocker"](blocker_id="B001", how="当场解决", slug="mw-a")
_all = _j(_tf["topic_status"](slug="mw-a", status="all"))
_op = _j(_tf["topic_status"](slug="mw-a", status="open"))
_rs = _j(_tf["topic_status"](slug="mw-a", status="resolved"))
check("all ⇒ 3 条", (_all["blockers_total"], len(_all["blockers"])), (3, 3))
check("★ open ⇒ 只出 2 条未闭环（逐条判状态，不是随便截两条）",
      (len(_op["blockers"]), [b["status"] for b in _op["blockers"]]),
      (2, ["open", "open"]))
check("★ resolved ⇒ 只出 1 条，且带「怎么解决的」",
      (len(_rs["blockers"]), [b["resolved_how"] for b in _rs["blockers"]]),
      (1, ["当场解决"]))
check("★ 三个筛选的 total 各不相同（同一个数就是筛了个寂寞）",
      (_all["blockers_total"], _op["blockers_total"], _rs["blockers_total"]),
      (3, 2, 1))
check("★ open_total 与筛选**无关**：三次都等于未闭环数（界面角标不许时有时无）",
      (_all["open_total"], _op["open_total"], _rs["open_total"]), (2, 2, 2))
check("卡点条目仍带旧的 `at` 键（旧调用方按它渲染时间）",
      sorted(_op["blockers"][0])[:2], ["at", "id"])
check("★ 工具层不吐 `items`（那是面板的键；工具契约里历史上只有 `blockers`）",
      "items" in _op, False)

_pg = _j(_tf["topic_status"](slug="mw-a", status="all", offset=1, limit=1))
check("★ 分页：offset=1 limit=1 ⇒ 恰好 1 条", (len(_pg["blockers"]), _pg["offset"],
                                              _pg["limit"]), (1, 1, 1))
check("★ 分页：total 仍是全量 3（不是本页条数）", _pg["blockers_total"], 3)
check("★ 分页：后面还有 ⇒ note 明说「还有几条没列出来」（M 型）",
      "没列出来" in _pg.get("note", ""), True)
check("负控：翻到头那一页就不该再喊「没列出来」",
      "note" in _j(_tf["topic_status"](slug="mw-a", status="all", offset=2, limit=1)),
      False)

# 面板用的那条路（`blocker_page`）：键名是前端读的那一组
_bp = _T.blocker_page("mw-a", status="open", offset=0, limit=100, root=TMP)
check("★ 面板契约：`items` 与 `blockers` 同时给出（前端两个名字都读过）",
      (_bp["items"] == _bp["blockers"], len(_bp["items"])), (True, 2))
check("★ 面板契约：total / offset / limit / truncated 齐全",
      (_bp["total"], _bp["offset"], _bp["limit"], _bp["truncated"]),
      (2, 0, 100, False))
raises("面板契约：status 只收 open/resolved/all（写错了要当场报，不许静默当 all）",
       lambda: _T.blocker_page("mw-a", status="whatever", root=TMP))

# ═══ 6. 单源：工具层与台账层必须算出**同一个数** ═══════════════════════
print("\n── 6. 单源核对：`topic_status` 与 `topic.status_view` 不许各算一套 ──")
_sv = _T.status_view("mw-a", root=TMP, status="open", limit=2)
_tj = _j(_tf["topic_status"](slug="mw-a", status="open", limit=2))
check("★ blockers_total 同源", _tj["blockers_total"], _sv["blockers_total"])
check("★ open_total 同源", _tj["open_total"], _sv["open_total"])
check("★ 列出的条目数同源", len(_tj["blockers"]), len(_sv["blockers"]))
check("★ 文档索引同源（逐字相等）", _tj["doc_index"], _sv["doc_index"])
check("索引确实是**同一份函数**算的（不是两处复制粘贴）",
      "doc_index(" in inspect.getsource(_T.status_view), True)

# ═══ 7. `mentor_mode` 事件必须带**本场会话**的课题 ═════════════════════
print("\n── 7. 事件字段：新任务的 mentor_mode 带上开局那一刻的课题 ──")


class ScriptLLM:
    """按脚本逐轮返回；`seen` 留档每一轮**实际收到**的消息。`boom_at` 让某轮抛异常。"""

    def __init__(self, script, boom_at=None):
        self.script = list(script)
        self.seen = []
        self.boom_at = boom_at

    def chat(self, messages, tools=None, meta=None):
        i = len(self.seen)
        self.seen.append([dict(m) for m in messages])
        if self.boom_at is not None and i == self.boom_at:
            raise RuntimeError("模拟网关炸了")
        content, calls = self.script[i] if i < len(self.script) else ("", [])
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = [
                {"id": f"c{i}_{j}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                for j, (n, a) in enumerate(calls)]
        return msg, Usage(), ""


class FakeGate:
    def reset(self):
        pass

    def check(self, claim, contract):
        return Verdict(passed=True, reasons=[], feedback="通过。")


class FakeContract:
    def __init__(self, tag="原件"):
        self.tag = tag
        self.raw_goal = "测试工作台"

    def to_prompt(self):
        return "目标：测试工作台 / tag=" + self.tag

    def to_dict(self):
        return {"raw_goal": "测试工作台", "tag": self.tag}


CALL = lambda n, **a: (n, a)                       # noqa: E731
DELIVER = lambda: CALL("deliver_task", summary="做完了", deliverables=["a.md"],  # noqa: E731
                       evidence=[{"call_seq": 1, "claim": "有证据"}], draft_text="正文。")


class Kernel:
    """`_run_serial` 能跑的最小心智内核。

    ★ 转调 vs staticmethod 的坑见 `resume_test.py` 的 `Kernel` 文档字符串：
      `_loop` 是实例方法，**必须**写成普通方法转调（包成 staticmethod ⇒
      `_run_serial` 传的 `goal` 字符串被顶成 `self`，报 `'str' object has no
      attribute 'cfg'`，看着像产品代码坏了）。
    """

    _draft_for_display = staticmethod(Rongke._draft_for_display)
    _written_files = staticmethod(Rongke._written_files)
    _archive = staticmethod(Rongke._archive)

    def _loop(self, *a, **kw):
        return Rongke._loop(self, *a, **kw)

    def _charge_billing(self, result):
        return Rongke._charge_billing(self, result)

    def make_workspace(self, tag: str = "") -> Path:
        return Rongke.make_workspace(self, tag)

    def __init__(self, max_steps=6, boom_at=None, script=None):
        self.cfg = SimpleNamespace(
            provider=SimpleNamespace(name="test", model="test-model"),
            pricing=None,
            billing=SimpleNamespace(enabled=True),
            runtime=SimpleNamespace(workspace_dir=ROOT / "runs"),
            loop=SimpleNamespace(max_steps=max_steps, max_observation_chars=4000,
                                 max_identical_calls=3, context_warn_chars=0))
        self.llm = ScriptLLM(script or [("交", [DELIVER()])], boom_at=boom_at)
        self.gate = FakeGate()
        self.registry = ToolRegistry()
        self.registry.register(Tool(
            name="probe", description="探针",
            parameters={"type": "object", "properties": {}},
            fn=lambda **kw: "探到了一些东西"))
        self.telemetry = None
        self.memory = None
        self.fence = None
        self.billing_root = _SCRATCH / "_billing_mw"
        self.mcp = SimpleNamespace(workspace=None, stop_all=lambda: None)


def _fixture_gaps(fixture, fns) -> list:
    """真实源码伸手要的 `self.X`（只算读取）里，夹具缺了哪些。

    夹具缺一个属性时报出来的 AttributeError 与被测代码的错**一模一样**，
    所以不靠记性、靠机器（同 `resume_test.py` 第 0 节）。
    """
    reads, writes = set(), set()
    for s in [inspect.getsource(f) for f in fns]:
        reads |= set(re.findall(r"self\.([A-Za-z_]\w*)", s))
        writes |= set(re.findall(r"self\.([A-Za-z_]\w*)\s*=(?!=)", s))
    return sorted(n for n in (reads - writes) if not hasattr(fixture, n))


check("★ 夹具覆盖了真代码要的全部协作对象（缺了当场报，别等跑到一半才炸）",
      _fixture_gaps(Kernel(), (Rongke._run_serial, Rongke._loop)), [])
check("★ `_loop` 必须是普通方法而不是 staticmethod（否则 self 会被 goal 顶掉）",
      isinstance(vars(Kernel).get("_loop"), staticmethod), False)


def newws(name: str) -> Path:
    return _tk.fresh_dir(_SCRATCH / name, f"mw-{name}")


def run_kernel(k, ws, goal="工作台测试", c=None, resume=None, kb_only=False,
               max_steps=6, llm=None):
    if llm is not None:
        k.llm = llm
    ev: list = []
    r = Rongke._run_serial(k, goal, c or FakeContract(), max_steps,
                           lambda kind, d: ev.append((kind, d)), ws,
                           None, None, resume, kb_only)
    return r, ev


def mentor_ev(ev):
    return [d for k, d in ev if k == "mentor_mode"]


_orig_current = _T.current
try:
    # ── 7.1 新任务：事件里带开局那一刻的 current() ──
    _T.current = lambda root=None: "mw-probe-new"
    _W1 = newws("_mw_w1")
    _k1 = Kernel(script=[("交", [DELIVER()])])
    _r1, _ev1 = run_kernel(_k1, _W1, kb_only=True)
    check("前提复核：这一场跑成功了", bool(_r1 and _r1.success), True)
    check("★ mentor_mode 事件带上了开局那一刻的课题",
          [d.get("topic") for d in mentor_ev(_ev1)], ["mw-probe-new"])
    check("★ `on` 照旧在（新字段是**加**的，不是替换）",
          [d.get("on") for d in mentor_ev(_ev1)], [True])
    # 归档是回放的唯一真相：事件必须落进 session.json，否则重启后历史页只能猜
    _a1 = json.loads((_W1 / "session.json").read_text(encoding="utf-8"))
    check("★ 归档里也带着这条事件（历史回放靠它复原课题，前端不用猜）",
          [d.get("topic") for k, d in
           [(e["kind"], e["data"]) for e in _a1["events"]] if k == "mentor_mode"],
          ["mw-probe-new"])
    check("★ 成功收尾改名后的快照里也带着课题（`.done.json` 同样要能复原）",
          json.loads((_W1 / "conversation.done.json").read_text(encoding="utf-8"))
          .get("topic"), "mw-probe-new")

    # ── 7.2 负控：台账没有当前课题 ⇒ 事件里是空串，不许编一个 ──
    _T.current = lambda root=None: ""
    _W2 = newws("_mw_w2")
    _r2, _ev2 = run_kernel(Kernel(), _W2)
    check("★ 负控：没有当前课题 ⇒ topic 是空串（不猜、不编）",
          [d.get("topic") for d in mentor_ev(_ev2)], [""])
    check("★ 负控：`on=False` 时这条事件照样落盘（回放才说得出「当时没开」）",
          [d.get("on") for d in mentor_ev(_ev2)], [False])

    # ── 7.3 追问/续跑：认**快照里那份**课题，不认此刻的 CURRENT ──
    _T.current = lambda root=None: "mw-other-topic"      # 别人刚切走了
    _W3 = newws("_mw_w3")
    _snap_msgs = [{"role": "system", "content": "S"},
                  {"role": "user", "content": "U"}]
    _k3 = Kernel(boom_at=1, script=[("一", [CALL("probe")]), ("二", [CALL("probe")])])
    _resume3 = {"messages": _snap_msgs, "contract": FakeContract().to_dict(),
                "step": 1, "topic": "mw-snap-topic", "follow_up": "接着说"}
    _r3, _ev3 = run_kernel(_k3, _W3, goal="接着说", resume=_resume3, kb_only=True)
    check("★ 追问轮：事件里的课题来自**快照**，不是此刻的 CURRENT（否则会串到别人的课题）",
          [d.get("topic") for d in mentor_ev(_ev3)], ["mw-snap-topic"])
    check("负控：CURRENT 确实是另一个（证明上一条不是碰巧相等）",
          _T.current(), "mw-other-topic")
    _snap3 = json.loads((_W3 / "conversation.json").read_text(encoding="utf-8"))
    check("★ 续跑落盘的快照继续带着这个课题（下一轮还能认回来）",
          _snap3.get("topic"), "mw-snap-topic")

    # ── 7.4 再让它跑一次：这次 CURRENT 又变了，课题仍必须还是快照里那个 ──
    _T.current = lambda root=None: "mw-third-topic"
    _W4 = newws("_mw_w4")
    _k4 = Kernel(boom_at=1, script=[("一", [CALL("probe")]), ("二", [CALL("probe")])])
    _resume4 = {k: v for k, v in _snap3.items() if k != "follow_up"}
    _r4, _ev4 = run_kernel(_k4, _W4, goal="工作台测试", resume=_resume4)
    check("★ 第二轮续跑仍然认快照里的课题（CURRENT 换了两次都不影响）",
          [d.get("topic") for d in mentor_ev(_ev4)], ["mw-snap-topic"])
    check("★ 而且快照里那份没被 CURRENT 覆盖",
          json.loads((_W4 / "conversation.json").read_text(encoding="utf-8")).get("topic"),
          "mw-snap-topic")

    # ── 7.5 写台账仍然必须**显式** slug（会话课题不许变成隐含默认值）──
    raises("★ 追问轮里 `topic_write` 缺 slug 照样报错（会话身份不是写入授权）",
           lambda: _tf["topic_write"](doc="plan", text="x"))
    _err = None
    try:
        _tf["topic_write"](doc="plan", text="x")
    except Exception as e:                                    # noqa: BLE001
        _err = str(e)
    has("★ 报错里点名 current() 与已知课题（人才查得出来写哪去了）",
        _err or "", "已知课题")
finally:
    _T.current = _orig_current
check("`topic.current` 已还原", _T.current is _orig_current, True)

# ═══ 8. 承重自检：拆掉机制，前面的断言必须当场失守 ═══════════════════
print("\n── 8. ★ 承重自检：把机制拆掉，对应的断言必须失守 ──")

print("\n   8.1 拆掉窗口 ⇒ 第 2 节的「逐字相等」必须失守")
_orig_read = _T.read_doc


def _no_window(slug, doc, offset=0, limit=None, root=None):
    """一个**无视 offset/limit** 的实现（"反正界面只显示前一段"）。"""
    full = _orig_read(slug, doc, offset=0, limit=10 ** 9, root=root)
    full["text"], full["chars"] = full["text"], len(full["text"])
    return full


_T.read_doc = _no_window
try:
    _bad = _T.read_doc("mw-a", "reading", offset=100, limit=50, root=TMP)
    check("★ 无视窗口之后，拿到的**不是**第 100–150 字（证明第 2 节测的是窗口）",
          _bad["text"] == _S[100:150], False)
    check("★ 而且它会把「未截断」这一点也说错（连带 truncated 一起失守）",
          _bad["truncated"], False)
finally:
    _T.read_doc = _orig_read
check("`read_doc` 已还原", _T.read_doc is _orig_read, True)

print("\n   8.2 拆掉状态过滤 ⇒ 第 5 节的三个数必须塌成一个")
_orig_page = _T.blocker_page


def _no_filter(slug, status="", offset=0, limit=10, root=None):
    """一个**忽略 status** 的实现（"反正卡点都列出来，界面自己筛"）。"""
    return _orig_page(slug, status="all", offset=offset, limit=limit, root=root)


_T.blocker_page = _no_filter
try:
    _badop = _T.blocker_page("mw-a", status="open", limit=100, root=TMP)
    check("★ 不过滤之后，open 也端出了已闭环（证明第 5 节的 open 断言在承重）",
          len(_badop["items"]), 3)
    check("★ 而且「all/open/resolved 三数不同」当场塌成一个（三条断言同时失守）",
          _badop["total"] == _T.blocker_page("mw-a", status="resolved",
                                             root=TMP)["total"], True)
finally:
    _T.blocker_page = _orig_page
check("`blocker_page` 已还原", _T.blocker_page is _orig_page, True)

print("\n   8.3 抽掉快照里那个 `topic` 键 ⇒ 第 7.3 节必须退化成「读 CURRENT」")
# 为什么这样拆而不是 patch `_run_serial`：`_loop` 里还有**第二道**兜底
# （`resume["topic"]`），把传参抹掉根本拆不动机制 —— 第一次写就栽在这里，
# 两条断言全绿（假绿），故留记。真正承重的是快照里那个键本身：
# 抽掉它，身份就只剩 `CURRENT()` 可依 —— 而那正是"串到别人课题"的失效形状。
_W5 = newws("_mw_w5")
_T.current = lambda root=None: "mw-other-topic"
_k5 = Kernel(boom_at=1, script=[("一", [CALL("probe")]), ("二", [CALL("probe")])])
_resume5 = {"messages": _snap_msgs, "contract": FakeContract().to_dict(),
            "step": 1, "follow_up": "接着说"}          # ← 只有 topic 被抽掉
_r5, _ev5 = run_kernel(_k5, _W5, goal="接着说", resume=_resume5, kb_only=True)
check("★ 抽掉之后，事件里落的就是 CURRENT（证明 7.3 那条靠的是快照里那个键）",
      [d.get("topic") for d in mentor_ev(_ev5)], ["mw-other-topic"])
check("★ 与 7.3 的期望值**不同**（那条断言因此有判别力，而不是碰巧相等）",
      [d.get("topic") for d in mentor_ev(_ev5)] == ["mw-snap-topic"], False)
_T.current = _orig_current
check("`topic.current` 已还原（第 8.3 节收尾）", _T.current is _orig_current, True)

# ═══ 9. 注册面：工具集与文档口径（改工具必须同步改数字） ═══════════
print("\n── 9. 注册面：课题工具集恰好 10 个，且与文档口径一致 ──")
# ★ 2026-11-02 由「恰好 9 个」改为「恰好 10 个」。
#   本轮加的是 `lab_method` —— 起因是一次**能力空转**：
#   `lab_assets.DAPI_PROTOCOLS / STAIN_FAILURE_BRANCHES / STAIN_FIRST_RUN`
#   早就在（每一条都挂着 doc_id + 页码），但**没有任何工具读它**，
#   只有测试引用过 ⇒ 它"通过"了，而模型永远看不见。
#   ＝ 本仓那条定律的第二次复发：**资产不进工具面，等于没写**。
#   ⇒ 加的是**把资产送到模型面前**的那只手，不是又写了一份建议。
# ★ 2026-11-02 由「恰好 5 个」改为「恰好 7 个」，同日再改「9 个」。
#   第一次（5 ⇒ 7）加的是 `topic_link` / `topic_update`：
#   用户原话「直接在对话模式里发消息就能管理课题、给他关联文献」，
#   而当时的现场是 `set_library()` 早就有、面板按钮就在调它，
#   模型侧**一个能把文献挂到课题上的工具都没有**；四个真实课题的 `library`
#   全是 0，即这套关联在生产上**一次都没被用过**。
#   第二次（7 ⇒ 9）加的是 `lab_record` / `lab_report`：
#   用户原话「老师说我没数据，在催我，我也很迷茫」「我需要他指导我下一步该干什么」。
#   核完发现他不是没做实验，是**做的全是定性观察**（"三株长得慢""其他几株快"
#   一条数字都没有）—— 缺的是**结构**，不是建议。
#   所以这两次都是补**根本不存在的东西**，不是"顺手多加几个"。
#   ⇒ 改的是**口径**（5 ⇒ 7 ⇒ 9），不是把断言绕过去：
#   工具数同时被 `product_doc_test` 与 `docs/产品说明.md` 的「内置 N 个工具」
#   钉着（现为 65），三处必须一起动，否则文档数字当场腐坏。
check("★ 课题工具恰好 10 个（与 `docs/产品说明.md` 的 66 个、`product_doc_test` 同口径）",
      sorted(_tf), ["lab_method", "lab_record", "lab_report", "raise_blocker",
                    "resolve_blocker", "topic_link", "topic_status", "topic_update",
                    "topic_use", "topic_write"])
check("★ `topic_status` 的 schema 是**加**字段（slug 还在，旧调用方不受影响）",
      sorted(L.TOPIC_STATUS_SCHEMA["properties"]),
      ["doc", "limit", "offset", "slug", "status"])
check("★ 新参数全部**可省**（没有 required ⇒ 旧调用行为一字不差）",
      L.TOPIC_STATUS_SCHEMA.get("required"), None)
# 工具说明是模型唯一的发现入口：schema 里有字段、说明里不提，模型就不会用
# （"能力在、没人知道"= 假开关，与 W25 那条挂载不变量同一条道理）。
_inst = inspect.getsource(Rongke._install_builtins)
has("工具说明里点了 `doc=plan|reading|evidence`（模型得知道这条路）",
    _inst, "doc=plan|reading|evidence")
has("工具说明里点了 `status=open|resolved|all`", _inst, "status=open|resolved|all")

# ═══ 10. 首条上下文与回放必须说**同一个**课题 ══════════════════════════
print("\n── 10. 首条上下文 / 事件 / 快照：三处课题身份不许分叉 ──")
# 为什么这节值得单开：`initial_messages` 与 `mentor_mode` 事件各自都会去问
# "用哪个课题"。两处各读一次 `CURRENT()` ⇒ 提示词说 A、事件记 B，
# **没有任何报错**，只有翻历史时才发现"这场会话到底动了哪个课题"答不上来。
# 修法是让它们共用同一个入参（`_sess_topic`），这节就是钉住那个"共"字。
_TK = _tk.fresh_dir(ROOT / "runs" / "_regress" / "_mw_ident", "mw-ident")
_tf2 = _make_topic_tools(root=_TK)
# 台账根必须也指到临时目录：`_run_serial`/`_loop` 里那条 `initial_messages`
# **故意不传** `topics_root`（单源 = `topic.topics_root()`），所以只能从这一层换根。
# 不换根的后果不是"测试失败"，而是**读到用户真账**（绿得毫无意义，还依赖别人的机器）。
_orig_topics_root = _T.topics_root
_T.topics_root = lambda root=None: Path(root) if root is not None else _TK
_tf2["topic_use"](slug="mw-ident-a", title="身份课题 A", question="A？")
_tf2["topic_use"](slug="mw-ident-b", title="身份课题 B", question="B？")
_tf2["topic_write"](doc="plan", text="A 的计划正文 IDENT_A_9f1", slug="mw-ident-a")
_tf2["topic_write"](doc="plan", text="B 的计划正文 IDENT_B_9f1", slug="mw-ident-b")
_tf2["topic_use"](slug="mw-ident-b")                      # CURRENT = b

# ★ 2026-11-04 修判据：文档正文自 11-03 起改成**按需注入** ——
#   goal 里没有「计划 / 开题 / 台账 …」这类触发词就不展开（见 topic._needs_docs）。
#   旧断言用 raw_goal="身份测试" 却要求首条消息里出现正文，量的是旧版行为。
#   「身份隔离」这条判据仍然承重，只是**前提要先成立**：先问到台账，再验读的是哪一份。
_DOC_GOAL = "我的计划写了什么"
_m_pin = L.initial_messages(TaskContract(raw_goal=_DOC_GOAL), None,
                            topics_root=_TK, topic_slug="mw-ident-a")[1]["content"]
_m_cur = L.initial_messages(TaskContract(raw_goal=_DOC_GOAL), None,
                            topics_root=_TK)[1]["content"]
has("前提复核：台账就绪且问到文档，不指定 slug 时读到的是 CURRENT=b",
    _m_cur, "IDENT_B_9f1")
hasnt("前提复核：那时**看不到** a 的正文（两份台账确实分得开）", _m_cur, "IDENT_A_9f1")
check("★ 指定 slug=a ⇒ 首条上下文讲的是 a（**不**跟着 CURRENT 走）",
      ("IDENT_A_9f1" in _m_pin, "mw-ident-a —— 身份课题 A" in _m_pin), (True, True))
hasnt("★ 而且**没有**把 b 的内容混进来（不串台账）", _m_pin, "IDENT_B_9f1")
has("★ 明写的 slug 也是 a（写台账时要传的那个值）",
    _m_pin, 'slug="mw-ident-a"')
check("负控：空 slug 仍退回 CURRENT（老行为一字不变，`topic_test` 第 6 节靠它）",
      ("IDENT_B_9f1" in _m_cur, "IDENT_A_9f1" in _m_cur), (True, False))
# ★ 按需注入的另一个方向：寒暄**不倾泻**正文，但也不许装作没有文档。
_m_hi = L.initial_messages(TaskContract(raw_goal="你好"), None,
                           topics_root=_TK)[1]["content"]
hasnt("负控：寒暄不展开任何一份台账正文", _m_hi, "IDENT_B_9f1")
hasnt("负控：寒暄同样不展开 a 的正文", _m_hi, "IDENT_A_9f1")
has("负控仍如实列出已有文档并给读回工具（不展开 ≠ 不存在）",
    _m_hi, "topic_status")
check("负控：台账里什么都没有时，仍然**不注入空壳**",
      L.initial_messages(TaskContract(raw_goal="x"), None,
                         topics_root=_tk.fresh_dir(
                             ROOT / "runs" / "_regress" / "_mw_ident0",
                             "mw-ident0"),
                         topic_slug="")[1]["content"].count("课题上下文"), 0)

# ── 答复形态：恒注入（2026-11-03）──────────────────────────────
# 起因：用户说「我是要让用户提出这种问题的时候，不再有卡点」。
# 而形态约束原先只长在 `mentor_mode_text()` 里 —— 那段**只在 kb_only 时**进提示词。
# 于是不开导师模式问「下一步干什么」，反问「你的株号是多少」是合规的 ——
# 那正是卡点。所以形态必须与开关解耦。
# ★ 2026-11-04 修判据：形态段的标题与措辞都改过（现为「两件用户抱怨过的事」），
#   旧断言在找「答复形态」「不许用反问开头」两串早已不在的字面量。
#   改成**从产品函数取首句/整段**当哨兵 ⇒ 以后再改措辞，判据不会跟着过期；
#   但内容本身仍被单独钉住，防止把函数改空后「段不在却全绿」。
_mark = L.mentor_mode_text().splitlines()[0]
_shape = L.answer_shape_text().strip()
check("前提复核：形态段本身非空、两句职责都在（防止函数被改空还全绿）",
      (len(_shape) > 20, "交付了文件" in _shape, "反问" in _shape),
      (True, True, True))
_plain = L.initial_messages(TaskContract(raw_goal="我下一步该干什么"), None,
                            topics_root=_TK, kb_only=False,
                            topic_slug="mw-ident-a")[1]["content"]
has("★ 没开导师模式也带完整形态段（卡点不因开关而存在）", _plain, _shape)
hasnt("负控：这不是导师模式泄漏进来的（MENTOR_MARK 不在）", _plain, _mark)
_open = L.initial_messages(TaskContract(raw_goal="我下一步该干什么"), None,
                           topics_root=_TK, kb_only=True,
                           topic_slug="mw-ident-a")[1]["content"]
has("开了导师模式也带完整形态段", _open, _shape)
check("形态段只出现一次（两处都写＝同一句话互相稀释）", _open.count(_shape), 1)

# ★ 端到端：**同一次运行里**，模型真看到的课题 与 事件/快照记的课题必须一字不差。
#   判别力靠一个「会变的 CURRENT」：第一次读给 a，第二次读给 b。
#   两次读就会分叉（提示词说 b、事件记 a）—— 正是工单点的那条"首条与回放不一致"。
_W6 = newws("_mw_w6")
_reads = {"n": 0}


def _flaky_current(root=None):
    _reads["n"] += 1
    return "mw-ident-a" if _reads["n"] == 1 else "mw-ident-b"


_k6_ctx = {}


class _SpyLLM(ScriptLLM):
    """把**首轮实际收到**的第一条 user 消息留档。

    比再调一次 `initial_messages` 硬：后者测的是函数，这测的是接线 ——
    传参接错（少写一个 `topic_slug=`）在函数层看不出来。
    """

    def chat(self, messages, tools=None, meta=None):
        if not _k6_ctx:
            _k6_ctx["first"] = [m for m in messages
                                if m.get("role") == "user"][0]["content"]
        return super().chat(messages, tools, meta)


_T.current = _flaky_current
_r6, _ev6 = run_kernel(Kernel(script=[("交", [DELIVER()])]), _W6, kb_only=True,
                       llm=_SpyLLM([("交", [DELIVER()])]))
# 成功收尾会把 `conversation.json` **改名**成 `conversation.done.json`
# （`SNAPSHOT_RENAMES`；挪不是删）。这里两个都认，免得判据依赖收尾方式。
_W6_snap = json.loads(next(p for p in (_W6 / "conversation.done.json",
                                       _W6 / "conversation.json")
                           if p.exists()).read_text(encoding="utf-8"))
check("★ 整场只读了**一次** CURRENT（读两次就会分叉，这就是那处失效的根）",
      _reads["n"], 1)
has("★ 模型真看到的课题 = a（第一次读到的那个）", _k6_ctx.get("first", ""),
    "mw-ident-a —— 身份课题 A")
hasnt("★ 提示词里**没有** b（没有第二次读留下的痕迹）",
      _k6_ctx.get("first", ""), "mw-ident-b")
check("★ 事件记的也是 a —— 与首条上下文**同一个值**（捕获与回放一致）",
      [d.get("topic") for d in mentor_ev(_ev6)], ["mw-ident-a"])
check("★ 快照记的还是 a —— 三处同源（不然历史页只能猜）",
      _W6_snap.get("topic"), "mw-ident-a")
_T.current = _orig_current
_T.topics_root = _orig_topics_root
check("`topic.current` / `topic.topics_root` 都已还原",
      (_T.current is _orig_current, _T.topics_root is _orig_topics_root), (True, True))

print("\n═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
