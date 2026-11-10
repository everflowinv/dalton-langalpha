"""Authorization, delivery-plan loading, and ``dalton_channel_deliveries`` claims for data tasks.

Authorizes ``(run_id, target)`` strictly from persisted data-task state, owner preferences,
dedicated Dalton application bindings, and current group access, sharing ``dalton_channel_deliveries``
as the single authoritative platform-send ledger without manufacturing conversation or automation rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
import logging
import re
import time
from typing import Any

from psycopg.rows import dict_row

from src.dalton.data_jobs.container_runner import (
    DataTaskPathMap,
    build_path_map,
    read_validated_run_json_metadata,
)
from src.dalton.data_jobs.notification_payloads import (
    DataTaskArtifactError,
    DataTaskArtifactNotFoundError,
    DataTaskNotificationError,
    ResolvedDataTaskPlan,
    ValidatedArtifactBytes,
    build_data_task_delivery_plan,
    normalize_run_uuid,
    read_validated_run_artifact,
)
from src.dalton.data_jobs.runner_wrapper import (
    extract_last_json_object,
    resolve_task_payload_source,
)
from src.dalton.data_jobs.safe_fs import SafeFsError
from src.dalton.data_jobs.scheduler import build_data_task_notify_automation
from src.dalton.database.channel_gateway import _verify_group_target_deliverable
from src.dalton.llm.model_prefs import resolve_dalton_notify_targets
from src.dalton.notify.channel_transport import UNCERTAIN_NOTIFY_ERROR_CODES
from src.dalton.notify.dispatch import evaluate_user_notify_eligibility
from src.dalton.settings import get_configured_pm_user_ids, get_dalton_settings
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

FEISHU_CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9_-]{3,128}$")
FEISHU_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]{3,128}$")
DISCORD_SNOWFLAKE_RE = re.compile(r"^[0-9]{5,32}$")
SUPPORTED_NOTIFY_PLATFORMS: frozenset[str] = frozenset({"feishu", "discord"})


@dataclass(frozen=True)
class AuthorizedDataTaskTarget:
    """Verified run, owner, recipient, and delivery plan inside a DB transaction."""

    allowed: bool
    deny_status: str = "failed"  # "failed" | "skipped"
    deny_code: str = "not_allowed"
    deny_message: str = ""
    run_id: str = ""
    job_key: str = ""
    owner_user_id: str = ""
    owner_platform_user_id: str | None = None
    platform: str = ""
    target_kind: str = ""  # "dm" | "group"
    requested_target: str = ""
    canonical_target_address: str = ""
    recipient_id: str = ""
    plan: ResolvedDataTaskPlan | None = None


def build_data_task_item_delivery_key(
    *,
    run_id: str,
    platform: str,
    recipient_id: str,
    item_index: int,
) -> str:
    """Construct the deterministic ``dalton_channel_deliveries`` key for one ordered report item."""
    norm_run_id = normalize_run_uuid(run_id)
    cleaned_platform = str(platform or "").strip().lower()
    cleaned_recipient = str(recipient_id or "").strip()
    return f"data_task:{norm_run_id}:{cleaned_platform}:{cleaned_recipient}:item:{int(item_index)}"


def _parse_requested_notify_target(raw_target: str) -> tuple[str, str, str] | None:
    """Parse ``raw_target`` into ``(platform, kind, raw_id)`` where ``kind`` is ``'dm'`` or ``'group'``."""
    cleaned = str(raw_target or "").strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return None
    if "://" in cleaned or "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        return None

    parts = cleaned.split(":")
    if len(parts) < 2:
        return None
    platform = parts[0].strip().lower()
    if platform not in SUPPORTED_NOTIFY_PLATFORMS:
        return None

    if len(parts) == 2:
        second = parts[1].strip()
        if second == "@me":
            return (platform, "dm", "@me")
        if platform == "feishu" and FEISHU_CHAT_ID_RE.match(second):
            return ("feishu", "group", second)
        if platform == "discord" and DISCORD_SNOWFLAKE_RE.match(second):
            return ("discord", "group", second)
        return None

    if len(parts) == 3:
        subkind = parts[1].strip().lower()
        ident = parts[2].strip()
        if subkind == "user":
            if platform == "feishu" and FEISHU_OPEN_ID_RE.match(ident):
                return ("feishu", "dm", ident)
            if platform == "discord" and DISCORD_SNOWFLAKE_RE.match(ident):
                return ("discord", "dm", ident)
            return None
        if subkind in ("chat", "channel", "group"):
            if platform == "feishu" and FEISHU_CHAT_ID_RE.match(ident):
                return ("feishu", "group", ident)
            if platform == "discord" and DISCORD_SNOWFLAKE_RE.match(ident):
                return ("discord", "group", ident)
            return None
    return None


def _load_persisted_run_raw_outbox(
    run_id: str,
    job_key: str,
    *,
    db_outbox_payload: Any,
    log_tail: Any,
    path_map: DataTaskPathMap,
) -> dict[str, Any] | None:
    """Resolve persisted run outbox with database precedence over bounded `outbox.json` fallback.

    Valid database `outbox_payload` takes strict precedence and performs zero disk metadata reads.
    When absent, reads only `outbox.json` (never `frozen_manifest.json`) via bounded, non-blocking,
    symlink-rejecting descriptor checks.
    """
    if isinstance(db_outbox_payload, Mapping) and db_outbox_payload:
        return dict(db_outbox_payload)
    if isinstance(db_outbox_payload, str) and db_outbox_payload.strip():
        try:
            parsed = json.loads(db_outbox_payload)
            if isinstance(parsed, Mapping) and parsed:
                return dict(parsed)
            if not isinstance(parsed, Mapping):
                return {"text": db_outbox_payload.strip()}
        except ValueError:
            return {"text": db_outbox_payload.strip()}

    try:
        disk_outbox = read_validated_run_json_metadata(
            run_id,
            "outbox.json",
            path_map=path_map,
        )
    except SafeFsError as exc:
        raise DataTaskArtifactError(str(exc), code=exc.code) from exc

    if isinstance(disk_outbox, Mapping):
        if not disk_outbox:
            raise DataTaskArtifactError(
                "Run outbox.json object is empty",
                code="invalid_metadata",
            )
        return dict(disk_outbox)

    cleaned_key = str(job_key or "").strip()
    if cleaned_key == "quantum-ew-monitor" or resolve_task_payload_source(cleaned_key) == "stdout_text":
        tail_str = str(log_tail or "").strip()
        if tail_str:
            parsed_obj = extract_last_json_object(tail_str)
            if isinstance(parsed_obj, Mapping) and parsed_obj:
                return dict(parsed_obj)
            return {"text": tail_str}

    return None


async def _target_matches_effective_routes(
    *,
    cleaned_target: str,
    platform: str,
    target_kind: str,
    raw_ident: str,
    effective_targets: list[str],
) -> bool:
    if cleaned_target in effective_targets:
        return True
    if target_kind == "group":
        alt_chat = f"{platform}:chat:{raw_ident}"
        alt_short = f"{platform}:{raw_ident}"
        return alt_chat in effective_targets or alt_short in effective_targets
    if target_kind == "dm" and raw_ident == "@me":
        prefix = f"{platform}:user:"
        matched = [t for t in effective_targets if t.startswith(prefix)]
        return len(matched) == 1
    return False


async def _authorize_run_and_target_in_tx(
    cur: Any,
    conn: Any,
    *,
    run_id: str,
    target: str,
    path_map: DataTaskPathMap | None = None,
    require_plan: bool = True,
) -> AuthorizedDataTaskTarget:
    if not get_dalton_settings().enabled:
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="skipped",
            deny_code="dalton_disabled",
            deny_message="Dalton notifications are disabled.",
        )

    try:
        norm_run_id = normalize_run_uuid(run_id)
    except DataTaskArtifactError as exc:
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code=exc.code,
            deny_message=str(exc),
        )

    cleaned_target = str(target or "").strip()
    parsed_target = _parse_requested_notify_target(cleaned_target)
    if parsed_target is None:
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code="invalid_target",
            deny_message="Target format or platform is not allowed for data-task notifications.",
        )
    platform, target_kind, raw_ident = parsed_target

    await cur.execute(
        """
        SELECT r.run_id::text AS run_id,
               r.job_key,
               r.status,
               r.notify_status,
               r.outbox_payload,
               r.log_tail,
               d.job_key AS def_job_key,
               d.enabled AS task_enabled,
               d.notify_enabled AS task_notify_enabled
        FROM dalton_data_task_runs r
        LEFT JOIN dalton_data_task_defs d
          ON d.job_key = r.job_key
        WHERE r.run_id = %s::uuid
        """,
        (norm_run_id,),
    )
    run_row = await cur.fetchone()
    if not run_row:
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code="run_not_found",
            deny_message="Data-task run does not exist.",
        )

    if not run_row.get("def_job_key"):
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code="unknown_task",
            deny_message="Run does not belong to a registered data-task definition.",
        )

    job_key = str(run_row["job_key"]).strip()
    run_status = str(run_row.get("status") or "").strip().lower()
    if run_status != "succeeded":
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code="run_not_succeeded",
            deny_message=f"Data-task run status is {run_status!r}, expected 'succeeded'.",
        )

    notify_status = str(run_row.get("notify_status") or "").strip().lower()
    if notify_status == "preview_only" or notify_status not in ("pending_dispatch", "dispatched"):
        code = "preview_run_forbidden" if notify_status in ("preview_only", "") else f"run_notify_{notify_status}"
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code=code,
            deny_message="Run is a preview/trial execution or is not eligible for live notification dispatch.",
        )

    if not bool(run_row.get("task_enabled")):
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="skipped",
            deny_code="task_disabled",
            deny_message="Data task is currently disabled.",
        )
    if not bool(run_row.get("task_notify_enabled")):
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="skipped",
            deny_code="task_notify_disabled",
            deny_message="Notifications are disabled for this data task.",
        )

    pm_ids = get_configured_pm_user_ids()
    owner_user_id = str(pm_ids[0]).strip() if pm_ids else ""
    if not owner_user_id:
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="failed",
            deny_code="owner_unconfigured",
            deny_message="Data-task owner account is not configured.",
        )

    automation = build_data_task_notify_automation(job_key)
    allowed_prefs, skip_reason, effective_targets = await evaluate_user_notify_eligibility(
        owner_user_id,
        automation,
    )
    if not allowed_prefs:
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="skipped",
            deny_code=skip_reason or "toggle_off",
            deny_message="Current user notification preferences disable delivery for this task.",
        )

    if not await _target_matches_effective_routes(
        cleaned_target=cleaned_target,
        platform=platform,
        target_kind=target_kind,
        raw_ident=raw_ident,
        effective_targets=list(effective_targets or []),
    ):
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_status="skipped",
            deny_code="target_not_routed",
            deny_message="Requested target is not authorized by the task's current notification route.",
        )

    bound_platform_uid: str | None = None
    if target_kind == "dm":
        # Resolve dedicated Dalton app binding for owner_user_id on `platform`
        await cur.execute(
            """
            SELECT platform_user_id
            FROM dalton_channel_identities
            WHERE user_id = %s AND platform = %s
            """,
            (owner_user_id, platform),
        )
        id_row = await cur.fetchone()
        resolved_uid = str(id_row.get("platform_user_id") or "").strip() if id_row else ""
        if not resolved_uid:
            return AuthorizedDataTaskTarget(
                allowed=False,
                deny_status="failed",
                deny_code="not_linked",
                deny_message=f"Owner account has no dedicated Dalton {platform} binding.",
            )
        bound_platform_uid = resolved_uid

        if platform == "feishu":
            if raw_ident != "@me" and raw_ident != bound_platform_uid:
                # Never reuse another user's Dalton open_id or a foreign Feishu app's open_id!
                await cur.execute(
                    """
                    SELECT user_id
                    FROM dalton_channel_identities
                    WHERE platform = 'feishu' AND platform_user_id = %s
                    """,
                    (raw_ident,),
                )
                other_row = await cur.fetchone()
                if other_row and str(other_row.get("user_id") or "").strip() != owner_user_id:
                    return AuthorizedDataTaskTarget(
                        allowed=False,
                        deny_status="failed",
                        deny_code="cross_user_target",
                        deny_message="Target belongs to a different Dalton user.",
                    )
                personal_targets = await resolve_dalton_notify_targets(owner_user_id)
                feishu_personal = [t for t in personal_targets if t.startswith("feishu:user:")]
                if (
                    f"feishu:user:{bound_platform_uid}" in feishu_personal
                    or len(feishu_personal) != 1
                    or feishu_personal[0] != cleaned_target
                ):
                    return AuthorizedDataTaskTarget(
                        allowed=False,
                        deny_status="failed",
                        deny_code="cross_user_target",
                        deny_message="Feishu personal target does not match owner's dedicated app binding.",
                    )
            canonical_target_address = "feishu:@me"
            recipient_id = bound_platform_uid
        else:
            # Discord user snowflake IDs are global across Discord apps; require exact owner match.
            if raw_ident != "@me" and raw_ident != bound_platform_uid:
                return AuthorizedDataTaskTarget(
                    allowed=False,
                    deny_status="failed",
                    deny_code="cross_user_target",
                    deny_message="Discord personal target does not match owner's bound Discord account.",
                )
            canonical_target_address = "discord:@me"
            recipient_id = bound_platform_uid
    else:
        canonical_target_address = f"{platform}:{raw_ident}"
        ok_group, group_err, _ = await _verify_group_target_deliverable(
            cur,
            user_id=owner_user_id,
            platform=platform,
            chat_address=canonical_target_address,
            conn=conn,
        )
        if not ok_group:
            return AuthorizedDataTaskTarget(
                allowed=False,
                deny_status="failed",
                deny_code="not_allowed",
                deny_message=group_err or "Group chat is not bound or accessible in Dalton channel settings.",
            )
        recipient_id = raw_ident

    resolved_plan: ResolvedDataTaskPlan | None = None
    if require_plan:
        paths = path_map or build_path_map()
        try:
            raw_outbox = _load_persisted_run_raw_outbox(
                norm_run_id,
                job_key,
                db_outbox_payload=run_row.get("outbox_payload"),
                log_tail=run_row.get("log_tail"),
                path_map=paths,
            )
            resolved_plan = build_data_task_delivery_plan(
                run_id=norm_run_id,
                job_key=job_key,
                raw_outbox=raw_outbox,
                run_artifacts_dir=paths.backend_run_artifacts_dir(norm_run_id),
            )
        except DataTaskNotificationError as exc:
            return AuthorizedDataTaskTarget(
                allowed=False,
                deny_status="failed",
                deny_code=exc.code,
                deny_message=str(exc),
            )

    return AuthorizedDataTaskTarget(
        allowed=True,
        run_id=norm_run_id,
        job_key=job_key,
        owner_user_id=owner_user_id,
        owner_platform_user_id=bound_platform_uid,
        platform=platform,
        target_kind=target_kind,
        requested_target=cleaned_target,
        canonical_target_address=canonical_target_address,
        recipient_id=recipient_id,
        plan=resolved_plan,
    )


def build_reconcile_identity_metadata(
    *,
    platform: str,
    recipient_id: str,
    canonical_target_address: str,
    delivery_keys: list[str],
) -> dict[str, Any]:
    """Build immutable recipient and item-key metadata for outer notification reconciliation."""
    return {
        "reconcile_platform": str(platform).strip().lower(),
        "reconcile_recipient_id": str(recipient_id).strip(),
        "reconcile_canonical_target": str(canonical_target_address).strip(),
        "expected_item_count": len(delivery_keys),
        "reconcile_delivery_keys": list(delivery_keys),
    }


async def _persist_reconcile_identity_on_outer_delivery(
    cur: Any,
    *,
    run_id: str,
    platform: str,
    requested_target: str,
    canonical_target_address: str,
    recipient_id: str,
    delivery_keys: list[str],
) -> None:
    """Stamp immutable original recipient and expected keys on the outer `dalton_notify_deliveries` row before external send."""
    if not delivery_keys or not recipient_id:
        return
    meta = build_reconcile_identity_metadata(
        platform=platform,
        recipient_id=recipient_id,
        canonical_target_address=canonical_target_address,
        delivery_keys=delivery_keys,
    )
    candidate_targets = list(
        dict.fromkeys(
            t for t in (requested_target.strip(), canonical_target_address.strip()) if t
        )
    )
    if not candidate_targets:
        return
    await cur.execute(
        """
        UPDATE dalton_notify_deliveries
        SET payload_json = COALESCE(payload_json, '{}'::jsonb) || %s::jsonb
        WHERE execution_id = %s
          AND channel = %s
          AND target = ANY(%s)
          AND NOT (
              COALESCE(payload_json, '{}'::jsonb) ? 'reconcile_recipient_id'
              AND COALESCE(payload_json, '{}'::jsonb) ? 'reconcile_delivery_keys'
          )
        """,
        (
             json.dumps(meta, ensure_ascii=False),
            run_id,
            platform,
            candidate_targets,
        ),
    )


async def authorize_data_task_notification_plan(
    *,
    run_id: str,
    target: str,
    path_map: DataTaskPathMap | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Authorize ``(run_id, target)`` and return the validated, redacted delivery plan."""
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                auth = await _authorize_run_and_target_in_tx(
                    cur,
                    owned_conn,
                    run_id=run_id,
                    target=target,
                    path_map=path_map,
                    require_plan=True,
                )
                if not auth.allowed or auth.plan is None:
                    return {
                        "authorized": False,
                        "status": auth.deny_status,
                        "code": auth.deny_code,
                        "message": auth.deny_message,
                    }

                delivery_keys = [
                    build_data_task_item_delivery_key(
                        run_id=auth.run_id,
                        platform=auth.platform,
                        recipient_id=auth.recipient_id,
                        item_index=item.item_index,
                    )
                    for item in auth.plan.items
                ]
                await _persist_reconcile_identity_on_outer_delivery(
                    cur,
                    run_id=auth.run_id,
                    platform=auth.platform,
                    requested_target=auth.requested_target,
                    canonical_target_address=auth.canonical_target_address,
                    recipient_id=auth.recipient_id,
                    delivery_keys=delivery_keys,
                )
                existing_by_key: dict[str, dict[str, Any]] = {}
                if delivery_keys:
                    await cur.execute(
                        """
                        SELECT delivery_key, status, sent_chunks, total_chunks, error_code
                        FROM dalton_channel_deliveries
                        WHERE delivery_key = ANY(%s)
                        """,
                        (delivery_keys,),
                    )
                    for row in await cur.fetchall():
                        existing_by_key[str(row["delivery_key"])] = dict(row)

                wire_items: list[dict[str, Any]] = []
                for item, d_key in zip(auth.plan.items, delivery_keys, strict=True):
                    item_dict = item.to_wire_dict(delivery_key=d_key)
                    ex = existing_by_key.get(d_key)
                    item_dict["existing_status"] = str(ex["status"]) if ex else None
                    wire_items.append(item_dict)

                plan_payload: dict[str, Any] = {
                    "authorized": True,
                    "status": "authorized",
                    "run_id": auth.run_id,
                    "job_key": auth.job_key,
                    "owner_user_id": auth.owner_user_id,
                    "platform": auth.platform,
                    "target_kind": auth.target_kind,
                    "requested_target": auth.requested_target,
                    "canonical_target_address": auth.canonical_target_address,
                    "recipient_id": auth.recipient_id,
                    "items": wire_items,
                }
                if auth.owner_platform_user_id is not None:
                    plan_payload["owner_platform_user_id"] = auth.owner_platform_user_id
                return plan_payload


