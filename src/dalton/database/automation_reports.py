"""Immutable storage for configured automation report batches.

A report batch is one original-card payload, one Markdown text body, or one
failure text, for one ``report_run_id`` of one automation. Rows land in
``dalton_notify_deliveries``
only after the running execution matches the server-supplied identity and
every configured recipient for that outcome is still authorized. This module
does not send or open a gateway call. Native plan and claim call the helpers
below to re-read these rows; they do not accept a replacement body.

A saved card batch may also store the failure recipients from that same
profile. After a card delivery is a certain terminal failure, one Discord
failure notice can be inserted for those frozen recipients. That notice is a
separate row and is not part of the original card aggregate. Plan and claim
read the native item ledger before authorizing that notice. A sent card item
clears a stale outer failure and skips an unsent notice. An unresolved native
item does not authorize the notice.

Identity arguments are server-sourced. The caller does not name an owner,
task, execution, thread, run, or recipient inside the report body.

Submit and status lock the automation row before the execution row, the same
table order ``settle_execution`` uses. Preference and tier reads during
authorization reuse that connection.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from src.dalton.notify.channel_transport import UNCERTAIN_NOTIFY_ERROR_CODES
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

REPORT_SOURCE = "automation_report"
FAILURE_ALERT_SOURCE = "automation_report_failure_alert"
PAYLOAD_VERSION = 1
# Certain card failures at the attempt cap may notify the saved failure targets.
# Uncertain native codes stay on reconciliation. Permission and route skips do not.
_NON_TERMINAL_CARD_CODES = frozenset(
    {
        "toggle_off",
        "target_removed",
        "target_revoked",
        "target_not_routed",
        "no_authorized_target",
        "profile_invalid",
        "task_disabled",
        "dalton_disabled",
        "report_recovered",
        "not_started",
    }
)
# Native item statuses that are still open. They are not a recovered card and
# they are not a certain card failure.
_UNRESOLVED_NATIVE_ITEM_STATUSES = frozenset(
    {"in_flight", "unknown", "partial", "gateway_restarted"}
)
_RECONCILE_LOCK_ATTEMPTS = 4
REPORT_PROFILES = frozenset(
    {
        "msft-tracker",
        "meta-tracker",
        "amzn-tracker",
        "googl-tracker",
        "market-digest-am",
        "market-digest-pm",
        "ai-finance-daily",
        "us-weekly-movers",
        "x-accounts-daily",
        "humanoid-robot-weekly",
        "weekly-trend-impact-scan",
        "ct-semi-sentiment",
    }
)
MAX_CARDS = 20
MAX_CARD_JSON_BYTES = 262144
MAX_FAILURE_TEXT_CHARS = 2000
MAX_TEXT_CHARS = 40000
MAX_PROFILE_TARGETS = 5
QUIET_MATERIALS = frozenset({"no", "no_change", "no-change"})

_PROFILE_REQUIRED_KEYS = frozenset({"profile", "report_targets", "failure_targets"})
_PROFILE_OPTIONAL_KEYS = frozenset({"text_targets"})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_REPORT_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FEISHU_CHAT_TARGET_RE = re.compile(r"^feishu:chat:oc_[A-Za-z0-9_-]{3,128}$")
_FEISHU_USER_TARGET_RE = re.compile(r"^feishu:user:ou_[A-Za-z0-9_-]{3,128}$")
_DISCORD_USER_TARGET_RE = re.compile(r"^discord:user:[0-9]{5,32}$")
_DISCORD_CHANNEL_TARGET_RE = re.compile(r"^discord:channel:[0-9]{5,32}$")
_FORBIDDEN_KEYS = frozenset(
    {
        "chat_id",
        "open_id",
        "receive_id",
        "union_id",
        "user_id",
        "email",
        "download_url",
        "file_path",
        "local_path",
        "attachment",
        "attachments",
        "artifact",
        "artifacts",
        "images",
        "path",
    }
)
_URL_KEYS = frozenset(
    {"url", "default_url", "pc_url", "ios_url", "android_url", "href"}
)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
_LOCAL_REF_RE = re.compile(
    r"(?:^|[\s\"'(=])(?:"
    r"file://"
    r"|/(?:Users|home|tmp|var|etc|private|opt)/"
    r"|[A-Za-z]:\\"
    r"|\./"
    r"|\.\./"
    r")",
    re.IGNORECASE,
)

# Lock order matches settle_execution: the automation row, then the execution
# row. A joined FOR UPDATE/FOR SHARE of both tables locks the execution first
# and deadlocks with that settlement. The peek is not a lock.
_PEEK_EXECUTION_AUTOMATION_SQL = """
SELECT automation_id::text AS automation_id
FROM automation_executions
WHERE automation_execution_id = %s::uuid
"""

_LOCK_AUTOMATION_FOR_UPDATE_SQL = """
SELECT automation_id::text AS automation_id,
       user_id,
       workspace_id::text AS workspace_id,
       name,
       metadata,
       delivery_config
FROM automations
WHERE automation_id = %s::uuid
FOR UPDATE
"""

_LOCK_AUTOMATION_FOR_SHARE_SQL = """
SELECT automation_id::text AS automation_id,
       user_id,
       workspace_id::text AS workspace_id,
       name,
       metadata,
       delivery_config
FROM automations
WHERE automation_id = %s::uuid
FOR SHARE
"""

_LOCK_EXECUTION_FOR_UPDATE_SQL = """
SELECT automation_execution_id::text AS automation_execution_id,
       automation_id::text AS automation_id,
       status,
       conversation_thread_id::text AS conversation_thread_id,
       conversation_response_id::text AS conversation_response_id
FROM automation_executions
WHERE automation_execution_id = %s::uuid
  AND automation_id = %s::uuid
FOR UPDATE
"""

_LOCK_EXECUTION_FOR_SHARE_SQL = """
SELECT automation_execution_id::text AS automation_execution_id,
       automation_id::text AS automation_id,
       status,
       conversation_thread_id::text AS conversation_thread_id,
       conversation_response_id::text AS conversation_response_id
FROM automation_executions
WHERE automation_execution_id = %s::uuid
  AND automation_id = %s::uuid
FOR SHARE
"""

# Preference and tier reads join this connection instead of taking another
# pooled one. The router stays installed; a task with no held connection uses
# the original pool function.
_HELD_CONN: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "automation_report_held_conn",
    default=None,
)

_SELECT_REPORT_ROWS = """
SELECT delivery_id::text AS delivery_id,
       execution_id,
       automation_id,
       user_id,
       channel,
       target,
       status,
       error_code,
       attempt_count,
       payload_json
FROM dalton_notify_deliveries
WHERE automation_id = %s
  AND user_id = %s
  AND COALESCE(payload_json->>'source', '') = 'automation_report'
  AND COALESCE(payload_json->>'report_run_id', '') = %s
ORDER BY target ASC, delivery_id ASC
"""


def parse_report_profile(
    metadata: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(profile, None)`` for a strict report profile.

    ``(None, None)`` means ``report_profile`` is absent. ``(None,
    "profile_invalid")`` means the key is present but not one of the
    configured profiles, or a list, target, or extra key is illegal.
    """
    if not isinstance(metadata, Mapping) or "report_profile" not in metadata:
        return None, None
    raw = metadata.get("report_profile")
    if not isinstance(raw, Mapping):
        return None, "profile_invalid"
    keys = set(raw)
    allowed_keys = _PROFILE_REQUIRED_KEYS | _PROFILE_OPTIONAL_KEYS
    if not _PROFILE_REQUIRED_KEYS <= keys or not keys <= allowed_keys:
        return None, "profile_invalid"
    profile_id = raw.get("profile")
    if not isinstance(profile_id, str) or profile_id not in REPORT_PROFILES:
        return None, "profile_invalid"
    report_targets = _clean_target_list(raw.get("report_targets"), outcome="cards")
    failure_targets = _clean_target_list(
        raw.get("failure_targets"), outcome="failure_text"
    )
    if report_targets is None or failure_targets is None:
        return None, "profile_invalid"
    if "text_targets" in raw:
        text_targets = _clean_target_list(raw.get("text_targets"), outcome="text")
        if text_targets is None:
            return None, "profile_invalid"
    else:
        text_targets = None
    # A report needs somewhere to send. failure_targets alone is not enough.
    # text_targets counts only when that key is present.
    if text_targets is None:
        if not report_targets:
            return None, "profile_invalid"
    elif not report_targets and not text_targets:
        return None, "profile_invalid"
    return {
        "profile": profile_id,
        "report_targets": report_targets,
        "failure_targets": failure_targets,
        "text_targets": text_targets,
    }, None


def automation_has_validated_report_profile(automation: Any) -> bool:
    """True only when ``automation["metadata"]`` carries a valid report profile."""
    if not isinstance(automation, Mapping):
        return False
    parsed, err = parse_report_profile(automation.get("metadata"))
    return parsed is not None and err is None


def held_delivery_configured(delivery_config: Any) -> bool:
    """True when ordinary held delivery methods are configured on the task."""
    if not isinstance(delivery_config, Mapping):
        return False
    methods = delivery_config.get("methods")
    if not isinstance(methods, list):
        return False
    return any(isinstance(item, str) and item.strip() for item in methods)


def is_automation_report_payload(payload: Any) -> bool:
    """True when ``payload`` is a saved automation report body."""
    return isinstance(payload, Mapping) and payload.get("source") == REPORT_SOURCE


def is_failure_alert_payload(payload: Any) -> bool:
    """True when ``payload`` is the one failure notice for a saved card batch."""
    return isinstance(payload, Mapping) and payload.get("source") == FAILURE_ALERT_SOURCE


def card_delivery_is_terminal(
    row: Mapping[str, Any],
    *,
    max_attempts: int = 3,
) -> bool:
    """True when a saved card row has exhausted certain retries.

    ``failed`` with ``attempt_count >= max_attempts``, or ``attempts_exhausted``,
    is terminal. Uncertain codes and permission or route skips are not.
    """
    status = str(row.get("status") or "").strip().lower()
    code = str(row.get("error_code") or "").strip().lower()
    if status != "failed" or not code:
        return False
    if code in UNCERTAIN_NOTIFY_ERROR_CODES or code in _NON_TERMINAL_CARD_CODES:
        return False
    if code == "attempts_exhausted":
        return True
    try:
        attempt = int(row.get("attempt_count") or 0)
    except (TypeError, ValueError):
        return False
    return attempt >= max(1, int(max_attempts))


def failure_target_snapshot_of(payload: Mapping[str, Any]) -> list[str] | None:
    """Return the frozen failure recipients, or None when the key was never stored."""
    if "failure_target_snapshot" not in payload:
        return None
    raw = payload.get("failure_target_snapshot")
    if not isinstance(raw, list):
        return None
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return None
        cleaned.append(item.strip())
    return cleaned


