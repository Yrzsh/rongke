"""版本号的单一真源守卫。

`CHANGELOG.md` 里写着「版本号的单一真源是 `rongke/__init__.py` 的
`__version__`，本文件顶部的版本号必须与它一致，`changelog_test.py`
会机械核对」。在写这条测试之前，**那个承诺是空的** —— 文件不存在。

一条没人执行的承诺比没有承诺更糟：它让人以为有守卫，于是不再人工看。

本测试守三件事：
  [1] `__version__` 是合法的语义化版本
  [2] `pyproject.toml` 用的是 `dynamic = ["version"]` 且指向它
         （而不是又手写了一个版本号 —— 那正是"两处真相"）
  [3] 装出来的包版本与 `__version__` 一致
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_fail = 0


def ok(m):
    print(f"✔ {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"✘ {m}")


# ── [1] __version__ 合法 ──
import rongke

v = str(rongke.__version__)
SEMVER = re.compile(r"^\d+\.\d+\.\d+"
                    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
if SEMVER.match(v):
    ok(f"__version__ 是合法语义化版本（{v}）")
else:
    bad(f"__version__ 不是合法语义化版本：{v!r}")

# ── [2] pyproject 用 dynamic 指向它 ──
pt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
if 'dynamic' in pt and 'version' in pt:
    ok("pyproject 声明了 dynamic version")
else:
    bad("pyproject 没声明 dynamic version")

m = re.search(r'\[tool\.setuptools\.dynamic\](.*?)(?=\n\[|\Z)', pt, re.S)
if not m:
    bad("pyproject 里没有 [tool.setuptools.dynamic] 段")
else:
    seg = m.group(1)
    vm = re.search(r'version\s*=\s*\{\s*attr\s*=\s*"([^"]+)"', seg)
    if not vm:
        bad("dynamic.version 没用 attr 指向代码里的版本")
    elif vm.group(1) != "rongke.__version__":
        bad(f"dynamic.version 指向了 {vm.group(1)!r}，期望 rongke.__version__")
    else:
        ok("dynamic.version 指向 rongke.__version__（唯一真源）")

# ★ 关键负向判据：`[project]` 段里**不许**再有一个字面 version。
m = re.search(r"^\[project\](.*?)(?=^\[)", pt, re.S | re.M)
if not m:
    bad("pyproject 里没有 [project] 段")
else:
    seg = m.group(1)
    if re.search(r"^version\s*=", seg, re.M):
        bad("[project] 里手写了 version —— 那就有两个真源了")
    else:
        ok("[project] 里没有手写 version（只有 dynamic 那一处）")

# ── [3] 装出来的元数据与 __version__ 一致（只在本机装过时查） ──
try:
    from importlib.metadata import version as _md_version, PackageNotFoundError
    try:
        installed = _md_version("rongke")
    except PackageNotFoundError:
        installed = ""
    if installed:
        if installed == v:
            ok(f"已安装的包版本与 __version__ 一致（{installed}）")
        else:
            bad(f"已安装版本 {installed} != __version__ {v}")
    else:
        print("· 本机没装成包，跳过安装元数据核对（源码树里直接跑）")
except Exception as e:                                          # noqa: BLE001
    print(f"· 安装元数据没读到（{type(e).__name__}），跳过")

# ── [4] CHANGELOG 顶部版本与 __version__ 的关系 ──
cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
# 顶部是「未发布」时是正常的（开发中还没打版本）
first_ver = re.search(r"^##\s*\[?([0-9][^\]\s]*)\]?", cl, re.M)
if "## [未发布]" in cl or "## [Unreleased]" in cl:
    ok("CHANGELOG 顶部是「未发布」段（开发中）")
elif first_ver:
    if first_ver.group(1) == v:
        ok(f"CHANGELOG 顶部版本与 __version__ 一致（{v}）")
    else:
        bad(f"CHANGELOG 顶部 {first_ver.group(1)} != __version__ {v}")
else:
    bad("CHANGELOG 里找不到版本段")

# ── [5] CHANGELOG 里提到的测试文件必须真实存在 ──
# ★ 这条是被自己的缺陷逼出来的：CHANGELOG 原文写着「changelog_test.py
#   会机械核对」，而这个文件当时并不存在 —— 一句无法执行的承诺。
cited = set(re.findall(r"`([A-Za-z0-9_\-]+_test\.py)`", cl))
if not cited:
    print("· CHANGELOG 没点名任何测试文件")
for name in sorted(cited):
    if (ROOT / name).is_file():
        ok(f"CHANGELOG 点名的 {name} 真实存在")
    else:
        bad(f"CHANGELOG 点名了 {name}，但文件不存在 —— 空承诺")

# ── [6] LICENSE 存在且被 pyproject 接线 ──
if (ROOT / "LICENSE").is_file():
    ok("LICENSE 存在")
else:
    bad("LICENSE 不存在")
if re.search(r'license\s*=\s*\{\s*file\s*=\s*"LICENSE"', pt) or \
        re.search(r'license-files\s*=', pt):
    ok("pyproject 里接线了 LICENSE")
else:
    bad("pyproject 没接线 LICENSE")

# ── [7] 发行件的基本礼貌：`--version` / `--help` 真的能用 ──
# 为什么这一条归在这里：这两个入口是**用户装上之后第一个会敲的**东西，
# 而它们坏了不会有任何测试红 —— 直到有人去敲。实测就是如此：
# `pip install -e .` 之后 `rongke --version` 报
# `unrecognized arguments: --version`，而 51 个套件全绿。
#
# 判据打在**真进程的 stdout** 上，不 import 完事 —— argparse 的
# `action="version"` 是在解析期抛 SystemExit，import 层面看不见它。
import subprocess as _sp
import sys as _sys


def _run_cli(*args, timeout=120):
    return _sp.run([_sys.executable, "-m", "rongke", *args],
                   cwd=str(ROOT), capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout)


try:
    r = _run_cli("--version")
    if r.returncode == 0 and __import__("rongke").__version__ in (r.stdout or ""):
        ok(f"`rongke --version` 可用且打印的是唯一真源里的版本（{r.stdout.strip()}）")
    else:
        bad(f"`rongke --version` 不可用：rc={r.returncode} "
            f"out={ (r.stdout or '').strip()[:60]!r} err={(r.stderr or '').strip()[:120]!r}")
    # 负控：这把尺子能响 —— 敲一个**不存在**的开关，必须非零
    r2 = _run_cli("--no-such-flag-at-all")
    if r2.returncode != 0:
        ok("负控：不存在的开关确实非零退出（判据不是恒绿）")
    else:
        bad("负控失败：不存在的开关居然返回 0 —— 这条判据在测空气")
except Exception as e:                                    # noqa: BLE001
    bad(f"跑 CLI 时异常：{type(e).__name__}: {e}")

print()
if _fail:
    print(f"✘ 失败 {_fail} 项")
    sys.exit(1)
print("✔ 版本与 CHANGELOG 守卫通过")
