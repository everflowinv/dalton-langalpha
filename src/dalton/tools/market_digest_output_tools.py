"""Save one market-digest body and finish its archive after the report is sent.

The Markdown store and the automation report are separate. Archive runs
only after ``automation_report`` has refreshed a sent, delivery-confirmed
card batch for this exact body. A finalize timeout leaves that report
unchanged and does not start another attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import is_internal_or_admin
from src.dalton.data_jobs.notification_payloads import markdown_to_card_elements
from src.dalton.database.automation_reports import automation_report_content_sha256
from src.dalton.database.market_digest import authorize_market_digest_execution
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_bridge_base_url,
    get_market_digest_owner_user_id,
)
from src.dalton.tools.automation_report_tools import (
    _server_identity,
    create_automation_report_tools,
)

logger = logging.getLogger(__name__)

_ACTIONS = frozenset({"submit", "status", "finalize"})
_PERIODS = frozenset({"AM", "PM"})
_PROFILES = {"AM": "market-digest-am", "PM": "market-digest-pm"}
_RECOVER_STATUSES = frozenset({"pending", "unknown", "failed"})
_MAX_BODY_BYTES = 128 * 1024
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_GET_BYTES = 1024 * 1024
_MAX_FINALIZE_BYTES = 128 * 1024
_GET_TIMEOUT_S = 30.0
_SAVE_TIMEOUT_S = 30.0
_FINALIZE_TIMEOUT_S = 360.0
_READ_CHUNK = 65536
_H1 = {
    "AM": "# \U0001f4ca \u5e02\u573a\u6668\u62a5 \u2014 {date}",
    "PM": "# \U0001f4ca \u5e02\u573a\u665a\u62a5 \u2014 {date}",
}
_CARD_TITLE = {
    "AM": "\U0001f4ca \u5e02\u573a\u6668\u62a5 \u2014 {date}",
    "PM": "\U0001f4ca \u5e02\u573a\u665a\u62a5 \u2014 {date}",
}
_SOURCE_HEADING = "## \U0001f4dd \u6765\u6e90"
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_TOKEN_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_RUN_ID_RE = re.compile(r"^market-digest:(AM|PM):\d{4}-\d{2}-\d{2}$")
_FIELDS = (
    "ok",
    "action",
    "code",
    "stage",
    "found",
    "period",
    "profile",
    "date",
    "report_run_id",
    "body_sha256",
    "input_sha256",
    "byte_count",
    "section_count",
    "source_line_count",
    "archive_status",
    "embedding_status",
    "index_status",
    "chunk_count",
    "report_ok",
    "report_found",
    "report_status",
    "report_delivery_confirmed",
    "report_content_kind",
    "content_sha256",
    "finalized",
)


@dataclass(frozen=True)
class _Saved:
    markdown: str
    period: str
    date: str
    report_run_id: str
    body_sha256: str
    input_sha256: str
    byte_count: int
    section_count: int
    source_line_count: int
    archive_status: str
    embedding_status: str
    index_status: str
    chunk_count: int | None


@dataclass(frozen=True)
class _Http:
    kind: str
    code: str
    payload: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _Report:
    ok: bool
    found: bool | None
    status: str | None
    delivery_confirmed: bool
    content_kind: str | None
    content_sha256: str | None
    report_run_id: str | None
    profile: str | None
    code: str | None
    failure: bool
    malformed: bool


def _token(value: object) -> str | None:
    if isinstance(value, str) and _TOKEN_RE.fullmatch(value):
        return value
    return None


def _nonneg(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _canonical_user_id(value: str) -> str | None:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _iso_date(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.strftime("%Y-%m-%d") != value:
        return None
    return value


def _is_skip(markdown: str) -> bool:
    lines = markdown.splitlines()
    if len(markdown.strip()) == 0 or len(lines) != 1:
        return False
    text = lines[0].strip()
    return text == "SKIP" or text.startswith("SKIP:")


def _section_count(markdown: str) -> int:
    return sum(1 for line in markdown.splitlines() if line.startswith("## "))


def _source_line_count(markdown: str) -> int:
    """Count non-empty lines in the host's trailing source section."""
    collecting = False
    count = 0
    for line in markdown.splitlines():
        if line == _SOURCE_HEADING:
            collecting = True
            continue
        if not collecting:
            continue
        if line.startswith("## "):
            break
        if line.strip():
            count += 1
    return count


