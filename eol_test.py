# -*- coding: utf-8 -*-
"""eol_test.py —— 换行策略（`.gitattributes`）的承重自检。

跑法：
    python eol_test.py            # 全部
    python eol_test.py --json     # 只吐 JSON

## 它守的是什么（2026-09-21 的实测血债）

一次提交要落地 11 个文件，**真实内容是 122 行新增**，而 `git diff --stat` 报的是
**2067 插入 / 358 删除**。逐个量才发现：`tools_test.py` 被整篇写成了 CRLF ——
那 321 行「删除」全是假的，是同一批行把行尾从 LF 翻成 CRLF 之后又「新增」了一遍。
⇒ **diff 开始说谎**，一次真改动差点被行尾噪声淹掉。

根因是**实测**出来的，不是猜的。Windows 上的文本模式写入会把 `\n` 翻成 `\r\n`：

    Path.write_text("x\\ny\\n")              ⇒ b'x\\r\\ny\\r\\n'   ✘
    Path.write_text(..., newline="\\n")      ⇒ b'x\\ny\\n'        ✔
    Path.write_bytes(...)                    ⇒ b'x\\ny\\n'        ✔

本仓代码里 `write_text(...)` 有十几处（审计 JSON、布局清单、台账、测试夹具），
所以这不是一次性事故，而是**结构性**的：只要有一处用文本模式写库内的文件，
行尾就会随工具漂移。

## 为什么是 `.gitattributes`，而不是"以后记得用二进制写"

"记得"不是机制。`.gitattributes` 把归一压到 git 层：**CRLF 一旦进库就自动变 LF**，
同一份内容不可能以两种字节形态并存于历史。代价是需要一个守卫 ——
因为 `.gitattributes` 自己也可能被改掉、写错、被更靠后的规则遮住，
而**这种失效是静默的**（G 型：机制在，但不再生效）。

⇒ 本文件不检查"我写了规则"，它检查"规则**生效**"。
   判据取自 `git check-attr` 与 `git hash-object --path`，即 **git 自己的答案**，
   不是我对文档的理解。

## 三档策略（每一档都有断言，含负控）

    * text=auto eol=lf              默认：一切文本 LF 入库
    vendor/** -text -eol            第三方钉版本副本：字节一个都不许动
                                    （自带 SHA256SUMS，`sha256sum -c` 现为 21/21）
    figures/*.pdf|png|tiff          二进制产物
    figures/*.csv|json|svg          生成物：换行由生成器决定
                                    （CSV 依 RFC 4180 显式写 CRLF）

⚠️ `eol` 是**独立于** `text` 的属性。只写 `-text` 时 `eol` 仍可能从 `*` 继承下来，
所以要豁免就两个一起关掉（`-text -eol`）—— 这条是本轮实测发现的，不是文档读来的。

`git` 不可用（或不在仓库里）⇒ 退 **3 = NOT REPORTABLE**，不许报"通过"：
**「没查」≠「查出来是 0」**。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _tkit as T                                                    # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
_QUIET = False
_TMP = T.fresh_dir(ROOT / "runs" / "_regress" / "_eol_test", "eol-test")

# 判据里用到的三类样本路径，每一类都有它代表的一"档"
DEFAULT_PATHS = ["README.md", "rongke/tools.py", "rongke/web/index.html"]
GENERATED_PATHS = ["figures/fig_conversion.layout.json",
                   "figures/fig_conversion.svg",
                   "figures/conversion_source_data.csv"]
EXEMPT_PATHS = ["vendor/ppt-mcp/server.py", "vendor/ppt-mcp/SHA256SUMS",
                "figures/fig_conversion.pdf", "figures/fig_conversion.png"]


def check(label: str, got, want=True, detail: str = "") -> None:
    ok = (got == want)
    (PASS if ok else FAIL).append(label)
    if not _QUIET:
        if ok:
            print(f"  ✔ {label}")
        else:
            print(f"  ✘ {label}\n        期望 {want!r}，实际 {got!r}"
                  + (f"\n        {detail}" if detail else ""))


def section(title: str) -> None:
    if not _QUIET:
        print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def git(*args: str, cwd: Path | None = None) -> str:
    """只取 stdout。rc 不在这里判 —— 有些判据（如 diff --no-index）本来就会退 1。

    `cwd` 默认为仓库根，而且它是个**承重参数**：实测（见 6.2）只要 cwd 落在
    仓库内，`git diff --no-index` 也会拿本仓的 `.gitattributes` 去归一 ——
    连仓库**外**的两个文件都会被归一成"无差异"。所以想复现"修复前"的世界，
    必须换一个 cwd 跑，光把文件放到仓库外是不够的。
    """
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(cwd or ROOT)).stdout


def git_ok() -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, cwd=str(ROOT))
        return r.returncode == 0 and r.stdout.strip() == "true"
    except OSError:
        return False


def numstat(args: list[str], cfg: tuple[str, ...] = (),
            cwd: Path | None = None) -> tuple[int, int]:
    """`--numstat` 的空输出 = 无差异 ⇒ (0, 0)。**别把"没有那一行"读成"有改动"**
    （本轮真栽过：awk 等到一个 0/0 行，结果 git 什么都不打印，于是把"纯行尾"误判成
    "有真内容变化"）。

    `cfg` 是子命令**之前**的全局开关（如 `-c core.autocrlf=false`）——
    它不能塞进 `args`，否则会落到 `diff` 后面被当成路径。
    """
    out = git(*cfg, "diff", *args, cwd=cwd).strip()
    if not out:
        return (0, 0)
    first = out.splitlines()[0]
    a, b, *_ = first.split("\t")
    return (int(a), int(b))


def attrs_of(paths: list[str]) -> dict[str, tuple[str, str]]:
    """问 git 自己要属性值（不采信"我写了规则"）。"""
    out = git("check-attr", "text", "eol", "--", *paths)
    got: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        path, attr, value = line.split(": ", 2)
        got.setdefault(path, {})[attr] = value
    return {p: (d.get("text", "?"), d.get("eol", "?")) for p, d in got.items()}


def blob_as(path: str, f: Path) -> str:
    """把 f 的内容**当作**位于 `path` 来算 blob —— 于是属性/归一全按 path 走。
    这是唯一能直接证明"CRLF 进库会变成 LF"的量法。"""
    return git("hash-object", "--path", path, str(f)).strip()


def crlf_in(paths: list[Path]) -> list[str]:
    """CRLF 探测器：返回带 CRLF 的文本文件。二进制（前 8000 字节含 NUL）跳过。"""
    hit: list[str] = []
    for p in paths:
        if not p.exists() or not p.is_file():
            continue
        b = p.read_bytes()
        if b"\x00" in b[:8000]:
            continue
        if b"\r\n" in b:
            hit.append(str(p))
    return hit


def main(argv=None) -> int:
    global _QUIET
    argv = list(sys.argv[1:] if argv is None else argv)
    _QUIET = "--json" in argv

    # ── 0 · 前置 ────────────────────────────────────────────
    section("0 前置：这个判据现在量得出来吗")
    if not git_ok():
        print("[DONE] NOT REPORTABLE：git 不可用或当前目录不在仓库里")
        return 3
    check("0.1 在 git 仓库里且能读到 HEAD", len(git("rev-parse", "HEAD").strip()) >= 7)

    # ── 1 · 策略在位 ────────────────────────────────────────
    section("1 策略在位")
    ga = ROOT / ".gitattributes"
    check("1.1 `.gitattributes` 存在", ga.exists())
    raw = ga.read_bytes() if ga.exists() else b""
    text = raw.decode("utf-8", "replace")
    lines = [ln.strip() for ln in text.splitlines()]

    check("1.2 默认档声明了 `* text=auto eol=lf`", "* text=auto eol=lf" in lines)
    check("1.3 豁免档对 vendor 用了 `-text -eol`（两个都要关）",
          "vendor/** -text -eol" in lines)
    check("1.4 生成物档声明为 `text eol=crlf`",
          all(x in lines for x in ("figures/*.csv  text eol=crlf",
                                   "figures/*.json text eol=crlf",
                                   "figures/*.svg  text eol=crlf")))
    check("1.5 ★ .gitattributes 自己是 LF（策略先适用于自己）",
          b"\r" not in raw, True, f"CRLF={raw.count(bytes([13, 10]))}")
    # 行尾注释会让整条规则**静默失效** —— 本项目在 .gitignore 上真踩过
    inline = [ln for ln in text.splitlines()
              if ln.strip() and not ln.lstrip().startswith("#") and "#" in ln]
    check("1.6 ★ 没有行尾注释（规则必须独立成行）", inline, [], str(inline))

    # ── 2 · 规则真的生效 ────────────────────────────────────
    section("2 规则真的生效（问 git，不采信「我写了」）")
    a = attrs_of(DEFAULT_PATHS + GENERATED_PATHS + EXEMPT_PATHS)
    for p in DEFAULT_PATHS:
        check(f"2.{DEFAULT_PATHS.index(p) + 1} 默认档 {p} ⇒ (auto, lf)",
              a.get(p), ("auto", "lf"))
    for p in GENERATED_PATHS:
        check(f"2.{GENERATED_PATHS.index(p) + 4} 生成物档 {p} ⇒ (set, crlf)",
              a.get(p), ("set", "crlf"))
    for p in EXEMPT_PATHS:
        check(f"2.{EXEMPT_PATHS.index(p) + 7} 豁免档 {p} ⇒ (unset, unset)",
              a.get(p), ("unset", "unset"))

    # ── 3 · ★ 归一承诺（正控 + 负控）─────────────────────────
    section("3 ★ 归一承诺：CRLF 进库后与 LF 同源（含负控）")
    lf = _TMP / "lf.txt"
    crlf = _TMP / "crlf.txt"
    lf.write_bytes(b"a\nb\nc\n")
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    check("3.0（夹具自证）两份夹具确实只在行尾不同",
          (len(lf.read_bytes()), len(crlf.read_bytes())), (6, 9))

    for i, p in enumerate(DEFAULT_PATHS + GENERATED_PATHS):
        check(f"3.{i + 1} {p} 的路径下，LF 与 CRLF 归一成**同一个 blob**"
              f"（⇒ 行尾造不出假差异）",
              blob_as(p, lf) == blob_as(p, crlf) and len(blob_as(p, lf)) == 40,
              True, f"{blob_as(p, lf)[:12]} vs {blob_as(p, crlf)[:12]}")
    for i, p in enumerate(EXEMPT_PATHS):
        check(f"3.{i + 7} ★ 负控：豁免档 {p} 下两者**不同源**（字节真不许动）",
              blob_as(p, lf) != blob_as(p, crlf), True,
              f"{blob_as(p, lf)[:12]} vs {blob_as(p, crlf)[:12]}")

    # ── 4 · 工作区没漂移 ────────────────────────────────────
    section("4 工作区没漂移：已跟踪源码/文档不含 CRLF")
    tracked = [x for x in git("ls-files").splitlines() if x.strip()]
    scan = [p for p in tracked
            if p.endswith((".py", ".md", ".html")) and not p.startswith("vendor/")]
    check("4.1 确实扫到了一批文件（不是空扫 ⇒ 空扫会假装全绿）",
          len(scan) >= 20, True, f"扫了 {len(scan)} 个")
    offenders = crlf_in([ROOT / p for p in scan])
    check("4.2 ★ 其中没有一个是 CRLF",
          offenders, [], "; ".join(x.replace(str(ROOT), ".") for x in offenders[:8]))

    # ── 5 · ★ 承重自检：4.2 的探测器真的会响吗 ───────────────
    section("5 ★ 承重自检：把 4.2 的探测器单独拎出来喂已知答案")
    probe = _TMP / "probe_crlf.py"
    probe.write_bytes(b"x = 1\r\ny = 2\r\n")
    check("5.1 正控：喂一个 CRLF 的 .py ⇒ 必须报出来",
          crlf_in([probe]), [str(probe)])
    check("5.2 负控：同一个内容换成 LF ⇒ 必须不报",
          crlf_in([lf]), [])
    binary = _TMP / "b.bin"
    binary.write_bytes(b"\x00\x01\r\n\x02")
    check("5.3 负控：二进制（含 NUL）即使有 CRLF 也不许误报",
          crlf_in([binary]), [])
    missing = _TMP / "根本没这个文件.py"
    check("5.4 负控：不存在的路径不许炸（跳过即可）",
          crlf_in([missing]), [])

    # ── 6 · 判据本身：行尾噪声怎么量才不会被骗 ───────────────
    section("6 ★ 行尾噪声的量法（本轮在上面错过两次，钉住它）")

    # 因果链的第一环：本仓**故意**关了 autocrlf（快照按字节算 md5）。
    # 副作用是 git 不再自动兜底归一 —— 这正是行尾噪声当初能进 diff、
    # 把 122 行真改动读成 443/321 的**前提**。
    check("6.1 本仓 core.autocrlf=false（为了不动字节级 md5 快照）",
          git("config", "--local", "--get", "core.autocrlf").strip(), "false")

    # 仓库**外**的夹具：那里没有任何 .gitattributes，最接近"修复前"的世界。
    # 落在 %TEMP% 下由系统自清 —— 不占删除额度，也不污染仓库。
    outside = Path(tempfile.mkdtemp(prefix="rongke-eol-"))
    olf, ocrlf = outside / "lf.txt", outside / "crlf.txt"
    olf.write_bytes(b"a\nb\nc\n")
    ocrlf.write_bytes(b"a\r\nb\r\nc\r\n")

    # ⚠️ 两个条件缺一不可：
    #   ① 显式关掉 autocrlf —— 仓库外会继承 Git for Windows 的 system 默认（true），
    #      那时 git 自己就归一了，演示变成**假阴性**（本轮真栽过这一次）；
    #   ② cwd 必须在仓库**外** —— 实测只要 cwd 在仓库里，`--no-index` 也会被本仓的
    #      .gitattributes 归一，连仓库外的两个文件都报"无差异"（也栽过一次）。
    NOAUTO = ("-c", "core.autocrlf=false")
    check("6.2 ★ 仓库外 + 无属性 + 关掉 autocrlf（= 修复前的处境）"
          "⇒ 3 行被读成 3/3",
          numstat(["--no-index", "--numstat", str(olf), str(ocrlf)],
                  cfg=NOAUTO, cwd=outside), (3, 3))
    check("6.3 ★ 同一对文件加 --ignore-cr-at-eol ⇒ 0/0 ⇒ 两数相减才是真改动",
          numstat(["--no-index", "--ignore-cr-at-eol", "--numstat",
                   str(olf), str(ocrlf)], cfg=NOAUTO, cwd=outside), (0, 0))
    check("6.4 ★ 同一份文件与自己比 ⇒ 0/0（对照：方法不无中生有）",
          (numstat(["--no-index", "--numstat", str(olf), str(olf)],
                   cfg=NOAUTO, cwd=outside),
           numstat(["--no-index", "--ignore-cr-at-eol", "--numstat",
                    str(olf), str(olf)], cfg=NOAUTO, cwd=outside)), ((0, 0), (0, 0)))
    check("6.5 ★ 仓库内（.gitattributes 生效）⇒ 直读就是 0/0：噪声再也进不了 diff",
          (numstat(["--no-index", "--numstat", str(lf), str(crlf)]),
           numstat(["--no-index", "--ignore-cr-at-eol", "--numstat",
                    str(lf), str(crlf)])), ((0, 0), (0, 0)))

    T.stash(_TMP, "eol-test")

    total = len(PASS) + len(FAIL)
    if _QUIET:
        print(json.dumps({"total": total, "pass": len(PASS), "fail": len(FAIL),
                          "failed": FAIL}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "═" * 62)
        print(f"合计 {total} 项：{len(PASS)} 通过，{len(FAIL)} 失败")
        if FAIL:
            print("失败项：" + "; ".join(FAIL))
        print("[DONE] 换行策略自检全部通过" if not FAIL else "[DONE] 有失败")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
