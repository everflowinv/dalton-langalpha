"""`market_digest_inputs`: prepare one batch, then read it one page at a time.

Server identity and the operator owner gate run before any host call.
The database transaction finishes before HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.database.market_digest import authorize_market_digest_execution
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_market_digest_owner_user_id,
)
from src.dalton.tools.automation_report_tools import _server_identity

logger = logging.getLogger(__name__)

PeriodName = Literal["AM", "PM"]
ActionName = Literal["prepare", "read"]
_MAX_OFFSET = 32 * 1024 * 1024
_MAX_CHARS = 32000
_DEFAULT_MAX_CHARS = 16000
_HTTP_TIMEOUT_S = 30.0
# Host timeout_seconds is at most 3600, and digest and TMTB may each use one.
# 2 * 3600 + 60 also leaves room for the execution lock.
_PREPARE_HTTP_TIMEOUT_S = 7260.0
_MAX_HOST_RESPONSE_BYTES = 128 * 1024
_READ_PATH = "/internal/market-digest/read"
_PREPARE_PATH = "/internal/market-digest/prepare"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_READY = frozenset({"ready", "ready_degraded"})
_DOCUMENT_KEYS = ("text", "emails", "tmtb_docs", "pending_snippets", "body", "content")
_HASH_KEYS = (
    "emails_sha256",
    "tmtb_sha256",
    "digest_source_sha256",
    "tmtb_source_sha256",
)
_PREPARE_OPTIONAL = (
    "emails_count",
    "priority_senders",
    "funda",
    "image_backlog",
    "tmtb",
    "digest_outcome",
    "reason",
    "reused",
    "digest_invoked",
    "tmtb_invoked",
    "schema",
)
_PAGE_KEYS = (
    "ok",
    "period",
    "date",
    "authoritative_date",
    "batch_id",
    "preparation_status",
    "degraded",
    "reason",
    "usable",
    "truncated",
    "offset",
    "max_chars",
    "total_chars",
    "next_offset",
    "content_sha256",
    "emails_count",
    "tmtb_count",
    "pending_snippets_count",
    "priority_senders",
    "funda",
    "image_backlog",
    "tmtb",
    "hashes",
    "digest_outcome",
    "started_hkt_date",
    "text",
)
_UNCONFIRMED = (
    "Prepare did not finish before the request budget. "
    "Call prepare again for this same run."
)


def create_market_digest_tools() -> list[BaseTool]:
    """Build `[market_digest_inputs]` for the configured internal owner."""

    @tool("market_digest_inputs")
    async def market_digest_inputs(
        period: PeriodName,
        date: str | None = None,
        offset: int = 0,
        max_chars: int = _DEFAULT_MAX_CHARS,
        action: ActionName = "read",
        *,
        config: RunnableConfig,
    ) -> str:
        """Prepare this run's AM or PM market-digest batch, then read it one page at a time.
        Only that running market-digest automation can call it. An ordinary chat, a wrong owner, an external member, or a wrong profile cannot.

        Call prepare first. It fixes the batch date and hashes for this server-started run. Then call read and repeat with next_offset until it is null. Keep content_sha256 the same on every page. A date other than the prepared authoritative date is rejected. ready_degraded can be read. If prepare returns prepare_unconfirmed, call prepare again for this same run; that retry keeps the original batch.

        AM and PM each keep their existing automation task and the one cron.

        Args:
            period: "AM" or "PM".
            date: Optional read date, YYYY-MM-DD. Omit it to use the prepared date. prepare does not use it.
            offset: Character offset into the serialized batch. 0 is the start.
            max_chars: Characters to return, from 1 to 32000.
            action: "prepare" or "read". The default is "read".

        Returns:
            JSON. prepare returns the fixed date, status, counts, and hashes. read returns one page.
        """
        parsed, refused = _parse_call(action, period, date, offset, max_chars)
        if refused is not None:
            return refused
        assert parsed is not None
        identity = _server_identity(config)
        if identity is None:
            return _refuse(
                "not_automation_run",
                "This turn is not a server-started automation run.",
            )
        owner = get_market_digest_owner_user_id()
        if owner is None:
            return _refuse(
                "owner_not_configured",
                "Market-digest read is not configured.",
            )
        if identity["user_id"].strip().lower() != owner:
            return _refuse(
                "not_allowed",
                "This account cannot read market-digest inputs.",
            )
        if not await is_internal_or_admin(identity["user_id"]):
            return _refuse(
                "not_allowed",
                "This account cannot read market-digest inputs.",
            )
        try:
            authorized = await authorize_market_digest_execution(
                user_id=identity["user_id"],
                workspace_id=identity["workspace_id"],
                thread_id=identity["thread_id"],
                run_id=identity["run_id"],
                automation_id=identity["turn_origin_automation_id"],
                execution_id=identity["automation_execution_id"],
                period=parsed["period"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market_digest_inputs authorize failed: %s",
                type(exc).__name__,
            )
            return _refuse("read_unavailable", "The prepared batch could not be read.")
        if not isinstance(authorized, Mapping) or authorized.get("ok") is not True:
            code = "read_unavailable"
            message = "The prepared batch could not be read."
            if isinstance(authorized, Mapping):
                code = _token(authorized.get("code")) or code
                message = _safe_text(authorized.get("error")) or message
            return _refuse(code, message)
        execution_id = identity["automation_execution_id"]
        try:
            if parsed["action"] == "prepare":
                status, payload = await _post_market_digest_prepare(parsed, execution_id)
            else:
                status, payload = await _post_market_digest_read(parsed, execution_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market_digest_inputs host failed: %s",
                type(exc).__name__,
            )
            return _refuse("host_unavailable", "The prepared batch could not be read.")
        if parsed["action"] == "prepare":
            return _finish_prepare(status, payload, parsed, execution_id)
        if status != 200 or not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return _host_failure(payload, status)
        accepted = _accept_page(payload, parsed)
        if accepted is None:
            return _refuse(
                "invalid_response",
                "The host response did not match this batch page.",
            )
        return json.dumps(accepted, ensure_ascii=False)

    return [market_digest_inputs]


def _parse_call(
    action: object,
    period: object,
    date: object,
    offset: object,
    max_chars: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if action not in {"prepare", "read"}:
        return None, _refuse("invalid_action", "action must be prepare or read.")
    if period not in {"AM", "PM"}:
        return None, _refuse("invalid_period", "period must be AM or PM.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > _MAX_OFFSET:
        return None, _refuse("invalid_offset", "offset must be a nonnegative integer.")
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not 1 <= max_chars <= _MAX_CHARS
    ):
        return None, _refuse(
            "invalid_max_chars",
            "max_chars must be an integer from 1 to 32000.",
        )
    cleaned_date = None
    if action == "read" and date is not None:
        cleaned_date = _iso_date(date)
        if cleaned_date is None:
            return None, _refuse("invalid_date", "date must be a real YYYY-MM-DD.")
    return {
        "action": action,
        "period": period,
        "date": cleaned_date,
        "offset": offset,
        "max_chars": max_chars,
    }, None


async def _post_market_digest_prepare(
    parsed: Mapping[str, Any],
    execution_id: str,
) -> tuple[int, dict[str, Any] | None]:
    body = {
        "period": parsed["period"],
        "automation_execution_id": execution_id,
    }
    try:
        return await _post_json(_PREPARE_PATH, body, _PREPARE_HTTP_TIMEOUT_S)
    except httpx.TimeoutException:
        return 0, {"ok": False, "code": "prepare_unconfirmed"}


async def _post_market_digest_read(
    parsed: Mapping[str, Any],
    execution_id: str,
) -> tuple[int, dict[str, Any] | None]:
    body: dict[str, Any] = {
        "period": parsed["period"],
        "offset": parsed["offset"],
        "max_chars": parsed["max_chars"],
        "automation_execution_id": execution_id,
    }
    if parsed["date"] is not None:
        body["date"] = parsed["date"]
    return await _post_json(_READ_PATH, body, _HTTP_TIMEOUT_S)


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


def _finish_prepare(
    status: int,
    payload: Mapping[str, Any] | None,
    parsed: Mapping[str, Any],
    execution_id: str,
) -> str:
    if isinstance(payload, Mapping) and payload.get("code") == "prepare_unconfirmed":
        return _refuse("prepare_unconfirmed", _UNCONFIRMED)
    if status == 200 and isinstance(payload, Mapping):
        if payload.get("ok") is True:
            accepted = _accept_prepare(payload, parsed, execution_id)
            if accepted is None:
                return _refuse(
                    "invalid_response",
                    "The host response did not match this prepared batch.",
                )
            return json.dumps(accepted, ensure_ascii=False)
        return _prepare_status(payload, parsed)
    return _prepare_host_failure(payload, status)


def _accept_prepare(
    payload: Mapping[str, Any],
    parsed: Mapping[str, Any],
    execution_id: str,
) -> dict[str, Any] | None:
    if any(key in payload for key in _DOCUMENT_KEYS):
        return None
    if payload.get("period") != parsed["period"]:
        return None
    if payload.get("automation_execution_id") != execution_id:
        return None
    preparation = payload.get("preparation_status")
    if preparation not in _READY or payload.get("usable") is not True:
        return None
    authoritative = payload.get("authoritative_date")
    if _iso_date(authoritative) != authoritative:
        return None
    batch_id = f"{parsed['period']}/{authoritative}"
    if payload.get("batch_id") != batch_id:
        return None
    hashes = payload.get("hashes")
    if not isinstance(hashes, Mapping):
        return None
    input_hash = hashes.get("input_sha256")
    if not isinstance(input_hash, str) or _HEX64.fullmatch(input_hash) is None:
        return None
    attempt_id = payload.get("execution_id")
    if not isinstance(attempt_id, str) or _HEX32.fullmatch(attempt_id) is None:
        return None
    kept_hashes: dict[str, Any] = {"input_sha256": input_hash}
    for key in _HASH_KEYS:
        if key not in hashes:
            continue
        value = hashes.get(key)
        if value is None or (isinstance(value, str) and _HEX64.fullmatch(value)):
            kept_hashes[key] = value
    accepted: dict[str, Any] = {
        "ok": True,
        "action": "prepare",
        "period": parsed["period"],
        "authoritative_date": authoritative,
        "batch_id": batch_id,
        "preparation_status": preparation,
        "usable": True,
        "execution_id": attempt_id,
        "automation_execution_id": execution_id,
        "hashes": kept_hashes,
    }
    started = _iso_date(payload.get("started_hkt_date"))
    if started is not None and payload.get("started_hkt_date") == started:
        accepted["started_hkt_date"] = started
    for key in _PREPARE_OPTIONAL:
        if key not in payload:
            continue
        value = payload[key]
        if _meta_safe(value):
            accepted[key] = value
    return accepted


def _prepare_status(payload: Mapping[str, Any], parsed: Mapping[str, Any]) -> str:
    preparation = _token(payload.get("preparation_status"))
    reason = _token(payload.get("reason"))
    code = _token(payload.get("code")) or preparation or "prepare_failed"
    body: dict[str, Any] = {
        "ok": False,
        "action": "prepare",
        "code": code,
        "error": "The market-digest batch was not prepared.",
    }
    if preparation is not None:
        body["preparation_status"] = preparation
    if reason is not None:
        body["reason"] = reason
    if payload.get("period") == parsed["period"]:
        body["period"] = parsed["period"]
    authoritative = payload.get("authoritative_date")
    if _iso_date(authoritative) == authoritative:
        body["authoritative_date"] = authoritative
    for key in ("emails_count", "digest_invoked", "tmtb_invoked", "reused", "batch_id"):
        if key in payload and _meta_safe(payload[key]) and key not in body:
            body[key] = payload[key]
    return json.dumps(body, ensure_ascii=False)


def _prepare_host_failure(payload: Mapping[str, Any] | None, status: int) -> str:
    code = "host_unavailable"
    preparation = None
    if isinstance(payload, Mapping):
        code = _token(payload.get("code")) or code
        preparation = _token(payload.get("preparation_status"))
    body: dict[str, Any] = {
        "ok": False,
        "action": "prepare",
        "code": code,
        "error": "The market-digest batch was not prepared.",
    }
    if preparation is not None:
        body["preparation_status"] = preparation
    if status and code == "host_unavailable":
        body["status"] = status
    return json.dumps(body, ensure_ascii=False)


def _accept_page(payload: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    if any(key in payload for key in ("emails", "tmtb_docs", "pending_snippets")):
        return None
    if payload.get("ok") is not True or payload.get("usable") is not True:
        return None
    if payload.get("truncated") is not False:
        return None
    if payload.get("period") != parsed["period"]:
        return None
    authoritative = payload.get("authoritative_date")
    if _iso_date(authoritative) != authoritative or payload.get("date") != authoritative:
        return None
    if parsed.get("date") not in (None, authoritative):
        return None
    if payload.get("batch_id") != f"{parsed['period']}/{authoritative}":
        return None
    if payload.get("preparation_status") not in _READY:
        return None
    if not isinstance(payload.get("degraded"), bool):
        return None
    digest = payload.get("content_sha256")
    if not isinstance(digest, str) or _HEX64.fullmatch(digest) is None:
        return None
    hashes = payload.get("hashes")
    if not isinstance(hashes, Mapping):
        return None
    input_hash = hashes.get("input_sha256")
    if not isinstance(input_hash, str) or _HEX64.fullmatch(input_hash) is None:
        return None
    if payload.get("offset") != parsed["offset"] or isinstance(payload.get("offset"), bool):
        return None
    if payload.get("max_chars") != parsed["max_chars"] or isinstance(payload.get("max_chars"), bool):
        return None
    total = payload.get("total_chars")
    text = payload.get("text")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    if not isinstance(text, str) or len(text) > parsed["max_chars"]:
        return None
    nxt = payload.get("next_offset")
    end = parsed["offset"] + len(text)
    if nxt is None:
        if end != total:
            return None
    elif (
        isinstance(nxt, bool)
        or not isinstance(nxt, int)
        or nxt != end
        or nxt <= parsed["offset"]
        or nxt >= total
    ):
        return None
    page: dict[str, Any] = {}
    for key in _PAGE_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if key == "text":
            page[key] = value
            continue
        if not _meta_safe(value):
            return None
        page[key] = value
    return page


def _host_failure(payload: Mapping[str, Any] | None, status: int) -> str:
    code = "host_unavailable"
    preparation = None
    if isinstance(payload, Mapping):
        code = _token(payload.get("code")) or code
        preparation = _token(payload.get("preparation_status"))
    body: dict[str, Any] = {
        "ok": False,
        "code": code,
        "error": "The prepared batch could not be read.",
    }
    if preparation is not None:
        body["preparation_status"] = preparation
    if status and code == "host_unavailable":
        body["status"] = status
    return json.dumps(body, ensure_ascii=False)


def _meta_safe(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return len(value) <= 200 and not _pathlike(value)
    if isinstance(value, list):
        return all(_meta_safe(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and len(key) <= 80 and not _pathlike(key) and _meta_safe(item)
            for key, item in value.items()
        )
    return False


def _pathlike(value: str) -> bool:
    folded = value.lower()
    if value.startswith(("/", "~", "\\")) or "\\" in value or "file:" in folded:
        return True
    return any(
        needle in folded
        for needle in ("/users/", "/home/", "/var/", "/tmp/", "/private/", "/opt/")
    )


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 10:
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
    if not text or len(text) > 200 or _pathlike(text) or "token" in text.lower():
        return None
    return text


def _refuse(code: str, error: str) -> str:
    return json.dumps(
        {"ok": False, "code": code, "error": error},
        ensure_ascii=False,
    )


__all__ = ["create_market_digest_tools"]