def _dump(
    *,
    action: str | None,
    ok: bool,
    code: str,
    stage: str,
    found: bool | None = None,
    period: str | None = None,
    profile: str | None = None,
    saved: _Saved | None = None,
    report: _Report | None = None,
    delivery: bool = False,
    finalized: bool = False,
    content_sha256: str | None = None,
    archive_status: str | None = None,
    embedding_status: str | None = None,
    index_status: str | None = None,
    chunk_count: int | None = None,
) -> str:
    """Return the fixed tool JSON. Host prose, bodies, and ids are not copied."""
    safe_code = _token(code) or "unavailable"
    safe_stage = _token(stage) or "unavailable"
    payload: dict[str, Any] = {key: None for key in _FIELDS}
    payload.update(
        {
            "ok": bool(ok),
            "action": action if action in _ACTIONS else None,
            "code": safe_code,
            "stage": safe_stage,
            "found": found if isinstance(found, bool) else None,
            "period": period if period in _PERIODS else None,
            "profile": profile if profile in _PROFILES.values() else None,
            "report_delivery_confirmed": bool(delivery),
            "finalized": bool(finalized),
        }
    )
    if saved is not None:
        run_id = saved.report_run_id if _RUN_ID_RE.fullmatch(saved.report_run_id) else None
        body_sha = saved.body_sha256 if _HEX64_RE.fullmatch(saved.body_sha256) else None
        input_sha = saved.input_sha256 if _HEX64_RE.fullmatch(saved.input_sha256) else None
        payload.update(
            {
                "date": saved.date if _iso_date(saved.date) else None,
                "report_run_id": run_id,
                "body_sha256": body_sha,
                "input_sha256": input_sha,
                "byte_count": saved.byte_count,
                "section_count": saved.section_count,
                "source_line_count": saved.source_line_count,
                "archive_status": _token(saved.archive_status),
                "embedding_status": _token(saved.embedding_status),
                "index_status": _token(saved.index_status),
                "chunk_count": saved.chunk_count,
            }
        )
    if archive_status is not None:
        payload["archive_status"] = _token(archive_status)
    if embedding_status is not None:
        payload["embedding_status"] = _token(embedding_status)
    if index_status is not None:
        payload["index_status"] = _token(index_status)
    if chunk_count is not None:
        payload["chunk_count"] = _nonneg(chunk_count)
    if report is not None:
        kind = report.content_kind if report.content_kind in {"cards", "failure_text"} else None
        payload.update(
            {
                "report_ok": bool(report.ok) and not report.malformed,
                "report_found": report.found if isinstance(report.found, bool) else None,
                "report_status": _token(report.status),
                "report_content_kind": kind,
            }
        )
    if content_sha256 is not None and _HEX64_RE.fullmatch(content_sha256):
        payload["content_sha256"] = content_sha256
    return json.dumps(payload, ensure_ascii=False)


def _refuse(action: str | None, code: str, *, period: str | None = None) -> str:
    return _dump(action=action, ok=False, code=code, stage="refused", period=period)


def _parse_call(
    action: object, period: object, markdown: object
) -> tuple[str, str, str | None] | str:
    chosen = action if isinstance(action, str) else ""
    if chosen not in _ACTIONS:
        return _refuse(None, "invalid_action")
    selected = period if isinstance(period, str) else ""
    if selected not in _PERIODS:
        return _refuse(chosen, "invalid_period")
    if chosen != "submit":
        if markdown is not None:
            return _refuse(chosen, "invalid_argument", period=selected)
        return chosen, selected, None
    if markdown is None:
        return chosen, selected, None
    if not isinstance(markdown, str):
        return _refuse(chosen, "body_invalid", period=selected)
    if "\x00" in markdown or markdown.strip() == "":
        return _refuse(chosen, "body_invalid", period=selected)
    if _is_skip(markdown):
        return _refuse(chosen, "skip_not_report", period=selected)
    try:
        size = len(markdown.encode("utf-8"))
    except UnicodeError:
        return _refuse(chosen, "body_invalid", period=selected)
    if size > _MAX_BODY_BYTES:
        return _refuse(chosen, "body_too_large", period=selected)
    return chosen, selected, markdown


