# -*- coding: utf-8 -*-
"""SSE aggregation layer regression (W27-A).

Criteria are written around REAL failures, not around "what the function returns":

  A. Aggregation correctness -- content/reasoning kept apart, tool_calls
     concatenated by index, finish_reason = last non-empty, usage = last non-empty.
  B. NEGATIVE CONTROL: switch concatenation to overwrite => the tool_calls
     criteria in A must break on the spot (proves they carry weight).
  C. Judgement is on the response HEADER, not by sniffing the body: the same
     SSE payload with an `application/json` header must NOT be aggregated.
  D. Error code 11101 is named into an actionable message.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # 本文件住在仓库根
sys.path.insert(0, str(ROOT))

from rongke.llm import aggregate_sse, LLM, LLMError  # noqa: E402
from rongke.config import (Provider, DEFAULTS,       # noqa: E402
                           PROVIDER_WRITE_KEYS, provider_write_key)

FAIL = []
N = 0


def ck(name, cond, detail=""):
    """One criterion. Prints a line the regress runner can COUNT.

    它认行首的 `[PASS]` / `[FAIL]` / `✔`（见 `regress.py:256` 的 `_RE_MARK`）。
    不按那个形状打，本套会被报成「空跑?」—— rc=0 却 0 项断言，
    也就是"跑了、没报错、但一条判据都没扫到"这种最坏的绿。
    """
    global N
    N += 1
    if cond:
        print(f"[PASS] {name}")
    else:
        FAIL.append(f"{name} {detail}")
        print(f"[FAIL] {name} {detail}")


def sse(*chunks, done=True):
    """Build an SSE payload. Each chunk is a delta-dict (with optional
    `_finish`) or a raw string line."""
    lines = []
    for c in chunks:
        if isinstance(c, str):
            lines.append(c)
            continue
        c = dict(c)
        fin = c.pop("_finish", None)
        lines.append("data: " + json.dumps(
            {"id": "x", "model": "deepseek-v4.1-flash",
             "object": "chat.completion.chunk",
             "choices": [{"index": 0, "delta": c, "finish_reason": fin}]},
            ensure_ascii=False))
    if done:
        lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


print("=" * 72)
print("[A] aggregation correctness")

# A1: content concatenated; reasoning kept SEPARATE
raw = sse(
    {"role": "assistant", "content": "", "reasoning_content": "think-A"},
    {"content": "pro", "reasoning_content": "think-B"},
    {"content": "toplast", "reasoning_content": ""},
    {"content": " fusion.", "reasoning_content": ""},
)
d = aggregate_sse(raw)
m = d["choices"][0]["message"]
ck("A1.content", m["content"] == "protoplast fusion.", repr(m.get("content")))
ck("A1.reasoning", m["reasoning"] == "think-Athink-B", repr(m.get("reasoning")))
# LOAD-BEARING: reasoning must never leak into the answer body
ck("A1.no-leak", "think-A" not in (m.get("content") or ""))
print(f"  content={m['content']!r} reasoning={m['reasoning']!r}")

# A2: tool_calls arrive in fragments -- the most easily broken criterion
raw = sse(
    {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "call_00_x", "type": "function",
         "function": {"name": "kb_search", "arguments": ""}}]},
    {"tool_calls": [{"index": 0, "function": {"arguments": '{"query":'}}]},
    {"tool_calls": [{"index": 0, "function": {"arguments": ' "protoplast"}'}}]},
    {"_finish": "tool_calls"},
)
d = aggregate_sse(raw)
tcs = d["choices"][0]["message"]["tool_calls"]
ck("A2.n_tools", len(tcs) == 1, str(len(tcs)))
ck("A2.name", tcs[0]["function"]["name"] == "kb_search")
ck("A2.args", tcs[0]["function"]["arguments"] == '{"query": "protoplast"}',
   repr(tcs[0]["function"]["arguments"]))
ck("A2.id", tcs[0]["id"] == "call_00_x")
ck("A2.finish", d["choices"][0]["finish_reason"] == "tool_calls",
   repr(d["choices"][0]["finish_reason"]))
print(f"  tool args={tcs[0]['function']['arguments']!r} "
      f"finish={d['choices'][0]['finish_reason']!r}")

# A3: parallel tool calls -- bucket by index, must not cross-contaminate
raw = sse(
    {"tool_calls": [
        {"index": 0, "id": "c0", "function": {"name": "kb_search", "arguments": '{"q"'}},
        {"index": 1, "id": "c1", "function": {"name": "kb_read", "arguments": '{"d"'}}]},
    {"tool_calls": [{"index": 1, "function": {"arguments": ':1}'}},
                    {"index": 0, "function": {"arguments": ':2}'}}]},
)
tcs = aggregate_sse(raw)["choices"][0]["message"]["tool_calls"]
ck("A3.n", len(tcs) == 2, str(len(tcs)))
ck("A3.0", tcs[0]["function"]["arguments"] == '{"q":2}',
   repr(tcs[0]["function"]["arguments"]))
ck("A3.1", tcs[1]["function"]["arguments"] == '{"d":1}',
   repr(tcs[1]["function"]["arguments"]))
ck("A3.name0", tcs[0]["function"]["name"] == "kb_search")
ck("A3.name1", tcs[1]["function"]["name"] == "kb_read")

# A4: usage = last non-empty; finish_reason = last non-empty
raw = "".join([
    "data: " + json.dumps({"model": "m",
                           "usage": {"prompt_tokens": 5, "completion_tokens": 1},
                           "choices": [{"index": 0, "delta": {"content": "a"},
                                        "finish_reason": None}]}) + "\n\n",
    "data: " + json.dumps({"usage": {"prompt_tokens": 5, "completion_tokens": 9,
                                     "prompt_tokens_details": {"cached_tokens": 4}},
                           "choices": [{"index": 0, "delta": {},
                                        "finish_reason": ""}]}) + "\n\n",
    "data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                        "finish_reason": "stop"}]}) + "\n\n",
    "data: [DONE]\n\n",
]).encode("utf-8")
d = aggregate_sse(raw)
ck("A4.usage_last", d["usage"]["completion_tokens"] == 9, str(d["usage"]))
ck("A4.finish_last", d["choices"][0]["finish_reason"] == "stop",
   repr(d["choices"][0]["finish_reason"]))

# A5: keep-alive / comment / event: / malformed lines are skipped, never raised
raw = (b": keep-alive\n\n"
       b"event: ping\n\n"
       b"data: {not json at all\n\n"
       b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "ok"},
                                            "finish_reason": None}]}).encode() + b"\n\n"
       b"data: [DONE]\n\n")
d = aggregate_sse(raw)
ck("A5.survives", d["choices"][0]["message"]["content"] == "ok",
   repr(d["choices"][0]["message"].get("content")))

print("=" * 72)
print("[B] NEGATIVE CONTROL: concatenation -> overwrite must break A2")


def aggregate_sse_overwrite(raw: bytes) -> dict:
    """Fault injection: `arguments` overwritten instead of appended."""
    src = open(ROOT / "rongke" / "llm.py", encoding="utf-8").read()
    needle = 'e["function"]["arguments"] += str(fn["arguments"])'
    assert needle in src, "injection anchor not found -- test is not load-bearing"
    src = src.replace(needle, 'e["function"]["arguments"] = str(fn["arguments"])')
    body = src[src.index("def _sse_chunks"):src.index("class LLM:")]
    ns: dict = {}
    exec("import json\n" + body, ns)   # noqa: S102
    return ns["aggregate_sse"](raw)


raw = sse(
    {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "call_00_x", "type": "function",
         "function": {"name": "kb_search", "arguments": ""}}]},
    {"tool_calls": [{"index": 0, "function": {"arguments": '{"query":'}}]},
    {"tool_calls": [{"index": 0, "function": {"arguments": ' "protoplast"}'}}]},
)
bad = aggregate_sse_overwrite(raw)["choices"][0]["message"]["tool_calls"][0]
ck("B.negctl-breaks", bad["function"]["arguments"] != '{"query": "protoplast"}',
   repr(bad["function"]["arguments"]))
print(f"  after injection args={bad['function']['arguments']!r}  <- must be incomplete")

print("=" * 72)
print("[C] judgement is on the response HEADER, not body sniffing")
src_llm = open(ROOT / "rongke" / "llm.py", encoding="utf-8").read()
ck("C.header-judged", "ctype.lower()" in src_llm and "text/event-stream" in src_llm)
ck("C.no-sniff", 'startswith(b"data:"' not in src_llm,
   "body sniffing appeared -- criterion must stay header-based")

print("=" * 72)
print("[D] 11101 named into an actionable message")
p = Provider(name="codebuddy", base_url="https://copilot.tencent.com/v2",
             model="deepseek-v4.1-flash", api_key_env="CODEBUDDY_API_KEY",
             reasoning_effort="medium", send_reasoning_effort=True,
             max_tokens=256, stream_required=False)
ck("D.stream-default-false", p.stream_required is False)
# Default MUST be False -- flipping it would send stream:true to every legacy channel
ck("D.defaults-false", DEFAULTS["provider"]["stream_required"] is False,
   str(DEFAULTS["provider"].get("stream_required")))
# Must be writable, or the settings page cannot switch channels
ck("D.writable", "stream_required" in PROVIDER_WRITE_KEYS, str(PROVIDER_WRITE_KEYS))
# ...but the whitelist must stay tight (not widened into "anything goes")
ck("D.whitelist-tight", provider_write_key("api_key") == ""
   and provider_write_key("stream_required") == "stream_required")
# The 11101 branch must exist and mention the fix
ck("D.11101-named", '11101' in src_llm and 'stream_required' in src_llm)

print("=" * 72)
if FAIL:
    print(f"FAILED {len(FAIL)}/{N}")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print(f"OK -- {N}/{N} passed")
print("[DONE] llm_sse")
