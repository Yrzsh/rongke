# -*- coding: utf-8 -*-
"""荣珂 LLM 客户端（OpenAI 兼容 /chat/completions）。

职责边界：只负责"把 messages + tools 发出去，把 assistant message 拿回来"。
不做循环（归 loop.py）、不做完成判定（归 gate.py）、不做目标锚定（归 contract.py）。

两条本机实测得来的硬约束：
  * reasoning_effort 默认**显式发送**：Command Code 网关收不到它就不思考，
    而且不报错——只是安静地给出看起来正常的错答案。
  * max_tokens 用标准字段名：该网关静默忽略 max_completion_tokens。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Provider


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / HTTP / 返回格式）。"""


class ContextOverflow(LLMError):
    """上下文超出模型窗口 —— 与"调用失败"**不是一回事**，所以必须是独立的类型。

    为什么值得单开一个异常（此前它是全仓唯一的硬崩路径）：
    溢出时网关回 400，走 `llm` 那句"确定性失败、重试无意义"直接抛 `LLMError`，
    主循环不接 ⇒ 被 `_run_serial` 的 `except BaseException` 兜成「执行中断」。
    一场跑了 20 步、烧了 50 万 token 的任务，就这么没了 —— 而且**没有任何降级尝试**。

    `body` 留着上游原文：判据是关键词匹配来的，那就必须让人能核对它凭什么判成溢出。
    只报"溢出了"而藏起依据，是本项目最反感的那种"看着像结论的断言"。
    """

    def __init__(self, message: str, body: str = "", code: int = 0) -> None:
        super().__init__(message)
        self.body = body
        self.code = code


# 上游各家对"上下文太长"的措辞不统一，且**只有 400/413 才可能是溢出** ——
# 拿状态码当判据是错的（400 更常见的原因是参数写错），必须正文里有长度类表述才算。
# 宁可漏判（漏了就照原样抛 LLMError，行为和从前逐字一致），也不要错判：
# 把一个真·参数错洗成"溢出→降级→重试"，会把 bug 藏进恢复路径里，那是新的静默失败。
_OVERFLOW_MARKS = (
    "context_length_exceeded", "context length", "maximum context",
    "input tokens exceeded", "too many tokens", "prompt is too long",
    "input is too long", "reduce your prompt", "exceeds the model",
    "context window", "超出上下文", "上下文长度", "长度超",
)
# 刻意**不收** `max_tokens`：400 里它更常见的意思是"这个参数取值不合法"
# （`max_tokens must be <= 8192`），那是参数错。把它收进来就等于把真 bug
# 洗成"溢出→降级→重试" —— 错判比漏判伤，漏判只是回到旧行为。


def looks_like_overflow(code: int, body: str) -> bool:
    """保守判据：状态码 + 正文措辞两条都命中才算。"""
    if code not in (400, 413, 422):
        return False
    t = (body or "").lower()
    return any(m in t for m in _OVERFLOW_MARKS)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def __add__(self, o: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + o.input_tokens,
            self.output_tokens + o.output_tokens,
            self.reasoning_tokens + o.reasoning_tokens,
            self.cache_read + o.cache_read,
            self.cache_write + o.cache_write,
        )

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }

    def line(self) -> str:
        return (f"in={self.input_tokens} out={self.output_tokens} "
                f"reason={self.reasoning_tokens} cache_r={self.cache_read}")

    @property
    def cache_ratio(self) -> float:
        """缓存命中比例（0~1）。

        为什么要看它：荣珂每次调用都把 SYSTEM（约 3.5k 字符）和工具 schema
        （随工具数增长）原样重发，这是**最值钱也最稳定的缓存前缀**。

        ⚠️ **恒为 0 有两种原因，治法相反**（2026-09-19 实测踩过）：
          (A) 网关真没开前缀缓存  ⇒ 只能等 / 换网关；
          (B) 网关给了命中数，但**我们没读对字段名** ⇒ 缓存一直生效，是我们瞎了。
        本项目实测就是 (B)：同前缀第二次调用命中 95.8%，而读数恒为 0，
        因为解析器当时只认 Anthropic 的字段名。**（见 `_cache_read_from`。）
        所以看到 0 先别下结论，跑 `python cache_probe.py` 看原始响应体里到底有没有那个字段。**
        """
        if self.input_tokens <= 0:
            return 0.0
        return round(self.cache_read / self.input_tokens, 4)


