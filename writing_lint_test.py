# -*- coding: utf-8 -*-
"""写作体检（rongke/writing.py）单测 —— 纯函数，不需要 LLM、不需要网络。

覆盖：
  1. 字数口径        —— 中文按字、西文按词（中文作者报字数的习惯）
  2. 目标字数抽取    —— 从用户原话抽，含区间/中文数字/英文；抽不到必须返回 None
  3. 体量门          —— 空稿硬拦；偏离 >40% 硬拦、>15% 软提示
  4. 结构门          —— 正文出现点列硬拦；allow_lists=True 时放行（含作用域回归）
  5. 句法门          —— 超长句硬拦、偏长句软提示、长句定位准确
  6. 套话判定        —— **看密度不看条数**（本轮修掉的核心失真）
  7. 标点与笔误      —— 叠字/连续标点硬拦；半角/不配对软提示
  8. 重复与冗余      —— 相邻句重复、冗余动词框架
  9. 英混排断句      —— 纯英文稿不能被算成"1 句"
 10. 聚合判定        —— needs_revision 阈值、score 计算
 11. 渲染            —— to_dict / brief / format_for_model 冒烟
 12. 文件入口        —— lint_file 读不到文件时硬拦（而不是静默放行）
 13. 工作台踩坑机查项 —— 引注首现顺序 / 占位符残留 / 行话泄漏 / 结尾同构 /
                        绝对化表述 / 英文句长。**每条都配正控 + 负控**：
                        只有负控的自检等于没测（「修完误报后它变成永不报」实测过）。

一个刻意的做法：所有**词数、段数**都由构造保证，不靠手数。
手数的数字会随改稿悄悄漂移，而断言仍然打印 PASS —— 那是最坏的一种通过。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke.writing import (          # noqa: E402
    Report, _cn_number, _expand_cite_group, _tail_repeat, count_chars,
    extract_target_chars, lint, lint_file,
)

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def codes(rep):
    return sorted(i.code for i in rep.issues)


def hard_codes(rep):
    return sorted(i.code for i in rep.hard())


def soft_codes(rep):
    return sorted(i.code for i in rep.soft())


# ══════════════════════════════════════════════════════════════
# 素材
# ══════════════════════════════════════════════════════════════
# 干净稿：具体、有数字、无套话、句长有变化、不超过 80 字/句
CLEAN = (
    "冬虫夏草的人工培育目前卡在三处。\n"
    "其一是寄主幼虫的规模化饲养，存活率长期低于三成。\n"
    "其二是侵染窗口极窄，错过春季两周便难以定殖，田间重复率不足两成。\n"
    "其三是子座分化依赖低温诱导，控温成本占到了总投入的六成以上。"
)

# 好稿（与 writing_gate_test 同源，132 字、零问题）：评分区间的上界对照
GOOD_PROBE = (
    "冬虫夏草（Ophiocordyceps sinensis）的完整生活史依赖蝙蝠蛾科幼虫与侵染期"
    "真菌的精确耦合，这一耦合在离体条件下极难重现。困难集中在三处：寄主昆虫"
    "规模化饲养的存活率长期偏低，侵染期菌丝活力在离体培养中迅速衰减，子实体"
    "诱导至今缺乏可重复的环境参数。近五年虽有零星室内出草报道，产量仍普遍低于"
    "野外采集一个数量级。"
)

# 空转稿：通篇套话，无一个具体事实（与 writing_gate_test 同源）
VACUOUS = (
    "随着冬虫夏草产业的飞速发展，人工培育技术受到了广泛关注。众所周知，人工培育"
    "具有十分重要的意义。近年来，越来越多的研究者取得了丰硕的成果，相关部门"
    "发挥着重要的作用。综上所述，该领域备受关注，值得我们深入探讨。"
)

# 套话稀疏散布于长文（同 8 处套话，但铺在 1500 字里只是行文习惯，不该重罚）
# 注意：VACUOUS 本身以「。」收尾，这里直接拼接，**不可再加一个句号**（会造出「。。」触发连续标点硬拦）
SPARSE = VACUOUS + ("试验记录了菌丝生长速率、子座长度与寄主存活率等指标，"
                    "逐日观测并留存原始数据。") * 40


print("═══ 1. 字数口径 ═══")
check("纯中文按字数", count_chars("冬虫夏草"), 4)
check("中文夹杂英文单词", count_chars("用 Python 统计"), 4)   # 用/统/计 + Python
check("连字符串算一个词", count_chars("state-of-the-art 方法"), 3)  # 1 词 + 2 字
check("纯标点不计", count_chars("，。；："), 0)
check("中英混排逐项累加", count_chars("检测 Ophiocordyceps sinensis 菌株"), 6)  # 4 字 + 2 词

print("\n═══ 2. 目标字数抽取（不采信模型自报）═══")
check("约120字", extract_target_chars("写一段约120字的引言"), 120)
check("120字左右", extract_target_chars("写120字左右"), 120)
check("区间取中值", extract_target_chars("写 300-500 字的摘要"), 400)
check("区间用波浪号", extract_target_chars("写 800~1000 字"), 900)
check("区间用「至」", extract_target_chars("写 200 至 400 字"), 300)
check("约一万字", extract_target_chars("写约一万字的综述"), 10000)
check("三千字", extract_target_chars("三千字左右"), 3000)
check("两万字", extract_target_chars("两万字的正文"), 20000)
check("十五字…过短不采", extract_target_chars("十五字的标语"), None)
check("英文 about 300 words", extract_target_chars("about 300 words"), 300)
check("抽不到 → None", extract_target_chars("核验这几篇文献的 DOI"), None)
check("空串 → None", extract_target_chars(""), None)
check("None 入参不炸", extract_target_chars(None, None), None)
check("多个来源合并", extract_target_chars("写引言", "约 250 字"), 250)

print("\n═══ 3. 中文数字解析 ═══")
check("一万", _cn_number("一万"), 10000)
check("一千", _cn_number("一千"), 1000)
check("三千五百", _cn_number("三千五百"), 3500)
check("二百", _cn_number("二百"), 200)
check("十五", _cn_number("十五"), 15)
check("十", _cn_number("十"), 10)
check("两万", _cn_number("两万"), 20000)
check("二万三千", _cn_number("二万三千"), 23000)
check("非数字 → None", _cn_number("甲乙"), None)
# 逆向解析曾把「一万」吞成 1，这里锁死正向行为
check("回归：一万不等于 1", _cn_number("一万"), 10000)

print("\n═══ 4. 体量门 ═══")
r = lint("")
check("空稿 → 硬拦 empty", "empty" in hard_codes(r), True)
check("空稿不该假装达标", r.needs_revision, True)

r = lint("太短。")
check("不足一句话 → 硬拦", "empty" in hard_codes(r), True)

r = lint(CLEAN * 5, target_chars=120)     # 远超目标
check("字数严重超出 → 硬拦 length_far", "length_far" in hard_codes(r), True)

r = lint(CLEAN, target_chars=120)         # 偏差约 -30%，落在 15%~40% 区间
check("偏差 30% → 软提示 length_off", "length_off" in soft_codes(r), True)
check("偏差 30% 不硬拦", "length_far" in hard_codes(r), False)

r = lint(CLEAN, target_chars=int(count_chars(CLEAN) * 1.05))
check("偏差 5% → 不提示", "length_off" in soft_codes(r), False)
check("偏差 5% → 不硬拦", "length_far" in hard_codes(r), False)

r = lint(CLEAN)
check("无目标字数 → 不做体量判定", any(c.startswith("length") for c in codes(r)), False)

print("\n═══ 5. 结构门（点列）═══")
BULLETS = (
    "主要难点如下：\n"
    "1. 寄主幼虫饲养困难，存活率低。\n"
    "2. 侵染窗口窄，错过即失败。\n"
    "3. 子座分化依赖低温，能耗高。"
)
r = lint(BULLETS)
check("正文点列 → 硬拦 bullet_prose", "bullet_prose" in hard_codes(r), True)

r = lint(BULLETS.replace("1. ", "- ").replace("2. ", "- ").replace("3. ", "- "))
check("半角短横线点列也认", "bullet_prose" in hard_codes(r), True)

r = lint(BULLETS.replace("1. ", "（一）").replace("2. ", "（二）").replace("3. ", "（三）"))
check("中文括号序号也认", "bullet_prose" in hard_codes(r), True)

# ── 作用域回归：bullet_paras 只在 `if not allow_lists` 里定义，
#     却出现在 metrics 里 —— 之前靠三目短路侥幸不炸。这里必须锁死。
r = lint(BULLETS, allow_lists=True)
check("allow_lists=True → 不硬拦点列", "bullet_prose" in hard_codes(r), False)
check("allow_lists=True 不炸（作用域回归）", isinstance(r, Report), True)
check("allow_lists=True 时 metrics 记账为 0", r.metrics["bullet_paras"], 0)
r2 = lint(BULLETS)
check("allow_lists=False 时 metrics 记真实条数", r2.metrics["bullet_paras"], 3)

print("\n═══ 6. 句法门 ═══")
# 最长句 125 字（实测），越过 120 硬线
LONG_ONE = ("本研究以冬虫夏草为对象，在四川甘孜的三个试验点连续观测了寄主幼虫的存活情况、"
            "菌丝侵染的时序进程、子座的形态建成以及不同温湿度组合下的产量表现，"
            "并借助多元回归分析了各因子之间的交互效应与主效应，"
            "同时结合土壤微生物群落的高通量测序数据，试图回答一个长期悬而未决的问题。"
            "结果如下。")
r = lint(LONG_ONE)
check("超长句(>120) → 硬拦 sentence_overlong",
      "sentence_overlong" in hard_codes(r), True)
check("硬拦时指标记下最长句", r.metrics["max_sentence"] > 120, True)
check("硬拦话术给出定位", "段第" in r.format_for_model(), True)

# 最长句 82 字（实测），落在 80–120 软区间
MID_ONE = "本研究在四川甘孜的三个试验点连续观测寄主存活率、菌丝侵染时序、子座形态建成与产量表现，并借助多元回归分析了各因子之间的交互效应与主效应与边际效应，结论是低温诱导环节不可替代。"
r = lint(MID_ONE)
check("偏长句(80~120) → 软提示 sentence_long",
      "sentence_long" in soft_codes(r), True)
check("偏长句不硬拦", "sentence_overlong" in hard_codes(r), False)

r = lint(CLEAN)
check("正常稿无句法硬拦", [c for c in hard_codes(r) if c.startswith("sentence")], [])

print("\n═══ 7. 套话判定（看密度不看条数）═══")
r = lint(VACUOUS, target_chars=120)
check("空转稿 → 硬拦 cliche", "cliche" in hard_codes(r), True)
check("空转稿评分跌破 70", r.score < 70, True)
check("空转稿触发改稿", r.needs_revision, True)
check("套话密度被记下", r.metrics["cliche_density"] >= 2.0, True)

r = lint(SPARSE)
check("套话稀疏铺在长文 → 仅软提示", "cliche" in soft_codes(r), True)
check("稀疏套话不硬拦", "cliche" in hard_codes(r), False)

r = lint(CLEAN)
check("干净稿不报套话", "cliche" in codes(r), False)

print("\n═══ 8. 标点与笔误 ═══")
r = lint("这是一段测试文字，用来检查体检器的标点判定能力，内容足够长以免触发空稿门。。")
check("连续标点 → 硬拦", "punct_double" in hard_codes(r), True)

r = lint("冬虫夏草的的培育存在难点，菌种退化问题需要系统研究才能说清楚。")
check("叠字笔误 → 硬拦", "typo_double" in hard_codes(r), True)

r = lint("这段文字里混用了半角逗号,所以应当被软提示出来,内容凑够长度避免空稿误报。")
check("中文里的半角标点 → 软提示", "punct_halfwidth" in soft_codes(r), True)

# 不配对的括号类一律软提示 —— 弯引号 / 角引号 / 双角引号 / 全角圆括号
for probe, name in (("这里只有左弯引号“没有右弯引号，内容凑够长度避免空稿误报的情况发生。", "弯引号"),
                    ("这里只有左角引号「没有右角引号，内容凑够长度避免空稿误报的情况发生。", "角引号"),
                    ("这里只有左双角引号『没有右双角引号，内容凑够长度避免空稿误报的情况。", "双角引号"),
                    ("这里只有左括号（没有右括号，内容凑够长度避免空稿误报的情况发生呀。", "全角括号")):
    r = lint(probe)
    check(f"{name}不配对 → 软提示", "punct_unpaired" in soft_codes(r), True)

r = lint("这是一句「引号成对」的话，用来确认配平后不会被误报，内容凑够长度避免空稿。")
check("引号配平 → 不误报", "punct_unpaired" in soft_codes(r), False)

r = lint(CLEAN)
check("干净稿无标点问题", [c for c in codes(r) if c.startswith("punct")], [])

print("\n═══ 9. 重复与冗余 ═══")
r = lint("冬虫夏草的人工培育存在明显难点。人工培育存在明显难点，需要系统研究。")
check("相邻句重复 → 软提示", "repeat" in soft_codes(r), True)

r = lint("研究者对该菌株进行了观察和记录，并对产量数据进行了分析，内容足够长。")
check("冗余动词框架 → 软提示", "redundant_frame" in soft_codes(r), True)
check("冗余框架给出替换建议", "观察" in r.format_for_model(), True)

print("\n═══ 10. 英混排断句 ═══")
ENG = ("Cordyceps cultivation is difficult. The host larvae survive poorly. "
       "The infection window is narrow. Low temperature is required.")
r = lint(ENG)
check("纯英文被切成 4 句（不是 1 句）", r.metrics["sentences"], 4)
check("英文稿不产生超长句假警报", "sentence_overlong" in hard_codes(r), False)

r = lint("Cordyceps sinensis 的人工培育存在难点，需要系统研究其菌种退化机理与防控路径。")
check("中英混排不产生假警报", "sentence_overlong" in hard_codes(r), False)

print("\n═══ 11. 聚合判定与评分 ═══")
r = lint("")
check("硬问题 → 必打回", r.needs_revision, True)
check("1 条硬问题 → 评分 60（<70 区）", r.score, 60)

# 评分两段式：信号必须与判定同向，否则「必修」会配着一个漂亮的分数一起显示
band = [
    ("空稿", ""),
    ("超长句", LONG_ONE),
    ("点列正文", BULLETS),
    ("叠字笔误", "冬虫夏草的的培育存在难点，菌种退化问题需要系统研究才能说清楚。"),
    ("连续标点", "这是一段测试文字，用来检查标点判定能力，内容足够长以免触发空稿门。。"),
    ("空转稿", VACUOUS),
]
for name, t in band:
    r = lint(t, 120)
    check(f"{name}：有硬问题 ⇒ 评分 <70", r.hard() and r.score < 70, True)

for name, t in [("干净稿", CLEAN), ("好稿", GOOD_PROBE), ("稀疏套话长文", SPARSE)]:
    # 对照组不设字数目标 —— 否则长文会被 length_far 硬拦，测的就不是套话判定了
    r = lint(t)
    check(f"{name}：无硬问题 ⇒ 评分 ≥70", (not r.hard()) and r.score >= 70, True)

# 软问题要攒够 4 条才打回（宁少打一次，不误伤一遍）
soft_pad = "这段文字里混用了半角逗号,所以会被软提示,内容凑够长度避免空稿误报的情况发生。"


def count_soft(rep):
    return len(rep.soft())


r1 = lint(soft_pad)
check("软问题 <4 → 不打回", count_soft(r1) < 4 and not r1.needs_revision, True)

r = lint(CLEAN)
check("干净稿评分 100", r.score, 100)
check("干净稿不打回", r.needs_revision, False)

print("\n═══ 12. 渲染与序列化 ═══")
r = lint(VACUOUS, target_chars=120)
d = r.to_dict()
check("to_dict 含 hard/soft 计数", (d["hard"] >= 1 and "soft" in d), True)
check("to_dict 可 JSON 化", isinstance(d["issues"], list) and isinstance(d["issues"][0], dict), True)
check("brief 含字数与评分", ("字" in r.brief() and "/100" in r.brief()), True)
check("brief 含目标字数", "目标约 120 字" in r.brief(), True)

fmt = r.format_for_model()
check("体检报告含必修段", "必修" in fmt, True)
check("体检报告含改法", "改法" in fmt, True)
check("体检报告声明边界", "不评判论证" in fmt, True)
check("体检报告含笔误定位" if False else "体检报告给出字数偏差",
      "偏差" in fmt, True)

r = lint(CLEAN)
check("干净稿报告如实说没问题", "未发现程序化可查的问题" in r.format_for_model(), True)

print("\n═══ 13. 文件入口 lint_file ═══")
r = lint_file(ROOT / "runs" / "_no_such_draft_存在即为错误.md")
check("读不到稿件 → 硬拦", "draft_unreadable" in hard_codes(r), True)
check("读不到稿件 → 不打回则等于静默放行", r.needs_revision, True)

# 落点用**固定名**，靠 `write_text` 同名覆盖 —— 本来就不需要"先删掉旧的"。
# （原来那句 `tmp.unlink()` 每跑一次花 1 点：本环境按**条目数**收删除费，见 `_tkit`。）
tmp = ROOT / "runs" / "_wlint_tmp.md"
tmp.parent.mkdir(parents=True, exist_ok=True)
tmp.write_text(CLEAN, encoding="utf-8")
r = lint_file(tmp)
check("读得到稿件 → 正常体检", r.metrics["chars"] > 0 and not r.hard(), True)

print("\n═══ 14. 工作台踩坑机查项（正控 + 负控）═══")
# 这一节的存在理由：那些坑**没有一条会抛异常**。所以每加一条检查，
# 都必须先证明它**能判出坏**（正控）；同时证明它在干净稿上闭嘴（负控）。
# 只测"干净稿不报警"是不够的 —— 一个恒返回空的检查器能完美通过那种测试。

print("\n── 14.1 引注首现顺序 ──")
# 负控：正序 + 号段展开应全部通过
r = lint("寄主饲养的存活率长期偏低[1]。侵染窗口极窄[2]，子座分化依赖低温[3]，成本高企。")
check("正序编号 → 不报警", "cite_order" in codes(r), False)
r = lint("寄主饲养的存活率长期偏低[1-3]，侵染窗口极窄[2]，子座分化依赖低温[4]，成本高企。")
check("号段 [1-3] 展开后判为正序", "cite_order" in codes(r), False)
check("号段被展平成 1,2,3,4", r.metrics["cite_first_order"], [1, 2, 3, 4])

# 正控：逆序必须报出来，且从 [1] 起编时是**硬门**（全文顺序可判）
BAD_CITE = "寄主饲养的存活率长期偏低[1]，子座分化依赖低温诱导[3]，侵染窗口极窄[2]，成本高企。"
r = lint(BAD_CITE)
check("逆序编号 → 报出 cite_order", "cite_order" in codes(r), True)
check("从 [1] 起编 → 硬门", "cite_order" in hard_codes(r), True)
check("首现序被完整记下", r.metrics["cite_first_order"], [1, 3, 2])
check("改法含纪律三件事", "重排参考文献表" in r.format_for_model(), True)

# 负控／降级：正文最小号 >1 ⇒ 多半只是论文的一节，不许硬拦
r = lint("子座分化依赖低温诱导[3]，寄主饲养的存活率长期偏低[1]，侵染窗口极窄[2]。")
check("节选（非 [1] 起编）→ 只提示不硬拦", "cite_order" in hard_codes(r), False)
check("节选 → 仍提示", "cite_order" in soft_codes(r), True)
check("节选 → 说明可忽略", "节选" in r.format_for_model(), True)

# 负控：年份区间不是引注（4 位数字不该被当成编号）
r = lint("观测期为 2020 至 2025 年的记录[2020-2025]，共设 6 个试验点与 3 个对照。")
check("年份区间 [2020-2025] 不算引注", r.metrics["cite_numbers"], 0)
check("年份区间不触发顺序判定", "cite_order" in codes(r), False)

# 正控：两套引注制混用（数字制 + 著者-出版年制）
r = lint("该结论与既有报道一致[1]，也与另一项独立研究（Zhang et al., 2020）吻合。")
check("混用两套引注制 → 报出 cite_mixed", "cite_mixed" in soft_codes(r), True)
check("混用计入两类", (r.metrics["cite_numbers"], r.metrics["cite_author_year"]), (1, 1))

# 号段展开的直接单测（含越界保护）
check("展开 1,3-5", _expand_cite_group("1,3-5"), [1, 3, 4, 5])
check("展开 1-3", _expand_cite_group("1-3"), [1, 2, 3])
check("号段跨度过大 → 只取起点", _expand_cite_group("1-99"), [1])
check("倒序号段 → 只取起点", _expand_cite_group("999-1"), [999])

print("\n── 14.2 占位符 / 模板残留（硬）──")
# 正控：每一种形状各来一次
for probe, name in (("子座分化依赖低温诱导[TODO]，成本占到了六成。", "TODO"),
                    ("该菌株的侵染机制待补充，田间数据也存在明显缺口。", "待补充"),
                    ("某某等报道了类似现象，但样本量偏小且缺少对照。", "某某"),
                    ("该机制【】尚未明确，需要进一步实验才能说清。", "空方括号"),
                    ("后处理留下的 \\1 没有回填，正文出现孤立的标签。", "未回填捕获组")):
    r = lint(probe)
    check(f"{name} 残留 → 硬拦 placeholder", "placeholder" in hard_codes(r), True)

# 负控：干净稿一个都不许报
r = lint(CLEAN)
check("干净稿无占位符", "placeholder" in codes(r), False)
check("干净稿占位符计数为 0", r.metrics["placeholders"], 0)

print("\n── 14.3 工具链行话泄漏（软）──")
r = lint("本稿由 workbench 流水线生成，正文取自 draft_text 字段，导出后另存 backup.bak 一份。")
check("内行话泄漏 → 报出 jargon", "jargon" in soft_codes(r), True)
check("行话不硬拦", "jargon" in hard_codes(r), False)
check("行话计数被记下", r.metrics["jargon_hits"] >= 3, True)
# 负控：正常学术用语不许误伤（JSON / prompt / token 是正当术语，故刻意不收）
r = lint("该模型以 JSON 格式记录输出，prompt 模板见附录，token 预算与既往研究一致。")
check("正当术语 JSON/prompt/token 不报", "jargon" in codes(r), False)

print("\n── 14.4 多段结尾同构（软）──")
SAME_TAIL = ("寄主饲养的存活率长期偏低，这一环节对规模化生产具有重要意义。\n"
             "侵染窗口极窄且难以把握，这一特性对田间推广具有重要意义。\n"
             "子座分化依赖低温诱导，这一条件对成本控制具有重要意义。")
r = lint(SAME_TAIL)
check("三段同尾 → 报出 tail_same", "tail_same" in soft_codes(r), True)
check("同尾不硬拦", "tail_same" in hard_codes(r), False)
check("点名同构的段号", "1、2、3" in r.format_for_model(), True)
check("同构尾巴被记下", r.metrics["tail_repeat"], 3)

# 正控（第二档）：4 段共享同一个二字落点
r = lint("寄主饲养环节存在存活率低的问题。\n侵染窗口的把控存在时序难的问题。\n"
         "子座分化存在低温诱导难的问题。\n成本控制存在能耗高的问题。")
check("4 段同二字落点 → 报出 tail_same", "tail_same" in soft_codes(r), True)
check("二字档说明可能是有意排比", "排比" in r.format_for_model(), True)

# 负控 1：干净稿
check("干净稿无结尾同构", "tail_same" in codes(lint(CLEAN)), False)
# 负控 2（关键）：SPARSE 是**同一句重复 40 次的一个段落** —— 段落级检查不许误伤它。
# 若这条检查退化成"句级同尾"，它会在这里炸，而 SPARSE 在总分对照里是「≥70 的正常长文」。
check("单段内同句重复 40 次 → 不算结尾同构",
      "tail_same" in codes(lint(SPARSE)), False)
# _tail_repeat 直接单测
check("_tail_repeat 命中", _tail_repeat(["甲乙丙丁戊", "X乙丙丁戊", "Y乙丙丁戊"], 4, 3),
      ("乙丙丁戊", [1, 2, 3]))
check("_tail_repeat 不足 need → 不报", _tail_repeat(["甲乙丙丁戊", "X乙丙丁戊"], 4, 3), ("", []))

print("\n── 14.5 绝对化表述（软）──")
# 正控：强档命中一处就报
r = lint("该结论毫无疑问成立，且方法完全避免了主观偏差带来的干扰。")
check("强档词（毫无疑问）→ 报出 absolute", "absolute" in soft_codes(r), True)
check("绝对化不硬拦", "absolute" in hard_codes(r), False)
# 正控：弱档攒够 3 处才报
r = lint("结果与既有报道完全一致，与前人结论完全一致，与另一组独立数据也完全一致。")
check("弱档 3 处 → 报出 absolute", "absolute" in soft_codes(r), True)
check("弱档计数为 3", r.metrics["absolutes"], 3)
# 负控：弱档只出现 1 处不许报（「完全一致」有时是正当的）
r = lint("本次结果与既有报道完全一致，说明该规律在本地材料中同样成立。")
check("弱档 1 处 → 不报", "absolute" in codes(r), False)
# 负控：干净稿
check("干净稿无绝对化", "absolute" in codes(lint(CLEAN)), False)


def en_sentence(n_words: int) -> str:
    """构造**恰好** n 个西文词的英文句 —— 词数由构造保证，不靠手数。"""
    return " ".join(["cultivation"] * (n_words - 1) + ["obstacle"]) + "."


print("\n── 14.6 英文句长（软 35 词 / 硬 70 词）──")
r = lint(en_sentence(30))
check("30 词 → 不报（负控）", "en_sentence_long" in codes(r), False)
r = lint(en_sentence(40))
check("40 词 → 软提示", "en_sentence_long" in soft_codes(r), True)
check("40 词计数准确", r.metrics["max_en_words"], 40)
r = lint(en_sentence(80))
check("80 词 → 硬拦", "en_sentence_long" in hard_codes(r), True)

# 现实英文句：口径必须与「按空格切词」一致
REAL_EN = ("Cordyceps cultivation remains difficult because the host larvae survive poorly "
           "under laboratory conditions while the infection window is extremely narrow and "
           "the stroma differentiation requires low temperature induction that is costly to "
           "maintain at scale in most facilities across the region.")
r = lint(REAL_EN)
check("现实长句 → 软提示", "en_sentence_long" in soft_codes(r), True)
check("英文词数口径 == 按空格切词", r.metrics["max_en_words"], len(REAL_EN.split()))
# 负控：ENG（4 个短句）不许被算成长句
check("短英文句不报（负控）", "en_sentence_long" in codes(lint(ENG)), False)
# 负控：中英混排的长句由**中文字数**那条管，不该被英文这条重复报
r = lint("Cordyceps sinensis 的人工培育存在难点，需要系统研究其菌种退化机理与防控路径。")
check("中英混排不算纯英文句", r.metrics["max_en_words"], 0)

print("\n── 14.10 字数口径：剔除文末文献表 + 图表计数 ──")
# 踩坑清单第 19 条：期刊上限按「含表图、剔除文末文献表」核，而这条**没有机验**。
# 内核若把文献表算进正文，模型就会为了"压到上限"去砍正文 —— 砍错地方，且不报错。
WITH_REFS = (
    "冬虫夏草的人工培育目前卡在三处。寄主幼虫的规模化饲养存活率长期低于三成，\n"
    "侵染窗口极窄、错过春季两周便难以定殖，子座分化依赖低温诱导、控温成本占到总投入六成以上。\n"
    "图 1 冬虫夏草的生活史各阶段\n"
    "表 1 三个试验点的产量对比\n"
    "参考文献\n"
    "[1] 张三. 冬虫夏草人工培育研究[J]. 菌物学报, 2020, 39(4): 12-20.\n"
    "[2] 李四. 蝙蝠蛾科幼虫饲养技术[J]. 昆虫学报, 2021, 44(2): 33-41."
)
r = lint(WITH_REFS)
check("检出参考文献表", r.metrics["has_refs"], 1)
check("剔除文献表后正文更短", r.metrics["chars_excl_refs"] < r.metrics["chars"], True)
check("图注计数", r.metrics["figures"], 1)
check("表注计数", r.metrics["tables"], 1)
check("口径提示出现在报告里", "剔除" in r.format_for_model(), True)
r2 = lint(WITH_REFS, target_chars=60)          # 必然超限 → 看改法里带不带口径
check("字数问题的改法里带口径", "剔除" in r2.format_for_model(), True)

# 负控 1：没有文献表时，口径数必须等于全文数（不许凭空缩水）
r = lint(CLEAN)
check("无文献表 → has_refs = 0", r.metrics["has_refs"], 0)
check("无文献表 → 口径数 == 全文数", r.metrics["chars_excl_refs"], r.metrics["chars"])
check("干净稿图表计数为 0", (r.metrics["figures"], r.metrics["tables"]), (0, 0))

# 负控 2（关键）：正文里出现「参考文献」四个字，不许被当成文献表标题 ——
# 否则一整篇稿子的字数口径被静默改掉（而它不会报错）。
r = lint("参考文献的著录格式在各刊之间并不统一，作者投稿前需要逐条核对，否则会被编辑退回修改。")
check("正文提到「参考文献」→ 不误判为文献表", r.metrics["has_refs"], 0)

# 负控 3（关键）：图表的交叉引用不许重复计数 ——
# 「同一张表被引三次 = 三张表」比不数更糟。所以只认**行首的图注/表注**。
r = lint("表 2 给出了三个试验点的产量对比，见表 2 与图 3 的对照，图 3 已在附录给出。")
check("行首表注计入", r.metrics["tables"], 1)
check("非行首的「图 3」不计入", r.metrics["figures"], 0)

# 正控：行首的三条图注/表注
r = lint("图 1 冬虫夏草的生活史\n图 2 菌丝侵染的时序\n表 1 三个试验点的产量对比")
check("三条行首注 → 图 2 表 1", (r.metrics["figures"], r.metrics["tables"]), (2, 1))

print("\n── 14.7 新增指标进 to_dict（UI / 回灌都要能用）──")
d = lint(BAD_CITE, 120).to_dict()
for k in ("placeholders", "cite_numbers", "cite_author_year", "cite_first_order",
          "jargon_hits", "tail_repeat", "absolutes", "max_en_words",
          "chars_excl_refs", "has_refs", "figures", "tables"):
    check(f"metrics 含 {k}", k in d["metrics"], True)

print("\n── 14.8 检查器自检：每条都必须「承重」──")
# 这一节针对的是纪律第 30 条那个实测事故：**修完一个误报后，它直接变成永不报**
# ——因为检查自我闭环、恒为通过。做法：把某条检查的正则换成「永不匹配」，
# 正控必须**当场失守**。失守 = 这条检查真的在承重；不失守 = 它本来就没在干活，
# 而上面那些正控 POSS 是别的原因在报。**只有负控的自检等于没测。**
import re                                        # noqa: E402
import rongke.writing as _W                      # noqa: E402

_MUTATIONS = (
    ("_PLACEHOLDER", "placeholder", "子座分化依赖低温诱导[TODO]，成本占到了六成。"),
    ("_JARGON", "jargon", "本稿由 workbench 流水线生成，正文取自 draft_text 字段。"),
    ("_ABS_STRONG", "absolute", "该结论毫无疑问成立，值得直接采用。"),
    ("_CITE_NUM", "cite_order",
     "寄主饲养的存活率长期偏低[1]，子座分化依赖低温诱导[3]，侵染窗口极窄[2]，成本高企。"),
)
for attr, code, probe in _MUTATIONS:
    live = code in codes(lint(probe))            # 未消掉 → 必须报出来
    old = getattr(_W, attr)
    setattr(_W, attr, re.compile(r"(?!)"))       # 永不匹配
    try:
        dead = code in codes(lint(probe))        # 消掉后 → 必须报不出来
    finally:
        setattr(_W, attr, old)                   # 一定要还原，否则污染后面所有断言
    check(f"消掉 {attr} ⇒ {code} 不再报（证明它承重）", (live, dead), (True, False))
    check(f"{attr} 已还原", getattr(_W, attr) is old, True)

print("\n── 14.9 _tail_repeat 的界限（段级 vs 句级）──")
# 这条检查最容易的退化方式：把"段尾"写成"句尾"。用一个段落里重复 40 次的 SPARSE
# 反向锁死它 —— 句级实现会在这里炸，而 SPARSE 是「≥70 分的正常长文」。
check("句子重复 ≠ 段尾同构", _tail_repeat(SPARSE.split("\n"), 4, 3), ("", []))
check("SPARSE 确为单段（前提复核）", len(SPARSE.split("\n")), 1)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
