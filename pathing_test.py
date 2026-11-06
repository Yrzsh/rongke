"""数据根（data_root）行为的判据。

这里守的是**可安装化**的核心不变量：
  · 源码树里 data_root() == project_root()，现有 210 个 runs/ 目录零迁移
  · 装成依赖（无 .git / 无 rongke 兄弟目录）时落到 ~/.rongke/data，不写 site-packages
  · RONGKE_DATA / RONGKE_ROOT 两个环境变量能显式改道

★ 这些判据必须能在**不去真的 pip install** 的情况下验证 —— 用环境变量 +
  伪造目录结构把两种情况都摆出来，比装一遍快、也更精确。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

_fail = 0


def ok(msg: str) -> None:
    global _fail
    print(f"✔ {msg}")


def bad(msg: str) -> None:
    global _fail
    _fail += 1
    print(f"✘ {msg}")


def eq(got, want, label: str) -> None:
    if got == want:
        ok(label)
    else:
        bad(f"{label} —— 期望 {want}，实际 {got}")


def with_env(vals: dict):
    """临时改环境变量，返回一个恢复用的上下文管理器。"""
    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in vals}
            for k, v in vals.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *a):
            for k, v in self.saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return False
    return _Ctx()


def test_source_tree_is_unchanged():
    """源码树里两者必须是同一个目录 —— 老用户零迁移。"""
    from rongke.config import data_root, project_root
    with with_env({"RONGKE_DATA": None, "RONGKE_ROOT": None}):
        pr, dr = project_root(), data_root()
    eq(pr, ROOT, "project_root() 指向仓库根")
    eq(dr, pr, "源码树里 data_root() == project_root()（零迁移）")
    # 正面证据：仓库里真有 .git 与 rongke/，判据踩的是真条件
    if (ROOT / ".git").exists() and (ROOT / "rongke").is_dir():
        ok("判据前提成立：仓库根下 .git 与 rongke/ 都在")
    else:
        bad("判据前提不成立：仓库根下应有 .git 与 rongke/")


def test_installed_layout_goes_to_home():
    """模拟装成 site-packages：没有 .git，应落到 ~/.rongke/data。"""
    from rongke.config import data_root
    with tempfile.TemporaryDirectory() as td:
        fake_pkg_parent = Path(td) / "site-packages"
        (fake_pkg_parent / "rongke").mkdir(parents=True)
        # 伪造"代码位置"：RONGKE_ROOT 指向一个没有 .git 的目录
        with with_env({"RONGKE_DATA": None, "RONGKE_ROOT": str(fake_pkg_parent)}):
            from rongke.config import project_root as pr2
            eq(pr2(), fake_pkg_parent.resolve(), "RONGKE_ROOT 生效")
            got = data_root()
            eq(got, Path.home() / ".rongke" / "data",
               "无 .git 的目录 ⇒ 数据落 ~/.rongke/data（不写 site-packages）")
            if str(got).startswith(str(fake_pkg_parent)):
                bad("数据根落进了包目录 —— 这正是要防的")
            else:
                ok("数据根不在包目录里")


def test_env_overrides():
    """两个环境变量都能显式改道。"""
    from rongke.config import data_root, project_root
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td) / "dataA", Path(td) / "rootB"
        with with_env({"RONGKE_DATA": str(a)}):
            eq(data_root(), a.resolve(), "RONGKE_DATA 改数据根")
        with with_env({"RONGKE_DATA": None, "RONGKE_ROOT": str(b)}):
            eq(project_root(), b.resolve(), "RONGKE_ROOT 改代码根")


def test_user_dirs_all_follow_data_root():
    """topics / memory / runs 必须全部跟着 data_root 走。

    ★ 这是**同族检查**：以前这里是 17 处散落的 project_root() 调用，
      改一处漏一处就会在装成依赖后把用户数据写进库目录。
    """
    from rongke.config import data_root
    from rongke.memory import default_root as mem_root
    from rongke.topic import topics_root
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "mydata"
        with with_env({"RONGKE_DATA": str(base), "RONGKE_MEMORY_DIR": None}):
            eq(topics_root(), base.resolve() / "topics", "topics 跟 data_root")
            eq(mem_root(None), base.resolve() / "memory", "memory 跟 data_root")


def test_config_write_goes_to_data_root():
    """写 config.json 的落点必须是 data_root —— 绝不能是包目录。"""
    import inspect
    from rongke import config as C
    src = inspect.getsource(C.save_provider_overrides)
    # 判据打在这行**赋值本身**上，不只看函数里有没有出现 data_root
    if "root = root or data_root()" in src:
        ok("save_provider_overrides 用 data_root 作写落点")
    else:
        bad("save_provider_overrides 没用 data_root 作写落点")
    if "root = root or project_root()" in src:
        bad("save_provider_overrides 仍在用 project_root 写配置")
    else:
        ok("save_provider_overrides 不再用 project_root 写配置")


def test_no_write_path_uses_project_root():
    """全仓扫描：**写**用户数据的路径不许用 project_root()。

    只挑已知会写盘的位置做白名单检查，不能靠"出现即红"
    （project_root 还被预设目录等**只读**用途合法使用）。

    ★ 必须跳过注释与 docstring 行：`memory.py` 的文档里就写着
      「此前 `Memory(project_root() / "memory")` 散在 5 处」——
      那是**历史说明**，不是真代码。直接按行匹配会假红（这里已经踩过一次）。
    """
    import io
    import re
    import tokenize
    hits = []
    for f in (ROOT / "rongke").rglob("*.py"):
        txt = f.read_text(encoding="utf-8", errors="replace")
        # 用 tokenize 拿真实代码行号，注释/字符串里的内容一律不算
        code_lines: set[int] = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(txt).readline):
                if tok.type in (tokenize.COMMENT,):
                    continue
                # 记录每个 token 覆盖的行；STRING 整段丢弃（docstring/字面量）
                if tok.type == tokenize.STRING:
                    continue
                for ln in range(tok.start[0], tok.end[0] + 1):
                    code_lines.add(ln)
        except Exception:
            continue
        for i, line in enumerate(txt.splitlines(), start=1):
            if i not in code_lines:
                continue
            if not re.search(r"project_root\(\)", line):
                continue
            if re.search(r'/\s*"(topics|memory|runs|materials)"', line):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    if hits:
        bad("仍有用户数据路径走 project_root()：" + "; ".join(hits))
    else:
        ok("无用户数据路径残留 project_root()（已排除注释与字符串）")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    print("=== [1] 源码树零迁移 ===")
    test_source_tree_is_unchanged()
    print("\n=== [2] 装成依赖后的落点 ===")
    test_installed_layout_goes_to_home()
    print("\n=== [3] 环境变量改道 ===")
    test_env_overrides()
    print("\n=== [4] 同族目录都跟 data_root ===")
    test_user_dirs_all_follow_data_root()
    print("\n=== [5] 写配置的落点 ===")
    test_config_write_goes_to_data_root()
    print("\n=== [6] 写路径残留扫描 ===")
    test_no_write_path_uses_project_root()
    print()
    if _fail:
        print(f"✘ 失败 {_fail} 项")
        sys.exit(1)
    print("✔ 全部通过")