def _cache_read_from(u: dict) -> int:
    """从 usage 里取「缓存命中的输入 token 数」。**各家字段名不同，必须都认。**

    为什么单拎出来（2026-09-19 实测事故）：荣珂跑在 OpenAI 兼容网关 + DeepSeek 模型上，
    命中数落在 `prompt_tokens_details.cached_tokens`，而原实现**只读 Anthropic 的
    `cache_read_input_tokens`** ⇒ 读数恒为 0 ⇒「前缀缓存完全没生效」被写进了优化靶、
    差一点被当成一件待办去"优化"。

    实测反证（`cache_probe.py`，同前缀连发两次）：
        第 1 次  prompt_tokens=5342  cached_tokens=0
        第 2 次  prompt_tokens=5342  cached_tokens=5120   ← 命中 95.8%
    结论：**缓存一直在生效，瞎的是探针。** 这是 N 型缺陷（假阴性守门）的另一种长相——
    功能没坏，只是量不到；而量不到会被读成"不存在"。

    **取值规则：认得的字段里取最大值，不是"第一个命中的"。**
    为什么不能先用先得：该网关**同时**会发 Anthropic 那一侧的残桩 ——
    实测原始体里就有 `"cache_creation_input_tokens": 0`。今天它还没发
    `cache_read_input_tokens`，但一旦哪天补一个值为 0 的残桩，
    "第一个命中的"就会把 5120 覆盖成 0，**静默退回原病**。
    这些字段指的是同一个物理量（被缓存的输入 token 数），所以取 max 是安全且单调的；
    真没命中时全部为 0 或缺失，仍然返回 0。
    """
    cands: list[int] = []
    for k in ("cache_read_input_tokens",   # Anthropic
              "prompt_cache_hit_tokens",   # DeepSeek 原生
              "cache_hit_tokens",
              "cached_tokens"):            # 有些网关的扁平写法
        if u.get(k) is not None:
            try:
                cands.append(int(u[k] or 0))
            except Exception:
                pass
    det = u.get("prompt_tokens_details")   # OpenAI 标准：嵌一层
    if isinstance(det, dict) and det.get("cached_tokens") is not None:
        try:
            cands.append(int(det["cached_tokens"] or 0))
        except Exception:
            pass
    return max(cands) if cands else 0


def _usage_from(data: dict) -> Usage:
    u = data.get("usage") or {}
    return Usage(
        input_tokens=int(u.get("prompt_tokens") or u.get("input_tokens") or 0),
        output_tokens=int(u.get("completion_tokens") or u.get("output_tokens") or 0),
        reasoning_tokens=int((u.get("completion_tokens_details") or {}).get("reasoning_tokens")
                             or u.get("reasoning_tokens") or 0),
        cache_read=_cache_read_from(u),
        cache_write=int(u.get("cache_creation_input_tokens") or 0),
    )


# ── SSE 聚合（2026-10-09 W27：CodeBuddy 通道）──────────────────
# ★ 为什么必须有一层聚合：copilot.tencent.com/v2 **只支持流式**。
#   实测（00_总控/_w27/probe_codebuddy.py）：
#     stream:false → HTTP 400 {"code":11101,"msg":"Non-stream chat request is currently not supported"}
#     stream:true  → 200, Content-Type: text/event-stream, 标准 OpenAI SSE
#   所以"整篇读回再 json.loads"的客户端一律 400。聚合层把 SSE **还原成**
#   一个与流式无关的 `data` 字典，让调用点一行都不用改（qiyi 那边同一条做法）。

