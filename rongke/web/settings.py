# -*- coding: utf-8 -*-
"""设置页的**字段契约**：哪些键能写、每个键长什么样、什么值算合法。

为什么单独一个模块，而不是继续留在 `server.py` 的 `settings_save` 里：
    设置页的「读」与「写」必须共用同一份字段表。读的那一侧要给出
    reasoning_choices / write_whitelist，写的那一侧要逐键校验；两边各写一份
    就是 H 型（一处改了、另一处没改）—— 例如白名单加了 `stream_required`
    而校验里没有它，接口就会把一个字符串原样写进配置。

这里只放**值语义**（类型、范围、白名单），不放文案、不放 HTML：
标签和说明归前端，这一层不替界面说话。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..config import PROVIDER_WRITE_KEYS, provider_write_key

REASONING_CHOICES: tuple[str, ...] = ("low", "medium", "high")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SettingsError(ValueError):
    """给用户看的设置校验失败。消息原样端到界面，不再加工。"""


def provider_field_specs() -> list[dict[str, Any]]:
    """写给前端看的字段目录（只描述能写什么，不含任何值）。"""
    return [
        {"key": "model", "kind": "text"},
        {"key": "base_url", "kind": "url"},
        {"key": "reasoning_effort", "kind": "choice",
         "choices": list(REASONING_CHOICES)},
        {"key": "api_key_env", "kind": "env"},
        {"key": "max_tokens", "kind": "int"},
        {"key": "stream_required", "kind": "bool"},
    ]


def _flatten(body: dict) -> dict[str, Any]:
    """把 `{"provider": {...}}` / `{"provider.model": ...}` / `{"model": ...}` 收平。"""
    src: dict[str, Any] = {}
    for k, v in body.items():
        if k == "provider" and isinstance(v, dict):
            for k2, v2 in v.items():
                src[str(k2)] = v2
        else:
            src[str(k)] = v
    return src


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    raise ValueError(f"stream_required 只接受 true/false（收到 {v!r}）")


def parse_provider_updates(body: Any) -> dict[str, Any]:
    """校验并归一化一次设置写入。合法 ⇒ 干净的裸键字典；不合法 ⇒ SettingsError。

    白名单外的键一律拒绝，不做静默忽略 —— 静默忽略会让调用方以为改成功了。
    """
    if not isinstance(body, dict):
        raise SettingsError("请求体不是对象")
    src = _flatten(body)

    rejected: list[str] = []
    clean: dict[str, Any] = {}
    errs: list[str] = []
    for k, v in src.items():
        bare = provider_write_key(k)
        if not bare:
            rejected.append(k)
            continue
        if bare == "model":
            s = str(v or "").strip()
            if not s:
                errs.append("model 不能为空")
            elif len(s) > 200 or any(c.isspace() for c in s):
                errs.append("model 不合法（不能含空白，不超过 200 字）")
            else:
                clean[bare] = s
        elif bare == "base_url":
            s = str(v or "").strip()
            if not s:
                errs.append("base_url 不能为空")
            elif not s.startswith(("http://", "https://")):
                errs.append(f"base_url 必须以 http:// 或 https:// 开头（收到 {s!r}）")
            elif not urllib.parse.urlparse(s).netloc:
                errs.append(f"base_url 缺主机名（收到 {s!r}）")
            else:
                clean[bare] = s
        elif bare == "reasoning_effort":
            s = str(v or "").strip()
            if not s:
                errs.append("reasoning_effort 不能为空（要么删掉这个字段，"
                            "要么填 low / medium / high）")
            elif s not in REASONING_CHOICES:
                errs.append("reasoning_effort 只接受 "
                            + " / ".join(REASONING_CHOICES) + f"（收到 {s!r}）")
            else:
                clean[bare] = s
        elif bare == "api_key_env":
            s = str(v or "").strip()
            # 只认合法环境变量名。这条同时挡住"把密钥值填进来"——
            # 密钥里没有这种形状，含 `.` `-` 空格的一律进不去。
            if not ENV_NAME_RE.match(s):
                errs.append("api_key_env 要的是一个**环境变量名**"
                            "（字母/数字/下划线，以字母或下划线开头）。"
                            "荣珂不存密钥值，只存变量名。")
            else:
                clean[bare] = s
        elif bare == "max_tokens":
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                errs.append("max_tokens 必须是正整数")
                continue
            try:
                n = int(str(v).strip())
            except Exception:                           # noqa: BLE001
                n = -1
            if n <= 0:
                errs.append(f"max_tokens 必须是正整数（收到 {v!r}）")
            else:
                clean[bare] = n
        elif bare == "stream_required":
            try:
                clean[bare] = _as_bool(v)
            except ValueError as e:
                errs.append(str(e))

    if rejected:
        raise SettingsError(
            "不被接受的键：" + "、".join(rejected)
            + "。这里只收 " + " / ".join("provider." + x for x in PROVIDER_WRITE_KEYS)
            + " —— 其它配置段请手改 config.json（那里有内核不认识的"
              "注释字段，程序不该去动）。")
    if errs:
        raise SettingsError("字段校验未通过，配置未改动：" + "；".join(errs))
    if not clean:
        raise SettingsError("没有要写入的字段（白名单一个都没填）")
    return clean
