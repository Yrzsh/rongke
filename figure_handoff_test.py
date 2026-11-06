# -*- coding: utf-8 -*-
"""Tavotto交接的负控与副本边界。没有本机Tavotto也必须能跑。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from rongke import figure_handoff as fh
from rongke import stages

CHECK = 0

def check(condition, message):
    global CHECK
    CHECK += 1
    assert condition, message
    print(f"[PASS] {CHECK} {message}")

def main():
    check(stages.resolve("tavotto-figure") is not None, "官方Tavotto技能真实可加载")
    check(stages.resolve("create-supervisor") is not None, "Supervisor.skill使用真实技能名")
    assert len(fh.HANDOFF.read_bytes()) > 1000
    old_diag, old_module, old_run = fh.diagnose, fh._module, fh.subprocess.run
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        original = root / "figure.pdf"
        script = root / "figure.py"
        original.write_bytes(b"original pdf")
        script.write_text("def main():\n    pass\n", encoding="utf-8")
        before = (original.read_bytes(), script.read_bytes())
        import hashlib
        check(fh._digest(original) == hashlib.sha256(b"original pdf").hexdigest(), "负控哈希基线真实计算")
        check(fh._digest(original) != hashlib.sha256(b"tampered pdf").hexdigest(), "负控能区分被篡改图件")
        try:
            fh.launch(original, script, workspace=root / "other")
            raise AssertionError("越界交接没有被拒绝")
        except ValueError:
            check(True, "工作区外输入被拒绝")
        fh.diagnose = lambda: {"available": False, "reason": "not installed"}
        fh._module = lambda: type("M", (), {"find_tavotto": lambda: {"cmd": None}})()
        check(fh.diagnose()["available"] is False, "未安装不伪报可用")
        class Proc:
            returncode = 0
            stdout = json.dumps({"ok": True, "parameterizable": True, "verified_success": False,
                                 "project": "copy", "stem": "figure"}).encode()
            stderr = b""
        def fake_run(argv, **kwargs):
            check(argv[argv.index("--run") + 1] == "never", "官方交接不会重跑副本脚本")
            check(Path(argv[2]) != script and Path(argv[2]).parent.parent.name == "tavotto", "传给官方的是独立副本路径")
            return Proc()
        fh.subprocess.run = fake_run
        result = fh.launch(original, script, workspace=root)
        second = fh.launch(original, script, workspace=root)
        check(second["edit_dir"] != result["edit_dir"], "连续交接不会复用或覆盖副本目录")
        check(result["state"] == "opened_unverified", "未确认渲染不被写成成功渲染")
        check(result["original_unchanged"] and before == (original.read_bytes(), script.read_bytes()),
              "原图与原脚本字节未变")
        copied = root / result["edit_dir"] / "figure.pdf"
        check(copied.read_bytes() == original.read_bytes(), "副本初态与原件相同")
        copied.write_bytes(b"edited copy")
        check(original.read_bytes() == before[0], "修改编辑副本不影响原件")
        class Bad(Proc):
            returncode = 3
            stdout = b'{"ok":false,"error":"missing"}'
        fh.subprocess.run = lambda *a, **k: Bad()
        try:
            fh.launch(original, script, workspace=root)
            raise AssertionError("官方失败被伪报成功")
        except RuntimeError:
            check(True, "官方非零退出码如实失败")
    fh.diagnose, fh._module, fh.subprocess.run = old_diag, old_module, old_run
    print(f"figure-handoff: {CHECK} assertions passed")

if __name__ == "__main__":
    main()
