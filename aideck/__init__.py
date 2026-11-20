# -*- coding: utf-8 -*-
"""aideck —— 给 AI 用的图元级幻灯片工具（类比 Origin 给人的作图台）。

    from aideck import Canvas, render_html, export_pptx
    c = Canvas(workdir, title="组会汇报")
    c.apply({"op": "new_page", "kind": "content"})
    c.apply({"op": "add", "type": "text", "box": [72, 36, 1136, 72],
             "text": "研究背景", "size": 40})
    c.apply({"op": "query"})            # AI 的眼睛
    c.apply({"op": "audit"})            # AI 的尺子
    render_html(c); export_pptx(c)      # 给人看 / 交付

内核零第三方依赖；导出 .pptx 时才用装了 python-pptx 的解释器起子进程。
"""
from .canvas import Canvas, CanvasError, CANVAS_PX, PX_EMU, TYPES
from .studio import (audit_rendered, backend_python, export_pptx,
                     render_html)

__all__ = ["Canvas", "CanvasError", "CANVAS_PX", "PX_EMU", "TYPES",
           "render_html", "export_pptx", "backend_python", "audit_rendered"]
__version__ = "0.1.0"
