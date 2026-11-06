# -*- coding: utf-8 -*-
"""记忆（rongke/memory.py）单测 —— 纯函数 + 临时台账，不需要 LLM、网络。

覆盖 2026-09-18 修掉的三个实测缺陷，每条都配**正控 + 负控 + 承重自检**：

  1. 相似度         —— 中文 bigram，同串 = 1.0 / 无关串趋近 0
  2. 新近度         —— 半衰期 14 天；**没有时间戳的旧记录给 0.5（不惩罚也不优待）**
  3. 重要性         —— 未完成+有 reason(1.0) > 成功(0.9) > 未完成无 reason(0.6)
  4. ★ 召回排序     —— **同分时必须取新的，不许取旧的**（原实现的稳定排序会永远偏袒最旧）
  5. ★ 召回去重     —— 同目标只出一条（原实现会把同一句话注入 3 遍）
                      且**不同目标必须都保留**（防过度去重）
  6. 注入预算       —— 超预算按阶梯缩；**绝不许截断到半句话**
  7. 蒸馏           —— 样本不足必须**返回空**（负控）；够了才出结论（正控）
  8. 承重自检       —— 把新近度函数换成恒 1、把去重阈值调到不可能命中，
                       对应的正控必须**当场失守**
  9. ★ 累计用量口径 —— **归档与台账是两个总体**：归档完整、台账少报 33.5%。
                       判据是「两把秤问同一个问题，答案必须不同，且必须选对的那把」；
                       配一个"少跳一步 `_`"的天真实现当**判别力正控**
                       （不然"跳 `_`"这条规矩可能根本没在承重）。

一条刻意的记法：**每个断言都成对出现**。只有正控 = 可能报的是别的原因；
只有负控 = 可能这条检查根本没在干活。本项目实测过这两种翻车。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                      # noqa: E402

from rongke import memory as _M                        # noqa: E402
from rongke import pricing as _PR                      # noqa: E402
from rongke.memory import Memory, _similarity, _recency_weight, _importance  # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


NOW = time.time()              # 必须取**真实**当前时间，不能写死一个常量。
#   ⚠️ 第一版这里写的是 `NOW = 1_800_000_000.0`，它落在**未来**（机器时钟当时是
#   1_789_740_xxx）⇒ `_recency_weight` 里的 `max(0.0, age_days)` 把每条记录的
#   age 都钳成 0 ⇒ **所有新近度都是 1.0** ⇒ 新近度机制整体失效、排序退回纯相似度。
#   于是承重自检报"抹掉新近度后结果没变" —— 看起来像代码没承重，
#   实际是测试把被测机制**在装置层就关掉了**。
#   教训：被测函数有 `now=time.time()` 这种默认值时，测试的时间常量必须相对真实时钟。
DAY = 86400.0


def mk_ledger(rows, root):
    """造一份临时台账。`rows` 是 dict 列表，按给定顺序（= 时间正序）写盘。"""
    p = Path(root) / "runs.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return Memory(Path(root))


def run(goal, success=True, steps=3, ts=NOW, reason="", tools=None, failed=None, rid=""):
    return {"goal": goal, "success": success, "steps": steps, "elapsed": 10.0,
            "tools_used": tools or [], "tools_failed": failed or [],
            "final": "", "reason": reason, "workspace": "", "run_id": rid, "ts": ts,
            "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
            "cost_usd": "UNKNOWN"}


print("── 1. 相似度 ──")
s = "写一段约120字的论文引言草稿"
check("同一串 → 1.0（正控）", _similarity(s, s), 1.0)
check("无关串 → 接近 0（负控）", _similarity(s, "今天天气不错去吃火锅") < 0.1, True)
check("空串 → 0（不除零）", _similarity("", s), 0.0)
check("两边都空 → 0（不除零）", _similarity("", ""), 0.0)
check("近乎相同（只多一句）→ 高相似但 < 1.0",
      # 不写死具体数字：Jaccard 对长度差**本就有惩罚**，
      # `s` 的 12 个 bigram 全在并集的 20 个里 ⇒ 实测 0.6。
      # 断言的是性质（高，且没到 1.0），不是那个我猜出来的数 ——
      # 猜出来的阈值是测试里最常见的假证据。
      0.55 <= _similarity(s, s + "。不需要检索文献") < 1.0, True)

print("\n── 2. 新近度：半衰期 14 天 ──")
check("刚刚发生 → 1.0（正控）", round(_recency_weight(NOW, NOW), 4), 1.0)
check("一个半衰期前 → 0.5", round(_recency_weight(NOW - 14 * DAY, NOW), 4), 0.5)
check("两个半衰期前 → 0.25", round(_recency_weight(NOW - 28 * DAY, NOW), 3), 0.25)
check("★ 没有时间戳的旧记录 → 0.5（不惩罚也不优待，负控式设计）",
      _recency_weight(0.0, NOW), 0.5)
check("未来时间戳不产生 >1 的权重（防脏数据放大）",
      _recency_weight(NOW + 100 * DAY, NOW) <= 1.0, True)
check("越新分越高（单调性）",
      _recency_weight(NOW, NOW) > _recency_weight(NOW - 7 * DAY, NOW)
      > _recency_weight(NOW - 30 * DAY, NOW), True)

print("\n── 3. 重要性 ──")
check("未完成 + 有 reason → 1.0（它写明了哪里没走通）",
      _importance({"success": False, "reason": "撞了步数上限"}), 1.0)
check("成功 → 0.9", _importance({"success": True}), 0.9)
check("未完成 + 没 reason → 0.6（信息量最低）",
      _importance({"success": False, "reason": ""}), 0.6)
check("reason 只有空白也算没有",
      _importance({"success": False, "reason": "   "}), 0.6)

print("\n── 4. ★ 召回排序：同分时必须取新的（原实现永远取最旧的）──")
# ★ 夹具放**仓库内**（与 `_trash` 同盘）。原版用 `tempfile.mkdtemp()` ——
#   那在系统盘，而 `stash` 是 `os.replace`、跨卷会 EXDEV，垃圾就得留在
#   系统临时目录里（更脏，且没人清）。同盘才不会绕路。
TMP = str(T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_memtest", "memtest"))
# ★ 第 10 节的夹具在这里就建好，**不进 try** —— 因为 `finally` 要无条件给它们兜底；
#   放在 try 里面定义的话，异常先于定义发生时 `finally` 会 NameError，
#   夹具就静默漏在盘上了。（`for` 里逐个数出来的事故，不是想象出来的。）
ARCH = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_usage", "usage")
ARCH2 = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_usage2", "usage2")
LED2 = T.fresh_dir(ROOT / "runs" / "_regress" / "_probe_usage_led", "usage-led")
try:
    G = "写一段约120字的论文引言草稿，主题是冬虫夏草人工培育的主要难点。不需要检索文献。"
    # 三条**目标完全相同** ⇒ 相似度都是 1.0 ⇒ 只能靠新近度决胜。
    # 按时间正序写盘（旧 → 新），这正是原实现会翻车的输入顺序。
    m = mk_ledger([
        run(G, steps=5, ts=NOW - 30 * DAY, rid="old"),
        run(G, steps=6, ts=NOW - 10 * DAY, rid="mid"),
        run(G, steps=4, ts=NOW - 1 * DAY, rid="new"),
    ], TMP)
    got = m.recall(G, k=1)
    check("三条同目标（相似度全为 1.0）→ 取到**最新**那条（正控）",
          [r.run_id for r in got], ["new"])
    check("★ 若取到最旧的，就是原缺陷复发（反面对照）",
          [r.run_id for r in got] != ["old"], True)

    print("\n── 5. ★ 召回去重 ──")
    m2 = mk_ledger([
        run(G, ts=NOW - 3 * DAY, rid="a"),
        run(G, ts=NOW - 2 * DAY, rid="b"),
        run(G, ts=NOW - 1 * DAY, rid="c"),
        run("用 ledger_query 查看台账，报告最近 5 次调用分别是什么工具",
            steps=2, ts=NOW - 1 * DAY, rid="other"),
    ], TMP)
    same = m2.recall(G, k=3)
    check("三条同目标 → **只出一条**（正控：去重生效）", len(same), 1)
    check("留下的那条是最新的", [r.run_id for r in same], ["c"])
    allg = {r.goal for r in same}
    check("去重后目标不重复（防注入里同一句话多遍）", len(allg), len(same))

    # 负控：**不同**目标不许被误去重
    m3 = mk_ledger([
        run("写一段约120字的论文引言草稿，主题是冬虫夏草人工培育的主要难点。",
            ts=NOW - 1 * DAY, rid="x"),
        run("写一段约120字的论文引言草稿，主题是深山灵芝人工培育的主要难点。",
            ts=NOW - 1 * DAY, rid="y"),
    ], TMP)
    two = m3.recall("写一段约120字的论文引言草稿，主题是人工培育的主要难点。", k=3)
    check("★ 只换了主题词的两个目标 → **不许合并**（负控：防过度去重）",
          len(two), 2)

    print("\n── 6. 注入预算与退化阶梯 ──")
    t_small = m2.inject_text(G, k=3, max_chars=1400)
    check("正常预算下出得来东西（正控）", bool(t_small), True)
    check("★ 不超预算", len(t_small) <= 1400, True)
    # 预算压到极小：装不下就**什么都不注入**，绝不截半句
    t_tiny = m2.inject_text(G, k=3, max_chars=30)
    check("★ 预算小到装不下 → 返回空串，**不许截断到半句话**（负控）", t_tiny, "")
    t_mid = m2.inject_text(G, k=3, max_chars=250)
    check("中等预算 → 要么空要么整行完整（不出现半截行）",
          t_mid == "" or t_mid.rstrip().endswith(("证据。", "。", "）")), True)

    print("\n── 7. 蒸馏：样本不足必须闭嘴，够了才开口 ──")
    few = mk_ledger([run("任务" + str(i), failed=["web_extract"]) for i in range(3)], TMP)
    check("只有 3 条记录 → **一条经验都不出**（负控：假先验比没先验更危险）",
          few.distill_stats(), [])
    empty = Memory(Path(TMP) / "nonexistent")
    check("台账文件不存在 → 空（不抛异常）", empty.distill_stats(), [])

    many = mk_ledger(
        [run("任务" + str(i), steps=5, ts=NOW - i * DAY,
             tools=["web_extract", "read_skill"], failed=["web_extract"])
         for i in range(6)],
        TMP)
    ds = many.distill_stats()
    check("样本够了 → 出结论（正控）", bool(ds), True)
    check("★ 报出常失败的 `web_extract`（正控：这条以前永远触发不了）",
          any("web_extract" in x for x in ds), True)
    check("不报没失败过的 `read_skill`（负控）",
          any("read_skill" in x for x in ds), False)

    # 样本够但工具调用次数不够 ⇒ 不许谈失败率
    shallow = mk_ledger(
        [run("任务" + str(i), steps=5, ts=NOW - i * DAY,
             tools=["rare_tool"], failed=["rare_tool"]) for i in range(6)],
        TMP)
    check("工具只被调用 6 次但阈值是 3… 恰好达标（边界）",
          any("rare_tool" in x for x in shallow.distill_stats()), True)

    print("\n── 8. 台账往返：新字段必须存下去、读回来 ──")

    class _Call:
        def __init__(self, name, ok):
            self.name, self.ok = name, ok

    class _Usage:
        input_tokens, output_tokens, cache_read = 100, 20, 0

    class _Result:
        success, steps, final, reason = True, 3, "交付了", ""
        workspace, run_id = "", "rid1"
        usage = _Usage()
        cost = "UNKNOWN"
        trace = [_Call("read_skill", True), _Call("web_extract", False)]

    m4 = Memory(Path(TMP) / "roundtrip")
    rec = m4.save_run("目标", _Result(), elapsed=1.0)
    check("save_run 记下失败的工具名（正控）", rec.tools_failed, ["web_extract"])
    check("tools_used 仍含全部调用（向后兼容）", rec.tools_used, ["read_skill", "web_extract"])
    back = m4.recent_runs(5)[0]
    check("落盘后能读回 tools_failed", back.get("tools_failed"), ["web_extract"])
    check("旧记录没有该字段时不炸（默认空表）",
          Memory(Path(TMP)).recall("完全不相干的目标xyz", k=1) == []
          or all(isinstance(r.tools_failed, list) for r in Memory(Path(TMP)).recall(G, k=1)),
          True)

    print("\n── 9. 承重自检（逐个隔离，不要一次抹掉两个机制）──")
    # 9.1 新近度：必须**精心构造**才隔离得出来。
    #   ⚠️ 第一版自检在这里翻车，值得留档：我用"两条目标完全相同"当探针，
    #   stub 掉 `_recency_weight` 后**照样取到新记录** —— 因为 sort 里还有一条
    #   `-ts` 兜底（同分时新的优先）。于是那条正控**通过的原因不是我声称的那个**。
    #   G 型缺陷（检查不承重）在我自己的自检上重演了一次。
    #   正确的隔离：让**旧的那条相似度更高**（1.0）而新的略低（<1），分数就不打平，
    #   `-ts` 兜底纯属观战 —— 此时"取新的"只可能来自新近度加成。
    G_OLD = G
    G_NEW = G.replace("主要难点", "核心瓶颈")
    check("隔离前提成立：旧那条相似度更高（否则下面的自检无效）",
          _similarity(G, G_OLD) > _similarity(G, G_NEW), True)
    mp = mk_ledger([run(G_OLD, ts=NOW - 30 * DAY, rid="old"),
                    run(G_NEW, ts=NOW - 1 * DAY, rid="new")], TMP)
    live_new = [r.run_id for r in mp.recall(G, k=1)]
    _old_rec = _M._recency_weight
    _M._recency_weight = lambda ts, now, half_life_days=14.0: 1.0   # 抹掉新近度
    try:
        dead_new = [r.run_id for r in mp.recall(G, k=1)]
    finally:
        _M._recency_weight = _old_rec
    check("★ 抹掉新近度 ⇒ 正控当场失守（相似度高的旧记录反超）",
          (live_new, dead_new), (["new"], ["old"]))
    check("_recency_weight 已还原", _M._recency_weight is _old_rec, True)

    # 9.2 `-ts` 兜底是**独立的第二道防线**：把新近度抹平，同分时也不偏袒最旧。
    mp2 = mk_ledger([run(G, steps=5, ts=NOW - 30 * DAY, rid="old"),
                     run(G, steps=4, ts=NOW - 1 * DAY, rid="new")], TMP)
    _M._recency_weight = lambda ts, now, half_life_days=14.0: 1.0
    try:
        tie = [r.run_id for r in mp2.recall(G, k=1)]
    finally:
        _M._recency_weight = _old_rec
    check("★ 即便新近度被抹平，同分也不会退回『永远取最旧』（ts 兜底独立承重）",
          tie, ["new"])

    md = mk_ledger([run(G, ts=NOW - 3 * DAY, rid="a"),
                    run(G, ts=NOW - 2 * DAY, rid="b"),
                    run(G, ts=NOW - 1 * DAY, rid="c")], TMP)
    live_dedup = len(md.recall(G, k=3))
    _old_sim = _M._DEDUP_GOAL_SIM
    _M._DEDUP_GOAL_SIM = 1.01          # 阈值调到不可能命中 ⇒ 去重失效
    try:
        dead_dedup = len(md.recall(G, k=3))
    finally:
        _M._DEDUP_GOAL_SIM = _old_sim
    check("★ 关掉去重 ⇒ 同目标又出 3 条（证明去重真的在承重）",
          (live_dedup, dead_dedup), (1, 3))
    check("_DEDUP_GOAL_SIM 已还原", _M._DEDUP_GOAL_SIM is _old_sim, True)

    # 蒸馏样本阈值承重
    small5 = mk_ledger([run("任务" + str(i), failed=["t"]) for i in range(4)], TMP)
    live_d = small5.distill_stats()
    _old_min = Memory._MIN_SAMPLES
    Memory._MIN_SAMPLES = 1
    try:
        dead_d = small5.distill_stats()
    finally:
        Memory._MIN_SAMPLES = _old_min
    check("★ 把样本门槛降到 1 ⇒ 4 条记录也开始出经验（证明门槛在承重）",
          (live_d, bool(dead_d)), ([], True))
    check("_MIN_SAMPLES 已还原", Memory._MIN_SAMPLES, _old_min)

    # ── 10. ★ 累计用量口径：归档 vs 台账，**两个总体** ────────────────────
    # 测的是 2026-09-19 修掉的缺陷：`--pricing` 的「历史累计用量」原先读 **台账**
    # （`memory/runs.jsonl`），而台账的用量字段是后加的、只覆盖 21 场里的 11 场
    # ⇒ 实测少报 33.5%（708,820 vs 归档的 1,065,746），且漏掉的恰是最贵的两场。
    #
    # 这一节的判据是「最终行为」而不是"字段存不存在"：
    #   **拿两把秤问同一个问题，答案必须不同，而且必须选对的那把。**
    # 所以下面既有真数据（必须选归档），也有一个**故意写错的天真实现**当判别力正控
    # （它就是在 glob 上少跳了一步的那种写法）—— 两个答案分得开，这条规矩才算在承重。
    print("\n── 10. ★ 累计用量口径（归档 vs 台账）──")

    def mk_archive(name, i, o, cache=0, cost=None, usage=True, root=None):
        """造一场归档。`usage=False` ⇒ `usage` 容器在、里面空 —— 模拟
        「那次运行早于用量记录机制」。**它与「写了 0」是两件事**，别混。"""
        d = Path(root or ARCH) / name
        d.mkdir(parents=True, exist_ok=True)
        u = ({"input_tokens": i, "output_tokens": o, "cache_read": cache} if usage else {})
        (d / "session.json").write_text(json.dumps(
            {"id": name, "goal": "任务" + name, "elapsed": 1.0,
             "result": {"run_id": name, "usage": u,
                        "cost_usd": cost if cost is not None else "UNKNOWN",
                        "cost_display": "UNKNOWN"}}, ensure_ascii=False), encoding="utf-8")
        return d

    def naive_usage(root):
        """**故意写错**：顶层目录一律数、不跳 `_`。只当**判别力正控**用 ——
        天真实现就长这样（`glob('*/session.json')` 一把梭）。"""
        tot = 0
        for dd in sorted(Path(root).iterdir()):
            p = dd / "session.json"
            if not dd.is_dir() or not p.exists():
                continue
            j = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            tot += int(((j.get("result") or {}).get("usage") or {}).get("input_tokens") or 0)
        return tot

    mk_archive("20260101-000001", 1000, 100, cache=400)
    mk_archive("20260101-000002", 2000, 200)
    mk_archive("20260101-000003", 3000, 300, usage=False)   # 有容器、没内容
    (Path(ARCH) / "20260101-000004").mkdir()                # 连 session.json 都没有
    mk_archive("_ui_fixture", 99999, 9999)                  # 建材：真运行的副本

    au = _M.archived_usage(ARCH)
    check("★ 只数**有用量**的场（3 场里 2 场用量齐）", au["runs"], 2)
    check("★ 总量 = 手数的 1000+2000，**不含**夹具那 99999", au["input_tokens"], 3000)
    check("★ 输出汇总（100+200）", au["output_tokens"], 300)
    check("★ 缓存命中读汇总（400）", au["cache_read"], 400)
    check("★ 缓存比 = 400/3000", au["cache_ratio"], round(400 / 3000, 4))
    check("★ `usage` 在但为空 → 进 `without_usage`（**不是**当成 0 混进总数）",
          au["without_usage"], ["20260101-000003"])
    check("★ 没有 session.json → 进 `no_archive`（「不可知」≠「0」）",
          au["no_archive"], ["20260101-000004"])
    check("★ `_` 前缀的建材进 `skipped`、不进总数", au["skipped"], ["_ui_fixture"])
    check("★ 判别力正控：天真实现（不跳 `_`）**会**多算夹具那一场 ⇒ 两个答案分得开",
          naive_usage(ARCH) - au["input_tokens"], 99999)
    check("★ 于是「跳 `_`」这条规矩真的在承重：把它删掉，上面那条 3000 就会失守",
          naive_usage(ARCH) != au["input_tokens"], True)

    # 成本：凡是有一场算不出来，总额就必须是 UNKNOWN —— 不许把"未知"当 0 相加。
    # （这是 F 型缺陷的标准形状：把"没有数据"渲染成"有数据"。）
    au2 = _M.archived_usage(ARCH)
    check("★ 成本一场都算不出来 ⇒ UNKNOWN（**不是 0**）", au2["cost_usd"], _PR.UNKNOWN)
    mk_archive("20260101-000005", 500, 50, cost=0.5)        # 这一场有真成本
    au2 = _M.archived_usage(ARCH)
    check("★ 只要还有一场算不出来，总额仍是 UNKNOWN（负控：不许拿已知去凑）",
          au2["cost_usd"], _PR.UNKNOWN)
    check("★ 且如实报出「几场算不出来」（2 场 UNKNOWN）", au2["uncosted"], 2)

    # 反向正控：每一场都有单价时才给真数字。缺了这一条，「恒 UNKNOWN」也能蒙混过关。
    mk_archive("a", 100, 10, cost=0.25, root=ARCH2)
    mk_archive("b", 200, 20, cost=0.75, root=ARCH2)
    au3 = _M.archived_usage(ARCH2)
    check("正控：每场都能算 ⇒ 总额是真数字 0.25+0.75", au3["cost_usd"], 1.0)
    check("★ 且此时 uncosted = 0（证明上一条不是恒 UNKNOWN 蒙对的）", au3["uncosted"], 0)
    check("★ 空目录：0 场 ⇒ 成本仍是 UNKNOWN（「0 场」不是「免费」）",
          _M.archived_usage(Path(ARCH) / "不存在的目录")["cost_usd"], _PR.UNKNOWN)

    # `since` 过滤（与 `eval/baseline.py::collect` 同一套口径：目录名前 8 位 = YYYYMMDD）
    mk_archive("20260102-000001", 700, 70)
    check("★ `since` 按目录名前 8 位过滤（20260102 之后只剩 1 场）",
          _M.archived_usage(ARCH, since="20260102")["runs"], 1)
    check("★ 不过滤则是 4 场（证明上一条不是「本来就 1 场」）",
          _M.archived_usage(ARCH)["runs"], 4)

    # `stats()` 的形状：两把秤**分开放**，谁也没覆盖谁。
    cols = [dict(run("台账里的任务一", rid="x"), input_tokens=111, output_tokens=11),
            dict(run("台账里的任务二", rid="y"), input_tokens=222, output_tokens=22)]
    ms = mk_ledger(cols, LED2)
    st_none = ms.stats()
    check("★ 不传 `runs_dir` ⇒ archived 是 None（**「没查」不是「查出来是 0」**）",
          st_none["archived"], None)
    check("★ 台账口径收进 `ledger` 子字典，顶层不再挂 input_tokens（形状即防呆）",
          ("input_tokens" in st_none, "input_tokens" in st_none["ledger"]), (False, True))
    st_both = ms.stats(runs_dir=ARCH2)
    check("★ 两把秤**各报各的**：台账 333 / 归档 300，谁也没覆盖谁",
          (st_both["ledger"]["input_tokens"], st_both["archived"]["input_tokens"]),
          (333, 300))

    # 真数据上的地雷：`_ui_resume` 是真运行 `20260918-203437` 的逐字节副本
    # （连 run_id 都一样）⇒ 数进来就是同一场计两遍。
    # ⚠️ 我第一版这里写的是 `_real["input_tokens"] != _real["input_tokens"] + 166979`
    #    —— **恒真断言**，正是本项目的 G 型缺陷（写了等于没写）。换成"差**恰好**等于
    #    所有跳过建材的用量之和"：它既非恒真，也不随数据变化而误报。
    _real = _M.archived_usage(ROOT / "runs")
    if (ROOT / "runs" / "_ui_resume").exists():
        check("★ 真数据：`_ui_resume`（真运行的副本）落在 skipped 里，没进总数",
              "_ui_resume" in _real["skipped"], True)
        _sk = 0
        for _n in _real["skipped"]:
            _p = ROOT / "runs" / _n / "session.json"
            if not _p.exists():
                continue
            try:
                _j = json.loads(_p.read_text(encoding="utf-8", errors="replace"))
                _sk += int(((_j.get("result") or {}).get("usage") or {})
                           .get("input_tokens") or 0)
            except Exception:
                pass
        check("★ 天真实现多出来的，**恰好**是 skipped 里那些带用量的建材（不是恒真）",
              naive_usage(ROOT / "runs") - _real["input_tokens"], _sk)
        check("★ 且这个差 > 0 ⇒ 「跳 `_`」不是写了等于没写", _sk > 0, True)
    # 文件签名缓存：不是永久快照。改用量与损坏归档都必须反映到下一次读取。
    uc = _M.ArchivedUsageCache()
    au_cached = _M.archived_usage(ARCH, cache=uc)
    check("★ 用量缓存与无缓存结果一致", au_cached, _M.archived_usage(ARCH))
    _p = ARCH / "20260101-000001" / "session.json"
    _before = _p.read_bytes()
    try:
        _p.write_text(json.dumps({"result": {"usage": {"input_tokens": 77, "output_tokens": 8}}}),
                      encoding="utf-8")
        check("★ 归档变化后缓存立即失效", _M.archived_usage(ARCH, cache=uc)["input_tokens"],
              au_cached["input_tokens"] - 1000 + 77)
        _p.write_text("{bad", encoding="utf-8")
        _broken = _M.archived_usage(ARCH, cache=uc)
        check("★ 缓存不掩盖损坏归档", "20260101-000001" in _broken["no_archive"], True)
    finally:
        _p.write_bytes(_before)
    # 尾部读取：大 prefix、坏尾行、空行均保持原语义；不许把 prefix 也算进去。
    tail = _M.Memory(Path(TMP) / "tail")
    tail.root.mkdir(parents=True, exist_ok=True)
    with tail.runs_path.open("w", encoding="utf-8") as _f:
        for _i in range(500):
            _f.write(json.dumps({"goal": str(_i), "text": "中" * 150}) + "\n")
        _f.write("{broken\n\n")
    check("★ 台账尾读不整表解析", [r["goal"] for r in tail.recent_runs(2)], ["498", "499"])
    check("★ 尾读 0 返回空", tail.recent_runs(0), [])
    check("★ 非阻断坏行不改变历史顺序", [r["goal"] for r in tail.recent_runs(500)], [str(i) for i in range(500)])

finally:
    for _d, _tag in ((TMP, "memtest"), (ARCH, "usage"),
                     (ARCH2, "usage2"), (LED2, "usage-led")):
        T.stash(_d, _tag)

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
