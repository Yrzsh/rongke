# -*- coding: utf-8 -*-
"""工具「可调用种类」的目录单源（2026-11-11 GitHub 调研备料）。

## 为什么有这个文件

工具框架定了之后，后续优化就是**往资产库里加种类**——相当于软件新增功能。
但「有哪些种类、从哪个仓库来、许可能不能并进发行件」如果散在聊天记录里，
下次加种类一定会重复调研、甚至误把 GPL / CC BY-SA 的数据搬进 Apache 框架。

这里把三件事固化成**可读回的数据**：
  ① `SOURCES`   —— GitHub 读回的仓库元数据（stars / license / 许可等级）
  ② `CATALOGS`  —— 每个工具「现在有哪些」＋「建议加哪些」，逐条带来源与批次
  ③ `verify_current()` —— 拿真实枚举来比，目录与代码漂移当场报红

## 边界

  · 这是**开发期资产目录**，不是给 AI 的新工具：不进 `/api/tools`，
    不改任何现有枚举，也不下载第三方代码/字体。
  · 绝大多数上游是 TS/JS/TeX，内核是 Python 零依赖 ⇒ 只借**种类名称与数值规范**
    这些事实，实现必须自己用标准库写（同 svgdiagram 避开 grandalf 的先例）。
  · 唯一可整份搬的是纯数据且许可兼容的 ColorBrewer JSON（Apache-2.0，
    保留版权声明与 Cynthia Brewer 致谢）。
"""
from __future__ import annotations

#: GitHub 连接器 2026-11-11 读回；stars / license 会变，接种类前需复核。
READBACK_DATE = "2026-11-11"

LICENSE_GRADE = {
    "MIT": "green", "BSD-3-Clause": "green", "Apache-2.0": "green",
    "OFL-1.1": "green",
    "CC BY-SA 3.0": "yellow", "LPPL-1.3c": "yellow",
    "NOASSERTION": "red", "GPL-2.0": "red", "GPL-3.0": "red",
}

#: green = 可借清单/数值；yellow = 只取事实或外部引用，不进框架/发行件；red = 排除。
GRADE_RULE = {
    "green": "可借种类清单、几何/版式数值、结构；纯数据且 Apache-2.0 可整份搬（留致谢）",
    "yellow": "ShareAlike / LPPL：只把规范当事实自己实现，或运行时外部引用，不进公开仓与发行件",
    "red": "与 Apache-2.0 双轨不兼容或许可不明，直接排除",
}

SOURCES = {
    "mermaid": {"repo": "mermaid-js/mermaid", "stars": 91073,
                "license": "MIT", "gives": "37 种图类型目录 + flowchart 节点形状"},
    "vega-lite": {"repo": "vega/vega-lite", "stars": 5551,
                  "license": "BSD-3-Clause", "gives": "15 primitive + 3 composite mark"},
    "plotly": {"repo": "plotly/plotly.js", "stars": 18380,
               "license": "MIT", "gives": "trace 类型全集（科学图型交叉核对）"},
    "pptxgenjs": {"repo": "gitbrent/PptxGenJS", "stars": 6370,
                  "license": "MIT", "gives": "SHAPE_TYPE / CHART_TYPE 枚举"},
    "revealjs": {"repo": "hakimel/reveal.js", "stars": 72553,
                 "license": "MIT", "gives": "幻灯片布局与主题结构"},
    "colorbrewer": {"repo": "axismaps/colorbrewer", "stars": 1113,
                    "license": "Apache-2.0",
                    "gives": "export/colorbrewer.json 三族色板（纯数据，可整份搬）"},
    "csl": {"repo": "citation-style-language/styles", "stars": 3937,
            "license": "CC BY-SA 3.0",
            "gives": "GB/T 7714 2015/2025 numeric/author-date/note 共 9 个 CSL"},
    "noto-cjk": {"repo": "notofonts/noto-cjk", "stars": 4151,
                 "license": "OFL-1.1", "gives": "中文字体名与字形兜底（字库 8GB 不入库）"},
    "thesis-templates": {"repo": "mohuangrui/ucasthesis 等", "stars": 3924,
                         "license": "LPPL-1.3c",
                         "gives": "中文学位论文页边距/字号阶梯的数值事实"},
    "libsbgn": {"repo": "sbgn/libsbgn", "stars": 21,
                "license": "NOASSERTION", "gives": "SBGN 生物通路节点/边语义（借概念）"},
    "scico": {"repo": "thomasp85/scico", "stars": 484,
              "license": "NOASSERTION", "gives": "Crameri 科学配色（排除）"},
    "sylsta-scm": {"repo": "sylsta/scientific-colour-maps", "stars": 0,
                   "license": "GPL-2.0", "gives": "科学配色（排除）"},
    "scut-thesis": {"repo": "mengchaoheng/SCUT_thesis", "stars": 594,
                    "license": "GPL-3.0", "gives": "学位模板（排除）"},
}


