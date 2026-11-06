# -*- coding: utf-8 -*-
"""**实验台账**验收：定性/定量必须分开、缺口必须能算、"下一步"必须机械可复现。

为什么单独立一套（而不是并进 `topic_test.py`）：
这套守的不是"接口在不在"，而是**两条业务判据**，它们错了不会报错，只会让人继续迷茫：

  ① 「没数据」必须能被代码算出来（`len(measures)==0 and len(observes)>0`）。
     如果定性观察能悄悄写进定量字段，这个式子永远为假，工具就会一直说
     "你已经有数据了"，而导师说的"没数据"还在那儿。
  ② 「下一步」必须**机械可复现**，且每条注明来源。
     模型可以润色措辞，但不能发明依据 —— 所以 `render_md(now, asof)` 两次
     必须逐字节相同，`next_actions()` 每条必须带 `source`。

★ 全程**离线**：临时 topics 根目录 + 临时课题，不碰真账。
  唯一的外部依赖是 `lab_assets.py` 里的锚点**格式**（`kbXXXXXXXX p.N`），
  锚点是否真在库里由 `kb_recall_test.py` §9 的 live 部分校验。

跑：python312 lab_test.py      退出码 0 全过 / 1 有失败
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import lab                                        # noqa: E402
from rongke import lab_assets as A                            # noqa: E402
from rongke import topic                                      # noqa: E402

ok_n = bad_n = 0


def ck(cond, label):
    global ok_n, bad_n
    cond = bool(cond)
    ok_n += cond
    bad_n += (not cond)
    print(("[PASS] " if cond else "[FAIL] ") + label)


def raises(fn, label, want=""):
    """负控：必须**抛错**。返回异常文本（给下一条断言查提示语）。"""
    try:
        fn()
    except Exception as e:                                  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        ck((want in msg) if want else True, label + (f"（提示含「{want}」）" if want else ""))
        return msg
    ck(False, label + " —— **没有抛错**（静默接受了脏数据）")
    return ""


TMP = Path(tempfile.mkdtemp(prefix="rk_lab_"))
SLUG = "lab-probe"
topic.ensure_topic(SLUG, title="融合子筛选（探针）",
                   question="三株 23℃ 适应但慢，能否作为融合子？", root=TMP)

print("=" * 72)
print("1. 空台账：不输出空壳块")
ck(lab.digest(SLUG, root=TMP) == "", "空台账 digest 返回空串（不注入空壳块）")
st = lab.state(SLUG, root=TMP)
ck(st["n_events"] == 0 and not st["measures"] and not st["observes"], "空台账 counts 全 0")
ck(lab.gaps(SLUG, root=TMP) == [], "空台账 gaps 为空（没菌株就谈不上缺口）")
ck(lab.matrix(SLUG, root=TMP)["rows"] == [], "空台账 matrix 无行")
ck(lab.next_actions(SLUG, root=TMP) == [], "空台账 next_actions 为空")

print("=" * 72)
print("2. 记现场：5 株（3 株 23℃ 慢 + 2 株快未测）+ 2 亲本")
for code, sp, role in (("S01", "H. sinensis", "candidate"),
                       ("S02", "H. sinensis", "candidate"),
                       ("S03", "H. sinensis", "candidate"),
                       ("S04", "P. hepiali", "candidate"),
                       ("S05", "P. hepiali", "candidate"),
                       ("P1", "H. sinensis", "parent"),
                       ("P2", "P. hepiali", "parent")):
    r = lab.record(SLUG, "strain", root=TMP, code=code, species=sp, role=role,
                   note="原生质体融合再生株" if role == "candidate" else "亲本")
    ck(r["verified"], f"strain {code} 落盘读回一致")
for code in ("S01", "S02", "S03"):
    lab.record(SLUG, "observe", root=TMP, code=code, temp_c=23, growth="slow",
               text="23℃ 可生长但菌落扩展明显慢于亲本",
               evidence="2026-10-28 平板照片")
for code in ("S04", "S05"):
    lab.record(SLUG, "observe", root=TMP, code=code, temp_c=23, growth="fast",
               text="看起来长得快，尚未测指标", evidence="肉眼观察")
st = lab.state(SLUG, root=TMP)
ck(len(st["strains"]) == 7, "7 株进台账")
ck(len(st["observes"]) == 5 and len(st["measures"]) == 0,
   "★ 5 条定性、0 条定量 —— 「没数据」在台账里的形态")

d = lab.digest(SLUG, root=TMP, asof="2026-11-02")
ck("0 条定量" in d, "digest 明写「0 条定量」")
ck("没数据" in d, "digest 点出这就是「没数据」")
ck(len(d) <= 700, f"digest ≤700 字（实测 {len(d)}）")
ck("下一步" in d, "digest 带下一步")

print("=" * 72)
print("3. ★ 负控：定性字不许进定量字段（这是整条链最容易的静默失败）")
raises(lambda: lab.record(SLUG, "measure", root=TMP, code="S01",
                          metric="growth_rate", value="慢", n=3),
       "value='慢' 必须抛错", "必须是")
raises(lambda: lab.record(SLUG, "measure", root=TMP, code="S01",
                          metric="kinase_activity", value=1.2, n=3),
       "未注册指标必须抛错", "未注册")
raises(lambda: lab.record(SLUG, "measure", root=TMP, code="S01",
                          metric="growth_rate", value=1.2, unit="cm/d", n=3),
       "单位不一致必须抛错", "单位")
raises(lambda: lab.record(SLUG, "observe", root=TMP, text="没有菌株编号"),
       "observe 缺 code 必须抛错", "code")
raises(lambda: lab.record(SLUG, "measure", root=TMP, code="S01",
                          metric="growth_rate", value=1.2, unit="mm/d", n="三"),
       "n='三' 必须抛错", "整数")
raises(lambda: lab.record(SLUG, "lab_record", root=TMP, code="S01"),
       "未知 kind 必须抛错", "kind")
raises(lambda: lab.record("../etc", "strain", root=TMP, code="S01"),
       "越界 slug 必须抛错（路径校验只有 topic 一处）")

print("=" * 72)
print("4. 一条定量落盘：矩阵/缺口/下一步同时变化")
r = lab.record(SLUG, "measure", root=TMP, code="S01", metric="growth_rate",
               value=1.2, unit="mm/d", n=3, sd=0.1, method="菌落半径法 24h")
ck(r["verified"], "measure 落盘读回一致（verified 不是自称）")
mx = lab.matrix(SLUG, root=TMP)
row1 = next(x for x in mx["rows"] if x["code"] == "S01")
ck(row1["cells"]["growth_rate"]["value"] == 1.2, "矩阵里 S01 生长速率 = 1.2")
ck(row1["cells"]["biomass"] is None, "S01 菌粉得率仍是空格（None，不是 0）")
gp = lab.gaps(SLUG, root=TMP)
ck(not any(g["code"] == "S01" and g["metric"] == "growth_rate" for g in gp),
   "S01 的生长速率不再算缺口")
ck(any(g["code"] == "S01" and g["metric"] == "biomass" and g["kind"] == "missing" for g in gp),
   "S01 的菌粉得率仍算缺口")
ck(any(g["kind"] == "no_control" for g in gp),
   "★ 候选有数、亲本没数 ⇒ 报 no_control（「快慢」缺参照系）")
ck(not any(g["code"] == "P1" for g in gp), "亲本本身不进缺口清单（它不是被改造的对象）")

print("=" * 72)
print("5. n 不足：测了但 n=1，不算重复")
lab.record(SLUG, "measure", root=TMP, code="S02", metric="growth_rate",
           value=0.9, unit="mm/d", n=1, method="单皿")
gp = lab.gaps(SLUG, root=TMP)
few = [g for g in gp if g["code"] == "S02" and g["metric"] == "growth_rate"]
ck(bool(few) and few[0]["kind"] == "few_reps", "n=1 ⇒ kind=few_reps")
ck("n=1" in few[0]["why"], "缺口说明里点明「n=1 是观察，不是重复」")

print("=" * 72)
print("6. 批次：到点了 ⇒ 排在下一步第一条（样品有时效）")
lab.record(SLUG, "batch", root=TMP, id="B01", code="S04", vessel="250mL 摇瓶",
           medium="PDB", temp_c=23, started="2026-10-25", expect_days=5, status="running")
acts = lab.next_actions(SLUG, asof="2026-11-02", root=TMP)
ck(bool(acts) and acts[0]["source"] == "台账·batch", "到点批次排第一，来源=台账·batch")
ck("B01" in acts[0]["what"], "第一条点名批次 B01")
ck("8 天" in acts[0]["what"] or "已 8" in acts[0]["what"], "算得出已 8 天")
ck("g/100mL" in acts[0]["what"], "★ 下一步带**单位**（不是「去测一下」）")
ck(all(a.get("source") for a in acts), "每条 next_action 都带 source（模型不许发明依据）")
ck(all(a.get("why") for a in acts), "每条 next_action 都带 why")
# ★ 缺口动作**按指标聚合**，不按株。第一版是"每株一条"，5 株会产出 5 条
#   几乎相同的句子，把前三条（批次/待办/身份证据）全挤掉。
_gap_acts = [a for a in acts if a["source"] == "台账·缺口"]
ck(len(_gap_acts) <= 3, f"缺口动作被封顶（实测 {len(_gap_acts)} 条 ≤ 3）")
combined = [a for a in _gap_acts if a["what"].count("S0") >= 2]
ck(bool(combined), "★ 一条缺口动作覆盖多株（一次上平板测完，而不是分株重开实验）")
ck(all("一次测完" in a["what"] for a in _gap_acts), "缺口动作的话术是「一次测完」")
ck(not [a for a in _gap_acts if a["what"].startswith("S01 补测")],
   "负控：不再出现「S01 补测…」这种逐株一条的旧形态")

print("=" * 72)
print("7. ★ 身份证据：没提到 ⇒ 提示；一旦提到 ⇒ 立刻不再提示（正负控成对）")
ck(any(a["source"] == "资产·IDENTITY_CHAIN" for a in acts),
   "台账没提过核相/染色 ⇒ 提示「做核相（最便宜的身份证据）」")
lab.record(SLUG, "todo", root=TMP, id="T01", what="摸核质染色条件（DAPI，甲醛固定）",
           due="2026-11-06", status="open")
acts2 = lab.next_actions(SLUG, asof="2026-11-02", root=TMP)
ck(not any(a["source"] == "资产·IDENTITY_CHAIN" for a in acts2),
   "负控：待办已提到核相 ⇒ 不再重复提示（不翻转说明判据是装饰）")

print("=" * 72)
print("8. update / close 折叠：永不改写已有行")
lab.record(SLUG, "update", root=TMP, id="S01", fields={"note": "已补生长速率"})
st = lab.state(SLUG, root=TMP)
ck(st["strains"]["S01"]["note"] == "已补生长速率", "update 折叠生效")
ck(len(lab.events(SLUG, root=TMP)) >= 3, "事件流仍在（追加，不覆盖）")
n_before = len(lab.events(SLUG, root=TMP))
lab.record(SLUG, "close", root=TMP, id="T01", how="参数已定，出第一张核相图")
ck(len(lab.events(SLUG, root=TMP)) == n_before + 1, "close 也是追加一行（不改历史）")
ck(lab.state(SLUG, root=TMP)["todos"]["T01"]["status"] == "closed", "close 后待办状态为 closed")
raises(lambda: lab.record(SLUG, "update", root=TMP, id="S01"),
       "空 update（没 fields）必须抛错", "fields")
# 改名（占位编号 → 真实株号）：历史观察必须跟着搬，否则那株会从矩阵里消失
lab.record(SLUG, "update", root=TMP, id="S05", fields={"code": "F05"})
_st = lab.state(SLUG, root=TMP)
ck("F05" in _st["strains"] and "S05" not in _st["strains"], "改名后菌株主键搬到新名")
ck(len(_st["observes"]) == 5, "改名不丢历史观察（条数仍是 5）")
ck(any(o.get("code") == "F05" for o in _st["observes"]), "历史观察的 code 也跟着搬了")
lab.record(SLUG, "update", root=TMP, id="F05", fields={"code": "S05"})   # 还原

print("=" * 72)
print("9. ★ 确定性：同输入两次渲染逐字节相同（否则不能当基线）")
md1 = lab.render_md(SLUG, now="2026-11-02 15:00", asof="2026-11-02", root=TMP)
md2 = lab.render_md(SLUG, now="2026-11-02 15:00", asof="2026-11-02", root=TMP)
ck(md1 == md2, "render_md 两次逐字节相同")
ck("—" in md1, "空格显示为「—」（留白会让「没测」看起来像「没这项」）")
ck("kb60993237" in md1, "方法来源带库内 doc_id（可在库里反查）")
ck("未注册" not in md1 and "None" not in md1, "渲染里没有 None / 未注册 之类的泄漏")
ck("n=3" in md1, "矩阵里带 n（n 是「这是数据」的必要条件）")

print("=" * 72)
print("10. CSV：定性行也在（evidence_type=qual），否则画图会漏株")
cs = lab.render_csv(SLUG, root=TMP)
ck("evidence_type" in cs.splitlines()[0], "CSV 表头有 evidence_type")
ck(",qual," in cs, "定性观察以 evidence_type=qual 出现")
ck(",quant," in cs, "定量测量以 evidence_type=quant 出现")
ck("growth_rate" in cs, "定量行带指标 key")

print("=" * 72)
print("11. 汇报落盘：LAB.md / LAB.csv 写进课题目录并读回比对")
rep = lab.write_report(SLUG, now="2026-11-02 15:00", asof="2026-11-02", root=TMP)
ck(rep["verified"], "write_report verified=True（读回与内存一致）")
d_ = topic.topic_dir(SLUG, root=TMP)
ck((d_ / "LAB.md").exists() and (d_ / "LAB.csv").exists(), "两个文件都在课题目录里")
ck(not (d_ / "LAB.jsonl").samefile(d_ / "LAB.md"), "台账本体与渲染视图是两个文件（视图可重生成）")

print("=" * 72)
print("12. 领域资产：每条锚点必须**格式可解析**（真库存在性由 kb_recall §9 验）")
pat = re.compile(r"kb[0-9a-f]{8} p\.\d")
bad = [(a, who) for a, who in A.all_anchors()
       if not pat.search(a) and not pat.search(a.replace("；", " "))]
ck(not bad, f"锚点全部带 doc_id+页码（不达标：{bad[:3]}）")
ck(len(A.TARGET_METRICS) >= 5, "定量指标 ≥5 项")
ck(all(m.get("unit") and m.get("n_min") for m in A.TARGET_METRICS),
   "每个指标都有 unit 与 n_min（没有单位的指标不是指标）")
ck(len(A.DAPI_PROTOCOLS) == 2, "染色方案恰好两条（必须并列，不许择一）")
ck(any(c["ratio"] == 1000 for c in A.PROTOCOL_CONFLICTS),
   "★ 1000 倍浓度冲突被显式记录（不许被合并掉）")
ck(all(f.get("next") for f in A.STAIN_FAILURE_BRANCHES), "染色失败三分支都有 next")
ck(A.IDENTITY_CHAIN[0]["key"] == "nuclear", "身份证据链第一环是核相（最便宜）")

print("=" * 72)
print("13. 负控：资产不许凭空写数（引用必须能被反查）")
_A_TEXT = str(A.TARGET_METRICS) + str(A.DAPI_PROTOCOLS) + str(A.IDENTITY_CHAIN)
ck("kb" in _A_TEXT, "资产里有 doc_id 引用")
ck(all("anchor" in m or "source" in m for m in A.TARGET_METRICS), "每个指标都带 marker")
ck("2.17" in _A_TEXT and "0.87" in _A_TEXT, "参照值来自 kb60993237 的表格（2.17 vs 0.87）")

print("=" * 72)
print("14. ★ 方法卡：资产必须**落到模型手里**，且参数不许自己长出来")
# 这一节存在的理由：`lab_assets` 曾经整整一轮**只有测试引用它** ——
# 每一条都挂着 doc_id + 页码，却没有任何工具读，模型永远看不见。
# 测试全绿而能力为零，这是最坏的一种"通过"。
_m_stain = lab.render_method_card("核质染色怎么做", SLUG, root=TMP)
ck(_m_stain["need"] == "staining", "中文问句「核质染色怎么做」路由到染色卡（不让模型猜枚举）")
ck(_m_stain["md"].count("#") > 3 and len(_m_stain["md"]) > 1500,
   f"染色卡有实体内容（{len(_m_stain['md'])} 字）")
ck(all(p["source"].split(" ")[0][:2] == "kb" for p in A.DAPI_PROTOCOLS),
   "两条方案都带 doc_id 出处")
ck("1000×" in _m_stain["md"] and "5 μg/mL" in _m_stain["md"] and "5 mg/mL" in _m_stain["md"],
   "★ 两条方案与 1000 倍冲突**同时**出现在卡里（不许择一）")
ck(all(f["symptom"] in _m_stain["md"] for f in A.STAIN_FAILURE_BRANCHES),
   "失败三分诊逐条在卡里（先对症状再改配方）")
ck(all(f"第 {s['step']} 步" in _m_stain["md"] for s in A.STAIN_FIRST_RUN),
   "第一版下手顺序在卡里（阳性对照排在最前）")
ck("芽生孢子" in _m_stain["md"] and "融合子菌丝" in _m_stain["md"],
   "★ 明说「你的样本不是它们用的样本」（浓度可照抄、前处理不能）")
# 参数表要**够得着就能照做**：离心条件、壁染通道、原文原句都是上一版缺的
# （它们当时只活在我手写的一张 md 里 —— 而手写产物工具重出不来的知识，等于没有）。
ck("8 000 r/min" in _m_stain["md"], "离心条件进了参数表（照做就缺这一行）")
ck("561 nm" in _m_stain["md"], "壁染通道进了参数表（找错通道＝什么都没看到）")
ck("原文（照抄" in _m_stain["md"], "原文原句照抄在卡里（可回库逐字核对）")
ck(A.STAIN_JUDGEMENT["label"].split("（")[0] in _m_stain["md"],
   "★ 推断部分单独成节并标明「不是原文」（推断混进参数表，下一轮就被当成引用）")
ck("S01" in _m_stain["md"] and "亲本" in _m_stain["md"],
   "卡里落到**这台**台账的株号上（阳性对照用亲本菌丝，不是泛泛一句「做对照」）")

_m_id = lab.render_method_card("identity", SLUG, root=TMP)
ck(all(x["label"] in _m_id["md"] for x in A.IDENTITY_CHAIN),
   "身份链四环逐条在卡里")
ck("未记录" in _m_id["md"] and "≠" in _m_id["md"],
   "★ 未记录 ≠ 没做：卡里必须写清这层区别（否则用户会以为被判定没做）")
ck("已记录" in _m_id["md"],
   "正控：待办里提过染色 ⇒ 核相那一环显示「已记录」（判据会翻转，不是常量）")

_m_q = lab.render_method_card("quant", SLUG, root=TMP)
ck("2.17" in _m_q["md"] and "0.87" in _m_q["md"],
   "定量卡带参照值（2.17 vs 0.87，来自 kb60993237 表格）")
ck("按指标聚合" in _m_q["md"], "缺口**按指标聚合**，不逐格列（否则 25 行同义句）")
ck("株待补" in _m_q["md"], "缺口写了「几株待补」，这一句才是可执行的")

# 负控①：同一 need 两次渲染逐字节相同（否则不能当基线、也不能当证据）
ck(lab.render_method_card("staining", SLUG, root=TMP)["md"] == _m_stain["md"],
   "确定性：同一台账 + 同一资产 ⇒ 逐字节相同")
# 负控②：资产里没有的方法必须**抛错并列出可用说法**，不许静默给一张不相干的卡
_m = raises(lambda: lab.render_method_card("western blot", SLUG, root=TMP),
            "负控：资产里没有的方法**抛错**（不许拿染色卡糊弄过去）", "staining")
ck("quant" in _m and "identity" in _m, "错误提示里列全了三张卡的名字（可操作）")

# 落盘：写进课题目录并读回比对；重写是覆盖而不是追加（它是视图，不是台账）
_w1 = lab.write_method_card(SLUG, "staining", root=TMP)
_p = Path(_w1["dir"]) / _w1["file"]
ck(_w1["ok"] and _p.is_file(), f"方法卡落盘并读回一致（{_w1['file']}）")
_n1 = len(lab.events(SLUG, root=TMP))
_w2 = lab.write_method_card(SLUG, "staining", root=TMP)
ck(len(lab.events(SLUG, root=TMP)) == _n1,
   "负控：出卡**不动台账事件**（卡是视图，不是一笔记录）")
ck(_p.read_text(encoding="utf-8") == _w2["md"], "重复出卡是覆盖，不是追加")

print("=" * 72)
print("15. ★ 作废（retract）：台账唯一的更正手段，且**必须精确**")
# 这一节存在的原因是一次真实事故：建本课题台账时脚本写重了 5 条 observe，
# 当时**没有作废这个操作**，唯一的办法是手工去改 LAB.jsonl —— 而台账 append-only
# 的全部意义就是"改过什么能看出来"，手改正好把这段抹掉。
# 所以这里守两条：① 作废本身是一次追加（不改历史）；② 作废必须点名到唯一一条。
_n0 = len(lab.events(SLUG, root=TMP))
_live0 = lab.state(SLUG, root=TMP)["n_events"]
for _ in range(2):                     # 模拟"脚本重复写入"：同一条 observe 记两遍
    lab.record(SLUG, "observe", root=TMP, code="S03", temp_c=23, growth="slow",
               text="重复写入的同一条观察", evidence="重跑脚本")
_dup_msg = raises(lambda: lab.record(SLUG, "retract", root=TMP,
                                    target={"kind": "observe", "code": "S03",
                                            "text": "重复写入的同一条观察"},
                                    reason="脚本重复写入"),
                  "负控：target 匹配到 2 条 ⇒ **抛错**（作废不许顺手批量）", "同时匹配")
ck("#" in _dup_msg and "observe" in _dup_msg,
   "报错里列出候选事件签名（否则模型手上没有列事件的工具，只能瞎猜序号）")
# 按 seq 点名：只作废后写的那一条，前一条必须留着
_evs = lab.events(SLUG, root=TMP)
_last_seq = _evs[-1].get("seq")
ck(isinstance(_last_seq, int) and _last_seq == len(_evs),
   f"新事件带 seq（#{_last_seq}），且等于当前条数（供 retract 指认）")
_r = lab.record(SLUG, "retract", root=TMP, seq=_last_seq, reason="脚本重复写入，留一条即可")
ck(_r["verified"], "retract 落盘读回一致")
_st = lab.state(SLUG, root=TMP)
ck(_st["n_retracted"] == 1, "折叠出 1 条已作废")
ck(_st["n_events"] == _live0 + 1, "作废后**有效**事件只多 1 条（2 条 observe 中 1 条被作废）")
ck(len(lab.events(SLUG, root=TMP)) == _n0 + 3,
   "负控：盘上事件数是 3 条（2 写 + 1 作废）—— 作废是追加，不是删除")
ck(len([o for o in _st["observes"] if o.get("text") == "重复写入的同一条观察"]) == 1,
   "正控：同一条观察只剩 1 条进状态")
ck(_st["retracted"][0]["reason"].startswith("脚本重复写入"), "作废理由被保留（三个月后还说得清）")
# 负控三条：不写理由、重复作废、作废一条作废记录
raises(lambda: lab.record(SLUG, "retract", root=TMP, seq=_last_seq),
       "负控：作废不写 reason ⇒ 抛错", "reason")
raises(lambda: lab.record(SLUG, "retract", root=TMP, seq=_last_seq, reason="再来一次"),
       "负控：同一条作废两次 ⇒ 抛错", "已经作废")
raises(lambda: lab.record(SLUG, "retract", root=TMP,
                          seq=len(lab.events(SLUG, root=TMP)), reason="作废作废记录"),
       "负控：作废一条作废记录 ⇒ 抛错", "作废记录")
raises(lambda: lab.record(SLUG, "retract", root=TMP, seq=9999, reason="越界"),
       "负控：seq 越界 ⇒ 抛错并报出总条数", "没有 #9999")
raises(lambda: lab.record(SLUG, "retract", root=TMP,
                          target={"kind": "observe", "code": "NO_SUCH"},
                          reason="找不到"),
       "负控：target 一条都不匹配 ⇒ 抛错且给出最近的签名", "最近的几条")
# 作废一条**定量**：矩阵与缺口必须同时回滚（这是"作废有用"的唯一证明）
_m = lab.record(SLUG, "measure", root=TMP, code="S03", metric="biomass",
                value=2.17, unit="g/100mL", n=3)
ck(next(x for x in lab.matrix(SLUG, root=TMP)["rows"] if x["code"] == "S03")
   ["cells"]["biomass"]["value"] == 2.17, "正控：先记一条定量，矩阵里有了")
lab.record(SLUG, "retract", root=TMP, seq=_m["event"]["seq"], reason="量错了，重测")
ck(next(x for x in lab.matrix(SLUG, root=TMP)["rows"] if x["code"] == "S03")
   ["cells"]["biomass"] is None, "★ 作废后矩阵回到空格（None，不是 0）")
ck(any(g["code"] == "S03" and g["metric"] == "biomass" for g in lab.gaps(SLUG, root=TMP)),
   "★ 作废后这一格重新算缺口（缺口的算法与矩阵同源）")
_md_ret = lab.render_md(SLUG, now="2026-11-02 15:00", asof="2026-11-02", root=TMP)
ck("已作废" in _md_ret and "量错了，重测" in _md_ret,
   "汇报页顶部报作废数，附表列理由（数字与上周不同时，这一节就是解释）")

print("=" * 72)
print("16. ★ 占位编号：临时株号不许在纸上冒充真株号")
# 「S01–S05」是我起的临时名，用户还没给真实株号。汇报页把它印得和真株号一模一样，
# 读的人（导师）无从分辨 —— 这就是一次"看着像事实"的编造，且错在源头。
_ph = lab.record(SLUG, "strain", root=TMP, code="S09", role="candidate",
                 placeholder=True, note="占位编号，等用户给真实株号")
ck(_ph["event"].get("placeholder") is True, "strain 落库带 placeholder=true")
_row9 = next(x for x in lab.matrix(SLUG, root=TMP)["rows"] if x["code"] == "S09")
ck(_row9["placeholder"] is True, "matrix 行带 placeholder（一路传到渲染层）")
_row1 = next(x for x in lab.matrix(SLUG, root=TMP)["rows"] if x["code"] == "S01")
ck(_row1["placeholder"] is False, "负控：没标的株不带占位标记（不是常量）")
_md_ph = lab.render_md(SLUG, now="2026-11-02 15:00", asof="2026-11-02", root=TMP)
ck("S09\\*" in _md_ph, "汇报页给占位编号加 `*`")
ck("占位编号" in _md_ph, "汇报页有脚注解释 `*` 是什么")
ck("占位" in lab.digest(SLUG, asof="2026-11-02", root=TMP),
   "digest 也点出来（模型靠它决定转述时要不要加说明）")
lab.record(SLUG, "update", root=TMP, id="S09",
           fields={"code": "HS-23-07", "placeholder": False})
_md_ph2 = lab.render_md(SLUG, now="2026-11-02 15:00", asof="2026-11-02", root=TMP)
ck("HS-23-07" in _md_ph2 and "S09\\*" not in _md_ph2,
   "正控：给了真株号并清标记 ⇒ 星号消失（判据会翻转，不是装饰）")

print("=" * 72)
print("17. ★ 批次没填预计周期：不编天数，给「用数据定周期」的做法")
lab.record(SLUG, "batch", root=TMP, id="B02", code="S01", started="2026-10-30",
           temp_c=23, status="running")          # 故意不给 expect_days
_a2 = lab.next_actions(SLUG, asof="2026-11-02", root=TMP, limit=8)
_b02 = [a for a in _a2 if a.get("id") == "B02"]
ck(bool(_b02), "无 expect_days 的批次仍进「下一步」（否则正在发酵的人一条提示都没有）")
ck("没填预计周期" in _b02[0]["what"], "明说「没填预计周期」，不替用户编一个天数")
ck("每 24 h 取样" in _b02[0]["what"], "给的是可执行做法：每日取样画曲线")
ck("kb60993237" in _b02[0]["why"], "周期参照带库内 doc_id（不是通用经验值）")
ck("诱变" in _b02[0]["why"] or "参照" in _b02[0]["why"], "说清参照来自别的研究")
_b01 = [a for a in _a2 if a.get("id") == "B01"]
ck(bool(_b01) and "没填预计周期" not in _b01[0]["what"],
   "负控：填了 expect_days 的批次走「到点」分支，不走这条（不是所有批次都报）")
ck(lab.render_method_card("quant", SLUG, root=TMP)["md"] != "",
   "定量卡照常可出（新增资产没打断渲染）")

print("=" * 72)
print("18. ★ 对话版（say）：不打开文件就能拿到答案")
_say_st = _m_stain["say"]
ck(bool(_say_st) and len(_say_st) <= 700,
   f"染色卡带 say（{len(_say_st)} 字，≤700）—— 一句话提问也能得到动作")
ck("5 μg/mL" in _say_st and "5 mg/mL" in _say_st,
   "★ say 里两个浓度都在（只留一个＝替用户挑了标准答案）")
ck("1000 倍" in _say_st, "say 明说两篇差多少倍（否则用户看不出这是冲突）")
ck("融合子菌丝" in _say_st and "前处理不能照抄" in _say_st,
   "say 点明「你的样本不是它们用的样本」")
ck(".md" not in _say_st and "方法卡-" not in _say_st,
   "★ 负控：say 里不出现文件名（回一句「已生成 xx.md」就是卡点）")
ck("方案 A" not in _say_st, "负控：不引用卡里才定义的名词（对话里没人看过那张卡）")
_say_q = lab.render_method_card("quant", SLUG, root=TMP)["say"]
ck("mm/d" in _say_q and "g/100mL" in _say_q,
   "定量卡的 say 带单位（说「测生长速率」不说单位，测完也不能用）")
_say_b = lab.say_report(SLUG, asof="2026-11-02", root=TMP)
ck(bool(_say_b) and len(_say_b) <= 700, f"台账说有 say（{len(_say_b)} 字）")
ck("下一步" in _say_b, "say 给的是动作（不是统计）")
ck("0 条" not in _say_b,
   "负控：已经测过的课题**不**说「定量 0 条」（不是无条件贴标签）")
ck(_say_b.count("\n") <= 8, "say 行数收敛（不复述整张表）")
ck("|" not in _say_b, "负控：say 里没有表格（对话里表格读不了）")
# 正控：只有定性、0 条定量的课题 —— 必须点出「没数据」
topic.ensure_topic("probe-only-observe", title="只有观察没有定量", root=TMP)
lab.record("probe-only-observe", "strain", root=TMP, code="X01", role="candidate")
lab.record("probe-only-observe", "observe", root=TMP, code="X01", growth="slow",
           text="长得慢")
_say_empty = lab.say_report("probe-only-observe", asof="2026-11-02", root=TMP)
ck("定量数据是 0 条" in _say_empty,
   "★ 正控：只有观察 ⇒ say 直接说「定量 0 条」（导师催的就是这个）")
raises(lambda: lab.say_report("no-such-topic", root=TMP),
       "负控：不存在的课题 ⇒ say 报错而不是编一段（与 lab_path 同一条纪律）",
       "课题不存在")

print()
print(f"=== {ok_n} 项 / {bad_n} 失败 ===")
if not bad_n:
    print("[DONE] lab 全部通过")
sys.exit(1 if bad_n else 0)