async def authorize_and_claim_data_task_delivery(
    *,
    delivery_key: str,
    run_id: str | None = None,
    target: str | None = None,
    has_non_empty_text: bool = True,
    total_chunks: int = 0,
    path_map: DataTaskPathMap | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-6 ``kind='data_task'`` path: serialize, check idempotency, re-authorize, and claim ``in_flight``."""
    cleaned_key = str(delivery_key or "").strip()
    cleaned_rid = str(run_id or "").strip()
    cleaned_target = str(target or "").strip()
    if not cleaned_key or not cleaned_rid or not cleaned_target:
        return {
            "claim_status": "denied",
            "status": "failed",
            "code": "not_allowed",
            "message": "delivery_key, run_id, and target are required for data_task delivery.",
        }

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
                )
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    FOR UPDATE
                    """,
                    (cleaned_key,),
                )
                existing = await cur.fetchone()
                if existing:
                    ex_status = str(existing["status"])
                    if ex_status in ("sent", "partial"):
                        return {
                            "claim_status": "already_completed",
                            "status": ex_status,
                            "first_response_json": existing.get("first_response_json"),
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": False,
                        }
                    if ex_status in ("in_flight", "unknown"):
                        return {
                            "claim_status": "in_flight_or_unknown",
                            "status": "unknown",
                            "code": "in_flight",
                            "message": "An earlier attempt of this data-task item is in flight or uncertain.",
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": False,
                        }

                auth = await _authorize_run_and_target_in_tx(
                    cur,
                    owned_conn,
                    run_id=cleaned_rid,
                    target=cleaned_target,
                    path_map=path_map,
                    require_plan=True,
                )
                if not auth.allowed or auth.plan is None:
                    return {
                        "claim_status": "denied",
                        "status": auth.deny_status if auth.deny_status in ("failed", "unknown") else "failed",
                        "code": auth.deny_code,
                        "message": auth.deny_message,
                    }

                ordered_expected_keys = [
                    build_data_task_item_delivery_key(
                        run_id=auth.run_id,
                        platform=auth.platform,
                        recipient_id=auth.recipient_id,
                        item_index=item.item_index,
                    )
                    for item in auth.plan.items
                ]
                expected_keys = set(ordered_expected_keys)
                if cleaned_key not in expected_keys:
                    return {
                        "claim_status": "denied",
                        "status": "failed",
                        "code": "not_allowed",
                        "message": "delivery_key does not match an authorized item for this run and target.",
                    }

                await _persist_reconcile_identity_on_outer_delivery(
                    cur,
                    run_id=auth.run_id,
                    platform=auth.platform,
                    requested_target=auth.requested_target,
                    canonical_target_address=auth.canonical_target_address,
                    recipient_id=auth.recipient_id,
                    delivery_keys=ordered_expected_keys,
                )

                await cur.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, thread_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, total_chunks, sent_chunks, platform_message_ids,
                        files_status, created_at, updated_at
                    )
                    VALUES (%s, 'data_task', %s, NULL, %s, %s::uuid, FALSE, %s, %s, %s, 'in_flight', %s, 0, '[]'::jsonb, '[]'::jsonb, NOW(), NOW())
                    ON CONFLICT (delivery_key) DO UPDATE SET
                        status = 'in_flight',
                        total_chunks = EXCLUDED.total_chunks,
                        error_code = NULL,
                        error_message = NULL,
                        updated_at = NOW()
                    """,
                    (
                        cleaned_key,
                        auth.owner_user_id,
                        auth.run_id,
                        auth.run_id,
                        bool(has_non_empty_text),
                        auth.platform,
                        auth.canonical_target_address,
                        int(total_chunks or 0),
                    ),
                )

                return {
                    "claim_status": "claimed",
                    "recipient_id": auth.recipient_id,
                    "platform": auth.platform,
                    "target_address": auth.canonical_target_address,
                    "is_current": False,
                    "home_workspace_id": get_home_workspace_id(auth.owner_user_id),
                    "weixin_context_token": None,
                }


async def load_authorized_data_task_artifact(
    *,
    run_id: str,
    artifact_id: str,
    path_map: DataTaskPathMap | None = None,
    conn: Any = None,
) -> ValidatedArtifactBytes:
    """Verify ``run_id`` is an eligible succeeded production run and read ``artifact_id`` safely."""
    norm_run_id = normalize_run_uuid(run_id)
    paths = path_map or build_path_map()

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT r.run_id::text AS run_id,
                       r.job_key,
                       r.status,
                       r.notify_status,
                       r.outbox_payload,
                       r.log_tail,
                       d.job_key AS def_job_key,
                       d.enabled AS task_enabled,
                       d.notify_enabled AS task_notify_enabled
                FROM dalton_data_task_runs r
                LEFT JOIN dalton_data_task_defs d
                  ON d.job_key = r.job_key
                WHERE r.run_id = %s::uuid
                """,
                (norm_run_id,),
            )
            run_row = await cur.fetchone()

    if not run_row or not run_row.get("def_job_key"):
        raise DataTaskArtifactNotFoundError("Data-task run not found")

    if str(run_row.get("status") or "").strip().lower() != "succeeded":
        raise DataTaskArtifactError("Data-task run did not succeed", code="run_not_succeeded")

    notify_status = str(run_row.get("notify_status") or "").strip().lower()
    if notify_status == "preview_only" or notify_status not in ("pending_dispatch", "dispatched"):
        raise DataTaskArtifactError(
            "Preview/trial run artifacts cannot be fetched for live channel delivery",
            code="preview_run_forbidden",
        )

    if not bool(run_row.get("task_enabled")) or not bool(run_row.get("task_notify_enabled")):
        raise DataTaskArtifactError("Data-task notifications are disabled", code="task_disabled")

    job_key = str(run_row["job_key"]).strip()
    raw_outbox = _load_persisted_run_raw_outbox(
        norm_run_id,
        job_key,
        db_outbox_payload=run_row.get("outbox_payload"),
        log_tail=run_row.get("log_tail"),
        path_map=paths,
    )
    run_art_dir = paths.backend_run_artifacts_dir(norm_run_id)
    plan = build_data_task_delivery_plan(
        run_id=norm_run_id,
        job_key=job_key,
        raw_outbox=raw_outbox,
        run_artifacts_dir=run_art_dir,
    )
    return read_validated_run_artifact(
        run_id=norm_run_id,
        artifact_id=artifact_id,
        plan=plan,
        run_artifacts_dir=run_art_dir,
    )


