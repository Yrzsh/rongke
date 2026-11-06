# -*- coding: utf-8 -*-
"""数据分析成图 —— 给一张表，画一张能被审计的图。

## 这个模块解决的是哪一段

荣珂过去能**审**图（`figure.py` 的四道门），也能**画**图（模型自己写脚本），
但中间那段是断的：拿到一份 CSV，没有人替它决定"这一列该画成什么"，
也没有人在画之前先把数据的形状摸清楚。结果是模型凭手感选图型 ——
两组的连续变量画成折线、时间序列画成箱线、配对数据画成分组柱 ——
**程序全都退出 0，图是错的**，四个门里只有图注门可能碰巧拦到。

所以本模块补三件事：
  1. **读表**（CSV/TSV/Excel/JSON）并**测形状**：每列的类型、缺失、基数；
  2. **按形状选图型**，并把"为什么选它、为什么不选那几个"写成**可读的理由**；
  3. **生成一个自包含的绘图脚本**并跑它，产物直接交给 `figure.FigureAuditor`。

## 三条纪律

1. **选图型有判据，不是凭手感。** `recommend()` 只看**测量出的形状**
   （几列、连续/分类、有没有重复单位、是不是配对、有没有时间轴），
   每条建议都带 `why` 与 `rejected`。模型可以否决，但必须先看到理由 ——
   这和 `figure.Panel.role`（职责而非画法）是同一条思路。

2. **生成的脚本必须过源码预检。** `validate_figure.py` 会扫生成的源码，
   其中三条最容易踩：
   · `FONT-FAMILY` 硬失败 —— 必须显式写 Arial/Helvetica/sans-serif；
   · `DATA-EXCLUSION` 警告 —— 只要有 `.dropna(`，就必须同时写 `n_before/n_after`；
   · `FINAL-WIDTH` 警告 —— 宽度要落在 89mm（单栏）或 183mm（双栏）附近。
   ⇒ 所以本模块生成的脚本一律：显式设字体、把缺失行**计数后再丢**、
     宽度按期刊栏宽写死。这不是为了骗过审计，是因为那三条本来就是对的。

3. **不做隐式丢数据。** 缺失、非数值、负值取对数 —— 每一处分流都记进
   `Exclusion`（前后行数 + 规则 + 理由），随报告一起回给模型。
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: 单栏 / 双栏的标准宽度（mm）。取自 `validate_figure.check_dimensions`
#: 的容差中心（89 ±4 / 183 ±6）—— 与审计脚本同一口径，不另立数字。
COL_WIDTH_MM = 89.0
FULL_WIDTH_MM = 183.0

#: 出图用的调色板（**模块级真常量**，不是模板里的一段文本）。
#:
#: ★★ 2026-10-26 修一次静默回退：这份 Okabe-Ito 六色**原先只写在
#:   `_PLOT_HEAD` 那个字符串模板里面**（生成脚本时才被拼进去），
#:   模块级根本没有 `PALETTE` 这个名字。后果是 `figure._quality_block()`
#:   里那句
#:       from .analysis import PALETTE as _DEFAULT
#:   **一直抛 ImportError**，而它被 `except Exception:` 吞掉，退回 `pal = []`，
#:   于是整条调色板质检在真实审计里**一次都没跑过**（quality 块照样有键、
#:   照样"看起来接通了"）。
#:   这和 `to_dict()` 丢 `warnings` 是同一族缺陷的两个面：
#:   一个返回端丢数据，一个取数端永远取不到 —— 两种都让"键存在"的检查变绿。
#:
#:   现在模块级持有真常量，模板通过 `{palette!r}` 引用同一份，
#:   不再有两处手抄。
PALETTE = ["#0072B2", "#009E73", "#D55E00", "#CC79A7", "#E69F00", "#56B4E9"]

#: 缺这几列就没法画。与 `figure._NEEDED_MODULES` 同一条纪律：
#: 探测的是**下游解释器**，荣珂自己装了不代表画图那个装了。
NEEDED_MODULES = ("matplotlib", "pandas")

#: 主程序默认用的字号阶梯（pt）。最小值 7 远高于审计的 5 pt 下限 ——
#: 那道门是**最后一道保险**，不该被当成设计目标贴着写。
FONT_SIZES = {"title": 9, "label": 8, "tick": 7, "legend": 7, "annot": 7}

#: 图型登记表。`family` 用来做"同一族只留一条建议"的去重：
#: 一张表不该同时建议画箱线和画小提琴（那是同一个论据画两遍，
#: 与 `figure.Panel.role` 的"职责不许重复"是同一条）。
CHART_KINDS: dict[str, dict[str, str]] = {
    "time_series":   {"family": "trend",  "name": "折线（时间序列）",
                      "needs": "1 个时间列 + ≥1 个连续列"},
    "line":          {"family": "trend",  "name": "折线",
                      "needs": "1 个有序/连续 x + ≥1 个连续 y"},
    "scatter":       {"family": "relate", "name": "散点（可加拟合线）",
                      "needs": "2 个连续列"},
    "box":           {"family": "dist",   "name": "箱线（分组分布）",
                      "needs": "1 个分类列 + 1 个连续列"},
    "violin":        {"family": "dist",   "name": "小提琴（分组分布）",
                      "needs": "1 个分类列 + 1 个连续列，每组样本够"},
    "bar":           {"family": "compare", "name": "柱状（分组汇总）",
                      "needs": "1 个分类列 + 1 个连续列（取均值/中位数）"},
    "paired":        {"family": "paired", "name": "配对连线",
                      "needs": "同一测量单位的两个条件"},
    "heatmap":       {"family": "matrix", "name": "热图（相关/交叉表）",
                      "needs": "≥2 个连续列，或 2 个分类列做交叉"},
    "hist":          {"family": "dist",   "name": "直方图（单变量分布）",
                      "needs": "1 个连续列，无分组"},
    # ── 2026-10-26 增补：真实论文里高频、而荣珂原先一种都画不出的图型 ──
    #   依据不是"猜它们有用"，是机器扫了 nature-figure 自带的 25 个
    #   真实论文绘图脚本（assets/figures4papers），数出来的调用次数：
    #     bar 37 · plot 36 · scatter 11 · **fill_between 10** · **imshow 7**
    #     · **barh 3** · quiver 3 · plot_surface 2 · contour 2
    #   ⇒ 置信带（fill_between）、矩形热图（imshow）、横向柱（barh）
    #     这三类在原 9 种里**一个都没有**，属于真空。
    #   误差棒与森林图同理：它们不是新语法，但"要不要画误差棒"是
    #   一个**论据层面的决定**（有没有重复测量、置信区间怎么来），
    #   不该留给模型每次自己即兴。
    "band":          {"family": "trend",  "name": "折线 + 置信带",
                      "needs": "1 个有序 x + 1 个连续 y + 1 个误差/区间列（或同 x 多次测量）"},
    "errorbar":      {"family": "compare", "name": "误差棒（分组均值 ± 误差）",
                      "needs": "1 个分类列 + 1 个连续列（每组 ≥2 个观测才能算误差）"},
    "barh":          {"family": "compare", "name": "横向柱（长标签排行）",
                      "needs": "1 个分类列 + 1 个连续列（标签较长时优于竖柱）"},
    "stacked_area":  {"family": "trend",  "name": "堆叠面积（构成随时间变化）",
                      "needs": "1 个时间/有序列 + ≥2 个连续列（各成分）"},
    "forest":        {"family": "compare", "name": "森林图（效应量 + 区间）",
                      "needs": "1 个分类列 + 效应量列 + 下界列 + 上界列"},
    "matrix":        {"family": "matrix", "name": "矩形热图（原值矩阵，非相关）",
                      "needs": "2 个分类/离散列 + 1 个数值列（长表透视）"},
}

#: 「偏好优先」：同一族里更常见的排前面。它们不是"必须"，只是先建议。
_FAMILY_PREF = {"dist": ("box", "hist", "violin")}


# ════════════════════════════════════════════════════════════════
# 一、读表与测形状
# ════════════════════════════════════════════════════════════════

class TableError(ValueError):
    """表读不进来 / 读进来了但画不了。消息要直接给人看。"""


def _sniff_delimiter(head: str) -> str:
    """猜分隔符。逗号/制表/分号/竖线各试一遍，取第一行切得最多的。

    ★ 不用 `csv.Sniffer`：它对**单列**表会抛 `_csv.Error: Could not determine
      delimiter`，而单列表是完全合法的（一列浓度值，画直方图）。
      自己数一遍，`>` 而不是 `>=`，让默认值在并列时落在逗号上。
    """
    first = ""
    for ln in head.splitlines():
        if ln.strip():
            first = ln
            break
    best, best_n = ",", 0
    for d in (",", "\t", ";", "|"):
        n = first.count(d)
        if n > best_n:
            best, best_n = d, n
    return best


def _decode(raw: bytes) -> tuple[str, str]:
    """按序试几种编码。返回 `(文本, 用的编码)`。

    ★ 中文表的现实：Excel 另存的 CSV 常是 `gb18030`，而 utf-8 先试会把
      GBK 字节解成乱码**而不报错**（GBK 的双字节几乎不产生非法 utf-8 序列的概率
      没那么高，但会解出替换字符）。所以顺序是 utf-8-sig → utf-8 → gb18030，
      且**解出替换字符就换下一个**。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            txt = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if enc != "latin-1" and "\ufffd" in txt:
            continue
        return txt, enc
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _to_number(v: Any) -> float | None:
    """能不能当数字用。空/坏值返回 None（**不抛、不当 0**）。"""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if (isinstance(v, float) and math.isnan(v)) else float(v)
    s = str(v).strip().replace(",", "") if isinstance(v, str) else str(v)
    if not s or s.lower() in {"na", "nan", "null", "none", "-", "--", ""}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class Column:
    """一列的**测量结果** —— 选图型的全部依据都在这里。"""
    name: str
    kind: str = "text"        # numeric / category / time / text
    n_total: int = 0
    n_missing: int = 0
    n_unique: int = 0
    numeric_ratio: float = 0.0   # 能解析成数字的比例（只在 numeric 时有意义）
    sample: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    mean: float | None = None

    @property
    def is_numeric(self) -> bool:
        return self.kind == "numeric"

    @property
    def is_category(self) -> bool:
        return self.kind == "category"

    @property
    def is_time(self) -> bool:
        return self.kind == "time"

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "n_total": self.n_total,
                "n_missing": self.n_missing, "n_unique": self.n_unique,
                "numeric_ratio": round(self.numeric_ratio, 3),
                "min": self.min, "max": self.max,
                "mean": None if self.mean is None else round(self.mean, 6),
                "sample": self.sample[:5]}


_TIME_PAT = re.compile(
    r"^\s*("
    r"\d{4}[-/]\d{1,2}([-/]\d{1,2})?([ T]\d{1,2}:\d{2}(:\d{2})?)?"   # 2024-01 / 2024/01/02 10:00
    r"|\d{1,2}:\d{2}(:\d{2})?"                                       # 10:30
    r"|\d{4}年\d{1,2}月(\d{1,2}日)?"
    r"|\d{4}"
    r")\s*$")

#: 列名里出现这些词就倾向判时间轴（**只是倾向**，还要看值）。
_TIME_HINT = ("date", "time", "day", "week", "month", "year", "日期", "时间",
              "天", "周", "月", "年", "批次序")


def profile_table(rows: list[dict], *, max_unique_for_category: int = 20) -> list[Column]:
    """测每一列的形状。

    `kind` 的判定顺序很重要：**先时间、再数值、再分类、最后文本**。
    反过来会把 `2024` 判成数值列（它确实能转数字），于是一个"年份"轴被画成
    连续 x 轴，刻度变成 2024.0 这种笑话。
    """
    if not rows:
        return []
    names: list[str] = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)
    out: list[Column] = []
    for nm in names:
        vals = [r.get(nm) for r in rows]
        strs = ["" if v is None else str(v).strip() for v in vals]
        n_total = len(vals)
        missing = sum(1 for s in strs if s == "" or s.lower() in
                      {"na", "nan", "null", "none", "-", "--"})
        present = [s for s in strs if s and s.lower() not in
                   {"na", "nan", "null", "none", "-", "--"}]
        uniq = len(set(present))
        nums = [_to_number(s) for s in present]
        n_ok = sum(1 for x in nums if x is not None)
        ratio = (n_ok / len(present)) if present else 0.0
        timeish = bool(present) and all(_TIME_PAT.match(s) for s in present[:200])
        name_lc = nm.lower()
        if name_lc in _TIME_HINT or any(h in name_lc for h in _TIME_HINT):
            # 名字像时间**且**值也像时间（或值全是 4 位年份）才算。
            if timeish or (ratio == 1.0 and present and
                           all(re.fullmatch(r"\d{4}", s) for s in present[:200])):
                timeish = True
        c = Column(name=nm, n_total=n_total, n_missing=missing,
                   n_unique=uniq, numeric_ratio=ratio, sample=present[:5])
        if timeish:
            c.kind = "time"
        elif present and ratio >= 0.9:
            c.kind = "numeric"
            good = [x for x in nums if x is not None]
            if good:
                c.min, c.max = round(min(good), 6), round(max(good), 6)
                c.mean = sum(good) / len(good)
        elif uniq <= max_unique_for_category:
            c.kind = "category"
        else:
            c.kind = "text"
        out.append(c)
    return out


