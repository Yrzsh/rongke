# -*- coding: utf-8 -*-
"""量真实 pptx 语料里「逐页视觉元素」的分布。

## 为什么要有这个脚本

调研（`docs/ppt-skill-survey-20260921-part2.md` §2.1）认定的剩余缺口是：
`no_visual_elements` 是**整份 deck 的聚合判据**（`slide_count>=10 且 图+表+图表==0`），
于是一份「11 页纯文字 + 2 张图」的 deck **从它下面走过去了** —— 而那正是被吐槽的
那份 13 页成片的形状。

我当时提的候选补丁是：**「某页正文含数字、而该页没有任何视觉元素 ⇒ WARN」**。

⚠️ 但这条判据**当场就该被否掉**，因为它撞一下封面页就碎：

  | 页面        | 含数字?            | 有图? | 它正确吗 |
  |-------------|--------------------|-------|----------|
  | 封面 / 扉页 | `2026-09-21`       | 无    | **正确** |
  | 目录页      | `1 背景 2 方法 3…` | 无    | **正确** |
  | 结论页      | `准确率 92%`       | 无    | **正确** |
  | 章节分隔页  | `第二部分`         | 无    | **正确** |

  ⇒ 它会在**几乎每一份正常 deck** 上永久报 WARN。
  ⇒ 而「一条永远亮着的警告等于没有警告」（设计定律 10：误报比漏报更伤信任）。
  ⇒ 本项目刚刚在 `layout_top_share > 0.7` 上吃过一模一样的亏（见 `rongke/deck.py`
     1663–1674 行的判据修正注释）。

## 这个脚本要回答的问题

不问"这条判据写得漂不漂亮"，只问三个**可复算**的问题：

  Q1. 真实产物里，逐页「无视觉元素」的**占比**是多少？
      —— 如果真实 deck 本身就是 80% 纯文字页，那么**任何**逐页判据都是噪音项，
         缺口不在"判据不够细"，而在别的地方（例如模型压根没伸手拿 `figure_render`）。
  Q2. 如果按「占比 ≥ X」来判，X 取多少才不会误伤真实产物？
  Q3. **合成夹具**（`runs/_trash/deck-type.*` 等）会被这条判据打红吗？
      —— 夹具是既有断言的分母。打红夹具 = 改判据就要改一堆测试 = 成本远大于收益。

## 与 `rongke/deck.py` 的关系（单源纪律）

本脚本里的 XML 解析是 `probe_pptx` 的**镜像**，不是调用 —— 因为
`probe_pptx` 目前**只累加 deck 总量、没有逐页计数**（`rongke/deck.py` 505–555 行），
正是这次要论证要不要补的东西。**改完 `probe_pptx` 后本脚本应当改回直接调用它**，
否则就是 H 型（同一件事两处实现，改一处忘一处 ⇒ 静默漂移）。
"""
from __future__ import annotations

import re
import sys
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]          # 仓库根
RUNS = ROOT / "runs"

_SLIDE_RE = re.compile(r"ppt/slides/slide(\d+)\.xml$")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _slide_no(name: str) -> int:
    m = _SLIDE_RE.search(name)
    return int(m.group(1)) if m else 0


def _shape_text(node) -> str:
    out: list[str] = []
    for t in node.iter():
        if _local(t.tag) == "t" and t.text:
            out.append(t.text)
    return "".join(out)


