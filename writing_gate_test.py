# -*- coding: utf-8 -*-
"""写作技能门 + 写作体检门 + 引文核验 自测。

覆盖：
  1. 写作意图判定 —— 尤其是「报告…」不被误判（实测过的反例）
  2. 写作技能门   —— 未装载专属技能必须打回；装载后放行
  3. 引文核验     —— 撤稿 / 查无此文 / 正常 三种结局（走真实 Crossref）
  4. 参数预校验   —— 缺必填参数时报清楚
  5. 写作体检门   —— 空转稿打回、正常稿放行、最多打回一次、字数目标抽取、否定语境
  9. 引注标准由内核判定 + 内核判据**终审**（正控/负控/两组承重自检）

1、2、4、5、9 不需要 LLM，确定性。3 走真实 Crossref（需要网络）。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke.citation import _norm as cnorm, check_doi       # noqa: E402
from rongke.contract import TaskContract, _looks_like_writing  # noqa: E402
from rongke.gate import CompletionClaim, CompletionGate, EvidenceRef  # noqa: E402
from rongke.tools import Tool, ToolRegistry                # noqa: E402
from rongke.writing import lint as wlint                   # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def echo(text):
    def fn(**kw):
        return text
    return fn


def make_registry(extra=()):
    reg = ToolRegistry()
    blank = {"type": "object", "properties": {}}
    reg.register(Tool(name="dummy", description="占位",
                      parameters=blank, fn=echo("条目齐全")))
    for name in extra:
        reg.register(Tool(name=name, description="占位",
                          parameters=blank, fn=echo(f"{name} 已执行")))
    reg.start_task()
    reg.call("dummy", {})
    return reg


print("═══ 1. 写作意图判定 ═══")
cases = [
    # (目标, 期望)
    ("写一篇关于冬虫夏草的综述", True),
    ("起草论文的引言部分", True),
    ("帮我润色摘要", True),
    ("polish this manuscript", True),
    ("写一份开题报告", True),
    # ── 反例：这些**不该**触发写作门 ──
    ("报告最近 3 次工具调用分别是什么工具", False),
    ("查看台账里有哪些工具", False),
    ("核验这几篇参考文献的 DOI 是否有效", False),
    ("搜索 Ophiocordyceps sinensis 的文献至少 3 篇", False),
    ("统计一下有多少条记录", False),
]
for goal, want in cases:
    check(goal[:34], _looks_like_writing(goal), want)

print()
print("═══ 2. 写作技能门 ═══")
# ★ 这里**显式钉住 stages=["draft"]**：本节测的是「写作技能门」这一条，
#   不是阶段推断。2026-10-22 起，综述形态会额外推出 fulltext/outline/figure
#   三个阶段（那是刻意的：库够不够 → 定大纲 → 先出图 → 再写），
#   不钉住的话，本节每条断言的失败都可能来自另外三个技能没装载，
#   而不是来自它要测的写作门 —— **一节测两件事，红了分不清是谁红**。
#   阶段推断本身由 `mentor_test.py` / 下面的第 12 节专门测。
contract = TaskContract(raw_goal="写一篇综述", goal="写一篇综述",
                        acceptance=["条目齐全"], needs_writing=True,
                        # ★ 这一节测的是**写作技能门**，不是综述的文献量门。
                        #   2026-10-24 加「综述最少 50 篇」硬门之后，这条裸契约
                        #   （`raw_goal="写一篇综述"` + 短稿正文、无文献表）会被
                        #   文献量门打回，于是 11.8「负控：无 outline 阶段照常放行」
                        #   被这条**完全无关**的新门拖红。
                        #   ⚠️ 第一版我把 `target_chars` 设成 1200 想让它"变短综述"，
                        #   结果**换了个门红**：1200 字却不给字数达标，写作体检门接手了。
                        #   正确做法是让契约**形态上就不是长综述、同时不引入新约束** ——
                        #   显式声明"这不是长综述"，而不是用字数去间接表达。
                        stages=["draft"])
# 上面构造完再钉住形态：这样 §2/§11 的断言都只看各自那道门。
contract._is_review_like = False
contract._is_long_review = False


def gate_verdict(reg):
    c = CompletionClaim(deliverables=["综述稿"],
                        evidence=[EvidenceRef(tool="dummy", call_seq=1,
                                              shows="条目齐全", acceptance="条目齐全")])
    return CompletionGate(reg).check(c, contract)


reg = make_registry()
v = gate_verdict(reg)
check("未装载技能 → 不通过", v.passed, False)
check("打回理由点名写作任务", any("写作任务" in r for r in v.reasons), True)
# 注意断言的是 `load_skill`（内核自带装载器），不是工作台的 search_skills/read_skill。
# 旧断言写的是后者 —— 那是**测试陈旧**，在本次改动之前就已经是红的。
check("打回话术含强制协议", "load_skill" in v.feedback, True)

# 只做了一半：搜了没读
reg = make_registry(extra=("search_skills",))
reg.call("search_skills", {"query": "写作"})
v = gate_verdict(reg)
check("只 search 未 read → 仍不通过", v.passed, False)
# ★ W36（2026-10-31）改：缺阶段技能**不再进 reasons**，改由 warnings 提醒。
#   用户原话：「要装载技能调用工具，但不用那么复杂的证据门，比如限制多少步」——
#   装技能是行为要求，不是交付许可。上面那条"不通过"仍然成立，但拦住它的是
#   第 7 条写作体检（这里没交正文），不是技能门。
#   ⚠️ 负控：如果哪天技能门又被改回硬拦，这条会立刻变红 —— 那是退步，不是修复。
check("缺阶段技能只提醒、不进打回理由",
      any("nature-writing" in r for r in v.reasons), False)
check("缺阶段技能仍被点名（进 warnings，人看得见）",
      any("nature-writing" in w for w in v.warnings), True)

# 两步都做（技能的装载器认 load_skill；写作任务还必须附正文，见第 5 节）
GOOD_DRAFT = ("冬虫夏草（Ophiocordyceps sinensis）的完整生活史依赖蝙蝠蛾科幼虫与侵染期"
              "真菌的精确耦合，这一耦合在离体条件下极难重现。困难集中在三处：寄主昆虫"
              "规模化饲养的存活率长期偏低，侵染期菌丝活力在离体培养中迅速衰减，子实体"
              "诱导至今缺乏可重复的环境参数。近五年虽有零星室内出草报道，产量仍普遍低于"
              "野外采集一个数量级。")
reg = make_registry(extra=("search_skills", "load_skill"))
reg.call("search_skills", {"query": "写作"})
reg.call("load_skill", {"name": "nature-writing"})
c = CompletionClaim(deliverables=["综述稿"],
                    evidence=[EvidenceRef(tool="dummy", call_seq=1,
                                          shows="条目齐全", acceptance="条目齐全")],
                    draft_text=GOOD_DRAFT)
check("已装载技能 + 附正文 → 通过", CompletionGate(reg).check(c, contract).passed, True)

# 非写作任务不受门约束
plain = TaskContract(raw_goal="查台账", goal="查台账",
                     acceptance=["条目齐全"], needs_writing=False)
c = CompletionClaim(evidence=[EvidenceRef(tool="dummy", call_seq=1,
                                          shows="条目齐全", acceptance="条目齐全")])
check("非写作任务 → 不卡技能", CompletionGate(make_registry()).check(c, plain).passed, True)

print()
print("═══ 3. 引文核验（真实 Crossref）═══")
r = check_doi("10.1016/S0140-6736(97)11096-0")           # Wakefield 1998，已撤稿
check("撤稿文献 → exists", r["exists"], True)
check("撤稿文献 → retracted", r["retracted"], True)
check("撤稿来源可溯源", bool(r.get("retraction", {}).get("source")), True)
print(f"         verdict = {r['verdict']}")

r = check_doi("10.1038/nature12373")                      # 正常对照
check("正常文献 → exists", r["exists"], True)
check("正常文献 → 未撤稿", r["retracted"], False)
check("正常文献 → 元数据一致", r["metadata_mismatch"], [])

r = check_doi("10.9999/this-doi-does-not-exist-rongke")   # 编造的
check("查无此文 → exists", r["exists"], False)
check("查无此文 → 不误报网络错误", r["error"], "")
check("查无此文 → 判为可疑", "不存在" in r["verdict"], True)

r = check_doi("10.1038/nature12373", expect_title="A completely different paper about unrelated topics",
              expect_year="1999")
check("元数据不符 → 被检出", len(r["metadata_mismatch"]) >= 2, True)
check("元数据不符 → 判定", "不一致" in r["verdict"], True)

# 中文元数据：_norm 必须保留汉字（否则比对静默失效 —— 本轮抓到的真实缺陷）
check("规范化保留中文", cnorm("冬虫夏草的研究"), "冬虫夏草的研究")
check("规范化保留中英混排", cnorm("Zhang 三, 李四"), "zhang三李四")

# 中英形式不同：不误报，但如实说明
r = check_doi("10.1038/nature12373", expect_title="完全不同的另一个标题")
check("中英标题不同 → 不误报", r["metadata_mismatch"], [])
check("中英标题不同 → 有说明", any("形式" in n for n in r.get("notes", [])), True)

r = check_doi("10.1038/nature12373", expect_authors="张三, 李四")
check("中英作者不同 → 不误报", r["metadata_mismatch"], [])
check("中英作者不同 → 有说明", any("姓名形式" in n for n in r.get("notes", [])), True)

print()
print("═══ 4. 参数预校验 ═══")
reg = ToolRegistry()
reg.register(Tool(name="needs_two", description="占位",
                  parameters={"type": "object",
                              "properties": {"skill": {"type": "string"},
                                             "project": {"type": "string"}},
                              "required": ["skill", "project"]},
                  fn=echo("ok")))
reg.start_task()
c = reg.call("needs_two", {})
check("空参数 → 未执行", c.ok, False)
check("报出缺失的两个", "skill" in c.error and "project" in c.error, True)
check("给出正确用法", "本工具接受" in c.error, True)
c = reg.call("needs_two", {"skill": "s", "project": "p"})
check("补齐后 → 正常执行", c.ok, True)

print()
print("═══ 5. 写作体检门（本轮新增）═══")
# 目的：核对「前 6 条只核流程，这条才看文本」。用 stages=["draft"] 把阶段门先喂饱，
# 这样每个断言失败都只可能来自写作体检，不会和技能门互相遮盖。
W = TaskContract(raw_goal="写一段约120字的引言", goal="写引言",
                 acceptance=["条目齐全"], needs_writing=True,
                 stages=["draft"], target_chars=120)

# 纯空转稿：通篇套话，没有一个具体事实
VACUOUS = ("随着冬虫夏草产业的飞速发展，人工培育技术受到了广泛关注。众所周知，人工培育"
           "具有十分重要的意义。近年来，越来越多的研究者取得了丰硕的成果，相关部门"
           "发挥着重要的作用。综上所述，该领域备受关注，值得我们深入探讨。")


def wreg():
    r = make_registry(extra=("load_skill",))
    r.call("load_skill", {"name": "nature-writing"})
    return r


def wclaim(**kw):
    return CompletionClaim(
        deliverables=["引言稿"],
        evidence=[EvidenceRef(tool="dummy", call_seq=1,
                              shows="条目齐全", acceptance="条目齐全")], **kw)


# （1）写作任务不带正文 → 打回（这是流程层的补充：没有正文就无处体检）
v = CompletionGate(wreg()).check(wclaim(), W)
check("写作任务缺正文 → 打回", v.passed, False)
check("理由点名 draft_text", any("draft_text" in r for r in v.reasons), True)

# （2）好稿 → 放行（**最关键的反向断言**：体检器不能误伤正常文风）
v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), W)
check("正常引言 → 放行", v.passed, True)
check("通过时带体检摘要", (v.lint or {}).get("score", 0) >= 80, True)

# （3）空转稿 → 打回，且回灌**逐条可改清单**（不是泛泛"提高质量"）
g = CompletionGate(wreg())
v1 = g.check(wclaim(draft_text=VACUOUS), W)
check("空转稿 → 打回", v1.passed, False)
check("回灌可改清单", "改法" in v1.feedback, True)
check("点名套话问题", "套话" in v1.feedback, True)
check("带结构化体检结果", (v1.lint or {}).get("hard", 0) >= 1, True)

# （4）同一 gate 再交一次 → 放行（**最多打回一次**，防止无限拉锯把稿子烧成十轮）
v2 = g.check(wclaim(draft_text=VACUOUS), W)
check("二次交付 → 放行（防无限拉锯）", v2.passed, True)
check("放行但保留告警", any("体检" in w for w in v2.warnings), True)

# （5）字数目标：从用户原话抽取，不采信模型自报
check("抽约120字", TaskContract(raw_goal="写一段约120字的引言").target_chars, 120)
check("抽区间取中值", TaskContract(raw_goal="写 300-500 字的摘要").target_chars, 400)
check("抽中文数字", TaskContract(raw_goal="写约一万字的综述").target_chars, 10000)
check("抽不到则留空", TaskContract(raw_goal="核验 DOI").target_chars, None)

# （6）否定语境不得触发阶段（否则门会逼模型去做用户禁止的事）
_neg = TaskContract(raw_goal="写引言，不需要检索文献", needs_writing=True)
check("不需要检索文献 → 不推 search 阶段", "search" in _neg.stages, False)
check("但仍是写作任务", _neg.needs_writing, True)

# （7）字数验收由**内核测量**满足，不比对模型措辞。
#     这是本轮端到端实测抓到的真缺陷：一次 119 字 / 偏差 -1% / 体检 100/100 的
#     正确交付，因为模型证据里没写「字数约120字」这几个字，被判"无证据支撑"并打回，
#     逼它多跑一整轮。内核明明已经数出来了 —— 能算的就不许靠匹配。
W2 = TaskContract(raw_goal="写一段约120字的引言", needs_writing=True,
                  stages=["draft"], acceptance=["字数约120字"], target_chars=120)
g2 = CompletionGate(wreg())
v = g2.check(CompletionClaim(
        deliverables=["引言稿"],
        evidence=[EvidenceRef(tool="dummy", call_seq=1,
                              shows="按 nature-writing 协议起草完成", acceptance="")],
        draft_text=GOOD_DRAFT), W2)
check("字数达标但措辞未提字数 → 不误伤", v.passed, True)
check("内核给出测量依据",
      "内核体检已达标" in g2._kernel_verdict("字数约120字", wlint(GOOD_DRAFT, 120))[1], True)
check("三态：达标 → pass",
      g2._kernel_verdict("字数约120字", wlint(GOOD_DRAFT, 120))[0], "pass")

# 反向：字数真不达标时，不能因为"有体检兜底"就放行
v = CompletionGate(wreg()).check(CompletionClaim(
        deliverables=["引言稿"],
        evidence=[EvidenceRef(tool="dummy", call_seq=1, shows="起草完成", acceptance="")],
        draft_text="冬虫夏草人工培育困难。"), W2)
check("字数真不达标 → 仍打回", v.passed, False)

# 没有目标字数时，内核不假装判得了（交回措辞匹配，不越权放行）
W3 = TaskContract(raw_goal="写一段引言", needs_writing=True,
                  stages=["draft"], acceptance=["内容覆盖三类难点"], target_chars=None)
v = CompletionGate(wreg()).check(CompletionClaim(
        deliverables=["引言稿"],
        evidence=[EvidenceRef(tool="dummy", call_seq=1,
                              shows="无关措辞", acceptance="")],
        draft_text=GOOD_DRAFT), W3)
check("无目标字数时不越权放行", v.passed, False)

# （8）新增的机查项必须能**拦到交付上** —— 体检器报出来 ≠ 门会拦。
#     这条断言的是端到端效果，不是 lint 的返回值：两个模块之间还有一层契约。
W4 = TaskContract(raw_goal="写一段引言", needs_writing=True,
                  stages=["draft"], acceptance=["条目齐全"], target_chars=None)

CITE_BAD = ("冬虫夏草的人工培育目前卡在三处。寄主幼虫的规模化饲养存活率长期低于三成[1]，"
            "侵染窗口极窄、错过春季两周便难以定殖，田间重复率不足两成[3]，"
            "子座分化依赖低温诱导、控温成本占到总投入六成以上[2]。"
            "近五年的室内出草报道，产量仍普遍低于野外采集一个数量级。")

# 先钉死「这份稿面**只有**引注一项硬问题」——否则下面"被打回"可能来自别的原因，
# 正控就成了空的（这正是纪律第 30 条：只有负控的自检等于没测）。
check("引注逆序稿面只有 cite_order 一项硬问题",
      [i.code for i in wlint(CITE_BAD).hard()], ["cite_order"])
v = CompletionGate(wreg()).check(wclaim(draft_text=CITE_BAD), W4)
check("引注逆序 → 交付被打回", v.passed, False)
check("打回理由点名写作体检", any("写作体检" in r for r in v.reasons), True)
check("回灌带纪律三件事改法", "重排参考文献表" in v.feedback, True)

PH_BAD = ("冬虫夏草的人工培育目前卡在三处：寄主幼虫的规模化饲养[TODO]尚未解决，"
          "侵染窗口极窄，子座分化所需的菌种退化机理待补充，控温成本占到总投入六成以上。")
check("占位符稿面只有 placeholder 一项硬问题",
      [i.code for i in wlint(PH_BAD).hard()], ["placeholder"])
v = CompletionGate(wreg()).check(wclaim(draft_text=PH_BAD), W4)
check("占位符残留 → 交付被打回", v.passed, False)
check("回灌要求如实写未核验状态", "未经核验" in v.feedback, True)

print()
print("═══ 9. 引注标准由内核判定 + 内核判据终审 ═══")
# ── 事故原样（真实运行 20260919-092658）──────────────────────────────
# 验收标准「全文不含任何文献引用（无引用标记、无参考文献列表）」，
# 草稿**一个引用标记都没有**、体检报告白纸黑字 `citations: 0` ——
# 证据门却判「以下验收标准没有任何证据支撑」并打回。
# 模型照着这句话去"找证据"，多跑 4 步、多烧 57,876 输入 token（68,974 → 126,850），
# 最后靠**把这条标准的原文抄进 evidence.acceptance** 才放行。
# 两个缺陷叠在一起，都在这一节里钉住：
#   ① 内核**数得出来**的标准却退化成比对措辞（误报 → 逼模型白跑一整轮）
#   ② 内核**量出来的事实**可以被被审者一句话推翻（自证可以上诉）
CITE_CRIT = "全文不含任何文献引用（无引用标记、无参考文献列表）"
W5 = TaskContract(raw_goal="写一段约120字的摘要，不要引用任何文献", needs_writing=True,
                  stages=["draft"], acceptance=[CITE_CRIT], target_chars=120)


def gate_claim(draft, acc_decl="", shows="条目齐全"):
    return CompletionClaim(
        deliverables=["摘要稿"],
        evidence=[EvidenceRef(tool="dummy", call_seq=1,
                              shows=shows, acceptance=acc_decl)],
        draft_text=draft)


# ① 正控：干净稿 + 内核判得了 → **不许再误打回**（事故里被打回的就是这一种）
v = CompletionGate(wreg()).check(gate_claim(GOOD_DRAFT), W5)
check("干净稿 + 「无引用」标准 → 内核数得出来，不再误打回", v.passed, True)
check("内核给出了它自己量到的依据",
      CompletionGate(wreg())._kernel_verdict(CITE_CRIT, wlint(GOOD_DRAFT, 120))[0], "pass")

# ② 正控：**内核实测不达标 + 模型复述标准原文** → 仍必须打回（自证不许上诉）
CITE_DIRTY = GOOD_DRAFT + "[1]"
check("前提复核：该稿确有引用标记", wlint(CITE_DIRTY, 120).metrics["citations"], 1)
v = CompletionGate(wreg()).check(gate_claim(CITE_DIRTY, CITE_CRIT), W5)
check("带引用标记 + 模型复述标准原文 → 仍打回（内核终审）", v.passed, False)
check("理由写的是「内核实测未达标」",
      any("内核实测未达标" in r for r in v.reasons), True)
check("不再把它误报成「没有任何证据支撑」",
      any("没有任何证据支撑" in r for r in v.reasons), False)

# ③ 正控：字数同理。实测 +22.5%、超容差 ±15%，模型声明「字数约120字」也翻不了案。
#    （挑在这个区间是刻意的：>15% 才会被内核判否，<40% 才不会触发 lint 的 length_far
#      —— 否则打回就变成 lint 干的，这条断言就不是在测内核终审了。）
LONG_DIRTY = GOOD_DRAFT + "此外，菌种继代稳定性同样值得关注。"
_lr = wlint(LONG_DIRTY, 120)
check("前提复核：超容差 ±15% 但**没有别的硬问题**",
      (abs(_lr.metrics["chars_delta_pct"]) > 15 and not _lr.hard()), True)
v = CompletionGate(wreg()).check(gate_claim(LONG_DIRTY, "字数约120字"), W2)
check("字数实测超标 + 模型声明达标 → 仍打回", v.passed, False)
check("理由写的是「内核实测未达标」",
      any("内核实测未达标" in r for r in v.reasons), True)

# ④ 负控：检测器**不许越权**。谈引用的「质量 / 格式 / 条数 / 页码」的标准，
#    内核一律交回措辞匹配 —— 判错了会把一篇带规范引用的好稿硬门打回。
_g5 = CompletionGate(wreg())
for _c in ("不得引用未经核实的文献", "全文不得引用未经核实的文献", "参考文献格式需统一",
           "文中引用需标注页码", "引用文献不超过 10 条", "需列出完整的参考文献表",
           "内容覆盖三类难点"):
    check(f"负控：{_c} → 内核不越权", _g5._kernel_verdict(_c, wlint(GOOD_DRAFT, 120))[0], "unknown")

# ⑤ 负控：只有文末文献表、正文零标记 —— `citations == 0` **不等于**「无参考文献表」。
#    拿它去证「无文献表」就是超范围断言（L 型：声明 ≠ 事实）。
REFS_ONLY = GOOD_DRAFT + "\n参考文献\n张三. 冬虫夏草研究进展[J]. 菌物学报, 2020, 39(4): 1-8."
_ro = wlint(REFS_ONLY, 120)
check("前提复核：该稿 citations==0 但 has_refs==1",
      (_ro.metrics["citations"] == 0 and _ro.metrics["has_refs"] == 1), True)
check("只有文末文献表 → 判 fail，不拿 citations==0 冒充「无文献表」",
      _g5._kernel_verdict(CITE_CRIT, _ro)[0], "fail")

# ⑥ 承重自检 A：把引注判据换成**永不匹配**的正则，正控①必须当场失守。
#    （只有负控的自检等于没测 —— 必须有一条"拿掉判据、正控就垮"的断言。）
import rongke.gate as _g  # noqa: E402
_saved_neg = _g._ACC_CITE_NEG
_g._ACC_CITE_NEG = re.compile(r"(?!)")
try:
    check("承重自检：拿掉引注判据后，正控①当场失守",
          CompletionGate(wreg()).check(gate_claim(GOOD_DRAFT), W5).passed, False)
finally:
    _g._ACC_CITE_NEG = _saved_neg

# ⑦ 承重自检 B：把内核判据整个停掉，「内核实测未达标」必须当场消失，
#    而且**旧漏洞原样复发**（复述标准原文即放行）。
#    这条同时证明②的拦截确实来自"内核终审"，而不是别的什么在报。
_orig_kv = CompletionGate._kernel_verdict
CompletionGate._kernel_verdict = lambda self, acc, rep: (_g.KERNEL_UNKNOWN, "")
try:
    v = CompletionGate(wreg()).check(gate_claim(CITE_DIRTY, CITE_CRIT), W5)
    check("承重自检：内核判据一停，「内核实测未达标」立刻消失",
          any("内核实测未达标" in r for r in v.reasons), False)
    check("承重自检：内核判据一停，旧漏洞原样复发（复述标准原文即放行）", v.passed, True)
finally:
    CompletionGate._kernel_verdict = _orig_kv
check("承重自检后判据已还原", _g._ACC_CITE_NEG is _saved_neg, True)

# ══════════════════ 10. 禁用工具契约：证据门「不适用」的边界 ══════════════════
# 2026-09-29 实测逼出来的：「不要调用任何工具」的任务，模型如实遵守契约，
# 却在交付时凑不出证据门要的 call_seq —— 契约与门禁互斥，只能报告阻塞。
# 口径：1/4/6 不适用；2/3 照常（编造照样拦）；内核实测标准照常终审。
print("\n═══ 10. 禁用工具契约（no_tools）：门的边界 ═══")

NT = TaskContract(raw_goal="请只回一句话：在线确认。不要调用任何工具，不要检索。")
check("10.1 判定命中（「不要调用任何工具」）", NT.no_tools, True)
check("10.2 「不需要检索文献」**不**算禁用工具（只否定了检索这一类）",
      TaskContract(raw_goal="写一段约120字的引言。不需要检索文献。").no_tools, False)
check("10.3 图任务不适用（做图必须动工具）",
      TaskContract(raw_goal="不要调用任何工具，画一张图", needs_figure=True).no_tools, False)

# 零证据 + 禁用工具 ⇒ 放行（这正是那次被卡死的任务形状）
_nt_claim = CompletionClaim(summary="在线确认。", deliverables=["一句确认"],
                            evidence=[], draft_text="")
_v_nt = CompletionGate(make_registry(), strict_coverage=True).check(_nt_claim, NT)
check("★ 10.4 禁用工具 + 零证据 ⇒ 交付放行（契约与门不再互斥）", _v_nt.passed, True)

# 编造证据 ⇒ 照样拦。make_registry 里 dummy 真跑过一次（seq=1），
# 所以这里编的是**不存在**的 seq —— 编造的定义就是"台账里没有"。
_forged = CompletionClaim(summary="在线确认。", deliverables=["一句确认"],
                          evidence=[EvidenceRef(call_seq=99, tool="dummy",
                                                shows="编造的证据")], draft_text="")
_v_forge = CompletionGate(make_registry(), strict_coverage=True).check(_forged, NT)
check("★ 10.5 禁用工具 + 编造证据 ⇒ 照样打回（不适用 ≠ 不设防）", _v_forge.passed, False)

# 有验收标准但内核测不了 ⇒ 降为警告，不是硬打回
_nt_acc = TaskContract(raw_goal="不要调用任何工具，直接回答", acceptance=["答案要有出处"])
_v_acc = CompletionGate(make_registry(), strict_coverage=True).check(_nt_claim, _nt_acc)
check("10.6 禁用工具 + 实测不了的标准 ⇒ 降为警告", _v_acc.passed, True)
check("10.7 且警告里说清了原因", any("禁用工具" in w for w in _v_acc.warnings), True)

print()
print("═══ 11. 大纲门（长综述：结构必须先冻结成文件）═══")
# 判据挂在哪：`Gate.check()` 里看 `"outline" in contract.stages`。
# 这一节用**契约形态**驱动（不写死 _is_long_review），因为那是判据本身的输入。
import os as _os
import tempfile as _tf

_old_cwd = _os.getcwd()
_tmp = _tf.mkdtemp()
_os.chdir(_tmp)
try:
    OUTLINE_CONTRACT = TaskContract(
        raw_goal="写一篇某个主题的文献综述", goal="写综述",
        acceptance=["条目齐全"], needs_writing=True,
        stages=["draft", "outline"])

    # ★ 这一节只核**大纲门**。契约里既然带了 outline 阶段，技能门也会要求
    #   `paper-spine` —— 尤其本机可能没装。所以判据取"理由里有没有大纲那几条"，
    #   而不是 `v.passed`：那是**四条门**的合取，在这里当判据等于一节测四件事。
    #   （2026-10-22 实测：用 v.passed 时 11.3 被「技能未装载 + 正文缺失」拖红，
    #    与大纲完全无关。这正是本仓记过的「一节测两件事，红了分不清是谁红」。）
    _OUT_KEYS = ("没有找到大纲文件", "读不出章标题", "没有绑定任何文献")

    def _outline_blocked(v):
        return any(k in r for r in v.reasons for k in _OUT_KEYS)

    # ① 没有大纲文件 ⇒ 打回，「没有找到大纲文件」必须点名
    _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), OUTLINE_CONTRACT)
    check("11.1 无大纲文件 ⇒ 打回", _v.passed, False)
    check("11.2 且理由点名「没有找到大纲文件」",
          any("没有找到大纲文件" in r for r in _v.reasons), True)

    # ② 大纲写得规范（章标题 + 每章 doc_id）⇒ **大纲这一条**不再拦
    Path("outline.md").write_text(
        "# 综述大纲\n\n## 第一章 引言\n一句话论证：x\ndoc_id: kb111aaaa, kb222bbbb\n"
        "本章 2000 字。\n\n## 第二章 融合机制\ndoc_id: 10.1038/abc\n本章 3000 字。\n",
        encoding="utf-8")
    _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), OUTLINE_CONTRACT)
    check("11.3 ★ 规范大纲 ⇒ 大纲门不再拦（门不是不可能通过的）",
          _outline_blocked(_v), False)

    # ③ 某一章没有 doc_id ⇒ 打回，且**点名那一章**
    Path("outline.md").write_text(
        "# 大纲\n## 第一章 绪论\n这一章没有映射文献\n## 第二章 材料\ndoc_id kb333cccc\n",
        encoding="utf-8")
    _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), OUTLINE_CONTRACT)
    check("11.4 有章没绑文献 ⇒ 打回", _v.passed, False)
    check("11.5 且点名是哪一章",
          any("第一章" in r for r in _v.reasons), True)

    # ④ 只有一行字、读不出章标题 ⇒ 打回
    Path("outline.md").write_text("只有一行，没有章标题\n", encoding="utf-8")
    _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), OUTLINE_CONTRACT)
    check("11.6 读不出章标题 ⇒ 打回", _v.passed, False)
    check("11.7 且理由点名「读不出章标题」",
          any("读不出章标题" in r for r in _v.reasons), True)

    # ⑤ 负控：契约里**没有** outline 阶段 ⇒ 门根本不看大纲
    #    这条防的是"把门写成全局硬门，连短任务也拦"。
    Path("outline.md").unlink()
    _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), contract)
    if _v.passed is not True:
        print("         诊断：reasons =", [r[:90] for r in _v.reasons])
    check("11.8 ★ 负控：无 outline 阶段的契约 ⇒ 不看大纲，照常放行", _v.passed, True)

    # ⑥ 承重自检：把判据摘掉，11.1 必须当场失守
    import rongke.gate as _g
    _orig = _g.CompletionGate._check_outline
    try:
        _g.CompletionGate._check_outline = lambda self: (True, "", {})
        _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), OUTLINE_CONTRACT)
        check("★ 11.9 承重自检：摘掉 _check_outline 后手册那几条理由当场消失",
              _outline_blocked(_v), False)
    finally:
        _g.CompletionGate._check_outline = _orig
    _v = CompletionGate(wreg()).check(wclaim(draft_text=GOOD_DRAFT), OUTLINE_CONTRACT)
    check("★ 11.10 承重自检后判据已还原（又拦住了）", _v.passed, False)
finally:
    _os.chdir(_old_cwd)

print()
print("═══ 12. 图意图派生 + 综述文献下限 50 篇 ═══")
# 这一节的两个判据都来自**用户当场提出的两条要求**，而且都是"用户能直接看见"的：
#   ① 「画一张图」的任务居然不认图（needs_figure 从没被关键词填过）
#   ② 综述的参考文献要**最少 50 篇**
# ★ 第 ① 条的教训值得单独记：`_looks_like_figure()` 一直是**对的**，
#   问题是 `__post_init__` 从来没人调用它。这类"函数定义对了、没人接线"的缺陷
#   表现是"看起来什么都在，就是不起作用"，只有端到端断言抓得住 ——
#   只测 `_looks_like_figure()` 本身会全绿。
from rongke.contract import _REVIEW_MIN_REFS, _looks_like_figure  # noqa: E402

# ① 图意图：正控（判据函数本身对，这是前提而不是结论）
check("12.1 判据函数对「画一张示意图」返回真", _looks_like_figure("画一张融合机制示意图"), True)
# ② 图意图：★ 端到端（真正的判据 —— 契约要吃到它）
for _g in ("画一张融合机制示意图", "把这段数据画成柱状图", "做一张期刊级示意图"):
    check(f"12.2 ★ 契约吃到图意图：{_g[:12]}",
          TaskContract(raw_goal=_g).needs_figure, True)
# ③ 负控：非图任务不许被误判
for _g in ("查台账", "写一篇 1200 字综述"):
    check(f"12.3 负控：{_g[:12]} 不该判成图任务",
          TaskContract(raw_goal=_g, needs_writing=("写" in _g)).needs_figure, False)
# ④ 负控：明说不要图必须听（这一条抓的是"派生提前后综述分支的否决失效"）
check("12.4 ★ 负控：明说「不用配图」时不许自动置位",
      TaskContract(raw_goal="写一篇中国被毛孢文献综述，不用配图",
                   needs_writing=True).needs_figure, False)
check("12.5 且该契约不该有 figure 阶段",
      "figure" in TaskContract(raw_goal="写一篇中国被毛孢文献综述，不用配图",
                               needs_writing=True).stages, False)

# ⑤ 文献下限：常量只有一个来源
check(f"12.6 阈值常量是 {_REVIEW_MIN_REFS}", _REVIEW_MIN_REFS, 50)

# ⑥ 数得出来吗 —— 直接量 `writing.lint` 的 `refs_n`
from rongke.writing import lint as _lint  # noqa: E402


def _draft_with_refs(n):
    body = ("# 综述\n\n"
            + "原生质体融合是重要手段[1]，本研究进展显著[2]。" * 60
            + "\n\n## 参考文献\n")
    return body + "\n".join(
        f"[{i}] 作者{i}. 题目[J]. 期刊, 2020, 1(1): 1-10." for i in range(1, n + 1))


check("12.7 数出 3 篇", _lint(_draft_with_refs(3)).metrics.get("refs_n"), 3)
check("12.8 数出 49 篇", _lint(_draft_with_refs(49)).metrics.get("refs_n"), 49)
check("12.9 数出 50 篇", _lint(_draft_with_refs(50)).metrics.get("refs_n"), 50)
# ⑦ 不数正文里的引注（否则同一篇被引三次会数成三篇 —— 那是假达标）
_no_refs = _lint("# 综述\n\n原生质体融合[1]与[2]与[3]都很重要。" * 30)
check("12.10 ★ 只有正文引注、没有文献表 ⇒ refs_n 为 0（不拿引注充数）",
      _no_refs.metrics.get("refs_n"), 0)

# ⑧ 门真的会拦吗 —— 用一个长综述契约 + 不足 50 篇的稿子
_REF_CONTRACT = TaskContract(
    raw_goal="写一篇某个主题的文献综述", goal="写综述",
    acceptance=["条目齐全"], needs_writing=True, target_chars=8000,
    stages=["draft"])
check("12.11 该契约被认成综述", getattr(_REF_CONTRACT, "_is_review_like", False), True)
check("12.12 且是长综述", getattr(_REF_CONTRACT, "_is_long_review", False), True)

_REF_KEYS = ("低于要求的", "参考文献只有")


def _ref_blocked(v):
    return any(k in r for r in v.reasons for k in _REF_KEYS)


_v = CompletionGate(wreg()).check(wclaim(draft_text=_draft_with_refs(20)), _REF_CONTRACT)
check("12.13 ★ 20 篇 ⇒ 打回", _ref_blocked(_v), True)
_v = CompletionGate(wreg()).check(wclaim(draft_text=_draft_with_refs(50)), _REF_CONTRACT)
check("12.14 50 篇 ⇒ 文献量这条不再拦", _ref_blocked(_v), False)

# ⑨ 负控：**非综述**的长文不许被 50 篇卡住（否则逼模型堆无关引文）
_not_review = TaskContract(raw_goal="写一份实验室安全操作规范", goal="写规范",
                           needs_writing=True, target_chars=8000, stages=["draft"])
check("12.15 负控：非综述长文不被认成综述",
      getattr(_not_review, "_is_review_like", False), False)
_v = CompletionGate(wreg()).check(wclaim(draft_text=_draft_with_refs(5)), _not_review)
check("12.16 ★ 负控：非综述稿只 5 篇也不因这条打回", _ref_blocked(_v), False)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
