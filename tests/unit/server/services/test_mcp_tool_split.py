"""How the composite install splits one server's tools between the sandbox
wrappers and the direct path."""

from __future__ import annotations

from src.server.services.mcp_tool_split import (
    build_direct_entries,
    split_server_tools,
)
from src.server.services.tool_binding import BindingPlan, OrderPolicy, Resolved


def _tool(name: str, **props) -> dict:
    return {"name": name, "input_schema": {"type": "object", "properties": props}}


def _plan(**bindings: str) -> BindingPlan:
    """A plan shaped the way ``resolve_plan`` builds one: every set is a
    projection of ``by_tool``, so the split cannot read two of them apart."""
    # DALTON-HOOK(hide-trading): order tools are stripped before splitting, so test split behavior with non-order tools
    by_tool = {
        name: Resolved(
            binding,
            "policy",
            order=(
                OrderPolicy("place", "live", True)
                if name in ("trading_order_place", "quote_stock_quote")
                else None
            ),
        )
        for name, binding in bindings.items()
    }
    return BindingPlan(
        direct=frozenset(
            t for t, r in by_tool.items() if r.binding in ("direct", "both")
        ),
        sandbox_excluded=frozenset(
            t for t, r in by_tool.items() if r.binding == "direct"
        ),
        by_tool=by_tool,
    )


# DALTON-HOOK(hide-trading): use non-order tools to test general split mechanics
TOOLS = [
    _tool("account_list"),
    _tool("Quote_Stock_Quote"),
    _tool("quote_cur_kline"),
]


def test_no_plan_means_everything_goes_to_the_sandbox():
    sandbox, direct = split_server_tools(TOOLS, denied=None, plan=None)
    assert [t["name"] for t in sandbox] == [t["name"] for t in TOOLS]
    assert direct is None


def test_direct_only_tools_leave_the_sandbox_and_both_keeps_them():
    plan = _plan(quote_stock_quote="direct", quote_cur_kline="both")
    sandbox, direct = split_server_tools(TOOLS, denied=None, plan=plan)
    assert [t["name"] for t in sandbox] == ["account_list", "quote_cur_kline"]
    # Folded matching: the vendor's recased name still lands in the direct set.
    assert [t.name for t in direct.tools] == [
        "Quote_Stock_Quote",
        "quote_cur_kline",
    ]
    # A ``both`` tool keeps the wrapper the prompt may still point the model at.
    assert [t.sandboxed for t in direct.tools] == [False, True]
    # Folded once, here, so no consumer has to remember to fold it again: the
    # recased schema carries the resolution stored under the plain spelling.
    assert [t.resolved.approval for t in direct.tools] == [True, False]


def test_a_denied_tool_reaches_neither_path():
    # DALTON-HOOK(hide-trading): use non-order tool in denied-tool split test
    plan = _plan(quote_stock_quote="direct")
    sandbox, direct = split_server_tools(
        TOOLS, denied=frozenset({"quote_stock_quote"}), plan=plan
    )
    assert [t["name"] for t in sandbox] == ["account_list", "quote_cur_kline"]
    assert direct is None


class _Server:
    def __init__(
        self, name: str, url: str | None = "https://vendor.example.com/mcp"
    ) -> None:
        self.name = name
        self.url = url


class _Snapshots:
    def __init__(self, by_name: dict[str, list[dict] | None]) -> None:
        self.by_name = by_name

    def ok(self, server) -> dict | None:
        tools = self.by_name.get(server.name)
        return None if tools is None else {"tools": tools}


def test_build_direct_entries_keys_both_halves_by_server_name():
    # DALTON-HOOK(hide-trading): use non-order tool in build_direct_entries test
    servers = [_Server("moomoo"), _Server("nosnapshot")]
    plan = _plan(quote_stock_quote="direct")
    sandbox, direct = build_direct_entries(
        servers,
        _Snapshots({"moomoo": TOOLS, "nosnapshot": None}),
        denied={},
        plans={"moomoo": plan},
    )
    # A server with no usable snapshot is in neither half, which is how the
    # composite install reads settlement.
    assert set(sandbox) == {"moomoo"}
    assert [t["name"] for t in sandbox["moomoo"]] == [
        "account_list",
        "quote_cur_kline",
    ]
    assert [t.name for t in direct["moomoo"].tools] == ["Quote_Stock_Quote"]


def test_build_direct_entries_keeps_a_planless_server_out_of_the_direct_half():
    sandbox, direct = build_direct_entries(
        [_Server("moomoo")], _Snapshots({"moomoo": TOOLS}), denied={}, plans={}
    )
    assert [t["name"] for t in sandbox["moomoo"]] == [t["name"] for t in TOOLS]
    assert direct == {}


def test_stdio_brokerage_order_tools_are_excluded_from_sandbox_wrappers():
    """DALTON-HOOK(hide-trading): a stdio MCP server carrying place_equity_order must not expose it in sandbox wrappers."""
    from unittest.mock import create_autospec

    from ptc_agent.config.agent import MCPServerConfig
    from src.server.services.mcp_discovery import ToolSnapshotIndex

    stdio_tools = [
        _tool("get_positions"),
        _tool("place_equity_order"),
        _tool("Trading_Order_Place"),
        _tool("sim_trade_input_order"),
    ]
    server = create_autospec(MCPServerConfig, instance=True)
    server.name = "stdio_broker"
    server.url = None
    snapshots = create_autospec(ToolSnapshotIndex, instance=True)
    snapshots.ok.return_value = {"tools": stdio_tools}
    sandbox, direct = build_direct_entries(
        [server],
        snapshots,
        denied={},
        plans={},
    )
    assert [t["name"] for t in sandbox["stdio_broker"]] == ["get_positions"]
    assert direct == {}
