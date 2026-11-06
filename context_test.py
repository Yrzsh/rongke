# -*- coding: utf-8 -*-
"""上下文出清的不变式 + 溢出分类 + 门禁零变化证明。

## 这一套在守什么

`rongke/context.py` 干的是**改写发给模型的历史**。这类改动最坏的失败方式不是报错，
而是安静地把证据链弄坏 —— 所以这里的判据全部围绕两条：

| 不许坏的东西 | 为什么它最容易悄悄坏 |
|---|---|
| **前缀稳定性** | 实测前缀缓存命中中位 94.4%。改写历史前段会打掉后续所有步的命中，**不报错、只是变贵**。没有任何异常会告诉你 cache 没了。 |
| **证据门语义** | `gate.check()` 读台账不读消息栈（`by_seq` 与 `tc.result`），所以脱水**应当**完全不影响判定。"应当"不算证据 —— 下面第 8 节拿真 `CompletionGate` 逐字节比了两次 Verdict。 |

## 三条口径

1. **正控必须真绿，负控必须真红。** 每处保护都配一条"抽掉它，断言必须当场失守"
   （承重自检）。这是本项目对"检查在、却永不触发"（G 型）唯一的解药。
2. **不联网。** 溢出分类走 `llm.looks_like_overflow`（纯函数）；LLM 一律用桩。
3. **不往仓库写东西。** 全部在内存里造消息栈与台账。
"""
from __future__ import annotations

import copy
import dataclasses
import io
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import context as C                                     # noqa: E402
from rongke.gate import CompletionGate                              # noqa: E402
from rongke.llm import ContextOverflow, LLMError, looks_like_overflow  # noqa: E402
from rongke.contract import TaskContract                            # noqa: E402
from rongke.tools import Tool, ToolCall, ToolRegistry               # noqa: E402

ok_all = True


def check(label, got, want):
    """比对 + 失败时打印。★ 打印必须截断：这条曾经吐出 70KB 的消息栈 JSON，
    把真正的失败行冲出了可见范围 —— 报错信息太长等于没报错。"""
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        s = str
        print(f"         期望 {short(want, 220)}\n         实得 {short(got, 220)}")


