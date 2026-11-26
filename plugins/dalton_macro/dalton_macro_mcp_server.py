#!/usr/bin/env python3
"""Dalton public US macro MCP server (`dalton_macro`).

Self-contained stdio MCP server providing keyless access to official:
- FRED / ALFRED macro time series (`get_fred_series`, including historical `vintage`)
- US Treasury Fiscal Data (`get_treasury_data`, `dataset="tga" | "debt"`)
- Common FRED series catalog (`list_macro_catalog`)

Matches the legacy `macro(route="fred")` and `macro(route="treasury")` payload
contracts and emits top-level `_sources` provenance compatible with
`src.dalton.provenance.declared_sources.expand_declared_sources`.
"""

from __future__ import annotations

import csv
import io
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

try:
    from _bootstrap import MCPServer
except ModuleNotFoundError:
    from mcp_servers._bootstrap import MCPServer

from mcp_servers._envelope import make_error, make_response
from mcp_servers._schemas import OBJECT, RECORDS, envelope_schema, output_model

mcp = MCPServer("dalton_macro")

_MacroOut = output_model("DaltonMacroSeriesOutput", envelope_schema(RECORDS))
_CatalogOut = output_model("DaltonMacroCatalogOutput", envelope_schema(OBJECT))

CATALOG: dict[str, str] = {
    "DGS10": "美国十年期国债收益率",
    "DGS2": "美国两年期国债收益率",
    "DFII10": "美国十年期实际收益率",
    "T10Y2Y": "美国十年期减两年期国债利差",
    "BAMLH0A0HYM2": "美国高收益债期权调整利差",
    "CPIAUCSL": "美国 CPI（季调）",
    "PCEPI": "美国 PCE 价格指数",
    "UNRATE": "美国失业率",
    "PAYEMS": "美国非农就业人数",
    "GDPC1": "美国实际 GDP",
    "WALCL": "美联储总资产",
    "RRPONTSYD": "隔夜逆回购余额",
    "WTREGEN": "财政部一般账户周度余额",
}

TREASURY_BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
TREASURY_DATASETS: dict[str, tuple[str, str]] = {
    "tga": (
        "v1/accounting/dts/operating_cash_balance",
        "https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/operating-cash-balance",
    ),
    "debt": (
        "v2/accounting/od/debt_to_penny",
        "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/debt-to-the-penny",
    ),
}

_SERIES_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_TREASURY_PAGES = 20


def validate_iso_date(value: str, *, allow_future: bool = False) -> str:
    """Validate strict `YYYY-MM-DD` date string (default: not after today)."""
    cleaned = str(value or "").strip()
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("日期须为 YYYY-MM-DD 格式") from exc
    if parsed.isoformat() != cleaned or (not allow_future and parsed > date.today()):
        raise ValueError("日期须为 YYYY-MM-DD，且不能晚于今天")
    return cleaned


def _decimal_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", ".", "null", "None"):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"Non-numeric value in provider response: {text!r}") from exc
    if not number.is_finite():
        raise ValueError("Non-finite numeric value in provider response")
    return str(number)


