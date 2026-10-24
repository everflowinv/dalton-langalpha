"""SEC probe: zero-token check for new SEC EDGAR filings via submissions JSON (PK.16)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

from src.dalton.autonomy.probes import ProbeSignal
from src.dalton.coverage import get_user_coverage

logger = logging.getLogger(__name__)

WATCHED_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "13D", "13G", "4", "SC 13D", "SC 13G"})
P0_FORMS = frozenset({"10-K", "10-Q", "20-F", "6-K", "8-K"})

BUILTIN_TICKER_CIKS: dict[str, str] = {
    "AAPL": "0000320193",
    "AMD": "0000002488",
    "AMZN": "0001018724",
    "AVGO": "0001730168",
    "BABA": "0001577552",
    "BRK-B": "0001067983",
    "COST": "0000909832",
    "CRM": "0001108524",
    "GOOG": "0001652044",
    "GOOGL": "0001652044",
    "JPM": "0000019617",
    "LLY": "0000059478",
    "META": "0001326801",
    "MSFT": "0000789019",
    "NFLX": "0001065280",
    "NVDA": "0001045810",
    "ORCL": "0001341439",
    "PDD": "0001737806",
    "TSLA": "0001318605",
    "TSM": "0001046179",
    "UNH": "0000731766",
    "V": "0001403161",
    "WMT": "0000104169",
}

_SEC_TICKERS_CACHE: dict[str, str] = {}


def _normalize_cik(val: Any) -> Optional[str]:
    if val is None:
        return None
    raw = str(val).strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    return digits.lstrip("0").zfill(10)


async def _lookup_local_cik(
    ticker: str,
    *,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[str]:
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    if sym in BUILTIN_TICKER_CIKS:
        return BUILTIN_TICKER_CIKS[sym]
    eff_user_id = str(user_id or "").strip() or None
    if not eff_user_id and workspace_id:
        try:
            from src.server.database import workspace as ws_db

            ws = await ws_db.get_workspace(str(workspace_id))
            if isinstance(ws, dict) and ws.get("user_id"):
                eff_user_id = str(ws["user_id"]).strip() or None
        except Exception:
            eff_user_id = None
    if not eff_user_id:
        return None
    try:
        data = await get_user_coverage(eff_user_id)
        for entry in data.get("companies") or data.get("coverage") or []:
            if isinstance(entry, dict) and str(entry.get("ticker") or "").upper() == sym:
                resolved = _normalize_cik(entry.get("cik"))
                if resolved:
                    return resolved
    except Exception:
        pass
    return None


async def resolve_company_cik(
    ticker: str,
    *,
    watch_state: Optional[dict[str, Any]] = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    allow_network: bool = True,
) -> Optional[str]:
    """Resolve 10-digit SEC CIK for ``ticker`` via watch_state -> company doc -> builtin/user coverage -> SEC JSON."""
    state = watch_state or {}
    from_state = _normalize_cik(state.get("cik"))
    if from_state:
        return from_state

    if workspace_id:
        try:
            from src.dalton.database.knowledge import latest_valid_version

            row = await latest_valid_version(str(workspace_id), "company")
            content = (row or {}).get("content") if isinstance(row, dict) else None
            if isinstance(content, dict):
                doc_cik = _normalize_cik(
                    content.get("cik") or (content.get("identifiers") or {}).get("cik")
                )
                if doc_cik:
                    return doc_cik
        except Exception as e:
            logger.debug("[DALTON_SEC_PROBE] company doc CIK lookup skipped: %s", e)

    local_cik = await _lookup_local_cik(ticker, user_id=user_id, workspace_id=workspace_id)
    if local_cik:
        return local_cik

    sym = (ticker or "").strip().upper()
    if sym in _SEC_TICKERS_CACHE:
        return _SEC_TICKERS_CACHE[sym]

    if allow_network and sym:
        try:
            ua = os.environ.get("DALTON_SEC_USER_AGENT", "DaltonResearch research@dalton.local")
            url = "https://www.sec.gov/files/company_tickers.json"
            async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": ua}) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
            if isinstance(payload, dict):
                for item in payload.values():
                    if isinstance(item, dict):
                        t_val = str(item.get("ticker") or "").strip().upper()
                        c_val = _normalize_cik(item.get("cik_str"))
                        if t_val and c_val:
                            _SEC_TICKERS_CACHE[t_val] = c_val
                if sym in _SEC_TICKERS_CACHE:
                    return _SEC_TICKERS_CACHE[sym]
        except Exception as e:
            logger.warning("[DALTON_SEC_PROBE] SEC company_tickers.json lookup failed for %s: %s", sym, e)

    return None


def _parse_items(raw_item: Any) -> list[str]:
    """Normalize SEC 8-K items representation (e.g. '2.02,9.01' or ['2.02', '9.01']) into list[str]."""
    if not raw_item:
        return []
    if isinstance(raw_item, (list, tuple)):
        out: list[str] = []
        for elem in raw_item:
            out.extend(_parse_items(elem))
        return out
    parts = [p.strip() for p in str(raw_item).replace(";", ",").split(",")]
    return [p for p in parts if p]


async def probe_sec(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    submissions_override: Optional[dict[str, Any]] = None,
    allow_network: bool = True,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Check SEC EDGAR submissions feed for filings newer than `last_sec_accession`."""
    state = dict(watch_state or {})
    data = submissions_override or state.get("sec_submissions_mock")

    cik = await resolve_company_cik(
        ticker,
        watch_state=state,
        workspace_id=workspace_id if allow_network else None,
        user_id=user_id,
        allow_network=(data is None and allow_network),
    )
    if cik:
        if not state.get("cik"):
            state["cik"] = cik
        state["sec_probe_degraded"] = False
    elif data is None:
        state["sec_probe_degraded"] = True
        logger.warning(
            "[DALTON_SEC_PROBE] CIK resolution failed across all fallbacks for ticker=%s; "
            "marking sec_probe_degraded=True",
            ticker,
        )
        return [], state

    if data is None and cik and allow_network:
        cik10 = str(cik).lstrip("0").zfill(10)
        ua = os.environ.get("DALTON_SEC_USER_AGENT", "DaltonResearch research@dalton.local")
        url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": ua}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

    if not isinstance(data, dict):
        return [], state

    recent = ((data.get("filings") or {}).get("recent")) or {}
    accessions: list[str] = recent.get("accessionNumber") or []
    forms: list[str] = recent.get("form") or []
    filing_dates: list[str] = recent.get("filingDate") or []
    primary_docs: list[str] = recent.get("primaryDocument") or []
    items_col: list[Any] = recent.get("items") or []
    descriptions: list[str] = recent.get("primaryDocDescription") or []

    last_seen = state.get("last_sec_accession")
    raw_seen_list = state.get("seen_accessions")
    has_seen_set = isinstance(raw_seen_list, list)
    seen_set: set[str] = {str(x) for x in raw_seen_list if x} if has_seen_set else set()

    signals: list[ProbeSignal] = []
    newest_accession: Optional[str] = None

    for idx, acc in enumerate(accessions[:100]):
        form = forms[idx] if idx < len(forms) else ""
        f_date = filing_dates[idx] if idx < len(filing_dates) else ""
        doc_name = primary_docs[idx] if idx < len(primary_docs) else ""
        raw_items = items_col[idx] if idx < len(items_col) else None
        desc = descriptions[idx] if idx < len(descriptions) else ""
        parsed_items = _parse_items(raw_items)

        if newest_accession is None:
            newest_accession = acc

        if has_seen_set:
            if acc in seen_set:
                continue
        else:
            if last_seen and acc == last_seen:
                break
            # If last_seen was None (first bootstrap), record watermark without flooding old filings
            if last_seen is None:
                break

        if form not in WATCHED_FORMS:
            continue

        prio = "P0" if form in P0_FORMS else "P1"
        item_suffix = f" items={','.join(parsed_items)}" if parsed_items else ""
        signals.append(
            ProbeSignal(
                probe="sec",
                signal_key=f"filing:{acc}",
                priority=prio,
                summary=f"{ticker} 新 SEC 披露：{form}（{f_date}，accession={acc}{item_suffix}）",
                payload={
                    "form": form,
                    "accession_number": acc,
                    "filing_date": f_date,
                    "primary_document": doc_name,
                    "items": parsed_items,
                    "description": desc,
                },
            )
        )

    if newest_accession:
        state["last_sec_accession"] = newest_accession
    if accessions:
        merged_seen = list(dict.fromkeys([*accessions[:100], *(raw_seen_list if has_seen_set else [])]))
        state["seen_accessions"] = merged_seen[:120]

    return signals, state
