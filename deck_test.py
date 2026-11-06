# -*- coding: utf-8 -*-
"""deck_test.py —— 「组会 PPT」（MCP + 第 9 条硬门）的回归测试。

跑法：
    python deck_test.py                 # 全部
    python deck_test.py --json          # 只吐 JSON

## 测试设计的四条规矩（沿用本项目血换的教训）

1. **每条判据配正控 + 负控。** 一个"只会报警"的守卫和一个"只会放行"的守卫
   一样没用 —— 两边都要钉住。
2. **每加一个守卫，必须配一条承重自检**：把守卫的机制**拿掉**，正向对照必须
   当场变红。否则无从知道这道守卫到底有没有在承重（G 型：检查在，却永不触发）。
3. **误报比漏报更伤**（设计定律 10）：每条"应该拦"的用例旁边，都要有"不该拦"的。
4. **不测"代码跑了"，测"判据对不对"。**

## 本模块专门盯着三类已经踩过的坑

| 形状 | 具体长相 | 钉在哪 |
|---|---|---|
| **N 型** | `notesSlide1.xml` 的大写 S 匹配不上小写正则 ⇒ 备注**计数对、正文恒空** | 2.x 正文非空断言 + 9.3 承重自检 |
| **G 型** | `title_text` 读了却没记 ⇒ "标题过长"这条检查**永不报** | 3.10 负控 + 9.4 承重自检 |
| **M 型** | "量不出字号"被渲染成"通过" | 3.5：必须留下 `SKIP` 且措辞写明"没查" |

`--no-real` 一类的开关**故意不给**：这份测试全部落在临时目录里，秒级，
没有"跳过"这种中间态可退 —— 一次真跑完，比一个可以关掉的检查诚实。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                                    # noqa: E402

from rongke import contract as C                                     # noqa: E402
from rongke import deck as D                                         # noqa: E402
from rongke import loop as L                                         # noqa: E402
from rongke.contract import TaskContract, _looks_like_deck, contract_wants_notes  # noqa: E402
from rongke.deck import (DeckAuditor, DeckFacts, DeckReport, GateResult,  # noqa: E402
                         Finding, HARD_MIN_FONT_PT, find_audit_script,
                         probe_pptx)
from rongke.gate import CompletionClaim, CompletionGate, EvidenceRef  # noqa: E402
from rongke.tools import Tool, ToolRegistry                          # noqa: E402

PASS, FAIL = [], []
_QUIET = False

# 测试用的一次性目录。**绝不写进产物目录**（`figures/`、仓库根这类）。
_TMPROOT = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_deck", "deck-tmproot")


def _py_pptx() -> str | None:
    """装了 python-pptx 的解释器 —— **必须问 config**，不能拿当前解释器顶替。

    🪤 **这里埋过一个 K 型（2026-09-21 晚抓到，症状是"断言数腰斩"）**：
    原实现全篇写的是 `_py_pptx()`，而它的候选表是
    `[explicit] + [sys.executable]` —— **空串是 falsy** ⇒ 候选表只剩
    "当前正在跑的这个解释器"。
    ⇒ 本套件在 **3.13（零依赖）** 下**永远拿不到**解释器：§21 把 `None` 直接塞进
      `subprocess.run([None, "-c", …])` ⇒ `TypeError: expected str, bytes or
      os.PathLike object, not NoneType` ⇒ **套件在 §21 当场崩掉**，
      §21–§24 一行都不跑、断言数 **361 → 224**。
      而运行器只会说"断言数比基线少了" —— **根因藏在一个 traceback 里**。

    ★★ **同一件事在仓库里有两个答案**：`notes_test.py` 问的是
      `D.find_notes_python(cfg.deck.python)`（**config 里那个**），这边问的是 `""`。
      **一处对、一处错 = H 型**，而错的那处正是断言数最多的套件。
    ★ 通用：**"我到哪里去找那个东西"必须只有一个答案**；
      凡"当前解释器"这种兜底，都要问一句「换个解释器跑，它还对吗」。
    """
    explicit = ""
    try:
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        explicit = raw.get("deck", {}).get("python", "") or ""
    except Exception:
        explicit = ""
    return D.find_notes_python(explicit or None)


def _style(pptx, **kw):
    """`style_deck` 的测试入口 —— **必须把解释器一起递进去**。

    ⚠️ 同一个 K 型的第二个坑（与 `_py_pptx()` 同源）：

    `style_deck(pptx, workdir=…)` 的解释器解析是
    `explicit = python_exe or (cfg.python if cfg else None)` → `find_notes_python(explicit)`，
    而 `explicit=None` 时它会退到 **当前解释器**。
    测试站在 3.13 上跑、又没传 `python_exe` / `cfg`
    ⇒ §18/§19/§21 的每一条都会以「**套主题的能力不可用**」失败。

    ★★★ **这才是这条缺陷最坏的地方：它的长相像"产品坏了"，其实是"测试没说清用哪个解释器"。**
      我当时差点就去查 `_STYLE_SCRIPT` 了（那正是"红得指错方向"）。
      根因一句话：**`find_deck_python` 允许退到当前解释器（审计器纯标准库，合理），
      可 `find_notes_python` 名字相近、门槛完全不同（要 python-pptx）——
      而调用点靠"默认值"糊过去了。**
    ★ 通用：**凡"哪个解释器/哪个环境"这类参数，测试里一律显式给，
      绝不靠被测代码的默认值** —— 默认值只在生产那条路上被验证过。
    """
    kw.setdefault("python_exe", _py_pptx())
    return D.style_deck(pptx, **kw)


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    if not _QUIET:
        print(f"  {'✔' if ok else '✘'} {name}" + (f"  — {detail}" if detail and not ok else ""))


def section(t: str) -> None:
    if not _QUIET:
        print(f"\n── {t} " + "─" * max(0, 58 - len(t)))


@contextmanager
def swapped(obj, name: str, value):
    """临时换掉一个模块级常量 —— 承重自检专用。

    模块级常量在被调函数里是**运行期**查全局的，所以换掉模块属性就真的生效。
    """
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


# ══════════════════════════════════════════════════════════════════
# 合成 .pptx 夹具（纯标准库）
#
# 为什么自己造而不是复用真夹具：**断言数必须每次一样**。
# 一个"有时跳过"的测试会让基线在"断言数腰斩"上误报，
# 而那个误报的方向最坏 —— 会教人忽略基线告警。
# ══════════════════════════════════════════════════════════════════
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_RELNS = "http://schemas.openxmlformats.org/package/2006/relationships"
_SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_LAYOUT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"


def _sp(ph_type: str, runs) -> str:
    """一个占位符形状。`runs = [(字号pt 或 None, 文字), ...]`。

    `pt=None` 表示**不写 `sz`**（继承母版）—— 这正是"量不出来"的那条路径，
    3.5 号断言专门要用它。
    """
    out = []
    for pt, txt in runs:
        rpr = f'<a:rPr sz="{int(round(pt * 100))}"/>' if pt else "<a:rPr/>"
        out.append(f"<a:p><a:r>{rpr}<a:t>{escape(txt)}</a:t></a:r></a:p>")
    return (f'<p:sp><p:nvSpPr><p:nvPr><p:ph type="{ph_type}"/></p:nvPr></p:nvSpPr>'
            f'<p:txBody>{"".join(out)}</p:txBody></p:sp>')


def _xml(body: str, root: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<{root} xmlns:a="{_A}" xmlns:p="{_P}">{body}</{root}>')


def _notes_xml(text: str, page_no: int) -> str:
    """备注页。**刻意带一个 sldNum 占位符** —— 它必须被排掉。

    否则"备注覆盖率"会把页码算成讲稿，那是个**恒真的假绿**。
    """
    body = _sp("body", [(12.0, text)]) if text else ""
    return _xml(f"<p:cSld><p:spTree>{body}"
                + _sp("sldNum", [(None, str(page_no))])
                + "</p:spTree></p:cSld>", "p:notes")


def _rels_xml(target: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_RELNS}">'
            f'<Relationship Id="rId1" Type="{_SLIDE_REL}" Target="{target}"/>'
            "</Relationships>")


def make_pptx(path, slides, notes=None, media=0, mtime=None,
              sld_sz=(12192000, 6858000), layout=None) -> Path:
    """造一份最小可用 .pptx（够 `probe_pptx` + 三道自研门读）。

    `slides`：[{title, body, body_pt, title_pt, extra}]；
      `title=None` ⇒ 不写标题占位符（3.9 用它）；
      `body_pt=None` ⇒ 不声明字号（3.5 用它）。
    `notes`：与 `slides` 等长的 list，元素为 str（有备注页）或 None（没备注页）。
    `sld_sz`：画布尺寸 EMU，默认 **16:9**（12192000×6858000）。
      要造"默认空白模板"那种 4:3，显式传 `(9144000, 6858000)`（§18 用它）。
    `layout`：给了就给每页写一条指向 slideLayout 的关系。
      传 **str** ⇒ 每页都指向同一个 ⇒ 造"通篇一种版式"（`layout_monotone` 正控）。
      传 **list** ⇒ 逐页指定（元素可为 `None` 表示该页不写 rels）
      ⇒ 造"有版式分工"的 deck（`layout_monotone` 负控）。**两侧都要能造**，
      否则"单调"这条判据只能测单向 —— 而单向测试正是它上一版翻车的原因。
      ⚠️ 判据只看 `Target` 里有没有 `slideLayout`，所以不必真造 layout 部件。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ppt/presentation.xml", _xml(
            f'<p:sldSz cx="{int(sld_sz[0])}" cy="{int(sld_sz[1])}"/>', "p:presentation"))
        for i, s in enumerate(slides, 1):
            parts = []
            if s.get("title") is not None:
                parts.append(_sp("title", [(s.get("title_pt", 28.0), s["title"])]))
            if s.get("body"):
                parts.append(_sp("body", [(s.get("body_pt", 18.0), s["body"])]
                                 + list(s.get("extra") or [])))
            if s.get("untitled_body"):
                # 不挂 ph 的裸形状 —— 既不是标题也不是 body 占位符
                parts.append(_sp("body", [(s.get("body_pt", 18.0),
                                           s["untitled_body"])]))
            z.writestr(f"ppt/slides/slide{i}.xml",
                       _xml(f"<p:cSld><p:spTree>{''.join(parts)}</p:spTree></p:cSld>", "p:sld"))
        if layout:
            lays = ([layout] * len(slides)) if isinstance(layout, str) else list(layout)
            for i, lay in enumerate(lays, 1):
                if not lay:
                    continue            # 该页不写 rels ⇒ 量不到它的版式
                z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels",
                           '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                           f'<Relationships xmlns="{_RELNS}">'
                           f'<Relationship Id="rId1" Type="{_LAYOUT_REL}"'
                           f' Target="../slideLayouts/{lay}"/></Relationships>')
        for i, n in enumerate(notes or [], 1):
            if n is None:
                continue
            z.writestr(f"ppt/notesSlides/notesSlide{i}.xml", _notes_xml(n, i))
            z.writestr(f"ppt/notesSlides/_rels/notesSlide{i}.xml.rels",
                       _rels_xml(f"../slides/slide{i}.xml"))
        for k in range(media):
            z.writestr(f"ppt/media/image{k + 1}.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


_BODY = "这一页的正文写到这里就应该停手，剩下的交给讲稿去说。"   # 25 字，> 门槛 12

# ★ 真实成片的标题（`runs/20260921-124950/deck.pptx`）—— **合规 deck 该长的样子**。
#   为什么把它抬成模块级常量（原来只硬编码在 §20 的误报守卫里）：
#     §3.1 / §6.3 / §19.3 是"合规 deck ⇒ PASS"的**基线正控**，而它们原先用的
#     标题是「第 N 部分的小标题」—— 那是**名词短语**，论证层判据上线后如实报 WARN，
#     于是基线正控转红（实测 4 项：3.1 / 6.3 / 19.8 / 19.9）。
#   ⚠️ 这**不是**"改测试迁就代码"，理由是拿数据分清的（`_probe_title_corpus.py`）：
#     真实任务目录 26 个 / 其中 .pptx 3 份 ⇒ 该判据命中率 **0%**（标题都是结论句）；
#     合成夹具 ⇒ 100%。**判据在真实稿子上不响、在"起手那份丑 deck"上响，它是对的**；
#     错的是拿丑夹具当"合规基线"（`make_real_pptx` 的 docstring 自己写着
#     "正是'起手那份丑 deck'的真实长相"）。
#   ⇒ 夹具向真实产物看齐 = **加强**（正控现在同时守论证层那一维）。
#     「名词短语必须响」的覆盖由 §20.1 专职守着，**没有丢**。
GOOD_TITLES = [
    "低温诱导冬虫夏草子实体分化：阈值与时长之争",
    "子实体诱导是资源化利用的主要卡点",
    "适宜温度报道分散在 4–15℃",
    "同一现象，两个相反结论",
    "我们的判断：差异来自诱导时长不受控",
    "研究设计：3 温度 × 3 时长，每组 120",
    "低温必要，尚不充分",
    "原基出现率：温度不够可以用时间补",
    "转化率随诱导时长单调上升",
    "延长诱导的代价：同步度下降",
    "推测：低温作用于早期开关",
    "现有证据只到表型层面",
    "下一步：收窄阈值，锁定开关基因",
]


def green_slides(n=6, body_pt=18.0, title_pt=28.0, titles=None):
    """一张"结构上全绿"的页表。

    `titles`：默认 `None` ⇒ 「第 N 部分的小标题」——**刻意保留这个默认**，
      因为它是"起手那份丑 deck"的标题长相，§20.1 的正控就靠它（28 个调用点里
      绝大多数只关心字号/页数/备注，与标题形态无关）。
      ⚠️ **凡断言 `type` 门 PASS 的地方，必须传结论式标题**（用 `GOOD_TITLES`）：
      名词短语标题会如实触发 `titles_are_noun_phrases` WARN ⇒ 把 verdict 拉成
      REVIEW REQUIRED。**"合规"的定义在论证层上线后变了，夹具要跟上。**
    """
    if titles is None:
        titles = [f"第 {i} 部分的小标题" for i in range(1, n + 1)]
    ts = [str(t) for t in list(titles)]
    if len(ts) < n:                       # 不够就明说，别静默少造几页
        raise ValueError(f"titles 只给了 {len(ts)} 条，要 {n} 条")
    return [{"title": ts[i], "body": _BODY,
             "body_pt": body_pt, "title_pt": title_pt} for i in range(n)]


def _png_1x1(rgb=(60, 84, 136)) -> bytes:
    """纯标准库造一张合法的 1×1 PNG。

    为什么要手写而不是用 PIL：`deck_test` 的立身之本就是**零依赖**
    （3.13 那个子集还要靠它）。为了造一张色块去引入 Pillow，会把
    这个性质卖掉。PNG 的最小结构只有三个 chunk，手写反而更稳。
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)      # 1×1, 8bit, truecolor
    raw = b"\x00" + bytes(rgb)                               # 行首 filter 字节 + 像素
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _png_grey_bytes(greys, w: int, h: int) -> bytes:
    """纯标准库造一张 `w×h` 的 8bit truecolor PNG，每像素亮度 = `greys[i]`。

    为什么测试要自己写 PNG：像素校验这条路的**成功分支**得有一张真的渲染图
    才能走到底，而真渲染要走 PowerPoint（本机 ~15 秒、还抢用户窗口）。
    手写一个三 chunk 的 PNG ⇒ 把"渲染成什么样"变成测试里可控的输入 ⇒
    深底浅字 / 浅底深字 / 纯色 三类都能端到端跑一遍。
    与 `_png_1x1` 同一个理由：**不为了造色块去引入 Pillow**。
    """
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    rows = bytearray()
    for y in range(h):
        rows.append(0)                                  # 行首 filter 字节：None
        for x in range(w):
            g = greys[y * w + x]
            rows += bytes((g, g, g))                    # truecolor，R=G=B ⇒ 灰
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))


def make_real_pptx(path, py: str, slides: int = 6, layout_idx=None, image=None,
                   titles=None) -> bool:
    """用 python-pptx 造一份**真的** .pptx（若干页「标题＋内容」）。

    为什么 §18 的负控必须用它：`make_pptx` 造的极简 zip **没有
    `[Content_Types].xml`**，python-pptx 根本打不开（实测报
    `KeyError: "no member '/[Content_Types].xml' in package"`）。
    而 `style_deck` 要动的正是真文件 ⇒ **测能力就得喂真输入**，
    否则测的是夹具的边界，不是工具的能力。

    `Presentation()` 的默认模板就是 python-pptx 的**空白模板**：
    **4:3**（9144000×6858000）、Office 主题、Calibri，每页都挂同一个
    `Title and Content` 版式 —— 正是"起手那份丑 deck"的真实长相。
    比合成夹具更有说服力：**丑不是我们造的，是默认值就长这样。**

    `layout_idx`：逐页指定用 `prs.slide_layouts` 里的第几个。
      默认 `None` ⇒ 每页都用 1（Title and Content），即**默认模板的真实长相**：
      通篇一种版式（`layout_monotone` 正控要的就是它）。
      传 list 可造"有版式分工"的 deck（0=Title Slide / 5=Title Only）——
      §19 的负控要用它，而**"分工"这一侧的输入以前根本造不出来**，
      所以那条判据一直只被单向测过（它的误报就是这么漏过去的）。

    `image`：给了就在**最后一页**插一张图。
      存在的理由：「`style_deck` 重写整包不丢 part」是配图流程的**承重假设**
      —— 而"重写整包"恰恰是最容易丢 part 的操作（图/表/备注都在别的 part 里），
      所以必须有测试盯着它，不能靠"探针跑过一次看着没事"（§19.12）。

    `titles`：逐页标题。默认 `None` ⇒ 「第 N 部分」（名词短语，丑 deck 的长相，
      与 `green_slides` 同一个默认）。**要断言 type 门 PASS 就传 `GOOD_TITLES`。**
      传参走 `\n` 分隔而不是逗号 —— 真实标题里带逗号（「同一现象，两个相反结论」）。
    """
    idx = list(layout_idx) if layout_idx else [1] * int(slides)
    ttl = "\n".join(str(t) for t in titles) if titles else ""
    if not py:
        # ⚠️ **不许把 None 交给 subprocess** —— 见 `_py_pptx()` 里那段 K 型：
        #    `subprocess.run([None, …])` 抛的是 `TypeError`，它会**崩掉整个套件**，
        #    于是调用方的"前置"断言根本没机会开口。
        #    这里返回 False ⇒ 外面那条 `check("…前置：造出…", …)` 如实报 ✘
        #    ⇒ **"没查"被说出来，而不是变成"没跑到"**。
        return False
    code = (
        "import sys\n"
        "from pptx import Presentation\n"
        "from pptx.util import Inches\n"
        "prs = Presentation()\n"
        "idx = [int(x) for x in sys.argv[3].split(',')]\n"
        "ttl = sys.argv[5].split(chr(10)) if len(sys.argv) > 5 and sys.argv[5] else []\n"
        "for i, li in enumerate(idx):\n"
        "    s = prs.slides.add_slide(prs.slide_layouts[li])\n"
        "    try:\n"
        "        s.shapes.title.text = ttl[i] if i < len(ttl) else '第 %d 部分' % (i + 1)\n"
        "    except Exception:\n"
        "        pass\n"
        "    for ph in s.placeholders:\n"
        "        if ph.placeholder_format.idx != 0:\n"
        "            ph.text = '正文若干字，剩下的交给讲稿去说。'\n"
        "            break\n"
        "if sys.argv[4]:\n"
        "    s.shapes.add_picture(sys.argv[4], Inches(1.2), Inches(1.8), Inches(6.0))\n"
        "prs.save(sys.argv[1])\n"
    )
    r = subprocess.run([py, "-c", code, str(path), str(len(idx)),
                        ",".join(str(i) for i in idx), image or "", ttl],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and Path(path).is_file()


class _Cfg:
    """审计器配置的替身（数值与 `config.json` 的 `deck` 段一致）。"""
    enabled = True
    python = ""
    skills_dir = ""
    timeout = 30.0
    min_slides = 6
    max_slides = 40
    min_notes_ratio = 0.6
    min_font_pt = 12.0
    max_title_chars = 40
    max_body_chars_per_slide = 600


def _aud() -> DeckAuditor:
    return DeckAuditor(cfg=_Cfg())


def _gate(rep: DeckReport, name: str) -> GateResult:
    for g in rep.gates:
        if g.name == name:
            return g
    raise AssertionError(f"报告里没有 {name} 门：{[g.name for g in rep.gates]}")


# ══════════════════════════════════════════════════════════════════
# 1 · 意图判定
# ══════════════════════════════════════════════════════════════════
def test_intent() -> None:
    section("1 意图判定（组会 PPT / 讲稿）")
    yes = ["把这篇文章做成组会汇报的 PPT", "做个 15 分钟的组会PPT", "生成演示文稿发我",
           "把这批结果做成 slides", "答辩ppt 帮我改一版", "make slides from this paper",
           "帮我把汇报幻灯片排一下"]
    for t in yes:
        check(f"1.1 命中：{t}", _looks_like_deck(t))

    # ⚠️ 反控：这几句**不能**命中 —— 命中就会把"整理会议记录"变成硬门。
    # 这正是 `_DECK_STRONG` 刻意不收光秃秃「组会」「汇报」的原因。
    no = ["整理一下这次的组会记录", "汇报一下检索结果", "写一篇 1200 字综述",
          "把结果做成一张图", "帮我把这段文字润色一下", "总结一下组会上老师的意见"]
    for t in no:
        check(f"1.2 不命中（防误伤）：{t}", not _looks_like_deck(t))

    for t in ["做成 PPT，每页要讲稿", "PPT 的备注里写清来源", "要 speaker notes"]:
        check(f"1.3 要讲稿：{t}", contract_wants_notes(t))
    for t in ["做个组会 PPT", "生成演示文稿，别太长", "帮我把汇报幻灯片排一下"]:
        check(f"1.4 没要讲稿：{t}", not contract_wants_notes(t))

    check("1.5 命题是「任意一段命中」而非「全部命中」",
          _looks_like_deck("随便写点东西", "这里有 PPT") and
          not _looks_like_deck("随便写点东西", "没有关键词"))
    check("1.6 空输入不炸、且判否", not _looks_like_deck("", None)
          and not contract_wants_notes(None, ""))


# ══════════════════════════════════════════════════════════════════
# 1b · `slides` 阶段 ⇄ deck 判据（2026-10-21 补：真凶一直在这里）
# ══════════════════════════════════════════════════════════════════
def test_stage_agrees_with_deck() -> None:
    """★ 上面第 1 节只测了 `_looks_like_deck`（contract 那一侧）。

    「要报告却去做 PPT」的真凶却在**另一侧**：`stages.py` 的 `slides` 阶段
    曾经自己手抄了一份触发词表，而且**收了光秃秃的「汇报」「组会」**——
    `_looks_like_deck` 判否（对），`infer_stages` 却判出 `slides`（错），
    `loop` 再按「阶段含 slides ⇒ 挂 ppt 域」把 37 个 ppt 工具挂给模型。
    因为这一节从来不存在，3274 项全量回归是绿的，缺陷照样出厂。

    这里把两边**绑在一起**测，并且测的是「同一份词表」这个事实本身。"""
    from rongke import stages as S
    from rongke.stages import infer_stages

    section("1b `slides` 阶段与 deck 判据必须同一口径")

    # ── 单一真源：三个入口引用的是**同一个对象**，不是三次手抄 ──
    check("1b.1 ★★ `contract._DECK_STRONG` 就是 `stages._DECK_WORDS`（同一对象，不是抄的）",
          C._DECK_STRONG is S._DECK_WORDS,
          f"{id(C._DECK_STRONG)} vs {id(S._DECK_WORDS)}")
    _sl = next(s for s in S.STAGES if s.id == "slides")
    check("1b.2 ★★ `slides` 阶段的触发词也是同一个对象",
          _sl.triggers is S._DECK_WORDS, f"{id(_sl.triggers)}")
    for _w in ("汇报", "组会"):
        check(f"1b.3 裸词「{_w}」不在词表里（它俩就是当年误判 6/12 的来源）",
              _w not in S._DECK_WORDS)

    # ── 反控：这些句子以前会被判成 slides 阶段，把 ppt 域挂给模型 ──
    #    ★ `infer_stages` 返回的是 **Stage 对象列表**，不是 id 字符串列表
    #      （我第一版写成 `"slides" in st`，恒为假 —— 正控当场把这条判据自己抓住了）。
    def _has_slides(t: str) -> bool:
        return any(s.id == "slides" for s in infer_stages(t))

    _fp = ["汇报一下检索结果", "整理一下组会记录",
           "帮我写一份月度报告，向老师汇报进展", "总结一下组会上老师的意见",
           "核对一下这个月的实验记录", "写一篇 1200 字综述"]
    for t in _fp:
        st = [s.id for s in infer_stages(t)]
        check(f"1b.4 不判出 slides 阶段：{t}", "slides" not in st, str(st))
        check(f"1b.5 且与 deck 判据一致（都是否）：{t}", not _looks_like_deck(t))

    # ── 正控：真要演示文稿时两边的判定都必须为真，否则是漏判 ──
    _tp = ["把这篇文章做成组会汇报的 PPT", "做个 15 分钟的组会PPT",
           "生成演示文稿发我", "把这批结果做成 slides", "答辩ppt 帮我改一版",
           "make slides from this paper", "帮我把汇报幻灯片排一下",
           "出一份汇报 deck 给我"]
    for t in _tp:
        st = [s.id for s in infer_stages(t)]
        check(f"1b.6 判出 slides 阶段：{t}", "slides" in st, str(st))
        check(f"1b.7 且 deck 判据一致（都是真）：{t}", _looks_like_deck(t))

    # ── 不变量：门会拦的情形，工具一定已经挂上了 ──
    #    `_looks_like_deck` 为真却判不出 slides，等于「第 9 条硬门要 PPT，
    #    却没给模型 ppt 工具」——模型必然被卡死。反向也必须成立（上面 1b.4/1b.5
    #    测的就是反向）。两边全等才是同一口径。
    _all = _fp + _tp + ["", "随便写点东西", "PPT", "组会"]
    _bad = [t for t in _all if _looks_like_deck(t) != _has_slides(t)]
    check("1b.8 ★★ 两边判定逐个全等（门要 PPT ⇔ 阶段含 slides ⇔ 挂 ppt 域）",
          not _bad, str(_bad))

    # ── 1b.9 ★ 负控：把事故版本注回去，上面这些判据必须当场变红 ──
    #
    #    只证明"新判据全绿"不够 —— 它完全可能是空转的（比如判据里的词写错、
    #    或者被测的产出方值域跟我以为的不一样）。这一条把事故版本**真的**
    #    装回去跑一遍，要求至少 1b.4/1b.6 变红，跑完复原并复验复原干净。
    #    （这轮开发时我第一版判据写成 `"slides" in infer_stages(t)`，恒为假，
    #      正是 1b.6 这个正控把它自己抓出来的 —— 判据必须能抓自己的失效。）
    _OLD_WORDS = ("ppt", "汇报", "组会", "幻灯片", "做图汇报")
    _o_stages = S.STAGES
    _o_strong = C._DECK_STRONG
    _i = next(i for i, s in enumerate(S.STAGES) if s.id == "slides")
    _o = S.STAGES[_i]
    _nl = list(S.STAGES)
    _nl[_i] = S.Stage(_o.id, _o.name, _o.skill, _OLD_WORDS, _o.purpose)
    try:
        S.STAGES = tuple(_nl)
        C._DECK_STRONG = _OLD_WORDS
        _leak = [t for t in ("汇报一下检索结果", "整理一下组会记录") if _has_slides(t)]
        check("1b.9 ★ 负控：注入事故词表后「汇报一下检索结果」「整理一下组会记录」"
              "重新被判成 slides 阶段（判据不是空转）", len(_leak) == 2, str(_leak))
    finally:
        S.STAGES = _o_stages
        C._DECK_STRONG = _o_strong
    check("1b.10 负控注入已复原（复原后再判，两句话重新回到否）",
          not _has_slides("汇报一下检索结果") and not _has_slides("整理一下组会记录"))


# ══════════════════════════════════════════════════════════════════
# 2 · probe_pptx 量事实（N 型的老家）
# ══════════════════════════════════════════════════════════════════
def test_probe() -> None:
    section("2 probe_pptx 量事实")
    tmp = T.fresh_dir(_TMPROOT / "probe", "deck-probe")

    # ── 正控：3 页、每页都有备注、1 个媒体文件 ──
    a = make_pptx(tmp / "with_notes.pptx", green_slides(3, body_pt=18.0),
                  notes=["讲稿一：先交代来源。", "讲稿二：口径是三次独立重复。",
                         "讲稿三：会被问到的两个问题。"], media=1)
    fa = probe_pptx(a)
    check("2.1 页数", fa.slide_count == 3, str(fa.slide_count))
    check("2.2 备注页数", fa.notes_slides == 3, str(fa.notes_slides))
    check("2.3 有备注的页数", fa.slides_with_notes == 3, str(fa.slides_with_notes))
    check("2.4 备注覆盖率 100%", abs(fa.notes_ratio - 1.0) < 1e-9, str(fa.notes_ratio))
    check("2.5 媒体文件计数", fa.media_count == 1, str(fa.media_count))
    check("2.6 版心尺寸被记下（4:3=9144000×6858000 之外也要如实记）",
          (fa.slide_cx, fa.slide_cy) == (12192000, 6858000),
          f"{fa.slide_cx}×{fa.slide_cy}")
    check("2.7 正文字号量到 18 pt",
          fa.min_body_font_pt is not None and abs(fa.min_body_font_pt - 18.0) < 1e-6,
          str(fa.min_body_font_pt))
    check("2.8 标题文字被记下（否则「标题过长」永不报 —— G 型）",
          all(s["title_text"].startswith("第 ") for s in fa.slides),
          str([s["title_text"] for s in fa.slides]))

    # ★★ N 型回归守卫：**计数对、正文恒空**是当初那个 bug 的全部症状。
    #    只断言 notes_slides 是抓不到它的（那个数从头到尾都是对的）。
    check("2.9 ★ 每页备注**正文非空**（只查计数抓不到 N 型那个 bug）",
          all(s["notes_chars"] > 0 for s in fa.slides),
          str([(s["slide"], s["notes_chars"]) for s in fa.slides]))
    # ★★ 这条同时钉住两件事：
    #   ① 备注正文真的被读出来了（不是空壳）；
    #   ② 备注页里的 **sldNum 占位符被排除**了 —— 否则每页会多出页码那 1~2 个字。
    _notes_in = ["讲稿一：先交代来源。", "讲稿二：口径是三次独立重复。",
                 "讲稿三：会被问到的两个问题。"]
    check("2.10 ★ 备注字数==真实备注长度（证明 sldNum 页码没被算成讲稿）",
          all(fa.slides[i]["notes_chars"] == len(_notes_in[i]) for i in range(3)),
          str([(fa.slides[i]["notes_chars"], len(_notes_in[i])) for i in range(3)]))

    # ── 负控 1：完全没有备注 ──
    b = make_pptx(tmp / "no_notes.pptx", green_slides(3))
    fb = probe_pptx(b)
    check("2.11 无备注 ⇒ 覆盖率 0", fb.slides_with_notes == 0 and fb.notes_ratio == 0.0,
          f"{fb.slides_with_notes}/{fb.ratio if hasattr(fb, 'ratio') else fb.notes_ratio}")
    check("2.12 无备注 ⇒ 备注页数也是 0（没造假的空壳）", fb.notes_slides == 0)

    # ── 负控 2：备注页只有 sldNum（页码），正文为空 ──
    #    这是最阴的一种假绿：备注**页存在**，但里面只有页码。
    c = make_pptx(tmp / "empty_notes.pptx", green_slides(3), notes=["", "", ""])
    fc = probe_pptx(c)
    check("2.13 备注页只有页码 ⇒ 不算有讲稿（不许把页码读成讲稿）",
          fc.notes_slides == 3 and fc.slides_with_notes == 0,
          f"notes_slides={fc.notes_slides}, with_notes={fc.slides_with_notes}")

    # ── 负控 3：文件不存在 / 不是 zip ──
    fmiss = probe_pptx(tmp / "根本不存在.pptx")
    check("2.14 文件不存在 ⇒ 留下 problems，页数为 0",
          fmiss.slide_count == 0 and bool(fmiss.problems), str(fmiss.problems[:1]))
    bad = tmp / "not_a_zip.pptx"
    bad.write_bytes("这不是一个 zip".encode("utf-8"))
    fbad = probe_pptx(bad)
    check("2.15 不是合法 pptx ⇒ 留下 problems（不抛异常）",
          fbad.slide_count == 0 and bool(fbad.problems), str(fbad.problems[:1]))

    # ── 混合：部分页有备注 ──
    d = make_pptx(tmp / "mixed.pptx", green_slides(4),
                  notes=["有讲的", None, "也有讲的", None])
    fd = probe_pptx(d)
    check("2.16 部分页有备注 ⇒ 覆盖率 2/4 = 50%",
          fd.slides_with_notes == 2 and abs(fd.notes_ratio - 0.5) < 1e-9,
          f"{fd.slides_with_notes}/{fd.slide_count}={fd.notes_ratio}")

    # ── 继承母版：不写 sz ⇒ 记 None，不许拿默认值填个好看的数 ──
    e = make_pptx(tmp / "inherited.pptx", green_slides(2, body_pt=None))
    fe = probe_pptx(e)
    check("2.17 正文未显式声明字号 ⇒ min_body_font_pt 记 None（宁可不出数，不出假数）",
          fe.min_body_font_pt is None, str(fe.min_body_font_pt))
    # 两个字段是**两种口径**，不许混：全篇最小值含标题（28 pt 是显式声明的），
    # 正文最小值只算正文体量的形状。混起来就会出现"正文字号 28 pt"这种假事实。
    check("2.18 但标题的显式字号仍被量到（全篇最小值 ≠ 正文最小值）",
          fe.min_font_pt is not None and abs(fe.min_font_pt - 28.0) < 1e-6,
          f"all={fe.min_font_pt}, body={fe.min_body_font_pt}")

    # ★★ 2026-09-21 补的回归：上面 2.14 / 2.15 只验到 **facts 层**
    #    （`slide_count == 0 and bool(problems)`），**从来没人把这两条输入喂进
    #    `audit()` + `format_for_model()`**。于是一个真实的崩溃活了下来：
    #    `facts.problems` 是 `list[str]`（三处 append 全塞纯字符串），
    #    `format_for_model()` 却按 Finding 去读 `.check_id` ⇒
    #    **报告恰好在"稿子读不出来"的那一刻崩掉**，而那正是最需要它的时刻。
    #    实测（runs/20260921-113729）：模型把 `.docx` 交给 `audit_deck`，
    #    拿回来的是 `AttributeError: 'str' object has no attribute 'check_id'`，
    #    而不是「这不是 pptx」—— 它于是完全无法自纠。
    #
    #    这段同时钉住两件事，缺一不可：
    #      ① 不抛异常；
    #      ② **那句话真的印进了给模型看的那份报告**。
    #    只断言 ① 会漏掉 G 型（记在 facts 里、却从不显示）。
    for _label, _path, _must in (("文件不存在", tmp / "根本不存在.pptx", "文件不存在"),
                                 ("不是合法 pptx", bad, "不是合法的 .pptx")):
        _rep = _aud().audit(_path, workdir=tmp)
        try:
            _txt, _boom = _rep.format_for_model(), ""
        except Exception as e:
            _txt, _boom = "", f"{type(e).__name__}: {e}"
        check(f"2.19 ★ {_label} ⇒ format_for_model() 不抛异常（原实现在此崩）",
              not _boom, _boom)
        check(f"2.20 ★ {_label} ⇒ 那句话**真的印进了报告**（不是只记在 facts.problems）",
              _must in _txt, _txt[:200])
        check(f"2.21 {_label} ⇒ verdict 不是 PASS（读不出稿子不许算过）",
              _rep.verdict != "PASS", _rep.verdict)
    # 负控：**合格的** deck 的报告里不许出现 problem 行（别把没问题说成有问题 ——
    # 光有正控没有负控，"永远印一句 problem"也能全绿）。
    check("2.22 负控：合格 deck 的报告里**没有** problem 行",
          "[problem]" not in _aud().audit(a, workdir=tmp).format_for_model())

    T.stash(tmp, "deck-probe")


# ══════════════════════════════════════════════════════════════════
# 3 · type 门（荣珂自己收紧的版式判据）
# ══════════════════════════════════════════════════════════════════
def test_type_gate() -> None:
    section("3 type 门（版式）")
    tmp = T.fresh_dir(_TMPROOT / "type", "deck-type")
    au = _aud()

    # 正控：合规 deck ⇒ PASS
    #   ⚠️ 必须传 `GOOD_TITLES`：论证层判据上线后，「第 N 部分的小标题」这种
    #   名词短语标题会如实触发 WARN ⇒ 把 verdict 拉成 REVIEW REQUIRED。
    #   **"合规"的定义变宽了（多了论证层），基线正控的输入要跟上**，
    #   而"名词短语必须响"由 §20.1 专职守着。
    g = au._gate_type(probe_pptx(make_pptx(
        tmp / "ok.pptx", green_slides(6, body_pt=18.0, titles=GOOD_TITLES[:6]))))
    check("3.1 合规 deck（6 页 / 18pt / 结论式标题）⇒ PASS",
          g.verdict == "PASS", f"{g.verdict} {g.detail}")

    # ── 字号 ──
    g = au._gate_type(probe_pptx(make_pptx(tmp / "tiny.pptx", green_slides(6, body_pt=6.0))))
    check("3.2 正文 6 pt（低于客观可读下限 8 pt）⇒ 阻断", g.verdict == "FIX BEFORE DELIVERY", g.verdict)
    check("3.3 且点名 font_too_small",
          any(f.check_id == "font_too_small" for f in g.findings), str([f.check_id for f in g.findings]))

    # 反控：低于**期望**但高于**可读下限** ⇒ 只提醒，不阻断（误报比漏报更伤）
    g = au._gate_type(probe_pptx(make_pptx(tmp / "small.pptx", green_slides(6, body_pt=10.0))))
    check("3.4 正文 10 pt（≥可读下限，<期望 12）⇒ 只 WARN，不阻断",
          g.verdict == "REVIEW REQUIRED"
          and any(f.check_id == "font_small" for f in g.findings), f"{g.verdict} {g.detail}")

    # 边界守卫：恰好 = 下限不许判 FAIL（判据是"低于"，不是"不高于"）
    g = au._gate_type(probe_pptx(make_pptx(tmp / "edge.pptx", green_slides(6, body_pt=HARD_MIN_FONT_PT))))
    check(f"3.5 正文恰好 {HARD_MIN_FONT_PT:.0f} pt ⇒ 不许判 FAIL（边界含不含必须钉住）",
          not any(f.check_id == "font_too_small" for f in g.findings),
          str([f.check_id for f in g.findings]))

    # ★ M 型守卫：量不出来 ≠ 通过
    g = au._gate_type(probe_pptx(make_pptx(tmp / "inh.pptx", green_slides(6, body_pt=None))))
    check("3.6 ★ 字号量不出来时留下 SKIP（不许渲染成「通过」）",
          any(f.check_id == "font_unmeasurable" and f.level == "SKIP" for f in g.findings),
          str([(f.check_id, f.level) for f in g.findings]))
    check("3.7 且措辞里明说「没查」",
          any("没查" in f.message for f in g.findings if f.check_id == "font_unmeasurable"))

    # ── 页数：只提醒，不阻断 ──
    g = au._gate_type(probe_pptx(make_pptx(tmp / "short.pptx", green_slides(3, body_pt=18.0))))
    check("3.8 只有 3 页 ⇒ WARN 不阻断（3 页进展速报是合法的）",
          g.verdict == "REVIEW REQUIRED"
          and any(f.check_id == "slide_count_small" for f in g.findings), f"{g.verdict} {g.detail}")
    g = au._gate_type(probe_pptx(make_pptx(tmp / "long.pptx", green_slides(45, body_pt=18.0))))
    check("3.9 45 页 ⇒ WARN 不阻断",
          g.verdict == "REVIEW REQUIRED"
          and any(f.check_id == "slide_count_large" for f in g.findings), g.verdict)

    # 0 页：这是**空文件**，与"页数少"是两件事
    empty = DeckFacts(path="(none)")
    g = au._gate_type(empty)
    check("3.10 0 页 ⇒ 阻断（不是「页数偏少」的提醒）",
          g.verdict == "FIX BEFORE DELIVERY"
          and any(f.check_id == "empty_deck" for f in g.findings), f"{g.verdict} {g.detail}")
    g = au._gate_type(DeckFacts(path="(bad)", problems=["不是合法的 .pptx"]))
    check("3.11 文件本身有问题 ⇒ NOT AUDITABLE（不是 PASS，也不是 FIX）",
          g.verdict == "NOT AUDITABLE", g.verdict)

    # ── 单页字数 ──
    fat = green_slides(6, body_pt=18.0)
    fat[2]["body"] = "把整段论文粘上来" * 80
    g = au._gate_type(probe_pptx(make_pptx(tmp / "fat.pptx", fat)))
    check("3.12 单页 700+ 字 ⇒ WARN slide_text_overload 并点名页码",
          any(f.check_id == "slide_text_overload" and "slide=3" in f.evidence
              for f in g.findings), str([(f.check_id, f.evidence) for f in g.findings]))

    # ── 标题 ──
    nt = green_slides(6, body_pt=18.0)
    nt[1]["title"] = None
    nt[1]["untitled_body"] = _BODY
    g = au._gate_type(probe_pptx(make_pptx(tmp / "untitled.pptx", nt)))
    check("3.13 有一页没标题占位符 ⇒ WARN slide_without_title",
          any(f.check_id == "slide_without_title" for f in g.findings),
          str([f.check_id for f in g.findings]))

    # ★★ G 型回归：这条检查曾经**永不报**（读到 s["title_text"] 但那字段没被记）。
    # 41 字 vs 下面的 40 字构成一对边界断言。
    lt = green_slides(6, body_pt=18.0)
    lt[3]["title"] = "字" * 41
    g = au._gate_type(probe_pptx(make_pptx(tmp / "longtitle.pptx", lt)))
    check("3.14 ★ 标题 41 字（超上限 40）⇒ WARN title_too_long"
          "（此检查曾经恒不报：G 型）",
          any(f.check_id == "title_too_long" for f in g.findings),
          str([(f.check_id, f.message[:40]) for f in g.findings]))

    # 反控：正好 40 字不报
    eq = green_slides(6, body_pt=18.0)
    eq[3]["title"] = "字" * 40
    g = au._gate_type(probe_pptx(make_pptx(tmp / "eqtitle.pptx", eq)))
    check("3.15 标题恰好 40 字 ⇒ 不报（边界）",
          not any(f.check_id == "title_too_long" for f in g.findings), g.detail)

    T.stash(tmp, "deck-type")


# ══════════════════════════════════════════════════════════════════
# 4 · notes 门（只报事实，不终审）
# ══════════════════════════════════════════════════════════════════
def test_notes_gate() -> None:
    section("4 notes 门")
    tmp = T.fresh_dir(_TMPROOT / "notes", "deck-notes")
    au = _aud()

    full = probe_pptx(make_pptx(tmp / "full.pptx", green_slides(6),
                                notes=["讲稿" + str(i) for i in range(6)]))
    g = au._gate_notes(full)
    check("4.1 6/6 覆盖 ⇒ PASS", g.verdict == "PASS", f"{g.verdict} {g.detail}")

    none = probe_pptx(make_pptx(tmp / "none.pptx", green_slides(6)))
    g = au._gate_notes(none)
    check("4.2 0/6 ⇒ REVIEW REQUIRED（默认不阻断）", g.verdict == "REVIEW REQUIRED", g.verdict)
    check("4.3 且明说「上游 37 个工具没有写备注的能力」这个真实约束",
          any("备注" in f.message and "python-pptx" in f.message for f in g.findings),
          str([f.message[:60] for f in g.findings]))
    check("4.4 证据里带上覆盖率三个数（可复核，不靠措辞）",
          any(any("ratio=" in e for e in f.evidence) for f in g.findings),
          str([f.evidence for f in g.findings]))

    # 边界：恰好 = 阈值算过（`>= want`）
    half = probe_pptx(make_pptx(tmp / "half.pptx", green_slides(10),
                                notes=["讲稿"] * 6 + [None] * 4))
    g = au._gate_notes(half)
    check("4.5 覆盖率恰好 60%（= 阈值）⇒ PASS（边界含，判据是「不低于」）",
          g.verdict == "PASS", f"{g.verdict} {g.detail}")

    just_below = probe_pptx(make_pptx(tmp / "jb.pptx", green_slides(10),
                                      notes=["讲稿"] * 5 + [None] * 5))
    g = au._gate_notes(just_below)
    check("4.6 覆盖率 50%（差一点点）⇒ REVIEW REQUIRED",
          g.verdict == "REVIEW REQUIRED", g.verdict)

    g = au._gate_notes(DeckFacts(path="(empty)"))
    check("4.7 没有幻灯片 ⇒ NOT APPLICABLE（不是 FAIL，也不是 PASS 冒充）",
          g.verdict == "NOT APPLICABLE", g.verdict)

    g_bad = au._gate_notes(none)
    check("4.8 ★ 本门不许自己升级成阻断 —— 升级的判据在契约侧（R 型防线）",
          g_bad.verdict == "REVIEW REQUIRED"
          and all(f.level != "FAIL" for f in g_bad.findings),
          f"{g_bad.verdict} {[f.level for f in g_bad.findings]}")

    T.stash(tmp, "deck-notes")


# ══════════════════════════════════════════════════════════════════
# 5 · freshness 门 + verdict 词表（M 型防线）
# ══════════════════════════════════════════════════════════════════
def test_freshness_and_vocab() -> None:
    section("5 freshness 门 + verdict 词表")
    tmp = T.fresh_dir(_TMPROOT / "fresh", "deck-fresh")
    au = _aud()
    now = time.time()

    p = make_pptx(tmp / "fresh.pptx", green_slides(6), mtime=now - 10)
    g = au._gate_freshness(p, started_at=now)
    check("5.1 报告比 pptx 新 ⇒ PASS", g.verdict == "PASS", f"{g.verdict} {g.detail}")

    # ★ 承重：pptx 在审计**之后**被改过 —— 「报告在位」≠「报告对得上这一版」
    p2 = make_pptx(tmp / "stale.pptx", green_slides(6), mtime=now + 60)
    g = au._gate_freshness(p2, started_at=now)
    check("5.2 ★ pptx 晚于审计开始 ⇒ STALE（旧报告不算合格证）",
          g.verdict == "STALE", f"{g.verdict} {g.detail}")
    check("5.3 且留下 stale_artifact 证据",
          any(f.check_id == "stale_artifact" for f in g.findings),
          str([f.check_id for f in g.findings]))

    # 容差边界：容差 2 秒 —— 写盘与取时间里外差一点点不该算过期
    p3 = make_pptx(tmp / "tol.pptx", green_slides(6), mtime=now + 1.5)
    check("5.4 容差内（+1.5s）不判过期（否则恒报，人学会无视它）",
          au._gate_freshness(p3, started_at=now).verdict == "PASS")

    g = au._gate_freshness(tmp / "gone.pptx", started_at=now)
    check("5.5 pptx 不存在 ⇒ NOT AUDITABLE（不是 PASS）", g.verdict == "NOT AUDITABLE", g.verdict)

    # ── 词表：不许把"审不了"压成"通过" ──
    check("5.6 SKIPPED 不算 ok", GateResult(name="x", verdict="SKIPPED").ok is False)
    check("5.7 NOT AUDITABLE 不算 ok 且算阻断",
          GateResult(name="x", verdict="NOT AUDITABLE").ok is False
          and GateResult(name="x", verdict="NOT AUDITABLE").blocking is True)
    check("5.8 STALE 算阻断", GateResult(name="x", verdict="STALE").blocking is True)
    check("5.9 NOT APPLICABLE 算 ok 但**不**算阻断（「与这件事无关」和「没有」是两件事）",
          GateResult(name="x", verdict="NOT APPLICABLE").ok is True
          and GateResult(name="x", verdict="NOT APPLICABLE").blocking is False)
    check("5.10 REVIEW REQUIRED 不阻断（它是要人看一眼，不是拦）",
          GateResult(name="x", verdict="REVIEW REQUIRED").blocking is False)

    T.stash(tmp, "deck-fresh")


# ══════════════════════════════════════════════════════════════════
# 6 · audit() 端到端 + 接口不变量（K 型路径 / H 型漂移）
# ══════════════════════════════════════════════════════════════════
def test_audit_e2e() -> None:
    section("6 audit() 端到端")
    tmp = T.fresh_dir(_TMPROOT / "e2e", "deck-e2e")
    make_pptx(tmp / "小组汇报.pptx", green_slides(6, body_pt=18.0,
                                                  titles=GOOD_TITLES[:6]),
              notes=["讲稿 %d" % i for i in range(6)], media=2)

    # ★ K 型回归：**喂相对路径**、同时 `cwd=str(pptx.parent)` 起子进程。
    #   审计器若不把路径钉成绝对值，子进程会在新 cwd 下找不到文件 ⇒
    #   报 "PPTX not found" ⇒ xml 门 SKIPPED ⇒ 整份报告"阻断"，
    #   看起来像 deck 有问题，其实是路径没解析。
    prev = Path.cwd()
    try:
        os.chdir(tmp)
        rep = _aud().audit("小组汇报.pptx", workdir=tmp)
    finally:
        os.chdir(prev)

    check("6.1 ★ 相对路径 + workdir ⇒ 报告里是**绝对**路径（K 型：子进程 cwd 变了）",
          Path(rep.pptx).is_absolute() and Path(rep.pptx).is_file(), rep.pptx)
    check("6.2 七道门都在（open/xml/type/density/contrast/notes/freshness）",
          {g.name for g in rep.gates}
          == {"open", "xml", "type", "density", "contrast", "notes", "freshness"},
          str([g.name for g in rep.gates]))
    check("6.3 type 门在合规 deck 上 PASS", _gate(rep, "type").verdict == "PASS",
          _gate(rep, "type").detail)
    check("6.4 notes 门 6/6 ⇒ PASS", _gate(rep, "notes").verdict == "PASS",
          _gate(rep, "notes").detail)
    check("6.5 freshness 门 PASS", _gate(rep, "freshness").verdict == "PASS")
    check("6.6 open 门记下页数与媒体数",
          "6 页" in _gate(rep, "open").detail and "2 个媒体" in _gate(rep, "open").detail,
          _gate(rep, "open").detail)
    check("6.7 判定落在词表内",
          rep.verdict in ("PASS", "PASS WITH WARNINGS", "REVIEW REQUIRED",
                          "FIX BEFORE DELIVERY"), rep.verdict)
    check("6.8 ok ⇔ 没有阻断项（两个口径不许分家）", rep.ok == (not rep.blockers))

    # ★ H 型防线：`deck.py` 与 `gate.py` 之间的**接口不变量**。
    #   门要读这些键；报告少给一个，门就会静默地把"没读到"当"没问题"。
    need = {"ok", "verdict", "blockers", "gates", "notes_ratio", "pptx_mtime",
            "pptx", "warnings"}
    sj = rep.summary_json()
    check("6.9 ★ summary_json 带齐第 9 条门要读的每一个键",
          need <= set(sj), str(sorted(need - set(sj))))
    check("6.10 gates 是 {门名: 判定} 的扁平字典（门就是按这个读的）",
          isinstance(sj.get("gates"), dict)
          and all(isinstance(v, str) for v in sj["gates"].values()), str(sj.get("gates")))
    check("6.11 notes_ratio 是数值不是字符串（门拿它去比大小）",
          isinstance(sj.get("notes_ratio"), (int, float)), str(type(sj.get("notes_ratio"))))
    check("6.12 pptx_mtime 是数值（门拿它跟磁盘现状比）",
          isinstance(sj.get("pptx_mtime"), (int, float))
          and abs(sj["pptx_mtime"] - Path(rep.pptx).stat().st_mtime) < 1e-6,
          str(sj.get("pptx_mtime")))

    # 落盘 / 回读
    saved = rep.save(tmp / "deck_report.json")
    check("6.13 报告落盘成功且路径被写进报告自身", Path(saved).is_file()
          and rep.report_path == str(Path(saved)), rep.report_path)
    reread = json.loads(Path(saved).read_text(encoding="utf-8"))
    check("6.14 落盘的是完整报告（含 facts 与逐门明细）",
          "facts" in reread and "gate_details" in reread
          and isinstance(reread["gate_details"], list), str(sorted(reread)[:8]))
    check("6.15 回读的判定与内存里的一致",
          reread["verdict"] == rep.verdict and reread["ok"] == rep.ok)
    check("6.16 每道门在落盘版里都带 verdict 与 detail",
          all("verdict" in g and "detail" in g for g in reread["gate_details"]))
    txt = rep.format_for_model()
    check("6.17 人读版开头就交代判定与页数/备注覆盖率",
          "判定：" in txt and "讲稿备注：" in txt, txt[:80])
    check("6.18 人读版逐门列出来（模型能定位到是哪道门）",
          all(g.name in txt for g in rep.gates))

    # 默认报告不落在别处
    check("6.19 不给 report_path 时，审计中间产物落在 pptx 同目录（围栏内）",
          (tmp / "deck_audit.json").is_file() or _gate(rep, "xml").verdict == "SKIPPED",
          str(_gate(rep, "xml").verdict))

    T.stash(tmp, "deck-e2e")


# ══════════════════════════════════════════════════════════════════
# 7 · 第 9 条硬门
# ══════════════════════════════════════════════════════════════════
def _deck_registry(payload_json: str, *, register_tool: bool = True) -> ToolRegistry:
    """桩注册表。

    ⚠️ 必须**真的装载** `nature-paper2ppt`：契约从"组会汇报 PPT"这句里推断出
    `slides` 阶段，第 6 条**技能门**就会要求装载该阶段的唯一专属技能。
    这是内核的正确行为 —— 夹具要满足它，而不是把它绕过去
    （绕过去就等于把技能门从这条测试的覆盖面里删掉了）。
    """
    reg = ToolRegistry()
    if register_tool:
        reg.register(Tool("audit_deck", "stub", {"type": "object", "properties": {}},
                          fn=lambda **k: payload_json, domain="ppt", mutating=True))
    reg.register(Tool("deliver_task", "stub", {"type": "object", "properties": {}},
                      fn=lambda **k: ""))
    reg.register(Tool("load_skill", "stub", {"type": "object", "properties": {}},
                      fn=lambda **k: "技能全文…", domain="core"))
    reg.call("load_skill", {"name": "nature-paper2ppt"})
    return reg


def _payload(pptx: Path, **over) -> dict:
    d = {
        "ok": True, "verdict": "PASS", "pptx": str(pptx), "slide_count": 8,
        "notes_ratio": 0.875, "min_body_font_pt": 16.0, "strict_notes": False,
        "gates": {"open": "PASS", "xml": "PASS", "type": "PASS",
                  "notes": "PASS", "freshness": "PASS"},
        "blockers": [], "warnings": [],
        "report_path": str(pptx.parent / "deck_report.json"),
        "audited_at": "2026-09-21T09:30:00", "audited_at_epoch": time.time(),
        "pptx_mtime": pptx.stat().st_mtime,
    }
    d.update(over)
    return d


def _dj(**over) -> TaskContract:
    # ⚠️ 验收标准的措辞必须与 `_dclaim` 里 `evidence.acceptance` **逐字相同**：
    # 证据门做的是措辞匹配，一个字不同就会把正控判红，
    # 而那个假红会让人去查几百行 deck 代码 —— 实际上问题在测试自己的措辞上。
    base = dict(raw_goal="把这篇稿子做成组会汇报的 PPT",
                acceptance=["deck 体检全过"], needs_deck=True)
    base.update(over)
    return TaskContract(**base)


def _dclaim(seq: int) -> CompletionClaim:
    return CompletionClaim(
        deliverables=["小组汇报.pptx"],
        evidence=[EvidenceRef(tool="audit_deck", call_seq=seq,
                              shows="deck 体检全过", acceptance="deck 体检全过")],
        summary="PPT 已交付")


def test_deck_gate() -> None:
    section("7 第 9 条 deck 门")
    tmp = T.fresh_dir(_TMPROOT / "gate", "deck-gate")
    pptx = tmp / "小组汇报.pptx"
    pptx.write_bytes(b"PK\x03\x04 placeholder")

    js = lambda d: json.dumps(d, ensure_ascii=False, default=str)      # noqa: E731

    # ── 正控 ──
    reg = _deck_registry(js(_payload(pptx)))
    tc = reg.call("audit_deck", {})
    v = CompletionGate(reg).check(_dclaim(tc.seq), _dj())
    check("7.1 体检全绿 ⇒ 放行", v.passed, str(v.reasons))

    # ── 承重 E：没做体检 —— 这是这道门存在的全部理由 ──
    reg2 = _deck_registry(js(_payload(pptx)))
    reg2.call("deliver_task", {})
    v2 = CompletionGate(reg2).check(
        CompletionClaim(deliverables=["小组汇报.pptx"], summary="done"), _dj())
    check("7.2 ★ 要产出 deck 却没做体检 ⇒ 打回", not v2.passed, str(v2.reasons))
    check("7.3 打回理由点名 audit_deck", "audit_deck" in " ".join(v2.reasons))
    check("7.4 理由里说清「能生成 .pptx ≠ 能拿去讲」",
          "能拿去讲" in " ".join(v2.reasons) or "不报错" in " ".join(v2.reasons))

    # ── 反控：不是 deck 任务就不许要求体检（否则误报会毁掉这道门）──
    reg3 = _deck_registry(js(_payload(pptx)))
    tc3 = reg3.call("audit_deck", {})
    v3 = CompletionGate(reg3).check(_dclaim(tc3.seq), _dj(needs_deck=False))
    check("7.5 非 deck 任务不要求 deck 体检", v3.passed, str(v3.reasons))

    # 反控：needs_deck 但用 stage 推断出的 —— 也应生效
    reg3b = _deck_registry(js(_payload(pptx)))
    tc3b = reg3b.call("audit_deck", {})
    v3b = CompletionGate(reg3b).check(_dclaim(tc3b.seq), _dj(needs_deck=True, stages=["slides"]))
    check("7.6 needs_deck（来自 stage）同样生效", v3b.passed, str(v3b.reasons))

    # ── 承重 F：体检没过 ⇒ 拦，且带上具体阻断项 ──
    bad = _payload(pptx, ok=False, verdict="FIX BEFORE DELIVERY",
                   gates={"open": "PASS", "xml": "PASS", "type": "FIX BEFORE DELIVERY",
                          "notes": "PASS", "freshness": "PASS"},
                   blockers=["【type 门：FIX BEFORE DELIVERY】正文字号最小 6.0 pt，低于 8 pt"])
    reg4 = _deck_registry(js(bad))
    tc4 = reg4.call("audit_deck", {})
    v4 = CompletionGate(reg4).check(_dclaim(tc4.seq), _dj())
    check("7.7 体检不过 ⇒ 打回", not v4.passed, str(v4.reasons))
    check("7.8 打回时带上具体阻断项（不许只说「没过」）",
          "6.0 pt" in " ".join(v4.reasons), str(v4.reasons)[:200])
    check("7.9 并要求改完**重跑**（报告不会自己变新）",
          "重跑" in " ".join(v4.reasons))

    # ── 承重 G：报告过期 ──
    stale = _payload(pptx)
    stale["pptx_mtime"] = float(stale["pptx_mtime"]) - 3600.0   # 假装审的是上一版
    reg5 = _deck_registry(js(stale))
    tc5 = reg5.call("audit_deck", {})
    v5 = CompletionGate(reg5).check(_dclaim(tc5.seq), _dj())
    check("7.10 ★ 报告比 pptx 旧 ⇒ 判过期并打回（「报告在位」≠「对得上这一版」）",
          not v5.passed, str(v5.reasons))
    check("7.11 打回理由点名「过期」", "过期" in " ".join(v5.reasons))

    # 报告指向的 pptx 不见了
    gone = _payload(pptx)
    gone["pptx"] = str(tmp / "已经删掉了.pptx")
    reg5b = _deck_registry(js(gone))
    tc5b = reg5b.call("audit_deck", {})
    v5b = CompletionGate(reg5b).check(_dclaim(tc5b.seq), _dj())
    check("7.12 报告指向的 pptx 读不到 ⇒ 打回",
          not v5b.passed and "读不到" in " ".join(v5b.reasons), str(v5b.reasons)[:160])

    # ── 承重 H：工具整体没装上 ⇒ 降为警告，不许硬拦 ──
    reg6 = _deck_registry(js(_payload(pptx)), register_tool=False)
    # ⚠️ seq 必须**取实际值**，不能写死 1：夹具里 `load_skill` 先跑过，
    # 写死 1 会把证据指到 load_skill 头上，于是假红在"证据张冠李戴"，
    # 而这条测试想问的根本不是那件事。
    tc_d = reg6.call("deliver_task", {})
    v6 = CompletionGate(reg6).check(
        CompletionClaim(deliverables=["小组汇报.pptx"], summary="PPT 已出",
                        evidence=[EvidenceRef(tool="deliver_task", call_seq=tc_d.seq,
                                              shows="deck 体检全过",
                                              acceptance="deck 体检全过")]),
        _dj())
    check("7.13 工具未装配（deck.enabled=false）⇒ 降为警告不硬拦", v6.passed, str(v6.reasons))
    check("7.14 但如实说明本次没做 deck 体检",
          any("没有做 deck 体检" in w for w in v6.warnings), str(v6.warnings)[:160])

    # ── 承重 I：返回被截断 ⇒ 视同没做体检（半截 JSON 不是合格证）──
    reg7 = _deck_registry('{"ok": true, "gates": {"type": "PA')
    tc7 = reg7.call("audit_deck", {})
    v7 = CompletionGate(reg7).check(_dclaim(tc7.seq), _dj())
    check("7.15 返回被截断 ⇒ 视同没做体检", not v7.passed, str(v7.reasons)[:160])

    # ── 承重 J：工具调用失败 ──
    def _boom(**k):
        raise RuntimeError("找不到能跑审计的解释器")

    reg8 = ToolRegistry()
    reg8.register(Tool("audit_deck", "stub", {"type": "object", "properties": {}},
                       fn=_boom, domain="ppt", mutating=True))
    reg8.register(Tool("load_skill", "stub", {"type": "object", "properties": {}},
                       fn=lambda **k: "技能全文…", domain="core"))
    reg8.call("load_skill", {"name": "nature-paper2ppt"})
    tc8 = reg8.call("audit_deck", {})
    check("7.16 工具自身失败 ⇒ 该次调用 ok=False", not tc8.ok)
    v8 = CompletionGate(reg8).check(_dclaim(tc8.seq), _dj())
    check("7.17 失败调用不算体检 ⇒ 打回", not v8.passed, str(v8.reasons)[:160])

    # ── ★★ 承重 K（R 型防线）：讲稿判据的输入必须来自契约，不许来自模型自报 ──
    notes_bad = _payload(pptx, verdict="REVIEW REQUIRED",
                         gates={"open": "PASS", "xml": "PASS", "type": "PASS",
                                "notes": "REVIEW REQUIRED", "freshness": "PASS"},
                         warnings=["[notes] 讲稿备注只覆盖 0/8（0% < 期望 60%）"],
                         strict_notes=True)   # ← 模型自己在参数里填了 true
    reg9 = _deck_registry(js(notes_bad))
    tc9 = reg9.call("audit_deck", {"require_notes": True})
    # 契约原话**没有**要讲稿 ⇒ 必须放行（把模型自报的那一票作废）
    v9 = CompletionGate(reg9).check(_dclaim(tc9.seq), _dj())
    check("7.18 ★★ 模型自报要讲稿、但契约没要 ⇒ 必须放行（不采信被审者：R 型）",
          v9.passed, str(v9.reasons)[:220])

    # 反过来：契约原话**要了**讲稿 ⇒ 备注不合格必须拦
    reg10 = _deck_registry(js(dict(notes_bad, strict_notes=False)))
    tc10 = reg10.call("audit_deck", {"require_notes": False})
    v10 = CompletionGate(reg10).check(
        _dclaim(tc10.seq), _dj(raw_goal="把这篇稿子做成组会 PPT，每页要给讲稿"))
    check("7.19 ★★ 契约要了讲稿 + 备注不合格 ⇒ 必须打回（判据来自用户原话）",
          not v10.passed, str(v10.reasons)[:220])
    # ⚠️ 这条断言 2026-09-21 改过口径，理由值得留着：
    #    原先它核的是「理由里提到 `python-pptx` / `notes_slide`（技术名词）」——
    #    而**技术名词不是可执行的动作**。模型读到的是一句"跑一小段 python-pptx"，
    #    它做不到（内核没有执行代码的能力）⇒ 于是它去幻觉一个
    #    `apply_professional_design(operation="add_speaker_notes")`，再烧 19 次调用。
    #    ⇒ 判据改成：理由里必须出现**模型真的能调用的工具名**。
    #    **「我告诉它怎么做了」不等于「它做得到」。**
    _why = " ".join(v10.reasons)
    check("7.20 ★ 打回理由给的是**能调用的工具名**（`set_slide_notes`），不是技术名词",
          "set_slide_notes" in _why, _why[:240])
    check("7.21 ★ 且不再指向「另跑 python-pptx」那条它走不通的路",
          "另跑" not in _why, _why[:240])

    # 反控：契约要了讲稿，但 deck 一页都没有（NOT APPLICABLE）⇒ 不许误报
    reg11 = _deck_registry(js(_payload(pptx, gates={"open": "PASS", "xml": "PASS",
                                                   "type": "PASS",
                                                   "notes": "NOT APPLICABLE",
                                                   "freshness": "PASS"})))
    tc11 = reg11.call("audit_deck", {})
    v11 = CompletionGate(reg11).check(
        _dclaim(tc11.seq), _dj(raw_goal="做个 PPT，带讲稿"))
    check("7.21 NOT APPLICABLE（没有幻灯片）⇒ 不误报为缺讲稿", v11.passed,
          str(v11.reasons)[:200])

    # ── 词表守卫：`NOT AUDITABLE` 不许被当成通过 ──
    na = _payload(pptx, ok=False, verdict="FIX BEFORE DELIVERY",
                  gates={"open": "NOT AUDITABLE", "xml": "SKIPPED", "type": "SKIPPED",
                         "notes": "SKIPPED", "freshness": "PASS"},
                  blockers=["【open 门：NOT AUDITABLE】不是合法的 .pptx（zip 打不开）"])
    reg12 = _deck_registry(js(na))
    tc12 = reg12.call("audit_deck", {})
    v12 = CompletionGate(reg12).check(_dclaim(tc12.seq), _dj())
    check("7.22 ★ 审不了（NOT AUDITABLE）不许当通过", not v12.passed, str(v12.reasons)[:200])

    T.stash(tmp, "deck-gate")


# ══════════════════════════════════════════════════════════════════
# 8 · 领域按需挂载（schema 裁剪）
# ══════════════════════════════════════════════════════════════════
_DOMAINS = {                                    # 合成的领域分布（形状与真注册表同）
    "core": ["read_file", "write_file", "deliver_task", "run_python"],
    "research": ["search_web", "fetch_page"],
    "figure": ["audit_figure", "render_figure"],
    "ppt": ["ppt_create_presentation", "ppt_add_slide", "ppt_manage_text",
            "ppt_save", "ppt_switch_presentation", "audit_deck", "set_slide_notes"],
    "sheet": ["xlsx_read", "xlsx_write"],
    "mail": ["mail_send", "mail_list"],
}


def _mount_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for dom, names in _DOMAINS.items():
        for n in names:
            reg.register(Tool(n, f"{dom} 域工具", {"type": "object", "properties": {}},
                              fn=lambda **k: "", domain=dom))
    return reg


def _mount(contract, reg: ToolRegistry) -> dict:
    dom = {n: (reg.get(n).domain if reg.get(n) else "") for n in reg.names()}
    return L.mounting_decision(contract, list(dom), lambda n: dom.get(n, ""))


def test_mounting() -> None:
    section("8 领域按需挂载（schema 裁剪）")
    reg = _mount_registry()

    deck_c = TaskContract(raw_goal="把这篇稿子做成组会汇报的 PPT", needs_deck=True)
    m_deck = _mount(deck_c, reg)
    check("8.1 要做 deck ⇒ ppt 域被挂上", "ppt" in m_deck["domains"], str(m_deck["domains"]))
    check("8.2 ★ 安全不变量：needs_deck ⇒ audit_deck 必在 allow 里"
          "（门会拦的情形，工具一定已挂上）",
          m_deck["allow"] is None or "audit_deck" in m_deck["allow"], str(m_deck["allow"]))

    write_c = TaskContract(raw_goal="写一篇 1200 字的论文引言", needs_writing=True)
    m_write = _mount(write_c, reg)
    check("8.3 纯写作任务 ⇒ ppt 域**不**挂（37 个工具的 schema 不压在每一场任务上）",
          "ppt" not in m_write["domains"], str(m_write["domains"]))
    if m_write["allow"] is not None:
        check("8.4 且 ppt 工具一个都够不到",
              not any(n in m_write["allow"] for n in _DOMAINS["ppt"]),
              str([n for n in _DOMAINS["ppt"] if n in m_write["allow"]]))
    else:
        check("8.4 且 ppt 工具一个都够不到", False, "allow=None 意味着根本没裁")

    check("8.5 core / figure 常驻（图面门点名要用 audit_figure，裁它风险大于收益）",
          set(L._MOUNT_ALWAYS) <= set(m_write["domains"]), str(m_write["domains"]))
    check("8.6 allow 里的名字都真实存在（不许造出不存在的名字）",
          m_deck["allow"] is None or set(m_deck["allow"]) <= set(reg.names()),
          str(sorted(set(m_deck["allow"] or []) - set(reg.names()))))

    # 真算一次字符数：裁剪到底省没省
    full = reg.openai_schemas(None)
    cut = reg.openai_schemas(m_write["allow"])
    cf = len(json.dumps(full, ensure_ascii=False))
    cc = len(json.dumps(cut, ensure_ascii=False))
    check(f"8.7 裁剪真的省了（全量 {cf} → 写作任务 {cc} 字符）", cc < cf, f"{cf} vs {cc}")
    # deck 任务拿到 = 常驻域（core/research/figure）**加** ppt。
    # 注意它**不等于**全量：`sheet` / `mail` 这些既不在常驻名单里、也不在本次需求里，
    # 照样被裁掉 —— 这正是裁剪的收益来源，不是漏挂。
    expect = sorted(n for d in ("core", "research", "figure", "ppt")
                    for n in _DOMAINS[d])
    check("8.8 deck 任务拿到「常驻域 + ppt」的**每一个**工具（一个都不能少）",
          m_deck["allow"] is not None and sorted(m_deck["allow"]) == expect,
          f"缺={sorted(set(expect) - set(m_deck['allow'] or []))} "
          f"多={sorted(set(m_deck['allow'] or []) - set(expect))}")
    check("8.8b 但 sheet / mail 仍被裁掉（收益来源，不是漏挂）",
          not any(n in (m_deck["allow"] or []) for n in _DOMAINS["sheet"] + _DOMAINS["mail"]),
          str(m_deck["allow"]))

    check("8.9 阶段含 slides 也挂 ppt 域（不止看 needs_deck）",
          "ppt" in L.mounting_decision(
              TaskContract(raw_goal="整理一份汇报材料", stages=["slides"]),
              {}, lambda n: "")["domains"])

    # ★ 静默失败防线：领域名一个都对不上时**退回不裁**，绝不悄悄把工具拿走
    m_none = L.mounting_decision(deck_c, ["a", "b"], lambda n: "")
    check("8.10 ★ 领域名对不上 ⇒ 退回不裁（allow=None），宁可多给也不静默拿走工具",
          m_none["allow"] is None, str(m_none["allow"]))
    check("8.11 且理由里说明「退回不裁」的原因",
          "退回不裁" in m_none["reason"], m_none["reason"])
    check("8.12 拿不到领域信息时不裁（allow=None）",
          L.mounting_decision(deck_c, None, None)["allow"] is None)

    check("8.13 理由字符串任何时候都非空（悄悄发生的裁剪会让人对着「工具调不到」发懵）",
          all(m["reason"] for m in (m_deck, m_write, m_none)))
    check("8.14 不挂 ppt 时理由明说「不挂 ppt 域」",
          "不挂 ppt 域" in m_write["reason"], m_write["reason"])

    # ★ `set_slide_notes` 为什么放 ppt 域而不是 core：**schema 体积是每场任务都要付的**。
    #   放 core ⇒ 所有任务（写作、检索、画图）都多背一段"怎么给 PPT 写备注"的说明；
    #   放 ppt ⇒ 只有要产出 deck 的任务付这笔钱。这条断言就是那个取舍的钉子。
    check("8.15 ★ set_slide_notes 在 ppt 域（只在要 deck 的任务上挂，不占常驻 schema）",
          "set_slide_notes" in _DOMAINS["ppt"] and "set_slide_notes" not in _DOMAINS["core"])
    check("8.16 负控：纯写作任务的 allow 里够不到 set_slide_notes",
          m_write["allow"] is not None and "set_slide_notes" not in m_write["allow"],
          str(m_write["allow"]))


# ══════════════════════════════════════════════════════════════════
# 9 · 承重自检：把守卫的机制拿掉，正控必须当场变红
# ══════════════════════════════════════════════════════════════════
def test_load_bearing() -> None:
    section("9 ★ 承重自检（拿掉机制 ⇒ 正控必须失守）")
    tmp = T.fresh_dir(_TMPROOT / "lb", "deck-lb")

    check("9.0 前置：正控此刻确实是绿的",
          _looks_like_deck("做个组会PPT") and contract_wants_notes("要讲稿"))

    with swapped(C, "_DECK_STRONG", ()):
        check("9.1 清空 _DECK_STRONG ⇒ 正控当场失守（证明关键词表在承重）",
              not _looks_like_deck("做个组会PPT"))
    check("9.2 复原后正控回来", _looks_like_deck("做个组会PPT"))

    with swapped(C, "_NOTES_ASK", ()):
        check("9.3 清空 _NOTES_ASK ⇒ 讲稿判定失守（证明它在承重）",
              not contract_wants_notes("要讲稿"))
    check("9.4 复原后讲稿判定回来", contract_wants_notes("要讲稿"))

    good = make_pptx(tmp / "lb.pptx", green_slides(3),
                     notes=["讲稿一", "讲稿二", "讲稿三"])
    check("9.5 前置：3/3 备注被量到",
          probe_pptx(good).slides_with_notes == 3)
    with swapped(D, "_notes_map", lambda zf: {}):
        check("9.6 把 _notes_map 换成恒空 ⇒ 3/3 那一条当场失守"
              "（证明备注检测器在承重，不是靠 fixture 的巧合）",
              probe_pptx(good).slides_with_notes == 0)
    check("9.7 复原后备注检测回来", probe_pptx(good).slides_with_notes == 3)

    check("9.8 前置：18 pt 正文被量到",
          probe_pptx(make_pptx(tmp / "lb2.pptx", green_slides(2, body_pt=18.0))
                     ).min_body_font_pt == 18.0)
    with swapped(D, "_BODY_TEXT_MIN_CHARS", 10 ** 9):
        check("9.9 把正文体量门槛抬到天上 ⇒ 字号量不到（证明门槛真的参与判定）",
              probe_pptx(tmp / "lb2.pptx").min_body_font_pt is None)

    au = _aud()
    with swapped(D, "HARD_MIN_FONT_PT", 0.0):
        g = au._gate_type(probe_pptx(make_pptx(tmp / "lb3.pptx",
                                               green_slides(6, body_pt=6.0))))
        check("9.10 把可读下限降到 0 ⇒ 6 pt 的 FAIL 正控当场失守",
              not any(f.check_id == "font_too_small" for f in g.findings))
    g = au._gate_type(probe_pptx(tmp / "lb3.pptx"))
    check("9.11 复原后 6 pt 又会被拦", any(f.check_id == "font_too_small" for f in g.findings))

    # 挂载那条不变量也要证它在承重
    reg = _mount_registry()
    deck_c = TaskContract(raw_goal="做个组会 PPT", needs_deck=True)
    check("9.12 前置：needs_deck 时 ppt 域确实挂上",
          "ppt" in _mount(deck_c, reg)["domains"])
    with swapped(L, "_DECK_DOMAIN", "__一个不存在的域__"):
        m = _mount(deck_c, reg)
        check("9.13 ★ 把 ppt 域名的常量改歪 ⇒ audit_deck 立刻够不到"
              "（证明「needs_deck ⇒ audit_deck 在 allow 里」是靠它承重的）",
              m["allow"] is None or "audit_deck" not in m["allow"], str(m["domains"]))

    T.stash(tmp, "deck-lb")


# ══════════════════════════════════════════════════════════════════
# 10 · 上游审计脚本（诚实分支：找不到就说找不到，不冒充通过）
# ══════════════════════════════════════════════════════════════════
def test_upstream_script() -> None:
    section("10 上游审计脚本")
    tmp = T.fresh_dir(_TMPROOT / "up", "deck-up")
    script = find_audit_script("")
    rep = _aud().audit(make_pptx(tmp / "up.pptx", green_slides(6)))

    if script is None:
        xg = _gate(rep, "xml")
        check("10.1 找不到审计脚本 ⇒ xml 门必须 SKIPPED（不得报 PASS）",
              xg.verdict == "SKIPPED", xg.verdict)
        check("10.2 且 reason 指明该配 config.json 的 deck.skills_dir",
              "skills_dir" in (xg.detail + " ".join(rep.notes)), xg.detail[:120])
    else:
        ok = script.is_file() and script.stat().st_size > 1000
        check(f"10.1 审计脚本在位且非空（{script.name}）", ok, str(script))
        xg = _gate(rep, "xml")
        # 合成 pptx 没有母版/版式，脚本可能判出一堆发现 —— 那没关系。
        # 要钉住的是：**要么真出了结论，要么如实 SKIPPED 并给出原因**，
        # 绝不出现"没跑成却报 PASS"（M 型）。
        check("10.2 ★ xml 门要么真出结论、要么如实 SKIPPED 并说明原因"
              "（不许「没跑成却 PASS」）",
              xg.verdict in ("PASS", "REVIEW REQUIRED", "FIX BEFORE DELIVERY")
              or (xg.verdict == "SKIPPED" and bool(xg.detail.strip())),
              f"{xg.verdict} / {xg.detail[:160]}")

    T.stash(tmp, "deck-up")


# ══════════════════════════════════════════════════════════════════
# 11 · 测试不许污染产物目录
# ══════════════════════════════════════════════════════════════════
_STRAY = ("deck_audit.json", "deck_report.json", "deck_report.json.tmp")


def snapshot_stray() -> set[str]:
    found = set()
    for d in (ROOT, ROOT / "figures", ROOT / "evidence", ROOT / "rongke", ROOT / "docs"):
        if not d.is_dir():
            continue
        for n in _STRAY:
            if (d / n).exists():
                found.add(str(d / n))
    return found


def test_no_pollution(before: set[str]) -> None:
    section("11 测试不许污染产物目录")
    after = snapshot_stray()
    added = sorted(after - before)
    check("11.1 没在产物目录/仓库里留下 deck 报告", not added, str(added))
    check("11.2 临时目录在 runs/_regress 下（与 _trash 同盘，收尾是干净的 rename）",
          str(_TMPROOT).startswith(str(ROOT / "runs" / "_regress")), str(_TMPROOT))


# ══════════════════════════════════════════════════════════════════
# 12 · 开工动作：整机跑不出 PPT 的**真根因**
#      （2026-09-21 补。`deck_test` 原本只证"判据对不对"，而真任务里暴露的是
#        "模型从没开始建稿" —— 58 次调用里 create_presentation / add_slide
#        **零次**，`audit_deck` 调了 1 次而且是对着一个 `.docx` 调的。）
# ══════════════════════════════════════════════════════════════════
def test_deck_recipe() -> None:
    section("12 开工动作（整机失败的真根因）")
    # 实测诊断：提示词里 `audit_deck` 出现在 30% 与 77% 两处 ⇒ 模型**执行了**它；
    # 而 `create_presentation` / `add_slide` 只以一行 ✅ 旁注藏在 39–52% 的
    # 「组会汇报纪律」段落**中间**（那段通篇在讲核验）⇒ **零次**。
    # ⇒ 修法是三条，这里逐条钉住：
    #     ① 配方**出现在**提示词里；② 它**靠前**且以**动作**命名；③ **只有一个来源**。
    c = TaskContract(raw_goal="把材料区那份素材整理成组会汇报用的 PPT",
                     needs_deck=True, stages=["slides"])
    p = c.to_prompt()
    kick = p.find("开工动作")
    disc = p.find("组会汇报纪律")
    plan = p.find("阶段专用技能")
    check("12.1 needs_deck ⇒ 提示词里有「开工动作」块", kick >= 0, str(kick))
    check("12.2 ★ 它排在「组会汇报纪律」**之前**（生产动作不许被核验段落淹没）",
          -1 < kick < disc, f"kick={kick}, disc={disc}")
    check("12.3 ★ 它排在「阶段专用技能」**之后**（先装技能，再动手）",
          -1 < plan < kick, f"plan={plan}, kick={kick}")
    check("12.4 ★ 块内点名产物是 .pptx、只能由 ppt 域工具建出来",
          ".pptx" in p[kick:kick + 500], p[kick:kick + 260])
    # ★ 命名那条**走错的路**：实测模型正是把 `export_docx` 出的 .docx 当成了产物。
    check("12.5 ★ 明说 .docx / .html **不是**演示文稿（实测就栽在这）",
          "不是**演示文稿" in p, True)
    check("12.6 点名 open_presentation 喂错文件的报错原文（让模型能自纠）",
          "is not a PowerPoint file" in p, True)
    # 逐条核配方：少一步就是残方。**一条断言**（不是循环一条一个）——
    # 循环会让断言数随常量长度变化，而"断言数必须每次一样"是基线纪律。
    _miss = [s for s in C.DECK_TOOLCHAIN_RECIPE if s not in p]
    check("12.7 ★ 配方**逐条**都在提示词里", not _miss, str(_miss))
    # ── ★ 单源：另一个渠道（SYSTEM）必须来自同一个常量 ──
    _rendered = C.deck_recipe_lines(indent="    ")
    check("12.8 ★★ SYSTEM 里嵌的**正是常量渲染出来的那一段**（不是手抄的）",
          _rendered in L.SYSTEM, L.SYSTEM[:120])
    check("12.9 ★ SYSTEM 里也点名 .docx 那条错路（模型读 system 最勤）",
          "is not a PowerPoint file" in L.SYSTEM, True)
    # ── ★★ 承重自检：扰动常量 ⇒ 契约侧必须**当场跟着变** ──
    #    这是"一处定义、两处引用"这句话唯一的实证方式：
    #    若契约里其实是抄了一份，这条自检就会失守。
    _orig, _S = C.DECK_TOOLCHAIN_RECIPE, "ZZ_SENTINEL_STEP_ZZ"
    try:
        C.DECK_TOOLCHAIN_RECIPE = tuple(_orig) + (_S,)
        _p2 = TaskContract(raw_goal="做个组会 PPT", needs_deck=True).to_prompt()
        check("12.10 ★★ 承重自检：改常量 ⇒ 契约提示词跟着变（否则就是抄了一份）",
              _S in _p2, True)
    finally:
        C.DECK_TOOLCHAIN_RECIPE = _orig
    check("12.11 自检自己收干净（恢复后哨兵消失）",
          _S not in TaskContract(raw_goal="做个组会 PPT", needs_deck=True).to_prompt(),
          True)
    # ── 负控：不需要 deck 的任务不许被塞这些（无差别加戏会污染别的任务）──
    _w = TaskContract(raw_goal="写一段产品说明", needs_deck=False).to_prompt()
    check("12.12 负控：needs_deck=False ⇒ 没有「开工动作」块",
          "开工动作" not in _w, True)
    check("12.13 负控：needs_deck=False ⇒ 也没有「组会汇报纪律」",
          "组会汇报纪律" not in _w, True)


# ══════════════════════════════════════════════════════════════════
# 13 · 写讲稿备注：缺的**第二个基元**
#      （2026-09-21 补。缺口①是「读不了输入」→ `read_file`；这是缺口②。）
#
# 实测血债 `runs/20260921-121201`（25 分钟 / 67 次调用）：
#   deck 建好之后，约 **19 次调用（28%）** 全耗在找「怎么写备注」上 ——
#   6 次幻觉 `apply_professional_design(operation="add_speaker_notes")`、
#   4 次 `dispatch_task`（4 个执行器全 `available: false`）、9 次 `load_skill`
#   翻文档；最后 `audit_deck`=0、`deliver_task`=0 —— **整场零交付。**
# 根因不是模型不努力，是**底座没给工具**：技能写的兜底路径「另跑一小段
# python-pptx」需要一个执行代码的能力，而内核没有。
#
# 本节钉两件事：
#   ① 新工具的**拒绝**与**错误通道**（成功路径在 `runs/_regress/_probe_notes.py`，
#      那边要一份真的由 MCP 工具建出来的 deck，本文件坚持纯标准库夹具）；
#   ② H 型防线：**所有**告诉模型「怎么补备注」的地方必须指向**同一个**工具 ——
#      上一版正是在这里裂开的（技能说「另跑 python-pptx」，工具却叫别的名字，
#      模型在两条之间反复撞）。
# ══════════════════════════════════════════════════════════════════
# ── §13.I 夹具：notesSlide **缺 body 占位符** 的那一页 ────────────
# 2026-09-28 W13-C/D 实测：真模板 58 个 notesSlide 里恰好有 1 个零占位符
# （`notesSlide3.xml`，挂在页型「章节页-1」上）。`notes_slide.notes_text_frame`
# 对这种页返回 **None**（python-pptx 的正常返回），内核原来直接 `.text`
# ⇒ AttributeError ⇒ 一个循环一次 save ⇒ **一页坏、全篇 0 写入**。
# 下面三个函数就是把那个形状**在临时目录里复刻出来**，好让回归不必抱着
# 58 MB 的学校模板跑（模板本体刻意不进仓库，见 config.json 的 deck.template）。
_BODY_SP_RE = re.compile(r"<p:sp\b.*?</p:sp>", re.S)


def _drop_body_sp(xml: str) -> str:
    """删掉这段 notes XML 里带 `type="body"` 的**整个** `<p:sp>`。"""
    return _BODY_SP_RE.sub(
        lambda m: "" if 'type="body"' in m.group(0) else m.group(0), xml)


def _make_notes_deck(path: Path, py: str, pages: int) -> bool:
    """python-pptx 造一份**每页都已挂 notesSlide（含 body 占位符）**的真 deck。

    为什么要先让它长出占位符再删：`write_slide_notes` 的防护是"缺了就补"，
    夹具必须**精确地只缺那一个东西**。空壳 notesSlide 和"根本没有 notesSlide
    部分"是两回事 —— 后者 python-pptx 会自己新建（自带占位符），测不到这条。
    """
    if not py:
        return False
    code = (
        "import sys\n"
        "from pptx import Presentation\n"
        "prs = Presentation()\n"
        "for i in range(int(sys.argv[2])):\n"
        "    prs.slides.add_slide(prs.slide_layouts[1])\n"
        "for i, s in enumerate(prs.slides):\n"
        "    s.notes_slide.notes_text_frame.text = '原有备注 %d' % (i + 1)\n"
        "prs.save(sys.argv[1])\n"
    )
    r = subprocess.run([py, "-c", code, str(path), str(pages)],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and Path(path).is_file()


def _strip_notes_body_ph(src: Path, dst: Path, page: int,
                         also_master: bool = False) -> bool:
    """把第 `page` 页的 body 占位符删掉，写到 `dst`。返回**是否真删掉了**。

    `also_master=True` ⇒ 连 notesMaster 的 body 占位符一起删 ⇒ 那一页
    **补不回来**（正是 §13.I 第二条要的"坏页被点名"输入）。
    ⚠️ 返回 False = 夹具没删到东西 ⇒ 用例测的就不再是这个缺陷了，必须说出来。
    """
    with zipfile.ZipFile(src) as z:
        rel = z.read("ppt/slides/_rels/slide%d.xml.rels" % page).decode("utf-8")
        m = re.search(r'Target="[^"]*(notesSlide\d+\.xml)"', rel)
        if not m:
            return False
        part = "ppt/notesSlides/" + m.group(1)
        items = {n: z.read(n) for n in z.namelist()}
    hit = False
    new = _drop_body_sp(items[part].decode("utf-8"))
    if new != items[part].decode("utf-8"):
        items[part] = new.encode("utf-8")
        hit = True
    if also_master:
        for n in list(items):
            if re.fullmatch(r"ppt/notesMasters/notesMaster\d+\.xml", n):
                x2 = items[n].decode("utf-8")
                n2 = _drop_body_sp(x2)
                if n2 != x2:
                    items[n] = n2.encode("utf-8")
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in items.items():
            z.writestr(n, b)
    return hit


def _slide_blobs(pptx: Path) -> dict:
    """`ppt/slides/slideN.xml` 的**内容**（同构基线的口径：产物页 = 模板页）。

    刻意不含 `ppt/slides/_rels/` —— 给一个原本没有备注部分的页补上 notesSlide，
    必然要在该页的 rels 里加一条关系，那是**备注侧**的正当改动，
    动的是"这一页有没有讲稿"，不是"这一页长什么样"。
    """
    with zipfile.ZipFile(pptx) as z:
        return {n: z.read(n) for n in z.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)}


def test_write_notes() -> None:
    section("13 写讲稿备注（缺的第二个基元）")
    tmp = T.fresh_dir(_TMPROOT / "write", "deck-write")
    make_pptx(tmp / "d.pptx", green_slides(3))
    ok3 = ["第一页讲稿", "第二页讲稿", "第三页讲稿"]

    # ── 13.A 参数形状：最便宜的地方先拦 ──────────────────────
    r = D.write_slide_notes("d.pptx", "一整段话", workdir=tmp)
    check("13.1 传字符串（不是列表）⇒ 拒绝", not r.ok, r.format_for_model()[:160])
    check("13.2 且说清要**逐页**给", any("逐页" in p for p in r.problems),
          str(r.problems)[:160])
    check("13.3 传 dict ⇒ 也拒绝",
          not D.write_slide_notes("d.pptx", {"1": "x"}, workdir=tmp).ok)
    check("13.4 空列表 ⇒ 拒绝（0 段 ≠ 3 页）",
          not D.write_slide_notes("d.pptx", [], workdir=tmp).ok)

    # ── 13.B 页数一一对应：负控 + 正控（同一道闸的两面）──────
    #    负控：段数不匹配 ⇒ 必须拒绝，且**点名真实页数**
    r2 = D.write_slide_notes("d.pptx", ok3[:2], workdir=tmp)
    check("13.5 ★ 2 段配 3 页 ⇒ 拒绝（不替模型猜哪段配哪页）", not r2.ok)
    check("13.6 ★ 拒绝理由里点名真实页数「3 页」",
          any("3 页" in p for p in r2.problems), str(r2.problems)[:200])
    check("13.7 且说明「不会替你猜」",
          any("猜" in p for p in r2.problems), str(r2.problems)[:200])
    #    正控：段数**匹配**时必须能过这道闸 —— 否则它就成了一道永远拦的墙
    #    （G 型的反面：只会拦的守卫和只会放行的守卫一样没用）。
    #    这里把解释器换成一根不存在的路径，让失败**必然、可预期**地发生在
    #    更后面一步：报错是「起不了子进程」，而不是「段数与页数不等」。
    with swapped(D, "find_notes_python", lambda *a, **k: "C:/___没有这个解释器___.exe"):
        r3 = D.write_slide_notes("d.pptx", ok3, workdir=tmp)
    check("13.8 ★ 正控：3 段配 3 页 ⇒ **过得了**页数闸（报错换成了别的）",
          not r3.ok and not any("段数与页数不等" in p for p in r3.problems)
          and any("起不了子进程" in p for p in r3.problems), str(r3.problems)[:200])

    # ── 13.C 能力不可用时**如实说**，不把模型推去乱撞 ────────
    with swapped(D, "find_notes_python", lambda *a, **k: None):
        r4 = D.write_slide_notes("d.pptx", ok3, workdir=tmp)
    check("13.9 装了不了 python-pptx ⇒ 拒绝", not r4.ok)
    check("13.10 ★ 措辞是「写备注的能力不可用」（能力缺失 ≠ 写了 0 页）",
          any("能力不可用" in p for p in r4.problems), str(r4.problems)[:200])
    check("13.11 ★ 并明说**不要反复重试**（否则就是让模型拿步数去撞墙）",
          any("不要反复重试" in p for p in r4.problems), str(r4.problems)[:200])
    check("13.12 而且点名试过哪个解释器（可自查，不靠猜）",
          any("deck.python" in p for p in r4.problems), str(r4.problems)[:200])

    # ── 13.D 路径围栏（工具自己关；注册表那道只看绝对路径）──
    esc = D.write_slide_notes("../../逃出去.pptx", ok3, workdir=tmp)
    check("13.13 ★ `../` 越界 ⇒ 拒绝（相对路径注册表围栏是放行的，必须自己关）",
          not esc.ok, str(esc.problems)[:200])
    check("13.14 越界时**一个文件都没产出**",
          not (tmp.parents[1] / "逃出去.pptx").exists())
    miss = D.write_slide_notes("根本没有.pptx", ok3, workdir=tmp)
    check("13.15 文件不存在 ⇒ 拒绝", not miss.ok)
    check("13.16 ★ 且列出工作区里**真实有的**名字（照抄就能改对）",
          any("d.pptx" in p for p in miss.problems), str(miss.problems)[:200])

    # ── 13.E 不是 pptx ──────────────────────────────────────
    (tmp / "素材.md").write_text("# markdown\n不是 deck\n", encoding="utf-8")
    check("13.17 拿 .md 当 deck ⇒ 拒绝（说它不是能改的 .pptx）",
          (not D.write_slide_notes("素材.md", ok3, workdir=tmp).ok))
    (tmp / "坏.pptx").write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    rbad = D.write_slide_notes("坏.pptx", ok3, workdir=tmp)
    check("13.18 坏 zip ⇒ 拒绝返回（不抛异常、不带崩调用方）", not rbad.ok)

    # ── 13.F 真注册表上的真工具：域 / 围栏 / 错误通道 ────────
    #    用 `install_mcp_servers=False` 造内核（不起 MCP 子进程，秒级），
    #    拿到的就是**真**注册对象：真 domain、真 mutating、真 describe。
    from rongke.loop import Rongke
    rk = Rongke(install_domain=False, use_memory=False, install_mcp_servers=False)
    rk_deck_on = getattr(getattr(rk.cfg, "deck", None), "enabled", True) is not False
    T_ = rk.registry.get("set_slide_notes")
    check("13.19 真注册表里注册出了 set_slide_notes（跟着 deck.enabled 走）",
          (T_ is not None) == rk_deck_on, f"tool={T_}")
    check("13.20 ★ 它的 domain 是 ppt（⇒ 只在要 deck 的任务上挂）",
          getattr(T_, "domain", "") == "ppt", getattr(T_, "domain", ""))
    check("13.21 ★ 它标成 mutating（真的改盘上的 .pptx ⇒ 受工作区围栏管）",
          bool(getattr(T_, "mutating", False)) is True)
    _d = getattr(T_, "description", "") or ""
    check("13.22 工具描述里点明：上游没有这个能力 / 页数要一一对应 / 别自己写代码",
          "没有写备注的能力" in _d and "等于页数" in _d and "自己写代码" in _d,
          _d[:200])
    # ★ 错误通道：拒绝必须走 `ok=False`，不许渲染成「成功文本里的一条警告」——
    #   那正是 G 型的制造法（模型会以为可以往下走了）。
    c_bad = rk.registry.call("set_slide_notes", {"pptx": "d.pptx", "notes": "一整段"})
    check("13.23 ★★ 工具层：参数不对 ⇒ ok=False（不是 ok=True + 一条警告）",
          c_bad.ok is False, f"ok={c_bad.ok}")
    check("13.24 且 error 里带着给人看的报告，说清该怎么给参数",
          "逐页" in (c_bad.error or ""), (c_bad.error or "")[:200])

    # ── 13.G H 型防线：提到「怎么补备注」的地方必须指向同一个工具 ──
    #    这是本节的**核心守卫**。上一版是这样裂开的：
    #      · 技能文档说「另跑一小段 python-pptx」（模型做不到，它没有执行能力）
    #      · 内核其实没有那个工具，而新工具叫 `set_slide_notes`（模型不知道）
    #    ⇒ 模型在两条说法之间反复撞，烧掉 19 次调用。
    #    「同一句话抄两遍」的修法不是"记得两边都改"，是让守卫盯着它们。
    c_notes = TaskContract(raw_goal="把材料区那份素材做成组会 PPT，每页都要有讲稿备注",
                           needs_deck=True)
    surfaces = {"SYSTEM": L.SYSTEM, "契约提示词": c_notes.to_prompt()}
    for _name, _text in surfaces.items():
        check(f"13.25/{_name} 点名了 `set_slide_notes`（模型得有路可走）",
              "set_slide_notes" in _text, _text[:120])
        check(f"13.26/{_name} 里**没有**「另跑 python-pptx」这条做不到的路",
              "另跑" not in _text, "还留着旧说法")
    _rec = list(C.DECK_TOOLCHAIN_RECIPE)
    _i_set = next((i for i, s in enumerate(_rec) if "set_slide_notes" in s), -1)
    _i_aud = next((i for i, s in enumerate(_rec) if "audit_deck" in s), -1)
    check("13.27 配方里 set_slide_notes 排在 audit_deck **之前**"
          "（先补备注再体检，否则体检永远先报不合格）",
          -1 < _i_set < _i_aud, f"set={_i_set} audit={_i_aud}")
    _gf = _aud()._gate_notes(probe_pptx(make_pptx(tmp / "z.pptx", green_slides(3))))
    check("13.28 notes 门的告警也点名了同一个工具（不是「另跑 python-pptx」）",
          any("set_slide_notes" in f.message for f in _gf.findings),
          str([f.message[:70] for f in _gf.findings]))

    # ── 13.H 报告文字：下一步必须是可执行的 ─────────────────
    check("13.29 失败报告里写「别重复同样的调用」（重复调用不会有不同结果）",
          "别重复同样的调用" in r2.format_for_model())
    _okrep = D.NotesWrite(ok=True, pptx="deck.pptx", slides=13, wrote=13,
                          notes_ratio=1.0, bytes_before=1, bytes_after=2)
    check("13.30 ★ 成功报告里要求**重跑 audit_deck**（改过 deck，旧报告就作废了）",
          "重跑" in _okrep.format_for_model()
          and "audit_deck" in _okrep.format_for_model())

    # ── 13.I W13-D：notesSlide 缺 body 占位符的两面 ────────────
    #    缺陷本体（2026-09-28 实测，runs/20260928-120048）：
    #      `notes_slide.notes_text_frame` 对这种页返回 None（python-pptx 的
    #      **正常返回**），内核拿到就 `.text` ⇒ AttributeError ⇒ 整份 0 写入。
    #    这里钉两面（规矩 1：只会放行和只会拦的都一样没用）：
    #      ① 补得回来 ⇒ 必须真补上、讲稿必须落盘、且不许动 slide 本体；
    #      ② 补不回来 ⇒ 坏页必须被点名，好页不许连累（原来是全篇 0）。
    tmp2 = T.fresh_dir(_TMPROOT / "write-ph", "deck-write-ph")
    py = _py_pptx()
    PAGES, BAD = 4, 2
    BODY_PH = 'type="body"'
    _src = tmp2 / "src.pptx"
    check("13.31 前置：造得出带备注占位符的真 deck（造不出来下面全是空跑）",
          _make_notes_deck(_src, py, PAGES), f"py={py}")
    _fx = tmp2 / "fx.pptx"
    check("13.32 ★ 夹具**真的删掉了** body 占位符（删不到 = 测的不是这个缺陷）",
          _strip_notes_body_ph(_src, _fx, BAD),
          "第 %d 页的 notesSlide 里根本没有 body 占位符可删" % BAD)
    with zipfile.ZipFile(_fx) as z:
        _rel = z.read("ppt/slides/_rels/slide%d.xml.rels" % BAD).decode("utf-8")
        _fx_part = "ppt/notesSlides/" + re.search(
            r'Target="[^"]*(notesSlide\d+\.xml)"', _rel).group(1)
        _fx_xml = z.read(_fx_part).decode("utf-8")
    check("13.33 夹具那一页的 notesSlide 里**没有** body 占位符（复刻 notesSlide3.xml）",
          BODY_PH not in _fx_xml, _fx_part)
    check("13.34 夹具那一页**仍有** notesSlide part（缺占位符 ≠ 缺整个备注页）",
          "<p:spTree" in _fx_xml, _fx_xml[:60])

    notes4 = ["新讲稿第%d页" % i for i in range(1, PAGES + 1)]
    _before = _slide_blobs(_fx)
    r5 = D.write_slide_notes(_fx.name, notes4, workdir=tmp2, python_exe=py)
    d5 = r5.to_dict()
    _back5 = D.read_notes(_fx) or {}
    check("13.35 ★ 缺占位符的页 ⇒ 不再抛异常、整份判成功", r5.ok,
          str(r5.problems)[:200] + str(r5.mismatches)[:200])
    check("13.36 ★ 那一页的讲稿**真写进去了**（不是只其余页成功）",
          _back5.get(BAD, "").strip() == notes4[BAD - 1],
          repr(_back5.get(BAD, ""))[:80])
    check("13.37 全篇覆盖 ⇒ 1.0，且 notes_ratio **仍是数值**（门拿它比大小）",
          isinstance(d5["notes_ratio"], float)
          and abs(d5["notes_ratio"] - 1.0) < 1e-9, repr(d5["notes_ratio"]))
    check("13.38 防护走通了 ⇒ 一个失败页都不该有", d5["failed_slides"] == [],
          str(d5["failed_slides"])[:200])
    _repaired = BODY_PH in zipfile.ZipFile(_fx).read(_fx_part).decode("utf-8")
    check("13.39 ★ 补法：body 占位符落进了**那一个 notesSlide part**", _repaired,
          "写完后该 notesSlide 仍无 body 占位符")
    _after5 = _slide_blobs(_fx)
    check("13.40 ★★ 同构基线不破：ppt/slides/slideN.xml 逐字节未变（只准动备注侧）",
          _after5 == _before,
          "变了 %d 页" % len([k for k in _before if _before[k] != _after5[k]]))
    check("13.41 分片编号原样保住（路线甲的模板基线靠它）",
          d5["parts_preserved"] is True, str(d5["slide_parts_after"]))

    # ② 补不回来：notesMaster 里也没有 body ⇒ 坏页点名、好页不连累
    _fx2 = tmp2 / "fx2.pptx"
    check("13.42 前置：造出连 notesMaster 都补不了的第二份夹具",
          _strip_notes_body_ph(_src, _fx2, BAD + 1, also_master=True))
    r6 = D.write_slide_notes(_fx2.name, notes4, workdir=tmp2, python_exe=py)
    d6 = r6.to_dict()
    _back6 = D.read_notes(_fx2) or {}
    check("13.43 ★ 坏页**被点名**（页码 + 原因），不是一句写备注失败了事",
          [f.get("page") for f in d6["failed_slides"]] == [BAD + 1]
          and "body" in str(d6["failed_slides"]), str(d6["failed_slides"])[:200])
    check("13.44 ★★ 好页不连累：%d 页里写成了 %d 页（原实现在是 0 页）"
          % (PAGES, PAGES - 1),
          d6["wrote"] == PAGES - 1
          and abs(d6["notes_ratio"] - (PAGES - 1) / PAGES) < 1e-9,
          f"wrote={d6['wrote']} ratio={d6['notes_ratio']}")
    check("13.45 有失败页 ⇒ 整份判不通过（不许把坏页粉饰成通过）",
          r6.ok is False and bool(r6.mismatches), str(r6.mismatches)[:160])
    check("13.46 报告里点名了是哪一页、为什么（模型据此能自纠）",
          "写不进去" in r6.format_for_model()
          and "第 %d 页" % (BAD + 1) in r6.format_for_model(),
          r6.format_for_model()[:200])
    check("13.47 ★ 负控的另一面：好页逐页对得上（不是全丢但计数好看）",
          all(_back6.get(i, "").strip() == notes4[i - 1]
              for i in range(1, PAGES + 1) if i != BAD + 1),
          str({i: _back6.get(i, "") for i in range(1, PAGES + 1)})[:200])

    # ③ 真模板血脉：莫干山 demo 的第 3 页就是「章节页-1」，
    #    其 notesSlide3.xml 是盘上原生的零占位符（不是我们造的）。
    _demo = ROOT / "docs" / "deck-templates" / "moganshan" / "moganshan-demo.pptx"
    _dst = tmp2 / "moganshan-demo.pptx"
    if _demo.is_file() and py:
        shutil.copyfile(_demo, _dst)
        _dn = len(_slide_blobs(_dst))
        _dbefore = _slide_blobs(_dst)
        r7 = D.write_slide_notes(_dst.name, ["章节页讲稿 %d" % i
                                             for i in range(1, _dn + 1)],
                                 workdir=tmp2, python_exe=py)
        d7 = r7.to_dict()
        check("13.48 ★★ 真模板「章节页-1」那份 deck：讲稿覆盖率 1.0"
              "（W13-C 卡住的正是它，实测 0.0）",
              r7.ok and abs(d7["notes_ratio"] - 1.0) < 1e-9
              and d7["failed_slides"] == [],
              str(r7.problems)[:160] + str(r7.mismatches)[:160]
              + f" ratio={d7['notes_ratio']}")
        check("13.49 真模板那份的 notesSlide3.xml 里确实被补上了 body 占位符",
              BODY_PH in zipfile.ZipFile(_dst).read(
                  "ppt/notesSlides/notesSlide3.xml").decode("utf-8"))
        check("13.50 ★ 真模板用例同样没动 slide 本体（%d 页逐字节一致）" % _dn,
              _slide_blobs(_dst) == _dbefore)
    else:
        check("13.48 真模板「章节页-1」覆盖率 1.0", False,
              "前置缺失：没有 %s 或没有装 python-pptx 的解释器" % _demo)
        check("13.49 真模板 notesSlide3 补上了占位符", False, "同上")
        check("13.50 真模板用例未动 slide 本体", False, "同上")


# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
# §18 视觉判据 + style_deck（2026-09-21）
# ══════════════════════════════════════════════════════════════════
FOOT_NAMES = ("juanqing-footer-line", "juanqing-footer-page")


def _count_footers(pptx: Path) -> int:
    """数这份 pptx 里带**自己命名**的页脚形状（纯标准库，两个解释器都能跑）。

    `style_deck` 画页脚时会给形状起名（`juanqing-footer-*`），这里的计数
    就是"幂等"那条断言的量具：连套两次，页脚形状数**不许增加**。
    """
    n = 0
    with zipfile.ZipFile(pptx) as z:
        for name in z.namelist():
            if not re.match(r"ppt/slides/slide\d+\.xml$", name):
                continue
            xml = z.read(name).decode("utf-8", "replace")
            for fn in FOOT_NAMES:
                n += xml.count(f'name="{fn}"')
    return n


def test_deck_visual() -> None:
    """用户看完一份**结构全绿**的 deck 只说了一句「那能做的这么丑」。

    这一节钉住三件事，缺任何一件，"丑"都会重新变回"合格"：
      V1 **正控** —— 4:3 ＋ 读不到主题 ＋ 零显式字体 ＋ 单一版式 ⇒ 必须被拦
      V2 **负控** —— 套过 `style_deck` 之后 ⇒ 视觉 FAIL 必须消失
      V3 **承重自检** —— 把 `style_deck` 的脚本打成**空转**（回 success 但不动
         文件）⇒ 它自己的复测必须抓住。**这条直接钉死本轮的根因**：
         上游 `apply_professional_design` 正是"自称成功、文件没动"。
    """
    section("18 视觉判据与 style_deck")
    tmp = T.fresh_dir(_TMPROOT / "visual", "deck-visual")

    # ── 18.A 正控：造一份"起手空白模板"样的 deck ───────────────
    ugly = make_pptx(tmp / "ugly.pptx", green_slides(6),
                     sld_sz=(9144000, 6858000), layout="slideLayout1.xml")
    f = probe_pptx(ugly)
    check("18.1 量得到：4:3 ⇒ is_widescreen 为 False",
          f.is_widescreen is False, f"aspect={f.aspect}")
    check("18.2 量得到：读不到主题 ⇒ theme_name() 为空（不编一个）",
          f.theme_name() == "", repr(f.theme_name()))
    check("18.3 量得到：一个显式字体的 run 都没有", f.fonts_set_ratio == 0.0,
          str(f.fonts_set_ratio))
    check("18.4 量得到：最常用版式占比 1.0（6 页同一个版式）",
          abs(f.layout_top_share - 1.0) < 1e-9, str(f.layout_top_share))

    rep = _aud().audit(ugly, workdir=tmp)
    tg = _gate(rep, "type")
    fails = [x.check_id for x in tg.findings if x.level == "FAIL"]
    check("18.5 ★ 正控：4:3 必须判 FAIL（aspect_not_16_9）",
          "aspect_not_16_9" in fails, f"FAIL={fails}")
    check("18.6 ★ 负控：合成夹具**读不到**主题（theme=''）⇒ **不**判 look_is_default",
          "look_is_default" not in fails, f"FAIL={fails}")
    check("18.6b ★ 理由：「读不到」≠「没打扮」—— 真实 .pptx **必然**带 theme1.xml，"
          "读到空只可能是极简夹具，那是「没查」。真文件上的正控在 18.14c / 19.4",
          f.theme_name() == "" and f.fonts_set_ratio == 0.0,
          f"theme={f.theme_name()!r} fonts={f.fonts_set_ratio}")
    check("18.6c ★ 但这一页仍有别的账要算：通篇一种版式 ⇒ layout_monotone 该响",
          "layout_monotone" in [x.check_id for x in tg.findings if x.level == "WARN"],
          str([x.check_id for x in tg.findings if x.level == "WARN"]))
    check("18.7 ★ 正控：报告整体不通过 —— 丑 deck 连交付都交不出来",
          rep.to_dict().get("ok") is False, str(rep.verdict))
    check("18.8 ★ 且拦的理由进了 blockers（第 9 条门读的就是它）",
          any(("16:9" in b or "4:3" in b)
              for b in (rep.to_dict().get("blockers") or [])),
          str(rep.to_dict().get("blockers"))[:160])
    check("18.9 报告里留了一节可复算的视觉数字（extra.visual）",
          isinstance((tg.extra or {}).get("visual"), dict),
          str((tg.extra or {}).get("visual"))[:160])

    # ── 18.B 能力不可用时如实说，不把模型推去乱撞 ───────────────
    with swapped(D, "find_notes_python", lambda *a, **k: None):
        r0 = _style("ugly.pptx", workdir=tmp)
    check("18.10 找不到装了 python-pptx 的解释器 ⇒ 拒绝", not r0.ok)
    check("18.11 ★ 措辞是「套主题的能力不可用」（能力缺失 ≠ 改了 0 页）",
          any("能力不可用" in p for p in r0.problems), str(r0.problems)[:200])

    # ── 18.C V3 承重自检：空转的 style_deck 必须被自己的复测抓住 ──
    #   ⚠️ 这是本轮真正的根因形状：上游那个工具**不抛异常**，只回一个满分自述
    #      `{"success": true, "slides_affected": 13}`，而文件一个字节都没变。
    #      ⇒ "脚本自述"永远不能当判据，判据只能来自**读回来的结果**。
    noop = ("import json,sys; "
            "print(json.dumps({'pres_saved': True, 'zip_rewritten': True}))")
    with swapped(D, "_STYLE_SCRIPT", noop):
        r_noop = _style("ugly.pptx", workdir=tmp)
    check("18.12 ★★ 承重自检：空转脚本（回 success 但不动文件）⇒ 必须 ok=False",
          not r_noop.ok, r_noop.format_for_model()[:220])
    check("18.13 ★★ 且报的是**复测结果**（不是「脚本自报 success 就算数」）",
          any("复测不过" in p for p in r_noop.problems),
          str(r_noop.problems)[:220])

    # ── 18.D V2 负控：真跑一次 ⇒ 视觉 FAIL 必须消失 ─────────────
    if _py_pptx() is None:
        # 按项目纪律：**"没查"不许渲染成"通过"**，所以这里判红并把原因写清楚。
        check("18.14 真跑 style_deck（需要装了 python-pptx 的解释器）", False,
              "本机找不到解释器 ⇒ 这一条**没查**，不是通过")
        return
    # 用**真** .pptx 做负控：合成的极简 zip python-pptx 打不开（见 make_real_pptx），
    # 而 `style_deck` 要动的正是真文件 —— 喂夹具等于没测能力。
    real = tmp / "real_ugly.pptx"
    check("18.14a 前置：能用 python-pptx 造出一份真 .pptx",
          make_real_pptx(real, _py_pptx(), 6), str(real))
    fr = probe_pptx(real)
    check("18.14b ★ 真文件上：默认模板**就是** 4:3（起手即丑，不是我们造的）",
          fr.is_widescreen is False, f"aspect={fr.aspect}")
    fr_fails = [x.check_id
                for x in _gate(_aud().audit(real, workdir=tmp), "type").findings
                if x.level == "FAIL"]
    check("18.14c ★ 真文件上：4:3 ＋ 纯默认外观 ⇒ 两道 FAIL 都要响",
          "aspect_not_16_9" in fr_fails and "look_is_default" in fr_fails,
          f"FAIL={fr_fails}")

    r1 = _style("real_ugly.pptx", workdir=tmp)
    check("18.15 ★ 负控：套主题成功（ok=True）", r1.ok, r1.format_for_model()[:220])
    check("18.16 ★ 负控：复测读回 16:9",
          abs((r1.aspect_after or 0) - 16 / 9) < 0.02, str(r1.aspect_after))
    check("18.17 ★ 负控：复测读回主题名 juanqing（不是脚本自报）",
          r1.theme_after == "juanqing", r1.theme_after)
    check("18.18 真改了盘：字节数变了（空转的话这里就该相等）",
          r1.bytes_after != r1.bytes_before,
          f"{r1.bytes_before} → {r1.bytes_after}")

    # ── 18.D2 幂等：连套两次不许叠出两层页脚 ─────────────────────
    #   ⚠️ 这条是实测才想到的：模型重试 / 多轮对话都可能调两次 `style_deck`。
    #   第一版每调一次就无脑 `add_shape` 一条线 ＋ 一个页码框 ⇒ 右下角会叠出
    #   两层数字（而且看不出来，直到有人上台翻页）。**幂等不是优化，是正确性。**
    n1 = _count_footers(Path(r1.pptx))
    r1b = _style("real_ugly.pptx", workdir=tmp)
    n2 = _count_footers(Path(r1b.pptx))
    check("18.18a ★ 幂等：连套两次仍然成功", r1b.ok, r1b.format_for_model()[:200])
    check(f"18.18b ★ 幂等：页脚形状数不增（{n1} → {n2}）", n2 == n1, f"{n1} → {n2}")

    rep2 = _aud().audit(Path(r1.pptx), workdir=tmp)
    tg2 = _gate(rep2, "type")
    fails2 = [x.check_id for x in tg2.findings if x.level == "FAIL"]
    check("18.19 ★ 负控：套过之后 aspect_not_16_9 必须消失",
          "aspect_not_16_9" not in fails2, f"FAIL={fails2}")
    check("18.20 ★ 负控：look_is_default 必须消失",
          "look_is_default" not in fails2, f"FAIL={fails2}")
    check("18.21 ★ 且报告整体转好 —— 这一对正/负控证明判据真的在**承重**",
          rep2.to_dict().get("ok") is True,
          str(rep2.to_dict().get("blockers"))[:160])

    # ── 18.E 不认识的主题名 ⇒ 拒绝（不替你猜一个色）─────────────
    r2 = _style("ugly.pptx", theme="高大上", workdir=tmp)
    check("18.22 主题名不认识 ⇒ 拒绝，并列出可选值",
          (not r2.ok) and any("不认识的主题" in p for p in r2.problems),
          str(r2.problems)[:200])

    # ── 18.F 围栏：与写备注同一道关（注册表那道只看绝对路径）────
    r3 = _style("../../逃出去.pptx", workdir=tmp)
    check("18.23 `../` 越界 ⇒ 拒绝", not r3.ok, str(r3.problems)[:200])

    # ── 18.G 真注册表：工具挂上了、域对、标了 mutating ───────────
    from rongke.loop import Rongke
    rk = Rongke(install_domain=False, use_memory=False, install_mcp_servers=False)
    T_ = rk.registry.get("style_deck")
    check("18.24 真注册表里注册出了 style_deck", T_ is not None, f"tool={T_}")
    check("18.25 ★ 它的 domain 是 ppt（⇒ 只在要 deck 的任务上挂，不白付 schema）",
          getattr(T_, "domain", "") == "ppt", getattr(T_, "domain", ""))
    check("18.26 ★ 标成 mutating（真改盘上的 .pptx ⇒ 受工作区围栏管）",
          bool(getattr(T_, "mutating", False)) is True)
    _d = getattr(T_, "description", "") or ""
    check("18.27 ★ 描述里点名那个空壳工具，免得模型又去撞"
          "（假成功是点名的缺陷形状）",
          "空壳" in _d and "apply_professional_design" in _d, _d[:220])
    check("18.28 且写明「套完别 save_presentation」（一存盘主题就没了）",
          "save_presentation" in _d, _d[:220])

    # ── 18.H 配方真的把这一步写进了**生产动作序列** ─────────────
    #   为什么单列一条：上一轮实测教训是**模型执行的是"它读到的动作"**，
    #   而不是"我们以为它知道的动作"（`runs/20260921-113729` 把 .docx 当产物）。
    recipe = "\n".join(C.DECK_TOOLCHAIN_RECIPE)
    check("18.29 ★ 契约配方里有 style_deck 这一步（否则模型读不到这个动作）",
          "style_deck" in recipe, recipe[:200])
    i_notes = next((i for i, s in enumerate(C.DECK_TOOLCHAIN_RECIPE)
                    if "set_slide_notes" in s), -1)
    i_style = next((i for i, s in enumerate(C.DECK_TOOLCHAIN_RECIPE)
                    if "style_deck" in s), -1)
    i_audit = next((i for i, s in enumerate(C.DECK_TOOLCHAIN_RECIPE)
                    if "audit_deck" in s), -1)
    check("18.30 ★ 顺序对：先写备注 → 再套主题 → 最后体检（体检必须在最后）",
          -1 < i_notes < i_style < i_audit, f"notes={i_notes} style={i_style} audit={i_audit}")


def test_layout_judgement() -> None:
    """§19 「版式单调」判据的修正 —— 一条**永远亮着**的警告等于没有警告。

    背景（2026-09-21）：上一版判据是 `layout_top_share > 0.7`。实测一份 13 页
    组会 PPT 的真实分布是「封面 1 ＋ 内容 10 ＋ 图表 2」（正文页占 **77%**），
    这是**合理的版式分工**，却被永久判 WARN ⇒ 门钉死在 REVIEW REQUIRED、
    **永远到不了 PASS**。同一份 deck 还说明 `look_is_default` 的第三连
    （同为版式占比）是噪音项 —— 它丑在 `4:3 ＋ Office 主题 ＋ 零显式字体`。

    这一节钉四件事，少一件都不算改对：
      ① **正控**：通篇一种版式 ⇒ `layout_monotone` **还在**（判据没被改没）
      ② **负控**：有分工且占比超旧门槛 ⇒ 它必须**消失**
      ③ ★ **守卫**：删掉第三连后**覆盖面不许下降** ——
         "有分工、但非已知主题 ＋ 零字体"必须**照样 FAIL**（那就是被吐槽那份的形状）
      ④ ★ **闭环**：有分工 ＋ 套过主题 ⇒ type 门必须能到 **PASS**（以前永远到不了）

    ⚠️ 全程用 `make_real_pptx`（真 python-pptx 文件），不用合成夹具：
    只有真文件才带"**非已知**主题"（Office 默认主题），而 ③ 正需要那个输入。
    """
    section("19 版式判据的修正（误报治理）")
    tmp = _TMPROOT / "layout19"
    tmp.mkdir(parents=True, exist_ok=True)
    py = _py_pptx()
    if py is None:
        check("19.0 前置：拿得到 python-pptx 解释器（否则本节的负控没法造真文件）",
              False, "find_notes_python() 返回 None")
        return

    # ── 19.A 先证明「有分工」的输入**真造得出来** ────────────────
    #   这条判据上一版翻车的直接原因就是这一侧造不出来（`make_pptx` 原先
    #   只能给每页同一个 layout）⇒ 它只被单向测过，误报一路漏过去。
    div = make_pptx(tmp / "divided.pptx", green_slides(8),
                    layout=(["slideLayout1.xml"] + ["slideLayout2.xml"] * 6
                            + ["slideLayout3.xml"]))
    fd = D.probe_pptx(div)
    check("19.1 夹具能造「有分工」的 deck：kinds=3（逐页 layout 真生效）",
          fd.layout_kinds == 3, f"kinds={fd.layout_kinds}")
    check("19.2 ★ 且它正落在**旧判据的误报区**：最常见一种占 75%（> 0.7 门槛）",
          fd.layout_top_share > 0.7, f"share={fd.layout_top_share:.2f}")
    check("19.2b 量不到版式时 kinds=0（那叫「没查」，不叫「只有一种」）",
          D.probe_pptx(make_pptx(tmp / "nolay.pptx", green_slides(6))
                       ).layout_kinds == 0)

    # ── 19.B ③ 守卫：有分工 ≠ 免疫「没打扮」 ───────────────────
    #   这是**删掉第三连之后覆盖面有没有下降**的判别力正控。
    #   若哪天有人把 `look_is_default` 也一起放宽了，这条会立刻转红。
    div_real = tmp / "real_divided.pptx"
    check("19.3 前置：真文件造好了（8 页：封面1 ＋ 内容6 ＋ 图表1）",
          make_real_pptx(div_real, py, slides=8,
                         layout_idx=[0, 1, 1, 1, 1, 1, 1, 5],
                         titles=GOOD_TITLES[:8]),
          "make_real_pptx 失败")
    fpre = [x.check_id for x in _gate(_aud().audit(div_real, workdir=tmp),
                                      "type").findings if x.level == "FAIL"]
    check("19.4 ★ 守卫：非已知主题（Office）＋ 零显式字体 ⇒ look_is_default 仍必须响",
          "look_is_default" in fpre, f"FAIL={fpre}")

    # ── 19.C/D 套主题之后：误报消失，且能走到 PASS ───────────────
    st = _style(div_real.name, workdir=tmp)
    check("19.5 按配方套主题成功", st.ok, st.format_for_model()[:200])
    tg = _gate(_aud().audit(div_real, workdir=tmp), "type")
    fpost = [x.check_id for x in tg.findings if x.level == "FAIL"]
    wpost = [x.check_id for x in tg.findings if x.level == "WARN"]
    check("19.6 ★ 负控：有分工 ⇒ layout_monotone 必须消失（旧判据在这里必报）",
          "layout_monotone" not in wpost, f"WARN={wpost}")
    check("19.7 ★ 负控：look_is_default 也消失了（主题已认得）",
          "look_is_default" not in fpost, f"FAIL={fpost}")
    check("19.8 ★★ 闭环：有分工 ＋ 套过主题 ⇒ type 门**无 FAIL 无 WARN**（= 能到 PASS）",
          not fpost and not wpost, f"FAIL={fpost} WARN={wpost}")
    check("19.9 ★ 这条以前永远做不到：旧判据下它必然是 REVIEW REQUIRED",
          tg.verdict == "PASS", tg.verdict)

    # ── 19.E ① 正控：判据本身没被改没 ──────────────────────────
    #   必须**先套主题**才测得到 —— 因为 `layout_monotone` 只在
    #   「非默认外观」那个分支里判（默认外观由 look_is_default 接管）。
    mono_real = tmp / "real_mono.pptx"
    ok_mono = make_real_pptx(mono_real, py, slides=6)      # 默认：全用 layout 1
    _style(mono_real.name, workdir=tmp) if ok_mono else None
    wmono = [x.check_id for x in _gate(_aud().audit(mono_real, workdir=tmp),
                                       "type").findings if x.level == "WARN"]
    check("19.10 ★ 正控：通篇一种版式（且已套主题）⇒ layout_monotone 必须**还在**",
          ok_mono and "layout_monotone" in wmono, f"built={ok_mono} WARN={wmono}")

    # ── 19.F 配图链路：套主题**不丢图**（配图流程整个建在这条假设上）──
    #   「重写整包」是最容易丢 part 的操作 —— 图/表/备注都住在各自的 part 里，
    #   而 `style_deck` 的实现正是"python-pptx 读写 ＋ zip 层替换 theme1.xml"。
    #   这条以前只有"我跑过一次探针看着没事"（一次性证据，随时可以失效），
    #   现在钉成断言：**凡是流程依赖的假设，都必须有测试盯着。**
    img_src = tmp / "src_fig.png"
    img_src.write_bytes(_png_1x1())
    img_deck = tmp / "img_deck.pptx"
    ok_img = make_real_pptx(img_deck, py, slides=4, image=str(img_src))
    fi0 = D.probe_pptx(img_deck)
    check("19.11 前置：造出带图的真 deck（4 页 / 1 张图）",
          ok_img and fi0.pictures == 1, f"built={ok_img} pics={fi0.pictures}")
    st_img = _style(img_deck.name, workdir=tmp)
    fi1 = D.probe_pptx(img_deck)
    check("19.12 ★ 套主题后**图片仍在**（重写整包不许丢 part）",
          st_img.ok and fi1.pictures == fi0.pictures == 1,
          f"ok={st_img.ok} 前={fi0.pictures} 后={fi1.pictures}")
    check("19.13 ★ 且是「换皮不换肉」：画布转了、页数没变",
          fi1.is_widescreen is True and fi1.slide_count == fi0.slide_count,
          f"16:9={fi1.is_widescreen} 页 {fi0.slide_count}→{fi1.slide_count}")


def test_argument_layer() -> None:
    """§20 论证层判据 —— 荣珂在这一层原先有 **0** 条判据。

    依据 `docs/ppt-skill-survey-20260921.md` §3 那张"判据铺到哪一层"的表：
    四家外部实践（PPT Master / CyberPPT / academic-pptx-skill）在**① 论证层**
    都有可跑的检查，而荣珂一条没有 —— 而这一层的数据**早就躺在
    `facts.slides[*].title_text` 里**，只是没进判据。

    ⚠️ 这一节的**重点不是"判据会不会响"，而是"它会不会在真实产物上误报"**。
    论证层是四层里最容易误伤的一层（**章节页的标题本来就该是名词短语**），
    而我刚在 §19 因为判据过度收紧打红过 5 项既有断言。所以三条各自配了负控，
    外加一条**真实产物守卫**。
    """
    section("20 论证层判据（标题形态 / 末页形态）")
    tmp = _TMPROOT / "argument"
    tmp.mkdir(parents=True, exist_ok=True)

    def warns_of(p) -> list[str]:
        return [x.check_id for x in _gate(_aud().audit(p, workdir=tmp),
                                          "type").findings if x.level == "WARN"]

    # ── 20.A 标题形态：是"一句话结论"还是"名词短语"──────────────
    noun = make_pptx(tmp / "noun.pptx",
                     [{"title": t, "body": _BODY} for t in
                      ["研究背景", "温度的影响", "实验设计",
                       "数据分析", "结果讨论", "小结"]])
    w1 = warns_of(noun)
    check("20.1 ★ 正控：标题全是名词短语 ⇒ titles_are_noun_phrases 必须响",
          "titles_are_noun_phrases" in w1, f"WARN={w1}")

    sent = make_pptx(tmp / "sent.pptx",
                     [{"title": t, "body": _BODY} for t in
                      ["研究背景", "低温必要，尚不充分", "转化率随诱导时长单调上升",
                       "现有证据只到表型层面", "延长诱导的代价：同步度下降",
                       "下一步：收窄阈值，锁定开关基因"]])
    w2 = warns_of(sent)
    check("20.2 ★ 负控：标题多数是结论句 ⇒ **不**响（只有 1/6 是名词短语）",
          "titles_are_noun_phrases" not in w2, f"WARN={w2}")

    # ★★ 误报守卫：把那份**真实成片**的标题原样喂进去。
    #   为什么这条比正控更重要：判据的收益是"防未来退化"，代价却是"今天就开始
    #   误伤真实产物"。§19 已经教过一次 —— 误报会教人**整体无视**体检报告。
    #   ⚠️ 单源：**直接用 `GOOD_TITLES`**（就是 §3.1 / §6.3 / §19.3 正控那批），
    #   不在这里再抄一份 —— 抄两份就是 H 型，改一处必漏另一处。
    real_titles = GOOD_TITLES
    realish = make_pptx(tmp / "real_titles.pptx",
                        [{"title": t, "body": _BODY} for t in real_titles])
    w3 = warns_of(realish)
    check("20.3 ★★ 误报守卫：那份**真实成片**的 13 个标题原样喂进去 ⇒ 必须不响",
          "titles_are_noun_phrases" not in w3, f"WARN={w3}")
    check("20.3b 且它含 `4–15℃` 那种连接号 —— 标点表里刻意不放横线，"
          "否则会被读成「句子」而让整条判据悄悄失效（G 型）",
          "titles_are_noun_phrases" not in w3)
    check("20.3c ★ 单源守卫：正控那批标题与本节守卫**是同一个对象**"
          "（谁把它们抄成两份副本，这条立刻转红）",
          real_titles is GOOD_TITLES and len(GOOD_TITLES) >= 13,
          f"same={real_titles is GOOD_TITLES} n={len(GOOD_TITLES)}")

    # ── 20.B 末页形态：该停在结论，不是「谢谢聆听」／空白 ────────
    head = [{"title": "第 %d 部分，结论是明确的" % i, "body": _BODY}
            for i in range(1, 5)]

    ty = make_pptx(tmp / "thanks.pptx", head + [{"title": "Thank You", "body": ""}])
    w4 = warns_of(ty)
    check("20.4 ★ 正控：末页 Thank You ⇒ ends_with_thanks 必须响",
          "ends_with_thanks" in w4, f"WARN={w4}")

    bl = make_pptx(tmp / "blank.pptx", head + [{"title": "", "body": ""}])
    w5 = warns_of(bl)
    check("20.5 ★ 正控：末页一个字都没有 ⇒ last_slide_blank 必须响",
          "last_slide_blank" in w5, f"WARN={w5}")

    good = make_pptx(tmp / "good_end.pptx",
                     head + [{"title": "下一步：收窄阈值，锁定开关基因", "body": _BODY}])
    w6 = warns_of(good)
    check("20.6 ★ 负控：末页是结论/下一步 ⇒ 这两条都不响",
          "ends_with_thanks" not in w6 and "last_slide_blank" not in w6,
          f"WARN={w6}")
    check("20.6b ★ 且它同时**不**触发标题形态那条（真实收尾句）",
          "titles_are_noun_phrases" not in w6, f"WARN={w6}")


def make_stale_skeleton(path, py: str, width: int = 12192000, slides: int = 6) -> bool:
    """造一份「**画布改了、骨架没改**」的 deck。

    刻意复现**原 `style_deck` 的产物形状**：只改 `p:sldSz` 的画布尺寸，
    占位符几何留在 master 里不动（那份 13 页成片就是这个形状）。

    ⚠️ 为什么必须专门造：修好之后的 `style_deck` **再也产不出这种文件**了，
    于是判据 `placeholder_geometry_stale` 若是靠它取正控输入，
    就会变成一条**永远不响**的判据（G 型）—— 判据的病侧输入得自己造。
    ★ 通用道理：**"改对了"的验证不能只用被改过的工具去造输入。**
    """
    if not py:
        return False          # 见 `_py_pptx()`：**别让 None 进 subprocess**（会崩掉整个套件）
    code = (
        "import sys\n"
        "from pptx import Presentation\n"
        "from pptx.util import Emu\n"
        "prs = Presentation()\n"                      # python-pptx 默认 = 4:3
        "for i in range(int(sys.argv[3])):\n"
        "    s = prs.slides.add_slide(prs.slide_layouts[1])\n"
        "    try:\n"
        "        s.shapes.title.text = '第 %d 部分' % (i + 1)\n"
        "    except Exception:\n"
        "        pass\n"
        "    for ph in s.placeholders:\n"
        "        if ph.placeholder_format.idx != 0:\n"
        "            ph.text = '正文若干字，剩下的交给讲稿去说。'\n"
        "            break\n"
        "prs.slide_width = Emu(int(sys.argv[2]))\n"   # ← 只改画布，骨架不动
        "prs.slide_height = Emu(6858000)\n"
        "prs.save(sys.argv[1])\n"
    )
    r = subprocess.run([py, "-c", code, str(path), str(width), str(slides)],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and Path(path).is_file()


def make_overflow_deck(path, py: str, *, box_w=4.0, box_h=1.0, pt=18.0,
                       chars=90, autofit="none") -> bool:
    """造一份「一个文本框，塞得下 or 塞不下」的 deck。**用来量判据的那把尺子。**

    刻意只放**一个非占位符文本框**：占位符在这套模板里被 master 的 `normAutofit`
    全部豁免（实测 `make_real_pptx` → 可测 **0** 个），拿它当正控会永远测不到东西。

    `autofit`：
      · `"none"` —— `<a:bodyPr/>` 空着 ⇒ 字多了**真的会溢出**（判据该响的那一侧）
      · `"sp"`   —— `spAutoFit`（python-pptx 的默认）⇒ 形状跟着字长高，不该响
      · `"norm"` —— `normAutofit` ⇒ 渲染器自动缩字，不该响
    ★ 这三种是**同一份框、同一段字**，只改这一个声明 —— 于是她是一组
      **只差一个变量的 A/B/C 对照**，比"各造一份"强得多。
    """
    if not py:
        return False          # 见 `_py_pptx()`：**别让 None 进 subprocess**（会崩掉整个套件）
    AUTO = {"none": "NONE", "sp": "SHAPE_TO_FIT_TEXT", "norm": "TEXT_TO_FIT_SHAPE"}
    code = (
        "import sys\n"
        "from pptx import Presentation\n"
        "from pptx.util import Inches, Pt\n"
        "from pptx.enum.text import MSO_AUTO_SIZE\n"
        "prs = Presentation()\n"
        "s = prs.slides.add_slide(prs.slide_layouts[6])\n"      # 6 = 空白版式
        "tb = s.shapes.add_textbox(Inches(0.5), Inches(0.5),\n"
        "                          Inches(float(sys.argv[2])), Inches(float(sys.argv[3])))\n"
        "tf = tb.text_frame\n"
        "tf.word_wrap = True\n"
        "tf.auto_size = getattr(MSO_AUTO_SIZE, sys.argv[6])\n"
        "tf.text = '文' * int(sys.argv[5])\n"
        "tf.paragraphs[0].runs[0].font.size = Pt(float(sys.argv[4]))\n"
        "prs.save(sys.argv[1])\n"
    )
    r = subprocess.run([py, "-c", code, str(path), str(box_w), str(box_h),
                        str(pt), str(chars), AUTO.get(autofit, "NONE")],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and Path(path).is_file()


def bump_master_title_style(src, dst, old="4400", new="8800") -> bool:
    """把 master `titleStyle` 里的 lvl1 字号从 `old` 改成 `new`（百分之一磅）。

    **为什么要动它**：字号这一路是"沿链回退到 master `txStyles`"才拿到的。
    如果实现里其实写死了一个默认字号，那么"改 master 却不影响结果"就是那个 bug 的指纹。
    ⇒ 这条是**"走链取字号"的承重自检**（改一个变量、看结果动不动）。
    纯 zipfile 改写，不需要 python-pptx。
    """
    import zipfile as _z
    try:
        with _z.ZipFile(src) as zin:
            items = [(i, zin.read(i.filename)) for i in zin.infolist()]
    except Exception:
        return False
    name = "ppt/slideMasters/slideMaster1.xml"
    hit = False
    out = []
    for info, data in items:
        if info.filename != name:
            out.append((info, data))
            continue
        txt = data.decode("utf-8")
        if ('sz="%s"' % old) not in txt:
            return False
        # 只替换**前一处** —— `titleStyle` 在 `txStyles` 里排最前，而 bodyStyle/otherStyle
        # 也可能有 4400 一类同样的值；全替换会把两个变量一起动、测不出归因。
        txt = txt.replace('sz="%s"' % old, 'sz="%s"' % new, 1)
        hit = True
        out.append((info, txt.encode("utf-8")))
    if not hit:
        return False
    try:
        with _z.ZipFile(dst, "w", _z.ZIP_DEFLATED) as zout:
            for info, data in out:
                zout.writestr(info, data)
    except Exception:
        return False
    return True


def test_text_overflow() -> None:
    """§22 文字溢出：**估算走到判据**（2026-09-21）。

    这是调研报告 §4 的 **P0-2**（"文字塞不下"是「丑」的最常见形态）。
    它分两步落地，**分界点就是这条判据该不该接**：

      第一步：只做估算 —— 因为**一个形状都量不到**。
        根因不是算法，是**查错了层**：几何在 master、字号在 master 的 `p:txStyles`，
        而 slide 层一个 `a:xfrm`、一个显式字号都没有。
      第二步：`PhChain` 把继承链解析出来之后，量到了，才谈得上定门槛。

    ⚠️ 这一节最要紧的不是"判据响了"，而是**它的豁免与"没查"两侧都被钉住**：
      · 自适应豁免失效 ⇒ **每一份 deck 的每个占位符都被误报**
      · 「量不到」被读成「合规」 ⇒ 判据静默报废（本项目栽过四次）
    """
    section("22 文字溢出（估算 → 判据；继承链是前提）")
    py = _py_pptx()
    tmp = _TMPROOT / "overflow"
    tmp.mkdir(parents=True, exist_ok=True)

    def warns_of(p) -> list[str]:
        return [x.check_id for x in _gate(_aud().audit(p, workdir=tmp),
                                          "type").findings if x.level == "WARN"]

    def skips_of(p) -> list[str]:
        return [x.check_id for x in _gate(_aud().audit(p, workdir=tmp),
                                          "type").findings if x.level == "SKIP"]

    # ── 22.A 正控：塞不下，且**没有**任何自适应 ⇒ 必须响 ─────────────
    big = tmp / "too_big.pptx"
    ok_big = make_overflow_deck(big, py, box_w=4.0, box_h=1.0, pt=18.0,
                                chars=120, autofit="none")
    fb = D.probe_pptx(big)
    check("22.1 前置：造出「小框 + 长文本 + 无自适应」的 deck，且**量得到**",
          ok_big and fb.overflow_measured > 0,
          f"built={ok_big} 可测={fb.overflow_measured} 分类={fb.overflow_skipped}")
    check("22.2 ★ 正控：估高超过可用高 ⇒ `text_overflows_box` 必须响",
          "text_overflows_box" in warns_of(big),
          f"比值={fb.overflow_worst} WARN={warns_of(big)}")
    check("22.3 且比值确实落在门槛那侧（> OVERFLOW_WARN_RATIO）",
          bool(fb.overflow_worst) and fb.overflow_worst > D.OVERFLOW_WARN_RATIO,
          f"比值={fb.overflow_worst} 门槛={D.OVERFLOW_WARN_RATIO}")

    # ── 22.B ★ 豁免守卫：**同一份框、同一段字**，只加自适应 ──────────
    #   这三种是只差一个变量的 A/B/C —— 比"各造一份"强得多（变量少 = 归因硬）。
    for tag, key, label in (("sp", "spAutoFit", "形状跟着字长高"),
                            ("norm", "normAutofit", "渲染器自动缩字")):
        p = tmp / f"fit_{tag}.pptx"
        ok = make_overflow_deck(p, py, box_w=4.0, box_h=1.0, pt=18.0,
                                chars=120, autofit=tag)
        fo = D.probe_pptx(p)
        check(f"22.4 ★ 豁免守卫（{key} = {label}）：同框同字，**一个都不许响**",
              ok and fo.overflow_measured == 0
              and "text_overflows_box" not in warns_of(p),
              f"built={ok} 可测={fo.overflow_measured} 分类={fo.overflow_skipped} "
              f"WARN={warns_of(p)}")
    check("22.5 ★ 「测的不是空气」：22.4 那两个形状**去掉自适应就会响**"
          "（同一夹具只差这一个声明）",
          fb.overflow_worst is not None and fb.overflow_worst > D.OVERFLOW_WARN_RATIO,
          f"无自适应那份比值={fb.overflow_worst}")

    # ── 22.C 负控：放得下 / 真实成片 ⇒ 不许响 ────────────────────
    small = tmp / "fits.pptx"
    ok_small = make_overflow_deck(small, py, box_w=4.0, box_h=1.0, pt=18.0,
                                  chars=8, autofit="none")
    fs = D.probe_pptx(small)
    check("22.6 负控：同样无自适应、但文字放得下 ⇒ 不响",
          ok_small and fs.overflow_measured > 0
          and "text_overflows_box" not in warns_of(small),
          f"built={ok_small} 比值={fs.overflow_worst}")

    real = ROOT / "runs" / "20260921-124950" / "deck.pptx"
    if real.is_file():
        fr = D.probe_pptx(real)
        check("22.7 ★ 真实成片负控：实测比值 0.973（**刚好排下**）⇒ 不响",
              fr.overflow_measured > 0 and 0.9 < (fr.overflow_worst or 0) < D.OVERFLOW_WARN_RATIO
              and "text_overflows_box" not in warns_of(real),
              f"可测={fr.overflow_measured} 比值={fr.overflow_worst} WARN={warns_of(real)}")

    # ── 22.D ★ 「没查」≠「合规」──────────────────────────────────
    exempt = tmp / "all_exempt.pptx"
    ok_ex = make_real_pptx(exempt, py, slides=6, titles=GOOD_TITLES[:6])
    fe = D.probe_pptx(exempt)
    check("22.8 ★ 全豁免的 deck ⇒ 可测 = 0，此时必须出 `overflow_unmeasurable`（SKIP）",
          ok_ex and fe.overflow_measured == 0 and "overflow_unmeasurable" in skips_of(exempt),
          f"可测={fe.overflow_measured} 分类={fe.overflow_skipped} SKIP={skips_of(exempt)}")
    check("22.9 ★ 且它**不许**把「没查」写成「合规」（既不能 WARN、也不能被读成 PASS）",
          "text_overflows_box" not in warns_of(exempt),
          f"WARN={warns_of(exempt)}")

    # ── 22.E ★ 会计守卫：0 必须能被解释 ─────────────────────────
    #   为什么值得单独立一条：`overflow_measured == 0` 有**四种互不相同**的原因
    #   （豁免 / 框量不到 / 字号推不出 / 无文字）。若某条 `return None` 忘了计数，
    #   那个 0 就重新变成**含混** —— 而"含混"正是这条判据最容易死掉的方式。
    counted = sum(fb.overflow_skipped.values())
    shapes = 0
    with zipfile.ZipFile(big) as zf:
        for n in zf.namelist():
            if re.match(r"ppt/slides/slide\d+\.xml$", n):
                shapes += sum(1 for e in D.ET.fromstring(zf.read(n)).iter()
                              if D._local(e.tag) == "sp")
    check("22.10 ★ 会计守卫：分类计数之和 == 页面里 `sp` 总数（没有形状漏记原因）",
          counted == shapes, f"计数和={counted} sp总数={shapes} 明细={fb.overflow_skipped}")
    check("22.11 ★ 且 `ok` 那一格与 `overflow_measured` 对得上",
          fb.overflow_skipped.get("ok") == fb.overflow_measured,
          f"ok={fb.overflow_skipped.get('ok')} measured={fb.overflow_measured}")

    # ── 22.F ★ 继承链承重自检：字号真的是**沿链取来的** ─────────────
    #   改一个变量（master `titleStyle` 的 lvl1 字号 44pt → 88pt）看结果动不动。
    #   若实现里其实写死了一个默认字号，"改了 master 却纹丝不动"就是这个 bug 的指纹。
    if real.is_file():
        bump = tmp / "title_style_2x.pptx"
        ok_bump = bump_master_title_style(real, bump)
        fb2 = D.probe_pptx(bump)
        check("22.12 ★ 承重自检：把 master `titleStyle` 字号抬到 2 倍 ⇒ 封面标题的"
              "溢出比**显著上升**（证明字号确实来自继承链，不是写死的默认值）",
              ok_bump and fb2.overflow_worst is not None and fr.overflow_worst is not None
              and fb2.overflow_worst > fr.overflow_worst * 1.5,
              f"改前={fr.overflow_worst} 改后={fb2.overflow_worst} ok={ok_bump}")

    # ── 22.G 口径守卫：占位符类型 → txStyles 的映射必须**全覆盖** ──
    #   漏一项的后果是静默的：那一类占位符会掉进 `otherStyle`，字号取错一档。
    miss = [t for t in D.CONTENT_PH_TYPES if t not in D._TXSTYLE_OF]
    check("22.13 ★ 口径守卫：`CONTENT_PH_TYPES` 每一项都在 `_TXSTYLE_OF` 里有归属",
          not miss, f"漏项={miss}")


def test_skeleton_geometry() -> None:
    """§21 骨架几何：**画布改了，骨架也必须跟着改**（2026-09-21）。

    这是"那能做的这么丑"的**第二层根因**（第一层是压根没套主题）：
    `style_deck` 原实现只改画布尺寸，而占位符的几何住在 master 里、还是 4:3 摆的
    ⇒ 16:9 页面上内容挤在左边、右侧空出 28.8%。

    实测那份成片**自带对照**（同一页！）：`style_deck` 自己加的页脚铺到 **95.4%**，
    而继承来的 Title/Content 只到 **71.2%** —— 两套坐标系挤在一张纸上。

    ⚠️ 这一节的重点仍然是**误报**：判据碰的是每一份 deck 的骨架，
    报错的代价是"所有稿子都被说骨架歪"。所以配了两条豁免守卫（页脚 / 分母）。
    """
    section("21 骨架几何（画布与骨架必须同源）")
    py = _py_pptx()
    tmp = _TMPROOT / "skeleton"
    tmp.mkdir(parents=True, exist_ok=True)

    def warns_of(p) -> list[str]:
        return [x.check_id for x in _gate(_aud().audit(p, workdir=tmp),
                                          "type").findings if x.level == "WARN"]

    # ── 21.A 正控：画布 16:9、骨架 4:3（原 style_deck 的产物形状）──
    stale = tmp / "stale_skeleton.pptx"
    ok_stale = make_stale_skeleton(stale, py, width=12192000)
    check("21.1 前置：造出「画布 16:9 ＋ 骨架 4:3」的 deck（原 style_deck 的形状）",
          ok_stale and D.probe_pptx(stale).is_widescreen is True, f"built={ok_stale}")
    w1 = warns_of(stale)
    check("21.2 ★ 正控：骨架与画布不同源 ⇒ placeholder_geometry_stale 必须响",
          "placeholder_geometry_stale" in w1, f"WARN={w1}")

    geoms = D.probe_pptx(stale).master_geo
    W = D.probe_pptx(stale).slide_cx
    ratios = []
    for g in geoms:
        ml, mr = g["x"], W - (g["x"] + g["cx"])
        if g.get("ph") in D.CONTENT_PH_TYPES and min(ml, mr) > 0:
            ratios.append(max(ml, mr) / min(ml, mr))
    check("21.3 且倍数确实落在断层那侧（> 门槛 3）",
          ratios and min(ratios) > D.GEO_ASYM_MAX,
          f"ratios={[round(r, 1) for r in ratios]}")

    # ── 21.B ★ 豁免守卫：页脚/日期/页码**本来就该靠边** ──────────
    #   实测 master 的 `sldNum`：ml=6553200 / mr=457200 ⇒ 14.3 倍。
    #   不排除它们的话，**每一份** deck 都会被报 ⇒ 判据当场变成噪声。
    foot = [g for g in geoms if g.get("ph") not in D.CONTENT_PH_TYPES]
    check("21.4 ★ 守卫：这份 deck 的 master **确实带着**靠边的页脚类占位符"
          "（否则这条守卫测的是空气）",
          bool(foot), f"非内容占位符={[g.get('ph') for g in foot]}")
    foot_ratio = []
    for g in foot:
        ml, mr = g["x"], W - (g["x"] + g["cx"])
        if min(ml, mr) > 0:
            foot_ratio.append(max(ml, mr) / min(ml, mr))
    check("21.5 ★ 且它们的边距比**超出门槛**（>3）却**不许**被点名",
          foot_ratio and max(foot_ratio) > D.GEO_ASYM_MAX,
          f"页脚类比值={[round(r, 1) for r in foot_ratio]}")
    check("21.6 ★ 所以 21.2 报的名单里只有内容占位符（页脚类一个都不许有）",
          all("sldNum" not in x and "ftr" not in x and "dt" not in x
              for x in [f.message for f in _gate(_aud().audit(stale, workdir=tmp),
                                                 "type").findings
                        if f.check_id == "placeholder_geometry_stale"]),
          "页脚类被误点名了")

    # ── 21.C 负控：几何同源的 deck 不许响 ────────────────────────
    good = tmp / "synced.pptx"
    ok_good = make_real_pptx(good, py, slides=6, titles=GOOD_TITLES[:6])
    check("21.7 负控：4:3 真稿（骨架与画布同源）⇒ 几何判据**不响**",
          ok_good and "placeholder_geometry_stale" not in warns_of(good),
          f"built={ok_good} WARN={warns_of(good)}")

    # ── 21.D 修复正控：style_deck 必须把骨架一起改 ───────────────
    fixed = tmp / "fixed.pptx"
    shutil.copy(stale, fixed)
    st = _style(fixed.name, workdir=tmp)
    f2 = D.probe_pptx(fixed)
    ratios2 = []
    for g in f2.master_geo:
        ml, mr = g["x"], f2.slide_cx - (g["x"] + g["cx"])
        if g.get("ph") in D.CONTENT_PH_TYPES and min(ml, mr) > 0:
            ratios2.append(max(ml, mr) / min(ml, mr))
    check("21.8 ★ 修复正控：套完主题后骨架**对称了**（比值回落到 ≤ 门槛）",
          st.ok and ratios2 and max(ratios2) <= D.GEO_ASYM_MAX,
          f"ok={st.ok} 比值={[round(r, 2) for r in ratios2]}")
    check("21.9 ★ 且判据随之消失（同源了）",
          "placeholder_geometry_stale" not in warns_of(fixed),
          f"WARN={warns_of(fixed)}")
    check("21.10 ★ `changed` 里如实写了骨架被重排（不许只报「画布改了」）",
          any("骨架" in c for c in st.changed), str(st.changed)[:220])

    # ── 21.E 幂等：连套两次不许累积漂移 ──────────────────────────
    #   "保持边距"方案白送这个性质（形状已在目标画布上时变换是恒等）；
    #   换成"等比缩放"就会每次再缩一点 ⇒ 这个断言会立刻抓住。
    geo1 = [(g["ph"], g["x"], g["cx"]) for g in D.probe_pptx(fixed).master_geo]
    st2 = _style(fixed.name, workdir=tmp)
    geo2 = [(g["ph"], g["x"], g["cx"]) for g in D.probe_pptx(fixed).master_geo]
    check("21.11 ★ 幂等：再套一次 ⇒ 骨架几何**一字不变**（不累积漂移）",
          st2.ok and geo1 == geo2, f"ok={st2.ok} 变了吗={geo1 != geo2}")

    # ── 21.H ★ 路径无关性：存量污染修出来，必须和正常流程落在同一几何 ──
    #   做硬化实验时撞见的一条**不变量**，比"变绿了"值钱：
    #     ① 存量污染（画布已 16:9 ＋ 骨架仍 4:3）—— 旧版 `style_deck` 的产物
    #     ② 真实流程（4:3 真稿 → 16:9）
    #   两条**不同的输入路径**，修完必须给出**同一个结果**。否则"修复"取决于
    #   你走哪条路 ⇒ 工具的输出不可预期，也就没法验收。
    #   ⚠️ 这条同时是硬化的**唯一正确性证据**：一开始我只让 ① 修好就行，
    #      而"把 7.67 拉成 1.0"有一万种办法（包括把内容瞎拉到离谱宽度）。
    #      真正证明"拉对了"的，是它和 ② 撞在同一个数上。
    def content_geo(p) -> list:
        f = D.probe_pptx(p)
        return sorted((g.get("ph"), g["x"], g["cx"]) for g in f.master_geo
                      if g.get("ph") in D.CONTENT_PH_TYPES)

    def width_pct(p) -> list:
        f = D.probe_pptx(p)
        return [round(100.0 * g["cx"] / f.slide_cx, 1) for g in f.master_geo
                if g.get("ph") in D.CONTENT_PH_TYPES]

    pol2, rel2 = tmp / "path_polluted.pptx", tmp / "path_real.pptx"
    shutil.copy(stale, pol2)
    shutil.copy(good, rel2)
    ok_p = _style(pol2.name, workdir=tmp).ok
    ok_r = _style(rel2.name, workdir=tmp).ok
    check("21.14 ★ 路径无关性：存量污染与 4:3 真稿修完**落在同一几何**"
          "（修复结果不取决于你走哪条路）",
          ok_p and ok_r and content_geo(pol2) == content_geo(rel2),
          f"ok={ok_p}/{ok_r} 污染源→{content_geo(pol2)} 真稿→{content_geo(rel2)}")
    check("21.15 ★ 且两者占宽都 ≥ 90%（右侧那块空当**真的**补上了，"
          "不是只把判据哄闭嘴）",
          ok_p and ok_r and bool(width_pct(pol2) + width_pct(rel2))
          and min(width_pct(pol2) + width_pct(rel2)) >= 90.0,
          f"污染源={width_pct(pol2)}% 真稿={width_pct(rel2)}%")

    # ── 21.I ★ 注入的门槛**真的在管**，不是装饰 ─────────────────
    #   硬化门槛由主进程经 payload 注入（`geo_asym_max`），因为 `_STYLE_SCRIPT`
    #   住在独立解释器里、引用不到 `GEO_ASYM_MAX`。**"注入"这件事本身要验**：
    #   若脚本里另有一个字面量门槛在管事，注入就是假的、两处会各自漂移（H 型）。
    #   ⚠️ 单向测不得：只测"门槛调大 ⇒ 不修"会被"这条链路压根没在修"白白蒙混过关
    #      ⇒ 必须先跑正控（真门槛下**确实修了**），再测负控。
    def _run_style(srcp, asym) -> dict:
        pl2 = json.dumps({"pptx": str(srcp), "colors": {}, "fonts": {},
                          "size": [12192000, 6858000], "bg": "", "footer": False,
                          "geo_asym_max": asym}, ensure_ascii=False)
        rr = subprocess.run([py, "-c", D._STYLE_SCRIPT], input=pl2.encode("utf-8"),
                            capture_output=True, timeout=180)
        for line in reversed((rr.stdout or b"").decode("utf-8", "replace").splitlines()):
            if line.strip().startswith("{"):
                return {"rc": rr.returncode, **json.loads(line.strip())}
        return {"rc": rr.returncode, "parse_fail": (rr.stderr or b"").decode("utf-8", "replace")[-200:]}

    pos = tmp / "inject_pos.pptx"
    shutil.copy(stale, pos)
    d_pos = _run_style(pos, D.GEO_ASYM_MAX)
    check("21.16 ★ 正控（先把这条链路的「能修」立住）：门槛 = `GEO_ASYM_MAX` ⇒ 确实重排了",
          d_pos.get("rc") == 0 and (d_pos.get("geometry_remapped") or 0) > 0,
          f"rc={d_pos.get('rc')} remapped={d_pos.get('geometry_remapped')} err={d_pos.get('error')}")
    neg = tmp / "inject_neg.pptx"
    shutil.copy(stale, neg)
    d_neg = _run_style(neg, 1000.0)          # 大到不可能触发
    check("21.17 ★ 负控：门槛调到 1000 ⇒ 修复**当场关掉**（remapped 归零）"
          "—— 证明管事的确实是注入的那个数，脚本里没有第二个字面量",
          d_neg.get("rc") == 0 and d_neg.get("geometry_remapped") == 0,
          f"rc={d_neg.get('rc')} remapped={d_neg.get('geometry_remapped')} err={d_neg.get('error')}")

    # ── 21.F 超画布：骨架比画布还宽 ⇒ 另一条判据 ─────────────────
    tiny = tmp / "tiny_canvas.pptx"
    ok_tiny = make_stale_skeleton(tiny, py, width=6000000)   # 画布缩到比骨架窄
    w_tiny = warns_of(tiny)
    check("21.12 ★ 正控：骨架**比画布还宽** ⇒ placeholder_offcanvas 必须响",
          ok_tiny and "placeholder_offcanvas" in w_tiny, f"built={ok_tiny} WARN={w_tiny}")

    # ── 21.G ★ 单源守卫：白名单在两处必须一致 ────────────────────
    #   `_STYLE_SCRIPT` 是独立解释器里的字符串，**引用不到** `CONTENT_PH_TYPES`，
    #   只能重写一份 ⇒ 改一处忘一处就是 H 型（这条断言就是盯着它的那只眼）。
    src = Path(D.__file__).read_text(encoding="utf-8")
    script = src.split("_STYLE_SCRIPT = r'''", 1)[-1].split("'''", 1)[0]
    missing = [t for t in D.CONTENT_PH_TYPES if '"%s"' % t not in script]
    check("21.13 ★ 单源守卫：`CONTENT_PH_TYPES` 的每一项都出现在 `_STYLE_SCRIPT` 里",
          not missing, f"漏项={missing}")


def test_design_assets() -> None:
    """§23 设计资产与「精排」（compose_deck，2026-09-21）。

    起因：用户看完一份**五门全绿**的 13 页组会 deck，只说了一句
    「太差了，不能直接上组会」。量下来根因不在模型、也不在判据 ——
    **整条链路上没有任何一处存着设计资产**（全链最"具体"的设计数值
    是一条 0.01 英寸的页脚线）。⇒ **判据只能划定下限；产出上限来自资产本身。**

    这一节钉四件事，少一件都不算立住：

      ① **资产自洽**：单位换算精确、三区网格首尾相接、页型几何落在网格内、
         色彩角色只写槽名（hex 单源在 `DECK_THEMES`）
      ② ★ **阶梯满足客观判据**：`min(LADDER_PT) >= 场景字号门槛`
         —— 自己定的阶梯不许把自己打红
      ③ ★ **对齐判据有牙**：负控（未加工的稿读回「未设对齐」）＋
         正控（排完读回 0 个未设对齐）
      ④ ★ **承重自检**：把 `set_align` 那一步从脚本里摘掉 ⇒ 正控**当场失守**

    ⚠️ ④ 为什么必须做：`set_align` 是本工具自己加的一步，而「自己加的机制」
      最容易在后续重构里被顺手删掉，且**删掉之后一切照旧绿** ——
      除非有一条断言盯着它。
    """
    section("23 设计资产与精排（compose_deck）")
    DG = D.design
    tmp = _TMPROOT / "design23"
    tmp.mkdir(parents=True, exist_ok=True)
    py = _py_pptx()

    # ══ 23.A 资产自洽（纯常量，零 IO）══════════════════════════
    check("23.1 单源：`LADDER_PT` 每一档都由 `LADDER_PX` 派生（不是手抄的）",
          all(DG.LADDER_PT[k] == DG.px_to_pt(v) for k, v in DG.LADDER_PX.items()),
          f"{DG.LADDER_PT}")
    check("23.2 单位换算精确：1280px → 12192000 EMU、960.0 pt（无取整误差）",
          DG.px_to_emu(1280) == 12192000 and DG.px_to_pt(1280) == 960.0,
          f"emu={DG.px_to_emu(1280)} pt={DG.px_to_pt(1280)}")
    check("23.3 ★ 画布与既有 16:9 常量同源（1280px 恰好等于 SLIDE_16_9 的宽）",
          D.SLIDE_16_9[0] == DG.px_to_emu(DG.CANVAS_PX[0]), f"{D.SLIDE_16_9}")
    # ★★ 这一条是「零回归」那句错话换来的：我第一版把页脚定在 10.5pt，
    #    理由是「与既有页脚同值 ⇒ 零回归」。实测那个"没报"是因为旧稿字号
    #    **继承母版、量不出来** ⇒ 判据走的是 SKIP（「没查」），不是 PASS。
    #    一旦把字号写实，10.5pt 立刻被 `font_small` 抓住。
    _th = _aud()._th("min_font_pt", 12.0)
    check(f"23.4 ★ 阶梯下限 ≥ 判据门槛（{_th:.0f}pt）—— 自己定的阶梯不许把自己打红",
          min(DG.LADDER_PT.values()) >= _th,
          f"min={min(DG.LADDER_PT.values())} < {_th}")
    check("23.5 阶梯**档档可辨**（pt 值互不重复，否则等于少一档）",
          len(set(DG.LADDER_PT.values())) == len(DG.LADDER_PT),
          f"{sorted(DG.LADDER_PT.values())}")
    check("23.6 加粗档位都在阶梯上（不许凭空造一个档）",
          DG.BOLD_KEYS <= set(DG.LADDER_PX), f"{sorted(DG.BOLD_KEYS)}")
    a, b, c = (DG.BANDS_PX[k] for k in ("A", "B", "C"))
    check("23.7 三区网格首尾相接、恰好覆盖整画布（不重叠、不留缝）",
          a[0] == 0 and a[1] == b[0] and b[1] == c[0] and c[1] == DG.CANVAS_PX[1],
          f"{a}{b}{c}")
    check("23.8 内容带宽度 == 画布宽 − 2×页边距（单源，不是另写一个数）",
          DG.CONTENT_W_PX == DG.CANVAS_PX[0] - 2 * DG.PAD_PX, f"{DG.CONTENT_W_PX}")
    _oob = []
    for _k, _pk in DG.PAGE_KINDS.items():
        for _s in _pk["slots"]:
            _l, _t, _w, _h = _s["box"]
            if _l < DG.PAD_PX or _l + _w > DG.CANVAS_PX[0] - DG.PAD_PX:
                _oob.append((_k, _s["role"], _s["box"]))
    check("23.9 ★ 六种页型的每个 slot 都落在左右页边距内（越界就是几何表写错了）",
          not _oob, f"{_oob}")
    check("23.10 页型别名都指向合法页型（别名表不许指到不存在的页型上）",
          set(DG.KIND_ALIASES.values()) <= set(DG.KIND_NAMES),
          f"{sorted(set(DG.KIND_ALIASES.values()) - set(DG.KIND_NAMES))}")
    # ★ 色彩角色只写**槽名**：色值单源在 `DECK_THEMES`，两处都写 hex 就是 H 型。
    _hexish = []
    for _role, _slot in DG.COLOR_ROLES.items():
        for _v in (_slot if isinstance(_slot, list) else [_slot]):
            if not isinstance(_v, str) or "#" in _v or re.fullmatch(r"[0-9A-Fa-f]{6}", _v):
                _hexish.append((_role, _v))
    check("23.11 ★ 色彩角色只写槽名、**不写 hex**（hex 单源在 DECK_THEMES）",
          not _hexish, f"{_hexish}")
    _slots_in_theme = set()
    for _t in D.DECK_THEMES.values():
        _slots_in_theme |= set(_t["colors"])
    _bad_slot = []
    for _role, _slot in DG.COLOR_ROLES.items():
        for _v in (_slot if isinstance(_slot, list) else [_slot]):
            if _v not in _slots_in_theme:
                _bad_slot.append((_role, _v))
    check("23.12 ★ 每个槽名都真的存在于 `DECK_THEMES` 里（否则 patch 写不进去）",
          not _bad_slot, f"{_bad_slot}")
    _pl = DG.as_payload()
    check("23.13 ★ payload 带全脚本要的键（**尤其 `align_default`** ——"
          " 少了它脚本会 KeyError ⇒ 复测报「核不了」）",
          all(k in _pl for k in ("canvas_px", "px_emu", "ladder_pt", "bold_keys",
                                 "align_default", "color_roles", "page_kinds",
                                 "footer")),
          f"{sorted(_pl)}")

    if py is None:
        check("23.13b 前置：拿得到 python-pptx 解释器（后面的正控要真文件）", False,
              "find_notes_python() 返回 None")
        return

    # ══ 23.B 对齐判据：负控 → 正控 ═════════════════════════════
    def _loose(p) -> int:
        rep, _err = D._align_report(p)
        return sum(v["loose"] for v in rep.values())

    def _loose_bul(p) -> int:
        rep, _err = D._bullet_report(p)
        return sum(v["loose"] for v in rep.values())

    raw = tmp / "raw.pptx"
    check("23.14 前置：用 python-pptx 造出真文件（默认模板 = 起手那份的真实长相）",
          make_real_pptx(raw, py, slides=3), "make_real_pptx 失败")
    check("23.15 ★ 负控：未经精排的稿 ⇒ 读回**确有**文字没设显式对齐"
          "（`algn` 缺省时 PowerPoint 去版式取值）",
          _loose(raw) > 0, f"loose={_loose(raw)}")

    K3 = ["cover", "content", "closing"]
    r1 = D.compose_deck(raw, K3, workdir=tmp, python_exe=py, cfg=_Cfg())
    check("23.16 ★ 正控：精排后 `ok=True`", r1.ok, f"{r1.problems}")
    check("23.17 ★ 且读回来**一个**未设显式对齐的文字形状都没有",
          _loose(raw) == 0, f"loose={_loose(raw)}")
    check("23.18 且复测确实数到了文字形状（不是「没查」被读成「合规」）",
          r1.align_text > 0, f"align_text={r1.align_text}")
    check("23.19 且三个页型标记都在（脚本真的走到了每一页）",
          sorted(r1.marks.values()) == sorted(K3), f"{r1.marks}")
    check("23.20 且视觉资产没变少（排版不许把图表弄丢）",
          r1.visuals_after >= r1.visuals_before,
          f"{r1.visuals_before}→{r1.visuals_after}")

    # ── 23.C ★ 承重自检：摘掉 `set_align` ⇒ 正控必须当场失守 ─────
    #   ⚠️ 必须用**另一份没排过版的**源文件。第一版这里图省事复用 `raw`，
    #      结果 23.22 假绿：`raw` 已经被 23.16 排过一次、`algn` 早就写进去了，
    #      摘掉 `set_align` 也**继承得到上一次的结果** ⇒ 测的不是"有没有设"，
    #      而是"上次设过没有"。**承重自检本身踩了"输入已经被改过"这个坑。**
    _cs = D._COMPOSE_SCRIPT
    _call = 'aligned += set_align(pick, slot.get("align") or align_default)'
    check("23.21 前置：脚本里**确实有** `set_align` 的调用点（否则摘的是空气）",
          _call in _cs, "调用点没找到 —— 23.22 测的就是空气")
    if _call in _cs:
        raw2 = tmp / "raw_broken.pptx"
        check("23.21b 前置：另造一份**没排过版**的源（否则复用的是上一轮的 algn）",
              make_real_pptx(raw2, py, slides=3), "make_real_pptx 失败")
        broken = _cs.replace(_call, "aligned += 0")
        with swapped(D, "_COMPOSE_SCRIPT", broken):
            r2 = D.compose_deck(raw2, K3, workdir=tmp, python_exe=py, cfg=_Cfg())
        check("23.22 ★ 承重自检：摘掉 `set_align` ⇒ `compose_deck` **报未完成**"
              "（机制在承重，不是装饰）",
              not r2.ok, f"居然还 ok=True；problems={r2.problems}")
        check("23.23 ★ 且被摘掉之后读回来**确有**未设对齐的字（失守是真的）",
              _loose(raw2) > 0, f"loose={_loose(raw2)}")
    check("23.24 承重自检自己收干净（脚本复原，调用点又在了）",
          _call in D._COMPOSE_SCRIPT, "没复原")

    # ══ 23.D 消息与两个渠道 ════════════════════════════════════
    _msg = r1.format_for_model()
    check("23.25 给模型的话里**点名**读回来核过对齐（不许只说「已排版」）",
          "显式水平对齐" in _msg and "读回来" in _msg, _msg[:200])
    c_deck = TaskContract(raw_goal="把材料做成组会 PPT", needs_deck=True)
    check("23.26 ★ SYSTEM 与契约两个渠道都点名 `compose_deck`（模型读得到这个动作）",
          "compose_deck" in L.SYSTEM and "compose_deck" in c_deck.to_prompt(),
          f"SYSTEM={'compose_deck' in L.SYSTEM}")
    check("23.27 负控：非 deck 任务读不到 `compose_deck`（无差别加戏会污染别的任务）",
          "compose_deck" not in TaskContract(raw_goal="写一段产品说明",
                                             needs_deck=False).to_prompt(), "")
    _rec = "\n".join(C.DECK_TOOLCHAIN_RECIPE)
    check("23.28 ★ 配方把它写成**二选一**（不许写成「再加一步」——"
          " 那模型会把两步都调，两套坐标系打架）",
          "二选一" in _rec and "compose_deck" in _rec, _rec[:200])
    _i_s = next((i for i, s in enumerate(C.DECK_TOOLCHAIN_RECIPE)
                 if "style_deck" in s), -1)
    _i_a = next((i for i, s in enumerate(C.DECK_TOOLCHAIN_RECIPE)
                 if "audit_deck" in s), -1)
    check("23.29 且外观这一步仍在 `audit_deck` **之前**（体检必须压轴）",
          -1 < _i_s < _i_a, f"style={_i_s} audit={_i_a}")

    # ══ 23.E 一行字占多高：文本框的最小高度是**算出来**的 ═══════
    #
    #  血债：我把 `subtitle` 从 32px 抬到 40px，**忘了封面副标题的框还是 44px**。
    #  复测报 `text_overflows_box`（1.395），但**渲染看并没有被裁掉**
    #  （PowerPoint 默认不裁剪）⇒ 它不是"画面坏了"，是**框装不下自己那一行**：
    #  字一换行，第二行就压到下面的署名上。
    #  ⇒ 治法不是放宽判据，是**让资产自己算过一遍**。
    #  ⚠️ `check` 的第二个参数是**布尔**（True = 通过）。第一版这里直接传了
    #  `DG.short_box_slots()` —— 空列表是 **falsy** ⇒ 合规反而被记成失败。
    #  凡是"断言某集合为空"的地方都要显式写 `== []`（或 `not ...`）。
    check("23.30 ★ 每个文本框都装得下自己那一行（`short_box_slots()` 为空）",
          DG.short_box_slots() == [], f"{DG.short_box_slots()}")
    check("23.31 ★ 行距/内边距**单源**：`deck` 取的是 `design` 的同一对象"
          "（两处各写一个 1.2 ⇒ 改了估算器、框高约束静默失效）",
          (D._LINE_SPACING is DG.LINE_SPACING
           and D._INS_LR == DG.TEXT_INSET_LR_EMU
           and D._INS_TB == DG.TEXT_INSET_TB_EMU),
          f"{D._LINE_SPACING} / {D._INS_LR} / {D._INS_TB}")
    check("23.32 最小框高确实随档位变（不是个常数，否则这条自检没意义）",
          DG.min_box_h_px("cover_title") > DG.min_box_h_px("footer"),
          f"{DG.min_box_h_px('cover_title')} vs {DG.min_box_h_px('footer')}")

    # ★ 负控：把框压矮 ⇒ 这条自检**必须当场响**。
    #   没有它，"`short_box_slots() == []`"就可能是"这函数永远返回空"。
    _pk_slots = DG.PAGE_KINDS["content"]["slots"]
    _saved_box = _pk_slots[0]["box"]
    try:
        _pk_slots[0]["box"] = (_saved_box[0], _saved_box[1], _saved_box[2], 10)
        _bad = DG.short_box_slots()
    finally:
        _pk_slots[0]["box"] = _saved_box
    check("23.33 ★ 负控：人为把框压矮 ⇒ 这条自检当场响（否则它在测空气）",
          len(_bad) >= 1, f"压矮后居然还是空的")
    check("23.34 负控自己收干净（`short_box_slots()` 又为空了）",
          DG.short_box_slots() == [], f"{DG.short_box_slots()}")

    # ══ 23.F 项目符号：按 slot 的 role 显式拥有 ════════════════
    #
    #  血债：参考样例的**章节页副标题**顶上冒出一个孤零零的「•」——
    #  因为那个槽吃的是 `body` 占位符，而 `body` 在版式里是带点的列表。
    #  **换一个没有列表样式的版式来建，点就没了** ⇒ 与对齐是同一个病。
    _roles = {s["role"] for pk in DG.PAGE_KINDS.values() for s in pk["slots"]}
    check("23.35 ★ `BULLET_ROLES` 里的 role 都真的出现在页型表里（不许写空气）",
          DG.BULLET_ROLES <= _roles, f"{sorted(DG.BULLET_ROLES)}")
    check("23.36 ★ payload 带 `bullet_roles`（少了它脚本 KeyError ⇒ 报核不了）",
          "bullet_roles" in DG.as_payload(), "")
    check("23.37 ★ 正文槽算「列表」、标题/副标题槽不算"
          "（口径按 role，不按形状出身）",
          ({"body", "body_left", "body_right"} <= DG.BULLET_ROLES
           and not ({"title", "subtitle", "anchor", "signature"}
                    & DG.BULLET_ROLES)),
          f"{sorted(DG.BULLET_ROLES)}")

    _bul_raw = tmp / "raw_bul.pptx"
    check("23.38 前置：另造一份**没排过版**的源（承重自检要干净输入）",
          make_real_pptx(_bul_raw, py, slides=3), "make_real_pptx 失败")
    check("23.39 ★ 负控：未精排的稿 ⇒ 读回**确有**段落没写项目符号声明"
          "（不写就去版式的列表样式取值）",
          _loose_bul(_bul_raw) > 0, f"loose={_loose_bul(_bul_raw)}")
    r3 = D.compose_deck(_bul_raw, K3, workdir=tmp, python_exe=py, cfg=_Cfg())
    check("23.40 ★ 正控：精排后 `ok=True` 且**一个**漏写的段落都没有",
          r3.ok and _loose_bul(_bul_raw) == 0,
          f"ok={r3.ok} loose={_loose_bul(_bul_raw)} {r3.problems}")
    check("23.41 且复测确实数到了段落（不是「没查」被读成「合规」）",
          r3.bullet_paras > 0, f"bullet_paras={r3.bullet_paras}")

    _b_call = 'bulleting += set_bullet(pick, str(slot["role"]) in bullet_roles)'
    check("23.42 前置：脚本里**确实有** `set_bullet` 的调用点（否则摘的是空气）",
          _b_call in D._COMPOSE_SCRIPT, "调用点没找到 —— 23.43 测的就是空气")
    if _b_call in D._COMPOSE_SCRIPT:
        _raw4 = tmp / "raw_bul2.pptx"
        check("23.42b 前置：另造一份没排过版的源（承重自检要干净输入）",
              make_real_pptx(_raw4, py, slides=3), "make_real_pptx 失败")
        with swapped(D, "_COMPOSE_SCRIPT",
                     D._COMPOSE_SCRIPT.replace(_b_call, "bulleting += 0")):
            r4 = D.compose_deck(_raw4, K3, workdir=tmp, python_exe=py, cfg=_Cfg())
        check("23.43 ★ 承重自检：摘掉 `set_bullet` ⇒ `compose_deck` **报未完成**"
              "（机制在承重，不是装饰）",
              not r4.ok, f"居然还 ok=True；problems={r4.problems}")
        check("23.44 ★ 且失守是真的（读回确有段落漏写符号声明）",
              _loose_bul(_raw4) > 0, f"loose={_loose_bul(_raw4)}")
    check("23.45 承重自检自己收干净（脚本复原，调用点又在了）",
          _b_call in D._COMPOSE_SCRIPT, "没复原")


# ══════════════════════════════════════════════════════════════════
# 24 · 参考样例（随库发布的 deck 必须**和代码同源**）
# ══════════════════════════════════════════════════════════════════
def _real_registry():
    """真的把内核装起来（跳过外挂域与 MCP —— 那两样跟本节的断言无关）。

    ⚠️ 为什么不自己 `ToolRegistry()` 手搓一个：手搓的注册表**证明不了工具真的被注册过**
       —— 那正是本节要问的问题（"模型够得到吗"）。
       本项目最贵的一次教训就是这个形状：`set_slide_notes` 那次
       「能力不在工具集里」⇒ 一次任务 25 分钟零交付。
    """
    from rongke.config import Config as _Config
    _cfg = _Config.load(ROOT)
    return L.Rongke(cfg=_cfg, install_domain=False, use_memory=False,
                    install_mcp_servers=False).registry


def test_deck_reference() -> None:
    """§24 参考样例：`docs/deck-reference/`。

    ★ 这一节存在的理由：**参考样例的全部价值在于它与工具一致**。
      手工做一份、再手工写一段说明，两边的漂移没有任何东西会拦住。
      所以这里逐条钉死「落盘的东西 == 代码生成的东西」，
      以及**参考样例本身的体检必须全绿**（它示范的不能是"这样也算过"）。
    """
    section("24 参考样例（docs/deck-reference）")

    from rongke import deckref as R          # noqa: PLC0415
    DG = D.design
    py = _py_pptx()

    check("24.1 ★ 落盘的四件都在（成品 / 源稿 / 预览图 / 对照图）",
          [p.name for p in (R.REFERENCE_PPTX, R.REFERENCE_SRC_PPTX,
                            R.REFERENCE_SHEET_PNG, R.REFERENCE_COMPARE_PNG)
           if p.is_file()].__len__() == 4,
          f"{[p.name for p in (R.REFERENCE_PPTX, R.REFERENCE_SRC_PPTX, R.REFERENCE_SHEET_PNG, R.REFERENCE_COMPARE_PNG) if p.is_file()]}")

    check("24.2 ★ 六种页型**全都用到**（参考样例的意义就是六种都示范一遍）",
          set(R.kind_sequence()) == set(DG.KIND_NAMES),
          f"{sorted(set(R.kind_sequence()))} vs {sorted(DG.KIND_NAMES)}")
    check("24.3 每一页都有讲稿备注（组会 deck 没有备注就是半成品）",
          all(str(x.get("note") or "").strip() for x in R.REFERENCE_DECK),
          f"{[i for i, x in enumerate(R.REFERENCE_DECK, 1) if not str(x.get('note') or '').strip()]}")
    check("24.4 `REFERENCE_PAGES` 由 `REFERENCE_DECK` 派生（不是手抄的第二份）",
          R.REFERENCE_PAGES == [{"kind": x["kind"]} for x in R.REFERENCE_DECK], "")

    if not R.REFERENCE_PPTX.is_file():
        check("24.5 成品存在（缺了后面几条无法判）", False, "文件不在")
        return
    f = D.probe_pptx(R.REFERENCE_PPTX)
    check("24.5 成品页数 == `REFERENCE_DECK` 的长度（内容与产物同源）",
          f.slide_count == len(R.REFERENCE_DECK),
          f"{f.slide_count} vs {len(R.REFERENCE_DECK)}")
    check("24.6 成品是 16:9 且主题为 `juanqing`（精排真的落进去了）",
          abs(float(f.aspect or 0) - 16 / 9) < 0.01
          and f.theme_name() == R.REFERENCE_THEME,
          f"aspect={f.aspect} theme={f.theme_name()}")

    # ★ 最强的一条：读回页型标记，必须**逐页等于** `REFERENCE_PAGES`。
    #   这证明落盘的成品**真的是按这份页型序列精排出来的**，
    #   而不是"内容碰巧写在 REFERENCE_DECK 里"。标记是脚本自己钉的，
    #   靠位置反推不出来（见 `_kind_marks` 的注释）。
    marks = D._kind_marks(R.REFERENCE_PPTX)
    check("24.7 ★ 逐页页型标记 == `REFERENCE_PAGES`（成品确实按它排的）",
          sorted(marks.items()) == sorted(
              (i, k) for i, k in enumerate(R.kind_sequence(), 1)),
          f"{marks}")

    check("24.8 两个原生图表都还在（精排不丢视觉资产）",
          f.charts == 2, f"charts={f.charts} pictures={f.pictures}")
    check("24.9 备注覆盖率 100%（≥ 门槛 0.6）", f.notes_ratio >= 0.6,
          f"notes_ratio={f.notes_ratio}")

    # ★ 落盘的说明必须**逐字等于**代码生成的说明 —— 防漂。
    #   改了 `reference_text()` 而忘了重跑构建脚本 ⇒ 这里当场红。
    _md_now = R.reference_text(summary=False)
    _md_disk = (R.REFERENCE_MD.read_text(encoding="utf-8")
                if R.REFERENCE_MD.is_file() else "")
    check("24.10 ★ 落盘的 `.md` 逐字等于 `reference_text()`（没漂）",
          _md_disk.strip() == _md_now.strip(),
          f"盘上 {len(_md_disk)} 字符 vs 现在生成 {len(_md_now)} 字符")

    # ★ 参考样例**自己**必须过门。它示范的不能是"这样也算过"。
    #   ⚠️ W5 之后**版心利用率**是新增的一维，而参考样例的"空"正是工单 §〇 事实 B
    #   认定的**已知缺陷**（"文字极好、画面很空"）。工单 §4.4 明确：判据把第 4/12 页
    #   报出来**才是对的**，不许调阈值压掉。所以这里把"零提示"收窄成
    #   **"除 density 外零提示 + 无阻断"**，并把 density 对参考样例的判定**钉成正控**
    #   （见 §25.2）—— 参考样例仍是其余各维的合格示范，只是新维度上被如实记分。
    #   ⚠️ `report_path` 必须显式指向临时目录：审计器默认把原始 JSON 写到
    #     **pptx 所在目录**（`deck.py: raw_json = p.parent / "deck_audit.json"`）。
    #     不指的话本测试会往**随库发布的 `docs/deck-reference/` 里扔垃圾**
    #     —— 实测踩到过。通用纪律：**测试产出的东西，一律落在测试自己的临时目录里。**
    #   ⚠️ 过滤**按 check_id**，不按文案子串：`rep.warnings` 里每条的格式是
    #      `"    · <check_id> <message>"`，拿 "density" 去 substring 匹配**匹配不上**
    #      （门名不出现在字符串里）—— 上一版就是这么漏掉 density 的提示、白红一条。
    _aud_tmp = _TMPROOT / "ref24"
    _aud_tmp.mkdir(parents=True, exist_ok=True)
    rep = DeckAuditor(cfg=_Cfg()).audit(
        R.REFERENCE_PPTX, workdir=str(ROOT),
        report_path=_aud_tmp / "reference_v1.audit.json")
    _den_ids = {f.check_id for f in _gate(rep, "density").findings} | {"density"}
    _non_density_warn = [w for w in rep.warnings
                         if not any(i in w for i in _den_ids)]
    check("24.11 ★ 参考样例：除 density 外各门全绿、无阻断（density 是新维度、见 §25）",
          not rep.blockers and _non_density_warn == []
          and all(g.verdict == "PASS" for g in rep.gates if g.name != "density"),
          f"blockers={rep.blockers[:2]} warn={_non_density_warn[:2]} "
          f"gates={[(g.name, g.verdict) for g in rep.gates]}")
    _dg = _gate(rep, "density")
    _flagged = sorted(int(e.split("=")[1]) for fd in _dg.findings
                      for e in fd.evidence if e.startswith("slide="))
    # ★ W6 把「样例带病被门报出」的反证换成了**标杆档位守卫**：
    #   工单 §一 的档位（content≥28 / two-col≥30 / chart≥60 / closing≥35）
    #   逐页钉死；同时**一个阈值数字都不许动**（内核的 40% 通过线照旧，
    #   25–40% 仍会 WARN——那是主控单独挂起的另一决策，不在本守卫的射程内）。
    _W6_BAND = {"content": 0.28, "two-col": 0.30, "chart": 0.60, "closing": 0.35}
    _below = [(r["slide"], r["kind"], r["util"])
              for r in _dg.extra["density"]["per_slide"]
              if r["kind"] in _W6_BAND
              and (r["util"] is None or r["util"] < _W6_BAND[r["kind"]])]
    _hi_med = [fd.message for fd in _dg.findings
               if any(e.startswith("severity=") and e.split("=")[1] in ("high", "medium")
                      for e in fd.evidence)]
    check("24.11b ★ W6 标杆档位：逐页实测 util 够到本页型档位，且无 high/medium 档提示",
          not _below and not _hi_med,
          f"不达标={_below} high/medium={_hi_med[:2]} flagged={_flagged}")

    # ★ 幂等：再精排一遍仍应成立。**必须在副本上做** ——
    #   `compose_deck` 是就地改写，直接跑会动到随库发布的那份。
    import shutil as _sh
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        _cp = Path(td) / "ref_idem.pptx"
        _sh.copy2(R.REFERENCE_PPTX, _cp)
        _r2 = D.compose_deck(_cp, R.REFERENCE_PAGES,
                             theme=R.REFERENCE_THEME, footer=True,
                             footer_label=R.REFERENCE_FOOTER_LABEL,
                             workdir=Path(td), python_exe=py, cfg=_Cfg())
        _f2 = D.probe_pptx(_cp)
        check("24.12 ★ 幂等：对成品再精排一遍仍 `ok=True`"
              "（参考样例可以反复重建，不会越排越坏）",
              _r2.ok and _f2.slide_count == f.slide_count
              and _f2.charts == f.charts,
              f"ok={_r2.ok} slides={_f2.slide_count} charts={_f2.charts} "
              f"{_r2.problems[:1]}")

    _msg24 = R.reference_text(summary=True)
    check("24.13 给模型的那份（summary）短得多，且仍逐页带页型",
          len(_msg24) < len(_md_now) and "cover" in _msg24, f"{len(_msg24)}")

    # ══ 24.B 接线：模型读得到参考样例 ══════════════════════════
    #
    #  ★ 用户那句「人家同样是用 agent，只加个 skill 就能做出好看的图了」
    #    的另一半是**接线**：样例做得再好，模型调不到就等于没有。
    #    （本项目最贵的一次教训就是这个形状：`set_slide_notes` 那次
    #      「能力不在工具集里」⇒ 25 分钟零交付。）
    _reg = _real_registry()
    _tok = _reg.get("deck_reference")
    check("24.14 ★ `deck_reference` 已注册（样例必须在工具集里，否则它等于不存在）",
          _tok is not None, "没注册")
    if _tok is not None:
        check("24.15 且是**只读**的（取参考样例不该动盘上任何文件）",
              not bool(getattr(_tok, "mutating", False)), f"{getattr(_tok, 'mutating', None)}")
        check("24.16 且挂在 `ppt` 域（只有要产出 deck 的任务才付它的 schema 体积）",
              str(getattr(_tok, "domain", "")) == "ppt",
              f"{getattr(_tok, 'domain', None)}")
    else:
        check("24.15 且是**只读**的（取参考样例不该动盘上任何文件）", False, "没注册")
        check("24.16 且挂在 `ppt` 域（只有要产出 deck 的任务才付它的 schema 体积）",
              False, "没注册")

    _rec24 = "\n".join(C.DECK_TOOLCHAIN_RECIPE)
    check("24.17 ★ 配方**第一步**就是它（动手之前先看，否则等于没有）",
          "deck_reference" in C.DECK_TOOLCHAIN_RECIPE[0],
          f"{C.DECK_TOOLCHAIN_RECIPE[0][:60]}")
    check("24.18 ★ 且它排在 `create_presentation` 之前（先看再建）",
          _rec24.index("deck_reference") < _rec24.index("create_presentation"), "")
    check("24.19 ★ 两个渠道都点名（SYSTEM + 契约）",
          "deck_reference" in L.SYSTEM and "deck_reference" in _rec24,
          f"SYSTEM={'deck_reference' in L.SYSTEM}")
    check("24.20 负控：非 deck 任务读不到它（无差别加戏会污染别的任务）",
          "deck_reference" not in TaskContract(raw_goal="写一段产品说明",
                                               needs_deck=False).to_prompt(), "")

    # 工具**真的能调通**（不是"注册了就算"）—— 直接跑执行体。
    _out = L._make_deck_reference(_Cfg())()
    check("24.21 ★ 执行体真能跑通，且文本里带页型序列与六种页型",
          isinstance(_out, str) and "content" in _out and "two-col" in _out
          and "chart" in _out, f"len={len(_out) if isinstance(_out, str) else '? '}")
    check("24.22 ★ 且**如实说明模型看不见图**（不许假装读过预览图）",
          "读不了图" in _out or "看不见" in _out, _out[:120])
    check("24.23 默认（summary）比 detail=True 短，两条都通",
          len(L._make_deck_reference(_Cfg())()) < len(L._make_deck_reference(_Cfg())(True)),
          "")

    # ── 24.Z ★★ 解释器从哪来（一条"腰斩"级缺陷的守卫，2026-09-21 晚补）──────
    #   血债：全篇原写 `D.find_notes_python("")`，空串是 falsy ⇒ 候选表只剩
    #   "当前解释器" ⇒ 在 3.13 下 §21 把 None 塞进 subprocess **崩掉整个套件**，
    #   断言数 361 → 224，而运行器只会说"比基线少了"。
    cfg_py = ""
    try:
        cfg_py = json.loads((ROOT / "config.json").read_text(
            encoding="utf-8")).get("deck", {}).get("python", "") or ""
    except Exception:
        cfg_py = ""
    check("24.24 ★ config 里确实配着 `deck.python`（否则 24.25 判的就是空气）",
          bool(cfg_py), f"deck.python={cfg_py!r}")
    seen: list = []

    def _spy(explicit=None, *a, **k):
        seen.append(explicit)
        return "C:/___spy___.exe"

    with swapped(D, "find_notes_python", _spy):
        _py_pptx()
    check("24.25 ★★ 且 `_py_pptx()` 把**config 那个解释器显式传进去**"
          "（传空串 ⇒ 退化成「当前解释器」⇒ 换个解释器跑就找不到）",
          seen == [cfg_py], f"传进去的={seen!r}，config 里的是={cfg_py!r}")
    check("24.26 ★ 承重自检：`_py_pptx()` 用的是真函数（没被上面的探针粘住）",
          _py_pptx() == cfg_py or D.has_python_pptx(cfg_py or ""),
          f"_py_pptx()={_py_pptx()!r}")

    # ── 24.27 / 24.28 · W5 的密度标准必须**在模型读的那份里** ──────
    #   工单目标 2 要的是"写进模型读的那段文本"。写得再漂亮，
    #   如果只落在 `summary=False` 那一版（默认不给模型），等于没写。
    # ── 24.27 / 24.28 · W5 的密度标准必须**在模型读的那份里** ──────
    #   工单目标 2 要的是"写进模型读的那段文本"。写得再漂亮，
    #   如果只落在 `summary=False` 那一版（默认不给模型），等于没写。
    #   ⚠️ W6 起落点区间是**现算**的，这里不再硬抄某个具体数（如 "68–74%"）
    #      —— 抄一个数进测试，就是往测试里再埋一处 H 型。
    import re as _re24
    check("24.27 ★ 密度标准出现在**默认那份**（summary）里：档位、逐页型落点、反例都在",
          "版面密度" in _msg24 and "反例" in _msg24
          and "cover / section" in _msg24
          and _re24.search(r"`chart`\*\* \d+(–\d+)?%（\d+ 页）", _msg24) is not None,
          f"{_msg24[:80]}")
    with swapped(D, "DENSITY_LOW_MAX", 0.90):
        _msg_live = R.reference_text(summary=True)
    check("24.28 ★ 三个门槛数字是**从内核现读**的（改内核阈值 ⇒ 这份说明自己跟着变；"
          "在这儿硬抄一份就是 H 型）",
          "≥ 90% 通过" in _msg_live and "≥ 40% 通过" not in _msg_live,
          [ln for ln in _msg_live.split("\n") if "通过" in ln][:1])

    # ── 24.29–24.31 · W6 目标 2：两处「手写字符串」改派生 + 守卫 ──────
    #   血债前身：`DENSITY_PER_KIND` / `DENSITY_NEGATIVE_EXAMPLE` 手抄样例内容，
    #   没有任何断言钉住 —— 第 4 页一改好，它们立刻变成假话（H 型，两处真相）。
    #   W6 的处理是**改成派生**（单源），下面两条守卫钉住"派生没有退回手抄"。
    _pieces = [one.split("：", 1)[1] if "：" in one else one
               for one in R.DENSITY_NEGATIVE_EXAMPLE]
    _paras = {p for it in R.REFERENCE_DECK
              for ln in (it.get("lines") or []) for p in R._para_blocks(ln)}
    _titles = {str(it.get("title")) for it in R.REFERENCE_DECK}
    _neg_ok = (len(R.DENSITY_NEGATIVE_EXAMPLE) == 3
               and all(pc in _paras or pc in _titles for pc in _pieces))
    check("24.29 ★ 反例的每一行都**逐字出现在样例正文里**（派生成立、没漂成手抄）",
          _neg_ok, f"行={R.DENSITY_NEGATIVE_EXAMPLE}")

    def _neg_ok_now():
        _pr = {p for it in R.REFERENCE_DECK
               for ln in (it.get("lines") or []) for p in R._para_blocks(ln)}
        _tl = {str(it.get("title")) for it in R.REFERENCE_DECK}
        return (len(R.DENSITY_NEGATIVE_EXAMPLE) == 3
                and all((q.split("：", 1)[1] if "：" in q else q) in _pr
                        or (q.split("：", 1)[1] if "：" in q else q) in _tl
                        for q in R.DENSITY_NEGATIVE_EXAMPLE))

    _saved_ln = R._EXAMPLE_PAGE["lines"][0]
    try:
        R._EXAMPLE_PAGE["lines"][0] = str(_saved_ln).replace(
            str(_saved_ln).split("\n")[0],
            str(_saved_ln).split("\n")[0] + "X")
        _neg_live = _neg_ok_now()
    finally:
        R._EXAMPLE_PAGE["lines"][0] = _saved_ln
    check("24.29b ★ 负控：源页正文**改一个字** ⇒ 反例守卫当场失守（证明 24.29 不是永真式）",
          not _neg_live, "改了一个字，守卫居然还绿 ⇒ 断言没盯住正文")

    def _band_of(vals):
        lo, hi = min(vals) * 100, max(vals) * 100
        return ((f"{hi:.0f}%" if round(lo) == round(hi)
                 else f"{lo:.0f}–{hi:.0f}%") + f"（{len(vals)} 页）")

    _rows = {k: b for k, b, _ in R.density_per_kind()}
    _meas = R.measured_density() or {}
    check("24.30 ★ 密度表区间列**由成品 .pptx 现算**：测试独立重算对撞一致；"
          "手抄常量 `DENSITY_PER_KIND` 不许复活",
          "DENSITY_PER_KIND" not in vars(R)
          and bool(_meas)
          and all(_band_of(v) == _rows.get(k) for k, v in _meas.items())
          and _rows.get("cover / section") == "不量",
          f"现算={ {k: _band_of(v) for k, v in _meas.items()} } 表={_rows}")

    _counts = {}
    for it in R.REFERENCE_DECK:
        _counts[str(it["kind"])] = _counts.get(str(it["kind"]), 0) + 1
    check("24.31 密度表每行的页数 == `REFERENCE_DECK` 里该页型的页数（对账，不含漏量）",
          all(len(v) == _counts.get(k, 0) for k, v in _meas.items())
          and {"content", "two-col", "chart", "closing"} <= set(_meas),
          f"实测={ {k: len(v) for k, v in _meas.items()} } 应有={_counts}")


# ══════════════════════════════════════════════════════════════════
# 25 · 版心利用率（W5 · 2026-09-27）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 本节的夹具**与别处不一样**：`make_pptx` 那套一个 `a:xfrm` 都没有
#   （合成夹具连 layout / master 部件都不写 ⇒ 继承链推不出几何）。
#   那正好是 25.14 要钉的形状（量不到 ⇒ "没查"，不许读成"合规"），
#   但**证不了判据真能响**。要证"响"，必须自己造带几何的页 ⇒ `make_geo_pptx`。
#
# 三件必须钉住的事（工单 §三 + §四）：
#   ① 负控：明显空的一页**必须**报出来（§4.3）；
#   ② 承重自检：把阈值改成 0 ⇒ 负控当场失守（否则这条判据可能是 fixture 巧合）；
#   ③ 只 WARN 不阻断 + cover/section 豁免 + 页脚那类装饰不进并集（误报治理）。
_PX = 9525
_GEO_W, _GEO_H = 12192000, 6858000       # 与 `make_pptx` 同尺寸（16:9）
_GEO_TITLE_BOX = (72, 60, 1136, 60)      # px：标题带
_GEO_BODY_BOX = (72, 152, 1136, 472)     # px：`design` 那块**铺满内容带**的正文框


def _geo_sp(name: str, ph: str, box, paras, anchor: str = "") -> str:
    """带真实几何的文本形状。`box` 用 **px**，`paras = [(字号pt, 文字), …]`。

    一律**显式写 `sz`** —— 墨迹估算推不出字号时会退回整个框（保守那条路），
    那是 25.8 单独要测的，不混进本节其它用例。
    """
    x, y, cx, cy = (int(round(v * _PX)) for v in box)
    body = "".join('<a:p><a:r>'
                   + (f'<a:rPr sz="{int(round(pt * 100))}"/>' if pt else '<a:rPr/>')
                   + f'<a:t>{escape(t)}</a:t></a:r></a:p>' for pt, t in paras)
    ph_tag = f'<p:ph type="{ph}"/>' if ph else ""
    anc = f' anchor="{anchor}"' if anchor else ""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="{escape(name)}"/>'
            f'<p:nvPr>{ph_tag}</p:nvPr></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/>'
            f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm></p:spPr>'
            f'<p:txBody><a:bodyPr{anc}/><a:lstStyle/>{body}</p:txBody></p:sp>')


def _geo_pic(name: str, box) -> str:
    """一张**实心**图片（`p:pic`）—— 版心口径里"框即内容"的那一类。"""
    x, y, cx, cy = (int(round(v * _PX)) for v in box)
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="9" name="{escape(name)}"/>'
            f'<p:cNvPicPr><p:picLocks noChangeAspect="1"/></p:cNvPicPr>'
            f'<p:nvPr/></p:nvPicPr><p:blipFill/>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/>'
            f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm></p:spPr></p:pic>')


def _geo_frame(name: str, box) -> str:
    """一个 `graphicFrame`（几何挂在 `p:xfrm` 上，与 `sp` 不同路径）。"""
    x, y, cx, cy = (int(round(v * _PX)) for v in box)
    return (f'<p:graphicFrame><p:nvGraphicFramePr>'
            f'<p:cNvPr id="10" name="{escape(name)}"/>'
            f'<p:nvGraphicFramePr/></p:nvGraphicFramePr>'
            f'<p:xfrm><a:off x="{x}" y="{y}"/>'
            f'<a:ext cx="{cx}" cy="{cy}"/></p:xfrm>'
            f'<a:graphic><a:graphicData/></a:graphic></p:graphicFrame>')


def make_geo_pptx(path, slides) -> Path:
    """造一份**带几何**的最小 .pptx。

    `slides`：`{"kind": 页型或 None, "shapes": [xml, …]}`。
    `kind` 靠 `rk-kind-<kind>` 标记形状表达 —— 与 `compose_deck` 的产物同一口径
    （判据读的就是那个标记，不是"猜页型"）。标记框**给满页**：
    它若没被排除掉，利用率会被顶成假绿 ⇒ 排除那条就白写。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ppt/presentation.xml", _xml(
            f'<p:sldSz cx="{_GEO_W}" cy="{_GEO_H}"/>', "p:presentation"))
        for i, s in enumerate(slides, 1):
            parts = list(s.get("shapes") or [])
            if s.get("kind"):
                parts.append(_geo_sp(f"rk-kind-{s['kind']}", "",
                                     (0, 0, 1280, 720), [(12.0, s["kind"])]))
            z.writestr(f"ppt/slides/slide{i}.xml",
                       _xml(f"<p:cSld><p:spTree>{''.join(parts)}</p:spTree></p:cSld>",
                            "p:sld"))
    return p


