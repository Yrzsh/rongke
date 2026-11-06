# -*- coding: utf-8 -*-
"""工具注册表（rongke/tools.py）单测 —— 纯函数，不需要 LLM、网络。

为什么单独给注册表开一套：它是**所有工具调用的唯一咽喉**，而此前没有任何套件直接测它
（`fence_test.py` 只覆盖围栏那条路径，`mcp_client_test.py` 只覆盖 MCP 一侧）。
2026-09-19 实测发现它的报错回灌路径有一个**每场任务都在付**的代价，于是把它钉住。

覆盖：
  1. 成功路径     —— ok=True / error 必须为空串（负控：不许给成功挂错）
  2. 报错回灌     —— **不许夹 traceback**、**消息不许出现两遍**（2026-09-19 修的那条）
                     用的是实测抓到的真实形状：6,272 字符、内含 73 行文件清单
  3. 报错上限     —— 成功有截断、失败也必须有；**短报错不许被截**（负控）
  4. 必填参数缺失 —— **不执行**（正控：拿副作用当判据，不比对措辞）
  5. 围栏拦截     —— mutating 工具的越界绝对路径被拒，且标记 blocked_by
  6. 承重自检     —— 把报错截断那行短路，超长断言必须**当场失守**

一条刻意的记法：判据尽量落在**可核对的量**上（长度、是否被调用、有无某个子串），
不落在"措辞像不像"。本项目为此付过一次学费（见 README 的 119 字事件）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                       # noqa: E402

from rongke.tools import Tool, ToolCall, ToolRegistry   # noqa: E402
from rongke.hooks import HookDecision, WorkspaceFence   # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


# ── 桩工具 ────────────────────────────────────────────────
CALLS: list[str] = []


def fn_ok(**kw):
    CALLS.append("ok")
    return "正常结果"


def fn_boom(**kw):
    CALLS.append("boom")
    raise RuntimeError("⛔ 简单错误")


def fn_boom_real(**kw):
    """★ 实测形状：6,272 字符的报错，内含一份 73 行文件清单（只列 60 行 + 省略号）。"""
    CALLS.append("boom_real")
    files = "\n".join(f"  - references/examples/abstract/template-{i:03d}.md" for i in range(60))
    raise RuntimeError(
        "⛔ `nature-writing` 里没有 'core/stance.md'。\n可用文件：\n"
        + files + "\n  …（共 73 个）")


def fn_boom_huge(**kw):
    CALLS.append("boom_huge")
    raise RuntimeError("X" * 40000)


def fn_huge(**kw):
    CALLS.append("huge")
    return "Y" * 40000


def fn_needs_arg(a: str = "", **kw):
    CALLS.append("needs_arg")
    return f"a={a}"


def fn_writes(path: str = "", **kw):
    CALLS.append("writes")
    return f"wrote {path}"


def mk(fn, name="t", mutating=False) -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool(name=name, description="测试用", parameters={"type": "object"}, fn=fn,
                    domain="test", mutating=mutating))
    return r


TRUNC = 12000

print("── 1. 成功路径 ──")
CALLS.clear()
tc = mk(fn_ok).call("t", {})
check("ok=True", tc.ok, True)
check("成功时 error 必须是空串（负控：不许给成功挂错）", tc.error, "")
check("结果原样带回", tc.result, "正常结果")
check("fn 真的被调用了", CALLS, ["ok"])

print("\n── 2. 报错回灌：不夹 traceback、消息不重复 ──")
CALLS.clear()
tc2 = mk(fn_boom).call("t", {})
check("ok=False", tc2.ok, False)
check("带异常类型前缀（模型要靠它分辨是哪类失败）", tc2.error.startswith("RuntimeError: "), True)
check("★ 不含 Python traceback（栈帧对模型零价值）",
      "Traceback (most recent call last)" in tc2.error, False)
check("★ 不含本项目文件名（否则说明栈还是漏进来了）",
      "rongke\\tools.py" in tc2.error or "rongke/tools.py" in tc2.error, False)
check("失败时 result 为空串", tc2.result, "")

# ★ 实测形状：原实现在这条上会把整份清单印两遍（一遍在 f"{e}"，一遍在 format_exc()）
tc3 = mk(fn_boom_real).call("t", {})
err3 = tc3.error
REAL_LEN = 6272          # 修复前实测长度（同一份实测形状，加上重复与栈）
check("★ 真实形状的报错：清单只能出现一次（原文里出现两遍）",
      err3.count("template-000.md"), 1)
check("★ 真实形状的报错：不能整段重复（用首行做哨兵）",
      err3.count("可用文件："), 1)
check(f"★ 修完长度必须显著短于修复前的实测值 {REAL_LEN}",
      len(err3) < REAL_LEN, True)
check("消息本体没丢（诊断信息仍然完整）",
      "`nature-writing` 里没有 'core/stance.md'" in err3, True)

print("\n── 3. 报错上限：成功有截断、失败也必须有 ──")
tc4 = mk(fn_boom_huge).call("t", {})
check("★ 超长报错被截断", len(tc4.error) < 40000, True)
# 别硬写原长（异常里带了前缀，实际是 40013 而不是 40000 —— 我第一版就是这么写错的）。
# 判据落在**结构**上：有"已截断"标记、且标记里报了原长。
check("★ 截断有明确标记且报出原长", ("已截断" in tc4.error and "原长" in tc4.error), True)
check("截断后总长有界（truncate + 标记行）", len(tc4.error) <= TRUNC + 60, True)

tc5 = mk(fn_huge).call("t", {})
check("成功侧超长结果照旧截断（正控：别把既有行为改坏）",
      "已截断" in tc5.result and len(tc5.result) <= TRUNC + 60, True)

tc6 = mk(fn_boom).call("t", {})
check("负控：短报错**不许**被截（否则诊断信息被砍掉）", "已截断" in tc6.error, False)
check("负控：短报错原样保留", tc6.error, "RuntimeError: ⛔ 简单错误")

print("\n── 4. 必填参数缺失：不执行 ──")
CALLS.clear()
r7 = ToolRegistry()
r7.register(Tool(name="needs", description="缺参测试",
                 parameters={"type": "object", "properties": {"a": {"type": "string"}},
                             "required": ["a"]},
                 fn=fn_needs_arg, domain="test"))
tc7 = r7.call("needs", {})
check("ok=False", tc7.ok, False)
check("★ fn **根本没被调用**（拿副作用当判据，不比对措辞）", CALLS, [])
check("报错里点名了缺哪个参数", "a" in tc7.error, True)
check("报错里给出该工具接受什么（否则模型只能再猜一次）", "本工具接受" in tc7.error, True)

print("\n── 5. 围栏：越界绝对路径被拒 ──")
with T.scratch("tools_", "tools-tmp") as td:
    ws = Path(td) / "ws"
    ws.mkdir()
    outside = str(Path(td) / "outside" / "x.txt")
    r8 = mk(fn_writes, name="wf", mutating=True)
    r8.hooks = [WorkspaceFence(roots=[ws], base=ws)]
    CALLS.clear()
    tc8 = r8.call("wf", {"path": outside}, truncate=TRUNC)
    check("ok=False", tc8.ok, False)
    check("★ 被围栏拦下（fn 没被调用）", CALLS, [])
    check("标记了 blocked_by", tc8.blocked_by, "workspace_fence")
    # 正控 + 负控：界内绝对路径必须放行（否则上面那条"拦下了"可能只是它拦所有绝对路径）
    CALLS.clear()
    tc9 = r8.call("wf", {"path": str(ws / "in.txt")}, truncate=TRUNC)
    check("负控：界内绝对路径放行", (tc9.ok, tc9.blocked_by), (True, ""))
    check("界内的确实执行了", CALLS, ["writes"])

print("\n── 6. 承重自检：把报错截断短路 ⇒ 超长断言必须当场失守 ──")
import rongke.tools as _T   # noqa: E402
_live = mk(fn_boom_huge).call("t", {})
_live_truncated = "已截断" in _live.error
_src = Path(_T.__file__).read_text(encoding="utf-8")
# 不 monkeypatch 模块（截断是内联在 call() 里的），改用**源码级短路**证明它承重：
# 把那段 if 的条件改成恒假，重跑同一场景 —— 与写测试里 `re.compile(r"(?!)")` 同一手法。
_patched = _src.replace('if len(err) > truncate:', 'if False and len(err) > truncate:')
check("短路补丁确实改到了源码（否则下面的自检是空的）", _patched != _src, True)
_ns: dict = {}
exec(compile(_patched, str(_T.__file__), "exec"), _ns)   # noqa: S102
# ⚠️ 必须用 `_ns["ToolRegistry"]` —— 第一版这里写的是裸 `ToolRegistry()`，
#    于是解析到**本模块导入的未打补丁的那个类**，自检恒过（G 型：正控是别的原因在过）。
_dead_r = _ns["ToolRegistry"]()
_dead_r.register(_ns["Tool"](name="t", description="x", parameters={"type": "object"},
                            fn=fn_boom_huge, domain="test"))
_dead = _dead_r.call("t", {})
check("★ 短路后超长报错不再被截断（证明这条守卫在承重）",
      (_live_truncated, "已截断" in _dead.error), (True, False))
check("短路后长度回到 4 万量级（与上面形成对照）", len(_dead.error) > 39000, True)

print("\n── 7. 结果锚点：digest 之外的『可核对物』──")
# 背景（Q 型缺陷：证据不可复算）：归档与事件里原先**只有 digest**（结果哈希），
# 于是证据门第 4 条离线不可复算、界面只能显示一串哈希、审计包只能被"相信"。
# 本节钉住新增的四件：预览 / 真实长度 / 是否截断 / 锚点。
from rongke.tools import extract_anchors, RESULT_PREVIEW_CHARS   # noqa: E402

SAMPLE = (
    "Ophiocordyceps sinensis 综述。DOI: 10.1038/s41586-021-03456-7。\n"
    "见 https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/, "
    "另见 https://doi.org/10.1126/science.abn1234。\n"
    "样本量 1,234 份，感染率 15.2%，P = 3.4e-5，置信区间 [0.87, 0.93]。\n"
    "版本 v1.2.3，编号 abc123，页码 350-360。中文句末。"
)
A = extract_anchors(SAMPLE)
check("★ DOI 抽到且规范化成小写", A.get("doi"),
      ["10.1038/s41586-021-03456-7", "10.1126/science.abn1234"])
check("URL 抽到两条（末尾的英文逗号必须剥掉）",
      A.get("url"), ["https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567/",
                     "https://doi.org/10.1126/science.abn1234"])
check("千分位归一：1,234 → 1234", "1234" in A.get("num", []), True)
check("小数与科学计数法都在", ("15.2" in A["num"], "3.4e-5" in A["num"]), (True, True))
check("标识符里的数字不算锚点（abc123 / 版本号尾段）",
      ("123" in A["num"], "3" in A["num"]), (False, False))
# ★ 承重负控 —— 首版实测就栽在这里：DOI 被数值规则**再切一次**，
#   凭空多出 021 / 03456 / 7。后果不是噪声，而是下游判据被**假通过**：
#   随手写个 021 都能在证据里"追到"。
check("★ DOI 不许被数值规则切碎（021 / 03456 必须缺席）",
      ("021" in A["num"] or "03456" in A["num"]), False)
check("空输入返回空表（负控：别把没结果渲染成有锚点）", extract_anchors(""), {})
check("纯文本无锚点 → 空表", extract_anchors("这是一段没有数字的散文。"), {})
_long = "、".join(str(i) for i in range(200))
check("锚点有上限（防单次超长结果撑爆归档）",
      len(extract_anchors(_long)["num"]) <= 40, True)

# ★★ 单位数不算锚点 —— 这条是**第一次真跑**才暴露的：
#    装载技能类的文本里满是编号清单，产出 ['1','2','3','4','5','6']。
#    收进锚点集 ⇒ "数字必须能追到证据"这条判据**恒真**（任何稿子里的 3 都能追到）。
#    一个拦不住东西的检查比没有检查更糟：它让人以为这一层已经被守住了。
check("★ 单位数不产出锚点（编号清单不是量值）",
      extract_anchors("1. 苹果\n2. 香蕉\n3. 梨\n4. 桃").get("num", []), [])
check("★ 两位及以上的数照收（10 / 2021 / 1234）",
      extract_anchors("12 篇文献，2021 年，共 1234 份样本").get("num", []), ["12", "2021", "1234"])
check("★ 小数与科学计数法照收（0.5 不是单位数）",
      ("0.5" in extract_anchors("均值 0.5，P=3.4e-5").get("num", [])), True)
# 全空时**整组缺席**（而不是给一个空 list）：载荷小，且下游一律 `anchors[k] || []`。
check("全空时整组缺席，不是空 list", extract_anchors("1. 甲\n2. 乙"), {})

print("\n── 7b. to_dict：digest 不许动，四件新品必须齐 ──")
tcA = mk(fn_ok).call("t", {})
dA = tcA.to_dict()
check("★ digest 一字未变（存量归档 / 去重 / 指纹都依赖它）", dA["digest"], tcA.digest())
check("六件新品齐全（preview / chars / orig_chars / dropped / truncated / anchors）",
      sorted(k for k in dA if k.startswith("result_") or k == "anchors"),
      ["anchors", "result_chars", "result_dropped", "result_orig_chars",
       "result_preview", "result_truncated"])
check("短结果：self-report 不许截断（负控 —— 不能对所有结果都喊截断）",
      (dA["result_truncated"], dA["result_chars"]), (False, len("正常结果")))
check("短结果预览 = 原文", dA["result_preview"], "正常结果")
check("成功调用的锚点表在（无锚点时是空 dict，不是缺失）",
      isinstance(dA["anchors"], dict), True)

tcB = mk(fn_huge).call("t", {})          # fn_huge 返回 40000 个 "Y"
dB = tcB.to_dict()
check("★ 长结果：truncated=True", dB["result_truncated"], True)
# ⚠️ 这里**有两层截断**，别把它们混成一层（我第一版就把期望值写成了 40018）：
#   ① `ToolRegistry.call(truncate=12000)` 截的是**进内核的那份结果**，标记写在文本里；
#   ② `to_dict(preview=800)` 截的是**给人看的那一段**，标记走 `result_truncated` 字段。
# 所以 result_chars 报的是"内核手里那份"的长度 —— 这是对的，别看错成"工具返回的原长"。
check("★ result_chars == 内核实际持有的长度（不是工具返回的原长，也不是预览长）",
      (dB["result_chars"], dB["result_chars"] == len(tcB.result)), (len(tcB.result), True))
check("★ 预览是持有的那份的前 N 字（截断位置可复算）",
      dB["result_preview"], tcB.result[:RESULT_PREVIEW_CHARS])
# ★★ 这一条是第一版**没通过**才补出来的：调用级截断的标记写在 12,000 字文本的**尾巴上**，
#     而界面给人看的是**头部 800 字** —— 预览里根本看不到它。
#     于是"截了两层"被渲染成"工具就返回了这么点"（M 型：把没看全说成看过了）。
#     治法是把原长**结构化**记下来（orig_chars），而不是回头去正则捞那句话。
check("★ 两层截断都必须自报：预览超限、且原件被截过",
      dB["result_truncated"], True)
check("★ 工具真正返回的原长被结构化记下（40,000，不是 12,024）",
      dB["result_orig_chars"], 40000)
check("★ 没被带出来的字数 = 原长 − 预览长（人一眼看出'这是第几成'）",
      dB["result_dropped"], 40000 - RESULT_PREVIEW_CHARS)
check("★ 短结果不许虚报丢失（负控：dropped 必须为 0）",
      dA["result_dropped"], 0)
check("★ 短结果 orig == 持有长度（负控：别把 -1 的哨兵值漏出去）",
      dA["result_orig_chars"], len("正常结果"))
check("预览长度受 RESULT_PREVIEW_CHARS 约束",
      len(dB["result_preview"]) <= RESULT_PREVIEW_CHARS, True)

tcC = mk(fn_boom).call("t", {})
dC = tcC.to_dict()
check("★ 失败调用不给锚点（失败结果按定义不是证据，配锚点=给假证据化妆）",
      dC["anchors"], {})

print("\n── 7c. 承重自检：短路遮蔽 ⇒ 假锚点必须当场复发 ──")
_A_src = Path(_T.__file__).read_text(encoding="utf-8")
_A_patched = _A_src.replace(
    'masked = _RE_DOI.sub(lambda m: " " * len(m.group(0)), t)', "masked = t")
check("短路补丁确实改到了源码（否则下面的自检是空的）", _A_patched != _A_src, True)
_A_ns: dict = {}
exec(compile(_A_patched, str(_T.__file__), "exec"), _A_ns)   # noqa: S102
_dead_anchors = _A_ns["extract_anchors"](SAMPLE)
check("★ 短路后 DOI 果然被切碎（证明遮蔽在承重，而不是恰好没事）",
      ("021" in _dead_anchors.get("num", [])), True)
check("★ 对照组：未短路时同一输入干净",
      ("021" in A["num"]), False)

# ★ 第二道守卫（单位数过滤）也要承重：短路它 ⇒ 编号清单必须重新变成锚点
_A_patched2 = _A_src.replace(
    "num = _dedup_cap([n for n in raw_num if len(n) >= 2], limit)",
    "num = _dedup_cap(raw_num, limit)")
check("补丁②确实改到了源码", _A_patched2 != _A_src, True)
_A_ns2: dict = {}
exec(compile(_A_patched2, str(_T.__file__), "exec"), _A_ns2)   # noqa: S102
_LIST = "1. 苹果\n2. 香蕉\n3. 梨"
_dead2 = _A_ns2["extract_anchors"](_LIST).get("num", [])
check("★ 短路后编号清单果然重新变成锚点（证明这条过滤在承重）",
      _dead2, ["1", "2", "3"])
check("★ 对照组：未短路时同一输入干净",
      extract_anchors(_LIST).get("num", []), [])

print("\n── 8. 参数没解析出来：**不许说成「你没传参数」，更不许拿空参数去跑** ──")
# 2026-09-21 实测（runs/20260921-113729）：模型某次 `import_document` 的
# arguments 流到 `"kind": "pdf</` 就断了（未闭合）⇒ json.loads 失败 ⇒
# 原实现静默 `args = {}` ⇒ 报回「缺少必填参数：file / 你传的是：（一个都没传）」。
# 模型**明明传了 file**，于是收到一条**自相矛盾**的反馈，无从下手。
# 两种情况的下一步动作完全不同 ——
#   参数坏掉 ⇒「重发一次完整 JSON」；真的没传 ⇒「补上这个字段」。
# 压成同一个口径就是 M 型（口径塌缩），所以单独一支分支 + 单独一套断言。
_SIDE: list[str] = []
_r8 = ToolRegistry()
_r8.register(Tool(name="needs", description="缺参",
                  parameters={"type": "object", "properties": {"file": {"type": "string"}},
                              "required": ["file"]},
                  fn=lambda **k: _SIDE.append("ran") or "ok", domain="test"))
# ★★ 这条才是真正的安全洞：**不要求必填参数**的工具在 `_missing_required`
#    眼里"参数齐全" ⇒ 原实现会拿 `{}` **真的把它跑起来**。
_r8.register(Tool(name="noarg", description="无必填",
                  parameters={"type": "object", "properties": {}},
                  fn=lambda **k: _SIDE.append("side-effect") or "ok", domain="test"))

_PARSE_ERR = ("JSONDecodeError: Expecting ',' delimiter: line 1 column 143 (char 142)\n"
              "   你给的原文共 143 字符，尾部 160 字符：…\"kind\": \"pdf</")
_t1 = _r8.call("needs", {}, parse_error=_PARSE_ERR)
check("解析失败 ⇒ ok=False", _t1.ok, False)
check("★★ 说清是「不是合法 JSON」，而不是「你没传参数」",
      "不是合法 JSON" in _t1.error, True)
check("★ 明说这一次**没有执行**", "没有执行" in _t1.error, True)
check("带出解析器原文（可诊断，不是一句「调用失败」）",
      "JSONDecodeError" in _t1.error, True)
check("★ 负控：**不许**再出现「（一个都没传）」那类假话",
      "一个都没传" in _t1.error, False)
check("标记 blocked_by=parse_error（可追溯拦截原因）", _t1.blocked_by, "parse_error")
check("进台账 —— 参数坏掉是模型可靠性的一手信号，不该只活在日志里",
      len(_r8.trace), 1)

_t2 = _r8.call("noarg", {}, parse_error=_PARSE_ERR)
check("★★ 无必填参数的工具**也绝不执行**（否则坏参数会带着 {} 动手）",
      _SIDE, [])
check("无必填参数时同样如实报解析失败", "不是合法 JSON" in _t2.error, True)

# 负控三连：**正常调用不许被这条新分支吃掉** —— 新判据最容易犯的错就是过度触发。
_t3 = _r8.call("needs", {})
check("负控：真·没传参数 ⇒ 仍走原来的必填校验（口径没被新分支吞掉）",
      "缺少必填参数" in _t3.error, True)
check("负控：真·没传参数 ⇒ 错误里**不**提 JSON（新分支确实没被触发）",
      "不是合法 JSON" in _t3.error, False)
_t4 = _r8.call("noarg", {})
check("负控：真·没传参数且无必填 ⇒ 照常执行（回归护栏）", _SIDE, ["side-effect"])

print("\n── 9. `read_file`：内核一直缺的**读基元**（2026-09-21 实跑抓到）──")
# 为什么单开一节：真实任务里材料是 `.md`，而当时挂载的 67 个工具
# **没有一个能读本地文本文件** —— `import_document` 只吃 pdf/docx
# （喂 .md 回「不支持的格式: md（仅 pdf/docx）」），`web_extract` 只吃 URL。
# 模型于是用 `export_docx` / `load_skill(file=材料)` / `web_extract` 三条歪路
# 各撞一遍，**31 次调用零进展**（`create_presentation` 0 次）。
# 修法是**补上基元**，所以这个基元自己必须有测试。
import os as _os
from rongke.loop import _make_read_file      # noqa: E402

_rf = _make_read_file()          # ← 直接就是执行体（不是 Tool 对象）
_D9 = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_readfile", "read-file")
(_D9 / "素材.md").write_text("# 标题\n\n正文第一段。\n", encoding="utf-8")
(_D9 / "旁证.txt").write_text("另一份材料", encoding="utf-8")
(_D9 / "gbk.txt").write_bytes("中文 GBK".encode("gbk"))
(_D9 / "bin.dat").write_bytes(b"\x00\x01\x02\xff\xfe\x00")
(_D9 / "sub").mkdir()

_prev9 = Path.cwd()
_os.chdir(_D9)          # ★ cwd 就是"工作区" —— 围栏的判据就是它，必须自己摆对
try:
    _out = _rf("素材.md")
    check("9.1 读到正文", "正文第一段" in _out, True)
    check("9.2 带出处与体量（模型能据以引用）",
          ("素材.md" in _out) and ("字节" in _out), True)
    # ★★ 这条治"猜路径"（实测连猜 58 次）：找不到时**列出真实邻居**
    try:
        _rf("材料区/素材.docx")
        _e_miss = ""
    except Exception as e:
        _e_miss = str(e)
    check("9.3 ★ 文件不存在 ⇒ 报错，不静默", bool(_e_miss), True)
    check("9.4 ★★ 报错里**列出工作区真实有的名字**（治猜路径）",
          ("旁证.txt" in _e_miss) and ("素材.md" in _e_miss), True)
    check("9.5 ★ 报错里点明「不要再换后缀或换目录去猜」",
          "不要再换后缀" in _e_miss, True)
    # ── 围栏：读工具**豁免注册表的围栏**（那里只管 mutating）⇒ 围栏必须写在这里，
    #    否则 `read_file("../../…")` 就是一条任意读取通道。
    try:
        _rf("../../../Windows/win.ini")
        _e_out = ""
    except Exception as e:
        _e_out = str(e)
    check("9.6 ★ 越界（`../` 绕出去）被拒", "越界" in _e_out, True)
    check("9.7 ★ 越界报错里点名工作区在哪（可诊断）", str(_D9) in _e_out, True)
    check("9.8 绝对路径但**在工作区内** ⇒ 允许（判的是位置，不是写法）",
          "正文第一段" in _rf(str(_D9 / "素材.md")), True)
    # ── 编码与二进制 ──
    check("9.9 GBK 文本也能读（中文环境常见）",
          "中文 GBK" in _rf("gbk.txt"), True)
    try:
        _rf("bin.dat")
        _e_bin = ""
    except Exception as e:
        _e_bin = str(e)
    check("9.10 ★ 二进制 ⇒ 明确拒绝，并**指向该用的那个工具**",
          ("import_document" in _e_bin) and bool(_e_bin), True)
    # ── 截断必须诚实（M 型防线：截了就说截了，并给出全文长度）──
    (_D9 / "long.md").write_text("行\n" * 5000, encoding="utf-8")
    _lt = _rf("long.md", max_chars=300)
    check("9.11 ★ 超长 ⇒ 截断并**说清全文多少字符**", "已截断" in _lt, True)
    check("9.12 长度真的被限住（不骗人）", len(_lt) < 900, True)
    check("9.13 目录 ⇒ 列内容而不是报错", "是**目录**" in _rf("sub"), True)
    try:
        _rf("")
        _e_emp = ""
    except Exception as e:
        _e_emp = str(e)
    check("9.14 空路径 ⇒ 明确拒绝", "不能为空" in _e_emp, True)
finally:
    _os.chdir(_prev9)
T.stash(_D9.parent, "read-file")

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
