# -*- coding: utf-8 -*-
"""工作区围栏单测 —— 不发 LLM 请求，直接验钩子行为。

用法：python fence_test.py

★ 为什么用合成工具而不是 `export_docx`：
  围栏是**内核能力**，不该依赖本机装没装 workbench。CI 裸环境里
  `install_workbench` 注册不到 `export_docx`，原来的测法会报
  "越界绝对路径未被拦截" —— 那是夹具缺件，不是围栏漏拦。
  这里注册两个只存在于测试里的工具（一 mutating、一只读），
  测的仍然是 `WorkspaceFence.before_call` 的真实判定。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke.hooks import WorkspaceFence           # noqa: E402
from rongke.tools import Tool, ToolRegistry       # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="rk-fence-") as td:
        ws = (Path(td) / "ws").resolve()
        ws.mkdir()
        reg = ToolRegistry()

        schema = {
            "type": "object",
            "properties": {"out_path": {"type": "string"}},
            "required": ["out_path"],
        }

        def _probe(out_path: str = "") -> dict:
            return {"written": out_path}

        reg.register(Tool(
            name="fence_probe", description="测试用 mutating 工具",
            parameters=schema, fn=_probe, domain="core",
            mutating=True, source="test"))
        reg.register(Tool(
            name="fence_probe_ro", description="测试用只读工具",
            parameters=schema, fn=_probe, domain="core",
            mutating=False, source="test"))

        fence = WorkspaceFence(roots=[ws], base=ws)
        reg.hooks = [fence]

        print(f"围栏根 = {ws}\n")
        # 落在临时根之外、但一定是**绝对路径**（Windows / POSIX 都对）
        outside = str((Path(td) / "outside.docx").resolve())

        # 1) mutating 工具 + 越界绝对路径 → 必须拦
        c1 = reg.call("fence_probe", {"out_path": outside})
        print(f"1) mutating + 越界绝对路径 -> {'BLOCKED' if c1.blocked_by else 'ALLOWED'}")
        if c1.blocked_by:
            print(f"   拦截理由: {c1.error[:160]}")

        # 2) mutating 工具 + 相对路径 → 必须放行
        c2 = reg.call("fence_probe", {"out_path": "out/draft.docx"})
        print(f"2) mutating + 相对路径     -> {'BLOCKED' if c2.blocked_by else 'ALLOWED'}")

        # 3) 只读工具 + 越界路径 → 不拦（只读不写，没理由拦）
        c3 = reg.call("fence_probe_ro", {"out_path": outside})
        print(f"3) 只读工具 + 越界路径     -> {'BLOCKED' if c3.blocked_by else 'ALLOWED (按设计)'}")

        # 4) mutating 工具 + 围栏内的绝对路径 → 放行
        c4 = reg.call("fence_probe", {"out_path": str(ws / "ok.docx")})
        print(f"4) mutating + 围栏内绝对路径 -> {'BLOCKED' if c4.blocked_by else 'ALLOWED'}")

        print(f"\n拦截记录: {fence.summary()}")

        ok = True
        if c1.blocked_by != "workspace_fence":
            print("✘ 失败：越界绝对路径未被拦截"); ok = False
        if c2.blocked_by:
            print("✘ 失败：相对路径被误拦"); ok = False
        if c3.blocked_by:
            print("✘ 失败：只读工具被误拦"); ok = False
        if c4.blocked_by:
            print("✘ 失败：围栏内路径被误拦"); ok = False

        print("\n[PASS] 围栏四条行为全部符合设计" if ok else "\n[FAIL]")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
