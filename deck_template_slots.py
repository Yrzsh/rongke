# -*- coding: utf-8 -*-
"""看一份**槽位表**里到底有什么（只读工具，给人用的）。

## 为什么它该常驻

`deck_template_extract.py` 把模板抽成表，`deck_from_template` 按表填字 ——
但**人**在中间那一步是瞎的：要写 `demo-spec.json` / 要排页序 / 要判断
"这份模板有没有我要的版式"，都得先把表看一遍。

⚠️ 它原来是一枚 `runs/_regress/_probe_*.py` 的一次性探针，**被
   `sweep_probes()` 扫走过一次** —— 而"接入下一份模板"这件事会反复用到它。
   ⇒ 挪到仓库根、按工具对待（与 `deck_preview.py` / `deck_template_extract.py` 同级）。

## 四种看法

    python deck_template_slots.py                   # 全部页型，一行一页（先看有什么）
    python deck_template_slots.py 封面 目录五项       # 点名页型，逐槽详情（写文案时对着看）
    python deck_template_slots.py --name zjut       # 换一份槽位表（默认取 config.json 的）
    python deck_template_slots.py --filter 图        # 页型名里带这个字的才打印
    python deck_template_slots.py --brief           # 路线 + 开工清单的**整份简报**（喂给别的 agent）

`--brief` 存在的理由（2026-09-22）：`docs/deck-templates/<tpl>/agent-brief.txt` 原先是一份
**手工重定向出来的快照**，全仓没有一个脚本生成它 ⇒ 内核把容量口径从"中文字按字算"
改成"英文数字只占 0.56 个字"之后，**它还在一整代地教错的东西**（两处真相 / H 型）。
⇒ 现在它有生成器了：`--brief [-o 文件]`，两段都直接调内核的渲染器（单源），
`template_test` §3d 断言它**与现渲染逐行相同** ⇒ 再漂移就报红。

**只读**：不碰任何文件（除 `--brief -o` 显式指定的输出）。
⚠️ 前三条**不需要 python-pptx**、两个解释器都能跑；`--brief` 要 `rongke.deck` ⇒ **要它**。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "docs" / "deck-templates"


def current_name() -> str:
    """`config.json` 里 `deck.template_name`（空 ⇒ zjut，与内核同一兜底）。"""
    try:
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return str((raw.get("deck") or {}).get("template_name") or "").strip() or "zjut"
    except Exception:
        return "zjut"


def load(name: str, root: Path | None = None) -> dict:
    f = (root or TEMPLATE_DIR) / name / "slots.json"
    if not f.is_file():
        raise SystemExit(f"槽位表不存在：{f}\n"
                         f"（生成：python deck_template_extract.py <模板.pptx> "
                         f"-o {f} --kinds {f.parent / 'kinds.json'}）")
    return json.loads(f.read_text(encoding="utf-8"))


def flat(pages: dict) -> dict:
    """展开成 `页id → {src, label, assets, slots}`（`xxx-1` / `xxx-2` 各一条）。"""
    out = {}
    for _kind, blk in pages.items():
        for e in blk.get("entries") or []:
            out[e["id"]] = {"src": e["src"], "label": blk.get("label", ""),
                            "assets": e.get("assets") or {},
                            "slots": e.get("slots") or []}
    return out


FILL = ("heading", "label", "body")

#: 简报两段的标题（`agent-brief.txt` 的形态；这里只存一次，守卫与生成器共用）。
BRIEF_HEAD_ROUTE = ("════════ 模型读到的「出稿路线」"
                    "（渲染自 DECK_ROUTE_GUIDE + DECK_TOOLCHAIN_RECIPE）════════")
BRIEF_HEAD_PLAN = ("════════ 模型读到的「品牌模板开工清单」"
                   "（deck_template_plan() 的返回值）════════")


def brief_text(name: str) -> str:
    """渲染简报全文（与 `docs/deck-templates/<tpl>/agent-brief.txt` **同形**）。

    **单源**：两段都直接调内核的渲染器 —— 路线段 `contract.deck_recipe_lines`、
    清单段 `deck.template_plan_text`。⇒ 它**永远不会和内核分家**：内核改了措辞或
    容量口径，重跑一次就同步。（旧那份是**手工重定向**的产物，没有生成器 ⇒
    悄悄漂移了一整代：内核已改成"英文数字只占 0.56 个字"，它还在教"中文字按字算"。）

    ⚠️ 与前三条只读用法不同，**它要 `rongke.deck` ⇒ 要 python-pptx**（重依赖）。
    """
    sys.path.insert(0, str(ROOT))
    from rongke import contract as C          # 延迟导入：顶层保持零依赖
    from rongke import deck as D
    tpl = D.load_template(name)
    return (f"{BRIEF_HEAD_ROUTE}\n\n{C.deck_recipe_lines(indent='')}\n\n"
            f"{BRIEF_HEAD_PLAN}\n\n{D.template_plan_text(tpl)}").rstrip() + "\n"


def overview(d: dict, want: str) -> None:
    rows = flat(d.get("pages") or {})
    nsrc = len({v["src"] for v in rows.values()})
    # ⚠️ 口径要说准（2026-09-22 修）：`rows` 是**页型条目**数，不是"页型种数" ——
    #    `data.pages` 是按**类**键的（如 `章节页`），一类可挂多个源页 ⇒
    #    莫干山实测 **64 类 / 72 个条目**（`章节页`/`时间轴` 等 7 类各占两页）。
    #    印成「72 种页型」会让人以为表里有 72 类，**说出口的话必须准**。
    print(f"【{d.get('template_name') or '?'}】画布 {d.get('aspect')} ｜ "
          f"缺省字号 {d.get('default_pt')}pt ｜ {len(rows)} 个页型条目"
          f"（{len(d.get('pages') or {})} 类）／ {nsrc} 个源页")
    print(f"{'页型 id':<22} {'源页':<8} {'槽':>3} {'可填':>4} {'容量(中文字)':>12} 资产")
    for pid, v in rows.items():
        if want and want not in pid:
            continue
        fl = [s for s in v["slots"] if s.get("role") in FILL]
        caps = [s["cap_chars"] for s in fl if s.get("cap_chars")]
        a = v["assets"]
        own = []
        for k, t in (("pics", "图"), ("tables", "表"), ("charts", "⚠图表"),
                     ("smartart", "SmartArt")):
            if a.get(k):
                own.append(f"{a[k]}{t}")
        rng = f"{min(caps)}–{max(caps)}" if caps else "?"
        print(f"{pid:<22} p{v['src']:<7} {len(v['slots']):>3} {len(fl):>4} "
              f"{rng:>12} {' '.join(own)}")


def detail(d: dict, ids: list[str]) -> None:
    rows = flat(d.get("pages") or {})
    for pid in ids:
        if pid not in rows:
            near = [k for k in rows if k.startswith(pid[:4])]
            print(f"!! 没有页型 {pid!r}；相近的：{near}")
            continue
        v = rows[pid]
        print(f"══ {pid}  (源页 p{v['src']})  {v['label']}")
        print(f"   资产 {v['assets']}  ｜ 缺省字号 {d.get('default_pt')}pt")
        for s in v["slots"]:
            role = s.get("role", "")
            mark = "▸" if role in FILL else " "
            print(f"   {mark}[{str(s.get('i')):>4}] {role:8s}"
                  f" 上限{str(s.get('cap_chars')):>4}字"
                  f"/{int((s.get('cap_chars') or 0) * 1.8):>4}字符"
                  f"  ({s.get('cap_cpl') and round(s['cap_cpl'], 2)}em×"
                  f"{s.get('cap_lines')}行)"
                  f" {s.get('w')}×{s.get('h')}in {s.get('pt')}pt"
                  f" 原文 {s.get('sample')!r}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description="看槽位表（只读）")
    ap.add_argument("ids", nargs="*", help="要点名看的页型 id；不给就打概览")
    ap.add_argument("--name", default="", help="哪份槽位表（默认 config.json 里的）")
    ap.add_argument("--filter", default="", help="概览里只显示名字含它的页型")
    ap.add_argument("--dir", default=str(TEMPLATE_DIR), help="槽位表根目录")
    ap.add_argument("--brief", action="store_true",
                    help="打印「路线 + 开工清单」简报全文（与 agent-brief.txt 同形；需 python-pptx）")
    ap.add_argument("-o", "--out", default="",
                    help="与 --brief 合用：写进文件（显式 LF）而不是只打印")
    a = ap.parse_args()
    name = a.name or current_name()
    if a.brief:
        try:
            txt = brief_text(name)
        except ImportError as e:                       # 缺 python-pptx ⇒ 换解释器，别装包
            print(f"⚠️ --brief 要 `rongke.deck`（依赖 python-pptx）而它不在这个解释器里：{e}\n"
                  f"   ⇒ 换 3.12 解释器重跑（本仓惯例：缺依赖先换解释器，绝不全局 pip install）。")
            return 2
        if a.out:
            # ★ 显式 newline="\n"：Windows 文本模式默认 CRLF ⇒ 违反 .gitattributes 的
            #   `* text=auto eol=lf` ⇒ eol_test 报红（本文件的兄弟脚本栽过一次）。
            Path(a.out).write_text(txt, encoding="utf-8", newline="\n")
            print(f"[OK] 已写 {a.out}（{len(txt)} 字符 / {txt.count(chr(10))} 行）")
        else:
            sys.stdout.write(txt)
        return 0
    d = load(name, Path(a.dir))
    if a.ids:
        detail(d, a.ids)
    else:
        overview(d, a.filter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
