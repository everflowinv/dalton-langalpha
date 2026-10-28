"""The direct-tool gate: refuses on the port's word, passes everything else."""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from langgraph.errors import GraphBubbleUp

from ptc_agent.agent.middleware.direct_mcp import (
    METADATA_KEY,
    DirectMcpPolicyMiddleware,
    direct_tool_middleware,
    direct_tool_summary,
    refused_message,
)
from ptc_agent.agent.middleware.tool.error_handling import format_tool_error


def _tool(name: str, *, stamp: dict | None) -> StructuredTool:
    tool = StructuredTool.from_function(func=lambda: "ok", name=name, description="t")
    if stamp is not None:
        tool.metadata = {METADATA_KEY: stamp}
    return tool


class _Request:
    def __init__(self, name: str) -> None:
        self.tool_call = {"id": "call-1", "name": name, "args": {}}


class _ToolSet:
    def __init__(self, tools, reason=None):
        self.tools = tools
        self.reason = reason
        self.asked: list[tuple[str, str, bool]] = []

    async def check(self, server, tool, approval=False):
        self.asked.append((server, tool, approval))
        return self.reason


async def _ran(request):
    return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])


DIRECT = _tool(
    "mcp__moomoo__sim_trade_input_order",
    stamp={"server": "moomoo", "tool": "sim_trade_input_order"},
)

# Approval only ever rides on an order tool: nothing else is gated.
GATED = _tool(
    "mcp__moomoo__trading_order_place",
    stamp={
        "server": "moomoo",
        "tool": "trading_order_place",
        "order": {"action": "place", "mode": "live"},
        "approval": True,
    },
)


@pytest.mark.asyncio
async def test_unstamped_tool_passes_without_consulting_the_port():
    toolset = _ToolSet([DIRECT], reason="never")
    mw = DirectMcpPolicyMiddleware(toolset)
    out = await mw.awrap_tool_call(_Request("web_search"), _ran)
    assert out.content == "ran"
    assert toolset.asked == []


@pytest.mark.asyncio
async def test_direct_tool_runs_when_the_port_allows_it():
    toolset = _ToolSet([DIRECT])
    mw = DirectMcpPolicyMiddleware(toolset)
    out = await mw.awrap_tool_call(_Request(DIRECT.name), _ran)
    assert out.content == "ran"
    assert toolset.asked == [("moomoo", "sim_trade_input_order", False)]


@pytest.mark.asyncio
async def test_direct_tool_is_refused_with_the_ports_reason():
    mw = DirectMcpPolicyMiddleware(_ToolSet([DIRECT], reason="consent withdrawn"))
    out = await mw.awrap_tool_call(_Request(DIRECT.name), _ran)
    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert out.tool_call_id == "call-1"
    assert out.content == "Refused: consent withdrawn"
    # The refusal is the one result whose identity the surface cannot recover
    # from the alias, because it is also the one the vendor never answered.
    assert out.artifact["direct_mcp"] == {
        "server": "moomoo",
        "tool": "sim_trade_input_order",
    }


def test_sync_path_fails_closed_for_direct_tools_only():
    mw = DirectMcpPolicyMiddleware(_ToolSet([DIRECT]))
    ran = mw.wrap_tool_call(
        _Request("web_search"),
        lambda r: ToolMessage(content="ran", tool_call_id="call-1"),
    )
    assert ran.content == "ran"
    refused = mw.wrap_tool_call(
        _Request(DIRECT.name),
        lambda r: ToolMessage(content="ran", tool_call_id="call-1"),
    )
    assert refused.status == "error"
    assert refused.artifact["direct_mcp"]["server"] == "moomoo"


@pytest.mark.asyncio
async def test_a_permitted_call_carries_the_vendor_names_out():
    """The alias is lossy, so the result is where identity has to travel."""
    tool = _tool(
        "mcp__moomoo__sim_trade_input_order",
        stamp={"server": "moomoo", "tool": "sim_trade_input_order"},
    )
    mw = DirectMcpPolicyMiddleware(_ToolSet([tool], reason=None))

    async def handler(request):
        return ToolMessage(content="ok", tool_call_id="1")

    out = await mw.awrap_tool_call(_Request(tool.name), handler)
    assert out.artifact["direct_mcp"] == {
        "server": "moomoo",
        "tool": "sim_trade_input_order",
    }