def _validate_saved(payload: Mapping[str, Any], period: str) -> _Saved | str:
    """Check one host get body. The Markdown is kept exactly."""
    if payload.get("code") != "ok":
        return "output_unavailable"
    if payload.get("period") != period:
        return "state_corrupt"
    date_value = _iso_date(payload.get("date"))
    if date_value is None:
        return "state_corrupt"
    report_run_id = f"market-digest:{period}:{date_value}"
    if payload.get("report_run_id") != report_run_id:
        return "state_corrupt"
    body_sha = payload.get("body_sha256")
    input_sha = payload.get("input_sha256")
    prepared = payload.get("prepared_execution_id")
    if not isinstance(body_sha, str) or _HEX64_RE.fullmatch(body_sha) is None:
        return "state_corrupt"
    if not isinstance(input_sha, str) or _HEX64_RE.fullmatch(input_sha) is None:
        return "state_corrupt"
    if not isinstance(prepared, str) or _HEX32_RE.fullmatch(prepared) is None:
        return "state_corrupt"
    stored_execution = payload.get("automation_execution_id")
    if (
        not isinstance(stored_execution, str)
        or not stored_execution
        or any(ch in stored_execution for ch in "\r\n\x00")
    ):
        return "state_corrupt"
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        return "state_corrupt"
    try:
        raw = markdown.encode("utf-8")
    except UnicodeError:
        return "body_invalid"
    byte_count = _nonneg(payload.get("byte_count"))
    if byte_count is None or byte_count != len(raw) or byte_count > _MAX_BODY_BYTES:
        return "body_invalid"
    if hashlib.sha256(raw).hexdigest() != body_sha:
        return "body_invalid"
    if _is_skip(markdown) or "\x00" in markdown:
        return "skip_not_report"
    lines = markdown.splitlines()
    title = _H1[period].format(date=date_value)
    if not lines or lines[0] != title:
        return "body_invalid"
    headings = [line for line in lines if line.startswith("## ")]
    if len(headings) < 2 or _SOURCE_HEADING not in headings or headings[-1] != _SOURCE_HEADING:
        return "body_invalid"
    sections = len(headings)
    sources = _source_line_count(markdown)
    if sources < 1:
        return "body_invalid"
    if _nonneg(payload.get("section_count")) != sections:
        return "body_invalid"
    if _nonneg(payload.get("source_line_count")) != sources:
        return "body_invalid"
    archive_status = _token(payload.get("archive_status"))
    embedding_status = _token(payload.get("embedding_status"))
    index_status = _token(payload.get("index_status"))
    if archive_status is None or embedding_status is None or index_status is None:
        return "state_corrupt"
    if "chunk_count" not in payload or payload.get("chunk_count") is None:
        chunk_count = None
    else:
        chunk_count = _nonneg(payload.get("chunk_count"))
        if chunk_count is None:
            return "state_corrupt"
    return _Saved(
        markdown=markdown,
        period=period,
        date=date_value,
        report_run_id=report_run_id,
        body_sha256=body_sha,
        input_sha256=input_sha,
        byte_count=byte_count,
        section_count=sections,
        source_line_count=sources,
        archive_status=archive_status,
        embedding_status=embedding_status,
        index_status=index_status,
        chunk_count=chunk_count,
    )


def _host_done(saved: _Saved) -> bool:
    return (
        saved.archive_status == "done"
        and saved.embedding_status == "ready"
        and saved.index_status == "done"
        and saved.chunk_count is not None
    )


def _request_payload(
    route: str,
    *,
    period: str,
    execution_id: str,
    markdown: str | None = None,
    body_sha256: str | None = None,
) -> dict[str, Any]:
    if route == "get":
        return {"period": period, "automation_execution_id": execution_id}
    if route == "save":
        return {
            "period": period,
            "automation_execution_id": execution_id,
            "markdown": markdown,
        }
    return {
        "period": period,
        "automation_execution_id": execution_id,
        "body_sha256": body_sha256,
    }


