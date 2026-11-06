# -*- coding: utf-8 -*-
"""对话主路径的判据：`/api/run` + `/api/stream` 的**契约**（不碰真模型）。

## 为什么需要它

2026-10-12 体检发现：`web_test.py`（188 项）一条判据都没打在 `/api/run` / `/api/stream` 上
—— 而这两条是**对话主路径**，用户看到的一切都从这条流里来。
它的实际验证当时只有 `ui_shot.py` 回放归档 + 人工真跑，**没有机器判据兜底**。

补它的意义：真跑一次要 225 秒 + 花 token + 依赖网关当时的心情。
而**契约**（事件形状、终局帧、404 口径、SSE 格式）是能确定性测的。

## 关键设计：用 Run 替身，不走真内核

`POST /api/run` 会真的起内核 → 起 Origin 子进程 → 调模型。那既慢又不可重复。
这里**只测"流"这件事本身**：
  1. 造一个 `Run` 对象，直接塞进 `mgr.runs`（模拟"任务已在跑"）
  2. 起线程往它 `push()` 事件，然后 `finish()`
  3. 用真 HTTP 去 `/api/stream?id=`，**逐帧核** SSE 载荷

这样测到的是产品代码里真在用的那条 `_stream()` + `snapshot()`，
且**跑一次不到一秒、零 token、可重复**。

## 判据来源（不是我猜的，是照前端与既有实现抄的）

· `index.html` 用 `EventSource`，且**没有** `addEventListener("event", ...)` ⇒ 用 `data:` 帧
· 前端读 `kind` 字段判类型 ⇒ 事件类型在 JSON 里，不在 SSE 的 `event:` 行
· `_stream()` 的终局分支发 `kind:"_end"` 且 `data.result` 带最终产物
  （★ 这条尤其值得钉住：**交付正文只在 `_end` 帧里**，
   我 2026-10-12 就是没找到它，误报过"跑完没给答案"）
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import rongke.web.server as S                          # noqa: E402
from rongke.web.server import Run                      # noqa: E402

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


# ⚠️ 不启动真内核（与 web_test.py / routes_test.py 同一手法）
S.Manager.boot = lambda self: None                     # type: ignore[method-assign]

M = S.Manager()
httpd = S.Server(("127.0.0.1", 0), S.Handler)
httpd.mgr = M                                          # type: ignore[attr-defined]
_port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = f"http://127.0.0.1:{_port}"


def get(path: str):
    try:
        with _opener.open(BASE + path, timeout=15) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def post_json(path: str, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def jload(b: bytes):
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:                                  # noqa: BLE001
        return {}


# ══════════════════ 1. SSE 帧的解析（纯函数级，最便宜） ══════════════════
print("\n── 1. SSE 帧格式：类型在 data 里，不在 event: 行 ──")


def parse_sse(raw: str) -> list[dict]:
    """把 SSE 原文解析成事件字典。

    ⚠️ 只认 `data:` 行。筛 `event:` 行会永远得 0 条 ——
    我 2026-10-12 就是这么把"流是好的"报成"SSE 坏了"的。
    """
    out = []
    for block in raw.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:].strip()))
                except Exception:                      # noqa: BLE001
                    pass
    return out


# ══════════════════ 2. 404 口径 ══════════════════
print("\n── 2. 不存在的会话：404 而不是空流 ──")
try:
    st, body, _ = get("/api/run?id=_nope_xyz")
    check("GET /api/run?id=不存在 → 404", st, 404)
    truthy("带可读原因", "任务不存在" in jload(body).get("error", ""))

    st, body, _ = get("/api/stream?id=_nope_xyz")
    check("★ GET /api/stream?id=不存在 → 404（不是 200 空流 —— "
          "空流会让前端永远转圈）", st, 404)

    st, body, _ = get("/api/run")
    check("★ GET /api/run 不带 id → 404（不是 500、不是把 None 当任务）", st, 404)
except Exception as e:                                 # noqa: BLE001
    check(f"404 段冒出异常：{type(e).__name__}: {e}", False, True)

# ══════════════════ 3. 造替身任务，走真 HTTP 流 ══════════════════
print("\n── 3. 真 HTTP 走一遍 /api/stream（Run 替身，零模型调用）──")
_FAKE_ID = "_routes_fake_run"
_FAKE_RESULT = {"final": "这是交付正文：结论是 X。", "run_id": "20260101-000000",
                "draft": "草稿"}


def _drive(run: Run) -> None:
    """模拟内核：推几个事件，停顿一下，再推，最后 finish。

    停顿是有意的：**证明流是"边跑边推"的，不是攒完一次性吐**。
    """
    run.push("workspace", {"dir": "runs/20260101-000000"})
    run.push("contract", {"goal": "测试目标"})
    time.sleep(0.35)
    run.push("step", {"n": 1, "text": "第 1 步：模型在想"})
    run.push("deliver_attempt", {"chars": 12})
    time.sleep(0.25)
    run.finish(_FAKE_RESULT, "")


_fake = Run(id=_FAKE_ID, goal="测试主路径契约", max_steps=3)
M.runs[_FAKE_ID] = _fake

try:
    # 先核 snapshot（GET /api/run 就是它的输出）
    st, body, _ = get(f"/api/run?id={_FAKE_ID}")
    j = jload(body)
    check("GET /api/run 已存在的会话 → 200", st, 200)
    check("snapshot 带回 goal（前端标题用）", j.get("goal"), "测试主路径契约")
    truthy("snapshot 有 events 字段（刷新页面要靠它重放）",
           isinstance(j.get("events"), list))
    truthy("snapshot 有 done / resumable / followable 三个开关",
           all(k in j for k in ("done", "resumable", "followable")))

    # 真开流
    t = threading.Thread(target=_drive, args=(_fake,), daemon=True)
    t.start()

    st, raw, hdr = get(f"/api/stream?id={_FAKE_ID}")
    text = raw.decode("utf-8", "replace")

    check("流 → 200", st, 200)
    truthy("Content-Type 是 text/event-stream",
           "text/event-stream" in (hdr.get("Content-Type") or ""))
    truthy("★ 带 X-Accel-Buffering: no（否则反代会把流攒起来，实时性全没了）",
           (hdr.get("X-Accel-Buffering") or "").lower() == "no")

    evs = parse_sse(text)
    truthy(f"★ 解析出事件（≥5 帧，实得 {len(evs)}）—— 用 data: 解析，不是 event:",
           len(evs) >= 5)

    kinds = [e.get("kind") for e in evs]
    check("★ 终局帧是最后一条且 kind == '_end'（前端的收流判据）",
          kinds[-1] if kinds else None, "_end")
    for want in ("workspace", "contract", "step", "deliver_attempt"):
        truthy(f"推过的事件原样出现在流里：{want}", want in kinds)

    # ★ 最关键的一条：交付正文在 _end 帧里
    tail = evs[-1]
    res = (tail.get("data") or {}).get("result") or {}
    check("★ 交付正文在 _end → data.result.final（**不在 messages 里**）",
          res.get("final"), _FAKE_RESULT["final"])
    check("★ _end 带回落盘 run_id（前端靠它取产物；与内存会话 id 不是同一个）",
          res.get("run_id"), "20260101-000000")

    truthy("每帧都带单调 n（前端靠它去重/续传）",
           [e.get("n") for e in evs] == sorted(e.get("n") for e in evs))
    truthy("每帧都带 t 时间戳（界面显示用）",
           all(e.get("t") for e in evs))
except Exception as e:                                 # noqa: BLE001
    check(f"流段冒出异常：{type(e).__name__}: {e}", False, True)

# ══════════════════ 4. 边跑边推，不是攒完一次吐 ══════════════════
print("\n── 4. 流是增量推送的（这条决定界面会不会卡住不更新）──")
try:
    _id2 = "_routes_fake_run2"
    _fake2 = Run(id=_id2, goal="增量推送", max_steps=3)
    M.runs[_id2] = _fake2

    _seen: list[tuple[float, int]] = []

    def _reader():
        for line in _opener.open(f"{BASE}/api/stream?id={_id2}", timeout=15):
            if line.startswith(b"data:"):
                _seen.append((time.time(), 1))

    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()
    time.sleep(0.3)
    _t0 = time.time()
    _fake2.push("step", {"n": 1})
    time.sleep(0.6)
    _n_mid = len(_seen)
    _fake2.push("step", {"n": 2})
    time.sleep(0.4)
    _fake2.finish({"final": "done"}, "")
    rt.join(timeout=10)

    truthy("★ 第一个事件在任务结束**之前**就到了（≥1 帧先到）", _n_mid >= 1)
    truthy("任务结束后又收到了剩下的帧", len(_seen) > _n_mid)
except Exception as e:                                 # noqa: BLE001
    check(f"增量段冒出异常：{type(e).__name__}: {e}", False, True)

# ══════════════════ 5. POST /api/run 的边界（不真起任务） ══════════════════
print("\n── 5. POST /api/run 的入参边界 ──")
try:
    st, body = post_json("/api/run", {"goal": "   "})
    check("★ 负控：空目标 → 400（不是 500，也不是静默开一个空任务）", st, 400)
    truthy("理由说得出话", bool(jload(body).get("error")))

    st, body = post_json("/api/run", {"goal": ""})
    check("★ 负控：空字符串目标 → 400", st, 400)
except Exception as e:                                 # noqa: BLE001
    check(f"POST 边界段冒出异常：{type(e).__name__}: {e}", False, True)

# 清理替身
M.runs.pop(_FAKE_ID, None)
M.runs.pop("_routes_fake_run2", None)
httpd.shutdown()
httpd.server_close()

print("\n═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
sys.exit(0 if ok_all else 1)
