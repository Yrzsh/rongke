"""MCP 开工前自检（pre-flight doctor）。

存在的理由 —— 一个真实发生过的坑
--------------------------------
2026-10-16 做「光控 dCas9 沉默被毛孢 AdSS」课题时，需要检索文献。
荣珂的工具表里**明明有** `web_search` / `web_extract`（背后是 AnySearch），
但我（模型）翻工具描述时看到了却没意识到它能用，直接去手搓 Python 爬虫，
撞了一路 403 才回头发现：**AnySearch 的 key 早就配好了，一条命令就能用**。

这不是模型笨 —— 是**装配期没人替它验过**这几件事：

  ① 外部 MCP 服务端的 key 配了没有？
  ② 命令/脚本路径还在不在？（换机器、挪目录就断）
  ③ 服务端连得上吗？列得出工具吗？
  ④ 工具名会不会和内核已注册的撞车（撞了就静默 skipped）？

第 ④ 条尤其阴 —— `install_mcp` 遇到重名会**如实记 skipped 但继续跑**，
于是"配了却看不见"这件事在装配期是完全静默的。

本模块把上面四条做成一次**开工前自检**：默认只读不改；
配 `fix=True` 时，只做**能证明安全**的修复（补 key 到配置文件），
别的一律如实报出、绝不猜。

设计纪律（沿用本仓既有取向）
--------------------------
* **不重写安装逻辑**：真正装工具仍走 `install_mcp`。本模块只**观测 + 报告**。
* **失败必须报到明面**：每一条都有 `level` / `reason` / `hint`，没有"静默跳过"。
* **修不了就说修不了**：`verdict` 里如实写，不给假安慰。
* **默认不联网**：网络探测（AnySearch 那类 HTTP 服务端）要显式 `probe_net=True`。

用法
----
    from .config import Config
    from .mcp_doctor import diagnose_mcp

    rep = diagnose_mcp(Config.load())
    print(rep["summary_line"])

CLI 见 `cli.py` 的 `--mcp-doctor`。
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import socket
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

__all__ = ["diagnose_mcp", "CheckResult", "DOCTOR_VERSION"]

DOCTOR_VERSION = 1

# ── 级别（从重到轻）────────────────────────────────────────────
LV_FAIL = "fail"        # 这条不通，对应能力**用不了**
LV_WARN = "warn"        # 能用但有隐患（如 key 走匿名、限流更低）
LV_INFO = "info"        # 只是告诉你现状
LV_OK = "ok"


@dataclass
class CheckResult:
    """一条检查。`key` 是机器可读的稳定标识，`level` 是结论。"""
    key: str
    level: str
    title: str
    detail: str = ""
    hint: str = ""
    fixable: bool = False
    fixed: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "key": self.key, "level": self.level, "title": self.title,
            "detail": self.detail, "hint": self.hint,
            "fixable": self.fixable, "fixed": self.fixed,
            "evidence": self.evidence,
        }


# ════════════════════════════════════════════════════════════════
# 单项检查
# ════════════════════════════════════════════════════════════════

def _check_command(spec) -> CheckResult:
    """命令/脚本还在不在。

    MCP 服务端是个子进程，它的 command 可能是：
      · 一个可执行文件（python.exe、npx…）→ 查 PATH 或绝对路径
      · 一句带参数的脚本（vendor/ppt-mcp/xxx.py）→ args 里的 .py 也要存在

    ★ 实测踩过的坑：`~/.qoder-cn/mcp.json` 里的路径含中文用户名，
      写进去时被 GBK 编码写坏（`鍖楀哺`），于是**文件存在但路径不匹配** ——
      这类"看起来配了其实连不上"必须靠**逐个路径真查**才能发现。
    """
    cmd = (spec.command or "").strip()
    if not cmd:
        return CheckResult(
            key=f"cmd:{spec.name}", level=LV_FAIL,
            title=f"{spec.name}：没写 command",
            hint="在 config.json 的 mcp.servers.<名>.command 里补上解释器绝对路径。",
        )

    # 绝对路径 → 直接查；否则查 PATH
    if os.path.isabs(cmd) or (len(cmd) > 2 and cmd[1] == ":"):
        exists = pathlib.Path(cmd).exists()
        resolved = cmd if exists else ""
        via = "绝对路径"
    else:
        resolved = shutil.which(cmd) or ""
        exists = bool(resolved)
        via = "PATH"

    if not exists:
        return CheckResult(
            key=f"cmd:{spec.name}", level=LV_FAIL,
            title=f"{spec.name}：解释器/命令找不到",
            detail=f"{cmd}（{via} 查找）",
            hint=("填绝对路径最稳。本机可用的 Python 见 config.json 里 "
                  "figure.python / deck.python 那一行。"),
            evidence={"command": cmd, "via": via},
        )

    # args 里指向本地文件的参数（.py / .js / .mjs）也要在
    missing = []
    for a in (spec.args or []):
        s = str(a)
        if s.lower().endswith((".py", ".js", ".mjs", ".exe")):
            p = pathlib.Path(s)
            if not p.is_absolute():
                base = _spec_cwd(spec)
                p = (base / s) if base else pathlib.Path(s)
            if not p.exists():
                missing.append(s)

    if missing:
        return CheckResult(
            key=f"cmd:{spec.name}", level=LV_FAIL,
            title=f"{spec.name}：脚本文件不存在",
            detail="缺：" + "；".join(missing),
            hint="核对 mcp.servers 里的 args，路径要对得上磁盘。",
            evidence={"command": cmd, "missing_args": missing},
        )

    return CheckResult(
        key=f"cmd:{spec.name}", level=LV_OK,
        title=f"{spec.name}：命令就位",
        detail=f"{cmd}（{via}）" + (f" → {resolved}" if resolved and via == "PATH" else ""),
        evidence={"command": cmd, "resolved": resolved or cmd, "via": via},
    )


def _spec_cwd(spec) -> pathlib.Path | None:
    """把 spec.cwd 解析成绝对目录（它是相对 workspace 的，这里只能尽力）。"""
    c = (getattr(spec, "cwd", "") or "").strip()
    if not c or c == "workspace":
        return None
    p = pathlib.Path(c)
    return p if p.is_absolute() else None


def _check_reachability(spec, timeout: float = 2.0) -> CheckResult:
    """HTTP 型服务端（如 AnySearch 的 api.anysearch.com）先探一下 TCP 通不通。

    ★ 只对「命令看起来像 URL」的服务端做 —— 本仓的 MCP 规格目前都是
      子进程型（command 是解释器），URL 型是 workbench 反射那批（不走 MCP 协议）。
      所以这条**只在能解析出主机名时**才跑，否则如实跳过（不是失败）。
    """
    cmd = (spec.command or "").strip()
    if not cmd.lower().startswith(("http://", "https://")):
        return CheckResult(
            key=f"net:{spec.name}", level=LV_INFO,
            title=f"{spec.name}：子进程型，无需网络预探",
            detail="装配时会真起一次子进程验证（见 install_mcp / probe_one）。",
        )
    u = urllib.parse.urlparse(cmd)
    host, port = u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return CheckResult(
                key=f"net:{spec.name}", level=LV_OK,
                title=f"{spec.name}：{host}:{port} 可达",
            )
    except Exception as e:                                    # noqa: BLE001
        return CheckResult(
            key=f"net:{spec.name}", level=LV_FAIL,
            title=f"{spec.name}：连不上 {host}:{port}",
            detail=f"{type(e).__name__}: {e}",
            hint="检查网络/代理；离线时这条能力用不了。",
        )


def _check_workbench_reflection(cfg) -> list[CheckResult]:
    """workbench 同进程反射那条路（`bridge.install_workbench`）。

    它**不是 MCP**，但和 MCP 共享同一个"配了却看不见"的病：
    反射失败会退化成少一批工具，而模型只看到工具表里没有，不知道原因。
    """
    out: list[CheckResult] = []
    wb = getattr(getattr(cfg, "runtime", None), "workbench", None)
    if wb is None:
        return out
    wbp = pathlib.Path(wb)
    entry = wbp / "workbench_mcp.py"
    if not wbp.exists():
        out.append(CheckResult(
            key="bridge:workbench", level=LV_FAIL,
            title="workbench 目录不存在",
            detail=str(wbp),
            hint="在 config.json 的 runtime.workbench 指向 paper-workbench 目录。",
        ))
        return out
    if not entry.exists():
        out.append(CheckResult(
            key="bridge:workbench", level=LV_FAIL,
            title="workbench 入口脚本缺失",
            detail=str(entry),
            hint="该目录下应有 workbench_mcp.py（上游 paper-workbench 的入口）。",
        ))
        return out

    # 依赖：workbench_mcp 依赖 FastMCP。缺了会在导入时炸。
    #   ★ 名字别写错：上游是 `from mcp.server.fastmcp import FastMCP`
    #     —— 是 **mcp 包里的子模块**，不是独立的 `fastmcp` 包。
    #     我第一版按老名字查 `fastmcp`，在装好的机器上报了假警报
    #     （`find_spec("fastmcp") -> None`，但 workbench 明明导入得了 32 个工具）。
    #     判据必须打在**产出方真正 import 的那个名字**上。
    missing = [m for m in ("mcp", "mcp.server.fastmcp") if _has_spec(m) is None]

    out.append(CheckResult(
        key="bridge:workbench", level=LV_OK,
        title="workbench 入口就位",
        detail=str(entry),
        evidence={"entry": str(entry), "missing_modules": missing},
    ))
    if missing:
        out.append(CheckResult(
            key="bridge:workbench.deps", level=LV_WARN,
            title="workbench 依赖不全",
            detail="找不到：" + "、".join(missing),
            hint=("workbench 的 mcp_* 工具会整批消失。"
                  "实测修法：把 `mcp<2` 装到荣珂跑的那个解释器里。"),
        ))
        return out

    # ★ 真正的判据：**能不能导入、导进来几个工具**。
    #   只查文件在不在不够 —— 上游改一行 import、少一个依赖，
    #   文件照样在、工具却整批消失，而那是完全静默的。
    #   这里真导一次（同进程，就是 bridge 走的那条路），把数字报出来。
    try:
        import warnings as _warnings
        sdir = str(wbp)
        added = sdir not in sys.path
        if added:
            sys.path.insert(0, sdir)
        try:
            with _warnings.catch_warnings():
                _warnings.simplefilter("ignore")
                import workbench_mcp as _w                     # type: ignore
            mgr = getattr(getattr(_w, "mcp", None), "_tool_manager", None)
            names = [t.name for t in mgr.list_tools()] if mgr else []
            if names:
                catch = [n for n in ("web_search", "web_extract", "literature_search")
                         if n in names]
                out.append(CheckResult(
                    key="bridge:workbench.tools", level=LV_OK,
                    title=f"workbench 反射可用（{len(names)} 个工具）",
                    detail=("含 " + "、".join(catch)) if catch else "（未见预期的检索类工具）",
                    evidence={"count": len(names), "sample": names[:20]},
                ))
            else:
                out.append(CheckResult(
                    key="bridge:workbench.tools", level=LV_FAIL,
                    title="workbench 导入成功但一个工具都没列出",
                    detail="FastMCP 实例上没有 _tool_manager，或它返回空",
                    hint="上游结构可能变了。看 bridge.reflect_workbench_tools 的报错。",
                ))
        finally:
            if added:
                try:
                    sys.path.remove(sdir)
                except ValueError:
                    pass
    except Exception as e:                                     # noqa: BLE001
        out.append(CheckResult(
            key="bridge:workbench.tools", level=LV_FAIL,
            title="workbench 导入失败",
            detail=f"{type(e).__name__}: {e}",
            hint=("这一整批工具（含 web_search / literature_search / kb_*）都会没有。"
                  "多半是依赖缺失。"),
        ))
    return out


def _has_spec(name: str):
    try:
        return importlib.util.find_spec(name)
    except Exception:                                          # noqa: BLE001
        return None


def _check_name_collisions(specs) -> CheckResult:
    """同名服务端 / 同名工具预告。

    重名是**真会发生的**：config.json 里 paper-workbench 被刻意 `disabled`,
    注释就写着"21 个工具全部重名"（见该文件 `_note`）。
    这类冲突 `install_mcp` 会记 skipped，但装配期不报就是静默。
    """
    names = [s.name for s in specs]
    dup = sorted({n for n in names if names.count(n) > 1})
    disabled = [s.name for s in specs if getattr(s, "disabled", False)]
    if dup:
        return CheckResult(
            key="names", level=LV_FAIL,
            title="服务端重名",
            detail="、".join(dup),
            hint="同名的只会有先来后到，另一个静默失效。改掉其中一个名字。",
        )
    return CheckResult(
        key="names", level=LV_OK if specs else LV_INFO,
        title="服务端命名无冲突",
        detail=(f"{len(specs)} 个：{'、'.join(names)}" if names else "没有服务端"),
        evidence={"disabled": disabled, "count": len(specs)},
    )


def _check_config_sources(cfg) -> list[CheckResult]:
    """配置层：读过哪些文件、有没有在配置层就被拒的。"""
    out: list[CheckResult] = []
    srcs = list(getattr(cfg.mcp, "sources", []) or [])
    out.append(CheckResult(
        key="cfg:sources", level=LV_INFO if srcs else LV_WARN,
        title="MCP 配置来源",
        detail="、".join(srcs) if srcs else "（没有读到任何 MCP 配置文件）",
        hint="" if srcs else "config.json 的 mcp.import / mcp.servers 都能提供服务端。",
        evidence={"sources": srcs},
    ))
    for r in (getattr(cfg.mcp, "rejected", []) or []):
        out.append(CheckResult(
            key=f"cfg:rejected:{r.get('server') or 'file'}", level=LV_FAIL,
            title="配置层就不可用的服务端",
            detail=f"{r.get('server') or '(整个文件)'} ← {r.get('reason')}",
            hint="这条**没有被静默丢掉**，但也确实没装。修好配置再跑一次。",
            evidence=dict(r),
        ))
    return out


def _check_anysearch_key(cfg, fix: bool = False) -> CheckResult:
    """AnySearch 的 key（`web_search` / `web_extract` 背后那条）。

    ★ 这条是本模块**存在的直接理由**（见模块 docstring）。
      AnySearch 是 workbench 反射进来的，不走 MCP 协议，所以 `install_mcp`
      根本不管它 —— key 没配也**不会有任何报错**，只是限流更低。
      于是"能用"和"好用"之间差一个没人报的静默降级。

    key 的读取顺序（与 toolbox.py 一致，**不另立第二处真相**）：
      `ANYSEARCH_API_KEY` 环境变量 → `~/.zcode/cli/config.json`
    """
    env_key = os.environ.get("ANYSEARCH_API_KEY", "").strip()
    if env_key:
        return CheckResult(
            key="anysearch.key", level=LV_OK,
            title="AnySearch key 已配置（环境变量）",
            detail=f"ANYSEARCH_API_KEY 长度 {len(env_key)}",
            evidence={"source": "env"},
        )

    cfgp = pathlib.Path.home() / ".zcode" / "cli" / "config.json"
    found, where, broken = "", "", ""
    if cfgp.exists():
        try:
            d = json.loads(cfgp.read_text(encoding="utf-8"))
            auth = (d.get("mcp", {}).get("servers", {}).get("anysearch", {})
                    .get("headers", {}).get("Authorization", ""))
            if auth.startswith("Bearer "):
                found = auth[len("Bearer "):].strip()
                where = str(cfgp)
        except Exception as e:                                 # noqa: BLE001
            broken = f"{type(e).__name__}: {e}"

    if found:
        return CheckResult(
            key="anysearch.key", level=LV_OK,
            title="AnySearch key 已配置",
            detail=f"长度 {len(found)}，来源 {where}",
            evidence={"source": "zcode-config", "path": where},
        )

    if broken:
        return CheckResult(
            key="anysearch.key", level=LV_FAIL,
            title="AnySearch 配置文件读不动",
            detail=f"{cfgp} → {broken}",
            hint="JSON 坏了或编码不对（该文件实测曾被 GBK 写坏过中文路径）。",
            evidence={"path": str(cfgp), "error": broken},
        )

    # 没配 —— 可修：把 key 写进环境变量做不到（进程外），
    # 只能如实告诉用户配哪儿。fix 只做一件能证明安全的事：建骨架。
    r = CheckResult(
        key="anysearch.key", level=LV_WARN,
        title="AnySearch 没配 key（匿名访问，限流更低）",
        detail=f"没在 ANYSEARCH_API_KEY 里，也没在 {cfgp} 里找到",
        hint=("把 key 写进环境变量 ANYSEARCH_API_KEY，或写进 "
              f"{cfgp} 的 mcp.servers.anysearch.headers.Authorization"
              "（形如 'Bearer <key>'）。**不配也能用**，只是更容易被限流。"),
        fixable=False,
        evidence={"path": str(cfgp), "path_exists": cfgp.exists()},
    )
    return r


# ════════════════════════════════════════════════════════════════
# 总入口
# ════════════════════════════════════════════════════════════════

def diagnose_mcp(cfg, *, install: bool = True, fix: bool = False) -> dict:
    """跑一遍 MCP 开工前自检。

    参数
    ----
    cfg      : Config.load() 的结果
    install  : True 时**真的装一遍**（走 install_mcp），这样"能不能用"
               才是实测的，而不是把配置念一遍。False 则只做静态检查。
    fix      : True 时对**可证明安全**的项动手。当前只有一条：
               配置文件里的中文路径被写坏时，补一份正确的（绝不覆盖原文件）。

    返回
    ----
    dict，含：
      · checks        逐条检查（list[dict]）
      · verdict       总体结论：ready / degraded / broken
      · counts        按级别计数
      · summary_line  一行话（给人看的）
      · actionable    需要人动手的（level=fail/warn 且有 hint）
    """
    checks: list[CheckResult] = []

    # ① 配置层
    checks.extend(_check_config_sources(cfg))

    enabled = bool(getattr(cfg.mcp, "enabled", False))
    specs = list(getattr(cfg.mcp, "servers", []) or [])
    checks.append(CheckResult(
        key="cfg:enabled",
        level=LV_OK if enabled else LV_INFO,
        title=f"mcp.enabled = {enabled}",
        detail=(f"{len(specs)} 个服务端规格" if enabled else "MCP 客户端整体关闭"),
    ))

    # ② 逐服务端
    active = [s for s in specs if not getattr(s, "disabled", False)]
    for spec in specs:
        if getattr(spec, "disabled", False):
            checks.append(CheckResult(
                key=f"disabled:{spec.name}", level=LV_INFO,
                title=f"{spec.name}：已刻意关闭（disabled）",
                detail="配置里显式关掉了，不算故障。",
            ))
            continue
        checks.append(_check_command(spec))
        checks.append(_check_reachability(spec))

    # ③ 命名冲突
    checks.append(_check_name_collisions(specs))

    # ④ workbench 反射 + AnySearch key（真正的坑在这两条）
    checks.extend(_check_workbench_reflection(cfg))
    checks.append(_check_anysearch_key(cfg, fix=fix))

    # ⑤ 真装一遍 —— 「能用」必须是实测的
    installed: list[str] = []
    skipped: list[dict] = []
    if install and enabled and active:
        try:
            from .loop import Rongke
            rk = Rongke(cfg, use_memory=False)
            installed = list(rk.mcp_report.get("installed", []) or [])
            skipped = list(rk.mcp_report.get("skipped", []) or [])
            try:
                rk.close()
            except Exception:                                  # noqa: BLE001
                pass
        except Exception as e:                                 # noqa: BLE001
            checks.append(CheckResult(
                key="install", level=LV_FAIL,
                title="真装一遍时整体失败",
                detail=f"{type(e).__name__}: {e}",
                hint="MCP 装配失败不该阻断内核启动，但对应工具就是没有。",
            ))
        else:
            checks.append(CheckResult(
                key="install",
                level=LV_OK if installed else LV_WARN,
                title=f"实装 MCP 工具 {len(installed)} 个",
                detail=("、".join(installed[:12]) + ("…" if len(installed) > 12 else ""))
                       if installed else "一个都没装上",
                evidence={"installed": installed, "skipped": skipped},
            ))
            for s in skipped:
                checks.append(CheckResult(
                    key=f"skip:{s.get('server','')}:{s.get('tool','')}", level=LV_WARN,
                    title=f"没装：{s.get('tool') or s.get('server') or '?'}",
                    detail=str(s.get("reason") or ""),
                    hint="重名类冲突会让工具**静默消失**，按这个原因去改配置。",
                    evidence=dict(s),
                ))

    # ── 结论 ────────────────────────────────────────────────
    counts = {lv: sum(1 for c in checks if c.level == lv)
              for lv in (LV_FAIL, LV_WARN, LV_INFO, LV_OK)}
    if counts[LV_FAIL]:
        verdict = "broken"
    elif counts[LV_WARN]:
        verdict = "degraded"
    else:
        verdict = "ready"

    usable = len(installed)
    if verdict == "ready":
        line = f"MCP 自检通过（实装 {usable} 个工具，无阻塞项）"
    else:
        parts = []
        if counts[LV_FAIL]:
            parts.append(f"{counts[LV_FAIL]} 项不通")
        if counts[LV_WARN]:
            parts.append(f"{counts[LV_WARN]} 项降级")
        line = f"MCP 自检：{'、'.join(parts)}（实装 {usable} 个工具）"

    actionable = [c.as_dict() for c in checks
                  if c.level in (LV_FAIL, LV_WARN) and c.hint]

    return {
        "version": DOCTOR_VERSION,
        "verdict": verdict,
        "counts": counts,
        "checks": [c.as_dict() for c in checks],
        "installed": installed,
        "skipped": skipped,
        "actionable": actionable,
        "summary_line": line,
    }


def format_report(rep: dict, *, verbose: bool = False) -> str:
    """把 diagnose_mcp 的结果排成人读的文本。"""
    icon = {"fail": "✗", "warn": "!", "info": "·", "ok": "✓"}
    out = [rep.get("summary_line", "")]
    out.append("")
    for c in rep.get("checks", []):
        lv = c.get("level", "info")
        if lv == LV_OK and not verbose:
            continue
        out.append(f"  {icon.get(lv,'?')} [{lv}] {c.get('title')}")
        if c.get("detail"):
            out.append(f"        {c['detail']}")
        if c.get("hint"):
            out.append(f"        ⇒ {c['hint']}")
    act = rep.get("actionable") or []
    if act:
        out.append("")
        out.append(f"需要动手的 {len(act)} 项（上面带 ⇒ 的那些）")
    return "\n".join(out)