async def _read_limited(response: httpx.Response, limit: int) -> bytes | None:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    size = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes(chunk_size=_READ_CHUNK):
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _post(
    route: str,
    body: Mapping[str, Any],
    *,
    timeout_s: float,
    max_response_bytes: int,
) -> _Http:
    try:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return _Http("error", "request_invalid")
    if len(encoded) > _MAX_REQUEST_BYTES:
        return _Http("error", "request_too_large")
    base = get_dalton_bridge_base_url().strip().rstrip("/")
    token = get_dalton_bridge_admin_token().strip()
    if not token or not base.startswith(("http://", "https://")):
        return _Http("error", "bridge_not_configured")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Dalton-Bridge-Token": token,
    }
    url = f"{base}/internal/market-digest/output/{route}"
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            async with client.stream("POST", url, content=encoded, headers=headers) as response:
                raw = await _read_limited(response, max_response_bytes)
                status_code = response.status_code
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        return _Http("unconfirmed", "timeout")
    except Exception as exc:
        logger.warning("market_digest_output %s failed: %s", route, type(exc).__name__)
        return _Http("error", "output_unavailable")
    if raw is None:
        return _Http("unconfirmed", "response_too_large")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return _Http("error", "output_unavailable")
    if not isinstance(parsed, dict):
        return _Http("error", "output_unavailable")
    if route == "get" and parsed.get("ok") is False and parsed.get("code") == "output_missing":
        return _Http("missing", "output_missing")
    code = _token(parsed.get("code")) or "output_unavailable"
    if status_code < 200 or status_code >= 300 or parsed.get("ok") is not True:
        return _Http("error", code, parsed)
    return _Http("ok", code, parsed)


def _parse_status(raw: object) -> _Report:
    empty = _Report(
        ok=False,
        found=None,
        status=None,
        delivery_confirmed=False,
        content_kind=None,
        content_sha256=None,
        report_run_id=None,
        profile=None,
        code="report_unavailable",
        failure=False,
        malformed=True,
    )
    if not isinstance(raw, str):
        return empty
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return empty
    if not isinstance(payload, dict):
        return empty
    if payload.get("ok") is not True:
        return _Report(
            ok=False,
            found=None,
            status=None,
            delivery_confirmed=False,
            content_kind=None,
            content_sha256=None,
            report_run_id=None,
            profile=None,
            code=_token(payload.get("code")) or "report_unavailable",
            failure=False,
            malformed=False,
        )
    found = payload.get("found")
    if found is not True and found is not False:
        return empty
    status = payload.get("status")
    if status is not None and _token(status) is None:
        return empty
    kind = payload.get("content_kind")
    if kind is not None and kind not in {"cards", "failure_text"}:
        return empty
    digest = payload.get("content_sha256")
    if digest is not None and (not isinstance(digest, str) or _HEX64_RE.fullmatch(digest) is None):
        digest = None
    run_id = payload.get("report_run_id")
    if run_id is not None and not isinstance(run_id, str):
        run_id = None
    profile = payload.get("profile")
    if profile is not None and not isinstance(profile, str):
        profile = None
    failure_text = payload.get("failure_text")
    failure = kind == "failure_text" or (isinstance(failure_text, str) and bool(failure_text.strip()))
    return _Report(
        ok=True,
        found=found,
        status=status if isinstance(status, str) else None,
        delivery_confirmed=payload.get("delivery_confirmed") is True,
        content_kind=kind if isinstance(kind, str) else None,
        content_sha256=digest if isinstance(digest, str) else None,
        report_run_id=run_id if isinstance(run_id, str) else None,
        profile=profile if isinstance(profile, str) else None,
        code=None,
        failure=failure,
        malformed=False,
    )


def _matches(report: _Report, saved: _Saved, expected_hash: str, profile: str) -> bool:
    if report.failure or report.content_kind != "cards":
        return False
    if report.content_sha256 != expected_hash:
        return False
    if report.report_run_id != saved.report_run_id:
        return False
    if report.profile not in (None, profile):
        return False
    return True


def _confirmed(report: _Report) -> bool:
    return (
        report.ok
        and report.found is True
        and report.status == "sent"
        and report.delivery_confirmed
        and not report.failure
        and not report.malformed
    )


def _progress(
    action: str,
    *,
    stage: str,
    saved: _Saved,
    profile: str,
    report: _Report | None,
    expected_hash: str | None,
    delivery: bool,
    ok: bool = True,
    code: str | None = None,
) -> str:
    archive_done = _host_done(saved)
    chosen = "done" if ok and delivery and archive_done else stage
    return _dump(
        action=action,
        ok=ok,
        code=code or chosen,
        stage=chosen,
        found=True,
        period=saved.period,
        profile=profile,
        saved=saved,
        report=report,
        delivery=delivery,
        finalized=bool(ok and archive_done),
        content_sha256=expected_hash,
    )


