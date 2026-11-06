# -*- coding: utf-8 -*-
"""运行中问人（loop.py 的三个介入哨位）单测 —— 假模型 + 假门 + 真工具注册表。

## 为什么单开这一套

评审第①条是「门打回或检测到打转时**主动暂停问人**，支持补一句、改一条约束、
**从断点续跑**，消灭整轮重来」。它给内核加了一条**新的对外契约**（`run(intervene=…)`），
而这条契约的失效方式**全是静默的**：

  * 呼叫了介入、但答复没被采纳 → 用户以为自己的话生效了（比不问更糟，M 型）
  * 该问的时候不问 → 又回到「闷头跑到死」
  * **不该问的时候乱问** → 噪音，训练用户无视提示（误报比漏报更伤）
  * **没人应答时把任务卡死或判失败** → 把可用性问题升级成可用性事故

## 测试策略：三条正交的「假」

  1. **假模型** `ScriptLLM` —— 按脚本逐轮返回，并**留档每一轮收到的 messages**。
     留档是关键：要证明「补的那句话真的进了消息栈」，只能去看模型下一轮**看到了什么**，
     而不是去看内核打算做什么（那是自证，R 型）。
  2. **假门** `FakeGate` —— 可控地「前 N 次打回」。第 1 次不问、第 2 次才问是业务规则，
     必须能精确触发第 N 次。
  3. **真工具注册表** —— `ToolRegistry` 本来就不是测试替身，直接用真的；
     打转检测（`repeated_call`）挂在它身上，假掉就等于把被测对象替换掉了。

一条刻意的记法：**每条核心断言都配一个「短路自己」的实验**（承重自检）。
做法是拿真实源码做字符串替换、以**真的包内模块**加载（见 `load_patched`），
把那个判据改坏，然后要求正控**当场失守**。没失守 ⇒ 那条判据其实没在干活（G 型）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                            # noqa: E402
_SCRATCH = ROOT / "runs" / "_regress"                        # 影子副本落点（挪去，不删）

from rongke import loop as L                                  # noqa: E402
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


# ══════════════════ 夹具 ══════════════════
def CALL(name: str, **args) -> tuple[str, dict]:
    return (name, args)


def DELIVER(summary="做完了") -> tuple[str, dict]:
    return CALL("deliver_task", summary=summary, deliverables=["a.md"],
                evidence=[{"call_seq": 1, "claim": "有证据"}], draft_text="正文一段。")


class ScriptLLM:
    """按脚本逐轮返回；脚本用尽后一律返回「通过交付」，好让循环收敛。

    `seen[i]` 是第 i 轮**实际收到**的消息列表 —— 想证明「用户补充进了上下文」，
    只能读它（模型真正看到的东西），不能读内核的自述。
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools=None, meta=None):
        self.seen.append([dict(m) for m in messages])
        i = len(self.seen) - 1
        content, calls = self.script[i] if i < len(self.script) else ("", [DELIVER()])
        msg: dict = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = [
                {"id": f"c{i}_{j}", "type": "function",
                 "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
                for j, (n, a) in enumerate(calls)]
        return msg, Usage(), ""


class FakeGate:
    """前 `fail_times` 次打回，之后通过。打回理由里写明是第几次。"""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.checks = 0

    def reset(self):
        self.checks = 0

    def check(self, claim, contract):
        self.checks += 1
        if self.checks <= self.fail_times:
            return Verdict(passed=False,
                           reasons=[f"第 {self.checks} 次打回：引注数 0 < 3"],
                           feedback="打回：证据不足，请补。")
        return Verdict(passed=True, reasons=[], feedback="通过。")


class FakeContract:
    # ★ W36：收尾处按 `needs_writing` 决定发不发任务单（写稿才发）。
    #   真契约总有这个字段 —— 夹具补上，否则这条分流在这里永远测不到。
    needs_writing = False

    def to_prompt(self):
        return "目标：测试介入机制"

    def to_dict(self):
        return {"raw_goal": "测试介入机制"}


class FakeKernel:
    """只装备 `_loop` 真正用到的那几样。"""

    # 用真的实现（零副作用），但**必须包 staticmethod**：
    # 直接赋函数对象的话，通过实例取属性会变成绑定方法 ⇒ self 占掉 claim 的位置，
    # 于是 `claim.draft_text` 变成 `FakeKernel.draft_text` → AttributeError。
    # 夹具自己出的错会和被测代码的错长得一模一样，最难查。
    _draft_for_display = staticmethod(Rongke._draft_for_display)
    _written_files = staticmethod(Rongke._written_files)

    def __init__(self, max_steps=6, fail_times=0, max_identical=3):
        self.cfg = SimpleNamespace(loop=SimpleNamespace(
            max_steps=max_steps, max_observation_chars=4000,
            max_identical_calls=max_identical, context_warn_chars=0))
        self.llm = None            # 由 run_loop 填
        self.gate = FakeGate(fail_times)
        self.registry = ToolRegistry()
        self.registry.register(Tool(
            name="probe", description="探针",
            parameters={"type": "object", "properties": {}},
            fn=lambda **kw: "探到了一些东西"))
        self.telemetry = None
        self.memory = None


def run_loop(script, intervene=None, max_steps=6, fail_times=0, max_identical=3):
    """跑一遍 `_loop`，返回 (结果, 内核, 事件列表)。"""
    k = FakeKernel(max_steps=max_steps, fail_times=fail_times,
                   max_identical=max_identical)
    k.llm = ScriptLLM(script)
    events: list[tuple[str, dict]] = []
    res = Rongke._loop(k, "测试目标", FakeContract(), max_steps,
                       lambda kind, data: events.append((kind, data)),
                       None, intervene)
    return res, k, events


def kinds(events):
    return [k for k, _ in events]


def asks(events, kind=None):
    out = [d for k, d in events if k == "intervene_request"]
    return [d for d in out if kind is None or d.get("kind") == kind]


# ══════════════════ 1. 门打回：第 1 次不问，第 2 次问 ══════════════════
print("── 1. 交付被打回：第 1 次不吵人，第 2 次才停下问 ──")

seen_calls: list[tuple[str, dict]] = []


def iv_record(kind, payload):
    seen_calls.append((kind, payload))
    return {"action": "continue"}


# 连交 3 次：第 1、2 次被打回，第 3 次通过
_res, _k, _ev = run_loop(
    [("我先交一版", [DELIVER()]), ("我再交一版", [DELIVER()]), ("再交", [DELIVER()])],
    intervene=iv_record, fail_times=2)

check("打回 2 次 ⇒ 介入只被叫了 1 次（第 1 次留给模型自己修）",
      len(seen_calls), 1)
check("★ 介入的 kind 是 gate_reject",
      [c[0] for c in seen_calls], ["gate_reject"])
check("★ 介入载荷带上了打回次数与理由（界面要能显示「为什么停」）",
      ("rejects" in seen_calls[0][1] and "reasons" in seen_calls[0][1]), True)
check("事件流里也广播了 intervene_request（界面才有卡可渲染）",
      len(asks(_ev, "gate_reject")), 1)

# 负控：只打回 1 次 ⇒ 一次都不许问
_calls2: list = []
run_loop([("交", [DELIVER()]), ("再交", [DELIVER()])],
         intervene=lambda k, d: (_calls2.append(k), {"action": "continue"})[1],
         fail_times=1)
check("负控：只被打回 1 次时不问人（否则就是「它还没试过就先怀疑它」）",
      _calls2, [])


# ══════════════════ 2. steer：补的那句话必须进上下文 ══════════════════
print("\n── 2. 补一句：必须**进到模型下一轮真正看到的**消息里 ──")

_ADD = "不要再用 Crossref 了，换 OpenAlex；顺带把目标字数降到 200。"


def iv_steer(kind, payload):
    return {"action": "steer", "text": _ADD}


_res2, _k2, _ev2 = run_loop(
    [("交一版", [DELIVER()]), ("交二版", [DELIVER()]), ("交三版", [DELIVER()])],
    intervene=iv_steer, fail_times=2)

_last = _k2.llm.seen[-1]
_joined = "\n".join(str(m.get("content") or "") for m in _last)
check("★ 用户补充出现在**最后一轮模型输入**里（这才是「从断点续跑」的物证）",
      _ADD in _joined, True)
check("★ 它就是一条 user 消息（位置对，模型才会当指令读）",
      any(m.get("role") == "user" and _ADD in str(m.get("content") or "")
          for m in _last), True)
check("带了「就地继续、不必重来」的限定语（否则模型可能推倒重做）",
      "不必重来" in _joined, True)
check("事件流广播了 intervene_applied，且写了字数",
      [d.get("chars") for k, d in _ev2 if k == "intervene_applied"], [len(_ADD)])
check("任务没被打断：仍然跑到了通过", bool(_res2.success), True)

# 承重自检：把 _steer 的 append 短路掉，上面那条正控必须当场失守
_SRC = Path(L.__file__).read_text(encoding="utf-8")


def load_patched(text: str):
    """以**真的包内模块名**加载打过补丁的源码（相对导入要包上下文）。

    ★ 影子副本落 `_SCRATCH`，**不落产品包**。为什么可以：`loop.py` 里没有任何
      `__file__` / `__path__` / ROOT 依赖（只用 `os.getcwd()`），而相对导入靠的是
      `sys.modules["rongke"]` 的 `__path__`，**与副本自己的位置无关** ⇒ 放哪儿都能
      被 import 成 `rongke._probe_loop`。
      （`web_test` 那份**不能**照搬：`server.py` 里有
      `HERE = Path(__file__).resolve().parent` 去找 `index.html`，所以它必须留在
      `rongke/web/` —— 位置本身就是它的语义。）
    ★ 为什么非改不可：副本写进产品包后，进程被**杀掉**（额度耗尽就是这么死的：
      rc≠0、无 traceback）时 `finally` 不跑 ⇒ 产品包里留下一份"含故意缺陷、
      可被 import 的 loop.py"。实测堆了 **27 个**（62KB × 27 = 1.7MB），
      而且它们会被指纹与语法闸算进去、让后来的人在 grep 里撞上一堆**假源码**。
    """
    # 文件名每进程唯一（并发时不互相踩）；`finally` 里再收成固定名 ⇒ 不堆积。
    p = _SCRATCH / f"_probe_loop_{os.getpid()}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    name = "rongke._probe_loop"
    try:
        spec = importlib.util.spec_from_file_location(name, p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.modules.pop(name, None)
        # 挪到固定名（覆盖），不删：产品包里不留"打过补丁的内核"，也不花删除额度。
        # 每进程唯一的名字照旧只用于**加载阶段** —— 那句注释说的是"不能互删"，而
        # `os.replace` 落在 `finally` 里（加载早已结束），并发也安全。
        os.replace(p, _SCRATCH / "_probe_loop.py")


_STEER_OLD = ('messages.append({"role": "user", "content":\n'
              '                             f"【运行中补充（因{why}暂停问你）—— 就地按它继续，不必重来】\\n{t}"})')
_STEER_NEW = "_ = t   # 承重自检：故意不插"
_s1 = _SRC.replace(_STEER_OLD, _STEER_NEW)
check("补丁锚点命中（没命中就必须当场报错，绝不能静默测错代码）",
      _s1 != _SRC, True)
_M = load_patched(_s1)
_k3 = FakeKernel(max_steps=6, fail_times=2)
_k3.llm = ScriptLLM([("a", [DELIVER()]), ("b", [DELIVER()]), ("c", [DELIVER()])])
_M.Rongke._loop(_k3, "t", FakeContract(), 6, lambda k, d: None, None, iv_steer)
_j3 = "\n".join(str(m.get("content") or "") for m in _k3.llm.seen[-1])
check("★ 短路 append 后补充果然**没**进上下文（证明这条断言在承重）",
      _ADD in _j3, False)
check("★ 对照组：未短路时同一输入是进的", _ADD in _joined, True)


# ══════════════════ 3. 步数耗尽：extend 必须真的延长 ══════════════════
print("\n── 3. 步数耗尽：说「再加 20 步」就得真能多跑 ──")

# 上限 2 步；头两步只调工具，第 3 步才交付。若 extend 不生效，第 3 步永远轮不到。
_SCRIPT3 = [("探一下", [CALL("probe")]), ("再探一下", [CALL("probe")]),
            ("好，交付", [DELIVER("补完就跑到了")])]

_res3, _k3b, _ev3 = run_loop(
    _SCRIPT3, intervene=lambda k, d: {"action": "extend", "steps": 20},
    max_steps=2)

check("★ 步数耗尽时确实问了人（kind=step_limit）",
      [a.get("kind") for a in asks(_ev3)], ["step_limit"])
check("★ 用户选「加步数」后**真的多跑了**：第 3 步的交付被执行到（steps=3 > 原上限 2）",
      (_res3.success, _res3.steps), (True, 3))
_app = [d for k, d in _ev3 if k == "intervene_applied"]
check("广播了 max_steps 的新值（界面要能如实显示「已放宽到 N 步」）",
      (_app[0]["max_steps"], _app[0]["action"]), (22, "extend"))

# 承重自检：短路 `max_steps += add`
_E1 = _SRC.replace("max_steps += add", "pass   # 承重自检：故意不加")
check("补丁②锚点命中", _E1 != _SRC, True)
_M2 = load_patched(_E1)
_k4 = FakeKernel(max_steps=2, fail_times=0)
_k4.llm = ScriptLLM(_SCRIPT3)
_r4 = _M2.Rongke._loop(_k4, "t", FakeContract(), 2, lambda k, d: None, None,
                       lambda k, d: {"action": "extend", "steps": 20})
check("★ 短路后第 3 步果然轮不到（证明 extend 分支在承重）",
      (_r4.success, _r4.steps), (False, 2))

# 负控：步数耗尽时说「停止」，必须如实结束而不是继续跑
_res3c, _k3c, _ev3c = run_loop(
    _SCRIPT3, intervene=lambda k, d: {"action": "stop"}, max_steps=2)
check("负控：步数耗尽时选「停止」⇒ 如实结束，理由写明是用户叫停",
      (_res3c.success, "用户叫停" in _res3c.reason), (False, True))
check("负控：停止后不再广播 intervene_applied（没收下任何指令）",
      [d for k, d in _ev3c if k == "intervene_applied"], [])


# ══════════════════ 4. stop：门打回时叫停 ══════════════════
print("\n── 4. 接受「叫停」：不是失败，是未完成 ──")

_res4, _k5, _ev4 = run_loop(
    [("一", [DELIVER()]), ("二", [DELIVER()]), ("三", [DELIVER()])],
    intervene=lambda k, d: {"action": "stop"}, fail_times=2)
check("★ 叫停后立刻收工（不会再多跑一轮）", _res4.steps, 2)
check("★ 结果如实标为未完成，且理由点明是用户叫停",
      (_res4.success, "用户叫停" in _res4.reason), (False, True))
check("措辞里说明「不是失败」（否则用户会以为是自己把它搞挂了）",
      "不是失败" in _res4.reason, True)


# ══════════════════ 5. 降级：没人应答绝不许改变结果 ══════════════════
print("\n── 5. 降级路径：没人应答 / 通道坏 / 没有通道 —— 三种都不许改变行为 ──")

_SCRIPT5 = [("一", [DELIVER()]), ("二", [DELIVER()]), ("三", [DELIVER()])]

# ① 超时返回 None
_r5a, _k5a, _ev5a = run_loop(_SCRIPT5, intervene=lambda k, d: None, fail_times=2)
check("★ 回调返回 None（超时）⇒ 任务照常跑到通过，不卡死也不判失败",
      _r5a.success, True)
check("★ 且如实广播「没有人应答，按原路继续」（不许假装收到答复）",
      [(d.get("outcome"), d.get("text")) for k, d in _ev5a if k == "intervene_note"],
      [("timeout", "没有人应答，按原路继续")])


def _boom(kind, payload):
    raise RuntimeError("通道炸了")


_r5b, _k5b, _ev5b = run_loop(_SCRIPT5, intervene=_boom, fail_times=2)
check("★ 回调抛异常 ⇒ 同样降级为原行为，任务仍然跑通",
      _r5b.success, True)
check("★ 且把异常如实记进事件流（不许吞掉）",
      [d.get("outcome") for k, d in _ev5b if k == "intervene_note"], ["error"])

# ③ 根本不传 intervene（CLI 走这条路）
_r5c, _k5c, _ev5c = run_loop(_SCRIPT5, intervene=None, fail_times=2)
check("★ 不传 intervene ⇒ 结果与「传了个返回 None 的回调」完全一致",
      (_r5c.success, _r5c.steps), (_r5a.success, _r5a.steps))
check("★ 且一个 intervene_* 事件都不发（CLI 的屏幕不该多出无人应答的噪音）",
      [k for k in kinds(_ev5c) if k.startswith("intervene")], [])


# ══════════════════ 6. 打转：同一个坑只问一次 ══════════════════
print("\n── 6. 原地打转：问一次就够，问多了就是烦人 ──")

_stall_hits: list = []
_SCRIPT6 = [("探", [CALL("probe")]), ("再探", [CALL("probe")]),
            ("还探", [CALL("probe")]), ("继续探", [CALL("probe")]),
            ("探到死", [CALL("probe")]), ("好，交付", [DELIVER()])]
_r6, _k6, _ev6 = run_loop(
    _SCRIPT6, intervene=lambda k, d: (_stall_hits.append(d), {"action": "continue"})[1],
    max_steps=8, fail_times=0, max_identical=2)

check("确实检测到了打转并广播 stall 事件",
      len([k for k in kinds(_ev6) if k == "stall"]) >= 1, True)
check("★ 同一个打转点**只问一次**（后续重复调用不再骚扰用户）",
      len(_stall_hits), 1)
check("★ 问的是 stall 类型", [d.get("kind") for d in _stall_hits], ["stall"])
check("★ 载荷带上了「卡在哪个动作」（界面才说得出一句人话）",
      [d.get("tool") for d in _stall_hits], ["probe"])

# 承重自检：去掉「只问一次」的守卫
_S1 = _SRC.replace("if call.fingerprint not in asked_stalls:",
                   "if True:   # 承重自检：每次都问")
check("补丁③锚点命中", _S1 != _SRC, True)
_M3 = load_patched(_S1)
_hits3: list = []
_k7 = FakeKernel(max_steps=8, fail_times=0, max_identical=2)
_k7.llm = ScriptLLM(_SCRIPT6)
_M3.Rongke._loop(_k7, "t", FakeContract(), 8, lambda k, d: None, None,
                 lambda k, d: (_hits3.append(d), {"action": "continue"})[1])
check("★ 短路守卫后果然被反复问（证明「只问一次」这条在承重）",
      len(_hits3) > 1, True)
check("★ 对照组：未短路时只问 1 次", len(_stall_hits), 1)


# ══════════════════ 7. 介入不该被「问到不该问的地方」 ══════════════════
print("\n── 7. 负控：一切正常时，一个介入点都不许触发 ──")

_hits8: list = []
_r8, _k8, _ev8 = run_loop([("直接交", [DELIVER()])],
                          intervene=lambda k, d: (_hits8.append(k), None)[1])
check("★ 一次交付就通过的任务：零介入（正常路径不许被打扰）", _hits8, [])
check("且零 intervene_* 事件",
      [k for k in kinds(_ev8) if k.startswith("intervene")], [])
check("它确实成功了（前提复核：不是因为压根没跑）", _r8.success, True)


print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