def probe(path: Path) -> dict | None:
    """返回 {deck, slide_count, pages:[{no,visuals,text_chars,has_title}]}。

    ⚠️ 读不动就返回 None —— **不把"没查"渲染成 0**（M 型）。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            slides = sorted((n for n in names if _SLIDE_RE.match(n)), key=_slide_no)
            if not slides:
                return None
            pages = []
            for name in slides:
                try:
                    root = ET.fromstring(zf.read(name))
                except Exception:
                    return None
                visuals = 0
                text_parts: list[str] = []
                has_title = False
                for node in root.iter():
                    tag = _local(node.tag)
                    if tag in ("pic", "tbl", "chart"):
                        # 与 probe_pptx 同口径：按标签各自数一次，
                        # 只求"有没有"，不求精确归属（见 deck.py 519–527 行）
                        visuals += 1
                    if tag not in ("sp", "graphicFrame"):
                        continue
                    text_parts.append(_shape_text(node))
                    for ph in node.iter():
                        if _local(ph.tag) == "ph":
                            t = (ph.get("type") or "")
                            if t in ("title", "ctrTitle"):
                                has_title = True
                text = "".join(text_parts)
                pages.append({"no": _slide_no(name), "visuals": visuals,
                              "text_chars": len(text),
                              "has_digit": bool(re.search(r"\d", text)),
                              "has_title": has_title})
            return {"deck": str(path.relative_to(ROOT)), "slide_count": len(pages),
                    "pages": pages}
    except Exception:
        return None


def main() -> int:
    if not RUNS.exists():
        print(f"[FAIL] 找不到语料目录：{RUNS}")
        return 2

    files = sorted(RUNS.rglob("*.pptx"))
    decks: list[dict] = []
    unreadable: list[str] = []
    for f in files:
        d = probe(f)
        if d is None:
            unreadable.append(str(f.relative_to(ROOT)))
        else:
            d["bytes"] = f.stat().st_size
            d["trash"] = "_trash" in f.parts
            decks.append(d)

    print(f"语料：扫到 {len(files)} 个 pptx；读得动 {len(decks)}；"
          f"读不动 {len(unreadable)}（**没查**，不计入任何分母）")
    if unreadable:
        print(f"  读不动的前 5 个：{unreadable[:5]}")
    print()

    # ── Q1/Q2：逐页无视觉元素的占比分布 ─────────────────────────────
    print("=" * 78)
    print("Q1/Q2 · 逐页「无视觉元素」占比分布")
    print("=" * 78)
    print(f"{'占比区间':<14}{'deck 数':>8}{'其中真实产物':>14}")
    buckets: Counter = Counter()
    zero_share_of_deck: dict[str, float] = {}
    for d in decks:
        n = d["slide_count"]
        z = sum(1 for p in d["pages"] if p["visuals"] == 0)
        share = z / n if n else 0.0
        zero_share_of_deck[d["deck"]] = share
        lo = min(int(share * 10) * 10, 90)
        buckets[lo] += 1
    for lo in sorted(buckets):
        real = sum(1 for d in decks if not d["trash"]
                   and min(int(zero_share_of_deck[d["deck"]] * 10) * 10, 90) == lo)
        bar = "#" * min(int(buckets[lo] / max(1, len(decks)) * 60), 60)
        print(f"{lo:>3}-{lo+9:<10}{buckets[lo]:>8}{real:>14}  {bar}")
    print()

    # ── 关键：真实产物逐份摊开（合成的只报计数）──────────────────────
    print("=" * 78)
    print("真实产物（非 _trash）逐份摊开 —— 这才是判据该长的形态")
    print("=" * 78)
    reals = sorted((d for d in decks if not d["trash"]), key=lambda d: -d["bytes"])
    for d in reals:
        z = sum(1 for p in d["pages"] if p["visuals"] == 0)
        tot = sum(p["visuals"] for p in d["pages"])
        print(f"\n  {d['deck']}")
        print(f"    {d['bytes']/1024:.0f} KB · {d['slide_count']} 页 · "
              f"视觉元素共 {tot} 个 · 无视觉页 {z}/{d['slide_count']} "
              f"= {z/d['slide_count']:.0%}")
        row = " ".join(f"{p['no']}:{'图'*min(p['visuals'],4) or '-'}"
                       f"({p['text_chars']}字)"
                       for p in d["pages"])
        print(f"    逐页  {row}")
    print()

    # ── Q3：合成夹具会被打红吗（误报分母）──────────────────────────
    print("=" * 78)
    print("Q3 · 候选判据打红面（合成夹具 = 既有断言的分母）")
    print("=" * 78)

    def fire_v1(d: dict) -> bool:
        """候选判据 v1（已被本脚本证否）：某页含数字且无视觉元素。"""
        return any(p["visuals"] == 0 and p["has_digit"]
                   for p in d["pages"] if p["has_title"] or p["text_chars"] > 40)

    def fire_v2(d: dict) -> bool:
        """候选判据 v2：无视觉页占比 ≥ 0.85 且页数 ≥ 8。"""
        if d["slide_count"] < 8:
            return False
        z = sum(1 for p in d["pages"] if p["visuals"] == 0)
        return z / d["slide_count"] >= 0.85

    def fire_v3(d: dict) -> bool:
        """候选判据 v3：把"含数字"换成"长正文页"（>120 字）无视觉页占比 ≥ 0.8。"""
        content = [p for p in d["pages"] if p["text_chars"] > 120]
        if len(content) < 6:
            return False
        z = sum(1 for p in content if p["visuals"] == 0)
        return z / len(content) >= 0.8

    for name, fn in (("v1 逐页:含数字却无图", fire_v1),
                     ("v2 整份:无视觉页占比>=85%", fire_v2),
                     ("v3 长正文页:无视觉占比>=80%", fire_v3)):
        hit = [d for d in decks if fn(d)]
        hit_real = [d for d in hit if not d["trash"]]
        print(f"\n  {name}")
        print(f"    触发 {len(hit)}/{len(decks)} 份（真实产物 {len(hit_real)}）"
              f" ⇒ 打红面 {len(hit)/max(1,len(decks)):.1%}")
        for d in hit_real:
            z = sum(1 for p in d["pages"] if p["visuals"] == 0)
            print(f"      · {d['deck']}  ({d['slide_count']} 页, 无视觉 {z})")

    # ── 既有判据的实测复核 ─────────────────────────────────────────
    print()
    print("=" * 78)
    print("既有判据 `no_visual_elements` 在真实产物上的实测")
    print("=" * 78)
    for d in reals:
        tot = sum(p["visuals"] for p in d["pages"])
        fires = d["slide_count"] >= 10 and tot == 0
        print(f"  {'打红' if fires else '放过'}  {d['deck']:<52} "
              f"{d['slide_count']} 页 / 视觉 {tot} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
