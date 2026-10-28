"""Dalton's OAuth routes must win over the upstream wildcard routes.

Upstream registers ``/api/v1/oauth/{key}/device/initiate`` (and ``/{key}`` /
``/{key}/status``) for codex/claude/grok; Dalton's muse/xai/antigravity routes
live under the same prefix. FastAPI matches in registration order, so Dalton's
router must be registered first — otherwise ``/oauth/muse/device/initiate``
lands on the upstream wildcard, whose ``_device_flow("muse")`` answers 404, and
``/oauth/muse/status`` bypasses Dalton's status projection.
"""

import pytest
from fastapi import HTTPException
from starlette.routing import Match

import src.dalton.app.oauth_native as oauth_native_mod
from src.server.app import oauth as oauth_app
from src.server.app import setup as setup_mod


def _first_matching_route(path: str, method: str):
    """The route the app would hand ``path`` to: first full match wins."""
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    for route in setup_mod.app.routes:
        match, _child_scope = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


@pytest.mark.parametrize(
    ("method", "path", "dalton_endpoint_name"),
    [
        ("POST", "/api/v1/oauth/muse/device/initiate", "muse_device_initiate"),
        ("POST", "/api/v1/oauth/muse/device/poll", "muse_device_poll"),
        ("GET", "/api/v1/oauth/muse/status", "muse_status"),
        ("POST", "/api/v1/oauth/xai/device/initiate", "xai_device_initiate"),
        ("POST", "/api/v1/oauth/xai/device/poll", "xai_device_poll"),
        ("GET", "/api/v1/oauth/xai/status", "xai_status"),
        ("POST", "/api/v1/oauth/antigravity/initiate", "antigravity_initiate"),
        ("POST", "/api/v1/oauth/antigravity/poll", "antigravity_poll"),
        ("POST", "/api/v1/oauth/antigravity/callback", "antigravity_callback"),
        ("GET", "/api/v1/oauth/antigravity/status", "antigravity_status"),
    ],
)
def test_dalton_oauth_routes_take_precedence(method, path, dalton_endpoint_name):
    """Every Dalton OAuth path resolves to Dalton's handler, never the
    upstream ``{key}`` wildcard."""
    route = _first_matching_route(path, method)
    assert route is not None, f"no route matches {method} {path}"
    endpoint = getattr(route, "endpoint", None)
    assert endpoint is getattr(oauth_native_mod, dalton_endpoint_name), (
        f"{method} {path} resolved to {endpoint!r}, not Dalton's {dalton_endpoint_name}"
    )


def test_upstream_codex_routes_still_resolve_upstream():
    """The reorder leaves upstream's own OAuth paths on upstream's handlers."""
    route = _first_matching_route("/api/v1/oauth/codex/device/initiate", "POST")
    assert route is not None
    assert getattr(route, "endpoint", None) is oauth_app.device_initiate


def test_grok_device_flow_refuses_in_dalton():
    """Item 8 rider: upstream's Grok (api.x.ai) device sign-in is not offered.

    ``_device_flow("grok")`` answers 404, so the device routes for it cannot
    start a sign-in Dalton never connects; the pinned grok-4.7/4.3 keys ride
    the xai-oauth line instead.
    """
    assert "grok" not in oauth_app._DEVICE_FLOWS
    with pytest.raises(HTTPException) as raised:
        oauth_app._device_flow("grok")
    assert raised.value.status_code == 404
