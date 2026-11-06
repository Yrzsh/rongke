# -*- coding: utf-8 -*-
"""构建 Windows 发行件：PyInstaller onedir → Inno Setup 安装器 → SHA256SUMS。

用法：
    python packaging/build_release.py

前置：
    python -m pip install pyinstaller pywebview
    winget install --id JRSoftware.InnoSetup

版本号只从 rongke/__init__.py 读，不在这里手写第二份。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
WORK = BUILD / "pyinstaller"
STAGE = DIST / "Rongke"
SPEC = ROOT / "packaging" / "rongke.spec"
ISS = ROOT / "packaging" / "rongke.iss"
VERSION_FILE = BUILD / "version_info.txt"


def version() -> str:
    text = (ROOT / "rongke" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise SystemExit("读不到 rongke/__init__.py 里的 __version__")
    return m.group(1)


def write_version_info(v: str) -> None:
    parts = (v.split("-")[0].split(".") + ["0", "0", "0", "0"])[:4]
    nums = ", ".join(str(int(x)) if x.isdigit() else "0" for x in parts)
    BUILD.mkdir(parents=True, exist_ok=True)
    VERSION_FILE.write_text(f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({nums}), prodvers=({nums}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', '荣珂'),
        StringStruct('FileDescription', '荣珂 · 科研 agent 控制台'),
        StringStruct('FileVersion', '{v}'),
        StringStruct('InternalName', 'Rongke'),
        StringStruct('LegalCopyright', '保留所有权利'),
        StringStruct('OriginalFilename', 'Rongke.exe'),
        StringStruct('ProductName', '荣珂'),
        StringStruct('ProductVersion', '{v}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""", encoding="utf-8", newline="\n")


def find_iscc() -> Path:
    cands = []
    env = os.environ.get("ISCC", "").strip()
    if env:
        cands.append(Path(env))
    for base in (os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        if base:
            cands.append(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe")
            cands.append(Path(base) / "Inno Setup 6" / "ISCC.exe")
    w = shutil.which("ISCC")
    if w:
        cands.append(Path(w))
    for p in cands:
        if p.is_file():
            return p
    raise SystemExit(
        "找不到 ISCC.exe。先安装 Inno Setup 6：\n"
        "  winget install --id JRSoftware.InnoSetup -e "
        "--accept-package-agreements --accept-source-agreements\n"
        "或设置环境变量 ISCC 指向 ISCC.exe。")


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT), **kw)


def main() -> int:
    v = version()
    print(f"荣珂 {v} · 构建 Windows 发行件")
    print(f"  源码根 {ROOT}")

    # 只清本次产物；dist/ 里别的文件（例如 wheel）不动。
    if STAGE.exists():
        shutil.rmtree(STAGE)
    if WORK.exists():
        shutil.rmtree(WORK)
    for old in DIST.glob("RongkeSetup-*.exe"):
        old.unlink()
    checksums = DIST / "SHA256SUMS.txt"
    if checksums.exists():
        checksums.unlink()

    write_version_info(v)

    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(WORK), str(SPEC)])
    exe = STAGE / "Rongke.exe"
    if not exe.is_file():
        raise SystemExit(f"PyInstaller 没产出 {exe}")

    # 先把当前版本的 wheel 备好（安装器/Release 里同版本配对）。
    wheel = DIST / f"rongke-{v}-py3-none-any.whl"
    if not wheel.is_file():
        run([sys.executable, "-m", "pip", "wheel", "--no-deps",
             "-w", str(DIST), str(ROOT)])

    iscc = find_iscc()
    chinese = iscc.parent / "Languages" / "ChineseSimplified.isl"
    defines = [f"/DAppVersion={v}", f"/DStageDir={STAGE}"]
    if chinese.is_file():
        defines.append("/DUseChinese=1")
    else:
        print("  [warn] Inno Setup 没有中文语言文件，安装器界面退回英文")
    run([str(iscc), *defines, str(ISS)])

    setup = DIST / f"RongkeSetup-{v}.exe"
    if not setup.is_file():
        raise SystemExit(f"Inno Setup 没产出 {setup}")

    artifacts = [setup] + ([wheel] if wheel.is_file() else [])
    lines = []
    for p in artifacts:
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {p.name}")
        print(f"  {p.name:<40} {h.hexdigest()}")
    checksums.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    print("\n完成：")
    print(f"  安装器  {setup}  ({setup.stat().st_size / 1024 / 1024:.1f} MiB)")
    print(f"  校验和  {checksums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
