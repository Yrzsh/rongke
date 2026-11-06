# -*- coding: utf-8 -*-
"""导师模式常驻回归（W22-B 的一次性守卫 → W25 起进 regress）。

W22 的守卫（`00_总控/_w22/w22b_check.py`）是一次性脚本：它带着当天的文件
指纹基线，之后任何正当改动都会把它撞红（实测：ui_check.py 演进后 19/20）。
⇒ 导师模式的内核逻辑**必须有一套不依赖指纹、跟着行为走**的常驻测试。
本文件就是它。覆盖：

  1. 开关链（W22 §4）    —— 开态注入 / 关态一个字不出现（两个方向都测）；
                            提示词点名的工具与判据元组**单源**（H 型防护）。
  2. mentor_kb_hits      —— 两代载荷形状（hits[{doc_id,page}] / refs[{id,page}]）；
                            ok=False 与非 JSON 回包如实记账（不许静默吞）。
  3. 三条出处判据        —— 正控 / 负控 / W22 那条「空库没标注≠违规」的修正判据，
                            每条都有判别力（判据反过来教人造假的那种要单测）。
  4. ★ P3 本地复算层     —— library_ids 给了：编造的 id 判 `doc_not_in_library`
                            （加重情节）；库里有但本次没命中判 `doc_not_in_hits`；
                            None = 如实跳过该层。`kb_library_ids` 拿不全就回 None
                            （不许拿空集冒充全库 —— 那会把每处出处都误判成编的）。
  5. ★ 挂载不变量（W25） —— needs_kb ⇒ kb_* 与 topic_* 必在 allow 里；
                            导师开关开着 ⇒ 同样必在（提示词点了名，挂载不给 =
                            假开关）；纯写作任务**不挂**；领域匹配不上 ⇒ 退回
                            不裁（宁可多给，也别静默拿走）。
  6. READING/EVIDENCE 引导 —— 有课题才有（不注入空壳）；没课题连提示都不出现。
  7. contract.needs_kb   —— 自报或关键词命中 / 回环 / kickoff 段落只在该出现时出现。

一条刻意的记法（与 memory_test 同）：**每个断言都成对出现**。
只有正控 = 可能报的是别的原因；只有负控 = 这条检查根本没在干活。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import topic as T                              # noqa: E402
from rongke.contract import TaskContract, DRAFT_SYSTEM     # noqa: E402
from rongke.loop import (                                  # noqa: E402
    ToolCall, initial_messages, kb_library_ids, mentor_exit_text,
    mentor_kb_hits, mentor_mode_text, mentor_trace_audit,
    MENTOR_KB_TOOLS, MENTOR_NET_TOOLS, MENTOR_MARK, MENTOR_OFF_MARK,
    mounting_decision, _KB_DOMAIN, _TOPIC_DOMAIN, _KB_STAGES,
)
from rongke.tools import Tool, ToolRegistry                # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def check_true(label, got):
    check(label, bool(got), True)


def mk_calls(rows):
    """造 ToolCall 台账。rows: (name, ok, result_dict_or_str) 三元组列表。"""
    out = []
    for i, (name, okc, result) in enumerate(rows, 1):
        out.append(ToolCall(
            seq=i, id=f"c{i}", name=name, args={}, ok=okc, ms=1,
            result=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)))
    return out


KB_HIT = {"ok": True, "hits": [{"doc_id": "kb111aaaa", "page": 3, "title": "T"}]}
ANS_GOOD = "结论A〔库·kb111aaaa p.3〕，另有外网补充〔外网·OpenAlex〕。"

print("═══ 导师模式常驻回归（mentor_test）═══")

# ═══ 1. 开关链：开态注入 / 关态一个字不出现 ═══════════════════
print("\n── 1. 开关链（提示词注入的两个方向）──")
_c_writing = TaskContract(raw_goal="写一段两百字的引言，主题是催化剂", stages=["draft"])
_off = initial_messages(_c_writing, kb_only=False)
_on = initial_messages(_c_writing, kb_only=True)
check_true("关态：整份消息栈里**不出现**导师模式标记（负控）",
           all(MENTOR_MARK not in str(m.get("content") or "") for m in _off))
check_true("开态：第一条 user 消息里有导师模式标记（正控）",
           MENTOR_MARK in str(_on[1].get("content") or ""))
check("开态：标记恰好出现一次（不重复注入）",
      str(_on[1].get("content") or "").count(MENTOR_MARK), 1)
# 承重自检：两态必须真的不同 —— 注入逻辑若恒真，上面两条就是摆设
check_true("开/关两态的 user 消息**确实不同**", _on[1]["content"] != _off[1]["content"])

# 单源：提示词点名的工具名必须与判据元组同源（H 型防护 —— W22 在 loop.py 里
# 写明了这条纪律，这里把它钉成测试）
_mt = mentor_mode_text()
for _n in MENTOR_KB_TOOLS + MENTOR_NET_TOOLS:
    check_true(f"提示词点名 `{_n}`（与判据元组单源）", f"`{_n}`" in _mt)
check_true("退出语带关闭标记", MENTOR_OFF_MARK in mentor_exit_text())
check_true("退出语明确说纪律不再生效", "不再生效" in mentor_exit_text())

# ═══ 2. mentor_kb_hits：两代载荷形状 + 如实记账 ═══════════════
print("\n── 2. mentor_kb_hits（载荷形状与记账）──")
_h = mentor_kb_hits(mk_calls([
    ("kb_search", True, {"ok": True, "hits": [{"doc_id": "kb111aaaa", "page": 3}]}),
    ("kb_ask", True, {"ok": True, "refs": [{"id": "kb222bbbb", "page": 7}]}),
]))
check("kb_search 的 hits 形状能取到", ("kb111aaaa", 3) in set(map(tuple, _h["pairs"])), True)
check("kb_ask 的 refs 形状也能取到（只认一种 = 误伤另一种的出处）",
      ("kb222bbbb", 7) in set(map(tuple, _h["pairs"])), True)
_h2 = mentor_kb_hits(mk_calls([
    ("kb_search", False, {"ok": True, "hits": [{"doc_id": "kbX", "page": 1}]}),
    ("kb_search", True, "这不是JSON"),
    ("kb_search", True, ["也不是dict"]),
]))
check("ok=False 的调用不进命中集", _h2["docs"], [])
check("非 JSON 回包如实记 unparsed（不许静默吞）", _h2["unparsed"], 2)
check("kb_ok_calls 只数成功的", _h2["kb_ok_calls"], 2)

# ═══ 3. 三条出处判据：正控 / 负控 / 修正判据 ══════════════════
print("\n── 3. 三条出处判据 ──")
_calls_hit = mk_calls([("kb_search", True, KB_HIT)])
_calls_none = mk_calls([("kb_search", True, {"ok": True, "hits": []})])
_net = mk_calls([("literature_search", True, {"ok": True})])

# ★ 三态复核器（2026-11-04）：不给 library_ids ⇒ 「全库查无此 id」这层
#   根本**没测**，verdict 必须是 unknown，不许把 unknown 报成 pass。
#   正控要模拟真实产品路径（收尾复核现在真传 kb_library_ids()），
#   否则「查库+标注齐全」这条正控反而是在量一个不完整的复核器。
_good = mentor_trace_audit(_calls_hit + _net, ANS_GOOD,
                           library_ids={"kb111aaaa", "kb222bbbb"})
check("正控：查库+标注+外网标注齐全 ⇒ pass", _good["pass"], True)
check("正控：违规清单为空", _good["violations"], [])
check("★ 负控：不给 library_ids ⇒ 如实回 unknown，不装作判全了",
      mentor_trace_audit(_calls_hit + _net, ANS_GOOD)["verdict"], "unknown")

_j1 = mentor_trace_audit(_net, "结论〔外网·OpenAlex〕")
check("① 开着导师模式一次库都没查 ⇒ 违规", _j1["judgments"]["must_query_library"], False)

_j2a = mentor_trace_audit(_calls_hit, "一段没有任何出处标记的话。")
check("② 有命中却一处不标 ⇒ 违规（用了不说=没依据）", _j2a["judgments"]["citations_ground"], False)

_j2b = mentor_trace_audit(_calls_none, "你的文献库里没有相关的东西，要不要联网补？")
check("②★ 空库 + 没标库内出处 ⇒ **放行**（W22 修正：判据不许逼人编造）",
      _j2b["judgments"]["citations_ground"], True)
check("②★ 空库事实要摆到 note 里（§3.4）", "没有命中任何段落" in _j2b["note"], True)

_j2c = mentor_trace_audit(_calls_hit, "结论〔库·kb999zzzz p.3〕")
check("② 标了库里没有的 doc ⇒ doc_not_in_hits", _j2c["citations"][0]["verdict"], "doc_not_in_hits")
_j2d = mentor_trace_audit(_calls_hit, "结论〔库·kb111aaaa p.9〕")
check("② 篇在命中里但这页没有 ⇒ page_not_hit", _j2d["citations"][0]["verdict"], "page_not_hit")

# ── ★ W27：`kb_read` 读过的页也算页码级依据 ────────────────────
# 真实事故（W27 第二轮端到端）：模型 `kb_search` 命中第 4 页、再 `kb_read`
# 读 4–6 页、然后引第 5 页 —— 内核判 `page_not_hit`，112 处标注里 11 处被误判。
# 被罚的**恰是读原文读得最认真**的那些页，判据方向反了。
print("\n   ★ W27：kb_read 的页必须算依据（否则罚的是最认真的做法）")
_rd = mk_calls([
    ("kb_search", True, {"ok": True, "hits": [{"doc_id": "kb111aaaa", "page": 4}]}),
    ("kb_read", True, {"ok": True, "doc_id": "kb111aaaa", "page_range": [4, 6],
                       "text": "[p.4] 甲\n[p.5] 乙\n[p.6] 丙"}),
])
_hr = mentor_kb_hits(_rd)
check_true("search_pairs 只含检索命中的那一页",
           (("kb111aaaa", 4) in set(_hr["search_pairs"])
            and ("kb111aaaa", 5) not in set(_hr["search_pairs"])))
check_true("★ read_pairs 覆盖 kb_read 的整个 page_range（4–6 页）",
           {("kb111aaaa", p) for p in (4, 5, 6)} <= set(_hr["read_pairs"]))
check("read_calls 如实计数", _hr["read_calls"], 1)

# 正控：引第 5 页（**只被 kb_read 覆盖**）必须判 grounded
_rd2 = mentor_trace_audit(_rd, "结论〔库·kb111aaaa p.5〕")
check("★ 正向：kb_read 读过的第 5 页 ⇒ grounded（W27 修正的核心）",
      _rd2["citations"][0]["verdict"], "grounded")
check_true("★ 该处不再进违规清单", not _rd2["violations"])
# 同一次运行的第 3 页**没读到**，仍必须判 page_not_hit —— 证明不是放宽
_rd3 = mentor_trace_audit(_rd, "结论〔库·kb111aaaa p.3〕")
check("负控：同一篇里**没读到**的第 3 页仍判 page_not_hit（没有放宽）",
      _rd3["citations"][0]["verdict"], "page_not_hit")
# page_range 缺失时靠正文 [p.N] 标记兜底
_rd4 = mentor_trace_audit(
    mk_calls([("kb_read", True, {"ok": True, "doc_id": "kb111aaaa",
                                 "text": "[p.7] 甲\n[p.8] 乙"})]),
    "结论〔库·kb111aaaa p.8〕")
check("★ page_range 缺失 ⇒ 从正文 [p.N] 标记取（第二来源）",
      _rd4["citations"][0]["verdict"], "grounded")
# 没读过、也没检索到 ⇒ 仍然拦得住编造
_rd5 = mentor_trace_audit(
    mk_calls([("kb_read", True, {"ok": True, "doc_id": "kb111aaaa",
                                 "text": "[p.1] 甲"})]),
    "结论〔库·kb111aaaa p.42〕")
check("负控：既没检索到也没读到 ⇒ 仍判 page_not_hit（编造拦得住）",
      _rd5["citations"][0]["verdict"], "page_not_hit")
check("审计如实上报两种依据的条数",
      (_rd2["search_pairs"] >= 1, _rd2["read_pairs"] >= 3), (True, True))

_j3a = mentor_trace_audit(_calls_hit + _net, "结论〔库·kb111aaaa p.3〕，联网补的说法。")
check("③ 联网了却没标〔外网·…〕⇒ 违规", _j3a["judgments"]["external_marked"], False)
_j3b = mentor_trace_audit(_calls_hit, "正文标了〔外网·OpenAlex〕但没联网。")
check("③ 标了〔外网·…〕却没联网 ⇒ 违规（双向）", _j3b["judgments"]["external_marked"], False)

# ═══ 4. ★ P3 本地复算层（library_ids）════════════════════════
print("\n── 4. P3 本地复算层 ──")
_fab = mentor_trace_audit(_calls_hit, "结论B〔库·kb888fake p.1〕〔库·kb222bbbb p.5〕",
                          library_ids={"kb111aaaa", "kb222bbbb"})
_vs = {c["doc_id"]: c["verdict"] for c in _fab["citations"]}
check("库里有但本次没命中 ⇒ doc_not_in_hits（不是编造档）",
      _vs["kb222bbbb"], "doc_not_in_hits")
check("全库查无此 id ⇒ doc_not_in_library（P3 本地可复算）",
      _vs["kb888fake"], "doc_not_in_library")
check_true("加重情节进了违规清单（措辞必须与『本次没命中』分开）",
           any("全库查无此 id" in v for v in _fab["violations"]))
check("library_ids_known 如实上报", _fab["library_ids_known"], True)

_old = mentor_trace_audit(_calls_hit, "结论B〔库·kb888fake p.1〕")
check("不给 library_ids ⇒ 退回老判 doc_not_in_hits", _old["citations"][0]["verdict"], "doc_not_in_hits")
# ★ 别再绑死「没判」这一个词：note 的措辞随复核器演进（现在是「未测 /
#   未取得」），判据该量**语义**：结构出口与人话出口都得把「这层没测」说出口。
check("不给 library_ids ⇒ library_ids_known 如实为 False",
      _old["library_ids_known"], False)
check_true("不给 library_ids ⇒ 结构出口 unknown_reasons 点名未测的那层",
           any("doc_not_in_library" in u for u in _old["unknown_reasons"]))
check_true("不给 library_ids ⇒ note 也把这层没测说给人听",
           ("未测" in _old["note"]) or ("没判" in _old["note"]))

_prop = mentor_trace_audit(_calls_hit, "结论A〔库·kb111aaaa p.3〕。",
                           library_ids={"kb111aaaa"}, stages=["proposal"])
check_true("proposal 阶段：note 摆出证据表纪律", "证据表" in _prop["note"])


class _FakeRegistry:
    """假注册表：只为测 kb_library_ids 的解析与失败路径。"""

    def __init__(self, fn):
        self._fn = fn

    def get(self, name):
        if name != "kb_list":
            return None

        class _T:
            fn = staticmethod(self._fn)

        return _T()


def _kblist(items, ok=True, matched=None):
    pay = {"ok": ok, "total": len(items), "matched": len(items) if matched is None else matched,
           "showing": len(items), "items": [{"id": it} for it in items]}
    return json.dumps(pay, ensure_ascii=False)


check("kb_library_ids：正常回包取全 id 集合",
      kb_library_ids(_FakeRegistry(lambda **k: _kblist(["kb1", "kb2"]))), {"kb1", "kb2"})
check("kb_library_ids：ok=False ⇒ None（不猜）",
      kb_library_ids(_FakeRegistry(lambda **k: _kblist([], ok=False))), None)
check("kb_library_ids：matched > 实际条数 = 清单被截 ⇒ None（拿不全就明说）",
      kb_library_ids(_FakeRegistry(lambda **k: _kblist(["kb1"], matched=50))), None)
check("kb_library_ids：回包不是 JSON ⇒ None",
      kb_library_ids(_FakeRegistry(lambda **k: "error")), None)
check("kb_library_ids：没有 kb_list 工具 ⇒ None",
      kb_library_ids(_FakeRegistry(lambda **k: (_ for _ in ()).throw(RuntimeError))), None)


class _Empty:
    def get(self, name):
        return None


check("kb_library_ids：空注册表 ⇒ None", kb_library_ids(_Empty()), None)

# ═══ 5. ★ 挂载不变量（needs_kb ⇒ kb_* 必在 allow）════════════
print("\n── 5. 挂载不变量（W25）──")
# 假领域表：不动 workbench 就能测 mounting_decision 的全部判据
_DOMAINS = {
    "deliver_task": "core", "load_skill": "core", "read_file": "core",
    "literature_search": "research", "web_search": "research", "fetch_doi": "research",
    "kb_search": _KB_DOMAIN, "kb_ask": _KB_DOMAIN, "kb_add": _KB_DOMAIN,
    "kb_list": _KB_DOMAIN,
    "topic_use": _TOPIC_DOMAIN, "topic_status": _TOPIC_DOMAIN,
    "raise_blocker": _TOPIC_DOMAIN, "resolve_blocker": _TOPIC_DOMAIN,
    "topic_write": _TOPIC_DOMAIN,
    # 2026-11-02 新增的两个 topic 工具（模型侧原来没有"关联文献/改课题元数据"的手）。
    # 假装载表是**真实注册表的镜像**，新增工具必须同步进来，否则下面的
    # 「allow 里有没有它」这类断言测的是一个不存在的世界。
    "topic_link": _TOPIC_DOMAIN, "topic_update": _TOPIC_DOMAIN,
    "audit_figure": "figure", "audit_deck": "ppt",
}
_names = list(_DOMAINS)
_dof = lambda n: _DOMAINS.get(n, "")                    # noqa: E731


def _allow(m):
    return set(m["allow"] or [])


_c_plain = TaskContract(raw_goal="随便写点什么", stages=["draft"])
_m_plain = mounting_decision(_c_plain, _names, _dof)
check_true("负控：纯写作任务的域清单**不含** kb/topic",
           _KB_DOMAIN not in _m_plain["domains"] and _TOPIC_DOMAIN not in _m_plain["domains"])
check_true("负控：纯写作任务的 allow 里没有 kb_search", "kb_search" not in _allow(_m_plain))
check_true("负控：纯写作任务的 allow 里没有 topic_use", "topic_use" not in _allow(_m_plain))

_m_switch = mounting_decision(_c_plain, _names, _dof, kb_only=True)
check_true("★ 不变量：导师开关开着 ⇒ kb/topic 域在 domains 里",
           _KB_DOMAIN in _m_switch["domains"] and _TOPIC_DOMAIN in _m_switch["domains"])
check_true("★ 不变量：导师开关开着 ⇒ kb_search 必在 allow（提示词点了名）",
           "kb_search" in _allow(_m_switch))
check_true("★ 不变量：导师开关开着 ⇒ topic_use 必在 allow",
           "topic_use" in _allow(_m_switch))

_c_kb = TaskContract(raw_goal="帮我在库里查结核疫苗的段落", stages=["draft"], needs_kb=True)
_m_kb = mounting_decision(_c_kb, _names, _dof)
check_true("needs_kb 契约 ⇒ kb/topic 都挂", "kb_search" in _allow(_m_kb)
           and "topic_use" in _allow(_m_kb))

_c_stage = TaskContract(raw_goal="做个文献调研", stages=["search"])
_m_stage = mounting_decision(_c_stage, _names, _dof)
check_true("阶段含 search（文献类超集）⇒ kb 域也挂", "kb_search" in _allow(_m_stage))

_m_deck = mounting_decision(
    TaskContract(raw_goal="做组会汇报幻灯片", stages=["slides"], needs_deck=True),
    _names, _dof)
check_true("deck 任务照旧挂 ppt 域（不牵连）", "audit_deck" in _allow(_m_deck))
check_true("deck 任务不自动带 kb/topic（目标没沾文献）",
           "kb_search" not in _allow(_m_deck))

# 承重自检①：把 kb 域名换成一个不存在的域 ⇒ mounting 必须**退回不裁**（宁可多给）
_m_broken = mounting_decision(_c_kb, _names, _dof)
_global_kb = _KB_DOMAIN
import rongke.loop as _L                                   # noqa: E402
_L._KB_DOMAIN = "kb_missing_domain"
_m_broken = mounting_decision(_c_kb, _names, _dof)
_L._KB_DOMAIN = _global_kb
check("承重：kb 域一个工具都匹配不上 ⇒ allow=None（退回全给，不静默拿走）",
      _m_broken["allow"], None)

# 承重自检②：判据全关（无 needs_kb / 无开关 / 阶段表清空）⇒ kb 不挂
# （证明挂载真的来自这三条判据，不是"反正都挂"）
_L._KB_STAGES = frozenset()
_m_off = mounting_decision(_c_stage, _names, _dof)
_L._KB_STAGES = _KB_STAGES
check_true("承重：三条判据全关时 kb 域**不挂**（挂载来自判据，不是常量）",
           "kb_search" not in _allow(_m_off))

# ══ 承重自检③（2026-11-02）：第四条判据 `has_topic` ════════════
# 它补的是一个真实的不一致：`initial_messages` 开场就把「你在课题 X 上」注入
# 提示词（`topic_slug` 空则退回 `current()`），而挂载此前只看契约与阶段 ⇒
# 普通对话里「提示词说有课题、工具却不给管课题的手」，模型只能答"我做不到"。
#
# ★ 正控与承重**必须成对**：只断言"传 True 就挂"，证明不了是这条判据让它挂的
#   （本仓那句「打红面 99.6% 与 0% 是同一种失败」说的就是这种只测一面的断言）。
#   所以：同一份契约、同一组判据，只翻 `has_topic` 一个开关，结果必须翻转。
_L._KB_STAGES = frozenset()
_m_topic_on = mounting_decision(_c_stage, _names, _dof, has_topic=True)
_m_topic_off = mounting_decision(_c_stage, _names, _dof, has_topic=False)
_L._KB_STAGES = _KB_STAGES
check_true("★ 会话带着课题身份 ⇒ kb/topic 域挂上，且 allow 里真有 topic_link",
           _TOPIC_DOMAIN in _m_topic_on["domains"]
           and "kb_search" in _allow(_m_topic_on)
           and "topic_link" in _allow(_m_topic_on))
check_true("★ 承重：has_topic 真→假，同一份契约下 kb_search 必须消失"
           "（证明挂载来自这条判据，不是别处顺带挂上的）",
           "kb_search" not in _allow(_m_topic_off))
check_true("挂载理由如实说出「会话带着课题身份」（裁剪必须带理由，不许静默）",
           "课题身份" in _m_topic_on["reason"])

# ═══ 6. READING / EVIDENCE 引导：有课题才有 ═══════════════════
print("\n── 6. READING/EVIDENCE 引导（不注入空壳）──")
import tempfile                                            # noqa: E402
with tempfile.TemporaryDirectory() as _td:
    _root = Path(_td)
    check("没课题：context_text 是空串（连引导都不出现）",
          T.context_text(root=_root), "")
    T.ensure_topic("probe-r", title="读书记录课题", root=_root)
    T.set_current("probe-r", root=_root)
    _ctx = T.context_text(root=_root)
    check_true("有课题：引导点名 topic_write(doc=reading)",
               'topic_write(doc=\\"reading\\"' in _ctx.replace('\\"', '\\"')
               or "reading" in _ctx and "topic_write" in _ctx)
    check_true("有课题：引导说明 evidence 证据表锚法", "证据表" in _ctx)
    check_true("有课题：引导带锚格式（库·doc_id p.页码）", "〔库·" in _ctx)

# ═══ 7. contract.needs_kb ═════════════════════════════════════
print("\n── 7. contract.needs_kb 与 kickoff 段落 ──")
check("综述文献目标 ⇒ needs_kb=True（关键词命中）",
      TaskContract.from_llm("帮我综述一下课题相关的文献", {}).needs_kb, True)
check("纯写作目标 ⇒ needs_kb=False（负控）",
      TaskContract.from_llm("写一段两百字的引言，主题是催化剂", {}).needs_kb, False)
check("模型自报 needs_kb=True 也算数（自报或命中取并集）",
      TaskContract.from_llm("整理一下", {"needs_kb": True}).needs_kb, True)
check("fallback 路径同样判 needs_kb", TaskContract.fallback("文献调研").needs_kb, True)
_d = TaskContract.from_llm("查一下库里关于X的段落", {}).to_dict()
check("to_dict/from_dict 回环保住 needs_kb", TaskContract.from_dict(_d).needs_kb, True)
check_true("DRAFT_SYSTEM 模板里有 needs_kb（模型自报的入口）", "needs_kb" in DRAFT_SYSTEM)
_tp_kb = TaskContract(raw_goal="查一下库里关于X的段落", needs_kb=True).to_prompt()
_tp_no = TaskContract(raw_goal="写一段两百字的引言", needs_kb=False).to_prompt()
check_true("needs_kb ⇒ to_prompt 带文献库开工动作", "文献库开工动作" in _tp_kb)
check_true("无 needs_kb ⇒ 不带（负控）", "文献库开工动作" not in _tp_no)
check_true("kickoff 点名 kb_search 与 kb_read（模型执行它读到的动作）",
           "kb_search" in _tp_kb and "kb_read" in _tp_kb)
check_true("kickoff 交代『只收 PDF』（W3 预检的同一条纪律，事前说清）",
           "只收 PDF" in _tp_kb)

# ═══ 汇总 ═════════════════════════════════════════════════════
print("\n═══ 汇总 ═══")
if ok_all:
    print("  全部通过 ✔")
    sys.exit(0)
print("  有失败 ✘")
sys.exit(1)
