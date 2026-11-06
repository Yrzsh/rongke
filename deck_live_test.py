# -*- coding: utf-8 -*-
"""deck_live_test.py —— 「组会 PPT」的**真跑**测试：真起 MCP 服务端、真建 .pptx、真过门。

跑法：
    python deck_live_test.py            # 全部
    python deck_live_test.py --json

## 与 `deck_test.py` 的分工（两者不许互相替代）

| 文件 | 管什么 | 环境 |
|---|---|---|
| `deck_test.py` | **判据本身对不对** | 纯标准库 · 合成夹具 · 秒级 · 3.12 与 3.13 都跑 |
| 本文件 | **接线通不通** | 真 spawn vendored 服务端 · 需 Python312 带 `mcp` · 十几秒 |

为什么必须分开：混在一个文件里，要么让"判据测试"变得挑环境（于是有人为了让它
在 3.13 通过而删断言），要么让"接线测试"被人嫌慢而跳过。
**判据与接线是两件事，各自要能单独承重。**

## 这里补的是 `deck_test.py` **测不到**的那一层

`deck_test.py` 的门测试用的是**桩注册表**（喂一段 JSON 进去）。它证明"门会拦/会放行"，
证明不了**这条链真的接通了**：

    `config.json` 的 `mcp.servers.ppt`
      → `parse_servers` 真解析出 `MCPServerSpec`（command/domain/cwd/path_args）
      → `MCPHub` 真 spawn 那个解释器 + 真跑 `initialize` / `tools/list` 握手
      → `install_mcp` 把工具**真的**注册进注册表（`domain="ppt"`、`source="mcp"`）
      → 走注册表调用 ⇒ **路径改写真的生效**、产物真的落在工作区
      → 产物送进 `DeckAuditor` ⇒ 四道门在**真 pptx** 上跑（不是合成夹具）

## 失败要红得响，不许"跳过"

本文件**没有** `--skip-if-missing` 之类的退路。服务端起不来就整片红 ——
因为"接线断了"正是本文件要抓的东西；给它留一条"跳过"的后门，
等于把唯一的接线守卫变成可关掉的装饰。报错里会带上服务端 stderr 尾巴。
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                                      # noqa: E402

from rongke.config import Config                                       # noqa: E402
from rongke.contract import TaskContract                               # noqa: E402
from rongke.deck import DeckAuditor, probe_pptx, style_deck        # noqa: E402
from rongke.loop import _make_audit_deck, mounting_decision            # noqa: E402
from rongke.mcp_client import MCPHub, MCPServerSpec, install_mcp       # noqa: E402
from rongke.tools import Tool, ToolRegistry                            # noqa: E402

PASS, FAIL = [], []
_QUIET = False

_WORK = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_deck_live", "deck-live")

# 实测口径，写死在这儿当**回归绊线**（改了要有人回头看文档，不许悄悄漂）。
# 见 `vendor/ppt-mcp/PROVENANCE.md`「上游有三处口径互相打架」。
EXPECTED_TOOL_COUNT = 37
UPSTREAM_CLAIMED = 32          # README / `get_server_info()` 的自称 —— 用它证明"声明≠事实"
DEFAULT_SLIDE_EMU = (9144000, 6858000)      # 上游新建默认 4:3（10 × 7.5 in）


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    if not _QUIET:
        print(f"  {'✔' if ok else '✘'} {name}" + (f"  — {detail}" if detail and not ok else ""))


def section(t: str) -> None:
    if not _QUIET:
        print(f"\n── {t} " + "─" * max(0, 58 - len(t)))


@contextmanager
def chdir(p: Path):
    """临时把进程 cwd 换到 `p`（结束**无条件**换回来）。

    为什么这条测试需要它：`audit_deck` 的执行体用 `wd = Path.cwd()` 当围栏
    （`loop.py::_make_audit_deck`），而 `loop.run()` 在生产里会
    `os.chdir(ws)`（`loop.py:829`）。测试站在仓库根直接调 ⇒ 报告落在仓库根。
    ⇒ **cwd 是被测语义的一部分**，测试必须自己把它摆对，否则测的不是那条链。
    """
    prev = Path.cwd()
    os.chdir(p)
    try:
        yield
    finally:
        os.chdir(prev)


# ══════════════════════════════════════════════════════════════════
# 夹具注册表：一个 minim 的内核视角
# ══════════════════════════════════════════════════════════════════
def build_registry(cfg, hub: MCPHub) -> tuple[ToolRegistry, dict]:
    """注册表 = 常驻域的一小撮桩 + 真正的 `audit_deck` + 真的 MCP ppt 工具。

    为什么常驻域要放几个**桩**：裁剪是要**量差**的。全是 ppt 工具的话，
    "剪掉 ppt 之后剩下什么"无从对比 ⇒ 那条判据会变成恒真。
    """
    reg = ToolRegistry()
    for name, dom in (("read_file", "core"), ("write_file", "core"),
                      ("deliver_task", "core"), ("search_web", "research"),
                      ("render_figure", "figure")):
        reg.register(Tool(name, "桩", {"type": "object", "properties": {}},
                          fn=lambda **k: "", domain=dom))
    # 内核自己那一个（第 9 条门点名要用它）—— 用**真的**执行体，
    # 不是桩：桩注册上去只能证明"名字在"，证明不了"工具真的能用"。
    reg.register(Tool("audit_deck", "组会 PPT 体检", {"type": "object", "properties": {}},
                      fn=_make_audit_deck(cfg.deck), domain="ppt", mutating=True))
    info = install_mcp(reg, hub)
    return reg, info


def _mount(reg: ToolRegistry, contract) -> dict:
    dom = {n: (reg.get(n).domain if reg.get(n) else "") for n in reg.names()}
    return mounting_decision(contract, list(dom), lambda n: dom.get(n, ""))


# ══════════════════════════════════════════════════════════════════
# 1 · 装配：config → 真 spawn → tools/list → 注册表
# ══════════════════════════════════════════════════════════════════
def _fixture_cfg() -> tuple[Config, bool]:
    """造一份**夹具配置**，不去读用户那份 `config.json`。返回 (cfg, 生产是否已摘)。

    ★ 2026-09-26 为什么非改不可：本测试验的是 **MCP 客户端接线**
      （`parse_servers` → 真 spawn → `tools/list` → 注册表 → 只读/改写判定），
      而原先它 `Config.load(ROOT)` 直接吃用户的 `mcp.servers.ppt` ——
      于是"用户把 PPT 生产面收拢到原生路线"这个**合法的产品决定**会让 64 项全红。
      测试把自己的判据焊在用户配置上，就是本项目反复记的那种"两处真相"。
      现在：夹具里强制带一份可用的 ppt 服务端（解释器取 `deck.python`，
      脚本取 `vendor/ppt-mcp/`），生产配置只用来**如实提示**当前状态。
    """
    try:
        user = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        user = {}
    servers = ((user.get("mcp") or {}).get("servers") or {})
    prod = dict(servers.get("ppt") or {})
    prod_disabled = bool(prod.get("disabled"))
    blk = {k: v for k, v in prod.items() if not k.startswith("_")}
    py = str(((user.get("deck") or {}).get("python") or sys.executable))
    blk.setdefault("command", py)
    blk.setdefault("args", [str(ROOT / "vendor" / "ppt-mcp" / "ppt_mcp_server.py")])
    blk.setdefault("domain", "ppt")
    blk.setdefault("cwd", "workspace")
    blk.setdefault("timeout", 180)
    blk.setdefault("env", {"PYTHONIOENCODING": "utf-8"})
    blk["disabled"] = False
    (_WORK / "config.json").write_text(
        json.dumps({"mcp": {"enabled": True, "servers": {"ppt": blk}}},
                   ensure_ascii=False) + chr(10),
        encoding="utf-8", newline=chr(10))
    return Config.load(_WORK), prod_disabled


def test_assembly() -> tuple[ToolRegistry | None, MCPHub | None, dict]:
    section("1 装配（夹具 config → 真服务端 → 注册表）")
    cfg, prod_off = _fixture_cfg()
    if prod_off:
        print("  ⚠️ 生产配置里 ppt 服务端已摘掉（PPT 走原生路线）"
              "—— 本节按夹具继续验 MCP 接线，不受影响")

    specs = [s for s in cfg.mcp.servers if s.name == "ppt"]
    check("1.1 config.json 里解析出 ppt 服务端", len(specs) == 1, str([s.name for s in cfg.mcp.servers]))
    if not specs:
        return None, None, {}
    spec = specs[0]

    check("1.2 它的 domain 是 ppt（裁剪就按这个域切）", spec.domain == "ppt", spec.domain)
    check("1.3 cwd 钉在 workspace（服务端按相对路径写出来的东西必须落在工作区）",
          spec.cwd == "workspace", spec.cwd)
    check("1.4 command 是**解释器绝对路径**（裸名 python 换个环境就找不到）",
          os.path.isabs(spec.command) and Path(spec.command).is_file(), spec.command)
    check("1.5 服务端脚本在 vendor/ 里且存在（钉版本副本，不靠网络安装）",
          any(str(ROOT / "vendor") in str(a) for a in spec.args)
          and all(Path(a).is_file() for a in spec.args if str(a).endswith(".py")),
          str(spec.args))
    check("1.6 `image_source` 不在 path_args 里（它收路径**或 base64**，当路径改写会把 base64 毁掉）",
          "image_source" not in (spec.path_args or []), str(spec.path_args))
    check("1.7 `save_presentation` 的 `file_path` 在 path_args 里（这是产物落进围栏的保证）",
          "file_path" in (spec.path_args or []), str(spec.path_args))

    hub = MCPHub([spec], workspace=_WORK)
    t0 = time.time()
    reg, info = build_registry(cfg, hub)
    dt = time.time() - t0

    err = hub.errors.get("ppt")
    check("1.8 服务端起来了并列出工具（没有 errors）", not err, (err or "")[:400])
    if err:
        print(f"      服务端 stderr 尾巴：{hub.diag()['clients'][-1].get('stderr_tail') if hub.diag()['clients'] else '（无）'}")
    check("1.9 install_mcp 的 skipped 里没有 ppt（没被重名挤掉、没起不来）",
          not [s for s in info.get("skipped", []) if s.get("server") == "ppt"],
          json.dumps(info.get("skipped"), ensure_ascii=False)[:300])

    installed = [n for n in info.get("installed", [])]
    # ★ L 型绊线：**拿协议自己的回执核对上游的自我声明**。
    #   上游三处口径打架（README 32 / 文件头 20 / get_server_info 32 / 实测 37），
    #   所以数量这条判据的输入必须是 `tools/list`，不是任何一句自述。
    check(f"1.10 真装上的 ppt 工具数 = {EXPECTED_TOOL_COUNT}（协议回执，不是上游自称的 {UPSTREAM_CLAIMED}）",
          len(installed) == EXPECTED_TOOL_COUNT,
          f"实装 {len(installed)}；上游自称 {UPSTREAM_CLAIMED}，实测口径 {EXPECTED_TOOL_COUNT}")
    check("1.11 数量与上游自称**不一致**这件事本身是已知的（L 型：声明≠事实）",
          len(installed) != UPSTREAM_CLAIMED or len(installed) == EXPECTED_TOOL_COUNT)

    bad_dom = [n for n in installed if (reg.get(n).domain != "ppt")]
    check("1.12 每个装上的工具 domain 都是 ppt（否则裁剪切不到它）", not bad_dom, str(bad_dom[:6]))
    bad_src = [n for n in installed if (reg.get(n).source != "mcp")]
    check("1.13 每个装上的工具 source 都是 mcp（证据门与台账要靠它区分来源）",
          not bad_src, str(bad_src[:6]))

    # 只读判定：`_is_mutating` 只看 readOnlyHint / 配置名单，不许靠猜名字
    ro = [n for n in installed if not reg.get(n).mutating]
    check("1.14 带 readOnlyHint 的工具被标成非改写（get_presentation_info 必在其中）",
          any("info" in n or "get" in n for n in ro), f"只读 {len(ro)} 个：{ro[:6]}")
    check("1.15 写入类工具被标成改写（save/add/delete 一类不许漏）",
          all(reg.get(n).mutating for n in installed
              if any(k in n for k in ("save", "add_", "delete", "update"))),
          str([n for n in installed
               if any(k in n for k in ("save", "add_", "delete", "update"))
               and not reg.get(n).mutating][:6]))
    print(f"      （装配耗时 {dt:.1f}s，注册表共 {len(reg)} 个工具）")
    return reg, hub, {"cfg": cfg, "spec": spec, "installed": installed}


# ══════════════════════════════════════════════════════════════════
# 2 · 走注册表真建一份 deck
# ══════════════════════════════════════════════════════════════════
TITLE = "低温诱导对冬虫夏草子实体分化的影响"
BODY = "三次独立重复，120 个样本；转化率随诱导时长单调上升。"


def test_build(reg: ToolRegistry, hub: MCPHub, ctx: dict) -> Path | None:
    section("2 真建一份 deck（走注册表 ⇒ 路径改写真的过一遍）")

    tc = reg.call("create_presentation", {"id": "live_deck"})
    check("2.1 create_presentation 成功", tc.ok, (tc.error or "")[:300])
    if not tc.ok:
        return None

    # ★ 上游已实测缺陷的**故意留痕**：`create_presentation_wrapper` 是死代码，
    #   所以创建**不会**把新演示文稿设为"当前"。这里不主动 switch 就直接 add_slide，
    #   必须失败 —— 把"必须 switch"这条纪律钉成一条**可执行**的判据。
    #
    #   ⚠️ 这条判据有个前提，2026-09-21 实测暴露：上游的失败形状是**裸
    #   `{"error": "…"}`**（无 `ok` 字段、`isError` 也是 false），
    #   而成功形状是 `{"message": …}`（**同样没有 `ok`**）。
    #   ⇒ 若载荷判据只认"字面 ok=false"，这里会读到 `tc.ok == True`，
    #     于是"没切当前就加页"会被当成成功，**假证据进证据门**。
    #   所以下面除了断言失败，还有一条**承重自检**：把载荷判据消掉之后，
    #   同一个调用必须变成"看起来成功"—— 证明拦住它的是那条判据，不是碰巧。
    tc_noswitch = reg.call("add_slide", {"title": "（故意不 switch）"})
    check("2.2 ★ 不 switch 就 add_slide ⇒ 失败（上游 create 不切「当前」，纪律不是空话）",
          not tc_noswitch.ok, f"竟然成功了 —— 上游行为变了，纪律要重写：{(tc_noswitch.result or '')[:160]}")
    check("2.2b 失败原因里带着上游原文（可诊断，不是一句'调用失败'）",
          "No presentation is currently loaded" in (tc_noswitch.error or ""),
          (tc_noswitch.error or "")[:240])

    # ── 承重自检：把载荷级失败判据摘掉，同一次调用必须"变成功" ──
    import rongke.mcp_client as _M                                     # noqa: E402
    _orig_pf = _M._payload_failure
    _M._payload_failure = lambda text, field: ""            # 消掉机制
    try:
        tc_dead = reg.call("add_slide", {"title": "（判据已摘）"})
    finally:
        _M._payload_failure = _orig_pf
    check("2.2c 承重自检：摘掉载荷判据后同一调用**读成成功**（证明 2.2 拦得住靠的是它，不是碰巧）",
          tc_dead.ok, "摘掉判据后仍然失败 ⇒ 2.2 是恒真的，等于没测")
    check("2.2d 承重自检恢复后判据重新生效（不许把状态留在摘掉的那一版）",
          not reg.call("add_slide", {"title": "（恢复后复查）"}).ok)

    tc = reg.call("switch_presentation", {"presentation_id": "live_deck"})
    check("2.3 switch_presentation 成功", tc.ok, (tc.error or "")[:300])

    for i in (1, 2, 3):
        tc = reg.call("add_slide", {"title": f"第 {i} 部分", "layout_index": 1,
                                    "presentation_id": "live_deck"})
        if not tc.ok:
            check(f"2.4 第 {i} 页 add_slide 成功", False, (tc.error or "")[:300])
            return None
    check("2.4 三页 add_slide 都成功", True)

    tc = reg.call("manage_text", {
        "slide_index": 1, "operation": "add", "text": BODY,
        "left": 1.0, "top": 2.2, "width": 8.0, "height": 1.6,
        "font_size": 18, "presentation_id": "live_deck"})
    check("2.5 manage_text operation=add 成功（正文框，显式 18 pt）", tc.ok, (tc.error or "")[:300])

    # ★★ **相对路径**：这是全链里最容易静默出错的一步。
    #   服务端 cwd 已被钉在工作区，但 `_resolve_path_args` 还要把相对路径
    #   **解析成绝对路径**再喂给它（PROVENANCE 里那条理由：路径可能被交给
    #   另一个进程去写，那时相对路径按**它的** cwd 解析，就跑别处去了）。
    tc = reg.call("save_presentation", {"file_path": "group_meeting.pptx"})
    check("2.6 save_presentation 用**相对**路径成功", tc.ok, (tc.error or "")[:300])

    rw = [r for r in hub.rewrites if r.get("arg") == "file_path"]
    check("2.7 ★ 路径改写真的发生了（相对 → 绝对），且被记录下来（悄悄改参数也是静默失败）",
          bool(rw), json.dumps(hub.rewrites[-4:], ensure_ascii=False)[:300])
    if rw:
        check("2.8 改写后的目标就在工作区里（围栏内）",
              Path(rw[-1]["to"]).parent.resolve() == _WORK.resolve(),
              f"{rw[-1]['to']} vs {_WORK}")

    deck = _WORK / "group_meeting.pptx"
    check("2.9 产物真的落盘在工作区（不是进程原始 cwd，也不是别处）",
          deck.is_file() and deck.stat().st_size > 5000,
          f"{deck} 存在={deck.is_file()}"
          + (f" 大小={deck.stat().st_size}" if deck.is_file() else ""))

    facts = probe_pptx(deck)
    check("2.10 量到 3 页", facts.slide_count == 3, str(facts.slide_count))
    check("2.11 量到标题占位符（3 页都有）",
          sum(1 for s in facts.slides if s["has_title"]) == 3,
          str([(s["slide"], s["has_title"]) for s in facts.slides]))
    check(f"2.12 新建默认版心 = 4:3 {DEFAULT_SLIDE_EMU}（上游实测事实，改了要回头看文档）",
          (facts.slide_cx, facts.slide_cy) == DEFAULT_SLIDE_EMU,
          f"{facts.slide_cx}×{facts.slide_cy}")
    check("2.13 正文的显式字号被量到 18 pt（MCP 这条路产出的是**可判**的字号，不是全靠继承）",
          facts.min_body_font_pt is not None and abs(facts.min_body_font_pt - 18.0) < 0.51,
          str(facts.min_body_font_pt))
    return deck


# ══════════════════════════════════════════════════════════════════
# 3 · 把这份**真**产物送进审计（不是合成夹具）
# ══════════════════════════════════════════════════════════════════
def test_audit(cfg, deck: Path | None) -> None:
    section("3 真产物送进 DeckAuditor")
    if deck is None or not deck.is_file():
        check("3.0 前置：产物存在", False, "上一步没产出 pptx")
        return

    # ★ 2026-09-21 新增：**按配方先套主题，再送审**。
    #   契约配方现在是「… save_presentation → set_slide_notes → style_deck → audit_deck」，
    #   本测试要模拟的是**按配方走完**的那份产物。不套主题就送审，等于拿一份
    #   与配方不符的东西去验门 —— 本节 3.4 与 §4 的 4.10 就是这么红的 2 项
    #   （判据没写错，是**前提**过时了）。
    #   顺带把「上游默认 4:3 ⇒ 套主题后 16:9」这个转换钉在**真文件**上。
    st = style_deck(deck.name, workdir=_WORK)
    check("3.0b ★ 按配方套主题成功（这条链上的产物也得走这一步）",
          st.ok, st.format_for_model()[:240])
    ft = probe_pptx(deck)
    check("3.0c ★ 套完画布 4:3 → 16:9（2.12 量的 4:3 是**上游事实**，不是终点）",
          ft.is_widescreen is True, f"aspect={ft.aspect}")

    rep = DeckAuditor(python_exe=cfg.deck.python, skills_dir=cfg.deck.skills_dir,
                      cfg=cfg.deck).audit(deck, workdir=_WORK)
    gates = {g.name: g for g in rep.gates}
    # ★ 门名清单**必须与 `deck_test.py` 6.2 同步**。这条曾经只写"五道门"，
    #   W5 加 density、W8 加 contrast 时都没跟上来 ⇒ 白红一项（判据没错，前提过时）。
    check("3.1 七道门都在（open/xml/type/density/contrast/notes/freshness）",
          set(gates) == {"open", "xml", "type", "density", "contrast",
                         "notes", "freshness"},
          str(sorted(gates)))
    check("3.2 判定落在词表内（不许冒出新词）",
          rep.verdict in ("PASS", "PASS WITH WARNINGS", "REVIEW REQUIRED",
                          "FIX BEFORE DELIVERY"), rep.verdict)
    check("3.3 ok ⇔ 没有阻断项", rep.ok == (not rep.blockers))
    check("3.4 type 门在这份产物上不阻断（18 pt 正文 / 3 页 / 有标题）",
          gates["type"].verdict != "FIX BEFORE DELIVERY", f"{gates['type'].verdict} {gates['type'].detail}")
    # 上游脚本真的跑了 —— 不是"没跑成"（那会 SKIPPED，而 SKIPPED 算阻断）
    xv = gates["xml"].verdict
    check("3.5 ★ xml 门在真 pptx 上真出了结论（不是 SKIPPED / NOT AUDITABLE）",
          xv in ("PASS", "REVIEW REQUIRED", "FIX BEFORE DELIVERY"),
          f"{xv} / {gates['xml'].detail[:200]}")
    check("3.6 上游脚本产出了 JSON 报告文件",
          bool(gates["xml"].report_path) and Path(gates["xml"].report_path).is_file(),
          gates["xml"].report_path)
    check("3.7 freshness 门 PASS（报告与产物是同一版）",
          gates["freshness"].verdict == "PASS", gates["freshness"].detail)

    # 门要读的键必须齐（`deck.py` ↔ `gate.py` 的接口不变量，在**真报告**上再核一次）
    need = {"ok", "verdict", "blockers", "gates", "notes_ratio", "pptx_mtime", "pptx", "warnings"}
    sj = rep.summary_json()
    check("3.8 summary_json 带齐第 9 条门要读的每个键", need <= set(sj), str(sorted(need - set(sj))))
    check("3.9 这份 deck 的备注事实被如实量出（0/3 —— 上游没有备注工具，这是真实约束）",
          isinstance(sj.get("notes_ratio"), (int, float)) and sj["notes_ratio"] == 0.0,
          str(sj.get("notes_ratio")))
    check("3.10 备注门因此是 REVIEW REQUIRED 而**不是**阻断（否则纯 MCP 路径永远过不了 = 死结）",
          gates["notes"].verdict == "REVIEW REQUIRED", gates["notes"].verdict)
    print(f"      判定：{rep.verdict} · 逐门："
          + " ".join(f"{k}={v.verdict}" for k, v in gates.items()))


# ══════════════════════════════════════════════════════════════════
# 4 · 挂载与门（在**真**注册表上）
# ══════════════════════════════════════════════════════════════════
def test_mount_and_gate(reg: ToolRegistry | None) -> None:
    section("4 真注册表上的挂载与门")
    if reg is None:
        check("4.0 前置：注册表可用", False, "装配失败")
        return

    deck_c = TaskContract(raw_goal="把这篇稿子做成组会汇报的 PPT", needs_deck=True)
    write_c = TaskContract(raw_goal="写一篇 1200 字的论文引言", needs_writing=True)

    m_deck = _mount(reg, deck_c)
    m_write = _mount(reg, write_c)

    check("4.1 ★ 安全不变量：needs_deck ⇒ audit_deck 必在 allow 里"
          "（门会拦的情形，工具一定已挂上）",
          m_deck["allow"] is None or "audit_deck" in m_deck["allow"], str(m_deck["domains"]))
    ppt_tools = [n for n in reg.names() if reg.get(n).domain == "ppt"]
    check("4.2 needs_deck ⇒ ppt 域**全部**工具都挂上（一个都不许少）",
          m_deck["allow"] is not None and set(ppt_tools) <= set(m_deck["allow"]),
          f"缺 {sorted(set(ppt_tools) - set(m_deck['allow'] or []))[:8]}")
    check("4.3 非 deck 任务 ⇒ ppt 域一个都够不到",
          m_write["allow"] is not None
          and not (set(ppt_tools) & set(m_write["allow"])),
          str(sorted(set(ppt_tools) & set(m_write["allow"] or []))[:8]))
    check("4.4 常驻域不受影响（core 的工具两个任务都拿得到）",
          (m_write["allow"] is None or "read_file" in m_write["allow"])
          and (m_deck["allow"] is None or "read_file" in m_deck["allow"]))

    full = len(json.dumps(reg.openai_schemas(None), ensure_ascii=False, default=str))
    cut = len(json.dumps(reg.openai_schemas(m_write["allow"]), ensure_ascii=False, default=str))
    check(f"4.5 裁剪在**真**注册表上也真的省了（{full} → {cut} 字符）", cut < full,
          f"{full} vs {cut}")
    pct = (1 - cut / full) * 100 if full else 0
    check(f"4.6 省幅言之有物（实测 {pct:.1f}% ≥ 50%）", pct >= 50, f"{pct:.1f}%")
    print(f"      真注册表：全量 {full} 字符 / 写作任务 {cut} 字符（省 {pct:.1f}%）"
          f" · ppt 工具 {len(ppt_tools)} 个")

    # 第 9 条门在**真**注册表上跑一遍（喂真报告 ⇒ 真判据）
    from rongke.gate import CompletionClaim, CompletionGate, EvidenceRef
    # 注册表是**只增**的：重名注册会 ValueError。`deliver_task` 已由
    # `build_registry` 装过 ⇒ 这里只补缺的，并断言前提（缺了要红，不许静默）。
    check("4.6b 前置：deliver_task 已在注册表里（装配阶段装过，不必也不许重复注册）",
          reg.get("deliver_task") is not None)
    if reg.get("load_skill") is None:
        reg.register(Tool("load_skill", "桩", {"type": "object", "properties": {}},
                          fn=lambda **k: "技能全文…", domain="core"))
    reg.call("load_skill", {"name": "nature-paper2ppt"})
    deck = _WORK / "group_meeting.pptx"

    # ── ★ cwd 就是围栏（K 型的活标本：假定的前提在"测试"这条分支上不成立）──
    # `_make_audit_deck` 的执行体里 `wd = Path.cwd()`，注释写着
    # "任务期间它就是本次 workspace（loop.run() 会 chdir 进去）"。
    # 那句在**生产**里成立（`loop.py:829 os.chdir(ws)`），在**测试**里不成立：
    # 直接调它时 cwd = 仓库根 ⇒ 报告落在仓库根。
    # 实测踩到（2026-09-21）：根目录冒出 `deck_report.json`，
    # 而 `deck_test.py` §11「测试不许污染产物目录」当时是**绿**的 ——
    # 因为污染源不在它那儿，在**这个文件**里。
    # ⇒ ① 先做**阳性对照**：站在仓库根调一次，报告必须落在仓库根（证判据可失守）
    #    ② 再 chdir 进工作区：报告必须落在工作区（还原真实语义，也是防污染手段）
    stray = ROOT / "deck_report.json"
    tc_root = reg.call("audit_deck", {"pptx": str(deck)})
    d_root = json.loads(tc_root.result) if tc_root.ok else {}
    check("4.7 阳性对照：站在仓库根调 audit_deck ⇒ 报告落在仓库根"
          "（`wd = Path.cwd()` ⇒ 判据不是恒真的，cwd 必须自己摆对）",
          stray.exists() and Path(str(d_root.get("report_path") or "")).parent == ROOT,
          f"{d_root.get('report_path')} / 根目录有={stray.exists()}")
    if stray.exists():
        T.stash(stray, "deck-stray-root")      # 挪走，不删

    with chdir(_WORK):
        tc = reg.call("audit_deck", {"pptx": str(deck)})
    check("4.8 真的走注册表调用了 audit_deck（产出的 JSON 可解析）",
          tc.ok and isinstance(json.loads(tc.result or "{}"), dict), (tc.error or "")[:200])
    d = json.loads(tc.result) if tc.ok else {}
    check("4.8b ★ chdir 进工作区后，报告落在**工作区**里（cwd 摆对了才不出围栏）",
          Path(str(d.get("report_path") or "")).parent.resolve() == _WORK.resolve()
          and Path(str(d.get("report_path"))).is_file(),
          f"{d.get('report_path')} vs {_WORK}")
    check("4.8c 仓库根**此刻是干净的**（测试不许污染产物目录 —— 本文件曾是污染源）",
          not stray.exists(), str(stray))
    check("4.9 报告里 pptx 是绝对路径且指向真产物",
          Path(str(d.get("pptx") or "")).is_absolute()
          and Path(str(d["pptx"])).resolve() == deck.resolve(), str(d.get("pptx")))

    claim = CompletionClaim(
        deliverables=["group_meeting.pptx"],
        evidence=[EvidenceRef(tool="audit_deck", call_seq=tc.seq,
                              shows="deck 体检全过", acceptance="deck 体检全过")],
        summary="PPT 已交付")
    c = TaskContract(raw_goal="把这篇稿子做成组会汇报的 PPT",
                     acceptance=["deck 体检全过"], needs_deck=True)
    v = CompletionGate(reg).check(claim, c)
    # 这份 deck 没讲稿 ⇒ notes 门 REVIEW REQUIRED ⇒ 不阻断；所以门应当放行。
    check("4.10 真报告过第 9 条门：没要讲稿时，缺讲稿**不**阻断交付", v.passed, str(v.reasons)[:300])
    check("4.11 但把「缺讲稿」如实写进警告里（不阻断 ≠ 不说）",
          any("deck" in w for w in v.warnings), str(v.warnings)[:240])

    # 反向：契约要了讲稿 ⇒ 同一份报告必须被打回
    c2 = TaskContract(raw_goal="把这篇稿子做成组会 PPT，每页要讲稿",
                      acceptance=["deck 体检全过"], needs_deck=True)
    v2 = CompletionGate(reg).check(claim, c2)
    check("4.12 ★ 契约要了讲稿 ⇒ 同一份真报告被打回（R 型防线在真数据上成立）",
          not v2.passed, str(v2.reasons)[:300])


# ══════════════════════════════════════════════════════════════════
# 5 · 负控：起不来必须报出来，不许静默假绿
# ══════════════════════════════════════════════════════════════════
def test_negative() -> None:
    section("5 负控（服务端起不来 ⇒ 必须记账并报原因）")
    bogus = MCPServerSpec(name="ppt_broken",
                          command=str(_WORK / "根本没有这个解释器" / "python.exe"),
                          args=[str(_WORK / "nope.py")], domain="ppt")
    hub = MCPHub([bogus], workspace=_WORK)
    reg = ToolRegistry()
    info = install_mcp(reg, hub)

    check("5.1 起不来 ⇒ installed 为空（不许假装装上了）", not info.get("installed"),
          str(info.get("installed"))[:200])
    skipped = [s for s in info.get("skipped", []) if s.get("server") == "ppt_broken"]
    check("5.2 起不来 ⇒ 记进 skipped（静默跳过 = 静默失败）", bool(skipped),
          json.dumps(info.get("skipped"), ensure_ascii=False)[:300])
    why = (skipped[0].get("reason") if skipped else "") or ""
    check("5.3 原因非空", bool(why.strip()), why[:200])
    check("5.4 原因里点名是哪个服务端（可诊断）", "ppt_broken" in why or "起不来" in why, why[:200])
    check("5.5 注册表里一个 ppt 工具都没有（失败不许留下半截状态）",
          not [n for n in reg.names() if reg.get(n).domain == "ppt"], str(reg.names()))
    check("5.6 errors 里有登记（`hub.diag()` 能报给上层）", bool(hub.errors.get("ppt_broken")),
          json.dumps(hub.errors, ensure_ascii=False)[:200])


# ══════════════════════════════════════════════════════════════════
def main() -> int:
    global _QUIET
    _QUIET = "--json" in sys.argv[1:]

    if not _QUIET:
        print("═" * 62)
        print("荣珂 · 组会 PPT **真跑**（真 MCP 服务端 / 真产物 / 真门）")
        print("═" * 62)

    reg, hub, ctx = None, None, {}
    try:
        reg, hub, ctx = test_assembly()
        try:
            cfg = ctx.get("cfg")
            deck = test_build(reg, hub, ctx) if reg is not None else None
            if cfg is not None:
                test_audit(cfg, deck)
            test_mount_and_gate(reg)
            test_negative()
        finally:
            if hub is not None:
                n = hub.stop_all()
                if not _QUIET:
                    print(f"\n  收尾   : 停掉 {n} 个 MCP 子进程")
    finally:
        T.stash(_WORK, "deck-live")

    total = len(PASS) + len(FAIL)
    if _QUIET:
        print(json.dumps({"total": total, "pass": len(PASS), "fail": len(FAIL),
                          "failed": FAIL}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "═" * 62)
        print(f"合计 {total} 项：{len(PASS)} 通过，{len(FAIL)} 失败")
        if FAIL:
            print("失败项：")
            for f in FAIL:
                print(f"  ✘ {f}")
        print("═" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
