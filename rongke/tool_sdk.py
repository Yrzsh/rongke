# -*- coding: utf-8 -*-
"""AI-facing 工具与常驻工作台共享 SDK。

把全仓 7 个常驻工作台（`caps_ext/*_desk.py` / `svg_diagram.py`）和有状态动作工具
（`diagram_state` / `pathway` / `lab` / `methods` / `advisor` / `canvas`）共同重复的
四件基础设施抽成唯一真源：

  1. `ToolResult`          —— 统一结构化返回/错误信封（`ToolRegistry.call()` 原生识别）
  2. `safe_workspace_path` —— 统一工作区路径围栏（防 `../` 越界与前缀误判）
  3. `ActionRouter`        —— 声明式 `op` 动作路由器 + 单源 `guide()` 生成器
  4. `DeskCapability`      —— 常驻工作台基类与批量 `Tool` 注册辅助

零第三方依赖，纯标准库实现；不引入任何硬门（只提供结构化信息面）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from rongke.caps import Capability

#: 工作台类工具在 schema 预算裁剪中的默认优先级（数值越小越优先保留）。
DESK_TOOL_PRIORITY = 80


@dataclass
class ToolResult:
    """AI-facing 工具的统一结构化返回信封。

    约定：
      * `ok`       —— 本次工具执行是否成功（`ToolRegistry.call()` 据此置 `ToolCall.ok`）。
      * `applied`  —— 对有副作用工具，是否真正改动了外部状态（`None` 表示不适用/只读）。
      * `data`     —— 结构化载荷（面向模型与前端消费）。
      * `error`    —— 失败原因（可诊断，不拼 Python traceback）。
      * `advisory` —— 标记为过程反馈/建议态（不参与交付硬门）。
      * `gaps`     —— 结构化缺口列表 `[{"code", "level", "detail", "action"}]`。
      * `next`     —— 给模型的下一步可执行指引（单步动作，不讲空话）。
      * `extra`    —— 需要扁平透出到顶层的附加字段（兼容存量调用方协议）。
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    applied: bool | None = None
    advisory: bool = False
    gaps: list[dict[str, Any]] = field(default_factory=list)
    next: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # 供 `rongke/tools.py` 零耦合鸭子类型识别（避免 `tools_test.py` 独立 exec 触发导入环）。
    _is_tool_result: bool = field(default=True, init=False, repr=False, compare=False)

    @classmethod
    def success(
        cls,
        data: dict[str, Any] | None = None,
        *,
        applied: bool | None = None,
        advisory: bool = False,
        gaps: list[dict[str, Any]] | None = None,
        next: str = "",
        **extra: Any,
    ) -> "ToolResult":
        return cls(
            ok=True,
            data=dict(data or {}),
            error="",
            applied=applied,
            advisory=bool(advisory),
            gaps=list(gaps or []),
            next=str(next or ""),
            extra=dict(extra),
        )

    @classmethod
    def fail(
        cls,
        error: str,
        *,
        data: dict[str, Any] | None = None,
        applied: bool | None = False,
        advisory: bool = False,
        gaps: list[dict[str, Any]] | None = None,
        next: str = "",
        **extra: Any,
    ) -> "ToolResult":
        return cls(
            ok=False,
            data=dict(data or {}),
            error=str(error or "工具执行失败"),
            applied=applied,
            advisory=bool(advisory),
            gaps=list(gaps or []),
            next=str(next or ""),
            extra=dict(extra),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": bool(self.ok)}
        if self.applied is not None:
            out["applied"] = bool(self.applied)
        if self.error or not self.ok:
            out["error"] = str(self.error or "")
        if self.data:
            out["data"] = dict(self.data)
        if self.advisory:
            out["advisory"] = True
        if self.gaps:
            out["gaps"] = list(self.gaps)
        if self.next:
            out["next"] = str(self.next)
        for k, v in (self.extra or {}).items():
            if k != "ok":
                out[k] = v
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


def safe_workspace_path(
    raw: "str | Path",
    root: "str | Path | None" = None,
    *,
    allow_abs: bool = False,
) -> Path:
    """把工具入参路径规范化到工作区内，阻断 `../` 越界与目录穿越。

    * `allow_abs=False`（默认）：无论相对还是绝对路径，resolve 后必须在 `root` 之下
      （或等于 `root`）。
    * `allow_abs=True`：相对路径（含 `../`）仍严格限制在 `root` 内；仅放行调用方
      显式传入的绝对路径（供单测或外部工作目录直调使用）。
    """
    raw_str = str(raw or "").strip()
    if not raw_str:
        raise ValueError("路径不能为空")
    base = (Path(root) if root is not None else Path.cwd()).resolve()
    q = Path(raw_str)
    if q.is_absolute():
        resolved = q.resolve()
        if not allow_abs and base not in resolved.parents and resolved != base:
            raise ValueError(f"路径越出工作区：{raw}")
        return resolved
    resolved = (base / q).resolve()
    if base not in resolved.parents and resolved != base:
        raise ValueError(f"路径越出工作区：{raw}")
    return resolved


class ActionRouter:
    """声明式 `op` 动作路由器：让动作表、可变性声明与 `guide` 共用唯一真源。"""

    def __init__(self, *, name: str = "", guide_header: str = "") -> None:
        self.name = str(name or "")
        self.guide_header = str(guide_header or "")
        self._ops: dict[str, tuple[Callable[..., Any], bool, str]] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        mutating: bool = False,
        doc: str = "",
    ) -> Callable[..., Any]:
        op_name = str(name or "").strip()
        if not op_name:
            raise ValueError("op 名称不能为空")
        self._ops[op_name] = (fn, bool(mutating), str(doc or "").strip())
        return fn

    def op(
        self,
        name: str,
        *,
        mutating: bool = False,
        doc: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            return self.register(name, fn, mutating=mutating, doc=doc)
        return _deco

    def ops(self) -> tuple[str, ...]:
        return tuple(self._ops.keys())

    def mutating_ops(self) -> tuple[str, ...]:
        return tuple(k for k, (_, m, _) in self._ops.items() if m)

    def is_mutating(self, name: str) -> bool:
        hit = self._ops.get(str(name or "").strip())
        return bool(hit[1]) if hit else False

    def guide(self, header: str = "") -> str:
        lines: list[str] = []
        head = str(header or self.guide_header or "").strip()
        if head:
            lines.append(head)
        for op_name, (_, mut, doc) in self._ops.items():
            tag = "写" if mut else "读"
            lines.append(f"· {op_name} [{tag}]" + (f"：{doc}" if doc else ""))
        return "\n".join(lines)

    @staticmethod
    def parse_action(action: Any) -> tuple[dict[str, Any] | None, str]:
        if isinstance(action, dict):
            return dict(action), ""
        if isinstance(action, str):
            raw = action.strip()
            if not raw:
                return None, "action 不能为空"
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                return None, f"action 不是合法 JSON：{exc}"
            if not isinstance(parsed, dict):
                return None, "action 必须是 JSON 对象"
            return parsed, ""
        return None, f"action 必须是对象或 JSON 字符串，收到 {type(action).__name__}"

    def dispatch(
        self,
        action: Any,
        *,
        catch_errors: bool = False,
        **ctx: Any,
    ) -> Any:
        payload, err = self.parse_action(action)
        if payload is None:
            return ToolResult.fail(
                err,
                applied=False,
                next=f"请传包含 op 的 JSON 对象；支持：{', '.join(self.ops())}",
            )
        op_name = str(payload.get("op") or "").strip()
        if not op_name:
            return ToolResult.fail(
                "缺少 op 字段",
                applied=False,
                next=f"支持 op：{', '.join(self.ops())}",
            )
        hit = self._ops.get(op_name)
        if hit is None:
            return ToolResult.fail(
                f"未知 op：{op_name}",
                applied=False,
                next=f"支持 op：{', '.join(self.ops())}",
            )
        fn, _, _ = hit
        if not catch_errors:
            return fn(payload, **ctx)
        try:
            return fn(payload, **ctx)
        except Exception as exc:                              # noqa: BLE001
            return ToolResult.fail(
                f"{type(exc).__name__}: {exc}",
                applied=False,
                op=op_name,
            )


def register_tool_specs(
    registry: Any,
    specs: Iterable[tuple[str, str, dict[str, Any], list[str], Callable[..., Any], bool]],
    *,
    domain: str = "core",
    source: str = "plugin",
    priority: int = DESK_TOOL_PRIORITY,
) -> list[str]:
    """把 `(name, desc, props, required, fn, mutating)` 规格表批量注册进 ToolRegistry。

    常驻工作台工具是长命工具，不登记进 `ctx["registered_tools"]`（不进预设撤销清单），
    并且自动跳过已注册的同名工具以保证幂等。
    """
    if registry is None:
        return []
    from rongke.tools import Tool

    added: list[str] = []
    for name, desc, props, required, fn, mutating in specs:
        if registry.get(name) is not None:
            continue
        registry.register(
            Tool(
                name=name,
                description=desc,
                parameters={
                    "type": "object",
                    "properties": dict(props or {}),
                    "required": list(required or []),
                },
                fn=fn,
                domain=domain,
                mutating=bool(mutating),
                source=source,
                priority=int(priority),
            )
        )
        added.append(name)
    return added


class DeskCapability(Capability):
    """常驻工作台能力基类（`ALWAYS_ON = True`）。

    收敛 `caps_ext/*_desk.py` 与 `svg_diagram.py` 的公共声明和批量注册样板；
    子类只需声明 `id` / `SECTION_TEXTS` / `ESSENTIAL_TOOLS` 以及 `mount()`。
    """

    ALWAYS_ON = True
    kind = "tool"
    version = "1.0.0"
    config_schema: dict[str, dict] = {}
    ALWAYS_CONFIG: dict[str, Any] = {}
    SECTION_TEXTS: dict[str, str] = {}
    ESSENTIAL_TOOLS: tuple[str, ...] = ()
    TOOL_PRIORITY: int = DESK_TOOL_PRIORITY

    def register_specs(
        self,
        ctx: dict[str, Any],
        specs: Iterable[tuple[str, str, dict[str, Any], list[str], Callable[..., Any], bool]],
        *,
        domain: str = "core",
        source: str = "plugin",
        priority: int | None = None,
    ) -> list[str]:
        prio = self.TOOL_PRIORITY if priority is None else int(priority)
        return register_tool_specs(
            ctx.get("registry"),
            specs,
            domain=domain,
            source=source,
            priority=prio,
        )