def read_table(path: str | Path) -> tuple[list[dict], dict]:
    """读 CSV/TSV/Excel/JSON 成 `[{列名: 值}]`。返回 `(行, 元信息)`。

    元信息里记 `encoding` / `delimiter` / `n_rows` / `truncated` ——
    **截断必须说出来**（`truncated=True` 时行数是"取了多少"不是"表有多少"）。
    """
    p = Path(path)
    if not p.exists():
        raise TableError(f"文件不存在：{p}")
    suf = p.suffix.lower()
    raw = p.read_bytes()
    if not raw:
        raise TableError(f"文件是空的：{p.name}")
    meta: dict[str, Any] = {"path": str(p), "format": suf.lstrip(".") or "csv",
                            "truncated": False, "encoding": "", "delimiter": ""}
    rows: list[dict] = []
    if suf in (".xlsx", ".xlsm", ".xls"):
        try:
            import pandas as pd
        except Exception as e:                                  # noqa: BLE001
            raise TableError(f"读 Excel 要 pandas + openpyxl，当前解释器没有：{e}")
        try:
            df = pd.read_excel(io.BytesIO(raw))
        except Exception as e:                                  # noqa: BLE001
            raise TableError(f"Excel 读不进来（{type(e).__name__}: {e}）。"
                             "如果是 .xls 老格式，先在 Excel 里另存为 .xlsx。")
        meta["encoding"] = "excel"
        rows = df.to_dict(orient="records")
    elif suf == ".json":
        txt, enc = _decode(raw)
        meta["encoding"] = enc
        try:
            data = json.loads(txt)
        except Exception as e:                                  # noqa: BLE001
            raise TableError(f"JSON 解析失败（{e}）")
        if isinstance(data, dict):
            # {"data": [...]} / {"rows": [...]} 这类包一层的写法
            for k in ("rows", "data", "records", "items", "result"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        if not isinstance(data, list):
            raise TableError("JSON 不是「对象数组」形状 —— 需要 [{\"列\": 值, ...}, ...]")
        rows = [r for r in data if isinstance(r, dict)]
    else:
        txt, enc = _decode(raw)
        meta["encoding"] = enc
        delim = _sniff_delimiter(txt[:8192])
        meta["delimiter"] = {"\t": "\\t"}.get(delim, delim)
        rd = csv.DictReader(io.StringIO(txt), delimiter=delim)
        rows = [dict(r) for r in rd]

    rows = [r for r in rows if any(str(v or "").strip() for v in r.values())]
    if not rows:
        raise TableError(f"读出来 0 行（{p.name}）—— 表头之外没有数据。")
    meta["n_rows"] = len(rows)
    meta["columns"] = list(rows[0].keys())
    return rows, meta


# ════════════════════════════════════════════════════════════════
# 二、按形状选图型
# ════════════════════════════════════════════════════════════════

@dataclass
class Suggestion:
    """一条图型建议。`why` 与 `rejected` 是**给模型看的**，不是装饰。"""
    kind: str
    x: str = ""
    y: str = ""
    group: str = ""
    why: str = ""
    rejected: list[str] = field(default_factory=list)
    confidence: str = "medium"      # high / medium / low

    @property
    def label(self) -> str:
        return CHART_KINDS.get(self.kind, {}).get("name", self.kind)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "x": self.x, "y": self.y,
                "group": self.group, "why": self.why,
                "rejected": list(self.rejected), "confidence": self.confidence}


def recommend(cols: list[Column], *, max_suggestions: int = 3) -> list[Suggestion]:
    """按测量出的形状给建议。**每条都写清"为什么"和"为什么不是别的"。**

    判据顺序（先强后弱）：时间轴 → 两连续 → 分组分布 → 单变量 → 矩阵。
    同族去重：`dist` 族只留最贴切的那一条。
    """
    nums = [c for c in cols if c.is_numeric]
    cats = [c for c in cols if c.is_category]
    times = [c for c in cols if c.is_time]
    out: list[Suggestion] = []
    used_family: set[str] = set()

    def _add(s: Suggestion) -> None:
        fam = CHART_KINDS[s.kind]["family"]
        if fam in used_family and len(out) >= 2:
            return
        used_family.add(fam)
        out.append(s)

    # ① 有时间列 + 连续列 ⇒ 时间序列。这是**最强**的一条判据：
    #    时间列如果被当成 category 去画柱状，趋势会被打散成一堆岛。
    if times and nums:
        t, y = times[0], nums[0]
        _add(Suggestion(
            kind="time_series", x=t.name, y=y.name, confidence="high",
            why=f"「{t.name}」是时间列、「{y.name}」是连续列 —— "
                f"时间是有序的，折线能保住顺序与间隔，柱状会把它打散成一堆岛。",
            rejected=[f"box/violin：{t.name} 是连续时间而不是离散组，"
                      f"按它分组等于把一条趋势砍成 {t.n_unique} 段。"]))

    # ② 两个连续列 ⇒ 散点。相关系数不给，那是分析不是画图（见 why）。
    if len(nums) >= 2:
        a, b = nums[0], nums[1]
        label = "scatter" if (times and nums) else "scatter"
        if not (times and nums and a.name == (nums[0].name)):
            pass
        _add(Suggestion(
            kind=label, x=a.name, y=b.name,
            confidence="high" if len(nums) == 2 else "medium",
            why=f"「{a.name}」与「{b.name}」都是连续列 —— "
                f"散点同时保留两个变量的分布形状，柱状会把它们压成两个均值。",
            rejected=["box：连续 x 分组要靠人为切箱，切法一换结论就换。",
                      "bar：把 {0} 个点压成一根柱，丢掉了全部离散信息。".format(a.n_total)]))

    # ③ 分类 + 连续 ⇒ 分布对比。箱线优先，样本太少才换柱状。
    if cats and nums:
        g, v = cats[0], nums[0]
        per = _group_sizes(g, v)
        small = per and min(per.values()) < 8
        if small:
            _add(Suggestion(
                kind="bar", x=g.name, y=v.name, group=g.name, confidence="high",
                why=f"「{g.name}」每组样本不足 8（最少 {min(per.values())}）—— "
                    f"小样本画箱线的四分位没有意义，改用柱状 + 个体点。",
                rejected=["box：每组 <8 时四分位由 1~2 个点撑起来，看起来像结论其实是噪声。"]))
        else:
            _add(Suggestion(
                kind="box", x=g.name, y=v.name, group=g.name, confidence="high",
                why=f"「{g.name}」是分类列、「{v.name}」是连续列 —— "
                    f"分组分布用箱线能同时给出中位与离散，比只画均值柱诚实。",
                rejected=["bar：只画均值会把「组内差异有多大」整个抹掉。"]))

    # ④ 只有连续列（无分组）⇒ 直方图。
    if nums and not cats and not times:
        v = nums[0]
        _add(Suggestion(
            kind="hist", x=v.name, y="count", confidence="medium",
            why=f"只有一个连续列「{v.name}」且没有分组变量 —— "
                f"先看它自己的分布形状（偏态/双峰）再决定下一步。",
            rejected=["box：单个变量画一个箱子，看不到形状。"]))

    # ⑤ 两个分类列 ⇒ 交叉表热图。
    if len(cats) >= 2:
        a, b = cats[0], cats[1]
        _add(Suggestion(
            kind="heatmap", x=a.name, y=b.name, group="",
            confidence="medium",
            why=f"「{a.name}」与「{b.name}」都是分类列 —— "
                f"交叉表画热图能一眼看出哪个格子的计数异常。",
            rejected=["bar：两个分类变量画分组柱，柱子会多到看不清。"]))

    # ⑥ 一个连续的兜底：数值列足够多时给相关矩阵。
    if len(nums) >= 3 and not out:
        _add(Suggestion(kind="heatmap", x=nums[0].name, y=nums[1].name,
                        confidence="low",
                        why=f"有 {len(nums)} 个连续列 —— 相关矩阵能先指出哪两列值得细看。",
                        rejected=[]))

    # ⑦ 插件图型：按它们自己声明的适用条件插进来。
    # ★ 没有这一步，插件图型就是**死登记** —— 它能注册、能画、能过审计，
    #   但 recommend() 的判据全是内置的硬编码分支，永远不会建议它，
    #   于是用户永远看不到它。这正是"扩展了但等于没扩展"的形态。
    #   放在最后：内置图型的判据是经过验证的，插件不该抢占它们的位子；
    #   插件只在"内置没给出更贴切建议"的地方补位（由 `_add` 的同族去重
    #   与 confidence 排序自然处理）。
    for s in _plugin_suggestions(cols):
        if s.kind not in {x.kind for x in out}:
            _add(s)

    return out[:max_suggestions]


#: 插件图型的适用条件表：`{kind: callable(cols) -> Suggestion | None}`。
#: 由 `plugins.load_plugins()` 在加载时填。用**表**而不是 import 回 plugins
#: 模块，是为了避免 analysis ↔ plugins 的循环依赖（plugins 要 import analysis
#: 才能写 CHART_KINDS）。
PLUGIN_SCOPERS: dict[str, "Callable[[list[Column]], Suggestion | None]"] = {}


def _plugin_suggestions(cols: list[Column]) -> list[Suggestion]:
    """跑一遍插件声明的适用条件，收集它们给出的建议。

    任何一个插件的 `applies` 抛错都**不能**拖垮内置建议 —— 那些是核心能力。
    抛出时打一行到 stderr 说明是哪个插件，然后跳过它。
    """
    out: list[Suggestion] = []
    for kind, fn in list(PLUGIN_SCOPERS.items()):
        if kind not in CHART_KINDS:
            continue
        try:
            s = fn(cols)
        except Exception as e:                                  # noqa: BLE001
            import sys as _sys
            print(f"[analysis] 插件图型 {kind} 的适用条件抛错，已跳过："
                  f"{type(e).__name__}: {e}", file=_sys.stderr)
            continue
        if s is None:
            continue
        # 插件不许把 kind 写成别的（那会让建议指向一个它画不出的图型）
        if isinstance(s, Suggestion) and s.kind != kind:
            s.kind = kind
        if isinstance(s, Suggestion):
            out.append(s)
    return out


def suggest(kind: str, *, x: str = "", y: str = "", group: str = "",
            why: str = "", rejected: list[str] | None = None,
            confidence: str = "medium") -> Suggestion:
    """给**插件作者**用的建议构造器。

    为什么不让插件直接 `from rongke.analysis import Suggestion`：
    `Suggestion` 的字段会随内核演进变（比如将来加 `weight`），
    插件硬绑字段名就会跟着碎。走一个工厂函数，签名可以兼容性地加参数。

    也顺便做了两件事：
      · 把 `kind` 覆盖成插件正在声明的那个（防止建议指向它画不出的图型）
      · 缺 `why` 时给一句能看的兜底 —— 建议不带理由等于让模型瞎选

    用法（在 `chart_kinds()` 里）：
        "applies": lambda cols: A.suggest(
            "term_bar", x=cols[0].name, y=cols[1].name,
            why="两列是一对「标签 + 计数」——横向条形最适合读名次。",
            rejected=["bar：竖着放的话，长标签会挤成一团。"])
    """
    if not str(why or "").strip():
        why = f"数据形状符合「{CHART_KINDS.get(kind, {}).get('name', kind)}」的适用条件。"
    return Suggestion(kind=str(kind), x=str(x or ""), y=str(y or ""),
                      group=str(group or ""), why=str(why),
                      rejected=list(rejected or []),
                      confidence=str(confidence or "medium"))


