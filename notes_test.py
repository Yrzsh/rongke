# -*- coding: utf-8 -*-
"""notes_test.py —— 「写讲稿备注」（`set_slide_notes`）的回归测试。

跑法：
    python notes_test.py            # 全部
    python notes_test.py --json     # 只吐 JSON

## 为什么独立成一个文件，而不是并进 `deck_test.py`

`deck_test` 坚持**纯标准库夹具**（它的注释写了理由：断言数必须每次一样，
"有时跳过"会让基线在"断言数腰斩"上误报）。而"真的把备注写进一份 .pptx"
必然要用 **python-pptx** —— 那是另一个依赖层。混在一起，就是让
`deck_test` 在某些环境下变成"有时跳过"。**依赖边界分开写，两边都诚实。**

## 它守的是什么（2026-09-21 的实测血债）

实测 `runs/20260921-121201`（25 分钟 / 67 次调用）：
  · deck 是建出来了（13 页 / 43,596 B）✔
  · 然后约 **19 次调用（28%）** 全耗在找「怎么写备注」上 ——
    6 次幻觉 `apply_professional_design(operation="add_speaker_notes")`、
    4 次 `dispatch_task`（4 个执行器全 `available: false`）、9 次 `load_skill`；
  · 最后 `audit_deck`=0、`deliver_task`=0 —— **整场跑完，零交付。**

根因不是模型不努力，是**底座没给工具**：技能里写的兜底路径
「另跑一小段 python-pptx」需要一个执行代码的能力，而内核当时没有。
补上之后（`deck.write_slide_notes` + `loop` 里的 `set_slide_notes`），
**一条基元换 13 页讲稿**；而在此之前两轮提示词改造换到的是 **0**。

⇒ 所以这个文件守的是**能力本身**，不是管道通不通。

## 夹具

**自己造**，不依赖 `runs/` 里那份真 run 的 deck：run 目录是证据，
可能被归档/清理，拿它当夹具就是把"能力有没有退步"绑在"那份证据还在不在"上。
用 `deck.python`（配置里那个装了 python-pptx 的解释器）现造一份 13 页 deck ——
这也更接近真实场景（上游的 deck 就是 python-pptx 建出来的）。

python-pptx 缺失时退 **3（NOT REPORTABLE）**，并写明是哪个解释器缺什么：
写备注是整个 deck 域**声明过的依赖**，缺了就不该报"通过"。
（`deck_test` 的 13.9~13.12 专门守"缺依赖时工具要如实拒绝"，那是另一件事。）
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _tkit as T                                                    # noqa: E402
from rongke import deck as D                                         # noqa: E402
from rongke.config import Config                                     # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
_QUIET = False
_TMP = T.fresh_dir(ROOT / "runs" / "_regress" / "_notes_test", "notes-test")


def check(label: str, got, want=True, detail: str = "") -> None:
    ok = (got == want)
    (PASS if ok else FAIL).append(label)
    if not _QUIET:
        if ok:
            print(f"  ✔ {label}")
        else:
            print(f"  ✘ {label}\n        期望 {want!r}，实际 {got!r}"
                  + (f"\n        {detail}" if detail else ""))


def section(t: str) -> None:
    if not _QUIET:
        print(f"\n── {t} " + "─" * max(0, 58 - len(t)))


# ══════════════════════════════════════════════════════════════════
# 夹具：用装了 python-pptx 的解释器现造一份 deck
# ══════════════════════════════════════════════════════════════════
_FIXTURE = r'''
import json
import sys
from pptx import Presentation
from pptx.util import Emu, Inches, Pt

req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
prs = Presentation()                      # 上游新建默认也是 4:3
layout = prs.slide_layouts[5]             # Title Only
for i in range(req["n"]):
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "第 %d 页标题" % (i + 1)
    tb = s.shapes.add_textbox(Inches(0.8), Inches(1.8), Inches(8.4), Inches(3.2))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.text = "第 %d 页正文：一页一个主张，展开留给讲稿。" % (i + 1)
    for p in tf.paragraphs:
        for r in p.runs:
            r.font.size = Pt(18)
prs.save(req["pptx"])
print(json.dumps({"ok": True, "slides": len(prs.slides._sldIdLst)}))
'''


def build_fixture(py: str, path: Path, n: int) -> bool:
    """造夹具。**不设 cwd**（给的是绝对路径），免得踩 K 型路径解析。"""
    try:
        r = subprocess.run([py, "-c", _FIXTURE],
                           input=json.dumps({"pptx": str(path), "n": n}).encode("utf-8"),
                           capture_output=True, timeout=180)
    except Exception as e:
        print(f"  !! 夹具造不出来：{type(e).__name__}: {e}")
        return False
    if not path.is_file():
        print("  !! 夹具没落盘。stdout/stderr 尾部："
              + (r.stdout or b"").decode("utf-8", "replace")[-300:]
              + (r.stderr or b"").decode("utf-8", "replace")[-300:])
        return False
    return True


# ══════════════════════════════════════════════════════════════════
# 正文
# ══════════════════════════════════════════════════════════════════
def main() -> int:
    global _QUIET
    _QUIET = "--json" in sys.argv[1:]

    if not _QUIET:
        print("═" * 62)
        print("荣珂 · 写讲稿备注（set_slide_notes） · 回归测试")
        print("═" * 62)

    cfg = Config.load()
    dcfg = getattr(cfg, "deck", None)
    py = D.find_notes_python(getattr(dcfg, "python", None))
    if py is None:
        # 不报"通过"：这是 deck 域**声明过的依赖**，缺了就没法验。
        print(f"[NOT REPORTABLE] 找不到装了 python-pptx 的解释器"
              f"（config 的 deck.python = {getattr(dcfg, 'python', '')!r}）。")
        print("  写讲稿备注依赖 python-pptx；缺它时 `set_slide_notes` 会如实拒绝，"
              "但**这条能力就没被验证过**，不许当成通过。")
        return 3

    deck = _TMP / "deck.pptx"
    if not build_fixture(py, deck, 13):
        return 3
    if not _QUIET:
        print(f"夹具：{deck.name}（现造 13 页） · 解释器 {py}")

    # ── 0 · 起点必须真的是"0 页备注" ─────────────────────────
    section("0 起点基线")
    base = D.probe_pptx(deck)
    check("0.1 夹具是 13 页", base.slide_count, 13, str(base.problems))
    check("0.2 起点真的没有备注（不是拿已有备注自欺）", base.slides_with_notes, 0)
    check("0.3 起点 notes_ratio = 0.0", base.notes_ratio, 0.0)

    # ── 1 · 正常路径 ────────────────────────────────────────
    section("1 正常路径（13 段 → 13 页）")
    n = base.slide_count
    notes = [f"第 {i} 页讲稿：\n本页讲什么 —— 一句话说清主张。\n"
             f"数据来源：素材.md 第 {i} 节。\n口径：只统计经同行评议的。\n"
             f"可能被问到：这个数字的样本量是多少？"
             for i in range(1, n + 1)]
    r = D.write_slide_notes("deck.pptx", notes, workdir=_TMP, cfg=dcfg)
    if not _QUIET:
        print(r.format_for_model())
    check("1.1 写入成功（ok=True）", r.ok, True,
          json.dumps(r.problems, ensure_ascii=False)[:300])
    check("1.2 复测页数 13", r.slides, 13)
    check("1.3 ★ 复测：13 页全都有备注", r.wrote, 13)
    check("1.4 ★ 复测覆盖率 = 100%", r.notes_ratio, 1.0)
    check("1.5 ★ 逐页比对无一处对不上（发出去的真的落进文件了）", r.mismatches, [])
    check("1.6 没有 unverified（复测做成了）", r.unverified, "")
    check("1.7 文件确实长大了", r.bytes_after > r.bytes_before, True,
          f"{r.bytes_before} → {r.bytes_after}")
    check("1.8 用的解释器真的装了 python-pptx", D.has_python_pptx(r.python_exe), True)
    check("1.9 原本没有备注 ⇒ 不该报「被覆盖」", r.replaced_slides, [])

    # ── 2 · 独立复核（换一条路读回来）────────────────────────
    section("2 独立复核（read_notes 直接读文本）")
    back = D.read_notes(deck)
    check("2.1 read_notes 读得动（不是 None）", back is not None)
    check("2.2 读出 13 页备注", len(back or {}), 13)
    check("2.3 第 7 页读回来的正文含「第 7 页讲稿」",
          "第 7 页讲稿" in (back or {}).get(7, ""))
    check("2.4 第 13 页也在", "第 13 页讲稿" in (back or {}).get(13, ""))

    # ── 3 · 负控：页数不匹配 ─────────────────────────────────
    section("3 负控 · 页数一一对应")
    r12 = D.write_slide_notes("deck.pptx", notes[:12], workdir=_TMP, cfg=dcfg)
    check("3.1 12 段配 13 页 ⇒ 拒绝", r12.ok, False)
    check("3.2 ★ 拒绝理由点名真实页数（13 页）",
          any("13 页" in p for p in r12.problems), True, str(r12.problems)[:240])
    check("3.3 ★ 且说明不会替它猜哪段配哪页",
          any("猜" in p for p in r12.problems), True, str(r12.problems)[:240])
    check("3.4 14 段 ⇒ 也拒绝（长了同样要拒）",
          D.write_slide_notes("deck.pptx", notes + ["多的一段"],
                              workdir=_TMP, cfg=dcfg).ok, False)

    # ── 4 · 负控：参数形状 ───────────────────────────────────
    section("4 负控 · 参数形状")
    rs = D.write_slide_notes("deck.pptx", "一整段文本", workdir=_TMP, cfg=dcfg)
    check("4.1 传字符串 ⇒ 拒绝", rs.ok, False)
    check("4.2 且说清要**逐页**给", any("逐页" in p for p in rs.problems), True)
    check("4.3 传 dict ⇒ 拒绝",
          D.write_slide_notes("deck.pptx", {"1": "x"}, workdir=_TMP, cfg=dcfg).ok, False)

    # ── 5 · 负控：路径围栏 ───────────────────────────────────
    section("5 负控 · 路径围栏")
    esc = D.write_slide_notes("../../逃出去.pptx", notes, workdir=_TMP, cfg=dcfg)
    check("5.1 `../` 越界 ⇒ 拒绝", esc.ok, False)
    check("5.2 拒绝理由印出工作区路径（可自查）",
          any(str(_TMP) in p for p in esc.problems), True, str(esc.problems)[:240])
    check("5.3 越界时**一个文件都没产出**",
          not (_TMP.parents[1] / "逃出去.pptx").exists())
    miss = D.write_slide_notes("根本没有.pptx", notes, workdir=_TMP, cfg=dcfg)
    check("5.4 文件不存在 ⇒ 拒绝", miss.ok, False)
    check("5.5 ★ 且列出工作区里真实有的名字（照抄就能改对）",
          any("deck.pptx" in p for p in miss.problems), True, str(miss.problems)[:240])

    # ── 6 · 负控：不是 pptx ──────────────────────────────────
    section("6 负控 · 不是 pptx")
    (_TMP / "素材.md").write_text("# markdown\n", encoding="utf-8")
    rmd = D.write_slide_notes("素材.md", notes, workdir=_TMP, cfg=dcfg)
    check("6.1 .md 当 deck ⇒ 拒绝", rmd.ok, False)
    check("6.2 理由说它不是能改的 .pptx",
          any(".pptx" in p for p in rmd.problems), True, str(rmd.problems)[:240])
    (_TMP / "坏.pptx").write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    check("6.3 坏 zip ⇒ 拒绝返回（不抛异常带崩调用方）",
          D.write_slide_notes("坏.pptx", notes, workdir=_TMP, cfg=dcfg).ok, False)

    # ── 7 · 空串页：覆盖率不许被凑出来 ───────────────────────
    section("7 空串页")
    partial = list(notes)
    partial[4] = ""
    rp = D.write_slide_notes("deck.pptx", partial, workdir=_TMP, cfg=dcfg)
    check("7.1 非空的段都写进去了（ok=True）", rp.ok, True,
          json.dumps(rp.mismatches, ensure_ascii=False)[:240])
    check("7.2 ★ 覆盖率如实降到 12/13", rp.wrote, 12)
    check("7.3 ★ 且点名第 5 页没有讲稿", rp.no_notes_slides, [5])
    check("7.4 报告里说明「你给的是空串」的是哪几页", rp.empty_slides, [5])
    check("7.5 并写明空串不算讲稿", "空串不算讲稿" in rp.format_for_model())
    f7 = D.probe_pptx(deck)
    check("7.6 ★ 空备注页与有正文的备注页：两个计数器分得开（不塌缩）",
          (f7.notes_slides, f7.slides_with_notes), (13, 12),
          f"notes_slides={f7.notes_slides} with_notes={f7.slides_with_notes}")

    # ── 8 · 覆盖写 ──────────────────────────────────────────
    section("8 覆盖写")
    rr = D.write_slide_notes("deck.pptx", notes, workdir=_TMP, cfg=dcfg)
    check("8.1 再写一次成功，覆盖率回到 13/13", (rr.ok, rr.wrote), (True, 13))
    # 期望**不是** 13：第 7 节把第 5 页设成了空串 ⇒ 它没有正文可覆盖。
    # （第一版断言写成 13，是**测试错了**、代码对的。）改成点名页号，
    # 比数个数强：个数对得上、页号错位也照样过。
    check("8.2 ★ 如实报出「被覆盖」的是除第 5 页以外的 12 页",
          sorted(rr.replaced_slides), [i for i in range(1, 14) if i != 5],
          str(rr.replaced_slides))

    # ── 9 · 给模型的下一步必须可执行 ─────────────────────────
    section("9 给模型的下一步")
    txt = rr.format_for_model()
    check("9.1 ★ 要求**重跑 audit_deck**（改过 deck，旧报告作废）",
          "重跑" in txt and "audit_deck" in txt)
    check("9.2 ★ 警告别再调 save_presentation（服务端内存里那份会抹掉备注）",
          "save_presentation" in txt and "抹掉" in txt, True, txt[-300:])
    check("9.3 失败报告里写「别重复同样的调用」",
          "别重复同样的调用" in r12.format_for_model())

    T.stash(_TMP, "notes-test")

    total = len(PASS) + len(FAIL)
    if _QUIET:
        print(json.dumps({"total": total, "pass": len(PASS), "fail": len(FAIL),
                          "failed": FAIL}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "═" * 62)
        print(f"合计 {total} 项：{len(PASS)} 通过，{len(FAIL)} 失败")
        if FAIL:
            print("失败项：" + "; ".join(FAIL))
        print("[DONE] 讲稿备注写入全部通过" if not FAIL else "[DONE] 有失败")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
