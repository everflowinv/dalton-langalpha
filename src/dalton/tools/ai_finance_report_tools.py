"""`ai_finance_report` for one saved AI-finance daily.

Server identity and the operator owner gate run before any host call.
The database transaction finishes before HTTP. Submit and finalize use the
card saved for that batch. Seen state changes only after delivery is confirmed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from src.dalton.database.automation_reports import automation_report_content_sha256
from src.dalton.settings import (
    get_ai_finance_owner_user_id,
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
)
from src.dalton.tools.automation_report_tools import (
    _server_identity,
    create_automation_report_tools,
)

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"submit", "status", "finalize"})
_HOST_PATH = "/internal/ai-finance/report"
_FIRST_DATE_ENV = "DALTON_AI_FINANCE_FIRST_REPORT_DATE"
_MARKER = "CRON_RECOVERED_OK"
_HTTP_TIMEOUT_SAVE_S = 60.0
_HTTP_TIMEOUT_READ_S = 30.0
_MAX_HOST_RESPONSE_BYTES = 384 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_RETRY_STATUS = frozenset({"pending", "unknown", "failed"})
_HOLD_STATUS = frozenset({"partial", "in_flight", "sent"})
_FORBIDDEN_HOST_KEYS = frozenset({
    "path",
    "state_root",
    "markdown",
    "text",
    "body",
    "content",
    "argv",
    "env",
    "stdout",
    "stderr",
    "api_key",
})


def create_ai_finance_report_tools() -> list[BaseTool]:
    """Build `[ai_finance_report]` for the configured internal owner."""

    @tool("ai_finance_report")
    async def ai_finance_report(
        action: str,
        batch_id: str,
        markdown: str | None = None,
        news_events: dict[str, Any] | None = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Save this run's AI-finance daily and finish its broadcast state, or read that progress.

        Use it after the signal bundle for this batch is ingested. submit sends the saved card. status only reads. finalize records news and GitHub seen after delivery is confirmed. A path, recipient, credential, model, or sent flag is refused. One batch keeps one daily.

        Args:
            action: submit, status, or finalize.
            batch_id: YYYY-MM-DD batch from ai_finance_prepare.
            markdown: Full daily on the first submit. Omit it when that daily is already saved. status and finalize reject it.
            news_events: Seen map of 64-character hex keys to numbers, one key per news item. Pass it only with markdown.

        Returns:
            JSON with delivery_confirmed and seen_status, and no card, markdown, or path.

        Reply with exactly CRON_RECOVERED_OK when marker is CRON_RECOVERED_OK. delivery_confirmed false, pending, unknown, failed, partial, and in_flight are not sent. finalize is the only action that records seen, and only after the saved card is confirmed sent.
        """
        parsed, refused = _parse_call(action, batch_id, markdown, news_events)
        if refused is not None:
            return refused
        assert parsed is not None
        identity, refused = await _gate(str(parsed["action"]), config)
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
            logger.warning("ai_finance_report authorize failed: %s", type(exc).__name__)
            return _refuse(str(parsed["action"]), "report_unavailable", "The daily could not be used.")
        failure = _authorization_failure(str(parsed["action"]), authorized)
        if failure is not None:
            return failure
        closed = _closed_date(str(parsed["batch_id"])) if parsed["action"] in {"submit", "finalize"} else None
        if closed is not None:
            return _refuse(str(parsed["action"]), closed, _closed_message(closed))
        try:
            saved, refusal = await _load_or_save(identity, parsed)
        except asyncio.CancelledError:
            raise
        if refusal is not None:
            return refusal
        assert saved is not None
        if saved["content_sha256"] != _official_hash(saved["card"]):
            return _refuse(str(parsed["action"]), "state_corrupt", "The saved card failed the hash check.")
        try:
            return await _finish(str(parsed["action"]), config, saved)
        except asyncio.CancelledError:
            raise

    return [ai_finance_report]


