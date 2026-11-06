# -*- coding: utf-8 -*-
"""样例稿跑手：把 `demo-spec.json`（逐页逐槽的文字）变成一份**真的 .pptx**，再验三遍。

## 为什么要有这个工具（2026-09-22）

接入一份新模板只有三步：**抽槽位表 → 写页型判断（kinds.json）→ 真出一份稿**。
前两步都有工具、有出生自检；第三步原先只在纸上"算过了"。而样例稿是唯一能把
「我用的是这份模板的版式」变成**看得见的事**的东西，也是下一份模板接入时的**页序范例**。

## 一份 spec + 三道互不同源的验收

    ① 出稿        `deck_from_template`（保真路线：留模板页、填字、删多余页）
    ② 逐槽读回    **用抽取端那把尺子**（`deck_template_extract.flat_shapes` /
                  `diagram_slots`）从磁盘重开读回来对
    ③ 全稿扫残留  整份 zip（所有 slide + SmartArt 部件）里不许再有「请输入…」

②③ 刻意**不复用**① 的判据：

  · ① 走填充端自己的 `_flat`；② 走抽取端的 `flat_shapes`。**两端对"第 i 个槽"的理解
    若有一天分家，只有 ② 抓得住**（两端各抄一份摊平实现 = H 型）。
  · ③ 一个编号都不看，直接在成品字节上搜模板的占位指纹 —— 这是唯一能抓住
    「某个部件工具压根不知道自己该填」的尺子（zjut 当年就栽在 SmartArt 上）。

## 用法

    python deck_template_demo.py docs/deck-templates/moganshan/demo-spec.json
    python deck_template_demo.py <spec.json> --no-preview
    python deck_template_demo.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 模板占位指纹的兜底（真正的清单住在 `kinds.json` 的 `rules` 里，随模板走）
HINT_FALLBACK = ["请输入文本", "请输入内容", "请输入标题", "请输入具体内容",
                 "这里请放入", "请输入"]
#: 扫残留时要看的部件：正文页 + 讲义备注 + SmartArt 数据
SWEEP_PAT = ("ppt/slides/slide", "ppt/notesSlides/notesSlide",
             "ppt/diagrams/data")


def _reimport() -> None:
    """自己的解释器里没有 python-pptx 就换一个重跑（**要说出口，不静默**）。"""
    global D, EXT, SF
    from rongke import deck as _D
    import deck_template_extract as _EXT
    from rongke import slotfit as _SF
    D, EXT, SF = _D, _EXT, _SF


def load_spec(p: Path) -> dict:
    d = json.loads(p.read_text(encoding="utf-8"))
    for k in ("template_name", "pages"):
        if not d.get(k):
            raise SystemExit(f"⛔ spec 缺 `{k}`：{p}")
    return d


def hints_of(tname: str) -> list[str]:
    """占位指纹：优先读模板自己的 `kinds.json`（与抽取器同源）。"""
    ks = ROOT / "docs" / "deck-templates" / tname / "kinds.json"
    if ks.is_file():
        try:
            rules = (json.loads(ks.read_text(encoding="utf-8")) or {}).get("rules") or {}
            out = list(rules.get("title_hints") or []) + list(rules.get("body_hints") or [])
            if out:
                return out
        except Exception:
            pass
    return HINT_FALLBACK


def read_slots(s) -> dict:
    """从磁盘上那份产物里把**一页**的槽读回来（`s` 是 slide 对象）。

    ⚠️ 用的是抽取端的尺子，但 SmartArt 那部分**必须换一条路去定位部件**（2026-09-22 实测）：
    产物里 `part.partname` 报的是 `slide1..slideN`，而 zip 的成员名是**模板源页号**
    （实测 12 页产物：成员为 slide1/2/3/4/7/13/16/29/42/46/59/61）—— 照 `partname` 拼
    `slideN.xml.rels` **一条都读不到**（本轮就是这么栽的，好在它响亮地失败了）。

    **根因**（本仓 `rongke/deck.py` 里那段血债）：`Presentation.slides` 是个
    **带副作用的 `@lazyproperty`** —— 光读一下就调 `PresentationPart.rename_slide_parts()`，
    把分片名**就地**改成 `slide1..N`；**不是 `save()` 干的**。所以调用方一律走
    `deck._slides_by_rid(prs)`，`partname` 就永远不可信。
    ⇒ 这里改用**部件自己的关系**（`reltype` 以 `/diagramData` 结尾）定位；
    编号本身仍来自抽取器的唯一实现 `diagram_slots_from_parts`。
    """
    out: dict[str, str] = {}
    for i, sh in enumerate(EXT.flat_shapes(s.shapes)):
        try:
            if sh.has_text_frame and sh.text_frame.text.strip():
                out[str(i)] = sh.text_frame.text
        except Exception:
            pass
    pairs = []
    for rel in s.part.rels.values():
        if not str(rel.reltype).endswith("/diagramData"):
            continue
        try:
            pairs.append((str(getattr(rel, "target_ref", "diagram")),
                          rel.target_part.blob.decode("utf-8", "replace")))
        except Exception:
            continue
    for sl in EXT.diagram_slots_from_parts(pairs):    # SmartArt：键 `d<i>`
        out[str(sl["i"])] = sl.get("sample") or ""
    return out


def sweep(path: Path, hints: list[str]) -> list[tuple[str, str]]:
    """整份 zip 里搜占位指纹。返回 [(部件, 命中文本)]。"""
    hits: list[tuple[str, str]] = []
    with zipfile.ZipFile(str(path)) as zf:
        for name in zf.namelist():
            if not name.startswith(SWEEP_PAT) or not name.endswith(".xml"):
                continue
            try:
                x = zf.read(name).decode("utf-8", "replace")
            except Exception:
                continue
            for m in EXT._re.finditer(r"<a:t>([^<]*)</a:t>", x):
                t = m.group(1)
                for h in hints:
                    if h and h in t:
                        hits.append((name, t[:40]))
                        break
    return hits


def tightness(spec: dict, tname: str) -> list[tuple[str, int, int, float, float]]:
    """填完之后**还剩多少余量**（给下一位作者看的）。

    取两个方向的较大者 —— **只看行数会骗人**：单行槽永远是 `1/1 = 100%`，
    看着最紧，其实一个字都没顶满（`展示人` 只占每行容量的 60%）。
    所以还要看**最宽那一行占每行容量的比例**。
    """
    tpl = D.template_pages(D.load_template(tname))
    rows: list[tuple[str, int, int, float, float]] = []
    for pg in spec["pages"]:
        info = tpl.get(pg["page"])
        if not info:
            continue
        by_i = {str(s["i"]): s for s in info["slots"]}
        for k, txt in (pg.get("slots") or {}).items():
            s = by_i.get(str(k))
            if not s or not s.get("cap_cpl"):
                continue
            txt = txt if isinstance(txt, str) else "\n".join(txt)
            paras = SF.as_paras(txt)
            used = SF.needed_lines(paras, s["cap_cpl"])
            cap_l = int(s.get("cap_lines") or 1)
            # ⚠️ 横向比例要**截断在 1.0**：段子长过一行时它会**换行**，
            #    换行后的每一行按定义都不超过每行容量 —— 拿整段的 em 去除，
            #    会得出 299% 这种没意义的数（本轮先写错了一次）。
            wid = min(1.0, max((SF.em_width(x) for x in paras), default=0.0)
                      / float(s["cap_cpl"]))
            # ★ 看**约束维度**，不要两个取 max：
            #   · 框只能装一行（`cap_lines == 1`）⇒ 横向是唯一余量
            #     （取 max 会让所有单行槽都印 100%，看着最紧、其实一个字没顶满）；
            #   · 框能换行 ⇒ 横向由换行兜住，**垂直行数**才是真正会爆的那一维。
            r = wid if cap_l <= 1 else used / float(cap_l)
            rows.append((f"{pg['page']}[{k}]", used, cap_l, wid, r))
    rows.sort(key=lambda r: -r[4])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec", nargs="?", help="demo-spec.json 的路径")
    ap.add_argument("--list", action="store_true", help="列出仓库里所有样例稿 spec")
    ap.add_argument("--no-preview", action="store_true", help="不渲染网格图")
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--width", type=int, default=1400)
    a = ap.parse_args()

    if a.list:
        found = sorted((ROOT / "docs" / "deck-templates").glob("*/demo-spec.json"))
        if not found:
            print("（还没有任何样例稿 spec）")
        for f in found:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                n = len(d.get("pages") or [])
                print(f"  {f.relative_to(ROOT)}  ｜ {d.get('template_name')} ｜ {n} 页")
            except Exception as e:
                print(f"  {f.relative_to(ROOT)}  ⛔ JSON 坏了：{e}")
        return 0

    if not a.spec:
        ap.error("要么给 spec 路径，要么 --list")

    # ── 解释器：读回复测要 python-pptx；没有就换个解释器**重跑自己** ──
    _reimport()
    if not D.has_python_pptx(sys.executable):
        py = D.find_notes_python()
        if not py:
            print("⛔ 找不到装了 python-pptx 的解释器 —— 出不了稿（别反复重试）。")
            return 2
        if os.environ.get("RK_DEMO_RELAUNCH"):
            print(f"⛔ 换到 {py} 之后仍然没有 python-pptx。")
            return 2
        print(f"↻ 当前解释器没有 python-pptx，换 {py} 重跑。")
        env = dict(os.environ, RK_DEMO_RELAUNCH="1")
        return subprocess.run([py, str(Path(__file__).resolve()), *sys.argv[1:]],
                              env=env, cwd=str(ROOT)).returncode

    from pptx import Presentation

    specp = Path(a.spec)
    if not specp.is_absolute():
        specp = ROOT / specp
    spec = load_spec(specp)
    tname = spec["template_name"]
    out = Path(spec["out"])
    if not out.is_absolute():
        out = ROOT / out

    print(f"═══ 样例稿 · 模板 {tname} · {len(spec['pages'])} 页 ═══")

    # ── 模板 .pptx 的路径：**以槽位表自己记的 `source` 为准**（单源）──
    #   表是从哪份 pptx 抽出来的，就在哪份上填字 —— 不另外找一处配置来"猜"。
    tbl = D.load_template(tname)
    tpl_pptx = str(spec.get("template_pptx") or tbl.get("source") or "").strip()
    if not tpl_pptx or not Path(tpl_pptx).is_file():
        print(f"⛔ 槽位表记的模板 .pptx 不在盘上：{tpl_pptx!r}\n"
              f"   ⇒ 先重抽槽位表（`deck_template_extract.py <pptx> --kinds …`），"
              f"或把模板放回该路径。")
        return 2

    # 与 `config.json` 那「一对」交叉核对：`template`（.pptx）与 `template_name`
    # （槽位表名）**只改一处**会造出「清单说 A 的页型、出稿拿 A 的页号去 B 的稿上填」
    # —— 两边都跑得通、只是内容错位，是本项目最难查的一类错。
    try:
        from rongke.config import Config
        c = Config.load(ROOT)
        cn = D.template_name_of(c.deck)
        cp = str(getattr(c.deck, "template", "") or "")
        if cn != tname:
            print(f"⚠️ config.json 指到槽位表 `{cn}`，本 spec 用的是 `{tname}`。\n"
                  f"   出稿不受影响（上面按表里的 source 走），但**体检门扣模板基线会按 "
                  f"`{cn}`** —— 两者不一致时会扣错模板的 findings。")
        elif cp and Path(cp).resolve() != Path(tpl_pptx).resolve():
            print(f"⚠️ config.json 的 `deck.template` 指到 {cp}，\n"
                  f"   而槽位表是从 {tpl_pptx} 抽的 —— 这一对**只改了一处**。")
    except Exception as e:
        print(f"（跳过 config 交叉核对：{type(e).__name__}: {e}）")

    print(f"   模板本体：{tpl_pptx}")
    res = D.deck_from_template(str(out), spec["pages"], template_pptx=tpl_pptx,
                               template_name=tname, workdir=str(ROOT))
    print(res.format_for_model())
    if not res.ok:
        print("\n⛔ 出稿没成 —— 上面 `problems` 里逐条写着原因。")
        return 1

    d = res.to_dict()
    print(f"\n  写入 {d['bytes_after']:,} B ｜ 模板 {d['slides_before']} 页 → 产物 "
          f"{d['slides_after']} 页 ｜ 填了 {len(d['filled'])} 槽"
          f"（缩号 {len(d['shrunk'])} 处）")

    # ── ② 逐槽读回（抽取端的尺子）───────────────────────────
    if not out.is_file():
        print("⛔ 产物不在盘上（自称成功不算数）。")
        return 1
    prs = Presentation(str(out))
    # ⚠️ **不许读 `prs.slides`**：它是个**带副作用的 `@lazyproperty`** —— 读一下就调
    #    `PresentationPart.rename_slide_parts()`，把分片名**就地**改成 `slide1..N`
    #    （**不是 `save()` 干的**，见 `rongke/deck.py` 里那段血债）。
    #    路线甲靠「分片号 == 模板源页号」做模板基线扣除，碰了 `.slides` 那套就废了。
    #    `_NOTES_SCRIPT` 里有一份同名同构的实现（它是子进程脚本，自带一份）；
    #    这里按 rId 直取，**三行库胶水**，刻意不引 `rongke.deck` 的私有名。
    _lst = prs.part._element.get_or_add_sldIdLst()
    slides = [prs.part.related_slide(_s.rId) for _s in _lst]
    n_slide = len(slides)
    if n_slide != len(spec["pages"]):
        print(f"⛔ 产物 {n_slide} 页 ≠ spec {len(spec['pages'])} 页。")
        return 1
    bad: list[str] = []
    n_ck = 0
    for k, pg in enumerate(spec["pages"]):
        got = read_slots(slides[k])
        for key, want in (pg.get("slots") or {}).items():
            want = want if isinstance(want, str) else "\n".join(want)
            have = got.get(str(key))
            n_ck += 1
            if have is None:
                bad.append(f"第{k+1}页 {pg['page']} 槽{key}：**读不到**（形状里没这个槽）")
            elif have.strip() != want.strip():
                bad.append(f"第{k+1}页 {pg['page']} 槽{key}：\n"
                           f"      给的是 {want.strip()[:60]!r}\n"
                           f"      读回来 {have.strip()[:60]!r}")
    print(f"\n[② 逐槽读回] {n_ck} 槽，用抽取端的 `flat_shapes` / `diagram_slots_from_parts` "
          f"从磁盘重开读回来的（SmartArt 按**关系**定位部件，不按 partname）：")
    if bad:
        for x in bad[:20]:
            print("   ✘", x)
        print(f"   ⇒ {len(bad)} 处对不上。**不继续**（先修，别拿它去体检）。")
        return 3
    print("   ✔ 逐槽与 spec 完全一致")

    # ── ③ 全稿扫残留占位文本 ───────────────────────────────
    hints = hints_of(tname)
    hits = sweep(out, hints)
    print(f"\n[③ 残留扫描] 整份 zip 搜 {len(hints)} 个占位指纹"
          f"（{'/'.join(hints[:3])}…，与抽取器同源）：")
    if hits:
        for part, t in hits[:20]:
            print(f"   ✘ {part}：{t!r}")
        print(f"   ⇒ {len(hits)} 处残留 —— 交出去就是把模板的占位文字印在成品上。")
        return 4
    print("   ✔ 0 处残留")

    # ── 余量：给下一位作者 ──────────────────────────────────
    rows = tightness(spec, tname)
    if rows:
        print("\n[余量] 最紧的 6 个槽（行 `用/上限` ｜ 最宽行占每行容量 ｜ 合起来）：")
        for name, used, cap_l, wid, r in rows[:6]:
            bar = "█" * int(round(r * 10))
            print(f"   {name:24s} {used:>2}/{cap_l:<2} {wid*100:4.0f}% "
                  f"{bar:<10s} {r*100:4.0f}%")

    # ── 渲染：让"长什么样"变成看得见的事 ────────────────────
    if not a.no_preview:
        sheet = out.with_name(out.stem + "-sheet.png")
        r = subprocess.run([sys.executable, str(ROOT / "deck_preview.py"), str(out),
                            "-o", str(sheet), "--cols", str(a.cols),
                            "--width", str(a.width)],
                           capture_output=True, cwd=str(ROOT))
        tail = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        if r.returncode == 0 and sheet.is_file():
            print(f"\n[渲染] {sheet.relative_to(ROOT)}"
                  f"（{sheet.stat().st_size:,} B）")
            print("   ⇒ **看图验收**：判据只看得到 XML，证明不了好看。")
        else:
            print("\n[渲染] 没成（非阻断）—— "
                  + " ｜ ".join(tail[-2:] or [(r.stderr or b'').decode('utf-8', 'replace')[-200:]]))

    print(f"\n[OK] 样例稿：{out.relative_to(ROOT)}")
    print("   ⚠️ 这只是**出稿**，还没体检 —— 交付前仍要 `audit_deck`。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
