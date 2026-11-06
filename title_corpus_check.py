#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据误报率体检器 —— **一条判据上线前，先拿真实产物量它会不会误伤。**

为什么它是**常驻**的（而不是一次性探针）：
  `deck.py::_gate_type` 里每加一条新判据，都该重跑一次。
  论证层那次实测的教训 —— 按 mtime 取"最近 60 份" ⇒ 报 **90% 命中**
  （差一步就把一条好判据砍了）；换成分母 = **真实任务目录** ⇒ **0%**。
  **采样分母错了，结论会完全反过来。**

⚠️ 必须用 **3.12** 跑（`probe_pptx` 走进程内 python-pptx；
   3.13 那个零依赖子集没有它）。
⚠️ 名字不带 `_probe_` 前缀是**刻意的**：`sweep_probes()` 会把 `_probe_*.py`
   挪进回收站，而这个是常驻守卫（与 `figure_glyph_check.py` 同族）。
   它**不进 `regress.py`**：依赖"仓库里有真实产物"，清过 `runs/` 就无话可说
   ⇒ 那是退出码 **2**（语料不足），不是失败。

用法：
    python title_corpus_check.py                  # 扫 runs/ 的真实任务目录
    python title_corpus_check.py --max-hit 0.15   # 命中率上限（默认 0.20）
    python title_corpus_check.py --show 12        # 摊开前 N 份命中样本的标题原文
    python title_corpus_check.py --json

退出码：
    0 = 语料够，且命中率 ≤ 阈值
    1 = 命中率 > 阈值 ⇒ **该收窄判据**（或它本来就不该上）
    2 = 语料不足 ⇒ **"没查"≠"查出来是 0"**，不许当通过
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rongke.deck import probe_pptx  # noqa: E402

# ── 与 `deck.py::_gate_type` 里那把尺子**逐字一致** ─────────────────────
#   刻意抄全（不 import 私有常量），并在末尾用源码比对做**一致性自检**：
#   尺子漂移 = 这个体检器量的是另一条判据 = 静默失效（而它正是防静默失效用的）。
PUNCT = "，。：；、！？,.:;!?"
VERB = re.compile(
    "是|为|需|要|应|能|会|不|使|导致|来自|说明|表明|"
    "支持|超过|低于|高于|随|由|在于|取决|显示|发现|"
    "证明|意味着|并非|才是"
)
MIN_TITLED = 5          # 与 deck.py 同门槛：够不着 5 页就别判
HIT_RATIO = 0.70        # 与 deck.py 同门槛


def looks_noun(t: str) -> bool:
    t = t.strip()
    return len(t) < 12 and not any(c in t for c in PUNCT) and not VERB.search(t)


def real_task_pptx(root: Path) -> tuple[list[Path], int]:
    """只认**真实任务目录** `runs/<日期>-<时间>/`。

    ⚠️ 这里是整个工具的要害。用 `rglob("*.pptx")` 会把 `_trash/`（被挪走的
       测试夹具）、`_regress/`、`_removed/` 一起采进来 —— 它们的 mtime 往往最新，
       按 mtime 取"最近 N 份"采到的**全是它们** ⇒ 量出判据对自己夹具的命中率。
    """
    TASK = re.compile(r"^\d{8}-\d{6}$")
    dirs = [d for d in root.iterdir() if d.is_dir() and TASK.match(d.name)] if root.is_dir() else []
    files: list[Path] = []
    for d in sorted(dirs, key=lambda p: p.name, reverse=True):
        files.extend(sorted(p for p in d.rglob("*.pptx") if p.is_file()))
    return files, len(dirs)


