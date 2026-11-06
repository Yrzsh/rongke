# -*- coding: utf-8 -*-
"""荣珂计价 —— 把 token 用量换成钱，或者**诚实地承认换不了**。

## 一条纪律：宁可不出数，不出假数

未知模型一律返回字面 `"UNKNOWN"`，**绝不返回 0**。

为什么这不是小事：`0` 是"这次调用不要钱"这一**断言**，而 `UNKNOWN` 是"我不知道"这一**事实**。
把后者渲染成前者，等于把「没测」伪装成「免费」—— 那是 F 型缺陷（假安慰），
和"备份文件存在但内容与源相同"是同一个形状：**看起来正常，实际上零信息**。

## 两条必须说清的假设

**① 价格表默认是空的。** 荣珂的默认网关是第三方转售，其单价不该由内核猜。
    空表 ⇒ 全部 UNKNOWN ⇒ 界面上明晃晃地写着"成本未知，去填价格表"。
    这比填一个猜来的数字好得多：**猜来的成本会让人据此做决策。**

**② `cache_read` 是否已含在 `input_tokens` 里，各家不一样。**
    OpenAI 兼容口径：`prompt_tokens` **包含** cached tokens ⇒ 计费要减掉缓存部分。
    Anthropic 口径：`cache_read_input_tokens` 与 `input_tokens` **并列** ⇒ 不减。
    这是典型的 B 型缺陷温床（两处真相）。所以做成显式开关
    `cache_read_included_in_input`（默认 True，即 OpenAI 口径），
    并把生效的假设**写进返回值旁边**，不允许它悄悄生效。
"""
from __future__ import annotations

# 每 100 万 token 的美元单价。表为空是刻意的（见模块说明 ①）。
DEFAULT_TABLE: dict = {
    "models": {},
    "default": None,
    # True = cache_read 已含在 input_tokens 内（OpenAI 兼容口径），计费时减掉
    "cache_read_included_in_input": True,
}

UNKNOWN = "UNKNOWN"


def normalize_model(name: str) -> str:
    return (name or "").strip()


def _rates(table: dict, model: str) -> dict | None:
    """取该模型的单价。查不到返回 None（**不返回零价**）。"""
    table = table or {}
    models = table.get("models") or {}
    m = normalize_model(model)
    if m and m in models:
        return models[m] or None
    # 允许 `deepseek/*` 这种前缀通配（同一家不同版本价差大时不要用）
    for key, val in models.items():
        if key.endswith("*") and m.startswith(key[:-1]):
            return val or None
    d = table.get("default")
    return d or None


def _usage_field(usage, *names) -> int:
    for n in names:
        v = getattr(usage, n, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(n)
        if v is not None:
            try:
                return int(v)
            except Exception:
                continue
    return 0


def estimate_cost(usage, model: str, table: dict | None = None):
    """估算一次调用的美元成本。

    返回 float（成功）或字面 `"UNKNOWN"`（价格表里没有这个模型）。
    **不返回 0 来冒充已知** —— 0 只在"价格表里明确写了 0 元"时才可能返回。
    """
    rates = _rates(table if table is not None else DEFAULT_TABLE, model)
    if not rates:
        return UNKNOWN

    inp = _usage_field(usage, "input_tokens", "prompt_tokens")
    out = _usage_field(usage, "output_tokens", "completion_tokens")
    cr = _usage_field(usage, "cache_read")
    _ = _usage_field(usage, "cache_write")

    if (table or DEFAULT_TABLE).get("cache_read_included_in_input", True):
        billable_in = max(0, inp - cr)
    else:
        billable_in = inp

    try:
        per_in = float(rates.get("input", 0.0) or 0.0)
        per_out = float(rates.get("output", 0.0) or 0.0)
        per_cr = float(rates.get("cache_read", per_in) or 0.0)
    except Exception:
        return UNKNOWN

    cost = (billable_in * per_in + cr * per_cr + out * per_out) / 1_000_000.0
    return round(cost, 8)


def add_cost(a, b):
    """成本相加。任一侧 UNKNOWN ⇒ 结果 UNKNOWN（**不许把未知吞成已知**）。"""
    if a == UNKNOWN or b == UNKNOWN:
        return UNKNOWN
    if a is None:
        return b
    if b is None:
        return a
    try:
        return round(float(a) + float(b), 8)
    except Exception:
        return UNKNOWN


def format_cost(v, digits: int = 4) -> str:
    """给界面/日志用的一行。UNKNOWN 原样输出，**不替换成 0 或 —**。"""
    if v == UNKNOWN or v is None:
        return UNKNOWN
    try:
        f = float(v)
    except Exception:
        return UNKNOWN
    if f == 0:
        return "$0"
    if f < 10 ** (-digits):
        return f"<${10 ** (-digits):.{digits}f}"
    return f"${f:.{digits}f}"


def template(models: list[str]) -> dict:
    """给用户填价格表的骨架（把实际出现过的模型名列出来，省得手打错）。"""
    return {
        "models": {m: {"input": 0.0, "output": 0.0, "cache_read": 0.0}
                   for m in sorted(set(m for m in models if m))},
        "default": None,
        "cache_read_included_in_input": True,
    }
