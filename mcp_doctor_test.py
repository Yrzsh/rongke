# -*- coding: utf-8 -*-
"""荣珂 MCP 开工前自检（mcp_doctor）自测。

验证思路（和项目里其它测试一致）：
  * **正控** —— 对**本机真配置**跑一遍，确认能报出真话（key 在哪、几个工具）。
  * **负控** —— 每一种坏法都要能被**看见**。这是本文件的重点：
      - 命令不存在            → 必须 fail（不是静默 ok）
      - 脚本文件缺失          → 必须 fail
      - 服务端重名            → 必须 fail
      - 配置层被拒            → 必须 fail
      - workbench 目录不存在  → 必须 fail
      - AnySearch 没 key      → 必须 warn（**不是 fail** —— 匿名也能用）
  * **承重自检** —— 把判据的机制**消掉**，对应用例必须变红。
    例如把"命令不存在"那条检查停掉，负控就不该再报 fail。

★ 本文件刻意**不联网**：`_check_reachability` 只对 http(s) 型 command 动手，
  而本仓的服务端规格全是子进程型 ⇒ 它天然走 info 分支。真网络探测在
  `--net` 下才跑。

用法：
    python mcp_doctor_test.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _tkit as T                                            # noqa: E402

from rongke.mcp_client import MCPServerSpec                  # noqa: E402
from rongke.mcp_doctor import (                              # noqa: E402
    LV_FAIL, LV_INFO, LV_OK, LV_WARN,
    _check_anysearch_key, _check_command, _check_config_sources,
    _check_name_collisions, _check_workbench_reflection,
    diagnose_mcp, format_report,
)

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def check_true(label, cond, extra=""):
    check(label + (f"  {extra}" if extra and not cond else ""), bool(cond), True)


# ════════════════════════════════════════════════════════════════
# 造配置的替身
# ════════════════════════════════════════════════════════════════

@dataclass
class _FakeMcp:
    enabled: bool = True
    servers: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    sources: list = field(default_factory=list)


@dataclass
class _FakeRuntime:
    workbench: str = ""


@dataclass
class _FakeCfg:
    mcp: _FakeMcp = field(default_factory=_FakeMcp)
    runtime: _FakeRuntime = field(default_factory=_FakeRuntime)


def cfg_with(specs=None, *, rejected=None, sources=None,
             workbench=None, enabled=True) -> _FakeCfg:
    """造配置替身。

    ★ `workbench` 默认取**本机真路径**，不是空串。
      第一版默认空串，于是 `pathlib.Path("")` 变成 `.`，
      `_check_workbench_reflection` 报「入口脚本缺失」——
      我在承重自检里被这条**无关的 fail** 干扰，看不出判据到底承不承重。
      替身的默认值必须是**真实且无关**的，否则负控量的是别的东西。
    """
    if workbench is None:
        workbench = str(Path.home() / ".dsh" / "papers" / "workbench")
    return _FakeCfg(
        mcp=_FakeMcp(enabled=enabled, servers=list(specs or []),
                     rejected=list(rejected or []),
                     sources=list(sources or [])),
        runtime=_FakeRuntime(workbench=workbench),
    )


def by_key(checks: list[dict], prefix: str) -> list[dict]:
    return [c for c in checks if c["key"] == prefix or c["key"].startswith(prefix)]


# ════════════════════════════════════════════════════════════════
# 1. 命令检查（负控重点）
# ════════════════════════════════════════════════════════════════
print("\n=== 1. 命令 / 脚本存在性 ===")

# 正控：真解释器存在
good_spec = MCPServerSpec(name="ok", command=sys.executable, args=["-c", "pass"])
r = _check_command(good_spec)
check("真解释器 → ok", r.level, LV_OK)

# 负控 A：命令根本不存在
bad_spec = MCPServerSpec(name="nope", command=r"C:\definitely\not\here\nope.exe")
r = _check_command(bad_spec)
check("不存在的命令 → fail", r.level, LV_FAIL)
check_true("  且给出了修法", bool(r.hint))

# 负控 B：解释器在、脚本没了（这是换机器/挪目录最常断的一种）
with T.scratch("doctor_", "mcp-doctor") as td:
    miss = td / "no_such_server.py"
    spec = MCPServerSpec(name="missScript", command=sys.executable, args=[str(miss)])
    r = _check_command(spec)
    check("脚本文件缺失 → fail", r.level, LV_FAIL)
    check_true("  原因里点了名", "no_such_server.py" in r.detail)

    # 承重自检：把"脚本存在"这条判据消掉，负控 B 必须不再 fail
    #   （做法：换一条命令走 PATH 分支 —— 它不该报「脚本文件」这一类）
    spec_path = MCPServerSpec(name="viacmd", command=sys.executable,
                              args=["-c", "pass"])
    r2 = _check_command(spec_path)
    check_true("承重自检：无脚本参数时不报『脚本文件不存在』",
               "脚本文件" not in r2.title)

    # 负控：目录在、入口脚本不在
    out = _check_workbench_reflection(cfg_with(workbench=str(td)))
    check("workbench 入口脚本缺失 → fail", out[0].level, LV_FAIL)
    check_true("  点名要 workbench_mcp.py", "workbench_mcp.py" in out[0].hint)

# 负控 C：命令为空
r = _check_command(MCPServerSpec(name="empty", command=""))
check("空 command → fail", r.level, LV_FAIL)


# ════════════════════════════════════════════════════════════════
# 2. 重名与配置层
# ════════════════════════════════════════════════════════════════
print("\n=== 2. 重名 / 配置层 ===")

r = _check_name_collisions([MCPServerSpec(name="a", command="x"),
                            MCPServerSpec(name="b", command="y")])
check("不同名 → ok", r.level, LV_OK)

r = _check_name_collisions([MCPServerSpec(name="dup", command="x"),
                            MCPServerSpec(name="dup", command="y")])
check("重名 → fail", r.level, LV_FAIL)

r = _check_name_collisions([])
check("没有服务端 → info（不是 fail）", r.level, LV_INFO)

# 配置层被拒：必须报 fail，且不静默
cfg = cfg_with(rejected=[{"server": "broken", "reason": "缺 command"}])
out = _check_config_sources(cfg)
bad = [c for c in out if c.level == LV_FAIL]
check("配置层被拒 → 有条 fail", len(bad), 1)
check_true("  原因带出来了", "缺 command" in bad[0].detail)

# 没有配置来源 → warn（能用但不透明）
out = _check_config_sources(cfg_with(sources=[]))
check("没有配置来源 → warn", out[0].level, LV_WARN)


# ════════════════════════════════════════════════════════════════
# 3. workbench 反射（真跑本机）
# ════════════════════════════════════════════════════════════════
print("\n=== 3. workbench 反射 ===")

WB = Path.home() / ".dsh" / "papers" / "workbench"

out = _check_workbench_reflection(cfg_with(workbench=str(WB)))
keys = {c.key: c for c in out}
check_true("真 workbench → 入口 ok", keys.get("bridge:workbench", None)
           and keys["bridge:workbench"].level == LV_OK)
tools_c = keys.get("bridge:workbench.tools")
check_true("★ 真导入了 workbench 并数出工具数", tools_c is not None
           and tools_c.level == LV_OK)
if tools_c is not None:
    n = tools_c.evidence.get("count", 0)
    check_true(f"  工具数 > 20（实测 {n}）", n > 20)
    check_true("  含 web_search / web_extract（AnySearch 那条）",
               "web_search" in tools_c.evidence.get("sample", [])
               or "含 web_search" in tools_c.detail)

# 负控：目录不存在
out = _check_workbench_reflection(cfg_with(workbench=r"C:\no\such\workbench\dir"))
check("workbench 目录不存在 → fail", out[0].level, LV_FAIL)


# ════════════════════════════════════════════════════════════════
# 4. AnySearch key（warn 而不是 fail —— 这是关键区分）
# ════════════════════════════════════════════════════════════════
print("\n=== 4. AnySearch key ===")

r = _check_anysearch_key(cfg_with())
# 本机 key 是配好的 ⇒ 期望 ok；但换机器可能没有。
# 无论哪种，都不该是 fail（匿名也能用，只是限流低）。
check_true(f"AnySearch key 检查 = {r.level}（不应是 fail）", r.level in (LV_OK, LV_WARN))
check_true("  给出了来源或修法", bool(r.detail or r.hint))
if r.level == LV_OK:
    check_true("  报出了 key 长度（不是只说'有'）",
               "长度" in r.detail)


# ════════════════════════════════════════════════════════════════
# 5. 总入口：真配置端到端
# ════════════════════════════════════════════════════════════════
print("\n=== 5. diagnose_mcp 端到端（本机真配置） ===")

from rongke.config import Config                             # noqa: E402

cfg = Config.load()
rep = diagnose_mcp(cfg, install=False)    # 不真装：静态检查，快
check_true("verdict 合法", rep["verdict"] in ("ready", "degraded", "broken"))
check_true("有 summary_line", bool(rep["summary_line"]))
check_true("counts 四项齐全",
           set(rep["counts"]) == {LV_FAIL, LV_WARN, LV_INFO, LV_OK})
check_true("每条 check 都有 key/level/title",
           all(c.get("key") and c.get("level") and c.get("title")
               for c in rep["checks"]))
check_true("★ 真配置里 AnySearch key 是配好的",
           any(c["key"] == "anysearch.key" and c["level"] == LV_OK
               for c in rep["checks"]))
check_true("★ 真配置里 workbench 反射可用",
           any(c["key"] == "bridge:workbench.tools" and c["level"] == LV_OK
               for c in rep["checks"]))

txt = format_report(rep)
check_true("format_report 有内容", len(txt) > 40)

# 负控：坏配置必须把 verdict 打成 broken
bad_cfg = cfg_with(sources=["x"], rejected=[{"server": "s", "reason": "坏"}])
rep2 = diagnose_mcp(bad_cfg, install=False)
check("坏配置 → verdict=broken", rep2["verdict"], "broken")
check("  且 actionable 非空", len(rep2["actionable"]) > 0, True)

# 承重自检：**把「配置层被拒」这条判据消掉**，同一个坏配置就不该再 broken。
#   ★ 第一版我拿**另一个**（sources 非空的干净配置）去验 —— 那测的是别的东西。
#     第二版又被替身默认 `workbench=""` 引入的**无关 fail** 干扰。
#     承重自检的要义：**同一个输入**，消掉机制 ⇒ 结论必须翻转。
import rongke.mcp_doctor as _D                               # noqa: E402
_orig = _D._check_config_sources
same_input = dict(sources=["x"], rejected=[{"server": "s", "reason": "坏"}])
try:
    _D._check_config_sources = lambda cfg: []
    rep3 = diagnose_mcp(cfg_with(**same_input), install=False)
    check_true(f"承重自检：消掉该判据后同一输入不再 broken（现为 {rep3['verdict']}）",
               rep3["verdict"] != "broken")
finally:
    _D._check_config_sources = _orig

# 机制装回去 ⇒ 必须重新变回 broken（证明判据真的在承重，而不是偶然）
rep4 = diagnose_mcp(cfg_with(**same_input), install=False)
check("机制复位后重新 broken（判据在承重）", rep4["verdict"], "broken")


# ════════════════════════════════════════════════════════════════
print()
print("=" * 60)
print("全部通过 ✔" if ok_all else "有失败项 ✘")
sys.exit(0 if ok_all else 1)
