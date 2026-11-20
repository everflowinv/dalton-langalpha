"""Host-side SQLite exact search cache for AlphaEngine (`search_library` & `transcript`).

Stdlib-only (`sqlite3`, `asyncio`, `hashlib`, `json`, `unicodedata`) so
``researchdata_bridge.py`` can import it with zero external dependencies.
All disk I/O runs inside ``asyncio.to_thread`` under a thread lock with WAL
mode and short transactions; any database open/read/write failure logs a single
warning and degrades cleanly to passthrough.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger("researchdata_bridge")

CACHE_KEY_VERSION = 4
LEGACY_CACHE_KEY_VERSION_V3 = 3
OP_SEARCH_LIBRARY = "alphaengine.search_library"
OP_TRANSCRIPT = "transcript"
DEFAULT_SEARCH_LIMIT = 3
SLICED_CACHE_NOTE = "结果由更大条数的缓存截取；需要更多结果请提高 limit 重新检索"

AE_BREAKER_INITIAL_S = 60.0
AE_BREAKER_MAX_S = 600.0

# Centralized personal-scope markers (never cached / never shared across users).
PERSONAL_MEETING_SCOPES: frozenset[str] = frozenset({"personal"})
PERSONAL_DOCUMENT_CATEGORIES: frozenset[str] = frozenset(
    {"personal_meetings", "personal_meeting", "personalmeeting", "personalmeetings", "personal"}
)
PERSONAL_DOCUMENT_CODES: frozenset[str] = frozenset(
    {"personalmeeting", "personal_meeting", "personal_meetings", "personalmeetings", "personal"}
)
PERSONAL_TYPE_IDS: frozenset[str] = frozenset(
    {"personalmeeting", "personal_meeting", "personal_meetings", "personalmeetings"}
)

TTL_TODAY_NON_EMPTY_S = 2 * 3600
TTL_TODAY_EMPTY_S = 30 * 60
TTL_RECENT_7D_S = 12 * 3600
TTL_OLDER_S = 7 * 86400
STALE_RETENTION_S = 7 * 86400
# The AlphaEngine MCP keeps search cursors in memory for 15 minutes (server.mjs
# SEARCH_CURSOR_TTL_MS). A cached page that has more results carries one of those
# cursors, so past this age a hit would hand the caller a dead cursor and page 2
# fails with E_CURSOR_EXPIRED (seen 2026-11-20). Such hits are treated as misses.
CURSOR_SAFE_AGE_S = 12 * 60

DEFAULT_MAX_ROWS = 10_000
CLEANUP_EVERY_WRITES = 50

_FILTER_CODE_ARRAY_KEYS: frozenset[str] = frozenset(
    {
        "type_ids",
        "source_ids",
        "document_codes",
        "document_categories",
        "markets",
        "stock_group_ids",
        "first_regions",
        "second_regions",
    }
)

_FILTER_KEYWORD_ARRAY_KEYS: frozenset[str] = frozenset(
    {
        "keywords",
        "company_names",
        "company_short_names",
        "industry_names",
        "source_names",
    }
)

_DATE_PREFIX_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?$")
_DATE_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")

_PROMOTED_OR_STRIPPED_NESTED_KEYS: frozenset[str] = frozenset(
    {
        "fresh",
        "cursor",
        "query",
        "limit",
        "sort",
        "date_from",
        "date_to",
        "start_date",
        "end_date",
        "start",
        "end",
    }
)


def default_bridge_today() -> str:
    """Return today's YYYY-MM-DD in Beijing time (or host local date if later)."""
    return max(
        date.today(), datetime.now(ZoneInfo("Asia/Shanghai")).date()
    ).isoformat()


def iso_utc(epoch_s: float) -> str:
    """Format an epoch timestamp as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat()


def normalize_search_query(raw: Any) -> str:
    """Normalize search query: NFKC, collapse whitespace, strip edges, preserve case."""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    return " ".join(text.split())


def normalize_search_query_v4(raw: Any) -> str:
    """Normalize search query for v4 cache keys: NFKC, collapse whitespace, strip, lowercase."""
    return normalize_search_query(raw).lower()


def normalize_date_to_day(raw: Any) -> str:
    """Normalize a date or ISO datetime parameter down to YYYY-MM-DD (day precision)."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    m = _DATE_PREFIX_RE.fullmatch(text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return text
    m = _DATE_COMPACT_RE.fullmatch(text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return text
    return text


_PURE_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATETIME_PREFIX_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})([T\s].+)$")


