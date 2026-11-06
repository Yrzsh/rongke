# -*- coding: utf-8 -*-
"""检索词层（纯函数）验收：问句剥壳 / 同义扩展 / 领域锚 / 零命中归因。

为什么这套能**离线**跑：`rongke/bridge.py` 顶层只 import 标准库与 .config/.tools，
workbench 是**函数内**懒导入（`_import_workbench`）。所以这里直接 import 纯函数
不会碰 250MB 的文献库，也不联网 —— 换机器、库没装，这套仍然要绿。

★ 每条"应该发生"的断言都配一条"不该发生"的负控。理由见项目纪律：
  只有一个方向的断言，等于没有断言（99.6% 与 0% 是同一种失败）。

★ 本文件钉住的是 2026-11-02 的实测根因：
  `核质染色` 在全库 19,283 段里出现 **0 次**，而 `DAPI` 有 50 段。
  用户用的是教科书词，文献用的是试剂名 —— 断在词层，不是没全文。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke import bridge as B                                  # noqa: E402

ok = bad = 0


def ck(cond, label):
    """标记必须顶格：regress.py 按行首 `[PASS]`/`[FAIL]` 计数。"""
    global ok, bad
    ok += bool(cond)
    bad += (not cond)
    print(("[PASS] " if cond else "[FAIL] ") + label)


def _terms(q):
    return [t["q"] for t in B._kb_expand_terms(q)["terms"]]


print("=" * 72)
print("1. 问句剥壳：只从串首/串尾剥，不动正文")
ck(B._kb_question_core("核质染色怎么做") == "核质染色",
   "『核质染色怎么做』→『核质染色』（旧实现整串当一个检索词 ⇒ 必 0）")
ck(B._kb_question_core("核质染色如何做？") == "核质染色", "问号 + 如何 也剥掉")
ck(B._kb_question_core("原生质体融合") == "原生质体融合",
   "负控：不是问句就不改一个字")
ck(B._kb_question_core("") == "", "空串返回空（不编一个）")
ck("怎么做" in B._kb_question_core("核质染色怎么做才对"),
   "负控：中段的『怎么做』**不许**剥（那是正文的一部分）")

print("=" * 72)
print("2. 同义扩展：教科书词 → 文献用词 + 领域锚")
p = B._kb_expand_terms("核质染色怎么做")
qs = [t["q"] for t in p["terms"]]
ck("DAPI" in qs and "Hoechst" in qs, "扩出 DAPI / Hoechst（文献真实用词）")
ck("核质染色怎么做" not in qs, "负控：整串问句不许留在检索词里")
ck("核质染色" in qs, "原词保留（但排在同义词之后）")
ck(qs.index("DAPI") < qs.index("核质染色"),
   "同义词排在原词之前（原词是用户的说法，命中率更低）")
ck(p["concepts"] == ["核质染色"], "认出概念 = 核质染色")
ck("原生质体" in p["anchors"], "领域锚带出『原生质体』（单查 DAPI 会被巨噬细胞占领）")
ck(p["anchors"][0] == "原生质体", "锚按表内顺序，第一个是原生质体（AND 只试前 2 个）")

print("=" * 72)
print("3. ★ 负控：不该扩展的绝不扩展")
p2 = B._kb_expand_terms("原生质体融合")
ck(p2["concepts"] == [], "『原生质体融合』不命中任何概念（'融合' 未被当 alias）")
ck(p2["anchors"] == [], "也不带锚（否则会把并列关系变成限制）")
ck([t["src"] for t in p2["terms"]][0] == "exact", "第一个词就是原样，不是同义词")
ck("原生质体融合" in [t["q"] for t in p2["terms"]], "原串保留可查（≥3 字，trigram 支持）")

print("=" * 72)
print("4. ★ 虚词保护：句子不许被切成三字碎片去撞库")
bad_q = "库里有关于量子色动力学的文献吗？请引用具体页码。"
qs4 = _terms(bad_q)
ck("有关于" not in qs4,
   "负控：『有关于』不许进检索词（它撞进三篇诱变育种文献，把『没有』答成『未提及』）")
ck(not any(ch in B._KB_FUNC_CHARS for t in qs4 for ch in t),
   "留在检索词里的都不含虚词（文献吗/请引用 这类已被丢掉）")
p4 = B._kb_expand_terms("量子色动力学")
ck(p4["sliced"] == ["量子色动力学"], "术语样长串才记账为切片")
ck(len(p4["terms"]) >= 3, "术语样长串切出三字片段（整串 0 命中时的退路）")

print("=" * 72)
print("5. ★ 零命中归因三态：读不到 ≠ 不符合")
meta_word = {"coverage": {"anchor_hits": {"原生质体": 1160}, "topic_hits": 1160},
             "term_plan": {"anchors": ["原生质体"], "terms": ["FB28"]}}
v = B._kb_zero_verdict(meta_word, "FB28 浓度")
ck(v["why"] == "word", "锚有命中、词全 0 ⇒ why=word（该换词，不是没文献）")
ck("1160" in v["text"] and "DAPI" in v["text"],
   "word 的回答给出真实下界与可替换的叫法")

meta_topic = {"coverage": {"anchor_hits": {"原生质体": 0}, "topic_hits": 0},
              "term_plan": {"anchors": ["原生质体"], "terms": ["PEG"]}}
v = B._kb_zero_verdict(meta_topic, "PEG")
ck(v["why"] == "topic", "锚也 0 ⇒ why=topic")
ck("不等于" in v["text"], "topic 的回答仍保留『不等于世上没人做过』")

meta_unknown = {"coverage": {"anchor_hits": {}, "topic_hits": 0},
                "term_plan": {"anchors": [], "terms": ["量子色动力学"]}}
v = B._kb_zero_verdict(meta_unknown, "量子色动力学")
ck(v["why"] == "unknown", "认不出锚 ⇒ why=unknown")
ck("分不清" in v["text"], "unknown 必须**明说分不清**（不许冒充 topic）")
ck("不等于" in v["text"], "unknown 也要守住『库里没有 ≠ 世上没有』")

print("=" * 72)
print("6. ★ 排序：带锚的在前（正控与负控**成对**）")
mac = [{"doc_id": f"kbmac{i}", "page_no": 1, "score": 9.0,
        "text": f"macrophage panel {i}: nuclei stained with DAPI"} for i in range(5)]
pro = [{"doc_id": "kbpro", "page_no": 2, "score": 3.0,
        "text": "原生质体悬液经 DAPI 染色后观察核相"}]
with_anchor = B._kb_rank(mac + pro, ["原生质体"])
ck(with_anchor[0]["doc_id"] == "kbpro",
   "带锚：原生质体那段排第一（分数只有 3.0，仍压过 9.0 的巨噬细胞）")
without = B._kb_rank(mac + pro, [])
ck(without[0]["doc_id"] != "kbpro",
   "负控：去掉锚 ⇒ 第一名**必须**变成巨噬细胞（不翻转说明锚是装饰）")
ck(without[0]["doc_id"].startswith("kbmac"), "负控：第一名确实是巨噬细胞那批")
ck(len(without) == len(with_anchor) == 6, "排序不增删条目（只换顺序）")

print("=" * 72)
print("7. 概念表自洽：加了条目就必须能触发")
for key, spec in B._KB_CONCEPTS.items():
    hit = any(B._kb_concept_hit(a) == key for a in spec["aliases"])
    ck(hit, f"概念 {key}：至少一个 alias 能被命中（否则是死条目）")
    ck(bool(spec["terms"]) and bool(spec["anchors"]),
       f"概念 {key}：terms 与 anchors 都不空")
all_aliases = [a for s in B._KB_CONCEPTS.values() for a in s["aliases"]]
ck("融合" not in all_aliases,
   "负控：泛词『融合』不许当 alias（会把『原生质体融合』误吞）")

print()
print(f"=== {ok} 项 / {bad} 失败 ===")
print("[DONE] kb_terms 全部通过")
sys.exit(1 if bad else 0)
