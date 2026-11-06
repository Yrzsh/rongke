# -*- coding: utf-8 -*-
"""荣珂命令行入口。

用法：
    python -m rongke "核验这篇稿子的参考文献"
    python -m rongke --contract-only "帮我写引言"            # 只看契约草稿
    python -m rongke --contract-out c.json "帮我写引言"      # 导出契约，人审后改
    python -m rongke --contract-in c.json                    # 用冻结的契约开跑
    python -m rongke --max-steps 60 "..."                    # 放宽步数
    python -m rongke --runs                                  # 历史任务一览
    python -m rongke --show 20260918-180940                  # 回放某次任务
    python -m rongke --open-ws "..."                         # 跑完打开工作区目录
    python -m rongke --json "..."                            # 机器可读输出
    python -m rongke --tools                                 # 列出已装配的工具
    python -m rongke --web --port 8765                       # 启动 Web 控制台

本轮体验改动（都来自实际使用的摩擦）：
  * **强制 UTF-8 输出** —— Windows 重定向到文件时默认走 cp936，
    `> run.log` 会得到整片乱码（历史 run1/run2.log 就是这么坏的）
  * **契约可审可改** —— 原来是"只看"，现在能导出、改完再喂回去
  * **Ctrl+C 优雅取消** —— 原来是直接抛 KeyboardInterrupt 崩掉；
    现在第一次按是"请求停止"（走内核的 cancel），第二次才是强杀
  * **思考心跳** —— 网关会间歇性挂起 120s+，原来这段时间屏幕一片死寂
  * **历史可回放** —— 读 `runs/<id>/session.json`，跨进程可用
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from .config import Config
from .contract import TaskContract
from .llm import LLM, LLMError
# `__version__` 是版本号的**唯一真源**（`pyproject` 用 dynamic attr 指向它）。
# CLI 只把它接出来，绝不在这里另写一个字符串 —— 两份版本号必然漂。
from . import __version__
from .loop import Rongke, LicenseError

_C = {"dim": "\033[2m", "b": "\033[1m", "r": "\033[0m", "g": "\033[32m",
      "y": "\033[33m", "red": "\033[31m", "c": "\033[36m"}


def force_utf8() -> None:
    """把 stdout/stderr 钉成 UTF-8。

    Windows 上重定向到文件时，Python 默认用 locale 编码（cp936），
    而荣珂的总结行里有 `═ ✔ ✘ ·` 这些字符 —— 轻则乱码，重则 UnicodeEncodeError
    把整段总结吞掉。errors="replace" 是刻意的：宁可个别字变问号，
    也不能让一次跑了五分钟的任务在最后一行崩掉。

    ⚠️ 注意 PowerShell `>` 会把控制台文本**再**转成 UTF-16，
    所以排查日志乱码时要分清是哪一层的锅（历史 run1/run2.log 是双层乱码）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass          # 被替换成非常规流（IDE/管道包装）时不强求


def make_reporter(color: bool = True, show_content: bool = False,
                  clock: bool = True):
    """`clock=False` 用于回放：回放是瞬时重放，那个"已用几秒"是假的，不如不显示。"""
    R = _C if color else {k: "" for k in _C}
    t0 = time.time()

    def el() -> str:
        return f"（{time.time() - t0:.0f}s）" if clock else ""

    def rep(kind: str, d: dict) -> None:
        if kind == "workspace":
            print(f"{R['dim']}· 工作区：{d.get('path')}{R['r']}")
        elif kind == "memory_recall":
            print(f"  {R['dim']}· 已注入历史参考（{d.get('chars')} 字符，仅供提示不作证据）{R['r']}")
        elif kind == "memory_saved":
            print(f"  {R['dim']}· 已存入记忆（累计 {d.get('runs')} 次任务）{R['r']}")
        elif kind == "contract_drafting":
            print(f"{R['dim']}· 起草目标契约…{R['r']}")
        elif kind == "contract":
            print(f"\n{R['b']}═══ 目标契约 ═══{R['r']}")
            print(f"  {R['b']}目标{R['r']}：{d.get('goal')}")
            for key, label in (("deliverables", "交付"), ("acceptance", "验收"),
                               ("constraints", "约束"), ("forbidden", "禁止")):
                for i, x in enumerate(d.get(key) or [], 1):
                    print(f"  {label}{i}. {x}")
            if d.get("target_chars"):
                print(f"  {R['dim']}目标字数：约 {d['target_chars']} 字{R['r']}")
            if d.get("stages"):
                print(f"  {R['dim']}阶段技能：{', '.join(d['stages'])}{R['r']}")
            print()
        elif kind == "llm_call":
            print(f"  {R['dim']}· 第 {d.get('n')} 步：模型思考中…{el()}{R['r']}")
        elif kind == "step":
            u = d.get("usage") or {}
            print(f"{R['c']}[step {d['n']}]{R['r']} {R['dim']}"
                  f"in={u.get('input_tokens')} out={u.get('output_tokens')} "
                  f"reason={u.get('reasoning_tokens')}{' · ' + el() if clock else ''}{R['r']}")
            if show_content and d.get("content"):
                print(f"  {R['dim']}{d['content'][:400]}{R['r']}")
        elif kind == "tool_start":
            print(f"  {R['dim']}· seq={d.get('seq')} {d.get('name')} 调用中…{R['r']}")
        elif kind == "tool":
            ok = bool(d.get("ok"))
            col = R["g"] if ok else R["red"]
            print(f"  {col}→ seq={d.get('seq')} {d.get('name')} "
                  f"{'ok' if ok else 'ERR'}{R['r']} {R['dim']}{d.get('ms')}ms{R['r']}")
            if d.get("error"):
                print(f"    {R['red']}{str(d['error'])[:220]}{R['r']}")
        elif kind == "deliver_attempt":
            if d.get("passed"):
                print(f"\n{R['g']}{R['b']}✔ 证据门通过{R['r']}")
                for e in d.get("evidence") or []:
                    print(f"  {R['dim']}证据 seq={e.get('call_seq')} {e.get('tool')} "
                          f"→ {str(e.get('shows'))[:80]}{R['r']}")
                lint = d.get("lint")
                if lint:
                    print(f"  {R['g']}写作体检：{_lint_brief(lint)}{R['r']}")
            else:
                print(f"\n{R['y']}✘ 证据门未通过，已打回补正：{R['r']}")
                for i, r in enumerate(d.get("reasons") or [], 1):
                    print(f"  {i}. {str(r)[:400]}")
                if d.get("writing"):
                    print(f"\n{R['y']}{d['writing']}{R['r']}")
        elif kind == "stall":
            print(f"  {R['y']}⚠ 收敛告警：seq={d.get('seq')} {d.get('tool')} 原地打转{R['r']}")
        elif kind == "nudge":
            print(f"  {R['dim']}· 空转提醒已注入{R['r']}")
        elif kind == "remind":
            print(f"  {R['dim']}· 目标重申已注入（防漂移）{R['r']}")
        elif kind == "cancel_requested":
            print(f"  {R['y']}· 已请求停止 —— 当前这次调用结束后退出{R['r']}")
        elif kind == "cancelled":
            print(f"  {R['y']}· 任务在第 {d.get('step')} 步取消（未完成）{R['r']}")
        elif kind == "blocker":
            print(f"\n{R['y']}■ 模型报告阻塞：{d.get('reason')}{R['r']}")
            for t in d.get("tried") or []:
                print(f"  {R['dim']}已试：{t}{R['r']}")
            if d.get("need"):
                print(f"  {R['dim']}需要：{d['need']}{R['r']}")

    return rep


def _lint_brief(lint: dict) -> str:
    m = lint.get("metrics") or {}
    s = (f"{m.get('chars', 0)} 字 · {m.get('paragraphs', 0)} 段 · "
         f"{m.get('sentences', 0)} 句 · 均句 {m.get('avg_sentence', 0)} 字 · "
         f"{lint.get('score', 0)}/100")
    if lint.get("hard"):
        s += f" · 必修 {lint['hard']}"
    if lint.get("soft"):
        s += f" · 建议 {lint['soft']}"
    return s