def normalize_date_param(raw: Any) -> str:
    """Keep pure dates at YYYY-MM-DD precision; normalize datetimes to canonical UTC ISO."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    m = _PURE_DATE_RE.fullmatch(text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return text
    m_compact = _DATE_COMPACT_RE.fullmatch(text)
    if m_compact:
        try:
            return date(int(m_compact[1]), int(m_compact[2]), int(m_compact[3])).isoformat()
        except ValueError:
            return text
    m_dt = _DATETIME_PREFIX_RE.fullmatch(text)
    if m_dt:
        try:
            canon_date = date(int(m_dt[1]), int(m_dt[2]), int(m_dt[3])).isoformat()
            iso_candidate = f"{canon_date}{m_dt[4]}"
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_utc = dt.astimezone(timezone.utc)
            base = dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
            if dt_utc.microsecond:
                frac = f"{dt_utc.microsecond:06d}".rstrip("0")
                return f"{base}.{frac}Z"
            return f"{base}Z"
        except ValueError:
            return text
    return text


def _is_truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return False


def _parse_positive_int(val: Any) -> int | None:
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            parsed = int(s)
            return parsed if parsed > 0 else None
    return None


def _normalize_generic_value(val: Any) -> Any:
    if isinstance(val, dict):
        return {
            str(k): _normalize_generic_value(val[k])
            for k in sorted(val.keys(), key=str)
            if val[k] is not None
        }
    if isinstance(val, list):
        return [_normalize_generic_value(item) for item in val]
    return val


def _dedup_sorted_items(items: list[Any]) -> list[Any]:
    dedup: dict[str, Any] = {}
    for item in items:
        serialized = json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        dedup.setdefault(serialized, item)
    return [dedup[k] for k in sorted(dedup.keys())]


def _normalize_filter_value(key: str, val: Any) -> Any:
    """v3 filter value normalization (only code arrays sorted/deduped)."""
    if isinstance(val, list):
        items = [_normalize_generic_value(item) for item in val if item is not None]
        if key in _FILTER_CODE_ARRAY_KEYS:
            return _dedup_sorted_items(items)
        return items
    return _normalize_generic_value(val)


def _normalize_filter_value_v4(key: str, val: Any) -> Any:
    """v4 filter value normalization: sort and dedup code and keyword arrays."""
    if isinstance(val, list):
        items = [_normalize_generic_value(item) for item in val if item is not None]
        if key == "keywords":
            norm_kw: list[Any] = []
            for item in items:
                if isinstance(item, str):
                    cleaned = normalize_search_query_v4(item)
                    if cleaned:
                        norm_kw.append(cleaned)
                else:
                    norm_kw.append(item)
            return _dedup_sorted_items(norm_kw)
        if key in _FILTER_CODE_ARRAY_KEYS or key in _FILTER_KEYWORD_ARRAY_KEYS:
            return _dedup_sorted_items(items)
        return items
    return _normalize_generic_value(val)


def _sha256_canonical(obj: Any) -> str:
    encoded = json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SearchCachePlan:
    op: str
    cacheable: bool
    bypass_reason: str
    fresh: bool
    user_scope: str
    cache_key: str
    base_key: str
    req_limit: int | None
    date_from: str
    date_to: str
    upstream_arguments: dict[str, Any]
    legacy_cache_key: str = ""
    legacy_base_key: str = ""


@dataclass(frozen=True)
class CacheHit:
    payload: dict[str, Any]
    fetched_at: float
    expires_at: float
    req_limit: int | None
    sliced_from_limit: int | None = None
    note: str = ""
    stale: bool = False
    stale_reason: str = ""


def _value_has_personal_code(val: Any, personal_codes: frozenset[str]) -> bool:
    if val is None:
        return False
    items = val if isinstance(val, (list, tuple, set)) else [val]
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            for part in item.split(","):
                tok = part.strip().lower()
                if tok in personal_codes:
                    return True
        else:
            tok = str(item).strip().lower()
            if tok in personal_codes:
                return True
    return False


def _is_personal_scope_request(params_dict: dict[str, Any]) -> bool:
    """Return True if any parameter targets user-private personal meetings."""
    meeting_scope = str(params_dict.get("meeting_scope") or "").strip().lower()
    if meeting_scope in PERSONAL_MEETING_SCOPES:
        return True
    # Upstream reads this flag with JavaScript Boolean(), so any non-empty value
    # (even the string "false") selects the caller's own reservations.
    own = params_dict.get("own_reservations_only")
    if own is not None and own is not False and own != "" and own != 0:
        return True
    if _value_has_personal_code(
        params_dict.get("document_categories"), PERSONAL_DOCUMENT_CATEGORIES
    ):
        return True
    if _value_has_personal_code(
        params_dict.get("document_codes"), PERSONAL_DOCUMENT_CODES
    ):
        return True
    if _value_has_personal_code(params_dict.get("type_ids"), PERSONAL_TYPE_IDS):
        return True
    return False


def prepare_alphaengine_search_request(
    arguments: dict[str, Any],
    *,
    op: str,
    caller_user: str = "",
    fanout: bool = False,
) -> SearchCachePlan:
    """Canonicalize parameters, build cache keys, and strip `fresh` from upstream args."""
    del caller_user  # Personal scope always bypasses cache; cached keys are team-wide only.
    upstream = copy.deepcopy(arguments) if isinstance(arguments, dict) else {}
    raw_params = upstream.get("params")
    params_dict: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}

    fresh_val = params_dict.pop("fresh", None)
    fresh = _is_truthy(fresh_val)

    if "params" in upstream:
        if params_dict:
            upstream["params"] = params_dict
        else:
            upstream.pop("params", None)

    eff_mode = str(upstream.get("mode") or "").strip().lower() or "fetch"
    if eff_mode == "plan":
        return SearchCachePlan(
            op=op,
            cacheable=False,
            bypass_reason="mode_plan",
            fresh=fresh,
            user_scope="",
            cache_key="",
            base_key="",
            req_limit=None,
            date_from="",
            date_to="",
            upstream_arguments=upstream,
        )

    if op == OP_TRANSCRIPT:
        ticker = str(upstream.get("ticker") or "").strip().upper()
        if ticker:
            upstream["ticker"] = ticker
        else:
            upstream.pop("ticker", None)
        norm_query = normalize_search_query(upstream.get("query"))
        norm_query_v4 = normalize_search_query_v4(upstream.get("query"))
        if norm_query:
            upstream["query"] = norm_query
        else:
            upstream.pop("query", None)
        date_from_raw = str(upstream.get("start") or "").strip()
        date_to_raw = str(upstream.get("end") or "").strip()
        date_from = normalize_date_param(date_from_raw)
        date_to = normalize_date_param(date_to_raw)
        req_limit = _parse_positive_int(upstream.get("limit"))
        if req_limit is not None:
            upstream["limit"] = req_limit
        user_scope = "team"
        base_obj_v4 = {
            "v": CACHE_KEY_VERSION,
            "op": OP_TRANSCRIPT,
            "scope": user_scope,
            "params": {
                "mode": eff_mode,
                "ticker": ticker,
                "query": norm_query_v4,
                "date_from": date_from,
                "date_to": date_to,
            },
        }
        cache_obj_v4 = {**base_obj_v4, "limit": req_limit}
        base_obj_v3 = {
            "v": LEGACY_CACHE_KEY_VERSION_V3,
            "op": OP_TRANSCRIPT,
            "scope": user_scope,
            "params": {
                "mode": eff_mode,
                "ticker": ticker,
                "query": norm_query,
                "date_from": date_from_raw,
                "date_to": date_to_raw,
            },
        }
        cache_obj_v3 = {**base_obj_v3, "limit": req_limit}
        return SearchCachePlan(
            op=OP_TRANSCRIPT,
            cacheable=True,
            bypass_reason="",
            fresh=fresh,
            user_scope=user_scope,
            cache_key=_sha256_canonical(cache_obj_v4),
            base_key=_sha256_canonical(base_obj_v4),
            req_limit=req_limit,
            date_from=date_from,
            date_to=date_to,
            upstream_arguments=upstream,
            legacy_cache_key=_sha256_canonical(cache_obj_v3),
            legacy_base_key=_sha256_canonical(base_obj_v3),
        )

    # OP_SEARCH_LIBRARY: normalize parameters uniformly for first-page, personal,
    # and cursor continuation requests so upstream always sees canonical params.
    # Keep the cursor exactly as the caller sent it (strings or arrays alike);
    # stringifying an array breaks the upstream cursor check.
    raw_cursor = params_dict.get("cursor")
    cursor_val = raw_cursor.strip() if isinstance(raw_cursor, str) else raw_cursor
    if cursor_val in ("", [], {}):
        cursor_val = None

    norm_ticker = str(upstream.get("ticker") or "").strip().upper()
    if norm_ticker:
        upstream["ticker"] = norm_ticker
    else:
        upstream.pop("ticker", None)

    raw_query = (
        upstream.get("query")
        if upstream.get("query") not in (None, "")
        else params_dict.get("query")
    )
    params_dict.pop("query", None)
    norm_query = normalize_search_query(raw_query)
    norm_query_v4 = normalize_search_query_v4(raw_query)
    if norm_query:
        upstream["query"] = norm_query
    else:
        upstream.pop("query", None)

    date_from_raw = str(
        upstream.get("start")
        or params_dict.get("date_from")
        or params_dict.get("start_date")
        or params_dict.get("start")
        or ""
    ).strip()
    date_to_raw = str(
        upstream.get("end")
        or params_dict.get("date_to")
        or params_dict.get("end_date")
        or params_dict.get("end")
        or ""
    ).strip()
    date_from = normalize_date_param(date_from_raw)
    date_to = normalize_date_param(date_to_raw)
    for _dk in ("date_from", "start_date", "start"):
        params_dict.pop(_dk, None)
    for _dk in ("date_to", "end_date", "end"):
        params_dict.pop(_dk, None)
    if date_from_raw:
        upstream["start"] = date_from_raw
    if date_to_raw:
        upstream["end"] = date_to_raw

    explicit_sort = str(params_dict.get("sort") or "").strip()
    eff_sort = explicit_sort or ("relevance" if norm_query else "newest")

    top_limit_raw = upstream.get("limit")
    nested_limit_raw = params_dict.pop("limit", None)
    raw_limit = top_limit_raw if top_limit_raw is not None else nested_limit_raw
    parsed_limit = _parse_positive_int(raw_limit)
    if fanout and parsed_limit is not None and parsed_limit > 50:
        parsed_limit = 50
    req_limit = parsed_limit if parsed_limit is not None else DEFAULT_SEARCH_LIMIT
    upstream["limit"] = req_limit

    canonical_filters_v3: dict[str, Any] = {}
    canonical_filters_v4: dict[str, Any] = {}
    normalized_params_out: dict[str, Any] = {}
    for k in sorted(params_dict.keys()):
        if k in _PROMOTED_OR_STRIPPED_NESTED_KEYS:
            continue
        v = params_dict[k]
        if v is None:
            continue
        norm_v3 = _normalize_filter_value(k, v)
        norm_v4 = _normalize_filter_value_v4(k, v)
        canonical_filters_v3[k] = norm_v3
        canonical_filters_v4[k] = norm_v4
        normalized_params_out[k] = copy.deepcopy(norm_v3)
    if "keywords" not in canonical_filters_v4 and upstream.get("keywords") is not None:
        canonical_filters_v4["keywords"] = _normalize_filter_value_v4(
            "keywords", upstream.get("keywords")
        )
    normalized_params_out["sort"] = eff_sort
    if cursor_val is not None:
        normalized_params_out["cursor"] = copy.deepcopy(cursor_val)
    upstream["params"] = normalized_params_out

    if cursor_val is not None:
        return SearchCachePlan(
            op=OP_SEARCH_LIBRARY,
            cacheable=False,
            bypass_reason="cursor",
            fresh=fresh,
            user_scope="",
            cache_key="",
            base_key="",
            req_limit=req_limit,
            date_from=date_from,
            date_to=date_to,
            upstream_arguments=upstream,
        )

    if _is_personal_scope_request(params_dict):
        return SearchCachePlan(
            op=OP_SEARCH_LIBRARY,
            cacheable=False,
            bypass_reason="personal_scope",
            fresh=fresh,
            user_scope="",
            cache_key="",
            base_key="",
            req_limit=req_limit,
            date_from=date_from,
            date_to=date_to,
            upstream_arguments=upstream,
        )

    user_scope = "team"
    base_obj_v4 = {
        "v": CACHE_KEY_VERSION,
        "op": OP_SEARCH_LIBRARY,
        "scope": user_scope,
        "params": {
            "mode": eff_mode,
            "ticker": norm_ticker,
            "query": norm_query_v4,
            "date_from": date_from,
            "date_to": date_to,
            "sort": eff_sort,
            "filters": canonical_filters_v4,
        },
    }
    cache_obj_v4 = {**base_obj_v4, "limit": req_limit}

    base_obj_v3 = {
        "v": LEGACY_CACHE_KEY_VERSION_V3,
        "op": OP_SEARCH_LIBRARY,
        "scope": user_scope,
        "params": {
            "mode": eff_mode,
            "ticker": norm_ticker,
            "query": norm_query,
            "date_from": date_from_raw,
            "date_to": date_to_raw,
            "sort": eff_sort,
            "filters": canonical_filters_v3,
        },
    }
    cache_obj_v3 = {**base_obj_v3, "limit": req_limit}

    return SearchCachePlan(
        op=OP_SEARCH_LIBRARY,
        cacheable=True,
        bypass_reason="",
        fresh=fresh,
        user_scope=user_scope,
        cache_key=_sha256_canonical(cache_obj_v4),
        base_key=_sha256_canonical(base_obj_v4),
        req_limit=req_limit,
        date_from=date_from,
        date_to=date_to,
        upstream_arguments=upstream,
        legacy_cache_key=_sha256_canonical(cache_obj_v3),
        legacy_base_key=_sha256_canonical(base_obj_v3),
    )


def compute_ttl_seconds(
    date_to: str,
    *,
    result_count: int,
    today_str: str,
) -> int:
    """Compute cache TTL in seconds from `date_to` relative to Beijing `today_str`."""
    raw_end = str(date_to or "").strip()
    clean_end = normalize_date_to_day(raw_end)
    if raw_end and ("T" in raw_end or "+" in raw_end or raw_end.endswith("Z")):
        try:
            dt = datetime.fromisoformat(raw_end)
            if dt.tzinfo is not None:
                clean_end = dt.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        except ValueError:
            pass
    if not clean_end or clean_end >= today_str:
        return TTL_TODAY_EMPTY_S if result_count <= 0 else TTL_TODAY_NON_EMPTY_S
    try:
        today_d = date.fromisoformat(today_str)
    except ValueError:
        return TTL_TODAY_EMPTY_S if result_count <= 0 else TTL_TODAY_NON_EMPTY_S
    seven_days_ago = (today_d - timedelta(days=7)).isoformat()
    if seven_days_ago <= clean_end < today_str:
        return TTL_RECENT_7D_S
    return TTL_OLDER_S


def _locate_all_search_results_containers(
    payload: dict[str, Any], *, op: str
) -> list[tuple[dict[str, Any], str, dict[str, Any] | None]]:
    """Return every `(container, list_key, text_block)` from direct dicts and `content[0].text` JSON."""
    data_obj = payload.get("data")
    data_dict = data_obj if isinstance(data_obj, dict) else None
    sc_in_data = (
        data_dict.get("structuredContent")
        if data_dict and isinstance(data_dict.get("structuredContent"), dict)
        else None
    )
    top_sc = (
        payload.get("structuredContent")
        if isinstance(payload.get("structuredContent"), dict)
        else None
    )

    candidate_keys = ("transcripts", "results", "items") if op == OP_TRANSCRIPT else ("results",)
    direct_containers = (
        (sc_in_data, data_dict, top_sc, payload)
        if op == OP_TRANSCRIPT
        else (sc_in_data, top_sc, data_dict, payload)
    )
    matches: list[tuple[dict[str, Any], str, dict[str, Any] | None]] = []
    seen_ids: set[int] = set()
    for container in direct_containers:
        if not isinstance(container, dict) or id(container) in seen_ids:
            continue
        for key in candidate_keys:
            if isinstance(container.get(key), list):
                seen_ids.add(id(container))
                matches.append((container, key, None))
                break

    for parent in (data_dict, payload):
        if not isinstance(parent, dict):
            continue
        blocks = parent.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            try:
                parsed = json.loads(block["text"])
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            for key in candidate_keys:
                if isinstance(parsed.get(key), list):
                    matches.append((parsed, key, block))
                    break

    return matches


def _locate_search_results_containers(
    payload: dict[str, Any], *, op: str
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None]:
    """Return primary `(container, list_key, text_block)` from direct dicts or `content[0].text` JSON."""
    matches = _locate_all_search_results_containers(payload, op=op)
    return matches[0] if matches else (None, None, None)


STALE_CURSOR_NOTE = "缓存副本：结果未取完，但翻页游标已失效，不能续查；需要更多结果请稍后重新检索。"


def _cursor_slots(obj: Any):
    """Yield every ``(dict, "cursor")`` slot holding a cursor, at any depth.

    search_library keeps it in the results container; transcript keeps it in
    ``coverage.continuation_params``. JSON text blocks are re-serialized by callers."""
    if isinstance(obj, dict):
        if obj.get("cursor") not in (None, "", [], {}):
            yield obj
        for value in obj.values():
            yield from _cursor_slots(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _cursor_slots(value)


def payload_has_live_cursor(payload: dict[str, Any], *, op: str) -> bool:
    """Whether *payload* carries a page cursor: in any nested dict, or in a results
    container that only exists as a JSON text block."""
    if any(True for _ in _cursor_slots(payload)):
        return True
    return any(
        container.get("cursor") not in (None, "", [], {})
        for container, _, _ in _locate_all_search_results_containers(payload, op=op)
    )


def strip_cursor(payload: dict[str, Any], *, op: str) -> dict[str, Any]:
    """A copy of *payload* with every cursor removed and the paging hint replaced.

    A stale copy cannot page on: the MCP dropped its cursor. ``has_more`` is kept so
    the caller still knows the list is incomplete; the note says to search again."""
    out = copy.deepcopy(payload)
    for slot in list(_cursor_slots(out)):
        slot["cursor"] = None
        if "pagination_note" in slot or "has_more" in slot:
            slot["pagination_note"] = STALE_CURSOR_NOTE
    for container, _, text_block in _locate_all_search_results_containers(out, op=op):
        if "cursor" in container:
            container["cursor"] = None
            container["pagination_note"] = STALE_CURSOR_NOTE
        if text_block is not None:
            text_block["text"] = json.dumps(container, ensure_ascii=False)
    return out


def extract_result_metrics(payload: dict[str, Any], *, op: str) -> tuple[int, bool]:
    """Return `(result_count, has_more)` from an unwrapped successful payload."""
    container, list_key, _ = _locate_search_results_containers(payload, op=op)
    items = container.get(list_key) if container and list_key else []
    count = len(items) if isinstance(items, list) else 0

    if op == OP_TRANSCRIPT:
        # `coverage.complete` reflects scan completion, not whether the returned
        # item list is untruncated (`candidates[:limit]` happens downstream).
        for c in (container, payload):
            if isinstance(c, dict) and "has_more" in c:
                return count, bool(c.get("has_more"))
        return count, True

    has_more = False
    for c in (container, payload):
        if isinstance(c, dict) and "has_more" in c:
            has_more = bool(c.get("has_more"))
            break
    return count, has_more


def slice_cached_payload(
    payload: dict[str, Any],
    *,
    op: str,
    target_limit: int,
) -> dict[str, Any]:
    """Return a deep-copied payload sliced across all representations to `target_limit`."""
    out = copy.deepcopy(payload)
    _, orig_has_more = extract_result_metrics(out, op=op)
    matches = _locate_all_search_results_containers(out, op=op)
    if not matches:
        return out

    data_obj = out.get("data")
    data_dict = data_obj if isinstance(data_obj, dict) else None

    truncated_any = any(
        len(container.get(list_key) or []) > target_limit
        for container, list_key, _ in matches
    )
    eff_new_len = target_limit
    for container, list_key, text_block in matches:
        items = container.get(list_key)
        if not isinstance(items, list):
            continue
        truncated_this = len(items) > target_limit
        if truncated_this:
            container[list_key] = items[:target_limit]
        new_len = len(container[list_key])
        eff_new_len = new_len

        for count_k in ("returned", "count", "result_count"):
            if (
                count_k in container
                and isinstance(container[count_k], int)
                and not isinstance(container[count_k], bool)
            ):
                container[count_k] = new_len

        if op == OP_TRANSCRIPT:
            if truncated_any:
                if "has_more" in container:
                    container["has_more"] = True
                if "cursor" in container:
                    container["cursor"] = None
        else:
            effective_has_more = bool(truncated_any or orig_has_more)
            container["has_more"] = effective_has_more
            if truncated_any and "cursor" in container:
                # Never fabricate a cursor; stale end-of-larger-page cursor is cleared.
                container["cursor"] = None

        if text_block is not None:
            text_block["text"] = json.dumps(container, ensure_ascii=False)

    for outer in (d for d in (data_dict, out) if d is not None):
        for count_k in ("returned", "count", "result_count"):
            if (
                count_k in outer
                and isinstance(outer[count_k], int)
                and not isinstance(outer[count_k], bool)
            ):
                outer[count_k] = eff_new_len
        if op == OP_TRANSCRIPT:
            if truncated_any and "has_more" in outer:
                outer["has_more"] = True
        else:
            if "has_more" in outer:
                outer["has_more"] = bool(truncated_any or orig_has_more)
            if truncated_any and "cursor" in outer:
                outer["cursor"] = None

    return out


class AlphaEngineSearchCache:
    """SQLite-backed exact search cache for AlphaEngine `search_library` and `transcript`."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        cleanup_every_writes: int = CLEANUP_EVERY_WRITES,
        today_fn: Callable[[], str] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self._max_rows = max(1, int(max_rows))
        self._cleanup_every_writes = max(1, int(cleanup_every_writes))
        self._today_fn = today_fn or default_bridge_today
        self._now_fn = now_fn or time.time
        self._lock = threading.Lock()
        self._write_count = 0
        self._degraded_warned = False

    def _warn_degraded(self, action: str, exc: BaseException) -> None:
        if not self._degraded_warned:
            self._degraded_warned = True
            logger.warning(
                "AlphaEngine search cache degraded to passthrough (%s: %s)",
                action,
                type(exc).__name__,
            )

    def _chmod_secure(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            try:
                p = Path(f"{self.db_path}{suffix}")
                if p.exists():
                    os.chmod(p, 0o600)
            except OSError:
                pass

    def _open_conn(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ae_search_cache (
                cache_key TEXT PRIMARY KEY,
                base_key TEXT NOT NULL,
                op TEXT NOT NULL,
                user_scope TEXT NOT NULL,
                req_limit INTEGER,
                result_count INTEGER NOT NULL,
                has_more INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                retain_until REAL NOT NULL,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ae_cache_base_key ON ae_search_cache(base_key, expires_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ae_cache_retain ON ae_search_cache(retain_until)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ae_read_docs (
                doc_id TEXT PRIMARY KEY,
                first_read_at REAL NOT NULL,
                last_read_at REAL NOT NULL,
                read_count INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self._chmod_secure()
        return conn

    def _cleanup_conn(self, conn: sqlite3.Connection, now: float) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM ae_search_cache WHERE retain_until <= ?", (now,))
            row = conn.execute("SELECT COUNT(*) FROM ae_search_cache").fetchone()
            total = int(row[0]) if row else 0
            if total > self._max_rows:
                excess = total - self._max_rows
                conn.execute(
                    """
                    DELETE FROM ae_search_cache
                    WHERE cache_key IN (
                        SELECT cache_key FROM ae_search_cache
                        ORDER BY last_accessed_at ASC, fetched_at ASC
                        LIMIT ?
                    )
                    """,
                    (excess,),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def initialize_sync(self) -> bool:
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._open_conn()
                self._cleanup_conn(conn, self._now_fn())
                return True
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._warn_degraded("initialize", exc)
                return False
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    async def initialize(self) -> bool:
        return await asyncio.to_thread(self.initialize_sync)

    @staticmethod
    def _lookup_valid_row(
        conn: sqlite3.Connection,
        now: float,
        cache_key: str,
        base_key: str,
        req_limit: int | None,
        *,
        op: str,
    ) -> tuple[tuple[Any, ...] | None, bool, int | None, str]:
        """Look up exact or limit-sliced valid row for a single key version."""
        row = conn.execute(
            """
            SELECT payload_json, fetched_at, expires_at, req_limit, result_count,
                   retain_until, has_more, user_scope
            FROM ae_search_cache
            WHERE cache_key = ? AND expires_at > ?
            """,
            (cache_key, now),
        ).fetchone()
        if row is not None:
            return row, False, None, cache_key

        if req_limit is None or not base_key:
            return None, False, None, cache_key

        if op == OP_TRANSCRIPT:
            sliced_row = conn.execute(
                """
                SELECT cache_key, payload_json, fetched_at, expires_at, req_limit,
                       result_count, retain_until, has_more, user_scope
                FROM ae_search_cache
                WHERE base_key = ?
                  AND expires_at > ?
                  AND (
                    (req_limit IS NOT NULL AND req_limit >= ?)
                    OR (req_limit IS NULL AND result_count >= ?)
                  )
                ORDER BY
                  CASE WHEN req_limit IS NOT NULL AND req_limit >= ? THEN 0 ELSE 1 END,
                  COALESCE(req_limit, 0) ASC,
                  fetched_at DESC
                LIMIT 1
                """,
                (base_key, now, req_limit, req_limit, req_limit),
            ).fetchone()
        else:
            sliced_row = conn.execute(
                """
                SELECT cache_key, payload_json, fetched_at, expires_at, req_limit,
                       result_count, retain_until, has_more, user_scope
                FROM ae_search_cache
                WHERE base_key = ?
                  AND expires_at > ?
                  AND (
                    (req_limit IS NOT NULL AND req_limit >= ?)
                    OR has_more = 0
                    OR (req_limit IS NULL AND result_count >= ?)
                  )
                ORDER BY
                  CASE WHEN req_limit IS NOT NULL AND req_limit >= ? THEN 0 ELSE 1 END,
                  COALESCE(req_limit, 0) ASC,
                  fetched_at DESC
                LIMIT 1
                """,
                (base_key, now, req_limit, req_limit, req_limit),
            ).fetchone()

        if sliced_row is None:
            return None, False, None, cache_key

        hit_key = str(sliced_row[0])
        rest = sliced_row[1:]
        sliced_from = (
            int(rest[3])
            if rest[3] is not None
            else int(rest[4] if rest[4] is not None else 0)
        )
        return rest, True, sliced_from, hit_key

    def get_sync(
        self,
        cache_key: str,
        base_key: str,
        req_limit: int | None,
        *,
        op: str,
        legacy_cache_key: str = "",
        legacy_base_key: str = "",
    ) -> tuple[CacheHit | None, bool]:
        """Look up exact or limit-sliced cache hit (v4 first, v3 fallback with v4 write-back)."""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                if not self.db_path.exists():
                    return None, False
                conn = self._open_conn()
                now = self._now_fn()

                row, used_slice_fallback, sliced_from, hit_key = self._lookup_valid_row(
                    conn, now, cache_key, base_key, req_limit, op=op
                )
                migrated_from_v3 = False
                if row is None and legacy_cache_key and legacy_cache_key != cache_key:
                    row, used_slice_fallback, sliced_from, hit_key = self._lookup_valid_row(
                        conn,
                        now,
                        legacy_cache_key,
                        legacy_base_key,
                        req_limit,
                        op=op,
                    )
                    if row is not None:
                        migrated_from_v3 = True

                if row is None:
                    return None, False

                payload_raw = json.loads(row[0])
                if not isinstance(payload_raw, dict):
                    return None, False

                hit_note = ""
                if used_slice_fallback and req_limit is not None:
                    payload = slice_cached_payload(
                        payload_raw, op=op, target_limit=req_limit
                    )
                    hit_note = SLICED_CACHE_NOTE
                else:
                    payload = copy.deepcopy(payload_raw)

                # Judged on what would be returned: slicing already clears a large
                # page's cursor, so only a page that still carries one is refreshed.
                if now - float(row[1]) >= CURSOR_SAFE_AGE_S and payload_has_live_cursor(payload, op=op):
                    return None, False

                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        UPDATE ae_search_cache
                        SET hit_count = hit_count + 1, last_accessed_at = ?
                        WHERE cache_key = ?
                        """,
                        (now, hit_key),
                    )
                    if migrated_from_v3:
                        # Write a v4 copy preserving the original fetched_at/expires_at/retain_until.
                        mig_payload = payload if used_slice_fallback else payload_raw
                        mig_count, mig_has_more = extract_result_metrics(mig_payload, op=op)
                        mig_limit = (
                            req_limit
                            if used_slice_fallback
                            else (int(row[3]) if row[3] is not None else None)
                        )
                        mig_has_more_int = (
                            1
                            if (mig_has_more if used_slice_fallback else bool(row[6]))
                            else 0
                        )
                        mig_result_count = (
                            mig_count
                            if used_slice_fallback
                            else int(row[4] if row[4] is not None else mig_count)
                        )
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO ae_search_cache (
                                cache_key, base_key, op, user_scope, req_limit,
                                result_count, has_more, payload_json,
                                fetched_at, expires_at, retain_until,
                                hit_count, last_accessed_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                            """,
                            (
                                cache_key,
                                base_key,
                                op,
                                str(row[7] or "team"),
                                mig_limit,
                                mig_result_count,
                                mig_has_more_int,
                                json.dumps(mig_payload, ensure_ascii=False),
                                float(row[1]),
                                float(row[2]),
                                float(row[5]),
                                now,
                            ),
                        )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

                return (
                    CacheHit(
                        payload=payload,
                        fetched_at=float(row[1]),
                        expires_at=float(row[2]),
                        req_limit=int(row[3]) if row[3] is not None else None,
                        sliced_from_limit=sliced_from,
                        note=hit_note,
                    ),
                    False,
                )
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._warn_degraded("get", exc)
                return None, True
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    async def get(
        self,
        cache_key: str,
        base_key: str,
        req_limit: int | None,
        *,
        op: str,
        legacy_cache_key: str = "",
        legacy_base_key: str = "",
    ) -> tuple[CacheHit | None, bool]:
        return await asyncio.to_thread(
            self.get_sync,
            cache_key,
            base_key,
            req_limit,
            op=op,
            legacy_cache_key=legacy_cache_key,
            legacy_base_key=legacy_base_key,
        )

    def get_stale_sync(
        self,
        cache_key: str,
        *,
        legacy_cache_key: str = "",
        stale_reason: str = "quota",
    ) -> CacheHit | None:
        """Look up a same-key row within the 7-day retention window for failure/breaker fallback."""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                if not self.db_path.exists():
                    return None
                conn = self._open_conn()
                now = self._now_fn()
                keys_to_try = [cache_key]
                if legacy_cache_key and legacy_cache_key != cache_key:
                    keys_to_try.append(legacy_cache_key)
                row = None
                for k in keys_to_try:
                    row = conn.execute(
                        """
                        SELECT payload_json, fetched_at, expires_at, req_limit
                        FROM ae_search_cache
                        WHERE cache_key = ? AND retain_until > ?
                        """,
                        (k, now),
                    ).fetchone()
                    if row is not None:
                        break
                if row is None:
                    return None
                payload_raw = json.loads(row[0])
                if not isinstance(payload_raw, dict):
                    return None
                return CacheHit(
                    payload=copy.deepcopy(payload_raw),
                    fetched_at=float(row[1]),
                    expires_at=float(row[2]),
                    req_limit=int(row[3]) if row[3] is not None else None,
                    stale=True,
                    stale_reason=stale_reason,
                )
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._warn_degraded("get_stale", exc)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    async def get_stale(
        self,
        cache_key: str,
        *,
        legacy_cache_key: str = "",
        stale_reason: str = "quota",
    ) -> CacheHit | None:
        return await asyncio.to_thread(
            self.get_stale_sync,
            cache_key,
            legacy_cache_key=legacy_cache_key,
            stale_reason=stale_reason,
        )

    def put_sync(
        self,
        *,
        cache_key: str,
        base_key: str,
        op: str,
        user_scope: str,
        req_limit: int | None,
        result_count: int,
        has_more: bool,
        payload: dict[str, Any],
        date_to: str,
    ) -> tuple[float, float] | None:
        """Persist a successful search payload. Returns `(fetched_at, expires_at)` or None on DB failure."""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._open_conn()
                now = self._now_fn()
                today_str = self._today_fn()
                ttl = compute_ttl_seconds(
                    date_to, result_count=result_count, today_str=today_str
                )
                fetched_at = now
                expires_at = now + ttl
                retain_until = expires_at + STALE_RETENTION_S
                payload_json = json.dumps(payload, ensure_ascii=False)

                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO ae_search_cache (
                            cache_key, base_key, op, user_scope, req_limit,
                            result_count, has_more, payload_json,
                            fetched_at, expires_at, retain_until,
                            hit_count, last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                        """,
                        (
                            cache_key,
                            base_key,
                            op,
                            user_scope,
                            req_limit,
                            int(result_count),
                            1 if has_more else 0,
                            payload_json,
                            fetched_at,
                            expires_at,
                            retain_until,
                            now,
                        ),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

                self._write_count += 1
                if self._write_count % self._cleanup_every_writes == 0:
                    self._cleanup_conn(conn, now)
                self._chmod_secure()
                return fetched_at, expires_at
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._warn_degraded("put", exc)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    async def put(
        self,
        *,
        cache_key: str,
        base_key: str,
        op: str,
        user_scope: str,
        req_limit: int | None,
        result_count: int,
        has_more: bool,
        payload: dict[str, Any],
        date_to: str,
    ) -> tuple[float, float] | None:
        return await asyncio.to_thread(
            self.put_sync,
            cache_key=cache_key,
            base_key=base_key,
            op=op,
            user_scope=user_scope,
            req_limit=req_limit,
            result_count=result_count,
            has_more=has_more,
            payload=payload,
            date_to=date_to,
        )

    def record_doc_read_sync(self, doc_id: str) -> bool:
        """Record a successful `get_document` read; return True if this is the first open."""
        clean_id = str(doc_id or "").strip()
        if not clean_id:
            return True
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._open_conn()
                now = self._now_fn()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    row = conn.execute(
                        "SELECT first_read_at FROM ae_read_docs WHERE doc_id = ?",
                        (clean_id,),
                    ).fetchone()
                    if row is None:
                        conn.execute(
                            """
                            INSERT INTO ae_read_docs (doc_id, first_read_at, last_read_at, read_count)
                            VALUES (?, ?, ?, 1)
                            """,
                            (clean_id, now, now),
                        )
                        is_first = True
                    else:
                        conn.execute(
                            """
                            UPDATE ae_read_docs
                            SET last_read_at = ?, read_count = read_count + 1
                            WHERE doc_id = ?
                            """,
                            (now, clean_id),
                        )
                        is_first = False
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                self._chmod_secure()
                return is_first
            except (sqlite3.Error, OSError, ValueError) as exc:
                self._warn_degraded("record_doc_read", exc)
                return True
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass

    async def record_doc_read(self, doc_id: str) -> bool:
        return await asyncio.to_thread(self.record_doc_read_sync, doc_id)


class AlphaEngineSearchCircuitBreaker:
    """In-process circuit breaker for AlphaEngine search (`search_library` / `transcript`).

    `researchdata_bridge.py` runs as a single process (`asyncio` event loop), so
    an in-memory breaker has no multi-worker split-brain issues. `get_document`
    (read) uses an independent upstream quota and is unaffected by this breaker.
    """

    def __init__(
        self,
        *,
        initial_s: float = AE_BREAKER_INITIAL_S,
        max_s: float = AE_BREAKER_MAX_S,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._initial_s = max(1.0, float(initial_s))
        self._max_s = max(self._initial_s, float(max_s))
        self._now_fn = now_fn or time.monotonic
        self._open_until: float = 0.0
        self._consecutive_trips: int = 0
        self._trip_count: int = 0
        self._last_reason: str = ""

    def is_open(self) -> bool:
        return self._now_fn() < self._open_until

    def remaining_seconds(self) -> float:
        return max(0.0, self._open_until - self._now_fn())

    @property
    def last_reason(self) -> str:
        return self._last_reason if self.is_open() else ""

    def record_quota_failure(self, reason: str = "") -> tuple[bool, float]:
        """Open (or keep open) the breaker on a quota/rate-limit error; doubles backoff on consecutive trips."""
        now = self._now_fn()
        cleaned_reason = re.sub(r"\s+", " ", str(reason or "").strip())[:160]
        if now < self._open_until:
            # Already open (e.g. an in-flight sibling finishing after trip); do not compound one burst.
            if cleaned_reason and not self._last_reason:
                self._last_reason = cleaned_reason
            return False, self._open_until - now
        self._consecutive_trips += 1
        self._trip_count += 1
        duration = min(
            self._max_s,
            self._initial_s * (2 ** (self._consecutive_trips - 1)),
        )
        self._open_until = now + duration
        self._last_reason = cleaned_reason or "额度或频率受限"
        return True, duration

    def record_success(self) -> None:
        """Reset consecutive backoff after an upstream search succeeds."""
        self._consecutive_trips = 0
        self._open_until = 0.0
        self._last_reason = ""

    def snapshot(self) -> dict[str, Any]:
        rem = self.remaining_seconds()
        snap = {
            "open": rem > 0.0,
            "remaining_seconds": round(rem, 1),
            "trip_count": self._trip_count,
            "consecutive_trips": self._consecutive_trips,
        }
        if rem > 0.0 and self._last_reason:
            snap["last_reason"] = self._last_reason
        return snap


# ---------------------------------------------------------------------------
# Coverage-list company recognition & in-memory session soft reminder (30m)
# ---------------------------------------------------------------------------

COMPANY_SEARCH_WINDOW_S = 30 * 60.0
QUERY_LOG_RETENTION_DAYS = 7

# Core aliases for `watchlist.txt` / `watchlist_cn_hk.txt` plus dual-listed links.
_CORE_WATCHLIST_ALIASES: dict[str, tuple[str, ...]] = {
    "AFRM": ("AFRM", "Affirm"),
    "ECHO": ("ECHO", "EchoStar"),
    "BABA": ("BABA", "9988.HK", "09988", "Alibaba", "Alibaba Group", "阿里巴巴", "阿里"),
    "ATAT": ("ATAT", "Atour", "Atour Lifestyle", "亚朵"),
    "PDD": ("PDD", "PDD Holdings", "拼多多"),
    "EDU": ("EDU", "New Oriental", "新东方"),
    "TCOM": ("TCOM", "9961.HK", "Trip.com", "携程"),
    "HTHT": ("HTHT", "H World", "H World Group", "华住"),
    "FUTU": ("FUTU", "Futu", "富途"),
    "TAL": ("TAL", "TAL Education", "好未来"),
    "GOOGL": ("GOOGL", "GOOG", "Alphabet", "Google", "谷歌"),
    "MSFT": ("MSFT", "Microsoft", "微软"),
    "META": ("META", "Meta Platforms", "Facebook"),
    "AMZN": ("AMZN", "Amazon", "亚马逊"),
    "NVDA": ("NVDA", "NVIDIA", "Nvidia", "英伟达"),
    "AVGO": ("AVGO", "Broadcom", "博通"),
    "TSM": ("TSM", "TSMC", "Taiwan Semiconductor", "台积电"),
    "AMD": ("AMD", "Advanced Micro Devices", "超威"),
    "INTC": ("INTC", "Intel", "英特尔"),
    "ASML": ("ASML", "阿斯麦"),
    "ACMR": ("ACMR", "ACM Research", "盛美"),
    "MU": ("MU", "Micron", "Micron Technology", "美光"),
    "NBIS": ("NBIS", "Nebius", "Nebius Group"),
    "LITE": ("LITE", "Lumentum"),
    "COHR": ("COHR", "Coherent"),
    "NOK": ("NOK", "Nokia", "诺基亚"),
    "DELL": ("DELL", "Dell", "Dell Technologies", "戴尔"),
    "SNDK": ("SNDK", "SanDisk", "Sandisk", "闪迪"),
    "DASH": ("DASH", "DoorDash"),
    "NFLX": ("NFLX", "Netflix", "奈飞"),
    "SPOT": ("SPOT", "Spotify"),
    "APP": ("APP", "AppLovin", "Applovin"),
    "RBLX": ("RBLX", "Roblox"),
    "PGR": ("PGR", "Progressive"),
    "CRWV": ("CRWV", "CoreWeave"),
    "IBKR": ("IBKR", "Interactive Brokers", "盈透", "盈透证券"),
    "HOOD": ("HOOD", "Robinhood"),
    "UBER": ("UBER", "Uber Technologies", "优步"),
    "CART": ("CART", "Instacart", "Maplebear"),
    "BKNG": ("BKNG", "Booking", "Booking Holdings"),
    "RDDT": ("RDDT", "Reddit"),
    "TSLA": ("TSLA", "Tesla", "特斯拉"),
    "KSPI": ("KSPI", "Kaspi", "Kaspi.kz"),
    "V": ("V", "Visa", "维萨"),
    "MA": ("MA", "Mastercard", "万事达"),
    "COST": ("COST", "Costco", "开市客"),
    "ISRG": ("ISRG", "Intuitive Surgical", "直觉外科"),
    "IDXX": ("IDXX", "IDEXX", "IDEXX Laboratories"),
    "CDNS": ("CDNS", "Cadence", "Cadence Design"),
    "MNST": ("MNST", "Monster Beverage"),
    "MSCI": ("MSCI",),
    "GE": ("GE", "GE Aerospace", "General Electric", "通用电气"),
    "SHOP": ("SHOP", "Shopify"),
    "TW": ("TW", "Tradeweb"),
    "SAP": ("SAP", "思爱普"),
    "HEI": ("HEI", "HEICO"),
    "WSE": ("WSE", "GPW"),
    "BAM": ("BAM", "Brookfield Asset Management", "博枫"),
    "300308": ("300308", "300308.SZ", "中际旭创", "Innolight"),
    "300502": ("300502", "300502.SZ", "新易盛", "Eoptolink"),
    "300750": ("300750", "300750.SZ", "宁德时代", "CATL"),
    "9992.HK": ("9992.HK", "09992", "泡泡玛特", "Pop Mart"),
    "0700.HK": ("0700.HK", "00700", "700.HK", "腾讯控股", "腾讯", "Tencent", "Tencent Holdings"),
    "3690.HK": ("3690.HK", "03690", "美团", "Meituan"),
    "0788.HK": ("0788.HK", "00788", "中国铁塔", "China Tower"),
}

_HK_TICKER_RE = re.compile(r"^0*(\d{1,5})\.HK$", re.IGNORECASE)
_CN_TICKER_RE = re.compile(r"^(\d{6})(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_ASCII_TOKEN_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_SHORT_UPPER_TICKERS: frozenset[str] = frozenset(
    {
        "V",
        "MA",
        "GE",
        "MU",
        "SE",
        "TW",
        "APP",
        "COST",
        "SAP",
        "SHOP",
        "ECHO",
        "LITE",
        "DASH",
        "CART",
        "HOOD",
        "SPOT",
        "BAM",
        "HEI",
        "TAL",
        "EDU",
        "NOK",
    }
)
_EXPLICIT_TICKER_SUFFIXES = (
    r"(?:\.(?:O|OQ|N|A|K|PK|OB|US|HK|SS|SH|SZ|BJ|TO|L|DE|PA|AS|MI|MC|SW|AX|SI|KS|TW|TWO)"
    r"|\s+(?:US|HK|SH|SZ|BJ|JP|UK|Equity|Stock))"
)
# Common English words or acronyms that collide with tickers (coverage lists + standard English
# terms): require either another company name/alias in the query OR explicit ticker syntax
# (`$APP`, `APP US`, `APP.O`, etc.). Pure-ASCII tickers are always matched case-sensitively.
_AMBIGUOUS_BARE_TICKERS: frozenset[str] = frozenset(
    {
        # Explicit review list (minimum required)
        "APP",
        "AI",
        "ON",
        "NOW",
        "ALL",
        "CAN",
        "IT",
        "META",
        "UBER",
        "SNOW",
        "PATH",
        "CARS",
        "FAST",
        "REAL",
        "LOVE",
        "OPEN",
        "PLAY",
        "TEAM",
        "BOX",
        "LIFE",
        "ARM",
        "FUN",
        # Single-letter and common English words / short acronyms from watchlist & universe lists
        "A",
        "H",
        "S",
        "U",
        "W",
        "V",
        "MA",
        "GE",
        "MU",
        "SE",
        "TW",
        "AG",
        "CL",
        "DG",
        "EL",
        "KO",
        "MO",
        "PG",
        "PM",
        "RH",
        "SO",
        "OR",
        "API",
        "IOT",
        "DNA",
        "EAT",
        "GEN",
        "GAP",
        "LOW",
        "FIG",
        "NET",
        "PAY",
        "WORK",
        "TRUE",
        "TECH",
        "CORE",
        "DATA",
        "GOOD",
        "HIGH",
        "BEST",
        "MIND",
        "MORE",
        "NEXT",
        "PLAN",
        "PLUS",
        "RISK",
        "SAFE",
        "SAVE",
        "STEP",
        "TIME",
        "UNIT",
        "VIEW",
        "WAY",
        "WIN",
        "WING",
        "DAO",
        "BILL",
        "COST",
        "DASH",
        "CART",
        "SPOT",
        "SHOP",
        "LITE",
        "ECHO",
        "HOOD",
        "NICE",
        "POST",
        "TRIP",
        "SAM",
        "MAN",
        "SAP",
        "BAM",
        "TAL",
        "EDU",
        "NOK",
    }
)

_COMPANY_INDEX_LOCK = threading.Lock()
_CACHED_COMPANY_INDEX: list[tuple[str, str, re.Pattern[str] | None]] | None = None


def _strip_corp_suffix(name: str) -> str:
    cleaned = re.sub(
        r"\s+(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|Limited|Co\.?,?\s*Ltd\.?|Holdings|Group|PLC|N\.V\.|S\.A\.)$",
        "",
        name.strip(),
        flags=re.IGNORECASE,
    ).strip()
    return cleaned


def _add_alias(
    alias_to_canon: dict[str, tuple[str, str, bool]],
    canon: str,
    raw_alias: str,
    *,
    is_ticker: bool = False,
) -> None:
    alias = normalize_search_query(raw_alias)
    if not alias or len(alias) < 1:
        return
    is_ascii = bool(_ASCII_TOKEN_RE.fullmatch(alias))
    if not is_ticker and is_ascii and alias.upper() in _AMBIGUOUS_BARE_TICKERS:
        # Never register an ambiguous English word (e.g. stripped "Box" or "Gap") as a bare company name.
        return
    if is_ticker and is_ascii:
        alias = alias.upper()
        key = f"T:{alias}"
    else:
        key = f"N:{alias.upper()}" if (is_ascii and alias.isupper()) else f"N:{alias.lower()}"
    if key not in alias_to_canon:
        alias_to_canon[key] = (canon, alias, is_ticker)


def _load_company_index() -> list[tuple[str, str, re.Pattern[str] | None]]:
    """Build sorted `(alias_display, canonical_id, compiled_regex_or_none)` from watchlists & universe JSONs."""
    global _CACHED_COMPANY_INDEX
    with _COMPANY_INDEX_LOCK:
        if _CACHED_COMPANY_INDEX is not None:
            return _CACHED_COMPANY_INDEX

        alias_to_canon: dict[str, tuple[str, str, bool]] = {}
        for canon, aliases in _CORE_WATCHLIST_ALIASES.items():
            for idx, a in enumerate(aliases):
                is_t = idx == 0 or bool(_HK_TICKER_RE.fullmatch(a) or _CN_TICKER_RE.fullmatch(a))
                _add_alias(alias_to_canon, canon, a, is_ticker=is_t)

        skills_dir = Path(__file__).resolve().parents[2] / "plugins" / "dalton" / "skills"
        wl_us = skills_dir / "dalton-daily-filing-alert" / "watchlist.txt"
        if wl_us.is_file():
            for line in wl_us.read_text(encoding="utf-8", errors="ignore").splitlines():
                tok = line.strip()
                if not tok or tok.startswith("#"):
                    continue
                _add_alias(alias_to_canon, tok.upper(), tok.upper(), is_ticker=True)

        wl_cn_hk = skills_dir / "dalton-daily-filing-alert" / "watchlist_cn_hk.txt"
        if wl_cn_hk.is_file():
            for line in wl_cn_hk.read_text(encoding="utf-8", errors="ignore").splitlines():
                tok = line.strip()
                if not tok or tok.startswith("#"):
                    continue
                parts = tok.split(None, 1)
                code = parts[0].strip().upper()
                if not code:
                    continue
                canon = code
                _add_alias(alias_to_canon, canon, code, is_ticker=True)
                hk_m = _HK_TICKER_RE.fullmatch(code)
                if hk_m:
                    _add_alias(alias_to_canon, canon, hk_m.group(1).zfill(5), is_ticker=True)
                if len(parts) > 1 and parts[1].strip():
                    _add_alias(alias_to_canon, canon, parts[1].strip(), is_ticker=False)

        if skills_dir.is_dir():
            for uni_path in sorted(skills_dir.glob("*/references/universe*.json")):
                try:
                    data = json.loads(uni_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                items = (
                    data.get("universe")
                    if isinstance(data, dict) and isinstance(data.get("universe"), list)
                    else (
                        data.get("companies")
                        if isinstance(data, dict) and isinstance(data.get("companies"), list)
                        else []
                    )
                )
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    raw_ticker = str(item.get("ticker") or "").strip().upper()
                    if not raw_ticker:
                        continue
                    canon = raw_ticker
                    # Map dual-listed tickers to existing canonical ticker if already registered
                    t_key = f"T:{raw_ticker}"
                    if t_key in alias_to_canon:
                        canon = alias_to_canon[t_key][0]
                    _add_alias(alias_to_canon, canon, raw_ticker, is_ticker=True)
                    cn_m = _CN_TICKER_RE.fullmatch(raw_ticker)
                    if cn_m:
                        _add_alias(alias_to_canon, canon, cn_m.group(1), is_ticker=True)
                    hk_m = _HK_TICKER_RE.fullmatch(raw_ticker)
                    if hk_m:
                        _add_alias(alias_to_canon, canon, hk_m.group(1).zfill(5), is_ticker=True)
                    for fld in ("name", "name_en", "name_zh", "ae_query"):
                        val = str(item.get(fld) or "").strip()
                        if not val or len(val) < 2:
                            continue
                        _add_alias(alias_to_canon, canon, val, is_ticker=False)
                        stripped = _strip_corp_suffix(val)
                        if stripped and len(stripped) >= 2 and stripped != val:
                            _add_alias(alias_to_canon, canon, stripped, is_ticker=False)

        compiled: list[tuple[str, str, re.Pattern[str] | None]] = []
        # Sort non-ambiguous / longest aliases first so specific company names match before short tickers.
        def _sort_key(kv: tuple[str, tuple[str, str, bool]]) -> tuple[int, int, str]:
            _, alias_s, is_t = kv[1]
            is_ambig = 1 if (is_t and alias_s.upper() in _AMBIGUOUS_BARE_TICKERS) else 0
            return (is_ambig, -len(alias_s), alias_s)

        for _, (canon, alias, is_t) in sorted(alias_to_canon.items(), key=_sort_key):
            if _ASCII_TOKEN_RE.fullmatch(alias):
                if is_t and alias.upper() in _AMBIGUOUS_BARE_TICKERS:
                    esc = re.escape(alias.upper())
                    pat = re.compile(
                        rf"(?<![A-Za-z0-9$])\${esc}(?![A-Za-z0-9])"
                        rf"|(?<![A-Za-z0-9$]){esc}{_EXPLICIT_TICKER_SUFFIXES}(?![A-Za-z0-9])",
                        flags=0,
                    )
                elif is_t or alias.isupper():
                    # Pure-ASCII tickers (and all-caps acronyms) match case-sensitively (uppercase only).
                    pat = re.compile(
                        rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                        flags=0,
                    )
                else:
                    pat = re.compile(
                        rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                        flags=re.IGNORECASE,
                    )
                compiled.append((alias, canon, pat))
            else:
                compiled.append((alias, canon, None))

        _CACHED_COMPANY_INDEX = compiled
        return compiled


def extract_companies_from_search(
    query: Any = "",
    keywords: Any = None,
    ticker: Any = "",
) -> list[str]:
    """Return canonical company identifiers recognized in `query`, `keywords`, or `ticker`."""
    parts: list[str] = []
    q_norm = normalize_search_query(query)
    if q_norm:
        parts.append(q_norm)
    if isinstance(keywords, list):
        for item in keywords:
            k_norm = normalize_search_query(item)
            if k_norm:
                parts.append(k_norm)
    elif isinstance(keywords, str) and keywords.strip():
        parts.append(normalize_search_query(keywords))
    t_norm = normalize_search_query(ticker).upper()
    if t_norm:
        parts.append(f"${t_norm}" if _ASCII_TOKEN_RE.fullmatch(t_norm) and "." not in t_norm else t_norm)

    if not parts:
        return []

    combined = " | ".join(parts)
    combined_lower = combined.lower()
    masked = [False] * len(combined)
    found: list[str] = []
    seen_canon: set[str] = set()

    for alias, canon, pat in _load_company_index():
        if pat is not None:
            for m in pat.finditer(combined):
                s, e = m.span()
                if any(masked[s:e]):
                    continue
                for idx in range(s, e):
                    masked[idx] = True
                if canon not in seen_canon:
                    seen_canon.add(canon)
                    found.append(canon)
        else:
            alias_l = alias.lower()
            start = 0
            while True:
                pos = combined_lower.find(alias_l, start)
                if pos < 0:
                    break
                end = pos + len(alias_l)
                if not any(masked[pos:end]):
                    for idx in range(pos, end):
                        masked[idx] = True
                    if canon not in seen_canon:
                        seen_canon.add(canon)
                        found.append(canon)
                start = end

    return found


class AlphaEngineCompanySearchTracker:
    """In-memory per-session company search counter with 30-minute sliding TTL.

    Tracks distinct query signatures per `(session_key, company_id)`. On the 3rd+
    distinct-keyword search for the same company within 30 minutes, emits a soft
    `advice` message without blocking or delaying the search.
    """

    def __init__(
        self,
        *,
        window_s: float = COMPANY_SEARCH_WINDOW_S,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._window_s = max(1.0, float(window_s))
        self._now_fn = now_fn or time.monotonic
        self._records: dict[tuple[str, str], list[tuple[float, str]]] = {}

    @staticmethod
    def resolve_session_key(caller_identity: dict[str, Any] | None) -> str:
        if not isinstance(caller_identity, dict):
            return ""
        thread = str(caller_identity.get("thread") or "").strip()
        if thread:
            return f"thread:{thread}"
        workspace = str(caller_identity.get("workspace") or "").strip()
        if workspace:
            return f"workspace:{workspace}"
        return ""

    @staticmethod
    def build_query_signature(query: Any, keywords: Any = None) -> str:
        q_v4 = normalize_search_query_v4(query)
        kw_parts: list[str] = []
        if isinstance(keywords, list):
            for item in keywords:
                k_v4 = normalize_search_query_v4(item)
                if k_v4:
                    kw_parts.append(k_v4)
        elif isinstance(keywords, str) and keywords.strip():
            kw_parts.append(normalize_search_query_v4(keywords))
        kw_sorted = ",".join(sorted(set(kw_parts)))
        return f"{q_v4}||{kw_sorted}"

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_s
        empty_keys: list[tuple[str, str]] = []
        for k, entries in self._records.items():
            kept = [(ts, sig) for ts, sig in entries if ts > cutoff]
            if kept:
                self._records[k] = kept
            else:
                empty_keys.append(k)
        for k in empty_keys:
            self._records.pop(k, None)

    def record_and_check(
        self,
        *,
        caller_identity: dict[str, Any] | None,
        query: Any,
        keywords: Any = None,
        ticker: Any = "",
    ) -> tuple[list[str], str | None]:
        """Record recognized company searches and return `(companies, advice_or_none)`."""
        companies = extract_companies_from_search(query, keywords, ticker)
        if not companies:
            return [], None
        session_key = self.resolve_session_key(caller_identity)
        if not session_key:
            return companies, None

        now = self._now_fn()
        self._prune(now)
        sig = self.build_query_signature(query, keywords)
        if sig == "||":
            return companies, None

        max_distinct = 0
        triggered = False
        for comp in companies:
            key = (session_key, comp)
            entries = self._records.setdefault(key, [])
            prior_sigs = {s for _, s in entries}
            is_new_keyword = sig not in prior_sigs
            entries.append((now, sig))
            distinct_now = len(prior_sigs | {sig})
            if is_new_keyword and distinct_now >= 3:
                triggered = True
                if distinct_now > max_distinct:
                    max_distinct = distinct_now

        if not triggered:
            return companies, None
        advice = (
            f"请先从现有结果中挑选（本会话已检索这家公司 {max_distinct} 次）；"
            "需要新主题时再检索"
        )
        return companies, advice


# ---------------------------------------------------------------------------
# Team-scope search query & params text logger (0600, 7-day daily rotation)
# ---------------------------------------------------------------------------

_QUERY_LOG_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


class AlphaEngineTeamQueryLogger:
    """Daily-rotated JSONL logger for team-scope AlphaEngine search query texts.

    Excludes personal-scope queries (`_is_personal_scope_request`), enforces `0600`
    file permissions and `0700` directory permissions, and automatically deletes
    daily log files older than 7 days.
    """

    def __init__(
        self,
        log_dir: Path | str,
        *,
        retention_days: int = QUERY_LOG_RETENTION_DAYS,
        today_fn: Callable[[], str] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.log_dir = Path(log_dir).expanduser()
        self._retention_days = max(1, int(retention_days))
        self._today_fn = today_fn or default_bridge_today
        self._now_fn = now_fn or time.time
        self._lock = threading.Lock()

    def _cleanup_old_files(self, today_str: str) -> None:
        try:
            today_d = date.fromisoformat(today_str)
        except ValueError:
            return
        cutoff = today_d - timedelta(days=self._retention_days)
        if not self.log_dir.is_dir():
            return
        for child in self.log_dir.iterdir():
            m = _QUERY_LOG_FILENAME_RE.fullmatch(child.name)
            if not m:
                continue
            try:
                file_d = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if file_d < cutoff:
                try:
                    child.unlink(missing_ok=True)
                except OSError:
                    pass

    def log_sync(self, entry: dict[str, Any]) -> bool:
        with self._lock:
            try:
                today_str = str(entry.get("day") or self._today_fn()).strip()
                payload = dict(entry)
                payload["day"] = today_str
                self.log_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self.log_dir, 0o700)
                except OSError:
                    pass
                log_file = self.log_dir / f"{today_str}.jsonl"
                line = json.dumps(payload, ensure_ascii=False) + "\n"
                fd = os.open(
                    str(log_file),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    0o600,
                )
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
                try:
                    os.chmod(log_file, 0o600)
                except OSError:
                    pass
                self._cleanup_old_files(today_str)
                return True
            except OSError as exc:
                logger.warning("Failed to write alphaengine query log: %s", type(exc).__name__)
                return False

    async def log(self, entry: dict[str, Any]) -> bool:
        return await asyncio.to_thread(self.log_sync, entry)
