# -*- coding: utf-8 -*-
"""引文核验 —— 存在性 / 撤稿 / 元数据一致性。

为什么内核自带这个，而不是只靠领域工具：

  * 领域工具 `fetch_doi` 只**取元数据**，不判真伪、更不查撤稿。它能告诉你
    "这篇论文的作者是 X"，但不会告诉你"这篇论文已经被撤了"。
  * `lit-review` 技能里写着 `retract`（撤稿查詢）指令 —— 如果工具面没有对应
    能力，技能就是在承诺一件模型做不到的事。**技能承诺的能力，工具必须兑现。**

撤稿数据是免费的：Crossref 的 `updated-by` / `update-to` 字段里带
`source: retraction-watch` 的记录。本机实测（2026-09-18）：

    10.1016/S0140-6736(97)11096-0  → 标题前缀 "RETRACTED:"，
        updated-by 含 {"type": "retraction", "label": "Retraction"}
    10.1038/nature12373            → 无 update 字段（对照，干净）

所以成本 = 一次 API 调用，和 fetch_doi 同一份数据源。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher

CROSSREF = "https://api.crossref.org/works/"
# ⚠️ 非通用 UA：Crossref 对匿名/默认 UA 会给更差的限流待遇，
#    并与 llm.py 保持一致的自我标识策略。
_UA = {"User-Agent": "Rongke/0.0.1 (research agent; mailto:rongke@localhost)"}

_RETRACT_TYPES = {"retraction", "retract", "retraction-of"}

# 中日韩统一表意文字（含扩展 A 与兼容区）
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK.search(s or ""))


def _norm(s: str) -> str:
    """Unicode 感知的规范化：保留字母（含中日韩）与数字，去掉标点空白。

    ⚠️ 这里**不能**写 `[^a-z0-9]` —— 那会把中文标题整个抹成空串，
    于是 `if a and b` 判断直接短路，中文元数据比对**静默失效**却毫无报错。
    Python 3 的 `\\W` 对 str 是 Unicode 感知的，正好保留汉字。
    （本机自测抓到的真实缺陷，见 writing_gate_test.py）
    """
    return re.sub(r"[\W_]+", "", (s or "").lower())


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _title_verdict(expect: str, actual: str) -> tuple[bool, float, str]:
    """返回 (是否算相符, 相似度, 说明)。拿不到值时不算人错。"""
    a, b = _norm(expect), _norm(actual)
    if not a or not b:
        return True, 1.0, ""                      # 无从判断，不报错
    if a in b or b in a:
        return True, 1.0, ""
    if _has_cjk(a) != _has_cjk(b):
        # 中文题 vs 英文题：同一篇文献的两种写法，字符串比对必然失败。
        # 与其误报"标题不符"，不如如实说明无法自动比对。
        return True, 0.0, "标题中英形式不同，未自动比对"
    r = _ratio(a, b)
    return r >= 0.7, r, ""


def _fetch(doi: str, timeout: int = 30) -> dict | None:
    """取 Crossref 记录。DOI 不存在返回 None（与网络故障区分开）。"""
    url = CROSSREF + urllib.parse.quote(doi.strip())
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace")).get("message") or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        raise


def _title_of(m: dict) -> str:
    t = m.get("title") or []
    return (t[0] if t else "").strip()


def check_doi(
    doi: str,
    expect_title: str = "",
    expect_year: str = "",
    expect_authors: str = "",
) -> dict:
    """核验一个 DOI。返回结构化结论，不抛异常（网络故障在 error 字段里如实报告）。"""
    out: dict = {
        "doi": doi.strip(),
        "exists": False,
        "retracted": False,
        "metadata_mismatch": [],
        "verdict": "unknown",
        "error": "",
    }
    try:
        m = _fetch(doi)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["verdict"] = "查不到（网络或接口故障，不代表 DOI 无效）"
        return out

    if m is None:
        out["verdict"] = "DOI 在 Crossref 中不存在 —— 高度可疑（可能是编造的引用）"
        return out

    out["exists"] = True
    title = _title_of(m)
    out["title"] = title
    out["type"] = m.get("type")
    out["journal"] = (m.get("container-title") or [""])[0]
    out["publisher"] = m.get("publisher")
    yr = (m.get("issued", {}).get("date-parts") or [[None]])[0][0]
    out["year"] = yr
    auths = m.get("author") or []
    out["authors"] = [
        " ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
        for a in auths[:12]
    ]

    # ── 撤稿 ────────────────────────────────────────────────
    ups = list(m.get("updated-by") or []) + list(m.get("update-to") or [])
    retractions = [
        {
            "type": u.get("type"),
            "label": u.get("label"),
            "source": u.get("source"),
            "date": ((u.get("updated") or {}).get("date-parts") or [[None]])[0],
            "doi": u.get("DOI"),
        }
        for u in ups
        if str(u.get("type") or "").lower() in _RETRACT_TYPES
    ]
    if retractions:
        out["retracted"] = True
        out["retraction"] = retractions[0]
        out["retractions"] = retractions
    elif title.lower().startswith(("retracted", "retraction")):
        # 少量撤稿只在标题上留痕（期刊改了标题但没登记 update 关系）
        out["retracted"] = True
        out["retraction"] = {"type": "retraction", "source": "title-prefix",
                             "label": "标题带 RETRACTED 前缀"}
    # 其他更新（更正/新版本）也报出来，供人工判断
    others = [u for u in ups if u not in [] and str(u.get("type") or "").lower() not in _RETRACT_TYPES]
    if others:
        out["other_updates"] = [
            {"type": u.get("type"), "label": u.get("label")} for u in others[:5]
        ]

    # ── 元数据一致性 ────────────────────────────────────────
    mm: list[str] = []
    notes: list[str] = []
    if expect_title:
        same, ratio, why = _title_verdict(expect_title, title)
        if why:
            notes.append(why)
        elif not same:
            mm.append(
                f"标题不符（相似度 {ratio:.0%}）：引用写的是「{expect_title[:70]}」，"
                f"Crossref 是「{title[:70]}」"
            )
    if expect_year and yr:
        if str(expect_year).strip()[:4] != str(yr)[:4]:
            mm.append(f"年份不符：引用写 {expect_year}，Crossref 是 {yr}")
    if expect_authors:
        names = [n for n in re.split(r"[,;、，；]| and ", expect_authors) if _norm(n)]
        blob = _norm(" ".join(out["authors"]))
        if not names or not blob:
            pass
        elif _has_cjk(" ".join(names)) != _has_cjk(blob):
            # 中文名 vs 罗马化名（Zhang San / 张三）—— 字符串比对必然失败，
            # 硬比会误报。如实说明，交给人判断。
            notes.append("作者中英姓名形式不同，未自动比对")
        else:
            hits = sum(1 for n in names if _norm(n) in blob)
            if hits / len(names) < 0.5:
                mm.append(
                    f"作者不符：引用写「{expect_authors[:60]}」，"
                    f"Crossref 前几位是「{', '.join(out['authors'][:3])}」"
                )
    out["metadata_mismatch"] = mm
    if notes:
        out["notes"] = notes

    # ── 结论 ────────────────────────────────────────────────
    if out["retracted"]:
        out["verdict"] = "⚠️ 该文献已被撤稿 —— 不得作为证据引用"
    elif mm:
        out["verdict"] = "元数据与引用描述不一致 —— 需人工核对"
    else:
        out["verdict"] = "存在且元数据一致" + ("（未提供期望值，仅核验存在性）"
                                              if not (expect_title or expect_year or expect_authors)
                                              else "")
    return out


def verify_citation(
    doi: str,
    expect_title: str = "",
    expect_year: str = "",
    expect_authors: str = "",
) -> str:
    """工具入口：返回 JSON 文本。"""
    if not (doi or "").strip():
        return json.dumps({"error": "doi 不能为空"}, ensure_ascii=False)
    r = check_doi(doi, expect_title, expect_year, expect_authors)
    # 给模型一句明确的行动指引，而不只是丢一堆字段
    if r.get("retracted"):
        r["action"] = "把这条引用从稿件里删掉，或替换为未撤稿的原始研究并说明。"
    elif not r.get("exists") and not r.get("error"):
        r["action"] = "这条引用查无此文，极可能是编造的。删掉它，不要试图改写 DOI 蒙混。"
    elif r.get("metadata_mismatch"):
        r["action"] = "核对原始文献页面，按真实元数据改正引用字段。"
    return json.dumps(r, ensure_ascii=False, indent=1)


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "doi": {"type": "string", "description": "要核验的 DOI，如 10.1038/nature12373"},
        "expect_title": {"type": "string",
                         "description": "（可选）稿件里写的标题，用于一致性核对"},
        "expect_year": {"type": "string",
                        "description": "（可选）稿件里写的年份，用于一致性核对"},
        "expect_authors": {"type": "string",
                           "description": "（可选）稿件里写的作者（逗号分隔），用于一致性核对"},
    },
    "required": ["doi"],
}
