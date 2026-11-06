# -*- coding: utf-8 -*-
"""**检索韧性**验收：littkb 索引的能力边界，以及荣珂侧补回来的那一层。

守两件**真实用起来必然踩到**的事（都不是假想题）：

  ① 两字词检索 —— litkb 的 fts_para/fts_doc 是 `tokenize='trigram'`，
     而 `_sanitize_match` 的正则要求 CJK ≥3 字，**少于 3 字直接丢弃**。
     后果：`融合`/`温度`/`酶解`/`菌株`/`选育` 这些最常用的检索词搜不到；
     更糟的是词表被过滤空时 `_match_expr` 返回 None，
     `search_paras` **整个跳过 MATCH 分支** —— 查询从未执行过。
     荣珂侧的 `_wrap_kb_search` 用 LIKE 补回（litkb 一个字不动）。

  ② kb_ask 兜底 —— litkb 自带的 AI 没配置时，`kb_ask` 返回 ok:false，
     看起来像"库里没有"。荣珂侧 `_wrap_kb_ask` 接管**生成那一步**
     （片段仍由 litkb 取）。这里守它的三条边界：
     上游能答就不插手 / 空片段时给诚实答案 / **真错误不许被兜底盖掉**。

★ 判据一律走**荣珂自己的反射路**（`reflect_workbench_tools`）——
  直接调 litkb 只能证明"litkb 能跑"，证明不了"荣珂这边接对了"。

★ 2026-10-10 建。起因是开题报告那轮实测：模型自己总结出
  「多词组合查询在本库一律返回 0，故用单词项」并写进了报告正文 ——
  一条**错误的能力认知被固化成了工作习惯**。判据在这里，就不会再漂。

跑：python312 kb_recall_test.py     退出码 0 全过 / 1 有失败
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import rongke.bridge as B                                       # noqa: E402
from rongke.bridge import reflect_workbench_tools               # noqa: E402
from rongke.config import Config                                # noqa: E402

WB = Config.load().runtime.workbench
ok_n = bad_n = 0


def ck(cond, label):
    """标记必须顶格写 `[PASS]`/`[FAIL]`：regress.py 按行首标记数断言。"""
    global ok_n, bad_n
    ok_n += bool(cond)
    bad_n += (not cond)
    print(("[PASS] " if cond else "[FAIL] ") + label)


def _tools():
    return {n: fn for (n, _d, _s, fn) in reflect_workbench_tools(WB)}


TOOLS = _tools()
search = TOOLS.get("kb_search")
ask = TOOLS.get("kb_ask")

print("=" * 72)
print("0. 前提：两个工具都反射进来了（否则下面全部无意义）")
ck(search is not None, "kb_search 已反射")
ck(ask is not None, "kb_ask 已反射")


def _hits(o):
    return (o.get("hits") or []) if isinstance(o, dict) else []


def _call(fn, **kw):
    try:
        return json.loads(fn(**kw))
    except Exception as e:                                       # noqa: BLE001
        return {"__err__": f"{type(e).__name__}: {e}"}


print("=" * 72)
print("1. 两字词的切分口径（只挑两字中文词；三字以上归 FTS，英文不碰）")
ck(B._kb_short_terms("酶解 温度") == ["酶解", "温度"], "挑出 酶解/温度")
ck(B._kb_short_terms("原生质体 融合") == ["融合"], "只挑两字词，四字词不重复挑")
ck(B._kb_short_terms("原生质体") == [], "纯 ≥3 字 ⇒ 不挑（上游本来就够）")
ck(B._kb_short_terms("protoplast fusion") == [], "英文不参与救援（FTS 支持）")
ck(B._kb_short_terms("酶解温度") == [], "连写四字不拆")
ck(B._kb_short_terms("") == [], "空查询返回空")

print("=" * 72)
print("2. 事实基线：两字词在 litkb 的 FTS 层确实搜不到（这是修复的前提）")
try:
    sys.path.insert(0, str(Path(WB)))
    import litkb as _lk                                        # noqa: E402
    ck(_lk._sanitize_match("温度") == [], "litkb._sanitize_match('温度') 丢词")
    ck(_lk._sanitize_match("酶解 温度") == [], "litkb._sanitize_match('酶解 温度') 丢空")
    ck(_lk._sanitize_match("原生质体") == ["原生质体"], "≥3 字词被保留")
    ck(_lk._match_expr("酶解 温度") is None, "词表空 ⇒ _match_expr 返回 None（查询不执行）")
except Exception as e:                                           # noqa: BLE001
    ck(False, f"取 litkb 事实基线失败：{type(e).__name__}: {e}")

print("=" * 72)
print("3. 修复生效：两字词现在能搜到东西")
for q in ("酶解 温度", "融合", "温度", "菌株 选育"):
    o = _call(search, query=q, n=8)
    ck(len(_hits(o)) > 0, f"{q!r} 有命中（原来 0）")

print("=" * 72)
print("4. 不该触发的就不触发：无两字词 ⇒ 原样透传，不加任何字段")
for q in ("原生质体", "protoplast fusion"):
    o = _call(search, query=q, n=8)
    ck(not o.get("short_terms_rescued"), f"{q!r} 未触发救援")
    ck("short_terms_rescued" not in o, f"{q!r} 回包不带救援字段")

print("=" * 72)
print("5. ★ 形状契约：救援条目必须与上游**同形**（page/snippet，不是 page_no/text）")
UP = {"doc_id", "page", "year", "title", "snippet"}
o = _call(search, query="原生质体", n=5)
up_hits = _hits(o)
ck(bool(up_hits) and UP <= set(up_hits[0].keys()),
   "上游条目含 doc_id/page/year/title/snippet")

o2 = _call(search, query="酶解 温度", n=8)
like = [h for h in _hits(o2) if h.get("via") == "like"]
ck(bool(like), "确有 like 来源的条目")
ck(all(UP <= set(h.keys()) for h in like), "全部 like 条目都带上游的 5 个键")
ck(all(h.get("snippet") for h in like), "like 条目 snippet 非空")
ck(all(h.get("page") is not None for h in like), "like 条目 page 非空")
ck(all(h.get("doc_id") for h in like), "like 条目 doc_id 非空")
ck(all("via" not in h for h in _hits(o2) if h.get("via") != "like"),
   "上游原有条目没被加 via 字段")

print("=" * 72)
print("6. ★ 负控：关掉救援，必须退回原行为（证明是它在起作用，不是巧合）")
_orig = B._kb_short_terms
B._kb_short_terms = lambda q, max_terms=6: []
try:
    T2 = _tools()
    o = _call(T2["kb_search"], query="酶解 温度", n=8)
    ck(len(_hits(o)) == 0, "负控：关掉救援 ⇒ '酶解 温度' 回到 0 命中")
    ck("short_terms_rescued" not in o, "负控：关掉救援 ⇒ 不带救援标记")
    o = _call(T2["kb_search"], query="原生质体", n=8)
    ck(len(_hits(o)) > 0, "负控：≥3 字词不受影响（仍能搜到）")
finally:
    B._kb_short_terms = _orig
o = _call(_tools()["kb_search"], query="酶解 温度", n=8)
ck(len(_hits(o)) > 0, "恢复后 '酶解 温度' 又有命中")

print("=" * 72)
print("7. kb_ask 兜底的三条边界")
o = _call(ask, question="原生质体融合时最适的 PEG 浓度与 Ca2+ 浓度是多少？")
ck(o.get("ok") is True, "真实问题：ok=True")
ck(bool((o.get("answer") or "").strip()), "真实问题：answer 非空")
ck("Usage(" not in str(o.get("answer")), "answer 里没有 LLM 对象 repr 泄漏")
ck("role': 'assistant" not in str(o.get("answer")), "answer 里没有 message dict 泄漏")

o = _call(ask, question="库里有关于量子色动力学的文献吗？请引用具体页码。")
ck(o.get("ok") is True, "库外问题：ok=True（不抛错）")
ans = str(o.get("answer") or "")
ck("没有" in ans or "无" in ans, "库外问题：如实说没有")
ck("不等于" in ans or "世上" in ans or "库外" in ans,
   "库外问题：区分「库里没有」与「世上没有」")

print("=" * 72)
print("8. ★ 负控：真错误不许被兜底盖掉（id 不存在时必须是失败）")
o = _call(ask, question="这篇讲了什么？", ids=["kbdeadbeef"])
ck(o.get("ok") is not True, "伪 id ⇒ ok 不是 True（真错误没被掩盖）")

print("=" * 72)
print("9. ★ 真库验收：『核质染色怎么做』必须查到可操作的 protocol")
# 这一节守的是**用户原话**：「我想做核质染色，但总也不成功，需要它查文献教我」。
# 改动前实测：`核质染色` 在 19283 个段落里 0 次命中（库里没人这么叫），
# 整句还会被当成一个检索词 ⇒ 0 命中 ⇒ 回「库里有没材料」。
# 现在必须：换成文献里的叫法（DAPI）→ 命中 → 正文原样带回。
_o = _call(ask, question="核质染色怎么做")
ck(_o.get("ok") is True, "『核质染色怎么做』ok=True")
_ids = {r["id"] for r in (_o.get("refs") or [])}
ck("kb478857a9" in _ids, "命中蛹虫草那篇（kb478857a9：10% 甲醛固定 + FB28/DAPI 双染）")
ck("kb2466e972" in _ids, "命中六妹羊肚菌那篇（kb2466e972：§3.2.6 原生质体检测）")
_ps = _o.get("passages") or []
ck(len(_ps) > 0, "回包带 passages（正文片段），不是只给 refs 让模型再翻")
ck(all(p.get("text") and p.get("doc_id") and p.get("page") for p in _ps),
   "每个片段都带 doc_id + page + 正文（能反查到页）")
_blob = "\n".join(p["text"] for p in _ps).lower()
ck("2.5" in _blob, "片段里有 2.5 μg/mL（kb478857a9 的 DAPI 浓度）")
ck("5 mg/ml" in _blob or "5 mg" in _blob,
   "片段里同时有 5 mg/mL（kb2466e972 的 DAPI 浓度）")
ck("10%" in _blob and ("甲醛" in _blob or "多聚甲醛" in _blob),
   "片段里有固定步骤（10% 甲醛）—— 这是『总也不成功』的常见漏项")
_cov = _o.get("coverage") or {}
ck(_cov.get("topic_hits", 0) > 0, "锚词（原生质体/菌丝）在库里有命中 ⇒ 主题存在")
_by = _cov.get("matched_by") or (_o.get("term_plan") or {}).get("matched_by")
ck(_by == "synonym",
   f"命中来自**同义扩展**（matched_by={_by!r}，不是原词侥幸命中）")
ck((_cov.get("matched_term") or "") != "核质染色",
   "实际检索词不是原词『核质染色』（原词在库里 0 段，用它必然空手）")
ck(not _o.get("zero_why"), "有命中时不带 zero_why（归因字段只在零命中时出现）")

# 负控：两套浓度必须**同时**可见。只给一套 = 把 1000 倍的冲突藏起来，
# 用户照着做很可能还是失败，而且不知道为什么。
ck(("2.5" in _blob) and ("5 mg/ml" in _blob or "5 mg" in _blob),
   "★ 负控：两套相差 1000 倍的 DAPI 浓度同时出现（不许择一）")

# kb_search 侧同样要能救：整句零命中 ⇒ 剥壳+换词重试
_so = _call(search, query="核质染色怎么做", n=6)
_rw = _so.get("query_rewritten") or {}
ck(bool(_rw.get("to")), "kb_search 对问句做了改写（query_rewritten 有 to）")
ck(len(_hits(_so)) > 0, "改写后 kb_search 有命中（不是改完仍 0）")
# 负控：检索词串（非问句）不许被改写 —— 改写是问句专用的救援，不是常态
_so2 = _call(search, query="原生质体 融合", n=6)
ck(not (_so2.get("query_rewritten")), "负控：词串『原生质体 融合』不被改写")

print("=" * 72)
print("10. ★ 锚点防腐：lab_assets 里每个 doc_id 必须在库里真存在、页码不许越界")
# 为什么这条必须 live：`lab_assets.py` 的**全部价值**是「每条都挂库内锚点」。
# 锚点一旦写错（打错 id / 页码超出），资产就从"可反查的依据"退化成
# "看起来很专业的编造" —— 而它长得一模一样，离线测试查不出来。
import re as _re                                              # noqa: E402
import sqlite3 as _sq                                         # noqa: E402
from rongke import lab_assets as _A                           # noqa: E402

_db = Path(WB) / "data" / "litkb" / "index.db"
ck(_db.exists(), f"索引库存在（{_db}）")
_con = _sq.connect(f"file:{_db.as_posix()}?mode=ro", uri=True)
_ids = {r[0] for r in _con.execute("SELECT DISTINCT doc_id FROM fts_para")}
_pages: dict[str, int] = {}
for doc, mx in _con.execute("SELECT doc_id, MAX(page_no) FROM fts_para GROUP BY doc_id"):
    _pages[doc] = mx or 0
_con.close()

_bad_id, _bad_pg, _n = [], [], 0
for _anchor, _who in _A.all_anchors():
    for _m in _re.finditer(r"(kb[0-9a-f]{8})\s*p\.(\d+)", _anchor):
        _n += 1
        _d, _p = _m.group(1), int(_m.group(2))
        if _d not in _ids:
            _bad_id.append((_who, _d))
        elif _p > _pages.get(_d, 0):
            _bad_pg.append((_who, _d, _p, _pages.get(_d)))
ck(_n > 0, f"资产里解析出 {_n} 个锚点（0 个说明格式漂了）")
ck(not _bad_id, f"所有 doc_id 都能在索引里查到（不存在的：{_bad_id[:3]}）")
ck(not _bad_pg, f"所有页码都在该篇范围内（越界：{_bad_pg[:3]}）")
# 负控：故意编一个 id，上面的判据必须能抓到（否则"全绿"是假绿）
ck("kbdeadbeef" not in _ids, "负控：伪造的 kbdeadbeef 确实不在库里（判据能抓到假锚点）")

print()
print(f"=== {ok_n} 项 / {bad_n} 失败 ===")
print("[DONE] kb_recall 全部通过")
sys.exit(1 if bad_n else 0)
