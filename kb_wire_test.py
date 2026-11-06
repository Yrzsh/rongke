# -*- coding: utf-8 -*-
"""**工具层**验收：kb_* 工具族是否真的对荣珂可达、可调、被正确围栏。

**判据一律走荣珂自己的反射路**（`reflect_workbench_tools` / `install_workbench`），
不直接调 litkb —— 直接调只能证明"litkb 能跑"（那早就证明过了），
证明不了"荣珂看得见它"。这正是本项目的 J 型/R 型纪律：用真尺子量。

★ 与 `kb_e2e_test.py` 分工，**不许互相替代**：
  这个量**工具层**（反射进来没有 / 围栏登记没有 / schema 有没有参数 / 真调回包对不对），
  那个量**链路层**（HTTP 路由 → `Manager.kb_upload` → 注册表 → litkb → 面板端点）。

★ 2026-09-22 从 `runs/_regress/kb_wire_probe.py` 搬来这里并挂进 `regress.py`：
  原来它是一个**孤儿** —— 不在任何回归清单里，只有人记得才会跑一次；
  而它守的恰恰是"荣珂看得见知识库"这件事。**靠人记得的判据等于没有判据。**

跑：python312 kb_wire_test.py     退出码 0 全过 / 1 有失败
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke.bridge import MUTATING, install_workbench, reflect_workbench_tools  # noqa: E402
from rongke.config import Config  # noqa: E402
from rongke.tools import ToolRegistry  # noqa: E402

import _tkit as T  # noqa: E402   （拆夹具一律"挪，不删" —— 本环境按条目数收删除费）

# 不写死盘上路径：workbench 位置由配置给（空则自动探测 ~/.dsh/papers/workbench）。
WB = Config.load().runtime.workbench
ok_n = bad_n = 0


def check(label, got, want):
    global ok_n, bad_n
    good = got == want
    ok_n += good
    bad_n += (not good)
    # 标记必须顶格写成 `[PASS]` / `[FAIL]` —— `regress.py` 按行首标记数断言。
    # 原来这里用自创的 `  ok  ` 写法，它一个都数不到，
    # 于是这 57 项断言在回归汇总里**等于不存在**（判据在，却没人被它拦住）。
    print(("  [PASS] " if good else "  [FAIL] ") + f"{label}")
    print(f"         实测={got!r}")
    if not good:
        print(f"         期望={want!r}")


def truthy(label, got):
    """`check(x, True)` 的糖。**单独一个函数**是因为 `check(..., True)` 里的
    `True` 容易被读成"这条判据本身没内容" —— 名字写出来才知道在验什么。"""
    check(label, bool(got), True)


print("=" * 78)
print("A · 反射层：kb_* 工具是否注册进来了")
print("=" * 78)
rows = reflect_workbench_tools(WB)
names = [r[0] for r in rows]
kb = [n for n in names if n.startswith("kb_")]
print(f"  反射到的工具总数 = {len(names)}")
# 2026-09-22：从 6 个补到 11 个。补的这 5 个（rank / compare / review / bibtex / ingest）
# **litkb 里早就实现好了，只是一个都没包成工具** ⇒ 荣珂够不到。
# 「外部有能力、本地有资产、中间没有工具」—— 本项目记录在案的老毛病，这是第三次复发。
check("kb_* 一族齐了（11 个）", sorted(kb),
      ["kb_add", "kb_ask", "kb_bibtex", "kb_compare", "kb_ingest",
       "kb_list", "kb_note", "kb_rank", "kb_read", "kb_review", "kb_search"])

# schema 必须有参数，否则模型只会空调用
by_name = {r[0]: r for r in rows}
for t in ("kb_list", "kb_search", "kb_read", "kb_ask", "kb_add", "kb_note",
          "kb_rank", "kb_compare", "kb_review", "kb_bibtex",
          "kb_ingest"):
    props = list(((by_name[t][2] or {}).get("properties") or {}).keys())
    check(f"{t} 的 schema 有参数（模型才知道怎么传）", bool(props) or t == "kb_list", True)
print(f"  kb_search 的 schema 参数 = "
      f"{list(((by_name['kb_search'][2] or {}).get('properties') or {}).keys())}")
print(f"  kb_ingest 的 schema 参数 = "
      f"{list(((by_name['kb_ingest'][2] or {}).get('properties') or {}).keys())}")
# 入参名字是模型唯一能看见的说明书。写类工具至少要能指定"入哪些"和"是否只看不动"。
check("kb_ingest 有 paths 参数（否则没法指定入什么）",
      "paths" in ((by_name["kb_ingest"][2] or {}).get("properties") or {}), True)
check("★ kb_ingest 有 dry_run（乱库整理前先看一眼，不许只能真写）",
      "dry_run" in ((by_name["kb_ingest"][2] or {}).get("properties") or {}), True)
check("kb_rank 有 n 参数", "n" in ((by_name["kb_rank"][2] or {}).get("properties") or {}), True)

print()
print("=" * 78)
print("B · 围栏登记：两个写类工具必须被登记成 mutating")
print("=" * 78)
check("kb_add 登记为 mutating", "kb_add" in MUTATING, True)
check("kb_note 登记为 mutating", "kb_note" in MUTATING, True)
check("kb_list 不该是 mutating（只读）", "kb_list" in MUTATING, False)
check("kb_search 不该是 mutating（只读）", "kb_search" in MUTATING, False)
# 2026-09-22 补的两个"会改"的工具。本项目的治理口令是「未知工具一律按'会改'」——
# 那么**已知会改的**更不该漏。kb_review 只在给了 out 时才落盘，
# 但它"能写"这件事是确定的 ⇒ 按写类登记，宁可多一道围栏。
check("★ kb_ingest 登记为 mutating（写库）", "kb_ingest" in MUTATING, True)
check("★ kb_review 登记为 mutating（给了 out 就落盘）", "kb_review" in MUTATING, True)
check("kb_rank 不该是 mutating（只读）", "kb_rank" in MUTATING, False)
check("kb_compare 不该是 mutating（只读）", "kb_compare" in MUTATING, False)
check("kb_bibtex 不该是 mutating（只读）", "kb_bibtex" in MUTATING, False)

reg = ToolRegistry()
installed = install_workbench(reg, WB)
# 口径（2026-10-15 改）：原先写死 `len(installed) == len(names)`，
# 即「装上的必须恰好等于 workbench 反射出来的那批」。
# 加 `kb_index` 之后这条恒假 —— 但**恒假的原因不是缺陷**：
# kb_index 是**荣珂自己的工具**（见 bridge._kb_index），上游 workbench 里没有它，
# 所以它本来就**不该**出现在 `names` 里。
#
# 若只是把右边改成 `len(names) + 1`，就把判据换成了另一个写死的数：
# 下次谁再加一件荣珂侧工具，这里照样假红，而假红会训练人忽略这条。
# 改成打**真正的不变量**：反射来的一个不少，且增补的每一件都在荣珂侧有出处。
_extra = sorted(set(installed) - set(names))
check("★ 反射来的 workbench 工具一个不少", set(names) - set(installed), set())
check("★ 荣珂侧增补的工具都在预期名单里（不是误注册）", _extra, ["kb_index"])
check("★ kb_index 已注册", "kb_index" in installed, True)
tool = reg.get("kb_add") if hasattr(reg, "get") else None
muts = sorted(n for n in installed if getattr(reg.get(n), "mutating", False) is True) \
    if hasattr(reg, "get") else []
print(f"  注册表里判定为 mutating 的 = {muts}")

print()
print("=" * 78)
print("C · 真调用：走反射出来的原函数（与荣珂注册的是同一个对象）")
print("=" * 78)
fn = {n: f for n, _d, _s, f in rows}

r = json.loads(fn["kb_list"](limit=3))
print(f"  kb_list      ok={r.get('ok')} total={r.get('total')} "
      f"全文={r.get('fulltext')} 列出={r.get('showing')}")
check("kb_list 回了真实总数（>0）", (r.get("total") or 0) > 0, True)
one = (r.get("items") or [{}])[0]
print(f"  首条: id={one.get('id')} {one.get('year')} {str(one.get('title'))[:44]}")
check("条目里**不带**整篇摘要（防上下文爆炸）", "abstract" in one, False)
STRIP = str(r)
check("kb_list 回包 < 4000 字符（实测曾单条 1800 字符×8）", len(STRIP) < 4000, True)
print(f"  kb_list 回包 = {len(STRIP)} 字符")

doc_id = one.get("id")
r = json.loads(fn["kb_search"]("immune response", n=3))
print(f"  kb_search    ok={r.get('ok')} hits={r.get('n')}")
if r.get("hits"):
    h = r["hits"][0]
    print(f"  首命中: {h.get('doc_id')} p.{h.get('page')} | {str(h.get('snippet'))[:60]}")
    check("命中带页码（可做出处）", isinstance(h.get("page"), int), True)
    check("snippet 不超过 300 字符", len(str(h.get("snippet") or "")) <= 300, True)

r = json.loads(fn["kb_read"](doc_id=doc_id, max_chars=1200))
print(f"  kb_read      ok={r.get('ok')} 页数={r.get('pages_total')} "
      f"本篇总字符={r.get('char_total')} 本次给={r.get('chars')} 截断={r.get('truncated')}")
check("kb_read 回了全文总长（与本次给了多少分开）", (r.get("char_total") or 0) > 0, True)
check("kb_read 截断如实标注", isinstance(r.get("truncated"), bool), True)
check("kb_read 的 text 真的被 cap 住", len(str(r.get("text") or "")) <= 1200, True)

r = json.loads(fn["kb_read"](doc_id="kb不存在的号"))
check("不存在的 id ⇒ ok=False（不静默）", r.get("ok"), False)
check("不存在的 id ⇒ 给出提示而不是干巴巴报错", bool(r.get("hint")), True)

print()
print("=" * 78)
print("D · 库位置可指定（多课题/多学生铺路）：set_data_dir 真的换库了吗")
print("=" * 78)
sys.path.insert(0, str(WB))
import litkb  # noqa: E402

default_root = litkb.set_data_dir(None)
check("默认库 = 引擎下的 data/litkb", str(default_root).replace("\\", "/").endswith("/data/litkb"), True)
other = default_root.parent / "_kb_switch_probe"
got = litkb.set_data_dir(other)
check("切到新目录后 DATA_DIR 真的变了", str(got), str(other.resolve()))
check("LIBRARY / DB_PATH 跟着一起走", 
      (str(litkb.LIBRARY), str(litkb.DB_PATH)),
      (str(other.resolve() / "library.json"), str(other.resolve() / "index.db")))

# ★ 2026-09-22 新增：**切过去的库必须立即可用**。
#   血债：`set_data_dir()` 原来只换三个全局、**不建表**，而 `init_db()` 全仓只有
#   CLI 的 `main()` 调过一次 ⇒ 任何新库根都是坏的：条目照样写进 `library.json`
#   （那一步先跑），FTS 表却不存在 ⇒ `reindex_item()` 抛 `no such table: fts_doc`
#   ⇒ `add_pdf_file()` 半途炸掉。
#   ⚠️ 症状极具误导性：**库里有这一篇、检索却什么都搜不到**，
#   而"检索不到"最容易被读成"这篇根本没入库"。
#   ★ 我原来的这一节**正好漏了它** —— 只量了三个全局变没变（中间量），
#   没量"切过去的库能不能用"（最终行为）。判据量中间量 = 白写。
import sqlite3  # noqa: E402

_con = sqlite3.connect(str(litkb.DB_PATH))
_tables = {r[0] for r in _con.execute("SELECT name FROM sqlite_master")}
_con.close()
check("★★ 切过去的新库已建表（否则入库半途炸、检索全空）",
      {"fts_meta", "fts_doc", "fts_para"} <= _tables, True)
r = json.loads(fn["kb_search"]("随便一个词", n=3, library=str(other)))
check("★★ 对新库检索不报错（0 命中 ≠ no such table）", r.get("ok"), True)
check("  新库确实是空的", r.get("n"), 0)

litkb.set_data_dir(None)
check("切回默认（幂等）", str(litkb.DATA_DIR), str(default_root))
r = json.loads(fn["kb_list"]())          # 切回来之后仍要读到真库
check("切回默认后仍读到真实库", (r.get("total") or 0) > 0, True)

print()
print("=" * 78)
print("E · 2026-09-22 新补的 5 个工具：真调一次（可达 ≠ 好用）")
print("=" * 78)

args = {"query": "tuberculosis vaccine BCG", "n": 3}
r = json.loads(fn["kb_rank"](**args))
print(f"  kb_rank      ok={r.get('ok')} n={r.get('n')} total={r.get('total')}")
check("kb_rank 回了结果", r.get("ok"), True)
check("kb_rank 确实排出了条目", (r.get("n") or 0) > 0, True)
if r.get("ranked"):
    print(f"  首位: {json.dumps(r['ranked'][0], ensure_ascii=False)[:200]}")
    truthy("排名条目带 id", r["ranked"][0].get("id"))
# 负控：**判据不能依赖库的内容**。我上一版拿"真菌 融合"当负控，
# 那是**当前**这个 BCG 演示库不匹配而已 —— 用户哪天真的入了一批真菌文献，
# 这条负控就会红，而红的样子像"排序坏了"。改用一个绝不可能有的词。
r = json.loads(fn["kb_rank"](query="zzqqxx_no_such_topic_zzqqxx", n=3))
check("★ 负控：查一个不存在的词 ⇒ ok=True 且 0 条（不是报错）",
      (r.get("ok"), r.get("n")), (True, 0))

r = json.loads(fn["kb_bibtex"]())
print(f"  kb_bibtex    ok={r.get('ok')} n={r.get('n')} entries={r.get('entries')}")
check("kb_bibtex 抽出了条目", r.get("ok"), True)
check("★ 给的是真 BibTeX（以 @ 开头）", str(r.get("bibtex") or "")[:1], "@")
check("entries 数与条目数一致", r.get("entries"), r.get("n"))

# ── 认不出的 id：必须原样报回来，不许静默丢弃 ──
r = json.loads(fn["kb_compare"](ids="kb0001,kb0002"))
print(f"  kb_compare   ok={r.get('ok')} error={str(r.get('error'))[:60]}")
truthy("★ 负控：编造的 id ⇒ 原样报回来（不静默丢成'0 篇'）", r.get("missing"))

# ── AI 依赖的工具：失败必须**指对根因** ──
# ★ 这条判据刻意写成"话与结果一致"，而不是"A 必须成立"：
#   写死"kb_review 必须失败"是个**定时炸弹** —— 用户哪天把 AI Key 配上，
#   它就会红，而红的时候代码一行没坏。
#   正确写法：先读配置**事实**，再要求工具的行为与之相符。
_cfg_p = WB / "app_config.json"
try:
    _ai_key = str((((json.loads(_cfg_p.read_text(encoding="utf-8"))).get("ai")
                    or {}).get("api_key") or "")).strip()
except Exception:                              # noqa: BLE001
    _ai_key = ""
print(f"  AI 凭证（读 {_cfg_p.name}）：{'已配' if _ai_key else '未配'}")
r = json.loads(fn["kb_review"](ids=doc_id))
if _ai_key:
    check("AI 已配 ⇒ kb_review 应当出得来草稿", r.get("ok"), True)
else:
    check("★ AI 未配 ⇒ kb_review 必须说实话（ok=False）", r.get("ok"), False)
    truthy("★ 且 hint 指向真根因（是 AI 配置，不是这个工具的毛病）",
           "AI" in str(r.get("hint") or ""))

# ── kb_ingest：dry_run 必须**一个字节都不动库** ──
_before = json.loads(fn["kb_list"](limit=1)).get("total")
_pdf = str(ROOT / "figures" / "fig_conversion.pdf")
r = json.loads(fn["kb_ingest"](paths=_pdf, dry_run=True))
print(f"  kb_ingest    dry_run ok={r.get('ok')} candidates={r.get('candidates')} "
      f"would_add={r.get('would_add')} would_skip={r.get('would_skip')}")
check("dry_run 回了计划", r.get("dry_run"), True)
check("candidates=1（那个文件确实在）", r.get("candidates"), 1)
_after = json.loads(fn["kb_list"](limit=1)).get("total")
check("★★ dry_run 真的没动库（前后总数一致）", (_before, _after), (_before, _before))

r = json.loads(fn["kb_ingest"](paths="D:/__no_such_dir__/nope.pdf", dry_run=True))
check("★ 负控：不存在的路径 ⇒ 写进 unmatched（不静默当没给过）",
      bool(r.get("unmatched")), True)

# ── 下载兜底开关：配置 → schubdl 真的推下去了没有（2026-10-12 加）──
# ★ 为什么这条判据必须有：schubdl 自己有个**环境变量**开关，而用户是**在
#   config.json 里配**的。两套开关各自为政的话，"我明明配了 True 却没生效"
#   就是必然 —— 而且它**不报错**，只是不按你配的来（本仓最防的静默失效）。
#   所以这里量的是**接线**：改配置 ⇒ 生效；拿不到配置 ⇒ 保持关（不许默认开）。
#
# ★★ 2026-10-12 负控抓到的一处**假判据**（记在这里，别再犯）：
#   第一版我直接调 `_apply_kb_download_switch(WB)` —— 于是把
#   `_import_workbench` 里那一行**删掉**，这几条判据照样全绿，因为它们只是在
#   测"那个函数自己能不能跑"。而真正要守的是"**它有没有被接上**"。
#   ⇒ 判据必须打在**新增的接线**上：走 `_import_workbench` 这个唯一入口，
#     再断言开关状态 —— 那天谁把那一行删了，这里就当场红。
from rongke import bridge as _bridge  # noqa: E402
import schubdl as _schubdl  # noqa: E402   （wb 已被 install_workbench 挂上 path）


class _FakeCfg:
    def __init__(self, val):
        self.kb = type("K", (), {"scihub_fallback": val})()
        self.runtime = type("R", (), {"workbench": WB})()


_orig_load = Config.load


def _via_import(val):
    """把假配置装上，然后走**真入口**（_import_workbench）跑一遍接线。"""
    Config.load = classmethod(lambda cls, root=None: _FakeCfg(val))
    try:
        _bridge._import_workbench(WB)
    finally:
        Config.load = _orig_load


_schubdl.enable(False)
_via_import(True)
check("★ 配置 scihub_fallback=True ⇒ 走导入入口后开关真的开了",
      _schubdl.enabled(), True)

_schubdl.enable(True)
_via_import(False)
check("★ 配置 =False ⇒ 关得回去（不是只能开不能关）", _schubdl.enabled(), False)


def _boom(cls, root=None):
    raise RuntimeError("模拟配置读不到")


_schubdl.enable(False)
Config.load = classmethod(_boom)
try:
    _bridge._import_workbench(WB)
finally:
    Config.load = _orig_load
check("★★ 负控：拿不到配置 ⇒ 保持关（拿不到依据时不许默认打开）",
      _schubdl.enabled(), False)
Config.load = _orig_load

# ★ 2026-10-12 用户把真 config.json 的 scihub_fallback 打开成了 true
#   （产品要给别人用，机构授权那条路绑作者本人身份、对用户是死的 ⇒ 走兜底路）。
#   这里原来钉的是 "真配置默认 = False"，那条判据**正确地红了** —— 它守住了
#   一次真实的配置变更，是好判据，只是它守的**对象**变了：
#   现在该守的不再是"默认关"这个**取值**，而是下面这两件**机制**：
#     ① 这个开关真的来自 config.json（而不是代码里写死 true）
#     ② 用户在配置里写什么，推下去的就是什么（写 False 能关回去 —— 已在上面测过）
#   换句话说：把"默认必须是关"降级成"必须由配置决定"。
#   为什么这么改是对的：默认值属于**产品决策**，会随用户意图变（今天已经变过一次）；
#   而"配置说了算"是**工程约束**，不随意图变。判据钉后者才不会每次改配置都误报。
_cfg_now = Config.load().kb.scihub_fallback
check("真 config.json 里的 scihub_fallback 是可读的布尔值（开关由配置决定，不是写死）",
      isinstance(_cfg_now, bool), True)
# 最强的一条：拿真配置跑一遍真入口，看开关是否**恰好等于**配置写的值。
# 这同时覆盖了"能开"和"能关"两个方向（取决于用户当前配置），且不预设取值。
_schubdl.enable(not _cfg_now)                     # 先设成反的，证明下面确实被推翻了
_bridge._import_workbench(WB)
check(f"★ 真 config.json = {_cfg_now} ⇒ 走真入口后开关恰好等于它（配置说了算）",
      _schubdl.enabled(), _cfg_now)
_schubdl.enable(False)

# 探针库挪走（**不删**）。原来这一节留下的 `_kb_switch_probe` 一直没人收 ——
# 攒着也是攒着，顺手收掉。
T.stash(other, "kb-switch-probe")

print()
print("=" * 78)
print(f"结果：{ok_n} 项通过 / {bad_n} 项失败")
print("=" * 78)
# 行首 `[DONE]` 是给 `regress.py` 认的收尾标记（它在日志里找这一行）。
print("[DONE] 知识库工具层验收全部通过" if not bad_n
      else "[DONE] 知识库工具层验收有失败项")
sys.exit(1 if bad_n else 0)