def failure_alert_text(
    *,
    profile: str,
    report_run_id: str,
    terminal_rows: Sequence[Mapping[str, Any]],
) -> str:
    """Short notice naming only the card targets that are already terminal.

    The text is fixed when the notice is first inserted. A later card failure
    does not extend it.
    """
    lines = [
        f"profile: {profile}",
        f"report_run_id: {report_run_id}",
        "Failed targets:",
    ]
    ordered = sorted(terminal_rows, key=lambda row: str(row.get("target") or ""))
    for row in ordered:
        target = str(row.get("target") or "").strip()
        code = str(row.get("error_code") or "").strip().lower()
        try:
            attempt = int(row.get("attempt_count") or 0)
        except (TypeError, ValueError):
            attempt = 0
        lines.append(f"- {target}: {code} attempt {attempt}")
    return "\n".join(lines)


def build_failure_alert_payload(
    *,
    report_payload: Mapping[str, Any],
    execution_id: str,
    terminal_rows: Sequence[Mapping[str, Any]],
    failure_targets: Sequence[str],
) -> dict[str, Any]:
    """Build the immutable failure-notice body for one saved card batch.

    The digest covers the notice text only. Cards, credentials, paths, and the
    research body are not stored on this payload.
    """
    profile = str(report_payload.get("profile") or "")
    report_run_id = str(report_payload.get("report_run_id") or "")
    text = failure_alert_text(
        profile=profile,
        report_run_id=report_run_id,
        terminal_rows=terminal_rows,
    )
    digest = automation_report_content_sha256(
        profile=profile,
        content_kind="failure_text",
        cards=None,
        failure_text=text,
    )
    summary: list[dict[str, Any]] = []
    for row in sorted(terminal_rows, key=lambda item: str(item.get("target") or "")):
        try:
            attempt = int(row.get("attempt_count") or 0)
        except (TypeError, ValueError):
            attempt = 0
        summary.append(
            {
                "target": str(row.get("target") or "").strip(),
                "error_code": str(row.get("error_code") or "").strip().lower(),
                "attempt_count": attempt,
            }
        )
    return {
        "source": FAILURE_ALERT_SOURCE,
        "version": PAYLOAD_VERSION,
        "original_execution_id": execution_id,
        "original_report_run_id": report_run_id,
        "original_content_sha256": str(report_payload.get("content_sha256") or ""),
        "owner_user_id": str(report_payload.get("owner_user_id") or ""),
        "automation_id": str(report_payload.get("automation_id") or ""),
        "profile": profile,
        "report_run_id": report_run_id,
        "first_execution_id": execution_id,
        "target_snapshot": list(failure_targets),
        "expected_item_count": 1,
        "content_kind": "failure_text",
        "failure_text": text,
        "content_sha256": digest,
        "failed_card_targets": summary,
    }


def automation_report_content_sha256(
    *,
    profile: str,
    content_kind: str,
    cards: list[dict[str, Any]] | None,
    failure_text: str | None,
    text: Any = None,
) -> str:
    """SHA-256 of the canonical report body.

    Key order does not change the digest. List order and string text do.
    Card and failure digests omit ``text`` so a resubmit of a saved batch
    still matches. A text body stores ``text`` and nulls the other two.
    """
    if content_kind == "text":
        canonical: dict[str, Any] = {
            "profile": profile,
            "content_kind": content_kind,
            "cards": None,
            "failure_text": None,
            "text": text,
        }
    elif content_kind == "cards":
        canonical = {
            "profile": profile,
            "content_kind": content_kind,
            "cards": cards,
            "failure_text": None,
        }
    else:
        canonical = {
            "profile": profile,
            "content_kind": content_kind,
            "cards": None,
            "failure_text": failure_text,
        }
    raw = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def prepare_automation_report_content(
    *,
    profile: str,
    cards: Any,
    failure_text: Any,
    text: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate exactly one of cards, text, or failure text.

    Ordinary https and http markdown links are kept. Attachment keys, local
    paths, and ``IMG:`` markers are refused on cards, text, and failure text.
    ``strip()`` decides whether a text body is empty. The saved text, its
    length, and its digest keep the original characters. Returns ``(None,
    code)`` on refusal.
    """
    if profile not in REPORT_PROFILES:
        return None, "profile_invalid"
    if failure_text is not None and not isinstance(failure_text, str):
        return None, "invalid_failure_text"
    if text is not None and not isinstance(text, str):
        return None, "invalid_text"
    failure = failure_text.strip() if isinstance(failure_text, str) else ""
    raw_text = text if isinstance(text, str) else ""
    has_failure = bool(failure)
    has_body = bool(raw_text.strip())
    if cards is None:
        card_list: list[Any] | None = None
    elif not isinstance(cards, list):
        return None, "invalid_card"
    else:
        card_list = cards
    has_cards = bool(card_list)
    supplied = sum(1 for flag in (has_cards, has_failure, has_body) if flag)
    if supplied > 1:
        return None, "mixed_outcome"
    if card_list is not None and not has_cards and not has_failure and not has_body:
        return None, "invalid_card"
    if supplied == 0:
        return None, "missing_content"
    if has_body:
        if len(raw_text) > MAX_TEXT_CHARS:
            return None, "content_too_large"
        if _scan_string(raw_text, url_key=False):
            return None, "invalid_text"
        digest = automation_report_content_sha256(
            profile=profile,
            content_kind="text",
            cards=None,
            failure_text=None,
            text=raw_text,
        )
        return {
            "content_kind": "text",
            "cards": None,
            "failure_text": None,
            "text": raw_text,
            "content_sha256": digest,
            "expected_item_count": 1,
        }, None
    if has_failure:
        if len(failure) > MAX_FAILURE_TEXT_CHARS:
            return None, "content_too_large"
        if _scan_string(failure, url_key=False):
            return None, "invalid_failure_text"
        digest = automation_report_content_sha256(
            profile=profile,
            content_kind="failure_text",
            cards=None,
            failure_text=failure,
        )
        return {
            "content_kind": "failure_text",
            "cards": None,
            "failure_text": failure,
            "content_sha256": digest,
            "expected_item_count": 1,
        }, None
    assert card_list is not None
    if len(card_list) > MAX_CARDS:
        return None, "invalid_card"
    stored: list[dict[str, Any]] = []
    for item in card_list:
        if not isinstance(item, Mapping):
            return None, "invalid_card"
        copied = deepcopy(dict(item))
        if _validate_card(copied):
            return None, "invalid_card"
        stored.append(copied)
    try:
        encoded = json.dumps(
            stored, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None, "invalid_card"
    if len(encoded) > MAX_CARD_JSON_BYTES:
        return None, "content_too_large"
    digest = automation_report_content_sha256(
        profile=profile,
        content_kind="cards",
        cards=stored,
        failure_text=None,
    )
    return {
        "content_kind": "cards",
        "cards": stored,
        "failure_text": None,
        "content_sha256": digest,
        "expected_item_count": len(stored),
    }, None


def aggregate_report_delivery(
    rows: Sequence[Mapping[str, Any]],
    expected_targets: Sequence[str],
) -> tuple[str, bool]:
    """Return ``(status, delivery_confirmed)`` for one saved batch.

    Confirmed only when every intended target has its own ``sent`` row and
    none of those rows carries an error. ``unknown``, ``partial``, ``failed``,
    ``pending``, and ``skipped`` stay unconfirmed. An uncertain error code
    (including ``partial`` and ``in_flight``) reports ``unknown``.
    """
    expected = [str(target) for target in expected_targets]
    by_target: dict[str, str] = {}
    uncertain = False
    any_failed = False
    any_pending = False
    any_error = False
    for row in rows:
        code = str(row.get("error_code") or "").strip().lower()
        if code:
            any_error = True
        if code in UNCERTAIN_NOTIFY_ERROR_CODES:
            uncertain = True
        status = str(row.get("status") or "").strip().lower()
        if status == "failed":
            any_failed = True
        elif status == "pending":
            any_pending = True
        by_target[str(row.get("target") or "")] = status
    if uncertain:
        return "unknown", False
    covered = bool(expected) and all(by_target.get(target) == "sent" for target in expected)
    same_set = set(by_target) == set(expected) and len(rows) == len(expected)
    if covered and same_set and not any_error:
        return "sent", True
    if any_failed and not any_pending:
        return "failed", False
    return "pending", False


async def submit_automation_report(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
    report_run_id: str,
    material: str,
    cards: list[dict[str, Any]] | None = None,
    failure_text: str | None = None,
    text: str | None = None,
    conn: Any = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None = None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None = None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None = None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None = None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Register one report batch, or return the immutable batch already saved.

    ``material="yes"`` stores every configured recipient for that outcome, or
    stores nothing. ``no``, ``no_change``, and ``no-change`` store nothing.
    The same body submitted by a later running execution of the same task
    returns the first payload and the first target snapshot.
    """
    action = "submit"
    identity = _clean_identity(
        user_id=user_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        automation_id=automation_id,
        execution_id=execution_id,
    )
    early = _early_identity_code(identity)
    if early:
        return _error(action, early, "Server run identity does not match a running automation execution.")
    cleaned_run = str(report_run_id or "").strip()
    if not _REPORT_RUN_ID_RE.fullmatch(cleaned_run):
        return _error(action, "invalid_report_run_id", "report_run_id is not an allowed token.")
    if material == "yes":
        quiet = False
    elif material in QUIET_MATERIALS:
        quiet = True
    else:
        return _error(action, "invalid_material", "material must be yes, no, no_change, or no-change.")
    content_supplied = _content_supplied(cards, failure_text, text)
    try:
        async with get_db_connection(conn) as owned:
            async with owned.transaction():
                async with owned.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                        (identity["automation_id"], cleaned_run),
                    )
                    row = await lock_report_execution(
                        cur, identity["execution_id"], for_update=True
                    )
                    code = _identity_code(row, identity)
                    if code:
                        return _error(action, code, "Server run identity does not match this execution.")
                    assert row is not None
                    parsed, profile_err = parse_report_profile(row.get("metadata"))
                    if profile_err or parsed is None:
                        return _error(action, "profile_invalid", "Automation metadata has no valid report profile.")
                    if quiet:
                        if content_supplied:
                            return _error(
                                action,
                                "quiet_with_content",
                                "A no-change report cannot include cards, text, or failure text.",
                            )
                        return {
                            "ok": True,
                            "action": action,
                            "quiet": True,
                            "report_run_id": cleaned_run,
                            "delivery_confirmed": False,
                            "created": False,
                        }
                    prepared, content_err = prepare_automation_report_content(
                        profile=str(parsed["profile"]),
                        cards=cards,
                        failure_text=failure_text,
                        text=text,
                    )
                    if content_err or prepared is None:
                        return _error(
                            action,
                            content_err or "invalid_card",
                            "Report content is not a bounded original card batch, text body, or failure text.",
                        )
                    existing = await _load_report_rows(
                        cur,
                        automation_id=str(row["automation_id"]),
                        user_id=str(row["user_id"]),
                        report_run_id=cleaned_run,
                        lock=True,
                    )
                    if existing:
                        if not _same_saved_body(existing, prepared["content_sha256"]):
                            return _error(
                                action,
                                "content_conflict",
                                "This report_run_id is already saved with a different body.",
                            )
                        return _success_from_rows(existing, created=False)
                    if await _execution_has_other_report(
                        cur,
                        execution_id=str(row["automation_execution_id"]),
                        report_run_id=cleaned_run,
                    ):
                        return _error(
                            action,
                            "execution_batch_conflict",
                            "This execution already has a different report batch.",
                        )
                    if held_delivery_configured(row.get("delivery_config")):
                        return _error(
                            action,
                            "held_delivery_configured",
                            "This report profile cannot also use held delivery methods.",
                        )
                    targets, auth_code = await _authorize_all_targets(
                        cur,
                        owned,
                        owner_user_id=str(row["user_id"]),
                        automation=_automation_view(row),
                        profile=parsed,
                        content_kind=str(prepared["content_kind"]),
                        resolve_enabled_fn=resolve_enabled_fn,
                        resolve_overrides_fn=resolve_overrides_fn,
                        resolve_targets_fn=resolve_targets_fn,
                        resolve_routes_fn=resolve_routes_fn,
                        is_admin_fn=is_admin_fn,
                    )
                    if auth_code or not targets:
                        return _error(
                            action,
                            auth_code or "no_authorized_target",
                            "Not every configured recipient is currently authorized.",
                        )
                    payload = _build_payload(
                        profile=parsed,
                        prepared=prepared,
                        report_run_id=cleaned_run,
                        execution_id=str(row["automation_execution_id"]),
                        owner_user_id=str(row["user_id"]),
                        automation_id=str(row["automation_id"]),
                        targets=targets,
                    )
                    await _insert_rows(
                        cur,
                        execution_id=str(row["automation_execution_id"]),
                        automation_id=str(row["automation_id"]),
                        user_id=str(row["user_id"]),
                        targets=targets,
                        payload=payload,
                    )
                    stored = await _load_report_rows(
                        cur,
                        automation_id=str(row["automation_id"]),
                        user_id=str(row["user_id"]),
                        report_run_id=cleaned_run,
                        lock=True,
                    )
                    return _success_from_rows(stored, created=True)
    except UniqueViolation:
        return _error(
            action,
            "execution_batch_conflict",
            "This execution already has a delivery row for one of these recipients.",
        )


