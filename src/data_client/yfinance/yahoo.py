"""Yahoo's side of the symbol boundary for this package.

Yahoo resolves Shanghai only as ``.SS``, where everything outside this package
writes ``.SH``. Every symbol handed to yfinance goes through one of these
helpers, so a new call site cannot forget the respell; a row Yahoo returns
goes back through ``display_spelling`` before it leaves the package.
"""

from __future__ import annotations

import math  # DALTON-HOOK(yfinance-prev-close)

import yfinance as yf
from market_protocol import vendor_spelling

_FX_CURRENCIES = frozenset({  # DALTON-HOOK(market-data-fallback)
    "AED", "ARS", "AUD", "BRL", "CAD", "CHF", "CLP", "CNH", "CNY", "COP",
    "CZK", "DKK", "EGP", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR",
    "JPY", "KRW", "KWD", "MXN", "MYR", "NOK", "NZD", "PEN", "PHP", "PKR",
    "PLN", "QAR", "RON", "RUB", "SAR", "SEK", "SGD", "THB", "TRY", "TWD",
    "UAH", "USD", "VND", "XAG", "XAU", "ZAR",
})


def _yahoo_fx_symbol(symbol: str) -> str | None:  # DALTON-HOOK(market-data-fallback)
    """Normalize a bare (``USDCNY``) or canonical hyphenated (``USD-CNY``) FX pair to Yahoo's ``=X`` spelling."""
    if "." in symbol or "=" in symbol or symbol.startswith("^"):
        return None
    if "-" in symbol:
        parts = symbol.split("-")
        if len(parts) != 2:
            return None
        base, quote = parts
    elif len(symbol) == 6 and symbol.isascii() and symbol.isalpha():
        base, quote = symbol[:3], symbol[3:]
    else:
        return None
    if base != quote and base in _FX_CURRENCIES and quote in _FX_CURRENCIES:
        return f"{base}{quote}=X"
    return None


class _CookieCacheWithA3:
    """yfinance's cookie cache, less an entry without the ``A3`` cookie.

    yfinance reads the cached cookie's ``A3`` without checking it is there, so
    one saved without it fails every request in every later process with
    ``KeyError: 'A3'``. Such an entry reads as a miss and is deleted. The cache
    stays: yfinance refetches the cookie on every request without it. The
    sandbox servers carry the same guard in ``mcp_servers/_yf_common.py``,
    since this package is mirrored into sandboxes where that one may not import.
    """

    checks_a3 = True

    def __init__(self, inner):
        self._inner = inner

    def lookup(self, strategy):
        entry = self._inner.lookup(strategy)
        try:
            next(iter(entry["cookie"].values()))["/"]["A3"]
        except (TypeError, KeyError, StopIteration, AttributeError):
            if entry is not None:
                self._inner.store(strategy, None)  # a None store deletes the row
            return None
        return entry

    def store(self, strategy, cookie):
        self._inner.store(strategy, cookie)

    # DALTON-HOOK(yfinance-cache): everything else the cache exposes (its
    # ``db`` handle among it — the per-process cache the proxy layer swaps in
    # reads it) passes through untouched.
    def __getattr__(self, name):
        return getattr(self._inner, name)


def _guard_cookie_cache() -> None:
    try:
        from yfinance import cache
        current = cache.get_cookie_cache()
        if not getattr(current, "checks_a3", False):
            cache._CookieCacheManager._Cookie_cache = _CookieCacheWithA3(current)
    except (ImportError, AttributeError):
        pass  # a release that moved the cache; the load it guards moved with it


_guard_cookie_cache()


def yahoo_symbol(symbol: str, *, is_index: bool = False) -> str:
    """Yahoo's spelling of *symbol*; a bare index gets its caret (``^GSPC``).

    A venue-suffixed index (``000300.SS``) is already a Yahoo symbol and never
    takes a caret.
    """
    s = vendor_spelling(symbol)
    if not is_index and (fx := _yahoo_fx_symbol(s)) is not None:  # DALTON-HOOK(market-data-fallback)
        return fx
    if not is_index or s.startswith("^") or "." in s or "=" in s:  # DALTON-HOOK(stock-move-upgrade)
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


def validated_previous_close(  # DALTON-HOOK(yfinance-prev-close)
    fast_info_prev: float | None,
    daily_rows: list[tuple[str, float, float]] | None,
    quote_date: str | None,
) -> float | None:
    """The previous close, only once recent daily bars confirm it (2525.HK 2026-10).

    Yahoo froze 2525.HK's daily rows at 15.47 with zero volume for sessions it
    never traded, and yfinance derived ``regularMarketPreviousClose`` from that
    tail, so the quote showed a phoney −16.61% two sessions after the real
    close had moved on. The previous close is the last bar strictly before the
    quote's own trading date among *valid* rows — zero-volume rows are Yahoo's
    fill, not a session. That value is cross-checked against the fast_info
    number; a conflict means neither can be trusted, so both the previous close
    and the change come back unknown rather than wrong. *daily_rows* None
    marks a history that could not be fetched (transport failure or an empty
    frame) — with no bars to check against, the fast_info number is exactly
    the unverified tail that once read −16.61%, so the previous close and the
    change come back unknown then too. An instrument whose whole window
    carries no volume (indexes, FX) has no fill rows to skip, so the
    date-adjacent bar is its previous session.
    """
    if daily_rows is None:
        return None

    rows_before = [r for r in daily_rows if not quote_date or r[0] < quote_date]
    any_volume = any(v > 0 for _, _, v in daily_rows)
    candidates = [r for r in rows_before if r[2] > 0] if any_volume else rows_before
    if not candidates:
        # Rows exist but every one before the quote is a frozen fill: there is
        # no valid previous session to quote a change against.
        return None
    daily_prev = candidates[-1][1]

    if fast_info_prev is None:
        return daily_prev
    if math.isclose(daily_prev, fast_info_prev, rel_tol=1e-3, abs_tol=1e-4):
        return fast_info_prev
    return None


def yahoo_symbol_search(symbol: str, **kwargs) -> yf.Search:
    """A search keyed on a ticker; a free-text name query calls ``yf.Search`` as typed."""
    return yf.Search(yahoo_symbol(symbol), **kwargs)
