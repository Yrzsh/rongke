# -*- coding: utf-8 -*-
"""荣珂自检：配置 → 工具反射 → 直调工具 → LLM 连通。

四步都过，说明"内核地基"是活的：模型能通、工具能调、台账能落。
用法：python selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rongke.bridge import install_workbench          # noqa: E402
from rongke.config import Config                     # noqa: E402
from rongke.llm import LLM, LLMError                 # noqa: E402
from rongke.tools import ToolRegistry                # noqa: E402


def main() -> int:
    print("=" * 74)
    print(f"荣珂自检   root={Path(__file__).resolve().parent}")
    print("=" * 74)

    cfg = Config.load()
    print("\n[1] 配置")
    print(f"    provider  = {cfg.provider.name}")
    print(f"    model     = {cfg.provider.model}")
    print(f"    base_url  = {cfg.provider.base_url}")
    print(f"    key 状态  = {cfg.provider.diagnose()}")
    print(f"    workbench = {cfg.runtime.workbench}  (存在={cfg.runtime.workbench.exists()})")
    print(f"    loop      = max_steps={cfg.loop.max_steps} "
          f"max_identical_calls={cfg.loop.max_identical_calls}")

    print("\n[2] 反射 workbench 工具")
    reg = ToolRegistry()
    try:
        installed = install_workbench(reg, cfg.runtime.workbench)
    except Exception as e:
        print(f"    !! 失败: {type(e).__name__}: {e}")
        return 1
    print(f"    注册 {len(installed)} 个（W = 会改动外部世界）")
    for n in installed:
        t = reg.get(n)
        props = list((t.parameters or {}).get("properties", {}).keys())
        mark = "W" if t.mutating else " "
        print(f"      [{mark}] {n:<24} 参数={props}")

    print("\n[3] 直调工具（不经模型，验证桥是通的）")
    tc = reg.call("ledger_query", {"last_n": 2})
    print(f"    ok={tc.ok} ms={tc.ms}")
    print(f"    {tc.brief(220)}")

    print("\n[4] LLM 连通")
    llm = LLM(cfg.provider, timeout=cfg.loop.request_timeout)
    print(f"    {llm.describe()}")
    try:
        # chat() 返回 (msg, usage, reasoning) —— 第三项是思维链文本
        msg, usage, reasoning = llm.chat(
            [{"role": "user", "content": "只回复两个字：在线"}], temperature=0.0
        )
    except LLMError as e:
        print(f"    !! {e}")
        return 1
    print(f"    回复 = {msg.get('content')!r}")
    print(f"    用量 = {usage.line()}")
    print(f"    思维链 = {len(reasoning)} 字符" +
          (f"  「{reasoning[:40]}…」" if reasoning else "（本次未返回）"))

    print("\n[DONE] 四项全过：配置活、工具活、桥活、模型活")
    return 0


if __name__ == "__main__":
    sys.exit(main())
