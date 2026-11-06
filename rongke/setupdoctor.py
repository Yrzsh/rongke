# -*- coding: utf-8 -*-
"""首次使用向导 —— 「做 PPT 和画图要用的那些工具，配好了没有」。

## 它补的是哪一处缺失

内核过去**没有任何一处**回答"我这台机器能不能做 PPT、能不能画图"。
能力是有的（`deck.py` 跑组会 PPT 体检、`figure.py` 跑图面审计、MCP 连 Origin 出图），
但它们的缺失形态全是**静默降级**：

  · 写讲稿备注要 python-pptx 的那个解释器 —— 没有 ⇒ 模型拿到一句
    `ModuleNotFoundError`，它既看不懂也修不了，只会换个参数重试；
  · 组会 PPT 体检要 `nature-paper2ppt` 的审计脚本 —— 找不到 ⇒ 判 `SKIPPED`，
    而 `SKIPPED` **不算通过**，于是交付门永远不放行，用户看不出为什么；
  · 图面审计要 matplotlib + PyMuPDF —— 荣珂自己可能跑在裸解释器上；
  · Origin 出图走 MCP 子进程 —— 服务端起不来只会记进 `skipped`；
  · `deck.template` 指向的 .pptx 被挪走 ⇒ 出稿时才发现。

⇒ 这一个模块把这些**逐项真探一遍**，并给每一项写清"缺什么、去哪儿补"。

## 与 `mcp_doctor` 的分工（不重叠，也不互相替代）

| | 管什么 |
|---|---|
| `mcp_doctor.py` | MCP **协议层**能不能开工（命令在不在、重名、真装一遍、AnySearch key） |
| `setupdoctor.py` | **做 PPT / 画图的工具与端到端装置**：解释器里有哪个库、审计脚本在不在、模板在不在、Origin 的 COM 在不在 |

两者共用 `CheckResult` 与四个级别（本模块**直接 import** 它，不抄第二份）——
"一条检查长什么样"只该有一处定义。

## 三条纪律（每条都对应本仓反复踩过的坑）

1. **结论打在哪一侧，必须写清楚。** `rongke/deck.py` 对 `find_deck_python` 专门
   留了一段注释：审计器纯标准库谁都能跑，但**写备注**要 python-pptx，
   合成一个函数就会长出「审计跑得动 ⇒ 以为备注也写得动」的错觉。
   所以这里的每一项都注明它探的是**荣珂自己那个进程**（`sys.executable`）
   还是**下游子进程解释器**（`deck.python` / `figure.python` / MCP 的 command）。

2. **修完必须重测，且重测读的是新配置。**
   `fix_setup()` 写盘之后会 `Config.load()` 现装一份再重跑那一条 ——
   如果重测还用进来时那个 `cfg`，读到的仍是旧路径，那就是一条**永远报失败**的假判据。

3. **不配也说明白，绝不阻断。** 做 PPT 是主活，画图不是唯一的路。
   只有 `core=True` 那几项缺了才判 `broken`；画图 / Origin / 渲染缺了判 `degraded`，
   措辞是"用不了"而不是"没配好"。而且本模块**只报不拦** —— 没有任何门调它。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .config import Config, _deep_merge, _read_json, project_root

# 四个级别与 CheckResult **只有一处定义**（在 mcp_doctor 里）。
# 抄一份过来就是两处真相：那边改文案，这边不会跟着改。
from .mcp_doctor import CheckResult, LV_FAIL, LV_INFO, LV_OK, LV_WARN  # noqa: F401

#: 自检器的版本。与 `mcp_doctor.DOCTOR_VERSION` 同一条用法：报告里带着它，
#: 「用户看到的那份报告是哪一版判据出的」才追得回来。
DOCTOR_VERSION = "setup/1"

#: 探测子进程的超时。**不是越短越好**：装了十几个解释器的机器上，
#: 冷启动 `import importlib.util` + 反病毒软件还可能插一手。
PROBE_TIMEOUT = 20.0

#: 起一次 MCP 服务端脚本、看它会不会当场因缺库死掉时等多久。
#: 这几个脚本起得来就会阻塞在 stdin 上等 JSON-RPC，所以等不到也会超时退出
#: —— 超时是**正常路径**，判据只看 stderr 有没有 `ModuleNotFoundError`。
LAUNCH_TIMEOUT = 10.0

#: 「哪些库装在哪个解释器里」的探测缓存。
#: 键是 `解释器|模块|…`；值 `(bool, 失败原因)`。缓存的是**事实**（装没装），
#: 不是"上次行不行" —— 所以修完配置要清一次（见 `fix_setup`）。
_PROBE_CACHE: dict[str, tuple[bool, str]] = {}

#: `origin_com()` 的结果缓存：`(探的时刻, 结果)`。
#: **单开一个**而不是塞进 `_PROBE_CACHE`：那个字典的键是「解释器|模块」形状，
#: 值语义是"装没装"，而这里是"COM 通不通 + 也许还要拉起一个实例"。
#:
#: ★ 为什么必须有它（W30 实测）：这台机器装了 Origin 2019b，`Dispatch` 在
#:   **冷的时候要拉起一个隐藏的 Origin 实例** —— 空载 9.2 秒，机器忙时
#:   （全量回归在跑）实测 **37.6 秒**。而 `/api/setup` 是「首次配置」页每次
#:   打开都打的接口，原先每次都重付这一笔：整页热加载 25.8 秒。
#:   加缓存后热路径该项 0 ms，整页降到 3.3 秒。
#:
#: ★ **带 TTL，不是永久缓存**。这一点我第一版写错了：当时在注释里写"装了要
#:   重启程序才生效"，对 COM 不成立 —— 用户装完 Origin、注册表项一起来，
#:   下一次 `Dispatch` 就该成功。永久缓存会让用户"装完了、页面还说没装"，
#:   而那正是这个自检页存在的唯一理由。5 分钟足够挡住"反复开页面"的重付，
#:   又短到用户照提示装完、切回来就看见新结论。
#: 探测失败时也用同一个 TTL：失败尤其可能是"Origin 正在启动中"的瞬时态。
_ORIGIN_CACHE: dict[str, tuple[float, bool, str]] = {}
_ORIGIN_TTL_S = 300.0


def _clear_cache() -> None:
    """清掉本模块的探测缓存。**只清这里**，不去动 `figure._probe_cache`。"""
    _PROBE_CACHE.clear()
    _ORIGIN_CACHE.clear()


# ════════════════════════════════════════════════════════════════
# 基础探测
# ════════════════════════════════════════════════════════════════

def probe_modules(exe: str, modules: tuple[str, ...]) -> tuple[bool, str]:
    """这个解释器里，`modules` 是不是**全都在**？返回 `(全在, 缺什么)`。

    ★ 子进程里必须写 `import importlib.util` 而**不是** `import importlib`
      —— 后者在子进程里 `importlib.util` 尚未绑定，会抛 `AttributeError`、
      退出码 1，于是**每个解释器都被判成"不能用"**。这个假阴性会让整条
      探测链静默失效（`figure.py:_interpreter_ok` 的注释里记着这次实测）。

    ★ 只 `find_spec`，**不真 import**：import matplotlib 要起整个绘图库，
      而这里只需要知道它在不在。
    """
    exe = (exe or "").strip()
    if not exe or not Path(exe).exists():
        return False, "解释器不存在"
    key = exe + "|" + ",".join(modules)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    code = ("import importlib.util, json, sys\n"
            "miss = [m for m in %r if importlib.util.find_spec(m) is None]\n"
            "print(json.dumps(miss))\n"
            "sys.exit(0 if not miss else 1)\n" % (list(modules),))
    missing = list(modules)          # 默认「全缺」：探不动时**不许**当成装好了
    try:
        r = subprocess.run([exe, "-c", code], capture_output=True,
                           timeout=PROBE_TIMEOUT)
        out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        if out:
            got = json.loads(out.splitlines()[-1])
            if isinstance(got, list):
                missing = [str(x) for x in got]
        elif r.returncode != 0:
            err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
            tail = err.splitlines()[-1] if err else "无输出"
            res = (False, f"解释器起不来（退出码 {r.returncode}）：{tail}")
            _PROBE_CACHE[key] = res
            return res
    except subprocess.TimeoutExpired:
        res = (False, f"探测超时（>{PROBE_TIMEOUT:.0f}s）")
        _PROBE_CACHE[key] = res
        return res
    except Exception as e:                                      # noqa: BLE001
        res = (False, f"{type(e).__name__}: {e}")
        _PROBE_CACHE[key] = res
        return res
    res = (not missing, ("缺 " + "、".join(missing)) if missing else "")
    _PROBE_CACHE[key] = res
    return res


def _launch(exe: str, script: Path, argv: list[str], *,
            timeout: float = LAUNCH_TIMEOUT) -> tuple[int, str]:
    """用**和 MCP 同一条**命令（exe + 脚本）起一次子进程，看它会不会以「缺库」收场。

    光看文件在不在没用 —— 它 `import` 的那几个包在不在才是真问题。
    而"起一个真 MCP 服务端再握手"要几十秒，首次使用向导不该让人对着转圈等。

    ⇒ 折中：真起一次，喂一个空 stdin（读到 EOF 会自己退），最多等 `timeout` 秒。
      被我们杀掉时退出码非 0 是**预期的**；判据只认 stderr 里有没有
      `ModuleNotFoundError` / `ImportError` —— 那两个是"永远起不来"，
      其余（没装 Origin、慢、收不到正常请求）都是"起得来但当时不通"。
    """
    exe = (exe or "").strip() or sys.executable
    try:
        p = subprocess.run([exe, str(script)] + list(argv), capture_output=True,
                           timeout=timeout, input=b"", cwd=str(script.parent))
        return p.returncode, (p.stderr or b"").decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        err = e.stderr or b""
        err_s = err if isinstance(err, str) else err.decode("utf-8", errors="replace")
        return -1, err_s
    except Exception as e:                                      # noqa: BLE001
        return -2, f"{type(e).__name__}: {e}"


def _import_crash(err: str) -> str:
    """stderr 里有没有"这个模块永远起不来"的证据？返回那一行，没有则空串。"""
    for ln in (err or "").splitlines():
        s = ln.strip()
        if "ModuleNotFoundError" in s or "ImportError" in s:
            return s
    return ""


def origin_com() -> tuple[bool, str]:
    """Origin 的 COM 通道在不在（Windows only）。

    为什么要单独探：`origin_mcp_server.py` 能起来，不代表背后有 Origin ——
    `origin_status` 调用时才连 COM，而那时用户已经在任务里了。
    这里先探一次，**只探不画图**（会起一个隐藏的 Origin 实例）。

    ★ **必须先 `CoInitialize`。** COM 是**按线程**初始化的，而 Web 控制台
      是在 `http.server` 的**工作线程**里调这个函数的 —— 实测同一条机器上：
      主线程 `Dispatch('Origin.ApplicationSI')` → OK，
      工作线程不 CoInitialize 直接 Dispatch → `com_error(-2147221008,
      '尚未调用 CoInitialize。')`。不修这一步，"首次配置"页会稳定报
      「Origin 连不上」而命令行 `--setup` 报「通」—— 同一个事实两个答案，
      用户会去重装 Origin（本条就是被一次真实探测逮到的）。
      解释器退出时 CoUninitialize 由进程收尾兜底，这里**不加**配对释放：
      配错一次 `Uninitialize` 会把别的线程的引用计数一起减掉。

    ★ 探到的实例**不 Quit**：那是用户的 Origin。多杀一个用户的程序，
      比"多开一个实例"糟得多（`deck.py` 对 PowerPoint 是同一条纪律）。

    ★ 结果**缓存 5 分钟**（`_ORIGIN_CACHE`，见上面常量处的说明）。
      一句话理由：这台机器上未命中缓存的那一次要 **9.2 秒**（机器忙时 37.6 秒，
      实测），而 `/api/setup` 是配置页每次打开都打的接口 —— 原先整页热加载
      25.8 秒，加上缓存后 3.3 秒。带 TTL 是因为**装完 Origin 就该看见新结论**，
      永久缓存会让用户对着"还没装"的提示干瞪眼。
    """
    if os.name != "nt":
        return False, "非 Windows，没有 COM"
    hit = _ORIGIN_CACHE.get("probe")
    if hit is not None:
        at, ok, why = hit
        if time.time() - at < _ORIGIN_TTL_S:
            return ok, why
    res = _origin_com_probe()
    _ORIGIN_CACHE["probe"] = (time.time(), res[0], res[1])
    return res


def _origin_com_probe() -> tuple[bool, str]:
    """真去摸一次 COM。**只该由 `origin_com()` 调** —— 直接调会绕开缓存。"""
    try:
        import pythoncom      # pywin32 自带；有 win32com 就一定有它
        import win32com.client  # type: ignore
    except Exception as e:                                      # noqa: BLE001
        return False, f"没装 pywin32（{type(e).__name__}: {e}）"
    try:
        pythoncom.CoInitialize()
    except Exception as e:                                      # noqa: BLE001
        return False, f"CoInitialize 失败（{type(e).__name__}: {e}）"
    try:
        app = win32com.client.Dispatch("Origin.ApplicationSI")
    except Exception as e:                                      # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    ver = ""
    try:
        ver = str(app.GetVersion() or "")
    except Exception:                                           # noqa: BLE001
        pass
    return True, ver


def _which_soffice() -> str:
    """LibreOffice 的 soffice：PATH 优先，其次 Windows 的默认安装位置。"""
    got = shutil.which("soffice") or shutil.which("soffice.exe")
    if got:
        return got
    for c in (Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
              Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe")):
        if c.is_file():
            return str(c)
    return ""


def _has_powerpoint() -> bool:
    """本机有没有 PowerPoint。**只看文件在不在**，不去起 COM —— 起 PowerPoint
    要十几秒，而首次使用向导不该为了报一行"有"就让用户等。

    `deck_preview.py` 那条路（COM 渲染）真正跑的时候自己会验；这里只报可能。
    """
    if os.name != "nt":
        return False
    for c in (Path(r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE"),
              Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE"),
              Path(r"C:\Program Files\Microsoft Office\Office16\POWERPNT.EXE")):
        if c.is_file():
            return True
    return False


# ════════════════════════════════════════════════════════════════
# 造一条检查（带 side / core 两个本模块独有的归因）
# ════════════════════════════════════════════════════════════════

def _imp(key: str, side: str, core: bool, level: str, title: str, *,
         detail: str = "", hint: str = "", fixable: bool = False,
         evidence: dict | None = None) -> CheckResult:
    """造一条带**归因**的检查结果。

    `CheckResult`（定义在 `mcp_doctor.py`）的字段就那几个。本模块另外要多两个：
    `side`（这条探的是**荣珂自己那个进程**还是**下游子进程解释器**）与
    `core`（缺了它做不了 PPT）。⇒ 用 `setattr` 挂上去，**不改 `mcp_doctor`
    的 dataclass** —— 那个 class `--mcp-doctor` 也在用，为一边加字段而让另一边的
    `as_dict()` 多出两个键，等于改了一份不属于本功能的契约。
    """
    r = CheckResult(key=key, level=level, title=title, detail=detail,
                    hint=hint, fixable=fixable, evidence=dict(evidence or {}))
    r.side = side               # type: ignore[attr-defined]
    r.core = bool(core)         # type: ignore[attr-defined]
    r.as_dict = lambda: {       # type: ignore[method-assign]
        "key": r.key, "level": r.level, "title": r.title,
        "detail": r.detail, "hint": r.hint,
        "fixable": r.fixable, "fixed": r.fixed, "evidence": r.evidence,
        "side": r.side, "core": r.core,
    }
    return r


def _as_row(c: CheckResult) -> dict:
    """一条检查 → 给界面/CLI 用的纯数据行（`side` / `core` 已含在 `as_dict()` 里）。"""
    return c.as_dict()


# ════════════════════════════════════════════════════════════════
# 上下文：一次自检要用到的事实（只装不改）
# ════════════════════════════════════════════════════════════════

class _Ctx:
    def __init__(self, cfg: Config, root: Path):
        self.cfg = cfg
        self.root = root
        # 荣珂自己跑在哪个解释器上 —— 就是 sys.executable，不另立来源。
        self.self_exe = sys.executable

    def template_name_of(self, explicit: str) -> str:
        """模板名 → 槽位表目录名。**裁决权在 `deck.template_name_of`，这里不另立。**"""
        try:
            from . import deck as _d
            return _d.template_name_of(explicit)
        except Exception:                                       # noqa: BLE001
            return ""


def _pptx_elsewhere() -> str:
    """本机**别的**解释器里有没有 python-pptx（"能不能自动修"的判据）。

    用 `figure._PY_CANDIDATES` 那张已知路径表 —— 那是本仓唯一一份"本机有哪些
    Python"的清单，在这里再抄一份就是第二处真相。
    """
    try:
        from .figure import _PY_CANDIDATES
    except Exception:                                           # noqa: BLE001
        return ""
    for exe in _PY_CANDIDATES:
        if exe != sys.executable and Path(exe).exists() \
                and probe_modules(exe, ("pptx",))[0]:
            return exe
    return ""


def _figure_py_elsewhere(have: str) -> str:
    """本机已知路径里，有没有一个装齐了 matplotlib + PyMuPDF 的解释器。"""
    from .figure import _NEEDED_MODULES, _PY_CANDIDATES
    for exe in _PY_CANDIDATES:
        if exe == have or not Path(exe).exists():
            continue
        if probe_modules(exe, _NEEDED_MODULES)[0]:
            return exe
    return ""


# ════════════════════════════════════════════════════════════════
# 逐项检查
# ════════════════════════════════════════════════════════════════

def check_ai_channel(ctx: _Ctx) -> list[CheckResult]:
    """模型通道：**一切**活的前提（没它一步都跑不了）。

    ★ 这一项不探网络（首次使用向导不该卡在网关上），只报配置齐不齐。
      密钥**只报"在不在"，不回显值、不报长度** —— 长度就够把一位数密钥猜个范围。
    """
    pv = ctx.cfg.provider
    if pv.ready:
        return [_imp("llm.channel", "rongke", True, LV_OK,
                     "模型通道已配齐", detail=f"{pv.name} · {pv.model}",
                     evidence={"provider": pv.name, "model": pv.model,
                               "key_env": pv.api_key_env})]
    return [_imp("llm.channel", "rongke", True, LV_FAIL,
                 "模型通道没配好（现在一步活都干不了）", detail=pv.diagnose(),
                 hint="在「设置」页填模型名与密钥（值落在 ~/.rongke/secrets.json，不进 config.json）。",
                 evidence={"provider": pv.name, "model": pv.model,
                           "key_env": pv.api_key_env})]


def check_deck_notes(ctx: _Ctx) -> list[CheckResult]:
    """写讲稿备注：**下游子进程解释器里**有没有 python-pptx。

    这一项就是 `deck.find_notes_python` 的前提。没有它，模型调
    `set_slide_notes` 会拿到 `ModuleNotFoundError` —— 而组会 PPT 的备注栏
    就是讲稿，结果是一份"技术上合规、上台没法讲"的稿子。
    """
    cfg = ctx.cfg
    exe = (cfg.deck.python or "").strip() if cfg.deck else ""
    cands: list[tuple[str, str]] = []
    if exe:
        cands.append((exe, "配置里 deck.python"))
    cands.append((ctx.self_exe, "荣珂当前使用的解释器"))
    hit, where = "", ""
    for c_exe, c_why in cands:
        if probe_modules(c_exe, ("pptx",))[0]:
            hit, where = c_exe, c_why
            break
    if hit:
        return [_imp("deck.notes.python", "sub", True, LV_OK,
                     "写讲稿备注：python-pptx 就位",
                     detail=f"{where} → {hit}",
                     evidence={"exe": hit, "why": where, "config": exe,
                               "config_used": bool(exe and hit == exe)})]
    other = _pptx_elsewhere()
    return [_imp(
        "deck.notes.python", "sub", True, LV_FAIL,
        "写讲稿备注用不了（没有 python-pptx）",
        detail=(f"配置的 {exe} 里没有，荣珂当前解释器（{ctx.self_exe}）里也没有"
                if exe else f"荣珂当前解释器（{ctx.self_exe}）里没有 python-pptx"),
        hint=("装上它：`\"<你的 python.exe>\" -m pip install python-pptx`。"
              "装到哪个解释器很关键 —— 装到别处等于没装。"
              + (f"（本机的 {other} 里已经有了，点「一键修」把 deck.python 指过去。）"
                 if other else "")),
        fixable=bool(other),
        evidence={"self": ctx.self_exe, "config": exe, "elsewhere": other})]


def check_deck_audit(ctx: _Ctx) -> list[CheckResult]:
    """组会 PPT 体检：审计脚本在不在、它那个解释器起不起得来。

    找不到 ⇒ 第 9 条硬门判 `SKIPPED`，而 **`SKIPPED` 不算通过** ——
    交付门永远不放行，而界面上只写一句"体检装置不可用"。
    """
    cfg = ctx.cfg
    if not (cfg.deck and cfg.deck.enabled):
        return [_imp("deck.audit", "rongke", False, LV_INFO,
                     "组会 PPT 体检：配置里关着（deck.enabled=false）",
                     detail="关着的门不报故障，但它也不替你检查稿子。")]
    from .deck import find_audit_script, find_deck_python
    script = find_audit_script(cfg.deck.skills_dir or None)
    if script is None:
        return [_imp(
            "deck.audit.present", "rongke", True, LV_FAIL,
            "组会 PPT 体检跑不了（找不到审计脚本）",
            detail=("在 " + (cfg.deck.skills_dir or "~/.agents/skills、~/.dsh/skills")
                    + " 下都没有 nature-paper2ppt/scripts/audit_pptx_quality.py"),
            hint=("装 nature-paper2ppt 技能（放到 ~/.agents/skills/ 下即可），"
                  "或把它的根目录写进 config.json 的 deck.skills_dir。"
                  "缺它的时候第 9 条硬门判 SKIPPED，而 SKIPPED 不算通过。"),
            evidence={"skills_dir": cfg.deck.skills_dir})]
    exe = find_deck_python(cfg.deck.python or None)
    return [_imp(
        "deck.audit.present", "sub", True, LV_OK if exe else LV_FAIL,
        "组会 PPT 体检就位" if exe else "组会 PPT 体检的解释器起不来",
        detail=f"{script}" + (f"（用 {exe} 跑）" if exe else ""),
        hint="" if exe else "config.json 的 deck.python 指向的解释器起不来：改掉，或清空让它自动探测。",
        evidence={"script": str(script), "exe": exe or "",
                  "skills_dir_used": cfg.deck.skills_dir})]


def check_deck_template(ctx: _Ctx) -> list[CheckResult]:
    """品牌模板 .pptx 与它的槽位表：**一对**，必须同源。

    `DeckConfig.template` 的注释写得很直：那份 .pptx 与
    `docs/deck-templates/<名>/slots.json` 必须同源，否则会变成"开工清单说的是
    A 模板的页型、出稿却拿 A 的页号去 B 的稿上填"—— 最难查的一类错
    （两边都跑得通，只是内容错位）。
    """
    cfg = ctx.cfg
    tpl = (cfg.deck.template or "").strip() if cfg.deck else ""
    if not tpl:
        return [_imp("deck.template.present", "rongke", False, LV_INFO,
                     "品牌模板：没配（用内核自带版式）",
                     detail="config.json 的 deck.template 是空的 —— 不算故障。",
                     hint="要用学校/课题组的模板：把 .pptx 的绝对路径填进 deck.template。")]
    p = Path(tpl)
    name = ctx.template_name_of(tpl)
    slots = Path(ctx.root) / "docs" / "deck-templates" / name / "slots.json"
    if not p.is_file():
        return [_imp(
            "deck.template.present", "rongke", True, LV_FAIL,
            "品牌模板：那份 .pptx 不在盘上了",
            detail=f"deck.template = {tpl}（模板名 {name or '?'}）",
            hint=("模板被挪走或改了名。把 deck.template 改成现在的位置，或清空它改用内置版式。"
                  "**别删槽位表** —— 那份才是随库的能力定义。"),
            evidence={"template": tpl, "template_name": name,
                      "slots": str(slots), "slots_ok": slots.is_file()})]
    if not slots.is_file():
        return [_imp(
            "deck.template.present", "rongke", True, LV_FAIL,
            "品牌模板：找不到它的槽位表",
            detail=f"期望 {slots}",
            hint="槽位表由 `python deck_template_extract.py` 从那份 .pptx 抽出来；没有它就只能回退内置版式。",
            evidence={"template": tpl, "template_name": name, "slots": str(slots)})]
    return [_imp("deck.template.present", "rongke", True, LV_OK,
                 "品牌模板就位（.pptx 与槽位表同源）",
                 detail=f"{p.name} → docs/deck-templates/{name}/slots.json",
                 evidence={"template": tpl, "template_name": name,
                           "slots": str(slots), "slots_ok": True})]


def check_figure_backend(ctx: _Ctx) -> list[CheckResult]:
    """图面审计：**下游子进程解释器里**要 matplotlib + PyMuPDF。

    一定要探下游那一侧：`figure._PY_CANDIDATES` 的注释记着，荣珂可能在什么都
    没装的裸解释器上跑（实测托管 3.13.12 连 numpy 都没有），所以
    "荣珂自己能 import matplotlib"完全不能说明审计能用。
    """
    cfg = ctx.cfg
    if not (cfg.figure and cfg.figure.enabled):
        return [_imp("figure.python", "sub", False, LV_INFO,
                     "图面审计：配置里关着（figure.enabled=false）")]
    from .figure import _NEEDED_MODULES, find_figure_python
    exe = find_figure_python(cfg.figure.python or None)
    if exe:
        return [_imp("figure.python", "sub", False, LV_OK,
                     "图面审计的解释器就位（matplotlib + PyMuPDF）",
                     detail=exe,
                     evidence={"exe": exe, "needed": list(_NEEDED_MODULES),
                               "config": cfg.figure.python})]
    other = _figure_py_elsewhere(cfg.figure.python or "")
    return [_imp(
        "figure.python", "sub", False, LV_FAIL,
        "图面审计跑不了（没有带 matplotlib + PyMuPDF 的解释器）",
        detail="配置的、荣珂当前的、以及已知路径里的解释器，没有一个同时装齐这两个库",
        hint=("任意一个 Python 里 `-m pip install matplotlib pymupdf`，再把它的绝对路径"
              "写进 config.json 的 figure.python。**画图本身不需要它** —— 它是交付前的自检。"
              + (f"（本机的 {other} 里已经齐了，点「一键修」指过去。）" if other else "")),
        fixable=bool(other),
        evidence={"needed": list(_NEEDED_MODULES), "elsewhere": other,
                  "config": cfg.figure.python})]


def check_figure_skills(ctx: _Ctx) -> list[CheckResult]:
    """图面审计要 nature-figure 技能里的 `validate_figure.py`。"""
    cfg = ctx.cfg
    if not (cfg.figure and cfg.figure.enabled):
        return [_imp("figure.scripts", "sub", False, LV_INFO,
                     "图面技能的审计脚本：配置里关着")]
    from .figure import locate_scripts
    got = locate_scripts(cfg.figure.skills_dir or None)
    if got:
        return [_imp("figure.scripts", "sub", False, LV_OK,
                     "图面技能的审计脚本就位", detail=str(got),
                     evidence={"scripts": str(got)})]
    return [_imp(
        "figure.scripts", "sub", False, LV_FAIL,
        "图面审计跑不了（找不到 nature-figure 的审计脚本）",
        detail=("在 " + (cfg.figure.skills_dir or "~/.agents/skills、~/.workbuddy/skills、vendor/")
                + " 里都没有 validate_figure.py"),
        hint="装 nature-figure 技能到 ~/.agents/skills/ 下，或把技能根目录写进 config.json 的 figure.skills_dir。",
        evidence={"skills_dir": cfg.figure.skills_dir})]


def check_literature_chain(ctx: _Ctx) -> list[CheckResult]:
    """**检索与取全文**这条链：写综述要用它，而它缺件时的表现全是"下不到"。

    为什么这项必须进首次自检（2026-10-22 加）：

    在此之前九项检查全是 PPT / 画图 / Office —— **一条都不管文献**。
    而真跑一次综述（`runs/20261014-114007`）的实际结果是：入 15 篇，
    磁盘上只落下 3 篇全文（20%）。用户看到的现象是"这工具查不到东西"，
    而根因分散在三个地方，没有一处会报错：

      ① `literature_fetch` 的三条路里，**Europe PMC 那条原来根本不存在**
         （整份 litkb.py `grep -i europepmc` 命中 0）；
      ② 库的 trigram 索引对**中文两字词**静默回空（融合/温度/酶解全废）；
      ③ 中文文献（CNKI）走不了国际 OA 那条路，需要机构登录态。

    ②③ 是运行时才知道的，这里探不了。**①是静态可探的**，所以探它：
    探的是"内核自带的取全文能力在不在、它依赖的网络出口通不通"。

    ★ 探**一个真实 DOI**，不是只判模块在不在：模块 import 得动不等于
      网络出口通。这台机器实测 `github.com` 与 `raw.githubusercontent.com`
      全部被重置，只有 `api.github.com` / `codeload.github.com` 通 ——
      "本机能上网"这句话在这里是假的，必须真发一次请求才知道。
    """
    out: list[CheckResult] = []
    from . import litfetch as _lf

    # ① 模块在不在（静态）
    out.append(_imp(
        "lit.fetch.module", "rongke", False, LV_OK,
        "取全文能力：内核自带（不依赖外部下载器）",
        detail=("三条路：Europe PMC fullTextXML → Unpaywall OA 直链 → doi.org 内容协商。"
                "工具名 `literature_fetch`，只用标准库。"),
        evidence={"routes": ["europepmc-fulltextxml", "unpaywall-pdf",
                             "doi-content-negotiation"]}))

    # ② 网络出口：真打一次 Europe PMC 检索接口
    #    挑的是一篇必定在 PMC 里的开放获取文献（PLOS ONE）。
    probe_doi = "10.1371/journal.pone.0123456"
    try:
        pmcid, _title = _lf._pmcid_for(probe_doi)
    except Exception as e:                                     # noqa: BLE001
        out.append(_imp(
            "lit.fetch.network", "rongke", False, LV_WARN,
            "取全文：Europe PMC 这次没问通",
            detail=f"{type(e).__name__}: {e}",
            hint="网络出口可能被拦。取全文还有 Unpaywall 与 doi.org 两条路，"
                 "但 Europe PMC 是最稳的那条 —— 换网络环境再试一次。",
            evidence={"probe_doi": probe_doi}))
    else:
        if pmcid:
            out.append(_imp(
                "lit.fetch.network", "rongke", False, LV_OK,
                "取全文：Europe PMC 通路可用（已用真实 DOI 探通）",
                detail=f"{probe_doi} → {pmcid}",
                evidence={"probe_doi": probe_doi, "pmcid": pmcid}))
        else:
            out.append(_imp(
                "lit.fetch.network", "rongke", False, LV_WARN,
                "取全文：Europe PMC 问得通，但这篇没查到 PMC 记录",
                detail=(f"{probe_doi} 没回 pmcid。可能是接口改版、也可能是"
                        "该 DOI 确实不在 PMC（探针文献本身失效了）。"),
                hint="不影响使用：真正取全文时按真实 DOI 查，"
                     "Unpaywall 与 doi.org 两条路仍在。",
                evidence={"probe_doi": probe_doi}))

    # ③ Unpaywall 的 email 参数：官方要求带，匿名会被限流
    #    这条只报事实，不判失败 —— 不带也能用（带 hardcode 的兜底值）。
    out.append(_imp(
        "lit.fetch.unpaywall", "rongke", False, LV_INFO,
        "Unpaywall：不需要 key，但要带 email 才有正常配额",
        detail=("内核默认用占位 email。要更好的配额就把自己的邮箱写进环境变量 "
                "UNPAYWALL_EMAIL（当前：" +
                (os.environ.get("UNPAYWALL_EMAIL") or "未设置") + "）"),
        hint="可选。不配也能用。"))

    # ④ 中文文献（CNKI）：如实说明这条路**走不了**，别让它看起来是"忘了配"
    _school = Path.home() / ".config" / "lit-dl" / "school.json"
    if _school.is_file():
        out.append(_imp(
            "lit.cnki", "sub", False, LV_INFO,
            "中文文献（CNKI）：本机有机构配置，但内核不直接走它",
            detail=(f"发现 {_school}（学校文献代理配置）。"
                    "荣珂的 `literature_fetch` 只走国际 OA 三条路 —— "
                    "CNKI 需要**已登录的浏览器**，那是另一条链。"),
            hint="要下中文全文：用浏览器登录学校图书馆后手动下载 PDF，"
                 "再把 PDF 交给 `kb_add` / `kb_ingest` 入库。"
                 "或者把 PDF 放到工作区里让它读。",
            evidence={"school_config": str(_school)}))
    else:
        out.append(_imp(
            "lit.cnki", "sub", False, LV_INFO,
            "中文文献（CNKI）：本机没有机构配置",
            detail="`literature_fetch` 不覆盖 CNKI（它需要已登录的浏览器）。",
            hint="中文全文靠浏览器登录学校图书馆后手动下载，再交给 kb_add 入库。"))
    return out


def check_mcp_servers(ctx: _Ctx) -> list[CheckResult]:
    """MCP 服务端（Origin 出图、PPT 服务端…）：命令在不在、缺不缺库。

    与 `--mcp-doctor` 的分工：那边要**真装一遍**（起满子进程握手），跑一次几十秒；
    这里是首次使用向导，只做到"命令在 + 脚本在 + 能 import 得动"。
    """
    from .mcp_doctor import _check_command
    specs = list(getattr(ctx.cfg.mcp, "servers", []) or [])
    if not specs:
        return [_imp(
            "mcp.servers", "rongke", False, LV_INFO,
            "MCP 服务端：一个都没配",
            detail="Origin 出图那条路要它",
            hint="要跑 Origin 出图：在 config.json 的 mcp.servers 里加一段 origin-figure"
                 "（command 指向 Python，args 指向 origin_mcp_server.py）。"
                 "不想用 Origin 也没关系 —— 内核自带的 python 绘图路线不需要它。")]
    out: list[CheckResult] = []
    for spec in specs:
        name = getattr(spec, "name", "?")
        if getattr(spec, "disabled", False):
            out.append(_imp(f"mcp:{name}", "rongke", False, LV_INFO,
                            f"{name}：刻意关着（disabled）",
                            detail="配置里显式关掉的，不算故障。"))
            continue
        cmd = _check_command(spec)              # ← 复用，不重写
        out.append(_imp(f"mcp:{name}", "sub", False, cmd.level, cmd.title,
                        detail=cmd.detail, hint=cmd.hint, evidence=cmd.evidence))
        if cmd.level == LV_FAIL:
            continue
        exe = (getattr(spec, "command", "") or "").strip()
        for a in (getattr(spec, "args", []) or []):
            p = Path(str(a))
            if p.suffix.lower() != ".py" or not p.is_absolute() or not p.is_file():
                continue
            rc, err = _launch(exe, p, [])
            crash = _import_crash(err)
            out.append(_imp(
                f"mcp:{name}:import", "sub", False,
                LV_FAIL if crash else LV_OK,
                (f"{name}：服务端脚本起不来（缺库）" if crash
                 else f"{name}：服务端脚本可加载"),
                detail=crash or (f"{p.name} 起得来：喂了空输入后它以退出码 {rc} 退出，"
                                 "stderr 里没有缺库记录"),
                hint=("这个库要装在 **command 指定的那个解释器**里 —— 装到别处等于没装。"
                      if crash else ""),
                evidence={"script": str(p), "crash": crash, "rc": rc}))
    return out


def check_origin(ctx: _Ctx) -> list[CheckResult]:
    """Origin 能不能真的接上（Windows）。**用不了不阻断任何任务。**"""
    has_spec = any(getattr(s, "name", "") == "origin-figure"
                   and not getattr(s, "disabled", False)
                   for s in (getattr(ctx.cfg.mcp, "servers", []) or []))
    ok, ver = origin_com()
    if ok:
        return [_imp("origin.com", "sub", False, LV_OK,
                     "Origin：COM 通道通" + (f"（{ver}）" if ver else ""),
                     detail="Origin 出图这条路可用" + (
                         "" if has_spec else
                         "；但 mcp.servers 里没有 origin-figure 这一段 —— 内核现在不会去调它。"),
                     evidence={"com": True, "version": ver, "mcp_spec": has_spec})]
    return [_imp(
        "origin.com", "sub", False, LV_WARN if has_spec else LV_INFO,
        ("Origin：配了出图服务端，但 COM 连不上" if has_spec
         else "Origin：没接上（Windows 上没装 Origin 或没装 pywin32）"),
        detail=ver or "未知原因",
        hint=("这一条**不影响做 PPT**。要用 Origin 出图：装 Origin（需授权）、"
              "`pip install pywin32`，再在 mcp.servers 里配 origin-figure。"
              "不想用 Origin 也完全没问题 —— 内核自带的 python 绘图路线不依赖它。"),
        evidence={"com": False, "mcp_spec": has_spec})]


def check_office_render(ctx: _Ctx) -> list[CheckResult]:
    """要把稿子渲成看得见的页时才需要它：只产出 .pptx 本身不需要。

    ★ 只认**两种渲染器**：LibreOffice 的 soffice、或本机装着 PowerPoint。
      为什么不认"有 python-pptx 就能渲染"：它只读 XML，**渲不出画面** ——
      版式撞车、字被切、颜色糊成一团，它一个都看不见。
    """
    so = _which_soffice()
    if so:
        return [_imp("office.render", "rongke", False, LV_OK,
                     "稿件渲染：LibreOffice 就位", detail=so,
                     evidence={"engine": "libreoffice", "path": so})]
    if _has_powerpoint():
        return [_imp("office.render", "rongke", False, LV_OK,
                     "稿件渲染：本机有 PowerPoint（可渲成逐页 PNG）",
                     detail="deploy 前的 deck_preview.py 走这条",
                     evidence={"engine": "powerpoint", "path": ""})]
    return [_imp(
        "office.render", "rongke", False, LV_INFO,
        "稿件渲染：没装 LibreOffice，也没有 PowerPoint",
        detail="交一份 .pptx 不需要它；要把稿子渲成图片看效果才需要。",
        hint="想要「出稿前先看一眼」：装 LibreOffice（免费）。只是把 .pptx 交给别人打开的话，现在这样就够。",
        evidence={"engine": "", "path": ""})]


#: 「做 PPT」那条核心链 —— 缺任何一条都判 broken。
#: 画图 / Origin / 渲染**不在这张表里**：做 PPT 是主活，不画图不该让整个内核
#: 被报成"坏了"（那正是把「这一次」当「这一类」）。
CORE_KEYS: tuple[str, ...] = ("llm.channel", "deck.notes.python",
                              "deck.audit.present", "deck.template.present")

#: 展示分组的判据：key 前缀 → 组。
_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ppt", "做 PPT", ("deck.", "office.")),
    ("figure", "画图与出图", ("figure.", "mcp", "origin.")),
    # ← 2026-10-22 新增：写综述/开题那条链。在此之前这里**一条都不管文献**，
    #   用户报"查不到东西"时，首次配置页给不出任何信息。
    ("lit", "查文献与取全文", ("lit.",)),
    ("base", "基础", ("llm.",)),
)

CHECKS: tuple[Callable[[_Ctx], list[CheckResult]], ...] = (
    check_ai_channel,
    check_deck_notes,
    check_deck_audit,
    check_deck_template,
    check_figure_backend,
    check_figure_skills,
    check_literature_chain,
    check_mcp_servers,
    check_origin,
    check_office_render,
)


# ════════════════════════════════════════════════════════════════
# 总入口
# ════════════════════════════════════════════════════════════════

def diagnose_setup(cfg: Config | None = None, *,
                   root: Path | None = None) -> dict:
    """跑一遍首次使用自检。**只读，不写盘。**

    返回
    ----
    `verdict`  ready（核心链条齐了）/ degraded（能做 PPT，但画图那侧缺东西）/ broken
    `counts`   按级别计数
    `items`    逐条检查（每条带 `side` / `core`）
    `groups`   按「做什么」分组后的同一批条目（界面直接铺）
    `fixable`  能一键修的 key
    `summary_line` 一行话
    """
    root = Path(root or project_root())
    cfg = cfg or Config.load(root)
    ctx = _Ctx(cfg, root)
    items: list[CheckResult] = []
    for fn in CHECKS:
        try:
            items.extend(fn(ctx))
        except Exception as e:                                  # noqa: BLE001
            # 一项检查自己炸了，**不许**把整份报告变成空白 ——
            # 如实把"这一项没查成"印出来，其余照常（同 mcp_doctor 的口径）。
            items.append(_imp(
                f"crash:{getattr(fn, '__name__', '?')}", "rongke", False, LV_FAIL,
                f"这一项没查成：{getattr(fn, '__name__', '?')}",
                detail=f"{type(e).__name__}: {e}",
                hint="这是荣珂自己的检查逻辑出了问题，不是你的环境。"))

    counts = {lv: sum(1 for c in items if c.level == lv)
              for lv in (LV_FAIL, LV_WARN, LV_INFO, LV_OK)}
    core_bad = [c for c in items if c.core and c.level == LV_FAIL]
    other_bad = [c for c in items if (not c.core) and c.level == LV_FAIL]
    if core_bad:
        verdict = "broken"
    elif other_bad or counts[LV_WARN]:
        verdict = "degraded"
    else:
        verdict = "ready"

    if verdict == "ready":
        line = "做 PPT 要用的东西都齐了"
    elif verdict == "broken":
        line = (f"做 PPT 还差 {len(core_bad)} 项："
                + "；".join(c.title for c in core_bad[:3]))
    elif other_bad:
        line = f"做 PPT 能用；另有 {len(other_bad)} 项画图/出图能力缺东西"
    else:
        line = f"做 PPT 能用；{counts[LV_WARN]} 项有隐患"

    rows = [_as_row(c) for c in items]
    groups = []
    for gid, gname, prefixes in _GROUPS:
        picked = [r for r in rows if r["key"].startswith(prefixes)]
        if picked:
            groups.append({"id": gid, "name": gname, "items": picked})
    return {
        "version": DOCTOR_VERSION,
        "verdict": verdict,
        "counts": counts,
        "items": rows,
        "groups": groups,
        "fixable": [c.key for c in items if c.fixable and c.level != LV_OK],
        "summary_line": line,
        "core_keys": list(CORE_KEYS),
        "self_exe": ctx.self_exe,
    }


# ════════════════════════════════════════════════════════════════
# 修复：只写**能证明安全**的那几项
# ════════════════════════════════════════════════════════════════

#: 向导**唯一**能动的那几个配置键。与 `config.PROVIDER_WRITE_KEYS` 同一条纪律：
#: "什么能落到盘上"是配置层的契约，不是某个接口的契约 —— 所以白名单写死在这里。
SETUP_WRITE_KEYS: tuple[str, ...] = ("deck.python", "figure.python",
                                     "figure.skills_dir", "deck.skills_dir")


def save_config_overrides(updates: dict[str, Any], *,
                          root: Path | None = None) -> dict[str, Any]:
    """把白名单里的配置键写进项目根 `config.json`，其它字段一个都不丢。

    与 `config.save_provider_overrides` 是**同一条**形状、同样四条硬规矩：
    白名单外一律 `ValueError`（不做"聪明的静默忽略"）、写前备份、原子写、
    绝不写密钥。另单独一个函数是因为那边的白名单是 provider 的五个键，
    把 setup 的键塞进去会让"设置页能改什么"与"向导能改什么"合成一团。

    ★ 键是 `"a.b"` 点路径（要动的是 deck / figure 两个不同小节）。
    """
    if not updates:
        raise ValueError("没有要写入的字段")
    clean: dict[str, dict[str, Any]] = {}
    for k, v in updates.items():
        key = str(k or "").strip()
        if key not in SETUP_WRITE_KEYS:
            raise ValueError(f"不被接受的键：{k}（只允许 "
                             + " / ".join(SETUP_WRITE_KEYS) + "）")
        sec, bare = key.split(".", 1)
        clean.setdefault(sec, {})[bare] = v

    root = Path(root or project_root())
    target = root / "config.json"
    existing = _read_json(target)            # 读不动就抛错，绝不拿空字典往下写

    backup = ""
    if target.exists():
        stamp = (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        bak = target.with_name(target.name + ".bak_" + stamp)
        bak.write_bytes(target.read_bytes())
        backup = str(bak)

    merged = _deep_merge(existing, clean)
    text = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)                # 同目录 rename：要么旧的、要么新的
    return {"path": str(target), "backup": backup,
            "written": {f"{s}.{k}": v for s, d in clean.items() for k, v in d.items()}}


def _fix_one(key: str, cfg: Config, root: Path) -> tuple[bool, str, dict]:
    """按 key 决定怎么修。返回 `(修了没, 给人看的一句话, 证据)`。

    **只做"把配置指向一个已经能用的东西"这一类修复。** 装包、装 Origin、
    装技能都不是荣珂该替用户做的：它不知道你想装到哪个环境，也没有你的授权。
    所以这里**从不** `pip install`。
    """
    if key == "deck.notes.python":
        got = _pptx_elsewhere()
        if not got:
            return False, "本机没有别的解释器装着 python-pptx —— 得先装它（这一项不能自动修）", {}
        res = save_config_overrides({"deck.python": got}, root=root)
        return True, f"把 deck.python 指向 {got}（那里有 python-pptx）。", res
    if key == "figure.python":
        got = _figure_py_elsewhere(cfg.figure.python or "")
        if not got:
            return False, ("本机没有别的解释器同时装着 matplotlib + PyMuPDF —— "
                           "得先装它们（这一项不能自动修）"), {}
        res = save_config_overrides({"figure.python": got}, root=root)
        return True, f"把 figure.python 指向 {got}（两个库都在）。", res
    return False, f"{key} 没有自动修法 —— 这一项要人去装、去指路", {}


def _recheck(key: str, root: Path) -> CheckResult | None:
    """**用现读的配置**重跑产出这个 key 的那一项。

    ★ 不能拿进来时那个 `cfg` 重跑：它里面还是旧路径，重测必然报同样的失败 ——
      那是一条永远为假的判据（模块 docstring 第 2 条）。
    """
    fresh = Config.load(root)
    ctx = _Ctx(fresh, root)
    for fn in CHECKS:
        try:
            got = fn(ctx)
        except Exception:                                       # noqa: BLE001
            continue
        for g in got:
            if g.key == key:
                return g
    return None


def fix_setup(keys: list[str], *, cfg: Config | None = None,
              root: Path | None = None) -> dict:
    """按 key 逐个修，**每修一项就重测那一项**。"""
    root = Path(root or project_root())
    cfg = cfg or Config.load(root)
    out: list[dict] = []
    for k in keys:
        k = str(k)
        try:
            did, why, ev = _fix_one(k, cfg, root)
        except Exception as e:                                  # noqa: BLE001
            did, why, ev = False, f"没修成：{type(e).__name__}: {e}", {}
        row: dict[str, Any] = {"key": k, "fixed": did, "why": why, "evidence": ev}
        if did:
            _clear_cache()          # ← 不清缓存，重测读的还是旧结论
            after = _recheck(k, root)
            row["after_level"] = after.level if after else "unknown"
            row["after_title"] = after.title if after else ""
            row["verified"] = bool(after) and after.level in (LV_OK, LV_INFO)
        out.append(row)
    report = diagnose_setup(Config.load(root), root=root)
    return {"fixed": out,
            "fixed_count": sum(1 for r in out if r.get("verified")),
            "report": report}


# ════════════════════════════════════════════════════════════════
# 命令行报告
# ════════════════════════════════════════════════════════════════

_ICON = {LV_OK: "✔", LV_INFO: "·", LV_WARN: "!", LV_FAIL: "✘"}
_SIDE = {"rongke": "荣珂自己", "sub": "下游解释器"}


def format_report(rep: dict, *, verbose: bool = False) -> str:
    """给人看的报告。**每一项都写出它探的是哪一侧**（模块 docstring 第 1 条）。"""
    lines: list[str] = []
    lines.append(f"═══ 荣珂 · 首次使用自检（{rep.get('version', '?')}）═══")
    lines.append(rep["summary_line"])
    c = rep.get("counts") or {}
    lines.append(f"  通过 {c.get(LV_OK, 0)} · 提示 {c.get(LV_INFO, 0)} · "
                 f"降级 {c.get(LV_WARN, 0)} · 不通 {c.get(LV_FAIL, 0)}")
    for g in rep.get("groups") or []:
        lines.append("")
        lines.append(f"【{g['name']}】")
        for it in g["items"]:
            side = _SIDE.get(it.get("side", ""), it.get("side", ""))
            core = " · 核心" if it.get("core") else ""
            lines.append(f"  {_ICON.get(it['level'], '?')} {it['title']}"
                         f"   （{side}{core}）")
            show = verbose or it["level"] in (LV_FAIL, LV_WARN)
            if it.get("detail") and show:
                for ln in str(it["detail"]).splitlines():
                    lines.append(f"      {ln}")
            if it.get("hint") and (verbose or it["level"] in (LV_FAIL, LV_WARN, LV_INFO)):
                for ln in str(it["hint"]).splitlines():
                    lines.append(f"      → {ln}")
    fx = rep.get("fixable") or []
    if fx:
        lines.append("")
        lines.append("可以一键修（改配置指向一个已经能用的东西，不动别处）：" + "、".join(fx))
        lines.append("  python -m rongke --setup --fix")
    lines.append("")
    if rep.get("verdict") == "ready":
        lines.append("结论：ready —— 做 PPT 那条链是通的。")
    elif rep.get("verdict") == "broken":
        lines.append("结论：broken —— 上面标「核心」的项不通，做 PPT 会卡住。")
    else:
        lines.append("结论：degraded —— 做 PPT 能用；缺的是画图/渲染那条支线，不影响交稿。")
    return "\n".join(lines)
