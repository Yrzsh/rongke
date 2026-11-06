# -*- coding: utf-8 -*-
"""magpie 通道 —— 把「模型从哪来」从手填三个字段收成一次选择。

## 为什么单独一层

magpie（https://github.com/yetone/magpie，MIT）是一个**本地模型网关**：
它在 `127.0.0.1:3425` 起一个服务，同时说 OpenAI Chat / OpenAI Responses /
Anthropic Messages / Gemini 四种协议，并在它们之间互转。荣珂只要把
`base_url` 指向它、`api_key` 随便填，就能用上 magpie 里配好的全部渠道。

这不是"又一个 base_url 预设"，而是把原先必须手改三处（base_url /
api_key_env / model）的事收成一次选择 —— 客户不该知道 `api_key_env`
是什么意思，更不该去查 magpie 的端口号。

## 两种形态，同一份契约

| 形态 | 网关地址 | 谁在跑 |
|---|---|---|
| 桌面版 / 本地 CLI | `http://127.0.0.1:3425` | 用户自己机器上的 magpie |
| Docker / 服务器 | `http://<host>:3425` | 客户内网的那台机器 |

所以 `endpoint` 是**参数**，不是常量：默认 loopback，Docker 形态传真地址。
本模块只负责校验与产出 provider 覆盖值，不自己起服务、不读密钥。

## 一条硬纪律

magpie 的网关**接受任意 key**（它靠 `MAGPIE_WEB_KEY` 管 Web 页，不管网关）。
荣珂这边的 `api_key` 仍然走"只存变量名、不落盘"的老规矩（见 config.py 的注释），
所以这里给的是一个**环境变量名**，不是值。
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

#: magpie 网关的默认端口。写死在这里的只有这一个数 —— 它是 magpie 的契约，
#: 不是荣珂可调的配置。要换端口请走 `endpoint` 参数，不要改这个常量。
DEFAULT_PORT = 3425

#: 网关接受任意 Bearer，但荣珂要求 `api_key` 非空才肯发请求
#: （`Provider.diagnose` 里 `if not self.api_key`）。
#: 所以这里给一个**真实存在的环境变量名**，用户设成任何非空值即可。
DEFAULT_KEY_ENV = "MAGPIE_API_KEY"

#: 网关上的占位 key。写进提示文案，不写进配置。
PLACEHOLDER_KEY = "magpie"

#: 走 magpie 时该关掉什么：magpie 会自己转发 `reasoning_effort`，
#: 但荣珂默认的 `commandcode` 通道**必须**带它（不带就静默不思考）。
#: 换成 magpie 后由 magpie 决定上游要什么，荣珂不再强加。
#: `user_agent` 不动 —— 那是 Cloudflare 那道非通用 UA 的坎，magpie 是本机进程，
#: 没有这道坎；而且它不在 `PROVIDER_WRITE_KEYS` 里，写了会被整体拒绝。
_COMMON: dict[str, object] = {
    "send_reasoning_effort": False,
    "stream_required": False,
}


class MagpieError(ValueError):
    """给用户看的错误：消息原样端到界面，不再加工。"""


@dataclass(frozen=True)
class MagpieTarget:
    """一个 magpie 网关地址，以及由它派生出的 provider 覆盖值。"""

    endpoint: str
    model: str
    key_env: str = DEFAULT_KEY_ENV

    def base_url(self) -> str:
        """荣珂要的 `base_url`：网关地址 + `/v1`（OpenAI 兼容路径）。"""
        base = self.endpoint.rstrip("/")
        # 用户可能已经带上了 /v1 —— 不要叠成 /v1/v1。
        if base.endswith("/v1"):
            return base
        return base + "/v1"

    def overrides(self) -> dict[str, object]:
        """要写进 `config.json` 的 `provider.*` 值。不含任何密钥。

        ★ 只产出 `PROVIDER_WRITE_KEYS` 内的键 —— 多给一个
          `save_provider_overrides` 就整体抛 ValueError（它的第 1 条硬规矩：
          白名单外一律报错，不静默忽略）。所以 `name` 不在这里：
          它只是个标签，写不进去也不影响任何行为。
        """
        out: dict[str, object] = {
            "base_url": self.base_url(),
            "api_key_env": self.key_env,
            "model": self.model,
        }
        out.update(_COMMON)
        return out

    def describe(self) -> str:
        """给人看的一行，说明往哪连、要设哪个变量。"""
        return (f"模型走 magpie 网关 {self.base_url()}（模型 {self.model}）；"
                f"把环境变量 {self.key_env} 设成任意非空值即可（magpie 网关接受任意 key）")


def _check_endpoint(endpoint: str) -> str:
    s = str(endpoint or "").strip()
    if not s:
        raise MagpieError("magpie 地址不能为空")
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https"):
        raise MagpieError(f"magpie 地址必须以 http:// 或 https:// 开头（收到 {s!r}）")
    if not u.hostname:
        raise MagpieError(f"magpie 地址缺主机名（收到 {s!r}）")
    try:
        port = u.port
    except ValueError as e:
        raise MagpieError(f"magpie 地址端口不合法：{e}") from e
    if port is None:
        # 补上默认端口，避免 base_url 落到 80/443 上 —— 那不会有 magpie 在听。
        host = u.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"                    # IPv6 字面量需要方括号
        u = u._replace(netloc=f"{host}:{DEFAULT_PORT}")
        s = urllib.parse.urlunsplit(u)
    return s


def target(model: str, endpoint: str = "") -> MagpieTarget:
    """造一个 magpie 连接目标。

    `endpoint` 留空 ⇒ 本机 magpie（`http://127.0.0.1:3425`）。
    Docker / 服务器形态请传 `http://<host>:3425`。
    `model` 是 magpie 里配好的模型名（例如 `deepseek/deepseek-v4-pro`），
    原样透传 —— 荣珂不校验它，因为模型清单归 magpie 管。
    """
    m = str(model or "").strip()
    if not m:
        raise MagpieError(
            "要指定用 magpie 里的哪个模型（例如 deepseek/deepseek-v4-pro）。"
            "先跑 `magpie quota` 或看 magpie 面板里的模型名，原样填进来。")
    ep = _check_endpoint(endpoint or f"http://127.0.0.1:{DEFAULT_PORT}")
    return MagpieTarget(endpoint=ep, model=m)


def _gateway_base(endpoint: str = "") -> str:
    """把用户给的地址收敛成网关根（不带 /v1），用于拼 /v1/models。"""
    url = (endpoint or f"http://127.0.0.1:{DEFAULT_PORT}").rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


def is_magpie_base(base_url: str) -> bool:
    """这个 `base_url` 是不是指向 magpie 网关？

    ★ 只看**是不是本机回环**不够 —— 用户可能自己跑了个别的服务在 3425 上。
      但反过来要求"必须能连上"也不行：诊断时网关可能正好没起，
      那一格就会显示"不是 magpie"，而它明明配的是。
      所以判据只认**地址形状**：端口等于 magpie 默认端口。
      这是个便宜的判据，答错的最坏后果是界面上少提示一句，
      不会导致任何致性后果（`target()` 才是真正的入口）。
    """
    s = str(base_url or "").strip()
    if not s:
        return False
    try:
        u = urllib.parse.urlsplit(s)
        return u.port == DEFAULT_PORT
    except ValueError:
        return False


def list_models(endpoint: str = "", timeout: float = 3.0) -> list[str]:
    """问网关要模型清单（`GET /v1/models`）。

    ★ 拿不到就回空列表，**不抛异常**。调用方（设置页）要的是
      "能点选就点选、拿不到就让用户手填"，不是一个红叉。
      为什么不是硬失败：magpie 的模型清单取决于用户在 magpie 里配了哪些渠道，
      空清单是合法状态，不该看起来像坏了。
    """
    import json
    import urllib.error
    import urllib.request

    url = _gateway_base(endpoint) + "/v1/models"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {PLACEHOLDER_KEY}",
        "User-Agent": "Rongke/0.0.1 (research agent)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:                                         # noqa: BLE001
        return []
    out: list[str] = []
    for it in (d.get("data") or []):
        if isinstance(it, dict):
            mid = str(it.get("id") or "").strip()
            if mid:
                out.append(mid)
        elif isinstance(it, str):
            out.append(it)
    return sorted(set(out))


def probe(endpoint: str = "", timeout: float = 3.0) -> tuple[bool, str]:
    """问一句"这里有 magpie 在听吗"。只看连通性，不发模型请求。

    返回 `(是否连通, 给人看的一句话)`。**不猜**：连不上就说连不上，
    不去试别的端口，也不说"可能没启动"这种模棱两可的话。
    """
    ep = endpoint or f"http://127.0.0.1:{DEFAULT_PORT}"
    url = _gateway_base(ep) + "/v1/models"
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {PLACEHOLDER_KEY}",
        "User-Agent": "Rongke/0.0.1 (research agent)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(4096)
        return True, f"magpie 网关在线（{url}）"
    except urllib.error.HTTPError as e:
        # 401/403 也算在线：服务在听，只是这个占位 key 不被接受。
        if e.code in (401, 403, 404):
            return True, f"magpie 网关在线（{url}，HTTP {e.code}）"
        return False, f"magpie 网关回了 HTTP {e.code}（{url}）"
    except Exception as e:                                    # noqa: BLE001
        return False, (f"连不上 magpie 网关（{url}）：{type(e).__name__}。"
                       "桌面版请确认 magpie 已启动；Docker 形态请确认 -p "
                       f"{DEFAULT_PORT}:{DEFAULT_PORT} 已映射、地址填的是那台机器")
