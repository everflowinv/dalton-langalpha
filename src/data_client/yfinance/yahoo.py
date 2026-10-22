"""Yahoo's side of the symbol boundary for this package.

Yahoo resolves Shanghai only as ``.SS``, where everything outside this package
writes ``.SH``. Every symbol handed to yfinance goes through one of these
helpers, so a new call site cannot forget the respell; a row Yahoo returns
goes back through ``display_spelling`` before it leaves the package.
"""

from __future__ import annotations

import math

import yfinance as yf
from market_protocol import vendor_spelling


def yahoo_symbol(symbol: str, *, is_index: bool = False) -> str:
    """Yahoo's spelling of *symbol*; a bare index gets its caret (``^GSPC``).

    A venue-suffixed index (``000300.SS``) is already a Yahoo symbol and never
    takes a caret.
    """
    s = vendor_spelling(symbol)
    if not is_index or s.startswith("^") or "." in s:
        return s
    return f"^{s}"


def yahoo_ticker(symbol: str) -> yf.Ticker:
    return yf.Ticker(yahoo_symbol(symbol))


def regular_previous_close(fast_info) -> float | None:  # DALTON-HOOK(yfinance-prev-close)
    """The previous regular session's close, the base a day's change is quoted against.

    yfinance's ``fast_info.previousClose`` is not that close for US listings
    (GOOGL 2026-10-21: 333.10 against an official 340.28), so the day's change
    came out wrong in sign and size; ``regularMarketPreviousClose`` matches the
    daily bars on every market checked (US, HK, Shanghai, indexes, ETFs).
    """
    for key in ("regularMarketPreviousClose", "previousClose"):
        v = fast_info.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0:
            return float(v)
    return None


def yahoo_symbol_search(symbol: str, **kwargs) -> yf.Search:
    """A search keyed on a ticker; a free-text name query calls ``yf.Search`` as typed."""
    return yf.Search(yahoo_symbol(symbol), **kwargs)
