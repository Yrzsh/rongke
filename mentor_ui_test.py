# -*- coding: utf-8 -*-
"""导师工作台的静态契约：实际接线、边界与无构建运行方式。"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
FAILED = []

def check(label, ok):
    print(("✔ " if ok else "✘ ") + label)
    if not ok:
        FAILED.append(label)

js = (ROOT / "rongke/web/static/mentor.js").read_text(encoding="utf-8")
css = (ROOT / "rongke/web/static/mentor.css").read_text(encoding="utf-8")
html = (ROOT / "rongke/web/index.html").read_text(encoding="utf-8")
ws = (ROOT / "rongke/web/static/workspace.js").read_text(encoding="utf-8")
for name in ("plan", "reading", "evidence"):
    check("文档标签与接口同源：" + name, f"['{name}'" in js or f"'{name}'" in js)
    check("文档请求限定名称：" + name, "'&doc='+doc" in js)
check("课题文档显式 slug", "encodeURIComponent(slug)" in js and "state.slug" in js)
check("卡点按状态查询", "/api/topic/blockers" in js and "status=" in js)
check("文档窗口真实传递", "offset=" in js and "limit=12000" in js)
check("新建带标题与研究问题，旧课题不覆盖", all(x in js for x in ("slug:s", "title:title", "question:question", "action:exists?'update':'use'")))
check("课题工作阶段随显式保存提交", "stage:q('#mwStage')" in js and "next_step:q('#mwNext')" in js)
check("运行中禁用切换", "live()" in js and "disabled" in js)
check("课题写请求声明JSON", "'Content-Type':'application/json'" in js)
check("请求有竞态保护", "state.seq" in js and "seq===state.seq" in js)
check("文献出处可按页复核", "openOrig" in ws and "mentorAuditCitations" in ws and "c.page" in ws)
check("历史课题读取事件而不是 CURRENT", 'd.topic' in ws and '/api/topic?slug=' in ws)
check("无 ES module 构建依赖", "export " not in js and "import " not in js)
check("深浅色共用变量", "--err-bg" in css and "#fff" not in css)
check("窄屏工作台有布局", "@media (max-width:640px)" in css)
check("脚本和样式接入", "/static/mentor.js" in html and "/static/mentor.css" in html)
check("保留每会话开关", 'id="mentorSwitch"' in html and "kb_only" in ws)
# ★ 导师回包读回事件必须有人收（2026-11-14）。内核从生产那天就
#   tee("mentor_review", ...) 推，而 case "mentor_review" 全仓没有 ——
#   同一类失效本仓已经发生过三次（mentor_audit 造了没 emit、
#   force_brief 判据写了没人调）。判据是双边的：事件名、处理分支、
#   渲染函数三件都在才算接线。
check("导师回包读回有事件分支", "case \"mentor_review\"" in ws and "renderMentorReview(d)" in ws)
check("渲染函数存在且读三态", "function renderMentorReview(d)" in ws and "d.gaps" in ws and "d.ok" in ws)
check("预演与终审在界面上说得清楚", "preview" not in ws or "rehearsal" in ws)
check("补齐清单被追加到答复时界面不装不知情", "appended_to_final" in ws)

# ★ 导师模型选择器（2026-11-14 续）。判据是**双边**的（与上面 mentor_review 同一条教训）：
#   界面有下拉框 + 它的值真的进了请求体 + 回放时能从事件恢复，三件都在才算接线。
#   只改界面、不进内核，就是本仓反复吃过的「假开关」（`force_brief` 那一类）。
check("下拉框在界面上（两个地方都要：导师横条 + 导师页）", "advisorSel" in ws and "mwAdvisor" in js)
check("选中值真的进了 /api/run", 'body.advisor = ADVISOR' in ws)
check("选中值真的进了 /api/follow（追问不能丢导师）", "advisor: ADVISOR" in ws)
check("回放时从 mentor_mode 事件恢复（历史里能看出这一场按谁的判准）", '"advisor" in d' in ws)
check("清单从内核读回（前端不自己编）", "/api/advisor" in ws)
check("读不到与「没建过」分开渲染（两件事）", "ADVISOR_ERR" in ws and "ADVISOR_ERR" in js)
check("选中的导师被删了会退回通用并说出来", "已不存在" in ws)
check("低素材在界面上有提醒（模板不能冒充导师原话）", "sufficiency" in ws and "素材偏少" in ws)
check("两个地方读的是同一份状态（不各存一份）", "typeof ADVISORS" in js and "typeof ADVISOR ===" in js)

check("JS 语法通过", subprocess.run(["node", "--check", str(ROOT / "rongke/web/static/mentor.js")], capture_output=True).returncode == 0)
if FAILED:
    print(f"✘ {len(FAILED)} 项失败")
    sys.exit(1)
print("全部通过 ✔")
