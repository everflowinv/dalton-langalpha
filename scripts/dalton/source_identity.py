"""Map `researchdata` (and `dalton_sec`) tool call outputs to structured `_declared_sources` / `_sources` records.

Used by `scripts/dalton/researchdata_bridge.py` on the host (Python 3.11+ stdlib only).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

_BEIJING_TZ = timezone(timedelta(hours=8))

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
    "unknown": None,
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
    if src == "sales-mail" or not src:
        lower_id = doc_id.lower()
        if lower_id.startswith("tmtb:"):
            src = "tmtb"
        elif lower_id.startswith("wiki:"):
            src = "company-wiki"
        elif lower_id.startswith("funda:"):
            src = "funda-ai"
        else:
            src = "sales-notes"

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


_ALPHAENGINE_DOC_SOURCE_OPS: frozenset[str] = frozenset(
    {
        "",
        "search_library",
        "get_document",
        "search",
        "query",
        "read",
        "read_document",
        "get",
    }
)
_ALPHAENGINE_NON_DOC_OPS: frozenset[str] = frozenset(
    {
        "list_recording_tasks",
        "get_recording_status",
        "get_recording_task",
        "get_document_info",
        "list_attachments",
        "list_filters",
        "auth_status",
    }
)


def _infer_alphaengine_identity(item: dict[str, Any]) -> str:
    doc_code = str(item.get("document_code") or item.get("code") or "").strip()
    type_id = str(item.get("type_id") or item.get("document_type_id") or "").strip()
    type_full_id = str(item.get("type_full_id") or "").strip()
    type_name = str(item.get("type_name") or item.get("document_type_name") or "").strip()
    type_show_name = str(item.get("type_show_name") or "").strip()
    type_full_name = str(item.get("type_full_name") or "").strip()
    first_type_name = str(item.get("first_type_name") or "").strip()
    type_blob = f"{type_name} {type_show_name} {type_full_name} {first_type_name}".strip()

    if (
        type_id == "IND_SECOND_5FA442AE"
        or "IND_SECOND_5FA442AE" in type_full_id.split(":")
        or doc_code in ("SimultaneousInterpretation", "companyAllAnnouncement", "announcement")
        or any(
            kw in type_blob
            for kw in ("业绩说明会", "业绩电话会", "业绩会", "财报电话会", "公司公告", "公告")
        )
    ):
        return "company"

    if (
        doc_code in ("expert_summary", "expert_library")
        or "expert" in doc_code.lower()
        or "expert" in type_id.lower()
        or "专家" in type_blob
        or "expert" in type_blob.lower()
    ):
        return "expert"

    if doc_code == "news" or any(kw in type_blob for kw in ("资讯", "新闻", "快讯")):
        return "news"

    # Third-party reports and global think tanks are not sell-side broker reports;
    # keep them as unknown since the tier taxonomy has no dedicated category for them.
    if doc_code in (
        "thirdReport",
        "third_party_reports",
        "globalThinkTank",
        "global_think_tank",
    ) or any(
        kw in type_blob
        for kw in ("第三方报告", "三方报告", "全球智库", "智库")
    ):
        return "unknown"

    # Recording scope (institution/personal) or generic summary/meeting category
    # alone does not prove sell-side identity; require an explicit sell-side code
    # or a subtype that confirms a broker research report.
    if doc_code in (
        "comment",
        "chinaReport",
        "foreignReport",
        "sell_side",
        "sell_side_report",
        "sell_side_comment",
        "domestic_report",
        "foreign_report",
    ) or any(
        kw in type_blob
        for kw in (
            "券商",
            "卖方",
            "研报",
            "研究报告",
            "买点卖点",
            "外资报告",
            "国内报告",
            "深度报告",
            "证券研究",
        )
    ):
        return "broker_report"

    return "unknown"


def _declare_alphaengine(
    inner: dict[str, Any], fallback_as_of: str, operation: str = ""
) -> list[dict[str, Any]]:
    data = _unwrap_mcp_proxy_payload(inner)
    if not isinstance(data, dict):
        return []

    eff_op = str(
        operation or inner.get("operation") or data.get("operation") or ""
    ).strip()
    if eff_op in _ALPHAENGINE_NON_DOC_OPS or eff_op not in _ALPHAENGINE_DOC_SOURCE_OPS:
        return []

    # Defensive structural guards when operation was omitted: recording tasks,
    # document metadata (get_document_info), attachments, filters, and auth_status
    # are not research document sources.
    if (
        "record_status" in data
        or ("attachments" in data and "text" not in data and "results" not in data)
        or ("has_summary_content" in data and "text" not in data and "results" not in data)
        or ("authenticated" in data and "results" not in data and "text" not in data)
    ):
        return []

    out: list[dict[str, Any]] = []

    def _from_item(item: dict[str, Any]) -> dict[str, Any] | None:
        if "record_status" in item or ("roadshow_id" in item and "doc_id" not in item):
            return None
        raw_id = str(
            item.get("doc_id") or item.get("document_id") or item.get("id") or ""
        ).strip()
        if not raw_id:
            return None
        doc_id = raw_id if raw_id.startswith("alphaengine:") else f"alphaengine:{raw_id}"
        identity = _infer_alphaengine_identity(item)
        sources = item.get("sources")
        if isinstance(sources, list) and sources and sources[0]:
            pub = str(sources[0])
        elif item.get("source_names") and isinstance(item.get("source_names"), list) and item["source_names"][0]:
            pub = str(item["source_names"][0])
        elif item.get("publisher"):
            pub = str(item["publisher"])
        else:
            pub = "AlphaEngine"
        raw_pub_time = str(
            item.get("publish_time") or item.get("publish_date") or item.get("date") or ""
        ).strip()[:19]
        as_of_val = raw_pub_time[:10] if raw_pub_time else ""
        as_of_basis_val = "publish_time" if as_of_val else "unknown"
        title = str(item.get("title") or item.get("title_cn") or doc_id)
        return _make_entry(
            document_id=doc_id,
            route="alphaengine",
            identity=identity,
            publisher=pub,
            title=title,
            as_of=as_of_val,
            as_of_basis=as_of_basis_val,
            source_type="research_doc",
            url=None,
            private=True,
            independent=True,
        )

    meta = data.get("metadata")
    if isinstance(meta, dict):
        merged_meta = dict(data)
        merged_meta.update(meta)
        entry = _from_item(merged_meta)
        if entry:
            out.append(entry)
    elif data.get("doc_id") and ("text" in data or "content_field" in data or eff_op == "get_document"):
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


def _declare_market_regime(arguments: dict[str, Any], inner: dict[str, Any], fallback_as_of: str) -> list[dict[str, Any]]:
    if inner.get("ok") is False:
        return []
    op = str(inner.get("operation") or arguments.get("operation") or "snapshot").strip() or "snapshot"
    raw_as_of = inner.get("date") or inner.get("trade_date") or ""
    if not raw_as_of and isinstance(inner.get("as_of"), str):
        raw_as_of = inner["as_of"]
    elif not raw_as_of and isinstance(inner.get("as_of"), dict):
        as_of_map = inner["as_of"]
        raw_as_of = (
            as_of_map.get("us")
            or as_of_map.get("cn_crowding")
            or as_of_map.get("cn_margin")
            or as_of_map.get("bbg")
            or ""
        )
    as_of = str(raw_as_of or fallback_as_of)[:10]
    doc_id = f"regime:{op}:{as_of}" if as_of else f"regime:{op}"
    url = inner.get("source_url") if isinstance(inner.get("source_url"), str) else None
    return [
        _make_entry(
            document_id=doc_id,
            route="market-regime",
            identity="derived",
            publisher="OpenClaw",
            title=f"Regime {op}",
            as_of=as_of,
            as_of_basis="date",
            source_type="data_api",
            url=url,
            private=True,
            independent=False,
        )
    ]


def _format_xueqiu_iso_date(
    item: dict[str, Any], fallback_as_of: str | None
) -> tuple[str, str | None]:
    """Convert Xueqiu `created_at_ms` (or numeric `created_at`) to ISO-8601 with `+08:00`, or parse `YYYY-MM-DD HH:MM` as Beijing time."""
    for ts_key in ("created_at_ms", "created_at"):
        ts_val = item.get(ts_key)
        if isinstance(ts_val, bool) or ts_val is None:
            continue
        ms: float | None = None
        if isinstance(ts_val, (int, float)) and ts_val > 0:
            ms = float(ts_val)
        elif isinstance(ts_val, str) and ts_val.strip().isdigit():
            ms = float(ts_val.strip())
        if ms is not None and ms > 0:
            if ms < 1e11:
                ms *= 1000.0
            dt = datetime.fromtimestamp(ms / 1000.0, tz=_BEIJING_TZ)
            return dt.isoformat(timespec="seconds"), dt.strftime("%Y-%m-%d")

    raw_date = str(
        item.get("created_at")
        or item.get("date")
        or item.get("published_at")
        or fallback_as_of
        or ""
    ).strip()
    if not raw_date:
        return "", fallback_as_of

    normalized = raw_date.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(normalized, fmt).replace(tzinfo=_BEIJING_TZ)
            return dt.isoformat(timespec="seconds"), dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    as_of = raw_date[:10] if len(raw_date) >= 10 else fallback_as_of
    return raw_date, as_of


def _declare_xueqiu(
    arguments: dict[str, Any],
    payload: dict[str, Any],
    inner: dict[str, Any],
    fallback_as_of: str,
) -> list[dict[str, Any]]:
    raw_data = payload.get("data")
    items: list[dict[str, Any]] = []
    if isinstance(raw_data, list):
        items = [x for x in raw_data if isinstance(x, dict)]
    elif isinstance(inner.get("posts"), list):
        items = [x for x in inner["posts"] if isinstance(x, dict)]
    elif isinstance(inner.get("comments"), list):
        items = [x for x in inner["comments"] if isinstance(x, dict)]
    elif isinstance(inner.get("results"), list):
        items = [x for x in inner["results"] if isinstance(x, dict)]
    elif isinstance(inner.get("items"), list):
        items = [x for x in inner["items"] if isinstance(x, dict)]
    elif isinstance(raw_data, dict) and any(
        k in raw_data for k in ("id", "url", "text", "title", "author", "created_at", "created_at_ms")
    ):
        items = [raw_data]
    elif any(k in inner for k in ("id", "url", "text", "title", "author", "created_at", "created_at_ms")) and "data" not in inner:
        items = [inner]

    out: list[dict[str, Any]] = []
    req_url = str(arguments.get("url") or "").strip()
    req_post_id = str(arguments.get("post_id") or "").strip()

    for idx, item in enumerate(items):
        item_id = str(item.get("id") or item.get("post_id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not url:
            if req_url:
                url = req_url
            elif req_post_id:
                url = f"https://xueqiu.com/status/{req_post_id}"
            elif item_id:
                url = f"https://xueqiu.com/status/{item_id}"
        iso_date, as_of = _format_xueqiu_iso_date(item, fallback_as_of)
        author = (
            str(item.get("author") or item.get("user") or item.get("screen_name") or "雪球用户").strip()
            or "雪球用户"
        )
        title = str(item.get("title") or item.get("text") or f"雪球 {item_id or (idx + 1)}").strip()
        doc_id = f"xueqiu:{item_id}" if item_id else f"xueqiu:{idx + 1}"
        entry = _make_entry(
            document_id=doc_id,
            route="xueqiu",
            identity="news",
            publisher="雪球",
            title=title,
            as_of=as_of,
            as_of_basis="publish_time",
            source_type="community_post",
            url=url or None,
            private=False,
            independent=True,
        )
        entry["platform"] = "xueqiu"
        entry["url"] = _trunc(url, MAX_URL_LEN)
        entry["date"] = _trunc(iso_date or as_of, 64)
        entry["author"] = _trunc(author, 120)
        out.append(entry)
        if len(out) >= MAX_SOURCES_PER_CALL:
            break
    return out[:MAX_SOURCES_PER_CALL]


def _declare_employee(
    arguments: dict[str, Any],
    payload: dict[str, Any],
    inner: dict[str, Any],
    fallback_as_of: str,
) -> list[dict[str, Any]]:
    if inner.get("ok") is False:
        return []
    raw_data = payload.get("data")
    reviews: list[dict[str, Any]] = []
    if isinstance(raw_data, list):
        reviews = [x for x in raw_data if isinstance(x, dict)]
    elif isinstance(inner.get("reviews"), list):
        reviews = [x for x in inner["reviews"] if isinstance(x, dict)]
    elif isinstance(inner.get("results"), list):
        reviews = [x for x in inner["results"] if isinstance(x, dict)]
    elif isinstance(inner.get("items"), list):
        reviews = [x for x in inner["items"] if isinstance(x, dict)]

    default_platform = (
        str(inner.get("source") or arguments.get("operation") or "blind").strip().lower() or "blind"
    )
    if default_platform == "summary":
        default_platform = "blind"
    company = str(inner.get("company") or arguments.get("company") or "").strip()
    req_url = str(inner.get("url") or arguments.get("url") or "").strip()
    out: list[dict[str, Any]] = []

    for idx, r in enumerate(reviews):
        platform = str(r.get("source") or default_platform).strip().lower() or default_platform
        rev_id = str(r.get("id") or "").strip()
        url = str(r.get("url") or req_url or "").strip()
        if not url and company and platform == "blind":
            url = f"https://www.teamblind.com/company/{company}/reviews"
        raw_date = str(
            r.get("created_at") or r.get("date") or r.get("published_at") or fallback_as_of or ""
        ).strip()
        as_of = raw_date[:10] if raw_date else fallback_as_of
        author = (
            str(
                r.get("author")
                or r.get("jobgroup")
                or r.get("job_title")
                or r.get("role")
                or "Anonymous Employee"
            ).strip()
            or "Anonymous Employee"
        )
        title = str(
            r.get("summary") or r.get("title") or f"{company or platform} review {rev_id or (idx + 1)}"
        ).strip()
        doc_id = f"employee:{platform}:{rev_id}" if rev_id else f"employee:{platform}:{idx + 1}"
        entry = _make_entry(
            document_id=doc_id,
            route="employee",
            identity="news",
            publisher=platform.capitalize(),
            title=title,
            as_of=as_of,
            as_of_basis="publish_time",
            source_type="employee_review",
            url=url or None,
            private=False,
            independent=True,
        )
        entry["platform"] = _trunc(platform, 60)
        entry["url"] = _trunc(url, MAX_URL_LEN)
        entry["date"] = _trunc(raw_date or as_of, 64)
        entry["author"] = _trunc(author, 120)
        out.append(entry)
        if len(out) >= MAX_SOURCES_PER_CALL:
            break

    if not out and (
        inner.get("monthly_ratings")
        or inner.get("top_pros_terms")
        or inner.get("top_cons_terms")
        or inner.get("overall_rating")
        or inner.get("count")
    ):
        url = req_url or (
            f"https://www.teamblind.com/company/{company}/reviews"
            if company
            else "https://www.teamblind.com"
        )
        latest_month = ""
        monthly = inner.get("monthly_ratings")
        if isinstance(monthly, list):
            months = [
                str(m.get("month") or "").strip()
                for m in monthly
                if isinstance(m, dict) and str(m.get("month") or "").strip()
            ]
            if months:
                latest_month = max(months)
        raw_date = latest_month or fallback_as_of
        as_of = raw_date[:10] if raw_date else fallback_as_of
        doc_id = f"employee:{default_platform}:{company or 'summary'}:summary"
        entry = _make_entry(
            document_id=doc_id,
            route="employee",
            identity="news",
            publisher=default_platform.capitalize(),
            title=f"{company or default_platform} employee review summary",
            as_of=as_of,
            as_of_basis="publish_time",
            source_type="employee_review",
            url=url,
            private=False,
            independent=True,
        )
        entry["platform"] = _trunc(default_platform, 60)
        entry["url"] = _trunc(url, MAX_URL_LEN)
        entry["date"] = _trunc(raw_date or as_of, 64)
        entry["author"] = "Blind Aggregate"
        out.append(entry)

    return out[:MAX_SOURCES_PER_CALL]


def _declare_x_timeline(
    arguments: dict[str, Any],
    payload: dict[str, Any],
    inner: dict[str, Any],
    fallback_as_of: str,
) -> list[dict[str, Any]]:
    raw_data = payload.get("data")
    items: list[dict[str, Any]] = []
    if isinstance(raw_data, dict) and isinstance(raw_data.get("items"), list):
        items = [x for x in raw_data["items"] if isinstance(x, dict)]
    elif isinstance(inner.get("items"), list):
        items = [x for x in inner["items"] if isinstance(x, dict)]

    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        status_id = str(item.get("id") or "").strip()
        if not status_id or status_id in seen_ids:
            continue
        seen_ids.add(status_id)
        handle = str(item.get("author_handle") or "").strip().lstrip("@") or "unknown"
        iso_date = str(item.get("created_at") or "").strip()
        as_of = iso_date[:10] if len(iso_date) >= 10 and iso_date[:4].isdigit() else fallback_as_of
        url = str(item.get("url") or "").strip() or f"https://x.com/{handle}/status/{status_id}"
        title = str(item.get("text") or f"X post {status_id}").strip()
        entry = _make_entry(
            document_id=f"x-timeline:{status_id}",
            route="x-timeline",
            identity="news",
            publisher="X",
            title=title,
            as_of=as_of,
            as_of_basis="publish_time",
            source_type="community_post",
            url=url,
            private=False,
            independent=True,
        )
        entry["platform"] = "x"
        entry["url"] = _trunc(url, MAX_URL_LEN)
        entry["date"] = _trunc(iso_date or as_of, 64)
        entry["author"] = _trunc(f"@{handle}", 120)
        out.append(entry)
        if len(out) >= MAX_SOURCES_PER_CALL:
            break
    return out[:MAX_SOURCES_PER_CALL]


def _declare_last30days(
    arguments: dict[str, Any],
    payload: dict[str, Any],
    inner: dict[str, Any],
    fallback_as_of: str,
) -> list[dict[str, Any]]:
    raw_data = payload.get("data")
    candidates: list[dict[str, Any]] = []
    if isinstance(raw_data, list):
        candidates = [x for x in raw_data if isinstance(x, dict)]
    elif isinstance(inner.get("results"), list):
        candidates = [x for x in inner["results"] if isinstance(x, dict)]
    elif isinstance(inner.get("ranked_candidates"), list):
        candidates = [x for x in inner["ranked_candidates"] if isinstance(x, dict)]
    elif isinstance(inner.get("items"), list):
        candidates = [x for x in inner["items"] if isinstance(x, dict)]
    elif isinstance(inner.get("items_by_source"), dict):
        for src_name, src_items in inner["items_by_source"].items():
            if isinstance(src_items, list):
                for item in src_items:
                    if isinstance(item, dict):
                        candidates.append({"source": src_name, **item})

    gen_date = str(inner.get("generated_at") or fallback_as_of or "")[:10]
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(candidates):
        sources_list = item.get("sources")
        platform = str(
            item.get("source")
            or item.get("platform")
            or (sources_list[0] if isinstance(sources_list, list) and sources_list else "")
            or "last30days"
        ).strip().lower()
        url = str(item.get("url") or "").strip()
        raw_date = str(
            item.get("published_at")
            or item.get("created_at")
            or item.get("date")
            or gen_date
            or fallback_as_of
            or ""
        ).strip()
        as_of = raw_date[:10] if raw_date else (gen_date or fallback_as_of)
        author = (
            str(
                item.get("author")
                or item.get("author_handle")
                or item.get("user")
                or item.get("subreddit")
                or item.get("channel")
                or platform
            ).strip()
            or platform
        )
        cand_id = str(item.get("candidate_id") or item.get("id") or "").strip()
        title = str(
            item.get("title") or item.get("text") or item.get("summary") or f"{platform} {cand_id or (idx + 1)}"
        ).strip()
        doc_id = f"last30days:{platform}:{cand_id}" if cand_id else f"last30days:{platform}:{idx + 1}"
        entry = _make_entry(
            document_id=doc_id,
            route="last30days",
            identity="news",
            publisher=platform,
            title=title,
            as_of=as_of,
            as_of_basis="publish_time",
            source_type="community_post",
            url=url or None,
            private=False,
            independent=True,
        )
        entry["platform"] = _trunc(platform, 60)
        entry["url"] = _trunc(url, MAX_URL_LEN)
        entry["date"] = _trunc(raw_date or as_of, 64)
        entry["author"] = _trunc(author, 120)
        out.append(entry)
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

    if route in ("company-wiki", "sales-notes", "tmtb", "funda-ai", "all", "sales-mail"):
        return _declare_research_library(route, inner, fallback_as_of)
    if route == "alphaengine":
        op_arg = str(args.get("operation") or args.get("action") or "").strip()
        return _declare_alphaengine(inner, fallback_as_of, operation=op_arg)
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
    if route == "market-regime":
        return _declare_market_regime(args, inner, fallback_as_of)
    if route == "cn-hk":
        return _declare_cnhk(args, inner, fallback_as_of)
    if route == "xueqiu":
        return _declare_xueqiu(args, payload, inner, fallback_as_of)
    if route == "employee":
        return _declare_employee(args, payload, inner, fallback_as_of)
    if route == "last30days":
        return _declare_last30days(args, payload, inner, fallback_as_of)
    if route == "x-timeline":
        return _declare_x_timeline(args, payload, inner, fallback_as_of)

    return []
