# -*- coding: utf-8 -*-
"""切到 magpie 这一刀会不会伤到别的东西。

判据（每条都能变红）：
  ① provider 段三个键写对
  ② send_reasoning_effort 被关掉 —— 换到 magpie 后不该再由荣珂替上游决定
  ③ 盘上**没有** api_key（本仓规矩：只存变量名）
  ④ config.json 的**其它段一个都不丢**（不是整份重写）
  ⑤ 写前有备份
  ⑥ 负控：白名单外的键必须抛错，不能静默忽略
全部在临时目录里做，绝不碰真实 config.json。
"""
import json
import sys
import tempfile
from pathlib import Path

from rongke.config import save_provider_overrides
from rongke.magpie import target

FAILS = []


def ok(m):
    print("[ok]  ", m)


def bad(m):
    FAILS.append(m)
    print("[FAIL]", m)


root = Path(tempfile.mkdtemp(prefix="rongke_magpie_"))
cfg = root / "config.json"
cfg.write_text(json.dumps({
    "provider": {"name": "commandcode",
                 "base_url": "https://api.commandcode.ai/provider/v1",
                 "model": "deepseek/deepseek-v4.1-flash",
                 "reasoning_effort": "medium",
                 "send_reasoning_effort": True},
    "loop": {"max_steps": 40},
    "别的段": {"别丢": [1, 2, 3]},
}, ensure_ascii=False), encoding="utf-8")

ov = target("deepseek/deepseek-v4-pro").overrides()
res = save_provider_overrides(dict(ov), root=root)
print("写入结果:", str(res)[:160])
print()

after = json.loads(cfg.read_text(encoding="utf-8"))
p = after.get("provider") or {}

if p.get("base_url") == "http://127.0.0.1:3425/v1":
    ok("base_url 写对了")
else:
    bad(f"base_url 错：{p.get('base_url')}")

if p.get("model") == "deepseek/deepseek-v4-pro":
    ok("model 写对了")
else:
    bad(f"model 错：{p.get('model')}")

if p.get("api_key_env") == "MAGPIE_API_KEY":
    ok("api_key_env 写对了")
else:
    bad(f"api_key_env 错：{p.get('api_key_env')}")

if p.get("send_reasoning_effort") is False:
    ok("send_reasoning_effort 关掉了（不再替上游决定要不要思考）")
else:
    bad(f"★ send_reasoning_effort 没关：{p.get('send_reasoning_effort')}")

if "api_key" not in p:
    ok("盘上没有 api_key（只存变量名）")
else:
    bad("★ 把密钥写到盘上了")

if after.get("别的段", {}).get("别丢") == [1, 2, 3]:
    ok("其它段没被清掉（不是整份重写）")
else:
    bad(f"★ 别的段丢了：{after.get('别的段')}")

if (after.get("loop") or {}).get("max_steps") == 40:
    ok("loop 段原样保留")
else:
    bad("★ loop 段被改了")

if res.get("backup"):
    ok(f"写前备份了：{Path(res['backup']).name}")
else:
    bad("没有备份")

# 负控：白名单外的键必须被拒，不能静默忽略
try:
    save_provider_overrides({"evil_key": "x"}, root=root)
    bad("★ 白名单外的键居然写成功了")
except ValueError:
    ok("负控成立：白名单外的键被拒（不静默忽略）")

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项失败")
    sys.exit(1)
print("✔ 切换路径可用，且没有副作用")