def _derive_expected_item_count_from_persisted_payload(
    payload: Mapping[str, Any] | None,
) -> int | None:
    """Derive expected item count `N` (`0 .. N-1`) strictly from frozen outer `payload_json`."""
    if not isinstance(payload, Mapping) or not payload:
        return None

    raw_cards = payload.get("feishu_cards")
    if raw_cards is not None:
        if not isinstance(raw_cards, list) or not raw_cards:
            return None
        if not all(
            isinstance(entry, Mapping) and isinstance(entry.get("card"), Mapping)
            for entry in raw_cards
        ):
            return None
        return len(raw_cards)

    raw_items = payload.get("items")
    if raw_items is not None:
        if not isinstance(raw_items, list) or not raw_items:
            return None
        seen_indices: set[int] = set()
        for pos, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, Mapping):
                return None
            try:
                idx_val = int(raw_item.get("index", pos))
            except (TypeError, ValueError):
                return None
            if idx_val < 0 or idx_val in seen_indices:
                return None
            seen_indices.add(idx_val)
        if sorted(seen_indices) != list(range(len(raw_items))):
            return None
        return len(raw_items)

    explicit_count = payload.get("expected_item_count")
    if isinstance(explicit_count, int) and not isinstance(explicit_count, bool) and explicit_count > 0:
        return explicit_count

    if isinstance(payload.get("card"), Mapping):
        return 1

    for text_key in ("feishu_message", "message", "text", "summary"):
        val = payload.get(text_key)
        if isinstance(val, str) and val.strip():
            return 1

    return None


