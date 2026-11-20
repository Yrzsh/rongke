# -*- coding: utf-8 -*-
"""引文—原文对齐判据：三态、内容级、不阻断、baseline 等价。

两个方向都量（本项目硬纪律）：
  · 正控：照着原文写 ⇒ aligned；
  · 负控：L1 明明「读过」，稿件句却是编的 ⇒ 仍判 unsupported；
  · 误伤负控：短转述 / 没原文 / 页缺失 / 文献表无 doc_id ⇒ 一律 unknown，
    绝不当成「你编了」；
  · 接线负控：不带对齐参数 ⇒ 过程反馈与 11-07 版本逐字节一致。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from rongke import caps
from rongke.process_feedback import build_feedback
from rongke.source_alignment import (
    ALIGNED, UNSUPPORTED, UNKNOWN, DEFAULT_MIN_SCORE,
    align, sources_from_calls,
)
from rongke.tools import ToolRegistry

_fail = 0


def ok(m):
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def eq(got, want, label):
    if got == want:
        ok(label)
    else:
        bad(f"{label}：得到 {got!r}，期望 {want!r}")


def truthy(v, label):
    if v:
        ok(label)
    else:
        bad(label)


DOC = "kbabcd12"
PAGE = ("原生质体在 28 ℃ 下用 0.6 M 甘露醇预处理 30 min，经 PEG 介导融合后，"
        "DAPI 染色显示细胞核融合率为 12.4%。对照组的融合率仅为 1.1%，"
        "差异具有统计学意义，该结果重复三次一致。")
SRC = {DOC: {"2": PAGE}}
GOOD = ("方法：原生质体在 28 ℃ 下用 0.6 M 甘露醇预处理 30 min，"
        "PEG 融合后 DAPI 染色显示细胞核融合率为 12.4%"
        f"〔库·{DOC} p.2〕。")
FAKE = ("结果显示量子色动力学耦合常数 αs 高达 97.3 TeV，"
        "显著优于双亲并已证实为融合子"
        f"〔库·{DOC} p.2〕。")
SHORT = (f"结果表明融合率为 12.4%[1]。\n\n参考文献\n"
         f"[1] 作者. 原生质体融合研究. 〔库·{DOC}〕\n")


def one(text, **kw):
    kw.setdefault("sources", SRC)
    kw.setdefault("read_docs", [DOC])
    return align(text, **kw)["items"][0]


print("== ① 内容级对齐：正负控两方向 ==")
i = one(GOOD)
eq(i["state"], ALIGNED, "正控：照着原文写 ⇒ aligned")
truthy(i["score"] >= DEFAULT_MIN_SCORE, "正控：相似度确实过阈值（不是碰巧）")
eq(i["page_text"], "page", "正控：L2 拿到的是指定页")
eq(i["origin"], "read", "正控：L1 是本次真读过")
truthy("12.4" in i["support"], "正控：回带支撑句原文，能人工复核")

i = one(FAKE)
eq(i["state"], UNSUPPORTED, "负控：编造句 ⇒ unsupported")
truthy(i["score"] < DEFAULT_MIN_SCORE, "负控：相似度真的低于阈值")
eq(i["origin"], "read", "★ 负控的 L1 仍是 read —— 旧探针在这里会放行")
truthy(i["state"] != i["origin"], "★ 内容级判据独立于来源存在性（本轮真正补的一层）")

i = one(GOOD, min_score=0.95)
eq(i["state"], UNSUPPORTED, "阈值负控：抬高 min_score 后正控也不再对齐")
MID = ("原生质体融合实验使用 DAPI 染色观察细胞核，方法重复三次结果一致"
       f"〔库·{DOC} p.2〕。")
i = one(MID, min_score=0.30)
eq(i["state"], ALIGNED, "阈值正控：中间句在 0.30 下对齐（实测 0.46）")
i = one(MID, min_score=DEFAULT_MIN_SCORE)
eq(i["state"], UNKNOWN,
   "同一句在默认 0.55 下不再对齐，且退回 unknown 而非 unsupported"
   "（阈值 + 短句保护双重防线）")

print("== ② 三态与误伤防线（读不到 ≠ 不符合） ==")
i = one(SHORT)
eq(i["state"], UNKNOWN, "短转述 ⇒ unknown，不当 unsupported（实测误伤已修）")
truthy(i["shallow"], "短转述被如实标 shallow，原因可读回")

r = align(GOOD, read_docs=[DOC])
eq(r["items"][0]["state"], UNKNOWN, "没给库内原文 ⇒ unknown，不判 unsupported")
eq(r["summary"]["sources_given"], False, "摘要如实说本次没原文")
truthy("没有提供库内原文" in r["text"], "回包明说只核了出处、没做内容比对")

i = one(f"原生质体在 28 ℃ 下用 0.6 M 甘露醇预处理 30 min，融合率为 12.4%〔库·{DOC} p.9〕。")
eq(i["state"], UNKNOWN, "页码在原文里不存在 ⇒ unknown")
eq(i["page_text"], "missing", "L2 如实标 missing，没拿别的页冒充")

i = one("原生质体融合率为 12.4%，详见文献[1]。\n\n参考文献\n[1] 作者. 某刊. 2020.\n")
eq(i["doc"], "", "编号制但文献表没标 doc_id ⇒ 不猜")
eq(i["state"], UNKNOWN, "没有 doc_id ⇒ unknown")

r = align("这是一段完全没有引注的普通文字。", sources=SRC, read_docs=[DOC])
eq(r["has_alignment"], False, "无引注 ⇒ 没有待核对项")
truthy("不等于" in r["text"] and "没有引文" in r["text"],
       "无引注时明说『判不了 ≠ 没有引文』")

r = align(GOOD + "\n" + FAKE + "\n" + SHORT, sources=SRC, read_docs=[DOC])
s = r["summary"]
eq(s["total"], s["aligned"] + s["unsupported"] + s["unknown"],
   "三态互斥且加总等于总条数")
truthy(s["aligned"] >= 1 and s["unsupported"] >= 1 and s["unknown"] >= 1,
       "同一篇稿里三态都出现，不是恒亮绿灯")
for k in ("对齐只证明", "不判结论对错", "不阻断交付"):
    truthy(k in r["text"], f"边界声明在回包里：{k}")

print("== ③ L1 来源三态与调用台账 ==")
eq(one(GOOD, read_docs=[], searched_docs=[DOC])["origin"], "searched",
   "只检索过 ⇒ searched")
eq(one(GOOD, read_docs=[], searched_docs=[])["origin"], "unrecorded",
   "无调用记录 ⇒ unrecorded（不污名化说『凭记忆』）")

hits = [{"doc_id": DOC, "page_no": 2, "text": PAGE}]
calls = [
    SimpleNamespace(name="kb_read", args={"doc_id": DOC}, ok=True,
                    result=json.dumps({"hits": hits}, ensure_ascii=False)),
    SimpleNamespace(name="kb_search", args={"query": "融合"}, ok=True,
                    result=json.dumps({"hits": [{"doc_id": "kb999999",
                                                 "page_no": 1, "text": PAGE}]})),
    SimpleNamespace(name="kb_read", args={"doc_id": "kbdeadbeef"}, ok=False,
                    result='{"hits": []}'),
]
f = sources_from_calls(calls)
eq(DOC in f["read_docs"], True, "台账：kb_read 的 doc 进 read")
eq("kb999999" in f["searched_docs"], True, "台账：kb_search 的 doc 进 searched")
eq("kbdeadbeef" in f["read_docs"], False, "负控：失败的调用不计入读过")
eq(len(f["sources"]), 2, "台账：两笔 ok 调用的原文都抽到了")
r = align(GOOD, sources=f["sources"], read_docs=f["read_docs"],
          searched_docs=f["searched_docs"])
eq(r["items"][0]["state"], ALIGNED,
   "kb_read 回包形状可直接喂给 align（工具路径闭环）")
r = align(GOOD, sources=SimpleNamespace())
eq(r["items"][0]["state"], UNKNOWN, "坏形状原文不抛错，如实 unknown")

f2 = sources_from_calls([SimpleNamespace(name="kb_read", args={}, ok=True,
                                         result="{ 截断的 json")])
eq(f2["sources"], [], "截断 JSON 不猜、不半解，静默留空")

print("== ④ sources 四种形状 ==")
eq(one(GOOD, sources={DOC: PAGE})["state"], ALIGNED, "形状1：{doc: 全文}")
eq(one(GOOD, sources={DOC: {"2": PAGE}})["state"], ALIGNED, "形状2：{doc: {页: 文本}}")
eq(one(GOOD, sources=hits)["state"], ALIGNED,
   "形状3：kb_search 的 [{doc_id,page_no,text}]")
eq(one(GOOD, sources=json.dumps(SRC, ensure_ascii=False))["state"], ALIGNED,
   "形状4：工具入参里的 JSON 字符串")
eq(one(GOOD, sources="{ 不是 json")["state"], UNKNOWN,
   "坏 JSON 字符串不抛错 ⇒ unknown")

print("== ⑤ 编号制与括号形状 ==")
r = align(SHORT.replace("[1]", "〔1〕").replace("\n[1] 作者", "\n[1] 作者"),
          sources={DOC: PAGE}, read_docs=[DOC])
truthy(len(r["items"]) >= 1, "〔1〕 全角编号也能抽出待核对项")
r = align(SHORT, sources={DOC: PAGE}, read_docs=[DOC])
eq(r["items"][0]["doc"], DOC, "[1] 经文献表映射到 doc_id")
eq(r["items"][0]["page"], None, "编号制没有页码 ⇒ 如实 None，不编页码")

# ★ 11-04 真实产物的形状（`_realp/out/A_artifacts/参考文献.md`）：
#   条目起头是 `〔1〕`，doc_id 写在**下一行**「用于：…〔库·kbXXXX p.4–5〕」，
#   页码还是范围。旧版只认单行 `[1]` ⇒ 真实稿件 17/18 条判不了。
REAL = ("方法：原生质体在 28 ℃ 下预处理 30 min，融合率 12.4%〔1〕。\n\n"
        "参考文献\n"
        "〔1〕郭成金, 赵润. 冬虫夏草原生质体融合初探[J]. 食品科学, 2010.\n"
        f"用于：再生菌落的三类可能来源〔库·{DOC} p.2–3〕\n")
r = align(REAL, sources={DOC: {"2": PAGE}}, read_docs=[DOC])
eq(len(r["items"]), 2,
   "真实形状：著录行的〔1〕没被当正文引注，只留正文引注 + 溯源锚点")
eq(r["items"][0]["doc"], DOC, "★ 全角〔1〕条目 + 跨行 doc_id ⇒ 映射成功")
eq(r["items"][0]["page"], 2, "★ 页码范围 p.2–3 取起始页，不截断错")
eq(r["items"][0]["state"], ALIGNED, "★ 真实形状下内容级对齐真能判 aligned")
eq(r["items"][1]["page"], 2, "溯源锚点自身也读到起始页")

print("== ⑥ 过程反馈接线：只给信息，不加严 ==")
fb0 = build_feedback(GOOD)
eq(fb0["alignment"], None, "不带对齐参数 ⇒ 完全不跑对齐（baseline 等价）")
truthy("引文溯源读回" not in fb0["text"], "不带参数 ⇒ 回包一个字都没多")
nb = build_feedback(GOOD, sources=SRC, read_docs=[DOC])
truthy(nb["alignment"] is not None, "带原文 ⇒ 对齐台账真的挂上")
eq(nb["gaps"], fb0["gaps"], "★ 对齐不进 gaps（不新增打回理由）")
eq(nb["needs_revision"], fb0["needs_revision"], "★ 对齐不改 needs_revision")
eq(nb["score"], fb0["score"], "对齐不改稿面评分")
truthy("引文溯源读回" in nb["text"], "对齐段真的进模型看得见的回包")

print("== ⑦ 能力挂载与配置纪律 ==")
reg = ToolRegistry()
n0 = len(reg)
# ★ 2026-11-09：常驻插件每场都加载，baseline 多出的工具按
#   caps.always_on_tool_names() 单源核，不在这里手写数字。
from rongke.caps import always_on_tool_names
ALWAYS = always_on_tool_names()
b = caps.load_capability_preset("baseline", ctx={"registry": reg})
eq(len(reg), n0 + len(ALWAYS), "baseline 只多出常驻工具（有些插件必须加载）")
eq(b.registered_tools, [], "baseline 撤销清单为空")
p = caps.load_capability_preset("paper-writing", ctx={"registry": reg})
truthy(reg.get("source_alignment") is not None, "paper-writing 挂上 source_alignment")
eq("source_alignment" in p.registered_tools, True, "登记进本场撤销清单")
p2 = caps.load_capability_preset("lit-verify", ctx={"registry": ToolRegistry()})
truthy(p2 is not None and not p2.broken, "lit-verify 也挂得上且健康")
cls = caps.resolve("@rongke/tool-source-alignment")
eq(cls.config_schema["min_score"]["required"], True, "min_score 必填")
eq("default" in cls.config_schema["min_score"], False, "必填项没有 default（schema 纪律）")
eq(caps.schema_discipline(), [], "全仓 schema 纪律未被打破")

t = reg.get("source_alignment")
out = t.fn(text=FAKE, sources=json.dumps(SRC, ensure_ascii=False),
           read_docs=[DOC])
for k in ("未找到支撑", "不判结论对错", "不阻断交付", "结构化台账"):
    truthy(k in out, f"工具回包可读：{k}")

# ★ 观感硬门：预设工具在主视图里不许显示裸英文标识符。
#   11-04 的 `plot_svg` 就是这么漏的 —— 空闲时 /api/tools 不含预设工具，
#   W18 判据测不到，等预设真跑起来用户才看见。这里做成常驻判据。
import re
from pathlib import Path
_js = Path("rongke/web/static/workspace.js").read_text("utf-8")
_m = re.search(r"const PRESET_TOOLHUMAN = \{(.*?)\};", _js, re.S)
_names = set(re.findall(r"(\w+)\s*:", _m.group(1))) if _m else set()
eq("source_alignment" in _names, True,
   "新工具在 PRESET_TOOLHUMAN 里有中文名（不显示裸标识符）")
eq("writing_metrics" in _names, True, "中文名表没被改坏，旧工具仍在")

print()
if _fail:
    print(f"[DONE] 有失败 · {_fail} 项")
    raise SystemExit(1)
print("[DONE] 全部通过")