async def read_automation_report_status(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
    report_run_id: str,
    conn: Any = None,
) -> dict[str, Any]:
    """Read one saved batch for a running execution of the same task and owner.

    The current execution must match the server identity. The batch itself may
    have been saved by an earlier execution. This read does not send or insert.
    """
    action = "status"
    identity = _clean_identity(
        user_id=user_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        automation_id=automation_id,
        execution_id=execution_id,
    )
    early = _early_identity_code(identity)
    if early:
        return _error(action, early, "Server run identity does not match a running automation execution.")
    cleaned_run = str(report_run_id or "").strip()
    if not _REPORT_RUN_ID_RE.fullmatch(cleaned_run):
        return _error(action, "invalid_report_run_id", "report_run_id is not an allowed token.")
    async with get_db_connection(conn) as owned:
        async with owned.transaction():
            async with owned.cursor(row_factory=dict_row) as cur:
                row = await lock_report_execution(
                    cur, identity["execution_id"], for_update=False
                )
                code = _identity_code(row, identity)
                if code:
                    return _error(action, code, "Server run identity does not match this execution.")
                assert row is not None
                existing = await _load_report_rows(
                    cur,
                    automation_id=str(row["automation_id"]),
                    user_id=str(row["user_id"]),
                    report_run_id=cleaned_run,
                    lock=False,
                )
                if not existing:
                    return {
                        "ok": True,
                        "action": action,
                        "found": False,
                        "delivery_confirmed": False,
                        "report_run_id": cleaned_run,
                    }
                result = _success_from_rows(existing, created=False)
                result["action"] = action
                result["found"] = True
                result.pop("created", None)
                return result


def _clean_target_list(raw: Any, *, outcome: str) -> list[str] | None:
    if not isinstance(raw, list) or len(raw) > MAX_PROFILE_TARGETS:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return None
        target = item.strip()
        if target in seen or not _target_allowed(target, outcome=outcome):
            return None
        seen.add(target)
        cleaned.append(target)
    return cleaned


def _target_allowed(target: str, *, outcome: str) -> bool:
    if any(ch.isspace() for ch in target):
        return False
    if outcome == "cards":
        return bool(
            _FEISHU_CHAT_TARGET_RE.fullmatch(target)
            or _FEISHU_USER_TARGET_RE.fullmatch(target)
        )
    if outcome == "text":
        return bool(
            _FEISHU_CHAT_TARGET_RE.fullmatch(target)
            or _FEISHU_USER_TARGET_RE.fullmatch(target)
            or _DISCORD_USER_TARGET_RE.fullmatch(target)
            or _DISCORD_CHANNEL_TARGET_RE.fullmatch(target)
        )
    return bool(_DISCORD_USER_TARGET_RE.fullmatch(target))


def _outcome_targets(profile: Mapping[str, Any], content_kind: str) -> list[str] | None:
    """Recipients for one saved outcome.

    Text uses ``text_targets`` when that key is a list, including an empty
    list. ``None`` means the key was omitted, so text falls back to
    ``report_targets``. Cards stay on ``report_targets``.
    """
    if content_kind == "cards":
        raw = profile.get("report_targets")
    elif content_kind == "failure_text":
        raw = profile.get("failure_targets")
    elif content_kind == "text":
        chosen = profile.get("text_targets")
        raw = profile.get("report_targets") if chosen is None else chosen
    else:
        return None
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        return None
    return list(raw)


def _validate_card(card: Mapping[str, Any]) -> str | None:
    if card.get("schema") != "2.0":
        return "invalid_card"
    body = card.get("body")
    if not isinstance(body, Mapping):
        return "invalid_card"
    elements = body.get("elements")
    if not isinstance(elements, list) or not elements:
        return "invalid_card"
    return _scan_value(card)


def _scan_value(value: Any, *, url_key: bool = False) -> str | None:
    if value is None or isinstance(value, (bool, int, float)):
        return None
    if isinstance(value, str):
        return _scan_string(value, url_key=url_key)
    if isinstance(value, list):
        for item in value:
            found = _scan_value(item)
            if found:
                return found
        return None
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                return "invalid_card"
            found = _scan_value(item, url_key=key.casefold() in _URL_KEYS)
            if found:
                return found
        return None
    return "invalid_card"


def _scan_string(value: str, *, url_key: bool) -> str | None:
    folded = value.casefold()
    if "__img__" in folded or "IMG:" in value:
        return "invalid_card"
    for match in _MD_IMAGE_RE.finditer(value):
        dest = match.group(1).strip()
        if not dest.lower().startswith(("https://", "http://")):
            return "invalid_card"
    if _LOCAL_REF_RE.search(value):
        return "invalid_card"
    if url_key and not value.lower().startswith(("https://", "http://")):
        return "invalid_card"
    return None


def _content_supplied(cards: Any, failure_text: Any, text: Any = None) -> bool:
    if cards is not None:
        return True
    if text is not None:
        if not isinstance(text, str) or text.strip():
            return True
    if failure_text is None:
        return False
    if isinstance(failure_text, str):
        return bool(failure_text.strip())
    return True