async def ensure_outer_reconcile_identity_persisted(
    *,
    run_id: str,
    channel: str,
    target: str,
    owner_user_id: str,
    payload: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Resolve and persist immutable recipient/item-key identity on the outer notification before external send."""
    try:
        norm_run_id = normalize_run_uuid(run_id)
    except DataTaskArtifactError:
        return None

    parsed_target = _parse_requested_notify_target(target)
    if parsed_target is None:
        return None
    platform, target_kind, raw_ident = parsed_target
    if platform != str(channel or "").strip().lower():
        return None

    expected_count = _derive_expected_item_count_from_persisted_payload(payload)
    if expected_count is None or expected_count <= 0:
        return None

    try:
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT payload_json
                        FROM dalton_notify_deliveries
                        WHERE execution_id = %s
                          AND channel = %s
                          AND target = %s
                        FOR UPDATE
                        """,
                        (norm_run_id, platform, target),
                    )
                    existing_row = await cur.fetchone()
                    existing_payload = (
                        existing_row.get("payload_json")
                        if existing_row and isinstance(existing_row.get("payload_json"), Mapping)
                        else None
                    )
                    if (
                        isinstance(existing_payload, Mapping)
                        and str(existing_payload.get("reconcile_recipient_id") or "").strip()
                        and isinstance(existing_payload.get("reconcile_delivery_keys"), list)
                    ):
                        return {
                            "reconcile_platform": str(existing_payload.get("reconcile_platform") or platform),
                            "reconcile_recipient_id": str(existing_payload["reconcile_recipient_id"]).strip(),
                            "reconcile_canonical_target": str(
                                existing_payload.get("reconcile_canonical_target") or target
                            ).strip(),
                            "expected_item_count": int(
                                existing_payload.get("expected_item_count") or expected_count
                            ),
                            "reconcile_delivery_keys": list(existing_payload["reconcile_delivery_keys"]),
                        }

                    if target_kind == "group":
                        canonical_target_address = f"{platform}:{raw_ident}"
                        recipient_id = raw_ident
                    else:
                        canonical_target_address = f"{platform}:@me"
                        await cur.execute(
                            """
                            SELECT platform_user_id
                            FROM dalton_channel_identities
                            WHERE user_id = %s AND platform = %s
                            """,
                            (owner_user_id, platform),
                        )
                        id_row = await cur.fetchone()
                        bound_uid = (
                            str(id_row.get("platform_user_id") or "").strip()
                            if id_row
                            else ""
                        )
                        if platform == "discord":
                            if raw_ident == "@me":
                                if not bound_uid:
                                    return None
                                recipient_id = bound_uid
                            else:
                                if bound_uid and bound_uid != raw_ident:
                                    return None
                                recipient_id = raw_ident
                        else:
                            if raw_ident == "@me":
                                if not bound_uid:
                                    return None
                                recipient_id = bound_uid
                            elif bound_uid:
                                recipient_id = bound_uid
                            else:
                                recipient_id = raw_ident

                    delivery_keys = [
                        build_data_task_item_delivery_key(
                            run_id=norm_run_id,
                            platform=platform,
                            recipient_id=recipient_id,
                            item_index=idx,
                        )
                        for idx in range(expected_count)
                    ]
                    await _persist_reconcile_identity_on_outer_delivery(
                        cur,
                        run_id=norm_run_id,
                        platform=platform,
                        requested_target=target,
                        canonical_target_address=canonical_target_address,
                        recipient_id=recipient_id,
                        delivery_keys=delivery_keys,
                    )
                    return build_reconcile_identity_metadata(
                        platform=platform,
                        recipient_id=recipient_id,
                        canonical_target_address=canonical_target_address,
                        delivery_keys=delivery_keys,
                    )
    except Exception:
        logger.debug(
            "Unable to persist initial reconcile identity for run_id=%s target=%s",
            run_id,
            target,
            exc_info=True,
        )
        return None