# ══════════════════ 历史与回放 ══════════════════
def _archives(ws_dir: Path) -> list[dict]:
    """读 runs/*/session.json。跨进程可用（这就是归档存在的理由）。"""
    out: list[dict] = []
    if not ws_dir.exists():
        return out
    for d in sorted(ws_dir.iterdir(), reverse=True):
        f = d / "session.json"
        if not d.is_dir() or not f.exists():
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        r = j.get("result") or {}
        out.append({
            "id": j.get("id") or d.name,
            "goal": j.get("goal") or "",
            "success": bool(r.get("success")),
            "steps": r.get("steps"),
            "reason": (r.get("reason") or "")[:80],
            "elapsed": j.get("elapsed") or 0,
            "events": len(j.get("events") or []),
            "path": str(f),
        })
    return out


def cmd_runs(cfg: Config) -> int:
    rows = _archives(cfg.runtime.workspace_dir)
    if not rows:
        print("还没有归档的任务。跑一次就有了（归档落在 runs/<时间戳>/session.json）。")
        return 0
    print(f"共 {len(rows)} 次任务（新 → 旧）\n")
    for r in rows:
        mark = "✔" if r["success"] else "✘"
        print(f"  {mark} {r['id']}  {str(r['steps'] or '-'):>3} 步 "
              f"{float(r['elapsed'] or 0):>5.0f}s  {r['goal'][:56]}")
    print("\n回放某一次：  python -m rongke --show <id>")
    return 0


def cmd_lessons(cfg: Config, goal: str = "", note: str = "") -> int:
    """看/写"经验层"，并**把注入内容原样摊开**。

    存在的理由：记忆注入是**看不见的** —— 它对模型的每一次决策都有影响，
    却在任何界面上都不出现。看不见的东西无法被信任，也无法被调试。
    这里做三件事：
      ① 分别列出**统计**（从台账算的）与**人工**（`notes`）两条来源 —— 溯源；
      ② 原样打印某目标会注入的整段文本，含**字符数**（它是每步重发的成本）；
      ③ `--note` 写入一条人工经验（这是 `Memory.note()` 的唯一入口 ——
         在此之前它是个没有任何调用方的死方法）。
    """
    from .memory import Memory, default_root
    m = Memory(default_root(cfg))

    if note:
        m.note(note)
        print(f"已写入人工经验：{note.strip()}")

    auto = m.distill_stats()
    manual = m.lessons(50)

    print("经验层")
    print(f"  统计（从台账算，可复算）   {len(auto)} 条")
    for s in auto:
        print(f"    · {s}")
    print(f"  人工（手写，不可复算）     {len(manual)} 条")
    for s in manual:
        print(f"    · {s[2:].strip() if s.startswith('- ') else s.strip()}")
    if not auto and not manual:
        print("    （空）—— 统计条数需要台账里至少 "
              f"{Memory._MIN_SAMPLES} 次任务记录；人工条目用 --note 写。")

    if goal:
        text = m.inject_text(goal)
        print()
        print(f"若目标是「{goal}」会注入的内容（{len(text)} 字符 —— 每步重发）：")
        print("┌" + "─" * 66)
        for line in (text or "（无 —— 没有够相似的历史，也没有经验）").splitlines():
            print("│ " + line)
        print("└" + "─" * 66)
    else:
        print()
        print("看某个目标实际会注入什么：  python -m rongke --lessons --goal \"<目标>\"")
    return 0


def cmd_presets(cfg: Config, *, live: bool = False) -> int:
    """列出能力预设与**挂载健康度**（不是「文件在不在」）。

    判健康只认 `MountReport.broken`：resolve 不到、缺必填 config、层栈冲突、
    个人层写进非法值，全都在这里报红并带上理由；坏件照样列出来，绝不静默滤掉。
    退出码 0 = 全好 / 1 = 有坏件，可直接挂进脚本做前置检查。
    """
    from .caps import all_presets_health, schema_discipline

    h = all_presets_health()
    print(f"能力预设台账 —— {len(h['good'])} 份 / 文件级坏件 {len(h['broken'])} 条"
          f" / healthy={h['healthy']}")
    print()
    for it in h["good"]:
        mark = "✔" if it.get("healthy") else "✘"
        print(f"  {mark} {it['id']}  scenes={it.get('scenes')}  "
              f"order={it.get('order')}  {it.get('summary', '')}")
        for e in it.get("broken") or []:
            print(f"      ✗ {e}")
        if live:
            print(f"      rows={it.get('rows')}（--live 逐份真挂载）")
    for b in h["broken"]:
        print(f"  ✘ {b['id']}：{b['why']}")
    bad = schema_discipline()
    if bad:
        print(f"\n⚠ 元判据失守（required 项不许有 default）：{bad}")
    print()
    if h["healthy"] and not bad:
        print("✔ 全部预设挂载成功。")
        return 0
    print("✘ 有预设没挂上 —— 它们声明的能力**不存在**，别以为装上了。")
    return 1


def cmd_plugins(cfg: Config, *, show_dirs: bool = False) -> int:
    """列出插件台账（含**加载失败的**），或只打印搜索路径。

    为什么必须要有一条命令能把失败摊开：插件是第三方代码，加载失败是常态。
    如果只在启动日志里打一行，用户会**以为装上了**——那是最坏的一种状态，
    因为它伪造了一个不存在的能力。所以 `--plugins` 把三件事都摆出来：
    从哪找的、哪些成了、哪些没成以及为什么。

    退出码：0 = 全都好（或一个插件都没有）；1 = 有失败项。
    这样能直接挂进脚本做前置检查，和 `--mcp-doctor-quiet` 同一个约定。
    """
    from .plugins import discover, load_plugins, plugin_dirs

    dirs = plugin_dirs(cfg)
    if show_dirs:
        print("插件搜索路径（按优先级）")
        for i, d in enumerate(dirs, 1):
            n = len(discover([d]))
            mark = f"{n} 个候选" if d.is_dir() else "目录不存在"
            print(f"  {i}. {d}   —— {mark}")
        home = Path.home() / ".rongke" / "plugins"
        print(f"\n放一个 .py 进去即可生效：{home}")
        print("完整写法见 examples/plugins/term_table.py 与 docs/插件开发.md")
        return 0

    # dry-run：不传 registry，只做校验（不真注册，避免和 rk.close() 的时序打架）
    rep = load_plugins(None, cfg=cfg, apply_chart_kinds=False)

    print(f"插件台账 —— {rep.summary()}")
    if not rep.results:
        print("\n  没找到插件。")
        print(f"  把 .py 文件放进：{Path.home() / '.rongke' / 'plugins'}")
        print("  用 --plugins-dirs 看全部搜索路径。")
        return 0

    print()
    for r in rep.results:
        print(r.line())
        if not r.ok and r.description:
            # 回溯只在这里给（开发者要看），不进 `line()` 的人读部分
            first = (r.description or "").strip().splitlines()
            if first:
                print(f"   回溯首行：{first[-1][:150]}")
        print()

    if rep.failed:
        print(f"⚠ {len(rep.failed)} 个插件没生效 —— 它们声明的工具/图型**不存在**，"
              f"别以为装上了。")
        return 1
    print("✔ 全部插件已生效。")
    return 0


_UNSET = object()


