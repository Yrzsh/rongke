# -*- coding: utf-8 -*-
"""荣珂工具注册表。

设计要点（与"对话循环"的本质区别）：
  * 工具是**一等公民**：注册表知道每个工具的名字、JSON Schema、是否改动世界、
    属于哪个领域。模型看到的 tools 参数由注册表生成，不手写。
  * 每次调用都产出结构化 ToolCall 记录（含 ok / ms / 结果哈希 / 序号），
    这是"完成证据门"唯一承认的证据来源——**模型嘴上说的不算**。
  * 支持**钩子**：调用前后可拦截（工作区围栏、审批、审计）。
  * 收敛检测需要调用指纹：同工具同参数连打就报警。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ══════════════════════ 结果锚点（Q 型缺陷的最小修复）══════════════════════
# 问题：归档与事件里原先**只有结果的摘要哈希 `digest`**。
#   后果一：证据门第 4 条（用工具结果文本核对验收标准）**离线不可复算** ——
#           任何"只审归档"的复核在架构上不可能。
#   后果二：界面只能显示 "结果哈希 c3cbd63dc4fa"，用户无从核验它到底查到了什么。
#   后果三：一份能交给第三方的审计包，只能让人**相信我们打印的判定**，不能让人重算。
#
# 治法不是"把结果原文塞进归档"（一次长检索就能让归档膨胀到几十 MB），
# 而是**留下可核对的锚点**：结果里的 DOI / URL / 数值。
# 这三样正好是"结论里最常被凭空写出来的东西"，也是字符串级可复核的：
#   * 数字锚点 —— 15.2% 是不是真的在检索结果里？
#   * 引文锚点 —— 那个 DOI 是不是真的被某次核验调用返回过？
# 纪律：**截断必须自报**（`result_truncated`），否则 800 字的预览会被
# 当成完整结果 —— 那是 M 型缺陷（把"没看全"渲染成"看过了"）。
RESULT_PREVIEW_CHARS = 800        # 预览给到"人眼够核一段"的长度
ANCHOR_LIMIT = 40                 # 每条锚点各自的上限（防单次超长结果把归档撑爆）

_RE_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>()\[\],;]+")
_RE_URL = re.compile(r"https?://[^\s\"'<>()\[\]]+")
# 数字锚点**必须收**：整数恰恰是结论里最常被引用的量（"350 份样本"、"12 篇"），
# 漏掉它，下游"正文里的数值必须能追到证据"这条判据会大面积假阴性 ——
# 而假阴性会让人把整条检查关掉，那比不查更糟。
# 守卫：前后不许贴字母（挡住 ophiocordyceps2、v1.2 这类标识符里的数字），
# 支持千分位与科学计数法。
_RE_NUM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?![\w])")
# 句末标点会粘在 DOI / URL 上（中文的「。」「，」尤其容易）——
# 带一个错标点的锚点**必然匹配不上**，于是看起来像"证据不足"，实则是采集器的错。
_TRAIL = ".,;:!?、)]}>,。；：！？）】」』…"


def _strip_trail(s: str) -> str:
    while s and s[-1] in _TRAIL:
        s = s[:-1]
    return s


def _dedup_cap(items: list[str], limit: int = ANCHOR_LIMIT) -> list[str]:
    """去重保序 + 限量。保序而不是排序：锚点要能按**在结果里出现的先后**排，
    否则界面上呈现的顺序与原文无关，人核对时得来回找。"""
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
            if len(out) >= limit:
                break
    return out


def extract_anchors(text: str, limit: int = ANCHOR_LIMIT) -> dict:
    """从工具结果里抽出可逐字核对的三类锚点：DOI / URL / 数值。

    刻意**不做语义抽取**（不判断"这句话是不是结论"）——
    语义判断会误报，而误报的代价是让整份锚点被无视。
    这里只做字符串级事实：它出现过，或它没出现。

    ⚠️ **抽取是有先后的，且必须先抽长、再抽短，并把已抽走的挖掉。**
    首版直接在原文上依次抽三类，实测立刻出错：DOI `10.1038/s41586-021-03456-7`
    被数值规则**再切一次**，凭空多出 `021` / `03456` / `7` 三个假锚点。
    后果不是"多几条噪声"，而是**下游判据会被假通过** ——
    随手写个 `021` 都能在证据里"追到"。这就是 A 型（判据在错误的抽象层）：
    数值锚点该回答"这个量有没有被量出来过"，而不是"这串字符出现过吗"。
    """
    if not text:
        return {}
    t = str(text)
    # ① 高精度锚点先抽（含 e-mail/DOI 这类长串的兜底顺序）
    doi = _dedup_cap([_strip_trail(m.group(0)).lower() for m in _RE_DOI.finditer(t)], limit)
    url = _dedup_cap([_strip_trail(m.group(0)) for m in _RE_URL.finditer(t)], limit)
    # ② 把它们从文本里**挖掉**（换成等长空格，保住位置不影响后续正则的边界判断），
    #    再在剩余文本上抽数值 —— 同一串字符只归一类锚点。
    masked = _RE_DOI.sub(lambda m: " " * len(m.group(0)), t)
    masked = _RE_URL.sub(lambda m: " " * len(m.group(0)), masked)
    # 数值规范化：去掉千分位逗号，让 "1,234" 与 "1234" 能对上
    raw_num = [m.group(1).replace(",", "") for m in _RE_NUM.finditer(masked)]
    # ★ **单位数不作数**（真实运行里立刻暴露的一条）。
    #   第一次真跑，"装载技能"这类文本正文里的编号清单产出了一整套锚点
    #   `['1','2','3','4','5','6']` —— 那是列表序号，不是被量出来的数。
    #   后果不是"多几条噪声"，而是**整条判据退化成恒真**：
    #   任何稿子里写个 "3" 都能在证据里"追到"，于是"数字必须追到证据"这句话
    #   永远成立、什么都拦不住 —— 一个拦不住东西的检查比没有检查更糟
    #   （它会让人以为这一层已经被守住了）。
    #   取舍：宁可漏掉"样本量 3"这种真单位数，也不收编号清单 ——
    #   同"套话看密度不看条数""图注只认行首"是同一条纪律：
    #   **挑得出来的才报，挑不出来的别报。**
    num = _dedup_cap([n for n in raw_num if len(n) >= 2], limit)
    out: dict[str, list[str]] = {}
    if doi:
        out["doi"] = doi
    if url:
        out["url"] = url
    if num:
        out["num"] = num
    return out


def _fingerprint(name: str, args: dict) -> str:
    """调用指纹：用于死循环检测。参数排序后哈希，保证 {"a":1,"b":2} 与 {"b":2,"a":1} 同指纹。"""
    try:
        blob = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        blob = str(args)
    return hashlib.sha256(f"{name}|{blob}".encode("utf-8")).hexdigest()[:16]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                      # JSON Schema
    fn: Callable[..., Any]
    domain: str = "core"
    mutating: bool = False                # 是否改动外部世界（写盘 / 联网提交）
    source: str = "builtin"               # 工具来源：builtin / workbench / plugin

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or self.name,
                "parameters": self.parameters
                or {"type": "object", "properties": {}, "required": []},
            },
        }


@dataclass
class ToolCall:
    """一次工具调用的完整事实记录。证据门只认这个。"""
    seq: int
    id: str
    name: str
    args: dict
    ok: bool
    ms: int
    result: str
    error: str = ""
    fingerprint: str = ""
    domain: str = "core"
    mutating: bool = False
    blocked_by: str = ""                  # 被哪个钩子拦下的
    # 工具**真正返回**了多少字符（截断之前）。
    # 为什么必须结构化记下来、而不是回头从 "…（已截断，原长 N 字符）" 里正则捞：
    #   ① 那条标记在 12,000 字文本的**尾巴上**，而界面给人看的是**头部 800 字** ——
    #      实测过：预览里根本看不到它，于是"截了两层"被渲染成"工具就返回了这么点"（M 型）。
    #   ② 本项目定律：能算的就不许靠匹配。内核量得出来，就当场记下。
    # 纪律：**截断必须自报**（`result_truncated`），否则 800 字的预览会被
    # 当成完整结果 —— 那是 M 型缺陷（把"没看全"渲染成"看过了"）。
    orig_chars: int = -1
    ts: float = field(default_factory=time.time)

    def digest(self) -> str:
        h = hashlib.sha256(self.result.encode("utf-8", errors="replace")).hexdigest()
        return h[:12]

    def brief(self, n: int = 300) -> str:
        head = self.result[:n].replace("\n", " ")
        return (f"#{self.seq} {self.name}({json.dumps(self.args, ensure_ascii=False)[:120]}) "
                f"-> {'ok' if self.ok else 'ERR'} {head}")

    def to_dict(self, preview: int = RESULT_PREVIEW_CHARS) -> dict:
        """给界面与归档的形态。

        `digest` 语义**一字未动**（存量归档、去重、指纹全部照旧），
        新增的是三类**派生**字段 —— 它们是"当时值"的快照，不是现值：
        结果原文只在这次调用里存在过，事后无法重算，所以必须当时记下。

        * `result_chars`   —— 内核实际持有的长度（截断后）
        * `result_truncated` —— 预览是否被截断。**必须自报**：
          少这个字段，800 字的预览就会被读成"结果就这么长"。
        * `result_orig_chars` / `result_dropped` —— 工具**真正返回**的长度，
          以及因为两层截断而没被带出来的字数。这两个字段存在的唯一理由：
          让人一眼看出"你看到的这一段是全部的第几成"。
        * `anchors`        —— DOI / URL / 数值。只对**成功**调用产出：
          失败结果（`ok=False`）按定义不是证据，给它配锚点等于给假证据化妆。
        """
        r = str(self.result or "")
        orig = self.orig_chars if self.orig_chars >= 0 else len(r)
        return {
            "seq": self.seq, "id": self.id, "name": self.name, "args": self.args,
            "ok": self.ok, "ms": self.ms, "error": self.error,
            "fingerprint": self.fingerprint, "domain": self.domain,
            "mutating": self.mutating, "blocked_by": self.blocked_by,
            "digest": self.digest(),
            "result_chars": len(r),
            "result_orig_chars": orig,
            "result_dropped": max(0, orig - min(len(r), preview)),
            "result_preview": r[:preview],
            "result_truncated": len(r) > preview or orig > len(r),
            "anchors": extract_anchors(r) if self.ok else {},
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
        }


class ToolRegistry:
    def __init__(self, hooks: list | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.trace: list[ToolCall] = []
        self._seq = 0
        self._fp_counts: dict[str, int] = {}
        self.hooks: list = list(hooks or [])

    # ── 钩子 ────────────────────────────────────────────────
    def add_hook(self, hook) -> None:
        self.hooks.append(hook)

    # ── 注册 ────────────────────────────────────────────────
    def register(self, tool: Tool, overwrite: bool = False) -> None:
        if tool.name in self._tools and not overwrite:
            raise ValueError(f"工具重名: {tool.name}（来自 {self._tools[tool.name].source}）")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """移除一个工具。能力预设**只在本次 run 生效**，跑完必须把临时
        工具撤掉 —— 否则长命单例会把上一场预设的工具泄漏给下一场任务。
        """
        if name not in self._tools:
            return False
        del self._tools[name]
        return True

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    def by_source(self, source: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.source == source]

    # ── 给模型看 ────────────────────────────────────────────
    def openai_schemas(self, allow: list[str] | None = None) -> list[dict]:
        tools = list(self._tools.values())
        if allow is not None:
            allow_set = set(allow)
            tools = [t for t in tools if t.name in allow_set]
        return [t.openai_schema() for t in sorted(tools, key=lambda x: x.name)]

    # ── 任务边界 ────────────────────────────────────────────
    def start_task(self) -> None:
        """开始新任务：清空台账与调用计数，seq 归零。

        seq 是证据门的锚点，必须每个任务独立编号 —— 否则上一轮的旧 seq
        可能被模型误引用，看起来"有证据"实则张冠李戴。
        """
        self.trace.clear()
        self._fp_counts.clear()
        self._seq = 0

    @property
    def next_seq(self) -> int:
        """**预测**下一次调用将使用的 seq（只读，不消耗序号）。

        存在的理由：主循环要在调用**开始前**广播 tool_start，此时调用还没发生、
        台账里还没有那条记录。前端拿这个 seq 建"运行中"的占位行，
        调用结束后再用真实 ToolCall 的 seq 接上。
        """
        return self._seq + 1

    # ── 调用 ────────────────────────────────────────────────
    # ── 参数预校验 ──────────────────────────────────────────
    # 模型最常见的失误是漏传必填参数。直接 fn(**args) 只会抛 Python 签名错误，
    # 例如实测（本机 2026-09-18）record_skill_use 收到空参数时给出
    # "missing 2 required positional arguments: 'skill' and 'project'" ——
    # 措辞是解释器的，模型不好定位。按 schema 先查一遍，把"缺什么、该传什么"说清楚。
    #
    # **只查缺少的必填项，不查未识别项**：桥是从工具签名反射出 schema 的，
    # 若某次反射漏了字段，拦截未识别参数反而会误伤原本能成功的调用。
    @staticmethod
    def _missing_required(tool: "Tool", args: dict) -> list[str]:
        params = tool.parameters or {}
        props = params.get("properties") or {}
        if not props:
            return []                     # 无 schema：不判，交给 fn 自己
        required = params.get("required") or []
        return [k for k in required if k not in args]

    @staticmethod
    def _usage_hint(tool: "Tool") -> str:
        props = (tool.parameters or {}).get("properties") or {}
        if not props:
            return "（该工具不需要参数）"
        return ", ".join(str(k) for k in props)

    def call(self, name: str, args: dict | None = None, truncate: int = 12000,
             parse_error: str = "", tcid: str = "") -> ToolCall:
        """执行一次调用并**如实记账**。

        `tcid` = 这次调用在 OpenAI 协议里的 `tool_calls[].id`。它此前恒为空串，
        于是台账和消息栈之间**没有任何连接** —— 上下文压缩要替模型把某条旧工具
        结果换成一份由台账现算的替身（`context.stub_for`），就必须知道"这条消息
        对应哪次调用"，而按位置对齐是不成立的：`deliver_task` / `report_blocker`
        的回灌不经过注册表（见 `loop.py` 那两个分支），位置从一开始就会错开。
        默认空串 ⇒ 现有全部调用方与测试的行为逐字不变。
        """
        args = dict(args or {})
        self._seq += 1
        seq = self._seq
        fp = _fingerprint(name, args)
        self._fp_counts[fp] = self._fp_counts.get(fp, 0) + 1
        t0 = time.time()

        tool = self._tools.get(name)
        if tool is None:
            tc = ToolCall(seq=seq, id=tcid, name=name, args=args, ok=False, ms=0,
                          result="", error=f"未注册的工具: {name}", fingerprint=fp)
            self.trace.append(tc)
            return tc

        # ── 参数没解析出来 ⇒ **绝不执行**，且如实说是「解析失败」而不是「没传」──
        # 两条理由，缺一不可：
        #  ① 口径（M 型）：把它当成"你没传参数"是**假的** —— 模型明明传了，
        #    只是它的 JSON 断了，于是反馈自相矛盾，模型无从下手（见 `_unpack`）。
        #  ② 安全：下面的 `_missing_required` 对**不要求必填参数**的工具返回空列表，
        #    于是坏参数会被当成 `{}` **真的执行下去**。一个无参的副作用工具
        #    就这样在"参数没读懂"的情况下动了手。"没读懂就别动手"没有例外。
        # 仍然记进 `trace`：参数坏掉是"模型可靠性"的一手信号，不该只活在日志里。
        if parse_error:
            tc = ToolCall(
                seq=seq, id=tcid, name=name, args=args, ok=False, ms=0, result="",
                error=(f"⛔ 你的 arguments **不是合法 JSON**，没能解析成参数对象。\n"
                       f"   所以这**不是**「你没传参数」，是「你传的东西坏了」。\n"
                       f"   解析器报：{parse_error}\n"
                       f"   这一次调用**没有执行** —— 即使该工具不要求必填参数，"
                       f"内核也不会拿空参数替你去跑。\n"
                       f"   请**重新发出**一次调用，确保 arguments 是完整闭合的 JSON 对象。"),
                fingerprint=fp, domain=tool.domain, mutating=tool.mutating,
                blocked_by="parse_error")
            self.trace.append(tc)
            return tc

        # ── 前置钩子（工作区围栏等）──
        for hook in self.hooks:
            try:
                decision = hook.before_call(tool, args)
            except Exception as e:
                decision = None
                _ = e
            if decision is None:
                continue
            if not decision.allow:
                tc = ToolCall(
                    seq=seq, id=tcid, name=name, args=args, ok=False, ms=0, result="",
                    error=f"[被 {getattr(hook, 'name', 'hook')} 拦截] {decision.reason}",
                    fingerprint=fp, domain=tool.domain, mutating=tool.mutating,
                    blocked_by=getattr(hook, "name", "hook"),
                )
                self.trace.append(tc)
                return tc
            if decision.args is not None:
                args = decision.args

        missing = self._missing_required(tool, args)
        if missing:
            given = ", ".join(str(k) for k in args) or "（一个都没传）"
            tc = ToolCall(
                seq=seq, id=tcid, name=name, args=args, ok=False, ms=0, result="",
                error=(f"⛔ 参数不完整，**未执行** {name}。\n"
                       f"  缺少必填参数：{', '.join(missing)}\n"
                       f"  你传的是：{given}\n"
                       f"  本工具接受：{self._usage_hint(tool)}\n"
                       f"  请补齐后重新调用（不要重复同样的空调用）。"),
                fingerprint=fp, domain=tool.domain, mutating=tool.mutating)
            self.trace.append(tc)
            return tc

        try:
            out = tool.fn(**args)
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
            ok, err = True, ""
        except TypeError as e:
            # 参数不匹配是最常见的模型失误，单独报清楚
            text, ok, err = "", False, f"参数不匹配: {e}"
        except Exception as e:
            # ⚠️ 这里**刻意不挂 traceback**（2026-09-19 实测删掉，理由要留着）：
            #    原实现是 `err = f"{type(e).__name__}: {e}"` 再 `+= format_exc(limit=3)`，
            #    而 `format_exc()` 的结尾本身就是 `RuntimeError: <同一条消息>` ⇒
            #    **同一条消息被印两遍**，中间夹 3 帧栈。
            #    实测代价（runs/20260919-092312）：一次 load_skill 路径写错，
            #    回灌文本 **6,272 字符**，其中一半是重复的 73 行文件清单、外加
            #    `tools.py:225 / loop.py:222` 两帧 —— 约 11% 的整场输入是这份噪声，
            #    而且它**每一步重发**（同 `line()` vs `line_detail()` 的道理：
            #    流向模型的东西只该放对模型有决策价值的东西）。
            #    栈帧对模型零价值（它改不了我们的代码），要调试就看 CLI/控制台/trace。
            text, ok, err = "", False, f"{type(e).__name__}: {e}"

        orig_chars = len(text)
        if len(text) > truncate:
            text = text[:truncate] + f"\n…（结果过长，已截断，原长 {len(text)} 字符）"
        # 报错同样要设上限：**成功有截断、失败没有**是个不对称 ——
        # 一个失败的工具（尤其把整个目录清单/上游 body 拼进消息的那种）能注入无限长文本，
        # 而它同样每一步重发。上限与成功路径共用同一个 `truncate`，不引入第二套阈值。
        if len(err) > truncate:
            err = err[:truncate] + f"\n…（报错过长，已截断，原长 {len(err)} 字符）"

        tc = ToolCall(seq=seq, id=tcid, name=name, args=args, ok=ok,
                      ms=int((time.time() - t0) * 1000), result=text, error=err,
                      fingerprint=fp, domain=tool.domain, mutating=tool.mutating,
                      orig_chars=orig_chars)
        self.trace.append(tc)

        for hook in self.hooks:
            try:
                hook.after_call(tc)
            except Exception:
                pass
        return tc

    # ── 收敛 / 死循环检测 ───────────────────────────────────
    def repeated_call(self, tc: ToolCall, threshold: int) -> bool:
        return self._fp_counts.get(tc.fingerprint, 0) >= threshold

    def successful(self) -> list[ToolCall]:
        return [c for c in self.trace if c.ok]

    def calls_by(self, name: str) -> list[ToolCall]:
        return [c for c in self.trace if c.name == name]


# 裁掉这两个 = 把「完成」和「诚实退出」两条通道一起拔掉，模型再努力也交不了稿。
# ★ 2026-11-05 补 `load_skill` / `read_skill`：门禁第 6 条（阶段专属技能必须
#   装载）是**硬门**，预算把它裁掉就等于造一个不可能通过的门 —— 真写作场
#   strict-v2 实测被卡 42s 后报阻塞（归档 `runs/20261105-1950*`），理由原文
#   「工具清单里没有 load_skill … 我不愿编造一个假的 call_seq」。
#   同一条不变量：**门会拦的动作，工具清单必须够得着**。宁可让预算被必留
#   通道吃掉并如实报 `essential_over_budget`，也不许把硬门拆了还回 ok。
#   （`read_skill` 是工作台兼容路径，不在 registry 时加了也不占位。）
ESSENTIAL_TOOLS = ("deliver_task", "report_blocker",
                   "load_skill", "read_skill")


def schema_chars(schemas: list[dict]) -> int:
    """与事件流里 `schema_chars` 同一个口径，别在两处各算一遍。"""
    return len(json.dumps(schemas, ensure_ascii=False, default=str))


def apply_schema_budget(schemas: list[dict], budget: int,
                        essential=ESSENTIAL_TOOLS) -> tuple[list[dict], list[str]]:
    """给模型看的工具清单做**确定性**硬预算裁剪。

    学 ITR 的「按需暴露」，但守住 prompt caching 的硬规则：只在开场算一次，
    结果整场冻结，不每步变形。顺序不是随手排的 —— `openai_schemas()` 已按
    工具名排序，这里保持原序再按「必留 → 其余」分组，同一份输入永远同一个结果。

    必留工具即使单独超预算也保留：宁可报「预算被必留通道吃掉」，也不许
    把交付门拆了还回 ok。裁掉的名字**全部返回**，交给调用方发事件 ——
    悄悄丢工具是这个改动最不能犯的错。
    """
    if not budget or budget <= 0 or not schemas:
        return list(schemas), []

    def _name(s):
        try:
            return s["function"]["name"]
        except (KeyError, TypeError):
            return ""

    must = [s for s in schemas if _name(s) in set(essential)]
    rest = [s for s in schemas if _name(s) not in set(essential)]
    kept = list(must)
    dropped: list[str] = []
    for s in rest:
        # 严格按整体口径量：逐个重算（开场一次、66 个工具，开销可忽略），
        # 免得「单个 schema 长度累加」与事件里的 `schema_chars` 对不上，
        # 又变成一处「自称在预算内」的假账。
        if schema_chars(kept + [s]) > budget:
            dropped.append(_name(s))
            continue
        kept.append(s)
    return kept, dropped


def scope_fallback(schemas: list[dict], registry: "ToolRegistry", name: str) -> dict | None:
    """工具可见性的置信度门控回退（ITR 在荣珂的形态）。

    `mounting_decision` 裁掉的是**给模型看的 schema**，注册表里工具其实还在，
    `registry.call()` 照样能执行。所以缺的从来不是「调得到」，而是「看得见」：
    模型凭经验点了一个本轮没暴露的名字，不该被当成幻觉打回。

    放开一次并记事件，之后整场保持放开（一次性扩张，不是每步变形 ⇒
    前缀缓存只 miss 一次）。名字在注册表里也查不到 ⇒ 返回 None，
    **内核不替模型编工具** —— 真幻觉仍由 `registry.call` 如实报「未注册」。
    """
    visible: set[str] = set()
    for s in (schemas or []):
        try:
            visible.add(s["function"]["name"])
        except (KeyError, TypeError):
            continue
    if name in visible:
        return None
    tool = registry.get(name) if registry is not None else None
    return tool.openai_schema() if tool is not None else None
