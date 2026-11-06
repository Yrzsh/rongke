# -*- coding: utf-8 -*-
"""magpie 通道验收。

判据（每条都能变红）：
  ① target() 造出的 base_url 是 http://host:3425/v1（不叠 /v1/v1）
  ② 端口缺省时补 3425，不是落到 80
  ③ overrides() 里**没有任何密钥值**，只有变量名
  ④ probe() 连不上时如实说连不上（负控：没人监听 ⇒ False）
  ⑤ probe() 面对一个真在听的服务 ⇒ True
  ⑥ 真发一次 OpenAI /chat/completions，荣珂能拿到回复（端到端）

⑤/⑥ 用一个本地假网关，它只实现 /v1/models 与 /v1/chat/completions，
拒绝一切其他路径 —— 这样"通了"不能是别的原因碰巧通了。
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from rongke import llm
from rongke.config import Provider
from rongke.magpie import DEFAULT_PORT, MagpieError, probe, target

FAILS: list[str] = []


def ok(m):
    print("[ok]  ", m)


def bad(m):
    FAILS.append(m)
    print("[FAIL]", m)


print("═══ 1. 造目标 ═══")
t = target("deepseek/deepseek-v4-pro")
ok(f"默认地址 → {t.base_url()}")
if t.base_url() == f"http://127.0.0.1:{DEFAULT_PORT}/v1":
    ok("默认是 loopback + 3425 + /v1")
else:
    bad(f"默认地址不对：{t.base_url()}")

t2 = target("x/y", "http://192.168.1.9:3425")
if t2.base_url() == "http://192.168.1.9:3425/v1":
    ok("Docker 形态：指定主机原样保留")
else:
    bad(f"Docker 形态地址错：{t2.base_url()}")

t3 = target("x/y", "http://nas.local")
if t3.base_url() == f"http://nas.local:{DEFAULT_PORT}/v1":
    ok(f"不给端口时补 {DEFAULT_PORT}，不落到 80")
else:
    bad(f"缺省端口没补对：{t3.base_url()}")

t4 = target("x/y", "http://h:3425/v1")
if t4.base_url() == "http://h:3425/v1":
    ok("用户已带 /v1 时不叠成 /v1/v1")
else:
    bad(f"叠了 /v1：{t4.base_url()}")

print()
print("═══ 2. 不落密钥 ═══")
ov = t.overrides()
blob = json.dumps(ov, ensure_ascii=False)
if "api_key" not in ov:
    ok("overrides 里没有 api_key 这个键")
else:
    bad("overrides 里出现了 api_key")
if ov.get("api_key_env") == "MAGPIE_API_KEY":
    ok("给的是环境变量**名**，不是值")
else:
    bad(f"api_key_env 不对：{ov.get('api_key_env')}")
if "sk-" not in blob and len(ov.get("api_key_env", "")) < 40:
    ok("没有任何像密钥的字面量")
else:
    bad("可能把密钥写进了配置")

# ★ 这条是实测踩出来的：第一版 overrides() 多带了 name / user_agent，
#   而 save_provider_overrides 的第 1 条硬规矩是"白名单外一律 ValueError"。
#   也就是切到 magpie 这一步会整体失败 —— 而单元测试不会碰写盘，抓不到。
from rongke.config import PROVIDER_WRITE_KEYS  # noqa: E402

outside = [k for k in ov if k not in PROVIDER_WRITE_KEYS]
if not outside:
    ok(f"overrides 的键全在写盘白名单内（{len(ov)} 个）")
else:
    bad(f"★ overrides 含白名单外的键 {outside} —— 写配置会整体 ValueError")

print()
print("═══ 3. 参数校验（错的值必须报错，不能静默）═══")
for bad_input, why in ((("x/y", "ftp://h:1"), "非 http 协议"),
                       (("", ""), "空模型名"),
                       (("x/y", "http://"), "缺主机名")):
    model, ep = bad_input
    try:
        if not ep:
            target(model, f"http://127.0.0.1:{DEFAULT_PORT}")
        target(model, ep)
        bad(f"{why} 居然没报错")
    except MagpieError as e:
        ok(f"{why} → 报错：{str(e)[:44]}")

print()
print("═══ 4. probe：没人监听时必须说连不上 ═══")
alive, msg = probe(f"http://127.0.0.1:1")
if alive is False:
    ok(f"负控成立：{msg[:70]}")
else:
    bad(f"★ 没服务却说在线：{msg}")


# ── 假 magpie 网关 ───────────────────────────────────────────────
class FakeMagpie(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [
                {"id": "deepseek/deepseek-v4-pro", "object": "model"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        if not str(self.headers.get("Authorization") or "").startswith("Bearer "):
            self._json(401, {"error": "no key"})
            return
        self._json(200, {
            "id": "chatcmpl-fake", "object": "chat.completion",
            "created": 1, "model": body.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "网关回话成功"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                      "total_tokens": 8},
        })

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


srv = HTTPServer(("127.0.0.1", 0), FakeMagpie)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

print()
print("═══ 5. probe：真在听 ⇒ 在线 ═══")
alive, msg = probe(f"http://127.0.0.1:{port}")
if alive is True:
    ok(f"正控成立：{msg[:70]}")
else:
    bad(f"★ 有服务却说连不上：{msg}")

print()
print("═══ 6. 端到端：荣珂真发一次 completion ═══")
import os

# Provider.api_key 从环境变量/密钥文件读；网关接受任意非空值。
os.environ["MAGPIE_API_KEY"] = "magpie"
ov2 = target("deepseek/deepseek-v4-pro",
             f"http://127.0.0.1:{port}").overrides()
prov = Provider(
    name="magpie", base_url=str(ov2["base_url"]),
    model=str(ov2["model"]), api_key_env=str(ov2["api_key_env"]),
    reasoning_effort="medium",
    send_reasoning_effort=bool(ov2["send_reasoning_effort"]),
    max_tokens=256, user_agent="",
    stream_required=bool(ov2["stream_required"]))

state = prov.diagnose()
if state == "ok":
    ok(f"provider 诊断通过（{prov.base_url} / {prov.model}）")
else:
    bad(f"★ provider 诊断不过：{state}")

client = llm.LLM(prov)
try:
    msg, usage, reasoning = client.chat(
        [{"role": "user", "content": "你好"}], max_retries=1)
    text = str((msg or {}).get("content") or "")
    if "网关回话成功" in text:
        ok(f"荣珂经网关拿到回复：{text[:40]}")
    else:
        bad(f"★ 回复内容不对：{text[:80]}")
    if usage and getattr(usage, "total_tokens", None) == 8:
        ok("用量回填正确（total_tokens=8）")
    else:
        ok(f"用量回填：{usage}")
except Exception as e:                                        # noqa: BLE001
    bad(f"★ 端到端调用失败：{type(e).__name__}: {e}")
finally:
    srv.shutdown()

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项失败")
    sys.exit(1)
print("✔ magpie 通道验收通过")
