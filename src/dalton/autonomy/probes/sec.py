"""SEC probe: zero-token check for new SEC EDGAR filings via submissions JSON (PK.16)."""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from src.dalton.autonomy.probes import ProbeSignal

WATCHED_FORMS = frozenset({"10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "13D", "13G", "4"})
P0_FORMS = frozenset({"10-K", "10-Q", "20-F", "6-K", "8-K"})


async def probe_sec(
    *,
    ticker: str,
    watch_state: dict[str, Any],
    submissions_override: Optional[dict[str, Any]] = None,
) -> tuple[list[ProbeSignal], dict[str, Any]]:
    """Check SEC EDGAR submissions feed for filings newer than `last_sec_accession`."""
    state = dict(watch_state or {})
    cik = state.get("cik")
    data = submissions_override or state.get("sec_submissions_mock")

    if data is None and cik:
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

    last_seen = state.get("last_sec_accession")
    signals: list[ProbeSignal] = []
    newest_accession: Optional[str] = None

    for idx, acc in enumerate(accessions[:15]):
        form = forms[idx] if idx < len(forms) else ""
        f_date = filing_dates[idx] if idx < len(filing_dates) else ""
        doc_name = primary_docs[idx] if idx < len(primary_docs) else ""
        if newest_accession is None:
            newest_accession = acc
        if last_seen and acc == last_seen:
            break
        if form not in WATCHED_FORMS:
            continue
        # If last_seen was None (first bootstrap), record watermark without flooding old filings
        if last_seen is None:
            break
        prio = "P0" if form in P0_FORMS else "P1"
        signals.append(
            ProbeSignal(
                probe="sec",
                signal_key=f"filing:{acc}",
                priority=prio,
                summary=f"{ticker} 新 SEC 披露：{form}（{f_date}，accession={acc}）",
                payload={
                    "form": form,
                    "accession_number": acc,
                    "filing_date": f_date,
                    "primary_document": doc_name,
                },
            )
        )

    if newest_accession:
        state["last_sec_accession"] = newest_accession

    return signals, state
