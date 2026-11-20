# -*- coding: utf-8 -*-
"""工作区级持久化原语：原子 JSON 状态存储 + 只追加 JSONL 事件台账。

把 `diagram_state` / `pathway` / `aideck` / `typeset` / `figure_registry` 的原子落盘、
乐观锁、事务提交、快照回滚，以及 `lab` / `methods` / `advisor` 的 JSONL 追加、
防 `seq` 覆盖守卫、写后读回抽成两套可复用原语。

硬纪律：
  * 零第三方依赖（纯标准库）。
  * 全程零删除调用：所有原子替换与回退均走 `os.replace()` + 固定停放名，不产生删除计费。
  * 写入统一使用 UTF-8 + `newline=""` 保持原样换行并做写后读回校验。
"""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable


def atomic_write_text(
    path: "str | Path",
    text: str,
    *,
    fsync: bool = False,
) -> dict[str, Any]:
    """原子写入文本并做写后读回校验（tmp + `os.replace` + readback）。"""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        if fsync:
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp, dest)
    back = dest.read_text(encoding="utf-8")
    equal = back == text
    return {
        "ok": equal,
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "readback_equal": equal,
    }


class JsonStateStore:
    """工作区级 JSON 状态文件存储（原子写回 / 乐观锁 / 快照 / 事务提交）。"""

    def __init__(
        self,
        path: "str | Path",
        *,
        schema_version: int = 1,
        empty_factory: Callable[[], dict[str, Any]] | None = None,
        fsync: bool = False,
    ) -> None:
        self.path = Path(path)
        self.schema_version = int(schema_version)
        self._empty_factory = empty_factory
        self.fsync = bool(fsync)

    def _default_state(self) -> dict[str, Any]:
        if self._empty_factory is not None:
            return copy.deepcopy(self._empty_factory())
        return {
            "schema_version": self.schema_version,
            "revision": 0,
            "checkpoints": [],
        }

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> tuple[dict[str, Any], str]:
        """读盘状态，返回 `(state, status)`。

        status 取值：
          * `"absent"`  —— 文件不存在，返回空初态。
          * `"ok"`      —— JSON 合法且为 dict。
          * `"error:*"` —— 读盘或解析失败（明说，不静默当空）。
        """
        if not self.path.is_file():
            return self._default_state(), "absent"
        try:
            raw = self.path.read_text(encoding="utf-8")
            obj = json.loads(raw)
        except (OSError, ValueError) as exc:
            return self._default_state(), f"error:{type(exc).__name__}:{exc}"
        if not isinstance(obj, dict):
            return self._default_state(), "error:TypeError:状态文件顶层必须是 JSON 对象"
        return obj, "ok"

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        blob = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        try:
            rep = atomic_write_text(self.path, blob, fsync=self.fsync)
        except OSError as exc:
            return {
                "ok": False,
                "path": str(self.path),
                "bytes": 0,
                "readback_equal": False,
                "revision": state.get("revision"),
                "error": f"{type(exc).__name__}: {exc}",
            }
        rep["revision"] = state.get("revision")
        return rep

    @staticmethod
    def check_revision(
        state: dict[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any] | None:
        if expected_revision is None:
            return None
        cur = state.get("revision")
        if cur != expected_revision:
            return {
                "ok": False,
                "applied": False,
                "code": "REVISION_CONFLICT",
                "error": f"版本冲突：期望 revision={expected_revision}，当前为 {cur}",
                "expected_revision": expected_revision,
                "current_revision": cur,
            }
        return None

    @staticmethod
    def snapshot(
        state: dict[str, Any],
        label: str = "",
        *,
        max_checkpoints: int = 30,
    ) -> dict[str, Any]:
        """在 `state["checkpoints"]` 里追加一条深拷贝快照（不含嵌套 checkpoints）。"""
        cps = list(state.get("checkpoints") or [])
        snap_body = {k: copy.deepcopy(v) for k, v in state.items() if k != "checkpoints"}
        cp_id = f"cp-{len(cps) + 1:03d}"
        entry = {
            "id": cp_id,
            "label": str(label or "").strip() or cp_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "revision": state.get("revision", 0),
            "state": snap_body,
        }
        cps.append(entry)
        if max_checkpoints > 0 and len(cps) > max_checkpoints:
            cps = cps[-max_checkpoints:]
        state["checkpoints"] = cps
        return entry

    @staticmethod
    def restore(
        state: dict[str, Any],
        checkpoint_id: str,
    ) -> dict[str, Any] | None:
        """按 id 或 label 恢复快照，保留 checkpoints 列表并递增 revision。"""
        target = str(checkpoint_id or "").strip()
        if not target:
            return None
        cps = list(state.get("checkpoints") or [])
        hit = next(
            (c for c in reversed(cps) if c.get("id") == target or c.get("label") == target),
            None,
        )
        if hit is None:
            return None
        restored = copy.deepcopy(hit.get("state") or {})
        restored["checkpoints"] = copy.deepcopy(cps)
        restored["revision"] = int(state.get("revision", 0)) + 1
        return restored

    def transaction(
        self,
        fn: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """整笔事务：读盘 → 校验版本 → 深拷贝执行 → sidecar 写回校验 → `os.replace` 原子提交。

        任意一步失败，目标状态文件一个字节不动；若提交前失败，sidecar 挪到固定停放文件
        `.{name}.tx.last.json`（零删除调用）。
        """
        state, status = self.load()
        if status.startswith("error:"):
            return {
                "ok": False,
                "applied": False,
                "path": str(self.path),
                "error": f"状态文件损坏：{status}",
            }
        conflict = self.check_revision(state, expected_revision)
        if conflict is not None:
            conflict["path"] = str(self.path)
            return conflict

        working = copy.deepcopy(state)
        try:
            updated = fn(working)
        except Exception as exc:                              # noqa: BLE001
            return {
                "ok": False,
                "applied": False,
                "path": str(self.path),
                "revision": state.get("revision"),
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(updated, dict):
            return {
                "ok": False,
                "applied": False,
                "path": str(self.path),
                "revision": state.get("revision"),
                "error": "事务回调必须返回状态 dict",
            }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tx_path = self.path.parent / f".{self.path.name}.tx.json"
        park_path = self.path.parent / f".{self.path.name}.tx.last.json"
        blob = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
        try:
            with open(tx_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(blob)
                if self.fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
            if tx_path.read_text(encoding="utf-8") != blob:
                os.replace(tx_path, park_path)
                return {
                    "ok": False,
                    "applied": False,
                    "path": str(self.path),
                    "revision": state.get("revision"),
                    "error": "事务暂存文件读回不一致，已中止提交",
                }
            os.replace(tx_path, self.path)
            back = self.path.read_text(encoding="utf-8")
            equal = back == blob
        except OSError as exc:
            if tx_path.is_file():
                try:
                    os.replace(tx_path, park_path)
                except OSError:
                    pass
            return {
                "ok": False,
                "applied": False,
                "path": str(self.path),
                "revision": state.get("revision"),
                "error": f"事务提交失败：{type(exc).__name__}: {exc}",
            }

        return {
            "ok": equal,
            "applied": equal,
            "path": str(self.path),
            "bytes": self.path.stat().st_size,
            "readback_equal": equal,
            "revision": updated.get("revision"),
            "state": updated,
        }


class EventLogStore:
    """只追加 JSONL 事件台账存储（单调 `seq` / `retract` 守卫 / 写后读回）。"""

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def read_events_checked(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """读取全部事件，同时返回损坏行清单（不静默丢行）。"""
        if not self.path.is_file():
            return [], []
        evs: list[dict[str, Any]] = []
        bad: list[dict[str, Any]] = []
        try:
            raw_lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return [], [{"line": 0, "error": f"{type(exc).__name__}: {exc}"}]
        for idx, line in enumerate(raw_lines, 1):
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except ValueError as exc:
                bad.append({"line": idx, "error": str(exc), "raw": s[:120]})
                continue
            if not isinstance(obj, dict):
                bad.append({"line": idx, "error": "非 JSON 对象", "raw": s[:120]})
                continue
            evs.append(obj)
        return evs, bad

    def events(self) -> list[dict[str, Any]]:
        evs, _ = self.read_events_checked()
        return evs

    def seqs(self, evs: list[dict[str, Any]] | None = None) -> set[int]:
        rows = self.events() if evs is None else evs
        out: set[int] = set()
        for e in rows:
            try:
                out.add(int(e["seq"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def dead_seqs(
        self,
        evs: list[dict[str, Any]] | None = None,
        *,
        retract_kind: str = "retract",
    ) -> set[int]:
        rows = self.events() if evs is None else evs
        dead: set[int] = set()
        for e in rows:
            if e.get("kind") != retract_kind:
                continue
            t = e.get("target_seq")
            if t is None:
                continue
            try:
                dead.add(int(t))
            except (TypeError, ValueError):
                continue
        return dead

    def next_seq(self, evs: list[dict[str, Any]] | None = None) -> int:
        existing = self.seqs(evs)
        return (max(existing) + 1) if existing else 1

    def append(
        self,
        kind: str,
        fields: dict[str, Any] | None = None,
        *,
        retract_kind: str = "retract",
        validator: Callable[[dict[str, Any], list[dict[str, Any]]], str | None] | None = None,
    ) -> dict[str, Any]:
        """追加一条事件并做尾行读回校验。

        防 `seq` 覆盖守卫：
          * `kind == retract_kind` 且调用方误传 `seq` 而未传 `target_seq` ⇒ 自动搬到 `target_seq`。
          * 其余类型不允许调用方伪造 `seq`，由台账单调分配。
        """
        kind_s = str(kind or "").strip()
        if not kind_s:
            return {"ok": False, "applied": False, "error": "事件 kind 不能为空"}
        evs = self.events()
        seq = self.next_seq(evs)
        data = dict(fields or {})
        if "seq" in data:
            val = data.pop("seq")
            if kind_s == retract_kind and "target_seq" not in data:
                data["target_seq"] = val
        rec: dict[str, Any] = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind_s,
            **data,
        }
        if validator is not None:
            err = validator(rec, evs)
            if err:
                return {"ok": False, "applied": False, "error": str(err)}
        line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="") as fh:
                fh.write(line)
            tail = self.events()
            last = tail[-1] if tail else {}
            verified = last.get("seq") == seq and last.get("kind") == kind_s
        except OSError as exc:
            return {
                "ok": False,
                "applied": False,
                "seq": seq,
                "kind": kind_s,
                "verified": False,
                "error": f"台账写入失败：{type(exc).__name__}: {exc}",
            }
        return {
            "ok": verified,
            "applied": verified,
            "seq": seq,
            "kind": kind_s,
            "verified": verified,
            "record": rec,
            "path": str(self.path),
        }
