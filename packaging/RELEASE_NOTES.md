# 荣珂 0.1.8

这一版做两件事：**框架按 Apache-2.0 开源、成品软件保持专有**；以及让
**装完就能原生出图**，不再依赖 Origin / workbench MCP / matplotlib。

## 两条授权，别混

| 对象 | 授权 | 文件 |
|---|---|---|
| 框架源码、`rongke-0.1.8-py3-none-any.whl`、`rongke-0.1.8.tar.gz` | **Apache-2.0**（可商用、可修改、可再分发） | `LICENSE` |
| `RongkeSetup-0.1.8.exe` / `Rongke.exe`（含 30 天试用与激活） | **专有，保留所有权利** | `LICENSE-DESKTOP.txt` |

安装包会把两份许可与 `NOTICE.md` 一起放进安装目录。第三方内容与商标的
边界见 `NOTICE.md`。

## 安装 / 升级

- 直接用软件：`RongkeSetup-0.1.8.exe`，按用户安装、免管理员权限。
  ⚠️ 与 0.1.5 / 0.1.7 同一个 AppId，安装会**覆盖**已有版本；数据在
  `%USERPROFILE%\.rongke`，不受影响。
- 用框架：`pip install rongke-0.1.8-py3-none-any.whl` → `rongke --setup`
  → `rongke --presets --live` → `rongke --web --port 8765`。
- 源码发行件 `rongke-0.1.8.tar.gz` 含 `config.example.json` 与打包脚本。
- 安装包未签名，SmartScreen 可能提示「未知发布者」；用 `SHA256SUMS.txt` 校验。
- ⚠️ 本机的 Inno Setup 缺中文语言文件，安装界面仍是英文；软件界面是中文。

## 这一版改了什么

- **开源双轨落地**：`LICENSE` 换成 Apache-2.0 官方全文；原专有协议原文
  一字不改移到 `LICENSE-DESKTOP.txt`，只约束二进制；新增 `NOTICE.md`；
  `packaging_test.py` 里那条把「必须专有」钉死在框架上的断言改成双轨边界判据
  （正控 + 负控，两份许可不许退化成同一份）。
- **第三方 NC 内容撤出公开仓**：`vendor/skills/` 是 CC BY-NC-SA 4.0
  （禁止商用 + 相同方式共享），与双轨冲突 ⇒ `git rm --cached`（**本地文件保留**，
  仅供回归），不进公开仓、不进 wheel / sdist / 安装包。
- **自研最小技能随包**：`rongke/skills/uncertainty-and-units/SKILL.md`
  （Apache-2.0 自写）接进 `stages.SKILL_ROOTS`，`stats-review` 预设在
  干净 clone 的机器上不再因这份第三方技能缺席而坏。
- **原生零依赖出图**：新增 `rongke/svgplot.py`（纯标准库 SVG 渲染器）与
  内置工具 `plot_svg`，支持 9 种图型，复用 `analysis.plan_from_shape()`
  的选图型判据；中文按 `Microsoft YaHei → SimHei → Arial` 字体栈回退。
  写完**读回文件**用 `xml.etree` 跑 14 项结构自检，任一不过 `ok:false`
  且保留文件供核查 —— 不重演「中文豆腐块、退出码 0、自称成功」。
- **`plot_data` 自动降级**：本机缺 matplotlib/pandas/PyMuPDF 时不再只回一句
  「装置不全」，而是自动用原生后端出预览图并在返回值里标明范围。
- **提示词不再指向不存在的工具**：deck 配方里写死的外部
  `figure_render(spec_json=…)` 换成内置 `plot_data` / `plot_svg`；
  同时写明 SVG 不能直接插 pptx，没有 PNG 时不许调 `manage_image`。
- **图面门新增预览分支**：只有台账里真出现过一次成功的 `plot_svg`
  （且没跑出 PDF、也没审过图）才把第 8 条硬门降成**警告**，警告里逐条列出
  本次没跑的六道门。缺件、没调工具、自称出了图 —— 一律照旧拦。
