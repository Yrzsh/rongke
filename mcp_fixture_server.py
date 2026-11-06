# -*- coding: utf-8 -*-
"""mcp_fixture_server.py — 一个**故意会坏**的 stdio MCP 服务端，专供测试。

为什么要有它：
  验证"报错能不能被看见"的东西，必须先有一个**能确定地坏**的对手。
  拿真 Origin 当测试对手是不行的 —— 它是好的，坏不了，测不出"坏了会不会被发现"。
  （这就是"负控"：光看到好结果，证明不了检查项在坏结果上会响。）

只用 stdlib，故意不 import mcp —— 这样它在两个解释器下都能跑，
测试不必挑解释器。

用环境变量 FIXTURE_MODE 操控它的坏法：

  ok        一切正常
  junk      每次回应**前**往 stdout 打两行非 JSON（横幅/日志），含 initialize
  hang      echo 永不回应（初始化与 tools/list 仍正常，否则连不上没法测）
  crash     echo 触发 os._exit(3)
  iserror   echo 回 isError=true
  notools   tools/list 回空数组

另有三个**始终稳定**的工具（不受 mode 影响），用来测载荷层：
  echo       回显 text
  fail_soft  回 {"ok": false, "error": ...} —— 协议层成功、载荷层失败
  write_note 把文本写到 out 指定的路径，回 {"ok": true, "file": 绝对路径}
  write_default  写到**本进程 cwd** 的固定文件名（不接路径参数）——
                 用来验证"子进程 cwd 有没有钉在工作区"，路径解析救不了这条
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

MODE = (os.environ.get("FIXTURE_MODE") or "ok").strip().lower()
PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "echo",
        "description": "回显 text。用于验证标准调用链路。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
    },
    {
        "name": "fail_soft",
        "description": "永远返回载荷级失败（{\"ok\": false}），但协议层成功。",
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "write_note",
        "description": "把 text 写到 out 指向的文件，返回绝对路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "out": {"type": "string", "description": "输出文件路径"},
                "text": {"type": "string"},
            },
            "required": ["out"],
        },
    },
    {
        "name": "write_default",
        "description": ("把固定文件名 default_note.txt 写到**服务端自己的 cwd**，"
                        "不接任何路径参数。专门用来验证'子进程 cwd 有没有被钉在工作区'"
                        "—— 路径解析那一层救不了它，因为没有路径参数可解析。"),
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": [],
        },
    },
]


def _flush_write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _reply(mid, result) -> None:
    if MODE == "junk":
        # 横幅走 stdout —— 真实 MCP 服务端常干这事（尤其用 print 打日志的那种）
        sys.stdout.write("[fixture] 这是一行横幅，不是 JSON\n")
        sys.stdout.write("INFO: 服务端启动了\n")
    _flush_write({"jsonrpc": "2.0", "id": mid, "result": result})


def _error(mid, code: int, message: str) -> None:
    _flush_write({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": code, "message": message}})


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def _handle_tools_call(mid, params: dict) -> None:
    name = str((params or {}).get("name") or "")
    args = (params or {}).get("arguments") or {}

    if name == "echo":
        if MODE == "hang":
            time.sleep(600)          # 永不回应：考客户端的超时
            return
        if MODE == "crash":
            sys.stderr.write("[fixture] 模拟崩溃：进程即将退出\n")
            sys.stderr.flush()
            os._exit(3)
        if MODE == "iserror":
            r = _text("模拟的服务端错误：ECHO_FAILED")
            r["isError"] = True
            _reply(mid, r)
            return
        _reply(mid, _text(f"echo: {args.get('text', '')}"))
        return

    if name == "fail_soft":
        payload = {"ok": False,
                   "error": str(args.get("reason") or "模拟的载荷级失败"), }
        _reply(mid, _text(json.dumps(payload, ensure_ascii=False)))
        return

    if name == "write_note":
        out = str(args.get("out") or "")
        try:
            p = Path(out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(args.get("text") or "note"), encoding="utf-8")
            _reply(mid, _text(json.dumps(
                {"ok": True, "file": str(p.resolve()), "bytes": p.stat().st_size},
                ensure_ascii=False)))
        except Exception as e:
            _reply(mid, _text(json.dumps(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)))
        return

    if name == "write_default":
        p = Path("default_note.txt")            # 相对路径，按**本进程 cwd** 解析
        try:
            p.write_text(str(args.get("text") or "note"), encoding="utf-8")
            _reply(mid, _text(json.dumps(
                {"ok": True, "file": str(p.resolve()), "cwd": os.getcwd()},
                ensure_ascii=False)))
        except Exception as e:
            _reply(mid, _text(json.dumps(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)))
        return

    _error(mid, -32602, f"未知工具: {name}")


def handle(msg: dict) -> None:
    mid = msg.get("id")
    method = str(msg.get("method") or "")
    params = msg.get("params") or {}

    if method == "initialize":
        if MODE == "noinit":
            if mid is not None:
                _error(mid, -32603, "模拟的初始化失败")
            return
        if mid is not None:
            _reply(mid, {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "rongke-fixture", "version": "0.0.1"},
            })
        return

    if method.startswith("notifications/"):
        return                        # 通知不需要回应

    if method == "tools/list":
        if mid is not None:
            _reply(mid, {"tools": [] if MODE == "notools" else TOOLS})
        return

    if method == "tools/call":
        if mid is not None:
            _handle_tools_call(mid, params)
        return

    if mid is not None:
        _error(mid, -32601, f"未知方法: {method}")


def main() -> int:
    if MODE == "junk":
        sys.stdout.write("[fixture] 启动横幅（不是 JSON）\n")
        sys.stdout.flush()
    for line in sys.stdin:
        s = line.strip()
        if not s:
            continue
        try:
            msg = json.loads(s)
        except Exception:
            continue
        if not isinstance(msg, dict):
            continue
        try:
            handle(msg)
        except Exception as e:
            mid = msg.get("id")
            if mid is not None:
                _error(mid, -32603, f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
