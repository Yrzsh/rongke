# -*- coding: utf-8 -*-
"""荣珂全套回归 —— 一条命令，把「跑没跑 / 真有断言吗 / 断言少了没」一起报出来。

    python regress.py              # 核心批（3.12）
    python regress.py --py313      # 零依赖子集换托管 3.13 复跑
    python regress.py --only web,resume
    python regress.py --save       # 把这轮计数存成基线
    python regress.py --with-ui-shot   # 附带真实浏览器截图（要先起控制台）

## 为什么要有这个文件

手敲批命令本身产出过**两个假信号**，都是"测量工具坏了，于是把失败读成成功"：

| 手敲的样子 | 实际读到的东西 |
|---|---|
| `$PY x.py \\| tail -3; echo $?` | `tail` 的退出码 —— **恒 0** |
| `grep -A2 FAIL \\| grep -c FAIL` | 管道 SIGPIPE 提前掐断 ⇒ **报 0** |

运行器把这两处收进一个地方：`subprocess` 直接拿 rc，输出**落盘**再解析 ——
不过管道、不碰 shell 变量。顺手落盘还有个好处：失败现场留着，不用重跑。

## 第三条纪律也落在代码里

**`rc=0` 且一条断言都没扫到 = 可疑，不算通过。**
一个测试可能因为提前 `return` / 异常被吞 / 断言被删空而"绿得毫无内容"。
所以有断言计数的脚本，计数为 0 一律报红（退出码 2），而不是混进"全绿"里。

## 第四条：断言数不许悄悄变少

`--save` 存基线；之后的每轮都跟基线比。**少了就报**（不自动改基线）——
删掉几条断言让红变绿，是测试最容易被无声腐蚀的方式。
多了不报（是好事），只记。
"""
from __future__ import annotations

import argparse
import atexit
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# ★ 运行器自己也要换成 UTF-8。本机控制台默认 GBK，而汇总行里有 `⚠️` / `✘` ——
#   实测被崩过一次：29 项全跑完、结果行都打出来了，最后**在打印基线警告时抛
#   UnicodeEncodeError 退出**，整份判决就此丢失，看起来像"回归没跑"。
#   测量工具自己挂掉，比被测对象挂掉更糟（没人知道有东西坏了）。
#   与被测脚本同一处治法（`template_test.py` 开头那两行），不是新发明。
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
LOGDIR = ROOT / "runs" / "_regress"
BASELINE = LOGDIR / "baseline.json"

# 测试工具箱（挪走代替删除，见它开头那段）。别名用 `TK` 不用 `T` ——
# 下面那个清单构造函数 `T()` 已经把这个名字占了。
import _tkit as TK                                            # noqa: E402

PY312 = os.environ.get(
    "RK_PY312", os.path.expanduser(r"~\AppData\Local\Programs\Python\Python312\python.exe"))
PY313 = os.environ.get(
    "RK_PY313", os.path.expanduser(r"~\.workbuddy\binaries\python\versions\3.13.12\python.exe"))

# 两种脚本，如实分开对待：
#   · 打 `[PASS]/[FAIL]` 或 `✔/✘` 的 ⇒ 默认 `counted=True`，计数为 0 就是可疑
#   · 只打自己那套人读报告的（selftest / eval）⇒ `counted=False` + `landmark`：
#     **必须出现这句收尾话**。它在 = 真跑到终点了；提前 return / 异常被吞就抓得住。
#     这比假装它们"有计数"诚实 —— 判据说什么、不说什么，都要写清楚。
def T(tid: str, cmd: list[str], counted: bool = True,
      zero_dep: bool = True, landmark: str = "") -> dict:
    return dict(id=tid, cmd=cmd, counted=counted,
                zero_dep=zero_dep, landmark=landmark)


