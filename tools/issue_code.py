# -*- coding: utf-8 -*-
"""发行方签发工具：一码一机（离线绑定）。

私钥只从文件或环境变量读，**不接受把它写进命令行**（shell history 会留底）：

    set RK_ISSUING_KEY=<私钥>                       # 或者用默认文件
    python tools/issue_code.py --customer "某大学" --mid RK-M-XXXXXXXXXXXXXX --days 365

默认私钥文件：`%USERPROFILE%\\.rongke\\issuing_key.txt`（不在仓库里）。
客户机器码在荣珂「试用与激活」页显示，形如 `RK-M-XXXXXXXXXXXXXX`。

输出一行 `RK1...`，把它发给客户；客户粘进同一页即可。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rongke import license as L                       # noqa: E402

DEFAULT_KEY = Path.home() / ".rongke" / "issuing_key.txt"


def _read_key(path: str, inline: str) -> str:
    if inline.strip():
        return inline.strip()
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(
            f"找不到私钥文件：{p}\n"
            "把它放进去（一行 base64），或用 --sk 临时传入；"
            "私钥不要提交进仓库。")
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise SystemExit(f"私钥文件是空的：{p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="荣珂发行方签发工具（一码一机）")
    ap.add_argument("--customer", default="", help="客户名（写进凭证）")
    ap.add_argument("--mid", default="", help="客户机器码 RK-M-...（授权页可见）")
    ap.add_argument("--plan", default="pro", help="套餐名（默认 pro）")
    ap.add_argument("--days", type=int, default=365, help="有效期天数；0=永久")
    ap.add_argument("--key-file", default=str(DEFAULT_KEY),
                    help=f"私钥文件（默认 {DEFAULT_KEY}）")
    ap.add_argument("--sk", default="",
                    help="私钥（不推荐：会进 shell history）；优先用 --key-file")
    ap.add_argument("--out", default="", help="把激活码另存到一个文件")
    a = ap.parse_args(argv)

    customer = a.customer.strip()
    if not customer:
        try:
            customer = input("客户名: ").strip()
        except EOFError:
            customer = ""
    if not customer:
        print("✘ 客户名不能为空", file=sys.stderr)
        return 2

    mid = a.mid.strip()
    if not mid:
        try:
            mid = input("客户机器码（RK-M-...）: ").strip()
        except EOFError:
            mid = ""
    mid = L.normalize_mid(mid)
    if not L.valid_mid(mid):
        print(f"✘ 机器码形状不对：{mid!r}（应为 RK-M- 加 16 位 Base32）",
              file=sys.stderr)
        return 2

    try:
        sk = L._b64d(_read_key(a.key_file, a.sk))
    except SystemExit:
        raise
    except Exception as e:                             # noqa: BLE001
        print(f"✘ 私钥不是合法 base64：{e}", file=sys.stderr)
        return 2

    derived = L._b64e(L.public_key(sk))
    if derived != L.EMBEDDED_PUBKEY:
        print("✘ 这把私钥与代码里内嵌的公钥不配对，签出来的码客户验不过。",
              file=sys.stderr)
        print(f"  私钥对应公钥：{derived[:12]}…  代码内嵌：{L.EMBEDDED_PUBKEY[:12]}…",
              file=sys.stderr)
        return 2

    expires = 0.0 if a.days <= 0 else time.time() + a.days * 86400
    payload = {"customer": customer, "plan": a.plan,
               "expires": expires, "issued": time.time(), "mid": mid}
    code = L.make_code(payload, sk)

    # 自检：用内嵌公钥把刚签的码验一遍，避免"签出来了但客户验不过"。
    try:
        got = L.parse_code(code)
    except ValueError as e:
        print(f"✘ 自检失败：{e}", file=sys.stderr)
        return 1
    if got.get("mid") != mid or got.get("customer") != customer:
        print("✘ 自检失败：解出的客户名/机器码不一致", file=sys.stderr)
        return 1

    print(code)
    print(f"\n  客户：{customer}    套餐：{a.plan}    "
          f"到期：{'永久' if not expires else L._ymd(expires)}")
    print(f"  机器码：{mid}（只在这台机器有效；换机/重装系统要新码）")
    print("  ↑ 把上面那行激活码发给客户，让他在「试用与激活」页粘贴。")

    if a.out:
        Path(a.out).write_text(code + "\n", encoding="utf-8", newline="\n")
        print(f"  已另存：{Path(a.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
