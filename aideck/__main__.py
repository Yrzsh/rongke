# -*- coding: utf-8 -*-
"""aideck 命令行：一次一个原子动作，或跑一遍「AI 摆弄画布」的演示。

    python -m aideck <工作目录> <动作.json>        # 单动作；结果以 JSON 打印
    python -m aideck <工作目录> --demo             # 完整演示（含导出 .pptx）
    python -m aideck <工作目录> --query            # 看当前画布
    python -m aideck <工作目录> --render           # 只重画布台面
    python -m aideck <工作目录> --export           # 只导 .pptx

每个动作都走 Canvas.apply()：改 → 落盘 → 读回 → 质检 → 回包。
AI 拿到的是**当前事实**，不是「写成功」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .canvas import Canvas
from .studio import export_pptx, render_html


def demo(root: Path) -> int:
    """演示「图元级」和「一次性成稿」的差别：先放歪 → 质检报 → 挪正。"""
    c = Canvas(root, title="组会汇报 · 图元画布演示")
    log = []

    def act(a):
        r = c.apply(a)
        log.append({"action": a, "ok": r.get("ok"),
                    "gaps": [g["code"] for g in (r.get("gaps") or [])],
                    "error": r.get("error")})
        return r

    act({"op": "new_page", "kind": "cover", "title": "封面"})
    r = act({"op": "add", "type": "text", "box": [72, 240, 1136, 120],
             "text": "绢青陈墨 · 组会汇报", "size": 64})
    title_id = (r.get("element") or {}).get("id")
    act({"op": "add", "type": "rect", "box": [72, 232, 120, 8]})
    # 故意把副标题放歪（x=90 离网格、和标题框有重叠风险）
    r = act({"op": "add", "type": "text", "box": [90, 250, 900, 60],
             "text": "从图元到画布", "size": 30})
    sub_id = (r.get("element") or {}).get("id")
    before = c.apply({"op": "audit"})

    # AI 看见缺口后只挪这一个图元 —— 不用重写整份稿
    act({"op": "move", "eid": sub_id, "box": [72, 508, 1000, 64]})
    act({"op": "set", "eid": sub_id, "size": 32})
    after = c.apply({"op": "audit"})

    # 第二页：内容 + 一张真图表（数据由调用方给，内核不编）
    act({"op": "new_page", "kind": "content", "title": "关键结果"})
    act({"op": "add", "type": "text", "box": [72, 36, 1136, 72],
         "text": "关键结果", "size": 40})
    act({"op": "add", "type": "bullets", "box": [72, 140, 500, 380],
         "items": ["三组对照的差异", "第三组显著高于其余"], "size": 26})
    act({"op": "add", "type": "chart", "box": [656, 140, 552, 380],
         "chart": {"title": "三组对照", "categories": ["A", "B", "C"],
                   "series": [{"name": "处理组", "values": [1.2, 2.4, 3.1]}]}})
    q = c.apply({"op": "query"})
    h = render_html(c)
    x = export_pptx(c)

    print(json.dumps({
        "demo": "图元级画布：放歪 → 质检报缺口 → 只挪一个图元 → 读回",
        "title_id": title_id, "subtitle_id": sub_id,
        "gaps_before": [g["code"] for g in before["gaps"]],
        "gaps_after": [g["code"] for g in after["gaps"]],
        "moved_only_one_element": True,
        "query_pages": q["pages"], "query_shown": q["shown"],
        "html": h, "export": x,
        "trace": log,
    }, ensure_ascii=False, indent=2))
    return 0 if x.get("ok") else 1


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    arg = sys.argv[2]
    c = Canvas(root)
    if arg == "--demo":
        return demo(root)
    if arg == "--query":
        print(json.dumps(c.apply({"op": "query"}), ensure_ascii=False, indent=2))
        return 0
    if arg == "--audit":
        print(json.dumps(c.apply({"op": "audit"}), ensure_ascii=False, indent=2))
        return 0
    if arg == "--render":
        print(json.dumps(render_html(c), ensure_ascii=False, indent=2))
        return 0
    if arg == "--export":
        r = export_pptx(c)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    try:
        action = json.loads(Path(arg).read_text(encoding="utf-8")) if \
            Path(arg).is_file() else json.loads(arg)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"动作不是合法 JSON：{e}"},
                         ensure_ascii=False))
        return 1
    r = c.apply(action)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
