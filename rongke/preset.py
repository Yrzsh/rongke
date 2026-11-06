# -*- coding: utf-8 -*-
"""预设装载器 —— 把「这一场是谁在干活」收成一份可校验的数据。

## 它补的是哪一处缺失

内核原本**没有身份这一层**：`rongke/` 里 `preset|profile|persona` 零命中，CLI /
Web / `Rongke.run()` 三处受理口都没有选身份的位置。事实上的"模式"只是
`TaskContract` 上三个具名布尔（`needs_writing/needs_figure/deck`）+ 三张关键词网，
方向是**内核从目标文本猜**，而不是人选定。猜的代价已经量过：真实论文目标里
8/11 条不被这张网接住，而 `TaskContract.fallback()` 的 `needs_writing` **只由这张表
决定** ⇒ 技能门与稿面体检门同时关着，且没有任何地方声明"门现在关着"。
人钉一个预设就把这条补上了：场景不再靠文本运气。

## 形状借的是 dsh，语义刻意不借

dsh（DeepSeek Harness）的一个预设是 `{id, name, description, order, plugins[]}`
—— 一组插件行的名字。**这里只借它的条目形状**，不借它的 `insert:` roster 组合语义：
那套 overlay/patch 引擎会和 `config._deep_merge` 并存成两处真相。
所以预设是一**份声明文件**，字段全是荣珂自己的轴（场景 / 阶段 / 挂载域 / 阈值 /
提示块 / 范例）。

★ 一处刻意的不同：dsh 把 `broken` 的预设从选择器里**静默丢掉**（`registry.js` 的
roster 直接把坏项滤掉）。荣珂不许这么办 —— 本项目的纪律是"静默失败"必须被印出来
（见 `README` 与 `mounting_decision` 的 reason）。坏预设照样返回，带着**为什么坏**，
由调用方打印；绝不悄悄降级成"没配预设"。

## 单源：这个模块不持有任何枚举

场景名从 `contract.SCENE_KEYS` 取，阶段名从 `stages.STAGE_BY_ID` 取，挂载域与提示块
由**调用方把当下真在用的集合传进来**（域来自注册表，块来自 contract 的注入函数表）。
在这里再抄一份枚举，就是第二处真相 —— 而且抄的这份会漂：`stages` 长一个、域改一个名，
校验器都还会绿灯。

## 本模块当前不接任何模型可见面

它只是装载与校验；把预设接到挂载决策、首条消息、阈值物化是后续步骤，
而且其中会改"模型看到什么"的那些一律单独出 diff。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import _deep_merge, project_root
from .contract import SCENE_KEYS
from .stages import STAGE_BY_ID

#: 预设文件里允许出现的顶层键。**未知键 ⇒ 整份拒绝**（不是忽略）：
#: 拼错的键会被 JSON 安静吞掉，而现场没人知道那条要求从来没生效过。
FIELDS: tuple[str, ...] = (
    "id", "name", "description", "order",
    "scenes", "stages", "mount", "config", "prompt", "exemplars", "tier",
    # ★ 2026-11-04 能力插件化：预设不再只是「装阈值的容器」，它同时是一份
    #   **能力行清单**（`caps.py` 消费）。`base` 让 patch 预设可以叠在另一份
    #   预设之上，层栈合并之后才由 mount 统一判健康（见 caps.mount_preset）。
    "plugins", "base",
)

#: `plugins[]` 里每行允许的键。未知键同样整份拒绝（拼错的配置必须被看见）。
ROW_FIELDS: tuple[str, ...] = ("id", "name", "config", "op", "override")
ROW_OPS: tuple[str, ...] = ("insert", "patch", "replace")

#: `prompt` 小节里允许的键。
PROMPT_FIELDS: tuple[str, ...] = ("block",)

#: `config` 小节允许覆盖的配置段（与 `Config` 上真有的小节对齐）。
CONFIG_SECTIONS: tuple[str, ...] = ("deck", "figure", "kb", "loop")

_REQUIRED: tuple[str, ...] = ("id", "scenes")


class PresetError(ValueError):
    """预设不合法。带**可直接印给人看**的一句话理由。"""


def presets_dir(root: Path | None = None) -> Path:
    """随库发布的预设目录（单源）。"""
    return (root or project_root()) / "rongke" / "presets"


@dataclass
class Preset:
    id: str
    scenes: list[str] = field(default_factory=list)
    name: str = ""
    description: str = ""
    order: int = 100
    stages: list[str] = field(default_factory=list)
    mount: list[str] = field(default_factory=list)
    config: dict = field(default_factory=dict)
    block: str = ""
    exemplars: list[str] = field(default_factory=list)
    tier: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                "order": self.order, "scenes": list(self.scenes),
                "stages": list(self.stages), "mount": list(self.mount),
                "config": dict(self.config), "prompt": {"block": self.block},
                "exemplars": list(self.exemplars), "tier": self.tier}


def _bad_str_list(v, label: str) -> str:
    if not isinstance(v, list):
        return f"{label} 必须是列表，实际是 {type(v).__name__}"
    for x in v:
        if not isinstance(x, str) or not x.strip():
            return f"{label} 里有非字符串或空项：{x!r}"
    if len(set(v)) != len(v):
        dup = sorted({x for x in v if v.count(x) > 1})
        return f"{label} 里有重复项：{dup}"
    return ""


def validate(data: dict, *, stem: str = "",
             scene_keys: tuple[str, ...] = SCENE_KEYS,
             stage_ids: tuple[str, ...] = tuple(STAGE_BY_ID),
             known_domains: set[str] | None = None,
             known_blocks: set[str] | None = None) -> str:
    """返回空串 = 合法；否则返回一句**能直接印出来**的理由。

    校验只做"这个名字存不存在"，不做"这个组合好不好" —— 后者需要判据，
    而判据该长在门里，不该长在装载器里。
    """
    if not isinstance(data, dict):
        return f"预设必须是 JSON 对象，实际是 {type(data).__name__}"
    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        return f"未知顶层键 {unknown}（允许的只有 {list(FIELDS)}）"
    for k in _REQUIRED:
        if k not in data:
            return f"缺必填键 {k}"
    pid = data["id"]
    if not isinstance(pid, str) or not pid.strip():
        return f"id 必须是非空字符串，实际是 {pid!r}"
    if stem and pid != stem:
        return f"id={pid!r} 与文件名 {stem!r} 不一致（选 id 时读的是文件名）"
    for k in ("name", "description", "tier"):
        if k in data and not isinstance(data[k], str):
            return f"{k} 必须是字符串"
    if "order" in data and (not isinstance(data["order"], int)
                            or isinstance(data["order"], bool)):
        return f"order 必须是整数，实际是 {data['order']!r}"
    for k in ("scenes", "stages", "mount", "exemplars"):
        if k in data:
            why = _bad_str_list(data[k], k)
            if why:
                return why
    for s in data["scenes"]:
        if s not in scene_keys:
            return (f"scenes 里有不认得的场景 {s!r}（当前只有 {list(scene_keys)}）"
                    f" —— 要加新场景请先在 contract.SCENE_KEYS 与消费侧各接一处")
    for st in data.get("stages") or []:
        if st not in stage_ids:
            return f"stages 里有不认得的阶段 {st!r}"
    if known_domains is not None:
        for d in data.get("mount") or []:
            if d not in known_domains:
                return (f"mount 里有没挂上的域 {d!r}（注册表当前只有 "
                        f"{sorted(known_domains)}）—— 挂不上的域不会报错，只会让"
                        f"这一场的工具比预设说的少")
    if "config" in data:
        cfg = data["config"]
        if not isinstance(cfg, dict):
            return "config 必须是对象"
        bad = sorted(set(cfg) - set(CONFIG_SECTIONS))
        if bad:
            return f"config 里有不认得的配置段 {bad}（只有 {list(CONFIG_SECTIONS)}）"
        for sec, sub in cfg.items():
            if not isinstance(sub, dict):
                return (f"config.{sec} 必须是**对象**（如 {{\"min_slides\": 12}}），"
                        f"实际是 {type(sub).__name__} —— 非对象会被整段替换，"
                        f"那一段的其余默认值就没了")
    # ★ 能力行：装载器只验「形状」，真正的 schema 校验留给 mount_preset ——
    #   一个预设只在一个地方判健康，坏行的理由才会集中出现在 `broken` 里。
    if "plugins" in data:
        rows = data["plugins"]
        if not isinstance(rows, list):
            return f"plugins 必须是列表，实际是 {type(rows).__name__}"
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                return f"plugins[{i}] 必须是对象，实际是 {type(row).__name__}"
            unknown_row = sorted(set(row) - set(ROW_FIELDS))
            if unknown_row:
                return f"plugins[{i}] 有未知键 {unknown_row}（允许的只有 {list(ROW_FIELDS)}）"
            if not str(row.get("id") or "").strip():
                return f"plugins[{i}] 缺 id（能力行的身份，不能靠数组下标猜）"
            op = str(row.get("op") or "insert")
            if op not in ROW_OPS:
                return f"plugins[{i}] 的 op={op!r} 不认得（只有 {list(ROW_OPS)}）"
            if not isinstance(row.get("config") or {}, dict):
                return f"plugins[{i}].config 必须是对象"
            if "override" in row and not isinstance(row["override"], bool):
                return f"plugins[{i}].override 必须是布尔"
    if "base" in data:
        b = data["base"]
        if not isinstance(b, str) or not b.strip():
            return f"base 必须是非空字符串（指向另一份预设的 id），实际是 {b!r}"
    if "prompt" in data:
        pr = data["prompt"]
        if not isinstance(pr, dict):
            return "prompt 必须是对象"
        upk = sorted(set(pr) - set(PROMPT_FIELDS))
        if upk:
            return f"prompt 里有未知键 {upk}"
        blk = pr.get("block", "")
        if not isinstance(blk, str):
            return "prompt.block 必须是字符串"
        if blk and known_blocks is not None and blk not in known_blocks:
            return (f"prompt.block={blk!r} 不是内核提供的注入块（当前有 "
                    f"{sorted(known_blocks)}）—— 提示文本住代码里，"
                    f"预设只点名，不许自带散文")
    return ""


def load(path: Path, *, stem: str = "",
         scene_keys: tuple[str, ...] = SCENE_KEYS,
         stage_ids: tuple[str, ...] = tuple(STAGE_BY_ID),
         known_domains: set[str] | None = None,
         known_blocks: set[str] | None = None) -> tuple[Preset | None, str]:
    """读一份预设。返回 `(Preset, "")` 或 `(None, 理由)`。

    解析失败与校验失败**分开报**：前者是 JSON 坏了，后者是内容坏了，
    两者的修法不同。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"读取失败 {Path(path).name}: {e}"
    why = validate(data, stem=stem or Path(path).stem, scene_keys=scene_keys,
                   stage_ids=stage_ids, known_domains=known_domains,
                   known_blocks=known_blocks)
    if why:
        return None, why
    pr = data.get("prompt") or {}
    return Preset(id=str(data["id"]).strip(),
                  name=data.get("name", "") or str(data["id"]).strip(),
                  description=data.get("description", ""),
                  order=int(data.get("order", 100)),
                  scenes=list(data["scenes"]),
                  stages=list(data.get("stages") or []),
                  mount=list(data.get("mount") or []),
                  config={k: dict(v) for k, v in (data.get("config") or {}).items()},
                  block=str(pr.get("block") or ""),
                  exemplars=list(data.get("exemplars") or []),
                  tier=str(data.get("tier") or "")), ""


