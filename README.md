# 荣珂（Rongke）

> 自研内核的科研 agent。模型调用、主循环、完成门禁都是本仓代码，不租 DSH / Claude Code / Codex 这类运行时；领域能力来自你本机的 workbench，同进程直调。

| | 别人的框架 | 荣珂 |
|---|---|---|
| 内核 | 别人的代码，只能挂插件 | 本仓代码 |
| 领域能力 | 从零攒 | 复用 workbench 的工具 / 门禁 / 台账 |
| 完成判定 | 模型说完了就完了 | 九条程序化判定，不采信自述 |

---

## 许可：框架开源，软件专有

| 对象 | 授权 | 文件 |
|---|---|---|
| Python 框架源码、`rongke-*.whl`、`rongke-*.tar.gz` | **Apache-2.0**（可商用 / 可修改 / 可再分发） | `LICENSE` |
| `RongkeSetup-*.exe` / `Rongke.exe`（含试用与激活） | **专有，保留所有权利** | `LICENSE-DESKTOP.txt` |

框架开源 ≠ 桌面软件开源。你可以拿框架做自己的产品、接自己的模型、
换自己的门禁；再分发作者签发的二进制安装包仍受专有条款约束。
第三方组件、不随包分发的资料与商标边界见 `NOTICE.md`。

---

## 安装

两条路按需要选，互不冲突：

| 想要什么 | 拿哪件 | 形态 |
|---|---|---|
| **直接用做好的软件** | `RongkeSetup-<版本>.exe` | Windows 安装包，按用户安装、免管理员，双击开桌面窗口；内置 Web 控制台、试用期与激活 |
| **用框架自己搭 / 二次开发** | `rongke-<版本>-py3-none-any.whl` 或源码 | `pip install` 后得到 `rongke` 命令与 Python 包，预设、能力、Web 控制台全部随包；配置放 `~/.rongke/config.json` |

框架是零第三方硬依赖的纯 Python 包（Python 3.10+）；软件是把同一份内核冻结成免 Python 环境的 exe。两者共用同一套能力预设与门禁。

### Windows 安装包

