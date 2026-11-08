"""Yahoo's side of the symbol boundary for this package.

Yahoo resolves Shanghai only as ``.SS``, where everything outside this package
writes ``.SH``. Every symbol handed to yfinance goes through one of these
helpers, so a new call site cannot forget the respell; a row Yahoo returns
goes back through ``display_spelling`` before it leaves the package.
"""

from __future__ import annotations

import math  # DALTON-HOOK(yfinance-prev-close)
import threading  # DALTON-HOOK(yfinance-prev-close)
import time  # DALTON-HOOK(yfinance-prev-close)
from typing import Any  # DALTON-HOOK(yfinance-prev-close)

import yfinance as yf
from market_protocol import AssetClass, to_canonical, vendor_spelling

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
    takes a caret. A rate is ``EURUSD=X`` and a coin ``BTC-USD``, however it
    arrives: on Yahoo the pair spelling ``EUR-USD`` is a defunct coin.
    """
    s = vendor_spelling(symbol)
    if is_index:
        return s if s.startswith("^") or "." in s or "=" in s else f"^{s}"  # DALTON-HOOK(stock-move-upgrade)
    if (fx := _yahoo_fx_symbol(s)) is not None:  # DALTON-HOOK(market-data-fallback)
        return fx
    try:
        ref = to_canonical(s)
    except ValueError:
        return s  # a spelling the protocol refuses still gets to ask Yahoo
    if ref.asset_class is AssetClass.FX:
        return f"{ref.symbol.replace('-', '')}=X"
    if ref.asset_class is AssetClass.CRYPTO:
        return ref.symbol
    return s


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


# DALTON-HOOK(yfinance-prev-close): quote types that trade with volume; for
# these a window with no traded row is a long suspension, not a volumeless
# instrument, so its zero-volume fills must never stand as a previous close.
_VOLUME_REQUIRED_QUOTE_TYPES = frozenset({"EQUITY", "ETF"})  # DALTON-HOOK(yfinance-prev-close)


def _normalize_quote_type(value: Any) -> str | None:  # DALTON-HOOK(yfinance-prev-close)
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def quote_type_from_history_metadata(meta: Any) -> str | None:  # DALTON-HOOK(yfinance-prev-close)
    """Yahoo's ``instrumentType`` (yfinance's ``quoteType``) from ``history_metadata``.

    Read it right after the snapshot's own ``history()`` call: yfinance's
    ``history_metadata`` is a live view that a later ``history()`` call
    replaces (emptied when that call fails), and reading it costs no request.
    """
    get = getattr(meta, "get", None)
    if not callable(get):
        return None
    return _normalize_quote_type(get("instrumentType"))


def validated_previous_close(  # DALTON-HOOK(yfinance-prev-close)
    fast_info_prev: float | None,
    daily_rows: list[tuple[str, float, float]] | None,
    quote_date: str | None,
    quote_type: str | None = None,
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

    On the resumption day itself (2525.HK 2026-10-26) that is still not
    enough: the last bar that traded (10/12) agreed with the fast_info number
    yet quoted the day at −12.28% against a real ~+8.6% move, because the
    exchange's resumption reference was not the pre-suspension close. Zero-
    volume fills between the last bar that traded and the quote's own date
    mean sessions in between were never fetched, so no bar can stand as the
    previous session's close — the previous close and the change come back
    unknown then too, not wrong.

    *quote_type* is yfinance's ``quoteType`` when the caller already has it.
    ``EQUITY``/``ETF`` trade with volume, so a whole window without one
    traded row is a suspension longer than the window, not a volumeless
    instrument: its fills are not a previous close and the result is unknown.
    Volumeless types (``INDEX``, ``CURRENCY``, …) and an unknown type keep
    the volumeless-window behaviour above, unchanged.
    """
    normalized_type = _normalize_quote_type(quote_type)  # DALTON-HOOK(yfinance-prev-close)
    if daily_rows is None:
        return None

    rows_before = [r for r in daily_rows if not quote_date or r[0] < quote_date]
    any_volume = any(v > 0 for _, _, v in daily_rows)
    if normalized_type in _VOLUME_REQUIRED_QUOTE_TYPES and not any_volume:  # DALTON-HOOK(yfinance-prev-close)
        # EQUITY/ETF with no traded row in the whole window (suspended longer
        # than a month): every row is Yahoo fill, so there is no previous
        # session to quote against — unknown, never the fill price.
        return None
    if not rows_before and not any_volume:
        # Volume-less instrument (e.g. an index) whose daily series holds
        # only the current session's bar: there are no fill rows before the
        # quote date to reject, so the chart's previous close stands.
        return fast_info_prev
    candidates = [r for r in rows_before if r[2] > 0] if any_volume else rows_before
    if not candidates:
        # Rows exist but every one before the quote is a frozen fill: there is
        # no valid previous session to quote a change against.
        return None
    last_valid = candidates[-1]
    if any_volume and any(r[2] <= 0 for r in rows_before if r[0] > last_valid[0]):
        # Frozen fills sit between the last bar that traded and the quote's
        # own date: the sessions in between were never fetched, so the last
        # traded bar's close is not the previous session's close (2525.HK
        # resumed 2026-10-26 quoting −12.28% on this path; the real move was
        # ~+8.6%).
        return None
    daily_prev = last_valid[1]

    if fast_info_prev is None:
        return daily_prev
    if math.isclose(daily_prev, fast_info_prev, rel_tol=1e-3, abs_tol=1e-4):
        return fast_info_prev
    return None


