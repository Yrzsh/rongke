# -*- coding: utf-8 -*-
"""知识库 / 导师模式「交付复核」的**常驻**验收探针（挂进 regress.py）。

2026-11-04 真跑抓到的四类缺陷，每一类都有正负控钉在这里：

  K1 复核覆盖错位 ── 只在 kb_only 下跑、只读 `final` 不读落盘稿、
     引注正则只认紧贴标记 ⇒ 普通任务查了 6 次库却完全没复核；
     写稿任务答复空白而盘上有 227 字，复核判「0 处出处」。
  K2 「读不到」被写成违规 ── 正文有 9 个 doc_id、11 处 p.N，
     只因格式是散文式就判「用了不说，等于没依据」，界面还弹「审计未通过」。
  K3 死代码 ── `kb_library_ids()` 单独跑能取 252 篇，产品里零消费点；
     `renderMentorAudit` 有 case 却无任何 emit ⇒ 编造 id 那层永久免检。
  K4 自报数字 ≠ 读回结果 ── 短词救援追加 hits 后不更新 n。

判据只认**读回来的结果**（函数调用 / 源码接线），不采信任何自述。
纯标准库 + 临时目录，两个解释器下应得同一个数。
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import loop as L                     # noqa: E402
from rongke import bridge as B                   # noqa: E402
from rongke.loop import Rongke                   # noqa: E402

FAILS: list[str] = []
N = 0


def check(label: str, got, want=True) -> None:
    global N
    N += 1
    ok = bool(got) if want is True else got == want
    if not ok:
        FAILS.append(f"{label}：得到 {got!r}，期望 {want!r}")
        print(f"  [FAIL] {label}  got={got!r}")
    else:
        # ★ 必须打 `[PASS]`（不是 ✓ U+2713）：regress.py 的 `_RE_MARK`
        #   只认 `[PASS|FAIL|OK|BAD]` / ✔ / ✘，✓ 会把整套件计成 1 项。
        print(f"  [PASS] {label}")


def main() -> int:
    print("── 1. 出处抽取：规范标记 / 散文式 / 负控（只认读得回来的） ──")
    c1 = L.extract_citations("〔库·kb12345678 p.1〕 结论成立。")
    check("规范标记读成 1 处 tight 引注", len(c1), 1)
    check("tight 的 doc_id / 页码解析正确",
          (c1[0]["doc_id"], c1[0]["page"], c1[0]["form"]),
          ("kb12345678", 1, "tight"))
    c2 = L.extract_citations("文献 kb12345678 的结果见 p.3 与 P4。")
    check("散文式（id 与页码分开写）读得到，不再判「一处都没有」",
          len(c2), 2)
    check("散文式的 form 标成 prose", {x["form"] for x in c2}, {"prose"})
    check("负控：无 kb 前缀的十六进制串不算 doc_id",
          L.extract_citations("deadbeef1234 p.2"), [])
    check("负控：tight 已标过的 id 不许被辖区法再配一次页码",
          len(L.extract_citations("〔库·kb12345678 p.1〕后面又写 p.7")), 1)
    check("负控：页码不许跨到下一个 doc_id 的辖区",
          [x["doc_id"] for x in L.extract_citations(
              "kb12345678 第一段。kb87654321 第二段 p.9")], ["kb87654321"])
    check("同一 (doc_id,page) 去重",
          len(L.extract_citations("〔库·kb12345678 p.1〕与〔库·kb12345678 p.1〕")), 1)

    print("\n── 2. 正文单源：final ∪ 落盘稿（去重、按后缀） ──")
    td = Path(tempfile.mkdtemp(prefix="kbmentor_audit_"))
    (td / "kb_summary.md").write_text("盘上正文 〔库·kb12345678 p.1〕", encoding="utf-8")
    (td / "chart.png").write_bytes(b"\x89PNG\r\n")
    (td / "meta.json").write_text("{}", encoding="utf-8")
    texts, sources = L.collect_delivery_text("聊天答复正文", td)
    check("聊天答复 + 落盘正文都进 texts", len(texts), 2)
    check("来源清单如实点名读过的文件", any("kb_summary.md" in s for s in sources), True)
    check("负控：图片 / JSON 不算正文",
          any(("chart.png" in s or "meta.json" in s) for s in sources), False)
    same = Path(tempfile.mkdtemp(prefix="kbmentor_audit_same_"))
    (same / "draft.md").write_text("同一段稿子", encoding="utf-8")
    check("final 与落盘同稿 ⇒ 只收一次（不把一处出处算成两处）",
          len(L.collect_delivery_text("同一段稿子", same)[0]), 1)
    check("负控：root=None 时只有聊天答复一段",
          len(L.collect_delivery_text("答复", None)[0]), 1)

    print("\n── 3. 行为触发：开关不再决定「要不要诚实」 ──")
    hit = json.dumps({"hits": [{"doc_id": "kb12345678", "page": 1}]},
                     ensure_ascii=False)
    calls = [NS(seq=1, name="kb_search", ok=True, args={}, result=hit, error="")]
    check("负控：既没查库也没写 doc_id ⇒ 不复核（不误触发）",
          L.should_audit(False, [], ["随便聊两句"]), False)
    check("★ 普通任务查过库 ⇒ 必须复核（实测 A 场漏判的那条）",
          L.should_audit(False, calls, ["随便聊两句"]), True)
    check("★ 正文出现 doc_id ⇒ 必须复核（落盘稿引注也能触发）",
          L.should_audit(False, [], ["参考 kb12345678"]), True)
    check("导师模式开关仍保留触发", L.should_audit(True, [], [""]), True)

    print("\n── 4. 三态 verdict：pass / fail / unknown，两方向都量 ──")
    lib = {"kb12345678"}
    a_pass = L._run_trace_audit(calls, "〔库·kb12345678 p.1〕 有出处",
                                library_ids=lib, require_library=False)
    check("正控：查库 + 出处对得上命中集 ⇒ pass", a_pass["verdict"], "pass")
    check("正控不带任何违规文案", a_pass["violations"], [])
    a_fail = L._run_trace_audit(calls, "一段完全没出处的回答",
                                library_ids=lib, require_library=False)
    check("负控：查了库、库里命中、却一处没标 ⇒ fail（不能丢）",
          a_fail["verdict"], "fail")
    check("负控的 violations 真的有话可说", len(a_fail["violations"]) >= 1, True)
    a_unk = L._run_trace_audit(calls, "正文出现 kb12345678 但没写页码",
                               library_ids=lib, require_library=False)
    check("★ 有 doc_id 却读不出页码 ⇒ unknown，不是 fail",
          a_unk["verdict"], "unknown")
    check("★ unknown 不许写进 violations（读不到 ≠ 不符合）",
          a_unk["violations"], [])
    check("unknown 必须给出原因，供界面显示「未测」",
          len(a_unk["unknown_reasons"]) >= 1, True)
    check("unknown 的 summary 说「未测」，不许说「零处出处」",
          "未测" in a_unk["summary"], True)
    check("三场都声明 advisory（只挂事实，不是门）",
          [a_pass["advisory"], a_fail["advisory"], a_unk["advisory"]],
          [True, True, True])
    check("派生 pass 与 verdict 一致，不许出现两处真相",
          [a_pass["pass"], a_fail["pass"], a_unk["pass"]],
          [True, False, False])
    a_fake = L._run_trace_audit(calls, "〔库·kbdeadbeef p.1〕",
                                library_ids=lib, require_library=False)
    check("★ 全库清单接回后，编造的 doc_id 被判 doc_not_in_library",
          [x["verdict"] for x in a_fake["citations"]], ["doc_not_in_library"])
    a_nolib = L._run_trace_audit(calls, "〔库·kb12345678 p.1〕",
                                 library_ids=None, require_library=False)
    check("负控：没取得全库清单 ⇒ library_ids_known=False（不许自称已核）",
          a_nolib["library_ids_known"], False)
    check("没取得全库清单 ⇒ 该层如实记为未测",
          any("未取得" in s or "未测" in s for s in a_nolib["unknown_reasons"]), True)

    print("\n── 5. 接线：防「工具造好了却没接上」的死代码复发 ──")
    loop_src = Path(L.__file__).read_text(encoding="utf-8")
    js = (ROOT / "rongke" / "web" / "static" / "workspace.js").read_text(
        encoding="utf-8")
    srv = (ROOT / "rongke" / "web" / "server.py").read_text(encoding="utf-8")
    # ⚠️ 判据要落在**真的那一层**：收尾复核在 `Rongke._run_serial` 里，
    #    不在外层 `run()`。第一版扫 `Rongke.run` 得到 False，差点误报「没接线」。
    klass_src = inspect.getsource(Rongke)
    serial_src = inspect.getsource(Rongke._run_serial)
    check("★ 内核类里真的调 audit_delivery()（不是只在别处定义）",
          "audit_delivery(" in klass_src, True)
    check("★ 且落在收尾串行段 _run_serial（作用域也对）",
          "audit_delivery(" in serial_src, True)
    check("★ 复核入口只此一个（不在别处再拼一遍参数）",
          loop_src.count("result.trace_audit = audit_delivery("), 1)
    check("★ 内核真的发 mentor_audit 事件（renderMentorAudit 不再是死代码）",
          'tee("mentor_audit"' in loop_src, True)
    check("前端有对应 case", 'case "mentor_audit"' in js, True)
    render = js[js.find("function renderMentorAudit"):][:2200]
    check("★ 界面只读内核 verdict，不在前端另算判决", "d.verdict" in render, True)
    check("界面把 unknown_reasons 摆出来", "d.unknown_reasons" in render, True)
    check("界面读 advisory 并说明「不是门」", "d.advisory" in render, True)
    # ⚠️ 不能用正则扫模块文本：`interrupted_final` 的 docstring 里就写着
    #    「六处 `final=""`」，文本判据会把它当成一处空答复（判据扫什么就得
    #    在什么上写清楚）。用 AST 只看 `RunResult(...)` 构造里的关键字实参。
    _empty_finals = []
    for _node in ast.walk(ast.parse(loop_src)):
        if not isinstance(_node, ast.Call):
            continue
        _fn = _node.func
        _nm = (_fn.id if isinstance(_fn, ast.Name)
               else _fn.attr if isinstance(_fn, ast.Attribute) else "")
        if _nm != "RunResult":
            continue
        for _kw in _node.keywords:
            if (_kw.arg == "final" and isinstance(_kw.value, ast.Constant)
                    and _kw.value.value == ""):
                _empty_finals.append(_node.lineno)
    check("★ RunResult 构造里不再有 final=\"\"（AST 结构判据，不扫注释）",
          _empty_finals, [])
    check("未完成分支统一走 interrupted_final() 单源",
          loop_src.count("interrupted_final(") >= 6, True)
    frag = L.interrupted_final("达到最大步数仍未通过证据门", 9, td)
    check("中断答复说清原因", "达到最大步数" in frag, True)
    check("中断答复说清断在第几步", "第 9 步" in frag, True)
    check("★ 中断答复带出已落盘正文（不再一片空白）",
          "盘上正文" in frag, True)
    check("★ 人工确认等待上限可配置（无人值守不白等 300s）",
          "RONGKE_ASK_TIMEOUT" in srv, True)
    check("超时下限有兜底（0 / 负数不许把等待关掉）",
          "max(1.0," in srv, True)

    print("\n── 6. 检索回包：自报的 n 恒等于读回来的 len(hits) ──")
    check("★ _kb_finalize 覆盖旧 n（请求 10 / hits 20 的事故不再发生）",
          json.loads(B._kb_finalize({"hits": [{"doc_id": "a"}], "n": 99}))["n"], 1)
    check("负控：空命中不许自报 99",
          json.loads(B._kb_finalize({"hits": [], "n": 99}))["n"], 0)
    check("逐词救援只收 ≥3 字词（保 kb_recall_test 的 `酶解 温度` 负控）",
          B._kb_relax_terms("酶解 温度"), [])
    check("中文多词查询能拆出两个可召回词",
          len(B._kb_relax_terms("菌丝生长速率 菌落直径")), 2)
    check("★ 救援分派只有一个出口（所有改过 hits 的路径都 finalize）",
          Path(B.__file__).read_text(encoding="utf-8").count("return _kb_finalize(o)") >= 3, True)
    check("负控：单字词串不冒充「多词拆词」",
          B._kb_relax_terms("虫草"), [])

    print(f"\n[DONE] {'有失败' if FAILS else '全部通过'} · {N} 项 / {len(FAILS)} 失败")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
