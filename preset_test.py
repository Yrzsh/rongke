# -*- coding: utf-8 -*-
"""预设装载器单测 —— 纯文件系统，不需要 LLM、不需要网络、不写任何真账。

`rongke/preset.py` 补的是内核的一处**层级缺失**：受理时没有人能选定"这一场是谁"
（CLI / Web / `Rongke.run()` 三处都没有这个位），事实上的模式只能由关键词网从目标
文本里猜。本模块只装与验，**当前不接任何模型可见面** —— 所以本套件全部离线可跑。

  1. 校验层     —— 未知键 / 缺必填 / id≠文件名 / 类型错 / 列表重复，一律给**能印**的理由
  2. 装载       —— JSON 坏 与 内容坏 **分开报**（修法不同）；好文件读出真字段
  3. ★ 坏项不静默 —— 目录里有一份坏预设 ⇒ 必须出现在 broken 里且带理由；
                    全好时 broken 必须**空**（只有负控 = 可能这条检查根本没在跑）
  4. 单源       —— 场景名来自 contract.SCENE_KEYS、阶段名来自 stages.STAGE_BY_ID，
                  本模块不许自带第二份枚举
  5. 个人覆盖层 —— dict 深合并；★ list 是**覆盖**不是追加；
                  个人层写进不认得的名字 ⇒ 合并**之后**必须被拒
  6. 阈值补丁   —— 无预设 / 空小节 ⇒ 返回 `{}`，调用方据此什么都不做
  7. 承重自检   —— 分别拆掉「未知键拒绝 / id=文件名 一致 / 合并后再校验」，
                  第 1、5 节对应的断言必须**当场失守**

沿用 `topic_test.py` 的两条纪律：
  · **每个断言成对出现**（正控 + 负控）。
  · **所有写操作落临时目录**（`home=` 指临时家目录），绝不碰真 `rongke/presets/`
    与真的 `~/.rongke/config.json` —— 那是一个人配子的地方，测一次脏一次。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import tempfile                                    # noqa: E402
from rongke import preset as P                     # noqa: E402
from rongke import contract as C                   # noqa: E402
from rongke import stages as S                     # noqa: E402

ok_all = True


def check(label, got, want):
    global ok_all
    good = got == want
    ok_all = ok_all and good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}")
    if not good:
        print(f"         期望 {want!r}，实际 {got!r}")


def rejects(label, data, **kw):
    """校验必须给出**非空**理由（负控用 `accepts`）。"""
    why = P.validate(data, **kw)
    check(label, bool(why), True)
    return why


def accepts(label, data, **kw):
    check(label, P.validate(data, **kw), "")


def write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


GOOD = {"id": "paper", "name": "论文", "description": "写稿这一路", "order": 10,
        "scenes": ["writing"], "stages": ["draft", "polish"], "mount": ["kb"],
        "config": {"deck": {"min_slides": 12}}, "prompt": {"block": "paper_ritual"},
        "exemplars": ["hirsutella-review"], "tier": "journal"}

# 校验时喂给装载器的"当下真在用的集合" —— 由调用方给，不在模块里抄第二份。
DOM = {"core", "research", "figure", "ppt", "kb"}
BLK = {"paper_ritual", "mentor_ritual"}

print("\n[1] 校验层：只认得的东西才放行")
# ── 正控 ──
accepts("合法预设过校验", GOOD, known_domains=DOM, known_blocks=BLK)
accepts("只有必填项也放行", {"id": "x", "scenes": ["writing"]})
accepts("空小节放行（不覆盖任何东西）", {"id": "x", "scenes": []})
# ★ 只带必填项时，校验器不许因为缺某个可选小节就 KeyError 崩掉 ——
#   实跑里连崩过两处（`stages`、`mount`），而"缺小节"恰恰是最常见的写法。
check("只带必填项 + 给了 known_domains ⇒ 不因缺 mount 而崩",
      P.validate({"id": "x", "scenes": ["writing"]}, known_domains=DOM), "")
check("只带必填项 + domains 与 blocks 都给 ⇒ 仍放行",
      P.validate({"id": "x", "scenes": ["writing"]}, known_domains=DOM,
                 known_blocks=BLK), "")
check("只带必填项 + 阶段名也校验 ⇒ 不因缺 stages 而崩",
      P.validate({"id": "x", "scenes": ["writing"]}, stage_ids=("draft",)), "")
# ── 负控：一条理由都不许是空的 ──
why = rejects("未知顶层键被拒", {**GOOD, "skillz": ["draft"]}, known_domains=DOM)
check("  理由里点出了那个键名", "skillz" in why, True)
check("  理由里列出允许键", "scenes" in why, True)
rejects("缺 id 被拒", {"scenes": ["writing"]})
rejects("缺 scenes 被拒", {"id": "paper"})
check("id≠文件名被拒（选 id 读的是文件名）",
      bool(P.validate({"id": "paper", "scenes": ["writing"]}, stem="dsh_paper")), True)
check("id 与文件名一致时放行",
      P.validate({"id": "paper", "scenes": ["writing"]}, stem="paper"), "")
rejects("id 空串被拒", {"id": "  ", "scenes": ["writing"]})
rejects("order 非整数被拒", {**GOOD, "order": "10"}, known_domains=DOM)
rejects("order 用 bool 蒙混被拒", {**GOOD, "order": True}, known_domains=DOM)
rejects("scenes 不是列表被拒", {**GOOD, "scenes": "writing"}, known_domains=DOM)
rejects("scenes 里有空项被拒", {**GOOD, "scenes": ["writing", "  "]}, known_domains=DOM)
why = rejects("scenes 里有重复项被拒",
              {**GOOD, "scenes": ["writing", "writing"]}, known_domains=DOM)
check("  重复项被点名", "writing" in why, True)
rejects("stages 里有不认得的阶段", {**GOOD, "stages": ["nope"]}, known_domains=DOM)
check("  认得的阶段放行",
      P.validate({**GOOD, "stages": ["discipline", "slides"]}, known_domains=DOM), "")
why = rejects("mount 里有没挂上的域", {**GOOD, "mount": ["vision"]}, known_domains=DOM)
check("  理由说清后果（少工具，不报错）", "注册表" in why, True)
check("  known_domains=None 时不猜（域交调用方判）",
      P.validate({**GOOD, "mount": ["vision"]}), "")
rejects("config 不是对象被拒", {**GOOD, "config": [1]}, known_domains=DOM)
rejects("config 里有不认得的段", {**GOOD, "config": {"provider": {"model": "x"}}},
        known_domains=DOM)
why = rejects("config.<段> 必须是对象", {**GOOD, "config": {"deck": 12}},
              known_domains=DOM)
check("  理由点名「会被整段替换」", "整段替换" in why, True)
rejects("prompt 不是对象被拒", {**GOOD, "prompt": "paper_ritual"}, known_domains=DOM)
rejects("prompt 里有未知键", {**GOOD, "prompt": {"text": "自带散文"}},
        known_domains=DOM)
why = rejects("block 不认得 ⇒ 拒（不许预设自带提示文本）",
              {**GOOD, "prompt": {"block": "made_up"}}, known_domains=DOM,
              known_blocks=BLK)
check("  理由里写了「提示文本住代码里」", "提示文本住代码里" in why, True)
rejects("整个预设不是对象", ["a", "b"])

print("\n[2] 装载：读坏的与读歪的要分开报")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    p = write(d / "paper.json", GOOD)
    preset, why = P.load(p, known_domains=DOM, known_blocks=BLK)
    check("合法文件装载成功", (preset is None, why), (False, ""))
    check("  字段逐个落地",
          (preset.id, preset.name, preset.order, tuple(preset.scenes),
           tuple(preset.stages), tuple(preset.mount), preset.block,
           tuple(preset.exemplars), preset.tier),
          ("paper", "论文", 10, ("writing",), ("draft", "polish"), ("kb",),
           "paper_ritual", ("hirsutella-review",), "journal"))
    check("缺 name 时落回 id，不编造描述",
          P.load(write(d / "min.json", {"id": "min", "scenes": []}),
                 known_domains=DOM)[0].to_dict()["description"], "")
    check("  缺 name 时确实回落到 id",
          P.load(write(d / "min.json", {"id": "min", "scenes": []}),
                 known_domains=DOM)[0].name, "min")
    badjson = d / "broken.json"
    badjson.write_text("{{{not json", encoding="utf-8")
    check("  坏 JSON ⇒ 「读取失败」", P.load(badjson)[1].startswith("读取失败"), True)
    check("  内容歪 ⇒ 走校验理由，不走读取失败",
          P.load(write(d / "wrong.json", {**GOOD, "id": "wrong", "order": "10"}),
                 known_domains=DOM)[1].startswith("order"), True)

print("\n[3] ★ 坏预设不许静默消失（这是与 dsh 刻意不同的那一条）")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    write(d / "paper.json", GOOD)
    write(d / "mentor.json", {**GOOD, "id": "mentor", "scenes": ["nope"]})
    write(d / "deck.json", {**GOOD, "id": "deck", "scenes": ["deck"],
                            "order": 5})
    good, broken = P.list_presets(d, known_domains=DOM, known_blocks=BLK)
    check("好的两份进来", sorted(x.id for x in good), ["deck", "paper"])
    check("  坏的那份**在 broken 里**，没被滤干净", [b[0] for b in broken], ["mentor"])
    check("  且带着能印的理由", bool(broken[0][1]), True)
    check("排序按 order（同值按 id）", [x.id for x in good], ["deck", "paper"])
    again_good, again_bad = P.list_presets(d, known_domains=DOM, known_blocks=BLK)
    check("同一目录两次列，结果一致（没有隐藏状态）",
          ([x.id for x in again_good], [b[0] for b in again_bad]),
          ([x.id for x in good], [b[0] for b in broken]))
    with tempfile.TemporaryDirectory() as clean:
        write(Path(clean) / "only.json", {"id": "only", "scenes": ["writing"]})
        _, b2 = P.list_presets(Path(clean), known_domains=DOM)
        check("★ 全好时 broken 必须是**空**（负控：证明不是恒返回一份）", b2, [])
    _, b3 = P.list_presets(Path(td) / "不存在", known_domains=DOM)
    check("目录不存在 ⇒ 如实报，不返回空列表假装没事",
          bool(b3) and "不存在" in b3[0][1], True)

print("\n[4] 单源：本模块不许自带第二份枚举")
check("场景键就是 contract.SCENE_KEYS（不是 preset 里另抄一份）",
      P.SCENE_KEYS, C.SCENE_KEYS)
check("  SCENE_KEYS 当前正好是三个真在生效的位",
      C.SCENE_KEYS, ("writing", "figure", "deck"))
check("阶段键就是 stages.STAGE_BY_ID 的键（默认值直接取它，不另立表）",
      P.load(write(Path(tempfile.mkdtemp()) / "s.json",
                   {"id": "s", "scenes": [], "stages": list(S.STAGE_BY_ID)}),
             known_domains=DOM)[0].stages, list(S.STAGE_BY_ID))
check("  一个都不认得的阶段名 ⇒ 拒",
      bool(P.validate({"id": "x", "scenes": [], "stages": ["nope"]})), True)
check("预设里写的场景若不在单源里 ⇒ 拒（新场景必须先去 contract 加）",
      bool(P.validate({"id": "x", "scenes": ["kb"]})), True)

print("\n[5] 个人覆盖层：合并形状与它的坑")
with tempfile.TemporaryDirectory() as home:
    h = Path(home)
    (h / ".rongke").mkdir()
    cfgp = h / ".rongke" / "config.json"
    check("个人层不存在 ⇒ 空 dict（不猜位置）", P.personal_layer(home=h), {})
    write(cfgp, {"presets": {"paper": {"config": {"deck": {"max_slides": 60}}}}})
    layer = P.personal_layer(home=h)
    check("  有 presets 小节时取到该小节", "paper" in layer, True)
    pp = write(Path(h) / "paper.json", GOOD)
    write(cfgp, {"presets": "不是对象"})
    raised = ""
    try:
        P.personal_layer(home=h)
    except P.PresetError as e:
        raised = str(e)
    check("★ 个人层小节形状不对 ⇒ **报错**，不许当成「没配」放过去",
          raised.startswith("个人层 presets 小节必须是对象"), True)
    check("  同一条错误从 load_with_layer 走**同一个出口**（返回理由，不抛）",
          P.load_with_layer(pp, home=h, known_domains=DOM)[1], raised)
    write(cfgp, {"presets": {"paper": "不是对象"}})
    check("  单个 id 的覆盖项形状不对 ⇒ 也报",
          P.load_with_layer(pp, home=h, known_domains=DOM)[1].startswith(
              "个人层 presets.paper"), True)
    write(cfgp, {"presets": {"paper": {"config": {"deck": {"max_slides": 60}},
                                       "stages": ["verify"]}}})
    preset, why = P.load_with_layer(pp, home=h, known_domains=DOM, known_blocks=BLK)
    check("  装载通过", why, "")
    check("★ 个人层写 list 是**覆盖**不是追加（_deep_merge 的语义）",
          preset.stages, ["verify"])
    check("  而 dict 小节是**逐键合并**（随库那份的 min_slides 还在）",
          preset.config.get("deck"), {"min_slides": 12, "max_slides": 60})
    write(cfgp, {"presets": {"paper": {"scenes": ["nope"]}}})
    why = P.load_with_layer(Path(h) / "paper.json", home=h,
                            known_domains=DOM)[1]
    check("★ 个人层写进不认得的名字 ⇒ **合并之后**被拒", bool(why), True)
    check("  且理由说明这是覆盖后的结果", "个人层覆盖后" in why, True)
    write(cfgp, {})
    check("  个人层没这个 id ⇒ 与直接 load 等价",
          P.load_with_layer(Path(h) / "paper.json", home=h, known_domains=DOM,
                            known_blocks=BLK)[0].to_dict(),
          P.load(Path(h) / "paper.json", known_domains=DOM, known_blocks=BLK)[0].to_dict())

print("\n[6] 阈值补丁：没东西可覆盖时必须是空")
check("无预设 ⇒ {}", P.config_patch(None), {})
check("预设不带 config ⇒ {}", P.config_patch(P.Preset(id="x")), {})
patched = P.config_patch(src := P.Preset(id="x", config={"deck": {"min_slides": 12}}))
check("有 config ⇒ 原样摊平一层", patched, {"deck": {"min_slides": 12}})
patched["deck"]["min_slides"] = 999
check("★ 返回的是副本（改补丁不许动到预设本体 —— 比的是**同一个对象**）",
      src.config["deck"]["min_slides"], 12)
check("  逐层都是副本，不止最外层",
      P.config_patch(src)["deck"] is not src.config["deck"], True)

print("\n[7] 承重自检：拆掉三道检查，对应断言必须当场失守")
_F = P.FIELDS
try:
    P.FIELDS = tuple(list(_F) + ["skillz"])
    check("拆掉「未知键拒绝」后，同一条数据**过了**（证明那条检查在承重）",
          P.validate({**GOOD, "skillz": ["draft"]}, known_domains=DOM), "")
finally:
    P.FIELDS = _F
check("恢复后再验：又拒了",
      bool(P.validate({**GOOD, "skillz": ["draft"]}, known_domains=DOM)), True)

check("拆掉 stem 后，id≠文件名这条**失效**（证明一致性的来源是校验器）",
      P.validate({"id": "paper", "scenes": ["writing"]}, stem=""), "")
check("带 stem 时又拦住",
      bool(P.validate({"id": "paper", "scenes": ["writing"]}, stem="other")), True)

with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    p = write(d / "paper.json", GOOD)
    h = d / "home"
    (h / ".rongke").mkdir(parents=True)
    write(h / ".rongke" / "config.json", {"presets": {"paper": {"scenes": ["nope"]}}})
    check("先校验原始那份 ⇒ 过（证明「合并前校验」会放过个人层的坏名字）",
          P.validate(json.loads(p.read_text(encoding="utf-8")), stem="paper",
                     known_domains=DOM), "")
    check("合并后再校验 ⇒ 拦住（这条顺序在承重）",
          bool(P.load_with_layer(p, home=h, known_domains=DOM)[1]), True)

print()
print("[DONE] 预设装载器全部通过" if ok_all else "[FAIL] 预设装载器有未通过项")
sys.exit(0 if ok_all else 1)
