"""watch_market tool behavior (single tool, action="watch"/"unwatch"/"cancel"),
and the price targets it sets, which stand apart from the watch list."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from src.config.settings import get_market_watch_max_targets
from src.tools.market_watch.tool import _already_met, _plan_target, _spellings, watch_market

_MOD = "src.tools.market_watch.tool"
_CFG = {"configurable": {"thread_id": "t-1"}}
_TCFG = {"configurable": {"thread_id": "t-1", "user_id": "u-1", "timezone": "America/New_York"}}


@pytest.fixture(autouse=True)
def storage():
    """Targets storage and the change nudge, never the real database."""

    async def _arm(**kw):
        return {"target_id": "00000000-0000-0000-0000-000000000001", **kw}

    with (
        patch(f"{_MOD}.arm_target", AsyncMock(side_effect=_arm)) as arm,
        patch(f"{_MOD}.cancel_targets", AsyncMock(return_value=[])) as cancel,
        patch(f"{_MOD}.targets_changed", AsyncMock()) as changed,
        patch(f"{_MOD}.list_thread_targets", AsyncMock(return_value=[])) as listed,
    ):
        yield {"arm": arm, "cancel": cancel, "changed": changed, "listed": listed}


def _provider(*snaps):
    provider = MagicMock()
    provider.get_snapshots = AsyncMock(return_value=list(snaps))
    return patch(f"{_MOD}.get_market_data_provider", AsyncMock(return_value=provider))


def _echo_watchlist():
    return patch(
        f"{_MOD}.add_symbols",
        AsyncMock(side_effect=lambda thread_id, symbols: list(dict.fromkeys(symbols))),
    )


# RunnableConfig is an injected tool arg: it is passed as the second positional
# arg to ainvoke (config=...), NOT as a key inside the tool-input dict.
@pytest.mark.asyncio
async def test_watch_registers_symbols():
    with patch(f"{_MOD}.add_symbols", AsyncMock(return_value=["NVDA", "TSLA"])) as mock_add:
        result = await watch_market.ainvoke({"symbols": ["NVDA", "TSLA"]}, config=_CFG)
    mock_add.assert_awaited_once_with("t-1", ["NVDA", "TSLA"])
    assert "NVDA, TSLA" in result
    assert "<market-watch>" in result


@pytest.mark.asyncio
async def test_watch_result_instructs_feed_usage():
    """The watch result carries the action-specific unwatch instruction.

    Feed mechanics live in the market-watch skill; the result only needs the
    reciprocal action (how to stop) plus what the feed looks like.
    """
    with patch(f"{_MOD}.add_symbols", AsyncMock(return_value=["NVDA"])):
        result = await watch_market.ainvoke({"symbols": ["NVDA"]}, config=_CFG)
    assert "<market-watch>" in result
    assert 'action="unwatch"' in result


@pytest.mark.asyncio
async def test_watch_without_thread_id_errors_gracefully():
    result = await watch_market.ainvoke({"symbols": ["NVDA"]}, config={"configurable": {}})
    assert "unavailable" in result.lower()


# A Monday in October, as the expected lapse texts read, still ahead.
_LAPSE = datetime(2037, 10, 12, 20, 0, tzinfo=timezone.utc)


def _armed(symbol, kind, value, *, market="stock", pct_from=None, status="armed", expires_at=_LAPSE):
    return {
        "symbol": symbol, "market": market, "kind": kind, "value": value, "pct_from": pct_from,
        "status": status, "expires_at": expires_at,
    }


@pytest.mark.asyncio
async def test_no_symbols_and_no_targets_shows_what_is_watched_and_armed(storage):
    """The one view of armed targets once a compaction drops the result that set them."""
    storage["listed"].return_value = [
        _armed("NVDA", "pct_up", 4.0, pct_from="previous_close"),
        _armed("MSFT", "below", 350.0, status="fired"),
    ]
    with patch(f"{_MOD}.get_watchlist", AsyncMock(return_value=["AAPL"])):
        result = await watch_market.ainvoke({}, config=_TCFG)
    assert result == (
        "Watching: AAPL.\n\nArmed targets:\n"
        "- NVDA up 4% from the previous close: lapses Mon Oct 12 16:00 EDT."
    )


@pytest.mark.asyncio
async def test_status_says_when_nothing_is_watched_or_armed(storage):
    with patch(f"{_MOD}.get_watchlist", AsyncMock(return_value=[])):
        result = await watch_market.ainvoke({}, config=_TCFG)
    assert result == "Not watching any symbols.\n\nNo armed targets."


@pytest.mark.asyncio
async def test_a_target_past_its_expiry_is_not_listed_as_armed(storage):
    """The sweep has not closed it yet, but the monitor no longer checks it."""
    lapsed = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
    storage["listed"].return_value = [_armed("NVDA", "above", 240.0, expires_at=lapsed)]
    with patch(f"{_MOD}.get_watchlist", AsyncMock(return_value=[])):
        result = await watch_market.ainvoke({}, config=_TCFG)
    assert result == "Not watching any symbols.\n\nNo armed targets."


@pytest.mark.asyncio
async def test_unwatch_specific():
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=["TSLA"])):
        result = await watch_market.ainvoke(
            {"symbols": ["NVDA"], "action": "unwatch"}, config=_CFG
        )
    assert "TSLA" in result


@pytest.mark.asyncio
async def test_unwatch_all():
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=[])) as mock_rm:
        result = await watch_market.ainvoke({"action": "unwatch"}, config=_CFG)
    mock_rm.assert_awaited_once_with("t-1", None)
    assert "stopped" in result.lower()


@pytest.mark.asyncio
async def test_watch_cache_unavailable():
    with patch(f"{_MOD}.add_symbols", AsyncMock(return_value=None)):
        result = await watch_market.ainvoke({"symbols": ["NVDA"]}, config=_CFG)
    assert "unavailable" in result.lower()


@pytest.mark.asyncio
async def test_watch_all_symbols_invalid():
    with patch(f"{_MOD}.add_symbols", AsyncMock(return_value=[])):
        result = await watch_market.ainvoke({"symbols": ["!!!"]}, config=_CFG)
    assert "no valid" in result.lower()


@pytest.mark.asyncio
async def test_unwatch_cache_unavailable_is_not_reported_as_stopped():
    """A failed mutation must never read as success — the Redis key survives
    and injection would resume once the cache recovers."""
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=None)):
        result = await watch_market.ainvoke({"action": "unwatch"}, config=_CFG)
    assert "unavailable" in result.lower()
    assert "stopped" not in result.lower()


@pytest.mark.asyncio
async def test_watch_reports_rejected_symbols():
    """A dropped symbol (bad grammar or cap overflow) must not read as success:
    with a non-empty watchlist, "Now watching: AAPL." alone is indistinguishable
    from the requested symbol having been added."""
    with patch(f"{_MOD}.add_symbols", AsyncMock(return_value=["AAPL"])):
        result = await watch_market.ainvoke({"symbols": ["SPX 500"]}, config=_CFG)
    assert "Now watching: AAPL" in result
    assert "Not added" in result
    assert "SPX 500" in result


@pytest.mark.asyncio
async def test_watch_no_rejection_note_when_all_added():
    # Lowercase input normalizes to the accepted symbol — not a rejection.
    with patch(f"{_MOD}.add_symbols", AsyncMock(return_value=["AAPL", "NVDA"])):
        result = await watch_market.ainvoke({"symbols": ["nvda"]}, config=_CFG)
    assert "Not added" not in result


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


async def _set(*targets, snaps=(), config=_TCFG):
    with _provider(*snaps), _echo_watchlist() as add:
        result = await watch_market.ainvoke({"targets": list(targets)}, config=config)
    return result, add


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target, reason",
    [
        ({"symbol": "NVDA", "above": 205, "below": 190}, "exactly one"),
        ({"symbol": "NVDA"}, "exactly one"),
        ({"symbol": "NVDA", "above": -5}, "greater than 0"),
        ({"symbol": "NVDA", "above": 205, "pct_from": "now"}, "only to pct_up and pct_down"),
        ({"symbol": "NVDA", "pct_down": 100}, "never hit"),
        ({"symbol": "BTC-USD", "above": 1}, "crypto, currency or futures"),
        ({"symbol": "ZZZZ.ZZ", "above": 1}, "never be checked"),
        ({"symbol": "!!!", "above": 1}, "not a ticker symbol"),
    ],
)
async def test_invalid_target_is_refused_with_its_reason(storage, target, reason):
    result, add = await _set(target)
    assert "Targets not set" in result
    assert reason in result
    storage["arm"].assert_not_awaited()
    storage["changed"].assert_not_awaited()
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_level_target_arms_with_price_distance_and_lapse(storage):
    result, add = await _set(
        {"symbol": "NVDA", "above": 205},
        snaps=[{"symbol": "NVDA", "price": 198.4, "previous_close": 195.0}],
    )
    kw = storage["arm"].await_args.kwargs
    assert kw["thread_id"] == "t-1"
    assert kw["user_id"] == "u-1"
    assert (kw["symbol"], kw["market"], kw["kind"], kw["value"]) == ("NVDA", "stock", "above", 205.0)
    assert kw["pct_from"] is None
    assert kw["armed_price"] == 198.4
    assert kw["max_armed"] == get_market_watch_max_targets()
    assert kw["expires_at"] > datetime.now(timezone.utc)
    assert kw["warn_at"] <= kw["expires_at"]
    # A target alone watches nothing: the server checks its price.
    add.assert_not_awaited()
    storage["changed"].assert_awaited_once_with("t-1")
    assert result.startswith("Targets set:\n- NVDA above 205: last 198.40, trigger 205.00, +3.33% away; lapses ")
    assert "Targets not set" not in result
    assert "<market-watch>" not in result


@pytest.mark.asyncio
async def test_already_met_is_refused(storage):
    result, add = await _set(
        {"symbol": "NVDA", "above": 205}, snaps=[{"symbol": "NVDA", "price": 206.1}]
    )
    assert "NVDA is already above 205 (last 206.10)" in result
    storage["arm"].assert_not_awaited()
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_level_target_arms_without_a_price(storage):
    result, _ = await _set({"symbol": "NVDA", "below": 190})
    assert storage["arm"].await_args.kwargs["armed_price"] is None
    assert "no live price right now" in result


@pytest.mark.asyncio
async def test_pct_from_now_needs_a_price(storage):
    result, _ = await _set({"symbol": "NVDA", "pct_up": 3, "pct_from": "now"})
    assert 'no live price to measure "now" from' in result
    storage["arm"].assert_not_awaited()


@pytest.mark.asyncio
async def test_pct_from_now_is_stored_as_armed_from_the_current_price(storage):
    result, _ = await _set(
        {"symbol": "NVDA", "pct_down": 2, "pct_from": "now"},
        snaps=[{"symbol": "NVDA", "price": 200.0, "previous_close": 190.0}],
    )
    kw = storage["arm"].await_args.kwargs
    assert (kw["kind"], kw["value"], kw["pct_from"], kw["armed_price"]) == ("pct_down", 2.0, "armed", 200.0)
    assert "NVDA down 2% from now: last 200.00, trigger 196.00, -2.00% away" in result


@pytest.mark.asyncio
async def test_pct_defaults_to_previous_close(storage):
    await _set({"symbol": "NVDA", "pct_up": 3})
    assert storage["arm"].await_args.kwargs["pct_from"] == "previous_close"


@pytest.mark.asyncio
async def test_cap_is_refused(storage):
    storage["arm"].side_effect = None
    storage["arm"].return_value = None
    result, add = await _set({"symbol": "NVDA", "above": 205})
    assert (
        f"already has {get_market_watch_max_targets()} armed targets; "
        'cancel one first with action="cancel".'
    ) in result
    storage["changed"].assert_not_awaited()
    add.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_failure_never_reads_as_armed(storage):
    storage["arm"].side_effect = RuntimeError("db down")
    result, _ = await _set({"symbol": "NVDA", "above": 205})
    assert "could not be saved, so it is not armed" in result
    assert "Targets set" not in result


@pytest.mark.asyncio
async def test_index_target_is_keyed_as_the_monitor_reads_it(storage):
    result, add = await _set(
        {"symbol": "^GSPC", "below": 5900},
        {"symbol": "SPX", "pct_up": 1},
        snaps=[{"symbol": "GSPC", "price": 6000.0, "previous_close": 5990.0}],
    )
    stored = [c.kwargs for c in storage["arm"].await_args_list]
    assert [(k["symbol"], k["market"], k["kind"]) for k in stored] == [
        ("SPX", "index", "below"),
        ("SPX", "index", "pct_up"),
    ]
    assert all(k["armed_price"] == 6000.0 for k in stored)
    add.assert_not_awaited()
    assert "- ^GSPC below 5900: last 6,000.00" in result


@pytest.mark.asyncio
async def test_index_trigger_measures_from_the_monitors_previous_close(storage):
    """Before the open, an index read as a stock comes from another vendor
    whose previous close is still a session behind; the trigger shown must be
    the one the monitor fires at."""
    rows = {
        "stocks": [{"symbol": "^GSPC", "price": 6100.0, "previous_close": 6050.0}],
        "indices": [{"symbol": "GSPC", "price": 6100.0, "previous_close": 6100.0}],
    }
    provider = MagicMock()
    provider.get_snapshots = AsyncMock(side_effect=lambda symbols, asset_type, user_id: rows[asset_type])
    with patch(f"{_MOD}.get_market_data_provider", AsyncMock(return_value=provider)), _echo_watchlist():
        result = await watch_market.ainvoke(
            {"targets": [{"symbol": "^GSPC", "pct_down": 1}]}, config=_TCFG
        )
    assert {c.kwargs["asset_type"] for c in provider.get_snapshots.await_args_list} == {"indices"}
    assert "trigger 6,039.00" in result


@pytest.mark.asyncio
async def test_same_kind_twice_in_one_call_keeps_the_last(storage):
    result, _ = await _set({"symbol": "NVDA", "above": 205}, {"symbol": "NVDA", "above": 210})
    assert [c.kwargs["value"] for c in storage["arm"].await_args_list] == [210.0]
    assert "NVDA above 205: replaced by a later above target" in result


@pytest.mark.asyncio
async def test_one_kind_on_both_listings_of_a_bare_symbol_arms_both(storage):
    """HSI the US ticker and ^HSI the Hang Seng are both stored as HSI; each
    is its own target, priced from its own row."""
    rows = {
        "stocks": [{"symbol": "HSI", "price": 12.0, "previous_close": 12.0}],
        "indices": [{"symbol": "HSI", "price": 20000.0, "previous_close": 20000.0}],
    }
    provider = MagicMock()
    provider.get_snapshots = AsyncMock(side_effect=lambda symbols, asset_type, user_id: rows[asset_type])
    with patch(f"{_MOD}.get_market_data_provider", AsyncMock(return_value=provider)), _echo_watchlist():
        result = await watch_market.ainvoke(
            {"targets": [{"symbol": "HSI", "above": 13}, {"symbol": "^HSI", "above": 21000}]},
            config=_TCFG,
        )
    stored = [c.kwargs for c in storage["arm"].await_args_list]
    assert [(k["symbol"], k["market"], k["armed_price"]) for k in stored] == [
        ("HSI", "stock", 12.0),
        ("HSI", "index", 20000.0),
    ]
    assert "replaced by a later" not in result


@pytest.mark.asyncio
async def test_targets_need_a_user(storage):
    result, _ = await _set({"symbol": "NVDA", "above": 205}, config=_CFG)
    assert "unavailable without a user" in result
    storage["arm"].assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_outage_does_not_read_as_a_target_failure(storage):
    with _provider(), patch(f"{_MOD}.add_symbols", AsyncMock(return_value=None)):
        result = await watch_market.ainvoke(
            {"symbols": ["NVDA"], "targets": [{"symbol": "NVDA", "above": 205}]}, config=_TCFG
        )
    assert "the targets below are still armed" in result
    assert "Targets set" in result


@pytest.mark.asyncio
async def test_unwatch_leaves_targets_armed_and_names_them(storage):
    storage["listed"].return_value = [_armed("SPX", "below", 5900.0, market="index")]
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=["NVDA"])) as rm:
        result = await watch_market.ainvoke(
            {"symbols": ["^GSPC"], "action": "unwatch"}, config=_TCFG
        )
    assert rm.await_args.args == ("t-1", ["^GSPC", "SPX"])
    storage["cancel"].assert_not_awaited()
    assert result == (
        'Stopped. Still watching: NVDA. Targets on ^GSPC stay armed; cancel them with action="cancel".'
    )


@pytest.mark.asyncio
async def test_unwatch_names_no_target_on_another_symbol(storage):
    storage["listed"].return_value = [_armed("NVDA", "above", 240.0)]
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=["NVDA"])):
        result = await watch_market.ainvoke({"symbols": ["AAPL"], "action": "unwatch"}, config=_TCFG)
    assert result == "Stopped. Still watching: NVDA."


@pytest.mark.asyncio
async def test_cancel_reaches_targets_under_every_spelling(storage):
    storage["cancel"].return_value = [
        {"symbol": "SPX", "market": "index", "kind": "below", "value": 5900.0, "pct_from": None},
        {"symbol": "SPX", "market": "index", "kind": "pct_up", "value": 1.0, "pct_from": "armed",
         "armed_price": 5800.0},
    ]
    with patch(f"{_MOD}.remove_symbols", AsyncMock()) as rm:
        result = await watch_market.ainvoke(
            {"symbols": ["^GSPC"], "action": "cancel"}, config=_TCFG
        )
    assert storage["cancel"].await_args.kwargs["listings"] == [("SPX", "index")]
    rm.assert_not_awaited()
    storage["changed"].assert_awaited_once_with("t-1")
    # Read back after the call that set it, "from now" would point at today's price.
    assert result == (
        "Cancelled targets: ^GSPC below 5900; ^GSPC up 1% from 5,800.00, its price when set."
    )


@pytest.mark.asyncio
async def test_a_cancel_names_one_listing_of_a_bare_symbol(storage):
    """Cancelling HSI, the US ticker, leaves a target on ^HSI, the Hang Seng."""
    with patch(f"{_MOD}.remove_symbols", AsyncMock()):
        await watch_market.ainvoke({"symbols": ["HSI"], "action": "cancel"}, config=_TCFG)
        assert storage["cancel"].await_args.kwargs["listings"] == [("HSI", "stock")]
        await watch_market.ainvoke({"symbols": ["^HSI"], "action": "cancel"}, config=_TCFG)
        assert storage["cancel"].await_args.kwargs["listings"] == [("HSI", "index")]


@pytest.mark.asyncio
async def test_unwatch_names_no_target_on_the_other_listing(storage):
    storage["listed"].return_value = [_armed("HSI", "below", 19000.0, market="index")]
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=["NVDA"])):
        result = await watch_market.ainvoke({"symbols": ["HSI"], "action": "unwatch"}, config=_TCFG)
    assert result == "Stopped. Still watching: NVDA."


@pytest.mark.asyncio
async def test_cancel_all_with_nothing_armed(storage):
    result = await watch_market.ainvoke({"action": "cancel"}, config=_TCFG)
    storage["cancel"].assert_awaited_once_with("t-1", listings=None)
    storage["changed"].assert_not_awaited()
    assert result == "No armed targets to cancel."


@pytest.mark.asyncio
async def test_cancel_failure_is_reported(storage):
    storage["cancel"].side_effect = RuntimeError("db down")
    result = await watch_market.ainvoke({"action": "cancel"}, config=_TCFG)
    assert result == "Targets could not be cancelled and are still armed."


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["unwatch", "cancel"])
async def test_only_watch_sets_targets(storage, action):
    with patch(f"{_MOD}.remove_symbols", AsyncMock(return_value=[])):
        result = await watch_market.ainvoke(
            {"action": action, "targets": [{"symbol": "NVDA", "above": 205}]}, config=_TCFG
        )
    storage["arm"].assert_not_awaited()
    assert result.endswith('Targets were not set: only action="watch" sets them.')


@pytest.mark.parametrize(
    "symbol, names",
    [
        ("^GSPC", ["^GSPC", "SPX"]),
        ("SPX", ["SPX", "^GSPC"]),
        # The Hang Seng is stored as HSI, which on the watch list is the US
        # ticker, so unwatching the index leaves the stock watched.
        ("^HSI", ["^HSI"]),
        ("HSI", ["HSI"]),
    ],
)
def test_unwatch_reaches_only_the_names_of_one_listing(symbol, names):
    assert _spellings([symbol]) == names


_NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    "now, refused",
    [
        (datetime(2026, 10, 6, 11, 0, tzinfo=_NY), True),  # in session
        # Saturday: the base rolls to Friday's close before the next print,
        # so Friday's move says nothing about the target.
        (datetime(2026, 10, 10, 11, 0, tzinfo=_NY), False),
    ],
)
def test_pct_from_previous_close_is_met_only_while_its_base_is_live(now, refused):
    plan, _, _ = _plan_target({"symbol": "NVDA", "pct_up": 3})
    reason = _already_met(plan, 104.0, 100.0, now)
    assert (reason is not None) is refused
    if refused:
        assert "already up 4.00% from the previous close" in reason
