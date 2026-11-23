"""Backend client for the host bridge's Feishu OpenAPI proxy (`POST /feishu/*`).

All Feishu HTTP runs on the host inside the bridge; the backend holds only the
bridge admin token (never the Feishu app secret). Mirrors `post_bridge_notify`
in `src/dalton/notify/dispatch.py`.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.dalton.settings import get_dalton_bridge_admin_token, get_dalton_bridge_base_url

logger = logging.getLogger(__name__)

FEISHU_HTTP_TIMEOUT_S = 45.0

# Errors that occur after a connection was established and the request may have
# been transmitted/processed by the bridge (contrast with ConnectError/ConnectTimeout).
_POST_SEND_HTTP_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.DecodingError,
    httpx.CloseError,
)

_UNCERTAIN_BRIDGE_ERRORS: frozenset[str] = frozenset(
    {
        "feishu_network_error",
        "feishu_invalid_response",
        "feishu_empty_error_response",
        "invalid_json",
    }
)


def is_uncertain_bridge_error(error_code: str | None, outcome: str | None = None) -> bool:
    """Return whether a bridge failure leaves commit state unknown rather than definitely unwritten."""
    norm_outcome = str(outcome or "").strip().lower()
    if norm_outcome == "unknown":
        return True
    if norm_outcome:
        return False
    code = str(error_code or "").strip()
    return code in _UNCERTAIN_BRIDGE_ERRORS or code.startswith(("feishu_http_5", "http_5"))

FEISHU_ROUTES = frozenset(
    {
        "doc/read",
        "wiki/nodes",
        "wiki/node",
        "doc/create",
        "doc/append",
        # C1: re-grant only, scoped to docs the bridge itself created.
        "doc/grant",
        # notify-routes: the chat-sending bot's display name and its own chats,
        # for the settings page and route validation.
        "bot-info",
        "chats",
    }
)


class FeishuBridgeError(RuntimeError):
    """A `/feishu/*` bridge call failed (transport failure or non-ok payload).

    ``payload`` carries the bridge's structured error body when one was
    parseable (e.g. `content_insert_failed` with `message`/`document_id`/`url`);
    callers surface it instead of a generic status error.
    """

    def __init__(self, error_code: str, detail: str = "", payload: dict[str, Any] | None = None) -> None:
        super().__init__(detail or error_code)
        self.error_code = error_code
        self.payload = payload or {}


_ERROR_MESSAGES: dict[str, str] = {
    "feishu_not_configured": "宿主机未配置 Bridge 专用飞书应用凭据（请在 ~/.config/dalton/researchdata-bridge.env 中配置 FEISHU_APP_ID / FEISHU_APP_SECRET）",
    "feishu_network_error": "无法连接飞书开放平台",
    "feishu_auth_failed": "飞书应用凭据无效",
    "wiki_node_not_found": "飞书 wiki 节点不存在",
    "markdown_convert_empty": "markdown 转换后没有可插入的内容块",
    "bridge_unreachable": "无法连接宿主机 researchdata bridge",
    "invalid_json": "bridge 返回了无法解析的响应",
}


def describe_feishu_error(error_code: str) -> str:
    """Return a user-readable Chinese message for a bridge Feishu error code."""
    if error_code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[error_code]
    if error_code.startswith("feishu_http_"):
        return f"飞书接口返回错误（{error_code[len('feishu_http_') :]}）"
    if error_code.startswith("http_"):
        return f"bridge 请求失败（{error_code}）"
    return f"飞书调用失败（{error_code}）"


async def post_bridge_feishu(route: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call ``POST {bridge}/feishu/<route>`` with the admin token; return the JSON payload.

    Raises :class:`FeishuBridgeError` on transport failure, non-2xx, or invalid
    JSON; never raises with token material. A 2xx payload with ``ok: false`` is
    returned as-is for the caller to interpret.
    """
    if route not in FEISHU_ROUTES:
        raise ValueError(f"Unknown feishu bridge route: {route!r}")
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
        raise FeishuBridgeError("capability_disabled", "researchdata_bridge capability is disabled")
    base_url = get_dalton_bridge_base_url().rstrip("/")
    if not base_url:
        raise FeishuBridgeError("bridge_unconfigured", "researchdata bridge URL is not configured")
    token = get_dalton_bridge_admin_token()  # DALTON-HOOK(bridge-admin-token): /feishu/* are admin routes
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Dalton-Bridge-Token"] = token
    try:
        async with httpx.AsyncClient(timeout=FEISHU_HTTP_TIMEOUT_S, trust_env=False) as client:
            resp = await client.post(f"{base_url}/feishu/{route}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Feishu bridge call %s failed: %s", route, type(exc).__name__)
        err_payload: dict[str, Any] = {}
        if isinstance(exc, _POST_SEND_HTTP_ERRORS):
            err_payload["outcome"] = "unknown"
        raise FeishuBridgeError("bridge_unreachable", type(exc).__name__, payload=err_payload) from exc
    if resp.status_code == 401:
        raise FeishuBridgeError("bridge_unauthorized", "bridge rejected the admin token")
    if not resp.is_success:
        # The bridge's error bodies are structured (error/message, and for
        # content_insert_failed also document_id/url) — carry them through so
        # the agent tool can tell the user exactly what happened.
        try:
            error_body = resp.json() if resp.content else {}
        except ValueError:
            error_body = {}
        if isinstance(error_body, dict) and str(error_body.get("error") or "").strip():
            err_code = str(error_body["error"]).strip()
            err_payload = dict(error_body)
            if (
                not str(err_payload.get("outcome") or "").strip()
                and is_uncertain_bridge_error(err_code)
            ):
                err_payload["outcome"] = "unknown"
            raise FeishuBridgeError(
                err_code,
                str(error_body.get("message") or "")[:200],
                payload=err_payload,
            )
        unknown_payload = {"outcome": "unknown"} if resp.status_code >= 500 else {}
        raise FeishuBridgeError(f"http_{resp.status_code}", payload=unknown_payload)
    try:
        data = resp.json() if resp.content else {}
    except ValueError as exc:
        raise FeishuBridgeError("invalid_json", payload={"outcome": "unknown"}) from exc
    if not isinstance(data, dict):
        raise FeishuBridgeError("invalid_json", payload={"outcome": "unknown"})
    return data
