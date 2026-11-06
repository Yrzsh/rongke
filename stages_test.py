# -*- coding: utf-8 -*-
"""技能装载（rongke/stages.py 的 `file` 参数）单测 —— 纯文件系统，不需要 LLM、网络。

覆盖 2026-09-18 实测挖出的那个缺口：**技能的 `SKILL.md` 指示模型"按需打开
`references/` 下的文件"，而内核没有任何工具能读它们** ⇒ 模型去猜路径
（实测猜了 `nature-writing/core/stance.md` 与 `nature-introduction`，连着报「找不到技能」）。

  1. 入口文档       —— `file` 为空读 `SKILL.md`（正控：与磁盘内容逐字一致）
  2. 按需读参考     —— 给相对路径读得到，且带**溯源头**（模型要知道自己在读哪个文件）
  3. 路径写错       —— 必须报错**并列出该技能下的可用文件**（正控：原来只倒整机技能名）
  4. ★ 目录穿越     —— 三种绕法全部拒绝（安全负控）
  5. 内核原生技能   —— 没有磁盘目录，带 `file` 必须明确拒绝而不是静默给正文
  6. 承重自检       —— 把"在不在目录内"的谓词换成恒真、把文件清单关掉，
                       对应的正控/负控必须**当场失守**

一条刻意的记法：**每个断言都成对出现**。只有正控 = 可能过的是别的原因；
只有负控 = 可能这条检查根本没在干活。本项目实测过这两种翻车。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import stages as _S                                  # noqa: E402
from rongke.stages import read_skill_text, skill_files            # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def has(label, got, needle):
    check(label, needle in got, True)


# 找一个"有参考文件的磁盘技能"当样本；找不到就整节跳过（不同机器装的东西不一样）。
CANDIDATES = ("nature-writing", "nature-polishing", "nature-figure")
SKILL = ""
for c in CANDIDATES:
    if _S.resolve(c) is not None and len(skill_files(c)) > 1:
        SKILL = c
        break

if not SKILL:
    print("  [SKIP] 本机没有带参考文件的磁盘技能，跳过（不是失败）")
    print()
    print("═══ 汇总 ═══")
    print("  全部通过 ✔（已跳过）")
    sys.exit(0)

root = _S.resolve(SKILL).parent
print(f"样本技能：{SKILL}（{len(skill_files(SKILL))} 个可读文件）于 {root}")

print("\n── 1. 入口文档：file 为空 ──")
entry = read_skill_text(SKILL)
on_disk = (root / "SKILL.md").read_text(encoding="utf-8", errors="replace")
check("`file` 为空 ⇒ 读到的就是 SKILL.md 本身（正控：逐字一致）", entry, on_disk)
has("入口文档带 frontmatter", entry, "---")

print("\n── 2. 按需读参考文件 ──")
refs = [f for f in skill_files(SKILL) if f.startswith("references/")]
if not refs:
    print("  [SKIP] 这个技能没有 references/ 下的文件")
else:
    rf = refs[0]
    got = read_skill_text(SKILL, file=rf)
    check("读得到（正控）", got.startswith("⛔"), False)
    has("★ 带溯源头（模型要知道自己在读哪个文件）", got, f"（{SKILL}/{rf}）")
    real = (root / rf).read_text(encoding="utf-8", errors="replace")
    check("正文与磁盘逐字一致（不是二次加工）", got.split("\n", 1)[1], real)
    check("★ 与入口文档不是同一份内容", got != entry, True)

print("\n── 3. 路径写错：必须报错 **并列出可用文件** ──")
bad = read_skill_text(SKILL, file="core/stance.md")
has("报错（正控）", bad, "⛔")
has("★ 列出该技能下的可用文件（原来只倒整机技能名，答非所问）", bad, "可用文件")
n_listed = sum(1 for line in bad.splitlines() if line.strip().startswith("- "))
check("★ 列出的文件数 > 1（确实在列文件，不是空话）", n_listed > 1, True)
has("★ 真的提到那个技能的名字（提示要答在点子上）", bad, SKILL)

print("\n── 4. ★ 目录穿越：三种绕法全部拒绝（安全负控）──")
for evil in ("../../../../etc/passwd",
             "..\\..\\secrets.txt",
             "references/../../SKILL.md",
             "/etc/hosts",
             "..%2f..%2fsecrets"):
    got = read_skill_text(SKILL, file=evil)
    check(f"{evil!r} → 拒绝", got.startswith("⛔"), True)
# 反面对照：**合法**的相对路径不许被误拒（否则安全措施会误伤正常用法）
if refs:
    okcall = read_skill_text(SKILL, file=refs[0])
    check("合法相对路径不被误拒（负控：安全措施不许误伤正常用法）",
          okcall.startswith("⛔"), False)

print("\n── 5. 内核原生技能：没有磁盘目录 ──")
kernel = "workbench-paper-pitfalls"
if kernel in _S.available():
    has("带 file 时明确拒绝", read_skill_text(kernel, file="x.md"), "内核原生")
    # 负控：不带 file 时必须正常给正文，别把它一起拦了
    check("★ 不带 file 时**不**以 ⛔ 开头（负控：别把它一起拦了）",
          read_skill_text(kernel).startswith("⛔"), False)
    check("不带 file 时确实有正文", len(read_skill_text(kernel)) > 500, True)
    check("skill_files(内核原生) 为空表（没有目录可列）", skill_files(kernel), [])
else:
    print(f"  [SKIP] 本机没有内核原生技能 {kernel!r}")

print("\n── 6. 边界：空文件名 / 指向目录 ──")
check("file='' 等同于不给 file", read_skill_text(SKILL, file=""), entry)
has("file 全是斜杠 → 当成空", read_skill_text(SKILL, file="///"), "⛔")
check("file 指向目录（references）→ 报错不崩",
      read_skill_text(SKILL, file="references").startswith("⛔"), True)

print("\n── 7. 承重自检：把安全谓词换成恒真，穿越正控必须当场失守 ──")
# ⚠️ 第一版这里用 `startswith("⛔")` 判"被拒" —— **分辨不出来**：
#   穿越被拦会返回 ⛔，而穿越目标（如 ../../etc/passwd）在本机**不存在**时
#   也会返回 ⛔（"里没有 xxx"）。两个原因都产出同一个前缀 ⇒ 断言恒真。
#   ⇒ 改判**消息内容**：真谓词下必须说"指向技能目录之外"，谓词恒真后就不该再说。
evil = "../../../../etc/passwd"
live = read_skill_text(SKILL, file=evil)
check("★ 真谓词下明确说『指向技能目录之外』", "指向技能目录之外" in live, True)
_old_inside = _S._inside
_S._inside = lambda r, t: True            # 恒真 ⇒ 目录限制形同虚设
try:
    leaked = read_skill_text(SKILL, file=evil)
finally:
    _S._inside = _old_inside
check("★ 谓词恒真后**不再**说『目录之外』（证明 `_inside` 真的在拦）",
      "指向技能目录之外" in leaked, False)
check("_inside 已还原", _S._inside is _old_inside, True)

print("\n── 8. 承重自检：关掉文件清单，第 3 节的提示必须失守 ──")
bad_live = read_skill_text(SKILL, file="core/stance.md")
_old_block = _S._file_list_block
_S._file_list_block = lambda name, cap=60: "  （清单已被测试关掉）"
try:
    bad_dead = read_skill_text(SKILL, file="core/stance.md")
finally:
    _S._file_list_block = _old_block
check("真实现下列出了文件", "- " in bad_live, True)
check("★ 关掉清单后不再列文件（证明『列出可用文件』是它在承重）",
      "- " in bad_dead, False)
check("_file_list_block 已还原", _S._file_list_block is _old_block, True)

print("\n── 9. ★ 技能门不许被 `file` 参数开大 ──")
# 这一节守的是"加功能时回头看门"。`load_skill` 多了 `file` 参数之后，
# 技能门若只看 `name`，模型只读一份参考文件就能凑齐"装载过 nature-writing"，
# 而它从没读过入口文档里那份**协议** —— 那道门存在的理由正是"不许凭记忆写稿"。
from rongke.gate import CompletionGate                          # noqa: E402
from rongke.tools import ToolCall, ToolRegistry                 # noqa: E402


def _call(seq, name, args, ok=True):
    return ToolCall(seq=seq, id="", name=name, args=args, ok=ok,
                    ms=1, result="x", error="")


def _loaded(calls):
    reg = ToolRegistry()
    reg.trace = list(calls)
    return CompletionGate(reg)._loaded_skills()


check("仅入口文档 → 记入（正控）",
      _loaded([_call(1, "load_skill", {"name": SKILL})]), {SKILL})
check("★ 仅参考文件 → **不得**记入（负控：门不许被开大）",
      _loaded([_call(1, "load_skill", {"name": SKILL, "file": "references/x.md"})]), set())
check("入口 + 参考 → 记入",
      _loaded([_call(1, "load_skill", {"name": SKILL}),
               _call(2, "load_skill", {"name": SKILL, "file": "references/x.md"})]), {SKILL})
check("file 为纯空白 → 视同入口（稳健，不被空白绕过）",
      _loaded([_call(1, "load_skill", {"name": SKILL, "file": "   "})]), {SKILL})
check("旧路径 read_skill（无 file 字段）→ 照常记入（向后兼容）",
      _loaded([_call(1, "read_skill", {"name": SKILL})]), {SKILL})
check("失败的装载不算", _loaded([_call(1, "load_skill", {"name": SKILL}, ok=False)]), set())

print("\n── 10. 承重自检：把 file 判据关掉，第 9 节的负控必须当场失守 ──")
probe_call = [_call(1, "load_skill", {"name": SKILL, "file": "references/x.md"})]
live_gate = _loaded(probe_call)
check("真实现下 → 不记入（负控）", live_gate, set())
_orig_loaded = CompletionGate._loaded_skills


def _load_any(self):
    """退回"只看 name、不看 file"的旧行为 —— 即"门被开大"的样子。"""
    out = set()
    for c in self.registry.trace:
        if not c.ok or c.name not in self.LOADER_TOOLS:
            continue
        a = c.args or {}
        nm = a.get("name") or a.get("skill")
        if nm:
            out.add(str(nm).strip())
    return out


CompletionGate._loaded_skills = _load_any
try:
    dead_gate = _loaded(probe_call)
finally:
    CompletionGate._loaded_skills = _orig_loaded
check("★ 退回『只看 name』的旧判据 ⇒ 参考文件也被算成装载（证明这条判据在承重）",
      dead_gate, {SKILL})
check("_loaded_skills 已还原", CompletionGate._loaded_skills is _orig_loaded, True)

print("\n── 11. 首条 user 消息里的**工作区清单**（治 40 步空转）──")
# 这一节守的是 2026-09-21 实跑抓到的**产品级**缺陷：
# 用户说「材料区那份《组会汇报素材》」，契约忠实原样重述（对的），
# 但控制台在提交时已把材料区**改名**成工作区 ⇒ `材料区/` 这个路径任务期不存在。
# 模型于是猜路径：`材料区/X.md`→`.docx`→`.pdf`→`materials/`→`input/`→`../材料区`
# →`../../..`…… 实测 58 次 import_document 全「文件不存在」，40 步里没发过一次 add_slide。
# ⇒ 修法：把**工作区现有条目**如实告诉它。「让模型看见目录」，而不是「指望它猜对」。
import _tkit as T                                                # noqa: E402
from rongke.contract import TaskContract                         # noqa: E402
from rongke.loop import initial_messages, workspace_listing_text  # noqa: E402

_PB = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_prompt", "prompt-listing")
(_PB / "组会汇报素材.md").write_text("x" * 3081, encoding="utf-8")
(_PB / "sub").mkdir(exist_ok=True)
(_PB / "sub" / "a.txt").write_text("y", encoding="utf-8")
_OUTSIDE = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_prompt_outside", "prompt-out")
(_OUTSIDE / "别处的文件.md").write_text("z", encoding="utf-8")

_t_goal = workspace_listing_text(_PB, goal="把材料区那份整理成 PPT")
_t_plain = workspace_listing_text(_PB, goal="写一篇 1200 字的引言")
_t_empty = workspace_listing_text(T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_prompt_empty",
                                              "prompt-empty"))

has("11.1 正控：清单里有工作区里**真**文件的名字", _t_goal, "组会汇报素材.md")
has("11.2 正控：带真实大小（不是一句『若干文件』）", _t_goal, "3081 B")
has("11.3 正控：目录被标出并带条目数", _t_goal, "sub/")
has("11.4 正控：目录的项数也量出来了", _t_goal, "（目录，1 项）")
check("11.5 负控：**工作区之外**的文件不许被列进来（清单 = 围栏内的事实）",
      "别处的文件.md" in _t_goal, False)
check("11.6 负控：目标里没有「材料区」字样 ⇒ **不**多那句澄清（不许每份提示词都塞无关话）",
      "提交之前" in _t_plain, False)
has("11.7 正控：目标里**有**「材料区」⇒ 明说那个路径任务期不存在", _t_goal, "没有 `材料区/` 这个路径")
has("11.8 ★ 正控：空工作区明说『空』，不是给一片白（静默给空 = 静默失败）", _t_empty, "（空")
has("11.9 恒定句：明说『没列出来的就是不存在，不要猜后缀/猜目录』",
      _t_goal, "就是不存在")

# ★ M 型：截断必须**如实说** —— 「没显示」≠「不存在」
# ⚠️ 判据必须用**截断专有**的标记（`…… 还有 `），不能用「没列出来」四个字 ——
#    那句恒定警示语里本来就含这四个字 ⇒ 正负控会一起命中，判据恒真。
#    （第一版就是这么写的，被这条负控当场抓出来。**负控的价值就在这里**。）
_CUT_MARK = "…… 还有 "
for i in range(5):
    (_PB / f"f{i}.txt").write_text("w", encoding="utf-8")
_t_cut = workspace_listing_text(_PB, goal="x", limit=3)
has("11.10 ★ 正控：超过上限时如实说『还有 N 项没列出来』（M 型：不许把没显示读成不存在）",
      _t_cut, _CUT_MARK)
_t_nocut = workspace_listing_text(_PB, goal="x", limit=99)
check("11.11 负控：没超上限时就**不**说『还有…没列出来』（否则那句话变噪音，模型会怀疑清单不全）",
      _CUT_MARK in _t_nocut, False)


class _NoRawGoal:
    """契约**替身**：`to_prompt()` 有，`raw_goal` 没有。"""

    def to_prompt(self):
        return "目标：测试用"


_m = initial_messages(TaskContract(raw_goal="把材料区那份做成 PPT"), _PB)
check("11.12 initial_messages 就是两条：system + user", [x["role"] for x in _m],
      ["system", "user"])
has("11.13 ★ 接线：user 里**同时**有契约正文与工作区清单（别只拼了契约）",
      _m[1]["content"], "组会汇报素材.md")
has("11.14 接线：清单排在契约之后（先读要求，再看有什么）",
      _m[1]["content"], "目标契约")
check("11.15 负控：不给工作区 ⇒ 不追加清单（不许凭空造一条路径出来）",
      "现有条目" in initial_messages(TaskContract(raw_goal="x"), None)[1]["content"], False)
try:
    _m2 = initial_messages(_NoRawGoal(), _PB)
    _m2_ok = any("组会汇报素材.md" in x["content"] for x in _m2)
except Exception as e:
    _m2_ok = f"{type(e).__name__}: {e}"
check("11.16 负控：契约替身没有 raw_goal ⇒ **不许崩**，且清单照出（提示词组装不是崩点）",
      _m2_ok, True)

print("\n── 12. 承重自检：把清单函数换成返回空串，第 11 节的接线断言必须当场失守 ──")
import rongke.loop as _L                                         # noqa: E402
_orig_wlt = _L.workspace_listing_text
_L.workspace_listing_text = lambda ws, goal="", limit=40: ""
try:
    _dead = initial_messages(TaskContract(raw_goal="把材料区那份做成 PPT"), _PB)[1]["content"]
finally:
    _L.workspace_listing_text = _orig_wlt
check("★ 消掉清单后 user 里找不到真文件名（证明 11.13 靠的是清单，不是契约里碰巧有）",
      "组会汇报素材.md" in _dead, False)
check("workspace_listing_text 已还原", _L.workspace_listing_text is _orig_wlt, True)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
