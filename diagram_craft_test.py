# -*- coding: utf-8 -*-
"""机制图工艺审计（diagram_craft）判据。

判的是**规则有没有分辨率**：六条工艺规则，每条都要有一条能把真产物改坏、
并且当场翻红的负控。只量「默认图过不过」等于给实现拍照，抓不到回归。

基准独立于被测模块：配色语义 / 尺度上限 / 断词 / 压线 / 框叠 / 穿节点
都从**几何清单里的坐标与文本**直接量，不读引擎内部变量。
"""
import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke import diagram_craft as C
from rongke import svgdiagram as D

PASS_N = FAIL_N = 0


def truth(cond, msg):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print(f"[PASS] {msg}")
    else:
        FAIL_N += 1
        print(f"[FAIL] {msg}")


SPEC = {
    "title": "TGF-β/SMAD 通路与负反馈", "direction": "LR", "width_mm": 260,
    "nodes": [
        {"id": "lig", "label": "TGF-β 配体", "kind": "entity", "group": "膜"},
        {"id": "rec", "label": "TβRII/TβRI 受体复合物", "kind": "subroutine",
         "group": "膜"},
        {"id": "smad", "label": "SMAD2/3 磷酸化", "kind": "circle",
         "group": "胞质"},
        {"id": "smad7", "label": "SMAD7 抑制因子", "kind": "hexagon",
         "group": "胞质"},
        {"id": "tf", "label": "靶基因转录", "kind": "stadium", "group": "核"},
    ],
    "edges": [
        {"from": "lig", "to": "rec", "label": "结合", "kind": "arrow"},
        {"from": "rec", "to": "smad", "label": "磷酸化", "kind": "arrow"},
        {"from": "smad", "to": "tf", "label": "入核", "kind": "arrow"},
        {"from": "smad7", "to": "rec", "label": "负反馈抑制", "kind": "inhibit"},
        {"from": "tf", "to": "smad7", "label": "诱导表达", "kind": "dashed"},
    ],
    "groups": [{"id": "膜", "label": "细胞膜"},
               {"id": "胞质", "label": "细胞质"},
               {"id": "核", "label": "细胞核"}],
}


def level(report, cid):
    for i in report["items"]:
        if i["id"] == cid:
            return i["level"]
    return ""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="craft_"))
    try:
        out = tmp / "d.svg"
        rep = D.render_diagram(SPEC, out)
        man = json.loads(Path(rep["layout"]).read_text("utf-8"))
        rep_c = C.audit(rep["layout"])

        print("── A 默认产物：六条规则都应该过 ──")
        truth(rep["ok"] and rep_c["ok"], f"A0 真出图且工艺审计全绿（{out.stat().st_size} B）")
        truth({i["id"] for i in rep_c["items"]} == {"P1", "P2", "P3", "P4", "P5",
                                                    "P6"},
              "A1 六条规则逐条有结论，没有一条缺项")
        truth(rep_c["metrics"]["colors"] <= len(SPEC["groups"]) + 2,
              f"A2 描边色 {rep_c['metrics']['colors']} 种 ≤ 区室数 + 2（不是彩虹）")

        print("── B 负控：把真产物改坏，规则必须翻红 ──")

        def mutate(fn):
            m = json.loads(json.dumps(man))
            fn(m)
            return C.audit(m)

        # B1 配色：同一区室两个节点不同色 ⇒ 按序号轮色回来了
        def b1(m):
            m["nodes"][0]["stroke"] = "#FF0000"
            if m["nodes"][1]["group"] == m["nodes"][0]["group"]:
                m["nodes"][1]["stroke"] = "#00FF00"
            else:                              # 造同一组两色
                m["nodes"][1]["group"] = m["nodes"][0]["group"]
                m["nodes"][1]["stroke"] = "#00FF00"
        truth(level(mutate(b1), "P1") == "FAIL", "B1 同区室两色 ⇒ 配色语义翻红")

        # B2 尺度：把一个节点撑到 4 倍
        def b2(m):
            m["nodes"][0]["box"][2] *= 4
        truth(level(mutate(b2), "P2") == "FAIL", "B2 单节点撑到 4 倍 ⇒ 尺度均衡翻红")

        # B3 断词：把「磷酸化」拆成「磷酸／化」
        def b3(m):
            for n in m["nodes"]:
                if n["id"] == "smad":
                    n["lines"] = ["SMAD2/3 磷酸", "化"]
        truth(level(mutate(b3), "P3") == "FAIL", "B3 末行孤字 ⇒ 折行质量翻红")

        # B4 压线：把边标签挪到另一条边的中段
        def b4(m):
            e = next(x for x in m["edges"] if x["label_box"])
            o = next(x for x in m["edges"] if x["k"] != e["k"])
            p0, p1 = o["points"][0], o["points"][1]
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            e["label_box"] = [mx - 20, my - 5, mx + 20, my + 5]
        truth(level(mutate(b4), "P4") == "FAIL", "B4 边标签压在别的线上 ⇒ 标签压线翻红")

        # B4b 标签互压：两个标签叠在同一处
        # （svgq 的碰撞门看不见这一种，P4 必须自己量）
        def b4b(m):
            a = next(x for x in m["edges"] if x["label_box"])
            b = next(x for x in m["edges"]
                     if x["label_box"] and x["k"] != a["k"])
            b["label_box"] = list(a["label_box"])
        truth(level(mutate(b4b), "P4") == "FAIL",
              "B4b 两个边标签叠在一起 ⇒ 标签压线翻红")

        # B5 框叠：让两个区室框完全重合
        def b5(m):
            c0, c1 = m["compartments"][0], m["compartments"][1]
            c1["box"] = list(c0["box"])
            c1["id"] = "核" if c0["id"] != "核" else "胞质"
        truth(level(mutate(b5), "P5") in ("FAIL", "WARN"),
              "B5 两块区室框重合 ⇒ 区室不嵌套翻红（或如实 WARN 同层占位）")

        # B6 穿节点：把回边的中段改成一条横穿某个节点框的直线
        def b6(m):
            rev = next(x for x in m["edges"] if x["reversed"])
            node = m["nodes"][2]["box"]
            cy = node[1] + node[3] / 2
            rev["points"] = [rev["points"][0],
                             [node[0] - 10, cy], [node[0] + node[2] + 10, cy],
                             rev["points"][-1]]
        truth(level(mutate(b6), "P6") == "FAIL", "B6 回边穿过节点 ⇒ 回边不穿节点翻红")

        print("── C 结构纪律 ──")
        src = Path(C.__file__).read_text("utf-8")
        mods = {a.name.split(".")[0] for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) and n.module}
        truth(mods <= {"__future__", "json", "re", "pathlib"},
              f"C1 审计层仍是零第三方依赖（imports={sorted(mods)}）")
        truth("honest_limit" in rep_c and rep_c["scope"] == "geometry",
              "C2 回执里写明「几何口径、未替代实拍与审美」——不冒充审过")
        truth(level(rep_c, "P6") in ("PASS", "SKIP"),
              "C3 正控 默认布局的回边不穿节点（不是靠负控才有分辨率）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print(f"\n[DONE] 机制图工艺审计：{PASS_N} PASS / {FAIL_N} FAIL")
    sys.exit(1 if FAIL_N else 0)
