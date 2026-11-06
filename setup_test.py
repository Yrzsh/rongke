# -*- coding: utf-8 -*-
"""首次使用自检（`rongke/setupdoctor.py`）单测。

这套件回答一个很具体的问题：**「做 PPT / 画图缺了东西」这件事，到底会不会被看见。**
在此之前它不会被看见 —— 缺 python-pptx ⇒ 模型拿到一句它看不懂的
`ModuleNotFoundError`；缺审计脚本 ⇒ 第 9 条硬门静默判 SKIPPED、交付门永不放行；
`deck.template` 被挪走 ⇒ 出稿时才发现。

  1. 探测原语   —— 有 / 没有 / 解释器不存在 / 结果进缓存
  2. 诚实降级   —— 缺库**不许**被当成装好了（`probe_modules` 的默认值就是"全缺"）
  3. 归因       —— 每条检查都带 `side`（荣珂自己 / 下游解释器）与 `core`
  4. 结论分级   —— 核心缺 ⇒ broken；只有画图侧缺 ⇒ degraded；齐 ⇒ ready
  5. 模板一对   —— .pptx 与槽位表必须**同源**，缺哪个都要单独报出来
  6. 配置写入   —— 白名单外一律拒；写进去**不许丢** config.json 里别人的字段
  7. ★ 修完重测 —— 修完必须读**新配置**重测，且真的变绿
  8. ★ 承重自检 —— 分别拆掉「缺库不许当成功」「白名单」「读新配置」三处，
                  第 2、6、7 节的对应断言必须**当场失守**

沿用 `preset_test.py` 的两条纪律：
  · **每个断言成对出现**（正控 + 负控）；
  · **所有写操作落临时目录** —— 绝不碰真的 `config.json`（那是用户一手资产）
    与真的 `~/.rongke/`。

零依赖：只 import `rongke.setupdoctor` / `config` / `deck`，不碰 `loop`、不碰
workbench 桥、不联网、不调模型。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from rongke import setupdoctor as S                     # noqa: E402
from rongke.config import Config                        # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def item(rep, key):
    for it in rep["items"]:
        if it["key"] == key:
            return it
    return None


# ══════════════════════════════════════════════════════════════
print("[1] 探测原语：这个解释器里有没有那个库")
# ══════════════════════════════════════════════════════════════
S._clear_cache()
good, why = S.probe_modules(sys.executable, ("json",))
check("标准库 json 在同一进程的解释器里 ⇒ 在", good, True)
check("  且没有'缺'的说明", why, "")

good, why = S.probe_modules(sys.executable, ("json", "definitely_not_a_module_xyz"))
check("★ 混进一个不存在的模块 ⇒ 判「不在」（不是「有一个在就算」）", good, False)
check("  且说清缺的是哪一个", "definitely_not_a_module_xyz" in why, True)

check("解释器路径不存在 ⇒ 判不在（负控：不许起子进程瞎猜）",
      S.probe_modules(r"Z:\no\such\python.exe", ("json",))[0], False)
check("空路径 ⇒ 判不在", S.probe_modules("", ("json",))[0], False)

key = sys.executable + "|json"
check("探测结果进了缓存（同一条问第二次不再起子进程）", key in S._PROBE_CACHE, True)
S._clear_cache()
check("_clear_cache 之后缓存空（修完配置要靠它读新结论）", len(S._PROBE_CACHE), 0)

# ── [1b] Origin COM 探一次就够：它每次会**拉起一个隐藏的 Origin 实例** ──
# 动机（W30 实测）：这台机器 Origin 2019b 装着，`Dispatch` 冷 **9.2 秒**
# （全量回归在跑、机器忙时实测 37.6 秒），同进程第二遍 4 毫秒。
# `/api/setup` 是「首次配置」页每次打开都打的接口，原先每次都重付这一笔 ——
# 整页热加载实测 25.8 秒，用户看到的是"点开要等半分钟"。
# 判据打在**调用次数**上，不打在耗时上：耗时随机器变，次数是契约。
print("\n[1b] ★ Origin COM：缓存要省掉重复探测，但不能把「装好了」永远挡在外面")
_real_probe = S._origin_com_probe
_calls = {"n": 0}


def _counting_probe():
    _calls["n"] += 1
    return True, "9.65.169"


S._origin_com_probe = _counting_probe
S._clear_cache()
try:
    r1 = S.origin_com()
    r2 = S.origin_com()
    r3 = S.origin_com()
    check("① 三次调用只真探一次（缓存生效）", _calls["n"], 1)
    check("② 三次结果一致（缓存不许改结论）", r1 == r2 == r3, True)
    check("③ 结果没被吞掉（版本号原样带出）", r1, (True, "9.65.169"))
    S._clear_cache()
    S.origin_com()
    check("★ ④ _clear_cache 之后必须重探（装完 Origin 不该读到旧结论）",
          _calls["n"], 2)

    # ★ ⑤ TTL 必须真的会过期。这条是第一版的漏洞：我当初写的是**永久**缓存，
    #    还在注释里给了一个错理由（"装了要重启程序才生效"）—— 对 COM 不成立，
    #    装完 Origin 注册表项立刻在，下一次 Dispatch 就该成功。永久缓存会让
    #    用户装完了、页面还报"没装"，而那正是这个自检页存在的唯一理由。
    #    这里把 TTL 改成 0 秒，等价于"立刻过期"，判据只看**有没有再探**。
    _saved_ttl = S._ORIGIN_TTL_S
    S._ORIGIN_TTL_S = 0.0
    try:
        S._clear_cache()
        _before = _calls["n"]
        S.origin_com()
        S.origin_com()
        check("★ ⑤ TTL 到期后必须重探（装完 Origin 要能看见新结论）",
              _calls["n"] - _before, 2)
    finally:
        S._ORIGIN_TTL_S = _saved_ttl
finally:
    S._origin_com_probe = _real_probe
    S._clear_cache()

# ══════════════════════════════════════════════════════════════
print("\n[2] ★ 诚实降级：探不动**不许**当成装好了")
# ══════════════════════════════════════════════════════════════
# 负控：把子进程探测改成永远抛异常（模拟超时/被杀/环境坏）
_real_run = S.subprocess.run


class _Boom:
    def __call__(self, *a, **kw):
        raise OSError("模拟：子进程起不来")


S.subprocess.run = _Boom()
S._clear_cache()
try:
    good, why = S.probe_modules(sys.executable, ("json",))
    check("★ 子进程探测失败 ⇒ 判「不在」（默认值是全缺，不是全有）", good, False)
    check("  并且把失败原因说出来（不静默）", "模拟" in why, True)
finally:
    S.subprocess.run = _real_run
    S._clear_cache()
check("恢复后同一个问题又判在（证明上一条是被那次注入打穿的）",
      S.probe_modules(sys.executable, ("json",))[0], True)

# ══════════════════════════════════════════════════════════════
print("\n[3] 归因：每条检查都带 side / core")
# ══════════════════════════════════════════════════════════════
rep = S.diagnose_setup()
check("报告里有 verdict", rep["verdict"] in ("ready", "degraded", "broken"), True)
check("版本号在报告里（追得回是哪一版判据出的）", rep["version"], S.DOCTOR_VERSION)
sides = {it["side"] for it in rep["items"]}
check("side 只取两值", sides <= {"rongke", "sub"}, True)
check("两种 side 都出现了（不能全是默认值 —— 那说明归因没接上）", sides, {"rongke", "sub"})
cores = [it["key"] for it in rep["items"] if it["core"]]
check("核心项就是 CORE_KEYS 那几条",
      sorted(cores), sorted(k for k in S.CORE_KEYS if item(rep, k)))
check("★ 每一项都写得出它探的是哪一侧（空字符串会让人拿着一条'已就位'去解释另一侧）",
      all(it["side"] in ("rongke", "sub") for it in rep["items"]), True)

# 分组不许吞项：所有 items 必须都能在某个组里找到
grouped = [i["key"] for g in rep["groups"] for i in g["items"]]
check("★ 分组覆盖了全部条目（吞掉一项 = 界面上少一块，用户看不见）",
      sorted(grouped), sorted(it["key"] for it in rep["items"]))

# ══════════════════════════════════════════════════════════════
print("\n[4] 结论分级：核心缺 ⇒ broken；只缺画图侧 ⇒ degraded")
# ══════════════════════════════════════════════════════════════
_real_checks = S.CHECKS


def _fake(chk):
    """把某一条替换成固定结论，其余照旧。"""
    return tuple(chk) if callable(chk) else chk


def _with(overrides):
    """返回一组 CHECKS：被点名的那几项换成返回固定结果的假函数。"""
    out = []
    for fn in _real_checks:
        key = overrides.get(fn.__name__)
        if key is None:
            out.append(fn)
        else:
            out.append(key)
    return tuple(out)


def _const(rows):
    def f(ctx):
        return [S._imp(k, side, core, lv, t) for (k, side, core, lv, t) in rows]
    return f


try:
    S.CHECKS = _with({"check_origin": _const(
        [("origin.com", "sub", False, S.LV_FAIL, "Origin 断了")])})
    r = S.diagnose_setup()
    check("★ 只有画图侧（非核心）不通 ⇒ degraded，不是 broken",
          r["verdict"], "degraded")
    check("  而且 summary 说的是'做 PPT 能用'（不吓人）",
          r["summary_line"].startswith("做 PPT 能用"), True)

    S.CHECKS = _with({"check_deck_notes": _const(
        [("deck.notes.python", "sub", True, S.LV_FAIL, "没有 python-pptx")])})
    r = S.diagnose_setup()
    check("★ 核心项不通 ⇒ broken", r["verdict"], "broken")
    check("  summary 点名了缺哪一项", "python-pptx" in r["summary_line"], True)

    # 负控：把那条核心项的 core 标成 False，分级必须跟着变 ——
    # 证明 degraded/broken 的差别**真的**由 core 决定，不是碰巧的其它条件
    S.CHECKS = _with({"check_deck_notes": _const(
        [("deck.notes.python", "sub", False, S.LV_FAIL, "没有 python-pptx")])})
    r = S.diagnose_setup()
    check("★ 同一条失败、core=False ⇒ 降级成 degraded（证明 core 在承重）",
          r["verdict"], "degraded")
finally:
    S.CHECKS = _real_checks

# 一项检查自己炸了，**不许**把整份报告吞成空白
def _explode(ctx):
    raise RuntimeError("故意的")


try:
    S.CHECKS = _with({"check_origin": _explode})
    r = S.diagnose_setup()
    crash = [it for it in r["items"] if it["key"].startswith("crash:")]
    check("★ 有一项检查自己炸了 ⇒ 它单独报一条 crash，其余照常",
          len(crash), 1)
    check("  且分到 items 里（不是静默消失）", bool(r["items"]), True)
finally:
    S.CHECKS = _real_checks

# ══════════════════════════════════════════════════════════════
print("\n[5] 品牌模板：.pptx 与槽位表必须同源")
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "config.json").write_text(json.dumps({
        "deck": {"template": "", "template_name": "", "python": "", "skills_dir": ""},
        "figure": {"python": ""},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = Config.load(root)

    ctx = S._Ctx(cfg, root)
    r = S.check_deck_template(ctx)[0]
    check("没配模板 ⇒ info（不是故障：内置版式是合法的）", r.level, S.LV_INFO)

    # 配了一个不存在的 .pptx
    cfg.deck.template = str(root / "nope.pptx")
    r = S.check_deck_template(ctx)[0]
    check("★ 模板文件不在盘上 ⇒ fail，并点名是哪一条",
          (r.level, "不在盘上" in r.title), (S.LV_FAIL, True))

    # .pptx 在，但槽位表没有
    tpl = root / "t.pptx"
    tpl.write_bytes(b"PK\x03\x04")
    cfg.deck.template = str(tpl)
    name = ctx.template_name_of(str(tpl))
    r = S.check_deck_template(ctx)[0]
    check("★ .pptx 在但槽位表缺 ⇒ 单独报 fail（两边都跑得通、内容错位，最难查）",
          (r.level, "槽位表" in r.title), (S.LV_FAIL, True))
    check("  它把期望的槽位表路径写出来了（能照着补）",
          str(root / "docs" / "deck-templates" / name / "slots.json") in r.detail, True)

    # 两个都在 ⇒ ok
    slots = root / "docs" / "deck-templates" / name / "slots.json"
    slots.parent.mkdir(parents=True)
    slots.write_text("{}", encoding="utf-8")
    r = S.check_deck_template(ctx)[0]
    check("两个都在 ⇒ ok", r.level, S.LV_OK)
    check("  说明里写的是'同源'（人一眼看懂这条判的是什么）", "同源" in r.title, True)

# ══════════════════════════════════════════════════════════════
print("\n[6] 配置写入：白名单 + 不丢别人的字段")
# ══════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    original = {
        "_note": "这是用户自己写的注释字段，一个字都不许丢",
        "provider": {"model": "some/model", "api_key_env": "MY_KEY"},
        "deck": {"template": "D:/x.pptx", "timeout": 180, "min_slides": 6},
        "figure": {"python": "OLD"},
        "mcp": {"servers": {"origin-figure": {"domain": "figure"}}},
    }
    (root / "config.json").write_text(
        json.dumps(original, ensure_ascii=False, indent=2), encoding="utf-8")

    res = S.save_config_overrides({"figure.python": "NEW"}, root=root)
    after = json.loads((root / "config.json").read_text(encoding="utf-8"))
    check("写进去了", after["figure"]["python"], "NEW")
    check("★ _note 还在", after["_note"], original["_note"])
    check("★ deck 段一个字段都没丢（template/timeout/min_slides）",
          after["deck"], original["deck"])
    check("★ provider 段没被动", after["provider"], original["provider"])
    check("★ mcp 段没被动", after["mcp"], original["mcp"])
    check("写前有备份（用户的配置是一手资产）", Path(res["backup"]).is_file(), True)

    # 负控 1：白名单外的键一律拒（不做"聪明的静默忽略"）
    rejected = False
    try:
        S.save_config_overrides({"deck.template": "D:/evil.pptx"}, root=root)
    except ValueError:
        rejected = True
    check("★ 白名单外的键 ⇒ ValueError（静默忽略会让人以为改成功了）",
          rejected, True)
    check("  拒掉之后盘上没变", json.loads(
        (root / "config.json").read_text(encoding="utf-8"))["deck"]["template"],
        "D:/x.pptx")

    rejected = False
    try:
        S.save_config_overrides({"provider.model": "zzz"}, root=root)
    except ValueError:
        rejected = True
    check("★ 换个名字也进不去（provider.* 不在这张白名单里 —— 那是设置页的事）",
          rejected, True)

    rejected = False
    try:
        S.save_config_overrides({}, root=root)
    except ValueError:
        rejected = True
    check("空更新 ⇒ 拒（不写一个空操作）", rejected, True)

    # 分节写法：一次写两个小节
    res = S.save_config_overrides({"deck.python": "D1", "figure.python": "F1"},
                                  root=root)
    after = json.loads((root / "config.json").read_text(encoding="utf-8"))
    check("一次写两个小节都对",
          (after["deck"]["python"], after["figure"]["python"]), ("D1", "F1"))
    check("written 回执用点路径报改了什么",
          res["written"], {"deck.python": "D1", "figure.python": "F1"})

# ══════════════════════════════════════════════════════════════
print("\n[7] ★ 修完重测：读的必须是**新**配置")
# ══════════════════════════════════════════════════════════════
# 构造一个"配置指错了、但本机另一个解释器是对的"的局面：
# deck.python 指到一个空目录（不存在），而它其实能用 → 检查仍然 ok，
# 所以这里改用 figure.python：指到一个不存在的路径，检查应当是 fail；
# `_fix_one` 会把它指向真实可用的解释器，重测必须变 ok。
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    bogus = str(root / "no" / "such" / "python.exe")
    (root / "config.json").write_text(json.dumps({
        "deck": {"python": "", "skills_dir": "", "template": ""},
        "figure": {"python": bogus, "skills_dir": ""},
    }, ensure_ascii=False), encoding="utf-8")

    cfg = Config.load(root)
    before = S.check_figure_backend(S._Ctx(cfg, root))[0]
    # 这条检查会自动探测别的解释器 ⇒ 本机装了 matplotlib 的话它本来就是 ok。
    real = S._figure_py_elsewhere(bogus)
    if real:
        check("前提：本机确实有另一个可用解释器（有它才谈得上'自动修'）",
              bool(real), True)
        check("配置指错时，检查仍能靠自动探测找到可用的（如实说明来源）",
              before.level, S.LV_OK)
        res = S.fix_setup(["figure.python"], cfg=cfg, root=root)
        row = res["fixed"][0]
        check("修了", row["fixed"], True)
        after = json.loads((root / "config.json").read_text(encoding="utf-8"))
        check("★ 盘上真的写成了那个可用的解释器",
              after["figure"]["python"], real)
        check("★ 重测变绿（读的是新配置，不是进来时那份）",
              (row.get("verified"), row.get("after_level")), (True, S.LV_OK))
        check("重测后 summary 也更新了", isinstance(res["report"]["summary_line"], str), True)
    else:
        # 本机没有第二个解释器：那就验"如实说修不了"，不许假装修了
        res = S.fix_setup(["figure.python"], cfg=cfg, root=root)
        row = res["fixed"][0]
        check("本机无可用解释器 ⇒ 如实说修不了", (row["fixed"], row.get("verified")),
              (False, None))

    # 非白名单 key 走到 fix 里必须是"没修成"，而不是抛出去
    res = S.fix_setup(["deck.template"], cfg=cfg, root=root)
    check("★ 不能修的 key ⇒ fixed=False 且带人话理由（不是异常）",
          (res["fixed"][0]["fixed"], bool(res["fixed"][0]["why"])), (False, True))

# ══════════════════════════════════════════════════════════════
print("\n[8] 承重自检：拆掉三处，对应断言必须当场失守")
# ══════════════════════════════════════════════════════════════
# ① 拆掉「探测失败 ⇒ 判不在」：把默认值改成"全有"
_orig_probe = S.probe_modules


def _lenient_probe(exe, modules):
    exe = (exe or "").strip()
    if not exe or not Path(exe).exists():
        return True, ""          # ← 故意把"不存在"也说成在（错误实现）
    return _orig_probe(exe, modules)


S._clear_cache()
try:
    S.probe_modules = _lenient_probe
    check("★ 拆掉「不存在 ⇒ 不在」后，坏路径**被判成在**（证明那条判据在承重）",
          S.probe_modules(r"Z:\no\such\python.exe", ("json",))[0], True)
finally:
    S.probe_modules = _orig_probe
    S._clear_cache()
check("恢复后又判不在",
      S.probe_modules(r"Z:\no\such\python.exe", ("json",))[0], False)

# ② 拆掉白名单
_keys = S.SETUP_WRITE_KEYS
try:
    S.SETUP_WRITE_KEYS = tuple(list(_keys) + ["deck.template"])
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "config.json").write_text(json.dumps({"deck": {"template": "A"}}),
                                          encoding="utf-8")
        S.save_config_overrides({"deck.template": "B"}, root=root)
        got = json.loads((root / "config.json").read_text(encoding="utf-8"))
    check("★ 把 deck.template 加进白名单后，同一次写入**过了**（白名单在承重）",
          got["deck"]["template"], "B")
finally:
    S.SETUP_WRITE_KEYS = _keys
check("恢复后它又进不去",
      "deck.template" in S.SETUP_WRITE_KEYS, False)

# ③ 拆掉「重测读新配置」：给 _recheck 塞一份旧配置
_real_recheck = S._recheck


def _stale_recheck(key, root):
    return None          # ← 假装重测了，但什么也没测


try:
    S._recheck = _stale_recheck
    out = S._recheck("figure.python", Path("."))
    check("★ 换掉 _recheck 之后，「修完重测」拿不到任何结果（原来那条判据在承重）",
          out, None)
finally:
    S._recheck = _real_recheck

print()
print("[DONE] 首次使用自检全部通过" if ok_all else "[FAIL] 首次使用自检有未通过项")
sys.exit(0 if ok_all else 1)