TESTS: list[dict] = [
    T("writing_gate", ["writing_gate_test.py"]),
    T("writing_lint", ["writing_lint_test.py"]),
    # 文献够用性判定 + 取全文能力 + 综述流程的接缝（2026-10-22 新增）。
    # 为什么它必须常跑：这一套守的是**用户流程的第一条**
    # ——「先判库够不够，不够才去检索入库」。零依赖纯标准库，
    # 只有 B 段（假 DOI 探三条路）走一次真网络；网络不通时那三条会失败，
    # 那是**如实报告**（取不到就说取不到），不是测试坏了。
    T("lit_coverage", ["lit_coverage_test.py"]),
    T("figure",       ["figure_test.py"]),
    # 原生 SVG 出图 + 第 8 条门「预览路线」分支。正负控都在同一份里：
    # 九种图型真出图并读回、结构自检能打红、门的三个前提逐个反例。
    T("svgplot",      ["svgplot_test.py"]),
    # SVG 质量门（svgq）：把结构自检升成可量的质量。纯标准库。
    # 与上一行分工：那边守「产物结构 + 预览路线放行语义」，这边守
    # 「字号物理量纲 / 对比度 / 文字容纳 / 碰撞 / 数据保真 / 字形」。
    # 承重断言是 3.1：同一份「锚点在框内、字画出框外」的产物，
    # 旧 inspect_svg 判通过、新门判红 —— 这条不成立门就是白加的。
    T("svgq",         ["svgq_test.py"]),
    # 第二层浏览器真尺子（svgmeasure）。**不是零依赖**：要 playwright + Chromium。
    # 与上一行分工：那边全用估算替身，证明不了「浏览器真量出来的东西是对的」。
    # 承重断言是 B5：本机 font 目录里确实装着的字体，探针必须判在。
    #   第一版拿「一个故意不存在的字体名」当唯一基线，而它在中文 Windows 上
    #   回退到的正是微软雅黑本身 ⇒ msyh.ttc 明明在却判不在（宽度全等）。
    #   只装默认那一个中文字体的机器上，这个假阴性会翻成 missing_fonts ⇒ 假 FAIL。
    #   已做变异自检：把三基线退回单基线，恰好 B5 失守（15 PASS / 1 FAIL）。
    # 缺件时响亮 SKIP 且 rc=2（同 kb_convert，本仓 I 型），绝不把没跑报成通过。
    T("svgmeasure",   ["svgmeasure_test.py"], zero_dep=False),
    # 机制图后端 + 常驻插件（svgdiagram / svg_diagram）。模型只声明规格，
    # 几何全由后端算；「线不穿框」「区室不吞并」是布局保证，门量不到，
    # 所以 B1/B3 直接量布局与产物几何，B3 另用整组并集框当负控证明检查器有牙。
    # 承重自检 C4/D3：让位失效 / 退回旧留白口径 ⇒ 碰撞门必须红。
    # D 段要浏览器，缺件响亮 SKIP 且 rc=2（同 svgmeasure）。
    T("svgdiagram",   ["svgdiagram_test.py"], zero_dep=False),
    # 组会 PPT（第 9 条硬门 + 领域按需挂载）。
    # ⚠️ **它已经不是"纯标准库"了**（2026-09-21 晚订正）：
    #   本行原写「纯标准库 ⇒ 零依赖，3.13 也跑」。前半句早就过时了 ——
    #   §18.14/§19/§21/§22/§23.F 要**造真 .pptx**（`make_real_pptx` /
    #   `make_stale_skeleton` / `make_overflow_deck`），都要 shell 到一个
    #   **装了 python-pptx 的解释器**。
    #   ★ 症状不是报错，是**断言数腰斩**：3.13 下 `find_notes_python("")`
    #     拿不到解释器 ⇒ §21 把 `None` 塞进 `subprocess.run` ⇒ `TypeError`
    #     ⇒ 套件当场崩，§21–§24 一行不跑，**361 → 224**。
    #   ★ 为什么现在仍留在零依赖子集：它**问 config**（`deck_test._py_pptx()`
    #     → `config.json` 的 `deck.python`，就是下面 `deck_live` 注释里那个
    #     Python312），宿主解释器是谁都不影响 —— 与 `notes` 同一种做法。
    #     反过来，凡"拿当前解释器兜底"的写法，换个解释器跑必坏。
    # 它自己的「承重自检」一节会把判据拿掉、要求正控当场失守 ——
    # 这是本项目对"检查在，却永不触发"（G 型）唯一的解药。
    T("deck",         ["deck_test.py"]),
    # 「按品牌模板出稿」这条**产出路径**（`deck_from_template` + 门侧的基线扣除）。
    # 与 `deck_test.py` 分工：那边管**体检判据**，这边管**产出路径**
    # （保留模板页 → 填字 → 删页 → 从磁盘复测 → 写 `.origin.json`）。
    # ⚠️ **刻意不依赖用户那份 47MB 真模板**：用**合成模板**（3 页、含一处故意越界），
    #    秒级、两个解释器下同一个数，也不会因为用户把桌面清干净了就红。
    # 真模板那条路由人跑的探针验（`runs/_regress/_zjut/_e2e_template.py`）。
    T("template",     ["template_test.py"]),
    # 「组会 PPT」的**接线**测试：真 spawn vendored MCP 服务端（`vendor/ppt-mcp`），
    # 真建 .pptx、真过第 9 条门。与 `deck_test.py` 分工不许互相替代 ——
    # 那边用桩注册表证"判据对不对"，这边证"这条链真接通了"。
    # zero_dep=False：它要 `vendor/ppt-mcp` 的依赖（mcp/python-pptx/…），
    # 而那由 **Python312** 提供（见 config.json 的 `mcp.servers.ppt.command`）
    # ⇒ 3.13 零依赖子集里跑不了，标错不如不标。实测约 20s，主要花在 spawn 上。
    T("deck_live",    ["deck_live_test.py"], zero_dep=False),
    # 「写讲稿备注」这条**能力本身**的测试（不是接线）。
    # 上游 37 个工具没有写备注的能力，内核自己补了一个（`set_slide_notes`）——
    # 实测血债：补之前那场任务 25 分钟零交付（详见该文件头的诊断）。
    # 与 `deck_test` 第 13 节分工：那边守拒绝/注册/H 型措辞（纯标准库），
    # 这边守"真的把备注写进一份 .pptx 并逐页核回来"（要用 python-pptx）。
    # zero_dep=True：宿主不需要重依赖，它只 shell 到 config 里那个
    # 装了 python-pptx 的解释器（`deck.python`）—— 3.13 下实测同为 42/42。
    T("notes",        ["notes_test.py"], landmark="[DONE] 讲稿备注写入全部通过"),
    T("memory",       ["memory_test.py"]),
    T("stages",       ["stages_test.py"]),
    # 课题台账（导师模式 P1/P2）：跨 run 的长生命上下文 + 卡点台账。
    # 补的是内核一直缺的那条**时间尺度** —— 原来只有「一次 run」，
    # `report_blocker` 是终局动作 ⇒「上次到哪了？」「这个坑你两周前卡过。」
    # 这两句话没有对象可查。与 `stages_test.py` 第 11/12 节同源（都测首条消息的注入）。
    # 纯标准库 + 纯文件系统 ⇒ 两个解释器下应得同一个数（实测 104/104）。
    # ★ 它有一条别处没有的纪律：**所有写操作落在临时目录**
    #   （`_make_topic_tools(root=…)`）—— 一个会写用户真账
    #   （`<项目根>/topics/`，那是学生几个月的计划与卡点）的测试是负资产。
    T("topic",        ["topic_test.py"]),
    # 实验台账（2026-11-02 加）：定性/定量必须分开、缺口必须能算、
    # 「下一步」必须机械可复现且逐条带来源。
    # 起因是用户的原话：「老师说我数据、在催我，我也很迷茫」——
    # 一核发现他不是没做实验，是**做的全是定性观察**（"三株长得慢""其他几株快"
    # 一条数字都没有）。所以台账里 observe 与 measure 是两个 kind，
    # 于是"没数据"能被代码算成 `len(measures)==0 and len(observes)>0`，
    # 而不是靠人读。
    # ★ 自带成对负控：定性字写进 value 必须抛错（不抛就永远算不出缺口）；
    #   `render_md(now, asof)` 两次必须逐字节相同（否则不能当基线）。
    # ★ 全程临时 topics 根目录，不碰真账 ⇒ 零依赖，两个解释器下同一个数。
    T("lab",          ["lab_test.py"]),
    # 导师模式常驻回归（W25）：开关链 / 三条出处判据 / P3 本地复算层 /
    # 挂载不变量（needs_kb ⇒ kb_* 必在 allow）。取代 00_总控/_w22 那个
    # 带文件指纹基线的一次性守卫 —— 指纹基线会被后续正当改动撞红（实测 19/20），
    # 行为级判据不会。纯标准库 + 假领域表 ⇒ 零依赖，两个解释器下应得同一个数。
    T("mentor",       ["mentor_test.py"]),
    # 导师「真视角」：立场 / 四镜头 / 可复算疑点 / 结构化 finding 读回，
    # 含十条定向负控（每条只翻自己）与「生产真接线」端到端断言。
    # 单开一套，是因为原来 mentor 套件只管出处标注与挂载，管不到「评得对不对」。
    T("mentor-review", ["mentor_review_test.py"]),
    # 组会预演 / 开题答辩（2026-11-14 用户点名的两个场景）。单开一套的理由：
    # mentor-review 那套锁的是「终审稿子评得对不对」，这一套锁的是**会前**的两种
    # 形态 —— 预演讲什么（Q 标号 / 预判提问）与「预演直接给 PASS」要打回去。
    # 三个真缺陷是在真回包与真材料上量出来的：换行被当成「递了成篇材料」、
    # Markdown 表不算原表、`[Q1]` 解析不出来。
    T("mentor-rehearsal", ["mentor_rehearsal_test.py"]),
    # 导师模型（advisor profile，2026-11-14 续）：把某位导师的判断方式蒸馏成四层
    # （method_core / academic / persona / playbook）。单开一套的理由：它锁的不是
    # 「评得对不对」，而是**判准从哪来、能不能核** —— source= 标注、空层不补模板、
    # 改前备份、命中不了就一个字都不注入。素材不够时靠模板蒙混，是这条链上
    # 唯一会静默失效的地方（用户会以为在用自己导师的判准，其实用的是通用模板）。
    T("advisor",      ["advisor_test.py"]),
    # 接线：从界面按下发送到内核组装提示词，中间四跳。断任何一跳都不会报错，
    # 所以判据是**真跑一遍去提示词里找那位导师的判准原文**，不是 grep 源码。
    T("advisor-wire", ["advisor_wire_test.py"]),
    T("outline", ["outline_test.py"]),
    # 方法台账 + 创新点（2026-11-15）：参数必带 src、版本不可复用/覆盖、
    # 作废不改旧行、专利线索**不下新颖性结论**。
    T("methods", ["methods_test.py"]),
    # 板内质控（2026-11-16）：算法口径取自 GitHub plateqc(MIT)，
    # 但修了它的 nan/inf 静默污染，并把删失值与缺失值分桶。
    T("plate", ["plate_test.py"]),
    T("mentor_ui",    ["mentor_ui_test.py"]),
    # 导师模式的**工作台**半边（W28）：有界文档索引 / 一窗正文（offset/limit）/
    # 卡点分页（open|resolved|all）/ 会话课题身份（`mentor_mode` 事件 + 快照）。
    # 为什么单开而不并进 `topic_test`：那套锁的是台账本身（建/切/卡点/注入），
    # 这套锁的是**界面读台账的方式** —— 它有三个只靠行为级判据才抓得到的静默失效：
    # 翻页翻回开头、文档"没写过"被报成"写了 0 字"、追问轮悄悄读到别人刚切走的课题
    # （最后一条是 `topic_write` 那次实测事故（证据写进 `cordyceps-cultivation`）的同一根因）。
    # 纯标准库 + 临时台账根 ⇒ 不碰用户真账，两个解释器下应得同一个数。
    T("mentor_ws",    ["mentor_workspace_test.py"]),
    # 交付复核的**常驻结构判据**（2026-11-04）：为什么要单开一套，
    # 而不是往 mentor_test / mentor_workspace_test 里加几条 —— 那两套锁的是
    # 「导师模式的开关链」与「界面读台账」，恰好都**没有量**这三件真跑抓到的事：
    #   ① 普通任务查了库却不复核 / 写稿答复空白而盘上有正文（复核只读 final）；
    #   ②「标注格式读不到」被写成违规文案（读不到 ≠ 不符合）；
    #   ③ 复核器与 renderMentorAudit 都曾是「造好了没接线」的死代码。
    # 判据全是 AST / 函数返回值 / 源码接线，纯标准库，两个解释器下同数（51）。
    T("kbmentor-audit", ["kbmentor_audit_test.py"]),
    T("caps-ext",     ["caps_ext_test.py"]),
    # 2026-11-05「检索文献做成插件」：外部能力的四条真消费点（提示段 /
    # 工具必留面 / SYSTEM 节 / 门禁规则）＋ baseline 等价与坏件负控。
    # 要 import rongke.loop 与 writing（重依赖）⇒ 3.13 零依赖子集跑不了。
    T("lit-search-cap", ["lit_search_cap_test.py"], zero_dep=False),
    # 2026-11-05 真写作长稿场抓到的死门：契约 forbidden=["做 PPT"] 却
    # needs_deck=true ⇒ 第 9 条硬门反复要 audit_deck，被互斥规则打回 4 次。
    # 判据量三条构造路径的自洽不变量 + 正反两方向 + 否定机器单源。
    # 要 import rongke.contract / writing（重依赖）⇒ 3.13 零依赖子集跑不了。
    T("contract-deck-veto", ["contract_deck_veto_test.py"], zero_dep=False),
    # 2026-11-05 内核优化：SYSTEM 可逆分节 / 开场一次装配 / complete:true
    # 接线 / 额外判据 slot 化 / 工具可见性回退。全是纯函数与真调接口，
    # 不碰共享状态，但 `import rongke.loop` 要重依赖 ⇒ 与 :239 同族，
    # 标 zero_dep=False（3.13 零依赖子集跑不了，标错不如不标）。
    T("system-assembly", ["system_assembly_test.py"], zero_dep=False),
    # 写作前技能简报：baseline 逐字节等价 / 挂载真注入 / 缺件不坏件 / 预算硬。
    # 正控负控都量，不把「挂上了」当成「生效了」。
    T("stage-brief",  ["stage_brief_test.py"], zero_dep=False),
    # 过程反馈：一次读回「还差什么」；判定面优先落盘全文（含附表）。
    T("process-feedback", ["process_feedback_test.py"], zero_dep=False),
    T("source-alignment", ["source_alignment_test.py"], zero_dep=False),
    # 2026-11-09 本场结构单源：形状→步数/预设、工作区读回同源、大纲门分母。
    # 正控负控都量：带附录的规范大纲必须一次过门，真空章仍必须打回。
    T("shape",        ["shape_test.py"], zero_dep=False),
    # 2026-11-09 稿件台常驻插件：格式体例 + 前端可视化数据。
    # 负控钉死用户裁决：不进任何预设、不加前端胶囊、baseline rows 不变。
    T("manuscript-desk", ["manuscript_desk_test.py"], zero_dep=False),
    # 2026-11-10 交付就绪度：判据/预算前移进工具回包；纯只读（绝不碰 gate）、
    # 成熟度分诊、长综述无人值守自动续步（intervene_test 第 8 节同场复验）。
    T("readiness",    ["readiness_test.py"], zero_dep=False),
    # 2026-11-10 图表编排台：图/表插进正文的原子基元 + 锚点判据；
    # 常驻、不进预设、预览级不冒充投稿级，正负控与承重自检都量。
    T("visual-desk",  ["visual_desk_test.py"], zero_dep=False),
    # 2026-11-10 幻灯片编排台：结构化内容 → 带正文源稿 → 精排 → 讲稿 →
    # 写后读回 → 幻灯片台。量常驻/不进预设、六种页型、短讲稿读回、
    # 失败不改交付件、能力不可用不静默降级、围栏、四个工具真调。
    T("deck-desk",    ["deck_desk_test.py"], zero_dep=False),
    # 2026-11-10 深夜：AI 画布（用户纠正方向后的图元级工具）。
    # aideck 是项目根下的**独立包**（不进 rongke/），判据走包内 selftest 的
    # 包装脚本；canvas-desk 量的是「主循环里的 AI 真够得着这个工具」。
    T("aideck",       ["aideck_test.py"], zero_dep=False),
    T("canvas-desk",  ["canvas_desk_test.py"], zero_dep=False),
    # 2026-11-11 格式排版台 + 论文生态总线：AI 声明 blocks，版式由 STYLES 单源算；
    # HTML/docx/tex 三条路逐块读回；论文台只读汇总写/画/排/讲/投的交付态。
    # 量常驻/不进预设、体例三档、坏图降级、源稿优先级、承重自检。
    T("typeset",      ["typeset_test.py"], zero_dep=False),
    # 2026-11-11 GitHub 调研备料：工具「可调用种类」的目录单源。
    # 量目录与真实枚举读回一致、许可分级有牙（GPL/CC BY-SA/不明一律不放行）。
    T("catalogs",     ["catalogs_test.py"], zero_dep=False),
    # 能力工具化准入：只量「成品会不会变好」这条轴。三套已验证工具（数据图/PPT/
    # 排版）必须 exemplar；查询侧工具不得冒充产出侧；每条准入都有短路承重负控。
    T("toolize",      ["toolize_test.py"], zero_dep=False),
    # 文献池单源：检索台账去重 / 逐篇判定留痕 / 精读卡回溯全文 / 章-文献映射 /
    # 五连读回每环单独打红 / 删文献成孤儿 / 老式编号引注不误伤 / 写后逐字节读回。
    T("litpool",      ["litpool_test.py"], zero_dep=False),
    # 文献池所有权层：回包自动入池 / 写后引导 / 交付态结论 / 章映射越界 advisory /
    # 主循环接线用 AST 钉住（含 gate.check 仍只 1 处、参数链未动）。
    T("litpool-guard", ["litpool_guard_test.py"], zero_dep=False),
    # 机制图状态面：给 AI 的图元级 Origin（机制图.json 单源、稳定 id、query /
    # 只改一个图元、级联删、投影剥内部字段、默认路径零副作用、七道门复用、
    # 真 ToolRegistry.call 参数链、AST 钉门数）。工具数不变、SYSTEM 不动。
    T("diagram-state", ["diagram_state_test.py"], zero_dep=False),
    # 交付物质量：真出图 / 真浏览器实测 / 形状内接框容量 / 文字容纳门的
    # verdict 与反馈闭环 / 区室结构身份 / 宽图可执行建议值。
    # 基准独立于被测模块（1.20 来自 12 样本实测上界），正负控两方向都量。
    T("diagram-quality", ["diagram_quality_test.py"], zero_dep=False),
    # AI 操作面协议：显式回执 / 乐观锁 / 分层读取 / 意图级真原子事务 /
    # 快照回滚 / 双通道反馈 / 默认路径零副作用。按 GitHub 上的
    # AI-native 画布接口模式落地，工具数与门数都不许长。
    T("diagram-surface", ["diagram_surface_test.py"], zero_dep=False),
    # 布局意图：layout/align/route 真的改变几何（量 polys/center 指纹，
    # 不量文案）；空 hints 与默认逐点一致；七道门不退化；默认路径零副作用。
    T("diagram-layout", ["diagram_layout_test.py"], zero_dep=False),
    # 工艺审计（2026-11-14）：配色语义 / 尺度均衡 / 折行断词 / 标签压线 /
    # 区室不嵌套 / 回边不穿节点，六条各带改坏即红的负控。
    T("diagram-craft", ["diagram_craft_test.py"], zero_dep=False),
    # 科研通路插图（另一种品类）：磷脂膜 / 细胞器 / 波浪 mRNA / 修饰点 /
    # 低饱和分区；写后读回、七道门实测、图元级操作面与事务回滚。
    T("pathway",       ["pathway_test.py"], zero_dep=False),
    # P0 第一批真接入的 11 种：几何结构指纹 / 七道门不退化 / 描述单源 / 许可防线。
    T("kinds-p0",     ["kinds_p0_test.py"], zero_dep=False),
    # 图表身份层：登记单源、编号重编、五连读回、向后兼容与承重自检。
    T("figure-registry", ["figure_registry_test.py"], zero_dep=False),
    # 真任务暴露的三处：调用时机简报 / 覆盖丢锚点 / writing 自动续步。
    T("call-timing",    ["call_timing_test.py"], zero_dep=False),
    T("fix-all",        ["fix_all_test.py"], zero_dep=False),
    # 岸记：实验手记 / 规划看板做成外部记忆插件。量体例、append-only、读回一致、
    # 规划同源、挂载与撤销纪律，以及「配置在场 ≠ 已生效」的真调 fn 落盘。
    T("shore-journal", ["journal_test.py"], zero_dep=False),
    T("telemetry",    ["telemetry_test.py"]),
    T("tools",        ["tools_test.py"]),
    # 接线全景只读自检（2026-11-14）：九条判据 + 每条定向负控 + 会话指纹两方向。
    # 单开一套，是因为「模块都在、线没接上」这类事故（force_brief 单测全绿但
    # 生产没调）在别处测不到；末尾还有源码级断言钉住 CLI / Web / regress 三处接线。
    T("wiring",       ["wiring_test.py"]),
    T("web",          ["web_test.py"]),
    # 零覆盖路由冒烟（2026-10-12 体检补）。为什么单开：那次对撞发现
    # server.py 34 条 `/api/` 路由里有 16 条**没有任何测试提到**，
    # 其中 `/api/orig/*` 里藏着「任意文件读取」（能读 `~/.rongke/secrets.json`）。
    # 这套只锁"打得到、边界会拒、不 5xx"——第一次跑就抓出 `/api/runs?n=abc`
    # 抛未捕获 ValueError 把连接掐断（客户端只见 RemoteDisconnected）。
    # 纯标准库 + 不启真内核 ⇒ 零依赖。
    T("routes",       ["routes_test.py"]),
    # 对话主路径的**契约**（`/api/run` + `/api/stream`）。为什么单开：
    # `web_test.py` 一条判据都没打在它上面，而它是用户看到一切的来源；
    # 当时只有 `ui_shot.py` 回放 + 人工真跑兜着（一次 225s + 花 token）。
    # 这里用 Run 替身走真 HTTP —— 零模型调用、跑一次约 1 秒、可重复。
    # 钉住的是契约：终局帧 `_end`、交付正文在 `data.result.final`、
    # 不带 id 的 404 口径、以及"边跑边推"（不是攒完一次吐）。
    T("chat_path",    ["chat_path_test.py"]),
    T("app",          ["app_test.py"]),
    T("intervene",    ["intervene_test.py"]),
    T("resume",       ["resume_test.py"]),
    T("cache_probe",  ["cache_probe.py", "--offline"]),
    T("ui_check",     ["ui_check.py"]),
    T("ui_check_st",  ["ui_check.py", "--self-test"]),
    # 「你连的控制台跑的是不是当前代码」这条判据的自检。
    # 它必须常跑：这条判据的职责是**拦住假绿**，而它自己一旦坏掉就是最大的假绿源
    # （我第一版就是坏的：每次请求现读磁盘 ⇒ 永远报"一致"）。
    # 有活控制台时含端到端正/负控；没有时它会**如实说跳过**，不冒充通过。
    T("ui_shot_st",   ["ui_shot.py", "--self-test"]),
    # 测试工具箱自己的正控/负控。它必须常跑：**所有**测试都 import 它，
    # 它错了的话每个用它的测试都会以"绿"的样子错（地基塌了楼还站着）。
    T("tkit_st",      ["_tkit.py", "--self-test"]),
    # 换行策略（`.gitattributes`）的承重自检。**它守的是「机制还在生效」**，
    # 不是"我写了规则"：属性被改错、被更晚的规则遮住、或被谁删掉时，
    # 失效是**静默**的（G 型：检查在，却不再触发）。
    # 血债：一次 122 行的真改动被 CRLF 噪声读成「443 增 / 321 删」。
    # 纯标准库 + 直接问 git 自己（`check-attr` / `hash-object --path`）⇒ 零依赖。
    T("eol",          ["eol_test.py"], landmark="[DONE] 换行策略自检全部通过"),
    # 仓库卫生：**内核包里不许出现删除调用**。
    # 它守的东西和 `eol` 同类 —— 不是"我改对了"，而是「机制还在生效」：
    # 本机的批量删除守卫按条目计费（50 点/轮），生产面多一个删除点就多一处
    # "跑到一半被守卫杀掉、却没有 traceback"的可能，而红的是**别的**测试。
    # 它的三条反向对照（真调用命中 / 字符串里的调用命中 / 注释引述不命中）
    # 是这套判据唯一不变成"大概挺严"的印象的地方 —— 见该文件头那段。
    T("hygiene",      ["hygiene_test.py"],
      landmark="[DONE] 仓库卫生自检全部通过"),
    T("eval",         ["eval/baseline.py", "--self-test"],
      counted=False, landmark="自检全过"),
    # 任务判据**执行器**的自检。与上一条分工不许互相替代：
    #   `baseline.py` 管归档侧的**口径**（分母怎么算、样本不够时拒绝出数）；
    #   `run_tasks.py` 管判据侧的**牙齿**（`chars_range`/`numeric_anchor`/`gate`
    #   这 8 种判据拿到一份真交付到底判不判得出「坏」）。
    # ★ 为什么必须进这套件而不是"人记得跑一下"：那 74 条 check 在 2026-09-26 之前
    #   **一条都没被执行过** —— 判据写在 `tasks.jsonl` 里长得跟"已经守住了"一模一样，
    #   而全仓只有一处 `grep` 得到的 JSON 形状校验。这正是 G 型（检查在，却永不触发）。
    # 它的四条承重探针（把判据核心抽掉后自检必须变红）是这套判据不恒真的唯一证据。
    T("eval_run",     ["eval/run_tasks.py", "--self-test"],
      counted=False, landmark="仪器自检通过"),
    # 「技能门值不值」四臂实验的**设计自检**（不跑任务、不花钱）：
    # 验的是"消融开关到底换没换、对照文本长度配没配、拼装是不是同一条路"。
    # 为什么值得进套件：它抓到过一次真 bug —— `TaskContract.__post_init__`
    # 见到空 stages 就从目标文本**重新推断**，所以 `replace(c, stages=[])` 关不掉门；
    # 没有 ② 这条断言，四臂会跑得一模一样，然后把噪声当成结论。
    # zero_dep=False：它 `import rongke.loop`（内核装配要碰 workbench 桥）。
    T("ablate_design", ["eval/ablate_skill_gate.py", "--stages", "draft"],
      counted=False, zero_dep=False, landmark="设计成立"),
    # 需要重依赖 / 拉外部进程 —— 零依赖解释器跑不了，不参与 --py313
    # `fence` 也得进这一组：它 `install_workbench`，而桥 `import workbench_mcp`
    # 又 `from mcp.server.fastmcp import FastMCP` ⇒ 3.13 下必 `No module named 'mcp'`。
    # （我起初想当然把它算成零依赖 —— 一跑就被这条判据打回来。标错不如不标。）
    T("fence",        ["fence_test.py"], zero_dep=False),
    T("mcp_client",   ["mcp_client_test.py"], zero_dep=False),
    # 知识库**链路层**端到端（2026-09-22）：上传 PDF → 解析入库 → 检索 → 库隔离。
    # 与 `runs/_regress/kb_wire_probe.py` 分工，**不许互相替代** —— 那个量工具层
    # （反射进来没有 / 围栏登记没有 / 真调回包对不对），这个量链路层
    # （HTTP 路由 → `Manager.kb_upload` → 工具注册表 → litkb → 再回到面板读的端点）。
    # 血债：工具层全绿**不代表链路上接对了**，而面板的数据全来自注册表 ——
    # 有一环没接上，界面会以"0 篇文献"的样子**假绿**。
    # ⚠️ 要 PyMuPDF（造夹具 + litkb 抽文本）⇒ 3.13 下跑不了，与 `fence` 同组。
    T("kb_e2e",       ["kb_e2e_test.py"], zero_dep=False,
      landmark="[DONE] 知识库端到端全部通过"),
    # 知识库**工具层**验收（2026-09-22 补进回归）：反射进来没有 / 围栏登记没有 /
    # schema 有没有参数 / 新补的 5 个工具真调一次对不对。
    # ⚠️ 它原来住在 `runs/_regress/` 且**不在任何清单里** —— 只有人记得才会跑。
    #    一个守着"荣珂看得见知识库"却靠人记得的判据，等于没有判据。
    T("kb_wire",      ["kb_wire_test.py"], zero_dep=False,
      landmark="[DONE] 知识库工具层验收全部通过"),
    # 知识库**检索韧性**（2026-10-10 W28 加）。守两件实操中必然踩到的事：
    #   ① 两字词 —— litkb 的 fts 表是 `tokenize='trigram'`，而 `_sanitize_match`
    #      要求 CJK ≥3 字，**少于 3 字直接丢词且不报错**。实测 `MATCH "温度"`
    #      → 0 行（索引里就没有）。词表被过滤空时 `_match_expr` 返回 None，
    #      `search_paras` 遂**整个跳过 MATCH** —— 查询从未执行。
    #      于是 `融合/温度/酶解/菌株/选育` 全搜不到，而这恰是中文检索最常用的词。
    #      荣珂侧 `_wrap_kb_search` 用 LIKE 补回（litkb 一个字不动）。
    #   ② kb_ask 兜底的三条边界：上游能答不插手 / 空片段给诚实答案 /
    #      **真错误（伪 id）不许被兜底盖掉**。这块原来只有一次性脚本覆盖，
    #      不在任何清单里 —— 与 kb_wire 当初的处境一模一样。
    # 判据走**荣珂自己的反射路**（reflect_workbench_tools），不直接调 litkb：
    # 直接调只能证明 litkb 能跑，证明不了荣珂这边接对了。
    # ★ 自带负控：模掉 `_kb_short_terms`，第 6 节必须当场失守。
    # 需要真文献库 ⇒ 与 kb_e2e 同组，3.13 下跑不了。
    T("kb_recall",    ["kb_recall_test.py"], zero_dep=False,
      landmark="[DONE] kb_recall 全部通过"),
    # 知识库**检索词层**（2026-11-02 加）。kb_recall 量的是"库能查到什么"，
    # 这套量的是"**问句话怎么变成检索词**"——两者是不同层，缺了这层，前一层
    # 全绿照样答不出用户的原话。
    # 起因是一次实测：用户问「核质染色怎么做」，
    #   ① 整句 7 字被当成**一个**检索词（AND 语义下 0 命中）；
    #   ② 更根本：`核质染色` 在 19283 个段落里**0 次** —— 库里没人这么叫，
    #      领域真实用词是 DAPI / 荧光染色 / 细胞核染色。
    # 于是工具回"你的文献库里没有材料"，用户读成"世上没人做过"。
    # 这套守四件事：问句剥壳 / 同义扩展（术语→文献叫法）/ 虚词保护（不许切片
    # 撞出「有关于」那种碎片）/ 零命中归因三态（读不到 ≠ 不符合）。
    # ★ **全离线纯函数**：bridge 顶层只 import 标准库，workbench 是懒导入
    #   ⇒ 两个解释器都跑得动，不需要真文献库。
    # ★ 自带负控（且是**成对**的）：去掉锚 ⇒ 巨噬细胞那批必须抢到第一名。
    T("kb_terms",     ["kb_terms_test.py"],
      landmark="[DONE] kb_terms 全部通过"),
    # PDF → Markdown 转换层（2026-09-25 新增）：文件名消毒 / frontmatter /
    # OCR 建议 / 写 Obsidian（幂等 + 绝不覆盖）/ 正文回传契约。
    # ⚠️ 它也**做真转换**（拿一份真 PDF 跑一遍 pymupdf4llm）⇒ 与 kb_e2e 同组，
    #    3.13 下跑不了。**拿不到真 PDF 或没装依赖时它会 SKIP 并 rc=2**，
    #    绝不把"没跑"报成"通过"（本仓 I 型）。
    T("kb_convert",   ["kb_convert_test.py"], zero_dep=False,
      landmark="[DONE] kb_convert 全部通过"),
    # DOI→PDF 兜底下载器（2026-10-12 新增）：多镜像轮询的契约与页面解析。
    # 为什么单独立一套：litkb 原来"下不到全文"这件事**毫无声响** ——
    # 用户只看到 has_fulltext=False，会以为世上没有开放版，而实测真相是
    # 出版商 403 / 反爬挑战页（见 litkb._try_download_oa_pdf 的注释）。
    # ⚠️ 它**默认关闭**（Sci-Hub 路，边界见 schubdl.py docstring），
    #    测试全离线（假镜像 + 本地 HTTP），不联网、不打外网 ⇒ 两种解释器都跑得动。
    #    自带负控：把"必须校验 %PDF"去掉，挑战页落盘那组判据必须当场失守。
    T("schubdl",      [str(Path.home() / ".dsh" / "papers" / "workbench"
                           / "schubdl_test.py")], zero_dep=False,
      landmark="schubdl_test:"),
    T("selftest",     ["selftest.py"], zero_dep=False,
      counted=False, landmark="[DONE] 四项全过"),
    # LLM 传输层：SSE 聚合（2026-10-10 W27 加）。
    # 为什么必须有它：CodeBuddy 通道（copilot.tencent.com/v2）**只支持流式**，
    # `stream:false` 回 HTTP 400 `code:11101`。于是荣珂第一次有了
    # 「整篇读回再 json.loads」之外的取数路径 —— 而聚合层最容易写坏的两处
    # （tool_calls 分片要**拼**不能覆盖、reasoning 不能混进正文）**都不会报错**，
    # 只会让原生 function-calling 拿到半截参数。这正是本仓最防的静默失败。
    # 自带负控：把拼接改成覆盖，A2 那组判据必须当场失守。
    # 零依赖（纯内存夹具，不联网、不读真 key）⇒ 两种解释器都跑得动。
    T("llm_sse",      ["llm_sse_test.py"], landmark="[DONE] llm_sse"),
    # ── 上下文经济（2026-09-25 加）────────────────────────────
    # 两个文件分工不许互相替代：
    #   `ctx_econ --offline`  管**判据对不对** —— 五条红线各配一份故意做坏的夹具，
    #                          塌不了红就说明那条判据是装饰（本项目对 G 型唯一的解药）。
    #   `context_test`       管**出清做坏了没有** —— 前缀字节等式、role/id 不丢、
    #                          幂等、以及"同一份台账压缩前后 gate.Verdict 逐字节相同"。
    # 为什么 `ctx_econ` 的**默认模式不进这里**：它读 `runs/2*/` 真归档并打「红/绿/NA」，
    #   那是给人看的报告，不是断言；而且**改动前的 19 份归档本来就判红 32 条** ——
    #   把它塞进回归等于用过去的账扣住今天的路（详见它文件头「测不到 ≠ 通过」）。
    T("ctx_econ",     ["ctx_econ.py", "--offline"]),
    T("context",      ["context_test.py"]),
    # 预设装载器（2026-09-26 加，阶段 1 的第一步；2026-11-04 由 caps.py 接活）。
    # 本套件只测「装与验」：validate 的两条主判据（未知键整份拒绝、坏项照样
    # 带理由返回）与「合并之后才校验」。**行为有没有真的换**由下面的
    # `presets_live` 端到端套件负责，两者分开才能定位是装载错还是接线断。
    # 为什么值得单独一套：`validate` 的两条主判据（未知键整份拒绝、坏项照样带理由
    # 返回而不是悄悄滤掉）防的正是本项目反复记的静默失败；而个人覆盖层那条
    # 「list 是覆盖不是追加」是 `_deep_merge` 的形状，不测就只会在半年后被发现。
    # 零依赖成立：只 import `rongke.preset/contract/stages`，不碰 `loop`、不碰
    # workbench 桥，也不写任何真账（临时目录 + `home=` 指临时家目录）。
    T("preset",       ["preset_test.py"]),
    # 能力预设的活链路：装载 + 6 组负控 + 「preset 真的到 kernel.run」。
    # **不是零依赖**：它 import loop / web.server 来打桩做端到端断言。
    T("presets_live", ["presets_live_test.py"], zero_dep=False),

    # 首次使用自检（rongke/setupdoctor.py + `python -m rongke --setup`）。
    # 零依赖成立：只 import `rongke.setupdoctor/config/deck/figure`，不碰 `loop`、
    # 不碰 workbench 桥、不联网、不调模型；所有写操作落临时目录 —— 绝不碰真的
    # `config.json`（那是用户一手资产）与真的 `~/.rongke/`。
    T("setup",        ["setup_test.py"]),

    # 数据分析成图（rongke/analysis.py + `plot_data` 工具）。
    # **不是零依赖**：端到端那段要真的起一个解释器画图（matplotlib + pandas），
    # 还要跑 nature-figure 的六道门 —— 所以照 `ui_shot` 的先例标 zero_dep=False。
    # 写盘全部落 tempfile 临时目录，跑完自毁；不碰真配置、不联网、不调模型。
    # 它自己的 [8] 节做负控（把 `_group_sizes` 换成假实现，看断言是否翻红），
    # 所以这条不只是"跑通了"，而是"判据打在真东西上"。
    T("analysis",     ["analysis_test.py"], zero_dep=False),
    # 实验设计三件套（experimental-design / statistical-power /
    # uncertainty-and-units，装在 vendor/skills 随产品分发）。
    # 它测的是**数字对不对**：5 个教科书算例（t 检验 n=64、相关 n=85、
    # 全因子 8 run、Box-Behnken 13、u_c=0.4997）。
    # 为什么必须测数字而不测「文件在不在」：算样本量的技能如果算错，
    # 比没有更糟 —— 用户会拿这个数去写基金、报伦理审查。
    # 依赖 statsmodels + pyDOE3（本机已装），所以 zero_dep=False。
    T("experimental_design", ["experimental_design_test.py"], zero_dep=False),
    # 打包与数据根：**纯静态检查**，不装包、不联网、不起子进程建 venv。
    # 它们守的是「别人 pip install 之后能不能用」这件事实 ——
    # 源码树里 data_root()==project_root() 这条不变量一旦被改坏，
    # 装成依赖的用户数据就会写进 site-packages（升级即丢）。
    # 真装一遍的验证在 `ci_local.py`（要建 venv，太慢，不进 regress）。
    T("packaging",    ["packaging_test.py"], zero_dep=False),
    T("pathing",      ["pathing_test.py"], zero_dep=False),
    # 插件系统：**纯内存操作**（临时目录里造插件文件 → 加载 → 查注册表），
    # 不联网、不起子进程、不调模型。图型那条只登记元数据不做图，
    # 真出图的验证在 `analysis` 套件与 `runs/_an/e2e_plugin_*.py`。
    T("plugins",      ["plugins_test.py"], zero_dep=False),
    # 文档示例可跑：把 docs/插件开发.md 里的代码抽出来真加载一遍。
    # 存在的理由——文档里的示例跑不通，文档就是假的。
    T("docs",         ["docs_test.py"], zero_dep=False),
    # 本地账单：**纯内存 + 临时目录**，不联网、不调模型、不接支付。
    # 重点判据是「UNKNOWN 消耗不许伪装成 0」（余额会静默永不减少）
    # 与「余额必须从流水算出」（缓存字段是漂移来源）。
    T("billing",      ["billing_test.py"], zero_dep=False),
    # 登录门禁：服务端 401 判据 + 真浏览器里门真的关上又打开。
    # 为什么两层都要：服务端那一层证不了"屏幕上有没有弹出登录层"，
    # 而浏览器那一层抓到了一个服务端测试永远看不见的真 bug ——
    # 服务端认令牌，但前端那个自检请求用裸 fetch 没带令牌，
    # 于是**每次刷新都要求重新登录**。静态检查也看不见它。
    T("login",        ["login_test.py"], zero_dep=False),
    # 授权（离线激活码）。含 Ed25519 对 RFC 8032 官方向量的自证 ——
    # 自写的密码学「看起来对」是最危险的状态，必须机械证伪。
    T("license",      ["license_gate_test.py"], zero_dep=False),
    # 订阅 v2 授权链：老内核零改动认新码、起算点、幂等、续期无空窗、双台账对账。
    # 全部落在 tools/ 与测试侧，不改 rongke/*.py；测试密钥临时生成并 try/finally 还原。
    T("license-v2",   ["license_v2_test.py"]),
    # 同一条链的另一层：命令行参数解析 / 退出码 / 跨工具交接 / sdist 清单自洽。
    # 缺发行私钥时明确 SKIP 且不计入通过数，不把「没测到」伪装成绿。
    T("license-v2-cli", ["license_v2_cli_test.py"]),
    # 产品说明与代码对账：文档里的数字（工具数/域数/图型数/价格/额度）
    # 必须与代码算出来的对得上。数字是最容易腐坏的东西，而没人会去数 ——
    # 实测已经抓到一次（我把"10 种图型"写成内置值，内置其实是 9 种）。
    T("product-doc",  ["product_doc_test.py"], zero_dep=False),
    # 版本号唯一真源 + CHANGELOG 的空承诺守卫。
    # 这条之所以存在，是因为 CHANGELOG 里写着「changelog_test.py 会机械核对」，
    # 而那个文件当时**并不存在** —— 一句没人执行的承诺。
    T("changelog",    ["changelog_test.py"], zero_dep=False),
    # 账单区块**真的上屏了吗**：真实浏览器渲染后读可见文本。
    # 不能由 `ui_check` 代劳 —— 那套静态检查判的是"页面里有没有这段代码"，
    # 而这里要验的恰恰是它看不见的那一半（loadBilling 是异步的，它可能
    # 404、可能抛错、可能把区块留成空壳，静态检查全绿）。
    # 要 playwright，所以 zero_dep=False；没装时脚本自己跳过并返回 0。
    T("billing-ui",   ["billing_ui_test.py"], zero_dep=False),
    T("hero-scroll",  ["hero_scroll_test.py"], zero_dep=False),
    # 文献库列表/按页读取的**边界条件**：默认值、筛选、翻页边界、source 三档、
    # 排序稳定性、非法入参、读取窗口收窄/越界/缺全文/工具失败。
    # 为什么不让 `kb_e2e_test` 代劳：那一套要真起服务、真入库 PDF，而这里要验的
    # 是"工具回 ok:false 时列表会不会冒充空库"这类 —— 在真库上造不出来（造它
    # 需要先把库弄坏）。所以这一套完全 mock，zero_dep=True。
    T("library-api",  ["library_api_test.py"]),
    T("library-ui",   ["library_ui_test.py"]),
    T("library-negctl", ["library_api_negctl_test.py"], zero_dep=False),
    T("topic-routes", ["topic_route_test.py"]),
    T("lab-routes",   ["lab_route_test.py"]),
    T("topic-library", ["topic_library_test.py"]),
    T("topic-library-http", ["_rev_probe_api.py"]),
    T("dialogue-thinking", ["dialogue_thinking_test.py"]),
    T("figure-handoff", ["figure_handoff_test.py"]),
    T("figure-route", ["figure_route_test.py"]),
    T("topic-workflow", ["topic_workflow_test.py"]),
    T("mentor-workspace", ["mentor_workspace_test.py"]),
    T("subagent", ["subagent_test.py"]),
]