@pytest.mark.asyncio
async def test_the_stamp_does_not_displace_an_artifact_the_tool_set():
    tool = _tool("mcp__moomoo__quote", stamp={"server": "moomoo", "tool": "quote"})
    mw = DirectMcpPolicyMiddleware(_ToolSet([tool], reason=None))

    async def handler(request):
        return ToolMessage(content="ok", tool_call_id="1", artifact={"rows": 3})

    out = await mw.awrap_tool_call(_Request(tool.name), handler)
    assert out.artifact["rows"] == 3
    assert out.artifact["direct_mcp"]["tool"] == "quote"


@pytest.mark.asyncio
async def test_a_raising_vendor_call_still_carries_the_vendor_names_out():
    # ToolErrorHandlingMiddleware is installed ahead of this one and the first
    # wrapper is the outermost, so it converts the exception only after this
    # frame unwound. Without converting here, a relay timeout is the one
    # result the surface renders under the alias instead of the vendor.
    mw = DirectMcpPolicyMiddleware(_ToolSet([DIRECT]))

    async def boom(request):
        raise TimeoutError("relay wall clock exceeded")

    out = await mw.awrap_tool_call(_Request(DIRECT.name), boom)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert out.tool_call_id == "call-1"
    assert out.artifact["direct_mcp"] == {
        "server": "moomoo",
        "tool": "sim_trade_input_order",
    }
    # Reported in the shape the converter would have used, so the two error
    # paths cannot drift into two different sentences for one failure.
    assert out.content == format_tool_error(
        TimeoutError("relay wall clock exceeded"), DIRECT.name
    )


@pytest.mark.asyncio
async def test_a_graph_interrupt_is_not_swallowed_as_a_tool_error():
    # The converter re-raises GraphBubbleUp because LangGraph control flow
    # travels as an exception; catching it here would strand the interrupt.
    mw = DirectMcpPolicyMiddleware(_ToolSet([DIRECT]))

    async def interrupt(request):
        raise GraphBubbleUp("resume me")

    with pytest.raises(GraphBubbleUp):
        await mw.awrap_tool_call(_Request(DIRECT.name), interrupt)


@pytest.mark.asyncio
async def test_a_policy_check_that_raises_is_stamped_like_the_call():
    # The check reads the connection row, so it fails the same transient ways
    # the vendor call does, and it sits before the handler: left outside the
    # guard it would be the one remaining path that unwinds without identity.
    class Broken(_ToolSet):
        async def check(self, server, tool, approval=False):
            raise ConnectionError("connection row unavailable")

    mw = DirectMcpPolicyMiddleware(Broken([DIRECT]))

    out = await mw.awrap_tool_call(_Request(DIRECT.name), _ran)

    assert out.status == "error"
    assert out.artifact["direct_mcp"]["tool"] == "sim_trade_input_order"
    assert "connection row unavailable" in out.content


class TestDirectToolSummaryImportHint:
    """A `both` tool's line has to name the symbol the wrapper actually defines."""

    def test_a_direct_only_tool_is_not_offered_an_import(self):
        line = direct_tool_summary(
            [
                _tool(
                    "mcp__moomoo__place",
                    stamp={
                        "server": "moomoo",
                        "tool": "trading_order_place",
                        "sandboxed": False,
                    },
                )
            ]
        )
        assert "importable" not in line

    def test_the_hint_uses_the_generated_name_not_the_vendor_name(self):
        # The wrapper is emitted under _safe_func_name, so a vendor name with
        # illegal characters is a different symbol in the module.
        line = direct_tool_summary(
            [
                _tool(
                    "mcp__acme__odd",
                    stamp={"server": "acme", "tool": "weird-name.v2", "sandboxed": True},
                )
            ]
        )
        assert "`tools.acme.weird_name_v2`" in line
        assert "weird-name.v2`" not in line

    def test_a_name_with_no_legal_form_loses_the_hint(self):
        line = direct_tool_summary(
            [_tool("mcp__acme__bad", stamp={"server": "acme", "tool": "---", "sandboxed": True})]
        )
        assert "importable" not in line


