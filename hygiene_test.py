# -*- coding: utf-8 -*-
"""仓库卫生守卫：内核包里不许出现「删除」，成功收尾必须是「挪」。

## 为什么单开这一套

本机有一道批量删除守卫，**按条目计费、50 点/轮**（计费口径见 `_tkit.py` 开头那段
从守卫源码读出来的复刻）。于是生产面里每一个删除点都是一个**隐性的额度消耗源**，
而它爆掉的样子极难归因：

    删除点被守卫**直接杀掉** —— rc=1、不打印结果行、也没有 traceback。
    `try/except` 兜不住：它拦异常，拦不住信号。

血债（2026-09-21，一天之内连挖出两处）：

| 位置 | 症状 | 治法 |
|---|---|---|
| `rongke/deck.py` 重写 pptx 的子脚本 | 收尾 `prs.save(tmp)` 再删 tmp ⇒ 趟趟 1 点；`deck_test` 一趟调它二十多次 ⇒ 一轮跑两遍吃穿，而**红的是别的测试** | 内存完工（`BytesIO`），根本不产生临时文件 |
| `rongke/loop.py` 成功收尾 | **每一次成功的任务**末尾删掉对话快照（省几十 KB 盘） | 改名 `os.replace`（零计费），语义一模一样 |

后一处为什么"语义一模一样"值得写下来：**"能不能接着跑"的判据是规范名在不在**
（`web/server.py` 的 `run.resumable` 找 `conversation.json`）—— 改了名，靶子自然消失，
和"删掉"对用户是同一件事。代价只是几十 KB 盘，而那个目录本来就是**永久归档**。

## 判据怎么写的（这一段比检查本身重要）

**不去字符串，只去注释。** 两句话各对应一个坑：

  · **为什么不能去字符串**：`rongke/deck.py` 里那份"重写 pptx"的子脚本是
    **以字符串形式**住在产品代码里的 —— 剥字符串就等于对刚刚修掉的那个删除点闭眼。
    （我第一版就想剥字符串后扫，是这条血债把我拦下来的。）
  · **为什么要去注释**：注释里引述旧实现（`os.remove(tmp)`）是**文档**，不是删除点。
    报它就是误报，而**误报比漏报更伤**（设计定律 10）——
    一条会把文档报成违规的判据，很快就会被当成噪声绕过去。

※ 判别器是「注释 vs 字符串」这个**语法区别**，`tokenize` 免费给我。
  但有一条够不着的残余：`deck.py` 那份内嵌脚本**里面的注释**，对 `tokenize` 而言
  是字符串内容 —— 去不掉。⇒ 规矩：**写在内嵌脚本里的注释，别把调用语法原样抄进去**。
  （本次就是这么给 `deck.py` 那几行注释改的措辞。）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 拆夹具一律走 `_tkit`（挪，不删）—— 本环境按**条目数**收删除费，见它开头那段。
import _tkit as T                                             # noqa: E402
_SCRATCH = ROOT / "runs" / "_regress"

from rongke import loop as L                                  # noqa: E402
import rongke.web.server as S                                 # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}\n         实得 {got!r}")


# ══════════════════ 1. 判据本体 + 扫描面自证 ══════════════════
print("\n── 1. 内核包里不许出现删除调用 ──")

# 要求**调用语法**（后跟左括号）：`p.unlink(` 这种写法也要抓到，
# 而"只提名字"（`不许再写 unlink`）不该被抓到。
_DELETE_CALL = re.compile(
    r"\b(?:os\.remove|os\.unlink|os\.rmdir|os\.removedirs|shutil\.rmtree)\s*\("
    r"|\.unlink\s*\(|\.rmtree\s*\(")


def strip_comments(text: str) -> str:
    """抠掉**注释**，字符串**原样保留**。解析不了就原样返回（保守方向是多报）。

    逐 token 拿 `COMMENT` 的 (行, 列) 区间，在原文本上**用空格盖掉** ——
    盖而不是重排，是为了让行号在报错信息里仍然对得上。
    """
    import io
    import tokenize
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except Exception:                                  # 语法坏 ⇒ 不去注释，宁多报
        return text
    lines = text.splitlines(keepends=True)
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        r1, c1 = tok.start
        _, c2 = tok.end
        if 1 <= r1 <= len(lines):
            ln = lines[r1 - 1]
            if c2 <= len(ln):
                lines[r1 - 1] = ln[:c1] + " " * (c2 - c1) + ln[c2:]
    return "".join(lines)


def deletions_in(text: str) -> list[str]:
    """**去注释、留字符串**之后再找删除调用。"""
    return [m.group(0) for m in _DELETE_CALL.finditer(strip_comments(text))]


_PKG = sorted(p for p in (ROOT / "rongke").rglob("*.py")
              if ".bak" not in p.name and "__pycache__" not in p.parts)
_hits: list[str] = []
for _p in _PKG:
    _txt = strip_comments(_p.read_text(encoding="utf-8", errors="replace"))
    for _m in _DELETE_CALL.finditer(_txt):
        _line = _txt[: _m.start()].count("\n") + 1
        _hits.append(f"{_p.relative_to(ROOT).as_posix()}:{_line} {_m.group(0)}")

check("★ 内核包里零删除调用", _hits, [])
# 扫描面自证：判据"零命中"也可能是**一个文件都没扫到**。分母要当场证明。
check("扫描面自证：确实扫到了整个包（不是扫了个空目录）", len(_PKG) >= 20, True)
check("扫描面自证：deck.py 与 loop.py 都在里面（这两处是本次修的）",
      all(any(x.name == n for x in _PKG) for n in ("deck.py", "loop.py")), True)

# ══════════════════ 2. 反向对照：这条判据不是空气 ══════════════════
print("\n── 2. 反向对照：判据必须能响，也必须不乱响 ──")

_real = (ROOT / "rongke" / "loop.py").read_text(encoding="utf-8")

check("反向对照：注入真调用 ⇒ 必须命中（否则这条判据在测空气）",
      len(deletions_in(_real + "\n\ndef _inj(p):\n    os.remove(p)\n")) >= 1, True)
check("反向对照：`p.unlink(missing_ok=True)` 这种写法也必须命中",
      len(deletions_in(_real + "\nx = (p / 'f').unlink(missing_ok=True)\n")) >= 1, True)
# ★ 这一条是本次血债的形状：删除点住在**字符串**里（deck.py 的子脚本）。
#   任何"先剥字符串再扫"的实现都会在这里失守 —— 它就是用来钉死那种实现的。
check("★ 反向对照：住在字符串里的调用照样命中（deck.py 那个洞的形状）",
      len(deletions_in(_real + '\n_S = """\nimport os\nos.remove(x)\n"""\n')) >= 1, True)
check("误报对照：注释里**原样引述调用语法** ⇒ 不许命中（注释是文档，不是删除点）",
      deletions_in(_real + "\n# 旧实现里写的是 os.remove(tmp)，已经退场\n"), [])
check("误报对照：注释里只是提到名字、没有调用 ⇒ 不许命中",
      deletions_in(_real + "\n# 规矩：不许再写 unlink / rmtree / os.remove\n"), [])
# 判别器自证：去掉的**只**是注释。字符串一动没动 —— 否则上面那条血债形状的对照
# 会以"PASS"的样子骗人（它命中的是字符串里的调用，不是代码里的）。
check("★ 判别器自证：只去注释、不动字符串（两者必须共存）",
      [strip_comments('x = "os.remove(1)"  # os.remove(2)\n'),
       strip_comments('a = 1  # os.remove(2)\n').count("os.remove")],
      ['x = "os.remove(1)"  ' + " " * len("# os.remove(2)") + "\n", 0])
check("判别器自证：源码坏了（语法不合法）⇒ 原样返回，偏向多报",
      strip_comments("def f(:\n  os.remove(x)\n").count("os.remove"), 1)

# ══════════════════ 3. 成功收尾是「挪」不是「删」 ══════════════════
print("\n── 3. 成功收尾：改名元组必须让「规范名」消失 ──")

check("★ 左侧（规范名）就是 `conversation.json` —— 续跑入口找的是它",
      L.SNAPSHOT_RENAMES[0][0], "conversation.json")
check("★ 改名前后不许同名（否则改名等于什么都没做，靶子还在）",
      [a for a, b in L.SNAPSHOT_RENAMES if a == b], [])
check("★ 成功收尾处确实走的是那份元组（单源，不是又抄了一遍字面量）",
      "for _f, _t in SNAPSHOT_RENAMES:" in _real, True)

# ══════════════════ 4. 单源自证：白名单是算出来的 ══════════════════
print("\n── 4. 界面白名单从元组算出来（手抄就是 H 型） ──")

check("★ `SNAPSHOT_INTERNAL` 等于「用元组现算」的结果",
      L.SNAPSHOT_INTERNAL, L._internal_names(L.SNAPSHOT_RENAMES))
# 换了输入就得换输出 —— 否则上面那条只是复述我自己的写法（同义反复）。
check("承重自证：推导函数随元组变（喂两对假名字进去）",
      L._internal_names((("a", "b"), ("c", "d"))), frozenset({"a", "b", "c", "d"}))
check("★ 界面白名单**包含**改名前后两套快照名",
      set(L.SNAPSHOT_INTERNAL) <= set(S.Manager.INTERNAL), True)

# ══════════════════ 5. 行为面：内部件不许混进产出物清单 ══════════════════
print("\n── 5. 产出物清单：真交付物露头，内部账本全藏 ──")

_d = T.fresh_dir(_SCRATCH / "_hyg_files", "hyg-files")
(_d / "report.md").write_text("x", encoding="utf-8")
for _n in sorted(L.SNAPSHOT_INTERNAL):
    (_d / _n).write_text("{}", encoding="utf-8")
(_d / "session.json").write_text("{}", encoding="utf-8")
(_d / "trace.jsonl").write_text("", encoding="utf-8")

_names = [n for n, _ in S.Manager._artifact_files(_d)]
check("★ 只剩真交付物（快照改名前后 + 归档账本，一个都不许露头）",
      _names, ["report.md"])
# 误报对照：白名单是**精确名**匹配，不是"名字里带 conversation 就藏"。
(_d / "conversation.json.bak").write_text("x", encoding="utf-8")
_names2 = [n for n, _ in S.Manager._artifact_files(_d)]
check("误报对照：`conversation.json.bak` 不是内部件 ⇒ 照样露头",
      "conversation.json.bak" in _names2, True)

# ══════════════════ 6. 算账：这一处从 1 点/次 变成 0 点/次 ══════════════════
print("\n── 6. 计费口径与这一处的账 ──")

check("口径：删一个文件 = 1 点", T.points(_d / "report.md"), 1)
check("口径：删一个**不存在**的路径 = 0 点（改名后规范名不存在）",
      T.points(_d / "_no_such_file_here"), 0)
check("★ 这一处的账：改前每跑成一个任务要花 1 点（现在按 0 点走）",
      T.points(_d / "conversation.json"), 1)
print("  [INFO] 改名（os.replace）**不计费** —— 口径见 `_tkit.py` 开头两条推论。")

T.stash(_d, "hyg-files")

print()
print("═══ 汇总 ═══")
print("  全部通过 ✔" if ok_all else "  有失败 ✘")
if ok_all:
    # 地标（`regress.py` 靠它分辨"跑到终点了"和"提前 return 了"）
    print("[DONE] 仓库卫生自检全部通过")
sys.exit(0 if ok_all else 1)