UI_SHOT = T("ui_shot", ["ui_shot.py"], counted=False, zero_dep=False,
            landmark="[DONE] 共")

# 标记必须在**行首**（允许缩进）：汇总行（`  全部通过 ✔`）不该被算成一条断言；
# 而 `fence_test` 的 `[PASS] …` 顶格写，也要认。
# 两种风格都认：`[PASS] xxx` 与 `✔ xxx`（figure_test 用后者）。
_RE_MARK = re.compile(r"^[ \t]*(?:\[(PASS|FAIL|OK|BAD)\]|(✔|✘))", re.M)

# 环境级"批量删除确认"守卫的标记（见 `_delete_probe`）。
# 它出现在**子进程的 stderr** 里 —— 而这批测试的 stderr 被合并进日志，
# 所以日志里留着凶手的指纹，只差有人去认。
_RE_DELGUARD = re.compile(r"\[safe-delete\]\[SAFE_DELETE_BULK_(\w+)\]")


def _syntax_gate() -> list[str]:
    """开工前把**要跑的脚本 + 整个包**编译一遍。返回语法错误列表。

    ★ 为什么要有：我为它栽过一次。删掉一个 `finally:` 之后**没跑 py_compile**
      就上了全套回归，于是 18 项里有两项以「rc≠0 / 0 项断言 / 0.2 秒」的样子死掉，
      看日志才知道是 `SyntaxError: expected 'except' or 'finally' block`。
      一个 0.3 秒的编译能省掉一次 60 秒的误判 —— 而且误判的方向最坏：
      它会让人以为"测试挂了"，去查那几百行测试。
    """
    files = sorted((ROOT / "rongke").rglob("*.py"))
    files += [ROOT / t["cmd"][0] for t in TESTS] + [ROOT / UI_SHOT["cmd"][0]]
    files.append(ROOT / "_tkit.py")     # **每个**测试都 import 它 —— 它坏了是全线崩
    bad, seen = [], set()
    for p in files:
        if p in seen or "__pycache__" in p.parts or p.name.startswith("_probe"):
            continue
        seen.add(p)
        try:
            # 用 `compile` 而不是 `py_compile`：只判语法，**不写 .pyc**、零副作用。
            compile(p.read_text(encoding="utf-8", errors="replace"), str(p), "exec")
        except SyntaxError as e:
            bad.append(f"{p.relative_to(ROOT)}:{e.lineno}  {e.msg}")
        except OSError as e:
            bad.append(f"{p.relative_to(ROOT)}  读不到：{type(e).__name__}")
    bad += _name_gate(seen)
    return bad


