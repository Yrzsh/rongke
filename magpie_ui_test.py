# -*- coding: utf-8 -*-
"""设置页里的 magpie 卡片：在真浏览器里点，看它是不是真的能用。

用户的原话：「主要是让用户配置的方便一点」。
所以这张卡的存在意义是**别的字段一个都不用填**，判据也就照着这个写：

  ① 网关不在线 → 卡片必须说清「为什么连不上」+「怎么启动」，不能是空白
  ② 网关在线且有模型 → 必须把模型列成可点的按钮，文案用模型名原文
  ③ 后端 /api/magpie 如实回 online 与模型清单（清单来自网关本身）
  ④ 页面不许有 JS 报错（卡片是异步画的，最容易在这里炸）

★ 不碰真配置：这里只测到"列出模型"为止。点按钮会写 config.json，
  留给 magpie_switch_test.py 在临时目录里测。
★ 需要真起一个服务（RONGKE_WEB / argv[1]）——卡片要打 /api/magpie。
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8834/"
FAILS = []


def ok(m):
    print("[ok]  ", m)


def bad(m):
    FAILS.append(m)
    print("[FAIL]", m)


class FakeMagpie(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            b = json.dumps({"object": "list", "data": [
                {"id": "deepseek/deepseek-v4-pro", "object": "model"},
                {"id": "moonshot/kimi-k2.5", "object": "model"},
                {"id": "zhipu/glm-5.3", "object": "model"},
            ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self.send_response(404)
            self.end_headers()


srv = HTTPServer(("127.0.0.1", 0), FakeMagpie)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"假 magpie 网关: 127.0.0.1:{port}")

with sync_playwright() as pw:
    b = pw.chromium.launch()
    pg = b.new_page(viewport={"width": 1280, "height": 900})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_timeout(1500)

    # 打开设置页（视图名是 "set" —— 见 workspace.js 的 goView）
    opened = pg.evaluate("""() => {
        const b = document.querySelector('.navbtn[data-v="set"]');
        if (!b) return false;
        b.click();
        return true;
    }""")
    if not opened:
        bad("找不到设置页的导航按钮 .navbtn[data-v=\"set\"]")
    pg.wait_for_timeout(2500)

    card = pg.query_selector("#magpieCard")
    if not card:
        bad("★ 设置页里没有 magpie 卡片")
    else:
        ok("设置页里出现了 magpie 卡片")
        txt = (card.inner_text() or "")
        print("   卡片内容（前 260 字）：", txt[:260].replace("\n", " | "))

    # ① 本机 3425 没服务时：应给出启动指引
    if "docker run" in txt or "启动" in txt or "没检测到" in txt:
        ok("网关不在线时给了启动指引（不是空白）")
    else:
        bad(f"★ 网关不在线时没有指引：{txt[:120]}")

    be = pg.evaluate("""async () => {
        const r = await fetch('/api/magpie?host=' + encodeURIComponent('http://127.0.0.1:%d'));
        return await r.json();
    }""" % port)
    if be.get("online"):
        ok(f"后端 probe 认得这个网关（online={be.get('online')}）")
    else:
        bad(f"★ 后端说不在线：{be}")

    ms = be.get("models") or []
    if set(ms) == {"deepseek/deepseek-v4-pro", "moonshot/kimi-k2.5", "zhipu/glm-5.3"}:
        ok(f"模型清单来自网关本身：{ms}")
    else:
        bad(f"★ 模型清单不对：{ms}")

    # ② 界面切到那个真网关：模型应变成可点的按钮
    pg.evaluate("""(p) => { paintMagpie('http://127.0.0.1:' + p); }""", port)
    pg.wait_for_timeout(2000)
    btns = pg.query_selector_all("#magpieBody [data-mg]")
    if len(btns) == 3:
        ok(f"网关上 3 个模型都列成了可点的按钮（{len(btns)} 个）")
    else:
        bad(f"★ 模型按钮数不对：{len(btns)}")

    if btns:
        labels = [(x.inner_text() or "").strip() for x in btns]
        print("   按钮文案：", labels)
        if "deepseek/deepseek-v4-pro" in labels:
            ok("按钮用的是模型名原文（没有改写成别的）")
        else:
            bad(f"★ 按钮文案不对：{labels}")

    print()
    js_errs = [e for e in errs if "favicon" not in e.lower()]
    if js_errs:
        bad(f"★ 页面有 JS 报错：{js_errs[:3]}")
    else:
        ok("页面没有 JS 报错")
    b.close()

srv.shutdown()

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项失败")
    sys.exit(1)
print("✔ magpie 卡片可用")
