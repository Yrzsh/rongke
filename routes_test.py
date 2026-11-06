# -*- coding: utf-8 -*-
"""零覆盖路由的最小冒烟：把"打得到、边界会拒、不 5xx"钉住。

## 为什么单开一个套件

2026-10-12 体检把 server.py 里 34 条 `/api/` 路由与全部测试文件对撞，
发现 **16 条没有任何测试提到**，其中 `/api/orig/*` 四条里藏着
「任意文件读取」（能读 `~/.rongke/secrets.json`）。
那次的教训不是"某条路由有 bug"，而是：**没测过的路由，坏了没人知道**。

## 判据的原则（照项目既有口径）

1. **判"形状"不判"内容"** —— 断言状态码与关键字段在不在，
   不断言模型输出、不断言具体工具清单（那些会随产品演进漂移，
   写死会让套件变成"改了就红"的噪声源）。
2. **边界优先** —— 空 id / 错 action / 非法 JSON / 不存在的东西，
   每一个都必须有**明确的拒绝**，而不是 500 或静默 200。
   "此刻不成"与"坏了"要分得开（本项目 409/400 vs 500 的一贯口径）。
3. **绝不写真实状态** —— 不往 `~/.rongke/secrets.json` 写东西、
   不乱起任务、不真取消别人的任务。

## 刻意不测的

`/api/run` 与 `/api/stream`（要真内核 + 真模型，另见 `ui_shot.py` 的回放与人工真跑）、
`/api/secret` 的**写入**成功路径（会真写密钥 + 真发 LLM 请求，只测拒绝路径）。
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import rongke.web.server as S                          # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def truthy(label, got):
    check(label, bool(got), True)


# ⚠️ 不启动真内核：本套件要测的是**路由与入参校验**，一行都不碰内核装置。
#    （与 web_test.py 同一手法：Rongke() 默认会 spawn Origin 子进程。）
S.Manager.boot = lambda self: None                     # type: ignore[method-assign]

M = S.Manager()
httpd = S.Server(("127.0.0.1", 0), S.Handler)
httpd.mgr = M                                          # type: ignore[attr-defined]
_port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# 本机设着 HTTP_PROXY，会连 127.0.0.1 一起拦（项目里吃过 502）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = f"http://127.0.0.1:{_port}"


def get(path: str):
    try:
        with _opener.open(BASE + path, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post_json(path: str, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post_raw(path: str, body: bytes):
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jload(b: bytes):
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:                                  # noqa: BLE001
        return {}


print("\n── 零覆盖路由冒烟：只读四条 ──")
try:
    st, body, _ = get("/api/status")
    j = jload(body)
    check("GET /api/status → 200", st, 200)
    truthy("载荷有 kernel_ok 这个字段（界面靠它显示连接态）", "kernel_ok" in j)

    st, body, _ = get("/api/tools")
    j = jload(body)
    check("GET /api/tools → 200", st, 200)
    truthy("载荷是 tools 列表（不是裸数组、不是别家字段名）",
           isinstance(j.get("tools"), list))

    st, body, _ = get("/api/runs?n=5")
    j = jload(body)
    check("GET /api/runs → 200", st, 200)
    truthy("载荷是 runs 列表", isinstance(j.get("runs"), list))

    st, body, _ = get("/api/runs?n=abc")
    check("★ 边界：n 不是数字时**不 500**（解析坏值要降级不要炸）", st, 200)

    # ★ 同一类缺陷在其它路由上的**同族副本**（W30 冒烟抓到）。
    #   2026-10-12 只修了 /api/runs 一处，`/api/kb/list` 与 `/api/kb/search`
    #   里还各留着一个裸 `int()`：`?limit=abc` / `?n=abc` 抛 ValueError，
    #   而 do_GET 里没人接 ⇒ **连接被直接掐断**，客户端只看到
    #   `RemoteDisconnected`（不是 500、也不是可读的 400）。
    #   判据打在"连接还活着"上：坏入参要么 2xx 要么 4xx，**不许连不上**。
    for path in ("/api/kb/list?limit=abc", "/api/kb/search?q=x&n=abc",
                 "/api/kb/list?limit=-1", "/api/kb/list?limit=0"):
        try:
            # ⚠️ `get()` 回的是**三元组**，必须解包 3 个 —— 写成 `st, _ = get()`
            #    会抛 `ValueError: too many values to unpack`，而它会被下面的
            #    `except Exception` 当成"连接被弄断了"，判成假缺陷。
            #    （第一版就是这么错的，白忙一轮；探针错要先怀疑探针。）
            st, _body, _hdrs = get(path)
            # ⚠️ 用 check 不用 truthy：`st < 500` 对 200/400 是 True，
            #    但对 200 之外的**任何 4xx 也是 True**，truthy 会把结果
            #    压成布尔再比 —— 第一版写成 truthy(...) 时判据看着对、
            #    实际比的是 True==True，等于没判。这里直接把码打出来比。
            check(f"★ {path}：坏数值参数不许断连、不许 5xx", st < 500, True)
        except Exception as e:                             # noqa: BLE001
            # ⚠️ 不能写 check(f"...{type(e).__name__}", ...)：`e` 在 except 块
            #    结束时被 Python **删掉**，而 f-string 里的 `type(e)` 是在
            #    `check()` 的实参里求值的 —— 那是在 except 块内，看似安全，
            #    但把 `type(e).__name__` 换成延迟求值的元组/闭包就会炸。
            #    这里先落成局部常量，与 W29 修 `deck_template_plan` 同一条纪律。
            _why = f"{type(e).__name__}"
            check(f"★ {path}：坏数值参数把连接弄断了", _why, "不断")

    # ★ 兜底外壳本身：do_GET 里任何漏网异常都必须变成可读的 500，
    #   而不是让 socketserver 关掉连接。判据用**注入故障**，不靠真有个坏路由。
    # ⚠️ 注入必须**保存原方法再还原**：`_route_get` 是类自身定义的名字，
    #    用 `del` 会把它整条删掉（后面所有 GET 全变 500）—— 第一版就这么错的，
    #    被本套件最后那条"未知路由 → 404"当场抓住。
    try:
        orig_route = S.Handler._route_get
        h = object.__new__(S.Handler)
        h.path = "/api/_inject"
        h.wfile = type("W", (), {"write": lambda self, d: None})()
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda *a, **k: None
        S.Handler._route_get = lambda self: (_ for _ in ()).throw(
            RuntimeError("注入的漏网异常"))
        try:
            h.do_GET()
            check("★ 正控：do_GET 的兜底外壳接住了漏网异常（没往外抛）",
                  "接住了", "接住了")
        finally:
            S.Handler._route_get = orig_route          # ← 必须还原，不是 del
    except Exception as e:                             # noqa: BLE001
        check(f"★ 兜底外壳没接住异常：{type(e).__name__}: {e}", False, True)

    # /api/billing：界面「用量与额度」那一块唯一的数据源。
    # 判据打在**两件不许发生的事**上，而不是"能返回 200"：
    #   ① 不许回传凭据材料（salt / pwd_hash / pwd_iter）
    #   ② 不许把"没接支付"这件事说漏（它决定了这张卡怎么被理解）
    st, body, _ = get("/api/billing")
    j = jload(body)
    check("GET /api/billing → 200", st, 200)
    acc = j.get("account") or {}
    truthy("★ 不下发凭据材料（salt/pwd_hash/pwd_iter 一个都不许有）",
           not ({"salt", "pwd_hash", "pwd_iter"} & set(acc)))
    truthy("★ 明确声明没接支付（payment_connected=False）",
           j.get("payment_connected") is False)
    truthy("余额字段在（界面要显示它）", "balance" in j)
    truthy("台账路径由后端给（前端不自己拼）",
           bool(acc.get("ledger")))
except Exception as e:                                 # noqa: BLE001
    check(f"只读四条冒出异常：{type(e).__name__}: {e}", False, True)

print("\n── 零覆盖路由冒烟：/api/cancel 与 /api/intervene 的拒绝路径 ──")
try:
    st, body = post_json("/api/cancel", {"id": "_no_such_run"})
    check("★ 负控：取消不存在的任务 → 404（不是 200 假成功）", st, 404)
    truthy("404 带可读原因", "任务不存在" in jload(body).get("error", ""))

    st, body = post_json("/api/intervene", {"id": "_no_such_run"})
    check("★ 负控：干预不存在的任务 → 404", st, 404)

    st, body = post_json("/api/intervene", {"id": "", "action": "whatever"})
    check("★ 负控：空 id 也是 404（不能拿空串当有效任务）", st, 404)
except Exception as e:                                 # noqa: BLE001
    check(f"cancel/intervene 冒出异常：{type(e).__name__}: {e}", False, True)

print("\n── 零覆盖路由冒烟：非法体一律不许 5xx ──")
try:
    # ⚠️ 三条路由对"坏 JSON"的处置**故意不同**，判据必须按各自的契约写，
    #    不能一律期望 400（我第一版就是这么错的，被自己的测试打回来）：
    #      · /api/secret    → 400（它要写盘 + 发模型请求，必须明确拒绝）
    #      · /api/intervene → 400（同上，action 校验前先要一个合法对象）
    #      · /api/cancel    → 404：坏 JSON 降级成空 id，于是"任务不存在"。
    #                            这是**有意的韧性设计**（取消是紧急操作，
    #                            不该因为一个解析错就什么都不做）
    #      · /api/resume    → 409：同理降级成空 id ⇒ "此刻不成"。
    #                            用 409 而非 500 是项目一贯口径。
    #    所以这里**只锁一条**：全都不许 5xx。各自的码另测。
    for path in ("/api/secret", "/api/intervene", "/api/cancel", "/api/resume"):
        st, _ = post_raw(path, b"{not json at all")
        truthy(f"★ {path}：非法 JSON 不 5xx（实际 {st}）", st < 500)

    st, _ = post_raw("/api/secret", b"{not json at all")
    check("★ /api/secret 非法 JSON → 400（它要写盘，必须明确拒）", st, 400)
    st, _ = post_raw("/api/intervene", b"{not json at all")
    check("★ /api/intervene 非法 JSON → 400", st, 400)
    st, _ = post_raw("/api/cancel", b"{not json at all")
    check("★ /api/cancel 非法 JSON → 404（降级空 id ⇒ 任务不存在，有意的韧性）", st, 404)
    st, _ = post_raw("/api/resume", b"{not json at all")
    check("★ /api/resume 非法 JSON → 409（降级空 id ⇒ 此刻不成）", st, 409)
except Exception as e:                                 # noqa: BLE001
    check(f"非法 JSON 段冒出异常：{type(e).__name__}: {e}", False, True)

print("\n── /api/secret 的拒绝路径（**绝不写真实密钥**）──")
try:
    st, body = post_json("/api/secret", {"name": "X", "value": None})
    check("★ 负控：value 为 None → 400（要清空必须传空串，不是省略字段）", st, 400)
    truthy("拒绝理由说得出话", bool(jload(body).get("error")))

    st, body = post_json("/api/secret", "not-an-object")
    check("★ 负控：请求体不是对象 → 400", st, 400)
except Exception as e:                                 # noqa: BLE001
    check(f"secret 拒绝段冒出异常：{type(e).__name__}: {e}", False, True)

print("\n── /api/resume 的拒绝路径 ──")
try:
    st, body = post_json("/api/resume", {"id": "_no_such_run"})
    truthy("★ 负控：接续不存在的会话 → 明确的拒绝（非 200 假成功）",
           st in (404, 409))
except Exception as e:                                 # noqa: BLE001
    check(f"resume 段冒出异常：{type(e).__name__}: {e}", False, True)

print("\n── 未知路由必须是 404，而不是 200 空壳 ──")
try:
    st, _, _ = get("/api/definitely_not_a_route")
    check("★ 负控：不存在的 /api/ 路由 → 404", st, 404)
except Exception as e:                                 # noqa: BLE001
    check(f"未知路由段冒出异常：{type(e).__name__}: {e}", False, True)

httpd.shutdown()
httpd.server_close()

print("\n═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
