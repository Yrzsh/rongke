# -*- coding: utf-8 -*-
"""常驻插件：机制图原生 SVG 出图 + 七道质量门（2026-11-11）。

## 为什么是「常驻」而不是预设

数据图有 `plot_svg`，机制图（谁激活谁、谁抑制谁、谁在哪个区室）此前没有
**质量可判定**的出图入口 —— 模型只能手写 SVG 字节，字画出框、箭头朝反、
区室套区室都无从拦截。本插件把 `diagram_svg` 挂成常驻工具：模型声明
`{nodes, edges, groups}` 规格，几何全由 `svgdiagram` 算，算完立刻交
`svgq` 七道门量，红项逼模型改规格重画。不该让人先点一个胶囊才生效。

## 与 plot_svg 同一条纪律

`ok` 只表示「SVG 结构自检通过」，`scope` 固定 `"preview"`，`checks` 全 bool。
质量结论另走 `quality` + `next`，**不去改 `gate.py` 预览分支的放行语义**
（那里钉死了 `ok is True and scope == "preview"`，且只认 plot_svg /
plot_data.native_svg；本工具不参与那条放行，也不冒充过 PDF 六道门）。
"""
from __future__ import annotations

import json
from pathlib import Path

from rongke.caps import register
from rongke.tool_sdk import DESK_TOOL_PRIORITY, DeskCapability, safe_workspace_path

ALWAYS_ON = True

_SECTION = "external-svg-diagram"

#: 规格口径的**单源**：工具描述与提示段共用这一份，免得两处漂移。
SPEC_GUIDE = (
    "spec 是一个对象：nodes=[{id, label, kind, group}]、"
    "edges=[{from, to, label, kind}]、groups=[{id, label}]，"
    "另有 direction（LR 横排 / TB 竖排）、title、width_mm（声明物理宽度）。"
    "node.kind ∈ process(圆角矩形) / decision(菱形) / terminator(胶囊) / entity(直角矩形)"
    " / database(竖立圆柱，数据集) / circle(正圆，分子) / hexagon(六边形，预处理)"
    " / subroutine(双竖线矩形，子流程) / stadium(跑道形双框，阶段) / cylinder(横放圆柱，样本)；"
    "edge.kind ∈ arrow(激活/流向) / inhibit(抑制，T 形横杠) / dashed(虚线，间接/假设)"
    " / phospho(磷酸化/修饰，末端圆点) / bind(结合/相互作用，双向箭头)"
    " / cleave(剪切/加工，末端双横杠) / secrete(分泌/释放，末端囊泡点)"
    " / degrade(降解/清除，末端叉号)。"
    "from/to 必须是已声明的 node.id；不画自环（回路请加中间节点）。"
    "插图模式：节点加 icon 就画成**生物图元**（标签自动放到图元下方，label 留空则不写）："
    "cell_t(T 细胞) / cell_cd4(CD4 细胞) / cell_b(B 细胞，带 Y 抗体) / cell(通用细胞)"
    " / dendritic(树突状细胞) / macrophage(巨噬细胞) / tumor(肿瘤细胞，多核异形)"
    " / tissue(组织切片) / organ(器官) / mouse(实验小鼠) / bacteria(细菌)"
    " / nucleus(细胞核) / er(内质网) / golgi(高尔基) / mito(线粒体) / protein(蛋白团)"
    " / dna / mrna / ribosome / antibody / mhc(MHC 分子) / liposome(脂质体，疫苗)"
    " / virus / exosome(外泌体) / lysate(肿瘤裂解物)。"
    "groups 加 kind=\"cell\" 时区室画成一圈**细胞膜**"
    "（成员在膜内），否则是浅色相背景块。图元不够用就老实说，别拿矩形冒充细胞。"
    "坐标、换行、层序、连线走向、箭头、区室框全由工具算 —— 不要手写 SVG 字节。"
    "多 panel：spec 顶层给 panels=[{id?, label?, spec}] + cols（默认 2）就把几张图"
    "拼成 A/B/C 组图（每个 panel 独立布局与自检，故事可以分格讲）；"
    "给了 panels 就不再走顶层 nodes/edges。"
    "图例：顶层给 legend=[{label, icon?} 或 {label, color}]，工具在画布底部排一行"
    "（细胞类型/膜/激活抑制这些符号必须在图例里交代，否则读者只能猜）。"
)

