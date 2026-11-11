"""`ai_finance_prepare` and `ai_finance_read` for one daily batch.

Server identity and the operator owner gate run before any host call.
The database transaction finishes before HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

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

ResourceName = Literal["manifest", "github", "earnings", "candidate"]
_MAX_OFFSET = 32 * 1024 * 1024
_MAX_CHARS = 32000
_DEFAULT_MAX_CHARS = 16000
_HTTP_TIMEOUT_S = 30.0
_PREPARE_HTTP_TIMEOUT_S = 7260.0
_MAX_HOST_RESPONSE_BYTES = 128 * 1024
_READ_PATH = "/internal/ai-finance/read"
_PREPARE_PATH = "/internal/ai-finance/prepare"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DOC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_RESOURCES = frozenset({"manifest", "github", "earnings", "candidate"})
_STAGE_STATUS = frozenset({"pending", "failed", "complete"})
_DOCUMENT_KEYS = ("text", "content", "body", "candidates", "github", "earnings")


def create_ai_finance_inputs_tools() -> list[BaseTool]:
    """Build `[ai_finance_prepare, ai_finance_read]` for the configured internal owner."""

    @tool("ai_finance_prepare")
    async def ai_finance_prepare(*, config: RunnableConfig) -> str:
        """Prepare this run's Hong Kong daily AI-finance batch. Only that running ai-finance-daily automation can call it.

        Ordinary chat, an external member, a wrong owner, or a wrong profile is refused. This call accepts no time, path, or execution id.

        Returns:
            JSON with batch_id, window, stages, and candidate doc_ids. No host path and no full text.

        Pass batch_id to ai_finance_read. For a candidate, also pass its doc_id. Follow next_offset until it is null.
        """
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
            logger.warning("ai_finance_prepare authorize failed: %s", type(exc).__name__)
            return _refuse("read_unavailable", "The prepared batch could not be read.")
        failure = _authorization_failure(authorized)
        if failure is not None:
            return failure
        assert isinstance(authorized, Mapping)
        body = _prepare_body(identity, authorized)
        if body is None:
            return _refuse("invalid_identity", "Server run identity is not a canonical UUID.")
        try:
            status, payload = await _post_json(_PREPARE_PATH, body, _PREPARE_HTTP_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            return _refuse(
                "prepare_unconfirmed",
                "Prepare did not finish before the request budget. Call prepare again for this same run.",
            )
        except Exception as exc:
            logger.warning("ai_finance_prepare host failed: %s", type(exc).__name__)
            return _refuse("host_unavailable", "The prepared batch could not be read.")
        return _finish_prepare(status, payload)

    @tool("ai_finance_read")
    async def ai_finance_read(
        batch_id: str,
        resource: ResourceName,
        doc_id: str | None = None,
        offset: int = 0,
        max_chars: int = _DEFAULT_MAX_CHARS,
        *,
        config: RunnableConfig,
    ) -> str:
        """Read one page of a prepared AI-finance batch for this owner and profile.

        A date batch prepared for this owner can be read during recovery. A path, another owner, or a doc_id outside that batch is refused.

        Args:
            batch_id: batch_id from prepare. A real YYYY-MM-DD business date.
            resource: manifest, github, earnings, or candidate.
            doc_id: Required only when resource is candidate. Use a doc_id from prepare.
            offset: Character offset. 0 is the start.
            max_chars: Characters to return, from 1 to 32000.

        Returns:
            One page plus the full-text hash, total_chars, and next_offset. next_offset means this page is not the whole resource.
        """
        parsed, refused = _parse_read(batch_id, resource, doc_id, offset, max_chars)
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
            logger.warning("ai_finance_read authorize failed: %s", type(exc).__name__)
            return _refuse("read_unavailable", "The prepared batch could not be read.")
        failure = _authorization_failure(authorized)
        if failure is not None:
            return failure
        assert isinstance(authorized, Mapping)
        body = _read_body(identity, parsed)
        if body is None:
            return _refuse("invalid_identity", "Server run identity is not a canonical UUID.")
        try:
            status, payload = await _post_json(_READ_PATH, body, _HTTP_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("ai_finance_read host failed: %s", type(exc).__name__)
            return _refuse("host_unavailable", "The prepared batch could not be read.")
        if status != 200 or not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return _host_failure(payload)
        accepted = _accept_page(payload, parsed)
        if accepted is None:
            return _refuse("invalid_response", "The host response did not match this batch page.")
        return json.dumps(accepted, ensure_ascii=False)

    return [ai_finance_prepare, ai_finance_read]


async def _gate(config: RunnableConfig) -> tuple[dict[str, str] | None, str | None]:
    identity = _server_identity(config)
    if identity is None:
        return None, _refuse("not_automation_run", "This turn is not a server-started automation run.")
    owner = get_ai_finance_owner_user_id()
    if owner is None:
        return None, _refuse("owner_not_configured", "AI-finance prepare is not configured.")
    if identity["user_id"].strip().lower() != owner:
        return None, _refuse("not_allowed", "This account cannot read AI-finance inputs.")
    if not await is_internal_or_admin(identity["user_id"]):
        return None, _refuse("not_allowed", "This account cannot read AI-finance inputs.")
    canonical = _canonical_identity(identity)
    if canonical is None:
        return None, _refuse("invalid_identity", "Server run identity is not a canonical UUID.")
    return canonical, None


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


def _authorization_failure(authorized: object) -> str | None:
    if not isinstance(authorized, Mapping) or authorized.get("ok") is not True:
        code = "read_unavailable"
        message = "The prepared batch could not be read."
        if isinstance(authorized, Mapping):
            code = _token(authorized.get("code")) or code
            message = _safe_text(authorized.get("error")) or message
        return _refuse(code, message)
    if authorized.get("profile") != PROFILE or "period" in authorized:
        return _refuse("profile_mismatch", "This automation profile is not ai-finance-daily.")
    if _parse_instant(authorized.get("scheduled_at")) is None or _parse_instant(authorized.get("started_at")) is None:
        return _refuse("execution_clock_invalid", "This automation execution has no usable schedule clock.")
    return None


def _prepare_body(identity: Mapping[str, str], authorized: Mapping[str, Any]) -> dict[str, str] | None:
    execution_id = _canonical_uuid(identity["automation_execution_id"])
    owner = get_ai_finance_owner_user_id()
    user_id = _canonical_uuid(identity["user_id"])
    workspace_id = _canonical_uuid(identity["workspace_id"])
    thread_id = _canonical_uuid(identity["thread_id"])
    scheduled_at = _parse_instant(authorized.get("scheduled_at"))
    started_at = _parse_instant(authorized.get("started_at"))
    if None in {execution_id, owner, user_id, workspace_id, thread_id, scheduled_at, started_at}:
        return None
    assert scheduled_at is not None and started_at is not None
    return {
        "automation_execution_id": execution_id,
        "scheduled_at": scheduled_at.isoformat(),
        "started_at": started_at.isoformat(),
        "owner_user_id": owner,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "profile": PROFILE,
    }


def _read_body(identity: Mapping[str, str], parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    execution_id = _canonical_uuid(identity["automation_execution_id"])
    owner = get_ai_finance_owner_user_id()
    if execution_id is None or owner is None:
        return None
    body: dict[str, Any] = {
        "automation_execution_id": execution_id,
        "owner_user_id": owner,
        "profile": PROFILE,
        "batch_id": parsed["batch_id"],
        "resource": parsed["resource"],
        "offset": parsed["offset"],
        "max_chars": parsed["max_chars"],
    }
    if parsed["doc_id"] is not None:
        body["doc_id"] = parsed["doc_id"]
    return body


def _parse_read(
    batch_id: object,
    resource: object,
    doc_id: object,
    offset: object,
    max_chars: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if _iso_date(batch_id) is None:
        return None, _refuse("invalid_batch", "batch_id must be a real YYYY-MM-DD business date.")
    if resource not in _RESOURCES:
        return None, _refuse("invalid_resource", "resource must be manifest, github, earnings, or candidate.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > _MAX_OFFSET:
        return None, _refuse("invalid_offset", "offset must be a nonnegative integer.")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 1 <= max_chars <= _MAX_CHARS:
        return None, _refuse("invalid_max_chars", "max_chars must be an integer from 1 to 32000.")
    cleaned_doc: str | None = None
    if resource == "candidate":
        if not isinstance(doc_id, str) or _DOC_ID.fullmatch(doc_id) is None or ".." in doc_id:
            return None, _refuse("invalid_doc_id", "doc_id must be a candidate id from prepare.")
        cleaned_doc = doc_id
    elif doc_id is not None:
        return None, _refuse("invalid_doc_id", "doc_id is only accepted for a candidate.")
    return {
        "batch_id": batch_id,
        "resource": resource,
        "doc_id": cleaned_doc,
        "offset": offset,
        "max_chars": max_chars,
    }, None


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


def _finish_prepare(status: int, payload: Mapping[str, Any] | None) -> str:
    if isinstance(payload, Mapping) and payload.get("code") == "prepare_unconfirmed":
        return _refuse(
            "prepare_unconfirmed",
            "Prepare did not finish before the request budget. Call prepare again for this same run.",
        )
    if status == 200 and isinstance(payload, Mapping) and payload.get("ok") is True:
        accepted = _accept_prepare(payload)
        if accepted is None:
            return _refuse("invalid_response", "The host response did not match this prepared batch.")
        return json.dumps(accepted, ensure_ascii=False)
    return _host_failure(payload)


def _accept_prepare(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    if any(key in payload for key in _DOCUMENT_KEYS):
        return None
    if payload.get("ok") is not True or payload.get("profile") != PROFILE:
        return None
    batch_id = payload.get("batch_id")
    if _iso_date(batch_id) != batch_id:
        return None
    if _parse_instant(payload.get("started_at")) is None:
        return None
    window = _window(payload.get("window"))
    stages = _stages(payload.get("stages"))
    if window is None or stages is None:
        return None
    if payload.get("manifest_ready") is True and stages != {"github": "complete", "earnings": "complete"}:
        return None
    doc_ids = payload.get("candidate_doc_ids")
    if not isinstance(doc_ids, list) or any(_safe_doc(item) is None for item in doc_ids):
        return None
    if not isinstance(payload.get("recovered"), bool) or not isinstance(payload.get("inconsistent"), bool):
        return None
    if payload.get("inconsistent") is True:
        return None
    reused = payload.get("reused")
    if not isinstance(reused, list) or any(item not in {"github", "earnings"} for item in reused):
        return None
    errors = payload.get("errors")
    if not isinstance(errors, list) or any(not isinstance(item, str) or _TOKEN.fullmatch(item) is None for item in errors):
        return None
    resources = payload.get("resources")
    if resources != ["manifest", "github", "earnings", "candidate"]:
        return None
    started_at = _parse_instant(payload.get("started_at"))
    if started_at is None or _pathlike(started_at.isoformat()):
        return None
    return {
        "ok": True,
        "profile": PROFILE,
        "batch_id": batch_id,
        "started_at": started_at.isoformat(),
        "window": window,
        "stages": stages,
        "reused": list(reused),
        "errors": list(errors),
        "inconsistent": False,
        "recovered": payload.get("recovered") is True,
        "manifest_ready": payload.get("manifest_ready") is True,
        "candidate_doc_ids": [str(item) for item in doc_ids],
        "resources": ["manifest", "github", "earnings", "candidate"],
    }


def _accept_page(payload: Mapping[str, Any], parsed: Mapping[str, Any]) -> dict[str, Any] | None:
    if payload.get("ok") is not True or payload.get("batch_id") != parsed["batch_id"]:
        return None
    if payload.get("resource") != parsed["resource"] or payload.get("profile") != PROFILE:
        return None
    if parsed["doc_id"] is None:
        if "doc_id" in payload:
            return None
    elif payload.get("doc_id") != parsed["doc_id"]:
        return None
    if payload.get("offset") != parsed["offset"] or payload.get("max_chars") != parsed["max_chars"]:
        return None
    total = payload.get("total_chars")
    text = payload.get("text")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    if not isinstance(text, str) or len(text) > parsed["max_chars"]:
        return None
    digest = payload.get("content_sha256")
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
        or nxt > total
        or complete is not False
    ):
        return None
    page: dict[str, Any] = {
        "ok": True,
        "batch_id": parsed["batch_id"],
        "resource": parsed["resource"],
        "offset": parsed["offset"],
        "max_chars": parsed["max_chars"],
        "total_chars": total,
        "next_offset": nxt,
        "complete": nxt is None,
        "content_sha256": digest,
        "text": text,
        "profile": PROFILE,
    }
    if parsed["doc_id"] is not None:
        page["doc_id"] = parsed["doc_id"]
    return page


def _host_failure(payload: Mapping[str, Any] | None) -> str:
    code = "host_unavailable"
    body: dict[str, Any] = {
        "ok": False,
        "code": code,
        "error": "The prepared batch could not be read.",
    }
    if not isinstance(payload, Mapping):
        return json.dumps(body, ensure_ascii=False)
    code = _token(payload.get("code")) or code
    body["code"] = code
    batch_id = payload.get("batch_id")
    if _iso_date(batch_id) == batch_id:
        body["batch_id"] = batch_id
    window = _window(payload.get("window"))
    if window is not None:
        body["window"] = window
    stages = _stages(payload.get("stages"))
    if stages is not None:
        body["stages"] = stages
    if isinstance(payload.get("recovered"), bool):
        body["recovered"] = payload["recovered"]
    errors = payload.get("errors")
    if (
        isinstance(errors, list)
        and all(isinstance(item, str) and _TOKEN.fullmatch(item) is not None for item in errors)
    ):
        body["errors"] = list(errors)
    return json.dumps(body, ensure_ascii=False)


def _window(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    start = _parse_instant(value.get("start"))
    end = _parse_instant(value.get("end"))
    hours = value.get("hours")
    if value.get("timezone") != "Asia/Hong_Kong" or start is None or end is None:
        return None
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
        return None
    return {
        "timezone": "Asia/Hong_Kong",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours": hours,
    }


def _stages(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != {"github", "earnings"}:
        return None
    stages: dict[str, str] = {}
    for name in ("github", "earnings"):
        status = value.get(name)
        if not isinstance(status, str) or status not in _STAGE_STATUS:
            return None
        stages[name] = status
    return stages


def _safe_doc(value: object) -> str | None:
    if isinstance(value, str) and _DOC_ID.fullmatch(value) and ".." not in value:
        return value
    return None


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


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64 or _pathlike(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


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
    return text


def _pathlike(value: str) -> bool:
    folded = value.lower()
    if value.startswith(("/", "~", "\\")) or "\\" in value or "file:" in folded:
        return True
    return any(
        needle in folded
        for needle in ("/users/", "/home/", "/var/", "/tmp/", "/private/", "/opt/")
    )


def _refuse(code: str, error: str) -> str:
    return json.dumps({"ok": False, "code": code, "error": error}, ensure_ascii=False)


__all__ = ["create_ai_finance_inputs_tools"]