def _group_sizes(g: Column, v: Column) -> dict[str, int]:
    """粗略估每组有多少行。**只用列摘要**，不回表 —— 回表由 `plan()` 带着原始行做。"""
    if not g.n_unique or not v.n_total:
        return {}
    avg = max(1, v.n_total // max(1, g.n_unique))
    return {"*": avg}


# ════════════════════════════════════════════════════════════════
# 三、生成绘图脚本
# ════════════════════════════════════════════════════════════════

_PLOT_HEAD = '''# -*- coding: utf-8 -*-
"""由荣珂 · 数据分析成图 生成的绘图脚本（可直接改、可重跑）。

为什么每个字都写死在这里而不是靠外部配置：
  · 字体必须**显式**声明 —— validate_figure 的 FONT-FAMILY 是硬失败；
  · 丢缺失行必须**先计数** —— DATA-EXCLUSION 会检查有没有 n_before/n_after；
  · 宽度必须落在 89 / 183 mm —— FINAL-WIDTH 检查栏宽；
  · 字号最小 7 pt，远高于 5 pt 下限（那道门是保险，不是设计目标）。
"""
import io                                  # load() 里要用 StringIO 先嗅分隔符
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                      # 无显示环境也要能出图
import matplotlib.pyplot as plt            # noqa: E402
import pandas as pd                        # noqa: E402

plt.rcParams.update({
    "font.family": "sans-serif",
    # ★ 2026-10-26：字体链按**实测**重排。两件事同时成立才算对：
    #   ① 中文要有字形（不然图上出豆腐块）；
    #   ② 西文字宽不能变（不然同样的字占更多横向空间，碰撞门会红一片）。
    #
    #   第一轮我只想到①，把 Microsoft YaHei 提到首位 —— 中文是不缺字了，
    #   但它的西文字面比 Arial 宽 **10.5%**，89mm 单栏里直接压出碰撞：
    #   analysis 套件 8 项失败，全部是 `text-text overlap`。
    #   修一个显示缺陷换来一堆版面缺陷，等于把问题搬家。
    #
    #   实测对照（同一句话、同字号，标题宽度 / 缺字总数）：
    #     Arial              西文 1.000x   缺 61 处（中文完全没字形）
    #     Microsoft YaHei    西文 1.105x   缺 12 处
    #     SimHei / SimSun    西文 1.08x    缺 12~13 处
    #     **DengXian（等线）  西文 1.021x   缺 16 处**  ← 宽度最接近 Arial
    #   逐字符集看（常用汉字/易错汉字/全角标点）：DengXian **零缺字**，
    #   而 Arial 分别是 23 / 12 / 11 处 —— 中文场景下 Arial 根本不可用。
    #   ⇒ 选 DengXian：它是 Windows 10 起自带的等线体，中文全覆盖、
    #     西文宽度只差 2%。
    #
    #   仍会缺的那几类（单位符号 U+207B、上下标、部分数学符号）**不是换字体
    #   能解决的** —— 任何中文字体都在同一批码位上缺字，Arial 缺得更多。
    #   那些字符的正确走法是 mathtext / LaTeX（见下面的 mathtext 设置），
    #   而不是指望正文字体自带。
    "font.sans-serif": ["DengXian", "Microsoft YaHei", "SimHei", "SimSun",
                        "Noto Sans CJK SC", "Source Han Sans SC",
                        "Arial", "Helvetica", "DejaVu Sans", "sans-serif"],
    # 数学文本里的负号：U+2212 在不少字体里没有，关掉用 ASCII '-' 更稳
    "axes.unicode_minus": False,
    "pdf.fonttype": 42,                    # 42 = TrueType，文字在 PDF 里仍可编辑
    "ps.fonttype": 42,
    "svg.fonttype": "none",                # 文字存成 <text> 而不是路径，SVG 才可编辑
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.linewidth": 0.5,
    "xtick.major.width": 0.5,
    "ytick.major.width": 0.5,
    "font.size": 7,
    "axes.labelsize": 8,
    "axes.titlesize": 9,
    "legend.fontsize": 7,
    "xtick.labelsize": 7,
    "ytick.labelsize": 7,
    "figure.dpi": 150,
    "savefig.dpi": 600,
    "savefig.bbox": "tight",
})

# 调色板：**Okabe-Ito 六色**（色盲友好领域的公认标准件）。
#
# ★ 2026-10-26 换掉旧调色板。旧的是
#   ["#0F4D92","#E8A33D","#3F8F6B","#A63A50","#6B5B95","#B9C4CF"]，
#   用 OKLab + CAM02-UCS 实测（判据与 figure-gate 的 check_palette.py 同一口径）：
#     · 亮度过带：  #0F4D92 L=0.4237（带下限 0.43）、#B9C4CF L=0.8152（带上限 0.77）
#     · 彩度太灰：  #3F8F6B C=0.0970、#6B5B95 C=0.0914、#B9C4CF C=0.0197（下限 0.10）
#     · **红绿色盲下分不开**：#3F8F6B（绿）vs #A63A50（红）deutan dE=9.7，门槛 10.5。
#       这是最要命的一条 —— 色盲读者看这两条线是同一个颜色。
#
# ★ 为什么不自己调一个"看起来更好"的：
#   调色板不是审美题，是**约束求解**。上表那些数字就是约束。
#   Okabe-Ito 是 Nature/NEJM 等刊物实际在用的标准件，且实测全过：
#     all-pairs CVD 最差 dE=12.8（>10.5）· 正常视觉最差 dE=21.6（>21.0）
#     六色亮度全在 0.53~0.75、彩度全在 0.117~0.171。
#   ⇒ 选它不是为了好看，是因为它**唯一同时满足可解释性（学界认得出）与全部判据**。
#
# ★ all-pairs 不是 adjacent：分类调色板里任何两色都可能同时出现在图上，
#   只比相邻色会放行"三个蓝"那种组合 —— 我第一版就这么错过一次。
#
# ★ 这一行是**从模块级常量拼进来的**（{palette!r}），不是第二次手抄。
#   原先这里写死一份字面量，模块级没有同名常量，导致 figure.py 的
#   调色板质检永远 import 失败（详见模块顶部 PALETTE 的注释）。
PALETTE = {palette!r}

WIDTH_MM = {width_mm!r}
HEIGHT_MM = {height_mm!r}


def _sniff_sep(sample):
    """自己猜分隔符：取第一个非空行里出现次数**最多**的那个。

    ★ 2026-10-26 新增，起因是一个静默毁列名的既有缺陷。
      旧代码用 `pd.read_csv(..., sep=None, engine="python")`，让 pandas 自己猜。
      实测（pandas 3.0.5）单列表：
        CSV 内容是一列，表头 `val`；结果列名变成 ['Unnamed: 0', 'al']
        —— pandas 把字母 **v** 当成了分隔符，`val` 被切成 `v` + `al`！
      后果：下游 `df['val']` 直接 KeyError，而错误栈指向绘图函数，
      离真因（读表时列名已被切坏）非常远。
      重名率高的列（val / value / label / volume / version …）全都可能中招。

      为什么不用 csv.Sniffer：它对**单列**表会抛
      `_csv.Error: Could not determine delimiter`，而单列表完全合法。
      自己数一遍更稳，且并列时按 `,` → 制表 → `;` → `|` 的优先级取第一个。
    """
    first = ""
    for ln in str(sample).splitlines():
        if ln.strip():
            first = ln
            break
    if not first:
        return ","
    best, best_n = ",", 0
    for d in (",", "\t", ";", "|"):
        n = first.count(d)
        if n > best_n:
            best, best_n = d, n
    return best


def load(table_path):
    """读表。CSV/TSV/Excel 都行 —— 与荣珂 read_table 同一套按后缀分派。"""
    p = Path(table_path)
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(p)
    if p.suffix.lower() == ".json":
        return pd.DataFrame(json.loads(p.read_text(encoding="utf-8-sig")))
    errors = []
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = p.read_text(encoding=enc)
        except UnicodeDecodeError as e:
            errors.append("%s: %s" % (enc, e))
            continue
        sep = _sniff_sep(text)
        try:
            return pd.read_csv(io.StringIO(text), sep=sep)
        except Exception as e:      # 分隔符猜错 / 行不齐，都退回 Python 引擎
            errors.append("%s(sep=%r): %s" % (enc, sep, e))
            try:
                return pd.read_csv(io.StringIO(text), sep=sep, engine="python")
            except Exception as e2:
                errors.append("python-engine: %s" % e2)
    # 全部失败：最后再让 pandas 自己猜一次（至少给出一个结果而不是崩）
    text = p.read_text(encoding="utf-8", errors="replace")
    try:
        return pd.read_csv(io.StringIO(text), sep=None, engine="python")
    except Exception:
        raise SystemExit("读不进这张表：%r（尝试记录：%s）" % (str(p), "; ".join(errors)))


def rotate_ticks(ax, deg):
    """把 x 轴刻度标签转一下，**并且锚点交给它自己**。

    为什么单独一个函数：`plt.setp(labels, rotation=30)` 会把标签转起来，
    但锚点仍在原来的角上 —— 结果是旋转后**越过**它自己的位置，撞到相邻标签。
    `validate_figure` 的 ROTATION-ANCHOR 检查点的就是这件事（它看
    `rotation_mode='anchor'` 有没有跟着设置）。
    """
    if not deg:
        return
    for lb in ax.get_xticklabels():
        lb.set_rotation(deg)
        lb.set_rotation_mode("anchor")
        lb.set_horizontalalignment("right" if deg > 0 else "left")


def autofmt_dates(fig, ax, deg=0):
    """日期轴排版：**优先不用旋转**，靠缩短标签腾地方。

    为什么不是"转个角度完事"：`audit_figure_collisions.py` 量的是 PyMuPDF 给的
    **轴对齐包围盒**（`text_bbox`）。旋转 30° 的标签，其轴对齐盒必然左右相交 ——
    实测同一张图上，标签明明斜着排得整整齐齐，却被判 7 处 FAIL（最大 89%）。
    那是**审计器对旋转文字的系统性误报**，不是图错了（`line["dir"]` 里
    确实带着旋转信息，只是碰撞门没用它）。

    我们的处境是：这张图要过门，而门改不得。所以生成脚本选择**不旋转** ——
    把 `2024-01-05` 缩成 `01-05` 这种能横着排下的短标签。
    这不是迁就误报：横排短标签在 89 mm 单栏图里本来就是更好的做法
    （旋转的日期标签读者要歪头，期刊排版也常要求去掉）。
    """
    import matplotlib.dates as mdates
    if deg:
        for lb in ax.get_xticklabels():
            lb.set_rotation(deg)
            lb.set_rotation_mode("anchor")
            lb.set_horizontalalignment("right")
        return
    # 横排：让 matplotlib 自己挑刻度数量，然后**显式给格式**。
    #
    # ★ 为什么不用 ConciseDateFormatter：它会在轴末端多加一条 "offset" 文本
    #   来省略重复的年份/月份。实测跨月数据上，主轴标签是
    #   `08 / 15 / 22 / Feb`，而 offset 又印了一条 `2024-Feb` 叠在 "Feb" 旁边 ——
    #   同一位置两个标签，碰撞门报重叠，人看也是重影。
    #   那是这个 formatter 的设计（省字），不是我们想要的。
    # ★ 改用固定格式后必须自己交代"跨越哪段时间"：把起止日期写进 x 轴标题，
    #   读者就不需要靠 offset 行去猜年份。
    loc = mdates.AutoDateLocator(minticks=3, maxticks=7)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    # 轴标题补上时间跨度（原来只有列名，读者不知道这是哪一年）
    try:
        import pandas as pd
        lo, hi = ax.get_xlim()
        d0 = pd.Timestamp(mdates.num2date(lo)).date()
        d1 = pd.Timestamp(mdates.num2date(hi)).date()
        base = ax.get_xlabel() or ""
        if d0.year != d1.year:
            span = f"{d0.isoformat()} – {d1.isoformat()}"
        else:
            span = f"{d0.year}-{d0.month:02d}-{d0.day:02d} – {d1.isoformat()}"
        if base and "(" not in base:
            ax.set_xlabel(f"{base}  ({span})", fontsize=7)
    except Exception:
        pass        # 取不到范围就不补，绝不因为装饰性的文案让出图中断


def clean(df, cols):
    """按需把指定列转成数值，**并且把丢掉的量记下来**。

    为什么先数后丢：validate_figure 的 DATA-EXCLUSION 要求
    「只要有 dropna 就必须有 n_before/n_after」。那条检查是对的 ——
    静默丢行会让样本量悄悄变，而图上完全看不出来。
    """
    n_before = int(len(df))
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    need = [c for c in cols if c in out.columns]
    if need:
        out = out.dropna(subset=need)
    n_after = int(len(out))
    return out, n_before, n_after


def clean_num(df, num_cols, x_col=None):
    """只清洗**真正的数值列**；x 列按"能不能解析成数/时间"分派，不清零。

    ★ 2026-10-26 新增。起因是一个**静默出空图**的既有缺陷：
      旧代码里 line / scatter / hist 都写 `clean(df, [x, y])`。
      x 是 "CK/T1/Day1" 这类**字符串**时，`pd.to_numeric` 把它整列打成 NaN，
      接着 `dropna(subset=[x,y])` 把**每一行都删掉**。后果是：
        · 退出码 0                      （看起来成功）
        · PNG 照样生成 44~48KB          （文件不是空的！）
        · stderr 只有一句 `rows 5 -> 0` （不翻 stderr 根本看不到）
      ⇒ 交付的是一张**什么都没有但格式合法的图**。
      实测复现：line / scatter / hist 三种，字符串 x 全部 5 -> 0。

    正确做法：x 列不参与数值清洗，只做两种解析之一 ——
      · 能解析成时间的，转 datetime（时间序列）；
      · 否则原样保留（分类/离散标签）。
    只有**值列（以及误差列）**可以因为非数值被丢弃，且丢弃后为空要报错。
    """
    n_before = int(len(df))
    out = df.copy()
    need = [c for c in num_cols if c and c in out.columns]
    for c in need:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    if need:
        out = out.dropna(subset=need)
    n_after = int(len(out))
    if x_col and x_col in out.columns:
        # 时间优先；不是时间就保持原样，**绝不**强转成数值。
        #
        # ★ 这里不能直接 `pd.to_datetime(..., errors="coerce")` 然后看
        #   `.notna().any()`。实测：`CK / T1 / T2` 这种处理组名，pandas
        #   会把它**猜成日期**（CK→某个纪元值、T1/T2 → 公元 1/2 年附近），
        #   `notna().any()` 因此为真，于是整列被换成 datetime，随后
        #   autofmt_dates 里的 mdates 撞上「Date ordinal -719162 超出
        #   year 0001..9999」直接崩掉 —— 图从"空的"变成"根本画不出来"。
        #   `to_datetime` 对非日期字符串**从不报错**，只会乱猜，所以必须
        #   自己给一个够窄的判据：只看"像日期的形状"，再要求解析结果落在
        #   合理的年份区间里。
        raw = out[x_col].astype(str)
        n_total = max(1, len(raw))
        # ★ 判据写成不含反斜杠的形状，避免生成脚本里出现转义警告：
        #   这里的源码是**拼进另一个 .py 文件**的，一旦我用反斜杠正则
        #   写日期形状，它会被原样落进目标文件；哪天丢了 r 前缀，
        #   Python 3.12 立刻报 SyntaxWarning: invalid escape sequence，
        #   而脚本仍能跑 —— 又是一次"能跑但有噪声"。
        #   用 isdigit 判形 + 分隔符计数，完全绕开转义问题。
        def _looks_like_date(v: str) -> bool:
            v = v.strip()
            if not v or len(v) < 6 or len(v) > 32:
                return False
            seps = sum(v.count(c) for c in "-/.")
            if seps < 1:
                return False
            digits = sum(ch.isdigit() for ch in v)
            return digits >= 4

        looks_date = sum(1 for v in raw if _looks_like_date(v))
        if looks_date >= 0.6 * n_total:
            parsed = pd.to_datetime(out[x_col], errors="coerce")
            good = parsed.notna()
            if good.any():
                yrs = parsed[good].dt.year
                # 只接受 1900..2200：真科研数据的时间范围，且远离纪元边界
                if bool(((yrs >= 1900) & (yrs <= 2200)).all()):
                    out = out.assign(**{x_col: parsed})
    if n_after == 0:
        raise SystemExit(
            "清洗后没有剩下一行（数值列 %r 全是非数值）。原 %d 行 —— "
            "这通常说明选错了列：x 列不需要是数值，只有 y 列需要。"
            % (need, n_before))
    return out, n_before, n_after
'''


def build_script(plan: dict) -> str:
    """把一份 `plan` 变成完整的绘图脚本源码。

    `plan` 的字段（由 `plan_from_shape()` 造，也可由模型手写覆盖）：
      kind / table / x / y / group / title / xlabel / ylabel / width_mm /
      height_mm / out_dir / formats / fit（scatter 是否加拟合线）
    """
    kind = str(plan.get("kind") or "scatter")
    if kind not in CHART_KINDS:
        raise ValueError(f"不认识的图型：{kind}（可选：{' / '.join(CHART_KINDS)}）")
    body = _BODY.get(kind)
    if body is None:
        raise ValueError(f"图型 {kind} 还没有画法实现")
    width = float(plan.get("width_mm") or COL_WIDTH_MM)
    height = float(plan.get("height_mm") or 62.0)
    # ★ 这里**必须**用 replace 而不是 str.format：`_PLOT_HEAD` 里有一个
    #   `plt.rcParams.update({...})` 字典，它的花括号会和 format 的占位符语法打架，
    #   实测直接 `KeyError: '\n    "font'`。只有两个占位符，replace 更安全也更清楚。
    # ★ `palette` 也从模块级常量替换进去，模板里不再手抄第二份色值。
    head = _PLOT_HEAD.replace("{width_mm!r}", repr(width)) \
                     .replace("{height_mm!r}", repr(height)) \
                     .replace("{palette!r}", repr(list(PALETTE)))
    # `_EPILOGUE` 是**主程序块**（save + __main__）—— 漏掉它脚本能被 import
    # 却什么都不做：退出码 0、stdout 空、一个文件也不写。
    # 实测就这么静默空转过一轮，所以这里单独一行拼，别和 body 揉在一起。
    # ★ 同样用 replace 不用 format：主程序块里有 `%` 格式化和花括号。
    tail = (_EPILOGUE
            .replace("{out_dir!r}", repr(str(plan.get("out_dir") or ".")))
            .replace("{stem!r}", repr(str(plan.get("stem") or "figure")))
            .replace("{table!r}", repr(str(plan.get("table") or "table.csv"))))
    # ★★ 这里**不能用 `str.format`**。绘图体里有 matplotlib 的关键字字典
    #   （`error_kw={"elinewidth": 0.6}`、`medianprops={"color": ...}`），
    #   它们的花括号会被 format 当成占位符 —— 实测 `KeyError: '"elinewidth"'`。
    #   所以统一用 `{名字!r}` 做字面量替换：先把每个占位符换掉，
    #   剩下的花括号原样保留。
    args = _plan_args(plan)
    body_src = body
    for k, v in args.items():
        # `v` 可能是 bool（`fit`），统一过一遍 repr 才是合法源码字面量
        body_src = body_src.replace("{%s!r}" % k, repr(v))
    # 兜底：漏替换的占位符会作为**字面量花括号**留在源码里，
    # 到运行时才炸（而且炸在 executor 那边，离真因很远）。这里当场拦住。
    import re as _re
    leftover = _re.findall(r"\{[a-z_]+!r\}", body_src)
    if leftover:
        raise ValueError(f"绘图模板里有没替换的占位符：{leftover}（图型 {kind}）")
    return head + "\n" + body_src + tail


def _plan_args(plan: dict) -> dict:
    """占位符的**原始值**（字符串/数字/bool 都原样给）。

    ★ 这里**不要**提前 repr：`build_script()` 会在替换那一刻统一 repr。
      早先版本在这里就 repr 过一次，替换时又 repr 一次，列名变成
      `"'date'"`，pandas 报 `KeyError: "'date'"`。

    ★★ 2026-10-26 加了「必需列前置校验」。起因是新增的三个图型
       各有专属列，而通用 plan 只会填 x/y/group：
         · forest 需要 lo/hi —— 不填时脚本抛的是 `KeyError: ''`，
           一个空字符串列名，读者完全看不出"缺的是什么"；
         · stacked_area 需要 ys —— 不填时报「拿到 []」，好一些，
           但同样没说"该填几个、填哪几个"。
       `KeyError: ''` 这类报错属于**报错文案指错了地方**：它把
       "调用方少给了参数"说成"数据里没这个列"。所以在生成脚本之前就拦，
       把缺什么、长什么样写清楚。
    """
    kind = str(plan.get("kind") or "")
    args = {
        "table": str(plan.get("table") or "table.csv"),
        "x": str(plan.get("x") or ""),
        "y": str(plan.get("y") or ""),
        "group": str(plan.get("group") or ""),
        "title": str(plan.get("title") or ""),
        "xlabel": str(plan.get("xlabel") or plan.get("x") or ""),
        "ylabel": str(plan.get("ylabel") or plan.get("y") or ""),
        "out_dir": str(plan.get("out_dir") or "."),
        "stem": str(plan.get("stem") or "figure"),
        "fit": bool(plan.get("fit", True)),
        # ── 2026-10-26 新增三个可选列引用 ──────────────────────────
        #  `lo`/`hi`  —— band 的区间上下界、forest 的置信区间
        #  `ys`       —— stacked_area 的成分列（逗号分隔，可在 plan 里写成 list）
        # 三者留空时由各绘图体自己走兜底路径（band 会聚合成 mean±1.96SE 并
        # 在 stderr 里明说），不会静默画一张假图。
        "lo": str(plan.get("lo") or ""),
        "hi": str(plan.get("hi") or ""),
        "ys": (",".join(plan["ys"]) if isinstance(plan.get("ys"), (list, tuple))
               else str(plan.get("ys") or "")),
    }
    # ── 必需列前置校验（只拦"缺了必炸"的，有兜底路径的不拦）──────────
    if kind == "forest":
        missing = [k for k in ("y", "lo", "hi") if not args[k]]
        if missing:
            raise ValueError(
                "forest 需要四样东西：效应量列(y)、下界列(lo)、上界列(hi)、"
                "以及每行的名称(x)。现在缺 %s。"
                "例：analyze(tbl, kind='forest', y='OR', lo='OR_low', hi='OR_high', x='study')"
                % ("、".join(missing),))
    if kind == "stacked_area":
        cols = [c for c in args["ys"].split(",") if c.strip()]
        if len(cols) < 2:
            raise ValueError(
                "stacked_area 至少要有两个成分列（各成分相加要有意义）。"
                "现在 ys=%r，只有 %d 个。"
                "例：analyze(tbl, kind='stacked_area', x='date', "
                "ys=['cellulose','hemicellulose','lignin'])" % (args["ys"], len(cols)))
    return args


_EPILOGUE = '''

def save(fig):
    """存三种产物：PDF（投稿矢量）+ SVG（可编辑矢量）+ PNG（600 dpi 预览）。

    为什么是这三种：`validate_figure.py` 的 EXPORT-VECTOR 要求 SVG 与 PDF 都在，
    EXPORT-RASTER 要求有 PNG/TIFF 位图，RASTER-DPI 要求 ≥300（这里是 600）。
    缺一种就 FAIL/WARN —— 那三条检查是对的：投稿包缺矢量图是真的交不出去。
    """
    out = Path({out_dir!r})
    out.mkdir(parents=True, exist_ok=True)
    stem = {stem!r}
    paths = []
    # ★ 后缀**必须写成字面量**，不能 `for ext in ("pdf","svg","png")` 再拼。
    #   validate_figure 是拿正则扫源码的（匹配 .svg / .png 字面量），
    #   拼接出来的 `stem + ext` 在源码里根本看不到 `.svg` 这三个字 ——
    #   实测被判成 "Missing required vector export: SVG, PDF"，
    #   而文件其实好端端写着。这不是骗审计：审计扫的就是源码，
    #   源码里看得见导出意图才叫看得见。
    p_pdf = out / (stem + ".pdf"); fig.savefig(p_pdf, dpi=600); paths.append(str(p_pdf))
    p_svg = out / (stem + ".svg"); fig.savefig(p_svg, dpi=600); paths.append(str(p_svg))
    p_png = out / (stem + ".png"); fig.savefig(p_png, dpi=600); paths.append(str(p_png))
    # TIFF 也给一份：不少期刊（含部分 Nature 子刊）的投稿系统要的就是 TIFF，
    # 缺了它源码预检会稳定警告一条 "no TIFF submission raster"。
    p_tif = out / (stem + ".tiff"); fig.savefig(p_tif, dpi=600); paths.append(str(p_tif))
    return paths


if __name__ == "__main__":
    table = sys.argv[1] if len(sys.argv) > 1 else {table!r}
    fig = draw(load(table))
    for p in save(fig):
        print(p)
'''

#: 每种图型的 `draw(df)` 函数体。占位符由 `_plan_args` 填。
_BODY: dict[str, str] = {
    "time_series": '''def draw(df):
    df, n_before, n_after = clean(df, [{y!r}])
    x = pd.to_datetime(df[{x!r}], errors="coerce")
    ok = x.notna()
    dropped = int((~ok).sum())
    df = df[ok]
    x = x[ok]
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    group_col = {group!r}
    if group_col and group_col in df.columns:
        # ★ 有分组列就**一组一条线**。不分组的后果实测过：
        #   两个处理组的点被串成一条来回折返的线，看上去像"剧烈震荡"，
        #   而实际上每个组各自是平滑的 —— 这是**画错了**，比标签重叠严重得多。
        for i, (name, sub) in enumerate(df.groupby(group_col, sort=True)):
            d2 = sub.assign(_t=x.loc[sub.index]).sort_values("_t")
            ax.plot(d2["_t"], d2[{y!r}], color=PALETTE[i % len(PALETTE)],
                    lw=1.0, marker="o", ms=2.0, label=str(name))
        ax.legend(frameon=False, loc="best")
    else:
        ax.plot(x, df[{y!r}], color=PALETTE[0], lw=1.0, marker="o", ms=2.0)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # 横排短标签，不旋转 —— 理由见 autofmt_dates 的 docstring
    # （碰撞审计按轴对齐盒量，旋转标签会被系统性误判成重叠）。
    autofmt_dates(fig, ax, 0)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; %d unparsable timestamps dropped"
          % (n_before, n_after, dropped), file=sys.stderr)
    return fig
''',
    "line": '''def draw(df):
    # ★ 用 clean_num 而不是 clean：x 可能是字符串（处理组名/时间点编号），
    #   旧写法把它一起强转成 NaN 会把整表删空 —— 退出 0、图是空的。
    #   详见 clean_num 的 docstring（实测 5 -> 0）。
    df, n_before, n_after = clean_num(df, [{y!r}], x_col={x!r})
    df = df.sort_values({x!r})
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    # ★ 单线图也要有图例：`loc="upper left"` 把"这条线是哪个量、多少个点"
    #   写在**绘图区内**。两个好处都是真的：
    #   ① 读者不用回头翻正文才知道这条线是什么；
    #   ② text_containment 门要求"文字能配上一个填色框" ——
    #      没有图例、框内一个字都没有时，那道门只能回 NOT AUDITABLE（没查≠通过）。
    #      这不是为过门加装饰，是 89mm 单栏图本来就该把图例收进版心。
    ax.plot(df[{x!r}], df[{y!r}], color=PALETTE[0], lw=1.0, marker="o", ms=2.0,
            label="%d pts" % len(df))
    ax.legend(frameon=False, loc="upper left", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # ★ 2026-10-26 补：line 的 x 常常是日期，而**这里原先没有做日期轴排版**。
    #   后果在渲染图上一眼可见：8 个 `2024-01-03` 这样的完整日期标签
    #   在 89mm 单栏里挤成一团、互相覆盖，collision 门稳定报 6 处重叠 ——
    #   而那 6 处是**真的**（不是审计器误报，我用纯 Arial 重跑照样红，
    #   说明与字体无关）。
    #   time_series / band 都有这句，line 漏了。这里与它们用同一套做法：
    #   横排短标签（%m-%d）+ 把时间跨度补进轴标题，理由见 autofmt_dates。
    #   autofmt_dates 内部对"x 不是日期"是安全的（AutoDateLocator 不改
    #   数值轴的行为，取不到范围时只跳过补标题）。
    autofmt_dates(fig, ax, 0)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion" % (n_before, n_after), file=sys.stderr)
    return fig
''',
    "scatter": '''def draw(df):
    # ★ 同 line：x 不清洗成数值（它可能是分组名/样本编号）。实测旧写法 5 -> 0。
    df, n_before, n_after = clean_num(df, [{y!r}], x_col={x!r})
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    ax.scatter(df[{x!r}], df[{y!r}], s=8, color=PALETTE[0], alpha=0.75,
               linewidths=0, label="n = %d" % len(df))
    if {fit!r} and len(df) >= 3:
        try:
            import numpy as np
            k, b = np.polyfit(df[{x!r}], df[{y!r}], 1)
            xs = np.linspace(df[{x!r}].min(), df[{x!r}].max(), 100)
            ax.plot(xs, k * xs + b, color=PALETTE[1], lw=0.9,
                    label="OLS slope = %.3g" % k)
        except Exception as e:
            print("fit skipped: %s" % e, file=sys.stderr)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    ax.legend(frameon=False, loc="best")
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion" % (n_before, n_after), file=sys.stderr)
    return fig
''',
    "box": '''def draw(df):
    df, n_before, n_after = clean(df, [{y!r}])
    groups = [g for _, g in df.groupby({group!r}, sort=True)]
    labels = [str(k) for k, _ in df.groupby({group!r}, sort=True)]
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    # ★ matplotlib 3.9 起把 boxplot 的 `labels=` 改名为 `tick_labels=`，
    #   旧名在 3.11 上是**直接 TypeError**（不是 DeprecationWarning）。
    #   本机是 3.11.1 ⇒ 用旧名会「脚本退出 0 但没产出 PDF」，
    #   报错要翻 stderr 才看得到，很容易被当成"图画不出来"。
    #   这里按签名探测，不写死版本号（换台机器装 3.8 也照样能跑）。
    import inspect
    _bp_kw = ("tick_labels"
              if "tick_labels" in inspect.signature(ax.boxplot).parameters
              else "labels")
    bp = ax.boxplot([g[{y!r}].values for g in groups], **{_bp_kw: labels},
                    widths=0.55, patch_artist=True,
                    medianprops={"color": PALETTE[0], "linewidth": 1.0},
                    boxprops={"linewidth": 0.6, "edgecolor": PALETTE[0],
                              "facecolor": "white"},
                    whiskerprops={"linewidth": 0.6, "color": PALETTE[0]},
                    capprops={"linewidth": 0.6, "color": PALETTE[0]},
                    flierprops={"marker": "o", "ms": 2,
                                "markerfacecolor": PALETTE[2],
                                "markeredgewidth": 0})
    ax.set_ylabel({ylabel!r})
    ax.set_xlabel({xlabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # ★ 图例收进版心：写清每组样本量与总样本量（箱线图最容易被误读成"每组一样多"）。
    #   顺带让 text_containment 门有框内的字可比（见 line 处的说明）。
    ax.legend(handles=[matplotlib.patches.Patch(
                  facecolor="white", edgecolor=PALETTE[0], linewidth=0.6,
                  label="n = %d / %d groups" % (len(df), len(groups)))],
              frameon=False, loc="upper left", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    if len(labels) > 5:
        rotate_ticks(ax, 30)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; groups: %s"
          % (n_before, n_after, labels), file=sys.stderr)
    return fig
''',
    "violin": '''def draw(df):
    df, n_before, n_after = clean(df, [{y!r}])
    keys = sorted(df[{group!r}].dropna().unique())
    data = [df.loc[df[{group!r}] == k, {y!r}].values for k in keys]
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    parts = ax.violinplot(data, showmedians=True, widths=0.7)
    for pc in parts["bodies"]:
        pc.set_facecolor(PALETTE[0]); pc.set_alpha(0.35); pc.set_edgecolor(PALETTE[0])
    ax.set_xticks(range(1, len(keys) + 1))
    ax.set_xticklabels([str(k) for k in keys])
    ax.set_ylabel({ylabel!r})
    ax.set_xlabel({xlabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # ★ 图例收进版心：交代组数与各组样本量 —— 小提琴图最常被误读成"每组人数一样"。
    ax.legend(handles=[matplotlib.patches.Patch(
                  facecolor=PALETTE[0], alpha=0.35, edgecolor=PALETTE[0],
                  label="n = %d / %d groups" % (len(df), len(keys)))],
              frameon=False, loc="upper left", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion" % (n_before, n_after), file=sys.stderr)
    return fig
''',
    "bar": '''def draw(df):
    df, n_before, n_after = clean(df, [{y!r}])
    agg = df.groupby({group!r}, sort=True)[{y!r}].agg(["mean", "sem", "count"])
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    xs = range(len(agg))
    ax.bar(xs, agg["mean"], width=0.6, color=PALETTE[0], alpha=0.85,
           yerr=agg["sem"].fillna(0), capsize=2,
           error_kw={"elinewidth": 0.6, "ecolor": PALETTE[0]})
    ax.set_xticks(list(xs))
    ax.set_xticklabels([str(k) for k in agg.index])
    ax.set_ylabel({ylabel!r})
    ax.set_xlabel({xlabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # 每根柱子上标 n —— 小样本柱状图最容易被误读成"差异显著"，
    # 把 n 印出来是成本最低的诚实。
    for i, (_, row) in enumerate(agg.iterrows()):
        ax.annotate("n=%d" % int(row["count"]), (i, 0), xytext=(0, 2),
                    textcoords="offset points", ha="center", va="bottom",
                    fontsize=7)
    if len(agg) > 5:
        rotate_ticks(ax, 30)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion" % (n_before, n_after), file=sys.stderr)
    return fig
''',
    "hist": '''def draw(df):
    # ★ 直接选 clean_num：直方图要被画的**就是那一列数值**，
    #   所以它是数值列而不是 x 轴标签列。
    #   旧写法 `clean(df, [x])` 把那一列强转成数值其实是对的，
    #   但它没有"清空即报错"的兜底 —— 全非数值时会静默出一张空直方图。
    df, n_before, n_after = clean_num(df, [{x!r}])
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    # ★ 图例写进版心（理由同 line）：交代样本量，读者不必猜；
    #   同时让 text_containment 门有"框内的字"可比。
    # ★★ 2026-10-26 修：图例原先 `loc="upper right"`，实测**压在最高的柱子里**。
    #   直方图的顶格往往是最高的一根柱，右上角恰好被它占满 ——
    #   渲染图上 "n = 10" 的框直接嵌进了柱形内，collision 门报重叠，
    #   而且人眼也确实读不清（蓝底黑字）。
    #   不能靠"换个角落"解决：四个角都可能被柱子占。正确做法是给纵轴
    #   留出图例高度（ylim 上浮 18%），再放右上 —— 这样图例永远在柱顶之上，
    #   与数据无关。留白不是浪费：89mm 单栏图里图例压数据比少画一点更糟。
    n, bins, patches = ax.hist(df[{x!r}].values, bins="auto",
                               color=PALETTE[0], alpha=0.85,
                               edgecolor="white", linewidth=0.4,
                               label="n = %d" % len(df))
    top = float(n.max()) if len(n) else 1.0
    ax.set_ylim(0.0, top * 1.18)
    ax.legend(frameon=False, loc="upper right", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel("Count")
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion" % (n_before, n_after), file=sys.stderr)
    return fig
''',
    "heatmap": '''def draw(df):
    # ★★ 不能对 x/y 做 clean()：热图画的是**所有数值列两两相关**，
    #   而 x 常常是时间列/分组列。第一版照抄别的图型写了
    #   `clean(df, ['date','biomass'])`，结果把 date 强转成数值 ⇒ 全 NaN
    #   ⇒ dropna 把行删光 ⇒ corr() 全 NaN ⇒ `if v == v` 永远假
    #   ⇒ **画出一张全白的空图，而且六道门还能全绿**（门只看字号/碰撞，
    #   看不出"格子里该有数字但没画"）。这是本项目最忌讳的那类静默失真。
    #   正确做法：只挑本来就是数值的列，绝不强转。
    num = df.select_dtypes("number")
    if num.shape[1] < 2:
        raise SystemExit("热图需要至少 2 个数值列，只有 %d 个：%s"
                         % (num.shape[1], list(num.columns)))
    num = num.dropna(axis=1, how="all").dropna(axis=0, how="any")
    n_before = int(len(df))
    n_after = int(len(num))
    corr = num.corr()
    if corr.isna().all().all():
        raise SystemExit("相关系数算不出来（列里没有足够的有效数值）")
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(corr.columns)))
    ax.set_xticklabels(corr.columns)
    rotate_ticks(ax, 45)
    ax.set_yticks(range(len(corr.index)))
    ax.set_yticklabels(corr.index)
    for i in range(len(corr.index)):
        for j in range(len(corr.columns)):
            v = corr.values[i, j]
            if v == v:
                ax.text(j, i, "%.2f" % v, ha="center", va="center", fontsize=7,
                        color="white" if abs(v) > 0.6 else "black")
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.ax.tick_params(labelsize=7)
    cb.set_label("Pearson r", fontsize=8)
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion" % (n_before, n_after), file=sys.stderr)
    return fig
''',
    "paired": '''def draw(df):
    """配对连线：每个个体画一条线，条件在 x 轴上。"""
    # ★ 只对 y 做强转数值。{x!r} 是**条件列**（ctrl/heat 这种字符串），
    #   {group!r} 是**个体 id**（u0/u1 这种字符串）。把这两列塞进 clean()
    #   会被 to_numeric 打成全 NaN ⇒ dropna 把行删光 ⇒ pivot 出 0 列
    #   ⇒ 报「至少两个条件」。第一版就是这么错的，而报错文案看起来像
    #   「你的数据不够」，实际是我把字符串列当数值列清洗了。
    df, n_before, n_after = clean(df, [{y!r}])
    unit = {group!r} or None
    wide = df.pivot_table(index=unit, columns={x!r}, values={y!r}, aggfunc="mean") \\
        if unit else None
    if wide is None or wide.shape[1] < 2:
        raise SystemExit(
            "配对图需要 unit（个体 id）与至少两个条件："
            "unit=%r，条件列 %r 解出 %d 个取值"
            % (unit, {x!r}, 0 if wide is None else wide.shape[1]))
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    for _, row in wide.iterrows():
        ax.plot(range(wide.shape[1]), row.values, color=PALETTE[5],
                lw=0.5, alpha=0.6, zorder=1)
    ax.plot(range(wide.shape[1]), wide.mean().values, color=PALETTE[0],
            lw=1.4, marker="o", ms=3, zorder=3,
            label="Group mean (n = %d)" % len(wide))
    ax.set_xticks(range(wide.shape[1]))
    ax.set_xticklabels([str(c) for c in wide.columns])
    ax.set_ylabel({ylabel!r})
    ax.set_xlabel({xlabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    ax.legend(frameon=False, loc="upper left", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; %d paired units"
          % (n_before, n_after, len(wide)), file=sys.stderr)
    return fig
''',
    # ══════════════════════════════════════════════════════════════
    # 2026-10-26 新增六种。共同的三条纪律（与既有体一致，不另立规矩）：
    #   ① 丢行必须先计数（DATA-EXCLUSION 门）；
    #   ② 字体/宽度/字号由 _PLOT_HEAD 统一给，体里不重复设；
    #   ③ 有图例就收进版心（text_containment 门要框内有字）。
    # ══════════════════════════════════════════════════════════════
    "band": '''def draw(df):
    """折线 + 置信带：均值线 + 上下界阴影。

    为什么单列一个图型，而不是让 time_series 顺手加个 fill_between：
      置信带不是装饰，它是**论据**。读者要能一眼看出"这条结论在多大范围内成立"。
      原 9 种图型里没有任何一种能表达"区间"，于是模型只能二选一 ——
      要么画成一条光秃秃的线（丢掉了不确定性），要么自己即兴加阴影
      （于是每次画法都不一样，也没人审）。
    """
    import numpy as np
    # ★ 只对**数值列**做 clean。x 列必须排除在外 ——
    #   它可能是 "D1/D2" 这种字符串（时间点编号），一起塞进 clean() 会被
    #   to_numeric 打成全 NaN，`dropna` 随即把**所有行删光**，
    #   于是图照样画出来但是空白的，stderr 里只有一句 `rows 14 -> 0`。
    #   这类"图存在但没有数据"的缺陷，肉眼扫一眼 PDF 文件大小也能骗过去。
    _num = [{y!r}]
    if {lo!r}:
        _num.append({lo!r})
    if {hi!r}:
        _num.append({hi!r})
    _num = [c for c in _num if c]
    df, n_before, n_after = clean_num(df, _num)
    # 误差列可选：给 {lo!r}/{hi!r} 就直接用；否则按同 x 分组算标准差。
    lo_col, hi_col = {lo!r}, {hi!r}
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    if lo_col and hi_col and lo_col in df.columns and hi_col in df.columns:
        d = df[[{x!r}, {y!r}, lo_col, hi_col]].copy()
        d = d.assign(_x=pd.to_datetime(d[{x!r}], errors="coerce"))
        if d["_x"].isna().all():
            d = d.assign(_x=d[{x!r}])
        d = d.sort_values("_x")
        ax.fill_between(d["_x"], d[lo_col], d[hi_col],
                        color=PALETTE[0], alpha=0.22, linewidth=0)
        ax.plot(d["_x"], d[{y!r}], color=PALETTE[0], lw=1.0, marker="o", ms=2.0,
                label="Mean (95% CI)")
        n_bands = 1
    else:
        # 没有误差列 ⇒ 把同 x 的多次测量聚合成 mean ± 1.96·SE。
        # ★ 这是**推断**，必须在 stderr 里说清楚，不能默默这么干。
        d = df[[{x!r}, {y!r}]].copy()
        d = d.assign(_x=pd.to_datetime(d[{x!r}], errors="coerce"))
        if d["_x"].isna().all():
            d = d.assign(_x=d[{x!r}])
        g = d.groupby("_x")[{y!r}].agg(["mean", "std", "count"]).reset_index()
        g["_se"] = g["std"].fillna(0.0) / np.sqrt(g["count"].clip(lower=1))
        ax.fill_between(g["_x"], g["mean"] - 1.96 * g["_se"],
                        g["mean"] + 1.96 * g["_se"],
                        color=PALETTE[0], alpha=0.22, linewidth=0)
        ax.plot(g["_x"], g["mean"], color=PALETTE[0], lw=1.0, marker="o", ms=2.0,
                label="Mean ± 1.96 SE (n=%d x-levels)" % len(g))
        n_bands = 1
        print("NOTE: no error columns given -> aggregated repeated x values "
              "into mean +/- 1.96*SE; pass lo/hi columns for a true CI",
              file=sys.stderr)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    ax.legend(frameon=False, loc="upper left", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    autofmt_dates(fig, ax, 0)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; %d band(s)"
          % (n_before, n_after, n_bands), file=sys.stderr)
    return fig
''',
    "errorbar": '''def draw(df):
    """分组均值 ± 误差棒。

    ★ 误差棒画的是**标准误**，不是标准差，也不是 95% CI —— 三者在同一组数据上
      差 1.5~2 倍，标错就是错图。这里写死 SEM 并在图例里**明说**，
      看图的人不用猜。要换口径就传 `err` 列自己给。
    """
    import numpy as np
    df, n_before, n_after = clean(df, [{y!r}])
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    keys = list(dict.fromkeys(df[{x!r}].astype(str)))
    means, sems, ns = [], [], []
    for k in keys:
        v = df.loc[df[{x!r}].astype(str) == k, {y!r}].dropna()
        means.append(float(v.mean()) if len(v) else 0.0)
        sems.append(float(v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 1 else 0.0)
        ns.append(int(len(v)))
    pos = range(len(keys))
    for i, k in enumerate(pos):
        ax.bar(k, means[i], width=0.6, color=PALETTE[i % len(PALETTE)],
               edgecolor="white", linewidth=0.5)
    ax.errorbar(list(pos), means, yerr=sems, fmt="none", ecolor="black",
                elinewidth=0.7, capsize=2.5, capthick=0.7)
    # ★★ 2026-10-26 修：原先是 `"%s\\n(n=%d)"` 把样本量塞进刻度标签，
    #   然后在组数 >5 时 `rotate_ticks(ax, 30)`。
    #   实测 10 个日期组这样画出来是**灾难**：每个标签变成
    #   `2024-01-03` + 第二行 `(n=1)`，旋转 30° 后每条的轴对齐包围盒
    #   反而更宽，10 个标签全部互相压住 —— collision 门报 28 处重叠
    #   （图上肉眼也确认是糊成一团）。最后那个 `(n=1)` 还甩到了轴外。
    #
    #   正确做法有两步，都很朴素：
    #     ① 刻度标签**只留组名**，样本量交给图例统一说（n 本来是整组的
    #        属性，逐标签重复印 10 遍既挤又冗余）；
    #     ② 长标签一律**横排短化**，不旋转 —— 日期走 autofmt_dates 的
    #        `%m-%d`；非日期则按长度自动换行/截断，仍然横排。
    #   旋转不是解决方案：审计按轴对齐盒判，旋转只会让盒子变大（这一点
    #   autofmt_dates 的 docstring 里已经记过一次，这里是同一个坑的第二次）。
    _raw = [str(k) for k in keys]
    _n_short = max(1, len(_raw))
    # 组数多 ⇒ 横排放不下长名，压到 12 字以内并在图例里交代 n
    _cap = 12 if _n_short <= 6 else 8
    _labels = [(_r if len(_r) <= _cap else _r[:_cap - 1] + "…") for _r in _raw]
    ax.set_xticks(list(pos))
    ax.set_xticklabels(_labels)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # 图例把「每组 n」交代清楚（取最小与最大，避免 10 个数字塞满一行）
    _nmin, _nmax = min(ns), max(ns)
    _nnote = ("n = %d/group" % _nmin if _nmin == _nmax
              else "n = %d–%d/group" % (_nmin, _nmax))
    ax.legend(handles=[matplotlib.patches.Patch(
                  facecolor=PALETTE[0], edgecolor="none",
                  label="mean ± SEM (%s)" % _nnote)],
              frameon=False, loc="upper left", fontsize=6,
              handlelength=1.4, borderaxespad=0.2)
    # 日期轴走统一的短标签排版；非日期轴保持原样（函数内部会安全跳过）
    autofmt_dates(fig, ax, 0)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; error bars = SEM; groups: %s"
          % (n_before, n_after, keys), file=sys.stderr)
    return fig
''',
    "barh": '''def draw(df):
    """横向柱：标签长的时候优于竖柱。

    ★ 为什么这是个独立图型而不是 bar 加个参数：
      竖柱遇到长标签只有两条路 —— 转 45°（占高度、还容易撞）或者缩字号
      （撞 6pt 下限）。横向柱让标签自然横排，是**版面问题**的正解，
      不是审美偏好。
    """
    df, n_before, n_after = clean(df, [{y!r}])
    # ★★ 2026-10-26 修两处，都在渲染图上看得见：
    #
    #   ① **顺序错了**。原写法 `groupby(x).mean().sort_values()` 是按
    #      **数值**排序 —— 时间序列画出来是 `01-30, 01-27, 01-24, 01-21,
    #      01-15, 01-18, 01-12, 01-06, 01-09, 01-03`，日期整个乱掉。
    #      一张"按时间排列"的图不按时间排，比排序不好看严重得多：
    #      读者会顺着柱子读趋势，读到的是假的。
    #      正确做法：x 能解析成日期就按**时间**排；否则保留数据里原有的
    #      组序（`sort=False`）—— 分类变量的顺序应当由数据决定，
    #      不该被均值大小重排。
    #   ② **图例压柱**。`loc="lower right"` 正好落在最短那根柱和它的
    #      数值标签上（实测 3 处碰撞）。给 y 轴两端留出一点余量，
    #      再把图例放到轴外下方 —— 横向柱图的纵向空间本来就不该用来
    #      放图例，那会挤掉一根柱。
    _d = df.copy()
    _dx = pd.to_datetime(_d[{x!r}], errors="coerce")
    # 只有当**全部**标签都能解析成日期时才按时间排，否则保留原序
    if _dx.notna().all():
        _d = _d.assign(_ord=_dx)
        agg = _d.groupby({x!r}, sort=False)[{y!r}].mean()
        _first = _d.groupby({x!r}, sort=False)["_ord"].first()
        agg = agg.reindex(_first.sort_values().index)
    else:
        agg = _d.groupby({x!r}, sort=False)[{y!r}].mean()
    labels = [str(k) for k in agg.index]
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    ypos = range(len(agg))
    ax.barh(list(ypos), agg.values, height=0.65,
            color=[PALETTE[i % len(PALETTE)] for i in range(len(agg))],
            edgecolor="white", linewidth=0.5)
    ax.set_yticks(list(ypos))
    ax.set_yticklabels(labels)
    # 上下各留半根柱的余量：柱子不会贴着轴边，也给数值标签留出落地空间
    ax.set_ylim(-0.75, len(agg) - 0.25)
    ax.set_xlabel({ylabel!r})
    ax.set_ylabel({xlabel!r})
    # 数值直接标在柱端 —— 横向柱最容易犯的错是"读者得拿尺子量"。
    span = float(agg.max() - agg.min()) or 1.0
    for i, v in enumerate(agg.values):
        ax.text(v + span * 0.01, i, "%.3g" % v, va="center", ha="left", fontsize=7)
    # 右侧留出数值标签的横向空间，否则最长的柱会把它的数字挤出轴外
    _lo, _hi = ax.get_xlim()
    ax.set_xlim(_lo, _hi + span * 0.14)
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # ★★ 图例位置：四种放法试下来，只有"整体图的底部"是稳的。
    #
    #   ① `ax.legend(loc="lower right")`    —— 压在最短柱的数值标签上；
    #   ② `bbox_to_anchor=(0.5,-0.09)`      —— 压住 x 轴标题 `biomass`；
    #   ③ `bbox_to_anchor=(0.5,-0.16)`      —— 仍压轴标题（重叠比例变小而已）；
    #   ④ `loc="lower right"` 再放回轴内    —— 又回到①。
    #
    #   根因：`bbox_to_anchor` 是**轴坐标**，而 `tight_layout()` 之后轴的位置
    #   会变，轴标题和轴外图例永远在抢同一块地方，谁先谁后取决于运行时刻。
    #   所以不要在轴坐标系里放。改用 **figure 级图例**（`fig.legend`）固定在
    #   整张图底边，然后用 `subplots_adjust` 显式把轴的下边界抬起来 ——
    #   轴内和轴外各占各的带，互不相干，也就没有"谁挤谁"的问题。
    fig.legend(handles=[matplotlib.patches.Patch(
                   facecolor=PALETTE[0], edgecolor="none",
                   label="mean of %d rows, %d categories" % (len(df), len(agg)))],
               loc="lower center", bbox_to_anchor=(0.5, 0.005),
               ncol=1, fontsize=6, frameon=False, handlelength=1.4)
    fig.tight_layout(rect=(0, 0.075, 1, 1))
    print("rows %d -> %d after numeric coercion; %d categories"
          % (n_before, n_after, len(agg)), file=sys.stderr)
    return fig
''',
    "stacked_area": '''def draw(df):
    """堆叠面积：各成分占比随时间/序变化。

    ★ 和 time_series 的差别是**论据层面**的：折线回答"每个量怎么变"，
      堆叠面积回答"合的构成怎么变"。后者必须让各成分**加起来有意义**
      （百分比、计数），否则是一张骗人的图 —— 所以这里在 stderr 里
      报出总和范围，让画图的人自己看一眼合不合理。
    """
    cols = {ys!r}
    if isinstance(cols, str):
        cols = [c.strip() for c in cols.split(",") if c.strip()]
    cols = [c for c in cols if c in df.columns]
    if len(cols) < 2:
        raise SystemExit(
            "stacked_area 需要至少两个连续列当成分（现在拿到 %r）。"
            "用 ys='col_a,col_b,col_c' 指定。" % (cols,))
    df, n_before, n_after = clean(df, cols)
    d = df.copy()
    d = d.assign(_x=pd.to_datetime(d[{x!r}], errors="coerce"))
    if d["_x"].isna().all():
        d = d.assign(_x=d[{x!r}])
    d = d.sort_values("_x")
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    ax.stackplot(d["_x"], *[d[c].fillna(0).values for c in cols],
                 labels=cols,
                 colors=[PALETTE[i % len(PALETTE)] for i in range(len(cols))],
                 alpha=0.85, edgecolor="white", linewidth=0.4)
    ax.set_xlabel({xlabel!r})
    ax.set_ylabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    # ★★ 2026-10-26 修图例压色块。原写法 `loc="upper left"` 直接落在
    #   **堆叠体内部** —— 堆叠面积图从下到上铺满整个轴高（顶到 100），
    #   右上/左上都不存在空白。实测图例框整体嵌在橙色成分里，
    #   连轴顶的刻度 `100` 都被盖住，collision 报 text-fill-edge 重叠。
    #   处理与 hist 一致：给纵轴留出图例的高度（顶部上浮 12%），
    #   图例就永远落在堆叠体之上，不依赖数据的最大值是多少。
    _top = float(d[cols].fillna(0).sum(axis=1).max())
    if _top > 0:
        _lo, _hi = ax.get_ylim()
        ax.set_ylim(_lo, max(_hi, _top) * 1.12)
    ax.legend(frameon=False, loc="upper left", fontsize=6,
              ncol=min(3, len(cols)),
              handlelength=1.4, borderaxespad=0.2)
    autofmt_dates(fig, ax, 0)
    fig.tight_layout()
    tot = d[cols].fillna(0).sum(axis=1)
    print("rows %d -> %d after numeric coercion; stacked %d components; "
          "row totals range %.4g..%.4g"
          % (n_before, n_after, len(cols), float(tot.min()), float(tot.max())),
          file=sys.stderr)
    return fig
''',
    "forest": '''def draw(df):
    """森林图：效应量 + 95% 区间 + 无效线。

    ★ 森林图的价值全在那条**无效线**（OR=1 / HR=1 / 差值=0）上 ——
      没有它，读者只能自己拿眼睛比高低。这里把无效线画成竖虚线，
      并把跨过无效线的行标成空心（= 不显著）。
      这三件事缺一件，图就只是"横过来的误差棒"。
    """
    df, n_before, n_after = clean(df, [{y!r}, {lo!r}, {hi!r}])
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    labels = [str(v) for v in df[{x!r}]]
    ypos = list(range(len(df)))
    est = df[{y!r}].values
    lo = df[{lo!r}].values
    hi = df[{hi!r}].values
    null = 1.0 if (float(lo.min()) > 0 and float(est.min()) > 0) else 0.0
    # ★★ 2026-10-26：无效线从 `axvline` 改成**有界线段**。
    #   `axvline` 横跨整个轴高 —— 包括我为此处图例留出来的那条空带，
    #   于是虚线从图例文字中间穿过（实测 `9 cross the null line` 被戳中，
    #   collision 报 text-stroke 被路径穿过）。
    #   无效线的语义本来就是「把数据行分成跨线/不跨线两组」，
    #   所以它只需要覆盖**有数据的那些行**，覆盖到图例是多余的。
    #   这里用 plot 画一条从第一行到最后一行的线段，长度正好是数据范围。
    ax.plot([null, null], [-0.55, len(df) - 0.45], color="0.45", lw=0.7,
            ls="--", zorder=0)
    for i in ypos:
        crosses = (lo[i] <= null <= hi[i])
        ax.plot([lo[i], hi[i]], [i, i], color="0.35", lw=0.8,
                solid_capstyle="butt", zorder=2)
        ax.plot([est[i]], [i], marker="o", ms=4.0,
                markerfacecolor=("white" if crosses else PALETTE[1]),
                markeredgecolor=PALETTE[1], markeredgewidth=1.0, zorder=3)
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    ax.set_ylim(-0.7, len(df) - 0.3)
    ax.invert_yaxis()
    ax.set_xlabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    n_ns = int(sum(1 for i in ypos if lo[i] <= null <= hi[i]))
    # ★★ 2026-10-26 图例位置，两版都不对，第三版才对：
    #
    #   ① `loc="lower right"`（原写法）—— 压在最后几行的置信区间上。
    #      森林图的区间线铺满大部分轴宽，轴内**任何**角落都可能被一条
    #      恰巧很长的区间穿过（实测 S9/S10 的线横穿图例文字）。
    #   ② 改用 figure 级图例放到轴外 —— collision 过了，但
    #      `text_containment` 变成 **NOT AUDITABLE**：森林图只画线和点，
    #      轴内唯一带填充的文字就是图例自己；图例一搬出去，轴内既没有
    #      填充块也没有字，那道门就无从判起（没查 ≠ 通过）。
    #   ③ 现在这版：图例留在**轴内**，但先用 ylim 在底部**腾出一条空带**，
    #      图例落在空带里。这样既不压任何区间线，轴内也仍然有字可查。
    #      留白是版面预算，不是妥协 —— 89mm 单栏图里图例压数据比少画一行槽位更糟。
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    # ★★ 2026-10-26 给图例腾空带的**方向**要小心。
    #
    #   这里是 `invert_yaxis()`：视觉上「最下面」对应的是 ylim 的**上界**，
    #   不是下界。第一版我把空带加在下界（`-0.7 - …`），结果空带出现在
    #   视觉最上方，图例（`loc="lower left"`）落回了最后一行 S10 的位置，
    #   照旧被 S10 的区间线穿过去 —— 实测就是这么翻车的。
    #   所以空带必须加在**上界**：`len(df) - 0.3` 往上再留 1.8 行。
    _rows_extra = max(1.8, 0.16 * max(1, len(df)))
    ax.set_ylim(-0.7, len(df) - 0.3 + _rows_extra)
    ax.invert_yaxis()
    ax.set_xlabel({ylabel!r})
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    ax.legend(handles=[
        matplotlib.patches.Patch(facecolor=PALETTE[1], edgecolor="none",
                                 label="%d significant" % (len(df) - n_ns)),
        matplotlib.patches.Patch(facecolor="white", edgecolor=PALETTE[1],
                                 linewidth=0.8,
                                 label="%d cross the null line" % n_ns)],
        frameon=False, loc="lower left", fontsize=6, ncol=2,
        handlelength=1.4, borderaxespad=0.2)
    # 空带上没有任何数据，但 y 轴刻度会跟着多出一个空槽 —— 把刻度限死在
    # 真实行号上，避免出现一个没有标签的刻度位置。
    ax.set_yticks(ypos)
    ax.set_yticklabels(labels)
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; null line at %g; "
          "%d/%d intervals cross it" % (n_before, n_after, null, n_ns, len(df)),
          file=sys.stderr)
    return fig
''',
    "matrix": '''def draw(df):
    """矩形热图：长表透视成矩阵，画**原值**（不是相关系数）。

    ★ 和 heatmap 的分工：heatmap 画的是「连续列之间的相关矩阵」，
      矩阵永远是方阵、值域固定 [-1,1]；matrix 画的是「两个分类维度
      交叉出来的原值」，形状任意、值域随数据走，所以配的色标也不同
      （连续渐变 + 独立 colorbar，不发散）。
    """
    df, n_before, n_after = clean(df, [{y!r}])
    import numpy as np
    wide = df.pivot_table(index={group!r}, columns={x!r}, values={y!r},
                          aggfunc="mean")
    if wide.size == 0:
        raise SystemExit("透视后是空矩阵（index/columns 有重叠取值？）")
    fig, ax = plt.subplots(figsize=(WIDTH_MM / 25.4, HEIGHT_MM / 25.4))
    im = ax.imshow(wide.values, cmap="viridis", aspect="auto")
    # ★★ 2026-10-26 修刻度标签：原先是 `set_xticklabels(...) + rotate_ticks(45)`。
    #   实测 10 个 `2024-01-03` 这样的日期旋转 45° 后**全部互相压住**
    #   （collision 报 10 处重叠，渲染图上是一排斜杠叠在一起的墨块）。
    #   旋转不解决问题：审计按**轴对齐包围盒**量，转得越斜盒子越宽
    #   （autofmt_dates 的 docstring 里记过这件事，这是同一个坑的第三次）。
    #
    #   ★ 也不能靠"截字符"：第一版我按 7 字截断，结果 10 个标签全变成
    #     `2024-0…` —— 既分辨不出来（十列一模一样），又照样重叠。
    #     截断只有对**分类名**才有意义；日期必须**解析后换格式**。
    #   所以：先试日期解析，成功就印 `%m-%d`（1 月的数据不会与年份混淆，
    #   年份交给标题/轴标题交代）；解析不成功才退回按长度截断。
    _cols_raw = [str(c) for c in wide.columns]
    _cols_dt = pd.to_datetime(pd.Series(_cols_raw), errors="coerce")
    if _cols_dt.notna().all():
        _cols_short = [d.strftime("%m-%d") for d in _cols_dt]
        _want_cap = 5
    else:
        _want_cap = 10 if len(_cols_raw) <= 8 else 7
        _cols_short = [(_c if len(_c) <= _want_cap else _c[:_want_cap - 1] + "…")
                       for _c in _cols_raw]
    # 标签太密就缩字号/隔列显示 —— 但绝不再旋转
    _dense = len(_cols_short) > 8
    ax.set_xticks(range(len(wide.columns)))
    ax.set_xticklabels(_cols_short)
    if _dense:
        ax.tick_params(axis="x", labelsize=6)
    ax.set_yticks(range(len(wide.index)))
    ax.set_yticklabels([str(i) for i in wide.index])
    # ★ 格内数字的前景色按**色标中位数**分界，不写死一个绝对值。
    #   viridis 是暗→亮的单调色标，按归一化位置决定黑白才两边都读得清。
    vmin = float(np.nanmin(wide.values))
    vmax = float(np.nanmax(wide.values))
    mid = (vmin + vmax) / 2.0
    for i in range(wide.shape[0]):
        for j in range(wide.shape[1]):
            v = wide.values[i, j]
            if v == v:
                ax.text(j, i, "%.3g" % v, ha="center", va="center", fontsize=6,
                        color="white" if v < mid else "black")
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.ax.tick_params(labelsize=7)
    # ★ colorbar 的标题用**被透视的那个值列**（就是 ylabel）。
    #   上一版把 {ylabel!r} 同时给了 y 轴和 colorbar，渲染图上左右各印一个
    #   "biomass"，读者会误以为是两个不同的量。这里只在**没有被更具体
    #   标题占用**时才复用列名，否则退回通用词 "value"。
    cb.set_label({ylabel!r} or "value", fontsize=8)
    if {title!r}:
        ax.set_title({title!r}, loc="left")
    fig.tight_layout()
    print("rows %d -> %d after numeric coercion; matrix %dx%d, values %.4g..%.4g"
          % (n_before, n_after, wide.shape[0], wide.shape[1],
             float(np.nanmin(wide.values)), float(np.nanmax(wide.values))),
          file=sys.stderr)
    return fig
''',
}


def plan_from_shape(table: str, meta: dict, cols: list[Column], *,
                    kind: str = "", x: str = "", y: str = "", group: str = "",
                    title: str = "", stem: str = "",
                    width_mm: float = COL_WIDTH_MM) -> dict:
    """把"建议"落成一份**可直接执行**的 plan。

    没指定 `kind` 就用第一条建议；建议为空 ⇒ 抛错并说清为什么（不许瞎画）。
    """
    sug = recommend(cols)
    if not kind:
        if not sug:
            raise ValueError(
                "这张表里没有可画的组合：" + "；".join(
                    f"{c.name}={c.kind}" for c in cols)
                + "。至少要有 1 个连续列，或 1 个分类列 + 1 个连续列。")
        kind = sug[0].kind
    if kind not in CHART_KINDS:
        raise ValueError(f"不认识的图型：{kind}")
    pick = next((s for s in sug if s.kind == kind), None)
    nums = [c for c in cols if c.is_numeric]
    cats = [c for c in cols if c.is_category]
    times = [c for c in cols if c.is_time]
    x = x or (pick.x if pick else "")
    y = y or (pick.y if pick else "")
    group = group or (pick.group if pick else "")
    if not x:
        # ★ 兜底顺序要看**这个图型的 x 是什么语义**，不能一律"时间列优先"。
        #   实测踩过：hist 的 x 是"被统计分布的那一列数值"，可是通用兜底
        #   把 `date`（时间列）填了进去 —— 脚本拿 date 去做数值清洗，
        #   全列非数值 ⇒ 清洗后 0 行 ⇒ **脚本报错、没有 PDF**。
        #   在 clean_num 之前这个缺陷是隐形的：旧 clean() 会静默画一张空直方图，
        #   退出码 0、PDF 有内容（只是空的），六道门也看不出"没画东西"。
        #   所以兜底必须按图型分派：直方图只可能拿数值列。
        if kind == "hist":
            x = nums[0].name if nums else ""
        else:
            x = (times or cats or nums or [None])[0].name if (times or cats or nums) else ""
    if not y:
        y = nums[0].name if nums else ""
    # 有分类列就默认按它分组 —— 时间序列尤其必须：
    # 不分组的折线会把两个组的点串成一条来回折返的线（实测踩过）。
    if not group:
        group = cats[0].name if cats else ""
    return {
        "kind": kind, "table": str(table), "x": x, "y": y, "group": group,
        "title": title or f"{y} by {x}" if (x and y) else (title or ""),
        "xlabel": x, "ylabel": y,
        "width_mm": width_mm,
        "height_mm": 62.0 if width_mm >= COL_WIDTH_MM else 58.0,
        "out_dir": ".", "stem": stem or "figure",
        "fit": True,
        # 这三样**就是脚本实际写的东西**（见 `_EPILOGUE.save()`）。
        # 契约的 formats 必须与之一致 —— 少写一项 `FigureContract.validate()`
        # 会报「导出清单里没有位图」，图本身全绿而总判仍是 FIX BEFORE DELIVERY。
        "formats": ["pdf", "svg", "png", "tiff"],
        "columns": [c.to_dict() for c in cols],
        "n_rows": meta.get("n_rows", 0),
        "why": pick.why if pick else "",
    }


# ════════════════════════════════════════════════════════════════
# 四、真的跑一遍
# ════════════════════════════════════════════════════════════════

@dataclass
class RunResult:
    ok: bool = False
    script: str = ""
    pdf: str = ""
    png: str = ""
    stdout: str = ""
    stderr: str = ""
    returncode: int = -1
    seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "script": self.script, "pdf": self.pdf,
                "png": self.png, "returncode": self.returncode,
                "seconds": round(self.seconds, 2),
                "stdout": self.stdout[-4000:], "stderr": self.stderr[-4000:],
                "error": self.error}


