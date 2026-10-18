#!/usr/bin/env python3
"""Market cap, period-end, and FX anchor resolver for Dalton Financial Lenses (PS.2).

Fixes two historical bugs from OpenClaw skills:
1. HK-listed companies reporting in CNY (`financial_currency="CNY"`, `listing_currency="HKD"`):
   reporting FX (`fx_pair` / `rate`) MUST follow `financial_currency` (`CNYUSD=X`),
   NOT `listing_currency` (`HKDUSD=X`), while listing-price conversion uses `listing_fx`.
2. `price_usd` MUST be converted from `price_local` into USD via `listing_fx_rate`
   (`price_local * listing_fx_rate`), never left as raw local-currency price.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from typing import Any

DEFAULT_FX_TO_USD: dict[str, float] = {
    "USD": 1.0,
    "HKD": 0.1282,
    "CNY": 0.1380,
    "RMB": 0.1380,
    "JPY": 0.0067,
    "EUR": 1.0850,
    "GBP": 1.2700,
    "SGD": 0.7450,
    "KRW": 0.00073,
    "TWD": 0.0312,
    "INR": 0.0120,
    "AUD": 0.6550,
    "CAD": 0.7350,
}

MARKET_DEFAULT_CURRENCY: dict[str, str] = {
    "US": "USD",
    "HK": "HKD",
    "CN": "CNY",
    "JP": "JPY",
    "SG": "SGD",
    "KR": "KRW",
    "TW": "TWD",
    "UK": "GBP",
}


def normalize_currency(currency: str | None, default: str = "USD") -> str:
    if not currency:
        return default
    c = currency.strip().upper()
    if c == "RMB":
        return "CNY"
    return c


def detect_market(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.endswith(".HK"):
        return "HK"
    if t.endswith((".SS", ".SZ", ".SH", ".BJ")):
        return "CN"
    if t.endswith(".T"):
        return "JP"
    if t.endswith(".SI"):
        return "SG"
    if t.endswith((".KS", ".KQ")):
        return "KR"
    if t.endswith((".TW", ".TWO")):
        return "TW"
    if t.endswith(".L"):
        return "UK"
    if re.fullmatch(r"\d{1,5}", t):
        # Pure digits without suffix: 4-5 digits in Dalton coverage typically HK, 6 digits CN
        return "HK" if len(t) <= 5 else "CN"
    return "US"


def normalize_hk_ticker(ticker: str) -> str:
    t = ticker.strip().upper()
    m = re.fullmatch(r"(\d+)\.HK", t)
    if m:
        digits = m.group(1).lstrip("0") or "0"
        return f"{digits.zfill(4)}.HK"
    if re.fullmatch(r"\d{1,5}", t):
        digits = t.lstrip("0") or "0"
        return f"{digits.zfill(4)}.HK"
    return t


def resolve_fx_pair(currency: str) -> str | None:
    """Return Yahoo Finance FX ticker converting 1 unit of `currency` to USD."""
    c = normalize_currency(currency)
    if c == "USD":
        return None
    return f"{c}USD=X"


def fetch_fx_rate_to_usd(
    currency: str,
    explicit_rate: float | None = None,
    allow_network: bool = False,
) -> tuple[float, str | None, str]:
    """Return (rate_to_usd, fx_pair, source) for converting 1 unit of `currency` to USD."""
    c = normalize_currency(currency)
    if c == "USD":
        return 1.0, None, "identity (USD=1.0)"
    pair = resolve_fx_pair(c)
    if explicit_rate is not None:
        if explicit_rate <= 0:
            raise ValueError(f"FX rate for {c}->USD must be positive, got {explicit_rate}")
        return float(explicit_rate), pair, f"explicit override ({c}->USD)"

    if allow_network and pair:
        try:
            import yfinance as yf  # type: ignore

            hist = yf.Ticker(pair).history(period="5d")
            if hist is not None and not hist.empty:
                val = float(hist["Close"].dropna().iloc[-1])
                if val > 0:
                    return val, pair, f"yfinance ({pair})"
        except Exception:
            pass

    if c in DEFAULT_FX_TO_USD:
        return DEFAULT_FX_TO_USD[c], pair, f"fallback reference ({c}->USD)"
    raise ValueError(f"Unsupported currency '{c}' without explicit --fx-rate")


def build_anchors(
    ticker: str,
    period_end: str | None = None,
    *,
    market_cap_usd_mn: float | None = None,
    price_local: float | None = None,
    shares: float | None = None,
    listing_currency: str | None = None,
    financial_currency: str | None = None,
    fx_rate: float | None = None,
    listing_fx_rate: float | None = None,
    as_of: str | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    """Build unified period-end, FX, and market-cap anchors across all three financial lenses.

    Guarantees:
    - `fx` reflects `financial_currency -> USD` (for financial statement items).
    - `listing_fx` reflects `listing_currency -> USD` (for share price / local market cap).
    - `price_usd` is ALWAYS converted to USD (`price_local * listing_fx.rate`), never left in local currency.
    """
    market = detect_market(ticker)
    normalized_ticker = normalize_hk_ticker(ticker) if market == "HK" else ticker.strip().upper()
    default_ccy = MARKET_DEFAULT_CURRENCY.get(market, "USD")

    # Optional network enrichment when offline values are omitted
    yf_info: dict[str, Any] = {}
    if allow_network and (
        listing_currency is None
        or financial_currency is None
        or (market_cap_usd_mn is None and price_local is None)
    ):
        try:
            import yfinance as yf  # type: ignore

            t_obj = yf.Ticker(normalized_ticker)
            yf_info = dict(t_obj.info or {})
        except Exception:
            yf_info = {}

    resolved_listing_ccy = normalize_currency(
        listing_currency or yf_info.get("currency"), default=default_ccy
    )
    resolved_financial_ccy = normalize_currency(
        financial_currency or yf_info.get("financialCurrency"),
        default=resolved_listing_ccy,
    )

    # 1. Reporting FX: financial_currency -> USD (fixes HK ticker + CNY reporting bug)
    rep_rate, rep_pair, rep_source = fetch_fx_rate_to_usd(
        resolved_financial_ccy, explicit_rate=fx_rate, allow_network=allow_network
    )

    # 2. Listing FX: listing_currency -> USD (fixes price_usd conversion bug)
    if resolved_listing_ccy == resolved_financial_ccy and listing_fx_rate is None:
        list_rate, list_pair, list_source = rep_rate, rep_pair, rep_source
    else:
        list_rate, list_pair, list_source = fetch_fx_rate_to_usd(
            resolved_listing_ccy, explicit_rate=listing_fx_rate, allow_network=allow_network
        )

    resolved_as_of = as_of or dt.date.today().isoformat()
    resolved_period_end = period_end or yf_info.get("mostRecentQuarter") or "UNSPECIFIED"
    if isinstance(resolved_period_end, (int, float)):
        resolved_period_end = dt.date.fromtimestamp(int(resolved_period_end)).isoformat()

    if price_local is None and yf_info.get("currentPrice") is not None:
        price_local = float(yf_info["currentPrice"])
    if shares is None and yf_info.get("sharesOutstanding") is not None:
        shares = float(yf_info["sharesOutstanding"])

    # Convert price_local -> price_usd via listing FX rate (never raw price_local!)
    price_usd: float | None = None
    if price_local is not None:
        price_usd = round(float(price_local) * list_rate, 6)

    # Compute market_cap_usd_mn
    mcap_source = "explicit --market-cap-usd-mn"
    if market_cap_usd_mn is None:
        if price_local is not None and shares is not None:
            # shares may be absolute count or millions; if shares > 1e5, treat as raw share count
            shares_mn = float(shares) / 1e6 if float(shares) > 1e5 else float(shares)
            market_cap_local_mn = float(price_local) * shares_mn
            market_cap_usd_mn = round(market_cap_local_mn * list_rate, 2)
            mcap_source = "computed (price_local * shares * listing_fx_rate)"
        elif yf_info.get("marketCap") is not None:
            market_cap_local_mn = float(yf_info["marketCap"]) / 1e6
            market_cap_usd_mn = round(market_cap_local_mn * list_rate, 2)
            mcap_source = "yfinance marketCap * listing_fx_rate"
    else:
        market_cap_usd_mn = round(float(market_cap_usd_mn), 2)

    shares_mn_out: float | None = None
    if shares is not None:
        shares_mn_out = round(float(shares) / 1e6 if float(shares) > 1e5 else float(shares), 4)

    return {
        "ticker": normalized_ticker,
        "market": market,
        "period_end": str(resolved_period_end),
        "as_of": resolved_as_of,
        "listing_currency": resolved_listing_ccy,
        "financial_currency": resolved_financial_ccy,
        "fx": {
            "pair": rep_pair or "USDUSD=X",
            "from_currency": resolved_financial_ccy,
            "to_currency": "USD",
            "rate": round(rep_rate, 6),
            "as_of": resolved_as_of,
            "source": rep_source,
        },
        "listing_fx": {
            "pair": list_pair or "USDUSD=X",
            "from_currency": resolved_listing_ccy,
            "to_currency": "USD",
            "rate": round(list_rate, 6),
            "as_of": resolved_as_of,
            "source": list_source,
        },
        "market_cap": {
            "market_cap_usd": market_cap_usd_mn,
            "price_local": round(float(price_local), 4) if price_local is not None else None,
            "price_usd": round(price_usd, 4) if price_usd is not None else None,
            "shares_mn": shares_mn_out,
            "as_of": resolved_as_of,
            "source": mcap_source if market_cap_usd_mn is not None else "unavailable",
        },
    }


def convert_reporting_to_usd(amount_reporting: float, anchors: dict[str, Any]) -> float:
    """Convert a financial statement figure (in `financial_currency`) into USD."""
    rate = float(anchors["fx"]["rate"])
    return round(float(amount_reporting) * rate, 2)


def convert_listing_to_usd(amount_listing: float, anchors: dict[str, Any]) -> float:
    """Convert a market/listing figure (in `listing_currency`) into USD."""
    rate = float(anchors["listing_fx"]["rate"])
    return round(float(amount_listing) * rate, 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build unified period-end, FX, and market-cap anchors for Dalton Financial Lenses."
    )
    parser.add_argument("--ticker", required=True, help="Ticker symbol (e.g. 0700.HK, AAPL, 600519.SS)")
    parser.add_argument("--period-end", default=None, help="Financial statement period end (YYYY-MM-DD)")
    parser.add_argument("--market-cap-usd-mn", type=float, default=None, help="Market cap in USD millions")
    parser.add_argument("--price-local", type=float, default=None, help="Share price in listing currency")
    parser.add_argument("--shares", type=float, default=None, help="Diluted shares (raw count or millions)")
    parser.add_argument("--listing-currency", default=None, help="Listing currency (e.g. HKD, USD, JPY)")
    parser.add_argument("--financial-currency", default=None, help="Reporting currency (e.g. CNY, USD, HKD)")
    parser.add_argument("--fx-rate", type=float, default=None, help="Reporting currency -> USD rate")
    parser.add_argument("--listing-fx-rate", type=float, default=None, help="Listing currency -> USD rate")
    parser.add_argument("--as-of", default=None, help="Valuation anchor date (YYYY-MM-DD)")
    parser.add_argument("--allow-network", action="store_true", help="Allow live yfinance lookup if needed")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    result = build_anchors(
        args.ticker,
        args.period_end,
        market_cap_usd_mn=args.market_cap_usd_mn,
        price_local=args.price_local,
        shares=args.shares,
        listing_currency=args.listing_currency,
        financial_currency=args.financial_currency,
        fx_rate=args.fx_rate,
        listing_fx_rate=args.listing_fx_rate,
        as_of=args.as_of,
        allow_network=args.allow_network,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