def cmd_license(cfg: Config, *, action: str = "show", code: str = "",
                days: int = 0, plan: str = "pro",
                customer: str = "", sk: str = "", mid: str = "") -> int:
    """离线授权：看状态、激活、注销；以及发行方用的 keygen / sign。

    ★ 买家只需要 `show` / `activate` / `deactivate`。
      `keygen` 与 `sign` 是**发行方**工具 —— 用它们的机器上才有私钥。
      私钥一旦泄露，任何人都能签出有效凭证，所以别提交进仓库。
    """
    from . import license as L

    action = str(action or "show").strip().lower()

    if action == "keygen":
        pri, pub = L.keygen()
        print("已生成一对密钥。")
        print(f"  公钥（填进 rongke/license.py 的 EMBEDDED_PUBKEY）：\n    {pub}")
        print(f"  私钥（**留在你自己手里，别提交**）：\n    {pri}")
        # ★ 直接给等号形式：base64url 可能以 `-` 开头，写成
        #   `--sk <私钥>` 时 argparse 会把它当成另一个选项而报
        #   `expected one argument`。这是个会真的打到人的坑。
        print("\n签码时用等号形式（私钥可能以 - 开头），并且必须带客户机器码：")
        print(f"  rongke --license sign --sk={pri} --mid=RK-M-XXXXXXXXXXXXXX "
              f"--customer \"某大学\" --days 365")
        print("\n填公钥之前，任何激活码都验不过 —— 这是有意的。")
        return 0

    if action == "sign":
        if not sk:
            print("签激活码要私钥（用等号形式，私钥可能以 - 开头）：")
            print("  rongke --license sign --sk=<私钥> --mid=RK-M-... "
                  "[--customer 名字] [--lic-plan pro] [--days 365]")
            return 2
        if not mid:
            print("一码一机：签码必须带客户的机器码（授权页「本机机器码」那一行）。")
            print("  rongke --license sign --sk=<私钥> --mid=RK-M-... "
                  "--customer \"某大学\" --days 365")
            return 2
        mid_n = L.normalize_mid(mid)
        if not L.valid_mid(mid_n):
            print(f"✘ 机器码形状不对：{mid!r}（应为 RK-M- 加 16 位 Base32）")
            return 2
        try:
            sk_bytes = L._b64d(sk)
        except Exception as e:                            # noqa: BLE001
            print(f"✘ 私钥不是合法 base64：{e}")
            return 2
        expires = 0.0 if days <= 0 else time.time() + days * 86400
        payload = {"customer": customer or "未署名", "plan": plan,
                   "expires": expires, "issued": time.time(), "mid": mid_n}
        code = L.make_code(payload, sk_bytes)
        print(code)
        print(f"\n  客户：{payload['customer']}    套餐：{plan}    "
              f"到期：{'永久' if not expires else L._ymd(expires)}")
        print(f"  机器码：{mid_n}（只在这台机器有效）")
        print("  ↑ 把上面那行激活码发给他，让他在「试用与激活」页粘贴。")
        return 0

    if action in ("machine-id", "machineid", "mid"):
        print(L.machine_id())
        return 0

    lic = L.License()

    if action == "activate":
        if not code:
            print("用法：rongke --license activate RK1.xxxx.yyyy")
            return 2
        try:
            lic.save_code(code)
        except ValueError as e:
            print(f"✘ {e}")
            return 1
        st = lic.state()
        if not st.ok:
            print(f"✘ 激活码已保存，但本机不可用：{st.reason}")
            return 1
        print(f"✔ 已激活：{st.reason}")
        if st.customer:
            print(f"  客户：{st.customer}    套餐：{st.plan}")
        return 0

    if action == "deactivate":
        lic.clear_code()
        st = lic.state()
        print(f"已注销激活码。当前状态：{st.reason}")
        return 0

    # show
    st = lic.state()
    print(f"授权状态：{st.reason}")
    print(f"  内部状态    {st.state}")
    print(f"  能否发起新任务  {'能' if st.ok else '不能'}")
    print(f"  本机机器码  {st.machine_id or L.machine_id()}")
    if st.customer:
        print(f"  客户       {st.customer}")
    if st.plan:
        print(f"  套餐       {st.plan}")
    if st.expires:
        print(f"  到期       {L._ymd(st.expires)}")
    if not L.EMBEDDED_PUBKEY:
        print("\n  ⚠ 这份代码里没有内嵌公钥 ⇒ 验不了激活码。")
        print("    发行方要先用 `rongke --license keygen` 生成密钥对，")
        print("    把公钥填进 rongke/license.py 的 EMBEDDED_PUBKEY。")
    print(f"\n  激活码存在：{lic._code_path}")
    print("  试用期从首次运行算起，共 "
          f"{L.TRIAL_DAYS} 天。到期后**只拦发起新任务** —— "
          "历史与已归档的结果照常能看。")
    return 0


def cmd_billing(cfg: Config, *, action: str = "show",
                plan=_UNSET, amount: str = "") -> int:
    """本地账单：看余额/套餐/用量，或换套餐、记一笔充值占位。

    ★ 这个命令**不会收钱**。`topup` 只往本地台账写一条记录 ——
      真实收款需要接支付网关，而那件事没有做过（见 `billing.py`）。
      命令输出里每次都会把这句话打出来，因为在命令行里
      "充值 10 美元"和"真付了 10 美元"长得太像了。
    """
    from . import billing as B
    from .pricing import format_cost

    b = B.Billing()
    action = str(action or "show").strip().lower()

    if action == "plan":
        if plan is _UNSET or not plan:
            print("换套餐：--billing plan <free|pro|lab|enterprise>")
            print("可用：" + " / ".join(B.PLAN_ORDER))
            return 2
        try:
            a = b.change_plan(plan)
        except ValueError as e:
            print(f"✘ {e}")
            return 2
        print(f"✔ 已切到「{B.PLANS[a.plan]['name']}」。"
              f"额度已按本地记账授予 —— **未实际收费**。")
        return 0

    if action == "password":
        # ★ 用 sentinel 区分「没给参数」与「给了空串」：
        #   `--billing password ""` 是**取消密码**这个合法意图，
        #   而用 `if not plan` 判断会把它当成"没给参数" —— 实测踩到，
        #   取消密码的命令看起来成功、实际什么都没做。
        if plan == _UNSET:
            st = b.login_state()
            print(f"登录状态：{st['note']}")
            print("设密码：--billing password <密码>")
            print("取消密码：--billing password \"\"")
            return 0
        b.set_password(plan)
        if plan:
            print("✔ 已设本地密码。下次打开控制台需要它。")
            print("   ⚠️ 这是**单机**密码，只挡路过的人；")
            print("      能读你的数据目录的人照样能改台账。")
        else:
            print("✔ 已取消密码，回到本地免登录。")
        return 0

    if action == "topup":
        if not amount:
            print("充值占位：--billing topup <美元数>")
            print("⚠️ 这**不会真的收钱**，只写一条本地流水。")
            return 2
        try:
            b.top_up(float(amount))
        except ValueError as e:
            print(f"✘ {e}")
            return 2
        print(f"✔ 本地台账已加 {amount} 美元额度 —— **未接支付网关，未实际收款**。")
        return 0

    # ── show ──
    r = b.report()
    a, bal, plan_m = r["account"], r["balance"], r["plan"]

    print("本地账单")
    print(f"  账户          {a['id']}" + (f"（{a['email']}）" if a["email"] else ""))
    # ★ `price_usd` 有三种语义，不能用真值判断：
    #   0.0 = 免费（truthy 为假！），None = 面议，>0 = 正常标价。
    #   写成 `if plan_m['price_usd']` 会把免费套餐显示成"面议" —— 实测踩到。
    price = plan_m["price_usd"]
    if price is None:
        price_s = "（面议）"
    elif float(price) == 0:
        price_s = ""          # 套餐名本身就叫「免费」，再印一遍就是「免费 免费」
    else:
        price_s = f"  ${float(price):.0f}/月"
    print(f"  套餐          {plan_m['name']}{price_s}")
    q = plan_m["quota_usd"]
    print(f"  套餐额度      {'不限' if q is None else format_cost(q)}")
    print(f"  累计授予      {format_cost(bal['granted'])}")
    print(f"  累计消耗      {format_cost(bal['spent'])}")
    rem = bal["remaining"]
    print(f"  余额          {format_cost(rem)}"
          + ("" if bal["exact"] else "  ← 只是上限，不是真值"))
    print(f"  流水条数      {bal['entries']}")

    if bal["bad_lines"]:
        print(f"\n  ⚠️ 流水里有 {bal['bad_lines']} 行读不出来（账本被破坏过）。"
              f"\n     余额只按能读的行算 —— 真实消耗可能更高。")
    if bal["unknown_charges"]:
        print(f"\n  ⚠️ 有 {bal['unknown_charges']} 笔消耗算不出金额（价格表没填全）。"
              f"\n     余额因此显示为 UNKNOWN —— 这不是零，是「不知道」。"
              f"\n     填价格表：rongke --pricing")
    if bal["exact"] and rem != "UNKNOWN" and float(rem) < 0:
        print("\n  ⚠️ 余额已为负。换套餐或充值占位后继续。")

    print("\n  " + "\n  ".join(r["assumptions"]))

    print("\n套餐对照")
    for row in B.plan_diff(a["plan"]):
        mark = "→" if row["current"] else " "
        # 同样：0.0 是"免费"，None 才是"面议" —— 不能用真值判断
        if row["price_usd"] is None:
            price = "面议"
        elif float(row["price_usd"]) == 0:
            price = "免费"
        else:
            price = f"${float(row['price_usd']):.0f}/月"
        quota = "不限" if row["quota_usd"] is None else f"额 ${row['quota_usd']:.0f}"
        seats = "" if row["seats"] is None else f" · {row['seats']} 席"
        print(f"  {mark} {row['name']:<6} {price:>10}  {quota}{seats}")
    print("\n  换套餐：rongke --billing plan pro")
    return 0