def _p(name, batch, source, why):
    return {"name": name, "batch": batch, "source": source,
            "license": SOURCES[source]["license"], "status": "planned", "why": why}


CATALOGS = {
    "svgplot": {
        "module": "rongke.svgplot", "enum": "SUPPORTED_KINDS",
        "current": ["line", "time_series", "scatter", "bar", "barh",
                    "hist", "box", "heatmap", "matrix",
                    "area", "stacked_bar", "errorbar", "errorband", "dotplot"],
        "proposed": [
            _p("area", "P0", "vega-lite", "论文高频；line 已有，增量小"),
            _p("stacked_bar", "P0", "vega-lite", "分组/堆叠对照"),
            _p("errorbar", "P0", "vega-lite", "误差棒，composite mark 已有口径"),
            _p("errorband", "P0", "vega-lite", "置信带"),
            _p("dotplot", "P0", "plotly", "小样本分布"),
            _p("pie", "P1", "vega-lite", "arc mark"),
            _p("violin", "P1", "plotly", "分布对照"),
            _p("volcano", "P1", "plotly", "组学差异图"),
            _p("forest", "P1", "plotly", "meta 分析森林图"),
            _p("survival", "P1", "plotly", "Kaplan–Meier"),
            _p("sankey", "P2", "mermaid", "流量/归因，复用 svgdiagram 布局层更合适"),
            _p("radar", "P2", "mermaid", "多维比较"),
        ],
    },
    "svgdiagram": {
        "module": "rongke.svgdiagram", "enum": "NODE_KINDS + EDGE_KINDS",
        "current_nodes": ["process", "decision", "terminator", "entity",
                          "database", "circle", "hexagon", "subroutine",
                          "stadium", "cylinder"],
        "current_edges": ["arrow", "inhibit", "dashed", "phospho", "bind",
                          "cleave", "secrete", "degrade"],
        "proposed": [
            _p("database", "P0", "mermaid", "节点形状：数据库/数据集"),
            _p("circle", "P0", "mermaid", "节点形状：分子/实体"),
            _p("hexagon", "P0", "mermaid", "节点形状：预处理"),
            _p("subroutine", "P0", "mermaid", "节点形状：子流程"),
            _p("stadium", "P0", "mermaid", "节点形状：起止"),
            _p("cylinder", "P0", "mermaid", "节点形状：样本/存储"),
            _p("activate", "P0", "libsbgn", "边语义：激活（借概念，不搬代码）"),
            _p("catalyze", "P0", "libsbgn", "边语义：催化"),
            _p("consume", "P0", "libsbgn", "边语义：消耗"),
            _p("state", "P1", "mermaid", "图型：状态机"),
            _p("sequence", "P1", "mermaid", "图型：时序/实验步骤"),
            _p("sankey", "P1", "mermaid", "图型：流向"),
            _p("timeline", "P1", "mermaid", "图型：时间线"),
            _p("gantt", "P1", "mermaid", "图型：项目排期"),
            _p("swimlanes", "P1", "mermaid", "图型：跨主体流程"),
            _p("ishikawa", "P1", "mermaid", "图型：鱼骨/归因"),
            _p("radar", "P2", "mermaid", "图型：多维评估"),
            _p("venn", "P2", "mermaid", "图型：集合关系"),
            _p("treemap", "P2", "mermaid", "图型：层级占比"),
        ],
    },
    "typeset": {
        "module": "rongke.typeset", "enum": "STYLES + ROLES",
        "current_styles": ["journal-cn", "thesis", "preprint"],
        "current_roles": ["title", "authors", "affiliation", "abstract", "keywords",
                          "heading", "paragraph", "figure", "table", "caption",
                          "ref_entry", "note"],
        "proposed": [
            _p("refs:numeric", "P0", "csl", "GB/T 7714 数字制（现有，需显式成种类）"),
            _p("refs:author-date", "P0", "csl", "GB/T 7714 著者-出版年制"),
            _p("refs:note", "P0", "csl", "GB/T 7714 注释制"),
            _p("gb7714-2015", "P0", "csl", "2015 版规则（事实自实现）"),
            _p("gb7714-2025", "P0", "csl", "2025 版规则（事实自实现）"),
            _p("ref_type_ids", "P0", "csl", "J/N/M/C/P/D/S/R/EB/DS/CP/OL/Z 类型标识"),
            _p("journal-en", "P1", "thesis-templates", "英文双栏期刊体例数值"),
            _p("conference", "P1", "thesis-templates", "IEEE/ACM 会议体例数值"),
            _p("grant", "P1", "thesis-templates", "基金申报体例数值"),
            _p("equation", "P2", "thesis-templates", "块角色：公式"),
            _p("algorithm", "P2", "thesis-templates", "块角色：算法"),
            _p("appendix", "P2", "thesis-templates", "块角色：附录"),
        ],
    },
    "design": {
        "module": "rongke.design", "enum": "PAGE_KINDS",
        "current": ["cover", "section", "content", "two-col", "chart", "closing"],
        "proposed": [
            _p("timeline", "P0", "revealjs", "页型：时间线"),
            _p("comparison", "P0", "pptxgenjs", "页型：左右对照"),
            _p("statistic", "P0", "pptxgenjs", "页型：大数字 KPI"),
            _p("bibliography", "P0", "revealjs", "页型：参考文献"),
            _p("agenda", "P1", "revealjs", "页型：目录"),
            _p("process", "P1", "pptxgenjs", "页型：步骤条"),
            _p("matrix", "P1", "pptxgenjs", "页型：2×2 矩阵"),
            _p("quote", "P1", "revealjs", "页型：引文"),
        ],
    },
    "aideck": {
        "module": "aideck.canvas", "enum": "TYPES",
        "current": ["text", "bullets", "rect", "line", "chart", "table", "image"],
        "proposed": [
            _p("connector", "P0", "pptxgenjs", "图元：带箭头连线"),
            _p("group", "P0", "pptxgenjs", "图元：组合"),
            _p("callout", "P0", "pptxgenjs", "图元：标注气泡"),
            _p("icon", "P1", "pptxgenjs", "图元：图标"),
            _p("formula", "P1", "pptxgenjs", "图元：公式块"),
            _p("legend", "P1", "vega-lite", "图元：独立图例"),
            _p("freeform", "P2", "pptxgenjs", "图元：自由形状"),
        ],
    },
    "palette": {
        "module": "rongke.svgplot / typeset.DESIGN", "enum": "_PALETTE / DESIGN.colors",
        "current": ["绢青陈墨 6 色"],
        "proposed": [
            _p("brewer:sequential", "P0", "colorbrewer", "单色渐深（热图/密度）"),
            _p("brewer:diverging", "P0", "colorbrewer", "双极发散（差异/相关）"),
            _p("brewer:qualitative", "P0", "colorbrewer", "分类色（分组对照）"),
            _p("noto-serif-cjk", "P1", "noto-cjk", "中文衬线回退，补进 FONT_STACK"),
        ],
    },
}


