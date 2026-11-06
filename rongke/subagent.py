# -*- coding: utf-8 -*-
"""荣珂子代理 —— 把一件事派给一个**隔离**的子智能体，只把结果交回来。

零第三方依赖（只用标准库）。设计来源见工单 W20-B：
声明格式抄 ZCode（一个子代理就是一个 `.md`，frontmatter 声明、正文是系统提示词），
「只回结果」抄 Kimi Code（中间过程不回灌父级历史），
「委派链默认终止」抄 Kimi 的**结构性**手段（不是靠提示词嘱咐，是那个工具压根不在清单里）。

三条红线，都是本模块的存在理由：

  1. **绝不在父实例上重入 `run()` / `_run_serial()`。**
     那两个方法会重写 `self.registry` 台账序号、`self.gate` 打回额度、
     `self.fence`、`self.telemetry`，还会 `os.chdir()`（**进程级**状态）。
     而 `loop._WORKSPACE_LOCK` 是 `RLock` —— 同线程重入**挡不住**，
     于是父任务会带着被踩脏的状态继续跑，且**不报错**。
     ⇒ 这里给子代理**另建一个 `Rongke` 实例**，自己的工作区、自己的
       registry / gate / fence / telemetry。`SubagentRunner.run()` 末尾还有一道
       **事后自检**：逐项比对父实例这几个对象的身份和 cwd，被动过就如实报出来
       ——「不许静默」比「我以为不会」值钱。

  2. **只回结果**：交回父级的是「最终交付文本 + 用了多少步 + 各工具调用次数」。
     子代理的中间思考、每次工具的原文**一律不回灌**。
     回灌就等于把父级上下文更早撑爆，这个功能就白做了。

  3. **默认不能再派子代理**：`allowed_tool_names()` 无条件把可再派发的目标
     （`DELEGATING_TOOLS`）从允许清单里剥掉，**除非 profile 显式写了 `subagents`**。
     剥的办法是**真的把它从子实例的注册表里删掉**（`_prune_registry`），
     所以子代理既**看不见**这个工具，也**调不动**它（`ToolRegistry.call` 会报未注册）。
"""
from __future__ import annotations

import fnmatch
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ── 两类工具名，语义完全不同，故分成两个常量 ──────────────────────
#: 「能再派发」的目标。子代理默认拿不到（Kimi 的 `withoutDelegatingTargets`）。
DELEGATING_TOOLS: frozenset[str] = frozenset({"subagent"})
#: 收尾基元。子代理没有它们就**永远无法结束**，只能烧满 maxTurns。
#: 因此它们在允许清单里是**默认保留**的，除非 profile 明确禁止。
EXIT_TOOLS: frozenset[str] = frozenset({"deliver_task", "report_blocker"})

#: `tools` 里表示「全部」的写法（ZCode 的内置 general-purpose 用 `["*"]`）
WILDCARD: str = "*"
#: `disallowedTools` 里的**元规格**：一条顶一整类 —— 所有 `mutating=True` 的工具。
#: 存在的理由不是省事，是**完备**：「挡住所有写类工具」用名字清单表达，
#: 永远会漏掉下一个新注册的工具。而本项目的既有口令是
#: 「未知工具一律按'会改'」（`bridge.py` MUTATING 上方那句）——
#: 既然判"会不会改"的元数据已经在注册表里，就该用它来挡，而不是靠人抄名字。
#: 名字模式仍然留着：它同时是**给作者读的说明**，看得懂比省几行重要。
BAN_MUTATING: str = "<mutating>"

#: frontmatter 里认的字段。表外的键**不静默丢弃**，出一条诊断。
KNOWN_FIELDS: tuple[str, ...] = (
    "name", "description", "tools", "disallowedTools", "maxTurns",
    "permissionMode", "injectAgentsMd", "background", "subagents",
)
#: 本项目规约文件的候选名（`injectAgentsMd` 找的就是它）。按优先级排。
AGENTS_FILENAMES: tuple[str, ...] = ("AGENTS.md", "QODER.md", "CLAUDE.md")

#: 交回父级的交付文本上限。子代理可以写很长，但父级的上下文比它值钱。
FINAL_CHARS = 6000
#: 结果文本的截断必须自报 —— 否则「没看全」会被读成「看过了」。
TRUNCATE_NOTE = "…（交付文本过长，此处截断，原长 {n} 字符。全文见子代理工作区的归档）"

#: profile 没声明 `maxTurns` 时的默认步数。**与 `config.DEFAULTS["subagent"]["max_turns"]`
#: 是同一个数**（那处是配置的默认，这里是一处引用；两处都写死字面量就是 H 型）。
DEFAULT_MAX_TURNS = 12


# ══════════════════════════ 诊断 ══════════════════════════
@dataclass
class Diagnosis:
    """一条可读的诊断。**解析不许抛异常**（ZCode 的口径：
    CLI 已加载的 profile 不能因为不是严格 YAML 就在 GUI 里消失）。"""
    code: str
    message: str
    field: str = ""
    severity: str = "error"            # error | warning

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message,
                "field": self.field, "severity": self.severity}

    def __str__(self) -> str:
        tag = "⚠️" if self.severity == "warning" else "⛔"
        where = f"（字段 {self.field}）" if self.field else ""
        return f"{tag} {self.code}{where}：{self.message}"


