# -*- coding: utf-8 -*-
"""荣珂配置层。

优先级（后者覆盖前者）：
    内置默认  →  ~/.rongke/config.json  →  项目内 config.json  →  环境变量 RONGKE_*

密钥的读取（W19-A 起，两级，前者覆盖后者）：
    环境变量 <api_key_env>  →  ~/.rongke/secrets.json 里的同名键
**密钥依旧不进 config.json**：配置里存的仍然只是变量名（api_key_env）。
`secrets.json` 是**另一个文件、只在家目录**，理由见下面 `SECRETS_PATH` 的注释。
默认值对齐用户本机已验证的 Command Code（GOAT 套餐）。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .mcp_client import parse_servers

DEFAULTS: dict[str, Any] = {
    "provider": {
        "name": "commandcode",
        "base_url": "https://api.commandcode.ai/provider/v1",
        "api_key_env": "COMMANDCODE_API_KEY",
        "model": "deepseek/deepseek-v4.1-flash",
        # ⚠️ 不可省：用户实测该网关不收到 reasoning_effort 就"静默不思考"，
        #    而且不报错，只是安静地给出看起来正常的错答案。
        "reasoning_effort": "medium",
        "send_reasoning_effort": True,
        "max_tokens": 8192,
        # 网关前置 Cloudflare 会拦「通用 UA」（Python-urllib 默认 UA → HTTP 403/1010）。
        # 留空则用 llm.py 里的 "Rongke/0.0.1 (research agent)"。
        "user_agent": "",
        # ★ CodeBuddy（copilot.tencent.com/v2）**只支持流式**：
        #   `stream:false` 回 HTTP 400 `{"code":11101,...Non-stream...not supported}`。
        #   换到那条通道时必须同时把它置 true，否则每一次调用都 400。
        #   默认 false —— 见 Provider 里那段注释（默认值反了会改坏所有老通道）。
        "stream_required": False,
    },
    "loop": {
        "max_steps": 40,
        "max_identical_calls": 3,   # 同一工具+同一参数重复 N 次 → 判死循环
        "temperature": 0.3,
        # 网关会**间歇性长挂起**（实测：同一进程内 1.5s 与 247s 交替出现）。
        # 正常调用都在 20s 内，所以把超时压到 120s：挂起时能更快触发重试，
        # 而不是让用户干等 4 分钟。240s 实测过一次「240s 超时 + 7s 重试成功」，
        # 压到 120s 可把最坏情况从 ~247s 降到 ~127s。
        "request_timeout": 120,
        "max_observation_chars": 6000,  # 工具结果回灌给模型前的截断
        # 单次请求体量超过它就发一次告警事件。**默认 0 = 关**：
        # 它的价值是"让上下文膨胀可见"，而不是"每步都叫一声"
        # （噪声会教人学会无视告警 —— 与本项目"误报比漏报更伤"同一条纪律）。
        # 想看时把它调成 120000 之类即可，一次任务最多告警一次。
        "context_warn_chars": 0,
        # ── 上下文经济（2026-09-25 加，实现见 `rongke/context.py`）─────
        # 历史超过这个字符数就出手出清。0 = 关。**默认开着**，理由：溢出此前是
        # 全仓唯一的硬崩路径（400 → LLMError → 主循环不接 → 判「执行中断」，
        # 跑了 20 步的进度当场作废）。
        # 阈值定得高，是为了让它成为**急刹车而不是例行打扫**：每次出清要付一次
        # 前缀缓存 miss，而实测命中率中位 94.4% —— 轻易压一下在成本上是亏的。
        # 验收判据因此写着「≤1 次/场」。
        "compact_target_chars": 120000,
        "compact_keep_live": 6,      # 最近这么多条消息绝不脱水（≈最后两步）
        # 收尾提示：剩余步数不足这么多时，明确说"停止新检索、先产出交付物"。
        # 此前只有 `remind_every=8` 重申目标，**与剩余量无关** —— 那 4 场烧到
        # 110 万 token 后被人工取消的任务，全程没有任何一道闸会拦。0 = 关。
        "budget_hint_steps": 5,
        # prompt token 闸的**分母**。0 = 不启（默认）：网关是第三方转售，上下文
        # 窗口没有可核对的声明，凭空写一个数就是造一个假数 —— 与 `pricing` 宁返
        # UNKNOWN 不返 0 同一条纪律。知道自己模型的窗口后再填这个数。
        "budget_max_prompt_tokens": 0,
        "budget_warn_ratio": 0.70,    # 到这个比例：提示"别再开新战线"
        "budget_force_ratio": 0.85,   # 到这个比例：强制走满出清阶梯
        # 工具按领域按需挂载。默认开。关掉 = 回到"每场任务都全量下发 schema"。
        # 实测（2026-09-21）：全量 50,244 字符，其中 ppt 一个域 34,461（68.6%）。
        "domain_mount": True,
    },
    "runtime": {
        "workbench": "",            # 空 = 自动探测 ~/.dsh/papers/workbench
        "evidence_dir": "evidence",
        "log_dir": "logs",
        # 每次任务一个独立工作区（围栏的根）。模型的相对路径产物都落在这里，
        # 不会污染项目目录 —— 首跑就踩过：它把项目建在了 cwd。
        "workspace_dir": "runs",
        # 记忆根（会话台账 + 经验层）。测试/实验必须能定向，否则"用户多跑几场"
        # 就会改变判据读到的东西 —— 2026-09-26 实测咬过一次，见 `memory.default_root`。
        "memory_dir": "memory",
    },
    # ── MCP 服务端（荣珂作为**客户端**去连它们）────────────────
    # 在这之前只有 bridge.py 那条同进程反射通道，方向是"别人连工作台"；
    # 荣珂自己够不到任何 MCP 服务端。这一段就是把方向掰回来。
    #   import  —— 直接吃别的客户端已有的配置（如 ~/.qoder-cn/mcp.json），
    #              省得把同一份 server 定义抄一遍（抄了就会漂）
    #   servers —— 荣珂自己的定义/覆盖，同名以后者为准
    # enabled=False 是**唯一**的开关，只在 Config.load 里判一次。
    "mcp": {
        "enabled": True,
        "import": [],
        "servers": {},
    },
    # ── 图面体检 ────────────────────────────────────────────
    # 渲染后的审计要 matplotlib / PyMuPDF，而它们**只装在系统 Python312**
    # （托管 3.13.12 连 numpy 都没有）。荣珂跑在哪个解释器上，不该决定
    # 它能不能审图 —— 和 MCP 那边同一条判断：需要特定解释器的是**被调用的那一侧**，
    # 于是由配置指路，figure.py 自己只用标准库。
    #   python      —— 跑审计/渲染的解释器绝对路径（留空 = 自动探测）
    #   skills_dir  —— nature-figure 技能根（留空 = 自动探测 + 环境变量）
    "figure": {
        "enabled": True,
        "python": "",
        "skills_dir": "",
        # 审计子进程超时。碰撞审计要开 PyMuPDF 逐页量几何，
        # 一张大图几秒到几十秒；给足余量，别把慢当成失败。
        "timeout": 300,
    },
    # ── 组会汇报 PPT 体检 ────────────────────────────────────
    # 与 figure 同一形状：**编排**技能自带的审计器 + **收紧**它量得出来却没判定的项
    # + 接进硬门。审计器来自 nature-paper2ppt 的 scripts/audit_pptx_quality.py
    # （纯标准库：zipfile + XML，所以它的解释器要求比 figure 那边低得多）。
    #   python      —— 跑审计的解释器绝对路径（留空 = 自动探测）
    #   skills_dir  —— nature-paper2ppt 技能根（留空 = 自动探测）
    # 阈值是**判据的一部分**，所以可配置：6 页的组会与 40 页的答辩不是一份尺子。
    # 默认值写的是"组会"这一个场景，**别把它当成所有 PPT 的通用标准**。
    "deck": {
        "enabled": True,
        "python": "",
        "skills_dir": "",
        "template": "",
        "timeout": 180,
        "min_slides": 6,
        "max_slides": 40,
        "min_notes_ratio": 0.6,
        "min_font_pt": 12.0,
        "max_title_chars": 40,
        "max_body_chars_per_slide": 600,
        # W9 任务 2：对比度门的**渲染像素校验**。默认关闭是**实测定的**：
        # 本机 COM 渲染一份 5 页 deck = 15.1 秒，而各门本是秒级 ——
        # 开着当默认 = 每次体检慢一个数量级，而绝大多数稿子不压照片。
        "deep_contrast": False,
    },
    # ── 知识库：PDF → Markdown 转换层（2026 新增）────────────
    # 与 figure / deck 同一形状：**重依赖走子进程**，由配置指路解释器，
    # 荣珂自己只用标准库。理由同 figure：荣珂可能跑在裸解释器上，
    # 而 pymupdf4llm 装在系统 3.12 里。
    #   python        —— 装了 pymupdf4llm 的解释器（留空 = 自动探测）
    #   obsidian_vault—— Obsidian 库的**目录**（留空 = 不写 vault，只转 md）
    #   md_dir        —— 转换产物的落点（相对工作区；"." 表示就地）
    #   use_ocr       —— 默认 **False**。实测：一份 21 页双栏论文
    #                    开 OCR 53.2s / 关 OCR 24.4s，而正文只多 1,999 字符
    #                    （占 1.3%）。扫描件才该开 —— 判据见 kb_convert.ocr_advice。
    "kb": {
        "enabled": True,
        "python": "",
        "obsidian_vault": "",
        "md_dir": "knowledge",
        "use_ocr": False,
        "images": False,
        "timeout": 600,
        # 最后兜底：多镜像 Sci-Hub 下载。**默认关**。
        # 为什么默认关：这条路走的是 Sci-Hub，它分发的是出版商版权作品，
        # 多数辖区属侵权，与本项目"合法拿到全文"的定位冲突。
        # 为什么还是留着：litkb 原来的自动下全文失败率高得离谱，而根因是
        # **出版商反爬**（Unpaywall 给了直链，取回来却是 HTML 挑战页/403），
        # 不是"世上没有开放版" —— 实测开这条路能多拿回一批（4 篇里 2 篇）。
        # ⇒ 谁要用谁自己开，且入库台账会标出哪几篇是这条路来的。
        "scihub_fallback": False,
    },
    # ── 子代理（2026-10-01 W20-B）──────────────────────────
    # 主智能体把一件事派给一个**隔离**的子智能体，只收回结果。
    #   enabled     —— 关掉就不注册 `subagent` 工具（模型压根看不见它，
    #                  而不是看得见却调不通 —— 后者会让它反复试并烧步数）
    #   max_turns   —— 子代理的步数上限默认值。**12 是刻意的**：子代理干的是一件
    #                  边界清楚的独立事，跑到几十步通常意味着它已经偏题，
    #                  而那时父级还在等 —— 止损比"再试一次"值钱。
    #                  profile 里的 `maxTurns` 覆盖它（作者说了算）。
    #   max_concurrent —— ★ **本刀固定 1**，写在这里是给下一刀（并行）留的位子，
    #                  不是现在就可调：`subagent.SubagentRunner` 会把它按 1 处理。
    #                  并行需要「谁的工作区、谁的台账、谁去 chdir」三个新答案，
    #                  而这三个答案现在都还没有 —— 先配一个开关就是假承诺。
    "subagent": {
        "enabled": True,
        "max_turns": 12,
        "max_concurrent": 1,
    },
    # ── 计价 ────────────────────────────────────────────────
    # **默认空表是刻意的**：荣珂的默认网关是第三方转售，其单价不该由内核猜。
    # 空表的后果是成本一律显示 `UNKNOWN` —— 那是**正确**的：
    # 它明说"我不知道"，而不是渲染一个 `$0.00` 让人以为免费。
    # 填法：把 models 里填上你的网关实际单价（每 100 万 token 的美元数）。
    #   python -m rongke --pricing   打出一份带实际模型名的骨架
    # cache_read_included_in_input：各家口径不一样，必须显式选（见 pricing.py）。
    "pricing": {
        "models": {},
        "default": None,
        "cache_read_included_in_input": True,
    },
}

# 默认 workbench 位置（用户本机实测路径）
_FALLBACK_WORKBENCH = Path.home() / ".dsh" / "papers" / "workbench"


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"配置解析失败 {p}: {e}") from e


def project_root() -> Path:
    """荣珂项目根目录（rongke/ 的上一级）。

    ★ 装成依赖之后这里会指向 site-packages/，**不能再往里写用户数据**
      （topics/ memory/ runs/ 都在 lib/site-packages 里，一升级就没了，
      而且在只读的虚拟环境/系统 Python 下还会直接报权限错）。
      ⇒ 找**包的代码与随包资产**用它；找**用户的东西**用 `data_root()`。
      这条分工是 2026-10 可安装化改造里加上的，两个函数不要混用。
    """
    env = os.environ.get("RONGKE_ROOT", "").strip()
    if env:
        # 显式指定优先：容器/多环境/开发时切仓库都用它，不必改代码位置。
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """用户数据根（topics/ memory/ runs/ materials/ …）——**可写**、跨升级保留。

    判定顺序：
      1. `RONGKE_DATA`：显式指定（部署/多租户/测试隔离都用它）
      2. 源码树（`project_root()` 下有 `.git` 或 `rongke/`）
         ⇒ 开发态沿用老行为：数据留在仓库里，符合多年来的习惯与备份路径
      3. 以上都不是（装成 site-packages 的正常用户）
         ⇒ `~/.rongke/data`

    ★ 第 3 条是这次改造的**真正目的**：让 `pip install` 的用户能用，
      同时**不改变**现有源码树里任何一条路径 —— 已跑起来的机器零迁移。
    """
    env = os.environ.get("RONGKE_DATA", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    root = project_root()
    # 源码树的判据：包目录与仓库标记同时在。只看 `rongke/` 不够 ——
    # 装成包之后 `site-packages/rongke/` 也存在，会把自己误判成源码树。
    if (root / "rongke").is_dir() and (root / ".git").exists():
        return root
    return Path.home() / ".rongke" / "data"


# ── 密钥文件（2026-10-01 W19-A）──────────────────────────────
#: 密钥落在**家目录下独立的一个文件**里，格式 `{"<环境变量名>": "<值>"}`。
#:
#: ★ 为什么必须有这一层：用户双击桌面窗口跑荣珂（`app.py`），
#:   那种进程**没有 shell 环境变量**。在此之前 `Provider.api_key` 只读
#:   `os.environ`，于是图形界面上永远「未连接」—— 这是"第一次打开就用不了"的根因。
#:
#: ★ 为什么不写进 `config.json`：那个文件在**项目根**，可能被提交、被分享、
#:   被快照。密钥写进去 = 一次 `git add .` 就把密钥推上去了。
#:   家目录这个文件在仓库外面，也天然被 `.gitignore` 之外的物理位置保护。
#:   （本模块 `save_provider_overrides` 的白名单里**永远不会有** `api_key`。）
#:
#: ★ 这个文件**不是**荣珂的配置来源之一：它只承载密钥值，
#:   不参与 `_deep_merge`，因此不会把用户的配置结构搅乱。
def secrets_path() -> Path:
    return Path.home() / ".rongke" / "secrets.json"


def read_secret(name: str) -> str:
    """按环境变量名取密钥值：环境变量优先，其次 `~/.rongke/secrets.json`。

    读不动/文件不存在一律返回空串（不是抛错）：密钥缺失是**一种正常状态**
    （用户还没填），把它变成异常会让 `/api/status` 这种只读探针跟着炸。
    """
    name = (name or "").strip()
    if not name:
        return ""
    env = os.environ.get(name, "").strip()
    if env:
        return env
    p = secrets_path()
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get(name) or "").strip()


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    api_key_env: str
    reasoning_effort: str
    send_reasoning_effort: bool
    max_tokens: int
    user_agent: str = ""
    # ★ 该通道**只支持流式**（2026-10-09 加，CodeBuddy 通道）。
    #   copilot.tencent.com/v2 对 `stream:false` 直接回 HTTP 400
    #   `{"code":11101,"msg":"Non-stream chat request is currently not supported"}`，
    #   所以走它的客户端**必须先加 SSE 聚合层**。
    #
    #   默认 **False** 是有意的：历史配置（commandcode）是能收非流式的，
    #   把默认值反过来会让全部老通道也收到 stream:true，而它们没聚合层 ⇒
    #   正文会变成一整段 SSE 文本。**默认值反了 = 静默改坏所有老通道。**
    stream_required: bool = False

    @property
    def api_key(self) -> str:
        # 环境变量优先，其次 ~/.rongke/secrets.json（见 read_secret 的注释：
        # 图形窗口启动的进程没有 shell 环境变量，必须有文件这一下落脚点）。
        return read_secret(self.api_key_env)

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def diagnose(self) -> str:
        if not self.base_url:
            return "base_url 为空"
        if not self.model:
            return "model 为空"
        if not self.api_key:
            return f"环境变量 {self.api_key_env} 未设置，且 {secrets_path()} 里也没有这个键"
        return "ok"


@dataclass
class LoopConfig:
    max_steps: int
    max_identical_calls: int
    temperature: float
    request_timeout: int
    max_observation_chars: int
    context_warn_chars: int = 0
    # ── 上下文经济 ──────────────────────────────────────────
    # 逐字解释在 DEFAULTS 那张表里（那里写着每条的**为什么**，这里是消费端）。
    compact_target_chars: int = 120000
    compact_keep_live: int = 6
    budget_hint_steps: int = 5
    budget_max_prompt_tokens: int = 0
    budget_warn_ratio: float = 0.70
    budget_force_ratio: float = 0.85
    # 工具按领域按需挂载（2026-09-21 加 PPT 服务端时引入）。
    # 关掉它就回到"每场任务都把所有工具的 schema 发给模型"的老行为 ——
    # 实测那是 50,244 字符（其中 ppt 一个域占 68.6%）。
    # 留这个开关是为了**可回退**：性能类改动悄悄生效、出问题时退不回去，
    # 是比慢更糟的事。
    domain_mount: bool = True


@dataclass
class RuntimeConfig:
    workbench: Path
    memory_dir: Path
    evidence_dir: Path
    log_dir: Path
    workspace_dir: Path


@dataclass
class McpConfig:
    """MCP 客户端的配置。`servers` 已经解析成规格，可直接喂给 MCPHub。

    `rejected` / `sources` 是**如实报告**用的：读不动的配置、v1 不支持
    的远程传输，都要能被看见（静默跳过就是假安慰）。
    """
    enabled: bool
    servers: list                 # list[MCPServerSpec]
    rejected: list[dict]
    sources: list[str]


@dataclass
class FigureConfig:
    """图面体检的配置。

    `python` / `skills_dir` 留空时由 `figure.py` 自动探测；自动探测失败时
    审计会**如实报卡点**（`SKIPPED`，且 SKIPPED 不算通过），
    绝不降级成"跳过审计并当它过了"。
    """
    enabled: bool
    python: str
    skills_dir: str
    timeout: float


@dataclass
class KbConfig:
    """知识库 PDF→Markdown 转换层的配置。与 FigureConfig 同一形状。

    ★ `use_ocr=False` 是**实测选出来的默认值，不是随手写的**：
    同一份 21 页双栏期刊论文，开 OCR 53.2s、关 OCR 24.4s（快 2.2 倍），
    而正文只多 1,999 字符（+1.3%）。默认开 OCR 会让每次入库都白等一倍时间，
    且**这件事是静默的**（控制台只打一行 rapidocr 横幅，不说会慢）。
    扫描件才需要它 —— 判据交给 `kb_convert.ocr_advice` 按文字密度提示。

    ★ `scihub_fallback` 默认 **False**，这个是**边界问题不是性能问题**：
    litkb 的"自动下全文"实测大量失败，而失败原因**不是"世上没有开放版"**，
    而是出版商反爬（实测 Unpaywall 明明给出直链，取回来却是 HTML 挑战页 /
    403）。设 True 会多走一条多镜像 Sci-Hub 兜底 —— 它真能拿回论文
    （实测 4 篇里拿回 2 篇，其中 1 篇是合法路径全败的那篇），
    但 Sci-Hub 分发的是**出版商版权作品**，多数辖区属侵权，与本项目
    "合法拿到全文"的定位相冲突。⇒ **默认关，要用自己开，风险自担**。
    """
    enabled: bool
    python: str
    obsidian_vault: str
    md_dir: str
    use_ocr: bool
    images: bool
    timeout: float
    #: 最后兜底：多镜像 Sci-Hub 下载。**默认 False**（见类 docstring 的理由）。
    #: 打开后，走这条路拿到的每篇都会在入库回包里带 `download_route="sci-hub"`
    #: 与 `download_route_note`，便于事后筛出、剔除。
    scihub_fallback: bool = False


@dataclass
class BillingConfig:
    """本地账单。**不接支付网关** —— 见 `billing.py` 的模块说明。

    默认开启。关掉它只影响"记不记账"，不影响任何任务能不能跑：
    这套东西存在的目的是让人看见花了多少，不是拿它当闸门。
    （闸门在别处 —— 真要限额，应该在**开始任务前**拦，而不是跑到一半中断。）
    """
    enabled: bool = True


@dataclass
class DeckConfig:
    """组会汇报 PPT 体检的配置。与 FigureConfig 同一形状（编排别人的审计器）。

    **阈值是判据的一部分，所以必须可配置**：6 页的组会汇报与 40 页的答辩
    不是一份尺子。默认值取的是"组会"这个场景，写死在代码里就等于把
    「组会」偷偷升级成「所有 PPT 的通用标准」—— 那正是 A 型
    （判据在错误的抽象层：拿「这一次」的身份当「这一类」的身份）。

    阈值只影响**收紧的那三条**（页数 / 备注覆盖率 / 字号 / 长度上限）；
    上游脚本自己的四条判据不受这里影响。
    """
    enabled: bool
    python: str
    skills_dir: str
    #: **品牌模板 .pptx 的绝对路径**（`deck_from_template` 用）。空 = 没有模板。
    #: ⚠️ 刻意**不进仓库**：品牌资产归用户，47MB 的 .pptx 也不该进 git。
    #: 它由 `deck_template_extract.py` 抽成 `docs/deck-templates/<名>/slots.json`
    #: （**那份**才是随库的能力定义）。
    template: str
    #: **用哪份槽位表**（`docs/deck-templates/<名>/slots.json`）。
    #: ★ 与 `template` 是**一对** —— 那份 .pptx 与这份槽位表必须同源，
    #:   否则会变成"开工清单说的是 A 模板的页型、出稿却拿 A 的页号去 B 的稿上填"，
    #:   而那是最难查的一类错（两边都跑得通，只是内容错位）。
    #:   空 = 用 `deck.DEFAULT_TEMPLATE_NAME`；唯一裁决处 `deck.template_name_of()`。
    template_name: str
    timeout: float
    min_slides: int
    max_slides: int
    min_notes_ratio: float
    min_font_pt: float
    max_title_chars: int
    max_body_chars_per_slide: int
    #: 对比度门的渲染像素校验（W9 任务 2）。**默认 False 是判据的一部分**：
    #: 渲染一份 5 页 deck 实测 15.1s（秒级门 ⇒ 数量级劣化），只给"字压照片"
    #: 这类纯 XML 量不到的场景显式打开。关闭时 contrast 门输出逐字不变。
    deep_contrast: bool = False


@dataclass
class SubagentConfig:
    """子代理（W20-B）。与 figure / deck 同一形状：`enabled` 只在 `Config.load` 判一次。

    ★ `max_concurrent` **本刀固定 1**：`SubagentRunner` 拿它去开信号量之前先 clamp。
      并行是下一刀的事（那时才需要回答"两个子代理各自的工作区 / 台账 / chdir 归谁"）。
    """
    enabled: bool
    max_turns: int
    max_concurrent: int


@dataclass
class Config:
    provider: Provider
    loop: LoopConfig
    runtime: RuntimeConfig
    mcp: McpConfig
    figure: FigureConfig
    raw: dict
    pricing: dict = field(default_factory=dict)
    deck: DeckConfig | None = None
    #: 知识库 PDF→Markdown 转换层。默认 None 是为了**向后兼容**：
    #: 直接用 `Config(...)` 手工构造（测试里常见）的老代码不该因此炸掉。
    #: 取用方一律用 `getattr(cfg, "kb", None)` 并对 None 有明确说法。
    kb: KbConfig | None = None
    #: 子代理（W20-B）。同 `kb` / `deck`：默认 None 让**手工构造 Config 的老代码**
    #: 不至于因为多了一个必填字段而炸；取用方 `getattr(cfg, "subagent", None)`。
    subagent: SubagentConfig | None = None
    #: 本地账单（阶段三）。同 `kb`/`deck`：默认 None 让手工构造 Config 的
    #: 老代码不至于因为多一个必填字段而炸；取用方 `getattr(cfg, "billing", None)`
    #: 并对 None 按"默认开启"处理（`billing.py` 里就是这么兜的）。
    billing: BillingConfig | None = None

    @classmethod
    def load(cls, root: Path | None = None) -> "Config":
        # root 是**项目配置**所在目录，所以默认取 data_root()：
        # 源码树里它等于仓库根（老行为不变）；装成依赖后是 ~/.rongke/data，
        # 用户的 config.json 才找得到。`~/.rongke/config.json` 恒在列表最前面，
        # 因此任何情况下都还有一层全局配置兜底。
        root = root or data_root()
        data = dict(DEFAULTS)
        for p in (Path.home() / ".rongke" / "config.json", root / "config.json"):
            data = _deep_merge(data, _read_json(p))

        # 环境变量覆盖（只覆盖少数高频项）
        env_over = {
            "provider.model": os.environ.get("RONGKE_MODEL"),
            "provider.base_url": os.environ.get("RONGKE_BASE_URL"),
            "provider.api_key_env": os.environ.get("RONGKE_API_KEY_ENV"),
            "provider.reasoning_effort": os.environ.get("RONGKE_REASONING_EFFORT"),
            "runtime.memory_dir": os.environ.get("RONGKE_MEMORY_DIR"),
        }
        for dotted, val in env_over.items():
            if not val:
                continue
            sec, key = dotted.split(".", 1)
            data.setdefault(sec, {})[key] = val

        pv = data["provider"]
        provider = Provider(
            name=str(pv.get("name", "commandcode")),
            base_url=str(pv.get("base_url", "")).rstrip("/"),
            model=str(pv.get("model", "")),
            api_key_env=str(pv.get("api_key_env", "COMMANDCODE_API_KEY")),
            reasoning_effort=str(pv.get("reasoning_effort", "medium")),
            send_reasoning_effort=bool(pv.get("send_reasoning_effort", True)),
            max_tokens=int(pv.get("max_tokens", 8192)),
            user_agent=str(pv.get("user_agent") or ""),
            stream_required=bool(pv.get("stream_required", False)),
        )

        lc = data["loop"]
        loop = LoopConfig(
            max_steps=int(lc.get("max_steps", 40)),
            max_identical_calls=int(lc.get("max_identical_calls", 3)),
            temperature=float(lc.get("temperature", 0.3)),
            request_timeout=int(lc.get("request_timeout", 240)),
            max_observation_chars=int(lc.get("max_observation_chars", 6000)),
            context_warn_chars=int(lc.get("context_warn_chars", 0) or 0),
            # 上下文经济六项。`_deep_merge` 会把 DEFAULTS 与 config.json 合起来，
            # 所以这里的兜底值只在"从裸 dict 造配置"（测试）时用得上。
            # **0 一律表示"这项关着"**，与 `context_warn_chars` 同一个约定 ——
            # 别让同一个文件里出现"0 有时是关、有时是没配"两种读法。
            compact_target_chars=int(lc.get("compact_target_chars", 120000) or 0),
            compact_keep_live=max(0, int(lc.get("compact_keep_live", 6))),
            budget_hint_steps=max(0, int(lc.get("budget_hint_steps", 5))),
            budget_max_prompt_tokens=int(lc.get("budget_max_prompt_tokens", 0) or 0),
            budget_warn_ratio=float(lc.get("budget_warn_ratio", 0.70)),
            budget_force_ratio=float(lc.get("budget_force_ratio", 0.85)),
            domain_mount=lc.get("domain_mount", True) is not False,
        )

        rt = data["runtime"]
        wb_raw = str(rt.get("workbench") or "").strip()
        workbench = Path(wb_raw) if wb_raw else _FALLBACK_WORKBENCH
        runtime = RuntimeConfig(
            workbench=workbench,
            evidence_dir=root / str(rt.get("evidence_dir", "evidence")),
            log_dir=root / str(rt.get("log_dir", "logs")),
            workspace_dir=root / str(rt.get("workspace_dir", "runs")),
            memory_dir=root / str(rt.get("memory_dir", "memory")),
        )

        # MCP：`enabled` 只在这里判一次（别的地方不要再判，否则就是"两处真相"）。
        mcp_raw = data.get("mcp") or {}
        mcp_enabled = mcp_raw.get("enabled", True) is not False
        if mcp_enabled:
            mcp_servers, mcp_rejected, mcp_sources = parse_servers(mcp_raw, root=root)
        else:
            mcp_servers, mcp_rejected, mcp_sources = [], [], []
        mcp = McpConfig(enabled=mcp_enabled, servers=mcp_servers,
                        rejected=mcp_rejected, sources=mcp_sources)

        # 图面体检：同 MCP，`enabled` 只在这里判一次。
        fg = data.get("figure") or {}
        figure = FigureConfig(
            enabled=fg.get("enabled", True) is not False,
            python=str(fg.get("python") or "").strip(),
            skills_dir=str(fg.get("skills_dir") or "").strip(),
            timeout=float(fg.get("timeout", 300)),
        )

        # 组会汇报 PPT 体检：同 figure，`enabled` 只在这里判一次。
        # 阈值给默认值是**有意的**：缺了它这一节，deck.py 就没法判"页数够不够"，
        # 而那正好是上游审计器量得出来、却没有任何判定的那一类。
        dk = data.get("deck") or {}
        deck = DeckConfig(
            enabled=dk.get("enabled", True) is not False,
            python=str(dk.get("python") or "").strip(),
            skills_dir=str(dk.get("skills_dir") or "").strip(),
            template=str(dk.get("template") or "").strip(),
            template_name=str(dk.get("template_name") or "").strip(),
            timeout=float(dk.get("timeout", 180)),
            min_slides=int(dk.get("min_slides", 6)),
            max_slides=int(dk.get("max_slides", 40)),
            min_notes_ratio=float(dk.get("min_notes_ratio", 0.6)),
            min_font_pt=float(dk.get("min_font_pt", 12.0)),
            max_title_chars=int(dk.get("max_title_chars", 40)),
            max_body_chars_per_slide=int(dk.get("max_body_chars_per_slide", 600)),
            deep_contrast=bool(dk.get("deep_contrast", False)),
        )

        # 知识库 PDF→Markdown：同 figure / deck，`enabled` 只在这里判一次。
        # `obsidian_vault` **不在这里 resolve 成绝对路径**：它可能是空的
        # （表示"不写 vault"），也可能是一个还没建的库目录。
        # 空字符串必须保持空 —— `Path("")` 会 resolve 成 cwd，
        # 于是"没配置"会变成"往项目根写笔记"（本仓 kb_root("") 那条
        # 血债的同一形状：**空值不许被静默解释成一个真实位置**）。
        kb_raw = data.get("kb") or {}
        kb = KbConfig(
            enabled=kb_raw.get("enabled", True) is not False,
            python=str(kb_raw.get("python") or "").strip(),
            obsidian_vault=str(kb_raw.get("obsidian_vault") or "").strip(),
            md_dir=str(kb_raw.get("md_dir") or "knowledge").strip() or "knowledge",
            use_ocr=kb_raw.get("use_ocr", False) is True,
            images=kb_raw.get("images", False) is True,
            timeout=float(kb_raw.get("timeout", 600)),
            scihub_fallback=kb_raw.get("scihub_fallback", False) is True,
        )

        # 子代理（W20-B）：同 figure / deck / kb，`enabled` 只在这里判一次。
        # ★ `max_concurrent` 在这里就**clamp 成 1** —— 不是"读出来再说"：
        #   配置文件是用户手写的，写个 4 上去不会有人来解释"其实还是 1"。
        #   在读配置这一层就把口径收死，`SubagentRunner` 拿到的一定是它真能实现的值。
        sa = data.get("subagent") or {}
        subagent = SubagentConfig(
            enabled=sa.get("enabled", True) is not False,
            max_turns=max(1, int(sa.get("max_turns", 12) or 12)),
            max_concurrent=1,
        )

        return cls(provider=provider, loop=loop, runtime=runtime, mcp=mcp,
                   figure=figure, deck=deck, kb=kb, subagent=subagent, raw=data,
                   billing=BillingConfig(
                       enabled=bool((data.get("billing") or {}).get(
                           "enabled", True))),
                   pricing=dict(data.get("pricing") or {}))

    def with_overrides(self, overrides: dict | None = None) -> "Config":
        """把预设的 `config` 小节摊到一份**新 Config** 上（不改 self）。

        存在的理由：`preset.config_patch()` 一直指向这个方法，而它此前
        **根本不存在** ⇒ 预设里写的阈值全部是死数据。这里补上让它生效。

        为什么不复用 `load()`：那个函数同时负责读盘 + 环境变量 + 投影，
        把它拆成 `_project(raw)` 要动 140 行，而本方法只需要覆盖四个已
        投影好的 dataclass 段。用 `dataclasses.replace` 做**字段级**覆盖：
        · 未知段 / 未知键 ⇒ **报错**，不静默吞（拼错的配置必须被看见）；
        · 按现有字段的类型做保守转换（bool/int/float/str），不猜新类型；
        · 原实例一个字节都不动 ⇒ 长命内核里换预设不会污染别的任务。
        """
        from dataclasses import fields as _fields, replace as _replace

        ov = dict(overrides or {})
        if not ov:
            return self
        sections = {"deck", "figure", "kb", "loop"}
        unknown_sec = sorted(set(ov) - sections)
        if unknown_sec:
            raise ValueError(f"with_overrides 不认得配置段 {unknown_sec}"
                             f"（只有 {sorted(sections)}）")
        changes: dict[str, Any] = {}
        for sec, sub in ov.items():
            if not isinstance(sub, dict):
                raise ValueError(f"config.{sec} 必须是对象，实际是 {type(sub).__name__}")
            cur = getattr(self, sec, None)
            if cur is None:
                # deck/kb 可能为 None（手工构造的老 Config）；有覆盖才要求它在
                raise ValueError(f"config.{sec} 这一段在当前 Config 上不存在，覆盖不了")
            allowed = {f.name for f in _fields(cur)}
            unknown = sorted(set(sub) - allowed)
            if unknown:
                raise ValueError(f"config.{sec} 有不认得的键 {unknown}"
                                 f"（该段只认 {sorted(allowed)}）")
            upd: dict[str, Any] = {}
            for k, v in sub.items():
                old = getattr(cur, k)
                try:
                    if isinstance(old, bool):
                        v = bool(v)
                    elif isinstance(old, int) and not isinstance(old, bool):
                        v = int(v)
                    elif isinstance(old, float):
                        v = float(v)
                    elif isinstance(old, str):
                        v = str(v)
                except Exception as e:
                    raise ValueError(f"config.{sec}.{k}={v!r} 转不成该字段类型：{e}") from e
                upd[k] = v
            changes[sec] = _replace(cur, **upd)
        changes["raw"] = _deep_merge(dict(self.raw or {}), ov)
        return _replace(self, **changes)


# ── 配置写入（2026-09-27 W3：设置页换模型）────────────────────
# ★ 这是本模块**唯一**会往盘上写的函数，`Config.load()` 一行没动。
#   为什么写必须单独一个函数而不是让界面直接 json.dump：那文件的里子有
#   大量 `_note` 注释字段和 deck/figure/kb/pricing 段，
#   手抄一份"只写 provider"的序列化器 = 下一次改配置就把它们全丢掉。
#   所以这里走 `_deep_merge`：**读进来、只盖住白名单里那几个键、原样写回**。
#
# ★ 白名单写死在这里，不写在调用方 —— "什么能落到盘上"是配置层的契约，
#   不是某个接口的契约。调用方多一个就漂一份，那正是本项目 H 型的成因。
# ★ 密钥**依旧绝不落盘**：能写的只有 `api_key_env`（一个环境变量**名字**）。
PROVIDER_WRITE_KEYS: tuple[str, ...] = (
    "model", "base_url", "reasoning_effort", "api_key_env", "max_tokens",
    # ★ 换到「只支持流式」的通道（CodeBuddy）时必须能把它一起写下去，
    #   否则设置页改完 base_url 仍留着 stream_required=false ⇒ 每次调用 400。
    #   它是个布尔而不是密钥，落盘没有风险。
    "stream_required",
    # ★ 2026-11-01：`send_reasoning_effort` 必须可写。
    #   默认通道（commandcode）**必须**带它（不带就静默不思考，见 DEFAULTS 注释），
    #   而换成 magpie 网关后荣珂不该替上游决定 —— magpie 自己会转发。
    #   原先它不在白名单里 ⇒ 切到 magpie 之后 `reasoning_effort` 仍在发，
    #   而设置页/CLI 没有任何办法把它关掉：一处能开、没有任何地方能关。
    #   它是个布尔，落盘没有密钥风险。
    "send_reasoning_effort",
)


def provider_write_key(key: str) -> str:
    """把界面传来的键名收敛成 provider 段里的裸键名；不在白名单内返回 ""。

    同时认 `provider.model` 与 `model` 两种写法（前者是配置文档里的口径，
    后者是表单字段名），但**认完之后仍然只有白名单那五个**能过。
    """
    k = str(key or "").strip()
    if k.startswith("provider."):
        k = k.split(".", 1)[1]
    return k if k in PROVIDER_WRITE_KEYS else ""


def save_provider_overrides(updates: dict[str, Any],
                            root: Path | None = None) -> dict[str, Any]:
    """把 provider 段的白名单键写进**项目根 config.json**，其它字段一个都不丢。

    返回 `{"path": 落点, "backup": 备份路径或空, "written": {改了什么}}`。

    四条硬规矩，每条都对应一种真实事故：
      1. **白名单外一律 `ValueError`** —— 不做"聪明的静默忽略"。
         静默忽略会让调用方以为改成功了，而盘上什么都没有。
      2. **写前先备份**成 `config.json.bak_<时间戳>-<短随机>`。
         用户的配置是一手资产，静默覆盖 = 静默的数据事故。
         （后缀带随机：同一秒连改两次是真实发生的，只用时间戳会
         让第二份备份盖掉第一份 —— 备份本身成了被丢的那个。
         分隔符用 `_` 而不是 `-`：`.gitignore` 里现成有 `*.bak_*` 这条规则
         （snapshot.py 的备份走的就是它），跟着它就不用为设置页新加一条排除。
         一份每次都往仓库根目录扔出**未忽略文件**的写接口，会让人 git status 长草。）
      3. **原子写**：先写同目录临时文件再 `os.replace`。
         写一半断电留下的不是"旧配置"，是"读不动的配置"。
      4. **不接受 `api_key`**（值），只接受 `api_key_env`（变量名）——
         见 `PROVIDER_WRITE_KEYS`。这个函数没有能力把密钥写到盘上。
    """
    if not updates:
        raise ValueError("没有要写入的字段")
    clean: dict[str, Any] = {}
    for k, v in updates.items():
        bare = provider_write_key(str(k))
        if not bare:
            raise ValueError(f"不被接受的键：{k}（只允许 "
                             + " / ".join("provider." + x for x in PROVIDER_WRITE_KEYS)
                             + "）")
        clean[bare] = v

    # 写项目配置的落点：**必须**是 data_root()。
    # 这是 write 路径 —— 用 project_root() 会让 pip 装的用户在
    # site-packages 里写配置（只读环境直接报错，可写环境升级即丢）。
    root = root or data_root()
    target = root / "config.json"
    existing = _read_json(target)          # 读不动就抛错，绝不拿空字典往下写

    backup = ""
    if target.exists():
        stamp = (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        bak = target.with_name(target.name + ".bak_" + stamp)
        bak.write_bytes(target.read_bytes())   # 拷，不挪不删
        backup = str(bak)

    merged = _deep_merge(existing, {"provider": clean})
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)                # 同目录 rename：要么旧的、要么新的
    return {"path": str(target), "backup": backup, "written": clean}


# ── 密钥写入（2026-10-01 W19-A）──────────────────────────────
def save_secret(name: str, value: str, *, path: Path | None = None) -> dict[str, Any]:
    """把密钥值写进 `~/.rongke/secrets.json` 的 `name` 键，其它键一个不丢。

    返回 `{"path": 落点, "name": 键名, "chars": 值长度}` —— **不回显值本身**，
    调用方（界面）也就没有把密钥印到页面上的机会。

    与 `save_provider_overrides` 同一套纪律（原子写、先读后合并），
    另加两条这一层特有的：

      1. **只接受"像环境变量名"的键**（`[A-Za-z_][A-Za-z0-9_]*`）。
         键名会成为界面上的一个可写字段，放开成任意字符串等于让用户
         往自己的密钥文件里塞任意结构 —— 下一次读出来还是一样解不动。
      2. **空值 = 删除这个键**，而不是写一个空串。
         "清掉密钥"和"留一个空密钥"在诊断上完全不同：后者会让
         `diagnose()` 说"已设置但为空"，把人引向错误的方向。

    ⚠️ 写的是**家目录**，不是项目根 —— 见 `secrets_path()` 上面那段注释。
    """
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"密钥名不合法：{name!r}（应为一个环境变量名）")

    target = path or secrets_path()
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception as e:
            # 读不动就抛错，绝不拿空字典往下写 —— 否则一次手抖就把
            # 用户其它几条密钥全抹了。
            raise RuntimeError(f"密钥文件读不动，未做任何改动 {target}: {e}") from e

    value = (value or "").strip()
    if value:
        existing[name] = value
    else:
        existing.pop(name, None)

    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(existing, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return {"path": str(target), "name": name, "chars": len(value),
            "cleared": not bool(value)}

