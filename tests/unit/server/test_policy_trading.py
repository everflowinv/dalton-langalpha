"""The ``trading`` rule: no live order for the user is bound, called, relayed
or granted while it stands, an approval given before it included. Paper,
preview and staged orders stay, and nothing is read for a tool that places
no live order or when no policy is configured."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from tests.unit.server.policy_standing import standing_under
from src.config.models import PolicyLayer
from src.server.services.brokerage_capabilities import (
    LIVE_ORDER_GROUPS,
    trading_rule_withholds,
)
from src.server.services.egress import RelayError, RelayRejection, direct_tools
from src.server.services.egress.direct_tools import DirectMCPBinding
from src.server.services.policy import ALLOW_ALL, TRADING, merge
from src.server.services.trading_permission import (
    TRADING_AGREEMENT_VERSION,
    TradingPermission,
)
from tests.unit.server.services.test_direct_tool_approval_gate import (
    LIVE,
    PAPER_ORDER,
    _connected,
)
from tests.unit.server.services.test_direct_tool_budget import _entry, _order, _read
from tests.unit.server.services.test_egress_order_gate import (
    EXECUTION_HEADER,
    _attempt,
    _body,
    _prepare,
    _token,
)

pytestmark = pytest.mark.asyncio

POLICY = "src.server.services.policy"
NO_TRADING = merge(None, PolicyLayer.model_validate({"trading": False}))



async def test_the_withheld_groups_are_the_live_order_tools():
    assert LIVE_ORDER_GROUPS == {"trading"}
    for vendor, tool in (
        ("moomoo", "trading_order_place"),
        ("moomoo", "crypto_account_cancel_order"),
        ("robinhood", "exercise_option"),
        ("robinhood", "decline_trade_approval"),
        ("ibkr", "answer_order_question"),
    ):
        assert trading_rule_withholds(vendor, tool), tool
    for vendor, tool in (
        ("moomoo", "sim_trade_input_order"),
        ("robinhood", "review_equity_order"),
        ("ibkr", "create_order_instruction"),
        ("webull", "place_stock_instruction"),
        ("moomoo", "account_funds"),
    ):
        assert not trading_rule_withholds(vendor, tool), tool


async def test_a_tool_no_group_classifies_at_a_known_brokerage_is_withheld():
    """A tool a broker adds after our classification may place orders, so the
    rule holds it until it is classified into a group known to place none.
    A server that is not a known brokerage carries no orders the rule sees."""
    assert trading_rule_withholds("moomoo", "a_tool_added_later")
    assert trading_rule_withholds("ibkr", "provide_customer_feedback")  # UNCURATED
    assert not trading_rule_withholds(None, "place_order")
    assert not trading_rule_withholds("not_a_brokerage", "place_order")


async def test_the_rule_merges_strictly_and_is_reported():
    assert merge(PolicyLayer(), PolicyLayer.model_validate({"trading": False})).allows(TRADING) is False
    client = NO_TRADING.for_client()
    assert client["trading"] is False
    assert client["set_by"] == {TRADING: "organization"}


# --- the relay, the choke point of record -----------------------------------


def _live_order():
    """A live order frame with a valid approval: an attempt already submitting."""
    return {
        "body": _body(LIVE),
        "headers": {EXECUTION_HEADER: _token(tool=LIVE)},
        "attempt": _attempt(tool=LIVE),
    }


@pytest.mark.parametrize("policy", [NO_TRADING, None], ids=["denied", "unknown"])
async def test_an_approved_live_order_is_refused_at_the_relay(policy):
    with standing_under(policy) as asked, pytest.raises(RelayRejection) as refused:
        await _prepare(**_live_order())
    assert (refused.value.status, refused.value.code) == (403, RelayError.POLICY_WITHHELD)
    asked.assert_awaited_once()


async def test_the_same_order_passes_once_the_policy_is_lifted():
    with standing_under(ALLOW_ALL):
        prepared, _ = await _prepare(**_live_order())
    assert prepared.order is not None and prepared.order.tool == LIVE


@pytest.mark.parametrize("policy", [NO_TRADING, None], ids=["denied", "unknown"])
async def test_an_unclassified_brokerage_tool_is_refused_at_the_relay(policy):
    with standing_under(policy) as asked, pytest.raises(RelayRejection) as refused:
        await _prepare(body=_body("a_tool_added_later", {}))
    assert (refused.value.status, refused.value.code) == (403, RelayError.POLICY_WITHHELD)
    asked.assert_awaited_once()
    with standing_under(ALLOW_ALL):
        prepared, _ = await _prepare(body=_body("a_tool_added_later", {}))
    assert prepared.order is None


async def test_a_paper_order_is_relayed_without_asking_the_policy():
    with standing_under(NO_TRADING) as asked:
        prepared, _ = await _prepare(headers={EXECUTION_HEADER: _token()})
    assert prepared.order is not None
    asked.assert_not_awaited()


# --- binding a turn's tools, and each call ----------------------------------


async def test_live_order_tools_are_not_bound_for_the_turn(monkeypatch):
    monkeypatch.setattr(direct_tools, "EGRESS_RELAY_SECRET", "s")
    seen: dict = {}

    def _spy(by_server):
        seen.update({n: [t.name for t in e.tools] for n, e in by_server.items()})
        return {name: [] for name in by_server}, []

    monkeypatch.setattr(direct_tools, "admit_within_budget", _spy)
    by_server = {
        "moomoo": _entry(
            _order("trading_order_place"),
            _order("sim_trade_input_order"),
            _read("account_funds"),
            _read("a_tool_added_later"),
            vendor="moomoo",
        )
    }
    with standing_under(NO_TRADING):
        await direct_tools.prepare_direct_mcp_tools(
            user_id="u", workspace_id="w", grants={"moomoo": "g"}, by_server=by_server
        )
    # DALTON-HOOK(hide-trading): Dalton also excludes all order tools (including sim_trade_input_order).
    assert seen["moomoo"] == ["account_funds"]

    with standing_under(ALLOW_ALL):
        await direct_tools.prepare_direct_mcp_tools(
            user_id="u", workspace_id="w", grants={"moomoo": "g"}, by_server=by_server
        )
    assert "a_tool_added_later" in seen["moomoo"]
    assert "trading_order_place" not in seen["moomoo"]
    assert "sim_trade_input_order" not in seen["moomoo"]


async def test_a_turn_without_live_order_tools_does_not_ask(monkeypatch):
    monkeypatch.setattr(direct_tools, "EGRESS_RELAY_SECRET", "s")
    monkeypatch.setattr(
        direct_tools, "admit_within_budget", lambda b: ({n: [] for n in b}, [])
    )
    with standing_under(NO_TRADING) as asked:
        await direct_tools.prepare_direct_mcp_tools(
            user_id="u",
            workspace_id="w",
            grants={"moomoo": "g"},
            by_server={"moomoo": _entry(_read("account_funds"), vendor="moomoo")},
        )
    asked.assert_not_awaited()


@pytest.mark.parametrize("approval", [False, True], ids=["ungated", "approved"])
async def test_a_live_order_call_is_refused_with_the_reason(monkeypatch, approval):
    _connected(monkeypatch, [], order_approval={"live": approval})
    binding = DirectMCPBinding(user_id="u1")
    with standing_under(NO_TRADING):
        reason = await binding.check("moomoo", LIVE, approval)
    assert reason == (
        "No order was sent: your organization does not allow trading, so live "
        "orders and brokerage tools not known to be safe are withheld. Tell the user."
    )
    with standing_under(None):
        unknown = await binding.check("moomoo", LIVE, approval)
    assert unknown.startswith("No order was sent:") and "cannot be read" in unknown
    # Read by the agent: advice to try again only repeats the refused call.
    assert "again" not in (reason + unknown).lower()


async def test_an_unclassified_brokerage_call_is_refused(monkeypatch):
    _connected(monkeypatch, [])
    binding = DirectMCPBinding(user_id="u1")
    with standing_under(NO_TRADING):
        assert await binding.check("moomoo", "a_tool_added_later", True) is not None
    with standing_under(None):
        assert await binding.check("moomoo", "a_tool_added_later", True) is not None


async def test_a_paper_order_call_does_not_ask(monkeypatch):
    _connected(monkeypatch, [])
    binding = DirectMCPBinding(user_id="u1")
    with standing_under(None):
        assert await binding.check("moomoo", PAPER_ORDER, True) is None


# --- granting -------------------------------------------------------------


async def test_every_brokerage_sign_in_is_refused_in_dalton():
    from src.server.services.mcp_oauth import connect
    from src.server.services.mcp_saves import BROKERAGE_DISABLED_DETAIL

    # DALTON-HOOK(hide-trading): Dalton disables all brokerage OAuth sign-ins regardless of capabilities.
    with (
        patch("src.server.services.account.require_policy", new_callable=AsyncMock, return_value=NO_TRADING),
        patch.object(connect, "oauth_http_client", side_effect=RuntimeError("dialled")),
    ):
        for caps in (["account", "trading"], ["account", "paper_trading"]):
            with pytest.raises(HTTPException) as refused:
                await connect.start_sign_in(
                    "u", "moomoo", "https://mcp.moomoo.com/mcp",
                    return_to=None, web_origin=None, loopback_redirect=None,
                    granted_capabilities=caps,
                )
            assert refused.value.status_code == 403
            assert refused.value.detail == BROKERAGE_DISABLED_DETAIL


@pytest.mark.parametrize(
    "requested",
    [
        TradingPermission.AUTONOMOUS,
        TradingPermission.APPROVE_EACH,
        TradingPermission.PLAN_FIRST,
        TradingPermission.NO_TRADING,
    ],
)
async def test_every_trading_permission_update_is_refused_in_dalton(requested):
    from src.server.app import trading_permission as route

    # DALTON-HOOK(hide-trading): Dalton locks trading permission to NO_TRADING and refuses all updates (403).
    body = route.TradingPermissionUpdate(
        level=requested, agreement_version=TRADING_AGREEMENT_VERSION
    )
    with patch("src.server.services.account.require_policy", new_callable=AsyncMock, return_value=NO_TRADING):
        with pytest.raises(HTTPException) as e:
            await route.update_trading_permission(body, "u")
        assert e.value.status_code == 403
        assert "Trading permissions are disabled in Dalton" in str(e.value.detail)


async def test_nothing_configured_reads_nothing_at_the_relay():
    cache = MagicMock()
    with (
        patch("src.server.services.account.is_feature_enabled_system", lambda key: False),
        patch("src.server.services.membership.is_feature_enabled_system", lambda key: False),
        patch("src.server.services.account.deployment_policy", return_value=None),
        patch("src.utils.cache.redis_cache.get_cache_client", return_value=cache),
    ):
        prepared, _ = await _prepare(**_live_order())
    assert prepared.order is not None
    assert not cache.method_calls
