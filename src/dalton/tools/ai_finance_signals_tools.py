"""`ai_finance_signals` for one reviewed daily signal bundle.

Server identity and the operator owner gate run before any host call.
The database transaction finishes before HTTP. Compare, ingest, stats, and
read use the bundle saved for that batch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.database.ai_finance import PROFILE, authorize_ai_finance_execution
from src.dalton.settings import (
    get_ai_finance_owner_user_id,
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
)
from src.dalton.tools.automation_report_tools import _server_identity

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"save", "compare", "ingest", "stats", "read"})
_SAVE_OBJECTS = ("github_decisions", "earnings_dispositions", "signal_records")
_HOST_PATH = "/internal/ai-finance/signals"
# Character offset. One character is at least one byte, so this matches the
# stored comparison byte cap in ai_finance_signals._COMPARE_FILE_MAX.
_MAX_OFFSET = 256 * (1 + 8) * (9 * 4000 * 4 + 8192) + 65_536
_MAX_CHARS = 32000
_DEFAULT_MAX_CHARS = 16000
_HTTP_TIMEOUT_SAVE_S = 60.0
_HTTP_TIMEOUT_READ_S = 60.0
_HTTP_TIMEOUT_STATS_S = 90.0
_HTTP_TIMEOUT_COMPARE_S = 960.0
_MAX_HOST_RESPONSE_BYTES = 128 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_EMBEDDING_EXCEPTIONS = frozenset({
    "RuntimeError",
    "HTTPError",
    "URLError",
    "TimeoutError",
    "JSONDecodeError",
})
_BUNDLE_STATUS = frozenset({"open", "ingesting", "ingested"})
_SEARCH_MODES = frozenset({"empty_history", "embedding", "fts", "none"})
_HISTORY_STATUS = frozenset({"empty", "complete", "partial", "none"})
_FORBIDDEN_HOST_KEYS = frozenset({
    "path",
    "state_root",
    "db",
    "argv",
    "env",
    "stdout",
    "stderr",
    "api_key",
    "db_bytes",
    "comparisons",
    "embedding_models",
})


def create_ai_finance_signals_tools() -> list[BaseTool]:
    """Build `[ai_finance_signals]` for the configured internal owner."""

    @tool("ai_finance_signals")
    async def ai_finance_signals(
        action: str,
        batch_id: str,
        github_decisions: dict[str, Any] | None = None,
        earnings_dispositions: dict[str, Any] | None = None,
        signal_records: dict[str, Any] | None = None,
        offset: int = 0,
        max_chars: int = _DEFAULT_MAX_CHARS,
        *,
        config: RunnableConfig,
    ) -> str:
        """Save or reuse this run's reviewed AI-finance signal bundle.

        Save takes the three business objects. Compare, ingest, stats, and read use that saved bundle. A path, credential, shell, model name, or embedding dimension is refused. Each object stays within the host size limit.

        Args:
            action: save, compare, ingest, stats, or read.
            batch_id: batch_id from ai_finance_prepare. A real YYYY-MM-DD business date.
            github_decisions: Required for save. One object whose records review the prepared GitHub candidates.
            earnings_dispositions: Required for save. One object whose records cover every prepared earnings candidate once.
            signal_records: Required for save. One object whose records are the confirmed signals only.
            offset: Character offset for read. 0 is the start.
            max_chars: Characters to return for read, from 1 to 32000.

        Returns:
            A bounded summary. Read returns one comparison page plus the full hash, total_chars, and next_offset.

        Compare this batch before ingest. After ingest starts, save the same bundle again to resume it. Follow next_offset until it is null.
        """
        parsed, refused = _parse_call(
            action,
            batch_id,
            github_decisions,
            earnings_dispositions,
            signal_records,
            offset,
            max_chars,
        )
        if refused is not None:
            return refused
        assert parsed is not None
        identity, refused = await _gate(config)
        if refused is not None:
            return refused
        assert identity is not None
        try:
            authorized = await authorize_ai_finance_execution(
                user_id=identity["user_id"],
                workspace_id=identity["workspace_id"],
                thread_id=identity["thread_id"],
                run_id=identity["run_id"],
                automation_id=identity["turn_origin_automation_id"],
                execution_id=identity["automation_execution_id"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ai_finance_signals authorize failed: %s", type(exc).__name__)
            return _refuse("signal_unavailable", "The signal bundle could not be used.")
        failure = _authorization_failure(authorized)
        if failure is not None:
            return failure
        body = _request_body(identity, parsed)
        if body is None:
            return _refuse("invalid_identity", "Server run identity is not a canonical UUID.")
        try:
            status, payload = await _post_json(_HOST_PATH, body, _timeout_for(str(parsed["action"])))
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            return _refuse("signal_timeout", "The signal operation did not finish before the timeout.")
        except Exception as exc:
            logger.warning("ai_finance_signals host failed: %s", type(exc).__name__)
            return _refuse("host_unavailable", "The signal bundle could not be used.")
        if status != 200 or not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return _host_failure(payload)
        accepted = _accept_success(payload, parsed)
        if accepted is None:
            return _refuse("invalid_response", "The host response did not match this signal bundle.")
        return json.dumps(accepted, ensure_ascii=False)

    return [ai_finance_signals]


async def _gate(config: RunnableConfig) -> tuple[dict[str, str] | None, str | None]:
    identity = _server_identity(config)
    if identity is None:
        return None, _refuse("not_automation_run", "This turn is not a server-started automation run.")
    owner = get_ai_finance_owner_user_id()
    if owner is None:
        return None, _refuse("owner_not_configured", "AI-finance signals are not configured.")
    if identity["user_id"].strip().lower() != owner:
        return None, _refuse("not_allowed", "This account cannot use AI-finance signals.")
    if not await is_internal_or_admin(identity["user_id"]):
        return None, _refuse("not_allowed", "This account cannot use AI-finance signals.")
    canonical = _canonical_identity(identity)
    if canonical is None:
        return None, _refuse("invalid_identity", "Server run identity is not a canonical UUID.")
    return canonical, None


def _parse_call(
    action: object,
    batch_id: object,
    github_decisions: object,
    earnings_dispositions: object,
    signal_records: object,
    offset: object,
    max_chars: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if action not in _ACTIONS:
        return None, _refuse("invalid_action", "action must be save, compare, ingest, stats, or read.")
    if _iso_date(batch_id) is None:
        return None, _refuse("invalid_batch", "batch_id must be a real YYYY-MM-DD business date.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > _MAX_OFFSET:
        return None, _refuse("invalid_offset", "offset must be a nonnegative integer.")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= _MAX_CHARS:
        return None, _refuse("invalid_max_chars", "max_chars must be an integer from 1 to 32000.")
    objects = {
        "github_decisions": github_decisions,
        "earnings_dispositions": earnings_dispositions,
        "signal_records": signal_records,
    }
    if action == "save":
        if any(not isinstance(value, dict) for value in objects.values()):
            return None, _refuse("bundle_invalid", "Save requires the three business objects.")
    elif any(value is not None for value in objects.values()):
        return None, _refuse("invalid_request", "Only save accepts the three business objects.")
    if action != "read" and (offset != 0 or max_chars != _DEFAULT_MAX_CHARS):
        return None, _refuse("invalid_request", "offset and max_chars are only accepted for read.")
    parsed: dict[str, Any] = {
        "action": action,
        "batch_id": batch_id,
        "offset": offset,
        "max_chars": max_chars,
    }
    if action == "save":
        parsed.update(objects)
    return parsed, None


def _request_body(identity: Mapping[str, str], parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    execution_id = _canonical_uuid(identity["automation_execution_id"])
    owner = get_ai_finance_owner_user_id()
    user_id = _canonical_uuid(identity["user_id"])
    workspace_id = _canonical_uuid(identity["workspace_id"])
    thread_id = _canonical_uuid(identity["thread_id"])
    if None in {execution_id, owner, user_id, workspace_id, thread_id}:
        return None
    body: dict[str, Any] = {
        "action": parsed["action"],
        "batch_id": parsed["batch_id"],
        "automation_execution_id": execution_id,
        "owner_user_id": owner,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "profile": PROFILE,
    }
    if parsed["action"] == "save":
        for key in _SAVE_OBJECTS:
            body[key] = parsed[key]
    elif parsed["action"] == "read":
        body["offset"] = parsed["offset"]
        body["max_chars"] = parsed["max_chars"]
    return body


def _timeout_for(action: str) -> float:
    if action in {"compare", "ingest"}:
        return _HTTP_TIMEOUT_COMPARE_S
    if action == "stats":
        return _HTTP_TIMEOUT_STATS_S
    if action == "read":
        return _HTTP_TIMEOUT_READ_S
    return _HTTP_TIMEOUT_SAVE_S


def _authorization_failure(authorized: object) -> str | None:
    if not isinstance(authorized, Mapping) or authorized.get("ok") is not True:
        code = "signal_unavailable"
        message = "The signal bundle could not be used."
        if isinstance(authorized, Mapping):
            code = _token(authorized.get("code")) or code
            message = _safe_text(authorized.get("error")) or message
        return _refuse(code, message)
    if authorized.get("profile") != PROFILE or "period" in authorized:
        return _refuse("profile_mismatch", "This automation profile is not ai-finance-daily.")
    return None


def _accept_success(payload: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    if _FORBIDDEN_HOST_KEYS & set(payload):
        return None
    if parsed["action"] != "read" and "text" in payload:
        return None
    if payload.get("ok") is not True or payload.get("action") != parsed["action"]:
        return None
    if payload.get("code") != "ok" or payload.get("profile") != PROFILE:
        return None
    if payload.get("batch_id") != parsed["batch_id"]:
        return None
    bundle_hash = payload.get("bundle_hash")
    if not isinstance(bundle_hash, str) or _HEX64.fullmatch(bundle_hash) is None:
        return None
    action = str(parsed["action"])
    if action == "read":
        return _accept_page(payload, parsed, bundle_hash)
    if payload.get("status") not in _BUNDLE_STATUS or not isinstance(payload.get("reused"), bool):
        return None
    if action == "save":
        return _accept_save(payload, bundle_hash)
    if action == "compare":
        return _accept_compare(payload, bundle_hash)
    if action == "ingest":
        return _accept_ingest(payload, bundle_hash)
    if action == "stats":
        return _accept_stats(payload, bundle_hash)
    return None


def _accept_save(payload: Mapping[str, Any], bundle_hash: str) -> dict[str, Any] | None:
    manifest = payload.get("manifest_sha256")
    if not isinstance(manifest, str) or _HEX64.fullmatch(manifest) is None:
        return None
    if payload.get("embedding_model") != "gemini-embedding-2" or payload.get("embedding_dim") != 768:
        return None
    summary: dict[str, Any] = {
        "ok": True,
        "action": "save",
        "code": "ok",
        "batch_id": payload["batch_id"],
        "profile": PROFILE,
        "bundle_hash": bundle_hash,
        "manifest_sha256": manifest,
        "status": payload["status"],
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
        "reused": payload["reused"],
    }
    if payload["reused"] is False:
        counts = _record_counts(payload.get("record_counts"))
        if counts is None:
            return None
        summary["record_counts"] = counts
    elif "record_counts" in payload:
        return None
    return summary


def _accept_compare(payload: Mapping[str, Any], bundle_hash: str) -> dict[str, Any] | None:
    manifest = payload.get("manifest_sha256")
    modes = payload.get("search_modes")
    digest = payload.get("content_sha256")
    total = payload.get("total_chars")
    count = payload.get("comparison_count")
    if not isinstance(manifest, str) or _HEX64.fullmatch(manifest) is None:
        return None
    if payload.get("embedding_model") != "gemini-embedding-2" or payload.get("embedding_dim") != 768:
        return None
    if not isinstance(modes, list) or any(item not in _SEARCH_MODES for item in modes):
        return None
    if payload.get("history_embedding_status_before_ingest") not in _HISTORY_STATUS:
        return None
    for key in ("history_signals_before_ingest", "history_embedded_signals_before_ingest"):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        return None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return {
        "ok": True,
        "action": "compare",
        "code": "ok",
        "batch_id": payload["batch_id"],
        "profile": PROFILE,
        "bundle_hash": bundle_hash,
        "manifest_sha256": manifest,
        "status": payload["status"],
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
        "reused": payload["reused"],
        "search_modes": list(modes),
        "history_signals_before_ingest": payload["history_signals_before_ingest"],
        "history_embedded_signals_before_ingest": payload["history_embedded_signals_before_ingest"],
        "history_embedding_status_before_ingest": payload["history_embedding_status_before_ingest"],
        "comparison_count": count,
        "content_sha256": digest,
        "total_chars": total,
    }


def _accept_ingest(payload: Mapping[str, Any], bundle_hash: str) -> dict[str, Any] | None:
    manifest = payload.get("manifest_sha256")
    if not isinstance(manifest, str) or _HEX64.fullmatch(manifest) is None:
        return None
    if payload.get("embedding_model") != "gemini-embedding-2" or payload.get("embedding_dim") != 768:
        return None
    if payload.get("configured_embedding_model") != "gemini-embedding-2":
        return None
    if payload.get("configured_embedding_dim") != 768:
        return None
    inserted = payload.get("inserted")
    updated = payload.get("updated")
    errors = payload.get("embedding_errors")
    if isinstance(inserted, bool) or not isinstance(inserted, int) or inserted < 0:
        return None
    if isinstance(updated, bool) or not isinstance(updated, int) or updated < 0:
        return None
    if not isinstance(errors, list) or any(not _safe_embedding_error(item) for item in errors):
        return None
    stats = _coverage(payload)
    if stats is None:
        return None
    return {
        "ok": True,
        "action": "ingest",
        "code": "ok",
        "batch_id": payload["batch_id"],
        "profile": PROFILE,
        "bundle_hash": bundle_hash,
        "manifest_sha256": manifest,
        "status": payload["status"],
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 768,
        "reused": payload["reused"],
        "inserted": inserted,
        "updated": updated,
        "embedding_errors": list(errors),
        **stats,
        "configured_embedding_model": "gemini-embedding-2",
        "configured_embedding_dim": 768,
    }


def _accept_stats(payload: Mapping[str, Any], bundle_hash: str) -> dict[str, Any] | None:
    if payload.get("configured_embedding_model") != "gemini-embedding-2":
        return None
    if payload.get("configured_embedding_dim") != 768:
        return None
    by_source = _count_map(payload.get("signals_by_source"))
    by_direction = _count_map(payload.get("signals_by_direction"))
    embedded_any = payload.get("embedded_signals_any_model")
    if by_source is None or by_direction is None:
        return None
    if isinstance(embedded_any, bool) or not isinstance(embedded_any, int) or embedded_any < 0:
        return None
    stats = _coverage(payload)
    if stats is None:
        return None
    return {
        "ok": True,
        "action": "stats",
        "code": "ok",
        "batch_id": payload["batch_id"],
        "profile": PROFILE,
        "bundle_hash": bundle_hash,
        "status": payload["status"],
        "reused": payload["reused"],
        **stats,
        "embedded_signals_any_model": embedded_any,
        "configured_embedding_model": "gemini-embedding-2",
        "configured_embedding_dim": 768,
        "signals_by_source": by_source,
        "signals_by_direction": by_direction,
    }


def _accept_page(
    payload: Mapping[str, Any],
    parsed: Mapping[str, Any],
    bundle_hash: str,
) -> dict[str, Any] | None:
    if payload.get("offset") != parsed["offset"] or payload.get("max_chars") != parsed["max_chars"]:
        return None
    total = payload.get("total_chars")
    text = payload.get("text")
    digest = payload.get("content_sha256")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0 or total > _MAX_OFFSET:
        return None
    if not isinstance(text, str) or len(text) > parsed["max_chars"]:
        return None
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        return None
    nxt = payload.get("next_offset")
    end = parsed["offset"] + len(text)
    complete = payload.get("complete")
    if nxt is None:
        if end != total or complete is not True:
            return None
    elif (
        isinstance(nxt, bool)
        or not isinstance(nxt, int)
        or nxt != end
        or nxt <= parsed["offset"]
        or nxt > _MAX_OFFSET
        or nxt > total
        or complete is not False
    ):
        return None
    return {
        "ok": True,
        "action": "read",
        "code": "ok",
        "batch_id": parsed["batch_id"],
        "profile": PROFILE,
        "bundle_hash": bundle_hash,
        "offset": parsed["offset"],
        "max_chars": parsed["max_chars"],
        "total_chars": total,
        "content_sha256": digest,
        "next_offset": nxt,
        "complete": nxt is None,
        "text": text,
    }


def _coverage(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    signals = payload.get("signals")
    embedded = payload.get("embedded_signals")
    status = payload.get("embedding_status")
    coverage = payload.get("embedding_coverage_pct")
    if isinstance(signals, bool) or not isinstance(signals, int) or signals < 0:
        return None
    if isinstance(embedded, bool) or not isinstance(embedded, int) or embedded < 0:
        return None
    if status not in _HISTORY_STATUS:
        return None
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)) or not math.isfinite(float(coverage)):
        return None
    if float(coverage) < 0 or float(coverage) > 100:
        return None
    return {
        "signals": signals,
        "embedded_signals": embedded,
        "embedding_status": status,
        "embedding_coverage_pct": float(coverage),
    }


def _record_counts(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or set(value) != set(_SAVE_OBJECTS):
        return None
    counts: dict[str, int] = {}
    for key in _SAVE_OBJECTS:
        count = value.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        counts[key] = count
    return counts


def _count_map(value: object) -> dict[str, int] | None:
    if not isinstance(value, Mapping) or len(value) > 16:
        return None
    cleaned: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or _TOKEN.fullmatch(key) is None:
            return None
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
        cleaned[key] = count
    return cleaned


def _embedding_record_key(record: str) -> bool:
    """Same business ids as the signal host. Words inside a repo name stay."""
    if len(record) > 4000 or "\x00" in record or "\\" in record or "\n" in record or "\r" in record:
        return False
    if record.startswith("github:"):
        owner_repo, separator, day = record[len("github:"):].rpartition(":")
        return (
            separator == ":"
            and _REPO.fullmatch(owner_repo) is not None
            and _iso_date(day) is not None
        )
    if record.startswith("earnings:"):
        external_id = record[len("earnings:"):]
        return (
            bool(external_id)
            and external_id == external_id.strip()
            and len(external_id) <= 128
            and ".." not in external_id
            and "/" not in external_id
            and "\\" not in external_id
        )
    return False


def _safe_embedding_error(value: object) -> bool:
    """Accept only ``{record_key}: {ExceptionClass}`` from the signal CLI."""
    if not isinstance(value, str) or "\n" in value or "\r" in value or "\\" in value or "\x00" in value:
        return False
    record, separator, kind = value.rpartition(": ")
    if separator != ": " or kind not in _EMBEDDING_EXCEPTIONS:
        return False
    return _embedding_record_key(record)


def _host_failure(payload: Mapping[str, Any] | None) -> str:
    body: dict[str, Any] = {
        "ok": False,
        "code": "host_unavailable",
        "error": "The signal bundle could not be used.",
    }
    if not isinstance(payload, Mapping):
        return json.dumps(body, ensure_ascii=False)
    if _FORBIDDEN_HOST_KEYS & set(payload) or "text" in payload or "comparisons" in payload:
        return json.dumps(body, ensure_ascii=False)
    code = _token(payload.get("code")) or "host_unavailable"
    body["code"] = code
    error = _safe_text(payload.get("error"))
    if error is not None:
        body["error"] = error
    batch_id = payload.get("batch_id")
    if _iso_date(batch_id) == batch_id:
        body["batch_id"] = batch_id
    reason = _token(payload.get("reason"))
    if reason is not None:
        body["reason"] = reason
    status = payload.get("status")
    if isinstance(status, str) and status in _BUNDLE_STATUS:
        body["status"] = status
    bundle_hash = payload.get("bundle_hash")
    if isinstance(bundle_hash, str) and _HEX64.fullmatch(bundle_hash):
        body["bundle_hash"] = bundle_hash
    if isinstance(payload.get("reused"), bool):
        body["reused"] = payload["reused"]
    action = payload.get("action")
    if isinstance(action, str) and action in _ACTIONS:
        body["action"] = action
    if payload.get("profile") == PROFILE:
        body["profile"] = PROFILE
    return json.dumps(body, ensure_ascii=False)


def _canonical_identity(identity: Mapping[str, str]) -> dict[str, str] | None:
    cleaned: dict[str, str] = {}
    for key in (
        "user_id",
        "workspace_id",
        "thread_id",
        "run_id",
        "turn_origin_automation_id",
        "automation_execution_id",
    ):
        value = _canonical_uuid(identity.get(key))
        if value is None:
            return None
        cleaned[key] = value
    return cleaned


def _canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 36:
        return None
    try:
        parsed = uuid.UUID(text)
    except (TypeError, ValueError, AttributeError):
        return None
    canonical = str(parsed)
    if canonical != text.lower():
        return None
    return canonical


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 10 or text != value:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != text:
        return None
    return text


def _token(value: object) -> str | None:
    if isinstance(value, str) and _TOKEN.fullmatch(value):
        return value
    return None


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 200 or _pathlike(text):
        return None
    folded = text.lower()
    if any(needle in folded for needle in ("secret", "token", "api_key", "gemini_api")):
        return None
    return text


def _pathlike(value: str) -> bool:
    folded = value.lower()
    if value.startswith(("/", "~", "\\")) or "\\" in value or "file:" in folded:
        return True
    return any(
        needle in folded
        for needle in ("/users/", "/home/", "/var/", "/tmp/", "/private/", "/opt/")
    )


async def _post_json(
    path: str,
    body: Mapping[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any] | None]:
    token = get_dalton_bridge_admin_token()
    if not token:
        return 0, {"ok": False, "code": "bridge_not_configured"}
    url = f"{get_dalton_bridge_base_url().rstrip('/')}{path}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Dalton-Bridge-Token": token,
    }
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        async with client.stream("POST", url, json=dict(body), headers=headers) as resp:
            raw = await _read_capped(resp)
            status = resp.status_code
    if raw is None:
        return status, {"ok": False, "code": "response_exceeds_bound"}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return status, None
    if not isinstance(data, dict):
        return status, None
    return status, data


async def _read_capped(resp: httpx.Response) -> bytes | None:
    declared = resp.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            return None
        if size < 0 or size > _MAX_HOST_RESPONSE_BYTES:
            return None
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        if not chunk:
            continue
        if len(buf) + len(chunk) > _MAX_HOST_RESPONSE_BYTES:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _refuse(code: str, error: str) -> str:
    return json.dumps({"ok": False, "code": code, "error": error}, ensure_ascii=False)


__all__ = ["create_ai_finance_signals_tools"]