async def _call_report(tool: BaseTool, **kwargs: Any) -> object:
    try:
        return await tool.coroutine(**kwargs)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("market_digest_output report failed: %s", type(exc).__name__)
        return None


async def _sync_report(
    action: str,
    config: RunnableConfig,
    saved: _Saved,
    profile: str,
    expected_hash: str,
    card: dict[str, Any],
) -> tuple[bool, _Report | None, str | None]:
    """Return ``(may_finalize, report, json_to_return)``."""
    try:
        report_tool = create_automation_report_tools()[0]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("market_digest_output report tool failed: %s", type(exc).__name__)
        return False, None, _progress(
            action,
            stage="unavailable",
            saved=saved,
            profile=profile,
            report=None,
            expected_hash=expected_hash,
            delivery=False,
            ok=False,
            code="report_unavailable",
        )
    current = _parse_status(
        await _call_report(
            report_tool,
            action="status",
            report_run_id=saved.report_run_id,
            config=config,
        )
    )
    if current.malformed or not current.ok:
        return False, current, _progress(
            action,
            stage="unavailable",
            saved=saved,
            profile=profile,
            report=current,
            expected_hash=expected_hash,
            delivery=False,
            ok=False,
            code=current.code or "report_unavailable",
        )
    should_submit = False
    if current.found is True:
        if not _matches(current, saved, expected_hash, profile):
            return False, current, _progress(
                action,
                stage="report_conflict",
                saved=saved,
                profile=profile,
                report=current,
                expected_hash=expected_hash,
                delivery=False,
                ok=False,
                code="report_conflict",
            )
        if _confirmed(current):
            if action == "status":
                return False, current, _progress(
                    action,
                    stage="archive_pending",
                    saved=saved,
                    profile=profile,
                    report=current,
                    expected_hash=expected_hash,
                    delivery=True,
                )
            return True, current, None
        if action == "submit" and current.status in _RECOVER_STATUSES:
            should_submit = True
        else:
            return False, current, _progress(
                action,
                stage="report_unconfirmed",
                saved=saved,
                profile=profile,
                report=current,
                expected_hash=expected_hash,
                delivery=False,
            )
    elif action != "submit":
        return False, current, _progress(
            action,
            stage="report_absent",
            saved=saved,
            profile=profile,
            report=current,
            expected_hash=expected_hash,
            delivery=False,
        )
    else:
        should_submit = True
    if not should_submit:
        return False, current, _progress(
            action,
            stage="report_unconfirmed",
            saved=saved,
            profile=profile,
            report=current,
            expected_hash=expected_hash,
            delivery=False,
        )
    submitted = await _call_report(
        report_tool,
        action="submit",
        report_run_id=saved.report_run_id,
        material="yes",
        cards=[card],
        config=config,
    )
    accepted = _parse_status(submitted) if isinstance(submitted, str) else _parse_status(None)
    submit_code = None
    submit_ok = False
    if isinstance(submitted, str):
        try:
            body = json.loads(submitted)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and body.get("ok") is True and body.get("quiet") is not True:
            submit_ok = True
        elif isinstance(body, dict):
            submit_code = _token(body.get("code")) or "report_unavailable"
        else:
            submit_code = "report_unavailable"
    else:
        submit_code = "report_unavailable"
    if not submit_ok:
        stage = "report_conflict" if submit_code == "content_conflict" else "unavailable"
        return False, accepted, _progress(
            action,
            stage=stage,
            saved=saved,
            profile=profile,
            report=current,
            expected_hash=expected_hash,
            delivery=False,
            ok=False,
            code="report_conflict" if submit_code == "content_conflict" else (submit_code or "report_unavailable"),
        )
    refreshed = _parse_status(
        await _call_report(
            report_tool,
            action="status",
            report_run_id=saved.report_run_id,
            config=config,
        )
    )
    if refreshed.malformed or not refreshed.ok or refreshed.found is not True:
        return False, refreshed, _progress(
            action,
            stage="report_unconfirmed",
            saved=saved,
            profile=profile,
            report=refreshed if refreshed.found is not None else current,
            expected_hash=expected_hash,
            delivery=False,
        )
    if not _matches(refreshed, saved, expected_hash, profile):
        return False, refreshed, _progress(
            action,
            stage="report_conflict",
            saved=saved,
            profile=profile,
            report=refreshed,
            expected_hash=expected_hash,
            delivery=False,
            ok=False,
            code="report_conflict",
        )
    if _confirmed(refreshed):
        return True, refreshed, None
    return False, refreshed, _progress(
        action,
        stage="report_unconfirmed",
        saved=saved,
        profile=profile,
        report=refreshed,
        expected_hash=expected_hash,
        delivery=False,
    )


