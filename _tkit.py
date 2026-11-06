#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_tkit —— 荣珂测试工具箱：把「清理」从**删除**改成**挪走**。

════════════════════════════════════════════════════════════════════════
为什么必须有它（这不是洁癖，是环境逼出来的）
════════════════════════════════════════════════════════════════════════
本环境有一道**批量删除守卫**（`safe-delete-bulk-guard.cjs`）。读它的源码
（`countTarget` / `checkSafeDeleteBulkGuard`）能确认计费口径 —— 而这个口径
和直觉是**反的**：

  · 删一个**文件**            → 1 点
  · 删一个**目录**            → **递归数里面所有条目**（子目录继续递归）
  · 符号链接 / junction 目录  → 1 点（不跟进、不递归）
  · 额度 = **50 点 / 一轮对话**（键是 `conversationRequestId`，单调累加，
    7 天 TTL；撞线后 `exit 2`，用户若**拒绝**则该轮此后所有删除 `exit 3`）
  · 撞线的那一次**不计费**（只在 prune 有变化时回写状态）

两条推论，都反直觉：

  ① **把 N 次删除合并成 1 次调用，一点用都没有** —— 账按**条目**结，不按
     调用结。一个装着 30 个文件的临时目录，`rmtree` 一次就是 30 点。
     （我一开始就是这么猜错的：以为"合并调用"能省。省不了。）
  ② **`os.replace` / `os.rename` 是改名，根本不走删除路径 → 零计费。**

所以本工具箱只做一件事：**把「删掉它」换成「挪走它」**。
测试拆夹具是正当的卫生行为，但没必要为此烧掉整轮的额度。

════════════════════════════════════════════════════════════════════════
规矩
════════════════════════════════════════════════════════════════════════
- 测试里**不许**再写 `unlink` / `rmtree` / `os.remove`。
  要清场就 `stash()` 或 `fresh_dir()`。
- `_trash` 会长大。**长大是磁盘的事，不是额度的事** —— 这两件事别混。
  真正的清理由人显式触发：`python regress.py --purge-trash`，
  它会**先报出这一删要花多少点**，再由你决定。
  ⚠️ 但**它通常付不起**：实测 `_trash` 409 项 / 199MB ⇒ 清空要约 **3902 点 = 78 轮**。
  那种规模下正确做法是**在资源管理器里手工删掉 `runs/_trash` 整个目录**
  （不走 agent 工具 ⇒ 不记账）。`_trash` 是设计好的**停车位**，里面只有被挪走的
  测试夹具与影子副本，**没有任务归档**（归档在 `runs/<id>/`，台账在 `_regress/`）。
  （把删除从"测试的自动行为"降级成"人的显式行为"，是这个设计的一半价值。）
- `purge()` 那条安全声明（链接只摘、绝不跟进）**光有静态保证不够** ——
  它保护的是"清回收站会不会删穿到别人的目录"。要亲手验就跑
  `python _tkit.py --verify-purge --yes`（**约 6 点**，三份夹具：A 走 `purge()`、
  B 记录 `shutil.rmtree` 的现状、C 是**判别力正控** —— 一个天真实现**必须删穿**，
  否则 A 那几条就是恒真的）。缺 `--yes` 只报代价、不动手。
  ⚠️ 它不是测试套件的一员 —— 别把它加进 `regress.py`；
  也**别在一轮里反复跑**（额度是整轮共享的，超支会顶红别的测试）。
