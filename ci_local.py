"""CI 的本地等价物 —— 在本机真跑一遍 CI 守的那些不变量。

为什么要有它：`.github/workflows/ci.yml` 在这台机器上一行都执行不了
（没有 CI runner）。只写 YAML 而从不运行 = 声明了一个没人验证过的承诺。
这个脚本把 CI 的每条关键断言在本机跑通，让 "CI 绿灯" 这件事
在提交前就已知，而不是等远端红一次才发现。

用法：
    python ci_local.py            # 全跑
    python ci_local.py --quick    # 跳过建 venv（最快的元数据检查）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable
_QUICK = "--quick" in sys.argv

_fail = 0


def step(label: str, args: list[str], cwd: Path | None = None,
         expect_rc: int = 0, timeout: int = 600) -> bool:
    global _fail
    r = subprocess.run(args, cwd=str(cwd or ROOT), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    good = r.returncode == expect_rc
    print(f"{'✔' if good else '✘'} {label}  (rc={r.returncode})")
    if not good:
        _fail += 1
        body = (r.stdout or "") + (r.stderr or "")
        for ln in body.strip().splitlines()[-12:]:
            print("     ", ln[:170])
    return good


def main() -> int:
    print("═══ CI 本地等价：离线测试层 ═══")
    step("eol（行尾纪律）", [PY, "eol_test.py"])
    step("fence（写入围栏）", [PY, "fence_test.py"])
    step("packaging（打包元数据）", [PY, "packaging_test.py"])
    step("pathing（数据根/代码根分离）", [PY, "pathing_test.py"])
    step("changelog（版本号唯一真源 + 空承诺守卫）", [PY, "changelog_test.py"])
    # 账单：纯内存 + 临时目录，连 matplotlib 都不 import，裸环境能过
    step("billing（本地账单）", [PY, "billing_test.py"])
    # 登录门禁：服务端那层裸环境能过；浏览器那层没装 playwright 会自动
    # SKIP 而不是失败 —— CI 机器上不一定有浏览器，那条不该是红的理由。
    step("login（登录门禁）", [PY, "login_test.py"])
    # 授权：Ed25519 自证 + 真 HTTP 闸门，裸环境能过（纯标准库实现，
    # 不依赖 cryptography）。
    step("license（离线授权）", [PY, "license_gate_test.py"])
    # 产品说明对账：裸环境里没装 MCP 服务端，工具数会差 3，
    # 所以带 --skip-counts（那个差距是环境不是缺陷）
    step("product-doc（产品说明对账）",
         [PY, "product_doc_test.py", "--skip-counts"])

    print("\n═══ 构建 wheel ═══")
    wh = ROOT / "runs" / "_an" / "wheelhouse"
    if wh.is_dir():
        shutil.rmtree(wh, ignore_errors=True)
    step("pip wheel --no-deps", [PY, "-m", "pip", "wheel", "--no-deps",
                                 "-q", "-w", str(wh), "."])
    wheels = sorted(wh.glob("rongke-*.whl")) if wh.is_dir() else []
    if not wheels:
        print("✘ 没产出 wheel")
        return 1
    print(f"     wheel: {wheels[-1].name} ({wheels[-1].stat().st_size/1024:.0f} KB)")

    if _QUICK:
        print("\n（--quick：跳过 venv 安装验证）")
        print(f"\n{'✘ 失败 %d 项' % _fail if _fail else '✔ 全部通过'}")
        return 1 if _fail else 0

    print("\n═══ 装进干净 venv 并验证（这才是「别人能不能用」） ═══")
    tmp = Path(tempfile.mkdtemp(prefix="rkci_"))
    try:
        venv = tmp / "v"
        step("建 venv", [PY, "-m", "venv", str(venv)], timeout=300)
        vpy = venv / ("Scripts" if os.name == "nt" else "bin") / (
            "python.exe" if os.name == "nt" else "python")
        step("装 wheel --no-deps", [str(vpy), "-m", "pip", "install",
                                    "--no-deps", "-q", str(wheels[-1])])

        # CI 里那段断言，逐条搬过来
        probe = (
            "import sys\n"
            "from pathlib import Path\n"
            "import rongke\n"
            "assert 'site-packages' in rongke.__file__, rongke.__file__\n"
            "from rongke.config import data_root\n"
            "d = str(data_root())\n"
            "assert 'site-packages' not in d.lower(), f'数据根落进包目录: {d}'\n"
            "import rongke.web as w\n"
            "idx = Path(w.__file__).parent / 'index.html'\n"
            "assert idx.is_file(), f'index.html 缺失: {idx}'\n"
            "assert idx.stat().st_size > 400_000, idx.stat().st_size\n"
            "from rongke.config import Config\n"
            "Config.load()\n"
            "print('OK', rongke.__version__, '| data_root =', d)\n"
        )
        step("安装后不变量（site-packages / 数据根 / 静态资源 / 配置）",
             [str(vpy), "-c", probe], cwd=tmp)

        # 命令入口
        exe = venv / ("Scripts" if os.name == "nt" else "bin") / (
            "rongke.exe" if os.name == "nt" else "rongke")
        if exe.is_file():
            step("rongke --help", [str(exe), "--help"], cwd=tmp)
        else:
            print(f"✘ 命令入口没生成: {exe}")
            globals()["_fail"] += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _fail:
        print(f"✘ 失败 {_fail} 项")
        return 1
    print("✔ CI 本地等价全部通过 —— 远端 CI 的关键断言在本机已成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
