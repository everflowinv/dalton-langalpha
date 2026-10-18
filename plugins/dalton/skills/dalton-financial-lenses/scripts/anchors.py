#!/usr/bin/env python3
"""Market cap, period-end, and FX anchor resolver for Dalton Financial Lenses (PS.2).

Fixes historical bugs from OpenClaw skills & R7 review:
1. HK-listed companies reporting in CNY (`financial_currency="CNY"`, `listing_currency="HKD"`):
   reporting FX (`fx_pair` / `rate`) MUST follow `financial_currency` (`CNYUSD=X`),
   NOT `listing_currency` (`HKDUSD=X`), while listing-price conversion uses `listing_fx`.
2. `price_usd` MUST be converted from `price_local` into USD via `listing_fx_rate`
   (`price_local * listing_fx_rate`), never left as raw local-currency price.
3. No hardcoded fallback FX rate table: FX rates must come from explicit `--fx-rate` /
   `--listing-fx-rate` or live `yfinance` (`--allow-network`), otherwise fail with a clear error.
4. Non-US stocks and known ADRs require explicit `--financial-currency` (`--reporting-currency`)
   when offline, preventing silent USD=1.0 mis-scaling of CNY/HKD/TWD/EUR financial statements.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from typing import Any

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

KNOWN_FOREIGN_OR_ADR_US_TICKERS: set[str] = {
    "BABA",
    "JD",
    "PDD",
    "BIDU",
    "NTES",
    "TCOM",
    "BEKE",
    "LI",
    "NIO",
    "XPEV",
    "ZTO",
    "FUTU",
    "TIGR",
    "BILI",
    "IQ",
    "TME",
    "VIPS",
    "YMM",
    "BZ",
    "HTHT",
    "ATAT",
    "MNSO",
    "TAL",
    "EDU",
    "QFIN",
    "FINV",
    "TSM",
    "ASML",
    "ARM",
    "SONY",
    "TM",
    "NVO",
    "SAP",
    "SE",
    "GRAB",
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
    """Return (rate_to_usd, fx_pair, source) for converting 1 unit of `currency` to USD.

    FX rates must come ONLY from explicit argument or live network fetch.
    """
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
                close_series = hist["Close"].dropna()
                if not close_series.empty:
                    val = float(close_series.iloc[-1])
                    if val > 0:
                        return val, pair, f"yfinance ({pair})"
        except Exception as exc:
            raise ValueError(
                f"Live FX lookup failed for {pair} ({type(exc).__name__}); pass explicit --fx-rate"
            ) from exc
        raise ValueError(
            f"Live FX lookup returned no data for {pair}; pass explicit --fx-rate"
        )

    raise ValueError(
        f"Non-USD currency '{c}' requires explicit --fx-rate (or --listing-fx-rate) or --allow-network"
    )


def _normalize_shares_to_mn(shares: float, shares_unit: str = "auto") -> float:
    val = float(shares)
    unit = (shares_unit or "auto").strip().lower()
    if unit == "raw":
        return val / 1e6
    if unit == "thousands":
        return val / 1e3
    if unit == "millions":
        return val
    if unit != "auto":
        raise ValueError(f"Invalid shares_unit '{shares_unit}', expected auto/raw/thousands/millions")
    return val / 1e6 if val > 1e5 else val


def build_anchors(
    ticker: str,
    period_end: str | None = None,
    *,
    market_cap_usd_mn: float | None = None,
    price_local: float | None = None,
    shares: float | None = None,
    shares_unit: str = "auto",
    adr_ratio: float = 1.0,
    listing_currency: str | None = None,
    financial_currency: str | None = None,
    reporting_currency: str | None = None,
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
    - Never uses hardcoded FX tables; requires explicit rate or `--allow-network`.
    """
    if adr_ratio <= 0:
        raise ValueError(f"adr_ratio must be positive, got {adr_ratio}")

    explicit_fin_ccy = financial_currency or reporting_currency
    market = detect_market(ticker)
    normalized_ticker = normalize_hk_ticker(ticker) if market == "HK" else ticker.strip().upper()
    default_ccy = MARKET_DEFAULT_CURRENCY.get(market, "USD")

    if not allow_network:
        if market != "US" and not explicit_fin_ccy:
            raise ValueError(
                f"Non-US ticker '{normalized_ticker}' (market={market}) requires explicit "
                f"--financial-currency / --reporting-currency (or --allow-network)"
            )
        if normalized_ticker in KNOWN_FOREIGN_OR_ADR_US_TICKERS and not explicit_fin_ccy:
            raise ValueError(
                f"ADR / foreign issuer '{normalized_ticker}' requires explicit "
                f"--financial-currency / --reporting-currency (or --allow-network)"
            )

    yf_info: dict[str, Any] = {}
    if allow_network and (
        listing_currency is None
        or explicit_fin_ccy is None
        or (market_cap_usd_mn is None and (price_local is None or shares is None))
    ):
        try:
            import yfinance as yf  # type: ignore

            t_obj = yf.Ticker(normalized_ticker)
            yf_info = dict(t_obj.info or {})
        except Exception as exc:
            if explicit_fin_ccy is None and (market != "US" or normalized_ticker in KNOWN_FOREIGN_OR_ADR_US_TICKERS):
                raise ValueError(
                    f"Failed to fetch yfinance metadata for '{normalized_ticker}' ({type(exc).__name__}); "
                    f"pass explicit --financial-currency and --fx-rate"
                ) from exc
            yf_info = {}

    yf_fin_ccy = yf_info.get("financialCurrency")
    if explicit_fin_ccy is None and not yf_fin_ccy:
        if market != "US" or normalized_ticker in KNOWN_FOREIGN_OR_ADR_US_TICKERS:
            raise ValueError(
                f"Could not determine reporting currency for '{normalized_ticker}'; "
                f"pass explicit --financial-currency / --reporting-currency"
            )

    resolved_listing_ccy = normalize_currency(
        listing_currency or yf_info.get("currency"), default=default_ccy
    )
    resolved_financial_ccy = normalize_currency(
        explicit_fin_ccy or yf_fin_ccy,
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

    caller_passed_shares = shares is not None
    if price_local is None and yf_info.get("currentPrice") is not None:
        price_local = float(yf_info["currentPrice"])
    if shares is None and yf_info.get("sharesOutstanding") is not None:
        shares = float(yf_info["sharesOutstanding"])

    price_usd: float | None = None
    if price_local is not None:
        price_usd = round(float(price_local) * list_rate, 6)

    shares_mn_out: float | None = None
    if shares is not None:
        raw_shares_mn = _normalize_shares_to_mn(float(shares), shares_unit=shares_unit)
        shares_mn_out = round(raw_shares_mn / float(adr_ratio), 4)

    mcap_source = "explicit --market-cap-usd-mn"
    if market_cap_usd_mn is None:
        yf_mcap_raw = yf_info.get("marketCap")
        if not caller_passed_shares and yf_mcap_raw is not None and float(yf_mcap_raw) > 0:
            market_cap_local_mn = float(yf_mcap_raw) / 1e6
            market_cap_usd_mn = round(market_cap_local_mn * list_rate, 2)
            mcap_source = "yfinance marketCap * listing_fx_rate"
            if price_local is not None and price_local > 0 and shares_mn_out is not None:
                implied_shares_mn = market_cap_local_mn / float(price_local)
                if shares_mn_out / max(implied_shares_mn, 1e-6) > 1.5:
                    shares_mn_out = round(implied_shares_mn, 4)
        elif price_local is not None and shares_mn_out is not None:
            market_cap_local_mn = float(price_local) * shares_mn_out
            market_cap_usd_mn = round(market_cap_local_mn * list_rate, 2)
            mcap_source = "computed (price_local * shares_mn * listing_fx_rate)"
        elif yf_mcap_raw is not None:
            market_cap_local_mn = float(yf_mcap_raw) / 1e6
            market_cap_usd_mn = round(market_cap_local_mn * list_rate, 2)
            mcap_source = "yfinance marketCap * listing_fx_rate"
    else:
        market_cap_usd_mn = round(float(market_cap_usd_mn), 2)

    market_cap_raw_usd = (
        round(market_cap_usd_mn * 1e6, 2) if market_cap_usd_mn is not None else None
    )

    return {
        "ticker": normalized_ticker,
        "company": yf_info.get("longName") or yf_info.get("shortName") or normalized_ticker,
        "market": market,
        "period_end": str(resolved_period_end),
        "as_of": resolved_as_of,
        "listing_currency": resolved_listing_ccy,
        "financial_currency": resolved_financial_ccy,
        "reporting_currency": resolved_financial_ccy,
        "fx": {
            "pair": rep_pair or "USDUSD=X",
            "from": resolved_financial_ccy,
            "to": "USD",
            "from_currency": resolved_financial_ccy,
            "to_currency": "USD",
            "rate": round(rep_rate, 6),
            "as_of": resolved_as_of,
            "source": rep_source,
        },
        "listing_fx": {
            "pair": list_pair or "USDUSD=X",
            "from": resolved_listing_ccy,
            "to": "USD",
            "from_currency": resolved_listing_ccy,
            "to_currency": "USD",
            "rate": round(list_rate, 6),
            "as_of": resolved_as_of,
            "source": list_source,
        },
        "market_cap": {
            "market_cap_usd_mn": market_cap_usd_mn,
            "market_cap_usd": market_cap_usd_mn,
            "value_mn_usd": market_cap_usd_mn,
            "value_usd": market_cap_raw_usd,
            "price_local": round(float(price_local), 4) if price_local is not None else None,
            "price_usd": round(price_usd, 4) if price_usd is not None else None,
            "shares_mn": shares_mn_out,
            "shares": int(round(shares_mn_out * 1e6)) if shares_mn_out is not None else None,
            "shares_unit": shares_unit,
            "adr_ratio": float(adr_ratio),
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
    parser.add_argument("--ticker", required=True, help="Ticker symbol (e.g. 0700.HK, AAPL, BABA)")
    parser.add_argument("--period-end", default=None, help="Financial statement period end (YYYY-MM-DD)")
    parser.add_argument("--market-cap-usd-mn", type=float, default=None, help="Market cap in USD millions")
    parser.add_argument("--price-local", type=float, default=None, help="Share price in listing currency")
    parser.add_argument("--shares", type=float, default=None, help="Diluted shares (raw count or millions)")
    parser.add_argument(
        "--shares-unit",
        choices=("auto", "raw", "thousands", "millions"),
        default="auto",
        help="Unit for --shares (default: auto)",
    )
    parser.add_argument(
        "--adr-ratio",
        type=float,
        default=1.0,
        help="Ordinary shares per ADR when --shares is in ordinary shares and --price-local is per ADR",
    )
    parser.add_argument("--listing-currency", default=None, help="Listing currency (e.g. HKD, USD, JPY)")
    parser.add_argument(
        "--financial-currency",
        "--reporting-currency",
        dest="financial_currency",
        default=None,
        help="Reporting currency of financial statements (e.g. CNY, USD, HKD)",
    )
    parser.add_argument("--fx-rate", type=float, default=None, help="Reporting currency -> USD rate")
    parser.add_argument("--listing-fx-rate", type=float, default=None, help="Listing currency -> USD rate")
    parser.add_argument("--as-of", default=None, help="Valuation anchor date (YYYY-MM-DD)")
    parser.add_argument("--allow-network", action="store_true", help="Allow live yfinance lookup if needed")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    try:
        result = build_anchors(
            args.ticker,
            args.period_end,
            market_cap_usd_mn=args.market_cap_usd_mn,
            price_local=args.price_local,
            shares=args.shares,
            shares_unit=args.shares_unit,
            adr_ratio=args.adr_ratio,
            listing_currency=args.listing_currency,
            financial_currency=args.financial_currency,
            fx_rate=args.fx_rate,
            listing_fx_rate=args.listing_fx_rate,
            as_of=args.as_of,
            allow_network=args.allow_network,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
