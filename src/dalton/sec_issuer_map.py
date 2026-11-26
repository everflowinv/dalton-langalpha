"""Shared cross-market SEC issuer alias mapping and confirmed non-SEC classification.

Used consistently by:
- ``plugins/dalton_sec/dalton_sec_mcp_server.py``
- ``scripts/dalton/researchdata_bridge.py``
- ``scripts/dalton/research_runtime/skills/findata-analyst/scripts/findata_cli.py``

Distinguishes three cases for non-US tickers:
1. ``mapped``: known dual-listed / ADR issuer with a US SEC ticker (e.g. ``9988.HK`` -> ``BABA``, ``7203.T`` -> ``TM``).
2. ``confirmed_non_sec``: explicitly confirmed to have no SEC registration / CIK (e.g. ``0700.HK``, ``005930.KS``, mainland A-shares).
3. ``unmapped_non_us``: non-US exchange ticker absent from the local issuer map — MUST NOT be mislabeled as ``sec_filer=False``.
"""

from __future__ import annotations

import re
from typing import Literal

SecTickerKind = Literal["direct", "mapped", "confirmed_non_sec", "unmapped_non_us"]

NON_SEC_MARKET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:\d{1,5}\.HK|HK:\d{1,5}|0\d{3,4})$", re.IGNORECASE), "港股"),
    (re.compile(r"^(?:\d{6}\.(?:KS|KQ)|(?:KR|KS|KQ):\d{6})$", re.IGNORECASE), "韩股"),
    (re.compile(r"^(?:\d{6}\.(?:SH|SZ|SS|BJ)|(?:SH|SZ|BJ)\d{6})$", re.IGNORECASE), "A股"),
    (
        re.compile(
            r"^[A-Z0-9._-]+\.(?:T|JP|TW|TWO|L|PA|DE|SW|AS|MI|TO|AX|SI|NS|BO|CO|HE|ST|MC)$",
            re.IGNORECASE,
        ),
        "非美股交易所标的",
    ),
)

_A_SHARE_RE = re.compile(r"^(?:\d{6}\.(?:SH|SZ|SS|BJ)|(?:SH|SZ|BJ)\d{6})$", re.IGNORECASE)
_HK_RE = re.compile(r"^(?:HK:)?0*(\d{1,5})(?:\.HK)?$", re.IGNORECASE)

# Known dual-listed / foreign-private-issuer tickers whose SEC EDGAR filings (20-F / 6-K) live under a US ADR/ticker.
DUAL_LISTED_SEC_ISSUER_MAP: dict[str, str] = {
    # Hong Kong dual-listed ADRs
    "9988.HK": "BABA",
    "09988": "BABA",
    "HK:9988": "BABA",
    "HK:09988": "BABA",
    "9618.HK": "JD",
    "09618": "JD",
    "HK:9618": "JD",
    "HK:09618": "JD",
    "9888.HK": "BIDU",
    "09888": "BIDU",
    "HK:9888": "BIDU",
    "HK:09888": "BIDU",
    "9999.HK": "NTES",
    "09999": "NTES",
    "HK:9999": "NTES",
    "9626.HK": "BILI",
    "09626": "BILI",
    "HK:9626": "BILI",
    "2015.HK": "LI",
    "02015": "LI",
    "HK:2015": "LI",
    "9866.HK": "NIO",
    "09866": "NIO",
    "HK:9866": "NIO",
    "9868.HK": "XPEV",
    "09868": "XPEV",
    "HK:9868": "XPEV",
    "9961.HK": "TCOM",
    "09961": "TCOM",
    "HK:9961": "TCOM",
    "9987.HK": "YUMC",
    "09987": "YUMC",
    "2057.HK": "ZTO",
    "02057": "ZTO",
    "0005.HK": "HSBC",
    "00005": "HSBC",
    "9901.HK": "EDU",
    "09901": "EDU",
    "1179.HK": "HTHT",
    "01179": "HTHT",
    "9898.HK": "WB",
    "09898": "WB",
    "9698.HK": "GDS",
    "09698": "GDS",
    "2076.HK": "BZ",
    "02076": "BZ",
    "9990.HK": "ZH",
    "09990": "ZH",
    "9878.HK": "HKD",
    # Taiwan ADRs
    "2330.TW": "TSM",
    "2303.TW": "UMC",
    "3711.TW": "ASX",
    "2412.TW": "CHT",
    # Japan ADRs (20-F filers)
    "7203.T": "TM",
    "6758.T": "SONY",
    "7267.T": "HMC",
    "8306.T": "MUFG",
    "8316.T": "SMFG",
    "8411.T": "MFG",
    "8604.T": "NMR",
    "8591.T": "IX",
    "4502.T": "TAK",
    # Korea ADRs (20-F filers)
    "005490.KS": "PKX",
    "015760.KS": "KEP",
    "017670.KS": "SKM",
    "030200.KS": "KT",
    "055550.KS": "SHG",
    "105560.KS": "KB",
    "316140.KS": "WF",
    "034220.KS": "LPL",
    # Europe / UK ADRs (20-F filers)
    "ASML.AS": "ASML",
    "SAP.DE": "SAP",
    "AZN.L": "AZN",
    "SHEL.L": "SHEL",
    "ULVR.L": "UL",
    "BP.L": "BP",
    "GSK.L": "GSK",
    "REL.L": "RELX",
    "RIO.L": "RIO",
    "BHP.AX": "BHP",
    "NOVO-B.CO": "NVO",
    "NOKIA.HE": "NOK",
    "ERIC-B.ST": "ERIC",
    "SAN.PA": "SNY",
    "SAN.MC": "SAN",
    "TTE.PA": "TTE",
}

