"""Agent tool to register/unregister tickers for live market watch and set price targets."""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Any, Dict, List, Literal, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from market_protocol import MarketPhase
from pydantic import BaseModel, Field

from src.config.settings import get_market_watch_max_targets
from src.data_client.instrument_clock import clock_for_ref
from src.data_client.registry import get_market_data_provider
from src.server.database.market_targets import arm_target, cancel_targets, list_thread_targets
from src.server.models.automation import MarketType
from src.server.services.market_targets.changes import targets_changed
from src.server.services.market_targets.listing import TargetListing, target_listing, watch_spelling
from src.server.services.market_targets.window import target_window
from src.server.services.price_monitor import Listing, listing_snapshots, watched_listing
from src.tools.market_data.quote_format import current_price
from src.utils.market_watch import add_symbols, clean_symbols, get_watchlist, remove_symbols
from src.utils.timezone_utils import zone_or_none

logger = logging.getLogger(__name__)

# Returned with every successful watch: the reciprocal action (how to stop).
# Feed mechanics — newest block = current price, throttling, venue hours — live
# in the market-watch skill, not here, so the result stays action-specific.
_FEED_INSTRUCTIONS = (
    "Live prices now arrive automatically as <market-watch> blocks. When live "
    'tracking no longer matters, call watch_market with action="unwatch" (omit '
    "symbols to stop watching everything)."
)

_KINDS = ("above", "below", "pct_up", "pct_down")
_PCT_KINDS = ("pct_up", "pct_down")
# The tool says "now"; storage says "armed", the moment the target was set.
_PCT_FROM_STORED = {"previous_close": "previous_close", "now": "armed"}


class PriceTarget(BaseModel):
    """Set exactly one of above, below, pct_up, pct_down."""

    symbol: str = Field(description="Ticker, spelled as you would watch it.")
    above: Optional[float] = Field(
        default=None, description="Alert when the price rises to this level."
    )
    below: Optional[float] = Field(
        default=None, description="Alert when the price falls to this level."
    )
    pct_up: Optional[float] = Field(
        default=None, description="Alert on a rise of this many percent (3 means 3%)."
    )
    pct_down: Optional[float] = Field(
        default=None, description="Alert on a fall of this many percent (3 means 3%)."
    )
    pct_from: Optional[Literal["previous_close", "now"]] = Field(
        default=None,
        description=(
            'What pct_up/pct_down measure from: "previous_close" (default) or '
            '"now", the price when you set it.'
        ),
    )


@dataclass
class _Plan:
    """A target that passed validation, waiting for a price and a slot."""

    symbol: str  # as the model spelled it
    listing: TargetListing
    kind: str
    value: float
    pct_from: Optional[str]  # tool vocabulary
    label: str


def _thread_id(config: RunnableConfig) -> Optional[str]:
    return (config.get("configurable") or {}).get("thread_id")


def _rejected(requested: Optional[List[str]], watched: List[str]) -> List[str]:
    """Requested symbols that didn't land in the watch list (bad grammar or
    over the max-symbols cap) — echoed back so a drop can't read as success.
    Tokens are bounded to 20 chars; valid symbols (≤15) never truncate."""
    accepted = set(watched)
    seen: set = set()
    out: List[str] = []
    for raw in requested or []:
        if not isinstance(raw, (str, int)):
            continue
        sym = str(raw).strip().upper()[:20]
        if sym and sym not in accepted and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def _num(value: float) -> str:
    return f"{value:,.2f}" if abs(value) >= 1 else f"{value:.4g}"


def _describe(symbol: str, kind: str, value: float, pct_from: Optional[str]) -> str:
    if kind == "above":
        return f"{symbol} above {value:g}"
    if kind == "below":
        return f"{symbol} below {value:g}"
    direction = "up" if kind == "pct_up" else "down"
    base = "the previous close" if pct_from == "previous_close" else "now"
    return f"{symbol} {direction} {value:g}% from {base}"


def _item_fields(item: Any) -> Dict[str, Any]:
    if isinstance(item, PriceTarget):
        return item.model_dump()
    if isinstance(item, dict):
        return item
    return {}


