"""打包与版本一致性判据。

这里守的是「别人 pip install 之后能不能用」这件事实，不是装饰：
  · 版本号只有一个真源，CHANGELOG / pyproject / 代码三处不许漂移
  · pyproject 的依赖声明必须与代码的实际 import 面一致
  · 用户数据目录绝不被打进 wheel

★ 判据不靠"文件里出现某个字符串"就算过 —— 每条都要打在**赋值/字段本身**上，
  并且都有对应的反例检查（负控见 runs/_an/negctl_packaging.py）。
"""
from __future__ import annotations

import re

try:
    import tomllib
except ModuleNotFoundError:                      # Python 3.10
    import tomli as tomllib

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
        ok(f"{label}（{got}）")
    else:
        bad(f"{label} —— 期望 {want}，实际 {got}")


def _code_version() -> str:
    """从源码里取 __version__（不 import，避免依赖副作用）。"""
    txt = (ROOT / "rongke" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', txt, re.M)
    return m.group(1) if m else ""


def test_version_single_source():
    """版本号只能有一个真源：rongke/__init__.py。"""
    v = _code_version()
    if not v:
        bad("rongke/__init__.py 里找不到 __version__")
        return
    ok(f"代码里的版本 = {v}")

    # pyproject 必须是 dynamic 而不是写死
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dyn = py.get("project", {}).get("dynamic") or []
    if "version" in dyn:
        ok("pyproject 用 dynamic version（不写死第二份）")
    else:
        bad("pyproject 没把 version 声明为 dynamic —— 会出现第二份版本号")

    attr = (py.get("tool", {}).get("setuptools", {})
              .get("dynamic", {}).get("version", {}).get("attr", ""))
    eq(attr, "rongke.__version__", "pyproject 的版本取处")

    # CHANGELOG 顶部若有已发布版本号，必须与代码一致
    cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    head = re.search(r"^##\s*\[(\d+\.\d+\.\d+)\]", cl, re.M)
    if head:
        eq(head.group(1), v, "CHANGELOG 顶部版本 == 代码版本")
    else:
        ok("CHANGELOG 顶部是「未发布」段（无需对齐已发布版本号）")

    # 反例检查：不许别处再写一个 __version__
    others = []
    for f in (ROOT / "rongke").rglob("*.py"):
        if f.name == "__init__.py":
            continue
        if re.search(r'^__version__\s*=', f.read_text(encoding="utf-8", errors="replace"), re.M):
            others.append(str(f.relative_to(ROOT)))
    if others:
        bad("这些文件里也定义了 __version__：" + ", ".join(others))
    else:
        ok("除 __init__.py 外没有第二处 __version__")


def test_dependencies_match_reality():
    """声明的核心依赖必须与代码的真实 import 面一致。

    荣珂的核心依赖是**空的**（模型调用走 urllib、Web 走 http.server）。
    如果哪天有人在核心路径里 import 了第三方，这条会红，逼他要么
    改成可选、要么明确加进 dependencies。
    """
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = py.get("project", {}).get("dependencies") or []
    eq(deps, [], "核心 dependencies 为空（零硬依赖是设计决定）")

    opt = py.get("project", {}).get("optional-dependencies") or {}
    for group in ("figure", "pdf", "com", "dev"):
        if group in opt:
            ok(f"可选依赖组 [{group}] 存在：{', '.join(opt[group]) or '(空)'}")
        else:
            bad(f"缺可选依赖组 [{group}]")

    # matplotlib/pandas 必须在**可选**里，不能进核心
    joined = " ".join(deps).lower()
    for mod in ("matplotlib", "pandas"):
        if mod in joined:
            bad(f"{mod} 被写进核心依赖 —— 它会强迫所有用户装绘图栈")
        else:
            ok(f"{mod} 不在核心依赖里（正确：它是子进程的依赖）")

    fig = " ".join(opt.get("figure") or []).lower()
    for mod in ("matplotlib", "pandas"):
        if mod in fig:
            ok(f"{mod} 在 [figure] 可选组里")
        else:
            bad(f"{mod} 没在 [figure] 可选组里 —— 选装的人拿不到")


def test_packaging_excludes_user_data():
    """用户数据目录绝不能出现在打包清单里。"""
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkgs = py.get("tool", {}).get("setuptools", {}).get("packages") or []
    eq(pkgs, ["rongke", "rongke.web"], "只打 rongke 与 rongke.web 两个包")
    for p in pkgs:
        if p in ("topics", "memory", "runs", "materials", "evidence"):
            bad(f"用户数据目录被打进包：{p}")
    ok("打包清单里没有用户数据目录")

    data = (py.get("tool", {}).get("setuptools", {})
              .get("package-data") or {})
    web = data.get("rongke.web") or []
    if any(x.endswith("index.html") for x in web):
        ok("index.html 在 package-data 里（否则装完打不开界面）")
    else:
        bad("index.html 没进 package-data —— 装完界面会是空白")
    for pattern in ("static/*.css", "static/*.js"):
        eq(pattern in web, True, f"{pattern} 随包发布（拆分后的页面依赖）")
    from html.parser import HTMLParser
    class Assets(HTMLParser):
        def __init__(self): super().__init__(); self.paths = []
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'script' and a.get('src', '').startswith('/static/'):
                self.paths.append(a['src'])
            if tag == 'link' and a.get('href', '').startswith('/static/'):
                self.paths.append(a['href'])
    parser = Assets(); parser.feed((ROOT / 'rongke/web/index.html').read_text(encoding='utf-8'))
    for rel in parser.paths:
        target = ROOT / 'rongke/web' / rel.lstrip('/')
        eq(target.is_file() and target.stat().st_size > 0, True, f"静态资产存在且非空：{rel}")


def test_entry_point_exists():
    """console_scripts 指向的函数必须真的存在。"""
    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = py.get("project", {}).get("scripts") or {}
    if "rongke" not in scripts:
        bad("缺 rongke 命令入口")
        return
    target = scripts["rongke"]
    eq(target, "rongke.cli:main", "rongke 命令指向 cli:main")
    mod, _, fn = target.partition(":")
    src = (ROOT / mod.replace(".", "/")).with_suffix(".py").read_text(encoding="utf-8")
    if re.search(rf"^def {fn}\(", src, re.M):
        ok(f"{mod}.{fn} 真的定义了")
    else:
        bad(f"{mod} 里没有 def {fn}( —— 命令行装完点了会崩")


def test_license_present_and_wired():
    """双轨许可：框架 Apache-2.0 + 桌面软件专有，两份都要真实在场且互不越界。

    ★ 这条判据改过一次（2026-11-05）。原版断言 LICENSE 里必须出现
      「保留所有权利」，把「专有」钉死在**框架源码**上 —— 与用户决定的
      「框架开源 + 成品软件专有」正好相反。所以现在量的是**边界是否清楚**，
      而不是某一种许可是否存在：
        · LICENSE 必须是 Apache-2.0 全文（不是只写个名字）；
        · LICENSE-DESKTOP.txt 必须单独存在、且明写它只管 exe；
        · 两份不许退化成同一份，也不许把专有条款混进框架 LICENSE；
        · wheel 的 license-files 两份都要在，只发 Apache 会让 exe 被误认开源。
    """
    lic = ROOT / "LICENSE"
    desk = ROOT / "LICENSE-DESKTOP.txt"
    if not lic.is_file():
        bad("缺 LICENSE 文件")
        return
    txt = lic.read_text(encoding="utf-8")
    ok(f"LICENSE 存在（{len(txt)} 字符）")

    # 正控：必须是**全文**，不是「本项目的许可为 Apache-2.0」这种一句话。
    for needle, label in (
            ("Apache License", "Apache License 抬头"),
            ("Version 2.0, January 2004", "版本号与日期"),
            ("TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
             "条款正文完整（不是只写了名字）")):
        if needle in txt:
            ok(f"LICENSE 含 {label}")
        else:
            bad(f"LICENSE 缺 {label} —— 不是可执行的 Apache-2.0 全文")
    # 负控：专有条款不许混回框架许可。
    if "保留所有权利" in txt or "All rights reserved" in txt:
        bad("框架的 LICENSE 里出现「保留所有权利」—— 双轨被写回成单一专有")
    else:
        ok("LICENSE 里没有专有条款（框架确实开源）")

    if not desk.is_file():
        bad("缺 LICENSE-DESKTOP.txt —— 桌面软件的专有授权没有独立声明")
    else:
        dtxt = desk.read_text(encoding="utf-8")
        ok(f"LICENSE-DESKTOP.txt 存在（{len(dtxt)} 字符）")
        # ★ 量「边界是否写清」，不量**某一个中文词**：措辞一演进就打红，
        #   是与「总数断言」同族的假红。所以用集合 + 三个方向的语义条件，
        #   并配一条负控证明判据不是恒绿。
        _bound_kw = ("不受本文件约束", "不在本许可范围", "不覆盖框架",
                     "不适用于框架", "不受 Apache", "不因")
        _has_boundary = any(k in dtxt for k in _bound_kw)
        _points_binary = "RongkeSetup" in dtxt and ("Rongke.exe" in dtxt or "LICENSE-DESKTOP" in dtxt)
        _points_framework = "Apache-2.0" in dtxt and ("框架" in dtxt or "源码" in dtxt)
        if _has_boundary and _points_binary and _points_framework:
            ok("桌面软件许可点名二进制、点名框架，并写明两者互不覆盖")
        else:
            bad(f"桌面软件许可没划清边界"
                f"（边界={_has_boundary} 点名二进制={_points_binary} "
                f"点名框架={_points_framework}）")
        _negative = dtxt
        for _k in _bound_kw:
            _negative = _negative.replace(_k, "")
        if not any(k in _negative for k in _bound_kw):
            ok("负控：抽掉全部边界表述后同一判据会打红（不是恒绿）")
        else:
            bad("负控失效：边界词仍留在负样本里，判据可能恒绿")
        if "保留所有权利" in dtxt or "All rights reserved" in dtxt:
            ok("桌面软件许可保留所有权利（专有）")
        else:
            bad("桌面软件许可没写保留所有权利")
        if dtxt.strip() == txt.strip():
            bad("两份许可内容相同 —— 双轨退化成一份")
        else:
            ok("两份许可内容不同（双轨没有退化）")

    py = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    proj = py.get("project", {})
    lf = proj.get("license") or {}
    if lf.get("file") == "LICENSE":
        ok("pyproject 的 license 指向 LICENSE 文件（框架那份）")
    else:
        bad(f"pyproject 的 license 没指向 LICENSE（现在是 {lf}）")
    files = py.get("tool", {}).get("setuptools", {}).get("license-files") or []
    for need in ("LICENSE", "LICENSE-DESKTOP.txt"):
        if need in files:
            ok(f"wheel 的 license-files 含 {need}")
        else:
            bad(f"wheel 的 license-files 漏了 {need} —— 发行件里看不到双轨")
    cls = proj.get("classifiers") or []
    if "License :: OSI Approved :: Apache Software License" in cls:
        ok("PyPI 分类器声明为 Apache-2.0")
    else:
        bad("分类器没声明 Apache-2.0 —— 元数据仍会被当成专有")


def test_gitignore_covers_build_artifacts():
    """构建产物与用户数据必须被 .gitignore 挡住。"""
    gi = ROOT / ".gitignore"
    if not gi.is_file():
        bad("缺 .gitignore")
        return
    txt = gi.read_text(encoding="utf-8", errors="replace")
    need = ["__pycache__", "*.egg-info", "build/", "dist/"]
    missing = [p for p in need if p not in txt]
    if missing:
        bad("`.gitignore` 缺：" + ", ".join(missing))
    else:
        ok("`.gitignore` 覆盖构建产物")
    # ★ 账单目录**是隐私，不是构建产物**：里面是账户 id、pbkdf2 的盐与
    #   密码摘要、每一次消耗的流水。原先它一条都没被忽略 —— 那个目录当时
    #   刚好还是 untracked，纯属运气；`git add .` 就会把它提交上去，
    #   推出去之后收不回来。这条判据是红的，不是"提示"。
    if re.search(r"^\s*billing/\s*$", txt, re.M) or \
            "**/billing/account.json" in txt:
        ok("★ `.gitignore` 挡住了账单目录（账户与流水是隐私）")
    else:
        bad("★ `.gitignore` 没挡账单目录 —— `git add .` 会把密码摘要提交上去")
    # 用户数据目录不能在 gitignore 之外还能被误提交 —— 这里只提示，不算红
    for d in ("runs/", "topics/", "memory/"):
        if d not in txt:
            print(f"     （提示）{d} 未在 .gitignore 里，确认是否要提交")


def _placeholder_urls(body: str) -> list[str]:
    """从 `[project.urls]` 的正文里挑出占位链接。

    占位形态：指向站点根（`https://github.com/`）、或 example/localhost
    这类明显不指向真资源的域名。**判据是可注入的纯函数**，负控才能真的
    走一遍它，而不是只测一条同义的正则。
    """
    import re as _re
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        u = _re.search(r"https?://([^/\s\"]+)", s)
        if not u:
            continue
        rest = s.split(u.group(0), 1)[1].strip().rstrip('",')
        host = u.group(1).lower()
        if rest in ("", "/", "/#") or host in ("example.com", "example.org",
                                               "localhost", "127.0.0.1"):
            out.append(s)
    return out


def test_no_placeholder_urls():
    """发行元数据里**不许有占位链接**。

    原先 `[project.urls]` 写着 `Documentation = "https://github.com/"` ——
    那是 GitHub 首页，不是这个项目的任何东西。会被人当真去点，也给了
    "有据可查"的错觉。**没有链接比指向首页的链接更诚实。**
    """
    import re as _re
    txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = _re.search(r"^\[project\.urls\](.*?)(?=^\[|\Z)", txt, _re.S | _re.M)
    if not m:
        ok("[project.urls] 整段没写（诚实：还没对外仓库就先不写）")
    else:
        bads = _placeholder_urls(m.group(1))
        if not bads:
            ok("[project.urls] 里的链接都不指向站点根/占位域名")
        else:
            bad("[project.urls] 有占位链接（指向首页等于假装有文档）："
                + "；".join(bads))
    # 负控：真的把占位样本喂进**同一个函数**，看它响不响
    if _placeholder_urls('Documentation = "https://github.com/"'):
        ok("负控：占位样本被同一个判据函数抓到（不是测空气）")
    else:
        bad("负控失败：占位样本没被抓到 —— 这条判据是空的")
    # 正控：一个像样的链接不该被误伤
    if not _placeholder_urls('Repository = "https://github.com/acme/rongke"'):
        ok("正控：带路径的真实链接不被误伤")
    else:
        bad("正控失败：带路径的链接被误报了")


def test_license_secrets_not_committed():
    """私钥绝不许进仓库，且公钥位置不许被误填成私钥。

    这条不是洁癖。发行私钥一旦进了仓库（尤其推到 GitHub），任何人都能
    用 `rongke --license sign --sk <那个私钥>` 签出有效激活码 ——
    整套授权当场失效，而且**无法撤回**（历史里已经有了）。

    Ed25519 的公钥与私钥都是 32 字节 base64，肉眼完全分不出来，所以
    这里不能靠"看起来像"，只能**拿算法验**：私钥推出来的公钥若等于
    填进去的那个值，说明填进去的就是一把私钥。
    """
    import re as _re
    src = ROOT / "rongke" / "license.py"
    if not src.is_file():
        bad("rongke/license.py 不存在，授权模块没落地")
        return
    text = src.read_text(encoding="utf-8")
    m = _re.search(r'^EMBEDDED_PUBKEY\s*=\s*"([^"]*)"', text, _re.M)
    if not m:
        bad("license.py 里找不到 EMBEDDED_PUBKEY 定义")
        return
    val = m.group(1).strip()
    if not val:
        # 空值是**合法且常见**的状态：这份源码还没发行过，只认已经
        # 发出去的码。不能判失败 —— 否则开源/内测分支永远红。
        ok("EMBEDDED_PUBKEY 为空（这份源码还没有发行能力，属正常状态）")
    else:
        # 拿算法验：它是不是一把私钥？
        # ★ 正确的判法：把填进去的值当**私钥**推一次公钥，看推出来的是不是
        #   另一个有效值。Ed25519 里任何 32 字节都能当私钥推出一个公钥，
        #   所以"推得出"不说明问题 —— 真正的危险是**填的人以为自己在填公钥，
        #   实际填的是私钥**，这无法从值本身判定。
        #
        #   能机械判定的只有一件事：这把密钥**是否已在仓库里以私钥身份出现过**
        #   （`--license sign --sk` 的用法、或 *.sk/*.privkey 文件）。所以这里
        #   改成查"这个值有没有和私钥一起被写进任何文档/脚本"。
        try:
            import sys as _s
            _s.path.insert(0, str(ROOT))
            from rongke.license import _b64d, _b64e, public_key
            raw = _b64d(val)
            if len(raw) != 32:
                bad(f"EMBEDDED_PUBKEY 不是 32 字节（实际 {len(raw)}）")
            else:
                # 真判据。
                #
                # ★ 这里前后错了两次，都留在这里当教训：
                #   第一次查「这个值有没有与『私钥』字样同时出现」—— 填上真
                #     公钥后立刻误报，因为 license.py 自己的注释里就有
                #     「私钥留在你手上」，于是文件必然同时含两样。
                #   第二次改成「把它当私钥推一次公钥，若等于它自己就是私钥」——
                #     这在数学上恒假（Ed25519 私钥推出的公钥绝不等于私钥），
                #     等于一条恒绿判据。负控当场把它抓出来：把私钥填进公钥位，
                #     [8] 照样 ✔。
                #
                # 结论：**光看这个 32 字节值，无法判定它是公钥还是私钥** ——
                # 两者都是 32 字节 base64url。能机械判定的只有一件事：
                # 它与**仓库外那把已知的发行私钥**是不是同一串。
                # 那把私钥按设计不在仓库里，所以这里去仓库外的约定位置找；
                # 找不到就如实说"查不了"，而不是假装查过了。
                derived = _b64e(public_key(raw))
                sk_candidates = [
                    Path.home() / ".rongke-issuer" / "private_key.txt",
                ]
                known_sks: list[str] = []
                for c in sk_candidates:
                    try:
                        body = c.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    # ★ 只认**私钥那一段**。那个文件里同时记了公钥，
                    #   第一版把全部 43 字符串都收进 known_sks，公钥也在里面
                    #   ⇒ 立刻误报"公钥位填了私钥"。
                    #   第二版要求"标签与值同一行"，又漏了 —— 实际文件是
                    #   `私钥（签激活码用）:` 单独一行，值在下一行。
                    #   改成向前看：见到"私钥"标记行，就把紧随其后的第一个
                    #   43 字符串当成私钥。
                    #   （不靠"看起来像"靠标签，是因为公钥私钥长得一模一样。）
                    grab = False
                    for ln in body.splitlines():
                        if "公钥" in ln:
                            grab = False
                            continue
                        if "私钥" in ln:
                            grab = True
                        found = _re.findall(r"[A-Za-z0-9_-]{43}", ln)
                        if grab and found:
                            known_sks.append(found[0])
                            grab = False

                if val in known_sks:
                    bad("★ EMBEDDED_PUBKEY 填的就是发行私钥本身 —— "
                        "任何拿到这个包的人都能签码，整套授权当场失效")
                elif known_sks:
                    # 有已知私钥可对照，且填的不是它 ⇒ 是真公钥。
                    # 再顺手核一下配对关系（公钥应等于 public_key(私钥)）。
                    paired = False
                    for s in known_sks:
                        try:
                            if _b64e(public_key(_b64d(s))) == val:
                                paired = True
                                break
                        except Exception:                   # noqa: BLE001
                            continue
                    if paired:
                        ok(f"EMBEDDED_PUBKEY 是真公钥，且与发行私钥配对"
                           f"（{val[:12]}…）")
                    else:
                        bad(f"★ EMBEDDED_PUBKEY 与发行私钥**不配对**"
                            f"（填的是 {val[:12]}…）—— 发出去的码会全部验不过")
                else:
                    ok(f"EMBEDDED_PUBKEY 是 32 字节（{val[:12]}…）；"
                       f"仓库外没找到发行私钥，无法核对配对关系"
                       f"（用 runs/_an/verify_issuing.py 验）")
        except Exception as e:                          # noqa: BLE001
            bad(f"EMBEDDED_PUBKEY 验不了：{e}")

    # 私钥文件不许出现在仓库里
    suspects = []
    for pat in ("*.sk", "*.privkey", "secrets.json", "*私钥*"):
        suspects.extend(ROOT.glob(pat))
    suspects = [p for p in suspects if p.is_file()]
    if not suspects:
        ok("仓库根目录没有私钥类文件")
    else:
        bad(f"★ 仓库里有疑似私钥文件：{[p.name for p in suspects]}")

    # .gitignore 必须挡住授权目录与私钥
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for want, what in [("license/", "授权目录"), ("*.sk", "私钥文件")]:
        if any(ln.strip() == want for ln in gi.splitlines()):
            ok(f".gitignore 挡住了{what}")
        else:
            bad(f".gitignore 没挡住{what}（{want}）")


def test_no_hardcoded_home_paths():
    """发行源码里不许有**写死的**家目录/用户名。

    两个理由，缺一都不足以说服人：
    ① 这是隐私 —— 包发出去，买家在 `rongke/figure.py` 里读到的是
       某个真人的 Windows 用户名。
    ② 这更是**缺陷** —— 那些路径对买家全都不存在。实测：`figure.py`
       与 `kb_convert.py` 曾经写死三条 `C:\\Users\\<某人>\\...\\python.exe`，
       在别人机器上候选表实际退化成"只剩一个兜底"，白跑三次 stat 才落地；
       `orig.py` 写死 DSH 的安装位置，别人 DSH 装在别处就找不到
       LibreOffice Kit。改成从 `sys.executable` / `%LOCALAPPDATA%` /
       `Path.home()` 推导之后，两边同时解决。

    判据打在**源码字面量**上：运行时 `Path.home()` 算出来的当然含真名，
    那是正确行为；不许有的是把它**打字打进去**。
    """
    import re as _re
    # 家目录的通用形状：C:\Users\<某段>\ 或 /home/<某段>/ 或 /Users/<某段>/
    #
    # ★ 切分边界要收全：第一版只按「反斜杠/引号/空白」切，于是
    #   `C:\Users\…」` 被切成 `…」`（省略号后面跟着中文右引号）——
    #   占位符白名单匹配不上 `…」`，把一句正常的中文注释判成了泄漏。
    #   第二版把中文标点也算边界，结果 `C:\Users\<你>` 被切成 `你`
    #   （`<` 是边界），负控当场抓出来：占位符被当成真用户名。
    #   ⇒ 尖括号必须算**名字的一部分**，让它整段 `<你>` 一起被捕获。
    namechar = r"(?:<[^>]*>|[^\\/\s\"'`,.;:|}{\[\]()<>（）「」『』、，。；：…⋯])+"
    shapes = [
        _re.compile(r"[Cc]:\\+Users\\+(" + namechar + ")"),
        _re.compile(r"/(?:home|Users)/(" + namechar + ")"),
    ]
    # 占位符的**两种**写法：尖括号/中文/英文占位词，以及省略号本身。
    placeholder = _re.compile(
        r"^(<[^>]*>|用户名|user|username|xxx|…|\.\.\.|⋯)$", _re.I)
    # 只查**发行包里的代码**；测试/探针/文档不在发行面内。
    roots = [ROOT / "rongke"]
    offenders: list[str] = []
    for base in roots:
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in (".py", ".html", ".json"):
                continue
            try:
                body = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for ln_no, line in enumerate(body.splitlines(), 1):
                for shape in shapes:
                    for m in shape.finditer(line):
                        seg = m.group(1)
                        if placeholder.match(seg):
                            continue
                        offenders.append(
                            f"{p.relative_to(ROOT).as_posix()}:{ln_no} → {m.group(0)}")
    if offenders:
        bad(f"★ 发行源码里有写死的家目录/用户名（{len(offenders)} 处）：")
        for o in offenders[:8]:
            print(f"        {o}")
        print("    改成从环境推导：sys.executable / %LOCALAPPDATA% / Path.home()。")
    else:
        ok("发行源码里没有写死的家目录/用户名（都是从环境推导的）")

    # 正控：确认这个判据真的会抓 —— 不然"没抓到"可能只是判据坏了
    probe = r'PY = r"C:\Users\someone\AppData\Local\Programs\Python\python.exe"'
    caught = any(sh.search(probe) and not placeholder.match(sh.search(probe).group(1))
                 for sh in shapes)
    if caught:
        ok("正控：一条真实形状的家目录确实会被抓出来（判据不是恒绿）")
    else:
        bad("正控失败：判据抓不到明显写死的家目录，这条守卫是坏的")

    # 负控：占位符不许被误伤
    probe2 = r'# 举例：C:\Users\<你>\.dsh\papers\x.png'
    m2 = shapes[0].search(probe2)
    if m2 and placeholder.match(m2.group(1)):
        ok("负控：占位符 <你> 不被误报")
    else:
        bad("负控失败：占位符被当成真用户名了")


if __name__ == "__main__":
    import sys
    print("=== [1] 版本号单一真源 ===")
    test_version_single_source()
    print("\n=== [2] 依赖声明与真实面一致 ===")
    test_dependencies_match_reality()
    print("\n=== [3] 打包不含用户数据 ===")
    test_packaging_excludes_user_data()
    print("\n=== [4] 命令入口 ===")
    test_entry_point_exists()
    print("\n=== [5] 许可证 ===")
    test_license_present_and_wired()
    print("\n=== [6] 构建产物被忽略 ===")
    test_gitignore_covers_build_artifacts()
    print("\n=== [7] 元数据里没有占位链接 ===")
    test_no_placeholder_urls()
    print("\n=== [8] 私钥不进仓库、公钥位没被误填 ===")
    test_license_secrets_not_committed()
    print("\n=== [9] 发行源码里没有写死的家目录/用户名 ===")
    test_no_hardcoded_home_paths()
    print()
    if _fail:
        print(f"✘ 失败 {_fail} 项")
        sys.exit(1)
    print("✔ 全部通过")