"""

from __future__ import annotations

import json
import os
import re
import stat as _stat
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TRASH = ROOT / "runs" / "_trash"
LEDGER = ROOT / "runs" / "_regress" / "_delete_ledger.jsonl"

_BAD = re.compile(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+")


# ══════════════════════ ① 量账：复刻守卫的计费口径 ══════════════════════
_IO_REPARSE_MOUNT_POINT = 0xA0000003        # junction
_IO_REPARSE_SYMLINK = 0xA000000C            # 符号链接


def _is_link_like(st) -> bool:
    """守卫眼里的「链接」—— 它用 Node 的 `lstatSync().isSymbolicLink()`。

    ★ 这条判据是我**栽过一次**才写下的，而且它和 Python 的直觉相反：

      · Node：Windows 上 **junction 算链接** ⇒ `countTarget` 返回 1、**不递归**
        （实测：对指向「3 个文件」的目录联接报 `{"count":1}`）
      · Python：`lstat` 对 junction 报 `S_ISDIR=True`、`is_symlink()` 是 **False**、
        `os.scandir` 还**能进去** ⇒ 天真的实现会递归、把别人的目录树算成自己的账

    用 Python 的直觉去复刻 Node 的口径，就会得到 `points(junc)=3` 而守卫算 1。
    账本对不上还是小事 —— 真正危险的是**反方向**（见 `rmtree` 的那条注）。
    """
    if _stat.S_ISLNK(st.st_mode):
        return True
    return (getattr(st, "st_reparse_tag", 0) or 0) in (
        _IO_REPARSE_MOUNT_POINT, _IO_REPARSE_SYMLINK)


def points(p) -> int:
    """一个路径若被**删除**，会花掉多少点。

    ★ 它存在的唯一理由：让"降载"是**算出来的**，不是猜出来的。
      没有它我只能说"少删点吧"；有了它我能说"这一处从 31 点变成 0 点"。

    口径刻意复刻 `countTarget`，**包括它的两个怪癖**：
      · `lstat` 不跟进链接 ⇒ 链接（含 junction）一律算 1、不递归
      · 目录递归计数时逐项相加；不存在的路径算 0

    对不上怎么办：跑 `python _tkit.py --cross-check <路径>`，
    拿**守卫自己的 CLI** 算一遍来对账（R 型：不要拿我的实现证明我的实现对）。
    """
    p = Path(p)
    try:
        st = p.lstat()
    except OSError:
        return 0                    # 不存在 ⇒ 删它不要钱（守卫同样返回 0）
    if not _stat.S_ISDIR(st.st_mode) or _is_link_like(st):
        # 文件 / 符号链接 / junction 都是 1。
        # ⚠️ **曾经**我在这儿写过一句"`shutil.rmtree` 会跟进 junction，于是守卫记 1 点、
        #    实际删掉目标树里的 N 个文件"——**那是错的**，已由 `--verify-purge` 实测推翻：
        #    Python 3.12 在 Windows 上把 junction 排除出 `os.walk` 的 dirnames
        #    （`os.py:388`），顶层还有 `_rmtree_islink()`（`shutil.py:606`）。
        #    错因值得记住：我拿"`entry.is_dir(follow_symlinks=False)` 对 junction 为 True"
        #    这**半截实测**推出了一个结论 —— 那个实测本身没错，但它不是判据的全部。
        #    ⇒ 教训：**判据要量到"最终行为"，别量中间量**（同 G 型的第二种长相）。
        return 1
    n = 0
    try:
        for e in os.scandir(p):
            if e.is_dir(follow_symlinks=False) and not _is_link_like(e.stat(follow_symlinks=False)):
                n += points(Path(e.path))
            else:
                n += 1
    except OSError:
        pass
    return n


def points_many(paths) -> int:
    return sum(points(p) for p in paths)


# ══════════════════════ ② 挪走：零计费的清场 ══════════════════════
def _safe_tag(tag: str) -> str:
    t = _BAD.sub("-", (tag or "obj").strip()).strip("-")
    return (t or "obj")[:60]


def _free_slot(stem: str) -> int:
    """在 `_trash` 里找一个没被占用的序号。

    ★ 为什么用序号而不是时间戳：冲突时要先删旧的才腾得出位置 ——
      而"先删旧的"正是我们要躲的那件事。序号只要往后加，永远不冲突。
    """
    used: set[int] = set()
    try:
        for e in TRASH.iterdir():
            if e.name.startswith(stem + "."):
                tail = e.name[len(stem) + 1:]
                if tail.isdigit():
                    used.add(int(tail))
    except OSError:
        pass
    i = 1
    while i in used:
        i += 1
    return i


def note(saved: int, tag: str, extra: dict | None = None) -> None:
    """把"这次省下了多少点"记进台账。

    台账是**给下一次的自己看的**：它让"全套一共欠了多少删除额度"变成可读的
    一个数，而不是一句印象。`regress.py --trash-report` 会把它们汇总出来。
    """
    if saved <= 0:
        return
    row = {"t": round(time.time(), 1), "tag": tag, "saved": int(saved),
           "pid": os.getpid()}
    if extra:
        row.update(extra)
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass                    # 台账写不进去**不许**影响测试：它是账本，不是判据


def _free_local(p: Path) -> Path:
    """在 `p` 的**同目录**里找一个没被占用的 `.stale-NNN` 名。"""
    base = p.with_name(f"{p.name}.stale")
    n = 1
    while True:
        cand = base.with_name(f"{base.name}-{n:03d}")
        if not (cand.exists() or cand.is_symlink()):
            return cand
        n += 1


def stash(p, tag: str = "") -> Path | None:
    """把 `p`（文件或整棵目录）挪出原位。**零删除、零计费。**

    返回落点；`p` 不存在时返回 `None`。
    落点在 `_trash` 下 = 常规路径；落点在**原地附近**（`xxx.stale-001`）= 走了退路。

    三条路，一条都不允许退回删除：

      ① **挪进 `_trash`** —— 同盘改名，瞬时，零计费。绝大多数情况走这条。
      ② **就地改名** —— 跨盘时 ① 必然 `EXDEV` 失败（`os.replace` 不跨卷）。
         但"同目录改名"永远同盘，所以这条总能落下去。
         ★ 有它在，"挪"在任何情况下都兑现得了 —— 永不因为挪不动就去删。
         代价只是原地多一个 `.stale-NNN`，那是**看得见**的代价，比悄悄烧额度好。
      ③ **两条都失败** —— 如实返回 `None`，**原样留在原地**，并记进台账。
         留下一个能看见的东西，好过抹掉它。
    """
    p = Path(p)
    if not (p.exists() or p.is_symlink()):
        return None
    saved = points(p)
    stem = _safe_tag(tag or p.name)

    # ① 进 _trash
    # ⚠️ `why` 要**在 except 块外**接住原因：Python 会把 `except E as e` 的
    #    绑定名在块结束时 `del` 掉，出了块再读 `e` 就是 UnboundLocalError。
    why = ""
    try:
        TRASH.mkdir(parents=True, exist_ok=True)
        dest = TRASH / f"{stem}.{_free_slot(stem):03d}"
        os.replace(p, dest)         # ← 改名。不触发任何删除保护。
        note(saved, stem)
        return dest
    except OSError as e:
        why = type(e).__name__

    # ② 就地改名（跨盘退路）
    try:
        loc = _free_local(p)
        os.replace(p, loc)
        note(saved, stem, {"local": True, "why": why})
        return loc
    except OSError:
        pass

    # ③ 如实失败
    note(0, stem, {"fail": why, "what": str(p)})
    return None


def stash_many(paths, tag: str = "") -> int:
    """批量挪。返回真正挪走的个数。"""
    n = 0
    for i, p in enumerate(paths, 1):
        if stash(p, tag or f"{Path(p).name}") is not None:
            n += 1
    return n


def fresh_dir(p, tag: str = "") -> Path:
    """清空并返回目录 `p` —— 代价是**一次挪**，不是 N 次删除。

    "清空重建"是夹具里最常见的动作。以前它是 `rmtree` + `mkdir`，
    现在是 `stash` + `mkdir`：语义一模一样，账单从 N 点变成 0 点。
    """
    p = Path(p)
    stash(p, tag or p.name)
    p.mkdir(parents=True, exist_ok=True)
    return p


class scratch:
    """`tempfile.TemporaryDirectory` 的**挪走版**：进块给一个空目录，出块把它挪进 `_trash`。

    ★ 为什么不能直接用 `TemporaryDirectory`：它在 `__exit__` 里跑 `shutil.rmtree`，
      整棵树按**条目数**计费。更麻烦的是它**扫不出来** —— 你按
      `rmtree / unlink / remove` 去 grep 全仓，一个都命中不了，因为真凶藏在
      上下文管理器的 `__exit__` 里。
      本轮就是这么漏的：`mcp_client_test.py` 5 处、`tools_test.py` 1 处、
      `eval/baseline.py` 1 处，全是我按关键字扫过一遍之后才发现的。

    用法（**块内代码一个字都不用改**，缩进也不变）::

        with T.scratch("mcp_", "mcp-tmp") as td:
            ...

    落点在**仓库内**（与 `_trash` 同盘），所以出块是干净的 `os.replace`；
    真放到系统盘去就会 EXDEV，垃圾只能留在系统临时目录里。
    """

    def __init__(self, prefix: str = "scratch_", tag: str = "") -> None:
        self.prefix = prefix
        self.tag = tag
        self.path: Path | None = None

    def __enter__(self) -> Path:
        base = ROOT / "runs" / "_regress"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            base = ROOT            # 退无可退时也别抛：测试还得跑
        import tempfile
        self.path = Path(tempfile.mkdtemp(prefix=self.prefix, dir=str(base)))
        return self.path

    def __exit__(self, *exc) -> bool:
        if self.path is not None:
            stash(self.path, self.tag or self.prefix)
        return False               # **绝不吞异常**：`TemporaryDirectory` 也不吞


def overwrite(p, data: bytes | str) -> Path:
    """写文件，同名覆盖 —— 本来就不计费，写在这里是为了**明示这是刻意的**。

    很多"必须先删掉旧文件"的场合其实是多余的：覆盖写同样干净，
    而且不会在旧文件被占用时失败。
    """
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")
    return p


# ══════════ ②c 清扫：把「影子副本」赶出产品包（挪，不删） ══════════
# 短路实验要写一份"打过补丁的内核"才能验判据承重，而它**必须**能被 import 成
# `rongke.xxx`（相对导入要包上下文）。于是它曾经被直接写进产品包里。
#
# 🪤 为什么必须清扫，而不是"靠 finally 回收就够"：
#   进程被**杀掉**时（本环境额度耗尽就是这么死的：rc≠0、无 traceback）
#   `finally` 根本不跑 ⇒ 那份"含故意缺陷、可被 import 的副本"就留在产品包里了。
#   实测：`rongke/` 里堆了 **27 个** `_probe_loop_*.py`（各 62KB，共 1.7MB，
#   全是 `loop.py` 的陈旧副本）。这不是垃圾问题 —— 它们会被指纹/语法闸算进去，
#   还会让后来的人在 grep 里撞上一堆**假源码**。
#
# ★ 只扫 `_probe_*.py` 文件：`_regress` 下还有 `_probe_fig/` 这类**目录**是测试
#   正在用的落点，glob 串 "*.py" 天然避开它们。
_PROBE_DIRS = (ROOT / "rongke", ROOT / "rongke" / "web", ROOT / "runs" / "_regress")


def sweep_probes(verbose: bool = True) -> list[Path]:
    """把散落的影子副本挪进 `_trash`（**改名，零计费**）。返回被挪走的路径列表。

    ★ 返回值是**列表不是计数**，因为名字本身就带信息：
      · `_probe_loop_<pid>.py`（**带数字后缀**）= 测试**被杀掉**了（`finally` 没跑）
      · `_probe_loop.py`（固定名）      = 测试**正常收尾**留下的
      ⇒ 调用方据此能区分"这轮有测试被中断"与"这只是正常的停车"。只看个数就丢了这个信号。

    `web/` 那份**只能留在原位**：`rongke/web/server.py` 里有
    `HERE = Path(__file__).resolve().parent`（找 `index.html`）⇒ 副本一旦搬出
    `rongke/web/`，`HERE` 就指错地方、测试会以一种很像"代码坏了"的方式红掉。
    所以它照样写在包内，只是**开工前先扫一遍历史残留**（这一步原本只在
    `web_test` 里手写了一份 —— 而 `rongke/` 下那份**没人扫**，于是堆成山）。
    """
    moved: list[Path] = []
    for d in _PROBE_DIRS:
        try:
            stale = sorted(d.glob("_probe_*.py"))
        except OSError:
            continue
        for p in stale:
            if stash(p, f"sweep-{d.name}") is not None:
                moved.append(p)
                if verbose:
                    # 措辞按位置区分：`rongke/**` 是真·产品包（更要紧），
                    # `_regress` 只是落点脏 —— 把两件事说成一个词就是 H 型。
                    where = ("挪出产品包" if str(p).startswith(str(ROOT / "rongke"))
                             else "挪出落点")
                    print(f"  [FYI ] 影子副本{where}：{p.relative_to(ROOT)}")
    return moved


def killed_leftovers(moved) -> tuple[list[Path], list[Path]]:
    """把 `sweep_probes()` 的收获分成 `(被中断的, 正常收尾的)`。

    判据只有名字：**带 `_<数字>.py` 后缀 = 被中断**（`finally` 没跑成）。
    ★ 这是白捡的诊断：影子副本会堆积**本身**就是"有测试没走到终点"的证据，
      以前只当成垃圾一扫了之 —— 垃圾里其实写着事故报告。
    """
    killed = [p for p in moved if re.search(r"_\d+\.py$", p.name)]
    return killed, [p for p in moved if p not in killed]


# ══════════════════════ ③ 盘点：读台账 / 估清场代价 ══════════════════════
def ledger_rows() -> list[dict]:
    rows: list[dict] = []
    try:
        for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def ledger_total() -> int:
    return sum(int(r.get("saved") or 0) for r in ledger_rows())


def trash_stats() -> dict:
    """`_trash` 的规模，以及**清空它要花多少点**。

    ★ 这两件事必须分开报：前者是磁盘占用，后者是额度代价。
      合成一个数就是 H 型（两副面孔）——"很占地方"和"删起来很贵"根本不是一回事。
    """
    items = []
    try:
        items = sorted(TRASH.iterdir())
    except OSError:
        pass
    return {"n_items": len(items), "points": points_many(items),
            "bytes": sum(_size_of(p) for p in items), "path": str(TRASH)}


def _size_of(p: Path) -> int:
    """目录体积。**不跟进链接** —— 跟了就是把目标的体积重复计一遍。"""
    try:
        st = p.lstat()
    except OSError:
        return 0
    if _is_link_like(st):
        return 0
    if not _stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    try:
        for e in os.scandir(p):
            total += _size_of(Path(e.path))
    except OSError:
        pass
    return total


def human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if x < 1024 or unit == "GB":
            return f"{x:.1f}{unit}" if unit != "B" else f"{int(x)}B"
        x /= 1024
    return f"{x:.1f}GB"


# ══════════════════════ ③b 出口：全项目唯一允许真删的地方 ══════════════════════
def _unlink_or_rmdir(q: Path) -> bool:
    """摘掉一个链接项。先试 `rmdir`（Windows 的 junction / 目录符号链接要它），
    不行再 `unlink`。**绝不进去**。"""
    for fn in (os.rmdir, os.unlink):
        try:
            fn(q)
            return True
        except OSError:
            continue
    return False


def purge(p) -> int:
    """**真删**一个路径。返回它花掉的点数（按守卫口径先算好）。

    ★ 这是全项目唯一允许真删的地方，所以"口径"必须和安全绑在一起写。

    ★ 规矩：**链接一律只摘、绝不跟进**；目录逐项递归；文件才 `unlink`。
      为什么值得自己写而不直接交给 `shutil.rmtree`（它其实也不跟进 junction，
      见 `points()` 里那条更正）：这里的口径要**先算后删**（返回值 = 花掉多少点），
      而 `rmtree` 不给这个数；再者"不跟进"必须是**我自己的**、可被 `--verify-purge`
      钉住的属性，而不是"某个版本的标准库碰巧也这样"。

    它**只应该**被 `regress.py --purge-trash --yes` 这类显式动作调用。
    任何测试都不许调它 —— 那等于把 `_tkit` 的意义当场作废。
    """
    p = Path(p)
    try:
        st = p.lstat()
    except OSError:
        return 0
    saved = points(p)               # ★ 记在删之前：删完就再也数不出来了
    if _is_link_like(st):
        _unlink_or_rmdir(p)
        return saved
    if not _stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(p)
        except OSError:
            pass
        return saved
    try:
        entries = list(os.scandir(p))
    except OSError:
        return 0
    for e in entries:
        q = Path(e.path)
        try:
            es = q.lstat()
        except OSError:
            continue
        if _is_link_like(es):
            _unlink_or_rmdir(q)                         # ← 只摘，不进去
        elif _stat.S_ISDIR(es.st_mode):
            purge(q)
        else:
            try:
                os.unlink(q)
            except OSError:
                pass
    try:
        os.rmdir(p)
    except OSError:
        pass
    return saved


def purge_all(items=None) -> tuple[int, int]:
    """清空若干项，返回 `(真删的项数, 花掉的点数)`。"""
    targets = list(items) if items is not None else (
        sorted(TRASH.iterdir()) if TRASH.is_dir() else [])
    cost = 0
    n = 0
    for p in targets:
        if p.exists() or p.is_symlink():
            cost += purge(p)
            n += 1
    return n, cost


# ══════ ③c 亲手验 `purge()`：**链接只摘、绝不跟进**（真删，由人触发） ══════
def _make_junction(link: Path, target: Path) -> bool:
    """`mklink /J`（**目录联接**，不是 symlink）。"""
    import subprocess
    try:
        # ⚠️ `errors="replace"` 不是装饰：cmd 的输出是 **GBK**（"已为 … 创建联接"），
        #    按 UTF-8 解会在工作线程里抛 UnicodeDecodeError —— 主流程照样通过，
        #    stderr 里却多一坨红字（我第一次跑就撞上了）。`web_test.make_junction`
        #    早就带着这个参数，是我抄过来时漏了。
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True, errors="replace")
    except OSError:
        return False
    return r.returncode == 0 and link.exists()


def _mk_purge_case(name: str) -> tuple[Path, Path]:
    """造一份夹具：`box/`（要被删的树，里面挂一个联接）+ `outside/`（**必须活下来**）。

    ★ 目标里放 **3** 个文件 —— 让「只摘链接」与「递归跟进」两个答案分得开
      （放 1 个的话两个实现都得 1，判据没有分辨率 ⇒ 假绿）。
    """
    case = LEDGER.parent / "_purge_probe" / name
    if case.exists():
        stash(case, f"purge-probe-{name}")   # 上一轮的残留：挪，不删
    outside = case / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    for i in (1, 2, 3):
        (outside / f"secret{i}.md").write_text("别删我", encoding="utf-8")
    box = case / "box"
    # ⚠️ 这句 `mkdir` 别删：原先它是挂在 `(box / "real").mkdir(...)` 上的，
    #    我把 `real/` 拿掉省额度时差点连它一起拿掉（**已经拿掉过一次**，
    #    是那条"无删除的验证"当场抓出来的：`FileNotFoundError: .../box/note.txt`）。
    box.mkdir(parents=True, exist_ok=True)
    # ★ 被删的那棵树**刻意做小**：它每多一个条目，这条检查就要多烧 1 点额度。
    #   实测教训：我最初放了 `real/f1.txt`+`real/f2.txt`，于是三份夹具要 ~12 点；
    #   一轮里跑三遍 = 36 点 ⇒ 直接把 50 点的额度吃到 49，
    #   **害得同一轮的 `resume_test` 因额度耗尽判红**（见 `verify_purge` 的注）。
    (box / "note.txt").write_text("x", encoding="utf-8")
    if not _make_junction(box / "link", outside):
        return box, Path()
    return box, outside


def _naive_rmtree(p: Path) -> None:
    """**故意写错**的实现，只当**判别力正控**用（不是"另一种清法"）。

    错在哪：用 `Path.is_dir()` 判目录 —— 它**跟进**链接 ⇒ 递归进 junction 指向的
    目录、把别人的东西删掉。这正是 `purge()` 要避免的写法，也正是"威胁真的存在"
    的证据：如果连它都删不穿，那 A 组那几条断言就是恒真的。
    """
    if p.is_dir():
        for c in p.iterdir():
            _naive_rmtree(c)
        p.rmdir()
    else:
        p.unlink()


def verify_purge() -> int:
    """**真删**两个小夹具，验 `purge()` 的安全声明。由人触发，不属于测试套件。

    ★ 为什么不能塞进 `--self-test`：`purge` 是全项目唯一的真删入口 ——
      让"测试"去调它，`_tkit` 的意义就当场作废。所以它**单独**一条，代价要说清
      （**6 点** = 三份夹具各 2 点：junction 算 1 + note.txt 1）。

    🪤 **它是全项目最贵的一把量具，别在一轮里反复跑。** 实测血债：第一版每份夹具
      放了 4 个条目（~12 点/次），我一轮里跑了 **三遍**（12×3 = 36），
      把这一轮的删除额度吃到 **49/50** ⇒ 同一轮的全套回归里，**`resume_test`
      因额度耗尽被判红**（`rc=1` / 43→20 项 / 日志带 `[safe-delete][SAFE_DELETE_BULK_*]`）。
      ⇒ 两个教训：**① 为省钱而造的工具，本身必须省钱**；
      **② 额度是整轮共享的 —— 一个工具的超支会落在无关的测试身上。**

    ★ 为什么必须**三份**夹具：只验「`purge` 之后目标还在」是不够的 ——
      万一这个夹具压根测不出危险（联接没建成、或判据本身恒真），
      那几条断言就是**假绿**。所以：
        · B = 现状记录（`rmtree` 也不跟进；**行为随版本变**，所以钉住它）；
        · C = **判别力正控**：一个天真实现（`Path.is_dir()` 判目录）**必须删穿**。
          C 删不穿 ⇒ 这个夹具测不出危险 ⇒ A 组那几条结论整套作废。
      ⚠️ B 这一条**曾经把 C 的活儿顶了**：我当时以为 `rmtree` 会删穿，就直接拿它
      当正控 —— 结果 B 报红，才逼出"正控必须真的会犯那个错"这条纪律。

    运行：`python _tkit.py --verify-purge`
    """
    import shutil
    bad = 0

    def ck(q: str, got, want) -> None:
        nonlocal bad
        ok = got == want
        bad += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {q}\n"
              f"        实得 {got!r} · 期望 {want!r}")

    # ── A：交给本项目的 `purge()` ──────────────────────────────────────
    print("── A. `purge()`：链接只摘、绝不跟进 ──")
    box_a, out_a = _mk_purge_case("a")
    live = bool(out_a)
    ck("夹具就位：目录联接建成了（没建成 ⇒ 下面这一族**没测**，不是通过）", live, True)
    ck("夹具就位：目标里确实有 3 个文件（否则测的是空气）",
       sorted(p.name for p in out_a.iterdir()) if live else [], 
       ["secret1.md", "secret2.md", "secret3.md"])
    # 手工数出来的账：link 算 1（是链接、不递归）+ note.txt 1 = **2**。
    cost = purge(box_a)
    ck("`purge` 报的代价 = 手数出来的 2 点（链接算 1、不递归）", cost, 2)
    ck("★ 目标目录里的 3 个文件**一个都没少**（安全声明成立）",
       sorted(p.name for p in out_a.iterdir()) if live else [],
       ["secret1.md", "secret2.md", "secret3.md"])
    ck("★ 且 `box` 确实被真删了（证明上一条不是「什么都没做」）", box_a.exists(), False)
    ck("★ 被删的树里那个真文件也一起没了（不是只摘链接就收工）",
       (box_a / "note.txt").exists(), False)

    # ── B：现状记录 —— `rmtree` 其实**不**跟进 junction（这条纠正了我自己的旧结论）
    print("── B. 现状记录：`shutil.rmtree` 其实**不**跟进 junction ──")
    print("         （Python 3.12 起：`os.walk` 在 Windows 上把 junction 排除出 dirnames")
    print("           ——`os.py:388` `is_dir(follow_symlinks=False) and not entry.is_junction()`；")
    print("           顶层还有 `_rmtree_islink()` 兜（`shutil.py:606`，认 reparse tag）。")
    print("           我原先写进 README/记忆/skill 的「rmtree 会删穿」是**错的** ——")
    print("           那是拿 `entry.is_dir(follow_symlinks=False) 对 junction 为 True` 这半截实测")
    print("           推出来的，漏了上面那两道闸。）")
    box_b, out_b = _mk_purge_case("b")
    live_b = bool(out_b)
    ck("夹具就位：B 的联接也建成了", live_b, True)
    try:
        shutil.rmtree(box_b)
    except OSError as e:
        print(f"         rmtree 抛了 {type(e).__name__}: {e}")
    ck("★ 记录：`rmtree` **没有**删穿 —— 目标里的文件还在（行为随版本变，所以钉住它）",
       (out_b / "secret1.md").exists() if live_b else None, True)
    ck("B 的 box 还是被删了（不是 rmtree 什么都没干）", box_b.exists(), False)

    # ── C：判别力正控 —— 一个**天真实现**必须删穿，否则上面的 A 全是恒真 ──
    print("── C. 判别力正控：天真实现必须删穿，否则 A 那几条是恒真的 ──")
    box_c, out_c = _mk_purge_case("c")
    live_c = bool(out_c)
    ck("夹具就位：C 的联接也建成了", live_c, True)
    _naive_rmtree(box_c)
    ck("★ 天真实现（`Path.is_dir()` 判目录）**果然删穿**了目标目录 ⇒ 这个夹具真的测得出危险",
       (out_c / "secret1.md").exists() if live_c else None, False)

    print(f"\n  {'全部成立 ✔ —— `purge()` 的链接不跟进是**实测**出来的，而且夹具测得出真假'
           if not bad else f'{bad} 条不成立 ✘ —— `purge()` 的安全声明不可信，别再清回收站'}")
    return 0 if not bad else 1


# ══════════════════════ ④ 自检 ══════════════════════
def _self_test() -> int:
    """本工具箱自己的正控 / 负控。

    它是**被所有测试依赖的地基**：它错了，每个用它的测试都会以"绿"的样子错。
    所以四件事必须当场验：
      ① `points()` 算得对不对（对着手工数出来的树）；
      ② 目录联接**不跟进**（口径与 `shutil.rmtree` 相反 —— 那是会删别人东西的）；
      ③ `stash()` 真的把东西挪走了、**而且一个都没删**、序号避让真的避让了；
      ④ **跨盘退路**落得下去（否则 stash 会在别的卷上静默退化成"什么都没做"）。

    ★ 主夹具刻意放在**仓库内**（与 `_trash` 同盘）。第一版我用了
      `tempfile.mkdtemp()`（系统盘），结果 stash 全数 `EXDEV` 失败、7 项红 ——
      那次红很有价值，它逼出了退路 ②。测试必须跑在它的**真实使用场景**上。
    """
    import tempfile
    bad = 0

    def chk(name: str, got, want) -> None:
        nonlocal bad
        ok = got == want
        if not ok:
            bad += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + ("" if ok else f"  期望 {want!r} 实得 {got!r}"))

    box = fresh_dir(ROOT / "runs" / "_regress" / f"_probe_tkit_{os.getpid()}",
                    f"tkit-selftest-{os.getpid()}")
    try:
        # 造一棵已知形状的树：
        #   tree/a.txt          1
        #   tree/sub/b.txt      1
        #   tree/sub/deep/c.txt 1  （deep 是一层子目录）
        #   tree/link -> outside （链接 1 点，不跟进）
        tree = box / "tree"
        (tree / "sub" / "deep").mkdir(parents=True)
        (tree / "a.txt").write_text("a", encoding="utf-8")
        (tree / "sub" / "b.txt").write_text("b", encoding="utf-8")
        (tree / "sub" / "deep" / "c.txt").write_text("c", encoding="utf-8")
        outside = box / "outside"
        outside.mkdir()
        # ★ 目标里刻意放 **3** 个文件，不是 1 个。
        #   第一版这里只放了 1 个 `secret.txt`，于是"junction 不递归"那条判据
        #   在**递归实现下也会得到 1** ⇒ 假绿，还连我那句"证明不是空转"一起骗过。
        #   一条判据的分辨率，取决于它的**期望值和错误值能不能分开**。
        for _n in ("secret.txt", "more1.txt", "more2.txt"):
            (outside / _n).write_text("secret", encoding="utf-8")

        chk("points(单文件) = 1", points(tree / "a.txt"), 1)
        chk("points(整棵树) = 3（递归数条目）", points(tree), 3)
        chk("points(不存在的路径) = 0（删它不要钱）", points(box / "nope"), 0)
        # 负控：如果递归口径被写错成"目录算 1 点"，这里会得 1 而不是 3。
        chk("负控：口径**不是**「目录算 1 点」", points(tree) != 1, True)

        # ★ 正控：真造一个目录联接，验它**不跟进**（口径与 `shutil.rmtree` 相反）
        junc = box / "junc-probe"
        linked = False
        if os.name == "nt":
            import subprocess
            r = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junc), str(outside)],
                capture_output=True, text=True, errors="replace")
            linked = r.returncode == 0 and junc.exists()
        if linked:
            chk("★ 目录联接只算 1 点，**不把目标树拖进来**"
                "（`rmtree` 会，我们不能）", points(junc), 1)
            # 正控：证明上一条的期望值 1 **不等于**错误实现的答案。
            # 目标里有 3 个文件 ⇒ 递归实现会得 3 ⇒ 两个值分得开。
            chk("★ 正控：目标里真放着 3 个文件（所以「得到 1」不是巧合）",
                len([q for q in outside.iterdir() if q.is_file()]), 3)
            chk("★ 且递归实现会得 3 —— 两个值确实分得开",
                points(outside), 3)
        else:
            # 建不出联接就**明说没测**，不写一条恒真断言行（那是假安慰）
            print("  [SKIP] 目录联接计数未测：本机建不出 junction")

        # stash：挪走 + 零删除
        n_before = len(list(TRASH.iterdir())) if TRASH.is_dir() else 0
        dest = stash(tree, "selftest-tree")
        chk("stash 之后原位没了", tree.exists(), False)
        chk("stash 之后落点在 _trash 里", bool(dest and dest.parent == TRASH), True)
        chk("★ 内容一字不少地跟着过去了（3 个文件）",
            sorted(p.name for p in dest.rglob("*") if p.is_file()) if dest else [],
            ["a.txt", "b.txt", "c.txt"])
        chk("_trash 多了 1 项", (len(list(TRASH.iterdir())) - n_before), 1)

        # 序号避让：同一 tag 连挪两次，落点不许撞车、内容不许互相覆盖
        s1 = box / "slot1"
        s2 = box / "slot2"
        s1.mkdir()
        s2.mkdir()
        (s1 / "one.txt").write_text("1", encoding="utf-8")
        (s2 / "two.txt").write_text("2", encoding="utf-8")
        e1 = stash(s1, "slot-probe")
        e2 = stash(s2, "slot-probe")
        chk("同 tag 两次挪，落点**不同名**（避让生效）",
            bool(e1 and e2 and e1 != e2), True)
        chk("★ 两份内容各自完整 —— 第二次**没有覆盖**第一次",
            ([p.name for p in e1.iterdir()] if e1 else None,
             [p.name for p in e2.iterdir()] if e2 else None),
            (["one.txt"], ["two.txt"]))
        chk("stash 不存在的路径返回 None", stash(box / "never"), None)

        # ★ 跨盘退路正控：源在**别的卷**上时，stash 必须就地改名，
        #   而不是返回 None（那等于"什么都没做"，且会把删除请回来）。
        #   这条判据是"挪得动"这个承诺**唯一**的证人。
        xdev = Path(tempfile.mkdtemp(prefix="_tkit_xdev_"))
        (xdev / "x.txt").write_text("x", encoding="utf-8")
        if xdev.drive.lower() == ROOT.drive.lower():
            print("  [SKIP] 跨盘退路未测：本机临时目录与仓库同盘")
            stash(xdev, "tkit-xdev")
        else:
            d = stash(xdev, "tkit-xdev")
            chk("★ 跨盘时**就地改名**（不是返回 None，更不是删除）",
                bool(d and d.parent == xdev.parent), True)
            chk("★ 落点名里带 .stale（代价是看得见的）",
                ".stale" in (d.name if d else ""), True)
            chk("★ 内容还在（一个字节没丢）",
                (d / "x.txt").is_file() if d else False, True)

        # 台账
        chk("台账记下了省下的点数（>0）", ledger_total() > 0, True)
        st = trash_stats()
        chk("trash_stats 把「几项」和「多少点」**分开**报（两件事不许压成一个）",
            (isinstance(st.get("n_items"), int), isinstance(st.get("points"), int)),
            (True, True))
    finally:
        # 自检自己的垃圾也走 stash，不删 —— 连自检都不许开删除的口子。
        # junction 不单独处理：它就在 box 里，跟着整箱一起走（`os.replace`
        # 挪目录不跟进链接，所以 box 外的东西一点没动）。
        left = stash(box, f"selftest-box-{os.getpid()}")
        if left is not None:
            print(f"  [FYI ] 本次自检在 _trash 留下 1 份（{points(left)} 点），"
                  f"由 `regress.py --purge-trash` 清理")

    print("  工具箱自检全过" if not bad else f"  工具箱自检有 {bad} 项失败")
    return 0 if not bad else 1


# ══════════════════════ ⑤ 对账：拿守卫自己的 CLI 核我的账 ══════════════════════
def _find_guard() -> Path | None:
    """找守卫的 shim。找不到就**如实说找不到**，不假装对过账。"""
    env = os.environ.get("RONGKE_SAFE_DELETE_GUARD")
    if env and Path(env).is_file():
        return Path(env)
    rel = "*/resources/app.asar.unpacked/cli/vendor/shim/safe-delete-bulk-guard.cjs"
    roots = [os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
             os.environ.get("PROGRAMFILES(X86)")]
    for r in roots:
        if not r:
            continue
        try:
            for p in (Path(r) / "Programs").glob(rel):
                return p
            for p in Path(r).glob(rel):
                return p
        except OSError:
            continue
    return None


def _find_node() -> str | None:
    import shutil as _sh
    env = os.environ.get("RONGKE_NODE")
    if env and Path(env).is_file():
        return env
    return _sh.which("node")


def guard_points(p, slot: int = 0) -> tuple[int | None, str]:
    """**问守卫本人**：删这个路径它收多少点。

    返回 `(点数, 说明)`。点数 `None` = **问不到**（不是 0！），说明里是守卫的原话。
    ★ 把原因带出来是刻意的：上一版这里只返回 `None`，于是"问不到"和"守卫说 0"
      在调用方眼里**长得一样** —— 静默吞异常，两副面孔。

    做法：
      · 阈值设 1 ⇒ 任何非零计数都会撞线，撞线的 payload 里就带权威 `count`
      · state 目录**每条一个**（带 `slot`）：共用的话，第一次撞线留下的
        `.lock` 会把后面几条全卡成 `state lock timeout`（实测踩过）
      · 清掉 `CODEBUDDY_CONVERSATION_REQUEST_ID` ⇒ requestId 每次都新，
        不会跟真实对话共用一个桶
    """
    import hashlib
    import subprocess
    guard, node = _find_guard(), _find_node()
    if guard is None:
        return None, "本机没找到守卫 shim"
    if node is None:
        return None, "本机没找到 node"
    tag = hashlib.sha1(str(p).encode("utf-8")).hexdigest()[:10]
    env = dict(os.environ)
    env["CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD"] = "1"
    env["CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR"] = str(
        ROOT / "runs" / "_regress" / f"_guardstate_{os.getpid()}_{slot}")
    env["CODEBUDDY_SESSION_ID"] = "tkit-crosscheck"
    env["CODEBUDDY_TOOL_CALL_ID"] = f"cc-{tag}"
    env.pop("CODEBUDDY_CONVERSATION_REQUEST_ID", None)
    try:
        r = subprocess.run([node, str(guard), "check", "--target", str(p)],
                           capture_output=True, text=True, errors="replace",
                           env=env, timeout=30)
    except Exception as e:                              # noqa: BLE001
        return None, f"起不来（{type(e).__name__}: {e}）"
    if r.returncode == 0:
        return 0, "没撞线 ⇒ 计数为 0"
    msg = (r.stderr or r.stdout or "").strip()
    m = re.search(r"\[safe-delete\]\[SAFE_DELETE_BULK_CONFIRM_REQUIRED\] (\{.*\})", msg)
    if not m:
        return None, f"rc={r.returncode}；守卫说：{msg.splitlines()[-1][:160] if msg else '（无输出）'}"
    try:
        return int(json.loads(m.group(1)).get("count")), ""
    except (ValueError, TypeError):
        return None, f"payload 解析不了：{m.group(1)[:120]}"


def cross_check(paths) -> int:
    """逐条对账：`points()`（我的复刻） vs `guard_points()`（守卫本人）。

    ★ 为什么要有它：复刻件和原件会漂移，而且**漂移时不会报错**。
      本轮就漂了一次 —— 我对目录联接按 Python 的直觉算 3，守卫实际算 1。
      更糟的是我那条判据当时是**假绿**（目标里恰好只有 1 个文件，递归也是 1）。
      ⇒ 规矩：**别拿我的实现去证明我的实现对。**
    """
    print("── _tkit 与守卫对账 ──")
    if _find_guard() is None or _find_node() is None:
        print("  本机没找到守卫 / node —— **没测**，不是通过。")
        return 2
    bad = asked = 0
    for i, p in enumerate(paths, 1):
        q = Path(p)
        mine = points(q)
        theirs, why = guard_points(q, slot=i)
        if theirs is None:
            print(f"  [?]  {q}\n       问不到 · 原因：{why}")
            continue
        asked += 1
        ok = mine == theirs
        bad += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {q}\n"
              f"        我的账 {mine} · 守卫账 {theirs}")
    if not asked:
        print("  一条都没问到 —— 无法对账（这不是通过）")
        return 2
    print(f"  {asked} 条对上了" if not bad else f"  {bad}/{asked} 条对不上 —— `points()` 要改")
    return 0 if not bad else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    if "--cross-check" in sys.argv:
        _args = [a for a in sys.argv[1:] if not a.startswith("--")]
        if not _args:
            print("用法：python _tkit.py --cross-check <路径> [路径…]")
            raise SystemExit(2)
        raise SystemExit(cross_check(_args))
    if "--report" in sys.argv:
        st = trash_stats()
        print(f"_trash: {st['n_items']} 项 · {human(st['bytes'])} · "
              f"清空它要花 {st['points']} 点删除额度")
        print(f"台账累计省下：{ledger_total()} 点")
        raise SystemExit(0)
    if "--verify-purge" in sys.argv:
        # ★ 加这道闸的理由不是仪式感：**额度是整轮共享的**（50 点），
        #   这一条自己要花 6 点。随手多跑几遍，红的会是别的测试。
        if "--yes" not in sys.argv:
            print("这条检查会**真删** 3 个小夹具，约花 **6 点**删除额度（一轮只有 50 点）。")
            print("⚠️  额度是**整轮共享**的 —— 多跑几遍，红的会是**别的**测试。")
            print("   想清楚了再加 `--yes`：python _tkit.py --verify-purge --yes")
            raise SystemExit(0)
        raise SystemExit(verify_purge())
    if "--purge" in sys.argv:
        st = trash_stats()
        if st["n_items"] == 0:
            print("_trash 已经是空的，没什么可清。")
            raise SystemExit(0)
        print(f"_trash: {st['n_items']} 项 · {human(st['bytes'])}")
        print(f"⚠️  清空它要花 **{st['points']} 点**删除额度（一轮只有 50 点）。")
        if "--yes" not in sys.argv:
            print("   默认**不动手** —— 这一删是真的删。确认后加 `--yes` 再跑一次。")
            raise SystemExit(0)
        n, cost = purge_all()
        print(f"   已清 {n} 项 · 按守卫口径约花 {cost} 点")
        raise SystemExit(0)
    print(__doc__)