def _finalize_success(payload: Mapping[str, Any], saved: _Saved) -> int | None:
    if payload.get("ok") is not True or payload.get("code") != "finalized":
        return None
    if payload.get("archive_status") != "done":
        return None
    if payload.get("embedding_status") != "ready":
        return None
    if payload.get("index_status") != "done":
        return None
    if payload.get("body_sha256") != saved.body_sha256:
        return None
    if payload.get("report_run_id") != saved.report_run_id:
        return None
    if payload.get("period") != saved.period or payload.get("date") != saved.date:
        return None
    archive = payload.get("archive")
    embedding = payload.get("embedding")
    index = payload.get("index")
    if not isinstance(archive, Mapping) or archive.get("status") not in {"written", "reused"}:
        return None
    if not isinstance(embedding, Mapping) or embedding.get("status") not in {
        "embedded",
        "cached",
        "skipped",
    }:
        return None
    if _token(embedding.get("code")) is None:
        return None
    if not isinstance(index, Mapping) or index.get("status") not in {"committed", "reused"}:
        return None
    if _token(index.get("code")) is None:
        return None
    chunk = _nonneg(index.get("chunk_count"))
    meta_chunk = _nonneg(payload.get("chunk_count"))
    if chunk is None or meta_chunk is None or chunk != meta_chunk:
        return None
    return chunk


async def _load_saved(
    action: str,
    period: str,
    markdown: str | None,
    execution_id: str,
    profile: str,
) -> _Saved | str:
    got = await _post(
        "get",
        _request_payload("get", period=period, execution_id=execution_id),
        timeout_s=_GET_TIMEOUT_S,
        max_response_bytes=_MAX_GET_BYTES,
    )
    if got.kind == "missing":
        if action != "submit" or markdown is None:
            return _dump(
                action=action,
                ok=False,
                code="output_missing",
                stage="output_missing",
                found=False,
                period=period,
                profile=profile,
            )
        return await _save_then_get(action, period, markdown, execution_id, profile)
    if got.kind == "unconfirmed":
        code = "response_too_large" if got.code == "response_too_large" else "output_unconfirmed"
        return _dump(
            action=action,
            ok=False,
            code=code,
            stage="output_unconfirmed",
            found=None,
            period=period,
            profile=profile,
        )
    if got.kind != "ok" or got.payload is None:
        return _dump(
            action=action,
            ok=False,
            code=got.code,
            stage="unavailable",
            found=None,
            period=period,
            profile=profile,
        )
    checked = _validate_saved(got.payload, period)
    if isinstance(checked, str):
        return _dump(
            action=action,
            ok=False,
            code=checked,
            stage="unavailable" if checked == "state_corrupt" else checked,
            found=True,
            period=period,
            profile=profile,
        )
    if markdown is not None and markdown.encode("utf-8") != checked.markdown.encode("utf-8"):
        return _dump(
            action=action,
            ok=False,
            code="body_conflict",
            stage="body_conflict",
            found=True,
            period=period,
            profile=profile,
            saved=checked,
        )
    return checked