def cmd_magpie(cfg: Config, *, models: list[str], host: str = "") -> int:
    """把「模型从哪来」接到 magpie 本地网关上。

    两种用法，都只做**能验证的事**：
      `--magpie`                 体检：网关在不在听。不改配置。
      `--magpie <模型名>`        切过去：写配置 + 打印下一步。

    ★ 刻意**不**在这里替用户设环境变量，也不把密钥写进 config.json ——
      前者是 shell 状态（写进子进程就没了），后者违反本仓
      「只存变量名、不存值」的规矩（见 config.py 的 provider 段注释）。
    """
    from . import magpie as M

    ep = host or f"http://127.0.0.1:{M.DEFAULT_PORT}"

    print("magpie 通道（github.com/yetone/magpie）")
    print(f"  网关地址        {ep}")
    print(f"  当前配置        {cfg.provider.name} / {cfg.provider.model}")
    print()

    alive, msg = M.probe(ep)
    print(f"  {msg}")
    if not alive:
        print()
        print("  网关没在听，先把它起起来：")
        print("    · 桌面版：装好 magpie 后打开它（菜单栏出现图标即可）")
        print(f"    · Docker：docker run -d --name magpie "
              f"-p 127.0.0.1:{M.DEFAULT_PORT}:{M.DEFAULT_PORT} "
              f"-v magpie-config:/config ghcr.io/yetone/magpie")
        print("    · 网关在别的机器上：加 --magpie-host http://<那台机器>:"
              f"{M.DEFAULT_PORT}")
        return 1

    if not models:
        print()
        print("  只有体检，没改配置。要切过去，在命令里带上模型名：")
        print("    rongke --magpie deepseek/deepseek-v4-pro")
        print("  模型名照抄 magpie 里的写法（面板里能看到，或跑 `magpie quota`）。")
        return 0

    model = str(models[0]).strip()
    extra = models[1:]
    if extra:
        print(f"  ✘ --magpie 只接一个模型名，多给了：{extra}")
        return 2

    try:
        tgt = M.target(model, ep)
    except M.MagpieError as e:
        print(f"  ✘ {e}")
        return 2

    ov = tgt.overrides()
    try:
        from .config import save_provider_overrides
        save_provider_overrides({k: v for k, v in ov.items()})
    except Exception as e:                                    # noqa: BLE001
        print(f"  ✘ 写配置失败：{type(e).__name__}: {e}")
        return 1

    print()
    print(f"  ✔ 已切到 magpie：{tgt.base_url()}")
    print(f"    模型 {model}")
    print()
    print("  还差一步 —— 让密钥变量非空（magpie 网关接受任意值）：")
    print(f"    PowerShell   $env:{M.DEFAULT_KEY_ENV}=\"{M.PLACEHOLDER_KEY}\"")
    print(f"    bash/zsh     export {M.DEFAULT_KEY_ENV}={M.PLACEHOLDER_KEY}")
    print()
    print("  然后验证：")
    print("    rongke --setup          # 看 provider 诊断是不是 OK")
    print("    rongke \"写下我的课题下一步\"   # 真跑一次")
    return 0