async def _gate(action: str, config: RunnableConfig) -> tuple[dict[str, str] | None, str | None]:
    identity = _server_identity(config)
    if identity is None:
        return None, _refuse(action, "not_automation_run", "This turn is not a server-started automation run.")
    owner = get_ai_finance_owner_user_id()
    if owner is None:
        return None, _refuse(action, "owner_not_configured", "AI-finance reports are not configured.")
    if identity["user_id"].strip().lower() != owner:
        return None, _refuse(action, "not_allowed", "This account cannot use AI-finance reports.")
    if not await is_internal_or_admin(identity["user_id"]):
        return None, _refuse(action, "not_allowed", "This account cannot use AI-finance reports.")
    canonical = _canonical_identity(identity)
    if canonical is None:
        return None, _refuse(action, "invalid_identity", "Server run identity is not a canonical UUID.")
    return canonical, None


def _parse_call(
    action: object,
    batch_id: object,
    markdown: object,
    news_events: object,
) -> tuple[dict[str, Any] | None, str | None]:
    if action not in _ACTIONS:
        return None, _refuse("submit", "invalid_action", "action must be submit, status, or finalize.")
    if _iso_date(batch_id) is None:
        return None, _refuse(str(action), "invalid_batch", "batch_id must be a real YYYY-MM-DD business date.")
    if action != "submit" and (markdown is not None or news_events is not None):
        return None, _refuse(str(action), "invalid_request", "Only submit accepts the daily and the news keys.")
    if action == "submit":
        if (markdown is None) != (news_events is None):
            return None, _refuse("submit", "invalid_request", "Pass the daily and the news keys together.")
        if markdown is not None and (not isinstance(markdown, str) or not markdown or not isinstance(news_events, dict)):
            return None, _refuse("submit", "invalid_request", "The daily and the news keys are not valid.")
    return {
        "action": action,
        "batch_id": batch_id,
        "markdown": markdown,
        "news_events": news_events,
    }, None


def _closed_date(batch_id: str) -> str | None:
    raw = os.environ.get(_FIRST_DATE_ENV, "").strip()
    if not raw:
        return None
    floor = _iso_date(raw)
    if floor is None:
        return "config_invalid"
    if batch_id < floor:
        return "report_date_closed"
    return None


def _closed_message(code: str) -> str:
    if code == "config_invalid":
        return "AI-finance config is invalid."
    return "This business date is outside the allowed report window."


