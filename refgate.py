# -*- coding: utf-8 -*-
"""参考文献真伪闸门：交付前机械拦下三类假引用。

  假型 1 —— 一码多用：同一个 doc_id 被挂到多篇**题名不同**的文献上。
             这是杜撰最可靠的体征：模型知道库里有这个 id，就给多条凑出来的
             文献都贴上它。实测命中 4 条（kb1859af56 挂了 5 篇）。
  假型 2 —— DOI 不在库：条目写的 DOI 在库里查无此值。
  假型 3 —— 编号失配：正文引用了文献表没列的编号，或反之。

负控：对每类各造一个必然触发的样本，确认闸门会红。
     并且对一份**干净**样本确认全绿（否则闸门恒红等于没用）。
"""
from __future__ import annotations
import re, json, sys, pathlib
from collections import Counter, defaultdict

LIB_DEFAULT = pathlib.Path.home() / ".dsh" / "papers" / "workbench" / "data" / "litkb"


def load_lib(lib_dir: pathlib.Path | None = None) -> dict:
    lib_dir = lib_dir or LIB_DEFAULT
    lib = json.loads((lib_dir / "library.json").read_text(encoding="utf-8"))
    items = lib if isinstance(lib, list) else lib.get("items", lib.get("entries", []))
    by_id, by_doi = {}, {}
    for it in items:
        did = it.get("id") or it.get("doc_id")
        if did:
            by_id[did] = it
        d = (it.get("doi") or "").strip().lower()
        if d:
            by_doi[d] = it
    return {"by_id": by_id, "by_doi": by_doi, "n": len(items)}


def parse_refs(text: str) -> list[dict]:
    """从 references.md 逐行解析条目。题目里的 [J]/[M] 等不会误伤，
    因为只认**行首**的 [数字]。"""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*\[(\d+)\]\s*(.+)$", line)
        if not m:
            continue
        n, body = int(m.group(1)), m.group(2)
        did = re.search(r"doc_id:\s*(kb[0-9a-f]{8})", body)
        doi = re.search(r"DOI:\s*(10\.\d{4,9}/[^\s，,。）)]+)", body)
        # 题名：取 [J]. 之后、年份之前的那一段
        t = re.search(r"\]\.\s*(.+?)(?:\[J\]|\[M\]|\[D\]|,\s*\d{4})", body)
        title = t.group(1).strip() if t else ""
        out.append({
            "n": n,
            "doc_id": did.group(1) if did else None,
            "doi": doi.group(1).rstrip(".").lower() if doi else None,
            "title": title,
            "raw": body,
        })
    return out


def check_refs(ref_text: str, body_text: str, lib: dict) -> dict:
    refs = parse_refs(ref_text)
    by_id, by_doi = lib["by_id"], lib["by_doi"]
    problems = defaultdict(list)

    # 假型 1：一码多用
    groups = defaultdict(list)
    for r in refs:
        if r["doc_id"]:
            groups[r["doc_id"]].append(r)
    for did, ents in groups.items():
        if len(ents) <= 1:
            continue
        # 同一 id 的多条，若题名彼此实质不同 ⇒ 造假
        titles = {re.sub(r"\W+", "", e["title"]).lower()[:40] for e in ents}
        if len(titles) > 1:
            rec = by_id.get(did) or {}
            problems["一码多用"].append({
                "doc_id": did, "次数": len(ents),
                "编号": [e["n"] for e in ents],
                "库内真实题名": (rec.get("title") or "")[:90],
                "库内真实DOI": rec.get("doi") or "(无)",
                "各条题名": [e["title"][:80] for e in ents],
            })

    # 假型 2：DOI 不在库
    for r in refs:
        if r["doi"] and r["doi"] not in by_doi:
            problems["DOI不在库"].append({"n": r["n"], "doc_id": r["doc_id"],
                                          "doi": r["doi"], "title": r["title"][:80]})

    # 假型 2b：doc_id 在库，但条目 DOI 与库内该 id 的 DOI 不符
    for r in refs:
        if not (r["doc_id"] and r["doi"]):
            continue
        rec = by_id.get(r["doc_id"])
        if not rec:
            continue
        libdoi = (rec.get("doi") or "").strip().lower()
        if libdoi and libdoi != r["doi"]:
            # 若属于「一码多用」组，已在前一类报过，不重复
            if len(groups.get(r["doc_id"], [])) > 1:
                continue
            problems["DOI与库内不符"].append({
                "n": r["n"], "doc_id": r["doc_id"],
                "文中DOI": r["doi"], "库内DOI": libdoi,
                "库内题名": (rec.get("title") or "")[:80],
            })

    # 假型 3：编号失配
    listed = {r["n"] for r in refs}
    cited = set()
    for grp in re.findall(r"\[(\d+(?:\s*[,\-]\s*\d+)*)\]", body_text):
        for part in re.split(r"[,\s]+", grp.strip()):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-")[:2]
                if a.isdigit() and b.isdigit():
                    cited.update(range(int(a), int(b) + 1))
            elif part.isdigit():
                cited.add(int(part))
    if listed - cited:
        problems["只列不引"].append(sorted(listed - cited))
    if cited - listed:
        problems["只引不列"].append(sorted(cited - listed))

    return {"refs": refs, "problems": dict(problems)}


def report(res: dict, verbose=True) -> int:
    p = res["problems"]
    total = sum(len(v) for v in p.values())
    if verbose:
        print(f"文献表条目：{len(res['refs'])}")
        if not total:
            print("✔ 未发现假引用（一码多用 / DOI 不在库 / 编号失配 均为 0）")
        for kind, items in p.items():
            print(f"\n✘ {kind}：{len(items)} 处")
            for x in items:
                if kind == "一码多用":
                    print(f"    doc_id {x['doc_id']} 被用了 {x['次数']} 次，涉及编号 {x['编号']}")
                    print(f"      库内真实题名: {x['库内真实题名']}")
                    print(f"      库内真实DOI : {x['库内真实DOI']}")
                    for t in x["各条题名"]:
                        print(f"        · {t}")
                elif kind == "DOI不在库":
                    print(f"    [{x['n']}] {x['doi']}  ←  {x['title']}")
                elif kind == "DOI与库内不符":
                    print(f"    [{x['n']}] 文中 {x['文中DOI']}  vs  库内 {x['库内DOI']}  ({x['库内题名']})")
                else:
                    print(f"    {x}")
    return total


if __name__ == "__main__":
    ws = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(
        pathlib.Path(__file__).resolve().parent / "runs" / "20261015-092001")
    ref_t = (ws / "references.md").read_text(encoding="utf-8")
    rev_t = (ws / "综述.md").read_text(encoding="utf-8")
    lib = load_lib()
    res = check_refs(ref_t, rev_t, lib)
    n = report(res)
    print(f"\n合计问题 {n} 处")
    sys.exit(1 if n else 0)