def cmd_pricing(cfg: Config) -> int:
    """看计价状态，并打出一份**可直接粘贴**的价格表骨架。

    存在的理由和 `--mcp` 一样：`UNKNOWN` 本身是个哑谜 ——
    用户分不清"这次真没花钱"和"内核不知道怎么算钱"。
    所以这里把三件事都摊开：当前填了什么、库里跑过哪些模型、还差什么。
    """
    from . import pricing as _P
    pr = getattr(cfg, "pricing", {}) or {}
    models = sorted((pr.get("models") or {}).keys())

    print("计价状态")
    print(f"  当前模型        {cfg.provider.model}")
    print(f"  已填单价        {len(models)} 个" + (f"：{', '.join(models)}" if models else "（空）"))
    print(f"  兜底单价        {'已设' if pr.get('default') else '未设'}")
    print(f"  cache_read      {'已含在 input 里（OpenAI 兼容口径，计费时减掉）' if pr.get('cache_read_included_in_input', True) else '与 input 并列（Anthropic 口径，不减）'}")

    covered = bool(_P._rates(pr, cfg.provider.model))
    if covered:
        print("\n  ✔ 当前模型可计价。")
    else:
        print("\n  ⚠️ **当前模型的单价没填 ⇒ 所有成本会显示 UNKNOWN。**")
        print("     这是刻意的：UNKNOWN 是「我不知道」，0 是「这次不要钱」——")
        print("     把前者渲染成后者，会让人据此做决策。")

    # 库里实际跑过的模型（含**网关响应侧**报的 model —— 它和请求侧不一致时
    # 说明网关换了后端或静默升级了模型，那种漂移值得知道）
    seen: dict[str, int] = {}
    st: dict = {}
    runs_dir = cfg.runtime.workspace_dir
    try:
        from .memory import Memory, default_root
        m = Memory(default_root(cfg))
        # ⚠️ `runs_dir` 必须传：不传就只有台账口径，而台账的用量字段是后加的、
        #    **会漏记 33.5%**（详见 `memory.archived_usage()` 的 docstring）。
        st = m.stats(runs_dir=runs_dir)
        if runs_dir.exists():
            for sess in sorted(runs_dir.glob("*/session.json"))[-50:]:
                # ⚠️ 跳过 `_` 前缀的建材目录。**这是防御，不是修一个已看到的错** ——
                #    实测（2026-09-19）跳与不跳**都是 ×66**：`_ui_resume` 是真运行
                #    `20260918-203437` 的逐字节副本，但那份运行早于 `llm_done` 事件，
                #    里面一条都没有 ⇒ 对计数零贡献。
                #    仍然要跳：万一将来有夹具拷的是**新**运行（带 `llm_done`），
                #    就会给那个模型灌水；而且"建材一律不数"在这段函数里该是统一规矩。
                #    （我当时先写了"实测确实在灌"，量完才发现没有 —— 教训：**"实测"两个字
                #     不许凭印象写**，写之前得真有那条命令的回显。）
                if sess.parent.name.startswith("_"):
                    continue
                try:
                    payload = json.loads(sess.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for ev in (payload.get("events") or []):
                    if ev.get("kind") != "llm_done":
                        continue
                    rm = str((ev.get("data") or {}).get("response_model") or "").strip()
                    if rm:
                        seen[rm] = seen.get(rm, 0) + 1
    except Exception as e:
        print(f"\n  （扫描历史失败，不影响下面：{type(e).__name__}: {e}）")

    if seen:
        print("\n  历史里实际见过的**响应侧**模型（按出现次数）：")
        for rm, n in sorted(seen.items(), key=lambda x: -x[1]):
            drift = "  ← 与请求侧不同，网关换过后端？" if rm != cfg.provider.model else ""
            print(f"    {rm}  ×{n}{drift}")

    # ── 累计用量：**以归档为准** ────────────────────────────────
    # 这一段原先读的是 `m.stats()` 的顶层 `input_tokens`（= **台账**口径），
    # 而台账的用量字段是后加的 ⇒ 少报 33.5%，漏掉的全是两个 14 步的大任务。
    # 2026-09-19 改：读 `stats(runs_dir=…)["archived"]`，由 `memory.archived_usage()`
    # 统一算（CLI 与 Web 共用那一份实现）。
    arch = st.get("archived") or {}
    led = st.get("ledger") or {}
    if arch.get("input_tokens") or arch.get("output_tokens"):
        print(f"\n  累计用量（**已归档的 {arch['runs']} 场**）"
              f"    {arch['input_tokens']} 输入 / {arch['output_tokens']} 输出 tokens")
        if arch.get("cache_read"):
            print(f"    其中缓存命中读    {arch['cache_read']} tokens"
                  f"（占输入 {arch['cache_ratio']:.1%}）")
        print("    （这些是待计价的部分）")
        # 与台账的差要**说出来**，不能悄悄换个数就完事 ——
        # 两个数不一致本身就是信息：它说明台账那份记录不完整。
        led_in = int(led.get("input_tokens") or 0)
        if led_in and led_in != arch["input_tokens"]:
            print(f"    ⚠️ 台账只记到 {led_in} 输入（{led_in / arch['input_tokens']:.0%}）—— "
                  f"台账的用量字段是后加的、**会少报**，别拿它当累计。")
        if arch.get("without_usage"):
            print(f"    ⚠️ 另有 {len(arch['without_usage'])} 场归档**没有用量字段**："
                  f"{'、'.join(arch['without_usage'])}")
        if arch.get("no_archive"):
            print(f"    ⚠️ 还有 {len(arch['no_archive'])} 场**早于归档/用量记录机制**"
                  f"（{arch['no_archive'][0]} 等），其用量**不可知**"
                  f" ⇒ 上面这个数是**下界**，不是总额。")
        if arch.get("cost_usd") == _P.UNKNOWN:
            print(f"    成本            UNKNOWN（{arch['uncosted']} 场归档落盘时单价表还没填）"
                  " —— **这不是 0，是「我不知道」。**")
    elif st:
        print("\n  累计用量        还没有带用量的归档。")

    skeleton = _P.template(sorted(set(list(seen) + [cfg.provider.model])))
    print("\n可以粘贴进 config.json 的 pricing 段（把 0.0 换成你的网关实际单价，"
          "单位是**每 100 万 token 的美元数**）：")
    print(json.dumps({"pricing": skeleton}, ensure_ascii=False, indent=2))
    print("\n填好之后，会话页与历史页的成本就会从 UNKNOWN 变成真数字。")
    return 0


def cmd_mcp(cfg: Config) -> int:
    """看 MCP 客户端的真实状态。

    存在的理由：MCP 的失败大多是"静默的"——配置写错、命令找不到、
    服务端连上却报载荷级失败。没有这个视图，用户只能对着
    "明明配了怎么没有"发懵。
    """
    print(f"MCP enabled = {cfg.mcp.enabled}")
    if cfg.mcp.sources:
        print("读过的服务端配置：")
        for s in cfg.mcp.sources:
            print(f"  · {s}")
    if cfg.mcp.rejected:
        print("\n⚠️ 配置层就不可用的（这些**没有**被静默丢掉）：")
        for r in cfg.mcp.rejected:
            who = r.get("server") or "(整个文件)"
            print(f"  · {who}  ←  {r.get('reason')}")
    if not cfg.mcp.enabled:
        print("\n（mcp.enabled=false，未装配任何服务端）")
        return 0
    if not cfg.mcp.servers:
        print("\n没有可用的 MCP 服务端。在 config.json 的 mcp.servers 里加，"
              "或用 mcp.import 指向别的客户端配置。")
        return 0

    # 真连一次，看到的才是真的（而不是把配置念一遍）
    rk = Rongke(cfg)
    print("\n" + rk.describe())
    d = rk.mcp_diag()
    for spec in d["specs"]:
        print(f"\n● {spec['name']}   [{spec['origin']}]")
        print(f"    命令     {spec['command']} {' '.join(spec['args'])}")
        print(f"    cwd      {spec['cwd']}（workspace = 本次任务工作区）")
        print(f"    超时     {spec['timeout']}s    domain={spec['domain']}")
        if spec["readonly"]:
            print(f"    只读     {spec['readonly']}")
        if spec["mutating"]:
            print(f"    会改世界 {spec['mutating']}")
    print("\n装进内核的 MCP 工具：")
    got = False
    for n in rk.tools:
        t = rk.registry.get(n)
        if t.source != "mcp":
            continue
        got = True
        print(f"  [{'W' if t.mutating else ' '}] {n:<22} {t.domain}")
    if not got:
        print("  （一个都没有）")
    if d["errors"]:
        print("\n⚠️ 连不上 / 列不出工具的服务端：")
        for name, err in d["errors"].items():
            print(f"  ● {name}\n{_indent(err, '      ')}")
    if d["skipped"]:
        print("\n没装的（每条都有原因）：")
        for s in d["skipped"]:
            who = s.get("tool") or s.get("server") or "?"
            print(f"  · [{s.get('level')}] {who}  ←  {s.get('reason')}")
    rk.close()
    return 0


def cmd_mcp_doctor(cfg: Config, *, quiet: bool = False) -> int:
    """MCP 开工前自检。

    与 `--mcp` 的分工：
      · `--mcp`      看**现状**（服务端规格 + 已装工具 + 连不上的原因）
      · `--mcp-doctor` 看**能不能开工**（key / 命令 / 依赖 / 重名 / 真装一遍，
                       每条给结论与修法）

    退出码是给脚本用的：0 = 可用（ready），1 = 有阻塞或不降级项。
    """
    from .mcp_doctor import diagnose_mcp, format_report

    rep = diagnose_mcp(cfg, install=True)
    if quiet:
        print(rep["summary_line"])
    else:
        print(format_report(rep))
    return 0 if rep["verdict"] == "ready" else 1


def cmd_setup(cfg: Config, *, fix: bool = False, quiet: bool = False,
              verbose: bool = False) -> int:
    """首次使用自检：做 PPT / 画图要用的那些工具，配好了没有。

    与两个邻居的分工（三件事，谁也别替谁答）：
      · `--mcp-doctor`  MCP **协议层**能不能开工（命令/重名/真装一遍/AnySearch key）
      · `--figure`      图面体检**装置**能不能用
      · `--setup`       这两件之外，再加上 **python-pptx / 审计脚本 / 模板 /
                        Origin COM / 渲染器** —— 一次问清"能不能做 PPT 和画图"

    `--fix` 只做**能证明安全**的那一类修复：把配置指向本机**已经能用**的东西
    （某个解释器里已经装好了 python-pptx，只是配置没指过去）。
    它**从不 pip install**，也从不碰 Origin / 技能目录。

    退出码：0 = ready；1 = degraded（能做 PPT，画图那侧缺东西）；2 = broken。
    """
    from .setupdoctor import diagnose_setup, fix_setup, format_report

    rep = diagnose_setup(cfg)
    if fix:
        res = fix_setup(list(rep.get("fixable") or []))
        for r in res["fixed"]:
            mark = "✔" if r.get("verified") else ("✘" if r["fixed"] else "·")
            print(f"[{mark}] {r['key']}：{r['why']}")
            if r["fixed"] and not r.get("verified"):
                print(f"      修完重测仍是 {r.get('after_level', '?')}："
                      f"{r.get('after_title', '')}")
        rep = res["report"]
        if not res["fixed"]:
            print("没有可一键修的项（其余项要人工装/指路）")
        print()
    if quiet:
        print(rep["summary_line"])
    else:
        print(format_report(rep, verbose=verbose))
        _print_next_steps(cfg, rep)
    return {"ready": 0, "degraded": 1, "broken": 2}.get(rep["verdict"], 2)


def _print_next_steps(cfg: Config, rep: dict) -> None:
    """自检之后给「下一步做什么」—— 这是 onboarding 的那一半。

    ★ 为什么放在 `--setup` 里而不是新造一个 `--welcome`：
      第一次用的人只会跑一个命令。做成两个命令，第二个永远没人跑 ——
      结果是自检报告很漂亮，人却不知道接下来干什么。

    三条信息，都是"新用户此刻真正会卡住的地方"：
      ① 现在能直接干什么（工具数、图型数、插件数）
      ② 花了多少钱、还剩多少（含"算不出来"这件事）
      ③ 下一步该敲哪条命令
    """
    print("\n【下一步】")

    # ① 能干什么
    try:
        from .loop import Rongke
        rk = Rongke(cfg, install_plugins=True, install_mcp_servers=False)
        tools = len(rk.registry)
        pr = getattr(rk, "plugins_report", {}) or {}
        # ★ 图型数必须在 `Rongke(...)` **之后**取：插件是在构造时注册图型的，
        #   在构造之前 import `CHART_KINDS` 拿到的是只有内置那 9 个的快照
        #   —— 装了插件的用户会看到"9 种图型"而其实有 10 种。
        try:
            from .analysis import CHART_KINDS
            kinds = len(CHART_KINDS)
        except Exception:                                      # noqa: BLE001
            kinds = 0
        print(f"  能用 {tools} 个工具、{kinds} 种图型。")
        # 按来源拆一下 —— 新用户最想知道「插件到底有没有生效」
        try:
            by = {}
            for n in rk.registry.names():
                t = rk.registry.get(n)
                src = getattr(t, "source", "") or "builtin"
                by[src] = by.get(src, 0) + 1
            if len(by) > 1:
                print("  来源：" + "、".join(
                    f"{k} {v}" for k, v in sorted(by.items())))
        except Exception:                                      # noqa: BLE001
            pass
        summ = str(pr.get("summary") or "")
        if summ:
            print(f"  插件：{summ}")
        try:
            rk.close()
        except Exception:                                      # noqa: BLE001
            pass
    except Exception as e:                                     # noqa: BLE001
        print(f"  （工具清单没取到：{type(e).__name__}: {e}）")

    # ② 账单
    try:
        from .billing import Billing, PLANS
        from .pricing import format_cost, UNKNOWN
        bal = Billing().balance()
        rem = bal["remaining"]
        if rem == UNKNOWN:
            print("  额度：本次账单算不出金额（价格表没填全）—— 填表用 `rongke --pricing`。")
        else:
            q = PLANS["free"]["quota_usd"]
            print(f"  额度：剩 {format_cost(rem)}" +
                  (f"（首次安装给了 {format_cost(q)} 试用额度）" if q else "") + "。")
            if not bal["exact"]:
                print("        ⚠️ 有算不出金额的历史消耗，这个数只是上限。")
    except Exception as e:                                     # noqa: BLE001
        print(f"  （账单没取到：{type(e).__name__}: {e}）")

    # ③ 下一步
    verdict = rep.get("verdict")
    print()
    if verdict == "ready":
        print("  可以直接开跑：")
        print("     rongke --web                打开控制台（推荐）")
        print("     rongke \"把 data.csv 画成图\"  命令行直接跑一个任务")
    else:
        print("  先把上面标 ✘ 的补齐，再跑。")
        print("     配 PPT/画图：rongke --setup --fix   （只把配置指向本机已有的东西）")
    print("  其他：")
    print("     rongke --tools      看全部工具")
    print("     rongke --plugins    看插件（放 .py 到 ~/.rongke/plugins/ 即可）")
    print("     rongke --billing    看余额与套餐（本地记账，不接支付）")


def _indent(text: str, pad: str) -> str:
    return "\n".join(pad + line for line in str(text).splitlines())


def cmd_figure(cfg: Config, spec_path: str = "") -> int:
    """跑一次图面体检；不给契约文件时只报「这套装置能不能用」。

    存在的理由和 `--mcp` 一样：图面体检的失败大多是**静默的** ——
    找不到能跑审计的解释器、找不到技能脚本、契约里两个分面画了同一件事。
    没有这个视图，用户只会看到"图面门不放行"而不知道为什么。
    """
    import json
    from .figure import FigureAuditor, FigureContract, route_for

    au = FigureAuditor(python_exe=cfg.figure.python or None,
                       skills_dir=cfg.figure.skills_dir or None,
                       timeout=cfg.figure.timeout)
    d = au.diag()
    print(f"图面体检 enabled = {cfg.figure.enabled}")
    print(f"  审计解释器   {d['python'] or '（没找到）'}")
    print(f"  技能脚本目录 {d['scripts'] or '（没找到）'}")
    if d["notes"]:
        print("\n⚠️ 有问题（这些**没有**被静默降级成'跳过并算过'）：")
        for n in d["notes"]:
            print(f"  · {n}")
    if not d["python"] or not d["scripts"]:
        print("\n装置不全 → 审计会判 SKIPPED，而 **SKIPPED 不算通过**。")
        print("请在 config.json 的 figure.python / figure.skills_dir 里补上绝对路径。")
        return 2
    if not spec_path:
        print("\n（没给契约文件。用法：--figure <契约.json>）")
        return 0

    p = Path(spec_path)
    if not p.exists():
        print(f"\n⛔ 契约文件不存在：{p}")
        return 2
    raw = json.loads(p.read_text(encoding="utf-8"))
    # 文件路径提示与五点契约混在一个 JSON 里：契约字段照常，
    # 多出来的 source/pdf/layout_manifest/legend_path 由这里取用。
    files = {k: raw.pop(k, None) for k in
             ("source", "pdf", "layout_manifest", "legend_path")}
    fc = FigureContract.from_dict(raw)

    # 先把契约头与路线打出来**再**校验：后端选错时，用户要能看到
    # "这张图被判该走哪条路"，而不只是收到一条"契约不成立"。
    print(f"\n契约：{fc.figure_id} | {fc.title}")
    print(f"  原型 {fc.archetype} · 后端 {fc.backend} · "
          f"{fc.width_mm}×{fc.height_mm} mm · {fc.panel_count} 分面")
    rd = route_for(fc)
    ok_mark = "✔ 声明后端满足" if rd.satisfied_by(fc.backend) else "✘ 声明后端不满足"
    # 这里只给一行结论。**判定依据不在这打印**：走通了由下面的报告块给，
    # 被拦了由 validate() 的问题文本给 —— 两处都给就成了同一句话说三遍。
    print(f"  路线 {rd.label}（{'强制' if rd.must else '可选'}）· {ok_mark}")

    problems = fc.validate()
    if problems:
        print("\n⛔ 契约不成立（先改契约，再谈画图）：")
        for x in problems:
            print(f"  · {x}")
        return 2

    rep = au.audit(fc, workdir=p.parent, **files)
    print()
    print(rep.format_for_model())
    print(f"\n报告：{rep.report_path}")
    return 0 if rep.ok else 1


def cmd_show(cfg: Config, rid: str, color: bool) -> int:
    for cand in (cfg.runtime.workspace_dir / rid / "session.json", Path(rid)):
        if cand.exists() and cand.is_file():
            path = cand
            break
    else:
        print(f"找不到任务 {rid!r}。用 --runs 看可用 id。", file=sys.stderr)
        return 2
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读不了 {path}：{e}", file=sys.stderr)
        return 2

    rep = make_reporter(color, clock=False)
    R = _C if color else {k: "" for k in _C}
    print(f"{R['b']}═══ 回放 {j.get('id')} ═══{R['r']}")
    print(f"  目标：{j.get('goal')}")
    print(f"  用时：{float(j.get('elapsed') or 0):.1f}s   "
          f"事件：{len(j.get('events') or [])} 个\n")
    for ev in j.get("events") or []:
        rep(ev.get("kind"), ev.get("data") or {})
    res = j.get("result") or {}
    print(f"\n{'═' * 62}")
    if res.get("success"):
        print(f"{R['g']}{R['b']}✔ 完成（证据门通过）{R['r']}")
    else:
        print(f"{R['red']}{R['b']}✘ 未完成{R['r']}")
    print(f"  原因：{res.get('reason', '')}")
    if res.get("final"):
        print(f"  交付：{res['final']}")
    if res.get("lint"):
        print(f"  写作体检：{_lint_brief(res['lint'])}")
    print(f"  步数：{res.get('steps')}   工具调用：{len(res.get('trace') or [])} 次")
    if res.get("draft"):
        print(f"\n{R['b']}── 交付正文（{_draft_head(res['draft'], res.get('lint'))}）──{R['r']}")
        print(res["draft"])
    print("═" * 62)
    return 0 if res.get("success") else 1


def _draft_head(draft: str, lint: dict | None) -> str:
    """正文标注的量纲要与体检一致。

    ★ 2026-10-26 修：这里原来写「体检报的是**字数**（中文字+西文词）」，
      而那正是 `writing.lint()` 里被修掉的量纲混算（词加到字上）。
      现在体检拆成三个量：`chars`（纯中文）、`en_words`（英文词）、
      `chars_eq`（折算总篇幅）。本函数按稿件语言取对应的那个：
      英文稿报「N words」，否则报折算后的「N 字」。
      `len()` 数的是**字符**（含标点），与任何一档都不同，只在没有体检时兜底。
    """
    if lint:
        m = lint.get("metrics") or {}
        if m.get("lang") == "en" and m.get("en_words"):
            return f"{m['en_words']} words"
        n = m.get("chars_eq") or m.get("chars")
        if n:
            return f"{n} 字"
    return f"{len(draft)} 字符"


def open_ws(path: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)                     # type: ignore[attr-defined]
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        print(f"  （打开工作区失败：{e}）", file=sys.stderr)


# ══════════════════ 入口 ══════════════════
def main(argv: list[str] | None = None) -> int:
    force_utf8()
    ap = argparse.ArgumentParser(
        prog="rongke", description="荣珂 —— 自研科研 agent 内核",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("goal", nargs="?", help="任务目标（自然语言）")
    # ★ `--version` 是发行件的基本礼貌：用户 `pip install rongke` 之后第一件事
    #   就是问"装上了啥"。`__version__` 是唯一真源（见 `changelog_test.py`），
    #   这里只是把它接出来，**不另存一份**。
    ap.add_argument("--version", action="version",
                    version=f"rongke {__version__}",
                    help="打印版本号后退出")
    ap.add_argument("--max-steps", type=int, default=None, help="最大步数")
    ap.add_argument("--contract-only", action="store_true", help="只起草契约并打印")
    ap.add_argument("--contract-out", metavar="PATH",
                    help="把起草的契约写到 JSON（人可改），配合 --contract-in 使用")
    ap.add_argument("--contract-in", metavar="PATH",
                    help="用冻结的契约文件开跑（跳过起草，人已审过）")
    ap.add_argument("--workspace", metavar="DIR",
                    help="指定工作区目录（默认按时间戳新建 runs/<stamp>/）。"
                         "★ 用来接着上一轮跑：内核每轮都新建空工作区，"
                         "上一轮的产物它看不到；把定稿预置进一个目录再用这个参数，"
                         "它才会接着改而不是从头重做。目录不存在会自动建。")
    ap.add_argument("--runs", action="store_true", help="列出历史任务后退出")
    ap.add_argument("--show", metavar="ID", help="回放某次任务后退出")
    ap.add_argument("--open-ws", action="store_true", help="跑完打开工作区目录")
    ap.add_argument("--json", action="store_true", help="结果以 JSON 输出")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--show-content", action="store_true", help="显示模型的思考文本")
    ap.add_argument("--tools", action="store_true", help="列出已装配工具后退出")
    ap.add_argument("--mcp", dest="mcp_status", action="store_true",
                    help="列出 MCP 服务端与工具（含没能连上的原因）后退出")
    ap.add_argument("--mcp-doctor", dest="mcp_doctor", action="store_true",
                    help="MCP **开工前自检**：key / 命令 / 依赖 / 重名 / 真装一遍，"
                         "逐条给结论与修法后退出")
    ap.add_argument("--mcp-doctor-quiet", dest="mcp_doctor_quiet", action="store_true",
                    help="只打印一行结论（适合放进脚本/启动前挂）："
                         "退出码 0=可用 / 1=有阻塞或降级")
    ap.add_argument("--pricing", dest="pricing_status", action="store_true",
                    help="看计价状态，并打印可粘贴的单价表骨架后退出")
    ap.add_argument("--plugins", dest="plugins_list", action="store_true",
                    help="列出已加载的插件（工具与图型），含**加载失败的**及原因，然后退出")
    ap.add_argument("--plugins-dirs", action="store_true",
                    help="只打印插件搜索路径与每个目录里有几个候选文件，然后退出")
    ap.add_argument("--presets", dest="presets_list", action="store_true",
                    help="列出能力预设与**挂载健康度**（坏件带理由），然后退出")
    ap.add_argument("--live", dest="presets_live", action="store_true",
                    help="配合 --presets：逐份真挂载并打印行数，坏件以退出码 1 报出")
    # ★ 用 `nargs="*"` 而不是 `nargs="?"`：
    #   `--billing plan pro` 里 `pro` 是第二个值，`nargs="?"` 只吃 `plan`，
    #   于是 `pro` 掉成位置参数（被当成任务目标）—— 实测踩到，
    #   `--billing plan pro` 打出了帮助。所以这里按"子命令 + 一个实参"吃。
    ap.add_argument("--billing", nargs="*", default=None,
                    metavar="子命令",
                    help="本地账单：show（默认看余额/套餐/用量）/ plan <套餐> / "
                         "topup <美元>。⚠️ **不接支付网关**，topup 只写本地流水，"
                         "不会真收钱")
    ap.add_argument("--license", nargs="*", default=None, metavar="子命令",
                    help="离线授权：show（默认看状态）/ machine-id（本机机器码）/ "
                         "activate <激活码> / deactivate。"
                         "发行方另有 keygen / sign --sk <私钥> --mid <客户机器码>")
    ap.add_argument("--customer", metavar="名字", default="",
                    help="配合 --license sign：写进凭证的客户名")
    # ★ `--magpie` 用 `nargs="*"`，理由与 `--billing` 相同：
    #   `--magpie deepseek/deepseek-v4-pro` 里模型名是第二个值。
    #   不带值 = 只体检（问一句"本机 magpie 在听吗"），不改任何配置。
    ap.add_argument("--magpie", nargs="*", default=None, metavar="模型",
                    help="走 magpie 本地网关（github.com/yetone/magpie）。"
                         "不带值：只体检网关是否在线，不改配置。"
                         "带模型名：把配置切到 magpie 并说明下一步。"
                         "加 --magpie-host <地址> 可指定 Docker/服务器形态的网关")
    ap.add_argument("--lic-plan", metavar="套餐", default="pro",
                    help="配合 --license sign：写进凭证的套餐名（默认 pro）。"
                         "★ 不能叫 --plan —— 那个名字已经被 --billing 语义占住")
    ap.add_argument("--magpie-host", metavar="地址", default="",
                    help="配合 --magpie：magpie 网关地址（默认 http://127.0.0.1:3425；"
                         "Docker/服务器形态填 http://<那台机器>:3425）")
    ap.add_argument("--days", type=int, default=0, metavar="天",
                    help="配合 --license sign：有效期天数（0=永久）")
    ap.add_argument("--sk", metavar="私钥", default="",
                    help="配合 --license sign：发行方私钥（base64）")
    ap.add_argument("--mid", metavar="机器码", default="",
                    help="配合 --license sign：客户机器码（RK-M-...，授权页可见）。"
                         "一码一机；不给会拒绝签发")
    ap.add_argument("--lessons", action="store_true",
                    help="看记忆的经验层（自动统计 + 人工），可配 --goal 预览实际注入内容")
    ap.add_argument("--goal", dest="preview_goal", metavar="目标",
                    help="预览这个目标会注入什么（隐含 --lessons）")
    ap.add_argument("--note", metavar="文本",
                    help="写一条人工经验到记忆的经验层（可单独用，也可配 --lessons）")
    ap.add_argument("--figure", dest="figure_spec", nargs="?", const="",
                    metavar="契约.json",
                    help="跑图面体检（不给文件则只报装置是否可用）；"
                         "退出码 0=全过 / 1=有阻断 / 2=装置不全或契约不成立")
    ap.add_argument("--setup", action="store_true",
                    help="**首次使用自检**：做 PPT / 画图要用的工具配好了没有"
                         "（python-pptx / 组会 PPT 审计脚本 / 品牌模板 / "
                         "matplotlib+PyMuPDF / Origin COM / 渲染器），逐项给修法")
    ap.add_argument("--fix", action="store_true",
                    help="配合 --setup：把**本机已经能用**的东西写进配置"
                         "（只改 deck.python / figure.python 这类指向，不装包）")
    ap.add_argument("--web", action="store_true", help="启动 Web 控制台")
    ap.add_argument("--port", type=int, default=None,
                    help="Web 控制台端口（不填用 `web/server.py:DEFAULT_PORT`）")
    args = ap.parse_args(argv)

    color = not args.no_color and not args.json

    if args.web:
        from .web.server import main as web_main
        # 端口默认值**不在这儿填** —— 交给服务端那份单源，避免两处各写一个数
        return web_main([] if args.port is None else ["--port", str(args.port)])

    try:
        cfg = Config.load()
    except Exception as e:
        print(f"配置加载失败: {e}", file=sys.stderr)
        return 2

    # ── 只读视图（不需要 LLM）──────────────────────────────
    if args.runs:
        return cmd_runs(cfg)
    if args.show:
        return cmd_show(cfg, args.show, color)
    if args.figure_spec is not None:
        return cmd_figure(cfg, args.figure_spec)

    if args.tools:
        rk = Rongke(cfg)
        print(rk.describe())
        for n in rk.tools:
            t = rk.registry.get(n)
            mark = "W" if t.mutating else " "
            # 只取描述首行：工作台的描述是多行文档串，直接切片会把表格撑歪
            desc = (t.description or "").strip().split("\n")[0]
            print(f"  [{mark}] {n:<24} {t.source:<10} {desc[:60]}")
        rk.close()
        return 0

    if args.mcp_status:
        return cmd_mcp(cfg)

    if args.mcp_doctor or args.mcp_doctor_quiet:
        return cmd_mcp_doctor(cfg, quiet=args.mcp_doctor_quiet)

    if args.setup or args.fix:
        # `--setup` 与它的一家（--fix 隐含 --setup）走这里。
        # 放在 provider 诊断**之前**：自检的全部意义就是"密钥还没配时也能问
        # 我缺什么"，跑到后面那几行 `LLM 不可用` 就没法用了。
        return cmd_setup(cfg, fix=args.fix, verbose=args.show_content)

    if args.plugins_list:
        return cmd_plugins(cfg, show_dirs=False)

    if args.plugins_dirs:
        return cmd_plugins(cfg, show_dirs=True)

    if args.presets_list:
        return cmd_presets(cfg, live=bool(args.presets_live))

    if args.pricing_status:
        return cmd_pricing(cfg)

    if args.magpie is not None:
        return cmd_magpie(cfg, models=list(args.magpie or []),
                          host=args.magpie_host)

    if args.billing is not None:
        bp = list(args.billing or [])
        action = bp[0] if bp else "show"
        # ★ 没有第二个值 → 传 _UNSET（"没给"）；有 → 原样传（可能是空串，
        #   而空串对 `password` 是**取消密码**这个合法意图）。
        arg = bp[1] if len(bp) > 1 else _UNSET
        extra = bp[2:]
        if extra:
            print(f"✘ --billing 最多接两个值（子命令 + 一个参数），多给了：{extra}")
            return 2
        return cmd_billing(cfg, action=action,
                           plan=arg if action in ("plan", "password") else _UNSET,
                           amount=arg if action == "topup" and arg is not _UNSET else "")

    if args.license is not None:
        lp = list(args.license or [])
        action = lp[0] if lp else "show"
        arg = lp[1] if len(lp) > 1 else ""
        if len(lp) > 2:
            print(f"✘ --license 最多接两个值（子命令 + 一个参数），多给了：{lp[2:]}")
            return 2
        return cmd_license(cfg, action=action,
                           code=arg if action == "activate" else "",
                           customer=args.customer or "",
                           days=int(args.days or 0),
                           plan=args.lic_plan or "pro",
                           sk=args.sk or "",
                           mid=args.mid or "")

    if args.lessons or args.note or args.preview_goal:
        # `--goal` 单独给也走这条路（隐含 `--lessons`）——
        # 否则它会掉进"位置参数为空 ⇒ 打帮助退出 2"的死胡同，
        # 而用户明明给出了目标，只是从 `--goal` 而不是位置参数给的。
        return cmd_lessons(cfg, goal=args.preview_goal or "", note=args.note or "")

    # ── 契约：起草 / 导出 / 载入 ────────────────────────────
    contract: TaskContract | None = None
    if args.contract_in:
        p = Path(args.contract_in)
        try:
            contract = TaskContract.from_dict(
                json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"契约文件用不了 {p}：{type(e).__name__}: {e}", file=sys.stderr)
            return 2
        print(f"{_C['dim'] if color else ''}· 已载入冻结契约："
              f"{p}（跳过起草，人已审过）{_C['r'] if color else ''}")

    if not args.goal and contract is None:
        if args.contract_only or args.contract_out:
            print("--contract-only / --contract-out 需要给出目标。", file=sys.stderr)
        ap.print_help()
        return 2

    if cfg.provider.diagnose() != "ok":
        print(f"LLM 不可用：{cfg.provider.diagnose()}", file=sys.stderr)
        print(f"  提示：设置环境变量 {cfg.provider.api_key_env} 后重试", file=sys.stderr)
        return 2

    goal = args.goal or contract.raw_goal      # type: ignore[union-attr]

    if args.contract_only or args.contract_out:
        if contract is None:
            llm = LLM(cfg.provider, timeout=cfg.loop.request_timeout)
            contract = TaskContract.draft(goal, llm)
        payload = json.dumps(contract.to_dict(), ensure_ascii=False, indent=2)
        if args.contract_out:
            Path(args.contract_out).write_text(payload, encoding="utf-8")
            print(f"契约已写入 {args.contract_out}")
            print("改完它，再用 python -m rongke --contract-in "
                  f"{args.contract_out} 开跑。")
            print("\n" + payload)
        else:
            print(payload)
        return 0

    rk = Rongke(cfg)
    if not args.json:
        print(f"{_C['dim'] if color else ''}{rk.describe()}{_C['r'] if color else ''}")

    # ── Ctrl+C 两级：先请求停止（走内核 cancel），再按才强杀 ──
    cancel = threading.Event()
    hits = {"n": 0}

    def _on_sigint(signum, frame):        # noqa: ANN001
        hits["n"] += 1
        if hits["n"] == 1:
            cancel.set()
            print(f"\n{R['y'] if color else ''}· 已请求停止 —— "
                  f"内核会在下一步开头退出（再按一次 Ctrl+C 强制中断）"
                  f"{R['r'] if color else ''}", file=sys.stderr)
        else:
            signal.signal(signal.SIGINT, signal.default_int_handler)
            raise KeyboardInterrupt

    R = _C if color else {k: "" for k in _C}
    old = signal.signal(signal.SIGINT, _on_sigint)

    t0 = time.time()
    try:
        result = rk.run(
            goal,
            contract=contract,
            max_steps=args.max_steps,
            on_event=None if args.json else make_reporter(color, args.show_content),
            cancel=cancel,
            workspace=Path(args.workspace).resolve() if args.workspace else None,
        )
    except LLMError as e:
        print(f"\nLLM 调用失败：{e}", file=sys.stderr)
        return 3
    except LicenseError as e:
        # 授权拦下的是"发起新任务"，不是"任务失败" —— 退出码单独给 4，
        # 让脚本能把它和真的运行失败（3）、强制中断（130）分开。
        print(f"\n✘ {e}", file=sys.stderr)
        print("  粘贴激活码：rongke --license activate <激活码>", file=sys.stderr)
        print("  查看状态：  rongke --license", file=sys.stderr)
        print("  （历史与已归档的结果不受影响，照常能看。）", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\n已强制中断（未完成）。", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGINT, old)
        # 关掉 MCP 子进程：它们是常驻的，留着不管就是泄漏
        # （Origin 服务端背后还挂着 COM 实例）
        rk.close()

    elapsed = time.time() - t0

    if args.json:
        out = result.to_dict()
        out["elapsed_sec"] = round(elapsed, 1)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if result.success else 1

    print(f"\n{'═' * 62}")
    if result.success:
        print(f"{R['g']}{R['b']}✔ 完成（证据门通过）{R['r']}")
    elif cancel.is_set():
        print(f"{R['y']}{R['b']}■ 已取消（未完成）{R['r']}")
    else:
        print(f"{R['red']}{R['b']}✘ 未完成{R['r']}")
    print(f"  原因：{result.reason}")
    if result.final:
        print(f"  交付：{result.final}")
    if result.lint:
        print(f"  写作体检：{_lint_brief(result.lint)}")
    print(f"  步数：{result.steps}   用时：{elapsed:.1f}s   用量：{result.usage.line()}"
          + ("" if result.usage_complete else
             "   ⚠️ 中断：这是中断前的累计，**下界而非总额**"))
    print(f"  工具调用：{len(result.trace)} 次"
          f"（成功 {len([c for c in result.trace if c.ok])}）")
    if result.run_id:
        print(f"  归档：{result.run_id}（python -m rongke --show {result.run_id}）")
    if result.draft:
        print(f"\n{R['b']}── 交付正文（{_draft_head(result.draft, result.lint)}）──{R['r']}")
        print(result.draft)
    print("═" * 62)

    if args.open_ws and result.workspace:
        open_ws(result.workspace)
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