def list_presets(directory: Path | None = None, *,
                 scene_keys: tuple[str, ...] = SCENE_KEYS,
                 stage_ids: tuple[str, ...] = tuple(STAGE_BY_ID),
                 known_domains: set[str] | None = None,
                 known_blocks: set[str] | None = None
                 ) -> tuple[list[Preset], list[tuple[str, str]]]:
    """列目录里的预设。返回 `(好的, [(id 或文件名, 为什么坏)])`。

    ★ 坏的那一份**照样返回**，绝不悄悄滤掉 —— 调用方要么印出来，要么它就是
      那个"静默失败"本身。排序按 `order`，同值按 id（dsh 的 roster 也是这个 tie-break）。
    """
    d = Path(directory) if directory is not None else presets_dir()
    good: list[Preset] = []
    broken: list[tuple[str, str]] = []
    if not d.is_dir():
        return good, [(str(d), "预设目录不存在")]
    for p in sorted(d.glob("*.json")):
        preset, why = load(p, scene_keys=scene_keys, stage_ids=stage_ids,
                           known_domains=known_domains, known_blocks=known_blocks)
        if preset is None:
            broken.append((p.stem, why))
        else:
            good.append(preset)
    good.sort(key=lambda x: (x.order, x.id))
    return good, broken


def personal_layer(home: Path | None = None) -> dict:
    """取 `~/.rongke/config.json` 里的 `presets` 小节。

    小节**不存在** ⇒ 返回 `{}`；存在而**形状不对** ⇒ `PresetError`。
    这条区分是刻意的：把"我写错了"当成"我没写"，用户会一直以为覆盖在生效。

    ⚠️ 这一层与项目 `config.json` 走同一份 `_deep_merge`，所以那个坑必须写清楚：
      `_deep_merge` 对**非 dict 是整体替换**，个人层写 `stages:[…]` 是**覆盖**
      预设钉死的阶段清单，不是往里追加。
    """
    p = (home or Path.home()) / ".rongke" / "config.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise PresetError(f"个人层解析失败 {p}: {e}") from e
    if not isinstance(data, dict):
        raise PresetError(f"个人层 {p} 顶层必须是对象")
    sec = data.get("presets")
    if sec is None:
        return {}
    if not isinstance(sec, dict):
        raise PresetError(f"个人层 presets 小节必须是对象（每个 id 一项），"
                          f"实际是 {type(sec).__name__}")
    return sec


