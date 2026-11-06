"""HTTP 请求体的单一入口。只依赖标准库，响应语义由 Handler 决定。"""
from __future__ import annotations

import json
import re

_LENGTH = re.compile(r"[0-9]+\Z")


class RequestError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def read_json_object(handler, max_bytes: int = 1024 * 1024) -> dict:
    """读取一个 JSON 对象；失败也已按声明长度 drain，保护 keep-alive。"""
    media = (handler.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise RequestError(415, "请求必须使用 application/json")
    raw_n = (handler.headers.get("Content-Length") or "").strip()
    if not _LENGTH.fullmatch(raw_n):
        raise RequestError(400, "Content-Length 必须是非负十进制整数")
    n = int(raw_n)
    if n > max_bytes:
        raise RequestError(413, "JSON 请求体超过大小上限")
    if n == 0:
        raise RequestError(400, "请求体不能为空")
    raw = handler.rfile.read(n)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError(400, "请求体不是合法 JSON") from None
    if not isinstance(value, dict):
        raise RequestError(400, "请求体必须是 JSON 对象")
    return value