def _geo_page(kind, bullets, *, pt=18.0, chars=40, title_chars=8, shapes=()):
    """一页「标题 + 铺满内容带的正文框 + `bullets` 条要点」。

    ★ 正文框**故意铺满**（`_GEO_BODY_BOX` = `compose_deck` 的真实做法）。
      于是"按框量"每页都 ≈88% —— 这就是工单那条定义在本库会失效的根因，
      也是 25.7 那一对 `util` / `util_box` 要当面量出来的东西。
    """
    sh = [_geo_sp("ttl", "title", _GEO_TITLE_BOX,
                  [(28.0, "标" * title_chars)]),
          _geo_sp("bdy", "body", _GEO_BODY_BOX,
                  [(pt, "要" * chars) for _ in range(bullets)])]
    sh += list(shapes)
    return {"kind": kind, "shapes": sh}


def _rects_of(shapes):
    """直接在内存里拼一页，喂 `_slide_content_rects`（不走 zip，测函数本体）。"""
    from xml.etree import ElementTree as _ET
    return D._slide_content_rects(_ET.fromstring(
        _xml("<p:cSld><p:spTree>" + "".join(shapes) + "</p:spTree></p:cSld>",
             "p:sld")))


def _den(tmp, name, slides):
    """端到端：造页 → `probe_pptx` → `_gate_density`。"""
    return _den_gate(make_geo_pptx(tmp / name, slides))


