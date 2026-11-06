# -*- coding: utf-8 -*-
"""跨进程断点续跑单测 —— 对话快照 / 崩溃归档 / resume 围栏。

## 为什么单开这一套

`intervene` 那条"从断点续跑"只活在**同一个进程**里：服务一重启、任务一崩，
内存里的消息栈就没了 —— 用户看到的是"跑到第 7 步的东西全没了，只能重来"。
评审第 2 条骂的「runs 里 7 个中断任务没有归档」是同一个根因的两个侧面。

本轮补三件事，每一件都有**特有的静默失效方式**：

| 机制 | 静默失效的样子 |
|---|---|
| 每步落盘快照 | 写了，但写的是**别的目录**／写到一半被 kill 留下半个 JSON |
| 续跑接回对话 | 看着"接着跑"了，其实**重新装配**了一份 —— 前情全丢，还换了把尺子 |
| 崩溃也归档 | 归档写出来了，但 `step`/原因填的是默认值 ⇒ 事后查不出停在哪 |

## 一条关键的方法论

「续跑接回了前情」**不能**看内核打算做什么（那是自述，R 型），只能看
**模型下一轮真正收到了什么**。所以这里的假模型依旧留档每一轮 messages ——
和 `intervene_test.py` 同一套路，因为要防的是同一类自欺。

承重自检照旧：短路 `resume` 分支，正控必须当场失守。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ★ 账单隔离：**不要**用 `RONGKE_DATA` 把整个数据根改道。
#   它同时决定 `workspace_dir`（`<data_root>/runs`），一改，本套件自己建的
#   工作区与内核算出来的工作区就分家了 —— 实测报「这个任务没有可续跑的
#   对话快照」（快照其实在，只是在另一棵树下）。
#   所以这里只钉 `BILLING_ROOT`（见下），数据根保持默认。

# 拆夹具一律走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                             # noqa: E402
_SCRATCH = ROOT / "runs" / "_regress"                         # 影子副本落点（挪去，不删）

from rongke import loop as L                                  # noqa: E402
from rongke.contract import TaskContract                      # noqa: E402
from rongke.gate import Verdict                               # noqa: E402
from rongke.llm import Usage                                  # noqa: E402
from rongke.loop import Rongke                                # noqa: E402
from rongke.tools import Tool, ToolRegistry                   # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}\n         实得 {got!r}")


def CALL(name: str, **args):
    return (name, args)


def DELIVER(summary="做完了"):
    return CALL("deliver_task", summary=summary, deliverables=["a.md"],
                evidence=[{"call_seq": 1, "claim": "有证据"}], draft_text="正文一段。")


class ScriptLLM:
    """按脚本逐轮返回；留档每一轮**实际收到**的消息。`boom_at` 让某轮直接抛异常。

    `usages`：逐轮返回的用量（默认全 0，保持既有调用方不变）。
    `on_boom`：抛异常**之前**跑一下 —— 只给"承重自检"用来抹掉现场别名。
    """

    def __init__(self, script: list, boom_at: int | None = None,
                 usages: list | None = None, on_boom=None):
        self.script = list(script)
        self.seen: list[list[dict]] = []
        self.boom_at = boom_at
        self.usages = usages
        self.on_boom = on_boom

    def chat(self, messages, tools=None, meta=None):
        i = len(self.seen)
        self.seen.append([dict(m) for m in messages])
        if self.boom_at is not None and i == self.boom_at:
            if self.on_boom is not None:
                self.on_boom()
            raise RuntimeError("模拟网关炸了")
        content, calls = self.script[i] if i < len(self.script) else ("", [DELIVER()])
        msg: dict = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = [
                {"id": f"c{i}_{j}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                for j, (n, a) in enumerate(calls)]
        return msg, (self.usages[i] if self.usages and i < len(self.usages)
                     else Usage()), ""


class FakeGate:
    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.checks = 0

    def reset(self):
        self.checks = 0

    def check(self, claim, contract):
        self.checks += 1
        if self.checks <= self.fail_times:
            return Verdict(passed=False, reasons=["打回"], feedback="打回。")
        return Verdict(passed=True, reasons=[], feedback="通过。")


class FakeContract:
    """带一个可辨认的标记字段 —— 「续跑有没有重新起草契约」靠它验。"""

    def __init__(self, tag="原件"):
        self.tag = tag
        # 真契约一定有 `raw_goal`（`to_prompt()` 里无条件用它），夹具就得有 ——
        # 夹具少一个真实现会读的字段，等于把「产品会不会崩」这件事测成假的。
        self.raw_goal = "测试续跑"
        # ★ W36：遍历收尾处按 `needs_writing` 决定发不发任务单（写稿才发）。
        #   真契约总有这个字段，夹具也补上 —— 缺了会让"续跑时界面长不长任务单"
        #   这件事永远测不到（`getattr` 兜底成 False，看起来永远像"非写稿"）。
        self.needs_writing = False

    def to_prompt(self):
        return "目标：测试续跑 / tag=" + self.tag

    def to_dict(self):
        return {"raw_goal": "测试续跑", "tag": self.tag}


class Kernel:
    """`_loop` 与 `_run_serial` 都要能跑的最小心智内核。

    ★ 接手真实现有**两种**方式，弄错就静默绑错 `self`：

    | 真实现的样子 | 夹具怎么写 | 写错的后果 |
    |---|---|---|
    | `Rongke` 上是 `@staticmethod` | `staticmethod(Rongke.x)` | 多绑一层，参数右移 |
    | `Rongke` 上是普通方法 | 写成**普通方法**转调 | `self` 不绑 ⇒ 第一个实参被顶成 `self` |

    第二种刚烧过一次：`_loop` 被 `staticmethod` 包了 ⇒ `_run_serial` 传的
    `goal`（一个字符串）成了 `self`，报出来是
    `'str' object has no attribute 'cfg'` —— 看着像产品代码坏了。

    另：夹具"少一个 `self.xxx`"的坑在本文件里烧了**三次**
    （`_draft_for_display` / `_archive` / `_loop`）。所以下面第 0 节不靠记性，
    靠机器去核（见 `_fixture_gaps`）。
    """

    # 纯函数 + IO，零副作用 —— 用真实现才测得出真行为
    _draft_for_display = staticmethod(Rongke._draft_for_display)
    _written_files = staticmethod(Rongke._written_files)
    _archive = staticmethod(Rongke._archive)

    # `_loop` / `make_workspace` 是真·实例方法，必须转调（见上表第二行）
    def _loop(self, *a, **kw):
        return Rongke._loop(self, *a, **kw)

    # 记账同样是实例方法。**保留真实现**是刻意的：它带的纪律
    # （任何异常都不许冒出去、余额可以是负数）本身值得被这条套件看着。
    def _charge_billing(self, result):
        return Rongke._charge_billing(self, result)

    def make_workspace(self, tag: str = "") -> Path:
        # 本测试**总是**显式传工作区，这条分支不该被走到；
        # 但夹具要保真 —— 万一哪天走到，行为得和真实现一致，不能静默建个陌生目录。
        return Rongke.make_workspace(self, tag)

    def __init__(self, max_steps=6, fail_times=0, boom_at=None, script=None,
                 usages=None, on_boom=None):
        self.cfg = SimpleNamespace(
            provider=SimpleNamespace(name="test", model="test-model"),
            pricing=None,
            # `_charge_billing` 要读它：缺这个字段会走 `skipped` 分支，
            # 于是"记账被跳过了"看起来像"记账成功" —— 夹具不能这么省。
            billing=SimpleNamespace(enabled=True),
            # `make_workspace` 要读它 —— 本测试走不到那条分支，但配置得齐
            runtime=SimpleNamespace(workspace_dir=ROOT / "runs"),
            loop=SimpleNamespace(max_steps=max_steps, max_observation_chars=4000,
                                 max_identical_calls=3, context_warn_chars=0))
        self.llm = ScriptLLM(script or [("交", [DELIVER()])], boom_at=boom_at,
                             usages=usages, on_boom=on_boom)
        self.gate = FakeGate(fail_times)
        self.registry = ToolRegistry()
        self.registry.register(Tool(
            name="probe", description="探针",
            parameters={"type": "object", "properties": {}},
            fn=lambda **kw: "探到了一些东西"))
        self.telemetry = None
        self.memory = None
        self.fence = None
        # 账单写进隔离目录：夹具转调真 `_charge_billing`，但它**不许**
        # 往用户真台账里记一笔测试消耗（那笔账事后看着像用户自己花的）。
        self.billing_root = ROOT / "runs" / "_regress" / "_billing_resume"
        self.mcp = SimpleNamespace(workspace=None, stop_all=lambda: None)


def newdir(name: str) -> Path:
    """建一个干净的任务目录。

    清场走 `fresh_dir`：**一次挪**，而不是 `rmtree` 那样按条目计费（见 `_tkit`）。
    """
    return T.fresh_dir(ROOT / "runs" / name, f"resume-{name}")


# ══════════════════ 0. 夹具自检：不漏协作对象 ══════════════════
# 夹具缺一个 `self.xxx`，报出来的 AttributeError 和被测代码的错**一模一样** ——
# 这个坑在本文件里烧了三次（`_draft_for_display` / `_archive` / `_loop`）。
# 所以不靠记性，靠机器：把真实源码里所有 `self.X` 的**读取**抠出来，
# 减掉源码里自己赋值的（那些运行时会自己长出来），剩下的夹具必须有。
print("── 0. 夹具自检：真实代码伸手要的协作对象，一个都不许缺 ──")


def _fixture_gaps(fixture, fns) -> list:
    """返回夹具缺失的协作对象名。

    只算**读取**：`self._snap_dir = ws` 这类赋值运行时会自己长出来，不该要求夹具预设。
    `self.registry.hooks = [...]` 这种"属性上的属性"算的是 `registry` —— 正好是我们要的。

    ⚠️ 这条判据的**边界**：它只保证"名字在"，**不保证 self 绑对了**。
    真·绑定错误（比如把实例方法当 staticmethod 包）它会放行 ——
    那类错由下面第二条断言点名，以及第 3 节的端到端跑兜底。
    判据说什么、不说什么，写清楚；含糊不清的判据比没有更危险。
    """
    reads, writes = set(), set()
    for s in [inspect.getsource(f) for f in fns]:
        reads |= set(re.findall(r"self\.([A-Za-z_]\w*)", s))
        writes |= set(re.findall(r"self\.([A-Za-z_]\w*)\s*=(?!=)", s))
    return sorted(n for n in (reads - writes) if not hasattr(fixture, n))


_gaps = _fixture_gaps(Kernel(), (Rongke._run_serial, Rongke._loop))
check("★ 夹具覆盖了真实代码要的全部协作对象（缺了当场报，别等跑到一半才炸）",
      _gaps, [])
# 上一条放行的盲区，这里点名补上：`_loop` 是实例方法，必须转调。
# 曾用 `staticmethod(Rongke._loop)` 包过 —— 不绑 self ⇒ `_run_serial` 传的
# `goal` 字符串被顶成 self，报 `'str' object has no attribute 'cfg'`。
check("★ `_loop` 必须是普通方法而不是 staticmethod（否则 self 会被 goal 顶掉）",
      isinstance(vars(Kernel).get("_loop"), staticmethod), False)


class _Bare:
    """故意空手 —— 验上面那条判据真的会响（否则它只是装饰）。"""


_gaps_neg = _fixture_gaps(_Bare(), (Rongke._run_serial, Rongke._loop))
check("★ 负控：给个空夹具，判据必须当场报缺一串", len(_gaps_neg) >= 5, True)
check("★ 负控点名到具体字段（只说「有问题」等于没说）",
      ("_loop" in _gaps_neg and "_archive" in _gaps_neg), True)


# ══════════════════ 1. 每步落盘：快照必须真的写出来、且是完整的 ══════════════════
print("\n── 1. 对话快照：每步原子落盘 ──")

_T1 = newdir("_resume_t1")
_k1 = Kernel(max_steps=4, script=[("第一步", [CALL("probe")]),
                                  ("第二步", [CALL("probe")])])
_k1._snap_dir = _T1                      # 直接调 _loop 时要自己给目录
_ev1: list = []
Rongke._loop(_k1, "快照测试", FakeContract(), 4,
             lambda k, d: _ev1.append((k, d)), None, None, None)

_snap = _T1 / "conversation.json"
check("★ 快照文件真的写出来了", _snap.is_file(), True)
_j1 = json.loads(_snap.read_text(encoding="utf-8")) if _snap.is_file() else {}
check("★ 快照里带着对话本身（不只记了个步数）", len(_j1.get("messages") or []) >= 5, True)
check("★ 快照记下了停在第几步", isinstance(_j1.get("step"), int) and _j1["step"] >= 1, True)
check("★ 快照带着契约（续跑要用它，不重新起草）",
      (_j1.get("contract") or {}).get("tag"), "原件")
check("★ 临时文件没有残留（原子写：要么是完整的，要么什么都没有）",
      (_T1 / "conversation.json.tmp").exists(), False)
check("★ 快照不含半个 JSON（能被解析出来就是完整的）", bool(_j1), True)

# 承重自检：把原子写短路掉（不 replace），快照必须消失
_SRC = Path(L.__file__).read_text(encoding="utf-8")


def load_patched(text: str):
    """以**真的包内模块名**加载打过补丁的源码（相对导入要包上下文）。

    ★ 影子副本落 `_SCRATCH`、**不落产品包**（`loop.py` 无 `__file__` 依赖，
      相对导入靠 `sys.modules["rongke"].__path__`、与副本位置无关）。
      理由见 `intervene_test.load_patched` —— 那里记着"产品包里堆了 27 份"的实测。
    """
    # 文件名每进程唯一（并发不互踩）；`finally` 收成固定名 ⇒ 不堆积。
    p = _SCRATCH / f"_probe_resume_{os.getpid()}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    name = "rongke._probe_resume"
    try:
        spec = importlib.util.spec_from_file_location(name, p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop(name, None)
        # 挪到固定名（覆盖），不删：产品包里不留"打过补丁的内核"，
        # 也不花删除额度。每进程唯一的名字照旧只用于**加载阶段**（防并发互踩）。
        os.replace(p, _SCRATCH / "_probe_resume.py")


_T1b = newdir("_resume_t1b")
_S1 = _SRC.replace("                os.replace(tmp, d / \"conversation.json\")",
                   "                pass   # 承重自检：不落盘")
check("补丁①锚点命中", _S1 != _SRC, True)
_M1 = load_patched(_S1)
_k1b = Kernel(max_steps=3, script=[("一", [CALL("probe")])])
_k1b._snap_dir = _T1b
_M1.Rongke._loop(_k1b, "快照测试", FakeContract(), 3, lambda k, d: None, None, None, None)
check("★ 短路落盘后快照果然不存在（证明这条在承重）",
      (_T1b / "conversation.json").exists(), False)


# ══════════════════ 2. 续跑：把前情原样接回来 ══════════════════
print("\n── 2. 续跑：模型第一轮必须看到**原来那份对话** ──")

_prev_msgs = _j1.get("messages") or []
_k2 = Kernel(max_steps=6, script=[("接着干", [CALL("probe")]), ("好，交付", [DELIVER()])])
_ev2: list = []
Rongke._loop(_k2, "快照测试", None, 6, lambda k, d: _ev2.append((k, d)), None, None,
             {"messages": _prev_msgs, "contract": _j1.get("contract"),
              "step": _j1.get("step")})

_first = _k2.llm.seen[0]
check("★ 第一轮收到的消息数 == 快照里的消息数（原样接回，没有重新装配）",
      len(_first), len(_prev_msgs))
check("★ 逐条内容一致（不是「看起来差不多」，是逐条相等）",
      [str(m.get("content") or "") for m in _first],
      [str(m.get("content") or "") for m in _prev_msgs])
check("★ 系统提示没有多出一份（重新装配会出现两条 system）",
      sum(1 for m in _first if m.get("role") == "system"),
      sum(1 for m in _prev_msgs if m.get("role") == "system"))
check("★ 事件流广播了 resumed，且带上「从第几步接回」（界面才说得出这是接回来的）",
      [d.get("from_step") for k, d in _ev2 if k == "resumed"],
      [int(_j1.get("step") or 0)])

# 契约：续跑**不重新起草**，用快照里的那份
# 标记必须放在**真契约认识**的字段上（acceptance），不能用自造字段：
# 首版我塞了个 `tag`，`TaskContract.from_dict` 直接把它丢了 ⇒ 断言拿到 None，
# 看起来像"代码没用快照的契约"，其实是我的标记没地方存（夹具的错）。
#
# ★ W36（2026-10-31）：这份快照契约要**带 `needs_writing=True`**。
#   因为收尾处现在按 needs_writing 分流：写稿才发 `contract` 事件，其余只发 `mode`。
#   这条测试验的是"续跑用快照那份契约"（靠 `contract` 事件的 acceptance 读出来），
#   所以必须让它落进**会发事件**的那一支 —— 否则读到的是 None，
#   看起来像"契约没带回来"，其实是"这一轮根本没发事件"（测试语义落错了支）。
_C_ORIG = TaskContract(raw_goal="测试续跑", acceptance=["标记：原件"], needs_writing=True)
_k3 = Kernel(max_steps=4, script=[("好，交付", [DELIVER()])])
_ev3: list = []
Rongke._loop(_k3, "快照测试", None, 4, lambda k, d: _ev3.append((k, d)), None, None,
             {"messages": _prev_msgs, "contract": _C_ORIG.to_dict(), "step": 5})
_ct = [d for k, d in _ev3 if k == "contract"]
check("★ 续跑用的是快照里那份契约（acceptance 标记原样带回来）",
      (_ct[0].get("acceptance") if _ct else None), ["标记：原件"])
check("★ 没有重新起草（不该出现 contract_drafting 事件）",
      [k for k, _ in _ev3 if k == "contract_drafting"], [])

# 承重自检：短路 resume 分支 ⇒ 第一轮变成"重新装配"的新对话
_S2 = _SRC.replace("        resumed = list((resume or {}).get(\"messages\") or [])",
                   "        resumed = []   # 承重自检：假装没有快照")
check("补丁②锚点命中", _S2 != _SRC, True)
_M2 = load_patched(_S2)
_k4 = Kernel(max_steps=6, script=[("接着干", [CALL("probe")])])
_M2.Rongke._loop(_k4, "快照测试", None, 6, lambda k, d: None, None, None,
                 {"messages": _prev_msgs, "contract": _j1.get("contract")})
_f4 = _k4.llm.seen[0]
check("★ 短路后续跑退化成重新装配（消息数变了 ⇒ 证明「接回前情」确实靠这段代码）",
      len(_f4) == len(_prev_msgs), False)


# ══════════════════ 2b. 会话内追问：follow_up 追加为一条 user 消息 ══════════════════
# 这是「连续会话」的内核半边：接回前情之后，把追问原话追加在栈尾 ——
# 断点就在消息栈里，所以是"接着说"，不是"另起炉灶"。
print("\n── 2b. 会话内追问：模型必须看到前情 + 追问 ──")

_k2b = Kernel(max_steps=6, script=[("改短一点", [CALL("probe")]), ("好，交付", [DELIVER()])])
_ev2b: list = []
Rongke._loop(_k2b, "把第二段改短", None, 6, lambda k, d: _ev2b.append((k, d)), None, None,
             {"messages": _prev_msgs, "contract": _j1.get("contract"), "step": 3,
              "follow_up": "把第二段改短"})
_first2b = _k2b.llm.seen[0]
check("★ 追问轮的消息数 == 快照消息数 + 1（追问追加在栈尾，不是重新装配）",
      len(_first2b), len(_prev_msgs) + 1)
check("★ 前情逐条原样在前（顺序没动、内容没改）",
      [str(m.get("content") or "") for m in _first2b[:len(_prev_msgs)]],
      [str(m.get("content") or "") for m in _prev_msgs])
check("★ 栈尾那条就是追问（user 角色 + 原话在内）",
      (_first2b[-1].get("role"),
       "把第二段改短" in str(_first2b[-1].get("content") or "")),
      ("user", True))
check("★ 事件流广播了 follow_up（界面才画得出这条气泡）",
      [d.get("chars") for k, d in _ev2b if k == "follow_up"],
      [len("把第二段改短")])
check("★ 追问轮没有重新起草契约（不该出现 contract_drafting —— 契约是这场会话的尺子）",
      [k for k, _ in _ev2b if k == "contract_drafting"], [])
# 空白追问 = 没有追问：普通续跑，一个字不多、一个事件不多。
_k2c = Kernel(max_steps=6, script=[("接着干", [CALL("probe")])])
_ev2c: list = []
Rongke._loop(_k2c, "快照测试", None, 6, lambda k, d: _ev2c.append((k, d)), None, None,
             {"messages": _prev_msgs, "contract": _j1.get("contract"), "follow_up": "   "})
check("★ 空白追问等于没有追问（消息数不变）",
      len(_k2c.llm.seen[0]), len(_prev_msgs))
check("★ 空白追问不广播 follow_up", [1 for k, _ in _ev2c if k == "follow_up"], [])


# ══════════════════ 3. `_run_serial`：成功删快照、崩溃也归档 ══════════════════
print("\n── 3. 收尾：成功清快照 / 崩溃留归档 ──")

_T3 = newdir("_resume_t3")
_k5 = Kernel(max_steps=4, script=[("好，交付", [DELIVER()])])
_r5 = Rongke._run_serial(_k5, "收尾测试", FakeContract(), 4, lambda k, d: None, _T3)
check("前提复核：这次确实跑成功了", bool(_r5 and _r5.success), True)
check("★ 跑成功的任务不留「可续跑的靶子」（规范名必须消失）",
      (_T3 / "conversation.json").exists(), False)
check("★ 但快照是**改名**不是**删掉**（内容必须还在，改名不计费、删除计费）",
      (_T3 / "conversation.done.json").is_file(), True)
_j5 = (json.loads((_T3 / "conversation.done.json").read_text(encoding="utf-8"))
       if (_T3 / "conversation.done.json").is_file() else {})
check("★ 改名后的快照**内容完整**（不是截断/半截 JSON）",
      bool(_j5) and len(_j5.get("messages") or []) >= 1, True)
check("★ 改名前后名字都算「内部件」（界面白名单是算出来的，不是手抄的）",
      sorted(L.SNAPSHOT_INTERNAL),
      ["conversation.done.json", "conversation.done.json.tmp",
       "conversation.json", "conversation.json.tmp"])
check("★ 但归档必须在（成功路径的归档不能被这个改动碰坏）",
      (_T3 / "session.json").is_file(), True)

_T4 = newdir("_resume_t4")
_k6 = Kernel(max_steps=6, boom_at=1, script=[("一", [CALL("probe")]),
                                             ("二", [CALL("probe")])])
_ev6: list = []
_r6 = Rongke._run_serial(_k6, "崩溃测试", FakeContract(), 6,
                         lambda k, d: _ev6.append((k, d)), _T4)
check("★ 崩溃不再往上抛（翻成如实的 RunResult，收尾逻辑才走得完）",
      _r6 is not None and _r6.success, False)
check("★ 崩溃的原因写清楚了，且说明「可以接着跑」",
      ("执行中断" in _r6.reason and "接着跑" in _r6.reason), True)
check("★ 崩溃也**归档**了（这是「7 个中断任务没有归档」那个缺口的正面）",
      (_T4 / "session.json").is_file(), True)
_j6 = json.loads((_T4 / "session.json").read_text(encoding="utf-8"))
check("★ 归档里带着崩溃的步数（事后查得出它跑到哪）", int(_j6["result"]["steps"]) >= 1, True)
check("★ 崩溃时**保留**快照（那正是用户要接着跑的场合）",
      (_T4 / "conversation.json").is_file(), True)
check("★ 事件流里有 crash（界面能显示 traceback 而不是干瞪眼）",
      [1 for k, _ in _ev6 if k == "crash"], [1])


# ── 3b. 崩溃必须把已烧掉的用量带走（2026-09-26 修）──────────────
# 病：兜崩溃那段写死 `usage=Usage()`，而累加器是 `_loop` 的局部、外层取不到
#     ⇒ 这条路径**必然**报 0。实测一例：真烧 51,241、报 0。
# 为什么 0 比报错更坏：`pricing.py` 早就定了这条 —— 0 是一个**断言**（"这场没花钱"），
#     而事实只是"在途那次的 usage 网关没来得及回"。把未知渲染成已知 = F 型缺陷（假安慰）。
# 治法：`_loop` 每累加一次就把 `self._run_usage` 重新指向同一个对象（**别名**，
#     不是第二个累加器），崩溃时按现场带走，并用 `usage_complete=False` 声明
#     "这是下界，不是总额"。
print("\n── 3b. 崩溃必须把已烧掉的用量带走 ──")

_US = [Usage(1000, 50, 10, 400, 0), Usage(2000, 80, 20, 1500, 0),
       Usage(300, 5, 1, 0, 0)]
_SC = [("一", [CALL("probe")]), ("二", [CALL("probe")]), ("三", [CALL("probe")])]

_T5 = newdir("_resume_t5")
_k7 = Kernel(max_steps=6, boom_at=3, script=_SC, usages=_US)
_ev7: list = []
_r7 = Rongke._run_serial(_k7, "崩溃带用量", FakeContract(), 6,
                         lambda k, d: _ev7.append((k, d)), _T5)
check("前提复核：前三次都拿到了 usage、第 4 次才崩",
      (bool(_r7), _r7.success, len(_k7.llm.seen)), (True, False, 4))
check("★ 前三次已烧掉的必须带走，一个都不许记成 0",
      _r7.usage.input_tokens, 1000 + 2000 + 300)
check("★ 输出与推理 token 同样带走",
      (_r7.usage.output_tokens, _r7.usage.reasoning_tokens), (135, 31))
check("★ 与事件流同源：result.usage == Σ llm_done（两处真相必须算出同一个数）",
      sum(d["usage"]["input_tokens"] for k, d in _ev7 if k == "llm_done"),
      _r7.usage.input_tokens)
check("★ 但它**不是全量**：在途那次没算进来", _r7.usage_complete, False)
_j7 = json.loads((_T5 / "session.json").read_text(encoding="utf-8"))
check("★ 归档里数字与声明**同时在**（只存数字就会被下游读成总账）",
      (_j7["result"]["usage"]["input_tokens"], _j7["result"]["usage_complete"]),
      (3300, False))

# 负控一：一次都没跑完的崩溃 —— 数字仍是 0，但必须带声明，不许裸 0 出门
check("★ 第一次调用就崩：无累计可带 ⇒ 0 配 usage_complete=False（不是「没花钱」）",
      (_r6.usage.input_tokens, _r6.usage_complete), (0, False))
check("★ 这条声明一路进归档（`--show` 与历史页读的是同一份）",
      _j6["result"].get("usage_complete"), False)
# 负控二：成功路径不许被这次修改牵连
check("★ 跑完了的场次恒为 True（没给任何已知数新增降级）",
      _r5.usage_complete, True)

# 承重自检：抹掉别名 = 改回修之前的世界（外层取不到现场）⇒ 上面那条正控当场失守
_T6 = newdir("_resume_t6")
_k7b = Kernel(max_steps=6, boom_at=3, script=_SC, usages=_US)
_k7b.llm.on_boom = lambda: setattr(_k7b, "_run_usage", None)
_r7b = Rongke._run_serial(_k7b, "替身：读不到现场", FakeContract(), 6,
                          lambda k, d: None, _T6)
check("★ 短路别名 ⇒ 累计退化成 0（证明那个数确实是被带走的，不是别处算的）",
      (_r7.usage.input_tokens, _r7b.usage.input_tokens), (3300, 0))
check("★ 替身跑完仍如实报 False（声明不依赖于取不取得到数）",
      _r7b.usage_complete, False)


# ══════════════════ 4. Manager.resume 的围栏 ══════════════════
print("\n── 4. 续跑入口的围栏：能回放 ≠ 能续跑 ──")

sys.path.insert(0, str(ROOT))
import rongke.web.server as S                                 # noqa: E402


def fake_manager():
    return SimpleNamespace(_lock=threading.Lock(), active=None, runs={},
                           kernel=object(), kernel_error="",
                           _exec=lambda run: None)


_m = fake_manager()
_r, _e = S.Manager.resume(_m, "_resume_t3")        # 有归档、无快照（刚跑成功）
check("★ 有归档但没有快照 ⇒ 如实拒绝，并说明原因",
      (_r is None, "没有可续跑的对话快照" in _e), (True, True))
check("★ 拒绝时**没有**偷偷开一个任务（骗人比拒绝更糟）", _m.runs, {})

_r2, _e2 = S.Manager.resume(_m, "_resume_t4")      # 崩溃过、快照在
check("★ 有快照 ⇒ 接得上", (_r2 is not None, _e2), (True, ""))
check("★ 续跑回到**原工作区**（否则那半截和新跑的分家，历史里会多出一个陌生任务）",
      Path(_r2.workspace).name if _r2 else None, "_resume_t4")
check("★ 对话确实被带上了（不是只带了个 id）",
      len(((_r2.resume or {}).get("messages")) or []) >= 1, True)

import shutil                                                  # noqa: E402


def manager_reject(rid):
    return S.Manager.resume(fake_manager(), rid)


for bad, why in (("../../etc", "路径穿越"), ("", "空 id"),
                 ("_resume_does_not_exist", "目录不存在")):
    r, e = manager_reject(bad)
    check(f"负控：{why} ⇒ 拒绝且不抛异常", (r is None, bool(e)), (True, True))

_T5 = newdir("_resume_t5")
(_T5 / "conversation.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
_r7, _e7 = manager_reject("_resume_t5")
check("★ 快照坏了 ⇒ 明确报「读不出来」，而不是抛异常或静默从头跑",
      (_r7 is None, "读不出来" in _e7), (True, True))

_T6 = newdir("_resume_t6")
(_T6 / "conversation.json").write_text('{"goal":"x","messages":[]}', encoding="utf-8")
_r8, _e8 = manager_reject("_resume_t6")
check("★ 快照里没有对话 ⇒ 拒绝（接着跑一个空对话没有意义）",
      (_r8 is None, "没有对话内容" in _e8), (True, True))


# ══════════════════ 5. 负控：没有快照时不许凭空续跑 ══════════════════
print("\n── 5. 负控：不传 resume 时，行为与从前一字不差 ──")

_k9 = Kernel(max_steps=4, script=[("好，交付", [DELIVER()])])
_ev9: list = []
_r9 = Rongke._loop(_k9, "普通跑", FakeContract(), 4,
                   lambda k, d: _ev9.append((k, d)), None, None, None)
check("★ 不传 resume ⇒ 走原装配路径，结果照旧成功", bool(_r9.success), True)
check("★ 且不发 resumed 事件（CLI 的屏幕不该多出「续跑」的噪音）",
      [k for k, _ in _ev9 if k == "resumed"], [])
check("★ 系统提示仍然只有一条（没有多装配）",
      sum(1 for m in _k9.llm.seen[0] if m.get("role") == "system"), 1)

print("\n── 6. 首条 user 消息里的**工作区清单**（真接线，不是助手函数的单元测试）──")
# 第 5 节那个 `_k9` 跑的是**真的** `Rongke._loop`，`ScriptLLM.seen[0]` 就是
# 模型开跑时真正收到的消息。这里用它验「清单有没有真的注进去」——
# 单元测试能证明清单函数写对了，但**证明不了 `_loop` 用了它**；
# 而"函数写好了却没人调"正是本项目最贵的 G 型缺陷。
_fm = list(_k9.llm.seen[0]) if getattr(_k9, "llm", None) and _k9.llm.seen else []
_u0 = next((m.get("content") or "" for m in _fm if m.get("role") == "user"), "")
_s0 = next((m.get("content") or "" for m in _fm if m.get("role") == "system"), "")
check("6.1 前置：抓到了模型开跑时收到的第一条消息", len(_fm) >= 2, True)
check("★ 6.2 首条 user 消息里带着**工作区清单**（2026-09-21 修：治 40 步猜路径空转）",
      "本任务工作区（你的 cwd，也是围栏边界）" in _u0, True)
check("★ 6.3 清单里的路径是**当时量出来的** cwd（不是写死的、也不是模板）",
      str(Path.cwd()) in _u0, True)
check("6.4 清单与契约同处一条 user 消息（模型一次读全，不必来回找）",
      ("目标：测试续跑" in _u0) and ("现有条目" in _u0), True)
check("6.5 负控：system 里**不**塞清单（system 讲纪律，user 讲事实，职责不混）",
      "本任务工作区（你的 cwd" in _s0, False)

# ── 6.6 家族：清单与磁盘**一致**，且"没显示"≠"不存在" ──
# ★ 2026-09-22 改写。原来这一条是：
#     `check(..., ("regress.py" in _u0) if (cwd/"regress.py").exists() else True, True)`
#   它拿"字母序靠后的某个具体文件"当锚 —— 而清单**有条数上限**
#   （`loop.py`：`entries[:limit]`，按 `(不是目录, 名字)` 排序），
#   仓库根一多两个文件（那天新加了 `kb_e2e_test.py` / `kb_wire_test.py`），
#   `regress.py` 就被挤出了可见范围 ⇒ 这条判据**当场变红**。
#   红得还很有欺骗性：看起来像"清单与磁盘不一致"，其实只是**根目录文件数涨了**。
#   ⇒ 判据不该锚在"某个恰好能挤进来的文件名"上，该锚在**规则**上：
#     · 列出来的都得真实存在（清单是量出来的）
#     · **截断时必须如实说截断了**（这才是这条真正要守的东西：
#       「没显示」与「不存在」是两件事 —— M 型缺陷的老家）
#   改完比原来**更强**：原来只看"某个名字在不在"，现在连"截断说明该不该出现"都两个方向量。
_lines = _u0.splitlines()
_i0 = next((i for i, l in enumerate(_lines) if l.startswith("现有条目（")), None)
_listed: list[str] = []
if _i0 is not None:
    for _l in _lines[_i0 + 1:]:
        if _l.startswith("⚠️") or not _l.strip():
            break                      # 清单区块到此为止（后面是纪律段落）
        if _l.strip().startswith("……"):
            continue                   # 截断说明那一行，不是条目
        # 名字与大小之间是**多个空格**（`  name        123 B`）⇒ 按 2+ 空格切，
        # 而不是按空白切 —— 按空白切会把"名字里带空格"的文件名切碎，
        # 于是 `.exists()` 假失败，看起来像"清单与磁盘不一致"（其实是解析器的问题）。
        _listed.append(re.split(r"\s{2,}", _l.strip())[0].rstrip("/"))
_m = re.search(r"现有条目（(\d+) 项）", _u0)
_declared = int(_m.group(1)) if _m else 0
_listed_ok = bool(_listed) and all(
    (Path.cwd() / _n).exists() for _n in _listed)
check("6.6 正控：清单里的每一条都在磁盘上真实存在（清单是量出来的）",
      _listed_ok, True)
check("6.6b 清单解析出了条目（不是空清单冒充一致）", len(_listed) > 0, True)
check("6.6c 声明的总数 ≥ 实际列出的条数", _declared >= len(_listed), True)
check("★★ 6.6d 截断与说明**同进同出**（该说时说、不该说时不说）",
      ("没列出来" in _u0), (_declared > len(_listed)))
check("6.7 负控：磁盘上不存在的名字**不会**出现在清单里（清单是量出来的，不是编的）",
      "绝对不存在的文件.md" in _u0, False)

print("\n── 7. `_unpack`：模型给坏 JSON 时，诊断必须**说得出原文** ──")
# 2026-09-21 实测（runs/20260921-113729）：某次 `import_document` 的 arguments
# 流到 `"kind": "pdf</` 就断了（未闭合）⇒ 原实现 `except Exception: args = {}`
# 把**解析失败**说成了**没传参数**（M 型：口径塌缩）。模型明明传了 file，
# 收到的却是「缺少必填参数：file / 你传的是：（一个都没传）」——自相矛盾，无从下手。
# 这一节钉住"诊断里有原文"：**断口处那段尾巴才是模型唯一能据以自纠的证据**。
_bad = '{"file": "runs/x/素材.md", "kind": "pdf</'
_n, _a, _i, _e = L._unpack({"id": "c1", "function": {"name": "import_document",
                                                    "arguments": _bad}})
check("7.1 名字仍被解出来（坏参数不影响识别是哪个工具）", _n, "import_document")
check("7.2 args 退化成空 dict", _a, {})
check("7.3 ★ 带出了非空的解析诊断（不再静默吞掉）", bool(_e), True)
check("7.4 ★ 诊断里点名异常类型", "JSONDecodeError" in _e, True)
check("7.5 ★★ 诊断里带着**原文长度**", f"共 {len(_bad)} 字符" in _e, True)
check("7.6 ★★ 诊断里带着**原文尾部**（断口处才可自纠）", "pdf</" in _e, True)
# 负控三板斧：新判据最容易犯的错就是过度触发。
check("7.7 负控：合法 JSON ⇒ args 正常、诊断为空",
      L._unpack({"function": {"name": "t", "arguments": '{"file": "a.md"}'}})[1:4:2],
      ({"file": "a.md"}, ""))
check("7.8 负控：真的没传 ⇒ 不算解析失败（口径留给必填校验去说）",
      L._unpack({"function": {"name": "t"}})[1:4:2], ({}, ""))
check("7.9 负控：纯空白也算没传（不是解析失败）",
      L._unpack({"function": {"name": "t", "arguments": "   "}})[3], "")
# 两个正控：**只有 dict 才是参数** —— 合法但非对象的 JSON 同样是坏的。
check("7.10 合法 JSON 但不是对象（数组）⇒ 也算解析失败",
      bool(L._unpack({"function": {"name": "t", "arguments": "[1,2]"}})[3]), True)
check("7.11 arguments 直接就是 dict（有些 provider 这么给）⇒ 原样收下",
      L._unpack({"function": {"name": "t", "arguments": {"a": 1}}})[1], {"a": 1})

for d in ("_resume_t1", "_resume_t1b", "_resume_t3", "_resume_t4", "_resume_t5", "_resume_t6"):
    T.stash(ROOT / "runs" / d, f"resume-{d}")
check("夹具已挪走", (ROOT / "runs" / "_resume_t4").exists(), False)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
