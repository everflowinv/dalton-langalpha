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

CACHE_KEY_VERSION = 3
OP_SEARCH_LIBRARY = "alphaengine.search_library"
OP_TRANSCRIPT = "transcript"
DEFAULT_SEARCH_LIMIT = 3
SLICED_CACHE_NOTE = "结果由更大条数的缓存截取；需要更多结果请提高 limit 重新检索"

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


def _normalize_filter_value(key: str, val: Any) -> Any:
    if isinstance(val, list):
        items = [_normalize_generic_value(item) for item in val if item is not None]
        if key in _FILTER_CODE_ARRAY_KEYS:
            dedup: dict[str, Any] = {}
            for item in items:
                serialized = json.dumps(
                    item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
                dedup.setdefault(serialized, item)
            return [dedup[k] for k in sorted(dedup.keys())]
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
        if norm_query:
            upstream["query"] = norm_query
        else:
            upstream.pop("query", None)
        date_from = str(upstream.get("start") or "").strip()
        date_to = str(upstream.get("end") or "").strip()
        req_limit = _parse_positive_int(upstream.get("limit"))
        if req_limit is not None:
            upstream["limit"] = req_limit
        user_scope = "team"
        base_obj = {
            "v": CACHE_KEY_VERSION,
            "op": OP_TRANSCRIPT,
            "scope": user_scope,
            "params": {
                "mode": eff_mode,
                "ticker": ticker,
                "query": norm_query,
                "date_from": date_from,
                "date_to": date_to,
            },
        }
        cache_obj = {**base_obj, "limit": req_limit}
        return SearchCachePlan(
            op=OP_TRANSCRIPT,
            cacheable=True,
            bypass_reason="",
            fresh=fresh,
            user_scope=user_scope,
            cache_key=_sha256_canonical(cache_obj),
            base_key=_sha256_canonical(base_obj),
            req_limit=req_limit,
            date_from=date_from,
            date_to=date_to,
            upstream_arguments=upstream,
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
    if norm_query:
        upstream["query"] = norm_query
    else:
        upstream.pop("query", None)

    date_from = str(
        upstream.get("start")
        or params_dict.get("date_from")
        or params_dict.get("start_date")
        or params_dict.get("start")
        or ""
    ).strip()
    date_to = str(
        upstream.get("end")
        or params_dict.get("date_to")
        or params_dict.get("end_date")
        or params_dict.get("end")
        or ""
    ).strip()
    for _dk in ("date_from", "start_date", "start"):
        params_dict.pop(_dk, None)
    for _dk in ("date_to", "end_date", "end"):
        params_dict.pop(_dk, None)
    if date_from:
        upstream["start"] = date_from
    if date_to:
        upstream["end"] = date_to

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

    canonical_filters: dict[str, Any] = {}
    normalized_params_out: dict[str, Any] = {}
    for k in sorted(params_dict.keys()):
        if k in _PROMOTED_OR_STRIPPED_NESTED_KEYS:
            continue
        v = params_dict[k]
        if v is None:
            continue
        norm_v = _normalize_filter_value(k, v)
        canonical_filters[k] = norm_v
        normalized_params_out[k] = copy.deepcopy(norm_v)
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
    base_obj = {
        "v": CACHE_KEY_VERSION,
        "op": OP_SEARCH_LIBRARY,
        "scope": user_scope,
        "params": {
            "mode": eff_mode,
            "ticker": norm_ticker,
            "query": norm_query,
            "date_from": date_from,
            "date_to": date_to,
            "sort": eff_sort,
            "filters": canonical_filters,
        },
    }
    cache_obj = {**base_obj, "limit": req_limit}

    return SearchCachePlan(
        op=OP_SEARCH_LIBRARY,
        cacheable=True,
        bypass_reason="",
        fresh=fresh,
        user_scope=user_scope,
        cache_key=_sha256_canonical(cache_obj),
        base_key=_sha256_canonical(base_obj),
        req_limit=req_limit,
        date_from=date_from,
        date_to=date_to,
        upstream_arguments=upstream,
    )


def compute_ttl_seconds(
    date_to: str,
    *,
    result_count: int,
    today_str: str,
) -> int:
    """Compute cache TTL in seconds from `date_to` relative to Beijing `today_str`."""
    clean_end = str(date_to or "").strip()
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

    def get_sync(
        self,
        cache_key: str,
        base_key: str,
        req_limit: int | None,
        *,
        op: str,
    ) -> tuple[CacheHit | None, bool]:
        """Look up exact or limit-sliced cache hit. Returns `(hit_or_none, degraded)`."""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                if not self.db_path.exists():
                    return None, False
                conn = self._open_conn()
                now = self._now_fn()

                row = conn.execute(
                    """
                    SELECT payload_json, fetched_at, expires_at, req_limit, result_count
                    FROM ae_search_cache
                    WHERE cache_key = ? AND expires_at > ?
                    """,
                    (cache_key, now),
                ).fetchone()
                used_slice_fallback = False
                sliced_from: int | None = None
                hit_key = cache_key

                if row is None and req_limit is not None:
                    if op == OP_TRANSCRIPT:
                        sliced_row = conn.execute(
                            """
                            SELECT cache_key, payload_json, fetched_at, expires_at, req_limit, result_count
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
                            SELECT cache_key, payload_json, fetched_at, expires_at, req_limit, result_count
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
                    if sliced_row is not None:
                        used_slice_fallback = True
                        hit_key = str(sliced_row[0])
                        row = sliced_row[1:]
                        sliced_from = (
                            int(row[3])
                            if row[3] is not None
                            else int(row[4] if row[4] is not None else 0)
                        )

                if row is None:
                    return None, False

                payload_raw = json.loads(row[0])
                if not isinstance(payload_raw, dict):
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
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

                hit_note = ""
                if used_slice_fallback and req_limit is not None:
                    payload = slice_cached_payload(
                        payload_raw, op=op, target_limit=req_limit
                    )
                    hit_note = SLICED_CACHE_NOTE
                else:
                    payload = copy.deepcopy(payload_raw)

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
    ) -> tuple[CacheHit | None, bool]:
        return await asyncio.to_thread(
            self.get_sync, cache_key, base_key, req_limit, op=op
        )

    def get_stale_sync(self, cache_key: str) -> CacheHit | None:
        """Look up an expired row within the 7-day retention window for quota fallback."""
        with self._lock:
            conn: sqlite3.Connection | None = None
            try:
                conn = self._open_conn()
                now = self._now_fn()
                row = conn.execute(
                    """
                    SELECT payload_json, fetched_at, expires_at, req_limit
                    FROM ae_search_cache
                    WHERE cache_key = ? AND retain_until > ?
                    """,
                    (cache_key, now),
                ).fetchone()
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
                    stale_reason="quota",
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

    async def get_stale(self, cache_key: str) -> CacheHit | None:
        return await asyncio.to_thread(self.get_stale_sync, cache_key)

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