# DALTON-HOOK(yfinance-prev-close): daily-rows helper & (symbol, trading date) memo
def _bar_date_str(idx: Any) -> str | None:
    if hasattr(idx, "strftime"):
        try:
            return idx.strftime("%Y-%m-%d")
        except Exception:
            return None
    return None


def _daily_rows_from_frame(df: Any) -> list[tuple[str, float, float]] | None:  # DALTON-HOOK(yfinance-prev-close)
    if df is None or getattr(df, "empty", True):
        return None
    rows: list[tuple[str, float, float]] = []
    for idx, row in df.iterrows():
        date_str = _bar_date_str(idx)
        try:
            close = float(row["Close"])
            volume = float(row["Volume"])
        except (TypeError, ValueError, KeyError):
            continue
        if date_str is None or not math.isfinite(close) or close <= 0:
            continue
        if volume != volume:  # NaN != NaN
            volume = 0.0
        rows.append((date_str, close, volume))
    return rows or None


def _recent_daily_rows(ticker: Any) -> list[tuple[str, float, float]] | None:  # DALTON-HOOK(yfinance-prev-close)
    """A month of daily bars as ``(date, close, volume)``, ascending."""
    try:
        df = ticker.history(period="1mo", auto_adjust=False)
    except Exception:
        return None
    return _daily_rows_from_frame(df)


_DAILY_ROWS_CACHE_TTL_S = 600.0
_DAILY_ROWS_CACHE_MAX = 256
_daily_rows_cache: dict[tuple[str, str], tuple[float, list[tuple[str, float, float]]]] = {}
# DALTON-HOOK(yfinance-prev-close): snapshots run in parallel threads; every
# read, write and eviction of the memo below happens under this lock, or the
# eviction's sorted(items()) can die with "dictionary changed size during
# iteration" and take the whole snapshot's quote with it.
_daily_rows_cache_lock = threading.Lock()  # DALTON-HOOK(yfinance-prev-close)


def _clear_prev_close_daily_cache() -> None:  # DALTON-HOOK(yfinance-prev-close)
    """Empty the previous-close daily-rows memo (tests isolate through this)."""
    with _daily_rows_cache_lock:  # DALTON-HOOK(yfinance-prev-close)
        _daily_rows_cache.clear()


def _recent_daily_rows_cached(  # DALTON-HOOK(yfinance-prev-close)
    sym: str, quote_date: str | None, ticker: Any
) -> list[tuple[str, float, float]] | None:
    """`_recent_daily_rows` behind the (symbol, trading date) memo.

    The lock is never held across the network fetch: check under the lock,
    fetch unlocked on a miss, then write and evict under the lock again.
    """
    key = (sym, quote_date or "")  # DALTON-HOOK(yfinance-prev-close)
    with _daily_rows_cache_lock:  # DALTON-HOOK(yfinance-prev-close)
        hit = _daily_rows_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < _DAILY_ROWS_CACHE_TTL_S:
            return hit[1]
    rows = _recent_daily_rows(ticker)  # DALTON-HOOK(yfinance-prev-close): fetched outside the lock
    if rows is not None:
        with _daily_rows_cache_lock:  # DALTON-HOOK(yfinance-prev-close)
            _daily_rows_cache[key] = (time.monotonic(), rows)
            if len(_daily_rows_cache) > _DAILY_ROWS_CACHE_MAX:
                stale = sorted(_daily_rows_cache.items(), key=lambda kv: kv[1][0])
                for stale_key, _entry in stale[: _DAILY_ROWS_CACHE_MAX // 2]:
                    _daily_rows_cache.pop(stale_key, None)
    return rows


def yahoo_symbol_search(symbol: str, **kwargs) -> yf.Search:
    """A search keyed on a ticker; a free-text name query calls ``yf.Search`` as typed."""
    return yf.Search(yahoo_symbol(symbol), **kwargs)