def source_consistency() -> tuple[bool, str]:
    """H 型自检：`deck.py` 里那把尺子必须与这里**同一个**。"""
    p = HERE / "rongke" / "deck.py"
    if not p.is_file():
        return False, "deck.py 不在"
    src = p.read_text(encoding="utf-8")
    ok_p = 'punct = "，。：；、！？,.:;!?"' in src
    ok_v = "是|为|需|要|应|能|会|不|使|导致|来自|说明|表明|" in src
    if ok_p and ok_v:
        return True, "标点表 OK · 动词表 OK"
    return False, f"标点表={'OK' if ok_p else '**漂移**'} 动词表={'OK' if ok_v else '**漂移**'}"


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--root", default=str(HERE / "runs"))
    ap.add_argument("--max-hit", type=float, default=0.20,
                    help="命中率上限（默认 0.20）")
    ap.add_argument("--min-corpus", type=int, default=3,
                    help="至少几份可读产物才算『查过』（默认 3）")
    ap.add_argument("--show", type=int, default=0, help="摊开前 N 份命中样本")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    root = Path(a.root)
    files, n_dirs = real_task_pptx(root)

    ok_src, src_msg = source_consistency()

    readable, unreadable = 0, 0
    hit, near, clean = [], [], []
    bag: Counter = Counter()
    skipped: list[str] = []

    for p in files:
        try:
            f = probe_pptx(p)
        except Exception as e:  # noqa: BLE001
            unreadable += 1
            skipped.append(f"{p.name}: {type(e).__name__}")
            continue
        titled = [s for s in f.slides if (s.get("title_text") or "").strip()]
        if len(titled) < MIN_TITLED:
            continue                       # 判据自己就有 ≥5 门槛，不足者不进分母
        readable += 1
        noun = [s["title_text"] for s in titled if looks_noun(s["title_text"])]
        for s in titled:
            bag[(s.get("title_text") or "").strip()] += 1
        rec = (len(noun) / len(titled), p.name, len(titled), noun)
        (hit if rec[0] >= HIT_RATIO else near if rec[0] >= 0.4 else clean).append(rec)

    hit_rate = (len(hit) / readable) if readable else None
    if readable < a.min_corpus:
        code = 2                       # 「没查」≠「查出来是 0」
    elif hit_rate is not None and hit_rate > a.max_hit:
        code = 1
    else:
        code = 0

    payload = {
        "ok": code == 0,
        "exit": code,
        "task_dirs": n_dirs,
        "pptx_total": len(files),
        "readable": readable,
        "unreadable": unreadable,
        "min_corpus": a.min_corpus,
        "max_hit": a.max_hit,
        "hit": len(hit), "near": len(near), "clean": len(clean),
        "hit_rate": hit_rate,
        "skipped": skipped[:20],
        "source_consistency": ok_src,
        "source_msg": src_msg,
        "top_titles": bag.most_common(15),
    }
    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return code

    print(f"真实任务目录 {n_dirs} 个 / .pptx {len(files)} 份"
          f"（打不开 {unreadable} 份，**不进分母**）")
    if readable == 0:
        print("\n⇒ **没有可读语料**（退出码 2）：『没查』不等于『查出来是 0』。")
        return code
    print(f"其中 ≥{MIN_TITLED} 页有标题（进分母）：**{readable}** 份\n")
    print(f"  命中（≥{HIT_RATIO:.2f}）：{len(hit):3d} 份 = {hit_rate:6.1%}"
          f"   上限 {a.max_hit:.0%} ⇒ "
          f"{'**超标，该收窄判据**' if hit_rate > a.max_hit else 'OK'}")
    print(f"  擦边（0.40~{HIT_RATIO:.2f}）：{len(near):3d} 份")
    print(f"  干净（<0.40）    ：{len(clean):3d} 份")

    if a.show:
        print("\n" + "=" * 78)
        print(f"命中样本的标题原文（前 {a.show} 份）—— 用眼睛判：这些该不该被提醒？")
        print("=" * 78)
        for ratio, name, n, noun in hit[:a.show]:
            print(f"\n[{ratio:.0%}] {name}  ({n} 页有标题)")
            for t in noun[:8]:
                print(f"        - {t}")

    print("\n标题词频 Top 15（真实标题的形态惯例）：")
    for t, c in bag.most_common(15):
        print(f"  {c:4d}  {t[:60]}")

    print(f"\n[H 型自检] deck.py 里的尺子与体检器一致：{src_msg}")
    if code == 2:
        print("⇒ 语料不足，**不算通过**（退出码 2）。")
    elif code == 1:
        print("⇒ 判据在真实产物上误报超标，**该收窄或撤下**（退出码 1）。")
    else:
        print("⇒ 语料够、误报在阈值内（退出码 0）。")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