def _den_gate(p):
    return _aud()._gate_density(probe_pptx(Path(p)))


def _den_pages(g: GateResult) -> list[int]:
    """这道门报了的页码（从 evidence 里取，不靠文案措辞）。"""
    return sorted(int(e.split("=")[1]) for fd in g.findings
                  for e in fd.evidence if e.startswith("slide="))


def _den_first(g: GateResult):
    for fd in g.findings:
        return fd
    return None


def _den_sev(fd) -> str:
    """从 evidence 里取 `severity=`（取不到 ⇒ 空串，不抛 —— 见 §25 顶上那条注释）。"""
    if fd is None:
        return ""
    for e in fd.evidence:
        if e.startswith("severity="):
            return e.split("=", 1)[1]
    return ""


def test_density_gate() -> None:
    section("25 版心利用率（W5）")
    tmp = T.fresh_dir(_TMPROOT / "den", "deck-den")

    # ── ① 负控（工单 §4.3）：一眼就空的一页，必须响 ──────────────
    g_empty = _den(tmp, "empty.pptx", [_geo_page("content", 1, chars=12)])
    fd = _den_first(g_empty)
    util0 = g_empty.extra["density"]["per_slide"][0]["util"]
    check("25.1 ★ 负控（工单 §4.3）：一条要点的 content 页**必须报**，且档位是 high"
          f"（实测墨迹利用率 {util0 * 100:.1f}%）",
          g_empty.verdict == "REVIEW REQUIRED" and _den_pages(g_empty) == [1]
          and fd is not None and fd.level == "WARN"
          and any("severity=high" in e for e in fd.evidence),
          f"verdict={g_empty.verdict} pages={_den_pages(g_empty)} "
          f"ev={fd.evidence if fd else None}")

    # ── ② 正控：吃满版面的一页，不许响（只会报警的守卫和不会报警的一样没用）──
    g_pic = _den(tmp, "pic.pptx",
                 [_geo_page("chart", 2, chars=20,
                            shapes=[_geo_pic("shot", (72, 100, 1136, 520))])])
    util_pic = g_pic.extra["density"]["per_slide"][0]["util"]
    check(f"25.2 ★ 正控：一张吃满版面的图 + 两条读数 ⇒ 不报（实测 {util_pic * 100:.1f}%）",
          g_pic.verdict == "PASS" and g_pic.findings == [] and util_pic >= D.DENSITY_LOW_MAX,
          f"verdict={g_pic.verdict} util={util_pic}")

    # ── ③ 档位实测：三要点页落在哪一档（写进执行报告 §五，不为了绿去动阈值）──
    g_mid = _den(tmp, "mid.pptx", [_geo_page("content", 3)])
    fd_mid = _den_first(g_mid)
    util_mid = g_mid.extra["density"]["per_slide"][0]["util"]
    check(f"25.3 参考样例密度的 content 页（3 条 × 40 字 18pt）实测 {util_mid * 100:.1f}%"
          " ⇒ 落在 medium（工单四档在本库偏严，§五如实报，阈值未动）",
          util_mid is not None and _den_sev(fd_mid) == "medium",
          f"util={util_mid} ev={fd_mid.evidence if fd_mid else None}")

    # ── ④ 页型豁免 + 「读不到页型一律不豁免」────────────────────
    g_sec = _den(tmp, "sec.pptx", [_geo_page("section", 1, chars=12),
                                   _geo_page("cover", 1, chars=12)])
    check("25.4 ★ cover / section 同样只有一行字 ⇒ **不报**（留白是刻意的，工单 §一）",
          g_sec.verdict == "PASS" and g_sec.findings == []
          and g_sec.extra["density"]["exempt"] == 2,
          f"verdict={g_sec.verdict} exempt={g_sec.extra['density']['exempt']}")
    g_unknown = _den(tmp, "unknown.pptx", [_geo_page(None, 1, chars=12)])
    check("25.5 ★ 页型读不到（`kind=\"\"`）⇒ **不豁免** —— 不知道页型不能当成可以溜",
          _den_pages(g_unknown) == [1], f"pages={_den_pages(g_unknown)}")

    # ── ⑤ 页脚装饰线 / 页码 / 落款 / 页型标记：不进并集 ───────────
    _chrome = [_geo_sp("juanqing-footer-label", "", (72, 690, 1136, 600),
                       [(12.0, "落款文字")]),
               _geo_sp("pageno", "sldNum", (1100, 690, 100, 30), [(12.0, "1")]),
               _geo_sp("rule", "", (72, 686, 1136, 2), [])]
    g_plain = _den(tmp, "plain.pptx", [_geo_page("content", 1, chars=12)])
    g_chrome = _den(tmp, "chrome.pptx",
                    [_geo_page("content", 1, chars=12, shapes=_chrome)])
    _u1 = g_plain.extra["density"]["per_slide"][0]["util"]
    _u2 = g_chrome.extra["density"]["per_slide"][0]["util"]
    check("25.6 ★ 页码 / 落款 / 无字装饰线（各给**满页框**）都不进并集"
          "（漏掉任何一条 ⇒ 利用率被顶成假绿）",
          _u1 == _u2 and _den_pages(g_chrome) == [1],
          f"带装饰={_u2} vs 不带={_u1}")

    # ── ⑦ 墨迹 vs 形状框：这条判据在本库为什么必须量墨迹 ──────────
    s0 = g_empty.extra["density"]["per_slide"][0]
    check("25.7 ★ 同一页两个数分开存：`util`（墨迹）远小于 `util_box`（形状框）——"
          "按框量这条判据**永远不会响**（正文框本来铺满内容带）",
          s0["util"] < 0.10 and s0["util_box"] > 0.70,
          f"util={s0['util']} util_box={s0['util_box']}")
    _ink_nofont, _box_nofont, _ = _rects_of(
        [_geo_sp("nofont", "body", _GEO_BODY_BOX, [(None, "无字号的一行")])])
    check("25.8 有字却推不出字号 ⇒ 墨迹**回退成整个框**（宁可少报空页，不误报）",
          len(_ink_nofont) == 1 and _ink_nofont == _box_nofont
          and _box_nofont[0] == (_GEO_BODY_BOX[0] * _PX, _GEO_BODY_BOX[1] * _PX,
                                 _GEO_BODY_BOX[2] * _PX, _GEO_BODY_BOX[3] * _PX),
          f"ink={_ink_nofont} box={_box_nofont}")

    # ── ⑨ pic / graphicFrame 取整框；grpSp 不递归（已知局限）───────
    _ink_pic, _box_pic, _ = _rects_of([_geo_pic("shot", (100, 100, 400, 300))])
    _ink_fr, _box_fr, _ = _rects_of([_geo_frame("frame", (640, 120, 400, 300))])
    check("25.9 图片与 graphicFrame ⇒ 墨迹=框=整框（实心内容）；两者几何路径不同"
          "（`a:xfrm` vs `p:xfrm`）都要量到",
          _ink_pic == _box_pic and _ink_fr == _box_fr
          and _ink_pic[0] == (int(100 * _PX), int(100 * _PX), int(400 * _PX),
                              int(300 * _PX))
          and _ink_fr[0] == (int(640 * _PX), int(120 * _PX), int(400 * _PX),
                             int(300 * _PX)),
          f"pic={_ink_pic} gf={_ink_fr}")
    _ink_grp, _box_grp, _n_grp = _rects_of(
        ['<p:grpSp><p:nvGrpSpPr><p:cNvPr id="4" name="grp"/><p:nvPr/>'
         '</p:nvGrpSpPr><p:grpSpPr><a:xfrm>'
         '<a:off x="685800" y="0"/><a:ext cx="10820400" cy="6286500"/>'
         f'</a:xfrm></p:grpSpPr>{_geo_sp("in", "body", _GEO_BODY_BOX, [(18.0, "字")])}'
         '</p:grpSp>'])
    check("25.10 ★ 已知局限钉成断言：组合形状（`grpSp`）**不递归** ⇒ 整组跳过、如实少计"
          "（不是静默算错位置；执行报告 §五写这条边界）",
          _ink_grp == [] and _box_grp == [] and _n_grp == 0,
          f"ink={_ink_grp} box={_box_grp} n={_n_grp}")

    # ── ⑪ 并集 ≠ 面积和；量不到 vs 真的空 分开 ────────────────────
    us = D._usable_region(_GEO_W, _GEO_H)
    ux0, uy0, ux1, uy1 = us
    uw, uh = ux1 - ux0, uy1 - uy0
    left = (ux0, uy0, uw / 2.0, uh)
    right = (ux0 + uw / 2.0, uy0, uw / 2.0, uh)
    u_one = D._util_from_rects([left], us)
    u_dup = D._util_from_rects([left, left], us)
    u_both = D._util_from_rects([left, right], us)
    check("25.11 ★ 并集不是面积求和：同一矩形数两遍不变；两块不重叠才翻倍"
          "（投影法，每轴 50 格 = 2%）",
          u_dup == u_one and u_both > u_one * 1.8 and u_both <= 1.0,
          f"one={u_one} dup={u_dup} both={u_both}")
    check("25.12 ★ 一个矩形都没量到 ⇒ `None`（「没查」），完全在版面外 ⇒ `0.0`（真空）"
          " —— M 型：两回事不许压成一个数",
          D._util_from_rects([], us) is None
          and D._util_from_rects([(-10 ** 7, -10 ** 7, 100.0, 100.0)], us) == 0.0
          and D._util_from_rects([left], None) is None,
          f"[]={D._util_from_rects([], us)} "
          f"越界={D._util_from_rects([(-10 ** 7, -10 ** 7, 100.0, 100.0)], us)}")

    # ── ⑬ 四档单调 ──────────────────────────────────────────────
    check("25.13 严重度四档单调：<15 high、<25 medium、<40 low、≥40 不报",
          [D._density_severity(x) for x in (0.05, 0.149, 0.20, 0.249,
                                            0.30, 0.399, 0.40, 0.95)]
          == ["high", "high", "medium", "medium", "low", "low", "", ""],
          f"{[D._density_severity(x) for x in (0.05, 0.20, 0.30, 0.40)]}")

    # ── ⑭ finding 五要素（工单 §一：页码/页型/百分比/档位/一句修法）──
    #   ⚠️ `fd` 在"判据被改坏"的那一轮里是 None —— 本节所有取值都不许裸取，
    #      否则注入故障的那一轮会**抛异常**（红得看不下去 ≠ 红得说明问题）。
    _msg = fd.message if fd else ""
    check("25.14 ★ 每条 finding 五要素齐：页码、页型、利用率（一位小数百分比）、"
          "档位、**一句能照着做的修法**",
          "第 1 页" in _msg and "content" in _msg
          and f"{util0 * 100:.1f}%" in _msg and "high" in _msg
          and "——" in _msg and len(_msg.split("——")[-1].strip()) > 10
          and {"slide=1", "kind=content", "severity=high"} <= set(fd.evidence)
          and any(e.startswith("util=") for e in fd.evidence),
          _msg)
    _fixes = {k: D._density_fix(k, 1) for k in
              ("content", "two-col", "chart", "closing", "")}
    check("25.15 修法**逐页型不同**，且把实测条数说进去（不是同一句万能话）",
          len(set(_fixes.values())) == 5 and "1 条" in _fixes["content"],
          f"{ {k: v[:18] for k, v in _fixes.items()} }")

    # ── ⑯ 只 WARN、永不阻断（工单硬约束）────────────────────────
    g_4 = _den(tmp, "allwarn.pptx", [_geo_page("content", 1, chars=12)] * 4)
    check("25.16 ★ 四页全空 ⇒ density 最多把 verdict 拉到 REVIEW REQUIRED："
          "finding 一律 WARN、门本身永不进 blocking（定律 10）",
          g_4.verdict == "REVIEW REQUIRED" and not g_4.blocking
          and len(g_4.findings) == 4
          and {f.level for f in g_4.findings} == {"WARN"}
          and "FIX BEFORE DELIVERY" not in g_4.verdict,
          f"verdict={g_4.verdict} blocking={g_4.blocking} "
          f"levels={{{[f.level for f in g_4.findings]}}}")

    # ── ⑰ 老夹具（无几何）⇒ 没查，不许读成合规 ──────────────────
    g_nogeo = _aud()._gate_density(probe_pptx(
        make_pptx(tmp / "nogeo.pptx", green_slides(3, titles=GOOD_TITLES[:3]))))
    check("25.17 ★ 一页几何都量不到（合成夹具无 `a:xfrm`）⇒ 出 `SKIP` 且写明"
          "**没查**；verdict 仍是 PASS（不进 blocking ⇒ 不会卡死所有 deck）",
          g_nogeo.verdict == "PASS" and not g_nogeo.blocking
          and [f.check_id for f in g_nogeo.findings] == ["density_unmeasurable"]
          and "没查" in g_nogeo.findings[0].message,
          f"verdict={g_nogeo.verdict} findings={[f.check_id for f in g_nogeo.findings]}")

    _x = g_empty.extra["density"]
    check("25.18 extra 里逐页数字都在（页码/页型/util/util_box）+ 阈值原样回显"
          " —— 主控要**独立复算**比对，光有结论文字对不上账",
          len(_x["per_slide"]) == 1 and _x["measured"] == 1
          and _x["per_slide"][0]["slide"] == 1
          and _x["per_slide"][0]["kind"] == "content"
          and _x["thresholds"] == {"high_lt": D.DENSITY_HIGH_MAX,
                                   "medium_lt": D.DENSITY_MEDIUM_MAX,
                                   "low_lt": D.DENSITY_LOW_MAX},
          f"{_x}")

    # ── ⑲ ★★ 承重自检：把阈值改成 0 ⇒ 负控必须当场失守 ──────────
    p_sab = make_geo_pptx(tmp / "sab.pptx", [_geo_page("content", 1, chars=12)])
    check("25.19 前置：不注入故障时，这一页确实报出来",
          _den_gate(p_sab).verdict == "REVIEW REQUIRED", "")
    with swapped(D, "DENSITY_HIGH_MAX", 0.0), \
         swapped(D, "DENSITY_MEDIUM_MAX", 0.0), \
         swapped(D, "DENSITY_LOW_MAX", 0.0):
        g_sab = _den_gate(p_sab)
        check("25.20 ★★ 注入故障：三档阈值→0 ⇒ 同一页**一条都不报**"
              "（工单 §4.3 反过来说：把判据改坏 ⇒ 25.1 那个负控用例必须变红 —— "
              "响的是判据，不是夹具的巧合）",
              g_sab.verdict == "PASS" and g_sab.findings == [],
              f"verdict={g_sab.verdict} findings={[f.message for f in g_sab.findings]}")
    g_back = _den_gate(p_sab)
    check("25.21 复原后同一页又被报出来（阈值真的参与判定，探针没粘住）",
          g_back.verdict == "REVIEW REQUIRED" and _den_pages(g_back) == [1],
          f"verdict={g_back.verdict} pages={_den_pages(g_back)}")
    with swapped(D, "_usable_region", lambda cx, cy: None):
        g_none = _den(tmp, "none.pptx", [_geo_page("content", 1, chars=12)])
        check("25.22 ★ 把可用版面换成「算不出来」⇒ 全篇变「没查」，"
              "而不是变「0% ⇒ high」—— 估算器失能不许冒充成结论（M 型）",
              [f.check_id for f in g_none.findings] == ["density_unmeasurable"]
              and g_none.verdict == "PASS",
              f"{[(f.check_id, f.message[:24]) for f in g_none.findings]}")

    # ── ⑳ 分母只有一套边距（H 型守卫）──────────────────────────
    DG = D.design
    check("25.23 ★ 分母**只从 `design.py` 取数**：手算 `PAD_PX`/`BANDS_PX` 必须逐位相等"
          "（另起一套边距 = 两套真相 = H 型）",
          us == (DG.PAD_PX / DG.CANVAS_PX[0] * _GEO_W, 0.0,
                 _GEO_W - DG.PAD_PX / DG.CANVAS_PX[0] * _GEO_W,
                 DG.BANDS_PX["B"][1] / DG.CANVAS_PX[1] * _GEO_H), f"{us}")
    margin = (0.0, 0.0, ux0, uh)            # 正好落在**左页边距带**里的一块
    with swapped(DG, "PAD_PX", 0):
        _us0 = D._usable_region(_GEO_W, _GEO_H)
        _u_marg = D._util_from_rects([margin], _us0)
    check("25.24 且分母真的在参与判定：落在边距带里的版面 ⇒ 原口径下 0%"
          "（边距不算可填版面）；把 `PAD_PX` 收到 0 ⇒ 立刻有数",
          D._util_from_rects([margin], us) == 0.0 and 0.0 < _u_marg < 0.10,
          f"原={D._util_from_rects([margin], us)} PAD=0 ⇒ {_u_marg} region={_us0}")
    check("25.25 复原后分母回来（探针没粘住）",
          D._usable_region(_GEO_W, _GEO_H) == us, "")

    T.stash(tmp, "deck-den")


