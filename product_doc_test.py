"""产品说明里的数字必须与代码对得上。

为什么单独做这条：文档里的数字是最容易腐坏的东西 —— 代码改了 59 个工具
变成 60 个，文档还写着 59，而**没有人会去数**。一条机械对账比十次
人工复核可靠。

判据打在「文档声称的数 == 代码算出的数」上，不是「文档里有没有数字」。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

_fail = 0


def ok(m):
    print(f"✔ {m}")


def bad(m):
    global _fail
    _fail += 1
    print(f"✘ {m}")


doc = (ROOT / "docs" / "产品说明.md").read_text(encoding="utf-8")

# ── [1] 工具数与域数 ──
# ★ 口径必须与 **CLI 自己印出来的那个**一致，不能另算一套：
#   `--tools` 表头写的是「工具 N 个（领域 M）」，
#   其中 N = len(registry)，M = len(Rongke._installed)（域插件安装数），
#   而**不是**工具 `domain` 字段的取值个数（那个只有 6）。
#   我第一版按 `domain` 字段数了一遍，得出 6，与文档的 33 打架 ——
#   量错了对象，不是文档错了。
#
# ★ `--skip-counts`：CI 的裸环境里没装 MCP 服务端，工具数是 56 而不是 59。
#   那个差距不是缺陷，是环境差异。所以裸环境里**跳过**这一段的数字核对，
#   其余（价格/额度/链接/诚实点）照常核 —— 不要因为一处量不了就整条不跑。
SKIP_COUNTS = "--skip-counts" in sys.argv

if SKIP_COUNTS:
    print("· --skip-counts：跳过工具数/域数的核对（裸环境里没装 MCP 服务端）")
else:
    from rongke.config import Config
    from rongke.loop import Rongke

    cfg = Config.load()
    # 与 `rongke --tools` 默认同口径：MCP 服务端**装**（那 3 个工具算进来）
    rk = Rongke(cfg, install_plugins=False, install_mcp_servers=True)
    n_tools = len(rk.registry)
    n_domains = len(getattr(rk, "_installed", {}) or {})
    rk.close()

    m = re.search(r"内置 \d+ 个工具[、，]\s*\d+ 个域", doc)
    if not m:
        bad("文档里找不到「内置 N 个工具、M 个域」这句（或句式变了）")
    else:
        d_tools = int(re.search(r"内置 (\d+) 个工具", m.group(0)).group(1))
        d_domains = int(re.search(r"(\d+) 个域", m.group(0)).group(1))
        if d_tools == n_tools:
            ok(f"工具数与 CLI 口径一致（{n_tools}）")
        else:
            bad(f"工具数对不上：文档写 {d_tools}，代码是 {n_tools}")
        if d_domains == n_domains:
            ok(f"域数与 CLI 口径一致（{n_domains}）")
        else:
            bad(f"域数对不上：文档写 {d_domains}，代码是 {n_domains}")

# ── [2] 图型数 ──
from rongke.analysis import CHART_KINDS

m = re.search(r"(\d+) 种图型", doc)
if not m:
    bad("文档里找不到图型数")
else:
    if int(m.group(1)) == len(CHART_KINDS):
        ok(f"图型数与代码一致（{len(CHART_KINDS)}）")
    else:
        bad(f"图型数对不上：文档写 {m.group(1)}，代码是 {len(CHART_KINDS)}")

# ── [3] 六道门 ──
# 门名必须真的都在 figure.py 的审计里有对应（用文档里的名字去代码里找）
GATES = ["source", "glyphs", "collision", "font_fallback",
         "text_containment", "alignment"]
fig_src = (ROOT / "rongke" / "figure.py").read_text(encoding="utf-8")
missing = [g for g in GATES if g not in fig_src]
if missing:
    bad(f"文档说的门在 figure.py 里找不到：{missing}")
else:
    ok(f"六道门都在 figure.py 里（{len(GATES)} 道）")
if "六道" in doc:
    ok("文档说的是六道")

# ── [4] 套餐价格与额度 ──
from rongke.billing import PLANS, PLAN_ORDER

for pid in PLAN_ORDER:
    p = PLANS[pid]
    if p["name"] and p["name"] not in doc:
        bad(f"套餐「{p['name']}」没出现在产品说明里")
    if pid != "free" and p["price_usd"] is not None:
        s = f"${p['price_usd']:.0f}"
        if s not in doc:
            bad(f"套餐 {pid} 的月费 {s} 没出现在产品说明里")
    if p["quota_usd"] is not None:
        s = f"${p['quota_usd']:.0f}"
        if s not in doc:
            bad(f"套餐 {pid} 的额度 {s} 没出现在产品说明里")
ok("套餐价格与额度都能在文档里找到对得上的数")

# ── [5] 必须声明的诚实点 ──
# ★ 判据用**正则**而不是裸字面量：中文里"没有接支付网关""不接支付网关"
#   "未接支付"说的是同一件事，钉死某一个措辞会让以后换个说法就假红。
#   这里要判的是**这件事有没有被说出来**，不是用了哪个词。
must = [
    (r"没有接|不接|未接|未实际收", "支付未接"),
    (r"UNKNOWN", "UNKNOWN 的语义"),
    (r"单机", "单机定位"),
    (r"专有", "许可证性质"),
    (r"占位", "定价是占位"),
]
for pat, label in must:
    mm = re.search(pat, doc)
    if mm:
        ok(f"文档声明了{label}（匹配「{mm.group(0)}」）")
    else:
        bad(f"文档没声明{label}（正则 {pat} 没命中）")

# ── [6] 引用的文件真实存在 ──
for m in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", doc):
    label, target = m.group(1), m.group(2)
    if target.startswith("http") or target.startswith("#"):
        continue
    p = (ROOT / "docs" / target).resolve()
    if not p.exists():
        bad(f"文档里的链接 {label} → {target} 目标不存在")
ok("文档里的相对链接目标都存在")

# ── [7] README 是产品门面：可以精简，但不许空成转发页 ──
# 原来卡 >90KB 防的是"README 被 docs/产品说明.md 顶掉"；2026-10-30 起
# README 主动收成简洁版，判据改成"关键章节都在 + 不是空壳"，防的还是同一件事。
if (ROOT / "README.md").exists():
    sz = (ROOT / "README.md").stat().st_size
    txt = (ROOT / "README.md").read_text(encoding="utf-8")
    need = ["## 安装", "## 快速开始", "## 九条判定", "regress.py"]
    missing = [k for k in need if k not in txt]
    if sz > 4_000 and not missing:
        ok(f"README 是完整门面（{sz // 1024} KB，安装/快速开始/判定/回归都在）")
    else:
        bad(f"README 只有 {sz // 1024} KB，缺 {missing} —— 是不是被顶掉或删空了？")

print()
if _fail:
    print(f"✘ 失败 {_fail} 项")
    sys.exit(1)
print("✔ 产品说明与代码对账通过")