# ══════════════════════ 极简 frontmatter 解析 ══════════════════════
# 荣珂零依赖 ⇒ 装不上 pyyaml。这里**手写**一个只认两种形状的解析器：
#     key: value            key: [a, b]
#     key:                  key:
#       - a                   - a
# 刻意**不做**的事，都是 YAML 里最容易把人绊倒的：锚点、多文档、嵌套映射、
# 折叠标量（| >）、流式映射 {a: 1}。碰到它们不猜、不崩 —— 当普通标量收下来，
# 并在诊断里说清楚「本解析器只认 key: value 与 - item」。
def _split_frontmatter(text: str) -> tuple[list[str] | None, list[str], str]:
    """切成 `(frontmatter 行, 正文行, 错误码)`。没有 frontmatter 不抛，返回错误码。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = raw.lstrip("\ufeff").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        return None, lines, "agent_missing_frontmatter"
    body_start = -1
    for j in range(i + 1, len(lines)):
        if lines[j].strip() in ("---", "..."):
            body_start = j
            break
    if body_start < 0:
        # 开了个头却没有闭合 ⇒ 整份正文会被当成 frontmatter 吃掉，那比失败更糟。
        return None, lines, "agent_unterminated_frontmatter"
    return lines[i + 1:body_start], lines[body_start + 1:], ""


def _strip_inline_comment(val: str) -> str:
    """去掉行内注释。**只认「空格 + #」**：`a.md#frag`、URL 里的 `#` 不许被切掉。"""
    out = []
    for k, ch in enumerate(val):
        if ch == "#" and (k == 0 or val[k - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).strip()


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _split_flow(val: str) -> list[str]:
    """`[a, b, "c, d"]` → 列表。**不引嵌套**：一层。逗号切分时先跳过引号内。"""
    inner = val[1:-1].strip()
    if not inner:
        return []
    items: list[str] = []
    buf, quote = "", ""
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(buf.strip())
            buf = ""
        else:
            buf += ch
    items.append(buf.strip())
    return [_unquote(x) for x in items if x.strip()]


def parse_frontmatter(lines: Iterable[str]) -> tuple[dict[str, Any], list[Diagnosis]]:
    """`key: value` / `- item` 两形状 → dict。认不动的**记录而非崩溃**。"""
    data: dict[str, Any] = {}
    diags: list[Diagnosis] = []
    key = ""                                # 当前挂起的空值键（等着收 `- item`）
    for line in lines:
        s = line.rstrip()
        if not s.strip() or s.lstrip().startswith("#"):
            continue
        indented = s[:len(s) - len(s.lstrip())]
        stripped = s.strip()
        if stripped.startswith("- "):
            if not key:
                diags.append(Diagnosis(
                    "frontmatter_orphan_item",
                    f"孤立的列表项 {stripped!r}：上面没有一行是「只写键、不写值」。",
                    severity="warning"))
                continue
            item = _unquote(_strip_inline_comment(stripped[2:]))
            if not isinstance(data.get(key), list):
                data[key] = []
            if item:
                data[key].append(item)
            continue
        if indented:
            diags.append(Diagnosis(
                "frontmatter_nested_unsupported",
                f"不支持的缩进/嵌套行 {stripped!r}（本解析器只认 key: value 与 - item；"
                f"这一行被跳过，不影响其余字段）。",
                severity="warning"))
            continue
        if ":" not in stripped:
            diags.append(Diagnosis(
                "frontmatter_unparsable_line",
                f"读不懂的行 {stripped!r}（没有冒号），已跳过。",
                severity="warning"))
            continue
        k, _, v = stripped.partition(":")
        key = k.strip()
        val = _strip_inline_comment(v.strip())
        if not key:
            diags.append(Diagnosis("frontmatter_empty_key",
                                   f"键名为空的行：{stripped!r}", severity="warning"))
            continue
        if not val:
            data[key] = ""                  # 等下面的 `- item`（没有就留空串）
        elif val.startswith("["):
            data[key] = _split_flow(val) if val.endswith("]") else [_unquote(val[1:])]
            if not val.endswith("]"):
                diags.append(Diagnosis(
                    "frontmatter_unterminated_flow_list",
                    f"{key} 的 `[` 没有闭合，按单项收下：{data[key]}",
                    field=key, severity="warning"))
        else:
            data[key] = _unquote(val)
    return data, diags


# ══════════════════════════ Profile ══════════════════════════
def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v if v is not None else "").strip().lower()
    if s in ("1", "true", "yes", "y", "on", "是"):
        return True
    if s in ("0", "false", "no", "n", "off", "否", ""):
        return False
    return default


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _as_list(v: Any) -> list[str] | None:
    """→ 列表。`None` = 「这一栏没写」（全部允许）；`[]` = 「写了但是空的」，
    两者**必须能分开** —— 压成一个就是 M 型（口径塌缩）。
    字符串 `"*"` 收成 `["*"]`（ZCode 的内置 general-purpose 就是这么写的）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        return [p.strip() for p in s.split(",") if p.strip()]
    if isinstance(v, (list, tuple, set)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


@dataclass
class SubagentProfile:
    """一个子代理 = 一份这样的声明。**字段名照抄 ZCode**（那是它的资产，不另起名字）。"""
    name: str
    description: str
    #: 允许清单。`None` = 没写 ⇒ 全部允许；`["a", "b*"]` = 只允许这些（支持 `*` 通配）。
    tools: list[str] | None = None
    #: 禁止清单。★ **与 tools 冲突时禁止优先**（见 `allowed_tool_names` 的注释）。
    disallowedTools: list[str] = field(default_factory=list)
    #: 步数上限，防跑飞烧钱。★ **默认值是正数**（12），不是 0：
    #:   一份没写 `maxTurns` 的声明如果读成 0/None，"无限跑"就是默认行为 ——
    #:   而无限跑在按轮次计费的网关上不是"慢一点"，是**烧光额度还不交活**。
    #:   写 0 或负数才表示"我没声明，去用配置里的 subagent.max_turns"。
    maxTurns: int = DEFAULT_MAX_TURNS
    #: `auto` | `plan`。`plan` = 只出方案不动手（**机制上是剔掉 mutating 工具**，
    #:   不是靠提示词嘱咐 —— 嘱咐会被绕过，清单里没有才是真没有）。
    permissionMode: str = "auto"
    #: 是否把项目规约（AGENTS.md）注入子代理上下文。
    injectAgentsMd: bool = True
    #: 是否后台跑。**本轮（W20-B）只解析、不实现**；为 True 时结果里带一条如实的警告。
    background: bool = False
    #: ★ 显式声明「这个子代理可以再派发哪些子代理」。
    #:   **没写 = 不允许再派发**（`allowed_tool_names` 会剥掉 `DELEGATING_TOOLS`）。
    subagents: list[str] | None = None
    #: 正文 = 子代理的角色指令。
    body: str = ""
    source: str = "builtin"                 # builtin | file | inline

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "tools": self.tools, "disallowedTools": list(self.disallowedTools),
                "maxTurns": self.maxTurns, "permissionMode": self.permissionMode,
                "injectAgentsMd": self.injectAgentsMd, "background": self.background,
                "subagents": self.subagents, "body": self.body, "source": self.source}

    @property
    def is_plan(self) -> bool:
        return str(self.permissionMode or "auto").strip().lower() == "plan"

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, body: str = "",
                 source: str = "inline") -> "SubagentProfile":
        tools = data.get("tools")
        dis = data.get("disallowedTools")
        subs = data.get("subagents")
        return cls(
            name=str(data.get("name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            tools=_as_list(tools) if tools not in (None, "") else None,
            disallowedTools=_as_list(dis) or [],
            #: **没写** = 用默认步数（不是 0 = 不限）；**显式写 0/负数** 才表示
            #: "去用配置里的 subagent.max_turns"。
            maxTurns=_as_int(data.get("maxTurns"), DEFAULT_MAX_TURNS),
            permissionMode=str(data.get("permissionMode") or "auto").strip().lower(),
            injectAgentsMd=_as_bool(data.get("injectAgentsMd"), True),
            background=_as_bool(data.get("background"), False),
            subagents=_as_list(subs) if subs not in (None, "") else None,
            body=(body or "").strip(),
            source=source,
        )


@dataclass
class ProfileParse:
    """`parse_profile_md` 的返回。**永远不抛异常**。

    ★ 它同时**可以直接当 profile 用**（`p.name` / `p.tools` / `p.maxTurns` …
      转发给 `p.profile`）。理由：调用方拿到一份声明，绝大多数情况下
      就是要读它的字段；如果必须先 `if p.profile is None: ...` 才能读，
      那么十处调用里有九处会写成 `p.profile.name` —— 而那九处里的若干处
      会**忘了判 None**，于是坏声明的"给诊断不抛异常"在下游变成
      `AttributeError: 'NoneType'`。坏输入的容错不许只在解析函数里成立。
      读字段拿不到时抛 `AttributeError`，因此 `hasattr()` 仍然如实回答"没有"。
    """
    profile: SubagentProfile | None
    diagnostics: list[Diagnosis] = field(default_factory=list)

    def __getattr__(self, item: str):
        # `__getattr__` 只在**正常查找失败后**才触发，所以自己的字段
        # （profile / diagnostics）与下面的 property 都走不到这里。
        if item.startswith("_") or item in ("profile", "diagnostics"):
            raise AttributeError(item)
        prof = self.__dict__.get("profile")
        if prof is None:
            raise AttributeError(item)
        return getattr(prof, item)

    @property
    def ok(self) -> bool:
        return self.profile is not None and not self.errors

    @property
    def errors(self) -> list[Diagnosis]:
        return [d for d in self.diagnostics if d.severity == "error"]

    @property
    def warnings(self) -> list[Diagnosis]:
        return [d for d in self.diagnostics if d.severity == "warning"]

    def report(self) -> str:
        return "\n".join(str(d) for d in self.diagnostics) or "（无诊断）"


def parse_profile_md(text: str, *, source: str = "inline") -> ProfileParse:
    """一份 `.md` → profile + 诊断。缺 name / description **返回诊断对象，不抛异常**。"""
    fm_lines, body_lines, code = _split_frontmatter(text)
    diags: list[Diagnosis] = []
    if code:
        diags.append(Diagnosis(
            code,
            "frontmatter 要用一行 `---` 开头、再来一行 `---` 闭合；"
            "缺任何一侧，整份文件都没法读成声明。"
            if code == "agent_missing_frontmatter"
            else "开了 `---` 却没有闭合，正文会被整段吞进声明里。"))
        return ProfileParse(None, diags)

    data, parsed_diags = parse_frontmatter(fm_lines or [])
    diags.extend(parsed_diags)
    for missing in ("name", "description"):
        if not str(data.get(missing) or "").strip():
            diags.append(Diagnosis(
                f"agent_missing_{missing}",
                f"必填字段 `{missing}` 缺失或为空 —— 一个子代理至少要有名字"
                f"（父级拿它点名）和一句话说明（什么时候该派它）。"))
    for extra in [k for k in data if k not in KNOWN_FIELDS]:
        diags.append(Diagnosis(
            "profile_unknown_field",
            f"字段 `{extra}` 不在 W20-B 的字段表里，**已忽略**（不是报错："
            f"CLI 已加载的 profile 不该因为多写一行就在别处消失）。",
            field=extra, severity="warning"))
    if str(data.get("permissionMode") or "").strip().lower() not in ("", "auto", "plan"):
        diags.append(Diagnosis(
            "profile_bad_permissionMode",
            f"`permissionMode` 只认 auto | plan，收到 "
            f"{data.get('permissionMode')!r} ⇒ 按 auto 处理。",
            field="permissionMode", severity="warning"))
    if str(data.get("maxTurns") or "").strip() and _as_int(data.get("maxTurns"), -1) <= 0:
        diags.append(Diagnosis(
            "profile_bad_maxTurns",
            f"`maxTurns` 要是正整数，收到 {data.get('maxTurns')!r} ⇒ 用配置默认值。",
            field="maxTurns", severity="warning"))

    profile = SubagentProfile.from_dict(data, body="\n".join(body_lines).strip(),
                                        source=source)
    return ProfileParse(profile if not diags or all(
        d.severity == "warning" for d in diags) else None, diags)


# ══════════════════════ 内置 profile ══════════════════════════
# 两个内置角色对应 ZCode 的 `Explore` 与 `general-purpose`。
# ★ `explore` 用**禁止清单**（而不是允许清单）来表达「只读」：
#   新增一个写类工具时，禁止清单**默认把它挡住**；允许清单默认把它漏掉。
#   两种漏法里，「默认挡住」是安全侧 —— 一个检索子代理意外能写盘才是事故。
_WRITE_PATTERNS: tuple[str, ...] = (
    "write*", "edit*", "create*", "delete*", "remove*", "rename*", "move*",
    "copy*", "save*", "export*", "import*", "append*", "patch*", "apply*",
    "shell", "bash", "run_command", "exec*", "set_*", "add_*", "build_*",
    "dispatch_task", "record_skill_use", "kb_add", "kb_note", "kb_ingest",
    "kb_review", "mechanical_fix", "figure_render", "figure_audit_extract",
)

BUILTIN_PROFILES: dict[str, SubagentProfile] = {
    "explore": SubagentProfile(
        name="explore",
        description="只读检索子代理：大范围找文献、翻代码、读材料，只回报事实与出处。",
        tools=None,
        # ★ `<mutating>` 才是"只读"落到机制上的那一条；下面那串名字模式是**给作者看的**。
        disallowedTools=[BAN_MUTATING] + list(_WRITE_PATTERNS) + ["subagent"],
        maxTurns=0,
        permissionMode="auto",
        injectAgentsMd=False,   # ★ 与 ZCode 的内置 Explore 一致：探索型不需要项目规约，
                                #   注入纯属浪费上下文。
        body="你是一个**只读**检索助手。只回报你**真的查到**的事实与出处；"
             "不许改动任何文件，也不许凭记忆补全 —— 查不到就说查不到。",
        source="builtin",
    ),
    "coder": SubagentProfile(
        name="coder",
        description="通用干活子代理：读写都行，用来完成一件边界清楚的独立任务。",
        tools=[WILDCARD],       # ★ 全部允许（但仍受委派链终止规则约束）
        disallowedTools=[],
        maxTurns=0,
        permissionMode="auto",
        injectAgentsMd=True,
        body="你是一个能干活的执行体。按任务契约把事情做完，"
             "交付时用 deliver_task 附上真实的 call_seq 证据。",
        source="builtin",
    ),
}


def get_profile(name: str = "") -> SubagentProfile | None:
    """按名字取内置 profile（空 = `coder`，即「不设特殊限制的那一个」）。"""
    key = (name or "").strip().lower()
    if not key:
        return BUILTIN_PROFILES["coder"]
    return BUILTIN_PROFILES.get(key)


def list_profiles() -> list[SubagentProfile]:
    return list(BUILTIN_PROFILES.values())


def load_profiles_from(directory: "Path | str") -> tuple[list[SubagentProfile],
                                                         list[Diagnosis]]:
    """把一个目录里的 `*.md` 读成 profiles。**坏文件只出诊断、不拦好文件**。"""
    out: list[SubagentProfile] = []
    diags: list[Diagnosis] = []
    d = Path(directory)
    if not d.is_dir():
        return out, [Diagnosis("profile_dir_missing", f"目录不存在：{d}")]
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            diags.append(Diagnosis("profile_unreadable",
                                   f"{p.name} 读不动：{type(e).__name__}: {e}",
                                   field=p.name, severity="warning"))
            continue
        parsed = parse_profile_md(text, source=str(p))
        diags.extend(parsed.diagnostics)
        if parsed.profile is not None:
            out.append(parsed.profile)
    return out, diags


# ══════════════════ 允许清单（委派链在这里终止）══════════════════
def _normalize_catalog(catalog: Any) -> dict[str, bool]:
    """工具清单 → `{名字: 是否改世界}`。

    接受四种喂法（**都要能喂**，否则调用方只能各抄一遍展开逻辑）：
      * `ToolRegistry`（有 `names()` / `get()`）—— 正常路径
      * `Tool` 对象序列（有 `.name` / `.mutating`）
      * `{name: mutating}` 字典
      * 纯名字列表 —— 此时**量不出** mutating，plan 模式会退化成软约束，
        所以 `profile_diagnoses` 对这一种出一条警告（不许静默当成"照样硬"）。
    """
    if catalog is None:
        return {}
    if isinstance(catalog, dict):
        return {str(k): bool(v) for k, v in catalog.items()}
    names_fn = getattr(catalog, "names", None)
    if callable(names_fn):
        get_fn = getattr(catalog, "get", lambda _n: None)
        out: dict[str, bool] = {}
        for n in names_fn():
            tool = get_fn(n)
            out[str(n)] = bool(getattr(tool, "mutating", False))
        return out
    out = {}
    for item in catalog:
        if isinstance(item, str):
            out[item] = False
        else:
            nm = getattr(item, "name", None)
            if nm:
                out[str(nm)] = bool(getattr(item, "mutating", False))
    return out


def _matches(pattern: str, meta: dict[str, bool]) -> list[str]:
    """一条工具规格 → 命中的真实工具名。

    三种形状：`<mutating>` 元规格、带 `*`/`?` 的通配、精确名。
    精确名**命不中就不命**（不往里塞一个不存在的名字 —— 那会让允许清单
    凭空长出一个"看得见却调不动"的条目）。
    """
    pat = (pattern or "").strip()
    if not pat:
        return []
    names = list(meta)
    if pat == BAN_MUTATING:
        return [n for n in names if meta.get(n, False)]
    if WILDCARD in pat or "?" in pat:
        return [n for n in names if fnmatch.fnmatchcase(n, pat)]
    return [pat] if pat in set(names) else []


def allowed_tool_names(profile: SubagentProfile, catalog: Any, *,
                       delegating: Iterable[str] = DELEGATING_TOOLS) -> list[str]:
    """这个 profile 下，子实例的注册表里**该剩下哪些工具**。

    四条规则，按顺序：

      1. 允许清单：`tools` 没写 / 写了 `*` ⇒ 全目录；否则取交集（支持 `*` 通配）。
      2. ★ **两张表冲突时禁止优先**：`disallowedTools` 赢。
         理由不是"这样方便"，是**安全默认**：允许清单的漏法是「新工具默认不能用」
         （难用但安全），禁止清单的漏法是「新工具默认能用」（好用但危险）。
         一个声明了「不许写盘」的检索子代理，因为内核新增了写类工具就获得写盘能力，
         那是把作者的意图押在"清单永远抄得全"上。既然要押，押安全那一侧。
         禁止清单里可以写 `<mutating>` 元规格（= 所有 `mutating` 工具），
         它比任何名字清单都完备 —— 见 `BAN_MUTATING` 上方那段。
      3. `permissionMode: plan` ⇒ 剔掉所有 `mutating` 工具（**动手**的能力，
         不是"建议它别动手"）。
      4. ★ **委派链默认终止**：除非 profile 显式声明了 `subagents`，
         可再派发的目标（`DELEGATING_TOOLS`）**压根不出现在结果里**。
         这是结构性保证，不依赖提示词，也不依赖模型自觉。

    收尾基元（`deliver_task` / `report_blocker`）默认保留 —— 剥掉它们等于
    逼着子代理烧满 maxTurns 也无法结束；只有 profile **明确禁止**才剥。
    """
    meta = _normalize_catalog(catalog)
    all_names = sorted(meta)

    declared = profile.tools
    unrestricted = (declared is None
                    or not declared
                    or any(str(x).strip() == WILDCARD for x in declared))
    if unrestricted:
        allow = set(all_names)
    else:
        allow: set[str] = set()
        for pat in declared:
            allow.update(_matches(str(pat), meta))

    # ② 禁止优先
    banned: set[str] = set()
    for pat in (profile.disallowedTools or []):
        banned.update(_matches(str(pat), meta))
    allow -= banned

    # ③ plan 模式：动手的能力整批拿走
    if profile.is_plan:
        allow = {n for n in allow if not meta.get(n, False)}

    # ④ 委派链终止：没显式声明 subagents ⇒ 可派发目标不存在于它的清单里
    if profile.subagents is None:
        allow -= {str(x) for x in delegating}

    # 收尾基元：没被明确禁止就留着
    allow |= {n for n in EXIT_TOOLS if n in meta and n not in banned}

    return sorted(allow)


def profile_diagnoses(profile: SubagentProfile, catalog: Any) -> list[Diagnosis]:
    """把「声明里写了但落不了地」的地方**如实报出来**，不静默吞。"""
    meta = _normalize_catalog(catalog)
    all_names = set(meta)
    out: list[Diagnosis] = []
    for label, pats in (("tools", profile.tools or []),
                        ("disallowedTools", profile.disallowedTools or [])):
        for pat in pats:
            pat = str(pat).strip()
            if not pat or pat == WILDCARD:
                continue
            if pat in (BAN_MUTATING,):
                continue
            if WILDCARD not in pat and "?" not in pat and pat not in all_names:
                out.append(Diagnosis(
                    "profile_tool_not_in_catalog",
                    f"`{label}` 里的 `{pat}` 在子实例的工具目录里**没有这个工具** —— "
                    f"它不会产生任何效果（写了却不生效，比不写更容易骗到自己）。",
                    field=label, severity="warning"))
    if profile.background:
        out.append(Diagnosis(
            "background_not_implemented",
            "`background: true` 本轮（W20-B）**只解析、未实现**，按前台同步执行。",
            field="background", severity="warning"))
    if profile.is_plan and not any(meta.values()):
        out.append(Diagnosis(
            "plan_mode_no_mutating_info",
            "工具目录没带 mutating 标记 ⇒ plan 模式**没有可剔的写类工具**，"
            "只靠提示词约束（这条约束因此是软的，不是硬的）。",
            field="permissionMode", severity="warning"))
    return out


# ══════════════════════════ 运行 ══════════════════════════
def find_agents_md(root: "Path | str | None" = None) -> Path | None:
    """项目规约文件。荣珂仓库当前**没有** AGENTS.md —— 找不到就返回 None，
    调用方据此如实说明「声明要注入但没有可注入的」，不许注入一个空壳。"""
    base = Path(root) if root else None
    if base is None:
        # 项目规约是**用户的文件**，不是随包发布的资产 ⇒ data_root。
        from .config import data_root
        base = data_root()
    for name in AGENTS_FILENAMES:
        p = base / name
        if p.is_file():
            return p
    return None


def subagent_brief(profile: SubagentProfile, task: str,
                   agents_md: str = "") -> str:
    """子代理的第一句话：角色 + 硬约束 + 任务。

    ★ 这里**必须**告诉它「父级只看得到你最后交的那一段」——
      否则它会写「如上所述」，而父级那边根本没有"上"。
    """
    lines = [
        f"═══ 子代理任务 · profile={profile.name} · mode={profile.permissionMode} ═══",
        "",
        f"【你的角色】{profile.description}",
    ]
    if profile.body:
        lines += ["", "【角色指令（来自 profile 正文）】", profile.body]
    rules = [
        "你跑在**自己的上下文**里。父级**只收到你 deliver_task 里那段交付文本**，"
        "看不到你的中间过程，也看不到任何工具原文。",
        "⇒ 交付必须**自足**：结论写全、出处逐条给、"
        "不要写「如上所述」「见前面的检索」。",
        "你**没有**再派发子代理的能力（清单里根本没有那个工具）。",
    ]
    if profile.is_plan:
        rules.append("权限模式是 **plan**：写类工具已经从你的清单里移除 —— "
                     "只交方案，方案要具体到可以直接动手。")
    if profile.maxTurns:
        rules.append(f"步数上限 {profile.maxTurns} 步，到点就结束。"
                     "请把力气花在交付上，不要开新战线。")
    lines += ["", "【这次的硬约束（内核给的，不是建议）】"]
    lines += [f"{i}. {r}" for i, r in enumerate(rules, 1)]
    lines += ["", "【任务】", (task or "").strip()]
    if agents_md:
        lines += ["", "【项目规约（AGENTS.md，节选）】", agents_md[:4000]]
    return "\n".join(lines)


@dataclass
class SubagentResult:
    """交回父级的**全部内容**。刻意没有 messages / 工具原文 —— 见模块 docstring 红线 2。"""
    profile: str = ""
    ok: bool = False
    steps: int = 0
    max_turns: int = 0
    final: str = ""
    tool_calls: dict[str, int] = field(default_factory=dict)
    workspace: str = ""
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    #: 子实例被 profile 摘掉的工具名。**只报清单不报正文** ——
    #: 它是"这一刀到底生效了什么"的一手事实，主控要能看见，
    #: 而它不属于"中间过程回灌"（不是模型说的话，是内核算出来的集合）。
    pruned: list[str] = field(default_factory=list)
    tools_visible: int = 0

    @property
    def call_total(self) -> int:
        return sum(self.tool_calls.values())

    def to_dict(self) -> dict:
        return {"profile": self.profile, "ok": self.ok, "steps": self.steps,
                "max_turns": self.max_turns, "final": self.final,
                "tool_calls": dict(self.tool_calls), "calls_total": self.call_total,
                "workspace": self.workspace, "reason": self.reason,
                "warnings": list(self.warnings),
                "tools_visible": self.tools_visible,
                "pruned_count": len(self.pruned), "pruned": list(self.pruned)}

    def as_text(self) -> str:
        """给父级看的那一段。**只有这一种形态**，不在两处各拼一遍（H 型）。"""
        head = (f"═══ 子代理 · {self.profile} ═══\n"
                f"状态：{'完成（证据门通过）' if self.ok else '未完成'}")
        if self.reason:
            head += f" —— {self.reason}"
        budget = (f"步数：{self.steps} / {self.max_turns}（耗尽 = 未完成，不是失败）"
                  if self.steps >= self.max_turns > 0
                  else f"步数：{self.steps} / {self.max_turns}")
        body = [
            head,
            budget,
            f"工具面：它只能用 {self.tools_visible} 个工具"
            f"（按 profile 摘掉 {len(self.pruned)} 个；派发工具{'已被摘掉' if 'subagent' in self.pruned else '本就不在清单里'}）",
            f"工具调用：{self.call_total} 次 · "
            + ("、".join(f"{k}×{v}" for k, v in sorted(self.tool_calls.items(),
                                                       key=lambda x: (-x[1], x[0])))
               if self.tool_calls else "（一次都没有）"),
        ]
        if self.workspace:
            body.append(f"工作区：{self.workspace}（完整台账归档在这里，可回放）")
        for w in self.warnings:
            body.append(f"⚠️ {w}")
        body += ["", "── 子代理交回的交付 ──", self.final or "（它没有交出任何文本）",
                 "", "※ 以上即子代理交回的全部内容 —— 中间过程按设计**不回灌**父级历史。"]
        return "\n".join(body)


class SubagentRunner:
    """把任务派给一个**独立实例**。`Rongke` 只在 `run()` 里延迟 import ——
    `loop.py` 在模块层 import 本文件，反向 import 会成环。

    本类**只调 `child.run()`，一次都不调 `host.run()`。**
    工单的铁律针对的是"在**同一个对象**上重入 run()"：`_run_serial` 写的是
    `self.fence` / `self.gate` / `self.registry` / `self.telemetry` 这些**实例属性**。
    换实例就把这条路整条绕开 —— 子级写它自己的属性，父级那几个对象的身份全程不变
    （`_check_host_state` 负责证伪，不信就响）。
    唯一绕不开的是 `os.chdir`（**进程级**状态）：`_run_serial` 用 `finally` 还原，
    而外层那把 `_WORKSPACE_LOCK` 是 `RLock` —— 同线程重入既不堵自己、
    也不给别的任务插队改 cwd 的机会。这条还原同样**不靠相信**：自检里比对了 cwd。
    """

    def __init__(self, cfg: Any = None, host: Any = None) -> None:
        from .config import Config
        self.cfg = cfg or Config.load()
        #: 父实例。本类**只读它的身份做事后自检**，绝不往里写、绝不调它的 run()。
        self.host = host
        sc = getattr(self.cfg, "subagent", None)
        self.enabled = bool(getattr(sc, "enabled", True))
        self.default_max_turns = int(getattr(sc, "max_turns", 12) or 12)
        #: ★ 本刀**不引入并行**。并发度在 `Config.load()` 就被 clamp 成 1
        #:   （配置文件是手写的，读出来什么样就照什么跑太软），
        #:   这里再 `min(..., 1)` 一次是**给手工构造的 Config 兜底** ——
        #:   信号量只是把这个事实写成机制：Web 控制台是多线程的，
        #:   光靠"主循环是串行的"这条约定挡不住未来。
        self.max_concurrent = min(1, int(getattr(sc, "max_concurrent", 1) or 1))
        self._slot = threading.Semaphore(self.max_concurrent)

    # ── 工具入口 ────────────────────────────────────────────
    def delegate(self, task: str, profile: str = "") -> str:
        """注册成 `subagent` 工具的那个 `fn`。**不许抛异常**：
        注册表会把异常当成"这次调用失败"回灌给模型，而模型据此会重发同一个调用。"""
        try:
            res = self.run(task, profile)
        except Exception as e:                       # noqa: BLE001
            return (f"⛔ 子代理没能起来：{type(e).__name__}: {e}\n"
                    f"（这不是任务失败 —— 是派发这一步就失败了，"
                    f"请自己接着做，或换一个 profile 再试一次。）")
        return res.as_text()

    # ── 主流程 ──────────────────────────────────────────────
    def run(self, task: str, profile: Any = "") -> SubagentResult:
        prof = self._resolve_profile(profile)
        if prof is None:
            known = "、".join(sorted(BUILTIN_PROFILES))
            return SubagentResult(
                profile=str(profile), ok=False,
                reason=f"没有这个子代理 profile：{profile!r}（内置只有 {known}；"
                       f"自定义的 .md 本轮需要由调用方读进来）")
        if not self.enabled:
            return SubagentResult(profile=prof.name, ok=False,
                                  reason="配置里 subagent.enabled=false，子代理被关掉")
        if not (task or "").strip():
            return SubagentResult(profile=prof.name, ok=False,
                                  reason="task 是空的 —— 派活要写清楚派什么")

        max_turns = prof.maxTurns if prof.maxTurns > 0 else self.default_max_turns
        # ★ 独立实例（红线 1）：自己的 registry / gate / fence / telemetry / MCP 连接。
        #   memory 不挂：一场子任务不该往父级的会话台账里写东西。
        from .loop import Rongke
        child = Rongke(cfg=self.cfg, use_memory=False)

        allowed = allowed_tool_names(prof, child.registry)
        notes = profile_diagnoses(prof, child.registry)
        removed = _prune_registry(child.registry, allowed)

        # 红线 3 的**当场核验**：不是"我以为剥掉了"，是"现在目录里确实没有"。
        leak = [n for n in DELEGATING_TOOLS if n in child.registry.names()]
        warnings = [str(n.message) for n in notes]
        if leak:
            warnings.append(f"⛔ 委派链没断开：子实例仍持有 {leak}")

        agents_md = ""
        if prof.injectAgentsMd:
            p = find_agents_md()
            if p is not None:
                try:
                    agents_md = p.read_text(encoding="utf-8")
                except Exception as e:                   # noqa: BLE001
                    warnings.append(f"AGENTS.md 读不动（{e}），本次不注入")
            else:
                warnings.append("profile 要求注入项目规约，但仓库里没有 "
                                + "/".join(AGENTS_FILENAMES) + " —— 不注入空壳")

        ws = self._make_workspace(prof.name)
        # ★ 交付文本只从**子实例的结果对象**里取。子级的事件一律**不外传**：
        #   `_collector` 只在本地数工具次数、记交付声明，父级的 emit 一条都不接。
        collected = _Collector()
        before = self._host_state()
        try:
            with self._slot:
                rr = child.run(goal=subagent_brief(prof, task, agents_md),
                               max_steps=max_turns, workspace=ws, on_event=collected)
        finally:
            # 子实例自带一套 MCP 连接与本地存储句柄。委派是可以反复发生的动作，
            # 每次都留一套 = 跑几场任务就把连接烧光了。父级收尾只认 `self.subagents`
            # （它没有 close），所以关子实例这件事只能在这里做。
            try:
                child.close()
            except Exception:                          # noqa: BLE001
                pass                                   # 收尾失败不该盖掉交付本身
        warnings.extend(self._check_host_state(before))

        final = (rr.final or "").strip()
        if not final:
            final = (rr.reason or "").strip()
        if len(final) > FINAL_CHARS:
            final = final[:FINAL_CHARS] + TRUNCATE_NOTE.format(n=len(final))

        counts: dict[str, int] = {}
        for call in rr.trace:
            if call.name in EXIT_TOOLS:
                continue          # 收尾基元不是"干活"，混进计数会让人以为它跑了更多活
            counts[call.name] = counts.get(call.name, 0) + 1

        return SubagentResult(
            profile=prof.name,
            ok=bool(rr.success),
            steps=int(rr.steps or 0),
            max_turns=int(max_turns),
            final=final,
            tool_calls=counts,
            workspace=str(ws),
            reason=("" if rr.success else (rr.reason or "未通过证据门").strip()),
            warnings=warnings,
            pruned=removed,
            tools_visible=len(child.registry.names()),
        )

    # ── 助手 ────────────────────────────────────────────────
    def _resolve_profile(self, profile: Any) -> SubagentProfile | None:
        # 调用方手上常见的就是 `parse_profile_md` 的返回值 —— 别逼他先拆一层。
        if isinstance(profile, ProfileParse):
            return profile.profile
        if isinstance(profile, SubagentProfile):
            return profile
        text = str(profile or "").strip()
        if not text:
            return BUILTIN_PROFILES["coder"]
        got = get_profile(text)
        if got is not None:
            return got
        low = text.lower()
        for p in BUILTIN_PROFILES.values():
            if p.name.lower() == low:
                return p
        return None

    def _make_workspace(self, name: str) -> Path:
        """`runs/subagent-<name>-<ts>/`。**不是**父级的工作区。"""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name) or "anon"
        root = Path(self.cfg.runtime.workspace_dir)
        ws = (root / f"subagent-{safe}-{stamp}").resolve()
        n = 1
        while ws.exists():           # 同一秒连派两个同名子代理是真实可能发生的
            n += 1
            ws = (root / f"subagent-{safe}-{stamp}-{n}").resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    def _parent_trace(self) -> list:
        """父实例的台账（**只读**）。没有父（脚本直接调用）时给空表。"""
        h = self.host
        if h is None:
            return []
        return list(getattr(getattr(h, "registry", None), "trace", None) or [])

    def _host_state(self) -> dict:
        """父实例这几样对象的**身份**+ 进程 cwd。用来事后证明没踩到它们。"""
        h = self.host
        if h is None:
            return {}
        gate = getattr(h, "gate", None)
        # ★ 存**对象本身**而不是 id()：id 会被回收后复用 —— 父级围栏若被换掉、
        #   旧对象又恰好被 GC，新对象可能拿到同一个 id，这条告警就**静默漏判**。
        #   抓着引用不放，既保证它不会被回收，也能用 `is` 判身份。
        refs = {"registry": getattr(h, "registry", None),
                "fence": getattr(h, "fence", None),
                "telemetry": getattr(h, "telemetry", None),
                "gate": gate}
        return {
            "refs": refs,
            "gate_writing_used": int(getattr(gate, "_writing_used", 0) or 0),
            "trace_len": len(self._parent_trace()),
            "cwd": os.getcwd(),
        }

    def _check_host_state(self, before: dict) -> list[str]:
        after = self._host_state()
        if not before or not after:
            return []
        # fence / telemetry 在**父级还没进 run() 时是 None**：None 对 None 比对
        # 什么都没判出来，所以那些键这次不计数（如实留着，不假装它挡住了什么）。
        brefs, arefs = before.get("refs", {}), after.get("refs", {})
        bad = [k for k in brefs
               if brefs[k] is not None and arefs.get(k) is not brefs[k]]
        out = [f"⛔ 子代理返回后发现父级状态被改动：{k} —— 这违反 W20-B 红线 1，"
               f"请把这条报给主控" for k in bad]
        # 父级台账**不许变短**（变短 = 被 `start_task()` 清空，正是红线 1 的那个症状）。
        # 变**长**是合法的，而且必须区分清楚是哪一种长法：
        #   * 长出 `subagent` 自己那一条 —— 注册表在 `fn()` 返回之后才落账，
        #     所以父实例的 `_seq` 也一起 +1。这是**设计如此**。
        #   * 长出别的 —— 说明有子级调用漏进了父级台账。那是红线 2 的破口。
        # 原实现把这条直接判红，于是**每一次正常委派都会喊一声狼来了**；
        # 而"每次都响"的告警，和没有告警是同一件事。
        delta = after.get("trace_len", 0) - before.get("trace_len", 0)
        if delta < 0:
            out.append("⛔ 父级台账长度**变短**了（被清空？）—— 同红线 1")
        elif delta > 0:
            extra = [c.name for c in self._parent_trace()[before["trace_len"]:]]
            unexpected = [n for n in extra if n not in DELEGATING_TOOLS]
            if unexpected:
                out.append(f"⛔ 父级台账里多出了非委派条目 {unexpected} —— "
                           f"子级的调用不该进父级台账（红线 2）")
        return out


class _Collector:
    """子实例的事件**终点站**：在本地数数，一条都不转给父级（红线 2）。

    为什么还留着它而不是干脆 `on_event=None`：工具调用次数要从台账算，
    而「它有没有真试过交付」只有事件里看得见。这两件事都**必须留在本地**。
    """

    def __init__(self) -> None:
        self.deliver_attempts = 0

    def __call__(self, kind: str, data: dict) -> None:
        if kind == "deliver_attempt":
            self.deliver_attempts += 1


def _prune_registry(registry: Any, allowed: list[str]) -> list[str]:
    """把子实例的工具表**收敛成 allowed**，返回被摘掉的名字。

    ★ 这里动的是 `registry._tools`（一个私有字典），是**权衡后的选择**：
      本刀的禁改清单不含 `tools.py`，而"允许清单"要成为**结构性保证**，
      就必须让工具**既不出现在 schema 里、也不能被真的调起来**。
      只在 `openai_schemas(allow=...)` 上过滤做不到后者 —— 模型凭记忆
      发一个调用名，注册表照样会执行它。那就退化成"靠提示词嘱咐"，
      而本工单第 4 节点名批评的正是这种退化。
      干净的落点是给 `ToolRegistry` 加一个 `retain(names)`，
      留到能改 `tools.py` 的那一刀。
    ★ 动的是**子实例自己刚建出来的注册表**，父实例的注册表一个字节都没碰。
    """
    table = getattr(registry, "_tools", None)
    if not isinstance(table, dict):
        return []
    keep = set(allowed)
    dropped = sorted(n for n in table if n not in keep)
    for n in dropped:
        del table[n]
    return dropped


# ── `subagent` 工具的 schema（放本模块，`loop.py` 只做注册）──────────
SUBAGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": ("交给子代理的**完整任务描述**。它看不到你的上下文，"
                            "所以这里要写全：要什么结论、什么范围、什么算做完。"),
        },
        "profile": {
            "type": "string",
            "description": ("子代理角色名。内置 `explore`（只读检索，不注入项目规约）"
                            "与 `coder`（通用干活）。留空 = coder。"),
        },
    },
    "required": ["task"],
}
