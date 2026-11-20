# -*- coding: utf-8 -*-
"""把 `aideck/selftest.py` 挂进 regress.py 的薄包装。

`aideck/selftest.py` 用包内相对 import，直接 `python aideck/selftest.py` 会
「attempted relative import with no known parent package」—— 那不是代码错，
是启动方式错。这里用 `runpy.run_module` 以模块方式跑，**不改判据本身**。
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    runpy.run_module("aideck.selftest", run_name="__main__")