从 [Releases](https://github.com/Yrzsh/rongke/releases) 下载 `RongkeSetup-<版本>.exe`，双击安装。

- 按用户安装、免管理员权限；程序在 `%LOCALAPPDATA%\Programs\Rongke`。
- 首次启动开始 **30 天试用**；到期只拦「发起新任务」与「会话内追问」，历史与文献库照常。
- 激活码**一码一机**：控制台「试用与激活」页显示机器码 `RK-M-...`，离线 Ed25519 签名，不经过服务器。
- 数据在 `%USERPROFILE%\.rongke`，卸载不删。
- 安装包未签名，SmartScreen 可能提示未知发布者；用 `SHA256SUMS.txt` 校验。

### 源码 / wheel

Python 3.10+，核心运行时零第三方依赖。

```bash
pip install rongke-<版本>-py3-none-any.whl     # 或 pip install .
rongke --setup                                 # 自检：缺什么、怎么补
```

配置模板是 `config.example.json`（sdist / 仓库里）：复制成 `~/.rongke/config.json`
再按本机情况填解释器与 MCP 路径。密钥不要写进配置文件，只走环境变量或
`~/.rongke/secrets.json`。`rongke --presets --live` 可逐份真挂载预设，把坏件
连同理由一起列出来。

可选能力，不装也能用，只是对应工具标「未接」：

```bash
pip install "rongke[figure]"    # matplotlib / pandas / PyMuPDF
pip install "rongke[pdf]"       # 读 PDF
pip install "rongke[com]"       # Windows 下操作 Origin
pip install "rongke[desktop]"   # 内嵌 WebView2 窗口
```

### 原生出图：不装任何东西也能画

荣珂自带 `rongke/svgplot.py`（**纯标准库**）与内置工具 `plot_svg`。
它不需要 Origin、不需要 workbench MCP、不需要 matplotlib / pandas /
PyMuPDF —— 装完框架就能出一张带中文的 SVG 矢量图。

- 选图型复用 `analysis.plan_from_shape()` 量出来的列形状判据（不是凭手感），
  支持 line / time_series / scatter / bar / barh / hist / box / heatmap / matrix。
- 字体栈 `Microsoft YaHei → SimHei → Noto Sans CJK SC → Arial`，中文不会变豆腐块。
- 写完**读回文件**、用 `xml.etree` 真解析，跑 14 项结构自检（viewBox、文本节点、
  数据标记、坐标出界、中文字体栈、外部引用……）；任一不过 `ok:false` 并保留
  文件供核查，不认「写文件成功」就算成功。

```python
from rongke import analysis, svgplot
rows, meta = analysis.read_table("data.csv")
plan = analysis.plan_from_shape(
    "data.csv", meta, analysis.profile_table(rows), title="各批次转化率")
rep = svgplot.render_svg(rows, plan, "figure.svg")
assert rep["ok"], rep["checks"]     # 不认自称成功
```

⚠️ **这是预览级产物**（网页 / 随稿附图 / 给用户看）：PDF 六道渲染后审计门
（源码预检 / 字形 ≥5 pt / 碰撞 / 分面对齐 / 图注 / 新鲜度）一道都没跑，
也没有 600 dpi 位图。**不要把它当投稿级图宣布完成**。要进投稿包请
`pip install "rongke[figure]"` 后走 `plot_data` + `audit_figure`。
`plot_data` 在缺绘图栈时会自动降级到这条原生路线，并在返回值里标明范围；
`plot_svg` 不能直接插 pptx（SVG 不是位图），此时作为随稿附图交付。

### 签发激活码（发行方）

客户把机器码 `RK-M-...` 发来，然后：

```bash
python tools/issue_code.py --customer "某大学" --mid RK-M-XXXXXXXXXXXXXX --days 365
```

输出一行 `RK1...` 发给客户粘贴。`--days 0` 为永久。私钥在
`%USERPROFILE%\.rongke\issuing_key.txt`，不进仓库。

---

## 快速开始

```bash
setx COMMANDCODE_API_KEY "sk-..."      # 密钥只存环境变量名

python -m rongke --tools               # 装配了哪些工具
python -m rongke --mcp                 # MCP 服务端状态（连上的 + 没连上的原因）
python -m rongke "核验参考文献：检索 xxx 至少 3 篇，并 fetch_doi 核验 2 篇"
python -m rongke --json "..." > result.json
python -m rongke --web --port 8765     # Web 控制台
python -m rongke.app                   # 桌面窗口
```

---

## 界面

Web 控制台是单页 `index.html` + `static/`，无 CDN、无构建：契约面板、思考流、
工具调用、证据门、文献库、材料区、历史回放。服务端是标准库 `http.server` + SSE。

桌面窗口有两代宿主，按可用性自动选：

| 宿主 | 条件 | 形态 |
|---|---|---|
| 内嵌 WebView2 | pywebview 可用 **且** 系统有 WebView2 Runtime | `Rongke.exe` 自己当窗口宿主，任务栏是荣珂，不派生 `msedge.exe` |
| Edge `--app` | 上面任一缺失 | 独立窗口，但进程是浏览器 |

`RONGKE_SHELL=edge` 或 `--edge-shell` 可强制回退。两条路径共用同一套判据：
`/api/build` 探活、单实例、关窗收摊、第二次双击切回已有窗口（`app_test.py`）。

---

## 内核

| 模块 | 职责 |
|---|---|
| 目标契约 `contract.py` | 一句话目标 → 交付物 / 验收标准 / 约束 / 禁止，常驻注入防漂移 |
| 主循环 `loop.py` | Plan→Act→Observe→Verify，收敛检测，空转纠正 |
| 九条判定 `gate.py` | 完成前程序化过 9 条，见下 |
| 写作体检 `writing.py` | 体量 / 结构 / 句法 / 套话密度 / 标点 / 引注首现顺序 / 占位符 / 收尾同构，纯标准库 |
| 图面契约 `figure.py` | 五点契约 + 渲染路线分工 + 四道渲染后审计 + 图注 + 报告新鲜度 |
| 阶段路由 `stages.py` | 写作阶段 → 唯一专属技能，含否定语境滤除 |
| 作业纪律 `pitfalls.py` | 30 条实测踩坑清单，内核原生技能，不落磁盘、不可被同名技能顶掉 |
| 工具注册表 `tools.py` | 工具一等公民，结构化台账，调用指纹，schema 预校验 |
| 钩子 `hooks.py` | 工作区围栏、审批、审计 |
| MCP 客户端 `mcp_client.py` | 主动连外部服务端，stdio + 手写 JSON-RPC，零依赖 |
| 执行引擎 `llm.py` | OpenAI 兼容，不经过任何 agent 运行时 |
| 引文核验 `citation.py` | DOI 存在性 / 撤稿 / 元数据一致性 |
| 上下文经济 `context.py` | 五级退化阶梯把旧工具结果换成台账现算的替身，纯程序化、可离线复算 |

辅助：`config.py`（密钥只存环境变量名）、`memory.py`（会话存档 + 跨任务经验）、
`bridge.py`（workbench 接入）、`cli.py`（入口）、`web/`（控制台）。

## 九条判定

宣布完成前内核必须过，全部程序化，不采信模型自述：

1. 证据非空
2. 引用的 `call_seq` 真实存在于台账
3. 被引调用 `ok=True`
4. 每条验收标准都被证据覆盖
5. 交付物逐项被提及（软检查，只警告）
6. 阶段技能已装载
7. 写作体检通过
8. 图面体检报告新鲜且通过
9. 组会 PPT 体检报告新鲜且通过

---

## 回归

```bash
python regress.py                  # 全套；唯一清单来源
python regress.py --only web,resume
python regress.py --with-ui-shot   # 附带真实截图
```

判据：空跑（rc=0 却零断言）、无终局地标、断言数比 `runs/_regress/baseline.json`
变少，一律报红。清场一律「挪」不「删」，跑一轮全套的删除消耗为 0。

单测也可以单独跑：`python app_test.py`、`python writing_lint_test.py`、
`python figure_test.py`、`python mcp_client_test.py` 等，均不需要网络。

---

## 目录

```
rongke/        内核 + Web 控制台（web/）
packaging/     PyInstaller spec / Inno Setup / 构建脚本
tools/         签发激活码等脚本
figures/       出版级配图参考实现
runs/          任务工作区 / 材料区 / 回归日志（_ 开头是夹具）
docs/          产品说明、模板、调研
regress.py     回归运行器（唯一清单来源）
_tkit.py       测试工具箱：清场挪不删
```

---

## 已知边界

- 安装包未签名；自动更新尚未接入。
- 试用与激活是君子协定，不承诺防住有意绕过。
- 产出型能力（PPT / 图 / PDF）依赖可选包与特定解释器，缺件时如实标「未接」并写明原因。
- 本机 8765 常被工作台占用，桌面快捷方式固定 `--port 8788`。
- 前端资源不许引 CDN、图标库、网络字体：离线可用是硬要求。

## 后续

- 图面门目前只认 `audit_figure` 一条证据；示意图的图层关系仍要人工确认。
- 证据门对「工具证不了的验收标准」会空转，需在契约阶段区分可证 / 不可证。
- 纪律的实际效果尚无度量：装载踩坑清单是否真的少踩坑，要攒真任务回看。