def truthy(label, cond, note=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  {note}" if note else ""))


# ══════════════════ 造现场：一份真实形状的消息栈 + 台账 ══════════════════
LONG = ("检索结果：DOI 10.1038/s41586-021-03456-7，转化率 92.4%，"
        "见 https://example.org/a\n") * 300          # ~1.5 万字符，够长出阈值


def make_trace(n=10, fail_at=5, prefix="call_"):
    """n 次成功调用，其中第 `fail_at` 次**失败**。

    失败那次刻意放在**中间**而不是末尾：它要落在可动区里才测得到 L2；
    放末尾就永远被 `KEEP_LIVE` 挡着，那条断言会变成一条永不触发的死检查。
    """
    tr = []
    for i in range(1, n + 1):
        bad = (i == fail_at)
        tr.append(ToolCall(
            seq=i, id=f"{prefix}{i}", name=("import_document" if bad else "web_extract"),
            args={"q": f"课题{i}"}, ok=not bad, ms=30 + i,
            result="" if bad else LONG[:6000],
            error=("文件不存在：材料区/不存在.md\n" + "   工作区里真实有的是：\n" * 300)
            if bad else "",
            fingerprint="fp%014d" % i, orig_chars=0 if bad else 90_000 + i))
    return tr


def _pair(tc):
    body = tc.result if tc.ok else f"[工具执行失败] {tc.error}"
    return [{"role": "assistant", "content": "我先看一下这份材料。" * 90,
             "tool_calls": [{"id": tc.id, "type": "function",
                             "function": {"name": tc.name,
                                          "arguments": json.dumps(tc.args)}}]},
            {"role": "tool", "tool_call_id": tc.id,
             "content": f"[seq={tc.seq} {tc.name} {'ok' if tc.ok else 'ERR'} "
                        f"{tc.ms}ms]\n{body[:6000]}"}]


def make_messages(trace):
    """真实形状的消息栈：中段夹一次交付被打回（无台账的 orphan 回灌）+ 两次重申。

    尾部刻意留 6 条**短**消息，让 `KEEP_LIVE` 的保护带正好落在它们身上 ——
    第 7 节按**原始下标**比那 6 条，而不是从末尾倒着比：出清可能在末尾追加
    台账摘要，从末尾比会因位移而假红。
    """
    m = [{"role": "system", "content": "SYSTEM 常驻" * 30},
         {"role": "user", "content": "契约正文" * 400}]
    for k, tc in enumerate(trace):
        if k == 4:                     # 中途一次交付被门打回：它不进台账
            m.append({"role": "user", "content": C.REMIND_MARK + "】原始目标：做个综述" * 60})
            m.append({"role": "assistant", "content": "我认为已经写完了。" * 80,
                      "tool_calls": [{"id": "call_orphan", "type": "function",
                                      "function": {"name": "deliver_task",
                                                   "arguments": "{}"}}]})
            m.append({"role": "tool", "tool_call_id": "call_orphan",
                      "content": "证据门打回：" + "缺少可核对的证据" * 400})
        m.extend(_pair(tc))
    m.append({"role": "user", "content": C.REMIND_MARK + "】原始目标：做个综述" * 60})
    m.append({"role": "assistant", "content": "旧的自我说明" * 400})
    m += [{"role": "assistant", "content": "短"}, {"role": "tool",
            "tool_call_id": "live", "content": "短"},
          {"role": "user", "content": "短"}, {"role": "assistant", "content": "短"}]
    return m


TRACE = make_trace()
MSGS = make_messages(TRACE)
BY_ID = C.by_call_id(TRACE)
N0 = len(MSGS)                    # 出清前的条数；保护带按原始下标比


def dump(ms):
    return json.dumps(ms, ensure_ascii=False, sort_keys=True, default=str)


def find_tool(ms, cid):
    """按 `tool_call_id` 定位，不写死下标 —— 布局一改就会错位（第一版就踩了）。"""
    return next((m for m in ms if m.get("role") == "tool"
                 and m.get("tool_call_id") == cid), None)


def short(s, n=150):
    s = str(s).replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + f"…（共 {len(s)} 字，略）"


# ══════════════════ 1. 单源：字符口径与 loop 一致 ══════════════════
print("\n── 1. 字符口径单源（loop._messages_chars 必须就是 context.messages_chars）──")
from rongke import loop as L                                          # noqa: E402

check("同一份消息，两个入口算出同一个数",
      L._messages_chars(MSGS), C.messages_chars(MSGS))
truthy("loop 里那份已经改成委托，不留第二份实现",
       "context.messages_chars" in Path("rongke/loop.py").read_text(encoding="utf-8"))


# ══════════════════ 2. 真省下了字节 ══════════════════
print("\n── 2. 出清确实省下了字节 ──")
before = C.messages_chars(MSGS)
OUT, ST = C.apply(MSGS, TRACE, target_chars=before // 3)
after = C.messages_chars(OUT)
truthy(f"压缩后严格下降（{before:,} → {after:,}）", after < before)
check("统计里的 freed_chars 是**算出来的**，不是估的",
      ST["freed_chars"], before - after)
truthy("省下的量级足够大（>40%）", ST["freed_chars"] / before > 0.40,
       f"实得 {ST['freed_chars'] / before:.1%}")


# ══════════════════ 3. 前缀稳定性：断言成字符串等式 ══════════════════
print("\n── 3. 前缀稳定（这条是整个模块存在的理由）──")
head = C.anchored_head(MSGS)
truthy(f"锚定段 = 前 {head} 条（system + 契约），逐字节等于压缩前",
       dump(OUT[:head]) == dump(MSGS[:head]))
check("锚定段判据不写死下标：少一条历史经验时仍然是 2",
      C.anchored_head([{"role": "system", "content": "s"},
                       {"role": "user", "content": "c"},
                       {"role": "assistant", "content": "a"}]), 2)
check("多一条历史经验时锚定段自动变 3（写死下标就会错）",
      C.anchored_head([{"role": "system", "content": "s"},
                       {"role": "user", "content": "c"},
                       {"role": "user", "content": "记忆"},
                       {"role": "assistant", "content": "a"}]), 3)
# 序列化前缀：锚定段 + tool_calls 的字节序都不许动
tcs_ok = all(a.get("tool_calls") is b.get("tool_calls")
             for a, b in zip(MSGS, OUT) if a.get("tool_calls"))
truthy("assistant 的 tool_calls 是**同一个对象**（按引用带走 ⇒ 序列化必等）", tcs_ok)
truthy("tool_calls 逐字节等（序列化后比字符串）",
       all(json.dumps(a["tool_calls"], sort_keys=True, default=str)
           == json.dumps(b["tool_calls"], sort_keys=True, default=str)
           for a, b in zip(MSGS, OUT) if a.get("tool_calls")))


# ══════════════════ 4. 不丢结构 ══════════════════
print("\n── 4. 不丢 role / tool_call_id / 配对关系 ──")
check("role 序列逐位不变", [m.get("role") for m in OUT[:len(MSGS)]],
      [m.get("role") for m in MSGS])
ids = lambda ms: [m.get("tool_call_id") for m in ms if m.get("role") == "tool"]
check("tool_call_id 清单一条不少", ids(OUT), ids(MSGS))
truthy("每个 assistant.tool_calls[].id 都能在后面的 tool 消息里找到对应",
       all(tc["id"] in set(ids(OUT))
           for m in OUT if m.get("tool_calls") for tc in m["tool_calls"]))
check("消息条数不减少（只出清、不删除）", len(OUT) >= len(MSGS), True)


# ══════════════════ 5. stub 可复算 ══════════════════
print("\n── 5. 替身由台账现算，第三方能重放核对 ──")
# ★ 内容级断言一律用 `FOUT`（走满五级的那份）。用按目标压的 `OUT` 会假红：
#   L1 就已经把体量压到目标以下 ⇒ 循环 break ⇒ L2/L3 压根没跑，
#   于是"失败调用被压成一行""旧重申被折叠"这两条永远看不到效果。
FOUT, FST = C.force_brief(MSGS, TRACE)
stub = C.stub_for(TRACE[0])
truthy("stub 里带 seq / 工具名 / 耗时",
       all(s in stub for s in ("#1", "web_extract", f"{TRACE[0].ms}ms")))
check("stub 的 digest == 现在从 ToolCall 重算的 digest",
      stub.split("digest=")[1].split("，")[0], TRACE[0].digest())
truthy("stub 带锚点：DOI 与 URL 都原样在里面",
       "10.1038/s41586-021-03456-7" in stub and "example.org" in stub)
check("★ 签名层面就不读消息：stub_for 只有一个参数",
      list(__import__("inspect").signature(C.stub_for).parameters), ["tc"])
truthy("out 里那条 call_1 的替身，开头就是台账现算的 stub",
       (find_tool(FOUT, "call_1") or {}).get("content", "").startswith("[#1 web_extract"))
# 把消息体换成任意内容，出清后的替身必须完全相同 —— 这才是"压缩率来自台账、
# 不来自语义理解"的可测形式（只比 `stub_for(tc)` 两次是**恒真**的废话断言）。
_m2 = copy.deepcopy(MSGS)
_m2[3]["content"] = "随便换成别的东西" * 500
_OUTb, _stb = C.apply(_m2, TRACE, target_chars=before // 3)
check("消息体换成任意内容，出清后的替身逐字相同",
      (find_tool(_OUTb, "call_1") or {}).get("content"),
      (find_tool(FOUT, "call_1") or {}).get("content"))
# 失败调用：L2 的一行替身，且**不许配锚点**（给假证据化妆）
_fail = TRACE[4]
_err_stub = (find_tool(FOUT, _fail.id) or {}).get("content", "")
truthy("失败调用走 L2 一行替身，标了 ERR", _err_stub.startswith("[#5 import_document ERR"),
       short(_err_stub, 90))
check("失败调用的替身里**没有** doi 锚点（失败按定义不是证据）",
      "doi=" in _err_stub.lower(), False)
# 无台账对应物的那条：**不许假装有出处**
_orph = (find_tool(FOUT, "call_orphan") or {}).get("content", "")
truthy("无台账回灌：明说没有 digest，而不是编一个",
       "无 digest 可给" in _orph and "digest=" not in _orph, short(_orph, 90))


# ══════════════════ 6. 幂等：不反复打爆 cache ══════════════════
print("\n── 6. apply(apply(x)) == apply(x) ──")
OUT2, ST2 = C.apply(OUT, TRACE, target_chars=before // 3)
check("第二次原样返回", dump(OUT2), dump(OUT))
check("第二次一分钱都没省（真省了就说明第一次没做完或有东西在漂）",
      ST2["freed_chars"], 0)


# ══════════════════ 7. 保护带与收益门 ══════════════════
print("\n── 7. keep_live / MIN_GAIN / 重申折叠 / 摘要不重复追加 ──")
# 保护带按**原始下标**比：出清会在末尾追加台账摘要，从末尾倒着比会因位移而假红。
truthy(f"最近 {C.KEEP_LIVE} 条（按原始下标）逐字节没被碰",
       dump(OUT[N0 - C.KEEP_LIVE:N0]) == dump(MSGS[N0 - C.KEEP_LIVE:]),
       short(dump(OUT[N0 - 1]), 80))
_short = make_messages(TRACE[:2])
for m in _short:
    if m["role"] == "tool":
        m["content"] = "短结果，不值得动" * 40          # ~1200 字符，收益不足 600+头部
_s, _st = C.apply(_short, TRACE[:2], target_chars=0)
truthy("净收益不足 MIN_GAIN_CHARS 的替换必须放弃（否则白付 cache miss）",
       _st["freed_chars"] < len("短结果，不值得动" * 40),
       f"实得 freed={_st['freed_chars']}")

_ridx = [i for i, m in enumerate(MSGS) if C.REMIND_MARK in str(m.get("content", ""))]
truthy("夹具里确实有 ≥2 次重申（否则折叠是一条永不触发的死检查）", len(_ridx) >= 2,
       f"实得 {len(_ridx)} 条 @ {_ridx}")
_folded = [i for i in _ridx[:-1] if "已折叠" in str(FOUT[i].get("content", ""))]
check("可动区里的旧重申全部被折叠", _folded, _ridx[:-1])
truthy("**最后一次重申必须原样留着**（它是模型当前该看到的那份尺子）",
       "已折叠" not in str(FOUT[_ridx[-1]].get("content", "")))
# ── L3 收益门分级的正负控（这条规则很容易写错成"永远不折"或"永远折"）──
_only3, _st_only3 = C.apply(MSGS, TRACE, target_chars=0, only_levels=(3,))
check("负控：没有任何更早的改写时，L3 不许为几百字单独打断前缀（净省 <600 就不该动）",
      _st_only3["freed_chars"], 0)
_ok13, _st13 = C.apply(MSGS, TRACE, target_chars=0, only_levels=(1, 3))
truthy("正控：L1 已在更早下标改写过（前缀那一下已经付了）⇒ L3 的折叠被放行",
       any("已折叠" in str(m.get("content", "")) for m in _ok13),
       f"freed={_st13['freed_chars']} levels={_st13['levels']}")
_brief_n = sum(1 for m in OUT if str(m.get("content", "")).startswith(C.BRIEF_MARK))
OUT3, _st3 = C.apply(OUT, TRACE, target_chars=0)
check("台账摘要就地更新、不重复追加（追加会破坏幂等）",
      sum(1 for m in OUT3 if str(m.get("content", "")).startswith(C.BRIEF_MARK)),
      _brief_n if _brief_n else 1)


# ══════════════════ 8. 门禁零变化（可审计性的正面证明）══════════════════
print("\n── 8. 同一份台账，压缩前后 gate.check 的 Verdict 必须逐字节相同 ──")
reg = ToolRegistry()
_payload = {"hits": [LONG[:6000]]}
reg.register(Tool(name="web_extract", description="检索",
                  parameters={"type": "object", "properties": {"q": {"type": "string"}},
                              "required": ["q"]},
                  fn=lambda q: json.dumps(_payload, ensure_ascii=False),
                  domain="research"))
for i in range(1, 4):
    reg.call("web_extract", {"q": f"课题{i}"}, tcid=f"call_{i}")
contract = TaskContract(raw_goal="整理一份综述", deliverables=["综述一段"],
                        acceptance=["给过检索结果"])
claim_args = {"summary": "已完成", "deliverables": ["综述一段"],
              "evidence": [{"call_seq": 1, "tool": "web_extract",
                            "acceptance": "给过检索结果", "shows": "检索结果"}]}
from rongke.gate import CompletionClaim                                   # noqa: E402


def verdict_json():
    g = CompletionGate(reg)                    # 每次新建：体检打回额度是**每任务**状态
    v = g.check(CompletionClaim.from_args(claim_args), contract)
    return json.dumps(dataclasses.asdict(v), ensure_ascii=False,
                      sort_keys=True, default=str)


V_BEFORE = verdict_json()
_msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "契约"}]
for c in reg.trace:
    _msgs.append({"role": "assistant", "content": "x" * 2000,
                  "tool_calls": [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": "{}"}}]})
    _msgs.append({"role": "tool", "tool_call_id": c.id, "content": c.result})
_msgs.append({"role": "user", "content": "收尾" * 900})
_comp, _st = C.apply(_msgs, reg.trace, target_chars=0)
truthy("这一步真的动了手（否则第 8 节是在拿两份相同的东西自证）",
       _st["freed_chars"] > 0, f"freed={_st['freed_chars']}")
check("★ Verdict 逐字节相同 —— 出清不触碰任何一道门的判定输入",
      verdict_json(), V_BEFORE)
check("台账原文一条都没被改写（证据仍在原处）",
      [len(c.result) for c in reg.trace], [len(c.result) for c in reg.trace])
truthy("消息栈确实变短了，而台账没变 —— 这就是两条轴分开",
       C.messages_chars(_comp) < C.messages_chars(_msgs))
# 反向：如果哪天有人让 stub 往台账回写，这一条必须当场失守
try:
    reg.trace[0].result = "被出清逻辑改写了"
    check("承重自检：台账一旦被改，Verdict 必须变（否则这条断言是死的）",
          verdict_json() == V_BEFORE, False)
finally:
    reg.trace[0].result = json.dumps(_payload, ensure_ascii=False)
    check("  恢复后重新相等（自证没把现场弄坏）", verdict_json() == V_BEFORE, True)


# ══════════════════ 9. 溢出分类（保守判据）══════════════════
print("\n── 9. ContextOverflow 只在真溢出时冒出来 ──")
check("OpenAI 口径的 context_length_exceeded",
      looks_like_overflow(400, '{"error":{"code":"context_length_exceeded"}}'), True)
check("Anthropic 口径的 prompt is too long",
      looks_like_overflow(400, '{"type":"error","error":{"type":"invalid_request_error",'
                               '"message":"prompt is too long: 250000 tokens"}}'), True)
check("中文网关的长度超限",
      looks_like_overflow(400, "请求失败：输入长度超出模型上限"), True)
check("413 也算", looks_like_overflow(413, "Payload Too Large context window"), True)
# ★ 最重要的一条：真·参数错**不许**被洗成"溢出→降级→重试"
check("★ 参数错的 400 必须**不是**溢出（把 bug 洗成降级是新的静默失败）",
      looks_like_overflow(400, '{"error":{"message":"messages[3]: invalid role"}}'), False)
check("max_tokens 取值不合法 ≠ 溢出（刻意没收这个词）",
      looks_like_overflow(400, "max_tokens must be <= 8192"), False)
check("缺必填参数不是溢出", looks_like_overflow(400, "missing required argument: path"), False)
check("500 不当溢出（那是可重试错误，走另一条路）",
      looks_like_overflow(500, "context length exceeded internally"), False)
truthy("ContextOverflow 是 LLMError 的子类（except LLMError 仍能兜住它）",
       issubclass(ContextOverflow, LLMError))
_e = ContextOverflow("上下文溢出 —— HTTP 400: 原文在此", body="上游原文", code=400)
check("上游原文留在异常里（判据凭什么判，得能核对）",
      (_e.body, _e.code, isinstance(_e, LLMError)), ("上游原文", 400, True))


# ══════════════════ 10. force_brief 走满阶梯 ══════════════════
print("\n── 10. 溢出兜底：force_brief 走满整级 ──")
_fb, _fst = C.force_brief(MSGS, TRACE)
truthy("比按目标压得更狠", C.messages_chars(_fb) < after,
       f"force={C.messages_chars(_fb):,} vs apply={after:,}")
check("五里至少动了 1 与 5 两级",
      (1 in _fst["levels"], 5 in _fst["levels"]), (True, True))
truthy("兜底摘要在里面",
       any(str(m.get("content", "")).startswith(C.BRIEF_MARK) for m in _fb))
_fb2, _fst2 = C.force_brief(_fb, TRACE)
check("force_brief 也幂等（不会每重试一次就再推长一截）", dump(_fb2), dump(_fb))


# ══════════════════ 12. 主循环节路：桩 LLM 驱动一次真 `_loop` ══════════════════
# 前 11 节测的都是 `context.py` 这个纯函数。但**函数对、接线错**是另一整类缺陷：
# 阈值取错配置、事件发了没记账、出清了却没把新栈发给模型 —— 全都在这一节这条路里。
print("\n── 12. 主循环接线（桩 LLM，零网络）──")
from types import SimpleNamespace                                      # noqa: E402
from rongke.llm import Usage                                           # noqa: E402


DELIVER_ARGS = {"summary": "已完成", "deliverables": ["一段正文"],
                "evidence": [{"call_seq": 1, "tool": "probe",
                              "acceptance": "正文成段", "shows": "观测正文"}]}
# 脚本用尽后的兜底必须是**交付**。兜成"再 probe 一次"的话任务永远跑不完，
# `res.reason` 说的是"步数耗尽"，被测的溢出恢复路径就被埋掉了
# （第一版就是这样，reason 里冒出一个莫名其妙的 "True"）。
_FALLBACK = dict(content="交付", calls=[("deliver_task", DELIVER_ARGS)])


class StubLLM:
    """按脚本逐轮回包；可注入 `Usage` 与异常。`seen` 是它**实际收到**的消息。

    要看"发出去的历史到底变短了没有"，只能读 `seen` —— 内核自述省了多少不算证据。
    """

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def chat(self, messages, tools=None, meta=None):
        i = len(self.seen)
        self.seen.append([dict(m) for m in messages])
        t = self.turns[i] if i < len(self.turns) else dict(_FALLBACK)
        if t.get("raises") is not None:
            raise t["raises"]
        if meta is not None:
            meta["finish_reason"] = "tool_calls"
        msg = {"role": "assistant", "content": t.get("content", "")}
        if t.get("calls"):
            msg["tool_calls"] = [{"id": f"s{i}_{j}", "type": "function",
                                  "function": {"name": n,
                                               "arguments": json.dumps(a)}}
                                 for j, (n, a) in enumerate(t["calls"])]
        return msg, t.get("usage", Usage()), t.get("reasoning", "")


class StubContract:
    """契约桩。字段要齐 —— `_reminder` 是**直接取属性**（`contract.acceptance`），
    只给 `to_prompt` 的话，跑到第 8 步（`remind_every`）才炸，报错位置离原因很远。"""

    raw_goal = "整理成一段正文"
    goal = "整理成一段正文"
    deliverables = ["一段正文"]
    acceptance = ["正文成段"]
    constraints = []
    forbidden = []
    stages = []
    needs_writing = False
    needs_figure = False
    needs_deck = False
    target_chars = None
    allow_lists = False

    def to_prompt(self):
        return "目标：把这份材料整理成一段正文" + "×" * 400

    def to_dict(self):
        return {"raw_goal": "整理成一段正文"}


def make_kernel(turns, *, loop_over=None, big_obs=6000, max_steps=10):
    k = SimpleNamespace()
    lp = dict(max_steps=max_steps, max_observation_chars=4000,
              max_identical_calls=99, context_warn_chars=0,
              compact_target_chars=0, compact_keep_live=4,
              budget_hint_steps=0, budget_max_prompt_tokens=0,
              budget_warn_ratio=0.70, budget_force_ratio=0.85)
    lp.update(loop_over or {})
    k.cfg = SimpleNamespace(loop=SimpleNamespace(**lp))
    k.llm = StubLLM(turns)
    k.gate = SimpleNamespace(reset=lambda: None,
                             check=lambda claim, ct: Verdict_ok())
    k.registry = ToolRegistry()
    k.registry.register(Tool(name="probe", description="探针",
                             parameters={"type": "object", "properties": {}},
                             fn=lambda **kw: "观测正文 " * (big_obs // 5),
                             domain="research"))
    k.telemetry = None
    k.memory = None
    # 交付时会调它取展示正文。用**真实现**（零副作用），但必须包 `staticmethod`：
    # 直接赋函数对象的话，经实例取属性会变成绑定方法 ⇒ self 占掉 claim 的位置。
    # （这条坑 `intervene_test.FakeKernel` 的注释里已经记过一遍了。）
    k._draft_for_display = staticmethod(L.Rongke._draft_for_display)
    k._written_files = staticmethod(L.Rongke._written_files)
    return k


def Verdict_ok():
    from rongke.gate import Verdict
    return Verdict(passed=True, reasons=[], feedback="通过。",
                   warnings=[], writing=None, lint=None)


def run_loop(k, max_steps=10):
    ev: list[tuple[str, dict]] = []
    res = L.Rongke._loop(k, "整理成一段正文", StubContract(), max_steps,
                       lambda kind, data: ev.append((kind, data)), None, None)
    return res, ev


def kinds(ev):
    return [k for k, _ in ev]


# ── 12.1 出清真的发生了，且**发出去的历史确实变短了**（A/B 对照，不是自述）──
_turns = [dict(content="看一下", calls=[("probe", {})]) for _ in range(9)]
_turns.append(dict(content="交付", calls=[]))
# 同一份脚本跑两遍：一遍关出清、一遍开出清。"省了多少"必须拿两次**实际发出去**的
# 字节比 —— 只看内核自报的 freed_chars 就是"测量工具自己造自己的数"。
_koff = make_kernel(_turns, loop_over={"compact_target_chars": 0})
_res_off, _ev_off = run_loop(_koff)
_off_sent = sum(C.messages_chars(s) for s in _koff.llm.seen)

_k = make_kernel(_turns, loop_over={"compact_target_chars": 9000,
                                    "compact_keep_live": 4})
_res, _ev = run_loop(_k)
_sent = [C.messages_chars(s) for s in _k.llm.seen]
_on_sent = sum(_sent)
_compact_ev = [d for k_, d in _ev if k_ == "context_compact"]
truthy("触发了出清（阈值 9k，观测每条 4k）", len(_compact_ev) >= 1,
       f"{len(_compact_ev)} 次")
check("关掉开关就一道都不发（开关是真开关）",
      kinds(_ev_off).count("context_compact"), 0)
truthy(f"整场实际发给模型的字节降了（{_off_sent:,} → {_on_sent:,}）",
       _on_sent < _off_sent * 0.85,
       f"降幅 {1 - _on_sent / _off_sent:.1%}，要求 >15%")
truthy("峰值那一步的发送体量也降了（不靠多跑几步换）",
       max(_sent) < max(C.messages_chars(s) for s in _koff.llm.seen))
# 事实必须落在**内核记的那份**上（`ctx_econ` 的跨源核对读的是归档里的它）
truthy("出清事实进了 `_compact_log`（含 freed_chars 与级别）",
       bool(k_compact := getattr(_k, "_compact_log", {})) and
       k_compact.get("freed_chars", 0) > 0 and k_compact.get("levels"),
       str({kk: k_compact.get(kk) for kk in ("freed_chars", "levels", "why")}))
_SRC = Path("rongke/loop.py").read_text(encoding="utf-8")
_sr = _SRC.split("def _run_serial")[1] if "def _run_serial" in _SRC else ""
check("收尾把这份事实挂进 RunResult.compacted（归档里才有第二个来源）",
      "result.compacted = dict(getattr(self, \"_compact_log\"" in _sr, True)
truthy("成功路径与**崩溃路径**都挂（溢出那条历史上是从崩溃出口走的）",
       _sr.count('"_compact_log"') >= 2, f"_run_serial 里引用了 {_sr.count('\"_compact_log\"')} 次")

# ── 12.2 迟滞：压不动的时候不许每步再试 ──
# 阈值设成 1000 字符 —— **永远压不到那么低**（锚定段自己就超了）。
# 没有迟滞的话，这里会出清 9 次（每步一次，每步一次 cache miss）；
# 有迟滞的话只该有个位数。这条就是迟滞机制的正负控合一。
_k2 = make_kernel(_turns, loop_over={"compact_target_chars": 1000,
                                     "compact_keep_live": 4})
_res2, _ev2 = run_loop(_k2)
_n2 = kinds(_ev2).count("context_compact") + kinds(_ev2).count("context_compact_skipped")
truthy(f"目标不可达时也没有每步空跑（10 步里只试了 {_n2} 次，应 ≤4）", _n2 <= 4)
truthy("至少还试过一次（迟滞不等于「从此不动」）", _n2 >= 1)
# ★ compact_log 与"有效出清"必须同起同落。以前 `_compact` 无条件写它，
#   于是"只空跑过"的一场任务会留下记录却没有对应事件 —— ctx_econ 的跨源核对
#   （R6）恰好把这形状判成红。这是一次**假警报**，比漏报更难查（它会让人去改判据）。
_eff2 = kinds(_ev2).count("context_compact")
check("compact_log 只在**真省到字节**时写（空跑不进账，否则 R6 会假警报）",
      bool(getattr(_k2, "_compact_log", {})), _eff2 > 0)
truthy("每条 skipped 事件的 freed_chars 都是 0（与「没写进账」同口径）",
       all(d.get("freed_chars") == 0 for k_, d in _ev2
           if k_ == "context_compact_skipped"), True)

# ── 12.3 收尾提示：恰在剩余步数不足时发一次，且**每场最多一次** ──
_k3 = make_kernel(_turns, loop_over={"budget_hint_steps": 5}, max_steps=10)
_res3, _ev3 = run_loop(_k3, max_steps=10)
_hint = [d for k_, d in _ev3 if k_ == "wrapup_hint"]
check("收尾提示恰好一条", len(_hint), 1)
truthy("它出现在剩余步数 ≤5 的那一步", _hint and _hint[0]["step"] >= 5,
       str(_hint[:1]))
check("提示里如实报了剩余步数", _hint and _hint[0]["steps_left"] <= 5, True)
# 负控：关掉就不该有
_k3b = make_kernel(_turns, loop_over={"budget_hint_steps": 0}, max_steps=10)
_, _ev3b = run_loop(_k3b, max_steps=10)
check("负控：budget_hint_steps=0 时一条都不发（开关是真开关）",
      kinds(_ev3b).count("wrapup_hint"), 0)
# 提示要真的进到发给模型的消息里（只发事件不改上下文＝假动作）
truthy("WRAPUP 确实被 append 进了下一次请求",
       any(L.WRAPUP in str(m.get("content", ""))
           for s in _k3.llm.seen for m in s))

# ── 12.4 token 压力闸：由**实测 usage** 驱动，且缺字段时自报降级 ──
_usages = [Usage(input_tokens=int(2000 * (1.5 ** i))) for i in range(9)]
_t4 = [dict(content="看", calls=[("probe", {})], usage=u) for u in _usages]
_k4 = make_kernel(_t4, loop_over={"budget_max_prompt_tokens": 20_000,
                                  "budget_warn_ratio": 0.7})
_res4, _ev4 = run_loop(_k4)
_bn = [d for k_, d in _ev4 if k_ == "budget_note"]
truthy(f"prompt 到 70% 时发了压力提示（{len(_bn)} 条）", len(_bn) == 1)
check("用的是网关实测 input_tokens，不是字数换算",
      _bn and _bn[0].get("degraded", ""), "")
check("负控：分母未配（窗口未知）时一道闸都不发 —— 造一个假数比不判更糟",
      kinds(run_loop(make_kernel(_t4, loop_over={"budget_max_prompt_tokens": 0}))[1])
      .count("budget_note"), 0)

# ── 12.5 溢出兜底：这是本轮真正要修的那条硬崩路径 ──────────────
# 场景必须**先跑几步攒出台账**再溢出。在第 1 步就溢出的话台账是空的、可动区也是空的，
# "出清"无事可做 ⇒ 测出来的是"没变短"，而那不是缺陷、是场景造假。
_probe = dict(content="看一下", calls=[("probe", {})])
_ov = ContextOverflow("上下文溢出 —— HTTP 400: context_length_exceeded",
                      body="context_length_exceeded", code=400)
_k5 = make_kernel([_probe, _probe, _probe, _probe, {"raises": _ov}],
                  loop_over={"compact_target_chars": 9000, "compact_keep_live": 4})
_res5, _ev5 = run_loop(_k5)
_i = 4                                     # 第 5 次 chat 就是崩的那一次
truthy("溢出被就地兜住：先发事件、再重试成功、任务最终交付",
       ("context_overflow" in kinds(_ev5)
        and "context_overflow_recovered" in kinds(_ev5) and _res5.success),
       f"success={_res5.success} reason={_res5.reason[:60]}")
_a = C.messages_chars(_k5.llm.seen[_i])
_b = C.messages_chars(_k5.llm.seen[_i + 1])
truthy("重试那一次收到的消息**比崩之前短**（真的出清了，不是原样重发）",
       _b < _a, f"{_a:,} → {_b:,}")
check("溢出事件带上了上游原文（判据凭什么判，得能核对）",
      bool(next(d for k_, d in _ev5 if k_ == "context_overflow")["body"]), True)
_eff = kinds(_ev5).count("context_compact")
_skip = kinds(_ev5).count("context_compact_skipped")
check("有效出清一场只一次（迟滞线抬起了，没有每步再压）", _eff, 1)
# `skipped` 不是浪费：它表示"又试了一次，但可动区已经压干净，一分钱没省"
# ⇒ 不动任何消息 ⇒ **不付前缀缓存那一下**。它存在的意义恰恰是"没静默"。
truthy("省不到字节的那一次如实发了 skipped，而不是假装压过了", _skip <= 1,
       f"eff={_eff} skipped={_skip}")
truthy("skipped 那条的 freed_chars 确然是 0（口径自洽）",
       all(d.get("freed_chars") == 0 for k_, d in _ev5
           if k_ == "context_compact_skipped"), True)
# 兜不住的时候：必须如实说"可续跑"，不许说成失败，也不许什么都不留
_k6 = make_kernel([_probe, _probe, {"raises": _ov}, {"raises": _ov}],
                  loop_over={"compact_target_chars": 9000})
_res6, _ev6 = run_loop(_k6)
truthy("两次都溢出 ⇒ 如实收场，且话里写明**可以接着跑**",
       (not _res6.success) and "可续跑" in _res6.reason, _res6.reason[:70])
check("失败样本里不发 recovered（否则就是把崩了渲染成救回来了）",
      "context_overflow_recovered" in kinds(_ev6), False)
# ★ 最关键的一条负控：真·参数错的 400 不许被咽成"溢出→降级→重试"
_k7 = make_kernel([{"raises": LLMError("LLM 调用失败 —— HTTP 400: invalid role")},
                   dict(content="交付", calls=[])],
                  loop_over={"compact_target_chars": 9000})
_ev7: list[tuple[str, dict]] = []
_escaped = None
try:
    L.Rongke._loop(_k7, "整理成一段正文", StubContract(), 10,
                   lambda kind, data: _ev7.append((kind, data)), None, None)
except LLMError as _e7:
    _escaped = _e7
check("★ 普通 LLMError 不进溢出分支：一条 context_overflow 都不发",
      kinds(_ev7).count("context_overflow"), 0)
truthy("它**原样向上抛**（由 `_run_serial` 兜成「执行中断」，行为逐字如旧）",
       isinstance(_escaped, LLMError) and not isinstance(_escaped, ContextOverflow),
       type(_escaped).__name__ if _escaped else "没抛出去")
check("也没有偷偷降级重试一次（那会把 bug 洗成看不见的成功）",
      len(_k7.llm.seen), 1)


print("\n── 11. 边界：空与极短 ──")
check("空消息栈", C.apply([], [], target_chars=0)[1]["freed_chars"], 0)
check("只有锚定段", C.messages_chars(C.apply(MSGS[:2], [], target_chars=0)[0]),
      C.messages_chars(MSGS[:2]))
check("keep_live 大于总长时什么都不动",
      C.apply(MSGS, TRACE, target_chars=0, keep_live=999)[1]["freed_chars"], 0)
check("台账为空时 tool 消息走 orphan 路径而不是崩",
      C.apply(MSGS, [], target_chars=0)[1]["freed_chars"] > 0, True)


print("\n── 13. ★ 保护窗口必须按「步」算（2026-10-15 真事故）──")
# 事故：run 20261015-193936，78 条 tool 结果里 **37 条被脱水**，
# 含 20,465 字符的 kb_bibtex 与 9,701 字符的 kb_read —— 模型一次都没看见，
# 于是把同一个调用反复重发（原地打转两轮）。
# 根因：`KEEP_LIVE = 6` 条消息，而模型平均每步发 2.89 个调用
# ⇒ 每步 3.89 条消息 ⇒ 只护住 1.54 步，**第 N 步的结果第 N+1 步就能被脱水**。
#
# 这一节就拿"密集并行调用"的形状钉住它：一步发 6 个调用。

def dense_msgs(steps=6, per=6, body=3000):
    """造一份**密集形状**的消息栈：每步 1 条 assistant(+tool_calls) + per 条结果。

    每条结果都足够长（body 字符），所以每一条都"值得"被脱水 ——
    这样测到的是"保护窗口挡没挡住"，而不是"长度不够所以没动"。
    """
    ms = [{"role": "system", "content": "S" * 200},
          {"role": "user", "content": "U" * 200}]
    tr = []
    seq = 0
    for s in range(steps):
        tcs = []
        for k in range(per):
            seq += 1
            cid = f"d{seq}"
            tcs.append({"id": cid, "type": "function",
                        "function": {"name": "web_extract", "arguments": "{}"}})
            tr.append(ToolCall(seq=seq, id=cid, name="web_extract",
                               args={"q": f"q{seq}"}, ok=True, ms=10,
                               result="R" * body, fingerprint="fp%012d" % seq,
                               orig_chars=body))
        ms.append({"role": "assistant", "content": "", "tool_calls": tcs})
        for tc in tcs:
            ms.append({"role": "tool", "tool_call_id": tc["id"],
                       "content": "R" * body})
    return ms, tr


_dm, _dtr = dense_msgs()
_dout, _dstat = C.apply(_dm, _dtr, target_chars=0, keep_live=6)

# ★ 承重判据：**最近 4 步的结果，一条都不许被脱水**。
#   判据必须按「步」定位，不能按「末尾 N 条」定位 ——
#   第一版写的是 `_dout[-28:]`，而 apply 会追加 L5 台账摘要，
#   数组长度变成 45，`[-28:]` 于是整体前移一格，把**上一步**的一条
#   也算进来，报出假红（24/28）。窗口的语义单位是「步」，
#   判据就得沿步边界取，见下面 `_last_steps_span`。
def _last_steps_span(messages, steps, per):
    """返回最近 `steps` 步覆盖的下标区间 —— 步边界=带 tool_calls 的 assistant。"""
    bounds = [i for i, m in enumerate(messages)
              if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")]
    return bounds[-steps] if len(bounds) >= steps else 0


_want = C.KEEP_LIVE_STEPS * 6      # 保护窗口（步）× 每步 6 条结果
_cut = _last_steps_span(_dout, C.KEEP_LIVE_STEPS, 6)
_kept_len = sum(1 for m in _dout[_cut:] if len(str(m.get("content") or "")) == 3000)
truthy("★ 密集形状（每步 6 调用）下，保护窗口内的结果一条都没被脱水",
       _kept_len >= _want, f"仍是原文的 {_kept_len} 条（至少应 {_want} 条）")

# 旧口径的反证：只按条数（6 条）保护时，同一段必然有大量被脱水。
_old, _ = C.apply(_dm, _dtr, target_chars=0, keep_live=6, keep_live_steps=0)
_ocut = _last_steps_span(_old, C.KEEP_LIVE_STEPS, 6)
_old_kept = sum(1 for m in _old[_ocut:] if len(str(m.get("content") or "")) == 3000)
truthy("★ 反证：按条数口径（keep_live_steps=0）时，同一段确有结果被脱水",
       _old_kept < _kept_len,
       f"条数口径 {_old_kept} 条 vs 步数口径 {_kept_len} 条")

# ★ 事故的最小复现判据：**第 N 步的结果，到第 N+1 步还读得到**。
#   这正是 2026-10-15 坏掉的那件事（模型一次都没看见数据）。
#   造 3 步，只看"倒数第二步"那批结果是否仍是原文。
_d3, _t3 = dense_msgs(steps=3, per=6)
_o3, _ = C.apply(_d3, _t3, target_chars=0)
_bounds3 = [i for i, m in enumerate(_o3)
            if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")]
_prev_step = _bounds3[-2]                      # 倒数第二步的 assistant
_prev_kept = sum(1 for m in _o3[_prev_step:_prev_step + 7]
                 if len(str(m.get("content") or "")) == 3000)
truthy("★★ 事故最小复现：上一步（第 N-1 步）的 6 条结果仍可读",
       _prev_kept >= 6, f"上一批仍是原文的 {_prev_kept} 条（应 6 条）")

# 单步稀疏形状仍要工作：一步只发 1 个调用时，条数口径兜底。
_sp, _str_ = dense_msgs(steps=6, per=1)
_so, _ = C.apply(_sp, _str_, target_chars=0, keep_live=6)
_scut = _last_steps_span(_so, C.KEEP_LIVE_STEPS, 1)
_sp_kept = sum(1 for m in _so[_scut:] if len(str(m.get("content") or "")) == 3000)
truthy("稀疏形状（每步 1 调用）下保护窗口仍然生效",
       _sp_kept >= C.KEEP_LIVE_STEPS, f"仍是原文的 {_sp_kept} 条")

# 幂等性不能因为新口径而破（本模块最核心的不变式）。
_o1, _ = C.apply(_dm, _dtr, target_chars=0)
_o2, _ = C.apply(_o1, _dtr, target_chars=0)
check("★ 新口径下 apply(apply(x)) == apply(x)（幂等未破）",
      C.messages_chars(_o2), C.messages_chars(_o1))

# 关得掉：keep_live 覆盖全栈时不动（"关"必须关得掉）。
check("keep_live 覆盖全栈 ⇒ 一步数口径也拦不住，确实不动",
      C.apply(_dm, _dtr, target_chars=0, keep_live=9999)[1]["freed_chars"], 0)


print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
if ok_all:
    print("[DONE] 上下文出清不变式全部成立")
sys.exit(0 if ok_all else 1)
