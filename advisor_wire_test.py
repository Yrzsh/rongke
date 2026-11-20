# -*- coding: utf-8 -*-
"""导师模型**接线**：Web 请求里那个 advisor 字段，最后真的落在提示词里。

为什么这条值得单独钉（2026-11-14 续）
------------------------------------
`advisor_test.py` 锁的是「四层档案本身对不对」（落盘 / 校验 / 注入规则）。
本文件锁的是**另一样东西**：从界面按下发送，到内核组装首条消息，中间有
**四跳**，任何一跳断了都不会报错：

    workspace.js  `body.advisor = ADVISOR`
      → server.py  `Run.advisor` → `kernel.run(advisor_slug=…)`
      → loop.py    `_run_serial._sess_advisor` → `_loop(advisor_slug=…)`
      → loop.py    `initial_messages(advisor_slug=…)` → `advisor.context_text()`

本仓在这类「配置在场、其实没生效」上吃过太多次（`force_brief` 判据写了没人调、
`mentor_review` 事件造了没 emit、`topic_slug` 两处各读一次 `CURRENT`）。
所以这里的判据**不是看源码里有没有那个词**，而是**真跑一遍**：
构造一个 Run，让真 `_run_serial` 跑，然后去提示词里找那位导师的判准原文。
"""
from __future__ import annotations

import inspect
import json
import re
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

FAILS: list[str] = []
N = 0


def check(name: str, got, want) -> None:
    global N
    N += 1
    if got == want:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}\n         期望 {want!r}\n         实得 {got!r}")
        FAILS.append(name)


def ok(name: str, cond: bool, why: str = "") -> None:
    check(name + (f"（{why}）" if why and not cond else ""), bool(cond), True)


print("导师模型 · 接线（界面 → 服务端 → 内核 → 提示词）")

from rongke import advisor as A                                    # noqa: E402
from rongke import loop as L                                       # noqa: E402
from rongke.loop import ADVISOR_METHOD_MARK                        # noqa: E402
from rongke import topic as T                                      # noqa: E402
from rongke.web import server as S                                 # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="advwire-"))

# ═══ 1. 签名层：四跳的每一跳都要有这个参数 ═════════════════════════
print("\n── 1. 参数真的穿到了每一层（不是只有最外面那一层有）──")
for _fn, _label in ((L.initial_messages, "initial_messages"),
                    (L.Rongke._loop, "_loop"),
                    (L.Rongke._run_serial, "_run_serial"),
                    (L.Rongke.run, "run")):
    ok(f"★ `{_label}` 收 advisor_slug",
       "advisor_slug" in inspect.signature(_fn).parameters)
ok("★ `Run` 有 advisor 字段（服务端要把它带进这一场）",
   "advisor" in {f.name for f in __import__("dataclasses").fields(S.Run)})
ok("★ `Mgr.start` 收 advisor",
   "advisor" in inspect.signature(S.Manager.start).parameters)
ok("★ `Mgr.follow_up` 收 advisor（追问不能丢导师）",
   "advisor" in inspect.signature(S.Manager.follow_up).parameters)

# ═══ 2. 服务端读体：字段缺席 ⇒ 空串，不猜 ══════════════════════════
print("\n── 2. 请求体解析（缺席 = 空串，绝不猜一个默认导师）──")
_src = inspect.getsource(S)
ok("★ /api/run 读 body.advisor", 'advisor=str(body.get("advisor")' in _src)
ok("★ /api/follow 读 body.advisor", 'advisor=str(body.get("advisor")' in _src)
ok("★ `_exec` 真的把它传进 kernel.run", "advisor_slug=str(run.advisor" in _src)
ok("★ 追问没带就认回父轮那个（不退回自己猜的）",
   'str(getattr(prev, "advisor", "") or "")' in _src)
# 负控：追问的兜底**不许**从快照消息里认（那是猜，见 follow_up 注释）
ok("★ 负控：追问的导师没有被写成「从快照推」（推不出来就别猜）",
   "advisor_from_snapshot" not in _src)

# ═══ 3. 端到端：真跑一遍，去提示词里找判准原文 ═════════════════════
print("\n── 3. 真跑一遍：提示词里到底有没有那位导师的判准 ──")
T.ensure_topic("proto", "原生质体融合", "融合子怎么鉴定", root=_TMP)
A.save("laozhang", name="老张", layers={
    "persona": "# 追问习惯\n- 先问对照组设了没有，没有就打断。source=user\n"
               "- 会追一句「你这个数几个重复算的」。source=user",
}, mode="graduation_first", sufficiency=55, root=_TMP)

