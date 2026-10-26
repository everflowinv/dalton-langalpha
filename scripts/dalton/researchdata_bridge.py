#!/usr/bin/env python3
"""Host-side Streamable HTTP MCP bridge for OpenClaw's `researchdata_mcp.py`.

Listens on `127.0.0.1:8960` (configurable via `DALTON_BRIDGE_HOST` / `DALTON_BRIDGE_PORT`).
Endpoints:
  - `GET /healthz` (unauthenticated health & quota check)
  - `POST /mcp` (Streamable HTTP JSON-RPC 2.0 endpoint; authenticated via
    `Authorization: Bearer <token>` or `X-Dalton-Bridge-Token` header; legacy
    `POST /mcp/<token>` path remains accepted during transition)

Pure Python 3.11+ standard library only.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import difflib
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

# Ensure sibling `source_identity.py` is importable regardless of cwd
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import source_identity  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] researchdata_bridge: %(message)s",
)
logger = logging.getLogger("researchdata_bridge")

MAX_REQUEST_BYTES = 256 * 1024  # 256 KiB
MAX_EMBED_REQUEST_BYTES = 128 * 1024  # 128 KiB dedicated limit for /embed
MAX_WIKI_INGEST_REQUEST_BYTES = 30 * 1024 * 1024  # 30 MiB HTTP envelope limit for /wiki/ingest (20 MB file b64)
WIKI_INGEST_MAX_TEXT_CHARS = 200_000
WIKI_INGEST_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MB
WIKI_INGEST_TIMEOUT_S: float = 180.0
WIKI_INGEST_SIMILARITY_THRESHOLD: float = 0.80
COMPANY_SLUG_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,29}$")
SECTOR_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
WIKI_INGEST_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".pdf", ".docx", ".png", ".jpg", ".webp"}
)
WIKI_INGEST_ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
    "管理层会议纪要",
    "专家访谈",
    "季度研究笔记",
    "Earnings Update",
    "NDR纪要",
    "Fireside Chat",
    "买方交流",
    "研究笔记",
    "新闻更新",
    "News Update",
    "行业趋势",
    "Industry Trends",
    "Flomo笔记",
    "微信文章",
    "IPO Prospectus",
    "投资备忘",
    "Investment Memo",
)

DEFAULT_ROUTE_TIMEOUT_S = 240
LONG_READ_TIMEOUT_S = 600
FANOUT_ALPHAENGINE_TIMEOUT_S = 60
FANOUT_GUIDEPOINT_TIMEOUT_S = 90
FEISHU_SEND_CARD_TIMEOUT_S: float = 15.0

LONG_READ_OPERATIONS: frozenset[str] = frozenset(
    {
        "read",
        "read-item",
        "read_item",
        "get_document",
        "get-document",
        "get_transcript",
        "get-transcript",
        "transcript",
        "read_filing",
        "read-filing",
        "search-text",
        "search_text",
        "attachments",
        "financials",
        "download_document",
        "download-document",
    }
)

# Route-level (default_s, max_s) timeout budgets aligned with P0-D.1 §274 & R3-2 (240s default, 600s full-text reads)
ROUTE_TIMEOUTS: dict[str, tuple[int, int]] = {
    "catalog": (15, 30),
    "company-wiki": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "company-wiki:read": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sales-notes": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "tmtb": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "funda-ai": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "all": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "alphaengine": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "alphaengine:get_document": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "alphaengine:download_document": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "guidepoint": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript:transcript": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript:get_transcript": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript:read": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "sec:read-item": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:read_filing": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:search-text": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:attachments": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:financials": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "estimates": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "fred": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "treasury": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "market-regime": (15, 30),
    "cn-hk": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "xueqiu": (60, 60),
    "employee": (120, 120),
    "last30days": (600, 600),
    "x-timeline": (60, 60),
}

BRIDGE_COMMUNITY_ROUTES: frozenset[str] = frozenset(
    {"xueqiu", "employee", "last30days", "x-timeline"}
)

COMMUNITY_ROUTE_CONCURRENCY: dict[str, int] = {
    "xueqiu": 2,
    "employee": 2,
    "last30days": 1,
    "x-timeline": 2,
}

COMMUNITY_SIGTERM_GRACE_S: float = 5.0

UPSTREAM_GENERIC_COMMUNITY_PARAMS: frozenset[str] = frozenset(
    {
        "mode",
        "timeout",
    }
)

_LAST30DAYS_HEALTHY_STATES: frozenset[str] = frozenset({"ok", "no-results"})

_LAST30DAYS_FORBIDDEN_QUERY_COMMANDS: frozenset[str] = frozenset(
    {
        "setup",
        "self-test",
        "doctor",
        "diagnose",
        "preflight",
        "welcome",
        "install",
        "upgrade",
        "update",
        "init",
        "config",
        "configure",
        "auth",
        "login",
        "store-key",
        "resolve-save-dir",
        "discover",
        "drill",
        "verify-freshness",
        "library",
        "library feed",
        "library search",
        "queue",
        "queue list",
        "queue cover",
    }
)

_LAST30DAYS_FORBIDDEN_QUERY_PREFIXES: tuple[str, ...] = (
    "setup ",
    "self-test ",
    "doctor ",
    "diagnose ",
    "preflight ",
    "welcome ",
    "install ",
    "upgrade ",
    "library feed ",
    "library search ",
    "queue list ",
    "queue cover ",
)

_COMMUNITY_ERROR_METADATA_KEYS: frozenset[str] = frozenset(
    {
        "source_status",
        "errors_by_source",
        "error_by_source",
        "source_errors",
        "errors",
        "error",
        "warnings",
        "warning",
        "diagnostics",
        "diagnostic",
        "quality",
        "reason",
        "detail",
        "details",
        "hint",
        "hints",
        "status_detail",
        "failure_reason",
    }
)

_SECRET_KEY_NAME_RE = re.compile(
    # `auth` only as a whole word so `author`/`author_id` survive; `token` not as a count (`totalTokens`).
    r"(?i)(?:api[_-]?key|apikey|token(?!s)|secret|password|passwd|cookie|authorization|(?<![a-z])auth(?![a-z])|credential|session[_-]?id|xq_[a-z_]+)"
)

_STDERR_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?im)^([^\r\n]*?\b(?:set-)?cookie\s*:\s*)[^\r\n]+$"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"""(?i)(['"]?[A-Za-z0-9_.\-]*(?:api[_-]?key|apikey|token|secret|password|passwd|cookie|set-cookie|authorization|proxy-authorization|x-api-key|auth|credential|session[_-]?id|xq_[a-z0-9_]+)[A-Za-z0-9_.\-]*['"]?\s*[:=]\s*)(['"])(?:\\.|(?!\2).)*\2"""
        ),
        r"\1\2[REDACTED]\2",
    ),
    (
        re.compile(
            r"(?i)(\b(?:proxy-)?authorization\s*[:=]\s*)(?:(?:bearer|basic|digest|token)\s+)?(?!['\"]?\[REDACTED\])[^\s;,'\"}\]\r\n]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)(\bbearer\s+)(?!\[REDACTED\])[A-Za-z0-9._\-+/=~]{4,}"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(['\"]?\b[A-Za-z0-9_.\-]*(?:api[_-]?key|apikey|token|secret|password|passwd|cookie|set-cookie|authorization|proxy-authorization|x-api-key|auth|credential|session[_-]?id|xq_[a-z0-9_]+)[A-Za-z0-9_.\-]*['\"]?\s*[:=]\s*)(?!['\"]?\[REDACTED\])[^\s;,'\"}\]\r\n]+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b(xai-[A-Za-z0-9_\-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b(tvly-[A-Za-z0-9_\-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b(fc-[A-Za-z0-9_\-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b(ghp_[A-Za-z0-9_\-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"\b(github_pat_[A-Za-z0-9_\-]{8,})\b"), "[REDACTED]"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),
    (re.compile(r"\b[0-9a-fA-F]{40,}\b"), "[REDACTED]"),
    (
        re.compile(
            r"(?<![A-Za-z0-9_.\-/])(?=[A-Za-z0-9+/_-]*[A-Za-z0-9])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9_.\-/])"
        ),
        "[REDACTED]",
    ),
)


def sanitize_stderr_text(raw: str | bytes | None, *, max_len: int = 500) -> str:
    """Sanitize secrets from subprocess stderr first, then keep at most the last `max_len` chars."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    text = text.strip()
    if not text:
        return ""
    for pat, repl in _STDERR_SECRET_PATTERNS:
        text = pat.sub(repl, text)
    if len(text) > max_len:
        text = text[-max_len:]
    return text.strip()


def _sanitize_metadata_tree(obj: Any, *, max_str_len: int = 300) -> Any:
    """Recursively sanitize strings and redact secret-keyed fields in metadata structures."""
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, str):
        return sanitize_stderr_text(obj, max_len=max_str_len)
    if isinstance(obj, list):
        return [_sanitize_metadata_tree(item, max_str_len=max_str_len) for item in obj]
    if isinstance(obj, dict):
        cleaned_dict: dict[str, Any] = {}
        for k, v in obj.items():
            k_str = str(k).strip()[:64]
            if not k_str:
                continue
            if _SECRET_KEY_NAME_RE.search(k_str):
                cleaned_dict[k_str] = "[REDACTED]"
            else:
                cleaned_dict[k_str] = _sanitize_metadata_tree(v, max_str_len=max_str_len)
        return cleaned_dict
    return sanitize_stderr_text(str(obj), max_len=max_str_len)


def _sanitize_source_status_map(raw_status: Any) -> dict[str, Any]:
    """Return a sanitized copy of `last30days` `source_status` without raw secrets."""
    if not isinstance(raw_status, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for src, val in raw_status.items():
        src_key = str(src).strip()[:64]
        if not src_key or _SECRET_KEY_NAME_RE.search(src_key):
            continue
        if isinstance(val, str):
            cleaned[src_key] = sanitize_stderr_text(val, max_len=120)
        elif isinstance(val, dict):
            entry_out: dict[str, Any] = {}
            for k, v in val.items():
                k_str = str(k).strip()
                if not k_str or _SECRET_KEY_NAME_RE.search(k_str):
                    continue
                if isinstance(v, (int, float, bool)) or v is None:
                    entry_out[k_str] = v
                else:
                    entry_out[k_str] = _sanitize_metadata_tree(v, max_str_len=200)
            cleaned[src_key] = entry_out
    return cleaned


def _sanitize_community_payload_metadata(parsed: Any) -> Any:
    """Sanitize `source_status`, `errors_by_source`, and all error/diagnostic metadata in `parsed` in-place."""
    if not isinstance(parsed, dict):
        return parsed
    for k in list(parsed.keys()):
        k_str = str(k).strip()
        val = parsed[k]
        if _SECRET_KEY_NAME_RE.search(k_str):
            parsed[k] = "[REDACTED]"
        elif k_str == "source_status":
            parsed[k] = _sanitize_source_status_map(val)
        elif k_str in _COMMUNITY_ERROR_METADATA_KEYS or k_str.endswith(
            ("_error", "_errors", "_warning", "_warnings", "_status")
        ):
            parsed[k] = _sanitize_metadata_tree(val, max_str_len=300)
    return parsed


def sanitize_error_data_summary(
    parsed: Any,
    *,
    route: str,
    operation: str,
    status: str,
    error: str,
) -> dict[str, Any]:
    """Build a sanitized summary dict for `ok: false` community responses instead of exposing raw objects."""
    summary: dict[str, Any] = {
        "route": route,
        "status": status,
        "error": sanitize_stderr_text(error, max_len=500),
    }
    if operation:
        summary["operation"] = operation
    if isinstance(parsed, dict):
        err_code = parsed.get("error_code") or parsed.get("code")
        if isinstance(err_code, (str, int)) and not isinstance(err_code, bool):
            summary["error_code"] = sanitize_stderr_text(str(err_code), max_len=120)
        if route == "last30days":
            if isinstance(parsed.get("source_status"), dict):
                summary["source_status"] = _sanitize_source_status_map(
                    parsed["source_status"]
                )
            if isinstance(parsed.get("errors_by_source"), dict):
                summary["errors_by_source"] = _sanitize_metadata_tree(
                    parsed["errors_by_source"], max_str_len=200
                )
    return summary


def _pg_is_alive(pgid: int) -> bool:
    """Return True if process group `pgid` still has at least one live member."""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


async def _terminate_and_reap_process(
    proc: Any, *, term_timeout_s: float = COMMUNITY_SIGTERM_GRACE_S
) -> None:
    """Send SIGTERM to the subprocess process group first, wait up to `term_timeout_s`, then SIGKILL and reap."""
    cancelled = False
    pid = getattr(proc, "pid", None)
    has_pgid = isinstance(pid, int) and pid > 0
    pg_alive = False
    sent_pg_term = False

    if has_pgid:
        try:
            os.killpg(pid, signal.SIGTERM)
            sent_pg_term = True
            pg_alive = True
        except ProcessLookupError:
            sent_pg_term = True
            pg_alive = False
        except OSError:
            sent_pg_term = False

    leader_alive = not isinstance(getattr(proc, "returncode", None), int)
    if not sent_pg_term and leader_alive:
        try:
            proc.terminate()
        except ProcessLookupError:
            leader_alive = False

    if leader_alive or pg_alive:
        deadline = time.monotonic() + max(0.0, float(term_timeout_s))
        waiter = asyncio.ensure_future(proc.wait()) if leader_alive else None
        timed_out = False

        if waiter is not None:
            while not waiter.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = not waiter.done()
                    break
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break

        if not timed_out and has_pgid and pg_alive:
            while _pg_is_alive(pid):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    await asyncio.sleep(min(0.02, remaining))
                except asyncio.CancelledError:
                    cancelled = True
                    timed_out = _pg_is_alive(pid)
                    break
            else:
                pg_alive = False

        if timed_out or (waiter is not None and not waiter.done()) or (has_pgid and pg_alive):
            sent_pg_kill = False
            if has_pgid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                    sent_pg_kill = True
                except ProcessLookupError:
                    sent_pg_kill = True
                except OSError:
                    sent_pg_kill = False
            if not sent_pg_kill and not isinstance(getattr(proc, "returncode", None), int):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    try:
        await _reap_process(proc)
    except asyncio.CancelledError:
        cancelled = True
    if cancelled:
        raise asyncio.CancelledError()


MARKET_REGIME_OPERATIONS: tuple[str, ...] = (
    "snapshot",
    "us_topbottom",
    "crowding",
    "cn_leverage",
)

CN_LEVERAGE_KEY_COLUMNS: tuple[str, ...] = (
    "date",
    "unwind_pressure_score",
    "unwind_direction",
    "regime",
    "gross_leverage_score",
    "neutral_crowding_score",
    "crowding_score",
)

_LOCAL_PATH_RE = re.compile(r"/(?:Users|private|opt|tmp|var)/[^\s,;:\"')\]>]+")


def _scrub_local_paths(text: Any) -> str:
    """Replace host absolute paths starting with `/Users/`, `/private/`, `/opt/`, `/tmp/`, or `/var/` with `<local>`."""
    if text is None:
        return ""
    return _LOCAL_PATH_RE.sub("<local>", str(text)).strip()


def _atomic_write_json_locked(path: Path, payload: dict[str, Any], *, label: str = "state") -> bool:
    """Atomically write JSON via a unique temp file + rename under an ``fcntl.flock`` lock."""
    import fcntl

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f"{path.name}.lock")
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        with lock_path.open("a+", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                tmp_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp_path, path)
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        return True
    except Exception as exc:
        logger.warning(
            "Failed writing %s file %s: %s",
            label,
            _scrub_local_paths(str(path)),
            type(exc).__name__,
        )
        return False

MARKET_REGIME_CATALOG_ENTRY: dict[str, Any] = {
    "id": "market-regime",
    "category": "macro",
    "tool": "macro",
    "kind": "bridge_readonly",
    "name": "Market Regime & Crowding Monitor",
    "description": "大盘过热/超卖、顶部底部阶段、拥挤度、两融杠杆；回答大盘和仓位问题时先读。",
    "operations": list(MARKET_REGIME_OPERATIONS),
    "default_operation": "snapshot",
    "parameters": {
        "operation": {
            "type": "string",
            "enum": list(MARKET_REGIME_OPERATIONS),
            "default": "snapshot",
        },
        "top": {
            "type": "integer",
            "default": 10,
            "maximum": 30,
            "description": "crowding 返回条数（默认 10，最多 30）",
        },
        "order": {
            "type": "string",
            "enum": ["high", "low"],
            "default": "high",
            "description": "crowding 排序方向（high / low）",
        },
        "category": {
            "type": "string",
            "description": "crowding 按类别过滤（可选）",
        },
        "query": {
            "type": "string",
            "description": "crowding 按指数名称或代码过滤（可选）",
        },
    },
}


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday for a given Gregorian year (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th (1-based) occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(d: date, allow_prior_year: bool = False) -> date | None:
    """NYSE weekend observance rule: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        obs = d - timedelta(days=1)
        if obs.year < d.year and not allow_prior_year:
            return None
        return obs
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _us_nyse_holidays(year: int) -> set[date]:
    """Return the set of NYSE full-day holidays for `year` using stdlib."""
    holidays: set[date] = set()
    nyd = _observed(date(year, 1, 1), allow_prior_year=False)
    if nyd:
        holidays.add(nyd)
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))
    holidays.add(_nth_weekday(year, 2, 0, 3))
    holidays.add(_easter_sunday(year) - timedelta(days=2))
    holidays.add(_last_weekday(year, 5, 0))
    if year >= 2022:
        jt = _observed(date(year, 6, 19))
        if jt:
            holidays.add(jt)
    ind = _observed(date(year, 7, 4))
    if ind:
        holidays.add(ind)
    holidays.add(_nth_weekday(year, 9, 0, 1))
    holidays.add(_nth_weekday(year, 11, 3, 4))
    xmas = _observed(date(year, 12, 25))
    if xmas:
        holidays.add(xmas)
    return holidays


def _cn_fixed_holidays(year: int) -> set[date]:
    """Return a Gregorian fixed-holiday subset approximation for A-shares (SSE/SZSE).

    Note: This is an approximation covering only fixed Gregorian holidays (Jan 1,
    May 1, Oct 1-3); it excludes lunar holidays (Spring Festival, Qingming, Dragon
    Boat, Mid-Autumn) and State Council lieu-day adjustments (调休/补假).
    """
    return {
        date(year, 1, 1),
        date(year, 5, 1),
        date(year, 10, 1),
        date(year, 10, 2),
        date(year, 10, 3),
    }


def _is_us_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _us_nyse_holidays(d.year)


def _is_cn_trading_day(d: date) -> bool:
    # Approximation: weekdays minus Gregorian fixed CN holidays (excludes lunar/调休).
    return d.weekday() < 5 and d not in _cn_fixed_holidays(d.year)


def _latest_us_trading_day(ref_date: date) -> date:
    """Return `ref_date` if it is an NYSE trading day, otherwise step back to the most recent NYSE trading day."""
    d = ref_date
    while not _is_us_trading_day(d):
        d -= timedelta(days=1)
    return d


def _latest_cn_trading_day(ref_date: date) -> date:
    """Return `ref_date` if it is an A-share trading day (approximate), otherwise step back."""
    d = ref_date
    while not _is_cn_trading_day(d):
        d -= timedelta(days=1)
    return d


def _prev_cn_trading_day(ref_date: date, steps: int = 1) -> date:
    """Return `steps` A-share trading days before `_latest_cn_trading_day(ref_date)`."""
    d = _latest_cn_trading_day(ref_date)
    for _ in range(max(0, steps)):
        d -= timedelta(days=1)
        while not _is_cn_trading_day(d):
            d -= timedelta(days=1)
    return d


def _parse_csv_scalar(val: Any) -> Any:
    """Convert numeric CSV cell strings to int/float while keeping text values intact."""
    if not isinstance(val, str):
        return val
    s = val.strip()
    if s == "":
        return None
    try:
        if "." not in s and "e" not in s.lower():
            return int(s)
        return float(s)
    except ValueError:
        return s


async def _reap_process(proc: Any) -> None:
    """Wait for a killed subprocess to exit even if this task is cancelled meanwhile.

    A cancel during the wait would otherwise leave the child unreaped; the
    cancellation is re-raised once the child is gone.
    """
    waiter = asyncio.ensure_future(proc.wait())
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        raise asyncio.CancelledError()


async def _cancel_and_reap(tasks: "tuple[asyncio.Task[Any], ...]") -> None:
    """Cancel unfinished fan-out tasks and wait for them, surviving a cancel of the caller."""
    pending = [t for t in tasks if not t.done()]
    for t in pending:
        t.cancel()
    if not pending:
        return
    gathered = asyncio.ensure_future(asyncio.gather(*pending, return_exceptions=True))
    cancelled = False
    while not gathered.done():
        try:
            await asyncio.shield(gathered)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()


class SubprocessExitError(RuntimeError):
    """Raised when researchdata_mcp exits without valid JSON-RPC output (never carries stderr)."""

    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code
        super().__init__(f"researchdata_mcp exited without JSON response (exit_code={exit_code})")


def _read_env_file_dict(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from `path` without consulting `os.environ`."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k:
                out[k] = v
    except Exception as exc:
        logger.warning("Failed reading env file %s: %s", path, type(exc).__name__)
    return out


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines into `os.environ`.

    `GEMINI_API_KEY` is authoritative from `path` (never inherited from process environment),
    while non-secret `DALTON_BRIDGE_*` runtime knobs only populate `os.environ` if unset.
    """
    parsed = _read_env_file_dict(path)
    if "GEMINI_API_KEY" in parsed and parsed["GEMINI_API_KEY"].strip():
        os.environ["GEMINI_API_KEY"] = parsed["GEMINI_API_KEY"].strip()
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    for k, v in parsed.items():
        if k == "GEMINI_API_KEY":
            continue
        if k not in os.environ:
            os.environ[k] = v


class BridgeConfig:
    def __init__(self) -> None:
        env_path = Path(
            os.environ.get(
                "DALTON_BRIDGE_ENV_FILE",
                os.path.expanduser("~/.config/dalton/researchdata-bridge.env"),
            )
        )
        self.env_path: Path = env_path
        _load_env_file(env_path)

        self.host: str = os.environ.get("DALTON_BRIDGE_HOST", "127.0.0.1")
        self.port: int = int(os.environ.get("DALTON_BRIDGE_PORT", "8960"))
        self.token: str = os.environ.get("DALTON_BRIDGE_TOKEN", "").strip()
        self.admin_token: str = os.environ.get("DALTON_BRIDGE_ADMIN_TOKEN", "").strip()
        self.ocw_root: Path = Path(
            os.environ.get(
                "DALTON_OCW_ROOT",
                os.path.expanduser("~/.openclaw/workspace"),
            )
        ).resolve()
        self.python_bin: str = os.environ.get(
            "DALTON_OCW_PYTHON", "/opt/homebrew/bin/python3"
        )
        if not os.path.exists(self.python_bin):
            self.python_bin = sys.executable

        self.mcp_script: Path = Path(
            os.environ.get(
                "DALTON_MCP_SCRIPT",
                str(self.ocw_root / "scripts" / "researchdata_mcp.py"),
            )
        ).resolve()
        self.xueqiu_script: Path = Path(
            os.environ.get(
                "DALTON_XUEQIU_SCRIPT",
                str(self.ocw_root / "scripts" / "xueqiu_posts.py"),
            )
        ).expanduser().resolve()
        self.xreach_bin: Path = Path(
            os.environ.get(
                "DALTON_XREACH_BIN",
                "~/.openclaw/tools/node/bin/xreach",
            )
        ).expanduser().resolve()
        self.employee_python: Path = Path(
            os.path.abspath(
                os.path.expanduser(
                    os.environ.get(
                        "DALTON_EMPLOYEE_PYTHON",
                        str(
                            self.ocw_root
                            / "skills"
                            / "employee-reviews"
                            / "venv"
                            / "bin"
                            / "python"
                        ),
                    )
                )
            )
        )
        self.employee_cli_script: Path = Path(
            os.environ.get(
                "DALTON_EMPLOYEE_CLI_SCRIPT",
                os.environ.get(
                    "DALTON_EMPLOYEE_SCRIPT",
                    str(
                        self.ocw_root
                        / "skills"
                        / "employee-reviews"
                        / "scripts"
                        / "employee_reviews_cli.py"
                    ),
                ),
            )
        ).expanduser().resolve()
        self.employee_script: Path = self.employee_cli_script
        default_l30_engine: Path = Path(
            os.environ.get(
                "DALTON_LAST30DAYS_ENGINE_DIR",
                "~/.local/share/last30days/3.27.2-openclaw-argon-20261013",
            )
        ).expanduser()
        self.last30days_python: Path = Path(
            os.path.abspath(
                os.path.expanduser(
                    os.environ.get(
                        "DALTON_LAST30DAYS_PYTHON",
                        str(default_l30_engine / "venv" / "bin" / "python"),
                    )
                )
            )
        )
        self.last30days_cli_script: Path = Path(
            os.environ.get(
                "DALTON_LAST30DAYS_CLI_SCRIPT",
                os.environ.get(
                    "DALTON_LAST30DAYS_SCRIPT",
                    str(default_l30_engine / "scripts" / "last30days.py"),
                ),
            )
        ).expanduser().resolve()
        self.last30days_script: Path = self.last30days_cli_script
        self.subprocess_proxy: str = os.environ.get(
            "DALTON_BRIDGE_SUBPROCESS_PROXY", ""
        ).strip()
        self.cache_dir: Path = Path(
            os.environ.get(
                "DALTON_BRIDGE_CACHE_DIR",
                os.path.expanduser("~/Library/Caches/dalton-researchdata-bridge"),
            )
        ).expanduser().resolve()
        self.data_sources_json: Path = (
            self.ocw_root / "config" / "data_sources.json"
        )
        self.sentiment_signals_json: Path = Path(
            os.environ.get("DALTON_SENTIMENT_SIGNALS_JSON")
            or os.environ.get("DALTON_MARKET_REGIME_SIGNALS_JSON")
            or str(
                self.ocw_root
                / "research-outputs"
                / "sentiment-dashboard"
                / "signals.json"
            )
        )
        self.index_crowding_json: Path = Path(
            os.environ.get("DALTON_INDEX_CROWDING_JSON")
            or os.environ.get("DALTON_MARKET_REGIME_CROWDING_JSON")
            or str(
                self.ocw_root
                / "research-outputs"
                / "index-crowding"
                / "official-all.json"
            )
        )
        self.cn_leverage_status_json: Path = Path(
            os.environ.get("DALTON_CN_LEVERAGE_STATUS_JSON")
            or os.environ.get("DALTON_MARKET_REGIME_LEVERAGE_STATUS_JSON")
            or str(
                self.ocw_root
                / "state"
                / "cn-leverage"
                / "pipeline_status.json"
            )
        )
        self.cn_leverage_unwind_csv: Path = Path(
            os.environ.get("DALTON_CN_LEVERAGE_UNWIND_CSV")
            or os.environ.get("DALTON_MARKET_REGIME_UNWIND_CSV")
            or str(
                self.ocw_root
                / "state"
                / "cn-leverage"
                / "quant_unwind_signals.csv"
            )
        )
        self.max_inflight: int = max(
            1, int(os.environ.get("DALTON_BRIDGE_MAX_INFLIGHT", "2"))
        )
        self.embed_max_inflight: int = max(
            1, int(os.environ.get("DALTON_BRIDGE_EMBED_MAX_INFLIGHT", "2"))
        )
        self.embed_max_bytes: int = max(
            4096,
            int(os.environ.get("DALTON_BRIDGE_EMBED_MAX_BYTES", str(MAX_EMBED_REQUEST_BYTES))),
        )
        # Default 625s matches agent_config.dalton.yaml call_timeout_s=625 (625 - 25 = 600s long-read ceiling)
        self.caller_budget_s: int = max(
            35, int(os.environ.get("DALTON_BRIDGE_CALLER_BUDGET_S", "625"))
        )
        self.usage_file: Path = Path(
            os.environ.get(
                "DALTON_BRIDGE_USAGE_FILE",
                os.path.expanduser("~/.cache/dalton/bridge-usage.json"),
            )
        )
        self.archive_state_file: Path = Path(
            os.environ.get(
                "DALTON_ARCHIVE_STATE_FILE",
                os.path.expanduser("~/.config/dalton/conversation-archive-state.json"),
            )
        ).expanduser()
        self.archive_status_file: Path = Path(
            os.environ.get(
                "DALTON_ARCHIVE_STATUS_FILE",
                os.path.expanduser("~/.cache/dalton/conversation-archive-status.json"),
            )
        ).expanduser()
        self.archive_log_file: Path = Path(
            os.environ.get(
                "DALTON_ARCHIVE_LOG_FILE",
                os.path.expanduser("~/Library/Logs/dalton-conversation-archive.log"),
            )
        ).expanduser()
        self.archive_err_log_file: Path = Path(
            os.environ.get(
                "DALTON_ARCHIVE_ERR_LOG_FILE",
                os.path.expanduser("~/Library/Logs/dalton-conversation-archive.err.log"),
            )
        ).expanduser()
        self.conversation_archive_py: Path = Path(
            os.environ.get(
                "DALTON_CONVERSATION_ARCHIVE_PY",
                str(_SCRIPT_DIR / "conversation_archive.py"),
            )
        ).expanduser().resolve()
        self.repo_python: Path = Path(
            os.environ.get(
                "DALTON_REPO_PYTHON",
                str(_SCRIPT_DIR.parents[1] / ".venv" / "bin" / "python"),
            )
        ).expanduser()
        try:
            self.archive_startup_probe_s: float = max(
                0.05,
                float(os.environ.get("DALTON_ARCHIVE_STARTUP_PROBE_S", "0.5") or 0.5),
            )
        except ValueError:
            self.archive_startup_probe_s = 0.5
        try:
            self.archive_run_timeout_s: float = max(
                1.0,
                float(os.environ.get("DALTON_ARCHIVE_RUN_TIMEOUT_S", "10800") or 10800.0),
            )
        except ValueError:
            self.archive_run_timeout_s = 10800.0
        self.ops_status_file: Path = Path(
            os.environ.get(
                "DALTON_OPS_STATUS_FILE",
                os.path.expanduser("~/.cache/dalton/ops-jobs-status.json"),
            )
        ).expanduser()
        self.openclaw_gateway_url: str = (
            os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
            .strip()
            .rstrip("/")
        )
        self.openclaw_gateway_token: str = os.environ.get(
            "OPENCLAW_GATEWAY_TOKEN", ""
        ).strip()
        self.feishu_send_card_script: Path = Path(
            os.environ.get(
                "FEISHU_SEND_CARD_SCRIPT",
                os.path.expanduser("~/.openclaw/workspace/scripts/feishu_send_card.py"),
            )
        ).expanduser()
        try:
            self.feishu_send_card_timeout_s: float = max(
                1.0,
                float(
                    os.environ.get(
                        "FEISHU_SEND_CARD_TIMEOUT_S",
                        str(FEISHU_SEND_CARD_TIMEOUT_S),
                    )
                    or FEISHU_SEND_CARD_TIMEOUT_S
                ),
            )
        except ValueError:
            self.feishu_send_card_timeout_s = FEISHU_SEND_CARD_TIMEOUT_S
        self.wiki_run_sh: Path | None = (
            Path(os.environ["DALTON_WIKI_RUN_SH"]).expanduser()
            if os.environ.get("DALTON_WIKI_RUN_SH", "").strip()
            else None
        )
        default_wiki_audit = _SCRIPT_DIR.parents[1] / "var" / "dalton" / "wiki_ingest_audit.jsonl"
        self.wiki_ingest_audit_file: Path = Path(
            os.environ.get("DALTON_WIKI_INGEST_AUDIT_FILE", str(default_wiki_audit))
        ).expanduser()
        default_wiki_tmp = _SCRIPT_DIR.parents[1] / "var" / "dalton" / "tmp" / "wiki_ingest"
        self.wiki_ingest_temp_dir: Path = Path(
            os.environ.get("DALTON_WIKI_INGEST_TEMP_DIR", str(default_wiki_tmp))
        ).expanduser()
        try:
            self.wiki_ingest_timeout_s: float = max(
                5.0,
                float(
                    os.environ.get(
                        "DALTON_WIKI_INGEST_TIMEOUT_S",
                        str(WIKI_INGEST_TIMEOUT_S),
                    )
                    or WIKI_INGEST_TIMEOUT_S
                ),
            )
        except ValueError:
            self.wiki_ingest_timeout_s = WIKI_INGEST_TIMEOUT_S
        self.test_mode: bool = os.environ.get("DALTON_BRIDGE_TEST_MODE", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self.test_delay_s: float = 0.0
        if self.test_mode:
            try:
                self.test_delay_s = max(
                    0.0, float(os.environ.get("DALTON_BRIDGE_TEST_DELAY_S", "0") or 0.0)
                )
            except ValueError:
                self.test_delay_s = 0.0

    def resolve_test_delay_s(self) -> float:
        """Return active test delay in seconds (0.0 unless self.test_mode is True)."""
        if not self.test_mode:
            return 0.0
        if self.test_delay_s > 0.0:
            return self.test_delay_s
        try:
            return max(0.0, float(os.environ.get("DALTON_BRIDGE_TEST_DELAY_S", "0") or 0.0))
        except ValueError:
            return 0.0


RESEARCH_EVIDENCE_AND_ROUTE_GUIDE: str = (
    '各来源提供什么证据：事实 → filings（finance(route="sec")）；'
    "解读 → alphaengine；一线 → guidepoint；管理层原话 → transcript；日常信息流 → sales-notes/tmtb/funda-ai；"
    "市场反应 → X/WebSearch。\n"
    "何时选用哪个路由（按研究情境，基于 catalog）：\n"
    "- 卖方观点与研报 → alphaengine（先 search_library 再 get_document）；\n"
    "- 行业一线反馈 → guidepoint；\n"
    "- 管理层口径 → transcript（AlphaEngine 业绩会转写；无匹配时用 | 分隔中英文公司名重试或扩大 start/end）；\n"
    "- 历史沉淀 → company-wiki（只读，params.company=TICKER）；\n"
    "- 交易台与买方预期 → sales-notes；\n"
    "- TMT 焦点 → tmtb；\n"
    "- 独立研究 → funda-ai；\n"
    "- 跨库初筛 → all（company-wiki + sales-notes + tmtb + funda-ai，并行检索 AlphaEngine 与 Guidepoint；不含 transcript）。"
)

ERROR_HANDLING_CONTRACT_NOTE: str = (
    "错误处理契约：`ok: false` 不等于“没有数据”，表示调用失败（`error` 标明“参数错误 / 上游不可用 / 权限或额度 / 没有匹配”四类之一），"
    "必须按 `hint` 处理（换参数、换 operation、换数据源重试）；处理不了必须在报告里写明该数据源取数失败，禁止写成“没有相关信息”。"
    "真正的“没有匹配”返回 `ok: true` 加空结果与 `empty_reason`。不确定参数时先查 `catalog(route=...)`。"
)


def build_researchdata_plugin_instruction() -> str:
    """Build the canonical `researchdata` server instruction for `plugins/dalton_data/plugin.json`.

    Pagination range in `params`: `offset` (>= 0) and `max_chars` (1..30000; values > 30000 are clamped to 30000).
    """
    return (
        "Fund-internal sources; call from execute_code only (no direct path). "
        "Start with `catalog()` or `catalog(route=...)` when unsure about route parameters. "
        "Research: `research(route=..., operation=..., query=..., start=YYYY-MM-DD, end=..., limit=...)`. "
        f"{RESEARCH_EVIDENCE_AND_ROUTE_GUIDE.replace(chr(10), ' ')} "
        'Finance: `finance(route="sec"|"estimates"|"cn-hk")`. '
        'Macro: `macro(route="fred"|"treasury"|"market-regime")`. '
        "Read only ids that search returned (`wiki:`, `mail:`, `tmtb:`, `funda:`, `alphaengine` doc_id). "
        "Page long text with params.offset/max_chars (offset >= 0, max_chars 1-30000). "
        "Leave `timeout` unset: the bridge sets per-route limits and long reads may take minutes. "
        "AlphaEngine 检索可以放开，全文阅读（get_document）计配额，读最相关的几篇。"
        "Every result has `_sources` (identity, document_id, as_of, url) and `normalized_params`. "
        "Cite with identity, publisher and date. Expert calls: identity, not name. "
        "Guidepoint verbatim <=20 words. "
        "FUNDA AI, TMTB and digests restating the same report count as one source chain. "
        "Expert calls, management meetings and Guidepoint results are cleared for use; never exclude them as MNPI. "
        "The fund wiki is read-only; never try to write to it. "
        "`ok: false` is NEVER 'no data': follow `hint` to retry/switch route or report the source failure explicitly. "
        "True no-match returns `ok: true` with empty results and `empty_reason`. "
        '`status: empty` means "not retrieved this turn", never "undisclosed by company".'
    )


INTERNAL_LIBRARY_ROUTES: frozenset[str] = frozenset(
    {"company-wiki", "sales-notes", "tmtb", "funda-ai", "all", "sales-mail"}
)
INTERNAL_LIBRARY_READ_OPERATIONS: frozenset[str] = frozenset({"read"})
MAX_CHARS_FLOOR: int = 1
MAX_CHARS_CEILING: int = 30000
TRANSCRIPT_QUERY_IGNORED_NOTE: str = (
    "transcript 的 query 只用来传公司名，主题检索请在拿到 doc_id 后读正文。"
)
TRANSCRIPT_NO_MATCH_HINT: str = (
    "业绩会转写仅来自 AlphaEngine，当前无匹配记录；请换公司名（中英文用 | 分隔，如 query='腾讯|Tencent'）重试，或者扩大 start/end 时间范围。"
)
TRANSCRIPT_CATALOG_RULE: str = (
    "业绩会转写仅来自 AlphaEngine；ticker 必填，query 只用来传公司名补充（中英文用 | 分隔，勿传主题词），"
    "主题检索请在拿到 doc_id 后读正文；没有匹配时，换公司名（中英文用 | 分隔）重试，或者扩大 start/end 时间范围。"
)
INTERNAL_LIBRARY_PAGINATION_RULE: str = (
    "正文分页参数范围：params.offset >= 0，params.max_chars 为 1–30000（超出 30000 自动截断为 30000）；"
    "读取用顶层 document 参数传带前缀 ID（wiki:/mail:/tmtb:/digest:/funda:），纯数字 ID 属 AlphaEngine 文档。"
)

_TRANSCRIPT_TOPIC_EN_RE = re.compile(
    r"\b(?:"
    r"earnings|call|guidance|capex|capital|expenditures?|revenue|margin|results|"
    r"outlook|transcript|conference|quarter|"
    r"q[1-4]|fy(?:\d{2,4})?|20\d\d"
    r")\b",
    re.IGNORECASE,
)
_TRANSCRIPT_TOPIC_ZH_KEYWORDS: tuple[str, ...] = (
    "业绩",
    "电话会",
    "指引",
    "资本开支",
    "营收",
    "利润率",
)


def _parse_int_param(val: Any) -> int | None:
    """Parse an int or integer string (excluding bool), returning None if not an integer."""
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if re.fullmatch(r"[+-]?\d+", s):
            try:
                return int(s)
            except ValueError:
                return None
    return None


def _is_topic_query_for_transcript(query: str) -> bool:
    """Return True only when `query` contains explicit topic signals (earnings/quarter/year/metric terms)."""
    q = query.strip()
    if not q or "|" in q:
        return False
    if _TRANSCRIPT_TOPIC_EN_RE.search(q):
        return True
    return any(kw in q for kw in _TRANSCRIPT_TOPIC_ZH_KEYWORDS)


CATALOG_DESCRIPTION_SUFFIX: str = (
    "\n\nNote: 何时使用：不确定任何工具或路由的必填/可选参数、日期与股票代码格式、返回结构或调用示例时，"
    "先调用 `catalog(route=...)` 或 `catalog(category=...)` 查询完整参数表。"
    "`ok: false` 不等于“没有数据”，表示调用失败，须按 `hint` 处理。"
)

FINANCE_DESCRIPTION_SUFFIX: str = (
    "\n\nNote: 何时使用：查美股 SEC 公告与 XBRL 财务（route=\"sec\"）、卖方一致预期（route=\"estimates\"）、"
    "港股与 A 股公告及财务问答（route=\"cn-hk\"，港股代码 5 位如 \"00700\"）。"
    "不确定各操作必填参数或格式时先查 `catalog(route=...)`。"
    "`ok: false` 不等于“没有数据”，必须按 `hint` 修正参数或换数据源重试；真无匹配返回 `ok: true` + `empty_reason`。"
)

MACRO_DESCRIPTION_SUFFIX: str = (
    "\n\nNote: 何时使用：查 FRED 宏观经济序列（route=\"fred\"）、美国财政部 TGA/国债数据（route=\"treasury\"）、"
    "大盘温度/顶底信号/拥挤度/两融杠杆（route=\"market-regime\"）。"
    "不确定必填参数（如 fred/treasury 必填 start/end）时先查 `catalog(route=...)`。"
    "`ok: false` 不等于“没有数据”，必须按 `hint` 处理；真无匹配返回 `ok: true` + `empty_reason`。"
)

RESEARCH_ROUTE_PARAM_CONTRACT_SUFFIX: str = (
    "\n不确定参数时先查 `catalog(route=...)`（全文分页 params.offset >= 0，params.max_chars 为 1–30000）。"
    "`ok: false` 不等于“没有数据”，必须按 `hint` 处理；真无匹配返回 `ok: true` + `empty_reason`。"
)

COMMUNITY_DESCRIPTION_SUFFIX: str = (
    "\n\nNote: 何时使用：查雪球讨论与个股情绪（route=\"xueqiu\"）、员工评价 Blind/Indeed/Glassdoor（route=\"employee\"）、"
    "近30天跨平台社区情绪（route=\"last30days\"）、X 账号时间线与检索（route=\"x-timeline\"）。"
    "不确定参数时先查 `catalog(route=...)`。"
    "`ok: false` 不等于“没有数据”，必须按 `hint` 处理；真无匹配返回 `ok: true` + `empty_reason`。"
)

NEWS_DESCRIPTION_SUFFIX: str = (
    "\n\nNote: 何时使用：web-news/web-page/x-search 在 LangAlpha 中已禁用（返回 handoff_filtered）；"
    "网页新闻请改用内置 WebSearch/WebFetch，X 检索请改用 `community(route=\"x-timeline\")`。"
    "不确定参数时先查 `catalog(route=...)`；`ok: false` 不等于“没有数据”，须按 `hint` 切换工具。"
)

COMMUNITY_CATALOG_ROUTE_SPECS: dict[str, dict[str, Any]] = {
    "xueqiu": {
        "id": "xueqiu",
        "category": "community",
        "kind": "cli",
        "title": "雪球讨论与个股情绪",
        "operations": ["posts", "post", "comments", "search", "user"],
        "parameters": {
            "operation": "posts | post | comments | search | user",
            "ticker": "股票代码（posts 必填，港股 5 位如 00700/06181，A 股如 SH600519/SZ300750）",
            "url": "帖子或用户主页 URL（post/user 必填，仅允许 xueqiu.com 域名）",
            "post_id": "雪球帖子 ID（comments 必填，纯数字字符串）",
            "query": "搜索关键词（search 必填）",
            "pages": "翻页数（1-3，默认 1）",
        },
        "rules": [
            "港股、A股、中概散户与中文 KOL 情绪首选",
            "仅算情绪与线索，不算事实证据；引用须带平台、日期与原帖 URL",
        ],
    },
    "employee": {
        "id": "employee",
        "category": "community",
        "kind": "cli",
        "title": "员工评价与内部反馈 (Blind / Indeed / Glassdoor)",
        "operations": ["blind", "summary", "indeed", "glassdoor"],
        "parameters": {
            "operation": "blind | summary | indeed | glassdoor",
            "company": "公司名称或 slug（blind/summary/indeed 必填，如 NVIDIA）",
            "url": "Glassdoor 评论页 URL（glassdoor 必填，仅允许 glassdoor.com 域名）",
            "pages": "翻页数（blind/indeed 1-3，summary 1-40）",
            "allow_paid": "是否允许调用付费源（bool，indeed 与 glassdoor 必须显式传 allow_paid=True）",
        },
        "rules": [
            "blind 与 summary 免费；indeed 与 glassdoor 付费，默认禁用（须 allow_paid=True）",
            "Blind 第 31 条以后的锁死占位正文（lorem ipsum）不得作为证据",
        ],
    },
    "last30days": {
        "id": "last30days",
        "category": "community",
        "kind": "cli",
        "title": "近30天跨平台社区情绪 (Reddit / X / StockTwits / Polymarket)",
        "operations": ["search"],
        "parameters": {
            "operation": "search（可选，默认 search）",
            "query": "检索主题（必填，不传 --search 以自动启用 StockTwits）",
            "days": "回溯天数（1-30，默认 30）",
            "quick": "快速模式（bool，可选）",
            "plan": "自定义检索计划对象（可选，subqueries 每项须同时含 search_query 与 ranking_query）",
        },
        "rules": [
            "覆盖 Reddit、X、YouTube、HN、Polymarket、StockTwits 跨平台情绪",
            "Polymarket 仅算市场定价信号，不算事实证据",
        ],
    },
    "x-timeline": {
        "id": "x-timeline",
        "category": "community",
        "kind": "cli",
        "title": "X 固定账号时间线 (xreach)",
        "operations": ["tweets", "tweet", "thread", "search"],
        "parameters": {
            "operation": "tweets | tweet | thread | search（可选，默认 tweets）",
            "handle": "X 账号（tweets 必填，接受 @user、user 或 x.com/twitter.com 主页 URL，规范化为 [A-Za-z0-9_]{1,15}）",
            "url": "单帖 URL（tweet/thread 与 tweet_id 二选一，仅允许 x.com / twitter.com 域名）",
            "tweet_id": "单帖 ID（tweet/thread 与 url 二选一，纯数字字符串）",
            "query": "搜索词（search 必填，<=300 字符；时间窗以 since:/until: 拼入 query）",
            "since": "窗口起点 YYYY-MM-DD（search 拼入 query；tweets 在结果里按日期过滤，含端点）",
            "until": "窗口终点 YYYY-MM-DD（同 since）",
            "count": "条数（1-100，默认 20；xreach 可能多返，bridge 按 count 截断）",
            "include_replies": "含回复（bool，仅 tweets；xreach search 无此选项）",
        },
        "rules": [
            "固定账号建\"不漏\"底表用它（按账号顺序全量拉取）；跨账号观点与媒体补充用 x_search",
            "x-timeline 与 x_search 的结果按原帖 URL 去重，同帖只计一次",
            "仅算情绪与线索；引用须带 @handle、日期与原帖 URL（source_attribution 已附）",
        ],
    },
}

# Canonical parameter alias mapping table at the bridge entry point.
# Maps common caller aliases to the interface's real canonical parameter names.
PARAM_ALIAS_TABLE: dict[str, str] = {
    # Stock / security code aliases -> canonical `ticker`
    "symbol": "ticker",
    "code": "ticker",
    "stock": "ticker",
    "stock_code": "ticker",
    "sec_code": "ticker",
    "security": "ticker",
    # Date window aliases -> canonical `start` / `end`
    "from": "start",
    "from_": "start",
    "from_date": "start",
    "date_from": "start",
    "start_date": "start",
    "begin": "start",
    "begin_date": "start",
    "since": "start",
    "to": "end",
    "to_date": "end",
    "date_to": "end",
    "end_date": "end",
    "until": "end",
    # Search query aliases -> canonical `query`
    "q": "query",
    "keyword": "query",
    "keywords": "query",
    "search_query": "query",
    "term": "query",
    "question": "query",
    "topic": "query",
    # Operation aliases -> canonical `operation`
    "op": "operation",
    "action": "operation",
    "method": "operation",
    "subcommand": "operation",
    "cmd": "operation",
    # Route aliases -> canonical `route`
    "source": "route",
    "channel": "route",
    "provider": "route",
    # Document ID aliases -> canonical `document`
    "doc_id": "document",
    "document_id": "document",
    "doc": "document",
    "id": "document",
    "accession": "document",
    "accession_number": "document",
    "file_id": "document",
    # Result limit aliases -> canonical `limit`
    "max_results": "limit",
    "size": "limit",
    "n": "limit",
    "top_k": "limit",
    "num_results": "limit",
    "per_page": "limit",
    "count": "limit",
    # SEC / finance parameter aliases
    "form_type": "form",
    "filing_type": "form",
    "report_type": "form",
    "section": "item",
    "item_no": "item",
    "part_item": "item",
    "xbrl_tag": "concept",
    "metric": "concept",
    "account": "concept",
    "as_of_date": "as_of",
    "point_in_time": "as_of",
    # Macro parameter aliases
    "series_id": "series",
    "indicator": "series",
    "fred_series": "series",
    "vintage_date": "vintage",
    "realtime_start": "vintage",
    "table": "dataset",
    "dataset_name": "dataset",
    # Community parameter aliases
    "user": "handle",
    "username": "handle",
    "screen_name": "handle",
    "author": "handle",
    "status_id": "post_id",
    "discussion_id": "post_id",
    "lookback_days": "days",
    "window_days": "days",
    "page_count": "pages",
    "page": "pages",
    "firm": "company",
    "employer": "company",
}


def _alias_prop(prop_type: str, target_desc: str) -> dict[str, Any]:
    return {"type": prop_type, "description": f"Alias for {target_desc}."}


_COMMON_ROUTE_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    k: _alias_prop("string", "'route'") for k in ("source", "channel", "provider")
}
_COMMON_OP_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    k: _alias_prop("string", "'operation'")
    for k in ("op", "action", "method", "subcommand", "cmd")
}
_COMMON_TICKER_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    k: _alias_prop("string", "'ticker'")
    for k in ("symbol", "code", "stock", "stock_code", "sec_code", "security")
}
_COMMON_DATE_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    **{
        k: _alias_prop("string", "'start' (or 'since' on x-timeline), YYYY-MM-DD")
        for k in ("from", "from_date", "date_from", "start_date", "begin", "begin_date", "since")
    },
    **{
        k: _alias_prop("string", "'end' (or 'until' on x-timeline), YYYY-MM-DD")
        for k in ("to", "to_date", "date_to", "end_date", "until")
    },
}
_COMMON_QUERY_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    k: _alias_prop("string", "'query'")
    for k in ("q", "keyword", "keywords", "search_query", "term", "question", "topic")
}
_COMMON_LIMIT_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    k: _alias_prop("integer", "'limit' (or 'count' on x-timeline, 'top' on market-regime)")
    for k in ("max_results", "size", "n", "top_k", "num_results", "per_page", "count")
}
_COMMON_DOC_ALIAS_PROPS: dict[str, dict[str, Any]] = {
    k: _alias_prop("string", "'document' (or 'params.doc_id' on alphaengine)")
    for k in ("doc_id", "document_id", "doc", "id", "accession", "accession_number", "file_id")
}

TOOL_SCHEMA_ALIAS_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "catalog": {
        **_COMMON_ROUTE_ALIAS_PROPS,
    },
    "finance": {
        **_COMMON_ROUTE_ALIAS_PROPS,
        **_COMMON_OP_ALIAS_PROPS,
        **_COMMON_TICKER_ALIAS_PROPS,
        **_COMMON_DATE_ALIAS_PROPS,
        **_COMMON_QUERY_ALIAS_PROPS,
        **_COMMON_LIMIT_ALIAS_PROPS,
        **_COMMON_DOC_ALIAS_PROPS,
        **{k: _alias_prop("string", "'form'") for k in ("form_type", "filing_type", "report_type")},
        **{k: _alias_prop("string", "'item'") for k in ("section", "item_no", "part_item")},
        **{k: _alias_prop("string", "'concept'") for k in ("xbrl_tag", "metric", "account")},
        **{k: _alias_prop("string", "'as_of'") for k in ("as_of_date", "point_in_time")},
    },
    "macro": {
        **_COMMON_ROUTE_ALIAS_PROPS,
        **_COMMON_OP_ALIAS_PROPS,
        **_COMMON_DATE_ALIAS_PROPS,
        **_COMMON_QUERY_ALIAS_PROPS,
        **_COMMON_LIMIT_ALIAS_PROPS,
        **_COMMON_TICKER_ALIAS_PROPS,
        **{k: _alias_prop("string", "'series'") for k in ("series_id", "indicator", "fred_series")},
        **{k: _alias_prop("string", "'vintage'") for k in ("vintage_date", "realtime_start")},
        **{k: _alias_prop("string", "'dataset'") for k in ("table", "dataset_name")},
    },
    "research": {
        **_COMMON_ROUTE_ALIAS_PROPS,
        **_COMMON_OP_ALIAS_PROPS,
        **_COMMON_TICKER_ALIAS_PROPS,
        **_COMMON_DATE_ALIAS_PROPS,
        **_COMMON_QUERY_ALIAS_PROPS,
        **_COMMON_LIMIT_ALIAS_PROPS,
        **_COMMON_DOC_ALIAS_PROPS,
    },
    "community": {
        **_COMMON_ROUTE_ALIAS_PROPS,
        **_COMMON_OP_ALIAS_PROPS,
        **_COMMON_TICKER_ALIAS_PROPS,
        **_COMMON_DATE_ALIAS_PROPS,
        **_COMMON_QUERY_ALIAS_PROPS,
        **_COMMON_LIMIT_ALIAS_PROPS,
        **_COMMON_DOC_ALIAS_PROPS,
        **{k: _alias_prop("string", "'company' on employee") for k in ("firm", "employer")},
        **{
            k: _alias_prop("string", "'handle' on x-timeline")
            for k in ("user", "username", "screen_name", "author", "account")
        },
        **{
            k: _alias_prop("string", "'post_id' on xueqiu or 'tweet_id' on x-timeline")
            for k in ("status_id", "discussion_id")
        },
        **{k: _alias_prop("integer", "'days' on last30days") for k in ("lookback_days", "window_days")},
        **{k: _alias_prop("integer", "'pages' on xueqiu/employee") for k in ("page_count", "page")},
    },
}

KNOWN_SCHEMA_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "route",
        "operation",
        "ticker",
        "query",
        "series",
        "concept",
        "item",
        "document",
        "form",
        "dataset",
        "start",
        "end",
        "as_of",
        "vintage",
        "limit",
        "timeout",
        "mode",
        "params",
        "top",
        "order",
        "category",
        "url",
        "post_id",
        "company",
        "pages",
        "allow_paid",
        "days",
        "quick",
        "plan",
        "handle",
        "tweet_id",
        "since",
        "until",
        "count",
        "include_replies",
    }
)

ROUTE_CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "sec": {
        "tool": "finance",
        "category": "finance",
        "operations": [
            "list-filings",
            "financials",
            "ratios",
            "facts",
            "smart-facts",
            "search-concepts",
            "search-text",
            "read-item",
            "attachments",
            "eight-k",
            "insider",
        ],
        "default_operation": "list-filings",
        "operation_aliases": {
            "list_filings": "list-filings",
            "filings": "list-filings",
            "list": "list-filings",
            "smart_facts": "smart-facts",
            "smartfacts": "smart-facts",
            "search_concepts": "search-concepts",
            "search_text": "search-text",
            "search": "search-text",
            "read_item": "read-item",
            "read": "read-item",
            "eight_k": "eight-k",
            "8-k": "eight-k",
            "8k": "eight-k",
        },
        "accepted_params": [
            "ticker",
            "query",
            "concept",
            "item",
            "document",
            "as_of",
            "form",
            "limit",
        ],
        "accepted_nested_params": [],
        "required_params": {
            "list-filings": ["ticker"],
            "financials": ["ticker"],
            "ratios": ["ticker"],
            "facts": ["ticker", "concept"],
            "smart-facts": ["ticker", "query"],
            "search-concepts": ["ticker", "query"],
            "search-text": ["ticker", "query"],
            "read-item": ["ticker", "item"],
            "attachments": ["ticker"],
            "eight-k": ["ticker"],
            "insider": ["ticker"],
        },
        "optional_params": ["form", "document", "as_of", "limit"],
        "param_formats": {
            "ticker": "美股代码（如 'META'、'NVDA'）",
            "query": "检索关键词（smart-facts / search-concepts / search-text 必填）",
            "as_of": "YYYY-MM-DD",
            "form": "10-K | 10-Q | 8-K | 20-F | 4",
            "item": "SEC 章节号（如 '1A'、'7'、'2.02'）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"ticker\": \"META\", \"rows\"|\"data\": ..., \"_sources\": [...], \"normalized_params\": {...}}",
        "examples": [
            "finance(route=\"sec\", operation=\"financials\", ticker=\"META\", form=\"10-K\", limit=5)",
            "finance(route=\"sec\", operation=\"smart-facts\", ticker=\"META\", query=\"revenue advertising\", limit=10)",
            "finance(route=\"sec\", operation=\"read-item\", ticker=\"META\", form=\"10-K\", item=\"7\")",
        ],
    },
    "estimates": {
        "tool": "finance",
        "category": "finance",
        "operations": ["consensus"],
        "default_operation": "consensus",
        "operation_aliases": {
            "estimates": "consensus",
            "estimate": "consensus",
            "get": "consensus",
        },
        "accepted_params": ["ticker", "limit"],
        "accepted_nested_params": [],
        "required_params": {"consensus": ["ticker"]},
        "optional_params": ["operation", "limit"],
        "param_formats": {
            "ticker": "美股代码（如 'META'、'NVDA'）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"ticker\": \"META\", \"data\": {\"consensus\": ...}, \"_sources\": [...], \"normalized_params\": {...}}",
        "examples": [
            "finance(route=\"estimates\", operation=\"consensus\", ticker=\"META\")",
        ],
    },
    "cn-hk": {
        "tool": "finance",
        "category": "finance",
        "operations": ["ask", "filings"],
        "default_operation": "filings",
        "operation_aliases": {
            "list-filings": "filings",
            "list_filings": "filings",
            "list": "filings",
            "qa": "ask",
            "query": "ask",
            "search": "ask",
        },
        "accepted_params": ["ticker", "query", "start", "end", "limit"],
        "accepted_nested_params": [],
        "required_params": {
            "filings": ["ticker", "start", "end"],
            "ask": ["query"],
        },
        "optional_params": ["operation", "ticker", "start", "end", "limit"],
        "param_formats": {
            "ticker": "港股用 5 位数字 '00700'（勿写 '0700.HK'，桥接层自动转为 '00700'）；A 股用 6 位数字 '600519'/'300750'",
            "start": "YYYY-MM-DD（filings 必填）",
            "end": "YYYY-MM-DD（filings 必填）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"ticker\": \"00700\", \"rows\"|\"data\": ..., \"_sources\": [...], \"normalized_params\": {...}}",
        "examples": [
            "finance(route=\"cn-hk\", operation=\"filings\", ticker=\"00700\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "finance(route=\"cn-hk\", operation=\"ask\", ticker=\"00700\", query=\"2026Q2 游戏收入与毛利率\")",
        ],
    },
    "fred": {
        "tool": "macro",
        "category": "macro",
        "operations": ["macro"],
        "default_operation": "macro",
        "operation_aliases": {
            "fred": "macro",
            "series": "macro",
            "get": "macro",
        },
        "accepted_params": ["series", "start", "end", "vintage", "limit"],
        "accepted_nested_params": [],
        "required_params": {"macro": ["series", "start", "end"]},
        "optional_params": ["operation", "vintage", "limit"],
        "param_formats": {
            "series": "FRED 序列 ID（如 'DGS10'、'SOFR'、'CPIAUCSL'）",
            "start": "YYYY-MM-DD（必填）",
            "end": "YYYY-MM-DD（必填）",
            "vintage": "YYYY-MM-DD（可选）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"series\": \"DGS10\", \"rows\": [{\"observation_date\": \"YYYY-MM-DD\", \"value\": ...}], \"_sources\": [...]}",
        "examples": [
            "macro(route=\"fred\", operation=\"macro\", series=\"DGS10\", start=\"2026-01-01\", end=\"2026-10-23\")",
        ],
    },
    "treasury": {
        "tool": "macro",
        "category": "macro",
        "operations": ["treasury"],
        "default_operation": "treasury",
        "operation_aliases": {
            "get": "treasury",
            "query": "treasury",
        },
        "accepted_params": ["dataset", "start", "end", "limit"],
        "accepted_nested_params": [],
        "required_params": {"treasury": ["dataset", "start", "end"]},
        "optional_params": ["operation", "limit"],
        "param_formats": {
            "dataset": "美国财政部数据集名：'tga' | 'debt'",
            "start": "YYYY-MM-DD（必填）",
            "end": "YYYY-MM-DD（必填）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"dataset\": \"tga\", \"rows\": [...], \"_sources\": [...]}",
        "examples": [
            "macro(route=\"treasury\", operation=\"treasury\", dataset=\"tga\", start=\"2026-01-01\", end=\"2026-10-23\", limit=20)",
        ],
    },
    "market-regime": {
        "tool": "macro",
        "category": "macro",
        "operations": ["snapshot", "us_topbottom", "crowding", "cn_leverage"],
        "default_operation": "snapshot",
        "operation_aliases": {
            "us-topbottom": "us_topbottom",
            "topbottom": "us_topbottom",
            "cn-leverage": "cn_leverage",
            "leverage": "cn_leverage",
        },
        "accepted_params": ["top", "order", "category", "query", "limit", "params"],
        "accepted_nested_params": [
            "top",
            "order",
            "category",
            "query",
            "limit",
            "operation",
        ],
        "required_params": {
            "snapshot": [],
            "us_topbottom": [],
            "crowding": [],
            "cn_leverage": [],
        },
        "optional_params": ["operation", "top", "order", "category", "query"],
        "param_formats": {
            "top": "整数 1-30（默认 10，仅 crowding）",
            "order": "'high' | 'low'（默认 'high'，仅 crowding）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"route\": \"market-regime\", \"operation\": \"snapshot\", \"date\": \"YYYY-MM-DD\", \"stale\": false, \"warnings\": [...], \"_sources\": [...]}",
        "examples": [
            "macro(route=\"market-regime\", operation=\"snapshot\")",
            "macro(route=\"market-regime\", operation=\"crowding\", top=10, order=\"high\")",
        ],
    },
    "all": {
        "tool": "research",
        "category": "research",
        "operations": ["search", "read", "list", "get_document"],
        "default_operation": "search",
        "operation_aliases": {
            "query": "search",
            "get": "read",
            "read_document": "read",
        },
        "accepted_params": [
            "query",
            "ticker",
            "start",
            "end",
            "document",
            "limit",
            "params",
        ],
        "accepted_nested_params": [
            "company",
            "sector",
            "tag",
            "sender",
            "layer",
            "sources",
            "search_mode",
            "no_decay",
            "offset",
            "max_chars",
            "media",
            "indexed_since",
            "keywords",
            "recency_bias",
        ],
        "required_params": {
            "search": ["query 或 ticker"],
            "read": ["document"],
            "list": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "ticker": "股票代码（如 'META'、'0700.HK'、'300750.SZ'）",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
            "document": "带库前缀的文档 ID（如 'wiki:123'、'mail:456'、'tmtb:789'、'funda:10'）",
            "params": "{\"offset\": >=0, \"max_chars\": 1..30000, \"sources\": [...], \"layer\": \"raw\"|\"digest\"}",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"alphaengine\": {...}, \"guidepoint\": {...}, \"source_status\": {...}, \"_sources\": [...]}",
        "examples": [
            "research(route=\"all\", operation=\"search\", query=\"META Reels AI\", ticker=\"META\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "research(route=\"all\", operation=\"read\", document=\"mail:111154\", params={\"offset\": 0, \"max_chars\": 30000})",
        ],
    },
    "alphaengine": {
        "tool": "research",
        "category": "research",
        "operations": ["search_library", "get_document", "auth_status", "list_filters"],
        "default_operation": "search_library",
        "operation_aliases": {
            "search": "search_library",
            "query": "search_library",
            "read": "get_document",
            "read_document": "get_document",
            "get": "get_document",
        },
        "accepted_params": ["query", "start", "end", "limit", "params", "document", "ticker"],
        "accepted_nested_params": [
            "doc_id",
            "query",
            "sort",
            "limit",
            "date_from",
            "date_to",
            "type_ids",
            "source_names",
            "companies",
            "industries",
            "include_snippets",
            "page",
            "pages",
            "offset",
            "max_chars",
        ],
        "required_params": {
            "search_library": ["query"],
            "get_document": ["params.doc_id（或顶层 document）"],
            "auth_status": [],
            "list_filters": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "query": "检索关键词（公司代码如 'META' 直接拼入 query）",
            "start": "YYYY-MM-DD（传入 start 时需与 end 成对传入）",
            "end": "YYYY-MM-DD",
            "params.doc_id": "纯数字字符串文档 ID（如 '99001122'；顶层传 document 时自动映射为 params.doc_id 并移除顶层 document）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"structuredContent\": {\"results\": [...]}}, \"_sources\": [...]}",
        "examples": [
            "research(route=\"alphaengine\", operation=\"search_library\", query=\"META AI capex\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "research(route=\"alphaengine\", operation=\"get_document\", params={\"doc_id\": \"99001122\", \"offset\": 0, \"max_chars\": 30000})",
        ],
    },
    "guidepoint": {
        "tool": "research",
        "category": "research",
        "operations": ["search_library"],
        "default_operation": "search_library",
        "operation_aliases": {
            "search": "search_library",
            "query": "search_library",
        },
        "accepted_params": ["query", "start", "end", "limit", "params", "ticker"],
        "accepted_nested_params": [
            "query",
            "start_date",
            "end_date",
            "size",
            "keywords",
            "recency_bias",
        ],
        "required_params": {"search_library": ["query"]},
        "optional_params": ["operation", "start", "end", "limit", "params"],
        "param_formats": {
            "query": "专家访谈检索词（如 'Meta Reels advertising'）",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
            "limit": "整数 1-40",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"structuredContent\": {\"data\": [...]}}, \"_sources\": [...]}",
        "examples": [
            "research(route=\"guidepoint\", operation=\"search_library\", query=\"Meta advertising Reels\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
        ],
    },
    "transcript": {
        "tool": "research",
        "category": "research",
        "operations": ["transcript"],
        "default_operation": "transcript",
        "operation_aliases": {
            "search": "transcript",
            "list": "transcript",
            "read": "transcript",
            "get": "transcript",
        },
        "accepted_params": ["ticker", "query", "start", "end", "limit"],
        "accepted_nested_params": [],
        "required_params": {"transcript": ["ticker"]},
        "optional_params": ["operation", "query", "start", "end", "limit"],
        "param_formats": {
            "ticker": "股票代码（必填，如 'META'、'GOOGL'、'00700'）",
            "query": "可选补充公司名，中英文用 | 分隔（如 '腾讯|Tencent'）；不能只传 query 不传 ticker，勿传主题词",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"transcripts\": [...]}, \"_sources\": [...]}（无匹配返回 ok: true, empty_reason: \"no_matching_transcripts\"）",
        "examples": [
            "research(route=\"transcript\", operation=\"transcript\", ticker=\"META\", start=\"2026-01-01\", end=\"2026-10-23\", limit=4)",
            "research(route=\"transcript\", operation=\"transcript\", ticker=\"00700\", query=\"腾讯|Tencent\", start=\"2026-01-01\", end=\"2026-10-23\", limit=4)",
        ],
    },
    "company-wiki": {
        "tool": "research",
        "category": "research",
        "operations": ["search", "read", "list", "topic"],
        "default_operation": "search",
        "operation_aliases": {
            "query": "search",
            "get": "read",
            "get_document": "read",
            "read_document": "read",
        },
        "accepted_params": [
            "query",
            "start",
            "end",
            "document",
            "limit",
            "params",
            "ticker",
        ],
        "accepted_nested_params": [
            "company",
            "sector",
            "tag",
            "search_mode",
            "no_decay",
            "offset",
            "max_chars",
            "media",
            "indexed_since",
        ],
        "required_params": {
            "search": ["query 或 params.company"],
            "read": ["document"],
            "list": [],
            "topic": ["query"],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "params.company": "股票代码（如 'META'、'0700.HK'、'300750.SZ'）",
            "document": "'wiki:<id>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}",
        "examples": [
            "research(route=\"company-wiki\", operation=\"search\", query=\"META advertising\", params={\"company\": \"META\"}, limit=10)",
            "research(route=\"company-wiki\", operation=\"read\", document=\"wiki:1632\", params={\"offset\": 0, \"max_chars\": 30000})",
        ],
    },
    "sales-notes": {
        "tool": "research",
        "category": "research",
        "operations": ["search", "read", "list"],
        "default_operation": "search",
        "operation_aliases": {
            "query": "search",
            "get": "read",
            "get_document": "read",
            "read_document": "read",
        },
        "accepted_params": [
            "query",
            "start",
            "end",
            "document",
            "limit",
            "params",
            "ticker",
        ],
        "accepted_nested_params": [
            "sender",
            "layer",
            "search_mode",
            "offset",
            "max_chars",
            "media",
        ],
        "required_params": {
            "search": ["query"],
            "read": ["document"],
            "list": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "document": "'mail:<id>' 或 'digest:<date>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}",
        "examples": [
            "research(route=\"sales-notes\", operation=\"search\", query=\"META bogey whisper\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "research(route=\"sales-notes\", operation=\"read\", document=\"mail:111154\", params={\"offset\": 0, \"max_chars\": 30000})",
        ],
    },
    "sales-mail": {
        "tool": "research",
        "category": "research",
        "operations": ["search", "read", "list"],
        "default_operation": "search",
        "operation_aliases": {
            "query": "search",
            "get": "read",
            "get_document": "read",
            "read_document": "read",
        },
        "accepted_params": [
            "query",
            "start",
            "end",
            "document",
            "limit",
            "params",
            "ticker",
        ],
        "accepted_nested_params": [
            "sender",
            "layer",
            "search_mode",
            "offset",
            "max_chars",
            "media",
        ],
        "required_params": {
            "search": ["query"],
            "read": ["document"],
            "list": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "document": "'mail:<id>' 或 'digest:<date>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}",
        "examples": [
            "research(route=\"sales-mail\", operation=\"search\", query=\"META bogey\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
        ],
    },
    "tmtb": {
        "tool": "research",
        "category": "research",
        "operations": ["search", "read", "list"],
        "default_operation": "search",
        "operation_aliases": {
            "query": "search",
            "get": "read",
            "get_document": "read",
            "read_document": "read",
        },
        "accepted_params": [
            "query",
            "start",
            "end",
            "document",
            "limit",
            "params",
            "ticker",
        ],
        "accepted_nested_params": [
            "sender",
            "layer",
            "search_mode",
            "offset",
            "max_chars",
            "media",
        ],
        "required_params": {
            "search": ["query"],
            "read": ["document"],
            "list": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "document": "'tmtb:<id>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}",
        "examples": [
            "research(route=\"tmtb\", operation=\"search\", query=\"META AI\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "research(route=\"tmtb\", operation=\"read\", document=\"tmtb:190000041634994\", params={\"offset\": 0, \"max_chars\": 30000})",
        ],
    },
    "funda-ai": {
        "tool": "research",
        "category": "research",
        "operations": ["search", "read", "list"],
        "default_operation": "search",
        "operation_aliases": {
            "query": "search",
            "get": "read",
            "get_document": "read",
            "read_document": "read",
        },
        "accepted_params": [
            "query",
            "start",
            "end",
            "document",
            "limit",
            "params",
            "ticker",
        ],
        "accepted_nested_params": [
            "sender",
            "layer",
            "search_mode",
            "offset",
            "max_chars",
            "media",
        ],
        "required_params": {
            "search": ["query"],
            "read": ["document"],
            "list": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "document": "'funda:<id>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}",
        "examples": [
            "research(route=\"funda-ai\", operation=\"search\", query=\"META GPU\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "research(route=\"funda-ai\", operation=\"read\", document=\"funda:26\", params={\"offset\": 0, \"max_chars\": 30000})",
        ],
    },
    "xueqiu": {
        "tool": "community",
        "category": "community",
        "operations": ["posts", "post", "comments", "search", "user"],
        "default_operation": "posts",
        "operation_aliases": {},
        "accepted_params": ["ticker", "url", "post_id", "query", "pages"],
        "accepted_nested_params": [],
        "required_params": {
            "posts": ["ticker"],
            "post": ["url"],
            "comments": ["post_id"],
            "search": ["query"],
            "user": ["url"],
        },
        "optional_params": ["pages"],
        "param_formats": {
            "ticker": "港股 5 位数字如 '00700'/'06181'（勿写 '0700.HK'）；A 股带前缀如 'SH600519'/'SZ300750'；美股如 'META'",
            "url": "仅允许 xueqiu.com 域名 URL",
            "post_id": "纯数字字符串",
            "pages": "整数 1-3（默认 1）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"posts\"|\"comments\"|\"results\": [...]}, \"_sources\": [...]}",
        "examples": [
            "community(route=\"xueqiu\", operation=\"posts\", ticker=\"00700\", pages=1)",
            "community(route=\"xueqiu\", operation=\"search\", query=\"腾讯 游戏\", pages=1)",
        ],
    },
    "employee": {
        "tool": "community",
        "category": "community",
        "operations": ["blind", "summary", "indeed", "glassdoor"],
        "default_operation": "blind",
        "operation_aliases": {},
        "accepted_params": ["company", "url", "pages", "allow_paid"],
        "accepted_nested_params": [],
        "required_params": {
            "blind": ["company"],
            "summary": ["company"],
            "indeed": ["company", "allow_paid=True"],
            "glassdoor": ["url", "allow_paid=True"],
        },
        "optional_params": ["pages", "allow_paid"],
        "param_formats": {
            "company": "公司英文名或 slug（如 'Meta'、'NVIDIA'）",
            "url": "仅允许 glassdoor.com 域名 URL",
            "pages": "blind/indeed 为 1-3，summary 为 1-40",
            "allow_paid": "布尔值（indeed/glassdoor 必须显式传 True）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"reviews\": [...]}, \"_sources\": [...]}",
        "examples": [
            "community(route=\"employee\", operation=\"blind\", company=\"Meta\", pages=1)",
            "community(route=\"employee\", operation=\"summary\", company=\"NVIDIA\", pages=40)",
        ],
    },
    "last30days": {
        "tool": "community",
        "category": "community",
        "operations": ["search"],
        "default_operation": "search",
        "operation_aliases": {},
        "accepted_params": ["query", "days", "quick", "plan"],
        "accepted_nested_params": [],
        "required_params": {"search": ["query"]},
        "optional_params": ["operation", "days", "quick", "plan"],
        "param_formats": {
            "query": "检索主题字符串（1-300 字符）",
            "days": "整数 1-30（默认 30）",
            "quick": "布尔值",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"items\": [...], \"source_status\": {...}}, \"_sources\": [...]}",
        "examples": [
            "community(route=\"last30days\", operation=\"search\", query=\"Meta Ray-Ban glasses\", days=30, quick=True)",
        ],
    },
    "x-timeline": {
        "tool": "community",
        "category": "community",
        "operations": ["tweets", "tweet", "thread", "search"],
        "default_operation": "tweets",
        "operation_aliases": {},
        "accepted_params": [
            "handle",
            "url",
            "tweet_id",
            "query",
            "since",
            "until",
            "count",
            "include_replies",
        ],
        "accepted_nested_params": [],
        "required_params": {
            "tweets": ["handle"],
            "tweet": ["url 或 tweet_id"],
            "thread": ["url 或 tweet_id"],
            "search": ["query"],
        },
        "optional_params": ["operation", "since", "until", "count", "include_replies"],
        "param_formats": {
            "handle": "@user、user 或 x.com/twitter.com 主页 URL",
            "since": "YYYY-MM-DD（start/from/start_date 自动映射为 since；禁用 params）",
            "until": "YYYY-MM-DD（end/to/end_date 自动映射为 until；禁用 params）",
            "count": "整数 1-100（默认 20；limit/max_results 自动映射为 count）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"data\": {\"items\": [{\"id\": ..., \"text\": ..., \"created_at\": ..., \"url\": ...}]}, \"_sources\": [...]}",
        "examples": [
            "community(route=\"x-timeline\", operation=\"tweets\", handle=\"@AIatMeta\", since=\"2026-10-01\", until=\"2026-10-23\", count=20)",
            "community(route=\"x-timeline\", operation=\"search\", query=\"Meta Llama\", since=\"2026-10-01\", until=\"2026-10-23\", count=20)",
        ],
    },
}

TOOL_VALID_ROUTES: dict[str, tuple[str, ...]] = {
    "finance": ("sec", "estimates", "cn-hk"),
    "macro": ("fred", "treasury", "market-regime"),
    "research": (
        "all",
        "alphaengine",
        "guidepoint",
        "transcript",
        "company-wiki",
        "sales-notes",
        "sales-mail",
        "tmtb",
        "funda-ai",
    ),
    "community": ("xueqiu", "employee", "last30days", "x-timeline"),
}

_HK_DOT_RE = re.compile(r"^0*(\d{1,5})\.HK$", re.IGNORECASE)
_CN_DOT_RE = re.compile(r"^(\d{6})\.(SH|SZ|SS)$", re.IGNORECASE)
_US_DOT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9.\-]{0,15})\.US$", re.IGNORECASE)


def _normalize_ticker_for_route(route: str, ticker: str) -> str:
    """Normalize stock code format to match the target route's expected representation."""
    s = ticker.strip()
    if not s:
        return s
    if route == "cn-hk":
        m_hk = _HK_DOT_RE.fullmatch(s)
        if m_hk:
            return f"{int(m_hk.group(1)):05d}"
        m_cn = _CN_DOT_RE.fullmatch(s)
        if m_cn:
            return m_cn.group(1)
        return s
    if route == "xueqiu":
        m_hk = _HK_DOT_RE.fullmatch(s)
        if m_hk:
            return f"{int(m_hk.group(1)):05d}"
        m_cn = _CN_DOT_RE.fullmatch(s)
        if m_cn:
            prefix = "SH" if m_cn.group(2).upper() in ("SH", "SS") else "SZ"
            return f"{prefix}{m_cn.group(1)}"
        return s
    if route in ("sec", "estimates", "transcript"):
        m_us = _US_DOT_RE.fullmatch(s)
        if m_us:
            return m_us.group(1).upper()
    return s


def _values_equal_for_alias(v1: Any, v2: Any) -> bool:
    if isinstance(v1, str) and isinstance(v2, str):
        return v1.strip() == v2.strip()
    return v1 == v2


def _build_normalized_params_view(args: dict[str, Any], *, is_community: bool = False) -> dict[str, Any]:
    """Build clean `normalized_params` dict showing the canonical parameters actually used."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if v is None:
            continue
        if k == "timeout":
            continue
        if is_community and k in UPSTREAM_GENERIC_COMMUNITY_PARAMS:
            continue
        if isinstance(v, dict):
            out[k] = {pk: pv for pk, pv in v.items() if pv is not None}
        else:
            out[k] = v
    return out


_NESTED_ALIAS_CONFLICT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("doc_id", ("doc_id", "document", "id", "document_id", "doc", "file_id")),
    ("start_date", ("start_date", "date_from", "from", "from_", "from_date", "start", "since")),
    ("end_date", ("end_date", "date_to", "to", "to_date", "end", "until")),
    ("limit", ("limit", "size", "max_results", "count", "top_k", "n")),
    ("offset", ("offset", "start_offset", "char_offset")),
    ("max_chars", ("max_chars", "max_length", "chars")),
)


def _normalize_and_validate_call_args(
    tool_name: str,
    raw_args: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Normalize parameter aliases, detect alias conflicts, and reject unknown/missing parameters at the bridge entry point.

    Returns `(normalized_args, normalized_params_view, error_payload_or_none)`.
    """
    route_keys = ("route", "source", "channel", "provider")
    route_entries = [(k, raw_args[k]) for k in route_keys if k in raw_args and raw_args[k] is not None]
    if len(route_entries) > 1 and not all(
        _values_equal_for_alias(v, route_entries[0][1]) for _, v in route_entries[1:]
    ):
        conflict_desc = ", ".join(f"{k}={v!r}" for k, v in route_entries)
        norm_view = _build_normalized_params_view(raw_args)
        err: dict[str, Any] = {
            "_declared_sources": [],
            "_sources": [],
            "ok": False,
            "status": "invalid_arguments",
            "error_code": "parameter_conflict",
            "error_category": "参数错误",
            "error": (
                f"[参数错误] 参数冲突：同一个参数 'route' 同时传入了不一致的值 ({conflict_desc})。"
                "请只传正名参数 'route'。"
            ),
            "hint": f"参数 'route' 同时收到别名与正名且值不一致（{conflict_desc}）；请统一使用标准参数名 'route' 重试。",
            "normalized_params": norm_view,
        }
        return dict(raw_args), norm_view, err

    route = str(route_entries[0][1]).strip() if route_entries else ""
    is_comm = tool_name == "community" or (tool_name != "catalog" and route in BRIDGE_COMMUNITY_ROUTES)

    eff_alias = dict(PARAM_ALIAS_TABLE)
    if route == "x-timeline":
        eff_alias.pop("since", None)
        eff_alias.pop("until", None)
        eff_alias.pop("count", None)
        for k in ("start", "from", "from_", "from_date", "date_from", "start_date", "begin", "begin_date"):
            eff_alias[k] = "since"
        for k in ("end", "to", "to_date", "date_to", "end_date"):
            eff_alias[k] = "until"
        for k in ("limit", "max_results", "size", "n", "top_k", "num_results", "per_page"):
            eff_alias[k] = "count"
        for k in ("id", "post_id", "status_id", "discussion_id", "doc_id", "document", "document_id"):
            eff_alias[k] = "tweet_id"
        for k in ("user", "username", "account", "screen_name", "author"):
            eff_alias[k] = "handle"
    elif route == "xueqiu":
        for k in ("id", "status_id", "discussion_id", "tweet_id", "doc_id"):
            eff_alias[k] = "post_id"
    elif route == "employee":
        for k in ("ticker", "symbol", "code", "stock", "stock_code", "firm", "employer"):
            eff_alias[k] = "company"
    elif route == "market-regime":
        for k in ("top_k", "n"):
            eff_alias[k] = "top"

    grouped: dict[str, list[tuple[str, Any]]] = {}
    none_keys: set[str] = set()
    for raw_k, raw_v in raw_args.items():
        if is_comm and raw_k in UPSTREAM_GENERIC_COMMUNITY_PARAMS:
            canon_k = raw_k
        else:
            canon_k = eff_alias.get(raw_k, raw_k)
        if raw_v is None:
            none_keys.add(canon_k)
            continue
        grouped.setdefault(canon_k, []).append((raw_k, raw_v))

    normalized: dict[str, Any] = {}
    for canon_k in none_keys:
        if canon_k not in grouped:
            normalized[canon_k] = None

    for canon_k, entries in grouped.items():
        if len(entries) > 1:
            first_val = entries[0][1]
            if not all(_values_equal_for_alias(v, first_val) for _, v in entries[1:]):
                conflict_desc = ", ".join(f"{k}={v!r}" for k, v in entries)
                partial_view = _build_normalized_params_view(normalized, is_community=is_comm)
                err = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "parameter_conflict",
                    "error_category": "参数错误",
                    "error": (
                        f"[参数错误] 参数冲突：同一个参数 '{canon_k}' 同时传入了不一致的值 ({conflict_desc})。"
                        f"请只保留标准参数名 '{canon_k}'。"
                    ),
                    "hint": (
                        f"参数 '{canon_k}' 存在正名与别名冲突（{conflict_desc}）；"
                        f"请统一使用标准参数名 '{canon_k}' 并只传一个值后重试。"
                    ),
                    "normalized_params": partial_view,
                }
                return dict(raw_args), partial_view, err
        chosen_val = next((v for k, v in entries if k == canon_k), entries[0][1])
        normalized[canon_k] = chosen_val

    spec = ROUTE_CONTRACT_SPECS.get(route)
    raw_op_val = normalized.get("operation")
    op_str = str(raw_op_val or "").strip()
    if spec and op_str:
        op_aliases = spec.get("operation_aliases") or {}
        if op_str in op_aliases:
            op_str = op_aliases[op_str]
            normalized["operation"] = op_str
        elif op_str.lower() in op_aliases:
            op_str = op_aliases[op_str.lower()]
            normalized["operation"] = op_str

    if isinstance(normalized.get("ticker"), str):
        normalized["ticker"] = _normalize_ticker_for_route(route, normalized["ticker"])

    # Validate nested `params` conflicts, top-level vs nested document conflicts, and alphaengine doc_id mapping
    if isinstance(normalized.get("params"), dict):
        p_dict = dict(normalized["params"])
        for group_label, group_keys in _NESTED_ALIAS_CONFLICT_GROUPS:
            present = [(k, p_dict[k]) for k in group_keys if k in p_dict and p_dict[k] is not None]
            if len(present) > 1 and not all(
                _values_equal_for_alias(v, present[0][1]) for _, v in present[1:]
            ):
                conflict_desc = ", ".join(f"params.{k}={v!r}" for k, v in present)
                norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                err = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "parameter_conflict",
                    "error_category": "参数错误",
                    "error": (
                        f"[参数错误] 嵌套参数冲突：params 内同一语义参数 '{group_label}' 同时传入了不一致的值 ({conflict_desc})。"
                    ),
                    "hint": f"params 内存在别名冲突（{conflict_desc}）；请只保留一个标准字段后重试。",
                    "normalized_params": norm_view,
                }
                return normalized, norm_view, err

        top_doc = normalized.get("document")
        if top_doc is not None and str(top_doc).strip():
            top_doc_str = str(top_doc).strip()
            for doc_k in ("document", "doc_id", "id", "document_id", "doc", "file_id"):
                if doc_k in p_dict and p_dict[doc_k] is not None and str(p_dict[doc_k]).strip():
                    if not _values_equal_for_alias(str(p_dict[doc_k]).strip(), top_doc_str):
                        conflict_desc = f"document={top_doc!r}, params.{doc_k}={p_dict[doc_k]!r}"
                        norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                        err = {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "invalid_arguments",
                            "error_code": "parameter_conflict",
                            "error_category": "参数错误",
                            "error": (
                                f"[参数错误] 参数冲突：顶层 document 与 params.{doc_k} 值不一致 ({conflict_desc})。"
                            ),
                            "hint": "请统一只传顶层 document（内部资料库）或 params.doc_id（AlphaEngine），不要同时传两个不同的值。",
                            "normalized_params": norm_view,
                        }
                        return normalized, norm_view, err

        if route == "alphaengine":
            for alias_doc_k in ("document", "id", "document_id", "doc", "file_id"):
                if alias_doc_k in p_dict:
                    alias_val = p_dict.pop(alias_doc_k)
                    if alias_val is not None and str(alias_val).strip() and not p_dict.get("doc_id"):
                        p_dict["doc_id"] = str(alias_val).strip()
            if top_doc is not None and str(top_doc).strip():
                if not p_dict.get("doc_id"):
                    p_dict["doc_id"] = str(top_doc).strip()
                normalized.pop("document", None)
            normalized["params"] = p_dict
    elif route == "alphaengine" and normalized.get("document") is not None:
        top_doc_str = str(normalized.pop("document")).strip()
        if top_doc_str:
            normalized["params"] = {"doc_id": top_doc_str}

    norm_view = _build_normalized_params_view(normalized, is_community=is_comm)

    # Validate unknown parameters and operations for catalog / finance / macro / research
    if tool_name == "catalog":
        allowed_cat = {"category", "route", "timeout"}
        extra_cat = sorted(k for k, v in normalized.items() if v is not None and k not in allowed_cat)
        if extra_cat:
            accepted_list = ["category", "route"]
            err = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "unknown_parameters",
                "error_category": "参数错误",
                "accepted_parameters": accepted_list,
                "error": (
                    f"[参数错误] catalog 收到未知参数: {extra_cat}。"
                    f"该操作接受的参数: {accepted_list}。"
                ),
                "hint": "catalog 仅接受可选参数 category（finance|macro|research|community|news）或 route（如 'sec'、'transcript'）。",
                "normalized_params": norm_view,
            }
            return normalized, norm_view, err
        return normalized, norm_view, None

    if tool_name in ("finance", "macro", "research", "community", "news") and not route:
        tool_routes = sorted(
            rid
            for rid, r_spec in ROUTE_CONTRACT_SPECS.items()
            if r_spec.get("tool") == tool_name
        )
        err = {
            "_declared_sources": [],
            "_sources": [],
            "ok": False,
            "status": "invalid_arguments",
            "error_code": "missing_required_parameters",
            "error_category": "参数错误",
            "missing_parameters": ["route"],
            "accepted_routes": tool_routes,
            "error": (
                f"[参数错误] {tool_name} 缺少必填参数 'route'（或其别名 'source'/'channel'/'provider'）。"
                f"可选路由: {tool_routes}。"
            ),
            "hint": (
                f"请传入必填参数 route（支持的值: {tool_routes}；也接受别名 source/channel/provider）后重试。"
            ),
            "normalized_params": norm_view,
        }
        return normalized, norm_view, err

    if is_comm:
        return normalized, norm_view, None

    if (
        tool_name in ("finance", "macro", "research")
        and spec is not None
        and spec.get("tool") == tool_name
    ):
        valid_ops: list[str] = list(spec.get("operations") or [])
        accepted_top: list[str] = sorted({"route", "operation", *spec.get("accepted_params", [])})
        if op_str and valid_ops and op_str not in valid_ops:
            err = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "invalid_operation",
                "error_category": "参数错误",
                "accepted_operations": valid_ops,
                "accepted_parameters": accepted_top,
                "error": (
                    f"[参数错误] 路由 '{route}' 不支持 operation='{op_str}'。"
                    f"支持的操作: {valid_ops}；接受的参数: {accepted_top}。"
                ),
                "hint": (
                    f"请将 operation 改为 {valid_ops} 之一（可调用 catalog(route='{route}') 查看完整参数说明与示例）。"
                ),
                "normalized_params": norm_view,
            }
            return normalized, norm_view, err

        allowed_set = set(accepted_top) | {"timeout", "mode"}
        unknown_top = sorted(
            k for k, v in normalized.items() if v is not None and k not in allowed_set
        )
        if unknown_top:
            err = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "unknown_parameters",
                "error_category": "参数错误",
                "accepted_parameters": accepted_top,
                "error": (
                    f"[参数错误] 路由 '{route}'（operation='{op_str or spec.get('default_operation', '')}'）"
                    f"收到不认识的参数: {unknown_top}。该操作接受的参数: {accepted_top}。"
                ),
                "hint": (
                    f"请移除或修正未知参数 {unknown_top}，仅使用 {accepted_top} 中的参数"
                    f"（不确定参数时先查 catalog(route='{route}')）。"
                ),
                "normalized_params": norm_view,
            }
            return normalized, norm_view, err

        # Validate unknown nested keys inside `params`
        if isinstance(normalized.get("params"), dict) and "params" in allowed_set:
            accepted_nested: list[str] = list(spec.get("accepted_nested_params") or [])
            allowed_nested_set = set(accepted_nested)
            if route in INTERNAL_LIBRARY_ROUTES:
                allowed_nested_set |= {"document", "doc_id", "id", "document_id"}
            unknown_nested = sorted(
                k
                for k, v in normalized["params"].items()
                if v is not None and k not in allowed_nested_set
            )
            if unknown_nested:
                err = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "unknown_parameters",
                    "error_category": "参数错误",
                    "accepted_parameters": accepted_top,
                    "accepted_nested_parameters": accepted_nested,
                    "error": (
                        f"[参数错误] 路由 '{route}' 的 params 收到未知嵌套参数: {unknown_nested}。"
                        f"params 接受的字段: {accepted_nested}。"
                    ),
                    "hint": (
                        f"请从 params 中移除未知字段 {unknown_nested}，仅使用 {accepted_nested}"
                        f"（不确定时先查 catalog(route='{route}')）。"
                    ),
                    "normalized_params": norm_view,
                }
                return normalized, norm_view, err

        # Validate required parameters aligned with upstream data_gateway.py
        eff_op = op_str or str(spec.get("default_operation") or "").strip()
        missing_req: list[str] = []
        if route == "fred":
            for req_k in ("series", "start", "end"):
                if not str(normalized.get(req_k) or "").strip():
                    missing_req.append(req_k)
        elif route == "treasury":
            for req_k in ("dataset", "start", "end"):
                if not str(normalized.get(req_k) or "").strip():
                    missing_req.append(req_k)
        elif route == "cn-hk":
            if eff_op == "filings":
                for req_k in ("ticker", "start", "end"):
                    if not str(normalized.get(req_k) or "").strip():
                        missing_req.append(req_k)
            elif eff_op == "ask":
                if not str(normalized.get("query") or "").strip():
                    missing_req.append("query")
        elif route == "sec":
            raw_op_orig = str(raw_args.get("operation") or "").strip().lower()
            if raw_op_orig != "search" and not str(normalized.get("ticker") or "").strip():
                missing_req.append("ticker")
            if eff_op == "facts" and not str(normalized.get("concept") or "").strip():
                missing_req.append("concept")
            elif eff_op in ("smart-facts", "search-concepts", "search-text") and not str(
                normalized.get("query") or ""
            ).strip():
                missing_req.append("query")
            elif eff_op == "read-item" and not str(normalized.get("item") or "").strip():
                missing_req.append("item")
        elif route == "estimates":
            if not str(normalized.get("ticker") or "").strip():
                missing_req.append("ticker")
        elif route == "transcript":
            if not str(normalized.get("ticker") or "").strip():
                missing_req.append("ticker")

        if missing_req:
            err = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "missing_required_parameters",
                "error_category": "参数错误",
                "missing_parameters": missing_req,
                "accepted_parameters": accepted_top,
                "error": (
                    f"[参数错误] 路由 '{route}'（operation='{eff_op}'）缺少必填参数: {missing_req}。"
                    f"该操作接受的参数: {accepted_top}。"
                ),
                "hint": (
                    f"请补齐必填参数 {missing_req} 后重试（可调用 catalog(route='{route}') 查看完整参数格式与示例）。"
                ),
                "normalized_params": norm_view,
            }
            return normalized, norm_view, err

    return normalized, norm_view, None


_BUSINESS_NO_MATCH_REASONS: frozenset[str] = frozenset(
    {
        "alphaengine_no_match",
        "no_match",
        "no_results",
        "empty",
        "no_matching_transcripts",
        "no_matching_records",
        "no_matching_items",
    }
)


def _is_business_no_match_reason(reason: Any) -> bool:
    """Return True when `reason` is an explicit business no-match code rather than an error."""
    return str(reason or "").strip().lower() in _BUSINESS_NO_MATCH_REASONS


_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "error",
        "failed",
        "forbidden",
        "unauthorized",
        "rate_limited",
        "timeout",
        "quota_exceeded",
        "script_error",
        "degraded",
        "canceled",
        "auth_expired",
        "invalid_output",
        "source_missing",
        "invalid_arguments",
        "not_found",
        "partial_error",
        "paid_source_not_allowed",
        "handoff_filtered",
    }
)
_GENERIC_FAILURE_STATUSES: frozenset[str] = frozenset({"error", "failed", "partial_error"})
_SPECIFIC_FAILURE_STATUSES: frozenset[str] = _FAILURE_STATUSES - _GENERIC_FAILURE_STATUSES


_NON_FAILURE_STATUS_WORDS = frozenset({"", "ok", "success", "succeeded", "empty", "no_match", "no-results", "no_results"})


def _pick_failure_status(*statuses: str) -> str:
    """Return the most specific failure status across layers; specific statuses (`forbidden`, `rate_limited`, `timeout`, ...) beat generic `error`/`failed`, and non-failure statuses (`ok`, `empty`, ...) are ignored."""
    generic_hit = ""
    for raw in statuses:
        st = str(raw or "").strip().lower()
        if not st or st not in _FAILURE_STATUSES:
            continue
        if st in _SPECIFIC_FAILURE_STATUSES:
            return st
        if not generic_hit:
            generic_hit = st
    return generic_hit


def _unwrap_upstream_error_info(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap nested error details and compute `failed: bool` across top-level, `data`, `data.structuredContent`, `data.content`, and `source_status`."""
    data_obj = payload.get("data")
    data_dict = data_obj if isinstance(data_obj, dict) else {}
    fb_obj = payload.get("fallback") if "fallback" in payload else data_dict.get("fallback")
    fb_dict = fb_obj if isinstance(fb_obj, dict) else {}
    fb_source = str(fb_dict.get("source") or "").strip().lower()
    fb_reason = str(fb_dict.get("reason") or "").strip().lower()
    data_reason = str(data_dict.get("reason") or "").strip().lower()
    raw_top_reason = str(payload.get("reason") or "").strip()
    top_reason = "" if _is_business_no_match_reason(raw_top_reason) else raw_top_reason
    top_status = str(payload.get("status") or "").strip()
    d_status = str(data_dict.get("status") or "").strip()
    top_code = str(payload.get("error_code") or "").strip()
    top_err = str(payload.get("error") or "").strip()
    d_err_raw = str(data_dict.get("error") or "").strip()
    ae_err = str(data_dict.get("alphaengine_error") or "").strip()

    is_alphaengine_no_match = (
        "alphaengine_no_match" in (fb_reason, data_reason, raw_top_reason.lower())
        and top_status.lower() not in _FAILURE_STATUSES
        and d_status.lower() not in _FAILURE_STATUSES
        and not top_err
        and not d_err_raw
        and not ae_err
        and payload.get("isError") is not True
        and data_dict.get("isError") is not True
    )

    is_top_err = (
        (payload.get("ok") is False and not is_alphaengine_no_match)
        or payload.get("isError") is True
        or bool(top_err)
        or top_status.lower() in _FAILURE_STATUSES
        or (fb_source == "roic" and not is_alphaengine_no_match)
    )
    if is_top_err and not top_err:
        top_msg = str(payload.get("message") or "").strip()
        if top_msg:
            top_err = top_msg

    unwrapped_err = ""
    status_candidates: list[str] = []
    if top_status.lower() in _FAILURE_STATUSES:
        status_candidates.append(top_status)
    unwrapped_code = ""
    is_data_err = False
    is_sc_err = False
    has_content_err = False

    if isinstance(data_obj, dict):
        d_reason = str(data_obj.get("reason") or "").strip()
        is_data_err = (
            is_top_err
            or (data_obj.get("ok") is False and not is_alphaengine_no_match)
            or data_obj.get("isError") is True
            or bool(d_err_raw)
            or bool(ae_err)
            or d_reason.lower() == "alphaengine_error"
            or d_status.lower() in _FAILURE_STATUSES
        )
        if ae_err:
            unwrapped_err = ae_err
        d_err = str(
            d_err_raw
            or (data_obj.get("message") if is_data_err else "")
            or ""
        ).strip()
        if d_err and not unwrapped_err:
            unwrapped_err = d_err
        if (
            d_reason
            and d_reason != "alphaengine_error"
            and not _is_business_no_match_reason(d_reason)
            and is_data_err
            and not unwrapped_err
        ):
            unwrapped_err = d_reason
        if d_status.lower() in _FAILURE_STATUSES:
            status_candidates.append(d_status)
        d_code = str(data_obj.get("error_code") or "").strip()
        if d_code:
            unwrapped_code = d_code

        sc = data_obj.get("structuredContent")
        if isinstance(sc, dict):
            sc_status = str(sc.get("status") or "").strip()
            sc_reason = str(sc.get("reason") or "").strip()
            sc_err_raw = str(sc.get("alphaengine_error") or sc.get("error") or "").strip()
            is_sc_err = (
                sc.get("ok") is False
                or sc.get("isError") is True
                or bool(sc_err_raw)
                or sc_reason.lower() == "alphaengine_error"
                or sc_status.lower() in _FAILURE_STATUSES
            )
            sc_err = str(
                sc_err_raw
                or (sc.get("message") if (is_sc_err or is_data_err) else "")
                or (
                    sc_reason
                    if (is_sc_err or is_data_err)
                    and sc_reason != "alphaengine_error"
                    and not _is_business_no_match_reason(sc_reason)
                    else ""
                )
                or ""
            ).strip()
            if sc_err and not unwrapped_err:
                unwrapped_err = sc_err
            if sc_status.lower() in _FAILURE_STATUSES:
                status_candidates.append(sc_status)
            sc_code = str(sc.get("error_code") or "").strip()
            if sc_code and not unwrapped_code:
                unwrapped_code = sc_code

        content_list = data_obj.get("content")
        if isinstance(content_list, list):
            for block in content_list:
                if not isinstance(block, dict):
                    continue
                if block.get("isError") is True:
                    has_content_err = True
                txt = str(block.get("text") or "").strip()
                if not txt:
                    continue
                try:
                    parsed_txt = json.loads(txt)
                except Exception:
                    parsed_txt = None
                if isinstance(parsed_txt, dict):
                    p_status = str(parsed_txt.get("status") or "").strip()
                    p_reason = str(parsed_txt.get("reason") or "").strip()
                    p_err_raw = str(parsed_txt.get("alphaengine_error") or parsed_txt.get("error") or "").strip()
                    is_pt_err = (
                        parsed_txt.get("ok") is False
                        or parsed_txt.get("isError") is True
                        or bool(p_err_raw)
                        or p_reason.lower() == "alphaengine_error"
                        or p_status.lower() in _FAILURE_STATUSES
                    )
                    if is_pt_err:
                        has_content_err = True
                    p_err = str(
                        p_err_raw
                        or (parsed_txt.get("message") if (is_pt_err or is_data_err) else "")
                        or (
                            p_reason
                            if (is_pt_err or is_data_err)
                            and p_reason != "alphaengine_error"
                            and not _is_business_no_match_reason(p_reason)
                            else ""
                        )
                        or ""
                    ).strip()
                    if p_err and not unwrapped_err:
                        unwrapped_err = p_err
                    if p_status.lower() in _FAILURE_STATUSES:
                        status_candidates.append(p_status)
                    p_code = str(parsed_txt.get("error_code") or "").strip()
                    if p_code and not unwrapped_code:
                        unwrapped_code = p_code
                elif (
                    block.get("isError") is True
                    or data_obj.get("isError") is True
                    or payload.get("ok") is False
                ) and not unwrapped_err:
                    unwrapped_err = txt

    sub_errors: list[str] = []
    for sub_key in ("alphaengine", "guidepoint"):
        sub_obj = (
            payload.get(sub_key)
            if isinstance(payload.get(sub_key), dict)
            else (data_dict.get(sub_key) if isinstance(data_dict.get(sub_key), dict) else None)
        )
        if isinstance(sub_obj, dict):
            sub_st = str(sub_obj.get("status") or sub_obj.get("state") or "").strip()
            if (
                sub_obj.get("ok") is False
                or sub_obj.get("isError") is True
                or bool(sub_obj.get("error"))
                or sub_st.lower() in _FAILURE_STATUSES
            ):
                sub_r = str(sub_obj.get("error") or sub_obj.get("reason") or sub_st or "failed").strip()
                sub_errors.append(f"{sub_key}: {sub_r}")
                if sub_st.lower() in _FAILURE_STATUSES:
                    status_candidates.append(sub_st)

    sc_dict = data_dict.get("structuredContent") if isinstance(data_dict.get("structuredContent"), dict) else {}
    for src_status in (
        payload.get("source_status"),
        data_dict.get("source_status"),
        sc_dict.get("source_status"),
    ):
        if not isinstance(src_status, dict):
            continue
        for s_name, s_val in src_status.items():
            if any(x.startswith(f"{s_name}:") for x in sub_errors):
                continue
            if isinstance(s_val, dict):
                s_st = str(s_val.get("status") or s_val.get("state") or "").strip()
                if (
                    s_val.get("ok") is False
                    or s_val.get("isError") is True
                    or bool(s_val.get("error"))
                    or s_st.lower() in _FAILURE_STATUSES
                ):
                    s_r = str(s_val.get("error") or s_val.get("reason") or s_st or "failed").strip()
                    sub_errors.append(f"{s_name}: {s_r}")
                    if s_st.lower() in _FAILURE_STATUSES:
                        status_candidates.append(s_st)
            elif isinstance(s_val, str):
                s_st = s_val.strip()
                if s_st.lower() in _FAILURE_STATUSES:
                    sub_errors.append(f"{s_name}: {s_st}")
                    status_candidates.append(s_st)

    eff_err = top_err
    if not eff_err or eff_err in ("调用失败", "alphaengine_error", "error", "failed"):
        if unwrapped_err:
            eff_err = unwrapped_err
        elif top_reason and top_reason != "alphaengine_error":
            eff_err = top_reason
        elif sub_errors:
            eff_err = f"部分数据源调用失败 ({'; '.join(sub_errors)})"
        elif top_reason:
            eff_err = top_reason
    elif unwrapped_err and unwrapped_err not in eff_err:
        eff_err = f"{eff_err} ({unwrapped_err})"

    failed = bool(
        is_top_err
        or is_data_err
        or is_sc_err
        or has_content_err
        or sub_errors
        or unwrapped_err
    )
    picked_failure_status = _pick_failure_status(*status_candidates)
    if failed:
        # A route's own failure status (e.g. market-regime's ``invalid_source``)
        # is more useful than a generic ``error``; only a success/empty status
        # is overwritten, since it contradicts the failure.
        keep_top = top_status if top_status.strip().lower() not in _NON_FAILURE_STATUS_WORDS else ""
        eff_status = picked_failure_status or keep_top or "error"
    else:
        eff_status = picked_failure_status or top_status
    eff_code = top_code or unwrapped_code
    return {
        "failed": failed,
        "error": eff_err,
        "status": eff_status,
        "error_code": eff_code,
        "combined_text": " ".join(filter(None, [top_err, top_reason, unwrapped_err, " ".join(sub_errors)])),
    }


def _hint_conflicts_with_category(category: str, hint: str) -> bool:
    """Return True when an upstream `hint` contradicts the classified `error_category`."""
    h_lower = hint.lower()
    if "roic" in h_lower:
        return True
    no_match_markers = ("无匹配", "未找到", "no match", "补充公司名重试", "没有匹配")
    if category != "没有匹配" and any(m in h_lower for m in no_match_markers):
        return True
    if category == "权限或额度" and any(m in h_lower for m in ("参数错误", "未知参数")):
        return True
    if category == "参数错误" and any(m in h_lower for m in ("暂不可用", "执行超时")):
        return True
    return False


def _classify_error_category_and_hint(
    tool_name: str,
    route: str,
    operation: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Unwrap real upstream error structures, classify into one of the 4 error categories, and build a consistent hint.

    Categories: `参数错误` | `上游不可用` | `权限或额度` | `没有匹配`.
    """
    unwrapped = _unwrap_upstream_error_info(payload)
    if unwrapped["error"] and (
        not payload.get("error")
        or str(payload.get("error")).strip() in ("调用失败", "alphaengine_error", "error", "failed")
    ):
        payload["error"] = unwrapped["error"]

    existing_cat = str(payload.get("error_category") or "").strip()
    status = str(unwrapped["status"] or payload.get("status") or "").strip().lower()
    err_code = str(unwrapped["error_code"] or payload.get("error_code") or "").strip().lower()
    err_text = str(unwrapped["combined_text"] or payload.get("error") or payload.get("reason") or "").strip()
    err_lower = err_text.lower()
    spec = ROUTE_CONTRACT_SPECS.get(route, {})
    accepted_params = spec.get("accepted_params") or []
    accepted_ops = spec.get("operations") or []

    if existing_cat in ("参数错误", "上游不可用", "权限或额度", "没有匹配"):
        category = existing_cat
    elif (
        status in ("invalid_arguments",)
        or err_code
        in (
            "invalid_arguments",
            "unknown_parameters",
            "parameter_conflict",
            "invalid_operation",
            "invalid_route",
            "missing_required_parameters",
        )
        or "参数" in err_text
        or "未知" in err_text
        or "alphaengine 文档" in err_lower
        or "unsupported" in err_lower
        or "is required" in err_lower
        or "缺少必填" in err_text
        or "不接受参数" in err_text
        or "不支持参数" in err_text
        or "must be" in err_lower
        or "format is invalid" in err_lower
        or "does not support" in err_lower
        or ("not allowed" in err_lower and status != "paid_source_not_allowed")
    ):
        category = "参数错误"
    elif (
        status
        in (
            "paid_source_not_allowed",
            "handoff_filtered",
            "auth_expired",
            "unauthorized",
            "forbidden",
            "quota_exceeded",
            "rate_limited",
        )
        or err_code in ("paid_source_not_allowed", "handoff_filtered")
        or bool(re.search(r"\b(?:401|403|429)\b", err_lower))
        or any(
            tok in err_lower
            for tok in (
                "unauthorized",
                "forbidden",
                "quota",
                "rate limit",
                "rate_limit",
                "too many requests",
                "auth_expired",
                "session expired",
                "login required",
                "paid source",
                "handoff route",
                "permission denied",
                "access denied",
            )
        )
    ):
        category = "权限或额度"
    elif status in ("empty", "no_match") and not payload.get("error"):
        category = "没有匹配"
    else:
        category = "上游不可用"

    existing_hint = str(payload.get("hint") or "").strip()
    if existing_hint and not _hint_conflicts_with_category(category, existing_hint):
        return category, existing_hint

    if category == "参数错误":
        if route and accepted_params:
            hint = (
                f"请核对路由 '{route}' 的参数名与格式（支持的操作: {accepted_ops}，接受的参数: {accepted_params}），"
                f"或先调用 catalog(route='{route}') 查看完整参数契约与调用示例后重试。"
            )
        else:
            hint = "请先调用 catalog() 查看目标路由支持的 operation 与参数列表，修正参数后重试。"
    elif category == "权限或额度":
        if status == "paid_source_not_allowed":
            hint = "employee 的 indeed/glassdoor 属于付费源，请改用免费操作 operation='blind' 或 'summary'，或在确认授权时显式传 allow_paid=True。"
        elif status == "handoff_filtered":
            hint = "该路由在 LangAlpha 中已禁用；网页新闻请改用内置 WebSearch/WebFetch 工具，X 检索请改用 community(route='x-timeline', operation='search', ...)。"
        else:
            hint = (
                f"路由 '{route or tool_name}' 出现权限、登录态或配额限制；请切换替代数据源（如内部库 company-wiki/sales-notes/tmtb 或 SEC 公告），"
                "若无替代源必须在报告中如实写明该数据源因权限/配额取数失败，禁止写成“没有相关信息”。"
            )
    elif category == "没有匹配":
        if route == "transcript":
            hint = TRANSCRIPT_NO_MATCH_HINT
        elif tool_name == "community":
            hint = (
                f"社区路由 '{route}' 未检索到匹配内容；可放宽关键词 query、调整时间窗或换用其他社区路由（xueqiu / employee / last30days / x-timeline）重试。"
            )
        else:
            hint = (
                f"路由 '{route or tool_name}' 未检索到匹配记录；请放宽关键词 query、扩大时间范围 start/end 或换用其他路由重试。"
            )
    else:
        hint = (
            f"路由 '{route or tool_name}' 上游服务暂不可用或执行超时；可缩小查询范围重试一次或切换备用路由，"
            "若仍失败必须在报告中如实披露该数据源取数失败，不得当成“没有数据”或“没有相关信息”。"
        )
    return category, hint


_FAILURE_SOURCE_STATES: frozenset[str] = _FAILURE_STATUSES


def _collect_multi_source_health(payload: dict[str, Any]) -> tuple[list[str], list[tuple[str, str]]]:
    """Return `(succeeded_sources, failed_sources_with_reason)` from `source_status`, `alphaengine`, and `guidepoint`."""
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    seen: set[str] = set()

    src_status = payload.get("source_status")
    if isinstance(src_status, dict):
        for s_name, s_val in src_status.items():
            name = str(s_name)
            seen.add(name)
            if isinstance(s_val, dict):
                st = str(s_val.get("status") or s_val.get("state") or "").strip().lower()
                if s_val.get("ok") is False or s_val.get("isError") is True or bool(s_val.get("error")) or st in _FAILURE_SOURCE_STATES:
                    reason = str(s_val.get("error") or s_val.get("reason") or st or "failed").strip()
                    failed.append((name, reason))
                elif s_val.get("ok") is True or st in ("ok", "empty", "no_match", "no_results"):
                    succeeded.append(name)
            elif isinstance(s_val, str):
                st = s_val.strip().lower()
                if st in _FAILURE_SOURCE_STATES:
                    failed.append((name, st))
                elif st in ("ok", "empty", "no_match", "no_results"):
                    succeeded.append(name)

    for sub_key in ("alphaengine", "guidepoint"):
        if sub_key in seen:
            continue
        sub_obj = payload.get(sub_key)
        if isinstance(sub_obj, dict):
            st = str(sub_obj.get("status") or "").strip().lower()
            if sub_obj.get("ok") is False or sub_obj.get("isError") is True or bool(sub_obj.get("error")) or st in _FAILURE_SOURCE_STATES:
                reason = str(sub_obj.get("error") or sub_obj.get("reason") or st or "failed").strip()
                failed.append((sub_key, reason))
            elif sub_obj.get("ok") is True or st in ("ok", "empty", "no_match"):
                succeeded.append(sub_key)

    return succeeded, failed


def _has_disqualifying_error_signals(payload: dict[str, Any]) -> bool:
    """Return True if `payload` carries any upstream error, failed sub-source, HTTP error status, or non-business failure."""
    unwrapped = _unwrap_upstream_error_info(payload)
    if unwrapped["failed"] or unwrapped["error"]:
        return True
    _succ, failed_sources = _collect_multi_source_health(payload)
    if failed_sources:
        return True
    return False


def _is_transcript_no_match_payload(payload: dict[str, Any]) -> bool:
    """Return True ONLY when a `transcript` response is a confirmed empty result (`ok: True`) or explicit `alphaengine_no_match`."""
    if _has_disqualifying_error_signals(payload):
        return False
    data_dict = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    fb_obj = payload.get("fallback") if "fallback" in payload else data_dict.get("fallback")
    fb_dict = fb_obj if isinstance(fb_obj, dict) else {}
    fb_reason = str(fb_dict.get("reason") or "").strip().lower()
    data_reason = str(data_dict.get("reason") or payload.get("reason") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()

    # Explicit business no-match from AlphaEngine transcript lookup (`alphaengine_no_match`)
    if fb_reason == "alphaengine_no_match" or data_reason == "alphaengine_no_match":
        transcripts_val = data_dict.get("transcripts") if "transcripts" in data_dict else payload.get("transcripts")
        if transcripts_val is None or (isinstance(transcripts_val, list) and len(transcripts_val) == 0):
            return True
        return False

    if status == "no_match" and payload.get("ok") is not False and data_dict.get("ok") is not False:
        return True

    # Any other `ok: False` or `status: "error"` (e.g. `{ok: False, status: "error", data: {transcripts: []}}`) must stay `ok: False`
    if payload.get("ok") is not True or data_dict.get("ok") is False or status == "error":
        return False

    if status == "empty":
        return True

    transcripts_val = data_dict.get("transcripts") if "transcripts" in data_dict else payload.get("transcripts")
    if isinstance(transcripts_val, list):
        return len(transcripts_val) == 0

    for container in (payload, data_dict):
        for key in ("results", "items"):
            val = container.get(key)
            if isinstance(val, list):
                return len(val) == 0
    return False


def _is_empty_non_community_payload(
    tool_name: str, route: str, operation: str, payload: dict[str, Any]
) -> bool:
    """Return True ONLY when a `finance`/`macro`/`research` payload succeeded with 0 records and no sub-source failures."""
    if tool_name not in ("finance", "macro", "research"):
        return False
    if route == "market-regime":
        return False
    if route == "transcript":
        return _is_transcript_no_match_payload(payload)
    if _has_disqualifying_error_signals(payload):
        return False
    if payload.get("ok") is not True:
        return False
    if operation in ("read", "get_document", "auth_status", "list_filters"):
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in ("no_match", "empty"):
        return True
    data_dict = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    sc_dict = (
        source_identity._unwrap_mcp_proxy_payload(data_dict)
        if isinstance(data_dict, dict) and data_dict
        else {}
    )
    for container in (payload, data_dict, sc_dict if isinstance(sc_dict, dict) else {}):
        if not isinstance(container, dict):
            continue
        for list_key in ("results", "rows", "transcripts", "filings", "observations", "data"):
            val = container.get(list_key)
            if isinstance(val, list):
                return len(val) == 0
    return False


def _apply_error_and_empty_contract(
    tool_name: str,
    route: str,
    operation: str,
    payload: dict[str, Any],
    normalized_params: dict[str, Any],
) -> None:
    """Mutate `payload` in-place to enforce `normalized_params`, `ok: False` error categories + `hint` when `failed=True` (failure takes priority over empty results), and `ok: True` + `empty_reason` on confirmed no-match."""
    payload["normalized_params"] = normalized_params

    unwrapped_check = _unwrap_upstream_error_info(payload)
    if tool_name != "community":
        succeeded_srcs, failed_srcs = _collect_multi_source_health(payload)
        if failed_srcs:
            payload["ok"] = False
            if route == "all" or succeeded_srcs:
                payload["partial"] = succeeded_srcs
            if str(payload.get("status") or "").strip().lower() in ("", "ok", "empty"):
                payload["status"] = "partial_error" if succeeded_srcs else "error"
            if not payload.get("error"):
                failed_desc = "; ".join(f"{name}: {reason}" for name, reason in failed_srcs)
                payload["error"] = f"部分数据源调用失败 ({failed_desc})"

    failed = bool(unwrapped_check["failed"] or payload.get("error"))
    if failed:
        data_dict = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        fb_obj = payload.get("fallback") if "fallback" in payload else data_dict.get("fallback")
        fb_dict = fb_obj if isinstance(fb_obj, dict) else {}
        fb_source = str(fb_dict.get("source") or "").strip().lower()
        fb_reason = str(fb_dict.get("reason") or "").strip().lower()
        data_reason = str(data_dict.get("reason") or payload.get("reason") or "").strip().lower()
        has_roic_failure = fb_source == "roic" and "alphaengine_no_match" not in (fb_reason, data_reason)

        payload["ok"] = False
        payload.pop("empty_reason", None)
        cur_st = str(payload.get("status") or "").strip().lower()
        unwrapped_st = str(unwrapped_check.get("status") or "").strip().lower()
        if cur_st in ("script_error", "degraded", "partial_error"):
            payload["status"] = cur_st
        elif unwrapped_st in _SPECIFIC_FAILURE_STATUSES:
            payload["status"] = unwrapped_st
        elif cur_st in _SPECIFIC_FAILURE_STATUSES:
            payload["status"] = cur_st
        elif unwrapped_st in _FAILURE_STATUSES:
            payload["status"] = unwrapped_st
        elif cur_st in _FAILURE_STATUSES:
            payload["status"] = cur_st
        elif cur_st not in _NON_FAILURE_STATUS_WORDS:
            # A route-specific failure word (market-regime's invalid_source,
            # source_missing) says more than a generic "error".
            payload["status"] = cur_st
        elif unwrapped_st not in _NON_FAILURE_STATUS_WORDS:
            payload["status"] = unwrapped_st
        else:
            payload["status"] = "error"
        if not payload.get("error"):
            if unwrapped_check["error"]:
                payload["error"] = unwrapped_check["error"]
            elif has_roic_failure:
                payload["error"] = str(
                    fb_dict.get("reason")
                    or data_dict.get("reason")
                    or "AlphaEngine 检索失败（roic 兜底已停用）"
                )
            elif payload["status"] not in ("error", "failed"):
                payload["error"] = f"调用失败 ({payload['status']})"
            else:
                payload["error"] = "调用失败"

        category, hint = _classify_error_category_and_hint(
            tool_name, route, operation, payload
        )
        payload["error_category"] = category
        unwrapped_err = _unwrap_upstream_error_info(payload)["error"]
        raw_err = str(
            payload.get("error")
            or payload.get("reason")
            or unwrapped_err
            or "调用失败"
        ).strip()
        if category not in raw_err:
            payload["error"] = f"[{category}] {raw_err}"
        else:
            payload["error"] = raw_err
        payload["hint"] = hint
        return

    # True no-match on non-community routes (`research`, `finance`, `macro`) -> `ok: True` + `empty_reason`
    if tool_name in ("finance", "macro", "research") and _is_empty_non_community_payload(
        tool_name, route, operation, payload
    ):
        payload["ok"] = True
        if str(payload.get("status") or "").strip().lower() in ("error", "no_match", ""):
            payload["status"] = "empty"
        payload.pop("fallback", None)
        if route == "transcript":
            payload["empty_reason"] = str(payload.get("empty_reason") or "no_matching_transcripts")
            payload.setdefault("results", [])
            if isinstance(payload.get("data"), dict):
                payload["data"]["ok"] = True
                payload["data"].pop("fallback", None)
                payload["data"].setdefault("transcripts", [])
            payload["hint"] = TRANSCRIPT_NO_MATCH_HINT
        else:
            payload["empty_reason"] = str(payload.get("empty_reason") or "no_matching_records")
            if "results" not in payload and "rows" not in payload and not isinstance(payload.get("data"), (dict, list)):
                payload["results"] = []
            if not payload.get("hint"):
                payload["hint"] = (
                    f"路由 '{route}' 查询成功但无匹配记录；可调整 query 关键词、扩大 start/end 时间范围或换路由重试。"
                )
        return

_TICKER_RE = re.compile(r"^[A-Za-z0-9._\-]{1,32}$")
_POST_ID_RE = re.compile(r"^[0-9]{1,32}$")
_COMPANY_RE = re.compile(r"^[A-Za-z0-9 .,_\\-]{1,100}$")
_ALLOWED_URL_DOMAINS: tuple[str, ...] = ("xueqiu.com", "glassdoor.com", "x.com", "twitter.com")


def _validate_community_url(url: Any, *, required_domain: str) -> str:
    """Validate and canonicalize URL, then enforce the strict domain allowlist (see `_ALLOWED_URL_DOMAINS`)."""
    if not isinstance(url, str):
        raise ValueError("url must be a string")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("url contains invalid whitespace or control characters")
    cleaned = url
    if not cleaned or len(cleaned) > 500:
        raise ValueError("url length must be between 1 and 500 characters")
    if "\\" in cleaned:
        raise ValueError("url must not contain backslashes")
    if "@" in cleaned:
        raise ValueError("url must not contain '@'")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ValueError("url contains invalid whitespace or control characters")
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("url scheme must be http or https")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ValueError("url must not contain credentials")
    raw_hostname = parsed.hostname or ""
    if "%" in (parsed.netloc or "") or "%" in raw_hostname:
        raise ValueError("url hostname must not contain percent-encoding")
    host = raw_hostname.lower().rstrip(".")
    if not host or ".." in host or any(
        ch not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for ch in host
    ):
        raise ValueError("url is missing or has invalid hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("url has invalid port") from exc
    canonical_netloc = f"{host}:{port}" if port is not None else host
    canonical_path = parsed.path or "/"
    canonical_url = urllib.parse.urlunsplit(
        (scheme, canonical_netloc, canonical_path, parsed.query, parsed.fragment)
    )
    reparsed = urllib.parse.urlparse(canonical_url)
    verified_host = (reparsed.hostname or "").lower()
    if (
        not verified_host
        or "%" in (reparsed.netloc or "")
        or "\\" in canonical_url
        or "@" in canonical_url
    ):
        raise ValueError("canonical url validation failed")
    if not any(
        verified_host == d or verified_host.endswith(f".{d}")
        for d in _ALLOWED_URL_DOMAINS
    ):
        raise ValueError(
            f"url domain '{verified_host}' is not allowed (only {', '.join(_ALLOWED_URL_DOMAINS)})"
        )
    req_dom = required_domain.lower().rstrip(".")
    if not (verified_host == req_dom or verified_host.endswith(f".{req_dom}")):
        raise ValueError(f"url domain '{verified_host}' must match '{req_dom}'")
    return canonical_url


def _validate_clean_text(val: Any, *, field_name: str, max_len: int) -> str:
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = val.strip()
    if not cleaned or len(cleaned) > max_len:
        raise ValueError(f"{field_name} length must be between 1 and {max_len} characters")
    if any(ord(ch) < 32 for ch in cleaned):
        raise ValueError(f"{field_name} contains control characters")
    if cleaned.startswith("-"):
        raise ValueError(f"{field_name} must not start with '-'")
    return cleaned


def _validate_int_range(
    val: Any, *, field_name: str, min_val: int, max_val: int, default: int
) -> int:
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{field_name} must be an integer")
    if val < min_val or val > max_val:
        raise ValueError(f"{field_name} must be between {min_val} and {max_val}")
    return val


_X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_YMD_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_XREACH_CREATED_AT_FMT = "%a %b %d %H:%M:%S %z %Y"
_X_QUOTED_ID_KEYS: tuple[str, ...] = (
    "quotedTweetId",
    "quoted_tweet_id",
    "quotedId",
    "quotedStatusId",
    "quoted_status_id",
)


def _normalize_x_handle(raw: Any) -> str:
    """Normalize a handle input (`@user`, `user`, or an x.com/twitter.com profile URL) to a bare screen name."""
    if not isinstance(raw, str):
        raise ValueError("handle must be a string")
    cleaned = raw.strip()
    if cleaned.startswith(("http://", "https://")):
        canonical: str | None = None
        for dom in ("x.com", "twitter.com"):
            try:
                canonical = _validate_community_url(cleaned, required_domain=dom)
                break
            except ValueError:
                continue
        if canonical is None:
            raise ValueError("handle URL domain must be x.com or twitter.com")
        path = (urllib.parse.urlsplit(canonical).path or "").strip("/")
        cleaned = path.split("/", 1)[0] if path else ""
    cleaned = cleaned.lstrip("@")
    if not _X_HANDLE_RE.fullmatch(cleaned):
        raise ValueError(
            "handle must match [A-Za-z0-9_]{1,15} (accepts @user, user, or profile URL)"
        )
    return cleaned


def _validate_x_date(val: Any, *, field_name: str) -> str:
    if not isinstance(val, str):
        raise ValueError(f"{field_name} must be a string")
    cleaned = val.strip()
    if not _YMD_RE.fullmatch(cleaned):
        raise ValueError(f"{field_name} must be YYYY-MM-DD")
    try:
        datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid calendar date") from exc
    return cleaned


def _extract_x_tweet_target(clean_args: dict[str, Any]) -> str:
    """Return the tweet URL or numeric id targeting a single tweet/thread (exactly one of the two)."""
    url = clean_args.get("url")
    tweet_id = clean_args.get("tweet_id")
    if (url is None) == (tweet_id is None):
        raise ValueError("exactly one of url or tweet_id is required")
    if url is not None:
        canonical: str | None = None
        for dom in ("x.com", "twitter.com"):
            try:
                canonical = _validate_community_url(url, required_domain=dom)
                break
            except ValueError:
                continue
        if canonical is None:
            raise ValueError("url domain must be x.com or twitter.com")
        return canonical
    if not isinstance(tweet_id, str):
        raise ValueError("tweet_id must be a string")
    tid = tweet_id.strip()
    if not _POST_ID_RE.fullmatch(tid):
        raise ValueError("tweet_id must be a numeric string (1-32 digits)")
    return tid


def _parse_xreach_created_at(raw: Any) -> tuple[str, str]:
    """Parse xreach `createdAt` (`Wed Oct 21 14:54:03 +0000 2026`) to `(ISO-8601, YYYY-MM-DD)`; falls back to raw text."""
    text = str(raw or "").strip()
    if not text:
        return "", ""
    for fmt in (_XREACH_CREATED_AT_FMT, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        candidate = text[:-1] + "+0000" if fmt.endswith("%z") and text.endswith("Z") else text
        try:
            dt = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return dt.isoformat(timespec="seconds"), dt.strftime("%Y-%m-%d")
    return text, text[:10] if _YMD_RE.fullmatch(text[:10]) else ""


def _x_metric_int(val: Any) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _normalize_xreach_item(item: Any) -> dict[str, Any] | None:
    """Map one xreach tweet object to the bridge-normalized `{id, url, author_handle, ...}` shape."""
    if not isinstance(item, dict):
        return None
    tid = str(item.get("id") or "").strip()
    if not tid:
        return None
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    handle = str(user.get("screenName") or "").strip() or "unknown"
    name = str(user.get("name") or "").strip() or handle
    iso_dt, day = _parse_xreach_created_at(item.get("createdAt"))
    quoted_id = ""
    for qk in _X_QUOTED_ID_KEYS:
        qv = item.get(qk)
        if qv is not None and str(qv).strip():
            quoted_id = str(qv).strip()
            break
    metrics_raw = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    attribution_day = f" ({day}; status {tid})" if day else f" (status {tid})"
    return {
        "id": tid,
        "url": f"https://x.com/{handle}/status/{tid}",
        "author_handle": handle,
        "author_name": name,
        "created_at": iso_dt,
        "text": str(item.get("text") or item.get("full_text") or "").strip(),
        "lang": str(item.get("lang") or "").strip(),
        "metrics": {
            "likes": _x_metric_int(metrics_raw.get("likes", item.get("likeCount"))),
            "reposts": _x_metric_int(metrics_raw.get("reposts", item.get("retweetCount"))),
            "replies": _x_metric_int(metrics_raw.get("replies", item.get("replyCount"))),
            "views": _x_metric_int(metrics_raw.get("views", item.get("viewCount"))),
        },
        "is_reply": bool(item.get("isReply")),
        "is_repost": bool(item.get("isRetweet")),
        "quoted_id": quoted_id or None,
        "source_attribution": f"**X**, @{handle}{attribution_day}",
        "_date": day,
    }


def _normalize_x_timeline_output(
    parsed: Any, *, operation: str, count: int, since: str, until: str
) -> dict[str, Any]:
    """Normalize raw xreach JSON into `{items, returned, has_more}`.

    xreach ignores an exact `-n` bound (observed: `-n 3` returned 15 items), so the bridge
    truncates to `count` itself; `tweets` additionally filters items to the `since`/`until`
    window (inclusive) before truncation.
    """
    if isinstance(parsed, dict):
        raw_items = parsed.get("items") if isinstance(parsed.get("items"), list) else [parsed]
        has_more = bool(parsed.get("hasMore"))
    elif isinstance(parsed, list):
        raw_items = parsed
        has_more = False
    else:
        raw_items = []
        has_more = False
    items = [n for n in (_normalize_xreach_item(x) for x in raw_items) if n is not None]
    if operation == "tweets" and (since or until):
        items = [
            it
            for it in items
            if (not since or it["_date"] >= since) and (not until or it["_date"] <= until)
        ]
    truncated = False
    if operation in ("tweets", "search"):
        truncated = len(items) > count
        items = items[:count]
    for it in items:
        it.pop("_date", None)
    return {
        "items": items,
        "returned": len(items),
        "has_more": has_more or truncated,
    }


def _validate_last30days_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or not plan:
        raise ValueError("plan must be a non-empty JSON object")
    subqueries = plan.get("subqueries")
    if not isinstance(subqueries, list) or not subqueries or len(subqueries) > 10:
        raise ValueError("plan.subqueries must be a non-empty list (max 10 items)")
    for idx, sq in enumerate(subqueries):
        if not isinstance(sq, dict):
            raise ValueError(f"plan.subqueries[{idx}] must be an object")
        s_q = sq.get("search_query")
        r_q = sq.get("ranking_query")
        if not isinstance(s_q, str) or not s_q.strip() or len(s_q.strip()) > 300:
            raise ValueError(f"plan.subqueries[{idx}].search_query must be a non-empty string (<=300 chars)")
        if not isinstance(r_q, str) or not r_q.strip() or len(r_q.strip()) > 300:
            raise ValueError(f"plan.subqueries[{idx}].ranking_query must be a non-empty string (<=300 chars)")
    encoded = json.dumps(plan, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 32768:
        raise ValueError("plan exceeds 32 KiB limit")
    return plan


def _is_within_openclaw_tree(target_dir: Path, ocw_root: Path) -> bool:
    """Return True if resolved `target_dir` falls anywhere under the real `~/.openclaw` tree or `ocw_root`."""
    real_target = target_dir.expanduser().resolve()
    forbidden_roots: set[Path] = {
        Path("~/.openclaw").expanduser().resolve(),
        ocw_root.expanduser().resolve(),
    }
    resolved_ocw = ocw_root.expanduser().resolve()
    if resolved_ocw.name == "workspace" and resolved_ocw.parent != resolved_ocw:
        forbidden_roots.add(resolved_ocw.parent)
    for anc in (resolved_ocw, *resolved_ocw.parents):
        if anc.name == ".openclaw":
            forbidden_roots.add(anc)
    for root in forbidden_roots:
        if real_target == root or root in real_target.parents:
            return True
    return False


def _extract_last30days_source_state(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip().lower()
    if isinstance(entry, dict):
        return str(entry.get("state") or entry.get("status") or "").strip().lower()
    return ""


def _has_last30days_items(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    for list_key in ("results", "ranked_candidates", "items", "clusters"):
        if isinstance(parsed.get(list_key), list) and len(parsed[list_key]) > 0:
            return True
    if isinstance(parsed.get("items_by_source"), dict):
        for src_items in parsed["items_by_source"].values():
            if isinstance(src_items, list) and len(src_items) > 0:
                return True
    return False


def _is_last30days_source_entry_failed(entry: Any) -> bool:
    if isinstance(entry, dict) and (
        entry.get("ok") is False
        or entry.get("isError") is True
        or bool(entry.get("error"))
    ):
        return True
    return _extract_last30days_source_state(entry) in _FAILURE_STATUSES


def _is_all_last30days_no_results(raw_src_status: Any) -> bool:
    """Return True only when `source_status` is a non-empty dict where every source state is `no-results` and none flagged an error."""
    if not isinstance(raw_src_status, dict) or not raw_src_status:
        return False
    for src_val in raw_src_status.values():
        if _is_last30days_source_entry_failed(src_val):
            return False
        if _extract_last30days_source_state(src_val) != "no-results":
            return False
    return True


def _is_empty_community_output(route: str, operation: str, parsed: Any) -> bool:
    if parsed is None:
        return True
    if isinstance(parsed, list):
        return len(parsed) == 0
    if not isinstance(parsed, dict) or not parsed:
        return True

    if route == "xueqiu":
        for list_key in ("posts", "comments", "results", "items"):
            if isinstance(parsed.get(list_key), list):
                return len(parsed[list_key]) == 0
        if operation == "post":
            return not any(parsed.get(k) for k in ("id", "text", "title", "url"))
        return False

    if route == "employee":
        if isinstance(parsed.get("reviews"), list):
            return len(parsed["reviews"]) == 0
        if operation == "summary":
            return not (
                parsed.get("count")
                or parsed.get("monthly_ratings")
                or parsed.get("top_pros_terms")
                or parsed.get("top_cons_terms")
                or parsed.get("overall_rating")
            )
        for list_key in ("results", "items"):
            if isinstance(parsed.get(list_key), list):
                return len(parsed[list_key]) == 0
        return False

    if route == "last30days":
        if _has_last30days_items(parsed):
            return False
        return _is_all_last30days_no_results(parsed.get("source_status"))

    if route == "x-timeline":
        if isinstance(parsed.get("items"), list):
            return len(parsed["items"]) == 0
        return True

    return False


class ResearchDataBridge:
    """HTTP-to-stdio MCP bridge for OpenClaw `researchdata`.

    Normalizes `params.offset` (>= 0) and `params.max_chars` (1..30000), guides internal-library
    document IDs vs numeric AlphaEngine IDs, and prevents topic queries from derailing `transcript`.
    """

    CATALOG_DESCRIPTION_SUFFIX: str = CATALOG_DESCRIPTION_SUFFIX
    FINANCE_DESCRIPTION_SUFFIX: str = FINANCE_DESCRIPTION_SUFFIX
    MACRO_DESCRIPTION_SUFFIX: str = MACRO_DESCRIPTION_SUFFIX
    RESEARCH_DESCRIPTION_SUFFIX: str = (
        "\n\nNote: "
        + RESEARCH_EVIDENCE_AND_ROUTE_GUIDE
        + RESEARCH_ROUTE_PARAM_CONTRACT_SUFFIX
    )
    COMMUNITY_DESCRIPTION_SUFFIX: str = COMMUNITY_DESCRIPTION_SUFFIX
    NEWS_DESCRIPTION_SUFFIX: str = NEWS_DESCRIPTION_SUFFIX

    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.sem = asyncio.Semaphore(cfg.max_inflight)
        self.embed_sem = asyncio.Semaphore(cfg.embed_max_inflight)
        self.community_sems: dict[str, asyncio.Semaphore] = {
            r: asyncio.Semaphore(limit)
            for r, limit in COMMUNITY_ROUTE_CONCURRENCY.items()
        }
        self.server_version: str = "unknown"
        self._cached_tools: list[dict[str, Any]] | None = None
        self._cached_version: str | None = None
        self._usage_lock = asyncio.Lock()
        self._resolved_embed_model: str | None = None
        self._resolved_embed_dims: int | None = None
        self._file_cache: dict[str, tuple[int, int, Any]] = {}
        self._xreach_version_cache: tuple[float, dict[str, Any]] | None = None
        self._archive_run_lock = asyncio.Lock()
        self._archive_proc: Any = None
        self._archive_watch_task: asyncio.Task[Any] | None = None
        self._archive_last_status: dict[str, Any] = {}
        if not getattr(cfg, "admin_token", ""):
            logger.warning(
                "DALTON_BRIDGE_ADMIN_TOKEN is not set (checked env and ~/.config/dalton/researchdata-bridge.env); "
                "all admin endpoints (/embed, /jobs/*) will reject requests (fail closed)"
            )

    def handoff_routes(self) -> set[str]:
        """Return route IDs whose `kind` is `agent_tool` or `skill` in `data_sources.json`, excluding bridge-executed community routes."""
        try:
            raw = json.loads(
                self.cfg.data_sources_json.read_text(encoding="utf-8")
            )
            routes = raw.get("routes") if isinstance(raw, dict) else []
            if not isinstance(routes, list):
                return set()
            return {
                str(r["id"])
                for r in routes
                if isinstance(r, dict)
                and r.get("id")
                and r.get("kind") in ("agent_tool", "skill")
                and str(r["id"]) not in BRIDGE_COMMUNITY_ROUTES
            }
        except Exception as exc:
            logger.warning("Failed to load data_sources.json: %s", type(exc).__name__)
            return {
                "web-news",
                "web-page",
                "filings",
                "agent-reach",
                "x-search",
            }

    def compute_timeout(self, tool_name: str, arguments: dict[str, Any]) -> int:
        """Compute clamped timeout in seconds for a tool call."""
        ceiling = max(10, self.cfg.caller_budget_s - 25)
        if tool_name == "catalog":
            default_s, max_s = ROUTE_TIMEOUTS["catalog"]
        else:
            route = str(arguments.get("route") or "").strip()
            if route in BRIDGE_COMMUNITY_ROUTES:
                default_s, max_s = ROUTE_TIMEOUTS[route]
                return max(5, min(default_s, max_s, ceiling))
            op = str(arguments.get("operation") or arguments.get("action") or "").strip()
            key = f"{route}:{op}"
            if key in ROUTE_TIMEOUTS:
                default_s, max_s = ROUTE_TIMEOUTS[key]
            elif op in LONG_READ_OPERATIONS:
                default_s, max_s = (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S)
            else:
                default_s, max_s = ROUTE_TIMEOUTS.get(
                    route, (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S)
                )

        req_timeout = arguments.get("timeout")
        if isinstance(req_timeout, int) and req_timeout > 0:
            chosen = min(req_timeout, max_s)
        else:
            chosen = default_s
        return max(5, min(chosen, max_s, ceiling))

    async def _run_stdio_rpc(
        self, requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        """Spawn a single `researchdata_mcp.py` subprocess, send JSON-RPC lines, and read responses."""
        proc = await asyncio.create_subprocess_exec(
            self.cfg.python_bin,
            str(self.cfg.mcp_script),
            cwd=str(self.cfg.ocw_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            payload_bytes = (
                "\n".join(json.dumps(r, ensure_ascii=False) for r in requests)
                + "\n"
            ).encode("utf-8")
            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=payload_bytes),
                timeout=timeout_s,
            )
        except BaseException:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await _reap_process(proc)
            raise

        responses: list[dict[str, Any]] = []
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    responses.append(parsed)
            except Exception:
                continue
        if not responses:
            # R3-1: NEVER decode or log stderr content; log/report only exception class and exit code
            logger.warning(
                "researchdata_mcp subprocess returned no JSON response (exit_code=%s)",
                proc.returncode,
            )
            raise SubprocessExitError(proc.returncode)
        return responses

    async def ensure_initialized(self) -> dict[str, Any]:
        """Run `initialize` against `researchdata_mcp.py` and track `server_version`."""
        reqs = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "dalton-researchdata-bridge", "version": "0.1.0"},
                },
            }
        ]
        resps = await self._run_stdio_rpc(reqs, timeout_s=15.0)
        init_res = (resps[0].get("result") or {}) if resps else {}
        srv_info = init_res.get("serverInfo") if isinstance(init_res, dict) else {}
        ver = str((srv_info or {}).get("version") or "unknown")
        if self._cached_version and self._cached_version != ver:
            logger.info(
                "researchdata server_version changed (%s -> %s); invalidating tools/list cache",
                self._cached_version,
                ver,
            )
            self._cached_tools = None
        self.server_version = ver
        self._cached_version = ver
        return init_res

    async def get_filtered_tools(self) -> list[dict[str, Any]]:
        """Fetch `tools/list` from `researchdata_mcp.py` and strip handoff routes."""
        await self.ensure_initialized()
        if self._cached_tools is not None:
            return self._cached_tools

        reqs = [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
        resps = await self._run_stdio_rpc(reqs, timeout_s=15.0)
        raw_tools = (
            ((resps[0].get("result") or {}).get("tools") or []) if resps else []
        )
        handoff = self.handoff_routes()
        filtered: list[dict[str, Any]] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            tool_copy = json.loads(json.dumps(tool))
            props = (
                tool_copy.get("inputSchema", {}).get("properties", {})
                if isinstance(tool_copy.get("inputSchema"), dict)
                else {}
            )
            route_prop = props.get("route")
            tool_name = str(tool_copy.get("name") or "")
            if isinstance(route_prop, dict) and isinstance(route_prop.get("enum"), list):
                if tool_name == "community":
                    kept = [r for r in route_prop["enum"] if r not in handoff]
                    for comm_r in ("xueqiu", "employee", "last30days", "x-timeline"):
                        if comm_r not in kept:
                            kept.append(comm_r)
                    route_prop["enum"] = kept
                else:
                    route_prop["enum"] = [
                        r
                        for r in route_prop["enum"]
                        if r not in handoff and r not in BRIDGE_COMMUNITY_ROUTES
                    ]
            if tool_name == "research":
                desc = str(tool_copy.get("description") or "").rstrip()
                if self.RESEARCH_DESCRIPTION_SUFFIX.strip() not in desc:
                    tool_copy["description"] = desc + self.RESEARCH_DESCRIPTION_SUFFIX
                if isinstance(props, dict) and isinstance(props.get("params"), dict):
                    p_desc = str(props["params"].get("description") or "").rstrip()
                    if "1-30000" not in p_desc and "1–30000" not in p_desc:
                        props["params"]["description"] = (
                            f"{p_desc} 分页范围：offset >= 0，max_chars 1–30000。"
                        ).strip()
            elif tool_name == "community":
                desc = str(tool_copy.get("description") or "").rstrip()
                if self.COMMUNITY_DESCRIPTION_SUFFIX.strip() not in desc:
                    tool_copy["description"] = desc + self.COMMUNITY_DESCRIPTION_SUFFIX
                if isinstance(props, dict):
                    for gen_key in UPSTREAM_GENERIC_COMMUNITY_PARAMS:
                        if isinstance(props.get(gen_key), dict):
                            props[gen_key].pop("default", None)
                    props.setdefault(
                        "operation",
                        {
                            "type": "string",
                            "description": "xueqiu: posts|post|comments|search|user; employee: blind|summary|indeed|glassdoor; last30days: search (optional, default search); x-timeline: tweets|tweet|thread|search (optional, default tweets)",
                        },
                    )
                    props.setdefault(
                        "ticker",
                        {
                            "type": "string",
                            "description": "Stock symbol for xueqiu posts (e.g. 00700, 06181, SH600519, SZ300750).",
                        },
                    )
                    props.setdefault(
                        "url",
                        {
                            "type": "string",
                            "description": "Target URL (xueqiu.com for xueqiu post/user; glassdoor.com for employee glassdoor).",
                        },
                    )
                    props.setdefault(
                        "post_id",
                        {
                            "type": "string",
                            "description": "Xueqiu post ID for comments operation.",
                        },
                    )
                    props.setdefault(
                        "query",
                        {
                            "type": "string",
                            "description": "Search keyword/topic for xueqiu search or last30days.",
                        },
                    )
                    props.setdefault(
                        "company",
                        {
                            "type": "string",
                            "description": "Company name/slug for employee blind/summary/indeed (e.g. NVIDIA).",
                        },
                    )
                    props.setdefault(
                        "pages",
                        {
                            "type": "integer",
                            "description": "Page count (xueqiu: 1-3; employee blind/indeed: 1-3; employee summary: 1-40).",
                        },
                    )
                    props.setdefault(
                        "allow_paid",
                        {
                            "type": "boolean",
                            "description": "Must be true to allow paid employee operations (indeed, glassdoor); default false.",
                        },
                    )
                    props.setdefault(
                        "days",
                        {
                            "type": "integer",
                            "description": "Lookback window in days for last30days (1-30, default 30).",
                        },
                    )
                    props.setdefault(
                        "quick",
                        {
                            "type": "boolean",
                            "description": "Quick mode for last30days.",
                        },
                    )
                    props.setdefault(
                        "plan",
                        {
                            "type": "object",
                            "description": "Optional query plan object for last30days (each subquery requires search_query and ranking_query).",
                        },
                    )
                    props.setdefault(
                        "handle",
                        {
                            "type": "string",
                            "description": "X handle for x-timeline tweets (accepts @user, user, or x.com/twitter.com profile URL).",
                        },
                    )
                    props.setdefault(
                        "tweet_id",
                        {
                            "type": "string",
                            "description": "Numeric tweet id for x-timeline tweet/thread (alternative to url).",
                        },
                    )
                    props.setdefault(
                        "since",
                        {
                            "type": "string",
                            "description": "Window start YYYY-MM-DD for x-timeline (search: spliced into query as since:; tweets: filters results, inclusive).",
                        },
                    )
                    props.setdefault(
                        "until",
                        {
                            "type": "string",
                            "description": "Window end YYYY-MM-DD for x-timeline (search: spliced into query as until:; tweets: filters results, inclusive).",
                        },
                    )
                    props.setdefault(
                        "count",
                        {
                            "type": "integer",
                            "description": "Item count for x-timeline tweets/search (1-100, default 20; bridge truncates to count).",
                        },
                    )
                    props.setdefault(
                        "include_replies",
                        {
                            "type": "boolean",
                            "description": "Include replies for x-timeline tweets only (xreach search has no --replies).",
                        },
                    )
            elif tool_copy.get("name") == "macro":
                if isinstance(route_prop, dict) and isinstance(route_prop.get("enum"), list):
                    if "market-regime" not in route_prop["enum"]:
                        route_prop["enum"].append("market-regime")
                if isinstance(props, dict):
                    op_prop = props.get("operation")
                    if isinstance(op_prop, dict):
                        op_desc = str(op_prop.get("description") or "").rstrip()
                        if "market-regime" not in op_desc:
                            op_prop["description"] = (
                                f"{op_desc} For market-regime: snapshot (default), us_topbottom, crowding, cn_leverage."
                            ).strip()
                    else:
                        props["operation"] = {
                            "type": "string",
                            "description": "Operation name (for market-regime: snapshot, us_topbottom, crowding, cn_leverage).",
                        }
                    props.setdefault(
                        "top",
                        {
                            "type": "integer",
                            "default": 10,
                            "maximum": 30,
                            "description": "For market-regime crowding: number of rows to return (default 10, max 30).",
                        },
                    )
                    props.setdefault(
                        "order",
                        {
                            "type": "string",
                            "enum": ["high", "low"],
                            "default": "high",
                            "description": "For market-regime crowding: sort order by crowding_percentile (high or low).",
                        },
                    )
                    props.setdefault(
                        "category",
                        {
                            "type": "string",
                            "description": "For market-regime crowding: optional category filter.",
                        },
                    )
                    props.setdefault(
                        "query",
                        {
                            "type": "string",
                            "description": "For market-regime crowding: optional filter by index name or code.",
                        },
                    )
                m_desc = str(tool_copy.get("description") or "").rstrip()
                if "market-regime" not in m_desc:
                    m_desc = (
                        f"{m_desc} Market regime & crowding: route='market-regime' (operations: snapshot, us_topbottom, crowding, cn_leverage)."
                    ).strip()
                if self.MACRO_DESCRIPTION_SUFFIX.strip() not in m_desc:
                    m_desc = m_desc + self.MACRO_DESCRIPTION_SUFFIX
                tool_copy["description"] = m_desc
            elif tool_name == "finance":
                f_desc = str(tool_copy.get("description") or "").rstrip()
                if self.FINANCE_DESCRIPTION_SUFFIX.strip() not in f_desc:
                    tool_copy["description"] = f_desc + self.FINANCE_DESCRIPTION_SUFFIX
            elif tool_name == "catalog":
                c_desc = str(tool_copy.get("description") or "").rstrip()
                if self.CATALOG_DESCRIPTION_SUFFIX.strip() not in c_desc:
                    tool_copy["description"] = c_desc + self.CATALOG_DESCRIPTION_SUFFIX
                if isinstance(props, dict):
                    props.setdefault(
                        "route",
                        {
                            "type": "string",
                            "description": "按单个 route 查询详细参数契约（必填/可选参数、日期与股票代码格式、返回结构、调用示例），如 'sec'、'cn-hk'、'alphaengine'、'transcript'、'xueqiu'、'x-timeline'。",
                        },
                    )
            elif tool_name == "news":
                n_desc = str(tool_copy.get("description") or "").rstrip()
                if self.NEWS_DESCRIPTION_SUFFIX.strip() not in n_desc:
                    tool_copy["description"] = n_desc + self.NEWS_DESCRIPTION_SUFFIX
            if isinstance(props, dict):
                schema_obj = tool_copy.get("inputSchema")
                if isinstance(schema_obj, dict) and isinstance(schema_obj.get("required"), list):
                    schema_obj["required"] = [
                        r_k for r_k in schema_obj["required"] if r_k != "route"
                    ]
                req_set = set(
                    (tool_copy.get("inputSchema") or {}).get("required") or []
                )
                for p_name, p_schema in props.items():
                    if p_name not in req_set and isinstance(p_schema, dict):
                        p_schema.pop("default", None)
                for alias_k, alias_schema in (
                    TOOL_SCHEMA_ALIAS_PROPERTIES.get(tool_name) or {}
                ).items():
                    props.setdefault(alias_k, dict(alias_schema))
            filtered.append(tool_copy)

        self._cached_tools = filtered
        return filtered

    def _should_fanout_alphaengine(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> bool:
        """Return True when a `research(route='all')` call is a search that should fan out to AlphaEngine and Guidepoint."""
        if tool_name != "research":
            return False
        if str(arguments.get("route") or "").strip() != "all":
            return False
        op = str(arguments.get("operation") or "").strip()
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if op == "search":
            return bool(query or ticker)
        if not op:
            return bool(query)
        return False

    def _compute_fanout_timeout(self, arguments: dict[str, Any], default_cap_s: int) -> int:
        """Compute clamped fanout timeout capped at `default_cap_s` (60s for AlphaEngine, 90s for Guidepoint)."""
        ceiling = max(10, self.cfg.caller_budget_s - 25)
        req_timeout = arguments.get("timeout")
        if isinstance(req_timeout, int) and not isinstance(req_timeout, bool) and req_timeout > 0:
            chosen = min(req_timeout, default_cap_s)
        else:
            chosen = default_cap_s
        return max(5, min(chosen, default_cap_s, ceiling))

    def _build_alphaengine_fanout_args(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Map `research(route='all')` search arguments to `route='alphaengine'` parameters.

        Mapping basis:
        - `data_gateway.py` sets `accepted['alphaengine'] = {'query', 'start', 'end'}` and rejects
          `--ticker` (and `series`/`concept`/`item`/`document`/`form`/`dataset`/`as_of`/`vintage`)
          with ValueError; if `ticker` was provided on `all`, fold it into `query`.
        - `data_gateway.py` and `config/data_sources.json` restrict `alphaengine` operations to
          `['search_library', 'get_document', 'auth_status', 'list_filters']`, so `operation` is
          mapped to `'search_library'`.
        - `alphaengine-mcp/server.mjs` (`requireDatePair`) requires `start` (`date_from`) and `end`
          (`date_to`) as a pair; if `start` is given without `end`, default `end` to today's UTC
          date, and omit a lone `end` without `start`.
        - Local-library `params` keys (`sources`, `layer`, `search_mode`, etc.) are omitted because
          `search_library` sets `additionalProperties: false`.
        - Timeout is capped at `FANOUT_ALPHAENGINE_TIMEOUT_S` (60s).
        """
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if ticker and ticker.lower() not in query.lower():
            query = f"{ticker} {query}".strip() if query else ticker

        ae_args: dict[str, Any] = {
            "route": "alphaengine",
            "operation": "search_library",
            "query": query,
        }
        start = str(arguments.get("start") or "").strip()
        end = str(arguments.get("end") or "").strip()
        if start and end:
            ae_args["start"] = start
            ae_args["end"] = end
        elif start and not end:
            ae_args["start"] = start
            ae_args["end"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        limit = arguments.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            ae_args["limit"] = min(limit, 100)

        ae_args["timeout"] = self._compute_fanout_timeout(
            arguments, FANOUT_ALPHAENGINE_TIMEOUT_S
        )
        return ae_args

    def _build_guidepoint_fanout_args(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Map `research(route='all')` search arguments to `route='guidepoint'` parameters.

        Mapping basis:
        - `catalog()` (`config/data_sources.json`) registers `guidepoint` with
          `operations: ['search_library']` (forwarded to `mcp__guidepoint__search_library`).
        - `researchdata_mcp.py` (`call_tool`) forwards top-level string fields + `limit` + `timeout`
          + `params` to `data_gateway.py fetch guidepoint`.
        - `data_gateway.py` (`build_command`) sets `accepted['guidepoint'] = {'query', 'start', 'end'}`
          and raises ValueError if `--ticker` (or `series`/`concept`/`item`/`document`/`form`/
          `dataset`/`as_of`/`vintage`) is passed; if `ticker` was provided on `all`, fold it into `query`.
        - `data_gateway.py` (`native_request`) maps `start` -> `start_date`, `end` -> `end_date`,
          and `limit` -> `size` (clamped to `min(limit, 40)`).
        - Local-library `params` keys (`sources`, `layer`, `media`, `search_mode`, `company`,
          `sector`, `tag`, `indexed_since`, `sender`, `no_decay`, `offset`, `max_chars`) are omitted;
          only Guidepoint-native optional keys (`keywords`, `recency_bias`) are forwarded if present.
        - Timeout is capped at `FANOUT_GUIDEPOINT_TIMEOUT_S` (90s).
        """
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if ticker and ticker.lower() not in query.lower():
            query = f"{ticker} {query}".strip() if query else ticker

        gp_args: dict[str, Any] = {
            "route": "guidepoint",
            "operation": "search_library",
            "query": query,
        }
        start = str(arguments.get("start") or "").strip()
        end = str(arguments.get("end") or "").strip()
        if start and end and start <= end:
            gp_args["start"] = start
            gp_args["end"] = end
        elif start and not end:
            gp_args["start"] = start
        elif end and not start:
            gp_args["end"] = end

        limit = arguments.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            gp_args["limit"] = min(limit, 40)

        raw_params = arguments.get("params")
        if isinstance(raw_params, dict):
            gp_params: dict[str, Any] = {}
            if isinstance(raw_params.get("keywords"), list):
                kw = [str(k).strip() for k in raw_params["keywords"] if str(k).strip()]
                if kw:
                    gp_params["keywords"] = kw
            if isinstance(raw_params.get("recency_bias"), str) and raw_params["recency_bias"].strip():
                gp_params["recency_bias"] = raw_params["recency_bias"].strip()
            if gp_params:
                gp_args["params"] = gp_params

        gp_args["timeout"] = self._compute_fanout_timeout(
            arguments, FANOUT_GUIDEPOINT_TIMEOUT_S
        )
        return gp_args

    async def _run_alphaengine_fanout(
        self, req_id: Any, ae_args: dict[str, Any], ae_budget_s: float
    ) -> dict[str, Any]:
        """Run the parallel `route='alphaengine'` search and normalize its result or failure state."""
        ae_timeout = int(ae_args.get("timeout") or FANOUT_ALPHAENGINE_TIMEOUT_S)
        ae_rpc_req = {
            "jsonrpc": "2.0",
            "id": f"{req_id}:alphaengine" if req_id is not None else "1:alphaengine",
            "method": "tools/call",
            "params": {"name": "research", "arguments": ae_args},
        }
        try:
            ae_resps = await self._run_stdio_rpc(
                [ae_rpc_req], timeout_s=ae_budget_s
            )
        except asyncio.TimeoutError:
            reason = f"alphaengine timed out after {ae_timeout}s"
            logger.warning("alphaengine fanout timeout timeout_s=%d", ae_timeout)
            return {
                "ok": False,
                "status": "timeout",
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except SubprocessExitError as exc:
            reason = f"SubprocessExitError(exit_code={exc.exit_code})"
            logger.warning("alphaengine fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning("alphaengine fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        if not ae_resps or not isinstance(ae_resps[0], dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": "Empty response from researchdata_mcp",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        ae_resp = ae_resps[0]
        if isinstance(ae_resp.get("error"), dict):
            err_msg = str(ae_resp["error"].get("message") or "RPC error")[:200]
            status = "timeout" if "timeout" in err_msg.lower() or "timed out" in err_msg.lower() else "failed"
            return {
                "ok": False,
                "status": status,
                "route": "alphaengine",
                "reason": err_msg,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        res_obj = ae_resp.get("result")
        ae_payload: dict[str, Any] | None = None
        if isinstance(res_obj, dict):
            blocks = res_obj.get("content")
            if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    parsed = json.loads(blocks[0]["text"])
                    if isinstance(parsed, dict):
                        ae_payload = parsed
                except Exception:
                    ae_payload = None
            if ae_payload is None and isinstance(res_obj.get("structuredContent"), dict):
                ae_payload = res_obj["structuredContent"]

        if not isinstance(ae_payload, dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": "Invalid JSON payload from alphaengine",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        inner = ae_payload.get("data") if isinstance(ae_payload.get("data"), dict) else ae_payload
        ae_unwrapped = _unwrap_upstream_error_info(ae_payload)
        is_err = (
            bool(isinstance(res_obj, dict) and res_obj.get("isError"))
            or bool(ae_unwrapped["failed"])
            or ae_payload.get("ok") is False
            or bool(isinstance(inner, dict) and inner.get("isError"))
        )
        if is_err:
            err_text = str(
                ae_unwrapped["error"]
                or ae_payload.get("error")
                or ae_payload.get("reason")
                or ""
            ).strip()
            if not err_text and isinstance(inner, dict) and isinstance(inner.get("content"), list):
                c_blocks = inner["content"]
                if c_blocks and isinstance(c_blocks[0], dict):
                    err_text = str(c_blocks[0].get("text") or "").strip()
            raw_status = str(ae_unwrapped["status"] or ae_payload.get("status") or "").strip().lower()
            if raw_status == "timeout" or "timeout" in err_text.lower() or "timed out" in err_text.lower():
                status = "timeout"
            elif raw_status in _SPECIFIC_FAILURE_STATUSES:
                status = raw_status
            else:
                status = "failed"
            reason = (
                err_text
                or (f"alphaengine status={status}" if status not in ("failed", "error") else "")
                or "alphaengine call returned error"
            )[:200]
            return {
                "ok": False,
                "status": status,
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": ae_payload,
            }

        sc = (
            source_identity._unwrap_mcp_proxy_payload(inner)
            if isinstance(inner, dict)
            else source_identity._unwrap_mcp_proxy_payload(ae_payload)
        )
        raw_items = (
            sc.get("results")
            if isinstance(sc, dict) and isinstance(sc.get("results"), list)
            else None
        )
        if not isinstance(raw_items, list):
            top_sc = source_identity._unwrap_mcp_proxy_payload(ae_payload)
            raw_items = (
                top_sc.get("results")
                if isinstance(top_sc, dict) and isinstance(top_sc.get("results"), list)
                else []
            )

        ae_items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            norm = dict(item)
            doc_id = str(
                norm.get("doc_id")
                or norm.get("id")
                or norm.get("document_id")
                or ""
            ).strip()
            norm["route"] = "alphaengine"
            norm.setdefault("source", "alphaengine")
            if doc_id:
                full_doc_id = (
                    doc_id
                    if doc_id.startswith("alphaengine:")
                    else f"alphaengine:{doc_id}"
                )
                norm.setdefault("document_id", full_doc_id)
                norm.setdefault("canonical_id", full_doc_id)
            if norm.get("publish_time") and not norm.get("published_at"):
                norm["published_at"] = norm["publish_time"]
            ae_items.append(norm)

        return {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "reason": None,
            "count": len(ae_items),
            "results": ae_items,
            "raw_payload": ae_payload,
        }

    async def _run_guidepoint_fanout(
        self, req_id: Any, gp_args: dict[str, Any], gp_budget_s: float
    ) -> dict[str, Any]:
        """Run the parallel `route='guidepoint'` search and normalize its result or failure state."""
        gp_timeout = int(gp_args.get("timeout") or FANOUT_GUIDEPOINT_TIMEOUT_S)
        gp_rpc_req = {
            "jsonrpc": "2.0",
            "id": f"{req_id}:guidepoint" if req_id is not None else "1:guidepoint",
            "method": "tools/call",
            "params": {"name": "research", "arguments": gp_args},
        }
        try:
            gp_resps = await self._run_stdio_rpc(
                [gp_rpc_req], timeout_s=gp_budget_s
            )
        except asyncio.TimeoutError:
            reason = f"guidepoint timed out after {gp_timeout}s"
            logger.warning("guidepoint fanout timeout timeout_s=%d", gp_timeout)
            return {
                "ok": False,
                "status": "timeout",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except SubprocessExitError as exc:
            reason = f"SubprocessExitError(exit_code={exc.exit_code})"
            logger.warning("guidepoint fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning("guidepoint fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        if not gp_resps or not isinstance(gp_resps[0], dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": "Empty response from researchdata_mcp",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        gp_resp = gp_resps[0]
        if isinstance(gp_resp.get("error"), dict):
            err_msg = str(gp_resp["error"].get("message") or "RPC error")[:200]
            status = "timeout" if "timeout" in err_msg.lower() or "timed out" in err_msg.lower() else "failed"
            return {
                "ok": False,
                "status": status,
                "route": "guidepoint",
                "reason": err_msg,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        res_obj = gp_resp.get("result")
        gp_payload: dict[str, Any] | None = None
        if isinstance(res_obj, dict):
            blocks = res_obj.get("content")
            if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    parsed = json.loads(blocks[0]["text"])
                    if isinstance(parsed, dict):
                        gp_payload = parsed
                except Exception:
                    gp_payload = None
            if gp_payload is None and isinstance(res_obj.get("structuredContent"), dict):
                gp_payload = res_obj["structuredContent"]

        if not isinstance(gp_payload, dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": "Invalid JSON payload from guidepoint",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        inner = gp_payload.get("data") if isinstance(gp_payload.get("data"), dict) else gp_payload
        gp_unwrapped = _unwrap_upstream_error_info(gp_payload)
        is_err = (
            bool(isinstance(res_obj, dict) and res_obj.get("isError"))
            or bool(gp_unwrapped["failed"])
            or gp_payload.get("ok") is False
            or bool(isinstance(inner, dict) and inner.get("isError"))
        )
        if is_err:
            err_text = str(
                gp_unwrapped["error"]
                or gp_payload.get("error")
                or gp_payload.get("reason")
                or ""
            ).strip()
            if not err_text and isinstance(inner, dict) and isinstance(inner.get("content"), list):
                c_blocks = inner["content"]
                if c_blocks and isinstance(c_blocks[0], dict):
                    err_text = str(c_blocks[0].get("text") or "").strip()
            raw_status = str(gp_unwrapped["status"] or gp_payload.get("status") or "").strip().lower()
            if raw_status == "timeout" or "timeout" in err_text.lower() or "timed out" in err_text.lower():
                status = "timeout"
            elif raw_status in _SPECIFIC_FAILURE_STATUSES:
                status = raw_status
            else:
                status = "failed"
            reason = (
                err_text
                or (f"guidepoint status={status}" if status not in ("failed", "error") else "")
                or "guidepoint call returned error"
            )[:200]
            return {
                "ok": False,
                "status": status,
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": gp_payload,
            }

        sc = source_identity._unwrap_mcp_proxy_payload(inner) if isinstance(inner, dict) else gp_payload
        raw_items = sc.get("data") if isinstance(sc, dict) and isinstance(sc.get("data"), list) else None
        if not isinstance(raw_items, list) and isinstance(sc, dict):
            raw_items = sc.get("results") if isinstance(sc.get("results"), list) else None
        if not isinstance(raw_items, list):
            raw_items = (
                gp_payload.get("data")
                if isinstance(gp_payload.get("data"), list)
                else gp_payload.get("results")
                if isinstance(gp_payload.get("results"), list)
                else []
            )

        gp_items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            norm = dict(item)
            raw_id = str(
                norm.get("id")
                or norm.get("transcript_id")
                or norm.get("document_id")
                or ""
            ).strip()
            m = source_identity._GP_ID_RE.search(raw_id)
            base_id = m.group(1) if m else raw_id
            norm["route"] = "guidepoint"
            norm.setdefault("source", "guidepoint")
            if base_id:
                full_doc_id = (
                    base_id
                    if base_id.startswith("guidepoint:")
                    else f"guidepoint:{base_id}"
                )
                norm.setdefault("document_id", full_doc_id)
                norm.setdefault("canonical_id", full_doc_id)
            if norm.get("transcript_name") and not norm.get("title"):
                norm["title"] = norm["transcript_name"]
            if norm.get("date") and not norm.get("published_at"):
                norm["published_at"] = norm["date"]
            if isinstance(norm.get("respondent"), dict) and "full_name" in norm["respondent"]:
                norm["respondent"] = {
                    k: v for k, v in norm["respondent"].items() if k != "full_name"
                }
            gp_items.append(norm)

        return {
            "ok": True,
            "status": "ok",
            "route": "guidepoint",
            "reason": None,
            "count": len(gp_items),
            "results": gp_items,
            "raw_payload": gp_payload,
        }

    def _merge_alphaengine_into_all(
        self,
        payload: dict[str, Any],
        declared_local: list[dict[str, Any]],
        ae_outcome: dict[str, Any],
        ae_args: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Merge AlphaEngine fanout results/status into `route='all'` payload and declared sources."""
        raw_ae_check = ae_outcome.get("raw_payload")
        ae_raw_unwrapped = (
            _unwrap_upstream_error_info(raw_ae_check)
            if isinstance(raw_ae_check, dict)
            else {"failed": False, "error": "", "status": ""}
        )
        ae_ok = bool(ae_outcome.get("ok")) and not bool(ae_raw_unwrapped["failed"])
        if ae_ok:
            ae_items = list(ae_outcome.get("results") or [])
            ae_section: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "route": "alphaengine",
                "count": len(ae_items),
                "results": ae_items,
            }
        else:
            st_cand = str(ae_outcome.get("status") or ae_raw_unwrapped.get("status") or "").strip().lower()
            st = st_cand if st_cand in _FAILURE_STATUSES else "failed"
            reason = str(ae_outcome.get("reason") or ae_raw_unwrapped.get("error") or st)
            ae_items = []
            ae_section = {
                "ok": False,
                "status": st,
                "route": "alphaengine",
                "reason": reason,
                "error": reason,
                "count": 0,
                "results": [],
            }

        if not isinstance(payload.get("source_status"), dict):
            local_probe = {
                k: v
                for k, v in payload.items()
                if k not in ("alphaengine", "guidepoint", "source_status")
            }
            local_unwrapped = _unwrap_upstream_error_info(local_probe)
            local_ok = bool(payload.get("ok")) and not bool(local_unwrapped["failed"])
            if local_ok:
                local_entry: dict[str, Any] = {
                    "ok": True,
                    "status": str(payload.get("status") or "ok"),
                    "count": (
                        len(payload.get("results") or [])
                        if isinstance(payload.get("results"), list)
                        else 0
                    ),
                }
            else:
                l_st_raw = str(local_unwrapped.get("status") or payload.get("status") or "").strip().lower()
                l_st = l_st_raw if l_st_raw in _FAILURE_STATUSES else "failed"
                l_reason = str(local_unwrapped.get("error") or payload.get("error") or l_st)
                local_entry = {
                    "ok": False,
                    "status": l_st,
                    "reason": l_reason,
                    "error": l_reason,
                    "count": 0,
                }
            payload["source_status"] = {"local_all": local_entry}
        payload["alphaengine"] = ae_section
        payload["source_status"]["alphaengine"] = {
            k: v for k, v in ae_section.items() if k != "results"
        }

        if ae_items and isinstance(payload.get("results"), list):
            existing_ids = {
                str(r.get("canonical_id") or r.get("document_id") or "")
                for r in payload["results"]
                if isinstance(r, dict)
            }
            existing_ids.discard("")
            for item in ae_items:
                cid = str(item.get("canonical_id") or item.get("document_id") or "")
                if cid and cid in existing_ids:
                    continue
                if cid:
                    existing_ids.add(cid)
                payload["results"].append(item)
            if "result_count" in payload:
                payload["result_count"] = len(payload["results"])
            if payload.get("ok") is True and payload.get("status") == "empty":
                payload["status"] = "ok"

        merged_declared: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        for src in declared_local:
            if not isinstance(src, dict):
                continue
            doc_id = str(src.get("document_id") or "")
            if doc_id:
                seen_doc_ids.add(doc_id)
            merged_declared.append(src)

        raw_ae = ae_outcome.get("raw_payload")
        if ae_outcome.get("ok") and isinstance(raw_ae, dict):
            declared_ae = source_identity.declare("research", ae_args, raw_ae)
            for src in declared_ae:
                if not isinstance(src, dict):
                    continue
                src_copy = dict(src)
                src_copy["route"] = "alphaengine"
                doc_id = str(src_copy.get("document_id") or "")
                if doc_id and doc_id in seen_doc_ids:
                    continue
                if doc_id:
                    seen_doc_ids.add(doc_id)
                merged_declared.append(src_copy)
                if len(merged_declared) >= source_identity.MAX_SOURCES_PER_CALL:
                    break

        return merged_declared[: source_identity.MAX_SOURCES_PER_CALL]

    def _merge_guidepoint_into_all(
        self,
        payload: dict[str, Any],
        declared_so_far: list[dict[str, Any]],
        gp_outcome: dict[str, Any],
        gp_args: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Merge Guidepoint fanout results/status into `route='all'` payload and declared sources."""
        raw_gp_check = gp_outcome.get("raw_payload")
        gp_raw_unwrapped = (
            _unwrap_upstream_error_info(raw_gp_check)
            if isinstance(raw_gp_check, dict)
            else {"failed": False, "error": "", "status": ""}
        )
        gp_ok = bool(gp_outcome.get("ok")) and not bool(gp_raw_unwrapped["failed"])
        if gp_ok:
            gp_items = list(gp_outcome.get("results") or [])
            gp_section: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "route": "guidepoint",
                "count": len(gp_items),
                "results": gp_items,
            }
        else:
            st_cand = str(gp_outcome.get("status") or gp_raw_unwrapped.get("status") or "").strip().lower()
            st = st_cand if st_cand in _FAILURE_STATUSES else "failed"
            reason = str(gp_outcome.get("reason") or gp_raw_unwrapped.get("error") or st)
            gp_items = []
            gp_section = {
                "ok": False,
                "status": st,
                "route": "guidepoint",
                "reason": reason,
                "error": reason,
                "count": 0,
                "results": [],
            }

        if not isinstance(payload.get("source_status"), dict):
            local_probe = {
                k: v
                for k, v in payload.items()
                if k not in ("alphaengine", "guidepoint", "source_status")
            }
            local_unwrapped = _unwrap_upstream_error_info(local_probe)
            local_ok = bool(payload.get("ok")) and not bool(local_unwrapped["failed"])
            if local_ok:
                local_entry: dict[str, Any] = {
                    "ok": True,
                    "status": str(payload.get("status") or "ok"),
                    "count": (
                        len(payload.get("results") or [])
                        if isinstance(payload.get("results"), list)
                        else 0
                    ),
                }
            else:
                l_st_raw = str(local_unwrapped.get("status") or payload.get("status") or "").strip().lower()
                l_st = l_st_raw if l_st_raw in _FAILURE_STATUSES else "failed"
                l_reason = str(local_unwrapped.get("error") or payload.get("error") or l_st)
                local_entry = {
                    "ok": False,
                    "status": l_st,
                    "reason": l_reason,
                    "error": l_reason,
                    "count": 0,
                }
            payload["source_status"] = {"local_all": local_entry}
        payload["guidepoint"] = gp_section
        payload["source_status"]["guidepoint"] = {
            k: v for k, v in gp_section.items() if k != "results"
        }

        if gp_items and isinstance(payload.get("results"), list):
            existing_ids = {
                str(r.get("id") or r.get("canonical_id") or r.get("document_id") or "")
                for r in payload["results"]
                if isinstance(r, dict)
            }
            existing_ids.discard("")
            for item in gp_items:
                cid = str(item.get("id") or item.get("canonical_id") or item.get("document_id") or "")
                if cid and cid in existing_ids:
                    continue
                if cid:
                    existing_ids.add(cid)
                payload["results"].append(item)
            if "result_count" in payload:
                payload["result_count"] = len(payload["results"])
            if payload.get("ok") is True and payload.get("status") == "empty":
                payload["status"] = "ok"

        merged_declared: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        for src in declared_so_far:
            if not isinstance(src, dict):
                continue
            doc_id = str(src.get("document_id") or "")
            if doc_id:
                seen_doc_ids.add(doc_id)
            merged_declared.append(src)

        raw_gp = gp_outcome.get("raw_payload")
        if gp_outcome.get("ok") and isinstance(raw_gp, dict):
            declared_gp = source_identity.declare("research", gp_args, raw_gp)
            for src in declared_gp:
                if not isinstance(src, dict):
                    continue
                src_copy = dict(src)
                src_copy["route"] = "guidepoint"
                doc_id = str(src_copy.get("document_id") or "")
                if doc_id and doc_id in seen_doc_ids:
                    continue
                if doc_id:
                    seen_doc_ids.add(doc_id)
                merged_declared.append(src_copy)
                if len(merged_declared) >= source_identity.MAX_SOURCES_PER_CALL:
                    break

        return merged_declared[: source_identity.MAX_SOURCES_PER_CALL]

    def _read_usage_today(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            if self.cfg.usage_file.is_file():
                data = json.loads(self.cfg.usage_file.read_text(encoding="utf-8"))
                day_entry = data.get(today) if isinstance(data, dict) else None
                if isinstance(day_entry, dict):
                    return {
                        "calls": int(day_entry.get("calls", 0)),
                        "chars": int(day_entry.get("chars", 0)),
                    }
        except Exception:
            pass
        return {"calls": 0, "chars": 0}

    async def _record_alphaengine_get_doc(self, chars: int) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self._usage_lock:
            try:
                self.cfg.usage_file.parent.mkdir(parents=True, exist_ok=True)
                data: dict[str, Any] = {}
                if self.cfg.usage_file.is_file():
                    try:
                        loaded = json.loads(
                            self.cfg.usage_file.read_text(encoding="utf-8")
                        )
                        if isinstance(loaded, dict):
                            data = loaded
                    except Exception:
                        data = {}
                day_entry = data.get(today)
                if not isinstance(day_entry, dict):
                    day_entry = {"calls": 0, "chars": 0}
                day_entry["calls"] = int(day_entry.get("calls", 0)) + 1
                day_entry["chars"] = int(day_entry.get("chars", 0)) + max(0, int(chars))
                data[today] = day_entry
                tmp_path = self.cfg.usage_file.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp_path.replace(self.cfg.usage_file)
            except Exception as exc:
                logger.warning("Failed to record alphaengine usage: %s", type(exc).__name__)

    @staticmethod
    def _make_tool_result_response(
        req_id: Any, payload: dict[str, Any], *, is_error: bool
    ) -> dict[str, Any]:
        effective_is_error = (
            not bool(payload.get("ok"))
            if isinstance(payload, dict) and "ok" in payload
            else bool(is_error)
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(payload, ensure_ascii=False),
                    }
                ],
                "structuredContent": dict(payload),
                "isError": effective_is_error,
            },
        }

    def _build_community_subprocess_env(self) -> dict[str, str]:
        """Build subprocess environment with CLAUDECODE/CLAUDE_CODE_* stripped and optional proxy applied."""
        env: dict[str, str] = {
            k: v
            for k, v in os.environ.items()
            if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["LAST30DAYS_ALLOW_ENGINE_PLAN"] = "1"
        env["FROM_BROWSER"] = "off"
        existing_excl = env.get("EXCLUDE_SOURCES", "").strip()
        excl_parts = [p.strip() for p in existing_excl.split(",") if p.strip()]
        if "reddit" not in [p.lower() for p in excl_parts]:
            excl_parts.append("reddit")
        env["EXCLUDE_SOURCES"] = ",".join(excl_parts)
        has_proxy = any(
            bool(env.get(pk, "").strip())
            for pk in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
        )
        if not has_proxy:
            proxy = (
                self.cfg.subprocess_proxy
                or os.environ.get("DALTON_BRIDGE_SUBPROCESS_PROXY", "")
            ).strip()
            if proxy:
                env["http_proxy"] = proxy
                env["https_proxy"] = proxy
                env["HTTP_PROXY"] = proxy
                env["HTTPS_PROXY"] = proxy
                env["ALL_PROXY"] = proxy
        return env

    def _xreach_proxy_args(self) -> list[str]:
        """Return an explicit `--proxy <url>` argv suffix mirroring the bridge subprocess env.

        xreach ignores proxy environment variables, so the proxy the other community
        scripts inherit must be passed as a flag. A URL carrying userinfo never reaches
        argv (it would leak credentials into the process list); the route then runs
        without a proxy and a warning is logged (never the URL itself).
        """
        env = self._build_community_subprocess_env()
        proxy = ""
        for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
            val = str(env.get(key) or "").strip()
            if val:
                proxy = val
                break
        if not proxy:
            return []
        parts = urllib.parse.urlsplit(proxy)
        if parts.username or parts.password:
            logger.warning(
                "x-timeline: configured proxy URL carries credentials; "
                "running xreach without --proxy"
            )
            return []
        return ["--proxy", proxy]

    def _check_community_source_files(self, route: str) -> str | None:
        """Return an error description if required interpreter or script files are missing, without installing anything."""
        if route == "employee":
            if not self.cfg.employee_python.is_file():
                return f"Employee reviews Python interpreter not found: {self.cfg.employee_python}"
            if not self.cfg.employee_cli_script.is_file():
                return f"Employee reviews CLI script not found: {self.cfg.employee_cli_script}"
            return None
        if route == "xueqiu":
            if not Path(self.cfg.python_bin).is_file():
                return f"Xueqiu Python interpreter not found: {self.cfg.python_bin}"
            if not self.cfg.xueqiu_script.is_file():
                return f"Xueqiu script not found: {self.cfg.xueqiu_script}"
            return None
        if route == "last30days":
            if not self.cfg.last30days_python.is_file():
                return f"last30days Python interpreter not found: {self.cfg.last30days_python}"
            if not self.cfg.last30days_script.is_file():
                return f"last30days CLI script not found: {self.cfg.last30days_script}"
            return None
        if route == "x-timeline":
            if not self.cfg.xreach_bin.is_file():
                return f"xreach binary not found: {self.cfg.xreach_bin}"
            return None
        return None

    async def probe_xreach_version(self) -> dict[str, Any]:
        """Run offline `xreach --version` for `/healthz` (cached 60s); reports status only, never secrets."""
        now = time.monotonic()
        if self._xreach_version_cache is not None and now - self._xreach_version_cache[0] < 60.0:
            return self._xreach_version_cache[1]
        bin_path = self.cfg.xreach_bin
        outcome: dict[str, Any]
        if not bin_path.is_file():
            outcome = {
                "ok": False,
                "status": "missing",
                "error": f"xreach binary not found: {bin_path}",
            }
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    str(bin_path),
                    "--version",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                        proc.communicate(), timeout=8.0
                    )
                except BaseException:
                    await _terminate_and_reap_process(
                        proc, term_timeout_s=COMMUNITY_SIGTERM_GRACE_S
                    )
                    raise
                version_lines = (stdout_bytes or b"").decode(
                    "utf-8", errors="replace"
                ).splitlines()
                version = sanitize_stderr_text(
                    version_lines[0] if version_lines else "",
                    max_len=120,
                )
                rc = proc.returncode if proc.returncode is not None else 0
                if rc == 0 and version:
                    outcome = {"ok": True, "status": "ok", "version": version}
                else:
                    outcome = {
                        "ok": False,
                        "status": "error",
                        "error": f"xreach --version exited with code {rc}",
                    }
            except asyncio.TimeoutError:
                outcome = {"ok": False, "status": "timeout", "error": "xreach --version timed out"}
            except Exception as exc:
                outcome = {"ok": False, "status": "error", "error": type(exc).__name__}
        self._xreach_version_cache = (now, outcome)
        return outcome

    def _validate_and_build_community_cmd(
        self,
        route: str,
        arguments: dict[str, Any],
        plan_file: Path | None = None,
    ) -> list[str]:
        """Validate community route arguments strictly against whitelists (ignoring upstream generic defaults) and return exec argv list."""
        clean_args = {
            k: v
            for k, v in arguments.items()
            if k not in UPSTREAM_GENERIC_COMMUNITY_PARAMS and v is not None
        }
        if route == "xueqiu":
            allowed_keys = {
                "route",
                "operation",
                "ticker",
                "url",
                "post_id",
                "query",
                "pages",
            }
            extra = set(clean_args.keys()) - allowed_keys
            if extra:
                raise ValueError(
                    f"Unsupported parameters for xueqiu: {sorted(extra)}. Accepted parameters: {sorted(allowed_keys)}"
                )
            op = clean_args.get("operation")
            if not isinstance(op, str) or op not in (
                "posts",
                "post",
                "comments",
                "search",
                "user",
            ):
                raise ValueError(
                    f"xueqiu operation must be one of: posts, post, comments, search, user. Accepted parameters: {sorted(allowed_keys)}"
                )
            if op == "posts":
                for forbidden in ("url", "post_id", "query"):
                    if clean_args.get(forbidden) is not None:
                        raise ValueError(
                            f"Parameter '{forbidden}' is not allowed for xueqiu posts. Accepted parameters for xueqiu posts: ['operation', 'pages', 'route', 'ticker']"
                        )
                raw_ticker = clean_args.get("ticker")
                if not isinstance(raw_ticker, str):
                    raise ValueError("ticker is required and must be a string")
                ticker = raw_ticker.strip()
                if not ticker or ticker.startswith("-") or not _TICKER_RE.fullmatch(ticker):
                    raise ValueError("ticker format is invalid")
                pages = _validate_int_range(
                    clean_args.get("pages"),
                    field_name="pages",
                    min_val=1,
                    max_val=3,
                    default=1,
                )
                return [
                    self.cfg.python_bin,
                    str(self.cfg.xueqiu_script),
                    "--json",
                    "posts",
                    ticker,
                    "--pages",
                    str(pages),
                ]
            if op == "post":
                for forbidden in ("ticker", "post_id", "query", "pages"):
                    if clean_args.get(forbidden) is not None:
                        raise ValueError(
                            f"Parameter '{forbidden}' is not allowed for xueqiu post. Accepted parameters for xueqiu post: ['operation', 'route', 'url']"
                        )
                url = _validate_community_url(
                    clean_args.get("url"), required_domain="xueqiu.com"
                )
                return [
                    self.cfg.python_bin,
                    str(self.cfg.xueqiu_script),
                    "--json",
                    "post",
                    url,
                ]
            if op == "comments":
                for forbidden in ("ticker", "url", "query"):
                    if clean_args.get(forbidden) is not None:
                        raise ValueError(
                            f"Parameter '{forbidden}' is not allowed for xueqiu comments. Accepted parameters for xueqiu comments: ['operation', 'pages', 'post_id', 'route']"
                        )
                raw_post_id = clean_args.get("post_id")
                if not isinstance(raw_post_id, str):
                    raise ValueError("post_id is required and must be a string")
                post_id = raw_post_id.strip()
                if not _POST_ID_RE.fullmatch(post_id):
                    raise ValueError("post_id must be a numeric string (1-32 digits)")
                pages = _validate_int_range(
                    clean_args.get("pages"),
                    field_name="pages",
                    min_val=1,
                    max_val=3,
                    default=1,
                )
                return [
                    self.cfg.python_bin,
                    str(self.cfg.xueqiu_script),
                    "--json",
                    "comments",
                    post_id,
                    "--pages",
                    str(pages),
                ]
            if op == "search":
                for forbidden in ("ticker", "url", "post_id"):
                    if clean_args.get(forbidden) is not None:
                        raise ValueError(
                            f"Parameter '{forbidden}' is not allowed for xueqiu search. Accepted parameters for xueqiu search: ['operation', 'pages', 'query', 'route']"
                        )
                query = _validate_clean_text(
                    clean_args.get("query"), field_name="query", max_len=200
                )
                pages = _validate_int_range(
                    clean_args.get("pages"),
                    field_name="pages",
                    min_val=1,
                    max_val=3,
                    default=1,
                )
                return [
                    self.cfg.python_bin,
                    str(self.cfg.xueqiu_script),
                    "search",
                    query,
                    "--pages",
                    str(pages),
                    "--json",
                ]
            # op == "user"
            for forbidden in ("ticker", "post_id", "query"):
                if clean_args.get(forbidden) is not None:
                    raise ValueError(
                        f"Parameter '{forbidden}' is not allowed for xueqiu user. Accepted parameters for xueqiu user: ['operation', 'pages', 'route', 'url']"
                    )
            url = _validate_community_url(
                clean_args.get("url"), required_domain="xueqiu.com"
            )
            pages = _validate_int_range(
                clean_args.get("pages"),
                field_name="pages",
                min_val=1,
                max_val=3,
                default=1,
            )
            return [
                self.cfg.python_bin,
                str(self.cfg.xueqiu_script),
                "--json",
                "user",
                url,
                "--pages",
                str(pages),
            ]

        if route == "employee":
            allowed_keys = {
                "route",
                "operation",
                "company",
                "url",
                "pages",
                "allow_paid",
            }
            extra = set(clean_args.keys()) - allowed_keys
            if extra:
                raise ValueError(
                    f"Unsupported parameters for employee: {sorted(extra)}. Accepted parameters: {sorted(allowed_keys)}"
                )
            op = clean_args.get("operation")
            if not isinstance(op, str) or op not in (
                "blind",
                "summary",
                "indeed",
                "glassdoor",
            ):
                raise ValueError(
                    f"employee operation must be one of: blind, summary, indeed, glassdoor. Accepted parameters: {sorted(allowed_keys)}"
                )
            allow_paid = clean_args.get("allow_paid")
            if allow_paid is not None and not isinstance(allow_paid, bool):
                raise ValueError("allow_paid must be a boolean")
            if op in ("blind", "summary", "indeed"):
                if clean_args.get("url") is not None:
                    raise ValueError(
                        f"Parameter 'url' is not allowed for employee {op}. Accepted parameters for employee {op}: ['allow_paid', 'company', 'operation', 'pages', 'route']"
                    )
                raw_company = clean_args.get("company")
                if not isinstance(raw_company, str):
                    raise ValueError("company is required and must be a string")
                company = raw_company.strip()
                if (
                    not company
                    or company.startswith("-")
                    or not _COMPANY_RE.fullmatch(company)
                ):
                    raise ValueError("company format is invalid")
                max_pages = 40 if op == "summary" else 3
                default_pages = 40 if op == "summary" else 1
                pages = _validate_int_range(
                    clean_args.get("pages"),
                    field_name="pages",
                    min_val=1,
                    max_val=max_pages,
                    default=default_pages,
                )
                return [
                    str(self.cfg.employee_python),
                    str(self.cfg.employee_cli_script),
                    op,
                    "--company",
                    company,
                    "--pages",
                    str(pages),
                    "--json",
                ]
            # op == "glassdoor"
            for forbidden in ("company", "pages"):
                if clean_args.get(forbidden) is not None:
                    raise ValueError(
                        f"Parameter '{forbidden}' is not allowed for employee glassdoor. Accepted parameters for employee glassdoor: ['allow_paid', 'operation', 'route', 'url']"
                    )
            url = _validate_community_url(
                clean_args.get("url"), required_domain="glassdoor.com"
            )
            return [
                str(self.cfg.employee_python),
                str(self.cfg.employee_cli_script),
                "glassdoor",
                "--url",
                url,
                "--json",
            ]

        if route == "last30days":
            allowed_keys = {"route", "operation", "query", "days", "quick", "plan"}
            extra = set(clean_args.keys()) - allowed_keys
            if extra:
                raise ValueError(
                    f"Unsupported parameters for last30days: {sorted(extra)}. Accepted parameters: {sorted(allowed_keys)}"
                )
            raw_op = clean_args.get("operation")
            if raw_op is not None:
                if not isinstance(raw_op, str) or raw_op.strip() not in ("", "search"):
                    raise ValueError(
                        f"last30days operation must be 'search' or omitted. Accepted parameters: {sorted(allowed_keys)}"
                    )
            query = _validate_clean_text(
                clean_args.get("query"), field_name="query", max_len=300
            )
            norm_query = " ".join(query.lower().split())
            if (
                norm_query in _LAST30DAYS_FORBIDDEN_QUERY_COMMANDS
                or norm_query.startswith(_LAST30DAYS_FORBIDDEN_QUERY_PREFIXES)
            ):
                raise ValueError(
                    f"Maintenance command '{query}' is not allowed for last30days"
                )
            days = _validate_int_range(
                clean_args.get("days"),
                field_name="days",
                min_val=1,
                max_val=30,
                default=30,
            )
            quick = clean_args.get("quick")
            if quick is not None and not isinstance(quick, bool):
                raise ValueError("quick must be a boolean")
            cmd = [
                str(self.cfg.last30days_python),
                str(self.cfg.last30days_script),
                query,
                "--days",
                str(days),
            ]
            if quick is True:
                cmd.append("--quick")
            if plan_file is not None:
                cmd.extend(["--plan", str(plan_file.resolve())])
            # The engine's default memory dir is ~/Documents/Last30Days. Under launchd that folder is
            # TCC-protected, so the engine's first scandir blocks forever on a permission prompt no one
            # can see (observed 2026-10-22: 600 s timeout with no socket open). Keep it in our cache.
            save_dir = self.cfg.cache_dir / "last30days-memory"
            save_dir.mkdir(parents=True, exist_ok=True)
            cmd.extend(["--save-dir", str(save_dir)])
            cmd.append("--emit=json")
            return cmd

        if route == "x-timeline":
            # `params` is not supported on x-timeline; `start`/`end`/`limit` are normalized to `since`/`until`/`count`.
            if arguments.get("params") is not None:
                raise ValueError(
                    "x-timeline does not support 'params'; pass top-level since/until/count. Accepted parameters: ['count', 'handle', 'include_replies', 'operation', 'query', 'route', 'since', 'tweet_id', 'until', 'url']"
                )
            allowed_keys = {
                "route",
                "operation",
                "handle",
                "url",
                "tweet_id",
                "query",
                "since",
                "until",
                "count",
                "include_replies",
            }
            extra = set(clean_args.keys()) - allowed_keys
            if extra:
                raise ValueError(
                    f"Unsupported parameters for x-timeline: {sorted(extra)}. Accepted parameters: {sorted(allowed_keys)}"
                )
            raw_op = clean_args.get("operation")
            if raw_op is None or (isinstance(raw_op, str) and not raw_op.strip()):
                op = "tweets"
            elif isinstance(raw_op, str) and raw_op.strip() in (
                "tweets",
                "tweet",
                "thread",
                "search",
            ):
                op = raw_op.strip()
            else:
                raise ValueError(
                    f"x-timeline operation must be one of: tweets, tweet, thread, search. Accepted parameters: {sorted(allowed_keys)}"
                )
            since = (
                _validate_x_date(clean_args.get("since"), field_name="since")
                if clean_args.get("since") is not None
                else ""
            )
            until = (
                _validate_x_date(clean_args.get("until"), field_name="until")
                if clean_args.get("until") is not None
                else ""
            )
            if since and until and since > until:
                raise ValueError("x-timeline since must not be after until")
            include_replies = clean_args.get("include_replies")
            if include_replies is not None and not isinstance(include_replies, bool):
                raise ValueError("include_replies must be a boolean")

            if op == "tweets":
                for forbidden in ("url", "tweet_id", "query"):
                    if clean_args.get(forbidden) is not None:
                        raise ValueError(
                            f"Parameter '{forbidden}' is not allowed for x-timeline tweets. Accepted parameters for x-timeline tweets: ['count', 'handle', 'include_replies', 'operation', 'route', 'since', 'until']"
                        )
                handle = _normalize_x_handle(clean_args.get("handle"))
                count = _validate_int_range(
                    clean_args.get("count"),
                    field_name="count",
                    min_val=1,
                    max_val=100,
                    default=20,
                )
                cmd = [
                    str(self.cfg.xreach_bin),
                    "tweets",
                    f"@{handle}",
                    "-n",
                    str(count),
                ]
                if include_replies is True:
                    cmd.append("--replies")
                cmd.append("--json")
                cmd.extend(self._xreach_proxy_args())
                return cmd

            if op == "search":
                for forbidden in ("handle", "url", "tweet_id"):
                    if clean_args.get(forbidden) is not None:
                        raise ValueError(
                            f"Parameter '{forbidden}' is not allowed for x-timeline search. Accepted parameters for x-timeline search: ['count', 'operation', 'query', 'route', 'since', 'until']"
                        )
                # xreach's search subcommand has no --replies option.
                if include_replies is not None:
                    raise ValueError(
                        "Parameter 'include_replies' is not allowed for x-timeline search. Accepted parameters for x-timeline search: ['count', 'operation', 'query', 'route', 'since', 'until']"
                    )
                query = _validate_clean_text(
                    clean_args.get("query"), field_name="query", max_len=300
                )
                windowed_query = query
                if since:
                    windowed_query += f" since:{since}"
                if until:
                    windowed_query += f" until:{until}"
                count = _validate_int_range(
                    clean_args.get("count"),
                    field_name="count",
                    min_val=1,
                    max_val=100,
                    default=20,
                )
                cmd = [
                    str(self.cfg.xreach_bin),
                    "search",
                    windowed_query,
                    "-n",
                    str(count),
                    "--json",
                ]
                cmd.extend(self._xreach_proxy_args())
                return cmd

            # op in ("tweet", "thread")
            for forbidden in (
                "handle",
                "query",
                "count",
                "include_replies",
                "since",
                "until",
            ):
                if clean_args.get(forbidden) is not None:
                    raise ValueError(
                        f"Parameter '{forbidden}' is not allowed for x-timeline {op}. Accepted parameters for x-timeline {op}: ['operation', 'route', 'tweet_id', 'url']"
                    )
            target = _extract_x_tweet_target(clean_args)
            cmd = [str(self.cfg.xreach_bin), op, target, "--json"]
            cmd.extend(self._xreach_proxy_args())
            return cmd

        raise ValueError(f"Unsupported community route '{route}'")

    async def _handle_community_call(
        self,
        req_id: Any,
        route: str,
        arguments: dict[str, Any],
        eff_timeout: int,
    ) -> dict[str, Any]:
        """Execute a bridge community route (`xueqiu`/`employee`/`last30days`/`x-timeline`) directly via subprocess without shell."""
        operation = str(arguments.get("operation") or "").strip()
        if route == "last30days" and not operation:
            operation = "search"
        if route == "x-timeline" and not operation:
            operation = "tweets"
        norm_params = _build_normalized_params_view(arguments, is_community=True)

        def _finish_comm(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
            _apply_error_and_empty_contract(
                "community", route, operation, payload, norm_params
            )
            final_is_error = (
                not bool(payload.get("ok"))
                if isinstance(payload, dict) and "ok" in payload
                else bool(is_error)
            )
            return self._make_tool_result_response(
                req_id, payload, is_error=final_is_error
            )

        # Paid source gate for employee indeed/glassdoor
        if route == "employee" and operation in ("indeed", "glassdoor"):
            if arguments.get("allow_paid") is not True:
                paid_err_msg = (
                    f"Employee operation '{operation}' uses a paid source and requires "
                    "allow_paid=True."
                )
                paid_err = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "paid_source_not_allowed",
                    "error_code": "paid_source_not_allowed",
                    "error": paid_err_msg,
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="paid_source_not_allowed",
                        error=paid_err_msg,
                    ),
                }
                return _finish_comm(paid_err, is_error=True)

        plan_obj: dict[str, Any] | None = None
        if route == "last30days" and arguments.get("plan") is not None:
            try:
                plan_obj = _validate_last30days_plan(arguments.get("plan"))
            except ValueError as exc:
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_arguments",
                    "error": str(exc),
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="invalid_arguments",
                        error=str(exc),
                    ),
                }
                return _finish_comm(err_payload, is_error=True)

        plan_file: Path | None = None
        try:
            if plan_obj is not None:
                cache_root = self.cfg.cache_dir.expanduser().resolve()
                if _is_within_openclaw_tree(cache_root, self.cfg.ocw_root):
                    err_msg = "Bridge cache_dir must not be inside ~/.openclaw"
                    err_payload = {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "invalid_arguments",
                        "error_code": "invalid_arguments",
                        "error": err_msg,
                        "data": sanitize_error_data_summary(
                            None,
                            route=route,
                            operation=operation,
                            status="invalid_arguments",
                            error=err_msg,
                        ),
                    }
                    return _finish_comm(err_payload, is_error=True)
                cache_root.mkdir(parents=True, exist_ok=True)
                plan_file = cache_root / f"last30days-plan-{uuid.uuid4().hex}.json"
                plan_file.write_text(
                    json.dumps(plan_obj, ensure_ascii=False), encoding="utf-8"
                )

            try:
                cmd = self._validate_and_build_community_cmd(
                    route, arguments, plan_file=plan_file
                )
            except ValueError as exc:
                exc_msg = str(exc)
                err_code = (
                    "unknown_parameters"
                    if (
                        "Unsupported parameters" in exc_msg
                        or "does not support" in exc_msg
                        or ("Parameter '" in exc_msg and "is not allowed for" in exc_msg)
                    )
                    else "invalid_arguments"
                )
                spec_comm = ROUTE_CONTRACT_SPECS.get(route) or {}
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": err_code,
                    "accepted_parameters": list(spec_comm.get("accepted_params") or []),
                    "error": exc_msg,
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="invalid_arguments",
                        error=exc_msg,
                    ),
                }
                return _finish_comm(err_payload, is_error=True)

            missing_err = self._check_community_source_files(route)
            if missing_err is not None:
                missing_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "source_missing",
                    "error_code": "source_missing",
                    "error": missing_err,
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="source_missing",
                        error=missing_err,
                    ),
                }
                return _finish_comm(missing_payload, is_error=True)

            route_sem = self.community_sems.get(route) or self.sem
            t0 = time.monotonic()
            deadline = t0 + float(eff_timeout)
            test_delay_s = self.cfg.resolve_test_delay_s()
            subproc_env = self._build_community_subprocess_env()

            try:
                wait_for_sem_s = deadline - time.monotonic()
                if wait_for_sem_s <= 0:
                    raise asyncio.TimeoutError()
                await asyncio.wait_for(route_sem.acquire(), timeout=wait_for_sem_s)
                try:
                    if test_delay_s > 0.0:
                        rem_before_delay = deadline - time.monotonic()
                        if rem_before_delay <= 0:
                            raise asyncio.TimeoutError()
                        await asyncio.wait_for(
                            asyncio.sleep(test_delay_s),
                            timeout=rem_before_delay,
                        )
                    remaining_budget_s = deadline - time.monotonic()
                    if remaining_budget_s <= 0:
                        raise asyncio.TimeoutError()
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        cwd=str(self.cfg.ocw_root),
                        env=subproc_env,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                    try:
                        stdout_bytes, stderr_bytes = await asyncio.wait_for(
                            proc.communicate(),
                            timeout=remaining_budget_s,
                        )
                    except BaseException:
                        await _terminate_and_reap_process(
                            proc, term_timeout_s=COMMUNITY_SIGTERM_GRACE_S
                        )
                        raise
                finally:
                    route_sem.release()
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    "community tools/call timeout route=%s op=%s timeout_s=%d elapsed_ms=%d",
                    route,
                    operation,
                    eff_timeout,
                    elapsed_ms,
                )
                timeout_msg = f"researchdata_bridge timed out after {eff_timeout}s on community/{route}"
                timeout_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "timeout",
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="timeout",
                        error=timeout_msg,
                    ),
                    "error": timeout_msg,
                }
                return _finish_comm(timeout_payload, is_error=True)

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            stderr_tail = sanitize_stderr_text(stderr_bytes, max_len=500)
            rc = proc.returncode if proc.returncode is not None else 0

            if rc != 0:
                err_msg = stderr_tail or f"Script exited with non-zero code {rc}"
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "script_error",
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="script_error",
                        error=err_msg,
                    ),
                    "error": err_msg,
                    "stderr": stderr_tail,
                    "exit_code": rc,
                }
                logger.warning(
                    "community script_error route=%s op=%s exit_code=%s elapsed_ms=%d",
                    route,
                    operation or "-",
                    rc,
                    elapsed_ms,
                )
                return _finish_comm(err_payload, is_error=True)

            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            parsed: Any = None
            valid_json = False
            if stdout_text:
                try:
                    parsed = json.loads(stdout_text)
                    if isinstance(parsed, (dict, list)):
                        valid_json = True
                except Exception:
                    for line in reversed(stdout_text.splitlines()):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            candidate = json.loads(line)
                            if isinstance(candidate, (dict, list)):
                                parsed = candidate
                                valid_json = True
                                break
                        except Exception:
                            continue

            if not valid_json:
                inv_msg = stderr_tail or "Script output is not valid JSON"
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_output",
                    "data": sanitize_error_data_summary(
                        None,
                        route=route,
                        operation=operation,
                        status="invalid_output",
                        error=inv_msg,
                    ),
                    "error": inv_msg,
                    "stderr": stderr_tail,
                }
                logger.warning(
                    "community invalid_output route=%s op=%s elapsed_ms=%d",
                    route,
                    operation or "-",
                    elapsed_ms,
                )
                return _finish_comm(err_payload, is_error=True)

            parsed = _sanitize_community_payload_metadata(parsed)

            raw_p_status = (
                str(parsed.get("status") or "").strip().lower()
                if isinstance(parsed, dict)
                else ""
            )
            comm_probe = (
                {k: v for k, v in parsed.items() if k != "source_status"}
                if isinstance(parsed, dict) and route == "last30days"
                else parsed
            )
            comm_unwrapped = (
                _unwrap_upstream_error_info(comm_probe)
                if isinstance(comm_probe, dict)
                else {"failed": False, "error": "", "status": ""}
            )
            if isinstance(parsed, dict) and (
                parsed.get("ok") is False
                or parsed.get("isError") is True
                or bool(parsed.get("error"))
                or raw_p_status in _FAILURE_STATUSES
                or bool(comm_unwrapped["failed"])
            ):
                eff_status_tok = raw_p_status or str(comm_unwrapped.get("status") or "").strip().lower()
                err_msg = sanitize_stderr_text(
                    str(
                        parsed.get("error")
                        or comm_unwrapped.get("error")
                        or parsed.get("message")
                        or stderr_tail
                        or (
                            f"Script reported status={eff_status_tok}"
                            if eff_status_tok in _FAILURE_STATUSES
                            else ""
                        )
                        or "Script reported ok=false"
                    ),
                    max_len=500,
                )
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "script_error",
                    "data": sanitize_error_data_summary(
                        parsed,
                        route=route,
                        operation=operation,
                        status="script_error",
                        error=err_msg,
                    ),
                    "error": err_msg,
                    "stderr": stderr_tail,
                }
                return _finish_comm(err_payload, is_error=True)

            if route == "x-timeline":
                parsed = _normalize_x_timeline_output(
                    parsed,
                    operation=operation,
                    count=_validate_int_range(
                        arguments.get("count"),
                        field_name="count",
                        min_val=1,
                        max_val=100,
                        default=20,
                    ),
                    since=str(arguments.get("since") or "").strip(),
                    until=str(arguments.get("until") or "").strip(),
                )

            if route == "last30days":
                raw_src_status = (
                    parsed.get("source_status") if isinstance(parsed, dict) else None
                )
                sanitized_src_status = _sanitize_source_status_map(raw_src_status)
                if isinstance(parsed, dict) and "source_status" in parsed:
                    parsed["source_status"] = sanitized_src_status
                non_healthy_sources: dict[str, str] = {}
                if isinstance(raw_src_status, dict):
                    for src_name, src_val in raw_src_status.items():
                        state = _extract_last30days_source_state(src_val) or "unknown"
                        if state not in _LAST30DAYS_HEALTHY_STATES or _is_last30days_source_entry_failed(src_val):
                            if isinstance(src_val, dict) and (
                                src_val.get("ok") is False
                                or src_val.get("isError") is True
                                or bool(src_val.get("error"))
                            ):
                                non_healthy_sources[str(src_name)] = str(
                                    src_val.get("error")
                                    or (state if state not in _LAST30DAYS_HEALTHY_STATES else "error")
                                )
                            else:
                                non_healthy_sources[str(src_name)] = state
                has_items = _has_last30days_items(parsed)
                if not has_items:
                    if _is_all_last30days_no_results(raw_src_status):
                        empty_payload: dict[str, Any] = {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": True,
                            "status": "empty",
                            "empty_reason": "no_matching_items",
                            "source_status": sanitized_src_status,
                            "data": parsed,
                            "stderr": stderr_tail,
                            "hint": (
                                f"社区路由 '{route}' 查询成功但未检索到匹配结果；可放宽 query 关键词、增大 days 或换用其他社区路由重试。"
                            ),
                        }
                        logger.info(
                            "community empty route=%s op=%s elapsed_ms=%d",
                            route,
                            operation or "-",
                            elapsed_ms,
                        )
                        return _finish_comm(empty_payload, is_error=False)
                    if non_healthy_sources:
                        deg_msg = (
                            stderr_tail
                            or "last30days failed with degraded sources: "
                            + ", ".join(
                                f"{k} ({v})"
                                for k, v in sorted(non_healthy_sources.items())
                            )
                        )
                    else:
                        deg_msg = (
                            stderr_tail
                            or "last30days returned no items with empty source_status"
                        )
                    degraded_payload = {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "degraded",
                        "source_status": sanitized_src_status,
                        "data": sanitize_error_data_summary(
                            parsed,
                            route=route,
                            operation=operation,
                            status="degraded",
                            error=deg_msg,
                        ),
                        "error": deg_msg,
                        "stderr": stderr_tail,
                    }
                    return _finish_comm(degraded_payload, is_error=True)
                if isinstance(parsed, dict) and non_healthy_sources:
                    existing_warnings = (
                        list(parsed.get("warnings"))
                        if isinstance(parsed.get("warnings"), list)
                        else []
                    )
                    for src_name, state in sorted(non_healthy_sources.items()):
                        warn_line = f"Source '{src_name}' failed with status '{state}'"
                        if warn_line not in existing_warnings:
                            existing_warnings.append(warn_line)
                    parsed["warnings"] = [
                        sanitize_stderr_text(str(w), max_len=300)
                        for w in existing_warnings
                    ]

            if _is_empty_community_output(route, operation, parsed):
                empty_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": True,
                    "status": "empty",
                    "empty_reason": "no_matching_items",
                    "data": parsed,
                    "stderr": stderr_tail,
                    "hint": (
                        f"社区路由 '{route}' 查询成功但未检索到匹配结果；可放宽检索词、调整时间范围或换用其他社区路由重试。"
                    ),
                }
                logger.info(
                    "community empty route=%s op=%s elapsed_ms=%d",
                    route,
                    operation or "-",
                    elapsed_ms,
                )
                return _finish_comm(empty_payload, is_error=False)

            base_envelope: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "data": parsed,
            }
            declared = source_identity.declare("community", arguments, base_envelope)
            enriched: dict[str, Any] = {
                "_declared_sources": declared,
                "_sources": declared,
                "ok": True,
                "status": "ok",
                "data": parsed,
                "normalized_params": norm_params,
            }
            if (
                route == "last30days"
                and isinstance(parsed, dict)
                and isinstance(parsed.get("warnings"), list)
                and parsed["warnings"]
            ):
                enriched["warnings"] = list(parsed["warnings"])
            logger.info(
                "tools/call tool=community route=%s op=%s status=ok declared_sources=%d elapsed_ms=%d",
                route,
                operation or "-",
                len(declared),
                elapsed_ms,
            )
            return self._make_tool_result_response(req_id, enriched, is_error=False)
        finally:
            if plan_file is not None:
                try:
                    plan_file.unlink(missing_ok=True)
                except Exception:
                    pass

    def _reference_date(self) -> date:
        """Return the current `America/New_York` calendar date for market-regime freshness checks."""
        return datetime.now(ZoneInfo("America/New_York")).date()

    def _read_cached_json_sync(
        self, path: Path, *, strip_crowding_history: bool = False
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """Read and parse a JSON object file synchronously with in-process `(mtime_ns, size)` caching."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except OSError as exc:
            return ("source_missing", None, f"Cannot access {path.name}: {type(exc).__name__}")
        if not path.is_file():
            return ("source_missing", None, f"Not a regular file: {path.name}")

        cache_key = f"json:{int(strip_crowding_history)}:{path}"
        cached = self._file_cache.get(cache_key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return ("ok", cached[2], None)

        try:
            raw_text = path.read_text(encoding="utf-8")
            parsed = json.loads(raw_text)
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except Exception as exc:
            return ("invalid_source", None, f"Failed to parse JSON in {path.name}: {type(exc).__name__}")

        if not isinstance(parsed, dict):
            return ("invalid_source", None, f"Expected JSON object in {path.name}")

        if strip_crowding_history and isinstance(parsed.get("rows"), list):
            parsed = dict(parsed)
            parsed["rows"] = [
                {k: v for k, v in r.items() if k != "history_1y"}
                for r in parsed["rows"]
                if isinstance(r, dict)
            ]

        self._file_cache[cache_key] = (st.st_mtime_ns, st.st_size, parsed)
        return ("ok", parsed, None)

    def _read_cached_csv_sync(
        self, path: Path
    ) -> tuple[str, list[dict[str, str]] | None, str | None]:
        """Read and parse a CSV file synchronously with in-process `(mtime_ns, size)` caching."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except OSError as exc:
            return ("source_missing", None, f"Cannot access {path.name}: {type(exc).__name__}")
        if not path.is_file():
            return ("source_missing", None, f"Not a regular file: {path.name}")

        cache_key = f"csv:{path}"
        cached = self._file_cache.get(cache_key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return ("ok", cached[2], None)

        try:
            raw_text = path.read_text(encoding="utf-8")
            lines = [ln for ln in raw_text.splitlines() if ln.strip()]
            if not lines:
                return ("invalid_source", None, f"Empty CSV in {path.name}")
            reader = csv.DictReader(lines)
            if not reader.fieldnames or "date" not in reader.fieldnames:
                return ("invalid_source", None, f"Invalid CSV header in {path.name}")
            rows: list[dict[str, str]] = []
            for r in reader:
                if not isinstance(r, dict) or None in r:
                    return ("invalid_source", None, f"Malformed CSV row in {path.name}")
                if not str(r.get("date") or "").strip():
                    return ("invalid_source", None, f"Missing date value in {path.name}")
                rows.append({str(k): str(v or "") for k, v in r.items() if k is not None})
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except Exception as exc:
            return ("invalid_source", None, f"Failed to parse CSV in {path.name}: {type(exc).__name__}")

        if not rows:
            return ("invalid_source", None, f"No data rows in {path.name}")

        self._file_cache[cache_key] = (st.st_mtime_ns, st.st_size, rows)
        return ("ok", rows, None)

    def _build_market_regime_sync(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Build the `macro(route='market-regime')` response synchronously (called via `asyncio.to_thread`)."""
        raw_params = arguments.get("params")
        params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
        op = (
            str(arguments.get("operation") or params.get("operation") or "snapshot")
            .strip()
            .lower()
            or "snapshot"
        )
        if op not in MARKET_REGIME_OPERATIONS:
            return {
                "ok": False,
                "status": "invalid_source",
                "route": "market-regime",
                "operation": op,
                "error": f"Unsupported market-regime operation '{op}' (expected one of {list(MARKET_REGIME_OPERATIONS)})",
            }

        ref_date = self._reference_date()
        latest_us_td = _latest_us_trading_day(ref_date).isoformat()
        latest_cn_td_m1 = _prev_cn_trading_day(ref_date, 1).isoformat()

        if op in ("snapshot", "us_topbottom"):
            status, data, err = self._read_cached_json_sync(self.cfg.sentiment_signals_json)
            if status != "ok" or data is None:
                return {
                    "ok": False,
                    "status": status,
                    "route": "market-regime",
                    "operation": op,
                    "error": err or status,
                }
            if data.get("ok") is False:
                return {
                    "ok": False,
                    "status": "invalid_source",
                    "route": "market-regime",
                    "operation": op,
                    "error": "signals.json reported ok=false",
                }
            us_market = data.get("us_market")
            topbottom = us_market.get("topbottom") if isinstance(us_market, dict) else None
            top_fw = topbottom.get("top_framework") if isinstance(topbottom, dict) else None
            bot_fw = topbottom.get("bottom_framework") if isinstance(topbottom, dict) else None
            if not isinstance(top_fw, dict) or not isinstance(bot_fw, dict):
                return {
                    "ok": False,
                    "status": "invalid_source",
                    "route": "market-regime",
                    "operation": op,
                    "error": "Malformed signals.json: missing us_market.topbottom frameworks",
                }

            sig_date = str(data.get("date") or "").strip()[:10]
            raw_as_of = data.get("as_of")
            as_of_dict = dict(raw_as_of) if isinstance(raw_as_of, dict) else {}
            raw_warnings = data.get("warnings")
            warnings: list[str] = (
                [_scrub_local_paths(w) for w in raw_warnings if str(w).strip()]
                if isinstance(raw_warnings, list)
                else []
            )
            us_stale = False
            if not sig_date or sig_date < latest_us_td:
                us_stale = True
                warnings.append(
                    f"signals.json date ({sig_date or 'missing'}) is earlier than latest US trading day ({latest_us_td})"
                )

            if op == "snapshot":
                cn_crowding_as_of = str(as_of_dict.get("cn_crowding") or "").strip()[:10]
                cn_margin_as_of = str(as_of_dict.get("cn_margin") or "").strip()[:10]
                cn_stale = False
                if cn_crowding_as_of and cn_crowding_as_of < latest_cn_td_m1:
                    cn_stale = True
                    warnings.append(
                        f"cn_crowding as_of ({cn_crowding_as_of}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
                    )
                if cn_margin_as_of and cn_margin_as_of < latest_cn_td_m1:
                    cn_stale = True
                    warnings.append(
                        f"cn_margin as_of ({cn_margin_as_of}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
                    )

                mom = us_market.get("momentum") if isinstance(us_market.get("momentum"), dict) else {}
                ndx_spw = us_market.get("ndx_spw") if isinstance(us_market.get("ndx_spw"), dict) else {}
                cn_a = data.get("cn_a") if isinstance(data.get("cn_a"), dict) else {}
                hk = data.get("hk") if isinstance(data.get("hk"), dict) else {}
                asia = data.get("asia") if isinstance(data.get("asia"), dict) else {}
                snap_res: dict[str, Any] = {
                    "ok": True,
                    "status": "ok",
                    "route": "market-regime",
                    "operation": "snapshot",
                    "date": sig_date,
                    "as_of": as_of_dict,
                    "stale": us_stale,
                    "us_topbottom": {
                        "top_framework": {
                            "stage": top_fw.get("stage"),
                            "active": top_fw.get("active"),
                            "summary": top_fw.get("summary"),
                            "window_remaining_td": top_fw.get("window_remaining_td"),
                        },
                        "bottom_framework": {
                            "stage": bot_fw.get("stage"),
                            "active": bot_fw.get("active"),
                            "summary": bot_fw.get("summary"),
                            "window_remaining_td": bot_fw.get("window_remaining_td"),
                        },
                    },
                    "put_hedge": top_fw.get("put_hedge"),
                    "momentum": {
                        "reading": mom.get("reading"),
                        "gscbhmom": mom.get("gscbhmom"),
                    },
                    "cn_a": {
                        "reading": cn_a.get("reading"),
                        "margin_rsi14": cn_a.get("margin_rsi14"),
                        "stale": cn_stale,
                    },
                    "hk": {
                        "reading": hk.get("reading"),
                        "ah_rsi30": hk.get("ah_rsi30"),
                    },
                    "asia": {
                        "reading": asia.get("reading"),
                    },
                    "warnings": warnings,
                }
                if ndx_spw.get("rsi14") is not None:
                    snap_res["ndx_spw"] = {
                        "rsi14": ndx_spw.get("rsi14"),
                    }
                return snap_res

            def _strip_large_arrays(fw: dict[str, Any]) -> dict[str, Any]:
                return {
                    k: v
                    for k, v in fw.items()
                    if k not in ("cycles", "signals", "history", "rows", "series")
                    and (k == "warnings" or not isinstance(v, list))
                }

            return {
                "ok": True,
                "status": "ok",
                "route": "market-regime",
                "operation": "us_topbottom",
                "date": sig_date,
                "generated_at": data.get("generated_at"),
                "as_of": as_of_dict,
                "stale": us_stale,
                "top_framework": _strip_large_arrays(top_fw),
                "bottom_framework": _strip_large_arrays(bot_fw),
                "warnings": warnings,
            }

        if op == "crowding":
            status, data, err = self._read_cached_json_sync(
                self.cfg.index_crowding_json, strip_crowding_history=True
            )
            if status != "ok" or data is None:
                return {
                    "ok": False,
                    "status": status,
                    "route": "market-regime",
                    "operation": "crowding",
                    "error": err or status,
                }
            raw_rows = data.get("rows")
            if data.get("ok") is False or not isinstance(raw_rows, list):
                return {
                    "ok": False,
                    "status": "invalid_source",
                    "route": "market-regime",
                    "operation": "crowding",
                    "error": "Malformed official-all.json: missing rows list",
                }

            trade_date = str(data.get("trade_date") or "").strip()[:10]
            warnings = []
            stale = False
            if not trade_date or trade_date < latest_cn_td_m1:
                stale = True
                warnings.append(
                    f"crowding trade_date ({trade_date or 'missing'}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
                )

            raw_top = arguments.get("top")
            if raw_top is None:
                raw_top = params.get("top")
            if raw_top is None:
                raw_top = arguments.get("limit") if arguments.get("limit") is not None else params.get("limit")
            try:
                top_n = max(1, min(int(raw_top), 30)) if raw_top is not None and not isinstance(raw_top, bool) else 10
            except (TypeError, ValueError):
                top_n = 10

            raw_order = str(arguments.get("order") or params.get("order") or "high").strip().lower()
            order = "low" if raw_order == "low" else "high"
            category_filter = str(arguments.get("category") or params.get("category") or "").strip()
            query_filter = str(arguments.get("query") or params.get("query") or "").strip()

            filtered_rows: list[dict[str, Any]] = []
            cat_lower = category_filter.lower()
            q_lower = query_filter.lower()
            for r in raw_rows:
                if not isinstance(r, dict):
                    continue
                clean_r = {k: v for k, v in r.items() if k != "history_1y"}
                if cat_lower:
                    row_cat = f"{clean_r.get('category') or ''} {clean_r.get('category_code') or ''}".lower()
                    if cat_lower not in row_cat:
                        continue
                if q_lower:
                    row_q = f"{clean_r.get('index_name') or ''} {clean_r.get('index_code') or ''}".lower()
                    if q_lower not in row_q:
                        continue
                filtered_rows.append(clean_r)

            def _crowding_sort_key(item: dict[str, Any]) -> float:
                val = item.get("crowding_percentile")
                try:
                    return float(val) if val is not None else (-1e9 if order == "high" else 1e9)
                except (TypeError, ValueError):
                    return -1e9 if order == "high" else 1e9

            filtered_rows.sort(key=_crowding_sort_key, reverse=(order == "high"))
            selected_rows = filtered_rows[:top_n]
            return {
                "ok": True,
                "status": "ok",
                "route": "market-regime",
                "operation": "crowding",
                "trade_date": trade_date,
                "as_of": trade_date,
                "stale": stale,
                "order": order,
                "top": top_n,
                "category": category_filter or None,
                "query": query_filter or None,
                "index_count": data.get("index_count", len(raw_rows)),
                "matched_count": len(filtered_rows),
                "count": len(selected_rows),
                "rows": selected_rows,
                "warnings": warnings,
            }

        # op == "cn_leverage"
        p_status, pipe_data, p_err = self._read_cached_json_sync(self.cfg.cn_leverage_status_json)
        if p_status != "ok" or pipe_data is None:
            return {
                "ok": False,
                "status": p_status,
                "route": "market-regime",
                "operation": "cn_leverage",
                "error": p_err or p_status,
            }
        if not pipe_data or not all(
            isinstance(v, dict) and "status" in v for v in pipe_data.values()
        ):
            return {
                "ok": False,
                "status": "invalid_source",
                "route": "market-regime",
                "operation": "cn_leverage",
                "error": "Malformed pipeline_status.json: expected mapping of pipeline status dicts",
            }

        c_status, csv_rows, c_err = self._read_cached_csv_sync(self.cfg.cn_leverage_unwind_csv)
        if c_status != "ok" or csv_rows is None:
            return {
                "ok": False,
                "status": c_status,
                "route": "market-regime",
                "operation": "cn_leverage",
                "error": c_err or c_status,
            }

        tail_raw = csv_rows[-5:]
        unwind_rows: list[dict[str, Any]] = []
        for r in tail_raw:
            item: dict[str, Any] = {}
            for col in CN_LEVERAGE_KEY_COLUMNS:
                if col in r:
                    item[col] = _parse_csv_scalar(r[col])
            if len(item) <= 1 and len(r) > 1:
                for k, v in list(r.items())[:6]:
                    item[k] = _parse_csv_scalar(v)
            unwind_rows.append(item)

        latest_date = str(unwind_rows[-1].get("date") or "").strip()[:10] if unwind_rows else ""
        warnings = []
        stale = False
        if not latest_date or latest_date < latest_cn_td_m1:
            stale = True
            warnings.append(
                f"cn_leverage unwind date ({latest_date or 'missing'}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
            )

        pipeline_rows: list[dict[str, Any]] = []
        for p_name, p_info in pipe_data.items():
            if not isinstance(p_info, dict):
                continue
            st_val = str(p_info.get("status") or "").strip()
            pipeline_rows.append(
                {
                    "name": str(p_name),
                    "status": st_val,
                    "updated": p_info.get("updated"),
                }
            )
            if st_val.lower() not in ("completed", "ok", "success", ""):
                detail_clean = _scrub_local_paths(p_info.get("detail") or "")
                warn_msg = f"pipeline {p_name} status={st_val}"
                if detail_clean:
                    warn_msg = f"{warn_msg}: {detail_clean}"
                warnings.append(warn_msg)

        return {
            "ok": True,
            "status": "ok",
            "route": "market-regime",
            "operation": "cn_leverage",
            "date": latest_date,
            "as_of": latest_date,
            "stale": stale,
            "pipeline_status": pipeline_rows,
            "latest_unwind": unwind_rows,
            "warnings": warnings,
        }

    async def _handle_market_regime(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Read and parse OpenClaw market-regime files in a background thread (`asyncio.to_thread`)."""
        return await asyncio.to_thread(self._build_market_regime_sync, arguments)

    async def handle_tools_call(
        self, req_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        raw_args = params.get("arguments")
        raw_arguments: dict[str, Any] = (
            dict(raw_args) if isinstance(raw_args, dict) else {}
        )
        arguments, norm_params_view, validation_err = _normalize_and_validate_call_args(
            tool_name, raw_arguments
        )
        route = str(arguments.get("route") or "").strip()
        operation = str(arguments.get("operation") or "").strip()

        handoff = self.handoff_routes()
        if validation_err is not None:
            return self._make_tool_result_response(req_id, validation_err, is_error=True)

        # Route `catalog` first so `catalog(route=...)` works for all routes (including community and market-regime)
        if tool_name == "catalog":
            if route and route not in ROUTE_CONTRACT_SPECS:
                err_cat: dict[str, Any] = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_route",
                    "accepted_routes": sorted(ROUTE_CONTRACT_SPECS.keys()),
                    "error": (
                        f"未知路由 '{route}'；可查询的路由列表: {sorted(ROUTE_CONTRACT_SPECS.keys())}。"
                    ),
                }
                _apply_error_and_empty_contract(
                    tool_name, route, "", err_cat, norm_params_view
                )
                return self._make_tool_result_response(
                    req_id, err_cat, is_error=True
                )
        else:
            if route and route in handoff:
                err_payload: dict[str, Any] = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "handoff_filtered",
                    "error": f"Route '{route}' is an OpenClaw handoff route and is disabled in LangAlpha.",
                }
                _apply_error_and_empty_contract(
                    tool_name, route, operation, err_payload, norm_params_view
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(err_payload, ensure_ascii=False),
                            }
                        ],
                        "isError": True,
                    },
                }
            # Upstream rejects a route it doesn't know (or one registered under
            # another category tool) with a message the classifier can't tell
            # from an outage; every non-community route the bridge serves has a
            # spec naming its tool, so an unknown or misrouted one is the
            # caller's mistake.
            route_spec = ROUTE_CONTRACT_SPECS.get(route)
            owner_tool = route_spec.get("tool") if route_spec else None
            if (
                route
                and tool_name != "community"
                and route not in BRIDGE_COMMUNITY_ROUTES
                and owner_tool != tool_name
            ):
                tool_routes = sorted(
                    rid
                    for rid, r_spec in ROUTE_CONTRACT_SPECS.items()
                    if r_spec.get("tool") == tool_name
                )
                if owner_tool:
                    hint = (
                        f"route='{route}' 属于 {owner_tool}，请用 {owner_tool}(route='{route}', ...) 调用；"
                        f"{tool_name} 的可选路由: {tool_routes}（也可调用 catalog(route='{route}') 查看参数契约）。"
                    )
                else:
                    hint = (
                        f"请将 route 改为 {tool_routes} 之一；不确定时先调用 catalog() 查看全部路由，"
                        "或 catalog(route=...) 查看单个路由的参数契约。"
                    )
                err_route: dict[str, Any] = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_route",
                    "error_category": "参数错误",
                    "accepted_routes": tool_routes,
                    "error": (
                        f"[参数错误] {tool_name} 不支持 route='{route}'。可选路由: {tool_routes}。"
                    ),
                    "hint": hint,
                }
                _apply_error_and_empty_contract(
                    tool_name, route, operation, err_route, norm_params_view
                )
                return self._make_tool_result_response(req_id, err_route, is_error=True)

        eff_timeout = self.compute_timeout(tool_name, arguments)
        if tool_name != "catalog" and (
            tool_name == "community" or route in BRIDGE_COMMUNITY_ROUTES
        ):
            if tool_name not in ("community", "") or route not in BRIDGE_COMMUNITY_ROUTES:
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_arguments",
                    "error": (
                        f"Invalid community tool/route combination: tool='{tool_name}', route='{route}'."
                    ),
                    "data": None,
                }
                _apply_error_and_empty_contract(
                    tool_name, route, operation, err_payload, norm_params_view
                )
                return self._make_tool_result_response(req_id, err_payload, is_error=True)
            return await self._handle_community_call(
                req_id, route, arguments, eff_timeout
            )
        call_args = dict(arguments)
        if tool_name == "catalog":
            call_args.pop("route", None)
        else:
            call_args["timeout"] = eff_timeout

        params_clamped: dict[str, list[Any]] = {}
        raw_params_obj = call_args.get("params")
        if isinstance(raw_params_obj, dict):
            clamped_p = dict(raw_params_obj)
            mc_raw = clamped_p.get("max_chars")
            mc_int = _parse_int_param(mc_raw)
            if mc_int is not None:
                if mc_int > MAX_CHARS_CEILING:
                    params_clamped["max_chars"] = [mc_raw, MAX_CHARS_CEILING]
                    clamped_p["max_chars"] = MAX_CHARS_CEILING
                elif mc_int < MAX_CHARS_FLOOR:
                    params_clamped["max_chars"] = [mc_raw, MAX_CHARS_FLOOR]
                    clamped_p["max_chars"] = MAX_CHARS_FLOOR
                elif isinstance(mc_raw, str):
                    clamped_p["max_chars"] = mc_int
            off_raw = clamped_p.get("offset")
            off_int = _parse_int_param(off_raw)
            if off_int is not None:
                if off_int < 0:
                    params_clamped["offset"] = [off_raw, 0]
                    clamped_p["offset"] = 0
                elif isinstance(off_raw, str):
                    clamped_p["offset"] = off_int
            call_args["params"] = clamped_p

        params_mapped_hint: str | None = None
        if tool_name == "research" and route in INTERNAL_LIBRARY_ROUTES:
            p_dict = call_args.get("params")
            if isinstance(p_dict, dict):
                doc_keys_present = [
                    k for k in ("document", "doc_id", "id", "document_id") if k in p_dict
                ]
                if doc_keys_present:
                    bad_k = doc_keys_present[0]
                    extracted_doc = ""
                    for k in doc_keys_present:
                        v = p_dict.get(k)
                        if not extracted_doc and v is not None and str(v).strip():
                            extracted_doc = str(v).strip()
                    how_to_pass_msg = (
                        f"未知内部资料参数: {bad_k}。内部资料库不支持 params.{bad_k}；"
                        f'读取文档请传顶层参数 research(route="{route}", operation="read", '
                        f'document="{extracted_doc or "<wiki:|mail:|tmtb:|digest:|funda:><id>"}", '
                        'params={"offset": 0, "max_chars": 30000})。'
                    )
                    if (
                        operation in INTERNAL_LIBRARY_READ_OPERATIONS
                        and (call_args.get("document") or extracted_doc)
                    ):
                        p_clean = dict(p_dict)
                        for k in doc_keys_present:
                            p_clean.pop(k, None)
                        call_args["params"] = p_clean
                        if not call_args.get("document"):
                            call_args["document"] = extracted_doc
                        params_mapped_hint = (
                            f'内部资料库读取请传顶层参数 document="{call_args["document"]}"'
                            f"（而非 params.{bad_k}），桥接层已自动映射。"
                        )
                    elif not operation:
                        # operation 为空时保持原样，只在结果里返回一条"应该怎么传"的提示
                        params_mapped_hint = how_to_pass_msg
                    else:
                        err_payload = {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "error",
                            "error": how_to_pass_msg,
                            "hint": how_to_pass_msg,
                        }
                        if params_clamped:
                            err_payload["params_clamped"] = params_clamped
                        _apply_error_and_empty_contract(
                            tool_name, route, operation, err_payload, norm_params_view
                        )
                        return self._make_tool_result_response(
                            req_id, err_payload, is_error=True
                        )

            doc_candidate = str(call_args.get("document") or "").strip()
            if doc_candidate and re.fullmatch(r"\d+", doc_candidate):
                ae_hint = (
                    f'这是 AlphaEngine 文档，用 research(route="alphaengine", '
                    f'operation="get_document", params={{"doc_id": "{doc_candidate}"}}) 读取'
                )
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "error",
                    "document_id": doc_candidate,
                    "doc_id": doc_candidate,
                    "error": ae_hint,
                    "hint": ae_hint,
                }
                if params_clamped:
                    err_payload["params_clamped"] = params_clamped
                _apply_error_and_empty_contract(
                    tool_name, route, operation, err_payload, norm_params_view
                )
                return self._make_tool_result_response(req_id, err_payload, is_error=True)

        transcript_query_ignored: str | None = None
        if tool_name == "research" and route == "transcript":
            t_ticker = str(call_args.get("ticker") or "").strip()
            t_query = str(call_args.get("query") or "").strip()
            if t_ticker and t_query and _is_topic_query_for_transcript(t_query):
                transcript_query_ignored = t_query
                call_args.pop("query", None)

        norm_params_view = _build_normalized_params_view(call_args, is_community=False)
        if tool_name == "catalog" and route:
            norm_params_view["route"] = route

        do_fanout = self._should_fanout_alphaengine(tool_name, arguments)
        ae_args: dict[str, Any] | None = (
            self._build_alphaengine_fanout_args(arguments) if do_fanout else None
        )
        gp_args: dict[str, Any] | None = (
            self._build_guidepoint_fanout_args(arguments) if do_fanout else None
        )
        ae_outcome: dict[str, Any] | None = None
        gp_outcome: dict[str, Any] | None = None

        t0 = time.monotonic()
        test_delay_s = self.cfg.resolve_test_delay_s()
        async with self.sem:
            try:
                if test_delay_s > 0.0:
                    await asyncio.wait_for(
                        asyncio.sleep(test_delay_s),
                        timeout=float(eff_timeout),
                    )
                elapsed_so_far = time.monotonic() - t0
                remaining_budget_s = max(
                    5.0, float(eff_timeout) - elapsed_so_far + 5.0
                )
                rpc_req = {
                    "jsonrpc": "2.0",
                    "id": req_id if req_id is not None else 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": call_args},
                }
                if tool_name != "catalog" and route == "market-regime":
                    mr_payload = await asyncio.wait_for(
                        self._handle_market_regime(arguments),
                        timeout=remaining_budget_s,
                    )
                    resps = [
                        {
                            "jsonrpc": "2.0",
                            "id": req_id if req_id is not None else 1,
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            mr_payload, ensure_ascii=False
                                        ),
                                    }
                                ],
                                "structuredContent": dict(mr_payload),
                                "isError": not bool(mr_payload.get("ok")),
                            },
                        }
                    ]
                elif ae_args is not None and gp_args is not None:
                    ae_timeout = int(
                        ae_args.get("timeout") or FANOUT_ALPHAENGINE_TIMEOUT_S
                    )
                    ae_budget_s = max(
                        5.0, float(ae_timeout) - elapsed_so_far + 5.0
                    )
                    gp_timeout = int(
                        gp_args.get("timeout") or FANOUT_GUIDEPOINT_TIMEOUT_S
                    )
                    gp_budget_s = max(
                        5.0, float(gp_timeout) - elapsed_so_far + 5.0
                    )
                    ae_task = asyncio.create_task(
                        self._run_alphaengine_fanout(req_id, ae_args, ae_budget_s)
                    )
                    gp_task = asyncio.create_task(
                        self._run_guidepoint_fanout(req_id, gp_args, gp_budget_s)
                    )
                    try:
                        try:
                            resps = await self._run_stdio_rpc(
                                [rpc_req], timeout_s=remaining_budget_s
                            )
                        except (asyncio.TimeoutError, Exception) as local_exc:
                            # Let online calls that are already done finish before cancelling the rest.
                            await asyncio.sleep(0)
                            if not ae_task.done():
                                ae_task.cancel()
                            if not gp_task.done():
                                gp_task.cancel()
                            raw_ae, raw_gp = await asyncio.gather(
                                ae_task, gp_task, return_exceptions=True
                            )
                            is_local_timeout = isinstance(local_exc, asyncio.TimeoutError)
                            local_st = "timeout" if is_local_timeout else "failed"
                            local_reason = (
                                f"researchdata_bridge timed out after {eff_timeout}s on {tool_name}/{route}"
                                if is_local_timeout
                                else (
                                    f"SubprocessExitError(exit_code={local_exc.exit_code})"
                                    if isinstance(local_exc, SubprocessExitError)
                                    else type(local_exc).__name__
                                )
                            )
                            ae_outcome = (
                                raw_ae
                                if isinstance(raw_ae, dict)
                                else {
                                    "ok": False,
                                    "status": "canceled",
                                    "route": "alphaengine",
                                    "reason": f"canceled after local all {local_st}",
                                    "count": 0,
                                    "results": [],
                                    "raw_payload": None,
                                }
                            )
                            gp_outcome = (
                                raw_gp
                                if isinstance(raw_gp, dict)
                                else {
                                    "ok": False,
                                    "status": "canceled",
                                    "route": "guidepoint",
                                    "reason": f"canceled after local all {local_st}",
                                    "count": 0,
                                    "results": [],
                                    "raw_payload": None,
                                }
                            )
                            fallback_payload: dict[str, Any] = {
                                "ok": False,
                                "status": local_st,
                                "error": local_reason,
                                "results": [],
                                "result_count": 0,
                                "source_status": {
                                    "local_all": {
                                        "ok": False,
                                        "status": local_st,
                                        "reason": local_reason,
                                        "error": local_reason,
                                        "count": 0,
                                    }
                                },
                            }
                            resps = [
                                {
                                    "jsonrpc": "2.0",
                                    "id": req_id if req_id is not None else 1,
                                    "result": {
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": json.dumps(
                                                    fallback_payload,
                                                    ensure_ascii=False,
                                                ),
                                            }
                                        ],
                                        "structuredContent": dict(fallback_payload),
                                        "isError": True,
                                    },
                                }
                            ]
                        except BaseException:
                            ae_task.cancel()
                            gp_task.cancel()
                            await asyncio.gather(
                                ae_task, gp_task, return_exceptions=True
                            )
                            raise
                        else:
                            ae_outcome, gp_outcome = await asyncio.gather(ae_task, gp_task)
                    finally:
                        # Whatever ends the fan-out (an error, a cancel mid-handler), no online
                        # subprocess outlives the call: cancel what is still running and reap it.
                        await _cancel_and_reap((ae_task, gp_task))
                else:
                    resps = await self._run_stdio_rpc(
                        [rpc_req], timeout_s=remaining_budget_s
                    )
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    "tools/call timeout tool=%s route=%s op=%s timeout_s=%d elapsed_ms=%d",
                    tool_name,
                    route,
                    operation,
                    eff_timeout,
                    elapsed_ms,
                )
                timeout_payload: dict[str, Any] = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "timeout",
                    "error": f"researchdata_bridge timed out after {eff_timeout}s on {tool_name}/{route}",
                }
                _apply_error_and_empty_contract(
                    tool_name, route, operation, timeout_payload, norm_params_view
                )
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    timeout_payload, ensure_ascii=False
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp = resps[0] if resps else {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Empty response from researchdata_mcp"}}
        resp["id"] = req_id

        result_obj = resp.get("result")
        status_str = "ok"
        declared_count = 0

        if isinstance(result_obj, dict) and isinstance(result_obj.get("content"), list):
            blocks = result_obj["content"]
            if blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    payload = json.loads(blocks[0]["text"])
                except Exception:
                    payload = None

                if isinstance(payload, dict):
                    # Filter handoff routes from catalog() output and include market-regime + community routes
                    if tool_name == "catalog" and isinstance(payload.get("routes"), list):
                        req_category = str(arguments.get("category") or "").strip().lower()
                        req_route = str(arguments.get("route") or "").strip()
                        if req_route and req_route not in ROUTE_CONTRACT_SPECS:
                            err_cat: dict[str, Any] = {
                                "_declared_sources": [],
                                "_sources": [],
                                "ok": False,
                                "status": "invalid_arguments",
                                "error_code": "invalid_route",
                                "accepted_routes": sorted(ROUTE_CONTRACT_SPECS.keys()),
                                "error": (
                                    f"未知路由 '{req_route}'；可查询的路由列表: {sorted(ROUTE_CONTRACT_SPECS.keys())}。"
                                ),
                            }
                            _apply_error_and_empty_contract(
                                tool_name, req_route, "", err_cat, norm_params_view
                            )
                            return self._make_tool_result_response(
                                req_id, err_cat, is_error=True
                            )
                        include_community = not req_category or req_category == "community"
                        include_macro = not req_category or req_category == "macro"
                        kept_routes: list[dict[str, Any]] = []
                        seen_comm: set[str] = set()
                        for r in payload["routes"]:
                            if not isinstance(r, dict):
                                continue
                            rid = str(r.get("id") or "").strip()
                            if not rid or rid in handoff:
                                continue
                            if rid in COMMUNITY_CATALOG_ROUTE_SPECS:
                                if not include_community:
                                    continue
                                seen_comm.add(rid)
                                merged_r = dict(r)
                                merged_r.update(
                                    json.loads(
                                        json.dumps(COMMUNITY_CATALOG_ROUTE_SPECS[rid])
                                    )
                                )
                                kept_routes.append(merged_r)
                            else:
                                r_copy = dict(r)
                                rules_list = list(r_copy.get("rules") or [])
                                if rid == "transcript":
                                    if TRANSCRIPT_CATALOG_RULE not in rules_list:
                                        rules_list.append(TRANSCRIPT_CATALOG_RULE)
                                    r_copy["rules"] = rules_list
                                elif rid in INTERNAL_LIBRARY_ROUTES:
                                    if INTERNAL_LIBRARY_PAGINATION_RULE not in rules_list:
                                        rules_list.append(INTERNAL_LIBRARY_PAGINATION_RULE)
                                    r_copy["rules"] = rules_list
                                kept_routes.append(r_copy)
                        if include_macro:
                            existing_ids = {
                                str(r.get("id") or "")
                                for r in kept_routes
                                if isinstance(r, dict)
                            }
                            if "market-regime" not in existing_ids:
                                kept_routes.append(dict(MARKET_REGIME_CATALOG_ENTRY))
                        if include_community:
                            for comm_id in ("xueqiu", "employee", "last30days", "x-timeline"):
                                if comm_id not in seen_comm and comm_id not in handoff:
                                    kept_routes.append(
                                        json.loads(
                                            json.dumps(COMMUNITY_CATALOG_ROUTE_SPECS[comm_id])
                                        )
                                    )
                        if req_route:
                            kept_routes = [
                                r for r in kept_routes if str(r.get("id") or "") == req_route
                            ]
                            if not kept_routes and req_route in ROUTE_CONTRACT_SPECS:
                                spec_entry = ROUTE_CONTRACT_SPECS[req_route]
                                synth: dict[str, Any] = {
                                    "id": req_route,
                                    "tool": spec_entry["tool"],
                                    "category": spec_entry["category"],
                                    "operations": list(spec_entry["operations"]),
                                }
                                if req_route == "transcript":
                                    synth["rules"] = [TRANSCRIPT_CATALOG_RULE]
                                elif req_route in INTERNAL_LIBRARY_ROUTES:
                                    synth["rules"] = [INTERNAL_LIBRARY_PAGINATION_RULE]
                                kept_routes.append(synth)
                        for r_item in kept_routes:
                            rid = str(r_item.get("id") or "").strip()
                            c_spec = ROUTE_CONTRACT_SPECS.get(rid)
                            if c_spec is not None:
                                r_item.setdefault("tool", c_spec["tool"])
                                if not r_item.get("operations"):
                                    r_item["operations"] = list(c_spec["operations"])
                                r_item["required_params"] = dict(c_spec["required_params"])
                                r_item["optional_params"] = list(c_spec["optional_params"])
                                r_item["accepted_params"] = list(c_spec["accepted_params"])
                                r_item["param_formats"] = dict(c_spec["param_formats"])
                                r_item["return_shape"] = c_spec["return_shape"]
                                r_item["examples"] = list(c_spec["examples"])
                        payload["routes"] = kept_routes
                        payload["route_count"] = len(payload["routes"])

                    if tool_name == "research" and route == "transcript":
                        if transcript_query_ignored is not None:
                            payload["query_ignored"] = transcript_query_ignored
                            payload["query_ignored_note"] = TRANSCRIPT_QUERY_IGNORED_NOTE
                            existing_note = str(payload.get("note") or "").strip()
                            payload["note"] = (
                                f"{existing_note} {TRANSCRIPT_QUERY_IGNORED_NOTE}".strip()
                                if existing_note
                                else TRANSCRIPT_QUERY_IGNORED_NOTE
                            )
                            if isinstance(payload.get("data"), dict):
                                payload["data"]["query_ignored"] = transcript_query_ignored
                                d_note = str(payload["data"].get("note") or "").strip()
                                payload["data"]["note"] = (
                                    f"{d_note} {TRANSCRIPT_QUERY_IGNORED_NOTE}".strip()
                                    if d_note
                                    else TRANSCRIPT_QUERY_IGNORED_NOTE
                                )

                    if params_clamped:
                        payload["params_clamped"] = params_clamped
                    if params_mapped_hint:
                        # Keep the downstream hint (truncation, errors); the mapping note rides along.
                        prior_hint = str(payload.get("hint") or "").strip()
                        payload["hint"] = (
                            f"{prior_hint} {params_mapped_hint}"
                            if prior_hint and params_mapped_hint not in prior_hint
                            else params_mapped_hint
                        )

                    declared = source_identity.declare(tool_name, arguments, payload)
                    if ae_outcome is not None and ae_args is not None:
                        declared = self._merge_alphaengine_into_all(
                            payload, declared, ae_outcome, ae_args
                        )
                    if gp_outcome is not None and gp_args is not None:
                        declared = self._merge_guidepoint_into_all(
                            payload, declared, gp_outcome, gp_args
                        )
                    declared_count = len(declared)

                    # Track AlphaEngine get_document quota usage
                    if route == "alphaengine" and operation == "get_document":
                        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                        sc = inner.get("structuredContent") if isinstance(inner, dict) else None
                        chars = 0
                        if isinstance(sc, dict):
                            chars = int(sc.get("returned_chars") or len(str(sc.get("text") or "")))
                        await self._record_alphaengine_get_doc(chars)

                    _apply_error_and_empty_contract(
                        tool_name, route, operation, payload, norm_params_view
                    )
                    if params_mapped_hint:
                        cur_hint = str(payload.get("hint") or "").strip()
                        if params_mapped_hint not in cur_hint:
                            payload["hint"] = (
                                f"{cur_hint} {params_mapped_hint}".strip()
                                if cur_hint
                                else params_mapped_hint
                            )
                    status_str = str(payload.get("status") or ("ok" if payload.get("ok") else "error"))
                    result_obj["isError"] = not bool(payload.get("ok"))

                    # Place _declared_sources and _sources first so 64 KiB truncation preserves them
                    enriched = {
                        "_declared_sources": declared,
                        "_sources": declared,
                        **payload,
                    }
                    blocks[0]["text"] = json.dumps(
                        enriched,
                        ensure_ascii=False,
                        separators=(",", ":") if route == "market-regime" else None,
                    )
                    if isinstance(result_obj.get("structuredContent"), dict):
                        result_obj["structuredContent"] = dict(enriched)

        logger.info(
            "tools/call tool=%s route=%s op=%s status=%s declared_sources=%d elapsed_ms=%d",
            tool_name,
            route or "-",
            operation or "-",
            status_str,
            declared_count,
            elapsed_ms,
        )
        return resp

    def _resolve_company_wiki_python_and_cli(self) -> tuple[str, Path]:
        """Locate the OpenClaw company-wiki skill Python binary and CLI script without modifying them."""
        skill_dir = self.cfg.ocw_root / "skills" / "company-wiki"
        cli_script = skill_dir / "scripts" / "company_wiki_cli.py"
        for candidate in (
            skill_dir / "venv" / "bin" / "python",
            self.cfg.ocw_root / "skills" / "market-digest" / "venv" / "bin" / "python",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate), cli_script
        return self.cfg.python_bin, cli_script

    async def _run_company_wiki_embed(
        self,
        texts: list[str],
        task_type: str,
        requested_model: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Invoke OpenClaw company-wiki's embedding functions in a read-only subprocess.

        Reads `GEMINI_API_KEY` strictly from `self.cfg.env_path` so process environment variables
        can never override or supply the `/embed` key.
        """
        env_file_vars = _read_env_file_dict(self.cfg.env_path)
        gemini_api_key = env_file_vars.get("GEMINI_API_KEY", "").strip()
        if not gemini_api_key:
            return {
                "ok": False,
                "status": "error",
                "error": "GEMINI_API_KEY not configured in bridge env file",
            }

        py_bin, cli_script = self._resolve_company_wiki_python_and_cli()
        if not cli_script.is_file():
            return {
                "ok": False,
                "status": "error",
                "error": "company_wiki_cli.py not found under OpenClaw workspace",
            }

        driver_code = (
            "import contextlib, importlib.util, io, json, pathlib, sys\n"
            "sys.dont_write_bytecode = True\n"
            "req = json.loads(sys.stdin.read())\n"
            "spec = importlib.util.spec_from_file_location('company_wiki_cli', req['cli_script'])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "try:\n"
            "    cfg_path = pathlib.Path(getattr(mod, 'CONFIG_PATH', pathlib.Path(req['cli_script']).resolve().parent.parent / 'config.json'))\n"
            "    if not cfg_path.is_file():\n"
            "        raise FileNotFoundError('company-wiki config.json not found')\n"
            "    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))\n"
            "    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):\n"
            "        model_name = req.get('model')\n"
            "        dims = req.get('dims')\n"
            "        if not model_name:\n"
            "            model_name, dims = mod.resolve_embedding_model(cfg)\n"
            "        else:\n"
            "            dims = int(dims or cfg.get('embedding', {}).get('dimensions', 3072))\n"
            "        texts = req.get('texts') or []\n"
            "        if req.get('task_type') == 'RETRIEVAL_QUERY':\n"
            "            vecs = [[float(x) for x in mod.embed_query(t, model_name)] for t in texts]\n"
            "        else:\n"
            "            vecs = [[float(x) for x in v] for v in mod.embed_texts(texts, model_name)]\n"
            "    if vecs and vecs[0]:\n"
            "        dims = len(vecs[0])\n"
            "    print(json.dumps({'ok': True, 'status': 'ok', 'model': model_name, 'dimensions': dims, 'embeddings': vecs}))\n"
            "except FileNotFoundError:\n"
            "    print(json.dumps({'ok': False, 'status': 'error', 'error': 'company_wiki_config_missing'}))\n"
            "except SystemExit:\n"
            "    print(json.dumps({'ok': False, 'status': 'error', 'error': 'company-wiki embedding exited'}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'status': 'error', 'error': type(e).__name__}))\n"
        )

        cmd = [py_bin, "-B", "-W", "ignore", "-c", driver_code]
        subproc_env = {
            **os.environ,
            **env_file_vars,
            "GEMINI_API_KEY": gemini_api_key,
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cfg.ocw_root),
            env=subproc_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        req_payload = {
            "cli_script": str(cli_script),
            "texts": texts,
            "task_type": task_type,
            "model": requested_model or self._resolved_embed_model,
            "dims": self._resolved_embed_dims,
        }
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(input=json.dumps(req_payload, ensure_ascii=False).encode("utf-8")),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {"ok": False, "status": "timeout", "error": "Embedding subprocess timed out"}
        except Exception as exc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {"ok": False, "status": "error", "error": type(exc).__name__}

        for line in reversed(stdout_bytes.decode("utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return {
            "ok": False,
            "status": "error",
            "error": f"Embedding subprocess returned no JSON (exit_code={proc.returncode})",
        }

    async def handle_embed(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle `POST /embed` requests without logging input text or secrets."""
        raw_texts = body.get("texts")
        if raw_texts is None and "input" in body:
            raw_texts = body.get("input")
        if raw_texts is None and "text" in body:
            raw_texts = [body.get("text")]
        if isinstance(raw_texts, str):
            texts = [raw_texts]
        elif isinstance(raw_texts, list) and all(isinstance(x, str) for x in raw_texts):
            texts = [x for x in raw_texts]
        else:
            return 400, {"ok": False, "status": "bad_request", "error": "texts must be a string or list of strings"}

        if not texts or len(texts) > 100:
            return 400, {"ok": False, "status": "bad_request", "error": "texts must contain 1..100 items"}

        task_type = str(body.get("task_type") or "RETRIEVAL_DOCUMENT").strip().upper()
        if task_type not in ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"):
            task_type = "RETRIEVAL_DOCUMENT"
        req_model = str(body.get("model") or "").strip() or None

        t0 = time.monotonic()
        async with self.embed_sem:
            res = await self._run_company_wiki_embed(
                texts=texts,
                task_type=task_type,
                requested_model=req_model,
                timeout_s=120.0,
            )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if res.get("ok") and isinstance(res.get("embeddings"), list):
            model_used = str(res.get("model") or "gemini-embedding-001")
            dims_used = int(res.get("dimensions") or 3072)
            if not req_model:
                self._resolved_embed_model = model_used
                self._resolved_embed_dims = dims_used
            logger.info(
                "embed count=%d task_type=%s model=%s dims=%d status=ok elapsed_ms=%d",
                len(texts),
                task_type,
                model_used,
                dims_used,
                elapsed_ms,
            )
            return 200, {
                "ok": True,
                "status": "ok",
                "model": model_used,
                "dimensions": dims_used,
                "embeddings": res["embeddings"],
            }

        err_msg = str(res.get("error") or "embedding_failed")
        logger.warning(
            "embed count=%d task_type=%s status=error error=%s elapsed_ms=%d",
            len(texts),
            task_type,
            err_msg,
            elapsed_ms,
        )
        return 502, {
            "ok": False,
            "status": str(res.get("status") or "error"),
            "error": err_msg,
        }

    def read_archive_enabled(self) -> bool:
        """Read whether daily conversation-archive schedule is enabled from bridge state file (default True)."""
        path = self.cfg.archive_state_file
        if not path.is_file():
            return True
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                return True
            if raw.lower() in ("false", "0", "off", "disabled"):
                return False
            if raw.lower() in ("true", "1", "on", "enabled"):
                return True
            data = json.loads(raw)
            if isinstance(data, dict) and "enabled" in data:
                return bool(data["enabled"])
        except Exception as exc:
            logger.warning("Failed reading archive state file: %s", type(exc).__name__)
        return True

    def write_archive_enabled(self, enabled: bool) -> dict[str, Any]:
        """Atomically persist conversation-archive schedule state file."""
        path = self.cfg.archive_state_file
        payload = {
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if not _atomic_write_json_locked(path, payload, label="archive_state"):
            raise OSError("写入宿主机归档开关状态文件失败")
        return payload

    def _compute_next_archive_run_at(
        self,
        *,
        enabled: bool,
        now: datetime | None = None,
    ) -> str | None:
        """Compute next daily 21:15 (`Asia/Shanghai`) run timestamp when enabled, else None."""
        if not enabled:
            return None
        try:
            tz = ZoneInfo("Asia/Shanghai")
        except Exception:
            tz = timezone.utc
        cur = (now or datetime.now(tz)).astimezone(tz)
        candidate = cur.replace(hour=21, minute=15, second=0, microsecond=0)
        if candidate <= cur:
            candidate = candidate + timedelta(days=1)
        return candidate.isoformat()

    def _archive_pid_files(self) -> list[Path]:
        """Return PID file path(s) in the same directory as the archive status/state files."""
        explicit = os.environ.get("DALTON_ARCHIVE_PID_FILE", "").strip()
        if explicit:
            return [Path(explicit).expanduser()]
        paths: list[Path] = []
        for parent in (self.cfg.archive_status_file.parent, self.cfg.archive_state_file.parent):
            cand = parent / "conversation-archive.pid"
            if cand not in paths:
                paths.append(cand)
        return paths

    def _archive_lock_file(self) -> Path:
        """Return the shared flock lock file in the same directory as the archive status file."""
        status_path = self.cfg.archive_status_file
        return status_path.with_name(f"{status_path.name}.lock")

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _write_archive_pid_file(self, pid: int | None) -> str:
        """Atomically check for an active PID and write ``pid`` under the shared status-dir ``flock``.

        Returns:
            ``"written"`` when ``pid`` was persisted, ``"already_running"`` when a live process
            holds the PID file, or ``"error"`` when locking or writing the PID file fails.
        """
        import fcntl

        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return "error"
        lock_path = self._archive_lock_file()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    for p in self._archive_pid_files():
                        if not p.is_file():
                            continue
                        try:
                            raw = p.read_text(encoding="utf-8").strip()
                            existing_pid = int(raw)
                            if existing_pid != pid and self._pid_is_alive(existing_pid):
                                return "already_running"
                        except Exception:
                            pass
                    for p in self._archive_pid_files():
                        p.parent.mkdir(parents=True, exist_ok=True)
                        tmp_p = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
                        tmp_p.write_text(f"{pid}\n", encoding="utf-8")
                        os.replace(tmp_p, p)
                    return "written"
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            logger.warning("Failed writing archive pid file: %s", type(exc).__name__)
            return "error"

    def _remove_archive_pid_file(self, *, expected_pid: int | None = None) -> None:
        """Remove PID file(s) under the shared ``flock`` only when owned by ``expected_pid`` (or dead)."""
        import fcntl

        lock_path = self._archive_lock_file()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    for p in self._archive_pid_files():
                        if not p.is_file():
                            continue
                        try:
                            raw = p.read_text(encoding="utf-8").strip()
                            if expected_pid is not None:
                                if raw.isdigit() and int(raw) != int(expected_pid):
                                    continue
                            elif raw.isdigit() and self._pid_is_alive(int(raw)):
                                continue
                            p.unlink(missing_ok=True)
                        except Exception:
                            pass
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

    def _is_archive_proc_running(self) -> bool:
        """Check whether an archive process is alive under the shared status-dir ``flock``."""
        import fcntl

        proc = self._archive_proc
        if proc is not None and getattr(proc, "returncode", None) is None:
            return True
        lock_path = self._archive_lock_file()
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    for p in self._archive_pid_files():
                        if not p.is_file():
                            continue
                        try:
                            raw = p.read_text(encoding="utf-8").strip()
                            pid = int(raw)
                            if self._pid_is_alive(pid):
                                return True
                            p.unlink(missing_ok=True)
                        except Exception:
                            try:
                                p.unlink(missing_ok=True)
                            except Exception:
                                pass
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        return False

    def _read_archive_status_file(self) -> dict[str, Any]:
        path = self.cfg.archive_status_file
        if not path.is_file():
            return dict(self._archive_last_status)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = {**data, **self._archive_last_status}
                if data.get("last_run_at") and (
                    not merged.get("last_run_at")
                    or str(data["last_run_at"]) >= str(merged.get("last_run_at") or "")
                ):
                    merged.update(data)
                return merged
        except Exception as exc:
            logger.warning("Failed reading archive status file: %s", type(exc).__name__)
        return dict(self._archive_last_status)

    def _write_archive_status_file(self, payload: dict[str, Any]) -> None:
        self._archive_last_status = dict(payload)
        path = self.cfg.archive_status_file
        _atomic_write_json_locked(path, payload, label="archive_status")

    @staticmethod
    def _read_complete_tail_lines(
        path: Path,
        *,
        max_lines: int = 25,
        max_scan_bytes: int = 4 * 1024 * 1024,
    ) -> list[str]:
        """Read the last ``max_lines`` complete lines from ``path`` without truncating a line's leading prefix."""
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            file_size = fh.tell()
            if file_size <= 0:
                return []
            block_size = 65536
            pos = file_size
            blocks: list[bytes] = []
            newline_count = 0
            scanned = 0
            while pos > 0 and newline_count <= max_lines and scanned < max_scan_bytes:
                read_len = min(block_size, pos, max_scan_bytes - scanned)
                pos -= read_len
                fh.seek(pos, os.SEEK_SET)
                chunk = fh.read(read_len)
                blocks.append(chunk)
                newline_count += chunk.count(b"\n")
                scanned += read_len
            raw = b"".join(reversed(blocks))
            if pos > 0:
                first_nl = raw.find(b"\n")
                if first_nl == -1:
                    return []
                raw = raw[first_nl + 1 :]
            decoded = raw.decode("utf-8", errors="replace").strip()
            if not decoded:
                return []
            return decoded.splitlines()[-max_lines:]

    def _read_sanitized_archive_log_tail(self, *, max_len: int = 8192) -> str:
        """Read complete tail lines of conversation-archive logs, sanitize secrets/paths first, then truncate to 8 KiB."""
        chunks: list[str] = []
        mem_tail = self._archive_last_status.get("log_tail")
        if isinstance(mem_tail, str) and mem_tail.strip():
            chunks.append(mem_tail.strip())
        for log_path in (self.cfg.archive_log_file, self.cfg.archive_err_log_file):
            if not log_path.is_file():
                continue
            try:
                lines = self._read_complete_tail_lines(log_path, max_lines=25)
                if lines:
                    chunks.append("\n".join(lines))
            except Exception:
                continue
        combined = "\n".join(chunks).strip()
        if not combined:
            return ""
        path_scrubbed = _scrub_local_paths(combined)
        sanitized = sanitize_stderr_text(
            path_scrubbed,
            max_len=max(len(path_scrubbed) + 1024, max_len, 8192),
        )
        cleaned = _scrub_local_paths(sanitized)
        byte_cap = min(max(1, int(max_len)), 8192)
        encoded = cleaned.encode("utf-8")
        if len(encoded) > byte_cap:
            cleaned = encoded[-byte_cap:].decode("utf-8", errors="ignore").strip()
        if len(cleaned) > max_len:
            cleaned = cleaned[-max_len:].strip()
        return _scrub_local_paths(cleaned)

    @staticmethod
    def _format_archive_result_label(
        *,
        running: bool,
        exit_code: int | None,
    ) -> str | None:
        if running:
            return "运行中"
        if exit_code is None:
            return None
        if exit_code == 0:
            return "成功 (exit_code=0)"
        if exit_code == 2:
            return "已在运行，跳过"
        if exit_code == 3:
            return "部分未完成 (exit_code=3)"
        return f"失败 (exit_code={exit_code})"

    async def handle_get_conversation_archive(self) -> tuple[int, dict[str, Any]]:
        """Handle `GET /jobs/conversation-archive`."""
        enabled = self.read_archive_enabled()
        running = self._is_archive_proc_running()
        status_data = self._read_archive_status_file()
        last_run_at = status_data.get("last_run_at")
        raw_exit = status_data.get("exit_code")
        exit_code: int | None = (
            int(raw_exit)
            if isinstance(raw_exit, int) and not isinstance(raw_exit, bool)
            else None
        )
        if not last_run_at and self.cfg.archive_log_file.is_file():
            try:
                mtime = self.cfg.archive_log_file.stat().st_mtime
                last_run_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            except Exception:
                pass
        saved_last_result = status_data.get("last_result")
        if running:
            last_result: str | None = "运行中"
        elif isinstance(saved_last_result, str) and saved_last_result.strip():
            last_result = _scrub_local_paths(
                sanitize_stderr_text(saved_last_result.strip(), max_len=200)
            )
        else:
            last_result = self._format_archive_result_label(
                running=running,
                exit_code=exit_code,
            )
        log_tail = self._read_sanitized_archive_log_tail()
        next_run_at = self._compute_next_archive_run_at(enabled=enabled)
        return 200, {
            "ok": True,
            "key": "conversation-archive",
            "enabled": enabled,
            "status": "on" if enabled else "off",
            "running": running,
            "last_run_at": last_run_at,
            "exit_code": exit_code,
            "last_result": last_result,
            "log_tail": log_tail,
            "next_run_at": next_run_at,
        }

    async def handle_toggle_conversation_archive(
        self, enabled: bool
    ) -> tuple[int, dict[str, Any]]:
        """Handle `POST /jobs/conversation-archive/{enable|disable}`."""
        try:
            state = self.write_archive_enabled(bool(enabled))
        except Exception as exc:
            logger.warning("Failed toggling conversation archive state: %s", type(exc).__name__)
            return 500, {
                "ok": False,
                "key": "conversation-archive",
                "status": "error",
                "error": "写入宿主机归档开关状态文件失败",
            }
        next_run_at = self._compute_next_archive_run_at(enabled=bool(enabled))
        return 200, {
            "ok": True,
            "key": "conversation-archive",
            "enabled": bool(enabled),
            "status": "on" if enabled else "off",
            "updated_at": state["updated_at"],
            "next_run_at": next_run_at,
        }

    def _resolve_archive_python_bin(self) -> str:
        if self.cfg.repo_python.is_file() and os.access(self.cfg.repo_python, os.X_OK):
            return str(self.cfg.repo_python)
        if os.path.exists(self.cfg.python_bin):
            return self.cfg.python_bin
        return sys.executable

    async def _terminate_and_cleanup_archive_proc(
        self,
        proc: Any,
        *,
        log_fh: Any = None,
        remove_pid: bool = True,
        terminate_timeout_s: float = 10.0,
        kill_timeout_s: float = 5.0,
        initial_wait_rc: int | None = None,
        initial_cancelled: asyncio.CancelledError | None = None,
    ) -> tuple[bool, int | None, asyncio.CancelledError | None]:
        """Terminate/kill ``proc`` if still running, wait with timeouts, and only clean up PID/log/ref once exited."""
        cancelled_exc = initial_cancelled
        wait_rc = initial_wait_rc

        rc = getattr(proc, "returncode", None) if proc is not None else None
        if not isinstance(rc, int) or isinstance(rc, bool):
            rc = wait_rc

        if proc is not None and rc is None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                w = await asyncio.wait_for(proc.wait(), timeout=terminate_timeout_s)
                if isinstance(w, int) and not isinstance(w, bool):
                    wait_rc = w
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError) and cancelled_exc is None:
                    cancelled_exc = exc
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    w = await asyncio.wait_for(proc.wait(), timeout=kill_timeout_s)
                    if isinstance(w, int) and not isinstance(w, bool):
                        wait_rc = w
                except BaseException as exc2:
                    if isinstance(exc2, asyncio.CancelledError) and cancelled_exc is None:
                        cancelled_exc = exc2

        rc = getattr(proc, "returncode", None) if proc is not None else None
        if not isinstance(rc, int) or isinstance(rc, bool):
            rc = wait_rc
        proc_exited = proc is None or (isinstance(rc, int) and not isinstance(rc, bool))

        if proc_exited:
            if log_fh is not None:
                try:
                    log_fh.flush()
                except Exception:
                    pass
                try:
                    log_fh.close()
                except Exception:
                    pass
            if proc is not None:
                if self._archive_proc is proc:
                    self._archive_proc = None
                if remove_pid:
                    self._remove_archive_pid_file(expected_pid=getattr(proc, "pid", None))

        return (
            proc_exited,
            (int(rc) if isinstance(rc, int) and not isinstance(rc, bool) else None),
            cancelled_exc,
        )

    async def _watch_archive_proc(
        self,
        proc: Any,
        started_at: str,
        log_fh: Any = None,
    ) -> None:
        """Wait for an asynchronous `conversation_archive.py run` subprocess and record its outcome."""
        timed_out = False
        cancelled_exc: asyncio.CancelledError | None = None
        watcher_error: str | None = None
        wait_rc: int | None = None
        try:
            w = await asyncio.wait_for(proc.wait(), timeout=self.cfg.archive_run_timeout_s)
            if isinstance(w, int) and not isinstance(w, bool):
                wait_rc = w
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                "Archive subprocess (pid=%s) timed out after %.1fs",
                getattr(proc, "pid", None),
                self.cfg.archive_run_timeout_s,
            )
        except asyncio.CancelledError as exc:
            cancelled_exc = exc
            watcher_error = "CancelledError"
            logger.warning("Archive watcher cancelled (pid=%s)", getattr(proc, "pid", None))
        except BaseException as exc:
            watcher_error = type(exc).__name__
            logger.warning("Archive watcher error: %s", watcher_error)
        finally:
            proc_exited, rc_val, cancelled_exc = await self._terminate_and_cleanup_archive_proc(
                proc,
                log_fh=log_fh,
                remove_pid=True,
                terminate_timeout_s=10.0,
                kill_timeout_s=5.0,
                initial_wait_rc=wait_rc,
                initial_cancelled=cancelled_exc,
            )
            tail = self._read_sanitized_archive_log_tail(max_len=8192)
            exit_code_val = int(rc_val) if proc_exited and rc_val is not None else 1
            if timed_out:
                last_result = "运行超时"
            elif exit_code_val == 2:
                last_result = "已在运行，跳过"
            elif watcher_error and exit_code_val != 0:
                last_result = f"失败 ({watcher_error})"
            else:
                last_result = self._format_archive_result_label(
                    running=False,
                    exit_code=exit_code_val,
                )
            self._write_archive_status_file(
                {
                    "last_run_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "exit_code": exit_code_val,
                    "last_result": last_result,
                    "log_tail": tail,
                }
            )
        if cancelled_exc is not None:
            raise cancelled_exc

    async def handle_run_conversation_archive(self) -> tuple[int, dict[str, Any]]:
        """Handle `POST /jobs/conversation-archive/run`.

        Prevents duplicate launches via PID file + advisory lock (exit code 2 -> `already_running`).
        """
        async with self._archive_run_lock:
            if self._is_archive_proc_running():
                return 200, {
                    "ok": False,
                    "key": "conversation-archive",
                    "status": "already_running",
                    "already_running": True,
                    "error": "已有归档在运行",
                }

            py_bin = self._resolve_archive_python_bin()
            script_path = self.cfg.conversation_archive_py
            repo_root = script_path.parents[2] if len(script_path.parents) >= 3 else _SCRIPT_DIR.parent
            env_vars = {
                **os.environ,
                **_read_env_file_dict(self.cfg.env_path),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            repo_env = repo_root / ".env"
            if repo_env.is_file():
                for k, v in _read_env_file_dict(repo_env).items():
                    env_vars.setdefault(k, v)

            started_at = datetime.now(timezone.utc).isoformat()
            log_fh: Any = None
            try:
                self.cfg.archive_log_file.parent.mkdir(parents=True, exist_ok=True)
                log_fh = self.cfg.archive_log_file.open("ab")
            except Exception as exc:
                logger.warning("Failed opening archive log file: %s", type(exc).__name__)
                log_fh = None

            proc: Any = None
            pid_written = False
            watcher_registered = False
            startup_cancelled: asyncio.CancelledError | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    py_bin,
                    str(script_path),
                    "run",
                    cwd=str(repo_root),
                    env=env_vars,
                    stdout=log_fh if log_fh is not None else asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.STDOUT if log_fh is not None else asyncio.subprocess.DEVNULL,
                )
                self._archive_proc = proc
                proc_pid = getattr(proc, "pid", None)
                pid_status = self._write_archive_pid_file(proc_pid)
                if pid_status == "already_running":
                    return 200, {
                        "ok": False,
                        "key": "conversation-archive",
                        "status": "already_running",
                        "already_running": True,
                        "error": "已有归档在运行",
                    }
                if pid_status != "written":
                    return 500, {
                        "ok": False,
                        "key": "conversation-archive",
                        "status": "error",
                        "error": "写入团队对话归档 PID 文件失败",
                    }
                pid_written = True

                # Wait briefly to catch immediate exit code 2 when another process holds the Postgres advisory lock
                probe_s = self.cfg.archive_startup_probe_s
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=probe_s)
                except asyncio.TimeoutError:
                    pass

                rc = getattr(proc, "returncode", None)
                if rc == 2:
                    self._write_archive_status_file(
                        {
                            "last_run_at": started_at,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                            "exit_code": 2,
                            "last_result": "已在运行，跳过",
                            "log_tail": self._read_sanitized_archive_log_tail(max_len=8192),
                        }
                    )
                    return 200, {
                        "ok": False,
                        "key": "conversation-archive",
                        "status": "already_running",
                        "already_running": True,
                        "exit_code": 2,
                        "last_result": "已在运行，跳过",
                        "error": "已有归档在运行",
                    }

                self._archive_watch_task = asyncio.create_task(
                    self._watch_archive_proc(proc, started_at, log_fh)
                )
                watcher_registered = True
                return 200, {
                    "ok": True,
                    "key": "conversation-archive",
                    "status": "started",
                    "async": True,
                    "pid": proc_pid,
                    "started_at": started_at,
                }
            except asyncio.CancelledError as exc:
                startup_cancelled = exc
                raise
            finally:
                if not watcher_registered:
                    _, _, cleanup_cancelled = await self._terminate_and_cleanup_archive_proc(
                        proc,
                        log_fh=log_fh,
                        remove_pid=pid_written,
                        terminate_timeout_s=2.0,
                        kill_timeout_s=2.0,
                        initial_cancelled=startup_cancelled,
                    )
                    if cleanup_cancelled is not None and startup_cancelled is None:
                        raise cleanup_cancelled

    async def handle_get_ops_jobs(self) -> tuple[int, dict[str, Any]]:
        """Handle `GET /jobs/ops` for read-only host operational jobs."""
        default_ops = [
            {
                "key": "ops-db-backup",
                "kind": "ops_db_backup",
                "title": "数据库备份",
                "status": "unknown",
                "last_run_at": None,
                "last_result": None,
                "next_run_at": None,
            },
            {
                "key": "ops-log-rotate",
                "kind": "ops_log_rotate",
                "title": "日志轮转",
                "status": "unknown",
                "last_run_at": None,
                "last_result": None,
                "next_run_at": None,
            },
            {
                "key": "ops-backend-watchdog",
                "kind": "ops_backend_watchdog",
                "title": "后端看门狗",
                "status": "unknown",
                "last_run_at": None,
                "last_result": None,
                "next_run_at": None,
            },
        ]
        if not self.cfg.ops_status_file.is_file():
            return 200, {"ok": True, "jobs": default_ops}
        try:
            raw = json.loads(self.cfg.ops_status_file.read_text(encoding="utf-8"))
            by_key = raw.get("jobs") if isinstance(raw, dict) else raw
            mapped: dict[str, dict[str, Any]] = {}
            if isinstance(by_key, dict):
                for k, v in by_key.items():
                    if isinstance(v, dict):
                        mapped[str(k)] = v
            elif isinstance(by_key, list):
                for item in by_key:
                    if isinstance(item, dict) and item.get("key"):
                        mapped[str(item["key"])] = item
            out: list[dict[str, Any]] = []
            for item in default_ops:
                override = mapped.get(item["key"]) or {}
                raw_st = str(override.get("status") or "unknown").strip().lower()
                st = raw_st if raw_st in ("on", "off", "partial", "unknown") else "unknown"
                last_res = override.get("last_result")
                out.append(
                    {
                        **item,
                        "status": st,
                        "last_run_at": override.get("last_run_at"),
                        "last_result": (
                            _scrub_local_paths(sanitize_stderr_text(str(last_res), max_len=200))
                            if last_res is not None
                            else None
                        ),
                        "next_run_at": override.get("next_run_at"),
                    }
                )
            return 200, {"ok": True, "jobs": out}
        except Exception:
            return 200, {"ok": True, "jobs": default_ops}

    def _invoke_openclaw_gateway(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload`` to OpenClaw ``/tools/invoke`` bypassing proxies and never logging tokens."""
        return _post_openclaw_tools_invoke(
            gateway_url=self.cfg.openclaw_gateway_url,
            gateway_token=self.cfg.openclaw_gateway_token,
            payload=payload,
            timeout_s=self.cfg.feishu_send_card_timeout_s,
        )

    async def handle_notify(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /notify`` by sending Feishu Card 2.0 via ``feishu_send_card.py`` or Discord DM via OpenClaw."""
        raw_targets = body.get("targets")
        raw_text = body.get("text")
        raw_card = body.get("card")
        card_dict = raw_card if isinstance(raw_card, dict) else None
        if not isinstance(raw_targets, list) or not raw_targets:
            return 400, {"ok": False, "error": "targets must be a non-empty list"}
        if (not isinstance(raw_text, str) or not raw_text.strip()) and card_dict is None:
            return 400, {"ok": False, "error": "text must be a non-empty string"}

        text = str(raw_text or "").strip()
        token = self.cfg.openclaw_gateway_token
        results: list[dict[str, Any]] = []

        for raw_target in raw_targets:
            target_str = str(raw_target).strip() if isinstance(raw_target, str) else ""
            if not target_str:
                results.append(
                    {"target": str(raw_target), "ok": False, "error": "empty_target"}
                )
                continue
            try:
                channel, target_arg = parse_notify_target(target_str)
            except ValueError:
                results.append(
                    {"target": target_str, "ok": False, "error": "invalid_target_format"}
                )
                continue

            if channel == "feishu" and card_dict is not None:
                open_id = (
                    target_arg.split(":", 1)[1]
                    if ":" in target_arg
                    else target_arg
                )
                try:
                    card_res = await asyncio.to_thread(
                        _run_feishu_send_card,
                        script_path=self.cfg.feishu_send_card_script,
                        open_id=open_id,
                        card=card_dict,
                        timeout_s=self.cfg.feishu_send_card_timeout_s,
                    )
                    ok = card_res.get("ok") is True
                    exit_code = card_res.get("exit_code")
                    if ok:
                        logger.info(
                            "notify target=%s channel=feishu mode=card status=ok exit_code=%s",
                            target_str,
                            exit_code,
                        )
                        results.append(
                            {"target": target_str, "channel": "feishu", "ok": True}
                        )
                    else:
                        err_code = str(
                            card_res.get("error") or f"exit_code={exit_code}"
                        )
                        logger.warning(
                            "notify target=%s channel=feishu mode=card status=error exit_code=%s",
                            target_str,
                            exit_code,
                        )
                        results.append(
                            {
                                "target": target_str,
                                "channel": "feishu",
                                "ok": False,
                                "error": err_code,
                            }
                        )
                except Exception as card_exc:
                    logger.warning(
                        "notify target=%s channel=feishu mode=card status=error error_type=%s",
                        target_str,
                        type(card_exc).__name__,
                    )
                    results.append(
                        {
                            "target": target_str,
                            "channel": "feishu",
                            "ok": False,
                            "error": type(card_exc).__name__,
                        }
                    )
                continue

            if not token:
                logger.warning(
                    "notify target=%s channel=%s status=error error_code=missing_openclaw_gateway_token",
                    target_str,
                    channel,
                )
                results.append(
                    {
                        "target": target_str,
                        "channel": channel,
                        "ok": False,
                        "error": "missing_openclaw_gateway_token",
                    }
                )
                continue

            invoke_payload = build_openclaw_message_invoke_body(
                channel=channel,
                target=target_arg,
                text=text,
            )
            try:
                resp_data = await asyncio.to_thread(
                    self._invoke_openclaw_gateway, invoke_payload
                )
                is_err = bool(
                    resp_data.get("ok") is False
                    or resp_data.get("isError")
                    or (
                        isinstance(resp_data.get("result"), Mapping)
                        and resp_data["result"].get("isError")
                    )
                )
                if is_err:
                    status_code = (
                        resp_data.get("status_code")
                        if isinstance(resp_data.get("status_code"), int)
                        else None
                    )
                    err_code = (
                        f"http_{status_code}"
                        if status_code is not None
                        else "openclaw_invoke_error"
                    )
                    logger.warning(
                        "notify target=%s channel=%s status=error error_code=%s status_code=%s",
                        target_str,
                        channel,
                        err_code,
                        status_code,
                    )
                    results.append(
                        {
                            "target": target_str,
                            "channel": channel,
                            "ok": False,
                            "error": err_code,
                        }
                    )
                else:
                    logger.info(
                        "notify target=%s channel=%s status=ok",
                        target_str,
                        channel,
                    )
                    results.append(
                        {"target": target_str, "channel": channel, "ok": True}
                    )
            except Exception as exc:
                status_code = getattr(exc, "code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if not isinstance(status_code, int):
                    status_code = None
                err_code = (
                    f"http_{status_code}"
                    if status_code is not None
                    else type(exc).__name__
                )
                logger.warning(
                    "notify target=%s channel=%s status=error error_type=%s status_code=%s",
                    target_str,
                    channel,
                    type(exc).__name__,
                    status_code,
                )
                results.append(
                    {
                        "target": target_str,
                        "channel": channel,
                        "ok": False,
                        "error": err_code,
                    }
                )

        all_ok = bool(results) and all(bool(item.get("ok")) for item in results)
        return 200, {"ok": all_ok, "results": results}

    def _resolve_wiki_run_sh(self) -> Path:
        """Locate the OpenClaw company-wiki `run.sh` entrypoint without modifying it."""
        explicit = getattr(self.cfg, "wiki_run_sh", None)
        if isinstance(explicit, Path):
            return explicit
        return self.cfg.ocw_root / "skills" / "company-wiki" / "run.sh"

    def _load_company_wiki_config(self) -> dict[str, Any]:
        """Read `skills/company-wiki/config.json` in read-only mode."""
        cfg_path = self.cfg.ocw_root / "skills" / "company-wiki" / "config.json"
        if not cfg_path.is_file():
            return {}
        try:
            parsed = json.loads(cfg_path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except Exception as exc:
            logger.warning("Failed reading company-wiki config.json: %s", type(exc).__name__)
            return {}

    def _resolve_wiki_base_dir(self, target_type: str, wiki_cfg: dict[str, Any]) -> Path:
        """Resolve `wiki/companies` or `wiki/sectors` directory under OpenClaw workspace."""
        raw_wiki_dir = str(wiki_cfg.get("wiki_dir") or "wiki").strip() or "wiki"
        wiki_dir = Path(raw_wiki_dir)
        if not wiki_dir.is_absolute():
            wiki_dir = self.cfg.ocw_root / wiki_dir
        subdir = "companies" if target_type == "company" else "sectors"
        return wiki_dir / subdir

    def _resolve_and_validate_wiki_slug(
        self, target_type: str, raw_target: str, wiki_cfg: dict[str, Any]
    ) -> tuple[str | None, bool, dict[str, Any] | None]:
        """Resolve `raw_target` against aliases/existing directories or validate new slug regex.

        Returns `(resolved_slug, is_existing, error_payload)`.
        """
        cleaned = raw_target.strip()
        if not cleaned:
            return None, False, {
                "ok": False,
                "status": "invalid_slug",
                "error_code": "empty_target",
                "error": "target 不能为空",
            }
        if cleaned.lower().startswith("temp"):
            return None, False, {
                "ok": False,
                "status": "invalid_slug",
                "error_code": "reserved_slug",
                "target": cleaned,
                "target_type": target_type,
                "error": f"无效的 {target_type} 名称 '{cleaned}'：temp* 为保留目录名，禁止入库。",
            }

        # 1. Check aliases in config.json (read-only)
        alias_map: dict[str, str] = {}
        raw_aliases = wiki_cfg.get("aliases")
        if isinstance(raw_aliases, dict):
            nested = raw_aliases.get(target_type)
            if isinstance(nested, dict):
                for k, v in nested.items():
                    if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                        alias_map[k.strip().lower()] = v.strip()
            for k, v in raw_aliases.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    alias_map.setdefault(k.strip().lower(), v.strip())

        base_dir = self._resolve_wiki_base_dir(target_type, wiki_cfg)
        existing_dirs: dict[str, str] = {}
        if base_dir.is_dir():
            try:
                existing_dirs = {
                    d.name.lower(): d.name for d in base_dir.iterdir() if d.is_dir()
                }
            except OSError:
                existing_dirs = {}

        lowered = cleaned.lower()
        if lowered in alias_map:
            resolved = alias_map[lowered]
            if resolved.lower().startswith("temp"):
                return None, False, {
                    "ok": False,
                    "status": "invalid_slug",
                    "error_code": "reserved_slug",
                    "target": cleaned,
                    "target_type": target_type,
                    "error": f"别名映射结果 '{resolved}' 命中保留前缀 temp*，禁止入库。",
                }
            is_existing = resolved.lower() in existing_dirs
            if is_existing:
                resolved = existing_dirs[resolved.lower()]
            return resolved, is_existing, None

        # 2. Check existing directory match (exact case-sensitive for new vs existing, or case-insensitive existing dir)
        if lowered in existing_dirs:
            resolved_existing = existing_dirs[lowered]
            pattern = COMPANY_SLUG_RE if target_type == "company" else SECTOR_SLUG_RE
            if cleaned == resolved_existing or pattern.fullmatch(cleaned):
                return resolved_existing, True, None

        # 3. Unmapped A-share / HK / Chinese names or numeric tickers -> do NOT auto-add alias
        if _WIKI_NEEDS_ALIAS_RE.search(cleaned) or not cleaned.isascii():
            return None, False, {
                "ok": False,
                "status": "needs_alias",
                "error_code": "needs_alias",
                "needs_alias": True,
                "target": cleaned,
                "target_type": target_type,
                "error": (
                    f"'{cleaned}' 未在 config.json 配置别名 (needs_alias)；"
                    "A股/港股/中文名称不自动添加别名，请找管理员在 config.json aliases 中配置后再入库。"
                ),
                "hint": "请联系管理员在 OpenClaw company-wiki config.json 的 aliases 中添加映射。",
            }

        if cleaned.isdigit():
            return None, False, {
                "ok": False,
                "status": "needs_alias",
                "error_code": "numeric_ticker_rejected",
                "needs_alias": True,
                "target": cleaned,
                "target_type": target_type,
                "error": (
                    f"纯数字 ticker '{cleaned}' 拒绝直接建目录 (needs_alias)；"
                    "A股/港股数字代码请找管理员在 config.json aliases 中添加映射后再入库。"
                ),
                "hint": "纯数字 ticker 禁止直接作为目录名，请联系管理员添加别名映射。",
            }

        # 4. Validate new slug regex
        pattern = COMPANY_SLUG_RE if target_type == "company" else SECTOR_SLUG_RE
        if not pattern.fullmatch(cleaned):
            expected_re = "^[A-Z][A-Z0-9-]{0,29}$" if target_type == "company" else "^[a-z][a-z0-9-]{0,39}$"
            return None, False, {
                "ok": False,
                "status": "invalid_slug",
                "error_code": "invalid_slug",
                "target": cleaned,
                "target_type": target_type,
                "error": f"无效的 {target_type} slug '{cleaned}'（须已存在或匹配 {expected_re}）",
            }

        return cleaned, False, None

    def _read_wiki_audit_entries(self) -> list[dict[str, Any]]:
        """Read existing JSONL entries from `self.cfg.wiki_ingest_audit_file`."""
        audit_path = self.cfg.wiki_ingest_audit_file
        if not audit_path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        try:
            for line in audit_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        entries.append(item)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Failed reading wiki ingest audit log: %s", type(exc).__name__)
        return entries

    def _append_wiki_audit_entry(self, record: dict[str, Any]) -> None:
        """Append a single JSONL audit record to `self.cfg.wiki_ingest_audit_file` under `flock`."""
        import fcntl

        audit_path = self.cfg.wiki_ingest_audit_file
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with audit_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(line)
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _wiki_idempotency_file(self) -> Path:
        """Return the path to the independent idempotency store file."""
        custom = getattr(self.cfg, "wiki_ingest_idempotency_file", None)
        if custom is not None:
            return Path(custom)
        return self.cfg.wiki_ingest_audit_file.with_name("wiki_ingest_idempotency.json")

    def _parse_wiki_idempotency_file_strict(self) -> dict[str, dict[str, Any]]:
        """Read and validate the idempotency store JSON; raises on I/O or format corruption."""
        idemp_path = self._wiki_idempotency_file()
        if not idemp_path.exists():
            return {}
        raw = json.loads(idemp_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or any(not isinstance(v, dict) for v in raw.values()):
            raise ValueError("Wiki idempotency store root must be a JSON object of records")
        return {str(k): v for k, v in raw.items()}

    def _read_wiki_idempotent_results(self) -> dict[str, dict[str, Any]]:
        """Read persisted `request_id -> record` entries from the independent idempotency store."""
        idemp_path = self._wiki_idempotency_file()
        if not idemp_path.is_file():
            return {}
        try:
            return self._parse_wiki_idempotency_file_strict()
        except Exception as exc:
            logger.error(
                "Failed reading/parsing wiki idempotency store %s: %s",
                idemp_path,
                type(exc).__name__,
            )
            return {}

    def _save_wiki_idempotent_result(self, key: str, record: dict[str, Any]) -> None:
        """Atomically persist `key -> record` to the independent idempotency store (temp file + fsync + rename + dir fsync).

        Refuses to overwrite the original file if an existing idempotency file is corrupted.
        """
        import fcntl

        idemp_path = self._wiki_idempotency_file()
        idemp_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = idemp_path.with_suffix(".lock")
        with lock_path.open("a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                if idemp_path.exists():
                    try:
                        current = self._parse_wiki_idempotency_file_strict()
                    except Exception as exc:
                        logger.error(
                            "Refusing to overwrite corrupted wiki idempotency store %s: %s",
                            idemp_path,
                            type(exc).__name__,
                        )
                        return
                else:
                    current = {}

                current[str(key)] = record
                tmp_fd, tmp_name = tempfile.mkstemp(
                    prefix=".wiki_idemp_",
                    suffix=".tmp",
                    dir=str(idemp_path.parent),
                )
                os.close(tmp_fd)
                tmp_path = Path(tmp_name)
                try:
                    os.chmod(tmp_path, 0o600)
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump(current, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(str(tmp_path), str(idemp_path))
                    dir_fd = os.open(str(idemp_path.parent), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except BaseException:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    def _find_wiki_audit_by_request_id(self, request_id: str | None) -> dict[str, Any] | None:
        """Return a prior successful audit record for `request_id` if one exists."""
        if not request_id:
            return None
        for entry in reversed(self._read_wiki_audit_entries()):
            if str(entry.get("request_id") or "").strip() != request_id:
                continue
            st = str(entry.get("status") or entry.get("result") or "").strip().lower()
            if st in ("ingested", "done", "ok", "added", "added_from_file"):
                return entry
        return None

    def _find_wiki_idempotent_result(self, request_id: str | None) -> dict[str, Any] | None:
        """Return a prior successful result for `request_id`, checking the idempotency store first."""
        if not request_id:
            return None
        idemp_map = self._read_wiki_idempotent_results()
        rec = idemp_map.get(request_id)
        if isinstance(rec, dict):
            st = str(rec.get("status") or rec.get("result") or "").strip().lower()
            if st in ("ingested", "done", "ok", "added", "added_from_file"):
                return rec
        return self._find_wiki_audit_by_request_id(request_id)

    def _try_acquire_wiki_claim_locks(
        self,
        *,
        content_sha256: str,
        slot_key: str,
        request_id: str | None = None,
    ) -> tuple[list[int] | None, str | None]:
        """Acquire non-blocking `fcntl.flock` claim locks in fixed order: `request_id` -> `sha256` -> `slot`.

        Lock files are stable and never unlinked; kernel automatically releases locks on process exit.
        """
        import fcntl

        claims_dir = self.cfg.wiki_ingest_temp_dir / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[tuple[Path, str]] = []
        if request_id:
            req_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]
            candidates.append((claims_dir / f"req_{req_hash}.lock", "same_request_inflight"))
        candidates.append((claims_dir / f"sha256_{content_sha256}.lock", "concurrent_inflight"))
        candidates.append((claims_dir / f"slot_{slot_key}.lock", "concurrent_inflight"))

        debug_payload = (
            json.dumps(
                {
                    "pid": os.getpid(),
                    "created_at": time.time(),
                    "request_id": request_id or "",
                }
            )
            + "\n"
        ).encode("utf-8")

        acquired_fds: list[int] = []
        try:
            for lock_path, fail_reason in candidates:
                fd = os.open(
                    str(lock_path),
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    self._release_wiki_claim_locks(acquired_fds)
                    acquired_fds.clear()
                    return None, fail_reason
                except BaseException:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                acquired_fds.append(fd)
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, debug_payload)
            return acquired_fds, None
        except BaseException:
            self._release_wiki_claim_locks(acquired_fds)
            acquired_fds.clear()
            raise

    @staticmethod
    def _release_wiki_claim_locks(claim_fds: list[int]) -> None:
        """Release held `fcntl.flock` file descriptors in reverse order (`LOCK_UN` then `os.close`)."""
        import fcntl

        for fd in reversed(claim_fds):
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _check_target_disk_file_sync(
        self,
        *,
        target_file: Path,
        content_sha256: str,
        norm_text: str,
    ) -> dict[str, Any] | None:
        """Check only the single target markdown file on disk for sha256 or text similarity match."""
        if not target_file.is_file():
            return None
        rel_disk_path = (
            str(target_file.relative_to(self.cfg.ocw_root))
            if target_file.is_relative_to(self.cfg.ocw_root)
            else str(target_file)
        )
        try:
            raw_bytes = target_file.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(raw_bytes).hexdigest() == content_sha256:
            return {
                "reason": "sha256_match",
                "doc_id": None,
                "filepath": rel_disk_path,
                "similarity": 1.0,
            }
        existing_raw = raw_bytes.decode("utf-8", errors="replace")
        existing_body = _strip_wiki_markdown_frontmatter(existing_raw)
        existing_norm = _normalize_wiki_text_for_hash(existing_body)
        if existing_norm:
            existing_sha = hashlib.sha256(existing_norm.encode("utf-8")).hexdigest()
            if existing_sha == content_sha256:
                return {
                    "reason": "sha256_match",
                    "doc_id": None,
                    "filepath": rel_disk_path,
                    "similarity": 1.0,
                }
            if norm_text:
                sim = _compute_wiki_text_similarity(norm_text, existing_norm)
                if sim >= WIKI_INGEST_SIMILARITY_THRESHOLD:
                    return {
                        "reason": "similar_content",
                        "doc_id": None,
                        "filepath": rel_disk_path,
                        "similarity": round(sim, 4),
                    }
        return None

    async def _check_wiki_duplicate(
        self,
        *,
        target_type: str,
        resolved_slug: str,
        canonical_content_type: str,
        date_str: str,
        content_sha256: str,
        norm_text: str,
        wiki_cfg: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check whether `content_sha256` or a highly similar same-(slug, date, type) doc already exists."""
        idemp_entries = list(self._read_wiki_idempotent_results().values())
        audit_entries = self._read_wiki_audit_entries()
        combined_entries = [*idemp_entries, *audit_entries]

        # 1. Exact SHA256 match against idempotency store and audit log (text AND binary PDF/images)
        for entry in combined_entries:
            st = str(entry.get("status") or entry.get("result") or "").strip().lower()
            if st not in ("ingested", "done", "ok", "added", "added_from_file"):
                continue
            if str(entry.get("sha256") or "").strip() == content_sha256:
                return {
                    "reason": "sha256_match",
                    "doc_id": entry.get("doc_id"),
                    "filepath": entry.get("filepath"),
                    "similarity": 1.0,
                }

        # 2. Text similarity against idempotency store and audit entries with same slug + date + content_type
        if norm_text:
            for entry in combined_entries:
                st = str(entry.get("status") or entry.get("result") or "").strip().lower()
                if st not in ("ingested", "done", "ok", "added", "added_from_file"):
                    continue
                if (
                    str(entry.get("target") or "").strip().lower() == resolved_slug.lower()
                    and str(entry.get("date") or "").strip() == date_str
                    and str(entry.get("content_type") or "").strip() == canonical_content_type
                ):
                    prev_excerpt = str(entry.get("_norm_excerpt") or "").strip()
                    if prev_excerpt:
                        sim = _compute_wiki_text_similarity(norm_text, prev_excerpt)
                        if sim >= WIKI_INGEST_SIMILARITY_THRESHOLD:
                            return {
                                "reason": "similar_content",
                                "doc_id": entry.get("doc_id"),
                                "filepath": entry.get("filepath"),
                                "similarity": round(sim, 4),
                            }
                    rel_fp = str(entry.get("filepath") or "").strip()
                    if rel_fp:
                        fp_candidate = Path(rel_fp)
                        if not fp_candidate.is_absolute():
                            fp_candidate = self.cfg.ocw_root / fp_candidate
                        if fp_candidate.is_file():
                            try:
                                raw_text = await asyncio.to_thread(
                                    fp_candidate.read_text,
                                    encoding="utf-8",
                                    errors="replace",
                                )
                                existing_body = _strip_wiki_markdown_frontmatter(raw_text)
                                sim = _compute_wiki_text_similarity(norm_text, existing_body)
                                if sim >= WIKI_INGEST_SIMILARITY_THRESHOLD:
                                    return {
                                        "reason": "similar_content",
                                        "doc_id": entry.get("doc_id"),
                                        "filepath": rel_fp,
                                        "similarity": round(sim, 4),
                                    }
                            except OSError:
                                pass

        # 3. Check only the single target file on disk via `asyncio.to_thread` (never scan the directory)
        base_dir = self._resolve_wiki_base_dir(target_type, wiki_cfg)
        type_slug = canonical_content_type.replace(" ", "-")
        target_file = base_dir / resolved_slug / f"{date_str}-{type_slug}.md"
        return await asyncio.to_thread(
            self._check_target_disk_file_sync,
            target_file=target_file,
            content_sha256=content_sha256,
            norm_text=norm_text,
        )

    def _build_wiki_subprocess_env(self) -> dict[str, str]:
        """Construct minimal environment for `company-wiki/run.sh` without leaking bridge tokens."""
        env_file_vars = _read_env_file_dict(self.cfg.env_path)
        gemini_api_key = (
            env_file_vars.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
        )
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", os.path.expanduser("~")),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "SKILL_WARNINGS": "ignore",
        }
        if gemini_api_key:
            env["GEMINI_API_KEY"] = gemini_api_key
        if self.cfg.subprocess_proxy:
            env["HTTP_PROXY"] = self.cfg.subprocess_proxy
            env["HTTPS_PROXY"] = self.cfg.subprocess_proxy
        return env

    async def _run_company_wiki_ingest_cli(
        self,
        cmd: list[str],
        *,
        env: dict[str, str],
        timeout_s: float,
    ) -> dict[str, Any]:
        """Run `company-wiki/run.sh --json add|add-file` as an argv list without a shell."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cfg.ocw_root),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await _reap_process(proc)
            raise
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await _reap_process(proc)
            return {
                "ok": False,
                "status": "timeout",
                "error": f"company-wiki CLI timed out after {timeout_s}s",
            }
        except Exception as exc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await _reap_process(proc)
            return {
                "ok": False,
                "status": "error",
                "error": type(exc).__name__,
            }

        stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
        stderr_tail = sanitize_stderr_text(stderr_bytes, max_len=300)
        parsed: dict[str, Any] | None = None
        if stdout_text:
            try:
                cand = json.loads(stdout_text)
                if isinstance(cand, dict):
                    parsed = cand
            except Exception:
                for line in reversed(stdout_text.splitlines()):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cand = json.loads(line)
                        if isinstance(cand, dict):
                            parsed = cand
                            break
                    except Exception:
                        continue

        if proc.returncode != 0 or not isinstance(parsed, dict) or parsed.get("ok") is False:
            err_msg = (
                (parsed.get("error") if isinstance(parsed, dict) else None)
                or stderr_tail
                or f"company-wiki CLI exited with code {proc.returncode}"
            )
            return {
                "ok": False,
                "status": "cli_error",
                "exit_code": proc.returncode,
                "error": sanitize_stderr_text(str(err_msg), max_len=300),
                "hint": parsed.get("hint") if isinstance(parsed, dict) else None,
            }
        return parsed

    async def handle_wiki_ingest(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle `POST /wiki/ingest` (admin token only) to validate, preview, or ingest into company-wiki."""
        target_type = str(body.get("target_type") or "").strip().lower()
        if target_type not in ("company", "sector"):
            return 400, {
                "ok": False,
                "status": "invalid_target_type",
                "error_code": "invalid_target_type",
                "error": "target_type 必须为 'company' 或 'sector'",
            }

        raw_target = body.get("target")
        if not isinstance(raw_target, str) or not raw_target.strip():
            return 400, {
                "ok": False,
                "status": "invalid_slug",
                "error_code": "empty_target",
                "error": "target 必须为非空字符串",
            }

        raw_content_type = body.get("content_type")
        if not isinstance(raw_content_type, str) or not raw_content_type.strip():
            return 400, {
                "ok": False,
                "status": "invalid_content_type",
                "error_code": "empty_content_type",
                "error": "content_type 必须为非空字符串",
            }

        # Check sell-side content_type first and prompt user to use sales notes
        if _SELL_SIDE_TYPE_RE.search(raw_content_type.strip()):
            return 400, {
                "ok": False,
                "status": "sell_side_rejected",
                "error_code": "sell_side_rejected",
                "suspected_sell_side": True,
                "error": f"卖方内容类型 '{raw_content_type.strip()}' 不进 company-wiki，请走 sales notes。",
                "hint": "卖方观点（券商研报、sales color、分析师电话会）请走 sales notes，不进 wiki。",
            }

        canonical_content_type = _ALLOWED_CONTENT_TYPE_MAP.get(
            _normalize_content_type_key(raw_content_type)
        )
        if not canonical_content_type:
            return 400, {
                "ok": False,
                "status": "invalid_content_type",
                "error_code": "invalid_content_type",
                "allowed_content_types": list(WIKI_INGEST_ALLOWED_CONTENT_TYPES),
                "error": (
                    f"content_type '{raw_content_type.strip()}' 不在允许列表中。"
                    f"允许的类型: {', '.join(WIKI_INGEST_ALLOWED_CONTENT_TYPES)}"
                ),
            }

        date_str = str(body.get("date") or "").strip()
        if not _YMD_RE.fullmatch(date_str):
            return 400, {
                "ok": False,
                "status": "invalid_date",
                "error_code": "invalid_date",
                "error": "date 必须为 YYYY-MM-DD 格式",
            }
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return 400, {
                "ok": False,
                "status": "invalid_date",
                "error_code": "invalid_date",
                "error": f"date '{date_str}' 不是有效日历日期",
            }

        raw_source = body.get("source")
        source_clean: str | None = None
        if raw_source is not None and str(raw_source).strip():
            try:
                source_clean = _validate_clean_text(
                    str(raw_source), field_name="source", max_len=500
                )
            except ValueError as exc:
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_source",
                    "error": str(exc),
                }
            if _SELL_SIDE_TYPE_RE.search(source_clean):
                return 400, {
                    "ok": False,
                    "status": "sell_side_rejected",
                    "error_code": "sell_side_rejected",
                    "suspected_sell_side": True,
                    "error": f"来源 '{source_clean}' 属于卖方渠道，请走 sales notes。",
                    "hint": "卖方观点（券商研报、sales color、分析师电话会）请走 sales notes，不进 wiki。",
                }

        raw_source_url = body.get("source_url")
        source_url_clean: str | None = None
        if raw_source_url is not None and str(raw_source_url).strip():
            url_cand = str(raw_source_url).strip()
            if (
                len(url_cand) > 1000
                or any(ord(ch) < 32 or ch.isspace() for ch in url_cand)
                or not url_cand.startswith(("http://", "https://"))
            ):
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_source_url",
                    "error": "source_url 必须为有效的 http/https URL（<=1000 字符）",
                }
            source_url_clean = url_cand

        # Validate ingested_by & ingested_via
        raw_by = body.get("ingested_by")
        if isinstance(raw_by, dict):
            by_user_id = str(raw_by.get("user_id") or "").strip()
            by_email = str(raw_by.get("email") or "").strip()
        elif isinstance(raw_by, str) and raw_by.strip():
            by_user_id = raw_by.strip()
            by_email = ""
        else:
            by_user_id = ""
            by_email = ""
        if not by_user_id:
            return 400, {
                "ok": False,
                "status": "bad_request",
                "error_code": "missing_ingested_by",
                "error": "ingested_by.user_id 必填",
            }
        ingested_by_norm = {"user_id": by_user_id, "email": by_email}

        raw_via = body.get("ingested_via")
        if isinstance(raw_via, dict):
            ingested_via_norm = {
                "workspace_id": str(raw_via.get("workspace_id") or "").strip(),
                "thread_id": str(raw_via.get("thread_id") or "").strip(),
            }
        else:
            ingested_via_norm = {"workspace_id": "", "thread_id": ""}

        raw_req_id = body.get("request_id")
        request_id: str | None = (
            str(raw_req_id).strip() if isinstance(raw_req_id, str) and raw_req_id.strip() else None
        )

        dry_run = bool(body.get("dry_run", False))

        # Validate mutually exclusive content vs file
        has_content = body.get("content") is not None
        has_file = body.get("file") is not None
        if has_content == has_file:
            return 400, {
                "ok": False,
                "status": "bad_request",
                "error_code": "content_or_file_required",
                "error": "content 与 file 必须二选一传入",
            }

        wiki_cfg = self._load_company_wiki_config()
        resolved_slug, is_existing_slug, slug_err = self._resolve_and_validate_wiki_slug(
            target_type, raw_target, wiki_cfg
        )
        if slug_err is not None or resolved_slug is None:
            return 400, slug_err or {"ok": False, "status": "invalid_slug", "error": "Invalid target"}

        content_text: str | None = None
        file_bytes: bytes | None = None
        upload_filename: str | None = None
        size_bytes: int = 0
        char_count: int = 0
        text_for_inspection: str = ""
        full_byte_scan_text: str = ""
        text_extracted: bool = True
        scan_note: str | None = None

        if has_content:
            raw_content = body.get("content")
            if not isinstance(raw_content, str) or not raw_content.strip():
                return 400, {
                    "ok": False,
                    "status": "empty_content",
                    "error_code": "empty_content",
                    "error": "content 不能为空",
                }
            if len(raw_content) > WIKI_INGEST_MAX_TEXT_CHARS:
                return 400, {
                    "ok": False,
                    "status": "payload_too_large",
                    "error_code": "text_too_large",
                    "max_chars": WIKI_INGEST_MAX_TEXT_CHARS,
                    "char_count": len(raw_content),
                    "error": f"文本长度 ({len(raw_content)}) 超过 {WIKI_INGEST_MAX_TEXT_CHARS} 字符上限",
                }
            content_text = raw_content
            char_count = len(content_text)
            size_bytes = len(content_text.encode("utf-8"))
            text_for_inspection = content_text
            full_byte_scan_text = content_text
            norm_text = _normalize_wiki_text_for_hash(content_text)
            content_sha256 = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
        else:
            raw_file = body.get("file")
            if not isinstance(raw_file, dict):
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_file_payload",
                    "error": "file 必须为包含 filename 与文件字节的对象",
                }
            raw_fname = str(raw_file.get("filename") or raw_file.get("name") or "").strip()
            if (
                not raw_fname
                or "/" in raw_fname
                or "\\" in raw_fname
                or ".." in raw_fname
                or raw_fname.startswith(".")
            ):
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_filename",
                    "error": "file.filename 非法或为空",
                }
            ext = Path(raw_fname).suffix.lower()
            if ext not in WIKI_INGEST_ALLOWED_EXTENSIONS:
                return 400, {
                    "ok": False,
                    "status": "invalid_extension",
                    "error_code": "invalid_extension",
                    "allowed_extensions": sorted(WIKI_INGEST_ALLOWED_EXTENSIONS),
                    "error": (
                        f"不支持的文件扩展名 '{ext}'；仅允许: "
                        f"{', '.join(sorted(WIKI_INGEST_ALLOWED_EXTENSIONS))}"
                    ),
                }
            upload_filename = Path(raw_fname).name
            decoded_bytes, decode_err = _extract_file_bytes(raw_file)
            if decode_err is not None or decoded_bytes is None:
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_file_bytes",
                    "error": decode_err or "无法解析文件字节",
                }
            if len(decoded_bytes) == 0:
                return 400, {
                    "ok": False,
                    "status": "empty_content",
                    "error_code": "empty_file",
                    "error": "文件内容不能为空",
                }
            if len(decoded_bytes) > WIKI_INGEST_MAX_FILE_BYTES:
                return 400, {
                    "ok": False,
                    "status": "payload_too_large",
                    "error_code": "file_too_large",
                    "max_bytes": WIKI_INGEST_MAX_FILE_BYTES,
                    "size_bytes": len(decoded_bytes),
                    "error": f"文件大小 ({len(decoded_bytes)} bytes) 超过 20 MB 上限",
                }
            file_bytes = decoded_bytes
            size_bytes = len(file_bytes)
            if ext in (".md", ".txt"):
                decoded_text = file_bytes.decode("utf-8", errors="replace")
                if not decoded_text.strip():
                    return 400, {
                        "ok": False,
                        "status": "empty_content",
                        "error_code": "empty_file",
                        "error": "文本文件内容不能为空",
                    }
                if len(decoded_text) > WIKI_INGEST_MAX_TEXT_CHARS:
                    return 400, {
                        "ok": False,
                        "status": "payload_too_large",
                        "error_code": "text_too_large",
                        "max_chars": WIKI_INGEST_MAX_TEXT_CHARS,
                        "char_count": len(decoded_text),
                        "error": f"文本文件字符数 ({len(decoded_text)}) 超过 {WIKI_INGEST_MAX_TEXT_CHARS} 上限",
                    }
                char_count = len(decoded_text)
                text_for_inspection = decoded_text
                full_byte_scan_text = decoded_text
                norm_text = _normalize_wiki_text_for_hash(decoded_text)
                content_sha256 = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
            else:
                (
                    full_byte_scan_text,
                    extracted_text,
                    text_extracted,
                    scan_note,
                ) = _inspect_binary_file_for_wiki(file_bytes, ext)
                text_for_inspection = extracted_text if text_extracted else full_byte_scan_text
                char_count = len(extracted_text) if text_extracted else 0
                norm_text = _normalize_wiki_text_for_hash(extracted_text) if text_extracted and extracted_text else ""
                content_sha256 = hashlib.sha256(file_bytes).hexdigest()

        # Full-byte secret scan across all file bytes / extracted text / metadata (no truncation)
        secret_kind = _detect_wiki_ingest_secret(
            full_byte_scan_text,
            text_for_inspection,
            source_clean or "",
            source_url_clean or "",
        )
        if secret_kind is not None:
            return 400, {
                "ok": False,
                "status": "secret_detected",
                "error_code": "secret_detected",
                "secret_kind": secret_kind,
                "error": "检测到疑似密钥或敏感凭证（API key / token / 私钥 PEM），拒绝入库。",
                "hint": "请先移除正文或文件中的密钥、Token 或私钥内容后再入库。",
            }

        # Check suspected sell-side markers across the full text and filename
        suspected_sell_side = bool(
            _SELL_SIDE_BODY_RE.search(text_for_inspection)
            or (upload_filename and _SELL_SIDE_TYPE_RE.search(upload_filename))
        )
        sell_side_warning = (
            "正文或文件名疑似包含卖方研报/Sales Color/分析师电话会特征；卖方观点请走 sales notes，请确认该材料为公司或专家一手纪要。"
            if suspected_sell_side
            else None
        )

        # Compute expected preview filename & relative filepath
        type_slug = canonical_content_type.replace(" ", "-")
        subdir = "companies" if target_type == "company" else "sectors"
        preview_filename = f"{date_str}-{type_slug}.md"
        preview_filepath = f"wiki/{subdir}/{resolved_slug}/{preview_filename}"

        def _build_replay_response(prior_entry: dict[str, Any]) -> dict[str, Any]:
            prior_doc_id = prior_entry.get("doc_id")
            prior_fp = str(prior_entry.get("filepath") or preview_filepath)
            prior_fn = str(prior_entry.get("filename") or Path(prior_fp).name)
            resp_obj: dict[str, Any] = {
                "ok": True,
                "status": "ingested",
                "dry_run": False,
                "idempotent_replay": True,
                "request_id": request_id,
                "doc_id": prior_doc_id,
                "document_id": f"wiki:{prior_doc_id}" if prior_doc_id is not None else None,
                "target_type": str(prior_entry.get("target_type") or target_type),
                "target": str(prior_entry.get("target") or resolved_slug),
                "content_type": str(prior_entry.get("content_type") or canonical_content_type),
                "date": str(prior_entry.get("date") or date_str),
                "filename": prior_fn,
                "filepath": prior_fp,
                "size_bytes": size_bytes,
                "char_count": char_count,
                "sha256": str(prior_entry.get("sha256") or content_sha256),
                "text_extracted": text_extracted,
                "scan_note": scan_note,
                "suspected_sell_side": suspected_sell_side,
                "sell_side_warning": sell_side_warning,
                "duplicate": False,
                "audit_path": "var/dalton/wiki_ingest_audit.jsonl",
            }
            return resp_obj

        def _build_duplicate_response(dup_info: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 400, {
                "ok": False,
                "status": "duplicate",
                "error_code": "duplicate",
                "duplicate": True,
                "duplicate_reason": dup_info.get("reason"),
                "existing_doc_id": dup_info.get("doc_id"),
                "existing_filepath": dup_info.get("filepath"),
                "similarity": dup_info.get("similarity"),
                "target_type": target_type,
                "target": resolved_slug,
                "content_type": canonical_content_type,
                "date": date_str,
                "filename": preview_filename,
                "filepath": preview_filepath,
                "size_bytes": size_bytes,
                "char_count": char_count,
                "sha256": content_sha256,
                "text_extracted": text_extracted,
                "scan_note": scan_note,
                "suspected_sell_side": suspected_sell_side,
                "sell_side_warning": sell_side_warning,
                "error": (
                    f"检测到重复文档 (duplicate: {dup_info.get('reason')})，已存在记录 "
                    f"{dup_info.get('filepath') or dup_info.get('doc_id') or ''}，拒绝重复入库。"
                ).strip(),
            }

        def _replay_and_backfill_audit_if_needed(prior_entry: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            if request_id is not None and self._find_wiki_audit_by_request_id(request_id) is None:
                try:
                    self._append_wiki_audit_entry(
                        {
                            "timestamp": str(
                                prior_entry.get("timestamp")
                                or datetime.now(timezone.utc).isoformat()
                            ),
                            "request_id": request_id,
                            "ingested_by": prior_entry.get("ingested_by") or ingested_by_norm,
                            "ingested_via": prior_entry.get("ingested_via") or ingested_via_norm,
                            "target_type": str(prior_entry.get("target_type") or target_type),
                            "target": str(prior_entry.get("target") or resolved_slug),
                            "content_type": str(
                                prior_entry.get("content_type") or canonical_content_type
                            ),
                            "date": str(prior_entry.get("date") or date_str),
                            "sha256": str(prior_entry.get("sha256") or content_sha256),
                            "doc_id": prior_entry.get("doc_id"),
                            "filepath": prior_entry.get("filepath"),
                            "filename": prior_entry.get("filename"),
                            "result": "ok",
                            "status": "ingested",
                            "_norm_excerpt": str(prior_entry.get("_norm_excerpt") or ""),
                        }
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed backfilling wiki ingest audit entry on replay: %s",
                        type(exc).__name__,
                    )
            return 200, _build_replay_response(prior_entry)

        # Idempotent replay check by request_id (reads independent idempotency store first)
        if not dry_run and request_id is not None:
            prior_entry = self._find_wiki_idempotent_result(request_id)
            if prior_entry is not None:
                return _replay_and_backfill_audit_if_needed(prior_entry)

        if dry_run:
            dup_info = await self._check_wiki_duplicate(
                target_type=target_type,
                resolved_slug=resolved_slug,
                canonical_content_type=canonical_content_type,
                date_str=date_str,
                content_sha256=content_sha256,
                norm_text=norm_text,
                wiki_cfg=wiki_cfg,
            )
            if dup_info is not None:
                return _build_duplicate_response(dup_info)
            return 200, {
                "ok": True,
                "status": "dry_run",
                "dry_run": True,
                "target_type": target_type,
                "target": resolved_slug,
                "raw_target": raw_target.strip(),
                "is_existing_slug": is_existing_slug,
                "content_type": canonical_content_type,
                "date": date_str,
                "filename": preview_filename,
                "upload_filename": upload_filename,
                "filepath": preview_filepath,
                "size_bytes": size_bytes,
                "char_count": char_count,
                "sha256": content_sha256,
                "text_extracted": text_extracted,
                "scan_note": scan_note,
                "suspected_sell_side": suspected_sell_side,
                "sell_side_warning": sell_side_warning,
                "duplicate": False,
            }

        # Built before any lock is taken: these touch the filesystem and can
        # raise, and only the try/finally below releases the claim locks.
        run_sh = self._resolve_wiki_run_sh()
        subproc_env = self._build_wiki_subprocess_env()

        # Kernel file locks (`fcntl.flock`) across duplicate check + CLI write + idempotency/audit persistence
        slot_key = hashlib.sha256(
            f"{target_type}:{resolved_slug.lower()}:{date_str}:{canonical_content_type}".encode("utf-8")
        ).hexdigest()
        claim_fds, claim_err = self._try_acquire_wiki_claim_locks(
            content_sha256=content_sha256,
            slot_key=slot_key,
            request_id=request_id,
        )
        if claim_err == "same_request_inflight":
            return 409, {
                "ok": False,
                "status": "in_progress",
                "error_code": "request_in_progress",
                "request_id": request_id,
                "error": "同一请求正在处理，稍后重试",
            }
        if claim_err is not None or claim_fds is None:
            return _build_duplicate_response(
                {
                    "reason": claim_err or "concurrent_inflight",
                    "doc_id": None,
                    "filepath": preview_filepath,
                    "similarity": 1.0,
                }
            )

        req_tmp_dir: Path | None = None
        tmp_file_path: Path | None = None
        tmp_content_path: Path | None = None

        try:
            if request_id is not None:
                prior_entry = self._find_wiki_idempotent_result(request_id)
                if prior_entry is not None:
                    return _replay_and_backfill_audit_if_needed(prior_entry)

            dup_info = await self._check_wiki_duplicate(
                target_type=target_type,
                resolved_slug=resolved_slug,
                canonical_content_type=canonical_content_type,
                date_str=date_str,
                content_sha256=content_sha256,
                norm_text=norm_text,
                wiki_cfg=wiki_cfg,
            )
            if dup_info is not None:
                return _build_duplicate_response(dup_info)

            self.cfg.wiki_ingest_temp_dir.mkdir(parents=True, exist_ok=True)
            req_tmp_dir = Path(
                tempfile.mkdtemp(
                    prefix="wiki_ingest_",
                    dir=str(self.cfg.wiki_ingest_temp_dir),
                )
            )
            os.chmod(req_tmp_dir, 0o700)

            if file_bytes is not None and upload_filename is not None:
                tmp_file_path = req_tmp_dir / upload_filename
                fd = os.open(
                    str(tmp_file_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.write(fd, file_bytes)
                finally:
                    os.close(fd)
            elif content_text is not None:
                tmp_content_path = req_tmp_dir / "content.txt"
                fd = os.open(
                    str(tmp_content_path),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    os.write(fd, content_text.encode("utf-8"))
                finally:
                    os.close(fd)

            cmd = build_wiki_ingest_cmd(
                run_sh=run_sh,
                target_type=target_type,
                resolved_slug=resolved_slug,
                content_type=canonical_content_type,
                date_str=date_str,
                content_file_path=tmp_content_path,
                file_path=tmp_file_path,
                source=source_clean,
                source_url=source_url_clean,
            )
            try:
                cli_res = await self._run_company_wiki_ingest_cli(
                    cmd,
                    env=subproc_env,
                    timeout_s=self.cfg.wiki_ingest_timeout_s,
                )
            except asyncio.CancelledError:
                audit_cancelled = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "request_id": request_id,
                    "ingested_by": ingested_by_norm,
                    "ingested_via": ingested_via_norm,
                    "target_type": target_type,
                    "target": resolved_slug,
                    "content_type": canonical_content_type,
                    "date": date_str,
                    "sha256": content_sha256,
                    "doc_id": None,
                    "filepath": None,
                    "result": "cancelled",
                    "status": "cancelled",
                    "error": "CancelledError",
                }
                try:
                    self._append_wiki_audit_entry(audit_cancelled)
                except Exception as exc:
                    logger.warning(
                        "Failed writing wiki cancelled audit entry: %s",
                        type(exc).__name__,
                    )
                raise

            if cli_res.get("ok") is not True:
                err_status = str(cli_res.get("status") or "cli_error")
                err_msg = str(cli_res.get("error") or "company-wiki CLI failed")
                audit_fail = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "request_id": request_id,
                    "ingested_by": ingested_by_norm,
                    "ingested_via": ingested_via_norm,
                    "target_type": target_type,
                    "target": resolved_slug,
                    "content_type": canonical_content_type,
                    "date": date_str,
                    "sha256": content_sha256,
                    "doc_id": None,
                    "filepath": None,
                    "result": "error",
                    "status": err_status,
                    "error": err_msg,
                }
                try:
                    self._append_wiki_audit_entry(audit_fail)
                except Exception as exc:
                    logger.warning("Failed writing wiki failure audit entry: %s", type(exc).__name__)
                return 502, {
                    "ok": False,
                    "status": err_status,
                    "request_id": request_id,
                    "error": err_msg,
                    "hint": cli_res.get("hint"),
                }

            doc_id = cli_res.get("doc_id")
            filepath = str(cli_res.get("filepath") or preview_filepath)
            filename = str(cli_res.get("filename") or Path(filepath).name)
            now_iso = datetime.now(timezone.utc).isoformat()
            idemp_record = {
                "timestamp": now_iso,
                "request_id": request_id,
                "ingested_by": ingested_by_norm,
                "ingested_via": ingested_via_norm,
                "target_type": target_type,
                "target": resolved_slug,
                "content_type": canonical_content_type,
                "date": date_str,
                "sha256": content_sha256,
                "doc_id": doc_id,
                "filepath": filepath,
                "filename": filename,
                "result": "ok",
                "status": "ingested",
                "chunks": cli_res.get("chunks"),
                "tags": cli_res.get("tags") or [],
                "related_companies": cli_res.get("related_companies") or [],
                "related_sectors": cli_res.get("related_sectors") or [],
                "_norm_excerpt": norm_text[:8192] if norm_text else "",
            }
            idemp_key = request_id or f"sha256:{content_sha256}"
            try:
                self._save_wiki_idempotent_result(idemp_key, idemp_record)
            except Exception as exc:
                logger.warning(
                    "Failed writing wiki idempotency result after CLI success: %s",
                    type(exc).__name__,
                )

            audit_ok = {
                "timestamp": now_iso,
                "request_id": request_id,
                "ingested_by": ingested_by_norm,
                "ingested_via": ingested_via_norm,
                "target_type": target_type,
                "target": resolved_slug,
                "content_type": canonical_content_type,
                "date": date_str,
                "sha256": content_sha256,
                "doc_id": doc_id,
                "filepath": filepath,
                "filename": filename,
                "result": "ok",
                "status": "ingested",
                "_norm_excerpt": norm_text[:8192] if norm_text else "",
            }
            try:
                self._append_wiki_audit_entry(audit_ok)
            except Exception as exc:
                logger.warning(
                    "Failed writing wiki ingest audit entry after CLI success: %s",
                    type(exc).__name__,
                )

            return 200, {
                "ok": True,
                "status": "ingested",
                "dry_run": False,
                "request_id": request_id,
                "doc_id": doc_id,
                "document_id": f"wiki:{doc_id}" if doc_id is not None else None,
                "target_type": target_type,
                "target": resolved_slug,
                "content_type": canonical_content_type,
                "date": date_str,
                "filename": filename,
                "filepath": filepath,
                "size_bytes": size_bytes,
                "char_count": char_count,
                "sha256": content_sha256,
                "text_extracted": text_extracted,
                "scan_note": scan_note,
                "chunks": cli_res.get("chunks"),
                "tags": cli_res.get("tags") or [],
                "related_companies": cli_res.get("related_companies") or [],
                "related_sectors": cli_res.get("related_sectors") or [],
                "suspected_sell_side": suspected_sell_side,
                "sell_side_warning": sell_side_warning,
                "duplicate": False,
                "audit_path": "var/dalton/wiki_ingest_audit.jsonl",
            }
        finally:
            for p in (tmp_content_path, tmp_file_path):
                if p is not None:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        pass
            if req_tmp_dir is not None:
                try:
                    shutil.rmtree(req_tmp_dir, ignore_errors=True)
                except OSError:
                    pass
            self._release_wiki_claim_locks(claim_fds)


def _normalize_content_type_key(raw: str) -> str:
    return re.sub(r"[\s\-_]+", " ", raw.strip().lower())


_ALLOWED_CONTENT_TYPE_MAP: dict[str, str] = {}
for _ct in WIKI_INGEST_ALLOWED_CONTENT_TYPES:
    _ALLOWED_CONTENT_TYPE_MAP[_normalize_content_type_key(_ct)] = _ct
    _ALLOWED_CONTENT_TYPE_MAP[_ct.lower().replace(" ", "")] = _ct

_SELL_SIDE_TYPE_RE = re.compile(
    r"(?i)(?:券商|卖方|sales|broker|sell[-_\s]?side|分析师电话会|研报|投行报告|机构研报)"
)
_SELL_SIDE_BODY_RE = re.compile(
    r"(?i)(?:证券研究报告|执业证书编号|SAC\s*执业|分析师电话会|sales\s+color|卖方研报|券商研报)"
)
_WIKI_NEEDS_ALIAS_RE = re.compile(
    r"(?i)(?:[\u4e00-\u9fff]|^(?:SH|SZ|BJ|HK)[.\-]?\d{4,6}$|^\d{4,6}\.(?:HK|SH|SZ|SS|BJ)$)"
)
_WIKI_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key_pem",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----",
            re.ASCII,
        ),
    ),
    (
        "api_key_prefix",
        re.compile(
            r"(?:sk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z\-_]{30,}|"
            r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|xai-[A-Za-z0-9_\-]{16,}|tvly-[A-Za-z0-9_\-]{16,}|"
            r"fc-[A-Za-z0-9_\-]{16,}|xox[baprs]-[A-Za-z0-9\-]{16,})",
            re.ASCII,
        ),
    ),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|"
            r"bearer[_-]?token|private[_-]?key|client[_-]?secret|dalton_bridge_(?:admin_)?token|"
            r"gemini_api_key|openai_api_key|anthropic_api_key)\b\s*[:=]\s*['\"]?[A-Za-z0-9._\-+/=]{12,}['\"]?",
            re.ASCII,
        ),
    ),
    (
        "bearer_token",
        re.compile(
            r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._\-+/=]{16,}",
            re.ASCII,
        ),
    ),
)


def _detect_wiki_ingest_secret(*texts: str) -> str | None:
    """Return the matched secret pattern name if any text contains a suspected key/token/PEM, else None."""
    for text in texts:
        if not text:
            continue
        for kind, pat in _WIKI_SECRET_PATTERNS:
            if pat.search(text):
                return kind
    return None


def _normalize_wiki_text_for_hash(text: str) -> str:
    """Normalize line endings and collapse whitespace for content hashing and similarity checks."""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\s+", " ", cleaned)


def _strip_wiki_markdown_frontmatter(raw_md: str) -> str:
    """Strip YAML frontmatter and auto-generated H1 header from an existing wiki markdown file."""
    text = raw_md.replace("\r\n", "\n").strip()
    if text.startswith("---\n"):
        end_idx = text.find("\n---\n", 4)
        if end_idx != -1:
            text = text[end_idx + 5 :].lstrip()
    if text.startswith("# "):
        nl_idx = text.find("\n")
        if nl_idx != -1:
            text = text[nl_idx + 1 :].lstrip()
    return text


def _compute_wiki_text_similarity(text_a: str, text_b: str) -> float:
    """Compute similarity in [0.0, 1.0] between two normalized texts using SequenceMatcher and 3-gram Jaccard."""
    norm_a = _normalize_wiki_text_for_hash(text_a)
    norm_b = _normalize_wiki_text_for_hash(text_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0
    seq_ratio = difflib.SequenceMatcher(None, norm_a[:12000], norm_b[:12000]).ratio()
    compact_a = re.sub(r"\s+", "", norm_a[:12000])
    compact_b = re.sub(r"\s+", "", norm_b[:12000])
    if len(compact_a) >= 3 and len(compact_b) >= 3:
        grams_a = {compact_a[i : i + 3] for i in range(len(compact_a) - 2)}
        grams_b = {compact_b[i : i + 3] for i in range(len(compact_b) - 2)}
        union = len(grams_a | grams_b)
        jaccard = (len(grams_a & grams_b) / union) if union else 0.0
        return max(seq_ratio, jaccard)
    return seq_ratio


def _extract_file_bytes(file_obj: dict[str, Any]) -> tuple[bytes | None, str | None]:
    """Decode raw bytes from `file` dict (`content_b64`, `bytes_b64`, `data_b64`, or `bytes`/`data`/`content`)."""
    for b64_key in ("content_b64", "bytes_b64", "data_b64"):
        val = file_obj.get(b64_key)
        if isinstance(val, str):
            try:
                return base64.b64decode(val.encode("ascii"), validate=True), None
            except Exception:
                return None, f"{b64_key} 不是有效的 base64 编码"
    for raw_key in ("bytes", "data", "content"):
        val = file_obj.get(raw_key)
        if isinstance(val, (bytes, bytearray)):
            return bytes(val), None
        if isinstance(val, str):
            try:
                return base64.b64decode(val.encode("ascii"), validate=True), None
            except Exception:
                return val.encode("utf-8"), None
    return None, "file 对象缺少 content_b64 / bytes 字段"


WIKI_BINARY_SCAN_ONLY_NOTE = "无法提取正文，仅做了字节扫描"


def _inspect_binary_file_for_wiki(
    file_bytes: bytes, ext: str
) -> tuple[str, str, bool, str | None]:
    """Return `(full_byte_scan_text, extracted_text, text_extracted, scan_note)` without truncating bytes."""
    full_latin1 = file_bytes.decode("latin1")
    full_utf8 = file_bytes.decode("utf-8", errors="ignore")
    full_byte_scan_text = f"{full_latin1}\n{full_utf8}"

    if ext == ".docx":
        import io
        import zipfile

        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                if "word/document.xml" in zf.namelist():
                    xml_text = zf.read("word/document.xml").decode("utf-8", errors="ignore")
                    extracted = re.sub(r"<[^>]+>", " ", xml_text).strip()
                    if extracted:
                        return full_byte_scan_text, extracted, True, None
        except Exception:
            pass

    return full_byte_scan_text, "", False, WIKI_BINARY_SCAN_ONLY_NOTE


def build_wiki_ingest_cmd(
    *,
    run_sh: Path | str,
    target_type: str,
    resolved_slug: str,
    content_type: str,
    date_str: str,
    content_file_path: Path | str | None = None,
    file_path: Path | str | None = None,
    source: str | None = None,
    source_url: str | None = None,
) -> list[str]:
    """Build the exact argv list for `company-wiki/run.sh --json add|add-file` (no shell).

    Body text is never passed directly in `--content`; callers must write validated
    text to a bridge-owned `0600` temp file and pass `content_file_path` so CLI
    receives `--content @<temp_file>`.
    """
    target_flag = "--company" if target_type == "company" else "--sector"
    if file_path is not None:
        cmd = [
            str(run_sh),
            "--json",
            "add-file",
            target_flag,
            resolved_slug,
            "--type",
            content_type,
            "--date",
            date_str,
            "--file",
            str(file_path),
        ]
    elif content_file_path is not None:
        cmd = [
            str(run_sh),
            "--json",
            "add",
            target_flag,
            resolved_slug,
            "--type",
            content_type,
            "--date",
            date_str,
            "--content",
            f"@{Path(content_file_path)}",
        ]
    else:
        raise ValueError("Either file_path or content_file_path is required")
    if source:
        cmd.extend(["--source", source])
    if source_url:
        cmd.extend(["--source-url", source_url])
    return cmd


_FEISHU_TARGET_RE = re.compile(r"^feishu:(user:ou_[A-Za-z0-9_-]+)$")
_DISCORD_TARGET_RE = re.compile(r"^discord:(user:[0-9]+)$")


def parse_notify_target(raw: str) -> tuple[str, str]:
    """Split ``<channel>:user:<id>`` into ``(channel, 'user:<id>')``."""
    if not isinstance(raw, str):
        raise ValueError("Target must be a string")
    cleaned = raw.strip()
    m_feishu = _FEISHU_TARGET_RE.match(cleaned)
    if m_feishu:
        return "feishu", m_feishu.group(1)
    m_discord = _DISCORD_TARGET_RE.match(cleaned)
    if m_discord:
        return "discord", m_discord.group(1)
    raise ValueError(f"Unsupported notify target format: {cleaned!r}")


def build_openclaw_message_invoke_body(
    channel: str, target: str, text: str
) -> dict[str, Any]:
    """Build the OpenClaw ``/tools/invoke`` payload for sending a direct message."""
    return {
        "tool": "message",
        "args": {
            "action": "send",
            "channel": channel,
            "target": target,
            "message": text,
        },
    }


def build_feishu_send_card_cmd(
    *,
    script_path: Path | str,
    open_id: str,
    card_json_file: Path | str,
    dry_run: bool = False,
) -> list[str]:
    """Build subprocess argv for ``feishu_send_card.py``."""
    cmd = [
        "python3",
        str(script_path),
        "--chat-id",
        str(open_id),
        "--receive-id-type",
        "open_id",
        "--card-json-file",
        str(card_json_file),
        "--json",
    ]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def _run_feishu_send_card(
    *,
    script_path: Path,
    open_id: str,
    card: dict[str, Any],
    timeout_s: float = FEISHU_SEND_CARD_TIMEOUT_S,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Invoke ``feishu_send_card.py`` with a temporary ``--card-json-file``.

    Never logs or returns subprocess stdout/stderr (which could contain credentials).
    Only reports ``ok`` and ``exit_code``.
    """
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix="dalton_feishu_card_",
            encoding="utf-8",
            delete=False,
        ) as fh:
            json.dump(card, fh, ensure_ascii=False)
            tmp_path = Path(fh.name)

        cmd = build_feishu_send_card_cmd(
            script_path=script_path,
            open_id=open_id,
            card_json_file=tmp_path,
            dry_run=dry_run,
        )
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "exit_code": proc.returncode,
                "error": f"exit_code={proc.returncode}",
            }
        if not dry_run and proc.stdout and proc.stdout.strip():
            try:
                parsed = json.loads(proc.stdout)
                if isinstance(parsed, dict) and parsed.get("status") == "error":
                    return {
                        "ok": False,
                        "exit_code": proc.returncode,
                        "error": "feishu_api_error",
                    }
            except ValueError:
                pass
        return {"ok": True, "exit_code": proc.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "error": "timeout"}
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _post_openclaw_tools_invoke(
    *,
    gateway_url: str,
    gateway_token: str,
    payload: dict[str, Any],
    timeout_s: float = FEISHU_SEND_CARD_TIMEOUT_S,
) -> dict[str, Any]:
    """Send ``POST {gateway_url}/tools/invoke`` bypassing HTTP proxies (``NO_PROXY``)."""
    url = f"{gateway_url.rstrip('/')}/tools/invoke"
    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {gateway_token}",
        },
    )
    # Bypass all proxies explicitly (NO_PROXY for local OpenClaw gateway)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout_s) as resp:
        raw_resp = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw_resp) if raw_resp.strip() else {"ok": True}
        return parsed if isinstance(parsed, dict) else {"ok": True, "result": parsed}


async def _send_http(
    writer: asyncio.StreamWriter,
    status_code: int,
    status_text: str,
    body_Obj: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body_bytes = (
        json.dumps(body_Obj, ensure_ascii=False).encode("utf-8")
        if body_Obj is not None
        else b""
    )
    headers = [
        f"HTTP/1.1 {status_code} {status_text}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body_bytes)}",
        "Connection: close",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append(f"{k}: {v}")
    header_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
    writer.write(header_bytes + body_bytes)
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


def _extract_supplied_token(headers: Mapping[str, str]) -> str:
    """Extract bridge authentication token from `Authorization: Bearer <token>` or `X-Dalton-Bridge-Token`."""
    lowered = {k.lower(): v for k, v in headers.items()}
    auth_header = lowered.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    bridge_header = lowered.get("x-dalton-bridge-token", "").strip()
    if bridge_header:
        return bridge_header
    return ""


def _is_valid_mcp_token(bridge: ResearchDataBridge, supplied_token: str) -> bool:
    """Return True if ``supplied_token`` matches ``DALTON_BRIDGE_TOKEN`` or ``DALTON_BRIDGE_ADMIN_TOKEN``."""
    if not supplied_token:
        return False
    supplied_bytes = supplied_token.encode("utf-8")
    mcp_token = (getattr(bridge.cfg, "token", "") or "").strip()
    admin_token = (getattr(bridge.cfg, "admin_token", "") or "").strip()
    matched = False
    if mcp_token and hmac.compare_digest(supplied_bytes, mcp_token.encode("utf-8")):
        matched = True
    if admin_token and hmac.compare_digest(supplied_bytes, admin_token.encode("utf-8")):
        matched = True
    return matched


async def _require_admin_token(
    bridge: ResearchDataBridge,
    headers: Mapping[str, str],
    writer: asyncio.StreamWriter,
) -> bool:
    """Verify ``DALTON_BRIDGE_ADMIN_TOKEN`` for all non-MCP routes (fail closed when unset)."""
    supplied_token = _extract_supplied_token(headers)
    expected_admin = (getattr(bridge.cfg, "admin_token", "") or "").strip()
    if not expected_admin or not supplied_token:
        await _send_http(
            writer, 401, "Unauthorized", {"ok": False, "error": "Unauthorized"}
        )
        return False
    supplied_bytes = supplied_token.encode("utf-8")
    if hmac.compare_digest(supplied_bytes, expected_admin.encode("utf-8")):
        return True
    mcp_token = (getattr(bridge.cfg, "token", "") or "").strip()
    if mcp_token and hmac.compare_digest(supplied_bytes, mcp_token.encode("utf-8")):
        await _send_http(
            writer, 403, "Forbidden", {"ok": False, "error": "Forbidden"}
        )
        return False
    await _send_http(
        writer, 401, "Unauthorized", {"ok": False, "error": "Unauthorized"}
    )
    return False


async def _handle_client(
    bridge: ResearchDataBridge,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=15.0)
        if not req_line_bytes:
            writer.close()
            return
        req_line = req_line_bytes.decode("utf-8", errors="replace").strip()
        parts = req_line.split(" ")
        if len(parts) < 2:
            await _send_http(writer, 400, "Bad Request", {"error": "Bad Request"})
            return
        method, raw_path = parts[0].upper(), parts[1]
        path = raw_path.split("?", 1)[0]

        # Read headers
        headers: dict[str, str] = {}
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=15.0)
            if not hline or hline in (b"\r\n", b"\n"):
                break
            decoded = hline.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                hk, hv = decoded.split(":", 1)
                headers[hk.strip().lower()] = hv.strip()

        content_length = int(headers.get("content-length") or "0")
        if path == "/embed":
            max_bytes = bridge.cfg.embed_max_bytes
        elif path == "/wiki/ingest":
            max_bytes = MAX_WIKI_INGEST_REQUEST_BYTES
        else:
            max_bytes = MAX_REQUEST_BYTES
        if content_length > max_bytes:
            await _send_http(
                writer,
                413,
                "Payload Too Large",
                {"error": f"Request body exceeds {max_bytes} bytes"},
            )
            return

        body_bytes = b""
        if content_length > 0:
            read_timeout = 60.0 if path == "/wiki/ingest" else 15.0
            body_bytes = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=read_timeout
            )

        # Route 1: GET /healthz
        if method == "GET" and path == "/healthz":
            if bridge.server_version == "unknown":
                try:
                    await bridge.ensure_initialized()
                except Exception as exc:
                    logger.warning(
                        "Healthz init probe failed: %s (exit_code=%s)",
                        type(exc).__name__,
                        getattr(exc, "exit_code", None),
                    )
            health_payload = {
                "ok": True,
                "status": "ok",
                "server_version": bridge.server_version,
                "max_inflight": bridge.cfg.max_inflight,
                "embed_max_inflight": bridge.cfg.embed_max_inflight,
                "embed_max_bytes": bridge.cfg.embed_max_bytes,
                "caller_budget_s": bridge.cfg.caller_budget_s,
                "alphaengine_get_document_today": bridge._read_usage_today(),
                "xreach": await bridge.probe_xreach_version(),
            }
            await _send_http(writer, 200, "OK", health_payload)
            return

        # Route 2: /mcp (header token) or legacy /mcp/<token> (path token, deprecated)
        if path in ("/mcp", "/mcp/") or path.startswith("/mcp/"):
            if path in ("/mcp", "/mcp/"):
                supplied_token = _extract_supplied_token(headers)
            else:
                supplied_token = path[len("/mcp/") :].strip("/") or _extract_supplied_token(headers)

            if not _is_valid_mcp_token(bridge, supplied_token):
                # Constant-time rejection; NEVER log supplied_token or path
                await _send_http(writer, 401, "Unauthorized", {"error": "Unauthorized"})
                return

            if method in ("GET", "DELETE"):
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return

            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return

            try:
                rpc_msg = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                await _send_http(
                    writer,
                    400,
                    "Bad Request",
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    },
                )
                return

            if not isinstance(rpc_msg, dict):
                await _send_http(
                    writer,
                    400,
                    "Bad Request",
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid Request"},
                    },
                )
                return

            req_id = rpc_msg.get("id")
            rpc_method = str(rpc_msg.get("method") or "")
            rpc_params = rpc_msg.get("params") if isinstance(rpc_msg.get("params"), dict) else {}

            # JSON-RPC notifications (no id, e.g., notifications/initialized) -> 202 Accepted
            if "id" not in rpc_msg or req_id is None:
                await _send_http(writer, 202, "Accepted", None)
                return

            if rpc_method == "initialize":
                init_res = await bridge.ensure_initialized()
                offered_proto = str(rpc_params.get("protocolVersion") or "2024-11-05")
                adopted_proto = (
                    offered_proto
                    if offered_proto in ("2024-11-05", "2025-03-26", "2025-11-25")
                    else str(init_res.get("protocolVersion") or "2024-11-05")
                )
                reply = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": adopted_proto,
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "researchdata",
                            "version": bridge.server_version,
                        },
                    },
                }
                await _send_http(writer, 200, "OK", reply)
                return

            if rpc_method == "ping":
                await _send_http(
                    writer, 200, "OK", {"jsonrpc": "2.0", "id": req_id, "result": {}}
                )
                return

            if rpc_method == "tools/list":
                tools = await bridge.get_filtered_tools()
                await _send_http(
                    writer,
                    200,
                    "OK",
                    {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}},
                )
                return

            if rpc_method == "tools/call":
                reply = await bridge.handle_tools_call(req_id, rpc_params)
                await _send_http(writer, 200, "OK", reply)
                return

            # Unsupported method (e.g. server/discover probe -> -32601 so client falls back to legacy initialize)
            await _send_http(
                writer,
                200,
                "OK",
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {rpc_method}"},
                },
            )
            return

        # Unified admin token gate for all non-MCP routes (/embed, /jobs/*, and future routes)
        if not await _require_admin_token(bridge, headers, writer):
            return

        # Route 3: POST /embed (admin token required; path tokens rejected)
        if path == "/embed":
            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return
            try:
                embed_body = json.loads(body_bytes.decode("utf-8"))
                if not isinstance(embed_body, dict):
                    raise ValueError("Expected JSON object")
            except Exception:
                await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Parse error"})
                return
            code, payload = await bridge.handle_embed(embed_body)
            status_label = "OK" if code == 200 else ("Bad Request" if code == 400 else "Bad Gateway")
            await _send_http(writer, code, status_label, payload)
            return

        # Route 4: /jobs/conversation-archive* and /jobs/ops (admin token required)
        if path == "/jobs/ops" or path.startswith("/jobs/conversation-archive"):
            if path == "/jobs/conversation-archive":
                if method != "GET":
                    await _send_http(writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"})
                    return
                code, payload = await bridge.handle_get_conversation_archive()
                await _send_http(writer, code, "OK", payload)
                return

            if path == "/jobs/conversation-archive/run":
                if method != "POST":
                    await _send_http(writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"})
                    return
                code, payload = await bridge.handle_run_conversation_archive()
                await _send_http(writer, code, "OK", payload)
                return

            if path in ("/jobs/conversation-archive/enable", "/jobs/conversation-archive/disable"):
                if method != "POST":
                    await _send_http(writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"})
                    return
                enable_flag = path.endswith("/enable")
                code, payload = await bridge.handle_toggle_conversation_archive(enable_flag)
                await _send_http(writer, code, "OK", payload)
                return

            if path == "/jobs/ops":
                if method != "GET":
                    await _send_http(writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"})
                    return
                code, payload = await bridge.handle_get_ops_jobs()
                await _send_http(writer, code, "OK", payload)
                return

            await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
            return

        # Route 5: POST /notify (behind the admin token gate above: only the
        # backend may push messages; a sandbox holds the MCP token only).
        # DALTON-HOOK(bridge-admin-token)
        if path == "/notify":
            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return
            try:
                notify_body = json.loads(body_bytes.decode("utf-8"))
                if not isinstance(notify_body, dict):
                    raise ValueError("Expected JSON object")
            except Exception:
                await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Parse error"})
                return
            code, payload = await bridge.handle_notify(notify_body)
            status_label = "OK" if code == 200 else "Bad Request"
            await _send_http(writer, code, status_label, payload)
            return

        # Route 6: POST /wiki/ingest (behind the admin token gate above; MCP token gets 403)
        # DALTON-HOOK(wiki-ingest)
        if path == "/wiki/ingest":
            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return
            try:
                ingest_body = json.loads(body_bytes.decode("utf-8"))
                if not isinstance(ingest_body, dict):
                    raise ValueError("Expected JSON object")
            except Exception:
                await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Parse error"})
                return
            code, payload = await bridge.handle_wiki_ingest(ingest_body)
            status_label = (
                "OK"
                if code == 200
                else ("Bad Request" if code == 400 else "Bad Gateway")
            )
            await _send_http(writer, code, status_label, payload)
            return

        await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
    except Exception as exc:
        logger.error("Unhandled bridge error: %s", type(exc).__name__)
        try:
            await _send_http(
                writer,
                500,
                "Internal Server Error",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Internal bridge error"},
                },
            )
        except Exception:
            pass


async def main() -> None:
    cfg = BridgeConfig()
    if not cfg.token:
        logger.error(
            "DALTON_BRIDGE_TOKEN is not set (checked env and ~/.config/dalton/researchdata-bridge.env)"
        )
        sys.exit(1)
    if not cfg.admin_token:
        logger.warning(
            "DALTON_BRIDGE_ADMIN_TOKEN is not set (checked env and ~/.config/dalton/researchdata-bridge.env); "
            "all admin endpoints (/embed, /jobs/*) will reject requests (fail closed)"
        )

    bridge = ResearchDataBridge(cfg)
    try:
        await bridge.ensure_initialized()
    except Exception as exc:
        logger.warning(
            "Startup initialize probe warning: %s (exit_code=%s)",
            type(exc).__name__,
            getattr(exc, "exit_code", None),
        )

    server = await asyncio.start_server(
        lambda r, w: _handle_client(bridge, r, w),
        host=cfg.host,
        port=cfg.port,
    )
    logger.warning(
        "路径令牌已弃用 (POST /mcp/<token> is deprecated; use POST /mcp with Authorization: Bearer <token> or X-Dalton-Bridge-Token)"
    )
    logger.info(
        "Listening on http://%s:%d (server_version=%s, max_inflight=%d, caller_budget_s=%d)",
        cfg.host,
        cfg.port,
        bridge.server_version,
        cfg.max_inflight,
        cfg.caller_budget_s,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
