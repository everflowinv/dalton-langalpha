#!/usr/bin/env python3
"""Host-side Streamable HTTP MCP bridge for OpenClaw's `researchdata_mcp.py`.

Listens on `127.0.0.1:8960` (configurable via `DALTON_BRIDGE_HOST` / `DALTON_BRIDGE_PORT`).
Endpoints:
  - `GET /healthz` (unauthenticated health & quota check)
  - `POST /mcp` (Streamable HTTP JSON-RPC 2.0 endpoint; authenticated via
    `Authorization: Bearer <token>` or `X-Dalton-Bridge-Token` header; legacy
    `POST /mcp/<token>` path remains accepted during transition)
  - `POST /notify`, `POST /feishu/*` (admin-token routes: notification fanout
    and the host-side Feishu OpenAPI proxy)

Pure Python 3.11+ standard library only.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import difflib
import hashlib
import hmac
import http.client
import io
import ipaddress
import json
import logging
import math
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

# Ensure sibling `source_identity.py` is importable regardless of cwd
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import log_redaction as _log_redaction  # noqa: E402
import source_identity  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] researchdata_bridge: %(message)s",
)
logger = logging.getLogger("researchdata_bridge")

# P0 fix1 item 7: correlation id for log lines emitted outside
# handle_tools_call's own scope (the stdio subprocess warning, the HTTP
# handler's last-resort catch). Set at each tools/call entry, read wherever a
# line is emitted — a ContextVar rather than a kwarg so _run_stdio_rpc's
# signature, and every test double built against it, stay untouched.
_current_corr_id: ContextVar[str | None] = ContextVar(
    "researchdata_bridge_corr_id", default=None
)

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

# P0 fix1 item 1: the secret-pattern stack now lives in the shared
# pure-stdlib `log_redaction` module (one rule set for this bridge and the
# review-window export); these aliases keep every existing reader unchanged.
_STDERR_SECRET_PATTERNS = _log_redaction.STDERR_SECRET_PATTERNS


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


def _sanitize_failure_log_excerpt(raw: str, *, max_len: int = 300) -> str:
    """The failure log's error excerpt: `sanitize_stderr_text`'s token/secret
    redaction plus the PII those patterns never caught — URL query values,
    whole Cookie/Set-Cookie strings (to end of line), email addresses and
    mainland-China phone numbers — then newlines / ESC / control characters
    collapsed to spaces and truncated to `max_len`.

    P0 fix2 A: delegates to `log_redaction.clean_log_field` so `tools/call`,
    `sec_call_failed`, and every other bridge log line run the exact same
    redaction + control-char stripping + truncation chain."""
    return _log_redaction.clean_log_field(raw, max_len=max_len)


# SEC cluster observability (backlog 三-4, 2026-10-28): the 10-27 list-filings
# cluster failed within 20s and the status-only lines could not say whether the
# upstream answered 4xx/5xx or never answered. One structured line per SEC
# route call carries the correlation id, any upstream HTTP status the payload
# or error text reveals, the sanitized upstream error head, elapsed time, the
# bridge's own stdio attempt count, and the ticker/form asked for.
_SEC_ERROR_EXCERPT_MAX = 500
_SEC_CODE_MAX = 64

# An error object the upstream stringified into the error text (Python repr
# or JSON): pull its `code` value out so attribution survives `_apply_error_
# and_empty_contract` having flattened the dict before we see it (P0 fix1 6).
_ERROR_CODE_IN_TEXT_RE = re.compile(
    r"(?i)(?:\b|[{'\"])(?:error[_-]?)?code['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_.\-]{1,64})['\"]"
)


def _error_code_from_text(err_text: str) -> str:
    match = _ERROR_CODE_IN_TEXT_RE.search(err_text)
    return match.group(1) if match is not None else ""


def _extract_upstream_http_status(payload: dict[str, Any], err_text: str) -> int | None:
    """Best-effort upstream HTTP status for a failed call: an explicit
    ``status_code``/``http_status``/``upstream_status`` int in the payload
    (top level, ``data``, ``data.structuredContent``, or the error object),
    else the first explicit status marker in the error text — via the shared
    `log_redaction.http_status_from_text`, which also reads a dict the
    upstream stringified (``'status_code': 403`` / ``"status_code": 403``).
    None when the upstream never said one."""
    containers: list[Any] = [payload]
    data_obj = payload.get("data")
    if isinstance(data_obj, dict):
        containers.append(data_obj)
        sc = data_obj.get("structuredContent")
        if isinstance(sc, dict):
            containers.append(sc)
    err_obj = payload.get("error")
    if isinstance(err_obj, dict):
        containers.append(err_obj)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("upstream_status", "http_status", "status_code"):
            raw = container.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int):
                continue
            if 100 <= raw <= 599:
                return raw
    return _log_redaction.http_status_from_text(err_text)


def _log_sec_call_outcome(
    *,
    corr_id: str,
    operation: str,
    arguments: Mapping[str, Any],
    payload: dict[str, Any] | None,
    elapsed_ms: int,
    stdio_attempts: int,
    error_text: str = "",
    error_code: str = "",
    upstream_status_hint: int | None = None,
    timeout_s: float | None = None,
) -> None:
    """One structured line per SEC route call — failure at WARNING, success at
    DEBUG. The correlation id ties it to the same request's other lines; a
    payload with no upstream status logs ``-`` rather than failing."""
    ticker = _log_redaction.clean_log_field(arguments.get("ticker") or "", max_len=32) or "-"
    form_raw = arguments.get("form")
    params_obj = arguments.get("params")
    if (form_raw is None or form_raw == "") and isinstance(params_obj, dict):
        form_raw = params_obj.get("form")
    form = _log_redaction.clean_log_field(form_raw or "", max_len=32) or "-"

    err_code = error_code
    upstream_status: int | None = upstream_status_hint
    if payload is not None:
        err_obj = payload.get("error")
        if isinstance(err_obj, dict):
            err_code = err_code or str(err_obj.get("code") or err_obj.get("error_code") or "")
        err_code = err_code or str(payload.get("error_code") or "")
        if not error_text:
            error_text = str(payload.get("error") or "")
        if upstream_status is None:
            upstream_status = _extract_upstream_http_status(payload, error_text)
    elif upstream_status is None and error_text:
        upstream_status = _log_redaction.http_status_from_text(error_text)
    if not err_code and error_text:
        err_code = _error_code_from_text(error_text)
    if timeout_s is not None and not error_text:
        error_text = f"researchdata_bridge timed out after {timeout_s}s"

    op = _log_redaction.clean_log_field(operation or "", max_len=64) or "list-filings"
    base_args = (
        corr_id,
        op,
        ticker,
        form,
        upstream_status if upstream_status is not None else "-",
        elapsed_ms,
        stdio_attempts,
        max(0, stdio_attempts - 1),
    )
    if payload is not None and bool(payload.get("ok")):
        logger.debug(
            "sec_call_ok corr=%s op=%s ticker=%s form=%s upstream_status=%s "
            "elapsed_ms=%d attempts=%d retries=%d",
            *base_args,
        )
        return
    logger.warning(
        "sec_call_failed corr=%s op=%s ticker=%s form=%s upstream_status=%s "
        "elapsed_ms=%d attempts=%d retries=%d error_code=%s error=%s",
        *base_args,
        _log_redaction.clean_log_field(err_code, max_len=_SEC_CODE_MAX) or "-",
        _log_redaction.clean_log_field(error_text, max_len=_SEC_ERROR_EXCERPT_MAX) or "-",
    )


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
        # B5: per-call AlphaEngine usage ledger (JSONL, one line per call —
        # search / read / fan-out leg), beside the bridge's other logs.
        self.alphaengine_usage_file: Path = Path(
            os.environ.get(
                "DALTON_ALPHAENGINE_USAGE_FILE",
                os.path.expanduser("~/Library/Logs/alphaengine_usage.jsonl"),
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
        self.launch_agents_dir: Path = Path(
            os.environ.get(
                "DALTON_LAUNCH_AGENTS_DIR",
                os.path.expanduser("~/Library/LaunchAgents"),
            )
        ).expanduser()
        self.archive_plist_file: Path = Path(
            os.environ.get(
                "DALTON_ARCHIVE_PLIST_FILE",
                str(self.launch_agents_dir / "com.dalton.conversation-archive.plist"),
            )
        ).expanduser()
        self.pg_backup_plist_file: Path = Path(
            os.environ.get(
                "DALTON_PG_BACKUP_PLIST_FILE",
                str(self.launch_agents_dir / "com.dalton.pg-backup.plist"),
            )
        ).expanduser()
        self.pg_backup_log_file: Path = Path(
            os.environ.get(
                "DALTON_PG_BACKUP_LOG_FILE",
                os.path.expanduser("~/Library/Logs/dalton-pg-backup.log"),
            )
        ).expanduser()
        self.backend_watchdog_plist_file: Path = Path(
            os.environ.get(
                "DALTON_BACKEND_WATCHDOG_PLIST_FILE",
                str(self.launch_agents_dir / "com.dalton.backend-watchdog.plist"),
            )
        ).expanduser()
        self.watchdog_failures_file: Path = Path(
            os.environ.get(
                "DALTON_WATCHDOG_FAILURES_FILE",
                os.path.expanduser("~/Backups/dalton/.watchdog_failures"),
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
        # D1: market-digest's run.sh for sales-notes ingestion.
        self.market_digest_run_sh: Path | None = (
            Path(os.environ["DALTON_MARKET_DIGEST_RUN_SH"]).expanduser()
            if os.environ.get("DALTON_MARKET_DIGEST_RUN_SH", "").strip()
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
        # Feishu doc read/write proxy (DALTON-HOOK(feishu-docs)): credentials live on
        # the host only — env first, else a read-only lookup of the OpenClaw channel
        # account. Never logged, never returned by any route.
        env_feishu_app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        env_feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        if env_feishu_app_id and env_feishu_app_secret:
            self.feishu_app_id = env_feishu_app_id
            self.feishu_app_secret = env_feishu_app_secret
        else:
            self.feishu_app_id, self.feishu_app_secret = _read_openclaw_feishu_credentials()
        try:
            self.feishu_http_timeout_s: float = max(
                5.0,
                float(os.environ.get("FEISHU_HTTP_TIMEOUT_S", "30") or 30.0),
            )
        except ValueError:
            self.feishu_http_timeout_s = 30.0
        # Same rule as OpenClaw's workspace AGENTS.md (飞书文档权限规则): every doc the
        # bot creates is shared full_access with the Eve tracking group, which holds
        # the internal team. Empty disables it.
        self.feishu_default_grant_chat_id: str = os.environ.get(
            "FEISHU_DEFAULT_GRANT_CHAT_ID", FEISHU_DEFAULT_GRANT_CHAT_ID
        ).strip()
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
    "解读/机构与个人会议 → alphaengine；一线 → guidepoint；管理层原话 → transcript；日常信息流 → sales-notes/tmtb/funda-ai；"
    "市场反应 → X/WebSearch。\n"
    "何时选用哪个路由（按研究情境，基于 catalog）：\n"
    "- 卖方观点与研报、机构/个人会议、录制状态与词典 → alphaengine（先 search_library 再 get_document）；\n"
    "- 行业一线反馈 → guidepoint；\n"
    "- 管理层口径 → transcript（AlphaEngine 业绩会转写；无匹配时用 | 分隔中英文公司名重试或扩大 start/end）；\n"
    "- 历史沉淀 → company-wiki（只读，params.company=TICKER）列目录用 list、search 须 query；\n"
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

    Pagination range in `params`: `offset` (>= 0) and `max_chars` (1..30000; SEC allows up to 500000).
    """
    return (
        "Fund-internal sources; call from execute_code only (no direct path). "
        "Start with `catalog()` or `catalog(route=...)` when unsure about route parameters. "
        "Research: `research(route=..., operation=..., query=..., start=YYYY-MM-DD, end=..., limit=...)`. "
        f"{RESEARCH_EVIDENCE_AND_ROUTE_GUIDE.replace(chr(10), ' ')} "
        'Finance: `finance(route="sec"|"estimates"|"cn-hk")`. '
        'Macro: `macro(route="fred"|"treasury"|"market-regime")`. '
        "Read only ids that search returned (`wiki:`, `mail:`, `tmtb:`, `funda:`, `alphaengine` opaque string doc_id including `_inst_` suffix). "
        "Page long text with params.offset/max_chars (offset >= 0, max_chars 1-30000; SEC attachments/read-item allow up to 500000; get_document supports params.content_field with independent pagination per field). "
        "Leave `timeout` unset: the bridge sets per-route limits and long reads may take minutes. "
        'AlphaEngine 支持文档与机构/个人会议浏览与筛选（params.meeting_scope="institution"|"personal"，无关键词默认 sort="newest"，按公司检索请用关键词 query（公司名 + ticker）及必要时 search_in="title"）、'
        "录制任务与状态查询（list_recording_tasks/get_recording_status/get_recording_task）、文档基础信息与附件列表（get_document_info/list_attachments）、"
        "按字段读取正文（get_document 的 params.content_field）及词典查询（list_filters）；检索可以放开，全文阅读（get_document）计配额，读最相关的几篇。"
        "Guidepoint 仅返回问答摘录（无全文接口、无 cursor 翻页；limit 省略默认 3，最大 30，不能由 30 条摘录推断已扫完全部访谈；"
        "params.keywords 为 1–10 个非空字符串，多项 OR、词组内 AND，先筛访谈再排片段，不筛选时省略或传 null；"
        "params.recency_bias 为 1–100 整数，偏好近期一般从 30 起，只调排序非日期窗口；start/end 按 UTC 日期硬过滤）。"
        'transcript 单次最多扫 10 页×50 条，返回 coverage（complete=false 时将 continuation_params 交给 route="alphaengine", operation="search_library" 续查，不要传回 transcript）。'
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
# Routes whose gateway (data_gateway.py) accepts query/start/end only (the
# library ones add `document`) and refuses `--ticker`; the bridge folds a
# ticker into the query instead of letting the call die upstream.
TICKER_FOLDS_INTO_QUERY_ROUTES: frozenset[str] = frozenset(
    {
        "all",
        "alphaengine",
        "guidepoint",
        "company-wiki",
        "sales-notes",
        "sales-mail",
        "tmtb",
        "funda-ai",
    }
)


def _query_contains_ticker(query: str, ticker: str) -> bool:
    """True when ``ticker`` already appears in ``query`` as a whole token (non-alphanumeric boundaries, case-insensitive)."""
    if not ticker or not query:
        return False
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])",
            query,
            re.IGNORECASE,
        )
    )
INTERNAL_LIBRARY_READ_OPERATIONS: frozenset[str] = frozenset({"read"})
MAX_CHARS_FLOOR: int = 1
MAX_CHARS_CEILING: int = 30000
SEC_MAX_CHARS_CEILING: int = 500000
TRANSCRIPT_QUERY_IGNORED_NOTE: str = (
    "transcript 的 query 只用来传公司名，主题检索请在拿到 doc_id 后读正文。"
)
TRANSCRIPT_NO_MATCH_HINT: str = (
    "业绩会转写仅来自 AlphaEngine（单次最多扫描 10 页 × 50 条）；若 coverage.complete=false 且带 coverage.continuation_params，"
    "表示本次有界检索未扫完全量，请把 continuation_params 交给 research(route='alphaengine', operation='search_library', ...) 继续翻页；"
    "若 complete=true 确定无匹配，请换公司名（中英文用 | 分隔，如 query='腾讯|Tencent'）重试，或者扩大 start/end 时间范围。"
)
TRANSCRIPT_CATALOG_RULE: str = (
    "业绩会转写仅来自 AlphaEngine（单次最多扫描 10 页 × 50 条 = 500 条）；ticker 必填，query 只用来传公司名补充（中英文用 | 分隔，勿传主题词），"
    "主题检索请在拿到 doc_id 后读正文；返回含 coverage（complete、pages_scanned、records_scanned、warnings、continuation_params），"
    "complete=false 时表示有界扫描未完（有候选时保留候选列表且 status=partial，零候选时仅表示本次有界检索未命中），续查参数 coverage.continuation_params 必须交给 research(route='alphaengine', operation='search_library', ...) 续查（切勿传回 transcript）；"
    "确定无匹配（complete=true）时，换公司名（中英文用 | 分隔）重试，或者扩大 start/end 时间范围。"
)
INTERNAL_LIBRARY_PAGINATION_RULE: str = (
    "正文分页参数范围：params.offset >= 0，params.max_chars 为 1–30000（超出 30000 自动截断为 30000）；"
    "读取用顶层 document 参数传带前缀 ID（wiki:/mail:/tmtb:/digest:/funda:），不透明字符串 doc_id（含 _inst_ 后缀）或 alphaengine:<doc_id> 属 AlphaEngine 文档。"
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

_ALPHAENGINE_DOC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,159}")
_ALPHAENGINE_ALLOWED_CONTENT_FIELDS: tuple[str, ...] = (
    "auto",
    "original_text",
    "content",
    "digest",
    "digest_cn",
    "main_point",
    "question_answer",
)


def _normalize_alphaengine_doc_id(raw_value: Any) -> str:
    """Strip at most one explicit 'alphaengine:' prefix without stripping whitespace."""
    text = "" if raw_value is None else str(raw_value)
    if text.startswith("alphaengine:"):
        text = text[len("alphaengine:") :]
    return text


def _is_valid_alphaengine_doc_id(doc_id: str) -> bool:
    """Validate opaque AlphaEngine document IDs (preserving _inst_ suffix, rejecting path/encoding chars)."""
    return bool(doc_id and _ALPHAENGINE_DOC_ID_RE.fullmatch(doc_id))


ALPHAENGINE_OPERATION_NESTED_PARAMS: dict[str, list[str]] = {
    "search_library": [
        "query",
        "date_from",
        "date_to",
        "source_names",
        "source_ids",
        "document_categories",
        "document_codes",
        "type_ids",
        "markets",
        "industry_names",
        "meeting_scope",
        "search_in",
        "company_names",
        "company_short_names",
        "stock_group_ids",
        "first_regions",
        "second_regions",
        "own_reservations_only",
        "optional_only",
        "sort",
        "cursor",
        "limit",
        "include_snippets",
    ],
    "get_document": [
        "doc_id",
        "mode",
        "offset",
        "max_chars",
        "content_field",
    ],
    "get_document_info": [
        "doc_id",
    ],
    "list_attachments": [
        "doc_id",
    ],
    "list_recording_tasks": [
        "meeting_scope",
        "page",
        "limit",
        "query",
        "states",
        "date_from",
        "date_to",
        "source_ids",
        "industry_ids",
        "own_reservations_only",
        "optional_only",
    ],
    "get_recording_status": [
        "meeting_scope",
    ],
    "get_recording_task": [
        "meeting_scope",
        "roadshow_id",
        "wechat_group",
    ],
    "list_filters": [
        "filter_type",
        "query",
        "document_code",
        "group_id",
        "limit",
        "offset",
    ],
    "auth_status": [],
}

ALPHAENGINE_ACCEPTED_NESTED_PARAMS_UNION: list[str] = list(
    dict.fromkeys(
        k
        for keys in ALPHAENGINE_OPERATION_NESTED_PARAMS.values()
        for k in keys
    )
)

SEC_OPERATION_NESTED_PARAMS: dict[str, list[str]] = {
    "list-filings": [],
    "attachments": ["accession", "document_type", "max_chars"],
    "financials": ["statement"],
    "ratios": [],
    "facts": [],
    "smart-facts": [],
    "search-concepts": [],
    "search-text": ["window", "max_matches"],
    "read-item": ["max_chars"],
    "eight-k": [],
    "insider": [],
}

SEC_ACCEPTED_NESTED_PARAMS_UNION: list[str] = list(
    dict.fromkeys(
        k
        for keys in SEC_OPERATION_NESTED_PARAMS.values()
        for k in keys
    )
)

