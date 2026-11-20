# -*- coding: utf-8 -*-
"""本场任务的结构单源（RunShape，2026-11-09 根因收敛）。

**为什么有这个文件**：内核原先没有「这一场任务是什么形状」的决策点。
契约在 `_loop` 里才起草（`loop.py:5009`），而步数上限与能力预设在它之前
就已定死（全局常数 40 + 前端胶囊）；收尾答复、大纲门、写作体检又各自
重新推断「盘上有什么正文」「大纲里哪一行算一章」。同一份事实五套真相。

2026-11-09 两场真跑把这五套真相的分歧全量暴露：长综述 40 步耗尽；
`final` 说「本轮没有正文落盘」而工作区有 9495 字稿件；大纲门把
「元数据与 DOI 处理规则」附录节算成第 9 章，判它没绑文献、逼模型返工。

    contract（已冻结） ──> resolve_shape() ──> RunShape（不可变）
                                              ├─ max_steps      → 主循环预算
                                              ├─ preset         → 能力装配
                                              ├─ is_chapter_head→ 大纲门分母
                                              └─ prose_files    → 收尾/复核读回

纪律（与 11-04~11-07 的既有裁决一致）：
  · **显式优先**：用户点了胶囊 / 填了步数，形状路由一律不覆盖。
  · **baseline 等价**：非长综述、问答、短稿一律 `kind="other"` ⇒ 步数、
    事件流、SYSTEM 一字不变。
  · **不加严任何门**：这里只决定「给多少步、用哪把尺子、读哪份稿」，
    不新增判据、不把软门改硬门。
  · **纯函数**：无 IO 副作用（除 `prose_files` 这个只读工作区的单源），
    便于正控/负控两方向都量。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 长综述的步数预算。2026-11-09 实测：40 步时稿写到第 6 章、无文献表、
#: 0 次交付门；第 50 步才交付成功（含 1 次打回补正）。70 = 50 步实证值
#: + 20 步余量给「打回后补正 / 图面门 / 文献核验」。不是拍脑袋的常数：
#: 它由 50 篇文献下限 + 5–8 章分节 + 先出图 + 一次检验的工作量倒推。
LONG_REVIEW_STEPS = 70

#: 写作形状自动挂载的能力预设。显式 `preset` 永远优先。
AUTO_PRESET_WRITING = "paper-writing"

#: 长综述步数耗尽时的「无人值守」兜底。2026-11-09 实测：默认请求两次都
#: 在第 40/70 步弹 `step_limit` 等人点「继续」，没等到就白耗 300 s 再失败。
#: 一次请求要能自己走完 —— 步数将尽时自动续一段，并如实记事件、设硬上限，
#: 绝不无限续。短任务 / 问答一律不启用（`auto_extend_steps=0`）。
AUTO_EXTEND_STEPS = 20
MAX_AUTO_EXTENDS = 2


def should_auto_extend(kind: str, tool_names, auto_extends: int) -> bool:
    """步数将尽时该不该**自动**续（无人值守）。纯函数，判据直接测。

    11-11 三场真任务坐实：原先只给 `long_review` 续步，而 R2/R3 两场
    `writing` 短文同样在第 24/40 步弹 `step_limit`，白等 300 s 后
    `success=false`。扩到 writing，但要有两道闸：
      ① 已经用 write_file 写过正文；
      ② 一次 deliver_task 都还没试过。
    否则「模型啥也没干就自动加步数」等于把预算放水。
    问答 / 短任务（kind=other）一律不续，守住原有的设计边界。
    """
    if AUTO_EXTEND_STEPS <= 0 or auto_extends >= MAX_AUTO_EXTENDS:
        return False
    names = {str(n) for n in (tool_names or [])}
    if kind == "long_review":
        return True
    if kind == "writing":
        return "write_file" in names and "deliver_task" not in names
    return False

#: 正文类后缀。原先分散在 `loop._PROSE_SUFFIXES` 与 `_written_files` 两处。
PROSE_SUFFIXES = frozenset({".md", ".txt", ".tex", ".rst", ".docx", ".doc"})

#: 章节标题：编号章（`第 N 章` / `1 引言` / `1.2 方法` / `一、绪论`）。
_RE_NUM_CHAPTER = re.compile(
    r"^(?:第\s*[一二三四五六七八九十百\d]+\s*[章节部分]\s*\S"
    r"|\d+(?:\.\d+)*[\s、.．]\s*\S"
    r"|[一二三四五六七八九十]+\s*[、.．]\s*\S)")
#: 附录/说明性标题 —— 不是章，不该进「每章绑定文献」的分母。
_RE_APPENDIX = re.compile(
    r"参考文献|元数据|DOI|处理规则|核算|术语|附录|说明|备注|目录|"
    r"大纲|提纲|写作计划|字数预算|图清单|图表清单", re.I)


def is_chapter_head(line: str) -> bool:
    """这一行是不是「一章」的标题（大纲门分母的唯一判据）。

    只认编号章；文档大标题、附录节、说明节一律不算。
    刻意取严：**少算一章只是少核一处，多算一节就是一次假红返工**。
    若整份大纲一条编号章都没有，`gate._check_outline` 会退回原有的宽松
    切块（见那里），不会把合法写法判成「读不出章标题」的死门。
    """
    s = str(line or "").strip().lstrip("#").strip()
    if not s or _RE_APPENDIX.search(s):
        return False
    return bool(_RE_NUM_CHAPTER.match(s))


def chapter_head_line(text: str, start: int) -> str:
    """取标题行的完整文字（门里 `body.splitlines()[0]` 拿不到的那个）。

    ★ 实测根因：`gate._RE_OUTLINE_HEAD` 以 `^\\s{0,3}` 开头，而 `\\s`
      **包含换行符** ⇒ `m.start()` 常落在**前一行的换行处**，不在标题行首。
      于是门里 `body.splitlines()[0]` 稳定是空串，章名全是 `(无标题)`，
      切块边界也整体偏一行。先跳过这段前导空白，再按行截，才拿得到
      模型真正写的那一行。
    """
    i = max(0, start)
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    lo = text.rfind("\n", 0, i) + 1
    hi = text.find("\n", i)
    return text[lo: (hi if hi >= 0 else n)].strip()


def prose_files(root: "Path | str | None" = None,
                limit: int = 12) -> list[tuple[str, Path]]:
    """本次工作区里成文的产物，按修改时间从新到旧 —— **读回的唯一入口**。

    ★ `root=None` 兜底到 `Path.cwd()`：主循环执行期间 `os.chdir(ws)` 已把
      cwd 切进工作区。原先这里 `return []`，于是 6 个未完成分支里 5 个
      不传 root 的调用全都在说「无正文产物」，而盘上明明有稿。
    """
    r = Path(root) if root is not None else Path.cwd()
    found: list[tuple[float, str, Path]] = []
    for base in (r, r / "data"):
        try:
            for p in base.iterdir():
                if p.is_file() and p.suffix.lower() in PROSE_SUFFIXES:
                    found.append((p.stat().st_mtime, p.name, p))
        except Exception:  # noqa: BLE001 - 任何异常只影响这一层
            continue
    found.sort(key=lambda x: x[0], reverse=True)
    return [(n, p) for _, n, p in found[:limit]]


@dataclass(frozen=True)
class RunShape:
    """本场结构事实。所有消费点只读它，不许自己再推断一遍。"""
    kind: str                      # long_review | writing | other
    max_steps: int
    preset: str                    # 本场最终应使用的预设 id（"" = 不挂载）
    routed: bool                   # preset 是否由形状自动选（而非用户显式指定）
    explicit_preset: str
    explicit_max_steps: int | None
    default_max_steps: int
    reasons: tuple[str, ...]

    def to_event(self) -> dict:
        return {
            "kind": self.kind, "max_steps": self.max_steps,
            "preset": self.preset, "routed": self.routed,
            "explicit_preset": self.explicit_preset,
            "explicit_max_steps": self.explicit_max_steps,
            "default_max_steps": self.default_max_steps,
            "reasons": list(self.reasons),
        }


def resolve_shape(contract, *, explicit_preset: str = "",
                  explicit_max_steps: int | None = None,
                  default_max_steps: int = 40,
                  auto_route: bool = True) -> RunShape:
    """契约冻结之后、装配之前的**唯一**形状决策点。

    显式参数 > 形状推断 > 全局默认。续跑由调用方决定是否使用（形状本身
    是纯函数，重算结果一致，不涉及任何副作用）。
    """
    ep = str(explicit_preset or "").strip()
    try:
        ems = (int(explicit_max_steps)
               if explicit_max_steps not in (None, "", 0) else None)
    except (TypeError, ValueError):
        ems = None
    base = int(default_max_steps or 40)

    writing = bool(getattr(contract, "needs_writing", False))
    answer_only = bool(getattr(contract, "answer_only", False))
    review_like = bool(getattr(contract, "_is_review_like", False))
    long_review = bool(getattr(contract, "_is_long_review", False))
    stages = list(getattr(contract, "stages", None) or [])
    has_outline = "outline" in stages

    if writing and not answer_only and review_like and long_review:
        kind = "long_review"
    elif writing and not answer_only:
        kind = "writing"
    else:
        kind = "other"

    reasons: list[str] = []
    if kind == "long_review":
        reasons.append("长综述：needs_writing + _is_review_like + _is_long_review")
        if has_outline:
            reasons.append("契约含 outline 工序（章-文献映射 + 50 篇文献下限）")
        steps = max(base, LONG_REVIEW_STEPS)
        reasons.append(f"步数 {base} → {steps}（50 篇文献 + 分节 + 图 + 一次检验）")
    else:
        steps = base
        reasons.append(f"{kind}：沿用默认步数 {base}")

    routed = bool(kind == "long_review" and not ep and auto_route)
    preset = ep or (AUTO_PRESET_WRITING if routed else "")
    if ep:
        reasons.append(f"用户显式指定 preset={ep}，形状路由不覆盖")
    elif routed:
        reasons.append(f"写作形状自动挂载 {AUTO_PRESET_WRITING}（默认路径也拿到下限能力）")
    if ems is not None:
        steps = ems
        reasons.append(f"用户显式指定 max_steps={ems}，覆盖形状预算")

    return RunShape(kind=kind, max_steps=steps, preset=preset, routed=routed,
                    explicit_preset=ep, explicit_max_steps=ems,
                    default_max_steps=base, reasons=tuple(reasons))


@dataclass(frozen=True)
class RunPlan:
    """单场运行的不可变装配计划（契约冻结后、挂载前的唯一优先级裁决单源）。

    与 `TaskContract` 分工：
      · `TaskContract` 只描述「用户要什么」（目标 / 交付 / 验收 / 推断意图）；
      · `RunPlan` 裁决「这一场怎么跑」（预设 / 模式 / 步数 / 导师 / 覆写后 Config）。

    显式优先级：
      1. 断点续跑（`resume` 且非 `follow_up`）：快照身份优先于一切，禁止中途换尺子；
      2. 会话追问（`resume` 且带 `follow_up`）：本轮显式参数 > 快照继承，不自动路由；
      3. 新任务：显式参数（`preset`/`mode`/`max_steps`/`kb_only`/`advisor`）> 形状自动路由 > `cfg` 默认；
      4. 阈值覆写：`preset.config` 通过 `Config.with_overrides()` 生成不可变 `effective_cfg`。
    """
    kind: str                          # long_review | writing | other
    mode: str                          # "" | "auto" | "task" | "answer"
    answer_only: bool
    preset: str                        # 最终生效的预设 id（"" = baseline/不显式挂载）
    explicit_preset: str
    routed: bool
    resumed: bool
    is_followup: bool
    max_steps: int
    explicit_max_steps: int | None
    default_max_steps: int
    kb_only: bool
    advisor: str
    topic: str
    effective_cfg: Any = None
    preset_config: dict = None         # type: ignore[assignment]
    preset_scenes: tuple[str, ...] = ()
    preset_stages: tuple[str, ...] = ()
    config_error: str = ""
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.preset_config is None:
            object.__setattr__(self, "preset_config", {})

    def to_event(self) -> dict:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "answer_only": self.answer_only,
            "preset": self.preset,
            "explicit_preset": self.explicit_preset,
            "routed": self.routed,
            "resumed": self.resumed,
            "is_followup": self.is_followup,
            "max_steps": self.max_steps,
            "explicit_max_steps": self.explicit_max_steps,
            "default_max_steps": self.default_max_steps,
            "kb_only": self.kb_only,
            "advisor": self.advisor,
            "topic": self.topic,
            "preset_config": {k: dict(v) for k, v in (self.preset_config or {}).items()},
            "preset_scenes": list(self.preset_scenes),
            "preset_stages": list(self.preset_stages),
            "config_error": self.config_error,
            "reasons": list(self.reasons),
        }

    def to_snapshot(self) -> dict:
        """可落盘进 `conversation.json` / `session.json` 的精简会话身份。"""
        return {
            "preset": self.preset,
            "explicit_preset": self.explicit_preset,
            "routed": self.routed,
            "mode": self.mode,
            "kb_only": self.kb_only,
            "advisor": self.advisor,
            "topic": self.topic,
            "kind": self.kind,
            "max_steps": self.max_steps,
        }


class _ContractView:
    """只读代理：在不修改传入 `contract` 对象的前提下覆盖 `answer_only`。"""

    def __init__(self, inner, answer_only: bool):
        self._inner = inner
        self.answer_only = answer_only

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def resolve_run_plan(
    contract,
    *,
    cfg=None,
    explicit_preset: str = "",
    mode: str = "",
    kb_only: bool | None = None,
    advisor: str = "",
    topic: str = "",
    explicit_max_steps: int | None = None,
    default_max_steps: int | None = None,
    resume: dict | None = None,
    auto_route: bool = True,
    preset_dir: Path | None = None,
    home: Path | None = None,
) -> RunPlan:
    """唯一运行计划裁决器（纯函数、不修改 `contract` 与 `cfg`）。"""
    reasons: list[str] = []
    snap = resume if isinstance(resume, dict) else {}
    is_resumed = bool(snap.get("messages") or [])
    is_followup = bool(is_resumed and str(snap.get("follow_up") or "").strip())

    snap_plan = snap.get("run_plan") if isinstance(snap.get("run_plan"), dict) else {}
    snap_preset = str(snap_plan.get("preset") or snap.get("preset") or "").strip()
    snap_mode = str(snap_plan.get("mode") or snap.get("mode") or "").strip()
    snap_advisor = str(snap_plan.get("advisor") or snap.get("advisor") or "").strip()
    snap_topic = str(snap_plan.get("topic") or snap.get("topic") or "").strip()
    snap_kb = snap_plan.get("kb_only") if "kb_only" in snap_plan else snap.get("kb_only")

    raw_ep = str(explicit_preset or "").strip()
    raw_mode = str(mode or "").strip().lower()
    raw_advisor = str(advisor or "").strip()
    raw_topic = str(topic or "").strip()

    if is_resumed and not is_followup:
        # 断点续跑：快照身份绝对优先，绝不中途换尺子
        eff_preset_in = snap_preset or raw_ep
        eff_mode = snap_mode or raw_mode
        eff_advisor = snap_advisor or raw_advisor
        eff_topic = snap_topic or raw_topic
        eff_kb = bool(snap_kb) if snap_kb is not None else bool(kb_only)
        if snap_preset and raw_ep and raw_ep != snap_preset:
            reasons.append(f"断点续跑：沿用快照 preset={snap_preset}（忽略新传 {raw_ep}）")
        elif snap_preset:
            reasons.append(f"断点续跑：从快照恢复 preset={snap_preset}")
    elif is_followup:
        # 会话追问：本轮显式覆盖 > 父轮快照继承
        eff_preset_in = raw_ep or snap_preset
        eff_mode = raw_mode or snap_mode
        eff_advisor = raw_advisor or snap_advisor
        eff_topic = raw_topic or snap_topic
        eff_kb = bool(kb_only) if kb_only is not None else bool(snap_kb)
        if not raw_ep and snap_preset:
            reasons.append(f"会话追问：继承快照 preset={snap_preset}")
    else:
        eff_preset_in = raw_ep
        eff_mode = raw_mode
        eff_advisor = raw_advisor
        eff_topic = raw_topic
        eff_kb = bool(kb_only)

    if eff_mode not in ("", "auto", "task", "answer"):
        reasons.append(f"忽略不认得的 mode={eff_mode!r}，回落自动判断")
        eff_mode = ""

    contract_ao = bool(getattr(contract, "answer_only", False))
    if eff_mode == "answer":
        answer_only = True
        if not contract_ao:
            reasons.append("mode=answer：覆盖契约为仅问答")
    elif eff_mode == "task":
        answer_only = False
        if contract_ao:
            reasons.append("mode=task：覆盖契约为任务模式")
    else:
        answer_only = contract_ao

    base_steps = int(
        default_max_steps
        if default_max_steps is not None
        else (getattr(getattr(cfg, "loop", None), "max_steps", 40) or 40)
    )
    view = _ContractView(contract, answer_only) if answer_only != contract_ao else contract
    shape = resolve_shape(
        view,
        explicit_preset=eff_preset_in,
        explicit_max_steps=explicit_max_steps,
        default_max_steps=base_steps,
        auto_route=(auto_route and not is_resumed),
    )
    reasons.extend(shape.reasons)

    preset_id = shape.preset
    effective_cfg = cfg
    preset_config: dict = {}
    preset_scenes: tuple[str, ...] = ()
    preset_stages: tuple[str, ...] = ()
    config_error = ""
    steps = shape.max_steps

    if preset_id:
        from .preset import resolve_preset
        resolved, perr = resolve_preset(preset_id, directory=preset_dir, home=home)
        if resolved is None:
            config_error = perr
            reasons.append(f"预设 {preset_id!r} 解析失败：{perr}")
        else:
            preset_config = resolved.config_patch()
            preset_scenes = tuple(resolved.scenes)
            preset_stages = tuple(resolved.stages)
            if cfg is not None and preset_config:
                try:
                    effective_cfg = cfg.with_overrides(preset_config)
                    reasons.append(
                        f"预设 {preset_id} 覆写配置段：{sorted(preset_config)}")
                    if (shape.explicit_max_steps is None
                            and shape.kind != "long_review"
                            and "max_steps" in (preset_config.get("loop") or {})):
                        steps = int(effective_cfg.loop.max_steps)
                        reasons.append(f"预设 loop.max_steps 覆盖步数 → {steps}")
                except Exception as e:                       # noqa: BLE001
                    config_error = str(e)
                    effective_cfg = cfg
                    reasons.append(f"预设 {preset_id} 配置覆写失败：{e}")

    if answer_only and preset_stages:
        reasons.append("仅问答模式与预设工序并存：阶段硬门将自动降级为提醒")
    if eff_advisor and not eff_kb:
        reasons.append(f"已指定导师模型 {eff_advisor!r} 但未开启导师模式（kb_only=False）")

    return RunPlan(
        kind=shape.kind,
        mode=eff_mode,
        answer_only=answer_only,
        preset=preset_id,
        explicit_preset=eff_preset_in,
        routed=shape.routed,
        resumed=is_resumed,
        is_followup=is_followup,
        max_steps=steps,
        explicit_max_steps=shape.explicit_max_steps,
        default_max_steps=base_steps,
        kb_only=eff_kb,
        advisor=eff_advisor,
        topic=eff_topic,
        effective_cfg=effective_cfg,
        preset_config=preset_config,
        preset_scenes=preset_scenes,
        preset_stages=preset_stages,
        config_error=config_error,
        reasons=tuple(reasons),
    )
