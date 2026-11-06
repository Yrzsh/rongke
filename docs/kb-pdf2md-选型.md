# PDF → Markdown 选型（已核实 / 未核实分开列）

> ⚠️ 本次会话 `pwsh` 完全不可用 ⇒ **没有实测任何性能数字，没跑过一行代码**。
> 下表只写**能从官方来源核到的**事实，核实不到的明确标「未核实」，不拿猜测填。

## 1. 转换器对比

| 工具 | 许可证 | 需要 torch/GPU | 特长 | CPU 速度 | 静默失败 |
|---|---|---|---|---|---|
| **marker** | ✅ **Apache-2.0** | 是（GPU 更佳） | 双栏、表格、公式、阅读顺序，精度第一梯队 | 未核实 | 未核实 |
| **Docling** (IBM) | ✅ **MIT** | 是，但有 CPU-only 路（`--no-ocr`/不启 VLM） | 表格、阅读顺序、版面 | 未核实 | 未核实 |
| **GROBID** | ✅ **Apache-2.0** | 否（Java/JVM） | **元数据 + 参考文献解析是它的专长** | 未核实 | 未核实 |
| **PyMuPDF4LLM** | ❌ **AGPL-3.0** | **否**，纯 wheel | 快、版面尚可；**无 OCR，公式弱** | 未核实 | 未核实 |
| **MinerU** | ⚠️ **NOASSERTION**，**不是标准 SPDX** | 是 | 版面+OCR+公式 | 未核实 | 未核实 |

### 许可证这条怎么看（★ 关键判断）

子代理给的结论是「干净 = marker / Docling / GROBID，被 AGPL 挡住的 = PyMuPDF4LLM」。
**但那个结论对本仓库不成立**，因为：

> 荣珂**已经在用 PyMuPDF** —— `rongke/figure.py` 的
> `_NEEDED_MODULES = ("matplotlib", "fitz")`，且 `kb_e2e_test.py:110` 明说
> `fitz` 是**被测链路本来就必须有**的硬依赖。

⇒ AGPL 在本机**已经是既成事实**。「AGPL 所以不能用」不是一条新代价，
  而是**既有决定**的延伸。真正的分水岭不是许可证，是**依赖重量**：
  - PyMuPDF4LLM：纯 wheel，装进已有的 3.12 就行，**无 torch**；
  - marker / MinerU / Docling：拖 torch + 模型权重，量级完全不同。

⇒ 若将来要商用，正确的动作是**连同已有的 `fitz` 一起重估**，
  而不是单独否掉新加的那一个。（这条值得写进决策记录。）

### 待补（决定了才算选完）
1. **MinerU 的真实许可证** —— 它是 `NOASSERTION`，而它正好是那个 MIT 的
   Zotero 插件的后端。**不能假设是 Apache**。
2. 三者的**安装体积**（GB）全部未核实。
3. **静默失败模式**（本次核心问题之一，零收获）——
   对本项目尤其重要：本仓反复吃过「退出码 0、产物是错的」。
   ⇒ 转换器必须配**负控**（拿一份已知会坏的 PDF 试，看它报不报）。

## 2. Zotero → Markdown 插件

| 插件 | 存在 | 维护 | 后端 | 输出目录可配 |
|---|---|---|---|---|
| **zotero-pdf2md** (qingpy) | ✅ | ✅ 活跃 | **MinerU** | 未核实 |
| **zotero-docling** | ✅ | ✅ 活跃 | docling-serve（HTTP） | ✅ 有 export-to-folder |
| **zotero-mdnotes** | ✅ | ❌ **已归档**，2024-10 停 | **不转 PDF**，只元数据+笔记 | N/A |

⚠️ 两个陷阱：
- **zotero-docling 自身是 AGPL-3.0**（虽然后端 Docling 是 MIT）。
- **zotero-mdnotes 已归档且不做 PDF 转换** —— 别再考虑它。
- zotero-pdf2md 是 MIT，但只有 **7 star / 1 contributor**，属未验证。
- **zotero-docling 需要本地跑 `docling-serve`（HTTP）**，且上游 **没有取消 API**、
  也不检测客户端断连 —— 这是真实的运维坑。

## 3. 仍未研究（★ 不要建在这上面）

Zotero Integration 插件、Obsidian Local REST API、Zotero/Obsidian 的 MCP 服务器
—— **全部未核实**，没有任何证据说它们存在、还维护着、或怎么鉴权。

⇒ 结论：**桥接层不能基于"假设有插件"来设计。**
   最稳的形状是荣珂**自己读写文件**（Obsidian vault 本质就是一堆 .md），
   而不是依赖某个第三方插件在中间转发。
