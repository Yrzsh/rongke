# -*- coding: utf-8 -*-
"""`rongke/kb_convert.py` 单测：PDF → Markdown 转换层。

## 分层（照本仓 `kb_wire_test` / `kb_e2e_test` 的分工纪律）

| 层 | 需要什么 | 本文件 |
|---|---|---|
| **纯函数层** | 只要标准库 | ✅ A~D 段，秒级，**任何机器都能跑** |
| **真转换层** | 一份真 PDF + 装了 pymupdf4llm 的解释器 | ✅ E 段，**拿不到就 SKIP 并如实说** |

★ **不许把 E 段的缺失写成"通过"** —— 本仓的 I 型缺陷就是"审不了被当成通过了"。
拿不到真 PDF 时，E 段逐条打 `[SKIP]`，并在末尾**如实报出跳过了几条**，
退出码取 2（区别于"全过"的 0 与"有失败"的 1）。

## 每条新增判据都配正控 + 负控（本仓定律 19）

尤其是 `safe_note_name` 与 `yaml_frontmatter` —— 它们守的是"脏输入不会
静默写坏文件"，所以**必须证明它们在脏输入上真的会动**，而不是恒为通过。

跑：python312 kb_convert_test.py     退出码 0 全过 / 1 有失败 / 2 有跳过
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke.kb_convert import (Conversion, PdfConverter, convert_pdf,  # noqa: E402
                               ocr_advice, safe_note_name, to_obsidian,
                               yaml_frontmatter)

ok_n = bad_n = skip_n = 0


def check(label, got, want):
    global ok_n, bad_n
    good = got == want
    ok_n += good
    bad_n += (not good)
    # 标记必须顶格 `[PASS]`/`[FAIL]` —— regress.py 按行首标记计数。
    print(("  [PASS] " if good else "  [FAIL] ") + label)
    if not good:
        print(f"         实测={got!r}\n         期望={want!r}")


def truthy(label, got):
    check(label, bool(got), True)


def skip(label, why):
    global skip_n
    skip_n += 1
    print(f"  [SKIP] {label} —— {why}")


print("=" * 78)
print("A · 纯函数：文件名消毒（正控 + 负控）")
print("=" * 78)

check("普通标题原样保留", safe_note_name("Antigen identification"), "Antigen identification")
check("★ 正控：Windows 禁用字符被换掉", safe_note_name('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
check("★ 正控：控制字符被换掉", safe_note_name("a\x01b"), "a_b")
check("★ 正控：空标题兜底（不许产出空文件名）", safe_note_name(""), "untitled")
check("★ 正控：全禁用字符也兜底", safe_note_name("???"), "___")
check("★ 正控：纯空白给 untitled", safe_note_name("   "), "untitled")
check("★ 负控：末尾的点被去掉（Windows 不允许）", safe_note_name("abc."), "abc")
check("负控：正常标题**不该**被改坏", safe_note_name("TB vaccine 2024"), "TB vaccine 2024")
truthy("超长标题被截断到 120", len(safe_note_name("x" * 500)) <= 120)

print()
print("=" * 78)
print("B · 纯函数：YAML frontmatter（正控 + 负控）")
print("=" * 78)

fm = yaml_frontmatter({"title": "A study", "year": 2024})
truthy("有字段 ⇒ 产出 frontmatter", fm.startswith("---\n"))
truthy("有字段 ⇒ 以 --- 收口", "\n---\n" in fm)
check("★ 负控：全空字段 ⇒ **不产出** frontmatter（不写空壳）",
      yaml_frontmatter({}), "")
check("★ 负控：空字符串不算有值", yaml_frontmatter({"doi": ""}), "")
check("★ 负控：空列表不算有值", yaml_frontmatter({"tags": []}), "")
truthy("doi 原样进 frontmatter", "10.1038/x" in yaml_frontmatter({"doi": "10.1038/x"}))
truthy("★ 正控：标题里的冒号触发加引号",
       '"' in yaml_frontmatter({"title": "TB: a review"}))
truthy("★ 正控：标题里的 # 触发加引号",
       '"' in yaml_frontmatter({"title": "TB #1"}))
check("负控：干净标题**不**乱加引号",
      yaml_frontmatter({"title": "TB review"}), "---\ntitle: TB review\n---\n\n")
truthy("列表字段按 YAML 序列写", "- a\n  - b" in yaml_frontmatter({"tags": ["a", "b"]}))
truthy("年份数字能序列化", "year: 2024" in yaml_frontmatter({"year": 2024}))
# ★ 承重自检：把 _yaml_scalar 换成恒等（永不加引号），上面那两条正控必须失守。
import rongke.kb_convert as kc  # noqa: E402
_orig = kc._yaml_scalar
kc._yaml_scalar = lambda s: s          # 换成"永不加引号"
_broken = kc.yaml_frontmatter({"title": "TB: a review"})
kc._yaml_scalar = _orig
check("★ 承重自检：把加引号逻辑拆掉后，正控当场失守",
      '"' in _broken, False)
check("★ 承重自检后恢复（同一输入回到加引号）",
      '"' in kc.yaml_frontmatter({"title": "TB: a review"}), True)

print()
print("=" * 78)
print("C · 纯函数：OCR 建议（阈值两侧都要量）")
print("=" * 78)

def _conv(chars, pages, verdict="OK"):
    c = Conversion(verdict=verdict, chars=chars, pages=pages)
    return c

check("正常论文密度（7300/页）⇒ **不提示**",
      ocr_advice(_conv(153402, 21)), "")
check("★ 正控：扫描件密度（50/页）⇒ 提示开 OCR",
      bool(ocr_advice(_conv(50 * 5, 5))), True)
truthy("提示里写了「不会自动替你重跑」",
       "不会自动替你重跑" in ocr_advice(_conv(100, 5)))
# ★ 负控：页面数为 0 ⇒ **判不了**，不许提示。
# 这一条是**真抓到的缺陷**：第一版只判 chars_per_page < 阈值，
# 而 pages=0 时它恒为 0.0 ⇒ 会理直气壮地说"每页只有 0 字符，很可能是扫描件"。
check("★ 负控：页面为 0（判不了）⇒ 不提示、不误报", ocr_advice(_conv(0, 0)), "")
check("★ 负控：页面为 0 但字符很多 ⇒ 同样不提示",
      ocr_advice(_conv(50000, 0)), "")
check("★ 负控：转换失败时不提示（没内容可判）",
      ocr_advice(_conv(0, 0, verdict="FAILED")), "")

print()
print("=" * 78)
print("D · 纯函数：写 Obsidian（用临时 vault，真写盘 + 回读）")
print("=" * 78)

with tempfile.TemporaryDirectory() as td:
    vault = Path(td) / "vault"
    # ★ 正控：vault 不存在 ⇒ 必须**报错**，不许静默新建一个假库
    r = to_obsidian("# hi", {"title": "T"}, vault)
    check("★ 正控：vault 不存在 ⇒ ok=False（不静默造库）", r.get("ok"), False)
    truthy("★ 正控：给出可操作提示", bool(r.get("hint")))

    vault.mkdir()
    r = to_obsidian("# hello\n\nbody", {"title": "My Note", "doi": "10.1/x"}, vault)
    check("vault 存在 ⇒ 写入成功", r.get("ok"), True)
    p = Path(r["path"])
    truthy("★ 文件真的在盘上（不看自述看盘）", p.exists())
    back = p.read_text(encoding="utf-8")
    truthy("★ 内容真的一致（写后回读核过）", "# hello" in back)
    truthy("frontmatter 落进去了", back.startswith("---\n"))
    truthy("doi 落进去了", "10.1/x" in back)
    check("文件名用了 title", p.name, "My Note.md")

    # 幂等：同样内容再写一次 ⇒ **不重写**，也不新建第二份
    r2 = to_obsidian("# hello\n\nbody", {"title": "My Note", "doi": "10.1/x"}, vault)
    check("★ 同内容重写 ⇒ 不重写（same_file=True）", r2.get("same_file"), True)
    check("★ 同内容重写 ⇒ 没有产生第二份文件",
          len(list(vault.glob("*.md"))), 1)

    # ★ 关键负控：同名不同内容 ⇒ **绝不覆盖**
    r3 = to_obsidian("# DIFFERENT", {"title": "My Note"}, vault)
    check("★ 负控：同名不同内容 ⇒ 存成新名字", r3.get("replaced_new_name"), True)
    check("★ 负控：原来的那份**一个字没动**",
          "# hello" in p.read_text(encoding="utf-8"), True)
    check("★ 负控：现在盘上确实有两份", len(list(vault.glob("*.md"))), 2)
    truthy("★ 语义清晰：说了「没有覆盖」", "没有覆盖" in str(r3.get("note")))

    # folder 子目录
    r4 = to_obsidian("# in folder", {"title": "Filed"}, vault, folder="papers")
    check("folder 参数 ⇒ 落进子目录", Path(r4["path"]).parent.name, "papers")

print()
print("=" * 78)
print("E · 真转换（需要真 PDF + pymupdf4llm；拿不到就 SKIP，不算通过）")
print("=" * 78)

cv = PdfConverter()
print(f"  解释器 = {cv.python_exe}")
print(f"  notes  = {cv.notes}")

REAL_PDFS = [
    Path.home() / ".dsh" / "papers" / "Recent-developments-in-systems-biology-a" / "data" / "lit_pdf" / "npj2024.pdf",
]
real = next((p for p in REAL_PDFS if p.exists()), None)

if not cv.available():
    skip("真转换全部", "找不到装了 pymupdf4llm 的解释器")
elif real is None:
    skip("真转换全部", "找不到一份真 PDF 做夹具")
else:
    print(f"  夹具 = {real.name} ({real.stat().st_size // 1024} KB)")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "sub" / "deep" / "paper.md"   # 顺带验"目录不存在要先建"
        c = cv.convert(real, out=out)
        check("verdict", c.verdict, "OK")
        truthy("pages > 0", c.pages > 0)
        truthy("chars 是真实量级（> 10000）", c.chars > 10000)
        check("★ 产物真的落到了盘上（含自动建目录）", out.exists(), True)
        truthy("★ 盘上文件大小与回报一致（自称成功不算证据）",
               abs(out.stat().st_size - c.chars) < 5000)
        truthy("★ 结构被识别出来了：标题 > 0", c.headings > 0)
        truthy("★ 表格被转出来了：table_pipes > 0", c.table_pipes > 0)
        check("★ 快路：默认没开 OCR", c.used_ocr, False)
        check("★ 快路：默认没写图", c.wrote_images, False)
        check("★ 快路：读的是文字层，不是 OCR", bool(c.notes), False)

        # 负控：坏输入必须**失败且说清原因**，不许静默给个空产物
        c2 = cv.convert(Path(td) / "nope.pdf")
        check("★ 负控：不存在的文件 ⇒ FAILED", c2.verdict, "FAILED")
        truthy("★ 负控：说明了是「不存在」", "不存在" in c2.error)

        notpdf = Path(td) / "a.txt"
        notpdf.write_text("hello", encoding="utf-8")
        c3 = cv.convert(notpdf)
        check("★ 负控：非 PDF ⇒ FAILED（不硬试）", c3.verdict, "FAILED")

        # ★ 正文回传契约（kb_to_obsidian 靠它不落临时文件）
        ctext = cv.convert(real, need_text=True)
        check("need_text=True ⇒ verdict OK", ctext.verdict, "OK")
        truthy("★ need_text=True ⇒ text 真的带回了正文", len(ctext.text) > 10000)
        truthy("★ text 里真有正文内容", "Antigen" in ctext.text or "#" in ctext.text)
        cnotext = cv.convert(real)
        check("★ 默认不带正文（省管道/省内存）", cnotext.text, "")
        check("★ 但统计量照样有（chars 不因不回正文而丢）",
              cnotext.chars > 10000, True)
        # ★ 承重自检：`to_dict()` **不许**夹带正文 ——
        #   回包是给模型读的紧凑载荷，塞进 15 万字符会一次撑爆上下文。
        d = ctext.to_dict()
        check("★ 承重自检：to_dict 里**没有** text 字段（回包不许夹带正文）",
              "text" in d, False)

        # convert_pdf 便利入口
        d = convert_pdf(str(real), out=str(Path(td) / "p2.md"))
        check("convert_pdf 走通", d.get("verdict"), "OK")
        truthy("convert_pdf 带 elapsed", "elapsed" in d)
        truthy("convert_pdf 带 chars_per_page", (d.get("chars_per_page") or 0) > 0)

        # convert_many：一份坏的不该拖垮整批
        many = cv.convert_many([real, Path(td) / "missing.pdf"], Path(td) / "many")
        check("convert_many 回两份结果", len(many), 2)
        check("convert_many 第一份成功", many[0].verdict, "OK")
        check("★ 第二份失败但不影响第一份（逐份独立）", many[1].verdict, "FAILED")

print()
print("=" * 78)
print("F · 契约回归：子进程 ↔ 父进程的正文回传（血债，必须钉住）")
print("=" * 78)
# ★ 血债的形状：`loop.py` 的 `kb_to_obsidian` 调 convert 时**忘了传 need_text=True**，
#   于是 `conv.text` 是空串。如果此处没有守卫，它会拿着空正文去写 vault
#   ⇒ 产出一份**看起来完全成功、实际是空壳**的笔记（本仓 I 型缺陷）。
#   那次是**内核自己的守卫拦下的**（"转换报告成功，但正文没有回传"），
#   这里把该守卫钉成判据，防止它被后来的人"顺手简化"掉。
if not cv.available() or real is None:
    skip("正文回传守卫", "需要真 PDF + pymupdf4llm")
else:
    import inspect                                              # noqa: E402
    from rongke import loop as _loop                            # noqa: E402
    src_loop = inspect.getsource(_loop._make_kb_convert_tools)
    truthy("★ 承重：kb_to_obsidian 确实传了 need_text=True",
           "need_text=True" in src_loop)
    truthy("★ 承重：确实有「正文没有回传」的守卫",
           "正文没有回传" in src_loop)
    truthy("★ 承重：守卫在写 vault **之前**（否则空壳已经写出去了）",
           src_loop.index("正文没有回传") < src_loop.index("to_obsidian(md"))
    truthy("★ 承重：没有临时文件那套（不许再引入落盘再删）",
           "_kb_tmp_" not in src_loop)

print()
print("=" * 78)
total = ok_n + bad_n
print(f"结果：{ok_n} 项通过 / {bad_n} 项失败 / {skip_n} 项跳过（共 {total} 项断言）")
print("=" * 78)
if bad_n:
    print("[FAIL] 有失败")
    sys.exit(1)
if skip_n:
    print("[SKIP] 有跳过 —— **跳过不算通过**，别把这次读成全绿")
    sys.exit(2)
print("[DONE] kb_convert 全部通过（含真转换）")