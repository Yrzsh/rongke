# -*- coding: utf-8 -*-
"""荣珂内置工具包（Schema、工具工厂与 `install_builtins` 装配实现）。

从 `rongke/loop.py` 拆出，使主循环专注会话编排与 Plan→Act→Observe→Verify，
同时由 `loop.py` 顶层完整重导出全部 `*_SCHEMA` 与 `_make_*` 符号以保持向后兼容。
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

from .citation import VERIFY_SCHEMA, verify_citation
from .stages import read_skill_text
from .subagent import SUBAGENT_SCHEMA, SubagentRunner
from .tools import Tool
from .workflow_prompts import BLOCKER_SCHEMA, DELIVER_SCHEMA


def _load_skill(name: str, file: str = "") -> str:
    """load_skill 的执行体：装载失败要**抛错**，而不是返回一句提示。

    因为注册表把"返回字符串"记为成功调用（ok=True），门就会误以为技能装载过了 ——
    一个打错名字的 load_skill 不该让证据门放行。抛错的提示文字一样会回灌给模型
    （走 _observation 的 [工具执行失败] 分支），不损失可读性。

    `file` 为空 ⇒ 入口文档（`SKILL.md`）；给了相对路径 ⇒ 该技能目录下的参考文件。
    技能自己的 `SKILL.md` 写着"`references/` 按需深读"，原来却没有工具能读 ——
    模型于是去猜路径并连着报错。这个参数把那句指示**变成可执行的**。
    """
    text = read_skill_text(name, file=file)
    if text.startswith("⛔"):
        raise RuntimeError(text)
    return text


AUDIT_FIGURE_SCHEMA = {
    "type": "object",
    "properties": {
        "contract": {
            "type": "object",
            "description": (
                "五点图面契约（**开画之前就要定死**，这里一并提交）：\n"
                "  figure_id: 'Fig. 1'\n"
                "  title: 图注总题（加粗名词短语，不含数字）\n"
                "  core_conclusion: 这张图要捍卫的那一句话\n"
                "  archetype: quantitative grid | schematic-led composite | "
                "image plate + quant | asymmetric mixed-modality\n"
                "  backend: python | r | origin | other（**一经选定即独占**）\n"
                "    ⚠️ **怎么选：看审计需求，不看引擎喜好**（已定案，别逐图纠结）——\n"
                "    满足任一条即**必须** python/r：① 分面数 ≥ 2（对齐门要「分面几何"
                "清单」，只有 python/r 量得出）② 面板里画了数据（center/spread 非空，"
                "含逐点原始值）③ 声明了目标期刊。\n"
                "    仅「单分面 + 面板不画数据 + 不进投稿包」的示意/装置/流程图"
                "可走 origin/other。\n"
                "    选错 = 契约不成立，本工具会在**画之前**把契约打回。\n"
                "  width_mm / height_mm: 最终物理尺寸（单栏 89 / 双栏 183 mm）\n"
                "  journal / formats: 如 'nature' / ['svg','pdf','tiff','png']\n"
                "  panels: [{letter:'a', role:'推断职责（不是画法）', claim:'独家回答什么问题', "
                "center:'mean', spread:'s.d.', unit:'batches'}] —— "
                "**每个分面的 role 必须互不相同**，重复即失败\n"
                "  engine_artifacts: 每个产物由哪个引擎产出（一种以上即失败）\n"
                "  exclusions: [{what, before, after, rule, reason}] 被排除的数据\n"
                "  canonical_names: 需保留原拼写的名字（XGBoost / GPT-5.2 …）"),
        },
        "pdf": {"type": "string",
                "description": "导出的 PDF（相对工作区）。字形下限与碰撞审计都按它量。"},
        "source": {"type": "string",
                   "description": "绘图源码（相对工作区）。后端是 python/r 时应当给；"
                                  "第三方渲染器出图时可以不给。"},
        "layout_manifest": {"type": "string",
                            "description": "分面几何清单（相对工作区）。多分面图必给，"
                                           "否则对齐门判 NOT AUDITABLE（**不等于通过**）。"},
        "legend": {"type": "string", "description": "图注文本（直接给全文）。"},
        "legend_path": {"type": "string",
                        "description": "或给图注文件路径（相对工作区）。"},
    },
    "required": ["contract"],
}


def _plot_kind_list() -> str:
    """列出当前可用的图型 —— **含插件注册的**。

    ★ 为什么必须动态取：这段文字是模型唯一能看到的"可以画哪些图"。
      `PLOT_DATA_SCHEMA` 是模块级常量，在 import 时就冻住了；
      而插件在 `Rongke.__init__` 里才加载，比 import 晚。
      如果描述在这里写死内置那 9 种，插件图型就**对模型不可见** ——
      能注册、能画、能过审计，但模型从来不知道它存在，永远不会填。
      那正是"扩展了但等于没扩展"的形态。
    """
    try:
        from . import analysis as _an
        kinds = sorted(_an.CHART_KINDS)
    except Exception:                                          # noqa: BLE001
        kinds = []
    if not kinds:
        kinds = ["time_series", "line", "scatter", "box", "violin",
                 "bar", "paired", "heatmap", "hist"]
    return " / ".join(kinds)


PLOT_DATA_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "description": "数据表路径（相对工作区或绝对）。支持 .csv / .tsv / .xlsx / .json",
        },
        "kind": {
            "type": "string",
            "description": ("图型。**留空是最常见也最正确的选择** —— 模块会按列的形状"
                            "自动选，并把「为什么选它、为什么不选别的」写进 suggestions。"
                            "只有你已经确定要画什么时才填。可用图型见调用时的返回"
                            "（含已装插件注册的）。内置的有："
                            "time_series / line / scatter / box / violin / bar / "
                            "paired / heatmap / hist"),
        },
        "x": {"type": "string", "description": "x 轴列名（留空则用建议里的）"},
        "y": {"type": "string", "description": "y 轴列名（留空则用建议里的）"},
        "group": {"type": "string",
                  "description": ("分组列名（分组的柱/箱/小提琴，以及时间序列的分线）。"
                                  "留空时若有分类列会自动用第一个 —— 时间序列**务必**"
                                  "按组分层，否则各组会被串成一条来回折返的线。")},
        "title": {"type": "string", "description": "图题（留空则自动生成为「y by x」）"},
        "width_mm": {"type": "number",
                     "description": "最终物理宽度 mm。默认 89（单栏），双栏用 183"},
        "journal": {"type": "string", "description": "目标期刊（如 nature），影响图注字数上限"},
    },
    "required": ["table"],
}


#: 原生 SVG 出图（2026-11-05 加）。参数刻意比 `plot_data` **少**：
#: 这条路线不产投稿级产物，也就不该有 journal / 物理精度这些假装要送审的旋钮。
#: 2026-11-11 起图型清单从 `svgplot.SUPPORTED_KINDS` 单源生成 —— 往资产库
#: 加一种图型时，工具描述自动跟上，不再有两处手抄清单会静默漂移。
from .svgplot import SUPPORTED_KINDS as _PLOT_SVG_KINDS       # noqa: E402

PLOT_SVG_SCHEMA = {
    "type": "object",
    "properties": {
        "table": {
            "type": "string",
            "description": ("数据表路径（相对工作区或绝对）。CSV / TSV / JSON 用标准库直读；"
                            ".xlsx 需要 pandas，没装会如实报错而不是画空图。"),
        },
        "kind": {
            "type": "string",
            "description": ("图型，留空自动按列形状选。原生后端支持："
                            + " / ".join(_PLOT_SVG_KINDS)
                            + "。errorbar / errorband 的误差由给出的观测值算"
                              "（同 x 重复的样本标准差 / 相邻 3 点滑动窗口），不编造；"
                              "stacked_bar 画分组累计。不支持的图型会**明确报错**，"
                              "不会偷换成别的图。"),
        },
        "x": {"type": "string", "description": "x 轴列名（留空用建议的）"},
        "y": {"type": "string", "description": "数值列（留空用建议的）"},
        "group": {"type": "string", "description": "分组列名（多系列折线 / 分组着色）"},
        "title": {"type": "string", "description": "图题（中文也可以，字体栈已按中文回退）"},
        "width_mm": {"type": "number",
                     "description": "图宽 mm，默认 89（单栏）；只决定 viewBox，不承诺印刷尺寸"},
        "out": {"type": "string",
                "description": "输出 .svg 路径（相对工作区或绝对）。留空 = 工作区下 figure.svg"},
    },
    "required": ["table"],
}


AUDIT_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要体检的 .pptx 路径（相对工作区或绝对路径都行）。",
        },
        "require_notes": {
            "type": "boolean",
            "description": (
                "**用户是否明确要了讲稿/备注**（原话里有『讲稿』『备注』『演讲词』"
                "『逐字稿』才填 true；只是『做个组会 PPT』填 false）。\n"
                "填 true 时，备注覆盖率不足会被**阻断**。\n"
                "⚠️ 这一项只是给你自己看的提示：内核在交付时会**从契约独立重判一次**，"
                "不采信这里的值（被审者不该给自己定标准）。"),
        },
        "deep_contrast": {
            "type": "boolean",
            "description": (
                "**是否额外跑一遍渲染像素级对比度校验**（默认 false）。\n"
                "⚠️ **慢**：本机实测 ~15 秒（一份 5 页 deck），而且**会调用 PowerPoint "
                "渲染**每一页。默认关闭时本工具仍是秒级。\n"
                "只在需要检查**『文字压在照片/渐变上』**这一类情形时开启 —— "
                "纯 XML 拿不到照片的亮度，那几处会被记成『量不到』(INFO "
                "contrast_unmeasurable)，只有渲染能看见它们。\n"
                "开启**不改变**任何既有判据与阈值，只追加一条像素口径的 "
                "WARN/INFO（**不阻断**交付）。\n"
                "留空或 false = 沿用配置 `deck.deep_contrast`（不是强制关掉）。"),
        },
    },
    "required": ["pptx"],
}


READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string",
                 "description": "工作区内的文件路径（相对工作区即可，例如「素材.md」）"},
        "max_chars": {"type": "integer",
                      "description": "最多返回多少字符（默认 20000）。超长会截断并如实说明。"},
    },
    "required": ["path"],
}


WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string",
                 "description": "工作区内的落点路径（相对工作区即可，例如「综述.md」"
                                "或「out/综述.md」）。中间目录会自动建。"},
        "text": {"type": "string",
                 "description": "要写入的**完整正文**。append=false 时它是全文（覆盖旧内容）；"
                                "append=true 时它是追加的一段。"},
        "append": {"type": "boolean",
                   "description": "true=追加到文件末尾（分段落盘用这个，避免一次要吐几万字）；"
                                  "false（默认）=整篇覆盖。"},
    },
    "required": ["path", "text"],
}


CONVERT_PDF_SCHEMA = {
    "type": "object",
    "properties": {
        "src": {"type": "string",
                "description": "要转的 PDF 路径（相对工作区即可，如 \"paper.pdf\"）。"},
        "out": {"type": "string",
                "description": "Markdown 落点（相对工作区，如 \"knowledge/paper.md\"）。"
                               "留空则只转不落盘（用于探路/看结构统计）。"},
        "use_ocr": {"type": "boolean",
                    "description": "是否开 OCR。**默认 false**。"
                                   "实测：21 页双栏论文关 OCR 24.4s、开 OCR 53.2s（慢 2.2 倍），"
                                   "而正文只多约 1.3%。⇒ 数字文本的期刊件**不要开**；"
                                   "只有扫描件（没有文字层）才开。"
                                   "拿不准就先关着转一次 —— 返回里会按每页字符数告诉你像不像扫描件。"},
        "images": {"type": "boolean",
                   "description": "是否导出图片（默认 false）。这份 Markdown 是给人读 / 给 Obsidian 的，"
                                  "默认不需要图；要把 PDF 里的图也落盘（Obsidian 里看图）才开"
                                  "（会慢约 7s/篇。kb 库只收 PDF，开不开与入库无关）。"},
    },
    "required": ["src"],
}

KB_TO_OBSIDIAN_SCHEMA = {
    "type": "object",
    "properties": {
        "src": {"type": "string", "description": "要转并写进 vault 的 PDF 路径。"},
        "title": {"type": "string",
                  "description": "笔记标题（也用作文件名）。留空则用 PDF 文件名。"},
        "authors": {"type": "array", "items": {"type": "string"},
                    "description": "作者列表（可留空 —— 空字段不会写进 frontmatter）。"},
        "year": {"type": "integer", "description": "年份（可留空）。"},
        "journal": {"type": "string", "description": "期刊名（可留空）。"},
        "doi": {"type": "string", "description": "DOI（可留空）。"},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "Obsidian 标签（可留空）。"},
        "folder": {"type": "string",
                   "description": "vault 内的子目录（如 \"文献\"）。留空则落在库根。"},
        "use_ocr": {"type": "boolean", "description": "同 convert_pdf，默认 false。"},
        "images": {"type": "boolean", "description": "同 convert_pdf，默认 false。"},
    },
    "required": ["src"],
}


LITERATURE_FETCH_SCHEMA = {
    "type": "object",
    "properties": {
        "dois": {"type": "array", "items": {"type": "string"},
                 "description": "要取全文的 DOI 列表（可带 `doi:` 前缀或 doi.org 外壳，会自动剥）。"
                                "一次别超过 20 个：这几个 API 都对突发请求限流。"},
        "out_dir": {"type": "string",
                    "description": "落盘目录（**相对工作区**，如 \"fulltext\"）。留空落在 "
                                   "`fulltext/`。落在这里的文件可直接喂给 kb_add/kb_ingest。"},
    },
    "required": ["dois"],
}

KB_COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "topics": {"type": "array", "items": {"type": "string"},
                   "description": "主题词列表（建议 5–8 个，对应大纲的章节或核心论点）。"
                                  "**中文请给三字以上的词** —— 两字词会被库的 trigram 索引"
                                  "静默丢弃、返回空，那不等于库里没有；本工具会把它们点名出来。"},
        "min_docs": {"type": "integer",
                     "description": "每个主题词至少要有几篇不同文献才算「够」。默认 3。"},
        "min_ok_ratio": {"type": "number",
                         "description": "达标主题词占比阈值（0–1）。默认 0.6。"
                                        "低于它就整体判「不够」。有任何硬缺口时一律判不够。"},
        "per_topic": {"type": "integer",
                      "description": "每个主题词检索几段。默认 8。调大更准但更慢。"},
    },
    "required": ["topics"],
}


def _make_literature_fetch(cfg) -> "Callable[..., dict]":
    """取全文的工具实现。落盘只在工作区内（围栏与自判双保险）。

    ★ 为什么这是内核自带的、而不是调用外部 skill 的脚本：
      荣珂没有 shell（实测 59 个工具里没有 exec/bash/子进程类），
      所以「让模型去跑 nature-downloader 的 batch_download.mjs」这条路
      **根本不存在**。要实现它就得给模型任意命令执行权 —— 那与这个仓
      的围栏设计方向相反，而且是把它做成产品时最不该开的口子。
      ⇒ 正确做法：把「合法取全文」这个动作本身做成一个**参数受限**的工具。
        能传的只有 DOI 列表与落点目录，**拼不出一条任意命令**。

    ★ 为什么 `mutating=True`：它往工作区写 pdf/xml 文件。
      围栏只查 mutating 工具的绝对路径参数，所以这里再自己判一遍
      （两处各写一遍是刻意的，见 hooks.py 的说明）。
    """
    def literature_fetch(dois, out_dir: str = "", **_ignored) -> dict:
        from . import litfetch

        if isinstance(dois, str):
            dois = [dois]
        if not isinstance(dois, (list, tuple)) or not dois:
            raise RuntimeError("dois 不能为空：给一个 DOI 列表，如 [\"10.1038/…\"]。")

        root = Path.cwd().resolve()
        raw = str(out_dir or "").strip() or "fulltext"
        p = Path(raw)
        if p.is_absolute():
            raise RuntimeError(
                f"⛔ out_dir 必须用**相对工作区**的路径（收到绝对路径 {raw}）。"
                f"\n   本任务工作区是 {root}。")
        dest = (root / p).resolve()
        if dest != root and root not in dest.parents:
            raise RuntimeError(f"⛔ out_dir 越过工作区：{raw}")

        rep = litfetch.fetch_many([str(d) for d in dois], dest)
        rep["out_dir"] = str(dest)
        if rep.get("ok"):
            rep["note"] = ("取全文**成功不等于已入库** —— 把每条的 `file` 交给 "
                           "kb_add / kb_ingest 才算进库。")
        return rep

    return literature_fetch


def _make_kb_coverage(root=None, *, registry=None) -> "Callable[..., dict]":
    """库够用性判定的工具实现：**发检索 + 算账**合在一个动作里。

    为什么不让模型自己循环调 `kb_search` 再自己数：
      让模型数"命中了几篇不同文献"，它数不准（会重复计、会漏计），
      而这一步的结论直接决定"去不去检索入库" —— 数错了就是白跑一轮或带着缺口开写。
      ⇒ 内核发请求、内核记账、内核下判语，模型只负责给主题词。

    实现要点：`kb_search` 是 workbench 反射进来的工具，本函数通过显式传入的
    `registry` 拿到它（**同一进程内直调**，不起子进程、不走 MCP）。
    """
    def kb_coverage(topics, min_docs: int = 3, min_ok_ratio: float = 0.6,
                    per_topic: int = 8, **_ignored) -> dict:
        from . import coverage

        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, (list, tuple)) or not topics:
            raise RuntimeError("topics 不能为空：给 5–8 个主题词。")

        reg = registry if registry is not None else (root if hasattr(root, "get") else None)
        # ★ `registry` 是 **`ToolRegistry` 对象**，不是 dict。
        #   第一版写成 `reg.get("kb_search")`/`reg["kb_search"]` —— ToolRegistry 上没有
        #   `get(key)` 这个方法签名（它的 `.get()` 收的是工具名并返回 `Tool`，
        #   但 `reg["..."]` 会抛 `TypeError: 'ToolRegistry' object is not iterable`），
        #   于是**每一次检索都在 except 里落地成"检索失败"**，底下好好的库一条也查不到。
        #   实测症状：五个主题词全报"检索失败"，而 `lib.search_paras` 直接调能返回 5 条。
        #   教训：反射拿到的东西先**打印它的类型**，别照着名字猜形状。
        _kb = reg.get("kb_search") if reg is not None else None
        if _kb is None:
            raise RuntimeError(
                "本任务没有挂载 kb_search（文献库不可用）。\n"
                "⇒ 这种情况**不能**判「库够用」：如实说明库里查不了，"
                "再问用户要不要换一条路（先建库 / 只按已给材料写）。")

        usable, short = coverage._usable_terms([str(t) for t in topics])
        rows: list[dict] = []
        for t in usable:
            try:
                # ★ 参数名是 `n`，**不是** `top_k`。
                #   workbench 的 kb_search 签名是 (query, n=8, doc_ids="", library="")。
                #   写成 top_k 会抛 TypeError，而它被上面的 kb_search 包装层
                #   catch 成"返回空串" —— 又是一个静默失败。
                #   **参数名必须照抄产出方。**
                raw = _kb.fn(query=t, n=int(per_topic))
                hits = []
                if isinstance(raw, dict):
                    hits = raw.get("hits") or raw.get("results") or []
                elif isinstance(raw, str):
                    # workbench 有时把 JSON 序列化成字符串返回；解析不了就如实记失败
                    import json as _json
                    try:
                        obj = _json.loads(raw)
                        hits = (obj.get("hits") or obj.get("results") or []
                                if isinstance(obj, dict) else [])
                    except Exception:
                        hits = []
                rows.append({"term": t, "ok": True, "hits": hits})
            except Exception as e:                      # noqa: BLE001
                rows.append({"term": t, "ok": False, "hits": [],
                             "error": f"{type(e).__name__}: {e}"})
        # 两字词没发检索，但必须出现在报告里 —— 静默丢弃会让模型
        # 得出"这章零文献"的假结论（见 coverage 模块文档 ①）。
        for t in short:
            rows.append({"term": t, "ok": False, "hits": [],
                         "error": ("该主题词是**中文两字词**，库的 trigram 索引会静默回空，"
                                   "本工具未发检索。换成三字以上或英文再判 —— "
                                   "它现在既不算「有」也不算「没有」。")})

        rep = coverage.assess(rows, min_docs=int(min_docs),
                              min_ok_ratio=float(min_ok_ratio))
        rep["judged_terms"] = len(usable)
        rep["next"] = coverage.next_action(rep)
        return rep

    return kb_coverage


def _make_kb_convert_tools(cfg=None) -> dict:
    """PDF → Markdown 的两个工具（`convert_pdf` / `kb_to_obsidian`）。

    ★ 为什么这两个必须挂 `core` 常驻，而不照 `_DECK_DOMAIN` 做成按需域：
    与 `_make_topic_tools` 同一条理由 —— 按需裁剪需要一个**硬判据**，
    而"这次要不要转 PDF"从契约里看不出来（任务是"帮我建知识库"还是
    "综述这篇稿子"都可能要转）。拿模糊判据裁工具，就是本仓记的
    「不打红面的判据」。**量不出来就不裁**。

    ⚠️ 与 `read_file` 同一条安全边界：围栏只管 `mutating` 工具，
    而这两个工具**要读工作区外的 PDF**（用户的 Zotero 库/下载目录）。
    ⇒ 入参 PDF 路径**允许绝对路径**（源文件本来就在工作区外，这是主路径）；
    而**输出**一律限制在工作区内（`out` 必须相对路径），
    理由同 kb_add：产物的落点必须可解释，不能让模型往任意位置写文件。
    """
    _bound_cfg = cfg

    def _out_path(out: str) -> Path:
        """把 `out` 解析成工作区内的绝对路径。**只收相对路径。**"""
        raw = str(out or "").strip()
        if not raw:
            raise RuntimeError("out 不能为空")
        p = Path(raw)
        if p.is_absolute():
            raise RuntimeError(
                f"⛔ out 必须用**相对工作区**的路径（收到绝对路径 {raw}）。"
                f"\n   理由：产物落点必须可解释 —— 本任务工作区是 {Path.cwd()}。")
        root = Path.cwd().resolve()
        full = (root / p).resolve()
        if full != root and root not in full.parents:
            raise RuntimeError(f"⛔ out 越过工作区：{raw}")
        return full

    def convert_pdf(src: str, out: str = "", use_ocr: bool = False,
                    images: bool = False) -> str:
        from .kb_convert import convert_pdf as _conv
        cfg = _bound_cfg
        py = getattr(getattr(cfg, "kb", None), "python", "") or "" if cfg else ""
        to = float(getattr(getattr(cfg, "kb", None), "timeout", 600) or 600) if cfg else 600.0

        raw = str(src or "").strip()
        if not raw:
            raise RuntimeError("src 不能为空：给要转的 PDF 路径。")
        # 源 PDF **允许绝对路径**（Zotero 库/下载目录本来就在工作区外），
        # 但相对路径按工作区解析，与 read_file 一致。
        sp = Path(raw)
        if not sp.is_absolute():
            sp = Path.cwd() / sp

        op = ""
        if str(out or "").strip():
            op = str(_out_path(out))

        r = _conv(str(sp), out=op, use_ocr=bool(use_ocr), images=bool(images),
                  python_exe=py, timeout=to)
        # 给模型的一句人话（回包里 verdict 已经是 SKIPPED/FAILED/OK，这里只补后果）
        if r.get("verdict") == "SKIPPED":
            r["next"] = ("**这次没有转换，产物不存在**。"
                         "要么在 config.json 的 kb.python 里指一个装了 pymupdf4llm 的解释器，"
                         "要么换一条不用转换的路（把 PDF 原文直接 `kb_add`/`kb_ingest` 入库 —— "
                         "litkb 入库时自己会抽全文）。"
                         "⚠️ 不要把 SKIPPED 当成「这篇 PDF 没内容」。")
        elif r.get("verdict") == "OK":
            # ★ 分工三句话（W3·R2）：转完这一刻正是模型最容易误以为
            #   "转出来的 md 就是入库格式"的位置，所以话要说在产物旁边。
            r["next"] = (f"结构化 Markdown 已产出：{r.get('out')}\n"
                         "★ 两套文本、两个去处：① **kb 库**（`kb_search`/`kb_ask` 查的那套索引）"
                         "**只收 PDF 原文**，入库把 PDF 交给 `kb_add`/`kb_ingest` ——"
                         "**litkb 入库时自己会抽全文**（实测同一篇：库内 148,087 字符），"
                         "同一篇没必要两边都存正文；② 这份 Markdown 是**给人读、给 Obsidian 精读**用的，"
                         "价值在标题层级 / 表格 / 阅读顺序 —— 它**不是**入库格式，"
                         "别把它喂给 `kb_ingest`。")
        return json.dumps(r, ensure_ascii=False, default=str)

    def kb_to_obsidian(src: str, title: str = "", authors=None, year=None,
                       journal: str = "", doi: str = "", tags=None,
                       folder: str = "", use_ocr: bool = False,
                       images: bool = False) -> str:
        from .kb_convert import PdfConverter, to_obsidian
        cfg = _bound_cfg
        kcfg = getattr(cfg, "kb", None) if cfg else None
        vault = (getattr(kcfg, "obsidian_vault", "") or "").strip() if kcfg else ""

        # ★ vault 没配就**如实报错**，不猜路径。
        # 猜一个"大概是这里"然后往那儿写笔记，是本仓最忌的静默行为：
        # 用户会在一个自己不知道的目录里发现一堆文件。
        if not vault:
            return json.dumps({
                "ok": False,
                "error": "没有配置 Obsidian 库目录（config.json 的 kb.obsidian_vault 是空的）",
                "hint": ("Obsidian 库就是**一个装了 .md 的目录** —— 在 Obsidian 里"
                         "「打开库」时选的那个文件夹。把它的绝对路径填进 "
                         "config.json 的 kb.obsidian_vault，本工具就会把笔记写进去。"
                         "⚠️ 本工具**不替你猜库在哪**，也不替你新建一个库。"),
                "note": "本次**没有写任何文件**。",
            }, ensure_ascii=False)

        raw = str(src or "").strip()
        sp = Path(raw)
        if not sp.is_absolute():
            sp = Path.cwd() / sp

        cv = PdfConverter(python_exe=(getattr(kcfg, "python", "") or None),
                          timeout=float(getattr(kcfg, "timeout", 600) or 600),
                          use_ocr=bool(use_ocr), images=bool(images))
        # ★ 这里**不落临时文件**，直接把 md 拿在内存里交给 to_obsidian。
        #   第一版是"先落一个临时 md、再删掉"，写出来当场撞了两条纪律：
        #   ① `hygiene_test` 的「内核包里零删除调用」—— 删临时文件需要一个
        #      删除调用，而本仓对这个包的要求是**一个都不许有**
        #      （删除一律走 `_tkit` 的重命名路线）；
        #   ② 白落一份文件再删，本身就给"删失败 / 删错对象"留了口子。
        #   既然 `to_obsidian` 收的就是**字符串**，那就没有理由经过磁盘 ——
        #   少一次写、少一次删、少一类失败。
        #   ⚠️ 注释里**不要原样写出删除调用的语法**：hygiene_test 有一条
        #   "误报对照"专门要求注释里引述调用语法的行**不许命中**，
        #   写了那个字样就会把那条对照判红（实测踩过）。
        conv = cv.convert(sp, out=None, use_ocr=bool(use_ocr),
                          images=bool(images), need_text=True)
        if not conv.ok:
            return json.dumps({
                "ok": False, "error": conv.error, "hint": conv.hint,
                "verdict": conv.verdict,
                "note": "转换没成功 ⇒ **vault 里没有写入任何东西**。",
            }, ensure_ascii=False)
        md = conv.text
        if not md:
            return json.dumps({
                "ok": False,
                "error": "转换报告成功，但**正文没有回传**（conv.text 为空）",
                "hint": "这是内核的缺陷，不是 PDF 的问题。请报告这一条。",
                "note": "本次**没有写任何文件**。",
            }, ensure_ascii=False)

        meta = {"title": title or sp.stem, "authors": authors or [],
                "year": year, "journal": journal, "doi": doi,
                "tags": tags or [], "_images": bool(images)}
        r = to_obsidian(md, meta, vault, folder=folder, name=title or sp.stem)
        r["convert"] = {"pages": conv.pages, "chars": conv.chars,
                        "seconds": conv.seconds, "notes": conv.notes}
        if r.get("ok"):
            r["next"] = (f"笔记已落进 Obsidian 库：{r.get('path')}\n"
                         "写进 vault **不等于**进了 kb 库 —— 两者是分开的两套："
                         "kb 库（`kb_search`/`kb_ask`）**只收 PDF**，"
                         "入库是把那份 **PDF 原文**交给 `kb_add`/`kb_ingest`，"
                         "**litkb 入库时自己会抽全文**（实测同一篇：库内 148,087 字符、"
                         "段落级索引带页码出处）；这份 Markdown 是**给人读、给 Obsidian 精读**用的，"
                         "不是入库格式，同一篇没必要两边都存正文。")
        return json.dumps(r, ensure_ascii=False, default=str)

    return {"convert_pdf": convert_pdf, "kb_to_obsidian": kb_to_obsidian}


SET_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要写备注的 .pptx 路径（相对工作区即可，如 \"deck.pptx\"）。",
        },
        "notes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "**按页序一一对应**的讲稿，每页一段，长度必须**恰好等于页数**"
                "（第 1 段配第 1 页）。某页确实不要讲稿也要占位给空串 \"\"。\n"
                "每段建议写清：这一页要讲什么、数据的来源与口径、"
                "听众最可能追问的那个问题。备注栏就是讲稿本身。"),
        },
    },
    "required": ["pptx", "notes"],
}


#: `deck_from_template`：在**品牌模板**的页上填字出稿（保真路线）。
TEMPLATE_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "out": {
            "type": "string",
            "description": "产出到工作区里的哪个 .pptx（相对路径即可，如 \"deck.pptx\"）。",
        },
        "pages": {
            "type": "array",
            "description": (
                "逐页：`[{\"page\": \"cover\", \"slots\": {\"2\": \"标题\", \"3\": \"汇报人：…\"}}, …]`。\n"
                "`page` 只能取 `deck_template_plan()` 列出的页 id（section-1..4 是四个章节页）。\n"
                "`slots` 的键是**槽位索引**（清单里的 `[ 2]`），值是文字（字符串列表 = 多段）。\n"
                "⚠️ **每个槽都必须填**：不填就会把模板里的「请输入文本」原样交出去，工具会拒绝并点名缺哪些。\n"
                "⚠️ **字数必须 ≤ 清单里标的上限**（那是从几何与字号算出来的，超了必被拒）。\n"
                "⚠️ 同一个模板页只能用一次（要两个章节页请用 section-1 / section-2）。"),
        },
        "template": {
            "type": "string",
            "description": "模板 .pptx 的绝对路径；留空则用 `config.json` 里配的那个。",
        },
    },
    "required": ["out", "pages"],
}


STYLE_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要套视觉主题的 .pptx 路径（相对工作区即可，如 \"deck.pptx\"）。",
        },
        "theme": {
            "type": "string",
            "description": (
                "主题名。目前只有 \"juanqing\"（绢青陈墨：绢底墨字、朱砂点睛、"
                "宋体标题＋雅黑正文）。**不要自己编色号或字体名** —— "
                "体检判据按同一份主题定义核，编出来的不算数。"),
        },
        "footer": {
            "type": "boolean",
            "description": "是否加页脚细线与页码（默认 true）。",
        },
    },
    "required": ["pptx"],
}


DECK_REFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "detail": {
            "type": "boolean",
            "description": (
                "false（默认）⇒ 给**逐页标题 + 页型序列**的紧凑版；"
                "true ⇒ 连同**每页正文与讲稿备注**一起给（照着填的完整示范）。"
                "没有把握时先用默认。"),
        },
    },
}


COMPOSE_DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "pptx": {
            "type": "string",
            "description": "要排版的 .pptx 路径（相对工作区即可，如 \"deck.pptx\"）。",
        },
        "pages": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "**逐页一个页型**，长度必须**恰好等于页数**（第 1 项配第 1 页）。\n"
                "可选值：cover（封面）/ section（章节）/ content（内容）/ "
                "two-col（双栏）/ chart（图表）/ closing（收束）。\n"
                "你**只报页型**，版面几何、字号、对齐全部由内核按设计规范落。\n"
                "判断页型的口径：第一页 cover，最后一页 closing，"
                "页面主体是一张图/表/图表就 chart，其余 content。"
                "**没把握就先调一次 `deck_reference`** —— 它给出一份六种页型"
                "全用到的参考样例，照着比对比猜准。"),
        },
        "theme": {
            "type": "string",
            "description": "主题名，目前只有 \"juanqing\"（绢青陈墨）。不要自己编色号。",
        },
        "footer": {
            "type": "boolean",
            "description": "是否加页脚细线、落款与页码（默认 true；封面/章节/收束页本就不带）。",
        },
        "footer_label": {
            "type": "string",
            "description": "页脚左侧的落款文字，如 \"课题名 ｜ 第 N 次组会汇报\"。留空则只画线不写字。",
        },
        "out": {
            "type": "string",
            "description": (
                "另存到新文件（相对工作区）。**建议留空** —— 留空即原地重排。\n"
                "⚠️ 本工具是**就地改写**：留空会覆盖 `pptx` 指向的那份。"
                "想保留未排版的原件，请先自己复制一份（内核无法替你回滚）。"),
        },
    },
    "required": ["pptx", "pages"],
}


def _make_read_file() -> "Callable[..., str]":
    """读**本任务工作区里的文本文件**。

    ★ 这是内核一直缺的一个**基元**，2026-09-21 实跑才抓到。
    当时挂载的 67 个工具里**没有任何一个能读本地文本文件**：
      · `import_document` 只吃 pdf/docx —— 喂 `.md` 直接回
        「不支持的格式: md（仅 pdf/docx）」。**它能"输出"规范化 Markdown，
        却读不进 Markdown**，这是最讽刺的一处；
      · `web_extract` 只吃 URL，给相对路径回 `invalid_extract_url`
        （而且它以 `ok: true` 返回，连"这条路不通"都不说）；
      · `read_skill` / `load_skill` 只能读**技能目录**里的文件，
        拿材料路径去调会被"指向技能目录之外"拒掉。
    于是模型拿到一份 `.md` 材料后**无路可走**：实测它把
    `export_docx`（想把 md 转成 docx 再 import）、`load_skill(file=材料)`、
    `web_extract` 三条歪路各撞一遍，**18 次调用零进展**，PPT 一个工具没碰。

    ⇒ 结论不是"教它绕路"，是**补上缺掉的基元**。
    通用问法：**「这件事，我给了它工具吗？」**
    —— 不是"我告诉它该怎么做了吗"。没有工具的动作，写多少纪律都是空转。

    安全边界：注册表的工作区围栏只管 `mutating` 工具，读工具默认豁免；
    所以**围栏必须写在这里**，否则 `read_file("/etc/passwd")` 就是一条读取通道。
    """
    def read_file(path: str, max_chars: int = 20000) -> str:
        root = Path.cwd().resolve()
        raw = str(path or "").strip()
        if not raw:
            raise RuntimeError("path 不能为空：给工作区里的文件路径（如「素材.md」）。")
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except Exception as e:
            raise RuntimeError(f"路径无法解析：{raw}（{type(e).__name__}: {e}）") from None

        # ── 围栏：解析后必须落在工作区内（**先解析再判**，防 `../` 绕出去）──
        if p != root and root not in p.parents:
            raise RuntimeError(
                f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能读它里面的文件。")

        if p.is_dir():
            names = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())
            return (f"（{raw} 是**目录**，不是文件）里面有 {len(names)} 项：\n  "
                    + "\n  ".join(names[:60]))
        if not p.is_file():
            # ★ 找不到时**列出真实邻居** —— 这一条直接治"猜路径"：
            #   实测模型会连猜 58 次不同的名字（.md→.docx→.pdf→materials/…），
            #   而它缺的从来不是"更努力的猜"，是**一份真实清单**。
            try:
                near = sorted(x.name + ("/" if x.is_dir() else "")
                              for x in root.iterdir())[:60]
            except Exception:
                near = []
            raise RuntimeError(
                f"文件不存在：{raw}\n   工作区 {root} 里真实有的是：\n    "
                + ("\n    ".join(near) if near else "（空）")
                + "\n   ⚠️ 请**照抄**上面某个名字，不要再换后缀或换目录去猜。")

        blob = p.read_bytes()
        text, enc, err = "", "", ""
        for _e in ("utf-8", "utf-8-sig", "gbk"):
            try:
                text, enc = blob.decode(_e), _e
                break
            except UnicodeDecodeError as e:
                err = f"{_e}: {e}"
        if not enc:
            raise RuntimeError(
                f"读不出文本：{p.name}（{len(blob)} 字节）不是 UTF-8/GBK 文本 —— "
                f"{err}。二进制文件（pdf/docx/图片）请用 `import_document` "
                f"或 `figure_audit_extract`，别用本工具。")

        cap = int(max_chars) if str(max_chars).strip() else 20000
        cap = max(200, min(cap, 200000))
        head = (f"═══ {p.name} ═══（{len(blob)} 字节 · 编码 {enc} · "
                f"{text.count(chr(10)) + 1} 行）\n")
        if len(text) > cap:
            return (head + text[:cap]
                    + f"\n…（已截断：全文 {len(text)} 字符，这里给了前 {cap} 字符。"
                      f"要读后面用 max_chars 调大，或分段读。）")
        return head + text

    return read_file


def _make_write_file() -> "Callable[..., str]":
    """把文本写进**本任务工作区里的文件**。`read_file` 的对偶基元。

    ★ 这是内核缺的**第三个基元**，2026-10-14 实跑抓到（第一个 `read_file`
    2026-09-21、第二个 `set_slide_notes`）。

    当时的情形：契约要求产出两份**指定路径**的文本文件（综述 + 写作说明），
    而挂载的 56 个工具里**没有任何一个能写任意文本**：
      · `topic_write` 只能写课题的 plan/reading/evidence **三个固定文件名**
        （`topic.py:write_doc` 的 doc 参数只认这三个 key），写不到 `综述.md`；
      · `kb_note` / `record_skill_use` 写的是库内记录与台账，不是文件；
      · `export_docx` 只吃 md 转 docx，且实测在路径不存在时直接抛 Traceback；
      · `mechanical_fix` 只能**就地修改已存在**的文件，不能创建 —— 实测回
        「文件不存在」；
      · `convert_pdf(out=…)` 要有上游 PDF 才谈得上落盘。
    模型因此**无路可走**：它老老实实把 9 条路各撞一遍（含派子代理、派
    workbuddy 执行者），拿到 9 份"此路不通"的证据，然后**如实报告阻塞**。
    它没编、没假装完成 —— 但一份能写文件的活干不成，缺的是工具。

    ⇒ 通用问法还是那一句：**「这件事，我给了它工具吗？」**
    这次的教训更硬：**"能读"不等于"能写"**。上一个基元补的是读，
    于是"读得到材料"这件事被解决了，而"把成果放回盘上"一直悬空 ——
    两次都是同一个盲区：把「模型会说话」当成「模型能落盘」。

    安全边界：与 `read_file` **完全同一条**（先 resolve 再判、必须落在
    工作区内）。写工具比读工具更需要它，所以这里再抄一遍而不复用 ——
    围栏这种东西，两处各写一遍才看得见；抽成函数会让"哪些工具受管"变模糊。
    注册时标 `mutating=True`，让注册表的工作区围栏也管一道（双保险）。
    """
    def write_file(path: str, text: str, append: bool = False) -> str:
        root = Path.cwd().resolve()
        raw = str(path or "").strip()
        if not raw:
            raise RuntimeError("path 不能为空：给工作区里的落点路径（如「综述.md」）。")
        p = Path(raw)
        if not p.is_absolute():
            p = root / p
        try:
            p = p.resolve()
        except Exception as e:
            raise RuntimeError(f"路径无法解析：{raw}（{type(e).__name__}: {e}）") from None

        # ── 围栏：与 read_file 同一条 —— 先解析再判，防 `../` 绕出去 ──
        if p != root and root not in p.parents:
            raise RuntimeError(
                f"⛔ 越界：{p}\n   本任务工作区是 {root}，只能写它里面的文件。")

        if p.is_dir():
            raise RuntimeError(
                f"⛔ 落点是个**目录**：{p}\n   请给一个文件名（如 `{p.name}/综述.md`）。")

        body = text if isinstance(text, str) else str(text or "")
        if not body.strip():
            raise RuntimeError("text 为空 —— 空写没有意义。要建空文件请写明用途再调。")

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise RuntimeError(f"建不了上级目录 {p.parent}：{type(e).__name__}: {e}") from None

        mode = "a" if append else "w"
        existed = p.is_file()
        before = p.stat().st_size if existed else 0
        try:
            with p.open(mode, encoding="utf-8", newline="\n") as f:
                if append and existed:
                    f.write("\n")
                f.write(body)
        except Exception as e:
            raise RuntimeError(f"写入失败 {p}：{type(e).__name__}: {e}") from None

        # ── 回读核验（照 `write_doc` 的做法）：写这类工具必须自己证明写进去了 ──
        try:
            back = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise RuntimeError(f"写后回读失败 {p}：{type(e).__name__}: {e}") from None
        tail = body.strip()[-200:].strip()
        ok = bool(tail) and back.strip().endswith(tail)
        if not ok:
            raise RuntimeError(
                f"⚠️ 写后回读**对不上**：{p}（{len(back)} 字符）末尾不是本次写入的内容。"
                f"文件可能没写成功，请用 read_file 核一遍再往下走。")

        after = p.stat().st_size
        act = "追加" if (append and existed) else ("覆盖" if existed else "新建")
        # ★ 回执里**必须带绝对路径**：`read_file`/`write_file` 的根都是 `Path.cwd()`，
        #   而内核在跑任务前会 `os.chdir(工作区)`（`loop.py:2817`）。
        #   于是模型写的相对路径「综述.md」实际落在 `<工作区>/综述.md` ——
        #   2026-10-14 实测：模型连写 3 次都报 ok，交付物却不在用户要的位置，
        #   因为回执只说 `综述.md`，模型**没有任何办法知道** cwd 不是它以为的那个。
        #   同型的教训见 `read_file`（找不到时列真实邻居）。这里同理：**把真相摊开**。
        return (f"✔ 已{act}：{p.name}（{before} → {after} 字节，全文 {len(back)} 字符）\n"
                f"   绝对路径：{p}\n"
                f"   ⚠️ 本任务的工作区根是 {root}；相对路径按它解析。"
                f"要落到别处，请在 path 里给**绝对路径**（也必须在工作区内）。\n"
                f"   末 80 字：…{back.strip()[-80:]}")

    return write_file


def _make_audit_deck(cfg) -> "Callable[..., str]":
    """造 `audit_deck` 工具的执行体。

    与 `audit_figure` 同构：返回**紧凑 JSON**，第 9 条 deck 门要
    `json.loads` 它来判定。全文报告写盘，路径一并返回。

    超长保护也照抄：注册表会把超长结果截断，一截断 JSON 就废了 ——
    宁可让模型少读一段散文，也不能让门拿到半截 JSON。
    """
    def audit_deck(pptx: str, require_notes: bool = False,
                   deep_contrast: bool = False) -> str:
        import json as _json
        from .deck import DeckAuditor

        if not str(pptx or "").strip():
            raise RuntimeError("pptx 不能为空：给最终 .pptx 的路径（相对工作区即可）。")
        # 相对路径按**进程 cwd** 解析 —— 任务期间它就是本次 workspace
        # （loop.run() 会 chdir 进去），所以报告只会落在围栏内。
        wd = Path.cwd()
        au = DeckAuditor(python_exe=getattr(cfg, "python", "") or None,
                         skills_dir=getattr(cfg, "skills_dir", "") or None,
                         cfg=cfg)
        # ★ W10：`deep_contrast` 不传/false ⇒ 传 **None** 而不是 False —— 让 `audit()`
        #   回落到配置项 `config.deck.deep_contrast` ⇒ **不传时与今天逐字相同**
        #   （工单 §二）。只有显式 true 才点名开启那条 ~15 秒的渲染像素校验。
        rep = au.audit(pptx, workdir=wd,
                       deep_contrast=None if not deep_contrast else True)
        # 只影响**给模型看的那份判定**；门会从契约独立重判（见 gate.py 第 9 条）。
        rep.strict_notes = bool(require_notes)
        try:
            rep.save(Path(wd) / "deck_report.json")
        except Exception:
            pass

        payload = rep.summary_json()
        payload["report_text"] = rep.format_for_model()
        payload["facts"] = rep.facts.to_dict()
        blob = _json.dumps(payload, ensure_ascii=False, default=str)
        if len(blob) > 9000:
            payload.pop("report_text", None)
            payload.pop("facts", None)
            payload["note"] = "报告已过长，人读版见 report_path。"
            blob = _json.dumps(payload, ensure_ascii=False, default=str)
        return blob

    return audit_deck


def _make_set_slide_notes(cfg) -> "Callable[..., str]":
    """造 `set_slide_notes` 工具的执行体。**与 `audit_deck` 成对：一个量，一个改。**

    ★ 这是内核缺的**第二个基元**（第一个是 `read_file`），同样是实跑抓到的，
    同样**一条基元抵得过若干轮提示词改造**。

    为什么不是 `run_python`：见 `deck.write_slide_notes` 的 docstring
    （确定性 / 可测 / 不把模型推到它够不着的地方）。这里只说工具层的取舍：

    * 失败一律走**异常**通道（`ok=False`），不渲染成"成功文本里的一个警告" ——
      参数不对、路径越界、页数不匹配都不是"结果的一种"，是**这次调用没成**。
      渲染成 ok 的成功文本，模型就会以为可以往下走了（G 型的经典制造法）。
    * `mutating=True`：它真的改盘上的 .pptx，该受工作区围栏管。
      ⚠️ 但围栏只看**绝对路径**参数，相对路径（含 `../`）它一律放行 ⇒
      越界判定由 `write_slide_notes` 自己**先 resolve 再判**地关掉。
    """
    def set_slide_notes(pptx: str, notes) -> str:
        from .deck import write_slide_notes

        r = write_slide_notes(pptx, notes, workdir=Path.cwd(), cfg=cfg)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return set_slide_notes


def _make_style_deck(cfg) -> "Callable[..., str]":
    """造 `style_deck` 工具的执行体。**与 `audit_deck` 成对：一个量，一个扮。**

    为什么不是 `run_python`：同 `write_slide_notes` —— 模型给**主题名**，
    代码由内核生成。它没有装包、改环境的能力，把它推到 `lxml` 的报错面前，
    换来的只会是"换个参数再试一次"。

    ⚠️ 这个工具的存在本身就是一条纪律的产物：**"我给了它工具吗"**。
    用户看完一份结构全绿的 deck 只说了一句"那能做的这么丑" —— 丑的原因之一
    就是**没有任何一个真能改外观的工具**（看着对口的那个是空壳）。
    """

    def style_deck(pptx: str, theme: "str | None" = None,
                   footer: bool = True) -> str:
        from .deck import style_deck as _style, DEFAULT_DECK_THEME

        r = _style(pptx, theme=(theme or DEFAULT_DECK_THEME),
                   footer=bool(footer), workdir=Path.cwd(), cfg=cfg)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return style_deck


def _make_deck_from_template(cfg) -> "Callable[..., str]":
    """造 `deck_from_template` 的执行体 —— **品牌保真**路线。

    ★ 为什么它是**第一条**该想到的路（2026-09-22，用户递来学校模板）：
      用户看完一份五门全绿的 deck 只说「太差了，不能直接上组会」，根因是
      **全链没有任何一处存着设计资产**。而"学校的模板"就是一份现成的、
      发过版的资产 —— 它的价值在**全部装饰**里（满版蓝底、450pt 章节数字、
      校徽、出血照片）。把设计反推成"12 个色槽 + 六组几何"只能得到**近似品**，
      品牌的要求是"就是它"。
      ⇒ **人怎么用模板，就让 agent 怎么用**：保留模板页、在槽上填字、多余页删掉。
        一个字都不重绘。

    ⚠️ 它**不代替门**：产出之后仍须 `audit_deck`（否则第 9 条硬门拦交付）。
       "能按模板出稿"和"这份稿能讲"是两件事。
    """

    def deck_from_template(out: str, pages, template: "str | None" = None) -> str:
        # ★ 主控勘误（2026-09-27）：`template_name_of` 必须在这儿 import。
        #   原先只 import 了 `deck_from_template` + `load_template`，而下面
        #   第 4 行就调 `template_name_of(cfg)` ⇒ **本工具每次调用都抛 NameError**，
        #   100% 不可用。而 `regress.py --only deck` 396 项全绿 ——
        #   因为套件直接测 `deck.py` 的函数，**从不经过这个薄包装层**
        #   （"测了库、没测接缝"）。
        #   讽刺的是同一文件 `loop.py:1799` 的 `deck_template_plan` 那条路
        #   **import 对了**，所以只有"真正出稿"这条路是断的。
        #   ⇒ 修这一行只是治标；`deck_test.py` 里补了"每个工具工厂都不得抛
        #     NameError"的冒烟守卫，那才是治本（这类错会再犯）。
        from .deck import deck_from_template as _tpl, load_template, template_name_of

        # ★ 槽位表由 `config.json` 的 `deck.template_name` **单源**指定。
        #   写死 "zjut" 就是"两处真相"——换模板要记得改两个地方，早晚漏一处。
        _name = template_name_of(cfg)
        r = _tpl(out, pages, template_pptx=(template or ""), workdir=Path.cwd(),
                 cfg=cfg, template_slots=load_template(_name), template_name=_name)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return deck_from_template


def _make_compose_deck(cfg) -> "Callable[..., str]":
    """造 `compose_deck` 工具的执行体。**与 `style_deck` 二选一：一个精排，一个快套。**

    ★ 为什么这个工具非有不可（2026-09-21，用户原话）：

      用户看完一份**五门全绿**的 13 页组会 deck，只说了一句
      「**太差了，不能直接上组会**」。而 `style_deck` 已经跑过了。
      量下来根因不在模型、也不在判据 —— 是**整条链路上没有任何一处存着设计资产**：
      起手是 python-pptx 的空白 4:3 默认母版，`style_deck` 的全部打扮 =
      「换 12 个色槽 + 换字体 + 一条 0.01 英寸的页脚线」。
      全链最"具体"的设计数值，就是那条页脚线。

      ⇒ **判据只能划定下限；产出上限来自资产本身。**
        别人的 agent「加个 skill 就好看」，加的其实就是这套东西：把设计师的
        隐性判断（版式比例、色彩克制、字号对比、留白）**固化成可判定的数字**。
        我们把这份资产放进 `rongke/design.py`，再由本工具**确定性地**落到版面上。

    ★ 为什么不能只改 `style_deck`：那边的契约是"**不动版面结构**"（快套）。
      而「好看」这件事，**版面本身才是大头** —— 只换色换字，骨架还是那副骨架。
      两个工具的目标不同，所以**并存**：赶时间/只想起手打扮 → `style_deck`；
      要交付级 → `compose_deck`。**两个都调 = 双重坐标系**（见各自 docstring）。

    ★ 为什么模型只报**页型**、不报坐标：它本来就没有"坐标感"
      （见 `config.py` 的模型名）。让它算 px 只会得到一串貌似合理的数字。
      它擅长的是**判断这一页是干什么的** —— 所以只要它交页型，
      几何由内核查表。**这是把"模型会做的"和"资产说了算的"分开。**
    """

    def compose_deck(pptx: str, pages, theme: "str | None" = None,
                     footer: bool = True, footer_label: str = "",
                     out: "str | None" = None) -> str:
        from .deck import compose_deck as _compose, DEFAULT_DECK_THEME

        r = _compose(pptx, pages, theme=(theme or DEFAULT_DECK_THEME),
                     footer=bool(footer), footer_label=str(footer_label or ""),
                     out=(out or None), workdir=Path.cwd(), cfg=cfg)
        text = r.format_for_model()
        if not r.ok:
            raise RuntimeError(text)
        return text

    return compose_deck


def _make_deck_reference(cfg) -> "Callable[..., str]":
    """造 `deck_reference` 工具的执行体。**只读**，不碰任何文件。

    ★★ 为什么要有这个工具（2026-09-21，用户那句方向纠正的直接产物）：

      用户说：**「人家同样是用 agent，只加了个 skill 就能做出好看的图了，
      而你在这里做了半天，依旧不好看。」**

      那份 skill 里真正起作用的不是形容词（「简洁大气」「留白充足」），
      而是**一个可以照着比对的成品**。所以我们把一份 12 页、六种页型全用到的
      组会 deck 固化进仓库（`docs/deck-reference/`），由本工具交给模型。

    ★★ 所以这个工具**返回文本，不返回图** —— 这不是偷懒，是模型看不见图：

      `llm.py` 把 `content` 硬编码成字符串、`loop.py::_messages_chars` 只处理
      `isinstance(c, str)`、全仓 `image_url`/`input_image` **零命中**。
      ⇒ 给模型一张 PNG 等于没给。**参考样例要起作用，必须是文本的、完整的、
        可抄的**：页型序列 + 逐页标题 + 逐页正文 + 讲稿备注。

      路径里的 `.pptx` / `.sheet.png` / `.compare.png` 是**给人看的**，
      工具会把路径报给模型，但**不假装模型看过**。

    ★ 为什么也把「笔记要写什么」放进示范：备注栏（来源、口径、会被问到的问题）
      才是汇报时真正要说的话。没有它，deck 只是给人看的摘要 ——
      而 `notes_coverage` 判据要 60%，光靠一句"记得写备注"是补不上的。
    """

    def deck_reference(detail: bool = False) -> str:
        from . import deckref

        head = [
            "【参考样例 · 组会 deck 长得对是什么样】",
            "",
            "★ 这是**文本示范**，直接照抄结构即可。"
            "（内核的模型通道是纯文本、读不了图片 —— 所以示范给的是文字，不是图。）",
            "",
        ]
        body = deckref.reference_text(summary=not bool(detail))
        tail = [
            "",
            "── 随库发布的成品（**给人看的**，你读不了图，别假装读过）──",
            f"  成品 deck ：{deckref.REFERENCE_PPTX}",
            f"  分页预览图：{deckref.REFERENCE_SHEET_PNG}",
            f"  前后对照图：{deckref.REFERENCE_COMPARE_PNG}",
            "",
            "── 接下来怎么用 ──",
            "  1) 先按上面的**页型序列**与每页论点密度，把你的内容写成逐页文本；",
            # 2026-11-10：原来这一步点名 `create_presentation` / 加页 / 写文字，
            # 那是**已退休的外部 PPT MCP 工具**，现网注册表里根本没有 ——
            # 配方让模型去调一个不存在的工具，就是 PPT 链路真正断掉的地方。
            # 改指常驻的幻灯片编排台：只写内容，几何/字号/主题由内核查表。
            # ⚠️ 只改这条**工具回包**（配方第 1 条必读），不动 SYSTEM、不动
            #    contract 的配方常量 ⇒ baseline 逐字节等价判据不受影响。
            "  2) **一次成稿**：调 `deck_plan()` 拿 spec 模板（六种页型各自的字段），"
            "把逐页内容填进去，再调 `deck_make(spec=...)` —— 它自动完成"
            "造源稿 → 精排 → 写讲稿备注 → 写后逐页读回 → 生成幻灯片台.html。"
            "**不要**再去调 `create_presentation` / `add_slide` / `manage_text`："
            "那套外部 MCP 已退休，注册表里没有它们。",
            "  3) 要改单页用 `deck_slide(index=N, ...)`，别重写整份 spec；"
            "`deck_make` **内部已经做过精排**，不要再对同一份稿补一次"
            "`compose_deck`（两套坐标系会打架）。",
            "  4) 讲稿已在 `deck_make` 里按每页 `note` 写好并读回；"
            "分步路线才需要 `set_slide_notes`（长度必须等于页数）。",
            "  5) 最后调 `audit_deck` —— 不过就不要宣布完成。",
            "",
            "⚠️ 参考样例**自己也是这么过门的**（五门全绿、零提示）："
            "它就是标准的可执行版本，不是理想化的描述。",
        ]
        return "\n".join(head) + body + "\n".join(tail)

    return deck_reference


def _make_plot_svg(cfg) -> "Callable[..., str]":
    """造 `plot_svg` 工具的执行体 —— **零第三方依赖**的原生出图。

    ## 为什么单独一个工具，而不是把 `plot_data` 改成「没装库就用 SVG」

    两条路线的**判据不同**，混在一个返回值里会长出最坏的那种假成功：
    模型拿到 `ok:true` 就以为图可以送审，而 `figure.py` 的六道门其实一道没跑。
    分开之后，工具名本身就是路线声明 —— 门（见 `gate.py` 的预览分支）
    能只看台账里出现过哪个工具，就知道本次有没有资格走预览路线。

    ## 不冒充过门
    `scope="preview"` 是固定文案，不随数据变化；`ok` 只表示「SVG 结构自检通过」，
    不表示「出版级审计通过」，也**不表示 SVG 质量门通过**。

    ## 质量结论走另一条键
    出图后立刻跑 `svgq` 七道门（够得着浏览器就升到实测级），结论放进 `quality`
    与 `next`，红项逼模型改规格重画。**刻意不去改 `ok` / `scope` / `checks`**：
    `gate.py` 的预览分支钉死了 `ok is True and scope == "preview"`，
    `svgplot_test` 钉死了 `checks` 全 bool 全真 —— 把质量结论塞进 `ok` 会同时
    撞坏这两处，且会让「预览路线」这个放行语义变成「质量合格」的假承诺。
    """
    def plot_svg(table: str, kind: str = "", x: str = "", y: str = "",
                 group: str = "", title: str = "", width_mm: float = None,
                 out: str = "") -> str:
        import json as _json
        from . import analysis as _an
        from . import svgplot as _sv

        wd = Path.cwd()
        tp = Path(table) if Path(table).is_absolute() else (wd / table)
        if not tp.exists():
            raise RuntimeError(f"数据表不存在：{tp}。先把数据放进工作区。")
        try:
            rows, meta = _an.read_table(tp)
        except _an.TableError as e:
            raise RuntimeError(f"表读不进来：{e}")
        cols = _an.profile_table(rows)
        if not cols:
            raise RuntimeError(f"{tp.name} 里一列都没测出来，不画空图。")
        try:
            plan = _an.plan_from_shape(
                tp.name, meta, cols, kind=kind, x=x, y=y, group=group,
                title=title,
                width_mm=float(width_mm) if width_mm else _an.COL_WIDTH_MM)
        except ValueError as e:
            raise RuntimeError(f"选图型失败：{e}")

        dest = Path(out) if out else (wd / f"{plan.get('stem') or 'figure'}.svg")
        if not dest.is_absolute():
            dest = wd / dest
        if dest.suffix.lower() != ".svg":
            dest = dest.with_suffix(".svg")
        try:
            rep = _sv.render_svg(rows, plan, dest)
        except _sv.SVGPlotError as e:
            raise RuntimeError(f"原生 SVG 后端画不了这张图：{e}")
        except Exception as e:                              # noqa: BLE001
            raise RuntimeError(f"原生 SVG 后端出错：{type(e).__name__}: {e}")

        payload = dict(rep)
        payload["columns"] = [c.to_dict() for c in cols]
        payload["suggestions"] = [s.to_dict() for s in _an.recommend(cols)]

        # ── SVG 质量门：出图 ≠ 合格 ─────────────────────────────
        # 第一层纯标准库，永远跑；第二层要浏览器，够得着就升到实测级，
        # 够不着就如实判「审不了」（**不等于通过**）。
        # ★ 三条不许撞：`ok` 仍只表示结构自检通过、`scope` 仍是 "preview"、
        #   `checks` 仍全 bool 全真 —— gate.py 的预览分支与 svgplot_test 钉死了
        #   这三条。质量结论另走 `quality` + `next`，不去改门的放行语义。
        from . import svgmeasure as _sm
        from . import svgq as _sq
        try:
            measured = _sm.measure(dest, python=cfg.figure.python)
        except Exception as e:                               # noqa: BLE001
            # measure() 自称不抛；真抛了也不能让「出图」这件事失败，
            # 但必须留下指纹，不许静默当成「没跑第二层」。
            measured = {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "boxes": []}
        q = _sq.audit_svg(dest, layout=rep.get("layout"), measured=measured)
        summary = q.pop("summary")
        payload["quality"] = {
            "tier": q["tier"],
            "gates": {g["name"]: g["verdict"] for g in q["gates"]},
            "blocking": q["blocking"],
            "fix_before_delivery": q["fix_before_delivery"],
            "not_auditable": q["not_auditable"],
            "n_texts": q["n_texts"], "n_marks": q["n_marks"],
            "manifest": q["manifest"],
            "measure_error": (measured or {}).get("error", ""),
        }
        tier_txt = {"measured": "浏览器实测级",
                    "partial": "浏览器部分实测级"}.get(q["tier"], "标准库估算级")
        payload["audit_scope"] = (
            f"已跑 SVG 质量门七道（{tier_txt}）：字号物理量纲 / 对比度 / 文字容纳 / "
            "碰撞 / 数据保真 / 字体栈声明 / 字形覆盖。"
            "⚠️ 仍未跑 PDF 六道渲染后审计门（那是投稿路线，审的是 PDF）；"
            "字形覆盖只判字体栈里声明的中文字体在本机是否可用，不判逐字豆腐块。")

        nxt = [
            "这是**预览级** SVG：可直接插网页 / PPT / 给用户看。",
            "⚠️ 不要把它当投稿级 PDF 交给 audit_figure —— 那六道门审的是 PDF，"
            "本次一道都没跑。若任务要求出版级配图，装 "
            'pip install "rongke[figure]" 后改用 plot_data + audit_figure。',
            "",
            summary,
        ]
        if q["fix_before_delivery"]:
            nxt += [
                "",
                f"⛔ 交付前必须修：{'、'.join(q['fix_before_delivery'])}。",
                "修法是**改规格重画**（调 title / width_mm / x / y / group / kind，"
                "或把数据表里的长标签缩短），再调一次 plot_svg 复测。",
                "不要去手改 SVG 字节，也不要靠缩小字号把重叠压下去 ——"
                "字号有 5 pt 物理下限，压下去只会把红项从碰撞门挪到字号门。",
            ]
        if q["not_auditable"]:
            nxt += [
                "",
                f"⚠️ 审不了（**不等于通过**）：{'、'.join(q['not_auditable'])}。"
                "交付时要把这几项未审如实告诉用户，别声称「图已审过」。",
            ]
        payload["next"] = "\n".join(nxt)
        return _json.dumps(payload, ensure_ascii=False, default=str)

    return plot_svg


def _make_plot_data(cfg) -> "Callable[..., str]":
    """造 `plot_data` 工具的执行体 —— 数据分析成图。

    ## 为什么这是**一个**工具而不是三个

    读表、选图型、写脚本、跑图，这四步之间没有**需要模型决策**的地方：
    选图型的判据是测量出来的（列类型、基数、有没有时间轴），不是靠灵感。
    拆成四个工具只会让模型在中间四次要一遍遍重述上下文，
    还多出四次填错参数的机会。所以合成一个，输出里把中间结论
    （`columns` / `suggestions` / `plan`）全部摊开给它看。

    ## 它**不**做审计

    图面审计仍由 `audit_figure` 负责（第 8 条硬门点名要它）。
    这里只把 `pdf` + `script` + **契约** + **图注草稿**一并返回，
    模型原样转手给 `audit_figure` 即可 —— 契约和图注都由模块按图型生成，
    不让模型手填，因为那两处最容易填错（`role` 填成"折线图"、
    `formats` 漏掉位图），而填错的后果是"门全绿但总判不过"。
    """
    def plot_data(table: str, kind: str = "", x: str = "", y: str = "",
                  group: str = "", title: str = "", width_mm: float = None,
                  journal: str = "") -> str:
        import json as _json
        from . import analysis as _an

        wd = Path.cwd()
        # 相对路径按**进程 cwd** 解析 —— 任务期间它就是本次 workspace
        # （`loop.run()` 会 chdir 进去），所以产物只会落在围栏内。
        tp = Path(table) if Path(table).is_absolute() else (wd / table)
        if not tp.exists():
            raise RuntimeError(f"数据表不存在：{tp}。先把数据放进工作区。")
        width = float(width_mm) if width_mm else _an.COL_WIDTH_MM
        try:
            rep = _an.analyze(tp, kind=kind, x=x, y=y, group=group,
                              title=title, width_mm=width,
                              out_dir=wd, python_exe=(cfg.python or None),
                              timeout=float(getattr(cfg, "timeout", 300)))
        except _an.TableError as e:
            raise RuntimeError(str(e))

        payload: dict = {
            "table": tp.name,
            "columns": rep.get("columns"),
            "suggestions": rep.get("suggestions"),
            "plan": rep.get("plan"),
            "ran": rep.get("ran"),
            "run": rep.get("run"),
            "error": rep.get("error"),
        }
        if rep.get("plan"):
            cd = _an.contract_for(rep["plan"], journal=journal)
            lg = _an.legend_for(cd, meta=rep.get("meta"),
                                center=str((cd["panels"] or [{}])[0].get("center") or ""),
                                spread=str((cd["panels"] or [{}])[0].get("spread") or ""))
            payload["contract"] = cd
            payload["legend_draft"] = lg
            payload["next"] = (
                "把 contract 与 legend_draft 原样交给 audit_figure（pdf 用 run.pdf、"
                "source 用 script 的文件名），图注里的 ___ 与结论句必须由你补全后再审。")

        # ── 2026-11-05：缺绘图栈时自动降级到荣珂原生 SVG 后端 ──────────
        # 起因：安装包把 matplotlib/pandas/PyMuPDF 全排除了（`rongke.spec`
        # 的 excludes），所以「别人装完」的 `plot_data` 只会回一句装置不全。
        # 那种状态下界面能开、工具在册、一张图都出不来 —— 是产品缺件，
        # 不是用户配置错。这里补一条零依赖路线，并且**如实标注**它是预览级。
        if rep.get("error") and not rep.get("ran"):
            try:
                from . import svgplot as _sv
                rows, _meta = _an.read_table(tp)
                svg_path = wd / f"{(rep.get('plan') or {}).get('stem') or 'figure'}.svg"
                payload["native_svg"] = _sv.render_svg(
                    rows, rep.get("plan") or {}, svg_path)
                payload["fallback"] = (
                    "本机缺 matplotlib/pandas/PyMuPDF：已**自动降级**为荣珂原生 SVG 后端，"
                    "产出预览图（见 native_svg.svg）。⚠️ 这不是投稿级 PDF，"
                    "PDF 六道渲染后审计门一道都没跑，不要把下面的 contract 交给 audit_figure。"
                    '要出版级请 pip install "rongke[figure]" 后重跑 plot_data。')
                if payload["native_svg"].get("ok"):
                    payload["next"] = (
                        "本次是**预览级 SVG 路线**：把 native_svg.svg 给用户看即可，"
                        "不要提交 audit_figure（没有 PDF 可审）。"
                        "contract / legend_draft 是投稿路线的模板，本次不适用。")
            except Exception as e:                          # noqa: BLE001
                payload["native_svg_error"] = (
                    f"原生 SVG 后端也没出成：{type(e).__name__}: {e}。"
                    "如实报告缺件，不要假装已有图。")
        blob = _json.dumps(payload, ensure_ascii=False, default=str)
        if len(blob) > 12000:
            # 与 audit_figure 同一条纪律：宁少给一段散文，不给半截 JSON。
            payload.pop("columns", None)
            payload["note"] = "列测量结果已省略（表太宽），plan 里已包含选定的列。"
            blob = _json.dumps(payload, ensure_ascii=False, default=str)
        return blob

    return plot_data


def _make_audit_figure(cfg) -> "Callable[..., str]":
    """造 `audit_figure` 工具的执行体。

    返回值刻意是**紧凑 JSON**：第 8 条图面门要 `json.loads` 它来判定
    （和证据门只认台账同一个路子 —— 不采信模型的复述）。
    全文报告写盘，路径一并返回，人要细看时去看文件。

    **超长保护**：注册表会把超过 `truncate` 的结果截断，一旦截断 JSON 就解析不了。
    所以这里先量一下长度，超了就丢掉人读的那一段、只留机器读的字段 ——
    宁可让模型少看一段散文，也不能让门拿到半截 JSON（那会变成"审计跑了却判不了"）。
    """
    def audit_figure(contract: dict, pdf: str = None, source: str = None,
                     layout_manifest: str = None, legend: str = None,
                     legend_path: str = None) -> str:
        if not isinstance(contract, dict):
            raise RuntimeError("contract 必须是一个对象（见工具说明里的字段清单）。")
        import json as _json
        from .figure import FigureAuditor, FigureContract

        fc = FigureContract.from_dict(contract)
        # 相对路径按**进程 cwd** 解析 —— 任务期间它就是本次 workspace
        # （loop.run() 会 chdir 进去），所以产物只会落在围栏内。
        wd = Path.cwd()
        au = FigureAuditor(python_exe=getattr(cfg, "python", "") or None,
                           skills_dir=getattr(cfg, "skills_dir", "") or None,
                           timeout=float(getattr(cfg, "timeout", 300)))
        rep = au.audit(fc, workdir=wd, source=source, pdf=pdf,
                       layout_manifest=layout_manifest,
                       legend_text=legend or "", legend_path=legend_path)

        payload = rep.summary_json()
        payload["report_text"] = rep.format_for_model()
        payload["auditor"] = au.diag()
        blob = _json.dumps(payload, ensure_ascii=False, default=str)
        if len(blob) > 9000:
            payload.pop("report_text", None)
            payload["note"] = "报告已过长，人读版见 report_path。"
            blob = _json.dumps(payload, ensure_ascii=False, default=str)
        return blob

    return audit_figure


# ══════════════════════ 工具按领域按需挂载 ══════════════════════
# ## 为什么必须做
#
# 加进 PPT 服务端那一刻，工具数从 29 涨到 66、schema 从 15,562 字符涨到
# **50,244**（实测 2026-09-21，`runs/_regress/_probe_domains.py`）。拆开看：
#
#   ppt       37 个  34,461 字符  ← 占全部 **68.6%**
#   research  21 个   9,767 字符
#   figure     4 个   3,422 字符
#   core       4 个   2,594 字符
#
# 而 `_loop` 每次调用都要把这坨 schema 原样重发（它是前缀缓存里最大的稳定段）。
# 给一场「写一段引言」的任务塞 37 个 PPT 工具是纯噪声：既撑大输入，
# 又给模型更多点错的机会。
#
# ## 判据只此一处
#
# 挂载的输入是 `TaskContract.needs_deck`（与第 9 条硬门**同源**），
# 外加一条超集：阶段里有 `slides` 也算。超集方向是**安全**的 ——
# 保证「门会拦的情形，工具一定已经挂上了」。有不变量断言钉住这条
# （`deck_test.py` 的「needs_deck ⇒ ppt 域必在 allow 里」）。
#
# ## 刻意不动别的领域
#
# `figure` 域只有 3,422 字符（6.8%），且 `audit_figure` 是第 8 条硬门**点名要用**
# 的工具 —— 裁它风险大、收益小。所以本次只裁 ppt 一个域：除 ppt 外的行为
# 与加 PPT 之前**一字不差**（可回退、可测量）。
# ── 课题上下文（导师模式 P1/P2，2026-09-22）──────────────────
# 详见 `topic.py` 模块头：为什么「课题」必须跨会话存在、为什么卡点台账是 append-only、
# 以及**为什么第一版的「相似度过滤」被推翻** —— 阈值要靠真实语料标定，而今天
# 一条卡点语料都还没有 ⇒ 任何阈值都是猜（「打红面 99.6% 与 0% 是同一种失败」）。
# 现在是无阈值的**全量浮现**：拦沉默，不拦选择。
TOPIC_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "课题名。留空 = 当前课题（topics/CURRENT 指向的那个）"},
        # ── 以下是**加**的（W28）：一份有界文档索引 + 一窗正文 + 卡点按状态筛。
        #    全部可省，省略时行为与旧版一字不差（旧调用方不受影响）。
        "doc": {"type": "string", "enum": ["plan", "reading", "evidence"],
                "description": "（可选）顺带取这份活文档的正文。省略 = 只给三份文档的"
                               "有界索引（有没有、多少字、截断没截断）。"},
        "offset": {"type": "integer",
                   "description": "（可选）取 `doc` 正文的起始**字符**位置，默认 0"},
        "limit": {"type": "integer",
                  "description": "（可选）卡点最多列几条，默认 10；配 `doc` 时是正文窗口字数"},
        "status": {"type": "string", "enum": ["open", "resolved", "all"],
                   "description": "（可选）卡点按状态筛，默认 all（与旧版一致）"},
    },
}

TOPIC_USE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "课题名（字母/数字/下划线/短横/点/中文，≤48 字）。已存在则切过去"},
        "title": {"type": "string",
                  "description": "人类可读的标题（只在新建时生效；已存在则原样保留，绝不覆盖）"},
        "question": {"type": "string",
                     "description": "这个课题要回答的科学问题（只在新建时生效）"},
    },
    "required": ["slug"],
}

TOPIC_LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "课题名。**必须显式传** —— 关联是改盘操作，不许靠「上次用的那个」"},
        "doc_id": {"type": "string",
                   "description": "文献 id（kbXXXXXXX）。必须来自 kb_search / kb_list 的真实返回，不要自己拼"},
        "linked": {"type": "boolean",
                   "description": "true = 关联到本课题（默认）；false = 解除关联"},
    },
    "required": ["slug", "doc_id"],
}

TOPIC_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "要改的课题名。**必须显式传**"},
        "title": {"type": "string", "description": "课题名称（≤160 字）。留空 = 不改，**不会被清掉**"},
        "question": {"type": "string", "description": "研究问题（≤1200 字）。留空 = 不改"},
        "stage": {"type": "string",
                  "description": "工作阶段：ideate（构思评估）/ design（研究设计）/ "
                                 "evidence（文献与证据）/ execute（实验与写作）/ submission（投稿与回复）"},
        "next_step": {"type": "string",
                      "description": "用户记下的下一步（≤240 字）。留空字符串 = 清空这一步"},
    },
    "required": ["slug"],
}

RAISE_BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "what": {"type": "string",
                 "description": "卡在哪 —— 一句话说清**具体的卡点**，"
                                "不要写「做了很多尝试」这种等于没写的话"},
        "tried": {"type": "array", "items": {"type": "string"},
                  "description": "已经试过什么（每条一句）。**这是下次最值钱的信息**，别省"},
        "where": {"type": "string",
                  "description": "（可选）卡在哪一步 / 哪个文件 / 哪条命令"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["what"],
}

RESOLVE_BLOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "blocker_id": {"type": "string", "description": "卡点 id，如 B003（从 topic_status 里拿）"},
        "how": {"type": "string", "description": "是怎么解决的 —— 写清手法，下次撞上同一个坑能直接照抄"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["blocker_id"],
}

TOPIC_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "doc": {"type": "string", "enum": ["plan", "reading", "evidence"],
                "description": "plan = 计划与技术路线 / reading = 读过的文献及从它拿到什么 / "
                               "evidence = 证据表（哪条结论由哪篇支撑）"},
        "text": {"type": "string", "description": "要写的正文"},
        "mode": {"type": "string", "enum": ["append", "replace"],
                 "description": "append（默认，追加）或 replace（整篇覆盖）"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["doc", "text"],
}

# ★ 实验台账（2026-11-02）。为什么单独立一张 schema 而不是复用 topic_write：
#   `topic_write` 写的是**自由文本**（PLAN/READING/EVIDENCE 都是散文），
#   而"没数据"这件事只有**结构化**才判得出来 —— 定性观察与定量测量必须是
#   两个不同的 kind，代码才能算出 `measures==0 and observes>0`。
#   一旦允许它们混在一个自由文本字段里，这个式子永远为假，工具就会一直说
#   "你已经有数据了"，而导师那句"没数据"还在那儿。
#   扁平字段是刻意的：模型填嵌套 object 的准确率明显更低。
LAB_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["strain", "batch", "measure", "observe", "metric", "todo",
                          "plan", "experiment", "progress", "update", "close", "retract"],
                 "description":
                     "记什么：\n"
                     "strain=建株（code 必填，role=parent/fusant/candidate）\n"
                     "observe=**定性**观察（「长得慢」「看起来快」，不算数据）\n"
                     "measure=**定量**测量（metric+value+unit+n；value 必须是数字）\n"
                     "batch=发酵批次（id/started/expect_days/temp_c）\n"
                     "metric=注册新指标（key/label/unit，之后才允许 measure 用它）\n"
                     "todo=待办/随访任务（id/what/due/follow_up_at/priority）\n"
                     "plan=实验计划（title/raw_text/start_date/target_date）\n"
                     "experiment=实验步骤/阶段任务（title/phase/started/due/follow_up_at/status/progress）\n"
                     "progress=更新某条实验/待办/批次的进度（id + progress/status/note/next_follow_up）\n"
                     "update=改已有记录（id + fields）\n"
                     "close=闭环（id + how）\n"
                     "retract=**作废**一条记错/记重的事件（seq 或 target + reason）"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
        "code": {"type": "string", "description": "菌株编号，如 S01（strain/observe/measure 必填）"},
        "species": {"type": "string", "description": "（可选）物种，如 Hirsutella sinensis"},
        "role": {"type": "string", "enum": ["parent", "fusant", "candidate"],
                 "description": "（可选）parent=亲本（快慢的参照系）/ fusant=融合子 / candidate=候选"},
        "id": {"type": "string", "description": "批次号 B01 / 待办号 T01 / 实验号 E01 / progress、update、close 要改的那条 id"},
        "title": {"type": "string", "description": "（plan/experiment）实验或计划名称"},
        "phase": {"type": "string", "description": "（experiment）所属阶段"},
        "objective": {"type": "string", "description": "（plan/experiment）实验目的"},
        "protocol": {"type": "string", "description": "（experiment）关键操作或对照设置"},
        "raw_text": {"type": "string", "description": "（plan）完整实验计划文本"},
        "start_date": {"type": "string", "description": "（plan）起始日期 YYYY-MM-DD"},
        "target_date": {"type": "string", "description": "（plan）目标完成日期 YYYY-MM-DD"},
        "follow_up_at": {"type": "string", "description": "（experiment/todo）下次跟进日期 YYYY-MM-DD"},
        "follow_up_days": {"type": "integer", "description": "（experiment）几天后跟进"},
        "next_follow_up": {"type": "string", "description": "（progress）重新排定的下次跟进日期 YYYY-MM-DD"},
        "priority": {"type": "string", "enum": ["high", "normal", "low"],
                     "description": "（experiment/todo）优先级"},
        "progress": {"type": "integer", "description": "（experiment/progress/todo）完成百分比 0–100"},
        "status": {"type": "string", "description": "（experiment/progress/plan）状态：planned/running/blocked/done/dropped"},
        "plan_id": {"type": "string", "description": "（experiment/todo）所属实验计划 id"},
        "experiment_id": {"type": "string", "description": "（todo）关联实验 id"},
        "metric": {"type": "string",
                   "description": "指标 key：growth_rate / biomass / mannitol / adenosine / "
                                  "ferment_period（或先用 kind=metric 注册的 key）"},
        "value": {"type": "number", "description": "测量值，**必须是数字**（定性判断用 observe）"},
        "unit": {"type": "string", "description": "单位，须与指标要求一致（g/100mL、%、mg/g、h、mm/d）"},
        "n": {"type": "integer", "description": "重复数。n=1 是观察，不是重复"},
        "sd": {"type": "number", "description": "（可选）标准差"},
        "method": {"type": "string", "description": "（可选）测量方法一句话"},
        "batch": {"type": "string", "description": "（可选）这条测量属于哪个批次"},
        "started": {"type": "string", "description": "（batch/experiment）起始日 YYYY-MM-DD"},
        "expect_days": {"type": "integer",
                        "description": "（batch）预计发酵天数；到点会进「下一步」第一条（样品有时效）"},
        "vessel": {"type": "string", "description": "（batch）容器，如 250mL 摇瓶 / 5L 罐"},
        "medium": {"type": "string", "description": "（batch）培养基，如 PDB"},
        "rpm": {"type": "integer", "description": "（batch）转速 r/min"},
        "temp_c": {"type": "number", "description": "（可选）培养温度 ℃"},
        "growth": {"type": "string", "description": "（可选）observe 的定性判断：slow / fast / normal"},
        "text": {"type": "string", "description": "observe 的原话（用户怎么描述的，照抄）"},
        "evidence": {"type": "string", "description": "（可选）这条观察的依据：照片/日期/平板号"},
        "what": {"type": "string", "description": "todo 的内容"},
        "due": {"type": "string", "description": "（可选）截止日 YYYY-MM-DD"},
        "how": {"type": "string", "description": "close 时：是怎么完成的/怎么解决的"},
        "note": {"type": "string", "description": "（可选）备注 / 进度说明"},
        "fields": {"type": "object",
                   "description": "（仅 update）要改的字段字典，如 {\"note\": \"已补生长速率\"}"},
        "placeholder": {"type": "boolean",
                        "description": "（strain，可选）这条株号是不是**占位**（你还没给真实株号时用 "
                                       "S01/S02 这类临时名）。标了之后汇报页会加 `*` 并注明，"
                                       "免得把临时编号当成真株号念给导师听"},
        "seq": {"type": "integer",
                "description": "（仅 retract）要作废第几条事件（#1 起，按追加顺序）。"
                               "不知道序号就先给 target，报错里会把候选的 #seq 列出来"},
        "target": {"type": "object",
                   "description": "（仅 retract）按字段匹配要作废的那条，如 "
                                  "{\"kind\":\"observe\",\"code\":\"S01\"}。"
                                  "**必须恰好匹配一条** —— 匹配到多条会报错并列出来，"
                                  "因为作废不能「顺手」批量"},
        "reason": {"type": "string",
                   "description": "（retract 必填）为什么作废，如「脚本重复写入，同一条记了 5 遍」"},
    },
    "required": ["kind"],
}

LAB_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
        "title": {"type": "string", "description": "（可选）实验计划名称"},
        "objective": {"type": "string", "description": "（可选）实验总体目标"},
        "raw_text": {"type": "string", "description": "用户提交的实验计划全文（支持分条/Markdown/含天数或日期）"},
        "start_date": {"type": "string", "description": "（可选）计划开始日期 YYYY-MM-DD，默认今天"},
        "auto_schedule": {"type": "boolean", "description": "（可选）是否自动为每步排定后续跟进待办，默认 true"},
        "default_follow_up_days": {"type": "integer", "description": "（可选）未指定天数时每步默认间隔几天，默认 3"},
        "steps": {
            "type": "array",
            "description": "（可选）若模型已拆好步骤，可直接传结构化步骤数组；否则由内核确定性解析 raw_text",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "phase": {"type": "string"},
                    "protocol": {"type": "string"},
                    "duration_days": {"type": "integer"},
                    "due": {"type": "string"},
                    "follow_up_at": {"type": "string"},
                    "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                },
                "required": ["title"],
            },
        },
    },
}

LAB_REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
}

LAB_METHOD_SCHEMA = {
    "type": "object",
    "properties": {
        "need": {"type": "string",
                 "description": "要哪张方法卡。三选一（也可以直接传用户原话，例如「核质染色怎么做」）："
                                "staining = 核质染色 / DAPI / 荧光 / 核相观察的参数与失败分诊；"
                                "identity = 怎么证明这几株是融合子（身份证据链，按成本排）；"
                                "quant = 该测哪些定量指标、单位、n、参照值（「没数据」缺哪个数字）"},
        "slug": {"type": "string", "description": "（可选）课题名，留空 = 当前课题"},
    },
    "required": ["need"],
}


OUTLINE_SAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "课题名（**必填**，大纲是改盘操作，不靠「上次用过的课题」）"},
        "title": {"type": "string", "description": "课题名。留空 = 沿用已有的"},
        "question": {"type": "string", "description": "研究问题。留空 = 沿用已有的"},
        "thesis": {"type": "string", "description": "一句话主张：这篇论文最后要证明的那句话"},
        "sections": {
            "type": "array",
            "description": "章节。**每一节必须写清它靠什么数据成立**（needs）。"
                           "没有数据需求的章节会被判成「这一节现在写不出来」。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "短 id（字母/数字/下划线/短横），实验靠它指回来"},
                    "title": {"type": "string", "description": "章节标题"},
                    "kind": {"type": "string", "enum": ["intro", "method", "result", "discussion"],
                             "description": "章节类型。只用来查漏（四类里有没有整类缺失），不用来填内容"},
                    "intent": {"type": "string", "description": "这一节要说服读者什么"},
                    "needs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string",
                                         "enum": ["table", "figure", "stat", "image", "text"],
                                         "description": "table/图/一个数/image/定性描述"},
                                "what": {"type": "string", "description": "要的是什么数据。"
                                                                   "写到能直接去做实验的程度"},
                                "status": {"type": "string",
                                           "enum": ["missing", "planned", "have"],
                                           "description": "missing=还没有；planned=已排进实验；"
                                                          "have=已有（**会去实验台账里核**，核不上就降为 unverified）"},
                                "unit": {"type": "string", "description": "单位（%, 栊, mg/L …）"},
                                "source": {"type": "string", "description": "用哪个数据集/实验算出来的"},
                                "note": {"type": "string"},
                            },
                            "required": ["kind", "what"],
                        },
                    },
                },
                "required": ["id", "title", "kind"],
            },
        },
        "experiments": {
            "type": "array",
            "description": "计划做的实验。**每个实验必须说清供哪几节的数据**（feeds），"
                           "一处都不供的实验会被判成「做了不知道写进哪」。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "短 id"},
                    "title": {"type": "string", "description": "实验名"},
                    "status": {"type": "string", "enum": ["planned", "running", "done", "dropped"],
                               "description": "done 只是**声明**完成；数据真有没有看实验台账"},
                    "feeds": {"type": "array", "items": {"type": "string"},
                              "description": "供哪几节的数据（节的 id）"},
                    "method": {"type": "string", "description": "怎么做（参数从方法卡来，不要凭常识填）"},
                    "controls": {"type": "string", "description": "对照组怎么设 —— 这一栏空着的实验，"
                                                              "在组会上第一个问题就是「对照组是谁」"},
                    "n": {"type": "string", "description": "重复数 / 样本量"},
                    "days": {"type": "integer", "description": "预计要多少天"},
                    "depends_on": {"type": "array", "items": {"type": "string"},
                                   "description": "先做哪个（实验 id）"},
                    "note": {"type": "string"},
                },
                "required": ["id", "title", "feeds"],
            },
        },
    },
    "required": ["slug", "sections"],
}


OUTLINE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "留空 = 当前课题"},
    },
}


METHOD_RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string",
                 "enum": ["method", "version", "observe", "retract", "patent"],
                 "description": "method=新建方法 / version=记一个新版本 / "
                                "observe=这次跑的结果 / retract=作废某一条 / "
                                "patent=登记专利线索（需 id + disclosed_at，"
                                "first_public 能填就填）"},
        "id": {"type": "string", "description": "方法 id（字母/数字/下划线/短横，≤48）。"
                                             "中文名放 title，不要进 id。"},
        "slug": {"type": "string", "description": "留空 = 当前课题"},
        "title": {"type": "string", "description": "kind=method 必填：这个方法叫什么"},
        "origin": {"type": "string", "enum": ["literature", "adapted", "own", "standard"],
                   "description": "kind=method 必填。literature=照文献 / "
                                  "adapted=文献+改了参数 / own=自己设计 / "
                                  "standard=标准或试剂盒说明书"},
        "version": {"type": "integer", "description": "kind=version 必填：第几版（≥1，"
                                                     "**不能复用**）"},
        "params": {"type": "array", "description": "参数表。**每项必填 src** "
                                                  "—— 没有出处的参数下一轮会被当成事实复述。",
                   "items": {"type": "object", "properties": {
                       "name": {"type": "string"},
                       "value": {"type": "string", "description": "写成什么值，带单位"},
                       "src": {"type": "string",
                               "description": "`kb:<doc_id> p.<页>` / `user` / `design` / `std:<标准号>`"}},
                       "required": ["name", "value", "src"]}},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "一步一句，带量与时长"},
        "pitfalls": {"type": "array", "items": {"type": "string"},
                     "description": "每条一句：会怎么失败、怎么认出来"},
        "differs_from": {"type": "array", "items": {"type": "string"},
                         "description": "这一版区别于哪些方法（id 或 kb:doc_id）。"
                                        "非空时 what_changed 必填。"},
        "what_changed": {"type": "string",
                         "description": "跟上一版/文献差在哪 —— 要说改了哪个参数、"
                                        "从多少改到多少。「我这版更好」不算。"},
        "text": {"type": "string", "description": "kind=observe 必填：这次发生了什么"},
        "worked": {"type": "boolean", "description": "observe：成了？"},
        "failed": {"type": "boolean", "description": "observe：失败了？"},
        "seq": {"type": "integer", "description": "kind=retract：要作废的那一条的 seq。"
                                                 "✔ 应当用 `why` 说明理由。"},
        "why": {"type": "string", "description": "kind=retract：为什么作废"},
        "disclosed_at": {"type": "string",
                         "description": "kind=patent：你把这件事整理成专利线索的日期"},
        "first_public": {"type": "string",
                         "description": "kind=patent：**首次公开**（论文投稿/预印/开会）。"
                                        "这个日期决定新颖性还在不在 —— "
                                        "不知道就留空，别猜"},
        "claims_basis": {"type": "array", "items": {"type": "string"},
                         "description": "kind=patent：你认为可以主张的点（每条一句）"},
    },
    "required": ["kind"],
}

METHOD_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "留空 = 当前课题"},
        "id": {"type": "string", "description": "给了就只回这一个方法（并附创新点视图）；"
                                            "留空 = 列全部方法的摘要。"},
    },
}


PATENT_LEADS_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "方法 id"},
        "slug": {"type": "string", "description": "留空 = 当前课题"},
    },
    "required": ["id"],
}


PLATE_QC_SCHEMA = {
    "type": "object",
    "properties": {
        "values": {"type": "object",
                   "additionalProperties": {"type": "string"},
                   "description": "孔号 -> 读数，如 {\"A1\": \"1.234\"}。"
                                  "直接把读板机导出的格子原样抄进来：数字、ND、<0.05、NA "
                                  "都收 —— 怎么解释是这只手套的事，调用方不用先清洗"},
        "layout": {"type": "object",
                   "description": "对照组：{\"high\": [\"A1\", ...], \"low\": [\"H1\", ...]}。"
                                  "high=高值/阳性对照，low=低值/空白对照；两边都有才算得出 Z'"},
        "slug": {"type": "string", "description": "课题 slug，可省"},
    },
    "required": ["values", "layout"],
}

ADVISOR_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "留空 = 列出全部导师模型；给了就读这一个（含读回校验：哪层空、"
                                "素材充分度多少、有没有全是模板）。"},
    },
}

ADVISOR_SAVE_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string",
                 "description": "导师模型名（小写字母/数字/连字符；中文名用 slugify 后的形式）。"},
        "name": {"type": "string", "description": "导师别名（建议用化名，别写真实姓名）。"},
        "layers": {
            "type": "object",
            "description": "四层内容，Markdown 正文。**只能写从用户给的材料里挖出来的东西**："
                           "没挖到就写一行「未挖到材料」（合法），"
                           "不要把你自己想像的「导师会问什么」写成 ^source=user^"
                           "—— 那是替他编了一句他没说过的话。"
                           "**每条事实后面必须标 source=**："
                           "user（他亲口说的/当场纠偏）/ material（从批注组会聊天里读出来的）/ "
                           "template（通用模板补的，**必须标出来**）/ merged。"
                           "没有 source= 的层会被读回判成不可用。",
            "properties": {
                "method_core": {"type": "string", "description": "怎么推进：任务拆解/优先级/风险分级/兜底"},
                "academic": {"type": "string", "description": "做什么：选题标准/实验规范/论文标准/里程碑/伦理红线"},
                "persona": {"type": "string", "description": "怎么问：他会先问哪几类问题/在哪一步不耐烦/什么必须当场答清"},
                "playbook": {"type": "string", "description": "怎么过线：最小可交付/保底方案/审稿最小改动"},
            },
        },
        "mode": {"type": "string", "enum": ["academic_ideal", "graduation_first"],
                 "description": "工作镜头。二选一，不选默认 academic_ideal。"
                                "它不是语气，是**判准**：学术理想型重严谨与长期价值，"
                                "毕业优先型重可交付与保底。两者给出的建议会不一样。"},
        "strategy": {"type": "string", "enum": ["strict_distill", "hybrid_distill", "template_first"],
                     "description": "素材怎么用。素材充分度 < 40 时别声明 strict_distill（自相矛盾）。"},
        "sufficiency": {"type": "integer",
                        "description": "素材充分度 0-100。老实填：填高了会让模板内容冒充他本人的判准。"},
        "sources": {"type": "array", "items": {"type": "string"},
                    "description": "导入过哪些材料（文件名或哈希），便于事后核对。"},
    },
    "required": ["slug"],
}

ADVISOR_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "导师模型名"},
        "layer": {"type": "string",
                  "enum": ["all", "method_core", "academic", "persona", "playbook"],
                  "description": "读哪一层，默认 all"},
        "max_chars": {"type": "integer", "description": "单层最多读多少字，默认 20000"},
    },
    "required": ["slug"],
}


def _make_topic_tools(root: "Path | str | None" = None) -> dict:
    """返回 10 个课题工具的执行体。`root` 显式传入 ⇒ 单测可指向临时目录，不碰真账。

    ★ **失败一律 raise，不返回 ok=false 的 JSON。**
      理由本仓记过：注册表把「返回字符串」记成**成功调用**（ok=True），
      于是 payload 里那个 ok=false 会滑过证据门 —— `_load_skill` 当初就是为此改成抛错的。
      抛错会走 `[工具执行失败]` 分支回灌给模型，可读性一点不损失。
    """
    from . import topic as T

    root = T.topics_root(root)

    def _cur(slug: str) -> str:
        s = (slug or "").strip() or T.current(root=root)
        if not s:
            raise RuntimeError(
                "还没有当前课题（topics/CURRENT 是空的）。"
                "先用 topic_use(slug=…, title=…, question=…) 建一个，或显式传 slug。")
        return s

    def topic_status(slug: str = "", doc: str = "", offset: int = 0,
                     limit: int = 0, status: str = "") -> str:
        # ★ W28：折叠逻辑搬到 `topic.status_view()` —— 模型工具与 Web 面板
        #   现在读的是**同一份状态**。此前这套"过滤 / 截断 / 计数"长在这个闭包里，
        #   面板要同一件事的另一半（文档正文、按状态筛）就只能再实现一遍；
        #   两份实现对"还有几条没列出来"迟早会各说一套（H 型）。
        #   这里只留**塑形**（把台账原始键映射成工具契约里的键名）。
        s = (slug or "").strip() or T.current(root=root)
        # `offset`/`limit` 是**一个窗口**,落在"这一趟最想要的东西"上：
        # 传了 `doc` 就是文档正文的字符窗口，否则是卡点分页。
        # 不让它同时管两件事 —— 一个参数一处语义，否则调用方永远猜不准。
        want_doc = bool((doc or "").strip())
        off = max(0, int(offset or 0))
        lim = max(0, int(limit or 0))
        try:
            st = T.status_view(
                s, root=root, status=status,
                offset=0 if want_doc else off,
                limit=(lim or T.BLOCKER_PAGE) if not want_doc else T.BLOCKER_PAGE,
                doc=doc, doc_offset=off,
                doc_limit=(lim or T.DOC_WINDOW) if want_doc else T.DOC_WINDOW)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        if not st["slug"]:
            return json.dumps(st, ensure_ascii=False)
        st["blockers"] = [{"id": b["id"], "status": b["status"], "at": b["opened_at"],
                           "what": b["what"], "tried": b["tried"],
                           "resolved_how": b["resolved_how"]} for b in st["blockers"]]
        st.pop("items", None)     # 工具契约里历史上只有 `blockers`；面板用 `blocker_page`
        return json.dumps(st, ensure_ascii=False)

    def topic_use(slug: str, title: str = "", question: str = "") -> str:
        s = (slug or "").strip()
        if not s:
            raise RuntimeError("slug 为空")
        if not T.valid_slug(s):
            raise RuntimeError(f"非法课题名: {s!r}（只收字母/数字/下划线/短横/点/中文，≤48 字）")
        existed = T.read_topic(s, root=root) is not None
        m = T.ensure_topic(s, title=title, question=question, root=root)
        T.set_current(s, root=root)
        return json.dumps({"ok": True, "slug": s, "created": not existed,
                           "title": m.get("title"), "question": m.get("question"),
                           "current": T.current(root=root),
                           "note": "已切到本课题。它的未闭环卡点会出现在每个新任务的第一条消息里。"},
                          ensure_ascii=False)

    def raise_blocker(what: str, tried=None, where: str = "", slug: str = "") -> str:
        s = _cur(slug)
        where = (where or "").strip()
        r = T.add_blocker(s, what, tried or [], where=where, root=root)
        return json.dumps(
            {"ok": r["ok"], "verified": r["verified"], "id": r["id"], "slug": s,
             "open_total": r["open_total"],
             "note": "已记进卡点台账。**这是记账、不是收工** —— 继续推进；"
                     "确实做不成才用 report_blocker。"},
            ensure_ascii=False)

    def resolve_blocker(blocker_id: str, how: str = "", slug: str = "") -> str:
        s = _cur(slug)
        r = T.resolve_blocker(s, blocker_id, how=how, root=root)
        return json.dumps({"ok": r["ok"], "verified": r["verified"], "id": r["id"],
                           "slug": s, "already": bool(r.get("already")),
                           "open_total": r["open_total"]}, ensure_ascii=False)

    def topic_write(doc: str, text: str, mode: str = "append", slug: str = "") -> str:
        # ★ W27 修正：**没传 slug 时不再静默兜到 current()**。
        #   实测事故（W27 第二轮真实运行）：CURRENT 还停在上一轮的
        #   `cordyceps-cultivation`（主题是「冬虫夏草人工培育难点」），
        #   而本轮课题是原生质体融合选育 —— 模型连调 6 次 `topic_write`
        #   全部不传 slug，于是**一整轮的原生质体融合证据表被写进了另一个课题的
        #   EVIDENCE.md**，而每次回包都是 `ok:true, slug:cordyceps-cultivation`，
        #   看着完全正常。它是本次运行里唯一一个**静默毁数据**的缺陷。
        #
        #   为什么不能只靠"模型该自己传 slug"：`current()` 的语义是
        #   「**上次**用的课题」，跨会话残留；而"这轮在写哪个课题"是**本轮**的
        #   事实。拿跨会话的残留当本轮的默认值，就是在允许它们悄悄不一致。
        #
        #   改法：写台账是**改盘**操作，意图必须**明示**。没传 slug 就报错，
        #   并把当前课题与全部课题列出来（模型据此自己决定传哪个）。
        #   `topic_status` / `kb_*` 这些**读**操作不受影响 —— 读错地方代价小，
        #   写错地方代价是数据被搅在一起。
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "topic_write 必须显式传 slug —— 台账是改盘操作，"
                f"不许默默写进「上次用过的课题」。当前 current()={T.current(root=root)!r}；"
                f"已知课题：{known}。"
                "如果本轮课题不在其中，先 topic_use(slug=…, title=…, question=…) 建/切过去。")
        if T.read_topic(s, root=root) is None:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"课题不存在: {s!r}。已知课题：{known}。")
        r = T.write_doc(s, doc, text, mode=mode, root=root)
        return json.dumps({"ok": r["ok"], "verified": r["verified"],
                           "doc": r["doc"], "chars": r["chars"], "slug": s},
                          ensure_ascii=False)

    def topic_link(slug: str = "", doc_id: str = "", linked: bool = True) -> str:
        # ★ 与 `topic_write` 同一条纪律：**改盘操作必须显式传 slug**。
        #   理由不是形式主义：`current()` 是「**上次**用的课题」，跨会话残留；
        #   「这篇文献属于这轮哪个课题」是本轮的事实。拿残留当默认值，
        #   就是允许两者悄悄不一致 —— W27 那次一整轮证据表写进另一个课题，
        #   根因正是这个。关联比写台账轻，但错挂的代价同样要事后人工翻查。
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "topic_link 必须显式传 slug —— 关联是改盘操作，"
                f"不许默默挂到「上次用过的课题」。当前 current()={T.current(root=root)!r}；"
                f"已知课题：{known}。")
        try:
            r = T.set_library(s, doc_id, bool(linked), root=root)
        except KeyError as e:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"{e}。已知课题：{known}。") from None
        # ★ 只回 `count` 不回全量 id 列表：某个课题挂着 172 篇时，全量回包会
        #   把一次"挂一篇"变成几 KB 噪音，而模型真正要确认的只有"现在一共几篇"。
        #   要看全量有 `topic_status`（它的 `library` 是完整列表）。
        return json.dumps({"ok": r["ok"], "slug": s, "doc_id": r["doc_id"],
                           "linked": r["linked"], "count": len(r["library"])},
                          ensure_ascii=False)

    def topic_update(slug: str = "", title=None, question=None,
                     stage=None, next_step=None) -> str:
        """改课题元数据（名称/研究问题/阶段/下一步）。

        ★ 为什么要这一层包装，而不是直接把 `T.update_topic` 给模型：
          它的 `title` / `question` 是**必填位置参数**，内部无条件
          `old.update(title=title.strip(), question=question.strip())`。
          于是"我只想把阶段改成 evidence"这句，模型按 schema 传空串，
          就会把**课题标题和研究问题一起清空** —— 又是一次静默毁数据
          （与 W27 那次写错课题同型：回包 ok、内容没了）。
          所以这里的口径是：**空 = 没打算改，不是清空**。

        ★ `next_step` 是例外，故意允许清空：它是"用户记下的下一步"，
          「这一步做完了，清掉」是真实意图。传 `""` = 清空；不传（None）= 不改。
        """
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "topic_update 必须显式传 slug —— 改盘操作不许靠「上次用过的课题」。"
                f"当前 current()={T.current(root=root)!r}；已知课题：{known}。")
        cur = T.read_topic(s, root=root)
        if cur is None:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"课题不存在: {s!r}。已知课题：{known}。")

        # 空串一律按"不改"处理；只有 next_step 的显式 "" 表示清空。
        nt = (title or "").strip() or (cur.get("title") or "")
        nq = (question or "").strip() or (cur.get("question") or "")
        st = (stage or "").strip() or None
        if next_step is None:
            nx = None
        else:
            nx = str(next_step).strip()      # "" ⇒ 清空，属显式意图
        if not any([(title or "").strip(), (question or "").strip(), st,
                    next_step is not None]):
            raise RuntimeError("什么都没给：title / question / stage / next_step "
                              "至少给一个要改的，否则这一趟是空转。")
        try:
            out = T.update_topic(s, nt, nq, root=root, stage=st, next_step=nx)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        return json.dumps({"ok": True, "slug": s,
                           "title": out.get("title"), "question": out.get("question"),
                           "stage": out.get("stage"), "next_step": out.get("next_step")},
                          ensure_ascii=False)

    def lab_record(kind: str, slug: str = "", code: str = "", id: str = "",
                   metric: str = "", value=None, unit: str = "", n=None, sd=None,
                   species: str = "", role: str = "", method: str = "", batch: str = "",
                   started: str = "", expect_days=None, vessel: str = "",
                   medium: str = "", rpm=None,
                   temp_c=None, growth: str = "", text: str = "", evidence: str = "",
                   what: str = "", due: str = "", how: str = "", note: str = "",
                   title: str = "", phase: str = "", objective: str = "",
                   protocol: str = "", raw_text: str = "", start_date: str = "",
                   target_date: str = "", follow_up_at: str = "",
                   follow_up_days=None, next_follow_up: str = "",
                   priority: str = "", progress=None, status: str = "",
                   plan_id: str = "", experiment_id: str = "",
                   fields=None, placeholder=None, seq=None, target=None,
                   reason: str = "") -> str:
        """记一条实验事实。**这是"下一步"唯一的依据来源**。

        ★ 为什么 observe 与 measure 是两个 kind，而不是一个 kind 加布尔位：
          「老师说我没数据」这句话必须在代码里有一个形态。那个形态就是
          `len(measures)==0 and len(observes)>0` —— 它要求定性观察
          **写不进**定量字段。合成一个 kind，这个式子永远为假，
          工具就会一直安慰"你已经有数据了"。
          所以 `measure.value` 非数字一律**抛错**，而不是存成字符串。

        ★ 为什么失败抛错而不是回 ok:false：本仓老债 —— 注册表把"返回字符串"
          记成成功调用，payload 里的 ok:false 会滑过证据门。
        """
        from . import lab as L
        s = _cur(slug)
        kw = dict(code=code, species=species, role=role, id=id, metric=metric,
                  value=value, unit=unit, n=n, sd=sd, method=method, batch=batch,
                  started=started, expect_days=expect_days, vessel=vessel,
                  medium=medium, rpm=rpm,
                  temp_c=temp_c, growth=growth, text=text, evidence=evidence,
                  what=what, due=due, how=how, note=note, reason=reason,
                  title=title, phase=phase, objective=objective, protocol=protocol,
                  raw_text=raw_text, start_date=start_date, target_date=target_date,
                  follow_up_at=follow_up_at, follow_up_days=follow_up_days,
                  next_follow_up=next_follow_up, priority=priority,
                  progress=progress, status=status, plan_id=plan_id,
                  experiment_id=experiment_id)
        if fields:
            kw["fields"] = fields
        if target:
            kw["target"] = target
        if seq is not None:
            kw["target_seq"] = seq
        if placeholder is not None:
            kw["placeholder"] = bool(placeholder)
        try:
            r = L.record(s, kind, root=root, **{k: v for k, v in kw.items() if v not in (None, "")})
        except (ValueError, KeyError) as e:
            # 把"现在该怎么做"一起说出来 —— 只说"未注册的指标"模型会开始瞎猜。
            raise RuntimeError(f"{e}") from None
        out = {"ok": r["ok"], "verified": r["verified"], "slug": s, "kind": r["kind"],
               "total_events": r["total_events"], "event": r["event"],
               # 作废之后"还剩几条有效"是模型判断后续动作的直接依据 ——
               # 只回原始条数会让它以为账里凭空多了两条（作废记录 + 被作废那条）。
               "live_events": r.get("live_events", r["total_events"]),
               "retracted": r.get("retracted", 0)}
        # 记完立刻回一份**处境摘要**：不这样的话，模型要再调一次 lab_report
        # 才知道自己刚改了什么（多一次往返，且中间那步容易忘）。
        try:
            out["digest"] = L.digest(s, root=root)
            out["gaps"] = len(L.gaps(s, root=root))
        except Exception:                                  # noqa: BLE001
            pass
        return json.dumps(out, ensure_ascii=False)

    def lab_plan(slug: str = "", title: str = "", objective: str = "",
                 raw_text: str = "", start_date: str = "",
                 steps=None, auto_schedule: bool = True,
                 default_follow_up_days: int = 3) -> str:
        """接收用户实验计划，按课题落盘为 plan + experiment + scheduled follow-up todo。"""
        from . import lab as L
        s = _cur(slug)
        try:
            r = L.import_plan(
                s, raw_text=raw_text, title=title, objective=objective,
                start_date=start_date, steps=steps, auto_schedule=bool(auto_schedule),
                default_follow_up_days=int(default_follow_up_days or 3), root=root,
            )
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"{e}") from None
        return json.dumps({
            "ok": r["ok"], "verified": r["verified"], "slug": s,
            "plan_id": r["plan_id"],
            "n_experiments": len(r["experiments"]),
            "n_todos": len(r["todos"]),
            "overall_progress_pct": r["progress"]["overall_progress_pct"],
            "experiments": [{"id": x["id"], "title": x["title"], "due": x.get("due", ""),
                             "follow_up_at": x.get("follow_up_at", "")} for x in r["experiments"]],
            "digest": L.digest(s, root=root),
        }, ensure_ascii=False)

    def lab_report(slug: str = "") -> str:
        """出「组会一页」：`LAB.md`（菌株×指标矩阵 + 缺口 + 下一步 + 方法出处）
        与 `LAB.csv`（tidy 长表，含定性行）。

        两个文件**都写进课题目录**（持久），同时把正文 `md_text` / `csv_text`
        一并返回 —— 这样模型可以再 `write_file("实验台账-<日期>.md", md_text)`
        落到本次 run 的工作区，用户在面板里就能直接看见、下载带去组会。
        为什么要两份：课题目录里那份是**台账的视图**（随时可重生成），
        run 工作区那份是**这次交付的凭据**（不会因为台账更新而变）。
        """
        from . import lab as L
        s = _cur(slug)
        try:
            r = L.write_report(s, root=root)
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"{e}") from None
        acts = L.next_actions(s, limit=5, root=root)
        return json.dumps({
            "ok": r["ok"], "verified": r["verified"], "slug": s,
            "say": r.get("say", ""),
            "md_file": r["md_file"], "csv_file": r["csv_file"], "dir": r["dir"],
            "md_chars": r["md_chars"], "csv_rows": r["csv_rows"],
            "next_actions": acts,
            "md_text": r["md_text"], "csv_text": r["csv_text"],
            "hint": "★ `say` 是**直接给人看的那一段**，贴进回答里（≤3 条动作），"
                    "不要只回一句「已生成 LAB.md」。"
                    "要放进本次交付：write_file(name='实验台账.md', content=md_text)；"
                    "要出图：write_file(name='lab.csv', content=csv_text) 后调 "
                    "plot_data(table='lab.csv')。"
                    "**CSV 里的定性行（evidence_type=qual）不要删**，"
                    "删了图上就看不见那几株。",
        }, ensure_ascii=False)

    def lab_method(need: str, slug: str = "") -> str:
        """出**方法卡**：染色参数 / 身份证据链 / 定量指标标尺。

        ★ 它存在的理由：这三类知识本来就在 `lab_assets` 里，每条都挂着
          `doc_id` + 页码 —— 但**没有任何工具读它，模型永远看不见**。
          资产不进工具面，等于没写（本仓那条定律的第二次复发）。
          所以这个工具只做一件事：**把资产确定性渲染成模型能直接用、能直接念给用户的文本**。
          参数不许模型自己补；它只负责解释、排序、对着台账说「你先做这一步」。

        ★ 为什么把「染色」和「身份证据」「定量指标」放同一个工具：
          三者回答的是同一个问题的三个层次 ——
          「我下一步干什么」（台账排出动作）→「怎么证明这几株是融合子」（身份链）
          →「缺哪个数字」（标尺）→「具体那个方法怎么做」（染色参数）。
          分成三个工具会让模型在「我该调哪个」上多花一次判断，而它们共用同一份资产。
        """
        from . import lab as L
        s = _cur(slug)
        try:
            r = L.write_method_card(s, need, root=root)
        except ValueError as e:
            # 映射不到就说清可用说法 —— 静默给一张不相干的卡比报错坏得多。
            raise RuntimeError(f"{e}") from None
        return json.dumps({
            "ok": r["ok"], "verified": r["verified"], "slug": s,
            "need": r["need"], "also_matched": r["also_matched"],
            "title": r["title"], "file": r["file"], "dir": r["dir"],
            "md_chars": r["md_chars"], "md_text": r["md"],
            "say": r.get("say", ""),
            "hint": "★ `say` 是**直接念给用户的那一段**（3-5 行动作），原样贴进回答，"
                    "不要改写成表格，也不要只回一句「已生成方法卡」。"
                    "卡是资产渲染的，参数照抄、别改写数字。"
                    "用户想留存：write_file(name='方法卡.md', content=md_text)。"
                    "卡里没有的，去 `kb_search` / `kb_ask` 查原文，不要凭空补。",
        }, ensure_ascii=False)

    def outline_save(slug: str = "", title: str = "", question: str = "",
                     thesis: str = "", sections=None, experiments=None) -> str:
        """敲定开题大纲：章节 → 每节要什么数据 → 计划做哪些实验供这些数据。

        ★ 它为什么不是又一份自由文本：课题台账里已经有 PLAN.md，
          再加一份散文大纲只会多一处对不上。这一份的硬要求是
          **每一条数据需求都必须被某个实验供应** —— 线断了就是写不出来。
        ★ 全量替换式保存：模型手上永远有完整一份（outline_status 会整份回给它）。
        """
        from . import outline as O
        s = (slug or "").strip()
        if not s:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(
                "outline_save 必须显式传 slug —— 大纲是改盘操作，不许默默写进"
                f"「上次用过的课题」。当前 current()={T.current(root=root)!r}；"
                f"已知课题：{known}。")
        if T.read_topic(s, root=root) is None:
            known = ", ".join(x["slug"] for x in T.list_topics(root=root)) or "（一个都没有）"
            raise RuntimeError(f"课题不存在: {s!r}。已知课题：{known}。")
        if not sections:
            # 空大纲不是“先占位”，是一个**看着像有、实际全空**的壳（本仓老坑）。
            raise RuntimeError("sections 不能为空 —— 空大纲比没大纲更糟：它让人以为已经计划好了。")
        try:
            r = O.save(s, title=title, question=question, thesis=thesis,
                       sections=sections, experiments=experiments or [], root=root)
        except (ValueError, KeyError) as e:
            raise RuntimeError(f"{e}") from None
        rep = O.reconcile(s, root=root)
        return json.dumps({
            "ok": r["ok"], "verified": r["verified"], "slug": s,
            "sections": r["sections"], "experiments": r["experiments"],
            "needs": r["needs"], "md_file": r["md_file"],
            "carried_orphan_sections": r["carried_orphan_sections"],
            "starved": len(rep.get("starved", [])),
            "orphan_experiments": len(rep.get("orphan_experiments", [])),
            "missing_kinds": [k["label"] for k in rep.get("missing_kinds", [])],
            "say": rep.get("say", ""),
            "next": "★ 马上把对账结果读给用户：哪几节现在拿不出数据、"
                    "哪几个实验做了不知道写进哪。"
                    "要把缺数据排成实验任务：outline_to_lab。",
        }, ensure_ascii=False)

    def outline_status(slug: str = "") -> str:
        """读大纲 + 与实验台账对账。**整份回给模型**（保存是全量替换，它得先有全份）。

        ★ 对账不是“再评一次”：它全部由 outline.json 与实验台账两份盘上的东西复算，
          不含任何模型判断。自称已有数据的条目会去台账里核，核不上就降为
          `unverified` —— 与导师模型那条 `source=` 纪律同源：能核的必须核。
        """
        from . import outline as O
        s = _cur(slug)
        ol = O.load(s, root=root)
        rep = O.reconcile(s, root=root)
        out = {"ok": rep["ok"], "slug": s, "verified": True,
               "exists": ol is not None,
               "why_missing": ("" if ol is not None else O.diagnose(s, root=root)),
               "outline": ol,
               "reconcile": {k: v for k, v in rep.items() if k not in ("ok", "lines")},
               "lines": rep.get("lines", [])}
        if ol is not None:
            out["say"] = O.render_md(s, ol)
        return json.dumps(out, ensure_ascii=False)

    def outline_to_lab(slug: str = "") -> str:
        """把大纲里**还没有数据**的需求，写成实验台账的 todo（幂等）。"""
        from . import outline as O
        s = _cur(slug)
        try:
            r = O.plan_to_lab(s, root=root)
        except RuntimeError as e:
            raise RuntimeError(f"{e}") from None
        return json.dumps({
            "ok": r["ok"], "slug": s,
            "added": r["added"], "skipped": r["skipped"],
            "total_added": r["total_added"],
            "note": "★ 这只写 todo，**不会凭空造出测量** —— 数据必须由真做的实验来，"
                    "用 lab_record(kind='measure') 记。排完发现顺序不对就调大纲，不要手改台账。",
        }, ensure_ascii=False)

    # ── 方法台账 + 创新点（2026-11-15）───────────────────────
    def method_record(kind: str, **kw) -> str:
        """记一个方法 / 新版本 / 一次跑的结果。形状校验全在 `methods._validate`。

        ★ 参数必填 `src=` 与版本号不可复用两条，故意让它们**抛错**而不是返回 ok=false：
          本仓记过——注册表把「返回字符串」记成成功调用，payload 里的 ok=false 会滑过证据门。
        """
        from . import methods as MW
        s = _cur(str(kw.pop("slug", "") or ""))
        r = MW.record(s, kind, root=root, **kw)
        if kind == "method" or kind == "version":
            w = MW.write_md(s, root=root)
            r["md_chars"] = w["md_chars"]
            r["md_verified"] = w["verified"]
        return json.dumps(r, ensure_ascii=False)

    def method_status(slug: str = "", id: str = "") -> str:
        """读方法台账。给了 `id` 就只回这一个（并附创新点视图）。"""
        from . import methods as MW
        s = _cur(slug)
        st = MW.state(s, root=root)
        if id:
            want = MW.check_id(id)
            if want not in st["methods"]:
                raise RuntimeError(f"方法不存在：{want!r}。已有的：{sorted(st['methods'])}")
            m = st["methods"][want]
            return json.dumps({"ok": True, "slug": s, "method": m,
                               "patent": MW.patent_view(s, want, root=root),
                               "md": MW.render_md(s, root=root)},
                              ensure_ascii=False)
        return json.dumps({
            "ok": True, "slug": s,
            "methods": {k: {"title": v["title"], "origin": v["origin"],
                            "n_versions": len(v["versions"]),
                            "latest": (v["latest"] or {}).get("version"),
                            "n_observes": len(v["observes"]),
                            "has_patent": bool(v["patent"])}
                        for k, v in st["methods"].items()},
            "n_retracted": st["n_retracted"],
            "md": MW.render_md(s, root=root),
            "say": ("还没有记过任何方法" if not st["methods"]
                    else "要看库里可依帕的参数用 lab_method；"
                         "这里给的是你这个课题里已经定下的那一份。"),
        }, ensure_ascii=False)

    def patent_leads(id: str, slug: str = "") -> str:
        """创新点盘点。**不下新颖性结论** —— 见 `methods.patent_view` 的 docstring。"""
        from . import methods as MW
        s = _cur(slug)
        return json.dumps(MW.patent_view(s, MW.check_id(id), root=root),
                          ensure_ascii=False)

    def plate_qc(values: dict, layout: dict, slug: str = "") -> str:
        """板内质控：读一张板的读数，算 Z' / SSMD / 复孔 CV / 边沿漂移。

        为什么要有这只手套：方案里写着「复孔 CV <= 15%」「dA 落 20%-80% 线性段」，
        但**没有任何东西在数据进来时真的去算** —— 判据写在文档里、没人执行，
        就是一句没人验证的愿望。这只手套把它变成一个读数。
        """
        from . import plate as PL
        rep = PL.run_wells(values, layout)
        rep["summary"] = PL.summary(_cur(slug), rep)
        rep["algo_source"] = ("plateqc (GitHub, MIT) 的算法口径；"
                              "已修 nan/inf 静默污染与删失值分桶")
        return json.dumps(rep, ensure_ascii=False)

    return {"topic_status": topic_status, "topic_use": topic_use,
            "raise_blocker": raise_blocker, "resolve_blocker": resolve_blocker,
            "topic_write": topic_write, "topic_link": topic_link,
            "topic_update": topic_update,
            "lab_record": lab_record, "lab_plan": lab_plan,
            "lab_report": lab_report,
            "lab_method": lab_method,
            "outline_save": outline_save, "outline_status": outline_status,
            "outline_to_lab": outline_to_lab,
            "method_record": method_record, "method_status": method_status,
            "patent_leads": patent_leads,
            "plate_qc": plate_qc,
            **_advisor_fns(root)}


# ── 导师模型（advisor profile，2026-11-14 续）──────────────────────────
def _advisor_fns(root=None):
    """把 `rongke/advisor.py` 接成模型可调的工具。

    为什么要给模型这四只手：`vendor/Supervisor.skill` 只写了「怎么蒸馏」，
    蒸馏完的产物落哪儿、怎么读回来、怎么接进本次会话 —— 那是宿主的事。
    原来这套东西只有命令行，模型读得到 SOP 却**写不成文件**，
    与「`topic_write` 之前没有写任意文本的基元」是同一个缺口。
    """
    from . import advisor as AD                          # noqa: PLC0415

    def advisor_status(slug: str = "") -> str:
        """列全部导师模型，或读一个（含读回校验）。**只读。**"""
        s = str(slug or "").strip()
        if not s:
            return json.dumps({"ok": True, "advisors": AD.list_all(root=root),
                               "root": str(AD.advisors_root(root)),
                               "hint": "要读某一层用 advisor_read(slug=…, layer=…)；"
                                       "要新建/更新用 advisor_save。"},
                              ensure_ascii=False)
        adv = AD.load(s, root=root)
        if adv is None:
            return json.dumps({"ok": False, "slug": s,
                               "error": f"找不到导师模型：{s}",
                               "known": [r["slug"] for r in AD.list_all(root=root)]},
                              ensure_ascii=False)
        rep = AD.audit(adv)
        return json.dumps({"ok": rep["ok"], "slug": adv.slug, "name": adv.name,
                           "version": adv.version, "audit": rep,
                           "versions": AD.versions(s, root=root),
                           "chars": {k: len(v) for k, v in adv.layers.items()}},
                          ensure_ascii=False)

    def advisor_save(slug: str, layers: dict | None = None, name: str = "",
                     mode: str = "", strategy: str = "hybrid_distill",
                     sufficiency: int = 60, sources: list | None = None) -> str:
        """建/改一个导师模型。**改之前自动备份，改完读回校验。**"""
        s = str(slug or "").strip()
        if not s:
            raise RuntimeError("advisor_save 必须显式传 slug（改成盘操作）。"
                               "新建请先用 advisor_status() 看已有清单，避免重名。")
        r = AD.save(s, name=name, layers=layers or {}, mode=mode,
                    strategy=strategy, sufficiency=sufficiency,
                    sources=sources or [], root=root)
        return json.dumps(r, ensure_ascii=False)

    def advisor_read(slug: str, layer: str = "all", max_chars: int = 20000) -> str:
        """读某一层（或全部）。**原文返回**，不总结 —— 判准的措辞本身是证据。"""
        adv = AD.load(str(slug or "").strip(), root=root)
        if adv is None:
            return json.dumps({"ok": False, "error": f"找不到导师模型：{slug}"},
                              ensure_ascii=False)
        keys = [k for k, _, _, _ in AD.LAYERS]
        want = keys if layer in ("", "all") else [layer]
        bad = [k for k in want if k not in keys]
        if bad:
            return json.dumps({"ok": False, "error": f"未知层：{bad}",
                               "layers": keys}, ensure_ascii=False)
        body = {k: (adv.layers.get(k, "")[:max(1, int(max_chars))]) for k in want}
        return json.dumps({"ok": True, "slug": adv.slug, "name": adv.name,
                           "version": adv.version,
                           "empty": [k for k in want if not body[k].strip()],
                           "layers": body}, ensure_ascii=False)

    return {"advisor_status": advisor_status, "advisor_save": advisor_save,
            "advisor_read": advisor_read}


def refresh_plot_schema(registry) -> None:
    """把插件注册的图型补进 `plot_data` 的参数说明。

    为什么需要这一步：`PLOT_DATA_SCHEMA` 是模块级常量，在 import 时
    就写死了内置图型；而插件在 `__init__` 里才加载。不刷新的话，
    插件图型对模型**不可见** —— 它能注册、能画、能被 suggest 推荐，
    但模型读到的"可用图型"清单里没有它，于是永远只会填内置那几种。

    ★ 只改 `kind` 那一条描述，别重建整个 schema —— 重建会把别人
      （工作台的、MCP 的）对它的改动抹掉。
    """
    reg = getattr(registry, "registry", registry)
    t = reg.get("plot_data") if reg is not None else None
    if t is None:
        return
    try:
        kinds = _plot_kind_list()
        t.parameters = copy.deepcopy(t.parameters or {})
        props = (t.parameters or {}).get("properties") or {}
        kd = props.get("kind")
        if not isinstance(kd, dict):
            return
        kd["description"] = (
            "图型。**留空是最常见也最正确的选择** —— 模块会按列的形状"
            "自动选，并把「为什么选它、为什么不选别的」写进 suggestions。"
            "只有你已经确定要画什么时才填。当前可用："
            f"{kinds}")
    except Exception:                                      # noqa: BLE001
        pass      # 刷新失败不该让内核起不来；模型至少还能看到内置那几种

def install_builtins(self, *, kb_domain: str = "kb", topic_domain: str = "topic") -> None:
    # 这两个不由 registry 执行，而是主循环特判（因为它们要触发门禁 / 退出）
    self.registry.register(Tool(
        name="deliver_task",
        description=("宣布任务完成。必须附上真实工具调用的证据（call_seq）。"
                     "证据门会核验，编造或证据不足会被打回并告知缺什么。"),
        parameters=DELIVER_SCHEMA,
        fn=lambda **kw: "（deliver_task 由主循环处理）",
        domain="core",
    ))
    self.registry.register(Tool(
        name="report_blocker",
        description=("诚实报告任务无法完成。当你确实推进不了时用它，"
                     "而不是假装完成。这是被鼓励的行为。"),
        parameters=BLOCKER_SCHEMA,
        fn=lambda **kw: "（report_blocker 由主循环处理）",
        domain="core",
    ))
    # ★ 读工作区里的文本文件。**这是内核一直缺的基元**（2026-09-21 实跑抓到）：
    #   在此之前 67 个工具里没有一个能读本地文本文件，于是材料是 `.md` 的
    #   任务**根本无法开工** —— 详见 `_make_read_file` 的 docstring。
    #   放 `core` ⇒ 任何任务都挂，因为"读输入"不是某个领域的能力。
    self.registry.register(Tool(
        name="read_file",
        description=("读**本任务工作区里的文本文件**（.md/.txt/.json/.csv…，UTF-8 或 GBK），"
                     "返回正文，超长会截断并如实说明。**找不到文件时会列出工作区里"
                     "真实有的名字**，直接照抄即可。注意：pdf/docx 用 `import_document`，"
                     "网页用 `web_extract` —— 本工具只读工作区内的纯文本。"),
        parameters=READ_FILE_SCHEMA,
        fn=_make_read_file(),
        domain="core",
    ))
    # ★ 写工作区里的文本文件。**`read_file` 的对偶基元**（2026-10-14 实跑抓到）：
    #   补了"读"之后，"写"一直悬空 —— 契约要求产出指定路径的文本文件时
    #   模型无路可走，只能如实报阻塞。详见 `_make_write_file` 的 docstring。
    #   标 `mutating=True`：注册表的工作区围栏再管一道（工具内已自判一次）。
    self.registry.register(Tool(
        name="write_file",
        description=("把文本写入**本任务工作区里的文件**（utf-8，自动建上级目录）。"
                     "**要产出任何 .md/.txt/.csv 正文文件，用这个。**"
                     "`append=true` 追加（长文分段写用这个，别一次吐几万字）；"
                     "默认整篇覆盖。**写完会回读核验**并回报字节与末段文字。"
                     "只能写工作区内；pdf/docx 用 `import_document`/`export_docx`。"),
        parameters=WRITE_FILE_SCHEMA,
        fn=_make_write_file(),
        domain="core",
        mutating=True,
    ))
    # ── 子代理（W20-B）────────────────────────────────────
    # 把一件事派给一个**隔离子智能体**：它在自己的工作区、自己的 registry /
    # gate / fence / telemetry 里干完，**只把结果交回来**。
    #
    # ★ 注册成**一个普通工具**，`_loop` 里没有为它加任何特判分支 ——
    #   特判才是风险所在：主循环一旦认识"子代理"这个概念，
    #   上下文隔离就要靠人记住（本项目反复付过这种债）。
    #
    # ★ 它**不是** mutating：子代理写的是**它自己的工作区**，
    #   父级工作区一个字节都不动。把它标成 mutating 会让围栏去检查
    #   `task` 这段自然语言 —— 而 `looks_like_path()` 对含 `/` 的中文句子
    #   返回 True，于是**正常的任务描述会被围栏当越界路径拒掉**。
    #   围栏管的是"参数里写的路径"，这里没有路径参数。
    #
    # `enabled=False` 时**不注册**（而不是注册了再拒绝）：
    # 看得见却调不通的工具，模型会反复试、反复失败、把步数烧光。
    if getattr(getattr(self.cfg, "subagent", None), "enabled", True):
        self.subagents = SubagentRunner(self.cfg, host=self)
        self.registry.register(Tool(
            name="subagent",
            description=("把**一件边界清楚的独立任务**派给一个隔离子智能体去做。"
                         "它有自己的上下文与工具面，干完只交回结果"
                         "（最终交付文本 + 用了多少步 + 各工具调用次数）——"
                         "**你看不到它的中间过程，它也看不到你的**。"
                         "适合大范围检索 / 翻材料 / 读代码这类"
                         "**会产生大量原文**的活：派出去，父级上下文就不被撑爆。"
                         "写清楚要什么结论、什么范围、什么算做完。"
                         "它**不能**再派子代理（派发工具不在它的清单里）。"),
            parameters=SUBAGENT_SCHEMA,
            fn=self.subagents.delegate,
            domain="core",
        ))
    # 只读工具（网络 GET），不参与工作区围栏检查。
    # 存在理由：领域工具 fetch_doi 只**取元数据**，不判真伪也不查撤稿；
    # 而 lit-review 技能的 retract 指令需要这个能力才能落地。
    self.registry.register(Tool(
        name="verify_citation",
        description=("核验单条引文：① DOI 是否真实存在 ② 是否**已被撤稿** "
                     "③ 元数据是否与稿件里写的一致（标题/年份/作者）。"
                     "核验参考文献时逐条调用它，不要只看 fetch_doi 的元数据 —— "
                     "fetch_doi 不会告诉你一篇论文已经被撤了。"),
        parameters=VERIFY_SCHEMA,
        fn=verify_citation,
        domain="core",
    ))
    # 荣珂自带的技能装载（只读）。
    # 为什么不用工作台的 read_skill：它的技能根只有三处（~/.zcode/skills 的 8 个
    # 旧副本、~/.dsh/skills、引擎自带），**扫不到 ~/.agents/skills 的 21 个真库**。
    # 而「每个阶段一个专属技能」要求能读到那 21 个。
    self.registry.register(Tool(
        name="load_skill",
        description=("装载一个阶段专属技能的**入口文档**（只读，自动记进过程记录）。"
                     "契约的「阶段专用技能」清单里，每个阶段有且只有一个技能，"
                     "进入该阶段前必须先用本工具装载它，再按它的协议执行。"
                     "凭记忆模仿不算数。找不到会明确告诉你有哪些可用技能。\n"
                     "技能的入口文档里若让你「按需打开 references/ 下的文件」，"
                     "用本工具的 `file` 参数读它们（例如 "
                     "file='references/introduction.md'）—— 别自己拼路径猜文件名，"
                     "路径写错时会把该技能下**所有可用文件**列给你。"),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "技能名，如 nature-writing / nature-polishing"},
                "file": {"type": "string",
                         "description": "（可选）技能目录内的相对路径，读它的参考文件；"
                                        "留空则读入口文档 SKILL.md"},
            },
            "required": ["name"],
        },
        fn=_load_skill,
        domain="core",
    ))
    # ★★ 课题台账（导师模式 P1/P2，2026-09-22；挂载形状 W25，2026-10-06）。
    #   存在理由：内核原来只有**一条时间尺度 —— 一次 run**。`report_blocker` 是
    #   **终局**动作（卡点被说出来一次就永远消失），`TaskContract` 跑完就归档。
    #   于是「导师」最核心的两句话**没有对象可查**：
    #     ·「上次到哪了？」 —— 课题不是一等公民
    #     ·「这个坑你两周前卡过。」 —— 没有台账，同一个坑换个说法会再踩一遍
    #   完整设计与三条纪律见 `rongke/topic.py` 模块头。
    #
    #   ⚠️ 挂载形状的演变：这 5 个原先**全挂 `core` 常驻**，理由是"按需裁剪需要
    #   一个硬判据，而当时量不出来 ⇒ 量不出来就不裁"。W25 把判据补上了 ——
    #   契约的 `needs_kb` 字段（照 `needs_deck` ⇒ ppt 域的同构先例）＋导师开关
    #   ＋文献类阶段（见 `mounting_decision`），于是照 `_DECK_DOMAIN` 的形状
    #   拆出 `_TOPIC_DOMAIN`，与 `_KB_DOMAIN` 一起按需挂。
    #   开关不变量（有断言钉着）：kb_only=True ⇒ `kb_search` 与 `topic_use`
    #   **必在 allow 里** —— 导师模式那段提示词点名了这两类工具，
    #   挂载不给就是假开关。
    #
    #   注：这 5 个的参数里**没有路径**，所以标 `mutating` 对 `WorkspaceFence`
    #   没有任何效果（围栏只查 mutating 工具的**绝对路径参数**）。越界防护由
    #   `topic._topic_dir()` 自己负责（非法 slug 与 `..` 一律返回 None，不抛异常）。
    # ★★ PDF → Markdown 转换层（2026-09-25）。
    #   补的是**知识库的入库前那一环**：litkb 自己抽的是纯文本，
    #   丢掉了标题层级/表格/阅读顺序；而"方便 AI 读"要有产物可查。
    #   详见 `rongke/kb_convert.py` 模块头（含实测数字）。
    _kbcfg = getattr(self.cfg, "kb", None)
    if _kbcfg is None or getattr(_kbcfg, "enabled", True):
        # ── 库够用性判定（2026-10-22，用户流程的第一条）───────────────
        # 为什么必须有它：在此之前整个内核**没有**这个判定（全仓 grep
        # 够用/够不够/suffic/coverage 在文献语料上是空集）。于是模型接到
        # 「写一篇综述」→ 先检索 → 再入库 → 一路烧到 200 轮，
        # 才发现某几章其实没有文献支撑。判在动笔之前，返工才不会被触发。
        #
        # 挂 `core` 的理由与 `convert_pdf` 同源：**量不出一个硬判据来裁它**。
        # 「这次要不要判够不够」从契约里看不出来 —— 看起来所有要引文献的任务都该判。
        self.registry.register(Tool(
            name="kb_coverage",
            description=(
                "**判「库里的文献够不够写这个题目」** —— 综述/开题/长文动笔前的第一件事。\n"
                "给一组主题词（建议 5–8 个，对应大纲各章或核心论点），它对每个词做段落级"
                "检索，数出**命中了多少篇不同文献**，然后下判语：\n"
                "  · `need_more_literature=false` ⇒ **库够用，不要再去全量检索入库**，"
                "直接进入大纲阶段；缺哪一篇只补那一篇。\n"
                "  · `need_more_literature=true` ⇒ 回包里 `gaps` 点名了缺哪几章，"
                "只针对它们补（literature_search → literature_fetch → kb_add）。\n"
                "判据是机械的（默认每个主题词 ≥3 篇不同文献、达标占比 ≥60%，"
                "可用 min_docs / min_ok_ratio 调），不是感觉。\n"
                "⚠️ **中文主题词请给三字以上**：两字词会被库的 trigram 索引"
                "静默回空，本工具会把它们单列在 `dropped_short_terms` 里 ——"
                "那既不算「有」也不算「没有」，别据此判定库里缺。\n"
                "⚠️ `distinct_docs_seen` 是**下界**（每个词只取前若干段），"
                "它回答「至少能引几篇」，不是「库里一共几篇」。"),
            parameters=KB_COVERAGE_SCHEMA,
            fn=_make_kb_coverage(registry=self.registry),
            domain="core",
        ))
        # ── 取全文（2026-10-22）──────────────────────────────────
        # 这是「20% 全文率」那条坑的正面对策。详见 litfetch.py 模块头。
        # ★ 为什么它必须由内核自带：荣珂**没有 shell**（59 个工具里没有
        #   exec/bash/子进程类），所以"让模型去跑外部下载器脚本"这条路
        #   根本不存在；要开它就得给任意命令执行权，与围栏设计相反。
        self.registry.register(Tool(
            name="literature_fetch",
            description=(
                "**按 DOI 取全文**（综述/长文备料的核心动作）。按三条路依次试：\n"
                "  ① Europe PMC `fullTextXML` —— 官方 API，回的是 **JATS 全文 XML**，"
                "稳定、无需 key，**多数开放获取的生物医学文献走这条**\n"
                "  ② Unpaywall 判 OA → 出版方 PDF 直链\n"
                "  ③ doi.org 内容协商\n"
                "回包逐条给 `ok` / `route`（走的哪条路）/ `file`（落盘路径）/ `chars`。\n"
                "取到的 `file` **直接喂给 kb_add / kb_ingest 入库**（本工具不写库）。\n"
                "⚠️ 回 `ok=false` 说的是「**这三条路这次没成**」，"
                "**不是「世上没有这篇」** —— 剩下的可能是机构订阅 / CNKI 中文库 / "
                "出版商账号。要如实写进检索死区，不许据此删掉一个论点。\n"
                "⚠️ 落盘只在工作区内，`out_dir` 只收相对路径。"),
            parameters=LITERATURE_FETCH_SCHEMA,
            fn=_make_literature_fetch(self.cfg),
            domain="core",
            mutating=True,     # 往工作区写 pdf/xml → 交给围栏管
        ))
        _kfn = _make_kb_convert_tools(cfg=self.cfg)
        self.registry.register(Tool(
            name="convert_pdf",
            description=(
                "把一份 PDF 转成**结构化 Markdown**（标题层级 / 表格 / 阅读顺序保留），"
                "落进工作区。**产物是给人读、给 Obsidian 精读用的** —— "
                "kb 库只收 PDF 原文（入库走 `kb_add`/`kb_ingest`，"
                "litkb 入库时自己会抽全文），别拿这份 md 去入库。\n"
                "什么时候用它：用户给的是 PDF，而你要的是**能读的结构化文本**时"
                "（做综述、精读、建库、抽证据）。它比 `import_document` 更适合"
                "**学术论文**——后者是通用格式转换，不做版面结构还原。\n"
                "★ 默认 `use_ocr=false`，**别乱开**：实测 21 页双栏论文关 OCR 24 秒、"
                "开 OCR 53 秒（慢 2.2 倍），正文只多约 1.3%。数字文本的期刊件不要开；"
                "只有扫描件才开。返回里会按每页字符数提示像不像扫描件。\n"
                "⚠️ 返回 `verdict=\"SKIPPED\"` 表示**解释器里没装 pymupdf4llm，"
                "这次根本没转**（产物不存在）—— 不要把它当成「这篇 PDF 是空的」。"),
            parameters=CONVERT_PDF_SCHEMA,
            fn=_kfn["convert_pdf"],
            domain="core",
            mutating=False,   # out 只许相对路径，且只写工作区 ⇒ 不动外部世界
        ))
        self.registry.register(Tool(
            name="kb_to_obsidian",
            description=(
                "把一份 PDF 转成 Markdown **并写进 Obsidian 库**（带 YAML frontmatter："
                "title/authors/year/journal/doi/tags）。\n"
                "Obsidian 库就是一个**装了 .md 的目录** —— 本工具直接写文件，"
                "**不依赖任何 Obsidian 插件**（那些插件在不在、还维不维护都不该决定"
                "这条链能不能用）。\n"
                "⚠️ 两条纪律：① 同名笔记**绝不覆盖** —— 内容不同就存成新名字，"
                "原件一个字不动；② 它写的是 **Obsidian 的库**，"
                "**不等于**进了荣珂的 kb 库（`kb_search`/`kb_ask` 查的是另一套索引）。"
                "两个都要，就对同一份 PDF 再 `kb_ingest` 一次。"),
            parameters=KB_TO_OBSIDIAN_SCHEMA,
            fn=_kfn["kb_to_obsidian"],
            domain="core",
            mutating=True,    # 往用户 vault 写文件 ⇒ 按写类登记（保守）
        ))
    _tfn = _make_topic_tools()
    # ① 入口：立课题 / 切课题 —— 导师模式的总开关。
    self.registry.register(Tool(
        name="topic_use",
        description=(
            "开启或切换一个**课题**（长生命的科研任务）—— 导师模式的总开关。\n"
            "**要带着学生做科研之前先调它一次**：把「这次做的大方向」立成一个课题，"
            "之后每个新任务的第一条消息里都会自动带上这个课题的进度与未闭环卡点。\n"
            "已存在的课题**原样切过去，绝不覆盖**（title/question 只在新建时生效）。\n"
            "不立课题也能干活，但每次任务都是孤立的 —— 上次卡在哪、读过什么，下次没人记得。"),
        parameters=TOPIC_USE_SCHEMA,
        fn=_tfn["topic_use"],
        domain=topic_domain,
        mutating=True,
    ))
    # ② 看进度 + 卡点全貌。卡点 id 只能从这里拿。
    self.registry.register(Tool(
        name="topic_status",
        description=(
            "看**课题到哪了**：计划/读书/证据三份活文档的**有界索引**"
            "（有没有、多少字、截断没截断），外加卡点条目（每条都带「当时试过什么」）。\n"
            "**新任务开工前、或觉得在重复踩坑时调它**。留空 slug = 当前课题。\n"
            "要看某份台账的**正文**就带 `doc=plan|reading|evidence`（可用 offset/limit "
            "取一窗，默认 12000 字）；卡点可用 `status=open|resolved|all` 筛。\n"
            "卡点 id（B001 这类）**只能从这里拿** —— 不要猜 id，猜错的会被拒并列出真实 id。"),
        parameters=TOPIC_STATUS_SCHEMA,
        fn=_tfn["topic_status"],
        domain=topic_domain,
    ))
    # ③ 记卡点。**与 report_blocker 分工明确**：一个是记账，一个是终局。
    self.registry.register(Tool(
        name="raise_blocker",
        description=(
            "把推进中遇到的卡点**记账**：卡在哪、已经试过什么。\n"
            "**这是记账，不是收工** —— 记完继续推进；确实做不成才用 `report_blocker`（那是终局）。\n"
            "为什么必须记：不记的卡点会随着这次 run 一起消失，下次换个说法再踩一遍。\n"
            "⚠️ `tried` 是**下次最值钱的信息** —— 别写「做了很多尝试」这种等于没写的话。"),
        parameters=RAISE_BLOCKER_SCHEMA,
        fn=_tfn["raise_blocker"],
        domain=topic_domain,
        mutating=True,
    ))
    # ④ 闭环卡点。
    self.registry.register(Tool(
        name="resolve_blocker",
        description=(
            "把一个卡点**闭环**（写明是怎么解决的）。闭环后它不再出现在路径提醒里。\n"
            "`blocker_id` 从 `topic_status` 拿。同一个 id 重复闭环是**幂等**的，不会报错。\n"
            "⚠️ 别把没解决的标成已解决 —— 那是把台账变成废纸，比不记更糟。"),
        parameters=RESOLVE_BLOCKER_SCHEMA,
        fn=_tfn["resolve_blocker"],
        domain=topic_domain,
        mutating=True,
    ))
    # ⑤ 写课题活文档。**这是内核唯一能写任意文本的基元**（补另一条真缺口）。
    self.registry.register(Tool(
        name="topic_write",
        description=(
            "写课题的**活文档**：`plan`（计划与技术路线）/ `reading`（读过什么、从它拿到什么）/ "
            "`evidence`（哪条结论由哪篇文献支撑）。\n"
            "为什么必须有：内核此前**没有任何「写任意文本文件」的基元** —— "
            "`read_file` 只读、`import_document` 只转格式，于是「把这一路读到的攒成一份台账」"
            "这件事根本没有工具可做。\n"
            "`mode=append`（默认，追加）或 `replace`（整篇覆盖）。写完内核会**读回来核字符数**。"),
        parameters=TOPIC_WRITE_SCHEMA,
        fn=_tfn["topic_write"],
        domain=topic_domain,
        # ★ 2026-10-01 主控勘误：这里原先**漏了** `mutating=True`。
        #   它是**会写盘**的（`T.write_doc` 往课题活文档写正文；
        #   紧邻的 `audit_figure` 注册处就明写"会写盘…所以标 mutating 让围栏管"）。
        #   漏标的后果很具体：W20-B 里标称"只读"的 `explore` 子代理，
        #   靠 `<mutating>` 元规格过滤时**挡不住它** ——
        #   一个只该查文献的子代理于是拿到了写台账的能力（实测已复现）。
        #   本项目定律：能算的就不许靠匹配。`mutating` 是元数据，
        #   补它比在每份 profile 的名字清单里逐个列举可靠得多。
        mutating=True,
    ))
    # ⑥ 把文献挂到课题上。**这是在补一只原来根本不存在的手。**
    #   实测缺口（2026-11-02，用户原话：「我让他…寻找文献关联在这个课题下，
    #   他做不到」）：`set_library()` 早就存在，Web 面板的「添加到课题 /
    #   解除关联」按钮就在调它；但模型侧只注册了 5 个 topic 工具，
    #   **没有任何一个能把"搜到的这篇"挂到课题上**。
    #   ⇒ 模型读得到 library（topic_status）、搜得到全库（kb_search），
    #     唯独走不完最后一步。这是本仓「没给工具的动作，写多少纪律都空转」
    #     的正面命中：能力全在，只给了 GUI、没给模型。
    self.registry.register(Tool(
        name="topic_link",
        description=(
            "把共享库里的一篇文献**关联到课题**（或解除关联）。\n"
            "共享库只存一份；这里只加/删那条关联 —— **不复制、不删除文献本身**。\n"
            "**「在这个课题下找文献关联」的落子一步就是它**：先用 `kb_search` 在共享库里搜、"
            "或用 `topic_status` 的 `library` 看本课题已挂哪些，再把选中的 id 传进来。\n"
            "`doc_id` 必须是 kb_search / kb_list **真实返回**的 id —— 不要自己拼"
            "（拼错的 id 会挂上去而查不到内容，面板会把它列为「共享库中已找不到」）。\n"
            "同一篇重复关联是幂等的，不会重复计入。"),
        parameters=TOPIC_LINK_SCHEMA,
        fn=_tfn["topic_link"],
        domain=topic_domain,
        # 改 `topic.json` ⇒ 按写类登记（同 `topic_write` 2026-10-01 那次勘误）。
        mutating=True,
    ))
    # ⑦ 改课题元数据。原来"管理课题"在对话里只能做一半：
    #   建（topic_use）/ 写台账（topic_write）/ 记卡点，但**改不了**
    #   名称、研究问题、工作阶段、下一步 —— 这四项只存在于 Web 面板的
    #   `update` 分支里。于是用户说"这个课题的研究问题改成 X""阶段推到
    #   证据了"，模型只能说"请到面板上改"。
    self.registry.register(Tool(
        name="topic_update",
        description=(
            "改课题的**元数据**：名称 / 研究问题 / 工作阶段 / 用户记下的下一步。\n"
            "「这个课题的问题改成……」「阶段推到证据了」「下一步记成……」都在这里做，"
            "不必让用户去面板上点。\n"
            "⚠️ 只传要改的那几项即可：**留空 = 不改**（不会把标题或研究问题清掉）。\n"
            "唯一例外是 `next_step`：传空字符串 `\"\"` 表示「这一步做完了，清掉」。\n"
            "阶段只收 ideate / design / evidence / execute / submission 五个值。"),
        parameters=TOPIC_UPDATE_SCHEMA,
        fn=_tfn["topic_update"],
        domain=topic_domain,
        mutating=True,      # 改 topic.json ⇒ 按写类登记
    ))
    # ⑧ 记实验台账 / ⑨ 出汇报页（2026-11-02）
    #    ★ 这两个是本轮唯一"用户点名的能力"：他说得很具体 ——
    #      「老师说我没数据，在催我，我也很迷茫」「我需要他指导我下一步该干什么」。
    #      核完之后发现他不是没做实验，是**做的全是定性观察**：
    #      「三株 23℃ 适应但长得慢」「其他几株看起来长得快，但还没测定指标」——
    #      一句数字都没有。所以缺的不是"更聪明的建议"，是**结构**：
    #      把定性与定量分成两个 kind，"没数据"才能被算出来，
    #      "下一步"才能从台账里机械排出来，而不是靠模型发挥。
    self.registry.register(Tool(
        name="lab_record",
        description=(
            "往**实验台账**里记一条事实（菌株 / 观察 / 测量 / 批次 / 待办 / 指标）。\n"
            "为什么要有它：课题的 PLAN/READING/EVIDENCE 都是**散文**，散文里算不出"
            "「你哪几格还没数」；而导师催的正是这个。台账是结构化的，所以能算。\n"
            "★ **定性用 observe，定量用 measure，两者不许混**：\n"
            "  · `observe`（code + text + growth）＝「长得慢」「看起来快」，**不算数据**；\n"
            "  · `measure`（code + metric + value + unit + n）＝ 1.2 mm/d (n=3)，**才算**。\n"
            "  `measure.value` 传「慢」会**直接报错** —— 这是刻意的，不是刁难：\n"
            "  混进去之后「你有没有数据」就永远答不准了。\n"
            "★ 用户随口说的处境（「三株 23℃ 长慢，其他几株快还没测，发酵第 5 天」）\n"
            "  应当被拆成：三条 observe + 一条 batch + 若干 strain，**不要只记一句话**。\n"
            "★ 记完回包里带 `digest`（当前处境摘要）与 `gaps`（缺口格数），\n"
            "  不用再调一次报告就能知道刚才那笔改变了什么。\n"
            "★ 指标 key 只能从已注册里选（growth_rate/biomass/mannitol/adenosine/"
            "ferment_period）；要加新指标先用 `kind=metric` 注册（key+label+unit），"
            "否则报错会列出已知的。\n"
            "★ **记错/记重了用 `kind=retract` 作废，不要手工去改 LAB.jsonl**："
            "作废是一次追加（seq 或 target 指定那一条 + reason 说明为什么），"
            "被作废的那条不再进矩阵/缺口/汇报，但原文仍留在盘上可查。"
            "台账 append-only 的全部意义就是「改过什么能看出来」，手改会把这段历史抹掉。\n"
            "★ 株号还没定（用户没给真实编号，我在用 S01/S02 这类临时名）时，"
            "建株要带 `placeholder=true` —— 汇报页会给它加 `*` 并注明，"
            "否则那几个临时编号在纸上和真株号无法分辨，会被当成事实念出去。"),
        parameters=LAB_RECORD_SCHEMA,
        fn=_tfn["lab_record"],
        domain=topic_domain,
        mutating=True,      # 追加写 LAB.jsonl
    ))
    self.registry.register(Tool(
        name="lab_report",
        description=(
            "把实验台账出成**能放到组会/导师桌上的一页**：\n"
            "`LAB.md`（菌株×指标矩阵，空格显示 —；缺口逐条；下一步带来源；方法带文献出处）"
            "+ `LAB.csv`（tidy 长表，可直接喂 `plot_data` 画图）。\n"
            "两个文件写进课题目录，同时把正文一并返回。\n"
            "★ 用户的原话是「没有可以拿出来汇报的东西」，所以这个工具交付的是**文件**，"
            "不是一段建议。拿到 `md_text` 后应当 `write_file` 落到本次工作区，"
            "`csv_text` 存成 csv 再 `plot_data` 出一张图 —— 一页字 + 一张图才叫能汇报。\n"
            "★ 表里的 `—` 是「还没测」，不是「没这项」。汇报时**不要**把 — 换成 0。"),
        parameters=LAB_REPORT_SCHEMA,
        fn=_tfn["lab_report"],
        domain=topic_domain,
        mutating=True,      # 写 LAB.md / LAB.csv
    ))
    self.registry.register(Tool(
        name="lab_method",
        description=(
            "**方法卡**：把库里的方法与参数，按课题渲染成一张能照着做的卡。\n"
            "三张卡：`staining`（核质染色 / DAPI / 荧光 / 核相：两套已发表方案的逐项参数、"
            "两篇浓度差 1000 倍的冲突、失败三分诊、第一版下手顺序）、"
            "`identity`（怎么证明这几株是融合子：四环证据链，按成本排，标注台账里哪一环还没记录）、"
            "`quant`（该测哪些指标：单位、n、参照值、你现在的缺口）。\n"
            "★ **问到「怎么做 X」「参数是多少」「下一步做什么实验」，先调这个** ——"
            "卡里的数字全部来自库里文献的 `doc_id` + 页码，是资产渲染的，不是模型编的。\n"
            "★ 卡里没有的方法，再用 `kb_search` / `kb_ask` 去库里查原文。"
            "**不许凭常识补参数**：浓度、时长、波长这三类数字编错一个，实验就白做一天。\n"
            "★ 卡里会给「第一步做什么」的顺序（如阳性对照先于调浓度），"
            "转述这个顺序时不要打乱 —— 打乱了用户就会同时改两个变量。"),
        parameters=LAB_METHOD_SCHEMA,
        fn=_tfn["lab_method"],
        domain=topic_domain,
        mutating=True,      # 写 topics/<slug>/方法卡-<key>.md
    ))
    self.registry.register(Tool(
        name="lab_plan",
        description=(
            "**接收实验计划并排期跟进**：把用户贴来的实验计划拆成结构化实验步骤（experiment）"
            "和带截止/跟进日期的后续任务（todo），全部按课题记进 `LAB.jsonl`。\n"
            "支持自由文本（编号列表/Day 1-3/预计 N 天）或直接传结构化 `steps` 数组。"
        ),
        parameters=LAB_PLAN_SCHEMA,
        fn=_tfn["lab_plan"],
        domain=topic_domain,
        mutating=True,
    ))
    # ── 开题大纲（outline，2026-11-15）─────────────────────────────
    # 存在理由：课题台账里已经有 PLAN.md，但它是**自由文本** ——
    # 没有任何一处强制回答「这句话靠哪个数据成立」。于是真实使用里长出来的是
    # 大纲一份散文、台账另一份散文、论文正文第三份 —— 三者对不上时
    # **没有任何东西会报错**，直到答辩被问「这一段的数据在哪」。
    self.registry.register(Tool(
        name="outline_save",
        description=(
            "**敲定开题大纲**：章节 → 每节要什么数据 → 计划做哪些实验供这些数据。\n"
            "★ 它和 PLAN.md 的区别：这一份的硬要求是**每一条数据需求都必须被某个实验供应**，"
            "每一个实验都必须说清它写进哪几节。断了的线会被当场报出来。\n"
            "★ 写之前先把**研究问题和一句话主张**定下来 —— 主张定不下来的大纲是章节堆砌。\n"
            "★ 全量替换保存：先 `outline_status` 拿到完整一份，改完整份交回来。\n"
            "\u26a0 不要替用户编实验。他说不清楚要测什么就问；"
            "不要拿通用实验清单把他的课题填满。"
            "参数不确定就去 `lab_method` / `kb_search` 查，不凭常识写浓度和时长。"),
        parameters=OUTLINE_SAVE_SCHEMA,
        fn=_tfn["outline_save"],
        domain=topic_domain,
        mutating=True,      # 写 OUTLINE.json / OUTLINE.md
    ))
    self.registry.register(Tool(
        name="outline_status",
        description=(
            "**读大纲 + 与实验台账对账**。整份大纲回给你，同时算出：\n"
            "  · `starved` —— 要数据、却没有任何实验在供 ⇒ **这一节现在写不出来**；\n"
            "  · `orphan_experiments` —— 做了实验、不知道写进哪几节；\n"
            "  · `unverified` —— 大纲自称已有数据，但台账里核不上；\n"
            "  · `missing_kinds` —— 不引言/方法/结果/讨论里整类缺了哪一类。\n"
            "★ 这些结论**全由盘上两份东西复算**，不含任何主观判断 —— 转述它，不要自己另推一套。\n"
            "★ 用户问「我这课题还差什么数据」「这一段能写了吗」时，**先调这个**。"),
        parameters=OUTLINE_STATUS_SCHEMA,
        fn=_tfn["outline_status"],
        domain=topic_domain,
        mutating=False,
    ))
    self.registry.register(Tool(
        name="outline_to_lab",
        description=(
            "把大纲里**还没有数据**的需求，写成实验台账的 todo（幂等：同名跳过）。\n"
            "★ 它**只写 todo，不会凭空造出测量** —— 数据必须由真做的实验来（lab_record measure）。\n"
            "★ 用于「把这份计划变成今天该做的事」。排完发现顺序不对，调大纲，不要手改台账。"),
        parameters=OUTLINE_STATUS_SCHEMA,
        fn=_tfn["outline_to_lab"],
        domain=topic_domain,
        mutating=True,      # 往台账写 todo
    ))
    # ── 方法台账 + 创新点（method / patent，2026-11-15 续）────────────
    # 存在理由：`lab_method` 只能渲染**库里已有**的三张资产卡。而做实验更长的
    # 那一段是：库里没人这么做过（自己设计）、参数改了三次（得记住按哪一版做的）、
    # 改完比原文好（可能够报专利，但事后想不起来区别在哪）。三件事今天全落在
    # 聊天记录里 —— 而「这个数是按 v2 还是 v3 跑出来的」恰恰是实验记录最容易被追责的一处。
    self.registry.register(Tool(
        name="method_record",
        description=(
            "**记一个方法 / 记一个新版本 / 记一次做的时候发生了什么**（方法台账，append-only）。\n"
            "三种用法：\n"
            "  · `kind='method'` —— **新建**一个方法（必填 `origin`："
            "literature 照文献 / adapted 改了参数 / own 自己设计 / standard 标准或试剂盒）；\n"
            "  · `kind='version'` —— 参数变了就记新一版（**版本号不能复用**）；\n"
            "  · `kind='observe'` —— 这次跑的时候成了/失败了，为什么。\n"
            "  · `kind='patent'` —— 登记专利线索（证据不够就别登记，先用 patent_leads 看缺什么）。\n"
            "★ **每个参数都必须带 `src=`**：`kb:<doc_id> p.<页>` / `user` / `design` / `std:<标准号>`。"
            "没有出处的参数下一轮就会被当成事实复述 —— 浓度、时长、波长编错一个实验就白做。\n"
            "★ 改了文献做法就要写 `differs_from` + `what_changed`"
            "（——“我这版更好”不算，要说清改了哪个参数、从多少改到多少）。\n"
            "★ 不要用 `write_file` 自己写一份方法 md：那份被覆盖之后，"
            "「上一版到底怎么做的」就永远找不回来了。"),
        parameters=METHOD_RECORD_SCHEMA,
        fn=_tfn["method_record"],
        domain=topic_domain,
        mutating=True,      # 追加写 METHODS.jsonl
    ))
    self.registry.register(Tool(
        name="method_status",
        description=(
            "**读方法台账**：每个方法的来源、各版本参数与其出处、做的时候成了/失败了、哪一版项缺。\n"
            "★ 用于：「这个方法我上次怎么做的」「现在到哪一版了」「哪些参数还是文献里的、"
            "哪些是我自己定的」。\n"
            "★ 与 `lab_method` 分工：`lab_method` 给的是**库里的可依帕参数**；"
            "这里给的是**你这个课题里已经定下的那一份**。"),
        parameters=METHOD_STATUS_SCHEMA,
        fn=_tfn["method_status"],
        domain=topic_domain,
        mutating=False,
    ))
    self.registry.register(Tool(
        name="patent_leads",
        description=(
            "**创新点盘点**：这个方法里有没有你自己的东西、改动写没写清、公开日期填没填。\n"
            "★ **它不下新颖性结论** —— 它只算三件可复算的事：\n"
            "  ① 最新一版里**有几条参数的 src 不是 kb:**（即你自己定的）；\n"
            "  ② `what_changed` 写没写（与文献差在哪）；\n"
            "  ③ `first_public`（首次公开）填了没 —— **这个日期决定新颖性还在不在**。\n"
            "三档结论：worth_asking（值得问代理人）/ check_first（先补缺口）/ "
            "weak（可能不够）/ published（已公开，新颖性可能受影响）。\n"
            "★ ★ **不要替用户下「这能申请专利」的结论** —— 他会真去报。能不能报要问代理人；"
            "你的职责是把材料整成代理人能看的样子，并**在他投稿/开会前提醒先看 first_public**。"),
        parameters=PATENT_LEADS_SCHEMA,
        fn=_tfn["patent_leads"],
        domain=topic_domain,
        mutating=False,
    ))
    self.registry.register(Tool(
        name="plate_qc",
        description=(
            "**一张板的数据到底能不能用。** 喂读数和对照组布局，它算 Z'-factor、SSMD、"
            "信号窗、复孔 CV、边沿效应与列漂移，并逐条给人话告警。\n"
            "★ 什么时候必须调：手上有一批读板机读数、准备拿它下任何结论之前。"
            "方案里写的「复孔 CV ≤15%」「ΔA 落线性段」这类判据，"
            "不调这只手套就等于没执行 —— 判据写在文档里、没人算，是一句没人验证的愿望。\n"
            "★ 它**只判不改**：不合格就报不合格，不会把难看的孔悄悄剔掉让 Z' 变好看。\n"
            "★ 缺值与删失值分开报：ND / <0.05 是「测了但落在检出限外」，"
            "NA / 空格是「根本没测」—— 这两种在定量筛选里结论完全不同。\n"
            "★ 对照组不足、某个对照孔全是 nan、板上全是同一个值时，Z' 返回 None 而不是 0，"
            "表示「算不出来」而不是「算出来很差」。"),
        parameters=PLATE_QC_SCHEMA,
        fn=_tfn["plate_qc"],
        domain=topic_domain,
        mutating=False,
    ))
    # ── 导师模型（advisor profile，2026-11-14 续）──────────────────
    # 存在理由：`vendor/Supervisor.skill`（`create-supervisor`）会教模型
    # 「怎么把导师的判断方式蒸馏成四层」，但它**只管方法论、不管落盘** ——
    # 上游的产物靠人手动跑 `tools/skill_writer.py`。而荣珂这边，
    # 模型读得到那份 SOP 却**没有任何一只手套能把它写成文件**，
    # 于是用户说「把你了解的我导师存下来」只能得到一段聊天记录。
    # 这与「`topic_write` 之前内核没有写任意文本的基元」是同一个缺口。
    self.registry.register(Tool(
        name="advisor_status",
        description=(
            "列/读**导师模型**（把某位真实导师的判断方式蒸馏成的四层档案）。\n"
            "四层：`method_core`（怎么推进）/ `academic`（做什么）/ "
            "`persona`（怎么问）/ `playbook`（怎么过线）。\n"
            "★ 先调它看清**已有谁、各层空不空、素材充分度多少**，再决定新建还是补。"
            "不要重复建同一个人。"),
        parameters=ADVISOR_STATUS_SCHEMA,
        fn=_tfn["advisor_status"],
        domain=topic_domain,
    ))
    self.registry.register(Tool(
        name="advisor_save",
        description=(
            "新建或更新一个导师模型。**改之前内核自动备份，改完读回校验**。\n"
            "★ 每条事实后面必须标 `source=`：`user`（他亲口说的）/ "
            "`material`（从批注组会聊天里读出来的）/ `template`（模板补的，"
            "**必须标出来**）/ `merged`。没有 source= 的层会被读回判成不可用。\n"
            "★ 素材不够就**空着**，不要用模板把它填满 —— 填满之后「他真这么说过」"
            "与「内核替他编的」再也分不开，而这正是这件事唯一的价值。\n"
            "★ 素材充分度（0-100）老实填；< 40 时别声明 `strict_distill`。"),
        parameters=ADVISOR_SAVE_SCHEMA,
        fn=_tfn["advisor_save"],
        domain=topic_domain,
        mutating=True,      # 写 topics/advisors/<slug>/
    ))
    self.registry.register(Tool(
        name="advisor_read",
        description=(
            "读导师模型的原文（某一层或全部）。**原样返回，不总结** —— "
            "判准的措辞本身就是证据。\n"
            "★ 要按他的标准评东西之前先读；不要凭记忆复述他的判断。"),
        parameters=ADVISOR_READ_SCHEMA,
        fn=_tfn["advisor_read"],
        domain=topic_domain,
    ))
    # 图面体检（**会写盘**：审计报告落在工作区里，所以标 mutating 让围栏管）。
    # 存在理由：图有一类独有的失败 —— **所有程序都退出 0，图是错的**。
    # 没有这道工具，第 1~7 条门全都拦不住它（它们核的是"流程走没走"）。
    if getattr(self.cfg.figure, "enabled", True):
        # 数据分析成图（**会写盘**：脚本与 PNG/PDF/SVG/TIFF 都落在工作区）。
        # 它补的是「拿到一份表」到「有一张可以送审的图」之间那一段断链：
        # 过去模型凭手感选图型（两组连续变量画折线、时间序列画箱线），
        # 程序全都退出 0 而图是错的。现在选图型的判据由被测出的列形状给出。
        self.registry.register(Tool(
            name="plot_data",
            description=(
                "**数据分析成图**：给一张数据表（CSV/TSV/Excel/JSON），"
                "自动测列形状 → 选图型 → 生成绘图脚本 → 跑出图。\n"
                "产出：单独一套绘图脚本 `plot.py` + PDF/SVG/PNG(600dpi)/TIFF，"
                "外带一份**可直接送审的五点契约**与**图注草稿**。\n"
                "为什么用它而不是自己写绘图代码：它会先把「每列是什么」量出来"
                "（连续/分类/时间、缺失、基数），再按这个选图型，并把"
                "「为什么选它 / 为什么不选那几个」写成理由。"
                "自己凭手感选，最常见的错是把时间序列画成箱线、把两个组的点"
                "串成一条折线 —— **程序退出码是 0，图是错的**。\n"
                "★ `kind` 留空即可（自动选）。有分组就填 group，"
                "时间序列**必须**按组分层。\n"
                "★ 跑完必须接着调 `audit_figure`：把返回的 contract 与 "
                "legend_draft 原样传过去（图注里的 ___ 和结论句要你先补全）。"
                "本工具只管画，不管审。\n"
                "★ `kind` 可选值不止内置那几种 —— 已装插件注册的图型"
                "也会出现在 `suggestions` 里，带它自己的适用理由。"),
            parameters=copy.deepcopy(PLOT_DATA_SCHEMA),
            fn=_make_plot_data(self.cfg.figure),
            domain="figure",
            mutating=True,        # 写脚本与产物 → 交给工作区围栏管
        ))
        # 原生 SVG 出图（2026-11-05）：**零第三方依赖**，装完就能画。
        # 与 plot_data 并列而不是替掉它 —— 两者判据不同：
        # plot_data 走投稿路线（PDF + 六道审计门，需要 rongke[figure]）；
        # plot_svg 走预览路线（SVG + 结构自检，只需要标准库）。
        self.registry.register(Tool(
            name="plot_svg",
            description=(
                "**原生出图（零依赖）**：给一张表，荣珂自己用标准库画出 SVG 矢量图。"
                "不需要 Origin、不需要 workbench MCP、不需要 matplotlib / pandas / PyMuPDF，"
                "装完框架就能用；中文按 `Microsoft YaHei → SimHei → Arial` 字体栈回退。\n"
                "选图型仍然复用 `analysis.plan_from_shape()` 的量出来的列形状判据，"
                "不是凭手感；出图后**读回文件**跑结构自检（viewBox / 文本节点 / "
                "数据标记 / 坐标出界 / 中文字体栈 / 外部引用），"
                "任一不过就 `ok:false` 并保留文件供核查，不假装成功。\n"
                "⚠️ 这是**预览级**产物（网页 / PPT 插图 / 给用户看）："
                "PDF 六道渲染后审计门（字形 ≥5pt / 碰撞 / 对齐 / 位图）**一道都没跑**，"
                "不许把它当投稿级图宣布完成。要出版级请改装 "
                '`pip install "rongke[figure]"` 后走 `plot_data` + `audit_figure`。\n'
                "★ `kind` 留空自动选；不支持的图型会明确报错，不会偷换成别的图。"),
            parameters=PLOT_SVG_SCHEMA,
            fn=_make_plot_svg(self.cfg.figure),
            domain="figure",
            mutating=True,        # 写 SVG 到工作区 → 交给围栏管
        ))
        self.registry.register(Tool(
            name="audit_figure",
            description=(
                "对**产出图**的任务做图面体检（出版级标准，nature-figure 合同）。"
                "跑四道程序化审计：① 源码预检 ② PDF 字形下限（每字形 ≥5 pt，含上下标）"
                "③ 渲染后碰撞（文字压字/线穿字/出界）④ 多分面对齐（容差 1.5 pt）；"
                "外加图注体检与**报告新鲜度**。\n"
                "同时判定**渲染路线**：要不要走 python/r 由这张图的审计需求决定"
                "（≥2 分面 / 有定量分面 / 声明了期刊 ⇒ 必须），"
                "声明成 origin 而条件不满足时，本工具会在**画之前**把契约打回。\n"
                "为什么必须跑：图最常见的失败是**每个程序都退出 0、图是错的** ——"
                "字体缺字渲染成豆腐块、面板差零点几 pt、字号被缩到 5 pt 以下，"
                "这些都不报错。\n"
                "要产出图的任务，**交付前必须调用本工具**，否则图面门会拦下交付。"
                "改过图之后必须**重跑** —— 旧报告不算合格证。"),
            parameters=AUDIT_FIGURE_SCHEMA,
            fn=_make_audit_figure(self.cfg.figure),
            domain="figure",
            mutating=True,        # 写审计报告 → 交给工作区围栏管
        ))
    # 组会 PPT 体检（同样**会写盘**）。存在理由与图面门同源：
    # **「能生成 .pptx」和「能讲」是两件事** —— 上游 MCP 只保证文件是合法
    # OOXML，不保证这份 deck 拿去组会不丢人。没有这道工具，
    # 第 1~8 条门全都拦不住它（它们核的是"流程走没走"）。
    if getattr(getattr(self.cfg, "deck", None), "enabled", True):
        self.registry.register(Tool(
            name="audit_deck",
            description=(
                "对**产出演示文稿**的任务做组会 PPT 体检。跑四道："
                "① 上游 XML 审计（形状越界 / 文字密度 / AI 套话 / 裁图 / "
                "小图 / 对齐差一点）② 荣珂收紧的版式判据（正文字号下限、"
                "页数区间、单页字数、标题）③ **讲稿备注覆盖率**"
                "（上游只数不判，而组会的备注就是讲稿）④ **报告新鲜度**。\n"
                "为什么必须跑：**能生成 .pptx ≠ 能拿去讲**。把论文整段粘上"
                "一页、没有讲稿、正文小到后排看不见 —— 这些全都不报错。\n"
                "⚠️ 上游 37 个 MCP 工具**没有**写备注的能力；补讲稿备注请调"
                " `set_slide_notes`（内核提供，你别自己写代码）。\n"
                "★ `deep_contrast=true` 会**额外调 PowerPoint 渲染**逐页看像素"
                "（本机实测 ~15 秒，慢一个数量级），**仅在需要检查"
                "'文字压在照片上'时开启**；默认关闭、不开启时输出与今天逐字相同。\n"
                "要产出 .pptx 的任务，**交付前必须调用本工具**，否则 deck 门会"
                "拦下交付。改过 deck 之后必须**重跑** —— 旧报告不算合格证。"),
            parameters=AUDIT_DECK_SCHEMA,
            fn=_make_audit_deck(self.cfg.deck),
            domain="ppt",
            mutating=True,        # 写体检报告 → 交给工作区围栏管
        ))
        # ★★ 品牌模板：**保真路线**（2026-09-22）。
        #   成对注册：一个**只读的清单**（模型得先知道有哪些页型、每槽能装多少字）
        #   + 一个**出稿工具**。清单独立成一个工具而不是塞进 schema 描述里，
        #   理由与 `deck_reference` 相同：它是**可变长的资产**，塞进 schema
        #   就是每场任务都付的固定开销，而只有真要做品牌稿的任务才需要它。
        #
        # ★ 清单**分两段读**（同日补，接一份 72 页的实测版式库时量出来的）：
        #   小模板（≤ `PLAN_INLINE_MAX` 页）一次给全；
        #   大模板只给**页型级概览**，详表按 `page=` 索要。
        #   依据：72 页版式库的全量清单 **36,290 字符 ≈ 18k tokens**，
        #   比全部 69 个工具的 schema 加起来还大，而它一旦读进上下文
        #   就是**整场任务都付**的钱。
        try:
            from .deck import (PLAN_INLINE_MAX, load_template,
                               template_overview_text, template_pages,
                               template_plan_text, template_name_of)
            _tname = template_name_of(self.cfg.deck)
            _tpl = load_template(_tname)
            _n_pages = len(template_pages(_tpl))

            def _plan(page: str = "") -> str:
                _ids = [x for x in re.split(r"[,，;；\s]+", str(page or "")) if x]
                if _ids:
                    return template_plan_text(_tpl, only=_ids)
                return (template_plan_text(_tpl) if _n_pages <= PLAN_INLINE_MAX
                        else template_overview_text(_tpl))
        except Exception as e:            # 槽位表不在 / 读不动 ⇒ **如实说**
            _tname, _n_pages = "(读不到)", 0
            # ★ except 块结束时 Python 会把 e 删掉，而 _plan 是嵌套函数、
            #   真正调用发生在块外 —— 直接闭包引用 e 时单元格已被清空
            #   （实测：NameError: cannot access free variable 'e'），
            #   模型收到纯噪声而不是「改走路线乙」的路由信号。必须先落常量。
            _err = f"{type(e).__name__}: {e}"

            def _plan(page: str = "") -> str:
                return (f"（品牌模板的槽位表读不到：{_err}）\n"
                        f"⇒ 本次**不能**按模板出稿；改用 compose_deck/style_deck 兜底，"
                        f"并在交付时说明这件事。")
        _plan_schema = {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "description": (
                        "想要**哪几页**的槽位与字数上限（逗号分隔的页型 id，"
                        "如 `\"封面,章节页-1\"`）。\n"
                        "留空则：页型少的模板直接给全量清单；"
                        f"页型多的模板（超过 {PLAN_INLINE_MAX} 页）只给**页型级概览**，"
                        "这时你要**按它列出的 id 再调一次**本工具拿详表。"),
                },
            },
        }
        self.registry.register(Tool(
            name="deck_template_plan",
            description=(
                "列出**学校品牌模板**有哪些页型、每一页有哪些槽位、"
                "每个槽能装多少字（上限从几何与字号**算出来**，不是估的）。\n"
                "**动手写文案之前先调它** —— 模板的槽位是有硬容量的"
                "（页标题只装 5 字、章节标题 6 字），不看上限去写必然被拒。\n"
                "只在概览模式下，用 `page=\"页型1,页型2\"` 再调一次拿详表；"
                "**槽位索引不要凭印象写**。\n"
                "只读，不产生任何文件。"),
            parameters=_plan_schema,
            fn=_plan,
            domain="ppt",
            mutating=False,
        ))
        self.registry.register(Tool(
            name="deck_from_template",
            description=(
                "**按学校品牌模板出稿**（保真路线）：保留模板页的原样装饰"
                "（满版蓝底、章节巨型数字、校徽、出血照片），只在文字槽上填字，"
                "多余的页删掉。品牌装饰**一个字节都不重绘**。\n"
                "这是**第一选择** —— 只要这份稿是要交给学校/组会看的，"
                "外观就该是学校的样子；`compose_deck` / `style_deck` 是**兜底**"
                "（模板不可用时才用，它们只是近似品）。\n"
                "用法：① 先调 `deck_template_plan()` 拿到页型与**每槽字数上限**；"
                "② 再调本工具，`pages` 按顺序给 `[{\"page\": \"cover\", "
                "\"slots\": {\"2\": \"标题\", …}}, …]`。\n"
                "内核会：逐槽检查字数上限（超了先缩字号、缩不动就拒绝并告诉你上限是多少）、"
                "**从磁盘重开文件逐槽读回来复测**，并写一份 `.origin.json` "
                "让体检能扣掉'模板自带的品牌装饰'（那些不是缺陷）。\n"
                "⚠️ **每个槽都必须填**；⚠️ 同一个模板页只能用一次；"
                "⚠️ 出稿后仍须调 `audit_deck`（本工具**不代替门**）。"),
            parameters=TEMPLATE_DECK_SCHEMA,
            fn=_make_deck_from_template(self.cfg.deck),
            domain="ppt",
            mutating=True,
        ))
        # ★ 写讲稿备注（第二个基元缺口的补法）。
        #   放在**同一个 `if` 里、同一个域**：只有要产出 deck 的任务才挂它。
        #   不挂到 `core` 域的理由是**每场任务都要付它的 schema 体积** ——
        #   而 schema 体积正是首步输入的最大一笔开销（实测口径见 skill）。
        self.registry.register(Tool(
            name="set_slide_notes",
            description=(
                "给已有 .pptx 的**每一页**写讲稿备注（备注栏就是讲稿本身）。\n"
                "为什么必须有这一步：**上游 37 个 MCP 工具没有写备注的能力** ——"
                "不调本工具，备注覆盖率就恒为 0，而本轮契约要了讲稿时"
                "第 9 条硬门必然拦下交付（实测：这一条曾让整场 25 分钟零交付）。\n"
                "用法：`notes` 是**按页序一一对应**的字符串列表，"
                "长度必须**恰好等于页数**（第 1 段配第 1 页）；"
                "某页确实不要讲稿也要占位给空串。页数不匹配会被拒绝，"
                "并告诉你这份 deck 真实有几页。\n"
                "写入由内核在子进程里用 python-pptx 完成 —— **你不需要、也不应该"
                "自己写代码**（你没有装包和改环境的能力）。写完内核会"
                "**独立复测**（重新读回文件逐页比对），把「写了几页 / 覆盖率 /"
                "哪几页还没有讲稿」如实报给你。\n"
                "⚠️ 补完**不要再调 `save_presentation`** 存同一份 deck —— "
                "MCP 服务端内存里那份没有备注，一存盘就会把它整片抹掉。\n"
                "⚠️ 改过 deck 会让之前的体检报告作废 ⇒ 写完必须**重跑** `audit_deck`。"),
            parameters=SET_NOTES_SCHEMA,
            fn=_make_set_slide_notes(self.cfg.deck),
            domain="ppt",
            mutating=True,        # 真的改盘上的 .pptx → 交给工作区围栏管
        ))
        # ★ 套视觉主题（2026-09-21 补的第三个缺口：**能生成 ≠ 拿得出手**）。
        #   起因是用户看完一份**结构全绿**的 deck 只说了一句「那能做的这么丑」。
        #   与「能生成 .pptx ≠ 能讲」同源，但量的是另一件事：**外观**。
        #   放在同一个 `if`、同一个域 —— 只有要产出 deck 的任务才挂它。
        self.registry.register(Tool(
            name="style_deck",
            description=(
                "给已有 .pptx 套上视觉主题。**真改 XML**：画布改 16:9（标准 "
                "12192000×6858000 EMU）、重写 `theme1.xml` 的 12 个色槽与 "
                "major/minor 字体、每页加绢色背景与页脚页码。\n"
                "主题 `juanqing` = 绢青陈墨（绢底 #F4EFE4、墨字 #23201C、"
                "朱砂点睛 #B03A2E、宋体标题＋雅黑正文）。\n"
                "为什么必须走这一步：上游 `create_presentation` 起手出的是 "
                "python-pptx 的**默认空白模板**（4:3、白底、Calibri），"
                "而体检会把「4:3 画布」和「纯默认外观」判成 **FAIL 并拦下交付**。\n"
                "⚠️ **不要**再指望 `apply_professional_design(operation=\"theme\")` ——"
                "那是**空壳**：它回 `{\"success\": true, \"slides_affected\": N}`，"
                "但实测 `theme1.xml` 的 md5、画布尺寸、part 数**一个都没变**"
                "（这类「自称改了、文件没动」的**假成功**是本项目点名的缺陷形状）。\n"
                "用法：保存 deck 之后调一次即可，内核会**独立复测**并把读回来的"
                "数字报给你。改完必须**重跑** `audit_deck`（旧报告作废）。\n"
                "⚠️ 套完**不要再调 `save_presentation`** 存同一份 deck —— "
                "服务端内存里那份没套主题，一存盘就会把主题整片抹回去"
                "（与写讲稿备注同一条禁忌）。"),
            parameters=STYLE_DECK_SCHEMA,
            fn=_make_style_deck(self.cfg.deck),
            domain="ppt",
            mutating=True,        # 真改盘上的 .pptx
        ))
        # ★ 参考样例（2026-09-21）。**只读**，排在 `compose_deck` **之前**
        #   —— 模型应当先看"长得对是什么样"，再动手排版。
        #   它是用户那句「人家加个 skill 就好看」的直接产物：
        #   参考样例要起作用就必须是**文本的**（模型读不了图，见执行体 docstring）。
        self.registry.register(Tool(
            name="deck_reference",
            description=(
                "取一份**随库发布的组会 deck 参考样例**（文本形态）："
                "12 页、六种页型全用到的完整示范 —— 逐页页型、标题、正文，"
                "外加每页的**讲稿备注**（写来源/口径/会被问到的问题）。"
                "还给出「页型 → 适合装什么内容」的对照表。\n"
                "**动手做 deck 之前先调它一次**：它把「什么样算好」变成"
                "可以照抄的结构，比任何形容词（「简洁大气」）都有用。\n"
                "⚠️ 这是**文本**，不是图：内核的模型通道读不了图片，"
                "所以示范以文字给出。样例另附成品 .pptx 与预览图路径 ——"
                "那是**给人看的**，你读不了，不要假装读过。\n"
                "⚠️ 参考样例自己的体检是 **PASS（五门全绿、零提示）**："
                "照着它的结构做，不会在门那里翻车。\n"
                "只读工具，不改任何文件，不占工作区围栏。"),
            parameters=DECK_REFERENCE_SCHEMA,
            fn=_make_deck_reference(self.cfg.deck),
            domain="ppt",
            mutating=False,       # **只读** —— 不碰盘上任何东西
        ))
        # ★ 按设计资产**精排**（2026-09-21 补的第四个缺口：**排版本身**）。
        #   上面三个缺口（读输入 / 写备注 / 外观）都补完之后，用户仍然说
        #   「太差了，不能直接上组会」。这一条补的是**版面**：
        #   三区网格、字号阶梯、色彩角色、页型几何 —— 全部来自
        #   `rongke/design.py`（单源），由内核确定性地落到每一页。
        #   放在同一个 `if`、同一个域：只有要产出 deck 的任务才挂它。
        self.registry.register(Tool(
            name="compose_deck",
            description=(
                "**按设计规范给整份 .pptx 精排**（比 `style_deck` 多管版面本身）。\n"
                "你只交**逐页页型**（cover/section/content/two-col/chart/closing），"
                "内核按 `design.py` 的三区网格（标题带 0–120px / 内容带 120–660px / "
                "页脚带 660–720px、左右边距 72px）、8 档字号阶梯、色彩角色"
                "与页型几何把每页排好，并补朱砂强调线 + 页脚落款与页码。\n"
                "**这是要交付组会时该走的最后一步**：`style_deck` 只换色换字、"
                "不动骨架，所以光靠它「版面还是那副版面」。\n"
                "⚠️ 动手之前建议先调一次 `deck_reference` —— 它给出一份"
                "六种页型全用到、五门全绿的参考样例，照着它定页型序列最稳。\n"
                "⚠️ 与 `style_deck` **二选一**：两个都调 = 双重坐标系。"
                "要精排就**只调本工具**（它内部已把画布与主题一并统一）。\n"
                "⚠️ 本工具**就地改写**：不传 `out` 会覆盖 `pptx` 指向的那份。"
                "想留未排版的原件，先自己复制一份 —— 内核无法替你回滚。\n"
                "⚠️ **不要**再指望 `add_chart` / `add_table` 之外的外形工具；"
                "排完**不要再调 `save_presentation`**（服务端内存那份没排过版，"
                "一存盘会把版面整片抹回去）。\n"
                "改完必须**重跑** `audit_deck` —— 旧报告作废。\n"
                "内核会**独立复测**并回报读回来的量：画布比例、主题槽、"
                "视觉资产前后相等（没丢图）、每页页型标记、"
                "以及「每个有文字的形状是否都带显式对齐」。"),
            parameters=COMPOSE_DECK_SCHEMA,
            fn=_make_compose_deck(self.cfg.deck),
            domain="ppt",
            mutating=True,        # 真改盘上的 .pptx
        ))
