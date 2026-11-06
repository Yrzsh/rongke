# -*- coding: utf-8 -*-
"""荣珂钩子体系 —— 在工具调用前后拦截。

目前有两个钩子，都挂在 `registry.hooks` 上：

  * **`WorkspaceFence`** —— 工作区围栏（**拦截**，本文件）
  * **`TraceRecorder`** —— 调用留痕（**观察**，见 `telemetry.py`）

为什么必须有围栏（这不是假想问题，是首跑实测踩到的）：
模型调用 `build_references(project="ophiocordyceps_ref_verification")`，
把项目目录直接建在了进程 cwd —— 内核当时没有任何机制阻止它写到任意位置。
一个自主 agent 没有围栏，迟早会写到不该写的地方（改坏稿件、覆盖配置、
把垃圾撒满项目根）。

围栏策略：
  * 允许写入的根 = 本次任务的 workspace（run() 期间进程 cwd 也在那里，
    所以相对路径天然落在里面）
  * 对 **mutating 工具**，参数里出现**绝对路径**且越界 → 拒绝执行
  * 拒绝不是崩溃：把原因当作观察结果回给模型，让它自己改用相对路径
  * 每次拦截都留审计记录，供事后溯源

**观察与拦截刻意分开**：留痕不该有拒绝权（一个记录器把关会让人分不清
"是策略不让做"还是"记录器坏了"）。`HookDecision` 的 deny 通道留给策略类钩子用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_UNC = re.compile(r"^\\\\")
_POSIX_ABS = re.compile(r"^/")


@dataclass
class HookDecision:
    allow: bool = True
    reason: str = ""
    args: dict | None = None          # 重写后的参数（None = 不改）

    @classmethod
    def ok(cls, args: dict | None = None) -> "HookDecision":
        return cls(allow=True, args=args)

    @classmethod
    def deny(cls, reason: str) -> "HookDecision":
        return cls(allow=False, reason=reason)


class Hook:
    """钩子基类。before_call 返回 HookDecision；after_call 只做记录。"""
    name = "hook"

    def before_call(self, tool: Any, args: dict) -> HookDecision:
        return HookDecision.ok()

    def after_call(self, call: Any) -> None:
        return None


def looks_like_path(v: str) -> bool:
    """粗判一个字符串是不是路径。宁可误判为路径（多检查一次）也不漏判。"""
    if not v or len(v) > 400:
        return False
    if _WIN_ABS.match(v) or _UNC.match(v) or _POSIX_ABS.match(v):
        return True
    if v.startswith("~"):
        return True
    return ("/" in v) or ("\\" in v)


class WorkspaceFence(Hook):
    """工作区围栏：mutating 工具的绝对路径参数不得越界。"""

    name = "workspace_fence"

    def __init__(self, roots: list[Path], base: Path | None = None):
        self.roots = [Path(r).resolve() for r in roots]
        self.base = Path(base).resolve() if base else self.roots[0]
        self.blocked: list[dict] = []

    # ── 判定 ────────────────────────────────────────────────
    def inside(self, p: Path) -> bool:
        try:
            rp = p.resolve()
        except Exception:
            return False
        return any(rp == r or r in rp.parents for r in self.roots)

    def before_call(self, tool: Any, args: dict) -> HookDecision:
        if tool is None or not getattr(tool, "mutating", False):
            return HookDecision.ok()

        for key, val in (args or {}).items():
            if not isinstance(val, str) or not looks_like_path(val):
                continue
            p = Path(val)
            if not p.is_absolute():
                continue          # 相对路径：进程 cwd 已在 workspace 内，放行
            if self.inside(p):
                continue
            reason = (
                f"⛔ 工作区围栏拦截：参数 `{key}` = {val!r} 指向围栏之外。\n"
                f"允许写入的位置只有：{', '.join(str(r) for r in self.roots)}\n"
                f"请改用**相对路径**（相对路径会自动落在本次任务的 workspace 内），"
                f"或直接使用工具默认输出位置。"
            )
            self.blocked.append({"tool": getattr(tool, "name", "?"), "arg": key, "value": val})
            return HookDecision.deny(reason)
        return HookDecision.ok()

    # ── 审计 ────────────────────────────────────────────────
    def summary(self) -> dict:
        return {
            "roots": [str(r) for r in self.roots],
            "blocked_count": len(self.blocked),
            "blocked": self.blocked[:20],
        }


class AuditLog(Hook):
    """已迁出 —— 保留此名仅为不打断任何外部引用。

    原本这里定义了一个 `after_call` 留痕钩子，但它**从未被实例化**（死代码）：
    它的职责（把每次工具调用记成结构化记录）现在由
    `rongke.telemetry.TraceRecorder` 承担，而且做得更完整
    （span 层级 + 时间戳 + 词汇表校验）。

    **不要再往这里加逻辑。** 同一个职责有两个实现就是「平行机制」，
    而平行机制的成本是维护面的平方 —— 改了一处忘了另一处，
    症状是"某条路径上的记录莫名消失"，且不报错。
    """
    name = "audit"

    def __new__(cls, *args, **kwargs):
        from .telemetry import TraceRecorder
        return TraceRecorder(*args, **kwargs)
