"""把 .pptx 渲染成 PNG 并拼成网格图 —— 让「好不好看」变成看得见的事。

## 为什么需要它（2026-09-21）

用户看完一份**五门全绿**的 13 页 deck，只说了一句「太差了，不能直接上组会」。
而这条链路里所有的判据**全部只读 XML** —— 没有一步能回答"它长什么样"。

> 判据能证明「不越界、字号够、备注齐」，**证明不了「好看」**。
> 要谈好看，先得有一张能看的图。

这也是 `docs/deck-reference/` 那套"参考样例"的底座：标杆 deck 长什么样，
不能只用文字描述 —— 得渲染出来给人看、也给模型看。

## 渲染链路（实测本机可用）

本机**没有** LibreOffice / poppler（`where soffice` / `where pdftoppm` 全空），
但有 Microsoft PowerPoint（`POWERPNT.EXE`）+ `win32com` ⇒ 走 COM：

    Slide.Export("<绝对路径>.png", "PNG", 1600, 900)

## ⚠️ 四条纪律（每条都是血债）

1. **绝对路径**：COM 对相对路径会**静默拒绝**（本项目踩过多次）。
2. **不抢用户的 PowerPoint**：`Dispatch` 会 attach 到已在运行的实例，
   而 `Quit()` 会把用户正在编辑的文稿一起关掉 ⇒ 先探测"本来有没有在跑"，
   **只在原本没跑时才 Quit**，否则只 `Close` 自己的那份。
3. **读回 PNG 头验尺寸**：COM 不报错 ≠ 导出成功。「自称成功」永远不能当证据。
4. **失败要响**：渲染不出来就退回一个明确的错误，**绝不生成假的空图**。

用法：

    python deck_preview.py <deck.pptx> [-o out.png] [--cols 3] [--width 1280]
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path


def _powerpoint_running() -> bool:
    """PowerPoint 现在是不是已经在跑（用户可能正开着别的稿子）。

    保守取值：**探测失败就当作"在跑"** —— 宁可少关一次，也不能误关用户的窗口。
    """
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE"],
                           capture_output=True, timeout=20)
        # ⚠️ **不能** `text=True`：中文 Windows 的 tasklist 输出是 GBK，
        #    默认按 utf-8 解码会在读取线程里抛 UnicodeDecodeError
        #    （本轮就抛了 —— 渲染照常完成，但 stderr 被污染，像是出了问题）。
        txt = (r.stdout or b"").decode("gbk", "replace")
        return "POWERPNT.EXE" in txt
    except Exception:
        return True


def render_pngs(pptx: str | Path, outdir: str | Path,
                width: int = 1600, height: int = 900) -> tuple[list[Path], str]:
    """把每页导出成 PNG。返回 (png 路径列表, 错误信息；无错则空串)。"""
    try:
        import win32com.client  # noqa: F401
    except Exception as e:
        return [], (f"没有 win32com（{type(e).__name__}）⇒ 渲染不可用。"
                    f"请用装了 pywin32 的 3.12 解释器跑本脚本。")

    src = Path(pptx).resolve()
    if not src.is_file():
        return [], f"文件不存在：{src}"
    dst = Path(outdir).resolve()
    dst.mkdir(parents=True, exist_ok=True)
    for old in dst.glob("slide-*.png"):
        old.unlink()          # 只清自己上一轮的产物

    was_running = _powerpoint_running()
    import win32com.client
    app = None
    pres = None
    pngs: list[Path] = []
    try:
        app = win32com.client.Dispatch("PowerPoint.Application")
        # 位置参数（late binding 下命名参数不生效）：
        # Open(FileName, ReadOnly, Untitled, WithWindow)
        pres = app.Presentations.Open(str(src), True, False, False)
        n = int(pres.Slides.Count)
        if n <= 0:
            return [], "这份 deck 一页都没有。"
        for i in range(1, n + 1):
            p = dst / f"slide-{i:02d}.png"
            pres.Slides(i).Export(str(p), "PNG", int(width), int(height))
            pngs.append(p)
    except Exception as e:
        return [], f"COM 渲染失败：{type(e).__name__}: {e}"
    finally:
        try:
            if pres is not None:
                pres.Close()
        except Exception:
            pass
        try:
            # ★ 只在"本来没跑"时才退应用 —— 否则会关掉用户自己的窗口
            if app is not None and not was_running:
                app.Quit()
        except Exception:
            pass

    # ★ 读回 PNG 头验尺寸：COM 没报错，不等于每个文件都真的写出来了
    bad = [p for p in pngs if not p.is_file() or p.stat().st_size < 2000]
    if bad:
        return [], (f"导出的 PNG 有 {len(bad)} 张缺失或过小："
                    f"{[p.name for p in bad[:4]]} ⇒ 渲染不可信。")
    try:
        from PIL import Image
        for p in pngs:
            with Image.open(p) as im:
                if im.size != (int(width), int(height)):
                    return [], (f"{p.name} 尺寸是 {im.size}，"
                                f"期望 {(int(width), int(height))}。")
    except Exception as e:
        return [], f"PNG 校验失败：{type(e).__name__}: {e}"
    return pngs, ""


def make_grid(pngs: list[Path], out: str | Path, cols: int = 3,
              label: str = "") -> str:
    """拼成网格图。返回错误信息（无错则空串）。"""
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        return f"没有 Pillow（{type(e).__name__}）⇒ 拼图不可用。"
    if not pngs:
        return "没有可拼的图。"

    imgs = []
    for p in pngs:
        with Image.open(p) as im:
            imgs.append(im.convert("RGB").copy())
    w, h = imgs[0].size
    cols = max(1, min(int(cols), len(imgs)))
    rows = int(math.ceil(len(imgs) / cols))
    pad = 18
    head = 34 if label else 0
    W = cols * w + (cols + 1) * pad
    H = head + rows * h + (rows + 1) * pad
    canvas = Image.new("RGB", (W, H), (238, 236, 230))
    d = ImageDraw.Draw(canvas)
    if label:
        d.text((pad, 9), label, fill=(40, 36, 30))
    for i, im in enumerate(imgs):
        r, c = divmod(i, cols)
        x = pad + c * (w + pad)
        y = head + pad + r * (h + pad)
        canvas.paste(im, (x, y))
        # 细边框，让"页"边界看得出来
        d.rectangle([x - 1, y - 1, x + w, y + h], outline=(150, 143, 130))
    outp = Path(out).resolve()
    outp.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(outp)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="把 pptx 渲染成网格预览图")
    ap.add_argument("pptx")
    ap.add_argument("-o", "--out", default="", help="输出的网格图路径")
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--label", default="")
    ap.add_argument("--pngdir", default="", help="PNG 落盘目录（默认放 pptx 同级 _preview/）")
    args = ap.parse_args()

    src = Path(args.pptx).resolve()
    pngdir = Path(args.pngdir).resolve() if args.pngdir else (src.parent / "_preview")
    pngs, err = render_pngs(src, pngdir, args.width, args.height)
    if err:
        print(f"[渲染失败] {err}")
        return 1
    out = Path(args.out).resolve() if args.out else (src.parent / (src.stem + ".sheet.png"))
    err = make_grid(pngs, out, args.cols, args.label or src.name)
    if err:
        print(f"[拼图失败] {err}")
        return 1
    print(f"[OK] {len(pngs)} 页 → {out}   （逐页 PNG 在 {pngdir}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