def _name_gate(files) -> list[str]:
    """抓「用了某个标准库模块名、却没 import 它」—— 那是 `NameError`，`compile()` 抓不到。

    ★ 为什么要有这道闸：**本轮我为它栽了一次，而且是在同一个坑里第二次栽。**
      改 `ui_check.py` 时我按 `shutil` 这个**关键字**搜了一遍全仓，看到的两处
      （import 行 + 一处 `rmtree`）都处理了，就删掉了 `import shutil` ——
      而 `find_npm_node()` 里还藏着一处 `shutil.which`。
      `py_compile` 全过、`regress.py` 的语法闸也全过，**跑起来才 `NameError`**，
      于是 `ui_check` 以「rc=1 / 0 项 / 0.4 秒」的样子红掉。
      同一轮里我刚写下「关键字扫不出全部删除点」—— 说明**光靠记性没用，得让机器查**。

    判据刻意**只查标准库模块名**（`sys.stdlib_module_names`）：误报极低 ——
    `os` / `shutil` / `json` 这些几乎不会当普通变量名用。**宁可窄而准，不要宽而吵**：
    吵的闸门会被人关掉，关掉就等于没有。
    """
    import ast
    import builtins
    std = set(getattr(sys, "stdlib_module_names", ())) or set()
    if not std:                     # 3.9 及更早没有这张表 —— 那就如实不查，别硬猜
        return []
    out: list[str] = []
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"), str(p))
        except (SyntaxError, OSError):
            continue                # 语法错归 `_syntax_gate` 管，不在这儿重复报
        bound = set(dir(builtins))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    bound.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    bound.add(a.asname or a.name)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                bound.add(n.id)     # 任何赋值都算绑定（宁可漏报，不可误报）
            elif isinstance(n, (ast.arg,)):
                bound.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                bound.update(n.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for nm in sorted((used & std) - bound):
            out.append(f"{p.relative_to(ROOT)}  用了 `{nm}` 却没 import —— "
                       f"`compile` 查不出，跑起来必 NameError")
    return out


def _delete_probe(py: str) -> str:
    """在**子进程**里试删一个自己刚建的文件。返回 "" = 这一轮还删得动。

    ★ 为什么要有这道前置：本环境有一道"批量删除确认"守卫，**轮内累计删够阈值就拦**。
      被拦时**子进程会当场消失**：rc≠0、日志截断、连 traceback 都没有。
      实测长相是「6 个测试变红，却 0 项断言失败，断言数腰斩」——
      这种红**极难归因**，会把人送去逐行查测试代码，而真凶在环境里。
      宁可开工前先问一句，也别让 18 个测试跑完再甩 6 个哑谜。

    ★ 为什么放子进程：被拦时进程可能直接没了。在主进程里试 = 拿运行器当祭品。
    """
    code = ("import pathlib\n"
            "p = pathlib.Path('runs/_regress/_delprobe.txt')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "p.write_text('x', encoding='utf-8')\n"
            "p.unlink()\n"
            "print('DELOK')\n")
    try:
        r = subprocess.run([py, "-c", code], cwd=str(ROOT), capture_output=True,
                           text=True, timeout=30)
    except Exception as e:                                  # noqa: BLE001
        return f"删除能力探针跑不起来（{type(e).__name__}: {e}）"
    if "DELOK" in (r.stdout or ""):
        return ""
    lines = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
    return (f"子进程删不掉自己刚建的文件（rc={r.returncode}）"
            + (f"；它最后说：{lines[-1][:160]}" if lines else ""))


def _scan(out: str) -> tuple[int, int]:
    good = bad = 0
    for m in _RE_MARK.finditer(out):
        if (m.group(1) or "") in ("PASS", "OK") or m.group(2) == "✔":
            good += 1
        else:
            bad += 1
    return good, bad


# ── 热控制台（RONGKE_WEB）真接线 ─────────────────────────────────────────
# 2026-11-05 final13 实测：billing_ui_test.py 注释里写着「全套回归里已经有一台
# 热控制台（RONGKE_WEB）」，但运行器**从来没设过这个变量** ⇒ 它每次自起冷台，
# /api/setup 首次要加载整条依赖链，60s 内账单区块还没上屏 ⇒ 4 条「元素不存在」；
# ui_shot.py --self-test 则默认连 8791 那台**早于源码落盘**启动的控制台 ⇒
# 2 条代码指纹漂移。两处都是「配置在场 ≠ 已生效」，不是产品红。
# ⇒ 运行器自己起一台**当前代码**的临时控制台，把 URL 交给所有子进程。
_HOT_WEB = {"url": "", "proc": None}


def _start_hot_web(py: str) -> str:
    """起一台当前代码的控制台；返回 URL，起不来如实返回空串（不静默）。"""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    finally:
        s.close()
    url = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(
        [py, "-m", "rongke", "--web", "--port", str(port)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env=env)
    # 环境里有 HTTP_PROXY，连 127.0.0.1 也会被拦 —— 显式绕开。
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    end = time.time() + 90
    while time.time() < end:
        try:
            with op.open(url + "/api/build", timeout=2) as r:
                json.loads(r.read().decode("utf-8", "replace"))
            _HOT_WEB["proc"] = proc
            print(f"  · 热控制台已就绪 {url}（当前代码，供 UI 判据复用）")
            return url
        except Exception:                                      # noqa: BLE001
            if proc.poll() is not None:
                break
            time.sleep(0.4)
    proc.kill()
    print("  ⚠️ 热控制台没起来 ⇒ UI 判据会退回冷启动/旧控制台，本轮结果按红处理")
    return ""


def _stop_hot_web() -> None:
    proc = _HOT_WEB.get("proc")
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=8)
        except Exception:                                      # noqa: BLE001
            pass
    _HOT_WEB["proc"] = None
    _HOT_WEB["url"] = ""


def _child_env() -> dict:
    """给被测脚本一份**钉死**的输出编码。

    ★ 2026-09-26 实测：本机控制台是 cp936，而 `_run` 把子进程 stdout 重定向到
      日志文件 —— 子进程的 `sys.stdout.encoding` 于是随**调用者的 shell** 漂。
      同一份代码、同一份配置，不带 `PYTHONIOENCODING` 起 ⇒ 10 项里 8 项在打印
      `✔ / ⇒` 时抛 UnicodeEncodeError 死在半路，回归把它们报成「0 项 / 0 失败 +
      rc=1」。断言数骤降的告警抓住了它（这次没被无声吃掉），但判据本身不该依赖
      调用姿势：判决工具因为编码而挂，和被测对象挂掉同样糟，而且更难归因。
      调用者已显式设过就尊重调用者（不覆盖），否则补 utf-8。
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # ★ 调用者显式指定过热台就尊重调用者；否则把本轮**真起了**的那台交下去。
    #   只写 URL 不真起服务 = 配置在场 ≠ 已生效，正是 final13 的病根。
    if _HOT_WEB["url"]:
        env.setdefault("RONGKE_WEB", _HOT_WEB["url"])
    return env


def _run(py: str, argv: list[str], log: Path) -> tuple[int, str]:
    """跑一个脚本：rc 直取，输出落盘（重定向到文件，绝不走管道）。"""
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with log.open("w", encoding="utf-8", errors="replace") as fh:
        p = subprocess.run([py, *argv], cwd=str(ROOT), stdout=fh,
                           stderr=subprocess.STDOUT, env=_child_env())
    return p.returncode, f"{time.time() - t0:.1f}s"


def _self_test() -> int:
    """本运行器自己的正控 / 负控。

    它是**判决工具**：别的测试红了会有人管，它红了自己却报"全绿" —— 最难被发现。
    所以两件事必须当场验：① 标记扫描认得出什么、不认什么；② 退出码**原样**送达。
    ② 是原始事故（`| tail -3; echo $?` 恒 0）的正面控制。
    """
    print("── regress.py 自检 ──")
    bad = 0
    for name, text, want in [
        ("普通断言行", "  [PASS] 甲\n  [FAIL] 乙\n", (1, 1)),
        ("顶格标记（fence_test 的写法）", "[PASS] 四条行为全部符合设计\n", (1, 0)),
        ("figure_test 的 ✔/✘", "  ✔ 甲\n  ✘ 乙\n", (1, 1)),
        ("汇总行不算断言（防虚高）", "  全部通过 ✔\n", (0, 0)),
        ("汇总失败行也不算", "  有失败 ✘\n", (0, 0)),
        ("空输出", "", (0, 0)),
    ]:
        got = _scan(text)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] 扫描：{name}")
        if not ok:
            print(f"         期望 {want} 实得 {got}")

    # 端到端：退出码必须原样送达（文件名带 pid —— 两个进程同时跑不能互删）
    LOGDIR.mkdir(parents=True, exist_ok=True)
    probe = LOGDIR / f"_rcprobe_{os.getpid()}.py"
    try:
        for want_rc in (0, 3):
            probe.write_text(f"import sys\nsys.exit({want_rc})\n", encoding="utf-8")
            rc, _ = _run(sys.executable, [str(probe)],
                         LOGDIR / f"_rcprobe_{os.getpid()}.log")
            ok = rc == want_rc
            bad += 0 if ok else 1
            print(f"  [{'PASS' if ok else 'FAIL'}] 退出码原样送达：期望 {want_rc} 实得 {rc}")

        # 编码承重探针：让子进程往**文件**里打一个 cp936 装不下的字符。
        # 把 `_child_env()` 那行 setdefault 删掉 ⇒ 本探针立刻红。它不是装饰，
        # 是那条 setdefault 的**存在性证明**（机制在却永不触发 = G 型缺陷）。
        probe.write_text('print("✔")\n', encoding="utf-8")
        rc, _ = _run(sys.executable, [str(probe)],
                     LOGDIR / f"_rcprobe_{os.getpid()}.log")
        ok = rc == 0
        bad += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] 子进程输出编码不随调用姿势漂"
              f"（打 cp936 外字符仍要 rc=0，实得 rc={rc}）")
    finally:
        # 挪走而不是删：这台机器按**条目数**收删除费（见 `_tkit`），
        # 而运行器自检不该参与那笔账。落点固定名 ⇒ 覆盖，不堆积。
        os.replace(probe, LOGDIR / "_rcprobe.py")

    print("  自检全过" if not bad else f"  自检有 {bad} 项失败")
    return 0 if not bad else 1


def _report_sweep(moved, when: str) -> None:
    """报告一次影子副本清扫的收获 —— **两类要分开说**。

    ★ 第一版我把它们一律说成"这一轮被中断的测试留下的"，日志当场打脸：
      正常收尾也会在落点留下**固定名**副本（那是 `finally` 里的停车位，故意的）。
      只有**带 pid 后缀**的才是"没走到终点就被杀"的证据。
    ⇒ 白捡的诊断：**垃圾里写着事故报告**，别一扫了之。
    """
    if not moved:
        return
    killed, parked = TK.killed_leftovers(moved)
    if killed:
        print(f"⚠️ {when}挪走 {len(killed)} 份**带 pid 的**影子副本 —— "
              f"有测试**没走到终点就被杀**（不是正常结束）：")
        for p in killed:
            print(f"     · {p.relative_to(ROOT)}")
    if parked:
        print(f"   {when}另挪走 {len(parked)} 份固定名副本"
              f"（停车位 → 回收站，**挪不是删**）："
              + "、".join(p.name for p in parked))


def _trash_report() -> int:
    """`_trash` 的规模，以及**清空它要花多少点**。

    ★ 两件事必须**分开**报：占多少磁盘、删起来多贵。合成一个数就是两副面孔。
    """
    st = TK.trash_stats()
    rows = TK.ledger_rows()
    print("── 测试回收站（`runs/_trash`）──")
    if st["n_items"] == 0:
        print("  空的 —— 这一轮所有清场都是「挪」，一件没删。")
    else:
        print(f"  {st['n_items']} 项 · {TK.human(st['bytes'])} · "
              f"清空它要花 **{st['points']} 点**删除额度（一轮只有 50 点）")
    print(f"  台账：{len(rows)} 次清理从「删」改成了「挪」，累计省下 "
          f"{TK.ledger_total()} 点")
    # ★ 「报出代价」还不够 —— 代价**高到付不起**的时候必须说清楚该怎么办。
    #   实测：`_trash` 409 项 / 199MB ⇒ 清空要约 **3902 点 = 78 轮**额度。
    #   即 `--purge-trash` **永远清不完它**。不说这一句，那句"先报代价"就是把人
    #   引去做一件做不到的事（同族毛病：报了个数却不给读法）。
    turns = st["points"] / 50 if st["points"] else 0
    if turns > 2:
        print()
        print(f"⚠️  这**不是一轮能清完的**：约要 {turns:.0f} 轮额度。别硬清 ——")
        print("    **手工删掉 `runs/_trash` 整个目录**才是对的（资源管理器里删即可，"
              "不走 agent 工具 ⇒ 不记账）。")
        print("    安全：`_trash` 是设计好的**停车位**，里面只有被挪走的测试夹具与")
        print("    影子副本，**没有任何任务归档**（归档在 `runs/<id>/`，台账在 `_regress/`）。")
        print("    「挪」的代价只是磁盘，不是额度 —— 这两件事本来就不该混着看。")
    else:
        print("  要清就显式跑：`python regress.py --purge-trash`"
              "（先报代价，再加 `--yes` 动手）")
    return 0


def _purge_trash(assume_yes: bool) -> int:
    """**本项目唯一会真删的地方。**

    ★ 为什么把它做成一条**由人触发的单独命令**，而不是让测试顺手清：
      删除额度按**轮对话**发（50 点），而跑一轮全套产生的临时文件远超 50 项。
      让测试自动清 ⇒ 必然撞线 ⇒ 长会话里回归永远跑不全绿
      （本会话开头就卡在这个状态：13 绿 + 5 项「环境拦删」）。
      让人在**额度干净的轮次**里显式决定 ⇒ 这笔账才有得商量。

      **把删除从「测试的自动行为」降级成「人的显式行为」，是 `_tkit` 一半的价值。**
    """
    st = TK.trash_stats()
    if st["n_items"] == 0:
        print("`_trash` 已经是空的，没什么可清。")
        return 0
    print(f"`runs/_trash`：{st['n_items']} 项 · {TK.human(st['bytes'])}")
    print(f"⚠️  清空它要花 **{st['points']} 点**删除额度（一轮 50 点）。")
    print("    里面全是测试自己攒的临时夹具（外加 `web_test` 带过来的一个目录联接）；"
          "没有一份是用户的稿子。但**它们会被真的删掉**。")
    if not assume_yes:
        print("\n    默认**不动手** —— 确认后加 `--yes` 再跑。")
        return 0
    n, cost = TK.purge_all()
    print(f"\n  已清 {n} 项 · 按守卫口径约花 {cost} 点")
    print(f"  剩余 {TK.trash_stats()['n_items']} 项"
          "（若期间又跑过测试，那几项是新攒的）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="荣珂全套回归")
    ap.add_argument("--only", default="", help="只跑这些（逗号分隔标签）")
    ap.add_argument("--py313", action="store_true", help="换托管 3.13 跑零依赖子集")
    ap.add_argument("--save", action="store_true", help="把这轮计数存成基线")
    ap.add_argument("--self-test", action="store_true",
                    help="只做本运行器自己的正控/负控")
    ap.add_argument("--with-ui-shot", action="store_true",
                    help="附带真实浏览器截图（需先起控制台）")
    ap.add_argument("--force", action="store_true",
                    help="明知本轮删除额度可能已耗尽也照跑（会出现若干「环境拦删」的红）")
    ap.add_argument("--trash-report", action="store_true",
                    help="看测试回收站的规模，以及清空它要花多少删除额度")
    ap.add_argument("--purge-trash", action="store_true",
                    help="清空测试回收站（**本项目唯一真删的地方**，先报代价再动手）")
    ap.add_argument("--yes", action="store_true", help="--purge-trash 的确认开关")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if args.trash_report:
        return _trash_report()
    if args.purge_trash:
        return _purge_trash(args.yes)

    py = PY313 if args.py313 else PY312
    if not Path(py).is_file():
        print(f"✘ 解释器不存在：{py}\n  （用环境变量 RK_PY312 / RK_PY313 覆盖）")
        return 2

    want = {s.strip() for s in args.only.split(",") if s.strip()}
    tests = [t for t in TESTS if not want or t["id"] in want]
    if args.py313:
        tests = [t for t in tests if t["zero_dep"]]
    if args.with_ui_shot:
        tests.append(UI_SHOT)
    if want:
        missing = want - {t["id"] for t in tests}
        if missing:
            print(f"✘ 没有这些标签：{', '.join(sorted(missing))}")
            return 2

    LOGDIR.mkdir(parents=True, exist_ok=True)
    print(f"═══ 荣珂全套回归 · {len(tests)} 项 · 解释器 {Path(py).name} "
          f"({Path(py).parts[-3] if len(Path(py).parts) > 3 else ''}) ═══")

    # 清扫：把上一轮**被杀掉**时留下的影子副本挪出产品包（挪，零计费）。
    # ★ 为什么要在运行器这一层做：短路实验的副本是"含故意缺陷、可被 import"的
    #   产品代码副本，进程被额度守卫杀掉时 `finally` 不跑 ⇒ 它就留在包里了
    #   （实测堆了 27 个）。放在语法闸**之前**还有个好处：这一轮要编译的就是干净的树。
    _report_sweep(TK.sweep_probes(verbose=False), "开工前")

    # 语法闸：先把要跑的脚本和整个包编译一遍。过不去就别浪费 60 秒。
    syn = _syntax_gate()
    if syn:
        print("✘ 语法闸没过（先编译再跑，别让 SyntaxError 伪装成「测试挂了」）：")
        for line in syn:
            print("   · " + line)
        return 2

    # ★ 语法闸过了再起热台：语法都没过就别白起一台服务。
    #   调用者显式给过 RONGKE_WEB 就尊重调用者（可能是他自己起的热台）。
    if not os.environ.get("RONGKE_WEB"):
        _HOT_WEB["url"] = _start_hot_web(py)
        atexit.register(_stop_hot_web)

    # 开工前先问一句"这一轮还删得动吗"。
    # ★ 只**警告**不拒跑：拒跑会把 12 个能过的信号一起挡掉，而拦删的红现在
    #   已经能逐项归因（见下面的 `环境拦删`），是可读的、不是哑谜。
    #   探针本身也只用一次删除，且被拦时**不计费**。
    if not args.force:
        why = _delete_probe(py)
        if why:
            print()
            print("⚠️  前置体检：这一轮的删除额度已用完 —— " + why)
            print("    本环境按**轮次**计数，累计删够阈值后，凡未被单独批准的删除一律拦截；")
            print("    被拦的子进程会**当场消失**（rc≠0、日志截断、没有 traceback）。")
            print("    所以下面会有若干项标成「环境拦删」：**那不是测试挂了**，"
                  "是环境的删除守卫拦的。")
            print("    验证办法：`--only` 单独跑那几项，或换一轮再跑。")
            print()

    old: dict = {}
    if BASELINE.is_file():
        try:
            old = json.loads(BASELINE.read_text(encoding="utf-8"))
        except Exception:
            old = {}

    rows, now, bad_rc, empty, no_land, dropped = [], {}, [], [], [], []
    blocked: list[str] = []
    for t in tests:
        label = t["id"]
        rc, took = _run(py, t["cmd"], LOGDIR / f"{label}.log")
        out = (LOGDIR / f"{label}.log").read_text(encoding="utf-8", errors="replace")
        good, bad = _scan(out) if t["counted"] else (0, 0)

        flag = "ok"
        if rc != 0:
            # 先认死因，再报红。`RC!=0` + 0 失败 + 断言数腰斩 是**环境拦删**的签名，
            # 不是测试写完蛋了 —— 不分开报，人就会去查错的那些 187 行测试。
            if _RE_DELGUARD.search(out):
                flag = "环境拦删"
                blocked.append(label)
            else:
                flag = "RC!=0"
            bad_rc.append(label)
        elif t["counted"] and good == 0:
            # 最坏的一种绿：跑了、没报错、但一条断言都没有
            flag = "空跑?"
            empty.append(label)
        elif t["landmark"] and t["landmark"] not in out:
            # 没打标记的脚本靠这句收尾话兜底：缺了它 = 没跑到终点
            flag = "无地标?"
            no_land.append(label)
        print(f"  {label:<14} rc={rc:<3} {good:>4} 项 / {bad} 失败  {took:>7}  {flag}")

        if t["counted"]:
            now[label] = good
            if label in old and good < old[label]:
                dropped.append((label, old[label], good))

        rows.append((label, rc, good, bad, took))

    print()
    if dropped:
        print("⚠️ 断言数比基线**少了**（删断言让红变绿，是测试最容易被无声腐蚀的方式）：")
        for label, was, is_now in dropped:
            print(f"    {label}: {was} → {is_now}  （确认是有意删的，再 --save 覆盖基线）")
    # ★★ `rc≠0` 且 **0 项断言失败** —— 这个组合只有三种已知成因，**别去查那几百行测试**。
    #    2026-09-21 实录：`web` 128→114，日志是客户端的
    #    `ConnectionAbortedError: [WinError 10053]`。
    #    ① 环境级删除守卫拦删（**有** `[safe-delete]` 指纹）；② 测试自带服务的
    #    **传输层偶发中止**（**没有**指纹，单跑必绿）；③ 套件**崩在半路**
    #    （有 traceback ⇒ 那才是真 bug，去读 traceback）。
    #    ⇒ 把这三条印出来：**红指错方向的代价是"查一小时测试，而根因在别处"。**
    if any(bad == 0 for _, rc, _, bad, _ in rows if rc != 0):
        print("⚠️ 上面有 rc≠0 但 **0 项断言失败** 的项 —— 这个组合只有三种成因，"
              "**先别去查测试代码**：")
        print("   ① 环境级删除守卫拦删：日志里有 `[safe-delete][SAFE_DELETE_BULK_*]` 指纹")
        print("   ② 传输层偶发中止（如 `WinError 10053`）：**没有**指纹、**单跑必绿**"
              " ⇒ `--only <该套件>` 复验一次")
        print("   ③ 套件崩在半路：日志尾巴有 traceback ⇒ 那才是真 bug，读 traceback")
    if empty:
        print(f"⚠️ 空跑（rc=0 但一条断言都没扫到）：{', '.join(empty)}")
    if no_land:
        for label in no_land:
            lm = next(t["landmark"] for t in tests if t["id"] == label)
            print(f"⚠️ {label}：rc=0 但没看到收尾地标「{lm}」—— 可能没跑到终点")
    if blocked:
        print(f"⚠️ 这几项**不是断言失败，是环境把它的删除拦了**：{', '.join(blocked)}")
        print("   签名：rc≠0 + 0 项断言失败 + 断言数腰斩；日志里有 "
              "[safe-delete][SAFE_DELETE_BULK_*]。")
        print("   根因：本环境按**轮次**给删除额度，累计删够阈值后不再放行。"
              "开新一轮重跑即可（别去改测试）。")
    if bad_rc:
        print(f"✘ 非零退出：{', '.join(bad_rc)}")
    total = sum(r[2] for r in rows)
    print(f"合计 {total} 项 / {sum(r[3] for r in rows)} 失败；"
          f"日志在 {LOGDIR.relative_to(ROOT)}/")

    # 收尾再扫一遍：开工前那次扫的是"上一轮的残留"，这次扫的是"这一轮留下的"。
    _report_sweep(TK.sweep_probes(verbose=False), "收尾时")

    if args.save:
        if bad_rc or empty or no_land:
            print("✘ 有红，拒绝存基线（基线只能是全绿的那一轮）")
            return 2
        BASELINE.write_text(json.dumps(now, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"✔ 基线已存（{len(now)} 个脚本的断言数）")

    if bad_rc:
        return 1
    if empty or no_land or dropped:
        # 0 退出留给"真全绿"：有可疑就非零，否则可疑迟早被当成绿
        return 2
    print("═══ 全部通过 ✔ ═══")
    return 0


if __name__ == "__main__":
    sys.exit(main())