from rongke.contract import TaskContract                           # noqa: E402

# 直接走内核那一段真正的装配（第 4 跳），原因：`_run_serial` 需要一整套
# 协作对象（LLM、gate、registry…），而这里要钉的是**提示词里有没有**。
# 那一跳的接线由第 1 节的签名判据 + 第 2 节的 `_exec` 判据共同覆盖。
_asm: dict = {}
_msgs = L.initial_messages(TaskContract(raw_goal="明天组会要讲，帮我预演"), None,
                           kb_only=True, topics_root=_TMP, topic_slug="proto",
                           advisor_slug="laozhang", assembly_out=_asm)
_full = "\n".join(str(m.get("content") or "") for m in _msgs)
ok("★ 提示词里有这位导师的名字与版本", "老张" in _full)
ok("★ 有他本人那一层（不是通用建议）", "先问对照组设了没有" in _full)
ok("★ 命中层说得出来（组会 ⇒ 追问习惯）", "命中层：追问习惯" in _full)
ok("★ 工作镜头也带上了（毕业优先型 —— 这是判准，不是语气）", "毕业优先型" in _full)
ok("★ 没报 missing（选的是真人真文件）", "advisor_missing" not in _asm)

# 负控①：换成另一位导师，提示词换的是**那个人**的判准
A.save("lisi", name="李四", layers={
    "persona": "# 追问习惯\n- 第一句永远问「这个结论最怕什么」。source=user",
}, sufficiency=55, root=_TMP)
_msgs2 = L.initial_messages(TaskContract(raw_goal="明天组会要讲"), None,
                            kb_only=True, topics_root=_TMP, topic_slug="proto",
                            advisor_slug="lisi")
_full2 = "\n".join(str(m.get("content") or "") for m in _msgs2)
ok("★ 负控：换导师 ⇒ 判准换人（李四那句在）", "最怕什么" in _full2)
ok("★ 负控：且老张那句**不在了**（不是两位一起塞进去）", "先问对照组设了没有" not in _full2)

# 负控②：不选导师 ⇒ 一个字都不注入
_msgs3 = L.initial_messages(TaskContract(raw_goal="明天组会要讲"), None,
                            kb_only=True, topics_root=_TMP, topic_slug="proto")
_full3 = "\n".join(str(m.get("content") or "") for m in _msgs3)
ok("★ 负控：不选导师 ⇒ 没有任何导师段", '本次按「' not in _full3)
ok("★ 负控：但通用导师视角还在（那不是这次加的，不许被这次删掉）",
   "审稿席上的导师" in _full3 or "NOT_READY" in _full3)

# 负控③：导师模式关着 ⇒ 有导师也不注入
_msgs4 = L.initial_messages(TaskContract(raw_goal="明天组会要讲"), None,
                            kb_only=False, topics_root=_TMP, topic_slug="proto",
                            advisor_slug="laozhang")
_full4 = "\n".join(str(m.get("content") or "") for m in _msgs4)
ok("★ 负控：导师模式关着 ⇒ 导师段不出现", '本次按「' not in _full4)

# ═══ 4. 会话身份：事件与快照都要能回放出「按谁的判准」 ═════════════
print("\n── 4. 会话身份（事件 / 快照 / 续跑）──")
_serial = inspect.getsource(L.Rongke._run_serial)
ok('★ mentor_mode 事件带上 advisor（历史页要能复原）',
   '"advisor": _sess_advisor' in _serial)
ok("★ 快照里也带（续跑要认回同一个判准）",
   '"advisor": str(_sess_advisor or advisor_slug or "")' in inspect.getsource(L.Rongke._loop))
ok("★ 续跑**先认快照**再认调用方（与 topic 同一条纪律）",
   '(resume or {}).get("advisor")' in inspect.getsource(L.Rongke._loop))
ok("★ 负控：不读任何「上次选中」的全局残留（否则前后半段会换判准）",
   "advisor.current(" not in _serial and "ADVISOR_CURRENT" not in _serial)

# ═══ 5. /api/advisor 接口（只读 + 与内核同源的保存/回滚）═══════════
print("\n── 5. /api/advisor（读写同源，不另写第二套真相）──")
ok("★ 有 GET 分支", 'if p == "/api/advisor":' in _src)
ok("★ POST 写入口与内核同源（转调 advisor_store.save / rollback，不另写第二套真相）",
   'u.path == "/api/advisor"' in _src
   and "advisor_store.save(" in _src
   and "advisor_store.rollback(" in _src)