#: 2026-11-11 第一批**真接进内核**的种类。不是「计划」，是代码枚举里读得到的。
#: 许可全绿（vega-lite BSD-3 / plotly MIT / mermaid MIT）。
#: ⚠️ `svgdiagram` proposed 里的 `activate/catalyze/consume` 来自 libsbgn
#:    （NOASSERTION = 红灯），**刻意不接**；CSL 的 refs 模式是 ShareAlike 黄灯，
#:    留到 P1「只取事实自实现」，本批也不碰。
DELIVERED_P0 = {
    "svgplot": ("area", "stacked_bar", "errorbar", "errorband", "dotplot"),
    "svgdiagram": ("database", "circle", "hexagon", "subroutine",
                   "stadium", "cylinder"),
}
for _tool, _names in DELIVERED_P0.items():
    for _item in CATALOGS[_tool]["proposed"]:
        if _item["name"] in _names:
            _item["status"] = "done"
            _item["delivered_in"] = "P0-2026-11-11"


def grade_of(license_name: str) -> str:
    """许可分级。认不出的按 red 处理——**不猜、不放行**。"""
    return LICENSE_GRADE.get(license_name, "red")


def check_license(source_key: str) -> dict:
    """某个来源能不能进框架/发行件。许可不明一律 red。"""
    s = SOURCES.get(source_key)
    if s is None:
        return {"ok": False, "grade": "red", "reason": "来源不在 SOURCES 里"}
    g = grade_of(s["license"])
    return {"ok": g == "green", "grade": g, "license": s["license"],
            "repo": s["repo"], "rule": GRADE_RULE[g],
            "may_vendor": g == "green" and s["license"] == "Apache-2.0"}