- **发行件补齐**：预设 JSON、随库能力 `caps_ext/*.py`、自研技能
  `rongke/skills/*/SKILL.md` 三者都进 wheel / sdist / 冻结版，逐文件读回核对。

## 22:40 修订（同一 0.1.8，三件发行件重出）

全套回归把 `RONGKE_WEB` 真接进运行器之后，原本「连不上控制台就没测」的
静态检查才第一次读到真实 `/api/tools`，随后抓出两处前端缺陷：

- **「环境检查」页死线太紧**：刚起的控制台首次 `/api/setup` 实测 **22.6s**
  （之后降到 3.5s），而前端只给 25s ⇒ 高负载下整页显示「检查没跑成」，
  连账单区块都不生成。死线放宽到 45s；浏览器判据改为**先预热该接口**，
  把「冷启动」这个无关变量从「账单有没有上屏」里摘出去。
- **`plot_svg` 漏中文名**：主视图会显示裸英文标识符。已补进 `TOOLHUMAN`，
  `ui_check` 的 W18 从 1 失败变 21/0 全绿。

这两处都在 `rongke/web/static/workspace.js`，而它随 wheel / sdist / 安装包
分发 ⇒ 三件重出，**旧的 `SHA256SUMS.txt` 作废**，请以新校验和为准。
同时修的还有回归侧内容：`docs/产品说明.md` 工具数 66→67、两份
`agent-brief.txt` 重新渲染（它们不进发行件）。

## 15:05 修订（同一 0.1.8，三件发行件重出）

修用户实报的「在输入框里输入字符，『你想让荣珂帮你完成什么』会一直往上跑」：

- **根因**：空态 `#v-chat` 占满一屏，sticky 的 `#composer` 又在流内占 248px，
  于是 `#scroll` 凭空多出 248px 可滚动余量。聚焦输入框时浏览器原生把 sticky
  输入框对齐到底（`scrollTop 0→248`），配合平滑滚动，标题就被推出视口
  （`h2.top 178→-70`）。`followBottom()` 的空态守卫挡得住 JS 主动跟随，
  **挡不住浏览器原生对齐** —— 这一版两处都补上了。
- **修法**：空态让 `#composer` 脱离流贴底（`scrollHeight` 回到等于视口），
  会话态**仍然保持 sticky**（输入框常驻底部、`.thread` 底部留白都靠它）；
  小屏那条会把 position 设回 sticky 的规则不再回设，避免两处各记一个真相。
- **判据**：新增 `hero_scroll_test.py`（真浏览器量几何，含会话态
  `followBottom(true)` 正控），修前 5 项 / 2 红、修后 **5/0**；四种视口
  （1440×900 / 1024×768 / 800×600 / 390×844）实测标题不动、无裁切。

改动落在 `workspace.js` / `workspace.css` / `layout.css`，三份都随
wheel / sdist / 安装包分发 ⇒ 三件重出，**旧 `SHA256SUMS.txt` 再次作废**。
已装用户不必重装：本次修复也已热同步到安装目录，刷新页面即可。

## 仍然要知道的

- **原生 SVG 是预览级**（网页 / 随稿附图 / 给用户看）：PDF 六道渲染后审计门
  一道都没跑，没有 600 dpi 位图与投稿级物理尺寸。要进投稿包请
  `pip install "rongke[figure]"` 后走 `plot_data` + `audit_figure`。
- 6 份预设仍点名 `nature-*` 外部技能（在你本机 `~/.agents/skills`），
  不在开源发行件里；`rongke --presets --live` 会逐份列坏件与理由。
  四份零外部技能预设（baseline / lean-context / strict-v2 /
  ext-answer-first）在干净机器上开箱可用。
- 文献库 / 检索 / 入库仍依赖本机 PaperWorkbench 及其数据。
- 30 天试用、一码一机规则不变；WebView2 Runtime 随系统 Edge，缺失时退回
  Edge `--app`。
- 未量检索质量、token 成本与延迟；`cost=UNKNOWN` 仍是已知缺口。