# Tickers explicitly confirmed to have NO SEC registration / CIK (canonical normalized keys).
CONFIRMED_NON_SEC_ISSUERS: frozenset[str] = frozenset(
    {
        # Hong Kong primary listings without US SEC registration
        "0700.HK",
        "00700",
        "3690.HK",
        "03690",
        "1810.HK",
        "01810",
        "9992.HK",
        "09992",
        "0788.HK",
        "00788",
        "1211.HK",
        "01211",
        "0981.HK",
        "00981",
        "1024.HK",
        "01024",
        "2318.HK",
        "02318",
        "0388.HK",
        "00388",
        "1299.HK",
        "01299",
        "0941.HK",
        "00941",
        "0883.HK",
        "00883",
        "9633.HK",
        "09633",
        "2020.HK",
        "02020",
        "2382.HK",
        "02382",
        "0268.HK",
        "00268",
        "6690.HK",
        "06690",
        # Korea primary listings without US SEC registration
        "005930.KS",
        "000660.KS",
        "035420.KS",
        "035420.KQ",
        "035720.KS",
        "035720.KQ",
        "373220.KS",
        "207940.KS",
        "005380.KS",
        "000270.KS",
        "068270.KS",
    }
)


def _hk_candidates(raw: str) -> tuple[str, ...]:
    m_hk = _HK_RE.fullmatch(raw)
    if not m_hk:
        return ()
    if not (
        raw.endswith(".HK")
        or raw.startswith("HK:")
        or (raw.isdigit() and len(raw) == 5 and raw.startswith("0"))
    ):
        return ()
    num = int(m_hk.group(1))
    return (f"{num:04d}.HK", f"{num}.HK", f"{num:05d}", f"HK:{num:04d}", f"HK:{num:05d}")


def detect_non_sec_market(ticker: str | None) -> str | None:
    """Return a non-US market label when ``ticker`` matches a non-US exchange format."""
    raw = str(ticker or "").strip()
    if not raw:
        return None
    for pat, label in NON_SEC_MARKET_PATTERNS:
        if pat.fullmatch(raw):
            return label
    return None


def _lookup_core_watchlist_alias(raw: str) -> str | None:
    """Consult ``scripts/dalton/alphaengine_search_cache._CORE_WATCHLIST_ALIASES`` when importable."""
    alias_map = None
    for mod_name in ("scripts.dalton.alphaengine_search_cache", "alphaengine_search_cache"):
        try:
            mod = __import__(mod_name, fromlist=["_CORE_WATCHLIST_ALIASES"])
            alias_map = getattr(mod, "_CORE_WATCHLIST_ALIASES", None)
            if isinstance(alias_map, dict):
                break
        except Exception:
            continue
    if not isinstance(alias_map, dict):
        return None

    cands = {raw, *_hk_candidates(raw)}
    for canon, aliases in alias_map.items():
        canon_str = str(canon or "").strip().upper()
        if not canon_str or detect_non_sec_market(canon_str) is not None:
            continue
        if any(str(a or "").strip().upper() in cands for a in (aliases or ())):
            return canon_str
    return None


def resolve_sec_issuer_ticker(ticker: str | None) -> str | None:
    """Resolve a known dual-listed / ADR ticker (e.g. ``9988.HK`` or ``7203.T``) to its SEC ticker."""
    raw = str(ticker or "").strip().upper()
    if not raw:
        return None
    if raw in DUAL_LISTED_SEC_ISSUER_MAP:
        return DUAL_LISTED_SEC_ISSUER_MAP[raw]
    for cand in _hk_candidates(raw):
        if cand in DUAL_LISTED_SEC_ISSUER_MAP:
            return DUAL_LISTED_SEC_ISSUER_MAP[cand]
    return _lookup_core_watchlist_alias(raw)


def is_confirmed_non_sec_ticker(ticker: str | None) -> bool:
    """Return True only when ``ticker`` is confirmed to have no SEC registration / CIK."""
    raw = str(ticker or "").strip().upper()
    if not raw:
        return False
    if _A_SHARE_RE.fullmatch(raw):
        return True
    if raw in CONFIRMED_NON_SEC_ISSUERS:
        return True
    for cand in _hk_candidates(raw):
        if cand in CONFIRMED_NON_SEC_ISSUERS:
            return True
    if raw.startswith(("KR:", "KS:", "KQ:")):
        code = raw.split(":", 1)[1].strip()
        if f"{code}.KS" in CONFIRMED_NON_SEC_ISSUERS or f"{code}.KQ" in CONFIRMED_NON_SEC_ISSUERS:
            return True
    return False


def classify_sec_ticker(ticker: str | None) -> tuple[SecTickerKind, str | None, str | None]:
    """Classify ``ticker`` for SEC EDGAR queries.

    Returns ``(kind, effective_sec_ticker, non_us_market_label)``:
    - ``("mapped", sec_ticker, market_label)``: resolved to a US SEC filer ticker.
    - ``("confirmed_non_sec", None, market_label)``: confirmed no SEC CIK; do not query SEC.
    - ``("unmapped_non_us", None, market_label)``: non-US ticker absent from issuer map; do NOT claim ``sec_filer=False``.
    - ``("direct", ticker, None)``: standard US ticker or CIK; query SEC directly.
    """
    raw = str(ticker or "").strip()
    if not raw:
        return ("direct", raw, None)

    mapped = resolve_sec_issuer_ticker(raw)
    market_label = detect_non_sec_market(raw)
    if mapped is not None:
        return ("mapped", mapped, market_label)
    if market_label is not None:
        if is_confirmed_non_sec_ticker(raw):
            return ("confirmed_non_sec", None, market_label)
        return ("unmapped_non_us", None, market_label)
    return ("direct", raw, None)
