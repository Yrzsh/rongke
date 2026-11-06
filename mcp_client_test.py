# -*- coding: utf-8 -*-
"""荣珂 MCP 客户端自测。

验证思路（和项目里其它测试一致）：
  * **正控** —— 真连一个服务端、真调工具、真拿到结果。
  * **负控** —— 每一种坏法都要能被**看见**。只证明"好的能通"是不够的，
    那证明不了坏的时候会不会被当成好的（本项目最贵的失败类就是静默失败）。
  * **承重自检** —— 把某道防线的机制**消掉**，对应用例必须变红。
    如果消掉机制它还是绿的，说明那道防线根本没在承重。

对手是 `mcp_fixture_server.py`（能按 FIXTURE_MODE 确定地坏）。
**不拿真 Origin 当负控对手**：它是好的，坏不了，测不出"坏了会不会发现"。
真 Origin 只在最后走一次端到端（--with-origin，默认跳过以免拖慢）。

用法：
    python mcp_client_test.py
    python mcp_client_test.py --with-origin
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）。★ 本文件原来有 **5 处** `tempfile.TemporaryDirectory`
# —— 它在 `__exit__` 里自动 `rmtree`，按**条目数**计费，而且按 `rmtree/unlink` 关键字
# grep 全仓时**一处都扫不到**（真凶藏在上下文管理器的 `__exit__` 里）。
# 现在统一换成 `T.scratch()`（同名语义，出块挪进 `_trash`）。
import _tkit as T                                            # noqa: E402

from rongke.hooks import WorkspaceFence                     # noqa: E402
from rongke.mcp_client import (                             # noqa: E402
    DEFAULT_PATH_ARGS, MCPClient, MCPError, MCPHub, MCPServerSpec,
    _is_mutating, _payload_failure, _resolve_path_args,
    install_mcp, parse_servers,
)
from rongke.tools import Tool, ToolRegistry                 # noqa: E402

FIXTURE = ROOT / "mcp_fixture_server.py"
PY = sys.executable                    # 用当前解释器跑 fixture ⇒ 顺带验证零依赖
ORIGIN_PY = os.path.expanduser(r"~\AppData\Local\Programs\Python\Python312\python.exe")
ORIGIN_SERVER = Path.home() / ".dsh" / "papers" / "workbench" / "origin_mcp_server.py"

ok_all = True
_open_hubs: list[MCPHub] = []


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def check_true(label, cond, extra=""):
    check(label + (f"  {extra}" if extra and not cond else ""), bool(cond), True)


def spec_for(mode: str, name: str | None = None, timeout: float = 12.0,
             **kw) -> MCPServerSpec:
    env = dict(kw.pop("env", {}))
    env["FIXTURE_MODE"] = mode
    return MCPServerSpec(name=name or f"fixture-{mode}", command=PY,
                         args=[str(FIXTURE)], env=env, timeout=timeout, **kw)


def hub_for(mode: str, workspace: Path | None = None, timeout: float = 12.0,
            **kw) -> MCPHub:
    h = MCPHub([spec_for(mode, timeout=timeout, **kw)], workspace=workspace)
    _open_hubs.append(h)
    return h


def raises(fn, *a, **kw) -> tuple[bool, str]:
    """跑一段代码，返回 (是否抛 MCPError, 错误原文)。"""
    try:
        r = fn(*a, **kw)
    except MCPError as e:
        return True, str(e)
    except Exception as e:                       # 别的异常也算"抛了"，但不是我们要的
        return False, f"非 MCPError: {type(e).__name__}: {e}"
    return False, f"（没抛，返回了）{str(r)[:200]}"


def cleanup() -> None:
    for h in _open_hubs:
        try:
            h.stop_all()
        except Exception:
            pass
    _open_hubs.clear()


def stop_hub(h: MCPHub) -> None:
    """停掉一个 Hub，并把它的子进程从 open 列表里摘掉。

    为什么必须在退出临时目录**之前**停：子进程的 cwd 被钉在工作区里，
    Windows 上只要还有进程的 cwd 在那个目录，rmtree 就会 WinError 32
    （实测踩过）。停完再等一小会儿让句柄真正释放。
    """
    try:
        h.stop_all()
    except Exception:
        pass
    if h in _open_hubs:
        _open_hubs.remove(h)
    time.sleep(0.2)


# ═══════════════════════════════════════════════════════════
print("═" * 74)
print(f"荣珂 MCP 客户端自测   python={PY}")
print("═" * 74)

# ───────────────────────────────────────────── 1. 规格解析
print("\n═══ 1. 规格解析：既有的 mcp.json 能不能直接吃 ═══")
_qoder = Path.home() / ".qoder-cn" / "mcp.json"
if _qoder.exists():
    specs, rejected, sources = parse_servers({"import": [str(_qoder)]}, root=ROOT)
    names = sorted(s.name for s in specs)
    check("从 ~/.qoder-cn/mcp.json 导入出两个服务端", names, ["origin-figure", "paper-workbench"])
    check("sources 记下读过的文件", len(sources), 1)
    og = [s for s in specs if s.name == "origin-figure"][0]
    check_true("origin-figure 的 command 是绝对路径（不是裸名 python）",
               Path(og.command).is_absolute(), f"command={og.command}")
else:
    print("  [SKIP] 本机没有 ~/.qoder-cn/mcp.json")

# 远程形态必须被**诚实拒绝**，不能静默跳过
specs2, rejected2, _ = parse_servers({
    "servers": {
        "remote-thing": {"type": "sse", "url": "https://example.com/sse"},
        "no-command": {"args": ["x.py"]},
        "good": {"command": "python", "args": ["a.py"]},
        "off": {"command": "python", "args": ["b.py"], "disabled": True},
    }
}, root=ROOT)
check("只保留可用的那一个", sorted(s.name for s in specs2), ["good", "off"])
check("远程形态被拒绝（不是静默丢弃）", len(rejected2), 2)
check_true("拒绝理由里点明了是 sse 形态",
           any("sse" in r["reason"] for r in rejected2),
           str([r["reason"] for r in rejected2]))
check_true("缺 command 的被拒绝",
           any("command" in r["reason"] for r in rejected2), str(rejected2))

# disabled 要保留在 specs 里，由 Hub 筛出来并**报到明面**
hub_d = MCPHub([s for s in specs2 if s.disabled])
check("disabled 的服务端不进 Hub.specs", sorted(hub_d.specs), [])
check("disabled 被记进 skipped（可见）", len(hub_d.skipped), 1)
check_true("skipped 里说明了原因",
           "disabled" in hub_d.skipped[0]["reason"], str(hub_d.skipped))

# import 路径不存在也要报出来
_, rejected3, _ = parse_servers({"import": [str(ROOT / "没有这个文件.json")]}, root=ROOT)
check_true("import 文件不存在 → 记进 rejected",
           any("不存在" in r["reason"] or "解析失败" in r["reason"] for r in rejected3),
           str(rejected3))

# ───────────────────────────────────────────── 2. 正控
print("\n═══ 2. 正控：真连、真列、真调 ═══")
try:
    hub = hub_for("ok")
    c = hub.client("fixture-ok")
    check("连接是活的", c.alive, True)
    check_true("拿到了 serverInfo.name=rongke-fixture",
               (c.server_info or {}).get("name") == "rongke-fixture", str(c.server_info))
    check("列出的工具名", sorted(t["name"] for t in c.list_tools()),
          ["echo", "fail_soft", "write_default", "write_note"])
    check("调用 echo 的返回", c.call("echo", {"text": "你好"}).strip(), "echo: 你好")
    check("调用不带参数的 echo（缺 text）也不崩", isinstance(c.call("echo", {}), str), True)
except Exception as e:
    ok_all = False
    print(f"  [FAIL] 正控整体崩了：{type(e).__name__}: {e}")

# 调不存在的工具 → 服务端 JSON-RPC error → 必须抛
try:
    bad_hub = hub_for("ok", name="fixture-badtool")
    caught, msg = raises(bad_hub.client("fixture-badtool").call, "no_such_tool", {})
    check("调不存在的工具必须抛", caught, True)
    check_true("错误里点到了 -32602 / 未知工具",
               ("-32602" in msg) or ("未知工具" in msg), msg[:200])
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# 死后能重启（不然一次抖动就要重启整个内核）
try:
    rh = hub_for("ok", name="fixture-restart")
    cc = rh.client("fixture-restart")
    cc.call("echo", {"text": "1"})
    pid1 = cc.pid
    cc.stop()
    check("stop 之后 alive=False", cc.alive, False)
    check("重启后又能调通", rh.client("fixture-restart").call("echo", {"text": "2"}).strip(),
          "echo: 2")
    check_true("重启换了新 pid", rh.client("fixture-restart").pid != pid1,
               f"{pid1} -> {rh.client('fixture-restart').pid}")
except Exception as e:
    ok_all = False
    print(f"  [FAIL] 重启用例崩了：{type(e).__name__}: {e}")

# ───────────────────────────────────────────── 3. 载荷级失败（最贵的一条）
print("\n═══ 3. 载荷级失败必须变成 ok=False（否则就是假证据）═══")
print("  说明：服务端 isError=false、协议层一路成功，只在**载荷里**写 ok=false。")
print("        这正是 origin_plot_xy 出错时的样子。")
try:
    ph = hub_for("ok", name="fixture-payload")
    pc = ph.client("fixture-payload")
    caught, msg = raises(pc.call, "fail_soft", {"reason": "磁盘满了"})
    check("调 fail_soft 必须抛（不许当成功返回）", caught, True)
    check_true("抛出的原因里带着服务端给的原因", "磁盘满了" in msg, msg[:300])
    check_true("错误文案点明了这是'协议层成功但载荷失败'的危险情形",
               "协议层" in msg, msg[:300])
    check("_payload_failure 认出 ok=false",
          _payload_failure(json.dumps({"ok": False, "error": "x"}), "ok"), "x")
    check("_payload_failure 对 ok=true 不误报",
          _payload_failure(json.dumps({"ok": True, "error": "x"}), "ok"), "")
    check("_payload_failure 对普通文本不误判（不误报比不漏报重要）",
          _payload_failure("就是一段普通结论，没有 JSON", "ok"), "")
    check("_payload_failure 对非 JSON 的 { 开头也不崩",
          _payload_failure("{这不是合法 JSON", "ok"), "")
    check("只认字面 false，不认 0（避免误报）",
          _payload_failure(json.dumps({"ok": 0}), "ok"), "")
    # ── 乙条：没有自述字段时，顶层 `error` 非空字符串就是失败 ──
    #    上游 `vendor/ppt-mcp` 149 处失败返回全是这个形状（无 ok、isError=false）
    #    ⇒ 甲条收不到 ⇒ 假证据会进证据门。以下 1 正控 + 5 负控。
    check("乙条：没有 ok 字段时，裸 {\"error\": \"…\"} 被认出（上游 149 处的形状）",
          _payload_failure(json.dumps({"error": "Presentation not found"}), "ok"),
          "Presentation not found")
    check("乙条负控：error 是空串 ⇒ 不算失败（不许把占位当失败）",
          _payload_failure(json.dumps({"error": ""}), "ok"), "")
    check("乙条负控：error 是空白串 ⇒ 不算失败",
          _payload_failure(json.dumps({"error": "   "}), "ok"), "")
    check("乙条负控：error 是 null ⇒ 不算失败（上游成功载荷偶尔带 error:null 的可能形状）",
          _payload_failure(json.dumps({"error": None}), "ok"), "")
    check("乙条负控：error 不是字符串（对象/数组）⇒ 不算失败（只认字符串，判据收窄）",
          _payload_failure(json.dumps({"error": {"code": 1}}), "ok"), "")
    check("乙条负控：上游**成功**载荷形状（success=true 一堆统计，无 error）⇒ 不算失败",
          _payload_failure(json.dumps({"success": True, "presentation_id": "p",
                                       "total_slides": 3}), "ok"), "")
    check("乙条负控：顶层是数组 ⇒ 不算失败（只处理 dict）",
          _payload_failure(json.dumps([{"error": "x"}]), "ok"), "")
    check("乙条负控：ok_field 留空 ⇒ 甲乙两条一起关（'我不想判载荷'的明说）",
          _payload_failure(json.dumps({"error": "x"}), ""), "")
    check("ok_field 留空 = 关掉判定",
          _payload_failure(json.dumps({"ok": False}), ""), "")
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# 承重自检：把 _payload_failure 消掉 ⇒ 上面那条防线必须失效
import rongke.mcp_client as _M          # noqa: E402
try:
    _live = raises(hub_for("ok", name="fixture-lb1").client("fixture-lb1").call,
                   "fail_soft", {})[0]
    _orig = _M._payload_failure
    _M._payload_failure = lambda text, field: ""          # 消掉机制
    try:
        _dead = raises(hub_for("ok", name="fixture-lb2").client("fixture-lb2").call,
                       "fail_soft", {})[0]
    finally:
        _M._payload_failure = _orig
    check("承重自检：机制在位时 fail_soft 抛（真）", _live, True)
    check("承重自检：消掉 _payload_failure 后 fail_soft 不抛了（证明它承重）", _dead, False)
except Exception as e:
    ok_all = False
    print(f"  [FAIL] 承重自检崩了：{type(e).__name__}: {e}")

# ───────────────────────────────────────────── 4. 传输层故障
print("\n═══ 4. 传输层每一种坏法都要被看见 ═══")

# 4.1 junk：服务端往 stdout 打非 JSON（横幅/日志）—— 必须还能工作
try:
    jh = hub_for("junk", name="fixture-junk")
    jc = jh.client("fixture-junk")
    check("stdout 有横幅也不影响握手与调用",
          jc.call("echo", {"text": "hi"}).strip(), "echo: hi")
    check_true("横幅被记进 junk（没有静默丢弃）", len(jc._junk) >= 2, str(jc._junk[:3]))
except Exception as e:
    ok_all = False
    print(f"  [FAIL] junk 用例崩了：{type(e).__name__}: {e}")

# 4.2 isError
try:
    ih = hub_for("iserror", name="fixture-iserr")
    caught, msg = raises(ih.client("fixture-iserr").call, "echo", {"text": "x"})
    check("isError=true 必须抛", caught, True)
    check_true("错误里带上了服务端给的内容", "ECHO_FAILED" in msg, msg[:200])
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# 4.3 crash：进程中途死掉 —— 必须抛，且报出 exit code
try:
    ch = hub_for("crash", name="fixture-crash")
    cc = ch.client("fixture-crash")
    caught, msg = raises(cc.call, "echo", {"text": "x"})
    check("服务端进程死掉必须抛", caught, True)
    check_true("错误里报出了退出码", "exit=3" in msg, msg[:300])
    check_true("错误里带上了服务端 stderr 末尾", "模拟崩溃" in msg, msg[:300])
    check("死后 alive=False", cc.alive, False)
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# 4.4 hang：超时 —— 必须抛，且**真的按超时回来**（不能挂死）
try:
    hh = hub_for("hang", name="fixture-hang", timeout=2.0)
    hc = hh.client("fixture-hang")
    check("hang 模式下 tools/list 仍正常（否则连不上就没法测）",
          len(hc.list_tools()), 4)
    t0 = time.time()
    caught, msg = raises(hc.call, "echo", {"text": "x"}, 2.0)
    dt = time.time() - t0
    check("hang 必须按超时抛", caught, True)
    check_true("错误里说明了超时", "超时" in msg, msg[:200])
    check_true(f"确实是超时返回而不是挂死（{dt:.1f}s < 20s）", dt < 20, f"{dt:.1f}s")
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# 4.5 notools / noinit
try:
    nh = hub_for("notools", name="fixture-notools")
    check("notools 模式下列出 0 个工具", nh.client("fixture-notools").list_tools(), [])
    ni = hub_for("noinit", name="fixture-noinit")
    caught, msg = raises(ni.client, "fixture-noinit")
    check("初始化失败必须抛", caught, True)
    check_true("初始化失败后不留半死进程", ni._clients["fixture-noinit"].alive is False,
               str(ni._clients["fixture-noinit"].alive))
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# 4.6 命令不存在 —— 报错要能直接告诉人怎么修
try:
    bh = MCPHub([MCPServerSpec(name="nope", command="绝对不存在的命令.exe")])
    _open_hubs.append(bh)
    caught, msg = raises(bh.client, "nope")
    check("命令不存在必须抛", caught, True)
    check_true("错误里告诉人改用绝对路径", "绝对路径" in msg, msg[:300])
except Exception as e:
    ok_all = False
    print(f"  [FAIL] {type(e).__name__}: {e}")

# ───────────────────────────────────────────── 5. mutating 判定
print("\n═══ 5. mutating 判定（决定围栏管不管它）═══")
_base = MCPServerSpec(name="s", command="py")
check("默认保守：未知工具按'会改'处理",
      _is_mutating(_base, "x", {}), True)
check("配置 readonly 点名 → 不改",
      _is_mutating(MCPServerSpec(name="s", command="py", readonly=["x"]), "x", {}), False)
check("配置 mutating 点名 → 改",
      _is_mutating(MCPServerSpec(name="s", command="py", mutating=["x"]), "x", {}), True)
check("服务端标 readOnlyHint → 不改",
      _is_mutating(_base, "x", {"annotations": {"readOnlyHint": True}}), False)
check("readonly 点名压过 readOnlyHint=false 的默认",
      _is_mutating(MCPServerSpec(name="s", command="py", readonly=["x"]),
                   "x", {"annotations": {"readOnlyHint": False}}), False)

# ───────────────────────────────────────────── 6. 路径解析 + cwd 钉死
print("\n═══ 6. 相对路径要落在工作区（修掉 figure_render 落错目录那个坑）═══")
with T.scratch("mcp_", "mcp-tmp") as td:
    ws = Path(td) / "run-workspace"
    ws.mkdir(parents=True, exist_ok=True)
    try:
        xh = hub_for("ok", workspace=ws, name="fixture-path")
        # 走 hub.call —— 模型实际走的就是这条路（client.call 不做路径解析）
        got = json.loads(xh.call("fixture-path", "write_note",
                                 {"out": "sub/n.txt", "text": "hello"}))
        check("服务端真的写了文件", got.get("ok"), True)
        check_true("相对路径落在**工作区**里（不是服务端目录、不是项目根）",
                   Path(got["file"]).is_absolute() and str(ws) in got["file"],
                   str(got.get("file")))
        check_true("文件确实存在", (ws / "sub" / "n.txt").exists(), str(ws / "sub" / "n.txt"))
        check_true("改写被记进 hub.rewrites（可见，不悄悄改参数）",
                   any(r["arg"] == "out" for r in xh.rewrites), str(xh.rewrites))
        # 光秃秃的文件名（无分隔符）也要认出来
        _c, rw3 = _resolve_path_args({"out": "fig.png"}, xh.specs["fixture-path"], ws)
        check_true("裸文件名 fig.png 也解析（Origin 把路径交给别进程时要靠它）",
                   bool(rw3) and _c["out"].startswith(str(ws)), str(rw3))
        # 绝对路径原样不动
        absp = ws / "abs.txt"
        _a, rw = _resolve_path_args({"out": str(absp)}, xh.specs["fixture-path"], ws)
        check("已经是绝对路径就不再改写", rw, [])
        # URL 与 DOI 都含 "/"，但**绝不能**当路径改写（实测踩过 DOI 那个坑）
        _b, rw2 = _resolve_path_args({"out": "10.1038/s41586-024"}, xh.specs["fixture-path"], ws)
        check("DOI 不动它（曾经被改写成 <工作区>\\10.1038\\...）", rw2, [])
        _d, rw4 = _resolve_path_args(
            {"out": "https://example.com/api/v1/fig.png"}, xh.specs["fixture-path"], ws)
        check("URL 不动它", rw4, [])
        _e, rw5 = _resolve_path_args(
            {"out": "一段带 / 的中文说明文字"}, xh.specs["fixture-path"], ws)
        check("散文不动它", rw5, [])
        # 非路径参数名不碰
        _f, rw6 = _resolve_path_args(
            {"doi": "10.1038/x", "note": "a/b"}, xh.specs["fixture-path"], ws)
        check("参数名不在清单里就不碰", rw6, [])
        check_true("DEFAULT_PATH_ARGS 认得 out", "out" in DEFAULT_PATH_ARGS)
    except Exception as e:
        ok_all = False
        print(f"  [FAIL] {type(e).__name__}: {e}")
    finally:
        stop_hub(xh)

# ───────────────────────────────────────────── 7. install_mcp + 围栏联动
print("\n═══ 7. 注册进内核 + 与工作区围栏联动 ═══")
with T.scratch("mcp_", "mcp-tmp") as td:
    ws = Path(td).resolve()
    try:
        ih2 = hub_for("ok", workspace=ws, name="fixture-install")
        reg = ToolRegistry()
        res = install_mcp(reg, ih2)
        check("装进去的工具", sorted(res["installed"]),
              ["echo", "fail_soft", "write_default", "write_note"])
        t = reg.get("write_note")
        check("source 标成 mcp", t.source, "mcp")
        check("domain 来自 spec", t.domain, "mcp")
        check("未知工具按'会改'，所以围栏会管它", t.mutating, True)

        # 围栏只在 mutating 时检查路径 —— 拿它验联动
        fence = WorkspaceFence(roots=[ws], base=ws)
        reg.hooks = [fence]
        reg.start_task()

        # 7.1 越界的绝对路径 → 必须被围栏拦
        outside = Path(td).parent / "绝对不该写在这里.txt"
        tc = reg.call("write_note", {"out": str(outside), "text": "x"})
        check("越界绝对路径被打回", tc.ok, False)
        check("拦它的钩子名字", tc.blocked_by, "workspace_fence")
        check_true("越界文件没有被创建", not outside.exists(), str(outside))

        # 7.2 围栏内的绝对路径 → 放行，且真落在那儿
        inside = ws / "ok.txt"
        tc2 = reg.call("write_note", {"out": str(inside), "text": "y"})
        check("围栏内绝对路径放行", tc2.ok, True)
        check_true("文件真落在工作区", inside.exists(), str(inside))

        # 7.3 相对路径 → 解析到工作区后放行
        tc3 = reg.call("write_note", {"out": "rel.txt", "text": "z"})
        check("相对路径放行", tc3.ok, True)
        check_true("相对路径产物落在工作区", (ws / "rel.txt").exists(), str(ws / "rel.txt"))

        # 7.4 载荷级失败经内核调用后 ok 必须是 False（端到端假证据防线）
        tc4 = reg.call("fail_soft", {"reason": "磁盘满"})
        check("经过内核调用，载荷失败 → ToolCall.ok=False", tc4.ok, False)
        check_true("错误原文进了 observation（模型看得见）", "磁盘满" in tc4.error, tc4.error[:200])

        # 7.5 重名不静默覆盖
        reg2 = ToolRegistry()
        reg2.register(Tool(name="echo", description="工作台已有的同名工具",
                           parameters={"type": "object", "properties": {}},
                           fn=lambda **k: "来自工作台", source="workbench"))
        ih3 = hub_for("ok", workspace=ws, name="fixture-collide")
        res2 = install_mcp(reg2, ih3)
        check("重名的 echo 没被装进来", "echo" in res2["installed"], False)
        check("原有的 echo 还在（没被覆盖）",
              reg2.call("echo", {}).ok and "工作台" in reg2.call("echo", {}).result, True)
        check_true("重名被记进 skipped 且说明原因",
                   any("重名" in s.get("reason", "") for s in res2["skipped"]),
                   str(res2["skipped"]))

        # 7.6 服务端起不来时，install 要报出来而不是当没事
        bad_hub2 = MCPHub([MCPServerSpec(name="dead", command="绝对不存在的命令.exe")],
                          workspace=ws)
        _open_hubs.append(bad_hub2)
        res3 = install_mcp(ToolRegistry(), bad_hub2)
        check("起不来的服务端一个工具都没装", res3["installed"], [])
        check_true("且被记进 skipped", len(res3["skipped"]) == 1, str(res3["skipped"]))
    except Exception as e:
        ok_all = False
        print(f"  [FAIL] 集成用例崩了：{type(e).__name__}: {e}")
    finally:
        # 子进程 cwd 钉在工作区里 ⇒ 必须**先停进程**再退出临时目录，
        # 否则 Windows 上 rmtree 会 WinError 32（实测踩过）。
        for _h in (ih2, ih3, bad_hub2):
            try:
                stop_hub(_h)
            except Exception:
                pass

# ───────────────────────────────────────────── 8. 配置层合并语义
print("\n═══ 8. 配置层：inline 覆盖 import 是**合并**，不是整体替换 ═══")
with T.scratch("mcp_", "mcp-tmp") as td:
    try:
        imp = Path(td) / "other-mcp.json"
        imp.write_text(json.dumps({"mcpServers": {"svc": {
            "command": "ABS_PY", "args": ["s.py"], "env": {"A": "1"}}}},
            ensure_ascii=False), encoding="utf-8")
        # 荣珂自己的 config.json 只声明**策略**，起服务的 command 留在那一份里
        specs, rej, srcs = parse_servers({
            "import": [str(imp)],
            "servers": {"svc": {"domain": "figure", "timeout": 42, "env": {"B": "2"}}},
        }, root=Path(td))
        check("没有 rejected（合并不该判成缺 command）", rej, [])
        s = specs[0]
        check("command 从 import 那份继承下来", s.command, "ABS_PY")
        check("args 也继承", s.args, ["s.py"])
        check("策略被 inline 覆盖", (s.domain, s.timeout), ("figure", 42.0))
        check("两边各写一半的 env 合起来了", s.env, {"A": "1", "B": "2"})
        check("sources 记下了读过的文件", len(srcs), 1)

        # disabled 先判：关掉的服务端不需要一份完整配置
        specs2, rej2, _ = parse_servers({"servers": {"off": {"disabled": True}}}, root=Path(td))
        check("disabled 项没有 command 也算合法",
              [(x.name, x.disabled) for x in specs2], [("off", True)])
        check("且不会被误报成'缺 command'", rej2, [])

        # 真正的 Config 层也要对得上
        cfg_servers, cfg_rej, _ = parse_servers({"servers": {
            "remote": {"type": "sse", "url": "https://e.com/sse"}}}, root=Path(td))
        check("远程传输在配置层就被拒（有理由）", len(cfg_rej), 1)
        check_true("理由里说明了 v1 只支持 stdio",
                   "stdio" in cfg_rej[0]["reason"], cfg_rej[0]["reason"])
    except Exception as e:
        ok_all = False
        print(f"  [FAIL] 配置层用例崩了：{type(e).__name__}: {e}")

# ───────────────────────────────────────────── 9. 换工作区
print("\n═══ 9. 换任务工作区后，产物必须跟着换 ═══")
print("  为什么这条要紧：每个任务的 workspace 不同，而 MCP 子进程的 cwd 是")
print("  起它那一刻钉死的。沿用旧连接 ⇒ 产物落进**上一个任务**，且毫无报错。")
with T.scratch("mcp_", "mcp-tmp") as td:
    ws1 = Path(td) / "run-1"
    ws2 = Path(td) / "run-2"
    ws1.mkdir()
    ws2.mkdir()
    try:
        # 9.1 负控：只改 workspace，不关连接 ⇒ cwd 还钉在旧工作区
        n1 = MCPHub([spec_for("ok", name="fx-ws-neg")], workspace=ws1)
        _open_hubs.append(n1)
        n1.call("fx-ws-neg", "write_default", {"text": "x"})
        n1.workspace = ws2                       # 只换 workspace
        # ★ 不再删上一次的产物：下面的断言读的是**返回值里的路径**，跟文件在不在
        #   毫无关系，而 `write_default` 本来就会覆盖写。这两行 `unlink`
        #   是纯粹的额度消耗（本环境按**条目数**收删除费，见 `_tkit`）。
        r = json.loads(n1.call("fx-ws-neg", "write_default", {"text": "y"}))
        check_true("负控：不关连接 ⇒ 产物仍落进**旧**工作区（这就是要 stop_all 的原因）",
                   str(ws1) in r["file"], f"file={r.get('file')} cwd={r.get('cwd')}")

        # 9.2 正控：换 workspace + stop_all ⇒ 重起后 cwd 是新工作区
        p1 = MCPHub([spec_for("ok", name="fx-ws-pos")], workspace=ws1)
        _open_hubs.append(p1)
        p1.call("fx-ws-pos", "write_default", {"text": "x"})
        p1.workspace = ws2
        p1.stop_all()                            # ← loop._run_serial 做的就是这两句
        # 同上：断言看的是返回值里的路径，覆盖写已经足够，不必先删（见 `_tkit`）。
        r2 = json.loads(p1.call("fx-ws-pos", "write_default", {"text": "y"}))
        check_true("正控：换 workspace + stop_all ⇒ 产物落进**新**工作区",
                   str(ws2) in r2["file"], f"file={r2.get('file')} cwd={r2.get('cwd')}")
        check_true("服务端自己看到的 cwd 也确实换了",
                   Path(str(r2.get("cwd") or "")).name == "run-2", str(r2.get("cwd")))

        # 9.3 带路径参数的那条路同样要跟着换
        q1 = MCPHub([spec_for("ok", name="fx-ws-arg")], workspace=ws1)
        _open_hubs.append(q1)
        q1.workspace = ws2
        q1.stop_all()
        r3 = json.loads(q1.call("fx-ws-arg", "write_note", {"out": "a.txt", "text": "z"}))
        check_true("带 out 参数时也落进新工作区", str(ws2) in r3["file"], r3["file"])
    except Exception as e:
        ok_all = False
        print(f"  [FAIL] 换工作区用例崩了：{type(e).__name__}: {e}")
    finally:
        for _h in (locals().get("n1"), locals().get("p1"), locals().get("q1")):
            if _h is not None:
                try:
                    stop_hub(_h)
                except Exception:
                    pass

# ───────────────────────────────────────────── 10. 真 Origin 端到端
print("\n═══ 10. 真 Origin 端到端（MCP 画图）═══")
if "--with-origin" not in sys.argv:
    print("  [SKIP] 未加 --with-origin（它会拉起 Origin COM，慢）")
elif not ORIGIN_SERVER.exists():
    print(f"  [SKIP] 找不到 {ORIGIN_SERVER}")
else:
    with T.scratch("mcp_", "mcp-tmp") as td:
        ws = Path(td).resolve()
        oh: MCPHub | None = None
        try:
            oh = MCPHub([MCPServerSpec(
                name="origin-figure", command=ORIGIN_PY, args=[str(ORIGIN_SERVER)],
                timeout=180.0, domain="figure",
                mutating=["origin_plot_xy", "origin_plot_spec"],
                readonly=["origin_status"],
            )], workspace=ws)
            _open_hubs.append(oh)
            oc = oh.client("origin-figure")
            print(f"    已连上：{oc.server_info}")
            check("列出 3 个 Origin 工具",
                  sorted(t["name"] for t in oc.list_tools()),
                  ["origin_plot_spec", "origin_plot_xy", "origin_status"])
            st = json.loads(oc.call("origin_status"))
            print(f"    origin_status -> {st}")
            check("Origin 可驱动", st.get("ok"), True)

            out = ws / "mcp_origin_test.png"
            r = json.loads(oc.call("origin_plot_xy", {
                "xs": [1, 2, 3, 4, 5], "ys": [2.1, 3.9, 6.2, 7.8, 10.1],
                "out": str(out), "kind": "scatter",
                "xlabel": "浓度", "ylabel": "响应",
            }, 180.0))
            print(f"    origin_plot_xy -> {r}")
            check("出图成功", r.get("ok"), True)
            check_true("图落在**工作区**里", out.exists() and out.stat().st_size > 1000,
                       f"{out} {out.stat().st_size if out.exists() else 0}B")
        except MCPError as e:
            ok_all = False
            print(f"  [FAIL] Origin 端到端失败：{str(e)[:600]}")
        except Exception as e:
            ok_all = False
            print(f"  [FAIL] {type(e).__name__}: {e}")
        finally:
            if oh is not None:
                stop_hub(oh)

cleanup()

print("\n" + "═" * 74)
print("全部通过 ✅" if ok_all else "有失败 ❌")
print("═" * 74)
sys.exit(0 if ok_all else 1)