def _plan_target(item: Any) -> Tuple[Optional[_Plan], str, Optional[str]]:
    """``(plan, label, refusal)``: one validated target, or why it was refused."""
    fields = _item_fields(item)
    raw_symbol = fields.get("symbol")
    cleaned = clean_symbols([raw_symbol])
    label = str(raw_symbol or "?").strip()[:20] or "?"
    if not cleaned:
        return None, label, "not a ticker symbol"
    symbol = cleaned[0]
    set_kinds = [k for k in _KINDS if fields.get(k) is not None]
    if len(set_kinds) != 1:
        return None, symbol, "set exactly one of above, below, pct_up, pct_down"
    kind = set_kinds[0]
    try:
        value = float(fields[kind])
    except (TypeError, ValueError):
        return None, symbol, f"{kind} must be a number"
    pct_from = fields.get("pct_from")
    if kind in _PCT_KINDS:
        pct_from = pct_from or "previous_close"
        if pct_from not in _PCT_FROM_STORED:
            return None, symbol, 'pct_from must be "previous_close" or "now"'
    elif pct_from is not None:
        return None, symbol, "pct_from applies only to pct_up and pct_down"
    label = _describe(symbol, kind, value, pct_from)
    if not math.isfinite(value) or value <= 0:
        return None, label, f"{kind} must be greater than 0"
    if kind == "pct_down" and value >= 100:
        return None, label, "a fall of 100% or more can never hit"
    listing, refusal = target_listing(symbol)
    if listing is None:
        return None, label, refusal
    return _Plan(symbol, listing, kind, value, pct_from, label), label, None


