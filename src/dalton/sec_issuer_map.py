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

import json
from pathlib import Path
import re
from typing import Any, Literal

SecTickerKind = Literal["direct", "mapped", "confirmed_non_sec", "unmapped_non_us"]

SEC_ISSUER_MAP_DATA_PATH: Path = (
    Path(__file__).resolve().parents[2] / "plugins" / "dalton_sec" / "sec_issuer_map.json"
)


def _load_issuer_map_data() -> dict[str, Any]:
    return json.loads(SEC_ISSUER_MAP_DATA_PATH.read_text(encoding="utf-8"))


_ISSUER_MAP_DATA: dict[str, Any] = _load_issuer_map_data()

NON_SEC_MARKET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(str(pat), re.IGNORECASE), str(label))
    for pat, label in (_ISSUER_MAP_DATA.get("non_sec_market_patterns") or ())
)

_A_SHARE_RE = re.compile(r"^(?:\d{6}\.(?:SH|SZ|SS|BJ)|(?:SH|SZ|BJ)\d{6})$", re.IGNORECASE)
_HK_RE = re.compile(r"^(?:HK:)?0*(\d{1,5})(?:\.HK)?$", re.IGNORECASE)

# Known dual-listed / foreign-private-issuer tickers whose SEC EDGAR filings (20-F / 6-K) live under a US ADR/ticker.
DUAL_LISTED_SEC_ISSUER_MAP: dict[str, str] = {
    str(k).upper(): str(v).upper()
    for k, v in (_ISSUER_MAP_DATA.get("dual_listed_sec_issuer_map") or {}).items()
}

# Tickers explicitly confirmed to have NO SEC registration / CIK (canonical normalized keys).
CONFIRMED_NON_SEC_ISSUERS: frozenset[str] = frozenset(
    str(item).upper() for item in (_ISSUER_MAP_DATA.get("confirmed_non_sec_issuers") or ())
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
