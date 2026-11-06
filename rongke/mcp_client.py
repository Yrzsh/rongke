# -*- coding: utf-8 -*-
"""荣珂 MCP 客户端 —— 让内核**主动**连上外部 MCP 服务端。

在此之前荣珂只有 `bridge.py` 那条**同进程反射**通道，方向是"别人连工作台"：
它把 workbench 的工具函数 import 进来直接调。荣珂自己**够不到任何 MCP 服务端**。
本机就摆着 `origin_mcp_server.py`（Origin 出图），荣珂却用不上 —— 这就是
"他是客户端"要补的那一块。

── 为什么用 stdlib 手写 JSON-RPC，而不是 `import mcp` ──────────────
本机两个解释器**依赖不同**（2026-09-18 实测）：

    系统 Python312      → 有 mcp，有 pywin32
    workbuddy 托管 3.13 → 两个都没有

荣珂跑在哪个解释器上，不该决定它能不能用 MCP。而 MCP 的 stdio 传输本来
就是"一行一个 JSON-RPC 2.0 对象"，没有哪一步非要 SDK 不可。
  ⇒ 客户端零依赖 ⇒ **内核与解释器解耦**。
  ⇒ 需要特定解释器的是**服务端**，那就让配置来指定（`spec.command`）——
     这本来就是每个服务端自己的事，不是内核的事。

── 三处必须做对，错了就是静默失败 ─────────────────────────────
1. **服务端报错必须变成 ok=False。**
   `origin_plot_xy` 出错时**不抛异常**：它返回 `{"ok": false, ...}`，
   而 MCP 层面 `isError` 仍是 false —— 整条链路看起来一切成功。
   包装函数若原样返回字符串，`ToolCall.ok` 就是 True，
   于是**假证据**进了证据门（这是本项目最贵的失败类，见 pitfalls.py）。
   ⇒ 包装函数必须 raise。

2. **子进程 cwd 必须钉在工作区。**
   围栏放行相对路径的理由是"进程 cwd 已在 workspace 内"，但这句话只对
   **本进程**成立。实测：workbench 的 `figure_render` 走 `_run_cli(cwd=WB)`，
   产物落到了 `~/.dsh/papers/workbench/probe_bar.png`，而它返回 `ok=True`。
   MCP 服务端由我们 spawn，cwd 由我们定 —— 这一层能把上述漏洞补上。

3. **诚实拒绝。**
   配置里 `type`/`url` 的远程服务端 v1 不支持，必须报到明面上。
   静默跳过 = 用户以为连上了，实际没连 —— 又一种假安慰。

── 位置与职责的边界（防止"两处真相"）──────────────────────────
`_resolve_path_args()` **只解析路径，不裁决越界**。
"能不能写这里"只有一处判定：`hooks.WorkspaceFence`。
这里若也拦一刀，同一条规则就有两个实现，迟早漂成两个口径。
所以本模块的职责单一：让相对路径变成**围栏内的绝对路径**，
使得即使服务端自己的 cwd 不是我们的工作区，产物也落在工作区内。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hooks import looks_like_path     # 复用"什么看起来像路径"的唯一判定
from .tools import Tool, ToolRegistry

PROTOCOL_VERSION = "2025-06-18"
_CLIENT_NAME = "rongke"

# 默认哪些**参数名**算"产出路径"——会被解析成绝对路径。
# 参数名是**准入条件**，不是唯一条件：值还要过 _should_rewrite()，
# 因为下面这几种值也含 "/"，会被 looks_like_path 认成路径，改了就把参数改坏。
# 想加就加，或者用 spec.path_args 覆盖。
DEFAULT_PATH_ARGS = frozenset({
    "out", "out_path", "output", "output_path",
    "dest", "destination", "save", "save_as", "target",
    "path", "file", "filepath", "file_path",
})

# 值层面**明确不改写**的两类。规则是精确的，不是猜的：
#   URL —— 有 scheme://
#   DOI —— 一律以 "10.<4~9 位登记号>/" 开头（DOI 规范如此）
# 它们是实测发现的真问题：`10.1038/s41586-024` 曾被改写成
# `<工作区>\10.1038\s41586-024` —— 参数被改坏，而且悄无声息。
_URL_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_DOI_LIKE = re.compile(r"^10\.\d{4,9}/")
# 光秃秃的文件名（fig.png）没有路径分隔符，looks_like_path 认不出；
# 但对"输出路径"参数来说它显然是个文件 ⇒ 按扩展名补一条。
_HAS_EXT = re.compile(r"\.\w{1,6}$")

# 载荷里判成败用的字段名（服务端自己的约定）；留空则不判。
DEFAULT_OK_FIELD = "ok"

_STDERR_KEEP = 80
_JUNK_KEEP = 40


class MCPError(RuntimeError):
    """MCP 链路上的任何失败。

    **必须抛**，不能吞成"看起来成功的返回值"—— 吞掉就等于给证据门喂假证据。
    """


# ══════════════════════ 服务端规格 ══════════════════════
@dataclass
class MCPServerSpec:
    """一个 MCP 服务端的启动与治理方式。"""

    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # 子进程工作目录。"workspace"（默认）= 本次任务的产物目录，
    # 见模块头「三处必须做对」第 2 条。也可以给绝对路径。
    cwd: str = "workspace"
    timeout: float = 120.0
    domain: str = "mcp"
    # 治理：谁改世界。判定优先级见 _is_mutating()（只此一处）。
    mutating: list[str] = field(default_factory=list)
    readonly: list[str] = field(default_factory=list)
    path_args: list[str] = field(default_factory=list)   # 空 = 用 DEFAULT_PATH_ARGS
    ok_field: str = DEFAULT_OK_FIELD                     # 留空 = 不判载荷成败
    disabled: bool = False
    origin: str = ""          # 这条规格来自哪个配置文件（诊断用）

    # ── 派生 ────────────────────────────────────────────────
    def script_dir(self) -> str:
        """子进程脚本所在目录（用来补 PYTHONPATH）。

        为什么需要：我们把 cwd 钉在工作区，若服务端靠"cwd 在它自己目录"
        才能 import 同目录模块，就会被打断。补一条 PYTHONPATH 把这条路留着。
        """
        for a in self.args or []:
            if isinstance(a, str) and a.lower().endswith(".py"):
                return str(Path(a).resolve().parent)
        c = (self.command or "")
        if c.lower().endswith(".py"):
            return str(Path(c).resolve().parent)
        return ""

    def resolve_cwd(self, workspace: Path | None) -> str:
        c = (self.cwd or "").strip()
        if c in ("", "workspace"):
            return str(Path(workspace) if workspace else Path.cwd())
        return str(Path(os.path.expanduser(c)))

    def child_env(self, workspace: Path | None = None) -> dict:
        _ = workspace
        e = dict(os.environ)
        # 管道里的 Python stdout 默认**块缓冲**：服务端不主动 flush，
        # 我们就一直读不到那行 JSON，最后表现为"超时"而不是"没反应"。
        # 这是真实故障，不是理论风险 —— 必须替子进程关掉缓冲。
        e["PYTHONUNBUFFERED"] = "1"
        e.setdefault("PYTHONIOENCODING", "utf-8")
        sd = self.script_dir()
        if sd:
            old = e.get("PYTHONPATH", "")
            e["PYTHONPATH"] = sd + (os.pathsep + old if old else "")
        e.update({str(k): str(v) for k, v in (self.env or {}).items()})
        return e

    @property
    def display_command(self) -> str:
        bits = [self.command] + [str(a) for a in self.args]
        return " ".join(bits)

    def as_dict(self) -> dict:
        return {
            "name": self.name, "command": self.command, "args": list(self.args),
            "cwd": self.cwd, "timeout": self.timeout, "domain": self.domain,
            "mutating": list(self.mutating), "readonly": list(self.readonly),
            "path_args": list(self.path_args), "ok_field": self.ok_field,
            "disabled": self.disabled, "origin": self.origin,
        }


# ══════════════════════ 配置解析 ══════════════════════
_REMOTE_TYPES = {"sse", "http", "streamable-http", "streamable_http", "ws", "websocket"}


def _spec_from_entry(name: str, entry: dict, origin: str) -> tuple[MCPServerSpec | None, dict | None]:
    """把配置里的一条 entry 变成规格。返回 (spec, 拒绝理由)。

    不支持的形态**必须**返回拒绝理由，让上层报到明面 —— 静默跳过就是假安慰。
    """
    if not isinstance(entry, dict):
        return None, {"server": name, "reason": f"配置不是对象（是 {type(entry).__name__}）", "file": origin}

    # disabled 先判：关掉的服务端**不需要**一份完整的配置（不该因为
    # 它没写 command 就报"缺少 command" —— 那是误导人的理由）。
    # 而且它也是别的客户端的字段（~/.workbuddy/mcp.json 里有），要尊重。
    if bool(entry.get("disabled", False)):
        return MCPServerSpec(name=name, command="", disabled=True, origin=origin), None

    etype = str(entry.get("type") or "").strip().lower()
    if etype in _REMOTE_TYPES or entry.get("url"):
        t = etype or "url"
        return None, {
            "server": name,
            "reason": (f"v1 只支持 stdio 传输，这个服务端是 {t} 形态"
                       f"（{entry.get('url') or '无 url'}）。"
                       f"远程传输要另外做 HTTP/SSE + 会话管理，先不支持而不是假装支持。"),
            "file": origin,
        }

    cmd = str(entry.get("command") or "").strip()
    if not cmd:
        return None, {"server": name, "reason": "配置里没有 command", "file": origin}

    args = entry.get("args") or []
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list):
        return None, {"server": name, "reason": f"args 不是数组（是 {type(args).__name__}）", "file": origin}
    if not all(isinstance(a, (str, int, float)) for a in args):
        return None, {"server": name, "reason": "args 里有非标量元素", "file": origin}

    env = entry.get("env") or {}
    if not isinstance(env, dict):
        return None, {"server": name, "reason": f"env 不是对象（是 {type(env).__name__}）", "file": origin}

    def _list(key: str) -> list[str]:
        v = entry.get(key) or []
        if isinstance(v, str):
            v = [v]
        return [str(x) for x in v] if isinstance(v, list) else []

    try:
        timeout = float(entry.get("timeout") or 120.0)
    except Exception:
        timeout = 120.0

    spec = MCPServerSpec(
        name=name,
        command=cmd,
        args=[str(a) for a in args],
        env={str(k): str(v) for k, v in env.items()},
        cwd=str(entry.get("cwd") or "workspace"),
        timeout=timeout,
        domain=str(entry.get("domain") or "mcp"),
        mutating=_list("mutating"),
        readonly=_list("readonly"),
        path_args=_list("path_args"),
        ok_field=str(entry.get("ok_field", DEFAULT_OK_FIELD)),
        disabled=False,
        origin=origin,
    )
    return spec, None


def read_server_file(path: Path) -> tuple[dict, str]:
    """读一个 MCP 配置文件，返回 ({name: entry}, 错误)。

    认得 `mcpServers` 与 `servers` 两种键（各家客户端各用各的）。
    """
    if not path.exists():
        return {}, f"文件不存在: {path}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {}, f"解析失败 {path}: {type(e).__name__}: {e}"
    if not isinstance(raw, dict):
        return {}, f"顶层不是对象: {path}"
    for key in ("mcpServers", "servers"):
        v = raw.get(key)
        if isinstance(v, dict):
            return {str(k): val for k, val in v.items()}, ""
    return {}, f"没有 mcpServers / servers 段: {path}"


def _merge_entry(base: dict, over: dict) -> dict:
    """inline 的 servers 覆盖 import 进来的同名项 —— **合并**，不是整体替换。

    为什么必须合并（实测踩过）：荣珂自己的 config.json 只想声明**策略**
    （domain / timeout / readonly / mutating），起服务的 command 与 args
    留在 ~/.qoder-cn/mcp.json 那一份里（同一份定义抄两遍必然漂）。
    整体替换的话，只有策略、没有 command 的那条会被判成"配置里没有 command"
    而被拒 —— 明明文件里写了，却报缺字段，很难查。
    """
    out = dict(base)
    out.update(over)
    # env 是最常被两边各写一半的字段，单独再合一层
    if isinstance(base.get("env"), dict) and isinstance(over.get("env"), dict):
        out["env"] = {**base["env"], **over["env"]}
    return out


def parse_servers(raw: dict, root: Path | None = None) -> tuple[list[MCPServerSpec], list[dict], list[str]]:
    """把配置的 `mcp` 段解析成规格。

    返回 (specs, rejected, sources)：
      * specs    —— 可用的服务端（含 disabled 的，由 Hub 再筛）
      * rejected —— **不支持的形态 / 文件读不到**，逐条给理由（绝不静默丢）
      * sources  —— 读过的配置文件（诊断用）

    `raw` 形如：
        {"enabled": true,
         "import": ["~/.qoder-cn/mcp.json"],   # 从别的客户端配置里导入
         "servers": {"origin-figure": {"domain": "figure", "mutating": [...]}}}
    同名以**后面**的为准：先 import 的顺序，后 inline servers；
    且 inline 是**合并在** import 之上，不是替换（见 _merge_entry）。
    """
    root = root or Path.cwd()
    merged: dict[str, tuple[dict, str]] = {}
    rejected: list[dict] = []
    sources: list[str] = []

    imports = raw.get("import") or raw.get("imports") or []
    if isinstance(imports, str):
        imports = [imports]
    for item in imports if isinstance(imports, list) else []:
        try:
            p = Path(os.path.expanduser(str(item)))
            if not p.is_absolute():
                p = (root / p).resolve()
        except Exception as e:
            rejected.append({"server": str(item), "reason": f"import 路径无法解析: {e}", "file": ""})
            continue
        entries, err = read_server_file(p)
        sources.append(str(p))
        if err:
            rejected.append({"server": "", "reason": err, "file": str(p)})
            continue
        for n, e in entries.items():
            merged[n] = (e, str(p))

    inline = raw.get("servers") or {}
    if isinstance(inline, dict):
        for n, e in inline.items():
            key = str(n)
            prev = merged.get(key)
            if prev and isinstance(prev[0], dict) and isinstance(e, dict):
                merged[key] = (_merge_entry(prev[0], e), "config.json")
            else:
                merged[key] = (e, "config.json")

    specs: list[MCPServerSpec] = []
    for name, (entry, origin) in merged.items():
        spec, why = _spec_from_entry(name, entry, origin)
        if spec is None:
            if why:
                rejected.append(why)
            continue
        specs.append(spec)

    specs.sort(key=lambda s: s.name)
    return specs, rejected, sources


# ══════════════════════ 客户端 ══════════════════════
def _build_command(spec: MCPServerSpec) -> list[str]:
    return [spec.command] + [str(a) for a in (spec.args or [])]


class MCPClient:
    """到某个 MCP 服务端的一条 stdio 连接。懒启动、可重启、绝不静默失败。"""

    def __init__(self, spec: MCPServerSpec, workspace: Path | None = None):
        self.spec = spec
        self.workspace = workspace
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._waiters: dict[int, queue.Queue] = {}
        self._id = 0
        self._threads: list[threading.Thread] = []
        self._stderr: list[str] = []
        self._junk: list[str] = []
        self._dead = threading.Event()
        self.server_info: dict = {}
        self.protocol: str = ""
        self.notices: list[str] = []      # 握手时记下的非常见情况（如实报告）

    # ── 状态 ────────────────────────────────────────────────
    @property
    def alive(self) -> bool:
        return (self._proc is not None
                and self._proc.poll() is None
                and not self._dead.is_set())

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def stderr_tail(self, n: int = 12) -> str:
        return "\n".join(self._stderr[-n:])

    def junk_tail(self, n: int = 8) -> str:
        return "\n".join(self._junk[-n:])

    # ── 启动 ────────────────────────────────────────────────
    def start(self) -> None:
        """spawn 子进程并完成 initialize 握手。失败一律抛 MCPError。"""
        if self.alive:
            return
        if not self.spec.command:
            raise MCPError(f"MCP 服务端 {self.spec.name} 没配 command")

        cwd = self.spec.resolve_cwd(self.workspace)
        cmd = _build_command(self.spec)
        flags = 0
        if os.name == "nt":
            # 不给子进程建控制台窗口：荣珂是无人值守跑的，弹个黑框既没用
            # 又会打断用户；MCP 服务端不需要交互。
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=cwd, env=self.spec.child_env(self.workspace),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=flags,
            )
        except FileNotFoundError as e:
            raise MCPError(
                f"起不来 MCP 服务端 {self.spec.name}：找不到命令 {self.spec.command!r}。\n"
                f"请在配置里把 command 写成**解释器的绝对路径**"
                f"（裸名 `python` 依赖 PATH，换个环境就找不到）。\n"
                f"原始错误：{e}"
            ) from e
        except Exception as e:
            raise MCPError(f"起不来 MCP 服务端 {self.spec.name}："
                           f"{type(e).__name__}: {e}\n命令：{self.spec.display_command}") from e

        self._dead.clear()
        self._stderr.clear()
        self._junk.clear()
        self._waiters.clear()
        t_out = threading.Thread(target=self._read_stdout, name=f"mcp-{self.spec.name}-out", daemon=True)
        t_err = threading.Thread(target=self._read_stderr, name=f"mcp-{self.spec.name}-err", daemon=True)
        t_out.start()
        t_err.start()
        self._threads = [t_out, t_err]

        try:
            self._handshake()
        except Exception:
            # 握手失败就别留个半死进程在系统里
            self.stop(grace=1.0)
            raise

    def _handshake(self) -> None:
        res = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": _CLIENT_NAME, "version": _version()},
        }, timeout=min(30.0, self.spec.timeout))
        self.server_info = res.get("serverInfo") or {}
        self.protocol = str(res.get("protocolVersion") or "")
        if self.protocol and self.protocol != PROTOCOL_VERSION:
            # 不硬失败：版本不同不等于不能用，但要如实记下来。
            self.notices.append(
                f"服务端协议版本 {self.protocol} ≠ 客户端 {PROTOCOL_VERSION}（继续用，但留意字段差异）")
        # 通知：不带 id，不期待回应
        self._notify("notifications/initialized")

    # ── 读线程 ──────────────────────────────────────────────
    def _read_stdout(self) -> None:
        try:
            stream = self._proc.stdout if self._proc else None
            if stream is None:
                return
            for line in stream:
                s = line.strip()
                if not s:
                    continue
                try:
                    msg = json.loads(s)
                except Exception:
                    # 服务端往 stdout 打了非 JSON（横幅 / 用它打日志）。
                    # 不能当协议消息，也不能静默丢 —— 记下来，出错时给人看。
                    self._push(self._junk, s, _JUNK_KEEP)
                    continue
                if not isinstance(msg, dict):
                    self._push(self._junk, s, _JUNK_KEEP)
                    continue
                mid = msg.get("id")
                if mid in self._waiters:
                    self._waiters[mid].put(msg)
                # id 不在等待集里 = 通知，或我们已超时放弃的响应 → 丢弃
        except Exception:
            pass
        finally:
            self._dead.set()
            for q in list(self._waiters.values()):
                try:
                    q.put({"__dead__": True})
                except Exception:
                    pass

    def _read_stderr(self) -> None:
        try:
            stream = self._proc.stderr if self._proc else None
            if stream is None:
                return
            for line in stream:
                self._push(self._stderr, line.rstrip("\n"), _STDERR_KEEP)
        except Exception:
            pass

    @staticmethod
    def _push(buf: list[str], item: str, keep: int) -> None:
        buf.append(item)
        while len(buf) > keep:
            buf.pop(0)

    # ── 收发 ────────────────────────────────────────────────
    def _write(self, obj: dict) -> None:
        p = self._proc
        if p is None or p.stdin is None:
            raise MCPError(f"MCP 服务端 {self.spec.name} 的连接未启动")
        try:
            p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            p.stdin.flush()
        except Exception as e:
            raise MCPError(f"写 MCP 请求失败（服务端可能已退出）：{e}\n{self._exit_reason()}") from e

    def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._write(msg)

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        if self._proc is None:
            raise MCPError(f"MCP 服务端 {self.spec.name} 未启动")
        if self._proc.poll() is not None or self._dead.is_set():
            raise MCPError(self._exit_reason(method))
        t = float(timeout or self.spec.timeout)
        with self._lock:            # 单管道 request/response，串行发
            self._id += 1
            mid = self._id
            q: queue.Queue = queue.Queue()
            self._waiters[mid] = q
            try:
                self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
                try:
                    msg = q.get(timeout=t)
                except queue.Empty:
                    raise MCPError(self._timeout_reason(method, t)) from None
            finally:
                self._waiters.pop(mid, None)

        if isinstance(msg, dict) and msg.get("__dead__"):
            raise MCPError(self._exit_reason(method))
        err = msg.get("error")
        if err:
            if isinstance(err, dict):
                raise MCPError(f"MCP {self.spec.name}.{method} 返回错误"
                               f"（code={err.get('code')}）：{err.get('message')}")
            raise MCPError(f"MCP {self.spec.name}.{method} 返回错误：{err}")
        res = msg.get("result")
        return res if isinstance(res, dict) else {"_raw": res}

    # ── 诊断文案 ────────────────────────────────────────────
    def _exit_reason(self, method: str = "") -> str:
        code = self._proc.poll() if self._proc is not None else None
        if code is None and self._dead.is_set():
            # stdout 已经 EOF ⇒ 进程正在退出，只是还没被回收（reader 线程先拿到 EOF，
            # poll() 这一刻可能还是 None）。报"已退出"却不给退出码，等于让人去猜
            # —— 实测就是这个坑。所以这里等一小会儿把退出码收回来。
            try:
                code = self._proc.wait(timeout=2.0)
            except Exception:
                code = None
        head = f"MCP 服务端 {self.spec.name} 已退出"
        if code is not None:
            head += f"（exit={code}）"
        if method:
            head += f"，当时正在等 {method} 的回应"
        parts = [head + "。", f"命令：{self.spec.display_command}"]
        t = self.stderr_tail()
        if t:
            parts.append("服务端 stderr 末尾：\n" + t)
        j = self.junk_tail()
        if j:
            parts.append("服务端往 stdout 打过非 JSON 内容：\n" + j)
        return "\n".join(parts)

    def _timeout_reason(self, method: str, timeout: float) -> str:
        parts = [f"MCP {self.spec.name}.{method} 超时：{timeout:.0f}s 没等到回应。",
                 f"命令：{self.spec.display_command}",
                 "常见原因：服务端卡住（例如 COM/LabTalk 在等 GUI 响应）、"
                 "或它没有 flush stdout。"]
        j = self.junk_tail()
        if j:
            parts.append("服务端往 stdout 打过非 JSON 内容（可能是它在用 stdout 打日志）：\n" + j)
        t = self.stderr_tail()
        if t:
            parts.append("服务端 stderr 末尾：\n" + t)
        return "\n".join(parts)

    # ── 协议方法 ────────────────────────────────────────────
    def list_tools(self, timeout: float | None = None) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(50):                       # 分页护栏，防服务端游标不收敛
            params = {"cursor": cursor} if cursor else {}
            res = self._request("tools/list", params, timeout=timeout)
            for t in res.get("tools") or []:
                if isinstance(t, dict) and t.get("name"):
                    out.append(t)
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return out

    def call(self, tool: str, args: dict | None = None, timeout: float | None = None) -> str:
        """调一个工具。**失败一定抛**，绝不返回"看起来成功"的字符串。"""
        res = self._request("tools/call", {"name": tool, "arguments": args or {}}, timeout=timeout)
        text = _content_text(res)
        if res.get("isError"):
            raise MCPError(
                f"MCP 服务端 {self.spec.name}.{tool} 报错（isError=true）：\n{text or '（无文本内容）'}")
        why = _payload_failure(text, self.spec.ok_field)
        if why:
            raise MCPError(
                f"MCP 服务端 {self.spec.name}.{tool} 在**协议层是成功的**"
                f"（isError=false），但载荷里写着失败：{why}\n"
                f"⚠️ 这正是最危险的一类：如果原样当成功返回，ToolCall.ok 会是 True，"
                f"假证据就进了证据门。所以这里按失败抛。\n"
                f"原始载荷：{text[:2000]}")
        return text

    # ── 关闭 ────────────────────────────────────────────────
    def stop(self, grace: float = 3.0) -> None:
        p = self._proc
        self._proc = None
        self._dead.set()
        for q in list(self._waiters.values()):
            try:
                q.put({"__dead__": True})
            except Exception:
                pass
        if p is None:
            return
        try:
            if p.stdin is not None:
                try:
                    p.stdin.close()
                except Exception:
                    pass
            try:
                p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(timeout=2)
                    except Exception:
                        pass
        except Exception:
            pass

    def diag(self) -> dict:
        return {
            "server": self.spec.name,
            "command": self.spec.display_command,
            "pid": self.pid,
            "alive": self.alive,
            "protocol": self.protocol,
            "server_info": self.server_info,
            "notices": list(self.notices),
            "origin": self.spec.origin,
        }


def _version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:
        return "?"


def _content_text(res: dict) -> str:
    """把 MCP 的 content 块拼成文本。"""
    parts: list[str] = []
    for blk in res.get("content") or []:
        if isinstance(blk, dict):
            t = blk.get("type")
            if t == "text":
                parts.append(str(blk.get("text") or ""))
            elif t == "resource":
                r = blk.get("resource") or {}
                parts.append(str(r.get("text") or r.get("uri") or ""))
            else:
                parts.append(json.dumps(blk, ensure_ascii=False))
        else:
            parts.append(str(blk))
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _payload_failure(text: str, field_name: str) -> str:
    """载荷里写着失败吗？返回失败原因；不是失败返回 ""。

    两条口径，理由各不相同：

    **(甲) 自述字段**（默认 `ok`）**是字面 false** —— 认；是别的（含 true / 0 / "" / null）
        一律**不认**。宁可漏认也不能误认：误报会把正常结果说成失败
        （"误报比漏报更伤"，设计定律 10）。
        ★ 有自述且自述不指向失败 ⇒ **以自述为准，直接收工**，不看别的键。

    **(乙) 没有自述字段时，顶层 `error` 是**非空字符串** —— 认。**
        这条是 2026-09-21 补的，来自实测：`vendor/ppt-mcp`（上游 Office-PowerPoint
        MCP Server v2.0.7）**149 处**失败返回全是裸 `{"error": "..."}`，
        既不带 `ok`，`isError` 也仍是 false ⇒ 甲条收不到它，
        于是「协议层成功 + 载荷写着失败」会滑成 `ToolCall.ok=True`，
        **假证据直接进证据门** —— 正是本文件 `call()` 里点名的那类最危险情形。
        判据刻意收得极窄：**只认 `str` 且 strip 后非空**。
        `null` / `""` / `false` / `0` / 对象 / 数组 一律不算，
        所以 `{"ok": true, "error": null}`、`{"error": ""}` 这类正常载荷不会被误读。
        为什么敢认这个键：在 JSON 载荷里 `error` **只可能表示失败**，
        没有哪种成功返回会把 error 填一句非空的话（上游成功的载荷用 `success`）。

    真要用别的口径，把 spec.ok_field 设成别的字段名，或设空关掉
    （设空 = "我不想判载荷"，这时甲乙两条一起关）。
    """
    if not field_name:
        return ""
    s = (text or "").strip()
    if not s or s[0] not in "{[":
        return ""
    try:
        d = json.loads(s)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    if field_name in d:
        if d[field_name] is False:
            for k in ("error", "msg", "message", "reason", "detail"):
                v = d.get(k)
                if v:
                    return str(v)
            return f"{field_name}=false（服务端没给原因）"
        return ""
    v = d.get("error")
    if isinstance(v, str) and v.strip():
        return v
    return ""


# ══════════════════════ Hub ══════════════════════
def _is_mutating(spec: MCPServerSpec, name: str, tool_def: dict) -> bool:
    """这个 MCP 工具会不会改动外部世界？

    **判定只此一处**（避免"两处真相"）。优先级：
      1. 配置 readonly 里点名      → 不改
      2. 配置 mutating 里点名      → 改
      3. 服务端自己标 readOnlyHint → 不改
      4. 其余                      → **按"改"处理**（保守）
    第 4 条为什么保守：围栏只在 mutating 时检查路径参数。把未知的当"不改"，
    等于给它开一条不受检查的写入通道；反过来把只读的当"会改"，
    代价只是多检查一次路径（可能被误拦 → 用 readonly 列表修）。
    在"可能写坏文件"与"可能误拦一次读"之间，选前者。
    """
    ro = set(spec.readonly or [])
    mu = set(spec.mutating or [])
    if name in ro:
        return False
    if name in mu:
        return True
    ann = tool_def.get("annotations")
    if isinstance(ann, dict) and ann.get("readOnlyHint") is True:
        return False
    return True


def _should_rewrite(v: str) -> bool:
    """这个值该被解析成绝对路径吗？

    **拿不准就不改写** —— 因为子进程的 cwd 已经被钉在工作区了（见 spec.cwd），
    不改写的代价很小：服务端按自己的 cwd 也能把相对路径落到工作区。
    而改错的代价是把参数弄坏，且悄无声息。两害相权取轻。

    ⚠️ 注意这里**不能直接用 hooks.looks_like_path 当判据**。
    那个函数的取向是"宁可误判为路径也不漏判"—— 它对**围栏**是对的
    （多检查一次很便宜），对**改写器**就是错的（多改一次会改坏数据）。
    实测：`一段带 / 的中文说明文字` 与 `10.1038/s41586-024` 都能骗过它。
    所以本函数在它之上再加两道：拒空白、拒 URL/DOI。

    代价（明说）：**含空格的路径不会被改写**（如 `my fig.png`）。
    这类路径靠 cwd 钉死仍能落到工作区；若真需要，把参数名加进
    spec.path_args 也解决不了 —— 那是"值"的问题不是"名"的问题。
    """
    if not v or len(v) > 260 or any(c.isspace() for c in v):
        return False
    if _URL_LIKE.match(v) or _DOI_LIKE.match(v):
        return False
    if looks_like_path(v):
        return True
    # 无分隔符但明显是文件名（fig.png / 图1.png）
    return bool(_HAS_EXT.search(v))


def _resolve_path_args(args: dict | None, spec: MCPServerSpec,
                       workspace: Path | None = None) -> tuple[dict, list[dict]]:
    """把"产出路径"参数解析成绝对路径 —— **只解析，不裁决**。

    裁决（越界就拒）只有 `hooks.WorkspaceFence` 一处。这里若也拦一刀，
    同一条规则就有两个实现，迟早漂成两个口径。

    为什么要解析成绝对路径（光有 cwd 钉死还不够）：
      服务端**自己**写文件时，相对路径按它的 cwd 走，没问题。
      但像 Origin 那种把路径交给**另一个进程**（COM/LabTalk）去写的，
      相对路径会按 Origin 自己的 cwd 解析 —— 那就跑到别处去了。
      所以把绝对路径喂给它，才是稳的。

    返回 (新参数, 改写记录)。改写记录要给人看得见 —— 悄悄改参数也是静默失败。
    """
    if not args:
        return dict(args or {}), []
    keys = set(spec.path_args) if spec.path_args else DEFAULT_PATH_ARGS
    base = Path(workspace) if workspace else Path.cwd()
    out = dict(args)
    rewrites: list[dict] = []
    for k, v in list(args.items()):
        if k not in keys or not isinstance(v, str) or not v.strip():
            continue
        if not _should_rewrite(v):
            continue
        p = Path(os.path.expanduser(v))
        if p.is_absolute():
            continue
        ap = (base / p).resolve()
        out[k] = str(ap)
        rewrites.append({"arg": k, "from": v, "to": str(ap)})
    return out, rewrites


class MCPHub:
    """一批 MCP 服务端的集合。懒启动：只有真要用才 spawn。

    为什么懒启动：`Rongke.__init__` 时还不知道本次任务的 workspace，
    而子进程 cwd 要钉在那个 workspace 上。真正的调用发生在 run() 期间，
    那时 cwd 已经是工作区（loop._run_serial 里 os.chdir(ws)）。
    """

    def __init__(self, specs: list[MCPServerSpec] | None = None,
                 workspace: Path | None = None):
        self.specs: dict[str, MCPServerSpec] = {}
        self.skipped: list[dict] = []
        for s in specs or []:
            if s.disabled:
                self.skipped.append({"server": s.name,
                                     "reason": f"配置里标了 disabled（来自 {s.origin or '?'}）"})
                continue
            if s.name in self.specs:
                self.skipped.append({"server": s.name, "reason": "服务端重名，后者被忽略"})
                continue
            self.specs[s.name] = s
        self.workspace = workspace
        self._clients: dict[str, MCPClient] = {}
        self.tools: dict[str, list[dict]] = {}      # server -> tools/list 原始定义
        self.errors: dict[str, str] = {}            # server -> 起不来/列不出工具的原因
        self.rewrites: list[dict] = []              # 路径改写记录（可审计）

    # ── 连接 ────────────────────────────────────────────────
    def client(self, name: str) -> MCPClient:
        spec = self.specs.get(name)
        if spec is None:
            raise MCPError(f"没有这个 MCP 服务端：{name}（可用：{', '.join(sorted(self.specs)) or '无'}）")
        c = self._clients.get(name)
        if c is None:
            c = MCPClient(spec, workspace=self.workspace)
            self._clients[name] = c
        else:
            # workspace 是**任务级**的（每个任务一个工作区），随时可能已换。
            # 不同步的话：连接一旦重起，进程会带着**上一个任务的 cwd** ——
            # 服务端按相对路径写出来的产物就落进了上一个任务，且毫无报错。
            # （实测踩过：这条正是靠 9.1/9.2 那对正负控抓出来的。）
            c.workspace = self.workspace
        if not c.alive:
            c.start()               # 起不来就抛，由调用方决定怎么报
        return c

    def probe(self, timeout: float | None = None, stop_after: bool = False) -> dict:
        """把每个服务端起一次、列出工具。失败的记进 errors，**不抛** ——
        一个服务端挂了不该让内核起不来。但失败必须能被上层看见。
        """
        for name in self.specs:
            self.probe_one(name, timeout=timeout, stop_after=stop_after)
        return self.diag()

    def probe_one(self, name: str, timeout: float | None = None,
                  stop_after: bool = False) -> list[dict]:
        """探一个服务端。

        `stop_after=True` 用于**装配期**：那时还不知道本次任务的 workspace，
        子进程 cwd 只能钉在项目根。探完就关掉，等真正要调用时再按
        正确的工作区重新起 —— 否则会留一个 cwd 钉错地方的进程在那儿。
        """
        try:
            c = self.client(name)
            tools = c.list_tools(timeout=timeout)
            self.tools[name] = tools
            self.errors.pop(name, None)
        except MCPError as e:
            tools = []
            self.tools[name] = []
            self.errors[name] = str(e)
        except Exception as e:
            tools = []
            self.tools[name] = []
            self.errors[name] = f"{type(e).__name__}: {e}"
        if stop_after:
            try:
                c = self._clients.get(name)
                if c is not None:
                    c.stop()
            except Exception:
                pass
        return tools

    # ── 调用 ────────────────────────────────────────────────
    def call(self, server: str, tool: str, args: dict | None = None,
             timeout: float | None = None) -> str:
        spec = self.specs.get(server)
        if spec is None:
            raise MCPError(f"没有这个 MCP 服务端：{server}")
        new_args, rewrites = _resolve_path_args(args, spec, self.workspace)
        for r in rewrites:
            self.rewrites.append({"server": server, "tool": tool, **r})
        return self.client(server).call(tool, new_args, timeout=timeout)

    # ── 关闭 ────────────────────────────────────────────────
    def stop_all(self) -> int:
        n = 0
        for c in list(self._clients.values()):
            try:
                c.stop()
                n += 1
            except Exception:
                pass
        return n

    def diag(self) -> dict:
        return {
            "specs": [s.as_dict() for s in self.specs.values()],
            "skipped": list(self.skipped),
            "errors": dict(self.errors),
            "tool_counts": {k: len(v) for k, v in self.tools.items()},
            "clients": [c.diag() for c in self._clients.values()],
            "rewrites": list(self.rewrites[-20:]),
        }


# ══════════════════════ 注册进内核 ══════════════════════
def install_mcp(registry: ToolRegistry, hub: MCPHub,
                allow: list[str] | None = None,
                deny: list[str] | None = None) -> dict:
    """把 MCP 服务端的工具注册进内核。

    返回 {"installed": [...], "skipped": [...], "servers": {...}}。

    两种"不装"都必须**报出来**，不能静默跳过：
      * 服务端起不来 / 列不出工具 → 记 skipped，附原因
      * 工具重名（例如 paper-workbench 已被 bridge.py 同进程反射）→ 记 skipped
        这是真实会发生的：同一个 workbench 既同进程反射又走 MCP，21 个名字全撞。
    """
    allow_set = set(allow) if allow else None
    deny_set = set(deny or [])
    installed: list[str] = []
    skipped: list[dict] = []
    servers: dict[str, dict] = {}

    for server, spec in hub.specs.items():
        tools = hub.tools.get(server)
        if tools is None:
            # stop_after=True：装配期还不知道本次任务的 workspace，
            # 探完就关，等真调用时按正确的工作区再起。
            tools = hub.probe_one(server, stop_after=True)
        if not tools:
            why = hub.errors.get(server) or "服务端没有列出任何工具"
            skipped.append({"server": server, "reason": why})
            servers[server] = {"installed": 0, "reason": why}
            continue

        n_ok = 0
        for td in tools:
            name = str(td.get("name") or "").strip()
            if not name:
                continue
            if allow_set is not None and name not in allow_set:
                continue
            if name in deny_set:
                skipped.append({"server": server, "tool": name, "reason": "配置 deny 里点名"})
                continue

            existing = registry.get(name)
            if existing is not None:
                skipped.append({
                    "server": server, "tool": name,
                    "reason": (f"工具重名：{name} 已被 {existing.source} 注册，MCP 版本不覆盖它"
                               f"（同名两个来源会出现「两处真相」，宁可只留一个）"),
                })
                continue

            schema = td.get("inputSchema") or td.get("input_schema") or {}
            if not isinstance(schema, dict):
                schema = {}
            desc = str(td.get("description") or "").strip()
            registry.register(Tool(
                name=name,
                description=(desc or f"[MCP:{server}] {name}"),
                parameters=schema,
                fn=_make_caller(hub, server, name),
                domain=spec.domain,
                mutating=_is_mutating(spec, name, td),
                source="mcp",
            ))
            installed.append(name)
            n_ok += 1
        servers[server] = {"installed": n_ok, "tools_total": len(tools)}

    return {"installed": installed, "skipped": skipped, "servers": servers}


def _make_caller(hub: MCPHub, server: str, tool: str):
    """闭包：模型调它 → hub 去调 MCP。

    **故意不吞异常**：MCPError 会被 ToolRegistry.call 捕获，
    于是 ToolCall.ok=False、错误原文进 observation。
    这才是对的 —— 吞掉异常就等于给证据门喂假证据。
    """

    def _call(**kwargs: Any) -> str:
        return hub.call(server, tool, kwargs)

    _call.__name__ = f"mcp_{tool}"
    _call.__doc__ = f"MCP 工具 {server}.{tool}"
    return _call