def _positive(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


async def _snapshots(listings: List[TargetListing], user_id: Any) -> Dict[Listing, Dict[str, Any]]:
    """Rows by the monitor's listing, fetched as it fetches its reference prices,
    so the trigger shown is the one it fires at. {} on any failure, since a
    level target can still arm without a price."""
    if not listings:
        return {}
    try:
        provider = await get_market_data_provider()
        return await listing_snapshots(
            provider, [(listing.key, listing.market) for listing in listings], user_id=user_id
        )
    except Exception:
        logger.warning("[MarketWatch] target snapshot fetch failed", exc_info=True)
        return {}


def _trigger_level(plan: _Plan, last: Optional[float], prev_close: Optional[float]) -> Optional[float]:
    if plan.kind in ("above", "below"):
        return plan.value
    base = last if plan.pct_from == "now" else prev_close
    if base is None:
        return None
    sign = 1 if plan.kind == "pct_up" else -1
    return base * (1 + sign * plan.value / 100)


def _base_is_live(plan: _Plan, now: datetime) -> bool:
    """Whether the snapshot's previous close is the base the monitor is
    measuring from right now. Between sessions it rolls before the next
    print, so a move already made says nothing about the next session."""
    ref = watched_listing(plan.listing.key, plan.listing.market)
    if ref is None:
        return False
    try:
        return clock_for_ref(ref).phase(now) is not MarketPhase.CLOSED
    except Exception:
        return False


def _already_met(
    plan: _Plan, last: Optional[float], prev_close: Optional[float], now: datetime
) -> Optional[str]:
    """Why a target would fire on the first price the monitor sees, or None."""
    if last is None:
        return None
    if plan.kind in ("above", "below"):
        crossed = last >= plan.value if plan.kind == "above" else last <= plan.value
        if not crossed:
            return None
        where = "at" if last == plan.value else plan.kind
        return f"{plan.symbol} is already {where} {plan.value:g} (last {_num(last)})"
    if plan.pct_from != "previous_close" or not prev_close or not _base_is_live(plan, now):
        return None
    move = (last - prev_close) / prev_close * 100
    if plan.kind == "pct_up" and move >= plan.value:
        return f"{plan.symbol} is already up {move:.2f}% from the previous close (last {_num(last)})"
    if plan.kind == "pct_down" and -move >= plan.value:
        return f"{plan.symbol} is already down {-move:.2f}% from the previous close (last {_num(last)})"
    return None


def _when(at: datetime, zone: tzinfo) -> str:
    local = at.astimezone(zone)
    return f"{local:%a %b} {local.day} {local:%H:%M %Z}"


def _lapse_text(key: str, market: MarketType, expires_at: datetime, user_tz: Optional[str]) -> str:
    """When a target lapses, on its venue's clock and the user's.

    The venue clock says which close it is; the user's clock is what a
    reply to the user needs, and converting it by hand is where a model
    slips.
    """
    ref = watched_listing(key, market)
    venue_time = _when(expires_at, zone_or_none(ref.tz if ref else None) or timezone.utc)
    user = zone_or_none(user_tz)
    if user is not None and _when(expires_at, user) != venue_time:
        return f"{venue_time} ({_when(expires_at, user)} your time)"
    return venue_time


def _armed_line(
    plan: _Plan,
    last: Optional[float],
    prev_close: Optional[float],
    expires_at: datetime,
    user_tz: Optional[str],
) -> str:
    level = _trigger_level(plan, last, prev_close)
    if last is None:
        where = "no live price right now"
    elif level is None:
        where = f"last {_num(last)}"
    else:
        where = f"last {_num(last)}, trigger {_num(level)}, {(level - last) / last * 100:+.2f}% away"
    lapse = _lapse_text(plan.listing.key, plan.listing.market, expires_at, user_tz)
    return f"- {plan.label}: {where}; lapses {lapse}."


async def _set_targets(
    thread_id: str, configurable: Dict[str, Any], items: List[Any]
) -> Tuple[List[str], List[str]]:
    """Arm *items*; ``(armed lines, refused lines)``."""
    refused: List[str] = []
    plans: List[_Plan] = []
    for item in items:
        plan, label, refusal = _plan_target(item)
        if plan is None:
            refused.append(f"- {label}: {refusal}.")
        else:
            plans.append(plan)

    # One armed target per (listing, kind): a later one in the same call would
    # replace the earlier in storage, so the earlier must not report as set.
    last_index = {(p.listing.key, p.listing.market, p.kind): i for i, p in enumerate(plans)}
    kept: List[_Plan] = []
    for i, plan in enumerate(plans):
        if last_index[(plan.listing.key, plan.listing.market, plan.kind)] != i:
            refused.append(
                f"- {plan.label}: replaced by a later {plan.kind} target on {plan.symbol} in this call."
            )
        else:
            kept.append(plan)

    user_id = configurable.get("user_id")
    if kept and not user_id:
        refused.extend(f"- {p.label}: targets are unavailable without a user." for p in kept)
        kept = []
    if not kept:
        return [], refused

    snaps = await _snapshots([p.listing for p in kept], user_id)
    max_targets = get_market_watch_max_targets()
    user_tz = configurable.get("timezone")
    now = datetime.now(timezone.utc)
    armed: List[str] = []
    changed = False
    for plan in kept:
        snap = snaps.get((plan.listing.key, plan.listing.market)) or {}
        last = _positive(current_price(snap)) if snap else None
        prev_close = _positive(snap.get("previous_close"))
        reason = _already_met(plan, last, prev_close, now)
        if reason is None and plan.pct_from == "now" and last is None:
            reason = 'no live price to measure "now" from; use previous_close or a price level'
        if reason is not None:
            refused.append(f"- {plan.label}: {reason}.")
            continue
        expires_at, warn_at = target_window(plan.listing.key, plan.listing.market, now)
        try:
            row = await arm_target(
                thread_id=thread_id,
                user_id=str(user_id),
                symbol=plan.listing.key,
                market=plan.listing.market.value,
                kind=plan.kind,
                value=plan.value,
                pct_from=_PCT_FROM_STORED.get(plan.pct_from) if plan.pct_from else None,
                armed_price=last,
                expires_at=expires_at,
                warn_at=warn_at,
                max_armed=max_targets,
            )
        except Exception:
            logger.warning("[MarketWatch] arming a target failed", exc_info=True)
            refused.append(f"- {plan.label}: could not be saved, so it is not armed.")
            continue
        if row is None:
            refused.append(
                f"- {plan.label}: this thread already has {max_targets} armed targets; "
                'cancel one first with action="cancel".'
            )
            continue
        changed = True
        armed.append(_armed_line(plan, last, prev_close, expires_at, user_tz))
    if changed:
        await targets_changed(thread_id)
    return armed, refused


def _spellings(symbols: List[str]) -> List[str]:
    """Every name a symbol's watch goes by: unwatching ``SPX`` must reach
    the ``^GSPC`` it was watched as, and ``^GSPC`` the ``SPX`` it was.

    A name that reads as another listing is left out: the Hang Seng is
    stored as ``HSI``, which on the watch list is the US ticker.
    """
    out: List[str] = []
    for symbol in clean_symbols(symbols):
        listing, _ = target_listing(symbol)
        names = (symbol,) if listing is None else tuple(
            n for n in (symbol, listing.key, listing.watch)
            if target_listing(n)[0] == listing
        )
        out.extend(n for n in names if n not in out)
    return out


def _target_listings(symbols: List[str]) -> List[Listing]:
    """The listings *symbols* name, as their targets are stored.

    ``HSI`` is the US ticker and ``^HSI`` the Hang Seng, both stored as
    ``HSI``, so matching the stored symbol alone would reach the other one.
    A symbol the monitor cannot read names none: no target is armed on it.
    """
    out: List[Listing] = []
    for symbol in clean_symbols(symbols):
        listing, _ = target_listing(symbol)
        if listing is not None and (listing.key, listing.market) not in out:
            out.append((listing.key, listing.market))
    return out


async def _cancel(thread_id: str, symbols: Optional[List[str]]) -> Tuple[List[str], bool]:
    """Cancel targets on *symbols* (all when None); ``(labels, failed)``."""
    listings = (
        [(key, market.value) for key, market in _target_listings(symbols)]
        if symbols is not None else None
    )
    try:
        rows = await cancel_targets(thread_id, listings=listings)
    except Exception:
        logger.warning("[MarketWatch] cancelling targets failed", exc_info=True)
        return [], True
    if rows:
        await targets_changed(thread_id)
    return [_row_label(r) for r in rows], False


def _row_market(row: Dict[str, Any]) -> MarketType:
    return MarketType(row.get("market") or "stock")


def _row_label(row: Dict[str, Any]) -> str:
    symbol = watch_spelling(row["symbol"], _row_market(row))
    armed_price = row.get("armed_price")
    if row.get("pct_from") == "armed" and armed_price is not None:
        # "From now" holds only in the call that set it; read back later,
        # it would point at today's price instead of the one it measures from.
        direction = "up" if row["kind"] == "pct_up" else "down"
        return (
            f"{symbol} {direction} {float(row['value']):g}% from "
            f"{_num(float(armed_price))}, its price when set"
        )
    return _describe(symbol, row["kind"], float(row["value"]), row.get("pct_from"))


async def _armed_rows(thread_id: str) -> Optional[List[Dict[str, Any]]]:
    """The thread's armed targets, oldest first; None when they can't be read.

    A target past its expiry is left out although the sweep has not closed
    it yet: the price monitor no longer checks it, so it is not armed.
    """
    try:
        rows = await list_thread_targets(thread_id, recent_seconds=0)
    except Exception:
        logger.warning("[MarketWatch] reading targets failed", exc_info=True)
        return None
    now = datetime.now(timezone.utc)
    return [
        r
        for r in reversed(rows)
        if r.get("status") == "armed" and r["expires_at"] > now
    ]


async def _status(thread_id: str, user_tz: Optional[str]) -> str:
    """What is watched and armed now: the agent's one view of its targets,
    which no later message restates once a compaction drops the result that
    set them."""
    watched = await get_watchlist(thread_id)
    parts = [f"Watching: {', '.join(watched)}." if watched else "Not watching any symbols."]
    rows = await _armed_rows(thread_id)
    if rows is None:
        parts.append("Armed targets could not be read.")
    elif not rows:
        parts.append("No armed targets.")
    else:
        lines = [
            f"- {_row_label(r)}: lapses "
            f"{_lapse_text(r['symbol'], _row_market(r), r['expires_at'], user_tz)}."
            for r in rows
        ]
        parts.append("Armed targets:\n" + "\n".join(lines))
    return "\n\n".join(parts)


async def _still_armed_on(thread_id: str, symbols: Optional[List[str]]) -> List[str]:
    """Symbols among *symbols* (any when None) that keep an armed target."""
    rows = await _armed_rows(thread_id) or []
    listings = set(_target_listings(symbols)) if symbols else None
    out: List[str] = []
    for r in rows:
        if listings is not None and (r["symbol"], _row_market(r)) not in listings:
            continue
        shown = watch_spelling(r["symbol"], _row_market(r))
        if shown not in out:
            out.append(shown)
    return out


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


@tool
async def watch_market(
    config: RunnableConfig,
    symbols: Optional[List[str]] = None,
    action: Literal["watch", "unwatch", "cancel"] = "watch",
    targets: Optional[List[PriceTarget]] = None,
) -> str:
    """Watch tickers for live prices during this conversation, and set price targets that alert you when they hit. The two are independent: a target needs no watch, and a watch needs no target.

    Watch for intraday-sensitive tasks (fast-moving stock, trading decision,
    live market event). Set a target when a price level or a % move should
    bring you back to this thread: the user asked to be told, or your next
    step waits on it. Call with no symbols and no targets to see what is
    watched and armed now.

    Args:
        symbols: Ticker symbols (e.g. ["NVDA", "TSLA"]) to watch. With
            action="unwatch" or "cancel", the symbols to act on; omit to act
            on all of them.
        action: "watch" (default) to watch symbols and set targets, "unwatch"
            to stop live prices (targets stay armed), "cancel" to cancel
            targets.
        targets: Price targets to set. A symbol holds one target per kind
            (above, below, pct_up, pct_down); setting a kind again replaces
            it, whatever its level or pct_from.

    Returns:
        A confirmation, not prices. Watched symbols then arrive on their own as
        `<market-watch>` updates before and inside your turns, so manual
        get_quote pulls are usually unnecessary for them.

    A target fires once. If it hits while you are working, the alert arrives
    in this turn; if it hits after your turn ends, it starts a new turn in
    this thread. A target lasts through the next full trading session of its
    market; shortly before it lapses unhit, an expiring-soon alert arrives
    the same way, so set it again then if it still matters.
    """
    thread_id = _thread_id(config)
    if not thread_id:
        return "Market watch unavailable: no thread context."
    configurable = config.get("configurable") or {}
    not_set = 'Targets were not set: only action="watch" sets them.'

    if action == "cancel":
        cancelled, cancel_failed = await _cancel(thread_id, symbols or None)
        parts: List[str] = []
        if cancelled:
            parts.append(f"Cancelled targets: {'; '.join(cancelled)}.")
        elif not cancel_failed:
            parts.append("No armed targets to cancel.")
        if cancel_failed:
            parts.append("Targets could not be cancelled and are still armed.")
        if targets:
            parts.append(not_set)
        return " ".join(parts)

    if action == "unwatch":
        remaining = await remove_symbols(thread_id, _spellings(symbols) if symbols else None)
        if remaining is None:
            parts = ["Market watch unavailable (cache offline)."]
        elif remaining:
            parts = [f"Stopped. Still watching: {', '.join(remaining)}."]
        else:
            parts = ["Stopped watching all symbols."]
        # Unwatching no longer touches targets, so say which ones outlive it
        # rather than let "stopped" read as the end of them.
        kept = await _still_armed_on(thread_id, symbols or None)
        if kept:
            parts.append(
                f"Targets on {', '.join(kept)} stay armed; cancel them with action=\"cancel\"."
            )
        if targets:
            parts.append(not_set)
        return " ".join(parts)

    if not symbols and not targets:
        return await _status(thread_id, configurable.get("timezone"))

    armed: List[str] = []
    refused: List[str] = []
    if targets:
        armed, refused = await _set_targets(thread_id, configurable, targets)

    sections: List[str] = []
    feed_on = False
    if symbols:
        watched = await add_symbols(thread_id, symbols)
        if watched is None:
            # Targets live in the database and are watched without the cache,
            # so an outage here must not read as their failure too.
            sections.append(
                "Live prices unavailable (cache offline); the targets below are still armed."
                if armed
                else "Market watch unavailable (cache offline)."
            )
        elif not watched:
            sections.append("No valid ticker symbols provided.")
        else:
            feed_on = True
            line = f"Now watching: {', '.join(watched)}."
            rejected = _rejected(symbols, watched)
            if rejected:
                line += (
                    f" Not added (invalid symbol or watch list full): {', '.join(rejected)}."
                )
            sections.append(line)
    if armed:
        sections.append("Targets set:\n" + "\n".join(armed))
    if refused:
        sections.append("Targets not set:\n" + "\n".join(refused))
    if feed_on:
        sections.append(_FEED_INSTRUCTIONS)
    return "\n\n".join(sections)
