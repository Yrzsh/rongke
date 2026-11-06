# 荣珂 · 许可与第三方声明

Copyright (c) 2026 The Rongke Authors

## 两条授权，互不覆盖

| 对象 | 授权 | 文件 |
|---|---|---|
| Python 框架源码、`rongke-*.whl`、`rongke-*.tar.gz` | **Apache License 2.0**（可商用、可修改、可再分发） | `LICENSE` |
| 预编译 Windows 桌面软件 `RongkeSetup-*.exe` / `Rongke.exe`（含试用与激活） | **专有，保留所有权利** | `LICENSE-DESKTOP.txt` |

框架开源**不**等于桌面软件开源。你可以拿框架做自己的产品、接自己的模型、
换自己的门禁；但直接再分发作者签发的二进制安装包仍受专有条款约束。
安装二进制时请一并附上 `LICENSE-DESKTOP.txt`。

## 第三方组件

荣珂核心运行时**不捆绑任何第三方库**。以下组件由使用者**自行安装**，
各自许可证独立适用，荣珂不分发它们的副本：

- `matplotlib`、`pandas`、`numpy`、`openpyxl`、`PyMuPDF` —— 出版级绘图与 PDF 渲染后审计（`pip install "rongke[figure]"`）。不装时荣珂用**纯标准库 SVG 后端**出预览图，不假装已过投稿级审计。
- `pywebview` + WebView2 Runtime —— 桌面内嵌窗口（`pip install "rongke[desktop]"`）。
- `pywin32` —— 仅 Windows，操作 Origin 等 COM 组件（`pip install "rongke[com]"`）。
- Origin / OriginLab、`~/.dsh/papers/workbench` 的 MCP 服务 —— 商业软件与本机工作台，**不是荣珂的依赖**。荣珂不调用、不分发、也不假设它们存在。

`vendor/ppt-mcp/` 是上游 MIT 的钉版本副本，其 `LICENSE` 随副本保留，按 MIT 处理。

## 不随开源发行件分发的内容

`vendor/skills/` 是第三方技能资料，主体许可为 **CC BY-NC-SA 4.0**（禁止商用、
相同方式共享），其中 `tavotto-figure` 另有 AGPL-3.0 上游。它与本仓库的双轨
授权冲突，因此**不进公开仓、不进 wheel / sdist / 安装包**；开发机上的本地副本
仅为回归测试保留，不构成再分发。

开源发行件自带的是 `rongke/skills/` 下**荣珂自研**的 Apache-2.0 最小技能。
其余 `nature-*` 系列技能属可选外部资料：缺了不会影响框架内核、对话、门禁、
证据门与四份零外部技能预设；`rongke --presets --live` 会逐份如实列出坏件与理由，
不会把「读不到」当成「已通过」。

## 商标

「荣珂 / Rongke」名称与图标不在 Apache-2.0 授权范围内。基于本框架再分发时，
请使用你自己的名称与标识。
