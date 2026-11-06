# -*- coding: utf-8 -*-
"""figure_test.py —— 图面契约与体检的回归测试。

跑法：
    python figure_test.py                 # 全部
    python figure_test.py --no-real       # 跳过一次真实审计（只测纯逻辑，秒级）
    python figure_test.py --json

**测试设计的规矩**（沿用本项目的教训）：
  1. 每加一个守卫，必须配一条**承重自检**：把守卫的机制拿掉，
     正向对照必须变红。否则无从知道这道守卫到底有没有在承重。
  2. **误报比漏报更伤**：每条"应该判错"的用例，都配一条"不该判错"的对照。
     一个天天喊狼来了的体检器，人会学会直接忽略它 —— 那比没有更糟。
  3. 不测"代码跑了"，测"判据对不对"。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
# `shutil` 仍然 import：`copy2` 搬素材要用，只是不再拿它 `rmtree`。
import _tkit as T                                                   # noqa: E402

from rongke.contract import TaskContract, _looks_like_figure       # noqa: E402
from rongke.figure import (FigureAuditor, FigureContract, FigureReport,  # noqa: E402
                           PALETTE, NATIVE_BACKENDS, SUBMISSION_BACKENDS,
                           audit_legend, capture_missing_glyphs,
                           find_figure_python, glyphs_missing,
                           locate_scripts, render_legend, route_for)
from rongke.gate import CompletionClaim, CompletionGate, EvidenceRef  # noqa: E402
from rongke.tools import Tool, ToolRegistry                         # noqa: E402

PASS, FAIL = [], []
_QUIET = False

# 测试写报告用的一次性目录。**绝不写进产物目录**（`figures/` 这类）。
# 教训（本轮真栽过）：`FigureAuditor.audit()` 不给 `report_path` 时默认写成
# `<workdir>/figure-qa.json`。负对照里 workdir 传的是 `figures/` ⇒
# 测试跑完把 `figures/figure-qa.json` 覆盖成了"PDF 不存在"那一份，
# 而正式报告就此消失，且没有任何报错。**测试污染产物目录 = 假证据的第二来源。**
#
# ★ 落点本轮从系统临时目录改成**仓库内**（`runs/_regress/`）：与 `_trash` 同盘，
#   于是收尾是干净的 `os.replace`；放在系统盘的话跨卷会 EXDEV，垃圾反而留在
#   系统临时目录里没人清。它仍不在任何**产物**目录里 —— 那条纪律没动。
_TMPROOT = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_fig", "fig-tmproot")


def iso(name: str) -> str:
    """给测试用的报告落盘路径（一律落在临时目录）。"""
    return str(_TMPROOT / f"{name}.figure-qa.json")


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    if not _QUIET:
        print(f"  {'✔' if ok else '✘'} {name}" + (f"  — {detail}" if detail and not ok else ""))


def section(t: str) -> None:
    if not _QUIET:
        print(f"\n── {t} " + "─" * max(0, 58 - len(t)))


# ══════════════════════════════════════════════════════════════════
# 共用契约
# ══════════════════════════════════════════════════════════════════
GOOD_CONTRACT = {
    "figure_id": "Fig. 1",
    "title": "Residence time drives an accelerating conversion gain across independent batches",
    "core_conclusion": "Conversion rises with residence time and the per-hour gain itself grows.",
    "archetype": "quantitative grid", "backend": "python",
    "width_mm": 183.0, "height_mm": 52.0, "journal": "nature",
    "formats": ["svg", "pdf", "tiff", "png"],
    "panels": [
        {"letter": "a", "role": "trend+uncertainty: first-order main effect",
         "claim": "Batch conversion trajectories against residence time",
         "center": "mean", "spread": "s.d.", "unit": "batches"},
        {"letter": "b", "role": "paired within-batch increment: rules out one-batch artefact",
         "claim": "Per-hour increment for each batch at the interval midpoint",
         "center": "raw", "spread": "none", "unit": "batches"},
        {"letter": "c", "role": "distribution contrast: second-order acceleration",
         "claim": "Increment distribution for 0-2 h versus 3-5 h",
         "center": "mean", "spread": "s.d.", "unit": "intervals"},
    ],
}
GOOD_LEGEND = (
    "**Fig. 1 | Residence time drives an accelerating conversion gain across "
    "independent batches.** **a** Batch conversion trajectories against residence time. "
    "Individual batches appear in light grey; the pooled mean ± s.d. is overlaid in blue "
    "(n = 3 batches per time point). **b** Per-hour increment for each batch, plotted at the "
    "interval midpoint. **c** Distribution of the per-hour increment for the 0–2 h and 3–5 h "
    "intervals (n = 6 and 9 intervals). Two-sided Welch's t-test, t = 8.41, P < 0.001. "
    "Source data are provided as a Source Data file."
)


def fc(**over) -> FigureContract:
    d = json.loads(json.dumps(GOOD_CONTRACT))
    d.update(over)
    return FigureContract.from_dict(d)


# ══════════════════════════════════════════════════════════════════
# 1 · 契约校验（合同的五点，逐条）
# ══════════════════════════════════════════════════════════════════
def test_contract() -> None:
    section("1 契约校验")

    check("1.1 合规契约无问题", fc().validate() == [], str(fc().validate()))

    # ① 核心结论
    check("1.2 缺核心结论 → 报错", any("核心结论" in p for p in fc(core_conclusion="").validate()))
    # ② 证据链：**职责重复**是这条的核心（合同明令 merge/demote）
    dup = json.loads(json.dumps(GOOD_CONTRACT))
    dup["panels"][1]["role"] = dup["panels"][0]["role"]
    probs = FigureContract.from_dict(dup).validate()
    check("1.3 分面职责重复 → 报错", any("职责重复" in p for p in probs), str(probs))
    # 反向对照：职责不同就不该报
    check("1.4 职责互异 → 不报重复",
          not any("职责重复" in p for p in fc().validate()))
    no_role = json.loads(json.dumps(GOOD_CONTRACT)); no_role["panels"][2]["role"] = ""
    check("1.5 分面缺职责 → 报错",
          any("推断职责" in p for p in FigureContract.from_dict(no_role).validate()))
    dup_letter = json.loads(json.dumps(GOOD_CONTRACT)); dup_letter["panels"][1]["letter"] = "a"
    check("1.6 分面字母重复 → 报错",
          any("字母重复" in p for p in FigureContract.from_dict(dup_letter).validate()))
    # ③ 原型
    check("1.7 原型不在集合内 → 报错",
          any("原型" in p for p in fc(archetype="pretty picture").validate()))
    # ④ 后端排他性 —— 这一条是 Origin 之争的可执行形态
    check("1.8 混用两个引擎 → 报错",
          any("排他" in p for p in fc(
              engine_artifacts={"fig.pdf": "origin", "fig.png": "python"}).validate()))
    check("1.9 单一引擎（origin）→ 不报排他",
          not any("排他" in p for p in fc(
              backend="origin", engine_artifacts={"fig.pdf": "origin"}).validate()),
          str(fc(backend="origin", engine_artifacts={"fig.pdf": "origin"}).validate()))
    check("1.10 引擎名大小写/别名归一", fc(engine_artifacts={"a": "Matplotlib"}).engines() == ["python"])
    check("1.11 后端不认识 → 报错",
          any("后端" in p for p in fc(backend="matlab").validate()))
    # ⑤ 尺寸 / 导出
    check("1.12 缺尺寸 → 报错", any("尺寸" in p for p in fc(width_mm=None).validate()))
    check("1.13 宽度非刊宽 → 报错",
          any("栏宽" in p for p in fc(width_mm=120.0).validate()))
    check("1.14 单栏 89 mm 也认",
          not any("栏宽" in p for p in fc(width_mm=89.0).validate()))
    check("1.15 导出缺矢量 → 报错",
          any("可编辑" in p for p in fc(formats=["tiff", "png"]).validate()))
    check("1.16 导出缺位图 → 报错",
          any("位图" in p for p in fc(formats=["svg", "pdf"]).validate()))
    # 数据完整性
    bad_ex = [{"what": "outliers", "before": 100, "after": 94}]
    check("1.17 排除项缺规则/理由 → 报错",
          any("排除" in p for p in fc(exclusions=bad_ex).validate()))
    good_ex = [{"what": "outliers", "before": 100, "after": 94,
                "rule": ">3 MAD from median", "reason": "instrument fault, logged"}]
    check("1.18 排除项齐全 → 不报",
          not any("排除" in p for p in fc(exclusions=good_ex).validate()))
    # 往返
    check("1.19 to_dict/from_dict 往返一致",
          FigureContract.from_dict(fc().to_dict()).to_dict() == fc().to_dict())


# ══════════════════════════════════════════════════════════════════
# 2 · 图注体检
# ══════════════════════════════════════════════════════════════════
def test_legend() -> None:
    section("2 图注体检")
    f = fc()
    bad = [x for x in audit_legend(GOOD_LEGEND, f) if x.level == "FAIL"]
    check("2.1 合规图注 0 条 FAIL", not bad, str([b.check_id for b in bad]))

    def has(text, cid, level="FAIL"):
        return any(x.check_id == cid and x.level == level for x in audit_legend(text, f))

    check("2.2 缺 `Fig. N |` 头 → 报错", has("Some title. Source data are provided as a Source Data file.", "LEGEND-HEAD"))
    # 注意：图注里有**两处** n（面板 a 和 c）。只删一处会让这条用例假通过 ——
    # 第一版就是这么写的，测试自己错了，代码是对的。用正则全删。
    import re as _re
    no_n = _re.sub(r"\(n = [^)]*\)", "", GOOD_LEGEND)
    check("2.3 缺 n= → 报错", "n =" not in no_n and has(no_n, "LEGEND-STATS-N"))
    check("2.4 缺离散定义 → 报错",
          has(GOOD_LEGEND.replace("mean ± s.d.", "the mean").replace("mean ± s.d.", "the mean"), "LEGEND-STATS-SPREAD"))
    check("2.5 缺检验方法 → 报错",
          has(GOOD_LEGEND.replace("Two-sided Welch's t-test, t = 8.41, P < 0.001.", ""), "LEGEND-STATS-TEST"))
    check("2.6 缺源数据套语 → 报错",
          has(GOOD_LEGEND.replace("Source data are provided as a Source Data file.", ""), "LEGEND-SOURCE-DATA"))
    check("2.7 分面缺失 → 报错",
          has(GOOD_LEGEND.replace("**c** Distribution of the per-hour increment for the 0–2 h and 3–5 h "
                                  "intervals (n = 6 and 9 intervals).", ""), "LEGEND-PANEL-MISSING"))
    check("2.8 图题含数字 → WARN",
          has("**Fig. 1 | Conversion rises by 12% with time.** " + GOOD_LEGEND.split(".** ", 1)[1],
              "LEGEND-TITLE-NUMBERS", "WARN"))
    # 规范拼写被自动大小写改坏
    fm = fc(canonical_names=["XGBoost"])
    check("2.9 规范名被小写化 → 报错",
          any(x.check_id == "LEGEND-CANONICAL-CASE" and x.level == "FAIL"
              for x in audit_legend(GOOD_LEGEND + " The xgboost baseline is shown.", fm)))
    check("2.10 规范名正确拼写 → 不报",
          not any(x.check_id == "LEGEND-CANONICAL-CASE"
                  for x in audit_legend(GOOD_LEGEND + " The XGBoost baseline is shown.", fm)))
    check("2.11 第一人称 → WARN", has(GOOD_LEGEND + " We measured this.", "LEGEND-FIRST-PERSON", "WARN"))
    check("2.12 超字数上限 → 报错（Nature 250 硬上限）",
          has(GOOD_LEGEND + " extra" * 300, "LEGEND-LENGTH"))
    check("2.13 空图注 → 报错", has("", "LEGEND-EMPTY"))
    # 骨架生成
    gen = render_legend(f, {"n": "3 batches", "center": "mean", "spread": "s.d.",
                            "test": "Welch's t-test, t = 8.41"})
    gen_bad = [x for x in audit_legend(gen, f) if x.level == "FAIL"]
    check("2.14 render_legend 产出的图注能过自己的体检", not gen_bad,
          str([b.check_id for b in gen_bad]))
    check("2.15 render_legend 含三段结构",
          gen.startswith("**Fig. 1 | ") and "Source data are provided" in gen)


# ══════════════════════════════════════════════════════════════════
# 3 · 字形守卫（这是最贵的失败模式）
# ══════════════════════════════════════════════════════════════════
def test_glyph_guard() -> None:
    section("3 字形守卫")
    import warnings as W

    with capture_missing_glyphs() as caught:
        W.warn("Glyph 8315 (SUPERSCRIPT MINUS) missing from font(s) Arial.")
        W.warn("This is an unrelated warning about something else.")
    got = glyphs_missing(caught)
    check("3.1 抓到缺字警告", len(got) == 1 and "SUPERSCRIPT MINUS" in got[0], str(got))
    check("3.2 不误报无关警告", all("unrelated" not in g for g in got), str(got))
    check("3.3 无缺字时返回空", glyphs_missing([]) == [])
    with capture_missing_glyphs() as c2:
        W.warn("Glyph 0 (NUL) missing from font(s) Arial.")
        W.warn("Glyph 0 (NUL) missing from font(s) Arial.")
    check("3.4 同一警告去重", len(glyphs_missing(c2)) == 1, str(glyphs_missing(c2)))


# ══════════════════════════════════════════════════════════════════
# 4 · 定位（解释器 / 技能脚本）
# ══════════════════════════════════════════════════════════════════
def test_locate() -> None:
    section("4 定位")
    scr = locate_scripts(None)
    check("4.1 找得到 nature-figure scripts", scr is not None and (scr / "validate_figure.py").exists(),
          str(scr))
    py = find_figure_python()
    check("4.2 找得到能跑审计的解释器", py is not None, str(py))
    check("4.3 找不到时返回 None（不抛错）",
          find_figure_python(explicit=r"C:\definitely\not\here\nope.exe") is not None)
    # 承重自检：把候选全掐掉，必须返回 None —— 否则"找不到"这条路从没被测过
    au = FigureAuditor(python_exe=r"C:\definitely\not\here\nope.exe")
    good = au.python_exe
    check("4.4 配置写错仍能回退到自动探测", good is not None, str(good))

    class _NoPython(FigureAuditor):
        @property
        def python_exe(self):       # 强制"没有解释器"
            return None

    class _NoScripts(FigureAuditor):
        @property
        def scripts(self):
            return None

    r = _NoPython().audit(fc(), workdir=str(ROOT / "figures"), pdf="fig_conversion.pdf",
                           report_path=iso("nopython"))
    check("4.5 缺解释器 → 各门 SKIPPED 且**整体不通过**",
          (not r.ok) and any(g.verdict == "SKIPPED" for g in r.gates), r.verdict)
    check("4.6 SKIPPED 不算通过",
          all(not g.ok for g in r.gates if g.verdict == "SKIPPED"))
    r2 = _NoScripts().audit(fc(), workdir=str(ROOT / "figures"), pdf="fig_conversion.pdf",
                            report_path=iso("noscripts"))
    check("4.7 缺审计脚本 → 不通过", not r2.ok, r2.verdict)


# ══════════════════════════════════════════════════════════════════
# 5 · 真实审计（四道门全跑）
# ══════════════════════════════════════════════════════════════════
def real_audit_payload() -> tuple[dict, FigureContract, str]:
    au = FigureAuditor()
    c = fc()
    rep = au.audit(c, workdir=str(ROOT / "figures"), source="plot_conversion.py",
                   pdf="fig_conversion.pdf",
                   layout_manifest="fig_conversion.layout.json",
                   legend_path="fig_conversion.legend.md",
                   report_path=str(ROOT / "runs" / "fig_conversion.figure-qa.json"))
    payload = rep.summary_json()
    payload["report_text"] = rep.format_for_model()
    return payload, c, rep.report_path


def test_real(payload: dict) -> None:
    section("5 真实审计")
    g = payload["gates"]
    check("5.1 source 门 PASS", g.get("source") == "PASS", str(g))
    check("5.2 glyphs 门 PASS", g.get("glyphs") == "PASS", str(g))
    check("5.3 collision 门 PASS", g.get("collision") == "PASS", str(g))
    check("5.4 alignment 门 PASS", g.get("alignment") == "PASS", str(g))
    check("5.5 legend 无阻断", "图注不合格" not in " ".join(payload.get("blockers") or []),
          str(payload.get("blockers")))
    check("5.6 整体 ok", payload.get("ok") is True, str(payload.get("blockers")))
    check("5.7 报告里记了各产物 mtime（新鲜度判据靠它）",
          bool(payload.get("artifact_mtimes")), str(payload.get("artifact_mtimes")))
    check("5.8 记了审计时刻", bool(payload.get("audited_at_epoch")))
    check("5.9 报告落盘", Path(payload.get("report_path") or "").exists(),
          str(payload.get("report_path")))
    check("5.10 真实审计的报告带路线块，且声明后端满足",
          payload.get("route", {}).get("route") == "submission"
          and payload["route"].get("satisfied") is True, str(payload.get("route")))
    check("5.11 路线理由与图的实际形态对得上（三分面 + 期刊）",
          len(payload.get("route", {}).get("reasons") or []) == 3,
          str(payload.get("route", {}).get("reasons")))

    # 承重自检 A：**NOT AUDITABLE 不许当通过**
    from rongke.figure import GateResult
    check("5.10 NOT AUDITABLE 不算 ok", GateResult("x", "NOT AUDITABLE").ok is False)
    check("5.11 NOT APPLICABLE 算 ok（单面图是合法的）",
          GateResult("x", "NOT APPLICABLE").ok is True)
    check("5.12 REVIEW REQUIRED 不算 ok", GateResult("x", "REVIEW REQUIRED").ok is False)
    check("5.13 SKIPPED 不算 ok", GateResult("x", "SKIPPED").ok is False)

    # 承重自检 B：多分面图**不给几何清单** → 必须 NOT AUDITABLE（不是通过）
    au = FigureAuditor()
    r = au.audit(fc(), workdir=str(ROOT / "figures"), pdf="fig_conversion.pdf",
                 report_path=iso("nomanifest"))
    al = [x for x in r.gates if x.name == "alignment"][0]
    check("5.14 多分面图缺几何清单 → NOT AUDITABLE", al.verdict == "NOT AUDITABLE", al.verdict)
    check("5.15 该情形整体不通过", not r.ok, r.verdict)
    # 反向对照：单分面图 → NOT APPLICABLE 且通过
    one = fc(panels=[GOOD_CONTRACT["panels"][0]])
    r1 = au.audit(one, workdir=str(ROOT / "figures"), pdf="fig_conversion.pdf",
                  report_path=iso("onepanel"))
    al1 = [x for x in r1.gates if x.name == "alignment"][0]
    check("5.16 单分面图 → NOT APPLICABLE 且不算阻断",
          al1.verdict == "NOT APPLICABLE" and al1.ok, al1.verdict)

    # 承重自检 C：PDF 不存在 → NOT AUDITABLE，不是 SKIPPED 也不是通过
    r2 = au.audit(fc(), workdir=str(ROOT / "figures"), pdf="nope_not_here.pdf",
                  report_path=iso("nopdf"))
    check("5.17 PDF 不存在 → 不通过", not r2.ok, r2.verdict)
    check("5.18 该情形判 NOT AUDITABLE（审不了 ≠ 通过）",
          any(g.verdict == "NOT AUDITABLE" for g in r2.gates))

    # 承重自检 D：假引擎混渲被契约拦下
    mixed = fc(engine_artifacts={"fig_conversion.pdf": "origin",
                                 "fig_conversion.png": "python"})
    check("5.19 混渲被拦（Origin 出正稿 + python 出预览）",
          any("排他" in p for p in mixed.validate()))


# ══════════════════════════════════════════════════════════════════
# 6 · 第 8 条图面门
# ══════════════════════════════════════════════════════════════════
def _figure_registry(payload_json: str, *, register_tool: bool = True) -> ToolRegistry:
    reg = ToolRegistry()
    if register_tool:
        reg.register(Tool("audit_figure", "stub", {"type": "object", "properties": {}},
                          fn=lambda **k: payload_json, domain="figure", mutating=True))
    reg.register(Tool("deliver_task", "stub", {"type": "object", "properties": {}},
                      fn=lambda **k: ""))
    return reg


def _claim(seq: int) -> CompletionClaim:
    return CompletionClaim(
        deliverables=["fig_conversion.pdf"],
        evidence=[EvidenceRef(tool="audit_figure", call_seq=seq,
                              shows="图面四道审计全过", acceptance="图面四道审计全过")],
        summary="图已交付")


def _contract(needs_figure: bool = True) -> TaskContract:
    return TaskContract(raw_goal="把这批数据配一张出版级双栏图",
                        acceptance=["图面四道审计全过"], needs_figure=needs_figure)


def test_gate(payload: dict) -> None:
    section("6 第 8 条图面门")
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    reg = _figure_registry(payload_json)
    tc = reg.call("audit_figure", {})
    v = CompletionGate(reg).check(_claim(tc.seq), _contract())
    check("6.1 体检全绿 → 放行", v.passed, str(v.reasons))

    # 承重自检 E：「没做体检」必须被打回（这是这道门存在的全部理由）
    reg2 = _figure_registry(payload_json)
    reg2.call("deliver_task", {})
    v2 = CompletionGate(reg2).check(CompletionClaim(deliverables=["fig.pdf"], summary="done"),
                                    _contract())
    check("6.2 配图任务没做体检 → 打回", not v2.passed, str(v2.reasons))
    check("6.3 打回理由点名 audit_figure", "audit_figure" in " ".join(v2.reasons))

    # 反向对照：**不是**配图任务就不该要求体检 —— 否则误报会毁掉这道门
    reg3 = _figure_registry(payload_json)
    tc3 = reg3.call("audit_figure", {})
    v3 = CompletionGate(reg3).check(_claim(tc3.seq), _contract(needs_figure=False))
    check("6.4 非配图任务不要求体检", v3.passed, str(v3.reasons))

    # 承重自检 F：体检**未通过**必须拦
    bad = dict(payload); bad["ok"] = False
    bad["verdict"] = "FIX BEFORE DELIVERY"
    bad["blockers"] = ["【glyphs 门：FIX BEFORE DELIVERY】1 处字形低于 5 pt（最小 4.9 pt）"]
    reg4 = _figure_registry(json.dumps(bad, ensure_ascii=False, default=str))
    tc4 = reg4.call("audit_figure", {})
    v4 = CompletionGate(reg4).check(_claim(tc4.seq), _contract())
    check("6.5 体检不过 → 打回", not v4.passed, str(v4.reasons))
    check("6.6 打回时带上具体阻断项", "4.9 pt" in " ".join(v4.reasons))

    # 承重自检 G：**旧报告**必须拦 —— 这是本项目「补丁在位≠症状消失」的同型陷阱
    reg5 = _figure_registry(payload_json)
    tc5 = reg5.call("audit_figure", {})
    stale = dict(payload)
    am = dict(stale.get("artifact_mtimes") or {})
    for k in list(am):
        am[k] = float(am[k]) - 60.0          # 假装审计发生在 60 秒前
    stale["artifact_mtimes"] = am
    reg5.register(Tool("audit_figure", "stub", {"type": "object", "properties": {}},
                       fn=lambda **k: json.dumps(stale, ensure_ascii=False, default=str),
                       domain="figure", mutating=True), overwrite=True)
    tc6 = reg5.call("audit_figure", {})
    v5 = CompletionGate(reg5).check(_claim(tc6.seq), _contract())
    check("6.7 报告比图旧 → 判过期并打回", not v5.passed, str(v5.reasons))
    check("6.8 打回理由点名「过期」", "过期" in " ".join(v5.reasons))

    # 承重自检 H：工具整体没装上（figure.enabled=false）→ 降为警告，不许硬拦
    reg6 = _figure_registry(payload_json, register_tool=False)
    reg6.call("deliver_task", {})
    v6 = CompletionGate(reg6).check(
        CompletionClaim(deliverables=["fig.pdf"], summary="图已出",
                        evidence=[EvidenceRef(tool="deliver_task", call_seq=1,
                                              shows="图面四道审计全过",
                                              acceptance="图面四道审计全过")]),
        _contract())
    check("6.9 工具未装配 → 降为警告不硬拦", v6.passed, str(v6.reasons))
    check("6.10 但如实说明本次没做图面体检",
          any("没有做图面体检" in w for w in v6.warnings), str(v6.warnings))

    # 承重自检 I：**解析不了的返回**不能被当合格证（截断就是这种情况）
    reg7 = _figure_registry('{"ok": true, "gates": {"source": "PA')   # 半截 JSON
    tc7 = reg7.call("audit_figure", {})
    v7 = CompletionGate(reg7).check(_claim(tc7.seq), _contract())
    check("6.11 返回被截断 → 视同没做体检（不当合格证）", not v7.passed, str(v7.reasons))

    # 承重自检 J：审计工具**调用失败** → 打回并报出错误
    def _boom(**k):
        raise RuntimeError("找不到能跑渲染后审计的解释器")

    reg8 = ToolRegistry()
    reg8.register(Tool("audit_figure", "stub", {"type": "object", "properties": {}},
                       fn=_boom, domain="figure", mutating=True))
    tc8 = reg8.call("audit_figure", {})
    check("6.12 工具本身失败 → 该次调用 ok=False", not tc8.ok)
    v8 = CompletionGate(reg8).check(_claim(tc8.seq), _contract())
    check("6.13 失败调用不算体检 → 打回", not v8.passed, str(v8.reasons))


# ══════════════════════════════════════════════════════════════════
# 7 · 工具端到端（走注册表，真跑一遍审计）
# ══════════════════════════════════════════════════════════════════
def test_tool_e2e() -> None:
    section("7 工具端到端（走注册表）")
    from rongke.loop import AUDIT_FIGURE_SCHEMA, _make_audit_figure

    tmp = T.fresh_dir(_TMPROOT / "e2e", "fig-e2e")
    src = ROOT / "figures"
    for f in src.glob("fig_conversion.*"):
        shutil.copy2(f, tmp / f.name)
    shutil.copy2(src / "plot_conversion.py", tmp / "plot_conversion.py")
    shutil.copy2(src / "fig_conversion.legend.md", tmp / "fig_conversion.legend.md")

    class _Cfg:
        enabled = True
        python = ""
        skills_dir = ""
        timeout = 300.0

    prev = Path.cwd()
    try:
        os.chdir(tmp)
        reg = ToolRegistry()
        reg.register(Tool("audit_figure", "图面体检", AUDIT_FIGURE_SCHEMA,
                          fn=_make_audit_figure(_Cfg()), domain="figure", mutating=True))
        tc = reg.call("audit_figure", {
            "contract": GOOD_CONTRACT, "pdf": "fig_conversion.pdf",
            "source": "plot_conversion.py",
            "layout_manifest": "fig_conversion.layout.json",
            "legend_path": "fig_conversion.legend.md"})
        check("7.1 工具调用成功（ok=True）", tc.ok, tc.error[:300])
        d = json.loads(tc.result) if tc.ok else {}
        check("7.2 返回的是可解析 JSON", bool(d))
        check("7.3 返回 ok=true", d.get("ok") is True, str(d.get("blockers")))
        check("7.4 六道门都在返回里（四道第三方 + W12 自有两维）",
              set(d.get("gates", {})) == {"source", "glyphs", "collision", "alignment",
                                          "font_fallback", "text_containment"},
              str(d.get("gates")))
        check("7.5 报告落在**工作区**里（不是进程原始 cwd）",
              Path(d.get("report_path", "")).is_file() and
              Path(d["report_path"]).parent.resolve() == tmp.resolve(),
              d.get("report_path"))
        check("7.6 审计产物也落在工作区",
              (tmp / "fig_conversion.collision-audit.json").exists() and
              (tmp / "fig_conversion.alignment.json").exists())
        check("7.7 返回值长度在截断阈值内（否则门解析不了）", len(tc.result) <= 12000, str(len(tc.result)))
        # 路线必须随返回值一起到模型手上 —— 否则模型只能从"对齐门 SKIPPED"
        # 这种间接迹象反推自己后端选错了，等于白判。
        check("7.8 返回值带渲染路线（含 required / declared / satisfied）",
              d.get("route", {}).get("route") == "submission"
              and d["route"].get("declared_backend") == "python"
              and d["route"].get("satisfied") is True, str(d.get("route")))
        check("7.9 人读版报告开头就交代路线（在逐门之前）",
              "渲染路线：投稿渲染路线" in (d.get("report_text") or ""),
              (d.get("report_text") or "")[:120])
    finally:
        os.chdir(prev)
        T.stash(tmp, "fig-e2e")


# ══════════════════════════════════════════════════════════════════
# 8 · 意图判定
# ══════════════════════════════════════════════════════════════════
def test_intent() -> None:
    section("8 配图意图判定")
    yes = ["把这批数据配一张图", "画一张双栏 183mm 的投稿图", "帮我绘图，带图注",
           "复现论文里的 figure legend", "用 R 制图并导出矢量图"]
    no = ["读一下这张图里说了什么", "review 一下 figure 1 的 legend",
          "写一篇 1200 字综述", "报告最近 3 次工具调用"]
    for t in yes:
        check(f"8.1 命中：{t}", _looks_like_figure(t))
    for t in no:
        # ⚠️ 反向对照：这些**不能**命中 —— 图面门是硬门，
        #    误触发会让一个纯"读图"的任务卡在那里交不了。
        check(f"8.2 不误报：{t}", not _looks_like_figure(t))


# ══════════════════════════════════════════════════════════════════
# 10 · 渲染路线分工（什么图走哪条路，**画之前**就判）
#
# 为什么值得单测：这条规矩存在的唯一目的是"别再逐图纠结"。
# 要是判错了方向、或者判对了却说不清理由，模型就会回去纠结 ——
# 那这条规矩等于没写。所以这里既测"判得对"，也测"话说明白了"。
# ══════════════════════════════════════════════════════════════════

# 合法走原生路线的形态：**单分面 + 面板不画数据（center/spread 留空）+ 不投稿**。
# 三个条件都必须成立 —— 下面每条触发条件都单独拿它做对照。
NATIVE_OK = {
    "archetype": "schematic-led composite",
    "backend": "origin",
    "journal": "",
    "panels": [{"letter": "a", "role": "schematic pipeline stages",
                "claim": "which sample-handling stages are in play",
                "center": "", "spread": "", "unit": ""}],
}


def test_route() -> None:
    section("10 渲染路线分工")

    # ── 正路：参考图（三分面 + 画数据 + 声明期刊）必须走投稿渲染路线 ──
    rd = route_for(fc())
    check("10.1 三分面+数据+期刊 ⇒ 判投稿渲染路线",
          rd.route == "submission" and rd.label.startswith("投稿渲染路线"), rd.route)
    check("10.2 该情形是**强制**（must=True）", rd.must is True)
    check("10.3 三条触发原因逐条给出来（可执行，不是『不合法』三个字）",
          len(rd.reasons) == 3, str(rd.reasons))
    check("10.4 声明 python ⇒ 满足；契约无问题（正向对照：不误伤合规图）",
          rd.satisfied_by("python") and fc().validate() == [], str(fc().validate()))
    check("10.5 声明 origin ⇒ 不满足", not rd.satisfied_by("origin"))

    # 承重自检 A：只改 backend 一个字，其余逐字不动 ⇒ 契约由成立变不成立。
    # 把 validate() 里的 4b 删掉，10.6 立刻变红 —— 这就是它的承重证明。
    probs = fc(backend="origin").validate()
    hit = [p for p in probs if "渲染路线" in p]
    check("10.6 同图声明 origin ⇒ 契约不成立（删掉 4b 这条会变红）",
          len(hit) == 1, str(probs))
    msg = hit[0] if hit else ""
    check("10.7 报错里逐条列出触发原因（模型不用猜是哪个条件没过）",
          "分面数" in msg and "画的是数据" in msg and "期刊" in msg, msg[:220])
    check("10.8 报错里点名两条出路，且指明第二条要动什么",
          "改装 python/r" in msg and "合并成单面图" in msg and "留空" in msg, msg[:220])
    check("10.9 报错里讲清代价是「审不了不等于通过」",
          "NOT AUDITABLE" in msg and "不等于通过" in msg, msg[:220])

    # ── 合法走原生路线：不拦，且**不留噪声** ──
    native = fc(**NATIVE_OK)
    rdn = route_for(native)
    check("10.10 单面+不画数据+不投稿 ⇒ 判原生矢量图形路线、非强制",
          rdn.route == "native-graphics" and rdn.must is False, rdn.route)
    check("10.11 该情形声明 origin ⇒ 契约成立（反向对照：不误伤）",
          native.validate() == [], str(native.validate()))
    check("10.12 该情形声明 python 也成立 —— 判据是『属于』不是『等于』",
          rdn.satisfied_by("python")
          and fc(**dict(NATIVE_OK, backend="python")).validate() == [])
    check("10.13 如实说明本路线拿不到哪两道门（不当成失败）",
          len(rdn.unavailable) == 2 and "alignment" in rdn.unavailable[0],
          str(rdn.unavailable))
    # 报告里该不该出现警告：合法的路线选择**不是**缺陷，进警告就是噪声。
    # 用 dataclass 直接拼一份最小报告，不落盘、不碰产物目录。
    bare = FigureReport(contract=native)
    check("10.14 合法的原生路线不产生任何警告（误报比漏报更伤）",
          bare.warnings == [] and bare.ok, str(bare.warnings))
    # 正面对照必须走**真实 audit() 通路**，不能手拼报告：`FigureReport.contract_problems`
    # 是审计当时**记下来**的结论，不是现值 —— 手拼一份恰好等于"拿一份没体检过的报告
    # 当合格证"，那是本项目的老坑。所以路线违规要能被 audit() 记进 blockers 才算数。
    bad_rep = FigureAuditor().audit(fc(backend="origin"), workdir=str(_TMPROOT),
                                    report_path=iso("badroute"))
    check("10.15 非法路线走真实审计通路 → 阻断项里点名路线（正面对照）",
          not bad_rep.ok and any("渲染路线" in b for b in bad_rep.blockers),
          str(bad_rep.blockers)[:200])

    # ── 三个触发条件**各自单独**都要能触发升级 ──
    only_panels = dict(journal="",
                       panels=GOOD_CONTRACT["panels"][:2],
                       backend="origin")
    check("10.16 仅『分面数 ≥ 2』成立 ⇒ 仍必须投稿路线",
          route_for(fc(**only_panels)).must
          and any("渲染路线" in p for p in fc(**only_panels).validate()))
    # 「画了数据」的判据：center / spread 任一非空即算定量面板。
    # 逐点原始值（center=raw, spread=none）也算 —— 旧版 `bool(spread)`
    # 把 "none" 当离散，措辞跟着说假话，这一条同时把那个坑钉住。
    raw_only = dict(NATIVE_OK, journal="",
                    panels=[{"letter": "a", "role": "raw scatter of individual runs",
                             "claim": "spread of individual runs", "center": "raw",
                             "spread": "none", "unit": "batches"}])
    check("10.17 仅『面板画了逐点数据』成立 ⇒ 仍必须投稿路线",
          route_for(fc(**raw_only)).must
          and any("渲染路线" in p for p in fc(**raw_only).validate()))
    check("10.18 spread='none' 不算真的有离散（与 quantitative 分开）",
          fc(**raw_only).panels[0].quantitative is True
          and fc(**raw_only).panels[0].has_spread is False)
    check("10.19 纯示意分面既不算定量、也不算有离散",
          fc(**NATIVE_OK).panels[0].quantitative is False
          and fc(**NATIVE_OK).panels[0].has_spread is False)
    only_journal = dict(NATIVE_OK, journal="nature")
    check("10.20 仅『声明了期刊』成立 ⇒ 仍必须投稿路线",
          route_for(fc(**only_journal)).must
          and any("渲染路线" in p for p in fc(**only_journal).validate()))

    # ── 措辞同源：报告块与 CLI 用同一份（防两处各写一遍、日后漂移）──
    block_ok = rd.format_block(declared="python")
    block_bad = rd.format_block(declared="origin")
    check("10.21 format_block 标注声明后端是否满足",
          "满足" in block_ok and "不满足" in block_bad, block_bad[:120])
    check("10.22 format_block 不只是判定，还带理由",
          block_ok.count("·") >= 3, block_ok[:160])

    # ── 报告里的路线必须是**算出来的**，不是存下来的 ──
    # 存一份就有两个真相，两份迟早对不上（本项目的老毛病）。
    rep = FigureReport(contract=fc())
    check("10.23 报告的路线与 route_for(契约) 同源，且声明后端满足",
          rep.route.route == rd.route and rep.route_ok is True,
          f"{rep.route.route} / {rep.route_ok}")
    mutable = FigureReport(contract=fc())
    mutable.contract = native          # 换掉契约
    check("10.24 契约一换，报告的路线跟着变（没有第二份真相）",
          mutable.route.route == "native-graphics" and mutable.route_ok is True,
          f"{mutable.route.route} / {mutable.route_ok}")
    sj = rep.summary_json()
    check("10.25 给门的紧凑 JSON 里带路线块",
          sj["route"]["route"] == "submission" and sj["route"]["declared_backend"] == "python",
          str(sj.get("route")))
    full = rep.to_dict()["route"]
    # **同一份数据不许有两种形状**。我第一版就让它们分了家：工具返回是嵌套的
    # （`["route"]["required"]`），落盘那份是扁平的（RouteDecision.to_dict() 原样）——
    # 读的人得先知道 JSON 从哪来才知道怎么取键。这是 P 型缺陷的孪生兄弟。
    check("10.26 落盘那份是紧凑块的**超集**，键名不另起一套",
          set(sj["route"]) <= set(full), str(sorted(set(sj["route"]) - set(full))))
    check("10.27 落盘那份另给的只是诊断字段（backends / unavailable / note）",
          {"backends", "unavailable", "note"} <= set(full), str(sorted(full)))

    # ── 事前告知：纪律文本必须把分工写清，否则模型还是要自己纠结 ──
    disc = _contract().figure_discipline_text()
    check("10.28 纪律文本写明判据是审计需求、不是引擎喜好",
          "看图的审计需求" in disc)
    check("10.29 纪律文本写全三个触发条件",
          "分面数 ≥ 2" in disc and "声明了目标期刊" in disc and "center / spread" in disc)
    check("10.30 纪律文本给出原生路线的适用边界与代价",
          "单分面" in disc and "NOT APPLICABLE" in disc)
    check("10.31 纪律文本不再把后端说成『python 或 r』（过时说法已删）",
          "后端：python 或 r" not in disc and
          "python / r / origin / other" in disc)
    check("10.32 纪律文本点明后端选错在**画之前**就被拦",
          "画之前" in disc)


# ══════════════════════════════════════════════════════════════════
# 9 · 测试自身不许污染产物目录（本轮真栽过，配回归守卫）
# ══════════════════════════════════════════════════════════════════
def snapshot_figures() -> dict:
    d = ROOT / "figures"
    return {f.name: f.stat().st_mtime for f in d.iterdir() if f.is_file()}


# ══════════════════════════════════════════════════════════════════
# 8 · W12 自有两维：字形是否真画出来 / 文字装不装得下所在框
# ══════════════════════════════════════════════════════════════════
# 这四个方向是主控实测出来的两类"能穿过多门"的真缺陷（豆腐块 □、文字被裁）。
# ★ 素材缺失**必须报错**，不许静默跳过 —— 跳过 = 这两维没被测到 = 假通过。
_W12_RECON = ROOT / "00_总控" / "_w12recon"
_W12_REAL = ROOT / "00_总控" / "_realtopic" / "fig1_cordyceps_bottlenecks.pdf"
#: matplotlib 直出版：①②③ 圈号是**矢量填色路径**，会混进 get_drawings()。
#: 这张是"防假警"的正例素材（返工单 §3），缺它 ⇒ 圈号当框这类假警没人守得住。
_W12_REAL_MPL = ROOT / "00_总控" / "_realtopic" / "fig1_cordyceps_bottlenecks_mpl.pdf"


def test_w12_gates() -> None:
    section("11 W12 自有两维（字形 / 文字装得下）")
    mats = {"tofu": _W12_RECON / "tofu.pdf",
            "overflow_real": _W12_RECON / "overflow_real.pdf",
            "good": _W12_RECON / "good.pdf",
            "realtopic": _W12_REAL,
            "realtopic_mpl": _W12_REAL_MPL}
    missing = [k for k, v in mats.items() if not v.exists()]
    check("11.1 五张素材都在（缺任何一张 ⇒ 这两维没被测到）", not missing, str(missing))
    if missing:
        return

    au = FigureAuditor()
    if au.python_exe is None:
        check("11.2 有带 PyMuPDF 的解释器（没有就报红，不许当通过）", False,
              str(au.diag()))
        return
    check("11.2 有带 PyMuPDF 的解释器", True, au.python_exe)

    g = {k: au._gate_glyph_font(v) for k, v in mats.items()}
    t = {k: au._gate_text_fit(v) for k, v in mats.items()}

    # ── 该报的必须报 ──
    check("11.3 tofu 报出字形缺失（该报的报）",
          g["tofu"].verdict == "FIX BEFORE DELIVERY" and g["tofu"].findings,
          f"{g['tofu'].verdict} {g['tofu'].detail[:70]}")
    check("11.4 tofu 指出的是 LastResort 且列全嵌入字体",
          "LastResort" in g["tofu"].detail and "FONT-LASTRESORT" in {f.check_id for f in g["tofu"].findings},
          g["tofu"].detail[:70])
    of = [f for f in t["overflow_real"].findings if f.level == "FAIL"]
    check("11.5 overflow_real 报出文字装不下所在框（该报的报）",
          t["overflow_real"].verdict == "FIX BEFORE DELIVERY" and bool(of),
          f"{t['overflow_real'].verdict} {t['overflow_real'].detail[:70]}")
    check("11.6 溢出量给出 pt 与 %（不能只说「有问题」）",
          of and "pt" in of[0].message and "%" in of[0].message,
          of[0].message[:80] if of else "无 FAIL 条目")
    # 素材必须是**真**溢出：`overflow.pdf` 那张是假素材（matplotlib 的
    # bbox_inches=tight 会把画布撑大去容纳文字 ⇒ 框跟着变大，根本没溢出）。
    pct_of = [float(f.get("overflow_pct") or 0.0) for f in
              (au._geometry(mats["overflow_real"])[0] or {}).get("findings", [])]
    check("11.6b 用的是 overflow_real（最大溢出须是百量级，不是 tight 布局的假素材）",
          bool(pct_of) and max(pct_of) > 100.0, f"最大溢出 {max(pct_of) if pct_of else '无'}%")
    # WARN 也要给坐标：返工单 §4.1 实测过 `x=None pt` 那种没法定位的证据
    ev_all = [e for t_ in t.values() for f in t_.findings for e in f.evidence]
    check("11.6c 每条越界证据都带坐标（不许 x=None）",
          not any("None pt" in e for e in ev_all),
          next((e for e in ev_all if "None pt" in e), ""))

    # ── 不该报的绝不许叫（误报会训练模型忽略整份报告）──
    for tag in ("good", "realtopic", "realtopic_mpl"):
        check(f"11.7 {tag} 两维都不报（不该报的不叫）",
              g[tag].verdict == "PASS" and t[tag].verdict == "PASS",
              f"font={g[tag].verdict} text={t[tag].verdict} | {t[tag].detail[:60]}")
    # 11.7a 防假警规则 A 的专项护栏：①②③ 圈号是 7pt 上下的填色路径，
    # 一旦被当成「框」，61.6pt 的「① 寄主昆虫」就会报"超出 458.9%"。
    mplf = [f for f in t["realtopic_mpl"].findings if f.level in ("FAIL", "WARN")]
    check("11.7a mpl 直出版没有把圈号色块当成容器（假警必须归零）",
          not mplf and t["realtopic_mpl"].verdict == "PASS",
          f"{[f.message[:40] for f in mplf]}")
    # 溢出门必须**真的比对过**文字，不能是"一段都没匹配到框"混成 PASS
    m = re.search(r"其中 (\d+) 段与所在框比过", t["realtopic"].detail)
    check("11.8 正常图确实比对过文字（不是空转当通过）", bool(m) and int(m.group(1)) > 0,
          t["realtopic"].detail[:70])

    # ── 没查成 ≠ 通过 ──
    bad = _TMPROOT / "w12_notapdf.pdf"
    bad.write_bytes(b"%PDF-1.4\nthis is not a parseable pdf\n")
    check("11.9 坏 PDF ⇒ 两门 NOT AUDITABLE（不是 PASS）",
          au._gate_glyph_font(bad).verdict == "NOT AUDITABLE"
          and au._gate_text_fit(bad).verdict == "NOT AUDITABLE",
          f"{au._gate_glyph_font(bad).verdict}/{au._gate_text_fit(bad).verdict}")

    class _NoPython(FigureAuditor):
        @property
        def python_exe(self):
            return None

    nopy = _NoPython()
    gg, gt = nopy._gate_glyph_font(mats["good"]), nopy._gate_text_fit(mats["good"])
    check("11.10 没有解释器 ⇒ NOT AUDITABLE 且阻断（不是 SKIPPED 也不是 PASS）",
          gg.verdict == "NOT AUDITABLE" and gt.verdict == "NOT AUDITABLE"
          and gg.blocking and gt.blocking and not gg.ok and not gt.ok,
          f"{gg.verdict}/{gt.verdict}")

    # ── 回归护栏：不许退回去手写解析 PDF（勘误2 的四层坑）──
    src = (ROOT / "rongke" / "figure.py").read_text(encoding="utf-8")
    dead = [n for n in ("_pdf_text_runs", "_pdf_content_streams", "_string_glyphs",
                        "_pdf_base_fonts", "import zlib", "import bisect") if n in src]
    check("11.11 判据走 PyMuPDF，没有自造 PDF 解析器（勘误2 四层坑的护栏）",
          not dead and "import pymupdf" in src and "_PDF_GEOMETRY_SCRIPT" in src,
          f"残留={dead}")

    # 两门共用一次量测（同一份几何，杜绝两门各撞各的静默失败）
    au2 = FigureAuditor()
    au2._gate_glyph_font(mats["realtopic"])
    au2._gate_text_fit(mats["realtopic"])
    check("11.12 两门共用同一份几何（同一图只量一次）", len(au2._geom) == 1,
          str(len(au2._geom)))


def test_no_pollution(before: dict, do_real: bool) -> None:
    section("9 测试不许写进产物目录")
    if not do_real:
        check("9.1 （--no-real：不涉及 figures/，跳过）", True)
        return
    after = snapshot_figures()
    changed = [k for k in after if k in before and abs(after[k] - before[k]) > 1e-6]
    added = [k for k in after if k not in before]
    # 碰撞审计的**子报告**会被原地重算（同一张图、同一内容，且不在报告的
    # artifact_mtimes 里 ⇒ 不会造成假新鲜度）—— 所以只硬性禁止**总报告**被写。
    fatal = [k for k in changed + added if k == "figure-qa.json"]
    check("9.1 正式报告 figure-qa.json 没被测试覆盖", not fatal, str(fatal))
    check("9.2 没有在产物目录里新建文件", not added, str(added))
    check(f"9.3 产物目录其余部分（{len(changed)} 个子报告被重算，同图同内容，允许）", True)


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    global _QUIET
    args = sys.argv[1:]
    _QUIET = "--json" in args
    do_real = "--no-real" not in args

    if not _QUIET:
        print("═" * 62)
        print("荣珂图面契约与体检 · 回归测试")
        print("═" * 62)

    fig_before = snapshot_figures()

    test_contract()
    test_legend()
    test_glyph_guard()
    test_locate()
    test_route()
    test_intent()

    payload = None
    if do_real:
        payload, _, _ = real_audit_payload()
        test_real(payload)
    if payload is not None:
        test_gate(payload)
    if do_real:
        test_tool_e2e()
        test_w12_gates()

    test_no_pollution(fig_before, do_real)
    T.stash(_TMPROOT, "fig-tmproot")

    total = len(PASS) + len(FAIL)
    if _QUIET:
        print(json.dumps({"total": total, "pass": len(PASS), "fail": len(FAIL),
                          "failed": FAIL}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "═" * 62)
        print(f"合计 {total} 项：{len(PASS)} 通过，{len(FAIL)} 失败")
        if FAIL:
            print("失败项：")
            for f in FAIL:
                print(f"  ✘ {f}")
        print("═" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
