# -*- coding: utf-8 -*-
"""Tavotto 的非破坏性桌面交接。原版只读，编辑/导出都在独立副本。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_ROOT = ROOT / "vendor" / "Tavotto" / "codex-plugin" / "skills" / "tavotto-figure"
HANDOFF = SKILL_ROOT / "scripts" / "handoff.py"
OUT_EXTS = (".pdf", ".png", ".svg", ".jpg", ".jpeg", ".eps", ".tif", ".tiff")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module():
    spec = importlib.util.spec_from_file_location("_rongke_tavotto_handoff", HANDOFF)
    if spec is None or spec.loader is None:
        raise RuntimeError("Tavotto官方交接脚本不可加载")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def diagnose() -> dict:
    if not HANDOFF.is_file():
        return {"available": False, "reason": "Tavotto官方交接脚本缺失", "version": ""}
    try:
        found = _module().find_tavotto()
        version = (ROOT / "vendor" / "Tavotto" / "src" / "tavotto" / "__init__.py").read_text(
            encoding="utf-8").split('__version__ = "', 1)[1].split('"', 1)[0]
        return {"available": bool(found.get("cmd")), "source": found.get("source") or "",
                "version": version, "desktop_only": bool(found.get("desktop") and not found.get("cmd")),
                "reason": "" if found.get("cmd") else "未找到可用的Tavotto命令行；不影响原有绘图与审计"}
    except Exception as exc:
        return {"available": False, "reason": f"Tavotto探测失败：{type(exc).__name__}: {exc}", "version": ""}


def launch(original: Path, script: Path, *, workspace: Path, python: str = "") -> dict:
    """复制脚本和同 stem 图件，再交给官方 handoff；绝不执行原目录脚本。"""
    if original.suffix.lower() not in OUT_EXTS or script.suffix.lower() != ".py":
        raise ValueError("Tavotto需要同目录、同文件名的Matplotlib .py脚本与图件")
    if original.stem != script.stem or original.parent != script.parent:
        raise ValueError("脚本必须与图件同目录且同名（如 figure.py 与 figure.pdf）")
    original, script, workspace = original.resolve(), script.resolve(), workspace.resolve()
    original.relative_to(workspace)
    script.relative_to(workspace)
    before = {str(p): _digest(p) for p in (original, script)}
    for suffix in range(100):
        dest = workspace / "tavotto" / (f"{original.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}" + (f"-{suffix}" if suffix else ""))
        try:
            dest.mkdir(parents=True)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("无法分配独立精修目录；未覆盖任何已有目录")
    for ext in OUT_EXTS:
        src = original.with_suffix(ext)
        if src.is_file() and src.resolve().is_relative_to(workspace):
            shutil.copy2(src, dest / src.name)
    shutil.copy2(script, dest / script.name)
    try:
        env = dict(os.environ, TAVOTTO_NO_TELEMETRY="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.run([sys.executable, str(HANDOFF), str(dest / script.name),
                               "--run", "never", "--python", python or sys.executable],
                              capture_output=True, timeout=120, env=env)
        lines = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
        try:
            payload = json.loads(lines[-1]) if lines else {}
        except json.JSONDecodeError:
            payload = {"ok": False, "error_code": "invalid_handoff_response"}
        if proc.returncode or not payload.get("ok") or payload.get("parameterizable") is not True:
            raise RuntimeError(payload.get("hint") or payload.get("error") or
                               payload.get("error_code") or proc.stderr.decode("utf-8", errors="replace")[-2000:] or
                               f"Tavotto交接失败（退出码{proc.returncode}）")
        if before != {str(p): _digest(p) for p in (original, script)}:
            raise RuntimeError("原始图件或脚本在交接中发生变化；请勿使用该副本，检查原文件")
        payload.update(ok=True, edit_dir=str(dest.relative_to(workspace)).replace("\\", "/"),
                       original_unchanged=True,
                       state="verified_render" if payload.get("verified_success") is True else
                             "render_failed" if payload.get("render_success") is False else "opened_unverified")
        return payload
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tavotto交接超时；原图与脚本未修改") from exc
    except OSError as exc:
        raise RuntimeError(f"Tavotto交接无法启动：{exc}") from exc


def reaudit(*, workdir: Path, original: Path, contract: dict, auditor) -> dict:
    """仅对独立编辑目录里的实际PDF复审；无法找到契约或PDF就不冒充通过。"""
    if original.suffix.lower() != ".pdf" or not original.is_file():
        raise ValueError("复审需要独立编辑目录中实际存在的PDF")
    from .figure import FigureContract
    result = auditor.audit(FigureContract.from_dict(contract), workdir=workdir,
                           source=original.with_suffix(".py").name,
                           pdf=original.name, report_path=str(workdir / "figure-qa-tavotto.json"),
                           extra_report={"integration": "Tavotto独立编辑层；非原图覆盖"})
    return {"ok": result.ok, "verdict": result.verdict,
            "report": "figure-qa-tavotto.json", "warnings": result.warnings}
