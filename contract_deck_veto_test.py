# -*- coding: utf-8 -*-
"""常驻判据：契约不许同时写着「禁止做 PPT」与「needs_deck=true」。

## 事故（真 HTTP 写作场，2026-11-05）

`_realp/run_writing_long_probe.py` 用一句纯写作目标（明写「不用做 PPT」）真跑：
契约 `forbidden=["做 PPT"]` 却 `needs_deck=true` ⇒ 第 9 条硬门反复要求
`audit_deck`（必须先有 .pptx），而契约禁止产出 .pptx ⇒ **不可能通过的门**。
两场都被打回 4 次、132s / 90s 后报阻塞（`runs/20261105-193745` 等）。

## 判据口径

- **正控**：真要做 PPT 仍认（`needs_deck=True`），窄表「整理组会记录」仍不认；
- **负控**：否定句里的 `ppt` 不再抬起意图位；模型自报 true 也要被自洽校正压住；
- **三条构造路径都量**：`from_llm` / `fallback` / 直接构造；
- **两个方向都量**：同一份 GOAL 把「不用做 PPT」换成「并做一版 PPT」必须翻成真；
- **单源**：否定判据复用 `stages._only_negated`，不许在 contract.py 另抄一份。

判据格式：逐条 `[PASS]`、结尾 `[DONE]`（`regress.py:_RE_MARK` 只认这些标记）。
"""
from __future__ import annotations

import ast
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

_N = 0
_FAIL = 0


def check(name: str, cond, detail: str = "") -> bool:
    global _N, _FAIL
    _N += 1
    ok = bool(cond)
    if not ok:
        _FAIL += 1
        print(f"[FAIL] {name} {detail}")
    else:
        print(f"[PASS] {name}")
    return ok


ACCIDENT_GOAL = (
    "请就「为什么科研写作要先写清方法与数据、再写结论」写一段约 800 字的正文，"
    "分三段，直接给出全文。不用查资料、不用写文件、不用做 PPT；"
    "交付时把正文全文原样附在答复里。"
)
ACCIDENT_PAYLOAD = {
    "goal": "写一段约800字、分三段的正文，论述为什么科研写作要先写清方法与数据、再写结论，并直接给出全文。",
    "deliverables": ["约800字、分三段的正文全文"],
    "acceptance": ["正文共三段", "正文总字数约800字",
                   "正文直接出现在答复中且完整无省略",
                   "正文内容围绕“先写清方法与数据、再写结论”展开"],
    "constraints": ["约800字", "分三段", "直接给出全文", "不查资料",
                    "不写文件", "不做 PPT", "交付时把正文全文原样附在答复里"],
    "forbidden": ["查资料", "写文件", "做 PPT"],
    "needs_writing": True,
    "needs_figure": False,
    "needs_deck": True,          # ← 事故现场：模型自报 true，与 forbidden 打架
    "needs_kb": False,
    "answer_only": False,
}


