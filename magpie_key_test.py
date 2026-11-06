# -*- coding: utf-8 -*-
"""切到 magpie 时，服务端会不会顺手把密钥补上 —— 走真接口，不抄逻辑。

为什么要这个文件：自动补密钥是**服务端替用户写一个密钥**。
一个"永远写 magpie 占位值"的实现也能让主判据变绿，
所以这里一半是正控、一半是负控：

  ① 切到 magpie + 本来没密钥 → 自动补上，重新读设置也说可用
  ② 切到非 magpie 端点 → 一个字节都不许碰密钥（否则会拿 magpie
     顶替别人的 key，那是最坏的一种串台）
  ③ 负控本身要有效：清空之后必须**回读确认真的空了**，
     否则②看着"没补"，实际只是"原来就有"

★ 不抄任何判断逻辑：只打 HTTP。第一版 _t_key.py 测的是我复写的判据，
  那不是产品代码，已删。
★ 会真改配置与密钥：跑前备份 config.json，跑完还原。
  密钥落在 $USERPROFILE 下 —— 要隔离就跑 RONGKE_ROOT/RONGKE_DATA/USERPROFILE
  都指向同一个临时目录的服务实例（本文件不负责起服务）。
"""
from __future__ import annotations

import json
import shutil
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8834"
ROOT = Path(__file__).parent
CFG = ROOT / "config.json"
FAILS: list[str] = []


def ok(m):
    print("[ok]  ", m)


def bad(m):
    FAILS.append(m)
    print("[FAIL]", m)


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


bak = CFG.with_suffix(".json.magpietest_bak")
had = CFG.exists()
if had:
    shutil.copy2(CFG, bak)

try:
    before = call("/api/settings").get("provider") or {}
    print(f"改前：{before.get('base_url')} / 密钥 {'有' if before.get('key_present') else '无'}")
    print()

    # ① 全新用户的情形：当前通道没密钥，直接切到 magpie。
    #    ★ 不在这一步之前清密钥 —— 清空是一次写，切换是另一次写，
    #      两次独立写之间没有顺序保证（第一次实测就是被这个卡住：
    #      回执说补了、重读却空。根因是我的测试写法，不是产品）。
    #      真实用户从"没配过密钥"到"点 magpie"就是一步。
    r = call("/api/settings", {
        "provider.base_url": "http://127.0.0.1:3425/v1",
        "provider.model": "deepseek/deepseek-v4-pro",
        "provider.api_key_env": "MAGPIE_API_KEY",
        "provider.send_reasoning_effort": "false",
    })
    if not r.get("ok"):
        bad(f"★ 切换失败：{r.get('error')}")
    else:
        ok("切到 magpie 成功")
        print("   回执 key_autofilled =", r.get("key_autofilled"))
        print("   回执 key_note       =", str(r.get("key_note"))[:110])
        if r.get("key_autofilled") is True:
            ok("服务端说它自动补了密钥")
        else:
            bad("★ 没有自动补密钥")
        if r.get("key_present"):
            ok("回执说密钥可用")
        else:
            bad(f"★ 回执说密钥仍不可用：{r.get('provider')}")

    # ② 再读一次设置：界面看到的是不是真的"密钥可用"
    # ★ `key_present` 是**顶层**字段（server.py 的 settings_view），
    #   不在 provider 里。第一版从 provider 取，永远是 None ⇒ 判据恒红，
    #   而那不是产品的问题，是我读错了结构。回执那边用对了才没一起红。
    after = call("/api/settings")
    ap = after.get("provider") or {}
    if ap.get("base_url") == "http://127.0.0.1:3425/v1":
        ok("端点确实写进去了")
    else:
        bad(f"★ 端点没写进去：{ap.get('base_url')}")
    if after.get("key_present"):
        ok("重新读设置：密钥可用（界面不会再喊「还没有密钥」）")
    else:
        bad(f"★ 重新读设置：密钥仍不可用（key_present="
            f"{after.get('key_present')}, in_file={after.get('key_in_secrets_file')}）")
    if after.get("key_in_secrets_file"):
        ok("设置页会显示「已存在本机密钥文件」")
    else:
        ok("密钥可用但不在本机文件里（环境变量提供）")

    # ③ 负控：切到非 magpie 端点，不许补密钥。
    #    先把密钥清成空 —— 清完**回读确认**它真的空了，再切。
    #    （不清就切，负控根本没被触发：密钥还是刚才补的那个，看着"没补"，
    #      但那是"原来就有"，不是"拒绝补"。）
    call("/api/secret", {"name": "MAGPIE_API_KEY", "value": ""})
    chk = call("/api/settings")
    if chk.get("key_present"):
        bad("★ 清空没生效，负控无效")
    else:
        ok("先把密钥清空并回读确认（负控才有意义）")

    r2 = call("/api/settings", {
        "provider.base_url": "https://api.commandcode.ai/provider/v1",
        "provider.model": "deepseek/deepseek-v4.1-flash",
    })
    if r2.get("key_autofilled"):
        bad("★ 切到非 magpie 端点也自动补了密钥 —— 会拿 magpie 顶替别人的 key")
    else:
        ok("负控成立：切到非 magpie 端点，没有补密钥")

    back = call("/api/settings")
    if not back.get("key_present"):
        ok("负控成立：切回去之后密钥仍是空的（没被 magpie 占位值污染）")
    else:
        bad("★ 非 magpie 通道的密钥被写入了")

finally:
    if had:
        shutil.copy2(bak, CFG)
        bak.unlink()
        print("\n已还原 config.json")
    else:
        CFG.unlink(missing_ok=True)
        print("\n已删除测试新建的 config.json")

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项失败")
    sys.exit(1)
print("✔ 自动补密钥在真接口上成立，且没有误伤别的通道")