async def _save_then_get(
    action: str,
    period: str,
    markdown: str,
    execution_id: str,
    profile: str,
) -> _Saved | str:
    saved = await _post(
        "save",
        _request_payload(
            "save",
            period=period,
            execution_id=execution_id,
            markdown=markdown,
        ),
        timeout_s=_SAVE_TIMEOUT_S,
        max_response_bytes=_MAX_GET_BYTES,
    )
    if saved.kind == "unconfirmed":
        code = "response_too_large" if saved.code == "response_too_large" else "save_unconfirmed"
        return _dump(
            action=action,
            ok=False,
            code=code,
            stage="save_unconfirmed",
            found=None,
            period=period,
            profile=profile,
        )
    if saved.kind != "ok" or saved.payload is None or saved.payload.get("code") not in {
        "saved",
        "reused",
    }:
        code = saved.code
        if code in {"body_conflict", "input_conflict"}:
            return _dump(
                action=action,
                ok=False,
                code=code,
                stage=code,
                found=True,
                period=period,
                profile=profile,
            )
        return _dump(
            action=action,
            ok=False,
            code=code,
            stage="unavailable",
            found=None,
            period=period,
            profile=profile,
        )
    got = await _post(
        "get",
        _request_payload("get", period=period, execution_id=execution_id),
        timeout_s=_GET_TIMEOUT_S,
        max_response_bytes=_MAX_GET_BYTES,
    )
    if got.kind != "ok" or got.payload is None:
        code = "output_unconfirmed" if got.kind == "unconfirmed" else got.code
        if got.kind == "unconfirmed" and got.code == "response_too_large":
            code = "response_too_large"
        return _dump(
            action=action,
            ok=False,
            code=code,
            stage="output_unconfirmed" if got.kind == "unconfirmed" else "unavailable",
            found=None,
            period=period,
            profile=profile,
        )
    checked = _validate_saved(got.payload, period)
    if isinstance(checked, str):
        return _dump(
            action=action,
            ok=False,
            code=checked,
            stage="unavailable" if checked == "state_corrupt" else checked,
            found=True,
            period=period,
            profile=profile,
        )
    if markdown.encode("utf-8") != checked.markdown.encode("utf-8"):
        return _dump(
            action=action,
            ok=False,
            code="body_conflict",
            stage="body_conflict",
            found=True,
            period=period,
            profile=profile,
            saved=checked,
        )
    return checked


async def _finish_finalize(
    action: str,
    execution_id: str,
    saved: _Saved,
    profile: str,
    report: _Report,
    expected_hash: str,
) -> str:
    result = await _post(
        "finalize",
        _request_payload(
            "finalize",
            period=saved.period,
            execution_id=execution_id,
            body_sha256=saved.body_sha256,
        ),
        timeout_s=_FINALIZE_TIMEOUT_S,
        max_response_bytes=_MAX_FINALIZE_BYTES,
    )
    if result.kind == "unconfirmed":
        code = "response_too_large" if result.code == "response_too_large" else "finalize_unconfirmed"
        return _dump(
            action=action,
            ok=True,
            code=code,
            stage="finalize_unconfirmed",
            found=True,
            period=saved.period,
            profile=profile,
            saved=saved,
            report=report,
            delivery=True,
            finalized=False,
            content_sha256=expected_hash,
        )
    if result.kind != "ok" or result.payload is None:
        archive = saved.archive_status
        embedding = saved.embedding_status
        index = saved.index_status
        chunk = saved.chunk_count
        if result.payload is not None:
            archive = _token(result.payload.get("archive_status")) or archive
            embedding = _token(result.payload.get("embedding_status")) or embedding
            index = _token(result.payload.get("index_status")) or index
            if _nonneg(result.payload.get("chunk_count")) is not None:
                chunk = _nonneg(result.payload.get("chunk_count"))
        return _dump(
            action=action,
            ok=False,
            code=result.code if result.code != "timeout" else "finalize_failed",
            stage="finalize_failed",
            found=True,
            period=saved.period,
            profile=profile,
            saved=saved,
            report=report,
            delivery=True,
            finalized=False,
            content_sha256=expected_hash,
            archive_status=archive,
            embedding_status=embedding,
            index_status=index,
            chunk_count=chunk,
        )
    chunk = _finalize_success(result.payload, saved)
    if chunk is None:
        return _dump(
            action=action,
            ok=False,
            code="finalize_failed",
            stage="finalize_failed",
            found=True,
            period=saved.period,
            profile=profile,
            saved=saved,
            report=report,
            delivery=True,
            finalized=False,
            content_sha256=expected_hash,
        )
    return _dump(
        action=action,
        ok=True,
        code="done",
        stage="done",
        found=True,
        period=saved.period,
        profile=profile,
        saved=saved,
        report=report,
        delivery=True,
        finalized=True,
        content_sha256=expected_hash,
        archive_status="done",
        embedding_status="ready",
        index_status="done",
        chunk_count=chunk,
    )