def _sse_chunks(raw: bytes) -> list[str]:
    """把 SSE 正文切成 `data:` 载荷串。跳过注释行 / `event:` 行 / 空行。

    上游会插**非 JSON 的保活块**，所以这里只负责切，不负责解析 ——
    解析交给调用方并**跳过错行而不是抛**（抛一次就废掉整轮回答）。
    """
    text = raw.decode("utf-8", errors="replace")
    out: list[str] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue                      # 含 `:` 注释行与 `event:` 行
        payload = line[5:].strip()
        if payload:
            out.append(payload)
    return out


def aggregate_sse(raw: bytes) -> dict:
    """把一段 OpenAI SSE 聚合成**与一次性响应同形**的 `data` 字典。

    聚合语义（每一条都有前科或实测依据）：
      * `delta.content` 逐块拼接 —— 有的上游把正文切片到达。
      * `delta.reasoning_content` / `delta.reasoning` **单独收**，
        最后合成 `reasoning` 字段。**绝不混进 content**：那是模型的思考，
        混进去等于把它当答案回灌上下文。
      * `tool_calls` **按 `index` 累积** `function.arguments` 字符串 ——
        参数是**分片到达**的，覆盖或丢片会让原生 function-calling
        拿到空参数（qiyi 有同类前科）。这里只拼接，不解析。
      * `finish_reason` 取**最后一个非空** —— 末块才是真结局。
      * `usage` 也取**最后一个非空**：实测该通道每个 chunk 都带 usage，
        但值随正文增长而更新，只有最后一个才是全量。
      * `model` / `id` 取首个非空（后面 block 可能缺这两个字段）。

    ★ 这条聚合**不做任何校验**：畸形 JSON 行跳过而不是抛。
      上游插保活块是常态，抛一次就等于让一次正常回答变成"LLM 调用失败"。
    """
    content_parts: list[str] = []
    reason_parts: list[str] = []
    tc: dict[int, dict] = {}
    finish = ""
    usage: dict = {}
    model = ""
    rid = ""
    role = "assistant"

    for payload in _sse_chunks(raw):
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue                       # 保活块 / 畸形行：跳过，不抛
        if not isinstance(obj, dict):
            continue
        if not model:
            model = str(obj.get("model") or "")
        if not rid:
            rid = str(obj.get("id") or "")
        u = obj.get("usage")
        if isinstance(u, dict) and u:
            usage = u                      # 最后一个非空 usage 胜出
        choices = obj.get("choices") or []
        if not choices:
            continue
        ch = choices[0] or {}
        if ch.get("finish_reason"):
            finish = str(ch["finish_reason"])
        d = ch.get("delta") or ch.get("message") or {}
        if d.get("role"):
            role = str(d["role"])
        c = d.get("content")
        if isinstance(c, str) and c:
            content_parts.append(c)
        r = d.get("reasoning_content") or d.get("reasoning")
        if isinstance(r, str) and r:
            reason_parts.append(r)
        for t in (d.get("tool_calls") or []):
            if not isinstance(t, dict):
                continue
            try:
                idx = int(t.get("index") or 0)
            except Exception:
                idx = 0
            e = tc.setdefault(idx, {"id": "", "type": "function",
                                    "function": {"name": "", "arguments": ""}})
            if t.get("id"):
                e["id"] = str(t["id"])
            if t.get("type"):
                e["type"] = str(t["type"])
            fn = t.get("function") or {}
            if fn.get("name"):
                e["function"]["name"] = str(fn["name"])
            if fn.get("arguments"):
                # ★ 拼接，不覆盖：参数是分片到达的。
                e["function"]["arguments"] += str(fn["arguments"])

    msg: dict = {"role": role,
                 "content": "".join(content_parts) if content_parts else None}
    if tc:
        msg["tool_calls"] = [tc[k] for k in sorted(tc)]
    reasoning = "".join(reason_parts).strip()
    if reasoning:
        msg["reasoning"] = reasoning
    return {"id": rid, "model": model, "choices": [{"index": 0,
            "message": msg, "finish_reason": finish}], "usage": usage}


