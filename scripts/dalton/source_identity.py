"""Map `researchdata` (and `dalton_sec`) tool call outputs to structured `_declared_sources` / `_sources` records.

Used by `scripts/dalton/researchdata_bridge.py` on the host (Python 3.11+ stdlib only).
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_SOURCES_PER_CALL = 50
MAX_DOC_ID_LEN = 200
MAX_TITLE_LEN = 300
MAX_PUBLISHER_LEN = 120
MAX_URL_LEN = 2000

TIER_BY_IDENTITY: dict[str, int | None] = {
    "filing": 1,
    "company": 2,
    "broker_report": 3,
    "official_data": 4,
    "expert": 5,
    "news": 6,
    "internal_note": None,
    "derived": None,
    "street_estimate": None,
}


def _trunc(val: Any, limit: int) -> str:
    if val is None:
        return ""
    return str(val).strip()[:limit]


def _trunc_opt(val: Any, limit: int) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    return s[:limit] if s else None


def _make_entry(
    *,
    document_id: str,
    route: str,
    identity: str,
    publisher: str,
    title: str,
    as_of: str,
    as_of_basis: str,
    source_type: str = "research_doc",
    url: str | None = None,
    private: bool = True,
    independent: bool = True,
) -> dict[str, Any]:
    doc_id = _trunc(document_id, MAX_DOC_ID_LEN)
    pub = _trunc(publisher, MAX_PUBLISHER_LEN) or route
    ttl = _trunc(title, MAX_TITLE_LEN) or doc_id
    as_of_clean = _trunc(as_of, 64)
    tier = TIER_BY_IDENTITY.get(identity)
    return {
        "document_id": doc_id,
        "identifier": doc_id,
        "route": _trunc(route, 60),
        "source_type": source_type,
        "identity": identity,
        "detail": identity,
        "tier": tier,
        "publisher": pub,
        "provider": _trunc(f"{route}:{pub}", 128),
        "title": ttl,
        "as_of": as_of_clean,
        "as_of_basis": as_of_basis,
        "url": _trunc_opt(url, MAX_URL_LEN),
        "private": bool(private),
        "independent": bool(independent),
    }


def _unwrap_gateway_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Unwrap `data_gateway.py` envelope if present, returning (inner_dict, fallback_as_of)."""
    gateway_at = str(payload.get("gateway_at") or payload.get("retrieved_at") or "")[:10]
    inner = payload.get("data")
    if isinstance(inner, dict) and (
        "ok" in payload
        or "status" in payload
        or "executed" in payload
        or "route" in payload
        or "gateway_at" in payload
    ):
        inner_at = str(inner.get("retrieved_at") or inner.get("fetched_at") or gateway_at)[:10]
        return inner, inner_at
    return payload, gateway_at


def _unwrap_mcp_proxy_payload(inner: dict[str, Any]) -> dict[str, Any]:
    """Unwrap AlphaEngine / Guidepoint MCP proxy response (`structuredContent` or `content[0].text`)."""
    sc = inner.get("structuredContent")
    if isinstance(sc, dict):
        return sc
    content = inner.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and isinstance(first.get("text"), str):
            try:
                parsed = json.loads(first["text"])
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return inner


def _infer_wiki_identity(item: dict[str, Any]) -> tuple[str, str]:
    """Infer `(identity, publisher)` for a `company-wiki` document item."""
    title = str(item.get("title") or "")
    orig_src = str(item.get("original_source") or "")
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    combined = f"{title} {orig_src} {' '.join(str(t) for t in tags)}"

    if "专家访谈" in combined or "专家" in orig_src or "Expert" in combined:
        pub = orig_src if orig_src and orig_src != "company-wiki" else "专家网络访谈"
        return "expert", pub
    if (
        "管理层会议纪要" in combined
        or "管理层交流" in combined
        or "管理层" in combined
        or "电话会" in combined
    ):
        pub = orig_src if orig_src and orig_src != "company-wiki" else "公司/管理层交流"
        return "company", pub
    if "券商研报" in combined or (
        orig_src
        and orig_src not in ("company-wiki", "Flomo")
        and not orig_src.endswith((".jpg", ".png", ".md"))
        and "笔记" not in combined
    ):
        pub = orig_src if orig_src and orig_src != "company-wiki" else "券商研报"
        return "broker_report", pub
    return "internal_note", "Fund Wiki"