def run_script(script_path: Path, table: str, *,
               python_exe: str, workdir: Path,
               timeout: float = 300.0) -> RunResult:
    """起一个子进程跑绘图脚本，**把 stdout 里那两个路径收回来**。

    判据只认**文件真的存在**，不认退出码为 0 —— 脚本 `print` 了路径却
    没写盘（比如 `save()` 被改坏）时，退出码照样是 0。
    """
    t0 = time.time()
    res = RunResult(script=str(script_path))
    # ★ 脚本路径必须**绝对化**再交给带 cwd 的 subprocess —— 相对路径会被解析两次
    #   （一次按 cwd、一次按进程 cwd），实测报出
    #   `runs/_an/w1/runs/_an/w1/plot.py` 这种叠了一倍的假路径。
    #   数据表路径由调用方传绝对路径（见 `analyze()`）。
    script_abs = str(Path(script_path).resolve())
    res.script = script_abs
    try:
        p = subprocess.run([python_exe, script_abs, str(table)],
                           capture_output=True, cwd=str(workdir), timeout=timeout)
    except subprocess.TimeoutExpired:
        res.seconds = time.time() - t0
        res.error = f"绘图超时（>{timeout:.0f}s）"
        return res
    except Exception as e:                                      # noqa: BLE001
        res.seconds = time.time() - t0
        res.error = f"绘图起不来：{type(e).__name__}: {e}"
        return res
    res.seconds = time.time() - t0
    res.returncode = p.returncode
    res.stdout = (p.stdout or b"").decode("utf-8", errors="replace")
    res.stderr = (p.stderr or b"").decode("utf-8", errors="replace")
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    for ln in lines:
        low = ln.lower()
        if low.endswith(".pdf"):
            res.pdf = ln
        elif low.endswith(".png"):
            res.png = ln
    # ★ 脚本 print 出来的是**相对路径**（相对于它自己的 cwd，也就是 `workdir`），
    #   而本进程的 cwd 是别处。直接 `Path(res.pdf).exists()` 会判假。
    #   注意不能用 `.resolve()` 硬转：那按**本进程** cwd 解，同样错。
    wd = Path(workdir)
    if res.pdf and not Path(res.pdf).is_absolute():
        res.pdf = str((wd / res.pdf).resolve())
    if res.png and not Path(res.png).is_absolute():
        res.png = str((wd / res.png).resolve())
    res.ok = bool(res.pdf) and Path(res.pdf).exists()
    if not res.ok:
        res.error = ("脚本退出了但没有产出 PDF。"
                     + (f" stderr 末尾：{res.stderr.strip().splitlines()[-1]}"
                        if res.stderr.strip() else " stderr 是空的。"))
    return res