async def _resolve_reconcile_recipient_identity(
    cur: Any,
    *,
    norm_run_id: str,
    owner_user_id: str,
    platform: str,
    target_kind: str,
    raw_ident: str,
    expected_count: int,
    payload: Mapping[str, Any] | None = None,
) -> tuple[str, str, list[str]] | None:
    """Resolve immutable `(canonical_target_address, recipient_id, expected_keys)` without querying current bindings.

    Never consults `dalton_channel_identities` or current routes/preferences.
    1. Uses the persisted `reconcile_*` identity recorded on the outer `dalton_notify_deliveries.payload_json`
       before the first external effect.
    2. For group targets, uses the deterministic group target `(f"{platform}:{raw_ident}", raw_ident)`.
    3. For legacy DM rows lacking `reconcile_*` in `payload_json`, inspects `dalton_channel_deliveries`
       for `(kind='data_task', run_id=norm_run_id, user_id=owner_user_id, platform=platform)` and
       accepts only a single unambiguous recipient matching the legacy target, refusing to guess
       when zero or multiple recipients exist.
    """
    if isinstance(payload, Mapping):
        persisted_platform = str(payload.get("reconcile_platform") or "").strip().lower()
        persisted_recipient = str(payload.get("reconcile_recipient_id") or "").strip()
        persisted_canon = str(payload.get("reconcile_canonical_target") or "").strip()
        persisted_keys = payload.get("reconcile_delivery_keys")

        if persisted_platform and persisted_platform != platform:
            return None

        if persisted_recipient or persisted_keys is not None:
            if not persisted_recipient:
                return None
            if target_kind == "group":
                expected_canon = f"{platform}:{raw_ident}"
                if persisted_recipient != raw_ident:
                    return None
                if persisted_canon and persisted_canon != expected_canon:
                    return None
                canonical_target_address = expected_canon
            else:
                expected_canon = f"{platform}:@me"
                if persisted_canon and persisted_canon != expected_canon:
                    return None
                canonical_target_address = expected_canon

            computed_keys = [
                build_data_task_item_delivery_key(
                    run_id=norm_run_id,
                    platform=platform,
                    recipient_id=persisted_recipient,
                    item_index=idx,
                )
                for idx in range(expected_count)
            ]
            if persisted_keys is not None:
                if not isinstance(persisted_keys, list) or len(persisted_keys) != expected_count:
                    return None
                cleaned_persisted_keys = [str(k or "").strip() for k in persisted_keys]
                if cleaned_persisted_keys != computed_keys:
                    return None
            return canonical_target_address, persisted_recipient, computed_keys

    if target_kind == "group":
        canonical_target_address = f"{platform}:{raw_ident}"
        recipient_id = raw_ident
        computed_keys = [
            build_data_task_item_delivery_key(
                run_id=norm_run_id,
                platform=platform,
                recipient_id=recipient_id,
                item_index=idx,
            )
            for idx in range(expected_count)
        ]
        return canonical_target_address, recipient_id, computed_keys

    # Legacy DM row without persisted `reconcile_*` metadata in `payload_json`:
    # Derive strictly from persisted `dalton_channel_deliveries` rows for this run/owner/platform,
    # never from current `dalton_channel_identities`, and refuse if zero or multiple recipients exist.
    canonical_target_address = f"{platform}:@me"
    await cur.execute(
        """
        SELECT delivery_key, target_address
        FROM dalton_channel_deliveries
        WHERE kind = 'data_task'
          AND run_id = %s
          AND user_id = %s
          AND platform = %s
        """,
        (norm_run_id, owner_user_id, platform),
    )
    ledger_rows = await cur.fetchall()
    if not ledger_rows:
        return None

    key_prefix = f"data_task:{norm_run_id}:{platform}:"
    distinct_recipients: set[str] = set()
    for lrow in ledger_rows:
        row_taddr = str(lrow.get("target_address") or "").strip()
        if row_taddr != canonical_target_address:
            continue
        dkey = str(lrow.get("delivery_key") or "").strip()
        if not dkey.startswith(key_prefix):
            return None
        remainder = dkey[len(key_prefix) :]
        if ":item:" not in remainder:
            return None
        rec_part, idx_part = remainder.rsplit(":item:", 1)
        rec_clean = rec_part.strip()
        if not rec_clean or ":" in rec_clean:
            return None
        try:
            idx_int = int(idx_part)
        except (TypeError, ValueError):
            return None
        if idx_int < 0 or idx_int >= expected_count:
            return None
        distinct_recipients.add(rec_clean)

    if len(distinct_recipients) != 1:
        return None

    ledger_recipient = next(iter(distinct_recipients))
    if raw_ident != "@me" and ledger_recipient != raw_ident:
        return None

    computed_keys = [
        build_data_task_item_delivery_key(
            run_id=norm_run_id,
            platform=platform,
            recipient_id=ledger_recipient,
            item_index=idx,
        )
        for idx in range(expected_count)
    ]
    return canonical_target_address, ledger_recipient, computed_keys