def create_market_digest_output_tools() -> list[BaseTool]:
    """Build ``[market_digest_output]`` for the configured digest owner."""

    @tool("market_digest_output")
    async def market_digest_output(
        action: Literal["submit", "status", "finalize"],
        period: Literal["AM", "PM"],
        markdown: str | None = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Save this server-started market digest and finish its archive, or read that progress.
        Only the configured owner on a running AM or PM market-digest automation can call it. Pass the full Markdown on the first submit only. Omit Markdown to resume the saved body. A SKIP line is not a report. Do not pass a path, recipient, execution id, or sent flag.

        Args:
            action: "submit" saves or resumes delivery and archive. "status" only reads. "finalize" resumes archive after the report is already sent.
            period: "AM" or "PM".
            markdown: Full report whose first line is "# 📊 市场晨报 — DATE" for AM or "# 📊 市场晚报 — DATE" for PM, using the prepared date. Omit it when a body is already saved. status and finalize reject it.

        Returns:
            JSON progress with stage, report_delivery_confirmed, and finalized, and no report body.

        Say DONE only when report_delivery_confirmed and finalized are both true. report_unconfirmed, pending, unknown, and failed are not sent. finalize_unconfirmed is not a failed send; retry finalize with the same period and no Markdown. body_conflict and report_conflict keep the saved copy.
        """
        parsed = _parse_call(action, period, markdown)
        if isinstance(parsed, str):
            return parsed
        chosen, selected, provided = parsed
        identity = _server_identity(config)
        if identity is None:
            return _refuse(chosen, "not_automation_run", period=selected)
        owner = get_market_digest_owner_user_id()
        canonical = _canonical_user_id(identity["user_id"])
        if not owner or not canonical or owner != canonical:
            code = "owner_not_configured" if not owner else "owner_mismatch"
            return _refuse(chosen, code, period=selected)
        try:
            allowed = await is_internal_or_admin(identity["user_id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "market_digest_output tier check failed: %s", type(exc).__name__
            )
            allowed = False
        if not allowed:
            return _refuse(chosen, "not_allowed", period=selected)
        try:
            auth = await authorize_market_digest_execution(
                user_id=identity["user_id"],
                workspace_id=identity["workspace_id"],
                thread_id=identity["thread_id"],
                run_id=identity["run_id"],
                automation_id=identity["turn_origin_automation_id"],
                execution_id=identity["automation_execution_id"],
                period=selected,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("market_digest_output authorize failed: %s", type(exc).__name__)
            return _refuse(chosen, "not_allowed", period=selected)
        if (
            not isinstance(auth, Mapping)
            or auth.get("ok") is not True
            or auth.get("code") != "ok"
        ):
            code = "not_allowed"
            if isinstance(auth, Mapping):
                code = _token(auth.get("code")) or "not_allowed"
            return _refuse(chosen, code, period=selected)
        profile = _PROFILES[selected]
        if auth.get("profile") != profile or auth.get("period") != selected:
            return _refuse(chosen, "profile_mismatch", period=selected)
        loaded = await _load_saved(
            chosen,
            selected,
            provided,
            identity["automation_execution_id"],
            profile,
        )
        if isinstance(loaded, str):
            return loaded
        try:
            card = markdown_to_card_elements(
                markdown=loaded.markdown,
                title=_CARD_TITLE[selected].format(date=loaded.date),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("market_digest_output render failed: %s", type(exc).__name__)
            return _dump(
                action=chosen,
                ok=False,
                code="body_invalid",
                stage="body_invalid",
                found=True,
                period=selected,
                profile=profile,
                saved=loaded,
            )
        if not isinstance(card, dict):
            return _dump(
                action=chosen,
                ok=False,
                code="body_invalid",
                stage="body_invalid",
                found=True,
                period=selected,
                profile=profile,
                saved=loaded,
            )
        try:
            expected_hash = automation_report_content_sha256(
                profile=profile,
                content_kind="cards",
                cards=[card],
                failure_text=None,
            )
        except (TypeError, ValueError):
            return _dump(
                action=chosen,
                ok=False,
                code="body_invalid",
                stage="body_invalid",
                found=True,
                period=selected,
                profile=profile,
                saved=loaded,
            )
        may_finalize, report, stop = await _sync_report(
            chosen,
            config,
            loaded,
            profile,
            expected_hash,
            card,
        )
        if stop is not None or report is None or not may_finalize:
            return stop or _progress(
                chosen,
                stage="report_unconfirmed",
                saved=loaded,
                profile=profile,
                report=report,
                expected_hash=expected_hash,
                delivery=False,
            )
        return await _finish_finalize(
            chosen,
            identity["automation_execution_id"],
            loaded,
            profile,
            report,
            expected_hash,
        )

    return [market_digest_output]


__all__ = ["create_market_digest_output_tools"]