class TestDirectToolMiddleware:
    """What a turn installs for its directly bound tools, and in what order."""

    def test_the_order_gate_wraps_the_consent_gate(self):
        # The first wrapper is the outermost, so a consent refusal comes back
        # through the order gate and settles the attempt it consumed.
        stack = direct_tool_middleware(_ToolSet([GATED]), object())
        assert [type(m).__name__ for m in stack] == [
            "OrderGovernanceMiddleware",
            "DirectMcpPolicyMiddleware",
        ]

    def test_without_a_ledger_nothing_governs_an_order(self):
        stack = direct_tool_middleware(_ToolSet([GATED]), None)
        assert [type(m).__name__ for m in stack] == ["DirectMcpPolicyMiddleware"]

    def test_a_turn_with_no_direct_tools_installs_nothing(self):
        assert direct_tool_middleware(_ToolSet([]), object()) == []
        assert direct_tool_middleware(None, object()) == []


@pytest.mark.asyncio
async def test_the_gate_stamp_is_what_the_port_is_asked_about():
    """The port refuses a call bound ungated on a row that now gates it, so it
    has to be told what this turn stamped rather than re-deriving it."""
    toolset = _ToolSet([GATED])
    mw = DirectMcpPolicyMiddleware(toolset)
    out = await mw.awrap_tool_call(_Request(GATED.name), _ran)
    assert out.content == "ran"
    assert toolset.asked == [("moomoo", "trading_order_place", True)]


class TestRefusedMessage:
    """The one builder both gates refuse through."""

    def test_the_caller_owns_the_whole_sentence(self):
        # The order gate names the order in its own words, so a prefix added
        # here would arrive in the middle of somebody else's sentence.
        out = refused_message(
            {"id": "call-1", "name": "mcp__moomoo__place"},
            "an order (live place at moomoo) runs only on the async path",
            {"server": "moomoo", "tool": "trading_order_place"},
        )
        assert out.status == "error"
        assert out.tool_call_id == "call-1"
        assert out.content.startswith("an order")
        assert out.artifact[METADATA_KEY] == {
            "server": "moomoo",
            "tool": "trading_order_place",
        }

    def test_a_receipt_rides_beside_the_stamp(self):
        out = refused_message(
            {"id": "call-1", "name": "mcp__moomoo__place"},
            "refused",
            {"server": "moomoo", "tool": "trading_order_place"},
            receipt={"order_attempt": {"id": "900001"}},
        )
        assert out.artifact["order_attempt"] == {"id": "900001"}
        assert out.artifact[METADATA_KEY]["server"] == "moomoo"


# DALTON-HOOK(hide-trading): bindable_direct_tools strips order/cancel tools before binding to the agent
def test_bindable_direct_tools_strips_order_tools():
    from ptc_agent.agent.middleware.direct_mcp import bindable_direct_tools

    quote = _tool(
        "mcp__moomoo__quote_stock_quote",
        stamp={"server": "moomoo", "tool": "quote_stock_quote"},
    )
    untagged_order = _tool(
        "mcp__robinhood__place_equity_order",
        stamp={"server": "robinhood", "tool": "place_equity_order"},
    )
    bound = bindable_direct_tools(_ToolSet([DIRECT, GATED, untagged_order, quote]))
    assert [t.name for t in bound] == ["mcp__moomoo__quote_stock_quote"]
    assert bindable_direct_tools(None) == []


def test_default_order_tool_names_matches_brokerage_capabilities_order_tools():
    # DALTON-HOOK(hide-trading): ensure _DEFAULT_ORDER_TOOL_NAMES stays in sync with brokerage_capabilities._ORDER_TOOLS
    from ptc_agent.agent.middleware.direct_mcp import _DEFAULT_ORDER_TOOL_NAMES
    from src.server.services.brokerage_capabilities import _ORDER_TOOLS
    from src.server.services.tool_binding import ALL_ORDER_TOOL_NAMES

    expected = frozenset(t.strip().lower() for tools in _ORDER_TOOLS.values() for t in tools)
    assert _DEFAULT_ORDER_TOOL_NAMES == expected
    assert _DEFAULT_ORDER_TOOL_NAMES == ALL_ORDER_TOOL_NAMES


