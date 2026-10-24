"""A stored level grants only what its agreement still covers."""

from __future__ import annotations

import pytest

from src.server.services.trading_permission import (
    DEFAULT_TRADING_PERMISSION,
    TRADING_AGREEMENT_VERSION,
    TradingPermission,
    effective_permission,
)


@pytest.mark.parametrize("level", ["plan_first", "autonomous"])
def test_a_level_that_skips_approval_holds_only_under_the_current_agreement(level):
    assert effective_permission(level, TRADING_AGREEMENT_VERSION) is TradingPermission(level)
    assert effective_permission(level, TRADING_AGREEMENT_VERSION - 1) is DEFAULT_TRADING_PERMISSION
    assert effective_permission(level, None) is DEFAULT_TRADING_PERMISSION


@pytest.mark.parametrize("level", ["no_trading", "approve_each"])
def test_a_level_that_asks_needs_no_agreement(level):
    assert effective_permission(level, None) is TradingPermission(level)


@pytest.mark.parametrize("stored", [None, "", "yolo", 3, {}])
def test_anything_unreadable_asks(stored):
    assert effective_permission(stored, TRADING_AGREEMENT_VERSION) is DEFAULT_TRADING_PERMISSION
    assert DEFAULT_TRADING_PERMISSION.asks


# DALTON-HOOK(hide-trading): trading-permission API always returns no_trading on GET and 403 on PUT
@pytest.mark.asyncio
async def test_trading_permission_endpoints_enforce_no_trading_and_forbid_writes():
    from httpx import ASGITransport, AsyncClient

    from src.server.app.trading_permission import router
    from tests.conftest import create_test_app

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        get_resp = await client.get("/api/v1/users/me/trading-permission")
        assert get_resp.status_code == 200
        assert get_resp.json()["level"] == "no_trading"

        for level in ("no_trading", "approve_each", "plan_first", "autonomous"):
            put_resp = await client.put(
                "/api/v1/users/me/trading-permission",
                json={"level": level, "agreement_version": TRADING_AGREEMENT_VERSION},
            )
            assert put_resp.status_code == 403
            assert "disabled in Dalton" in put_resp.json()["detail"]