class LLM:
    def __init__(self, provider: Provider, timeout: int = 240,
                 default_temperature: float = 0.3):
        self.p = provider
        self.timeout = timeout
        self.default_temperature = default_temperature

    # ── 诊断 ────────────────────────────────────────────────
    def describe(self) -> str:
        st = self.p.diagnose()
        flag = "OK" if st == "ok" else f"不可用({st})"
        return (f"provider={self.p.name} model={self.p.model} "
                f"base={self.p.base_url} reasoning_effort={self.p.reasoning_effort} → {flag}")

    # ── 主调用 ──────────────────────────────────────────────
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_retries: int = 3,
        meta: dict | None = None,
    ) -> tuple[dict, Usage, str]:
        """返回 (assistant_message, usage, reasoning)。

        assistant_message 形如 {"role": "assistant", "content": str|None, "tool_calls": [...]}，
        **已剔除响应侧字段**，可直接 append 回 messages（含 tool_calls），否则下一轮 API 会报错。
        reasoning 是模型自己的思考正文（可能为空串），只用于展示，不回灌上下文。

        `meta` 是可选的**出参**（传 `{}` 进来即可），回填响应侧的可观测字段：
        耗时 / finish_reason / 响应侧 model / 首次字节。
        为什么做成显式出参而不是实例属性：实例属性是一条**隐藏通道**，
        调用方看不出"这里有东西会变"；出参则把契约写在签名上。
        也刻意不改返回签名 —— 那会波及所有既有调用点，
        而这次改动本不该动它们。
        """
        if not self.p.ready:
            raise LLMError(f"LLM 未就绪：{self.p.diagnose()}")

        payload: dict = {
            "model": self.p.model,
            "messages": messages,
            "temperature": (self.default_temperature if temperature is None
                            else float(temperature)),
            "max_tokens": self.p.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.p.send_reasoning_effort and self.p.reasoning_effort:
            payload["reasoning_effort"] = self.p.reasoning_effort
        # ★ 只支持流式的通道（CodeBuddy）必须显式打开 stream，否则 HTTP 400。
        #   非流式通道**不要**在这里塞 stream:false：有些网关把显式 false
        #   当成"参数不认得"，而缺省本来就已经是非流式。
        if self.p.stream_required:
            payload["stream"] = True

        url = self.p.base_url + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.p.api_key}",
            "Accept": "text/event-stream" if self.p.stream_required
                      else "application/json",
            # ⚠️ 必需：网关前置 Cloudflare 会拦「通用 UA」——
            #    Python-urllib 的默认 UA 直接吃 HTTP 403 + error code 1010。
            #    上游要的是 **non-generic** UA：天枢用 "Tianshu/1.0 (terminal coding agent)"
            #    通过（其源码注释原文："Upstream requires ... a non-generic User-Agent"）。
            #    荣珂用自己的标识，不伪装浏览器。可用 config.json 的 provider.user_agent 覆盖。
            "User-Agent": self.p.user_agent or "Rongke/0.0.1 (research agent)",
        }

        last_err = ""
        t0 = time.time()
        attempts = 0
        for attempt in range(1, max_retries + 1):
            attempts = attempt
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    ctype = str(resp.headers.get("Content-Type") or "")
                    blob = resp.read()
                # ★ 判据是**响应头**，不是嗅探正文。
                #   为什么不能嗅探：上游的 400 正文也是合法 JSON，
                #   而一段真 SSE 正文又是"以 data: 开头的文本"——
                #   两者都能被错的嗅探法判反。Content-Type 是上游给的、
                #   与正文内容无关的**唯一**可靠信号（qiyi 实测结论）。
                if "text/event-stream" in ctype.lower():
                    data = aggregate_sse(blob)
                else:
                    data = json.loads(blob.decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:600]
                last_err = f"HTTP {e.code}: {detail}"
                # 4xx（除限流）是确定性失败，重试无意义
                if e.code not in (408, 409, 429) and e.code < 500:
                    # ★ 但"上下文太长"是确定性失败里**唯一可以自救**的一种：
                    #   主循环收到它会出清一次历史再重试（见 loop 的 ContextOverflow 分支）。
                    #   判据保守（要状态码 + 正文措辞同时命中），不命中就照旧抛 LLMError。
                    if looks_like_overflow(e.code, detail):
                        raise ContextOverflow(
                            f"上下文溢出 —— HTTP {e.code}: {detail}",
                            body=detail, code=e.code) from e
                    # ★ 11101 是"这条通道只吃流式"的**特征码**，与溢出一样属于
                    #   "配置写成非流式"这一类**可以点名**的确定性失败。
                    #   不点名的话它只会显示成一句 HTTP 400，而真实修法是
                    #   把 provider.stream_required 打开 —— 没人能从 400 猜到这件事。
                    if not self.p.stream_required and "11101" in detail:
                        raise LLMError(
                            "该通道只支持流式（HTTP 400 code:11101）—— "
                            "请把 config.json 的 provider.stream_required 置为 true"
                        ) from e
                    raise LLMError(f"LLM 调用失败 —— {last_err}") from e
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 20))
        else:
            raise LLMError(f"LLM 调用失败（重试 {max_retries} 次）—— {last_err}")

        latency_ms = round((time.time() - t0) * 1000, 3)

        try:
            choice = data["choices"][0]
            raw = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"返回格式异常: {json.dumps(data, ensure_ascii=False)[:400]}") from e

        # ── 思维链 ──────────────────────────────────────────────
        # 本机实测：Command Code 网关把推理正文放在 **reasoning**，不是 reasoning_content，
        # 并另给一份结构化副本 reasoning_details：
        #   reasoning         = '我们需要回答中文。用户问：盒子里3红5蓝…'
        #   reasoning_details = [{'type':'reasoning.text','text':'…','format':'unknown','index':0}]
        # 两者都取不到时留空串，界面据此不渲染思维链，而不是渲染一个空壳。
        reasoning = raw.get("reasoning") or raw.get("reasoning_content") or ""
        if not reasoning:
            for d in (raw.get("reasoning_details") or []):
                if isinstance(d, dict) and d.get("text"):
                    reasoning += d["text"]
        reasoning = (reasoning or "").strip()

        # ── 回灌用的干净消息 ────────────────────────────────────
        # reasoning / reasoning_details 是**响应侧**产物。旧实现把整个 msg 原样 append 回
        # messages（loop.py），等于把这两个字段也灌进下一轮请求——未定义行为。
        # 这里只保留 API 认得的三个字段。
        msg: dict = {"role": raw.get("role") or "assistant"}
        if raw.get("content") is not None:
            msg["content"] = raw["content"]
        if raw.get("tool_calls"):
            msg["tool_calls"] = raw["tool_calls"]

        usage = _usage_from(data)

        if meta is not None:
            # `finish_reason` 是**唯一**能区分"模型说完了"和"被 max_tokens 截断"的字段。
            # 不采集它，就永远看不出"答案写到一半被砍掉"这种静默失败。
            meta["finish_reason"] = str(choice.get("finish_reason") or "")
            meta["latency_ms"] = latency_ms
            meta["attempts"] = attempts
            meta["request_model"] = self.p.model
            # 响应侧 model 与请求侧不一致 ⇒ 网关换了后端（或模型被静默升级）。
            # OTel 刻意同时收这两个字段，就是为了让这种漂移可见。
            meta["response_model"] = str(data.get("model") or "")
            meta["cache_ratio"] = usage.cache_ratio

        return msg, usage, reasoning
