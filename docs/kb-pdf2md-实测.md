# PDF → Markdown 实测结论（2026，真跑过）

> 来源文件：`~\.dsh\papers\Recent-developments-in-systems-biology-a\data\lit_pdf\npj2024.pdf`
> 21 页、双栏、英文综述（npj Vaccines 2024, DOI 10.1038/s41541-024-00834-y）。
> 解释器：系统 Python 3.12.10。

## 1. 装了 / 版本

```
pip install pymupdf4llm     →  exit 0
pymupdf  1.28.0  →  1.28.2   （安装时被动升级）
```

⚠️ **注意这次被动升级**：PyMuPDF 的**完全 AGPL 化正是从 1.28.2 开始**的
（[官方博客](https://pymupdf.io/blog/open-source-all-the-way-down-pymupdf4llm-goes-fully-agpl)，
日期 2026-08-10）。也就是说 `pip install pymupdf4llm` 这一步**顺带把许可证也换了**。
本项目本来就依赖 `fitz`（`rongke/figure.py`），所以 AGPL 不是新增负担；
但**这是一次真实的许可证变更，值得记在案**，不能当成"只是装了个包"。

## 2. 实测数字（★ 核心）

| 配置 | 21 页耗时 | 每页 | 产出字符 |
|---|---|---|---|
| **默认**（`use_ocr` 默认开 + 写图） | **53.2 s** | 2.53 s | 155,401 |
| `use_ocr=False` | 31.8 s | 1.51 s | 153,402 |
| `use_ocr=False, write_images=False` | **24.4 s** | **1.16 s** | 153,402 |

**读出来的三件事**：

1. **OCR 吃掉了约 21 秒**（53.2 → 31.8），而它**只多产出 1,999 字符**。
2. **写图又吃掉约 7 秒**（31.8 → 24.4），且字符数**一个不差**（153,402）。
3. ⇒ 默认配置比"关掉 OCR + 不写图"**慢 2.2 倍**，而正文内容几乎一样。
   **这两个开关是这条路上最值钱的两个参数。**

⚠️ **默认开着 OCR 这件事本身是静默的**：控制台只打了
`rapidocr_api using backend: rapidocr_onnxruntime` / `OCR on page.number=4/5`，
**没有说"这会让你慢一倍"**。这正是本仓一贯在防的那类失败 —— 它不报错，只是慢。

### 一个尚未解释的现象（**如实记下，不当它不存在**）
`use_ocr=False` 比默认**少了 1,999 字符**。差额可能来自被 OCR 救回来的
扫描页/图内文字。**本次没有逐字比对那 1,999 字符是什么**，
所以"关 OCR 无损失"这句话**现在还不能说**。要用在关键稿子上时，需先补这一步。

## 3. 质量（同一份产物核的）

| 维度 | 实测 |
|---|---|
| 标题层级 | **19** 个 `#` 标题 —— 结构被识别出来了 |
| 表格 | **132** 个 `|` —— 表格确实转成了 md 表 |
| 参考文献 | **220** 条编号条目，且**格式干净**（`1. Trunz, B. B., Fine, P. & Dye, C. …`） |
| 双栏阅读顺序 | **正常** —— 正文一路连贯到末页版权声明，**没有左右栏串行** |
| 尾页 | 落到 `© The Author(s) 2024 / npj Vaccines | (2024) 9:57`，**完整** |

⇒ 对**英文双栏期刊论文**，PyMuPDF4LLM 的表现**够用且快**。
   公式（LaTeX）本次未评估 —— 这篇综述本身公式很少，**不能据此下结论**。

## 4. 一个踩到的坑（本仓文档早有记载，这次亲自撞上）

```
UnicodeEncodeError: 'gbk' codec can't encode character '\xa9'
```
Python 在 Windows 上重定向/管道输出时默认走 **cp936**，遇到 `©` 直接抛错
**并丢掉整段输出**。本仓 `cli.py::force_utf8()` 早就为这个写过注释。
⇒ 任何新的子进程/脚本**必须 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`**，
   否则日志会在某个非 GBK 字符处**无声断掉**。

## 5. 结论与建议配置

```python
pymupdf4llm.to_markdown(doc, use_ocr=False, write_images=False)   # 默认走这条
```

- 对**已是数字文本的 PDF**（绝大多数期刊下载件）—— 走上面这条，1.16 s/页。
- 对**扫描件**（没有文字层）—— 才开 `use_ocr=True`，并接受 2 倍耗时。
- **判据不该由人猜**：`litkb` 抽出的文字量/页可以当信号（近似空白页 ⇒ 大概率是扫描件）。

⇒ 于是"要不要 OCR"应当是**自动判定**，而不是一个拍脑袋的默认值。
   这条留给实现。