def main() -> int:
    print("== 1 词表层：否定语境不再抬起 deck 意图 ==")
    from rongke.contract import _looks_like_deck, TaskContract, DRAFT_SYSTEM
    from rongke.stages import infer_stages, _only_negated

    check("★ 负控：'不用做 PPT' 不再被认成要做 deck",
          not _looks_like_deck("写一段正文，不用做 PPT"))
    check("★ 负控：'不做演示文稿' 不再被认成要做 deck",
          not _looks_like_deck("写一段正文，不做演示文稿"))
    check("正控：'做一份组会 PPT' 仍认", _looks_like_deck("做一份组会 PPT"))
    check("正控：'把论文做成 PPT' 仍认", _looks_like_deck("把论文做成 PPT"))
    check("既有窄表不变：'整理组会记录' 仍不认",
          not _looks_like_deck("整理组会记录"))
    check("★ 一否一肯不误杀：'不用做 PPT，另外做一份组会汇报' 仍认",
          _looks_like_deck("不用做 PPT，另外做一份组会汇报"))
    check("事故 GOAL 的词表判定为假", not _looks_like_deck(ACCIDENT_GOAL))

    print("== 2 契约层：三条构造路径都守自洽不变量 ==")
    c = TaskContract.from_llm(ACCIDENT_GOAL, dict(ACCIDENT_PAYLOAD))
    check("★ 事故现场复现后 needs_deck=False（模型自报 true 也被压住）",
          c.needs_deck is False, str(c.needs_deck))
    check("★ 自洽校正没把写作位一起关掉", c.needs_writing is True)
    check("★ 阶段里不推 slides", "slides" not in (c.stages or []),
          str(c.stages))
    check("★ plan/to_prompt 不再注入 PPT 开工纪律",
          "本任务的产物是 **.pptx**" not in (c.to_prompt() or ""))

    f = TaskContract.fallback(ACCIDENT_GOAL)
    check("★ fallback（起草失败）路径也不判要 deck", f.needs_deck is False,
          str(f.needs_deck))

    d = TaskContract(raw_goal=ACCIDENT_GOAL, needs_writing=True,
                     needs_deck=True, forbidden=["做 PPT"],
                     constraints=["不做 PPT"], stages=["draft"])
    check("★ 调用方直接构造也被校正（三条路径同一条不变量）",
          d.needs_deck is False, str(d.needs_deck))

    print("== 3 两个方向都量：换成真要做，必须翻回 True ==")
    flipped_goal = ACCIDENT_GOAL.replace("不用做 PPT", "并做一版 PPT")
    flipped_payload = dict(ACCIDENT_PAYLOAD)
    flipped_payload["forbidden"] = ["查资料", "写文件"]
    flipped_payload["constraints"] = [x for x in ACCIDENT_PAYLOAD["constraints"]
                                      if "PPT" not in x]
    flipped_payload["needs_deck"] = False
    g = TaskContract.from_llm(flipped_goal, flipped_payload)
    check("★ 同一 GOAL 改成要做 PPT ⇒ needs_deck=True",
          g.needs_deck is True, str(g.needs_deck))
    check("★ 反向场仍推得出 slides 阶段", "slides" in (g.stages or []),
          str(g.stages))
    check("★ 反向场 to_prompt 会注入 PPT 开工纪律",
          "本任务的产物是 **.pptx**" in (g.to_prompt() or ""))

    print("== 4 forbiddden 清单本身算否决，不依赖原话有没有「不」字 ==")
    h = TaskContract(raw_goal="写一段正文", needs_deck=True,
                     forbidden=["做 PPT"], stages=["draft"])
    check("★ forbidden 含 deck 词即否决（清单语义自带否定）",
          h.needs_deck is False)
    check("★ _user_vetoed_deck 对外可读（判据不靠猜）",
          h._user_vetoed_deck is True)
    i = TaskContract(raw_goal="写一段正文", needs_deck=True,
                     forbidden=["查资料"], stages=["draft"])
    check("负控：forbidden 里没有 deck 词 ⇒ 不否决",
          i.needs_deck is True and i._user_vetoed_deck is False)

    print("== 5 单源与提示词（不许另抄一份否定机器）==")
    src = (_ROOT / "rongke" / "contract.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "stages":
            imported.update(a.name for a in node.names)
    check("★ contract.py 从 stages 导入 _only_negated（单源）",
          "_only_negated" in imported, str(sorted(imported)))
    check("★ contract.py 没有自己定义 _NEGATIONS 这类第二份词表",
          "_NEGATIONS" not in src and "_NEG_TAIL" not in src)
    check("★ 起草提示词里明写禁止与 needs_deck 必须自洽",
          "否定句里出现 \"PPT\"" in DRAFT_SYSTEM)
    _look = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "_looks_like_deck")
    _look_calls = {c.func.id for c in ast.walk(_look)
                   if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    check("★ _looks_like_deck 真的调用了单源否定机器（防止以后被改回裸词表）",
          "_only_negated" in _look_calls, str(sorted(_look_calls)))
    check("★ 事故 GOAL 的 infer_stages 不推 slides",
          "slides" not in [s.id for s in infer_stages(ACCIDENT_GOAL)])

    print(f"[DONE] {_N} 项 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
