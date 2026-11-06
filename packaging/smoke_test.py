# -*- coding: utf-8 -*-
"""冻结版端到端冒烟：对构建出来的 Rongke.exe 验「真的能用」。

验的不是单元测试，而是打包最危险的几件事：
  1. 静态资产/index.html/favicon 有没有漏进 _internal；
  2. 全新数据根第一次打开是否真的从 30 天试用开始；
  3. 试用过期后 /api/run 与 /api/follow 是否 403，而只读接口照常 200；
  4. --check 是否仍然只读（不创建 ~/.rongke）。

用法：
    python packaging/smoke_test.py [--exe dist/Rongke/Rongke.exe]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = ROOT / "dist" / "Rongke" / "Rongke.exe"

_fail: list[str] = []


def ok(msg: str) -> None:
    print(f"  [PASS] {msg}")


def bad(msg: str) -> None:
    _fail.append(msg)
    print(f"  [FAIL] {msg}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url: str, timeout: float = 10.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    # ★ 2026-11-05：只捕 HTTPError 时，`wait_up()` 的 1.5s 冷启动轮询会撞上
    #   socket 层 TimeoutError / ConnectionResetError，异常直接冲出测试 ⇒
    #   「0 个 FAIL 但整场腰斩」，看起来像产品崩、实际是夹具自己没兜住
    #   「没测到」。没应答是轮询的正常状态，必须降成 (0, 原因)，不是抛。
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, f"{type(e).__name__}: {e}".encode("utf-8", "replace")


def post(url: str, obj: dict, timeout: float = 15.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, method="POST",
        data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:                                  # noqa: BLE001
            return e.code, {}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def wait_up(base: str, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, body = get(base + "/api/build", timeout=1.5)
        if code == 200 and body:
            return True
        time.sleep(0.25)
    return False


def start(exe: Path, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [str(exe), "--serve", "--port", str(port)],
        cwd=str(exe.parent), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:                                      # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=str(DEFAULT_EXE))
    a = ap.parse_args()
    exe = Path(a.exe).resolve()
    if not exe.is_file():
        print(f"找不到 {exe}，先跑 python packaging/build_release.py", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="rk_smoke_"))
    home = tmp / "home"
    data = tmp / "data"
    home.mkdir()
    env = dict(os.environ)
    env.update({
        "RONGKE_DATA": str(data),
        "USERPROFILE": str(home),
        "HOME": str(home),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "0",
    })
    proc = None
    try:
        print("1. --check 只读")
        r = subprocess.run([str(exe), "--check"], env=env,
                           capture_output=True, timeout=90)
        if r.returncode == 0:
            ok("--check 退出码 0")
        else:
            bad(f"--check 退出码 {r.returncode}")
        if not (home / ".rongke").exists():
            ok("--check 没有创建 ~/.rongke")
        else:
            bad("--check 创建了 ~/.rongke —— 不是只读")

        print("2. 全新数据根：静态资产与试用起点")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        proc = start(exe, port, env)
        if not wait_up(base):
            bad("服务没起来（/api/build 没应答）")
            return 1
        ok("服务起来了（/api/build 200）")

        for path, label, least in (("/", "index.html", 20_000),
                                   ("/static/workspace.js", "workspace.js", 100_000),
                                   ("/static/tokens.css", "tokens.css", 1_000),
                                   ("/favicon.ico", "app.ico", 1_000)):
            code, body = get(base + path)
            if code == 200 and len(body) >= least:
                ok(f"{label} 在包里且非空（{len(body):,} 字节）")
            else:
                bad(f"{label} 缺失或过小：HTTP {code} / {len(body)} 字节")

        # ── 能力资产（2026-11-05 补）────────────────────────────────────────
        # 之前冻结版把 presets/*.json 与 caps_ext/*.py 全漏了：界面能开、静态
        # 资产全绿，但一份预设都挂不上、文献检索能力凭空消失 —— 旧判据只量
        # 静态文件，覆盖不到这一层。量「集合包含 + 接口读回」，不量总数，
        # 免得以后加一份预设就假红。
        print("2b. 能力资产随包（预设 JSON + 随库插件 .py）")
        stage = exe.parent
        asset_root = stage / "_internal" / "rongke"      # PyInstaller 6 onedir
        if not (asset_root / "presets").is_dir():
            asset_root = stage / "rongke"                # 兼容旧布局
        pjs = {p.name for p in (asset_root / "presets").glob("*.json")}
        cps = {p.name for p in (asset_root / "caps_ext").glob("*.py")
               if not p.name.startswith("_")}
        for want in ("baseline.json", "lit-search.json", "strict-v2.json"):
            if want in pjs:
                ok(f"冻结版带上了预设 {want}")
            else:
                bad(f"冻结版缺预设 {want}（目录：{asset_root / 'presets'}）")
        for want in ("answer_first.py", "lit_search.py"):
            if want in cps:
                ok(f"冻结版带上了随库能力 {want}")
            else:
                bad(f"冻结版缺随库能力 {want}（目录：{asset_root / 'caps_ext'}）")

        # ── 开源双轨 + 原生出图（2026-11-05 补）──────────────────────────
        # 判据分两层：**产物层**读回（技能文件 / 工具接口），**配置层**只判
        # 安装器脚本有没有把两份许可写进 [Files] —— 后者明确标成配置判据，
        # 不拿「配置在场」冒充「安装包里真有」（本机没有 innoextract/7z 可解包）。
        print("2c. 原生出图能力与双轨许可")
        sk = asset_root / "skills" / "uncertainty-and-units" / "SKILL.md"
        if sk.is_file() and "Apache-2.0" in sk.read_text(encoding="utf-8",
                                                        errors="replace"):
            ok("冻结版带上了自研 Apache-2.0 最小技能")
        else:
            bad(f"冻结版缺自研技能或它不是 Apache-2.0：{sk}")
        code, body = get(base + "/api/tools")
        if code == 200 and b"plot_svg" in body:
            ok("/api/tools 里挂载了原生出图工具 plot_svg")
        else:
            bad(f"/api/tools 没挂 plot_svg（HTTP {code} / {len(body)} B）")
        _root = Path(__file__).resolve().parent.parent
        _lic = _root / "LICENSE"
        _desk = _root / "LICENSE-DESKTOP.txt"
        if _lic.is_file() and "Apache License" in _lic.read_text(encoding="utf-8") \
                and _desk.is_file() and "保留所有权利" in _desk.read_text(encoding="utf-8"):
            ok("仓库根两份许可分别是 Apache-2.0 与专有（双轨没退化成一份）")
        else:
            bad("仓库根的双轨许可缺件或内容不对")
        _iss = (_root / "packaging" / "rongke.iss").read_text(encoding="utf-8",
                                                              errors="replace")
        if all(s in _iss for s in ("LICENSE-DESKTOP.txt", "NOTICE.md", "..\\LICENSE")):
            ok("安装器 [Files] 配齐两份许可与 NOTICE（配置判据，非解包判据）")
        else:
            bad("安装器 [Files] 没配齐双轨许可")

        code, body = get(base + "/api/presets")
        try:
            pstate = json.loads(body.decode("utf-8"))
        except Exception:                                  # noqa: BLE001
            pstate = {}

        def _ids(node):
            out = []
            if isinstance(node, dict):
                if node.get("id"):
                    out.append(str(node["id"]))
                for v in node.values():
                    out += _ids(v)
            elif isinstance(node, list):
                for v in node:
                    out += _ids(v)
            return out

        seen_ids = set(_ids(pstate))
        # 隔离 HOME 下部分预设会因外部技能缺失被标坏 —— 那是另一件事；
        # 这里只判「内核有没有把它们读出来」，不拿健康度冒充资产在不在。
        for want in ("baseline", "lit-search", "strict-v2", "figure", "deck"):
            if want in seen_ids:
                ok(f"/api/presets 读得到预设 {want}")
            else:
                bad(f"/api/presets 读不到预设 {want}（HTTP {code}）")

        code, body = get(base + "/api/license")
        try:
            st = json.loads(body.decode("utf-8"))
        except Exception:                                  # noqa: BLE001
            st = {}
        if st.get("state") == "trial" and 29 <= int(st.get("days_left") or 0) <= 30:
            ok(f"全新数据根是试用态，days_left={st.get('days_left')}")
        else:
            bad(f"试用态不对：{st}")
        mid = str(st.get("machine_id") or "")
        if mid.startswith("RK-M-") and len(mid) == 21:
            ok(f"授权接口带回本机机器码（{mid}）")
        else:
            bad(f"机器码缺失或形状不对：{mid!r}")
        trial = data / "license" / "trial.json"
        if trial.is_file():
            ok("首次启动写了 trial.json")
        else:
            bad("首次启动没写 trial.json")
        stop(proc)
        proc = None

        print("3. 过期：只拦发起任务与追问，不锁只读")
        trial.parent.mkdir(parents=True, exist_ok=True)
        trial.write_text(json.dumps({"first_seen": time.time() - 400 * 86400}),
                         encoding="utf-8")
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        proc = start(exe, port, env)
        if not wait_up(base):
            bad("过期态服务没起来")
            return 1

        code, body = get(base + "/api/license")
        st = json.loads(body.decode("utf-8"))
        if st.get("state") == "expired":
            ok("/api/license 如实报 expired")
        else:
            bad(f"过期态不对：{st}")

        code, j = post(base + "/api/run", {"goal": "x"})
        if code == 403 and j.get("license"):
            ok("过期 ⇒ /api/run 403 + license")
        else:
            bad(f"过期 /api/run 返回 {code} {j}")

        code, j = post(base + "/api/follow", {"id": "nope", "text": "x"})
        if code == 403 and j.get("license"):
            ok("过期 ⇒ /api/follow 403 + license")
        else:
            bad(f"过期 /api/follow 返回 {code} {j}")

        for path in ("/api/status", "/api/runs", "/api/tools"):
            code, _ = get(base + path)
            if code == 200:
                ok(f"过期后 {path} 仍 200")
            else:
                bad(f"过期后 {path} 是 {code}")
        stop(proc)
        proc = None
        if trial.is_file():
            ok("停止服务后 trial.json 仍在（没被清掉）")
        else:
            bad("trial.json 被清掉了")
    finally:
        if proc is not None:
            stop(proc)

    print()
    if _fail:
        print(f"✘ 冻结版冒烟失败 {len(_fail)} 项")
        return 1
    print("✔ 冻结版冒烟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
