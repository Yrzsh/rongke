# -*- coding: utf-8 -*-
"""导师模型（advisor profile）：把「某个真实导师的判断方式」做成可复用的判断层。

为什么要这一层（2026-11-14 续）
------------------------------
`mentor_review` 给的是**通用导师视角**：四个审查镜头 + 两个预演镜头，判据是
「这份稿子站不站得住」「这句追问你答不答得上」。但用户点的那两个场景里，
真正扎人的从来不是泛泛的错误 —— 是**他自己导师特有的那几问**：

  组会/开题前，「我导师会先问什么」比「审稿人可能问什么」准得多，
  因为那个人读了你的全部工作、有自己的偏好与雷区。

上游 `vendor/Supervisor.skill`（`create-supervisor`）就是这个问题的通用解法：
把导师的批注/组会/聊天蒸馏成 `Method Core + Academic Style + Persona +
Playbook` 四层，带证据来源与版本管理。本模块是它在荣珂里的**宿主半边**：

* 它只写**内容**（方法论 / 学术判准 / 追问习惯 / 务实路线），
  本模块负责**落盘、读回、校验、选镜头**——与 svgplot / typeset / pathway 同范式：
  模型做判断，内核做能复算的那一半。
* 生成物落在 `topics/advisors/<slug>/` —— 与 `topic.py` 同一棵树，
  跟着课题一起备份、一起「干净卸载」。**不散落在项目根**，这是与上游
  `./advisors/` 默认值的有意分歧：一个用户碰不到第二次的位置等于没做。

为什么要自己写、不 import 上游的 `tools/skill_writer.py`
--------------------------------------------------------
上游那套是「命令行工具 + 人类手动跑」，落盘位置、slug 规则、frontmatter 校验
都是它的私有约定（`require_folder_match` 要求目录名 == frontmatter name，
而荣珂的技能索引按目录名做 key）。本模块**复用它的产物契约**（四层文件 +
`meta.json` + `versions/`），但落盘走荣珂自己的原子写与路径校验，
这样两件事同时成立：上游 skill 照常可读，荣珂这边不引入第二套路径真相。

三态纪律（与本仓其它模块同源）
------------------------------
`UNVERIFIED` / 「没写」 ≠ 「不是这样」。素材不足时**宁可空着并写清楚**
（`evidence=template` / `material_sufficiency_score` 低），
也不许拿通用模板冒充「你导师就是这样」。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: 上游 `create-supervisor` 的四层产物。**单源**：写入、读回、校验共用这一张表，
#: 名字各写一遍就是 H 型缺陷（改一处忘一处）。
LAYERS: tuple[tuple[str, str, str], ...] = (
    ("method_core", "METHOD.md", "方法论核心",
     "怎么推进：任务拆解、优先级、风险分级、兜底路线"),
    ("academic", "ACADEMIC.md", "学术判准",
     "做什么：选题标准、实验规范、论文标准、里程碑、伦理红线"),
    ("persona", "PERSONA.md", "追问习惯与脾气",
     "怎么问：他会先问哪几类问题、在哪一步开始不耐烦、什么必须当场答清"),
    ("playbook", "PLAYBOOK.md", "过线路线",
     "怎么过线：最小可交付、保底方案、审稿最小改动（红线内）"),
)

#: 两层「工作镜头」，互斥。**这是上游最值钱的一个设计**：
#: 让它先表态「我要的是把方法做扎实，还是先把学位拿到手」，
#: 之后的每一条建议才不会自相矛盾。
MODES: dict[str, str] = {
    "academic_ideal": "学术理想型：优先方法严谨、长期价值、完整性",
    "graduation_first": "毕业优先型：在红线内优先可交付、先保底再冲刺",
}

#: 蒸馏策略。**只影响「怎么写」，不影响「写什么」** —— 上游把这两件事分开，
#: 是一个好设计：素材少的时候用模板打底不丢人，但必须**标出来**。
STRATEGIES: dict[str, str] = {
    "strict_distill": "strict_distill：只用用户素材，推不出来的写「待确认」",
    "hybrid_distill": "hybrid_distill：素材为主、模板补空位（补的必须标出来）",
    "template_first": "template_first：模板优先，只把素材当纠偏",
}

#: 每条事实的来源。**这一列是这个模块存在的理由**：没有它，
#: 「模板里的一句话」和「你导师真的这么说过」在文件里长得一模一样，
#: 而后者才是预演时敢拿来当判据的东西。
EVIDENCE = {
    "user": "用户直接说的/当场纠偏的",
    "material": "从导入材料（批注/组会/聊天/邮件）里读出来的",
    "template": "通用模板补的，**不是**这位导师说的",
    "merged": "素材与模板合并",
}

DEFAULT_VERDICT = "NOT_READY"

# ★★ 2026-11-15 真实使用跑出来的一类失效：模型把提示词里的**格式样本**
#   「先问对照组」当成了用户导师**真说过的话**，标成 source=user 存了进去。
#   于是用户后续拿到的「张老师的判准」里，第一条是他从来没说过的话 ——
#   而且它长得像真的，因为它标了 source=user。模板写得再好也不能是他的话。
#   所以给一个**合法的空状态**：没挖到就写这一行，不报缺口（否则等于逼着模型去填满）。
NO_MATERIAL = "未挖到材料"
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_LAYER_CHARS = 60000          # 单层上限：一条事实不该长到读不动
MAX_MATERIAL_BYTES = 2 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def advisors_root(root=None) -> Path:
    """导师模型住在**课题树**下：`topics/advisors/`。

    为什么不放项目根：`topics/` 是荣珂唯一的用户数据根（跟着备份、跟着清理），
    放外面就会变成「卸载后还留一地文件」。也不放 `~/.dsh`：那是 harness 的地盘，
    荣珂写进去就是两套真相。
    """
    from . import topic                                    # noqa: PLC0415
    return topic.topics_root(root) / "advisors"


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.fullmatch(str(slug or "")))


def slugify(name: str) -> str:
    """导师别名 → 目录名。非 ASCII 一律退化成 `advisor-<hash>`，**不赌拼音库**。

    上游用 `pypinyin`（可选依赖），荣珂零依赖，所以走确定性哈希：
    同名 ⇒ 同 slug（可复现、可回访），不同名 ⇒ 几乎不可能撞。
    """
    s = str(name or "").strip().lower()
    ascii_ok = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if ascii_ok and len(ascii_ok) <= 48:
        return ascii_ok
    import hashlib                                        # noqa: PLC0415
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:10]
    return f"advisor-{h}"


def _d(slug: str, root=None) -> Path | None:
    if not valid_slug(slug):
        return None
    base = advisors_root(root)
    d = base / slug
    # 越界防护：即使 slug 合法也再核一遍落点（符号链接 / 大小写怪癖）。
    try:
        d.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return d


def _atomic_write(path: Path, text: str) -> None:
    """原子写入 = 同目录**改名**（委托 `state_store.atomic_write_text` 单源实现）。"""
    from .state_store import atomic_write_text
    atomic_write_text(path, text, fsync=True)


# ── 读回 ────────────────────────────────────────────────────
@dataclass
class Advisor:
    slug: str
    name: str = ""
    version: str = "v1"
    mode: str = "academic_ideal"
    strategy: str = "hybrid_distill"
    sufficiency: int = 60
    layers: dict[str, str] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def text(self, layer: str) -> str:
        return self.layers.get(layer, "")

    def filled(self) -> list[str]:
        return [k for k, _, _, _ in LAYERS if self.layers.get(k, "").strip()]


def load(slug: str, root=None) -> Advisor | None:
    """读一个导师模型。**缺失层返回空串，不补模板** —— 缺就是缺。

    为什么不在读的时候补默认值：补过之后「他有这一层」和「内核替他编了这一层」
    再也分不开，而那正是这个模块唯一要保证的事。
    """
    d = _d(slug, root)
    if d is None or not d.is_dir():
        return None
    meta: dict = {}
    mp = d / "meta.json"
    if mp.is_file():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            meta = {}
    adv = Advisor(
        slug=slug,
        name=str(meta.get("name") or slug),
        version=str(meta.get("version") or "v1"),
        mode=str(meta.get("working_mode") or "academic_ideal"),
        strategy=str(meta.get("distill_strategy") or "hybrid_distill"),
        sufficiency=int(meta.get("material_sufficiency_score") or 0),
        meta=meta,
    )
    for key, filename, _, _ in LAYERS:
        p = d / filename
        if p.is_file():
            try:
                adv.layers[key] = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                adv.layers[key] = ""
    return adv


def list_all(root=None) -> list[dict]:
    base = advisors_root(root)
    if not base.is_dir():
        return []
    rows: list[dict] = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.is_symlink() or not valid_slug(d.name):
            continue
        a = load(d.name, root)
        if a is None:
            continue
        rows.append({"slug": a.slug, "name": a.name, "version": a.version,
                     "mode": a.mode, "strategy": a.strategy,
                     "sufficiency": a.sufficiency,
                     "layers": a.filled(),
                     "updated": str(a.meta.get("updated_at") or ""),
                     "corrections": int(a.meta.get("corrections_count") or 0)})
    return rows


# ── 校验（判据，不是装饰）────────────────────────────────────
def check_layer(key: str, text: str) -> list[str]:
    """单层的结构判据。返回**缺口清单**，空表示通过。

    这些不是格式洁癖，每一条都对应一种「写完了但用不上」：
      · 空层 ⇒ 预演时会静默回落到通用视角，而用户以为用的是自己导师；
      · 没有 `source=` ⇒ 模板内容会冒充导师原话（上游最核心的那条边界）；
      · 全是 template ⇒ 这份东西和通用导师视角没有区别，要说出来。
    """
    gaps: list[str] = []
    body = str(text or "").strip()
    if not body:
        return [f"{key}:空的"]
    # ★ 「没挖到就写没挖到」不是口号（隐私边界那段里写着它）。
    #   一层里只有一行「未挖到材料」，比装满了对不起来的条目要诚实得多，
    #   所以它**不报缺口** —— 报了就等于逼着模型去填满。
    if body == NO_MATERIAL:
        return []
    if len(body) > MAX_LAYER_CHARS:
        gaps.append(f"{key}:超过 {MAX_LAYER_CHARS} 字（读不动就等于没有）")
    if "source=" not in body:
        gaps.append(f"{key}:没有任何 source= 标注（模板内容会冒充导师原话）")
    if "template" in body and not re.search(r"source=(user|material|merged)", body):
        gaps.append(f"{key}:全是模板（与通用视角没有区别，要说出来）")
    return gaps


def audit(adv: Advisor | None) -> dict:
    """一个导师模型的完整读回。缺一层就记红，不静默跳过。"""
    if adv is None:
        return {"ok": False, "gaps": ["找不到这个导师模型"],
                "filled": [], "empty": [k for k, _, _, _ in LAYERS],
                "sufficiency": 0, "mode": "", "mode_label": ""}
    gaps: list[str] = []
    for key, _, _, _ in LAYERS:
        gaps += check_layer(key, adv.layers.get(key, ""))
    if adv.mode not in MODES:
        gaps.append(f"工作镜头未知：{adv.mode!r}")
    if not (0 <= adv.sufficiency <= 100):
        gaps.append(f"素材充分度越界：{adv.sufficiency}")
    if adv.sufficiency < 40 and adv.strategy == "strict_distill":
        # 上游的规矩：素材不足 40 分却声明「纯素材蒸馏」，等于把那 40 分以下的空白
        # 冒充成了「你已经了解他」。这不是错，是**自相矛盾**，所以记红。
        gaps.append("素材充分度 < 40 却声明 strict_distill（空的地方会被读成「他不这样」）")
    return {"ok": not gaps, "gaps": gaps,
            "filled": adv.filled(),
            "empty": [k for k, _, _, _ in LAYERS if k not in adv.filled()],
            "sufficiency": adv.sufficiency, "mode": adv.mode,
            "mode_label": MODES.get(adv.mode, ""),
            "strategy": adv.strategy,
            "version": adv.version}


# ── 写入 ────────────────────────────────────────────────────
def save(slug: str, *, name: str, layers: dict[str, str], mode: str = "",
         strategy: str = "hybrid_distill", sufficiency: int = 60,
         sources: list[str] | None = None, root=None) -> dict:
    """建/改一个导师模型。**改之前先备份**，改完读回来核。

    与上游 `skill_writer.py` 的契约对齐（四层文件 + meta.json + versions/），
    但落盘用荣珂自己的原子写。`slug` 已存在 ⇒ 走更新（升版本、留快照）；
    不存在 ⇒ 新建。
    """
    if not valid_slug(slug):
        return {"ok": False, "error": "非法的导师名（只能小写字母/数字/连字符）"}
    base = advisors_root(root)
    d = _d(slug, root)
    if d is None:
        return {"ok": False, "error": "落点越界，已拒绝"}
    exists = d.is_dir() and any(d.iterdir())
    old = load(slug, root) if exists else None
    merged_mode = mode or (old.mode if old else "academic_ideal")
    if merged_mode not in MODES:
        return {"ok": False, "error": f"工作镜头只能是 {'/'.join(MODES)}"}

    if exists and old is not None:
        _backup(d, old.version)

    written: dict[str, int] = {}
    for key, filename, _, _ in LAYERS:
        body = str((layers or {}).get(key, "") or "").strip()
        if not body and old is not None:
            body = old.layers.get(key, "").strip()   # 没给的层保持原样，**不清空**
        if not body:
            continue
        _atomic_write(d / filename, body + "\n")
        written[key] = len(body)

    version = "v1"
    if old is not None:
        m = re.match(r"v(\d+)", old.version)
        version = f"v{int(m.group(1)) + 1}" if m else "v2"
    meta = dict(old.meta) if old else {}
    meta.update({
        "slug": slug, "name": str(name or (old.name if old else slug)),
        "version": version, "working_mode": merged_mode,
        "distill_strategy": strategy,
        "material_sufficiency_score": max(0, min(100, int(sufficiency))),
        "sources": list(sources or meta.get("sources") or []),
        "corrections_count": int(meta.get("corrections_count") or 0),
        "updated_at": _now(), "schema_version": 2, "host": "rongke",
    })
    meta.setdefault("created_at", meta["updated_at"])
    _atomic_write(d / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")

    back = load(slug, root)
    rep = audit(back)
    # ★ 三件事必须分开报（这一点是本仓反复吃过的 M 型）：
    #   ① 写成了吗（`written` + `verified`）、
    #   ② 读回来完整吗（`audit.ok`）、
    #   ③ 不完整在哪（`audit.gaps`）。
    #   `ok` 只回答①—— 因为②本来就是**常态**：一位导师刚建时
    #   四层只有一层是正常的。把它读成「保存失败」会让模型重试到天荒，
    #   而真正需要它做的事是 —— 把 `gaps` 原文告诉用户。
    return {"ok": back is not None, "slug": slug, "version": version,
            "created": not exists, "written": written,
            "verified": back is not None, "audit": rep,
            "complete": bool(rep["ok"]),
            "path": str(d)}


def _backup(d: Path, version: str) -> Path:
    vdir = d / "versions"
    vdir.mkdir(parents=True, exist_ok=True)
    target = vdir / re.sub(r"[^A-Za-z0-9._-]", "_", version or "v1")
    if target.exists():
        target = vdir / f"{target.name}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    target.mkdir(parents=True, exist_ok=True)
    for _, filename, _, _ in LAYERS:
        src = d / filename
        if src.is_file():
            shutil.copy2(src, target / filename)
    mp = d / "meta.json"
    if mp.is_file():
        shutil.copy2(mp, target / "meta.json")
    return target


def versions(slug: str, root=None) -> list[dict]:
    d = _d(slug, root)
    if d is None or not (d / "versions").is_dir():
        return []
    rows = []
    for p in sorted((d / "versions").iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_dir() and not p.is_symlink():
            rows.append({"version": p.name,
                         "files": sorted(f.name for f in p.iterdir() if f.is_file()),
                         "at": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")})
    return rows


def rollback(slug: str, version: str, root=None) -> dict:
    """回滚到某个快照。**回滚前先备份当前状态** —— 回滚也是一次写操作。"""
    d = _d(slug, root)
    if d is None:
        return {"ok": False, "error": "非法的导师名"}
    src = d / "versions" / re.sub(r"[^A-Za-z0-9._-]", "_", str(version or ""))
    if not src.is_dir():
        return {"ok": False, "error": f"找不到版本：{version}"}
    cur = load(slug, root)
    if cur is not None:
        _backup(d, f"before_rollback_{datetime.now().strftime('%Y%m%d%H%M%S')}")
    restored = []
    for _, filename, _, _ in LAYERS:
        s = src / filename
        if s.is_file():
            _atomic_write(d / filename, s.read_text(encoding="utf-8"))
            restored.append(filename)
    mp = src / "meta.json"
    if mp.is_file():
        _atomic_write(d / "meta.json", mp.read_text(encoding="utf-8"))
    return {"ok": True, "slug": slug, "restored": restored, "from": version}


def delete(slug: str, root=None, *, confirm: bool = False) -> dict:
    """**不可逆**：明确要求 `confirm=True` 才动。挪到回收站而不是直接删。"""
    d = _d(slug, root)
    if d is None or not d.is_dir():
        return {"ok": False, "error": "找不到这个导师模型"}
    if not confirm:
        return {"ok": False, "error": "删除需要显式确认（confirm=True）", "path": str(d)}
    trash = advisors_root(root) / "_deleted"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{slug}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    shutil.move(str(d), str(dest))
    return {"ok": True, "moved_to": str(dest)}


# ── 接进导师模式：选镜头 + 生成提示段 ─────────────────────────
def lens_for(goal: str, topic_stage: str = "") -> str:
    """这句话该用哪一层。**窄判据**：认不出就返回空串，不硬套。

    与 `mentor_review.is_rehearsal_text` 同一纪律：词表要窄，否则
    「帮我改一下」会被当成开题论证，导师模型开始答非所问。
    """
    g = str(goal or "")
    if not g.strip():
        return ""
    if re.search(r"开题|proposal|选题论证|中期|答辩|预答辩", g):
        return "academic"
    # 理解课题（开题前的理解验收）走学术判准层；「课题组」是研究组，不算。
    if re.search(r"(理解|吃透|搞懂|弄懂).{0,4}(课题(?!组)|本题|这个题|研究方向)", g):
        return "academic"
    if re.search(r"组会|汇报|讲一遍|要讲|预讲|试讲|解答|答疑", g):
        return "persona"
    if re.search(r"回复|审稿意见|rebuttal|revision", g, re.I):
        return "playbook"
    if re.search(r"进度|deadline|截稿|毕业|过线|最低|保底|多久", g):
        return "playbook"
    if re.search(r"实验|设计|方案|对照|样本|功效|统计|重复", g):
        return "method_core"
    return ""


_STAGE_LENS = {"ideate": "academic", "design": "method_core",
               "evidence": "academic", "write": "academic",
               "submit": "playbook", "rebut": "playbook"}


def context_text(slug: str, goal: str = "", root=None,
                 topic_stage: str = "", max_chars: int = 3200) -> str:
    """注入首条消息的「你导师的那一层」。

    ★ 三条硬纪律，都来自本仓踩过的坑：

    ① **没选导师就一个字都不注入**。空壳比没有更糟：它会让人以为
       「内核已经在用我导师的判准了」。
    ② **只注入命中的那一层**，不把四层全塞进去。理由是同一句老话：
       5826 字的课题上下文遮住了「你好」（2026-11-03）。而且四层全进会让
       模型分不清这次该按哪一层的标准判断。
    ③ **判定要能说出来**。提示段必须写明「这一层是素材还是模板」
       （`source=` 统计），否则模型会把模板内容当成导师原话复述给用户。
    """
    adv = load(slug, root)
    if adv is None:
        return ""
    # ★ 先认目标里的词（说得出来的意图最准），**认不出才**退到课题阶段。
    #   不能写成 `lens_for(goal) or stage_lens` —— 那么写是对的，但下面那一行
    #   `if not lens` 会把两者混在一起判；分开写才能说清楚「这一层是怎么选上的」。
    lens = lens_for(goal)
    if not lens:
        lens = _STAGE_LENS.get(str(topic_stage or ""), "")
    if not lens or not adv.text(lens).strip():
        # 命中不了任何一层 ⇒ 宁可不说，也不要拿别的层凑数。
        if not any(adv.text(k).strip() for k, _, _, _ in LAYERS):
            return ""
        lens = next((k for k, _, _, _ in LAYERS if adv.text(k).strip()), "")
        if not lens:
            return ""
    label = dict((k, t) for k, _, t, _ in LAYERS)[lens]
    body = adv.text(lens).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + f"\n…（已截断，全文 {len(adv.text(lens))} 字，需要时用 advisor_read 读）"
    n_tpl = len(re.findall(r"source=template", body))
    n_real = len(re.findall(r"source=(user|material|merged)", body))
    head = [f"═══ 本次按「{adv.name}」的判断方式（{adv.version}）═══",
            f"命中层：{label}（{MODES.get(adv.mode, adv.mode)}）"]
    head.append(f"素材标记：{n_real} 条有来源，{n_tpl} 条是模板补的。"
                + ("　※ 模板补的那几条**不是**他真说过的话，别当成他的原话复述。"
                   if n_tpl else ""))
    head.append("下面是他自己的判准，不是通用建议。用它判断，但**不要冒充他本人**；"
                "素材没覆盖到的地方如实说「这一层没材料」，不要编。")
    if adv.sufficiency < 40:
        head.append(f"※ 素材充分度 {adv.sufficiency}/100，偏低 —— 拿不准的地方要标出来。")
    return "\n".join(head) + "\n\n" + body


def ingest_material(paths: list[str], *, max_bytes: int = MAX_MATERIAL_BYTES) -> dict:
    """把一批材料读成「不可信证据」。

    与上游 `material_normalizer.py` 同一条边界，但**判据写在返回值里**
    而不是只打一行警告：调用方必须拿到 `flagged_lines`（像指令的行）
    与 `hashes`（材料指纹），否则「导入过哪些材料」无法核对。

    这里只做**归并 + 标注**，不做蒸馏 —— 蒸馏是模型的判断，内核不越界。
    """
    out: list[dict] = []
    total = 0
    for raw in paths:
        p = Path(str(raw or "")).expanduser()
        row: dict = {"path": str(p), "ok": False}
        if not p.is_file():
            row["error"] = "文件不存在"
            out.append(row)
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            row["error"] = f"读不动：{e}"
            out.append(row)
            continue
        if len(data) > max_bytes:
            row["error"] = f"超过 {max_bytes} 字节上限"
            out.append(row)
            continue
        import hashlib                                    # noqa: PLC0415
        row["sha256"] = hashlib.sha256(data).hexdigest()
        row["bytes"] = len(data)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
            row["replacement_chars"] = text.count("\ufffd")
        row["chars"] = len(text)
        row["flagged_lines"] = _instruction_like(text)
        row["ok"] = True
        total += len(text)
        out.append(row)
    return {"ok": any(r["ok"] for r in out), "items": out, "total_chars": total,
            "n_files": sum(1 for r in out if r["ok"]),
            "untrusted": True}


_INSTRUCTION_LIKE = tuple(re.compile(p, re.I) for p in (
    r"ignore\s+(all\s+)?previous", r"system\s+prompt", r"reveal\s+(the\s+)?(prompt|secret|token|password)",
    r"run\s+(this\s+)?(command|script)", r"install\s+(this\s+)?(package|dependency)",
    r"忽略.{0,12}(之前|以上|前面).{0,8}(指令|规则)", r"(泄露|显示|输出).{0,8}(系统提示|密钥|密码)",
    r"(执行|运行).{0,8}(命令|脚本)", r"安装.{0,8}(依赖|软件包)",
))


def _instruction_like(text: str) -> list[int]:
    """材料里「像指令」的行号。**只报不动** —— 内容不能改流程。"""
    return [i for i, line in enumerate(str(text or "").splitlines(), 1)
            if any(rx.search(line) for rx in _INSTRUCTION_LIKE)][:50]


def layer_brief() -> str:
    """四层各自的写作要求。**单源**：提示词与文档都从这里取。"""
    return "\n".join(
        f"- `{filename}`（{title}）：{what}" for _, filename, title, what in LAYERS
    )