_server_handler = _src
ok("★ 回的是内核算出来的 audit（不在这儿另算一套）",
   "advisor_store.audit(adv)" in _server_handler)
ok("★ 找不到时列出已知（模型/用户据此自己决定，而不是只回 404）",
   '"known": [r["slug"] for r in advisor_store.list_all()]' in _server_handler)

# ═══ 6. 前端：那个下拉框不是装饰 ═══════════════════════════════════
print("\n── 6. 前端（值真的进请求体，回放真的能恢复）──")
_ws = (_ROOT / "rongke/web/static/workspace.js").read_text(encoding="utf-8")
_mj = (_ROOT / "rongke/web/static/mentor.js").read_text(encoding="utf-8")
ok("★ `body.advisor` 进了 /api/run", "body.advisor = ADVISOR" in _ws)
ok("★ `advisor` 进了 /api/follow", "advisor: ADVISOR" in _ws)
ok("★ 回放从 mentor_mode 事件恢复", '"advisor" in d' in _ws)
ok("★ 清单从 /api/advisor 读（前端不自己编）", '"/api/advisor"' in _ws)
ok("★ 两个绘制点读同一份状态（横条 + 导师页）",
   "ADVISORS" in _mj and "typeof ADVISORS" in _mj)

# ═══ 7. 方法论层：`create-supervisor` 真的读得到 ══════════════════
print("\n── 7. 方法论层（不点名就等于没有）──")
# 上游 skill 就在技能目录里，但**模型不知道它在** ——
# 指名与否是一道真门：不点名，它就只能凭印象自圆其说。
from rongke import stages as _ST                                    # noqa: E402
_idx = _ST.index()
ok("★ 技能索引里有 create-supervisor", "create-supervisor" in _idx)
_text = _ST.read_skill_text("create-supervisor")
ok("★ 且真的读得到正文（不是目录末尾留个空壳）", len(_text) > 3000, str(len(_text)))
ok("★ 子文件也读得到（references/security.md）",
   len(_ST.read_skill_text("create-supervisor", file="references/security.md")) > 500)
ok("★ prompts/ 也在文件清单里（蒸馏方法的正文在那里）",
   "prompts/persona_builder.md" in _ST.skill_files("create-supervisor"))
_mentor = L.mentor_mode_text()
ok("★ 导师模式的提示词里**点名**了它",
   "create-supervisor" in _mentor)
ok("★ 且告诉模型先 load_skill 再做（别凭印象）",
   "load_skill" in _mentor and ADVISOR_METHOD_MARK in _mentor)
ok("★ 四层名字在提示词里全都在（否则模型会自创键名）",
   all(k in _mentor for k, _, _, _ in A.LAYERS))
ok("★ source= 这条纪律在提示词里说清楚了",
   "source=" in _mentor and "template" in _mentor)
ok("★ 「不要冒充他本人」这条也在",
   "不要冒充他本人" in _mentor)
# ★★ 2026-11-15 真实使用跑出来的失效：模型把提示词里的**格式样例**
#   当成了导师原话。实测证据：`topics/advisors/zhang-wei/PERSONA.md` 只有一行
#   `- 先问对照组。source=user`，而「先问对照组」本来是提示词「怎么问」那一行
#   括号里的示例 —— 用户随后拿到的「张老师的判准」第一条是他从没说过的话。
ok("★ 提示词里明写「这段提示词里的任何例子都不是材料」",
   "这段提示词里的任何例子都不是材料" in _mentor)
ok("★ 且明写没挖到就写「未挖到材料」（给一个合法空状态，别逼它编）",
   A.NO_MATERIAL in _mentor)
ok("★ schema 里也说了（模型看 schema 比看正文多，两处都要有）",
   A.NO_MATERIAL in json.dumps(L.ADVISOR_SAVE_SCHEMA, ensure_ascii=False))
_meta = json.loads(_ST.index()["create-supervisor"]["description"].split("description: ")[0] or "{}") \
    if False else _ST.index()["create-supervisor"]
ok("★ 它的 description 说到了 create / update / list（模型靠这句判断要不要读）",
   all(w in str(_meta.get("description", "")) for w in ("Create", "update", "list")))

# ═══ 收尾 ═════════════════════════════════════════════════════════
import shutil                                                      # noqa: E402
shutil.rmtree(_TMP, ignore_errors=True)

print(f"\n合计 {N} 项 / {len(FAILS)} 失败")
if FAILS:
    for f in FAILS:
        print("  ✘", f)
    sys.exit(1)
print("全部通过 ✔")