# ══════════════════════════════════════════════════════════════════
# 26 · 工具接缝冒烟（W7 · 2026-09-27）★★ 这类守卫比修 bug 更重要
# ══════════════════════════════════════════════════════════════════
#
# ★ 为什么补这一节（真实缺陷，不是假想）：
#
#   `rongke/loop.py::_make_deck_from_template` 里调了 `template_name_of(cfg)`，
#   而那一行的 import 语句**漏了这个名字** ⇒ **该工具每次调用都抛 `NameError`**，
#   100% 不可用。而 `regress.py --only deck` **396 项全绿**。
#
#   为什么全绿也发现不了：在这之前，**整个套件直接测 `deck.py` 的函数**，
#   从不经过 `loop.py` 那一层**薄包装**。
#   ⇒ 库 100% 健康，而**模型实际够到的那一层是坏的**。
#   ⇒ "测了库、没测接缝"：**门全绿而工具不可用**。
#
#   而它偏偏是最该好的那个 —— 它的工具描述原文写着
#   「这是**第一选择** …… `compose_deck` / `style_deck` 是**兜底**」。
#   模型会优先调它、拿到 `NameError`、然后退到兜底路径，
#   于是交付出去的是"近似品"而不是真正的品牌模板。
#
# ★ 判据是**类别**的，不是"`template_name_of` 这个字面量"：
#   **`NameError` 永远是代码缺陷，不是用户输入问题。**
#   ⇒ 拿最小参数调每个工具，**只把 `NameError` 当失败**；
#     其余异常（参数缺失、文件不存在、模板没配）都是**正常业务拒绝**。
#   好处：下次谁再漏一个 import，不管哪个工具哪个名字，这一条都会红。
#   **修一行只解决今天，补守卫解决这一类。**

