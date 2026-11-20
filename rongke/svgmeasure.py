# -*- coding: utf-8 -*-
"""SVG 质量门第二层：浏览器真尺子（可选依赖，缺件如实判审不了）。

## 它量什么、不量什么

✅ 量：每段 `<text>` 的**真实占位** —— `getBBox()` 经 `getCTM()` 变换回
   viewBox 坐标（所以旋转文字也对），外加 `getComputedTextLength()` 的前进宽度。
   这一份真值喂给 `svgq` 的文字容纳门与碰撞门，把它们从「估算级」升到「实测级」。

✅ 量：字体栈里**声明的中文字体在这台机器上到底存不存在**。判据是 canvas
   探针：同一个探针串分别用 `"候选字体", monospace/sans-serif/serif` 渲染，
   与三条通用基线**全都同宽**才判它不在本机。三条而不是一条，是因为单条
   「不存在的字体名」基线会回退到系统默认字体，中文 Windows 上那就是微软雅黑
   本身 —— 单基线法会把装了的雅黑判成没装（本机实测踩过）。这是可判定的。

❌ 不量：逐字字形覆盖（"这个字有没有烤成豆腐块"）。浏览器没有 API 能告诉你
   某个码点最终用了哪个字体，像素法探针在本仓有过负控失败被删的前例。
   所以本模块的 PASS 只代表「字体栈里确实有可用的中文字体」，返回值里
   `glyph_basis` 把这句话写死，`svgq` 照抄进门的 detail，**不许放大成逐字保证**。

## 缺件纪律

playwright 没装 / 浏览器起不来 / 页面加载失败 ⇒ `ok=False` 并写明原因。
`svgq` 收到 `ok=False` 会把相关门退回 `NOT AUDITABLE`，**绝不判 PASS**。

「本进程没装」不等于「量不了」：荣珂可能跑在托管 3.13 上，而 playwright 只装在
系统 Python312 —— 与 matplotlib/PyMuPDF 同一种处境。所以 `measure()` 会按
`figure.python` 那套找一个装了的解释器走子进程（复用 `figure._PY_CANDIDATES`
与 `_interpreter_ok`，不另写一套发现逻辑）。两条路都不通才如实判审不了。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

#: 显式指定浏览器可执行文件（与 figure.python 同一种「用户环境自己说了算」的做法）
ENV_BROWSER = "RONGKE_BROWSER"

#: 量不到就跳过、并记进 unchecked —— 借 diagram-audit 的 instrument.js 口径：
#: `try { getBBox() } catch { continue }`。一个量不到的节点不该让整份报告变成假通过。
_MEASURE_JS = r"""
async () => {
  if (document.fonts && document.fonts.ready) { try { await document.fonts.ready; } catch (e) {} }
  const texts = Array.from(document.querySelectorAll('text'));
  const boxes = [], unchecked = [], families = new Set();
  for (const el of texts) {
    const id = el.getAttribute('id') || '';
    const txt = el.textContent || '';
    let b;
    try { b = el.getBBox(); } catch (e) {
      unchecked.push({id: id, text: txt.slice(0, 24), reason: 'getBBox 抛错: ' + e});
      continue;
    }
    if (!b || (b.width === 0 && b.height === 0)) {
      unchecked.push({id: id, text: txt.slice(0, 24), reason: 'getBBox 量出零尺寸'});
      continue;
    }
    // getBBox 是元素自己用户空间的框，**不含自身 transform**。
    // 经 getCTM 变回 viewBox 坐标，旋转文字（y 轴标签）才对。
    const m = el.getCTM();
    const corners = [[b.x, b.y], [b.x + b.width, b.y],
                     [b.x, b.y + b.height], [b.x + b.width, b.y + b.height]];
    const pts = corners.map(([x, y]) => m
      ? {x: m.a * x + m.c * y + m.e, y: m.b * x + m.d * y + m.f}
      : {x: x, y: y});
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const x0 = Math.min.apply(null, xs), y0 = Math.min.apply(null, ys);
    let adv = null;
    try { adv = el.getComputedTextLength(); } catch (e) { adv = null; }
    const fam = getComputedStyle(el).fontFamily || '';
    families.add(fam);
    boxes.push({id: id, text: txt, x: x0, y: y0,
                width: Math.max.apply(null, xs) - x0,
                height: Math.max.apply(null, ys) - y0,
                advance: adv, family: fam,
                size: parseFloat(getComputedStyle(el).fontSize) || null});
  }
  // 字体可用性探针。**三条通用基线，与任一条不同即判在**。
  // ★ 为什么不能只用一个「故意不存在的字体名」当基线：那个名字会回退到浏览器
  //   默认字体，而中文 Windows 上默认 sans-serif 就是微软雅黑 —— 于是被测的
  //   微软雅黑与基线宽度一模一样，**明明装了却判不在**（本机实测 msyh.ttc 在，
  //   单基线法给出 1342.626953125 == 1342.626953125）。若一台机器只装了默认
  //   那一个中文字体，这个假阴性会直接翻成 missing_fonts ⇒ 假 FAIL。
  //   候选后面显式接上通用族，才能保证「字体缺席」时回退到**已知那条基线**。
  const probe = '荣珂受体激活ilijIWMmw0123';
  const cv = document.createElement('canvas');
  const ctx = cv.getContext('2d');
  const widthIn = (font) => { ctx.font = font; return ctx.measureText(probe).width; };
  const generics = ['monospace', 'sans-serif', 'serif'];
  const base = generics.map(g => widthIn('100px ' + g));
  const present = [], absent = [];
  const seen = new Set();
  for (const stack of families) {
    for (const raw of stack.split(',')) {
      const name = raw.trim().replace(/^["']|["']$/g, '');
      if (!name || seen.has(name)) continue;
      seen.add(name);
      if (/^(sans-serif|serif|monospace|cursive|fantasy|system-ui|inherit|initial)$/i.test(name)) continue;
      const safe = name.replace(/"/g, '');
      let hit = false, measurable = true;
      for (let i = 0; i < generics.length; i++) {
        let w = null;
        try { w = widthIn('100px "' + safe + '", ' + generics[i]); } catch (e) { w = null; }
        if (w === null) { measurable = false; break; }
        if (Math.abs(w - base[i]) > 0.5) hit = true;
      }
      if (!measurable) { absent.push(name); continue; }
      (hit ? present : absent).push(name);
    }
  }
  return {boxes: boxes, unchecked: unchecked,
          fonts_present: present, fonts_absent: absent,
          n_texts: texts.length};
}
"""


def _candidates() -> list[str | None]:
    """浏览器可执行文件候选。None = 交给 playwright 自己的版本注册表。"""
    out: list[str | None] = []
    env = os.environ.get(ENV_BROWSER, "").strip()
    if env:
        out.append(env)
    cache = Path(os.path.expanduser("~/AppData/Local/ms-playwright"))
    if cache.is_dir():
        hits = sorted(cache.glob("chromium-*/chrome-win64/chrome.exe"),
                      reverse=True)
        out.extend(str(h) for h in hits[:2])
    out.append(None)
    return out


def available() -> tuple[bool, str]:
    """快速探一次**本进程**能不能跑第二层，不启动浏览器。

    判 False 不代表第二层没救：`measure()` 还会按 `figure.python` 那套
    找一个装了 playwright 的解释器走子进程（荣珂可能跑在托管 3.13 上，
    而 playwright 只装在系统 Python312 —— 与 matplotlib/PyMuPDF 同一种处境）。
    """
    if importlib.util.find_spec("playwright") is None:
        return False, "本进程未安装 playwright（可选依赖）"
    return True, ""


def find_measure_python(explicit: str = "") -> str | None:
    """找一个装了 playwright 的解释器。找不到返回 None，**不降级成假通过**。

    ★ 复用 `figure.py` 的候选表与探测缓存，不另写一套解释器发现逻辑：
      两处各写一份的结果是「配置改了 figure.python，SVG 这边却没跟上」。
    """
    from . import figure as _fig

    seen: set[str] = set()
    order = ([explicit.strip()] if explicit and explicit.strip() else []) \
        + [sys.executable] + list(_fig._PY_CANDIDATES)
    for exe in order:
        if not exe or exe in seen:
            continue
        seen.add(exe)
        if not Path(exe).exists():
            continue
        if _fig._interpreter_ok(exe, ("playwright",)):
            return exe
    return None


#: 子进程入口。**只调 `_measure_local`**，绝不调 `measure()` —— 后者会再找
#: 解释器、再起子进程，无限递归。
_CHILD = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from rongke.svgmeasure import _measure_local\n"
    "print(json.dumps(_measure_local(sys.argv[2], timeout=float(sys.argv[3])),"
    " ensure_ascii=False))\n"
)


def measure(svg: str | Path, *, timeout: float = 30.0,
            python: str = "") -> dict[str, Any]:
    """量一份 SVG 的真实文本几何。返回形状对齐 `figure.py` 的几何探针报告。

    两条路：本进程装了 playwright 就直接量；没装就按 `python`（一般是
    `cfg.figure.python`）找一个装了的解释器走子进程。两条都不通 ⇒
    `ok=False` + 写明卡在哪，`svgq` 据此退回 NOT AUDITABLE。

    **不抛异常**：任何失败都变成 `ok=False` + `error`。
    """
    p = Path(svg)
    if not p.is_file():
        return _blank(f"SVG 不存在：{p}")
    ok, _why = available()
    if ok:
        return _measure_local(p, timeout=timeout)

    from . import figure as _fig

    exe = find_measure_python(python)
    if not exe:
        return _blank(
            "第二层跑不了：本进程没有 playwright，也没找到装了它的解释器"
            + (f"（配置/候选里试了 {python!r}）" if python else "")
            + "。第一层估算级照跑，字形覆盖如实判审不了。")
    root = str(Path(__file__).resolve().parent.parent)
    try:
        # 子进程自己还要起浏览器，死线必须比浏览器那段宽，否则量到一半被掐断
        r = _fig._run([exe, "-c", _CHILD, root, str(p), str(timeout)],
                      cwd=None, timeout=timeout + 60.0)
    except Exception as e:                                   # noqa: BLE001
        return _blank(f"第二层子进程起不来（{exe}）：{type(e).__name__}: {e}")
    out = _fig._dec(r.stdout)
    err = _fig._dec(r.stderr)
    if r.returncode != 0:
        return _blank(f"第二层子进程 rc={r.returncode}（{exe}）：{err[-400:] or out[-400:]}")
    try:
        rep = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception as e:                                   # noqa: BLE001
        return _blank(f"第二层子进程输出解析不了：{type(e).__name__}: {e}；{out[-300:]}")
    if not isinstance(rep, dict) or "ok" not in rep:
        return _blank(f"第二层子进程返回的形状不对：{str(rep)[:200]}")
    rep["engine"] = f"{rep.get('engine') or '?'}（子进程 {Path(exe).name}）"
    return rep


def _blank(error: str) -> dict[str, Any]:
    """失败报告。**形状与成功时完全一致**，调用方不需要分支去猜有哪些键。"""
    return {
        "ok": False, "engine": "", "boxes": [], "fonts": [],
        "fonts_absent": [], "missing_fonts": [], "texts_unchecked": [],
        "n_spans": 0, "n_compared": 0, "n_boxes": 0, "findings": [],
        "thresholds": {"fail_pct": 15.0, "warn_pct": 2.0},
        "glyph_basis": ("判的是字体栈里声明的中文字体在本机是否可用，"
                        "**不是逐字字形覆盖**"),
        "error": error,
    }


def _measure_local(svg: str | Path, *, timeout: float = 30.0) -> dict[str, Any]:
    """在**本进程**里量。调用方负责确认 playwright 可用（`measure()` 已确认）。"""
    p = Path(svg)
    rep = _blank("")
    if not p.is_file():
        rep["error"] = f"SVG 不存在：{p}"
        return rep
    ok, why = available()
    if not ok:
        rep["error"] = why
        return rep
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                                   # noqa: BLE001
        rep["error"] = f"playwright 导入失败：{type(e).__name__}: {e}"
        return rep

    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<style>html,body{margin:0;padding:0;background:#fff}</style>"
            "</head><body>" + p.read_text(encoding="utf-8") + "</body></html>")
    last = ""
    for exe in _candidates():
        try:
            with sync_playwright() as pw:
                kw: dict[str, Any] = {
                    # 环境里有 HTTP_PROXY，连本地内容都可能被拦（ui_shot 踩过 502）
                    "proxy": {"server": "direct://"},
                    "args": ["--no-proxy-server", "--force-color-profile=srgb",
                             "--font-render-hinting=none"],
                    "timeout": timeout * 1000,
                }
                if exe:
                    kw["executable_path"] = exe
                browser = pw.chromium.launch(**kw)
                try:
                    ver = browser.version
                    page = browser.new_page(viewport={"width": 1400, "height": 1000})
                    page.set_content(html, wait_until="load",
                                     timeout=timeout * 1000)
                    data = page.evaluate(_MEASURE_JS)
                finally:
                    browser.close()
            rep["engine"] = f"chromium {ver} via playwright"
            rep["boxes"] = list(data.get("boxes") or [])
            rep["n_spans"] = int(data.get("n_texts") or 0)
            rep["n_boxes"] = len(rep["boxes"])
            rep["n_compared"] = len(rep["boxes"])
            rep["fonts"] = list(data.get("fonts_present") or [])
            rep["fonts_absent"] = list(data.get("fonts_absent") or [])
            rep["texts_unchecked"] = [
                f"{u.get('id') or '?'}「{u.get('text')}」：{u.get('reason')}"
                for u in (data.get("unchecked") or [])]
            # 只有当**整个栈里没有一个字体可用**时才算缺字回退。
            # 单个候选缺席是正常的（字体栈本来就是回退链）。
            rep["missing_fonts"] = (rep["fonts_absent"]
                                    if not rep["fonts"] and rep["fonts_absent"]
                                    else [])
            rep["ok"] = True
            return rep
        except Exception as e:                               # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            continue
    rep["error"] = f"浏览器起不来或量不了（试过 {len(_candidates())} 个候选）：{last}"
    return rep