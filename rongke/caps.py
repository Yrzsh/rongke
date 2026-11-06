# -*- coding: utf-8 -*-
"""能力插件 SPI —— 「换行为 = 换预设」的地基。

与 `plugins.py` 的分工（**两条路，不许合并**）：
  · `plugins.py` 是给第三方**加工具 / 图型 / 图注**的老协议，CLI `--plugins`
    台账、`LoadReport.ok/failed` 全保持原样。
  · 本模块是**组装这个 agent 本身**的能力行：判据 / 门 / 契约 / 挂载 /
    上下文 / 工具 / 提示词 / 阶段。它们由预设文件里的 `plugins[]` 行选配。

形状对齐 dsh（DeepSeek Harness）：

    dsh                                    荣珂
    ---------------------------------------------------------------
    - {id, name, config} 插件行             CapRow
    agent preset 容器                       CapPreset（预设文件的 plugins[]）
    bundle → profile → home → personal      assemble(layers) 四层
    zod schema 只在 mount 时校验            config_schema + mount_preset()
    agentPresets.list().broken              MountReport.broken ← **唯一健康判据**
    complete: true 结构性覆盖                op="replace"

四条不许破（都来自实踩）：
  1. **坏件照样返回**，绝不静默滤掉 —— 否则「没装」会伪装成「装好了」。
  2. **必填 config 不给默认值** —— dsh 的 sampleOverCapGlobResults 事故：
     67 项静态检查全绿，两个预设却因 mount 时缺必填项而 broken。
  3. **层栈合并之后才校验** —— 个人层可以写进非法值，合并前校验会放过它。
  4. **冲突行也进 broken** —— 只记在 conflicts 里的话，「唯一判据」就是空话。

旧行为为默认：`Caps.empty()`（没有任何能力行）时所有 `flag()/cfg()` 取调用点
传入的 default，而 default 就是改代码之前的那个常量 ⇒ 逐字节等价。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("metric", "gate", "contract", "mount", "context", "tool", "prompt", "stage")

# 提示段白名单。**声明了一个不存在的段 ≠ 追加了一个空段**：前者是坏预设，
# 后者会被「7 份全绿」盖过去 —— 与 dsh 的必填 config 事故是同一种假成功。
# · 前六个是内核 SYSTEM 已经承载的段的只读别名，内容为空、不重复注入；
#   baseline / strict-v2 里写的正是它们，所以加这道校验不改变旧行为。
# · `stats-checklist` 有独立文本，由 loop.STATS_CHECKLIST 单源定义，按需追加。
PROMPT_SECTION_IDS = frozenset({
    "identity", "tools", "runtime", "safety", "skills", "tool-policy",
    "stats-checklist",
})
INJECTABLE_SECTIONS = frozenset({"stats-checklist"})


# ── 能力契约 ────────────────────────────────────────────────────────────

class Capability:
    """能力单元基类。鸭子类型：第三方实现同名属性也行。

    `config_schema` 形如：
        {"unit": {"type": "str", "required": True, "choices": [...]},
         "min_score": {"type": "int", "min": 0, "max": 100, "default": 90}}
    required=True 的项**不许给 default**。
    """

    id: str = ""
    kind: str = ""
    version: str = "0.0.0"
    config_schema: dict[str, dict] = {}

    def mount(self, cfg: dict, ctx: dict) -> list[str]:
        """挂载。返回它实际提供的能力名清单；抛异常 = broken。"""
        return []

    def provides(self) -> list[str]:
        return []

    #: 外部能力**自带**的提示段 {段名: 正文}。用类属性（不依赖 mount/config），
    #: 挂载期就能校验「预设点名的段有没有人真提供」。内置能力留空 ⇒ 旧行为不变。
    SECTION_TEXTS: dict[str, str] = {}


_REGISTRY: dict[str, type[Capability]] = {}
_BUILTIN_LOADED = False
_EXTERNAL_LOADED = False
_EXTERNAL_ERRORS: list[dict] = []
_EXTERNAL_FILES: list[str] = []
_EXTERNAL_NAMES: list[str] = []


def register(name: str):
    """把 `@rongke/xxx` 这样的行名解析到一个能力类。"""

    def deco(cls):
        if name in _REGISTRY:
            raise ValueError(f"能力名重复注册：{name}")
        _REGISTRY[name] = cls
        return cls

    return deco


def external_cap_dirs() -> list[Path]:
    """外部能力目录（按顺序，对齐 dsh 的 bundle / home / personal 三层感）：

      1. 随库 `rongke/caps_ext/` —— 项目自带、跟 git 走的能力包
      2. 个人 `~/.rongke/caps/`   —— 用户自己装的能力，不进仓库
      3. `RONGKE_CAP_EXT`        —— 临时试验目录，`os.pathsep` 分隔多个

    不存在的目录直接跳过 ⇒ 没装外部能力时**一行额外行为都没有**。
    """
    out: list[Path] = [Path(__file__).resolve().parent / "caps_ext",
                       Path.home() / ".rongke" / "caps"]
    for raw in str(os.environ.get("RONGKE_CAP_EXT", "") or "").split(os.pathsep):
        if raw.strip():
            out.append(Path(raw.strip()).expanduser())
    seen: set[str] = set()
    dirs: list[Path] = []
    for p in out:
        key = str(p)
        if key not in seen and p.is_dir():
            seen.add(key)
            dirs.append(p)
    return dirs


def ensure_external_loaded() -> list[dict]:
    """★ 吸收第三方长处的唯一入口：把外部 `.py` 里的 `@register` 装进注册表。

    导入失败（语法错、重复注册名、缺依赖）**不静默**：记进 `_EXTERNAL_ERRORS`，
    由 `mount_preset()` 转成 `MountResult(ok=False)` ⇒ 健康仍只认 `broken`。
    只扫一次；`reset_external_loader()` 仅供测试换目录时用。
    """
    global _EXTERNAL_LOADED
    if _EXTERNAL_LOADED:
        return list(_EXTERNAL_ERRORS)
    _EXTERNAL_LOADED = True
    for d in external_cap_dirs():
        for i, f in enumerate(sorted(d.glob("*.py"))):
            if f.name.startswith("_"):
                continue
            try:
                mod = f"_rongke_ext_{abs(hash(str(f)))}_{i}"
                spec = importlib.util.spec_from_file_location(mod, f)
                if spec is None or spec.loader is None:
                    raise ImportError(f"无法为 {f} 建立 import spec")
                m = importlib.util.module_from_spec(spec)
                sys.modules[mod] = m
                _before = set(_REGISTRY)
                spec.loader.exec_module(m)
                _EXTERNAL_NAMES.extend(set(_REGISTRY) - _before)
                _EXTERNAL_FILES.append(str(f))
            except Exception as e:                          # noqa: BLE001
                _EXTERNAL_ERRORS.append(
                    {"file": str(f), "error": f"{type(e).__name__}: {e}"})
    return list(_EXTERNAL_ERRORS)


def available_sections() -> list[str]:
    """所有已注册能力**自带**的提示段名（挂载期校验用，纯查类属性）。"""
    ensure_external_loaded()
    names: set[str] = set()
    for cls in _REGISTRY.values():
        names.update((getattr(cls, "SECTION_TEXTS", {}) or {}).keys())
    return sorted(names)


def reset_external_loader() -> None:
    """仅供测试：换 `RONGKE_CAP_EXT` 后重新扫。生产代码不许调。

    必须把上一轮外部文件注册的名字撤掉，否则重扫会撞「能力名重复注册」——
    那是**测试夹具自己造的假红灯**，不是产品缺陷。
    """
    global _EXTERNAL_LOADED, _EXTERNAL_ERRORS, _EXTERNAL_FILES
    for n in _EXTERNAL_NAMES:
        _REGISTRY.pop(n, None)
    _EXTERNAL_NAMES.clear()
    _EXTERNAL_LOADED = False
    _EXTERNAL_ERRORS = []
    _EXTERNAL_FILES = []


def resolve(name: str) -> type[Capability] | None:
    """★ 唯一 resolve 判据：解析不到就是 broken。

    不查文件在不在、不查 JSON 能不能解析 —— 与 dsh 的 health 判据同口径。
    `caps_builtin` 在这里 lazy import，避免 caps ↔ caps_builtin 循环导入；
    外部能力目录也在这一刻扫 ⇒「丢一个 .py 进 caps_ext」就能被预设点名。
    """
    global _BUILTIN_LOADED
    if not _BUILTIN_LOADED:
        from . import caps_builtin  # noqa: F401
        _BUILTIN_LOADED = True
    ensure_external_loaded()
    return _REGISTRY.get(name)


def registered_names() -> list[str]:
    resolve("")            # 触发内置注册
    return sorted(_REGISTRY)


def schema_check(schema: dict, cfg: dict) -> list[str]:
    """返回错误清单（空 = 合法）。零外部依赖的 mount 时校验。"""
    errs: list[str] = []
    if not isinstance(cfg, dict):
        return [f"config 必须是对象，实际是 {type(cfg).__name__}"]
    for k, rule in (schema or {}).items():
        if k not in cfg:
            if rule.get("required"):
                errs.append(f"缺必填 config.{k}（{rule.get('hint') or '无默认值，必须显式写'}）")
            continue
        v = cfg[k]
        want = rule.get("type")
        ok = {
            "str": lambda x: isinstance(x, str),
            "int": lambda x: isinstance(x, int) and not isinstance(x, bool),
            "float": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "bool": lambda x: isinstance(x, bool),
            "list": lambda x: isinstance(x, list),
            "dict": lambda x: isinstance(x, dict),
        }.get(want, lambda x: True)(v)
        if not ok:
            errs.append(f"config.{k} 类型应为 {want}，实际是 {type(v).__name__}: {v!r}")
            continue
        if "choices" in rule and v not in rule["choices"]:
            errs.append(f"config.{k}={v!r} 不在允许值 {rule['choices']}")
        if want in ("int", "float"):
            if "min" in rule and v < rule["min"]:
                errs.append(f"config.{k}={v!r} 小于下限 {rule['min']}")
            if "max" in rule and v > rule["max"]:
                errs.append(f"config.{k}={v!r} 大于上限 {rule['max']}")
        if want == "list" and "item_type" in rule:
            it = rule["item_type"]
            bad = [x for x in v if not isinstance(x, {"str": str, "int": int}[it])]
            if bad:
                errs.append(f"config.{k} 有非 {it} 元素：{bad[:3]}")
    unknown = sorted(set(cfg) - set(schema or {}))
    if unknown:
        errs.append(f"config 有不认得的键 {unknown}（schema 只认 {sorted(schema or {})}）")
    return errs


def schema_discipline() -> list[str]:
    """元判据：所有 required 项都**不许**带 default。防 dsh 那次事故重演。"""
    bad = []
    resolve("")
    for name, cls in _REGISTRY.items():
        for k, rule in (cls.config_schema or {}).items():
            if rule.get("required") and "default" in rule:
                bad.append(f"{name}.{k} 是 required 却给了 default")
    return bad


# ── 预设与层栈 ──────────────────────────────────────────────────────────

@dataclass
class CapRow:
    id: str
    name: str
    config: dict = field(default_factory=dict)
    op: str = "insert"          # insert | patch | replace
    override: bool = False
    source: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "config": dict(self.config),
                "op": self.op, "override": self.override, "source": self.source}


@dataclass
class CapPreset:
    id: str
    name: str = ""
    order: int = 100
    rows: list[CapRow] = field(default_factory=list)


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v        # 非 dict 整体替换（与 config._deep_merge 同口径）
    return out


def assemble(layers: list[dict]) -> tuple[CapPreset, list[str]]:
    """把 base 层 → 预设层 → 个人覆盖 叠成一棵预设树。

    **合并之后不校验**，校验统一留给 mount_preset —— 一个预设只在一个地方判健康。
    """
    rows: dict[str, CapRow] = {}
    conflicts: list[str] = []
    meta: dict[str, Any] = {"id": "unnamed", "name": "", "order": 100}
    for li, layer in enumerate(layers):
        src = str(layer.get("source") or f"layer[{li}]")
        for k in ("id", "name", "order"):
            if k in layer:
                meta[k] = layer[k]
        for raw in layer.get("plugins") or []:
            if not isinstance(raw, dict):
                conflicts.append(f"{src}: 插件行不是对象：{raw!r}")
                continue
            op = str(raw.get("op") or "insert")
            rid = str(raw.get("id") or "").strip()
            if not rid:
                conflicts.append(f"{src}: 插件行缺 id")
                continue
            if op not in ("insert", "patch", "replace"):
                conflicts.append(f"{src}: {rid} 的 op={op!r} 不认得")
                continue
            if op == "replace":
                if rid in rows and not raw.get("override"):
                    conflicts.append(f"{src}: replace 撞上已存在的 {rid}（要覆盖请显式 override）")
                    continue
                rows[rid] = CapRow(rid, str(raw.get("name") or ""),
                                   dict(raw.get("config") or {}),
                                   "replace", bool(raw.get("override")), src)
                continue
            if op == "patch":
                if rid not in rows:
                    conflicts.append(f"{src}: patch 的 {rid} 不存在")
                    continue
                cur = rows[rid]
                cur.config = _deep_merge(cur.config, dict(raw.get("config") or {}))
                if raw.get("name"):
                    cur.name = str(raw["name"])
                cur.source += f" → {src}"
                continue
            if rid in rows and not raw.get("override"):
                conflicts.append(f"{src}: 插件行 id 重复 {rid}（dsh 的 Duplicate agent preset）")
                continue
            rows[rid] = CapRow(rid, str(raw.get("name") or ""),
                               dict(raw.get("config") or {}),
                               "insert", bool(raw.get("override")), src)
    return CapPreset(str(meta["id"]), str(meta["name"]), int(meta["order"]),
                     list(rows.values())), conflicts


# ── mount：唯一健康判据 ─────────────────────────────────────────────────

@dataclass
class MountResult:
    id: str
    name: str
    ok: bool
    kind: str = ""
    provides: list[str] = field(default_factory=list)
    error: str = ""
    hint: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "ok": self.ok, "kind": self.kind,
                "provides": list(self.provides), "error": self.error,
                "hint": self.hint, "source": self.source}


class MountReport:
    """一次挂载的全部结局。**失败项留在 results 里，不滤掉。**"""

    def __init__(self, results: list[MountResult], conflicts: list[str]):
        self.results = results
        self.conflicts = conflicts

    @property
    def ok(self) -> list[MountResult]:
        return [r for r in self.results if r.ok]

    @property
    def broken(self) -> list[MountResult]:
        """★ 唯一健康判据：不等于「文件在不在」「JSON 能不能解析」。"""
        return [r for r in self.results if not r.ok]

    @property
    def healthy(self) -> bool:
        return not self.broken and not self.conflicts

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for r in self.ok:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
        return (f"挂载 {len(self.results)} 行 / 好 {len(self.ok)} / 坏 {len(self.broken)} "
                f"/ 层栈冲突 {len(self.conflicts)}｜按类 {json.dumps(kinds, ensure_ascii=False)}")

    def to_dict(self) -> dict:
        return {"healthy": self.healthy, "summary": self.summary(),
                "results": [r.to_dict() for r in self.results],
                "broken": [r.to_dict() for r in self.broken],
                "conflicts": list(self.conflicts)}


def mount_preset(preset: CapPreset, conflicts: list[str] | None = None,
                 ctx: dict | None = None) -> MountReport:
    """resolve → config 校验 → mount。任一步失败即 broken，且保留原话。"""
    ctx = ctx if ctx is not None else {}
    ctx.setdefault("capabilities", {})
    results: list[MountResult] = []
    # ★ 外部能力导入失败也算坏件。不在这里记，坏 .py 会「装不上但预设全绿」——
    #   与 dsh 的必填 config 事故同族：唯一健康判据必须是 broken。
    for bad in ensure_external_loaded():
        results.append(MountResult(
            "<外部能力>", bad["file"], False, kind="external",
            error=f"外部能力模块导入失败：{bad['error']}",
            hint="修好该 .py，或把它移出 caps_ext / RONGKE_CAP_EXT 目录"))
    for row in preset.rows:
        cls = resolve(row.name)
        if cls is None:
            results.append(MountResult(row.id, row.name, False, source=row.source,
                                       error="模块名 resolve 不到（能力没注册或名字写错）",
                                       hint="检查 name 是否与 caps_builtin 的注册名一致"))
            continue
        errs = schema_check(cls.config_schema, row.config)
        if errs:
            results.append(MountResult(row.id, row.name, False, kind=cls.kind,
                                       source=row.source, error="；".join(errs),
                                       hint=f"补齐 {row.name} 的 config，"
                                            f"schema={json.dumps(cls.config_schema, ensure_ascii=False)}"))
            continue
        cfg = {k: row.config.get(k, rule.get("default"))
               for k, rule in (cls.config_schema or {}).items()
               if k in row.config or "default" in rule}
        try:
            inst = cls()
            got = list(inst.mount(cfg, ctx) or [])
            ctx["capabilities"].setdefault(cls.kind, []).append((row.id, inst, cfg))
            results.append(MountResult(row.id, row.name, True, cls.kind,
                                       got or list(inst.provides()), source=row.source))
        except Exception as e:                                   # noqa: BLE001
            results.append(MountResult(row.id, row.name, False, kind=cls.kind,
                                       source=row.source,
                                       error=f"mount 抛异常：{type(e).__name__}: {e}"))
    # ★ 层栈冲突也进 broken：冲突行在 assemble 阶段就被挡在 rows 之外，
    #   只记 conflicts 的话，调用方还得再读第二个字段才算看得全。
    for c in (conflicts or []):
        results.append(MountResult("<层栈冲突>", "", False, error=c,
                                   hint="检查层栈 id 是否重复、replace 是否显式 override"))
    # ★ 点名了外部段、却没挂提供它的能力 ⇒ 也是坏件（拦沉默，不拦选择）。
    #   段名在白名单里 ≠ 正文有人给；「读不到」必须被记账，不许假装已注入。
    _declared: list[str] = []
    for row in preset.rows:
        if row.id == "prompt":
            _declared = list((row.config or {}).get("sections") or [])
    if _declared:
        _provided: set[str] = set()
        for _bucket in ctx["capabilities"].values():
            for _rid, _inst, _cfg in _bucket:
                _provided.update((getattr(_inst, "SECTION_TEXTS", {}) or {}).keys())
        _external_known = set(available_sections())
        # 只查「合法外部段却没人提供」这一种。拼错的段名已由
        # PromptSections 的白名单打红，这里再报一遍是重复记账（假红）。
        _missing = [s for s in _declared
                    if s in _external_known and s not in _provided]
        if _missing:
            results.append(MountResult(
                "<提示段>", "", False, kind="prompt",
                error=f"点名了外部提示段 {_missing!r}，但本次挂载没有任何能力提供它",
                hint="在 plugins[] 里加一行提供该段的能力，或去掉这个段名"))
    return MountReport(results, list(conflicts or []))


# ── 内核消费接口 ────────────────────────────────────────────────────────

class Caps:
    """一次挂载结果给内核用的只读视图。

    所有取值都走 `(行 id, 键, 旧值默认值)` 三元组：没有这一行 / 这一行 broken
    ⇒ 取旧值 ⇒ 等价于改代码之前。这就是「旧行为为默认」的物理形态。
    """

    def __init__(self, preset_id: str = "", report: MountReport | None = None,
                 by_id: dict | None = None,
                 registered_tools: list[str] | None = None,
                 preset_stages: list[str] | None = None):
        self.preset_id = preset_id
        self.report = report or MountReport([], [])
        self.by_id: dict[str, tuple[Any, dict]] = by_id or {}
        # ★ 本次挂载**新注册**的工具名。预设绑定的是「这一次 run」，跑完必须
        #   按这份清单撤销 —— 否则长命单例会把上一场的工具泄漏给下一场任务。
        #   只记「原本没有、这次才加」的，绝不误撤别人注册的同名工具。
        self.registered_tools: list[str] = list(registered_tools or [])
        # ★ 预设声明的「这一路活要经过哪几道工序」。每道工序 = 一个专属
        #   skill（stages.py 的 Stage 表），所以这就是「每种任务所需 skill
        #   不一样」的机器形态。baseline 为空 ⇒ 完全退回话术关键词推断。
        #   注意它**不是**一份要全量塞进上下文的清单：清单只用来选工序，
        #   协议正文仍由模型按 `load_skill` 按需读（渐进式披露）。
        self.preset_stages: list[str] = list(preset_stages or [])

    @classmethod
    def empty(cls) -> "Caps":
        return cls("")

    @property
    def broken(self) -> list[MountResult]:
        return self.report.broken

    @property
    def healthy(self) -> bool:
        return self.report.healthy

    def prompt_texts(self, section_ids) -> dict[str, str]:
        """收集**本次挂载**的能力自带的提示段正文。

        只有真被 `plugins[]` 行挂载的能力才在 `by_id` 里 ⇒ 没选的能力不会
        偷偷往 system 里塞字。内置 `stats-checklist` 仍由 loop 单源兜底。
        """
        want = list(section_ids or [])
        if not want:
            return {}
        out: dict[str, str] = {}
        for _rid, (inst, _cfg) in self.by_id.items():
            for k, v in (getattr(inst, "SECTION_TEXTS", {}) or {}).items():
                if k in want and isinstance(v, str) and v.strip():
                    out[k] = v
        return out

    # ── 通用贡献点（2026-11-05 内核优化）───────────────────────────
    # 学自 dsh 的「按 slot 遍历层栈」与 Anthropic 的「能力自带指令」：
    # 旧内核是「按 8 个写死的行 id 去问某个能力在不在」，于是加一种新行为
    # 就必须改一次内核。这里反过来 —— 遍历**本次已挂载的全部能力**，问
    # 它们「你贡献了这个 slot 吗」。新行为 = 新能力 + 预设点名，零改内核。
    #
    # 只认 `by_id` 里的实例 ⇒ 没被点名的能力不会偷偷生效；类属性与
    # `mount()` 里写的实例属性都读得到（getattr 对两者一视同仁）。
    def contributions(self, attr: str) -> list[tuple[str, Any]]:
        out: list[tuple[str, Any]] = []
        for rid, (inst, _cfg) in self.by_id.items():
            if inst is None:
                continue
            v = getattr(inst, attr, None)
            if v:
                out.append((rid, v))
        return out

    def system_sections(self) -> list[str]:
        """本次要保留的可选 SYSTEM 节（去重保序）。空 = 走全量 baseline。"""
        out: list[str] = []
        for _rid, v in self.contributions("SYSTEM_SECTIONS"):
            for s in (v if isinstance(v, (list, tuple, set)) else [v]):
                s = str(s)
                if s not in out:
                    out.append(s)
        return out

    def keep_sections(self) -> list[str]:
        """`complete:true` 结构性覆盖时仍要保留的可选 SYSTEM 节（去重保序）。

        与 `system_sections()` 分工：那边是「按任务点名要哪些节」，这边是
        「已经决定只发骨架了，但哪几节不许丢」。两条都走通用贡献点，
        外部能力挂同名属性即可参与，不需要改内核。
        """
        out: list[str] = []
        for _rid, v in self.contributions("SYSTEM_KEEP_SECTIONS"):
            for s in (v if isinstance(v, (list, tuple, set)) else [v]):
                s = str(s)
                if s not in out:
                    out.append(s)
        return out

    def system_complete(self) -> str:
        """能力自带的结构性覆盖全文（dsh `completeSection` 同款）。"""
        vals = [str(v) for _rid, v in self.contributions("SYSTEM_COMPLETE")
                if isinstance(v, str) and v.strip()]
        return vals[-1] if vals else ""

    def complete_flag(self) -> bool:
        """`prompt` 行的 `complete=true` 是否要求结构性覆盖。

        ⚠️ 读的是**行 id 的 config**，不是 `prompt.sections` 那份声明式段名
        清单 —— baseline 的 sections 是 `identity/tools/runtime/safety/skills`，
        与 SYSTEM 的语义节名完全不是一回事，混用会把段名当节名喂进装配器。
        """
        return bool(self.flag("prompt", "complete", False))

    def extra_rules(self) -> list[tuple]:
        """能力贡献的额外内核判据，与 `gate._EXTRA_RULES` 同形状。"""
        out: list[tuple] = []
        for _rid, v in self.contributions("EXTRA_RULES"):
            for row in v:
                if isinstance(row, (tuple, list)) and len(row) == 4:
                    out.append(tuple(row))
        return out

    def essential_tools(self) -> tuple[str, ...]:
        """本次挂载的能力声明「工具预算裁剪时不许裁掉」的工具名。

        ★ 与 `EXTRA_RULES` 同一条定律：内核不再把必留名单写死成两个交付
          通道，而是遍历已挂载能力问「你要保住哪些工具」。检索插件据此
          把 `literature_search` / `verify_citation` 这一串保住 —— 于是
          「不同任务 = 不同工具箱」真的由插件决定，而不是靠内核猜。
          baseline 没有任何贡献 ⇒ 返回空元组 ⇒ 与旧表逐字等价。
        """
        out: list[str] = []
        for _rid, v in self.contributions("ESSENTIAL_TOOLS"):
            for n in (v if isinstance(v, (list, tuple, set)) else [v]):
                n = str(n)
                if n and n not in out:
                    out.append(n)
        return tuple(out)

    def cfg(self, rid: str) -> dict:
        return dict(self.by_id.get(rid, (None, {}))[1])

    def inst(self, rid: str):
        return self.by_id.get(rid, (None, {}))[0]

    def flag(self, rid: str, key: str, default=None):
        v = self.cfg(rid).get(key, default)
        return default if v is None else v

    def metrics(self) -> list[str]:
        return list(self.flag("gate", "measured_metrics", []) or [])

    def bracket_pairs(self) -> list[str]:
        return list(self.flag("citation", "bracket_pairs", ["[n]"]) or [])

    def invalidate_mount_cache(self) -> None:
        """工具清单变动后，让 `refresh=once` 的 schema 缓存失效。

        不清的话，缓存里那串 schema 还带着刚撤掉的工具 —— registry 干净了，
        模型看到的却还是上一场的 67 个工具，是另一种形式的泄漏。
        """
        m = self.inst("mount")
        if m is not None and hasattr(m, "_cache"):
            m._cache = None
            m._last_stage = None

    def to_dict(self) -> dict:
        return {"preset": self.preset_id, "healthy": self.healthy,
                "rows": [r.to_dict() for r in self.report.results],
                "broken": [r.to_dict()["error"] for r in self.report.broken],
                "registered_tools": list(self.registered_tools),
                "stages": list(self.preset_stages),
                "values": {rid: dict(c) for rid, (_, c) in self.by_id.items()}}


def _layer_from_file(path: Path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d["source"] = Path(path).name
    return d


def load_capability_preset(pid: str, *, directory: Path | None = None,
                           home: Path | None = None,
                           ctx: dict | None = None) -> Caps:
    """装载一份预设并**挂载**。判健康只认 `MountReport.broken`。

    层顺序：base 层（可选） → 预设层 → 个人覆盖层（`~/.rongke/config.json`
    的 `presets.<id>.plugins`）。个人层未写 `op` 的行默认按 `patch` 处理。
    """
    from .preset import presets_dir, personal_layer, list_presets, PresetError

    d = Path(directory) if directory is not None else presets_dir()
    # ① 复用 preset.py 的 (good, broken) 与 (order, id) 排序 —— 它本来就是死代码，
    #    这里把它接活；坏件不静默滤掉。
    good, broken = list_presets(d)
    matches = [p for p in good if p.id == pid]
    if not matches:
        return Caps(pid, MountReport([
            MountResult(pid, "", False,
                        error=f"预设 {pid!r} 不可用：" +
                        (f"装载失败 {broken}" if broken else
                         f"目录里没有这个 id（可用 {[p.id for p in good]}）"))], []))
    for stem, why in broken:
        if stem == pid:
            return Caps(pid, MountReport([
                MountResult(pid, "", False, error=f"预设文件校验失败：{why}")], []))

    path = d / f"{pid}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    layers: list[dict] = []
    base = str(raw.get("base") or "").strip()
    if base:
        bp = d / f"{base}.json"
        if not bp.is_file():
            return Caps(pid, MountReport([
                MountResult(pid, "", False,
                            error=f"base 层 {base!r} 不存在（{bp}）")], []))
        layers.append(_layer_from_file(bp))
    layers.append(_layer_from_file(path))
    try:
        personal = personal_layer(home)
    except PresetError as e:
        return Caps(pid, MountReport([
            MountResult(pid, "", False, error=f"个人层不合法：{e}")], []))
    layer = personal.get(pid)
    if layer is not None:
        if not isinstance(layer, dict):
            return Caps(pid, MountReport([
                MountResult(pid, "", False,
                            error=f"个人层 presets.{pid} 必须是对象")], []))
        rows = []
        for r in (layer.get("plugins") or []):
            r = dict(r) if isinstance(r, dict) else {}
            r.setdefault("op", "patch")
            rows.append(r)
        if rows:
            layers.append({"source": "personal", "plugins": rows})

    cap_preset, conflicts = assemble(layers)
    cap_preset.id = pid
    cap_preset.name = str(matches[0].name or pid)
    # ★ ctx 必须由这里创建并传进去：mount_preset 往它里面写 capabilities，
    #   若传 None 它会在自己作用域里另建一个 dict，外面就永远读不到实例。
    live_ctx: dict = dict(ctx or {})
    report = mount_preset(cap_preset, conflicts, ctx=live_ctx)
    by_id = {}
    for _kind, items in live_ctx.get("capabilities", {}).items():
        for rid, inst, cfg in items:
            by_id[rid] = (inst, cfg)
    # ★ 从**同一个 live_ctx** 里读回挂载期登记的工具名：它是 dict(ctx) 的浅
    #   拷贝，mount 写进去的东西不会出现在调用方那个 ctx 上，只有这里看得到。
    #   坏件也照样带上 —— 中途失败时更要能清理掉已经注册的工具。
    #
    # ★ 工序清单从**校验过的 Preset 对象**取（不是 raw JSON）：`preset.validate()`
    #   已经逐个核过 id 必须存在于 STAGE_BY_ID，写错一个字整份预设就 broken，
    #   不会出现「声明了却查无此工序」的静默失效。个人覆盖层只管 plugins，
    #   不改工序 —— 这样「这轮按哪套工序跑」始终只有一个来源。
    from .stages import STAGE_BY_ID as _STAGE_BY_ID, available as _available
    declared = [s for s in list(getattr(matches[0], "stages", []) or [])
                if s in _STAGE_BY_ID]
    # ★ 2026-11-04：只验「工序 id 存在」还不够 —— 工序名是代码里的常量，
    #   它指向的 **skill 是磁盘上的文件**。客户机没带 vendor/skills、
    #   或用户没装 ~/.agents/skills 时，工序照样验过、预设照样全绿，
    #   硬门却要求模型去 load_skill 一个根本读不到的技能：
    #   「读不到」被当成「没做」，一场本来能交的活会被自己的门罚死。
    #   所以挂载期就把这条读回来，缺件直接进 broken（唯一健康判据）。
    _have = _available()
    _missing = [f"{s} → {_STAGE_BY_ID[s].skill}" for s in declared
                if _STAGE_BY_ID[s].skill not in _have]
    if _missing:
        report.results.append(MountResult(
            pid, "<按任务挂载 skill>", False, kind="stage", source="presets",
            error="预设声明的工序，其专属技能读不到：" + "；".join(_missing),
            hint="检查 vendor/skills 是否随包分发、~/.agents/skills 是否装有该技能；"
                 "缺件时不要把 stage.hard 打开"))
    caps = Caps(pid, report, by_id,
                registered_tools=list(live_ctx.get("registered_tools") or []),
                preset_stages=declared)
    # 结构性覆盖只允许一个来源。两个能力都声称「这份 system 由我说了算」
    # 时静默挑一个，等于用户配了两份却永远看不到其中一份 ⇒ 拦沉默，
    # 且仍只认 `MountReport.broken` 这一个健康出口。
    _owners = caps.contributions("SYSTEM_COMPLETE")
    if len(_owners) > 1:
        report.results.append(MountResult(
            pid, "<system 骨架>", False, kind="prompt", source="presets",
            error="有多个能力同时提供 SYSTEM_COMPLETE：" +
                  "、".join(rid for rid, _ in _owners),
            hint="结构性覆盖只能有一个来源；其余能力改用 SYSTEM_SECTIONS 贡献节"))
    return caps


def merge_preset_stages(contract, declared: list[str]) -> dict:
    """把预设点名的工序**只加不删**地并进契约，按流水线顺序归位。

    抽成模块级纯函数是为了可测：判据要能真调它、真读回 `contract.stages`，
    而不是去 `inspect.getsource()` 里扫一行字符串就宣布「接好了」。

    为什么是并集不是替换：用户原话里推出来的工序是他说过要做的事，预设
    不能替他删；预设只补「这类任务按流程本来还该有」的那几道。
    """
    from .stages import STAGE_BY_ID, STAGE_ORDER

    old = list(getattr(contract, "stages", None) or [])
    new = list(old)
    for sid in declared:
        if sid in STAGE_BY_ID and sid not in new:
            new.append(sid)
    new.sort(key=lambda s: STAGE_ORDER.get(s, 999))
    changed = new != old
    if changed and hasattr(contract, "stages"):
        contract.stages = new
    return {
        "declared": list(declared),
        "added": [s for s in new if s not in old],
        "stages": list(new),
        "skills": [STAGE_BY_ID[s].skill for s in new if s in STAGE_BY_ID],
        "changed": changed,
    }


def all_presets_health(*, directory: Path | None = None,
                       home: Path | None = None) -> dict:
    """给 CLI / `--presets` / `GET /api/presets` 用的只读健康清单。"""
    from .preset import presets_dir, list_presets

    d = Path(directory) if directory is not None else presets_dir()
    good, broken = list_presets(d)
    caps = {}
    for p in good:
        c = load_capability_preset(p.id, directory=d, home=home)
        caps[p.id] = {"healthy": c.healthy, "rows": len(c.report.results),
                      "broken": [r.error for r in c.report.broken],
                      "summary": c.report.summary()}
    items = [{"id": p.id, "name": p.name, "order": p.order,
              "scenes": list(p.scenes), **caps.get(p.id, {})} for p in good]
    bad = [{"id": stem, "why": why} for stem, why in broken]
    return {"good": items, "broken": bad,
            "healthy": not bad and all(x.get("healthy") for x in items)}
