# -*- coding: utf-8 -*-
"""原页渲染：把 PDF / PPTX / DOCX 渲成逐页 PNG，供右栏「原页阅读」用。

## 为什么要单独一个模块

稿件台原来只能看 markdown。用户要的是**论文/PPT 原页**——
双栏、图表、上下标、公式。那只能靠渲染原件，不能靠抽文本
（实测抽取结果：页眉页脚混排、上下标压平、图表全丢）。

## ★ 一条铁律：pptx 绝不能直出 png

`libreoffice-kit` 的 `render --input x.pptx` 有 bug：内容整体**放大 2 倍**，
右边下边被裁。实测（10×7.5in 画布、方块放在 1.0in、边长 0.5in）：

    render x.pptx --dpi 96    -> 画在 2.00..2.99 in，边长 0.99   ✘
    render x.pdf  --dpi 96    -> 画在 1.00..1.49 in，边长 0.49   ✔

且与 --dpi 无关（48/72/96/192 四档全一样）。
所以 office 类一律 **先 convert 成 pdf，再 render pdf**。

## 缓存

渲染慢（一页零点几秒，几十页就是十几秒），且一份文件会被反复打开。
按**源文件 sha256 + dpi** 做缓存目录名，命中就不重渲。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

# ★ 本仓铁律：**内核包里零删除调用**（hygiene_test 逐文件扫，见 rongke/loop.py
#   里那段说明）。清场一律走**改名**路线，不写删除调用。
#   改名是同盘瞬时操作：不触发删除保护、不烧额度、也不会"删错对象"。
#   优先用仓根的 `_tkit.stash`（它还会记台账）；拿不到就自己就地改名 ——
#   **两条路都是 rename，没有一条是 delete**。


def _retire(p: Path, tag: str = "") -> Path | None:
    """把 `p`（文件或整棵目录）挪出原位，绝不删除。返回落点或 None。

    为什么不用 `_tkit`：`orig.py` 会在**没装整个仓**的场景下被单独用（渲染本身
    只依赖 node + libreoffice-kit）。所以这里自带一份最小改名实现，
    语义与 `_tkit.stash` 的退路②一致（就地改名 `.stale-NNN`）。
    """
    p = Path(p)
    if not (p.exists() or p.is_symlink()):
        return None
    try:
        from _tkit import stash as _stash      # 仓根在 sys.path 上时优先用它
        d = _stash(p, tag or p.name)
        if d is not None:
            return d
    except Exception:
        pass
    # 就地改名：同目录 ⇒ 永远同盘 ⇒ 一定落得下去（跨盘也不怕）
    for i in range(1, 100):
        loc = p.with_name("%s.stale-%03d" % (p.name, i))
        if loc.exists():
            continue
        try:
            os.replace(p, loc)
            return loc
        except OSError:
            continue
    return None                          # 挪不动就原样留着 —— 看得见好过抹掉

# ── LibreOffice Kit 的绝对路径（DSH 自带，不依赖 PATH、不装东西） ──
# ★ 曾经写死 `C:\Users\<某个用户名>\AppData\Local\Programs\DeepSeek Harness\...`。
#   两个问题：那个用户名随包发出去就是隐私泄漏；而别人的 DSH 可能装在别的
#   地方（或根本没装）。改成从环境推导 + 允许用环境变量显式覆盖：
#     RONGKE_LOK_NODE / RONGKE_LOK_CLI 指到具体文件；
#     找不到就是找不到，上层会如实说"这台机器没有 LibreOffice Kit"。
def _dsh_root() -> Path | None:
    """DSH 的**安装根**（含 resources/ 的那一层），从环境推。

    ⚠️ 不要拿 `DSH_HOME` 当安装根：实测它指的是 `C:\\Users\\<你>\\.dsh`，
    那是 DSH 的**用户数据目录**，不是程序安装目录 —— 用它推出来的
    node.exe / cli.js 全都不存在（我第一版就是这么写的，被探针抓出来了）。
    安装根只认两处：显式覆盖变量，以及 %LOCALAPPDATA%\\Programs\\DeepSeek Harness。
    """
    for env in ("RONGKE_DSH_ROOT", "DSH_INSTALL_ROOT"):
        v = os.environ.get(env)
        if v and (Path(v) / "resources").is_dir():
            return Path(v)
    cands: list[Path] = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(Path(la) / "Programs" / "DeepSeek Harness")
    cands.append(Path.home() / "AppData" / "Local" / "Programs" / "DeepSeek Harness")
    for cand in cands:
        if (cand / "resources").is_dir():
            return cand
    return None


def _lok_paths() -> tuple[Path, Path]:
    n = os.environ.get("RONGKE_LOK_NODE")
    c = os.environ.get("RONGKE_LOK_CLI")
    if n and c:
        return Path(n), Path(c)
    root = _dsh_root()
    if root is None:
        # 返回一对不存在的路径：让上层"探不到"这件事以原来的方式暴露，
        # 而不是在这里抛异常（本模块的契约是尽量不炸）。
        return Path("__no_dsh__") / "node.exe", Path("__no_dsh__") / "cli.js"
    return (
        root / "resources" / "runtime" / "primary-runtime" / "dependencies"
        / "node" / "bin" / "node.exe",
        root / "resources" / "app.asar.unpacked" / "dsh" / "node_modules"
        / "@deepseek-ai" / "libreoffice-kit" / "lib" / "cli.js",
    )


_LOK_NODE, _LOK_CLI = _lok_paths()

#: office 系必须经 pdf 中转（见模块 docstring 的 2 倍 bug）
_DIRECT_PDF = {".pdf"}
_VIA_PDF = {".pptx", ".ppt", ".odp", ".docx", ".doc", ".odt"}

DEFAULT_DPI = 110
MAX_PAGES = 400                      # 防一份巨型 PDF 把磁盘塞满
_CACHE_ENV = "RONGKE_ORIG_CACHE"

_lock = threading.Lock()


def available() -> tuple[bool, str]:
    """渲染器在不在。返回 (能不能用, 为什么不能)。"""
    if not _LOK_NODE.exists():
        return False, "找不到 node：" + str(_LOK_NODE)
    if not _LOK_CLI.exists():
        return False, "找不到 libreoffice-kit：" + str(_LOK_CLI)
    return True, ""


def cache_root() -> Path:
    """缓存根目录。可用 RONGKE_ORIG_CACHE 覆盖（测试要用独立目录）。"""
    env = os.environ.get(_CACHE_ENV)
    if env:
        return Path(env)
    return Path.home() / ".dsh" / "papers" / "workbench" / "data" / "orig_cache"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _run(args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _parse_manifest(stdout: str):
    """CLI 回的是 JSON。**只用它的 images 个数**，不用里面的 path
    ——path 经管道回传时中文会乱码（按 GBK 解过），拿它开文件必炸。"""
    try:
        return json.loads(stdout)
    except Exception:
        return None


def peek(src: Path, dpi: int = DEFAULT_DPI) -> dict:
    """**只查缓存，绝不渲染**。命中就回页数，没命中就说不命中。

    存在的理由：`render()` 在缓存未命中时会**真的去渲**（几十页论文十几秒）。
    `/api/orig/open?probe=1` 那条路的语义是"告诉我大概要多久、多少页，
    别自作主张开始渲"（前端自己有一个「开始渲染」按钮）。但它原先直接调
    `render()` —— 于是"探测"变成了"全量渲染"：一个自称 probe 的请求
    占住工作线程十几秒，10 秒超时的客户端先走，服务端还在渲，
    最后把 `ConnectionAbortedError` 打进日志，回归据此判失败。
    实测就是这么被它拖红过一次。

    语义上这也更对：探测就该是**廉价**的。要真渲就走 `/api/orig/render`。
    """
    src = Path(src)
    if not src.exists():
        return {"ok": False, "error": "文件不存在：" + str(src), "cached": False}
    ext = src.suffix.lower()
    if ext not in _DIRECT_PDF and ext not in _VIA_PDF:
        return {"ok": False, "cached": False,
                "error": "这类文件不能原页渲染：" + (ext or "(无扩展名)")}
    try:
        digest = _sha256(src)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "cached": False, "error": "读不了这个文件：" + str(e)}
    outdir = cache_root() / (digest[:16] + "_d%d" % dpi)
    meta_f = outdir / "meta.json"
    if not meta_f.exists():
        return {"ok": False, "cached": False, "miss": True}
    try:
        m = json.loads(meta_f.read_text(encoding="utf-8"))
        if m.get("pages") and all((outdir / p["file"]).exists() for p in m["pages"]):
            m["cached"] = True
            return m
    except Exception:
        pass                       # 缓存坏了就当没有 —— 但**也不在这里重渲**
    return {"ok": False, "cached": False, "miss": True}


def render(src: Path, dpi: int = DEFAULT_DPI, force: bool = False) -> dict:
    """把 src 渲成逐页 PNG，返回 {ok, pages:[{n, file, width, height}], dir, cached, ...}。

    幂等：同一份文件同样 dpi，第二次直接命中缓存。
    """
    src = Path(src)
    if not src.exists():
        return {"ok": False, "error": "文件不存在：" + str(src)}
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}
    ext = src.suffix.lower()
    if ext not in _DIRECT_PDF and ext not in _VIA_PDF:
        return {"ok": False, "error": "这类文件不能原页渲染：" + (ext or "(无扩展名)")}

    digest = _sha256(src)
    outdir = cache_root() / (digest[:16] + "_d%d" % dpi)
    meta_f = outdir / "meta.json"

    if not force and meta_f.exists():
        try:
            m = json.loads(meta_f.read_text(encoding="utf-8"))
            # 顺手校验图还在（缓存目录被清理过就不能算命中）
            if m.get("pages") and all((outdir / p["file"]).exists() for p in m["pages"]):
                m["cached"] = True
                return m
        except Exception:
            pass                       # 缓存坏了就当没有，重渲一遍

    with _lock:                        # 同一份文件可能被两处同时点开
        if not force and meta_f.exists():
            try:
                m = json.loads(meta_f.read_text(encoding="utf-8"))
                if m.get("pages") and all((outdir / p["file"]).exists() for p in m["pages"]):
                    m["cached"] = True
                    return m
            except Exception:
                pass
        return _render_locked(src, ext, digest, outdir, dpi)


def _render_locked(src: Path, ext: str, digest: str, outdir: Path, dpi: int) -> dict:
    t0 = time.time()
    cache_root().mkdir(parents=True, exist_ok=True)
    # ★ CLI 只 mkdir 一级：父目录要自己建，**目标目录本身不能预建**（预建 EEXIST）。
    #   上一次渲染若留下半份，挪走而不是删 —— 挪走的落点是看得见的。
    if outdir.exists():
        _retire(outdir, "orig-render-" + outdir.name)

    workdir = outdir.parent / (outdir.name + ".work")
    if workdir.exists():
        _retire(workdir, "orig-work-" + outdir.name)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        pdf_path = src
        converted = False
        if ext in _VIA_PDF:
            # office -> pdf（唯一正确的第一步）
            r = _run([str(_LOK_NODE), str(_LOK_CLI), "convert",
                      "--input", str(src), "--output", str(workdir / "doc.pdf")])
            if r.returncode != 0:
                return {"ok": False, "error": "转 PDF 失败：" + (r.stderr or r.stdout or "")[:300],
                        "stage": "convert"}
            pdf_path = workdir / "doc.pdf"
            if not pdf_path.exists():
                return {"ok": False, "error": "转 PDF 后产物不见了", "stage": "convert"}
            converted = True

        r = _run([str(_LOK_NODE), str(_LOK_CLI), "render",
                  "--input", str(pdf_path), "--output-dir", str(outdir),
                  "--dpi", str(dpi)])
        if r.returncode != 0:
            return {"ok": False, "error": "渲染失败：" + (r.stderr or r.stdout or "")[:300],
                    "stage": "render"}
        man = _parse_manifest(r.stdout) or {}

        # 用**自己知道的目录**拼路径，不用 manifest 里的（中文乱码）
        files = sorted(outdir.glob("page-*.png"))
        if not files:
            return {"ok": False, "error": "渲染完成但一张图都没有", "stage": "render"}
        pages = [{"n": i + 1, "file": f.name} for i, f in enumerate(files)]

        out = {
            "ok": True,
            "pages": pages,
            "pageCount": len(pages),
            "dir": str(outdir),
            "cached": False,
            "dpi": dpi,
            "source": str(src),
            "digest": digest,
            # converted=True 说明走过 pdf 中转 —— 前端可在「技术细节」里如实说
            "converted": converted,
            "minutes": round((time.time() - t0) / 60.0, 2),
            "seconds": round(time.time() - t0, 1),
            # manifest 声明的总页数（可能 > 我们截的，用于如实说明"只渲了前 N 页"）
            "declared": man.get("pageCount"),
        }
        (outdir / "meta.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return out
    finally:
        # 中转目录用完就挪走（不删）。它可能带着一份转换出来的 PDF，
        # 留着只会越堆越多；挪走则由 _trash 的台账统管。
        _retire(workdir, "orig-work-done-" + outdir.name)


def page_path(src: Path, n: int, dpi: int = DEFAULT_DPI) -> Path | None:
    """取第 n 页（1-based）的 PNG 路径；没渲过就返回 None。"""
    d = render(src, dpi=dpi)
    if not d.get("ok"):
        return None
    for p in d["pages"]:
        if p["n"] == n:
            return Path(d["dir"]) / p["file"]
    return None
