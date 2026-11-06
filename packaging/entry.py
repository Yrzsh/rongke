# -*- coding: utf-8 -*-
"""PyInstaller 的唯一入口。

桌面启动器本来就有 `main()`；这里不复制任何逻辑，只把它接到 exe 上。
"""
import sys

from rongke.app import main

if __name__ == "__main__":
    sys.exit(main())
