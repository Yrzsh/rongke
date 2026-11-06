# -*- coding: utf-8 -*-
"""常驻探针：外部能力自动发现 / 通用提示段 / 三条负面判据。

覆盖「吸收其他 agent 长处」这条通道的两个方向：
  · 正控：丢一个 .py 进目录 ⇒ 预设点一行 ⇒ 正文真进 system
  · 负控：坏件、点名却没挂能力、拼错段名、baseline 逐字节等价

判据口径：逐条 `[PASS]`，结尾 `[DONE]`（`regress.py:_RE_MARK` 只认这些标记）。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))

_N = 0
_FAIL = 0


def check(name: str, cond, detail: str = "") -> bool:
    global _N, _FAIL
    _N += 1
    ok = bool(cond)
    if not ok:
        _FAIL += 1
        print(f"[FAIL] {name} {detail}")
    else:
        print(f"[PASS] {name}")
    return ok


def main() -> int:
    from rongke import caps
    from rongke.caps import (CapPreset, CapRow, mount_preset,
                             reset_external_loader)
    from rongke.contract import TaskContract
    from rongke.loop import SYSTEM, STATS_CHECKLIST, initial_messages

    EXT = "@external/answer-first"
    SEG = "external-answer-first"

    def system_of(c) -> str:
        return initial_messages(TaskContract(raw_goal="测试"), None,
                                caps=c)[0]["content"]

    print("== ① 自动发现（正控）==")
    reset_external_loader()
    errs = caps.ensure_external_loaded()
    check("随库 caps_ext 目录被扫到",
          any("caps_ext" in str(p) for p in caps.external_cap_dirs()))
    check("外部能力模块导入无错", errs == [], str(errs))
    check("外部能力不用手工 import 就能 resolve",
          caps.resolve(EXT) is not None)
    check("注册名出现在 registered_names()",
          EXT in caps.registered_names())
    check("available_sections 读回自带段名",
          SEG in caps.available_sections())

    print("== ② 额外目录 / 坏件 / 恢复（负控两个方向）==")
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "probe_only.py").write_text(
            "from rongke.caps import Capability, register\n"
            "@register('@external/probe-only')\n"
            "class P(Capability):\n"
            "    kind = 'tool'\n"
            "    SECTION_TEXTS = {'probe-seg': 'PROBE'}\n",
            encoding="utf-8")
        os.environ["RONGKE_CAP_EXT"] = str(d)
        reset_external_loader()
        check("RONGKE_CAP_EXT 目录里的能力被自动加载",
              caps.resolve("@external/probe-only") is not None)
        check("额外目录的自带段也进 available_sections",
              "probe-seg" in caps.available_sections())
        # 反事实：把 env 清掉，这个名字就再没有来源
        os.environ.pop("RONGKE_CAP_EXT", None)
        reset_external_loader()
        check("移出扫描目录 ⇒ 该能力 resolve 不到（反事实）",
              caps.resolve("@external/probe-only") is None)

        os.environ["RONGKE_CAP_EXT"] = str(d)
        (d / "broken.py").write_text("this is not python !!!\n",
                                     encoding="utf-8")
        reset_external_loader()
        bad = caps.ensure_external_loaded()
        check("坏 .py 被记账，没有静默吞掉",
              any("broken.py" in e["file"] for e in bad), str(bad))
        rep = mount_preset(CapPreset("probe", "probe", 1, []))
        check("坏 .py ⇒ MountReport.broken 里有 external 行",
              any(r.kind == "external" for r in rep.broken))
        check("坏 .py ⇒ healthy=False（唯一健康判据没被绕过）",
              rep.healthy is False)
        (d / "broken.py").unlink()
        os.environ.pop("RONGKE_CAP_EXT", None)
        reset_external_loader()
        check("移走坏件后恢复干净", caps.ensure_external_loaded() == [])

    print("== ②b 工具型长处（别的 agent 的主流形态）也能零改内核 ==")
    with tempfile.TemporaryDirectory() as td2:
        d2 = pathlib.Path(td2)
        (d2 / "probe_tool.py").write_text(
            "from rongke.caps import Capability, register\n"
            "@register('@external/probe-tool')\n"
            "class T(Capability):\n"
            "    kind = 'tool'\n"
            "    config_schema = {'on': {'type': 'bool', 'required': True}}\n"
            "    def mount(self, cfg, ctx):\n"
            "        reg = ctx.get('registry')\n"
            "        if reg is None or not cfg.get('on'):\n"
            "            return ['no registry']\n"
            "        from rongke.tools import Tool\n"
            "        reg.register(Tool(name='probe_echo', description='probe',\n"
            "                          parameters={'type': 'object', 'properties': {}},\n"
            "                          fn=lambda **kw: 'ok', domain='core',\n"
            "                          source='plugin'))\n"
            "        ctx.setdefault('registered_tools', []).append('probe_echo')\n"
            "        return ['probe_echo']\n",
            encoding="utf-8")
        os.environ["RONGKE_CAP_EXT"] = str(d2)
        reset_external_loader()
        from rongke.tools import ToolRegistry
        reg = ToolRegistry()
        live_ctx = {"registry": reg}
        prow = CapRow("ext-tool", "@external/probe-tool", {"on": True},
                      "insert", False, "probe")
        rep = mount_preset(CapPreset("probe-tool", "probe-tool", 1, [prow]),
                           ctx=live_ctx)
        check("外部 tool 能力挂载健康", rep.healthy,
              str([r.error for r in rep.broken]))
        check("外部能力真把工具注册进 registry",
              reg.get("probe_echo") is not None)
        check("按约定登记 registered_tools（run 收尾能撤销）",
              "probe_echo" in (live_ctx.get("registered_tools") or []))
        os.environ.pop("RONGKE_CAP_EXT", None)
        reset_external_loader()

    print("== ③ 点名与提供必须成对（拦沉默）==")
    reset_external_loader()
    row = CapRow("prompt", "@rongke/prompt-sections",
                 {"complete": False, "sections": [SEG]},
                 "insert", False, "probe")
    rep = mount_preset(CapPreset("probe2", "probe2", 1, [row]))
    check("点名外部段却没挂能力 ⇒ broken",
          any("没有任何能力提供它" in r.error for r in rep.broken),
          str([r.error for r in rep.broken]))
    row2 = CapRow("prompt", "@rongke/prompt-sections",
                  {"complete": False, "sections": ["no-such-section"]},
                  "insert", False, "probe")
    rep2 = mount_preset(CapPreset("probe3", "probe3", 1, [row2]))
    check("拼错段名仍打红（白名单没被放宽）",
          any("不存在的提示段" in r.error for r in rep2.broken))

    print("== ④ 真进 system（正控）==")
    reset_external_loader()
    c = caps.load_capability_preset("ext-answer-first")
    check("第 8 份预设挂载健康", c.healthy,
          str([r.error for r in c.report.broken]))
    check("第 8 份预设比 baseline 多一行", len(c.report.results) == 10,
          str(len(c.report.results)))
    s = system_of(c)
    check("外部能力自带的正文真进 system", "答复纪律（外部能力" in s)
    check("system 确实变长（不是只挂了名字）", len(s) > len(SYSTEM))
    check("prompt_texts 只给点名的段", set(c.prompt_texts([SEG])) == {SEG})
    check("没点名的段不会被塞进去", c.prompt_texts(["identity"]) == {})
    check("段正文非空（不是空字符串冒充注入）",
          bool(c.prompt_texts([SEG]).get(SEG, "").strip()))

    print("== ⑤ 旧行为为默认（最重的负控）==")
    c0 = caps.load_capability_preset("baseline")
    check("baseline system 逐字节 == SYSTEM", system_of(c0) == SYSTEM)
    check("Caps.empty() system 逐字节 == SYSTEM",
          system_of(caps.Caps.empty()) == SYSTEM)
    check("baseline 仍挂 9 行 / 0 坏件",
          len(c0.report.results) == 9 and c0.healthy)
    cs = caps.load_capability_preset("stats-review")
    ss = system_of(cs)
    check("stats-review 仍追加内核单源段", STATS_CHECKLIST in ss)
    check("stats-review 不混入外部段", "答复纪律（外部能力" not in ss)
    check("7 份原预设没被新增校验打红",
          not [p for p in ("baseline", "strict-v2", "paper-writing",
                           "lit-verify", "stats-review", "figure", "deck")
               if not caps.load_capability_preset(p).healthy])

    print("== ⑥ 结构判据（防死代码复发）==")
    src_caps = (caps.__file__ and pathlib.Path(caps.__file__).read_text(
        encoding="utf-8")) or ""
    src_loop = (pathlib.Path(sys.modules["rongke.loop"].__file__).read_text(
        encoding="utf-8"))
    src_reg = (_ROOT / "regress.py").read_text(encoding="utf-8")
    check("caps 有自动发现入口", "def ensure_external_loaded" in src_caps
          and "def external_cap_dirs" in src_caps)
    check("坏件转成 broken 的接线在场", 'kind="external"' in src_caps)
    check("交叉校验的实现在场", "没有任何能力提供它" in src_caps)
    check("loop 用通用消费点，不再只有一个硬编码 if",
          "prompt_texts" in src_loop and "for _name in _sections" in src_loop
          and 'if "stats-checklist" in _sections' not in src_loop)
    check("常驻探针已挂 regress.py", "caps_ext_test.py" in src_reg)

    reset_external_loader()
    caps.ensure_external_loaded()
    check("收尾：随库能力仍可 resolve（探针没污染进程）",
          caps.resolve(EXT) is not None)

    print(f"[DONE] {'全部通过' if _FAIL == 0 else '有失败'} · "
          f"{_N} 项 / {_FAIL} 失败")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