_DECK_TOOL_FACTORIES = (
    ("_make_deck_from_template", "deck_from_template", "品牌模板出稿（第一选择）"),
    ("_make_compose_deck", "compose_deck", "设计资产精排"),
    ("_make_style_deck", "style_deck", "快套主题"),
    ("_make_audit_deck", "audit_deck", "体检"),
    ("_make_set_slide_notes", "set_slide_notes", "讲稿备注"),
    ("_make_deck_reference", "deck_reference", "参考样例（只读）"),
)


def _find_unbound_names(fac) -> list:
    """静态找出 `fac` 引用不到的名字（空 = 干净）。

    ★ 必须**保留闭包作用域**：工厂体的形状是
        `def _make_x(cfg):        # ← 外层参数 cfg
             def x(...):          # ← 内层函数闭包引用 cfg
                 ...`
    只扫内层函数、丢掉外层参数，就会把合法的闭包变量 `cfg` 报成"未绑定"。
    主控第一版就是这么错的 —— **新写的守卫必须先拿没改过的代码跑一遍**，
    否则报的红是守卫自己，不是被检查物。
    """
    import ast
    import builtins
    import textwrap

    import rongke.loop as _mod

    src_all = open(_mod.__file__, encoding="utf-8").read()
    want = fac.__code__.co_firstlineno
    node0 = None
    for node in ast.walk(ast.parse(src_all)):
        if isinstance(node, ast.FunctionDef) and node.lineno == want:
            node0 = node
            break
    if node0 is None:
        return []
    src = textwrap.dedent(ast.get_source_segment(src_all, node0) or "")
    if not src:
        return []
    g = set(dir(_mod)) | set(dir(builtins))
    tree = ast.parse(src)
    fn_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    # ★ 外层工厂的参数/局部名，内层闭包看得见 ⇒ 并入可见集
    outer: set = set()
    if fn_nodes:
        top = fn_nodes[0]
        for a in top.args.args + top.args.kwonlyargs:
            outer.add(a.arg)
        if top.args.vararg:
            outer.add(top.args.vararg.arg)
        if top.args.kwarg:
            outer.add(top.args.kwarg.arg)
        for sub in ast.walk(top):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                outer.add(sub.id)
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    outer.add(a.asname or a.name.split(".")[0])
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                outer.add(sub.name)
    out = []
    for node in fn_nodes:
        local = set(outer)
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    local.add(a.asname or a.name.split(".")[0])
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                local.add(sub.name)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                local.add(sub.id)
            if isinstance(sub, ast.arg):
                local.add(sub.arg)
            if isinstance(sub, ast.ExceptHandler) and sub.name:
                local.add(sub.name)
            if isinstance(sub, ast.comprehension) and isinstance(sub.target, ast.Name):
                local.add(sub.target.id)
        used = {s.id for s in ast.walk(node)
                if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Load)}
        out.extend(sorted(used - local - g))
    return sorted(set(out))