async def _load_or_save(
    identity: Mapping[str, str],
    parsed: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    action = str(parsed["action"])
    loaded, refusal = await _host_get(identity, str(parsed["batch_id"]))
    if refusal == "report_missing" and action == "submit" and parsed["markdown"] is not None:
        saved, save_refusal, violations, violation_count = await _host_save(identity, parsed)
        if save_refusal is not None:
            return None, _refuse(
                action,
                save_refusal,
                _host_error_text(save_refusal),
                violations,
                violation_count,
            )
        return saved, None
    if refusal is not None:
        code = refusal if _TOKEN.fullmatch(refusal) else "host_unavailable"
        return None, _refuse(action, code, _host_error_text(code))
    assert loaded is not None
    if action == "submit" and parsed["markdown"] is not None:
        checked, conflict, violations, violation_count = await _host_save(identity, parsed)
        if conflict is not None:
            return None, _refuse(
                action,
                conflict,
                _host_error_text(conflict),
                violations,
                violation_count,
            )
        return checked, None
    return loaded, None


async def _host_get(
    identity: Mapping[str, str],
    batch_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    body = _request_body(identity, "get", batch_id)
    if body is None:
        return None, "invalid_identity"
    try:
        status, payload = await _post_json(body, _HTTP_TIMEOUT_READ_S)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        return None, "report_timeout"
    except Exception as exc:
        logger.warning("ai_finance_report host read failed: %s", type(exc).__name__)
        return None, "host_unavailable"
    return _accept_host(payload, status, action="get", batch_id=batch_id)


async def _host_save(
    identity: Mapping[str, str],
    parsed: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None, list[str], int | None]:
    body = _request_body(identity, "save", str(parsed["batch_id"]))
    if body is None:
        return None, "invalid_identity", [], None
    body["markdown"] = parsed["markdown"]
    body["news_events"] = parsed["news_events"]
    try:
        status, payload = await _post_json(body, _HTTP_TIMEOUT_SAVE_S)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        return None, "report_timeout", [], None
    except Exception as exc:
        logger.warning("ai_finance_report host save failed: %s", type(exc).__name__)
        return None, "host_unavailable", [], None
    saved, code = _accept_host(payload, status, action="save", batch_id=str(parsed["batch_id"]))
    if code is None:
        return saved, None, [], None
    if code != "report_invalid":
        return None, code, [], None
    violations, violation_count = _report_invalid_feedback(payload)
    return None, code, violations, violation_count


async def _host_commit(
    identity: Mapping[str, str],
    saved: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    body = _request_body(identity, "commit_seen", str(saved["batch_id"]))
    if body is None:
        return None, "invalid_identity"
    body["body_sha256"] = saved["body_sha256"]
    body["content_sha256"] = saved["content_sha256"]
    try:
        status, payload = await _post_json(body, _HTTP_TIMEOUT_READ_S)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        return None, "report_timeout"
    except Exception as exc:
        logger.warning("ai_finance_report seen commit failed: %s", type(exc).__name__)
        return None, "host_unavailable"
    if status != 200 or not isinstance(payload, Mapping) or payload.get("ok") is not True:
        return None, _failure_code(payload)
    if payload.get("action") != "commit_seen" or payload.get("seen_status") != "complete":
        return None, "seen_incomplete"
    if payload.get("body_sha256") != saved["body_sha256"] or payload.get("content_sha256") != saved["content_sha256"]:
        return None, "hash_mismatch"
    return {"seen_status": "complete", "reused": payload.get("reused") is True}, None


def _request_body(identity: Mapping[str, str], action: str, batch_id: str) -> dict[str, Any] | None:
    execution_id = _canonical_uuid(identity["automation_execution_id"])
    owner = get_ai_finance_owner_user_id()
    user_id = _canonical_uuid(identity["user_id"])
    workspace_id = _canonical_uuid(identity["workspace_id"])
    thread_id = _canonical_uuid(identity["thread_id"])
    if None in {execution_id, owner, user_id, workspace_id, thread_id}:
        return None
    return {
        "action": action,
        "batch_id": batch_id,
        "automation_execution_id": execution_id,
        "owner_user_id": owner,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "profile": PROFILE,
    }


def _accept_host(
    payload: object,
    status: int,
    *,
    action: str,
    batch_id: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, Mapping):
        return None, "invalid_response"
    if _FORBIDDEN_HOST_KEYS & set(payload):
        return None, "invalid_response"
    if payload.get("ok") is not True or status != 200:
        return None, _failure_code(payload)
    if payload.get("action") != action or payload.get("code") != "ok" or payload.get("profile") != PROFILE:
        return None, "invalid_response"
    if payload.get("batch_id") != batch_id or payload.get("report_run_id") != f"ai-finance:{batch_id}":
        return None, "invalid_response"
    owner = get_ai_finance_owner_user_id()
    if not isinstance(owner, str) or payload.get("owner_user_id") != owner:
        return None, "invalid_response"
    card = payload.get("card")
    if not isinstance(card, dict):
        return None, "invalid_response"
    body_sha = payload.get("body_sha256")
    content_sha = payload.get("content_sha256")
    bundle_hash = payload.get("bundle_hash")
    if not all(isinstance(item, str) and _HEX64.fullmatch(item) for item in (body_sha, content_sha, bundle_hash)):
        return None, "invalid_response"
    if payload.get("seen_status") not in {"pending", "complete"} or not isinstance(payload.get("reused"), bool):
        return None, "invalid_response"
    return {
        "batch_id": batch_id,
        "report_run_id": f"ai-finance:{batch_id}",
        "body_sha256": body_sha,
        "content_sha256": content_sha,
        "bundle_hash": bundle_hash,
        "seen_status": payload["seen_status"],
        "reused": payload["reused"],
        "card": card,
    }, None


def _official_hash(card: dict[str, Any]) -> str | None:
    try:
        return automation_report_content_sha256(
            profile=PROFILE,
            content_kind="cards",
            cards=[card],
            failure_text=None,
        )
    except (TypeError, ValueError):
        return None


async def _finish(action: str, config: RunnableConfig, saved: Mapping[str, Any]) -> str:
    current, kind = await _delivery_status(config, str(saved["report_run_id"]), str(saved["content_sha256"]))
    if action == "submit" and kind in {"retry", "unknown"}:
        submitted = await _submit_card(config, saved)
        if submitted == "cancelled":
            raise asyncio.CancelledError
        current, kind = await _delivery_status(config, str(saved["report_run_id"]), str(saved["content_sha256"]))
    if kind == "unavailable":
        return _model(action, saved, delivery_confirmed=False, code="report_unavailable", status=None, ok=False)
    if kind != "sent":
        return _model(
            action,
            saved,
            delivery_confirmed=False,
            code="report_unconfirmed",
            status=_status_token(current),
            ok=True,
        )
    if action != "finalize":
        if saved["seen_status"] == "complete":
            return _model(action, saved, delivery_confirmed=True, code="ok", status="sent", ok=True, done=True)
        return _model(action, saved, delivery_confirmed=True, code="seen_pending", status="sent", ok=True)
    identity = _server_identity(config)
    if identity is None:
        return _refuse(action, "not_automation_run", "This turn is not a server-started automation run.")
    canonical = _canonical_identity(identity)
    if canonical is None:
        return _refuse(action, "invalid_identity", "Server run identity is not a canonical UUID.")
    committed, refusal = await _host_commit(canonical, saved)
    if refusal is not None or committed is None:
        code = refusal or "seen_incomplete"
        return _model(action, saved, delivery_confirmed=True, code=code, status="sent", ok=False)
    saved_done = dict(saved)
    saved_done["seen_status"] = "complete"
    if isinstance(committed.get("reused"), bool):
        saved_done["reused"] = committed["reused"]
    return _model(action, saved_done, delivery_confirmed=True, code="ok", status="sent", ok=True, done=True)


async def _delivery_status(
    config: RunnableConfig,
    report_run_id: str,
    content_sha256: str,
) -> tuple[dict[str, Any] | None, str]:
    try:
        payload = await _call_report(action="status", report_run_id=report_run_id, config=config)
    except asyncio.CancelledError:
        raise
    return payload, _delivery_kind(payload, report_run_id, content_sha256)


async def _submit_card(config: RunnableConfig, saved: Mapping[str, Any]) -> str | None:
    try:
        await _call_report(
            action="submit",
            report_run_id=str(saved["report_run_id"]),
            material="yes",
            cards=[saved["card"]],
            config=config,
        )
    except asyncio.CancelledError:
        raise
    return None


async def _call_report(**kwargs: Any) -> dict[str, Any] | None:
    try:
        tool = create_automation_report_tools()[0]
        raw = await tool.coroutine(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("ai_finance_report delivery call failed: %s", type(exc).__name__)
        return None
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _delivery_kind(payload: object, report_run_id: str, content_sha256: str) -> str:
    if not isinstance(payload, Mapping):
        return "unknown"
    if (
        payload.get("ok") is True
        and payload.get("profile") == PROFILE
        and payload.get("content_kind") == "cards"
        and payload.get("report_run_id") == report_run_id
        and payload.get("content_sha256") == content_sha256
        and payload.get("status") == "sent"
        and payload.get("delivery_confirmed") is True
    ):
        return "sent"
    status = payload.get("status")
    if status in _HOLD_STATUS:
        return "hold"
    if status in _RETRY_STATUS or payload.get("found") is False:
        return "retry"
    if payload.get("ok") is False and payload.get("code") in {"not_found", "report_missing"}:
        return "retry"
    if payload.get("ok") is False:
        return "unavailable"
    return "unknown"


def _model(
    action: str,
    saved: Mapping[str, Any],
    *,
    delivery_confirmed: bool,
    code: str,
    status: str | None,
    ok: bool,
    done: bool = False,
) -> str:
    body: dict[str, Any] = {
        "ok": ok,
        "action": action,
        "code": code,
        "batch_id": saved["batch_id"],
        "profile": PROFILE,
        "report_run_id": saved["report_run_id"],
        "body_sha256": saved["body_sha256"],
        "content_sha256": saved["content_sha256"],
        "seen_status": saved["seen_status"],
        "delivery_confirmed": delivery_confirmed,
        "reused": saved.get("reused") is True,
    }
    if status is not None:
        body["status"] = status
    if done and delivery_confirmed and saved["seen_status"] == "complete":
        body["marker"] = _MARKER
    return json.dumps(body, ensure_ascii=False)


def _status_token(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    status = payload.get("status")
    if isinstance(status, str) and status in _RETRY_STATUS | _HOLD_STATUS:
        return status
    return None


def _authorization_failure(action: str, authorized: object) -> str | None:
    if not isinstance(authorized, Mapping) or authorized.get("ok") is not True:
        code = "report_unavailable"
        message = "The daily could not be used."
        if isinstance(authorized, Mapping):
            code = _token(authorized.get("code")) or code
            message = _safe_text(authorized.get("error")) or message
        return _refuse(action, code, message)
    if authorized.get("profile") != PROFILE:
        return _refuse(action, "profile_mismatch", "This automation profile is not ai-finance-daily.")
    return None


def _report_invalid_feedback(payload: object) -> tuple[list[str], int | None]:
    if not isinstance(payload, Mapping) or _FORBIDDEN_HOST_KEYS & set(payload):
        return [], None
    raw_count = payload.get("violation_count")
    count = raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) and 0 <= raw_count <= 100 else None
    return _bounded_violations(payload.get("violations")), count


_VIOLATION_CREDENTIAL = re.compile(
    r"(?i)(?<![\w.-])(?:"
    r"bearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|sk-[A-Za-z0-9-]{16,}"
    r"|(?:api[_-]?key|secret|token|password|passwd|authorization)(?![\w.-])\s*[:=]\s*[^\s,，。;]+"
    r")"
)
_VIOLATION_URL = re.compile(r"(?i)\b(?:https?|file)://[^\s,，。;]+")
_VIOLATION_DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s,，。;]*")
_VIOLATION_UNC = re.compile(r"\\\\[^\s,，。;]*")
_VIOLATION_TILDE = re.compile(r"~[/\\][^\s,，。;]*")
_VIOLATION_ABSOLUTE = re.compile(r"(?<![\w.-])/(?:[^\s,，。;\\])+")
_VIOLATION_SLASH_RUN = re.compile(r"[^\s,，。;]*[/\\][^\s,，。;]*")
_VIOLATION_EDGE = "([{\"'.,:;)]}"


def _violation_repo(token: str) -> bool:
    if len(token) > 201 or token.count("/") != 1 or "\\" in token:
        return False
    owner, repo = token.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        return False
    if not re.fullmatch(r"[\w.-]{1,100}", owner) or not re.fullmatch(r"[\w.-]{1,100}", repo):
        return False
    return bool(re.search(r"[A-Za-z0-9]", owner) and re.search(r"[A-Za-z0-9]", repo))


def _keep_violation_repo(match: re.Match[str]) -> str:
    token = match.group(0).strip(_VIOLATION_EDGE)
    if _violation_repo(token):
        return token
    return " "


def _clip_violation(text: str) -> str:
    if len(text) <= 180:
        return text
    clipped = text[:180]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.strip()


def _violation_residue_unsafe(text: str) -> bool:
    if "~/" in text or "~\\" in text or _VIOLATION_CREDENTIAL.search(text):
        return True
    residue = _VIOLATION_SLASH_RUN.sub(
        lambda match: " " if _violation_repo(match.group(0).strip(_VIOLATION_EDGE)) else match.group(0),
        text,
    )
    return "/" in residue or "\\" in residue


def _public_violation(text: str) -> str:
    cleaned = _VIOLATION_CREDENTIAL.sub(" ", text)
    cleaned = _VIOLATION_URL.sub(" ", cleaned)
    cleaned = _VIOLATION_DRIVE.sub(" ", cleaned)
    cleaned = _VIOLATION_UNC.sub(" ", cleaned)
    cleaned = _VIOLATION_TILDE.sub(" ", cleaned)
    cleaned = _VIOLATION_ABSOLUTE.sub(" ", cleaned)
    cleaned = _VIOLATION_SLASH_RUN.sub(_keep_violation_repo, cleaned)
    cleaned = _clip_violation(re.sub(r"\s+", " ", cleaned).strip())
    cleaned = _VIOLATION_SLASH_RUN.sub(_keep_violation_repo, cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 180:
        cleaned = _clip_violation(cleaned)
    if not cleaned or _violation_residue_unsafe(cleaned):
        return ""
    return cleaned[:180]


def _bounded_violations(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    safe: list[str] = []
    for item in raw[:8]:
        if not isinstance(item, str):
            continue
        cleaned = _public_violation(item)
        if cleaned:
            safe.append(cleaned)
    return safe


def _failure_code(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return "host_unavailable"
    if _FORBIDDEN_HOST_KEYS & set(payload):
        return "invalid_response"
    code = _token(payload.get("code"))
    return code or "host_unavailable"


def _host_error_text(code: str) -> str:
    sentences = {
        "report_missing": "Save the daily before this action.",
        "content_conflict": "This batch already has a different daily.",
        "report_invalid": "The daily did not pass the report check.",
        "report_date_closed": "This business date is outside the allowed report window.",
        "report_timeout": "The report operation did not finish before the timeout.",
        "not_ready": "The ingested signal bundle is not ready for a report.",
        "hash_mismatch": "The saved report failed the hash check.",
        "state_corrupt": "The saved card failed the hash check.",
        "seen_incomplete": "The broadcast state was only partly updated. Retry the same commit.",
        "invalid_response": "The host response did not match this daily.",
        "host_unavailable": "The daily could not be used.",
        "report_unavailable": "The daily could not be used.",
    }
    return sentences.get(code, "The report operation did not finish.")


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
    if not text or len(text) > 200 or "/" in text or "\\" in text:
        return None
    folded = text.lower()
    if any(needle in folded for needle in ("secret", "token", "api_key")):
        return None
    return text


async def _post_json(body: Mapping[str, Any], timeout: float) -> tuple[int, dict[str, Any] | None]:
    token = get_dalton_bridge_admin_token()
    if not token:
        return 0, {"ok": False, "code": "bridge_not_configured"}
    url = f"{get_dalton_bridge_base_url().rstrip('/')}{_HOST_PATH}"
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


def _refuse(
    action: str,
    code: str,
    error: str,
    violations: list[str] | None = None,
    violation_count: int | None = None,
) -> str:
    body: dict[str, Any] = {
        "ok": False,
        "action": action,
        "code": code,
        "delivery_confirmed": False,
        "error": error,
    }
    if code == "report_invalid":
        if violations:
            body["violations"] = violations
        count = violation_count if violation_count is not None else (len(violations) if violations else None)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            body["violation_count"] = count
    return json.dumps(body, ensure_ascii=False)


__all__ = ["create_ai_finance_report_tools"]
