#!/usr/bin/env python3
"""构建参考样例 deck：`docs/deck-reference/reference_v1.*`。

    用 3.12 跑（要 python-pptx + Pillow + win32com）：
    python deck_reference_build.py [--no-sheet]   # 用装了 python-pptx 的解释器

## 它产出什么

    docs/deck-reference/reference_v1.src.pptx   未排版的源稿（4:3 + Office 默认主题）
    docs/deck-reference/reference_v1.pptx       成品（16:9 + 绢青陈墨 + 六种页型）
    docs/deck-reference/reference_v1.sheet.png  分页预览（**给人看的**）
    docs/deck-reference/reference_v1.md         工具说明的落盘副本（**由代码生成**）

## 为什么必须"可重跑"（而不是手工做一份搁那儿）

参考样例的全部价值在于**它和工具是一致的**。手工做一份、再手工写一段说明，
两边的漂移没有任何东西会拦住 —— 那就是 H 型：改一处忘一处，静默漂移。

所以：

  · 内容 **只有一个来源**：`rongke/deckref.py::REFERENCE_DECK`；
  · `.md` **不手写**，由 `deckref.reference_text()` 生成；
  · `deck_test` §24 会**重新生成一遍**再和落盘的对，不一致就红。

⇒ 改了 `REFERENCE_DECK` 或 `reference_text()` 而忘了重跑本脚本，
   测试会当场打红，而不是让一份对不上的说明静静躺在库里。

## 为什么要有 `.src.pptx`

用来**演示前后差在哪**。成品是 16:9 + 绢青陈墨；源稿是 python-pptx 的
空白 4:3 默认母版 —— 也就是"整条链路本来能给你的东西"。
两张对照一看就知道设计资产在管什么。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation                      # noqa: E402
from pptx.chart.data import CategoryChartData      # noqa: E402
from pptx.enum.chart import XL_CHART_TYPE          # noqa: E402
from pptx.util import Inches, Pt                   # noqa: E402

from rongke import deckref                         # noqa: E402
from rongke.config import Config                   # noqa: E402
from rongke.deck import DeckAuditor, compose_deck, write_slide_notes  # noqa: E402


# ── 图表数据（只有这两页有图）。与 `REFERENCE_DECK` 的正文口径一致 ──────
_CHART_SPECS: dict[int, dict] = {
    # 第 8 页 · 原基出现率
    8: {
        "categories": ["4℃", "8℃", "12℃"],
        "series": [("7d", (0.0, 0.0, 0.0)),
                   ("14d", (0.78, 0.61, 0.0)),
                   ("21d", (0.81, 0.74, 0.0))],
        "title": "原基出现率",
    },
    # 第 9 页 · 转化率随时长
    9: {
        "categories": ["7d", "14d", "21d"],
        "series": [("4℃", (0.05, 0.42, 0.68)),
                   ("8℃", (0.03, 0.31, 0.55)),
                   ("12℃", (0.0, 0.0, 0.01))],
        "title": "转化率",
    },
}


def _set_ph(slide, idx: int, text: str) -> bool:
    """把第 idx 号占位符填上文本。没有这个占位符就返回 False（**不静默吞掉**）。"""
    for ph in slide.placeholders:
        if int(ph.placeholder_format.idx) == idx:
            ph.text = text
            return True
    return False


def _textbox(slide, text: str, w_in: float = 4.0, h_in: float = 2.0):
    """加一个普通文本框（在源稿里的位置/尺寸**无所谓** —— 精排会搬走它）。

    这里故意用粗糙的默认几何：正因为"精排会把它搬到设计网格上"，
    源稿的几何本来就不该是模型要操心的事。
    """
    tb = slide.shapes.add_textbox(Inches(0.6), Inches(3.0),
                                  Inches(w_in), Inches(h_in))
    tf = tb.text_frame
    tf.word_wrap = True
    lines = [x for x in str(text).split("\n") if x.strip()]
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = ln
    return tb


def build_source(path: Path) -> Path:
    """按 `REFERENCE_DECK` 造一份**未排版的源稿**（4:3 + 默认主题）。

    ⚠️ **占位符类型决定落位**（`deck.kind_of` 只认形状/占位符类型，不猜内容）。
       真踩过：section 页原先用两个普通文本框装「说明 + 编号」，
       而 `subtitle` slot 只接受 `subTitle`/`body` ⇒ 它一个都没匹配到，
       于是贪婪的 `anchor` slot（`from=("other",)`）把**说明**吃掉、
       把**编号**剩在版外且没设对齐 ⇒ 复测当场报「2 页仍有文字没设显式对齐」。
       ⇒ 凡是要落进 `subTitle`/`body` slot 的，就必须是**真的 body 占位符**。
    """
    prs = Presentation()                      # python-pptx 默认模板 = 空白 4:3
    for n, item in enumerate(deckref.REFERENCE_DECK, 1):
        kind = str(item["kind"])
        lines = [str(x) for x in item.get("lines", [])]

        if kind == "cover":
            # 「标题幻灯片」版式：idx0 = ctrTitle，idx1 = subTitle
            sl = prs.slides.add_slide(prs.slide_layouts[0])
            sl.shapes.title.text = str(item["title"])
            if lines and not _set_ph(sl, 1, lines[0]):
                raise RuntimeError(f"第 {n} 页（cover）没有 subTitle 占位符")
            if len(lines) > 1:
                _textbox(sl, "\n".join(lines[1:]), w_in=4.0, h_in=0.6)
            continue

        if kind in ("section", "content", "closing"):
            # 「标题和内容」版式：idx0 = title，idx1 = body。
            # **必须用真 body 占位符** —— 否则 subtitle/body slot 匹配不到（见 docstring）。
            sl = prs.slides.add_slide(prs.slide_layouts[1])
            sl.shapes.title.text = str(item["title"])
            if lines and not _set_ph(sl, 1, lines[0]):
                raise RuntimeError(f"第 {n} 页（{kind}）没有 body 占位符")
            # section 多一个「巨型编号」放 anchor slot
            if kind == "section" and len(lines) > 1:
                _textbox(sl, lines[1], w_in=2.0, h_in=1.6)
            continue

        # two-col / chart：「仅标题」起手，其余形状由我们决定（可复现）
        sl = prs.slides.add_slide(prs.slide_layouts[5])
        sl.shapes.title.text = str(item["title"])

        if kind == "chart":
            spec = _CHART_SPECS[n]
            cd = CategoryChartData()
            cd.categories = list(spec["categories"])
            for nm, vals in spec["series"]:
                cd.add_series(str(nm), tuple(float(v) for v in vals))
            gf = sl.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,
                                     Inches(0.6), Inches(1.2),
                                     Inches(5.0), Inches(3.4), cd)
            gf.chart.has_title = True
            gf.chart.chart_title.text_frame.text = spec["title"]
            if lines:
                _textbox(sl, lines[0], w_in=3.0, h_in=3.4)
        else:                                  # two-col
            for ln in lines:
                _textbox(sl, ln)

    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return path


def _render_sheet(pptx: Path, png: Path) -> str:
    """分页预览图。失败**不算致命** —— 它是给人看的，不是承重件。

    分页 PNG 落到 `runs/_deck_reference/`（gitignore 内、可再生），
    库里只留拼好的那张 sheet。
    """
    try:
        import deck_preview
    except Exception as e:                    # noqa: BLE001
        return f"⚠ 预览图没生成（导不进 deck_preview：{type(e).__name__}）"
    tmp = ROOT / "runs" / "_deck_reference"
    try:
        pngs, err = deck_preview.render_pngs(pptx, tmp)
    except Exception as e:                    # noqa: BLE001 —— 兜全部，只为不阻断
        return f"⚠ 预览图没生成（{type(e).__name__}: {e}）"
    if err:
        return (f"⚠ 预览图没生成（{err}）。"
                f"这**不影响** .pptx 与 .md —— 那两样才是承重件。")
    err2 = deck_preview.make_grid(
        pngs, png, cols=4, label="参考样例 · reference_v1（16:9 · 绢青陈墨 · 六种页型）")
    if err2:
        return f"⚠ 预览图没生成（{err2}）"
    return f"✔ 预览图 {png.name}（{len(pngs)} 页拼成）"


def _render_compare(src: Path, dst: Path, out_png: Path, pairs: int = 4) -> str:
    """前后对照图：**同一批内容**，上排 = 未排版源稿，下排 = 精排成品。

    为什么值得单独做一张：`.gitignore` 里那句「可再生的参考产物」之所以成立，
    靠的就是"能一眼看出差在哪"。两张各自看，看不出；**并排看，一眼就看出**。
    这也是回答用户那句「**那能做的这么丑**」最直接的一张图。
    """
    try:
        import deck_preview
        from PIL import Image, ImageDraw
    except Exception as e:                    # noqa: BLE001
        return f"⚠ 对照图没生成（{type(e).__name__}）"
    tmp = ROOT / "runs" / "_deck_reference_compare"
    a, e1 = deck_preview.render_pngs(src, tmp / "src")
    b, e2 = deck_preview.render_pngs(dst, tmp / "dst")
    if e1 or e2 or not a or not b:
        return f"⚠ 对照图没生成（{e1 or e2 or '没有图'}）"

    n = max(1, min(int(pairs), len(a), len(b)))
    W = 760                                   # 每页统一缩放到这个宽
    colgap, rowgap, pad = 16, 12, 22
    head = 30

    def _row(paths_: list[Path]) -> tuple[list[Image.Image], int]:
        out, h = [], 0
        for p in paths_[:n]:
            with Image.open(p) as im:
                im = im.convert("RGB")
                hh = int(round(im.height * W / im.width))
                im = im.resize((W, hh), Image.LANCZOS)
                out.append(im)
                h = max(h, hh)
        return out, h

    top, ht = _row(a)
    bot, hb = _row(b)
    canvas = Image.new("RGB",
                       (pad * 2 + n * W + (n - 1) * colgap,
                        head * 2 + ht + hb + rowgap * 3),
                       (238, 236, 230))
    d = ImageDraw.Draw(canvas)
    y = pad
    d.text((pad, y), f"① 未排版源稿（4:3 · Office 默认母版）—— 整条链路本来给你的东西",
           fill=(150, 60, 40))
    y += head
    for i, im in enumerate(top):
        canvas.paste(im, (pad + i * (W + colgap), y))
    y += ht + rowgap * 2
    d.text((pad, y), "② 精排成品（16:9 · 绢青陈墨 · 字号阶梯 · 三区网格）—— 现在给你的东西",
           fill=(60, 110, 70))
    y += head
    for i, im in enumerate(bot):
        canvas.paste(im, (pad + i * (W + colgap), y))

    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png)
    return f"✔ 对照图 {out_png.name}（各取前 {n} 页，上=源稿、下=成品）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sheet", action="store_true", help="跳过预览图渲染")
    args = ap.parse_args()

    out = deckref.REFERENCE_DIR
    out.mkdir(parents=True, exist_ok=True)

    # ① 源稿
    build_source(deckref.REFERENCE_SRC_PPTX)
    print(f"① 源稿   {deckref.REFERENCE_SRC_PPTX}")

    # ② 精排（**就地**：compose_deck 没有 in/out 两个路径的语义）
    shutil.copy2(deckref.REFERENCE_SRC_PPTX, deckref.REFERENCE_PPTX)
    r = compose_deck(
        deckref.REFERENCE_PPTX,
        deckref.REFERENCE_PAGES,
        theme=deckref.REFERENCE_THEME,
        footer=True,
        footer_label=deckref.REFERENCE_FOOTER_LABEL,
        workdir=ROOT,
    )
    print(f"② 精排   ok={r.ok}  {r.aspect_before} → {r.aspect_after}"
          f"  页数={len(r.kinds)}  视觉={r.visuals_before}→{r.visuals_after}"
          f"  显式对齐={r.align_text}")
    if not r.ok:
        print(r.format_for_model())
        return 1

    # ③ 讲稿备注（**必须在精排之后** —— 精排会重写整包，见下面的实测）
    cfg = Config.load(ROOT)
    notes = [str(x.get("note") or "") for x in deckref.REFERENCE_DECK]
    nr = write_slide_notes(deckref.REFERENCE_PPTX, notes,
                           workdir=ROOT, cfg=cfg.deck)
    print(f"③ 备注   ok={nr.ok}  {getattr(nr, 'filled', '?')}"
          f"/{getattr(nr, 'slides', '?')} 页")
    if not nr.ok:
        print(nr.format_for_model())
        return 1

    # ④ 体检（**参考样例自己必须干净** —— 否则它示范的就是错的东西）
    #  ⚠️ `report_path` 必须显式给：审计器默认把原始 JSON 写在 **pptx 所在目录**
    #     （`deck.py: raw_json = p.parent / "deck_audit.json"`），
    #     不改的话会把 `deck_audit.json` 扔进随库发布的 `docs/deck-reference/`。
    au = DeckAuditor(python_exe=getattr(cfg.deck, "python", "") or None,
                     skills_dir=getattr(cfg.deck, "skills_dir", "") or None,
                     cfg=cfg.deck)
    _aud_out = ROOT / "runs" / "_deck_reference"
    _aud_out.mkdir(parents=True, exist_ok=True)
    rep = au.audit(deckref.REFERENCE_PPTX, workdir=ROOT,
                   report_path=_aud_out / "reference_v1.audit.json")
    print(f"④ 体检   verdict={rep.verdict}  "
          f"阻断={len(rep.blockers)}  提示={len(rep.warnings)}")
    print("   门：" + "  ".join(f"{g.name}={g.verdict}" for g in rep.gates))
    if not rep.ok or rep.verdict == "FIX BEFORE DELIVERY":
        for b in rep.blockers:
            print("   ✗", b)
        print("   ⛔ 参考样例自己没过门 ⇒ 不许发布。先把它修干净。")
        return 1
    for w in rep.warnings:
        print("   ⚠", w)
    if rep.warnings:
        print("   ⚠ 参考样例带了提示项 —— **够用但不理想**："
              "发给模型的样例最好零提示，否则它在示范「这样也算过」。")

    # ⑤ 说明落盘（**由代码生成**，不手写）
    # ⚠️ `newline="\n"` **不能省**：Windows 的文本模式会把 `\n` 翻成 `\r\n`，
    #    而 `.gitattributes` 写的是 `* text=auto eol=lf` —— 于是这份**随库产物**
    #    会在工作区里变成 CRLF，让 `eol_test` 的 §4.2（"没有一个 CRLF"）报红。
    #    实测：省掉它，这个文件是 148 行 CRLF。
    #    ⇒ 换行策略是**生成器的责任**，不是靠事后 `--renormalize` 补救。
    deckref.REFERENCE_MD.write_text(
        deckref.reference_text(summary=False) + "\n", encoding="utf-8", newline="\n")
    print(f"⑤ 说明   {deckref.REFERENCE_MD}")

    # ⑥ 预览图
    if args.no_sheet:
        print("⑥ 预览图 跳过（--no-sheet）")
    else:
        print("⑥ 预览图", _render_sheet(deckref.REFERENCE_PPTX,
                                        deckref.REFERENCE_SHEET_PNG))
        print("⑦ 对照图", _render_compare(deckref.REFERENCE_SRC_PPTX,
                                          deckref.REFERENCE_PPTX,
                                          deckref.REFERENCE_COMPARE_PNG))

    print("\n═══ 参考样例已构建 ✔ ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
