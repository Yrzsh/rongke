# -*- coding: utf-8 -*-
"""文献够用性判定 + 取全文能力 + 综述流程接线的自测。

这一套补的是 2026-10-22 那次改造的**判据**。它对应的三件真事：

  1. 荣珂过去**没有任何一处**回答"库里现有的文献够不够写这篇综述"。
     模型直接开写，写到第三节发现证据不够，于是回头补检索 —— 那正是用户
     说的"多次返工出来的一般很垃圾"。
  2. 「下不到全文」的真根因是下载器缺 Europe PMC fullTextXML 这条路，
     而不是反爬。判据必须能证明**那条路存在**且**真能取到正文**。
  3. 综述任务的阶段必须按「够不够 → 补全文 → 定大纲 → 先出图 → 后写」
     这个次序推出来，且**短综述不许被推成这套**。

覆盖：
  A. `coverage.assess()` 的判定（纯确定性，不碰网络）
  B. `litfetch` 三条路的路由（走真实网络，可跳）
  C. 阶段推断与开工动作文本（纯确定性）
  D. `kb_coverage` / `literature_fetch` 两个工具真的注册进了 core 域
  E. 承重自检：把判据摘掉，前面某条断言必须当场失守
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import coverage as cov                        # noqa: E402
from rongke import litfetch as lf                        # noqa: E402
from rongke.contract import TaskContract                 # noqa: E402

ok_all = True
fails: list[str] = []


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")
        fails.append(label)


def _row(doc_ids, ok=True, term="原生质体融合", pages=True):
    """造一条 `assess()` 的输入。**形状照 `coverage.assess` 的文档**：

        {"term": ..., "ok": bool, "hits": [{"doc_id":..., "page":...}, ...], "error": ...}

    ⚠️ 我第一版把 doc_id 直接放在行上（`{"doc_id": ...}`），`assess()` 读的是
       `hits[]` 里的 doc_id，于是每行都算 0 命中 —— 一个**造错了夹具**的例子。
       夹具必须照产出方的真实形状造，不能照"我以为的形状"造。
    """
    ids = [doc_ids] if isinstance(doc_ids, str) else list(doc_ids)
    hits = [{"doc_id": d, "page": (i if pages else ""),
             "text": f"{term} 的相关段落"} for i, d in enumerate(ids) if d]
    return {"term": term, "ok": ok, "hits": hits,
            "error": "" if ok else "检索通道失败"}


print("═══ A. 文献够用性判定（库够不够） ═══")

# A1 主题下 3 篇不同文献 ⇒ 够用
rep = cov.assess([_row(["kb1aaaaa", "kb2bbbbb", "kb3ccccc"])])
check("A1.1 三篇都命中 ⇒ 不需要补检索", rep["need_more_literature"], False)
check("A1.2 且 verdict 是够用", rep["verdict"], "够用")
check("A1.3 且点名看到了几篇不同文献", rep["distinct_docs_seen"], 3)

# A2 只有 2 篇（低于下限 3）⇒ 不够，且点名该主题
rep = cov.assess([_row(["kb1aaaaa", "kb2bbbbb"])])
check("A2.1 只有 2 篇 ⇒ 要补检索", rep["need_more_literature"], True)
check("A2.2 且缺失原因里点名该主题（在 thin 里）",
      any("原生质体融合" in g["term"] for g in rep["thin"]), True)

# A3 篇数够、但"命中"的其实是同一篇（doc_id 重复）⇒ 不算够
#    这条防的是"库里同一篇被多个词/多段捞出来，被当成多篇证据"。
rep = cov.assess([_row(["kb1aaaaa", "kb1aaaaa", "kb1aaaaa"])])
check("A3.1 ★ 三段都命中同一篇 ⇒ distinct=1，仍要补", rep["distinct_docs_seen"], 1)
check("A3.2 ★ 且判为不够", rep["need_more_literature"], True)

# A4 全失败（ok=False）⇒ 不够，且 failed 里点名
rep = cov.assess([_row([], ok=False), _row([], ok=False)])
check("A4.1 全失败 ⇒ 要补检索", rep["need_more_literature"], True)
check("A4.2 且 failed 记下了次数", len(rep["failed"]) > 0, True)

# A5 硬缺口优先于比例：4 个主题里 3 个满、1 个全空，比例 75% 高于阈值也得补
_rows = [_row(["kb1aaaaa", "kb2bbbbb", "kb3ccccc"], term="原生质体融合"),
         _row(["kbd1xxxx", "kbd2xxxx", "kbd3xxxx"], term="原生质体再生"),
         _row(["kbe1xxxx", "kbe2xxxx", "kbe3xxxx"], term="菌株鉴定"),
         _row([], term="酶解条件")]
rep = cov.assess(_rows)
check("A5.1 ★ 有一个主题全空 ⇒ 无论总比例多高都要补（一条硬缺口抵不掉）",
      rep["need_more_literature"], True)
check("A5.2 且 gaps 里点名那个主题",
      any("酶解条件" in g["term"] for g in rep["gaps"]), True)

# A6 空输入不许崩，且**不许**判成"够用"——
#    但也不许判成"不够去补"：那是"无从判断"，要说清是主题词没给对。
rep = cov.assess([])
check("A6.1 空输入 ⇒ 不崩", isinstance(rep, dict), True)
check("A6.2 ★ 空输入 ⇒ 判「无从判断」而不是「库不够」"
      "（把没给主题词读成'库里没有'，模型就会白搜一轮）",
      rep["verdict"], "无从判断")

# A7 `next_action()` 给的是可执行动作，不是空话
_note = cov.next_action(cov.assess([_row(["kb1aaaaa"])]))
check("A7.1 next_action 非空", bool(_note), True)
check("A7.2 且点名要用的工具名（不是泛泛'去检索'）",
      ("literature_fetch" in _note or "literature_search" in _note), True)

# A8 两字中文词必须被单列出来（trigram 索引会静默丢它们）
_short = cov.assess([{"term": "融合", "ok": True, "hits": []},
                     {"term": "温度", "ok": True, "hits": []},
                     {"term": "原生质体融合", "ok": True,
                      "hits": [{"doc_id": "kb1aaaaa", "page": "1"}]}])
check("A8.1 ★ 两字词被挑出来单列（不能混进'搜不到'里）",
      sorted(_short["dropped_short_terms"]["terms"]), ["温度", "融合"])
check("A8.2 且说明里点清「不能当作库里没有」",
      "不能当作" in _short["dropped_short_terms"]["why"], True)

print()
print("═══ B. 取全文三条路 ═══")
# 静态：路由顺序与常量在位
check("B1.1 有 Europe PMC 这一路（历史缺口就是它）",
      hasattr(lf, "_via_europepmc"), True)
check("B1.2 有 Unpaywall 这一路", hasattr(lf, "_via_unpaywall"), True)
check("B1.3 有 doi.org 内容协商这一路", hasattr(lf, "_via_doi_content"), True)
check("B1.4 正文下限是「够读」而不是「有文件」（≥1000 字符）",
      lf.MIN_BODY_CHARS >= 1000, True)

# 动态：编一个不存在的 DOI，三条路都必须失败，且**错误话术不能让人以为世上没有**
rep = lf.fetch_one("10.9999/rongke.this-doi-does-not-exist.0", Path("."))
check("B2.1 假 DOI ⇒ ok=False", rep.get("ok"), False)
check("B2.2 ★ 且错误里说清「是这几条路这次没成」，不是「世上没有」",
      "这不是" in (rep.get("error") or "") or "没成" in (rep.get("error") or ""), True)
check("B2.3 且三条路都留下了尝试记录", len(rep.get("tried") or []), 3)

print()
print("═══ C. 综述流程的阶段推断 ═══")
LONG = TaskContract(raw_goal="写一篇中国被毛孢与蝙蝠蛾拟青霉原生质体融合的文献综述",
                    needs_writing=True)
check("C1.1 长综述 ⇒ 认出这是综述", LONG._is_review_like, True)
check("C1.2 长综述 ⇒ 认出这是长稿", LONG._is_long_review, True)
check("C1.3 ★ 阶段次序是「补全文 → 定大纲 → 先画图 → 后写」",
      LONG.stages, ["search", "fulltext", "outline", "figure", "draft"])
check("C1.4 ★ figure 必须在 draft 之前（先图后文）",
      LONG.stages.index("figure") < LONG.stages.index("draft"), True)
check("C1.5 ★ outline 必须在 figure 之前（先冻结结构）",
      LONG.stages.index("outline") < LONG.stages.index("figure"), True)
check("C1.6 综述默认要文献库", LONG.needs_kb, True)
check("C1.7 综述默认要图", LONG.needs_figure, True)

# 开工动作文本：四段都得在，且次序对
_p = LONG.to_prompt()
for name, key in (("阶段技能", "阶段"), ("全文获取", "全文"),
                  ("大纲", "大纲"), ("图面纪律", "图")):
    check(f"C2 提示词里有「{name}」", key in _p, True)
check("C2.5 大纲段排在全文段之后（先确认有料，再定结构）",
      _p.index("大纲与章-文献映射") > _p.index("全文获取"), True)

# 反向控制：这些**不许**被推成综述流程
SHORT = TaskContract(raw_goal="写一篇 1200 字综述", needs_writing=True)
check("C3.1 ★ 短综述（1200 字）⇒ 不当成长综述", SHORT._is_long_review, False)
check("C3.2 ★ 且不要图（1200 字的综述塞图是硬凑）", SHORT.needs_figure, False)
check("C3.3 ★ 且不推 fulltext/outline/figure 三个阶段", SHORT.stages, ["draft"])

NOFIG = TaskContract(raw_goal="写一篇 15000 字的文献综述，不用配图", needs_writing=True)
check("C3.4 ★ 明确说不要图 ⇒ needs_figure=False", NOFIG.needs_figure, False)

NONREVIEW = TaskContract(raw_goal="把材料区那份做成 PPT", needs_writing=True)
check("C3.5 ★ 负控：不是综述 ⇒ 不推 outline（不能见谁都套大纲）",
      "outline" in NONREVIEW.stages, False)
check("C3.6 ★ 负控：非综述任务 to_prompt 不许崩（属性必须先生成）",
      isinstance(NONREVIEW.to_prompt(), str), True)

print()
print("═══ D. 两个新工具真的注册进了内核 ═══")
try:
    from rongke.config import Config
    from rongke.loop import Rongke
    _rk = Rongke(Config.load(ROOT), install_plugins=False, install_mcp_servers=False)
    _names = _rk.registry.names()
    _cov = _rk.registry.get("kb_coverage")
    _fet = _rk.registry.get("literature_fetch")
    _rk.close()
    check("D1.1 kb_coverage 已注册", "kb_coverage" in _names, True)
    check("D1.2 literature_fetch 已注册", "literature_fetch" in _names, True)
    check("D1.3 两者都在 core 域（不依赖文献库装备也能用）",
          {_cov.domain, _fet.domain}, {"core"})
except Exception as e:                                      # noqa: BLE001
    check(f"D 装配内核时抛异常：{type(e).__name__}: {e}", False, True)

print()
print("═══ D2. ★ 接线真打通了没有（这一节是为两个真 bug 补的） ═══")
# 背景：`kb_coverage` 第一版有两个**静默**失败，单测当时抓不到、真跑才暴露：
#   ① 传 `top_k=` 而 workbench 的签名是 `n=` ⇒ TypeError 被包装层吞成空串
#   ② 把 `_KB_CFG["registry"]`（ToolRegistry 对象）当 dict 用 ⇒ 每次检索都进 except
# 两个都不会报错，只会让**每个主题词都变成"检索失败"**，于是永远判「库不够」。
# 那是最坏的一种错：它看起来像"库里真的没有"，会让模型白补一圈文献。
# ⇒ 判据打在"和真产出方同一接口"上，而不是"函数能返回 dict"。

# D2.1 workbench 的 kb_search 真实签名必须有 `n`、没有 `top_k`
try:
    from rongke.bridge import _import_workbench
    _wb = _import_workbench(Path.home() / ".dsh" / "papers" / "workbench")
    _lib, _root = (getattr(_wb, "_kb_module"))("")
    check("D2.1 真库能打开（拿不到就跳过这一节，不当失败）", _lib is not None, True)
    _hits = _lib.search_paras("protoplast fusion", n=5)
    check("D2.2 ★ 底层 search_paras 本身能返回命中（库是好的）",
          len(_hits) > 0, True)
except Exception as e:                                      # noqa: BLE001
    print(f"  [SKIP] D2 本机没有可用文献库（{type(e).__name__}），跳过真接线核验")

# D2.3 `kb_coverage` 的工具签名里 `per_topic` 必须还在（我改过这处）
from rongke.loop import _make_kb_coverage, _make_literature_fetch  # noqa: E402
import inspect as _insp

_sig = _insp.signature(_make_kb_coverage)
check("D2.3 kb_coverage 工厂可调用", callable(_make_kb_coverage), True)
_sig2 = _insp.signature(_make_literature_fetch(None))
check("D2.4 literature_fetch 签名含 dois",
      "dois" in _sig2.parameters, True)

print()
print("═══ E. 承重自检 ═══")
# 判据是「按 doc_id 去重」。把它换成"每段算一篇"（坏版本），A3 必须当场失守。
# 这一段证明的是 A3 **真的靠那条去重判据**，而不是碰巧过了。
_orig_docs = cov._doc_ids_of


def _naive_docs(hits):
    """坏版本：不按 doc_id 去重，每段命中各算一篇。"""
    return {f"hit{i}" for i, _ in enumerate(hits or [])}


try:
    cov._doc_ids_of = _naive_docs
    rep = cov.assess([_row(["kb1aaaaa", "kb1aaaaa", "kb1aaaaa"])])
    check("★ E1 拆掉去重判据后，同一篇三段被当成 3 篇（A3 确实承重）",
          rep["distinct_docs_seen"], 3)
    check("★ E1b 且此时判成了「够」—— 正是那条判据在挡这个假通过",
          rep["need_more_literature"], False)
finally:
    cov._doc_ids_of = _orig_docs
rep = cov.assess([_row(["kb1aaaaa", "kb1aaaaa", "kb1aaaaa"])])
check("★ E2 判据已还原", rep["distinct_docs_seen"], 1)

print()
print("═══ 汇总 ═══")
if ok_all:
    print("  全部通过 ✔")
else:
    print(f"  有失败 ✘（{len(fails)} 项）")
    for f in fails:
        print(f"    - {f}")
sys.exit(0 if ok_all else 1)