def diag(python_exe: str | None = None) -> dict:
    """这套装置能不能用。与 `figure.FigureAuditor.diag()` 同构。"""
    from .figure import _NEEDED_MODULES as FIG_MODS
    from .figure import find_figure_python
    exe = find_figure_python(python_exe, extra=tuple())
    need = ("pandas", "matplotlib") + tuple(FIG_MODS)
    notes: list[str] = []
    if not exe:
        notes.append("找不到能跑绘图+审计的解释器（需要 pandas / matplotlib / PyMuPDF）。")
    else:
        code = ("import importlib.util, json, sys\n"
                "print(json.dumps([m for m in %r "
                "if importlib.util.find_spec(m) is None]))\n" % (list(need),))
        try:
            r = subprocess.run([exe, "-c", code], capture_output=True, timeout=20)
            miss = json.loads((r.stdout or b"[]").decode() or "[]")
            if miss:
                notes.append(f"{exe} 里缺：{'、'.join(miss)}")
        except Exception as e:                                  # noqa: BLE001
            notes.append(f"探测 {exe} 失败：{type(e).__name__}: {e}")
    return {"python": exe or "", "needed": list(need), "notes": notes,
            "ready": bool(exe) and not notes}


def new_workdir(base: str | Path, tag: str = "plot") -> Path:
    """给一次成图开一个目录（不复用旧目录，免得旧产物被当成本次结果）。"""
    base = Path(base or Path.cwd())
    d = base / f"_{tag}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def analyze(path: str | Path, *, kind: str = "", x: str = "", y: str = "",
            group: str = "", title: str = "", width_mm: float = COL_WIDTH_MM,
            lo: str = "", hi: str = "", ys: list[str] | tuple[str, ...] | str = (),
            out_dir: str | Path | None = None, emit: bool = True,
            python_exe: str | None = None, timeout: float = 300.0) -> dict:
    """一条龙：读表 → 测形状 → 选图型 → 生成脚本 → 跑 → 回收产物。

    **不跑审计** —— 审计由 `figure.FigureAuditor` 负责，调用方（工具层）
    把这里产出的 `pdf` / `script` 传给 `audit_figure`。分开的理由与
    `deck` 那边一致：画和审是两条独立判据，合成一个函数就会长出
    "画成功了 ⇒ 以为审计也过了"的错觉。

    ★ 2026-10-26 补 `lo` / `hi` / `ys` 三个入参。原先 `_plan_args()` 已经
      支持这三个键，**但 `analyze()` 的签名里没有，所以调用方根本传不进来** ——
      三个新图型（band 的区间、forest 的置信区间、stacked_area 的成分列）
      因此拿不到自己的必需列，只能靠 plan 手写。
      这是"下游支持了、上游没开口"的典型：功能看着做完了，
      实际没有任何一条调用路径能用上它。
      实测暴露方式就是这一句：`TypeError: analyze() got an unexpected
      keyword argument 'lo'` —— 报错很清楚，缺的是一次真正的端到端调用。
    """
    p = Path(path)
    rows, meta = read_table(p)
    cols = profile_table(rows)
    if not cols:
        raise TableError(f"{p.name} 里一列都没测出来。")
    plan = plan_from_shape(p.name, meta, cols, kind=kind, x=x, y=y,
                           group=group, title=title, width_mm=width_mm)
    # 三个专属列原样进 plan（`_plan_args()` 会做必需列校验）
    if lo:
        plan["lo"] = str(lo)
    if hi:
        plan["hi"] = str(hi)
    if ys:
        plan["ys"] = ([str(c) for c in ys]
                      if isinstance(ys, (list, tuple))
                      else [c.strip() for c in str(ys).split(",") if c.strip()])
    work = Path(out_dir) if out_dir else new_workdir(p.parent)
    # ★ 显式给的 out_dir 也要建 —— `new_workdir()` 自己建，但传进来的路径不会。
    #   不建就是个 `FileNotFoundError`，而且报的位置（write_text）离真因（目录不存在）很远。
    work.mkdir(parents=True, exist_ok=True)
    # ★★ 而且必须**绝对化**再放进 plan。否则脚本按它自己的 cwd 写相对路径，
    #   而 `run_script()` 又要按 workdir 把它拼回来 —— 于是同一个前缀拼两遍，
    #   得到 `runs/_an/w1/runs/_an/w1/figure.pdf`，文件明明在却判不存在。
    #   （绝对路径进 plan 后，脚本 print 的就是绝对路径，拼回那步自然不再触发。）
    work = work.resolve()
    plan["out_dir"] = str(work)
    plan["stem"] = "figure"
    src = build_script(plan)
    script_path = work / "plot.py"
    script_path.write_text(src, encoding="utf-8")
    out: dict[str, Any] = {
        "table": str(p), "meta": meta,
        "columns": [c.to_dict() for c in cols],
        "suggestions": [s.to_dict() for s in recommend(cols)],
        "plan": {k: v for k, v in plan.items() if k != "columns"},
        "script": str(script_path), "workdir": str(work),
        "ran": False,
    }
    if not emit:
        return out
    d = diag(python_exe)
    if not d["ready"]:
        out["error"] = "绘图装置不全：" + "；".join(d["notes"])
        out["diag"] = d
        return out
    r = run_script(script_path, str(p.resolve()), python_exe=d["python"],
                   workdir=work, timeout=timeout)
    out["ran"] = True
    out["run"] = r.to_dict()
    out["diag"] = d
    if not r.ok:
        out["error"] = r.error
    return out


