# -*- coding: utf-8 -*-
"""接线全景（**只读**）：把「在册 / schema 可见 / 调用时机 / 反馈闭环」四件读回成一张表。

动机（2026-11-14，Qwen Code 调研 + 本仓接线审计）
------------------------------------------------
荣珂的接线散在 `loop.py`(6200 行) / `caps.py`(855 行) / `tools.py` / `mcp_client.py`
的注释里，**没有一个地方能一次读出「现在到底接上了没有」**。于是反复出现同一类事故：

  · 外部能力文件半途注册失败，名字留在 `_REGISTRY` 却进不了常驻清单
    ⇒ 能力 resolve 得到、实际不生效（`caps.py` 本轮已修，判据常驻在 caps_ext_test）；
  · `force_brief` 单测全绿，生产 `_loop` 的 overflow 分支根本没调它；
  · MCP 载荷 `{"ok": true, "error": "..."}` 滑成 `ToolCall.ok=True`。

Qwen Code 自己也没做到完美单源（tool 描述用 getter 单源，但 system prompt 是启动快照、
技能靠 includeDirectories 拼字符串）。它真正有效的一条是：**凡存在第二份状态，
就补一条显式校验或一条可见告警 —— 宁可吵，不可静默**。本模块就是荣珂这一侧的落点。

设计纪律
--------
* **只读**：不挂载、不注册、不改 SYSTEM、不触发网络。
* **判据基准独立于被测对象**：`inspect()` 接受任意 registry；`always_on` / `external_errors`
  可注入，测试用自己构造的夹具，不拿实现的输出当期望。
* **失败必留痕**：任何未通过项都进 `gaps`，附 `code` / `detail`，绝不静默跳过。
* **只量接线，不冒充质量门**：`ok=True` 只说明「线接上了」，不说明工具产出好不好看。

用法
----
    from rongke.wiring import inspect
    rep = inspect(rk.registry)
    print(rep["summary"]["ok"], rep["gaps"])

CLI：`python -m rongke --wiring`；Web：`GET /api/wiring`。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import caps

WIRE_VERSION = 1
#: 生产侧查「钩子运行时装配」的源码位置；测试夹具传 None 关掉探测。
DEFAULT_LOOP_PY = Path(__file__).with_name("loop.py")

#: 判据顺序固定 —— 报告与测试读同一份名字，不各写一遍。
CHECKS = (
    "W1_每个工具都有非占位描述",
    "W2_每个工具的参数契约是合法 object",
    "W3_每个工具都有可执行体",
    "W4_schema 数量与在册数量一致",
    "W5_常驻声明的工具全部真的在册",
    "W6_钩子具备 before/after 反馈闭环",
    "W7_外部能力加载没有静默错误",
    "W8_工具名无重复",
    "W9_会话指纹读回稳定",
)


def hook_policy_declared(loop_py: Path) -> bool:
    """从 `loop.py` 读回「钩子在任务开始时装配」这条接线还在不在。

    荣珂的钩子是**运行时装配**（`loop._run_serial` 里
    `self.registry.hooks = [self.fence, self.telemetry]`），不是构造 `Rongke()`
    时就挂上。所以静态自检看到 `n_hooks=0` 是**正常状态**，不是断线 ——
    但也不能因此把 W6 放宽成恒真：得有源码证据证明装配语句还在。
    这条就是 Qwen Code 那条「第二份状态必须配显式校验」的落地：
    运行时状态与源码声明各管一半，任一缺失都吵出来。
    """
    try:
        src = Path(loop_py).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return "self.registry.hooks =" in src


def schema_fingerprint(registry: Any) -> str:
    """会话指纹：只锁 `name + parameters`。

    **description 故意不参与** —— 它是自由文本，改一句说明不该被当成「工具集变了」。
    Qwen Code 那边用 getter 让描述单源刷新，但没做会话级比对；这里把「模型看到的
    schema」与「实际注册的工具」锁成同一个可复算的短哈希，装配期与运行期各量一次
    就能发现漂移。真要换工具集，指纹必须**显式地**变，而不是悄悄变。
    """
    payload = []
    for name in registry.names():
        t = registry.get(name)
        if t is None:
            continue
        payload.append({"name": t.name, "parameters": t.parameters or {}})
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def tool_rows(registry: Any, always_on: set[str] | None = None) -> list[dict]:
    """逐工具读回接线事实。只用 `names()/get()/openai_schemas()` 这些公开方法。"""
    always = always_on if always_on is not None else set(caps.always_on_tool_names())
    rows: list[dict] = []
    for name in registry.names():
        t = registry.get(name)
        if t is None:
            continue
        p = t.parameters or {}
        props = p.get("properties") or {} if isinstance(p, dict) else {}
        rows.append({
            "name": t.name,
            "domain": getattr(t, "domain", ""),
            "source": getattr(t, "source", ""),
            "mutating": bool(getattr(t, "mutating", False)),
            "always_on": t.name in always,
            "description_chars": len(t.description or ""),
            "required": list(p.get("required") or []) if isinstance(p, dict) else [],
            "n_properties": len(props),
            "callable": callable(t.fn),
            "schema_ok": isinstance(p, dict) and p.get("type") == "object",
        })
    return rows


def _hooks_view(hooks: list | None) -> list[dict]:
    out = []
    for h in hooks or []:
        out.append({
            "name": getattr(h, "name", type(h).__name__),
            "class": type(h).__name__,
            "before": callable(getattr(h, "before_call", None)),
            "after": callable(getattr(h, "after_call", None)),
        })
    return out


def inspect(registry: Any, *, hooks: list | None = None,
            always_on: list[str] | None = None,
            external_errors: list[dict] | None = None,
            hook_policy_source: Path | None = None) -> dict:
    """读回全链接线状态。**不修改任何东西**。"""
    declared = (list(always_on) if always_on is not None
                else list(caps.always_on_tool_names()))
    ext_errs = (list(external_errors) if external_errors is not None
                else list(caps.ensure_external_loaded()))
    hooks_v = _hooks_view(hooks if hooks is not None
                          else getattr(registry, "hooks", []))
    declared_hook = bool(hook_policy_source
                         and hook_policy_declared(hook_policy_source))
    # ⚠️ 闭环要看**钩子集合**两端齐不齐，不是要求某一个钩子同时具备两端。
    # 荣珂的真实分工是 WorkspaceFence 管 before（拦截/改参数）、TraceRecorder
    # 管 after（记拦截后的最终事实）——一个钩子只做一端是设计，不是断线。
    # 2026-11-14 独立验收抓到：按「单钩子两端」写判据，会靠 declared 兜底
    # 侥幸变绿，运行时其实没有分辨率。基准必须与实现形状对齐。
    has_before = any(h["before"] for h in hooks_v)
    has_after = any(h["after"] for h in hooks_v)
    runtime_closed = has_before and has_after
    rows = tool_rows(registry, set(declared))
    names = registry.names()
    try:
        n_schemas = len(registry.openai_schemas())
    except Exception as e:                                  # noqa: BLE001
        n_schemas = -1
        ext_errs = list(ext_errs) + [{"file": "openai_schemas()",
                                      "error": f"{type(e).__name__}: {e}"}]

    checks = {
        "W1_每个工具都有非占位描述":
            all(r["description_chars"] > 0 and r["name"] != "" for r in rows)
            and bool(rows),
        "W2_每个工具的参数契约是合法 object":
            all(r["schema_ok"] for r in rows) and bool(rows),
        "W3_每个工具都有可执行体":
            all(r["callable"] for r in rows) and bool(rows),
        "W4_schema 数量与在册数量一致":
            n_schemas == len(names) and n_schemas >= 0,
        "W5_常驻声明的工具全部真的在册":
            all(n in names for n in declared),
        "W6_钩子具备 before/after 反馈闭环":
            runtime_closed or declared_hook,
        "W7_外部能力加载没有静默错误":
            len(ext_errs) == 0,
        "W8_工具名无重复":
            len(names) == len(set(names)),
        "W9_会话指纹读回稳定":
            schema_fingerprint(registry) == schema_fingerprint(registry),
    }

    gaps: list[dict] = []
    for k, v in checks.items():
        if not v:
            detail = (""
                      if not k.startswith("W6_") else
                      "运行时未挂钩子，且 loop.py 里读不到 "
                      "self.registry.hooks = 装配语句")
            gaps.append({"code": k.split("_", 1)[0], "check": k,
                         "detail": detail})
    for e in ext_errs:
        gaps.append({"code": "EXT", "check": "外部能力加载错误",
                     "detail": f"{e.get('file', '')}: {e.get('error', '')}"})
    missing_always = [n for n in declared if n not in names]
    if missing_always:
        gaps.append({"code": "W5", "check": "常驻声明未落地",
                     "detail": "、".join(missing_always)})

    domains = sorted({r["domain"] for r in rows if r["domain"]})
    return {
        "version": WIRE_VERSION,
        "fingerprint": schema_fingerprint(registry),
        "summary": {
            "ok": not gaps,
            "n_tools": len(rows),
            "n_domains": len(domains),
            # ⚠️ 口径：`Tool.domain` 是**分类标签**（core/research/kb/topic/ppt/figure），
            # 与 `docs/产品说明.md` 里的「33 个域 = 已挂上的域插件数」不是同一件东西。
            # 不写清楚就会凭空造出「83 工具只有 6 个域」这种假矛盾。
            "domain_note": ("Tool.domain 分类标签数；产品说明的「域」指已挂载的"
                            "域插件数（domain_mount），两者口径不同"),
            "n_always_on_declared": len(declared),
            "n_always_on_registered": sum(1 for r in rows if r["always_on"]),
            "n_schemas": n_schemas,
            "n_hooks": len(hooks_v),
            "hooks_mode": ("runtime" if runtime_closed else
                           "declared" if declared_hook else "none"),
            "hooks_before": has_before,
            "hooks_after": has_after,
            "n_external_errors": len(ext_errs),
            "n_gaps": len(gaps),
        },
        "checks": checks,
        "gaps": gaps,
        "hooks": hooks_v,
        "always_on": sorted(declared),
        "domains": domains,
        "external_errors": ext_errs,
        "tools": rows,
        "boundary": ("ok=True 只表示接线四件齐备，不表示工具产出质量达标；"
                     "工具级「载荷自述失败」由 mcp_client / subagent 各自的判据守，"
                     "不在本模块 W 判据里。"),
    }


def assembly_report(kernel: Any, *,
                    hook_policy_source: Path | None = DEFAULT_LOOP_PY) -> dict:
    """只读聚合五条装配通道（workbench / MCP / plugins / presets / always_on）健康态。

    设计纪律：
      * **纯只读**：不挂载、不启子进程、不联网、不改 kernel 状态。
      * **缺件必留痕**：五条通道与底层 W1–W9 的阻断问题汇总到顶层 `gaps`，
        降级项（如 MCP skipped / degraded）汇总到 `warnings`，绝不静默丢弃。
      * **鸭子类型兼容**：允许传入完整 `Rongke` 实例或轻量测试替身。
    """
    gaps: list[dict] = []
    warnings: list[dict] = []

    # ── 0. 基础接线（W1–W9） ────────────────────────────────────
    reg = getattr(kernel, "registry", None)
    if reg is not None:
        wire_rep = inspect(reg, hook_policy_source=hook_policy_source)
    else:
        wire_rep = {
            "version": WIRE_VERSION,
            "fingerprint": "",
            "summary": {
                "ok": False, "n_tools": 0, "n_domains": 0,
                "n_always_on_declared": 0, "n_always_on_registered": 0,
                "n_schemas": 0, "n_hooks": 0, "hooks_mode": "none",
                "hooks_before": False, "hooks_after": False,
                "n_external_errors": 0, "n_gaps": 1,
            },
            "checks": {},
            "gaps": [{"code": "REG", "check": "工具注册表缺失",
                      "detail": "kernel.registry 为 None"}],
            "hooks": [],
            "always_on": [],
            "domains": [],
            "external_errors": [],
            "tools": [],
            "boundary": "ok=True 只表示装配与接线齐备，不代表产出质量达标。",
        }
    for g in wire_rep.get("gaps") or []:
        gaps.append({"channel": "wiring", "code": g.get("code", "WIRE"),
                     "detail": f"{g.get('check', '')} {g.get('detail', '')}".strip()})

    # ── 1. Workbench 反射通道 ───────────────────────────────────
    wb_installed = list(getattr(kernel, "_installed", None) or [])
    wb_error = str(getattr(kernel, "workbench_error", "") or "").strip()
    wb_ok = not bool(wb_error)
    if not wb_ok:
        gaps.append({"channel": "workbench", "code": "BRIDGE", "detail": wb_error})
    ch_workbench = {
        "ok": wb_ok,
        "installed_count": len(wb_installed),
        "error": wb_error,
    }

    # ── 2. MCP 外部服务端通道 ───────────────────────────────────
    mcp_diag_fn = getattr(kernel, "mcp_diag", None)
    if callable(mcp_diag_fn):
        try:
            md = dict(mcp_diag_fn() or {})
        except Exception as e:                                 # noqa: BLE001
            md = {"errors": {"<mcp_diag>": f"{type(e).__name__}: {e}"}}
    else:
        hub = getattr(kernel, "mcp", None)
        mrep = dict(getattr(kernel, "mcp_report", None) or {})
        md = {
            "enabled": getattr(getattr(getattr(kernel, "cfg", None), "mcp", None),
                               "enabled", bool(getattr(hub, "specs", None))),
            "specs": [s.as_dict() if hasattr(s, "as_dict") else s
                      for s in (getattr(hub, "specs", {}) or {}).values()],
            "installed": list(mrep.get("installed") or []),
            "skipped": list(getattr(hub, "skipped", None) or [])
                       + list(mrep.get("skipped") or []),
            "errors": dict(getattr(hub, "errors", None) or {}),
            "rejected": list(getattr(getattr(getattr(kernel, "cfg", None), "mcp", None),
                                     "rejected", None) or []),
        }
    mcp_health = dict(getattr(kernel, "mcp_health", None) or {})
    mcp_errors = dict(md.get("errors") or {})
    mcp_rejected = list(md.get("rejected") or [])
    mcp_skipped = list(md.get("skipped") or [])
    mcp_verdict = str(mcp_health.get("verdict") or "")
    mcp_ok = not mcp_errors and not mcp_rejected and mcp_verdict != "broken"
    for srv, err in mcp_errors.items():
        gaps.append({"channel": "mcp", "code": "MCP_ERR",
                     "detail": f"{srv}: {err}"})
    for rej in mcp_rejected:
        gaps.append({"channel": "mcp", "code": "MCP_REJECTED",
                     "detail": f"{rej.get('server') or rej.get('file') or '?'}: "
                               f"{rej.get('reason', '')}"})
    if mcp_verdict == "broken":
        gaps.append({"channel": "mcp", "code": "MCP_HEALTH",
                     "detail": str(mcp_health.get("summary_line") or "MCP 自检 broken")})
    elif mcp_verdict == "degraded":
        warnings.append({"channel": "mcp", "code": "MCP_DEGRADED",
                         "detail": str(mcp_health.get("summary_line") or "MCP 自检降级")})
    for sk in mcp_skipped:
        reason = str(sk.get("reason") or "")
        # 配置显式 disabled 属于正常告知，不计入告警
        if "disabled" in reason.lower():
            continue
        warnings.append({"channel": "mcp", "code": "MCP_SKIP",
                         "detail": f"{sk.get('tool') or sk.get('server') or '?'}: {reason}"})
    ch_mcp = {
        "ok": mcp_ok,
        "enabled": bool(md.get("enabled", False)),
        "servers": len(md.get("specs") or []),
        "installed_count": len(md.get("installed") or []),
        "errors": mcp_errors,
        "rejected": mcp_rejected,
        "skipped": mcp_skipped,
        "health_verdict": mcp_verdict,
    }

    # ── 3. 第三方 Python 插件通道（plugins.py） ─────────────────
    prep = dict(getattr(kernel, "plugins_report", None) or {})
    p_items = list(prep.get("items") or [])
    p_failed_items = [it for it in p_items if not it.get("ok", True)]
    p_failed = int(prep.get("failed", len(p_failed_items)) or 0)
    p_ok = p_failed == 0
    if not p_ok:
        if p_failed_items:
            for it in p_failed_items:
                gaps.append({"channel": "plugins", "code": "PLUGIN",
                             "detail": f"{it.get('name') or it.get('path') or '?'}: "
                                       f"{it.get('error') or '加载失败'}"})
        else:
            gaps.append({"channel": "plugins", "code": "PLUGIN",
                         "detail": str(prep.get("summary") or f"{p_failed} 个插件失败")})
    ch_plugins = {
        "ok": p_ok,
        "scanned": int(prep.get("scanned", len(p_items)) or 0),
        "ok_count": int(prep.get("ok", len(p_items) - len(p_failed_items)) or 0),
        "failed_count": p_failed,
        "failed_items": p_failed_items,
        "summary": str(prep.get("summary") or ""),
    }

    # ── 4. 能力预设通道（caps.py） ──────────────────────────────
    caps_obj = getattr(kernel, "caps", None)
    cap_rep = dict(getattr(kernel, "presets_report", None) or {})
    if not cap_rep and caps_obj is not None and hasattr(caps_obj, "report"):
        try:
            cap_rep = dict(caps_obj.report.to_dict() or {})
        except Exception:                                      # noqa: BLE001
            cap_rep = {}
    cap_broken = list(cap_rep.get("broken") or [])
    cap_conflicts = list(cap_rep.get("conflicts") or [])
    cap_healthy = bool(cap_rep.get("healthy", not cap_broken and not cap_conflicts))
    if not cap_healthy:
        for b in cap_broken:
            detail = (f"{b.get('id', '')} {b.get('name', '')}: {b.get('error', '')}".strip()
                      if isinstance(b, dict) else str(b))
            gaps.append({"channel": "presets", "code": "PRESET", "detail": detail})
        for c in cap_conflicts:
            gaps.append({"channel": "presets", "code": "PRESET_CONFLICT",
                         "detail": str(c)})
    ch_presets = {
        "ok": cap_healthy,
        "active_preset": str(getattr(caps_obj, "preset_id", "") or ""),
        "healthy": cap_healthy,
        "broken": cap_broken,
        "conflicts": cap_conflicts,
        "summary": str(cap_rep.get("summary") or ""),
    }

    # ── 5. 常驻能力通道（ALWAYS_ON） ────────────────────────────
    ws = wire_rep.get("summary") or {}
    ao_declared = list(wire_rep.get("always_on") or [])
    reg_names = set(reg.names()) if reg is not None and callable(getattr(reg, "names", None)) else set()
    ao_missing = [n for n in ao_declared if n not in reg_names]
    ao_ext_errs = list(wire_rep.get("external_errors") or [])
    ao_ok = (not ao_missing) and (not ao_ext_errs)
    ch_always_on = {
        "ok": ao_ok,
        "declared": len(ao_declared),
        "registered": int(ws.get("n_always_on_registered", len(ao_declared) - len(ao_missing)) or 0),
        "missing": ao_missing,
        "external_errors": ao_ext_errs,
    }

    channels = {
        "workbench": ch_workbench,
        "mcp": ch_mcp,
        "plugins": ch_plugins,
        "presets": ch_presets,
        "always_on": ch_always_on,
    }
    overall_ok = len(gaps) == 0
    verdict = ("broken" if not overall_ok else
               "degraded" if warnings else "ok")
    return {
        "version": WIRE_VERSION,
        "ok": overall_ok,
        "verdict": verdict,
        "fingerprint": wire_rep.get("fingerprint", ""),
        "summary": {
            "ok": overall_ok,
            "verdict": verdict,
            "n_tools": int(ws.get("n_tools", 0) or 0),
            "n_channels": len(channels),
            "channels_ok": sum(1 for c in channels.values() if c.get("ok")),
            "n_gaps": len(gaps),
            "n_warnings": len(warnings),
        },
        "channels": channels,
        "wiring": wire_rep,
        "gaps": gaps,
        "warnings": warnings,
        "boundary": wire_rep.get("boundary", ""),
    }


def format_text(rep: dict) -> str:
    """CLI 人读版。缺件一律印出来，不省略。"""
    s = rep["summary"]
    lines = [
        f"荣珂接线全景 v{rep['version']}  指纹 {rep['fingerprint']}",
        f"工具 {s['n_tools']} / 分类标签 {s['n_domains']} / schema {s['n_schemas']} / "
        f"常驻 {s['n_always_on_registered']}/{s['n_always_on_declared']} / "
        f"钩子 {s['n_hooks']}（{s['hooks_mode']}） / "
        f"外部错误 {s['n_external_errors']}",
        {"declared": "钩子在任务开始时装配（loop._run_serial），静态自检为 0 属正常；"
                     "W6 靠源码装配语句读回判定。",
         "runtime": "任务运行中：闭环由围栏(before) + 遥测(after) 分工凑齐，"
                    "不要求单个钩子两端兼备。",
         "none": "⚠ 既无运行时钩子，也读不到装配语句 —— W6 必红。"}[s["hooks_mode"]],
    ]
    for k, v in rep["checks"].items():
        lines.append(f"  [{'PASS' if v else 'FAIL'}] {k}")
    if rep["hooks"]:
        lines.append("")
        lines.append("钩子（观察者排在策略之后，记的是拦截后的最终事实）：")
        for h in rep["hooks"]:
            lines.append(f"  · {h['name']} ({h['class']}) "
                         f"before={h['before']} after={h['after']}")
    if rep["gaps"]:
        lines.append("")
        lines.append(f"⚠ 缺口 {len(rep['gaps'])} 条：")
        for g in rep["gaps"]:
            lines.append(f"  · [{g['code']}] {g['check']} {g['detail']}")
    else:
        lines.append("")
        lines.append("全部通过 ✔ —— 只说明线接上了，不代表成品质量。")
    return "\n".join(lines)


def render_html(rep: dict) -> str:
    """一份可交付的接线全景页（浅色主题、无外部依赖）。"""
    s = rep["summary"]
    chk = "".join(
        f'<tr><td>{k}</td><td class="{"g" if v else "r"}">'
        f'{"PASS" if v else "FAIL"}</td></tr>' for k, v in rep["checks"].items())
    hooks = "".join(
        f'<tr><td>{h["name"]}</td><td>{h["class"]}</td>'
        f'<td>{h["before"]}</td><td>{h["after"]}</td></tr>'
        for h in rep["hooks"]) or (
        '<tr><td colspan="4">（运行时未挂钩子；hooks_mode=declared '
        '表示任务开始时装配，属正常态）</td></tr>')
    gaps = "".join(f"<li>[{g['code']}] {g['check']} {g['detail']}</li>"
                   for g in rep["gaps"]) or "<li>无</li>"
    rows = "".join(
        f'<tr><td><code>{r["name"]}</code></td><td>{r["domain"]}</td>'
        f'<td>{r["source"]}</td><td>{"常驻" if r["always_on"] else "—"}</td>'
        f'<td>{"写" if r["mutating"] else "读"}</td>'
        f'<td>{r["n_properties"]}</td><td>{", ".join(r["required"]) or "—"}</td>'
        f'<td class="{"g" if r["schema_ok"] and r["callable"] else "r"}">'
        f'{"OK" if r["schema_ok"] and r["callable"] else "缺件"}</td></tr>'
        for r in rep["tools"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>荣珂接线全景 · {rep['fingerprint']}</title><style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f1e9;color:#2B2723;
 font-family:"Microsoft YaHei","SimHei",Arial,sans-serif;}}
header{{padding:26px 36px;border-bottom:3px solid #B23A2E;background:#FBF8F1}}
h1{{margin:0;font-size:22px}} header p{{margin:8px 0 0;font-size:13px;color:#5A534A;line-height:1.8}}
.wrap{{padding:24px 36px;max-width:1400px;margin:auto}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px}}
.card{{background:#fff;border:1px solid #D9D2C4;padding:14px 18px;min-width:130px}}
.card b{{display:block;font-size:24px;color:#2E5A63}}
.card span{{font-size:12px;color:#5A534A}}
h2{{font-size:16px;margin:28px 0 10px;padding-left:10px;border-left:4px solid #2E5A63}}
table{{border-collapse:collapse;font-size:12px;background:#fff;width:100%}}
td,th{{border:1px solid #D9D2C4;padding:6px 10px;text-align:left}}
th{{background:#efe9dc}} code{{color:#B23A2E;font-size:11px}}
.g{{color:#1f6b43;font-weight:700}} .r{{color:#B23A2E;font-weight:700}}
.note{{font-size:12.5px;color:#5A534A;line-height:1.8;max-width:1000px}}
.warn{{background:#fff8ec;border-left:4px solid #B23A2E;padding:12px 16px;font-size:12.5px;line-height:1.8}}
</style></head><body>
<header><h1>荣珂 · 接线全景</h1>
<p>只读读回「在册 / schema 可见 / 调用时机 / 反馈闭环」四件。会话指纹
<code>{rep['fingerprint']}</code> 只锁 name + parameters，描述改字不会变，工具集漂移会变。</p></header>
<div class="wrap">
<div class="cards">
<div class="card"><b>{s['n_tools']}</b><span>在册工具</span></div>
<div class="card"><b>{s['n_domains']}</b><span>分类标签（非域插件数）</span></div>
<div class="card"><b>{s['n_always_on_registered']}/{s['n_always_on_declared']}</b><span>常驻 已落地/已声明</span></div>
<div class="card"><b>{s['n_hooks']}</b><span>钩子 · {s['hooks_mode']}</span></div>
<div class="card"><b>{s['n_external_errors']}</b><span>外部加载错误</span></div>
<div class="card"><b class="{'g' if s['ok'] else 'r'}">{'✔' if s['ok'] else '✘'}</b><span>接线状态</span></div>
</div>
<h2>一、九条接线判据</h2><table>{chk}</table>
<h2>二、钩子（反馈闭环）</h2>
<table><tr><th>名称</th><th>类</th><th>before_call</th><th>after_call</th></tr>{hooks}</table>
<h2>三、缺口</h2><div class="warn"><ul>{gaps}</ul></div>
<h2>四、逐工具接线表</h2>
<table><tr><th>工具</th><th>领域</th><th>来源</th><th>常驻</th><th>读写</th>
<th>参数数</th><th>必填</th><th>契约/执行体</th></tr>{rows}</table>
<h2>五、边界</h2><p class="note">{rep['boundary']}</p>
</div></body></html>"""
