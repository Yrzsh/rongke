# -*- coding: utf-8 -*-
"""前缀缓存探针 —— 查清 "cache_read 恒为 0" 是**事实**还是**读数假象**。

为什么要单独做这个：
  2026-09-19 优化靶里写着「cache_read 连续 9 场归档恒为 0 ⇒ 前缀缓存完全没生效」。
  但 `0` 只是一个**读数**。读数恒为 0 只有两种解释，而它们的治法完全相反：
    (A) 网关真的没开前缀缓存  ⇒ 只能等/换网关；
    (B) 网关给了命中数，但**内核没读那个字段名** ⇒ 缓存一直在生效，是我们瞎了。
  把 (B) 当成 (A)，就会去"优化"一个已经好了的东西；把 (A) 当成 (B)，就会去读一个不存在的字段。
  **判别两者要求信号比两者的共同点更细** —— 共同点是"cache_read=0"，那么信号必须来自
  **原始响应体里的字段名**，而不是解析后的值。

`rongke/llm.py::_usage_from` 现状只读 Anthropic 的 `cache_read_input_tokens`；
而本项目跑在 OpenAI 兼容网关 + DeepSeek 模型上，命中数的常见落点是
`prompt_tokens_details.cached_tokens`（OpenAI 标准）或 `prompt_cache_hit_tokens`（DeepSeek 原生）。

用法：
    $PY cache_probe.py --offline   # 只跑字段口径的纯逻辑对照（不联网，秒级）
    $PY cache_probe.py             # 联网：同样前缀连发两次，打印**原始** usage
                                   #   并用被测模块自己的 _usage_from 解析它

退出码：0 全过 / 1 有 FAIL / 2 联网失败（别把"没测"当"通过"）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke.config import Config          # noqa: E402
from rongke.llm import LLM, _usage_from   # noqa: E402  ← 只用它自己的判据，不另写一份

# ── 字段口径对照表：期望的命中数 vs 现状能不能读出来 ──────────────────
# 每项 = (说明, 原始 usage 片段, 期望 cache_read)
CASES: list[tuple[str, dict, int]] = [
    ("Anthropic 口径 cache_read_input_tokens",
     {"input_tokens": 2000, "output_tokens": 10, "cache_read_input_tokens": 1792}, 1792),
    ("OpenAI 标准 prompt_tokens_details.cached_tokens",
     {"prompt_tokens": 2000, "completion_tokens": 10,
      "prompt_tokens_details": {"cached_tokens": 1792}}, 1792),
    ("DeepSeek 原生 prompt_cache_hit_tokens",
     {"prompt_tokens": 2000, "completion_tokens": 10, "prompt_cache_hit_tokens": 1792}, 1792),
    ("顶格 cached_tokens",
     {"prompt_tokens": 2000, "prompt_tokens_details": {"cached_tokens": 2000}}, 2000),
    # 负控：压根没有缓存字段 ⇒ 必须读成 0，**不许瞎猜**
    ("无缓存字段（负控：必须读 0）", {"prompt_tokens": 2000, "completion_tokens": 10}, 0),
]


def offline() -> int:
    """纯逻辑对照 —— 全部走被测模块自己的 `_usage_from`。"""
    print("── 字段口径对照（调用 rongke.llm._usage_from，不联网）──")
    fails = 0
    for name, raw, want in CASES:
        got = _usage_from({"usage": raw}).cache_read
        ok = got == want
        mark = "✔" if ok else "✗"
        print(f"  {mark} {name:<48} 期望 {want:<6} 得到 {got}")
        if not ok:
            fails += 1
    # 一句判词：谁在承重
    have_anthropic = _usage_from({"usage": CASES[0][1]}).cache_read == 1792
    miss_openai = _usage_from({"usage": CASES[1][1]}).cache_read != 1792
    miss_deepseek = _usage_from({"usage": CASES[2][1]}).cache_read != 1792
    if have_anthropic and (miss_openai or miss_deepseek):
        print("\n  ⇒ 解析器**只认 Anthropic 的字段名**，OpenAI/DeepSeek 口径的命中数一律丢成 0。")
        print("     这不是「网关没开缓存」，而是「我们没读那个字段」—— 两者治法相反。")
    elif not have_anthropic:
        print("\n  ⇒ 连 Anthropic 口径都读不出来：解析器整体失效（比「缺一个分支」更严重）。")
    print(f"\n离线对照：{'全过' if not fails else f'{fails} 项 FAIL'}")
    return 1 if fails else 0


def _post(llm: LLM, messages: list[dict], tools: list[dict] | None) -> dict:
    """最小 HTTP 直发，**只要原始响应体**（LLM.chat 会把 usage 解析掉，我们要看原文）。

    这不是"照着被测模块再写一遍判据"—— 判据仍然是 `_usage_from`（下面用它解析）。
    这里重复的只是**取字节**这一层，因为模块刻意不暴露原始响应。
    """
    p = llm.p
    payload: dict = {"model": p.model, "messages": messages, "temperature": 0,
                     "max_tokens": 64}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if p.send_reasoning_effort and p.reasoning_effort:
        payload["reasoning_effort"] = p.reasoning_effort
    req = urllib.request.Request(
        p.base_url + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {p.api_key}",
                 "Accept": "application/json",
                 "User-Agent": p.user_agent or "Rongke/0.0.1 (research agent)"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _long_prefix(n_chars: int = 9000) -> str:
    """造一段够长的稳定前缀（前缀缓存通常要 ≥1024 token 才触发）。"""
    unit = ("前缀缓存只对**稳定的前缀**生效。荣珂每步都把 SYSTEM 与工具 schema 原样重发，"
            "这是最值钱也最稳定的缓存对象。判定它有没有生效，看的不是解析后的比例，"
            "而是**原始响应体里到底有没有那个字段**。")
    return (unit * (n_chars // len(unit) + 1))[:n_chars]


def live() -> int:
    cfg = Config.load()
    llm = LLM(cfg.provider)
    tools = [{"type": "function", "function": {
        "name": "noop", "description": "占位工具，只为让 tools 出现在前缀里。",
        "parameters": {"type": "object", "properties": {}}}}]
    sysmsg = {"role": "system", "content": _long_prefix()}
    msgs = [sysmsg, {"role": "user", "content": "回答一个字：好"}]

    print("── 联网：同样前缀连发两次 ──")
    raws = []
    for i in (1, 2):
        try:
            d = _post(llm, msgs, tools)
        except urllib.error.HTTPError as e:
            print(f"  第 {i} 次 HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
            return 2
        except Exception as e:
            print(f"  第 {i} 次失败：{type(e).__name__}: {e}")
            return 2
        raw = d.get("usage") or {}
        raws.append(raw)
        print(f"  第 {i} 次 usage 原文（**全字段**）：")
        print("    " + json.dumps(raw, ensure_ascii=False))
        print(f"    平台说这是哪个模型：{d.get('model')!r}")
        print(f"    被测模块 _usage_from 读出来的 cache_read = "
              f"{_usage_from({'usage': raw}).cache_read}")

    # ── 判词 ──
    flat = json.dumps(raws, ensure_ascii=False).lower()
    hit_names = [k for k in ("cached_tokens", "cache_read_input_tokens",
                             "prompt_cache_hit_tokens", "cache_hit_tokens")
                 if k in flat]
    parsed_hits = [_usage_from({"usage": r}).cache_read for r in raws]
    print("\n── 判词 ──")
    print(f"  原始体里出现的缓存字段：{hit_names or '（一个都没有）'}")
    print(f"  被测模块读出的 cache_read：{parsed_hits}")
    if any(parsed_hits):
        print("  ⇒ (B) **缓存生效，读数曾经是瞎的** —— 不是网关切了，是解析器漏了字段名。")
        return 0
    if hit_names:
        print("  ⇒ (B) **缓存生效，但解析器没读这个字段名** —— 证据：原始体里有 "
              f"{hit_names}，模块读出 0。")
        print("     修法：给 _usage_from 补分支，并加一条离线对照钉住（本脚本 --offline）。")
        return 1
    print("  ⇒ (A) 原始体里根本没有缓存字段。要么网关没开，要么它按 OpenAI 口径"
          "把命中数折进了 prompt_tokens（那样就该看 prompt_tokens 的绝对量）。")
    print("     代价如实说：**这一次探测不足以区分「网关没缓存」与「网关缓存了但不报」。**")
    print("     要分开，得看第二次的 prompt_tokens 是否明显低于第一次 —— 见下面的数。")
    if len(raws) == 2:
        a = raws[0].get("prompt_tokens") or 0
        b = raws[1].get("prompt_tokens") or 0
        print(f"     prompt_tokens：第一次 {a} / 第二次 {b}"
              f"{'（第二次更少 ⇒ 缓存有生效但未上报）' if b and b < a else '（没变化）'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="前缀缓存探针：区分「网关没缓存」与「我们没读那个字段」")
    ap.add_argument("--offline", action="store_true", help="只跑字段口径的纯逻辑对照")
    a = ap.parse_args(argv)
    rc = offline()
    if a.offline:
        return rc
    print()
    return live()


if __name__ == "__main__":
    sys.exit(main())
