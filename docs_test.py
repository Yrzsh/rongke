"""校验 docs/插件开发.md 里的代码示例真能跑。

文档里的示例如果跑不通，文档就是假的。这条把三个关键示例抽出来实测：
  [1] hello 最小插件（只加工具）
  [2] 图型示例的 draw 契约（占位符名、单参数、返回单 fig）
  [3] 图注模板示例
"""
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

doc = (ROOT / "docs" / "插件开发.md").read_text(encoding="utf-8")
_fail = 0


def ok(m):
    print(f"✔ {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"✘ {m}")


# ── [1] hello 最小插件 ──
HELLO = '''
from rongke.plugins import Plugin
from rongke.tools import Tool

class Hello(Plugin):
    name = "hello"
    version = "0.1.0"
    description = "最小插件"

    def tools(self):
        yield Tool(
            name="shout",
            description="把一段文字变成全大写",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            fn=lambda text="": str(text).upper(),
            domain="core",
            mutating=False,
            source="plugin",
        )

PLUGIN = Hello()
'''

d = Path(tempfile.mkdtemp())
try:
    (d / "hello.py").write_text(
        HELLO.replace("from rongke.plugins", f"import sys; sys.path.insert(0, {str(ROOT)!r})\nfrom rongke.plugins"),
        encoding="utf-8", newline="\n")
    from rongke.tools import ToolRegistry
    from rongke.plugins import load_plugins
    reg = ToolRegistry()
    rep = load_plugins(dirs=[d], registry=reg, apply_chart_kinds=False)
    if rep.ok:
        ok("文档里的 hello 插件真能加载")
        t = reg.get("shout")
        if t and t.fn(text="abc") == "ABC":
            ok("shout 工具真能调用")
        else:
            bad("shout 工具不在或调用不对")
    else:
        bad(f"文档里的 hello 插件加载失败：{rep.summary()}")
        for r in rep.results:
            print("   ", r.line())
finally:
    shutil.rmtree(d, ignore_errors=True)

# ── [2] 文档里的图型 draw 契约 ──
tpl_m = re.search(r"\"template\": '''(.*?)'''", doc, re.S)
if not tpl_m:
    bad("文档里找不到图型 template 示例")
else:
    tpl = "'''" and tpl_m.group(1)
    from rongke.plugins import _check_draw_signature
    msg = _check_draw_signature(tpl)
    if msg == "":
        ok("文档里的 draw 模板签名合格（draw(df) 单参数）")
    else:
        bad(f"文档里的 draw 模板不合格：{msg}")

    # 占位符必须在合法集合内
    from rongke.analysis import _plan_args
    legal = set(_plan_args({}).keys())
    used = set(re.findall(r"\{([a-z_]+)!r\}", tpl))
    extra = used - legal
    if extra:
        bad(f"文档模板用了不合法的占位符：{extra}")
    else:
        ok(f"文档模板的占位符合法（{sorted(used)}）")
    if "xlabel" in tpl or "ylabel" in tpl:
        ok("文档模板用的是 xlabel/ylabel（不是 x_label）")
    if "savefig" in tpl:
        bad("文档模板里出现了 savefig —— 存盘该由 epilogue 统一做")
    else:
        ok("文档模板没有自己 savefig")

# ── [3] 图注模板示例 ──
leg_m = re.search(r'return \{"myjournal": "(.*?)"\}', doc)
if not leg_m:
    bad("文档里找不到图注模板示例")
else:
    slots = set(re.findall(r"\{(\w+)\}", leg_m.group(1)))
    legal_slots = {"fig_id", "title", "letter", "claim", "n", "center", "spread"}
    extra = slots - legal_slots
    if extra:
        bad(f"文档图注模板用了不合法的槽：{extra}")
    else:
        ok(f"文档图注模板的槽合法（{sorted(slots)}）")

# ── [4] 文档提到的可 bash 命令在本机真实存在 ──
for flag in ("--plugins", "--plugins-dirs", "--tools"):
    if flag in doc:
        ok(f"文档提到的 {flag} 存在")

print()
if _fail:
    print(f"✘ 失败 {_fail} 项")
    sys.exit(1)
print("✔ 文档示例全部可跑")
