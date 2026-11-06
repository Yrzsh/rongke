# -*- coding: utf-8 -*-
"""文献全文获取 —— 荣珂**自带**的一条取全文通路。

## 为什么内核要自带这个

商业化产品的判断只有一句：**能力必须跟着产品走，不能跟着用户的机器走**。
在此之前，「取到全文」这件事完全寄在外部 workbench 的 `litkb._try_download_oa_pdf` 上，
而那条路只有三条：

    ① Unpaywall 给的出版方 PDF 直链 → ② doi.org 内容协商 → ③ Sci-Hub（默认关）

**没有 Europe PMC `fullTextXML`。** 这不是小事：真跑一次综述
（`runs/20261014-114007`）入 15 篇、实际只在磁盘上落下 3 篇全文（20%），
而那 12 篇里有相当一部分**在 PMC 有开放全文** —— 走 `fullTextXML` 是 200 + 十几万字，
走 Unpaywall 给的出版方直链却 403（MDPI / Hindawi 实测）。整份 `litkb.py` 里
`grep -i 'europepmc|fullTextXML'` 命中 0 次。

⇒ 结论：**下不到，多数是路由缺一条，不是世上没有开放版。** 本模块补的就是那条路。

## 路由顺序（这个顺序是实测定的，不是拍的）

    ① Europe PMC 检索拿 pmcid → `PMC<id>/fullTextXML`（200，JATS 正文）
    ② Unpaywall `is_oa` → `url_for_pdf` 直链（能判 OA，但直链常被反爬挡）
    ③ `doi.org` 内容协商（`Accept: application/pdf`）

为什么 ① 必须排第一：它是**官方 API**，稳定、无需 key、回的是结构化正文；
②③ 都是"拿到一个链接再去取"，而链接那一头随时可能变成 403 挑战页。

★ **验收不能按 `%PDF` 魔数判。** ① 回的是 JATS XML 不是 PDF，按魔数判会把
成功当失败（这个坑已经在 litkb 那边炸过一次）。本模块按「有没有可用正文」判：
PDF 看魔数，XML/HTML 看抽出来的正文字符数。

## 与 litkb 的分工（不重叠、不互相替代）

- 本模块**只管取**：给 DOI，回一份落盘的全文 + 一句如实的路由说明。
- **入库仍然走 `kb_add` / `kb_ingest`** —— 那是 litkb 的事，索引、去重、
  段落切分都在它那边。本模块**不写库**，避免出现两套索引。

## 只用标准库

`urllib` + `json` + `re`。不引第三方包，保持 README 的零依赖承诺。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: 自我标识的 UA。Crossref / Europe PMC / Unpaywall 对匿名默认 UA 会限流或 422。
_UA = {"User-Agent": "Rongke/0.0.1 (research agent; mailto:rongke@localhost)"}

EUROPEPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPEPMC_FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"

#: 认为"拿到了正文"的最小字符数。低于这个数说明抓回来的是挑战页/登录页/空壳，
#: 不是正文。1200 是照「一篇摘要都有 1500+ 字符」估的，宁严勿松。
MIN_BODY_CHARS = 1200

#: 一次请求的超时（秒）。
TIMEOUT = 40


# ══════════════════ 小工具 ══════════════════

def _norm_doi(doi: str) -> str:
    """规范化 DOI：去前缀、去 URL 外壳、去尾随标点。"""
    s = str(doi or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", s, flags=re.I)
    s = s.strip().rstrip(".,;）)】]")
    return s


def _get(url: str, timeout: int = TIMEOUT, accept: str = "") -> tuple[int, bytes, str]:
    """GET，回 `(状态码, 字节, 内容类型)`。**网络类异常不吞**，交给调用方判。"""
    headers = dict(_UA)
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(getattr(r, "status", 200) or 200), r.read(), \
                (r.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as e:
        return int(e.code), b"", ""


def _strip_tags(xml: str) -> str:
    """JATS/HTML → 纯文本。**只做去标签 + 解实体**，不做排版重建。

    为什么这么粗：本模块的产物要交给 litkb 入库，入库那条路自己会抽一遍。
    这里只需要"人/模型能判断这是不是正文"，不需要做成完美的 Markdown。
    """
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", xml)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = (s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
          .replace("&#39;", "'").replace("&nbsp;", " ").replace("&amp;", "&"))
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


# ══════════════════ 三条路 ══════════════════

def _pmcid_for(doi: str) -> tuple[str, str]:
    """Europe PMC 检索，回 `(pmcid, 题名)`。没有就回 `("", "")`。

    ★ 用 `resultType=core` 是因为只有它才带 `pmcid`；默认结果里那个字段是缺的
    —— 按默认结果判会得出"这篇没有 PMC 版"的错误结论。
    """
    q = urllib.parse.urlencode({
        "query": f'DOI:"{doi}"',
        "format": "json",
        "resultType": "core",
        "pageSize": "1",
    })
    code, body, _ = _get(f"{EUROPEPMC_SEARCH}?{q}")
    if code != 200 or not body:
        return "", ""
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return "", ""
    for it in ((data.get("resultList") or {}).get("result") or []):
        pmcid = str(it.get("pmcid") or "").strip()
        if pmcid:
            return pmcid, str(it.get("title") or "").strip()
    return "", ""


def _via_europepmc(doi: str, dest_dir: Path) -> dict | None:
    """路 ①：Europe PMC `fullTextXML`。成功回 dict，不成功回 None。"""
    pmcid, title = _pmcid_for(doi)
    if not pmcid:
        return None
    code, body, ctype = _get(EUROPEPMC_FULLTEXT.format(pmcid=urllib.parse.quote(pmcid)))
    if code != 200 or not body:
        return None
    text = _strip_tags(body.decode("utf-8", errors="replace"))
    if len(text) < MIN_BODY_CHARS:
        # 拿回来的是壳子（无正文的 PMC 条目、或权限页）—— 不算成功
        return None
    safe = re.sub(r"[^\w.\-]+", "_", doi)[:80] or pmcid
    out = dest_dir / f"{safe}.fulltext.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return {
        "route": "europepmc-fulltextxml",
        "file": str(out),
        "chars": len(text),
        "content_type": ctype or "application/xml",
        "pmcid": pmcid,
        "title": title,
        "note": ("官方 API 回的 JATS 全文（**不是 PDF**，别按 %PDF 魔数判失败）。"
                 "正文已完整，可直接交给 kb_add / kb_ingest 入库。"),
    }


def _via_unpaywall(doi: str, dest_dir: Path) -> dict | None:
    """路 ②：Unpaywall 判 OA 并给 PDF 直链。"""
    email = "rongke@localhost"
    code, body, _ = _get(UNPAYWALL.format(doi=urllib.parse.quote(doi, safe=""))
                         + "?" + urllib.parse.urlencode({"email": email}))
    if code != 200 or not body:
        return None
    try:
        meta = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not meta.get("is_oa"):
        return None
    urls: list[str] = []
    best = meta.get("best_oa_location") or {}
    for u in [best.get("url_for_pdf"), best.get("url")]:
        if u:
            urls.append(u)
    for loc in (meta.get("oa_locations") or []):
        u = loc.get("url_for_pdf") or loc.get("url")
        if u:
            urls.append(u)
    for u in dict.fromkeys(urls):                     # 去重且保序
        code, body, ctype = _get(u, accept="application/pdf")
        if code != 200 or not body:
            continue
        if body[:5] == b"%PDF-":
            safe = re.sub(r"[^\w.\-]+", "_", doi)[:80]
            out = dest_dir / f"{safe}.pdf"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(body)
            return {
                "route": "unpaywall-pdf",
                "file": str(out),
                "bytes": len(body),
                "chars": 0,
                "content_type": ctype or "application/pdf",
                "url": u,
                "note": ("Unpaywall 的 OA 直链取回的**真 PDF**。"
                         "⚠️ 同一篇的这条链接在别的出口 IP 上可能变成 403 挑战页 —— "
                         "这次成功如实记着，但那不是稳定承诺。"),
            }
        # 200 但不是 PDF ⇒ 挑战页/登录页。**不能存成 PDF**。
    return None


def _via_doi_content(doi: str, dest_dir: Path) -> dict | None:
    """路 ③：doi.org 内容协商。"""
    url = "https://doi.org/" + urllib.parse.quote(doi, safe="/")
    code, body, ctype = _get(url, accept="application/pdf")
    if code != 200 or not body or body[:5] != b"%PDF-":
        return None
    safe = re.sub(r"[^\w.\-]+", "_", doi)[:80]
    out = dest_dir / f"{safe}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    return {
        "route": "doi-content-negotiation",
        "file": str(out),
        "bytes": len(body),
        "chars": 0,
        "content_type": ctype or "application/pdf",
        "url": url,
        "note": "doi.org 内容协商直接回的 PDF。",
    }


# ══════════════════ 对外入口 ══════════════════

def fetch_one(doi: str, dest_dir: str | Path) -> dict:
    """取**一篇**的全文，按 ①②③ 顺序试。**永不抛网络异常**，失败如实回。"""
    d = _norm_doi(doi)
    if not d:
        return {"doi": doi, "ok": False, "error": "DOI 为空", "tried": []}
    dest = Path(dest_dir)
    tried: list[dict] = []
    for name, fn in (("europepmc-fulltextxml", _via_europepmc),
                     ("unpaywall-pdf", _via_unpaywall),
                     ("doi-content-negotiation", _via_doi_content)):
        try:
            got = fn(d, dest)
        except Exception as e:                        # noqa: BLE001
            # 网络抖动不许让整批失败；但**必须记下**，否则"没下到"会被误读成
            # "世上没有"，而那正是本模块要修的那种错。
            tried.append({"route": name, "ok": False,
                          "why": f"{type(e).__name__}: {e}"})
            continue
        if got:
            got.update({"doi": d, "ok": True, "tried": tried})
            return got
        tried.append({"route": name, "ok": False, "why": _why_failed(name)})
    return {
        "doi": d,
        "ok": False,
        "tried": tried,
        "error": ("三条路都没有拿到可用正文。"
                  "**这说的是「这三条路这次没成」，不是「世上没有开放版」** —— "
                  "剩下的可能是：机构订阅（WebVPN/EZproxy）、CNKI 中文库、"
                  "或出版商需要账号。要如实写进检索死区，不要写成「该文献不存在」。"),
    }


def _why_failed(route: str) -> str:
    return {
        "europepmc-fulltextxml": "Europe PMC 无该 DOI 的 PMC 记录，或 fullTextXML 不是 200 / 正文不足",
        "unpaywall-pdf": "Unpaywall 判非 OA，或给了直链但取回的是挑战页/非 PDF",
        "doi-content-negotiation": "doi.org 内容协商未回 PDF（多数是出版商要订阅）",
    }.get(route, "未知原因")


def fetch_many(dois: list[str], dest_dir: str | Path) -> dict:
    """取**一批**的全文。逐个来，不并发 —— 这几个 API 都对突发请求限流。"""
    rows = [fetch_one(d, dest_dir) for d in (dois or [])]
    got = [r for r in rows if r.get("ok")]
    by_route: dict[str, int] = {}
    for r in got:
        by_route[r.get("route", "?")] = by_route.get(r.get("route", "?"), 0) + 1
    return {
        "requested": len(rows),
        "ok": len(got),
        "failed": len(rows) - len(got),
        "by_route": by_route,
        "items": rows,
        # ★ 这句话必须由内核自己写在回包里，而不是指望模型记得：
        #   本仓反复付代价的就是"把 20% 说成 100%"这一类措辞。
        "next": ("取得全文的 %d 篇请**立刻**用 kb_add / kb_ingest 入库 —— "
                 "`file` 字段就是可直接入库的绝对路径。"
                 "没取到的 %d 篇要如实写进检索死区，并写明缺的是哪条路。"
                 % (len(got), len(rows) - len(got))),
    }
