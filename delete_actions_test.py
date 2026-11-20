# -*- coding: utf-8 -*-
"""删除 / 撤回 / 重置全链路回归测试。

覆盖本轮补齐的「之前只能添加、不能删除」链路：
  1. 历史会话移出列表（`POST /api/run/drop`）：归档目录移入 `runs/_trash/`、
     仅在 ledger 里的老任务一并隐藏、运行中任务返回 409、非法路径返回 400。
  2. 课题删除（`POST /api/topic` `action="delete"`）：需 confirm=True，
     移入 `topics/_deleted/`，删当前课题时清空 CURRENT，删非当前课题保留 CURRENT。
  3. 导师档案删除（`POST /api/advisor` `action="delete"`）：需 confirm=True，
     移入 `topics/advisors/_deleted/` 并从清单消失。
  4. 方法台账作废（`methods._fold` 带出 `seq` + `POST /api/methods` `kind="retract"`）。
  5. 设置页重置自定义配置（`POST /api/settings/reset` 保留非 provider 段并备份）
     与命名 API 方案管理（`POST /api/settings/profiles` save / apply / delete）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import _tkit as T  # noqa: E402
from rongke import advisor as advisor_store  # noqa: E402
from rongke import config as config_mod  # noqa: E402
from rongke import methods as methods_store  # noqa: E402
from rongke import topic as topic_store  # noqa: E402
from rongke.memory import Memory  # noqa: E402
from rongke.web import server as server_mod  # noqa: E402

FAILS: list[str] = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print(f"[PASS] {name}")
    else:
        msg = f"[FAIL] {name}" + (f": {detail}" if detail else "")
        print(msg)
        FAILS.append(msg)


def _req(base: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rk_del_"))
    try:
        home_dir = tmp / "home"
        data_dir = tmp / "data"
        home_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)

        env_patch = {
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "RONGKE_DATA": str(data_dir),
            "RONGKE_NO_BOOT_VERIFY": "1",
        }
        with mock.patch.dict(os.environ, env_patch):
            runs_dir = data_dir / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)

            # 准备一个归档任务 + 一个仅存在于 ledger 的旧任务
            arch_id = "20261120-010101"
            arch_dir = runs_dir / arch_id
            arch_dir.mkdir(parents=True, exist_ok=True)
            (arch_dir / "session.json").write_text(json.dumps({
                "id": arch_id,
                "goal": "归档测试任务",
                "success": True,
                "steps": 1,
                "elapsed": 1.2,
                "ts_iso": "2026-11-20 01:01:01",
            }), encoding="utf-8")

            ledger_id = "20261119-090909"
            mem = Memory(data_dir / "memory")
            mem.runs_path.parent.mkdir(parents=True, exist_ok=True)
            mem.runs_path.write_text(json.dumps({
                "goal": "纯台账老任务",
                "success": True,
                "steps": 1,
                "elapsed": 0.5,
                "run_id": ledger_id,
                "workspace": str(runs_dir / ledger_id),
                "ts": 1763514549.0,
                "ts_iso": "2026-11-19 09:09:09",
            }, ensure_ascii=False) + "\n", encoding="utf-8")

            mgr = server_mod.Manager()
            with mock.patch.object(server_mod, "ws_dir", return_value=runs_dir), \
                 mock.patch.object(server_mod, "default_root", return_value=data_dir / "memory"):
                srv = server_mod.Server(("127.0.0.1", 0), server_mod.Handler)
                srv.mgr = mgr  # type: ignore[attr-defined]
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                base = f"http://127.0.0.1:{srv.server_port}"
                try:
                    # 1. 历史会话移出
                    st, res = _req(base, "GET", "/api/runs?n=20")
                    ids = [r["id"] for r in res.get("runs", [])]
                    check("runs_initial_list", st == 200 and arch_id in ids and ledger_id in ids, str(ids))

                    st, res = _req(base, "POST", "/api/run/drop", {"id": "../bad"})
                    check("drop_run_traversal_400", st == 400 and not res.get("ok"), str((st, res)))

                    mgr.active = arch_id
                    st, res = _req(base, "POST", "/api/run/drop", {"id": arch_id})
                    check("drop_run_active_409", st == 409 and not res.get("ok"), str((st, res)))
                    mgr.active = None

                    st, res = _req(base, "POST", "/api/run/drop", {"id": arch_id})
                    check("drop_run_archived_ok", st == 200 and res.get("ok") and res.get("moved"), str(res))
                    check("drop_run_moved_to_trash", not arch_dir.exists() and (runs_dir / "_trash").is_dir())

                    st, res = _req(base, "POST", "/api/run/drop", {"id": ledger_id})
                    check("drop_run_ledger_only_ok", st == 200 and res.get("ok") and not res.get("moved"), str(res))

                    st, res = _req(base, "GET", "/api/runs?n=20")
                    ids_after = [r["id"] for r in res.get("runs", [])]
                    check("runs_hidden_after_drop", arch_id not in ids_after and ledger_id not in ids_after, str(ids_after))

                    # 2. 课题删除
                    topic_store.ensure_topic("t-cur", title="当前课题", question="Q1")
                    topic_store.ensure_topic("t-other", title="另一个课题", question="Q2")
                    topic_store.set_current("t-cur")

                    st, res = _req(base, "POST", "/api/topic", {"action": "delete", "slug": "t-other"})
                    check("topic_delete_needs_confirm", st == 400 and not res.get("ok"), str((st, res)))

                    st, res = _req(base, "POST", "/api/topic", {"action": "delete", "slug": "t-other", "confirm": True})
                    check("topic_delete_non_current_ok",
                          st == 200 and res.get("ok") and res.get("current") == "t-cur",
                          str(res))

                    st, res = _req(base, "POST", "/api/topic", {"action": "delete", "slug": "t-cur", "confirm": True})
                    check("topic_delete_current_clears_current",
                          st == 200 and res.get("ok") and res.get("was_current") and res.get("current") == "",
                          str(res))
                    remain_slugs = [x["slug"] for x in topic_store.list_topics()]
                    check("topic_deleted_not_listed", remain_slugs == [], str(remain_slugs))
                    check("topic_moved_to_deleted", (data_dir / "topics" / "_deleted").is_dir())

                    # 3. 导师档案删除
                    advisor_store.save(
                        "prof-del",
                        layers={
                            "method_core": "核对原始图谱\n<!-- source=user -->",
                            "academic": "重视统计自由度\n<!-- source=user -->",
                            "persona": "直截了当\n<!-- source=user -->",
                            "playbook": "先给结论\n<!-- source=user -->",
                        },
                        name="待删导师",
                        mode="academic_ideal",
                    )
                    check("advisor_created", any(a["slug"] == "prof-del" for a in advisor_store.list_all()))
                    st, res = _req(base, "POST", "/api/advisor", {"action": "delete", "slug": "prof-del"})
                    check("advisor_delete_needs_confirm", st == 400 and not res.get("ok"), str((st, res)))
                    st, res = _req(base, "POST", "/api/advisor", {"action": "delete", "slug": "prof-del", "confirm": True})
                    check("advisor_delete_ok", st == 200 and res.get("ok"), str((st, res)))
                    check("advisor_deleted_not_listed",
                          not any(a["slug"] == "prof-del" for a in advisor_store.list_all()))

                    # 4. 方法台账 seq 与 retract
                    topic_store.ensure_topic("t-lab", title="方法测试", question="Q")
                    topic_store.set_current("t-lab")
                    st, res = _req(base, "POST", "/api/methods", {
                        "slug": "t-lab", "kind": "method", "id": "m-del",
                        "title": "待作废方法", "origin": "own",
                    })
                    check("method_create_ok", st == 200 and res.get("ok"), str((st, res)))
                    st, mget = _req(base, "GET", "/api/methods?slug=t-lab")
                    mlist = mget.get("methods", [])
                    check("method_folded_has_seq", len(mlist) == 1 and isinstance(mlist[0].get("seq"), int), str(mlist))
                    mseq = mlist[0]["seq"]
                    st, res = _req(base, "POST", "/api/methods", {
                        "slug": "t-lab", "kind": "retract", "seq": mseq, "why": "旧方案弃用",
                    })
                    check("method_retract_ok", st == 200 and res.get("ok"), str((st, res)))
                    st, mget2 = _req(base, "GET", "/api/methods?slug=t-lab")
                    check("method_retract_folded_out",
                          len(mget2.get("methods", [])) == 0 and mget2.get("n_retracted") == 1,
                          str(mget2))

                    # 5. 设置页：自定义配置重置 + 多 API 方案保存/切换/删除
                    cfg_path = data_dir / "config.json"
                    cfg_path.write_text(json.dumps({"deck": {"timeout": 99}}, indent=2), encoding="utf-8")
                    config_mod.save_provider_overrides({
                        "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1/chat/completions",
                        "send_reasoning_effort": False,
                    })
                    st, sview = _req(base, "GET", "/api/settings")
                    check("settings_has_custom_provider",
                          st == 200 and sview.get("has_custom_provider") is True
                          and sview["provider"]["model"] == "deepseek-chat"
                          and sview["provider"]["base_url"] == "https://api.deepseek.com/v1",
                          str(sview))

                    st, pres = _req(base, "POST", "/api/settings/profiles", {
                        "action": "save",
                        "name": "DeepSeek 官方",
                        "model": "deepseek-chat",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key_env": "DEEPSEEK_API_KEY",
                        "send_reasoning_effort": False,
                        "stream_required": False,
                        "max_tokens": 4096,
                    })
                    check("profile_save_ok", st == 200 and pres.get("ok"), str((st, pres)))
                    pid = pres["profile"]["id"]

                    st, rres = _req(base, "POST", "/api/settings/reset", {})
                    check("settings_reset_ok", st == 200 and rres.get("ok") and rres.get("changed"), str((st, rres)))
                    raw_after_reset = json.loads(cfg_path.read_text(encoding="utf-8"))
                    check("settings_reset_preserves_non_provider",
                          "provider" not in raw_after_reset and raw_after_reset.get("deck", {}).get("timeout") == 99,
                          str(raw_after_reset))

                    st, sview2 = _req(base, "GET", "/api/settings")
                    check("settings_after_reset_clears_flag",
                          st == 200 and sview2.get("has_custom_provider") is False
                          and any(p["id"] == pid for p in sview2.get("profiles", [])),
                          str(sview2))

                    st, ares = _req(base, "POST", "/api/settings/profiles", {"action": "apply", "id": pid})
                    st_after, sview3 = _req(base, "GET", "/api/settings")
                    check("profile_apply_ok",
                          st == 200 and ares.get("ok") and ares["provider"]["model"] == "deepseek-chat"
                          and st_after == 200
                          and sview3["provider"]["send_reasoning_effort"] is False,
                          str((st, ares, sview3)))

                    st, dres = _req(base, "POST", "/api/settings/profiles", {"action": "delete", "id": pid})
                    check("profile_delete_ok",
                          st == 200 and dres.get("ok") and dres.get("profiles") == [],
                          str((st, dres)))
                finally:
                    srv.shutdown()
                    srv.server_close()
    finally:
        T.stash(tmp, tag="delete_actions_test")

    print(f"\nChecked {N} assertions, failures: {len(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
