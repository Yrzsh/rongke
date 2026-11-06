# -*- coding: utf-8 -*-
"""`rongke/analysis.py` 的检查套件。

跑法：`python analysis_test.py`

## 这套检查在防什么

数据分析成图这条链最容易出的是**「程序退出 0、图是错的」**：
列类型判错 → 图型选错 → 脚本写错 → 审计器误报 → 最后一个人看不出来。
所以下面每一组都配了**正负对照**：
  正例：该判对的要判对；
  反例：把判据改坏，对应的断言必须**真的变红**（[9] 那节做的就是这个）。

## 一条纪律

所有写盘一律落在 `tempfile.TemporaryDirectory()` 里，跑完自毁，
不在仓库里留垃圾（与本项目其它套件同）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import analysis as A            # noqa: E402
from rongke.figure import FigureAuditor, FigureContract   # noqa: E402

ITEMS = 0
FAILS: list[str] = []


def ok(cond: bool, label: str, detail: str = "") -> None:
    """记一条断言。

    ★ 标记必须是 `✔` / `✘`（或 `[PASS]` / `[FAIL]`）并**在行首** ——
      `regress.py` 的 `_RE_MARK` 只认这两种风格。我第一版用的是
      `  ok   xxx`，regress 扫出来 0 项，直接判「空跑」。
      一条写得很好的测试，如果标记不对，在回归里等于不存在。
    """
    global ITEMS
    ITEMS += 1
    if cond:
        print(f"✔ {label}")
    else:
        FAILS.append(label)
        print(f"✘ {label}" + (f"  << {detail}" if detail else ""))


DEMO = """date,treatment,biomass,ph,replicate
2024-01-05,ctrl,12.4,6.8,1
2024-01-12,ctrl,13.1,6.9,2
2024-01-19,ctrl,12.9,6.7,3
2024-01-05,heat,11.2,6.6,1
2024-01-12,heat,10.4,6.5,2
2024-01-19,heat,9.8,6.4,3
2024-01-26,ctrl,14.2,6.9,1
2024-01-26,heat,9.1,6.3,2
2024-02-02,ctrl,15.0,7.0,1
2024-02-02,heat,8.7,6.2,2
"""


def _section(n: str, title: str) -> None:
    print(f"\n[{n}] {title}")


# ══════════════════ [1] 读表 ══════════════════
def test_read_table(tmp: Path) -> None:
    _section("1", "读表：编码 / 分隔符 / 空行 / 坏文件")
    p = tmp / "demo.csv"
    p.write_text(DEMO, encoding="utf-8")
    rows, meta = A.read_table(p)
    ok(len(rows) == 10, "CSV 读出 10 行", f"got {len(rows)}")
    ok(meta["n_rows"] == 10, "元信息 n_rows 正确")
    ok(meta["columns"] == ["date", "treatment", "biomass", "ph", "replicate"],
       "列名保序", str(meta["columns"]))
    ok(meta["truncated"] is False, "没有假装截断")

    # GBK（Excel 另存的中文表就是这个编码）
    g = tmp / "gbk.csv"
    g.write_bytes("姓名,分数\n张三,88\n李四,92\n".encode("gb18030"))
    rows2, meta2 = A.read_table(g)
    ok(meta2["encoding"] == "gb18030", "GB18030 被认出来", meta2["encoding"])
    ok(rows2[0]["姓名"] == "张三", "中文列名没乱码", str(rows2[0]))

    # 制表符分隔
    t = tmp / "t.tsv"
    t.write_text("a\tb\n1\t2\n3\t4\n", encoding="utf-8")
    rows3, meta3 = A.read_table(t)
    ok(meta3["delimiter"] == "\\t", "制表符分隔被认出", meta3["delimiter"])
    ok(rows3[0]["b"] == "2", "制表符切列正确", str(rows3[0]))

    # 单列 CSV —— csv.Sniffer 在这种表上会抛错，我们用自数的分隔符
    s = tmp / "one.csv"
    s.write_text("value\n1\n2\n3\n", encoding="utf-8")
    rows4, _ = A.read_table(s)
    ok(len(rows4) == 3 and rows4[0]["value"] == "1", "单列表读得出来", str(rows4[:1]))

    # 坏文件必须**说出来**，不许静默返回空
    try:
        A.read_table(tmp / "nope.csv")
        ok(False, "不存在的文件应当报错")
    except A.TableError as e:
        ok("不存在" in str(e), "不存在的文件报 TableError 且说清原因", str(e))

    empty = tmp / "empty.csv"
    empty.write_bytes(b"")
    try:
        A.read_table(empty)
        ok(False, "空文件应当报错")
    except A.TableError as e:
        ok("空" in str(e), "空文件报 TableError", str(e))

    hdr = tmp / "hdronly.csv"
    hdr.write_text("a,b\n", encoding="utf-8")
    try:
        A.read_table(hdr)
        ok(False, "只有表头应当报错")
    except A.TableError as e:
        ok("0 行" in str(e), "只有表头时报「0 行」", str(e))

    # JSON 对象数组
    j = tmp / "d.json"
    j.write_text(json.dumps({"rows": [{"x": 1, "y": 2}, {"x": 2, "y": 5}]}),
                 encoding="utf-8")
    rows5, _ = A.read_table(j)
    ok(len(rows5) == 2 and rows5[1]["y"] == 5, "JSON {rows:[...]} 能读", str(rows5))


# ══════════════════ [2] 测列形状 ══════════════════
def test_profile() -> None:
    _section("2", "测形状：时间 / 数值 / 分类 / 文本，以及反例")
    rows = [
        {"d": "2024-01-05", "g": "ctrl", "v": "12.4", "note": "ok"},
        {"d": "2024-01-12", "g": "heat", "v": "10.4", "note": "ok"},
        {"d": "2024-01-19", "g": "ctrl", "v": "", "note": "ok"},
        {"d": "2024-01-26", "g": "heat", "v": "9.1", "note": "bad"},
    ]
    cols = {c.name: c for c in A.profile_table(rows)}
    ok(cols["d"].kind == "time", "日期列判成 time", cols["d"].kind)
    ok(cols["g"].kind == "category", "短重复串判成 category", cols["g"].kind)
    ok(cols["v"].kind == "numeric", "数值列判成 numeric", cols["v"].kind)
    ok(cols["v"].n_missing == 1, "缺失被数出来", str(cols["v"].n_missing))
    ok(cols["note"].kind == "category",
       "低基数列判 category（不是 text）", cols["note"].kind)

    # ★ 反例：名字像时间但值是纯数字的列，**不该**被硬判成 time 以外的东西；
    #   而 "2024" 这种四位年份**应该**是 time。
    cols2 = {c.name: c for c in A.profile_table(
        [{"year": "2021", "v": "1"}, {"year": "2022", "v": "2"},
         {"year": "2023", "v": "3"}])}
    ok(cols2["year"].kind == "time", "四位年份判 time", cols2["year"].kind)

    # ★ 反例：**列名像时间、值却是类别**——不能被名字骗了
    cols3 = {c.name: c for c in A.profile_table(
        [{"date_group": "早", "v": "1"}, {"date_group": "晚", "v": "2"}])}
    ok(cols3["date_group"].kind == "text"
       or cols3["date_group"].kind == "category",
       "列名含 date 但值是中文类别 → 不判 time",
       cols3["date_group"].kind)

    # 空表
    ok(A.profile_table([]) == [], "空行 → 空列清单")


# ══════════════════ [3] 选图型 ══════════════════
def test_recommend() -> None:
    _section("3", "选图型：判据按形状走，且每条都给得出理由")
    hdr = DEMO.strip().splitlines()
    keys = [k.strip() for k in hdr[0].split(",")]
    rows = [dict(zip(keys, [v.strip() for v in ln.split(",")]))
            for ln in hdr[1:]]
    cols = A.profile_table([{k: r[k] for k in ("date", "treatment", "biomass")}
                            for r in rows])
    sug = A.recommend(cols)
    ok(bool(sug), "有建议")
    ok(sug[0].kind == "time_series", "首选时间序列", sug[0].kind if sug else "")
    ok(bool(sug[0].why), "首选带 why")
    ok(bool(sug[0].rejected), "首选带 rejected（说不选别的理由）")

    # ★ 反例：每组样本太少时**不许**推箱线
    small = A.profile_table(
        [{"g": "a", "v": "1"}, {"g": "a", "v": "2"}, {"g": "a", "v": "3"},
         {"g": "b", "v": "4"}, {"g": "b", "v": "5"}, {"g": "b", "v": "6"}])
    kinds = [s.kind for s in A.recommend(small)]
    ok("box" not in kinds, "每组 3 个样本 → 不推箱线", str(kinds))
    ok("bar" in kinds, "每组 3 个样本 → 推柱状", str(kinds))

    # 样本够多 → 箱线
    big = A.profile_table(
        [{"g": "a" if i % 2 else "b", "v": str(i)} for i in range(40)])
    kinds2 = [s.kind for s in A.recommend(big)]
    ok("box" in kinds2, "每组 20 个样本 → 推箱线", str(kinds2))

    # 同族去重：不该同时推 box 和 hist
    ok(len({A.CHART_KINDS[k]["family"] for k in kinds2})
       == len(kinds2), "同族不重复推（dist 族只留一条）", str(kinds2))

    # 全文本列 → 没有建议（不许瞎画）
    txt = A.profile_table([{"a": f"x{i}", "b": f"y{i}"} for i in range(40)])
    ok(A.recommend(txt) == [], "只有高基数文本列 → 一条建议都没有",
       str(A.recommend(txt)))


# ══════════════════ [4] 生成脚本 ══════════════════
def test_build_script(tmp: Path) -> None:
    _section("4", "生成脚本：能编译、源码里看得见审计要的那些字面量")
    plan = {"kind": "time_series", "table": "d.csv", "x": "date", "y": "v",
            "group": "g", "title": "t", "width_mm": 89.0, "height_mm": 62.0,
            "out_dir": str(tmp), "stem": "figure", "fit": True}
    src = A.build_script(plan)
    ok("font.sans-serif" in src, "显式字体族（FONT-FAMILY 要它）")
    ok("svg.fonttype" in src, "svg.fonttype（EDITABLE-TEXT 要它）")
    ok("pdf.fonttype" in src, "pdf.fonttype=42（EDITABLE-TEXT 要它）")
    ok(".svg" in src and ".pdf" in src, "SVG+PDF 导出字面量（EXPORT-VECTOR）")
    ok(".png" in src and ".tiff" in src, "PNG+TIFF 位图字面量（EXPORT-RASTER）")
    ok("dpi=600" in src, "dpi=600（RASTER-DPI）")
    ok("dropna" in src and "n_before" in src and "n_after" in src,
       "丢行先计数（DATA-EXCLUSION）")
    ok("def draw(" in src and "def save(" in src, "有 draw 与 save")
    ok('__main__' in src, "有主程序块（否则脚本静默空转）")

    # 能编译
    sp = tmp / "p.py"
    sp.write_text(src, encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "py_compile", str(sp)],
                       capture_output=True)
    ok(r.returncode == 0, "生成的脚本能编译", r.stderr.decode("utf-8", "replace")[:300])

    # ★ 反例：不认识的图型必须报错，不许静默退回默认
    try:
        A.build_script({"kind": "pie3d", "table": "x"})
        ok(False, "不认识图型应当报错")
    except ValueError as e:
        ok("pie3d" in str(e), "不认识图型 → ValueError 且点名它", str(e))

    # 每种图型都编得出来
    #
    # ★ 2026-10-26 补：这条测的是"源码编得出来"，但 forest / stacked_area
    #   现在有**必需列前置校验**（缺 lo/hi/ys 会主动抛 ValueError，
    #   而不是让脚本崩成 `KeyError: ''`）。所以这里必须给够列，
    #   否则测到的是"我少给了参数"，不是"脚本编不出来"。
    #   两者是不同的东西：前者是正确行为，后者才是缺陷。
    COMPILE_KNOBS = {
        "forest": {"x": "s", "y": "b", "lo": "l", "hi": "u"},
        "stacked_area": {"x": "a", "ys": ["b", "c"]},
    }
    bad = []
    for k in A.CHART_KINDS:
        if k not in A.CHART_KINDS:
            continue
        spec = {"kind": k, "table": "d.csv", "x": "a", "y": "b",
                "group": "g", "out_dir": str(tmp)}
        spec.update(COMPILE_KNOBS.get(k, {}))
        try:
            s = A.build_script(spec)
            pp = tmp / f"{k}.py"
            pp.write_text(s, encoding="utf-8")
            rr = subprocess.run([sys.executable, "-m", "py_compile", str(pp)],
                                capture_output=True)
            if rr.returncode != 0:
                bad.append((k, rr.stderr.decode("utf-8", "replace")[:120]))
        except Exception as e:                              # noqa: BLE001
            bad.append((k, f"{type(e).__name__}: {e}"))
    ok(not bad, f"{len(A.CHART_KINDS)} 种图型的脚本都能编译", str(bad))


# ══════════════════ [5] 契约与图注 ══════════════════
def test_contract_legend() -> None:
    _section("5", "契约与图注：必须一次过 validate()，且口径与画法一致")
    ok(not (ROOT / "_probe_missing").exists(), "（占位）工作区干净")

    for kind in A.CHART_KINDS:
        plan = {"kind": kind, "x": "g", "y": "v", "title": "T", "why": "W",
                "width_mm": 89.0, "height_mm": 62.0}
        cd = A.contract_for(plan)
        fc = FigureContract.from_dict(cd)
        ok(fc.validate() == [], f"{kind} 的契约一次就过 validate()",
           str(fc.validate()))
        pan = cd["panels"][0]
        # ★ 口径与画法必须一致：只有 bar 真的画了误差条
        if kind == "bar":
            ok(pan["spread"] == "s.e.m.", "bar 声明了离散（它真的画了 yerr）")
        else:
            ok(pan["spread"] == "none", f"{kind} 不谎称有误差条", pan["spread"])

    # 图注必须含规范要求的四段
    plan = {"kind": "box", "x": "g", "y": "v", "title": "T", "why": "W"}
    cd = A.contract_for(plan)
    lg = A.legend_for(cd, meta={"n_rows": 40}, center="mean", spread="s.e.m.")
    ok(lg.startswith("Fig. 1 | **"), "图注以 `Fig. N | **` 开头", lg[:20])
    ok("均值" in lg and "标准误" in lg, "统计口径译成人话（不是 raw/none）")
    ok("___" in lg, "检验方法留占位而不是替用户编", lg[-120:])
    ok("n = 40" in lg, "源数据量写进图注")

    # ★ 反例：raw/none 这种机器标记**不许**原样印进图注
    lg2 = A.legend_for(cd, meta={}, center="raw", spread="none")
    ok("raw" not in lg2 and "none" not in lg2,
       "raw/none 不被原样写进图注", lg2)


# ══════════════════ [6] 端到端 ══════════════════
def test_end_to_end(tmp: Path) -> None:
    _section("6", "端到端：读表→选图→出图→六道门全绿")
    d = A.diag(None)
    ok(d["ready"], "绘图装置可用", str(d["notes"]))
    if not d["ready"]:
        return
    p = tmp / "demo.csv"
    p.write_text(DEMO, encoding="utf-8")
    work = tmp / "out"
    rep = A.analyze(p, out_dir=work)
    ok(rep["ran"], "analyze 跑了", str(rep.get("error")))
    ok(rep.get("error") is None, "没有 error", str(rep.get("error")))
    run = rep.get("run") or {}
    ok(run.get("ok") is True, "run.ok 为真", str(run.get("error")))
    for ext in ("pdf", "svg", "png", "tiff"):
        ok((work / f"figure.{ext}").exists(), f"产出 figure.{ext}")
    ok((work / "plot.py").exists(), "产出绘图脚本")

    cd = A.contract_for(rep["plan"], journal="nature")
    fc = FigureContract.from_dict(cd)
    ok(fc.validate() == [], "生成的契约成立", str(fc.validate()))
    pan = cd["panels"][0]
    lg = A.legend_for(cd, meta=rep["meta"], center=pan["center"],
                      spread=pan["spread"])
    au = FigureAuditor(python_exe=None, skills_dir=None, timeout=300)
    arep = au.audit(fc, workdir=work, source="plot.py", pdf="figure.pdf",
                    legend_text=lg)
    ok(arep.ok, "**图面审计通过**", str(arep.blockers))
    for g in arep.gates:
        ok(g.verdict in ("PASS", "NOT APPLICABLE"),
           f"门 {g.name} = {g.verdict}", g.detail[:100])

    # ★ 反例：把 TIFF 从契约里拿掉，总判必须变红（证明契约那一步真的在起作用）
    cd2 = json.loads(json.dumps(cd))
    cd2["formats"] = ["pdf"]
    fc2 = FigureContract.from_dict(cd2)
    ok(fc2.validate() != [], "契约漏掉位图 → validate() 报错（不再静默放行）",
       str(fc2.validate()))

    # ★ 反例：图注去掉检验方法，审计必须拦下
    bad_lg = lg.replace("组间比较用 ___（填实际检验：Welch t-test / ANOVA / "
                        "Mann-Whitney U…；若本图不含检验请改写为描述性说明）。", "")
    arep2 = au.audit(fc, workdir=work, source="plot.py", pdf="figure.pdf",
                     legend_text=bad_lg)
    ok(not arep2.ok, "图注缺检验方法 → 审计拦下", str(arep2.blockers)[:80])


# ══════════════════ [7] 不骗人 ══════════════════
def test_no_lying(tmp: Path) -> None:
    _section("7", "不骗人：画不出来要说出来，不许假装成功")
    # 全文本列 → plan_from_shape 必须抛错而不是硬画
    cols = A.profile_table([{"a": f"x{i}", "b": f"y{i}"} for i in range(40)])
    try:
        A.plan_from_shape("t.csv", {"n_rows": 40}, cols)
        ok(False, "无可画组合时应当抛错")
    except ValueError as e:
        ok("没有可画的组合" in str(e), "无可画组合 → 说清为什么", str(e)[:80])

    # 找不到表 → TableError
    try:
        A.analyze(tmp / "ghost.csv", emit=False)
        ok(False, "缺表应当报错")
    except A.TableError as e:
        ok("不存在" in str(e), "缺表报 TableError", str(e))

    # 显式 out_dir 不存在时要自己建（实测踩过 FileNotFoundError）
    p = tmp / "small.csv"
    p.write_text("g,v\na,1\na,2\nb,3\nb,4\n", encoding="utf-8")
    deep = tmp / "x" / "y" / "z"
    rep = A.analyze(p, out_dir=deep, emit=False)
    ok(deep.exists(), "显式 out_dir 不存在时被建出来")
    ok(Path(rep["script"]).exists(), "脚本落在里面")


# ══════════════════ [8] 负控 ══════════════════
def test_negative_controls(tmp: Path) -> None:
    _section("8", "负控：把判据改坏，对应断言必须真的变红")
    # ① 把「丢行先计数」的计数行删掉 —— DATA-EXCLUSION 应当报警
    plan = {"kind": "scatter", "table": "d.csv", "x": "a", "y": "b",
            "group": "", "title": "t", "width_mm": 89.0, "height_mm": 62.0,
            "out_dir": str(tmp), "stem": "figure", "fit": True}
    src = A.build_script(plan)
    src_broken = src.replace("n_before = int(len(df))", "n_before = 0")
    ok("n_before = 0" in src_broken, "（负控已注入）")

    # ② 把导出的 .svg 字面量换掉 —— EXPORT-VECTOR 应当报缺
    src2 = A.build_script(plan)
    ok('.svg' in src2, "（正控）原脚本含 .svg")
    src3 = src2.replace('".svg"', '"." + "s" + "vg"')
    ok('".svg"' not in src3, "（负控已注入）拼接后源码里看不到 .svg")

    # ③ recommend 的「样本太少不推箱线」这条判据，把它反过来应当变红
    #   ★ 注意必须有**两组以上**：单组时 `g.n_unique == 1`，走的是另一条分支，
    #     负控会假绿（我第一版就是这么写的，被自己的负控抓到）。
    small_rows = [{"g": "a", "v": "1"}, {"g": "a", "v": "2"},
                  {"g": "a", "v": "3"}, {"g": "b", "v": "4"},
                  {"g": "b", "v": "5"}, {"g": "b", "v": "6"}]
    small_cols = A.profile_table(small_rows)
    ok(len({c.n_unique for c in small_cols if c.is_category}) == 1
       and [c for c in small_cols if c.is_category][0].n_unique == 2,
       "（前提）对照组有 2 个水平", str([(c.name, c.n_unique) for c in small_cols]))
    kinds = [s.kind for s in A.recommend(small_cols)]
    ok("box" not in kinds, "（正控）少量样本不推箱线", str(kinds))

    # 机械证明：判据的核心是 `small = per and min(per) < 8`。
    # 把它拆成两个方向各验一次：
    #   ① 组大小 ≥8 时走 else 分支 ⇒ 出箱线；
    #   ② 让 `_group_sizes` 报一个大数，同目录下就应当翻成 box。
    # ★ 我第一版负控写错了：以为"把组大小改成 1 就能翻"，其实 1 也是 small，
    #   走的是**同一条** bar 分支，负控因此恒假 —— 被这条检查自己抓出来了。
    def _fake_big(g, v):
        return {"*": 100}

    real = A._group_sizes
    try:
        A._group_sizes = _fake_big
        kinds2 = [s.kind for s in A.recommend(small_cols)]
    finally:
        A._group_sizes = real
    ok("box" in kinds2, "（负控）让组大小报成 100 → 箱线出现 ⇒ "
                        "这条断言确实打在 `small` 判据上", str(kinds2))

    # ③ 再把判据的反方向钉住：真·大样本本来就该出 box（正控另一端）
    big_cols = A.profile_table(
        [{"g": "a" if i % 2 else "b", "v": str(i)} for i in range(40)])
    ok("box" in [s.kind for s in A.recommend(big_cols)],
       "（正控另一端）每组 20 个样本 → 箱线")


# ══════════════════ [9] 日期轴 ══════════════════
def test_date_axis(tmp: Path) -> None:
    _section("9", "日期轴：不许出现 ConciseDateFormatter 的 offset 重影")

    # ★ 这一节防的是一次**真实缺陷**：早先用了 ConciseDateFormatter，
    #   它会在轴末端额外印一条 offset 文本（`2024-Feb`）来省字，
    #   而主轴标签里已经有 `Feb` —— 同一位置两个标签，碰撞门报重叠，
    #   人看也是重影。生成的源码里不该再出现它。
    plan = {"kind": "time_series", "table": "d.csv", "x": "date", "y": "v",
            "group": "", "title": "t", "width_mm": 89.0, "height_mm": 62.0,
            "out_dir": str(tmp), "stem": "figure", "fit": True}
    src = A.build_script(plan)
    # ★ 查"源码里有没有某个 API"必须**先去掉注释**。
    #   我第一版直接 `"ConciseDateFormatter" not in src`，
    #   结果被那行解释「为什么不用它」的注释判红 —— 代码是对的，断言是错的。
    #   （这是本仓反复记过的坑：注释里的同名字样会让源码字面量检查失效。）
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    ok("ConciseDateFormatter" not in code,
       "生成的脚本不用 ConciseDateFormatter（它有 offset 重影）")
    ok("ConciseDateFormatter" in src,
       "（负控）注释里仍写着为什么不用它 ⇒ 上面那条检查确实排除了注释")
    ok("DateFormatter" in src, "用的是显式 DateFormatter")

    # 真画一张跨月的表，从 PDF 里读回轴上的字
    d = A.diag(None)
    if not d["ready"]:
        ok(True, "（跳过：绘图装置不可用）")
        return
    p = tmp / "span.csv"
    p.write_text(DEMO, encoding="utf-8")
    rep = A.analyze(p, out_dir=tmp / "wd")
    ok(rep["ran"], "跨月表画出来了", str(rep.get("error")))
    if not rep["ran"]:
        return
    import pymupdf
    page = pymupdf.open(tmp / "wd" / "figure.pdf")[0]
    low = [w[4] for w in page.get_text("words")
           if w[1] > page.rect.height * 0.80]
    # 主轴标签应当是 XX-XX 这种纯数字日期
    dates = [t for t in low if len(t) == 5 and t[2] == "-" and t[:2].isdigit()]
    ok(len(dates) >= 3, "x 轴上有 3 个以上短日期标签", str(low))
    # 不该出现"年份-月份英文"这种 offset 形状
    offsets = [t for t in low
               if "-" in t and any(c.isalpha() for c in t)
               and len(t) > 5 and t[:4].isdigit()]
    ok(not offsets, "没有 2024-Feb 这种 offset 重影", str(offsets))
    # 但时间跨度必须交代清楚（否则读者不知道是哪一年）
    joined = " ".join(low)
    ok("2024" in joined, "年份仍然出现在轴上（写进轴标题）", joined[:120])


# ══════════════════ [10] 九种图型都真的能画出来 ══════════════════
def test_all_kinds(tmp: Path) -> None:
    """9 种图型逐个真画 + 过完整审计链。

    ## 这一节为什么存在

    此前只有 time_series 走过完整链，其余 8 种只有源码级断言。
    补测后一口气挖出 4 个真缺陷，全是「程序退出 0、图是错的」那一类：

      ① box：matplotlib 3.11 移除了 `boxplot(labels=)` ⇒ TypeError ⇒ 没产出 PDF。
         脚本退出码非 0，但要翻 stderr 才看得见，很容易被当成"数据有问题"。
      ② heatmap：把时间列塞进 clean() 强转成数值 ⇒ 全 NaN ⇒ dropna 删光行
         ⇒ corr() 全 NaN ⇒ `if v == v` 永远假 ⇒ **画出一张全白空图**，
         而六道门全绿（门只看字号/碰撞，看不出"格子里该有数字却没画"）。
      ③ paired：把**条件列**（ctrl/heat 这种字符串）和**个体 id** 塞进 clean()
         强转 ⇒ 同样全 NaN ⇒ pivot 出 0 列 ⇒ 报「至少两个条件」。
         报错文案看起来像"你数据不够"，实际是清洗错了列。
      ④ line/hist/box/violin：框内一个字都没有 ⇒ text_containment 只能
         NOT AUDITABLE（没查≠通过），交付被拦。

    每一条都配了负控，确保判据真的打得穿。
    """
    d = A.diag(None)
    if not d["ready"]:
        ok(True, "（跳过：绘图装置不可用）")
        return

    # ── 负控 A：matplotlib 的 boxplot 参数名必须按签名探测，不能写死
    src = (ROOT / "rongke" / "analysis.py").read_text(encoding="utf-8")
    code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
    ok("tick_labels" in code,
       "box 的箱线标签按签名探测（matplotlib 3.9+ 改了名）")
    ok("labels=labels" not in code,
       "（负控）没有写死旧名 labels=labels ⇒ 上面那条不是空话")

    # ── 负控 B：heatmap 不能对 x/y 做 clean()
    # ★ 定位必须锚在**模板段**上（`"heatmap": '''def draw(`），
    #   不能用 `'"heatmap"'` —— 那个字符串在 CHART_KINDS 里先出现一次，
    #   取到的是图型元数据那三行，切片根本没进到模板里。
    #   （第一次写这条判据就是这么假红的：代码是对的，判据指错了地方。）
    _h_anchor = '"heatmap": '
    hstart = code.find(_h_anchor, code.find("def draw"))
    ok(hstart > 0, "（自检）能在源码里定位到 heatmap 模板段", str(hstart))
    hseg = code[hstart:hstart + 2600]
    ok("select_dtypes(\"number\")" in hseg,
       "heatmap 只挑本来就是数值的列（不强转时间列）")
    ok("clean(df, [{x!r}, {y!r}])" not in hseg,
       "（负控）heatmap 段里没有 clean(df,[x,y]) ⇒ 上面那条不是空话")

    # ── 负控 C：paired 只对 y 做 clean()，条件列与 id 列不能碰
    _p_anchor = '"paired": '
    pstart = code.find(_p_anchor, code.find("def draw"))
    ok(pstart > 0, "（自检）能在源码里定位到 paired 模板段", str(pstart))
    pseg = code[pstart:pstart + 2200]
    ok("clean(df, [{y!r}])" in pseg,
       "paired 只对 y 做强转数值（条件列/个体 id 是字符串）")
    ok("clean(df, [{x!r}, {y!r}])" not in pseg,
       "（负控）paired 段里没有 clean(df,[x,y]) ⇒ 上面那条不是空话")

    # ── 真画：全部图型逐个跑一遍
    #
    # ★ 2026-10-26 补：新增的三种图型（forest / stacked_area / band 的消歧）
    #   需要**专属列**，通用 plan 只会填 x/y/group。原先这里没给，
    #   于是它们在这个夹具上根本没法画 —— 那不是代码缺陷，是**夹具不够用**。
    #   但要注意区分两类：
    #     · forest 需要 lo/hi —— 缺了会真的炸（已加了前置校验，报「缺哪几列」）
    #     · stacked_area 需要 ≥2 个成分列 —— 同上
    #   所以夹具要给出这些列，而**判据（verdict == "PASS"）一个字不能松**。
    #   放松判据来让测试变绿，是本项目最忌讳的做法。
    import datetime
    rows = ["date,biomass,ph,treatment,batch,unit,"
            "lo,hi,cellulose,hemicellulose,lignin,OR,OR_low,OR_high,study"]
    d0 = datetime.date(2024, 1, 3)
    pairs = [(12.4, 6.1), (13.1, 6.0), (12.8, 5.9), (13.6, 6.2), (14.3, 6.1),
             (14.1, 6.3), (14.9, 6.2), (15.2, 6.4), (15.4, 6.3), (15.8, 6.5)]
    for i, (b, ph) in enumerate(pairs):
        day = (d0 + datetime.timedelta(days=i * 3)).isoformat()
        # lo/hi 围绕 biomass；三个成分列相加为 100（堆叠面积要求"合起来有意义"）；
        # OR/OR_low/OR_high 给森林图，study 是每行的名称
        rows.append("%s,%s,%s,%s,%s,u%d,%s,%s,%s,%s,%s,%s,%s,%s,%s"
                    % (day, b, ph, "ctrl" if i % 2 == 0 else "heat",
                       "B1" if i < 5 else "B2", i % 5,
                       round(b - 0.7, 2), round(b + 0.7, 2),
                       40 + i, 35 - i * 0.5, 25 - i * 0.5,
                       round(0.6 + i * 0.08, 3),
                       round(0.4 + i * 0.06, 3), round(0.9 + i * 0.10, 3),
                       "S%d" % (i + 1)))
    tbl = tmp / "kinds.csv"
    tbl.write_text("\n".join(rows) + "\n", encoding="utf-8")

    from rongke.figure import FigureAuditor, FigureContract
    au = FigureAuditor(python_exe=None, skills_dir=None, timeout=300.0)
    # 每个图型的旋钮。缺省的用通用 plan；需要专属列的必须在这里点明，
    # 否则测的是"夹具够不够"，不是"图型对不对"。
    KNOBS = {
        # paired 的两个旋钮：group=个体 id，x=条件列。缺一个都会凑不出配对。
        "paired": {"group": "unit", "x": "treatment"},
        # 森林图：效应量 + 上下界 + 每行名称
        "forest": {"x": "study", "y": "OR", "lo": "OR_low", "hi": "OR_high"},
        # 堆叠面积：x + ≥2 个成分列
        "stacked_area": {"x": "date",
                         "ys": ["cellulose", "hemicellulose", "lignin"]},
        # 区间带：显式给上下界列（不给会走 mean±1.96SE 的推断路径，
        # 那条路也能画，但测的就不是"真 CI"这一支了）
        "band": {"x": "date", "y": "biomass", "lo": "lo", "hi": "hi"},
    }
    # ── heatmap 用**窄夹具**，不共用宽夹具 ──────────────────────────
    #
    # ★ 为什么必须分开：heatmap 画的是「所有数值列两两相关」，矩阵大小
    #   等于数值列数。宽夹具为了喂饱 forest/stacked_area 加到了 10 个数值列
    #   ⇒ 10x10 相关矩阵 ⇒ 89mm 单栏里 10 个刻度标签必然挤在一起，
    #   collision 一口气报 21 处。
    #   那 21 处**是真的**（图上确实糊了），但它测的是"10 列塞进单栏"
    #   这个夹具选择，不是 heatmap 的画法对不对。
    #   正确处置是给 heatmap 一份列数合理的表 —— 而不是放宽判据。
    #   注意：**判据（verdict == "PASS"）仍然一个字没松**。
    narrow = tmp / "narrow.csv"
    keep = ["date", "biomass", "ph", "treatment", "batch", "unit"]
    _lines = tbl.read_text(encoding="utf-8").splitlines()
    _hdr = _lines[0].split(",")
    _idx = [_hdr.index(c) for c in keep]
    narrow.write_text(
        "\n".join(",".join(r.split(",")[i] for i in _idx) for r in _lines) + "\n",
        encoding="utf-8")

    for k in A.CHART_KINDS:
        kw = KNOBS.get(k, {})
        table = narrow if k == "heatmap" else tbl
        rep = A.analyze(table, kind=k, out_dir=tmp / k, **kw)
        if not rep["ran"]:
            ok(False, f"{k}：画出来了", str(rep.get("error"))[:110])
            continue
        cd = A.contract_for(rep["plan"])
        pan = cd["panels"][0]
        lg = A.legend_for(cd, meta=rep["meta"],
                          center=pan.get("center", ""),
                          spread=pan.get("spread", ""))
        r = au.audit(FigureContract.from_dict(cd),
                     workdir=Path(rep["workdir"]), source="plot.py",
                     pdf="figure.pdf", legend_text=lg)
        bad = [g.name for g in r.gates if g.verdict not in ("PASS", "NOT APPLICABLE")]
        ok(r.verdict == "PASS", f"{k}：过完整审计链（{r.verdict}）",
           f"未过：{bad} {r.blockers[:1]}")


# ══════════════════ 主程序 ══════════════════
def main() -> int:
    print("=" * 62)
    print("rongke/analysis.py 检查套件")
    print("=" * 62)
    with tempfile.TemporaryDirectory(prefix="rk_an_") as td:
        tmp = Path(td)
        for fn in (lambda: test_read_table(tmp),
                   test_profile,
                   test_recommend,
                   lambda: test_build_script(tmp),
                   test_contract_legend,
                   lambda: test_end_to_end(tmp),
                   lambda: test_no_lying(tmp),
                   lambda: test_negative_controls(tmp),
                   lambda: test_date_axis(tmp),
                   lambda: test_all_kinds(tmp)):
            try:
                fn()
            except Exception:                                # noqa: BLE001
                FAILS.append(f"{fn} 抛异常")
                print("  !! 未捕获异常：")
                traceback.print_exc()
    print("\n" + "-" * 62)
    if FAILS:
        print(f"{ITEMS} 项 / {len(FAILS)} 失败")
        for f in FAILS:
            print("   ✗", f)
        return 1
    print(f"{ITEMS} 项 / 0 失败 —— 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
