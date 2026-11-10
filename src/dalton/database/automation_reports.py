"""Immutable storage for configured automation report batches.

A report batch is one original-card payload, or one failure text, for one
``report_run_id`` of one automation. Rows land in ``dalton_notify_deliveries``
only after the running execution matches the server-supplied identity and
every configured recipient for that outcome is still authorized. This module
does not send, claim, or open a gateway call. A later stage reads the saved
rows and delivers them.

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

REPORT_SOURCE = "automation_report"
PAYLOAD_VERSION = 1
REPORT_PROFILES = frozenset(
    {"msft-tracker", "market-digest-am", "market-digest-pm"}
)
MAX_CARDS = 20
MAX_CARD_JSON_BYTES = 262144
MAX_FAILURE_TEXT_CHARS = 2000
MAX_PROFILE_TARGETS = 5
QUIET_MATERIALS = frozenset({"no", "no_change", "no-change"})

_PROFILE_KEYS = frozenset({"profile", "report_targets", "failure_targets"})
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_REPORT_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FEISHU_CHAT_TARGET_RE = re.compile(r"^feishu:chat:oc_[A-Za-z0-9_-]{3,128}$")
_FEISHU_USER_TARGET_RE = re.compile(r"^feishu:user:ou_[A-Za-z0-9_-]{3,128}$")
_DISCORD_USER_TARGET_RE = re.compile(r"^discord:user:[0-9]{5,32}$")
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
    "profile_invalid")`` means the key is present but not one of the three
    configured profiles, or a list, target, or extra key is illegal.
    """
    if not isinstance(metadata, Mapping) or "report_profile" not in metadata:
        return None, None
    raw = metadata.get("report_profile")
    if not isinstance(raw, Mapping):
        return None, "profile_invalid"
    if set(raw) != _PROFILE_KEYS:
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
    if not report_targets and not failure_targets:
        return None, "profile_invalid"
    return {
        "profile": profile_id,
        "report_targets": report_targets,
        "failure_targets": failure_targets,
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


def automation_report_content_sha256(
    *,
    profile: str,
    content_kind: str,
    cards: list[dict[str, Any]] | None,
    failure_text: str | None,
) -> str:
    """SHA-256 of the canonical report body.

    Key order does not change the digest. List order and string text do.
    The unused body is null: cards for a failure text, and the reverse.
    """
    if content_kind == "cards":
        body_cards: Any = cards
        body_text: Any = None
    else:
        body_cards = None
        body_text = failure_text
    canonical = {
        "profile": profile,
        "content_kind": content_kind,
        "cards": body_cards,
        "failure_text": body_text,
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
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate cards xor failure text and return the stored body plus digest.

    Ordinary https and http markdown links are kept. Attachment keys, local
    paths, and recipient keys are refused. Returns ``(None, code)`` on refusal.
    """
    if profile not in REPORT_PROFILES:
        return None, "profile_invalid"
    if failure_text is not None and not isinstance(failure_text, str):
        return None, "invalid_failure_text"
    text = failure_text.strip() if isinstance(failure_text, str) else ""
    has_text = bool(text)
    if cards is None:
        card_list: list[Any] | None = None
    elif not isinstance(cards, list):
        return None, "invalid_card"
    else:
        card_list = cards
    has_cards = bool(card_list)
    if has_cards and has_text:
        return None, "mixed_outcome"
    if card_list is not None and not has_cards and not has_text:
        return None, "invalid_card"
    if not has_cards and not has_text:
        return None, "missing_content"
    if has_text:
        if len(text) > MAX_FAILURE_TEXT_CHARS:
            return None, "content_too_large"
        if _scan_string(text, url_key=False):
            return None, "invalid_failure_text"
        digest = automation_report_content_sha256(
            profile=profile,
            content_kind="failure_text",
            cards=None,
            failure_text=text,
        )
        return {
            "content_kind": "failure_text",
            "cards": None,
            "failure_text": text,
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
    content_supplied = _content_supplied(cards, failure_text)
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
                                "A no-change report cannot include cards or failure text.",
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
                    )
                    if content_err or prepared is None:
                        return _error(
                            action,
                            content_err or "invalid_card",
                            "Report content is not a bounded original card batch or failure text.",
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
    return bool(_DISCORD_USER_TARGET_RE.fullmatch(target))


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


def _content_supplied(cards: Any, failure_text: Any) -> bool:
    if cards is not None:
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

    intended = (
        list(profile["report_targets"])
        if content_kind == "cards"
        else list(profile["failure_targets"])
    )
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
