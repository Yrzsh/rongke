# -*- coding: utf-8 -*-
"""快照 —— 带校验的备份，专治"以为备了、其实没备"。

## 它治的那个病

本轮真实事故：给 `rongke/web/index.html` 建了个 `index.html.bak_20260918`，
事后比 md5 —— **和改后的文件一模一样**。方向拷反了，等于根本没备份，
而改动最大的那个文件恰恰因此没有回滚路径。

问题不在"忘了备份"，而在**备份这件事没有校验**。
所以这个工具把"拷贝"和"核对"绑成一步：存完立刻记下 md5，
并提供 `verify` 告诉你**每一份备份到底还能不能用来回滚**：

  * `相同` —— 备份内容 == 当前文件 ⇒ **这份备份没有回滚价值**（就是那次事故）
  * `已改` —— 备份内容 != 当前文件 ⇒ 这份备份是有效的回滚点
  * `缺失` —— 当前文件没了 ⇒ 只能靠备份恢复

## 用法

    python snapshot.py save rongke/web/index.html rongke/cli.py -n "重写控制台前"
    python snapshot.py list
    python snapshot.py verify                 # 核对最近一次
    python snapshot.py verify .backups/xxx    # 核对指定快照
    python snapshot.py restore .backups/xxx rongke/web/index.html --yes

`restore` 会先把当前文件**再快照一次**（前缀 `pre-restore-`），所以误恢复也能退回来。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / ".backups"


def md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel(p: Path) -> str:
    """统一成相对 ROOT 的 posix 路径。**项目外的文件直接拒绝** ——
    否则绝对路径拼进快照目录会静默跑到别处去，比报错危险得多。"""
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        raise SystemExit(f"只快照本项目（{ROOT}）内的文件，这个在外面：{p}")


def cmd_save(args) -> int:
    srcs = [Path(a) for a in args.paths]

    # 校验一律前移到建目录之前 —— 否则中途失败会留下一个空快照目录（实测踩到）
    missing = [p for p in srcs if not p.is_file()]
    if missing:
        print("这些路径不是文件，先确认：")
        for p in missing:
            print(f"  ✗ {p}")
        return 2
    rels: list[str] = []
    outside: list[Path] = []
    for p in srcs:
        try:
            rels.append(_rel(p))
        except SystemExit:
            outside.append(p)
    if outside:
        print(f"只快照本项目（{ROOT}）内的文件，这些在外面：")
        for p in outside:
            print(f"  ✗ {p}")
        return 2

    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap = STORE / stamp
    n = 1
    while snap.exists():                     # 同一秒存两次也不互相覆盖
        n += 1
        snap = STORE / f"{stamp}-{n}"
    snap.mkdir(parents=True)

    man: dict = {
        "id": snap.name,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": args.note or "",
        "files": {},
    }
    print(f"快照 → {_rel(snap)}")
    if args.note:
        print(f"  说明：{args.note}")
    for p, rel in zip(srcs, rels):
        dest = snap / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        digest = md5(p)
        man["files"][rel] = {"md5": digest, "bytes": p.stat().st_size}
        print(f"  + {rel}  {p.stat().st_size:>7} B  md5={digest[:12]}")

    # 立刻回读校验：拷完马上核，别"先拷了回头再核"
    bad = [r for r, m in man["files"].items() if md5(snap / r) != m["md5"]]
    if bad:
        print(f"\n!! 拷贝后校验失败（备份不可信）：{bad}")
        return 3

    (snap / "MANIFEST.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] {len(man['files'])} 个文件已快照并校验通过。")
    print(f"     提示：验证它是否有效，请跑 `python snapshot.py verify {_rel(snap)}`"
          "（会告诉你哪些文件自备份后已被改动）。")
    return 0


def cmd_list(args) -> int:
    if not STORE.exists():
        print("还没有任何快照。")
        return 0
    dirs = sorted((d for d in STORE.iterdir() if (d / "MANIFEST.json").is_file()),
                  reverse=True)
    if not dirs:
        print("还没有任何快照。")
        return 0
    for d in dirs:
        man = json.loads((d / "MANIFEST.json").read_text(encoding="utf-8"))
        print(f"{man['id']}  {man['created']}  {len(man['files'])} 个文件"
              + (f"  「{man['note']}」" if man.get("note") else ""))
        for rel in man["files"]:
            print(f"    - {rel}")
    return 0


def _verify(snap: Path, quiet: bool = False) -> tuple[int, int]:
    man = json.loads((snap / "MANIFEST.json").read_text(encoding="utf-8"))
    same = diff = gone = 0
    if not quiet:
        print(f"核对快照 {man['id']}（{man['created']}）"
              + (f"「{man['note']}」" if man.get("note") else ""))
    for rel, meta in man["files"].items():
        cur = ROOT / rel
        old = meta["md5"]
        if not cur.is_file():
            gone += 1
            if not quiet:
                print(f"  [缺失] {rel} —— 当前文件不在，只能靠这份备份恢复")
            continue
        now = md5(cur)
        if now == old:
            same += 1
            if not quiet:
                print(f"  [相同] {rel} —— 与当前一致：它此刻回滚不到任何别的状态，"
                      "但此后一旦改动，它就是回滚点")
        else:
            diff += 1
            if not quiet:
                print(f"  [已改] {rel} —— 备份是改动前的状态 ⇒ 可回滚（{old[:10]} → {now[:10]}）")
    return same, diff + gone


def cmd_verify(args) -> int:
    if args.snap:
        snap = Path(args.snap)
    else:
        if not STORE.exists():
            print("还没有任何快照。")
            return 2
        cands = sorted((d for d in STORE.iterdir() if (d / "MANIFEST.json").is_file()),
                       reverse=True)
        if not cands:
            print("还没有任何快照。")
            return 2
        snap = cands[0]
    if not (snap / "MANIFEST.json").is_file():
        print(f"不是有效的快照目录：{snap}")
        return 2
    same, usable = _verify(snap)
    print()
    if usable:
        print(f"结论：{usable} 个文件有改动前的版本可回滚，{same} 个与当前一致。")
    else:
        print(f"结论：{same} 个文件**全部与当前一致** —— 这份快照是「当前状态的基线」，")
        print("      本身不含任何改动前的版本。")
        print("      若你的本意是「改坏了能退回去」，请在动手**之前** save；")
        print("      本轮就犯过这个错：给 index.html 的备份和改后文件 md5 完全相同，")
        print("      方向拷反，等于没备份 —— 而它恰恰是改动最大的那个文件。")
    return 0


def cmd_restore(args) -> int:
    snap = Path(args.snap)
    man_path = snap / "MANIFEST.json"
    if not man_path.is_file():
        print(f"不是有效的快照目录：{snap}")
        return 2
    man = json.loads(man_path.read_text(encoding="utf-8"))
    targets = [_rel(Path(a)) for a in args.paths]
    unknown = [t for t in targets if t not in man["files"]]
    if unknown:
        print("这份快照里没有：")
        for t in unknown:
            print(f"  ✗ {t}")
        return 2

    print(f"将从 {man['id']} 恢复 {len(targets)} 个文件：")
    for t in targets:
        cur = ROOT / t
        state = "当前不存在" if not cur.is_file() else (
            "内容相同，无需恢复" if md5(cur) == man["files"][t]["md5"] else "将被覆盖")
        print(f"  {t}  —— {state}")

    if not args.yes:
        print("\n这是改动磁盘的操作。确认无误后加 --yes 执行。")
        return 1

    # 覆盖前先把"当前状态"存一份，误恢复也能退回来
    exist = [str(ROOT / t) for t in targets if (ROOT / t).is_file()]
    if exist:
        ns = argparse.Namespace(paths=exist, note=f"恢复 {man['id']} 之前的现场")
        if cmd_save(ns) != 0:
            print("!! 恢复前的保护性快照失败，已中止恢复。")
            return 3

    for t in targets:
        src, dst = snap / t, ROOT / t
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        ok = md5(dst) == man["files"][t]["md5"]
        print(f"  {'✓' if ok else '✗'} {t}  md5={md5(dst)[:12]} {'校验通过' if ok else '校验失败!'}")
        if not ok:
            return 3
    print("\n[OK] 恢复完成。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="带校验的快照（备份）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("save", help="存快照（请在改动文件**之前**跑）")
    p.add_argument("paths", nargs="+")
    p.add_argument("-n", "--note", default="")
    p.set_defaults(fn=cmd_save)

    p = sub.add_parser("list", help="列出所有快照")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("verify", help="核对快照还能不能回滚")
    p.add_argument("snap", nargs="?")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("restore", help="从快照恢复（需 --yes）")
    p.add_argument("snap")
    p.add_argument("paths", nargs="+")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_restore)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
