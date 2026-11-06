# -*- coding: utf-8 -*-
"""荣珂 Web 控制台 —— 服务端。

零依赖：只用标准库 `http.server` + SSE，不引入任何 web 框架、不需要 npm/构建。
和你原有的工作台 web 是同一套做法。

形态与工作台不同：工作台是**项目仪表盘**（看状态、点按钮），
荣珂是**agent 控制台**（看它想、它调、门怎么判）——所以核心是**实时事件流**。

一个任务 = 一个线程 + 一个事件队列；前端用 SSE 订阅：

    目标契约 → step → 工具调用 → 证据门判定 → 完成/阻塞

并发：内核自己带串行锁（run() 会 chdir），所以这里允许开线程，但同一时刻
只跑一个任务；有任务在跑时新请求被拒（409），前端如实提示。
"""
from __future__ import annotations

import argparse
import hashlib
import html as _html          # 别名：本文件里已有局部变量 `html`（那是响应正文），不撞名
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import (DEFAULTS, Config, PROVIDER_WRITE_KEYS, data_root,
                      project_root, provider_write_key, save_provider_overrides,
                      save_secret, secrets_path)
from ..loop import Rongke, SNAPSHOT_INTERNAL
from ..memory import ArchivedUsageCache, Memory, archived_usage, default_root
from . import library_api
from .request import RequestError, read_json_object
from .settings import (REASONING_CHOICES, SettingsError,
                        parse_provider_updates, provider_field_specs)
from .. import pricing as _pricing

HERE = Path(__file__).resolve().parent
#: 控制台默认端口。**单源在这里** —— `cli.py --port` 与桌面启动器都从这里取，
#: 原来三处各写一个 8765，改端口要翻三个文件，漏一个就出现"CLI 起的端口
#: 和界面告诉你的端口不一样"这种查起来很贵的错。
DEFAULT_PORT = 8765

# ── 代码指纹：回答"**这个进程**加载的是哪份代码" ─────────────
# 为什么必须有：`ui_shot.py` 连的是**已启动**的控制台，不自己起。
# 于是"改完代码忘了重启服务"时，它会**安静地跑在旧构建上**，
# 而结论算在新代码头上 —— 旧行为报错还算好的，最坏是旧行为报"全绿"。
# 这和"补丁在位 ≠ 症状消失（进程可能启动早于补丁落盘）"是同一个形状。
#
# ★★ 第一版我把它写成"每次请求现读磁盘算 sha1" —— 于是它**永远等于磁盘**，
#    **永远发现不了进程是旧的**。上线当场就闹了笑话：我改完 `server.py` 没重启，
#    它照样报"一致"。这是 **N 型第二种长相：测量探针自己有 bug ⇒ 能力被读成不存在**。
#    探针要回答的是"**这个进程**载入了哪份代码"，所以指纹只能在**加载那一刻**取，
#    不能在提问时取。⇒ `_FROZEN` 冻在 import 时，`code_stale()` 才拿它跟磁盘比。
PKG = HERE.parent                      # `rongke/` 包目录
STARTED = time.time()


def _fingerprint(root: Path) -> dict:
    """给 `root` 下所有源码算指纹（相对路径 → sha1 前 12 位）。"""
    import_fn = hashlib.sha1
    out: dict = {}
    try:
        paths = sorted(root.rglob("*.py"))
    except OSError:
        return out
    for p in paths:
        if "__pycache__" in p.parts or p.name.startswith("_probe"):
            continue                # 测试写的影子副本不算产品代码
        try:
            out[str(p.relative_to(root)).replace("\\", "/")] = \
                import_fn(p.read_bytes()).hexdigest()[:12]
        except OSError:
            continue
    # 前端也算进去：它虽然每次请求现读磁盘（改了不用重启），
    # 但"进程里的 HTML 缓存"一旦存在就会漂，留着一起看更省心。
    idx = HERE / "index.html"
    try:
        out["web/index.html"] = import_fn(idx.read_bytes()).hexdigest()[:12]
    except OSError:
        pass
    return out


_FROZEN = _fingerprint(PKG)            # ← 冻住。这就是"这个进程加载的代码"。


def _license_denial(err: str) -> bool:
    """这条失败原因是「授权」还是「忙」。

    只认授权子系统自己会说的三种话术。`/api/run`、`/api/resume`、`/api/follow`
    共用它决定 403 还是 409 —— 前端据此决定弹激活层还是提示等一会儿。
    """
    return bool(err) and any(k in err for k in ("激活码", "试用", "授权"))


def code_stale(root: Path | None = None) -> list[str]:
    """进程加载的代码 vs 磁盘上的代码，哪些对不上。空列表 = 进程是当前版本。"""
    now = _fingerprint(root or PKG)
    return sorted(k for k in set(_FROZEN) | set(now)
                  if _FROZEN.get(k) != now.get(k))


def build_stamp() -> dict:
    """给截图脚本/前端看的一张小卡片。

    ★ 同时给出 `loaded`（进程加载时的指纹）与 `stale`（自述）。
      为什么要给 `loaded`：`stale` 是**被检查者自己的结论**（R 型：输入由被评者提供）。
      调用方拿 `loaded` 跟**自己算的磁盘指纹**比一遍，就是一条独立锚点 ——
      万一 `code_stale` 本身写坏了，这条路照样能戳穿。
    """
    stale = code_stale()
    return {
        "started": STARTED,
        "n_files": len(_FROZEN),
        "stale": stale,
        "loaded": _FROZEN,
        "why": ("这 %d 个文件在**进程启动之后**被改过，而 Python 不会热重载 —— "
                "你现在连的这个控制台跑的是改动前的代码。" % len(stale)) if stale else "",
    }


# ── 材料区（上传）────────────────────────────────────────────
# 为什么要有：评审第④条「输入输出离科研工作流太远」的**入口**。
# 没有它，用户能提的"真活"只剩"写一段摘要" —— 他手上的稿子、数据、PDF
# 根本递不进来。而第③条承诺的"30 秒跑通第一件真活"也就落不了地。
UPLOAD_MAX = 20 * 1024 * 1024          # 单文件 20MB（够放稿件与数据表，挡掉误传的大包）
# 能**预览**的（纯文本类，零依赖直接读）
UPLOAD_TEXT_EXT = {
    ".md", ".markdown", ".txt", ".text", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".py", ".r", ".js",
    ".tex", ".bib", ".html", ".htm", ".xml", ".svg", ".sql", ".sh", ".ps1",
}
# 收下但**不给预览**的（二进制，解析要重依赖 —— 交给工作台自己的 import_document 工具）。
# 明说"没有预览"，而不是假装读过了（M 型：把"没看"渲染成"看过"）。
UPLOAD_DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods"}
UPLOAD_ALLOWED = UPLOAD_TEXT_EXT | UPLOAD_DOC_EXT

# Windows 保留名：这类名字能写进某些路径 API 却打不开、删不掉，一律改名。
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}
_UPLOAD_PREVIEW_CHARS = 2000

# 知识库收件上限**故意比材料区大**：材料区放的是稿件与数据表（20MB 足够），
# 而文献 PDF 动辄三五十兆 —— 拿稿件的上限去卡 PDF，会让人以为"上传坏了"。
KB_UPLOAD_MAX = 200 * 1024 * 1024


def safe_upload_name(raw: str) -> str:
    """把客户端给的文件名洗成一个**只能落在工作区里**的安全文件名。

    这不是"帮忙转义"，是**围栏的第一道**：文件名是用户可控输入，
    而它会被拼进路径。`../../x` 或者 `C:\\Windows\\x` 一旦被直接 join，
    围栏再严也没用 —— 文件在读它之前就已经写错了地方。

    规矩（每条都对应一种真实的坏法）：
    · 只取 `basename`（同时在 `/` 与 `\\` 上切）—— 挡路径穿越与绝对路径；
    · 去掉控制字符与 Windows 非法字符 `<>:"|?*` —— 挡写不出来的名字；
    · `.` / `..` / 空 → 拒；
    · Windows 保留名（CON/NUL/COM1…）加前缀 —— 这类名字能创建却读不回；
    · 限长 120 —— 挡超长名撑爆路径。
    """
    s = str(raw or "")
    # 两种分隔符都要切：`os.path.basename` 在 Linux 上不认 `\`
    s = s.replace("\\", "/").split("/")[-1]
    s = "".join(ch for ch in s if ord(ch) >= 32 and ch not in '<>:"|?*')
    s = s.strip()
    # 只削**结尾**的点与空格（Windows 建不出这种名字）；
    # 开头的点要留着 —— `.env` / `.gitignore` 是正经文件名，不该被"洗干净"成另一个名字。
    while s.endswith((".", " ")):
        s = s[:-1]
    s = s[:120]
    if not s or s in (".", ".."):
        raise ValueError("文件名不可用")
    stem = s.rsplit(".", 1)[0]
    if stem.lower() in _WIN_RESERVED:
        s = "_" + s
    return s


def ws_dir() -> Path:
    """任务归档根（runs/）。配置坏了也要能降级到默认位置。"""
    try:
        return Config.load().runtime.workspace_dir
    except Exception:
        # 降级到**用户数据根**，不是 project_root()：装成依赖后后者是
        # site-packages，往那儿写任务归档会在升级时丢失、只读环境下直接报错。
        return data_root() / "runs"


# ══════════════════════ 任务管理 ══════════════════════
# 运行中问人的等待上限。默认 300s：够人从"去倒杯水"回来。
# 无人值守跑批把它调短（环境变量，秒）—— 实测 C 场撞 max_steps 后弹确认，
# 没人点就白等满 5 分钟才降级继续，任务挂了 322.8s。
# 不写死成短值：有人在场时 30s 会把「想看一眼再决定」的窗口关掉。
try:
    ASK_TIMEOUT = max(1.0, float(os.environ.get("RONGKE_ASK_TIMEOUT", "300")))
except (TypeError, ValueError):
    ASK_TIMEOUT = 300.0


def mode_from(body: dict) -> str:
    """从请求体里读用户亲手按下的**模式开关**：`"chat"`（任务模式）或 `"answer"`（仅问答）。

    这是用户原话要的那件事 —— 「直接加个任务模式和仅问答不就行了」。
    存在的理由：在它之前，**一轮对话算不算任务**全靠契约起草器去猜，
    而它被要求"绝不添加用户没说过的要求"，对一句问话只能给出 `acceptance=[]`，
    证据门又对空验收标准一律打回 ⇒ 问话永远过不了门（`runs/20261031-085105`：
    31 条 assistant 全是空正文，最后用户叫停）。
    有了这个开关，"这一轮按什么走"是**用户说的**，不是我们猜的。

    口径与 `kb_only_from` 一样，两处入口（`/api/run`、`/api/follow`）共用一个读法：
      · `"answer"` / `"qa"` / `"chat"`（问） ⇒ `"answer"`
      · `"task"` / `"work"` / `"auto"`      ⇒ `"chat"`
      · 缺省 / 认不出                        ⇒ `"auto"`（交给分类器兜底，不擅自定死）
    """
    v = (body or {}).get("mode")
    s = v.strip().lower() if isinstance(v, str) else ""
    if s in ("answer", "qa", "ask", "question"):
        return "answer"
    if s in ("task", "work", "chat", "run"):
        return "task"
    return "auto"


def mode_label(mode: str) -> str:
    """给界面/日志看的名字。**唯一一份**，不许各处各写一版中文。"""
    return {"answer": "仅问答", "task": "任务模式"}.get(mode, "自动")


def kb_only_from(body: dict) -> bool:
    """从请求体里读导师模式开关。**两处入口共用一个读法**（`/api/run` 与 `/api/follow`）。

    为什么单开一个函数而不是两处各写一遍真值判断：两处口径一旦不同，
    "新任务开着、追问变成关"这种半真半假的状态就必然出现（H 型：同一个事实两套判法）。
    规则：`true/1/on/yes`（字符串大小写不敏感）算开；**字段缺席 = 关**，
    不回头猜上一个状态 —— 默认开会悄悄改变所有人的行为。
    """
    v = (body or {}).get("kb_only")
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)


def mentor_from_snapshot(snap: dict | None) -> bool:
    """这场会话**当时**开没开导师模式 —— 唯一说得通的依据是消息栈里那段在不在。

    为什么不看前端传来的值、也不看服务端内存里那个 `Run.mentor`：
    那两者说的是"**此刻的意图**"，不是"**当时的状态**"。服务一重启内存就蒸发，
    而对话快照还在盘上。续跑/追问要接的是后者。
    （内核 `loop._loop` 的 resumed 分支按同一个事实做相反动作：栈里没有而这次要开
    ⇒ 补注入；栈里有而这次要关 ⇒ 补一句撤销。两边共用同一条判据，不各写一套。）
    """
    from ..loop import MENTOR_MARK
    return any(MENTOR_MARK in str(m.get("content") or "")
               for m in ((snap or {}).get("messages") or []))


@dataclass
class Run:
    id: str
    goal: str
    max_steps: int | None = None
    contract: dict | None = None      # 人审过的冻结契约（None = 让内核自动起草）
    # 用户亲手按的模式开关："" / "auto" = 由分类器定，"answer" = 仅问答，"task" = 任务模式。
    # 见 `mode_from` —— 这一项**只降不升**：它能把一轮定死成仅问答，
    # 不能凭空替用户加一条验收标准。
    mode: str = ""
    # ★ 能力预设：这一场会话用哪套判据/门/工具。绑在 Run 上而不是全局，
    #   追问才能从 `prev.preset` 认回父轮那一套（长命单例不重建）。
    preset: str = ""
    events: list[dict] = field(default_factory=list)
    done: bool = False
    result: dict | None = None
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    cond: threading.Condition = field(default_factory=threading.Condition)
    cancel: threading.Event = field(default_factory=threading.Event)
    # ── 跨进程续跑 ──
    resume: dict | None = None         # 从快照接回来的对话（见 POST /api/resume）
    workspace: str = ""                # 非空 = 回到这个工作区跑（续跑用），不是新开一个
    # 有没有**可续跑的对话快照** —— 注意它 ≠ 有没有归档。
    # 跑成功的任务会删掉快照，所以"能回放"的任务大多"不能续跑"，
    # 界面必须能区分这两件事（否则会给出一个点了就报错的按钮）。
    resumable: bool = False
    # ── 导师模式（W22-B）：开关是**会话级**的事实，跟着这一场会话走 ──
    #   ⚠️ 这里存的只是"**这一次请求要开着跑**"的意图，不是"当时开没开"的台账 ——
    #   后者由内核发出的 `mentor_mode` 事件负责（进 SSE、进 `session.json`）。
    #   分清的理由：状态一旦在前端/服务端各存一份，重启后就成了猜的。
    #   默认 `False`：不许默认开（那会悄悄改变所有人的行为，见工单 §1）。
    mentor: bool = False
    # ── 运行中问人（评审第①条）──
    ask: dict | None = None            # 当前挂起的问题；None = 没在等人
    _answer: dict | None = None        # 人给的答复（一次性）
    _ack: threading.Event = field(default_factory=threading.Event)

    def ask_human(self, kind: str, payload: dict,
                  timeout: float = ASK_TIMEOUT) -> dict | None:
        """内核从此处**阻塞等**回答；超时返回 None，内核据此降级为原行为。

        为什么超时返回 None 而不是抛异常：内核的契约是
        「没人应答 ⇒ 按原路继续」。抛异常会把它变成**一次任务失败** ——
        那是把可用性问题升级成可用性事故（人不在工位 ≠ 任务出错）。
        """
        with self.cond:
            self.ask = {"kind": kind, **payload, "timeout": timeout,
                        "t": time.strftime("%H:%M:%S")}
            self._answer = None
            self._ack.clear()
            self.cond.notify_all()          # 让 SSE 立刻把问题推出去
        got = self._ack.wait(timeout)
        with self.cond:
            self.ask = None
            ans, self._answer = self._answer, None
        return ans if got else None

    def respond(self, action: str, text: str = "",
                steps: int | None = None) -> bool:
        """人给的答复。**没有待答问题就返回 False** —— 不许凭空塞一个答案进来。

        为什么要有这道守卫：`/api/intervene` 是公开路由，重复提交/延迟提交
        都会打到这里。若不做检查，第二次提交就会把答案留给**下一个**介入点
        （那张卡还没显示，却已经有答案了）⇒ 用户"未卜先知"。
        """
        with self.cond:
            if not self.ask:
                return False
            ans: dict = {"action": action, "text": text}
            if steps is not None:
                ans["steps"] = steps
            self._answer = ans
            self._ack.set()
            self.cond.notify_all()
            return True

    def push(self, kind: str, data: dict) -> None:
        with self.cond:
            self.events.append({
                "n": len(self.events), "kind": kind, "data": data,
                "t": time.strftime("%H:%M:%S"),
            })
            self.cond.notify_all()

    def finish(self, result: dict | None, error: str = "") -> None:
        with self.cond:
            self.done = True
            self.result = result
            self.error = error
            self.finished = time.time()
            self.cond.notify_all()

    def _followable(self) -> bool:
        """这个任务**现在**能不能「接着说」：它的会话目录里有没有对话快照。

        与 `resumable`（能不能续跑半截任务）口径一致，只是两份快照名字都认：
        成功完成的任务快照已改名（conversation.done.json），追问照样接得回来。
        """
        bases: list[Path] = []
        if self.workspace:
            bases.append(Path(self.workspace))
        bases.append(ws_dir() / self.id)
        return any(
            (b / "conversation.json").is_file()
            or (b / "conversation.done.json").is_file()
            for b in bases)

    def snapshot(self) -> dict:
        with self.cond:
            return {
                "id": self.id, "goal": self.goal, "done": self.done,
                "events": list(self.events), "result": self.result, "error": self.error,
                "elapsed": round((self.finished or time.time()) - self.started, 1),
                # 带上"正在等你回答什么"：否则刷新页面后那张介入卡就消失了，
                # 而内核还在等 —— 界面与内核各说各话（B 型）。
                "ask": dict(self.ask) if self.ask else None,
                # 能否"接着跑" —— 界面靠它决定要不要给那个按钮。
                # 给了却点不通的按钮，比没有按钮更伤信任。
                "resumable": self.resumable,
                # 能否「接着说」（会话内追问，与 resumable 是两件事）
                "followable": self._followable(),
            }


