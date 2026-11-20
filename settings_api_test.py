# -*- coding: utf-8 -*-
"""设置页添加/切换 API 的契约与兼容性回归。

覆盖用户"添加了 API 用不了"的几条真实卡点：
  1. `send_reasoning_effort` 不在 settings 校验器里被静默丢弃（白名单/字段目录/解析三处对齐）。
  2. `base_url` 误粘贴末尾 `/chat/completions` 时自动规范化，不拼成 `/chat/completions/chat/completions`。
  3. `save_provider_overrides` 在 `data_root()` 尚未建目录时先 `mkdir` 再写。
  4. `LLM.chat` 在 `send_reasoning_effort=False` 时不发送 `reasoning_effort`；
     在 `send_reasoning_effort=True` 且上游回 400 拒收 `reasoning_effort` 时给出可操作提示。
  5. `Manager.secret_save` 正确从 `(msg, usage, reasoning)` 三元组里取回显文本，
     `settings_view` 的项目级落点指向 `data_root()/config.json`。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke.config import Config, Provider, save_provider_overrides  # noqa: E402
from rongke.llm import LLM, LLMError  # noqa: E402
from rongke.web.server import Manager  # noqa: E402
from rongke.web.settings import (  # noqa: E402
    SettingsError,
    parse_provider_updates,
    provider_field_specs,
)

FAILS: list[str] = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print(f"[PASS] {name}")
    else:
        FAILS.append(f"{name}: {detail}")
        print(f"[FAIL] {name} :: {detail}")


# 1. settings.py 字段目录与解析
specs = {f["key"]: f for f in provider_field_specs()}
check("settings.specs-has-send_reasoning_effort",
      specs.get("send_reasoning_effort") == {"key": "send_reasoning_effort", "kind": "bool"},
      repr(specs.get("send_reasoning_effort")))

parsed = parse_provider_updates({
    "provider.model": "gpt-4o",
    "provider.base_url": "https://api.openai.com/v1/chat/completions/",
    "provider.send_reasoning_effort": "false",
    "provider.stream_required": False,
    "provider.api_key_env": "OPENAI_API_KEY",
    "provider.max_tokens": "4096",
})
check("settings.parse-keeps-send_reasoning_effort-false",
      parsed.get("send_reasoning_effort") is False, repr(parsed))
check("settings.parse-strips-chat-completions",
      parsed.get("base_url") == "https://api.openai.com/v1", repr(parsed.get("base_url")))

try:
    parse_provider_updates({"provider.send_reasoning_effort": "maybe"})
    check("settings.rejects-bad-send_reasoning_effort", False, "no exception")
except SettingsError as e:
    check("settings.rejects-bad-send_reasoning_effort",
          "send_reasoning_effort" in str(e), str(e))


# 2. config.py 目录创建与 URL 规范化
with tempfile.TemporaryDirectory() as td:
    td_path = Path(td)
    home = td_path / "home"
    home.mkdir()
    fresh_data = td_path / "fresh_data"
    save_provider_overrides({
        "base_url": "https://api.deepseek.com/chat/completions/",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "send_reasoning_effort": False,
    }, root=fresh_data)
    check("config.save-creates-parent-dir", (fresh_data / "config.json").is_file())
    with mock.patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}):
        cfg = Config.load(root=fresh_data)
    check("config.load-normalizes-base_url",
          cfg.provider.base_url == "https://api.deepseek.com", cfg.provider.base_url)
    check("config.load-reads-send_reasoning_effort-false",
          cfg.provider.send_reasoning_effort is False, repr(cfg.provider.send_reasoning_effort))


# 3. llm.py 发送行为与 400 提示
captured: dict = {}


class _FakeResp:
    def __init__(self, body: dict):
        self.headers = {"Content-Type": "application/json"}
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=0):
    captured["url"] = req.full_url
    captured["body"] = json.loads(req.data.decode("utf-8"))
    return _FakeResp({
        "id": "1",
        "model": "deepseek-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "可用"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    })


with mock.patch.dict(os.environ, {"TEST_DS_KEY": "sk-test-123"}), \
     mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
    p = Provider(name="custom", base_url="https://api.deepseek.com/v1/chat/completions",
                 model="deepseek-chat", api_key_env="TEST_DS_KEY",
                 reasoning_effort="medium", send_reasoning_effort=False,
                 max_tokens=4096, stream_required=False)
    msg, _, _ = LLM(p).chat([{"role": "user", "content": "hi"}])
    check("llm.url-not-doubled",
          captured["url"] == "https://api.deepseek.com/v1/chat/completions", captured["url"])
    check("llm.reasoning_effort-omitted-when-disabled",
          "reasoning_effort" not in captured["body"], repr(captured["body"]))
    check("llm.returns-clean-content", msg.get("content") == "可用", repr(msg))


def _fake_400_reasoning(req, timeout=0):
    err_body = json.dumps(
        {"error": {"message": "Unsupported parameter: 'reasoning_effort'"}}
    ).encode()
    raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(err_body))


with mock.patch.dict(os.environ, {"TEST_OAI_KEY": "sk-test-123"}), \
     mock.patch("urllib.request.urlopen", side_effect=_fake_400_reasoning):
    p2 = Provider(name="custom", base_url="https://api.openai.com/v1",
                  model="gpt-4o", api_key_env="TEST_OAI_KEY",
                  reasoning_effort="medium", send_reasoning_effort=True,
                  max_tokens=4096, stream_required=False)
    try:
        LLM(p2).chat([{"role": "user", "content": "hi"}])
        check("llm.400-reasoning_effort-actionable-hint", False, "no exception")
    except LLMError as e:
        check("llm.400-reasoning_effort-actionable-hint",
              "send_reasoning_effort" in str(e) and "设置" in str(e), str(e))


# 4. server.Manager: settings_view / settings_save / secret_save
with tempfile.TemporaryDirectory() as td:
    td_path = Path(td)
    home = td_path / "home"
    data = td_path / "data"
    home.mkdir()
    data.mkdir()
    with mock.patch.dict(os.environ, {
        "USERPROFILE": str(home), "HOME": str(home), "RONGKE_DATA": str(data),
    }), mock.patch("rongke.web.server.Rongke",
                   return_value=mock.MagicMock(cfg=Config.load(root=data))):
        mgr = Manager()
        sv = mgr.settings_view()
        paths = [f["path"] for f in sv["config_files"]]
        check("server.settings_view-uses-data_root",
              str(data / "config.json") in paths, repr(paths))

        res, err = mgr.settings_save({
            "provider.model": "gpt-4o-mini",
            "provider.base_url": "https://api.openai.com/v1/chat/completions",
            "provider.api_key_env": "MY_TEST_KEY",
            "provider.send_reasoning_effort": False,
        })
        check("server.settings_save-persists-send_reasoning_effort",
              err == "" and res is not None
              and res["written"].get("send_reasoning_effort") is False,
              f"err={err} res={res}")

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            sres, serr = mgr.secret_save({"name": "MY_TEST_KEY", "value": "sk-abc123456"})
        check("server.secret_save-extracts-verify_sample",
              serr == "" and sres is not None and sres.get("verified") is True
              and sres.get("verify_sample") == "可用",
              f"serr={serr} sres={sres}")


if FAILS:
    print(f"\nFAILED {len(FAILS)}/{N}")
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print(f"\nOK -- {N}/{N} passed")