def _map_library_item(item: dict[str, Any], default_route: str, fallback_as_of: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    src = str(item.get("source") or default_route).strip()
    doc_id = str(item.get("document_id") or item.get("canonical_id") or "").strip()
    if not doc_id:
        return None

    date_str = str(item.get("published_at") or item.get("date") or fallback_as_of or "")[:19]
    title = str(item.get("title") or doc_id)
    url = item.get("url") if isinstance(item.get("url"), str) else None
    layer = str(item.get("layer") or "raw")

    if src == "company-wiki":
        identity, pub = _infer_wiki_identity(item)
        return _make_entry(
            document_id=doc_id,
            route="company-wiki",
            identity=identity,
            publisher=pub,
            title=title,
            as_of=date_str[:10] if date_str else fallback_as_of,
            as_of_basis="document_date",
            source_type="research_doc",
            url=url,
            private=True,
            independent=True,
        )

    if src == "sales-notes":
        is_digest = layer == "digest" or doc_id.startswith("digest:") or item.get("independent_evidence") is False
        identity = "derived" if is_digest else "broker_report"
        pub = str(item.get("original_source") or "Sales Notes")
        return _make_entry(
            document_id=doc_id,
            route="sales-notes",
            identity=identity,
            publisher=pub,
            title=title,
            as_of=date_str[:10] if date_str else fallback_as_of,
            as_of_basis="document_date",
            source_type="research_doc",
            url=url,
            private=True,
            independent=not is_digest,
        )

    if src == "tmtb":
        # Prefer canonical alphaengine:<id> if present for cross-route dedup
        canon = str(item.get("canonical_id") or item.get("original_document_id") or "")
        effective_id = canon if canon.startswith("alphaengine:") else doc_id
        return _make_entry(
            document_id=effective_id,
            route="tmtb",
            identity="broker_report",
            publisher="TMT Breakout",
            title=title,
            as_of=date_str[:10] if date_str else fallback_as_of,
            as_of_basis="document_date",
            source_type="research_doc",
            url=url,
            private=True,
            independent=bool(item.get("independent_evidence", True)),
        )

    if src == "funda-ai":
        return _make_entry(
            document_id=doc_id,
            route="funda-ai",
            identity="broker_report",
            publisher="FUNDA AI",
            title=title,
            as_of=date_str,
            as_of_basis="publish_time",
            source_type="research_doc",
            url=url,
            private=True,
            independent=bool(item.get("independent_evidence", True)),
        )

    return None


def _declare_research_library(route: str, inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    doc = inner.get("document")
    if isinstance(doc, dict):
        mapped = _map_library_item(doc, route, fallback_as_of)
        if mapped:
            out.append(mapped)
    results = inner.get("results")
    if isinstance(results, list):
        for item in results:
            mapped = _map_library_item(item, route, fallback_as_of)
            if mapped:
                out.append(mapped)
            if len(out) >= MAX_SOURCES_PER_CALL:
                break
    return out[:MAX_SOURCES_PER_CALL]


def _declare_alphaengine(inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    data = _unwrap_mcp_proxy_payload(inner)
    out: list[dict[str, Any]] = []

    def _from_item(item: dict[str, Any]) -> dict[str, Any] | None:
        raw_id = str(item.get("doc_id") or item.get("id") or "").strip()
        if not raw_id:
            return None
        doc_id = raw_id if raw_id.startswith("alphaengine:") else f"alphaengine:{raw_id}"
        type_id = str(item.get("type_id") or "")
        identity = "company" if type_id == "IND_SECOND_5FA442AE" else "broker_report"
        sources = item.get("sources")
        if isinstance(sources, list) and sources and sources[0]:
            pub = str(sources[0])
        elif item.get("publisher"):
            pub = str(item["publisher"])
        else:
            pub = "AlphaEngine"
        pub_time = str(item.get("publish_time") or item.get("date") or fallback_as_of)[:19]
        title = str(item.get("title") or doc_id)
        return _make_entry(
            document_id=doc_id,
            route="alphaengine",
            identity=identity,
            publisher=pub,
            title=title,
            as_of=pub_time[:10] if pub_time else fallback_as_of,
            as_of_basis="publish_time",
            source_type="research_doc",
            url=None,
            private=True,
            independent=True,
        )

    meta = data.get("metadata")
    if isinstance(meta, dict):
        entry = _from_item(meta)
        if entry:
            out.append(entry)
    elif data.get("doc_id"):
        entry = _from_item(data)
        if entry:
            out.append(entry)

    results = data.get("results")
    if isinstance(results, list):
        for item in results:
            if isinstance(item, dict):
                entry = _from_item(item)
                if entry:
                    out.append(entry)
            if len(out) >= MAX_SOURCES_PER_CALL:
                break

    return out[:MAX_SOURCES_PER_CALL]


_GP_ID_RE = re.compile(r"library_ts:([A-Za-z0-9-]+)")


def _declare_guidepoint(inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    data = _unwrap_mcp_proxy_payload(inner)
    rows = data.get("data") if isinstance(data.get("data"), list) else data.get("results")
    if not isinstance(rows, list):
        return []

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or item.get("transcript_id") or "").strip()
        m = _GP_ID_RE.search(raw_id)
        base_id = m.group(1) if m else raw_id
        if not base_id:
            continue
        doc_id = base_id if base_id.startswith("guidepoint:") else f"guidepoint:{base_id}"
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)

        # Compliance rule: NEVER include expert full_name in publisher or title!
        respondent = item.get("respondent") if isinstance(item.get("respondent"), dict) else {}
        role_title = str(respondent.get("title") or "").strip()
        role_company = str(respondent.get("company") or "").strip()
        role_parts = [p for p in (role_title, role_company) if p]
        role_desc = ", ".join(role_parts)
        pub = f"Guidepoint ({role_desc})" if role_desc else "Guidepoint"

        title = str(item.get("transcript_name") or item.get("title") or doc_id)
        call_date = str(item.get("date") or fallback_as_of)[:10]
        ref_url = item.get("reference_url") if isinstance(item.get("reference_url"), str) else None

        out.append(
            _make_entry(
                document_id=doc_id,
                route="guidepoint",
                identity="expert",
                publisher=pub,
                title=title,
                as_of=call_date,
                as_of_basis="call_date",
                source_type="research_doc",
                url=ref_url,
                private=True,
                independent=True,
            )
        )
        if len(out) >= MAX_SOURCES_PER_CALL:
            break
    return out


def _declare_transcript(inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    rows = inner.get("transcripts")
    if not isinstance(rows, list):
        return []
    is_fallback_roic = bool(inner.get("fallback")) or inner.get("source") == "roic"
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("doc_id") or item.get("id") or "").strip()
        if not raw_id:
            continue
        prefix = "roic" if is_fallback_roic else "alphaengine"
        doc_id = raw_id if ":" in raw_id else f"{prefix}:{raw_id}"
        kind = str(item.get("kind") or "company_call")
        identity = "broker_report" if kind == "broker_callback" else "company"
        pub = "roic.ai" if is_fallback_roic else "AlphaEngine"
        title = str(item.get("title") or doc_id)
        pub_time = str(item.get("publish_time") or item.get("date") or fallback_as_of)[:10]
        out.append(
            _make_entry(
                document_id=doc_id,
                route="transcript",
                identity=identity,
                publisher=pub,
                title=title,
                as_of=pub_time,
                as_of_basis="call_date",
                source_type="research_doc",
                url=None,
                private=True,
                independent=True,
            )
        )
        if len(out) >= MAX_SOURCES_PER_CALL:
            break
    return out


def _declare_sec(arguments: dict[str, Any], inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    ticker = str(inner.get("ticker") or arguments.get("ticker") or "").upper().strip() or "UNKNOWN"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_filing(form: str, filing_date: str, accession_no: str, report_date: str = "", extra_title: str = "") -> None:
        key = accession_no or f"{form}:{filing_date}"
        if not key or key in seen:
            return
        seen.add(key)
        doc_id = f"sec:{ticker}:{key}"
        title_parts = [ticker, form or "Filing"]
        if report_date:
            title_parts.append(f"(period {report_date})")
        if extra_title:
            title_parts.append(extra_title)
        title = " ".join(p for p in title_parts if p)
        out.append(
            _make_entry(
                document_id=doc_id,
                route="sec",
                identity="filing",
                publisher="SEC EDGAR",
                title=title,
                as_of=filing_date or fallback_as_of,
                as_of_basis="filed",
                source_type="sec_filing",
                url=None,
                private=False,
                independent=True,
            )
        )

    rows = inner.get("rows")
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            acc = str(r.get("accession_no") or r.get("accession") or "").strip()
            fdate = str(r.get("filing_date") or r.get("filed") or "").strip()[:10]
            rdate = str(r.get("report_date") or r.get("period_end") or "").strip()[:10]
            form = str(r.get("form") or r.get("form_type") or arguments.get("form") or "").strip()
            if acc or fdate:
                _add_filing(form, fdate, acc, rdate)
            if len(out) >= MAX_SOURCES_PER_CALL:
                break

    filings = inner.get("filings")
    if isinstance(filings, list):
        for f in filings:
            if not isinstance(f, dict):
                continue
            acc = str(f.get("accession_no") or f.get("accession") or "").strip()
            fdate = str(f.get("filing_date") or "").strip()[:10]
            form = str(f.get("form") or arguments.get("form") or "").strip()
            if acc or fdate:
                _add_filing(form, fdate, acc)
            if len(out) >= MAX_SOURCES_PER_CALL:
                break

    if not out and (inner.get("filing_date") or inner.get("accession_no")):
        acc = str(inner.get("accession_no") or "").strip()
        fdate = str(inner.get("filing_date") or "").strip()[:10]
        form = str(inner.get("form") or arguments.get("form") or "").strip()
        item_label = f"Item {inner['item']}" if inner.get("item") else ""
        _add_filing(form, fdate, acc, extra_title=item_label)

    return out[:MAX_SOURCES_PER_CALL]


def _declare_estimates(arguments: dict[str, Any], inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    if inner.get("ok") is False:
        return []
    ticker = str(inner.get("ticker") or arguments.get("ticker") or "").upper().strip()
    if not ticker:
        return []
    fetched_at = str(inner.get("fetched_at") or fallback_as_of)[:19]
    as_of_date = fetched_at[:10] if fetched_at else fallback_as_of
    doc_id = f"estimates:{ticker}:{as_of_date}" if as_of_date else f"estimates:{ticker}"
    return [
        _make_entry(
            document_id=doc_id,
            route="estimates",
            identity="street_estimate",
            publisher="Yahoo Finance (yfinance)",
            title=f"{ticker} Consensus Estimates Snapshot",
            as_of=as_of_date,
            as_of_basis="fetched_at",
            source_type="data_api",
            url=None,
            private=False,
            independent=True,
        )
    ]


def _declare_macro(route: str, arguments: dict[str, Any], inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    if inner.get("ok") is False and not inner.get("rows"):
        return []
    rows = inner.get("rows") if isinstance(inner.get("rows"), list) else []
    latest_obs = ""
    for r in reversed(rows):
        if isinstance(r, dict) and r.get("observation_date"):
            latest_obs = str(r["observation_date"])[:10]
            break
    if not latest_obs and rows and isinstance(rows[0], dict):
        latest_obs = str(rows[0].get("observation_date") or rows[0].get("record_date") or "")[:10]
    as_of = latest_obs or str(inner.get("vintage") or fallback_as_of)[:10]

    if route == "fred":
        series = str(inner.get("series") or arguments.get("series") or arguments.get("series_id") or "SERIES").upper()
        vintage = str(inner.get("vintage") or arguments.get("vintage") or as_of)
        doc_id = f"fred:{series}:{vintage}" if vintage else f"fred:{series}"
        url = inner.get("series_url") or inner.get("source_url")
        return [
            _make_entry(
                document_id=doc_id,
                route="fred",
                identity="official_data",
                publisher="FRED",
                title=f"FRED {series}",
                as_of=as_of,
                as_of_basis="observation_date",
                source_type="data_api",
                url=url if isinstance(url, str) else None,
                private=False,
                independent=True,
            )
        ]

    dataset = str(inner.get("dataset") or arguments.get("dataset") or "treasury")
    doc_id = f"treasury:{dataset}:{as_of}" if as_of else f"treasury:{dataset}"
    url = inner.get("source_url")
    return [
        _make_entry(
            document_id=doc_id,
            route="treasury",
            identity="official_data",
            publisher="US Treasury Fiscal Data",
            title=f"US Treasury {dataset}",
            as_of=as_of,
            as_of_basis="observation_date",
            source_type="data_api",
            url=url if isinstance(url, str) else None,
            private=False,
            independent=True,
        )
    ]


def _declare_cnhk(arguments: dict[str, Any], inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    if inner.get("ok") is False:
        return []
    ticker = str(inner.get("ticker") or arguments.get("ticker") or "").upper().strip() or "CNHK"
    rows = inner.get("rows")
    out: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for idx, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            fdate = str(r.get("date") or fallback_as_of)[:10]
            title = str(r.get("title") or f"{ticker} Filing")
            pdf_link = r.get("pdf_link") if isinstance(r.get("pdf_link"), str) else None
            r_idx = r.get("index", idx + 1)
            doc_id = f"cn-hk:{ticker}:{fdate}:{r_idx}"
            out.append(
                _make_entry(
                    document_id=doc_id,
                    route="cn-hk",
                    identity="filing",
                    publisher=str(r.get("source") or "CNINFO / HKEX"),
                    title=title,
                    as_of=fdate,
                    as_of_basis="filed",
                    source_type="sec_filing",
                    url=pdf_link,
                    private=False,
                    independent=True,
                )
            )
            if len(out) >= MAX_SOURCES_PER_CALL:
                break
    return out[:MAX_SOURCES_PER_CALL]


def declare(tool: str, arguments: dict[str, Any] | None, payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return up to 50 structured source records for a `researchdata` tool call result."""
    if not isinstance(payload, dict):
        return []
    args = arguments if isinstance(arguments, dict) else {}
    inner, fallback_as_of = _unwrap_gateway_payload(payload)
    if not isinstance(inner, dict):
        return []

    # Check if top-level or inner status is an unexecuted error
    if payload.get("ok") is False and not inner.get("results") and not inner.get("rows"):
        return []

    route = str(args.get("route") or "").strip()
    if not route and isinstance(payload.get("route"), dict):
        route = str(payload["route"].get("id") or "").strip()
    if not route and isinstance(payload.get("request"), dict):
        route = str(payload["request"].get("route") or "").strip()

    if tool == "catalog" or not route:
        return []

    if route in ("company-wiki", "sales-notes", "tmtb", "funda-ai", "all"):
        return _declare_research_library(route, inner, fallback_as_of)
    if route == "alphaengine":
        return _declare_alphaengine(inner, fallback_as_of)
    if route == "guidepoint":
        return _declare_guidepoint(inner, fallback_as_of)
    if route == "transcript":
        return _declare_transcript(inner, fallback_as_of)
    if route == "sec":
        return _declare_sec(args, inner, fallback_as_of)
    if route == "estimates":
        return _declare_estimates(args, inner, fallback_as_of)
    if route in ("fred", "treasury"):
        return _declare_macro(route, args, inner, fallback_as_of)
    if route == "cn-hk":
        return _declare_cnhk(args, inner, fallback_as_of)

    return []