class Manager:
    def __init__(self) -> None:
        self.runs: dict[str, Run] = {}
        self.active: str | None = None
        self._lock = threading.Lock()
        self._hist_lock = threading.Lock()
        self._hist_cache: dict[str, tuple[tuple[int, ...], dict]] = {}
        self._usage_cache = ArchivedUsageCache()
        self._artifact_lock = threading.Lock()
        self._artifact_cache: dict[str, tuple[tuple, dict[str, Path]]] = {}
        self._settings_lock = threading.Lock()
        # 文献库的写入锁：litkb 对 library.json 是整份读-改-写，
        # 两个面板请求（或面板 + 任务）同时写会丢条目。只串行化"写"。
        self._kb_write_lock = threading.Lock()
        self.kernel: Rongke | None = None
        self.kernel_error = ""
        # ── 材料区 ──
        # 用户先递材料、再提目标。落地在 `runs/_inbox/`（`_` 开头的不是任务归档，
        # 历史列表只认有 `session.json` 的目录，所以它不会被当任务列出来）。
        # ★ **提交时把它改名成正式工作区**，而不是"就地当工作区" ——
        #   否则第二个任务会把第一个任务的 `session.json` 覆盖掉（归档路径就是
        #   `runs/<工作区名>/session.json`）。改在同盘上是一次原子 rename，零拷贝。
        self.inbox: Path | None = None
        # ⚠️ **没有 `self.materials` 这个字段** —— 清单是 `materials` property
        #    从目录**算**出来的，不是存下来的。理由见 `scan_materials`。
        self.removed: Path | None = None      # 移除的材料挪这儿（**不删**，见 `removed_dir`）
        self.inbox_leftover: list[str] = []   # 提交时没能搬进工作区的那些（如实留话）
        # ── 账单与会话 ──
        # ★ 必须是**一个长命的实例**，不能每次请求新建一个：
        #   会话令牌只活在 `Billing` 实例的内存里（见 `issue_token`），
        #   每个请求都 `Billing()` 一次的话，登录发出去的令牌下一次请求就
        #   认不出来了 —— 表现是"登录成功但立刻又让你登录"。
        try:
            from ..billing import Billing
            self.billing = Billing()
        except Exception as e:                                # noqa: BLE001
            self.billing = None
            self.billing_error = f"{type(e).__name__}: {e}"
        else:
            self.billing_error = ""
        # ── 授权（离线激活码）──
        # 同样是长命实例：它在内存里缓存不了什么，但每次请求重新读盘没必要。
        try:
            from ..license import License
            self.license = License()
        except Exception as e:                                # noqa: BLE001
            self.license = None
            self.license_error = f"{type(e).__name__}: {e}"
        else:
            self.license_error = ""
        # 试用从**服务第一次起来**开始计时：安装后第一次打开窗口就落
        # first_seen，而不是等用户第一次点「运行」。授权子系统出错时放行。
        if self.license is not None:
            try:
                self.license.state()
            except Exception:                             # noqa: BLE001
                pass
        self.boot()

    def boot(self) -> None:
        try:
            self.kernel = Rongke()
        except Exception as e:
            self.kernel = None
            self.kernel_error = f"{type(e).__name__}: {e}"

    # ── 材料区（上传）───────────────────────────────────────
    def inbox_dir(self) -> Path:
        """材料区目录（惰性创建）。"""
        if self.inbox is None:
            self.inbox = ws_dir() / "_inbox"
            self.inbox.mkdir(parents=True, exist_ok=True)
        return self.inbox

    def scan_materials(self) -> list[dict]:
        """材料清单 —— **事实来源是磁盘，不是内存**。

        为什么非这么写：清单原来存在进程里的一个 `list` 上，于是
        **控制台一重启，用户递进来的材料还在 `_inbox` 躺着、界面却显示"空"**
        —— 又一次"界面与事实各说各话"。这不是假想：本轮修 bug 必须重启服务，
        当场就撞上了（盘上 1 份、清单 0 份）。
        改成由目录算出来之后：谁在盘上，谁就在清单里。**进程死活都与清单无关。**
        """
        d = self.inbox_dir()
        out: list[dict] = []
        try:
            entries = sorted(p for p in d.iterdir() if p.is_file())
        except OSError:
            return out
        for p in entries:
            ext = p.suffix.lower()
            if ext not in UPLOAD_ALLOWED:
                continue            # 不是我们收的类型，就不冒充"材料"
            try:
                st = p.stat()
            except OSError:
                continue
            item = {
                "name": p.name,
                "size": st.st_size,
                "path": str(p),
                "t": time.strftime("%H:%M:%S", time.localtime(st.st_mtime)),
                "previewable": ext in UPLOAD_TEXT_EXT,
                "preview": "",
            }
            if ext in UPLOAD_TEXT_EXT:
                # 坏编码不许炸：`errors="replace"` 之后照原样给出去。
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as f:
                        item["preview"] = f.read(_UPLOAD_PREVIEW_CHARS)
                except OSError:
                    item["preview"] = ""
            out.append(item)
        return out

    @property
    def materials(self) -> list[dict]:
        """当前材料清单（每次现算，见 `scan_materials`）。"""
        return self.scan_materials()

    def _license_ok(self) -> tuple[bool, str]:
        """能不能发起新任务。返回 `(能不能, 不能时的原因)`。

        **只有"发起新任务"和"续跑"要过这里**：控制台打开、看历史、
        读已归档的结果、查文献库 —— 一律不受限。被锁在自己文件外面
        既不能提醒人该买，又会让人愤怒。

        授权子系统自己出错时**放行**：把"我算不出授权状态"升级成
        "你用不了"是不可接受的降级。
        """
        lic = getattr(self, "license", None)
        if lic is None:
            return True, ""
        try:
            ok, st = lic.allows_run()
        except Exception:                                  # noqa: BLE001
            return True, ""
        if ok:
            return True, ""
        reason = getattr(st, "reason", "") or "授权无效"
        return False, (f"{reason}。到「授权」页粘贴激活码，"
                       f"或运行 `rongke --license activate <激活码>`。")

    def license_state(self) -> dict:
        """给界面看的授权状态。出错要**如实说**，不是渲染成"已授权"。"""
        try:
            from ..license import machine_id as _machine_id
            mid = _machine_id()
        except Exception:                                  # noqa: BLE001
            mid = ""
        lic = getattr(self, "license", None)
        if lic is None:
            return {"state": "unknown", "ok": False, "customer": "", "plan": "",
                    "expires": 0, "days_left": 0, "machine_id": mid,
                    "reason": "授权子系统没起来",
                    "error": getattr(self, "license_error", "")}
        try:
            return lic.state().to_dict()
        except Exception as e:                             # noqa: BLE001
            return {"state": "unknown", "ok": False, "customer": "", "plan": "",
                    "expires": 0, "days_left": 0, "machine_id": mid,
                    "reason": f"授权状态读不出来（{type(e).__name__}）",
                    "error": str(e)}

    def busy(self) -> str:
        """有任务在跑就返回原因。**上传与清空都要走它** —— 运行中往工作区
        塞文件，等于在 agent 正读目录的时候改目录，两边都不可解释。"""
        with self._lock:
            if self.active:
                cur = self.runs.get(self.active)
                if cur and not cur.done:
                    return "已有一个任务在跑，等它结束再动材料（内核是单任务串行的）。"
        return ""

    # ── 文献知识库（2026-09-22）──────────────────────────────
    # ★ 面板**不实现任何查询逻辑**：一律转发荣珂工具注册表里那几个 `kb_*` 函数。
    #
    # 为什么必须这样：workbench 的 `web/server.py` **早就有** `/api/kb/*` 一整族端点
    # 和「文献知识库」页面 —— 那是给**人**的 UI 层。这里如果照抄一份查询逻辑，
    # 就是本项目反复吃过的那条：**两处实现必然漂成两副面孔**（H 型）。
    # 转发注册表还顺带买到一条性质：面板看到的和 agent 看到的是**同一份**，
    # 于是"面板能查、agent 调不到"这种最难查的分家不可能发生。
    KB_READ_TOOLS = ("kb_list", "kb_search", "kb_read", "kb_rank", "kb_bibtex")
    KB_MORE_TOOLS = ("kb_ask", "kb_compare", "kb_review")
    KB_WRITE_TOOLS = ("kb_add", "kb_ingest", "kb_note")

    def kb_tools(self) -> dict:
        """从工具注册表取 `kb_*` 的函数；取不到就回空字典。

        **取不到 ≠ 库是空的** —— 调用方必须把这两种情况分开说，
        否则用户会看到"0 篇文献"而去重新导入一遍已经存在的库。
        """
        reg = getattr(self.kernel, "registry", None) if self.kernel else None
        if reg is None:
            return {}
        out: dict[str, object] = {}
        for n in self.KB_READ_TOOLS + self.KB_MORE_TOOLS + self.KB_WRITE_TOOLS:
            fn = getattr(reg.get(n), "fn", None) if reg.get(n) is not None else None
            if callable(fn):
                out[n] = fn
        return out

    def topic_tool(self, name: str, **args) -> dict:
        """调一个课题工具（`topic_status` / `topic_use`），把 JSON 回包解析出来。

        走法与 `kb_call` 一模一样：**从内核的注册表取函数**，不在服务端另写一份
        台账读写 —— 否则"当前课题"就有两套真相，而 `topics/CURRENT` 只有一份。
        工具内部失败是 `raise`（不是 ok=false），所以这里必须接住并**如实转成错误**，
        不许转成"空课题"（那会被界面读成"你还没立课题"，而真相可能是"内核没起来"）。
        """
        reg = getattr(self.kernel, "registry", None) if self.kernel else None
        tool = reg.get(name) if reg is not None else None
        fn = getattr(tool, "fn", None) if tool is not None else None
        if not callable(fn):
            return {"ok": False, "error": f"荣珂看不到 {name}",
                    "hint": "课题工具挂在 core 域常驻 —— 取不到说明内核没起来，"
                            "不是「你还没有课题」"}
        try:
            raw = fn(**args)
        except Exception as e:                      # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(str(raw))
        except Exception:                           # noqa: BLE001
            return {"ok": False, "error": "课题工具回包不是合法 JSON",
                    "raw": str(raw)[:800]}

    def tool_forward(self, fn, args: dict) -> dict:
        """调用一个从注册表取到的工具函数，把它的 JSON 回包解析成 dict。

        ★ `kb_call` 与「找文献」那条链**共用这一份**：两处各写一遍「怎么调、
          怎么解析、失败怎么如实报」，早晚漂成两种答案（本项目吃过的 H 型）。
        工具内部失败是 `raise`（不是 ok=false），所以这里必须接住并**如实转成错误**，
        不许转成"空结果"（那会被界面读成"库里没有"，而真相可能是"内核没起来"）。
        """
        try:
            raw = fn(**args)
        except Exception as e:                      # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return {"ok": False, "error": "工具回包既不字典也不是字符串",
                    "raw": str(raw)[:800]}
        try:
            return json.loads(raw)
        except Exception:                           # noqa: BLE001
            return {"ok": False, "error": "工具回包不是合法 JSON", "raw": raw[:1200]}

    def registry_fn(self, name: str):
        """从内核工具注册表取一个函数的**可调用体**；取不到回 None。

        只是"取"，不含任何该工具自己的逻辑 —— 逻辑始终在工具那一头，
        所以面板与 agent 跑的是同一份实现。
        """
        reg = getattr(self.kernel, "registry", None) if self.kernel else None
        tool = reg.get(name) if reg is not None else None
        fn = getattr(tool, "fn", None) if tool is not None else None
        return fn if callable(fn) else None

    def kb_call(self, name: str, **args) -> dict:
        """调一个 `kb_*` 工具，把它的 JSON 回包解析出来。

        工具一律回 JSON 字符串（那是给模型读的紧凑载荷）；解析失败就**如实报原样**，
        不吞掉转成一个"空结果" —— 空结果会被读成"库里没有"。

        ★ 取函数时**先查名单、再问注册表**：`kb_tools()` 只认 `KB_*_TOOLS` 那三张
          名单，而 `kb_index` 是 `bridge.install_workbench` 直接注册的、不在名单里 ——
          只查名单的话，"盘库"这条最省的路会被判成"荣珂看不到"而永远退回，
          并且不报任何错（实测就是这么静默退回的）。名单在前是为了让名单里的工具
          保持原路径；缺失判定不变：两边都没有才是真没挂载。
        """
        fn = self.kb_tools().get(name) or self.registry_fn(name)
        if fn is None:
            return {"ok": False,
                    "error": f"荣珂看不到 {name}",
                    "hint": "workbench 未挂载，或 kb 域被裁剪；"
                            "先看 /api/tools 里有没有 kb_* —— 这是接线断了，不是库空了"}
        return self.tool_forward(fn, args)

    def kb_ai_fact(self) -> dict:
        """workbench 那份 `app_config.json` 里 AI 填了没 —— **只读文件，不猜运行时**。

        只回答「配置文件里 api_key 是否非空」这一个事实，**不声称「AI 能不能用」**
        （直连还取决于网关可达与 key 有效）。读不到就回 unknown，不编一个结论。
        有它，面板才能在"问答/对比/综述"按下之前先说清它们依赖什么。
        """
        cfg = getattr(self.kernel, "cfg", None)
        wb = getattr(getattr(cfg, "runtime", None), "workbench", None) if cfg else None
        if not wb:
            return {"state": "unknown", "why": "拿不到 workbench 目录"}
        p = Path(str(wb)) / "app_config.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:                      # noqa: BLE001
            return {"state": "unknown",
                    "why": f"读不到 {p.name}: {type(e).__name__}", "file": str(p)}
        ai = d.get("ai") or {}
        key = str(ai.get("api_key") or "").strip()
        return {"state": "configured" if key else "missing",
                "file": str(p),
                "base_url": str(ai.get("base_url") or ""),
                "model": str(ai.get("model") or ""),
                "env_names": ["PAPER_WORKBENCH_API_KEY", "PAPER_WORKBENCH_BASE_URL",
                              "PAPER_WORKBENCH_MODEL"],
                # ⚠️ 这三个是**要调模型**的 kb 工具。加新的 AI 依赖工具时这里要跟着改
                #    （它与 kb 工具的实现不在同一处，所以只能靠人守）。
                "ai_dependent": list(self.KB_MORE_TOOLS)}

    def kb_stat(self, library: str = "") -> dict:
        """库的覆盖度 + 「哪些能力现在可用」。给人看的一页。

        ⚠️ `kb_list` 有 limit 上限 ⇒ 条目多时这里只能统计**前 N 条**。
        那就如实写进 `partial`，不把截断过的分布当成全库的分布。
        """
        tools = self.kb_tools()
        if not tools:
            return {"ok": False, "error": "荣珂看不到任何 kb_* 工具",
                    "hint": "workbench 未挂载，或 kb 域被裁剪",
                    "tools": [], "ai": self.kb_ai_fact()}
        cap = 500
        d = self.kb_query("kb_list", library=library, limit=cap, status="")
        if not d.get("ok"):
            return {"ok": False, "error": d.get("error") or "知识库不可用",
                    "raw": d.get("raw"), "hint": d.get("hint"),
                    "tools": sorted(tools), "ai": self.kb_ai_fact()}
        items = d.get("items") or []
        total = int(d.get("total") or 0)
        matched = int(d.get("matched") or 0)
        years: dict[str, int] = {}
        tags: dict[str, int] = {}
        nofull: list[dict] = []
        # 库况和列表**必须用同一套口径**，否则页面上会自己打自己：
        # 实测过的翻车 —— 上面统计卡写"有全文 12"，下面每行却写"只有文本"，
        # 两边说的是同一批文献。所以这里也算一模一样的**盘上事实**（原件在不在），
        # 分成三档回给前端：有原文 / 只有文本 / 仅元数据。
        pdf_dir = Path(str(d.get("library") or "") or self.kb_root(library)) / "pdfs"
        n_pdf = n_txt_only = n_meta = 0
        for it in items:
            y = str(it.get("year") or "").strip()
            if y:
                years[y] = years.get(y, 0) + 1
            for t in (it.get("tags") or []):
                tags[t] = tags.get(t, 0) + 1
            try:
                did = str(it.get("id") or "")
                cands = [pdf_dir / (did + ".pdf")] if did else []
                for key in ("local_path", "path", "pdf_path"):
                    v = it.get(key)
                    if v:
                        cands.append(Path(str(v)))
                has_pdf = any(p.is_file() for p in cands)
            except Exception:
                has_pdf = False
            it["has_pdf"] = has_pdf
            if has_pdf:
                n_pdf += 1
            elif it.get("fulltext"):
                n_txt_only += 1
            else:
                n_meta += 1
            if not it.get("fulltext"):
                nofull.append({"id": it.get("id"), "title": it.get("title"),
                               "year": y, "status": it.get("status")})
        top_years = sorted(years.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        top_tags = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        return {
            "ok": True,
            "library": d.get("library") or "",
            "total": total,
            "matched": matched,
            "scanned": len(items),
            "partial": matched > len(items),
            "partial_note": (f"库共 {matched} 篇，本次只统计了 {len(items)} 篇"
                             if matched > len(items) else ""),
            # ★ 这里曾经有两个不同口径的「有全文」同时回给前端，实测差 23 篇：
            #   kb_list 自报的 fulltext=207（它的口径是「抽过正文」，逐条看还不等于
            #   has_pdf），而盘上数出来的是 n_pdf 29 + n_text_only 201 = 230。
            #   前端首屏概览卡取了前者、文献库页取了后者 —— 也就是说用户点一下导航，
            #   同一个问题会看到两个答案（207 / 230），且不可信的是**首屏第一眼那个**。
            #   上面这段注释本来就写着「必须用同一套口径，否则页面会自己打自己」，
            #   但下一行仍旧把旧口径的值原样吐了出去 —— 注释和实现正好相反。
            #   所以这里把旧口径**改名保留为 fulltext_claimed**（诊断用，不给人看），
            #   而把 `fulltext` 这个名字让给盘上事实，保证任何调用方取到的都是同一个数。
            "fulltext": n_pdf + n_txt_only,          # = 盘上真有正文的（原文 + 只有文本）
            "fulltext_claimed": int(d.get("fulltext") or 0),   # kb_list 自报，口径不同，勿上屏
            # 三档盘上事实（与 /api/kb/list 的 has_pdf 同一口径）：
            "n_pdf": n_pdf,                 # 原件还在，能「看原页」
            "n_text_only": n_txt_only,      # 只有抽出来的文本，原件没留下
            "n_meta": n_meta,               # 只有元数据
            "no_fulltext": nofull[:30],
            "no_fulltext_n": len(nofull),
            "years": top_years,
            "tags": top_tags,
            "tools": sorted(tools),
            "tools_missing": [n for n in (self.KB_READ_TOOLS + self.KB_MORE_TOOLS
                                          + self.KB_WRITE_TOOLS) if n not in tools],
            "ai": self.kb_ai_fact(),
        }

    def kb_dir(self, library: str = "") -> Path:
        """一个库的目录：入库的 PDF 留在这里，同时**它就是这个库的库根**。

        与材料区（`_inbox`）**分开**是有意的：材料区是"这一次任务的材料"，
        提交时会被整体改名搬进工作区；而入库的 PDF 是**库的长期资产**，
        不能被任何一次任务的生命周期带走。好在 `litkb.add_pdf_file` 不复制文件，
        只把 `local_path` 记成原路径 ⇒ 这个目录就是那些 PDF 唯一的家。

        ★ `library` 的语义**只有一个**：**库名**（不是路径；传路径只取末段）。
        为什么必须钉死这一条：这个值要同时喂给两个地方 ——
          ① 这里，决定 PDF 存进哪个目录；
          ② workbench 的 `litkb.set_data_dir()`，那边要的是**库根路径**。
        之前②是**原样透传**的，于是同一个 `library` 在两处被解释成两样东西：
        PDF 落进 `runs/_kb/<名>/`，而引擎被切去了别处（传相对名时甚至会被
        `resolve()` 到进程 CWD）—— 两个"库"各写各的，谁指着谁全靠运气。
        这就是本项目反复吃的 H 型。现在收敛成：**一个库名 → 一条路径 → 两处共用**。
        """
        d = ws_dir() / "_kb"
        if library:
            # 库名来自 query，是可写路径的输入 ⇒ 只取 basename，防 `../` 逃逸。
            # `..` 这类没有名字的输入是客户端错误，不是内部异常。
            name = Path(str(library)).name
            if not name or str(library) in (".", "..") or "/" in str(library) or "\\" in str(library):
                raise RequestError(400, "library 必须是一个库名，不能是路径")
            d = d / safe_upload_name(name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def kb_root(self, library: str = "") -> str:
        """库名 → 喂给 `litkb.set_data_dir()` 的**库根路径**。

        ⚠️ **空库名必须回空字符串**，那是有意义的值：litkb 把空解释成
        "切回默认库"，也就是用户真正在用的那个。**绝不在这里顺手 `resolve()`
        成绝对路径** —— 那会把"默认库"换成一个刚建出来的空目录，
        用户已有的文献一篇都看不见，而且**不报任何错**（最坏的一种失败）。
        这个函数存在的全部理由就是守住这一条。
        """
        return str(self.kb_dir(library)) if library else ""

    def kb_query(self, tool: str, library: str = "", **args) -> dict:
        """面板的读查询入口：库名 → 库根，再转发给工具。

        **端点一律走这里**，别自己去调 `kb_root()` —— 否则每加一个端点都要
        记得翻译一次，迟早漏一个，而漏掉的那个端点会把库悄悄切去别处。
        让"翻译"只有一条路可走，比指望每个调用点都记得更可靠。
        """
        return self.kb_call(tool, library=self.kb_root(library), **args)

    def kb_names(self) -> set:
        """注册表里**现在真的有**哪些 `kb_*` 工具（含荣珂自己的 `kb_index`）。

        与 `kb_tools()` 的区别：那个只认 `KB_*_TOOLS` 那三张名单（是给
        `/api/kb/stat` 报"缺了哪个"用的），而 `kb_index` 不在名单里 ——
        它由 `bridge.install_workbench` 直接注册。查询层要判断"能不能走
        `kb_index`"，问的必须是**注册表**，不是名单：名单漏一个，面板就会
        永远退回较慢的那条路，且没有任何报错。
        """
        names = set(self.kb_tools())
        if self.registry_fn("kb_index") is not None:
            names.add("kb_index")
        return names

    def kb_fetch(self, tool: str, **args) -> dict:
        """`library_api` 的取数口：工具名 + 参数 → 已解析的回包。

        存在的理由只有一个：**"库名 → 库根"这条换算只许有一份**（`kb_query`）。
        查询层（`library_api`）拿到的 `library` 是**库名**（用户/前端说的那个），
        翻译成库根留在这里 —— 它自己不该知道 `kb_dir`/`safe_upload_name` 那一套。

        ★ 参数里**没有** `library` 就不翻译、也不补：`kb_index` 的签名里根本没有
          这个参数（它是荣珂自己的工具，只读默认库）。给它塞一个 `library=""`
          会 TypeError，而 `tool_forward` 会把那个 TypeError 如实转成
          `{"ok": false}` —— 于是"盘库"永远失败，还查不出原因。
        """
        if "library" not in args:
            return self.kb_call(tool, **args)
        lib = str(args.pop("library") or "")
        return self.kb_query(tool, library=lib, **args)

    def kb_upload(self, raw_name: str, data: bytes, library: str = "",
                  no_enrich: bool = False) -> dict:
        """收一份 PDF 并入库（**写入类**：落盘 + 改库）。

        文件名照抄材料区那套（`safe_upload_name`）—— 不另写一套洗法。
        入库走 `kb_ingest` 而不是 `kb_add`：前者带去重，重复喂同一篇会被如实跳过，
        而不是在库里堆第二份。
        `library` 是**库名**（与 `kb_dir` 同一份契约：一个库名 → 一条路径）。
        `no_enrich=True` 跳过联网元数据补全（离线/网络差时的快路）。
        """
        if not data:
            raise ValueError("文件是空的")
        if len(data) > KB_UPLOAD_MAX:
            raise ValueError(f"超过单文件上限 {KB_UPLOAD_MAX // 1024 // 1024}MB")
        name = safe_upload_name(raw_name)
        if Path(name).suffix.lower() != ".pdf":
            raise ValueError(f"知识库只收 PDF，收到的是 {Path(name).suffix or '（无扩展名）'}")
        lock = getattr(self, "_kb_write_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._kb_write_lock = lock
        with lock:
            return self._kb_upload_locked(name, data, library, no_enrich)

    def _kb_upload_locked(self, name: str, data: bytes, library: str,
                         no_enrich: bool) -> dict:
        """`kb_upload` 的临界区：落盘 + 改库不能和别人交叉。"""
        # 收件目录按库分开：换库时 PDF 也跟着分开，否则两个库共用一堆文件、
        # 谁指着谁全靠 local_path 记住 —— 那种账没人对得清。
        d = self.kb_dir(library)
        target = d / name
        # 同名时**先比内容**再决定是"同一篇"还是"恰好同名"：由字节说了算，不由文件名说了算。
        # 为什么必须这一步：荣珂「同名绝不覆盖」会把重传的文件存成 `-2`，而 `kb_ingest`
        # 的路径去重键**只认原路径** ⇒ 换名后那条键必然不命中 ⇒ **同一篇 PDF 会真的
        # 入库第二份**，工具层精心加的三键去重被这一手悄悄绕过。
        # 两处"安全"互相拆台，就是本项目反复吃的 H 型，所以在这里补死。
        same_file = False
        if target.exists():
            try:
                psize = target.stat().st_size
            except OSError:
                psize = -1
            if psize == len(data):
                try:
                    same_file = target.read_bytes() == data
                except OSError:
                    same_file = False
            if not same_file:
                # 内容确实不同：同名不同文，**绝不覆盖**（库里每条都记着自己的
                # local_path，覆盖 = 静默换掉一篇文献，还不会报错）。
                stem, suffix = target.stem, target.suffix
                for i in range(2, 100):
                    cand = d / f"{stem}-{i}{suffix}"
                    if not cand.exists():
                        target = cand
                        break
                else:
                    raise ValueError("同名文件太多，换个名字")
        if not same_file:
            target.write_bytes(data)
        res = self.kb_query("kb_ingest", library=library, paths=str(target),
                            no_enrich=bool(no_enrich))
        out = dict(res) if isinstance(res, dict) else {"ok": False, "raw": str(res)[:400]}
        out["saved_at"] = str(target)
        # ★ `raw_name` 是**外层 `kb_upload` 的参数**，这个临界区拿到的是已经洗过的
        #   `name`。这里原本写 `str(raw_name or "")` —— NameError，上传 PDF 入库
        #   一律 500（kb_e2e_test 报 `name 'raw_name' is not defined`）。
        #   `requested_name` 的语义是"用户给的原名"，所以如实回洗过的 `name`：
        #   洗名只做替换非法字符，同一个 PDF 的可见名字仍然是它自己。
        out["requested_name"] = str(name or "")
        out["stored_name"] = target.name
        if same_file:
            # 如实说明"没有重写"，否则用户会以为盘上刚落了第二份文件。
            out["same_file"] = True
            out["same_note"] = ("同名文件的字节与已存的那份完全相同 ⇒ 判定为同一篇，"
                                "**没有重写、也没有新建文件**；"
                                "库那边按「路径已在库中」跳过（added=0 是正确结果）")
        elif out["stored_name"] != name:
            # 同名、内容却不同 —— 最像"重传了一份修订稿"。
            # ★ 这里**刻意什么都不替用户做**：不覆盖（覆盖 = 静默换掉一篇文献）。
            #   能做的、也该做的只有一件：**把话说全**，而且要说**实际发生的那件**。
            out["renamed_note"] = ("同名文件已存在且**内容不同**，本次存成了新名字"
                                   "（**没有覆盖**原来那一份）")
            out["same_name_new_content"] = True
            skips = [s.get("skip") for s in (out.get("skipped_items") or [])]
            if skips:
                out["skipped_by"] = skips
            # ⚠️ 措辞必须**由结果推**，不许由我的假设推。
            #   我第一版是拍脑袋写的（"要让新版进库请改名再传"），而实际行为是
            #   **新版已经单独入库了** —— 因为改名后文件名变了，
            #   工具层的标题判重（拿文件名 stem 归一）自然不命中。
            #   一句与实际相反的说明比不说更坏：它会让人去重复一趟已经完成的操作。
            if int(out.get("added") or 0) > 0:
                out["note"] = (
                    "同名但内容不同：新版已存成新名字，并**单独入库为一条新条目**"
                    "（原件没有被覆盖）。⚠️ 库里现在**同时有旧版与新版两条**，"
                    "标题都取自文件名 —— 引用/综述前请核对要哪一条；"
                    "要清掉旧的那条，先 `kb_list` 拿到 id 再决定（**删除不可逆，不替你删**）。")
            else:
                out["note"] = (
                    "同名但内容不同：新版已存成新名字，但这一轮**没有入库**"
                    + (f"（跳过原因：{'；'.join(str(s) for s in skips)}）" if skips else "")
                    + "。⇒ **索引里仍是旧版**。要让新版进库：换个文件名再传，"
                      "或先核对并处理掉旧条目（**删除不可逆，不替你删**）。")
        return out

    # ── 「让 AI 找文献」：检索 → 列候选 → 用户勾选 → 才入库（W23-B）──
    # ★ 本刀**不实现任何检索或下载**：
    #   检索转 `literature_search`，入库转 `kb_add(src="doi:…")` —— 就是 agent
    #   自己用的那两个工具（同一批注册表函数）。服务端只做传输、比对、如实报数。
    #   为什么不是"开一个 agent 任务"：那条链要的是一张**能勾选**的表，
    #   而任务回的是散文；把候选交给模型复述一遍，等于多一次会改写事实的转述。
    #   为什么不是"自己写下载"：那是第二套下载真相（本项目老的
    #   「两个读法早晚对同一份稿给出两种答案」）。
    LIT_FIND_TOOL = "literature_search"
    LIT_FIND_MAX = 40
    LIT_PICK_MAX = 20

    @staticmethod
    def _doi_key(x: object) -> str:
        """DOI 归一成比对键：大小写、`doi:` 前缀、各种 doi.org URL、尾斜杠。

        ★ 2026-10-29：原先只去 `doi:` 与尾斜杠，而库里/检索结果常带
          `https://doi.org/...`。同一个 DOI 两种写法 ⇒ 候选标「库里没有」、
          勾下去又被 `kb_add` 判「已存在」—— 用户看到的是"入库坏了"。
          这里只归一"同一个 DOI 的不同外衣"，不改 DOI 本身。
        """
        s = str(x or "").strip().lower()
        for pre in ("https://doi.org/", "http://doi.org/",
                    "https://dx.doi.org/", "http://dx.doi.org/",
                    "doi.org/", "dx.doi.org/", "doi:"):
            if s.startswith(pre):
                s = s[len(pre):]
                break
        return s.strip().strip("/")

    @staticmethod
    def _plain(s: str) -> str:
        """把标题/期刊名里的标记清成纯文本：解实体、去 `<i></i>` 这类成对标签、压空白。

        ★ 为什么必须做这一道：Crossref 把物种名包在 `<i> </i>` 里原样带回来（实测，
          如 `... Production in<i> Hirsutella sinensis</i> ...`）。前端 `esc()` 会
          把这两个尖括号转义回**字面文本** ⇒ 候选表里读起来就是坏字符。
          斜体排不出（那是表格，不是稿件），但**不该露出标签**。
        """
        t = _html.unescape(str(s or ""))
        t = re.sub(r"</?(?:i|em|b|strong|sub|sup|span)>", "", t, flags=re.I)
        return re.sub(r"\s+", " ", t).strip()

    def library_doi_map(self, library: str = "", cap: int = 500) -> tuple[dict, str]:
        """库里的 DOI → 条目（给候选标「库里已有」用）。回 `(映射, 如实说明)`。

        ★ 读不到映射时**必须带出来说**：那等于「库里有没有这篇」这一列现在答不了，
          默默当成"库里没有"会让人勾第二遍、甚至在库里堆第二份。
        """
        d = self.kb_query("kb_list", library=library, limit=cap, status="")
        if not d.get("ok"):
            return {}, ("读不到库清单（" + str(d.get("error") or "未知原因")
                        + "）⇒ 这一列的「库里已有」现在判不了，别当成库里没有")
        items = d.get("items") or []
        m: dict[str, dict] = {}
        no_doi = 0
        for it in items:
            k = self._doi_key(it.get("doi"))
            if k:
                m.setdefault(k, it)
            else:
                no_doi += 1
        note = ""
        matched = int(d.get("matched") or 0)
        if matched > len(items):
            note = (f"库共 {matched} 篇，本次只比对了前 {len(items)} 篇"
                    "⇒ 排在后面的没比对到，标出来的「库里已有」只对这些条目可靠")
        elif no_doi:
            note = (f"库里有 {no_doi} 条没记 DOI ⇒ 那几条比对不上，"
                    "候选表说「库里没有」对它们不作保证")
        return m, note

    def lit_find(self, query: str, library: str = "", limit: object = 10,
                 sources: str = "") -> dict:
        """四源检索一遍，把候选**列出来给人挑**（一步都不入库）。"""
        q = str(query or "").strip()
        if not q:
            raise ValueError("先说要找什么 —— 空着检索不了")
        fn = self.registry_fn(self.LIT_FIND_TOOL)
        if fn is None:
            return {"ok": False, "error": f"荣珂看不到 {self.LIT_FIND_TOOL}",
                    "hint": "它是 research 域的检索工具；取不到说明内核没起来 —— "
                            "这是接线断了，不是「世上没有这类文献」"}
        try:
            lim = int(limit)
        except Exception:                           # noqa: BLE001
            lim = 10
        lim = max(1, min(lim, self.LIT_FIND_MAX))
        args = {"query": q, "limit": lim}
        if str(sources or "").strip():
            args["sources"] = str(sources).strip()
        d = self.tool_forward(fn, args)
        if not d.get("ok"):
            return d
        have, have_note = self.library_doi_map(library)
        cands = []
        for it in (d.get("results") or []):
            doi = str(it.get("doi") or "").strip()
            hit = have.get(self._doi_key(doi)) if doi else None
            row = {
                # 标题里的 `&lt;i&gt;` 这类实体是 Crossref 原样带回来的（实测有），
                # 前端 esc() 会把 `&` 再转一次 ⇒ 用户在表里看见的是字面 `&lt;i&gt;`。
                # 这里解一道，让斜文章节名回到本来的样子。
                "title": self._plain(str(it.get("title") or "(无标题)")),
                "year": it.get("year"),
                "venue": self._plain(str(it.get("venue") or it.get("journal") or "")),
                "source": str(it.get("source") or ""),
                "doi": doi,
                "url": str(it.get("url") or ""),
                "citations": it.get("citations"),
                "in_library": bool(hit),
                "library_id": (hit or {}).get("id") or "",
                # ★ 库里已有这篇时，它有没有全文是**已知**的（库自己记着）；
                #   库里没有时只能报未知 —— 见下面 `pdf_note`。
                "library_fulltext": bool((hit or {}).get("fulltext")) if hit else None,
                # 「勾了有意义」= 有 DOI **且**库里还没有。库里已有的那篇勾下去
                # 必然被 `add_doi` 以「文献已存在」顶回来，所以它不算可勾 ——
                # 判据放在这一份字段里，前端不必再自己拼第二个条件（两处拼法
                # 早晚不一致，就会出现"这里勾得动、那里说已存在"）。
                "can_pick": bool(doi) and not hit,
            }
            cands.append(row)
        return {
            "ok": True,
            "query": q,
            "candidates": cands,
            "returned": len(cands),
            "per_source_limit": lim,
            # ★ 实测：`toolbox.search_literature` 是**逐源**取 limit 再按 DOI 去重，
            #   所以要 6 条、四源合并回 20 多条是正常结果，不是失控。
            #   不写这一句，用户会以为限流没生效。
            "limit_note": (f"每源上限 {lim} 条 · 四源（OpenAlex / arXiv / Crossref / "
                           f"PubMed）各自返回后按 DOI 去重合并 ⇒ 共 {len(cands)} 条"),
            "library_scanned": len(have),
            "library_note": have_note,
            "pdf_note": ("「能不能拿到全文」这一列：literature_search 的回包里"
                         "没有这个字段（实测只有 标题/年份/DOI/作者/出处/链接/被引），"
                         "所以库里没有的那些只能标**未知** —— 不猜。"
                         "真正的答案要等勾下去由 kb_add 实测：下到了给页码，"
                         "下不到会如实报「只入了元数据」或失败原因。"),
            "note": d.get("note") or "",
        }

    def lit_pick(self, dois: object, library: str = "") -> dict:
        """把**勾选中的**那几篇入库（逐篇转 `kb_add`，一篇都不多入）。

        如实分四类计数：入了且有全文 / 入了但只有元数据 / 跳过（库里已有）/ 失败。
        「文献已存在」在工具那一头是 `raise` ⇒ 回包是 error；这一类**不是故障**，
        必须单独报成"跳过（重复）"，否则用户看到一串红色会以为入库坏了。

        ★ 人因台账（W23 §10 遗留，W25 补上）：勾哪几篇是**人做的决定**，库又是
          真库 —— "谁在什么时候勾了哪几篇"必须**有据可查**。`kb_add` 自己写的
          工具台账记的是"工具被调了"，记不了"人勾了哪些" —— 那一层在面板，
          所以账记在这里（append-only，一行一次勾选）。写账失败**不拦住**已完成的
          入库（库已经动了，账可以补），但要如实带进回包的 `ledger` —— 不许静默。
        """
        want: list[str] = []
        for x in (dois or []):
            k = self._doi_key(x)
            if k and k not in want:
                want.append(k)
        if not want:
            raise ValueError("一篇都没勾 —— 先勾再入库（不许盲入）")
        if len(want) > self.LIT_PICK_MAX:
            raise ValueError(f"一次最多入 {self.LIT_PICK_MAX} 篇，勾多了请分几次")
        lock = getattr(self, "_kb_write_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._kb_write_lock = lock
        with lock:
            return self._lit_pick_locked(want, library)

    def _lit_pick_locked(self, want: list[str], library: str) -> dict:
        """`lit_pick` 的临界区：一串 `kb_add` 必须是原子的。"""
        rows, counts = [], {"fulltext": 0, "metadata": 0, "duplicate": 0, "failed": 0}
        for doi in want:
            d = self.kb_query("kb_add", library=library, src="doi:" + doi)
            err = str(d.get("error") or "")
            # ★ 判据必须由**实测的回包形状**推，不许由"成功应该有 ok"这种假设推。
            #   实测：kb_add 走 DOI 这条分支成功时回的是 litkb 的条目本体
            #   （有 id / title / has_fulltext / pages），**根本没有 ok 这个键** ——
            #   `_ok()` 只是 json.dumps，不补 ok=True。
            #   按 `d.get("ok")` 判成败会把**已经入库成功**的那篇报成失败，
            #   用户看到一串红、库里却多了一条 —— 那是比不报更坏的假信号。
            if err:
                # 「文献已存在」在工具那一头是 raise ⇒ 到这里是 error。
                # 这一类**不是故障**，必须单独报成"跳过（重复）"。
                kind = "duplicate" if "已存在" in err else "failed"
            elif d.get("id"):
                kind = "fulltext" if d.get("has_fulltext") else "metadata"
            else:
                kind = "failed"
                err = err or "回包既没有条目 id 也没有 error —— 判不了成败，按失败报"
            counts[kind] += 1
            rows.append({"doi": doi, "kind": kind,
                         "title": self._plain(str(d.get("title") or ""))[:160],
                         "id": str(d.get("id") or ""),
                         "pages": d.get("pages") or 0,
                         "error": err if kind in ("metadata", "failed") else ""})
        res = {"ok": counts["failed"] == 0,
               "added": counts["fulltext"] + counts["metadata"],
               "counts": counts, "rows": rows,
               "asked": len(want),
               "note": ("入库走的是 agent 自己那把 `kb_add`（含下载 PDF 与建段落索引），"
                        "服务端没有第二套下载实现。")}
        res["ledger"] = self._append_pick_ledger(res, want)
        return res

    def _append_pick_ledger(self, report: dict, dois: list[str]) -> dict:
        """把一次勾选入库**追加**进 `memory/kb_picks.jsonl`。

        落点选 `memory/`（与 runs.jsonl 同一家）：它记的是**人因**（哪次会话、
        哪个时刻、勾了哪几篇），不是库内容 —— 库自己的真相在 litkb，两边不许混。
        """
        ledger = {"ok": False, "error": ""}
        try:
            # ★ 走 `default_root()` 而不是自己拼 `data_root()/memory`：
            #   记忆根的唯一算法在那个函数里（cfg → 环境变量 → 数据根）。
            #   这里自己拼一份，就等于在"唯一算法"旁边又开了一个口径 ——
            #   测试夹具换根时只会换到一处，另一处照旧写进真台账
            #   （2026-10-20 实测：web_test 的 kb_picks 正控因此变红）。
            p = default_root() / "kb_picks.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "dois": list(dois),
                "counts": report.get("counts") or {},
                "rows": [{"doi": r.get("doi"), "kind": r.get("kind"), "id": r.get("id")}
                         for r in (report.get("rows") or [])],
            }, ensure_ascii=False)
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            ledger = {"ok": True, "path": str(p), "line": line}
        except Exception as e:                       # noqa: BLE001
            ledger = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return ledger

    def add_material(self, raw_name: str, data: bytes) -> dict:
        """把一份材料落进材料区。返回它的清单项。"""
        if not data:
            raise ValueError("文件是空的")
        if len(data) > UPLOAD_MAX:
            raise ValueError(f"超过单文件上限 {UPLOAD_MAX // 1024 // 1024}MB")
        name = safe_upload_name(raw_name)
        ext = Path(name).suffix.lower()
        if ext not in UPLOAD_ALLOWED:
            raise ValueError(
                f"不收 {ext or '（无扩展名）'} 这类文件 —— 现在只收文本/稿件类："
                + "、".join(sorted(UPLOAD_ALLOWED)))
        d = self.inbox_dir()
        target = d / name
        # 同名**绝不覆盖**：用户递进来的是一手材料，静默覆盖 = 静默的数据事故。
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for i in range(2, 100):
                cand = d / f"{stem}-{i}{suffix}"
                if not cand.exists():
                    target = cand
                    break
            else:
                raise ValueError("同名文件太多，换个名字")
        target.write_bytes(data)
        # 返回**扫描出来的那一项**，而不是另造一份：清单以磁盘为准，
        # 于是"上传时返回的"和"刷新页面后看到的"必然是同一个东西 ——
        # 两处各造一份 item，早晚会漂移成两张脸。
        for m in self.materials:
            if m["name"] == target.name:
                return m
        raise ValueError("落盘后没扫到它（磁盘与清单不一致）")

    def removed_dir(self) -> Path:
        """被移除的材料挪到这里 —— **不删**。

        为什么是"挪"而不是"删"，三条理由，一条比一条硬：
        · 这是**用户自己递进来的稿子**。点一个 `×` 就把它从磁盘抹掉是不可逆的，
          而 OS 的回收站、以及"别删个人文件、进回收站"这条通用纪律，说的都是同一件事。
        · `os.replace` 是**改名**，不是删除 —— 顺带绕开了环境里对"批量删除"的确认拦截。
          那条拦截正是上一轮把 `unlink` 打回来的元凶（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`，
          50 个/轮），而它被打回来之后**又被 `except Exception: pass` 吞掉**，
          于是界面说"已移除"、盘上文件还在。**同一个坑，两道防线一起填。**
        · 改名还可能失败（占用、权限），所以调用方拿到的是**两件事分开报**的结果，
          而不是一个 True。
        """
        if self.removed is None:
            self.removed = ws_dir() / "_removed"
            self.removed.mkdir(parents=True, exist_ok=True)
        return self.removed

    def drop_material(self, name: str) -> dict:
        """把一份材料移出清单。返回**两件事分开报**的结果。

        ★ 「清单里没有了」和「盘上没有了」是两件不同的事。
        上一版把它们压成一个 `True/False`，于是 `unlink` 失败时返回 True ——
        界面报"已移除"、文件还在盘上，**两副面孔**（H 型）。
        这一版的纪律：
          · 清单**一定**移掉（用户点的就是"它别在这儿了"，这一步不该失败）；
          · 文件**挪到 `_removed/`** 并如实报它去哪儿了；
          · 挪不动就直说挪不动、原件在哪，**绝不声称删掉了**。
        """
        try:
            name = safe_upload_name(name)
        except ValueError:
            return {"ok": False, "error": "文件名不可用"}
        # 清单是从目录算出来的 ⇒ "在不在材料里"就是"在不在盘上"，
        # 不需要（也不允许）再去维护一份会漂移的内存副本。
        if not any(m["name"] == name for m in self.materials):
            return {"ok": False, "error": "材料里没有这个文件"}
        out: dict = {"ok": True, "name": name, "moved": False, "kept_at": "", "note": ""}
        src = self.inbox_dir() / name
        if not src.exists():
            # 盘上本来就没有（清单与磁盘不同步的历史遗留）。这不是错误，
            # 但也**不许**顺势说成"删掉了" —— 如实说"盘上没有"。
            out["note"] = "清单已移除（盘上本就没有这个文件）"
            return out
        try:
            dest = self.removed_dir() / name
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                for i in range(2, 1000):
                    cand = dest.with_name(f"{stem}-{i}{suffix}")
                    if not cand.exists():
                        dest = cand
                        break
            os.replace(src, dest)          # 同盘改名：不触发任何删除保护
        except OSError as e:
            out["note"] = (f"已从清单移除，但原件没挪动（{type(e).__name__}）"
                           f"—— 它还在 {src}")
            out["kept_at"] = str(src)
            return out
        out["moved"] = True
        out["kept_at"] = str(dest)
        out["note"] = "已移除（原件挪到了 _removed，可找回）"
        return out

    def take_inbox(self) -> Path | None:
        """把材料区**改名**成这次任务的正式工作区并交出去；没有材料则 None。

        为什么是改名而不是"就地复用"或"拷贝进新目录"：
        · 就地复用 ⇒ 第二个任务覆盖第一个的 `session.json`（归档路径 = 工作区路径）；
        · 拷贝 ⇒ 用户递进来的原件与 agent 读到的副本分家，改一个不影响另一个
          （用户会以为"它看了我改过的稿子"，其实看的是旧的）。
        同盘 rename 一次搞定，原件在哪、agent 读哪，是同一份事实。
        """
        if not self.materials:
            return None
        src = self.inbox_dir()
        base = ws_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for i in range(1, 100):
            name = stamp if i == 1 else f"{stamp}-{i}"
            dest = base / name
            if not dest.exists():
                break
        stuck: list[str] = []
        try:
            os.replace(src, dest)
        except OSError:
            # 改名失败（比如被占用）就**如实退回**：宁可用一个新的空工作区另存材料，
            # 也不能假装材料在里面 —— 那是最容易让用户白跑一轮的假象。
            dest.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                try:
                    os.replace(f, dest / f.name)
                except OSError:
                    # ★ 搬不动就**留在清单里**。以前这里是 `pass`：材料留在材料区、
                    #   清单却被清空 ⇒ agent 少了这份材料开工，而界面一个字都不说。
                    #   用户只会发现"它好像没看我的稿子"，然后无从查起。
                    stuck.append(f.name)
        self.inbox = None
        # ★ 没搬过去的**不用记账**：它们还在 `_inbox` 里，而清单是从目录算出来的，
        #   所以下次 `materials` 会自动只剩它们 —— 看得见、能重试。
        #   （以前这里要手工回写一份内存列表，正是那种"忘记同步就静默漂移"的活。）
        self.inbox_leftover = stuck
        return dest

    # ── 启动任务 ────────────────────────────────────────────
    def start(self, goal: str, max_steps: int | None,
              contract: dict | None = None,
              mentor: bool = False,
              mode: str = "", preset: str = "") -> tuple[Run | None, str]:
        with self._lock:
            # ★ 授权闸门在**起线程之前**、也在"忙"之前。放到线程里就晚了一步：
            #   那个 Run 已经进了 self.runs、会话已经建好，失败时界面会
            #   显示一个"跑过但没成功"的任务，历史里也多一条假记录。
            # ★ 为什么排在"忙"前面：两者同时成立时，"等它结束"是个**假出路**
            #   —— 等完了照样开不了新任务。先告诉他"你没授权"才是可行动的。
            # ★ getattr 兜底同上：替身对象没有这个方法时视为放行，不要把
            #   "算不出"变成"不能用"。
            _lk = getattr(self, "_license_ok", None)
            if callable(_lk):
                ok, why = _lk()
                if not ok:
                    return None, why
            if self.active:
                cur = self.runs.get(self.active)
                if cur and not cur.done:
                    return None, "已有一个任务在跑，等它结束再开新的（内核是单任务串行的）。"
            if self.kernel is None:
                return None, f"内核未就绪：{self.kernel_error}"
            run = Run(id=uuid.uuid4().hex[:12], goal=goal, max_steps=max_steps,
                      contract=contract, mentor=mentor, mode=mode,
                      preset=str(preset or "").strip())
            # 有材料 ⇒ 这次任务就跑在材料区改名后的那个目录里（原文件原位、零拷贝）
            ws = self.take_inbox()
            if ws is not None:
                run.workspace = str(ws)
            self.runs[run.id] = run
            self.active = run.id
        threading.Thread(target=self._exec, args=(run,), daemon=True).start()
        return run, ""

    # ── 续跑 ────────────────────────────────────────────────
    def resume(self, rid: str) -> tuple[Run | None, str]:
        """从一个**未完成**的任务接着跑（跨进程）。

        为什么必须先查快照在不在：**能回放 ≠ 能续跑**。
        `session.json` 只记结果与事件（够回放），续跑要的是**对话本身**
        （`conversation.json`）。跑成功过的任务会把快照**改名**成
        `conversation.done.json`（不删，见 `loop.py` 收尾那段的理由），
        所以「有归档、没快照」是**常态** —— 这时要如实说"这次没法接着跑"，
        而不是偷偷开一个从头开始的任务去骗用户。

        续跑**回到原工作区**：不然那半截和新跑的分家，历史里会多出一个
        看起来毫不相干的任务。
        """
        if not rid or "/" in rid or "\\" in rid or ".." in rid:
            return None, "任务 id 不合法"
        with self._lock:
            # 续跑也是"发起一个会烧模型的运行"，必须同样过授权闸门。
            # ★ 用 getattr 兜底：`resume_test.py` 用 SimpleNamespace 造替身来测
            #   "快照在不在"那段逻辑，替身上没有 _license_ok。直接点调用会把
            #   那些与本功能无关的测试打崩 —— 已实测撞到（AttributeError）。
            #   兜底的语义与本模块一致：**算不出授权状态就放行**，绝不把
            #   "我不知道"读成"你不行"。
            _lk = getattr(self, "_license_ok", None)
            if callable(_lk):
                ok, why = _lk()
                if not ok:
                    return None, why
            if self.active:
                cur = self.runs.get(self.active)
                if cur and not cur.done:
                    return None, "已有一个任务在跑，等它结束再接着跑（内核是单任务串行的）。"
            if self.kernel is None:
                return None, f"内核未就绪：{self.kernel_error}"
        ws = ws_dir() / rid
        p = ws / "conversation.json"
        if not p.is_file():
            return None, ("这个任务没有可续跑的对话快照 —— "
                          "它可能已经跑完了，或者中断在还没有对话的阶段")
        try:
            snap = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            return None, f"对话快照读不出来（{type(e).__name__}: {e}）"
        if not (snap.get("messages") or []):
            return None, "快照里没有对话内容，接着跑没有意义"
        # 续跑**半截任务**时的开关口径：看快照，不看前端（见 `mentor_from_snapshot`）。
        _mentor_on = mentor_from_snapshot(snap)
        with self._lock:
            run = Run(id=uuid.uuid4().hex[:12],
                      goal=str(snap.get("goal") or ""),
                      resume=snap, workspace=str(ws), mentor=_mentor_on)
            self.runs[run.id] = run
            self.active = run.id
        threading.Thread(target=self._exec, args=(run,), daemon=True).start()
        return run, ""

    def turn_archives(self, rid: str, snap: dict) -> list[dict]:
        """追问之前的父轮归档（只读，不补造）。

        内核按**工作区**写 `session.json`（`runs/<id>/session.json`），追问
        复用同一工作区 ⇒ 新一轮会把父轮那份覆盖。`follow_up()` 起跑前把
        父轮拷进 `turns/`，这里按 `started` 排序交回给界面，让历史回放能
        连着上一轮一起读 —— 否则用户看到的是「原目标 + 1 + 一段没头没尾的回答」。
        """
        roots: list[Path] = []
        w = str(snap.get("workspace") or "")
        if w:
            roots.append(Path(w))
        roots.append(ws_dir() / rid)
        seen: set[tuple] = set()
        out: list[dict] = []
        for root in roots:
            d = root / "turns"
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json")):
                try:
                    t = json.loads(f.read_text(encoding="utf-8"))
                except Exception:                            # noqa: BLE001
                    continue
                if not isinstance(t, dict):
                    continue
                key = (t.get("id"), t.get("started"), t.get("finished"))
                if key in seen:
                    continue
                seen.add(key)
                out.append({k: t.get(k) for k in
                            ("id", "goal", "started", "finished", "elapsed",
                             "events", "result", "error")})
        out.sort(key=lambda t: (t.get("started") or 0))
        return out

    def followup_texts(self, rid: str) -> list[str]:
        """旧归档的 `follow_up` 事件只记了长度；追问原文从会话快照补。

        会话快照里追问是一条带固定标头的 user message（见 loop.py 的
        resume 分支）。这里只取这些消息、按顺序返回，供回放画成用户气泡。
        读不动就回空列表 —— 回放不能因为一个可选字段整页失败。
        """
        ws = ws_dir() / str(rid)
        for name in ("conversation.done.json", "conversation.json"):
            p = ws / name
            if not p.is_file():
                continue
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except Exception:                            # noqa: BLE001
                return []
            out: list[str] = []
            for m in (snap.get("messages") or []):
                if not isinstance(m, dict) or m.get("role") != "user":
                    continue
                c = str(m.get("content") or "")
                mark = "【会话内追问 —— 就地接着说，不必重来】"
                if mark in c:
                    t = c.split(mark, 1)[1].strip()
                    if t:
                        out.append(t)
            return out
        return []

    # ── 会话内追问 ──────────────────────────────────────────
    def follow_up(self, rid: str, text: str,
                  mentor: "bool | None" = None, mode: str = "",
                  preset: str = "") -> tuple[Run | None, str]:
        """接着一个**已开过头的会话**继续说（ZCode 式连续会话的那一半）。

        与 `resume()` 的分工：`resume` 接的是**半截任务**（conversation.json 还在）；
        这里接的是**整场会话** —— 跑成功过的任务快照已改名成
        `conversation.done.json`，追问时同样要接回来。两个名字都找，谁在用谁。

        工作区必须**回到原会话的目录**：前情、产物、快照都以那个目录为边界，
        新开一个目录，追问就变成"另起炉灶"—— 历史里会出现两个本该是一个的会话。
        """
        text = (text or "").strip()
        if not text:
            return None, "追问内容是空的"
        if not rid or "/" in rid or "\\" in rid or ".." in rid:
            return None, "任务 id 不合法"
        with self._lock:
            # 追问也是「发起新任务」：过期用户不该得到一条跑失败的历史记录，
            # 而应该像 /api/run 一样被 403 拦下。授权原因排在「忙」前面。
            _lk = getattr(self, "_license_ok", None)
            if callable(_lk):
                ok, why = _lk()
                if not ok:
                    return None, why
            if self.active:
                cur = self.runs.get(self.active)
                if cur and not cur.done:
                    return None, "已有一个任务在跑，等它结束再接着说（内核是单任务串行的）。"
            if self.kernel is None:
                return None, f"内核未就绪：{self.kernel_error}"
        # 定位会话工作区，三步兜底：内存 → 归档记录 → 目录名。
        # 内存里有（本轮刚跑完）就直接用；服务重启后内存空了，归档里的
        # `workspace` 字段就是单源（`_archive` 写它正是为了这一刻）。
        ws: Path | None = None
        prev = self.runs.get(rid)
        if prev is not None and prev.workspace:
            p = Path(prev.workspace)
            if p.is_dir():
                ws = p
        if ws is None:
            arc = ws_dir() / rid / "session.json"
            if arc.is_file():
                try:
                    _w = str(json.loads(arc.read_text(encoding="utf-8"))
                             .get("workspace") or "")
                except Exception:
                    _w = ""
                if _w and Path(_w).is_dir():
                    ws = Path(_w)
        if ws is None:
            p = ws_dir() / rid
            ws = p if p.is_dir() else None
        if ws is None:
            return None, "找不到这个会话的工作区 —— 它可能是在别的机器/目录上跑的"
        snap: dict | None = None
        for name in ("conversation.json", "conversation.done.json"):
            p = ws / name
            if not p.is_file():
                continue
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                return None, f"对话快照读不出来（{type(e).__name__}: {e}）"
            break
        if not isinstance(snap, dict) or not (snap.get("messages") or []):
            return None, ("这个会话没有可接续的对话 —— 它中断在还没对话的阶段，"
                          "或者快照已经丢了。开个新对话吧。")
        # ★ 追问会在**同一个工作区**里跑，而内核的归档路径就是 `ws/session.json`
        #   ⇒ 不先留一份，父轮的结果与事件会被这一轮直接覆盖掉（实测
        #   `runs/20261021-194113` 只剩 follow_up + resumed）。拷到 `turns/`
        #   只增不改；拷失败不影响追问本身（归档是附赠品）。
        try:
            prev_arc = ws / "session.json"
            if prev_arc.is_file():
                tdir = ws / "turns"
                tdir.mkdir(exist_ok=True)
                dst = tdir / f"{prev_arc.stat().st_mtime_ns}.json"
                if not dst.exists():
                    shutil.copy2(prev_arc, dst)
        except Exception:
            pass
        # 追问的话由**内核**追加进消息栈（loop.py 的 follow_up 分支）——
        # 服务端只负责把话带到，不自己改写对话内容。
        snap["follow_up"] = text
        # 导师模式的口径：界面**每次都显式给**当前开关状态；没给（老前端/直接打接口）
        # 就回到"当时的状态"—— 也就是这场会话的对话栈里那段在不在（同一把尺子，
        # 不与 `resume()` 各写一套）。
        if mentor is None:
            mentor = mentor_from_snapshot(snap)
        with self._lock:
            # 追问没带 preset（老前端 / 直接打接口）⇒ 认回父轮那一套，
            # 否则同一场会话前后两轮会按不同的尺子写（实测 C 场的形状）。
            explicit = str(preset or "").strip()
            inherited = explicit or (str(getattr(prev, "preset", "") or "")
                                     if prev is not None else "")
            run = Run(id=uuid.uuid4().hex[:12], goal=text,
                      resume=snap, workspace=str(ws), mentor=bool(mentor),
                      mode=mode, preset=inherited)
            self.runs[run.id] = run
            self.active = run.id
        threading.Thread(target=self._exec, args=(run,), daemon=True).start()
        return run, ""

    def _exec(self, run: Run) -> None:
        try:
            assert self.kernel is not None
            frozen = None
            if run.contract:
                # 人审过的契约：跳过起草，直接按冻结的那份跑
                from ..contract import TaskContract
                try:
                    frozen = TaskContract.from_dict(run.contract)
                except Exception as e:
                    run.push("note", {"text": f"冻结契约用不了，回退到自动起草：{e}"})
            # 续跑必须**回到原工作区**：快照、产物、归档都以工作区为边界，
            # 新开一个目录就等于"续跑"跑出来的东西和原来那半截分了家，
            # 用户会在历史里看到两个任务，而它们本该是同一个。
            into = Path(run.workspace) if run.workspace else None

            def on_event(kind: str, data: dict) -> None:
                # 内核开工第一件事就是广播 workspace（它自己定的目录）。
                # 在这里回填 `run.workspace` —— 不然"没走材料区的新任务"在内存里
                # 没有工作区可查，跑完之后「接着说」就找不到这份会话（见 follow_up）。
                if kind == "workspace" and not run.workspace:
                    run.workspace = str(data.get("path") or "")
                run.push(kind, data)

            result = self.kernel.run(
                run.goal, contract=frozen, max_steps=run.max_steps, cancel=run.cancel,
                workspace=into,
                on_event=on_event,
                # 运行中问人的物理通道。内核负责**何时问/问什么**，
                # 这里只负责"把问题端给人、等他的答复"（传输层不做业务判断）。
                intervene=lambda kind, data: run.ask_human(kind, data),
                # 续跑：把上一次留下的对话交回内核，它据此跳过装配、接着跑。
                resume_from=run.resume,
                # ★ 导师模式：会话级开关**真的进内核**（不是只改界面）。
                #   内核拿它去决定首条消息里注入不注入那段纪律、以及跑完算不算
                #   那三条出处判据 —— 见 `loop.initial_messages(kb_only=…)`。
                kb_only=bool(run.mentor),
                # ★ W36 模式开关：用户按的「任务模式 / 仅问答」真的进内核。
                #   `""` 时内核按契约起草器的判断走（不猜用户的意图）。
                mode=str(run.mode or ""),
                # ★ 能力预设：绑本次 run，不重建长命单例。
                preset=str(run.preset or ""),
            )
            run.finish(result.to_dict())
        except Exception as e:
            run.finish(None, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
        finally:
            with self._lock:
                if self.active == run.id:
                    self.active = None

    # ── 查询 ────────────────────────────────────────────────
    def get(self, rid: str) -> Run | None:
        r = self.runs.get(rid)
        if r is not None:
            return r
        return self._from_archive(rid)

    @staticmethod
    def _from_archive(rid: str) -> "Run | None":
        """从 runs/<id>/session.json 还原一次历史任务。

        这是"历史可回放"的关键：内存里的 Run 会随服务重启蒸发，
        磁盘上的归档不会。id 就是工作区目录名（时间戳），人也读得懂。
        """
        if not rid or "/" in rid or "\\" in rid or ".." in rid:
            return None                      # 挡住路径穿越
        p = ws_dir() / rid / "session.json"
        if not p.is_file():
            return None
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        run = Run(id=str(j.get("id") or rid), goal=str(j.get("goal") or ""))
        run.events = list(j.get("events") or [])
        run.done = True
        run.result = j.get("result")
        run.error = str(j.get("error") or "")
        run.started = float(j.get("started") or 0)
        run.finished = float(j.get("finished") or 0)
        # 能不能接着跑，只取决于**对话快照**在不在（不是归档在不在）。
        run.resumable = (ws_dir() / run.id / "conversation.json").is_file()
        return run

    #: `session.json` 解析结果的缓存：`路径 → ((mtime_ns, size), 派生行)`。
    #:
    #: ★ 为什么必须有（W30 实测）：`history(n)` **完全不随 n 变** —— 为了给 10 条，
    #   它把 48 份归档全读全解析（最大 2.2 MB），热跑 169 ms。
    #   淘汰得越久、跑得越多它越慢，而 `/api/runs` 是界面会反复打的接口，
    #   `/api/figures` 更是在它外面又套一层。归档是**写完就不再改**的成品，
    #   所以按 (mtime_ns, size) 做键是安全的：文件真变了键就变，不会读到旧结论。
    # ★ 缓的是**派生行**而不是整个 `j`：行是几 KB，`j` 可能几 MB。
    #   行里没有 `draft` 正文（只有 `has_draft` 布尔），所以内存不会跟着
    #   最长的那篇稿子走。
    # ★ 有界：只留 `_HIST_CACHE_MAX` 条，超了按插入序砍最旧的 ——
    #   缓存无界增长和泄漏只是同一个问题换个说法。
    _HIST_CACHE_MAX = 256

    def _archived_row(self, d: Path, f: Path) -> dict | None:
        """读一份归档，产出历史页那一行。**带缓存**。

        ★ 缓存键必须是**这个行依赖的全部输入**，不只是 `session.json`：
          这一行里 `followable` 看的是 `conversation.json` / `conversation.done.json`
          在不在，而那两份文件和 `session.json` 是**分开写、分别改名**的
          （`loop.py` 跑成功后把 `conversation.json` 改名成 `.done.json`）。
          只用 session 的 mtime 做键，就会出现"归档没动、对话快照动了"时
          读到过时的 `followable` —— 表现是任务跑完了却一直不给「接着说」入口。
        ★ 四个 stat 约 0.08 ms，被省掉的那次 `json.loads` 是 3.5 ms（48 份共 167 ms）。
        """
        sigs: list[int] = []
        try:
            st = f.stat()
        except OSError:
            return None
        sigs += [st.st_mtime_ns, st.st_size]
        for name in ("conversation.json", "conversation.done.json"):
            try:
                sigs += [os.stat(d / name).st_mtime_ns]
            except OSError:
                sigs.append(0)
        key = str(f)
        sig = tuple(sigs)
        with self._hist_lock:
            hit = self._hist_cache.get(key)
        if hit is not None and hit[0] == sig:
            return dict(hit[1])                # 给出副本：调用方改不到缓存
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(j, dict):
            return None
        rid = j.get("id") or d.name
        r = j.get("result") or {}
        row = {
            "id": rid,
            "goal": j.get("goal") or "",
            "success": bool(r.get("success")),
            "steps": r.get("steps"),
            "elapsed": j.get("elapsed") or 0,
            # 成本：归档里存的是 cost_usd（可能是字面 "UNKNOWN"）。
            # 统一成显示串，前端不重算 —— 判定与格式化都在内核。
            "cost": r.get("cost_display") or _pricing.format_cost(
                r.get("cost_usd", _pricing.UNKNOWN)),
            "in_tokens": (r.get("usage") or {}).get("input_tokens") or 0,
            "ts_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(float(j.get("started") or 0))),
            "replay": True,
            # 能不能「接着说」—— 侧栏会话列表靠它决定给不给追问入口。
            # 与 replay（能不能回放）是两件事：快照改名后回放照旧、追问照旧。
            "followable": ((d / "conversation.json").is_file()
                           or (d / "conversation.done.json").is_file()),
            # 有没有可展示的**交付正文**（result.draft）——
            # 「成品」区用它判断这次任务有没有产出文章可读；
            # 没有 draft 的任务（如纯 PPT）靠 files 展示，见 deliverables()。
            "has_draft": bool(str(r.get("draft") or "")),
            "_ts": float(j.get("started") or 0),
        }
        with self._hist_lock:
            cache = self._hist_cache
            if len(cache) >= self._HIST_CACHE_MAX and key not in cache:
                # 按插入序淘汰最旧的一条（dict 保序）
                cache.pop(next(iter(cache)), None)
            cache[key] = (sig, row)
        return dict(row)

    def history(self, n: int = 30) -> list[dict]:
        """历史任务：**归档与台账合并**列出，能回放的那几行标 replay=True。

        为什么不是"有归档就只给归档"：
            归档（`runs/<id>/session.json`）是后加的，更早的任务没有它。
            原先写成"只要找到任意一个归档就 return"，结果那些没归档的旧任务
            在历史页里**整个消失** —— 用户看到的是"我的记录怎么变少了"。
            而界面本来就支持不可回放的行（不给「回放」按钮即可）。
            两路都列才是原意：**看得见**优先于**能回放**。
        """
        seen: set[str] = set()
        out: list[dict] = []
        try:
            base = ws_dir()
            if base.exists():
                for d in sorted(base.iterdir(), reverse=True):
                    # `_` 开头的是**建材不是成品**：材料区 `_inbox`、移除区 `_removed`、
                    # 回归日志 `_regress`、以及测试夹具。它们里头的 `session.json`
                    # 是夹具凑出来的，不是用户真跑过的任务 —— 列进去就是往用户的历史里
                    # 塞假记录。（`ui_shot.latest_archive()` 早就是这么筛的：同一个目录，
                    # 两处判"什么算一个任务"必须给同一个答案，否则就是两副面孔。）
                    if d.name.startswith("_"):
                        continue
                    f = d / "session.json"
                    if not d.is_dir() or not f.is_file():
                        continue
                    row = self._archived_row(d, f)
                    if row is None:
                        continue
                    seen.add(row["id"])
                    out.append(row)
        except Exception:
            pass

        # 台账里那些**没有归档**的更早任务：照列，只是不给回放入口
        try:
            for r in Memory(default_root()).recent_runs(200):
                rid = str(r.get("run_id") or "")
                if not rid:
                    # 兜底：加 run_id 字段之前写的旧记录，用 workspace 目录名反推。
                    # 少了这一步，同一个任务会以"可回放"和"不可回放"两副面孔各出现一次 ——
                    # 实测就是这样（19:31 与 19:30 两行同目标）。
                    _ws = str(r.get("workspace") or "").replace("\\", "/").rstrip("/")
                    rid = _ws.rsplit("/", 1)[-1] if _ws else ""
                if rid and rid in seen:
                    continue                     # 与归档重复；归档那行信息更全，留归档
                out.append({
                    "id": rid,
                    "goal": r.get("goal") or "",
                    "success": bool(r.get("success")),
                    "steps": r.get("steps"),
                    "elapsed": r.get("elapsed") or 0,
                    "cost": _pricing.format_cost(r.get("cost_usd", _pricing.UNKNOWN)),
                    "in_tokens": int(r.get("input_tokens") or 0),
                    "ts_iso": r.get("ts_iso") or "",
                    "replay": False,             # 没有归档 → 回放不了，但看得见
                    "followable": False,         # 没有归档 → 更没有对话快照
                    "has_draft": False,          # 没有归档 → 读不到正文
                    "_ts": float(r.get("ts") or 0),
                })
        except Exception:
            pass

        out.sort(key=lambda x: x.get("_ts") or 0, reverse=True)
        for row in out:
            row.pop("_ts", None)
        return out[:n]

    def status(self) -> dict:
        out: dict[str, Any] = {
            "kernel_ok": self.kernel is not None,
            "kernel_error": self.kernel_error,
            "active": self.active,
        }
        try:
            cfg = Config.load()
            out["provider"] = {
                "name": cfg.provider.name, "model": cfg.provider.model,
                "base_url": cfg.provider.base_url, "state": cfg.provider.diagnose(),
                "reasoning_effort": cfg.provider.reasoning_effort,
                "api_key_env": cfg.provider.api_key_env,
            }
            out["loop"] = {
                "max_steps": cfg.loop.max_steps,
                "max_identical_calls": cfg.loop.max_identical_calls,
                "temperature": cfg.loop.temperature,
                "context_warn_chars": getattr(cfg.loop, "context_warn_chars", 0),
                # 上下文经济这六项必须在这里看得见。理由不是"锦上添花"：
                # `compact_target_chars=0` 与 `budget_max_prompt_tokens=0` 都表示"关着"，
                # 界面上不报，用户就分不清"这次没触发"和"根本没开"——
                # 与下面计价那句"用户分不清'真没花钱'和'内核不会算钱'"是同一条纪律。
                "compact_target_chars": getattr(cfg.loop, "compact_target_chars", 0),
                "compact_keep_live": getattr(cfg.loop, "compact_keep_live", 6),
                "budget_hint_steps": getattr(cfg.loop, "budget_hint_steps", 0),
                "budget_max_prompt_tokens": getattr(cfg.loop,
                                                    "budget_max_prompt_tokens", 0),
                "budget_warn_ratio": getattr(cfg.loop, "budget_warn_ratio", 0.7),
                "budget_force_ratio": getattr(cfg.loop, "budget_force_ratio", 0.85),
            }
            out["workbench"] = str(cfg.runtime.workbench)
            out["workbench_error"] = getattr(self.kernel, "workbench_error", "")
            # 计价：把"价格表填了没"报到明面。
            # 不报的话，界面上一个 UNKNOWN 就是个哑谜 ——
            # 用户分不清"这次真没花钱"和"内核不知道怎么算钱"。
            try:
                from .. import pricing as _pricing
                pr = getattr(cfg, "pricing", {}) or {}
                models = sorted((pr.get("models") or {}).keys())
                out["pricing"] = {
                    "models": models,
                    "count": len(models),
                    "has_default": bool(pr.get("default")),
                    "cache_read_included_in_input": bool(
                        pr.get("cache_read_included_in_input", True)),
                    "covered": bool(models or pr.get("default")),
                    "current_model": cfg.provider.model,
                    "current_model_priced": bool(_pricing._rates(pr, cfg.provider.model)),
                }
            except Exception as e:
                out["pricing"] = {"error": f"{type(e).__name__}: {e}"}
        except Exception as e:
            out["config_error"] = f"{type(e).__name__}: {e}"

        if self.kernel is not None:
            out["tools"] = len(self.kernel.registry)
            out["domain_tools"] = len(self.kernel._installed)
            # MCP：连上的 / 没连上的都要报，别让"配了却没生效"变成哑谜
            try:
                d = self.kernel.mcp_diag()
                out["mcp"] = {
                    "enabled": d.get("enabled"),
                    "servers": len(d.get("specs") or []),
                    "tools": len(d.get("installed") or []),
                    "errors": d.get("errors") or {},
                    "skipped": d.get("skipped") or [],
                    "rejected": d.get("rejected") or [],
                }
            except Exception as e:
                out["mcp"] = {"error": f"{type(e).__name__}: {e}"}
            # 图面体检装置：**审不了 ≠ 通过**，所以"装置是否可用"必须能一眼看到。
            # 静默降级成"跳过并算过"是这块最容易犯的错，界面上不许有这种模糊。
            try:
                from ..figure import FigureAuditor
                fg = self.kernel.cfg.figure
                au = FigureAuditor(python_exe=fg.python or None,
                                   skills_dir=fg.skills_dir or None,
                                   timeout=fg.timeout)
                dg = au.diag()
                out["figure"] = {
                    "enabled": fg.enabled,
                    "python": dg.get("python") or "",
                    "scripts": dg.get("scripts") or "",
                    "tool": self.kernel.registry.get("audit_figure") is not None,
                    "notes": dg.get("notes") or [],
                }
            except Exception as e:
                out["figure"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            # `runs_dir` 要传：不传的话 `stats()` 里只有台账口径，而台账**会漏记**
            # 用量（详见 `memory.archived_usage()`）。传了才有 `archived` 那把完整的秤，
            # 与 `--pricing` 报的是同一个数。
            memory = Memory(default_root()).stats()
            memory["archived"] = archived_usage(ws_dir(), cache=self._usage_cache)
            out["memory"] = memory
        except Exception:
            pass
        return out

    # ── 设置（W3：换模型）───────────────────────────────────
    # ★ 换模型**不需要动内核**。`Rongke.__init__` 里 `LLM(...)` 是在构造时
    #   把模型冻住的（loop.py:1505-1506），而 `boot()` 本来就是可重入的
    #   （`/api/boot` 走的就是它）。所以正确接法只有一条：
    #   **写 config.json → 调 boot() 重建 agent**。一行内核逻辑都不改。
    #
    # ★ 密钥**绝不回显、绝不落盘**。这里只报「环境变量设没设」这一个布尔，
    #   连长度都不报 —— 报长度就够把一位数密钥猜个范围了，而这不是需要的信息。
    ENV_OVERRIDE_VARS = (
        ("provider.model", "RONGKE_MODEL"),
        ("provider.base_url", "RONGKE_BASE_URL"),
        ("provider.api_key_env", "RONGKE_API_KEY_ENV"),
        ("provider.reasoning_effort", "RONGKE_REASONING_EFFORT"),
        ("runtime.memory_dir", "RONGKE_MEMORY_DIR"),
    )

    def setup_view(self) -> dict:
        """首次使用自检（只读）。问答"这台机器能不能做 PPT / 画图"。

        ★ 为什么放在服务端而不是让前端自己拼判据：这几项的真值全在**盘上和
          子进程里**（哪个解释器装了 python-pptx、审计脚本在哪、Origin 的 COM
          通不通）。前端够不到那些东西，硬拼一份就必然和真相漂。
        ★ 与 `--mcp-doctor` 的差别：那条路要起满 MCP 子进程握手（几十秒），
          这里只做"命令在 + 脚本在 + import 得动"，够回答首次使用的问题。
        """
        from ..setupdoctor import diagnose_setup
        return diagnose_setup(Config.load())

    def setup_fix(self, body: dict) -> tuple[dict | None, str]:
        """一键修：**只改配置指向本机已经能用的东西**，改完当场重测。

        与 `settings_save` 同一套纪律：白名单外一律拒、写前备份、原子写、
        修完重建 agent（deck.python / figure.python 是在构造时读进 `cfg` 的，
        不重建的话这一轮跑的就是旧值）。**它不会 pip install 任何东西。**
        """
        from ..setupdoctor import SETUP_WRITE_KEYS, fix_setup
        if not isinstance(body, dict):
            return None, "请求体不是对象"
        keys = body.get("keys")
        if keys is None:
            return None, "缺少 keys（要修哪几项）"
        if not isinstance(keys, list) or not keys:
            return None, "keys 必须是非空数组"
        clean = [str(k) for k in keys]
        allowed = set(SETUP_WRITE_KEYS) | {"figure.skills_dir"}
        bad = [k for k in clean if k not in allowed]
        if bad:
            return None, ("这几项不能一键修：" + "、".join(bad)
                          + "。向导只能改 " + " / ".join(SETUP_WRITE_KEYS)
                          + " 这类**指向**（装包、装 Origin 得你自己来）。")
        try:
            res = fix_setup(clean)
        except Exception as e:                          # noqa: BLE001
            return None, f"修复没做成：{type(e).__name__}: {e}"
        self.boot()                 # 解释器路径是构造时读进 cfg 的 ⇒ 必须重建
        res["rebuilt"] = self.kernel is not None
        res["kernel_error"] = self.kernel_error
        return res, ""

    def analysis_view(self) -> dict:
        """数据分析成图的**能力视图**（只读，快）。

        ★ 这里**不跑分析**。分析要起解释器画图（几秒到几十秒），
          放进轮询或进页面即跑，会让「点开一页」变成「等一次任务」。
          真正跑的是 `analysis_run()`（POST）。这一条只回答：
          装置齐不齐、支持哪些图型、有哪些现成数据表可选。
        """
        from ..analysis import CHART_KINDS, COL_WIDTH_MM, FULL_WIDTH_MM, diag
        d = diag(Config.load().figure.python or None)
        # ★ 用模块级的 `ws_dir()`（任务归档根 runs/），不是 `self.root` ——
        #   Manager 上**没有** root 字段（照现有代码的写法来，别自造一个）。
        base_root = ws_dir()
        # 候选数据表：扫 runs/ 下常见的表后缀。**只扫两层**，
        # 免得在深目录里翻出几万行（这一步要快）。
        tables: list[dict] = []
        try:
            for sub in ("", "data", "_uploads"):
                base = base_root / sub if sub else base_root
                if not base.is_dir():
                    continue
                for p in sorted(base.glob("*")):
                    if p.is_file() and p.suffix.lower() in (
                            ".csv", ".tsv", ".xlsx", ".json"):
                        if p.name.startswith("_"):
                            continue
                        # ★ 排除诊断产物。runs/ 里躺着一堆 `*.figure-qa.json`、
                        #   `*session.json` 这类**机器写的** JSON，把它们列进
                        #   "可选数据表"是在诱导用户拿审计报告去画图
                        #   （实测第一次探针就这么列出来了一条 fig_conversion.figure-qa.json）。
                        low = p.name.lower()
                        if any(t in low for t in ("-qa.", "session.", "audit",
                                                  "collision", "manifest")):
                            continue
                        tables.append({"name": p.name,
                                       "rel": str(p.relative_to(base_root)),
                                       "kb": round(p.stat().st_size / 1024, 1)})
        except Exception:                                  # noqa: BLE001
            tables = []
        return {
            "ready": d["ready"],
            "python": d["python"],
            "notes": d["notes"],
            "kinds": [{"kind": k, "name": v["name"], "needs": v["needs"]}
                      for k, v in CHART_KINDS.items()],
            "widths": {"column": COL_WIDTH_MM, "full": FULL_WIDTH_MM},
            "tables": tables[:40],
            "root": str(base_root),
        }

    def analysis_run(self, body: dict) -> tuple[dict | None, str]:
        """真跑一次：读表 → 选图型 → 出图 → **顺手过图面体检**。

        ★ 与 `plot_data` 工具的分工：工具是给模型用的（返回契约让模型转手
          `audit_figure`）；这里是给**人**用的，所以一次跑到底、直接把
          图的路径和体检结论摆出来，不用人在两个接口之间搬运。
        ★ 但**判据只有一份**：体检仍然调 `figure.FigureAuditor`，
          前端不重算任何一条结论。
        """
        from .. import analysis as _an
        from ..figure import FigureAuditor, FigureContract

        if not isinstance(body, dict):
            return None, "请求体不是对象"
        rel = str(body.get("table") or "").strip()
        if not rel:
            return None, "缺少 table（要画哪张表）"
        base_root = ws_dir()
        # ★ 路径必须在 runs/ 里面。这一条不是可选的：接口收的是**相对路径**，
        #   不校验就成了任意文件读取（本仓 /api/orig/raw 就栽过这个）。
        tp = (base_root / rel).resolve()
        try:
            tp.relative_to(base_root.resolve())
        except ValueError:
            return None, f"table 必须落在 runs/ 里：{rel}"
        if not tp.exists():
            return None, f"数据表不存在：{rel}"
        if tp.suffix.lower() not in (".csv", ".tsv", ".xlsx", ".json"):
            return None, f"不支持的表格类型：{tp.suffix}"

        try:
            rep = _an.analyze(
                tp,
                kind=str(body.get("kind") or ""),
                x=str(body.get("x") or ""), y=str(body.get("y") or ""),
                group=str(body.get("group") or ""),
                title=str(body.get("title") or ""),
                width_mm=float(body.get("width_mm") or _an.COL_WIDTH_MM),
                out_dir=base_root / "_analysis",
                python_exe=(Config.load().figure.python or None),
                timeout=180.0)
        except _an.TableError as e:
            return None, str(e)
        except Exception as e:                             # noqa: BLE001
            return None, f"成图失败：{type(e).__name__}: {e}"

        out: dict = {
            "columns": rep.get("columns"),
            "suggestions": rep.get("suggestions"),
            "plan": rep.get("plan"),
            "ran": rep.get("ran"),
            "run": rep.get("run"),
            "error": rep.get("error"),
        }
        if not rep.get("ran") or rep.get("error"):
            out["audit"] = None
            return out, ""

        cd = _an.contract_for(rep["plan"], journal=str(body.get("journal") or ""))
        fc = FigureContract.from_dict(cd)
        pan = (cd["panels"] or [{}])[0]
        lg = _an.legend_for(cd, meta=rep.get("meta"),
                            center=str(pan.get("center") or ""),
                            spread=str(pan.get("spread") or ""))
        au = FigureAuditor(python_exe=(Config.load().figure.python or None),
                           skills_dir=None, timeout=300.0)
        try:
            arep = au.audit(fc, workdir=Path(rep["workdir"]),
                            source="plot.py", pdf="figure.pdf", legend_text=lg)
            out["audit"] = {
                "ok": arep.ok,
                "verdict": arep.verdict,
                "gates": [{"name": g.name, "verdict": g.verdict,
                           "detail": g.detail} for g in arep.gates],
                "blockers": [str(b) for b in arep.blockers],
                "warnings": [str(w) for w in arep.warnings],
              }
        except Exception as e:                             # noqa: BLE001
            out["audit"] = {"ok": None, "verdict": "NOT AUDITABLE",
                            "gates": [], "blockers": [],
                            "warnings": [f"体检没跑起来：{type(e).__name__}: {e}"]}
        out["contract"] = cd
        out["legend"] = lg
        # 图给前端看的相对路径。PNG 走静态文件服务（`/runs/...`）；
        # PDF 走已有的 `/api/orig/raw`。两个都**只给相对 runs/ 的那一段**，
        # 前端自己拼前缀 —— 接口不吐绝对路径。
        wdp = Path(rep["workdir"])
        for ext, key in (("png", "png_rel"), ("pdf", "pdf_rel")):
            f = wdp / f"figure.{ext}"
            try:
                out[key] = f.relative_to(base_root).as_posix() if f.exists() else ""
            except ValueError:
                out[key] = ""       # 落在 runs/ 外就不给（前端也就不会拼错）
        return out, ""

    def settings_view(self) -> dict:
        """当前配置的**真相**（只读）。回答三件事：现在用哪个模型、
        它是被哪一层决定的、我要改的东西落哪儿。"""
        cfg = Config.load()
        pv = cfg.provider
        files = []
        for scope, path in (("用户级", Path.home() / ".rongke" / "config.json"),
                            ("项目级（写入落点）", project_root() / "config.json")):
            files.append({"scope": scope, "path": str(path), "exists": path.is_file()})
        # 「我改的为什么没生效」的另一个常见答案是环境变量 —— 它在 config.json
        # **之上**，所以只报配置文件会漏掉整整一层。不列出来就是让人对着
        # 一个改对了却仍不生效的界面发呆。
        env_over = [{"key": k, "var": v, "set": bool(os.environ.get(v))}
                    for k, v in self.ENV_OVERRIDE_VARS]
        return {
            "provider": {
                "name": pv.name, "base_url": pv.base_url, "model": pv.model,
                "reasoning_effort": pv.reasoning_effort,
                "send_reasoning_effort": pv.send_reasoning_effort,
                "api_key_env": pv.api_key_env, "max_tokens": pv.max_tokens,
                "stream_required": pv.stream_required,
                "state": pv.diagnose(), "ready": pv.ready,
            },
            # 只报"设没设"。`GET /api/settings` 里没有第二处能泄漏密钥的地方。
            # W19-A：密钥现在有两级来源（环境变量 → 家目录文件），
            # 这里额外报"是本机文件里存着，还是只在环境变量里" ——
            # 决定设置页给不给「清掉本机密钥」按钮，以及输入框的占位文案。
            "key_present": bool(pv.api_key),
            "key_from_env": bool(os.environ.get(pv.api_key_env, "").strip()),
            "secrets_path": str(secrets_path()),
            "key_in_secrets_file": self._key_in_secrets_file(pv.api_key_env),
            "model_default": str(DEFAULTS["provider"]["model"]),
            "reasoning_choices": list(REASONING_CHOICES),
            "fields": provider_field_specs(),
            "write_whitelist": ["provider." + k for k in PROVIDER_WRITE_KEYS],
            "config_files": files,
            "env_overrides": env_over,
            "shadowed_by_env": [e["key"] for e in env_over if e["set"]],
        }

    @staticmethod
    def _key_in_secrets_file(name: str) -> bool:
        """家目录密钥文件里有没有 `name` 这个键（**只判在不在，不返回值**）。

        为什么不复用 `read_secret()`：那个函数环境变量优先，所以
        "环境变量设了、文件里没有"和"文件里存着"会给出同一个非空结果，
        而设置页要区分这两件事（决定给不给「清掉」按钮）。
        这里直接读文件、只回布尔 —— 界面上没有任何路径能拿到密钥值。
        """
        path = secrets_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:                               # noqa: BLE001
            return False
        return bool(isinstance(data, dict) and str(data.get(name) or "").strip())

    def magpie_probe(self, endpoint: str = "") -> dict:
        """问 magpie 网关：在不在听、上面有哪些模型。

        ★ 这段**只读**：不改配置、不写盘、不发模型请求。
          界面打开设置页时自动调它，用来把「填一串模型名」变成「点一个」。

        返回里一定带 `online` 布尔与 `note` 一句人话 —— 连不上时界面
        要显示的是**为什么**连不上，而不是一片空白让人以为功能坏了。
        """
        from .. import magpie as M
        ep = (endpoint or "").strip() or f"http://127.0.0.1:{M.DEFAULT_PORT}"
        alive, note = M.probe(ep)
        out: dict = {
            "online": alive,
            "endpoint": ep,
            "note": note,
            "models": [],
            "current_base_url": "",
            "already_on_magpie": False,
        }
        try:
            cfg_now = Config.load()
            out["current_base_url"] = cfg_now.provider.base_url
            out["already_on_magpie"] = bool(
                M.is_magpie_base(cfg_now.provider.base_url))
        except Exception:                                     # noqa: BLE001
            pass
        if alive:
            out["models"] = M.list_models(ep)
        return out

    def settings_save(self, body: dict) -> tuple[dict | None, str]:
        """唯一会改配置文件的入口。校验 → 备份 → 原子写 → **重建 agent** → 回读。

        返回 `(结果字典, "")` 或 `(None, 给用户看的原因)`。
        "任务在跑不许改"那道 409 闸在**路由**上（与 `/api/upload`、
        `/api/material_drop` 同一处判据 `Manager.busy()`），不在这里。
        """
        try:
            clean = parse_provider_updates(body)
        except SettingsError as e:
            return None, str(e)
        # 串行化"读配置 → 合并写 → 重建 agent"：两个标签页同时点保存时，
        # 没有这把锁会各自读到旧文件、后写的覆盖先写的（两份都合法的写入丢一份）。
        with self._settings_lock:
            try:
                res = save_provider_overrides(clean)
            except Exception as e:                      # noqa: BLE001
                return None, f"写入失败，配置没有改动：{type(e).__name__}: {e}"

            self.boot()                                 # ← 换模型的全部"魔法"就这一行
            st = self.status()
            cfg_now = Config.load()
            out = {
                "ok": True,
                "written": res["written"],
                "path": res["path"],
                "backup": res["backup"],
                "provider": st.get("provider") or {},
                "rebuilt": self.kernel is not None,
                "kernel_error": self.kernel_error,
                # 已构造的 agent 真正在用的那个模型 —— 与上面 `provider.model`
                # （现读配置）分开报，才能证明"冻住的那个"真的换了。
                "kernel_model": (self.kernel.cfg.provider.model if self.kernel else ""),
            }
            # 密钥可能在环境变量里，也可能在家目录 secrets.json 里。只查
            # os.environ 会把"刚存进本机文件、还没重启"误报成没有密钥。
            env_now = str(cfg_now.provider.api_key_env or "")
            out["key_present"] = bool(cfg_now.provider.api_key)
            out["key_from_env"] = bool(os.environ.get(env_now, "").strip())
            out["key_in_secrets_file"] = self._key_in_secrets_file(env_now)

            # ── 切到 magpie 时顺手把"密钥"补上 ─────────────────────
            # magpie 网关**接受任意 key**，所以这一步对用户是零风险的；
            # 而不补的后果是他切完模型，界面仍显示「还没有密钥」，
            # 得自己去下面那格敲 "magpie" 才算能用 —— 用户的原话是
            # 「主要是让用户配置的方便一点」，那这一步就该替他做掉。
            #
            # ★ 三个前提缺一不可，任一不满足就**不碰**密钥：
            #   ① 目标 base_url 确实是 magpie（不然会拿 magpie 顶替别人的 key）
            #   ② 当前没有任何可用密钥（有就用他的，绝不覆盖）
            #   ③ 用户没在环境变量里自己设过（那种情况已经能用，别多事）
            try:
                from .. import magpie as _M
                if (_M.is_magpie_base(str(clean.get("base_url") or ""))
                        and not out["key_present"]
                        and not out["key_from_env"]):
                    sres = save_secret(env_now or _M.DEFAULT_KEY_ENV,
                                       _M.PLACEHOLDER_KEY)
                    self.boot()
                    cfg_now = Config.load()
                    out["key_present"] = bool(cfg_now.provider.api_key)
                    out["key_in_secrets_file"] = self._key_in_secrets_file(
                        str(cfg_now.provider.api_key_env or env_now))
                    out["key_autofilled"] = True
                    out["key_note"] = (
                        f"magpie 网关接受任意 key，已自动把 "
                        f"{sres['name']} 存成本机密钥（值就是 "
                        f"「{_M.PLACEHOLDER_KEY}」，方便你以后认出来）。")
                    out["provider"] = self.status().get("provider") or out["provider"]
            except Exception as e:                        # noqa: BLE001
                # 自动补密钥失败**不该让整个切换失败**：模型已经切过去了，
                # 只是还差密钥。如实报告，让用户自己填。
                out["key_autofilled"] = False
                out["key_note"] = (f"没能自动补密钥（{type(e).__name__}: {e}）；"
                                   "请在「接口密钥」里填 magpie 后点保存。")
            return out, ""

    def secret_save(self, body: dict) -> tuple[dict | None, str]:
        """W19-A：把接口密钥存到本机、重建 agent、**并真发一次最小请求验证它**。

        为什么这个接口必须存在：在此之前密钥只从环境变量读，而用户是
        双击桌面窗口跑荣珂（`app.py`）—— 那种进程**没有 shell 环境变量**，
        于是图形界面上永远「未连接」，一个任务都跑不起来。
        存进 `~/.rongke/secrets.json`（家目录，不在仓库里）之后就能用了。

        与 `settings_save` 的三点不同，都是有意的：

          1. **不进 `config.json`**（那在项目根、可能被提交）。见 `save_secret`。
          2. **不比对白名单**：键名是用户当前的 `api_key_env`，不是固定五个。
          3. **真的发一次 LLM 请求**。"已保存"和"能用"是两件事 ——
             用户填错一个字符，保存也会成功。所以这里当场调一次网关，
             把**服务端的原话**带回去，而不是让用户自己去跑个任务才发现。
             ★ 验证失败时**不删刚写进去的值**：那可能是密钥对、只是网关当时抽风
             （本项目早就实测过该网关「间歇性长挂起」，1.5s 与 247s 交替出现）。
             删掉会让用户在"明明是对的"和"又没了"之间反复横跳。
        """
        if not isinstance(body, dict):
            return None, "请求体不是对象"
        name = str(body.get("name") or "").strip()
        value = body.get("value")
        if value is None:
            return None, "缺少 value（要清空就传空字符串，不是省略这个字段）"
        if not name:
            # 没给名字就用当前配置里的变量名 —— 界面上那个框默认就是这个值，
            # 让调用方可以只传 value。
            cfg = Config.load()
            name = cfg.provider.api_key_env

        try:
            res = save_secret(name, str(value))
        except Exception as e:                          # noqa: BLE001
            return None, f"写入失败，密钥没有改动：{type(e).__name__}: {e}"

        out: dict[str, Any] = {
            "ok": True, "path": res["path"], "name": res["name"],
            "chars": res["chars"], "cleared": res["cleared"],
        }
        if res["cleared"]:
            out.update({"verified": False, "verify_error": "已清空，没有可验证的密钥"})
            self.boot()
            out["kernel_ok"] = self.kernel is not None
            return out, ""

        # ── 重建 agent：模型与密钥都在构造时冻住（config.py 的既有接缝）──
        self.boot()
        out["kernel_ok"] = self.kernel is not None
        out["kernel_error"] = self.kernel_error

        # ── 真发一次最小请求 ────────────────────────────────
        try:
            from ..llm import LLM
            cfg = Config.load()
            if not cfg.provider.ready:
                out.update({"verified": False,
                            "verify_error": "配置仍不完整：" + cfg.provider.diagnose()})
                return out, ""
            llm = LLM(cfg.provider)
            r = llm.chat([{"role": "user", "content": "只回答两个字：可用"}], tools=None)
            out["verified"] = True
            out["verify_sample"] = str(getattr(r, "content", r) or "")[:40]
        except Exception as e:                          # noqa: BLE001
            out["verified"] = False
            out["verify_error"] = f"{type(e).__name__}: {e}"
        return out, ""

    def tools(self) -> list[dict]:
        k = self.kernel
        if k is None:
            return []
        out = []
        for name in k.registry.names():
            t = k.registry.get(name)
            if t is None:
                continue
            out.append({
                "name": t.name,
                "description": t.description,
                "domain": t.domain,
                "mutating": t.mutating,
                "source": t.source,
                "params": sorted((t.parameters or {}).get("properties", {}).keys()),
            })
        return out

    # ── 产出物 ──────────────────────────────────────────────
    # 为什么这是产品层最该补的一件：原来的界面只回答"agent 干了什么"
    # （步数、工具、门判定），而科研用户第一个问题永远是
    # **"我的东西在哪 / 能不能用"**。没有这一块，任务跑完 = 屏幕上多了一段文本，
    # 工作区里落了什么文件全靠自己去翻 runs/ 目录。
    # 内核自己的账本，不是产出物。
    # `conversation.json` 是 2026-09-19 新增的对话快照（跨进程续跑的锚点）——
    # 它必须在这里，否则「接着跑用的中间态」会伪装成用户交付的文件出现在产出物清单里。
    # ★ 名字**从 `loop.py` 算出来**，不在这边手抄（2026-09-21）：成功收尾会把快照
    #   改成 `conversation.done.json`，两套名字都得藏 —— 手抄就是 H 型，
    #   改一处忘一处时快照会静默漏进产出物清单。
    INTERNAL = {"session.json", "trace.jsonl", "turns"} | set(SNAPSHOT_INTERNAL)
    MAX_FILES = 300
    MAX_DEPTH = 4        # 相对工作区的路径分量上限（含文件名）

    @staticmethod
    def _inside(p: Path, base: Path) -> bool:
        """p 解析之后是不是真的还在 base 里面。

        **判据必须是结构，不能是字符串前缀。** 第一版探针我用
        `str(p.resolve()).startswith(str(base))`，而 base=`…\\runs\\_symtest`、
        目标=`…\\runs\\_symtest_out` —— 前缀重合，于是"逃出去了"被印成"没逃出去"。
        这和本项目的 J 型（判据换用途不换取向）是同一个形状：
        `relative_to` 才表达"在不在里面"。
        """
        try:
            p.resolve().relative_to(base)
            return True
        except (ValueError, OSError):
            return False

    @classmethod
    def _walk(cls, base: Path, cur: Path, depth: int,
              out: list[tuple[str, Path]]) -> None:
        """有界遍历：深度、条数、以及**目录联接**三重边界。

        为什么不用 `rglob` + 事后过滤：`rglob` 会**走进**目录联接
        （junction，实测 `st_reparse_tag = 0xA0000003`），而且它没有剪枝钩子 ——
        清单里于是混进工作区外的文件名。更糟的是它已经在别人的目录里走完了：
        联接一旦指向一个大目录，光是枚举就够慢。
        所以在**下降之前**就问 `_inside`，不问就不进去。
        """
        if depth >= cls.MAX_DEPTH or len(out) >= cls.MAX_FILES:
            return
        try:
            entries = sorted(cur.iterdir(), key=lambda x: x.name)
        except OSError:
            return
        for p in entries:
            if len(out) >= cls.MAX_FILES:
                return
            name = p.name
            if name.startswith(".") or name in cls.INTERNAL:
                continue
            try:
                # ① 软链一律不跟：它可能是通向工作区外的门（实测本机
                #    `Path.symlink_to()` 会**静默降级成 0 字节普通文件**、
                #    `is_symlink()` 返回 False —— 所以这一条是补充，不是主力）。
                if p.is_symlink():
                    continue
                # ② 主力：解析后出界的一律不算产出物。这一条同时覆盖
                #    目录联接 / 挂载点 / 任何形式的 reparse point。
                if not cls._inside(p, base):
                    continue
                if p.is_dir():
                    cls._walk(base, p, depth + 1, out)
                elif p.is_file():
                    out.append((p.relative_to(base).as_posix(), p))
            except OSError:
                continue

    @classmethod
    def _artifact_files(cls, root: Path) -> list[tuple[str, Path]]:
        """工作区里**人看得见的产出物**。唯一的一处定义 ——
        列表与取回共用它，否则两边各写一套过滤条件就又多了一条真相（B 型）。"""
        out: list[tuple[str, Path]] = []
        try:
            base = root.resolve()
            cls._walk(base, base, 0, out)
        except Exception:
            pass
        return out

    def artifacts(self, rid: str) -> dict:
        """列出一次任务的产出物。

        对**没有归档的旧任务也照列** —— 那个目录里可能什么都没有，也可能
        留着当时写下的文件。定律：**看得见优先于能回放**，
        而且"什么都没有"本身就是要让人看见的事实，不该渲染成"读取失败"。
        """
        if not rid or "/" in rid or "\\" in rid or ".." in rid:
            return {"error": "非法任务 id"}
        root = ws_dir() / rid
        if not root.is_dir():
            return {"id": rid, "exists": False, "files": [], "count": 0,
                    "total_bytes": 0, "workspace": "", "archived": False}
        allowed = self._artifact_index(root)
        rows = []
        for rel, p in sorted(allowed.items()):
            try:
                st = p.stat()
            except Exception:
                continue
            rows.append({
                "path": rel,
                "name": p.name,
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "ext": p.suffix.lower().lstrip("."),
            })
        rows.sort(key=lambda r: r["path"])
        return {
            "id": rid, "exists": True, "files": rows, "count": len(rows),
            "total_bytes": sum(r["size"] for r in rows),
            "workspace": str(root),
            "archived": (root / "session.json").is_file(),
        }

    def _artifact_index(self, root: Path) -> dict[str, Path]:
        """按目录/文件签名复用白名单索引。每次仍 stat，未变化才跳过遍历。

        签名含排除文件：session 由排除变为包含时不应命中陈旧索引。
        条目级 mtime_ns+size 对改动、删除、重命名均敏感，不依赖目录 mtime。
        """
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
            sig = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                        for p in entries)
        except OSError:
            return {}
        key = str(root)
        with self._artifact_lock:
            hit = self._artifact_cache.get(key)
            if hit is not None and hit[0] == sig:
                return dict(hit[1])
        index = {rel: p for rel, p in self._artifact_files(root)}
        with self._artifact_lock:
            if len(self._artifact_cache) >= 128 and key not in self._artifact_cache:
                self._artifact_cache.pop(next(iter(self._artifact_cache)), None)
            self._artifact_cache[key] = (sig, index)
        return dict(index)

    def tavotto_handoff(self, rid: str, rel: str) -> dict:
        """用户点名的图与同名脚本只经产出物围栏；官方工具只执行副本。"""
        why = self.busy()
        if why:
            return {"ok": False, "status": 409, "error": "任务正在运行，结束后再交接图件。"}
        original = self.artifact_path(rid, rel)
        if original is None or original.suffix.lower().lstrip(".") not in self.FIG_EXT:
            return {"ok": False, "status": 400, "error": "图件不存在或不在产出物围栏内"}
        script = self.artifact_path(rid, str(Path(rel).with_suffix(".py")).replace("\\", "/"))
        if script is None:
            return {"ok": False, "status": 400,
                    "error": "未找到同目录、同名的Matplotlib脚本。PDF/PNG本身不能恢复参数化编辑；请先用荣珂生成脚本与图件。"}
        from ..figure_handoff import diagnose, launch
        if not diagnose().get("available"):
            return {"ok": False, "status": 503, "error": diagnose().get("reason") or "Tavotto不可用"}
        contract = self._figure_contract_for(rid, rel)
        if contract is None:
            return {"ok": False, "status": 400, "error": "找不到绑定这张原图的审计契约，请先运行原图的图面体检。"}
        fg = self.kernel.cfg.figure if self.kernel else None
        result = launch(original, script, workspace=ws_dir() / rid,
                        python=(fg.python if fg else "") or "")
        with self._artifact_lock:
            self._artifact_cache.pop(str((ws_dir() / rid).resolve()), None)
        edit_dir = (ws_dir() / rid / result["edit_dir"]).resolve()
        edit_dir.relative_to((ws_dir() / rid / "tavotto").resolve())
        edit_dir.mkdir(parents=True, exist_ok=True)
        (edit_dir / "rongke-handoff.json").write_text(
            json.dumps({"source": rel, "contract": contract}, ensure_ascii=False, indent=1), encoding="utf-8")
        result.update(rid=rid, source=rel, audit="pending",
                      audit_note="交接不等于导出或通过审计。请在Tavotto中导出到独立编辑目录，再点击“复审精修版”。")
        return result

    def _figure_contract_for(self, rid: str, rel: str) -> dict | None:
        """契约只从明确指向这张PDF的原审计报告取得，不从同任务其他JSON猜。"""
        for row in self.artifacts(rid).get("files", []):
            if not str(row.get("path", "")).endswith(".json"):
                continue
            path = self.artifact_path(rid, row["path"])
            if path is None:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                files = raw.get("files") or {}
                pdf = Path(str(files.get("pdf") or ""))
                if not pdf.is_absolute():
                    from_report = (path.parent / pdf).resolve()
                    from_root = (ws_dir() / rid / pdf).resolve()
                    pdf = from_report if from_report == (ws_dir() / rid / rel).resolve() else from_root
                if pdf.resolve() == (ws_dir() / rid / rel).resolve() and isinstance(raw.get("contract"), dict):
                    return raw["contract"]
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return None

    def tavotto_reaudit(self, rid: str, rel: str) -> dict:
        why = self.busy()
        if why:
            return {"ok": False, "status": 409, "error": "任务正在运行，结束后再复审图件。"}
        original = self.artifact_path(rid, rel)
        if original is None or original.suffix.lower() != ".pdf" or "/tavotto/" not in "/" + rel.replace("\\", "/"):
            return {"ok": False, "status": 400, "error": "请选择本任务tavotto独立编辑目录中已导出的PDF"}
        from ..figure import FigureAuditor
        from ..figure_handoff import reaudit
        handoff = self.artifact_path(rid, str(Path(rel).parent / "rongke-handoff.json").replace("\\", "/"))
        if handoff is None:
            return {"ok": False, "status": 400, "error": "精修目录缺少荣珂交接回执，不能猜用其他图的契约。"}
        try:
            record = json.loads(handoff.read_text(encoding="utf-8"))
            contract = record["contract"]
            source = str(record["source"])
            if original.stem != Path(source).stem:
                raise ValueError("回执与导出图不同名")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return {"ok": False, "status": 400, "error": f"交接回执无效：{exc}"}
        if contract is None:
            return {"ok": False, "status": 400, "error": "找不到该图的原始契约，不能编造审计标准；请先运行原图的图面体检。"}
        fg = self.kernel.cfg.figure if self.kernel else None
        if fg is None:
            return {"ok": False, "status": 503, "error": "图面审计装置未初始化"}
        auditor = FigureAuditor(python_exe=fg.python or None, skills_dir=fg.skills_dir or None,
                                timeout=fg.timeout)
        result = reaudit(workdir=original.parent, original=original, contract=contract, auditor=auditor)
        with self._artifact_lock:
            self._artifact_cache.pop(str((ws_dir() / rid).resolve()), None)
        return result

    def artifact_path(self, rid: str, rel: str) -> Path | None:
        """把 (任务 id, 相对路径) 解成一个**确定在工作区内**的真实文件。

        裁决只在这里做一次：先按唯一那份文件清单核对，再 resolve 复核。
        两道都要 —— 只核清单挡不住 `a/../b` 这种穿出去的写法，
        只 resolve 又会把内核自己的账本文件也放出去。
        """
        if not rid or "/" in rid or "\\" in rid or ".." in rid:
            return None
        root = (ws_dir() / rid)
        if not root.is_dir():
            return None
        want = str(rel or "").replace("\\", "/").lstrip("/")
        if not want:
            return None
        for listed, p in self._artifact_index(root).items():
            if listed == want:
                try:
                    real = p.resolve()
                    real.relative_to(root.resolve())     # 二次复核：真在工作区内
                except Exception:
                    return None
                return real if real.is_file() else None
        return None


    # ══════════════ W1 只读接口：能力 / 门禁 / 交付物 / 图表 ══════════════
    #
    # 工单：`00_总控/qoder-orchestration/W1-服务端只读接口.md`（上位 S0 §3 的新导航）。
    # 四个接口**全是 GET、纯读**：数据从既有结构（history/artifacts/gate 模块
    # docstring）现算，不新增状态、不写文件、不联网、不加依赖。
    # 门禁文案**逐字取自 `rongke/gate.py` 的模块 docstring** —— 那是内核真实实现，
    # 前端抄一份措辞就是第二套真相（S0 纪律第 4 条）。

    #: `/api/capabilities` 的条目（S0 §2 那张 14 行的表，一项不少；tools+hooks 拆两条）。
    #: `file` 相对仓库根，请求时逐个 `exists()` 核实 —— 不许把"我以为有"写成 live。
    CAPABILITIES = [
        {"group": "kernel", "id": "contract", "name": "目标契约",
         "file": "rongke/contract.py",
         "digest": "把一句话目标固化成交付物/验收标准/约束/禁止，常驻注入防漂移",
         "wired": ["view:chat", "view:traj"]},
        {"group": "kernel", "id": "loop", "name": "主循环 Plan→Act→Observe→Verify",
         "file": "rongke/loop.py",
         "digest": "推进任务每一步，每步的工具调用都记进过程记录，交付前交给 9 道检查核验",
         "wired": ["view:chat"]},
        {"group": "kernel", "id": "context", "name": "上下文经济",
         "file": "rongke/context.py",
         "digest": "五级退化阶梯：长任务的上下文按固定次序逐级压缩，不静默截断",
         "wired": []},
        {"group": "kernel", "id": "stages", "name": "阶段路由 + 专属技能",
         "file": "rongke/stages.py",
         "digest": "每个阶段有且只有一个专属技能，一对一；杜绝多技能抢同一件事",
         "wired": ["view:tools"]},
        {"group": "kernel", "id": "pitfalls", "name": "它自带的技能（踩坑纪律）",
         "file": "rongke/pitfalls.py",
         "digest": "30 条实测踩坑的判决表，经 load_skill 装载、受阶段技能门保护",
         "wired": []},
        {"group": "gate", "id": "gate", "name": "完成前的 9 道检查",
         "file": "rongke/gate.py",
         "digest": "说完成必须交可追溯证据：8 条硬门 + 1 条软检查，逐条定义见 /api/gates",
         "wired": ["view:traj"]},
        {"group": "gate", "id": "writing", "name": "写作体检",
         "file": "rongke/writing.py",
         "digest": "程序化核稿面：体量/结构/句长/套话/标点/引注等逐项检查，只核客观事实、不评论证",
         "wired": ["view:chat"]},
        {"group": "gate", "id": "figure", "name": "图面契约与渲染后审计",
         "file": "rongke/figure.py",
         "digest": "源码预检 / PDF 字形下限 / 渲染后碰撞 / 多分面对齐 + 图注体检，落盘体检报告",
         "wired": []},
        {"group": "gate", "id": "citation", "name": "引文核验",
         "file": "rongke/citation.py",
         "digest": "核验 DOI / 元数据 / 撤稿状态，防编造与失效引用",
         "wired": []},
        {"group": "domain", "id": "tools", "name": "工具清单与过程记录",
         "file": "rongke/tools.py",
         "digest": "所有工具的登记、调用与全量留痕；核对交付证据看的就是这份过程记录",
         "wired": ["view:tools"]},
        {"group": "domain", "id": "hooks", "name": "钩子围栏",
         "file": "rongke/hooks.py",
         "digest": "工具调用前后的拦截钩子，围栏判据落在服务端而不是界面",
         "wired": ["view:tools"]},
        {"group": "domain", "id": "preset", "name": "组件技能（组会 PPT 等）",
         "file": "rongke/preset.py",
         "digest": "预置工作流与专属体检的打包件（deck 体检即第 9 条检查的数据源）；"
                   "能力预设经 caps.py 装配，健康度见 /api/presets",
         "wired": ["view:chat"]},
        {"group": "infra", "id": "mcp_client", "name": "MCP 客户端",
         "file": "rongke/mcp_client.py",
         "digest": "连接外部 MCP 服务端，把第三方工具接进同一份工具清单与过程记录",
         "wired": []},
        {"group": "infra", "id": "memory", "name": "跨任务记忆",
         "file": "rongke/memory.py",
         "digest": "任务历史与经验沉淀，后续任务可引用先前任务的记录",
         "wired": ["view:hist"]},
        {"group": "infra", "id": "artifacts", "name": "交付物清点",
         "file": "rongke/web/server.py",
         "digest": "列一次任务的产出物并经唯一围栏取回（能被列出来的才能被取回）",
         "wired": ["view:chat"]},
    ]
    CAP_GROUP_NAMES = {"kernel": "它怎么干活", "gate": "质量把关",
                       "domain": "工具与技能", "infra": "配套服务"}
    # S0 §5″ 的分类表（按代码真实性质标，不许自己重排）：条号 → (kind, 类别)
    GATE_CLASS = {1: "证据类（硬门）", 2: "证据类（硬门）", 3: "证据类（硬门）",
                  4: "证据类（硬门）", 5: "软检查", 6: "阶段技能门（硬门）",
                  7: "写作体检门（硬门，最多打回 1 次）", 8: "图面门（硬门）",
                  9: "deck 门（硬门）"}
    #: 图类扩展名（`/api/figures` 用；与图面体检的审计对象口径一致）
    FIG_EXT = ("png", "pdf", "svg", "jpg", "jpeg")

    def capabilities(self) -> dict:
        """内核能力清单。`live` 的判据就一条：**那份文件真的在盘上**（逐个现查）。
        查不到 ⇒ `live:false` 并如实给 `why`，不冒充。"""
        root = PKG.parent
        grouped: dict[str, list[dict]] = {}
        n_live = 0
        for it in self.CAPABILITIES:
            p = root / it["file"]
            exists = p.exists()
            n_live += 1 if exists else 0
            row = {"id": it["id"], "name": it["name"], "file": it["file"],
                   "digest": it["digest"], "live": exists, "wired": list(it["wired"])}
            if not exists:
                row["why"] = f"文件不存在：{it['file']}（相对仓库根解析不到）"
            grouped.setdefault(it["group"], []).append(row)
        groups = [{"id": gid, "name": self.CAP_GROUP_NAMES.get(gid, gid), "items": rows}
                  for gid, rows in grouped.items()]
        return {"groups": groups, "count": len(self.CAPABILITIES), "live": n_live}

    @staticmethod
    def gate_defs() -> tuple[list[dict], str]:
        """9 条判定的**静态定义**：逐字读 `gate.py` 的模块 docstring，不写死条数。
        docstring 里形如 `N. 文案` 的行即一条判定；更深缩进的行是它的续行
        （第 7 条就折了两行），拼接但不改写。返回 `(defs, 错误文案)`。"""
        try:
            from .. import gate as _g
        except Exception as e:                      # 内核 import 不了也要说实话
            return [], f"gate 模块导入失败：{type(e).__name__}: {e}"
        doc = _g.__doc__ or ""
        defs: list[dict] = []
        cur: dict | None = None
        base_indent = 0
        for line in doc.splitlines():
            m = re.match(r"^\s*(\d+)\.\s+(.*)$", line)
            if m:
                n = int(m.group(1))
                cur = {"n": n, "text": m.group(2).strip(),
                       "kind": "soft" if n == 5 else "hard",
                       "class": Manager.GATE_CLASS.get(n, "硬门")}
                defs.append(cur)
                base_indent = len(line) - len(line.lstrip())
                continue
            if (cur is not None and line.strip()
                    and (len(line) - len(line.lstrip())) > base_indent):
                cur["text"] += "\n" + line.strip()   # 续行：拼接原文，不改一个字
        return defs, ("" if defs else "gate.py docstring 里没解析出任何判定")

    def _last_gate_verdict(self) -> tuple[dict | None, str]:
        """最近一次门判定：优先内存里跑完的 Run，其次磁盘归档（服务重启也在）。
        没有就返回 `None` + hint —— 不许编一个空的假结果（W1 §2.2）。"""
        cands: list[Run] = [r for r in list(self.runs.values())
                            if r.done and isinstance(r.result, dict)]
        seen = {r.id for r in cands}
        try:
            for h in self.history(50):
                rid = str(h.get("id") or "")
                if rid and rid not in seen:
                    run = self._from_archive(rid)
                    if run is not None and isinstance(run.result, dict):
                        cands.append(run)
        except Exception:
            pass
        if not cands:
            return None, "还没有跑过任务 —— 跑一次任务后这里显示最近一道门的判定"
        run = max(cands, key=lambda r: r.finished or r.started or 0)
        v = (run.result or {}).get("verdict")
        if not isinstance(v, dict):
            return None, f"最近一次任务（{run.id}）没走到门判定（未宣布完成或失败在半路）"
        return {"rid": run.id, "goal": run.goal,
                "passed": bool(v.get("passed")),
                "reasons": list(v.get("reasons") or []),
                "warnings": list(v.get("warnings") or []),
                "lint": v.get("lint")}, ""

    def gates(self) -> dict:
        defs, err = self.gate_defs()
        last, hint = self._last_gate_verdict()
        out: dict = {
            "defs": defs,
            "source": "rongke/gate.py 模块 docstring（逐字现读）",
            "last": last,
        }
        if err:
            out["error"] = err
            out["hint"] = "门禁定义读不出来 —— 检查 rongke/gate.py 是否完好"
        elif hint:
            out["hint"] = hint
        return out

    def _artifact_url(self, rid: str, rel: str) -> str:
        """取回 URL **只有既有那一条通路**（`/api/artifact?id=…&path=…`，
        即工单示例里写的 `rid=/rel=`，实现以现有路由为准）。
        新开下载通路 = 围栏多一个口子，不做。"""
        return (f"/api/artifact?id={urllib.parse.quote(str(rid))}"
                f"&path={urllib.parse.quote(str(rel))}")

    def deliverables(self, n: int = 100) -> dict:
        """跨任务交付物总览：遍历 history() 的每一条 rid，调既有 artifacts(rid)。
        老任务没归档**照常出现**、files 为空即可 —— 看得见优先于能回放（web_test 开头定律）。"""
        items: list[dict] = []
        by_kind: dict[str, int] = {}
        errors: list[str] = []
        for h in self.history(n):
            rid = str(h.get("id") or "")
            if not rid:
                continue
            try:
                a = self.artifacts(rid)
            except Exception as e:
                a = {"files": [], "archived": False}
                errors.append(f"{rid}: {type(e).__name__}: {e}")
            if a.get("error"):
                errors.append(f"{rid}: {a['error']}")
            files: list[dict] = []
            for f in a.get("files", []):
                kind = str(f.get("ext") or "").lower().lstrip(".") or "other"
                files.append({"rel": f["path"], "kind": kind,
                              "bytes": int(f.get("size") or 0),
                              "mtime": f.get("mtime") or "",
                              "url": self._artifact_url(rid, f["path"])})
                by_kind[kind] = by_kind.get(kind, 0) + 1
            items.append({"rid": rid, "goal": str(h.get("goal") or ""),
                          "when": str(h.get("ts_iso") or ""),
                          "archived": bool(a.get("archived")), "files": files,
                          # history() 已判过「有没有交付正文」—— 原样带出去，
                          # 「成品」区靠它把只有正文没落文件的任务也摆上橱窗
                          "has_draft": bool(h.get("has_draft"))})
        out: dict = {"total": sum(by_kind.values()), "by_kind": by_kind,
                     "items": items}
        if errors:
            out["errors"] = errors
            out["hint"] = "个别任务的产出物目录没读全 —— 上面的列表是真的，缺的照实列"
        return out

    def figures(self, n: int = 100) -> dict:
        """历次配图资产 + 它们的图面体检报告，两者并排。

        ★ `fresh` 的实现是**复用内核代码**，不是近似：直接调用
        `figure.FigureAuditor._freshness()`（`figure.py:1175` 的 @staticmethod，
        内核新鲜度第一层本体）——报告 mtime 必须不早于它审的对象（容差 `_FRESH_SLACK_S`）。
        这里只负责把"报告文件与被审对象"递给它，判据一个字都没重写。
        ⚠️ 诚实的边界：新鲜度的**第二层**（gate.py:562-579，交付时把审计记下的
        `artifact_mtimes` 与磁盘现状对表）**内联在 `CompletionGate.check()` 里，
        不修改 gate.py（禁改）就抽不出来**，本接口对它**不重算**。已在响应里明说。"""
        try:
            from ..figure import FigureAuditor
        except Exception as e:
            return {"error": f"figure 模块导入失败：{type(e).__name__}: {e}",
                    "hint": "检查 rongke/figure.py 是否完好", "items": []}
        items: list[dict] = []
        errors: list[str] = []
        for h in self.history(n):
            rid = str(h.get("id") or "")
            if not rid:
                continue
            try:
                a = self.artifacts(rid)
            except Exception as e:
                errors.append(f"{rid}: {type(e).__name__}: {e}")
                continue
            # 同一份白名单同时服务列图、读报告与新鲜度检查；不重复扫目录。
            allowed = self._artifact_index(ws_dir() / rid)
            figs: list[dict] = []
            audits: list[dict] = []
            for f in a.get("files", []):
                rel = str(f["path"])
                ext = str(f.get("ext") or "").lower().lstrip(".")
                name = Path(rel).name.lower()
                if ext in self.FIG_EXT:
                    figs.append({"rel": rel, "bytes": int(f.get("size") or 0),
                                 "mtime": f.get("mtime") or "",
                                 "url": self._artifact_url(rid, rel)})
                elif ext == "json" and ("audit" in name or name in ("figure-qa.json",
                                                                     "figure-qa-tavotto.json", "deck-qa.json")):
                    # 体检报告：走**同一道围栏**取回再解析（artifact_path 是唯一通路）
                    row: dict = {"rel": rel, "verdict": "UNREADABLE",
                                 "at": f.get("mtime") or ""}
                    p = allowed.get(rel)
                    if p is not None:
                        try:
                            rep = json.loads(p.read_text(encoding="utf-8"))
                            if isinstance(rep, dict):
                                row["verdict"] = str(rep.get("verdict") or "")
                                row["at"] = str(rep.get("audited_at") or row["at"])
                                row["detail"] = {k: v for k, v in rep.items()
                                                 if k not in ("gate_details", "contract",
                                                              "legend_findings", "notes")}
                        except Exception as e:
                            row["verdict"] = f"PARSE-ERROR: {type(e).__name__}"
                    else:
                        row["why"] = "报告不在产出物围栏清单里（列不出即不读）"
                    audits.append(row)
            item: dict = {"rid": rid, "goal": str(h.get("goal") or ""),
                          "when": str(h.get("ts_iso") or ""),
                          "figs": figs, "audits": audits}
            # fresh：**调用内核的 `_freshness`**（第一层）。判据不在这里重写。
            if not audits:
                item["fresh"] = None
                if figs:
                    item["hint"] = "这些图没过图面体检（没有体检报告）"
            else:
                subject = None
                for g in figs:
                    if str(g["rel"]).lower().endswith(".pdf"):
                        subject = allowed.get(g["rel"])
                        if subject is not None:
                            break
                if subject is None and figs:
                    subject = allowed.get(figs[0]["rel"])
                rep_paths = [allowed.get(r["rel"]) for r in audits]
                rep_paths = [x for x in rep_paths if x is not None]
                findings = FigureAuditor._freshness(rep_paths, subject)
                item["fresh"] = not any(f.level == "FAIL" for f in findings)
                item["stale_findings"] = [f.message for f in findings
                                          if f.level == "FAIL"]
                item["fresh_basis"] = (
                    "复用 figure.FigureAuditor._freshness（新鲜度第一层判定：报告 mtime "
                    "不得早于被审对象）。gate.py 第 8 条的交付时第二层核对内联在 "
                    "check() 里、依赖当次运行的工具调用记录，归档任务不重算 —— "
                    "它当时判没判过期，见 /api/gates 的 last。")
            if figs or audits:
                items.append(item)
        try:
            from ..figure_handoff import diagnose
            tavotto = diagnose()
        except Exception as exc:
            tavotto = {"available": False, "reason": f"Tavotto能力探测失败：{exc}"}
        out: dict = {"items": items, "tavotto": tavotto}
        if errors:
            out["errors"] = errors
            out["hint"] = "个别任务的目录没读全 —— 列出的都是真的"
        return out

# ══════════════════════ HTTP ══════════════════════
class Handler(BaseHTTPRequestHandler):
    server_version = "RongkeWeb/0.1"

    def log_message(self, *a):        # 静音：stdout 留给启动信息
        pass

    @property
    def mgr(self) -> Manager:
        return self.server.mgr          # type: ignore[attr-defined]

    # ── 基础 ────────────────────────────────────────────────
    def _send(self, code: int, body, ctype: str = "application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            # ★ 客户端先走了（超时断开 / 关页面 / 被杀掉的探针）。
            #
            # 这不是服务端的错，也不该变成一段 traceback：
            # `socketserver` 会把每个请求线程里的异常整个打到 stderr，
            # 而回归是**按日志里有没有 Traceback 判**的 —— 于是"用户关了
            # 页面"这种正常事会把一次全绿的回归判成失败。
            # 实测踩过：`/api/orig` 响应写得慢一点，探针那边先超时走了，
            # 服务端照样把整段栈打出来，回归的判定被这行日志带偏。
            #
            # 只吞"连不上/断开了"这一类，**不吞**别的 OSError ——
            # 磁盘满、编码错、响应写了一半，那些是真问题，必须照旧冒出去。
            if not self._is_disconnect():
                raise

    def _is_disconnect(self) -> bool:
        """这次异常是不是"对端已经走了"。

        在 `except` 块里调，靠 `sys.exc_info()` 取当前异常。
        `BrokenPipeError` / `ConnectionResetError` / `ConnectionAbortedError`
        都是 `OSError` 的子类；WinError 10053/10054 也落在这里。
        """
        import sys as _sys
        e = _sys.exc_info()[1]
        return isinstance(e, (BrokenPipeError, ConnectionResetError,
                              ConnectionAbortedError))

    # ── 会话 / 登录 ─────────────────────────────────────────
    def _token(self) -> str:
        """从请求里取会话令牌。

        走**请求头**而不是 cookie：
        - 控制台是单文件前端，`localStorage` 一行就能存取，cookie 要处理
          Path / SameSite / 各种跨端口差异，全是与需求无关的复杂度；
        - 头里带令牌不会顺手被浏览器带到别的站点去（cookie 会）。
        也接受 cookie —— 有人可能想用浏览器直接开接口看数据。
        """
        t = self.headers.get("X-Rongke-Token") or ""
        if t:
            return t.strip()
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "rongke_token":
                return v.strip()
        return ""

    def _auth_state(self) -> tuple[bool, bool]:
        """返回 (要不要登录, 当前这次请求过没过)。

        没设密码 ⇒ (False, True)：本地免登录是单机工具的默认，
        **不是漏洞**；设了密码才叫上了锁。这两件事界面上都要说清。
        """
        mgr = getattr(self, "server", None)
        b = getattr(getattr(mgr, "mgr", None), "billing", None)
        if b is None:
            return (False, True)          # 账单子系统起不来时不锁死控制台
        try:
            need = bool(b.account().has_password())
        except Exception:                             # noqa: BLE001
            return (False, True)
        if not need:
            return (False, True)
        return (True, bool(b.token_valid(self._token())))

    def _require_login(self) -> bool:
        """需要登录却没登录 ⇒ 回 401 并返回 False。"""
        need, ok = self._auth_state()
        if need and not ok:
            self._json(401, {
                "error": "需要登录",
                "login_required": True,
                # 明确说去哪登录，别让前端猜
                "hint": "POST /api/login {password}",
            })
            return False
        return True


    def _library_by_ids(self, ids):
        """按ID对账共享库；不把任一检索窗口之外误判为不存在。

        ★ W36（2026-10-31）修一个**会让挂载功能整个失效**的缺陷：
          这里原来传 `status="all"` 给 `_collect`。但 `_collect` 的判据是
          `if not status and "kb_index" in tools` —— 传了任何**非空** status 就退回
          `kb_list` 那条路；而 `kb_list` 把 `status` 当**真过滤条件**（只认
          unread/read…），`"all"` 在那里匹配 **0 条**。
          于是 `rows=[]` ⇒ 所有 id 都判"共享库里没有" ⇒ 「把这篇挂到课题上」
          永远 404。实测现场：`kb/list` 明明列得出 `kb0372eadd`，挂载却回
          「共享库中没有这篇文献」。
          修法：不传 status（空串 ⇒ 走 `kb_index` 全量盘点，也允许退回 kb_list 且
          不乱过滤）。要全部文献，**不传过滤**才是"全部"，不是传个 "all" 去求它。
        """
        wanted = set(ids)
        if not wanted:
            return {}
        got = library_api._collect(self.mgr.kb_fetch, "", "", "", self.mgr.kb_names())
        if "fail" in got:
            return None
        root = str(got.get("root") or self.mgr.kb_root() or "").strip()
        found = {str(item.get("id")): item for item in
                 (library_api._item(row, root) for row in got.get("rows", []))
                 if str(item.get("id")) in wanted}
        if library_api._int(got.get("matched"), 0) > len(got.get("rows", [])):
            for did in wanted - set(found):
                # 全量索引不可用且列表截断时，用精确读取核实；不是拿列表当预言机。
                d = self.mgr.kb_fetch("kb_read", doc_id=did, pages=[1], max_chars=1)
                if isinstance(d, dict) and (d.get("ok") or d.get("empty_page")):
                    found[did] = {"id": did, "title": did, "year": "", "status": "unknown",
                                  "fulltext": True, "has_pdf": (Path(root) / "pdfs" / (did + ".pdf")).is_file()}
                elif not isinstance(d, dict) or not d.get("not_found"):
                    return None
        return found

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False))

    # ── GET ─────────────────────────────────────────────────
    def do_GET(self):
        """GET 的**兜底外壳**：任何没被路由自己接住的异常，都要变成可读的 500。

        ★ 为什么需要它（W30 实测）：`BaseHTTPRequestHandler` 不会替我们接异常 ——
          do_GET 里抛出去的东西会让 `socketserver` 直接**关掉这条连接**，
          客户端拿到的是 `RemoteDisconnected`，既不是 500 也不是可读的错误。
          症状看起来像"网络断了/界面卡死"，而真正的原因（比如一个坏的
          `int()`）只出现在服务端 stderr 里。
        ★ 和"坏输入降级"的分工：坏**入参**仍该在各路由里降级成 200/400
          （那是本仓一贯口径，见 `/api/runs` 的注释）；这层只管"漏网的异常"，
          它出现就说明有一条路我们没料到 —— 如实报 500 + 异常类型，不撒谎。
        """
        try:
            return self._route_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 对端先走了：不是错误，也不该写响应（连接已经没了）
            return
        except RequestError as e:
            self._json(e.status, {"ok": False, "error": e.detail})
        except Exception as e:                             # noqa: BLE001
            self._internal_error("GET", e)

    def _internal_error(self, verb: str, e: BaseException) -> None:
        """把漏网异常回成 500（尽力而为：连接可能已经不可写）。"""
        try:
            self._json(500, {
                "error": f"{type(e).__name__}: {e}",
                "hint": f"{verb} {urllib.parse.urlparse(self.path).path} "
                        f"未捕获异常 —— 这是缺陷，不是你的入参问题。"
                        f"详情见服务端 stderr。"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception:                                  # noqa: BLE001
            # 连 500 都发不出去：至少别把栈埋掉
            import traceback
            traceback.print_exc()

    def _route_get(self):
        u = urllib.parse.urlparse(self.path)
        p, qs = u.path, urllib.parse.parse_qs(u.query)

        if p in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if p.startswith("/static/"):
            # 只发明确列出的同源静态资产，绝不把本路由变成磁盘浏览器。
            assets = {"tokens.css", "workspace.css", "reader.css", "layout.css",
                      "library.css", "mentor.css",
                      "core.js", "workspace.js", "library.js", "mentor.js", "reader.js"}
            name = p[len("/static/"):]
            if name not in assets:
                return self._json(404, {"error": "静态资源不存在"})
            f = (HERE / "static" / name).resolve()
            try:
                f.relative_to((HERE / "static").resolve())
            except ValueError:
                return self._json(404, {"error": "静态资源不存在"})
            if not f.is_file():
                return self._json(404, {"error": "静态资源不存在"})
            ctype = "text/css; charset=utf-8" if f.suffix == ".css" else "text/javascript; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype)
        if p == "/favicon.ico":
            # 桌面 app 模式（Edge `--app`）的窗口与任务栏图标**只**取自这里 ——
            # 原来回 204，等于让一个有自己身份的应用长着一张浏览器的脸。
            # 资产由 `python app_icon.py` 从设计令牌重画（派生物，不入库手做 png）。
            ico = HERE / "app.ico"
            if not ico.exists():
                return self._send(204, b"")      # 没生成过也不该 500
            return self._send(200, ico.read_bytes(), "image/x-icon")
        if p == "/api/build":
            # "你跑的是哪份代码"必须问得出来。见 `build_stamp` 的说明。
            return self._json(200, build_stamp())
        # ★ 登录相关的三个端点**必须在门禁之前**：
        #   否则前端连"要不要登录"都问不出来（会拿到 401 而不是状态），
        #   登录界面就没法自己决定显不显示。
        if p == "/api/auth":
            need, ok = self._auth_state()
            return self._json(200, {"login_required": need, "signed_in": ok})
        # 门禁：设了密码而这次请求没带有效令牌 ⇒ 一律 401。
        # 只拦 `/api/` —— 首页（登录界面本身）与图标必须放行。
        if p.startswith("/api/") and not self._require_login():
            return
        if p == "/api/status":
            return self._json(200, self.mgr.status())
        if p == "/api/license":
            return self._json(200, self.mgr.license_state())
        if p == "/api/tools":
            return self._json(200, {"tools": self.mgr.tools()})
        if p == "/api/presets":
            # 只读挂载健康清单。判健康**只认 MountReport.broken**：
            # 不查文件在不在、不查 JSON 能不能解析，坏件照样带着理由返回。
            from ..caps import all_presets_health
            return self._json(200, all_presets_health())
        if p == "/api/runs":
            # ★ `int()` 必须包住（2026-10-12 冒烟抓到）：原先 `?n=abc` 会抛
            #   ValueError，而它在 do_GET 里没人接 —— 结果是**连接被直接掐断**
            #   （客户端只看到 RemoteDisconnected），不是 500、更不是可读的 400。
            #   一个坏查询参数不该让整条连接消失：降级到默认值 30 更合适，
            #   这也是本文件其它地方的统一口径（坏输入降级，不炸）。
            try:
                n = int((qs.get("n", ["30"])[0]) or 30)
            except (TypeError, ValueError):
                n = 30
            return self._json(200, {"runs": self.mgr.history(n)})
        if p == "/api/artifacts":
            return self._json(200, self.mgr.artifacts(qs.get("id", [""])[0]))
        if p == "/api/materials":
            # 刷新页面后材料还在 —— 否则用户递完材料、页面一动，清单就凭空消失，
            # 而文件其实躺在盘上（B 型：界面与事实各说各话）。
            return self._json(200, {
                "materials": self.mgr.materials,
                "busy": self.mgr.busy(),
                "inbox": str(self.mgr.inbox) if self.mgr.inbox else "",
                # 上次提交时没搬进工作区的那些：留着让前端如实说，而不是当没发生
                "leftover": self.mgr.inbox_leftover,
            })
        if p == "/api/artifact":
            return self._artifact(qs)
        if p == "/api/orig/open":
            # 「原页阅读」第一步：算出这份文件有多少页、渲染要多久，
            # 但**不自作主张开始渲** —— 几十页的论文要十几秒，
            # 让用户自己点「开始渲染」比对着转圈等要好。
            return self._json(200, self._orig_target(qs, probe=True))
        if p == "/api/orig/render":
            return self._json(200, self._orig_render(qs))
        if p == "/api/orig/page":
            return self._orig_page(qs)
        if p == "/api/orig/raw":
            return self._orig_raw(qs)
        if p == "/api/kb/stat":
            # 覆盖度 + 「哪些能力现在可用」。**只读，且不触发任何模型调用**。
            return self._json(200, self.mgr.kb_stat(
                library=(qs.get("library", [""])[0] or "")))
        if p == "/api/kb/list":
            # ★ 参数解析、筛选、排序、**服务端翻页**全在 `library_api.list_payload`，
            #   这里只把 query 原样递过去 —— 那层写成纯函数（注入 fetch），
            #   所以它有条能脱开 HTTP 的独立单测（`library_api_test.py`）。
            # ★ 旧参数 `?limit=abc` 必须**不断连、不 5xx**（`routes_test.py` 钉着
            #   这一条，`/api/runs?n=abc` 是同一个坑的历史）：新层对 `limit` 仍然
            #   宽容降级，只对新参数（page/page_size/sort/order/source）严格 400。
            lib = qs.get("library", [""])[0] or ""
            if lib and (lib in (".", "..") or "/" in lib or "\\" in lib
                        or not Path(lib).name):
                return self._json(400, {"ok": False, "error": "library 必须是一个库名，不能是路径"})
            code, body = library_api.list_payload(
                self.mgr.kb_fetch, qs=qs, library=lib,
                tools=self.mgr.kb_names(),
                library_root=lambda: self.mgr.kb_root(lib))
            return self._json(code, body)
        if p == "/api/kb/read":
            # 按页读正文。**转发** `kb_read`（面板不自己抽文本、不自己截页）。
            # 错误口径见 `library_api.read_payload` 的文档：空 id / 非法数字 /
            # 越界 / 跨度超限 → 400；工具失败 → 5xx，**绝不冒充空文档**。
            code, body = library_api.read_payload(
                self.mgr.kb_fetch,
                doc_id=qs.get("doc_id", [""])[0],
                page_from=qs.get("page_from", [""])[0],
                page_to=qs.get("page_to", [""])[0],
                library=(qs.get("library", [""])[0] or ""),
                tools=self.mgr.kb_names())
            return self._json(code, body)
        if p == "/api/kb/search":
            kw = (qs.get("q", [""])[0] or "").strip()
            # 同上：包住 `int()`，坏值降级到默认 20（W30 冒烟实测 `?n=abc` 会断连）。
            try:
                n = int((qs.get("n", ["20"])[0]) or 20)
            except (TypeError, ValueError):
                n = 20
            if not kw:
                # 空查询拒掉，而不是回一个空列表 —— 空列表会被读成"库里没这段内容"。
                return self._json(400, {"ok": False, "error": "q 为空"})
            return self._json(200, self.mgr.kb_query(
                "kb_search", library=(qs.get("library", [""])[0] or ""),
                query=kw, n=max(1, min(n, 50))))
        if p in ("/api/topic/doc", "/api/topic/blockers"):
            # 正文窗口与卡点筛选只在 topic.py 里定义一次；Web 不另读台账文件。
            slug = (qs.get("slug", [""])[0] or "").strip()
            if not slug:
                return self._json(400, {"ok": False, "error": "slug 不能为空；请先选择课题"})
            try:
                offset = int(qs.get("offset", ["0"])[0] or 0)
                limit = int(qs.get("limit", ["12000" if p.endswith("/doc") else "100"])[0] or 0)
                if offset < 0 or limit < 1:
                    raise ValueError("offset 必须非负，limit 必须大于 0")
                from .. import topic as topic_store
                if p.endswith("/doc"):
                    doc = (qs.get("doc", [""])[0] or "").strip()
                    if doc not in topic_store.DOC_FILE:
                        return self._json(400, {"ok": False, "error": "doc 只能是 plan / reading / evidence"})
                    if limit > topic_store.DOC_WINDOW_MAX:
                        return self._json(400, {"ok": False, "error": f"单次最多读取 {topic_store.DOC_WINDOW_MAX} 字"})
                    body = topic_store.read_doc(slug, doc, offset=offset, limit=limit)
                else:
                    if limit > 200:
                        return self._json(400, {"ok": False, "error": "单次最多读取 200 条卡点"})
                    body = topic_store.blocker_page(
                        slug, status=qs.get("status", ["open"])[0],
                        offset=offset, limit=limit)
                    tdir = topic_store.topics_root() / slug
                    if topic_store.read_topic(slug) is None:
                        body = {"ok": False, "not_found": not tdir.is_dir(),
                                "reason": "no_topic" if not tdir.is_dir() else "invalid_topic",
                                "error": f"课题不存在: {slug!r}" if not tdir.is_dir()
                                         else f"课题台账损坏或不可读: {slug!r}"}
            except (TypeError, ValueError) as e:
                return self._json(400, {"ok": False, "error": str(e)})
            except OSError as e:
                return self._json(500, {"ok": False, "error": f"课题台账读不动：{e}"})
            if not body.get("ok"):
                return self._json(404 if body.get("not_found") else 400, body)
            return self._json(200, body)
        if p == "/api/topic/library":
            from .. import topic as topic_store
            slug = (qs.get("slug", [""])[0] or "").strip()
            t = topic_store.read_topic(slug)
            if t is None:
                return self._json(404, {"ok": False, "error": "课题不存在或台账不可读"})
            docs = self._library_by_ids(t.get("library", []))
            if docs is None:
                return self._json(502, {"ok": False, "error": "共享文献库读取失败"})
            ids = t.get("library", [])
            return self._json(200, {"ok": True, "slug": slug, "items": [docs[x] for x in ids if x in docs],
                                    "missing_ids": [x for x in ids if x not in docs], "total": len(ids)})
        if p == "/api/topic":
            # 导师模式的课题面板（§3.5 最小接线）：读的是内核那份 `topics/` 台账，
            # 服务端**不缓存、不另算** —— 「当前课题」有两处真相就必然漂一处。
            body = self.mgr.topic_tool("topic_status", slug=(qs.get("slug", [""])[0] or ""))
            return self._json(200 if body.get("ok") else 404, body)
        if p == "/api/run":
            rid = qs.get("id", [""])[0]
            run = self.mgr.get(rid)
            if not run:
                return self._json(404, {"error": "任务不存在"})
            snap = run.snapshot()
            try:
                fu = self.mgr.followup_texts(rid)
            except Exception:                            # noqa: BLE001
                fu = []
            if fu:
                snap["followups"] = fu
            # 追问过的会话：把此前的轮次一起交回（旧归档没有 turns/ 就照旧单轮）
            try:
                turns = self.mgr.turn_archives(rid, snap)
            except Exception:                            # noqa: BLE001
                turns = []
            if turns:
                snap["turns"] = turns
            return self._json(200, snap)
        if p == "/api/stream":
            run = self.mgr.get(qs.get("id", [""])[0])
            if not run:
                return self._json(404, {"error": "任务不存在"})
            return self._stream(run)
        # ── W1 只读接口（全 GET、纯读；异常回真实错误文案 + hint，不把失败渲染成空成功）──
        if p == "/api/capabilities":
            try:
                return self._json(200, self.mgr.capabilities())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "能力清单现算失败 —— 检查 rongke/ 包是否完好"})
        if p == "/api/gates":
            try:
                return self._json(200, self.mgr.gates())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "门禁真相读取失败 —— 检查 rongke/gate.py 是否完好"})
        if p == "/api/deliverables":
            try:
                return self._json(200, self.mgr.deliverables())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "交付物总览现算失败 —— 检查 runs/ 目录是否可读"})
        if p == "/api/figures":
            try:
                return self._json(200, self.mgr.figures())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "图表资产现算失败 —— 检查 runs/ 目录与 rongke/figure.py"})
        if p == "/api/analysis":
            # 数据分析成图的**能力视图**（只读、快）。真跑走 POST。
            try:
                return self._json(200, self.mgr.analysis_view())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "数据分析成图的能力探测失败 —— "
                                                "检查 rongke/analysis.py 是否完好"})
        if p == "/api/analysis/fig":
            # 把成图的 PNG 发回浏览器。
            # ★ 为什么不复用 /api/artifact：那条路由要 `id`（任务工作区名），
            #   而 `_analysis/` 不属于任何一次任务 —— 硬套就得编一个假 id。
            # ★ 为什么不走通用静态文件服务：`runs/` 下有 session.json、
            #   secrets 之类不该公开的东西。这里**只认 `_analysis/` 下的图**，
            #   后缀白名单也只放图片扩展名。
            qs = urllib.parse.parse_qs(u.query)
            rel = (qs.get("f", [""])[0] or "").replace("\\", "/")
            base = (ws_dir() / "_analysis").resolve()
            tgt = (base / rel).resolve()
            try:
                tgt.relative_to(base)
            except ValueError:
                return self._json(400, {"error": "只能取 _analysis/ 下的图"})
            if tgt.suffix.lower() not in (".png", ".svg", ".tiff", ".pdf"):
                return self._json(400, {"error": f"不发的类型：{tgt.suffix}"})
            if not tgt.exists():
                return self._json(404, {"ok": False, "error": "这张图还没画出来"})
            ctype = {".png": "image/png", ".svg": "image/svg+xml",
                     ".tiff": "image/tiff", ".pdf": "application/pdf"}[
                         tgt.suffix.lower()]
            data = tgt.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # 每次成图都会重写这一组文件 ⇒ **不能**长缓存，否则换表画完
            # 浏览器还拿旧图（前端已经加了 ?t= 时间戳作为第二道保险）。
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/api/billing":
            # 本地账单（只读）。和 /api/setup 一样**不塞进 /api/status**：
            # status 是每次轮询都在打的轻量心跳，而这里要读台账文件。
            #
            # ★ 这个端点只读不写：扣费发生在任务跑完之后（loop._charge_billing），
            #   界面不该有"扣一笔"的入口 —— 那会变成一个人能自己给自己改账。
            #   充值同理：本地占位，只能从 CLI 走，而且会明说"没真收款"。
            try:
                # ★ 用 `mgr.billing`，**不要** `Billing()` 新建一个：
                #   新实例的令牌表是空的，于是"登录成功"之后这里仍然
                #   认不出你 —— 表现是登录后立刻又被要求登录。
                b = getattr(self.mgr, "billing", None)
                if b is None:
                    return self._json(500, {
                        "error": "账单子系统没起来",
                        "detail": getattr(self.mgr, "billing_error", ""),
                    })
                return self._json(200, b.report())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "账单读不出来 —— 先确认 "
                                                "rongke/billing.py 完好，"
                                                "台账在 <数据目录>/billing/ 下"})

        if p == "/api/magpie":
            # 只读：这个网关在不在听、上面有哪些模型。
            # ★ GET 而不是 POST：设置页一打开就要问（自动发现），
            #   而它**不改任何东西** —— 写成 POST 会让"打开设置页"
            #   看起来像一次写操作。切换模型仍然走 POST /api/settings。
            # ★ 必须放在 _route_get 里：第一版误加进了 _route_post，
            #   前端 GET 过去就是 404 —— 卡片显示"检测失败：not found"。
            try:
                ep = (urllib.parse.parse_qs(u.query).get("host") or [""])[0]
                return self._json(200, self.mgr.magpie_probe(ep))
            except Exception as e:                            # noqa: BLE001
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "先确认 rongke/magpie.py 完好"})
        if p == "/api/setup":
            # 首次使用自检（只读）。**不是 /api/status 的一部分**：
            # status 每次轮询都在打，而这里要起子进程探库（几秒），
            # 混进去会让整个控制台的轮询跟着变慢。
            try:
                return self._json(200, self.mgr.setup_view())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "首次使用自检跑不起来 —— "
                                                "先确认 rongke/setupdoctor.py 完好"})
        if p == "/api/settings":
            # 只读。★ 这个响应里**没有密钥值**，只有一个 `key_present` 布尔 ——
            # 把密钥端给浏览器，等于把用户的额度挂在一个 URL 上。
            try:
                return self._json(200, self.mgr.settings_view())
            except Exception as e:
                return self._json(500, {"error": f"{type(e).__name__}: {e}",
                                        "hint": "配置读不出来 —— "
                                                "先检查 config.json 是不是合法 JSON"})
        return self._json(404, {"error": "not found"})

    # ── 产出物取回 ──────────────────────────────────────────
    #: `/api/orig/*` 的 `?file=` 入口能读哪些目录。
    #: 空的 list 表示"整台盘"——那是修之前的实际行为，**不是**要保留的选项。
    #: 这里只放三类：任务工作区、素材区、文献库根。都不是的话就如实拒。
    def _orig_roots(self) -> list[Path]:
        """`?file=` 允许落在这几个根之下的**具体**目录（不存在或解析不动的跳过）。"""
        out: list[Path] = []
        for p in (
            ws_dir(),                       # runs/<id>/ —— 任务工作区
            data_root() / "materials",      # 素材区（用户丢进来的原件）
        ):
            try:
                if p.is_dir():
                    out.append(p.resolve())
            except Exception:
                continue
        try:
            # 文献库根：工具自己报的绝对路径才是唯一可信那份（同 _orig_target 的告诫）
            d = self.mgr.kb_query("kb_list", limit=1)
            root = str((d or {}).get("library") or "") if isinstance(d, dict) else ""
            if root:
                rp = Path(root)
                if rp.is_dir():
                    out.append(rp.resolve())
        except Exception:
            pass
        return out

    def _orig_file_allowed(self, f: Path) -> str:
        """判 `?file=` 给的这份文件能不能读。返回空串 = 放行，否则是**给人看的**原因。

        两道判据，缺一不可：

        ① **类型白名单** —— 这个接口是"给我原页/原件"用的，天然只该服务 PDF 与
           能转 PDF 的文档。不设它，`win.ini` 这种"存在、也能读"的文件照样被认下来
           （实测 `/api/orig/open` 对 `C:\\Windows\\win.ini` 回 `ok=True`）。
        ② **根围栏** —— `resolve()` 之后必须真的落在某个允许的根之下。
           必须先 resolve 再比：不 resolve 挡不住 `..\\..\\` 这种写法；
           只比字符串前缀又会被 `/allowed/root_evil` 骗过（前缀撞车），
           所以用 `relative_to()` 而不是 `startswith()`。
        """
        from . import orig as _orig

        ext = f.suffix.lower()
        if ext not in _orig._DIRECT_PDF and ext not in _orig._VIA_PDF:
            return ("这个接口只开 PDF 与能转 PDF 的文档"
                    f"（{', '.join(sorted(_orig._DIRECT_PDF | _orig._VIA_PDF))}），"
                    f"收到的是 {ext or '无扩展名'}")
        try:
            real = f.resolve()
        except Exception as e:  # noqa: BLE001
            return f"路径解析失败：{type(e).__name__}"
        for root in self._orig_roots():
            try:
                real.relative_to(root)
                return ""
            except ValueError:
                continue
        return "这个文件不在本任务的工作区、素材区或文献库之内"

    def _orig_target(self, qs: dict, probe: bool = False) -> dict:
        """把 `?kb=<id>` / `?file=<绝对路径>` 解析成一份**可渲染的真文件**。

        两条入口都要守同一条围栏：**只能渲盘上真实存在、且类型允许的文件**。
        kb 那条尤其要小心——`kb_list` 里的 `local_path` 是**入库时记下的原路径**，
        它可能指向一个后来被移走的文件，所以这里必须 `exists()` 复核，
        不能因为"库里写着有"就当真。
        """
        from . import orig as _orig

        kb_id = (qs.get("kb", [""])[0] or "").strip()
        lib = (qs.get("library", [""])[0] or "").strip()
        raw = (qs.get("file", [""])[0] or "").strip()

        title = ""
        if kb_id:
            # 原件路径有两个来源：条目自己记的 `local_path`（手工上传/从别的
            # 目录入库的 PDF 就在原处），或 litkb 约定的 `<库根>/pdfs/<id>.pdf`。
            # 只看约定会让本地 PDF 全部"找不到原页"（2026-10-29 实测）。
            # ⚠️ 库根**不能**问 `kb_root("")`：它对空库名故意回空串（见那里的说明），
            #    空串拼出来是相对路径，必然找不到。真正的库根在 `kb_list` 回包的
            #    `library` 字段里 —— 那是工具自己报的绝对路径，也是唯一可信的那份。
            # 但仍不凭记录就当真：下面 `is_file()` 复核，缺了就如实说"盘上没有"。
            d = self.mgr.kb_query("kb_list", library=lib, limit=500)
            items = (d.get("items") or []) if isinstance(d, dict) else []
            hit = next((x for x in items if str(x.get("id")) == kb_id), None)
            title = (hit or {}).get("title") or kb_id
            root = str((d or {}).get("library") or "") or self.mgr.kb_root(lib)
            f = None
            for key in ("local_path", "path", "pdf_path"):
                v = (hit or {}).get(key)
                if not v:
                    continue
                try:
                    p = Path(str(v))
                    if p.is_file():
                        f = p
                        break
                except OSError:
                    pass
            if f is None and root:
                f = Path(root) / "pdfs" / (kb_id + ".pdf")
            if f is None or not f.is_file():
                if hit is not None and not hit.get("fulltext"):
                    # 只有元数据、没有 PDF —— 如实说，别糊一句"打不开"
                    return {"ok": False, "error": "这篇只有元数据，没有 PDF 原文",
                            "no_pdf": True, "title": title}
                return {"ok": False, "error": "找不到这篇的 PDF 文件", "missing": True,
                        "title": title}
        elif raw:
            f = Path(raw)
            if not f.is_absolute():
                return {"ok": False, "error": "file 必须是绝对路径"}
            if not f.exists():
                return {"ok": False, "error": "文件不存在"}
            # ★ 围栏（2026-10-12 体检补）：`?file=` 这条入口原先只判"绝对路径 + 存在"，
            #   于是 `/api/orig/raw?file=C:\Users\<你>\.rongke\secrets.json` 能把
            #   **荣珂自己的密钥**原样读出去（byte-exact 实证）。同文件的
            #   `/api/artifact` 一直是严格围栏的（"能被列出来的才能被取回"），
            #   这条却裸奔 —— 两条取文件的通路必须守同一条纪律。
            #   判据两道：① 类型白名单（只管渲染得了的那几种）
            #             ② 根围栏（只在本任务工作区 / 素材区 / 文献库根之内）
            why = self._orig_file_allowed(f)
            if why:
                return {"ok": False, "error": why}
            title = qs.get("title", [""])[0] or f.name
        else:
            return {"ok": False, "error": "要 ?kb=<文献id> 或 ?file=<绝对路径>"}

        ok, why = _orig.available()
        if not ok:
            return {"ok": False, "error": "渲染器不可用：" + why}
        try:
            dpi = int((qs.get("dpi", [str(_orig.DEFAULT_DPI)])[0]) or _orig.DEFAULT_DPI)
        except Exception:
            dpi = _orig.DEFAULT_DPI
        dpi = max(60, min(dpi, 300))

        out = {"ok": True, "title": title, "source": str(f), "dpi": dpi,
               "renderable": f.suffix.lower() in (_orig._DIRECT_PDF | _orig._VIA_PDF),
               "via_pdf": f.suffix.lower() in _orig._VIA_PDF}
        if probe:
            # 只探**缓存**，不在这里渲 —— `render()` 缓存未命中时会真渲
            # （几十页论文十几秒），一个自称 probe 的请求不该占住工作线程。
            # 要真渲走 `/api/orig/render`。实测教训见 `orig.peek()` 的说明。
            cached = _orig.peek(f, dpi=dpi)
            out["rendered"] = bool(cached.get("ok"))
            out["cached"] = bool(cached.get("cached"))
            if cached.get("ok"):
                out["pageCount"] = cached.get("pageCount")
                out["seconds"] = cached.get("seconds")
            elif cached.get("miss"):
                # 没缓存过 = 还没渲过。这是**正常状态**，不是错误，
                # 前端据此显示「开始渲染」而不是「打不开」。
                out["not_rendered_yet"] = True
            else:
                out["why_not"] = cached.get("error")
        return out

    def _orig_render(self, qs: dict) -> dict:
        """真渲染（可能十几秒）。只在用户按下「渲染原页」时走这里。"""
        from . import orig as _orig
        t = self._orig_target(qs)
        if not t.get("ok"):
            return t
        f = Path(t["source"])
        d = _orig.render(f, dpi=t["dpi"], force=(qs.get("force", ["0"])[0] not in ("0", "", "false")))
        if not d.get("ok"):
            return d
        # 只回**页文件名清单**，不回绝对路径：前端拿 `/api/orig/page` 取图，
        # 盘上路径不该出现在界面能读到的地方。
        return {"ok": True, "title": t["title"], "pageCount": d["pageCount"],
                "pages": [p["n"] for p in d["pages"]],
                "cached": d.get("cached"), "seconds": d.get("seconds"),
                "via_pdf": t["via_pdf"], "dpi": t["dpi"]}

    def _orig_page(self, qs: dict) -> None:
        """把某一页的 PNG 发回去。"""
        from . import orig as _orig
        t = self._orig_target(qs)
        if not t.get("ok"):
            return self._json(404, t)
        try:
            n = int(qs.get("n", ["1"])[0] or 1)
        except Exception:
            n = 1
        p = _orig.page_path(Path(t["source"]), n, dpi=t["dpi"])
        if p is None or not p.exists():
            return self._json(404, {"ok": False, "error": "这一页还没有渲染"})
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        # 同一页的图不会变（变了说明源文件变了，而缓存键是 sha256）⇒ 可以长缓存
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _orig_raw(self, qs: dict) -> None:
        """把原件本身发回去（「原件」按钮：浏览器自带的 PDF 阅读器/下载）。

        为什么要有这条路：我们渲染的图是**服务端的一次解释**；
        用户想自己放大看细节、或者想存下来，得拿到原件。
        """
        t = self._orig_target(qs)
        if not t.get("ok"):
            return self._json(404, t)
        self._file_response(Path(t["source"]), download=None, no_store=True)

    def _file_response(self, f: Path, download: bool, no_store: bool = False) -> None:
        """流式发送文件：不把几十 MB 的原稿或 PDF 整份复制进内存。

        原稿接口原来 inline 但无 Content-Disposition；保留它的下载语义。
        """
        size = f.stat().st_size
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        if download is not None:
            disposition = "attachment" if download else "inline"
            self.send_header("Content-Disposition", disposition + "; filename*=UTF-8''"
                             + urllib.parse.quote(f.name))
        # 在发响应头之前打开：文件竞态消失/权限失败仍可回 JSON 500。
        with f.open("rb") as src:
            self.end_headers()
            while chunk := src.read(1024 * 1024):
                self.wfile.write(chunk)

    def _artifact(self, qs: dict) -> None:
        """把工作区里的一个产出物发回去（预览或下载）。

        围栏只有一句：**能被列出来的，才能被取回**（`Manager.artifact_path`）。
        这不是安全上的保守，而是同一条纪律：列表与取回共用一份定义，
        才不会出现"列表里没有它、却能下载它"这种两副面孔。
        """
        rid = qs.get("id", [""])[0]
        rel = qs.get("path", [""])[0]
        f = self.mgr.artifact_path(rid, rel)
        if f is None:
            return self._json(404, {"error": "产出物不存在（或不在该任务的工作区内）"})
        try:
            dl = qs.get("dl", ["0"])[0] not in ("0", "", "false")
            self._file_response(f, download=dl, no_store=True)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except OSError as e:
            # 已发头不能改状态码，必须断开，避免被当成一份完整文件。
            if self.wfile.closed or self.close_connection:
                return
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})

    # ── SSE ─────────────────────────────────────────────────
    def _stream(self, run: Run) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        idx = 0
        try:
            while True:
                with run.cond:
                    if idx >= len(run.events) and not run.done:
                        run.cond.wait(timeout=20)
                    batch = run.events[idx:]
                    idx = len(run.events)
                    done = run.done

                if batch:
                    for ev in batch:
                        self.wfile.write(
                            f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                elif done:
                    tail = {"n": idx, "kind": "_end", "t": time.strftime("%H:%M:%S"),
                            "data": {"result": run.result, "error": run.error}}
                    self.wfile.write(
                        f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    # ── POST ────────────────────────────────────────────────
    def do_POST(self):
        """POST 的兜底外壳（与 `do_GET` 同一理由，见那里的说明）。

        ★ 写端点还有个额外的坑：异常发生在**读过 body 之后**的话，这条
          keep-alive 连接上剩下的字节已经没有归属了。所以这里宁可
          `Connection: close`，也不要把一条状态不明的连接留给下一个请求
          —— 那会变成本仓吃过一次的串味事故（`WinError 10053`）。
        """
        try:
            return self._route_post()
        except RequestError as e:
            self.close_connection = e.status == 413
            return self._json(e.status, {"error": e.detail})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as e:                             # noqa: BLE001
            self._internal_error("POST", e)

    def _route_post(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/login":
            # ★ 登录必须在门禁**之前** —— 它是唯一一个"没登录也能调"的写端点。
            #   这是逻辑上必须的：不先允许它，就永远拿不到令牌。
            #
            # ★ 这个分支里**每个提前 return 之前都要把 body 读完**。
            #   本类是 keep-alive 的：没读走的 body 会被当成下一个请求的
            #   开头，同一连接上的后续请求就串味了，客户端只看到
            #   `WinError 10053`（连接被中止），完全看不出是这个原因。
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json(400, {"error": "Content-Length 必须是整数"})

            def _drain():
                """把没用的 body 读走丢掉的**唯一**出口。

                单独抽出来是为了让"每个提前 return 都调过它"这件事显而易见
                —— 散着写五份，漏一份就又回到那个 10053。
                """
                if 0 < n <= 65536:
                    try:
                        self.rfile.read(n)
                    except OSError:
                        pass

            b = getattr(self.mgr, "billing", None)
            if b is None:
                _drain()
                return self._json(500, {
                    "error": "账单子系统没起来，无法登录",
                    "detail": getattr(self.mgr, "billing_error", "")})
            if n <= 0 or n > 8192:
                _drain()
                return self._json(400, {"error": "没有收到内容"})
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:                             # noqa: BLE001
                _drain()
                return self._json(400, {"error": "请求体不是合法 JSON"})
            if not isinstance(payload, dict):
                return self._json(400, {"error": "请求体必须是对象"})
            pw = payload.get("password")
            if not isinstance(pw, str):
                # 明确区分"没给"与"给了空串"：空串是**合法的密码值**
                # （虽然不该有人这么设），省略字段才叫"没给"。
                return self._json(400, {"error": "password 必须是字符串"})
            try:
                ok = b.verify_password(pw)
            except Exception as e:                        # noqa: BLE001
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            if not ok:
                # 不透露"密码错了几次""还差什么" —— 那些只会帮猜的人
                return self._json(401, {"error": "密码不对", "ok": False})
            tok = b.issue_token()
            return self._json(200, {"ok": True, "token": tok,
                                    "note": "令牌只存在这次控制台进程的内存里，"
                                            "进程一停就要重新登录。"})
        if u.path == "/api/logout":
            # ★ 必须把这个请求的 body **读完再返回**，哪怕它没用。
            #   `BaseHTTPRequestHandler` 是 keep-alive 的：没读走的 body
            #   会被当成下一个请求的开头，于是同一连接上的后续请求串味，
            #   客户端看到的是 `WinError 10053`（连接被中止）而不是任何
            #   可读的错误。实测就是这条把 login 套件打崩了 —— 而单跑
            #   往往侥幸通过，因为那时每条请求都新开连接。
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json(400, {"error": "Content-Length 必须是整数"})
            if 0 < n <= 65536:
                try:
                    self.rfile.read(n)
                except OSError:
                    pass
            b = getattr(self.mgr, "billing", None)
            if b is not None:
                try:
                    b.drop_token(self._token())
                except Exception:                         # noqa: BLE001
                    pass
            return self._json(200, {"ok": True})
        if u.path == "/api/license":
            # 激活 / 注销。**这个端点在门禁之内**（不像 /api/login）：
            # 改授权状态是花钱才该换来的一件事，不该任何路过的人都能做。
            # 读请求体、以及每个提前 return 都要 drain（keep-alive 纪律）。
            n2 = int(self.headers.get("Content-Length") or 0)

            def _d2():
                if 0 < n2 <= 65536:
                    try:
                        self.rfile.read(n2)
                    except OSError:
                        pass

            # ★ 这句必须真的在这儿：注释写"在门禁之内"而代码在 return 之后才
            #   调 _require_login()，等于任何路过的人都能注销授权。
            #   先 drain 再把 401 发出去，否则同一条 keep-alive 连接会串味。
            if not self._require_login():
                _d2()
                return

            lic = getattr(self.mgr, "license", None)
            if lic is None:
                _d2()
                return self._json(500, {
                    "error": "授权子系统没起来",
                    "detail": getattr(self.mgr, "license_error", "")})
            if n2 <= 0 or n2 > 65536:
                _d2()
                return self._json(400, {"error": "没有收到内容"})
            try:
                body2 = json.loads(self.rfile.read(n2).decode("utf-8"))
            except Exception:                             # noqa: BLE001
                _d2()
                return self._json(400, {"error": "请求体不是合法 JSON"})
            if not isinstance(body2, dict):
                return self._json(400, {"error": "请求体必须是对象"})
            action = str(body2.get("action") or "activate")
            if action == "deactivate":
                try:
                    lic.clear_code()
                except Exception as e:                    # noqa: BLE001
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
                return self._json(200, {"ok": True,
                                        "license": self.mgr.license_state()})
            code = body2.get("code")
            if not isinstance(code, str) or not code.strip():
                return self._json(400, {"error": "请把激活码填进来"})
            try:
                lic.save_code(code)
            except ValueError as e:
                # 验签不过 ⇒ 400 而不是 500：这是用户输入的问题，不是服务器坏了。
                return self._json(400, {"error": str(e), "ok": False})
            except Exception as e:                        # noqa: BLE001
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(200, {"ok": True,
                                    "license": self.mgr.license_state()})
        # 其余写端点一律过门禁
        if u.path.startswith("/api/") and not self._require_login():
            return
        if u.path == "/api/upload":
            # 用**裸字节体**而不是 multipart：`http.server` 里手写 multipart 解析
            # 是一堆容易出错的边界处理，而我们要的只是"一个文件一条请求"。
            # 文件名走 query（`?name=`），内容走 body —— 前端一行 `fetch` 就能发。
            q = urllib.parse.parse_qs(u.query)
            fname = (q.get("name") or [""])[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json(400, {"error": "Content-Length 必须是整数"})
            if n <= 0:
                return self._json(400, {"error": "没有收到内容"})
            if n > UPLOAD_MAX:
                # 413 而不是 400：这是"太大"，不是"格式不对"。
                # **先看长度再读**：超限还照读进内存，等于用内存换一条错误提示。
                return self._json(413, {
                    "error": f"文件超过 {UPLOAD_MAX // 1024 // 1024}MB 上限"})
            why = self.mgr.busy()
            if why:
                return self._json(409, {"error": why})
            data = self.rfile.read(n)
            try:
                item = self.mgr.add_material(fname, data)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:                      # noqa: BLE001
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(200, {"ok": True, "item": item,
                                    "materials": self.mgr.materials})

        if u.path == "/api/kb/upload":
            # 与 `/api/upload` 同一套形状（裸字节体 + `?name=`），但去向不同：
            # 这里**直接入库**，不经过材料区 —— 材料区会被下一次任务改名搬走，
            # 而入库的 PDF 是库的长期资产。两件事共用一个端点才是真的危险。
            q = urllib.parse.parse_qs(u.query)
            fname = (q.get("name") or [""])[0]
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json(400, {"error": "Content-Length 必须是整数"})
            if n <= 0:
                return self._json(400, {"ok": False, "error": "没有收到内容"})
            if n > KB_UPLOAD_MAX:
                return self._json(413, {
                    "ok": False,
                    "error": f"文件超过 {KB_UPLOAD_MAX // 1024 // 1024}MB 上限"})
            why = self.mgr.busy()
            if why:
                # 把体读完再回 409：keep-alive 下漏读的 body 会串到下一条请求。
                self.rfile.read(n)
                return self._json(409, {"ok": False, "error": why})
            data = self.rfile.read(n)
            try:
                res = self.mgr.kb_upload(
                    fname, data,
                    library=(q.get("library", [""])[0] or ""),
                    # `?no_enrich=1` ⇒ 跳过联网补全。默认照旧（补全），
                    # 只有显式要来才跳过 —— 默认值变了会静默改变所有既有调用方的行为。
                    no_enrich=(q.get("no_enrich", ["0"])[0] or "0") not in ("0", "", "false"))
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:                      # noqa: BLE001
                return self._json(500, {"ok": False,
                                        "error": f"{type(e).__name__}: {e}"})
            return self._json(200, res)

        if u.path in ("/api/kb/find", "/api/kb/pick"):
            # W23-B「让 AI 找文献」：两步分开，**中间那一步是人勾选**。
            # find = 只检索列表（一个都不入）；pick = 只入勾中的（逐篇转 kb_add）。
            # 合并成一步就是"自动全入"，那是用户明令不许的。
            body = read_json_object(self)
            lib = str(body.get("library") or "").strip()
            if u.path == "/api/kb/pick":
                why = self.mgr.busy()
                if why:
                    return self._json(409, {"ok": False, "error": why})
            try:
                if u.path == "/api/kb/find":
                    res = self.mgr.lit_find(str(body.get("query") or ""),
                                            library=lib,
                                            limit=body.get("limit") or 10,
                                            sources=str(body.get("sources") or ""))
                else:
                    res = self.mgr.lit_pick(body.get("dois") or [], library=lib)
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:                  # noqa: BLE001
                return self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
            return self._json(200, res)

        if u.path == "/api/material_drop":
            name = str(read_json_object(self).get("name") or "")
            why = self.mgr.busy()
            if why:
                return self._json(409, {"error": why})
            res = self.mgr.drop_material(name)
            if not res.get("ok"):
                return self._json(404, {"error": res.get("error") or "材料里没有这个文件"})
            # `note` / `kept_at` 是**给用户看的那一行**：原件挪去哪了、或者没挪动。
            # 不返回它们，前端就只剩"成功"二字可显示 —— 而成功里可能藏着"文件还在"。
            return self._json(200, {
                "ok": True,
                "materials": self.mgr.materials,
                "moved": bool(res.get("moved")),
                "kept_at": res.get("kept_at", ""),
                "note": res.get("note", ""),
            })

        if u.path == "/api/settings":
            # ★ 全站**唯一**会改配置文件的写接口。
            # 409 的判据与上传/材料区同源：`Manager.busy()`。
            why = self.mgr.busy()
            if why:
                # 判据与上传/材料区**同源**（同一个 busy()），但文案不复用它那句话：
                # busy() 说的是"别动材料"，这一路挡的是"别改配置"，
                # 把那句原样塞进括号里，人看完会以为不能改的是文件而不是模型。
                return self._json(409, {
                    "error": "任务在跑，改配置要等它结束 —— 模型是在 agent 构造时冻住的，"
                             "跑到一半换它没有意义。等右上角的任务结束再保存。"})
            body = read_json_object(self)
            res, err = self.mgr.settings_save(body)
            if res is None:
                # 400 而不是 500：这是"你给的字段我不收"，不是"服务端坏了"。
                # 文案原样端给界面显示，不许在前端改写成一句好听的话。
                return self._json(400, {"error": err})
            return self._json(200, res)

        if u.path == "/api/secret":
            # W19-A：存接口密钥到 ~/.rongke/secrets.json 并当场验证。
            # 与 /api/settings 同一道 409 闸：写密钥要重建 agent，
            # 而 agent 是构造时冻住的，跑到一半换它没有意义。
            why = self.mgr.busy()
            if why:
                return self._json(409, {
                    "error": "任务在跑，改密钥要等它结束 —— 换密钥会重建 agent，"
                             "跑到一半换它没有意义。等右上角的任务结束再保存。"})
            body = read_json_object(self)
            res, err = self.mgr.secret_save(body)
            if res is None:
                return self._json(400, {"error": err})
            return self._json(200, res)

        if u.path == "/api/analysis":
            # 真跑一次成图。它会起子进程画图 + 跑六道图面门（几秒到几十秒），
            # 所以：① 走 busy() 闸门 —— 和上传/材料区/设置页同一条判据；
            #       ② 不塞进任何轮询。
            why = self.mgr.busy()
            if why:
                return self._json(409, {
                    "error": "有任务在跑，成图要等它结束 —— "
                             "它会占满一个解释器画图，同时跑会和任务抢资源。"})
            body = read_json_object(self)
            res, err = self.mgr.analysis_run(body)
            if res is None:
                return self._json(400, {"error": err})
            return self._json(200, res)

        if u.path == "/api/setup":
            # 一键修。409 的判据与上传/材料区/设置页**同源**（`Manager.busy()`）：
            # 它写完要重建 agent，跑到一半重建等于在 agent 正读配置时换配置。
            why = self.mgr.busy()
            if why:
                return self._json(409, {
                    "error": "任务在跑，改配置要等它结束 —— 修完会重建 agent，"
                             "跑到一半重建没有意义。等右上角的任务结束再点。"})
            body = read_json_object(self)
            res, err = self.mgr.setup_fix(body)
            if res is None:
                # 400 而不是 500：这是"你给的项我不收"，不是"服务端坏了"。
                return self._json(400, {"error": err})
            return self._json(200, res)

        if u.path == "/api/boot":          # 内核没起来时重试
            self.mgr.boot()
            return self._json(200, self.mgr.status())
        if u.path == "/api/cancel":
            try:
                rid = read_json_object(self).get("id", "")
            except RequestError:
                # 历史兼容：取消接口把不可读/空 id 当成无目标，而不是协议失败。
                rid = ""
            run = self.mgr.get(rid)
            if not run:
                return self._json(404, {"error": "任务不存在"})
            run.cancel.set()
            # 取消必须能**立刻穿透**一个挂起的"问人"：内核那时阻塞在等待里，
            # 不检查 cancel。若不一并唤醒，点了停止还要等满 300s 超时 ——
            # 用户会认定按钮坏了（而这恰恰是评审说的"失败体验是硬伤"）。
            # respond() 在没有待答问题时会自己返回 False，所以这行对普通取消无害。
            run.respond("stop", "用户取消了任务")
            run.push("cancel_requested", {})
            return self._json(200, {"ok": True})

        if u.path == "/api/intervene":
            body = read_json_object(self)
            run = self.mgr.get(str(body.get("id") or ""))
            if not run:
                return self._json(404, {"error": "任务不存在"})
            action = str(body.get("action") or "continue").strip()
            if action not in ("continue", "steer", "extend", "stop"):
                return self._json(400, {"error": f"未知的 action：{action}"})
            text = str(body.get("text") or "")
            steps = body.get("steps")
            try:
                steps = int(steps) if steps not in (None, "") else None
            except Exception:
                steps = None
            if not run.respond(action, text, steps):
                # 如实说"没人在等答案"，而不是回一个 ok 让人以为采纳了。
                return self._json(409, {
                    "error": "当前没有待你回答的问题（可能已超时或已回答过）"})
            run.push("intervene_answered", {
                "action": action, "chars": len(text.strip()), "steps": steps})
            return self._json(200, {"ok": True, "action": action})

        if u.path == "/api/resume":
            try:
                rid = str(read_json_object(self).get("id") or "")
            except RequestError:
                # 历史兼容：续跑把不可读/空 id 交给 busy() 回「此刻不成」。
                rid = ""
            run, err = self.mgr.resume(rid)
            if run is None:
                # 授权不足是 403，与"忙"的 409 分开（与 /api/run 同一口径）。
                if _license_denial(err):
                    return self._json(403, {"error": err, "license": True,
                                            "license_state": self.mgr.license_state()})
                # 409 而不是 500：这是"此刻不成"，不是"坏了"。原因原样端给用户看。
                return self._json(409, {"error": err})
            return self._json(200, {"id": run.id, "goal": run.goal})

        if u.path == "/api/follow":
            # 会话内追问：跟 `/api/resume` 同一套形状（JSON 体 + 409 挡此刻不成）。
            body = read_json_object(self)
            run, err = self.mgr.follow_up(str(body.get("id") or ""),
                                          str(body.get("text") or ""),
                                          mentor=kb_only_from(body),
                                          mode=mode_from(body),
                                          preset=str(body.get("preset") or "").strip())
            if run is None:
                # 追问同样是"发起新任务"：过期时回 403 + license 标记，
                # 前端据此弹激活层，而不是显示一条跑失败的历史。
                if _license_denial(err):
                    return self._json(403, {"error": err, "license": True,
                                            "license_state": self.mgr.license_state()})
                return self._json(409, {"error": err})
            return self._json(200, {"id": run.id, "goal": run.goal})

        if u.path == "/api/figure/tavotto":
            body = read_json_object(self)
            try:
                if str(body.get("action") or "handoff") == "reaudit":
                    result = self.mgr.tavotto_reaudit(str(body.get("rid") or ""), str(body.get("path") or ""))
                else:
                    result = self.mgr.tavotto_handoff(str(body.get("rid") or ""), str(body.get("path") or ""))
                return self._json(int(result.pop("status", 200)), result)
            except (ValueError, RuntimeError, OSError) as exc:
                return self._json(400, {"ok": False, "error": str(exc)})
        if u.path == "/api/topic":
            # 课题台账的**最小接线**（W22-B §3.5）：界面上看得见当前课题、能立/切。
            # ★ 这里**不另写一套台账读写**：动作全部转给内核那两个常驻工具
            #   （`topic_status` / `topic_use`），服务端只做传输与如实报错。
            body = read_json_object(self)
            slug = str(body.get("slug") or "").strip()
            # 兼容旧客户端：只带 slug 的 POST 表示切换；显式 status 仍为只读。
            action = str(body.get("action") or ("use" if slug else "status")).strip()
            if action == "use":
                if not slug:
                    return self._json(400, {"error": "课题名是空的 —— 先起个名字再立课题"})
                if len(str(body.get("title") or "")) > 160 or len(str(body.get("question") or "")) > 1200:
                    return self._json(400, {"error": "课题名称最多 160 字，研究问题最多 1200 字"})
                why = self.mgr.busy()
                if why:
                    return self._json(409, {"error": "任务正在运行，结束后再切换课题。"})
                return self._json(200, self.mgr.topic_tool(
                    "topic_use", slug=slug,
                    title=str(body.get("title") or "").strip(),
                    question=str(body.get("question") or "").strip()))
            if action in ("update", "link", "unlink"):
                why = self.mgr.busy()
                if why:
                    return self._json(409, {"error": "任务正在运行，结束后再修改课题。"})
                from .. import topic as topic_store
                if not topic_store.valid_slug(slug):
                    return self._json(400, {"ok": False, "error": "非法的课题名"})
                try:
                    if action == "update":
                        out = topic_store.update_topic(slug, str(body.get("title") or ""),
                            str(body.get("question") or ""),
                            stage=body.get("stage") if "stage" in body else None,
                            next_step=str(body.get("next_step") or "") if "next_step" in body else None)
                    else:
                        did = str(body.get("doc_id") or "").strip()
                        if action == "link":
                            docs = self._library_by_ids([did])
                            if docs is None:
                                return self._json(502, {"ok": False, "error": "共享文献库读取失败，未写入关联"})
                            if did not in docs:
                                return self._json(404, {"ok": False, "error": "共享库中没有这篇文献，未写入关联"})
                        out = topic_store.set_library(slug, did, action == "link")
                    return self._json(200, {"ok": True, "topic": out} if action == "update" else out)
                except KeyError as e:
                    return self._json(404, {"ok": False, "error": str(e.args[0])})
                except ValueError as e:
                    return self._json(400, {"ok": False, "error": str(e)})
            if action in ("doc", "docs"):
                # ★ W36：让**对话**能改课题内容（用户原话「我直接跟他说修改课题内容」）。
                #   这里**不另写一套台账**：读写全部转给 `topic.write_doc/read_doc`，
                #   与内核的 `topic_write` 工具共用同一条通路、同一份校验。
                #   为什么要挡 busy()：跑着的时候内核可能正在写同一份文件，
                #   两边同时改就是"两份真相抢一个文件"。
                from .. import topic as topic_store
                if not topic_store.valid_slug(slug):
                    return self._json(400, {"ok": False, "error": "非法的课题名"})
                meta = topic_store.read_topic(slug)
                if meta is None:
                    return self._json(404, {"ok": False, "error": f"课题不存在：{slug}"})
                if action == "docs":
                    # 列三份文档各自在不在、多少字 —— 让对话先说"有哪些"，再决定改哪份。
                    rows = []
                    for key in topic_store.DOC_FILE:
                        try:
                            head, n = topic_store._doc_head(slug, key)
                            rows.append({"doc": key, "exists": bool(head) or n > 0,
                                         "chars": n})
                        except Exception:
                            rows.append({"doc": key, "exists": False, "chars": 0})
                    return self._json(200, {"ok": True, "slug": slug, "docs": rows})
                why = self.mgr.busy()
                if why:
                    return self._json(409, {"ok": False, "error": "任务正在运行，结束后再改课题内容。"})
                dname = str(body.get("doc") or "plan")
                if "text" not in body:
                    # 只读：回正文（带窗口上限，与内核读法同一口径）
                    r = topic_store.read_doc(slug, dname,
                                             offset=int(body.get("offset") or 0),
                                             limit=int(body.get("limit") or topic_store.DOC_WINDOW))
                    return self._json(200, {"ok": True, "slug": slug, "doc": dname,
                                            "text": r.get("text") or "",
                                            "chars": r.get("chars") or 0})
                mode = str(body.get("mode") or "append")
                if mode not in ("append", "overwrite"):
                    return self._json(400, {"ok": False, "error": "mode 只能是 append 或 overwrite"})
                try:
                    r = topic_store.write_doc(slug, dname, str(body.get("text") or ""), mode=mode)
                except (KeyError, ValueError) as e:
                    return self._json(400, {"ok": False, "error": str(e)})
                # `verified` 是"写完读回来核过"的事实 —— 界面必须显示它，
                # 否则"改了但没生效"会被渲染成"改好了"。
                return self._json(200, {"ok": bool(r.get("ok")), "slug": slug,
                                        "doc": r.get("doc") or dname,
                                        "chars": r.get("chars") or 0,
                                        "verified": bool(r.get("verified"))})
            if action in ("status", "status_all"):
                # status = 当前课题的台账；status_all = 全部课题（切课题要用这份清单）。
                # 两份都由 `topic_status` 一个工具给出，不在这儿拼第二套真相。
                return self._json(200, self.mgr.topic_tool("topic_status", slug=slug))
            return self._json(400, {"error": f"未知的 action：{action}"})

        if u.path != "/api/run":
            if u.path == "/api/contract":
                return self._contract()
            return self._json(404, {"error": "not found"})

        body = read_json_object(self)

        goal = str(body.get("goal") or "").strip()
        frozen = body.get("contract")
        if not goal and isinstance(frozen, dict):
            goal = str(frozen.get("raw_goal") or "").strip()
        if not goal:
            return self._json(400, {"error": "请先填写任务目标"})

        ms = body.get("max_steps")
        try:
            ms = int(ms) if ms not in (None, "", 0) else None
        except Exception:
            ms = None

        # ★ 导师模式开关（W22-B）：字段读法与 `/api/follow` 共用 `kb_only_from`。
        mentor = kb_only_from(body)

        run, err = self.mgr.start(goal, ms,
                                  frozen if isinstance(frozen, dict) else None,
                                  mentor=mentor,
                                  mode=mode_from(body),
                                  preset=str(body.get("preset") or "").strip())
        if run is None:
            # 授权不足是 403（"你没有权限"），与"忙"的 409 分开 ——
            # 界面据此决定该弹激活框还是提示等一会儿。
            if _license_denial(err):
                return self._json(403, {"error": err, "license": True,
                                        "license_state": self.mgr.license_state()})
            return self._json(409, {"error": err})
        return self._json(200, {"id": run.id, "goal": run.goal})

    # ── 契约预审（只起草，不执行）────────────────────────────
    def _contract(self) -> None:
        """把一句话目标拆成契约草稿并返回，**不启动任务**。

        存在的理由：原来是"填完目标就直接开跑"，人只有在任务跑完
        才知道内核把目标理解成了什么。有了这个接口，可以先审契约、
        改完再带着冻结契约开跑（`/api/run` 的 `contract` 字段）。
        """
        # ★ 请求体**只读一次**。`read_json_object(self)` 读的是 `rfile`，
        #   它消费完就到底了 —— 第二次数出来的是 `{}`。我第一版在这里写了两次，
        #   于是 `mode` 永远是"自动"、`answer_only` 永远是假，而且**不报错**
        #   （契约照常返回，只是那一个字段是错的）。这是本仓最典型的一类缺陷：
        #   一条链上两处各读一次同一个流，第二处静默拿到空。
        body = read_json_object(self)
        goal = str(body.get("goal") or "").strip()
        if not goal:
            return self._json(400, {"error": "请先填写任务目标"})
        k = self.mgr.kernel
        if k is None:
            return self._json(409, {"error": f"内核未就绪：{self.mgr.kernel_error}"})
        try:
            from ..contract import TaskContract
            c = TaskContract.draft(goal, k.llm)
            # ★ 预审面板也要看见「这一轮是不是仅问答」 —— 否则用户按了开关、
            #   面板却只显示一份空验收标准，问题又回到"到底为什么没有标准"。
            if mode_from(body) == "answer":
                c.answer_only = True
            elif mode_from(body) == "task":
                # ★ 用户按了「任务模式」⇒ 必须**真的**按任务走，哪怕起草器/词表
                #   把这句话判成了问话。只看 answer 那一半是"开关只朝一个方向通"，
                #   实测就是这样：切回任务模式后契约里 answer_only 仍是 True。
                c.answer_only = False
            return self._json(200, {"contract": c.to_dict()})
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rongke-web", description="荣珂 Web 控制台")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--no-boot-check", action="store_true", help="跳过启动自检")
    args = ap.parse_args(argv)

    httpd = Server((args.host, args.port), Handler)
    httpd.mgr = Manager()               # type: ignore[attr-defined]

    url = f"http://{args.host}:{args.port}/"
    print("═" * 62)
    print("  荣珂 Web 控制台")
    print(f"  {url}")
    mgr = httpd.mgr                    # type: ignore[attr-defined]
    if mgr.kernel is not None:
        st = mgr.status()
        p = st.get("provider") or {}
        print(f"  模型：{p.get('name')} / {p.get('model')}  [{p.get('state')}]")
        print(f"  工具：{st.get('tools')} 个（领域 {st.get('domain_tools')}）")
    else:
        print(f"  ⚠ 内核未就绪：{mgr.kernel_error}")
        print("    服务仍会启动，可在页面上点「重试装载」。")
    print("═" * 62)
    print("  Ctrl+C 停止")

    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