def _minimal_call_args(fn) -> tuple:
    """按签名给最小位置参数（空串）⇒ 走到"文件不存在"那类业务拒绝。"""
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return ()
    out = []
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) or p.default is not p.empty:
            continue
        out.append("")
    return tuple(out)


def test_tool_seam_smoke():
    """每个 deck 工具的工厂：① 存在 ② 无未绑定名 ③ 真调不抛 NameError。"""
    from rongke import loop as _L

    for factory_name, tool_name, what in _DECK_TOOL_FACTORIES:
        fac = getattr(_L, factory_name, None)
        check(f"26.1 `{factory_name}` 存在（{what}）", fac is not None,
              f"loop.py 里没有 {factory_name}")
        if fac is None:
            continue

        # ① 静态：不需要真跑 ⇒ 连"参数不对走不到那行"的漏网也抓得住
        try:
            unbound = _find_unbound_names(fac)
        except Exception as e:                              # noqa: BLE001
            unbound = [f"<静态扫描失败 {type(e).__name__}>"]
        check(f"26.2 `{factory_name}` 无未绑定名字（NameError 类缺陷的根因）",
              not unbound,
              f"引用了无法解析的名字 {unbound} ⇒ `{tool_name}` 一调就崩。"
              f"补 import 或改局部变量名。")

        # ② 动态：真调。只有 NameError 算失败
        try:
            fn = fac(_Cfg())
        except NameError as e:
            check(f"26.3 `{factory_name}` 造得出工具（无 NameError）", False,
                  f"{type(e).__name__}: {e}")
            continue
        except Exception:                                   # noqa: BLE001
            continue                     # 需要真 cfg 才行 ⇒ 只信静态那条
        try:
            fn(*_minimal_call_args(fn))
            err = None
        except NameError as e:
            err = f"{type(e).__name__}: {e}"
        except Exception:                                   # noqa: BLE001
            err = None                   # 业务拒绝
        check(f"26.3 `{tool_name}` 被调用时不抛 NameError", err is None,
              f"{err} —— `NameError` 一律是代码缺陷（漏 import / 拼错名），"
              f"不是用户输入问题。")