async def reconcile_uncertain_data_task_deliveries(
    *,
    limit: int = 5,
    max_duration_s: float = 30.0,
    started_at: float | None = None,
    mark_delivery_fn: Callable[..., Awaitable[Any]] | None = None,
    conn: Any = None,
) -> int:
    """Read-only reconciliation of uncertain `dalton_notify_deliveries` rows against `dalton_channel_deliveries`.

    Remains active even after `attempt_count >= max_attempts`. Never authorizes a claim,
    never invokes platform/gateway sends, never resets unknown items, and never increments
    `attempt_count`. Marks the outer row `sent` iff all expected items for the exact original
    `(run_id, owner_user_id, platform, canonical_target_address, recipient_id)` are durably `sent`.
    """
    eff_limit = max(1, int(limit or 5))
    t0 = started_at if started_at is not None else time.monotonic()
    uncertain_codes = sorted(UNCERTAIN_NOTIFY_ERROR_CODES)
    rotation_seed = str(time.time_ns())
    reconciled_count = 0

    try:
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT d.execution_id, d.automation_id, d.user_id,
                           d.channel, d.target, d.status, d.attempt_count,
                           d.error_code, d.payload_json
                    FROM dalton_notify_deliveries d
                    WHERE d.status = 'pending'
                      AND COALESCE(d.error_code, '') = ANY(%s)
                      AND (
                          d.automation_id LIKE 'data:%%'
                          OR (d.payload_json ->> 'route_key') LIKE 'data:%%'
                      )
                    ORDER BY
                        (
                            EXISTS (
                                SELECT 1
                                FROM dalton_channel_deliveries cd
                                WHERE cd.kind = 'data_task'
                                  AND cd.run_id = d.execution_id
                                  AND cd.user_id = d.user_id
                                  AND cd.platform = d.channel
                                  AND cd.status = 'sent'
                            )
                            AND NOT EXISTS (
                                SELECT 1
                                FROM dalton_channel_deliveries cd2
                                WHERE cd2.kind = 'data_task'
                                  AND cd2.run_id = d.execution_id
                                  AND cd2.user_id = d.user_id
                                  AND cd2.platform = d.channel
                                  AND cd2.status <> 'sent'
                            )
                        ) DESC,
                        md5(
                            COALESCE(d.execution_id, '') || ':' ||
                            COALESCE(d.channel, '') || ':' ||
                            COALESCE(d.target, '') || ':' || %s
                        ) ASC
                    LIMIT %s
                    """,
                    (uncertain_codes, rotation_seed, eff_limit),
                )
                candidates = await cur.fetchall()

        for row in candidates or []:
            if time.monotonic() - t0 >= max_duration_s:
                break

            raw_exec_id = str(row.get("execution_id") or "").strip()
            owner_user_id = str(row.get("user_id") or "").strip()
            channel = str(row.get("channel") or "").strip().lower()
            target = str(row.get("target") or "").strip()
            payload = (
                row.get("payload_json")
                if isinstance(row.get("payload_json"), Mapping)
                else None
            )
            if not (raw_exec_id and owner_user_id and channel and target):
                continue

            try:
                norm_run_id = normalize_run_uuid(raw_exec_id)
            except DataTaskArtifactError:
                continue

            parsed_target = _parse_requested_notify_target(target)
            if parsed_target is None:
                continue
            platform, target_kind, raw_ident = parsed_target
            if platform != channel:
                continue

            expected_count = _derive_expected_item_count_from_persisted_payload(payload)
            if expected_count is None or expected_count <= 0:
                continue

            try:
                async with get_db_connection(conn) as row_conn:
                    async with row_conn.cursor(row_factory=dict_row) as cur:
                        resolved_ident = await _resolve_reconcile_recipient_identity(
                            cur,
                            norm_run_id=norm_run_id,
                            owner_user_id=owner_user_id,
                            platform=platform,
                            target_kind=target_kind,
                            raw_ident=raw_ident,
                            expected_count=expected_count,
                            payload=payload,
                        )
                        if resolved_ident is None:
                            continue
                        canonical_target_address, _recipient_id, expected_keys = resolved_ident

                        await cur.execute(
                            """
                            SELECT delivery_key, status
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
                        item_rows = await cur.fetchall()

                    status_by_key = {
                        str(ir["delivery_key"]): str(ir.get("status") or "").strip().lower()
                        for ir in (item_rows or [])
                    }
                    if len(status_by_key) != expected_count or not all(
                        status_by_key.get(k) == "sent" for k in expected_keys
                    ):
                        continue

                    updated_outer = False
                    async with row_conn.transaction():
                        async with row_conn.cursor(row_factory=dict_row) as tx_cur:
                            await tx_cur.execute(
                                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                                (f"dalton_reconcile:{norm_run_id}:{channel}:{target}",),
                            )
                            await tx_cur.execute(
                                """
                                SELECT delivery_key, status
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
                            tx_items = await tx_cur.fetchall()
                            tx_status_by_key = {
                                str(ir["delivery_key"]): str(ir.get("status") or "").strip().lower()
                                for ir in (tx_items or [])
                            }
                            if len(tx_status_by_key) != expected_count or not all(
                                tx_status_by_key.get(k) == "sent" for k in expected_keys
                            ):
                                continue

                            await tx_cur.execute(
                                """
                                UPDATE dalton_notify_deliveries
                                SET status = 'sent',
                                    error_code = NULL,
                                    updated_at = NOW()
                                WHERE execution_id = %s
                                  AND channel = %s
                                  AND target = %s
                                  AND status = 'pending'
                                  AND COALESCE(error_code, '') = ANY(%s)
                                """,
                                (raw_exec_id, channel, target, uncertain_codes),
                            )
                            if isinstance(tx_cur.rowcount, int) and tx_cur.rowcount > 0:
                                updated_outer = True
                                await tx_cur.execute(
                                    """
                                    UPDATE dalton_data_task_runs r
                                    SET notify_status = 'dispatched',
                                        updated_at = NOW()
                                    WHERE r.run_id = %s::uuid
                                      AND r.notify_status = 'pending_dispatch'
                                      AND EXISTS (
                                          SELECT 1
                                          FROM dalton_notify_deliveries d
                                          WHERE d.execution_id = %s
                                            AND d.status = 'sent'
                                      )
                                      AND NOT EXISTS (
                                          SELECT 1
                                          FROM dalton_notify_deliveries d
                                          WHERE d.execution_id = %s
                                            AND d.status NOT IN ('sent', 'skipped')
                                      )
                                    """,
                                    (norm_run_id, raw_exec_id, raw_exec_id),
                                )

                    if updated_outer:
                        reconciled_count += 1
                        if mark_delivery_fn is not None:
                            await mark_delivery_fn(
                                raw_exec_id,
                                channel,
                                target,
                                status="sent",
                                error_code=None,
                                expected_status="pending",
                            )
            except asyncio.CancelledError:
                raise
            except Exception as row_exc:
                logger.warning(
                    "Data-task uncertain delivery reconciliation skipped row execution_id=%s: error_type=%s",
                    raw_exec_id,
                    type(row_exc).__name__,
                )
    except asyncio.CancelledError:
        raise
    except Exception as scan_exc:
        logger.warning(
            "Data-task uncertain delivery reconciliation scan failed: error_type=%s",
            type(scan_exc).__name__,
        )

    return reconciled_count
