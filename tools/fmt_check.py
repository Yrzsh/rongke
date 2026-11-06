# -*- coding: utf-8 -*-
"""中文学术格式验收：对着「通用格式规范」逐条判 docx 达标与否。

用法： python fmt_check.py 综述.docx

判据全部取自**产出物本身**（原始 OOXML），不采信渲染观感 ——
渲染会把"字体回退""内容合并"这类问题藏起来。
"""
import sys, re, zipfile, pathlib
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results = []


def chk(cond, name, detail, level=FAIL):
    results.append((PASS if cond else level, name, detail))


def main():
    if len(sys.argv) < 2:
        print("用法: python fmt_check.py <docx>")
        return 2
    p = pathlib.Path(sys.argv[1])
    if not p.exists():
        print(f"✗ 找不到 {p}")
        return 2
    z = zipfile.ZipFile(p)
    d = z.read("word/document.xml").decode("utf-8")
    st = z.read("word/styles.xml").decode("utf-8")

    def sdef(sid):
        m = re.search(rf'<w:style [^>]*w:styleId="{sid}".*?</w:style>', st, re.S)
        return m.group(0) if m else ""

    # ── 1 页面 ──
    a4 = 'w:w="11906"' in d and 'w:h="16838"' in d
    chk(a4, "纸张 A4", "11906×16838 twips" if a4 else "非 A4（西文模板常为 Letter）")

    m = re.search(r'<w:pgMar[^>]*w:left="(\d+)"[^>]*w:right="(\d+)"', d)
    if not m:
        m2 = re.search(r'<w:pgMar[^>]*/>', d)
        lr = re.findall(r'w:(?:left|right)="(\d+)"', m2.group(0)) if m2 else []
    else:
        lr = [m.group(1), m.group(2)]
    chk(bool(lr), "页边距已设置", f"左右 {lr}" if lr else "无 pgMar")

    # ── 2 字体 ──
    h1 = sdef("Heading1")
    hf = re.findall(r'w:eastAsia="([^"]+)"', h1)
    chk(hf and hf[0] != "Times New Roman",
        "标题中文字体非西文字体",
        f"Heading1 eastAsia={hf[0] if hf else '未设'}")
    chk(hf and hf[0] == "黑体", "标题用黑体", f"实际 {hf[0] if hf else '未设'}", WARN)

    bt = sdef("BodyText")
    bf = re.findall(r'w:eastAsia="([^"]+)"', bt)
    chk(bf and bf[0] == "宋体", "正文用宋体", f"BodyText eastAsia={bf[0] if bf else '未设'}")
    theme = z.read("word/theme/theme1.xml").decode("utf-8")
    mm = re.search(r'<a:font script="Hans" typeface="([^"]*)"', theme)
    hans = mm.group(1) if mm else ""
    chk(hans == "宋体", "Hans 回退字体为宋体（非云字体等线）", f"Hans={hans!r}")

    # ── 3 段落 ──
    fl = re.findall(r'w:firstLineChars="(\d+)"', bt) or re.findall(r'w:firstLine="(\d+)"', bt)
    chk(bool(fl), "正文首行缩进 2 字符", f"firstLineChars={fl}" if fl else "无首行缩进")
    ln = re.findall(r'w:line="(\d+)"', bt)
    chk(bool(ln) and int(ln[0]) >= 300, "正文行距 ≥1.25 倍",
        f"w:line={ln[0]}（240=单倍, 360=1.5倍）" if ln else "未设行距")
    jc = re.findall(r'<w:jc w:val="([^"]+)"', bt)
    chk("both" in jc or "justify" in jc or "left" in jc, "正文对齐已设", f"jc={jc}")

    # ── 4 标题层级递进 ──
    sizes = {}
    for lvl in range(1, 5):
        s = sdef(f"Heading{lvl}")
        sz = re.findall(r'<w:sz w:val="(\d+)"', s)
        if sz:
            sizes[lvl] = int(sz[0])
    ok_desc = all(sizes.get(i, 0) > sizes.get(i + 1, 9999)
                  for i in range(1, len(sizes)))
    chk(len(sizes) >= 2 and ok_desc, "标题字号逐级递进",
        " ".join(f"H{k}={v/2:.0f}pt" for k, v in sorted(sizes.items())))

    # ── 5 表格 ──
    n_tbl = d.count("<w:tbl>")
    chk(n_tbl > 0, "含表格", f"{n_tbl} 个")
    if n_tbl:
        chk(d.count("<w:tblBorders>") >= n_tbl, "表格有边框",
            f"tblBorders {d.count('<w:tblBorders>')} / 表 {n_tbl}")
        # 单元格不能是两端对齐（会把中文撑成「出 发 菌 株」）
        bad_jc = 0
        for t in re.findall(r"<w:tbl>.*?</w:tbl>", d, re.S):
            for c in re.findall(r"<w:tc>.*?</w:tc>", t, re.S):
                m = re.search(r"<w:jc w:val=\"([^\"]+)\"", c)
                if m and m.group(1) in ("both", "justify"):
                    bad_jc += 1
        chk(bad_jc == 0, "单元格无两端对齐（防字间被撑开）", f"两端对齐单元格 {bad_jc} 个")
        # 列宽和 ≈ 版心
        for i, t in enumerate(re.findall(r"<w:tbl>.*?</w:tbl>", d, re.S), 1):
            cw = [int(x) for x in re.findall(r'<w:gridCol w:w="(\d+)"', t)]
            if cw:
                chk(sum(cw) > 7000, f"表{i} 列宽铺满版心",
                    f"合计 {sum(cw)} twips", WARN)
        # 表头加粗
        chk(d.count("<w:b/>") > 0, "表头有加粗", f"w:b 共 {d.count('<w:b/>')} 处")

    # ── 6 图 ──
    n_img = d.count("<w:drawing>")
    if n_img:
        chk("CaptionedFigure" in st or "ImageCaption" in st,
            "图/图注样式有定义（防样式悬空）",
            "CaptionedFigure" if "CaptionedFigure" in st else "ImageCaption")
        # 图注应居中
        for sid in ("ImageCaption", "Caption"):
            s = sdef(sid)
            if s:
                cj = re.findall(r'<w:jc w:val="([^"]+)"', s)
                chk("center" in cj, f"{sid} 居中", f"jc={cj}")
                break

    # ── 7 参考文献悬挂缩进 ──
    paras = re.findall(r"<w:p>.*?</w:p>", d, re.S)
    refs = [q for q in paras
            if re.match(r"^\s*\[\d+\]\s*\S",
                        "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", q)))]
    if refs:
        hg = sum(1 for q in refs if "w:hanging=" in q)
        chk(hg == len(refs), "文献条目全部悬挂缩进",
            f"{hg}/{len(refs)} 条有悬挂缩进")
        chk(len(refs) > 1, "文献表条目已分段（非合并成一段）",
            f"识别到 {len(refs)} 条独立段落")

    # ── 8 引用上标 ──
    n_sup = d.count("w:vertAlign")
    if refs:
        ref_sup = sum(1 for q in refs if "w:vertAlign" in q)
        chk(ref_sup == 0, "文献序号未被误加上标", f"文献区上标 {ref_sup} 处")
    chk(n_sup > 0, "正文引用有上标", f"w:vertAlign {n_sup} 处", WARN)

    # ── 输出 ──
    print(f"=== 中文学术格式验收：{p.name} ===")
    print()
    nf = 0
    for lvl, name, detail in results:
        mark = {"PASS": "✔", "FAIL": "✗", "WARN": "!"}[lvl]
        print(f"  [{lvl:4s}] {mark} {name:34s} {detail}")
        if lvl == FAIL:
            nf += 1
    np_ = sum(1 for r in results if r[0] == PASS)
    nw = sum(1 for r in results if r[0] == WARN)
    print()
    print(f"  {np_} 通过 / {nf} 失败 / {nw} 待改进")
    return 0 if nf == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
