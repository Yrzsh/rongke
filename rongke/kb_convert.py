# -*- coding: utf-8 -*-
"""PDF → Markdown 转换（知识库的**入库前**那一步）。

## 为什么需要它

`litkb` 入库存的是**它自己从 PDF 抽出来的纯文本**（`data/litkb/texts/*.txt`）。
纯文本丢掉了版面信息：标题层级、表格、双栏阅读顺序、参考文献编号 ——
于是下游 `kb_search` 的段落切分、`kb_ask` 的引用质量都受它限制。

本模块补的是**中间层**：把 PDF 转成**结构化 Markdown**，让"方便 AI 读"这件事
有产物可查，而不是停在口号上。

## 形状：与 `figure.py` / `deck.py` 同构（本仓既有范式，不另造）

**重依赖一律走子进程**。理由是本项目的实测事实：
荣珂自己可能跑在一个**裸解释器**上（托管 3.13.12 连 numpy 都没有），
而 `pymupdf4llm` 装在系统 3.12 里。**"荣珂跑在哪个解释器上"不该决定
"它能不能转 PDF"** —— 这与 `figure.py` 找 matplotlib/PyMuPDF、
`mcp_client` 让配置去指定服务端解释器，是同一条判断。

⇒ 本模块**只用标准库**（`subprocess` + `json`），真正干活的代码在
`_CONVERT_SCRIPT` 里，由子进程用**指定解释器**执行。

## ★ 三个设计决定，都有实测依据

### ① 默认**关 OCR、不写图** —— 实测快 2.2 倍，正文几乎不变

同一份 21 页双栏期刊论文（npj Vaccines 2024），系统 Python 3.12：

| 配置 | 耗时 | 每页 | 产出字符 |
|---|---|---|---|
| 默认（开 OCR + 写图） | **53.2 s** | 2.53 s | 155,401 |
| `use_ocr=False` | 31.8 s | 1.51 s | 153,402 |
| `use_ocr=False, write_images=False` | **24.4 s** | **1.16 s** | 153,402 |

OCR 吃掉约 21 秒，**只多产出 1,999 字符**；写图又吃掉约 7 秒，字符数**一个不差**。

⚠️ **而默认开着 OCR 这件事是静默的** —— 控制台只说
`rapidocr_api using backend: rapidocr_onnxruntime`，**不会告诉你慢了一倍**。
这类"不报错、只是慢"正是本仓一贯在防的失败模式。

⇒ 所以本模块**默认走快路**，并且把"这次走的是哪条路"**如实写进返回值**
（`used_ocr`），让人看得见。

⚠️ **尚未解释的现象（如实记，不当它不存在）**：`use_ocr=False` 比默认少 1,999 字符。
差额可能来自被 OCR 救回的扫描页/图内文字。**没有逐字比对过那 1,999 字符是什么**，
所以"关 OCR 无损失"这句话**现在还不能说**。见 `ocr_advice()`：本模块因此
**不替用户断言收益**，只如实回报文字密度，把"要不要 OCR"交回去。

### ② OCR 该不该开，由**文字密度**提示，不由人拍脑袋

扫描件（没有文字层）不开 OCR 会得到近乎空白。判据用**转换后每页字符数**：
明显偏低就**提示**可能该开 OCR。这是**提示、不是自动重跑** ——
自动重跑会让一次慢 2 倍的转换**在你不知情时发生**，那是把可控变成不可控。

### ③ `write_images=False` 是默认，因为**图不是文字库要的东西**

本模块的产物是**喂知识库的正文**。图会带来两笔真实成本（实测约 7 秒/篇），
而 `kb_search` / `kb_ask` 读的是文字。要图请显式 `images=True`。
⚠️ 这与"Obsidian 里想看图"是有张力的 —— 见 `to_obsidian()` 的说明：
**vault 里需不需要图，与知识库需不需要图，是两个问题**，不该用同一个默认值糊过去。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 子进程要用到的解释器候选。与 `figure.py::_PY_CANDIDATES` **同一份形状**，
# 但**各自列各自的**：那边要的是 matplotlib+fitz，这边要的是 pymupdf4llm。
# 合成一份会制造"两处要一起改"的 H 型，而且两边的依赖本来就不同。
#
# ★ 曾经这里写死了 `C:\Users\<某个用户名>\...` 的绝对路径。那既是隐私
#   （会把开发机的用户名随包公开），也是缺陷（别人的机器上那些路径全不存在，
#   候选表退化成只剩一个兜底）。改为从环境推导，见 figure.py 同处注释。
def _py_candidates() -> tuple[str, ...]:
    out: list[str] = [sys.executable]
    bases: list[Path] = []
    la = os.environ.get("LOCALAPPDATA")
    if la:
        bases.append(Path(la) / "Programs" / "Python")
    bases.append(Path.home() / "AppData" / "Local" / "Programs" / "Python")
    for base in bases:
        if not base.is_dir():
            continue

        def _ver(p: Path) -> tuple[int, ...]:
            m = re.match(r"Python(\d)(\d+)$", p.name)
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        try:
            subs = sorted((d for d in base.iterdir() if d.is_dir()),
                          key=_ver, reverse=True)
        except OSError:
            continue
        for d in subs:
            for exe in ("python.exe", "python"):
                out.append(str(d / exe))
    out.extend([r"C:\Python312\python.exe", "python3", "python"])
    seen: set[str] = set()
    uniq = []
    for c in out:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return tuple(uniq)


_PY_CANDIDATES = _py_candidates()

#: 转换所需的模块名。**只列真正要 import 的那个** ——
#: `pymupdf4llm` 会自动带起 `pymupdf`，多列一个只会多一次误判机会。
_NEEDED_MODULES = ("pymupdf4llm",)

_probe_cache: dict[str, bool] = {}


# ══════════════════════════════════════════════════════════════════
# 子进程里真正执行的转换器（**只用标准库 + pymupdf4llm**）
# ══════════════════════════════════════════════════════════════════
# 为什么把代码写成字符串、在子进程里 `exec`：
# 与本仓 `figure.py` / `deck.py` 的做法一致 —— 被调用侧需要**另一个解释器**，
# 所以它必须是一段能独立跑起来的东西，而不是本进程内的函数调用。
#
# ⚠️ 它**必须**重新配置 stdout 为 UTF-8：Windows 上 Python 接管道时默认 cp936，
# 遇到 `©` 这类字符会抛 UnicodeEncodeError 并**丢掉整段输出**
# （本仓 cli.py::force_utf8 早就为这个写过注释，本次实测又撞了一次）。
_CONVERT_SCRIPT = r'''
import json, sys, time, re
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ⚠️ 载荷从 **argv[1]** 读，不从 stdin。
# 第一版走的是 `sys.stdin.read()`，而父进程当时**没有把载荷写进 stdin**
# ⇒ 子进程读到空串 ⇒ `json.loads("")` 抛 JSONDecodeError ⇒
# 整个转换"失败"但原因看起来像"解析问题"，而不是"你没喂参数"（实测抓到）。
# 走 argv 还有个附带好处：没有管道，就不必操心 stdin 的编码与关闭时序。
payload = json.loads(sys.argv[1])
try:
    import pymupdf, pymupdf4llm
except Exception as e:
    print(json.dumps({"ok": False,
                      "error": "缺少依赖：%s: %s" % (type(e).__name__, e),
                      "hint": "在解释器 %s 上装：pip install pymupdf4llm" % sys.executable},
                     ensure_ascii=False))
    sys.exit(0)

src = payload["src"]
out = payload.get("out") or ""
kwargs = {"use_ocr": bool(payload.get("use_ocr")),
          "write_images": bool(payload.get("images"))}
try:
    doc = pymupdf.open(src)
except Exception as e:
    print(json.dumps({"ok": False,
                      "error": "打不开 PDF：%s: %s" % (type(e).__name__, e)},
                     ensure_ascii=False))
    sys.exit(0)

t0 = time.time()
try:
    md = pymupdf4llm.to_markdown(doc, **kwargs)
except Exception as e:
    print(json.dumps({"ok": False,
                      "error": "转换失败：%s: %s" % (type(e).__name__, e)},
                     ensure_ascii=False))
    sys.exit(0)
dt = time.time() - t0

if out:
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": "写不出 md：%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        sys.exit(0)

print(json.dumps({
    "ok": True,
    "chars": len(md),
    "pages": doc.page_count,
    "seconds": round(dt, 2),
    "used_ocr": kwargs["use_ocr"],
    "wrote_images": kwargs["write_images"],
    "out": out,
    # 正文**只有当父进程明确要**（need_text）时才随回包回来。
    # 为什么不总是回：正文可达几十万字符，而回包要经 stdout 走一遍管道，
    # 且父进程多半只是要"落个盘 + 看统计"。要正文的只有 kb_to_obsidian
    # 那一条（它要把 md 直接交给 to_obsidian，不落临时文件）。
    "text": md if payload.get("need_text") else "",
    # 只回**结构统计**，不回正文：正文可能几十万字符，
    # 喂给 stdout 会让父进程读到一大坨（本仓 K 型教训：工具自带 cwd/capture
    # 会把产物搬去别处；这里同理，回包不是通道）。
    #
    # ⚠️ 标题计数用 **re** 数行首的 `#`（1~6 级），不用 `md.count("\n# ")`。
    # 第一版就是后者，实测把这份 21 页论文数成 **headings=1** ——
    # 因为它的正文标题是二级三级（`## `），而那个写法只认一级。
    # 一个被低报 18 倍的结构指标会让人误判"转换丢了层级"
    # （真实值：19 个标题），**假指标比没有指标更坏**。
    "headings": len(re.findall(r"^#{1,6} ", md, re.M)),
    "table_pipes": md.count("|"),
}, ensure_ascii=False))
'''


def _run(cmd: list[str], cwd: str | None, timeout: float = 600.0) -> subprocess.CompletedProcess:
    """跑子进程并完整拿到输出。逐条对齐 `figure.py::_run` 的既有教训。"""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    # ★ 关键：让子进程的 stdio 走 UTF-8，而不是 cp936。
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict[str, Any] = {
        "cwd": cwd, "env": env, "capture_output": True,
        "timeout": max(5.0, float(timeout)),
    }
    if os.name == "nt":
        # 只用 CREATE_NO_WINDOW 消控制台，**不要**再传 `windowsHide`
        # —— 它不是 Popen 参数，传了直接 TypeError（figure.py 实测记过）。
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, **kwargs)


def _interpreter_ok(exe: str, modules: tuple[str, ...] = _NEEDED_MODULES) -> bool:
    """这个解释器有 pymupdf4llm 吗？（结果进缓存，探一次就够）

    ⚠️ 必须 `import importlib.util` —— `import importlib` 在子进程里
    `importlib.util` 尚未绑定，会抛 AttributeError → 退出码 1，
    于是**每个解释器都被判成不能用**，整条链静默失效（figure.py 实测踩过）。
    """
    key = f"{exe}|{','.join(modules)}"
    if key in _probe_cache:
        return _probe_cache[key]
    code = ("import importlib.util, sys\n"
            "missing = [m for m in %r if importlib.util.find_spec(m) is None]\n"
            "sys.exit(1 if missing else 0)\n" % (list(modules),))
    ok = False
    try:
        r = _run([exe, "-c", code], cwd=None, timeout=90)
        ok = r.returncode == 0
    except Exception:
        ok = False
    _probe_cache[key] = ok
    return ok


def find_convert_python(explicit: str | None = None,
                        extra: tuple[str, ...] = ()) -> str | None:
    """找一个装了 `pymupdf4llm` 的解释器。

    顺序：显式配置 → 额外候选 → 本进程自己 → 已知常见路径。
    **找不到返回 None** —— 调用方必须如实报卡点，
    不许降级成"跳过转换并当它转过了"（本仓 I 型：审不了 ≠ 通过）。
    """
    seen: set[str] = set()

    def _try(exe: str | None) -> str | None:
        if not exe or exe in seen:
            return None
        seen.add(exe)
        if not Path(exe).exists():
            return None
        return exe if _interpreter_ok(exe) else None

    order = ([explicit.strip()] if explicit and explicit.strip() else []) \
        + list(extra) + [sys.executable] + list(_PY_CANDIDATES)
    for cand in order:
        got = _try(cand)
        if got:
            return got
    return None


# ══════════════════════════════════════════════════════════════════
# 结果
# ══════════════════════════════════════════════════════════════════

@dataclass
class Conversion:
    """一次转换的结果。

    ★ `verdict` 用词**与本仓 GateResult 同族**：`OK` / `FAILED` / `SKIPPED`。
    `SKIPPED` 专门表示"依赖没找到，所以**没转**"——
    它**不是成功**。把"没做"渲染成"做好了"是本仓反复记的失败方式。
    """
    verdict: str = "SKIPPED"          # OK / FAILED / SKIPPED
    src: str = ""
    out: str = ""
    chars: int = 0
    pages: int = 0
    seconds: float = 0.0
    used_ocr: bool = False
    wrote_images: bool = False
    headings: int = 0
    table_pipes: int = 0
    python_exe: str = ""
    error: str = ""
    hint: str = ""
    notes: list[str] = field(default_factory=list)
    #: 转换出的 Markdown 正文（**内存里传给调用方，不进 stdout、不进 to_dict**）。
    #: 为什么不落临时文件再读回来：本仓对内核包的要求是**零删除调用**
    #: （hygiene_test 逐文件扫），而"落一份再删掉"既需要一个删除调用，
    #: 又白给"删失败/删错对象"留口子。`to_obsidian` 收的就是字符串，
    #: 那就没有理由经过磁盘。
    #: ⚠️ `to_dict()` **刻意不包含它** —— 回包是给模型读的紧凑载荷，
    #: 正文几十万字符塞进去会把上下文一次撑爆（本仓量过：单次 schema 就 5 万字符）。
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "OK"

    @property
    def chars_per_page(self) -> float:
        return (self.chars / self.pages) if self.pages else 0.0

    def to_dict(self) -> dict:
        """回包用的紧凑字典。

        ★ **必须显式排除 `text`** —— 不能用 `asdict()` 一把梭。
        血债：第一版就是 `asdict(self)`，而 dataclass 的字段里包含 `text`，
        于是那一份 **153,424 字符的正文会原样进回包**，
        而回包是每步都要发给模型的东西 ⇒ **一次把上下文撑爆**。
        本仓量过这个代价：单是工具 schema 就有 5 万字符，
        而这是它的 3 倍，且**不会报错**，只会让后面的调用悄悄变贵、变蠢。
        这条由 `kb_convert_test` 的「承重自检：to_dict 里没有 text」钉住。
        """
        d = asdict(self)
        d.pop("text", None)
        d["ok"] = self.ok
        d["chars_per_page"] = round(self.chars_per_page, 1)
        return d


#: 每页低于这么多字符 ⇒ 提示"可能是扫描件，考虑开 OCR"。
#: ⚠️ 这是**提示阈值，不是判定**。取 200 的依据：正常英文双栏论文每页
#: 实测约 7,300 字符（153,402 / 21），差一个数量级才提示，避免误报
#: （本仓定律：误报比漏报更伤 —— 它教人无视整份报告）。
_OCR_HINT_CPP = 200.0


def ocr_advice(conv: Conversion) -> str:
    """按**实测的文字密度**提示要不要开 OCR。只提示，不自动重跑。

    为什么不自动重跑：自动重跑会让一次**慢 2.2 倍**的转换在用户不知情时发生，
    而在"网络/算力"这类问题上，把可控变成不可控是更坏的取舍。
    """
    if not conv.ok:
        return ""
    # ★ 没有页数 ⇒ **判不了**，不许提示。
    # 血债：第一版只判 `cpp < 阈值`，而 `pages=0` 时 `chars_per_page` 恒为 0.0
    # ⇒ 它会理直气壮地说"每页只有 0 字符，很可能是扫描件"。
    # 那是把**「我不知道」渲染成了「我查出来它有问题」**（F 型缺陷，
    # 与 pricing 那边"UNKNOWN 不许写成 0"同源）。
    # 触发它的路径很平常：转换写了 md 却漏回 pages，或 0 页的破损 PDF。
    if conv.pages <= 0:
        return ""
    cpp = conv.chars_per_page
    if cpp < _OCR_HINT_CPP:
        return (f"每页只有 {cpp:.0f} 字符（正常期刊论文约 7,300）——"
                f"**这份 PDF 很可能是扫描件**（没有文字层）。"
                f"建议用 `use_ocr=True` 重转一次；代价是约 2.2 倍耗时。"
                f"⚠️ 本工具**不会自动替你重跑** —— 重跑由你决定。")
    return ""


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

class PdfConverter:
    """把 PDF 转成 Markdown。用法与 `FigureAuditor` / `DeckAuditor` 同构。

        cv = PdfConverter(python_exe=...)
        c = cv.convert("paper.pdf", out="paper.md")
        if not c.ok: ...        # c.error / c.hint 里是原因
    """

    def __init__(self, python_exe: str | None = None, timeout: float = 600.0,
                 use_ocr: bool = False, images: bool = False):
        self._python_exe = python_exe
        self.timeout = timeout
        self.use_ocr = use_ocr
        self.images = images
        self.notes: list[str] = []
        self._py: str | None = None

    @property
    def python_exe(self) -> str | None:
        if self._py is None:
            self._py = find_convert_python(self._python_exe)
            if self._py is None:
                self.notes.append(
                    "找不到装了 pymupdf4llm 的解释器。"
                    "请在 config.json 的 kb.python 里指定一个绝对路径，"
                    "或用它装一次：pip install pymupdf4llm")
        return self._py

    def available(self) -> bool:
        return self.python_exe is not None

    def convert(self, src: str | Path, out: str | Path | None = None,
                use_ocr: bool | None = None,
                images: bool | None = None,
                need_text: bool = False) -> Conversion:
        """转一份 PDF。`out` 为空则**只转不落盘**（用于探路/统计）。

        `need_text=True` 时把正文一并带回 `Conversion.text`
        （给 `kb_to_obsidian` 用，免得落一份临时文件再删）。

        依赖缺失时返回 `verdict="SKIPPED"` 并带 `hint` ——
        **绝不静默返回一个空结果**（空结果会被读成"这篇没内容"）。
        """
        src_p = Path(src)
        conv = Conversion(
            src=str(src_p),
            used_ocr=self.use_ocr if use_ocr is None else bool(use_ocr),
            wrote_images=self.images if images is None else bool(images),
        )
        if not src_p.exists():
            conv.verdict, conv.error = "FAILED", f"文件不存在：{src_p}"
            return conv
        if src_p.suffix.lower() != ".pdf":
            conv.verdict = "FAILED"
            conv.error = f"只转 PDF，收到的是 {src_p.suffix or '（无扩展名）'}"
            return conv

        exe = self.python_exe
        conv.python_exe = exe or ""
        if exe is None:
            conv.verdict = "SKIPPED"
            conv.error = "找不到装了 pymupdf4llm 的解释器"
            conv.hint = ("在那台解释器上执行 `pip install pymupdf4llm`，"
                         "或在 config.json 的 kb.python 里写明解释器绝对路径。"
                         "⚠️ 本次**没有转换**，产物不存在。")
            conv.notes = list(self.notes)
            return conv

        payload = {"src": str(src_p), "out": str(out) if out else "",
                   "use_ocr": conv.used_ocr, "images": conv.wrote_images,
                   "need_text": bool(need_text)}
        # ★ 输出目录**必须由父进程先建出来**。
        # 实测（本机）：不建目录时子进程会以
        # `FileNotFoundError: ... '_kb_probe\npj2024.md'` 失败 ——
        # 而那句报错看起来像"路径拼错了"，真实原因却是"目录不存在"，
        # 属于最难查的那一类（说的和做的不是一回事）。
        # 在这里显式建，并且**失败要如实报到明面**，不留给子进程去猜。
        if out:
            try:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
            except Exception as e:                   # noqa: BLE001
                conv.verdict = "FAILED"
                conv.error = (f"建不了输出目录 {Path(out).parent}："
                              f"{type(e).__name__}: {e}")
                return conv
        try:
            # 载荷走 argv（不是 stdin）——理由见 `_CONVERT_SCRIPT` 里的注释。
            r = _run([exe, "-c", _CONVERT_SCRIPT,
                      json.dumps(payload, ensure_ascii=False)],
                     cwd=str(src_p.parent), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            conv.verdict = "FAILED"
            conv.error = f"转换超时（>{self.timeout:.0f}s）"
            conv.hint = "大文件可调大 kb.timeout；或先用 use_ocr=False 快路。"
            return conv
        except Exception as e:                       # noqa: BLE001
            conv.verdict = "FAILED"
            conv.error = f"起子进程失败：{type(e).__name__}: {e}"
            return conv

        # ⚠️ 这里原来写着"stdin 用 input= 最稳" —— 那是**注释跟着第一版实现
        # 一起漂了**：改成 argv 之后这句已经不成立，留着它会误导下一个人
        # 去重新引入管道。删掉，只留真正发生的事。
        raw = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        if not raw:
            conv.verdict = "FAILED"
            conv.error = "转换器没有输出"
            conv.hint = ((r.stderr or b"").decode("utf-8", errors="replace")[:600]
                         or "（stderr 也是空的）")
            return conv
        last = raw.splitlines()[-1]
        try:
            data = json.loads(last)
        except Exception:
            conv.verdict = "FAILED"
            conv.error = "转换器输出不是合法 JSON"
            conv.hint = raw[-600:]
            return conv

        if not data.get("ok"):
            conv.verdict = "FAILED"
            conv.error = str(data.get("error") or "未知失败")
            conv.hint = str(data.get("hint") or "")
            return conv

        conv.verdict = "OK"
        conv.out = str(data.get("out") or "")
        conv.chars = int(data.get("chars") or 0)
        conv.pages = int(data.get("pages") or 0)
        conv.seconds = float(data.get("seconds") or 0.0)
        conv.headings = int(data.get("headings") or 0)
        conv.table_pipes = int(data.get("table_pipes") or 0)
        conv.text = str(data.get("text") or "")
        # ★ 要了正文却没拿到 ⇒ **这是失败，不是"这篇没内容"**。
        # 静默放行会让 kb_to_obsidian 往 vault 里写一份**空的**笔记，
        # 而那看起来完全像一次成功（本仓 I 型：把"没做"渲染成"做好了"）。
        if need_text and not conv.text:
            conv.verdict = "FAILED"
            conv.error = "要求回传正文，但转换器回了空正文"
            conv.hint = "这是内核/子进程之间的契约破裂，不是 PDF 的问题。"
            return conv
        adv = ocr_advice(conv)
        if adv:
            conv.notes.append(adv)
        return conv

    def convert_many(self, srcs: list[str | Path], out_dir: str | Path,
                     use_ocr: bool | None = None,
                     images: bool | None = None) -> list[Conversion]:
        """批量转，落进 `out_dir`（同名 `.md`）。

        逐份独立、**一份失败不影响其余** —— 一份坏 PDF 不该让整批停下。
        每份的结果都在返回里，失败原因**一条不吞**。
        """
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        out: list[Conversion] = []
        for s in srcs:
            p = Path(s)
            out.append(self.convert(p, out=d / (p.stem + ".md"),
                                    use_ocr=use_ocr, images=images))
        return out


# ══════════════════════════════════════════════════════════════════
# Obsidian：vault 就是一个装满 md 的目录
# ══════════════════════════════════════════════════════════════════
# ★ 设计上刻意**不假设任何插件存在**。
#   本会话查过：Zotero / Obsidian 在本机**都没装**（`%APPDATA%\Zotero`、
#   `%APPDATA%\obsidian` 均不存在），而"Zotero Integration / Local REST API /
#   Obsidian MCP"这些桥接件**一个都没核实过**（是否存在、是否还维护、怎么鉴权）。
#   ⇒ **不能把功能建在假设存在的第三方插件上。**
#   Obsidian vault 的本质就是**一个目录里的 .md 文件** —— 直接读写文件
#   是最稳、最少依赖、也最不容易被上游改动打断的形状。

_FM_FIELDS = ("title", "authors", "year", "journal", "doi", "tags", "source")


def yaml_frontmatter(meta: dict) -> str:
    """拼一段 YAML frontmatter。**只写真的有的字段**，不填占位符。

    为什么重要：Obsidian 的 Dataview 之类插件按 frontmatter 建索引，
    一个 `doi: TODO` 会被当成**真实值**索引进库 —— 假数据比缺字段更坏。
    """
    lines: list[str] = []
    for k in _FM_FIELDS:
        v = meta.get(k)
        if v in (None, "", [], {}):
            continue                       # 没有就**不写**，不写空值
        if isinstance(v, (list, tuple)):
            v = [str(x).strip() for x in v if str(x).strip()]
            if not v:
                continue
            lines.append(f"{k}:")
            lines += [f"  - {_yaml_scalar(x)}" for x in v]
        else:
            lines.append(f"{k}: {_yaml_scalar(str(v))}")
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _yaml_scalar(s: str) -> str:
    """需要引号就加引号。**: 与 # 在 YAML 里是结构字符**，
    标题里的冒号不引会让整份 frontmatter 解析失败（而 Obsidian 不报错）。"""
    s = s.strip()
    if not s:
        return '""'
    need = (s[0] in "-?:,[]{}#&*!|>'\"%@`") or (": " in s) or (" #" in s) \
        or s.endswith(":") or ("\n" in s)
    if need:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def safe_note_name(name: str) -> str:
    """把标题洗成 Obsidian 里能用的文件名。

    ⚠️ Windows 禁用字符 + 路径分隔符都要处理；**空结果必须兜底** ——
    标题全是禁用字符时得到一个空文件名，会静默写到目录本身上去。
    """
    bad = '<>:"/\\|?*'
    out = "".join(("_" if c in bad or ord(c) < 32 else c) for c in (name or ""))
    out = " ".join(out.split()).strip(" .")
    for suffix in (".",):
        if out.endswith(suffix):
            out = out[:-len(suffix)].strip()
    return out[:120] or "untitled"


def to_obsidian(md: str, meta: dict, vault: str | Path,
                folder: str = "", name: str = "") -> dict:
    """把一份 Markdown 落进 Obsidian vault（**就是写一个 .md 文件**）。

    ★ 关于图：本模块默认 `images=False`（不导出图片、md 里也不留图链接）。
    如果 vault 里想看图，**那是另一个问题**，需要显式 `images=True` 重转 ——
    因为 "知识库要喂给 AI 的文字" 与 "给人在 vault 里翻阅的稿" 对图片的
    需求**正好相反**（前者图是噪音，后者图是内容）。
    用同一个默认值糊过去必然有一边不满意，所以这里**把选择留给调用方**，
    并把这件事写在返回值里（`images_note`）。
    """
    v = Path(vault)
    if not v.exists():
        return {"ok": False, "error": f"vault 目录不存在：{v}",
                "hint": "先确认 Obsidian 库路径（Obsidian 里「打开库」的位置），"
                        "再把它填进 config.json 的 kb.obsidian_vault。"}
    target_dir = v / folder if folder else v
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": f"建不了目录 {target_dir}：{type(e).__name__}: {e}"}

    note = safe_note_name(name or meta.get("title") or "untitled")
    target = target_dir / f"{note}.md"
    body = yaml_frontmatter(meta) + md

    existed = target.exists()
    # ⚠️ **绝不静默覆盖**：本仓在 kb_upload 那边为同名文件专门做过字节比对，
    # 理由是"覆盖 = 静默换掉一份内容，且不报错"。这里沿用同一纪律。
    if existed:
        try:
            if target.read_text(encoding="utf-8", errors="replace") == body:
                return {"ok": True, "path": str(target), "written": False,
                        "note": "与已存在的那份**内容完全相同** ⇒ 没有重写。",
                        "same_file": True}
        except Exception:
            pass
        stem, i = target.stem, 2
        while i < 1000:
            cand = target_dir / f"{stem} ({i}).md"
            if not cand.exists():
                target = cand
                break
            i += 1
        else:
            return {"ok": False, "error": "同名笔记太多，换个标题"}

    try:
        target.write_text(body, encoding="utf-8")
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": f"写不进去 {target}：{type(e).__name__}: {e}"}

    # 写完**读回来核**（本仓老规矩：自称成功永不算证据）。
    try:
        back = target.read_text(encoding="utf-8")
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "path": str(target),
                "error": f"写后回读失败：{type(e).__name__}: {e}",
                "hint": "文件可能写了，但**没有证据** —— 请自己去盘上核。"}
    if back != body:
        return {"ok": False, "path": str(target),
                "error": "写后回读**内容不一致**",
                "hint": f"盘上 {len(back)} 字符，应交 {len(body)} 字符。"}

    return {"ok": True, "path": str(target), "written": True,
            "chars": len(body), "replaced_new_name": existed,
            "images_note": ("本次未导出图片（默认）—— vault 里若是想看图的结构图/结果图，"
                            "需用 images=True 重转一次。") if not meta.get("_images")
            else "",
            "note": ("同名笔记已存在且内容不同 ⇒ 存成了新名字，**没有覆盖**原来那份。"
                     if existed else "")}


# ══════════════════════════════════════════════════════════════════
# 便利入口：给工具层用
# ══════════════════════════════════════════════════════════════════

def convert_pdf(src: str, out: str = "", use_ocr: bool = False,
                images: bool = False, python_exe: str = "",
                timeout: float = 600.0) -> dict:
    """一步到位：转一份 PDF，可选落盘。返回**给模型看的紧凑 JSON**。

    刻意返回 dict 而不是 Conversion 对象：工具层要的是可序列化的回包，
    而 `Conversion.to_dict()` 里已经带了 `verdict` 与 `notes`。
    """
    cv = PdfConverter(python_exe=python_exe or None, timeout=timeout,
                      use_ocr=use_ocr, images=images)
    t0 = time.time()
    c = cv.convert(src, out=out or None, use_ocr=use_ocr, images=images)
    d = c.to_dict()
    d["elapsed"] = round(time.time() - t0, 2)
    if c.verdict == "SKIPPED":
        d["note"] = ("**没有转换**（依赖缺失）—— 产物不存在。"
                     "不要把这次结果当成'这篇 PDF 是空的'。")
    return d
