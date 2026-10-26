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

FEISHU_ROUTES = frozenset(
    {
        "doc/read",
        "wiki/nodes",
        "wiki/node",
        "doc/create",
        "doc/append",
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
    "feishu_not_configured": "宿主机未配置飞书应用凭据（FEISHU_APP_ID / FEISHU_APP_SECRET 或 ~/.openclaw/openclaw.json）",
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
    base_url = get_dalton_bridge_base_url().rstrip("/")
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
        raise FeishuBridgeError("bridge_unreachable", type(exc).__name__) from exc
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
            raise FeishuBridgeError(
                str(error_body["error"]),
                str(error_body.get("message") or "")[:200],
                payload=error_body,
            )
        raise FeishuBridgeError(f"http_{resp.status_code}")
    try:
        data = resp.json() if resp.content else {}
    except ValueError as exc:
        raise FeishuBridgeError("invalid_json") from exc
    if not isinstance(data, dict):
        raise FeishuBridgeError("invalid_json")
    return data