def _clean_identity(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
) -> dict[str, str]:
    return {
        "user_id": str(user_id or "").strip(),
        "workspace_id": str(workspace_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "run_id": str(run_id or "").strip(),
        "automation_id": str(automation_id or "").strip(),
        "execution_id": str(execution_id or "").strip(),
    }


def _early_identity_code(identity: Mapping[str, str]) -> str | None:
    if not all(identity.get(key) for key in (
        "user_id",
        "workspace_id",
        "thread_id",
        "run_id",
        "automation_id",
        "execution_id",
    )):
        return "not_automation_run"
    if not _UUID_RE.fullmatch(identity["execution_id"]):
        return "invalid_execution"
    return None


def _identity_code(row: Mapping[str, Any] | None, identity: Mapping[str, str]) -> str | None:
    if row is None:
        return "invalid_execution"
    if str(row.get("user_id") or "").strip() != identity["user_id"]:
        return "owner_mismatch"
    row_workspace = str(row.get("workspace_id") or "").strip().lower()
    if not row_workspace or row_workspace != identity["workspace_id"].lower():
        return "workspace_mismatch"
    row_automation = str(row.get("automation_id") or "").strip().lower()
    if not row_automation or row_automation != identity["automation_id"].lower():
        return "not_automation_run"
    row_thread = str(row.get("conversation_thread_id") or "").strip().lower()
    if not row_thread or row_thread != identity["thread_id"].lower():
        return "thread_mismatch"
    row_run = str(row.get("conversation_response_id") or "").strip().lower()
    if not row_run or row_run != identity["run_id"].lower():
        return "run_mismatch"
    if str(row.get("status") or "").strip().lower() != "running":
        return "execution_not_running"
    return None


def _automation_view(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    return {
        "automation_id": str(row.get("automation_id") or "").strip(),
        "user_id": str(row.get("user_id") or "").strip(),
        "name": str(row.get("name") or "").strip() or "automation",
        "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
    }


def _payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _same_saved_body(rows: Sequence[Mapping[str, Any]], digest: str) -> bool:
    """True when every saved row is this report source and carries ``digest``.

    The digest was computed before insert. Replay compares that stored field
    and does not rebuild it from jsonb, which does not preserve object key order.
    """
    if not rows:
        return False
    for row in rows:
        payload = _payload_dict(row.get("payload_json"))
        if payload.get("source") != REPORT_SOURCE:
            return False
        if payload.get("content_sha256") != digest:
            return False
    return True


def _snapshot_of(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    payload = _payload_dict(rows[0].get("payload_json")) if rows else {}
    raw = payload.get("target_snapshot")
    if isinstance(raw, list) and all(isinstance(item, str) and item for item in raw):
        return list(raw)
    return []


def _success_from_rows(rows: Sequence[Mapping[str, Any]], *, created: bool) -> dict[str, Any]:
    payload = _payload_dict(rows[0].get("payload_json")) if rows else {}
    targets = _snapshot_of(rows)
    status, confirmed = aggregate_report_delivery(rows, targets)
    return {
        "ok": True,
        "action": "submit",
        "quiet": False,
        "report_run_id": str(payload.get("report_run_id") or ""),
        "profile": str(payload.get("profile") or ""),
        "content_kind": str(payload.get("content_kind") or ""),
        "content_sha256": str(payload.get("content_sha256") or ""),
        "execution_id": str(
            payload.get("first_execution_id") or (rows[0].get("execution_id") if rows else "")
        ),
        "delivery_confirmed": confirmed,
        "status": status,
        "targets": targets,
        "payload": payload,
        "created": created,
    }


def _build_payload(
    *,
    profile: Mapping[str, Any],
    prepared: Mapping[str, Any],
    report_run_id: str,
    execution_id: str,
    owner_user_id: str,
    automation_id: str,
    targets: Sequence[str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": REPORT_SOURCE,
        "version": PAYLOAD_VERSION,
        "profile": profile["profile"],
        "report_run_id": report_run_id,
        "content_kind": prepared["content_kind"],
        "content_sha256": prepared["content_sha256"],
        "first_execution_id": execution_id,
        "owner_user_id": owner_user_id,
        "automation_id": automation_id,
        "target_snapshot": list(targets),
        "expected_item_count": prepared["expected_item_count"],
    }
    if prepared["content_kind"] == "cards":
        payload["cards"] = prepared["cards"]
        payload["failure_target_snapshot"] = list(profile["failure_targets"])
    elif prepared["content_kind"] == "text":
        payload["text"] = prepared["text"]
    else:
        payload["failure_text"] = prepared["failure_text"]
    return payload


def _error(action: str, code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "code": code,
        "delivery_confirmed": False,
        "error": message,
    }


async def lock_report_execution(
    cur: Any,
    execution_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    """Lock the task row, then its execution row, and return both.

    ``for_update`` is the submit path (``FOR UPDATE``). Status uses ``FOR SHARE``.
    Both paths peek the execution's ``automation_id`` without a lock, lock that
    ``automations`` row, then lock the ``automation_executions`` row whose
    automation id still matches. Settlement takes ``FOR NO KEY UPDATE`` on the
    task and then ``FOR UPDATE`` on the execution; this function takes the task
    first so the two wait in one order. Returns None when either row is gone
    or the execution no longer belongs to the locked task.
    """
    await cur.execute(_PEEK_EXECUTION_AUTOMATION_SQL, (execution_id,))
    peeked = await cur.fetchone()
    if not isinstance(peeked, Mapping):
        return None
    automation_id = str(peeked.get("automation_id") or "").strip()
    if not automation_id:
        return None
    await cur.execute(
        _LOCK_AUTOMATION_FOR_UPDATE_SQL if for_update else _LOCK_AUTOMATION_FOR_SHARE_SQL,
        (automation_id,),
    )
    automation = await cur.fetchone()
    if not isinstance(automation, Mapping):
        return None
    await cur.execute(
        _LOCK_EXECUTION_FOR_UPDATE_SQL if for_update else _LOCK_EXECUTION_FOR_SHARE_SQL,
        (execution_id, automation_id),
    )
    execution = await cur.fetchone()
    if not isinstance(execution, Mapping):
        return None
    locked_task = str(automation.get("automation_id") or "").strip()
    locked_execution_task = str(execution.get("automation_id") or "").strip()
    if not locked_task or locked_task != locked_execution_task or locked_task != automation_id:
        return None
    return {
        "automation_execution_id": execution.get("automation_execution_id"),
        "automation_id": locked_execution_task,
        "status": execution.get("status"),
        "conversation_thread_id": execution.get("conversation_thread_id"),
        "conversation_response_id": execution.get("conversation_response_id"),
        "user_id": automation.get("user_id"),
        "workspace_id": automation.get("workspace_id"),
        "name": automation.get("name"),
        "metadata": automation.get("metadata"),
        "delivery_config": automation.get("delivery_config"),
    }


async def _load_report_rows(
    cur: Any,
    *,
    automation_id: str,
    user_id: str,
    report_run_id: str,
    lock: bool,
) -> list[dict[str, Any]]:
    sql = _SELECT_REPORT_ROWS
    if lock:
        sql += " FOR UPDATE"
    await cur.execute(sql, (automation_id, user_id, report_run_id))
    fetched = await cur.fetchall()
    if not isinstance(fetched, list):
        return []
    return [dict(row) for row in fetched if isinstance(row, Mapping)]


async def _execution_has_other_report(
    cur: Any,
    *,
    execution_id: str,
    report_run_id: str,
) -> bool:
    await cur.execute(
        """
        SELECT 1
        FROM dalton_notify_deliveries
        WHERE execution_id = %s
          AND COALESCE(payload_json->>'source', '') = 'automation_report'
          AND COALESCE(payload_json->>'report_run_id', '') <> %s
        LIMIT 1
        """,
        (execution_id, report_run_id),
    )
    return await cur.fetchone() is not None


async def _insert_rows(
    cur: Any,
    *,
    execution_id: str,
    automation_id: str,
    user_id: str,
    targets: Sequence[str],
    payload: Mapping[str, Any],
) -> None:
    encoded = json.dumps(dict(payload), ensure_ascii=False)
    for target in targets:
        channel = target.split(":", 1)[0].strip().lower()
        await cur.execute(
            """
            INSERT INTO dalton_notify_deliveries (
                delivery_id, execution_id, automation_id, user_id,
                channel, target, status, attempt_count, payload_json
            )
            VALUES (%s::uuid, %s, %s, %s, %s, %s, 'pending', 0, %s::jsonb)
            """,
            (
                str(uuid4()),
                execution_id,
                automation_id,
                user_id,
                channel,
                target,
                encoded,
            ),
        )


def _install_held_connection_router() -> None:
    """Route ``user.get_db_connection()`` to the connection held in this task."""
    from src.server.database import user as user_db

    current = user_db.get_db_connection
    if getattr(current, "_automation_report_router", False):
        return
    original = current

    @asynccontextmanager
    async def _router(existing: Any = None):
        if existing is not None:
            yield existing
            return
        held = _HELD_CONN.get()
        if held is not None:
            yield held
            return
        async with original() as borrowed:
            yield borrowed

    _router._automation_report_router = True  # type: ignore[attr-defined]
    user_db.get_db_connection = _router


@asynccontextmanager
async def _use_held_connection(conn: Any):
    """Make preference and user reads inside this task use ``conn``."""
    _install_held_connection_router()
    token = _HELD_CONN.set(conn)
    try:
        yield
    finally:
        _HELD_CONN.reset(token)


async def _authorize_all_targets(
    cur: Any,
    conn: Any,
    *,
    owner_user_id: str,
    automation: Mapping[str, Any],
    profile: Mapping[str, Any],
    content_kind: str,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None,
) -> tuple[list[str] | None, str | None]:
    """Require the whole outcome route. A partial route stores no rows."""
    from src.dalton.database.channel_gateway import _verify_group_target_deliverable
    from src.dalton.database.data_task_notifications import _parse_requested_notify_target
    from src.dalton.notify.dispatch import evaluate_user_notify_eligibility

    intended = _outcome_targets(profile, content_kind)
    if not intended:
        return None, "no_authorized_target"
    from src.dalton.database import channel_gateway as channel_gateway_db

    kwargs: dict[str, Any] = {}
    if resolve_enabled_fn is not None:
        kwargs["resolve_enabled_fn"] = resolve_enabled_fn
    if resolve_overrides_fn is not None:
        kwargs["resolve_overrides_fn"] = resolve_overrides_fn
    if resolve_targets_fn is not None:
        kwargs["resolve_targets_fn"] = resolve_targets_fn
    if resolve_routes_fn is not None:
        kwargs["resolve_routes_fn"] = resolve_routes_fn
    if is_admin_fn is not None:
        kwargs["is_admin_fn"] = is_admin_fn
    async with _use_held_connection(conn):
        # One tier lookup on this connection. Passing it keeps the group helper
        # from calling get_user_tier, which would open another pooled connection.
        user_tier = await channel_gateway_db.get_user_tier(owner_user_id)
        allowed, reason, effective = await evaluate_user_notify_eligibility(
            owner_user_id,
            automation,
            **kwargs,
        )
        if not allowed:
            if reason == "target_removed":
                return None, "no_authorized_target"
            return None, reason or "toggle_off"
        effective_set = {str(item).strip() for item in effective}
        if any(target not in effective_set for target in intended):
            return None, "no_authorized_target"
        for target in intended:
            parsed = _parse_requested_notify_target(target)
            if parsed is None:
                return None, "profile_invalid"
            platform, kind, ident = parsed
            if kind == "group":
                ok_group, _group_err, _name = await _verify_group_target_deliverable(
                    cur,
                    user_id=owner_user_id,
                    user_tier=user_tier,
                    platform=platform,
                    chat_address=f"{platform}:{ident}",
                    conn=conn,
                )
                if not ok_group:
                    return None, "target_revoked"
                continue
            await cur.execute(
                """
                SELECT platform_user_id
                FROM dalton_channel_identities
                WHERE user_id = %s AND platform = %s
                """,
                (owner_user_id, platform),
            )
            id_row = await cur.fetchone()
            bound = (
                str(id_row.get("platform_user_id") or "").strip()
                if isinstance(id_row, Mapping)
                else ""
            )
            if not bound or bound != ident:
                return None, "target_revoked"
        return list(intended), None


def native_items_from_report_payload(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return ordered native items copied from a saved report payload.

    Cards stay in stored order and carry no images and no artifact. A text
    report and a failure text are each one text item. The digest is not
    rebuilt from jsonb.
    """
    if not is_automation_report_payload(payload):
        return None, "payload_invalid"
    if not str(payload.get("content_sha256") or "").strip():
        return None, "payload_invalid"
    expected = payload.get("expected_item_count")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        return None, "payload_invalid"
    kind = payload.get("content_kind")
    if kind == "cards":
        cards = payload.get("cards")
        if not isinstance(cards, list) or not cards or expected != len(cards):
            return None, "payload_invalid"
        items: list[dict[str, Any]] = []
        for index, card in enumerate(cards):
            if not isinstance(card, Mapping):
                return None, "payload_invalid"
            items.append(
                {
                    "item_index": index,
                    "kind": "feishu_card",
                    "card": deepcopy(dict(card)),
                    "images": [],
                }
            )
        return items, None
    if kind == "failure_text":
        text = payload.get("failure_text")
        if not isinstance(text, str) or not text or expected != 1:
            return None, "payload_invalid"
        return [{"item_index": 0, "kind": "text", "text": text}], None
    if kind == "text":
        body = payload.get("text")
        if not isinstance(body, str) or not body or expected != 1:
            return None, "payload_invalid"
        return [{"item_index": 0, "kind": "text", "text": body}], None
    return None, "payload_invalid"


def native_items_from_failure_alert_payload(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return the one text item for a saved failure notice.

    The original report converter stays limited to ``automation_report``.
    This notice has no cards.
    """
    if not is_failure_alert_payload(payload):
        return None, "payload_invalid"
    if str(payload.get("content_kind") or "") != "failure_text":
        return None, "payload_invalid"
    if "cards" in payload and payload.get("cards") not in (None,):
        return None, "payload_invalid"
    text = payload.get("failure_text")
    if not isinstance(text, str) or not text.strip():
        return None, "payload_invalid"
    expected = payload.get("expected_item_count")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected != 1:
        return None, "payload_invalid"
    if not str(payload.get("content_sha256") or "").strip():
        return None, "payload_invalid"
    if not str(payload.get("original_execution_id") or "").strip():
        return None, "payload_invalid"
    if not str(payload.get("original_content_sha256") or "").strip():
        return None, "payload_invalid"
    return [{"item_index": 0, "kind": "text", "text": text}], None


def saved_report_target_still_configured(
    automation: Any,
    payload: Mapping[str, Any],
    target: str,
) -> tuple[bool, str]:
    """Return whether ``target`` is still on the task's current report profile.

    A removed or replaced route is not a reason to choose another recipient.
    The saved snapshot is left unchanged.
    """
    cleaned = str(target or "").strip()
    if not is_automation_report_payload(payload) or not cleaned:
        return False, "payload_invalid"
    if not automation_has_validated_report_profile(automation):
        return False, "profile_invalid"
    parsed, err = parse_report_profile(
        automation.get("metadata") if isinstance(automation, Mapping) else None
    )
    if err or parsed is None:
        return False, "profile_invalid"
    if str(parsed.get("profile") or "") != str(payload.get("profile") or ""):
        return False, "profile_invalid"
    snapshot = payload.get("target_snapshot")
    if not isinstance(snapshot, list) or cleaned not in snapshot:
        return False, "target_removed"
    kind = str(payload.get("content_kind") or "")
    intended = _outcome_targets(parsed, kind)
    if intended is None or cleaned not in intended:
        return False, "target_removed"
    return True, ""


def report_status_view(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Saved-row view. Confirmed only when every snapshot target is sent."""
    if not rows:
        return {
            "ok": False,
            "code": "not_found",
            "delivery_confirmed": False,
            "status": "pending",
            "targets": [],
            "results": [],
        }
    payload = _payload_dict(rows[0].get("payload_json"))
    targets = _snapshot_of(rows)
    status, confirmed = aggregate_report_delivery(rows, targets)
    return {
        "ok": True,
        "execution_id": str(
            payload.get("first_execution_id") or rows[0].get("execution_id") or ""
        ),
        "report_run_id": str(payload.get("report_run_id") or ""),
        "profile": str(payload.get("profile") or ""),
        "content_kind": str(payload.get("content_kind") or ""),
        "content_sha256": str(payload.get("content_sha256") or ""),
        "delivery_confirmed": confirmed,
        "status": status,
        "targets": targets,
        "payload": payload,
        "results": [
            {
                "target": str(row.get("target") or ""),
                "status": str(row.get("status") or ""),
                "error_code": row.get("error_code"),
                "attempt_count": row.get("attempt_count"),
            }
            for row in rows
        ],
    }


async def load_execution_report_rows(
    execution_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return saved report rows for one original execution, or an empty list."""
    exec_id = str(execution_id or "").strip()
    if not exec_id:
        return []
    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT delivery_id::text AS delivery_id,
                       execution_id,
                       automation_id,
                       user_id,
                       channel,
                       target,
                       status,
                       error_code,
                       attempt_count,
                       payload_json
                FROM dalton_notify_deliveries
                WHERE lower(execution_id) = lower(%s)
                  AND COALESCE(payload_json->>'source', '') = 'automation_report'
                ORDER BY target ASC, delivery_id ASC
                """,
                (exec_id,),
            )
            fetched = await cur.fetchall()
    if not isinstance(fetched, list):
        return []
    return [dict(row) for row in fetched if isinstance(row, Mapping)]


async def load_execution_failure_alert_rows(
    execution_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return saved failure-notice rows for one execution, or an empty list."""
    exec_id = str(execution_id or "").strip()
    if not exec_id:
        return []
    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT delivery_id::text AS delivery_id,
                       execution_id,
                       automation_id,
                       user_id,
                       channel,
                       target,
                       status,
                       error_code,
                       attempt_count,
                       payload_json
                FROM dalton_notify_deliveries
                WHERE lower(execution_id) = lower(%s)
                  AND COALESCE(payload_json->>'source', '') = 'automation_report_failure_alert'
                ORDER BY target ASC, delivery_id ASC
                """,
                (exec_id,),
            )
            fetched = await cur.fetchall()
    if not isinstance(fetched, list):
        return []
    return [dict(row) for row in fetched if isinstance(row, Mapping)]


class _FailureAlertConflict(Exception):
    """An existing delivery row is not this card batch's failure notice."""


def _alert_result(
    code: str,
    *,
    ok: bool,
    created: bool = False,
    targets: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
        "created": created,
        "targets": list(targets),
    }


def _failure_alert_payload_matches(
    stored: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> bool:
    """True when ``stored`` is the same notice, ignoring later reconcile stamps."""
    if stored.get("source") != FAILURE_ALERT_SOURCE:
        return False
    for key in (
        "original_report_run_id",
        "original_content_sha256",
        "content_sha256",
        "failure_text",
        "content_kind",
        "owner_user_id",
        "profile",
    ):
        if stored.get(key) != expected.get(key):
            return False
    if str(stored.get("original_execution_id") or "").lower() != str(
        expected.get("original_execution_id") or ""
    ).lower():
        return False
    if str(stored.get("first_execution_id") or "").lower() != str(
        expected.get("first_execution_id") or ""
    ).lower():
        return False
    if str(stored.get("automation_id") or "").lower() != str(
        expected.get("automation_id") or ""
    ).lower():
        return False
    return list(stored.get("target_snapshot") or []) == list(
        expected.get("target_snapshot") or []
    )


class _ReconcileLockSetChanged(Exception):
    """The delivery rows changed while their reconcile locks were being taken."""


def _delivery_dicts(fetched: Any) -> list[dict[str, Any]]:
    if not isinstance(fetched, list):
        return []
    return [dict(row) for row in fetched if isinstance(row, Mapping)]


def _reconcile_lock_name(execution_id: str, channel: str, target: str) -> str | None:
    """Return the reconciler's advisory name, or None when the execution is not a UUID.

    This is the same name ``reconcile_single_data_task_delivery_row`` takes before
    it updates the outer row: ``dalton_reconcile:{normalized execution}:{channel}:{target}``.
    """
    from src.dalton.data_jobs.notification_payloads import (
        DataTaskArtifactError,
        normalize_run_uuid,
    )

    try:
        normalized = normalize_run_uuid(execution_id)
    except DataTaskArtifactError:
        return None
    cleaned_channel = str(channel or "").strip().lower()
    cleaned_target = str(target or "").strip()
    if not cleaned_channel or not cleaned_target:
        return None
    return f"dalton_reconcile:{normalized}:{cleaned_channel}:{cleaned_target}"


def _reconcile_lock_names(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    names: set[str] = set()
    for row in rows:
        name = _reconcile_lock_name(
            str(row.get("execution_id") or ""),
            str(row.get("channel") or ""),
            str(row.get("target") or ""),
        )
        if name:
            names.add(name)
    return tuple(sorted(names))


def _reconcile_lock_names_including_failure_targets(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Row locks plus the frozen failure recipients those rows may insert."""
    names = set(_reconcile_lock_names(rows))
    for row in rows:
        snapshot = failure_target_snapshot_of(_payload_dict(row.get("payload_json")))
        if not snapshot:
            continue
        execution_id = str(row.get("execution_id") or "")
        for target in snapshot:
            name = _reconcile_lock_name(
                execution_id,
                target.split(":", 1)[0],
                target,
            )
            if name:
                names.add(name)
    return tuple(sorted(names))


async def _acquire_reconcile_locks(cur: Any, names: Sequence[str]) -> None:
    for name in names:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (name,),
        )


async def _lock_after_reconcile_advisories(
    cur: Any,
    *,
    preview_sql: str,
    locked_sql: str,
    params: tuple[Any, ...],
    lock_names_fn: Callable[[Sequence[Mapping[str, Any]]], tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Take reconcile advisories in sorted order, then lock the outer rows.

    The reconciler takes ``dalton_reconcile:...`` and only then updates
    ``dalton_notify_deliveries``. Callers that already hold the outer row and
    then enter the reconciler deadlock with it. A savepoint releases both locks
    when the row set changes so the retry takes the expanded set first.
    """
    derive = lock_names_fn or _reconcile_lock_names
    for _attempt in range(_RECONCILE_LOCK_ATTEMPTS):
        await cur.execute("SAVEPOINT dalton_reconcile_order")
        try:
            await cur.execute(preview_sql, params)
            preview = _delivery_dicts(await cur.fetchall())
            row_names = _reconcile_lock_names(preview)
            await _acquire_reconcile_locks(
                cur,
                tuple(sorted(set(row_names) | set(derive(preview)))),
            )
            await cur.execute(locked_sql, params)
            locked = _delivery_dicts(await cur.fetchall())
            if _reconcile_lock_names(locked) != row_names:
                raise _ReconcileLockSetChanged()
        except _ReconcileLockSetChanged:
            await cur.execute("ROLLBACK TO SAVEPOINT dalton_reconcile_order")
            await cur.execute("RELEASE SAVEPOINT dalton_reconcile_order")
            continue
        else:
            await cur.execute("RELEASE SAVEPOINT dalton_reconcile_order")
            return locked
    raise _ReconcileLockSetChanged()


_EXECUTION_DELIVERY_SQL = """
SELECT delivery_id::text AS delivery_id,
       execution_id,
       automation_id,
       user_id,
       channel,
       target,
       status,
       error_code,
       attempt_count,
       payload_json
FROM dalton_notify_deliveries
WHERE lower(execution_id) = lower(%s)
ORDER BY target ASC, delivery_id ASC
"""


async def _lock_execution_delivery_rows(
    cur: Any,
    execution_id: str,
) -> list[dict[str, Any]]:
    return await _lock_after_reconcile_advisories(
        cur,
        preview_sql=_EXECUTION_DELIVERY_SQL,
        locked_sql=_EXECUTION_DELIVERY_SQL + " FOR UPDATE",
        params=(execution_id,),
        lock_names_fn=_reconcile_lock_names_including_failure_targets,
    )


def _report_rows_of(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        if is_automation_report_payload(_payload_dict(row.get("payload_json"))):
            selected.append(dict(row))
    return selected


async def _ensure_report_failure_alert_locked(
    cur: Any,
    owned: Any,
    *,
    execution_id: str,
    max_attempts: int,
    reconcile_row_fn: Callable[..., Awaitable[Any]] | None,
) -> dict[str, Any]:
    locked = await lock_report_execution(cur, execution_id, for_update=True)
    if locked is None:
        return _alert_result("not_found", ok=False)
    rows = await _lock_execution_delivery_rows(cur, execution_id)
    report_rows = _report_rows_of(rows)
    if not report_rows:
        return _alert_result("not_found", ok=False)
    kinds = {
        str(_payload_dict(row.get("payload_json")).get("content_kind") or "")
        for row in report_rows
    }
    if kinds != {"cards"}:
        return _alert_result("not_cards", ok=True)
    report_payload = _payload_dict(report_rows[0].get("payload_json"))
    snapshot = failure_target_snapshot_of(report_payload)
    if snapshot is None or any(
        failure_target_snapshot_of(_payload_dict(row.get("payload_json"))) != snapshot
        for row in report_rows
    ):
        return _alert_result("failure_snapshot_missing", ok=True)
    if not snapshot:
        return _alert_result("no_failure_targets", ok=True)

    reconciler = reconcile_row_fn
    if reconciler is None:
        from src.dalton.database.data_task_notifications import (
            reconcile_single_data_task_delivery_row,
        )

        reconciler = reconcile_single_data_task_delivery_row
    for row in report_rows:
        await reconciler(
            row,
            conn=owned,
            allow_retry_candidate_statuses=True,
        )

    rows = await _lock_execution_delivery_rows(cur, execution_id)
    report_rows = _report_rows_of(rows)
    if not report_rows:
        return _alert_result("not_found", ok=False)
    terminal = [
        row
        for row in report_rows
        if card_delivery_is_terminal(row, max_attempts=max_attempts)
    ]
    if not terminal:
        return _alert_result("not_terminal", ok=True, targets=snapshot)
    digests = {
        str(_payload_dict(row.get("payload_json")).get("content_sha256") or "")
        for row in report_rows
    }
    run_ids = {
        str(_payload_dict(row.get("payload_json")).get("report_run_id") or "")
        for row in report_rows
    }
    if len(digests) != 1 or len(run_ids) != 1 or "" in digests or "" in run_ids:
        raise _FailureAlertConflict()
    report_payload = _payload_dict(report_rows[0].get("payload_json"))
    stored_execution = str(report_rows[0].get("execution_id") or "").strip()
    if (
        not stored_execution
        or str(report_payload.get("first_execution_id") or "").lower()
        != stored_execution.lower()
        or str(report_payload.get("owner_user_id") or "")
        != str(locked.get("user_id") or "")
        or str(report_payload.get("automation_id") or "").lower()
        != str(locked.get("automation_id") or "").lower()
    ):
        raise _FailureAlertConflict()
    expected = build_failure_alert_payload(
        report_payload=report_payload,
        execution_id=stored_execution,
        terminal_rows=terminal,
        failure_targets=snapshot,
    )
    by_target = {str(row.get("target") or "").strip(): row for row in rows}
    for target in snapshot:
        existing = by_target.get(target)
        if existing is None:
            continue
        if not _failure_alert_payload_matches(
            _payload_dict(existing.get("payload_json")),
            expected,
        ):
            raise _FailureAlertConflict()
    encoded = json.dumps(expected, ensure_ascii=False)
    created = False
    owner_user_id = str(report_rows[0].get("user_id") or "")
    automation_id = str(report_rows[0].get("automation_id") or "")
    for target in snapshot:
        if target in by_target:
            continue
        channel = target.split(":", 1)[0].strip().lower()
        await cur.execute(
            """
            INSERT INTO dalton_notify_deliveries (
                delivery_id, execution_id, automation_id, user_id,
                channel, target, status, attempt_count, payload_json
            )
            VALUES (%s::uuid, %s, %s, %s, %s, %s, 'pending', 0, %s::jsonb)
            ON CONFLICT (execution_id, channel, target) DO NOTHING
            RETURNING delivery_id::text AS delivery_id
            """,
            (
                str(uuid4()),
                stored_execution,
                automation_id,
                owner_user_id,
                channel,
                target,
                encoded,
            ),
        )
        if await cur.fetchone() is not None:
            created = True
    refreshed = await _lock_execution_delivery_rows(cur, execution_id)
    for target in snapshot:
        matched = [
            row
            for row in refreshed
            if str(row.get("target") or "").strip() == target
        ]
        if len(matched) != 1 or not _failure_alert_payload_matches(
            _payload_dict(matched[0].get("payload_json")),
            expected,
        ):
            raise _FailureAlertConflict()
    return _alert_result(
        "created" if created else "exists",
        ok=True,
        created=created,
        targets=snapshot,
    )


async def ensure_report_failure_alert(
    execution_id: str,
    *,
    max_attempts: int = 3,
    conn: Any = None,
    reconcile_row_fn: Callable[..., Awaitable[Any]] | None = None,
) -> dict[str, Any]:
    """Insert one failure notice after a saved card batch is terminally failed.

    Locks the task, then the execution, then the reconcile advisories for the
    saved rows and frozen failure recipients, and only then the delivery rows.
    Re-reads the card rows after reconciliation and inserts only the frozen
    failure recipients. A different saved body on the same recipient rolls the
    insert back. Uncertain card rows and permission skips do not qualify.
    """
    exec_id = str(execution_id or "").strip()
    if not exec_id:
        return _alert_result("not_found", ok=False)
    try:
        async with get_db_connection(conn) as owned:
            async with owned.transaction():
                async with owned.cursor(row_factory=dict_row) as cur:
                    return await _ensure_report_failure_alert_locked(
                        cur,
                        owned,
                        execution_id=exec_id,
                        max_attempts=max(1, int(max_attempts)),
                        reconcile_row_fn=reconcile_row_fn,
                    )
    except (_FailureAlertConflict, _ReconcileLockSetChanged):
        logger.warning(
            "Report failure alert conflict for execution_id=%s",
            exec_id,
        )
        return _alert_result("failure_alert_conflict", ok=False)


async def failure_alert_still_required(
    execution_id: str,
    *,
    max_attempts: int = 3,
    conn: Any = None,
    reconcile_row_fn: Callable[..., Awaitable[Any]] | None = None,
    mark_fn: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Reconcile the original cards and decide whether the notice may be sent.

    A late native confirmation that leaves no terminal card target marks an
    unsent notice ``skipped`` / ``report_recovered``. A notice that already
    left pending or failed keeps its real status. Some remaining terminal
    targets still allow the frozen text to send.
    """
    exec_id = str(execution_id or "").strip()
    if not exec_id:
        return False
    reconciler = reconcile_row_fn
    if reconciler is None:
        from src.dalton.database.data_task_notifications import (
            reconcile_single_data_task_delivery_row,
        )

        reconciler = reconcile_single_data_task_delivery_row
    rows = await load_execution_report_rows(exec_id, conn=conn)
    for row in rows:
        await reconciler(
            row,
            conn=conn,
            allow_retry_candidate_statuses=True,
        )
    rows = await load_execution_report_rows(exec_id, conn=conn)
    bounded = max(1, int(max_attempts))
    terminal = [
        row
        for row in rows
        if str(_payload_dict(row.get("payload_json")).get("content_kind") or "")
        == "cards"
        and card_delivery_is_terminal(row, max_attempts=bounded)
    ]
    if terminal:
        return True
    alerts = await load_execution_failure_alert_rows(exec_id, conn=conn)
    if not alerts:
        return False
    if mark_fn is None:
        from src.dalton.notify.dispatch import mark_target_delivery

        marker = mark_target_delivery
    else:
        marker = mark_fn
    for row in alerts:
        status = str(row.get("status") or "").strip().lower()
        if status not in {"pending", "failed"}:
            continue
        channel = str(row.get("channel") or "").strip().lower()
        target = str(row.get("target") or "").strip()
        row_execution = str(row.get("execution_id") or exec_id).strip()
        if not channel or not target or not row_execution:
            continue
        await marker(
            row_execution,
            channel,
            target,
            status="skipped",
            error_code="report_recovered",
            expected_status=("pending", "failed"),
        )
    return False


async def read_execution_report_confirmed(
    execution_id: str,
    *,
    conn: Any = None,
) -> bool:
    """True only when this execution's saved report is fully sent.

    A missing batch, including a quiet no-change report that stored nothing,
    is not sent. Rows from another execution are not read.
    """
    rows = await load_execution_report_rows(execution_id, conn=conn)
    if not rows:
        return False
    return bool(report_status_view(rows).get("delivery_confirmed"))


async def lock_saved_report_automation(cur: Any, execution_id: str) -> bool:
    """Share-lock the task row when this execution saved a report.

    Callers take this lock before the outer-identity advisory lock. The
    execution row is not locked: a settled run can still be delivered.
    Returns False when the execution has no saved report or failure notice.
    """
    exec_id = str(execution_id or "").strip()
    if not exec_id:
        return False
    await cur.execute(
        """
        SELECT DISTINCT automation_id
        FROM dalton_notify_deliveries
        WHERE lower(execution_id) = lower(%s)
          AND COALESCE(payload_json->>'source', '') IN (
              'automation_report',
              'automation_report_failure_alert'
          )
        """,
        (exec_id,),
    )
    fetched = await cur.fetchall()
    if not isinstance(fetched, list):
        return False
    automation_ids = sorted(
        {
            str(row.get("automation_id") or "").strip()
            for row in fetched
            if isinstance(row, Mapping)
            and _UUID_RE.fullmatch(str(row.get("automation_id") or "").strip())
        }
    )
    for automation_id in automation_ids:
        await cur.execute(_LOCK_AUTOMATION_FOR_SHARE_SQL, (automation_id,))
    return bool(automation_ids)


def _deny_report(
    *,
    status: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "scope": "report",
        "allowed": False,
        "deny_status": status,
        "deny_code": code,
        "deny_message": message,
    }


def _resolver_kwargs(
    *,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if resolve_enabled_fn is not None:
        kwargs["resolve_enabled_fn"] = resolve_enabled_fn
    if resolve_overrides_fn is not None:
        kwargs["resolve_overrides_fn"] = resolve_overrides_fn
    if resolve_targets_fn is not None:
        kwargs["resolve_targets_fn"] = resolve_targets_fn
    if resolve_routes_fn is not None:
        kwargs["resolve_routes_fn"] = resolve_routes_fn
    if is_admin_fn is not None:
        kwargs["is_admin_fn"] = is_admin_fn
    return kwargs


async def _native_card_ledger_state(cur: Any, row: Mapping[str, Any]) -> str:
    """Return ``sent``, ``unresolved``, or ``open`` from the native item ledger.

    ``open`` means the ledger neither confirms every item sent nor shows an
    unresolved item. The caller then keeps the outer row's own terminal state.
    Identity resolution is the reconciler's, so a stale outer failure cannot
    hide a native ``sent`` row.
    """
    from src.dalton.data_jobs.notification_payloads import (
        DataTaskArtifactError,
        normalize_run_uuid,
    )
    from src.dalton.database.data_task_notifications import (
        _derive_expected_item_count_from_persisted_payload,
        _parse_requested_notify_target,
        _resolve_reconcile_recipient_identity,
    )

    payload = _payload_dict(row.get("payload_json"))
    execution_id = str(row.get("execution_id") or "").strip()
    owner_user_id = str(row.get("user_id") or "").strip()
    channel = str(row.get("channel") or "").strip().lower()
    target = str(row.get("target") or "").strip()
    if not (execution_id and owner_user_id and channel and target):
        return "open"
    try:
        norm_run_id = normalize_run_uuid(execution_id)
    except DataTaskArtifactError:
        return "open"
    parsed = _parse_requested_notify_target(target)
    if parsed is None:
        return "open"
    platform, target_kind, raw_ident = parsed
    if platform != channel:
        return "open"
    expected_count = _derive_expected_item_count_from_persisted_payload(payload)
    if expected_count is None or expected_count <= 0:
        return "open"
    resolved = await _resolve_reconcile_recipient_identity(
        cur,
        norm_run_id=norm_run_id,
        owner_user_id=owner_user_id,
        platform=platform,
        target_kind=target_kind,
        raw_ident=raw_ident,
        expected_count=expected_count,
        payload=payload,
    )
    if resolved is None:
        return "open"
    canonical_target_address, _recipient_id, expected_keys = resolved
    await cur.execute(
        """
        SELECT delivery_key, status, error_code
        FROM dalton_channel_deliveries
        WHERE delivery_key = ANY(%s)
          AND kind = 'data_task'
          AND run_id = %s
          AND user_id = %s
          AND platform = %s
          AND target_address = %s
        """,
        (
            expected_keys,
            norm_run_id,
            owner_user_id,
            platform,
            canonical_target_address,
        ),
    )
    fetched = await cur.fetchall()
    by_key = {
        str(item.get("delivery_key") or ""): item
        for item in fetched
        if isinstance(item, Mapping)
    }
    saw_unresolved = False
    for key in expected_keys:
        item = by_key.get(key)
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "").strip().lower()
        code = str(item.get("error_code") or "").strip().lower()
        if status in _UNRESOLVED_NATIVE_ITEM_STATUSES or (
            status != "sent" and code in UNCERTAIN_NOTIFY_ERROR_CODES
        ):
            saw_unresolved = True
    if saw_unresolved:
        return "unresolved"
    if len(by_key) == expected_count and all(
        str((by_key.get(key) or {}).get("status") or "").strip().lower() == "sent"
        for key in expected_keys
    ):
        return "sent"
    return "open"


async def _failure_alert_parent_gate(
    cur: Any,
    report_rows: Sequence[Mapping[str, Any]],
    *,
    max_attempts: int,
) -> str:
    """Return ``send``, ``recovered``, or ``unresolved`` for a failure notice.

    A native ``sent`` item clears that card target even when its outer row is
    still a stale failure. Any unresolved native item blocks the notice. The
    outer row is the terminal source only when the ledger is silent.
    """
    saw_terminal = False
    for row in report_rows:
        payload = _payload_dict(row.get("payload_json"))
        if str(payload.get("content_kind") or "") != "cards":
            return "unresolved"
        ledger = await _native_card_ledger_state(cur, row)
        if ledger == "unresolved":
            return "unresolved"
        if ledger == "sent":
            continue
        if card_delivery_is_terminal(row, max_attempts=max_attempts):
            saw_terminal = True
    if saw_terminal:
        return "send"
    return "recovered"


async def _skip_unsent_failure_alerts(cur: Any, execution_id: str) -> None:
    await cur.execute(
        """
        UPDATE dalton_notify_deliveries
        SET status = 'skipped',
            error_code = 'report_recovered',
            updated_at = NOW()
        WHERE lower(execution_id) = lower(%s)
          AND COALESCE(payload_json->>'source', '') = 'automation_report_failure_alert'
          AND status IN ('pending', 'failed')
        """,
        (execution_id,),
    )


async def _authorize_failure_alert_row(
    cur: Any,
    conn: Any,
    *,
    execution_id: str,
    target: str,
    row: Mapping[str, Any],
    report_rows: Sequence[Mapping[str, Any]],
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None = None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None = None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None = None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None = None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Authorize one saved failure notice without widening the original cards."""
    from src.dalton.database.data_task_notifications import _parse_requested_notify_target
    from src.dalton.notify.dispatch import evaluate_user_notify_eligibility

    payload = _payload_dict(row.get("payload_json"))
    items, item_err = native_items_from_failure_alert_payload(payload)
    if item_err or items is None:
        return _deny_report(
            status="failed",
            code=item_err or "payload_invalid",
            message="Saved failure notice is not one text item.",
        )
    if not report_rows:
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="The original card batch for this failure notice is missing.",
        )
    parent = _payload_dict(report_rows[0].get("payload_json"))
    parent_hash = str(parent.get("content_sha256") or "")
    parent_run = str(parent.get("report_run_id") or "")
    if (
        str(parent.get("content_kind") or "") != "cards"
        or not parent_hash
        or not parent_run
        or any(
            str(_payload_dict(item.get("payload_json")).get("content_kind") or "") != "cards"
            or str(_payload_dict(item.get("payload_json")).get("content_sha256") or "")
            != parent_hash
            or str(_payload_dict(item.get("payload_json")).get("report_run_id") or "")
            != parent_run
            for item in report_rows
        )
    ):
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="The original card batch does not match this failure notice.",
        )
    stored_execution = str(payload.get("first_execution_id") or "").strip()
    original_execution = str(payload.get("original_execution_id") or "").strip()
    row_execution = str(row.get("execution_id") or "").strip()
    parent_execution = str(parent.get("first_execution_id") or "").strip()
    if (
        not _UUID_RE.fullmatch(stored_execution)
        or stored_execution.lower() != row_execution.lower()
        or stored_execution.lower() != execution_id.lower()
        or original_execution.lower() != stored_execution.lower()
        or parent_execution.lower() != stored_execution.lower()
        or str(payload.get("original_content_sha256") or "") != parent_hash
        or str(payload.get("original_report_run_id") or "") != parent_run
    ):
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="Saved failure notice does not match the original card batch.",
        )
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    automation_id = str(payload.get("automation_id") or "").strip()
    if (
        not owner_user_id
        or owner_user_id != str(row.get("user_id") or "").strip()
        or owner_user_id != str(parent.get("owner_user_id") or "").strip()
        or not _UUID_RE.fullmatch(automation_id)
        or automation_id.lower() != str(row.get("automation_id") or "").strip().lower()
        or automation_id.lower() != str(parent.get("automation_id") or "").strip().lower()
    ):
        return _deny_report(
            status="failed",
            code="owner_mismatch",
            message="Saved failure notice owner does not match the original report.",
        )
    frozen_snapshot = failure_target_snapshot_of(parent)
    alert_snapshot = payload.get("target_snapshot")
    if frozen_snapshot is None or not isinstance(alert_snapshot, list):
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="Saved failure recipients are missing.",
        )
    if target not in frozen_snapshot or target not in alert_snapshot:
        return _deny_report(
            status="skipped",
            code="target_removed",
            message="The requested target is not in the frozen failure recipients.",
        )
    await cur.execute(
        """
        SELECT automation_id::text AS automation_id,
               user_id,
               name,
               metadata
        FROM automations
        WHERE automation_id = %s::uuid
        """,
        (automation_id,),
    )
    automation_row = await cur.fetchone()
    if not isinstance(automation_row, Mapping):
        return _deny_report(
            status="skipped",
            code="profile_invalid",
            message="The report task no longer exists.",
        )
    if str(automation_row.get("user_id") or "").strip() != owner_user_id:
        return _deny_report(
            status="failed",
            code="owner_mismatch",
            message="The task owner no longer matches the saved failure notice.",
        )
    automation = _automation_view(automation_row)
    parsed, err = parse_report_profile(automation.get("metadata"))
    if err or parsed is None or str(parsed.get("profile") or "") != str(payload.get("profile") or ""):
        return _deny_report(
            status="skipped",
            code="profile_invalid",
            message="The current report profile does not match this failure notice.",
        )
    live_targets = parsed.get("failure_targets")
    if not isinstance(live_targets, list) or target not in live_targets:
        return _deny_report(
            status="skipped",
            code="target_removed",
            message="The failure recipient is no longer on the current profile.",
        )
    parsed_target = _parse_requested_notify_target(target)
    if parsed_target is None:
        return _deny_report(
            status="failed",
            code="invalid_target",
            message="Saved failure target format is not allowed.",
        )
    platform, target_kind, raw_ident = parsed_target
    if target_kind != "dm":
        return _deny_report(
            status="failed",
            code="invalid_target",
            message="A failure notice is sent only to a bound personal target.",
        )
    canonical_target_address = f"{platform}:@me"
    recipient_id = raw_ident
    frozen_recipient = str(payload.get("reconcile_recipient_id") or "").strip()
    frozen_platform = str(payload.get("reconcile_platform") or "").strip().lower()
    frozen_canon = str(payload.get("reconcile_canonical_target") or "").strip()
    if frozen_recipient and (
        frozen_recipient != recipient_id
        or (frozen_platform and frozen_platform != platform)
        or (frozen_canon and frozen_canon != canonical_target_address)
    ):
        return _deny_report(
            status="failed",
            code="recipient_identity_conflict",
            message="Frozen failure recipient does not match the saved target.",
        )
    async with _use_held_connection(conn):
        allowed, reason, _effective = await evaluate_user_notify_eligibility(
            owner_user_id,
            automation,
            target=target,
            **_resolver_kwargs(
                resolve_enabled_fn=resolve_enabled_fn,
                resolve_overrides_fn=resolve_overrides_fn,
                resolve_targets_fn=resolve_targets_fn,
                resolve_routes_fn=resolve_routes_fn,
                is_admin_fn=is_admin_fn,
            ),
        )
    if not allowed:
        return _deny_report(
            status="skipped",
            code=reason or "toggle_off",
            message="Current notification settings do not allow this failure recipient.",
        )
    await cur.execute(
        """
        SELECT platform_user_id
        FROM dalton_channel_identities
        WHERE user_id = %s AND platform = %s
        """,
        (owner_user_id, platform),
    )
    id_row = await cur.fetchone()
    bound = (
        str(id_row.get("platform_user_id") or "").strip()
        if isinstance(id_row, Mapping)
        else ""
    )
    if not bound or bound != raw_ident:
        return _deny_report(
            status="skipped",
            code="target_revoked",
            message="The failure recipient is no longer bound to the owner.",
        )
    # Plan and claim do not receive the worker attempt cap. 3 is the same
    # default ``card_delivery_is_terminal`` and the sender use.
    parent_gate = await _failure_alert_parent_gate(
        cur,
        report_rows,
        max_attempts=3,
    )
    if parent_gate == "unresolved":
        return _deny_report(
            status="pending",
            code="parent_unresolved",
            message="A saved card item is still unresolved in the native ledger.",
        )
    if parent_gate == "recovered":
        await _skip_unsent_failure_alerts(cur, row_execution)
        return _deny_report(
            status="skipped",
            code="report_recovered",
            message="The saved card batch is no longer a certain terminal failure.",
        )
    return {
        "scope": "report",
        "allowed": True,
        "run_id": stored_execution.lower(),
        "job_key": automation_id.lower(),
        "owner_user_id": owner_user_id,
        "owner_platform_user_id": recipient_id,
        "platform": platform,
        "target_kind": target_kind,
        "requested_target": target,
        "canonical_target_address": canonical_target_address,
        "recipient_id": recipient_id,
        "items": items,
        "payload": payload,
    }


async def authorize_saved_report_target(
    cur: Any,
    conn: Any,
    *,
    execution_id: str,
    target: str,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None = None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None = None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None = None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None = None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Authorize one saved report target from immutable rows and live eligibility.

    ``scope="none"`` means this execution has no saved report; the caller keeps
    the data-task path. A report execution never falls through. The recipient
    is the identity frozen in the saved target, not a later account binding.
    """
    from src.dalton.database import channel_gateway as channel_gateway_db
    from src.dalton.database.data_task_notifications import _parse_requested_notify_target
    from src.dalton.notify.dispatch import evaluate_user_notify_eligibility

    exec_id = str(execution_id or "").strip()
    cleaned_target = str(target or "").strip()
    saved_rows_sql = """
        SELECT delivery_id::text AS delivery_id,
               execution_id,
               automation_id,
               user_id,
               channel,
               target,
               status,
               error_code,
               attempt_count,
               payload_json
        FROM dalton_notify_deliveries
        WHERE lower(execution_id) = lower(%s)
          AND COALESCE(payload_json->>'source', '') IN (
              'automation_report',
              'automation_report_failure_alert'
          )
        ORDER BY target ASC, delivery_id ASC
    """
    try:
        rows = await _lock_after_reconcile_advisories(
            cur,
            preview_sql=saved_rows_sql,
            locked_sql=saved_rows_sql + " FOR UPDATE",
            params=(exec_id,),
        )
    except _ReconcileLockSetChanged:
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="Saved report rows changed while locking.",
        )
    if not rows:
        return {"scope": "none"}
    report_rows = _report_rows_of(rows)
    alert_rows = [
        row
        for row in rows
        if is_failure_alert_payload(_payload_dict(row.get("payload_json")))
    ]
    report_match = [
        row for row in report_rows if str(row.get("target") or "").strip() == cleaned_target
    ]
    alert_match = [
        row for row in alert_rows if str(row.get("target") or "").strip() == cleaned_target
    ]
    if len(alert_match) == 1 and not report_match:
        return await _authorize_failure_alert_row(
            cur,
            conn,
            execution_id=exec_id,
            target=cleaned_target,
            row=alert_match[0],
            report_rows=report_rows,
            resolve_enabled_fn=resolve_enabled_fn,
            resolve_overrides_fn=resolve_overrides_fn,
            resolve_targets_fn=resolve_targets_fn,
            resolve_routes_fn=resolve_routes_fn,
            is_admin_fn=is_admin_fn,
        )
    if len(report_match) != 1 or alert_match:
        if report_match or alert_match:
            return _deny_report(
                status="failed",
                code="payload_invalid",
                message="Saved report target and failure notice target overlap.",
            )
        return _deny_report(
            status="skipped",
            code="target_not_routed",
            message="Requested target is not in the saved report batch.",
        )
    row = report_match[0]
    payload = _payload_dict(row.get("payload_json"))
    if not is_automation_report_payload(payload):
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="Saved report payload is missing.",
        )
    stored_execution = str(payload.get("first_execution_id") or "").strip()
    row_execution = str(row.get("execution_id") or "").strip()
    if (
        not _UUID_RE.fullmatch(stored_execution)
        or stored_execution.lower() != row_execution.lower()
        or stored_execution.lower() != exec_id.lower()
    ):
        return _deny_report(
            status="failed",
            code="payload_invalid",
            message="Saved report execution does not match this request.",
        )
    owner_user_id = str(payload.get("owner_user_id") or "").strip()
    automation_id = str(payload.get("automation_id") or "").strip()
    row_owner = str(row.get("user_id") or "").strip()
    row_automation = str(row.get("automation_id") or "").strip()
    if (
        not owner_user_id
        or owner_user_id != row_owner
        or not _UUID_RE.fullmatch(automation_id)
        or automation_id.lower() != row_automation.lower()
    ):
        return _deny_report(
            status="failed",
            code="owner_mismatch",
            message="Saved report owner does not match the delivery row.",
        )
    items, item_err = native_items_from_report_payload(payload)
    if item_err or items is None:
        return _deny_report(
            status="failed",
            code=item_err or "payload_invalid",
            message="Saved report body is not a native card batch, text body, or failure text.",
        )
    await cur.execute(
        """
        SELECT automation_id::text AS automation_id,
               user_id,
               name,
               metadata
        FROM automations
        WHERE automation_id = %s::uuid
        """,
        (automation_id,),
    )
    automation_row = await cur.fetchone()
    if not isinstance(automation_row, Mapping):
        return _deny_report(
            status="skipped",
            code="profile_invalid",
            message="The report task no longer exists.",
        )
    if str(automation_row.get("user_id") or "").strip() != owner_user_id:
        return _deny_report(
            status="failed",
            code="owner_mismatch",
            message="The task owner no longer matches the saved report.",
        )
    automation = _automation_view(automation_row)
    configured, config_code = saved_report_target_still_configured(
        automation, payload, cleaned_target
    )
    if not configured:
        return _deny_report(
            status="skipped",
            code=config_code,
            message="The saved target is no longer on the current report profile.",
        )
    parsed_target = _parse_requested_notify_target(cleaned_target)
    if parsed_target is None:
        return _deny_report(
            status="failed",
            code="invalid_target",
            message="Saved report target format is not allowed.",
        )
    platform, target_kind, raw_ident = parsed_target
    if target_kind == "group":
        canonical_target_address = f"{platform}:{raw_ident}"
        recipient_id = raw_ident
    else:
        canonical_target_address = f"{platform}:@me"
        recipient_id = raw_ident
    frozen_recipient = str(payload.get("reconcile_recipient_id") or "").strip()
    frozen_platform = str(payload.get("reconcile_platform") or "").strip().lower()
    frozen_canon = str(payload.get("reconcile_canonical_target") or "").strip()
    if frozen_recipient and (
        frozen_recipient != recipient_id
        or (frozen_platform and frozen_platform != platform)
        or (frozen_canon and frozen_canon != canonical_target_address)
    ):
        return _deny_report(
            status="failed",
            code="recipient_identity_conflict",
            message="Frozen report recipient does not match the saved target.",
        )
    kwargs: dict[str, Any] = {}
    if resolve_enabled_fn is not None:
        kwargs["resolve_enabled_fn"] = resolve_enabled_fn
    if resolve_overrides_fn is not None:
        kwargs["resolve_overrides_fn"] = resolve_overrides_fn
    if resolve_targets_fn is not None:
        kwargs["resolve_targets_fn"] = resolve_targets_fn
    if resolve_routes_fn is not None:
        kwargs["resolve_routes_fn"] = resolve_routes_fn
    if is_admin_fn is not None:
        kwargs["is_admin_fn"] = is_admin_fn
    async with _use_held_connection(conn):
        user_tier = await channel_gateway_db.get_user_tier(owner_user_id)
        allowed, reason, _effective = await evaluate_user_notify_eligibility(
            owner_user_id,
            automation,
            target=cleaned_target,
            **kwargs,
        )
    if not allowed:
        skip_code = reason or "toggle_off"
        return _deny_report(
            status="skipped",
            code=skip_code,
            message="Current notification settings do not allow this saved target.",
        )
    if target_kind == "group":
        ok_group, group_err, _name = await channel_gateway_db._verify_group_target_deliverable(
            cur,
            user_id=owner_user_id,
            user_tier=user_tier,
            platform=platform,
            chat_address=canonical_target_address,
            conn=conn,
        )
        if not ok_group:
            return _deny_report(
                status="skipped",
                code="target_revoked",
                message=group_err or "Group target is no longer deliverable.",
            )
    else:
        await cur.execute(
            """
            SELECT platform_user_id
            FROM dalton_channel_identities
            WHERE user_id = %s AND platform = %s
            """,
            (owner_user_id, platform),
        )
        id_row = await cur.fetchone()
        bound = (
            str(id_row.get("platform_user_id") or "").strip()
            if isinstance(id_row, Mapping)
            else ""
        )
        if not bound or bound != raw_ident:
            return _deny_report(
                status="skipped",
                code="target_revoked",
                message="The saved personal target is no longer bound to the owner.",
            )
    return {
        "scope": "report",
        "allowed": True,
        "run_id": stored_execution.lower(),
        "job_key": automation_id.lower(),
        "owner_user_id": owner_user_id,
        "owner_platform_user_id": recipient_id if target_kind == "dm" else None,
        "platform": platform,
        "target_kind": target_kind,
        "requested_target": cleaned_target,
        "canonical_target_address": canonical_target_address,
        "recipient_id": recipient_id,
        "items": items,
        "payload": payload,
    }
