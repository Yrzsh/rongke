# -*- coding: utf-8 -*-
"""课题台账（导师模式 P1/P2）单测 —— 纯文件系统，不需要 LLM、不需要网络。

覆盖 `rongke/topic.py` 与挂在 `core` 域上的 5 个 `topic_*` 工具。
它们补的是本项目的一处**尺度缺失**：内核原来只有「一次 run」这一条时间尺度，
`report_blocker` 是**终局**动作（卡点说出来一次就永远消失）⇒「导师」最核心的
两句话**没有对象可查**：

  ·「上次到哪了？」     —— 课题不是一等公民
  ·「这个坑你两周前卡过。」 —— 没有台账，同一个坑换个说法会再踩一遍

  1. 注册层       —— 5 个工具都注册了、都在 core 域、都有真 description（正控）
  2. 课题生命周期 —— 建 / 切 / ★ 已存在绝不覆盖 / 非法 slug 一律拒
  3. 卡点台账     —— append-only（★ 已有行逐字节不变）/ 重复闭环幂等 / 不猜 id
  4. ★ 卡点必须浮现 —— 未闭环的必须进首条消息上下文；已闭环的必须**不进**
  5. 课题文档     —— append / replace / 非法 doc / 超长**如实标注**
  6. ★ 接线自检   —— `initial_messages` 真的把课题上下文注进 user 了吗
  7. 工具层契约   —— 失败必须 **raise**，不许回 `ok:false` 的 JSON
  8. 承重自检     —— 分别拆掉「浮现机制 / 未闭环过滤 / 覆盖保护」，
                     第 4、2 节对应的断言必须**当场失守**

一条刻意的记法（沿用 `stages_test.py`）：**每个断言都成对出现**。
只有正控 = 可能过的是别的原因；只有负控 = 可能这条检查根本没在干活。

★ 本套件另加一条纪律：**所有写操作都落在临时目录**（走 `_make_topic_tools(root=…)`），
  绝不碰真账 `<项目根>/topics/`。一个会写用户数据的测试是负资产 —— 它会污染
  「上次卡在哪」这本账，而那正是本模块唯一的价值。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import topic as _T                                    # noqa: E402
from rongke.loop import TaskContract, initial_messages            # noqa: E402

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


def hasnt(label, got, needle):
    check(label, needle in got, False)


def raises(label, fn):
    """断言 `fn()` 抛异常。返回异常本身（后面要看它的话有没有用）。"""
    global ok_all
    try:
        fn()
    except Exception as e:
        print(f"  [PASS] {label}（{type(e).__name__}: {str(e)[:60]}）")
        return e
    ok_all = False
    print(f"  [FAIL] {label} —— 没抛异常（失败被静默吞掉了）")
    return None


# 全部写操作落在这个临时根里。真账在 `<项目根>/topics/`，本套件一个字节都不碰。
TMP = Path(tempfile.mkdtemp(prefix="rk_topic_test_"))

import rongke.loop as _L                                          # noqa: E402
from rongke.loop import _make_topic_tools                         # noqa: E402

print(f"临时台账根：{TMP}")

# ═══ 1. 注册层 ═══════════════════════════════════════════════
print("\n── 1. 注册层：5 个工具真的挂上去了吗 ──")
_NAMES = ("topic_use", "topic_status", "raise_blocker", "resolve_blocker", "topic_write")
try:
    _R = _L.Rongke(install_domain=False, use_memory=False, install_mcp_servers=False)
    _reg = [n for n in _R.registry.names() if n in _NAMES]
    check("5 个课题工具**全部**注册（正控）", sorted(_reg), sorted(_NAMES))
    for _n in _NAMES:
        _t = _R.registry.get(_n)
        # W25 起 5 件套改挂 `_TOPIC_DOMAIN`（按需域，与 _KB_DOMAIN 一起进退）——
        # 判据已补硬（contract.needs_kb / 导师开关 / 文献类阶段，见 mounting_decision）。
        check(f"{_n} 在 topic 域（needs_kb/导师开关时挂）", _t.domain, _L._TOPIC_DOMAIN)
        check(f"{_n} 有非空 description", bool((_t.description or "").strip()), True)
        _p = _t.parameters or {}
        check(f"{_n} 的 parameters 是合法 JSON Schema",
              (_p.get("type"), isinstance(_p.get("properties"), dict)), ("object", True))
    # 负控：确认没有把已存在的工具名撞掉（撞名会让 registry 静默覆盖一个）
    check("工具总数没被撞掉（负控：≥ 18 个内置）", len(_R.registry.names()) >= 18, True)
except Exception as e:
    ok_all = False
    print(f"  [FAIL] 构造内核失败：{type(e).__name__}: {e}")

# 工厂：`root=TMP` ⇒ 与内核注册的那份**同一段代码**，只是账本换了地方
_tf = _make_topic_tools(root=TMP)


def _j(s):
    return json.loads(s)


# ═══ 2. 课题生命周期 ═════════════════════════════════════════
print("\n── 2. 课题生命周期 ──")
_r = _j(_tf["topic_status"]())
check("空台账：current 是空串（**不猜、不编**）", _r["current"], "")
check("空台账：topic 列表为空", _r["topics"], [])
check("空台账：依然 ok=True（没课题不是错误）", _r["ok"], True)
check("空台账：带 hint 告诉模型下一步做什么", "topic_use" in _r.get("hint", ""), True)

_r = _j(_tf["topic_use"](slug="probe-a", title="探测课题 A", question="Q-A 是什么？"))
check("新建：created=True", _r["created"], True)
check("新建：CURRENT 已指向它", _r["current"], "probe-a")
check("新建：标题被记下", _r["title"], "探测课题 A")
check("磁盘上真有 topic.json", (TMP / "probe-a" / "topic.json").exists(), True)
check("磁盘上真有 CURRENT", (TMP / "CURRENT").read_text(encoding="utf-8").strip(), "probe-a")

# ★ 承重判据：已存在时绝不覆盖（静默覆盖用户一手内容是数据事故）
_r = _j(_tf["topic_use"](slug="probe-a", title="改个名试试", question="换问题"))
check("★ 重复建同名课题：created=False", _r["created"], False)
check("★ 旧标题**没被覆盖**", _r["title"], "探测课题 A")
check("★ 旧科学问题**没被覆盖**", _r["question"], "Q-A 是什么？")

# 第二个课题（测隔离）
_tf["topic_use"](slug="probe-b", title="探测课题 B")
_st = _j(_tf["topic_status"]())                   # 此刻 current = probe-b
check("★ 有当前课题时**依然**列全量课题（否则模型不知道库里还有什么）",
      sorted(_st["topics"]), ["probe-a", "probe-b"])
check("切到 B 之后 current=B", _j(_tf["topic_status"]())["current"], "probe-b")
_tf["topic_use"](slug="probe-a")                     # 切回来
check("切回 A 之后 current=A", _j(_tf["topic_status"]())["current"], "probe-a")
check("切回来的 A 标题还是原样", _j(_tf["topic_status"]())["title"], "探测课题 A")

print("\n   负控：非法课题名必须**拒**，不是静默建个怪目录")
for _bad, _why in (("../evil", "目录穿越"), ("a/b", "带斜杠"), ("", "空串"),
                   ("x" * 49, "超 48 字"), (".", "点"), ("..", "双点")):
    raises(f"topic_use(slug={_bad!r}) 被拒（{_why}）",
           lambda b=_bad: _tf["topic_use"](slug=b))
check("被拒之后没有在根外面留下东西（负控：穿越没得逞）",
      sorted(p.name for p in TMP.iterdir() if p.is_dir()), ["probe-a", "probe-b"])
raises("topic_status(slug='不存在') 明确报错",
       lambda: _tf["topic_status"](slug="no-such-topic"))

# ═══ 3. 卡点台账 ═════════════════════════════════════════════
print("\n── 3. 卡点台账：append-only / 幂等 / 不猜 id ──")
_bl = TMP / "probe-a" / "BLOCKERS.jsonl"
# 用**唯一标记**当判据，避免"碰巧文本里有这串字"造成假阳性
CLOSED = "UNIQ_CLOSED_AAA"
OPENED = "UNIQ_OPEN_BBB"

_r = _j(_tf["raise_blocker"](what=CLOSED, tried=["试过办法一", "试过办法二"], where="step-3"))
check("记卡点回 id=B001", _r["id"], "B001")
check("★ 写完读回来核（verified）", _r["verified"], True)
check("未闭环数 = 1", _r["open_total"], 1)
check("台账文件已落盘", _bl.exists(), True)

_r = _j(_tf["raise_blocker"](what=OPENED, tried=["试过办法三"]))
check("第二条回 id=B002", _r["id"], "B002")
check("未闭环数 = 2", _r["open_total"], 2)
_lines_before = _bl.read_text(encoding="utf-8").splitlines()
check("append-only：两条各占一行", len([x for x in _lines_before if x.strip()]), 2)
# ★ 把"已有行"逐字节抄下来 —— 稍后要证明它们真的没被动过
_first_line_copy = _lines_before[0]

print("\n   闭环一条，并证明**已有行逐字节未变**")
_r = _j(_tf["resolve_blocker"](blocker_id="B001", how="换个思路就好了"))
check("闭环成功", _r["ok"], True)
check("闭环后 verified", _r["verified"], True)
check("闭环后未闭环数 = 1", _r["open_total"], 1)
_lines_after = _bl.read_text(encoding="utf-8").splitlines()
_kept = [x for x in _lines_after if x.strip()]
check("★ 行数 = 2 + 1（**追加**，不是重写整份）", len(_kept), 3)
check("★ 第一行**逐字节**与闭环前完全相同（不改已有行）",
      _kept[0], _first_line_copy)
check("★ 第二条（open）也逐字节未变", _kept[1], _lines_before[1])
check("新追加的那行是一条 resolve 事件", _j(_kept[2]).get("kind"), "resolve")

print("\n   幂等与负控")
_r = _j(_tf["resolve_blocker"](blocker_id="B001", how="再闭环一次"))
check("重复闭环：already=True（幂等，不报错）", _r["already"], True)
check("★ 幂等分支也带 verified（契约两端必须一致，少一个键上层就 KeyError）",
      "verified" in _r, True)
check("★ 重复闭环**没有再写一行**（行数仍 3）",
      len([x for x in _bl.read_text(encoding="utf-8").splitlines() if x.strip()]), 3)
_e = raises("闭环一个不存在的 id 必须报错", lambda: _tf["resolve_blocker"](blocker_id="B999"))
check("★ 报错里**列出真实 id**（模型不用猜）", "B002" in str(_e), True)
raises("what 为空必须报错（空卡点等于没记）", lambda: _tf["raise_blocker"](what="   "))

# 隔离：probe-b 的台账里不该有 probe-a 的东西
check("跨课题隔离：b 没有台账文件（还没记过）",
      (TMP / "probe-b" / "BLOCKERS.jsonl").exists(), False)
_tf["raise_blocker"](what="B 自己的坑", slug="probe-b")
check("跨课题隔离：b 记完之后 open_total=1",
      _j(_tf["topic_status"](slug="probe-b"))["open_total"], 1)

# ═══ 4. ★ 卡点必须浮现（导师的灵魂） ═════════════════════════
print("\n── 4. ★ 卡点必须浮现：这是「导师记得你上次卡在哪」的唯一判据 ──")
_ctx = _T.context_text(root=TMP)
has("★ 未闭环卡点的 what 出现在上下文里", _ctx, OPENED)
has("★ 它「试过什么」也出现了（下次最值钱的信息）", _ctx, "试过办法三")
has("★ 明确标出「未闭环」", _ctx, "未闭环卡点")
has("上下文里有课题标题（学生一眼知道在说哪个课题）", _ctx, "探测课题 A")
hasnt("★ **已闭环**的那条**不出现**（否则台账就是噪音）", _ctx, CLOSED)
has("上下文明确指导：解决了就用 resolve_blocker 闭环", _ctx, "resolve_blocker")

print("\n   全部闭环之后：必须如实说「没有未闭环卡点」，而不是留个空壳")
_tf["resolve_blocker"](blocker_id="B002", how="也解决了")
_ctx2 = _T.context_text(root=TMP)
has("全闭环 ⇒ 明说「没有未闭环卡点」", _ctx2, "没有未闭环卡点")
hasnt("全闭环 ⇒ 不再出现卡点行", _ctx2, OPENED)

print("\n   超上限必须**如实报数**（M 型：别把「没列出来」渲染成「不存在」）")
for _i in range(1, _T.BLOCKER_SHOW + 3):          # 造 SHOW+2 条未闭环
    _tf["raise_blocker"](what=f"UNIQ_FLOOD_{_i:02d}")
_ctx3 = _T.context_text(root=TMP)
_n_open = len(_T.open_blockers("probe-a", root=TMP))
check(f"未闭环共 {_T.BLOCKER_SHOW + 2} 条", _n_open, _T.BLOCKER_SHOW + 2)
has("★ 明说还有几条**没列出来**（不是不存在）", _ctx3, "没列出来")
has(f"列出的条数 = 上限 {_T.BLOCKER_SHOW}", _ctx3,
    f"未闭环卡点 {_n_open} 条（新 → 旧，列 {_T.BLOCKER_SHOW} 条）")
for _i in range(1, _T.BLOCKER_SHOW + 3):
    # 这 7 条的 id 是 B003…B009（B001/B002 已被上面两次闭环占掉号）
    _tf["resolve_blocker"](blocker_id=f"B{_i + 2:03d}", how="清场")

print("\n   负控：没有当前课题时**必须闭嘴**（空壳大纲比没有更糟）")
_EMPTY = Path(tempfile.mkdtemp(prefix="rk_topic_empty_"))
check("★ 空台账根 ⇒ context_text 返回**空串**（不注入空壳）",
      _T.context_text(root=_EMPTY), "")
shutil.rmtree(_EMPTY, ignore_errors=True)

# ═══ 5. 课题文档 ═════════════════════════════════════════════
print("\n── 5. 课题文档：append / replace / 超长如实标注 ──")
# ★ W27：`topic_write` 现在**必须显式传 slug**（缺了就报错）。
#   起因是一次真实事故：CURRENT 还停在上一轮的**另一个**课题时，
#   模型不传 slug 就会把整轮证据静默写进那个课题 —— 回包还是 ok:true。
#   所以本节所有写调用都显式带 slug。
_SLUG = "probe-a"
_r = _j(_tf["topic_write"](doc="plan", text="# 计划\n第一条技术路线", slug=_SLUG))
check("写 plan：verified=True", _r["verified"], True)
check("写 plan：落盘", (TMP / "probe-a" / "PLAN.md").exists(), True)
_r = _j(_tf["topic_write"](doc="plan", text="第二条追加", slug=_SLUG))
_plan = (TMP / "probe-a" / "PLAN.md").read_text(encoding="utf-8")
has("append 之后**旧内容还在**", _plan, "第一条技术路线")
has("append 之后新内容也在", _plan, "第二条追加")
check("append 之后字符数增加", _r["chars"] > len("# 计划\n第一条技术路线"), True)

_tf["topic_write"](doc="plan", text="只有这一行", mode="replace", slug=_SLUG)
_plan = (TMP / "probe-a" / "PLAN.md").read_text(encoding="utf-8")
check("★ replace 之后**只剩新内容**（旧的不留）", _plan.strip(), "只有这一行")

raises("非法 doc 名必须报错",
       lambda: _tf["topic_write"](doc="secret", text="x", slug=_SLUG))
raises("空 text 必须报错（空写没有意义）",
       lambda: _tf["topic_write"](doc="plan", text="  ", slug=_SLUG))

print("\n   ★ W27 负控：不传 slug 必须**报错**，不许静默写进 current()")
# 这一条是本次事故的直接判据：错写目标**必须**可见，而不是 ok:true。
raises("★ 缺 slug ⇒ 报错（不许兜到 current()）",
       lambda: _tf["topic_write"](doc="plan", text="偷偷写进别的课题"))
raises("★ 缺 slug 的报错里要点名 current()（人才查得出来）",
       lambda: _tf["topic_write"](doc="plan", text="x"))
raises("★ 传一个不存在的课题也要报错，不许新建",
       lambda: _tf["topic_write"](doc="plan", text="x", slug="probe-does-not-exist"))
# 而且**确实没有落下东西**：probe-b 的台账不该被这次调用碰到
check("★ 被拒的写入**没有**污染 probe-b",
      (TMP / "probe-b" / "PLAN.md").exists(), False)

print("\n   超长文档：截断必须如实标注（**按需注入**口径，2026-11-04 修判据）")
# 旧断言不传 goal 就找「前 N 字 / 全文 N 字」，可产品 11-03 起已改成
# 「文档正文只在问到台账时展开」—— 这两条是判据过期，不是产品缺陷。
# 现在两个方向都量：问到 ⇒ 必须展开并如实标注；寒暄/无 goal ⇒ 不倾泻，
# 但**必须如实告诉模型有哪些文档、怎么读**（不展开 ≠ 不存在）。
_long = "很长的一行。" * 400                                     # 6 * 400 = 2400 字
_tf["topic_write"](doc="reading", text=_long, mode="replace", slug=_SLUG)
_total = len((TMP / "probe-a" / "READING.md").read_text(encoding="utf-8").strip())
check("读回来的总长 > DOC_HEAD（前提成立）", _total > _T.DOC_HEAD, True)
_ctx4 = _T.context_text(root=TMP, goal="我的开题报告写了什么", slug=_SLUG)
has("★ 问到台账 ⇒ 展开正文并标注「前 N 字」", _ctx4, f"前 {_T.DOC_HEAD} 字")
has("★ 问到台账 ⇒ 标注「全文 N 字」", _ctx4, f"全文 {_total} 字")
_ctx5 = _T.context_text(root=TMP, goal="你好", slug=_SLUG)
hasnt("负控：寒暄不倾泻文档正文", _ctx5, f"前 {_T.DOC_HEAD} 字")
has("负控也不许装作没有文档（如实列清单＋指路）", _ctx5, "本轮没有自动展开")
has("负控给出可读回文档的工具", _ctx5, "topic_status")
_ctx6 = _T.context_text(root=TMP, slug=_SLUG)
hasnt("负控：不传 goal 同样不展开正文", _ctx6, f"前 {_T.DOC_HEAD} 字")

# ═══ 6. ★ 接线自检 ═══════════════════════════════════════════
print("\n── 6. ★ 接线自检：initial_messages 真的把课题上下文注进去了吗 ──")
# 这一节测的是本项目反复踩的那个坑：**能力造出来了，但没接线**。
# 做法：把 context_text 换成返回一个哨兵串，看它能不能走到 user 消息里。
_orig_ctx = _T.context_text
_SENTINEL = "SENTINEL_TOPIC_CTX_7f3a"
_T.context_text = lambda *a, **k: _SENTINEL
try:
    _msgs_real = initial_messages(TaskContract(raw_goal="把上次那个实验接着做完"),
                                  None, topics_root=TMP)
    _user_real = _msgs_real[1]["content"]
    has("★ 哨兵串出现在 user 消息里（接线是通的）", _user_real, _SENTINEL)
    check("★ 消息仍是两条：system + user（没多塞第三条）",
          [m["role"] for m in _msgs_real], ["system", "user"])
finally:
    _T.context_text = _orig_ctx

# 用真的 context_text 再走一遍端到端：卡点必须出现在**首条消息**里
_tf["raise_blocker"](what="ENDO2END_卡点_9c1")
_msgs2 = initial_messages(TaskContract(raw_goal="继续推进"), None, topics_root=TMP)
has("★ 端到端：真卡点出现在首条消息里", _msgs2[1]["content"], "ENDO2END_卡点_9c1")
check("★ 还原后 context_text 是原函数", _T.context_text is _orig_ctx, True)
# 负控：指向一个**确定的空台账根**时必须不注入。
# ⚠️ 这里刻意**不**用「不传 topics_root ⇒ 走真项目根」当负控 ——
#    真项目根一旦正常开始带课题，这条断言就会假红（脆弱判据 = 早晚要修的债）。
_BARE = Path(tempfile.mkdtemp(prefix="rk_topic_bare_"))
_msgs3 = initial_messages(TaskContract(raw_goal="随便一个不带课题的任务"), None,
                          topics_root=_BARE)
hasnt("负控：空台账根 ⇒ 不注入课题上下文", _msgs3[1]["content"], "课题上下文")
shutil.rmtree(_BARE, ignore_errors=True)

# ═══ 7. 工具层契约：失败必须 raise ═══════════════════════════
print("\n── 7. 工具层契约：失败必须 **raise**，不许回 ok:false 的 JSON ──")
# 为什么这条值钱：注册表把「返回字符串」记成**成功调用**（ok=True），
# 于是 payload 里那个 ok=false 会**滑过证据门** —— `_load_skill` 当初就是为此改成抛错的。
# ── 正控（与下面的负控成对）：这两只手**该成的时候真的成** ──────
# 只写"失败会抛"是不够的：一个永远抛错的工具也能让负控全绿。
_T.ensure_topic("probe-edit", title="原标题甲", question="原问题乙", root=TMP)
_n0 = len(_T.read_topic("probe-edit", root=TMP).get("library") or [])
check("topic_link 正控：挂上一篇，count +1",
      _j(_tf["topic_link"](slug="probe-edit", doc_id="kbPROBE01"))["count"], _n0 + 1)
check("topic_link 幂等：同一篇重复挂不重复计入",
      _j(_tf["topic_link"](slug="probe-edit", doc_id="kbPROBE01"))["count"], _n0 + 1)
check("topic_link 落盘读回（自称成功不算证据）",
      _T.read_topic("probe-edit", root=TMP)["library"], ["kbPROBE01"])
check("topic_link 解除关联后 count 回落",
      _j(_tf["topic_link"](slug="probe-edit", doc_id="kbPROBE01", linked=False))["count"], _n0)

# ★ 这一条是本轮最该钉住的：`update_topic` 的 title/question 是**必填位置参数**，
#   内部无条件覆写。包装层若照 schema 传空串，一次"只改阶段"就会把标题和研究问题
#   一起抹掉 —— 静默毁数据，回包还是 ok。所以正控必须验"没被抹掉"，不能只验改了。
_tf["topic_update"](slug="probe-edit", stage="evidence")
_m = _T.read_topic("probe-edit", root=TMP)
check("★ 只改 stage：标题**保住**（空串=不改，不是清空）", _m["title"], "原标题甲")
check("★ 只改 stage：研究问题**保住**", _m["question"], "原问题乙")
check("只改 stage：stage 真的改了", _m["stage"], "evidence")
_tf["topic_update"](slug="probe-edit", next_step="补 E7 的实验设计")
check("next_step 可设置", _T.read_topic("probe-edit", root=TMP)["next_step"], "补 E7 的实验设计")
_tf["topic_update"](slug="probe-edit", next_step="")
check("next_step 传空串 = 清空（这是唯一允许清空的字段）",
      _T.read_topic("probe-edit", root=TMP)["next_step"], "")

# ★ 实验台账两个工具的**正控**（走工具层，不是直接调 `lab.py` ——
#   模型真的碰到的是工具层；直接调内核只能证明"内核能跑"，证明不了接线对）。
_lr = _j(_tf["lab_record"](slug="probe-a", kind="observe", code="S01",
                           temp_c=23, growth="slow", text="23℃ 可长但慢"))
check("lab_record 回包 verified（写完读回，不是自称成功）", _lr.get("verified"), True)
_lr = _j(_tf["lab_record"](slug="probe-a", kind="measure", code="S01",
                           metric="growth_rate", value=1.2, unit="mm/d", n=3))
check("lab_record 记完带 digest（模型不用再调一次才知道变了什么）",
      bool(_lr.get("digest")), True)

# ★ 作废走**工具层**（2026-11-03）。为什么必须在这一层验：
#   工具层的 `seq` 意思是"要作废第几条"，而事件自己的序号**也叫 seq**。
#   这两个同名不同义的参数在两层之间最容易被压成一个 —— 串了之后台账序号全歪，
#   而且**不报错**（它只是把每条事件都写成同一个号）。所以正控放在这里。
_lr2 = _j(_tf["lab_record"](slug="probe-a", kind="observe", code="S01",
                            temp_c=23, growth="slow", text="重复的一条"))
_rr = _j(_tf["lab_record"](slug="probe-a", kind="retract",
                           seq=_lr2["event"]["seq"], reason="重复写入"))
check("★ 工具层 retract 认 seq（作废第几条）", _rr.get("verified"), True)
check("retract 回包带作废计数（模型能立刻知道还剩几条有效）",
      _rr.get("retracted"), 1)
check("★ 作废不吞事件：盘上 2 条不进状态（作废记录 1 + 被作废 1），其余照旧",
      _rr.get("total_events", 0) - _rr.get("live_events", 0), 2)
_lrep = _j(_tf["lab_report"](slug="probe-a"))
check("lab_report 带 md_text（模型可 write_file 落到本次工作区）",
      bool(_lrep.get("md_text")), True)
check("lab_report 带 csv_text（可直接喂 plot_data 出图）",
      bool(_lrep.get("csv_text")), True)
check("lab_report 把 LAB.md 写进课题目录", _lrep.get("md_file"), "LAB.md")
check("LAB.md 里出现矩阵表头（不是空壳）",
      "菌株 × 指标" in (_lrep.get("md_text") or ""), True)

# ★ 方法卡（2026-11-02）。补它的理由不是"多一个工具"，而是**资产曾经没人读**：
#   `lab_assets` 里两套染色方案、失败三分诊、身份证据链全都挂着 doc_id + 页码，
#   但只有测试引用过 —— 测试全绿、模型够不着。这条断言钉的就是"够得着"。
_lm = _j(_tf["lab_method"](need="核质染色怎么做", slug="probe-a"))
check("★ lab_method 认中文原话（模型常直接传用户说法，不让它猜枚举）",
      _lm.get("need"), "staining")
check("lab_method 写完读回 verified（卡是文件，不是口头承诺）",
      _lm.get("verified"), True)
check("lab_method 把卡写进课题目录", _lm.get("file"), "方法卡-staining.md")
check("染色卡里两条方案 + 1000 倍冲突同时在场（不许择一）",
      ("5 μg/mL" in (_lm.get("md_text") or "")) and
      ("5 mg/mL" in (_lm.get("md_text") or "")) and
      ("1000×" in (_lm.get("md_text") or "")), True)
_lm2 = _j(_tf["lab_method"](need="identity", slug="probe-a"))
check("身份链卡在工具层也出得来", _lm2.get("need"), "identity")
check("身份链卡写明「未记录 ≠ 没做」",
      "≠" in (_lm2.get("md_text") or ""), True)

_BAD_CALLS = [
    ("topic_use", lambda: _tf["topic_use"](slug="../x")),
    ("topic_status", lambda: _tf["topic_status"](slug="no-such")),
    ("raise_blocker", lambda: _tf["raise_blocker"](what="")),
    ("resolve_blocker", lambda: _tf["resolve_blocker"](blocker_id="B999")),
    # 带合法 slug，才测得到「非法 doc 名」这一层（缺 slug 会先在更外层报错）
    ("topic_write", lambda: _tf["topic_write"](doc="nope", text="x", slug="probe-a")),
    # ★ 2026-11-02 新增的两只手（模型侧原先没有"关联文献 / 改课题元数据"的工具）。
    #   它们同样是**改盘**操作，所以"失败必须抛"这条规矩必须一视同仁地钉住。
    ("topic_link", lambda: _tf["topic_link"](doc_id="kb123", slug="")),        # 缺 slug
    ("topic_link", lambda: _tf["topic_link"](slug="probe-a", doc_id="../../etc")),  # 非法 id
    ("topic_update", lambda: _tf["topic_update"](slug="probe-a")),             # 什么都没给
    # ★ 2026-11-02 再补两个实验台账工具。**这条负控是整轮最要紧的一条**：
    #   `lab_record` 把「定性观察」与「定量测量」分成两个 kind，
    #   靠的就是"value 不是数字就抛错"。如果工具层把它吞了、回个 ok:false，
    #   模型会以为记上了，而 `gaps()` 会把这格算成"已有值"——
    #   于是「你有没有数据」永远答不准，而那正是用户被导师追问的那一句。
    ("lab_record", lambda: _tf["lab_record"](slug="probe-a", kind="measure",
                                             code="S01", metric="growth_rate",
                                             value="慢", n=3)),
    ("lab_record", lambda: _tf["lab_record"](slug="probe-a", kind="measure",
                                             code="S01", metric="没这个指标",
                                             value=1.2, n=3)),
    ("lab_record", lambda: _tf["lab_record"](slug="probe-a", kind="乱写")),
    # 作废的三条负控：不写理由 / 作废不存在的序号 / 作废已作废的 ——
    # 每一条都必须是"抛错"，因为它们全都是**改历史**，吞掉就等于悄悄改账。
    ("lab_record", lambda: _tf["lab_record"](slug="probe-a", kind="retract",
                                             seq=1, reason="")),
    ("lab_record", lambda: _tf["lab_record"](slug="probe-a", kind="retract",
                                             seq=9999, reason="越界")),
    ("lab_record", lambda: _tf["lab_record"](slug="probe-a", kind="retract",
                                             seq=_lr2["event"]["seq"],
                                             reason="重复作废")),
    # ★ 资产里没有的方法必须抛错并列出三张卡 —— 静默回一张不相干的卡，
    #   用户会照着染色卡去跑 western blot（这是最坏的一种"有帮助"）。
    ("lab_method", lambda: _tf["lab_method"](need="western blot", slug="probe-a")),
]
for _n, _call in _BAD_CALLS:
    try:
        _out = _call()
    except Exception:
        print(f"  [PASS] {_n} 非法输入 ⇒ 抛异常（会走「工具执行失败」分支回灌给模型）")
        continue
    ok_all = False
    print(f"  [FAIL] {_n} 非法输入竟然**正常返回**了：{str(_out)[:80]}")

# ═══ 8. ★ 承重自检 ═══════════════════════════════════════════
print("\n── 8. ★ 承重自检：把机制拆掉，前面的断言必须当场失守 ──")

print("\n   8.1 拆掉「未闭环过滤」⇒ 第 4 节的负控（已闭环不浮现）必须失守")
# ⚠️ 必须用一条**最新的**已闭环卡点来测：旧的（B001）排在第 9 位，会被
#    「只列前 BLOCKER_SHOW 条」的截断挤掉，于是这条承重自检会**假绿**
#    —— 第一次写就栽在这里（自检自己掉了坑，故此处留记）。
FRESH = "UNIQ_CLOSED_FRESH_5e2"
_rid = _j(_tf["raise_blocker"](what=FRESH))["id"]
_tf["resolve_blocker"](blocker_id=_rid, how="当场解决")
hasnt("（前提）这条最新的已闭环卡点**本来不浮现**", _T.context_text(root=TMP), FRESH)
_orig_open = _T.open_blockers
_T.open_blockers = lambda *a, **k: _T.blockers(*a, **k)     # 不过滤，把已闭环也端上来
try:
    _ctx_bad = _T.context_text(root=TMP)
    check("★ 不过滤之后，**已闭环**的那条果然冒出来了（证明 4 节负控在承重）",
          FRESH in _ctx_bad, True)
finally:
    _T.open_blockers = _orig_open
check("open_blockers 已还原", _T.open_blockers is _orig_open, True)

print("\n   8.2 拆掉「浮现机制」⇒ 第 4 节的正控（未闭环必须出现）必须失守")
_T.open_blockers = lambda *a, **k: []
try:
    _ctx_dead = _T.context_text(root=TMP)
    hasnt("★ 卡点不再浮现（证明 4 节正控测的是**浮现机制**，不是碰巧文里有那串字）",
          _ctx_dead, OPENED)
    has("★ 而且退化成「没有未闭环卡点」——正是最该避免的那种假报告",
        _ctx_dead, "没有未闭环卡点")
finally:
    _T.open_blockers = _orig_open

print("\n   8.3 拆掉「绝不覆盖」⇒ 第 2 节的重建断言必须失守")
_orig_ensure = _T.ensure_topic


def _evil_ensure(slug, title="", question="", root=None):
    """一个**总是覆盖**的版本：证明第 2 节那条判据真的在挡这件事。

    ⚠️ 刻意**不改盘**、只返回一份 title/question 被换掉的元数据 ——
    如果它真去写 topic.json，这个承重自检**自己就成了数据事故**：
    会把后面所有断言的前提一起改掉（第一次写就踩了，故此处留记）。
    """
    real = _T.read_topic(slug, root=root) or {}
    return dict(real, title=title or slug, question=question)


_T.ensure_topic = _evil_ensure
_evil = _make_topic_tools(root=TMP)
try:
    _r = _j(_evil["topic_use"](slug="probe-a", title="改个名试试"))
    check("★ 换成会覆盖的实现后，旧标题**果然丢了**（证明 2 节的断言在承重）",
          _r["title"], "改个名试试")
finally:
    _T.ensure_topic = _orig_ensure
check("ensure_topic 已还原", _T.ensure_topic is _orig_ensure, True)
# 还原后再建一次同名课题：旧标题必须回来（证明真实现确实不覆盖）
_r = _j(_tf["topic_use"](slug="probe-a", title="又想改名"))
check("★ 真实现下旧标题**依然保住**", _r["title"], "探测课题 A")

shutil.rmtree(TMP, ignore_errors=True)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
