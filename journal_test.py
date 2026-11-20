# -*- coding: utf-8 -*-
"""《岸记》记忆插件判据：体例 / append-only / 读回 / 规划同源 / 挂载纪律。

判据口径：逐条 `[PASS]`，结尾 `[DONE]`（`regress.py:_RE_MARK` 只认这些标记）。
刻意量的四件事，都是这个项目实踩过的坑：
  · 「没测到」不许伪装成通过；· 「写成功」不许冒充「读回一致」；
  · 配置在场 ≠ 已生效（真调 fn 落盘再看）；· 同名工具不覆盖、也不登记撤销。
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from rongke.journal import KINDS, RULE, WATERS, JournalBook, JournalError

_fail = 0


def ok(m):
    print(f"[PASS] {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"[FAIL] {m}")


def eq(got, want, label):
    if got == want:
        ok(label)
    else:
        bad(f"{label}：得到 {got!r}，期望 {want!r}")


def truthy(v, label):
    if v:
        ok(label)
    else:
        bad(label)


def raises(fn, label, exc=JournalError):
    try:
        fn()
    except exc:
        ok(label)
    except Exception as e:
        bad(f"{label}：抛了别的 {type(e).__name__}: {e}")
    else:
        bad(f"{label}：居然没抛（静默成功是重罪）")


ROOT = Path(__file__).resolve().parent

with tempfile.TemporaryDirectory(prefix="rk_journal_") as td:
    tmp = Path(td) / "journal"
    book = JournalBook(tmp)

    print("== ① 空册与体例校验（拦沉默，不拦选择）==")
    eq(book.days(), [], "空册没有渡")
    eq(book.rows(), [], "空台账读到 0 事件（不是异常）")
    raises(lambda: book.note("记", "没开页就写"), "未开页就续写 ⇒ 拒绝")
    raises(lambda: book.open_day("2026-11-02", "浪", "问"), "非法水位 ⇒ 拒绝")
    raises(lambda: book.open_day("2026-13-45", "潮", "问"), "不是真实日期 ⇒ 拒绝")
    raises(lambda: book.open_day("2026-11-02", "潮", ""), "今日问为空 ⇒ 拒绝")
    eq(len(book.rows()), 0, "四条拒绝一个事件都没落（不污染台账）")
    truthy("潮" in WATERS and "礁" in WATERS and len(WATERS) == 4, "水位四态齐全")
    eq(KINDS, ("记", "验", "朱批", "收", "明日", "锚"), "六段顺序单源")

    print("== ② 开页 / 续写 / append-only ==")
    ev = book.open_day("2026-11-02", "潮", "对话里能不能管理课题")
    eq(ev["water"], "潮", "开页水位落账")
    eq(book.rows()[0], ev, "台账首行读回 == 刚写入的事件")
    raises(lambda: book.open_day("2026-11-02", "礁", "再问一次"),
           "同日重复开页 ⇒ 拒绝覆盖")
    eq(len(book.rows()), 1, "重复开页没多写一行")
    book.note("记", "用户说让他创建课题、关联文献，他做不到；先去读真账，不猜。")
    book.note("验", "topic_link 到底缺在哪",
              evidence="registry 63 工具 / 4 个真实课题 library 全 0",
              verdict="能力早就存在，只是没给模型")
    book.note("朱批", "252 篇 status 全 unread，状态机从未被走过 —— 我先前以为它在跑。")
    book.note("收", "补上 topic_link 与 has_topic 挂载判据，63→65 工具。",
              boundary="EVIDENCE.md 仍无形状，note 字段仍被机器占满")
    book.note("明日", "在写入端给 EVIDENCE.md 定形状（断言→锚点→状态）",
              evidence="topic_write(doc=evidence) 输出能反解出列，且四个课题形状一致")
    book.note("锚", ".workbuddy/memory/2026-11-02.md")
    eq(len(book.rows()), 7, "1 开页 + 6 段笔记，事件数对得上")

    print("== ③ 渲染体例（判词是压缩，不是省略）==")
    md = book.render_day("2026-11-02")
    truthy("岸记 · 第 1 渡" in md, "标题带渡次")
    truthy("水位 **潮**（推进 · 全绿）" in md, "题头带水位与释义")
    truthy("今日问：对话里能不能管理课题" in md, "题头带今日问")
    for seg in ("## 起", "## 验", "## 朱批", "## 收", "## 明日", "## 锚"):
        truthy(seg in md, f"段落 {seg} 出现在渲染里")
    truthy("**读回**　registry 63 工具" in md, "验：读回数字没被文体吃掉")
    truthy("〔能力早就存在，只是没给模型〕" in md, "验：判词单独成行")
    truthy("**不宣称**" in md, "收：不宣称是硬段")
    truthy("`.workbuddy/memory/2026-11-02.md`" in md, "锚：证据路径带反引号")
    truthy(RULE in md, "页脚岸律在")
    truthy(md.index("## 朱批") < md.index("## 收"), "六段顺序固定（朱批在收之前）")

    print("== ④ 证据只认读回 ==")
    r = book.read_day("2026-11-02")
    eq(r["same"], True, "渲染文本 == 落盘全文")
    eq(r["markdown"], r["disk"], "读回内容与磁盘逐字一致")
    eq(r["crossing"], 1, "第一渡")
    eq(len(r["notes"]), 6, "读回 6 段笔记")
    truthy((tmp / "2026-11-02.md").exists(), "日记文件真落盘")
    truthy((tmp / "规划.md").exists(), "规划看板随写随渲染")
    truthy((tmp / "index.html").exists(), "HTML 日记本随写随渲染")

    print("== ⑤ 规划与日记同源（不另立第二份真相）==")
    pr = book.plan_rows()
    eq(len(pr), 1, "一条「明日」= 一条规划")
    eq(pr[0]["status"], "待渡", "初始状态待渡")
    eq(pr[0]["accept"],
       "topic_write(doc=evidence) 输出能反解出列，且四个课题形状一致",
       "验收判据来自 evidence，不是另写一遍")
    plan_md = (tmp / "规划.md").read_text(encoding="utf-8")
    truthy("在写入端给 EVIDENCE.md 定形状" in plan_md, "规划看板含该事项")
    truthy("勿手改" in plan_md, "规划页明写自己是视图")
    raises(lambda: book.done("根本不存在的条目"),
           "找不到的条目 ⇒ 拒绝结案（读不到 ≠ 已办）")
    d = book.done("EVIDENCE.md 定形状")
    eq(d["dates"], ["2026-11-02"], "结案定位到那一渡")
    eq(book.plan_rows()[0]["status"], "已渡", "结案后状态翻转")
    truthy("在写入端给 EVIDENCE.md 定形状" in book.render_day("2026-11-02"),
           "结案不删原「明日」，日记原文仍在")
    truthy("- [x]" in book.render_day("2026-11-02"), "日记里勾掉但保留")

    print("== ⑥ 第二渡与 HTML 日记本 ==")
    book.open_day("2026-11-03", "礁", "答复形态是不是比工具更卡")
    eq(book.crossing("2026-11-03"), 2, "第二渡编号按日期序")
    book.note("验", "把答案从文件搬回对话", evidence="/api/run 24 秒 / 776 字 / 无反问",
              verdict="卡点在答复形态，不在缺工具",
              boundary="导师开关那条路径没复验")
    book.note("明日", "给 lab 加 retract（追加式作废）", evidence="lab_test 正负控各一条")
    book.note("锚", ".workbuddy/memory/2026-11-03.md")
    book.note("朱批", "<script>alert(1)</script> 不该被当成正文执行")
    h = (tmp / "index.html").read_text(encoding="utf-8")
    truthy("2026-11-02" in h and "2026-11-03" in h, "日记本含两渡")
    truthy("&lt;script&gt;" in h and "<script>alert" not in h, "朱批里的尖括号被转义（不注入）")
    truthy("岸记" in h and "w-礁" in h, "默认手记版：标题与水位戳都在")
    truthy("#fdfaf3" in h and "border-radius:16px" in h,
           "默认手记版：暖白圆角皮肤真生效（不是只挂了个名字）")
    truthy("--paper:#f8f4ea" not in h, "默认皮肤里没有宋版的陈墨底")
    truthy(RULE in h, "HTML 页脚也有岸律")
    truthy("http://" not in h and "https://" not in h and "cdn" not in h,
           "HTML 日记本零外部依赖")
    truthy("暂无待渡" not in h, "待渡条目真的进了侧栏")
    rr = book.render_all()
    eq(rr["days"], 2, "重渲染覆盖两渡")
    before = (tmp / "index.html").read_bytes()
    book.render_all()
    eq((tmp / "index.html").read_bytes(), before, "重复重渲染逐字节幂等")
    raises(lambda: book.read_day("2026-11-04"), "没开过的那一渡 ⇒ read 明说没有")

    print("== ⑦ 能力挂载与配置纪律（配置在场 ≠ 已生效）==")
    from rongke import caps
    from rongke.tools import Tool, ToolRegistry

    eq(caps.ensure_external_loaded(), [], "外部能力文件导入零错误")
    v = caps.resolve("@external/shore-journal-voice")
    t = caps.resolve("@external/shore-journal")
    truthy(v is not None and t is not None, "两个能力都注册进 _REGISTRY")
    truthy("external-shore-journal" in v.SECTION_TEXTS, "提示段自带且段名对得上预设")
    truthy("岸律" in v.SECTION_TEXTS["external-shore-journal"], "提示段里真写了岸律")
    truthy("禁用句式" in v.SECTION_TEXTS["external-shore-journal"],
           "提示段带可判定的禁用句式清单")
    eq(t.config_schema["dir"]["required"], True, "dir 必填")
    eq(t.config_schema["expose"]["required"], True, "expose 必填")
    eq("default" in t.config_schema["dir"], False, "必填项没有 default")
    eq("default" in t.config_schema["expose"], False, "必填项没有 default（第二项）")
    eq(caps.schema_discipline(), [], "全仓 schema 纪律未被打破")
    raises(lambda: v().mount({"enabled": False}, {}), "enabled=false 不该被点名", ValueError)

    reg0 = ToolRegistry()
    n0 = len(reg0)
    # ★ 2026-11-09：常驻插件每场都加载，baseline 多出的工具按
    #   caps.always_on_tool_names() 单源核，不在这里手写数字。
    from rongke.caps import always_on_tool_names
    ALWAYS = always_on_tool_names()
    b = caps.load_capability_preset("baseline", ctx={"registry": reg0})
    eq(len(reg0), n0 + len(ALWAYS), "baseline 只多出常驻工具（有些插件必须加载）")
    eq(b.registered_tools, [], "baseline 撤销清单为空")
    eq(len(b.broken), 0, "baseline 健康")

    reg1 = ToolRegistry()
    p = caps.load_capability_preset("journal", ctx={"registry": reg1})
    eq(len(p.broken), 0, "journal 预设挂载零 broken（唯一健康判据）")
    truthy(p.healthy, "journal 预设 healthy")
    eq(sorted(p.registered_tools), sorted(t.TOOL_NAMES),
       "5 个工具全登记进本场撤销清单")
    truthy(reg1.get("journal_note") is not None, "工具真注册（不是只写在配置里）")
    eq(reg1.get("journal_note").mutating, True, "续写工具标 mutating")
    eq(reg1.get("journal_read").mutating, False, "读回工具不标 mutating")
    truthy("external-shore-journal" in caps.available_sections(),
           "预设 patch 的段名有人提供（否则该 broken）")

    reg2 = ToolRegistry()
    reg2.register(Tool(name="journal_note", description="别人的同名工具",
                       parameters={"type": "object", "properties": {}, "required": []},
                       fn=lambda **kw: "other"))
    ctx2 = {"registry": reg2}
    got = t().mount({"dir": str(tmp / "ext"), "expose": True}, ctx2)
    eq(reg2.get("journal_note").description, "别人的同名工具", "同名工具不覆盖")
    eq("journal_note" in ctx2.get("registered_tools", []), False,
       "同名工具不登记撤销（不会误撤别人的）")
    eq(len(ctx2.get("registered_tools", [])), 4, "其余 4 个照常登记")
    truthy("4/5" in got[0], "挂载回包如实说明少装了 1 个")

    ctx3 = {"registry": ToolRegistry()}
    eq(t().mount({"dir": str(tmp / "off"), "expose": False}, ctx3),
       ["未暴露（岸记只留内核 API）"], "expose=false 不注册工具")
    eq(ctx3.get("registered_tools", []), [], "expose=false 撤销清单为空")
    truthy("只读挂载检查" in t().mount({"dir": str(tmp), "expose": True}, {})[0],
           "registry 缺席时不炸（只读挂载检查）")

    print("== ⑧ 真调工具 fn：写盘后读回，不采信自述 ==")
    reg4 = ToolRegistry()
    ctx4 = {"registry": reg4}
    t().mount({"dir": str(tmp / "tools"), "expose": True}, ctx4)
    b4 = JournalBook(tmp / "tools")
    reg4.get("journal_open").fn(date="2026-11-07", water="礁",
                                question="引注能不能在原文里找到支撑句")
    reg4.get("journal_note").fn(kind="验", text="真产物跑一遍",
                                evidence="17/17 映射到读过的文献",
                                verdict="真实形状把覆盖率当场打露")
    reg4.get("journal_note").fn(kind="明日", text="write_file 写完自动附对齐简报",
                                evidence="落盘全文可见对齐台账")
    out = reg4.get("journal_read").fn(date="2026-11-07")
    truthy("落盘一致=True" in out, "读回工具自己报告落盘一致")
    truthy("17/17" in out, "读回工具带出原始数字")
    eq(len(b4.rows()), 3, "工具真的往台账写了 3 条事件")
    eq(b4.read_day("2026-11-07")["same"], True, "工具产出的日记读回一致")
    raises(lambda: reg4.get("journal_open").fn(date="2026-11-07", water="潮", question="覆盖"),
           "工具层重复开页也拒绝（异常会置 ToolCall.ok=False）")
    eq(len(b4.rows()), 3, "被拒的调用没写进台账")
    reg4.get("journal_done").fn(item="write_file 写完自动附对齐简报")
    eq(b4.plan_rows()[0]["status"], "已渡", "工具层结案生效")
    truthy("重渲染" in reg4.get("journal_render").fn(), "重渲染工具可用")

    print("== ⑨ 前端与常驻判据（别让用户看见裸英文）==")
    js = (ROOT / "rongke/web/static/workspace.js").read_text(encoding="utf-8")
    m = re.search(r"const PRESET_TOOLHUMAN = \{(.*?)\};", js, re.S)
    names = set(re.findall(r"(\w+)\s*:", m.group(1))) if m else set()
    for n in t.TOOL_NAMES:
        truthy(n in names, f"{n} 在 PRESET_TOOLHUMAN 有中文名")
    truthy('"journal": "岸记' in js, "PRESET_LABELS 有中文短名")
    rg = (ROOT / "regress.py").read_text(encoding="utf-8")
    truthy('T("shore-journal"' in rg, "判据已挂进 regress.py 的 TESTS")
    pj = json.loads((ROOT / "rongke/presets/journal.json").read_text(encoding="utf-8"))
    eq(pj["id"], "journal", "预设 id == 文件名 stem")
    eq(pj["scenes"], ["writing"], "scenes 在 contract.SCENE_KEYS 内")
    eq([r["name"] for r in pj["plugins"] if r.get("op") == "insert"],
       ["@external/shore-journal-voice", "@external/shore-journal"],
       "两行 insert 按体例在前、工具在后的顺序")
    truthy("external-shore-journal" in
           next(r["config"]["sections"] for r in pj["plugins"]
                if r.get("id") == "prompt"), "prompt patch 真的追加了外部段")

    print("== ⑩ 双主题：只换皮，不换台账 ==")
    song_html = JournalBook(tmp, theme="宋版").render_book()
    casual_html = JournalBook(tmp).render_book()
    truthy("岸記" in song_html and "--paper:#f8f4ea" in song_html,
           "宋版仍是原来那套陈墨古籍皮")
    truthy("岸记" in casual_html and "岸記" not in casual_html,
           "手记版用简体标题，不假装古籍")
    def norm(s):
        # 两版只差 CSS 与标题字形（繁/简）：都归一成简体再比内容。
        return re.sub(r"<style>.*?</style>", "", s, flags=re.S).replace("岸記", "岸记")

    eq(norm(song_html), norm(casual_html),
       "剥离 CSS 与字形后两版逐字相同（只换皮，不动内容）")
    for key in ("2026-11-02", "2026-11-03", "registry 63 工具", RULE):
        truthy(key in song_html and key in casual_html,
               f"两版都保住内容 {key[:14]}")
    truthy("&lt;script&gt;" in song_html and "&lt;script&gt;" in casual_html,
           "两版都转义（换皮没把安全判据换掉）")
    truthy("http" not in song_html and "http" not in casual_html,
           "两版都零外部依赖")
    raises(lambda: JournalBook(tmp, theme="碑刻"), "非法主题 ⇒ 拒绝")
    eq(t.config_schema["theme"]["required"], False, "theme 是可选配置")
    eq(t.config_schema["theme"]["default"], "手记", "可选配置才有 default，且默认手记")
    eq("default" in t.config_schema["dir"], False, "必填项依然没有 default")
    eq(caps.schema_discipline(), [], "加主题没打破全仓 schema 纪律")
    truthy('"theme": "手记"' in
           (ROOT / "rongke/presets/journal.json").read_text(encoding="utf-8"),
           "预设显式声明主题（不是靠代码里的默认值蒙混）")
    reg5 = ToolRegistry()
    ctx5 = {"registry": reg5}
    t().mount({"dir": str(tmp / "songtool"), "expose": True, "theme": "宋版"}, ctx5)
    reg5.get("journal_open").fn(date="2026-11-08", water="平", question="换皮会不会动台账")
    reg5.get("journal_note").fn(kind="验", text="宋版是否真生效",
                                evidence="读回 index.html 的 CSS", verdict="配置在场 ≠ 已生效")
    h5 = (tmp / "songtool" / "index.html").read_text(encoding="utf-8")
    truthy("--paper:#f8f4ea" in h5 and "换皮会不会动台账" in h5,
           "工具层传宋版 ⇒ 真渲染出宋版（不是只写在配置里）")
    h4 = (tmp / "tools" / "index.html").read_text(encoding="utf-8")
    truthy("#fdfaf3" in h4, "工具层不传 theme ⇒ 默认手记版")
    eq(JournalBook(tmp / "tools").rows(), JournalBook(tmp / "tools").rows(),
       "同一台账读两次一致（换皮不写台账）")

print()
if _fail:
    print(f"[DONE] 有失败 · {_fail} 项")
    raise SystemExit(1)
print("[DONE] 岸记记忆插件判据全部通过")
