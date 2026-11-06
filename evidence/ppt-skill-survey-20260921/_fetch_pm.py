"""拉取 PPT Master 深层参考文档（Part 2 调研）。

设计要点（都是这次踩出来的）：
  · raw.githubusercontent / curl(schannel) / 网页 三条都不通 ⇒ 只用
    ① api.github.com/contents（主力，抖，需重试）
    ② fastly.jsdelivr.net 镜像（兜底，更快）
  · 一个进程只拉一个文件 —— 连拉多个会被前台 SIGTERM 静默带走。
  · 判据：内容不能以 '<' 开头（镜像会静默回落成 HTML 首页）。
"""
import base64
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

REPO = "hugohe3/ppt-master"
BRANCH = "main"
OUT = Path(__file__).resolve().parent / "pm"
CTX = ssl.create_default_context()

# 路径候选：同一份文件在不同文档里的相对写法不同，两条都试
CANDIDATES = [
    "skills/ppt-master/governance/failure-recovery.md",
    "skills/ppt-master/workflows/governance/failure-recovery.md",
    "skills/ppt-master/references/strategist.md",
    "skills/ppt-master/references/executor-base.md",
    "skills/ppt-master/references/semantic-svg.md",
    "skills/ppt-master/references/artifact-ownership.md",
    "docs/technical-design.md",
    "docs/why-ppt-master.md",
]


def _fetch(url, tries=8, timeout=15):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
        except Exception:
            time.sleep(0.8)
    return None


def fetch(path):
    """先 contents API，再 jsDelivr 镜像。"""
    b = _fetch(f"https://api.github.com/repos/{REPO}/contents/{path}")
    if b:
        try:
            d = json.loads(b)
            if "content" in d:
                return base64.b64decode(d["content"]), "api"
        except Exception:
            pass
    for host in ("https://fastly.jsdelivr.net/gh/", "https://cdn.jsdelivr.net/gh/"):
        b = _fetch(f"{host}{REPO}@{BRANCH}/{path}")
        if b and b.lstrip() and not b.lstrip().startswith(b"<"):
            return b, "jsdelivr"
    return None, None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1:] or CANDIDATES
    for path in only:
        out = OUT / Path(path).name
        if out.exists() and out.stat().st_size > 500:
            print(f"skip (exists) {out.name}")
            continue
        data, via = fetch(path)
        if not data:
            print(f"FAIL  {path}")
            continue
        out.write_bytes(data)
        print(f"OK    {out.name:34} {len(data):>7}  via={via}")


if __name__ == "__main__":
    main()
