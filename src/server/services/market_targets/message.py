"""What a target's notice says to the model, and what it tells the UI.

The running turn and a wake turn put the same text in the same user-role
slot, so this is the only place it is worded. It opens like the
``<market-watch>`` stamp: a tagged block that says up front it is automated,
because the slot it rides is the one the user's own messages use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Union

from market_protocol import AssetClass, InstrumentRef

from src.server.models.automation import MarketType
from src.server.services.market_targets.listing import watch_spelling
from src.tools.market_data.currency import fmt_price
from src.tools.market_data.display import (
    _symbol_currency,
    resolve_ref,
    venue_local_time,
)
from src.tools.market_data.quote_format import current_price

ALERT_OPEN = "<market-alert>"
ALERT_CLOSE = "</market-alert>"
_ALERT_NOTE = (
    "Automated alert from a price target you set with watch_market "
    "(not a user message)."
)

# Stored ``pct_from`` -> the vocabulary the agent's tool and the thread API
# speak, so the UI reads one spelling whichever surface fed it.
_TOOL_PCT_FROM = {"previous_close": "previous_close", "armed": "now"}

Quote = Union[float, Mapping[str, Any], None]


def listing_ref(target: Mapping[str, Any]) -> Optional[InstrumentRef]:
    """The listing a target watches; its market decides what a bare spelling
    names (``HSI`` on an index target is the Hang Seng)."""
    asset_class = (
        AssetClass.INDEX if target.get("market") == "index" else AssetClass.EQUITY
    )
    return resolve_ref(target.get("symbol"), asset_class)


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _iso(value: Any) -> Optional[str]:
    at = _as_datetime(value)
    return at.isoformat() if at else None


def _venue_time(ref: Optional[InstrumentRef], value: Any) -> Optional[str]:
    """``2026-10-12 10:31 EDT``: the venue's own clock, since that is the
    clock a session, a close and an expiry are read on."""
    at = _as_datetime(value)
    if at is None:
        return None
    local = venue_local_time(ref, at) or at.astimezone(timezone.utc)
    return f"{local.strftime('%Y-%m-%d %H:%M')} {local.strftime('%Z') or 'UTC'}"


def _price(value: Optional[float], ref: Optional[InstrumentRef]) -> str:
    try:
        return fmt_price(value, _symbol_currency(ref), group=True)
    except Exception:
        return f"{value:,.2f}" if value is not None else "N/A"


def _pct(value: Any) -> str:
    return f"{float(value):g}%"


def _target_phrase(target: Mapping[str, Any], ref: Optional[InstrumentRef]) -> str:
    """The target as the agent set it: ``above $230.00``, ``up 3% from the
    previous close``."""
    kind = target.get("kind")
    value = target.get("value")
    if kind in ("above", "below"):
        return f"{kind} {_price(value, ref)}"
    direction = "up" if kind == "pct_up" else "down"
    if target.get("pct_from") == "armed" and target.get("armed_price") is not None:
        base = (
            f"{_price(target['armed_price'], ref)}, "
            f"its price when you set the target"
        )
    else:
        base = "the previous close"
    return f"{direction} {_pct(value)} from {base}"


def _move_from_armed(target: Mapping[str, Any], price: Optional[float]) -> str:
    """`` (+3.02%)`` against the arming price, the one base the row holds."""
    base = target.get("armed_price")
    if (
        price is None
        or target.get("pct_from") != "armed"
        or not isinstance(base, (int, float))
        or base <= 0
    ):
        return ""
    change = (price - base) / base * 100
    return f" ({'+' if change >= 0 else ''}{change:.2f}%)"


def _quote_price(quote: Quote) -> Optional[float]:
    if isinstance(quote, (int, float)):
        return float(quote)
    if isinstance(quote, Mapping):
        try:
            return current_price(dict(quote))
        except Exception:
            return None
    return None


def _shown_symbol(target: Mapping[str, Any]) -> Optional[str]:
    """The spelling the tool, the thread API and the chip use, so an index
    target is ``^GSPC`` everywhere rather than its stored ``SPX`` here."""
    symbol = target.get("symbol")
    if not symbol:
        return None
    try:
        return watch_spelling(symbol, MarketType(target.get("market") or "stock"))
    except ValueError:
        return symbol


def render_notice(target: Mapping[str, Any], notice: str, quote: Quote = None) -> str:
    """The ``<market-alert>`` block for one notice.

    ``quote`` is the latest price (a number or a quote row), shown on an
    ``expiring`` notice when known; a hit carries the price it fired at.
    """
    ref = listing_ref(target)
    symbol = _shown_symbol(target) or "?"
    phrase = _target_phrase(target, ref)
    if notice == "hit":
        fired_price = target.get("fired_price")
        line = f"{symbol} hit your target ({phrase}): it traded at {_price(fired_price, ref)}"
        line += _move_from_armed(target, fired_price)
        when = _venue_time(ref, target.get("fired_at"))
        if when:
            line += f" at {when}"
        body = [f"{line}.", "The target is spent and will not fire again."]
    else:
        line = f"Your target on {symbol} ({phrase}) has not hit"
        when = _venue_time(ref, target.get("expires_at"))
        line += f" and expires at {when}." if when else " and expires soon."
        last = _quote_price(quote)
        if last is not None:
            line += f" Last price: {_price(last, ref)}{_move_from_armed(target, last)}."
        body = [
            line,
            "To keep it, set the same target again with watch_market, "
            "which restarts its window; otherwise let it expire.",
        ]
    return "\n".join([ALERT_OPEN, _ALERT_NOTE, *body, ALERT_CLOSE])


def _number(value: Any) -> Optional[float]:
    """A JSON number: a ``Decimal`` from a raw row would serialize as a
    string, which the client cannot compare or format."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def notice_payload(target: Mapping[str, Any], notice: str) -> dict[str, Any]:
    """One notice as the UI reads it: a ``steering_delivered`` entry's
    ``market_alert`` when a live turn took it in, and a wake turn's query
    metadata. Enough to draw the alert row without reading the target back."""
    return {
        "target_id": str(target.get("target_id")),
        "notice": notice,
        "symbol": _shown_symbol(target),
        "kind": target.get("kind"),
        "value": _number(target.get("value")),
        "pct_from": _TOOL_PCT_FROM.get(target.get("pct_from") or ""),
        "armed_price": _number(target.get("armed_price")),
        "fired_price": _number(target.get("fired_price")),
        "fired_at": _iso(target.get("fired_at")),
        "expires_at": _iso(target.get("expires_at")),
    }
