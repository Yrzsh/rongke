# -*- mode: python ; coding: utf-8 -*-
"""荣珂 Windows 冻结版（onedir）。

为什么 onedir 而不是 onefile：
  * Inno Setup 的 [Files] 直接收一棵目录，卸载/更新都是文件级操作；
  * onefile 每次启动都要把自己解压到临时目录，首启明显变慢，杀软也更容易拦；
  * 出问题时 `_internal/` 里的东西看得见，能直接判"是漏资产还是代码问题"。

datas 按目录枚举，不手写 11 个静态文件名 —— 以后界面再加一个 js/css，
这里不会静默漏掉。
"""
import glob
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.dirname(SPECPATH)
WEB = os.path.join(ROOT, "rongke", "web")

datas = [
    (os.path.join(WEB, "index.html"), "rongke/web"),
    (os.path.join(WEB, "app.ico"), "rongke/web"),
]
datas += [(p, "rongke/web/static") for p in sorted(glob.glob(os.path.join(WEB, "static", "*")))]

# ── 能力层栈的资产（2026-11-05 补，之前冻结版一份都没有）──────────────────
# preset.presets_dir() = project_root()/rongke/presets；冻结版 project_root()
# 落在 _MEIPASS，所以目标目录必须是 `rongke/presets`，与 web 那两条同理。
# caps.external_cap_dirs() 第一条 = Path(caps.py).parent/caps_ext，按 *.py
# 扫盘加载；不放进 datas 的话，安装版会静默少掉两个随库能力，而预设里
# 点名 `@external/lit-search` 只会报「解析不到」，用户无从判断少了什么。
# 下划线开头的探针/试验文件不发布。
datas += [(p, "rongke/presets")
          for p in sorted(glob.glob(os.path.join(ROOT, "rongke", "presets", "*.json")))]
datas += [(p, "rongke/caps_ext")
          for p in sorted(glob.glob(os.path.join(ROOT, "rongke", "caps_ext", "*.py")))
          if not os.path.basename(p).startswith("_")]

# ── 自研最小技能（2026-11-05 开源双轨补）──────────────────────────────
# `vendor/skills/` 是 CC BY-NC-SA 4.0 第三方内容，不进公开仓也不进发行件。
# `stages.SKILL_ROOTS` 会扫包内 `rongke/skills/<name>/SKILL.md`；冻结版
# 少了这一层，`stats-review` 预设在装好的软件里会报「专属技能读不到」。
# 按目录枚举（不是写死一个技能名），以后新增自研技能不会静默漏掉。
_skill_root = os.path.join(ROOT, "rongke", "skills")
for _sd in sorted(glob.glob(os.path.join(_skill_root, "*"))):
    if os.path.isdir(_sd):
        _dest = os.path.join("rongke", "skills", os.path.basename(_sd))
        datas += [(p, _dest)
                  for p in sorted(glob.glob(os.path.join(_sd, "*.md")))]

# 核心是纯标准库；但 server/loop 里有大量惰性导入，交给 collect_submodules 扫全。
hiddenimports = collect_submodules("rongke")
# workbench 的 litkb 是外部文件，PyInstaller 看不到它的 import：
# sqlite3 是它建 FTS 索引用的标准库，别的核心路径没用到，必须显式带上。
hiddenimports += ["sqlite3"]
binaries = []

# ── 内嵌窗口（pywebview + WebView2）──────────────────────────────────────
# 冻结版要"自己就是窗口宿主"，pywebview 的 winforms 后端是按平台字符串
# 动态导入的，静态分析看不到，必须显式列出。
# webview/lib 下的 WebView2 托管 DLL 与 Python.Runtime.dll 由
# pyinstaller-hooks-contrib 的 hook-webview / hook-clr 自动收集。
# WebView2 Runtime 本身是系统组件（随 Edge 分发），不打进安装包 ——
# 因此 app.py 保留了"运行时缺失就回退 Edge --app"的判定。
hiddenimports += ["webview", "webview.platforms.winforms"]

# ── workbench 是外部可选的 Python 包（~/.dsh/papers/workbench）────────────
# bridge.py 在**进程内**导入它；冻结版若不把这些依赖打进来，import 会失败，
# 结果就是安装版只有 29 个内置工具、文献库/检索/入库全部消失
# （2026-10-29 实测：源码 61 个，安装版 29 个）。
# 这里只打 workbench 工具**运行所需**的那几组；matplotlib/pandas 等仍排除，
# 它们由被调用的外部解释器提供，不是本进程的依赖。
for pkg in ("pydantic", "pydantic_core", "pydantic_settings",
            "anyio", "httpx", "starlette", "sse_starlette",
            "fitz", "pymupdf"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:                              # noqa: BLE001
        print(f"[spec] collect_all({pkg!r}) skipped: {type(e).__name__}: {e}")

# ★ 不能 collect_all("mcp")：它会扫到 mcp.cli，而 mcp.cli 依赖 typer
#   （只装了 mcp 本体时没有）→ 整个 collect 失败，结果 mcp 一个都没打进去。
#   workbench 只 import mcp.server.fastmcp，收 server 子模块足够。
try:
    datas += collect_data_files("mcp")
    hiddenimports += ["mcp", "mcp.server", "mcp.server.fastmcp"]
    hiddenimports += collect_submodules("mcp.server")
except Exception as e:                                  # noqa: BLE001
    print(f"[spec] mcp.server collection skipped: {type(e).__name__}: {e}")

# 这些要么只在被调用的子进程/生成的脚本字符串里出现，要么是开发依赖。
excludes = [
    "matplotlib", "pandas", "numpy", "scipy", "openpyxl",
    "pptx", "PIL", "lxml",
    "win32com", "pythoncom", "pywin32",
    "playwright", "pytest", "tkinter",
]

a = Analysis(
    [os.path.join(ROOT, "packaging", "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Rongke",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(WEB, "app.ico"),
    version=os.path.join(ROOT, "build", "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Rongke",
)
