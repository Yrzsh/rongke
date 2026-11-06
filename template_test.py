# -*- coding: utf-8 -*-
"""品牌模板路线（`deck_from_template` + 基线扣除）的判据测试。

## 为什么单独一个套件，不塞进 `deck_test.py`

`deck_test.py` 已经有 364 项、管的是**体检判据**；这一份管的是**产出路径**
（保留模板页 → 填字 → 删页 → 复测 → 写 origin）。两者的夹具完全不同：
那边合成 `.pptx` 是为了"喂给审计器"，这边合成 `.pptx` 是为了"当作模板去填"。
混在一起会让两边都变慢、变难读。

## ★ 刻意**不依赖**用户本机那份真模板（47MB，路径不写死）

测试依赖用户文件 = 测试随时会因为没有那个文件而红，而红的方向是错的
（看起来像代码坏了）。⇒ 用**合成模板**：3 页、几个文本框、其中一个是
**故意越界的**（用来造"模板自带的发现"，好验基线扣除）。
真模板那条路留给人跑的探针（`runs/_regress/_zjut/_e2e_template.py`）。

## 判据不许只测单向

每条拒绝都有一对：**该拒的必须拒**，同时**该放行的必须放行**（正控）。
基线扣除尤其如此 —— 它是一条"可以少报"的判据，所以必须有
"**map 对不上就一条都不扣**"的负控钉着，否则它随时会变成洗白通道。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
SKIP = 0


def skip(label: str, why: str) -> None:
    """**SKIPPED 不算通过**：读不到就说读不到，不混进 PASS。"""
    global SKIP
    SKIP += 1
    print(f"  ~ 跳过：{label}　（{why}）")


def ck(cond, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✔ {label}")
    else:
        FAIL += 1
        print(f"  ✘ {label}" + (f"\n      {extra}" if extra else ""))


def load_cfg():
    from rongke.config import Config
    return Config.load(ROOT).deck


def py_pptx() -> str | None:
    """要 python-pptx 的解释器 —— **显式问 config**，不靠当前解释器兜底。

    （`deck_test.py` 的 `_py_pptx()` 同一条纪律：默认值只在**生产那条路**上
     被验证过；换一个宿主解释器跑，它就是一条没人测过的分支。）
    """
    d = load_cfg()
    cands = [str(d.python or "").strip(), sys.executable]
    for exe in cands:
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-c", "import pptx"], capture_output=True,
                               timeout=30)
            if r.returncode == 0:
                return exe
        except Exception:
            continue
    return None


# ══════════════════════════════════════════════════════════════════
# 夹具：合成一份 3 页"模板"（含一页故意越界的文字）
# ══════════════════════════════════════════════════════════════════
_MAKE = r'''
import sys
from pptx import Presentation
from pptx.util import Inches, Pt, Emu

out = sys.argv[1]
prs = Presentation()
prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
blank = prs.slide_layouts[6]          # 空白版式

def tb(slide, name, x, y, w, h, pt, text):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    box.name = name
    tf = box.text_frame
    tf.text = text
    tf.paragraphs[0].runs[0].font.size = Pt(pt)
    return box

# ① 封面：标题 32pt（2.0in 宽 → 容量小），副标题 14pt
s = prs.slides.add_slide(blank)
tb(s, "Title", 1.0, 2.0, 2.0, 0.6, 32, "请输入标题")
tb(s, "Sub",   1.0, 3.0, 4.0, 0.4, 14, "汇报人：XXX")

# ② 内容页：正文 14pt；**另加一个故意越界的文本框**（造基线用）
s = prs.slides.add_slide(blank)
tb(s, "Body",  1.0, 1.0, 4.0, 1.2, 14, "请输入文本请输入文本")
tb(s, "Over",  12.6, 0.2, 2.0, 0.6, 20, "越界的装饰字")

# ③ 结尾页
s = prs.slides.add_slide(blank)
tb(s, "End",   1.0, 3.0, 6.0, 1.0, 28, "感谢各位老师")

# ④ **组合页** —— 组合（p:grpSp）里的文字，`slide.shapes` **摸不到**。
#    实测真模板有 14 处这样的文字（其中 8 处是四个章节页上的「请输入文本」），
#    不管它们，页面上就明晃晃露着占位文本，而所有判据都是绿的。
s = prs.slides.add_slide(blank)
tb(s, "Solo",  1.0, 0.6, 4.0, 0.5, 20, "组合外的字")
_g1 = tb(s, "InA", 1.0, 2.0, 2.0, 0.4, 14, "组合甲")
_g2 = tb(s, "InB", 1.0, 2.6, 2.0, 0.4, 14, "组合乙")
s.shapes.add_group_shape([_g1, _g2])      # 两者被**移进**组合
prs.save(out)
'''


def make_template(dst: Path, py: str) -> bool:
    r = subprocess.run([py, "-c", _MAKE, str(dst)], capture_output=True)
    return r.returncode == 0 and dst.is_file()


_INSPECT = r'''
import sys
from pptx import Presentation
prs = Presentation(sys.argv[1])
s = prs.slides[0]
top = list(s.shapes)

def flat(shapes, out):
    for sh in shapes:
        out.append(sh)
        sub = getattr(sh, "shapes", None)
        if sub is not None:
            flat(list(sub), out)
    return out

print(len(top), len(flat(top, [])))
'''


def slot(i, name, w, h, pt, role, sample, orig, _cap=None):
    """槽位。**容量按公式现算** —— 不许手填。

    踩过：第一版手写 `cap`，于是夹具里出现了"第二个真相"（写 4、公式算 4 对得上，
    但另一处写 25、公式算 28 对不上），把我引去查一个没坏的实现。
    验的是**被测方**算得对不对，夹具这边的容量必须与它同源。

    ★ 2026-09-22：模型搬进 `rongke/slotfit.py` 之后，这里也改成**调它**。
      先前这里是"抄一份公式"，同源的只是**那个抄本**；模型一改（em 宽度、
      真实内边距、原文行数），夹具就会当场分家 —— 而那会表现成
      "产品坏了"，不是"夹具旧了"。
    """
    from rongke import slotfit as SF
    _c0, _, _ = SF.slot_capacity(w, h, pt)
    ml = SF.needed_lines([sample], _c0)
    cpl, lines, cap = SF.slot_capacity(w, h, pt, min_lines=ml)
    return {"i": i, "name": name, "x": 0.0, "y": 0.0, "w": w, "h": h,
            "pt": pt, "pt_inherited": False, "role": role, "orig_chars": orig,
            "ins_emu": list(SF.INSET_DEF_EMU), "wrap": "square", "vert": "",
            "min_lines": ml,
            "cap_chars": int(cap), "cap_cpl": cpl, "cap_lines": lines,
            "sample": sample}


def slots_doc() -> dict:
    return {
        "source": "(synthetic)", "slide_size": [12192000, 6858000],
        "aspect": 1.7778, "default_pt": 18.0,
        "pages": {
            "cover": {"label": "封面", "entries": [{"id": "cover", "src": 1, "slots": [
                slot(0, "Title", 2.0, 0.6, 32, "heading", "请输入标题", 4, 4),
                slot(1, "Sub", 4.0, 0.4, 14, "label", "汇报人：XXX", 6, 18),
            ]}]},
            "body": {"label": "内容页", "entries": [{"id": "body", "src": 2, "slots": [
                slot(0, "Body", 4.0, 1.2, 14, "body", "请输入文本请输入文本", 10, 120),
                slot(1, "Over", 2.0, 0.6, 20, "anchor", "越界的装饰字", 6, None),
            ]}]},
            # 组合页：形状顺序 = [Solo, 组合, InA, InB]（`add_group_shape` 把组挂到末尾）
            # ⇒ 摊平后索引 0=Solo / 1=组合 / 2=InA / 3=InB
            "grp": {"label": "组合页", "entries": [{"id": "grp", "src": 4, "slots": [
                slot(0, "Solo", 4.0, 0.5, 20, "heading", "组合外的字", 5, 20),
                slot(2, "InA", 2.0, 0.4, 14, "label", "组合甲", 3, 8),
                slot(3, "InB", 2.0, 0.4, 14, "label", "组合乙", 3, 8),
            ]}]},
            "closing": {"label": "结尾页", "entries": [{"id": "closing", "src": 3, "slots": [
                slot(0, "End", 6.0, 1.0, 28, "heading", "感谢各位老师", 6, 25),
            ]}]},
        },
    }


def main() -> int:
    print("=== 品牌模板路线 ===")
    from rongke import deck as D

    # ── §1 公式单源：谁都别自己抄一份 ────────────────────────
    print("\n[1] 容量模型单源（`rongke/slotfit.py`）")
    from rongke import slotfit as SF
    try:
        import deck_template_extract as EX
    except Exception as e:
        ck(False, "导得到抽取器", f"{type(e).__name__}: {e}")
        EX = None
    if EX is not None:
        ck(EX.slot_capacity is SF.slot_capacity,
           "抽取器用的是 `slotfit.slot_capacity`（同一个函数对象，不是抄一份）")
        cases = [(2.17, 0.57, 28.0), (5.26, 1.01, 54.0), (1.58, 0.64, 24.0),
                 (7.18, 0.71, 12.0), (0.93, 0.54, 44.0), (13.33, 7.5, 96.0)]
        same = all(D.slot_capacity(w, h, pt) == SF.slot_capacity(w, h, pt)
                   for w, h, pt in cases)
        ck(same, f"{len(cases)} 组几何下 `deck.slot_capacity` 与 slotfit 完全一致")
        ck(D.slot_capacity(2.0, 0.6, None) == (None, None, None),
           "字号读不到 ⇒ 三项都 None（不是 0、不是猜）")
        # ★ 承重：把内边距改掉，结果必须变 —— 否则上面那条是恒真
        a = SF.slot_capacity(2.17, 0.57, 28.0)
        b = SF.slot_capacity(2.17, 0.57, 28.0, ins_emu=(0, 0, 0, 0))
        ck(b[0] > a[0], f"★ 承重自检：内边距归零后每行容量变大（{a[0]:.2f}→{b[0]:.2f}）")

    # ── §1b em 模型：**汉字与英文不能按同一个数算** ─────────────
    # 这是 2026-09-22 那条血债的核心。写成断言，别让它悄悄退回去。
    print("\n[1b] em 宽度模型（中文 1.0 / 英文数字 ≈0.56）")
    ck(abs(SF.em_width("目录") - 2.0) < 1e-9, "两个汉字 = 2em")
    ck(SF.em_width("Contents") < SF.em_width("目" * 8),
       f"★ 8 个英文字符（{SF.em_width('Contents'):.2f}em）比 8 个汉字（8em）窄")
    ck(abs(SF.ASCII_RATIO * SF.char_em("a") - 1.0) < 0.05,
       f"★ 换算系数与字符表自洽：0.556em × {SF.ASCII_RATIO} = "
       f"{SF.ASCII_RATIO * SF.char_em('a'):.2f}em ≈ 1 个汉字")
    # ★ 存在性证明：作者原文需要的行数**只能把上限抬高、绝不能压低**
    ck(SF.slot_capacity(2.0, 0.6, 20, min_lines=3)[1] == 3
       and SF.slot_capacity(2.0, 0.6, 20)[1] == 1,
       "★ `min_lines`（原文行数）只会抬高行数上限，不会压低")
    # ★ 不换行：框高说了不算
    ck(SF.slot_capacity(2.0, 0.1, 20, wrap="none", min_lines=2)[1] == 2
       and SF.slot_capacity(2.0, 0.1, 20, min_lines=0)[1] == 1,
       "★ `wrap=\"none\"` ⇒ 行数由原文段数决定，不看框高")
    # ★ 竖排：行列互换
    ck(SF.slot_capacity(0.5, 2.0, 20, vert="eaVert")[0]
       > SF.slot_capacity(0.5, 2.0, 20)[0],
       "★ 竖排框的长边是高度 ⇒ 每行容量按高度算（不换这一下相差 4 倍）")
    # 已知值复核：任何一个数变了都要有人知道
    _kv = SF.slot_capacity(2.17, 0.57, 28.0)
    ck(all(abs(a - b) < 1e-9 for a, b in zip(_kv, (5.065714285714286, 1,
                                                  5.065714285714286))),
       f"已知值：2.17in/0.57in/28pt ⇒ (5.066, 1, 5.066)，实得 {_kv}")

    # ── §1c 反 H 型：公式**不许**再有第二份 ───────────────────
    print("\n[1c] 防复制：容量算式只准出现在 slotfit.py")
    _srcs = {"rongke/deck.py": (ROOT / "rongke/deck.py").read_text(encoding="utf-8"),
             "deck_template_extract.py": (ROOT / "deck_template_extract.py")
             .read_text(encoding="utf-8")}
    # 旧公式的**指纹**是 `… * 72 - …`（新式写成 `(w_in - il - ir) * 72.0`）。
    # 只盯代码形状，不盯散文 —— 抽取器的文档里写明这条公式是**有意的**。
    _bad = [k for k, v in _srcs.items() if "* 72 - " in v]
    ck(not _bad, f"`deck.py` / 抽取器里没有抄公式（有的：{_bad}）"
                 if _bad else "`deck.py` / 抽取器里都只是 import，没有抄旧公式")
    ck("slotfit" in _srcs["rongke/deck.py"]
       and "slotfit" in _srcs["deck_template_extract.py"],
       "两处都明确 import slotfit")
    _sf = (ROOT / "rongke/slotfit.py").read_text(encoding="utf-8")
    ck("/ (pt * LINE_SPACING)" in _sf and "EM_FLOOR" in _sf,
       "★ 公式**在场**（不然「两边都没有」也会假绿）")

    # ── §2 槽位表读不动的样子 ─────────────────────────────────
    print("\n[2] `load_template`：读不到不许静默降级")
    try:
        D.load_template("绝对不存在这个名字")
        ck(False, "不存在的槽位表必须抛")
    except FileNotFoundError as e:
        ck("deck_template_extract.py" in str(e),
           "抛 FileNotFoundError，且在报错里点出了生成命令")

    # ── §3 开工清单必须把容量摆到模型眼前 ─────────────────────
    print("\n[3] `template_plan_text` 的覆盖面")
    doc = slots_doc()
    txt = D.template_plan_text(doc)
    ck("cover" in txt and "body" in txt and "closing" in txt, "列出全部页 id")
    # ★ 期望值从夹具**现算**：容量模型一改（em / 原文行数），写死的数字就过期，
    #   而过期的期望会红成"产品坏了"。
    _exp = doc["pages"]["cover"]["entries"][0]["slots"][0]["cap_chars"]
    _flat = txt.replace(" ", "")
    ck(f"上限{_exp}字" in _flat and f"/{int(_exp * D.slotfit.ASCII_RATIO)}字符" in _flat,
       f"把每个槽的容量写出来（封面标题 {_exp} 字 = "
       f"{int(_exp * D.slotfit.ASCII_RATIO)} 字符）")
    ck("请输入文本" in txt, "带上原文样例（模型据此判断这是哪个槽）")
    ck("汇报人：XXX" in txt, "短槽的样例也在")
    # ★ `deck_from_template` **只填字** —— 图片/图表/表格换不了。清单必须说出口，
    #   否则模型会以为"这页填完就齐了"，而 p4 那个甜甜圈图带着模板的示例数据
    #   （"销售额 20:80"）交出去 —— **假数字进组会汇报，比缺一张图严重得多**。
    doc3 = slots_doc()
    doc3["pages"]["body"]["entries"][0]["assets"] = {
        "pics": 3, "charts": 1, "tables": 0, "smartart": 0, "pic_ph": 0}
    t3 = D.template_plan_text(doc3)
    ck("自带照片" in t3, "清单里报了**自带照片**张数")
    ck("自带示例图表" in t3, "清单里给自带图表打了 ⚠️")
    ck("换不了图表数据" in t3 or "换不了" in t3,
       "★ 明说**换不了**（不说 ⇒ 模型以为填完字就齐了）")
    ck("路线乙" in t3, "并给出出路：要真实数据图就改走路线乙")
    # ★★ 夹具里**本来没有 diagram 槽**，于是 `template_plan_text` 的格式化路径
    #    （`[{s['i']:>2d}]`）压根没被走到 —— 而 `d<i>` 是**字符串**，`:>2d` 当场抛
    #    `ValueError`。崩的偏偏是**模型必须先调**的那个只读工具。
    #    ⇒ 夹具里补一个 diagram 槽，把这条路径钉住。
    doc2 = slots_doc()
    doc2["pages"]["body"]["entries"][0]["slots"].append({
        "i": "d0", "name": "ppt/diagrams/data1.xml#0", "x": None, "y": None,
        "w": None, "h": None, "pt": None, "pt_inherited": True, "role": "diagram",
        "orig_chars": 3, "cap_chars": None, "cap_cpl": None, "cap_lines": None,
        "sample": "图示字", "part": "ppt/diagrams/data1.xml", "part_index": 0})
    try:
        t2 = D.template_plan_text(doc2)
        ck("d0" in t2 and "图示字" in t2,
           "★ 清单能吃下 SmartArt 槽（`d<i>` 走 `str()`，不是 `:>2d`）")
        ck("上限?字" in t2.replace(" ", ""),
           "★ SmartArt 的容量如实写「?」（量不出就说量不出，不编数）")
        ck("字符" in t2, "★ 容量按 em 写：`N字/M字符`（英文不再按汉字数算）")
    except Exception as e:
        ck(False, "★ 清单能吃下 SmartArt 槽", f"{type(e).__name__}: {e}")

    # ── §3b 随库槽位表的**常驻**守卫（两条，缺一不可）──────────
    # 为什么必须常驻：「这张表可用吗」这件事，只有这里在守。
    #   ① 表里的数 == 按表内事实重算的数  → 表与执行体没分家
    #   ② 模板作者自己写的原文装得下      → 这个数没算小（存在性证明）
    # 只做 ① 是不够的：两边可以**一起错**（旧版就一起错了：整段按 1em/字，
    #   于是莫干山 703 槽里 156 槽连模板自己的文案都判"装不下"）。
    print("\n[3b] 随库槽位表常驻守卫（改完 `slotfit.py` 必须重抽，这里会当场失守）")
    _tdir = ROOT / "docs" / "deck-templates"
    _tabs = sorted(p for p in _tdir.glob("*/slots.json")) if _tdir.is_dir() else []
    ck(bool(_tabs), f"随库槽位表 {len(_tabs)} 份：{[p.parent.name for p in _tabs]}")
    for _p in _tabs:
        try:
            _d = json.loads(_p.read_text(encoding="utf-8"))
        except Exception as e:
            ck(False, f"{_p.parent.name} 读得动", f"{type(e).__name__}: {e}")
            continue
        _n1, _bad1 = EX.verify_capacity(_d) if EX else (0, ["导不到抽取器"])
        _n2, _bad2 = EX.verify_orig_fits(_d) if EX else (0, ["导不到抽取器"])
        ck(not _bad1, f"{_p.parent.name}：{_n1} 个可比槽，容量与表内事实逐位一致",
           "\n".join(_bad1[:3]))
        ck(not _bad2, f"{_p.parent.name}：{_n2} 个可比槽，**模板自己的原文全装得下**"
                      f"（容量没被算小）", "\n".join(_bad2[:3]))
        # ★ 承重：把一格的容量改小一点，②必须当场报出来（证明它不是恒真）
        if _n2 and not _bad2:
            _d2 = json.loads(json.dumps(_d))
            for _blk in _d2.get("pages", {}).values():
                _hit = False
                for _e in _blk.get("entries", []):
                    for _s in _e.get("slots", []):
                        if _s.get("role") not in ("diagram", "anchor", "empty") \
                                and _s.get("cap_lines") and _s["cap_lines"] > 1:
                            _s["cap_lines"] = 1
                            _hit = True
                            break
                    if _hit:
                        break
                if _hit:
                    break
            _n3, _bad3 = EX.verify_orig_fits(_d2) if EX else (0, [])
            ck(bool(_bad3), "★ 承重自检：偷偷把某格行数改成 1 ⇒ ② 当场报出来")

    # ── §3c 样例稿 spec 的常驻守卫 ─────────────────────────────
    # 为什么要有：`demo-spec.json` 是**签进仓库的产物**（下一份模板接入时的页序范例）。
    # 它按槽位索引写字，而索引是**表重抽 / 页型改名**就会变的 —— 没人守着就会悄悄腐烂，
    # 等到下一个人照它出稿时才发现（那时离"为什么"已经很远了）。
    # 这里只读表、不出稿，所以很便宜；出稿那一步由 `deck_template_demo.py` 干。
    print("\n[3c] 样例稿 spec 常驻守卫（页型在位 / 槽位全覆盖 / 每段都装得下）")
    if not _tdir.is_dir():
        skip("样例稿 spec 守卫", "没有 docs/deck-templates 目录")
    else:
        _specs = sorted(_tdir.glob("*/demo-spec.json"))
        if not _specs:
            skip("样例稿 spec 守卫", "仓库里还没有样例稿 spec")
        for _sp in _specs:
            _nm = _sp.parent.name
            try:
                _sd = json.loads(_sp.read_text(encoding="utf-8"))
            except Exception as e:
                ck(False, f"{_nm}：spec JSON 读得动", f"{type(e).__name__}: {e}")
                continue
            _tn = str(_sd.get("template_name") or "")
            ck(_tn == _nm, f"{_nm}：spec 里的模板名与目录一致（{_tn!r}）")
            try:
                _tp = D.template_pages(D.load_template(_tn))
            except Exception as e:
                ck(False, f"{_nm}：槽位表加载得动", f"{type(e).__name__}: {e}")
                continue
            _miss_page, _miss_slot, _extra, _over = [], [], [], []
            for _pg in _sd.get("pages") or []:
                _pid = str(_pg.get("page") or "")
                _info = _tp.get(_pid)
                if _info is None:
                    _miss_page.append(_pid)
                    continue
                _have = {str(x["i"]) for x in _info["slots"]
                         if x.get("role") not in ("anchor", "fixed", "empty")}
                _want = {str(k) for k in (_pg.get("slots") or {})}
                _miss_slot += [f"{_pid}[{k}]" for k in sorted(_have - _want)]
                _extra += [f"{_pid}[{k}]" for k in sorted(_want - _have)]
                for _k, _t in (_pg.get("slots") or {}).items():
                    _s = next((x for x in _info["slots"] if str(x["i"]) == str(_k)), None)
                    if not _s or not _s.get("cap_cpl"):
                        continue
                    _t = _t if isinstance(_t, str) else "\n".join(_t)
                    if not SF.fits(_t, _s["w"], _s["h"], _s["pt"],
                                   ins_emu=_s.get("ins_emu"),
                                   wrap=_s.get("wrap") or "square",
                                   vert=_s.get("vert") or "",
                                   min_lines=_s.get("min_lines") or 0):
                        _over.append(f"{_pid}[{_k}]")
            ck(not _miss_page, f"{_nm}：{len(_sd.get('pages') or [])} 页的页型全在位",
               "、".join(_miss_page[:4]))
            ck(not _miss_slot, f"{_nm}：**没有漏填的槽**（漏一个就交出击占位文本）",
               "、".join(_miss_slot[:6]))
            ck(not _extra, f"{_nm}：没有不存在的槽索引",
               "、".join(_extra[:6]))
            # ⚠️ 用 `SF.fits`（**执行体那把尺子**）而不是重算一遍容量 ——
            #    重算就是第二份实现，一旦分家就变成 H 型。
            ck(not _over, f"{_nm}：每段都装得下（按 em，与出稿时同一把尺子）",
               "、".join(_over[:6]))

    # ── §3d 简报（agent-brief.txt）不许和内核分家 ─────────────
    # 血债（2026-09-22 现抓）：`agent-brief.txt` 原先是一份**手工重定向**出来的快照，
    # 全仓没有一个脚本生成它 ⇒ 内核把容量模型从"按行数粗算"改成"逐字符 em 精算"、
    # 又把模板校名从 `fixed` 改走之后，它**落后了整整两代**：
    #   · 旧文还在教"中文字按字算"（真口径：英文数字只占 0.56 个字）；
    #   · `chart` 页它说"可填 12 槽 / 上限 144 字"，现行是 **13 槽 / 97 字**
    #     （`[25] 'X%'` 那个槽是甜甜圈图的假数据标签，旧表里它还是 `anchor`）。
    # 这偏偏是**喂给别的 agent 读**的那一份 ⇒ 错得更远。典型的 **H 型两处真相**。
    # ⇒ 现在它有生成器（`deck_template_slots.py --brief`）＋ 这条守卫：
    #   **凡目录里放着 brief，它就必须逐字节等于现渲染**，再漂移就报红。
    print("\n[3d] 简报不许和内核分家（agent-brief.txt 逐字节等于现渲染）")
    try:
        import deck_template_slots as _SL
    except Exception as e:
        ck(False, "导得到简报生成器", f"{type(e).__name__}: {e}")
        _SL = None
    _briefs = sorted(_tdir.glob("*/agent-brief.txt")) if _tdir.is_dir() else []
    if _SL is None:
        pass
    elif not _briefs:
        skip("简报守卫", "仓库里还没有 agent-brief.txt")
    else:
        for _bf in _briefs:
            _bn = _bf.parent.name
            try:
                _want = _SL.brief_text(_bn)
            except Exception as e:
                ck(False, f"{_bn}：简报渲染得动", f"{type(e).__name__}: {e}")
                continue
            _got = _bf.read_text(encoding="utf-8")
            ck(_got == _want,
               f"{_bn}：agent-brief.txt 与现渲染逐字节一致（改了内核就重跑 --brief）",
               f"现渲染 {len(_want)} 字符 / 文件 {len(_got)} 字符")

    # ── §3e 继承链的**页号口径**：演示顺序 ≠ 分片号（路线甲）────────────
    # 血债（2026-09-22 现抓）：`probe_pptx` 把**演示顺序**喂给了 `PhChain`，而
    # `PhChain.layout_of` 是**按部件名**建索引的 ⇒ 路线甲产物上查到不存在的 layout、
    # 一路退回 master ⇒ 同一形状真值 24pt 被算成 28pt，「估高比」从 2.32 虚高到 **2.71**，
    # 还多报两页（第 9、11 页）。**普通 deck 里两个页码恰好相等 ⇒ 这个 bug 一次都不会现形。**
    # ⇒ 守卫分两段：① 前提（这个仓库里确实存在"两个页码不等"的页，否则守卫空转）；
    #             ② 承重（同一个形状、两个键给出**不同**的比值 —— 证明键选对选错是有后果的）。
    print("\n[3e] 继承链的页号口径（演示顺序 ≠ 分片号）")
    _demo = _tdir / "moganshan" / "moganshan-demo.pptx"
    if not _demo.is_file():
        skip("继承链页号口径", "仓库里没有 moganshan-demo.pptx（样例稿）")
    else:
        import zipfile as _zfm
        import xml.etree.ElementTree as _ET
        from rongke.deck import PhChain as _Ph
        with _zfm.ZipFile(_demo) as _z:
            _order = list(D._presentation_order(_z) or [])
            _bad = [(i, p) for i, p in enumerate(_order, 1) if i != p]
            ck(bool(_bad),
               "★ 承重前提：产物里存在「演示顺序 ≠ 分片号」的页（否则这条守卫空转）"
               f" —— 实测 {_bad[:4]}",
               f"order={_order[:8]}")
            _ch = _Ph(_z)
            _pair = None
            for _i, _p in _bad[:4]:
                try:
                    _root = _ET.fromstring(_z.read("ppt/slides/slide%d.xml" % _p))
                except Exception:
                    continue
                for _sp in _root.iter():
                    if not _sp.tag.endswith("}sp"):
                        continue
                    _ok = D._sp_overflow(_sp, _ch, _p, {})      # 对键：分片号
                    _no = D._sp_overflow(_sp, _ch, _i, {})      # 错键：演示顺序
                    if _ok and _no and abs(_ok[0] / _ok[1] - _no[0] / _no[1]) > 0.05:
                        _pair = (_i, _p, _ok[0] / _ok[1], _no[0] / _no[1])
                        break
                if _pair:
                    break
            ck(bool(_pair),
               "★★ 承重自检：同一形状，**错键（演示顺序）与对键（分片号）算出不同比值**"
               " —— 把修复拿掉，虚高的 2.71 就会回来",
               (f"页={_pair[0]} 分片={_pair[1]} 对键={_pair[2]:.2f} 错键={_pair[3]:.2f}"
                if _pair else "没找到两个键结果不同的形状"))

    # ── §4 真跑一次（合成模板）───────────────────────────────
    print("\n[4] 正控：合成模板上真跑一遍")
    py = py_pptx()
    if py is None:
        ck(False, "找不到装了 python-pptx 的解释器（本套件的前提）")
        print(f"\n{PASS} 项通过 / {FAIL} 项失败")
        return 1
    work = Path(tempfile.mkdtemp(prefix="rk_tpl_"))
    from _tkit import stash
    try:
        tpl = work / "template.pptx"
        if not make_template(tpl, py):
            ck(False, "合成模板失败（夹具本身坏了，不许当产品问题）")
            print(f"\n{PASS} 项通过 / {FAIL} 项失败")
            return 1

        def run(name, pages, slots=None):
            return D.deck_from_template(
                str(work / name), pages, template_pptx=str(tpl),
                template_slots=(slots or doc), workdir=str(work), python_exe=py)

        OK_PAGES = [
            {"page": "cover", "slots": {"0": "冬虫夏草", "1": "汇报人：北岸"}},
            {"page": "body", "slots": {"0": "这是正文，写得比样例长一些也还行。"}},
            {"page": "closing", "slots": {"0": "感谢各位老师的指导"}},
        ]
        r = run("ok.pptx", OK_PAGES)
        ck(r.ok, "正控：三页合法输入 ⇒ ok", "\n".join(r.problems[:3]))
        ck(r.slides_after == 3 and r.slides_before == 4,
           f"页数：模板 4 → 产物 {r.slides_after}（请求 3 页，裁掉 1 页）")
        ck(len(r.filled) == 4, f"填了 {len(r.filled)} 个槽（期望 4）")
        ck(Path(r.origin_path).is_file(), "写了 `.origin.json`（门做基线扣除要用它）")
        ck("逐槽从磁盘读回" in "".join(r.checks), "复测是**从磁盘重开**读回来的")
        ck((work / "ok.pptx").stat().st_size > 0, "产物真的有字节")
        # 删页：只留 2 页
        r2 = run("two.pptx", OK_PAGES[:2])
        ck(r2.ok and r2.slides_after == 2, f"删页：请求 2 页 ⇒ 产物 {r2.slides_after} 页")

        # ── §5 负控 ─────────────────────────────────────────
        print("\n[5] 负控：该拒的必须拒，且要说清怎么改")
        r = run("miss.pptx", [
            {"page": "cover", "slots": {"0": "只有标题"}},
            {"page": "body", "slots": {"0": "正文"}},
            {"page": "closing", "slots": {"0": "感谢"}}])
        ck(not r.ok and any("没填" in p for p in r.problems),
           "缺槽 ⇒ 拒绝并点名", str(r.problems[:2])[:200])
        ck(any("汇报人：XXX" in p for p in r.problems),
           "缺槽的报错里带**原文样例**（模型才知道自己缺的是哪个）")

        r = run("badpage.pptx", [
            {"page": "封面", "slots": {}},
            {"page": "body", "slots": {"0": "正文"}},
            {"page": "closing", "slots": {"0": "感谢"}}])
        ck(not r.ok and any("不认识的页型" in p for p in r.problems),
           "未知页型 ⇒ 拒绝", str(r.problems[:1])[:160])

        # ⚠️ 长度要**按模型现算**，不许写死：写死的那个数会随模型漂
        #    （2026-09-22 em 模型一上，原来写死的 70 字在 9pt 下反而装得下了 ——
        #     于是这条负控变成了"缩号"，测不到"拒绝"）。
        #    取值 = 缩字号下限处的容量 + 30 ⇒ **任何字号都装不下**。
        _c0 = doc["pages"]["cover"]["entries"][0]["slots"][0]
        _, _, _cap_floor = D.slot_capacity(_c0["w"], _c0["h"],
                                           D.TEMPLATE_SZ_FLOOR)
        _too_long = "标" * (int(_cap_floor) + 30)
        r = run("over.pptx", [
            {"page": "cover", "slots": {"0": _too_long, "1": "汇报人：北岸"}},
            {"page": "body", "slots": {"0": "正文"}},
            {"page": "closing", "slots": {"0": "感谢"}}])
        ck(not r.ok and any("装不下" in p for p in r.problems),
           f"超容量（{len(_too_long)} 字，9pt 下限也只装 {int(_cap_floor)} 字）⇒ 拒绝",
           str(r.problems[:1])[:200])
        ck(any("上限" in p for p in r.problems), "报错里写出上限是多少（字/em/行）")

        r = run("dup.pptx", [
            {"page": "cover", "slots": {"0": "标题", "1": "汇报人：北岸"}},
            {"page": "cover", "slots": {"0": "标题", "1": "汇报人：北岸"}},
        ])
        ck(not r.ok and any("只能出现一次" in p for p in r.problems),
           "同一模板页用两次 ⇒ 拒绝并指出该改用 section 系列",
           str(r.problems[:1])[:160])

        r = D.deck_from_template("../escape.pptx", OK_PAGES, template_pptx=str(tpl),
                                 template_slots=doc, workdir=str(work), python_exe=py)
        ck(not r.ok and any("越界" in p for p in r.problems), "写出工作区 ⇒ 拒绝")

        r = D.deck_from_template(str(work / "notpl.pptx"), OK_PAGES,
                                 template_pptx=str(work / "没有这个文件.pptx"),
                                 template_slots=doc, workdir=str(work), python_exe=py)
        ck(not r.ok and any("模板文件不可用" in p for p in r.problems),
           "模板不在盘上 ⇒ 拒绝并指路（别去撞）")

        # ── §6 基线扣除：正控 + 负控 ─────────────────────────
        print("\n[6] 基线扣除（唯一一条「可以少报」的判据，必须两侧都测）")
        aud = D.DeckAuditor()
        rj = work / "ok_audit.json"
        rep = aud.audit(str(work / "ok.pptx"), workdir=str(work), report_path=str(rj))
        g = [x for x in rep.gates if x.name == "xml"][0]
        bl = (g.extra or {}).get("template_baseline") or {}
        ck(bl.get("subtracted", 0) >= 1,
           f"正控：模板自带的越界发现被扣掉（subtracted={bl.get('subtracted')}）",
           g.detail[:200])
        ck(g.verdict == "PASS",
           f"正控：扣完之后 xml 门 PASS（实得 {g.verdict}）")
        ck("已扣掉模板自带" in g.detail, "**扣了什么、扣了几条**写在门的话里（不静默）")

        # 负控①：把 origin 的 map 改成对不上 ⇒ 一条都不许扣
        op = Path(str(work / "ok.pptx") + ".origin.json")
        good_origin = json.loads(op.read_text(encoding="utf-8"))
        bad = dict(good_origin)
        bad["map"] = [[1, 1], [2, 2], [3, 1]]        # 不是模板真页 ⇒ 不自洽
        op.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        rep2 = aud.audit(str(work / "ok.pptx"), workdir=str(work),
                         report_path=str(work / "ok_audit2.json"))
        g2 = [x for x in rep2.gates if x.name == "xml"][0]
        ck(not (g2.extra or {}).get("template_baseline"),
           "负控①：map 不自洽 ⇒ 一条都不扣")
        ck("没能用上" in g2.detail, "负控①：而且**把原因写在门的话里**（不是静默跳过）")

        # 负控②：把 origin 整个删掉 ⇒ 不是模板产出的稿，本来就该全报
        op.unlink()
        rep3 = aud.audit(str(work / "ok.pptx"), workdir=str(work),
                         report_path=str(work / "ok_audit3.json"))
        g3 = [x for x in rep3.gates if x.name == "xml"][0]
        ck(not (g3.extra or {}).get("template_baseline"),
           "负控②：没有 origin ⇒ 不扣（普通 deck 的发现照报）")
        ck(g3.verdict != "PASS" or not g3.findings,
           "负控②：模板自带的越界在**没有 origin** 时照样报出来",
           f"verdict={g3.verdict} findings={len(g3.findings)}")
    # ── §7 组合（p:grpSp）里的文字：**摸不到就会静默漏掉** ──────
        print("\n[7] 组合里的文字（`slide.shapes` 只给顶层）")
        r = run("grp.pptx", [{"page": "grp", "slots": {
            "0": "组合外的字", "2": "改过的甲", "3": "改过的乙"}}])
        ck(r.ok, "正控：组合里的两个字都能填（索引 2 / 3）",
           "\n".join(r.problems[:3]))
        if r.ok:
            _o = Path(r.pptx)
            _chk = subprocess.run(
                [py, "-c", _INSPECT, str(_o)], capture_output=True)
            top, flat = (int(v) for v in _chk.stdout.decode().split())
            ck(top == 2 and flat == 4,
               f"★ 承重自检：同一页 `slide.shapes` 给 {top} 个、摊平后 {flat} 个"
               f"（差的就是组合里的两个）")
            ck(top < flat, "★ 若哪天只认顶层，这条会当场失守（不是恒真）")

    # ── §7b SmartArt 槽的编号：`d<i>` 必须**跨部件连续** ────────────
    # 旧写法 `for i, m in enumerate(...)` 让每个部件都从 `d0` 重新开始 ⇒
    # 一页挂**两个** SmartArt 时键当场撞车：后一个部件的槽被前一个覆盖，
    # 清单上槽数看着是够的，**产物里却原样留着「请输入文本」**。
    # ⚠️ 两份随库模板都没有这种页（**隐患未发作**）⇒ 只能**合成**一页来测。
    #    这也是本项目的纪律：判据要在**正确的靶子**上量，靶子不存在就自己造。
        print("\n[7b] SmartArt 槽的编号（`d<i>` 跨部件连续）")
        _X1 = "<a:t>甲</a:t><a:t>乙</a:t>"
        _X2 = "<a:t>丙</a:t><a:t>丁</a:t>"
        _X1b = "<a:t>甲</a:t><a:t> </a:t><a:t>乙</a:t>"     # 中间隔着空 `<a:t>`

        class _Zf:
            """只喂 `diagram_slots` 要的那一样东西：部件字节。"""
            def __init__(self, d):
                self._d = d

            def read(self, n):
                return self._d[n].encode("utf-8")

        def _num(parts):
            _old = EX.diagram_parts
            try:
                EX.diagram_parts = lambda zf, n: list(parts)     # 两个部件
                return EX.diagram_slots(_Zf(parts), 1)
            finally:
                EX.diagram_parts = _old

        try:
            _two = {"ppt/diagrams/data1.xml": _X1, "ppt/diagrams/data2.xml": _X2}
            _sl = _num(_two)
            _keys = [s["i"] for s in _sl]
            ck(_keys == ["d0", "d1", "d2", "d3"],
               f"两个部件连着编号：{_keys}", str(_keys))
            ck(len(set(_keys)) == len(_keys),
               "★ 承重自检：键不重复（旧写法在这里会撞成 d0/d1 两对）")
            ck([s["part_index"] for s in _sl] == [0, 1, 0, 1],
               "★ `part_index` 仍**按部件**计（填字要靠它定位）："
               f"{[s['part_index'] for s in _sl]}")
            # 空 `<a:t>` 也**占一个号**（不出槽、但计数照走）—— 这条是"存量槽位表
            # 不用重抽"的依据，写错方向就是全库 `d` 键集体错位。
            _blank = {"ppt/diagrams/data1.xml": _X1b}
            _kb = [s["i"] for s in _num(_blank)]
            ck(_kb == ["d0", "d2"], f"空 `<a:t>` 占号不出槽：{_kb}", str(_kb))
        except Exception as e:
            ck(False, "SmartArt 槽的编号", f"{type(e).__name__}: {e}")
    # ── §8 接线验收：模型真的调得到吗 ────────────────────────
    # 前七节验的都是**函数**。模型走的是另一条路：注册 → 域挂载 → fn → 提示词。
    # 这四段里断哪一段，函数再对也没用（`deck_live_test.py` 就是为这件事存在的）。
        print("\n[8] 接线验收")
        from rongke.loop import Rongke, mounting_decision, SYSTEM
        try:
            rk = Rongke(install_domain=False, install_mcp_servers=False)
            _names = set(rk.registry.names())
            ck("deck_template_plan" in _names and "deck_from_template" in _names,
               "两个工具都注册了")
            _dom = {k: rk.registry.get(k).domain for k in
                    ("deck_template_plan", "deck_from_template")}
            ck(set(_dom.values()) == {"ppt"}, f"都归 ppt 域（{_dom}）")
        except Exception as e:
            ck(False, "起得来 Rongke（不起 workbench / 不连 MCP）", f"{type(e).__name__}: {e}")

        # ★★ 兜底路径要真能开口（W29 实测抓到的闭包陷阱）：
        #    注册期 `except Exception as e` 里定义的嵌套 _plan 引用了 e，
        #    而 except 块结束时 Python 会删掉 e ⇒ 模板读不到时工具炸
        #    NameError，模型收到纯噪声、拿不到「改走路线乙」的路由信号。
        #    判据必须打**真调用**（registry.call），只看注册成功拦不住这一层。
        _orig_load = D.load_template
        def _boom_load(*a, **k):
            raise RuntimeError("模拟槽位表读不到")
        D.load_template = _boom_load
        try:
            _rk_bad = Rongke(install_domain=False, install_mcp_servers=False)
            _tc = _rk_bad.registry.call("deck_template_plan", {})
            ck(_tc.ok and "槽位表读不到" in (_tc.result or "")
               and "模拟槽位表读不到" in (_tc.result or ""),
               "★ 模板读不到 ⇒ deck_template_plan 说得出「读不到」（旧代码此处 NameError）",
               f"ok={_tc.ok} err={_tc.error!r}")
        except Exception as e:
            ck(False, "★ 模板读不到 ⇒ 兜底消息开口", f"{type(e).__name__}: {e}")
        finally:
            D.load_template = _orig_load

        # ★ 清单要吃**真**槽位表 —— 合成夹具吃不下真表里的全部形态
        #   （这一条正是"夹具与被测代码不同形"补上的洞）
        try:
            _real = D.template_plan_text(D.load_template("zjut"))
            ck(len(_real) > 3000 and _real.count("上限") >= 100,
               f"清单吃真槽位表：{len(_real)} 字 / {_real.count('上限')} 处容量")
            ck("[  d0]" in _real or "[   d0]" in _real,
               "真表里的 SmartArt 槽在清单里显示正常（不抛 ValueError）")
        except Exception as e:
            ck(False, "清单吃真槽位表", f"{type(e).__name__}: {e}")

        class _C:
            def __init__(self, needs_deck, stages=()):
                self.needs_deck = needs_deck
                self.stages = list(stages)

        _names = rk.registry.names() if 'rk' in dir() else []
        _dom = {kk: rk.registry.get(kk).domain for kk in _names} if _names else {}
        if _names:
            _d = mounting_decision(_C(False), _names, lambda x: _dom.get(x, ""))
            ck("ppt" not in _d["domains"],
               "契约不要 deck ⇒ **ppt 域不挂**（不白付 schema 体积）")
            _d2 = mounting_decision(_C(True), _names, lambda x: _dom.get(x, ""))
            ck("ppt" in _d2["domains"], "契约要 deck ⇒ ppt 域挂上")

        # ★★ 盯**配方**而不是盯某段散文。
        #   本项目用血债换来的教训：**「模型执行的是它读到的动作」** ——
        #   动作藏在散文里 = 模型看不见（`runs/20260921-113729` 把 .docx 当产物）。
        #   所以"品牌模板这条路"必须进**编号动作序列**，且**单源**
        #   （`DECK_TOOLCHAIN_RECIPE` 一处定义、SYSTEM 与契约提示词两处引用）。
        from rongke.contract import (DECK_ROUTE_GUIDE, DECK_TOOLCHAIN_RECIPE,
                                     deck_recipe_lines)
        # ★ 断言**渲染出来的那一段** —— 那才是模型真读到的东西。
        #   盯 `DECK_TOOLCHAIN_RECIPE` 会漏：路线说明刻意**不参与编号**
        #   （它决定走哪串步骤，不是步骤之一）。
        _all = deck_recipe_lines(indent="    ")
        ck("deck_from_template" in _all and "deck_template_plan" in _all,
           "★ 渲染出的动作序列里点了这两个工具的名字（不是散文）")
        ck("取代" in _all and "路线甲" in _all,
           "★ 明说路线甲**取代**建页那一段（不写明模型会两套都做）")
        ck("硬容量" in _all and "请输入文本" in _all,
           "带着硬容量与「不填就交占位文本」的警告")
        ck(_all.index("deck_from_template") < _all.index("create_presentation"),
           "★ 路线甲**排在** create_presentation 之前（生产动作必须靠前）")
        ck(_all.index("路线甲") < _all.index("1. "),
           "★ 而且路线说明在**编号之前**（它决定走哪串步骤，不是步骤之一）")
        ck(len(DECK_ROUTE_GUIDE) == 1 and "deck_reference" in DECK_TOOLCHAIN_RECIPE[0],
           "步骤序列本身没被搅动（第一步仍是 `deck_reference`）")
        ck("deck_from_template" in SYSTEM and "路线甲" in SYSTEM,
           "SYSTEM（f-string）里那段话真的在 —— 插错花括号会静默少一段")
    # ── §9 自带示例图表的页：产出必须带"不阻断但要说出口"的警告 ──────
    # 这一条要用**真模板**（合成夹具造不出原生图表）。真模板不在 ⇒ **如实跳过**，
        print("\n[9] 自带图表页的警告（需要真模板）")
        _cfg = load_cfg()
        _tpl_path = str(getattr(_cfg, "template", "") or "")
        _tname = D.template_name_of(_cfg)
        if not _tpl_path or not Path(_tpl_path).is_file():
            skip("自带图表页的警告",
                 "config.json 里没配 deck.template，或那个文件不在盘上")
        else:
            try:
                # ★ 页型 id 与"哪一页带自带图表"**都从表里现找**。
                #   写死 `"chart"` 的代价实测过：config 一换到莫干山（没有 chart
                #   这个页型），这条就会红成"不认识的页型"，而真正的原因是
                #   「拿 A 模板的页号去 B 模板的稿上填」—— 本项目最难查的那类错。
                _ts = D.load_template(_tname)
                _tp = D.template_pages(_ts)
                _cid = next((k for k, v in _tp.items()
                             if (v.get("assets") or {}).get("charts")), "")
                if not _cid:
                    skip("自带图表页的警告",
                         f"槽位表 `{_tname}` 里没有带自带图表的页型")
                    _rr = None
                else:
                    _info = _tp[_cid]
                    _sl = {str(x["i"]): ("示例" if not x.get("cap_chars")
                                         else "示例"[:x["cap_chars"]])
                           for x in _info["slots"]
                           if x.get("role") not in ("anchor", "fixed", "empty")}
                    _rr = D.deck_from_template(
                        str(work / "warn.pptx"), [{"page": _cid, "slots": _sl}],
                        template_pptx=_tpl_path,
                        # ⚠️ 这里必须传**真**槽位表 —— 合成夹具里没有这个页型
                        #    （第一版传错了，4 条断言全红：`不认识的页型 chart`）
                        template_slots=_ts, template_name=_tname,
                        workdir=str(work), python_exe=py)
                if _rr is not None:
                    ck(_rr.ok, f"正控：能出一页（`{_cid}` 图表页本身是可填的）",
                       "\n".join(_rr.problems[:3]))
                    ck(bool(_rr.warnings) and any("图表" in x for x in _rr.warnings),
                       "★ 产出带了「自带示例图表」的警告（不阻断，但说出口）")
                    ck("假数字" in " ".join(_rr.warnings),
                       "★ 警告里点明了后果是**假数字**（不是含糊的『请注意』）")
                    # ★ 这一条原先被误放进 `except` 分支 ⇒ **正常路径上从不执行**
                    #   （"断言长错分支" = 一条永远不跑的判据）。挪回来。
                    ck(not _rr.problems,
                       "它是**警告**不是**问题**（能交付，只是要知情）",
                       "\n".join(_rr.problems[:3]))

                    # ★★ 页号必须是**产物里的位次**，不是「第几条警告」：
                    #    旧实现用 `len(res.warnings)+1` 当页号，12 页稿里唯一的
                    #    那条警告印成「第 1 页」，而那一页实际排第 11 位。
                    #    ⇒ 要**凑两页**、把图表页放在第 2 位，两个数才分得开。
                    _other = next((k for k in _tp if k != _cid
                                   and _tp[k].get("src") != _info.get("src")), "")
                    if not _other:
                        skip("警告里的页号是产物位次", "表里找不到第二个页型可搭")
                    else:
                        _sl2 = {str(x["i"]): ("示例" if not x.get("cap_chars")
                                              else "示例"[:x["cap_chars"]])
                                for x in _tp[_other]["slots"]
                                if x.get("role") not in ("anchor", "fixed", "empty")}
                        _rr2 = D.deck_from_template(
                            str(work / "warn2.pptx"),
                            [{"page": _other, "slots": _sl2},
                             {"page": _cid, "slots": _sl}],
                            template_pptx=_tpl_path, template_slots=_ts,
                            template_name=_tname, workdir=str(work), python_exe=py)
                        if not _rr2.ok:
                            skip("警告里的页号是产物位次",
                                 "两页正控没起来：" + "；".join(_rr2.problems[:2]))
                        else:
                            _w = " ".join(_rr2.warnings)
                            ck("第 2 页" in _w and "第 1 页" not in _w,
                               "★ 警告里的页号是**产物位次**"
                               "（图表页排第 2 ⇒ 印「第 2 页」）", _w[:160])
            except Exception as e:
                ck(False, "自带图表页的警告", f"{type(e).__name__}: {e}")
    finally:
        # 清场一律「挪」不「删」（本机删除按条目计费，见 _tkit）
        stash(work, "template-test")

    print(f"\n{PASS} 项通过 / {FAIL} 项失败" + (f" / {SKIP} 项跳过" if SKIP else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
