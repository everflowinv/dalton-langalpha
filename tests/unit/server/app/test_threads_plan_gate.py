"""The platform-plan gate in ``_handle_send_message``.

A user with no own key and no OAuth needs a plan. The platform saying they have
none is a 403 that points at provider setup; the platform not being asked is an
outage, and pointing a paying user at setup for it is the bug this guards.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER = "usr-plan-gate-001"


async def _send(access_tier):
    from src.server.app.threads import router
    from src.server.dependencies.usage_limits import ChatAuthResult, enforce_chat_limit

    app = create_test_app(router)
    app.dependency_overrides[enforce_chat_limit] = lambda: ChatAuthResult(
        user_id=USER, access_tier=access_tier, burst_slot_id="slot-1"
    )
    release = AsyncMock()
    with (
        patch("src.config.settings.HOST_MODE", "platform"),
        patch("src.server.services.runs.admission.RunScope.release_slot", new=release),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/threads/messages",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
    return resp, release


@pytest.mark.asyncio
async def test_an_unknown_tier_is_an_outage_not_a_missing_provider():
    resp, release = await _send(access_tier=None)

    assert resp.status_code == 503
    assert resp.headers["Retry-After"] == "15"
    assert resp.json()["detail"]["type"] == "service_unavailable"
    release.assert_awaited()


@pytest.mark.asyncio
async def test_a_tier_the_platform_denied_still_asks_for_a_provider():
    resp, release = await _send(access_tier=-1)

    assert resp.status_code == 403
    assert resp.json()["detail"]["type"] == "no_provider"
    release.assert_awaited()
