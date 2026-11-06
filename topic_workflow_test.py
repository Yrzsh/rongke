# -*- coding: utf-8 -*-
import json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from rongke import topic
CHECK = 0

def check(value, text):
    global CHECK
    CHECK += 1
    assert value, text
    print(f"[PASS] {CHECK} {text}")

def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        t = topic.ensure_topic("test", "研究", "问题", root=root)
        check(t["stage"] == "ideate" and t["next_step"] == "", "老课题默认构思且非完成度")
        raw = root / "test" / "topic.json"
        data = json.loads(raw.read_text(encoding="utf-8")); data["unknown"] = {"keep": True}
        raw.write_text(json.dumps(data), encoding="utf-8")
        topic.set_library("test", "paper-1", True, root=root)
        result = topic.update_topic("test", "新名", "新问题", root=root, stage="evidence", next_step="核对全文")
        check(result["unknown"]["keep"] and result["library"] == ["paper-1"], "未知字段与关联保留")
        check(result["stage"] == "evidence" and result["stage_updated"], "人工阶段及时间戳")
        check(not (root / "test" / "topic.json.tmp").exists(), "原子替换没有残留")
        for bad in ("bogus", "plan"):
            try:
                topic.update_topic("test", "新名", "新问题", root=root, stage=bad)
                raise AssertionError("非法阶段未拒绝")
            except ValueError:
                check(True, f"非法阶段 {bad} 被拒绝")
        try:
            topic.update_topic("test", "新名", "新问题", root=root, next_step="x"*241)
            raise AssertionError("过长下一步未拒绝")
        except ValueError:
            check(True, "下一步长度受限")
        ctx = topic.context_text(root=root, slug="test")
        check("文献与证据" in ctx and "核对全文" in ctx and "不代表完成度" in ctx, "上下文注入阶段与下一步")
        print(f"topic-workflow: {CHECK} assertions passed")

if __name__ == "__main__": main()