def merged_dict(raw: dict, personal: dict) -> dict:
    """随库预设 + 个人覆盖，合成一份待校验的 dict。

    ★ 合并**之后**再校验，不是之前：个人层可能写进一个不认得的场景名，
      在合并前校验会放过它、合并后再也没人复核。
    """
    return _deep_merge(dict(raw or {}), dict(personal or {}))


def load_with_layer(path: Path, *, home: Path | None = None,
                    scene_keys: tuple[str, ...] = SCENE_KEYS,
                    stage_ids: tuple[str, ...] = tuple(STAGE_BY_ID),
                    known_domains: set[str] | None = None,
                    known_blocks: set[str] | None = None
                    ) -> tuple[Preset | None, str]:
    """带个人覆盖层的装载。个人层没有这个 id 的条目时，与 `load` 完全等价。

    个人层自己坏了（解析不了 / 小节形状不对）也走**同一条出口**：
    `(None, 理由)`，不抛 —— 调用方只有一种处理方式，就不会漏掉一支。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"读取失败 {Path(path).name}: {e}"
    if not isinstance(data, dict):
        return None, f"预设必须是 JSON 对象，实际是 {type(data).__name__}"
    pid = str(data.get("id") or Path(path).stem)
    try:
        sec = personal_layer(home)
    except PresetError as e:
        return None, str(e)
    layer = sec.get(pid)
    if layer is not None and not isinstance(layer, dict):
        return None, f"个人层 presets.{pid} 必须是对象，实际是 {type(layer).__name__}"
    merged = merged_dict(data, layer or {})
    why = validate(merged, stem=pid, scene_keys=scene_keys, stage_ids=stage_ids,
                   known_domains=known_domains, known_blocks=known_blocks)
    if why:
        return None, why + ("" if not layer else "（含个人层覆盖后的结果）")
    pr = merged.get("prompt") or {}
    return Preset(id=pid, name=merged.get("name", "") or pid,
                  description=merged.get("description", ""),
                  order=int(merged.get("order", 100)),
                  scenes=list(merged["scenes"]),
                  stages=list(merged.get("stages") or []),
                  mount=list(merged.get("mount") or []),
                  config={k: dict(v) for k, v in (merged.get("config") or {}).items()},
                  block=str(pr.get("block") or ""),
                  exemplars=list(merged.get("exemplars") or []),
                  tier=str(merged.get("tier") or "")), ""


def config_patch(preset: Preset | None) -> dict:
    """把预设的阈值小节摊成 `Config.with_overrides` 的入参。

    没预设 / 小节为空 ⇒ 返回 `{}`，调用方据此**什么都不做**。这条纪律要说清：
    预设不是一条覆盖规则，只是一个能装覆盖的地方。
    """
    if preset is None:
        return {}
    return {k: dict(v) for k, v in (preset.config or {}).items()}
