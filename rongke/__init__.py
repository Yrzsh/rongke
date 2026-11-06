# -*- coding: utf-8 -*-
"""荣珂 —— 自研科研 agent 内核。

不依赖任何外部 agent 运行时：模型调用、主循环、完成证据门都是本仓库自己的代码。
领域能力（工具 / 门禁 / 台账）来自 workbench，同进程直调，零拷贝复用。
"""
__version__ = "0.1.8"
__all__ = ["config", "tools", "llm", "bridge", "contract", "gate", "loop", "memory", "hooks"]