#: 图元级状态面的**动作口径单源**：工具描述与「都没给」的报错指引共用这一份。
#: 刻意不进 SECTION_TEXTS —— 常驻工具只进工具表，SYSTEM 一字不动。
#: 完整参数手册按需通过 `{"op":"guide"}` 渐进披露，不在工具 schema 里重复堆长文。
ACTION_GUIDE = (
    "机制图增量状态面（机制图.json，不存坐标，可传 state_path 与 expected_revision）："
    "发 {\"op\":\"guide\"} 读完整参数口径。"
    "读：describe / inspect / query；"
    "原子编辑：new / add_node / add_edge / add_group / set_node / set_edge / set_canvas / remove；"
    "意图事务：group / relabel / connect / batch；"
    "布局与版本：layout / align / route / snapshot / restore；"
    "出图：render（投影 spec 并跑七道质量门）。"
    "与 spec 二选一：只给 spec=一次性出图；只给 action=增量操作面。"
)


def _register_tools(ctx: dict, cfg: dict) -> list[str]:
    from rongke.tools import Tool
    registry = ctx.get("registry")
    if registry is None:
        return []

    def _fn_diagram(spec=None, out: str = "", action=None) -> str:
        from rongke import svgdiagram as _sd
        from rongke import svgmeasure as _sm
        from rongke import svgq as _sq
        from rongke import diagram_craft as _dc

        # ── 图元级状态面（可选入口）──────────────────────────────
        # 不传 action 时，下面那条「整份 spec → 一次性出图」路径一字未动，
        # 连 `机制图.json` 都不会碰（默认路径零副作用，判据有正负控）。
        # action 走状态单源；op=render 只把投影出的 spec 交给同一条出图链，
        # 因此七道质量门、写后读回、quality/next 全部复用，不另开一套。
        state_note = None
        hints = None
        if action:
            from rongke import diagram_state as _DS
            arep = _DS.apply(action)
            if spec not in (None, "", {}):
                arep["warning"] = ("同时给了 spec 与 action：本次以 action 为准，"
                                   "spec 未使用（明说，不静默丢弃）。")
            if not arep.get("ok") or "spec" not in arep:
                return json.dumps(arep, ensure_ascii=False, default=str)
            spec = arep["spec"]
            hints = arep.get("hints")
            state_note = arep
        if spec is None:
            return json.dumps(
                {"ok": False, "error": "spec 与 action 都没给",
                 "next": "二选一：一次性出图给 spec；图元级状态面给 action。",
                 "spec_guide": SPEC_GUIDE, "action_guide": ACTION_GUIDE},
                ensure_ascii=False)

        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except ValueError as e:
                return json.dumps(
                    {"ok": False, "error": f"spec 不是合法 JSON：{e}",
                     "next": "按工具描述里的规格格式重发 spec。"},
                    ensure_ascii=False)
        wd = Path.cwd()
        if out:
            try:
                dest = safe_workspace_path(out, wd, allow_abs=True)
            except ValueError as e:
                return json.dumps(
                    {"ok": False, "error": str(e),
                     "next": "out 必须是工作区内的相对路径（不许用 ../ 越界）。"},
                    ensure_ascii=False)
        else:
            dest = wd / "机制图.svg"
        if dest.suffix.lower() != ".svg":
            dest = dest.with_suffix(".svg")
        try:
            rep = _sd.render_diagram(spec, dest, hints)
        except _sd.DiagramError as e:
            return json.dumps(
                {"ok": False, "error": str(e),
                 "next": "这是规格不合法，不是画布问题：按报错改 spec 再调一次。"},
                ensure_ascii=False)
        except Exception as e:                              # noqa: BLE001
            return json.dumps(
                {"ok": False, "error": f"机制图后端出错：{type(e).__name__}: {e}"},
                ensure_ascii=False)

        payload = dict(rep)
        # 第二层够得着就升实测级；够不着如实判「审不了」，不等于通过。
        try:
            from rongke.config import Config
            py = Config.load().figure.python or ""
        except Exception:                                   # noqa: BLE001
            py = ""
        try:
            measured = _sm.measure(dest, python=py)
        except Exception as e:                              # noqa: BLE001
            measured = {"ok": False, "error": f"{type(e).__name__}: {e}",
                        "boxes": []}
        q = _sq.audit_svg(dest, layout=rep.get("layout"), measured=measured)
        summary = q.pop("summary")
        # 工艺层（2026-11-14）：七道门管「没画坏」，这一层管「像不像期刊插图」。
        # 量的是配色语义、尺度均衡、折行断词、标签压线、区室嵌套、回边通道 ——
        # 每条都带可执行的修法，AI 不用猜「这张凑不凑合」。
        try:
            craft = _dc.audit(rep.get("layout") or {})
        except Exception as e:                              # noqa: BLE001
            craft = {"ok": None, "items": [], "must_fix": [],
                     "error": f"{type(e).__name__}: {e}"}
        payload["craft"] = craft
        payload["quality"] = {
            "tier": q["tier"],
            "gates": {g["name"]: g["verdict"] for g in q["gates"]},
            "blocking": q["blocking"],
            "fix_before_delivery": q["fix_before_delivery"],
            "not_auditable": q["not_auditable"],
            "n_texts": q["n_texts"], "n_marks": q["n_marks"],
            "manifest": q["manifest"],
            "measure_error": (measured or {}).get("error", ""),
        }
        tier_txt = {"measured": "浏览器实测级",
                    "partial": "浏览器部分实测级"}.get(q["tier"], "标准库估算级")
        payload["audit_scope"] = (
            f"已跑 SVG 质量门七道（{tier_txt}）：字号物理量纲 / 对比度 / 文字容纳 / "
            "碰撞 / 数据保真 / 字体栈声明 / 字形覆盖。"
            "⚠️ 仍未跑 PDF 六道渲染后审计门（那是投稿路线，审的是 PDF）。")

        nxt = [
            "这是**预览级** SVG 机制图：可直接插网页 / PPT / 给用户看。",
            "⚠️ 不要把它当投稿级 PDF 交给 audit_figure —— 那六道门审的是 PDF，"
            "本次一道都没跑。",
            "",
            summary,
        ]
        for wn in rep.get("warnings") or []:
            nxt += ["", f"⚠️ 布局警告：{wn}"]
        if isinstance(craft, dict) and craft.get("ok") is False:
            bad = [i for i in craft["items"] if i["level"] == "FAIL"]
            nxt += ["", f"⛔ 工艺审计未过（{len(bad)} 项，几何口径）："]
            for i in bad:
                nxt.append(f"· {i['name']}：{i['detail']}")
            if craft.get("must_fix"):
                # must_fix 是**条目 dict**，不是字符串数组。以前只有全绿时
                # 才不会走到这一行，多 panel 一出现红项就把 dict 拼进字符串、
                # 整条回包抛 TypeError —— AI 拿到的是崩溃而不是修法。
                fixes = [str(i.get("fix") or i.get("detail") or "")
                         for i in craft["must_fix"] if isinstance(i, dict)]
                fixes = [f for f in fixes if f]
                if fixes:
                    nxt.append("修法：「" + "」；「".join(fixes) + "」")
            nxt.append("**没过工艺审计就先别交付**：这些不是玄学，是每张图都会"
                       "犯的结构问题；改规格重画到 craft.ok=true 再给用户。")
        elif isinstance(craft, dict) and craft.get("ok") is True:
            m = craft.get("metrics") or {}
            if m.get("panels"):
                nxt.append(f"工艺审计通过：{m['panels']} 个 panel 逐格过"
                           f"（配色 / 尺度 / 折行 / 压线 / 区室 / 回边）")
            else:
                nxt.append(f"工艺审计通过：{m.get('colors')} 色 / 宽差 "
                           f"{m.get('w_ratio')}× / 折行 {m.get('n_wrapped')} 个 / "
                           f"回边 {m.get('n_reversed')} 条（{craft.get('honest_limit')}）")
        if q["fix_before_delivery"]:
            nxt += [
                "",
                f"⛔ 交付前必须修：{'、'.join(q['fix_before_delivery'])}。",
                "修法是**改规格重画**：缩短节点/边标签、改 direction（LR↔TB）、"
                "改 width_mm、或把图拆成两张，再调一次 diagram_svg 复测。",
                "不要去手改 SVG 字节，也不要靠缩小字号把重叠压下去 ——"
                "字号有 5 pt 物理下限，压下去只会把红项从碰撞门挪到字号门。",
            ]
        if q["not_auditable"]:
            nxt += [
                "",
                f"⚠️ 审不了（**不等于通过**）：{'、'.join(q['not_auditable'])}。"
                "交付时要把这几项未审如实告诉用户，别声称「图已审过」。",
            ]
        payload["next"] = "\n".join(nxt)
        if state_note is not None:
            # 状态信息只作 advisory 附带：ok / scope / checks 三条纪律一个字不改。
            payload["diagram_state"] = {
                "path": state_note.get("state_path", ""),
                "revision": state_note.get("revision"),
                "status": state_note.get("status", ""),
                "gaps": state_note.get("gaps", []),
                "present": state_note.get("present"),
                "hints": hints or {},
            }
            payload["next"] = (state_note.get("next", "") + "\n\n"
                               + payload["next"])
            # 双通道写后反馈（GitHub 调研的硬结论）：
            # ① 结构化通道 —— scene / spec / affected / diff / revision 给模型继续操作；
            # ② 视觉通道 —— SVG 是可读文本（不是 PNG 黑盒），附七道门与写后自检结论。
            # PNG 需浏览器，内核零依赖不强求；缺席如实写进 note，不假装给过图。
            payload["scene"] = state_note.get("scene", {})
            payload["receipt"] = {
                "revision": state_note.get("revision"),
                "affected": state_note.get("affected", {}),
                "diff": state_note.get("diff", []),
                "summary": state_note.get("summary", ""),
            }
            payload["visual"] = {
                "svg": payload.get("svg", ""),
                "layout": payload.get("layout", ""),
                "png": "",
                "png_note": "PNG 需浏览器；SVG 是可读文本，模型可直接解析后继续编辑。",
                "writeback": {
                    "structure_checks": payload.get("checks", {}),
                    "quality_tier": payload["quality"]["tier"],
                    "gates": payload["quality"]["gates"],
                    "not_auditable": payload["quality"]["not_auditable"],
                },
            }
        return json.dumps(payload, ensure_ascii=False, default=str)

    name = "diagram_svg"
    if registry.get(name) is not None:
        return []
    registry.register(Tool(
        name=name,
        description="画机制图（通路/流程/调控网络）成原生 SVG，零第三方依赖。"
                    "模型只声明规格（详口径见 spec 参数），几何由工具算；出图后立刻跑七道 SVG 质量门，"
                    "红项在 next 里给出改规格的重画指引。\n" + ACTION_GUIDE,
        parameters={"type": "object", "properties": {
            "spec": {"type": "object", "description": SPEC_GUIDE},
            "action": {"type": "object", "description": ACTION_GUIDE},
            "out": {"type": "string",
                    "description": "输出 .svg 路径；留空写 机制图.svg"}},
            # 两条入口二选一，都不再是必填；没给时 fn 自己回明确指引。
            "required": []},
        fn=_fn_diagram, domain="core", mutating=True, source="plugin",
        priority=DESK_TOOL_PRIORITY))
    return [name]


@register("@external/svg-diagram")
class SvgDiagram(DeskCapability):
    """常驻能力：机制图出图工具 + 规格口径单源。"""

    ALWAYS_ON = True
    id = "svg-diagram"
    kind = "tool"
    version = "1.0.0"
    config_schema: dict[str, dict] = {}
    ALWAYS_CONFIG: dict = {}
    SECTION_TEXTS = {_SECTION: SPEC_GUIDE}
    ESSENTIAL_TOOLS = ("diagram_svg",)

    def mount(self, cfg, ctx):
        self.cfg = dict(cfg or {})
        self.added = _register_tools(ctx, self.cfg)
        return [f"注册工具 {len(self.added)} 个：{'、'.join(self.added)}",
                "规格口径单源 SPEC_GUIDE",
                "质量结论走 quality/next，不改 ok/scope/checks"]

    def provides(self) -> list[str]:
        return ["always-on:svg-diagram", f"prompt:{_SECTION}",
                f"tools:{len(getattr(self, 'added', []))}/1"]