def list_kinds(tool: str, batch: str | None = None,
               grade: str | None = None,
               status: str | None = None) -> list[dict]:
    """列某工具的建议种类；按批次/许可等级/交付状态过滤。"""
    c = CATALOGS.get(tool)
    if c is None:
        return []
    out = []
    for item in c["proposed"]:
        if batch and item["batch"] != batch:
            continue
        if grade and grade_of(item["license"]) != grade:
            continue
        if status and item["status"] != status:
            continue
        out.append(dict(item, grade=grade_of(item["license"])))
    return out


def verify_current(tool: str, actual, field: str | None = None) -> dict:
    """★ 拿真实枚举读回比目录，防止目录与代码静默漂移。

    支持两类工具：
      * 单枚举工具（`svgplot` / `design` / `aideck` / `palette`）：
        `verify_current("svgplot", svgplot.SUPPORTED_KINDS)`
      * 分项枚举工具（`svgdiagram` 的 `current_nodes`/`current_edges`，
        `typeset` 的 `current_styles`/`current_roles`）：
        可传单项 `verify_current("svgdiagram", svgdiagram.NODE_KINDS, field="nodes")`
        或传字典 `verify_current("svgdiagram", {"nodes": ..., "edges": ...})`。
    """
    c = CATALOGS.get(tool)
    if c is None:
        return {"ok": False, "error": f"未知工具 {tool!r}"}
    split_keys = [k for k in c if k.startswith("current_")]

    if field is not None:
        key = field if field in c else f"current_{field}"
        if key not in c:
            avail = ", ".join(split_keys or (["current"] if "current" in c else []))
            return {"ok": False, "tool": tool,
                    "error": f"{tool} 没有枚举字段 {field!r}（可用：{avail}）"}
        got = sorted(str(x) for x in actual)
        want = sorted(str(x) for x in c[key])
        return {"ok": got == want, "tool": tool, "field": key,
                "expected": want, "actual": got,
                "drift": sorted(set(got) ^ set(want))}

    expected = c.get("current")
    if expected is None:
        if not split_keys:
            return {"ok": False, "tool": tool,
                    "error": f"{tool} 未声明 current 枚举"}
        if not isinstance(actual, dict):
            return {"ok": False, "tool": tool,
                    "error": f"{tool} 用分项字段（{', '.join(split_keys)}），请传 field=... 或按字典传入 actual"}
        fields_rep: dict[str, dict] = {}
        all_drift: list[str] = []
        missing_fields: list[str] = []
        for key in split_keys:
            short = key[len("current_"):]
            if key in actual:
                raw_vals = actual[key]
            elif short in actual:
                raw_vals = actual[short]
            else:
                missing_fields.append(short)
                want = sorted(str(x) for x in c[key])
                fields_rep[short] = {"ok": False, "field": key,
                                     "expected": want, "actual": [],
                                     "drift": want, "missing_input": True}
                all_drift.extend(f"{short}:{x}" for x in want)
                continue
            got = sorted(str(x) for x in raw_vals)
            want = sorted(str(x) for x in c[key])
            drift = sorted(set(got) ^ set(want))
            fields_rep[short] = {"ok": got == want, "field": key,
                                 "expected": want, "actual": got, "drift": drift}
            all_drift.extend(f"{short}:{x}" for x in drift)
        all_ok = (not missing_fields) and all(f["ok"] for f in fields_rep.values())
        rep = {"ok": all_ok, "tool": tool, "fields": fields_rep,
               "drift": sorted(all_drift)}
        if missing_fields:
            rep["missing_fields"] = missing_fields
        return rep

    got = sorted(str(x) for x in actual)
    want = sorted(str(x) for x in expected)
    return {"ok": got == want, "tool": tool, "expected": want,
            "actual": got,
            "drift": sorted(set(got) ^ set(want))}


