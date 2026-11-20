# -*- coding: utf-8 -*-
"""AI 画布服务 —— 让任何 agent 直接调这个工具，不必进荣珂内核。

    python -m aideck.serve <工作目录> --port 8795

    GET  /            画布台面（给人看的窗口，= Origin 的画布窗口）
    GET  /state       当前画布 JSON（AI 读状态）
    GET  /health      工具在不在、后端在不在
    POST /act         {"op":"add", ...} 一个原子动作 → 写后读回 + 质检
    POST /export      导出 .pptx + 重画布台面

为什么单独起一个服务而不是只做荣珂插件：用户要的是**一个专门给 AI 用的
工具**，不是一个流程里的又一层封装。独立地址、独立状态、独立画布窗口，
任何 agent（荣珂 / 别的框架 / 一段脚本）都能用，互不绑死。

每个 `/act` 的回包都是「现在画布长什么样」，不是「写成功」：
    {ok, applied, action, element?, gaps, counts, readback_equal, ...}
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .canvas import Canvas
from .studio import backend_python, export_pptx, render_html

_LOCK = threading.Lock()
_C: Canvas | None = None


def _json(handler, code: int, obj) -> None:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_act(action) -> dict:
    """可被直接调用的纯函数（不起服务也能测）。"""
    if _C is None:
        return {"ok": False, "error": "画布未初始化"}
    with _LOCK:
        rep = _C.apply(action)
        rep["state_path"] = str(_C.state_path)
        return rep


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass                     # 安静：动作本身已经写进画布状态了

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"__parse_error__": True}

    def do_GET(self):
        if self.path.startswith("/health"):
            return _json(self, 200, {"ok": True, "tool": "aideck",
                                     "python_pptx": bool(backend_python()),
                                     "pages": len(_C.doc["pages"]) if _C else 0})
        if self.path.startswith("/state"):
            return _json(self, 200, _C.doc if _C else {"ok": False})
        if self.path.startswith("/"):
            with _LOCK:
                r = render_html(_C)
            body = Path(r["page"]).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/act"):
            obj = self._read_body()
            if obj.get("__parse_error__"):
                return _json(self, 400, {"ok": False, "error": "请求体不是合法 JSON"})
            return _json(self, 200, handle_act(obj))
        if self.path.startswith("/export"):
            with _LOCK:
                r = export_pptx(_C)
            return _json(self, 200, r)
        self.send_error(404)


def main() -> int:
    global _C
    ap = argparse.ArgumentParser(description="AI 画布服务")
    ap.add_argument("root", help="画布工作目录")
    ap.add_argument("--port", type=int, default=8795)
    args = ap.parse_args()
    _C = Canvas(Path(args.root).resolve())
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"aideck 画布服务 http://127.0.0.1:{args.port}/  root={_C.root}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