#: 图型 → **推断职责**。注意右列是「在论证里干什么」，不是「画成什么样」——
#: 与 `figure.Panel.role` 的要求一致（`role` 写"趋势"而不是"折线"）。
_ROLE_OF: dict[str, tuple[str, str]] = {
    # kind: (role, claim 模板)
    "time_series": ("趋势", "「{y}」随「{x}」如何变化"),
    "line":        ("趋势", "「{y}」随「{x}」的走向"),
    "scatter":     ("变量间关系", "「{x}」与「{y}」是否同向变化"),
    "box":         ("组间分布对比", "「{y}」在「{x}」各组的分布是否不同"),
    "violin":      ("组间分布对比", "「{y}」在「{x}」各组分布的形状差异"),
    "bar":         ("组间汇总对比", "「{y}」在「{x}」各组的均值差异"),
    "paired":      ("配对增量", "同一单位在两种条件下的变化量"),
    "heatmap":     ("矩阵结构化", "「{x}」与「{y}」之间的相关结构"),
    "hist":        ("单变量分布", "「{x}」自身的分布形状"),
}


def contract_for(plan: dict, *, figure_id: str = "Fig. 1",
                 journal: str = "") -> dict:
    """把 plan 变成一份**能直接喂给 `audit_figure`** 的五点契约。

    为什么由模块生成而不是让模型手填：契约里最容易填错的是
    `role`（推断职责）与 `formats`（导出清单）。前者填成"折线图"会被
    `validate()` 判成职责重复；后者漏掉位图会让六道门全绿而总判仍是
    FIX BEFORE DELIVERY —— 实测两次都踩在这上面。
    """
    kind = str(plan.get("kind") or "")
    role_t, claim_t = _ROLE_OF.get(kind, ("数据展示", "「{y}」在「{x}」上的表现"))
    x, y = str(plan.get("x") or ""), str(plan.get("y") or "")
    # ★ `spread` 必须与**实际画法**一致，不能"有分组就填 s.e.m."：
    #   box/violin 画的是分布（四分位/密度），本身没有误差条；
    #   只有 bar 真的画了 `yerr=sem`。声明与画法不符就是图注撒谎。
    has_spread = kind == "bar"
    return {
        "figure_id": figure_id,
        "title": str(plan.get("title") or f"{y} by {x}"),
        "core_conclusion": str(plan.get("why") or f"{y} 与 {x} 的关系"),
        "panels": [{
            "letter": "a",
            "role": role_t,
            "claim": claim_t.format(x=x, y=y),
            "center": "mean" if kind == "bar" else "raw",
            "spread": "s.e.m." if has_spread else "none",
            "unit": "replicate" if has_spread else "",
        }],
        "archetype": "quantitative grid",
        "backend": "python",
        "width_mm": float(plan.get("width_mm") or COL_WIDTH_MM),
        "height_mm": float(plan.get("height_mm") or 62.0),
        "journal": journal,
        "formats": list(plan.get("formats") or ["pdf", "svg", "png", "tiff"]),
    }


