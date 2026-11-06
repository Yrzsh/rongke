# -*- coding: utf-8 -*-
"""给 Web 控制台拍真实截图 —— 用真实浏览器、灌真实事件流。

为什么要有这个：
    上一轮我报告「UI 改好了」，但**从没真正加载过页面**，只做了 HTTP 探测和静态检查。
    那是"没看就说好看"。这个脚本把证据补齐：Playwright 起真实 Chromium，
    把 `runs/<id>/session.json` 里那 30 条**真实事件**逐条喂给页面的 `onEvent()`，
    再截图。走的是正经渲染路径，不是伪造 DOM —— 截图里看到什么，
    用户在浏览器里就能看到什么。

用法：
    python ui_shot.py                     # 默认跑最近一个可回放的归档
    python ui_shot.py runs/xxxx/          # 指定归档
    python ui_shot.py --port 8791         # 指定已启动的控制台端口
输出：ui_shots/ 下的若干 png
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "ui_shots"
# 缓存的 Chromium（不走 playwright 自带的版本注册表，避开版本不匹配）
CHROME = Path(os.path.expanduser(r"~\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe"))

VIEWS = [("chat", "成果"), ("traj", "过程"), ("cap", "它能做什么"), ("gate", "质量把关"),
         ("tools", "科研工具"), ("kb", "我的文献库"), ("deliv", "产出"), ("fig", "图与配图"),
         ("set", "设置")]
# ★ 2026-09-29 W15-A 同步：标签跟着界面改成用户说法（原为 轨迹/能力/门禁/工具/知识库/
#   交付物/图表）。**这只影响日志里给每行起的名字，不参与任何断言**（见 :392 的 f"{label:<4}"），
#   所以同步它是为了"日志与页面各叫各的"这件事不再骗人 —— 判据没变，rc 不变。
#   （W15-A 执行体发现并报出，按红线它没动本文件；主控同步。）
# ★ 2026-09-27 W3 补第 10 项「设置」。理由与上一次补四页**同一条**：
#   设置页落地后本表若仍是 9 项，跑它照样 rc=0、照样拍出 9 张图，
#   而**设置页在正式截图证据链里是空的** —— 下一个人会以为十页都覆盖了。
#   这是本项目点名的 G 型失效（守卫在、不覆盖新页），W3 的执行体主动报了它。
# ★ 2026-09-27 从 5 项补到 9 项（clawsgo 对齐轮新增「能力/门禁/交付物/图表」四页）。
#   这不只是"补上截图"，而是**修一个已经发生的证据缺口**：
#   新四页落地后，`ui_shot.py` 仍只遍历 5 项 ⇒ 跑它会拍出 5 张图、
#   返回 0、结论"看着没事"，而**新四页在正式截图证据链里是空的**。
#   下一个人跑全套回归会以为覆盖了九页。这正是本项目点名的 G 型失效
#   （检查在、不承重）—— 由 W2 执行体主动报出，主控补上。
#   配套：`clawsgo_align_probe.py` 另做**数据对撞**（接口原始 JSON vs 渲染文本），
#   两者分工不同 —— 这里证明"不空"，那里证明"数字对得上"。
# ★ 2026-09-22 补 ("kb", "知识库")：这一页是**新加的**，此前只有 `ui_check.py`
#   （静态体检：只核 id 与函数名在不在）和 HTTP 探测覆盖 ——
#   **它的 JS 渲染路径一条判据都没有**。静态检查能证明"字符串都在"，
#   证明不了"点下去真的出内容"；而"界面与事实各说各话"恰是本项目最忌讳的一类。
#   顺带白捡一条：末尾的 `blanks`（σ<2.0 = 空屏）本身就是失败项 ⇒
#   面板渲染成空壳会被当场抓住，而不是拍出一张"看着没事"的图。
#   （`goView()` 走的是真点导航按钮，所以 `loadKb()` 会照常触发、数据照常拉。）

# 回放装配完成的**结构锚点**。v3 把默认视图重写成「成果卡」后，
# `.gate` / `.draft` / `.trow` 这些 v1/v2 的类名**一个都不存在了** ——
# 而旧脚本正是因为「等不到选择器只打印一行就继续」，
# 能拍出 10 张看似正常的空壳图并返回 0（G 型：检查在、不承重）。
ANCHOR_SEL = "#resultCard .res"


def _ink(path: Path) -> tuple[float, float]:
    """客观测量一张图「有没有内容」：返回 (均值亮度, 标准差)。

    为什么不用肉眼判读缩略图：上一轮就这么栽过 —— 我看缩略图以为拍到了东西，
    实际是深色空屏。**客观测量 > 目测。** 纯色空屏 σ≈0，有内容的页面 σ 显著 > 0。

    PIL 缺失时返回 (-1, -1) 并**跳过该判据**（缺依赖 ≠ 页面坏，不制造假失败）。
    """
    try:
        from PIL import Image
    except ImportError:
        return (-1.0, -1.0)
    with Image.open(path) as im:
        g = im.convert("L").resize((240, 160))
        # Pillow 14 起废弃 getdata()（每次跑都刷一条 DeprecationWarning，纯噪音）；
        # 用新 API，旧版本回退 —— 不因 Pillow 版本差异制造假失败。
        try:
            px = list(g.get_flattened_data())
        except AttributeError:
            px = list(g.getdata())
    n = len(px)
    mean = sum(px) / n
    sd = (sum((v - mean) ** 2 for v in px) / n) ** 0.5
    return (mean, sd)


def _rid_with_draft(prefer: str = "") -> tuple[str, str]:
    """挑一个**真有正文落盘**的归档，返回 (rid, draft)。

    为什么要单独挑：`latest_archive()` 取的是"事件最多"，而**多 ≠ 有正文**。
    实测 14 份归档里 12 份有正文，可事件最多的那两份恰好是 draft=0。
    直接拿 `rid` 用，稿件台那一节就会**每次都走进"只核开合"的分支**，
    内容判据永远不执行 —— 那就成了"没检查"被渲染成"没问题"。
    """
    def _draft_of(d: Path) -> str:
        try:
            j = json.loads((d / "session.json").read_text(encoding="utf-8"))
        except Exception:
            return ""
        return str((j.get("result") or {}).get("draft") or "").strip()

    if prefer:
        d = ROOT / "runs" / prefer
        if d.is_dir():
            t = _draft_of(d)
            if t:
                return prefer, t
    best = ("", "")
    for d in sorted((ROOT / "runs").iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith("_") or not (d / "session.json").exists():
            continue
        t = _draft_of(d)                     # 挑最长的那份：证据厚一点总没坏处
        if t and len(t) > len(best[1]):
            best = (d.name, t)
    return best


def latest_archive() -> Path:
    """挑一个**真有内容可看**的归档。

    两条规矩都是被坑出来的：

    * 跳过 `_` 开头的目录 —— 那是测试夹具（`_resume_t1`、`_diag_t`…）。
      它们混进来会把"最近一个"变成 1 条事件的空壳归档。
    * 取**事件最多**的那个，不是名字最新的那个：名字最新 ≠ 内容最多。
      2026-09-19 实测：原来的 `cands[-1]`（按名字排序）挑中了 `_diag_t`，
      成果卡只剩 301 可读字符，而脚本照样报"成果卡有实内容、零异常"——
      **证据薄得像纸，结论却和厚证据一模一样。**
    """
    cands = [p for p in (ROOT / "runs").iterdir()
             if p.is_dir() and not p.name.startswith("_")
             and (p / "session.json").exists()]
    if not cands:
        raise SystemExit("runs/ 下没有可回放的归档（缺 session.json）")

    def _n_events(d: Path) -> int:
        try:
            return len(json.loads((d / "session.json").read_text(encoding="utf-8"))
                       .get("events") or [])
        except Exception:
            return 0

    return max(cands, key=_n_events)


def _srv_build(port: int) -> tuple[dict | None, list[str]]:
    """取 `/api/build`。返回 (载荷, 出错说明)。"""
    url = f"http://127.0.0.1:{port}/api/build"
    # 环境里有 HTTP_PROXY，连 127.0.0.1 都会被拦 —— 显式绕开。
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with op.open(url, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace")), []
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, ["控制台没有 /api/build ⇒ 它是**带不出代码指纹的旧版本**。"
                          "这本身就是'跑的是旧代码'的直接证据，先重启它。"]
        return None, [f"取代码指纹失败：HTTP {e.code}"]
    except Exception as e:                                  # noqa: BLE001
        return None, [f"连不上控制台 {url}（{type(e).__name__}）—— 先把它启动起来"]


def build_drift(port: int) -> list[str]:
    """问控制台「**你这个进程**加载的是哪份代码」，跟磁盘比。空列表 = 一致。

    ★ 这条判据防的是**最贵的一种假绿**：本脚本连的是**已启动**的控制台（`--port`），
    不自己起。于是"改完代码忘了重启服务"时，它会**安静地跑在旧构建上**，
    而结论却算在新代码头上 —— 旧行为报错还算好的，最坏是旧行为报"全绿"。
    这和"补丁在位 ≠ 症状消失（进程可能启动早于补丁落盘）"是同一个形状：
    **磁盘上是新的，内存里是旧的，而现场看不出来。**

    ★★ 这里刻意**不采信服务端的 `stale` 自述**，而是拿它给出的 `loaded`
       （它 import 时冻住的指纹）跟**我自己算的磁盘指纹**比。理由：
       `stale` 是"被检查者自己的结论"（R 型：判据的输入由被评者提供）；
       而 `loaded` 是它只能在那时才能取到的值 —— 换成我这边独立核对，就多了一条锚点。
       （这个区分不是洁癖：第一版探针"每次请求现读磁盘"，自己就是个假绿发生器。）
    """
    srv, err = _srv_build(port)
    if srv is None:
        return err
    started = time.strftime("%H:%M:%S", time.localtime(srv.get("started") or 0))
    loaded = srv.get("loaded") or {}          # 它加载时看到的（冻住的）
    if not loaded:
        # 给不出 `loaded` ⇒ 这条判据**没法判**。别把它读成"一致"（那就是假绿），
        # 也别硬判失败 —— 如实说"判不了"，并且要求重启（能给出指纹的版本才可判）。
        return ["这个控制台给不出 `loaded` 指纹（版本太旧），无法判定它跑的是不是"
                "当前代码。重启它以启用这条判据。"]
    return _compare(loaded, disk_fingerprint(), started)


def _compare(loaded: dict, mine: dict, started: str = "") -> list[str]:
    """`loaded`（进程 import 时冻住的指纹）vs `mine`（此刻磁盘）⇒ 差异说明。

    抽成纯函数是为了**它能被单独对照**：`build_drift` 依赖一个活着的控制台，
    而这段比对逻辑不该只能在"恰好有个服务在跑"的时候才验得到。
    """
    return [f"{nm}：进程加载的是 {loaded.get(nm) or '（当时还没有）'}，"
            f"磁盘现在是 {mine.get(nm) or '（已被删）'}"
            + (f"（该服务启动于 {started}）" if started else "")
            for nm in sorted(set(mine) | set(loaded))
            if loaded.get(nm) != mine.get(nm)]


def disk_fingerprint() -> dict:
    """磁盘上的代码指纹。

    **刻意复用服务端那份实现**（`rongke.web.server._fingerprint`），而不是在这儿
    再写一套 —— 两套"选哪些文件、怎么算"的实现早晚会漂移成两张脸，
    到时候判据就变成各说各话（H 型）。
    """
    try:
        sys.path.insert(0, str(ROOT))
        import rongke.web.server as _srv            # noqa: PLC0415
        return _srv._fingerprint(_srv.PKG)          # noqa: SLF001
    except Exception:                               # noqa: BLE001
        return {}


def _self_test(port: int) -> int:
    """给「构建戳」这条判据配对照 —— **判"全绿"的工具自己必须有对照**。

    为什么非配不可：`build_drift` 返回 `[]` 表示"一致"，而一个**永远返回 `[]`**
    的实现会让所有检查都通过。所以必须能**人为造出漂移**，看它抓不抓得住。
    """
    bad = 0

    def ck(name: str, got, want) -> None:
        nonlocal bad
        ok = got == want
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            bad += 1

    # ── A. 纯比对逻辑（不需要控制台）──
    ck("比对：完全一致 ⇒ 空", _compare({"a": "1"}, {"a": "1"}), [])
    ck("比对：内容变了 ⇒ 报出来", len(_compare({"a": "1"}, {"a": "2"})), 1)
    ck("比对：进程有、磁盘没了（被删）⇒ 报出来",
       len(_compare({"a": "1"}, {})), 1)
    ck("比对：磁盘有、进程当时没有（新增）⇒ 报出来",
       len(_compare({}, {"a": "1"})), 1)

    # ★ 空指纹会让**所有**比对都"一致" —— 这是这条判据唯一的假绿入口，必须堵。
    mine = disk_fingerprint()
    ck("★ 磁盘指纹非空（空 = 一切都会'通过'，那是假绿）", len(mine) > 0, True)
    ck("★ 指纹里真的盯着 server.py（不盯它就等于没查）",
       "web/server.py" in mine, True)

    # ── B. 活控制台：正控 + 负控 ──
    srv, err = _srv_build(port)
    if srv is None:
        print(f"  ⚠️  {port} 上没有控制台 —— 端到端正/负控**跳过**"
              f"（不算通过：没测就是没测，别读成绿）")
    else:
        ck("负控：活控制台跑的就是当前代码（无漂移）", _compare(
            srv.get("loaded") or {}, mine, "now"), [])
        # ★ 正控：往包里**新放一个源文件** ⇒ 磁盘指纹必然多一项，
        #   而它 import 时不可能见过这个文件 ⇒ 必须报出漂移。
        #   这一条正好戳穿我第一版探针的病（"每次请求现读磁盘" ⇒ 永远一致）。
        touch = ROOT / "rongke" / "_uist_touch.py"
        try:
            touch.write_text("# 只看指纹的临时文件\n", encoding="utf-8")
            hot = _compare(srv.get("loaded") or {}, disk_fingerprint(), "now")
            ck("★ 正控：盘上新出现一个文件 ⇒ 抓得住（证明指纹是 import 时冻住的）",
               len(hot) > 0, True)
            ck("★ 报出来的是那个新文件（不是一句含糊的'不一致'）",
               any("_uist_touch" in x for x in hot), True)
        finally:
            if touch.exists():
                os.replace(touch, ROOT / "runs" / "_regress" / "_uist_touch.py")
        ck("★ 撤掉之后又回到干净（正控没留下副作用）",
           _compare(srv.get("loaded") or {}, disk_fingerprint(), "now"), [])

    # 端口没人 ⇒ 必须**说话**。空列表会被上游读成"一致" = 假绿。
    ck("★ 负控：端口无人应答 ⇒ 给得出原因（空 = 假绿）",
       bool(build_drift(port + 12345)), True)

    print(f"\n[DONE] 代码指纹判据自检 · {bad} 项失败")
    return 1 if bad else 0


def main() -> int:
    args = sys.argv[1:]
    # ★ 全套回归会注入 RONGKE_WEB（指向**当前代码**的临时控制台），优先连它。
    #   写死 8791 时 --self-test 会连到一台早于源码落盘就启动的旧台，把
    #   「跑的是旧代码」记成产品红（final13 实测 2 条指纹漂移）。
    port = 8791
    _web = os.environ.get("RONGKE_WEB", "").rstrip("/")
    _tail = _web.rsplit(":", 1)[-1]
    if _web and _tail.isdigit():
        port = int(_tail)
    if "--port" in args:
        port = int(args[args.index("--port") + 1])
        args = [a for a in args if a != "--port" and a != str(port)]
    if "--self-test" in args:
        return _self_test(port)
    # ★ 先问一句"你连的控制台跑的是哪份代码"。放在**取归档之前** ——
    #   漂移了就没必要再往下走：接着拍只会得到一串"用旧代码验新代码"的图。
    drift = build_drift(port)
    if drift:
        print("\n❌ 你连的控制台跑的**不是当前这份代码** —— 先重启它再跑本脚本：")
        for line in drift:
            print("   · " + line)
        print("\n   （重启示例）python -m rongke --web --port %d" % port)
        return 2
    arc = Path(args[0]) if args else latest_archive()
    if arc.is_dir():
        arc = arc / "session.json"
    sess = json.loads(arc.read_text(encoding="utf-8"))
    events = sess.get("events", [])
    result = sess.get("result")
    print(f"归档   : {arc}")
    print(f"目标   : {sess.get('goal', '')[:60]}")
    print(f"事件   : {len(events)} 条 · 成功={bool(result and result.get('success'))}")
    if len(events) < 5:
        # 薄证据不等于没证据 —— 但要**说出来**。1 条事件的归档能证明页面会渲染，
        # 证明不了"内容长得好不好看"；两者都报同一种绿，就是在骗自己。
        print(f"  ⚠️ 归档很薄（{len(events)} 条事件）—— 只能验渲染，验不了内容观感")

    OUT.mkdir(exist_ok=True)
    rid = sess.get("id") or arc.parent.name
    from playwright.sync_api import sync_playwright

    # ── 未捕获异常收集 ──────────────────────────────────────
    # 为什么必须有：`node --check` 只验**语法**，`ui_check.py` 只核对内联事件
    # 引用的函数。**纯 JS 里调用一个不存在的函数，两个静态检查都放过，运行时才炸。**
    # 而这类错误（拼错名字、定义被删、跨 <script> 块调用）恰恰是改前端时最常见的。
    #
    # 只把 `pageerror`（未捕获异常）判为致命：它是**确定**的 bug，零误报。
    # `console.error` 不判致命 —— 网络 404、SSE 断流都会往里写，
    # 把它们当失败会训练人无视这份报告（"误报比漏报更伤"）。
    js_errors: list[str] = []
    console_errors: list[str] = []

    def _hook(pg, tag: str) -> None:
        pg.on("pageerror", lambda e: js_errors.append(f"[{tag}] {e}"))
        pg.on("console",
              lambda m: console_errors.append(f"[{tag}] {m.text}")
              if getattr(m, "type", "") == "error" else None)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=str(CHROME),
            # 环境里有 HTTP_PROXY（上一轮就因此吃过 502），必须显式绕开，
            # 否则连 127.0.0.1 都会被代理拦掉。
            proxy={"server": "direct://"},
            args=["--force-color-profile=srgb", "--font-render-hinting=none",
                  "--no-proxy-server"],
        )

        # ── 有内容：直接用页面自己的回放入口 `/?run=<id>` ──
        #    不自己灌事件。理由：灌事件要手工模拟"开始对话"那几步（切空态、addUser、
        #    建 round），一旦漏一步就会拍出一个**看似正常其实没加载**的页面 ——
        #    我第一版就是这么翻车的：底栏显示"完成 5 步"，正文却还是欢迎页。
        #    走 `?run=` 则由页面自己的 replay() 负责全部装配，用户点历史也是这条路。
        page = browser.new_page(viewport={"width": 1440, "height": 980},
                               device_scale_factor=2)
        _hook(page, "回放页")
        fatal: list[str] = []
        page.goto(f"http://127.0.0.1:{port}/?run={rid}", wait_until="load")
        try:
            page.wait_for_selector(ANCHOR_SEL, timeout=15000)
        except Exception as e:
            # ★ fail-loud：渲染失败就**没必要继续** —— 接着拍只会得到一堆
            #   看起来没事的空壳图，等于用产出的数量掩盖产出的质量。
            browser.close()
            print(f"\n❌ 回放内容没渲染出来（等 {ANCHOR_SEL} 超时，{type(e).__name__}）")
            print("   截图已放弃：此刻拍到的只会是空壳，返回 0 就是假通过。")
            return 2
        page.wait_for_timeout(600)

        # 锚点存在 ≠ 有内容（`.res` 可以是个空 div）。量一下真实可读文本。
        rc_len = page.evaluate(
            "() => { const r = document.querySelector('#resultCard');"
            " return r ? r.innerText.trim().length : -1; }")
        heads = page.evaluate(
            "() => Array.from(document.querySelectorAll('#resultCard .sec h4'))"
            ".map(h => h.innerText.trim().split('\\n')[0])")
        print(f"  成果卡 : {rc_len} 可读字符 · 区块 {heads}")
        if rc_len < 30:
            fatal.append(f"成果卡渲染了但几乎是空的（可读字符 {rc_len}）")

        shots = []
        blanks = []
        for view, label in VIEWS:
            page.evaluate("v => { goView(v); }", view)
            page.wait_for_timeout(280)
            for theme in ("light", "dark"):
                page.evaluate("t => { setTheme(t, true); }", theme)
                page.wait_for_timeout(260)
                p = OUT / f"{view}_{theme}.png"
                page.screenshot(path=str(p))
                shots.append(p)
                mean, sd = _ink(p)
                if 0 <= sd < 2.0:
                    blanks.append(p.name)
                print(f"  [{theme:5}] {label:<4} -> {p.name}  "
                      f"(亮度 {mean:.1f} σ {sd:.1f})")

        # ── 文献库：真页面 + HTTP 边界候选，量四档视口 ──
        def _kb_find_fixture(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "candidates": [{
                    "title": "Responsive candidate with a long English title for horizontal layout verification",
                    "year": 2026, "venue": "Journal of Interface Verification",
                    "doi": "10.0000/responsive-layout-probe", "can_pick": True,
                    "in_library": False
                }]
            }))

        page.route("**/api/kb/find", _kb_find_fixture)
        page.evaluate("() => goView('kb')")
        page.wait_for_function("() => document.querySelector('#kbOut').dataset.filled === '1'")
        # ★ library.js:268 buildAddPanel() 把 #kbFindQ 整张卡片搬进「新增文献」
        #   tab，而 tab 默认停在「馆藏」⇒ 控件在 hidden 面板里，直接 fill 会抛
        #   「element is not visible」。按真实用户路径先点 tab，再等它真的可见。
        page.evaluate("() => { const b = document.querySelector('[data-kbtab=\"add\"]');"
                      " if (b) b.click(); }")
        page.wait_for_selector("#kbFindQ", state="visible", timeout=15000)
        page.fill("#kbFindQ", "responsive layout")
        page.click("#kbFindGo")
        page.wait_for_selector("#kbFindOut .kbfindscroll tbody tr")
        kb_pdf = page.evaluate("""async () => {
          const d = await (await fetch('/api/kb/list?limit=500')).json();
          return (d.items || []).find(x => x.has_pdf) || null;
        }""")
        for vw in (375, 530, 820, 1440):
            page.set_viewport_size({"width": vw, "height": 900})
            page.evaluate("() => goView('kb')")
            page.wait_for_timeout(260)
            for theme in ("light", "dark"):
                page.evaluate("t => setTheme(t, true)", theme)
                page.wait_for_timeout(180)
                geo = page.evaluate("""() => {
                  const vw = window.innerWidth;
                  const sel = '#v-kb .card, #v-kb .filt, #v-kb .filt input, '
                    + '#v-kb .filt button, #kbOut .kbrow';
                  const bad = Array.from(document.querySelectorAll(sel)).filter(e => {
                    if (!e.offsetParent) return false;
                    const r = e.getBoundingClientRect();
                    return r.left < -1 || r.right > vw + 1;
                  }).map(e => e.id || e.className).slice(0, 5);
                  const sc = document.querySelector('.kbfindscroll');
                  return {vw, docW: document.documentElement.scrollWidth, bad,
                          table: sc ? {client: sc.clientWidth, scroll: sc.scrollWidth} : null};
                }""")
                print(f"  [KB {vw}px {theme}] 页面宽 {geo['docW']} · 越界 {geo['bad']}")
                if geo["docW"] > vw + 1:
                    fatal.append(f"文献库 {vw}px/{theme} 页面横向溢出：{geo['docW']} > {vw}")
                if geo["bad"]:
                    fatal.append(f"文献库 {vw}px/{theme} 控件越界：{geo['bad']}")
                if vw <= 530:
                    scrolled = page.evaluate("""() => {
                      const sc = document.querySelector('.kbfindscroll');
                      const last = sc.querySelector('tbody tr:last-child td:last-child');
                      sc.scrollLeft = sc.scrollWidth;
                      const sr = sc.getBoundingClientRect(), lr = last.getBoundingClientRect();
                      return {needed: sc.scrollWidth > sc.clientWidth + 1,
                              visible: lr.left >= sr.left - 1 && lr.right <= sr.right + 1};
                    }""")
                    if not scrolled["needed"] or not scrolled["visible"]:
                        fatal.append(f"文献库 {vw}px/{theme} 候选表不能独立横滚到最右列：{scrolled}")
                    page.evaluate("() => { document.querySelector('.kbfindscroll').scrollLeft = 0; }")
                p = OUT / f"kb_{vw}_{theme}.png"
                page.screenshot(path=str(p))
                shots.append(p)
            if vw <= 820:
                has_pdf = page.evaluate("""async fallback => {
                  const row = document.querySelector('#kbOut .kbrow.clickable');
                  if (row) { row.click(); return true; }
                  if (fallback) {
                    await openOrig({kb: fallback.id, title: fallback.title});
                    return true;
                  }
                  return false;
                }""", kb_pdf)
                if has_pdf:
                    page.wait_for_timeout(520)
                    mobile_orig = page.evaluate("""() => {
                      const d = document.querySelector('#desk'), r = d.getBoundingClientRect();
                      return {open: d.dataset.open, pos: getComputedStyle(d).position,
                              w: Math.round(r.width), vw: innerWidth,
                              title: document.querySelector('#deskTitle').textContent.trim()};
                    }""")
                    if mobile_orig["open"] != "1" or mobile_orig["pos"] != "fixed" \
                            or mobile_orig["w"] < vw - 4 or not mobile_orig["title"]:
                        fatal.append(f"文献库 {vw}px 原页未走全屏浮层：{mobile_orig}")
                    page.evaluate("() => document.querySelector('#btnDesk').click()")
                else:
                    print(f"  FYI {vw}px：当前馆藏前 20 条没有可打开 PDF，原页入口由专项夹具补测")
        page.unroute("**/api/kb/find", _kb_find_fixture)
        page.set_viewport_size({"width": 1440, "height": 980})
        page.evaluate("""() => {
          goView('kb');
          document.querySelector('#scroll').scrollTop = 600;
          goView('cap');
        }""")
        if page.evaluate("() => document.querySelector('#scroll').scrollTop") != 0:
            fatal.append("长页面切页后继承了旧 scrollTop")
        cursor = page.evaluate("""() => {
          goView('chat');
          const el = document.querySelector('.wbcstat .kbn');
          return el ? getComputedStyle(el).cursor : 'missing';
        }""")
        if cursor in ("pointer", "missing"):
            fatal.append(f"首页统计卡仍有假可点外观：cursor={cursor}")

        # ── 稿件台：右侧阅读抽屉 ──
        # 为什么非要真点一次：静态检查只能证明"字符串都在"。这一件的价值**全在"看得见"**上，
        # 而它的三种坏法在源码里都看不出来 —— 抽屉没宽度、抽屉开着但是空的、大纲是空壳
        # （承诺了导航却一个可点项都没有）。所以这里量的是渲染后的几何量与可读字符。
        _drid, _draft = _rid_with_draft(rid)
        if not _draft:
            print("  （runs/ 下没有任何落盘正文的归档，稿件台只核开合与宽度）")
            page_d = page
        elif _drid == rid:
            page_d = page
        else:
            # 换一页去读那个有稿的归档 —— 用**真实归档**，不用合成夹具。
            print(f"  （借归档 {_drid} 读稿：它这次落了 {len(_draft)} 字正文）")
            page_d = browser.new_page(viewport={"width": 1440, "height": 980},
                                      device_scale_factor=2)
            _hook(page_d, "稿件台账")
            page_d.goto(f"http://127.0.0.1:{port}/?run={_drid}", wait_until="load")
            try:
                page_d.wait_for_selector("#resultCard .res", timeout=15000)
                page_d.wait_for_timeout(520)
            except Exception as e:
                fatal.append(f"稿件台账：归档 {_drid} 没渲染出来（{type(e).__name__}）")
                page_d = page
        page_d.evaluate("() => document.getElementById('btnDesk').click()")
        page_d.wait_for_timeout(460)
        dsk = page_d.evaluate("""() => {
          const d = document.getElementById('desk');
          const inner = document.getElementById('deskInner');
          const toc = document.getElementById('deskToc');
          return { open: d ? d.dataset.open : '?',
                   w: d ? Math.round(d.getBoundingClientRect().width) : -1,
                   chars: inner ? inner.innerText.trim().length : -1,
                   anchors: toc ? toc.querySelectorAll('button[data-go]').length : -1,
                   refs: toc ? toc.querySelectorAll('a.ref').length : -1 };
        }""")
        print(f"  稿件台 : open={dsk['open']} · 宽 {dsk['w']}px · 可读 {dsk['chars']} 字符"
              f" · 导航 {dsk['anchors']} 项 · 出处 {dsk['refs']} 条"
              f"（这次归档正文 {len(_draft)} 字）")
        if dsk["open"] != "1":
            fatal.append("点了「稿件台」但抽屉没打开（data-open 仍是 0）")
        if dsk["w"] < 300:
            fatal.append(f"抽屉打开了却没有宽度（{dsk['w']}px）—— CSS 没生效")
        if _draft:
            if dsk["chars"] < 20:
                fatal.append(f"抽屉里几乎没有正文（可读 {dsk['chars']} 字符，"
                             f"而归档里有 {len(_draft)} 字）—— 抽屉开着但是空的")
            if dsk["anchors"] < 1:
                fatal.append("大纲是空壳 —— 承诺了可导航却一个可点项都没有")
        for theme in ("light", "dark"):
            page_d.evaluate("t => { setTheme(t, true); }", theme)
            page_d.wait_for_timeout(240)
            p = OUT / f"desk_{theme}.png"
            page_d.screenshot(path=str(p))
            shots.append(p)
            mean, sd = _ink(p)
            if 0 <= sd < 2.0:
                blanks.append(p.name)
            print(f"  [{theme:5}] 稿件台 -> {p.name}  (亮度 {mean:.1f} σ {sd:.1f})")

        # ── 稿件台的窄屏断点：两侧各量一次 ──
        # "880px 以下变全屏浮层"这句话，只有断点**上下两侧给出不同答案**才算成立。
        # 只测一侧等于没测边界 —— 断点写成 300px 也照样能过。
        NARROW = 800
        desk_mem0 = None
        for vw, want_pos, label in ((900, "relative", "断点上 · 并排"),
                                    (NARROW, "fixed", "断点下 · 全屏浮层")):
            page_d.set_viewport_size({"width": vw, "height": 900})
            if vw <= NARROW:
                page_d.evaluate("""() => {
                  const b = document.querySelector('#btnDesk');
                  if (document.querySelector('#desk').dataset.open === '1') b.click();
                  b.click();
                }""")
            page_d.wait_for_timeout(340)
            mr = page_d.evaluate("""() => {
              const d = document.getElementById('desk');
              const inner = document.getElementById('deskInner');
              const toc = document.querySelector('.dtoc');
              const r = d.getBoundingClientRect();
              return { pos: getComputedStyle(d).position,
                       w: Math.round(r.width), left: Math.round(r.left),
                       vw: window.innerWidth,
                       toc: toc ? getComputedStyle(toc).display : '缺',
                       chars: inner ? inner.innerText.trim().length : -1 };
            }""")
            if vw > NARROW:
                desk_mem0 = page_d.evaluate("() => DESK_W")
            print(f"  窄屏 {vw}px（{label}）: position={mr['pos']} · 宽 {mr['w']}px / 视口 "
                  f"{mr['vw']}px · 目录 {mr['toc']} · 正文 {mr['chars']} 字")
            if mr["pos"] != want_pos:
                fatal.append(f"稿件台在 {vw}px 下 position 应为 {want_pos}，实际 {mr['pos']}"
                             f" —— 媒体查询没接上，或断点数值写错了")
            if vw <= NARROW:
                if mr["w"] < mr["vw"] - 4:
                    fatal.append(f"窄屏 {vw}px 下抽屉没铺满（{mr['w']}px / 视口 {mr['vw']}px）")
                if mr["toc"] != "none":
                    fatal.append(f"窄屏 {vw}px 下目录栏该让位，实际 display={mr['toc']}")
                if mr["chars"] < 20:
                    fatal.append(f"窄屏 {vw}px 下抽屉里没有正文（{mr['chars']} 字）")
                if page_d.evaluate("() => DESK_W") != desk_mem0:
                    fatal.append("窄屏重开稿件台改写了桌面记忆宽度")
        # 拍之前切回明色：上一轮的循环停在 dark，不切的话这张窄屏图会是暗的
        # （测量不受主题影响，但截图要能跟别的图并排看）。
        page_d.evaluate("t => { setTheme(t, true); }", "light")
        page_d.wait_for_timeout(240)
        p = OUT / "desk_narrow.png"
        page_d.screenshot(path=str(p))
        shots.append(p)
        mean, sd = _ink(p)
        if 0 <= sd < 2.0:
            blanks.append(p.name)
        print(f"  [窄屏] 稿件台 -> {p.name}  (亮度 {mean:.1f} σ {sd:.1f})")
        page_d.set_viewport_size({"width": 1440, "height": 980})

        # ── 空态：新用户打开时看到什么 ──
        page2 = browser.new_page(viewport={"width": 1440, "height": 980},
                                 device_scale_factor=2)
        _hook(page2, "空态页")
        page2.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page2.wait_for_timeout(700)

        # 评审第 5 条「冷启动即劝退」的唯一真实验证：模板与能力边界**在渲染后的 DOM 里**，
        # 不是在 HTML 源码里。静态检查只能证明字符串存在；这里量的是用户真看到的东西，
        # 而且量「点下去填得进去吗」（data-goal 非空），不是「有没有这个标签」。
        n_tpl, tpl_ok, has_bounds = page2.evaluate(
            "() => { const cs = Array.from(document.querySelectorAll('#tplBody .chip'));"
            " return [cs.length,"
            "   cs.filter(c => (c.dataset.goal || '').trim().length > 8).length,"
            "   !!document.querySelector('.bounds')]; }")
        n_visible = page2.evaluate(
            "() => Array.from(document.querySelectorAll('#tplBody .chip'))"
            ".filter(c => c.offsetParent !== null).length")
        print(f"  空态   : 场景模板 {n_tpl} 个（可用 {tpl_ok} · 默认露出 {n_visible}）· "
              f"能力边界 {'有' if has_bounds else '缺'}")
        if n_tpl < 5 or tpl_ok < n_tpl:
            fatal.append(f"空态场景模板不足/不可用：{n_tpl} 个，只有 {tpl_ok} 个能真填进输入框")
        if n_visible != 3:
            fatal.append(f"空态默认应只露出 3 个常用场景，实际 {n_visible} 个")
        if not has_bounds:
            fatal.append("空态缺「能做/不能做」能力边界（评审第 5 条）")

        # ── 新手主链：用请求计数证明「确认前不执行」，不靠读源码猜 ──
        contract_calls: list[int] = []
        run_calls: list[dict] = []

        def _contract_ok(route):
            contract_calls.append(1)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "contract": {
                    "raw_goal": "帮我总结这篇论文", "goal": "总结论文的重点、结论与局限",
                    "deliverables": ["一份中文总结"], "acceptance": ["覆盖重点、结论与局限"],
                    "target_chars": None, "forbidden": []
                }
            }))

        def _run_ok(route):
            run_calls.append(json.loads(route.request.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"id": "ui-shot-contract-run"}))

        page2.route("**/api/contract", _contract_ok)
        page2.route("**/api/run", _run_ok)
        page2.route("**/api/stream*", lambda route: route.abort())
        page2.click("#btnSend")
        page2.wait_for_timeout(180)
        empty_hint = page2.locator("#hint").inner_text()
        empty_invalid = page2.locator("#goal").get_attribute("aria-invalid")
        if contract_calls or run_calls:
            fatal.append("空输入竟然发起了合同或运行请求")
        if "帮我总结这篇论文" not in empty_hint or empty_invalid != "true":
            fatal.append("空输入没有给示例提示或没有标记输入框")

        page2.fill("#goal", "帮我总结这篇论文")
        page2.click("#btnSend")
        page2.wait_for_selector("#cGo", timeout=5000)
        if len(contract_calls) != 1 or run_calls:
            fatal.append(f"整理任务单时请求时序错误：contract={len(contract_calls)} run={len(run_calls)}")
        preview_text = page2.locator("#cPreview").inner_text()
        if "尚未执行" not in preview_text or "确认，开始执行" not in preview_text:
            fatal.append("任务单没有明确尚未执行/确认后执行")
        contract_shot = OUT / "contract_light.png"
        page2.screenshot(path=str(contract_shot))
        shots.append(contract_shot)
        page2.click("#cGo")
        page2.wait_for_timeout(350)
        if len(run_calls) != 1:
            fatal.append("确认任务单后没有且只发起一次运行请求")
        elif run_calls[0].get("contract", {}).get("raw_goal") != "帮我总结这篇论文":
            fatal.append("运行请求没有携带确认后的合同")
        page2.unroute("**/api/contract")
        page2.unroute("**/api/run")
        page2.unroute("**/api/stream*")

        # 预审失败：原话必须留住，且不许偷偷降级成直接运行。
        page2.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page2.wait_for_timeout(500)
        failed_contracts: list[int] = []
        def _contract_fail(route):
            failed_contracts.append(1)
            route.fulfill(status=500, content_type="application/json",
                          body=json.dumps({"error": "模拟预审失败"}))
        page2.route("**/api/contract", _contract_fail)
        page2.fill("#goal", "这段原话不能因为预审失败而丢失")
        page2.click("#btnSend")
        page2.wait_for_timeout(300)
        if page2.locator("#goal").input_value() != "这段原话不能因为预审失败而丢失":
            fatal.append("预审失败后原话被清空")
        if "任务单没整理出来" not in page2.locator("#hint").inner_text():
            fatal.append("预审失败没有明确告诉用户发生了什么")
        if len(failed_contracts) != 1:
            fatal.append("预审失败重试链的请求次数不对")
        page2.unroute("**/api/contract")
        page2.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page2.wait_for_timeout(500)

        for theme in ("light", "dark"):
            page2.evaluate("t => { setTheme(t, true); }", theme)
            page2.wait_for_timeout(260)
            p = OUT / f"empty_{theme}.png"
            page2.screenshot(path=str(p))
            shots.append(p)
            print(f"  [{theme:5}] 空态   -> {p.name}")
            # 主题按钮的文字，在 2880px 截图的缩略图上会被糊成一个字 ——
            # 2026-09-19 我因此裁了两次图去找一个**不存在**的 bug。
            # 量 textContent 是一行的事，且从此不必再看图纠结（客观测量 > 目测）。
            lbl = page2.evaluate(
                "() => { const l = document.querySelector('#themeLabel');"
                " return l ? l.textContent : null; }")
            want_lbl = "深色" if theme == "dark" else "浅色"
            if lbl != want_lbl:
                fatal.append(f"主题按钮文字不对：{theme} 下显示 {lbl!r}，应为 {want_lbl!r}")

        # 新手首屏自己也必须过响应式，不能只拿文献库页面代替全站检查。
        page2.evaluate("t => { setTheme(t, true); }", "light")
        for width, height in ((1024, 768), (768, 1024), (390, 844), (320, 568)):
            page2.set_viewport_size({"width": width, "height": height})
            page2.wait_for_timeout(220)
            geo = page2.evaluate(
                "() => ({vw: innerWidth, sw: document.documentElement.scrollWidth,"
                " send: document.querySelector('#btnSend').getBoundingClientRect().toJSON(),"
                " visible: Array.from(document.querySelectorAll('#tplBody .chip'))"
                ".filter(c => c.offsetParent !== null).length})")
            if geo["sw"] > geo["vw"] + 1:
                fatal.append(f"空态 {width}x{height} 横向溢出：{geo['sw']} > {geo['vw']}")
            if geo["send"]["left"] < 0 or geo["send"]["right"] > geo["vw"] + 1:
                fatal.append(f"空态 {width}x{height} 主按钮越出视口")
            if geo["visible"] != 3:
                fatal.append(f"空态 {width}x{height} 默认场景数变成 {geo['visible']}")
            p = OUT / f"empty_{width}x{height}.png"
            page2.screenshot(path=str(p))
            shots.append(p)
            print(f"  [空态 {width}x{height}] 页面宽 {geo['sw']} · 主按钮在视口内 · 场景 3 个")

        latest = page2.evaluate(
            """async () => { await openLatestProcess();
              await new Promise(r => setTimeout(r, 800));
              const expected = SESS.find(canReplay);
              return { expected: expected && expected.id, actual: CUR && CUR.id,
                       traj: document.querySelector('#v-traj').classList.contains('on') }; }""")
        if not latest["expected"] or latest["actual"] != latest["expected"] or not latest["traj"]:
            fatal.append(f"最近执行记录打开错误：{latest}")

        # ── 介入卡：内核"停下来问人"时长什么样 ──
        # 不自己灌事件流（那要手工模拟装配，第一版正是这么翻的车）；
        # 走 `?run=` 让 replay() 负责全部装配，再**调用页面自己的 renderAsk()**
        # 补上"此刻有个提问"这一态 —— 走的是正经渲染路径。
        page3 = browser.new_page(viewport={"width": 1440, "height": 980},
                                 device_scale_factor=2)
        _hook(page3, "介入卡")
        page3.goto(f"http://127.0.0.1:{port}/?run={rid}", wait_until="load")
        try:
            page3.wait_for_selector(ANCHOR_SEL, timeout=15000)
        except Exception:
            fatal.append("介入卡场景：回放页没渲染出来，卡没地方挂")
        page3.evaluate("""() => renderAsk({
          kind: "gate_reject", step: 7, rejects: 2, timeout: 300,
          summary: "冬虫夏草低温诱导摘要",
          reasons: ["引注数 0 < 3：稿里的数字/DOI 追不到任何一次真实调用",
                    "未装载阶段专用技能 nature-writing（技能门 1:1 独占）"]
        })""")
        page3.wait_for_timeout(400)
        n_btn = page3.evaluate(
            "() => document.querySelectorAll('#askCard .acts button').length")
        # ★ 2026-10-07 修过期判据：这里原来 grep『证据门』——那是 W14-D「文案去工程化」
        #   之前的行话，现役界面上一个都没有（改动前快照里也是 0 次）。
        #   「为什么停」现在由两样东西承担：ASKWHY[kind].why 的 `.why` 说明段，
        #   加 gate_reject 专属的 `.reasons` 打回理由列表。判据改成量这两样 ——
        #   它们才是"解释了为什么"的实体，行话换个词不会再把守卫带红。
        has_why = page3.evaluate(
            """() => { const a = document.querySelector('#askCard .ask');
                 if (!a) return false;
                 const why = a.querySelector('.ab .why');
                 const reasons = a.querySelectorAll('.reasons li');
                 return !!why && why.innerText.trim().length > 4 && reasons.length > 0; }""")
        for theme in ("light", "dark"):
            page3.evaluate("t => { setTheme(t, true); }", theme)
            page3.wait_for_timeout(260)
            p = OUT / f"ask_{theme}.png"
            page3.screenshot(path=str(p))
            shots.append(p)
            mean, sd = _ink(p)
            if 0 <= sd < 2.0:
                blanks.append(p.name)
            print(f"  [{theme:5}] 介入卡 -> {p.name}  (亮度 {mean:.1f} σ {sd:.1f})")
        # 展开"补一句"的输入框，再拍一张（这条支路是评审点名要的那条）
        page3.evaluate(
            "() => { const b = document.querySelector('#askBox'); if (b) b.dataset.open = '1'; }")
        page3.wait_for_timeout(280)
        p = OUT / "ask_input_dark.png"
        page3.screenshot(path=str(p))
        shots.append(p)
        print(f"  [dark ] 介入卡+输入 -> {p.name}")

        # 判据要承重：卡必须真的有那三条出路，且解释了"为什么停"。
        if n_btn < 3:
            fatal.append(f"介入卡出路不足：只有 {n_btn} 个按钮"
                         "（至少要「补一句 / 继续 / 停止」三条）")
        if not has_why:
            fatal.append("介入卡没说明「为什么停」——只报一句「出问题了」等于让人瞎猜")

        # 同一时刻只该有一张卡：replay 页的成果卡与介入卡并存时，
        # 视线必须落在**要你动手**的那张上（它就是默认层的主角）。
        ask_top = page3.evaluate(
            "() => { const a = document.querySelector('#askCard');"
            " const r = document.querySelector('#resultCard');"
            " return (a && r) ? (a.getBoundingClientRect().top"
            "                    < r.getBoundingClientRect().top) : null; }")
        if ask_top is not True:
            fatal.append("介入卡不在成果卡**上方**（停下来等你的事该排在最前面）")

        # ── 续跑卡：没跑完的任务，界面有没有给出"接着跑"这条路 ──
        # ★ 这一条**必须**自己造夹具，随便挑真归档是拍不到的：
        #   跑成功的内核会删掉快照（`resumable=False`），跑到一半的未必还在盘上。
        #   而这条支路正是评审第①条的正果 —— 不验到，就等于
        #   "我写了前端分支，但从没见过它渲染"。
        #
        #   夹具用**真归档的 session.json 改两处**（照着现成的改，schema 天然合法），
        #   再补一份 `conversation.json` 当快照 —— 服务端就是靠它在不在定 `resumable`。
        fix = ROOT / "runs" / "_ui_resume"
        fix.mkdir(parents=True, exist_ok=True)
        try:
            fx = json.loads(arc.read_text(encoding="utf-8"))
            fx["id"] = "_ui_resume"
            fx["goal"] = str(fx.get("goal") or "续跑卡场景")
            fres = fx.setdefault("result", {})
            fres["success"] = False
            fres["reason"] = "步数耗尽（到 40 步上限）—— 是未完成，不是失败；可以接着跑"
            (fix / "session.json").write_text(json.dumps(fx, ensure_ascii=False),
                                              encoding="utf-8")
            snap = {"goal": fx["goal"], "step": 12, "contract": None,
                    "messages": [{"role": "system", "content": "（夹具）系统提示"},
                                 {"role": "user", "content": "（夹具）任务契约"},
                                 {"role": "assistant", "content": "（夹具）已跑到第 12 步"}]}
            sp = fix / "conversation.json"
            sp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")

            page4 = browser.new_page(viewport={"width": 1440, "height": 980},
                                     device_scale_factor=2)
            _hook(page4, "续跑卡")
            page4.goto(f"http://127.0.0.1:{port}/?run=_ui_resume", wait_until="load")
            try:
                page4.wait_for_selector(ANCHOR_SEL, timeout=15000)
                page4.wait_for_timeout(450)
                has_res = page4.evaluate(
                    """() => !!document.querySelector('#resultCard button[data-act="resume"]')""")
                # 成对判据：光有"接着跑"不够，还得说清**凭什么能接着跑**
                why = page4.evaluate(
                    """() => { const r = document.querySelector('#resultCard');
                     const t = r ? r.innerText : '';
                     return t.indexOf('从断点接着跑') >= 0
                         && t.indexOf('不会把前面的步骤再烧一遍') >= 0; }""")
                for theme in ("light", "dark"):
                    page4.evaluate("t => { setTheme(t, true); }", theme)
                    page4.wait_for_timeout(240)
                    p = OUT / f"resume_{theme}.png"
                    page4.screenshot(path=str(p))
                    shots.append(p)
                    mean, sd = _ink(p)
                    if 0 <= sd < 2.0:
                        blanks.append(p.name)
                    print(f"  [{theme:5}] 续跑卡 -> {p.name}  "
                          f"(亮度 {mean:.1f} σ {sd:.1f})")

                # 负控：抽掉快照 ⇒ 按钮必须消失。
                # 「有快照才给按钮」若没真接上，按钮会常驻 —— 而**点不通的按钮
                # 比没有按钮更伤信任**（index.html 里那句注释就是为此写的）。
                # 负控：把快照**挪走**而不是删掉 —— 服务端只看"在不在"。
                # 挪（`os.replace`）不触发环境的删除拦截，所以这一步不会连累整轮。
                os.replace(sp, ROOT / "runs" / "_regress" / "_ui_resume_conv.json")
                page4.goto(f"http://127.0.0.1:{port}/?run=_ui_resume", wait_until="load")
                page4.wait_for_selector(ANCHOR_SEL, timeout=15000)
                page4.wait_for_timeout(450)
                gone = page4.evaluate(
                    """() => !document.querySelector('#resultCard button[data-act="resume"]')""")
                print(f"  负控   : 抽掉快照后按钮{'消失 ✔' if gone else '**仍在 ✘**'}")
                if not gone:
                    fatal.append("抽掉对话快照后「接着上次跑」按钮仍在 —— "
                                 "它是个点不通的按钮，比没有更伤信任")

                print(f"  续跑卡 : 按钮 {'有' if has_res else '缺'} · "
                      f"理由说明 {'有' if why else '缺'}")
                if not has_res:
                    fatal.append("未跑完的任务没有「接着上次跑」按钮（评审第①条的正果）")
                if not why:
                    fatal.append("「接着跑」没说明为什么能接着跑（少了那句「不会把前面的"
                                 "步骤再烧一遍」，用户凭什么信它）")
            except Exception as e:
                fatal.append(f"续跑卡场景崩了：{type(e).__name__}: {e}")
        except Exception as e:
            # 外层出口：**夹具本身没搭起来**（比如归档坏了、写不进去）。
            # 这一步以前没有出口 —— 唯一的出口是那句 `finally: rmtree`。
            # 把 rmtree 拿掉时就露了馅：Python 直接 SyntaxError（"expected 'except'
            # or 'finally' block"），而我**没先跑 py_compile 就上了全套回归**，
            # 于是整套 18 项里有两项死在这行语法上。教训见 regress.py 的语法闸。
            # 现在给它一个真出口：**一张图拍不到，好过 17 张全没有**。
            fatal.append(f"续跑卡夹具没搭起来：{type(e).__name__}: {e}")
        # ★ 夹具**不删**。`shutil.rmtree(fix)` 这一句原来既是一次真实删除
        #   （会撞上环境的批量删除拦截、把整轮拖下水），又毫无必要：
        #   下一轮开头会原样重写 `session.json` / `conversation.json`。
        #   留着一个 `_ui_resume` 目录是干净的 —— `history()` 与 `latest_archive()`
        #   都按约定跳过 `_` 开头的目录，它不会冒充成一条用户任务。

        # ── 材料区：把稿件递进来那条路有多大？ ──
        # 评审第④条「输入输出离科研工作流太远」的入口。这里必须走**页面自己的**
        # `uploadFiles`（`set_input_files` ⇒ `change` ⇒ `fetch(body: File)`）——
        # 这才顺带回答了一个纸面推不出来的问题：**浏览器到底带不带 Content-Length**？
        # （服务端按裸字节体读，不带就整个收不到。实测 Chromium 会带。）
        #
        # ⚠️ 材料区是**真实的** `runs/_inbox`（服务端只有这一个），所以：
        #   ① 只删**自己刚传进来**的那几份 —— 用户可能真在里面放着材料，
        #      一句 `clearMaterials()` 就把人家的稿子清了，那是数据事故；
        #   ② 节前节后都记下清单，末了比对，没回到原样就报。
        page5 = browser.new_page(viewport={"width": 1440, "height": 980},
                                 device_scale_factor=2)
        _hook(page5, "材料区")
        page5.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page5.wait_for_timeout(500)

        def _mat_names(pg) -> list:
            return pg.evaluate(
                "() => Array.from(document.querySelectorAll('#mats .mat b'))"
                ".map(b => b.textContent)")

        _before = _mat_names(page5)
        fixture = ROOT / "runs" / "_regress" / "ui_shot_material.md"
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_text("题名：低温诱导对冬虫夏草子实体分化的影响\n样本量：120\n",
                           encoding="utf-8")
        page5.set_input_files("#matFile", str(fixture))
        try:
            page5.wait_for_selector("#mats .mat", timeout=10000)
        except Exception:
            fatal.append("材料区：传完之后清单里没出现材料")
        page5.wait_for_timeout(400)
        _after = _mat_names(page5)
        # ★★ 判据**只能看这一趟新进来的那一份**（`_added`），不许用
        # "清单里有没有这个名字" —— 那样会被**前置状态**满足。
        #
        # 踩过（2026-09-21，真事）：`runs/_inbox` 里留着上一轮**被 SIGTERM 掐掉**的
        # 那份同名材料（收尾逻辑只在正常路径上跑，被 kill 就轮不到它），
        # 服务端按「同名绝不覆盖」把本次上传改名成 `ui_shot_material-2.md`，于是：
        #   · 旧判据 `named`（清单里**存在** `ui_shot_material.md`）被**旧那份**满足
        #     ⇒ **假绿**（上传这条路其实没验到）；
        #   · `noted`（目标框里要有 `ui_shot_material.md`）落空 ⇒ 真红，
        #     但**红得指错方向** —— 看着像"没写进目标框"，实际是"两份同名"。
        # ⇒ 一个能被前置状态满足的判据不是判据。判据必须是「清单**增长了**」。
        _added = [n for n in _after if n not in _before]
        if _before:
            print(f"  前置   : 材料区节前已有 {len(_before)} 份 —— 判据只看**本趟新增**，"
                  f"不受它影响（⚠️ 也可能是上一轮被中断留下的，值得顺手看一眼）")
        _grew = len(_added) > 0
        has_mats = page5.evaluate("() => !$('#mats').hidden")
        named = _grew and any(n.startswith("ui_shot_material") for n in _added)
        # ★ **所见即所发**：材料进来时，那行"材料在工作区里"必须写进**目标框**。
        #   悄悄往提示词里塞上下文的做法，一旦出错用户连"为什么它没看我的稿子"都查不出来。
        #   逐份核**新增的那几个名字**（服务端可能洗过名，以它返回的为准）。
        _goal_val = page5.evaluate("() => $('#goal').value")
        noted = _grew and all(n in _goal_val for n in _added)

        # 负控：不收的扩展名必须**如实报错**，且一个字都不许进清单
        _badf = ROOT / "runs" / "_regress" / "ui_shot_reject.exe"
        _badf.write_bytes(b"MZ")
        page5.set_input_files("#matFile", str(_badf))
        page5.wait_for_timeout(900)
        _after2 = _mat_names(page5)
        hint_txt = page5.evaluate("() => $('#hint').innerText")
        rejected = ("没收下" in hint_txt) and (len(_after2) == len(_after))

        for theme in ("light", "dark"):
            page5.evaluate("t => { setTheme(t, true); }", theme)
            page5.wait_for_timeout(240)
            p = OUT / f"materials_{theme}.png"
            page5.screenshot(path=str(p))
            shots.append(p)
            mean, sd = _ink(p)
            if 0 <= sd < 2.0:
                blanks.append(p.name)
            print(f"  [{theme:5}] 材料区 -> {p.name}  (亮度 {mean:.1f} σ {sd:.1f})")

        print(f"  材料区 : 清单 {'有' if has_mats else '缺'} · "
              f"本趟新增 {len(_added)} 份 · 名字 {'对' if named else '错'} · "
              f"目标框写明 {'是' if noted else '否'} · "
              f"负控{'（不收的扩展名被如实挡下）' if rejected else '失守 ✘'}")
        if not _grew:
            fatal.append("材料区：传完之后清单**没有增长** —— 上传这条路没通"
                         "（用户会以为没传上去）。⚠️ 排查第一步：`runs/_inbox` 是不是脏的。")
        elif not named:
            fatal.append(f"材料区：本趟新增的是 {_added}，里面没有预期的那一份")
        if not noted:
            fatal.append(f"材料进来却没写进目标框（本趟新增 {_added}）"
                         " —— 用户不知道它会把哪些文件带进这次任务")
        if not rejected:
            fatal.append("不收的文件没有被如实挡下（或混进了清单）")

        # 只撤自己带来的那几份 —— 别人放在 `_inbox` 里的东西一个字都不许动。
        #
        # ★ 收尾**一次删除都不做**，这不是洁癖，是把假信号从根上掐掉：
        #   环境里有一道"批量删除确认"守卫（一轮内累计删够阈值就拦），
        #   它拦下来是抛异常 —— 而抛异常在服务端曾被 `except: pass` 吞掉，
        #   于是"删了"和"还在"各说各话。移除走的是 `os.replace`（**改名**，不是删除），
        #   原件落到 `runs/_removed/`；这里再把它**搬**进本轮 scratch 目录。
        #   全程零删除 ⇒ 收尾天然不受那道守卫影响，也不会连累同一轮里别的东西。
        _kept, _unmoved = [], []
        for n in _added:
            d = page5.evaluate("async (nm) => await dropMaterial(nm)", n)
            if isinstance(d, dict) and d.get("kept_at"):
                (_kept if d.get("moved") else _unmoved).append(Path(d["kept_at"]))
        page5.wait_for_timeout(600)
        _left = _mat_names(page5)
        _stashed = 0
        for src in _kept:
            try:
                if src.is_file():
                    os.replace(src, ROOT / "runs" / "_regress" / src.name)
                    _stashed += 1
            except OSError:
                pass
        print(f"  收尾   : 自己带来的 {len(_added)} 份已撤"
              f"（清单里还剩 {len(_left)} 份 · 节前原有的 {len(_before)} 份未动）"
              + (f" · 原件挪进 _regress {_stashed} 份" if _stashed else " · 零删除"))
        if sorted(_left) != sorted(_before):
            fatal.append(f"材料区收尾没回到原样：节前 {_before} → 节后 {_left}")
        if _unmoved:
            # 挪不动的原件：不是测试失败，但**必须如实说** —— 沉默才是问题。
            print("  ⚠️  有 %d 份原件没挪动（还在原位）：%s"
                  % (len(_unmoved), "、".join(str(p) for p in _unmoved)))
        # 这两份 scratch 文件**不删**：下一轮 `write_text` 直接覆盖同名。
        # （留在 `_regress/` 里，是运行器自己的地盘，不是用户的材料区。）

        browser.close()

    print(f"\n[DONE] 共 {len(shots)} 张，落在 {OUT}")
    if console_errors:
        print(f"\n控制台 error 级日志 {len(console_errors)} 条（**不判失败**，仅供排查）：")
        for line in console_errors[:8]:
            print(f"  · {line[:160]}")
    bad = 0
    if js_errors:
        bad += 1
        print(f"\n❌ 页面抛了 {len(js_errors)} 个未捕获异常 —— 这是真 bug，必须修：")
        for line in js_errors[:10]:
            print(f"  · {line[:400]}")
    if fatal:
        bad += 1
        print(f"\n❌ 内容判据 {len(fatal)} 条不过（**拍到了图不等于拍到了东西**）：")
        for line in fatal:
            print(f"  · {line}")
    if blanks:
        bad += 1
        print(f"\n❌ {len(blanks)} 张图几乎纯色（σ<2.0），是空屏不是界面：")
        for line in blanks[:8]:
            print(f"  · {line}")
    if bad:
        return 1
    print("\n✔ 页面零未捕获异常 · 成果卡有实内容 · 空态模板与能力边界齐全 · 无空屏")
    print("   （JS 引用完整性 + 「冷启动 30 秒能开工」都由真实浏览器验证过了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