def _http_get_text(url: str, *, timeout: float = 25.0) -> str:
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS official endpoints are permitted")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DaltonMacro/0.1 (public macro data client)",
            "Accept": "text/csv, application/json, */*",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(_MAX_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise ValueError("Official endpoint response exceeded maximum allowed size")
    return raw.decode("utf-8-sig")


def parse_fred_csv(
    content: str,
    series: str,
    start: str,
    end: str,
    vintage: str | None = None,
) -> list[dict[str, Any]]:
    """Parse FRED/ALFRED CSV into sorted `{observation_date, value}` rows."""
    reader = csv.DictReader(io.StringIO(content))
    expected_col = series + ("_" + vintage.replace("-", "") if vintage else "")
    if reader.fieldnames != ["observation_date", expected_col]:
        raise ValueError(
            f"Unexpected CSV columns; expected observation_date,{expected_col}. "
            "Historical requests never fall back to current data."
        )
    rows: list[dict[str, Any]] = []
    for row in reader:
        observed = validate_iso_date(str(row.get("observation_date") or ""))
        if start <= observed <= end and (not vintage or observed <= vintage):
            rows.append(
                {
                    "observation_date": observed,
                    "value": _decimal_value(row.get(expected_col)),
                }
            )
    dates = [r["observation_date"] for r in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Duplicate observation dates in provider response")
    return sorted(rows, key=lambda r: r["observation_date"])


def _make_macro_source(
    *,
    identifier: str,
    route: str,
    publisher: str,
    title: str,
    as_of: str,
    url: str,
) -> dict[str, Any]:
    return {
        "document_id": identifier[:200],
        "identifier": identifier[:200],
        "route": route,
        "source_type": "data_api",
        "identity": "official_data",
        "detail": "official_data",
        "tier": 1,
        "publisher": publisher,
        "provider": f"dalton_macro:{publisher}",
        "title": title[:300],
        "as_of": as_of[:10],
        "as_of_basis": "observation_date",
        "url": url,
        "private": False,
        "independent": True,
    }


def fetch_fred_series(
    series: str,
    start: str,
    end: str,
    vintage: str | None = None,
    *,
    limit: int | None = None,
    fetch: Callable[[str], str] = _http_get_text,
) -> dict[str, Any]:
    """Fetch a FRED or ALFRED macro series (pure/testable with injectable `fetch`)."""
    cleaned_series = str(series or "").strip()
    if not _SERIES_ID_RE.fullmatch(cleaned_series):
        raise ValueError("Use a FRED series ID, for example DGS10; not a URL")
    start_iso = validate_iso_date(start)
    end_iso = validate_iso_date(end)
    if start_iso > end_iso:
        raise ValueError("start must not be later than end")

    vintage_iso: str | None = None
    if vintage is not None and str(vintage).strip():
        vintage_iso = validate_iso_date(str(vintage).strip())
        if start_iso > vintage_iso:
            raise ValueError("start must not be later than vintage")
        end_iso = min(end_iso, vintage_iso)

    params: dict[str, str] = {"id": cleaned_series, "cosd": start_iso, "coed": end_iso}
    if vintage_iso:
        params["vintage_date"] = vintage_iso
    base_url = (
        "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
        if vintage_iso
        else "https://fred.stlouisfed.org/graph/fredgraph.csv"
    )
    url = f"{base_url}?{urlencode(params)}"
    rows = parse_fred_csv(fetch(url), cleaned_series, start_iso, end_iso, vintage_iso)
    if limit is not None:
        capped = max(1, int(limit))
        rows = rows[-capped:]

    valid = [r for r in rows if r["value"] is not None]
    source_name = "alfred" if vintage_iso else "fred"
    as_of = rows[-1]["observation_date"] if rows else (vintage_iso or end_iso)
    series_url = f"https://alfred.stlouisfed.org/series?seid={cleaned_series}"
    sources = [
        _make_macro_source(
            identifier=f"fred:{cleaned_series}:{vintage_iso or end_iso}",
            route="fred",
            publisher="FRED/ALFRED" if vintage_iso else "FRED",
            title=f"FRED {cleaned_series}" + (f" (vintage {vintage_iso})" if vintage_iso else ""),
            as_of=as_of,
            url=series_url,
        )
    ]
    return {
        "_sources": sources,
        "ok": True,
        "status": "ok" if valid else "empty",
        "source": source_name,
        "source_url": url,
        "series_url": series_url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "cache": {"hit": False},
        "published_at": None,
        "series": cleaned_series,
        "start": start_iso,
        "end": end_iso,
        "vintage": vintage_iso,
        "point_in_time": "alfred_vintage_daily" if vintage_iso else "current_revised_series",
        "unit": None,
        "metadata_status": (
            "CSV does not supply units/frequency; verify series notes for the requested vintage, "
            "especially rebased GDP"
        ),
        "missing_count": len(rows) - len(valid),
        "count": len(rows),
        "rows": rows,
    }


def parse_treasury_payload(
    payload: dict[str, Any],
    dataset: str,
    start: str,
    end: str,
) -> list[dict[str, Any]]:
    """Parse US Treasury Fiscal Data JSON into normalized daily rows."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Treasury response has no data array")
    records = payload["data"]
    rows: list[dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        observed = validate_iso_date(str(row.get("record_date") or ""))
        if not start <= observed <= end:
            raise ValueError("Treasury returned an observation outside the requested window")
        if dataset == "tga":
            acct = row.get("account_type")
            if acct == "Treasury General Account (TGA) Closing Balance":
                value, field = row.get("open_today_bal"), "open_today_bal"
            elif (
                acct == "Treasury General Account (TGA)"
                and _decimal_value(row.get("close_today_bal")) is not None
            ):
                value, field = row["close_today_bal"], "close_today_bal"
            else:
                continue
            rows.append(
                {
                    "observation_date": observed,
                    "value": _decimal_value(value),
                    "unit": "USD million",
                    "account_type": acct,
                    "value_field": field,
                }
            )
        else:
            fields = ("debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt")
            values = {key: _decimal_value(row.get(key)) for key in fields}
            if all(values[key] is not None for key in fields):
                if (
                    abs(
                        Decimal(str(values[fields[0]]))
                        + Decimal(str(values[fields[1]]))
                        - Decimal(str(values[fields[2]]))
                    )
                    > Decimal("0.01")
                ):
                    raise ValueError("Treasury debt components do not reconcile")
            rows.append({"observation_date": observed, "unit": "USD", **values})
    if records and not rows:
        raise ValueError(
            "Treasury returned records but no recognized closing balance; "
            "inspect raw schema instead of reporting no data"
        )
    dates = [r["observation_date"] for r in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Multiple selected Treasury observations for a date; inspect dimensions")
    return sorted(rows, key=lambda r: r["observation_date"])


def fetch_treasury_data(
    dataset: str,
    start: str,
    end: str,
    *,
    limit: int | None = None,
    max_pages: int = _DEFAULT_MAX_TREASURY_PAGES,
    fetch: Callable[[str], str] = _http_get_text,
) -> dict[str, Any]:
    """Fetch official US Treasury Fiscal Data (`tga` or `debt`)."""
    ds_key = str(dataset or "").strip().lower()
    if ds_key not in TREASURY_DATASETS:
        raise ValueError(f"dataset must be one of {sorted(TREASURY_DATASETS)}, got {dataset!r}")
    start_iso = validate_iso_date(start)
    end_iso = validate_iso_date(end)
    if start_iso > end_iso:
        raise ValueError("start must not be later than end")

    path, source_url = TREASURY_DATASETS[ds_key]
    merged: list[dict[str, Any]] = []
    request_urls: list[str] = []
    meta: dict[str, Any] = {}
    complete = True
    capped_pages = max(1, int(max_pages))
    for page_num in range(1, capped_pages + 1):
        query = {
            "filter": f"record_date:gte:{start_iso},record_date:lte:{end_iso}",
            "sort": "record_date",
            "page[size]": 1000,
            "page[number]": page_num,
        }
        url = TREASURY_BASE + path + "?" + urlencode(query)
        request_urls.append(url)
        payload = json.loads(fetch(url))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Invalid Treasury data response")
        merged.extend(payload["data"])
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        pages = meta.get("total-pages")
        if pages is None:
            raise ValueError("Treasury response missing pagination metadata")
        if page_num >= int(pages):
            break
    else:
        complete = False

    rows = parse_treasury_payload({"data": merged}, ds_key, start_iso, end_iso)
    if limit is not None:
        capped = max(1, int(limit))
        rows = rows[-capped:]

    as_of = rows[-1]["observation_date"] if rows else end_iso
    sources = [
        _make_macro_source(
            identifier=f"treasury:{ds_key}:{start_iso}:{end_iso}",
            route="treasury",
            publisher="US Treasury Fiscal Data",
            title=f"US Treasury {ds_key.upper()} ({start_iso}..{end_iso})",
            as_of=as_of,
            url=source_url,
        )
    ]
    return {
        "_sources": sources,
        "ok": True,
        "status": ("ok" if rows else "empty") if complete else "partial",
        "complete": complete,
        "source": "us_treasury_fiscaldata",
        "source_url": source_url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "cache": {"hit": False},
        "published_at": None,
        "dataset": ds_key,
        "start": start_iso,
        "end": end_iso,
        "point_in_time": "not_available",
        "date_semantics": (
            "record_date bounds observations only; this endpoint does not supply historical release vintages"
        ),
        "count": len(rows),
        "rows": rows,
        "request_urls": request_urls,
        "provider_metadata": meta,
        "warnings": [] if complete else [f"Pagination capped at {capped_pages} pages; narrow the date range"],
    }


@mcp.tool()
def get_fred_series(
    series: str,
    start: str,
    end: str,
    vintage: str | None = None,
    limit: int | None = None,
) -> _MacroOut:
    """Fetch official FRED or ALFRED macro time series observations.

    Supports point-in-time historical vintages via `vintage` (ALFRED CSV endpoint)
    without falling back to current revised data.

    Args:
        series: FRED series ID (e.g. 'DGS10', 'SOFR', 'CPIAUCSL', 'WALCL').
        start: Window start date YYYY-MM-DD.
        end: Window end date YYYY-MM-DD.
        vintage: Optional ALFRED vintage date YYYY-MM-DD for historical as-known values.
        limit: Optional maximum number of most recent observations to return.

    Returns:
        dict: Observation rows (`observation_date`, `value`), metadata, and `_sources`.
    """
    try:
        res = fetch_fred_series(series=series, start=start, end=end, vintage=vintage, limit=limit)
        env = make_response(
            data=res["rows"],
            source=res["source"],
            symbol=res["series"],
            count=res["count"],
        )
        return {**res, **env}
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "source": "alfred" if vintage else "fred",
            **make_error("upstream_error", str(exc), symbol=series),
        }


@mcp.tool()
def get_treasury_data(
    dataset: str,
    start: str,
    end: str,
    limit: int | None = None,
) -> _MacroOut:
    """Fetch US Treasury Fiscal Data (`tga` daily closing balance or `debt` to the penny).

    Args:
        dataset: Treasury dataset name: 'tga' (Treasury General Account closing balance)
            or 'debt' (Debt to the Penny).
        start: Window start date YYYY-MM-DD.
        end: Window end date YYYY-MM-DD.
        limit: Optional maximum number of most recent observations to return.

    Returns:
        dict: Daily Treasury rows, completeness metadata, warnings, and `_sources`.
    """
    try:
        res = fetch_treasury_data(dataset=dataset, start=start, end=end, limit=limit)
        env = make_response(
            data=res["rows"],
            source=res["source"],
            symbol=res["dataset"],
            count=res["count"],
            incomplete=not bool(res.get("complete", True)),
        )
        return {**res, **env}
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "source": "us_treasury_fiscaldata",
            **make_error("upstream_error", str(exc), symbol=dataset),
        }


@mcp.tool()
def list_macro_catalog() -> _CatalogOut:
    """Return a starter catalog of common FRED series IDs and descriptions.

    Any valid FRED series ID may be queried via `get_fred_series`; this catalog
    lists frequently used rates, inflation, employment, GDP, and liquidity series.
    """
    env = make_response(
        data={"series": dict(CATALOG)},
        source="dalton_macro",
        count=len(CATALOG),
    )
    return {
        "ok": True,
        "status": "ok",
        "series": dict(CATALOG),
        **env,
    }


if __name__ == "__main__":
    mcp.run()
