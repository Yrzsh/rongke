# -*- coding: utf-8 -*-
"""从运行归档重建 deck —— **`session.json` 里存着完整的工具入参，`trace.jsonl` 只有摘要。**

## 为什么要有这个工具（2026-09-21 真事，不是推想）

我把 `runs/20260921-124950/deck.pptx`（4:3 原件）**就地套了主题** ——
`style_deck` 的语义是**原地改写**（`res.pptx = str(p)`，它不产出第二个文件）。
`runs/` 在 `.gitignore` 里 ⇒ git 捞不回；`_tkit` 只挪了另一份。
⇒ 原件没了。**唯一忠实的恢复路径 = 照归档重放一遍。**

## 两条硬事实（这就是本工具存在的理由）

| 归档文件 | 里面有什么 | 能不能拿来恢复 |
|---|---|---|
| `session.json` | `events[].data.args` = **完整入参** | ★ 能 |
| `trace.jsonl` | `agent.result_digest` —— 只是个**摘要** | ✘ 不能（那是给遥测看的） |

⇒ **推论：任何"清理运行痕迹"的守卫，都不该顺手把 `session.json` 当垃圾扫掉。**
  它同时是"这次任务到底怎么建出那个文件的"的唯一答案。

## 用法

    python replay_deck.py <run_dir>                      # 重放到 deck_replay.pptx 并校验
    python replay_deck.py <run_dir> --out X.pptx         # 换产出名
    python replay_deck.py <run_dir> --apply              # 校验通过后顶替 deck.pptx（先 stash 旧的）

校验目标 = 同目录 `deck_report.json`（对**原件**的体检记录）：
页数 / notes_ratio / 最小正文字号 / 画布宽 —— 逐项比。**不一致就不许 --apply。**

⚠️ `set_slide_notes` **不在 MCP 服务端里**（上游 37 个工具都不会写备注），
   它是荣珂内核自己那个工具（`rongke.deck.write_slide_notes`）⇒ 重放要单独接。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 会**改变演示文稿状态**的工具（只重放它们；load_skill / read_file / audit_deck 是只读的）
BUILD = ("create_presentation", "switch_presentation", "add_slide", "add_bullet_points",
         "add_chart", "manage_text", "populate_placeholder", "set_core_properties",
         "save_presentation", "set_slide_notes")


def _calls(sess: dict) -> list[tuple[str, dict]]:
    out = []
    for e in sess.get("events", []):
        if e.get("kind") != "tool_start":
            continue
        a = e.get("data") or {}
        if a.get("name") in BUILD:
            out.append((a["name"], dict(a.get("args") or {})))
    return out


def replay(run: Path, out_name: str) -> tuple[bool, dict]:
    """重放一次。返回 (是否成功, 校验明细)。"""
    from rongke.config import Config
    from rongke.deck import probe_pptx, write_slide_notes
    from rongke.mcp_client import MCPHub, install_mcp
    from rongke.tools import ToolRegistry

    sess = json.loads((run / "session.json").read_text(encoding="utf-8"))
    ws = Path(sess["workspace"])
    if ws.resolve() != run.resolve():
        print("⚠️ session 里记的工作区是 %s，不是 %s —— 仍按记录跑" % (ws, run))
    calls = _calls(sess)
    print("待重放 %d 次调用" % len(calls))

    cfg = Config.load(ROOT)
    specs = [s for s in cfg.mcp.servers if s.name == "ppt"]
    if len(specs) != 1:
        print("✘ config.json 里没解析出唯一的 ppt 服务端 —— 无从重放")
        return False, {}
    hub = MCPHub([specs[0]], workspace=ws)
    reg = ToolRegistry()
    install_mcp(reg, hub)

    n_ok = n_bad = 0
    for name, args in calls:
        if name == "set_slide_notes":                 # ← 内核工具，不在 MCP 里
            try:
                res = write_slide_notes(out_name, args.get("notes") or [], workdir=ws)
                ok = bool(res.ok)
                detail = ("复测 %d/%d 页有备注" % (res.wrote, res.slides) if ok
                          else "段数 %d / 页数 %d：%s" % (
                              res.requested, res.slides,
                              "；".join(res.problems)[:110] or "（没给原因）"))
            except Exception as ex:
                ok, detail = False, "%s: %s" % (type(ex).__name__, ex)
        else:
            if name == "save_presentation":
                args["file_path"] = out_name
            try:
                tc = reg.call(name, args)
                ok, detail = bool(tc.ok), str(tc.error or "")[:150]
            except Exception as ex:
                ok, detail = False, "%s: %s" % (type(ex).__name__, ex)
        n_ok += ok
        n_bad += (not ok)
        print("  %-20s %s %s" % (name, "✔" if ok else "✘", detail))
    print("重放结果：%d 成功 / %d 失败" % (n_ok, n_bad))
    try:
        hub.stop_all()
    except Exception:
        pass

    out = ws / out_name
    if not out.is_file():
        print("✘ 没产出文件：%s" % out)
        return False, {}

    # ── 校验：对着 `deck_report.json` 里对**原件**的记录逐项比 ──
    rec_p = run / "deck_report.json"
    if not rec_p.is_file():
        print("⚠️ 没有 deck_report.json ⇒ 只能自证，**没有独立锚点**。产出在 %s" % out)
        return True, {}
    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    f = probe_pptx(out)
    want = {
        "slide_count": int(rec["slide_count"]),
        "notes_ratio": round(float(rec["notes_ratio"]), 3),
        "min_body_font_pt": float(rec["min_body_font_pt"]),
        "slide_cx": int(rec["facts"]["slide_cx"]),
    }
    got = {
        "slide_count": int(f.slide_count),
        "notes_ratio": round(float(f.notes_ratio), 3),
        "min_body_font_pt": float(f.min_body_font_pt or 0),
        "slide_cx": int(f.slide_cx or 0),
    }
    print("\n%-18s %-12s %-12s" % ("项", "原件记录", "重放得到"))
    bad = [k for k in want if want[k] != got[k]]
    for k in want:
        print("%-18s %-12s %-12s %s" % (k, want[k], got[k], "✔" if k not in bad else "★ 不一致"))
    print("\n结论：", "重放与原件记录**逐项一致**" if not bad else "★ 不一致项 = %s" % bad)
    return not bad, got


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    run = Path(argv[1]).resolve()
    if not (run / "session.json").is_file():
        print("✘ %s 里没有 session.json ⇒ 没有可重放的入参" % run)
        return 2

    out_name = "deck_replay.pptx"
    apply_it = "--apply" in argv
    if "--out" in argv:
        out_name = argv[argv.index("--out") + 1]

    ok, _ = replay(run, out_name)
    if not ok:
        print("★ 校验没过 ⇒ **不落位**（宁可留着重放件，也不许拿一份对不上的当原件）")
        return 1
    if not apply_it:
        print("（没加 --apply ⇒ 产出留在 %s，原件一个字节没动）" % (run / out_name))
        return 0

    # ── --apply：老的 deck.pptx 先进回收站（挪，不是删），再把重放件顶上去 ──
    import shutil
    import _tkit as T
    native = run / "deck.pptx"
    if native.is_file():
        dst = T.stash(native, "deck-pre-replay")
        print("旧原件已归档 →", dst)
    src = run / out_name
    try:
        import os
        os.replace(src, native)
    except OSError as e:
        # ⚠️ 覆盖一个**被别的进程打开**的 .pptx 会 `WinError 5`（预览器/浏览器面板占着）。
        #    `copyfile` 走的是另一条路，通常能过；改名则一定不行。实测过一次。
        print("⚠️ 改名失败（%s）—— 退回复制覆盖" % e)
        shutil.copyfile(src, native)
        # 复制成功之后那份中间稿就没用了。**挪，不删** —— 本机删除按条目计费，
        # 而且这正是"顺手一删"最容易漏掉的地方（`_tkit.py` 开头那条规矩）。
        T.stash(src, "replay-src")
    print("✔ 已顶替：%s（%d B）" % (native, native.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