# ══════════════════════════════════════════════════════════════════
# 27 · 对比度门（W8 · 2026-09-27）
# ══════════════════════════════════════════════════════════════════
#
# ★ 这一维补的是**看得见吗**：前面六道门全部只读 XML 的结构事实，
#   没有任何一维在问"这行字压在什么上面、衬不衬得出来"。
#
# 工单 §五 的六条逐条对着写，三条最要紧的写在断言名里：
#   ① **负控（27.3）最重要** —— 新判据必须先拿"已经正确的输入"跑一遍。
#      本项目的血债：自建守卫一半是假警（`learned-pitfall-selfbuilt-guard-false-positives`）。
#      所以除了白底黑字，27.14 还拿**随库发布的参考样例**当负控。
#   ② 「没查」绝不允许读成「通过」（27.4 / 27.5 / 27.12 / 27.13）。
#   ③ 纯 XML 抓不到"字压在照片上" —— **如实报量不到本身就是交付物**（27.4）。
#
# ⚠️ 与工单 §三②.5 的**一处有意偏离**（写在 `deck.py` 的门里，也在 27.5 钉住）：
#   一条都没量到时用 `NOT APPLICABLE` + 一条 `SKIP` finding，**不用** `SKIPPED`。
#   原因：`GateResult.blocking` 把 `SKIPPED` 算成阻断，而"字色继承母版、XML 里没写"
#   是 python-pptx 产物的常态 ⇒ 一旦 SKIPPED，凡是没逐 run 写死颜色的 deck 都会
#   永久 `FIX BEFORE DELIVERY`，模型怎么改都过不去 —— 那就是 notes 门那条
#   "纯 MCP 路径永远过不了 = 死结"（§3.10）换了个地方重演。
#   ⇒ 27.5 两头都钉：**不是 PASS**（诚实）+ **不阻断**（不死结）。
_PX_EMU = 9525


def _ct_box(box) -> str:
    x, y, cx, cy = (int(round(v * _PX)) for v in box)
    return (f'<a:xfrm><a:off x="{x}" y="{y}"/>'
            f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm>')


def _ct_sp(name: str, box, runs, fill: str = "", ph: str = "") -> str:
    """带**几何与字色**的文本形状。`runs = [(pt, 文字, 字色或 None, 是否粗体)]`。

    `字色=None` ⇒ 不写 `a:solidFill` = 继承母版 ⇒ 门必须报"量不到"，
    **不许**拿白色顶上去（工单 §四第 5 条）。
    `fill` 给值 ⇒ 这个形状自己涂了色（深色圆角矩形里写白字那种日常排版）。
    `ph` 给值 ⇒ 挂占位符类型（`sldNum`/`ftr`/`dt` 那类页脚 chrome 靠它认）。
    """
    body = "".join(
        '<a:p><a:r><a:rPr{}{}>{}</a:rPr><a:t>{}</a:t></a:r></a:p>'.format(
            f' sz="{int(round(pt * 100))}"' if pt else "",
            ' b="1"' if bold else "",
            (f'<a:solidFill><a:srgbClr val="{hexc}"/></a:solidFill>' if hexc else ""),
            escape(txt))
        for pt, txt, hexc, bold in runs)
    own = (f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>' if fill else "")
    pht = f'<p:ph type="{ph}"/>' if ph else ""
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="{escape(name)}"/>'
            f'<p:nvPr>{pht}</p:nvPr></p:nvSpPr>'
            f'<p:spPr>{_ct_box(box)}{own}</p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>')


def _ct_rect(name: str, box, fill: str) -> str:
    """**没有文字**的实心形状（色块 / 装饰矩形）。`fill` = `"RRGGBB"` 或 `"grad"`。"""
    if fill == "grad":
        fl = ('<a:gradFill><a:gsLst>'
              '<a:gs pos="0"><a:srgbClr val="1F3864"/></a:gs>'
              '<a:gs pos="100000"><a:srgbClr val="064178"/></a:gs>'
              '</a:gsLst><a:lin ang="5400000" scaled="0"/></a:gradFill>')
    else:
        fl = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="7" name="{escape(name)}"/>'
            f'<p:nvPr/></p:nvSpPr>'
            f'<p:spPr>{_ct_box(box)}{fl}</p:spPr>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/></p:txBody></p:sp>')


def _ct_theme_xml(slots) -> str:
    """一份最小 `theme1.xml`。值写成 `("sys", "windowText")` 造**默认 Office 主题**
    那种"槽位里是系统色**名字**、不是色值"的页 —— 那条路必须量不到。"""
    parts = []
    for name, v in slots.items():
        inner = (f'<a:sysClr val="{v[1]}"/>' if isinstance(v, tuple)
                 else f'<a:srgbClr val="{v}"/>')
        parts.append(f'<a:{name}>{inner}</a:{name}>')
    return _xml('<a:themeElements><a:clrScheme name="T">'
                + "".join(parts) + "</a:clrScheme></a:themeElements>", "a:theme")


