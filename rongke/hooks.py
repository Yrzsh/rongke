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
from typing import Any, Iterable, Iterator

HOOK_LAYERS: tuple[str, ...] = ("policy", "ext", "observer")

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
    layer = "ext"

    def before_call(self, tool: Any, args: dict) -> HookDecision:
        return HookDecision.ok()

    def after_call(self, call: Any) -> None:
        return None


class WorkflowHook(Hook):
    """工作流扩展钩子协议（零依赖鸭子类型，完全兼容 `Hook`）。

    除工具调用前后的 `before_call` / `after_call` 外，可选实现两个工作流插槽：
      * `after_tool_obs(name, args, call, obs) -> str | None`
        在工具观察文本回灌给模型前执行；返回非 None 字符串即替换观察文本，
        返回 None 表示保持原样。
      * `before_deliver(claim, ctx) -> dict | None`
        在交付进入完成证据门前执行（如确定性对账/规范化）；返回结构化事实字典
        或 None，不替代完成证据门。
    """
    name = "workflow_hook"
    layer = "ext"

    def after_tool_obs(self, name: str, args: dict, call: Any, obs: str) -> str | None:
        return None

    def before_deliver(self, claim: Any, ctx: dict) -> dict | None:
        return None


class HookPipeline:
    """分层钩子管线：按固定顺序 `policy → ext → observer` 组合执行。

    设计理由：
      · 运行时每次任务装配 `policy`（工作区围栏）与 `observer`（遥测）时，
        通过 `set_layer()` 只更新各自槽位，绝不抹掉插件/能力注册在 `ext` 槽的钩子。
      · 同时实现 `__iter__` / `__len__` / `as_list()`，可直接传给
        `ToolRegistry.hooks` 与 `wiring.inspect()`，完全兼容列表语义。
    """

    def __init__(self, *,
                 policy: Iterable[Any] = (),
                 ext: Iterable[Any] = (),
                 observer: Iterable[Any] = ()) -> None:
        self._layers: dict[str, list[Any]] = {
            "policy": [h for h in policy if h is not None],
            "ext": [h for h in ext if h is not None],
            "observer": [h for h in observer if h is not None],
        }

    @staticmethod
    def _check_layer(layer: str) -> str:
        lv = str(layer or "ext").strip().lower()
        if lv not in HOOK_LAYERS:
            raise ValueError(f"未知钩子层 {layer!r}（仅支持 {list(HOOK_LAYERS)}）")
        return lv

    def add(self, hook: Any, layer: str | None = None) -> None:
        if hook is None:
            return
        lv = self._check_layer(layer if layer is not None else getattr(hook, "layer", "ext"))
        bucket = self._layers[lv]
        if hook not in bucket:
            bucket.append(hook)

    def set_layer(self, layer: str, hooks: Iterable[Any]) -> None:
        lv = self._check_layer(layer)
        self._layers[lv] = [h for h in (hooks or ()) if h is not None]

    def clear_layer(self, layer: str) -> None:
        lv = self._check_layer(layer)
        self._layers[lv].clear()

    def remove(self, hook_or_name: Any) -> bool:
        removed = False
        for lv in HOOK_LAYERS:
            before = len(self._layers[lv])
            if isinstance(hook_or_name, str):
                self._layers[lv] = [
                    h for h in self._layers[lv]
                    if getattr(h, "name", "") != hook_or_name
                ]
            else:
                self._layers[lv] = [h for h in self._layers[lv] if h is not hook_or_name]
            if len(self._layers[lv]) != before:
                removed = True
        return removed

    def layer(self, layer: str) -> list[Any]:
        return list(self._layers[self._check_layer(layer)])

    def as_list(self) -> list[Any]:
        out: list[Any] = []
        for lv in HOOK_LAYERS:
            out.extend(self._layers[lv])
        return out

    def __iter__(self) -> Iterator[Any]:
        return iter(self.as_list())

    def __len__(self) -> int:
        return sum(len(self._layers[lv]) for lv in HOOK_LAYERS)

    def run_before_call(self, tool: Any, args: dict) -> tuple[HookDecision, str]:
        """按层序执行 `before_call`，返回 `(HookDecision, blocked_by)`。"""
        cur_args = dict(args or {})
        rewritten = False
        for h in self.as_list():
            fn = getattr(h, "before_call", None)
            if not callable(fn):
                continue
            try:
                dec = fn(tool, cur_args)
            except Exception:                                  # noqa: BLE001
                continue
            if dec is None:
                continue
            if not dec.allow:
                return dec, str(getattr(h, "name", "hook"))
            if dec.args is not None:
                cur_args = dec.args
                rewritten = True
        return HookDecision.ok(cur_args if rewritten else None), ""

    def run_after_call(self, call: Any) -> None:
        for h in self.as_list():
            fn = getattr(h, "after_call", None)
            if not callable(fn):
                continue
            try:
                fn(call)
            except Exception:                                  # noqa: BLE001
                pass

    def run_after_tool_obs(self, name: str, args: dict, call: Any, obs: str) -> str:
        cur = obs
        for h in self.as_list():
            fn = getattr(h, "after_tool_obs", None)
            if not callable(fn):
                continue
            try:
                nxt = fn(name, args, call, cur)
            except Exception:                                  # noqa: BLE001
                continue
            if isinstance(nxt, str):
                cur = nxt
        return cur

    def run_before_deliver(self, claim: Any, ctx: dict | None = None) -> list[dict]:
        """执行所有支持 `before_deliver` 的钩子，失败留痕不静默吞掉。"""
        out: list[dict] = []
        payload = ctx if ctx is not None else {}
        for h in self.as_list():
            fn = getattr(h, "before_deliver", None)
            if not callable(fn):
                continue
            hname = str(getattr(h, "name", type(h).__name__))
            try:
                res = fn(claim, payload)
            except Exception as e:                             # noqa: BLE001
                out.append({"hook": hname, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
                continue
            if res is not None:
                out.append({"hook": hname, "ok": True, "result": res})
        return out

    def summary(self) -> dict:
        all_h = self.as_list()
        return {
            "layers": {lv: [str(getattr(h, "name", type(h).__name__))
                            for h in self._layers[lv]]
                       for lv in HOOK_LAYERS},
            "total": len(all_h),
            "has_before": any(callable(getattr(h, "before_call", None)) for h in all_h),
            "has_after": any(callable(getattr(h, "after_call", None)) for h in all_h),
        }


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
    layer = "policy"

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