@pytest.mark.asyncio
async def test_default_capability_grants_and_direct_mcp_never_expose_live_order_tools(monkeypatch):
    # DALTON-HOOK(hide-trading): default grants exclude live order tools and neither
    # new users, existing users, nor colleague-dispatched turns can bind order tools
    import src.server.services.egress.direct_tools as dt_mod
    from src.ptc_agent.agent.middleware.direct_mcp import bindable_direct_tools
    from src.server.services.brokerage_capabilities import (
        _CURATION,
        _ORDER_TOOLS,
        denied_tools,
        effective_capabilities,
        group_keys_for,
        groups_for,
    )
    from src.server.services.mcp_tool_split import (
        DirectServerTools,
        DirectTool,
        split_server_tools,
    )
    from src.server.services.tool_binding import (
        BindingInputs,
        resolve_plan,
        resolve_tool,
    )

    for vendor, groups_map in _CURATION.items():
        live_trading_tools = frozenset(t.lower() for t in groups_map.get("trading", ()))
        vendor_order_tools = frozenset(t.lower() for t in _ORDER_TOOLS.get(vendor, ()))

        # 1. Unset/empty grant (new or pre-consent existing connection) denies all order tools
        assert effective_capabilities(vendor, ()) == ()
        unset_denied = denied_tools(vendor, ()) or frozenset()
        assert live_trading_tools <= unset_denied
        assert vendor_order_tools <= unset_denied

        # 2. Default non-danger grant excludes 'trading' and denies all live trading tools
        default_keys = [g.key for g in groups_for(vendor) if g.tone != "danger"]
        assert "trading" not in default_keys
        assert "trading" not in effective_capabilities(vendor, default_keys)
        default_denied = denied_tools(vendor, default_keys) or frozenset()
        assert live_trading_tools <= default_denied

        # 3. Even if all groups (including trading/staged_orders) are in a BindingPlan,
        # split_server_tools strips every order tool from both sandbox and direct paths
        read_tool = groups_map["account"][0]
        plan = resolve_plan(
            vendor,
            group_keys_for(vendor),
            BindingInputs(overrides={read_tool: "direct"}),
        )
        raw_tools = [{"name": name, "description": name} for name in (vendor_order_tools | {read_tool})]
        sandbox_tools, direct_entry = split_server_tools(
            raw_tools,
            denied=None,
            plan=plan,
            vendor=vendor,
        )
        assert sandbox_tools == []
        assert direct_entry is not None
        assert [t.name for t in direct_entry.tools] == [read_tool]

    # 4. Colleague-dispatched turn (owner_user_id != caller_user_id) and owner turn
    # both strip every order tool in prepare_direct_mcp_tools
    monkeypatch.setattr(dt_mod, "EGRESS_RELAY_SECRET", "test-secret-key-32-bytes-long-00")
    inputs = BindingInputs(overrides={"get_positions": "direct"})
    ibkr_entry = DirectServerTools(
        vendor="ibkr",
        tools=(
            DirectTool({"name": "submit_order", "description": "Submit IBKR live order", "input_schema": {}}, resolve_tool("ibkr", "submit_order", inputs)),
            DirectTool({"name": "modify_order", "description": "Modify IBKR live order", "input_schema": {}}, resolve_tool("ibkr", "modify_order", inputs)),
            DirectTool({"name": "cancel_order", "description": "Cancel IBKR live order", "input_schema": {}}, resolve_tool("ibkr", "cancel_order", inputs)),
            DirectTool({"name": "answer_order_question", "description": "Confirm IBKR order warning", "input_schema": {}}, resolve_tool("ibkr", "answer_order_question", inputs)),
            DirectTool({"name": "get_positions", "description": "Read positions", "input_schema": {}}, resolve_tool("ibkr", "get_positions", inputs)),
        ),
    )
    for owner_id, caller_id in (("user-new", "user-new"), ("owner-b", "colleague-a")):
        binding = await dt_mod.prepare_direct_mcp_tools(
            user_id=owner_id,
            owner_user_id=owner_id,
            caller_user_id=caller_id,
            workspace_id="ws-1",
            grants={"ibkr": "grant-token-1"},
            by_server={"ibkr": ibkr_entry},
        )
        bound_names = [t.name for t in bindable_direct_tools(binding)]
        assert bound_names == ["mcp__ibkr__get_positions"]
