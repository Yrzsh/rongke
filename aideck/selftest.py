# -*- coding: utf-8 -*-
"""AI 画布自检 —— 两个方向都量，且只认读回。

    python -m aideck.selftest        （用装了 python-pptx 的 3.12 跑）

判据重点不在「能不能造出文件」，而在**工具替 AI 看画布这件事是不是真的**：
  · 每次动作写后读回、返回当前状态；
  · 查询不必依赖上下文记忆；
  · 几何缺口（越界/重叠/离网格/空页）如实报，且不把合法中间状态判成失败；
  · 失败动作回滚，状态一字不动；
  · 导出 .pptx 后逐行读回文字、按 part 数读回图表。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .canvas import Canvas, _plain
from . import studio

_fail = 0


def ok(m):
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def eq(got, want, label):
    ok(label) if got == want else bad(f"{label}：期望 {want!r}，实际 {got!r}")


def truth(got, label):
    ok(label) if got else bad(label)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="aideck_test_") as td:
        root = Path(td)
        c = Canvas(root, title="自检画布")
        eq(c.doc["pages"], [], "新画布是空的")
        eq(c.doc["canvas"]["w"], 1280, "画布宽 = design 网格 1280px")

        r = c.apply({"op": "new_page", "kind": "content", "title": "第一页"})
        eq(r["ok"], True, "★ 新建空页是合法中间状态，不能判成失败")
        truth(any(g["code"] == "EMPTY_PAGE" and g["level"] == "soft"
                  for g in r["gaps"]), "空页要说出口，但只作 soft 提示")
        eq(r["applied"], True, "回包把「已执行」与「有无缺口」分开报")
        pid = r["page"]                       # _commit 回的是页 id 字符串

        r = c.apply({"op": "add", "type": "text", "box": [72, 36, 1136, 72],
                     "text": "研究背景", "size": 40, "page": pid})
        eq(r["ok"], True, "放一个标题图元成功")
        eid = r["element"]["id"]
        eq(r["readback_equal"], True, "写后读回一致")
        q = c.apply({"op": "query"})
        truth(q["state"][0]["elements"][0]["id"] == eid,
              "★ query 能报出当前 id —— AI 不必靠上下文记住状态")
        truth(q["state"][0]["elements"][0]["text"] == "研究背景",
              "query 报出图元文字（AI 的眼睛）")
        eq(json.loads((root / "画布.json").read_text(encoding="utf-8"))
           ["pages"][0]["elements"][0]["text"], "研究背景",
           "盘上状态与内存一致（不是只回包说成功）")

        # 负控：越界是 hard，但动作确实执行了，工具要分开报
        r = c.apply({"op": "add", "type": "text", "box": [9999, 0, 100, 50],
                     "text": "越界了", "page": pid})
        eq(r["ok"], False, "越界 ⇒ ok=False")
        eq(r["applied"], True, "但 applied=True：东西确实在画布上，别让 AI 重发")
        truth(any(g["code"] == "OUT_OF_BOUNDS" for g in r["gaps"]),
              "缺口点名 OUT_OF_BOUNDS 并给 move 动作")
        bad_id = r["element"]["id"]
        r2 = c.apply({"op": "move", "eid": bad_id, "box": [72, 140, 500, 80]})
        eq(r2["ok"], True, "只挪这一个图元就修好了（不用重写整份稿）")

        # 重叠正负控
        r3 = c.apply({"op": "add", "type": "text", "box": [100, 150, 400, 70],
                      "text": "压上去了", "page": pid})
        truth(any(g["code"] == "OVERLAP" for g in r3["gaps"]),
              "正控：两个文字图元重叠 ⇒ OVERLAP")
        c.apply({"op": "move", "eid": r3["element"]["id"],
                 "box": [72, 300, 400, 70]})
        truth(not any(g["code"] == "OVERLAP" for g in c.apply({"op": "audit"})["gaps"]),
              "负控：挪开后 OVERLAP 消失（判据不是恒真）")

        # set / duplicate / align / remove
        r4 = c.apply({"op": "set", "eid": eid, "text": "研究背景（改过）", "size": 42})
        eq(r4["element"]["text"], "研究背景（改过）", "set 定点改文字")
        r5 = c.apply({"op": "duplicate", "eid": eid, "dx": 24, "dy": 24})
        truth(r5["element"]["id"] != eid, "duplicate 给新 id（不覆盖原图元）")
        c.apply({"op": "align", "eids": [eid, r5["element"]["id"]], "mode": "left"})
        eq(c.apply({"op": "query"})["state"][0]["elements"][0]["box"][0], 72,
           "align 把左边缘对齐到网格")
        n_before = len(c.doc["pages"][0]["elements"])
        c.apply({"op": "remove", "eid": r5["element"]["id"]})
        eq(len(c.doc["pages"][0]["elements"]), n_before - 1, "remove 真删了一个")

        # 失败动作必须回滚、状态一字不动
        raw = (root / "画布.json").read_bytes()
        for label, act in [
            ("坏图元类型", {"op": "add", "type": " hologram", "box": [1, 2, 3, 4]}),
            ("坏 box", {"op": "add", "type": "text", "box": [1, 2], "text": "x"}),
            ("找不到 id", {"op": "move", "eid": "nope", "box": [1, 2, 3, 4]}),
            ("未知动作", {"op": "teleport"}),
            ("不许改的字段", {"op": "set", "eid": eid, "z_index": 99}),
            ("chart 缺数据", {"op": "add", "type": "chart",
                              "box": [72, 140, 500, 300]}),
        ]:
            rr = c.apply(act)
            truth(rr.get("ok") is False and rr.get("error"),
                  f"负控：{label} ⇒ ok=False 且给原因")
            eq((root / "画布.json").read_bytes(), raw,
               f"负控：{label} 后盘上状态一字不动")

        # 真图表 + 表格（数据由调用方给，内核不编）
        c.apply({"op": "add", "type": "chart", "box": [656, 140, 552, 380],
                 "chart": {"title": "三组", "categories": ["A", "B"],
                           "series": [{"name": "组", "values": [1, 2]}]}})
        c.apply({"op": "add", "type": "table", "box": [72, 420, 500, 120],
                 "rows": [["项", "值"], ["甲", "3.2"]]})
        a = c.apply({"op": "audit"})
        eq(a["counts"]["visuals"], 2, "audit 数出 2 个视觉件（chart + table）")

        # 渲染台面
        h = studio.render_html(c)
        eq(h["ok"], True, "render_html 生成台面")
        txt = (root / "画布.html").read_text(encoding="utf-8")
        # ⚠️ 别拿裸 "http://" 当外链判据：SVG 的 xmlns 就是标准命名空间 URI，
        #    不是外部资源。判「真正的引用标签」，不是判字符串里有没有冒号。
        truth("<!doctype html>" in txt.lower()
              and "<script" not in txt and "<link " not in txt
              and 'src="http' not in txt and 'href="http' not in txt
              and 'xmlns="http://www.w3.org/2000/svg"' in txt,
              "台面自包含：无脚本/无外链/无外部样式（SVG 只用标准命名空间）")
        eq(txt.count("<svg "), len(c.doc["pages"]), "每页一张 SVG 画布")
        truth("缺口与下一步动作" in txt and "query / audit" in txt,
              "台面带缺口面板，且声明与 query/audit 同一份事实")

        # 导出 .pptx：写后逐行读回
        ex = studio.export_pptx(c)
        eq(ex.get("ok"), True, f"导出 .pptx 成功（missing={ex.get('readback_missing')}）")
        eq(ex.get("slides"), 1, "导出页数与画布一致")
        truth((root / "deck.pptx").stat().st_size > 10000, "盘上真有 .pptx")
        eq(ex.get("readback_missing"), [], "★ 文字逐行读回、图表按 part 读回，零缺失")

        # 负控：后端不可用要明说，不静默降级
        _orig = studio.backend_python
        try:
            studio.backend_python = lambda *a, **k: None
            r6 = studio.export_pptx(c)
            truth(r6.get("ok") is False
                  and "EXPORT_BACKEND_UNAVAILABLE" in str(r6.get("error")),
                  "★ 后端不可用 ⇒ 明说能力不可用，不静默降级")
        finally:
            studio.backend_python = _orig

        # 承重自检：伪造一行 pptx 里根本没有的文字 ⇒ 读回必须当场翻红
        _orig_lines = studio._lines_of
        try:
            studio._lines_of = (lambda e: ["这段字根本不在 pptx 里"]
                                if e["type"] == "text" else [])
            r7 = studio.export_pptx(c)
            truth(r7.get("ok") is False and bool(r7.get("readback_missing")),
                  "★ 短路逐行读回：伪造一行不存在的文字 ⇒ 当场翻红（判据在承重）")
        finally:
            studio._lines_of = _orig_lines

    print()
    if _fail:
        print(f"[FAIL] 失败 {_fail} 项")
        return 1
    print("[DONE] AI 画布自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
