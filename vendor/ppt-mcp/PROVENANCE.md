# vendor/ppt-mcp — 第三方代码，钉版本

本目录**不是荣珂的代码**，是上游开源项目的一份**钉版本副本**。放进来是为了让
「组会 PPT」这条能力**不依赖任何一次成功的网络安装**：装包会失败、环境会半成品、
缓存会被清理，而这里 20 个上游运行文件 + `LICENSE` + `SHA256SUMS`
让"服务端到底是哪一份"永远可回答。

## 出处

| 项 | 值 |
|---|---|
| 上游 | <https://github.com/GongRzhe/Office-PowerPoint-MCP-Server> |
| 许可证 | MIT |
| PyPI 分发名 | `office-powerpoint-mcp-server` |
| **钉住的版本** | **2.0.7**（取自 `office_powerpoint_mcp_server-2.0.7.dist-info`） |
| 取件方式 | `uv tool install office-powerpoint-mcp-server`，取 `Lib/site-packages/` 下的运行文件 |
| 取件日期 | 2026-09-21 |
| 文件数 / 体积 | 20 个上游运行文件（`.py`+`.json`）+ 本目录自个儿的 `LICENSE`/`SHA256SUMS`/`PROVENANCE.md` / ≈384 KB |

## ⚠️ 上游有三处口径互相打架（实测，不是转述）

同一个包里自称三个版本号，**所以任何"上游说有几个工具"都不作数，只有 `tools/list` 算数**：

| 出处 | 自称 |
|---|---|
| `dist-info` | 版本 **2.0.7** |
| `ppt_mcp_server.py` 文件头 | *"Consolidated version with **20 tools**"* |
| 同文件 `get_server_info()` 返回值 | 版本 **2.1.0**、`total_tools: **32**` |
| 上游 README | **32** 工具 / 11 模块（正文另一处写 34） |
| **`tools/list` 实测** | **37 个** |

⇒ 这是本项目记在案的 **L 型（声明≠事实）**：字段在、值在，就是不对。
**纪律：凡是"数量/能力"这类声明，一律拿协议自己的回执核对。**
（`runs/_regress/ppt_tools.json` 是那次实测的原始回执。）

## 另一处已实测的上游缺陷：`create_presentation` 不切"当前演示文稿"

`ppt_mcp_server.py` 里定义了 `create_presentation_wrapper` / `open_presentation_wrapper`
两个包装器用于同步 `current_presentation_id`，**但它们从未被任何地方调用**（死代码）。
后果：`create_presentation` 成功返回 `presentation_id` 之后，紧接着调
`get_presentation_info` 会得到 `"No presentation is currently loaded"` ——
**看起来像"创建没生效"，其实是"没切过去"**。

⇒ 正确用法：`create_presentation` / `open_presentation` 之后**必须**再调一次
`switch_presentation(presentation_id=...)`，或者给每个后续工具显式传 `presentation_id`。
这条已写进内核的组会纪律，落在**两处**（改一处忘一处就是 H 型）：
`rongke/contract.py::deck_discipline_text()`（事前注入给模型的那段）
与 `rongke/loop.py` 的 `SYSTEM`（常驻纪律）。否则模型第一枪就打空，
而报错信息会把它引向"再创建一次"——那是另一个坑。

## 许可证合规

本目录**自带**一份上游 `LICENSE`（MIT, © 2025 GongRzhe）。
MIT 的条件是"许可证与版权声明须随副本或实质部分一并保留"⇒ 分发本仓库时
**不能删掉这个文件**。它已纳入 `SHA256SUMS`，所以"被谁改过"也查得出来。

⚠️ 上游仓库根目录的 LICENSE 不在 `site-packages` 里（那是**取件位置**），
所以这份是从上游仓库 `raw` 取的，**不是**从安装产物里拷的 —— 出处不同，
已在上面「取件方式」一行之外单独记：上游仓库 `LICENSE`（`main` 分支，取件日期同上）。

## 怎么用

依赖**不由这里承担**：`mcp` / `python-pptx` / `Pillow` / `fontTools` / `lxml` 由
`config.json` 的 `mcp.servers.ppt.command` 所指的那个解释器提供（本机 = 系统 Python 3.12，
它已经装齐；本项目纪律：**需要特定依赖的是被调用那一侧**，由配置指路，不给内核加依赖）。

```jsonc
"ppt": {
  "command": "C:\\...\\Python312\\python.exe",
  "args": ["D:\\桌面\\荣珂\\vendor\\ppt-mcp\\ppt_mcp_server.py"]
}
```

`ppt_mcp_server.py` 用 `from tools import ...` 做**顶层导入**，而 Python 会把
**脚本所在目录**放进 `sys.path[0]` ⇒ 同目录的 `tools/`、`utils/` 会被正确找到。
**这也是不能把它"只拷一个文件"出去的原因。**

自检（一条命令，不含任何删除操作）：

```bash
cd vendor/ppt-mcp && sha256sum -c SHA256SUMS
```

## 怎么升级

1. 重新取件（`uv tool install` 或直接下 GitHub tag），
2. 覆盖本目录的运行文件，
3. **重新生成** `SHA256SUMS`（别手改），
4. 核对 `tools/list` 的真实工具数与 `readonly` 名单是否变化 ——
   名单住在 `config.json` 的 `mcp.servers.ppt.readonly`，**上游加了新只读工具时这里不会自动跟上**，
   而"未知工具一律按会改处理"是保守的默认，代价只是多检查一次路径。
