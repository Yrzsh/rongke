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
check("JS 语法通过", subprocess.run(["node", "--check", str(ROOT / "rongke/web/static/mentor.js")], capture_output=True).returncode == 0)
if FAILED:
    print(f"✘ {len(FAILED)} 项失败")
    sys.exit(1)
print("全部通过 ✔")