SEC_VALID_STATEMENTS: frozenset[str] = frozenset({"all", "income", "balance", "cash"})
_SEC_ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")

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
            "params",
        ],
        "accepted_nested_params": SEC_ACCEPTED_NESTED_PARAMS_UNION,
        "accepted_nested_params_by_operation": SEC_OPERATION_NESTED_PARAMS,
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
        "optional_params": ["form", "document", "as_of", "limit", "params"],
        "param_formats": {
            "ticker": "美股代码（如 'META'、'NVDA'）",
            "query": "检索关键词（smart-facts / search-concepts / search-text 必填）",
            "as_of": "YYYY-MM-DD",
            "form": "10-K | 10-Q | 8-K | 20-F | 4",
            "item": "SEC 章节号（如 '1A'、'7'、'2.02'，仅 read-item）",
            "document": "附件文件名（仅 attachments，与 params.document_type 互斥）",
            "params.accession": "SEC 完整 accession 编号（10-2-6 数字，如 '0001326801-26-000010'，仅 attachments）",
            "params.document_type": "附件类型（非空字符串，如 'EX-99.1'，仅 attachments，与顶层 document 互斥）",
            "params.max_chars": "正文读取最大字符数（正整数 1–500000，仅 attachments / read-item）",
            "params.statement": "财务报表筛选（'all' | 'income' | 'balance' | 'cash'，仅 financials）",
            "params.window": "命中上下文窗口字符数（正整数 1–500000，仅 search-text）",
            "params.max_matches": "最大返回命中条数（正整数 1–100，仅 search-text）",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"ticker\": \"META\", \"rows\"|\"data\": ..., \"_sources\": [...], \"normalized_params\": {...}}",
        "examples": [
            "finance(route=\"sec\", operation=\"financials\", ticker=\"META\", form=\"10-K\", limit=5, params={\"statement\": \"income\"})",
            "finance(route=\"sec\", operation=\"smart-facts\", ticker=\"META\", query=\"revenue advertising\", limit=10)",
            "finance(route=\"sec\", operation=\"read-item\", ticker=\"META\", form=\"10-K\", item=\"7\", params={\"max_chars\": 100000})",
            "finance(route=\"sec\", operation=\"attachments\", ticker=\"META\", form=\"8-K\", params={\"accession\": \"0001326801-26-000010\", \"document_type\": \"EX-99.1\", \"max_chars\": 100000})",
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
        "accepted_params": ["ticker", "limit", "params"],
        "accepted_nested_params": ["shares_bn", "tax"],
        "required_params": {"consensus": ["ticker"]},
        "optional_params": ["operation", "limit", "params"],
        "param_formats": {
            "ticker": "美股或港股代码（如 'META'、'NVDA'、'9999.HK'）",
            "params.shares_bn": "摊薄股数（十亿股，有限正数 > 0，用于覆盖或补齐隐含模型股数）",
            "params.tax": "有效税率（0 <= tax < 1 的有限数值，用于覆盖隐含模型默认税率）",
        },
        "return_shape": (
            "{\"ok\": true, \"status\": \"ok\"|\"partial\"|\"error\", \"ticker\": \"META\", "
            "\"data\": {\"ok\": bool, \"status\": \"ok\"|\"partial\"|\"error\", "
            "\"field_status\": {\"earnings_estimate\": \"ok\"|\"empty\"|\"error\", \"revenue_estimate\": ..., "
            "\"eps_trend\": ..., \"eps_revisions\": ..., \"growth_estimates\": ..., \"price_targets\": ..., "
            "\"recommendations\": ..., \"price\": ..., \"implied_model\": ...}, "
            "\"earnings_estimate\": {\"0q\": {\"avg\": ..., \"currency\": \"USD\"|\"CNY\"|...}, ...}, "
            "\"revenue_estimate\": {\"0q\": {\"avg\": ..., \"currency\": \"USD\"|\"CNY\"|...}, ...}, "
            "\"eps_trend\": {...}, \"eps_revisions\": {...}, \"price_targets\": {...}, "
            "\"implied_model\": {\"available\": bool, \"currency\": str|null, \"warnings\": [...]}}, "
            "\"_sources\": [...], \"normalized_params\": {...}}"
        ),
        "examples": [
            "finance(route=\"estimates\", operation=\"consensus\", ticker=\"META\")",
            "finance(route=\"estimates\", operation=\"consensus\", ticker=\"META\", params={\"shares_bn\": 2.55, \"tax\": 0.16})",
        ],
    },
    "cn-hk": {
        "tool": "finance",
        "category": "finance",
        "operations": ["ask", "filings", "quote"],
        "default_operation": "filings",
        "operation_aliases": {
            "price": "quote",
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
            "quote": ["ticker"],
        },
        "optional_params": ["operation", "ticker", "start", "end", "limit"],
        "param_formats": {
            "ticker": "港股用 5 位数字 '00700'（勿写 '0700.HK'，桥接层自动转为 '00700'）；A 股用 6 位数字 '600519'/'300750'。filings / quote 用；ask 不单独接受 ticker（桥接层会并入 query）",
            "start": "YYYY-MM-DD（filings 必填；ask 的日期写进 query）",
            "end": "YYYY-MM-DD（filings 必填；ask 的日期写进 query）",
            "query": "ask 用的自然语言问题，公司和日期写在问题里（如 '腾讯00700 2026Q2 游戏收入与毛利率'）；filings / quote 不接受",
            "quote": "港 A 股当前报价的备用源（AkShare→腾讯→新浪），只接受 ticker、不接受历史日期；返回里保留实际数据源与备用原因。当前价优先用 get_quote，它取不到或明显过期时再用这里，并写明来源",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"ticker\": \"00700\", \"rows\"|\"data\": ..., \"_sources\": [...], \"normalized_params\": {...}}",
        "examples": [
            "finance(route=\"cn-hk\", operation=\"filings\", ticker=\"00700\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "finance(route=\"cn-hk\", operation=\"ask\", query=\"腾讯00700 2026Q2 游戏收入与毛利率\")",
            "finance(route=\"cn-hk\", operation=\"quote\", ticker=\"00700\")",
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
            "start_date",
            "end_date",
        ],
        "required_params": {
            "search": ["query 或 ticker"],
            "read": ["document"],
            "list": [],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "ticker": "股票代码（如 'META'、'0700.HK'、'300750.SZ'）；会并入 query 检索词（上游不支持单独的 ticker 参数）",
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
        "operations": [
            "search_library",
            "get_document",
            "get_document_info",
            "list_attachments",
            "list_recording_tasks",
            "get_recording_status",
            "get_recording_task",
            "list_filters",
            "auth_status",
        ],
        "default_operation": "search_library",
        "operation_aliases": {
            "search": "search_library",
            "query": "search_library",
            "read": "get_document",
            "read_document": "get_document",
            "get": "get_document",
            "filters": "list_filters",
            "status": "auth_status",
        },
        "accepted_params": ["query", "start", "end", "limit", "params", "document", "ticker"],
        "accepted_nested_params": ALPHAENGINE_ACCEPTED_NESTED_PARAMS_UNION,
        "accepted_nested_params_by_operation": ALPHAENGINE_OPERATION_NESTED_PARAMS,
        "required_params": {
            "search_library": [],
            "get_document": ["params.doc_id（或顶层 document）"],
            "get_document_info": ["params.doc_id（或顶层 document）"],
            "list_attachments": ["params.doc_id（或顶层 document）"],
            "list_recording_tasks": ["params.date_from 与 params.date_to 若提供须成对（支持未来日期）"],
            "get_recording_status": [],
            "get_recording_task": ["params.roadshow_id（不可用 doc_id 代替）"],
            "list_filters": [
                "filter_type='companies' 时必填 params.query（须走 params.query）；"
                "filter_type='watchlist_stocks' 时必填 params.group_id"
            ],
            "auth_status": [],
        },
        "optional_params": ["query", "start", "end", "limit", "params", "document", "ticker"],
        "param_formats": {
            "query": (
                "检索关键词（search_library 非必填：有有效关键词时 sort 仅允许并默认 'relevance'；"
                "无关键词浏览时 sort 默认 'newest'，亦允许显式 'relevance'；"
                "list_filters 必须用 params.query，不可用顶层 query）"
            ),
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数）",
            "start": (
                "YYYY-MM-DD（search_library 按 publish_time 筛选，须与 end 成对；"
                "list_recording_tasks 支持未来日期，规范化进 params.date_from/date_to，必须成对提供）"
            ),
            "end": "YYYY-MM-DD",
            "limit": (
                "单页条数：search_library 1-50（不传 limit 时网关默认只返回 3 条，建议显式传 limit，上限 50）；"
                "list_recording_tasks 1-100（默认 20）；"
                "list_filters 1-200（上限 200 须走 params.limit）"
            ),
            "params.doc_id": (
                "不透明字符串文档 ID（如 '290001790954279_inst_134249' 或 '99001122'，保留 _inst_ 后缀；"
                "支持剥除一次 'alphaengine:' 前缀；顶层传 document 时自动映射为 params.doc_id）"
            ),
            "params.meeting_scope": (
                "'institution'（本机构录制会议）| 'personal'（个人会议）；"
                "不可同次混查，且不可与非会议文档分类混用；"
                "source_names 是资料来源（如券商），meeting_scope 与 institution_meeting_flag/personal_meeting_flag 才表示录制归属"
            ),
            "params.document_categories": (
                "分类数组，可选值（与上游 CATEGORY_DEFS 严格一致）：'sell_side', 'sell_side_comment', 'sell_side_report', "
                "'domestic_report', 'foreign_report', 'meeting_minutes', 'institution_meetings', 'personal_meetings', "
                "'third_party_reports', 'expert_library', 'global_think_tank', 'announcement', 'news'；"
                "注意 'institution_meetings' / 'personal_meetings' 与非会议分类（如卖方研报/点评）同用会触发 E_SCOPE_CONFLICT，必须拆成独立调用"
            ),
            "params.search_in": "'full_text'（默认）| 'title'（仅标题检索；可与 cursor 翻页同用，续页时须保持相同 search_in 与筛选条件）",
            "params.company_names": (
                "公司名称数组（旧参数名 companies 已废弃；依赖上游公司 / 地区标签，可能漏召回或错配，不建议作为主要筛选，按公司检索请用 query）"
            ),
            "params.company_short_names": (
                "公司简称数组（依赖上游公司 / 地区标签，可能漏召回或错配，不建议作为主要筛选，按公司检索请用 query）"
            ),
            "params.industry_names": "行业名称数组（旧参数名 industries 已废弃；list_recording_tasks 用 industry_ids）",
            "params.stock_group_ids": (
                "自选分组 ID 数组（依赖上游公司 / 地区标签，可能漏召回或错配，不建议作为主要筛选）"
            ),
            "params.first_regions": (
                "一级地区数组（依赖上游公司 / 地区标签，可能漏召回或错配，不建议作为主要筛选）"
            ),
            "params.second_regions": (
                "二级地区数组（依赖上游公司 / 地区标签，可能漏召回或错配，不建议作为主要筛选）"
            ),
            "params.content_field": (
                "get_document 正文字段选择：仅接受 'auto'（默认）| 'original_text'（原文/转录）| 'content'（正文）| "
                "'digest'（摘要）| 'digest_cn'（中文摘要）| 'main_point'（要点）| 'question_answer'（问答）"
                "（映射：摘要 → digest / digest_cn，要点 → main_point，问答 → question_answer，原文 / 转录 → original_text；"
                "available_content_fields 中的 full_content / text / markdown 仅为长度信息，不能作为 content_field 请求）；"
                "各字段独立分页（params.offset/max_chars），params.mode 为 'auto'|'public'|'private'"
                "（不可与顶层 researchdata mode='fetch'|'plan' 混用）"
            ),
            "params.filter_type": (
                "list_filters 词典类型：'categories'（无参默认）| 'upstream_categories' | 'document_types'（可配 document_code）| "
                "'sources' | 'industries'（可配 document_code）| 'companies'（必填 params.query）| "
                "'regions' | 'watchlist_groups' | 'watchlist_stocks'（必填 params.group_id；"
                "注：companies / regions / watchlist_groups / watchlist_stocks 依赖上游公司 / 地区标签，可能漏召回或错配，不建议作为主要筛选）"
            ),
            "params.roadshow_id": "录制任务 ID（get_recording_task 必填，不可用 doc_id 代替）",
            "pagination_rules": (
                "三种分页机制互不通用：1) search_library 文档检索用 params.cursor（直到 has_more=false；"
                "newest 保留上游顺序，rank_date 是入库排序时间而非会议开始时间，日期筛选按 publish_time）；"
                "2) list_filters 词典用 params.offset / 返回 next_offset；"
                "3) list_recording_tasks 录制任务用 params.page / 返回 next_page（以 has_more 为准）；"
                "get_document 正文按 next_offset 续读，直到 complete=true 或 next_offset 为空（无 has_more）。"
                "旧参数名 pages 已废弃；search_library 禁用 page。"
            ),
        },
        "return_shape": (
            "search_library: {\"ok\": true, \"data\": {\"structuredContent\": {\"results\": [{\"doc_id\": str, \"title\": str, "
            "\"title_cn\": str, \"publish_time\": str, \"rank_date\": str, \"type_show_name\": str, "
            "\"institution_meeting_flag\": int|bool, \"personal_meeting_flag\": int|bool, ...}], "
            "\"cursor\": str|null, \"has_more\": bool, \"total_upstream\": int, \"filters\": {...}}}}; "
            "get_document: {\"ok\": true, \"data\": {\"structuredContent\": {\"metadata\": {\"doc_id\": str, ...}, \"content_field\": str, "
            "\"available_content_fields\": {\"<field>\": int}, \"warning\": str|null, \"content_chars\": int, "
            "\"content_sha256\": str|null, \"offset\": int, \"returned_chars\": int, \"text\": str, "
            "\"next_offset\": int|null, \"complete\": bool}}}（无顶层 doc_id，也无 has_more；按 next_offset 续读，直到 complete=true 或 next_offset 为空）; "
            "get_document_info: {\"ok\": true, \"data\": {\"structuredContent\": {\"doc_id\": str, ...metadata, \"note\": str}}}; "
            "list_attachments: {\"ok\": true, \"data\": {\"structuredContent\": {\"doc_id\": str, \"returned\": int, "
            "\"attachments\": [{\"attachment_index\": int, ..., \"downloadable_document\": bool}], \"next_step\": str}}}; "
            "list_recording_tasks: {\"ok\": true, \"data\": {\"structuredContent\": {\"meeting_scope\": str, \"total\": int, "
            "\"page\": int, \"limit\": int, \"returned\": int, \"results\": [...], \"has_more\": bool, \"next_page\": int|null, "
            "\"state_definitions\": {...}, \"note\": str}}}; "
            "get_recording_status: {\"ok\": true, \"data\": {\"structuredContent\": {\"meeting_scope\": str, \"counts\": {...}, "
            "\"preview\": [...], \"state_definitions\": {...}, \"note\": str}}}; "
            "get_recording_task: {\"ok\": true, \"data\": {\"structuredContent\": {\"meeting_scope\": str, \"roadshow_id\": str, \"detail\": {...}}}}"
        ),
        "examples": [
            "research(route=\"alphaengine\", operation=\"search_library\", params={\"meeting_scope\": \"institution\", \"sort\": \"newest\", \"limit\": 5})",
            "research(route=\"alphaengine\", operation=\"search_library\", query=\"宁德时代 300750\", start=\"2026-09-01\", end=\"2026-10-28\", limit=20, params={\"search_in\": \"title\", \"sort\": \"relevance\"})",
            "research(route=\"alphaengine\", operation=\"list_filters\", params={\"filter_type\": \"document_types\", \"document_code\": \"summary\"})",
            "research(route=\"alphaengine\", operation=\"get_document\", params={\"doc_id\": \"290001790954279_inst_134249\", \"content_field\": \"original_text\", \"offset\": 0, \"max_chars\": 30000})",
            "research(route=\"alphaengine\", operation=\"list_recording_tasks\", params={\"meeting_scope\": \"institution\", \"page\": 1, \"limit\": 100})",
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
            "query": "专家访谈检索词（1–1000 字符，英文研究问题优先，如 'Meta Reels advertising'）",
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数，合并后总长 <= 1000 字符）",
            "start": "窗口起点 YYYY-MM-DD（按 UTC 日期硬过滤；或放入 params.start_date）",
            "end": "窗口终点 YYYY-MM-DD（按 UTC 日期硬过滤；或放入 params.end_date）",
            "limit": "整数 1-30（省略时默认 3，最大 30；超出 30 会被桥接层截到 30）",
            "params.keywords": (
                "访谈级关键词过滤（1–10 个非空字符串列表，单个非空字符串自动包成单元素列表；"
                "多项之间 OR、单个词组内 AND，先筛访谈再排片段；不筛选时省略或传 null，空数组不转发，超过 10 项报错）"
            ),
            "params.recency_bias": (
                "近期偏好权重（1–100 的整数，数字字符串自动转 int；偏好近期一般从 30 起；"
                "只影响排序权重，不是日期过滤窗口；省略或传 null 表示不启用）"
            ),
        },
        "return_shape": (
            "{\"ok\": true, \"status\": \"ok\", \"data\": {\"structuredContent\": {\"data\": [...]}}, \"_sources\": [...]}"
            "（仅返回问答摘录，无全文接口、无 cursor 翻页；不能由 30 条摘录推断已扫完全部访谈）"
        ),
        "examples": [
            "research(route=\"guidepoint\", operation=\"search_library\", query=\"Meta advertising Reels\", start=\"2026-01-01\", end=\"2026-10-23\", limit=10)",
            "research(route=\"guidepoint\", operation=\"search_library\", query=\"GPU data center rack demand\", start=\"2026-07-01\", end=\"2026-10-23\", limit=20, params={\"keywords\": [\"NVIDIA\", \"NVDA\"], \"recency_bias\": 30})",
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
        "return_shape": (
            "{\"ok\": true, \"status\": \"ok\"|\"partial\"|\"empty\", \"data\": {\"source\": \"alphaengine\", "
            "\"transcripts\": [{\"doc_id\": ..., \"title\": ..., \"publish_time\": ..., \"kind\": \"company_call\"|\"broker_callback\", \"company_name\": ...}], "
            "\"coverage\": {\"complete\": bool, \"pages_scanned\": int, \"records_scanned\": int, \"warnings\": [...], \"continuation_params\": {...}}}, "
            "\"_sources\": [...]}（单次最多扫描 10 页×50 条；complete=false 时有候选则保留候选且 status=\"partial\"，零候选则仅表示本次有界检索未命中，"
            "续查参数 continuation_params 交给 route=\"alphaengine\", operation=\"search_library\"，不要传回 transcript；"
            "complete=true 且无匹配返回 ok: true, empty_reason: \"no_matching_transcripts\"）"
        ),
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
            "search": ["query（或 ticker）"],
            "read": ["document"],
            "list": [],
            "topic": ["query 或 params.tag"],
        },
        "optional_params": ["start", "end", "limit", "params"],
        "param_formats": {
            "query": "检索关键词（search 必填，或传 ticker 折叠进 query；只列公司目录请用 operation='list', params={'company': 'TICKER'}）",
            "params.company": "股票代码（如 'META'、'0700.HK'、'300750.SZ'；list 可仅传 params.company 列目录，search 仍须带 query）",
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数）",
            "document": "'wiki:<id>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}",
        "examples": [
            "research(route=\"company-wiki\", operation=\"list\", params={\"company\": \"META\"})",
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
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数）",
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
            "deprecation_note": "兼容旧名（已迁入内部库 document_v1 格式，可能返回 sales-notes 或 tmtb 记录），新调用请改用 sales-notes / tmtb / all",
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数）",
            "document": "'mail:<id>'、'digest:<date>' 或 'tmtb:<id>'",
            "start": "YYYY-MM-DD",
            "end": "YYYY-MM-DD",
        },
        "return_shape": "{\"ok\": true, \"status\": \"ok\", \"results\": [...], \"result_count\": int, \"_sources\": [...]}（兼容旧名，已转为内部库 document_v1 格式，建议改用 sales-notes / tmtb / all）",
        "examples": [
            'research(route="sales-mail", operation="search", query="META bogey", start="2026-01-01", end="2026-10-23", limit=10)',
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
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数）",
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
            "ticker": "股票代码；会并入 query 检索词（上游不支持单独的 ticker 参数）",
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


_DATE_PARAM_FIELDS: tuple[str, ...] = ("start", "end", "as_of", "vintage")


def _bridge_today() -> str:
    """Today as the upstream CLI checks it: this host's date or Beijing's, the later.

    AlphaEngine and the internal library stamp Beijing dates, a day ahead of
    this host's for part of every day.
    """
    import datetime as _dt
    from zoneinfo import ZoneInfo

    return max(_dt.date.today(), _dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()).isoformat()


def _invalid_date_error(field: str, value: Any, norm_view: dict[str, Any]) -> dict[str, Any]:
    return {
        "_declared_sources": [],
        "_sources": [],
        "ok": False,
        "status": "invalid_arguments",
        "error_code": "invalid_date",
        "error_category": "参数错误",
        "error": f"[参数错误] {field}={value!r} 不是可识别的日期；请用 YYYY-MM-DD。",
        "hint": f"把 {field} 改成 YYYY-MM-DD（例如 2026-09-01）后重试；月份、季度也可写成 2026-09、2026Q3。",
        "normalized_params": norm_view,
    }


def _normalize_date_param(field: str, value: Any) -> str | None:
    """Coerce a caller's date to the YYYY-MM-DD the upstream CLI parses.

    The CLI exits on any other shape before printing, which surfaced as an
    opaque "upstream unavailable"; a month, quarter or year widens to its first
    day for `start` and its last day otherwise. None when it is not a date.
    """
    import calendar
    import datetime as _dt

    text = str(value).strip()
    if not text:
        return None
    last = field != "start"

    def _day(y: int, m: int, d: int) -> str | None:
        try:
            return _dt.date(y, m, d).isoformat()
        except ValueError:
            return None

    def _span(y: int, m_first: int, m_last: int) -> str | None:
        if not (1 <= m_first <= 12 and 1 <= m_last <= 12) or not (1900 <= y <= 2100):
            return None
        if last:
            return _day(y, m_last, calendar.monthrange(y, m_last)[1])
        return _day(y, m_first, 1)

    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?", text)
    if m:
        return _day(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if m:
        return _day(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", text)
    if m:
        return _span(int(m[1]), int(m[2]), int(m[2]))
    m = re.fullmatch(r"(\d{4})\s*-?\s*[Qq]([1-4])|[Qq]([1-4])\s*-?\s*(\d{4})", text)
    if m:
        y, q = (int(m[1]), int(m[2])) if m[1] else (int(m[4]), int(m[3]))
        return _span(y, 3 * q - 2, 3 * q)
    m = re.fullmatch(r"(\d{4})", text)
    if m:
        return _span(int(m[1]), 1, 12)
    return None


def _normalize_point_date_param(value: Any) -> str | None:
    """Coerce a point-in-time parameter (`as_of`, `vintage`) to YYYY-MM-DD.

    A point has no span to widen: a month, quarter or year is refused (the
    caller must name the day), while a full date with a time suffix keeps its
    date part. None when the text is not a specific day.
    """
    import datetime as _dt

    text = str(value).strip()
    if not text:
        return None

    def _day(y: int, m: int, d: int) -> str | None:
        try:
            return _dt.date(y, m, d).isoformat()
        except ValueError:
            return None

    for pat in (
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T ].*)?",
        r"(\d{4})(\d{2})(\d{2})",
    ):
        m = re.fullmatch(pat, text)
        if m:
            return _day(int(m[1]), int(m[2]), int(m[3]))
    return None


def _point_date_error(field: str, value: Any, norm_view: dict[str, Any]) -> dict[str, Any]:
    return {
        "_declared_sources": [],
        "_sources": [],
        "ok": False,
        "status": "invalid_arguments",
        "error_code": "invalid_date",
        "error_category": "参数错误",
        "error": (
            f"[参数错误] {field}={value!r} 不是具体日期；as_of/vintage 是时点，"
            "请用 YYYY-MM-DD（月 / 季 / 年写法不支持）。"
        ),
        "hint": f"把 {field} 改成具体日期 YYYY-MM-DD（例如 2026-09-30）后重试。",
        "normalized_params": norm_view,
    }


def _route_all_leg_windows(
    normalized: dict[str, Any], nested: dict[str, Any]
) -> list[tuple[str, str, str | None, str | None]]:
    """The date window each `route='all'` fan-out leg actually carries.

    AlphaEngine always rides the top-level start/end. Guidepoint carries its
    nested params.start_date/end_date when either is present (the fan-out sends
    no other window on that leg), else the top-level window. The two legs are
    independent windows: judging them as one combined window both misses a
    reversed nested one and reads a legit split (top-level start for
    AlphaEngine, nested end_date for Guidepoint) as "start after end".
    """
    top_start, top_end = normalized.get("start"), normalized.get("end")
    gp_start = nested.get("start_date")
    gp_end = nested.get("end_date")
    if _non_empty_str(gp_start) or _non_empty_str(gp_end):
        return [
            ("alphaengine", "顶层 start/end", top_start, top_end),
            ("guidepoint", "params.start_date/end_date", gp_start, gp_end),
        ]
    return [
        ("alphaengine", "顶层 start/end", top_start, top_end),
        ("guidepoint", "顶层 start/end", top_start, top_end),
    ]


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_date_window(
    route: str,
    normalized: dict[str, Any],
    nested: dict[str, Any] | None = None,
    *,
    operation: str | None = None,
) -> dict[str, Any] | None:
    """Apply the shared date-window rules to a call in place; error payload or None.

    One definition for the bridge entry point and the `route='all'` fan-out
    legs, so both agree on today (`_bridge_today`, max of this host's date and
    Beijing's — a UTC date is a day behind part of every day and the gateway
    rejects `start > end`): an `end` after today stops at today, a `start`
    after today has nothing to find, AlphaEngine gets its window closed to a
    pair, and a reversed window is refused.
    `alphaengine` `list_recording_tasks` is exempt from historical-document
    date rules: it accepts future dates, requires a paired `date_from`/`date_to`
    when either is given, normalizes them into `params.date_from`/`params.date_to`,
    and strips top-level `start`/`end` so `data_gateway.py` does not reject future dates.
    """
    nested = nested if nested is not None else {}
    eff_op = (
        str(operation or normalized.get("operation") or "").strip()
        or str((ROUTE_CONTRACT_SPECS.get(route) or {}).get("default_operation") or "").strip()
    )

    if route == "alphaengine" and eff_op == "list_recording_tasks":
        top_start = normalized.get("start")
        top_end = normalized.get("end")
        nested_from = nested.get("date_from") or nested.get("start") or nested.get("start_date")
        nested_to = nested.get("date_to") or nested.get("end") or nested.get("end_date")
        if top_start and nested_from and top_start != nested_from:
            norm_view = _build_normalized_params_view(normalized)
            return {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "parameter_conflict",
                "error_category": "参数错误",
                "error": f"[参数错误] 参数冲突：start={top_start!r} 与 params.date_from={nested_from!r} 不一致。",
                "hint": "请统一使用 params.date_from 与 params.date_to 传入成对日期。",
                "normalized_params": norm_view,
            }
        if top_end and nested_to and top_end != nested_to:
            norm_view = _build_normalized_params_view(normalized)
            return {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "parameter_conflict",
                "error_category": "参数错误",
                "error": f"[参数错误] 参数冲突：end={top_end!r} 与 params.date_to={nested_to!r} 不一致。",
                "hint": "请统一使用 params.date_from 与 params.date_to 传入成对日期。",
                "normalized_params": norm_view,
            }
        task_from = nested_from or top_start
        task_to = nested_to or top_end
        if (task_from is None) != (task_to is None):
            norm_view = _build_normalized_params_view(normalized)
            return {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "E_BAD_DATE_RANGE",
                "error_category": "参数错误",
                "error": (
                    "[参数错误] E_BAD_DATE_RANGE: list_recording_tasks 的 date_from 和 date_to 必须成对提供"
                    "（录制任务包含未来预约，不按历史文档规则自动补齐单边日期）。"
                ),
                "hint": "请同时提供成对的 params.date_from 与 params.date_to（YYYY-MM-DD），或都不传。",
                "normalized_params": norm_view,
            }
        if task_from and task_to:
            if task_from > task_to:
                norm_view = _build_normalized_params_view(normalized)
                err = _invalid_date_error("date_from", task_from, norm_view)
                err["error"] = f"[参数错误] 开始日期 {task_from} 晚于结束日期 {task_to}。"
                err["hint"] = "把 date_from 改成不晚于 date_to 的成对日期后重试。"
                return err
            p_out = dict(nested)
            for alias_k in ("start", "start_date", "end", "end_date"):
                p_out.pop(alias_k, None)
            p_out["date_from"] = task_from
            p_out["date_to"] = task_to
            normalized["params"] = p_out
        normalized.pop("start", None)
        normalized.pop("end", None)
        return None

    today = _bridge_today()

    if isinstance(normalized.get("end"), str) and normalized["end"] > today:
        normalized["end"] = today
    for start_k, start_v in (("start", normalized.get("start")),) + tuple(
        (f"params.{k}", nested.get(k)) for k in ("start", "date_from", "start_date")
    ):
        if isinstance(start_v, str) and start_v > today:
            norm_view = _build_normalized_params_view(normalized)
            err = _invalid_date_error(start_k, start_v, norm_view)
            err["error"] = f"[参数错误] {start_k}={start_v!r} 晚于今天（{today}），没有可查的数据。"
            return err

    # route='all' fans out to legs that carry independent date windows (see
    # _route_all_leg_windows), so each leg is judged on its own window — never
    # on the two spliced together.
    if route == "all":
        for leg, which, leg_start, leg_end in _route_all_leg_windows(normalized, nested):
            if _non_empty_str(leg_start) and _non_empty_str(leg_end) and leg_start > leg_end:
                norm_view = _build_normalized_params_view(normalized)
                err = _invalid_date_error("start", leg_start, norm_view)
                err["error"] = (
                    f"[参数错误] route='all' 的 {leg} 腿日期窗口无效：{which} 中 "
                    f"开始日期 {leg_start} 晚于结束日期 {leg_end}。"
                )
                err["hint"] = f"把 {which} 改成开始不晚于结束的成对日期后重试。"
                return err
        return None

    win_start = normalized.get("start") or nested.get("start") or nested.get("date_from") or nested.get("start_date")
    win_end = normalized.get("end") or nested.get("end") or nested.get("date_to") or nested.get("end_date")
    # AlphaEngine takes a date window only as a pair; one end alone is refused
    # upstream, so close it: up to today, or a year back.
    if route == "alphaengine" and (win_start is None) != (win_end is None):
        import datetime as _dt

        if win_end is None:
            win_end = normalized["end"] = max(today, win_start)
        else:
            end_day = _dt.date.fromisoformat(win_end)
            win_start = normalized["start"] = (end_day - _dt.timedelta(days=365)).isoformat()
    if win_start and win_end and win_start > win_end:
        norm_view = _build_normalized_params_view(normalized)
        err = _invalid_date_error("start", win_start, norm_view)
        err["error"] = f"[参数错误] 开始日期 {win_start} 晚于结束日期 {win_end}。"
        err["hint"] = "把 start 改成不晚于 end 的日期后重试。"
        return err
    return None


def _normalize_guidepoint_params(
    params_obj: dict[str, Any], norm_view: dict[str, Any]
) -> dict[str, Any] | None:
    """Normalize and validate Guidepoint nested `params` (`keywords`, `recency_bias`) in place.

    Returns an error payload dict on invalid values, or None when valid.
    - `recency_bias`: `None` or empty/whitespace string is omitted; `int` or integer string in 1..100
      is normalized to `int`; any non-integer or out-of-range value returns `[参数错误]`.
    - `keywords`: `None`, empty list `[]`, or whitespace-only items are omitted; a bare non-empty string
      is wrapped as `[s]`; a list of 1..10 non-empty strings is kept; >10 non-empty strings or non-string
      items return `[参数错误]` (never silently truncated).
    """
    if "recency_bias" in params_obj:
        rb_raw = params_obj.get("recency_bias")
        if rb_raw is None or (isinstance(rb_raw, str) and not rb_raw.strip()):
            params_obj.pop("recency_bias", None)
        else:
            rb_int = _parse_int_param(rb_raw)
            if rb_int is None or not (1 <= rb_int <= 100):
                return {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_nested_param_value",
                    "error_category": "参数错误",
                    "error": (
                        f"[参数错误] Guidepoint params.recency_bias={rb_raw!r} 无效："
                        "recency_bias 须为 1–100 的整数或 null（不启用时请省略或传 null）。"
                    ),
                    "hint": "请将 params.recency_bias 改为 1–100 的整数（偏好近期一般从 30 起），或省略该参数。",
                    "normalized_params": norm_view,
                }
            params_obj["recency_bias"] = rb_int

    if "keywords" in params_obj:
        kw_raw = params_obj.get("keywords")
        if kw_raw is None:
            params_obj.pop("keywords", None)
        elif isinstance(kw_raw, str):
            s = kw_raw.strip()
            if not s:
                params_obj.pop("keywords", None)
            else:
                params_obj["keywords"] = [s]
        elif isinstance(kw_raw, list):
            if not all(isinstance(item, str) for item in kw_raw):
                return {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_nested_param_value",
                    "error_category": "参数错误",
                    "error": (
                        f"[参数错误] Guidepoint params.keywords={kw_raw!r} 无效："
                        "Guidepoint keywords 须为 1–10 个非空字符串，不筛选时请省略或传 null。"
                    ),
                    "hint": "请确保 params.keywords 为字符串列表（1–10 个非空字符串），或省略该参数。",
                    "normalized_params": norm_view,
                }
            cleaned_kw = [item.strip() for item in kw_raw if item.strip()]
            if not cleaned_kw:
                params_obj.pop("keywords", None)
            elif len(cleaned_kw) > 10:
                return {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_nested_param_value",
                    "error_category": "参数错误",
                    "error": (
                        f"[参数错误] Guidepoint keywords 须为 1–10 个非空字符串（当前传入了 {len(cleaned_kw)} 个），"
                        "不筛选时请省略或传 null。"
                    ),
                    "hint": "请将 params.keywords 精简到 1–10 个非空字符串（不做静默截断）。",
                    "normalized_params": norm_view,
                }
            else:
                params_obj["keywords"] = cleaned_kw
        else:
            return {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "invalid_nested_param_value",
                "error_category": "参数错误",
                "error": (
                    f"[参数错误] Guidepoint params.keywords={kw_raw!r} 无效："
                    "Guidepoint keywords 须为 1–10 个非空字符串，不筛选时请省略或传 null。"
                ),
                "hint": "请将 params.keywords 传为 1–10 个非空字符串列表（或单个非空字符串），或省略该参数。",
                "normalized_params": norm_view,
            }
    return None


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
    elif route == "alphaengine":
        # Do not silently alias top-level `page` to `pages` on alphaengine.
        eff_alias.pop("page", None)
    elif route == "sec":
        # On SEC, top-level `document` is the attachment filename (--document)
        # while `accession` is the filing accession number (`params.accession`).
        eff_alias.pop("accession", None)
        eff_alias.pop("accession_number", None)

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
    eff_op = op_str or str((spec or {}).get("default_operation") or "").strip()

    if isinstance(normalized.get("ticker"), str):
        normalized["ticker"] = _normalize_ticker_for_route(route, normalized["ticker"])

    # The gateway accepts query/start/end only on these routes (the library
    # ones add `document`) and refuses a separate `--ticker`, so fold it into
    # the query the same way the route='all' fan-out legs do.
    if route in TICKER_FOLDS_INTO_QUERY_ROUTES:
        folded_ticker = str(normalized.pop("ticker", "") or "").strip()
        if folded_ticker:
            folded_query = str(normalized.get("query") or "").strip()
            if not _query_contains_ticker(folded_query, folded_ticker):
                normalized["query"] = (
                    f"{folded_ticker} {folded_query}".strip() if folded_query else folded_ticker
                )
            else:
                normalized["query"] = folded_query

    if not is_comm:
        for date_k in _DATE_PARAM_FIELDS:
            raw_date = normalized.get(date_k)
            if raw_date is None:
                continue
            if date_k in ("as_of", "vintage"):
                # A point in time is not a window end: upstream has no "up to
                # now" for it, so a future date or a month/quarter/year is the
                # caller's mistake, not something to truncate.
                coerced = _normalize_point_date_param(raw_date)
                if coerced is None:
                    norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                    return normalized, norm_view, _point_date_error(date_k, raw_date, norm_view)
                if coerced > _bridge_today():
                    norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                    err = _point_date_error(date_k, raw_date, norm_view)
                    err["error"] = (
                        f"[参数错误] {date_k}={raw_date!r} 晚于今天（{_bridge_today()}），没有可查的数据。"
                    )
                    return normalized, norm_view, err
            else:
                coerced = _normalize_date_param(date_k, raw_date)
                if coerced is None:
                    norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                    return normalized, norm_view, _invalid_date_error(date_k, raw_date, norm_view)
            normalized[date_k] = coerced
        params_obj = normalized.get("params")
        is_ae_recording_tasks = route == "alphaengine" and eff_op == "list_recording_tasks"
        if isinstance(params_obj, dict):
            params_obj = dict(params_obj)
            # Guidepoint spells its nested window start_date/end_date; the
            # same coercion and end truncation as the top-level start/end
            # (except list_recording_tasks, which preserves future dates).
            for nested_k in ("start", "date_from", "start_date", "end", "date_to", "end_date"):
                if params_obj.get(nested_k) is None:
                    continue
                as_field = "start" if nested_k in ("start", "date_from", "start_date") else "end"
                coerced = _normalize_date_param(as_field, params_obj[nested_k])
                if coerced is None:
                    norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                    return normalized, norm_view, _invalid_date_error(f"params.{nested_k}", params_obj[nested_k], norm_view)
                params_obj[nested_k] = (
                    coerced
                    if (is_ae_recording_tasks or as_field != "end")
                    else min(coerced, _bridge_today())
                )
            if route in ("guidepoint", "all"):
                gp_norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
                gp_param_err = _normalize_guidepoint_params(params_obj, gp_norm_view)
                if gp_param_err is not None:
                    return normalized, gp_norm_view, gp_param_err
            normalized["params"] = params_obj
        window_err = _normalize_date_window(
            route,
            normalized,
            params_obj if isinstance(params_obj, dict) else None,
            operation=eff_op,
        )
        if window_err is not None:
            return normalized, window_err["normalized_params"], window_err

    # Validate nested `params` conflicts, top-level vs nested document conflicts, and alphaengine doc_id mapping
    if isinstance(normalized.get("params"), dict):
        p_dict = dict(normalized["params"])
        for group_label, group_keys in _NESTED_ALIAS_CONFLICT_GROUPS:
            present = [(k, p_dict[k]) for k in group_keys if k in p_dict and p_dict[k] is not None]
            if len(present) > 1:
                if route == "alphaengine" and group_label == "document":
                    first_doc = _normalize_alphaengine_doc_id(present[0][1])
                    has_conflict = not all(
                        _normalize_alphaengine_doc_id(v) == first_doc for _, v in present[1:]
                    )
                else:
                    has_conflict = not all(
                        _values_equal_for_alias(v, present[0][1]) for _, v in present[1:]
                    )
                if has_conflict:
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
            top_doc_str = str(top_doc) if route == "alphaengine" else str(top_doc).strip()
            for doc_k in ("document", "doc_id", "id", "document_id", "doc", "file_id"):
                if doc_k in p_dict and p_dict[doc_k] is not None and str(p_dict[doc_k]).strip():
                    nested_doc_str = str(p_dict[doc_k]) if route == "alphaengine" else str(p_dict[doc_k]).strip()
                    same_doc = (
                        _normalize_alphaengine_doc_id(nested_doc_str) == _normalize_alphaengine_doc_id(top_doc_str)
                        if route == "alphaengine"
                        else _values_equal_for_alias(nested_doc_str, top_doc_str)
                    )
                    if not same_doc:
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
                    if alias_val is not None and str(alias_val) != "" and not p_dict.get("doc_id"):
                        p_dict["doc_id"] = str(alias_val)
            if top_doc is not None and str(top_doc) != "":
                if not p_dict.get("doc_id"):
                    p_dict["doc_id"] = str(top_doc)
                normalized.pop("document", None)
            if p_dict.get("doc_id") is not None and str(p_dict.get("doc_id")) != "":
                p_dict["doc_id"] = _normalize_alphaengine_doc_id(p_dict["doc_id"])
            normalized["params"] = p_dict
    elif route == "alphaengine" and normalized.get("document") is not None:
        top_doc_str = _normalize_alphaengine_doc_id(normalized.pop("document"))
        if top_doc_str != "":
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
            extra_op_note = (
                "（download_document 为独立原生工具，local_path 属宿主工作区路径、沙箱不可读，不通过桥接提供；"
                "读取正文请用 get_document，查补充附件列表请用 list_attachments）"
                if route == "alphaengine" and op_str == "download_document"
                else ""
            )
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
                    f"[参数错误] 路由 '{route}' 不支持 operation='{op_str}'{extra_op_note}。"
                    f"支持的操作: {valid_ops}；接受的参数: {accepted_top}。"
                ),
                "hint": (
                    f"请将 operation 改为 {valid_ops} 之一{extra_op_note}（可调用 catalog(route='{route}') 查看完整参数说明与示例）。"
                ),
                "normalized_params": norm_view,
            }
            return normalized, norm_view, err

        allowed_set = set(accepted_top) | {"timeout", "mode"}
        unknown_top = sorted(
            k for k, v in normalized.items() if v is not None and k not in allowed_set
        )
        if unknown_top:
            ae_top_replacements: list[str] = []
            if route == "alphaengine":
                if "companies" in unknown_top:
                    ae_top_replacements.append("companies -> params.company_names")
                if "industries" in unknown_top:
                    ae_top_replacements.append("industries -> params.industry_names")
                if "pages" in unknown_top or "page" in unknown_top:
                    ae_top_replacements.append(
                        "page/pages -> params.cursor（search_library 文档检索）或 params.page（list_recording_tasks）"
                    )
            elif route == "sec":
                if "accession" in unknown_top or "accession_number" in unknown_top:
                    ae_top_replacements.append(
                        "accession -> params.accession（仅 operation='attachments'）"
                    )
            repl_suffix = f" 请改用新参数名：{'; '.join(ae_top_replacements)}。" if ae_top_replacements else ""
            err = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "unknown_parameters",
                "error_category": "参数错误",
                "accepted_parameters": accepted_top,
                "error": (
                    f"[参数错误] 路由 '{route}'（operation='{eff_op}'）"
                    f"收到不认识的参数: {unknown_top}。{repl_suffix}该操作接受的参数: {accepted_top}。"
                ),
                "hint": (
                    f"请移除或修正未知参数 {unknown_top}，{repl_suffix}仅使用 {accepted_top} 中的参数"
                    f"（不确定参数时先查 catalog(route='{route}')）。"
                ),
                "normalized_params": norm_view,
            }
            return normalized, norm_view, err

        # Validate unknown nested keys inside `params` (per-operation for alphaengine and sec)
        if isinstance(normalized.get("params"), dict) and "params" in allowed_set:
            if route == "alphaengine":
                accepted_nested = list(
                    ALPHAENGINE_OPERATION_NESTED_PARAMS.get(
                        eff_op, list(spec.get("accepted_nested_params") or [])
                    )
                )
            elif route == "sec":
                accepted_nested = list(
                    SEC_OPERATION_NESTED_PARAMS.get(
                        eff_op, list(spec.get("accepted_nested_params") or [])
                    )
                )
            else:
                accepted_nested = list(spec.get("accepted_nested_params") or [])
            allowed_nested_set = set(accepted_nested)
            if route in INTERNAL_LIBRARY_ROUTES:
                allowed_nested_set |= {"document", "doc_id", "id", "document_id"}
            unknown_nested = sorted(
                k
                for k, v in normalized["params"].items()
                if v is not None and k not in allowed_nested_set
            )
            if unknown_nested:
                ae_nested_replacements: list[str] = []
                if route == "alphaengine":
                    if "companies" in unknown_nested:
                        ae_nested_replacements.append("'companies' 已废弃，请改用 'company_names'")
                    if "industries" in unknown_nested:
                        ae_nested_replacements.append(
                            "'industries' 已废弃，请改用 'industry_names'（list_recording_tasks 用 'industry_ids'）"
                        )
                    if "pages" in unknown_nested:
                        ae_nested_replacements.append(
                            "'pages' 已废弃，search_library 请改用 'cursor'（list_recording_tasks 用 'page'，list_filters 用 'offset'）"
                        )
                    if "page" in unknown_nested:
                        ae_nested_replacements.append(
                            f"operation='{eff_op}' 不支持 'page'，文档检索请改用 'cursor'（'page' 仅用于 list_recording_tasks）"
                        )
                    if "doc_id" in unknown_nested and eff_op == "get_recording_task":
                        ae_nested_replacements.append(
                            "get_recording_task 不接受 'doc_id'，请改用 'roadshow_id'"
                        )
                repl_text = f" 替代说明：{'; '.join(ae_nested_replacements)}。" if ae_nested_replacements else ""
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
                        f"[参数错误] 路由 '{route}'（operation='{eff_op}'）的 params 收到未知嵌套参数: {unknown_nested}。"
                        f"{repl_text}该操作 params 接受的字段: {accepted_nested}。"
                    ),
                    "hint": (
                        f"请从 params 中移除或替换未知字段 {unknown_nested}。{repl_text}"
                        f"仅使用 {accepted_nested}（不确定时先查 catalog(route='{route}')）。"
                    ),
                    "normalized_params": norm_view,
                }
                return normalized, norm_view, err

        # Validate required parameters and operation-specific rules aligned with upstream
        eff_op = op_str or str(spec.get("default_operation") or "").strip()
        if route == "cn-hk" and eff_op == "ask":
            # researchdata 1.5 refuses ticker/start/end beside an `ask` (the
            # question carries them); fold them into the question so older
            # callers and examples keep working instead of failing upstream.
            folded = [
                str(normalized.pop(k) or "").strip() for k in ("ticker", "start", "end")
                if k in normalized
            ]
            folded = [v for v in folded if v]
            if folded:
                base_q = str(normalized.get("query") or "").strip()
                # Whole-token match: "00700" inside "300700" is another company.
                extra = " ".join(v for v in folded if not _query_contains_ticker(base_q, v))
                normalized["query"] = f"{extra} {base_q}".strip() if extra else base_q
        cn_hk_refused: list[str] = []
        if route == "cn-hk" and eff_op == "filings" and str(normalized.get("query") or "").strip():
            cn_hk_refused = ["query"]
        elif route == "cn-hk" and eff_op == "quote":
            cn_hk_refused = [
                k for k in ("query", "start", "end") if str(normalized.get(k) or "").strip()
            ]
        if cn_hk_refused:
            return normalized, norm_view, {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "invalid_arguments",
                "error_code": "unsupported_parameters",
                "error_category": "参数错误",
                "unsupported_parameters": cn_hk_refused,
                "error": (
                    f"[参数错误] cn-hk operation='{eff_op}' 不接受 {cn_hk_refused}："
                    "filings 用 ticker/start/end；quote 只用 ticker（当前报价，没有历史日期）；"
                    "自然语言问题用 operation='ask'，公司和日期写进 query。"
                ),
                "normalized_params": norm_view,
            }
        missing_req: list[str] = []
        if route == "alphaengine":
            p_cur = dict(normalized.get("params")) if isinstance(normalized.get("params"), dict) else {}
            if eff_op not in ("search_library", "list_recording_tasks"):
                bad_top_for_op = [
                    k for k in ("query", "start", "end") if str(normalized.get(k) or "").strip()
                ]
                if bad_top_for_op:
                    err = {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "invalid_arguments",
                        "error_code": "E_BAD_ARGS",
                        "error_category": "参数错误",
                        "error": (
                            f"[参数错误] E_BAD_ARGS: alphaengine 的 operation='{eff_op}' 不接受顶层 {bad_top_for_op}，"
                            "请将对应字段放入 params（例如 list_filters 必须使用 params.query）。"
                        ),
                        "hint": f"请移除顶层 {bad_top_for_op}，改用 params 内字段（如 params.query）后重试。",
                        "normalized_params": norm_view,
                    }
                    return normalized, norm_view, err
            else:
                top_q = str(normalized.get("query") or "").strip()
                nested_q = str(p_cur.get("query") or "").strip()
                if top_q and nested_q and not _values_equal_for_alias(top_q, nested_q):
                    err = {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "invalid_arguments",
                        "error_code": "parameter_conflict",
                        "error_category": "参数错误",
                        "error": f"[参数错误] 参数冲突：顶层 query={top_q!r} 与 params.query={nested_q!r} 不一致。",
                        "hint": "请统一只传顶层 query 或 params.query 之一。",
                        "normalized_params": norm_view,
                    }
                    return normalized, norm_view, err

            if eff_op in ("get_document", "get_document_info", "list_attachments"):
                raw_doc_id_val = p_cur.get("doc_id")
                doc_id_val = "" if raw_doc_id_val is None else str(raw_doc_id_val)
                if doc_id_val == "":
                    missing_req.append("params.doc_id")
                elif not _is_valid_alphaengine_doc_id(doc_id_val):
                    err = {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "invalid_arguments",
                        "error_code": "invalid_doc_id",
                        "error_category": "参数错误",
                        "error": (
                            f"[参数错误] params.doc_id={doc_id_val!r} 格式非法：AlphaEngine 文档 ID 是不透明字符串"
                            "（仅允许字母、数字、下划线与连字符，如 '290001790954279_inst_134249'），拒绝路径、点号、百分号、空白或换行。"
                        ),
                        "hint": "请传入 search_library 返回的原始 doc_id（或单层 'alphaengine:<doc_id>'），不要包含 '/'、'.'、'%' 或首尾空白/换行。",
                        "normalized_params": norm_view,
                    }
                    return normalized, norm_view, err
                if eff_op == "get_document" and "content_field" in p_cur and p_cur["content_field"] is not None:
                    cf_val = p_cur["content_field"]
                    if not isinstance(cf_val, str) or cf_val not in _ALPHAENGINE_ALLOWED_CONTENT_FIELDS:
                        allowed_cf_list = list(_ALPHAENGINE_ALLOWED_CONTENT_FIELDS)
                        err = {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "invalid_arguments",
                            "error_code": "E_BAD_CONTENT_FIELD",
                            "error_category": "参数错误",
                            "accepted_content_fields": allowed_cf_list,
                            "error": (
                                f"[参数错误] E_BAD_CONTENT_FIELD: params.content_field={cf_val!r} 不合法；"
                                f"仅支持 {allowed_cf_list}（默认 'auto'；映射：摘要 → 'digest'/'digest_cn'，"
                                "要点 → 'main_point'，问答 → 'question_answer'，原文/转录 → 'original_text'；"
                                "available_content_fields 中的 full_content/text/markdown 仅为长度信息，不能作为 content_field 请求）。"
                            ),
                            "hint": (
                                f"请将 params.content_field 改为 {allowed_cf_list} 之一"
                                "（摘要用 'digest'/'digest_cn'，要点用 'main_point'，问答用 'question_answer'，原文/转录用 'original_text'）。"
                            ),
                            "normalized_params": norm_view,
                        }
                        return normalized, norm_view, err
            elif eff_op == "get_recording_task":
                if not str(p_cur.get("roadshow_id") or "").strip():
                    missing_req.append("params.roadshow_id")
            elif eff_op == "list_filters":
                ft_val = str(p_cur.get("filter_type") or "categories").strip()
                if ft_val == "companies" and not str(p_cur.get("query") or "").strip():
                    missing_req.append("params.query")
                elif ft_val == "watchlist_stocks" and (
                    p_cur.get("group_id") is None or str(p_cur.get("group_id")).strip() == ""
                ):
                    missing_req.append("params.group_id")
            elif eff_op == "search_library":
                effective_query = str(
                    normalized.get("query") or p_cur.get("query") or ""
                ).strip()
                raw_sort = p_cur.get("sort")
                if raw_sort is not None and str(raw_sort).strip() != "":
                    sort_val = str(raw_sort).strip()
                    if sort_val not in ("relevance", "newest"):
                        err = {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "invalid_arguments",
                            "error_code": "E_BAD_SORT",
                            "error_category": "参数错误",
                            "error": f"[参数错误] params.sort={raw_sort!r} 非法，仅支持 'relevance' 或 'newest'。",
                            "hint": "有关键词检索请传 sort='relevance'；无关键词浏览请传 sort='newest'（或 'relevance'）。",
                            "normalized_params": norm_view,
                        }
                        return normalized, norm_view, err
                    if effective_query and sort_val != "relevance":
                        err = {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "invalid_arguments",
                            "error_code": "E_QUERY_SORT",
                            "error_category": "参数错误",
                            "error": (
                                f"[参数错误] E_QUERY_SORT: 有有效关键词（query={effective_query!r}）时仅允许 sort='relevance'，"
                                "不支持 sort='newest'；无关键词浏览才可使用 sort='newest'。"
                            ),
                            "hint": "请将 params.sort 改为 'relevance'（或省略 sort），或去掉 query/ticker 进行无关键词最新浏览。",
                            "normalized_params": norm_view,
                        }
                        return normalized, norm_view, err
                    p_cur["sort"] = sort_val
                else:
                    p_cur["sort"] = "relevance" if effective_query else "newest"
                normalized["params"] = p_cur
                norm_view = _build_normalized_params_view(normalized, is_community=is_comm)
        elif route == "fred":
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
            elif eff_op == "quote":
                if not str(normalized.get("ticker") or "").strip():
                    missing_req.append("ticker")
            elif eff_op == "ask":
                if not str(normalized.get("query") or "").strip():
                    missing_req.append("query")
        elif route == "sec":
            sec_req_map = (spec or {}).get("required_params") or {}
            for req_k in sec_req_map.get(eff_op, ["ticker"]):
                if not str(normalized.get(req_k) or "").strip():
                    missing_req.append(req_k)

            sec_op_flags: dict[str, set[str]] = {
                "query": {"smart-facts", "search-concepts", "search-text"},
                "concept": {"facts"},
                "item": {"read-item"},
                "document": {"attachments"},
            }
            for flag_k, flag_ops in sec_op_flags.items():
                if str(normalized.get(flag_k) or "").strip() and eff_op not in flag_ops:
                    return normalized, norm_view, {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "invalid_arguments",
                        "error_code": "unsupported_parameters",
                        "error_category": "参数错误",
                        "unsupported_parameters": [flag_k],
                        "error": (
                            f"[参数错误] SEC {eff_op} 不接受 --{flag_k.replace('_', '-')}"
                            f"（参数 '{flag_k}' 仅用于 {sorted(flag_ops)}）。"
                        ),
                        "hint": f"请从 SEC {eff_op} 调用中移除参数 '{flag_k}'，或切换为支持该参数的操作 {sorted(flag_ops)}。",
                        "normalized_params": norm_view,
                    }

            p_sec = normalized.get("params")
            if isinstance(p_sec, dict):
                for sec_pk, sec_pv in sorted(p_sec.items()):
                    if sec_pv is None:
                        continue
                    if sec_pk in ("max_chars", "window", "max_matches"):
                        sec_lim = 100 if sec_pk == "max_matches" else SEC_MAX_CHARS_CEILING
                        if type(sec_pv) is not int or not (1 <= sec_pv <= sec_lim):
                            return normalized, norm_view, {
                                "_declared_sources": [],
                                "_sources": [],
                                "ok": False,
                                "status": "invalid_arguments",
                                "error_code": "invalid_nested_param_value",
                                "error_category": "参数错误",
                                "error": (
                                    f"[参数错误] SEC {sec_pk} 须为范围内的正整数（1–{sec_lim}，当前为 {sec_pv!r}）。"
                                ),
                                "hint": f"请将 params.{sec_pk} 设为 1–{sec_lim} 之间的正整数。",
                                "normalized_params": norm_view,
                            }
                    elif sec_pk == "accession":
                        if type(sec_pv) is not str or not _SEC_ACCESSION_RE.fullmatch(sec_pv):
                            return normalized, norm_view, {
                                "_declared_sources": [],
                                "_sources": [],
                                "ok": False,
                                "status": "invalid_arguments",
                                "error_code": "invalid_nested_param_value",
                                "error_category": "参数错误",
                                "error": (
                                    f"[参数错误] SEC accession 须为 SEC 完整编号（10-2-6 数字，如 '0001326801-26-000010'，当前为 {sec_pv!r}）。"
                                ),
                                "hint": "请传入格式为 NNNNNNNNNN-NN-NNNNNN 的 SEC accession 编号。",
                                "normalized_params": norm_view,
                            }
                    elif sec_pk == "document_type":
                        if type(sec_pv) is not str or not sec_pv.strip():
                            return normalized, norm_view, {
                                "_declared_sources": [],
                                "_sources": [],
                                "ok": False,
                                "status": "invalid_arguments",
                                "error_code": "invalid_nested_param_value",
                                "error_category": "参数错误",
                                "error": f"[参数错误] SEC document_type 须为非空字符串（当前为 {sec_pv!r}）。",
                                "hint": "请传入非空附件类型字符串（如 'EX-99.1'）。",
                                "normalized_params": norm_view,
                            }
                    elif sec_pk == "statement":
                        if type(sec_pv) is not str or sec_pv not in SEC_VALID_STATEMENTS:
                            return normalized, norm_view, {
                                "_declared_sources": [],
                                "_sources": [],
                                "ok": False,
                                "status": "invalid_arguments",
                                "error_code": "invalid_nested_param_value",
                                "error_category": "参数错误",
                                "error": f"[参数错误] SEC statement 须为 all|income|balance|cash（当前为 {sec_pv!r}）。",
                                "hint": "请将 params.statement 设为 'all'、'income'、'balance' 或 'cash'。",
                                "normalized_params": norm_view,
                            }
                if (
                    str(normalized.get("document") or "").strip()
                    and isinstance(p_sec.get("document_type"), str)
                    and p_sec["document_type"].strip()
                ):
                    return normalized, norm_view, {
                        "_declared_sources": [],
                        "_sources": [],
                        "ok": False,
                        "status": "invalid_arguments",
                        "error_code": "parameter_conflict",
                        "error_category": "参数错误",
                        "error": "[参数错误] SEC attachments 的 document 与 document_type 不能同时使用。",
                        "hint": "请只传顶层 document（指定附件文件名）或 params.document_type（指定附件类型如 'EX-99.1'）之一。",
                        "normalized_params": norm_view,
                    }
        elif route == "estimates":
            if not str(normalized.get("ticker") or "").strip():
                missing_req.append("ticker")
            p_est = normalized.get("params")
            if isinstance(p_est, dict):
                if "shares_bn" in p_est and p_est["shares_bn"] is not None:
                    sb_val = p_est["shares_bn"]
                    if type(sb_val) not in (int, float) or not math.isfinite(sb_val) or sb_val <= 0:
                        return normalized, norm_view, {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "invalid_arguments",
                            "error_code": "invalid_nested_param_value",
                            "error_category": "参数错误",
                            "error": (
                                f"[参数错误] estimates params.shares_bn={sb_val!r} 无效："
                                "shares_bn 须为正数（> 0 的有限数值，不接受布尔值）。"
                            ),
                            "hint": "请将 params.shares_bn 设为大于 0 的有限数值（十亿股）。",
                            "normalized_params": norm_view,
                        }
                if "tax" in p_est and p_est["tax"] is not None:
                    tax_val = p_est["tax"]
                    if (
                        type(tax_val) not in (int, float)
                        or not math.isfinite(tax_val)
                        or not (0 <= tax_val < 1)
                    ):
                        return normalized, norm_view, {
                            "_declared_sources": [],
                            "_sources": [],
                            "ok": False,
                            "status": "invalid_arguments",
                            "error_code": "invalid_nested_param_value",
                            "error_category": "参数错误",
                            "error": (
                                f"[参数错误] estimates params.tax={tax_val!r} 无效："
                                "股数须为正数，税率须在 0–1 之间（0 <= tax < 1 的有限数值，不接受布尔值）。"
                            ),
                            "hint": "请将 params.tax 设为 0 <= tax < 1 的有限数值（如 0.16）。",
                            "normalized_params": norm_view,
                        }
        elif route == "transcript":
            if not str(normalized.get("ticker") or "").strip():
                missing_req.append("ticker")
        elif route == "guidepoint":
            p_gp = normalized.get("params") if isinstance(normalized.get("params"), dict) else {}
            eff_gp_q = str(normalized.get("query") or p_gp.get("query") or "").strip()
            if not eff_gp_q:
                missing_req.append("query")
            elif len(eff_gp_q) > 1000:
                return normalized, norm_view, {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "invalid_arguments",
                    "error_code": "invalid_param_value",
                    "error_category": "参数错误",
                    "error": (
                        f"[参数错误] Guidepoint search_library 需要 1–1000 字符的 query"
                        f"（合并 ticker 后当前长度为 {len(eff_gp_q)} 字符）。"
                    ),
                    "hint": "请将 query 精简到 1000 字符以内后重试。",
                    "normalized_params": norm_view,
                }
        elif route == "company-wiki":
            p_wiki = normalized.get("params") if isinstance(normalized.get("params"), dict) else {}
            has_wiki_q = bool(str(normalized.get("query") or "").strip())
            if (eff_op == "search" and not has_wiki_q) or (
                eff_op == "topic" and not (has_wiki_q or str(p_wiki.get("tag") or "").strip())
            ):
                missing_req.append("query")

        if missing_req:
            wiki_list_note = (
                "（company-wiki 的 search 必须带 query；若只需按公司列目录，请改用 operation='list', params={'company': ...}）"
                if route == "company-wiki" and eff_op == "search" and "query" in missing_req
                else ""
            )
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
                    f"[参数错误] 路由 '{route}'（operation='{eff_op}'）缺少必填参数: {missing_req}{wiki_list_note}。"
                    f"该操作接受的参数: {accepted_top}。"
                ),
                "hint": (
                    f"请补齐必填参数 {missing_req}{wiki_list_note} 后重试（可调用 catalog(route='{route}') 查看完整参数格式与示例）。"
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
    if category == "没有匹配" and any(m in h_lower for m in ("暂不可用", "执行超时")):
        return True
    if category == "权限或额度" and any(m in h_lower for m in ("参数错误", "未知参数")):
        return True
    if category == "参数错误" and any(m in h_lower for m in ("暂不可用", "执行超时")):
        return True
    return False


# Upstream "found nothing for that concept/attachment/filing/item/accession"
# messages (SEC): a completed lookup with nothing to show, not an outage.
_SEC_OBJECT_NOT_FOUND_RE = re.compile(
    r"(?i)\b(?:"
    r"no\s+(?:concepts?|attachments?|filings?|items?|accessions?)\s+(?:matching|found)"
    r"|(?:requested\s+)?(?:concept|attachment|filing|item|accession(?:\s+number)?)"
    r"(?:\s+(?:'[^']*'|\"[^\"]*\"|[A-Za-z0-9_.:-]+))?\s+(?:was\s+|is\s+)?not\s+found"
    r")\b"
)


def _sec_data_error_text(payload: dict[str, Any]) -> str:
    data_obj = payload.get("data")
    if isinstance(data_obj, dict):
        inner = str(data_obj.get("error") or "").strip()
        if inner:
            return inner
    return str(payload.get("error") or "").strip()


def _is_sec_object_not_found(route: str, payload: dict[str, Any]) -> bool:
    if route != "sec":
        return False
    err_field = _sec_data_error_text(payload)
    if not err_field:
        return False
    return bool(_SEC_OBJECT_NOT_FOUND_RE.search(err_field))


def _reason_is_upstream_argument_error(reason: str) -> bool:
    """True when one failed source's own reason is upstream's `E_BAD_*` argument check."""
    return "e_bad_" in str(reason or "").strip().lower()


_GATEWAY_VALIDATION_ERROR_SUBSTRINGS: tuple[str, ...] = (
    # Literal fragments of the upstream gateway's own ValueError messages
    # (rd/data_gateway.py, rd/research_library.py, rd/researchdata_mcp.py,
    # Guidepoint 1.4.1), copied verbatim — f-string messages contribute their
    # static parts only. Wording here must match upstream exactly; anything
    # paraphrased never matches and falls through to "上游不可用".
    "--as-of 只支持 SEC；FRED 用 --vintage，其他源未声明历史可见版本",
    "--params-json 仅用于研究来源的只读参数",
    "--vintage 只支持 FRED/ALFRED",
    "Guidepoint keywords 须为 1–10 个非空字符串；无筛选时省略或传 null",
    "Guidepoint recency_bias 须为 1–100 的整数或 null",
    "Guidepoint search_library 的 size 须为 1–30 的整数（以实时 tools/list 为准）",
    "Guidepoint search_library 需要 1–1000 字符的 query；keywords 不能替代 query",
    "TMTB/FUNDA AI 只读原文缓存",
    "accession 须为 SEC 完整编号 XXXXXXXXXX-XX-XXXXXX",
    "cn-hk ask 的公司和日期请写入 query；结构化 ticker/start/end 用 filings，当前报价用 quote+ticker",
    "cn-hk filings 不接受 query；使用 ticker/start/end",
    "cn-hk quote 使用5位港股或6位A股代码",
    "cn-hk quote 只接受 ticker；当前报价不接受历史日期",
    "company 与 sector 只能选一个",
    "document 与 document_type 不能同时使用",
    "document/offset/max_chars 仅用于 read",
    "estimates params 仅支持 shares_bn 和 tax",
    "indexed_since 须为 YYYY-MM-DD 或 UTC ISO 时间",
    "layer 须为 raw、digest 或 all",
    "layer=all 需包含 sales-notes；其他来源只有原文层",
    "limit 须在 1–100",
    "limit 须在 1–100；timeout 须在 1–600 秒",
    "media 须为 all、text 或 images",
    "no_decay 须为布尔值",
    "offset 超过已存正文长度",
    "params 仅支持",
    "params 须为对象",
    "params-json 必须是 JSON 对象",
    "params-json 须为对象",
    "query 与 params-json.query 冲突",
    "read 不接受日期筛选",
    "read 只接受 document、offset、max_chars 和 all 的 sources",
    "read 需要搜索返回的 document_id",
    "search 需要 query",
    "search_mode 须为 hybrid 或 keyword",
    "sender 不能用于含 wiki 的联搜",
    "sender 筛选仅用于原文层",
    "shares_bn 须为正数，tax 须为 0≤税率<1",
    "sources 仅支持 company-wiki、sales-notes、tmtb、funda-ai",
    "sources 仅用于 all",
    "start 不能晚于 end",
    "statement 须为 all|income|balance|cash",
    "topic 仅用于 company-wiki",
    "topic 需要 tag 或 query",
    "wiki 不支持 sender/layer",
    "wiki 专用筛选需要 sources=[company-wiki]；跨库公司名用 query",
    "wiki 需要 wiki:<id>",
    "不在当前分类的可选值中",
    "不支持 --",
    "与 params-json.",
    "主题检索须使用 sort=relevance",
    "只支持 search/read/list/topic",
    "只查摘要须用 sales-notes 或 sources=[sales-notes]；联搜附带摘要用 layer=all",
    "图片读取不使用正文分页参数",
    "存在未支持的参数",
    "摘要仅属于 sales-notes",
    "文档不属于选定 sources",
    "文档属于 sales-notes",
    "文档属于 tmtb，请使用对应来源",
    "日期参数冲突",
    "日期格式错误",
    "未登记的只读操作",
    "未知内部资料参数:",
    "未知工具或参数格式错误",
    "未知路由；先运行 catalog",
    "起止日期倒置",
    "邮件/TMTB 不支持 wiki 专用筛选；公司名放 query",
    "需要 --",
    "需要搜索返回的 mail:/tmtb:/digest: 文档 ID",
    "非 search_library 操作的参数请通过 params-json 完整传入",
    "须为范围内的正整数",
    "须为非空字符串",
    "；参数不会被静默忽略",
)

_GATEWAY_VALIDATION_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    # rd/data_gateway.py _reject_extra_args: "{route_label} 不支持 --{flag}；参数不会被静默忽略"
    re.compile(r"\S+\s+不支持\s+--[a-z0-9_-]+；参数不会被静默忽略"),
    # rd/data_gateway.py _sec_params: "SEC {op} 不接受 {field}；参数不会被静默忽略"
    re.compile(r"SEC\s+\S+\s+不接受\s+[a-z_]+；参数不会被静默忽略"),
    # rd/data_gateway.py _sec_params: "SEC {op} params 仅支持 [...]"
    re.compile(r"SEC\s+\S+\s+params\s+仅支持\s+\["),
    # rd/data_gateway.py _merge_gateway_arg: "{name} 与 params-json.{field} 冲突"
    re.compile(r"\S+\s+与\s+params-json\.\S+\s+冲突"),
    # researchdata_mcp.py: "{rid} 不支持 {op}；可用：[...]"
    re.compile(r"\S+\s+不支持\s+\S+；可用：\["),
    # researchdata_mcp.py schema checks: "{key} 超出范围"
    re.compile(r"\b(?:route|category|params|mode|operation|ticker|query|series|concept|item|document|form|dataset|start|end|as_of|vintage|limit|offset|max_chars|timeout)\s+超出范围"),
    # researchdata_mcp.py: "{key} 类型错误" — STRING_FIELDS plus the integer and
    # routing arguments it type-checks
    re.compile(r"\b(?:route|category|params|mode|operation|ticker|query|series|concept|item|document|form|dataset|start|end|as_of|vintage|limit|offset|max_chars|timeout)\s+类型错误"),
    # researchdata_mcp.py schema checks: "缺少 {key}"; data_gateway.py catalog: "分类必须为 [...]"
    re.compile(r"缺少\s+(?:route|category|params|mode|operation|ticker|query|series|concept|item|document|form|dataset|start|end|as_of|vintage|limit|offset|max_chars|timeout)\b|分类必须为"),
)


def _is_known_gateway_validation_error(err_text: str) -> bool:
    """Return True when `err_text` matches an exact upstream gateway/library validation ValueError."""
    if not err_text:
        return False
    if any(sub in err_text for sub in _GATEWAY_VALIDATION_ERROR_SUBSTRINGS):
        return True
    if any(pat.search(err_text) for pat in _GATEWAY_VALIDATION_ERROR_PATTERNS):
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

    # Upstream's own argument checks (E_BAD_ARGS, E_BAD_DATE_RANGE, ...) ride
    # only on their own leg; split them from the other parameter-error signals
    # so a merged multi-source failure is not labeled by one leg's E_BAD_*.
    e_bad_signal = "e_bad_" in err_lower or err_code.startswith("e_bad_")
    ae_query_sort = err_code == "e_query_sort" or "e_query_sort" in err_lower
    ae_scope_conflict = err_code == "e_scope_conflict" or "e_scope_conflict" in err_lower
    ae_cursor_filter_mismatch = (
        err_code == "e_cursor_filter_mismatch" or "e_cursor_filter_mismatch" in err_lower
    )
    ae_cursor_expired = err_code == "e_cursor_expired" or "e_cursor_expired" in err_lower
    ae_upstream_pagination = (
        err_code in ("e_upstream_cursor", "e_upstream_page")
        or "e_upstream_cursor" in err_lower
        or "e_upstream_page" in err_lower
    )
    ae_upstream_response = (
        err_code in ("e_upstream_attachments", "e_upstream_response")
        or "e_upstream_attachments" in err_lower
        or "e_upstream_response" in err_lower
    )
    ae_desktop_token_expired = (
        err_code == "e_desktop_token_expired" or "e_desktop_token_expired" in err_lower
    )
    ae_desktop_no_module_page = (
        err_code == "e_desktop_no_module_page" or "e_desktop_no_module_page" in err_lower
    )
    ae_desktop_not_running = (
        err_code == "e_desktop_not_running" or "e_desktop_not_running" in err_lower
    )
    # The researchdata MCP server's own field validation (a limit past the
    # upstream cap, a wrong type) is a caller mistake, not an outage — the
    # upstream message names the field, so the hint carries it.
    mcp_field_validation = (
        "mcp_validation_invalid_field" in err_lower
        or err_code.startswith("mcp_validation")
    )
    gateway_validation = _is_known_gateway_validation_error(err_text)
    other_param_signal = (
        status in ("invalid_arguments",)
        or err_code
        in (
            "invalid_arguments",
            "unknown_parameters",
            "parameter_conflict",
            "invalid_operation",
            "invalid_route",
            "missing_required_parameters",
            "invalid_doc_id",
            "invalid_param_value",
            "invalid_nested_param_value",
            "unsupported_parameters",
        )
        or ae_query_sort
        or ae_scope_conflict
        or ae_cursor_filter_mismatch
        or ae_cursor_expired
        or mcp_field_validation
        or gateway_validation
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
    )
    sec_no_match = _is_sec_object_not_found(route, payload)
    # B5: AlphaEngine's "E_UPSTREAM_API: 数据不存在" is one document that does
    # not exist while other documents in the same round read fine — the
    # upstream answered, so it is a no-match, not an outage, and retrying the
    # same document cannot help.
    ae_doc_not_found = "数据不存在" in err_text and (
        err_code == "e_upstream_api" or "e_upstream_api" in err_lower
    )
    # B5: "E_SOURCE_NOT_RESOLVED: 无法唯一匹配来源" is the caller's source
    # name not matching exactly — a parameter mistake the upstream message
    # itself says how to fix (list_filters), not an outage worth retrying
    # as-is (a real task retried it 4 times for nothing).
    source_name_unresolved = (
        err_code == "e_source_not_resolved"
        or "e_source_not_resolved" in err_lower
        or "无法唯一匹配来源" in err_text
    )
    # B5: Xueqiu's comments API answers an expired login cookie with an
    # HTTP 400 (not 401/403) whose wording matches none of the auth tokens
    # below; five straight batch requests failed on it. Matched on the
    # wording itself, never on "400" alone — most 400s are parameter errors.
    xueqiu_login_expired = "雪球" in err_text and (
        "用户未登录" in err_text or "cookie 缺失或已过期" in err_lower
    )
    auth_or_quota_signal = (
        ae_desktop_token_expired
        or status
        in (
            "paid_source_not_allowed",
            "handoff_filtered",
            "auth_expired",
            "unauthorized",
            "forbidden",
            "quota_exceeded",
            "rate_limited",
        )
        or err_code
        in (
            "paid_source_not_allowed",
            "handoff_filtered",
            "unauthorized",
            "forbidden",
            "auth_expired",
        )
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
                # B4: AlphaEngine's Chinese refusals — "无查看权限或额度已达
                # 上限" — carry none of the English tokens above, so they fell
                # into 上游不可用 and invited pointless retries.
                "无查看权限",
                "没有查看权限",
                "无权访问",
                "权限不足",
                "没有权限",
                "未授权",
                "需要授权",
                "授权已过期",
                "授权失效",
                "额度已达上限",
                "额度已用完",
                "额度已耗尽",
                "额度不足",
                "配额已达上限",
                "配额已用完",
                "登录已过期",
                "登录失效",
                "未登录",
                "需要登录",
                "请先登录",
            )
        )
        or xueqiu_login_expired
    )

    _succeeded, failed_legs = (
        _collect_multi_source_health(payload) if route == "all" else ([], [])
    )

    if existing_cat in ("参数错误", "上游不可用", "权限或额度", "没有匹配"):
        category = existing_cat
    elif route == "all" and failed_legs:
        leg_cats = {
            name: _classify_error_category_and_hint(
                tool_name,
                name if name in ROUTE_CONTRACT_SPECS else route,
                operation,
                {"error": reason},
            )[0]
            for name, reason in failed_legs
        }
        if len(set(leg_cats.values())) == 1:
            category = next(iter(leg_cats.values()))
        else:
            payload["leg_categories"] = leg_cats
            category = "上游不可用"
    elif sec_no_match:
        # An unknown concept/attachment/filing/item/accession found nothing;
        # the upstream answered, so neither "参数错误" nor "上游暂不可用，可重试" is the fix.
        category = "没有匹配"
    elif ae_doc_not_found:
        category = "没有匹配"
    elif status != "invalid_arguments" and auth_or_quota_signal:
        category = "权限或额度"
    elif other_param_signal or e_bad_signal or source_name_unresolved:
        category = "参数错误"
    elif auth_or_quota_signal:
        category = "权限或额度"
    elif ae_upstream_pagination or ae_upstream_response or ae_desktop_no_module_page or ae_desktop_not_running:
        category = "上游不可用"
    elif status in ("empty", "no_match") and not payload.get("error"):
        category = "没有匹配"
    else:
        category = "上游不可用"

    existing_hint = str(payload.get("hint") or "").strip()
    if existing_hint and not _hint_conflicts_with_category(category, existing_hint):
        return category, existing_hint

    if category == "参数错误":
        if ae_query_sort:
            hint = (
                "有关键词的主题检索必须使用 sort='relevance'；无关键词浏览才可使用 sort='newest'。"
                "请将 params.sort 改为 'relevance'（或去掉关键词）后重试。"
            )
        elif ae_scope_conflict:
            hint = (
                "机构会议（meeting_scope='institution'）与个人会议（meeting_scope='personal'）必须分开查，"
                "且不能与非会议文档类别混用；请拆分调用后重试。"
            )
        elif ae_cursor_filter_mismatch:
            hint = (
                "cursor 翻页时必须保持与上一页完全相同的 query 与筛选条件；"
                "请恢复原筛选条件继续翻页，或去掉 cursor 从第一页重新起始。"
            )
        elif ae_cursor_expired:
            hint = (
                "游标（cursor）已失效或过期；请去掉 cursor 从第一页重新起始检索，"
                "并按完整 doc_id 去重，禁止重复提交旧 cursor。"
            )
        elif source_name_unresolved:
            hint = (
                "来源名无法唯一匹配；请先调用 list_filters(filter_type=sources) 词典获取准确的来源名称，"
                "或改传 source_ids 重查。"
            )
        elif mcp_field_validation:
            hint = (
                f"上游字段校验失败（{err_text[:200]}）；请核对报错指出的字段名与取值范围后重试"
                f"（可调用 catalog(route='{route}') 查看该路由的参数契约）。"
            )
        elif route and accepted_params:
            hint = (
                f"请核对路由 '{route}' 的参数名与格式（支持的操作: {accepted_ops}，接受的参数: {accepted_params}），"
                f"或先调用 catalog(route='{route}') 查看完整参数契约与调用示例后重试。"
            )
        else:
            hint = "请先调用 catalog() 查看目标路由支持的 operation 与参数列表，修正参数后重试。"
    elif category == "权限或额度":
        if ae_desktop_token_expired:
            hint = (
                "AlphaEngine Desktop 登录已失效，请在客户端内重新登录并刷新模块页；"
                "当前请切换替代数据源（如内部库 company-wiki/sales-notes/tmtb 或 SEC 公告），"
                "并在报告中如实写明该数据源因登录态失效未能取数，禁止写成“没有相关信息”。"
            )
        elif xueqiu_login_expired:
            hint = (
                "雪球登录态失效（Cookie 缺失或已过期）；停止继续请求雪球评论，"
                "改用其他社区来源（employee / last30days / x-timeline），"
                "并在报告中如实说明雪球评论因登录态失效未能获取。"
            )
        elif status == "paid_source_not_allowed":
            hint = "employee 的 indeed/glassdoor 属于付费源，请改用免费操作 operation='blind' 或 'summary'，或在确认授权时显式传 allow_paid=True。"
        elif status == "handoff_filtered":
            hint = "该路由在 LangAlpha 中已禁用；网页新闻请改用内置 WebSearch/WebFetch 工具，X 检索请改用 community(route='x-timeline', operation='search', ...)。"
        else:
            hint = (
                f"路由 '{route or tool_name}' 出现权限、登录态或配额限制；请切换替代数据源（如内部库 company-wiki/sales-notes/tmtb 或 SEC 公告），"
                "若无替代源必须在报告中如实写明该数据源因权限/配额取数失败，禁止写成“没有相关信息”。"
            )
    elif category == "没有匹配":
        if sec_no_match:
            hint = "先用 search-concepts / list-filings / attachments 拿到可用的名称或 ID 再读，不要猜"
        elif ae_doc_not_found:
            hint = (
                "该文档在上游不存在（E_UPSTREAM_API: 数据不存在，同一轮其他文档可正常读取）；"
                "请核对文档 ID 或链接是否准确，不要对同一文档重复请求。"
            )
        elif route == "transcript":
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
        if ae_upstream_pagination:
            hint = (
                "AlphaEngine 上游分页异常（游标未推进或录制任务分页提前返回空页）；"
                "请将该来源保持为失败/未完成状态，不得把已返回数据当作全量或无匹配数据，也不要用同一游标死循环重试。"
            )
        elif ae_upstream_response:
            hint = (
                "AlphaEngine 上游响应异常（附件列表或接口响应异常）；请保持该操作为失败状态，"
                "稍后重试或切换其他数据源并在报告中如实说明，不得当成“没有数据”。"
            )
        elif ae_desktop_no_module_page:
            hint = (
                "AlphaEngine Desktop 已启动但未打开模块页；请在客户端内点开任意模块页（如纪要中心），不能一概提示重新登录；"
                "当前可切换备用数据源并在报告中如实披露，不得当成“没有数据”或“没有相关信息”。"
            )
        elif ae_desktop_not_running:
            hint = (
                "AlphaEngine Desktop 未启动；请先启动 Desktop 并打开模块页；"
                "当前可切换备用数据源并在报告中如实披露，不得当成“没有数据”或“没有相关信息”。"
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
                if st == "skipped":
                    continue
                if s_val.get("ok") is False or s_val.get("isError") is True or bool(s_val.get("error")) or st in _FAILURE_SOURCE_STATES:
                    reason = str(s_val.get("error") or s_val.get("reason") or st or "failed").strip()
                    failed.append((name, reason))
                elif s_val.get("ok") is True or st in ("ok", "empty", "no_match", "no_results"):
                    succeeded.append(name)
            elif isinstance(s_val, str):
                st = s_val.strip().lower()
                if st == "skipped":
                    continue
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
            if st == "skipped":
                continue
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


def _extract_transcript_coverage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the `coverage` dict from a `transcript` payload (`payload.coverage` or `payload.data.coverage`)."""
    cov = payload.get("coverage")
    if isinstance(cov, dict):
        return cov
    data_val = payload.get("data")
    if isinstance(data_val, dict):
        d_cov = data_val.get("coverage")
        if isinstance(d_cov, dict):
            return d_cov
        sc_val = data_val.get("structuredContent")
        if isinstance(sc_val, dict) and isinstance(sc_val.get("coverage"), dict):
            return sc_val["coverage"]
    return None


def _extract_transcript_candidates(payload: dict[str, Any]) -> list[Any]:
    """Extract the candidate list from a `transcript` payload (`data.transcripts` / `transcripts` / `results`)."""
    data_val = payload.get("data")
    if isinstance(data_val, dict):
        for k in ("transcripts", "results", "items"):
            v = data_val.get(k)
            if isinstance(v, list):
                return v
    for k in ("transcripts", "results", "items"):
        v = payload.get(k)
        if isinstance(v, list):
            return v
    return []


def _is_transcript_no_match_payload(payload: dict[str, Any]) -> bool:
    """Return True ONLY when a `transcript` response is a confirmed empty result (`ok: True`) or explicit `alphaengine_no_match` with complete coverage."""
    if _has_disqualifying_error_signals(payload):
        return False
    cov = _extract_transcript_coverage(payload)
    if isinstance(cov, dict) and cov.get("complete") is False:
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
    """Return True ONLY when a `finance`/`macro`/`research` payload succeeded with 0 records, `has_more` is not True, `status` is not `partial`, and no sub-source failed."""
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
    op_lower = (operation or "").strip().lower()
    if op_lower in (
        "read",
        "get_document",
        "read_document",
        "get",
        "get_document_info",
        "list_attachments",
        "get_recording_status",
        "get_recording_task",
        "auth_status",
        "status",
        "list_filters",
        "filters",
    ):
        return False
    data_dict = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    sc_dict = (
        source_identity._unwrap_mcp_proxy_payload(data_dict)
        if isinstance(data_dict, dict) and data_dict
        else {}
    )
    ae_leg = payload.get("alphaengine") if isinstance(payload.get("alphaengine"), dict) else {}
    for container in (payload, data_dict, sc_dict if isinstance(sc_dict, dict) else {}, ae_leg):
        if not isinstance(container, dict):
            continue
        if container.get("has_more") is True:
            return False
        if str(container.get("status") or "").strip().lower() == "partial":
            return False
        cov = container.get("coverage")
        if isinstance(cov, dict) and cov.get("complete") is False:
            return False
    status = str(payload.get("status") or "").strip().lower()
    if status in ("no_match", "empty"):
        return True
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

    # Bounded incomplete scan on `transcript` (`coverage.complete is False`) -> never `no_matching_transcripts`
    if tool_name == "research" and route == "transcript":
        tr_cov = _extract_transcript_coverage(payload)
        if isinstance(tr_cov, dict) and tr_cov.get("complete") is False:
            candidates = _extract_transcript_candidates(payload)
            raw_records_sc = tr_cov.get("records_scanned")
            records_sc = (
                raw_records_sc
                if isinstance(raw_records_sc, int) and not isinstance(raw_records_sc, bool)
                else None
            )
            raw_pages_sc = tr_cov.get("pages_scanned")
            pages_sc = (
                raw_pages_sc
                if isinstance(raw_pages_sc, int) and not isinstance(raw_pages_sc, bool)
                else None
            )
            payload["ok"] = True
            payload["status"] = "partial"
            payload.pop("empty_reason", None)
            payload["coverage"] = tr_cov
            if isinstance(tr_cov.get("warnings"), list) and tr_cov["warnings"]:
                payload.setdefault("warnings", list(tr_cov["warnings"]))
            if len(candidates) > 0:
                if isinstance(payload.get("data"), dict):
                    payload["data"]["ok"] = True
                    payload["data"]["coverage"] = tr_cov
                scan_clause = f"（扫描了 {records_sc} 条）" if records_sc is not None else ""
                payload["hint"] = (
                    f"已找到 {len(candidates)} 篇候选，但检索未覆盖完整期间{scan_clause}，最新一场可能不在其中；"
                    "可用 continuation 参数续查（请将 coverage.continuation_params 交给 research(route='alphaengine', operation='search_library', ...)，不要传回 transcript）。"
                )
            else:
                payload.pop("fallback", None)
                if isinstance(payload.get("data"), dict):
                    payload["data"]["ok"] = True
                    payload["data"].pop("fallback", None)
                    payload["data"].setdefault("transcripts", [])
                    payload["data"]["coverage"] = tr_cov
                if pages_sc is not None and records_sc is not None:
                    scan_desc = f"AlphaEngine 已扫描前 {pages_sc} 页 / {records_sc} 条，coverage.complete=false，"
                elif records_sc is not None:
                    scan_desc = f"AlphaEngine 已扫描 {records_sc} 条，coverage.complete=false，"
                elif pages_sc is not None:
                    scan_desc = f"AlphaEngine 已扫描前 {pages_sc} 页，coverage.complete=false，"
                else:
                    scan_desc = "coverage.complete=false，"
                payload["hint"] = (
                    f"本次有界检索未命中（{scan_desc}尚未扫完全量，不能据此判为无匹配）；"
                    "请将 coverage.continuation_params 交给 research(route='alphaengine', operation='search_library', ...) 继续翻页检索（不要传回 transcript），"
                    "或缩小 start/end 窗口、用 | 分隔中英文公司名重试。"
                )
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

    # Non-empty / special operation shape handling on successful responses
    if tool_name in ("finance", "macro", "research") and payload.get("ok") is True:
        payload.pop("empty_reason", None)
        data_dict = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        sc_dict = (
            source_identity._unwrap_mcp_proxy_payload(data_dict)
            if isinstance(data_dict, dict) and data_dict
            else {}
        )
        sc_obj = sc_dict if isinstance(sc_dict, dict) else {}
        op_lower = (operation or "").strip().lower()

        if any(
            isinstance(c, dict) and str(c.get("status") or "").strip().lower() == "partial"
            for c in (payload, data_dict, sc_obj)
        ):
            payload["status"] = "partial"
            for partial_k in ("warnings", "coverage", "image_status", "field_status"):
                for c in (data_dict, sc_obj):
                    if isinstance(c, dict) and partial_k in c and c[partial_k] is not None and partial_k not in payload:
                        payload[partial_k] = c[partial_k]

        has_more_flag = any(
            isinstance(c, dict) and c.get("has_more") is True
            for c in (payload, data_dict, sc_obj)
        )
        results_list = next(
            (
                c.get("results")
                for c in (sc_obj, data_dict, payload)
                if isinstance(c, dict) and isinstance(c.get("results"), list)
            ),
            None,
        )
        if has_more_flag and isinstance(results_list, list) and len(results_list) == 0:
            if str(payload.get("status") or "").strip().lower() in ("empty", "no_match", ""):
                payload["status"] = "ok"
            payload.setdefault(
                "hint",
                "当前页返回 results=[] 但 has_more=true（后续页仍有数据），请继续携带返回的 cursor（或 next_page）翻页，不得判为无匹配记录。",
            )

        if route == "alphaengine":
            if op_lower in ("get_document", "read", "read_document", "get"):
                for meta_k in (
                    "content_field",
                    "available_content_fields",
                    "warning",
                    "next_offset",
                    "complete",
                    "content_sha256",
                ):
                    if meta_k in sc_obj and sc_obj[meta_k] is not None and meta_k not in payload:
                        payload[meta_k] = sc_obj[meta_k]
            elif op_lower == "list_attachments":
                atts = sc_obj.get("attachments")
                if isinstance(atts, list) and len(atts) == 0:
                    payload.setdefault(
                        "note",
                        "attachments=[] 仅表示没有补充附件，不表示文档不存在；读取正文请调用 get_document。",
                    )

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
    document IDs vs opaque string AlphaEngine IDs (including `_inst_` suffixes), and prevents
    topic queries from derailing `transcript`.
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
        # Serializes formal company-wiki writes (see `handle_wiki_ingest`).
        self._wiki_write_lock = asyncio.Lock()
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
        self._last_ae_fanout_limit_clamped: list[int] | None = None
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
            corr = _current_corr_id.get()
            logger.warning(
                "researchdata_mcp subprocess returned no JSON response (exit_code=%s corr=%s)",
                proc.returncode,
                corr or "-",
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
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Map `research(route='all')` search arguments to `route='alphaengine'` parameters.

        Returns `(leg_args, window_error_or_none)`; a window error fails the
        whole route='all' call as a parameter error at the fan-out call site,
        it is never silently dropped.

        Mapping basis:
        - `data_gateway.py` sets `accepted['alphaengine'] = {'query', 'start', 'end'}` and rejects
          `--ticker` (and `series`/`concept`/`item`/`document`/`form`/`dataset`/`as_of`/`vintage`)
          with ValueError; if `ticker` was provided on `all`, fold it into `query`.
        - `data_gateway.py` and `config/data_sources.json` route `alphaengine` search calls to
          `operation='search_library'` (one of the 9 supported bridge operations:
          `search_library`, `get_document`, `get_document_info`, `list_attachments`,
          `list_recording_tasks`, `get_recording_status`, `get_recording_task`,
          `list_filters`, `auth_status`).
        - `alphaengine-mcp/server.mjs` (`requireDatePair`) requires `start` (`date_from`) and `end`
          (`date_to`) as a pair; a lone `start` is closed to `end=_bridge_today()` by the
          shared window rules (same helper as a direct call), and `limit` is capped at 50.
        - Local-library `params` keys (`sources`, `layer`, `search_mode`, etc.) are omitted because
          `search_library` sets `additionalProperties: false`.
        - Timeout is capped at `FANOUT_ALPHAENGINE_TIMEOUT_S` (60s).
        """
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if ticker and not _query_contains_ticker(query, ticker):
            query = f"{ticker} {query}".strip() if query else ticker

        ae_args: dict[str, Any] = {
            "route": "alphaengine",
            "operation": "search_library",
            "query": query,
        }
        start = str(arguments.get("start") or "").strip()
        end = str(arguments.get("end") or "").strip()
        if start:
            ae_args["start"] = start
        if end:
            ae_args["end"] = end
        # The same normalization/truncation/ordering as a direct call. The
        # route='all' entry point already validated the caller's dates per leg,
        # so the error return should be unreachable here — but it is returned,
        # not dropped: a window this leg cannot serve legally fails the whole
        # call as a parameter error. What matters day to day is that today is
        # `_bridge_today()` (max of this host's and Beijing's date), not the
        # UTC date: in the Beijing small hours UTC is a day behind and the
        # gateway refused the resulting start > end window.
        ae_win_err = _normalize_date_window("alphaengine", ae_args)

        self._last_ae_fanout_limit_clamped = None
        limit = arguments.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            if limit > 50:
                self._last_ae_fanout_limit_clamped = [limit, 50]
            ae_args["limit"] = min(limit, 50)

        ae_args["timeout"] = self._compute_fanout_timeout(
            arguments, FANOUT_ALPHAENGINE_TIMEOUT_S
        )
        return ae_args, ae_win_err

    def _build_guidepoint_fanout_args(
        self, arguments: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Map `research(route='all')` search arguments to `route='guidepoint'` parameters.

        Returns `(leg_args, window_error_or_none)`; a window error fails the
        whole route='all' call as a parameter error at the fan-out call site,
        it is never silently dropped.

        Mapping basis:
        - `catalog()` (`config/data_sources.json`) registers `guidepoint` with
          `operations: ['search_library']` (forwarded to `mcp__guidepoint__search_library`).
        - `researchdata_mcp.py` (`call_tool`) forwards top-level string fields + `limit` + `timeout`
          + `params` to `data_gateway.py fetch guidepoint`.
        - `data_gateway.py` (`build_command`) sets `accepted['guidepoint'] = {'query', 'start', 'end'}`
          and raises ValueError if `--ticker` (or `series`/`concept`/`item`/`document`/`form`/
          `dataset`/`as_of`/`vintage`) is passed; if `ticker` was provided on `all`, fold it into `query`.
        - `data_gateway.py` (`native_request`) maps `start` -> `start_date`, `end` -> `end_date`,
          and `limit` -> `size` (clamped to `min(limit, 30)`, the upstream
          search_library maximum).
        - Local-library `params` keys (`sources`, `layer`, `media`, `search_mode`, `company`,
          `sector`, `tag`, `indexed_since`, `sender`, `no_decay`, `offset`, `max_chars`) are omitted;
          only Guidepoint-native optional keys (`keywords`, `recency_bias`) are forwarded if present.
        - Timeout is capped at `FANOUT_GUIDEPOINT_TIMEOUT_S` (90s).
        """
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if ticker and not _query_contains_ticker(query, ticker):
            query = f"{ticker} {query}".strip() if query else ticker

        gp_args: dict[str, Any] = {
            "route": "guidepoint",
            "operation": "search_library",
            "query": query,
        }
        start = str(arguments.get("start") or "").strip()
        end = str(arguments.get("end") or "").strip()
        raw_params = arguments.get("params")
        # A nested window (params.start_date/end_date) belongs to this leg
        # alone; carrying the top-level start/end beside it makes the gateway
        # reject the whole call with a date-parameter conflict when the two
        # disagree, so when a nested window is present it is the only one sent.
        # AlphaEngine's leg keeps the top-level window either way.
        has_nested_window = isinstance(raw_params, dict) and any(
            str(raw_params.get(k) or "").strip() for k in ("start_date", "end_date")
        )
        gp_win_err: dict[str, Any] | None = None
        if not has_nested_window:
            if start:
                gp_args["start"] = start
            if end:
                gp_args["end"] = end
            # Shared window rules (end truncated to `_bridge_today()`, ordering
            # checked); a lone end stays lone — Guidepoint, unlike AlphaEngine,
            # accepts half a window. The entry point already validated the
            # dates; the error, should one still arise, is returned, not dropped.
            win_err_top = _normalize_date_window("guidepoint", gp_args)
            if win_err_top is not None:
                gp_win_err = gp_win_err or win_err_top

        limit = arguments.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            gp_args["limit"] = min(limit, 30)

        if isinstance(raw_params, dict):
            gp_params: dict[str, Any] = {}
            gp_check_dict: dict[str, Any] = {}
            if "keywords" in raw_params:
                gp_check_dict["keywords"] = raw_params["keywords"]
            if "recency_bias" in raw_params:
                gp_check_dict["recency_bias"] = raw_params["recency_bias"]
            if gp_check_dict:
                norm_view = _build_normalized_params_view(arguments)
                gp_param_err = _normalize_guidepoint_params(gp_check_dict, norm_view)
                if gp_param_err is not None:
                    gp_win_err = gp_win_err or gp_param_err
                else:
                    if "keywords" in gp_check_dict:
                        gp_params["keywords"] = gp_check_dict["keywords"]
                    if "recency_bias" in gp_check_dict:
                        gp_params["recency_bias"] = gp_check_dict["recency_bias"]
            for nested_date_k, as_field in (("start_date", "start"), ("end_date", "end")):
                raw_nd = raw_params.get(nested_date_k)
                if raw_nd is not None and str(raw_nd).strip():
                    coerced_nd = _normalize_date_param(as_field, raw_nd)
                    gp_params[nested_date_k] = (
                        min(coerced_nd, _bridge_today())
                        if coerced_nd is not None and as_field == "end"
                        else (coerced_nd if coerced_nd is not None else str(raw_nd).strip())
                    )
            if gp_params:
                nested_win_err = _normalize_date_window("guidepoint", gp_args, gp_params)
                if nested_win_err is not None:
                    gp_win_err = gp_win_err or nested_win_err
                gp_args["params"] = gp_params

        gp_args["timeout"] = self._compute_fanout_timeout(
            arguments, FANOUT_GUIDEPOINT_TIMEOUT_S
        )
        return gp_args, gp_win_err

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
        meta_dict: dict[str, Any] = sc if isinstance(sc, dict) else {}
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
            if isinstance(top_sc, dict):
                meta_dict = top_sc

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

        out_outcome: dict[str, Any] = {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "reason": None,
            "count": (
                int(meta_dict.get("returned"))
                if isinstance(meta_dict.get("returned"), int)
                and not isinstance(meta_dict.get("returned"), bool)
                else len(ae_items)
            ),
            "results": ae_items,
            "raw_payload": ae_payload,
            "cursor": meta_dict.get("cursor"),
            "has_more": bool(meta_dict.get("has_more")) if "has_more" in meta_dict else False,
            "total_upstream": meta_dict.get("total_upstream"),
            "filters": dict(meta_dict["filters"]) if isinstance(meta_dict.get("filters"), dict) else {},
        }
        if getattr(self, "_last_ae_fanout_limit_clamped", None):
            out_outcome["limit_clamped"] = list(self._last_ae_fanout_limit_clamped)
        return out_outcome

    async def _run_guidepoint_fanout(
        self, req_id: Any, gp_args: dict[str, Any], gp_budget_s: float
    ) -> dict[str, Any]:
        """Run the parallel `route='guidepoint'` search and normalize its result or failure state."""
        gp_query_str = str(gp_args.get("query") or "")
        if len(gp_query_str) > 1000:
            return {
                "ok": True,
                "status": "skipped",
                "route": "guidepoint",
                "reason": "query 超过 Guidepoint 1000 字上限，本腿未执行",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
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
        ae_sc = (
            source_identity._unwrap_mcp_proxy_payload(raw_ae_check)
            if isinstance(raw_ae_check, dict)
            else {}
        )
        if not isinstance(ae_sc, dict):
            ae_sc = {}
        ae_cursor = (
            ae_outcome.get("cursor")
            if "cursor" in ae_outcome
            else ae_sc.get("cursor")
        )
        ae_has_more = (
            bool(ae_outcome.get("has_more"))
            if "has_more" in ae_outcome
            else (bool(ae_sc.get("has_more")) if "has_more" in ae_sc else False)
        )
        ae_total_upstream = (
            ae_outcome.get("total_upstream")
            if "total_upstream" in ae_outcome
            else ae_sc.get("total_upstream")
        )
        ae_filters = (
            ae_outcome.get("filters")
            if isinstance(ae_outcome.get("filters"), dict)
            else (ae_sc.get("filters") if isinstance(ae_sc.get("filters"), dict) else {})
        )
        clamped_pair = ae_outcome.get("limit_clamped") or getattr(
            self, "_last_ae_fanout_limit_clamped", None
        )
        next_page_hint = (
            '翻取下一页请改用 research(route="alphaengine", operation="search_library", '
            'query=..., start=..., end=..., params={"cursor": <payload.alphaengine.cursor>, ...})，'
            "必须沿用同一 query、日期与筛选条件。"
        )
        if ae_ok:
            ae_items = list(ae_outcome.get("results") or [])
            ae_section: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "route": "alphaengine",
                "count": len(ae_items),
                "results": ae_items,
                "cursor": ae_cursor,
                "has_more": ae_has_more,
                "total_upstream": ae_total_upstream,
                "filters": dict(ae_filters) if isinstance(ae_filters, dict) else {},
                "next_page_hint": next_page_hint,
            }
            if clamped_pair:
                ae_section["limit_clamped"] = list(clamped_pair)
                ae_section["note"] = (
                    f"AlphaEngine search_library 单次上限为 50，已将 limit 从 {clamped_pair[0]} 截断为 50；"
                    '如需更多结果请改用 research(route="alphaengine", operation="search_library") 并传 params.cursor 翻页。'
                )
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
                "cursor": None,
                "has_more": False,
                "total_upstream": None,
                "filters": {},
            }
            if clamped_pair:
                ae_section["limit_clamped"] = list(clamped_pair)
                ae_section["note"] = (
                    f"AlphaEngine search_library 单次上限为 50，已将 limit 从 {clamped_pair[0]} 截断为 50。"
                )

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
        gp_status_raw = str(gp_outcome.get("status") or "").strip().lower()
        gp_ok = bool(gp_outcome.get("ok")) and not bool(gp_raw_unwrapped["failed"])
        if gp_status_raw == "skipped":
            reason = str(
                gp_outcome.get("reason") or "query 超过 Guidepoint 1000 字上限，本腿未执行"
            )
            gp_items: list[dict[str, Any]] = []
            gp_section: dict[str, Any] = {
                "ok": True,
                "status": "skipped",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
            }
        elif gp_ok:
            gp_items = list(gp_outcome.get("results") or [])
            gp_section = {
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

    # B5: the per-call AlphaEngine ledger. One JSONL line per served call —
    # a direct route='alphaengine' call or the AlphaEngine leg of a
    # route='all' fan-out — with the caller identity the request headers
    # carried (empty when they carried none). The query itself never lands
    # in the file: only a 12-hex sha256 prefix, enough to spot repeat
    # searches without keeping the wording.
    _ALPHAENGINE_USAGE_MAX_BYTES: int = 32 * 1024 * 1024

    @staticmethod
    def _alphaengine_usage_op(operation: str) -> str:
        op_clean = str(operation or "").strip()
        return {
            "search_library": "search",
            "list_filters": "search",
            "list_recording_tasks": "search",
            "get_document_info": "meta",
            "list_attachments": "meta",
            "get_recording_status": "meta",
            "get_recording_task": "meta",
            "get_document": "read",
            "auth_status": "status",
        }.get(op_clean, op_clean or "call")

    @staticmethod
    def _extract_alphaengine_usage_count(
        operation: str, payload: dict[str, Any]
    ) -> int:
        if not isinstance(payload, dict) or not bool(payload.get("ok")):
            return 0
        op_clean = str(operation or "search_library").strip()
        inner = (
            payload.get("data")
            if isinstance(payload.get("data"), dict)
            else payload
        )
        sc = source_identity._unwrap_mcp_proxy_payload(inner)
        if not isinstance(sc, dict):
            sc = {}
        if op_clean in ("search_library", "list_recording_tasks", ""):
            ret_val = sc.get("returned")
            if isinstance(ret_val, int) and not isinstance(ret_val, bool):
                return max(0, ret_val)
            if isinstance(sc.get("results"), list):
                return len(sc["results"])
            if isinstance(payload.get("result_count"), int) and not isinstance(payload.get("result_count"), bool):
                return max(0, payload["result_count"])
            if isinstance(payload.get("results"), list):
                return len(payload["results"])
            return 0
        if op_clean == "list_attachments":
            att_cnt = sc.get("attachment_count")
            if isinstance(att_cnt, int) and not isinstance(att_cnt, bool):
                return max(0, att_cnt)
            if isinstance(sc.get("attachments"), list):
                return len(sc["attachments"])
            if isinstance(payload.get("attachments"), list):
                return len(payload["attachments"])
            return 0
        if op_clean == "list_filters":
            ret_val = sc.get("returned")
            if isinstance(ret_val, int) and not isinstance(ret_val, bool):
                return max(0, ret_val)
            for list_key in (
                "categories",
                "document_types",
                "companies",
                "industries",
                "sources",
                "watchlist_groups",
                "stocks",
                "regions",
                "items",
                "results",
            ):
                if isinstance(sc.get(list_key), list):
                    return len(sc[list_key])
                if isinstance(payload.get(list_key), list):
                    return len(payload[list_key])
            cnt_val = sc.get("count")
            if isinstance(cnt_val, int) and not isinstance(cnt_val, bool):
                return max(0, cnt_val)
            return 0
        if op_clean in (
            "get_document",
            "get_document_info",
            "get_recording_status",
            "get_recording_task",
            "auth_status",
        ):
            return 1
        return 0

    async def _record_alphaengine_usage(
        self,
        *,
        operation: str,
        arguments: dict[str, Any] | None,
        ok: bool,
        error_category: str | None,
        count: int,
        caller: dict[str, str] | None,
    ) -> None:
        args = arguments if isinstance(arguments, dict) else {}
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        query = str(args.get("query") or params.get("query") or "").strip()
        raw_doc_id = str(params.get("doc_id") or args.get("document") or "")
        doc_id = _normalize_alphaengine_doc_id(raw_doc_id).strip() if raw_doc_id.strip() else ""
        op_raw = str(operation or "").strip()
        op_code = self._alphaengine_usage_op(op_raw)
        op_category_zh = {
            "search": "检索",
            "meta": "元数据",
            "read": "read",
            "status": "状态",
        }.get(op_code, op_code)
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "op": op_code,
            "operation": op_raw or "search_library",
            "op_category": op_category_zh,
            "query_sha256_12": (
                hashlib.sha256(query.encode("utf-8")).hexdigest()[:12] if query else ""
            ),
            "doc_id": doc_id,
            "count": max(0, int(count)) if isinstance(count, int) else 0,
            "ok": bool(ok),
            "error_category": str(error_category or "").strip(),
            "user": str((caller or {}).get("user") or ""),
            "workspace": str((caller or {}).get("workspace") or ""),
            "thread": str((caller or {}).get("thread") or ""),
        }
        if op_code == "read" or op_raw == "get_document":
            entry["content_field"] = str(params.get("content_field") or "auto").strip()
            off_parsed = _parse_int_param(params.get("offset"))
            entry["offset"] = max(0, int(off_parsed)) if off_parsed is not None else 0
        if params.get("roadshow_id") is not None and str(params.get("roadshow_id")).strip():
            entry["roadshow_id"] = str(params.get("roadshow_id")).strip()
        async with self._usage_lock:
            try:
                usage_file = self.cfg.alphaengine_usage_file
                usage_file.parent.mkdir(parents=True, exist_ok=True)
                if usage_file.is_file() and (
                    usage_file.stat().st_size >= self._ALPHAENGINE_USAGE_MAX_BYTES
                ):
                    rotated = usage_file.with_suffix(usage_file.suffix + ".1")
                    try:
                        rotated.unlink(missing_ok=True)
                    except OSError:
                        pass
                    usage_file.replace(rotated)
                with usage_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception as exc:
                logger.warning(
                    "Failed to append alphaengine usage ledger entry: %s",
                    type(exc).__name__,
                )

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
        self,
        req_id: Any,
        params: dict[str, Any],
        *,
        caller_identity: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Serve one JSON-RPC ``tools/call``.

        ``caller_identity`` is the ``X-Dalton-User/-Workspace/-Thread`` trio
        the HTTP request carried (B5); it rides into the AlphaEngine usage
        ledger, empty when the caller sent none.
        """
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
        # SEC observability: one correlation id per bridge request, threaded
        # through every line this request logs (tools/call result, timeout,
        # sec_call_*), plus how many stdio attempts the request made.
        corr_id = uuid.uuid4().hex[:12]
        _current_corr_id.set(corr_id)
        stdio_attempts = 0

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
            mc_ceiling = (
                SEC_MAX_CHARS_CEILING
                if (tool_name == "finance" and route == "sec")
                else MAX_CHARS_CEILING
            )
            mc_raw = clamped_p.get("max_chars")
            mc_int = _parse_int_param(mc_raw)
            if mc_int is not None:
                if mc_int > mc_ceiling:
                    params_clamped["max_chars"] = [mc_raw, mc_ceiling]
                    clamped_p["max_chars"] = mc_ceiling
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
            # Guidepoint's upstream search_library caps size at 30; a bigger
            # value is an MCP_VALIDATION_INVALID_FIELD refusal, so the bridge
            # clamps it the way the route='all' fan-out leg already does and
            # reports the change beside the rest.
            gp_size_raw = clamped_p.get("size")
            gp_size_int = _parse_int_param(gp_size_raw)
            if gp_size_int is not None and gp_size_int > 30:
                params_clamped["params.size"] = [gp_size_raw, 30]
                clamped_p["size"] = 30
            call_args["params"] = clamped_p

        # The same ceiling on guidepoint's top-level `limit` (the fan-out leg
        # clamps at min(limit, 30); a direct call must not fare worse).
        if tool_name == "research" and route == "guidepoint":
            gp_limit_raw = call_args.get("limit")
            gp_limit_int = _parse_int_param(gp_limit_raw)
            if gp_limit_int is not None and gp_limit_int > 30:
                params_clamped["limit"] = [gp_limit_raw, 30]
                call_args["limit"] = 30

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
            has_local_prefix = bool(
                re.match(
                    r"^(?:wiki|mail|tmtb|digest|funda|image):",
                    doc_candidate,
                    re.IGNORECASE,
                )
            )
            has_ae_prefix = doc_candidate.lower().startswith("alphaengine:")
            norm_ae_candidate = _normalize_alphaengine_doc_id(doc_candidate)
            is_ae_doc_id = (
                not has_local_prefix
                and bool(norm_ae_candidate)
                and _is_valid_alphaengine_doc_id(norm_ae_candidate)
                and (
                    has_ae_prefix
                    or bool(re.fullmatch(r"\d+(?:_[A-Za-z0-9_-]+)?", norm_ae_candidate))
                )
            )
            if is_ae_doc_id:
                ae_hint = (
                    f'这是 AlphaEngine 文档，用 research(route="alphaengine", '
                    f'operation="get_document", params={{"doc_id": "{norm_ae_candidate}"}}) 读取'
                )
                err_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "error",
                    "document_id": norm_ae_candidate,
                    "doc_id": norm_ae_candidate,
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
        ae_args: dict[str, Any] | None = None
        gp_args: dict[str, Any] | None = None
        if do_fanout:
            ae_args, ae_win_err = self._build_alphaengine_fanout_args(arguments)
            gp_args, gp_win_err = self._build_guidepoint_fanout_args(arguments)
            # A leg window the entry point's per-leg validation somehow let
            # through fails the whole call as a parameter error — the old code
            # dropped the error and sent the bad window upstream anyway.
            fanout_win_err = ae_win_err or gp_win_err
            if fanout_win_err is not None:
                return self._make_tool_result_response(
                    req_id, fanout_win_err, is_error=True
                )
        if tool_name == "research" and route == "all" and isinstance(call_args.get("params"), dict):
            # Guidepoint-only nested keys (`keywords`, `recency_bias`, `start_date`, `end_date`)
            # are accepted on `route="all"` only for the Guidepoint fan-out leg; the local
            # 4-library gateway rejects them on `route=all`, so strip them before building `rpc_req`.
            gp_only_keys = ("keywords", "recency_bias", "start_date", "end_date")
            if any(k in call_args["params"] for k in gp_only_keys):
                local_all_params = {
                    k: v
                    for k, v in call_args["params"].items()
                    if k not in gp_only_keys
                }
                if local_all_params:
                    call_args["params"] = local_all_params
                else:
                    call_args.pop("params", None)
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
                            stdio_attempts += 1
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
                    stdio_attempts += 1
                    try:
                        resps = await self._run_stdio_rpc(
                            [rpc_req], timeout_s=remaining_budget_s
                        )
                    except asyncio.TimeoutError:
                        # P0 fix1 5: the outer asyncio.TimeoutError handler
                        # below already writes this request's one
                        # sec_call_failed line — logging here too would emit
                        # two for the same failure.
                        raise
                    except Exception as call_exc:
                        # SEC observability: a stdio leg that died before any
                        # payload (subprocess exit, spawn failure) still gets
                        # its structured failure line.
                        if route == "sec":
                            _log_sec_call_outcome(
                                corr_id=corr_id,
                                operation=operation,
                                arguments=arguments,
                                payload=None,
                                elapsed_ms=int((time.monotonic() - t0) * 1000),
                                stdio_attempts=stdio_attempts,
                                error_text=f"{type(call_exc).__name__}: {call_exc}",
                            )
                        raise
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    "tools/call timeout corr=%s tool=%s route=%s op=%s timeout_s=%d elapsed_ms=%d",
                    corr_id,
                    tool_name,
                    route,
                    operation,
                    eff_timeout,
                    elapsed_ms,
                )
                if route == "sec":
                    _log_sec_call_outcome(
                        corr_id=corr_id,
                        operation=operation,
                        arguments=arguments,
                        payload=None,
                        elapsed_ms=elapsed_ms,
                        stdio_attempts=stdio_attempts,
                        timeout_s=eff_timeout,
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
                # B5: a timed-out AlphaEngine call was still a call — it lands
                # in the ledger with its classified failure.
                if route == "alphaengine":
                    await self._record_alphaengine_usage(
                        operation=operation or "search_library",
                        arguments=arguments,
                        ok=False,
                        error_category=str(timeout_payload.get("error_category") or ""),
                        count=0,
                        caller=caller_identity,
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
        # B5: a failure line carries the classified category and the head of
        # the error message — the SEC list-filings cluster was first debugged
        # against status-only lines, with the reason invisible.
        error_category_str = "-"
        error_excerpt_str = ""
        # P0 fix1 5: guards against a second sec_call_failed line for the same
        # request (payload path already logged, or top-level error below).
        sec_outcome_logged = False
        mcp_is_error = isinstance(result_obj, dict) and bool(result_obj.get("isError"))
        raw_content_text = ""

        if isinstance(result_obj, dict) and isinstance(result_obj.get("content"), list):
            blocks = result_obj["content"]
            if blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                raw_content_text = blocks[0]["text"]
                try:
                    payload = json.loads(raw_content_text)
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
                                r_item["operations"] = list(c_spec["operations"])
                                r_item["required_params"] = dict(c_spec["required_params"])
                                r_item["optional_params"] = list(c_spec["optional_params"])
                                r_item["accepted_params"] = list(c_spec["accepted_params"])
                                if "accepted_nested_params" in c_spec:
                                    r_item["accepted_nested_params"] = list(
                                        c_spec["accepted_nested_params"]
                                    )
                                if "accepted_nested_params_by_operation" in c_spec:
                                    r_item["accepted_nested_params_by_operation"] = {
                                        k: list(v)
                                        for k, v in c_spec[
                                            "accepted_nested_params_by_operation"
                                        ].items()
                                    }
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

                    # P0 fix1 6: extract status_code / code from any structured
                    # error object BEFORE _apply_error_and_empty_contract
                    # stringifies payload["error"].
                    pre_sec_err_code = ""
                    pre_sec_status: int | None = None
                    if route == "sec":
                        err_pre = payload.get("error")
                        if isinstance(err_pre, dict):
                            pre_sec_err_code = str(
                                err_pre.get("code") or err_pre.get("error_code") or ""
                            )
                        pre_sec_status = _extract_upstream_http_status(
                            payload, str(err_pre or "")
                        )

                    _apply_error_and_empty_contract(
                        tool_name, route, operation, payload, norm_params_view
                    )

                    # Track AlphaEngine get_document quota usage (only after unwrapping/status classification)
                    if (
                        route == "alphaengine"
                        and operation == "get_document"
                        and bool(payload.get("ok"))
                    ):
                        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                        sc = inner.get("structuredContent") if isinstance(inner, dict) else None
                        if not isinstance(sc, dict):
                            sc = source_identity._unwrap_mcp_proxy_payload(payload)
                        chars = 0
                        if isinstance(sc, dict):
                            chars = int(sc.get("returned_chars") or len(str(sc.get("text") or "")))
                        await self._record_alphaengine_get_doc(chars)

                    # B5: one ledger line per served AlphaEngine call — the
                    # direct route and the route='all' fan-out leg each.
                    # Unwrapping and error classification run first so ok,
                    # error_category, and structuredContent count are accurate.
                    if route == "alphaengine":
                        eff_op_for_usage = operation or "search_library"
                        await self._record_alphaengine_usage(
                            operation=eff_op_for_usage,
                            arguments=arguments,
                            ok=bool(payload.get("ok")),
                            error_category=str(payload.get("error_category") or ""),
                            count=self._extract_alphaengine_usage_count(
                                eff_op_for_usage, payload
                            ),
                            caller=caller_identity,
                        )
                    if ae_outcome is not None and ae_args is not None:
                        ae_ok = bool(ae_outcome.get("ok"))
                        ae_reason = str(ae_outcome.get("reason") or "")
                        await self._record_alphaengine_usage(
                            operation=str(ae_args.get("operation") or "search_library"),
                            arguments=ae_args,
                            ok=ae_ok,
                            error_category=(
                                "" if ae_ok else _classify_error_category_and_hint(
                                    "research", "alphaengine", "search_library", {"error": ae_reason}
                                )[0]
                            ),
                            count=int(ae_outcome.get("count") or 0),
                            caller=caller_identity,
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
                    err_body = str(payload.get("error") or "").strip()
                    if err_body:
                        error_category_str = str(payload.get("error_category") or "").strip() or "-"
                        # The failure-log excerpt's own redaction (D-r2): URL
                        # query values, whole cookie strings, emails and phone
                        # numbers ride past the token patterns, so this layer
                        # strips them before the 300-char slice.
                        error_excerpt_str = _sanitize_failure_log_excerpt(err_body)
                    if route == "sec":
                        _log_sec_call_outcome(
                            corr_id=corr_id,
                            operation=operation,
                            arguments=arguments,
                            payload=payload,
                            elapsed_ms=elapsed_ms,
                            stdio_attempts=stdio_attempts,
                            error_code=pre_sec_err_code,
                            upstream_status_hint=pre_sec_status,
                        )
                        sec_outcome_logged = True

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

        # P0 fix1 5 + fix2 D: a JSON-RPC top-level error reply (no result.content)
        # or an MCP tool error with non-JSON text (`result.isError=true`, e.g.
        # `Error calling tool finance: HTTP 403 Forbidden`) never reaches the
        # dict payload branch above — log `sec_call_failed` with the extracted
        # HTTP status and ensure the `tools/call` log line does not say `status=ok`
        # (only the log field changes; `resp` returned to the caller is untouched).
        top_error = resp.get("error") if isinstance(resp.get("error"), dict) else None
        if (
            route == "sec"
            and not sec_outcome_logged
            and (top_error is not None or mcp_is_error)
        ):
            _log_sec_call_outcome(
                corr_id=corr_id,
                operation=operation,
                arguments=arguments,
                payload=None,
                elapsed_ms=elapsed_ms,
                stdio_attempts=stdio_attempts,
                error_text=(
                    str(top_error.get("message") or "")
                    if top_error is not None
                    else raw_content_text
                ),
                error_code=(
                    str(top_error.get("code") or "")
                    if top_error is not None
                    else ""
                ),
            )
            sec_outcome_logged = True

        if status_str == "ok" and (mcp_is_error or top_error is not None):
            status_str = "error"
            if not error_excerpt_str:
                fallback_err = (
                    raw_content_text
                    if raw_content_text
                    else (str(top_error.get("message") or "") if top_error is not None else "")
                )
                if fallback_err:
                    error_excerpt_str = _sanitize_failure_log_excerpt(fallback_err)

        # B5: failures append the category and the sanitized error excerpt;
        # success lines keep their original shape.
        error_log_suffix = ""
        error_log_args: tuple[str, ...] = ()
        if error_excerpt_str:
            error_log_suffix = " error_category=%s error=%s"
            error_log_args = (error_category_str, error_excerpt_str)
        logger.info(
            "tools/call corr=%s tool=%s route=%s op=%s status=%s declared_sources=%d elapsed_ms=%d"
            + error_log_suffix,
            corr_id,
            tool_name,
            route or "-",
            operation or "-",
            # P0 fix3: the status can come from upstream JSON, so it is cleaned too
            _log_redaction.clean_log_field(status_str, max_len=32),
            declared_count,
            elapsed_ms,
            *error_log_args,
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
        """Compute next run timestamp from the conversation-archive launchd plist when enabled, else None."""
        if not enabled:
            return None
        plist_data = self._load_plist_dict(self.cfg.archive_plist_file)
        return self._compute_next_calendar_interval_iso(plist_data, now=now)

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

    @staticmethod
    def _slice_last_archive_run_lines(
        lines: list[str],
        *,
        max_lines: int = 25,
    ) -> list[str]:
        """Slice ``lines`` from the last run header (``=== ...``, excluding ``=== exit...``) and keep its tail."""
        start_idx = 0
        for idx, line in enumerate(lines):
            if line.startswith("=== ") and not line.startswith("=== exit"):
                start_idx = idx
        run_lines = lines[start_idx:]
        if max_lines > 0 and len(run_lines) > max_lines:
            return run_lines[-max_lines:]
        return run_lines

    def _read_sanitized_archive_log_tail(self, *, max_len: int = 8192) -> str:
        """Return the sanitized tail (max 8 KiB) of the last conversation-archive run.

        The launchd script and the bridge's manual run both write the whole run, stderr
        included, to the stdout log under one ``=== <time>`` header, so the run is sliced
        from there. The separate stderr log is read only when the stdout log is missing or
        empty (an install that predates the merged log); a run header that scrolled out of
        the scan window must not pull in another file's older lines.
        """
        chunks: list[str] = []
        stdout_has_content = False
        if self.cfg.archive_log_file.is_file():
            try:
                raw_stdout = self._read_complete_tail_lines(
                    self.cfg.archive_log_file, max_lines=2000
                )
                stdout_has_content = any(line.strip() for line in raw_stdout)
                stdout_lines = self._slice_last_archive_run_lines(
                    raw_stdout, max_lines=25
                )
                if stdout_lines:
                    chunks.append("\n".join(stdout_lines))
            except Exception:
                pass
        if not stdout_has_content and self.cfg.archive_err_log_file.is_file():
            try:
                raw_stderr = self._read_complete_tail_lines(
                    self.cfg.archive_err_log_file, max_lines=200
                )
                stderr_lines = self._slice_last_archive_run_lines(
                    raw_stderr, max_lines=25
                )
                if stderr_lines:
                    chunks.append("\n".join(stderr_lines))
            except Exception:
                pass
        if not chunks:
            mem_tail = self._archive_last_status.get("log_tail")
            if isinstance(mem_tail, str) and mem_tail.strip():
                mem_lines = self._slice_last_archive_run_lines(
                    mem_tail.strip().splitlines(),
                    max_lines=25,
                )
                if mem_lines:
                    chunks.append("\n".join(mem_lines))
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
                    if isinstance(rc, int) and not isinstance(rc, bool):
                        log_fh.write(f"=== exit {int(rc)}\n".encode("utf-8"))
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
            env_vars["DALTON_BRIDGE_EMBED_URL"] = "http://127.0.0.1:8960/embed"
            env_vars["no_proxy"] = (
                f"localhost,127.0.0.1,::1,host.docker.internal,{env_vars.get('no_proxy', '')}"
            )

            started_at = datetime.now(timezone.utc).isoformat()
            log_fh: Any = None
            try:
                self.cfg.archive_log_file.parent.mkdir(parents=True, exist_ok=True)
                log_fh = self.cfg.archive_log_file.open("ab")
                ts_str = (
                    datetime.now()
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S %Z")
                    .strip()
                )
                log_fh.write(f"=== {ts_str} attempt 1\n".encode("utf-8"))
                log_fh.flush()
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
                    if log_fh is not None:
                        try:
                            log_fh.write(b"=== exit 2\n")
                            log_fh.flush()
                            log_fh.close()
                        except Exception:
                            pass
                        log_fh = None
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

    @staticmethod
    def _probe_launchd_label_status(label: str) -> str:
        """Return ``'on'`` if ``launchctl print gui/<uid>/<label>`` succeeds, ``'off'`` on non-zero exit, else ``'unknown'``."""
        try:
            uid = os.getuid()
            res = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            return "on" if res.returncode == 0 else "off"
        except Exception:
            return "unknown"

    @staticmethod
    def _load_plist_dict(path: Path) -> dict[str, Any] | None:
        """Read a launchd plist file via ``plistlib`` and return its root dict, or ``None`` on error."""
        try:
            if not path.is_file():
                return None
            with path.open("rb") as fh:
                data = plistlib.load(fh)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @classmethod
    def _resolve_host_tzinfo(
        cls,
        localtime_path: Path = Path("/etc/localtime"),
        *,
        fallback_now: datetime | None = None,
    ) -> Any:
        """Resolve host IANA timezone from ``/etc/localtime`` symlink via ``ZoneInfo``, falling back to ``datetime.now().astimezone().tzinfo``."""
        for target_fn in (
            lambda: os.readlink(localtime_path),
            lambda: str(localtime_path.resolve()),
        ):
            try:
                raw_target = target_fn()
                marker = "zoneinfo/"
                idx = raw_target.find(marker)
                if idx != -1:
                    iana_name = raw_target[idx + len(marker) :].strip("/")
                    if iana_name:
                        return ZoneInfo(iana_name)
            except Exception:
                continue
        if fallback_now is not None and isinstance(fallback_now.tzinfo, ZoneInfo):
            return fallback_now.tzinfo
        return datetime.now().astimezone().tzinfo or timezone.utc

    @classmethod
    def _compute_next_calendar_interval_iso(
        cls,
        plist_data: dict[str, Any] | None,
        *,
        now: datetime | None = None,
        localtime_path: Path = Path("/etc/localtime"),
    ) -> str | None:
        """Compute next run ISO 8601 timestamp in host local timezone from ``StartCalendarInterval``."""
        if not isinstance(plist_data, dict):
            return None
        raw_interval = plist_data.get("StartCalendarInterval")
        intervals: list[dict[str, Any]] = []
        if isinstance(raw_interval, dict):
            intervals.append(raw_interval)
        elif isinstance(raw_interval, list):
            intervals.extend(x for x in raw_interval if isinstance(x, dict))
        if not intervals:
            return None

        for item in intervals:
            if any(k in item for k in ("Weekday", "Day", "Month")):
                return None

        tz = cls._resolve_host_tzinfo(
            localtime_path=localtime_path,
            fallback_now=now,
        )
        now_local = (now or datetime.now(tz)).astimezone(tz)
        candidates: list[datetime] = []
        for item in intervals:
            raw_hour = item.get("Hour")
            raw_min = item.get("Minute", 0)
            if not isinstance(raw_hour, int) or isinstance(raw_hour, bool):
                continue
            if not isinstance(raw_min, int) or isinstance(raw_min, bool):
                continue
            if not (0 <= raw_hour <= 23 and 0 <= raw_min <= 59):
                continue
            cand = datetime(
                now_local.year,
                now_local.month,
                now_local.day,
                raw_hour,
                raw_min,
                0,
                tzinfo=tz,
            )
            if cand <= now_local:
                next_day = now_local.date() + timedelta(days=1)
                cand = datetime(
                    next_day.year,
                    next_day.month,
                    next_day.day,
                    raw_hour,
                    raw_min,
                    0,
                    tzinfo=tz,
                )
            candidates.append(cand)
        if not candidates:
            return None
        return min(candidates).isoformat()

    def _inspect_pg_backup_job_sync(self) -> dict[str, Any]:
        """Inspect ``com.dalton.pg-backup`` launchd status, log file, and schedule."""
        job: dict[str, Any] = {
            "key": "ops-db-backup",
            "kind": "ops_db_backup",
            "title": "数据库备份",
            "status": "unknown",
            "last_run_at": None,
            "last_result": None,
            "next_run_at": None,
        }
        try:
            job["status"] = self._probe_launchd_label_status("com.dalton.pg-backup")
        except Exception:
            job["status"] = "unknown"

        try:
            log_path = self.cfg.pg_backup_log_file
            if log_path.is_file():
                lines = self._read_complete_tail_lines(log_path, max_lines=25)
                non_empty = [ln.strip() for ln in lines if ln.strip()]
                if non_empty:
                    last_line = non_empty[-1]
                    parts = last_line.split()
                    if len(parts) >= 3:
                        naive_dt = datetime.strptime(
                            f"{parts[0]} {parts[1]}",
                            "%Y-%m-%d %H:%M:%S",
                        )
                        job["last_run_at"] = naive_dt.astimezone().isoformat()
                        status_tok = parts[2].lower()
                        if status_tok == "ok":
                            size_tok = (
                                parts[3]
                                if len(parts) >= 4
                                and not parts[3].startswith(("/", "~"))
                                else ""
                            )
                            res_text = f"成功（{size_tok}）" if size_tok else "成功"
                        else:
                            res_text = "失败"
                        job["last_result"] = _scrub_local_paths(res_text)
        except Exception:
            pass

        try:
            plist_data = self._load_plist_dict(self.cfg.pg_backup_plist_file)
            job["next_run_at"] = self._compute_next_calendar_interval_iso(plist_data)
        except Exception:
            pass

        return job

    def _inspect_backend_watchdog_job_sync(self) -> dict[str, Any]:
        """Inspect ``com.dalton.backend-watchdog`` launchd status, failures file, and next interval."""
        job: dict[str, Any] = {
            "key": "ops-backend-watchdog",
            "kind": "ops_backend_watchdog",
            "title": "后端看门狗",
            "status": "unknown",
            "last_run_at": None,
            "last_result": None,
            "next_run_at": None,
        }
        try:
            job["status"] = self._probe_launchd_label_status("com.dalton.backend-watchdog")
        except Exception:
            job["status"] = "unknown"

        start_interval = 60
        try:
            plist_data = self._load_plist_dict(self.cfg.backend_watchdog_plist_file)
            if isinstance(plist_data, dict):
                raw_iv = plist_data.get("StartInterval")
                if isinstance(raw_iv, int) and not isinstance(raw_iv, bool) and raw_iv > 0:
                    start_interval = raw_iv
        except Exception:
            pass

        try:
            failures_path = self.cfg.watchdog_failures_file
            if failures_path.is_file():
                mtime = failures_path.stat().st_mtime
                last_run_dt = datetime.fromtimestamp(mtime).astimezone()
                job["last_run_at"] = last_run_dt.isoformat()
                raw_text = failures_path.read_text(encoding="utf-8", errors="replace").strip()
                if raw_text.isascii() and raw_text.isdigit():
                    count = int(raw_text)
                    job["last_result"] = "健康" if count == 0 else f"连续失败 {count} 次"
                else:
                    job["last_result"] = None
                job["next_run_at"] = (
                    last_run_dt + timedelta(seconds=start_interval)
                ).isoformat()
        except Exception:
            pass

        return job

    def _collect_ops_jobs_sync(self) -> list[dict[str, Any]]:
        """Synchronously inspect host launchd operational jobs (intended to run via ``asyncio.to_thread``)."""
        return [
            self._inspect_pg_backup_job_sync(),
            self._inspect_backend_watchdog_job_sync(),
        ]

    async def handle_get_ops_jobs(self) -> tuple[int, dict[str, Any]]:
        """Handle `GET /jobs/ops` for read-only host operational jobs."""
        try:
            jobs = await asyncio.to_thread(self._collect_ops_jobs_sync)
            return 200, {"ok": True, "jobs": jobs}
        except Exception:
            return 200, {
                "ok": True,
                "jobs": [
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
                        "key": "ops-backend-watchdog",
                        "kind": "ops_backend_watchdog",
                        "title": "后端看门狗",
                        "status": "unknown",
                        "last_run_at": None,
                        "last_result": None,
                        "next_run_at": None,
                    },
                ],
            }

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

    # Dalton-owned alias store. OpenClaw's company-wiki config.json stays
    # read-only for the bridge; aliases an internal member registers while
    # ingesting live in this separate file and are merged after config.json
    # (config.json wins), so neither system rewrites the other's file.
    def _wiki_alias_file(self) -> Path:
        custom = os.environ.get("DALTON_WIKI_ALIAS_FILE", "").strip()
        if custom:
            return Path(custom).expanduser()
        return self.cfg.wiki_ingest_audit_file.with_name("wiki_aliases.json")

    def _parse_wiki_alias_file_strict(self) -> dict[str, dict[str, dict[str, Any]]]:
        """`{target_type: {alias_lower: record}}`; raises on I/O or format corruption."""
        path = self._wiki_alias_file()
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Wiki alias store root must be a JSON object")
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for ttype, entries in raw.items():
            if ttype not in ("company", "sector") or not isinstance(entries, dict):
                raise ValueError("Wiki alias store sections must be company/sector objects")
            section: dict[str, dict[str, Any]] = {}
            for key, rec in entries.items():
                if not isinstance(rec, dict) or not isinstance(rec.get("slug"), str):
                    raise ValueError("Wiki alias records must carry a slug")
                section[str(key)] = rec
            out[ttype] = section
        return out

    def _read_wiki_aliases(self, target_type: str) -> dict[str, str]:
        """`alias_lower -> slug` from the Dalton alias store.

        Raises `WikiAliasStoreError` when the store exists but cannot be read:
        an empty map there would quietly file a registered alias under a new
        directory of the same name.
        """
        try:
            store = self._parse_wiki_alias_file_strict()
        except Exception as exc:
            logger.error("Failed reading wiki alias store: %s", type(exc).__name__)
            raise WikiAliasStoreError(type(exc).__name__) from exc
        return {
            k: str(rec["slug"]).strip()
            for k, rec in (store.get(target_type) or {}).items()
            if str(rec.get("slug") or "").strip()
        }

    def _config_wiki_aliases(self, target_type: str, wiki_cfg: dict[str, Any]) -> dict[str, str]:
        """`alias_lower -> slug` from OpenClaw config.json (nested per type, then flat)."""
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
        return alias_map

    def _plan_wiki_alias_additions(
        self,
        target_type: str,
        resolved_slug: str,
        raw_aliases: Any,
        wiki_cfg: dict[str, Any],
    ) -> tuple[list[str], list[str], dict[str, Any] | None]:
        """Validate requested aliases for `resolved_slug`: `(to_add, already, error)`.

        An alias that already names another company (a config/Dalton alias or
        another directory) is a conflict and fails the whole request, so a
        mistaken mapping never lands silently.
        """
        if raw_aliases is None:
            return [], [], None
        if not isinstance(raw_aliases, list) or len(raw_aliases) > _WIKI_ALIASES_MAX:
            return [], [], {
                "ok": False,
                "status": "bad_request",
                "error_code": "invalid_aliases",
                "error": f"aliases 必须是字符串列表，最多 {_WIKI_ALIASES_MAX} 个。",
            }
        config_map = self._config_wiki_aliases(target_type, wiki_cfg)
        try:
            dalton_map = self._read_wiki_aliases(target_type)
        except WikiAliasStoreError:
            return [], [], dict(_WIKI_ALIAS_STORE_UNREADABLE)
        base_dir = self._resolve_wiki_base_dir(target_type, wiki_cfg)
        existing_dirs: dict[str, str] = {}
        if base_dir.is_dir():
            try:
                existing_dirs = {d.name.lower(): d.name for d in base_dir.iterdir() if d.is_dir()}
            except OSError:
                existing_dirs = {}
        to_add: list[str] = []
        already: list[str] = []
        seen: set[str] = set()
        slug_lower = resolved_slug.lower()
        for item in raw_aliases:
            alias = item.strip() if isinstance(item, str) else ""
            if not alias or len(alias) > 60 or not _WIKI_ALIAS_TEXT_RE.fullmatch(alias):
                return [], [], {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_alias",
                    "alias": item if isinstance(item, str) else None,
                    "error": "别名须为 1–60 个字符的公司名称或股票代码，不能含路径字符或控制字符。",
                }
            key = alias.lower()
            if key in seen or key == slug_lower:
                continue
            seen.add(key)
            mapped = config_map.get(key) or dalton_map.get(key) or existing_dirs.get(key)
            if mapped is not None and mapped.lower() != slug_lower:
                return [], [], {
                    "ok": False,
                    "status": "alias_conflict",
                    "error_code": "alias_conflict",
                    "alias": alias,
                    "mapped_to": mapped,
                    "target": resolved_slug,
                    "error": f"别名 '{alias}' 已指向 '{mapped}'，不能再指向 '{resolved_slug}'。",
                }
            if mapped is not None:
                already.append(alias)
            else:
                to_add.append(alias)
        return to_add, already, None

    def _save_wiki_aliases(
        self,
        target_type: str,
        resolved_slug: str,
        aliases: list[str],
        added_by: Any,
    ) -> list[str]:
        """Persist `aliases -> resolved_slug` atomically under flock; returns those written.

        Re-checks each alias inside the lock and skips one another writer
        mapped elsewhere in the meantime. Refuses to overwrite a corrupted store.
        """
        import fcntl

        if not aliases:
            return []
        path = self._wiki_alias_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        written: list[str] = []
        with lock_path.open("a", encoding="utf-8") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    store = self._parse_wiki_alias_file_strict()
                except Exception as exc:
                    logger.error(
                        "Refusing to overwrite corrupted wiki alias store %s: %s",
                        path,
                        type(exc).__name__,
                    )
                    return []
                section = store.setdefault(target_type, {})
                now_iso = datetime.now(timezone.utc).isoformat()
                # A directory or config.json alias that appeared since the
                # check (another writer created the company meanwhile) wins:
                # the alias is skipped rather than shadowing that company.
                # config.json is re-read here, not taken from the request:
                # an admin may have mapped the name meanwhile.
                fresh_cfg = self._load_company_wiki_config()
                taken = {**self._config_wiki_aliases(target_type, fresh_cfg)}
                base_dir = self._resolve_wiki_base_dir(target_type, fresh_cfg)
                if base_dir.is_dir():
                    for d in base_dir.iterdir():
                        if d.is_dir():
                            taken.setdefault(d.name.lower(), d.name)
                for alias in aliases:
                    key = alias.lower()
                    other = taken.get(key)
                    if other is not None and other.lower() != resolved_slug.lower():
                        logger.warning("Wiki alias %r now names %r; skipped", alias, other)
                        continue
                    prior = section.get(key)
                    if prior is not None:
                        if str(prior.get("slug") or "").lower() == resolved_slug.lower():
                            continue
                        logger.warning("Wiki alias %r was mapped concurrently; skipped", alias)
                        continue
                    section[key] = {
                        "alias": alias,
                        "slug": resolved_slug,
                        "added_by": added_by,
                        "added_at": now_iso,
                    }
                    written.append(alias)
                if not written:
                    return []
                tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                with tmp_path.open("w", encoding="utf-8") as fh:
                    json.dump(store, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_path, path)
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        return written

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

        # 1. Check aliases: config.json (read-only) first, then the Dalton alias store
        alias_map = self._config_wiki_aliases(target_type, wiki_cfg)
        try:
            dalton_aliases = self._read_wiki_aliases(target_type)
        except WikiAliasStoreError:
            return None, False, dict(_WIKI_ALIAS_STORE_UNREADABLE)
        for k, v in dalton_aliases.items():
            alias_map.setdefault(k, v)

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

        # 3. Unmapped A-share / HK / Chinese names or numeric tickers -> do NOT auto-add alias.
        # The answer lists the existing directory names: the company is often
        # already there under an English slug (东方雨虹 → YUHONG), and the
        # caller can retry with it instead of waiting on an admin.
        all_targets = sorted(existing_dirs.values())
        existing_targets = all_targets[:_WIKI_EXISTING_TARGETS_MAX]
        targets_truncated = len(all_targets) > len(existing_targets)
        if _WIKI_NEEDS_ALIAS_RE.search(cleaned) or not cleaned.isascii():
            return None, False, {
                "ok": False,
                "status": "needs_alias",
                "error_code": "needs_alias",
                "needs_alias": True,
                "target": cleaned,
                "target_type": target_type,
                "existing_targets": existing_targets,
                "existing_targets_truncated": targets_truncated,
                "error": (
                    f"'{cleaned}' 未在 config.json 配置别名 (needs_alias)；"
                    "A股/港股/中文名称不自动添加别名。"
                ),
                "hint": _WIKI_NEEDS_ALIAS_HINT,
            }

        if cleaned.isdigit():
            return None, False, {
                "ok": False,
                "status": "needs_alias",
                "error_code": "numeric_ticker_rejected",
                "needs_alias": True,
                "target": cleaned,
                "target_type": target_type,
                "existing_targets": existing_targets,
                "existing_targets_truncated": targets_truncated,
                "error": (
                    f"纯数字 ticker '{cleaned}' 拒绝直接建目录 (needs_alias)。"
                ),
                "hint": _WIKI_NEEDS_ALIAS_HINT,
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

    # ------------------------------------------------------------------
    # Created-doc registry (C1: the /feishu/doc/grant scope). Same storage
    # discipline as the wiki idempotency store beside it — a JSON file in the
    # bridge's state directory, written under an flock atomically.
    # ------------------------------------------------------------------

    def _feishu_doc_registry_file(self) -> Path:
        """Return the path to the created-doc registry file."""
        custom = getattr(self.cfg, "feishu_doc_registry_file", None)
        if custom is not None:
            return Path(custom)
        return self.cfg.wiki_ingest_audit_file.with_name("feishu_doc_registry.json")

    def _read_feishu_doc_registry(self) -> dict[str, dict[str, Any]]:
        """Read `document_id -> record` from the created-doc registry; {} on any failure."""
        reg_path = self._feishu_doc_registry_file()
        if not reg_path.is_file():
            return {}
        try:
            raw = json.loads(reg_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error(
                "Failed reading feishu doc registry %s: %s", reg_path, type(exc).__name__
            )
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _register_feishu_created_doc(
        self,
        document_id: str,
        *,
        creator_email: str,
        title: str = "",
        space_id: str = "",
        node_token: str = "",
    ) -> None:
        """Record a doc this bridge created, so `/feishu/doc/grant` can scope itself (C1).

        The registry names the creating user's email — the same address the
        create-time auto-grant used — and the grant endpoint accepts only it.
        A failed write only logs: an unregistered doc then fails closed at
        grant time, which is the safe direction.
        """
        import fcntl

        reg_path = self._feishu_doc_registry_file()
        try:
            reg_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = reg_path.with_suffix(".lock")
            record = {
                "creator_email": creator_email.strip().lower(),
                "title": title,
                "space_id": space_id,
                "node_token": node_token,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            with lock_path.open("a", encoding="utf-8") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    current: dict[str, Any] = {}
                    if reg_path.exists():
                        try:
                            parsed = json.loads(reg_path.read_text(encoding="utf-8"))
                            if isinstance(parsed, dict):
                                current = {
                                    str(k): v for k, v in parsed.items() if isinstance(v, dict)
                                }
                        except Exception as exc:
                            logger.error(
                                "Refusing to overwrite corrupted feishu doc registry %s: %s",
                                reg_path,
                                type(exc).__name__,
                            )
                            return
                    current[document_id] = record
                    tmp_fd, tmp_name = tempfile.mkstemp(
                        prefix=".feishu_reg_",
                        suffix=".tmp",
                        dir=str(reg_path.parent),
                    )
                    os.close(tmp_fd)
                    tmp_path = Path(tmp_name)
                    try:
                        os.chmod(tmp_path, 0o600)
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(current, f, ensure_ascii=False, indent=2)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(str(tmp_path), str(reg_path))
                        dir_fd = os.open(str(reg_path.parent), os.O_RDONLY)
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
        except Exception:
            logger.warning(
                "Failed registering created feishu doc %s", document_id, exc_info=True
            )

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
        allow_similar: bool = False,
    ) -> dict[str, Any] | None:
        """Check only the single target markdown file on disk for sha256 or text similarity match.

        ``allow_similar`` (a confirmed "ingest anyway" answer) skips the
        similarity verdict but never the exact-fingerprint one.
        """
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
            if norm_text and not allow_similar:
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
        allow_similar: bool = False,
    ) -> dict[str, Any] | None:
        """Check whether `content_sha256` or a highly similar same-(slug, date, type) doc already exists.

        With ``allow_similar`` (the user answered a suspected-duplicate preview
        with an explicit "ingest anyway"), the text-similarity verdicts are
        skipped so a revision of an existing memo can land; the exact sha256
        fingerprint check always runs — byte-identical content never writes a
        second copy regardless of the flag.
        """
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
        if norm_text and not allow_similar:
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
            allow_similar=allow_similar,
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
        """Handle `POST /wiki/ingest` (admin token only) to validate, preview, or ingest into company-wiki.

        Writes run one at a time (the bridge is one process): target
        resolution, the alias check, the document write and the alias save
        all see one state of directories and aliases, so a name can never be
        registered for one company while another write creates it as a
        directory. Previews do not wait. Wiki writes are rare and rate-limited.
        """
        if bool(body.get("dry_run", False)):
            return await self._handle_wiki_ingest_unlocked(body)
        # The wait is bounded: the backend gives the whole call a budget only a
        # few seconds above the CLI's own timeout, so a write queued longer
        # would be reported failed to the user and then land anyway. A busy
        # answer writes nothing and the caller retries.
        try:
            await asyncio.wait_for(
                self._wiki_write_lock.acquire(), timeout=WIKI_WRITE_QUEUE_WAIT_S
            )
        except TimeoutError:
            return 503, {
                "ok": False,
                "status": "busy",
                "error_code": "wiki_write_busy",
                "retryable": True,
                "error": "另一份资料正在写入 company-wiki，本次没有写入；请稍后重试（同一请求重试不会重复入库）。",
            }
        try:
            return await self._handle_wiki_ingest_unlocked(body)
        finally:
            self._wiki_write_lock.release()

    async def _handle_wiki_ingest_unlocked(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
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
                "next_action": (
                    "改用 ingest_sales_note（speaker='sell_side' 时 ingest_research 会自动路由）："
                    "institution=机构名、title=标题、kind=研报|点评|卖方电话会|销售观点，"
                    "content/file 与本次相同即可。"
                ),
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
                    "next_action": (
                        "改用 ingest_sales_note（speaker='sell_side' 时 ingest_research 会自动路由）："
                        "institution 填该卖方机构名，content/file 与本次相同即可。"
                    ),
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
        # A caller that already showed the user a suspected-duplicate preview
        # and got an explicit "ingest anyway" answers with this flag: the
        # similarity verdicts step aside (a same-content revision is the
        # workflow), while the exact sha256 fingerprint check stays.
        allow_similar = bool(body.get("allow_similar", False))

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
            status_code = 503 if (slug_err or {}).get("status") == "alias_store_unreadable" else 400
            return status_code, slug_err or {"ok": False, "status": "invalid_slug", "error": "Invalid target"}
        aliases_to_add, aliases_existing, alias_err = self._plan_wiki_alias_additions(
            target_type, resolved_slug, body.get("aliases"), wiki_cfg
        )
        if alias_err is not None:
            return _wiki_alias_error_status(alias_err), alias_err

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
        sell_side_next_action = (
            "若确认是卖方观点，改用 ingest_sales_note（speaker='sell_side' 时 ingest_research 会自动路由）："
            "institution=机构名、title=标题、kind=研报|点评|卖方电话会|销售观点，content/file 与本次相同。"
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
                "sell_side_next_action": sell_side_next_action,
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
                "sell_side_next_action": sell_side_next_action,
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
            replay = _build_replay_response(prior_entry)
            # A retry that brings aliases the first write lacked registers them
            # now: the document is already filed under this slug.
            if aliases_to_add and str(replay.get("target") or "").lower() == resolved_slug.lower():
                added: list[str] = []
                try:
                    added = self._save_wiki_aliases(
                        target_type, resolved_slug, aliases_to_add, ingested_by_norm
                    )
                except Exception as exc:
                    logger.warning("Failed saving wiki aliases on replay: %s", type(exc).__name__)
                replay["aliases_added"] = added
                replay["aliases_not_added"] = [a for a in aliases_to_add if a not in added]
            return 200, replay

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
                allow_similar=allow_similar,
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
                "sell_side_next_action": sell_side_next_action,
                "duplicate": False,
                "aliases_to_add": aliases_to_add,
                "aliases_existing": aliases_existing,
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
                allow_similar=allow_similar,
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

            # Aliases are registered only once the document is filed under the
            # slug, so a failed write never leaves a mapping behind.
            aliases_added: list[str] = []
            if aliases_to_add:
                try:
                    aliases_added = self._save_wiki_aliases(
                        target_type, resolved_slug, aliases_to_add, ingested_by_norm
                    )
                except Exception as exc:
                    logger.warning("Failed saving wiki aliases: %s", type(exc).__name__)
                if aliases_added:
                    try:
                        self._append_wiki_audit_entry(
                            {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "request_id": None,
                                "ingested_by": ingested_by_norm,
                                "ingested_via": ingested_via_norm,
                                "target_type": target_type,
                                "target": resolved_slug,
                                "result": "ok",
                                "status": "aliases_added",
                                "aliases": aliases_added,
                            }
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed writing wiki alias audit entry: %s", type(exc).__name__
                        )

            return 200, {
                "aliases_added": aliases_added,
                "aliases_not_added": [a for a in aliases_to_add if a not in aliases_added],
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
                "sell_side_next_action": sell_side_next_action,
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

    # ------------------------------------------------------------------
    # Sales-notes ingest (D1): OpenClaw market-digest `add-note`, host-side
    # ------------------------------------------------------------------

    _SALES_NOTES_KINDS = frozenset({"研报", "点评", "卖方电话会", "销售观点"})
    _SALES_NOTES_AUDIT_REL = "var/dalton/sales_notes_ingest_audit.jsonl"

    def _resolve_market_digest_run_sh(self) -> Path:
        explicit = getattr(self.cfg, "market_digest_run_sh", None)
        if isinstance(explicit, Path):
            return explicit
        return self.cfg.ocw_root / "skills" / "market-digest" / "run.sh"

    def _sales_notes_audit_path(self) -> Path:
        return self.cfg.ocw_root / self._SALES_NOTES_AUDIT_REL

    def _append_sales_notes_audit_entry(self, entry: dict[str, Any]) -> None:
        path = self._sales_notes_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _find_sales_notes_audit_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        path = self._sales_notes_audit_path()
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict) and parsed.get("request_id") == request_id:
                        return parsed
        except OSError:
            return None
        return None

    async def handle_sales_notes_ingest(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle `POST /sales-notes/ingest` (admin token only): sell-side material into sales notes.

        The host-side twin of `/wiki/ingest`, aimed at OpenClaw's
        `market-digest/run.sh --json add-note`: the CLI and its credentials
        never enter a sandbox. File types, size ceilings and the secret scan
        are the wiki ingest's; dedup rides the CLI's own doc_key (a repeat of
        the same body returns `duplicate`), with request-id replay on top.
        """
        institution = str(body.get("institution") or "").strip()
        title = str(body.get("title") or "").strip()
        if not institution:
            return 400, {"ok": False, "status": "bad_request", "error_code": "missing_institution",
                         "error": "institution 必填（卖方机构名，如 'Morgan Stanley'）"}
        if not title:
            return 400, {"ok": False, "status": "bad_request", "error_code": "missing_title",
                         "error": "title 必填"}
        try:
            institution = _validate_clean_text(institution, field_name="institution", max_len=200)
            title = _validate_clean_text(title, field_name="title", max_len=500)
        except ValueError as exc:
            return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_text", "error": str(exc)}

        analyst: str | None = None
        raw_analyst = body.get("analyst")
        if isinstance(raw_analyst, str) and raw_analyst.strip():
            try:
                analyst = _validate_clean_text(raw_analyst, field_name="analyst", max_len=200)
            except ValueError as exc:
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_analyst", "error": str(exc)}

        kind: str | None = None
        raw_kind = body.get("kind")
        if raw_kind is not None:
            kind = str(raw_kind).strip()
            if kind and kind not in self._SALES_NOTES_KINDS:
                return 400, {
                    "ok": False,
                    "status": "bad_request",
                    "error_code": "invalid_kind",
                    "allowed_kinds": sorted(self._SALES_NOTES_KINDS),
                    "error": f"kind 必须是 {sorted(self._SALES_NOTES_KINDS)} 之一或不传",
                }

        doc_date: str | None = None
        raw_doc_date = body.get("doc_date")
        if raw_doc_date is not None and str(raw_doc_date).strip():
            doc_date = str(raw_doc_date).strip()
            if not _YMD_RE.fullmatch(doc_date):
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_doc_date",
                             "error": "doc_date 必须为 YYYY-MM-DD"}
        received_date: str | None = None
        raw_received = body.get("date")
        if raw_received is not None and str(raw_received).strip():
            received_date = str(raw_received).strip()
            if not _YMD_RE.fullmatch(received_date):
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_date",
                             "error": "date 必须为 YYYY-MM-DD"}

        source_url: str | None = None
        raw_source_url = body.get("source_url")
        if raw_source_url is not None and str(raw_source_url).strip():
            url_cand = str(raw_source_url).strip()
            if (
                len(url_cand) > 1000
                or any(ord(ch) < 32 or ch.isspace() for ch in url_cand)
                or not url_cand.startswith(("http://", "https://"))
            ):
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_source_url",
                             "error": "source_url 必须为有效的 http/https URL（<=1000 字符）"}
            source_url = url_cand

        tickers: list[str] = []
        raw_tickers = body.get("tickers")
        if raw_tickers is not None:
            if not isinstance(raw_tickers, list):
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_tickers",
                             "error": "tickers 必须为字符串列表"}
            for t in raw_tickers[:10]:
                t_clean = str(t or "").strip().upper()
                if t_clean and len(t_clean) <= 16 and re.fullmatch(r"[A-Z0-9.\-^/=]+", t_clean):
                    tickers.append(t_clean)
            if not tickers and raw_tickers:
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_tickers",
                             "error": "tickers 中的代码均不合法"}

        raw_by = body.get("ingested_by")
        if isinstance(raw_by, dict):
            by_user_id = str(raw_by.get("user_id") or "").strip()
            by_email = str(raw_by.get("email") or "").strip()
        else:
            by_user_id = str(raw_by or "").strip()
            by_email = ""
        if not by_user_id:
            return 400, {"ok": False, "status": "bad_request", "error_code": "missing_ingested_by",
                         "error": "ingested_by.user_id 必填"}
        ingested_by_norm = {"user_id": by_user_id, "email": by_email}
        raw_via = body.get("ingested_via")
        ingested_via_norm = (
            {
                "workspace_id": str(raw_via.get("workspace_id") or "").strip(),
                "thread_id": str(raw_via.get("thread_id") or "").strip(),
            }
            if isinstance(raw_via, dict)
            else {"workspace_id": "", "thread_id": ""}
        )

        raw_req_id = body.get("request_id")
        request_id: str | None = (
            str(raw_req_id).strip() if isinstance(raw_req_id, str) and raw_req_id.strip() else None
        )
        dry_run = bool(body.get("dry_run", False))

        has_content = body.get("content") is not None
        has_file = body.get("file") is not None
        if has_content == has_file:
            return 400, {"ok": False, "status": "bad_request", "error_code": "content_or_file_required",
                         "error": "content 与 file 必须二选一传入"}

        content_text: str | None = None
        file_bytes: bytes | None = None
        upload_filename: str | None = None
        scan_text = ""
        content_sha256 = ""

        if has_content:
            raw_content = body.get("content")
            if not isinstance(raw_content, str) or not raw_content.strip():
                return 400, {"ok": False, "status": "empty_content", "error_code": "empty_content",
                             "error": "content 不能为空"}
            if len(raw_content) > WIKI_INGEST_MAX_TEXT_CHARS:
                return 400, {"ok": False, "status": "payload_too_large", "error_code": "text_too_large",
                             "max_chars": WIKI_INGEST_MAX_TEXT_CHARS,
                             "error": f"文本长度 ({len(raw_content)}) 超过 {WIKI_INGEST_MAX_TEXT_CHARS} 字符上限"}
            content_text = raw_content
            scan_text = content_text
            content_sha256 = hashlib.sha256(
                _normalize_wiki_text_for_hash(content_text).encode("utf-8")
            ).hexdigest()
        else:
            raw_file = body.get("file")
            if not isinstance(raw_file, dict):
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_file_payload",
                             "error": "file 必须为包含 filename 与文件字节的对象"}
            raw_fname = str(raw_file.get("filename") or raw_file.get("name") or "").strip()
            if not raw_fname or "/" in raw_fname or "\\" in raw_fname or ".." in raw_fname or raw_fname.startswith("."):
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_filename",
                             "error": "file.filename 非法或为空"}
            ext = Path(raw_fname).suffix.lower()
            if ext not in WIKI_INGEST_ALLOWED_EXTENSIONS:
                return 400, {"ok": False, "status": "invalid_extension", "error_code": "invalid_extension",
                             "allowed_extensions": sorted(WIKI_INGEST_ALLOWED_EXTENSIONS),
                             "error": f"不支持的文件扩展名 '{ext}'；仅允许: {', '.join(sorted(WIKI_INGEST_ALLOWED_EXTENSIONS))}"}
            upload_filename = Path(raw_fname).name
            decoded_bytes, decode_err = _extract_file_bytes(raw_file)
            if decode_err is not None or decoded_bytes is None:
                return 400, {"ok": False, "status": "bad_request", "error_code": "invalid_file_bytes",
                             "error": decode_err or "无法解析文件字节"}
            if len(decoded_bytes) == 0:
                return 400, {"ok": False, "status": "empty_content", "error_code": "empty_file",
                             "error": "文件内容不能为空"}
            if len(decoded_bytes) > WIKI_INGEST_MAX_FILE_BYTES:
                return 400, {"ok": False, "status": "payload_too_large", "error_code": "file_too_large",
                             "max_bytes": WIKI_INGEST_MAX_FILE_BYTES,
                             "error": f"文件大小 ({len(decoded_bytes)} bytes) 超过 20 MB 上限"}
            file_bytes = decoded_bytes
            if ext in (".md", ".txt"):
                scan_text = file_bytes.decode("utf-8", errors="replace")
                content_sha256 = hashlib.sha256(
                    _normalize_wiki_text_for_hash(scan_text).encode("utf-8")
                ).hexdigest()
            else:
                binary_scan, _extracted, _ok, _note = _inspect_binary_file_for_wiki(file_bytes, ext)
                scan_text = binary_scan
                content_sha256 = hashlib.sha256(file_bytes).hexdigest()

        secret_kind = _detect_wiki_ingest_secret(scan_text, "", "", "")
        if secret_kind is not None:
            return 400, {"ok": False, "status": "secret_detected", "error_code": "secret_detected",
                         "secret_kind": secret_kind,
                         "error": "检测到疑似密钥或敏感凭证（API key / token / 私钥 PEM），拒绝入库。",
                         "hint": "请先移除正文或文件中的密钥、Token 或私钥内容后再入库。"}

        base_audit = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id,
            "ingested_by": ingested_by_norm,
            "ingested_via": ingested_via_norm,
            "institution": institution,
            "title": title,
            "kind": kind,
            "doc_date": doc_date,
            "sha256": content_sha256,
        }

        # request-id replay: the same confirmed request answers from its record
        if not dry_run and request_id is not None:
            prior = self._find_sales_notes_audit_by_request_id(request_id)
            if prior is not None and prior.get("result") == "ok":
                prior_out = dict(prior.get("cli_result") or {})
                prior_out.update(
                    {
                        "ok": True,
                        "status": "ingested",
                        "idempotent_replay": True,
                        "request_id": request_id,
                        "audit_path": self._SALES_NOTES_AUDIT_REL,
                    }
                )
                return 200, prior_out

        run_sh = self._resolve_market_digest_run_sh()
        env = self._build_wiki_subprocess_env()

        cmd: list[str] = [str(run_sh), "--json", "add-note"]
        cmd.extend(["--institution", institution])
        cmd.extend(["--title", title])
        if analyst:
            cmd.extend(["--analyst", analyst])
        if kind:
            cmd.extend(["--kind", kind])
        if doc_date:
            cmd.extend(["--doc-date", doc_date])
        if received_date:
            cmd.extend(["--date", received_date])
        if source_url:
            cmd.extend(["--source-url", source_url])
        if tickers:
            cmd.extend(["--tickers", ",".join(tickers)])
        if dry_run:
            cmd.append("--dry-run")

        self.cfg.wiki_ingest_temp_dir.mkdir(parents=True, exist_ok=True)
        req_tmp_dir = Path(tempfile.mkdtemp(prefix="sales_note_", dir=str(self.cfg.wiki_ingest_temp_dir)))
        os.chmod(req_tmp_dir, 0o700)
        tmp_payload: Path | None = None
        try:
            if file_bytes is not None and upload_filename is not None:
                tmp_payload = req_tmp_dir / upload_filename
                fd = os.open(str(tmp_payload), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, file_bytes)
                finally:
                    os.close(fd)
                cmd.extend(["--file", str(tmp_payload)])
            else:
                assert content_text is not None
                tmp_payload = req_tmp_dir / "content.md"
                fd = os.open(str(tmp_payload), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, content_text.encode("utf-8"))
                finally:
                    os.close(fd)
                cmd.extend(["--content", f"@{tmp_payload}"])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.cfg.ocw_root),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, _stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.cfg.wiki_ingest_timeout_s
                )
            except asyncio.TimeoutError:
                proc.kill()
                await _reap_process(proc)
                return 502, {"ok": False, "status": "timeout", "request_id": request_id,
                             "error": f"add-note 超过 {self.cfg.wiki_ingest_timeout_s}s 未完成"}
            cli_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            cli_payload: dict[str, Any] = {}
            last_line = cli_text.splitlines()[-1].strip() if cli_text else ""
            if last_line.startswith("{"):
                try:
                    parsed_cli = json.loads(last_line)
                    if isinstance(parsed_cli, dict):
                        cli_payload = parsed_cli
                except ValueError:
                    cli_payload = {}
            if proc.returncode != 0 and not cli_payload:
                return 502, {
                    "ok": False,
                    "status": "cli_error",
                    "request_id": request_id,
                    "exit_code": proc.returncode,
                    "error": "market-digest add-note 运行失败（详见桥接日志）",
                }

            status = str(cli_payload.get("status") or cli_payload.get("result") or "").strip().lower()
            is_duplicate = status == "duplicate" or cli_payload.get("duplicate") is True
            outcome_ok = proc.returncode == 0 and not is_duplicate

            if dry_run:
                return 200, {
                    "ok": True,
                    "status": "dry_run",
                    "dry_run": True,
                    # Canonical duplicate verdict (D1/r3): derived here from the
                    # CLI's own status/result/duplicate spelling, so callers
                    # read one field instead of replaying this normalization.
                    "duplicate": is_duplicate,
                    "request_id": request_id,
                    "institution": institution,
                    "title": title,
                    "kind": kind,
                    "doc_date": doc_date,
                    "tickers": tickers,
                    "upload_filename": upload_filename,
                    "size_bytes": len(file_bytes) if file_bytes is not None else len((content_text or "").encode("utf-8")),
                    "char_count": len(content_text) if content_text is not None else None,
                    "sha256": content_sha256,
                    "cli_preview": cli_payload,
                }

            audit_entry = {
                **base_audit,
                "result": "ok" if outcome_ok else ("duplicate" if is_duplicate else "error"),
                "status": status or ("ok" if outcome_ok else "error"),
                "cli_result": cli_payload,
            }
            try:
                self._append_sales_notes_audit_entry(audit_entry)
            except Exception as exc:
                logger.warning("Failed writing sales-notes audit entry: %s", type(exc).__name__)

            if not outcome_ok and not is_duplicate:
                return 502, {
                    "ok": False,
                    "status": "cli_error",
                    "request_id": request_id,
                    "error": str(cli_payload.get("error") or "market-digest add-note 运行失败"),
                }

            response = {
                "ok": True,
                "status": "duplicate" if is_duplicate else "ingested",
                "dry_run": False,
                "request_id": request_id,
                "duplicate": is_duplicate,
                "institution": institution,
                "title": title,
                "kind": kind,
                "sha256": content_sha256,
                "cli_result": cli_payload,
                "audit_path": self._SALES_NOTES_AUDIT_REL,
            }
            doc_key = str(cli_payload.get("doc_key") or "")
            if doc_key:
                response["doc_key"] = doc_key
                response["document_id"] = f"manual:{doc_key}" if not doc_key.startswith("manual:") else doc_key
            return 200, response
        finally:
            if tmp_payload is not None:
                try:
                    tmp_payload.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                shutil.rmtree(req_tmp_dir, ignore_errors=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Feishu doc read/write handlers (DALTON-HOOK(feishu-docs))
    # ------------------------------------------------------------------

    def _feishu_client(self) -> FeishuClient | None:
        """Return the cached Feishu client, or None when no credentials are configured."""
        cached = getattr(self, "_feishu_client_instance", None)
        if cached is not None:
            return cached
        app_id = (getattr(self.cfg, "feishu_app_id", "") or "").strip()
        app_secret = (getattr(self.cfg, "feishu_app_secret", "") or "").strip()
        if not app_id or not app_secret:
            return None
        client = FeishuClient(
            app_id, app_secret, timeout_s=getattr(self.cfg, "feishu_http_timeout_s", 30.0)
        )
        self._feishu_client_instance = client
        return client

    async def _run_feishu(self, fn, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking FeishuClient call off the event loop; map errors to (code, payload)."""
        client = self._feishu_client()
        if client is None:
            return 503, {"ok": False, "error": "feishu_not_configured"}
        try:
            result = await asyncio.to_thread(fn, client, *args, **kwargs)
        except FeishuApiError as exc:
            logger.warning(
                "feishu route call failed error=%s detail=%s",
                exc.error_code,
                str(exc)[:200],
            )
            return 502, {"ok": False, "error": exc.error_code, "detail": str(exc)[:200]}
        return 200, result

    async def handle_feishu_doc_read(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /feishu/doc/read``: resolve a docx/wiki link or token and return title + plain text.

        ``document_id`` may be supplied by a caller that already resolved the
        token through ``/feishu/wiki/node`` (the external-member permission
        check); it is then used as-is so the check and the read act on the
        same resolution instead of resolving twice.
        """
        raw_token = body.get("token")
        kind, token = parse_feishu_token(raw_token if isinstance(raw_token, str) else "")
        pre_resolved = str(body.get("document_id") or "").strip()
        if not token and not pre_resolved:
            return 400, {"ok": False, "error": "invalid_token"}
        try:
            max_chars = int(body.get("max_chars") or FEISHU_DEFAULT_CONTENT_CHARS)
        except (TypeError, ValueError):
            max_chars = FEISHU_DEFAULT_CONTENT_CHARS
        max_chars = max(1000, min(max_chars, FEISHU_MAX_CONTENT_CHARS))

        def _read(client: FeishuClient) -> dict[str, Any]:
            if pre_resolved:
                document_id, node = pre_resolved, {}
            else:
                document_id, node = resolve_feishu_document(client, kind, token)
            document = client.get_document(document_id)
            content = client.raw_content(document_id)
            truncated = len(content) > max_chars
            payload: dict[str, Any] = {
                "ok": True,
                "document_id": document_id,
                "title": str(document.get("title") or node.get("title") or ""),
                "content": content[:max_chars] if truncated else content,
                "total_chars": len(content),
                "truncated": truncated,
            }
            if node:
                payload["node_token"] = str(node.get("node_token") or "")
                payload["space_id"] = str(node.get("space_id") or "")
            return payload

        code, result = await self._run_feishu(_read)
        return code, result

    async def handle_feishu_wiki_nodes(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /feishu/wiki/nodes``: list spaces (no ``space_id``) or a space's nodes."""
        space_id = str(body.get("space_id") or "").strip()
        parent_node_token = str(body.get("parent_node_token") or "").strip()
        if not space_id:

            def _spaces(client: FeishuClient) -> dict[str, Any]:
                return {
                    "ok": True,
                    "spaces": [
                        {
                            "space_id": str(s.get("space_id") or ""),
                            "name": str(s.get("name") or ""),
                            "description": str(s.get("description") or ""),
                        }
                        for s in client.list_spaces()
                    ],
                }

            code, result = await self._run_feishu(_spaces)
            return code, result

        def _nodes(client: FeishuClient) -> dict[str, Any]:
            return {
                "ok": True,
                "space_id": space_id,
                "nodes": [_feishu_node_summary(n) for n in client.list_nodes(space_id, parent_node_token)],
            }

        code, result = await self._run_feishu(_nodes)
        return code, result

    async def handle_feishu_wiki_node(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /feishu/wiki/node``: resolve a link/token to its wiki node (or ``node: None``).

        Uses `resolve_feishu_node_for_check`: same resolution as the read path
        (wiki link -> node, docx link -> reverse lookup, bare token -> wiki
        first, then a docx reverse lookup), so the permission check and the
        read act on one resolution and a bare docx token is not rejected as
        `not_in_wiki` when its space is whitelist-checkable.
        """
        raw_token = body.get("token")
        kind, token = parse_feishu_token(raw_token if isinstance(raw_token, str) else "")
        if not token:
            return 400, {"ok": False, "error": "invalid_token"}
        obj_type = str(body.get("obj_type") or "").strip()

        def _resolve(client: FeishuClient) -> dict[str, Any]:
            if obj_type:
                node = _soft_get_node(client, token, obj_type=obj_type)
            else:
                _, node = resolve_feishu_node_for_check(client, kind, token)
            if not node:
                return {"ok": True, "node": None, "reason": "not_in_wiki"}
            return {"ok": True, "node": _feishu_node_summary(node)}

        code, result = await self._run_feishu(_resolve)
        return code, result

    async def handle_feishu_doc_create(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /feishu/doc/create``: create a docx (wiki node or drive) and insert markdown blocks."""
        title = str(body.get("title") or "").strip()
        markdown = str(body.get("markdown") or "").strip()
        if not title:
            return 400, {"ok": False, "error": "title_required"}
        if not markdown:
            return 400, {"ok": False, "error": "markdown_required"}
        space_id = str(body.get("space_id") or "").strip()
        parent_node_token = str(body.get("parent_node_token") or "").strip()
        folder_token = str(body.get("folder_token") or "").strip()
        grant_email = str(body.get("grant_email") or "").strip().lower()

        def _create(client: FeishuClient) -> dict[str, Any]:
            block_ids, blocks = client.convert_markdown(markdown)
            if not block_ids:
                raise FeishuApiError(
                    "markdown_convert_empty", "markdown conversion produced no blocks"
                )

            node_token = ""
            document_id = ""
            if space_id:
                node = client.create_wiki_doc(space_id, title, parent_node_token)
                document_id = str(node.get("obj_token") or "")
                node_token = str(node.get("node_token") or "")
            else:
                document = client.create_document(title, folder_token)
                document_id = str(document.get("document_id") or "")
            if not document_id:
                raise FeishuApiError("feishu_create_failed")

            # C1: every doc this bridge creates goes into the registry with
            # its creator, so /feishu/doc/grant stays scoped to Dalton's own
            # docs and the user they were created for.
            self._register_feishu_created_doc(
                document_id,
                creator_email=grant_email,
                title=title,
                space_id=space_id,
                node_token=node_token,
            )

            def _content_insert_failed(reason: str) -> dict[str, Any]:
                url = feishu_doc_url(document_id, node_token)
                return {
                    "ok": False,
                    "error": "content_insert_failed",
                    "message": (
                        f"document created but its body was NOT written ({reason}); "
                        f"fix manually at {url}"
                    ),
                    "detail": reason,
                    "document_id": document_id,
                    "url": url,
                }

            try:
                insert_resp = client.insert_descendants(
                    document_id, document_id, block_ids, blocks, 0
                )
            except FeishuApiError as exc:
                sanitized_reason = _sanitize_feishu_response_preview(
                    f"{exc.error_code}: {str(exc)}",
                    secrets=(client.app_secret, client._tenant_token),
                    dynamic_ids=(document_id, node_token, space_id, parent_node_token, folder_token),
                    limit=200,
                )
                return _content_insert_failed(sanitized_reason)
            readback = client.get_document(document_id)

            payload: dict[str, Any] = {
                "ok": True,
                "document_id": document_id,
                "title": str(readback.get("title") or title),
            }
            if space_id:
                payload["space_id"] = space_id
            if node_token:
                payload["node_token"] = node_token

            # C2: convert left each markdown image as a placeholder block; the
            # images are only real once their bytes are uploaded and bound to
            # those blocks. A partial failure is reported per image — an
            # all-failed bind never claims success.
            image_files = body.get("image_files")
            images_outcome = bind_docx_images(
                client,
                document_id,
                markdown,
                image_files if isinstance(image_files, dict) else {},
                converted_blocks=blocks,
                insert_response=insert_resp if isinstance(insert_resp, dict) else None,
            )
            if images_outcome["total"]:
                payload["images"] = images_outcome
                if images_outcome["failed"] and images_outcome["bound"] > 0:
                    failed_refs = "; ".join(
                        f"{f.get('ref') or f.get('block_id')}（{f.get('reason')}）"
                        for f in images_outcome["failed"][:5]
                    )
                    payload["warning"] = (
                        f"image_bind_partial: {images_outcome['bound']}/{images_outcome['total']} "
                        f"张图片已写入，失败的图片：{failed_refs}"
                    )

            warnings: list[str] = []
            if payload.get("warning"):
                warnings.append(payload["warning"])
            grant_chat_id = getattr(self.cfg, "feishu_default_grant_chat_id", "")
            if grant_chat_id:
                try:
                    client.grant_chat_full_access(document_id, grant_chat_id)
                except FeishuApiError as exc:
                    sanitized = _sanitize_feishu_response_preview(
                        f"{exc.error_code}: {str(exc)}",
                        secrets=(client.app_secret, client._tenant_token),
                        dynamic_ids=(document_id, node_token, space_id, parent_node_token, folder_token),
                        limit=200,
                    )
                    warnings.append(
                        f"grant_failed: document created, but sharing it with the team "
                        f"group failed ({sanitized}); share it manually."
                    )
            if grant_email:
                try:
                    spelling = client.grant_full_access(document_id, grant_email)
                except FeishuApiError as exc:
                    # C1: keep Feishu's own code and msg (sanitized) so the
                    # caller can tell an unmatched email from a missing app
                    # scope instead of guessing at a bare error code.
                    sanitized = _sanitize_feishu_response_preview(
                        f"{exc.error_code}: {str(exc)}",
                        secrets=(client.app_secret, client._tenant_token),
                        dynamic_ids=(document_id, node_token, space_id, parent_node_token, folder_token),
                        limit=200,
                    )
                    warnings.append(
                        f"grant_failed: document created, but granting {grant_email} "
                        f"full access failed ({sanitized}); share it manually or "
                        f"re-grant with feishu_grant_access."
                    )
                else:
                    payload["grant_member"] = spelling
            if warnings:
                payload["warning"] = " ".join(warnings)
            # C2: no image bound out of the images the markdown carried — the
            # doc exists but its images are placeholders, which must not be
            # reported as a success.
            if images_outcome["total"] and images_outcome["bound"] == 0:
                payload["ok"] = False
                payload["error"] = "image_bind_failed"
                payload["message"] = (
                    "文档已创建，但全部图片都没有绑定成功（见 images.failed），"
                    "请在文档中手动补图或重试。"
                )
                # The doc exists — its id and url must survive into the tool's
                # answer so the model can point the user at it.
                payload["url"] = feishu_doc_url(document_id, node_token)
            return payload

        code, result = await self._run_feishu(_create)
        if result.get("error") == "content_insert_failed":
            # The document exists; failing loudly beats silently returning an
            # empty doc, and 502 (not 200) makes the tool surface the message.
            return 502, result
        return code, result

    async def handle_feishu_doc_grant(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /feishu/doc/grant``: (re-)grant one user full access to a doc (C1).

        Only does the grant — no create, no content write — so a user whose
        automatic share failed at doc creation can be re-authorized against
        the existing document. Scoped to docs this bridge created and to the
        creating user's own email: anything else is refused with 403, so the
        endpoint can never spread the app's reach onto someone else's doc or
        grant an arbitrary address.
        """
        raw_token = body.get("token")
        kind, token = parse_feishu_token(raw_token if isinstance(raw_token, str) else "")
        if not token:
            return 400, {"ok": False, "error": "invalid_token"}
        email = str(body.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return 400, {"ok": False, "error": "invalid_email"}

        def _grant(client: FeishuClient) -> dict[str, Any]:
            document_id, node = resolve_feishu_document(client, kind, token)
            record = self._read_feishu_doc_registry().get(document_id)
            if record is None:
                return {
                    "ok": False,
                    "error": "grant_scope_denied",
                    "document_id": document_id,
                    "message": (
                        "该文档不是通过 Dalton 创建的（未在创建登记中），"
                        "不能通过本端点补授权；请在飞书客户端手动分享。"
                    ),
                }
            creator = str(record.get("creator_email") or "").strip().lower()
            if not creator:
                return {
                    "ok": False,
                    "error": "grant_scope_denied",
                    "document_id": document_id,
                    "message": (
                        "该文档创建时没有记录创建者邮箱，无法核验补授权对象；"
                        "请在飞书客户端手动分享。"
                    ),
                }
            if email != creator:
                return {
                    "ok": False,
                    "error": "grant_scope_denied",
                    "document_id": document_id,
                    "message": "补授权邮箱与文档创建者不一致，只允许给创建者本人补授权。",
                }
            node_token = (
                str(node.get("node_token") or "")
                if isinstance(node, dict) and node.get("node_token")
                else (token if kind == "wiki" else "")
            )
            spelling = client.grant_full_access(document_id, email)
            return {
                "ok": True,
                "document_id": document_id,
                "url": feishu_doc_url(document_id, node_token),
                "granted": email,
                "grant_member": spelling,
            }

        code, result = await self._run_feishu(_grant)
        if isinstance(result, dict) and result.get("error") == "grant_scope_denied":
            return 403, result
        if code == 502 and isinstance(result, dict):
            # _run_feishu truncates the detail to 200 chars and sanitizes
            # nothing; redo both so Feishu's code/msg arrives clean.
            detail = _sanitize_feishu_response_preview(
                str(result.get("detail") or ""),
                secrets=(),
                dynamic_ids=(token,),
                limit=200,
            )
            result = {
                "ok": False,
                "error": str(result.get("error") or "grant_failed"),
                "detail": detail,
                "message": f"授权失败（{detail}）；若是邮箱对不上账号，请改用该用户的飞书登录邮箱重试。",
            }
        return code, result

    async def handle_feishu_doc_append(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle ``POST /feishu/doc/append``: append markdown blocks at the end of an existing doc."""
        raw_token = body.get("token")
        kind, token = parse_feishu_token(raw_token if isinstance(raw_token, str) else "")
        if not token:
            return 400, {"ok": False, "error": "invalid_token"}
        markdown = str(body.get("markdown") or "").strip()
        if not markdown:
            return 400, {"ok": False, "error": "markdown_required"}

        def _append(client: FeishuClient) -> dict[str, Any]:
            document_id, node = resolve_feishu_document(client, kind, token)
            block_ids, blocks = client.convert_markdown(markdown)
            if not block_ids:
                raise FeishuApiError(
                    "markdown_convert_empty", "markdown conversion produced no blocks"
                )
            node_token = (
                str(node.get("node_token") or "")
                if isinstance(node, dict) and node.get("node_token")
                else (token if kind == "wiki" else "")
            )
            try:
                index = client.root_children_count(document_id)
                insert_resp = client.insert_descendants(
                    document_id, document_id, block_ids, blocks, index
                )
            except FeishuApiError as exc:
                url = feishu_doc_url(document_id, node_token)
                reason = _sanitize_feishu_response_preview(
                    f"{exc.error_code}: {str(exc)}",
                    secrets=(client.app_secret, client._tenant_token),
                    dynamic_ids=(document_id, node_token, token),
                    limit=200,
                )
                outcome = (
                    "unknown"
                    if _feishu_outcome_unknown(exc.error_code)
                    else "rejected"
                )
                lead = (
                    "追加请求结果未知，可能已经写入；请先打开文档检查，不要直接重试"
                    if outcome == "unknown"
                    else "追加内容没有写入，原文档未改变"
                )
                return {
                    "ok": False,
                    "error": "content_insert_failed",
                    "outcome": outcome,
                    "message": f"{lead} ({reason}); url={url}",
                    "detail": reason,
                    "document_id": document_id,
                    "url": url,
                }
            # C2: the same bind flow as doc/create — appended images are
            # placeholder blocks until their bytes are uploaded and bound.
            image_files = body.get("image_files")
            images_outcome = bind_docx_images(
                client,
                document_id,
                markdown,
                image_files if isinstance(image_files, dict) else {},
                converted_blocks=blocks,
                insert_response=insert_resp if isinstance(insert_resp, dict) else None,
            )
            payload = {
                "ok": True,
                "document_id": document_id,
                "appended_blocks": len(block_ids),
            }
            if images_outcome["total"]:
                payload["images"] = images_outcome
                if images_outcome["failed"] and images_outcome["bound"] > 0:
                    failed_refs = "; ".join(
                        f"{f.get('ref') or f.get('block_id')}（{f.get('reason')}）"
                        for f in images_outcome["failed"][:5]
                    )
                    payload["warning"] = (
                        f"image_bind_partial: {images_outcome['bound']}/{images_outcome['total']} "
                        f"张图片已写入，失败的图片：{failed_refs}"
                    )
                if images_outcome["bound"] == 0:
                    payload["ok"] = False
                    payload["error"] = "image_bind_failed"
                    payload["message"] = (
                        "追加已写入，但全部图片都没有绑定成功（见 images.failed），"
                        "请在文档中手动补图或重试。"
                    )
                    payload["url"] = feishu_doc_url(document_id, node_token)
            return payload

        code, result = await self._run_feishu(_append)
        if result.get("error") == "content_insert_failed":
            return 502, result
        return code, result




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
_WIKI_EXISTING_TARGETS_MAX = 2000
# Must stay below the backend's call budget minus the CLI timeout
# (WIKI_INGEST_HTTP_TIMEOUT_S 190 s vs WIKI_INGEST_TIMEOUT_S).
WIKI_WRITE_QUEUE_WAIT_S = 5.0


def _wiki_alias_error_status(err: dict[str, Any]) -> int:
    return {"alias_conflict": 409, "alias_store_unreadable": 503}.get(str(err.get("status")), 400)


class WikiAliasStoreError(RuntimeError):
    """The Dalton wiki alias store exists but cannot be read."""


_WIKI_ALIAS_STORE_UNREADABLE: dict[str, Any] = {
    "ok": False,
    "status": "alias_store_unreadable",
    "error_code": "alias_store_unreadable",
    "error": "公司别名表（var/dalton/wiki_aliases.json）读取失败，为避免写错目录已暂停入库；请联系管理员修复该文件。",
}
_WIKI_ALIASES_MAX = 10
# Company names and ticker forms (东方雨虹, 002271.SZ, SZ002271, Oriental Yuhong);
# no path separators, quotes or control characters.
_WIKI_ALIAS_TEXT_RE = re.compile(r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff .&()（）·'-]*")
_WIKI_NEEDS_ALIAS_HINT = (
    "先在 existing_targets 里找这家公司已有的英文目录名（例如东方雨虹 → YUHONG），"
    "确认是同一家公司后用它作为 target 重新入库；确实没有时，可用大写英文名新建目录"
    "（^[A-Z][A-Z0-9-]{0,29}$，如 ORIENTAL-YUHONG），并在回执里告诉用户用了哪个目录名。"
    "拿不准是不是同一家公司时先问用户。重新入库时把这次用的中文名或代码放进 aliases"
    "（如 [\"002271.SZ\", \"东方雨虹\"]），以后就能直接用它们入库。"
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


# ---------------------------------------------------------------------------
# Feishu OpenAPI proxy (DALTON-HOOK(feishu-docs))
#
# All Feishu calls run here on the host with app identity
# (tenant_access_token). Credentials come from FEISHU_APP_ID/FEISHU_APP_SECRET
# or a read-only lookup of ~/.openclaw/openclaw.json; they are never logged
# and never appear in any route response.
# ---------------------------------------------------------------------------

FEISHU_API_BASE = "https://open.feishu.cn"
FEISHU_TOKEN_REFRESH_MARGIN_S = 300.0
FEISHU_MAX_CONTENT_CHARS = 50000
FEISHU_DEFAULT_CONTENT_CHARS = 20000
FEISHU_PAGE_SIZE = 50
FEISHU_MAX_PAGES = 20
# C2: a markdown image bound into a docx image block — Feishu's upload cap is
# 20 MB; the bridge's own ceiling stays below it, and the HTTP envelope for
# the feishu routes is sized to carry one document's worth of inline images.
FEISHU_IMAGE_MAX_BYTES = 10 * 1024 * 1024
FEISHU_IMAGE_TOTAL_MAX_BYTES = 20 * 1024 * 1024
FEISHU_DOC_IMAGE_BLOCK_TYPE = 27
FEISHU_IMAGE_ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
# C2: the /feishu/doc/create|append bodies carry inline image bytes (base64),
# so they need the wiki-ingest-sized envelope, not the 256 KiB MCP default.
MAX_FEISHU_DOC_REQUEST_BYTES = 30 * 1024 * 1024

# URL path segment -> doc kind (docs is the legacy docx path; both carry a docx token)
_FEISHU_LINK_KINDS: dict[str, str] = {"wiki": "wiki", "docx": "docx", "docs": "docx"}

# C2: markdown image references, in order of appearance — the Nth converted
# image block binds to the Nth reference.
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


def _markdown_image_refs(markdown: str, *, dedup: bool = True) -> list[str]:
    refs: list[str] = []
    for match in _MARKDOWN_IMAGE_RE.finditer(markdown or ""):
        ref = match.group(1).strip()
        if ref and (not dedup or ref not in refs):
            refs.append(ref)
    return refs


def _feishu_image_ip_blocked(ip: str) -> bool:
    """Whether *ip* is a private/loopback/link-local/reserved/unspecified/multicast address.

    The rule is the backend SSRF guard's ``_ip_is_blocked``
    (src/tools/web/inhouse/guard.py), restated here because the bridge is a
    standalone host process and cannot import backend code.
    """
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    )


def _feishu_image_url_block_reason(url: str) -> str | None:
    """Why this URL may not be fetched host-side, or None if it may (C2).

    Same semantics as the crawler's check, restated locally: the bridge is a
    standalone host process and cannot import the sandbox-side copy. This is
    the literal-address half — the fetch itself resolves hostnames and
    re-checks every redirect hop (see `_feishu_image_fetch`).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        return f"unparseable URL ({exc})"
    if parsed.scheme not in ("http", "https"):
        return f"URL must be http(s), got scheme {parsed.scheme!r}"
    hostname = parsed.hostname or ""
    if hostname in ("", "localhost"):
        return "access to localhost is not allowed"
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return None  # a name, not a literal address — the fetch resolves it
    if _feishu_image_ip_blocked(hostname):
        return f"access to private/reserved address {hostname} is not allowed"
    return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """An HTTP connection whose TCP dial goes to one specific, SSRF-checked IP.

    ``self.host`` keeps the URL's hostname, so the ``Host`` header is
    unchanged; only the socket's target address is pinned. This closes the
    rebinding window where the check resolves the name once and the client
    resolves it again (C2/r3).
    """

    def __init__(self, host: str, port: int | None = None, *, pinned_ip: str,
                 timeout: float = 30.0) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        # stdlib connect with the dial aimed at the checked address instead of
        # resolving `self.host` again.
        self.sock = self._create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection dialed to a checked IP with the TLS handshake kept
    on the URL's hostname: SNI (``server_hostname``) and the certificate's
    hostname validation both check the name the URL carried, not the pinned
    address."""

    def __init__(self, host: str, port: int | None = None, *, pinned_ip: str,
                 timeout: float = 30.0) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = self._create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


#: Manual redirect budget for one external image fetch (C2/SSRF).
FEISHU_IMAGE_MAX_REDIRECTS = 3
_FEISHU_IMAGE_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_FEISHU_IMAGE_FETCH_TIMEOUT_S = 30.0


def _feishu_image_open_connection(
    parsed_url: urllib.parse.ParseResult, *, pinned_ip: str, timeout_s: float
) -> http.client.HTTPConnection:
    """The hop's http.client connection, dialed to *pinned_ip* (the address
    the SSRF check approved) while Host/SNI/cert-hostname stay on the original
    name. A seam the tests replace instead of the dialing logic itself."""
    hostname = parsed_url.hostname or ""
    port = parsed_url.port or (443 if parsed_url.scheme == "https" else 80)
    conn_cls = _PinnedHTTPSConnection if parsed_url.scheme == "https" else _PinnedHTTPConnection
    return conn_cls(hostname, port, pinned_ip=pinned_ip, timeout=timeout_s)


def _feishu_image_fetch(url: str) -> tuple[bytes, str]:
    """Download an external image under the SSRF checks; ``(bytes, final_url)``.

    Every hop — the original URL and up to 3 redirects — must be http(s), its
    host's DNS answers must hold no private/loopback/link-local/reserved
    address, and the connection is pinned to the address that passed the
    check: the socket never resolves the hostname again, so a rebinding answer
    arriving between check and connect cannot aim the dial at an internal
    address. Redirects are followed by hand (never automatically) so each hop
    goes back through the same checks.
    """
    current = url
    for _hop in range(FEISHU_IMAGE_MAX_REDIRECTS + 1):
        reason = _feishu_image_url_block_reason(current)
        if reason is not None:
            raise FeishuApiError("image_url_blocked", reason)
        try:
            parsed = urllib.parse.urlparse(current)
        except ValueError as exc:
            raise FeishuApiError("image_url_blocked", f"unparseable URL ({exc})") from exc
        hostname = parsed.hostname or ""
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as exc:
            raise FeishuApiError(
                "image_url_blocked", f"DNS resolution failed for {hostname!r} ({exc})"
            ) from exc
        blocked = sorted(
            {info[4][0] for info in addr_infos if _feishu_image_ip_blocked(info[4][0])}
        )
        if blocked:
            raise FeishuApiError(
                "image_url_blocked",
                f"host {hostname!r} resolves to private/reserved address {blocked[0]}",
            )
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        conn = _feishu_image_open_connection(
            parsed, pinned_ip=addr_infos[0][4][0], timeout_s=_FEISHU_IMAGE_FETCH_TIMEOUT_S
        )
        try:
            conn.request(
                "GET",
                request_target,
                headers={
                    "User-Agent": "dalton-researchdata-bridge/1.0",
                    "Accept": "*/*",
                },
            )
            resp = conn.getresponse()
            if resp.status in _FEISHU_IMAGE_REDIRECT_CODES:
                location = resp.getheader("Location")
                if location:
                    if _hop == FEISHU_IMAGE_MAX_REDIRECTS:
                        raise FeishuApiError(
                            "image_url_blocked",
                            f"more than {FEISHU_IMAGE_MAX_REDIRECTS} redirects fetching {url}",
                        )
                    current = urllib.parse.urljoin(current, location)
                    continue
            if not 200 <= resp.status < 300:
                raise FeishuApiError("image_fetch_failed", f"HTTP {resp.status}")
            data = resp.read(FEISHU_IMAGE_MAX_BYTES + 1)
            return data, current
        except FeishuApiError:
            raise
        except (http.client.HTTPException, OSError) as exc:
            raise FeishuApiError(
                "feishu_network_error", f"{type(exc).__name__}: {exc}"[:200]
            ) from exc
        finally:
            conn.close()
    raise FeishuApiError(
        "image_url_blocked", f"more than {FEISHU_IMAGE_MAX_REDIRECTS} redirects fetching {url}"
    )


def _feishu_image_payload(
    ref: str, image_files: dict[str, Any]
) -> tuple[bytes, str]:
    """``(bytes, filename)`` for one image reference (C2).

    Workspace paths arrive pre-read from the caller as base64 (the sandbox's
    files are not host paths); external links are fetched here under the
    SSRF check and the size ceiling.
    """
    supplied = image_files.get(ref)
    if isinstance(supplied, dict):
        raw = supplied.get("content_b64")
        if isinstance(raw, str) and raw.strip():
            data = base64.b64decode(raw, validate=False)
            filename = str(supplied.get("filename") or "image.png")
            if len(data) > FEISHU_IMAGE_MAX_BYTES:
                raise FeishuApiError("image_too_large", f"{len(data)} bytes")
            return data, filename
        raise FeishuApiError("image_payload_invalid", "content_b64 missing")
    if ref.startswith(("http://", "https://")):
        data, final_url = _feishu_image_fetch(ref)
        if len(data) > FEISHU_IMAGE_MAX_BYTES:
            raise FeishuApiError("image_too_large", f"{len(data)} bytes")
        if not data:
            raise FeishuApiError("image_empty")
        path_name = urllib.parse.urlparse(final_url).path or "image"
        filename = Path(path_name).name or "image"
        if Path(filename).suffix.lower() not in FEISHU_IMAGE_ALLOWED_EXTENSIONS:
            filename = filename + ".png" if "." not in filename else filename
        return data, filename
    raise FeishuApiError(
        "image_unavailable",
        "本地路径未随请求提供图片字节（调用方需读取工作区文件后以 image_files 传入）",
    )


def _feishu_image_block_bound(client: FeishuClient, document_id: str, block_id: str) -> bool:
    """Whether the image block carries a bound media token (the verify half of C2)."""
    try:
        block = client.get_block(document_id, block_id)
    except FeishuApiError:
        return False
    image = block.get("image") if isinstance(block.get("image"), dict) else {}
    return bool(str(image.get("token") or ""))


def _resolve_inserted_image_block_ids(
    converted_blocks: list[dict[str, Any]] | None,
    insert_response: dict[str, Any] | None,
) -> list[str]:
    """Resolve converted placeholder image blocks to actual document ``block_id``s
    from the descendant-insert response (``block_id_relations`` or ``children``)."""
    if not isinstance(insert_response, dict):
        return []
    relations = insert_response.get("block_id_relations")
    if isinstance(relations, list) and converted_blocks:
        rel_map = {
            str(r.get("temporary_block_id") or ""): str(r.get("block_id") or "")
            for r in relations
            if isinstance(r, dict)
            and str(r.get("temporary_block_id") or "")
            and str(r.get("block_id") or "")
        }
        conv_img_ids = [
            str(b.get("block_id") or "")
            for b in converted_blocks
            if isinstance(b, dict) and b.get("block_type") == FEISHU_DOC_IMAGE_BLOCK_TYPE
        ]
        if conv_img_ids and all(cid in rel_map for cid in conv_img_ids):
            return [rel_map[cid] for cid in conv_img_ids]
    children = insert_response.get("children")
    if isinstance(children, list):
        child_img_ids = [
            str(b.get("block_id") or "")
            for b in children
            if isinstance(b, dict)
            and b.get("block_type") == FEISHU_DOC_IMAGE_BLOCK_TYPE
            and str(b.get("block_id") or "")
        ]
        if child_img_ids:
            return child_img_ids
    return []


def bind_docx_images(
    client: FeishuClient,
    document_id: str,
    markdown: str,
    image_files: dict[str, Any],
    *,
    converted_blocks: list[dict[str, Any]] | None = None,
    insert_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Upload and bind every markdown image the convert step left as a
    placeholder block (C2). Returns ``{total, bound, failed}``; each failed
    entry carries the reference, block id and a reason. Runs on the caller's
    thread (the handlers already run inside ``asyncio.to_thread``).
    """
    all_refs = _markdown_image_refs(markdown, dedup=False)
    unique_refs = _markdown_image_refs(markdown, dedup=True)
    if not all_refs:
        return {"total": 0, "bound": 0, "failed": []}

    placeholder_ids = _resolve_inserted_image_block_ids(converted_blocks, insert_response)
    if not placeholder_ids:
        blocks = client.list_blocks(document_id)
        placeholder_ids = [
            str(b.get("block_id") or "")
            for b in blocks
            if b.get("block_type") == FEISHU_DOC_IMAGE_BLOCK_TYPE
            and not str((b.get("image") or {}).get("token") or "")
            and str(b.get("block_id") or "")
        ]

    if len(placeholder_ids) == len(all_refs):
        refs = all_refs
    elif len(placeholder_ids) == len(unique_refs):
        refs = unique_refs
    else:
        return {
            "total": len(all_refs),
            "bound": 0,
            "failed": [
                {
                    "ref": ref,
                    "block_id": "",
                    "reason": (
                        f"文档中的占位图片块（{len(placeholder_ids)}）与 markdown 图片"
                        f"（{len(all_refs)}）数量不一致，无法一一对应绑定"
                    ),
                }
                for ref in all_refs
            ],
        }
    bound = 0
    failures: list[dict[str, str]] = []
    for block_id, ref in zip(placeholder_ids, refs):
        try:
            data, filename = _feishu_image_payload(ref, image_files)
            file_token = client.upload_docx_image(
                document_id=document_id,
                image_block_id=block_id,
                filename=filename,
                data=data,
            )
            if file_token:
                try:
                    client.replace_block_image(document_id, block_id, file_token)
                except FeishuApiError:
                    pass
            if _feishu_image_block_bound(client, document_id, block_id):
                bound += 1
                continue
            # The asset is up but the block did not take it; the document id
            # is the other documented spelling of the docx_image upload point.
            file_token_retry = client.upload_docx_image(
                document_id=document_id,
                image_block_id=document_id,
                filename=filename,
                data=data,
            )
            if file_token_retry:
                try:
                    client.replace_block_image(document_id, block_id, file_token_retry)
                except FeishuApiError:
                    pass
            if _feishu_image_block_bound(client, document_id, block_id):
                bound += 1
                continue
            failures.append(
                {
                    "ref": ref,
                    "block_id": block_id,
                    "reason": "素材已上传但图片块未绑定 token（回读校验失败）",
                }
            )
        except FeishuApiError as exc:
            failures.append(
                {"ref": ref, "block_id": block_id, "reason": f"{exc.error_code}: {str(exc)[:120]}"}
            )
        except Exception as exc:
            failures.append(
                {"ref": ref, "block_id": block_id, "reason": f"{type(exc).__name__}: {exc}"[:160]}
            )
    return {"total": len(refs), "bound": bound, "failed": failures}


class FeishuApiError(RuntimeError):
    """Feishu OpenAPI call failure. Never carries credentials or tokens."""

    def __init__(self, error_code: str, message: str = "") -> None:
        super().__init__(message or error_code)
        self.error_code = error_code


#: Codes where Feishu never returned a definite verdict: the connection died
#: mid-response or the body was unparseable, so a write may have been committed
#: anyway. Callers must not claim nothing was written for these.
_FEISHU_UNKNOWN_OUTCOME_CODES: frozenset[str] = frozenset({
    "feishu_network_error",
    "feishu_invalid_response",
    "feishu_empty_error_response",
})


def _feishu_outcome_unknown(error_code: str) -> bool:
    """Whether a Feishu failure leaves it unknown if the write landed: no
    response, an unparseable or empty error body, or a 5xx (a gateway may
    answer after the upstream committed). Only a 4xx with a body is a refusal."""
    return error_code in _FEISHU_UNKNOWN_OUTCOME_CODES or error_code.startswith(
        "feishu_http_5"
    )


#: OpenClaw's Eve tracking group (internal team), the default share target for
#: docs the bot creates; override with FEISHU_DEFAULT_GRANT_CHAT_ID.
FEISHU_DEFAULT_GRANT_CHAT_ID = "oc_397207208fb52a0b09969794ef3cd421"


def _read_openclaw_feishu_credentials() -> tuple[str, str]:
    """Return ``(appId, appSecret)`` from ``~/.openclaw/openclaw.json`` (read-only, empty on any failure).

    Reads ``channels.feishu.accounts.<defaultAccount or "default">``.
    """
    ocw_home = Path(
        os.environ.get("DALTON_OCW_HOME", os.path.expanduser("~/.openclaw"))
    ).expanduser()
    config_path = ocw_home / "openclaw.json"
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        logger.info("Feishu credentials not available from %s (%s)", config_path, type(exc).__name__)
        return "", ""
    try:
        feishu = ((data or {}).get("channels") or {}).get("feishu") or {}
        accounts = feishu.get("accounts") or {}
        account_key = str(feishu.get("defaultAccount") or "default")
        account = accounts.get(account_key) or accounts.get("default") or {}
        app_id = str(account.get("appId") or "").strip()
        app_secret = str(account.get("appSecret") or "").strip()
        return app_id, app_secret
    except Exception as exc:
        logger.info("Feishu account lookup failed in %s (%s)", config_path, type(exc).__name__)
        return "", ""


def parse_feishu_token(raw: str) -> tuple[str, str]:
    """Split a docx/wiki URL or bare token into ``(kind, token)``.

    ``kind`` is ``"wiki"`` for wiki node tokens (``/wiki/<token>`` links),
    ``"docx"`` for document tokens (``/docx/<token>``, ``/docs/<token>``
    links), and ``""`` for a bare token, whose kind is resolved later by
    `resolve_feishu_document` (wiki node lookup first, then docx id).
    Query strings and fragments are ignored.
    """
    cleaned = str(raw or "").strip()
    if not cleaned:
        return "", ""
    if "://" in cleaned or cleaned.startswith("/"):
        try:
            parsed = urllib.parse.urlparse(cleaned if "://" in cleaned else f"https://x{cleaned}")
            segments = [seg for seg in (parsed.path or "").split("/") if seg]
        except ValueError:
            return "", ""
        for idx, seg in enumerate(segments):
            if seg in _FEISHU_LINK_KINDS and idx + 1 < len(segments):
                token = segments[idx + 1].strip()
                if token:
                    return _FEISHU_LINK_KINDS[seg], token
        return "", ""
    return "", cleaned


# Read-only fields the convert endpoint returns but the descendant-insert
# endpoint rejects (e.g. table-cell merge state). Dropped before inserting.
_FEISHU_BLOCK_READONLY_KEYS = frozenset({"merge_info"})

# Official docx block_type ids (see Feishu docx block docs / OpenClaw
# extensions/feishu/src/docx-table-ops.ts cleanBlocksForDescendant).
_FEISHU_BLOCK_TYPE_TABLE = 31
_FEISHU_BLOCK_TYPE_TABLE_CELL = 32

# Tenant link base for docs; derived from the API domain (the bridge never
# learns the tenant slug, so links are best-effort `<base>/docx/<id>`).
FEISHU_DOC_URL_BASE = FEISHU_API_BASE.replace("://open.", "://")


def _sanitize_feishu_block(block: Any) -> Any:
    """Make a convert-produced block submittable to the descendant-insert API.

    Mirrors OpenClaw's ``cleanBlocksForDescendant``: drop ``parent_id`` on every
    block; rebuild a table block's ``table.property`` keeping only the writable
    layout fields (``row_size`` / ``column_size`` / ``column_width`` — this
    drops read-only ``cells`` and ``merge_info``); normalize a table cell's
    string ``children`` to a list. Other read-only keys are still dropped
    recursively as belt-and-braces.
    """
    if isinstance(block, list):
        return [_sanitize_feishu_block(item) for item in block]
    if not isinstance(block, dict):
        return block
    clean = {k: v for k, v in block.items() if k != "parent_id"}
    block_type = clean.get("block_type")
    if block_type == _FEISHU_BLOCK_TYPE_TABLE_CELL and isinstance(clean.get("children"), str):
        clean["children"] = [clean["children"]]
    if block_type == _FEISHU_BLOCK_TYPE_TABLE and isinstance(clean.get("table"), dict):
        prop = clean["table"].get("property") or {}
        rebuilt = {
            k: prop[k]
            for k in ("row_size", "column_size", "column_width")
            if k in prop
        }
        clean["table"] = {"property": rebuilt}
    return {
        k: _sanitize_feishu_block(v)
        for k, v in clean.items()
        if k not in _FEISHU_BLOCK_READONLY_KEYS
    }


def feishu_doc_url(document_id: str, node_token: str = "") -> str:
    """Best-effort tenant link for a created document (wiki node links use the node token)."""
    return f"{FEISHU_DOC_URL_BASE}/wiki/{node_token}" if node_token else (
        f"{FEISHU_DOC_URL_BASE}/docx/{document_id}"
    )


def _soft_get_node(client: "FeishuClient", token: str, *, obj_type: str = "") -> dict[str, Any]:
    """`get_node` that maps "node not found"-shaped upstream answers to ``{}``.

    Transport failures still raise: only API-level rejections (error code or
    4xx/5xx payload) mean "this token is not a wiki node".
    """
    try:
        return client.get_node(token, obj_type=obj_type)
    except FeishuApiError as exc:
        if exc.error_code == "feishu_api_error" or exc.error_code.startswith("feishu_http_"):
            return {}
        raise


def _feishu_node_summary(node: dict[str, Any]) -> dict[str, Any]:
    """Normalize a wiki node for wire responses, keeping shortcut cross-space fields."""
    return {
        "node_token": str(node.get("node_token") or ""),
        "obj_token": str(node.get("obj_token") or ""),
        "obj_type": str(node.get("obj_type") or ""),
        "title": str(node.get("title") or ""),
        "space_id": str(node.get("space_id") or ""),
        "node_type": str(node.get("node_type") or ""),
        "origin_space_id": str(node.get("origin_space_id") or ""),
        "parent_node_token": str(node.get("parent_node_token") or ""),
        "has_child": bool(node.get("has_child")),
    }


def resolve_feishu_document(
    client: "FeishuClient", kind: str, token: str
) -> tuple[str, dict[str, Any]]:
    """Resolve a parsed token to ``(document_id, node)`` exactly once.

    ``/wiki/`` links resolve through ``get_node``; ``/docx/`` links reverse-look
    up their wiki node with ``obj_type=docx`` (empty node when not in a wiki);
    bare tokens try the wiki-node lookup first and fall back to treating the
    token as a docx document id. Callers must reuse this one resolution for
    both the permission check and the actual read/write.
    """
    if kind == "wiki":
        node = client.get_node(token)
        document_id = str(node.get("obj_token") or "")
        if not document_id:
            raise FeishuApiError("wiki_node_not_found")
        return document_id, node
    if kind == "docx":
        node = _soft_get_node(client, token, obj_type="docx")
        document_id = str(node.get("obj_token") or token)
        return document_id, node
    node = _soft_get_node(client, token)
    document_id = str(node.get("obj_token") or "")
    if document_id:
        return document_id, node
    return token, {}


def resolve_feishu_node_for_check(
    client: "FeishuClient", kind: str, token: str
) -> tuple[str, dict[str, Any]]:
    """Permission-check resolution: `resolve_feishu_document`, plus a docx reverse lookup.

    Unlike `resolve_feishu_document`, this never raises ``wiki_node_not_found``
    (an unresolvable token is simply "not in a wiki" — the caller denies), and
    a bare token that is NOT a wiki node gets one ``obj_type=docx`` reverse
    lookup so a bare docx token's wiki space can still be whitelist-checked
    instead of being rejected as `not_in_wiki` outright.
    """
    if kind == "wiki":
        node = _soft_get_node(client, token)
        return str(node.get("obj_token") or ""), node
    if kind == "docx":
        node = _soft_get_node(client, token, obj_type="docx")
        return str(node.get("obj_token") or token), node
    node = _soft_get_node(client, token)
    if node:
        return str(node.get("obj_token") or ""), node
    node = _soft_get_node(client, token, obj_type="docx")
    if node:
        return str(node.get("obj_token") or token), node
    return token, {}


_FEISHU_STATIC_PATH_SEGMENTS: frozenset[str] = frozenset({
    "open-apis", "auth", "v1", "v2", "v3", "tenant_access_token", "internal",
    "docx", "documents", "blocks", "convert", "raw_content", "descendant",
    "children", "wiki", "spaces", "get_node", "nodes", "drive", "permissions",
    "members", "files", "download", "medias", "upload_all", "contact", "users",
    "batch_get_id", "im", "messages", "chats", "bitable",
    "apps", "tables", "records", "fields",
})
_FEISHU_AUTH_TOKEN_RE = re.compile(
    r"(?i)\b(?:(?:t|u)-[A-Za-z0-9._\-]{6,}|cli_[A-Za-z0-9._\-]{6,})\b"
)
_FEISHU_DOC_ID_RE = re.compile(
    r"(?i)\b(?:doc|dox|wik|bas|box|fld)[A-Za-z0-9]{8,}\b"
)
_FEISHU_DYNAMIC_ID_KEYS: frozenset[str] = frozenset({
    "document_id",
    "block_id",
    "node_token",
    "space_id",
    "folder_token",
    "parent_node_token",
    "obj_token",
    "origin_node_token",
    "origin_space_id",
    "token",
    "member_id",
})


def _feishu_path_template(path: str) -> str:
    clean = urllib.parse.urlsplit(str(path or "")).path or "/"
    parts = [p for p in clean.split("/") if p]
    out: list[str] = []
    for i, seg in enumerate(parts):
        prev = parts[i - 1] if i > 0 else ""
        if prev == "documents" and seg not in _FEISHU_STATIC_PATH_SEGMENTS:
            out.append("{document_id}")
        elif prev == "blocks" and seg not in _FEISHU_STATIC_PATH_SEGMENTS:
            out.append("{block_id}")
        elif prev == "spaces" and seg not in _FEISHU_STATIC_PATH_SEGMENTS:
            out.append("{space_id}")
        elif prev == "permissions" and seg not in _FEISHU_STATIC_PATH_SEGMENTS:
            out.append("{token}")
        elif seg not in _FEISHU_STATIC_PATH_SEGMENTS:
            out.append("{token}")
        else:
            out.append(seg)
    return "/" + "/".join(out)


def _feishu_collect_dynamic_ids(node: Any, ids: list[str]) -> None:
    """Collect dynamic Feishu ids from a nested request payload, at any depth.

    An id-shaped key (`*_id`, `*_token`, plus `children`/`cells`, which carry
    bare block ids) marks every string under it — its own value or each string
    item of its list, e.g. `descendants[*].block_id` or a child block's
    `children` — as an id; everything else is only recursed into.
    """
    if isinstance(node, dict):
        for key, val in node.items():
            k = str(key)
            if k.endswith(("_id", "_token")) or k in ("children", "cells"):
                if isinstance(val, str):
                    if val.strip():
                        ids.append(val.strip())
                    continue
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            if item.strip():
                                ids.append(item.strip())
                        else:
                            _feishu_collect_dynamic_ids(item, ids)
                    continue
            _feishu_collect_dynamic_ids(val, ids)
    elif isinstance(node, list):
        for item in node:
            _feishu_collect_dynamic_ids(item, ids)


def _feishu_request_dynamic_ids(
    path: str,
    query: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    clean = urllib.parse.urlsplit(str(path or "")).path or "/"
    ids: list[str] = [
        seg for seg in clean.split("/") if seg and seg not in _FEISHU_STATIC_PATH_SEGMENTS
    ]
    if isinstance(query, dict):
        for key, val in query.items():
            if key in _FEISHU_DYNAMIC_ID_KEYS and isinstance(val, str) and val.strip():
                ids.append(val.strip())
    _feishu_collect_dynamic_ids(json_body, ids)
    # Sort longest-first so a longer id is replaced before a shorter prefix.
    return tuple(sorted(set(ids), key=len, reverse=True))


def _sanitize_feishu_response_preview(
    raw: str,
    *,
    secrets: tuple[str, ...] = (),
    dynamic_ids: tuple[str, ...] = (),
    limit: int = 120,
) -> str:
    text = " ".join((raw or "").split())
    if not text:
        return ""
    for sec in secrets:
        if sec:
            text = text.replace(sec, "[REDACTED]")
    for did in dynamic_ids:
        if did:
            text = text.replace(did, "<id>")
    # Take the leading slice first (with a small lookahead buffer so tokens
    # straddling the boundary still match regexes) rather than calling
    # sanitize_stderr_text(), which truncates to the *last* 500 chars.
    text = text[: max(limit * 2, 240)]
    for pat, repl in _STDERR_SECRET_PATTERNS:
        text = pat.sub(repl, text)
    text = _FEISHU_AUTH_TOKEN_RE.sub("[REDACTED]", text)
    text = _FEISHU_DOC_ID_RE.sub("<id>", text)
    text = " ".join(text.split())
    return text[:limit]


class FeishuClient:
    """Minimal blocking Feishu OpenAPI client (tenant token + docx/wiki/permissions).

    Bridge handlers wrap every call in ``asyncio.to_thread``; the tenant token is
    cached in memory and refreshed ``FEISHU_TOKEN_REFRESH_MARGIN_S`` before expiry.
    """

    def __init__(self, app_id: str, app_secret: str, timeout_s: float = 30.0) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.timeout_s = timeout_s
        self._tenant_token = ""
        self._token_expires_at = 0.0

    def _tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_token and now < self._token_expires_at:
            return self._tenant_token
        body = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self.app_id, "app_secret": self.app_secret},
            with_auth=False,
            unwrap_data=False,
        )
        token = str(body.get("tenant_access_token") or "")
        expire_s = body.get("expire")
        if not token:
            raise FeishuApiError("feishu_auth_failed")
        try:
            ttl = max(60.0, float(expire_s)) if expire_s is not None else 7200.0
        except (TypeError, ValueError):
            ttl = 7200.0
        self._tenant_token = token
        self._token_expires_at = now + ttl - FEISHU_TOKEN_REFRESH_MARGIN_S
        return token

    def _invalid_response_detail(
        self,
        status: int,
        content_type: str,
        path: str,
        raw: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> str:
        ct = " ".join((content_type or "").split()) or "unknown"
        tmpl = _feishu_path_template(path)
        dynamic_ids = _feishu_request_dynamic_ids(path, query=query, json_body=json_body)
        preview = _sanitize_feishu_response_preview(
            raw,
            secrets=(self.app_secret, self._tenant_token),
            dynamic_ids=dynamic_ids,
            limit=120,
        )
        return f"http_{status} content_type={ct} path={tmpl} body={preview!r}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        with_auth: bool = True,
        unwrap_data: bool = True,
    ) -> dict[str, Any]:
        url = f"{FEISHU_API_BASE}{path}"
        if query:
            cleaned_query = {k: v for k, v in query.items() if v is not None and v != ""}
            if cleaned_query:
                url = f"{url}?{urllib.parse.urlencode(cleaned_query)}"
        headers = {"Content-Type": "application/json"}
        if with_auth:
            headers["Authorization"] = f"Bearer {self._tenant_access_token()}"
        data_bytes = (
            json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
        )
        req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        content_type = ""
        try:
            with opener.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
                resp_headers = getattr(resp, "headers", None)
                if resp_headers is not None and hasattr(resp_headers, "get"):
                    content_type = str(
                        resp_headers.get("Content-Type") or resp_headers.get("content-type") or ""
                    )
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            status = exc.code
            exc_headers = getattr(exc, "headers", None)
            if exc_headers is not None and hasattr(exc_headers, "get"):
                content_type = str(
                    exc_headers.get("Content-Type") or exc_headers.get("content-type") or ""
                )
        except Exception as exc:
            raise FeishuApiError("feishu_network_error", type(exc).__name__) from exc
        if status >= 500 and not raw.strip():
            # A server error with no body says nothing about whether the request
            # took effect (an empty gateway 502 after a committed write). An
            # empty 4xx is still a refusal and keeps its feishu_http_4xx code.
            raise FeishuApiError("feishu_empty_error_response", f"http_{status}")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise FeishuApiError(
                "feishu_invalid_response",
                self._invalid_response_detail(
                    status, content_type, path, raw, query=query, json_body=json_body
                ),
            ) from None
        if not isinstance(payload, dict):
            raise FeishuApiError(
                "feishu_invalid_response",
                self._invalid_response_detail(
                    status, content_type, path, raw, query=query, json_body=json_body
                ),
            )
        if status < 200 or status >= 300:
            raise FeishuApiError(
                f"feishu_http_{status}",
                f"code={payload.get('code')} msg={str(payload.get('msg') or '')[:200]}",
            )
        code = payload.get("code")
        if code not in (0, None):
            raise FeishuApiError(
                "feishu_api_error",
                f"code={code} msg={str(payload.get('msg') or '')[:200]}",
            )
        if not unwrap_data:
            # Some endpoints (tenant_access_token) carry their fields at the
            # top level instead of under `data`.
            return payload
        return payload.get("data") if isinstance(payload.get("data"), dict) else {}

    # -- docx --

    def get_document(self, document_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/open-apis/docx/v1/documents/{document_id}")
        return data.get("document") if isinstance(data.get("document"), dict) else {}

    def raw_content(self, document_id: str) -> str:
        data = self._request(
            "GET", f"/open-apis/docx/v1/documents/{document_id}/raw_content"
        )
        return str(data.get("content") or "")

    def create_document(self, title: str, folder_token: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token
        data = self._request("POST", "/open-apis/docx/v1/documents", json_body=body)
        return data.get("document") if isinstance(data.get("document"), dict) else {}

    def convert_markdown(self, markdown: str) -> tuple[list[str], list[dict[str, Any]]]:
        """Return ``(first_level_block_ids, blocks)`` from markdown conversion.

        The descendant-insert API needs both: ``children_id`` (the top-level
        ids) and ``descendants`` (the full block definitions) — submitting only
        the ids inserts nothing.
        """
        data = self._request(
            "POST",
            "/open-apis/docx/v1/documents/blocks/convert",
            json_body={"content_type": "markdown", "content": markdown},
        )
        ids = data.get("first_level_block_ids")
        blocks = data.get("blocks")
        return (
            [str(i) for i in ids] if isinstance(ids, list) else [],
            [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else [],
        )

    def insert_descendants(
        self,
        document_id: str,
        block_id: str,
        children_ids: list[str],
        descendants: list[dict[str, Any]],
        index: int,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/descendant",
            json_body={
                "children_id": children_ids,
                "descendants": [_sanitize_feishu_block(b) for b in descendants],
                "index": index,
            },
        )

    def replace_block_image(
        self, document_id: str, block_id: str, file_token: str
    ) -> dict[str, Any]:
        """Bind an uploaded media ``file_token`` onto a docx image block via ``PATCH``."""
        return self._request(
            "PATCH",
            f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}",
            json_body={"replace_image": {"token": file_token}},
        )

    def root_children_count(self, document_id: str) -> int:
        """Count the document root block's direct children (insert index for appends)."""
        total = 0
        page_token: str | None = None
        for _ in range(FEISHU_MAX_PAGES):
            data = self._request(
                "GET",
                f"/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children",
                query={"page_size": FEISHU_PAGE_SIZE, "page_token": page_token},
            )
            items = data.get("items")
            total += len(items) if isinstance(items, list) else 0
            page_token = data.get("page_token") or None
            if not data.get("has_more") or not page_token:
                break
        return total

    def list_blocks(self, document_id: str) -> list[dict[str, Any]]:
        """The document's full flat block list (id, block_type, image token …)."""
        blocks: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(FEISHU_MAX_PAGES):
            data = self._request(
                "GET",
                f"/open-apis/docx/v1/documents/{document_id}/blocks",
                query={"page_size": FEISHU_PAGE_SIZE, "page_token": page_token},
            )
            items = data.get("items")
            if isinstance(items, list):
                blocks.extend(b for b in items if isinstance(b, dict))
            page_token = data.get("page_token") or None
            if not data.get("has_more") or not page_token:
                break
        return blocks

    def get_block(self, document_id: str, block_id: str) -> dict[str, Any]:
        data = self._request(
            "GET", f"/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}"
        )
        return data.get("block") if isinstance(data.get("block"), dict) else {}

    def upload_docx_image(
        self,
        *,
        document_id: str,
        image_block_id: str,
        filename: str,
        data: bytes,
    ) -> str:
        """Upload image bytes as a docx image asset; returns the ``file_token``.

        ``medias/upload_all`` with ``parent_type=docx_image``. The 素材概述
        doc names the *image block's* id as the upload point for a docx
        image; where an app's scope rejects that, the caller retries with
        the document id — both spellings are tried by the binding flow, and
        whichever the live API accepts is the one that matters.
        """
        if not data:
            raise FeishuApiError("image_empty")
        if len(data) > FEISHU_IMAGE_MAX_BYTES:
            raise FeishuApiError("image_too_large", f"{len(data)} bytes")

        boundary = f"----daltonbridge{uuid.uuid4().hex}"
        fields: list[tuple[str, str]] = [
            ("file_name", filename),
            ("parent_type", "docx_image"),
            ("parent_node", image_block_id),
            # C2: the 素材上传 doc requires `extra` for docx_image uploads —
            # the docx route token the asset belongs to.
            ("extra", json.dumps({"drive_route_token": document_id})),
            ("size", str(len(data))),
        ]
        body = io.BytesIO()
        for name, value in fields:
            body.write(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        body.write(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
        )
        body.write(data)
        body.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))
        payload_bytes = body.getvalue()

        url = f"{FEISHU_API_BASE}/open-apis/drive/v1/medias/upload_all"
        req = urllib.request.Request(
            url,
            data=payload_bytes,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._tenant_access_token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        raw = ""
        status = 0
        try:
            with opener.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                raw = ""
            status = exc.code
        except Exception as exc:
            raise FeishuApiError("feishu_network_error", type(exc).__name__) from exc
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise FeishuApiError(
                "feishu_invalid_response",
                self._invalid_response_detail(
                    status, "multipart", "/open-apis/drive/v1/medias/upload_all", raw
                ),
            ) from None
        if not isinstance(parsed, dict) or parsed.get("code") not in (0, None) or not (
            200 <= status < 300
        ):
            raise FeishuApiError(
                f"feishu_http_{status}" if not (200 <= status < 300) else "feishu_api_error",
                f"code={parsed.get('code')} msg={str(parsed.get('msg') or '')[:200]}",
            )
        data_obj = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        file_token = str(data_obj.get("file_token") or "")
        if not file_token:
            raise FeishuApiError("image_upload_no_token")
        return file_token

    # -- wiki --

    def get_node(self, token: str, *, obj_type: str = "") -> dict[str, Any]:
        data = self._request(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            query={"token": token, "obj_type": obj_type},
        )
        return data.get("node") if isinstance(data.get("node"), dict) else {}

    def list_spaces(self) -> list[dict[str, Any]]:
        spaces: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(FEISHU_MAX_PAGES):
            data = self._request(
                "GET",
                "/open-apis/wiki/v2/spaces",
                query={"page_size": FEISHU_PAGE_SIZE, "page_token": page_token},
            )
            items = data.get("items")
            if isinstance(items, list):
                spaces.extend(i for i in items if isinstance(i, dict))
            page_token = data.get("page_token") or None
            if not data.get("has_more") or not page_token:
                break
        return spaces

    def list_nodes(self, space_id: str, parent_node_token: str = "") -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(FEISHU_MAX_PAGES):
            data = self._request(
                "GET",
                f"/open-apis/wiki/v2/spaces/{space_id}/nodes",
                query={
                    "page_size": FEISHU_PAGE_SIZE,
                    "parent_node_token": parent_node_token,
                    "page_token": page_token,
                },
            )
            items = data.get("items")
            if isinstance(items, list):
                nodes.extend(i for i in items if isinstance(i, dict))
            page_token = data.get("page_token") or None
            if not data.get("has_more") or not page_token:
                break
        return nodes

    def create_wiki_doc(
        self, space_id: str, title: str, parent_node_token: str = ""
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"obj_type": "docx", "title": title}
        if parent_node_token:
            body["parent_node_token"] = parent_node_token
        data = self._request(
            "POST", f"/open-apis/wiki/v2/spaces/{space_id}/nodes", json_body=body
        )
        return data.get("node") if isinstance(data.get("node"), dict) else {}

    # -- drive permissions --

    def user_open_id_by_email(self, email: str) -> str:
        """The Feishu ``open_id`` for *email*, via the directory's own
        batch-get-id lookup; ``''`` when the app cannot look (no contact
        scope) or the email matches no account.

        The endpoint is ``POST /open-apis/contact/v3/users/batch_get_id``
        with ``user_id_type=open_id`` and a JSON body ``{"emails": [...]}``;
        the answer's ``data.user_list[]`` carries one entry per queried
        address, ``{"email": ..., "user_id": <open_id>}`` — an unmatched
        address comes back with an empty ``user_id``.

        C1: an email grant 400s when the address is not a Feishu login
        email, while an open_id grant names the account itself — so the id
        lookup is the primary spelling and the email form only the
        fallback.
        """
        data = self._request(
            "POST",
            "/open-apis/contact/v3/users/batch_get_id",
            query={"user_id_type": "open_id"},
            json_body={"emails": [email]},
        )
        user_list = data.get("user_list")
        if isinstance(user_list, list):
            for item in user_list:
                if not isinstance(item, dict):
                    continue
                if str(item.get("email") or "").strip().lower() != email.strip().lower():
                    continue
                open_id = str(item.get("user_id") or "")
                if open_id:
                    return open_id
        return ""

    def grant_chat_full_access(self, token: str, chat_id: str) -> None:
        self._request(
            "POST",
            f"/open-apis/drive/v1/permissions/{token}/members",
            query={"type": "docx", "need_notification": "false"},
            json_body={
                "member_type": "openchat",
                "member_id": chat_id,
                "perm": "full_access",
            },
        )

    def grant_full_access(self, token: str, email: str) -> str:
        """Grant *email* full access; returns the member spelling that worked.

        Tries the account's ``open_id`` first (C1) and falls back to the raw
        email; both failure paths raise, so the caller's warning carries the
        code and msg of the spelling that was actually refused.
        """
        try:
            open_id = self.user_open_id_by_email(email)
        except FeishuApiError:
            open_id = ""
        if open_id:
            try:
                self._request(
                    "POST",
                    f"/open-apis/drive/v1/permissions/{token}/members",
                    query={"type": "docx", "need_notification": "false"},
                    json_body={
                        "member_type": "openid",
                        "member_id": open_id,
                        "perm": "full_access",
                    },
                )
                return f"openid:{open_id}"
            except FeishuApiError:
                pass  # the email spelling gets its own chance, and its own error
        self._request(
            "POST",
            f"/open-apis/drive/v1/permissions/{token}/members",
            query={"type": "docx"},
            json_body={
                "member_type": "email",
                "member_id": email,
                "perm": "full_access",
            },
        )
        return f"email:{email}"


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
    _current_corr_id.set(uuid.uuid4().hex[:12])
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
        elif path in ("/wiki/ingest", "/sales-notes/ingest"):
            # D1: both ingest bodies carry a base64 file beside the metadata,
            # so they share the wiki-ingest-sized envelope, not the MCP default.
            max_bytes = MAX_WIKI_INGEST_REQUEST_BYTES
        elif path in ("/feishu/doc/create", "/feishu/doc/append"):
            # C2: these bodies carry inline image bytes beside the markdown.
            max_bytes = MAX_FEISHU_DOC_REQUEST_BYTES
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
            read_timeout = (
                60.0
                if path
                in ("/wiki/ingest", "/sales-notes/ingest", "/feishu/doc/create", "/feishu/doc/append")
                else 15.0
            )
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
                # B5: who the call runs for, as the sandbox's MCP client
                # stamps it; missing headers record as empty identity.
                caller_identity = {
                    "user": headers.get("x-dalton-user", "").strip(),
                    "workspace": headers.get("x-dalton-workspace", "").strip(),
                    "thread": headers.get("x-dalton-thread", "").strip(),
                }
                reply = await bridge.handle_tools_call(
                    req_id, rpc_params, caller_identity=caller_identity
                )
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
                    writer, 405, "Method Not Allowed", {"ok": False, "error": "Method Not Allowed"}
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

        # Route 6b: POST /sales-notes/ingest (D1) — same admin gate, the
        # sell-side twin of the wiki ingest above.
        if path == "/sales-notes/ingest":
            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"ok": False, "error": "Method Not Allowed"}
                )
                return
            try:
                sn_body = json.loads(body_bytes.decode("utf-8"))
                if not isinstance(sn_body, dict):
                    raise ValueError("Expected JSON object")
            except Exception:
                await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Parse error"})
                return
            code, payload = await bridge.handle_sales_notes_ingest(sn_body)
            status_label = (
                "OK"
                if code == 200
                else ("Bad Request" if code == 400 else "Bad Gateway")
            )
            await _send_http(writer, code, status_label, payload)
            return

        # Route 7: POST /feishu/* (behind the admin token gate above; the
        # sandbox holds only the MCP token, so it can never reach Feishu).
        # DALTON-HOOK(feishu-docs)
        if path.startswith("/feishu/"):
            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return
            try:
                feishu_body = json.loads(body_bytes.decode("utf-8"))
                if not isinstance(feishu_body, dict):
                    raise ValueError("Expected JSON object")
            except Exception:
                await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Parse error"})
                return
            feishu_routes: dict[str, Any] = {
                "/feishu/doc/read": bridge.handle_feishu_doc_read,
                "/feishu/wiki/nodes": bridge.handle_feishu_wiki_nodes,
                "/feishu/wiki/node": bridge.handle_feishu_wiki_node,
                "/feishu/doc/create": bridge.handle_feishu_doc_create,
                "/feishu/doc/append": bridge.handle_feishu_doc_append,
                # C1: re-grant only, for shares that failed at doc creation.
                "/feishu/doc/grant": bridge.handle_feishu_doc_grant,
            }
            handler = feishu_routes.get(path)
            if handler is None:
                await _send_http(writer, 404, "Not Found", {"ok": False, "error": "Not Found"})
                return
            code, payload = await handler(feishu_body)
            status_label = {
                200: "OK",
                400: "Bad Request",
                403: "Forbidden",
                502: "Bad Gateway",
                503: "Service Unavailable",
            }.get(code, "Bad Gateway")
            await _send_http(writer, code, status_label, payload)
            return

        await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
    except Exception as exc:
        # P0 fix1 7: the request that died carries its correlation id in the
        # ContextVar (set at tools/call entry), so this last-resort line still
        # groups with the request's own sec_call_* / tools/call lines.
        corr = _current_corr_id.get()
        logger.error(
            "Unhandled bridge error: %s corr=%s",
            type(exc).__name__,
            corr or "-",
        )
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