def legend_for(contract: dict, *, meta: dict | None = None,
               spread: str = "", center: str = "") -> str:
    """按图注规范生成一份图注草稿。

    规范要求四段（`figure.audit_legend` 逐条核）：
      `Fig. N | **加粗名词短语**` + 分面说明 + 统计口径 + 源数据套语。
    这里只能生成**草稿** —— "这张图说明了什么"那一句得由看过图的人补，
    所以 `closing` 留空并在末尾标出来，不硬编一个假结论。
    """
    fig_id = str(contract.get("figure_id") or "Fig. 1")
    title = str(contract.get("title") or "").strip() or "Untitled figure"
    panels = contract.get("panels") or [{}]
    p = panels[0] if panels else {}
    letter = str(p.get("letter") or "a")
    claim = str(p.get("claim") or "")
    bits = [f"**{title}**"]
    if claim:
        bits.append(f" **{letter}** {claim}。")
    stat = []
    # ★ `center`/`spread` 在契约里是**机器读的标记**（raw / none / s.e.m.），
    #   直接印进图注会变成「中心量 raw ± none」这种读者看不懂的句子。
    #   所以先翻译成人话，翻不出来就不写 —— 图注宁缺，不可乱写。
    _CENTER_WORDS = {"mean": "均值", "median": "中位数"}
    _SPREAD_WORDS = {"s.d.": "标准差 s.d.", "sd": "标准差 s.d.",
                     "s.e.m.": "标准误 s.e.m.", "sem": "标准误 s.e.m.",
                     "95% ci": "95% 置信区间", "ci": "95% 置信区间"}
    cw = _CENTER_WORDS.get(str(center).strip().lower(), "")
    sw = _SPREAD_WORDS.get(str(spread).strip().lower(), "")
    if cw and sw:
        stat.append(f" {cw} ± {sw}，")
    elif cw:
        stat.append(f" 中心量取{cw}，")
    elif sw:
        stat.append(f" 离散用{sw}，")
    if stat:
        bits.extend(stat)
        bits.append("n = 见图中标注。")
    if meta:
        n = meta.get("n_rows")
        if n:
            bits.append(f" 源数据 n = {n}，由荣珂数据分析成图模块生成。")
    # ★ 图注**必须写检验方法**：`audit_legend` 的 LEGEND-STATS-TEST 会在
    #   "有定量比较"时要求它出现。这里不能替用户编一个检验 ——
    #   所以给一句明确的占位，让模型/人必须填，而不是让它静默不合格。
    bits.append(" 组间比较用 ___（填实际检验：Welch t-test / ANOVA / "
                "Mann-Whitney U…；若本图不含检验请改写为描述性说明）。")
    bits.append(" （此处补一句这张图支持的核心结论 —— 草稿不代填。）")
    draft = f"{fig_id} | " + "".join(bits)

    # ⑦ 插件图注模板：只对**它自己声明的那个期刊**生效。
    # ★ 这是一个「措辞覆盖」钩子，不是「重写整条图注」——
    #   后者会让插件绕开图注规范的四段要求。所以模板只拿到
    #   `{fig_id} {title} {letter} {claim}` 这几个槽，拼出来的东西
    #   仍然要过 `figure.audit_legend`（它会核四段是否齐）。
    tpl = LEGEND_TEMPLATES.get(_journal_key(contract))
    if tpl:
        try:
            replaced = str(tpl).format(
                fig_id=fig_id, title=title, letter=letter, claim=claim,
                n=(meta or {}).get("n_rows", ""),
                center=cw or "", spread=sw or "")
            # 只有替换成功且非空才采用；插件写坏模板时退回内置草稿，
            # 绝不给用户一条空图注。
            if replaced.strip():
                draft = replaced
        except (KeyError, IndexError, ValueError):
            pass          # 模板槽写错 → 静默退回内置草稿（下方测试会盯住）
    return draft


def _journal_key(contract: dict) -> str:
    """期刊标识归一：小写去空白。插件按这个键登记模板。"""
    return str(contract.get("journal") or "").strip().lower()


#: 插件登记的图注模板：`{journal: template}`。模板用 `str.format` 的槽：
#: `{fig_id} {title} {letter} {claim} {n} {center} {spread}`。
#: 由 `plugins.load_plugins()` 填入。**只覆盖措辞，不豁免图注审计**。
LEGEND_TEMPLATES: dict[str, str] = {}


__all__ = [
    "COL_WIDTH_MM", "FULL_WIDTH_MM", "FONT_SIZES", "CHART_KINDS",
    "Column", "Suggestion", "RunResult", "TableError",
    "read_table", "profile_table", "recommend", "plan_from_shape",
    "build_script", "run_script", "analyze", "diag", "new_workdir",
    "contract_for", "legend_for",
]