def make_ct_pptx(path, slides, theme=None) -> Path:
    """带几何 + 颜色的页。`slides` = 每项一页（形状 xml 的列表）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ppt/presentation.xml", _xml(
            f'<p:sldSz cx="{_GEO_W}" cy="{_GEO_H}"/>', "p:presentation"))
        for i, shapes in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml",
                       _xml("<p:cSld><p:spTree>" + "".join(shapes)
                            + "</p:spTree></p:cSld>", "p:sld"))
        if theme:
            z.writestr("ppt/theme/theme1.xml", _ct_theme_xml(theme))
    return p


def _ct(tmp, name, slides, theme=None) -> GateResult:
    return _aud()._gate_contrast(probe_pptx(make_ct_pptx(tmp / name, slides, theme)))


def _ct_rows(g: GateResult) -> list[dict]:
    return g.extra["contrast"]["per_slide"]


def _ct_low(g: GateResult) -> list:
    return [f for f in g.findings if f.check_id == "text_low_contrast"]


def _ct_unmeas(g: GateResult) -> list:
    return [f for f in g.findings if f.check_id == "contrast_unmeasurable"]


def _ct_ev(fd, key: str) -> str:
    for e in fd.evidence:
        if e.startswith(key + "="):
            return e.split("=", 1)[1]
    return ""


def _ct_levels(g: GateResult) -> set:
    return {f.level for f in g.findings}


def test_contrast_gate() -> None:
    section("27 对比度门（W8）")
    tmp = T.fresh_dir(_TMPROOT / "ct", "deck-ct")
    BAND = (0, 0, 1280, 720)                     # 铺满一页的下层色块
    TXT = (100, 100, 600, 80)                    # 文字框（整块都在 BAND 里）

    # ── 27.1 正控（该报就报）：深色填充上的深蓝字，工单给的那对色 ──────
    g1 = _ct(tmp, "low.pptx", [[_ct_rect("band", BAND, "1F3864"),
                                 _ct_sp("TextBox 42", TXT,
                                        [(12.0, "适宜温度报道分散在 4–15℃", "064178", False)])]],
              theme={"lt1": "FFFFFF", "dk1": "000000"})
    low = _ct_low(g1)
    check("27.1 ★ 正控：深色填充上的深蓝字**必须报** `text_low_contrast`"
          f"（实测 {(_ct_ev(low[0], 'ratio') if low else '—')}:1）",
          g1.verdict == "REVIEW REQUIRED" and len(low) == 1
          and low[0].level == "WARN"
          and _ct_ev(low[0], "slide") == "1"
          and _ct_ev(low[0], "fg") == "064178"
          and _ct_ev(low[0], "bg") == "1F3864"
          and _ct_ev(low[0], "threshold") == "4.5"
          and float(_ct_ev(low[0], "ratio") or 9) < 4.5,
          f"verdict={g1.verdict} findings={[(f.level, f.check_id) for f in g1.findings]}")
    check("27.1b ★ 这条 WARN **不阻断**（工单 §四第 3 条：全部 WARN/INFO，不许 FAIL）",
          g1.findings and _ct_levels(g1) <= {"WARN", "INFO", "SKIP"}
          and not any(f.level == "FAIL" for f in g1.findings),
          str(_ct_levels(g1)))
    check("27.1c 修法文案能指导动手（既有「改法」也说清了字色压在哪层上）",
          "改法" in low[0].message and "1F3864" in low[0].message,
          low[0].message[:80])

    # ── 27.2 正控（大字豁免）：同一组配色，12pt 报 / 24pt 不报 ──────────
    PALE = "F4EFE4"
    TAN = "8C7A5B"                               # 与绢底实测 3.63:1 —— 卡在两档之间
    g2 = _ct(tmp, "big.pptx",
             [[_ct_rect("band", BAND, PALE), _ct_sp("bdy", TXT,
                                                    [(12.0, "小字一处", TAN, False)])],
              [_ct_rect("band", BAND, PALE), _ct_sp("bdy", TXT,
                                                    [(24.0, "大字同一配色", TAN, False)])],
              [_ct_rect("band", BAND, PALE), _ct_sp("bdy", TXT,
                                                    [(14.0, "14pt 粗体", TAN, True)])],
              [_ct_rect("band", BAND, PALE), _ct_sp("bdy", TXT,
                                                    [(14.0, "14pt 非粗", TAN, False)])]],
             theme={"lt1": PALE})
    rows2 = _ct_rows(g2)
    check("27.2 ★ 正控（大字豁免）：同一组配色 3.63:1 —— 12pt **报**、24pt **不报**"
          "（WCAG 大字线 18pt / 门槛 3:1）",
          [r["status"] for r in rows2] == ["low", "ok", "ok", "low"]
          and [_ct_ev(f, "slide") for f in _ct_low(g2)] == ["1", "4"]
          and rows2[1]["threshold"] == D.CONTRAST_LARGE_AA,
          f"rows={[(r.get('status'), r.get('threshold')) for r in rows2]} "
          f"low={[_ct_ev(f, 'slide') for f in _ct_low(g2)]}")
    check("27.2b ★ 粗体大字线按 WCAG 取 14pt：14pt 粗体豁免、14pt 非粗仍按正文判",
          rows2[2]["status"] == "ok" and rows2[3]["status"] == "low"
          and rows2[2]["threshold"] == 3.0 and rows2[3]["threshold"] == 4.5,
          f"rows={[(r.get('threshold'), r.get('status')) for r in rows2[2:]]}")

    # ── 27.3 ★★ 负控（本节最重要的一条）：白底黑字必须一条都不报 ────────
    g3 = _ct(tmp, "white_black.pptx",
             [[_ct_rect("band", BAND, "FFFFFF"),
               _ct_sp("bdy", TXT, [(12.0, "白底黑字一处正文", "000000", False)])],
              [_ct_sp("bdy", TXT, [(18.0, "没有下层形状，退页面底色", "000000", False)])],
              [_ct_sp("Title 1", TXT, [(36.0, "标题大字", "23201C", True)])]],
             theme={"lt1": "FFFFFF", "dk1": "000000"})
    check("27.3 ★★ 负控（工单 §五.3）：白底黑字**一条都不报** —— 三处全量到、零告警",
          g3.verdict == "PASS" and g3.findings == []
          and g3.extra["contrast"]["measured"] == 3
          and g3.extra["contrast"]["unmeasured"] == 0,
          f"verdict={g3.verdict} measured={g3.extra['contrast']['measured']} "
          f"unmeasured={g3.extra['contrast']['unmeasured']} "
          f"findings={[(f.level, f.check_id) for f in g3.findings]}")
    check("27.3b ★ 负控的第二层：黑字压白底 = 21:1，量到的每一个比值都 ≥ 门槛",
          all(r["status"] == "ok" and r["ratio"] >= r["threshold"]
              for r in _ct_rows(g3) if r.get("status") == "ok")
          and _ct_rows(g3)[0]["ratio"] > 20.9,
          str(_ct_rows(g3)))

    # ── 27.4 图片情形：抓不到就**如实说抓不到**（本次的核心交付物）───────
    g4 = _ct(tmp, "over_pic.pptx",
             [[_geo_pic("图片 5", BAND),
               _ct_sp("TextBox 42", TXT, [(10.0, "适宜温度报道分散在 4–15℃", "FFFFFF", False)])]],
             theme={"lt1": "FFFFFF", "dk1": "000000"})
    check("27.4 ★ 文字压在 `pic` 上 ⇒ 收到 `contrast_unmeasurable`，"
          "且 verdict **不是 PASS**（纯 XML 拿不到照片亮度 —— 不许默认它合格）",
          len(_ct_unmeas(g4)) == 1 and _ct_low(g4) == []
          and g4.verdict != "PASS"
          and "TextBox 42" in _ct_unmeas(g4)[0].message
          and g4.extra["contrast"]["measured"] == 0,
          f"verdict={g4.verdict} ids={[f.check_id for f in g4.findings]}")

    # ── 27.5 没查 ≠ 通过（极简夹具：无填充、无几何、无主题）──────────────
    p5 = make_pptx(tmp / "bare.pptx", green_slides(6, body_pt=18.0,
                                                   titles=GOOD_TITLES[:6]),
                   notes=["讲稿 %d" % i for i in range(6)], media=2)
    rep5 = _aud().audit(p5, workdir=str(tmp), report_path=tmp / "bare.report.json")
    g5 = _gate(rep5, "contrast")
    check("27.5 ★ 没有任何配色信息的极简 deck ⇒ verdict **不是 PASS**"
          "（工单 §五.5「没查 ≠ 通过」）",
          g5.verdict != "PASS" and g5.extra["contrast"]["measured"] == 0,
          f"verdict={g5.verdict} detail={g5.detail[:80]}")
    check("27.5b ★ 而且「没查」是**说出来**的：有一条 SKIP finding 写明「对比度没查」，"
          "它进 `warnings`（不是只活在 detail 里）",
          any(f.level == "SKIP" and "没查" in f.message for f in g5.findings)
          and any("contrast_not_checked" in w for w in rep5.warnings),
          f"findings={[(f.level, f.check_id) for f in g5.findings]} "
          f"warn={rep5.warnings[:2]}")
    check("27.5c ★★ 但**不阻断** —— 这是与工单 §三②.5 的有意偏离：`SKIPPED` 在本库算阻断，"
          "而「字色继承母版」是 python-pptx 产物的常态 ⇒ 会变成谁也过不去的死结"
          "（同 §3.10 给 notes 门留的那句话）",
          g5.verdict == "NOT APPLICABLE" and not rep5.blockers and rep5.ok
          and rep5.verdict != "FIX BEFORE DELIVERY",
          f"verdict={g5.verdict} blockers={rep5.blockers[:1]} rep={rep5.verdict}")

    # ── 27.6 端到端：门注册上了，人读版也列出来了 ────────────────────────
    check("27.6 ★ 门在 audit() 里注册（七道门，且排在 density 之后）",
          [g.name for g in rep5.gates] == ["open", "xml", "type", "density",
                                           "contrast", "notes", "freshness"],
          str([g.name for g in rep5.gates]))
    check("27.6b 人读版逐门列出了 contrast（模型定位得到是哪道门）",
          "contrast" in rep5.format_for_model(), rep5.format_for_model()[:120])
    check("27.6c ★ 文件打不开时这一维**不凭空消失**（降级分支也补了，工单 §三③）",
          _gate(_aud().audit(tmp / "不存在.pptx", workdir=str(tmp),
                             report_path=tmp / "gone.report.json"),
                "contrast").verdict == "SKIPPED",
          "降级分支里没有 contrast 或判定不对")

    # ── 27.7 承重自检：把门槛拿掉，正控必须当场变哑（否则判据是空的）──────
    with swapped(D, "CONTRAST_BODY_AA", 0.0), swapped(D, "CONTRAST_LARGE_AA", 0.0):
        g_zero = _ct(tmp, "zero.pptx",
                     [[_ct_rect("band", BAND, "1F3864"),
                       _ct_sp("bdy", TXT, [(12.0, "深底深字", "064178", False)])]],
                     theme={"lt1": "FFFFFF"})
    check("27.7 ★ 承重：门槛调成 0 ⇒ 27.1 那份正控**一条都不报**"
          "（证明报的是「比较」，不是文案里硬写的数字）",
          g_zero.findings == [] and g_zero.verdict == "PASS",
          f"verdict={g_zero.verdict} ids={[f.check_id for f in g_zero.findings]}")
    with swapped(D, "CONTRAST_BODY_AA", 99.0):
        g_huge = _ct(tmp, "huge.pptx",
                     [[_ct_sp("bdy", TXT, [(12.0, "白底黑字", "000000", False)])]],
                     theme={"lt1": "FFFFFF"})
    check("27.7b 承重（反方向）：门槛抬到 99 ⇒ 连 21:1 的白底黑字也要报"
          "（判据真的在读 WCAG 数，不是一串恒真的 if）",
          len(_ct_low(g_huge)) == 1, f"ids={[f.check_id for f in g_huge.findings]}")

    # ── 27.8 WCAG 数学：钉死几个可手算的值（主控会拿自己的实现交叉校验）──
    check("27.8 ★ 数学对得上 WCAG：黑/白 = 21.00，767676/白 = 4.54（AA 边界经典值），"
          "白/黑 对称",
          abs(D._contrast_ratio("000000", "FFFFFF") - 21.0) < 0.01
          and abs(D._contrast_ratio("767676", "FFFFFF") - 4.54) < 0.02
          and abs(D._contrast_ratio("FFFFFF", "000000")
                  - D._contrast_ratio("000000", "FFFFFF")) < 1e-9,
          f"21={D._contrast_ratio('000000','FFFFFF'):.2f} "
          f"767676={D._contrast_ratio('767676','FFFFFF'):.2f}")
    check("27.8b 线性化按 sRGB 公式（不是直接除 255）：中灰 808080 的相对亮度 ≈ 0.216",
          abs(D._rel_luminance("808080") - 0.2159) < 0.002,
          f"{D._rel_luminance('808080'):.4f}")

    # ── 27.9 误报治理：擦边的装饰短线不是"背景" ──────────────────────────
    g9 = _ct(tmp, "rule.pptx",
             [[_ct_rect("rk-decor-rule", (100, 100, 600, 10), "1F3864"),
               _ct_sp("bdy", TXT, [(12.0, "字只擦到一根短线", "064178", False)])]],
             theme={"lt1": "FFFFFF"})
    check("27.9 ★ 误报治理：几 px 高的装饰短线只压到文字框一角 ⇒ **不算背景**，"
          "按页面底色比（否则深字配深线 = 假警）",
          _ct_low(g9) == [] and _ct_rows(g9)[0]["bg"] == "FFFFFF",
          f"rows={_ct_rows(g9)[:2]} ids={[f.check_id for f in g9.findings]}")

    # ── 27.10 误报治理：页脚 / 页码与 density 门同一口径，一律不判 ────────
    g10 = _ct(tmp, "footer.pptx",
              [[_ct_rect("band", BAND, "1F3864"),
                _ct_sp("juanqing-footer-label", TXT,
                       [(12.0, "落款是刻意弱化的米棕字", "8C7A5B", False)]),
                _ct_sp("rk-decor-x", TXT, [(12.0, "rk- 前缀的装饰", "8C7A5B", False)]),
                _ct_sp("pageno", TXT, [(12.0, "3", "8C7A5B", False)], ph="sldNum")]],
              theme={"lt1": "FFFFFF"})
    f10 = probe_pptx(make_ct_pptx(tmp / "footer_probe.pptx",
                                  [[_ct_rect("band", BAND, "1F3864"),
                                    _ct_sp("juanqing-footer-label", TXT,
                                           [(12.0, "落款", "8C7A5B", False)])]],
                                  theme={"lt1": "FFFFFF"}))
    check("27.10 ★ 页脚/落款那类装饰字**不进 text_shapes**（与 `_DENSITY_CHROME_*` 同一份口径，"
          "不另设第二套「什么算装饰」）",
          _ct_low(g10) == []
          and [t["name"] for t in f10.slides[0]["text_shapes"]] == [],
          f"ids={[f.check_id for f in g10.findings]} "
          f"names={[t['name'] for t in f10.slides[0]['text_shapes']]}")

    # ── 27.11 深色填充形状里的白字：自己涂的色必须先算（否则必假警）────────
    g11 = _ct(tmp, "own_fill.pptx",
              [[_ct_sp("圆角矩形 7", TXT, [(12.0, "深底里的白字", "FFFFFF", False)],
                       fill="1F3864")]],
              theme={"lt1": "FFFFFF"})
    check("27.11 ★ 文字形状**自己**带 solidFill（深色圆角矩形里写白字）⇒ 拿它自己的填充算，"
          "不许拿页面底色去比（那是 12:1 量成 1.15:1 的假警）",
          _ct_low(g11) == [] and _ct_rows(g11)[0]["bg"] == "1F3864"
          and _ct_rows(g11)[0]["ratio"] > 11,
          f"rows={_ct_rows(g11)[:2]} ids={[f.check_id for f in g11.findings]}")

    # ── 27.12 schemeClr 解不出 ⇒ 量不到，**不许猜成白色**（工单 §四第 5 条）──
    g12 = _ct(tmp, "sysclr.pptx",
              [[_ct_sp("bdy", TXT, [(12.0, "字色写的是主题槽 bg1", None, False)])]],
              theme={"lt1": ("sys", "window"), "dk1": ("sys", "windowText")})
    check("27.12 ★ 主题槽是 `sysClr`（默认 Office 主题的 `window`/`windowText`，"
          "`read_theme_colors` 原样给出**名字**不是色值）⇒ 量不到，不猜成白",
          g12.verdict == "NOT APPLICABLE" and _ct_low(g12) == []
          and g12.extra["contrast"]["page_bg"] is None
          and _ct_rows(g12)[0]["status"] == "unmeasurable",
          f"verdict={g12.verdict} page_bg={g12.extra['contrast']['page_bg']} "
          f"rows={_ct_rows(g12)[:2]}")
    probe12 = probe_pptx(make_ct_pptx(tmp / "sysclr_probe.pptx",
                                      [[_ct_sp("bdy", TXT,
                                               [(12.0, "继承色", None, False)])]],
                                      theme={"lt1": ("sys", "window")}))
    check("27.12b 事实层没有把解不出的字色**填成一个好看的数**（记 None = 不知道）",
          probe12.slides[0]["text_shapes"][0]["runs"][0]["rgb"] is None
          and probe12.theme_colors.get("lt1") == "WINDOW",
          str(probe12.slides[0]["text_shapes"][0]["runs"][:1])
          + f" lt1={probe12.theme_colors.get('lt1')}")

    # ── 27.13 渐变 ⇒ 量不到（不许拿第一个 stop 冒充实测底色）──────────────
    g13 = _ct(tmp, "grad.pptx",
              [[_ct_rect("矩形 2", BAND, "grad"),
                _ct_sp("TextBox 9", TXT, [(16.0, "压在渐变上的字", "FFFFFF", False)])]],
              theme={"lt1": "FFFFFF"})
    check("27.13 ★ 渐变没有单一底色 ⇒ `contrast_unmeasurable`，**不拿渐变首色凑一个数**",
          len(_ct_unmeas(g13)) == 1 and _ct_low(g13) == [] and g13.verdict != "PASS"
          and all(r["status"] == "unmeasurable" for r in _ct_rows(g13)),
          f"verdict={g13.verdict} rows={_ct_rows(g13)[:2]}")

    # ── 27.14 ★★ 负控的第二份输入：**随库发布的好 deck** ─────────────────
    #   工单 §五.3 那句"先拿已经正确的输入跑一遍"在本库有现成的答案：参考样例
    #   是 W6 定下来的标杆，它**不是**我为了这条判据造的夹具 —— 拿它跑，假警才露得出来。
    #   ★ 次序要说清楚：动手**之前**先把这份样例的每一对配色量了一遍，撞出来的正是
    #   页脚落款那处 —— `8C7A5B` 米棕字 / 12pt 压在 `F4EFE4` 绢底上 **3.63:1**，
    #   按正文门槛必报；可它是"刻意弱化的落款"，不是"后排看不清的正文"。
    #   于是有了 27.10 那条"页脚 chrome 与 density 门同一口径、一律不判"。
    #   （等测试红了再去改判据 = 拿假警当证据，那是本项目另一笔血债。）
    ref = ROOT / "docs" / "deck-reference" / "reference_v1.pptx"
    if ref.is_file():
        gref = _aud()._gate_contrast(probe_pptx(ref))
        check("27.14 ★★ 负控（真产物）：随库发布的参考样例**一条对比度提示都没有**"
              "（全部配色量到、全部在 AA 门槛上）",
              gref.verdict == "PASS" and gref.findings == []
              and gref.extra["contrast"]["measured"] >= 20
              and gref.extra["contrast"]["unmeasured"] == 0,
              f"verdict={gref.verdict} measured={gref.extra['contrast']['measured']} "
              f"unmeasured={gref.extra['contrast']['unmeasured']} "
              f"ids={[f.check_id for f in gref.findings][:4]}")
    else:
        check("27.14 ★★ 负控（真产物）：参考样例在位（文件没找到 ⇒ 这一条没查）",
              False, str(ref))

    # ── 27.15 全节统一约束：本门**永远不产出 FAIL**（工单 §四第 3 条）──────
    all_g = [g1, g2, g3, g4, g5, g9, g10, g11, g12, g13, g_zero, g_huge]
    check("27.15 ★ 本节所有 fixture 里，这一门只出 WARN/INFO/SKIP —— 一条 FAIL 都没有",
          all(f.level in ("WARN", "INFO", "SKIP") for g in all_g for f in g.findings),
          str({f.level for g in all_g for f in g.findings}))
    check("27.15b 也没有任何一页把「没量到」报成 PASS（measured==0 ⇒ verdict≠PASS）",
          all(not (g.extra["contrast"]["measured"] == 0 and g.verdict == "PASS")
              for g in all_g),
          str([(g.verdict, g.extra["contrast"]["measured"]) for g in all_g]))

    # 新字段只许追加：把"别动已有键"钉住（工单 §四第 4 条）
    old_keys = {"slide", "part", "text_chars", "has_title", "max_font_pt",
                "title_text", "max_run_chars", "min_font_pt", "notes_chars",
                "has_notes", "overflow_ratio", "kind", "util", "util_box", "bullets"}
    check("27.16 ★ `facts.slides[i]` 的既有键一个没少，新键只追加",
          old_keys <= set(f10.slides[0])
          and {"text_shapes", "fill_shapes"} <= set(f10.slides[0]),
          str(sorted(set(f10.slides[0]))))

    T.stash(tmp, "deck-ct")


#: 用 python-pptx 造**混排**真 deck（W9 §28 的输入）。三种页各司其职：
#:   p1 该修 —— 12pt `4472C4` 压在 `4F9FDB` 上（工单抓到的那对色，1.64:1）
#:   p2 该留 —— 白底黑字（负控：一个字都不许碰）
#:   p3 量不到 —— 白字压在图片上（不猜、不改）
#:   p4 该修 —— 20pt `4472C4` 压在 `4F9FDB` 上（大字档，门槛 3:1）
#: `mode="good"` ⇒ 只造 p2 那种全合格页（负控的独立输入，用来验"零 diff"）。
_W9_BUILD = r'''
import base64, io, sys
from pptx import Presentation
from pptx.util import Pt, Emu, Inches
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
out, mode = sys.argv[1], sys.argv[2]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
    "WjR9awAAAABJRU5ErkJggg==")
prs = Presentation()
prs.slide_width = Emu(12192000); prs.slide_height = Emu(6858000)
blank = prs.slide_layouts[6]
def band(sl, hexv):
    sh = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0),
                             prs.slide_width, prs.slide_height)
    sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor.from_string(hexv)
    sh.line.fill.background()
def txt(sl, name, s, pt, hexv):
    tb = sl.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(0.8))
    tb.name = name
    r = tb.text_frame.paragraphs[0].add_run(); r.text = s
    r.font.size = Pt(pt); r.font.color.rgb = RGBColor.from_string(hexv)
if mode == "good":
    s = prs.slides.add_slide(blank); band(s, "FFFFFF")
    txt(s, "正常正文框", "白底黑字一个字都不许碰", 12.0, "000000")
else:
    s = prs.slides.add_slide(blank); band(s, "4F9FDB")
    txt(s, "圆角矩形 70", "甲组：低温必要", 12.0, "4472C4")
    s = prs.slides.add_slide(blank); band(s, "FFFFFF")
    txt(s, "正常正文框", "白底黑字一个字都不许碰", 12.0, "000000")
    s = prs.slides.add_slide(blank)
    s.shapes.add_picture(io.BytesIO(PNG), Inches(0), Inches(0),
                         prs.slide_width, prs.slide_height)
    txt(s, "照片上文字", "压在照片上的白字", 12.0, "FFFFFF")
    s = prs.slides.add_slide(blank); band(s, "4F9FDB")
    txt(s, "圆角矩形 71", "乙组：加速", 20.0, "4472C4")
prs.save(out)
'''


def _w9_build_real(path, py: str, mode: str = "mixed") -> bool:
    """`_W9_BUILD` 的测试入口。解释器为 None ⇒ 返回 False（别让 None 进 subprocess）。"""
    if not py:
        return False
    r = subprocess.run([py, "-c", _W9_BUILD, str(path), mode],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and Path(path).is_file()


def _w9_text_rgb(facts, slide_no: int) -> str | None:
    for s in facts.slides:
        if s["slide"] == slide_no:
            for t in s.get("text_shapes") or []:
                for run in t.get("runs") or []:
                    if run.get("rgb"):
                        return run["rgb"]
    return None


def _w9_legacy_pixel_ratio(vals):
    """**W9 那版分布口径**（背景=中位数、文字=离中位数最远的 5% 尾）原样复算。

    放在测试里而不是模块里：它唯一的用处是把"取最暗 5%"这个已被否证的取法
    钉成一个可调用对象 ⇒ 28.9d 拿它证明"深底浅字必塌成 1.00:1"。
    哪天有人把 `_pixel_contrast_stats` 改回那一版，正控会红，而这条复算
    先一步说明**红的是口径，不是运气**。
    """
    vs = sorted(vals)
    n = len(vs)
    tail = max(1, n // 20)
    dark, light, mid = vs[tail - 1], vs[n - tail], vs[n // 2]
    lm, ld, ll = D._srgb_lin(mid), D._srgb_lin(dark), D._srgb_lin(light)
    fg = dark if abs(ld - lm) >= abs(ll - lm) else light
    lf = D._srgb_lin(fg)
    hi, lo = max(lf, lm), min(lf, lm)
    return round((hi + 0.05) / (lo + 0.05), 2)


def _strip_clock(blob: str) -> str:
    """抹掉**墙上时钟**再拿来比对（"不传参数时逐字相同"唯一站得住的写法）。

    报告里有三个天生每次都不同的字段：`audited_at` / `audited_at_epoch`
    （体检时刻）与 `pptx_mtime`（文件落盘时刻，与体检时刻同源）。
    它们不是行为差异，把它们算进差异 ⇒ 这条断言**恒假**；
    反过来只要不比它们 ⇒ 又松到看不出回归。所以显式列出、只抹这三处。
    """
    out = re.sub(r'"audited_at(?:_epoch)?"\s*:\s*(?:[\d.]+|"[^"]*")',
                 '"audited_at":0', blob)
    return re.sub(r'"pptx_mtime"\s*:\s*[\d.]+', '"pptx_mtime":0', out)


def test_readability_and_pixel() -> None:
    section("28 可读性修正与像素校验（W9）")
    tmp = T.fresh_dir(_TMPROOT / "w9", "deck-w9")
    BAND = (0, 0, 1280, 720)
    TXT = (100, 100, 600, 80)

    # 一份**两页**的 stdlib 夹具（不需要 python-pptx）：
    #   第 1 页 深底深字（可量、低对比）  第 2 页 白字压图片（量不到 ⇒ 像素目标）
    off_path = make_ct_pptx(
        tmp / "off.pptx",
        [[_ct_rect("band", BAND, "1F3864"),
          _ct_sp("bdy", TXT, [(12.0, "深底深字要报", "064178", False)])],
         [_geo_pic("图片 5", BAND),
          _ct_sp("TextBox 42", TXT, [(10.0, "压在照片上的白字", "FFFFFF", False)])]],
        theme={"lt1": "FFFFFF", "dk1": "000000"})
    facts_off = probe_pptx(off_path)

    # ── 28.4 ★★ 默认关闭 ≡ 与 W8 逐字相同（承重：换了会抛的渲染 tripwire）──
    g_default = _aud()._gate_contrast(facts_off, pptx_path=off_path)

    def _tripwire(*a, **k):
        raise AssertionError("关闭时不许调用渲染（deep_contrast 默认必须为 False）")

    try:
        with swapped(D, "_render_deck_pngs", _tripwire):
            g_off = _aud()._gate_contrast(facts_off, deep_contrast=False,
                                          pptx_path=off_path)
    except AssertionError:
        g_off = None
    check("28.4 ★★ 默认关闭：整门**不碰渲染器**（tripwire 未被触发）⇒ 关闭时"
          "与 W8 逐字相同（verdict/detail/findings/extra 全等），extra 里没有 pixel 键",
          g_off is not None and "pixel" not in g_off.extra["contrast"]
          and repr(vars(g_off)) == repr(vars(g_default)),
          f"渲染被调用（g_off=None）" if g_off is None
          else f"pixel={'pixel' in g_off.extra['contrast']} "
               f"equal={repr(vars(g_off)) == repr(vars(g_default))}")
    g_rep = _gate(_aud().audit(off_path, workdir=str(tmp),
                               report_path=tmp / "off.report.json"), "contrast")
    check("28.4b 走完整 audit()：默认关闭时 contrast 的 extra 也没有 pixel 键",
          "pixel" not in (g_rep.extra.get("contrast") or {}),
          str(sorted((g_rep.extra.get("contrast") or {}).keys())))

    # ── 28.5 ★ 模拟渲染失败 ⇒ 不抛异常 + 一条 SKIP「没跑成」+ 不是 PASS ──────
    with swapped(D, "_render_deck_pngs", lambda *a, **k: ([], "模拟渲染失败")):
        try:
            g_fail = _aud()._gate_contrast(facts_off, deep_contrast=True,
                                           pptx_path=off_path)
            raised = ""
        except Exception as e:                                    # noqa: BLE001
            g_fail, raised = None, f"{type(e).__name__}: {e}"
    pf = [f for f in (g_fail.findings if g_fail else [])
          if f.check_id == "contrast_pixel_unavailable"]
    check("28.5 ★ 渲染没跑成（天塌下来）⇒ 不抛异常、有一条 SKIP 明写「没跑成」"
          "且说「没查」，verdict **不是 PASS**（绝不把没渲染读成通过 —— M 型）",
          raised == "" and len(pf) == 1 and pf[0].level == "SKIP"
          and "没跑成" in pf[0].message and "没查" in pf[0].message
          and g_fail.verdict != "PASS",
          f"raised={raised!r} pf={[(f.level, f.check_id) for f in pf]} "
          f"verdict={getattr(g_fail, 'verdict', None)}")
    check("28.5b detail 里也说出了「没跑成」（不只是藏在 finding 里）",
          g_fail is not None and "没跑成" in g_fail.detail,
          g_fail.detail[:120] if g_fail else "渲染失败抛了异常")

    # ── 28.6 ★ 勘误选色（纯数学，py 无关）：4F9FDB 上取 argmax ⇒ 深色不是白字 ──
    #   候选取**裸集**（默认 Office 主题的 dk1/lt1 是 sysClr、解不出 ⇒ 不进候选，
    #   与真 deck 那份一致；带可解槽位的主题 argmax 会选到那条更深的色，也对）。
    cands = D._contrast_fix_candidates({})
    r_white = D._contrast_ratio("FFFFFF", "4F9FDB")
    r_dark = D._contrast_ratio("1A1A1A", "4F9FDB")
    best = max(cands, key=lambda c: D._contrast_ratio(c, "4F9FDB"))
    check("28.6 ★ 勘误：底色 4F9FDB 上**不亮度分档**、取候选里对比度最大者 ⇒ 深色 1A1A1A"
          f"（≈{r_dark:.2f}:1），**不是**分档会选的白字（≈{r_white:.2f}:1，修了还是不合格）",
          r_white < 3.0 < r_dark and best == "1A1A1A"
          and abs(r_dark - 6.06) < 0.15,
          f"FFFFFF={r_white:.2f} 1A1A1A={r_dark:.2f} argmax={best}")

    # ── 28.7 ★ 分布口径的单位 bug 承重（纯单元，py 无关）：双峰像素 ≠ 1.0:1 ──
    #   踩过的坑：`_srgb_lin` 内部已经 /255，`_pixel_contrast_stats` 又把 0-255
    #   的亮度**再 /255** 传进去 ⇒ 相对亮度被平方地压到近 0 ⇒ 任何区域都算出 ≈1.0:1
    #   （真 deck 上一眼假警）。这条用合成像素钉死"深字压浅底"该得到的高比值。
    bimodal = D._pixel_contrast_stats([255] * 950 + [53] * 50)
    flat = D._pixel_contrast_stats([200] * 1000)
    check("28.7 ★ 分布口径**数学对**：53 深字 / 255 浅底（各占尾）⇒ 比值 ≈12:1，"
          "绝不是单位错配会给出的 1.0:1（W10 后：文字=离主体档最远的少数撮）",
          bimodal is not None and bimodal.get("measurable") is True
          and bimodal["ratio"] > 8.0 and abs(bimodal["ratio"] - 12.27) < 1.5
          and "暗侧" in bimodal["text_side"],
          f"bimodal={bimodal and (bimodal['ratio'], bimodal['text_side'])}")
    check("28.7b ★ **纯色区（无文字）⇒ 量不到**：measurable=False 且结果里**根本没有 "
          "ratio 键**（W10 §一③：找不出两撮就绝不报一个数字。W9 在这里报的是 1.00:1）",
          flat is not None and flat.get("measurable") is False and "ratio" not in flat
          and "1.00" not in str(flat),
          str(flat)[:160])

    # ── 28.9 ★★ W10 主修：判"谁是文字"靠**哪一撮是少数**，不靠哪端更极端 ──────
    #   真 deck 上 12 条像素 WARN 里 3 条报 1.00:1，而那三个形状在渲染图上都
    #   **清清楚楚有字**（矩形 55 深蓝底白字 / 矩形 68 蓝底白字 / 小李PPT37 白底深字）。
    #   1.00:1 的定义 = 前景与背景完全同色 ⇒ 那种区域根本没有字 ⇒ 必然是假警。
    #   根因：W9 在 overlap 分支取"最暗 5%"当文字，而**深底浅字**时最暗那一撮
    #   **正是背景本身**（实测那份 deck 96% 的像素在同一档里）。
    deep_on_light = D._pixel_contrast_stats([56] * 960 + [250] * 40)    # 深底浅字
    light_on_deep = D._pixel_contrast_stats([250] * 960 + [56] * 40)    # 浅底深字
    check("28.9 ★ 正控（深底浅字，96% 灰阶 56 + 4% 灰阶 250，即真 deck 的矩形 55）"
          "⇒ 算出**高**对比度（>7:1），**绝不是 1.0**",
          deep_on_light is not None and deep_on_light.get("measurable") is True
          and deep_on_light["ratio"] > 7.0
          and deep_on_light["text_luma"] == 250 and deep_on_light["bg_luma"] == 56,
          str(deep_on_light)[:170])
    check("28.9b ★★ **反向正控（浅底深字）**：96% 灰阶 250 + 4% 灰阶 56 ⇒ 同样高对比度。"
          "**两个方向都必须对** —— 本项目血债：一个'开关'只测一个方向会漏掉反方向",
          light_on_deep is not None and light_on_deep.get("measurable") is True
          and light_on_deep["ratio"] > 7.0
          and light_on_deep["text_luma"] == 56 and light_on_deep["bg_luma"] == 250,
          str(light_on_deep)[:170])
    check("28.9c 两条正控是**镜像**：比值逐位相等 ⇒ 方向不是靠特例分支撑起来的",
          deep_on_light is not None and light_on_deep is not None
          and deep_on_light["ratio"] == light_on_deep["ratio"]
          and deep_on_light["text_side"].endswith("亮侧峰")
          and light_on_deep["text_side"].endswith("暗侧峰"),
          f"{deep_on_light and deep_on_light['ratio']} vs "
          f"{light_on_deep and light_on_deep['ratio']} "
          f"{[deep_on_light and deep_on_light['text_side'], light_on_deep and light_on_deep['text_side']]}")
    check("28.9d ★ **反注入**：把'取少数撮'换回 W9 的'最暗 5%'口径 ⇒ 深底浅字**必然"
          "塌成 1.00:1**（下面这条独立复算就是当初那条假警）⇒ 正控真会红",
          _w9_legacy_pixel_ratio([56] * 960 + [250] * 40) == 1.0
          and deep_on_light is not None and deep_on_light["ratio"] > 7.0,
          f"legacy={_w9_legacy_pixel_ratio([56] * 960 + [250] * 40)} "
          f"new={deep_on_light and deep_on_light['ratio']}")
    low_real = D._pixel_contrast_stats([200] * 960 + [170] * 40)
    check("28.9e ★ **没把判据糊过去**：真低对比（浅底 200 + 离主体两档的深字 170）"
          "⇒ 仍然量得到、且比值低于正文门槛 4.5（不是'一律记量不到'）",
          low_real is not None and low_real.get("measurable") is True
          and 1.0 < low_real["ratio"] < D.CONTRAST_BODY_AA,
          str(low_real)[:170])
    wide = D._pixel_contrast_stats(list(range(0, 256)) * 4)
    check("28.9f 照片/渐变式宽分布（没有一档占主体）⇒ **量不到**，不硬编一个比值"
          "（那块区域压根没有'背景'可比 —— 主控 0.5 门限）",
          wide is not None and wide.get("measurable") is False and "ratio" not in wide,
          str(wide)[:150])
    half = D._pixel_contrast_stats([250] * 700 + [56] * 300)
    check("28.9g 占比 30% 的那一撮**不是文字**（超过 20% ⇒ 是第二块背景）⇒ 量不到",
          half is not None and half.get("measurable") is False and "ratio" not in half,
          str(half)[:150])

    # ── 28.10 ★★ 端到端：手写渲染图 + 假渲染器 ⇒ 走通 `_contrast_pixel_pass` ──
    #   28.5 只测了"渲染没跑成"，成功分支此前**一条都没测过**（W9 的洞）。
    W_PX, H_PX = 120, 80                                   # 9600 像素，够分布口径

    def _pixel_e2e(name, greys, stats=None):
        png = tmp / f"px-{name}.png"
        png.write_bytes(_png_grey_bytes(greys, W_PX, H_PX))
        tg = [{"slide": 1, "shape": f"压图框{name}", "runs": [(12.0, False)],
               "rect": (0, 0, facts_off.slide_cx, facts_off.slide_cy)}]
        with swapped(D, "_render_deck_pngs", lambda *a, **k: ([str(png)], "")):
            if stats is None:
                return D._contrast_pixel_pass(facts_off, tg, str(off_path), "py")
            with swapped(D, "_pixel_contrast_stats", stats):
                return D._contrast_pixel_pass(facts_off, tg, str(off_path), "py")

    def _mix(main: int, share: float, ink: int) -> list:
        n_ink = int(W_PX * H_PX * share)
        return [main] * (W_PX * H_PX - n_ink) + [ink] * n_ink

    ew, ei, es = _pixel_e2e("deep", _mix(56, 0.04, 250))
    check("28.10 ★ 端到端（**深底浅字**渲染图喂进像素校验）⇒ 零条 WARN、量到 1 处，"
          "比值高（W9 在这条路上报的是 1.00:1 假警）",
          ew == [] and es["measured"] == 1 and es["per_shape"][0]["ratio"] > 7.0,
          f"warns={[(f.check_id, f.message[:40]) for f in ew]} summary="
          f"{ {k: es[k] for k in ('measured', 'unmeasurable')} }")
    ew2, ei2, es2 = _pixel_e2e("light", _mix(250, 0.04, 56))
    check("28.10b ★ 端到端**反方向**（浅底深字）⇒ 同样零 WARN 且量到高比值："
          "**像素维的两个方向都测到了**",
          ew2 == [] and es2["measured"] == 1 and es2["per_shape"][0]["ratio"] > 7.0,
          f"warns={len(ew2)} ratio={es2['per_shape'] and es2['per_shape'][0]['ratio']}")
    ew3, ei3, es3 = _pixel_e2e("flat", [200] * (W_PX * H_PX))
    um3 = [f for f in ei3 if f.check_id == "contrast_pixel_unmeasurable"]
    check("28.10c ★ 端到端**失败要响**：纯色渲染图 ⇒ 零 WARN + 一条 INFO"
          "「量不到」，evidence 里**没有 ratio=**（不报数），且不计入 measured",
          ew3 == [] and len(um3) == 1 and um3[0].level == "INFO"
          and "量不到" in um3[0].message and "没查" in um3[0].message
          and not any(str(e).startswith("ratio=") for e in um3[0].evidence)
          and es3["measured"] == 0 and es3["unmeasurable"] == 1,
          f"warns={len(ew3)} um={[f.message[:60] for f in um3]} "
          f"ev={um3 and um3[0].evidence}")
    legacy = {"ratio": 1.0, "text_luma": 53, "text_side": "最暗5%",
              "bg_luma": 53, "overlap": True, "n_px": 9600}
    ew4, ei4, es4 = _pixel_e2e("inj", _mix(56, 0.04, 250),
                               stats=lambda v: dict(legacy))
    check("28.10d ★★★ **出口硬断言**（W10 §一④）：就算取样函数退回旧口径、"
          "吐出一个 text_luma==bg_luma 的 1.00:1（连 measurable 都没写），"
          "像素校验这一层也**不许**把它变成 WARN —— 拦在出口、记取样失败",
          ew4 == [] and es4["measured"] == 0 and es4["unmeasurable"] == 1
          and not any("1.0" in f.message for f in ew4)
          and any(f.check_id == "contrast_pixel_unmeasurable" for f in ei4),
          f"warns={[(f.check_id, f.message[:50]) for f in ew4]} "
          f"infos={[(f.level, f.check_id) for f in ei4]}")
    mixed_pk = D._pixel_contrast_stats([56] * 9200 + [70] * 300 + [250] * 100)
    check("28.10e 抗噪：深底上同时有'肩部'（70，占 3%）与真笔画（250）⇒ 取**离主体"
          "更远**的那撮（250），不是最靠边的第一撮",
          mixed_pk is not None and mixed_pk.get("measurable") is True
          and mixed_pk["text_luma"] >= 240 and mixed_pk["ratio"] > 7.0,
          str(mixed_pk)[:170])

    # ── 28.12 ★★ W10 §二：把 `deep_contrast` 接进 MCP 工具面（`audit_deck`）─────
    #   W9 只有两条入口（`audit()` 的 kwargs 与 `config.deck.deep_contrast），
    #   schema 里没这个入参 ⇒ 模型侧只能改配置、**不能逐次点名开启**。
    from rongke.loop import AUDIT_DECK_SCHEMA as ADS, _make_audit_deck  # noqa: E402

    ap_ = ADS["properties"]
    check("28.12a 工具 schema 有 `deep_contrast`：type=boolean 且**不在 required 里**"
          "（老调用方一字不改照样能调）",
          "deep_contrast" in ap_ and ap_["deep_contrast"].get("type") == "boolean"
          and "deep_contrast" not in ADS["required"],
          f"required={ADS['required']} keys={sorted(ap_)}")
    adsc = str((ap_.get("deep_contrast") or {}).get("description") or "")
    check("28.12b 描述把**代价与适用面**说出口：慢（本机实测 ~15 秒）、会调用 PowerPoint "
          "渲染、仅在查「文字压在照片上」时开启、默认 false（工单 §二 点名的四句）",
          "15" in adsc and "PowerPoint" in adsc and "照片" in adsc
          and "默认 false" in adsc,
          adsc[:150])

    seen_kw: list = []
    renders: list = []
    _real_audit = D.DeckAuditor.audit

    def _rec_audit(self, *a, **k):
        seen_kw.append(k.get("deep_contrast", "«没传»"))
        return _real_audit(self, *a, **k)

    def _fake_render(pptx_path, python_exe, outdir, timeout=0.0):
        """假渲染器：**不叫 PowerPoint**，但真的走通 `_contrast_pixel_pass` 往下。"""
        renders.append(str(pptx_path))
        out = Path(outdir)
        pngs = []
        for i in (1, 2):
            p = out / f"slide-{i:02d}.png"
            p.write_bytes(_png_grey_bytes(_mix(56, 0.04, 250), W_PX, H_PX))
            pngs.append(str(p))
        return pngs, ""

    def _tool_call(c=None, **kw):
        prev = Path.cwd()
        try:
            os.chdir(tmp)                             # 工具按 cwd 解析相对路径与报告落点
            blob = _make_audit_deck(c or _Cfg())(str(off_path), **kw)
            rp = tmp / "deck_report.json"
            return blob, (rp.read_text(encoding="utf-8") if rp.is_file() else "")
        finally:
            os.chdir(prev)

    class _CfgDcOn(_Cfg):
        deep_contrast = True                          # 配置里开着（W9 那条老入口）

    with swapped(D.DeckAuditor, "audit", _rec_audit), \
            swapped(D, "_render_deck_pngs", _fake_render):
        out_default, rep_default = _tool_call()
        n_default, r_default = list(seen_kw), list(renders)
        seen_kw.clear(); renders.clear()
        out_off, rep_off = _tool_call(deep_contrast=False)
        n_off, r_off = list(seen_kw), list(renders)
        seen_kw.clear(); renders.clear()
        out_on, rep_on = _tool_call(deep_contrast=True)
        n_on, r_on = list(seen_kw), list(renders)
        seen_kw.clear(); renders.clear()
        out_cfg, rep_cfg = _tool_call(c=_CfgDcOn())
        n_cfg, r_cfg = list(seen_kw), list(renders)

    check("28.12c ★★ **不传该参数时行为逐字相同**：往 `audit()` 传的是 **None**"
          "（不是 False ⇒ 才能继续回落配置）、渲染器**一次都没碰**、"
          "且给模型的 JSON 与显式 false 一字不差（只抹掉墙上时钟那几个键）",
          n_default == [None] and r_default == []
          and _strip_clock(out_default) == _strip_clock(out_off)
          and _strip_clock(rep_default) == _strip_clock(rep_off),
          f"kwargs={n_default} renders={len(r_default)} "
          f"equal={_strip_clock(out_default) == _strip_clock(out_off)} "
          f"raw_equal={out_default == out_off}")
    check("28.12d ★ 显式 `deep_contrast=False` 与不传**完全等价**（同样不叫渲染器）",
          n_off == [None] and r_off == [] and _strip_clock(out_off) == _strip_clock(out_default),
          f"kwargs={n_off} renders={len(r_off)}")
    check("28.12e ★ 传 `true` ⇒ **真的走像素路径**：`audit()` 收到 True、渲染器被叫了一次、"
          "写盘的报告里出现像素口径那段（不传时那份报告里**没有**这段）",
          n_on == [True] and len(r_on) == 1 and "像素" in rep_on
          and "像素" not in rep_default and out_on != out_default,
          f"kwargs={n_on} renders={len(r_on)} 像素 in rep_on={'像素' in rep_on} "
          f"in default={'像素' in rep_default}")
    check("28.12f ★★ 承重（默认值的方向）：**配置里开着** deep_contrast=True 时，"
          "不传参数**仍然走配置**（渲染被叫）⇒ 工面新增的默认不是'强制关掉'那条老入口",
          n_cfg == [None] and len(r_cfg) >= 1,
          f"kwargs={n_cfg} renders={len(r_cfg)}")

    # ── 任务 1 的三条（正控 / 负控 / 量不到）需要 python-pptx：先守门 ──────────
    py = _py_pptx()
    if py is None:
        check("28.0 前置：拿得到装了 python-pptx 的解释器"
              "（否则 28.1/28.2/28.3 的真修正**没查**，不是通过）",
              False, "find_notes_python() 返回 None")
        T.stash(tmp, "deck-w9")
        return

    mixed = tmp / "mixed.pptx"
    good = tmp / "good.pptx"
    check("28.P 前置：造出混排真 deck（该修/该留/量不到 三类页齐）",
          _w9_build_real(mixed, py, "mixed"), str(mixed))
    check("28.Pb 前置：造出全合格真 deck（负控输入）",
          _w9_build_real(good, py, "good"), str(good))

    # ── 28.1 正控：计划抓到该修的两处、放过量不到的一处（纯读，未动文件）──────
    plan = D._plan_contrast_fixes(probe_pptx(mixed))
    fix_by = {(f["slide"], f["shape"]): f for f in plan["fixes"]}
    check("28.1 ★ 正控计划：抓到 p1「圆角矩形 70」（12pt→门槛4.5）与"
          "p4「圆角矩形 71」（20pt→门槛3.0）两处，都从 4472C4 压 4F9FDB 起",
          len(plan["fixes"]) == 2 and (1, "圆角矩形 70") in fix_by
          and (4, "圆角矩形 71") in fix_by
          and all(f["fg"] == "4472C4" and f["bg"] == "4F9FDB" for f in plan["fixes"])
          and fix_by[(1, "圆角矩形 70")]["threshold"] == 4.5
          and fix_by[(4, "圆角矩形 71")]["threshold"] == 3.0
          and fix_by[(1, "圆角矩形 70")]["before"] < 4.5,
          str([(f["slide"], f["shape"], f["before"], f["threshold"])
               for f in plan["fixes"]]))
    check("28.1b ★ 计划里的新色都是 **argmax 深色 1A1A1A**（非白字），改后比值 ≥ 门槛"
          "（勘误落地）",
          all(f["new"] == "1A1A1A" and f["after"] >= f["threshold"]
              for f in plan["fixes"]),
          str([(f["new"], f["after"]) for f in plan["fixes"]]))
    check("28.1c ★ 量不到的一处（p3 照片上文字）进 unmeasurable、**不进 fixes**（不猜不改）",
          len(plan["unmeasurable"]) == 1
          and plan["unmeasurable"][0]["shape"] == "照片上文字"
          and plan["unmeasurable"][0]["slide"] == 3
          and all(f["slide"] != 3 for f in plan["fixes"])
          and plan["unfixable"] == [],
          f"um={plan['unmeasurable']} uf={len(plan['unfixable'])}")

    # ── 28.2 ★★ 负控（本节最重要）：全合格 deck ⇒ 文件一个字节不动 ─────────────
    good_before = good.read_bytes()
    fix_good = D.apply_readability_fix(good, python_exe=py)
    check("28.2 ★★ 负控：白底黑字的全合格 deck ⇒ touched=False、fixed=[]、"
          "文件字节**一字未动**（越界给正常稿引入 diff 比不修更糟）",
          fix_good["touched"] is False and fix_good["fixed"] == []
          and good.read_bytes() == good_before,
          f"touched={fix_good['touched']} fixed={len(fix_good['fixed'])} "
          f"bytes_equal={good.read_bytes() == good_before}")

    # ── 28.1★ 闭环：真改一次，**从磁盘读回复测**才算数（子进程自述不算）──────────
    fix = D.apply_readability_fix(mixed, python_exe=py)
    ver = [v for v in fix["fixed"] if v.get("verified")]
    check("28.1★ 闭环：两处改色**读回磁盘复测** verified=True，读回比值 ≥ 门槛，"
          "touched=True（不是拿子进程自报当判据）",
          fix["touched"] and len(ver) == 2
          and all(v["after"] is not None and v["after"] >= v["threshold"]
                  and v["new"] == "1A1A1A" for v in ver),
          f"touched={fix['touched']} verified={len(ver)}/{len(fix['fixed'])} "
          f"after={[v['after'] for v in ver]}")

    fm = probe_pptx(mixed)
    check("28.2b ★ 混排稿里**该留的一个字没动**：p2 白底黑字仍 000000、"
          "p3 照片上仍 FFFFFF（只碰该修的 p1/p4）",
          _w9_text_rgb(fm, 2) == "000000" and _w9_text_rgb(fm, 3) == "FFFFFF"
          and _w9_text_rgb(fm, 1) == "1A1A1A" and _w9_text_rgb(fm, 4) == "1A1A1A",
          f"p1={_w9_text_rgb(fm,1)} p2={_w9_text_rgb(fm,2)} "
          f"p3={_w9_text_rgb(fm,3)} p4={_w9_text_rgb(fm,4)}")

    # ── 28.3 报告口径：改成功的进复测段、量不到的**说出口**且没被读成通过 ────────
    checks, warns = D.readability_fix_lines(fix)
    uml = [w for w in warns if "量不到" in w]
    check("28.3 ★ 量不到的在报告里**说出口**：一条 warn 明写「量不到」+ 指向 p3「照片上文字」"
          "+「没猜」，且**不进**复测段（不被读成通过）",
          len(uml) == 1 and "照片上文字" in uml[0] and "没猜" in uml[0]
          and not any("照片上文字" in c for c in checks),
          str(warns)[:220])
    check("28.3b 改成功两处进复测段：文案含「可读性修正」与新色 1A1A1A（比值 1.64→6.06）",
          len(checks) >= 1 and any("可读性修正" in c and "1A1A1A" in c and "6.06" in c
                                   for c in checks),
          str(checks)[:220])

    # ── 28.8 承重：把候选集里深色拿掉 ⇒ 4F9FDB 上「修不了」（不误判成能修）────────
    mixed2 = tmp / "mixed2.pptx"
    check("28.8a 前置：再造一份**未改过**的混排 deck（承重专用）",
          _w9_build_real(mixed2, py, "mixed"), str(mixed2))
    with swapped(D, "CONTRAST_FIX_CANDIDATES", ("FFFFFF",)):
        plan_cw = D._plan_contrast_fixes(probe_pptx(mixed2))
    check("28.8 ★ 承重：只给白字候选 ⇒ 4F9FDB 上两处都**修不了**（进 unfixable、"
          "fixes 空）—— 判据真在读比值，不是恒真（勘误：白字 2.87:1 不达标）",
          plan_cw["fixes"] == [] and len(plan_cw["unfixable"]) == 2,
          f"fixes={len(plan_cw['fixes'])} unfixable={len(plan_cw['unfixable'])}")

    T.stash(tmp, "deck-w9")


def main() -> int:
    global _QUIET
    _QUIET = "--json" in sys.argv[1:]
    if not _QUIET:
        print("═" * 62)
        print("荣珂 · 组会 PPT（MCP + 第 9 条硬门） · 回归测试")
        print("═" * 62)

    stray_before = snapshot_stray()

    test_intent()
    test_stage_agrees_with_deck()
    test_probe()
    test_type_gate()
    test_notes_gate()
    test_freshness_and_vocab()
    test_audit_e2e()
    test_deck_gate()
    test_mounting()
    test_load_bearing()
    test_upstream_script()
    test_no_pollution(stray_before)
    test_deck_recipe()
    test_write_notes()
    test_deck_visual()
    test_layout_judgement()
    test_argument_layer()
    test_skeleton_geometry()
    test_text_overflow()
    test_design_assets()
    test_deck_reference()
    test_density_gate()
    test_contrast_gate()
    test_readability_and_pixel()
    test_tool_seam_smoke()

    T.stash(_TMPROOT, "deck-tmproot")

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