def verify_delivered(tool: str, actual) -> dict:
    """★ 目录标 `done` 的种类必须在**真实枚举**里读得到。

    防的是「目录宣称已接入、代码里其实没有」—— 与工具注册表那条
    「注册≠生效≠用上」同型。短路掉一个真实种类就应当场报红。
    """
    c = CATALOGS.get(tool)
    if c is None:
        return {"ok": False, "error": f"未知工具 {tool!r}"}
    claimed = sorted(i["name"] for i in c["proposed"] if i["status"] == "done")
    got = {str(x) for x in actual}
    missing = [n for n in claimed if n not in got]
    return {"ok": not missing, "tool": tool, "claimed": claimed,
            "missing": missing}


def delivery_license_ok() -> dict:
    """★ 已交付种类必须全绿：不许把 ShareAlike / GPL / NOASSERTION 接进内核。"""
    done = [i for c in CATALOGS.values() for i in c["proposed"]
            if i["status"] == "done"]
    # 实时查 SOURCES，不读条目自己的快照字段：抄错一次照样拦得住。
    violations = [i for i in done
                  if grade_of(SOURCES[i["source"]]["license"]) != "green"]
    return {"ok": not violations, "n_done": len(done),
            "violations": [{"name": i["name"], "source": i["source"],
                            "license": SOURCES[i["source"]]["license"]}
                           for i in violations]}


def summary() -> dict:
    """目录总览：多少个工具、多少建议种类、许可分级分布。"""
    props = [i for c in CATALOGS.values() for i in c["proposed"]]
    grades = {"green": 0, "yellow": 0, "red": 0}
    batches = {"P0": 0, "P1": 0, "P2": 0}
    for i in props:
        grades[grade_of(i["license"])] += 1
        batches[i["batch"]] += 1
    return {"readback_date": READBACK_DATE, "tools": len(CATALOGS),
            "sources": len(SOURCES), "proposed": len(props),
            "delivered": sum(1 for i in props if i["status"] == "done"),
            "grades": grades, "batches": batches}
