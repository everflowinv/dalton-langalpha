"""Channel Phase 2 Batch 3: interactive action tokens, runtime barrier config,
three-terminal HITL resume contention, card binding/self-heal, and recovery.

Implements §3.1–§3.3, §3.5 (shared predicates), §5.2, and §5.4:
- Authoritative outer-kind + inner-type L0–L3 risk classification and strict
  managed-cloud ``create_workspace`` snapshot verification.
- Single-row Level 7b-0 runtime barrier (``dalton_channel_interrupt_runtime_config``)
  strictly separating button-only toggle (``set_interrupt_runtime_config``) from
  three-switch drain mode (``enter_interrupt_drain_mode``).
- Global strictly ascending lock hierarchy (Levels 0b-1 -> 0b-2 -> 3a -> 6 -> 7a ->
  7b-0 -> 7b-1 -> 7b-2 -> 7b-3) with immediate ``weixin_account_id`` lock-set
  verification after locking ``dalton_channel_inbound_events``, unconditional
  ``claim_id`` re-verification after locking ``7b-2``, and ``cur.rowcount == 1``
  guards on every insert/takeover.
- Shared 3-branch card binding helper ``_apply_confirm_bound_in_tx`` invoked by
  ``bind-card(confirm_bound)``, ``inspect-gate`` self-heal, and ``consume`` self-heal.
- Shared 3-way terminal card patch gate ``compute_can_patch_terminal`` used by click
  callbacks, ``/internal/recover``, and card-only ``ack``.
- Separate origin-channel vs per-card/late-card ``ack`` capability checks and
  ``credit_supplement`` ``pending_resubmit`` preservation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import logging
import random
import secrets
from typing import Any
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton import sharing as dalton_sharing
from src.dalton.access_tiers import (
    check_team_member_status_live,
    evaluate_weixin_member_access,
    get_user_tier,
)
from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    platform_to_channel_capability,
    require_channel_platform_capability,
)
from src.dalton.company_identity import reject_company_system_auth
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import weixin_multi_account as wx_db
from src.dalton.settings import (
    is_channel_bt2_link_recovery_enabled,
    is_channel_interactive_actions_enabled,
    is_channel_interrupt_ledger_enabled,
)
from src.server.contracts.status import (
    INTERRUPT_REASON_CREDIT_PAUSE,
    asked_for,
    is_restart_interrupted,
    stopper_of,
)
from src.server.database import pool as db_pool
from src.server.handlers.checkpoint_handler import _snapshot_matches_action
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)


def get_db_connection(existing_conn: Any = None):
    return db_pool.get_db_connection(existing_conn)


# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------

DALTON_HITL_RESUME_NAMESPACE = UUID("6f3c9d10-4b28-4c5e-9a7d-2e1f8b4a0003")

ALL_CHANNEL_PLATFORMS: tuple[str, ...] = ("feishu", "discord", "weixin")
CARD_PLATFORMS: tuple[str, ...] = ("feishu", "discord")
DISCORD_ONLY_PLATFORMS: tuple[str, ...] = ("discord",)

ALLOWED_OUTER_KINDS: frozenset[str] = frozenset(
    {"ask_user_question", "tool_approval", "hitl_confirm"}
)
HIGH_RISK_OUTER_KINDS: frozenset[str] = frozenset(
    {"order_approval", "publish_approval"}
)
MANAGED_CLOUD_COMPUTER_KINDS: frozenset[str] = frozenset(
    {"daytona", "e2b", "docker"}
)
NOTIFY_CONFIRM_TOKEN_ACTIONS: frozenset[str] = frozenset(
    {
        "dalton_notify_set_route",
        "dalton_notify_add_personal_target",
        "dalton_notify_test",
    }
)
TEXT_CLAIM_ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(
    {"ask_user_question", INTERRUPT_REASON_CREDIT_PAUSE}
)

RISK_TIER_TTL_SECONDS: dict[str, int] = {
    "L0": 6 * 3600,  # 6 hours
    "L1": 30 * 60,  # 30 minutes
    "L2": 10 * 60,  # 10 minutes
}
EPHEMERAL_CONFIRM_MAX_TTL_SECONDS = 14 * 60  # 14 minutes (< Discord 15m token TTL)
PENDING_BIND_EXPIRE_SECONDS = 90
RESUME_LEASE_SECONDS = 90
DISPATCH_LEASE_SECONDS = 300  # 5 minutes
MAX_RESUME_RETRIES = 5
MAX_CARD_PATCH_ATTEMPTS = 8
MAX_BT2_DELIVERY_ATTEMPTS = 8

CARD_TERMINAL_PATCH_STATUSES: frozenset[str] = frozenset(
    {
        "patched",
        "deleted",
        "uneditable_expired",
        "failed_exhausted",
        "skipped_platform_disabled",
    }
)
BT2_TERMINAL_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "not_allowed",
        "max_attempts_exceeded",
        "skipped_platform_disabled",
    }
)
ACTIVE_OR_ADVANCED_RUN_STATUSES: tuple[str, ...] = (
    "in_progress",
    "completed",
    "interrupted",
)
UNADVANCED_RUN_STATUSES: tuple[str, ...] = ("error", "cancelled")

_TX_MAX_ATTEMPTS = 6
_TX_BACKOFF_MIN_S = 0.015
_TX_BACKOFF_MAX_S = 0.06


class _ResumeInboundLockSetChanged(Exception):
    """Internal retryable signal raised when previewed claim_id, inbound keys, or
    Weixin account lock sets changed before row locks were acquired."""


# Deterministic concurrency test hooks (always None in production).
_after_preview_hook: Callable[[str], Awaitable[None]] | None = None
_before_7b0_lock_hook: Callable[[str], Awaitable[None]] | None = None
_after_7b0_lock_hook: Callable[[str], Awaitable[None]] | None = None
_after_7a_inbound_lock_hook: Callable[[str], Awaitable[None]] | None = None
_after_7b2_lock_hook: Callable[[str], Awaitable[None]] | None = None


# ---------------------------------------------------------------------------
# Pure Helpers & Platform Guards
# ---------------------------------------------------------------------------


def require_known_channel_platform(
    platform: str | None,
    *,
    allowed: tuple[str, ...] | frozenset[str] = ALL_CHANNEL_PLATFORMS,
    invalid_status: int = 400,
) -> str:
    """Validate ``platform`` against an explicit allowlist BEFORE capability enforcement."""
    cleaned = str(platform or "").strip().lower()
    if cleaned not in allowed:
        raise HTTPException(
            status_code=invalid_status,
            detail={
                "code": "invalid_platform",
                "message": f"Unsupported channel platform: {platform!r}",
            },
        )
    require_channel_platform_capability(cleaned)
    return cleaned


def get_enabled_channel_platforms() -> list[str]:
    """Return channel platforms whose gateway + sub-capability are currently enabled."""
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return []
    out: list[str] = []
    for plat in ALL_CHANNEL_PLATFORMS:
        cap = platform_to_channel_capability(plat)
        if cap is not None and has_capability(cap):
            out.append(plat)
    return out


def generate_approval_nonce() -> str:
    """Generate a 128-bit Base64URL nonce (22 chars, fits Discord 100-char custom_id)."""
    return secrets.token_urlsafe(16)


def hash_approval_nonce(nonce: str) -> str:
    """Return SHA-256 hex digest of an approval nonce."""
    return hashlib.sha256(str(nonce).encode("utf-8")).hexdigest()


def derive_hitl_resume_request_key(
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    claim_id: str,
) -> str:
    """Derive a deterministic UUIDv5 ChatRequest.request_key bound to ``claim_id``."""
    return str(
        uuid5(
            DALTON_HITL_RESUME_NAMESPACE,
            f"{thread_id}:{run_id}:{interrupt_id}:{claim_id}",
        )
    )


def compute_args_sha256(action_req: Mapping[str, Any]) -> str:
    """Canonical SHA-256 digest of a pending action request for tamper/drift detection."""
    encoded = json.dumps(
        dict(action_req),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_can_patch_terminal(resume_row: Mapping[str, Any] | None) -> bool:
    """Shared 3-way terminal card patch gate (click callbacks, /internal/recover, card-only ack)."""
    if not resume_row:
        return False
    if not bool(resume_row.get("delivery_settled")):
        return False
    status = str(resume_row.get("resume_status") or "").strip().lower()
    if status == "terminal_failed":
        return True
    return status == "admitted" and resume_row.get("run_consumed_at") is not None


def is_retryable_disconnect_cancel(run_row: Mapping[str, Any] | None) -> bool:
    """True iff a cancelled run was interrupted by disconnect/shutdown/graceful_restart with no explicit stop."""
    if not run_row or str(run_row.get("status") or "").strip().lower() != "cancelled":
        return False
    if stopper_of(run_row) is not None:
        return False
    meta = run_row.get("metadata")
    cancel_source = (
        str(meta.get("dalton_cancel_source") or "").strip().lower()
        if isinstance(meta, Mapping)
        else ""
    )
    if cancel_source in ("client_disconnect", "server_shutdown", "graceful_restart"):
        return True
    return is_restart_interrupted(run_row)


def _classify_non_retryable_cancel_reason(run_row: Mapping[str, Any] | None) -> str:
    if not run_row:
        return "cancelled_unclassified"
    status = str(run_row.get("status") or "").strip().lower()
    if status == "error":
        return "run_error"
    if asked_for(run_row):
        return "cancelled_by_user"
    meta = run_row.get("metadata")
    cancel_source = (
        str(meta.get("dalton_cancel_source") or "").strip().lower()
        if isinstance(meta, Mapping)
        else ""
    )
    if cancel_source == "stale_workspace":
        return "stale_workspace"
    return "cancelled_unclassified"


def _coerce_utc(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str) and val.strip():
        try:
            dt = datetime.fromisoformat(val.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(val: Any) -> str | None:
    dt = _coerce_utc(val)
    return dt.isoformat() if dt is not None else (str(val) if val is not None else None)


def _parse_epoch_strict(raw_epoch: int | str | None, *, required: bool = True) -> int | None:
    if raw_epoch is None or not str(raw_epoch).strip():
        if required:
            raise wx_db.StaleGatewayEpochError("Missing X-Gateway-Epoch header")
        return None
    try:
        val = int(str(raw_epoch).strip())
    except ValueError as exc:
        raise wx_db.StaleGatewayEpochError("Invalid X-Gateway-Epoch header") from exc
    if val <= 0:
        raise wx_db.StaleGatewayEpochError("Invalid X-Gateway-Epoch header")
    return val


def _is_credit_supplement_pending_resubmit(inbound_row: Mapping[str, Any] | None) -> bool:
    """True when a credit_pause supplement has transitioned into pending_resubmit takeover (§3.3 #1/#4)."""
    if not inbound_row:
        return False
    if str(inbound_row.get("status") or "").strip().lower() == "pending_resubmit":
        return True
    if inbound_row.get("resubmitted_request_key") is not None:
        return True
    raw_p = inbound_row.get("raw_payload")
    if isinstance(raw_p, Mapping) and str(raw_p.get("credit_steer_state") or "").strip().lower() == "resubmitted":
        return True
    return False


def _token_consumed_via_for_resume(
    resume_row: Mapping[str, Any],
    token_hash: str,
) -> str:
    if str(resume_row.get("token_hash") or "") == token_hash:
        return "channel"
    settled_via = str(resume_row.get("settled_via") or "").strip().lower()
    if settled_via == "web":
        return "web"
    if settled_via == "channel_text":
        return "text_reply"
    return "superseded"


def _backoff_seconds(attempts: int, *, base: float = 5.0, cap: float = 300.0) -> float:
    return min(cap, base * (2 ** max(0, int(attempts) - 1)))


def _is_retryable_interrupt_tx_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            _ResumeInboundLockSetChanged,
            wx_db.WeixinLockSetChanged,
            psycopg.errors.DeadlockDetected,
        ),
    )


async def _run_interrupt_tx_with_retry(
    op_name: str,
    conn: Any,
    attempt_fn: Callable[[Any], Awaitable[Any]],
    *,
    max_attempts: int = _TX_MAX_ATTEMPTS,
) -> Any:
    """Retry transaction on lock-set drift or deadlock when we own the connection."""
    if conn is not None:
        return await attempt_fn(conn)
    for attempt in range(1, max_attempts + 1):
        try:
            return await attempt_fn(None)
        except Exception as exc:
            if not _is_retryable_interrupt_tx_error(exc) or attempt >= max_attempts:
                raise
            logger.debug(
                "[channel_action_tokens] %s retry %d/%d after %s",
                op_name,
                attempt,
                max_attempts,
                type(exc).__name__,
            )
            await asyncio.sleep(random.uniform(_TX_BACKOFF_MIN_S, _TX_BACKOFF_MAX_S))
    raise RuntimeError(f"{op_name} retry loop exhausted")


# ---------------------------------------------------------------------------
# Live Checkpoint Reader Helper (replaceable via autospec in unit tests)
# ---------------------------------------------------------------------------


async def load_live_pending_interrupts(
    thread_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Return ``(pending_by_id, approval_snapshots)`` from the thread's newest checkpoint."""
    from src.server.utils.checkpoint_helpers import (
        get_checkpointer,
        interrupt_records,
        newest_checkpoint,
    )

    checkpointer = get_checkpointer()
    newest = await newest_checkpoint(checkpointer, thread_id)
    if newest is None:
        return {}, {}
    pending_list = interrupt_records(newest)
    pending_by_id: dict[str, dict[str, Any]] = {}
    for rec in pending_list:
        if isinstance(rec, Mapping):
            iid = str(rec.get("id") or "").strip()
            if iid:
                pending_by_id[iid] = dict(rec)
    cp_data = newest.checkpoint if isinstance(newest.checkpoint, Mapping) else {}
    ch_vals = cp_data.get("channel_values") if isinstance(cp_data.get("channel_values"), Mapping) else {}
    snaps = ch_vals.get("approval_snapshots") if isinstance(ch_vals.get("approval_snapshots"), Mapping) else {}
    return pending_by_id, dict(snaps)


# ---------------------------------------------------------------------------
# Managed-Cloud create_workspace Verification & Risk Classification (§3.1)
# ---------------------------------------------------------------------------


async def is_verified_managed_cloud_create_workspace(
    action_req: Mapping[str, Any] | None,
    snap: Mapping[str, Any] | None,
    initiator: str,
    *,
    cur: Any = None,
    conn: Any = None,
) -> bool:
    """Verify ``create_workspace`` targets a verified managed cloud computer or cloud primary bootstrap.

    Both ``action_req["computer_is_own_machine"]`` and ``snap["computer_is_own_machine"]``
    must be literal boolean ``False`` (missing or ``None`` is rejected -> L3).
    """
    if not isinstance(action_req, Mapping) or not isinstance(snap, Mapping):
        return False
    if action_req.get("type") != "create_workspace":
        return False
    if snap.get("action") != "create_workspace":
        return False
    if action_req.get("computer_is_own_machine") is not False:
        return False
    if snap.get("computer_is_own_machine") is not False:
        return False
    if not _snapshot_matches_action(dict(snap), dict(action_req)):
        return False

    snap_cid = snap.get("computer_id")
    if isinstance(snap_cid, str) and snap_cid.strip():
        clean_cid = snap_cid.strip()
        if str(action_req.get("computer_id") or "").strip() != clean_cid:
            return False
        if str(snap.get("computer_kind") or "").strip().lower() == "ssh":
            return False
        norm_cid = normalize_uuid(clean_cid)
        if not norm_cid:
            return False

        async def _check_computer(c: Any) -> bool:
            await c.execute(
                """
                SELECT user_id, kind, status
                FROM computers
                WHERE computer_id = %s::uuid
                """,
                (norm_cid,),
            )
            comp = await c.fetchone()
            if not comp:
                return False
            if str(comp.get("user_id") or "") != str(initiator):
                return False
            if str(comp.get("status") or "").strip().lower() == "deleted":
                return False
            return str(comp.get("kind") or "").strip().lower() in MANAGED_CLOUD_COMPUTER_KINDS

        if cur is not None:
            return await _check_computer(cur)
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.cursor(row_factory=dict_row) as c:
                return await _check_computer(c)

    if snap_cid is None:
        return (
            snap.get("bootstrap_cloud_primary") is True
            and action_req.get("bootstrap_cloud_primary") is True
        )
    return False


async def _are_notify_targets_self_dm_only(
    cur: Any,
    *,
    dalton_user_id: str,
    action_req: Mapping[str, Any],
) -> bool:
    """Return True iff every resolved target in a notify action belongs to the user's own bound DM set."""
    raw_targets: list[str] = []
    if isinstance(action_req.get("targets"), list):
        raw_targets.extend(str(t).strip() for t in action_req["targets"] if str(t).strip())
    elif action_req.get("target"):
        raw_targets.append(str(action_req["target"]).strip())
    elif action_req.get("address"):
        raw_targets.append(str(action_req["address"]).strip())

    if not raw_targets:
        return False

    await cur.execute(
        """
        SELECT platform, platform_user_id
        FROM dalton_channel_identities
        WHERE user_id = %s
        """,
        (dalton_user_id,),
    )
    bound_by_plat: dict[str, str] = {
        str(r["platform"]).strip().lower(): str(r["platform_user_id"]).strip()
        for r in await cur.fetchall()
        if r.get("platform") and r.get("platform_user_id")
    }
    for target in raw_targets:
        if ":" not in target:
            return False
        plat, rest = target.split(":", 1)
        plat_l = plat.strip().lower()
        rest_s = rest.strip()
        if plat_l not in ALL_CHANNEL_PLATFORMS:
            return False
        if not cg_db._is_dm_address(plat_l, target):
            return False
        if rest_s == "@me":
            if plat_l not in bound_by_plat:
                return False
            continue
        puid = bound_by_plat.get(plat_l)
        if not puid:
            return False
        if rest_s not in (puid, f"user:{puid}"):
            return False
    return True


async def _classify_ptc_agent_workspace_tier(
    cur: Any,
    *,
    dalton_user_id: str,
    action_req: Mapping[str, Any],
    conn: Any = None,
) -> str:
    ws_id = normalize_uuid(action_req.get("workspace_id"))
    if not ws_id:
        return "L3"
    await cur.execute(
        """
        SELECT workspace_id::text AS workspace_id, user_id
        FROM workspaces
        WHERE workspace_id = %s::uuid
        """,
        (ws_id,),
    )
    ws_row = await cur.fetchone()
    if not ws_row:
        return "L3"
    if str(ws_row.get("user_id") or "") == str(dalton_user_id):
        return "L1"
    # Check colleague shared writable access (cos_access == "rw")
    explicit_access = str(action_req.get("cos_access") or "").strip().lower()
    if explicit_access == "rw":
        ok, _, _ = await cg_db._verify_workspace_write_permission(
            cur, ws_id, dalton_user_id, conn=conn
        )
        return "L2" if ok else "L3"
    try:
        access = await dalton_sharing.resolve_workspace_access(
            workspace_id=ws_id,
            user_id=dalton_user_id,
            conn=conn,
        )
        if getattr(access, "can_write", False) or (
            isinstance(access, Mapping) and access.get("cos_access") == "rw"
        ):
            return "L2"
    except Exception:
        ok, _, _ = await cg_db._verify_workspace_write_permission(
            cur, ws_id, dalton_user_id, conn=conn
        )
        if ok:
            return "L2"
    return "L3"


async def classify_interrupt_for_channel(
    cur: Any,
    *,
    interrupt_value: Mapping[str, Any],
    pending_interrupt_count: int,
    dalton_user_id: str,
    approval_snapshots: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> tuple[str, str, str | None]:
    """Return ``(risk_tier, action_type, reason)`` for a channel HITL interrupt.

    ``risk_tier`` is one of ``'L0'``, ``'L1'``, ``'L2'``, ``'L3'``, or ``'credit_pause'``.
    """
    if int(pending_interrupt_count) != 1:
        return "L3", "multi_interrupt", "multi_interrupt"

    outer_kind = str(interrupt_value.get("kind") or "").strip().lower()
    action_requests = interrupt_value.get("action_requests")
    first_action: Mapping[str, Any] | None = (
        action_requests[0]
        if isinstance(action_requests, list)
        and len(action_requests) == 1
        and isinstance(action_requests[0], Mapping)
        else None
    )
    first_type = str(first_action.get("type") or "").strip() if first_action else ""

    # 1. Outer kind check first
    if outer_kind == INTERRUPT_REASON_CREDIT_PAUSE or first_type == INTERRUPT_REASON_CREDIT_PAUSE:
        return "credit_pause", INTERRUPT_REASON_CREDIT_PAUSE, "credit_pause_text_or_web"
    if outer_kind in HIGH_RISK_OUTER_KINDS:
        return "L3", first_type or outer_kind, f"high_risk_kind:{outer_kind}"
    if outer_kind and outer_kind not in ALLOWED_OUTER_KINDS:
        return "L3", first_type or outer_kind, f"unsupported_kind:{outer_kind}"

    # 2. Single slot & inner action_requests[0].type allowlist
    if not isinstance(action_requests, list) or len(action_requests) != 1 or first_action is None:
        return "L3", first_type or outer_kind or "unknown", "multi_or_empty_action_requests"

    if first_type == "ask_user_question" and outer_kind in ("", "ask_user_question"):
        return "L0", "ask_user_question", None

    if first_type == "ptc_agent":
        tier = await _classify_ptc_agent_workspace_tier(
            cur, dalton_user_id=dalton_user_id, action_req=first_action, conn=conn
        )
        return tier, "ptc_agent", None if tier != "L3" else "workspace_not_writable"

    if first_type == "create_workspace":
        tool_call_id = first_action.get("tool_call_id")
        snap = (
            approval_snapshots.get(tool_call_id)
            if isinstance(approval_snapshots, Mapping) and isinstance(tool_call_id, str)
            else None
        )
        is_cloud = await is_verified_managed_cloud_create_workspace(
            first_action, snap, dalton_user_id, cur=cur, conn=conn
        )
        return ("L1", "create_workspace", None) if is_cloud else (
            "L3",
            "create_workspace",
            "not_verified_managed_cloud",
        )

    if first_type in ("dalton_notify_add_personal_target", "dalton_notify_test"):
        self_only = await _are_notify_targets_self_dm_only(
            cur, dalton_user_id=dalton_user_id, action_req=first_action
        )
        return ("L1" if self_only else "L2"), first_type, None

    if first_type in ("delete_thread", "dalton_notify_set_route"):
        return "L2", first_type, None

    return "L3", first_type or "unknown", "not_in_allowlist"


# ---------------------------------------------------------------------------
# Level 7b-0 Runtime Barrier Config (Path A vs Path B)
# ---------------------------------------------------------------------------


async def read_effective_interrupt_runtime_config_in_tx(
    cur: Any,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    """Lock ``dalton_channel_interrupt_runtime_config (singleton_id = 1)`` at Level 7b-0
    and return both raw DB values and effective (static AND DB) booleans."""
    lock_clause = "FOR UPDATE" if for_update else "FOR SHARE"
    await cur.execute(
        f"""
        SELECT singleton_id,
               interactive_actions_enabled,
               interrupt_ledger_enabled,
               bt2_link_recovery_enabled,
               config_revision,
               updated_at
        FROM dalton_channel_interrupt_runtime_config
        WHERE singleton_id = 1
        {lock_clause}
        """
    )
    row = await cur.fetchone()
    if row is None:
        await cur.execute(
            """
            INSERT INTO dalton_channel_interrupt_runtime_config (singleton_id)
            VALUES (1)
            ON CONFLICT (singleton_id) DO NOTHING
            """
        )
        await cur.execute(
            f"""
            SELECT singleton_id,
                   interactive_actions_enabled,
                   interrupt_ledger_enabled,
                   bt2_link_recovery_enabled,
                   config_revision,
                   updated_at
            FROM dalton_channel_interrupt_runtime_config
            WHERE singleton_id = 1
            {lock_clause}
            """
        )
        row = await cur.fetchone()
    row = dict(row) if row else {
        "interactive_actions_enabled": True,
        "interrupt_ledger_enabled": True,
        "bt2_link_recovery_enabled": True,
        "config_revision": 1,
        "updated_at": None,
    }
    static_interactive = is_channel_interactive_actions_enabled()
    static_ledger = is_channel_interrupt_ledger_enabled()
    static_bt2 = is_channel_bt2_link_recovery_enabled()

    db_interactive = bool(row["interactive_actions_enabled"])
    db_ledger = bool(row["interrupt_ledger_enabled"])
    db_bt2 = bool(row["bt2_link_recovery_enabled"])

    eff_ledger = static_ledger and db_ledger
    eff_interactive = static_interactive and db_interactive and eff_ledger
    eff_bt2 = static_bt2 and db_bt2

    return {
        "config_revision": int(row["config_revision"]),
        "db_interactive_actions_enabled": db_interactive,
        "db_interrupt_ledger_enabled": db_ledger,
        "db_bt2_link_recovery_enabled": db_bt2,
        "static_interactive_actions_enabled": static_interactive,
        "static_interrupt_ledger_enabled": static_ledger,
        "static_bt2_link_recovery_enabled": static_bt2,
        "interactive_actions_enabled": eff_interactive,
        "interrupt_ledger_enabled": eff_ledger,
        "bt2_link_recovery_enabled": eff_bt2,
        "updated_at": _iso(row.get("updated_at")),
    }


async def set_interrupt_runtime_config(
    *,
    interactive_actions_enabled: bool | None = None,
    interrupt_ledger_enabled: bool | None = None,
    bt2_link_recovery_enabled: bool | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Path A (Lock Order ⑫ branch 1): update ONLY ``interactive_actions_enabled`` on
    the single-row Level 7b-0 barrier table.

    ``interrupt_ledger_enabled`` and ``bt2_link_recovery_enabled`` are strictly
    rejected on Path A and may only be changed through ``enter_interrupt_drain_mode``
    (Path B / ``mode='enter_drain'``). Existing tokens and BT2 settings stay untouched.
    """
    if interrupt_ledger_enabled is not None or bt2_link_recovery_enabled is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "drain_mode_required",
                "message": "interrupt_ledger_enabled and bt2_link_recovery_enabled can only be modified via enter_drain (enter_interrupt_drain_mode).",
            },
        )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                current = await read_effective_interrupt_runtime_config_in_tx(
                    cur, for_update=True
                )
                new_interactive = (
                    current["db_interactive_actions_enabled"]
                    if interactive_actions_enabled is None
                    else bool(interactive_actions_enabled)
                )
                if not current["db_interrupt_ledger_enabled"] and new_interactive:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "invalid_runtime_config",
                            "message": "interactive_actions_enabled must be false when interrupt_ledger_enabled is false.",
                        },
                    )
                await cur.execute(
                    """
                    UPDATE dalton_channel_interrupt_runtime_config
                    SET interactive_actions_enabled = %s,
                        config_revision = config_revision + 1,
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    RETURNING config_revision,
                              interactive_actions_enabled,
                              interrupt_ledger_enabled,
                              bt2_link_recovery_enabled,
                              updated_at
                    """,
                    (new_interactive,),
                )
                upd = dict(await cur.fetchone())
                return {
                    "ok": True,
                    "mode": "config_update",
                    "config_revision": int(upd["config_revision"]),
                    "interactive_actions_enabled": bool(upd["interactive_actions_enabled"]),
                    "interrupt_ledger_enabled": bool(upd["interrupt_ledger_enabled"]),
                    "bt2_link_recovery_enabled": bool(upd["bt2_link_recovery_enabled"]),
                    "expired_bound_tokens": 0,
                    "expired_in_flight_tokens": 0,
                    "updated_at": _iso(upd.get("updated_at")),
                }


async def enter_interrupt_drain_mode(
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Path B (Lock Order ⑫ branch 2): three-switch production drain barrier.

    1. Acquires Level 7b-0 ``FOR UPDATE`` on ``dalton_channel_interrupt_runtime_config``
       (waiting for all cross-worker ``FOR SHARE`` token/resume/I-6 writers to finish)
       and atomically sets ``interactive_actions_enabled = FALSE``,
       ``interrupt_ledger_enabled = FALSE``, ``bt2_link_recovery_enabled = FALSE``,
       incrementing ``config_revision``.
    2. Acquires Level 7b-3 ``ORDER BY token_hash ASC FOR UPDATE`` on unconsumed
       persistent tokens (``direct`` / ``gate``):
       - ``bind_status = 'bound'`` -> ``consumed_via = 'expired', consumed_at = NOW()``,
         ``late_patch_status = 'pending'`` (or ``'skipped_platform_disabled'``).
       - ``bind_status IN ('pending_bind', 'send_in_flight', 'send_unknown')`` ->
         ``consumed_via = 'expired', consumed_at = NOW()`` (preserving ``bind_status``).
    """
    enabled_platforms = set(get_enabled_channel_platforms())
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await read_effective_interrupt_runtime_config_in_tx(cur, for_update=True)
                await cur.execute(
                    """
                    UPDATE dalton_channel_interrupt_runtime_config
                    SET interactive_actions_enabled = FALSE,
                        interrupt_ledger_enabled = FALSE,
                        bt2_link_recovery_enabled = FALSE,
                        config_revision = config_revision + 1,
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    RETURNING config_revision, updated_at
                    """
                )
                upd = dict(await cur.fetchone())

                await cur.execute(
                    """
                    SELECT token_hash, platform, bind_status, card_message_id
                    FROM dalton_channel_action_tokens
                    WHERE token_role IN ('direct', 'gate')
                      AND consumed_at IS NULL
                      AND bind_status IN ('bound', 'pending_bind', 'send_in_flight', 'send_unknown')
                    ORDER BY token_hash ASC
                    FOR UPDATE
                    """
                )
                tokens = [dict(r) for r in await cur.fetchall()]
                expired_bound = 0
                expired_pending = 0
                for tok in tokens:
                    th = str(tok["token_hash"])
                    bs = str(tok["bind_status"])
                    plat = str(tok["platform"]).strip().lower()
                    if bs == "bound" and tok.get("card_message_id"):
                        lp_status = (
                            "pending"
                            if plat in enabled_platforms
                            else "skipped_platform_disabled"
                        )
                        await cur.execute(
                            """
                            UPDATE dalton_channel_action_tokens
                            SET consumed_via = 'expired',
                                consumed_at = NOW(),
                                late_patch_status = COALESCE(late_patch_status, %s),
                                late_patch_attempts = CASE
                                    WHEN late_patch_status IS NULL THEN 0
                                    ELSE late_patch_attempts
                                END,
                                late_patch_next_retry_at = CASE
                                    WHEN late_patch_status IS NULL AND %s = 'pending' THEN NOW()
                                    ELSE late_patch_next_retry_at
                                END
                            WHERE token_hash = %s
                            """,
                            (lp_status, lp_status, th),
                        )
                        expired_bound += 1
                    else:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_action_tokens
                            SET consumed_via = 'expired',
                                consumed_at = NOW()
                            WHERE token_hash = %s
                            """,
                            (th,),
                        )
                        expired_pending += 1

                return {
                    "ok": True,
                    "mode": "drain",
                    "config_revision": int(upd["config_revision"]),
                    "interactive_actions_enabled": False,
                    "interrupt_ledger_enabled": False,
                    "bt2_link_recovery_enabled": False,
                    "expired_bound_tokens": expired_bound,
                    "expired_in_flight_tokens": expired_pending,
                    "updated_at": _iso(upd.get("updated_at")),
                }


# ---------------------------------------------------------------------------
# Shared 3-Branch Card Binding Helper (§3.2 #2)
# ---------------------------------------------------------------------------


def _merge_card_targets(
    existing_targets: Any,
    new_cards: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate card_targets by token_hash while preserving existing terminal statuses."""
    by_hash: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    if isinstance(existing_targets, list):
        for elem in existing_targets:
            if not isinstance(elem, Mapping):
                continue
            th = str(elem.get("token_hash") or "").strip()
            if not th:
                continue
            by_hash[th] = dict(elem)
            order.append(th)
    for card in new_cards:
        th = str(card.get("token_hash") or "").strip()
        if not th:
            continue
        prev = by_hash.get(th)
        if prev is not None and str(prev.get("patch_status") or "") in CARD_TERMINAL_PATCH_STATUSES:
            continue
        if prev is None:
            order.append(th)
        by_hash[th] = dict(card)
    return [by_hash[th] for th in order]


def _append_unique_str(existing: Any, item: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(existing, list):
        for val in existing:
            s = str(val).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    if item and item not in seen:
        out.append(item)
    return out


async def _apply_confirm_bound_in_tx(
    cur: Any,
    *,
    token_row: dict[str, Any],
    card_channel_id: str | None,
    card_message_id: str,
    resume_row: dict[str, Any] | None,
    ledger_enabled: bool,
    enabled_platforms: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Unified 3-branch card binding transaction helper (§3.2 #2).

    Shared by ``bind-card(confirm_bound)``, ``inspect-gate`` self-heal, and
    ``consume`` self-heal. Caller MUST already hold:
    ``0b-2 -> 7b-0 (FOR SHARE) -> 7b-1 -> 7b-2 (interrupt_resumes FOR UPDATE) -> 7b-3 (action_tokens FOR UPDATE)``.
    """
    token_hash = str(token_row["token_hash"])
    token_role = str(token_row["token_role"])
    platform = str(token_row["platform"]).strip().lower()
    plat_enabled = platform in enabled_platforms
    eff_channel_id = (
        str(card_channel_id).strip()
        if card_channel_id is not None and str(card_channel_id).strip()
        else token_row.get("card_channel_id")
    )
    eff_message_id = str(card_message_id).strip()

    if token_role == "ephemeral_confirm":
        await cur.execute(
            """
            UPDATE dalton_channel_action_tokens
            SET card_channel_id = %s,
                card_message_id = %s
            WHERE token_hash = %s
            """,
            (eff_channel_id, eff_message_id, token_hash),
        )
        token_row["card_channel_id"] = eff_channel_id
        token_row["card_message_id"] = eff_message_id
        return {
            "branch": "ephemeral",
            "bind_status": "ephemeral",
            "reopen_refused": False,
            "should_patch_terminal_immediately": False,
            "late_patch_queued": False,
        }

    # Persistent card (direct / gate): transition bind_status -> 'bound'
    # Branch 1: ledger enabled AND no resume_row
    if ledger_enabled and resume_row is None:
        await cur.execute(
            """
            UPDATE dalton_channel_action_tokens
            SET bind_status = 'bound',
                card_channel_id = %s,
                card_message_id = %s,
                late_patch_status = NULL
            WHERE token_hash = %s
            """,
            (eff_channel_id, eff_message_id, token_hash),
        )
        token_row["bind_status"] = "bound"
        token_row["card_channel_id"] = eff_channel_id
        token_row["card_message_id"] = eff_message_id
        token_row["late_patch_status"] = None
        return {
            "branch": "branch_1_open",
            "bind_status": "bound",
            "reopen_refused": False,
            "should_patch_terminal_immediately": False,
            "late_patch_queued": False,
        }

    # Branch 2 (T-6): ledger enabled AND unconsumed resume_row (pending_dispatch or admitted without run_consumed_at)
    if (
        ledger_enabled
        and resume_row is not None
        and resume_row.get("run_consumed_at") is None
        and str(resume_row.get("resume_status") or "") in ("pending_dispatch", "admitted")
    ):
        consumed_via = _token_consumed_via_for_resume(resume_row, token_hash)
        await cur.execute(
            """
            UPDATE dalton_channel_action_tokens
            SET bind_status = 'bound',
                card_channel_id = %s,
                card_message_id = %s,
                consumed_via = %s,
                consumed_at = COALESCE(consumed_at, NOW())
            WHERE token_hash = %s
            RETURNING consumed_at
            """,
            (eff_channel_id, eff_message_id, consumed_via, token_hash),
        )
        t_upd = await cur.fetchone()
        token_row["bind_status"] = "bound"
        token_row["card_channel_id"] = eff_channel_id
        token_row["card_message_id"] = eff_message_id
        token_row["consumed_via"] = consumed_via
        token_row["consumed_at"] = (t_upd or {}).get("consumed_at") or datetime.now(timezone.utc)

        patch_status = "pending" if plat_enabled else "skipped_platform_disabled"
        new_superseded = _append_unique_str(
            resume_row.get("superseded_token_hashes"), token_hash
        )
        new_targets = _merge_card_targets(
            resume_row.get("card_targets"),
            [
                {
                    "token_hash": token_hash,
                    "platform": platform,
                    "card_channel_id": eff_channel_id,
                    "card_message_id": eff_message_id,
                    "patch_status": patch_status,
                    "attempt_count": 0,
                }
            ],
        )
        if patch_status == "pending":
            await cur.execute(
                """
                UPDATE dalton_channel_interrupt_resumes
                SET superseded_token_hashes = %s,
                    card_targets = %s,
                    requires_gateway_ack = TRUE,
                    delivery_settled = FALSE,
                    channel_ack_at = NULL,
                    next_retry_at = NOW(),
                    updated_at = NOW()
                WHERE thread_id = %s::uuid
                  AND run_id = %s
                  AND interrupt_id = %s
                """,
                (
                    Jsonb(new_superseded),
                    Jsonb(new_targets),
                    str(resume_row["thread_id"]),
                    str(resume_row["run_id"]),
                    str(resume_row["interrupt_id"]),
                ),
            )
            resume_row["requires_gateway_ack"] = True
            resume_row["delivery_settled"] = False
            resume_row["channel_ack_at"] = None
        else:
            await cur.execute(
                """
                UPDATE dalton_channel_interrupt_resumes
                SET superseded_token_hashes = %s,
                    card_targets = %s,
                    updated_at = NOW()
                WHERE thread_id = %s::uuid
                  AND run_id = %s
                  AND interrupt_id = %s
                """,
                (
                    Jsonb(new_superseded),
                    Jsonb(new_targets),
                    str(resume_row["thread_id"]),
                    str(resume_row["run_id"]),
                    str(resume_row["interrupt_id"]),
                ),
            )
        resume_row["superseded_token_hashes"] = new_superseded
        resume_row["card_targets"] = new_targets
        return {
            "branch": "branch_2_unconsumed_resume",
            "bind_status": "bound",
            "reopen_refused": False,
            "should_patch_terminal_immediately": False,
            "late_patch_queued": False,
            "resume_status": str(resume_row["resume_status"]),
            "settled_via": str(resume_row["settled_via"]),
        }

    # Branch 3 (T-7): drain mode (not ledger_enabled) OR resume already consumed / terminal_failed
    consumed_via = (
        _token_consumed_via_for_resume(resume_row, token_hash)
        if resume_row is not None
        else "expired"
    )
    lp_status = "pending" if plat_enabled else "skipped_platform_disabled"
    await cur.execute(
        """
        UPDATE dalton_channel_action_tokens
        SET bind_status = 'bound',
            card_channel_id = %s,
            card_message_id = %s,
            consumed_via = %s,
            consumed_at = COALESCE(consumed_at, NOW()),
            late_patch_status = %s,
            late_patch_attempts = 0,
            late_patch_next_retry_at = CASE WHEN %s = 'pending' THEN NOW() ELSE NULL END
        WHERE token_hash = %s
        RETURNING consumed_at
        """,
        (eff_channel_id, eff_message_id, consumed_via, lp_status, lp_status, token_hash),
    )
    t_upd = await cur.fetchone()
    token_row["bind_status"] = "bound"
    token_row["card_channel_id"] = eff_channel_id
    token_row["card_message_id"] = eff_message_id
    token_row["consumed_via"] = consumed_via
    token_row["consumed_at"] = (t_upd or {}).get("consumed_at") or datetime.now(timezone.utc)
    token_row["late_patch_status"] = lp_status
    return {
        "branch": "branch_3_late_patch",
        "bind_status": "bound",
        "reopen_refused": True,
        "should_patch_terminal_immediately": plat_enabled,
        "late_patch_queued": plat_enabled,
        "late_patch_status": lp_status,
        "resume_status": str(resume_row["resume_status"]) if resume_row else None,
        "settled_via": str(resume_row["settled_via"]) if resume_row else None,
    }


# ---------------------------------------------------------------------------
# Shared Lock-Order Helpers (Levels 0b-1, 3a, 7a, 7b-1, 7b-2, 7b-3)
# ---------------------------------------------------------------------------


async def _acquire_inbound_advisory_locks_in_tx(
    cur: Any,
    inbound_keys: Sequence[tuple[str, str]],
) -> list[tuple[str, str]]:
    unique_sorted = sorted({(str(p).strip().lower(), str(m).strip()) for p, m in inbound_keys if p and m})
    for plat, mid in unique_sorted:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"dalton_inbound:{plat}:{mid}",),
        )
    return unique_sorted


async def _acquire_weixin_account_key_share_in_tx(
    cur: Any,
    account_ids: Sequence[str | None],
) -> set[str]:
    clean_ids = sorted({str(normalize_uuid(a)) for a in account_ids if a and normalize_uuid(a)})
    locked: set[str] = set()
    if not clean_ids:
        return locked
    await cur.execute(
        """
        SELECT account_id::text AS account_id
        FROM dalton_channel_weixin_accounts
        WHERE account_id = ANY(%s::uuid[])
        ORDER BY account_id ASC
        FOR KEY SHARE
        """,
        (clean_ids,),
    )
    for row in await cur.fetchall():
        if row.get("account_id"):
            locked.add(str(row["account_id"]))
    return locked


async def _lock_inbound_events_and_verify_weixin_set_in_tx(
    cur: Any,
    inbound_keys: Sequence[tuple[str, str]],
    locked_weixin_account_ids: set[str],
    *,
    op_name: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Level 7a step 1: lock inbound_events in (platform, message_id) ASC order and
    IMMEDIATELY verify every non-null weixin_account_id is in the Level 3a lock set
    BEFORE acquiring any thread_watches or downstream lock (R-4 / T-8)."""
    unique_sorted = sorted({(str(p).strip().lower(), str(m).strip()) for p, m in inbound_keys if p and m})
    locked_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for plat, mid in unique_sorted:
        await cur.execute(
            """
            SELECT *, weixin_account_id::text AS weixin_account_id_text
            FROM dalton_channel_inbound_events
            WHERE platform = %s AND message_id = %s
            FOR UPDATE
            """,
            (plat, mid),
        )
        row = await cur.fetchone()
        if row is not None:
            rdict = dict(row)
            wx_aid = rdict.pop("weixin_account_id_text", None)
            if wx_aid:
                rdict["weixin_account_id"] = str(wx_aid)
                if str(wx_aid) not in locked_weixin_account_ids:
                    raise _ResumeInboundLockSetChanged(
                        f"{op_name}: inbound ({plat}, {mid}) references unlocked weixin_account_id={wx_aid}"
                    )
            locked_rows[(plat, mid)] = rdict
    if _after_7a_inbound_lock_hook is not None:
        await _after_7a_inbound_lock_hook(op_name)
    return locked_rows


async def _lock_thread_watch_and_verify_weixin_set_in_tx(
    cur: Any,
    thread_id: str,
    locked_weixin_account_ids: set[str],
    *,
    op_name: str,
) -> dict[str, Any] | None:
    """Level 7a step 2: lock thread_watch and immediately verify its weixin_account_id is in 3a."""
    norm_tid = normalize_uuid(thread_id)
    if not norm_tid:
        return None
    await cur.execute(
        """
        SELECT *, weixin_account_id::text AS weixin_account_id_text
        FROM dalton_channel_thread_watches
        WHERE thread_id = %s::uuid
        FOR UPDATE
        """,
        (norm_tid,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    rdict = dict(row)
    wx_aid = rdict.pop("weixin_account_id_text", None)
    if wx_aid:
        rdict["weixin_account_id"] = str(wx_aid)
        if str(wx_aid) not in locked_weixin_account_ids:
            raise _ResumeInboundLockSetChanged(
                f"{op_name}: thread_watch {norm_tid} references unlocked weixin_account_id={wx_aid}"
            )
    return rdict


async def _acquire_hitl_resume_lock_in_tx(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
) -> None:
    """Level 7b-1: interrupt-scoped advisory transaction lock."""
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"dalton_hitl_resume:{thread_id}:{run_id}:{interrupt_id}",),
    )


async def _lock_interrupt_resume_in_tx(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
) -> dict[str, Any] | None:
    """Level 7b-2: lock dalton_channel_interrupt_resumes row FOR UPDATE."""
    await cur.execute(
        """
        SELECT *,
               thread_id::text AS thread_id_text,
               claim_id::text AS claim_id_text,
               request_key::text AS request_key_text,
               weixin_account_id::text AS weixin_account_id_text
        FROM dalton_channel_interrupt_resumes
        WHERE thread_id = %s::uuid
          AND run_id = %s
          AND interrupt_id = %s
        FOR UPDATE
        """,
        (thread_id, run_id, interrupt_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    rdict = dict(row)
    rdict["thread_id"] = rdict.pop("thread_id_text")
    rdict["claim_id"] = rdict.pop("claim_id_text")
    rdict["request_key"] = rdict.pop("request_key_text")
    wx_aid = rdict.pop("weixin_account_id_text", None)
    rdict["weixin_account_id"] = str(wx_aid) if wx_aid else None
    return rdict


async def _lock_interrupt_tokens_in_tx(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
) -> list[dict[str, Any]]:
    """Level 7b-3: lock all tokens for the interrupt in token_hash ASC order."""
    await cur.execute(
        """
        SELECT *, thread_id::text AS thread_id_text
        FROM dalton_channel_action_tokens
        WHERE thread_id = %s::uuid
          AND run_id = %s
          AND interrupt_id = %s
        ORDER BY token_hash ASC
        FOR UPDATE
        """,
        (thread_id, run_id, interrupt_id),
    )
    out: list[dict[str, Any]] = []
    for r in await cur.fetchall():
        d = dict(r)
        d["thread_id"] = d.pop("thread_id_text")
        out.append(d)
    return out


async def _collect_bound_card_targets_in_tx(
    tokens: Sequence[Mapping[str, Any]],
    *,
    existing_targets: Any = None,
    enabled_platforms: set[str] | frozenset[str],
) -> list[dict[str, Any]]:
    """Build card_targets from bound persistent cards while preserving existing terminal states."""
    new_cards: list[dict[str, Any]] = []
    for tok in tokens:
        if str(tok.get("token_role") or "") not in ("direct", "gate"):
            continue
        if str(tok.get("bind_status") or "") != "bound" or not tok.get("card_message_id"):
            continue
        plat = str(tok.get("platform") or "").strip().lower()
        new_cards.append(
            {
                "token_hash": str(tok["token_hash"]),
                "platform": plat,
                "card_channel_id": tok.get("card_channel_id"),
                "card_message_id": str(tok["card_message_id"]),
                "patch_status": "pending" if plat in enabled_platforms else "skipped_platform_disabled",
                "attempt_count": 0,
            }
        )
    return _merge_card_targets(existing_targets, new_cards)


async def _has_active_or_advanced_run_for_key(
    cur: Any,
    *,
    thread_id: str,
    request_key: str | None = None,
    admitted_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Query conversation_responses for any run matching request_key or admitted_run_id."""
    if admitted_run_id:
        norm_admitted = normalize_uuid(admitted_run_id)
        if norm_admitted:
            await cur.execute(
                """
                SELECT conversation_response_id, status, metadata
                FROM conversation_responses
                WHERE conversation_response_id = %s::uuid
                LIMIT 1
                """,
                (norm_admitted,),
            )
            r = await cur.fetchone()
            if r:
                return dict(r)
    if request_key:
        norm_rk = normalize_uuid(request_key)
        if norm_rk:
            await cur.execute(
                """
                SELECT conversation_response_id, status, metadata
                FROM conversation_responses
                WHERE conversation_thread_id = %s::uuid
                  AND request_key = %s::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (thread_id, norm_rk),
            )
            r = await cur.fetchone()
            if r:
                return dict(r)
    return None


async def _has_in_progress_run_on_thread(cur: Any, thread_id: str) -> bool:
    await cur.execute(
        """
        SELECT 1
        FROM conversation_responses
        WHERE conversation_thread_id = %s::uuid
          AND status = 'in_progress'
        LIMIT 1
        """,
        (thread_id,),
    )
    return (await cur.fetchone()) is not None


async def _rearm_pending_interrupt_in_tx(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    platform: str | None,
    origin_scope_key: str | None,
    dalton_user_id: str | None,
    session_generation: int | None,
    pending_record: Mapping[str, Any] | None,
) -> None:
    """Idempotently restore pending_interrupt on session_generations & thread_watches when still paused."""
    if not pending_record:
        return
    val = pending_record.get("value") if isinstance(pending_record.get("value"), Mapping) else {}
    action_requests = val.get("action_requests") if isinstance(val.get("action_requests"), list) else []
    summary = {
        "interrupt_id": interrupt_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "kind": val.get("kind"),
        "action_requests": action_requests,
    }
    if platform and origin_scope_key and dalton_user_id and session_generation is not None:
        await cur.execute(
            """
            UPDATE dalton_channel_session_generations
            SET pending_interrupt = %s,
                updated_at = NOW()
            WHERE platform = %s
              AND scope_key = %s
              AND dalton_user_id = %s
              AND generation = %s
            """,
            (Jsonb(summary), platform, origin_scope_key, dalton_user_id, int(session_generation)),
        )
    await cur.execute(
        """
        UPDATE dalton_channel_thread_watches
        SET pending_interrupt = %s,
            updated_at = NOW()
        WHERE thread_id = %s::uuid
          AND (%s::integer IS NULL OR generation = %s::integer)
        """,
        (
            Jsonb(summary),
            thread_id,
            int(session_generation) if session_generation is not None else None,
            int(session_generation) if session_generation is not None else None,
        ),
    )


async def _preview_interrupt_context(
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    extra_inbound_key: tuple[str, str] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Unlocked pre-read snapshot of resume row, watch row, and Weixin account IDs."""
    preview: dict[str, Any] = {
        "preview_claim_id": None,
        "preview_resume": None,
        "inbound_keys": [],
        "weixin_account_ids": set(),
    }
    inb_keys: set[tuple[str, str]] = set()
    if extra_inbound_key and extra_inbound_key[0] and extra_inbound_key[1]:
        inb_keys.add((extra_inbound_key[0].strip().lower(), extra_inbound_key[1].strip()))

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT claim_id::text AS claim_id,
                       platform,
                       inbound_message_id,
                       settled_via,
                       weixin_account_id::text AS weixin_account_id,
                       origin_scope_key,
                       session_generation,
                       settled_by_user_id
                FROM dalton_channel_interrupt_resumes
                WHERE thread_id = %s::uuid
                  AND run_id = %s
                  AND interrupt_id = %s
                """,
                (thread_id, run_id, interrupt_id),
            )
            r_row = await cur.fetchone()
            if r_row:
                r_dict = dict(r_row)
                preview["preview_resume"] = r_dict
                preview["preview_claim_id"] = str(r_dict["claim_id"])
                if r_dict.get("platform") and r_dict.get("inbound_message_id"):
                    inb_keys.add(
                        (str(r_dict["platform"]).strip().lower(), str(r_dict["inbound_message_id"]).strip())
                    )
                if r_dict.get("weixin_account_id"):
                    preview["weixin_account_ids"].add(str(r_dict["weixin_account_id"]))

            await cur.execute(
                """
                SELECT platform, weixin_account_id::text AS weixin_account_id
                FROM dalton_channel_thread_watches
                WHERE thread_id = %s::uuid
                """,
                (thread_id,),
            )
            w_row = await cur.fetchone()
            if w_row and w_row.get("weixin_account_id"):
                preview["weixin_account_ids"].add(str(w_row["weixin_account_id"]))

            for plat, mid in sorted(inb_keys):
                await cur.execute(
                    """
                    SELECT weixin_account_id::text AS weixin_account_id,
                           dalton_user_id
                    FROM dalton_channel_inbound_events
                    WHERE platform = %s AND message_id = %s
                    """,
                    (plat, mid),
                )
                i_row = await cur.fetchone()
                if i_row and i_row.get("weixin_account_id"):
                    preview["weixin_account_ids"].add(str(i_row["weixin_account_id"]))
                elif i_row and plat == "weixin" and i_row.get("dalton_user_id"):
                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                        """,
                        (str(i_row["dalton_user_id"]),),
                    )
                    for a_row in await cur.fetchall():
                        if a_row.get("account_id"):
                            preview["weixin_account_ids"].add(str(a_row["account_id"]))

    preview["inbound_keys"] = sorted(inb_keys)
    return preview


# ---------------------------------------------------------------------------
# 1. Mint Action Token (POST /internal/action-tokens/mint)
# ---------------------------------------------------------------------------


async def mint_channel_action_token(
    *,
    platform: str,
    dalton_user_id: str,
    initiator_platform_user_id: str,
    origin_chat_type: str,
    origin_chat_address: str,
    origin_scope_key: str,
    session_generation: int,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    interrupt_value: Mapping[str, Any],
    pending_interrupt_count: int = 1,
    card_chat_type: str | None = None,
    card_delivery_address: str | None = None,
    prompt_summary: Mapping[str, Any] | None = None,
    approval_snapshots: Mapping[str, Any] | None = None,
    gateway_epoch: int | str | None = None,
    require_gateway_epoch: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Classify an interrupt (L0–L3 / credit_pause) and mint an opaque action token when eligible."""
    cleaned_plat = require_known_channel_platform(platform, allowed=CARD_PLATFORMS)
    reject_company_system_auth(dalton_user_id)
    norm_tid = normalize_uuid(thread_id)
    if not norm_tid or not str(run_id or "").strip() or not str(interrupt_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_interrupt_ref", "message": "thread_id, run_id, and interrupt_id are required."},
        )
    cleaned_origin_ct = str(origin_chat_type or "").strip().lower()
    if cleaned_origin_ct not in ("dm", "group"):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_chat_type", "message": "origin_chat_type must be 'dm' or 'group'."},
        )
    parsed_epoch = _parse_epoch_strict(gateway_epoch, required=require_gateway_epoch)

    # Load live approval_snapshots outside the write transaction if caller did not supply them
    eff_snapshots = approval_snapshots
    if eff_snapshots is None:
        try:
            _, eff_snapshots = await load_live_pending_interrupts(norm_tid)
        except Exception:
            eff_snapshots = {}

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SET LOCAL lock_timeout = '2s'")
                await cur.execute("SET LOCAL statement_timeout = '2s'")

                # Level 0b-2: gateway_lease FOR SHARE
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    parsed_epoch,
                    required=require_gateway_epoch,
                    platform=cleaned_plat,
                    enforce_post_lease_for_non_weixin=False,
                )

                # Level 7b-0: runtime config FOR SHARE
                if _before_7b0_lock_hook is not None:
                    await _before_7b0_lock_hook("mint")
                rt_cfg = await read_effective_interrupt_runtime_config_in_tx(cur, for_update=False)
                if not rt_cfg["interactive_actions_enabled"] or not rt_cfg["interrupt_ledger_enabled"]:
                    return {
                        "ok": True,
                        "interactive_enabled": False,
                        "effective_tier": "text_or_web_fallback",
                        "risk_tier": "L3",
                        "token": None,
                        "config_revision": rt_cfg["config_revision"],
                    }

                tier, action_type, reason = await classify_interrupt_for_channel(
                    cur,
                    interrupt_value=interrupt_value,
                    pending_interrupt_count=pending_interrupt_count,
                    dalton_user_id=dalton_user_id,
                    approval_snapshots=eff_snapshots,
                    conn=owned_conn,
                )
                if tier == "credit_pause":
                    return {
                        "ok": True,
                        "interactive_enabled": True,
                        "effective_tier": "credit_pause",
                        "risk_tier": "credit_pause",
                        "action_type": action_type,
                        "reason": reason,
                        "token": None,
                    }
                if tier == "L3":
                    return {
                        "ok": True,
                        "interactive_enabled": True,
                        "effective_tier": "L3",
                        "risk_tier": "L3",
                        "action_type": action_type,
                        "reason": reason,
                        "token": None,
                    }

                action_req = dict((interrupt_value.get("action_requests") or [{}])[0])
                args_hash = compute_args_sha256(action_req)
                server_confirm_token: str | None = None
                if action_type in NOTIFY_CONFIRM_TOKEN_ACTIONS:
                    raw_ct = action_req.get("confirm_token")
                    if isinstance(raw_ct, str) and raw_ct.strip():
                        server_confirm_token = raw_ct.strip()

                options_snapshot: Any = None
                if tier == "L0":
                    opts = action_req.get("options") or action_req.get("choices") or interrupt_value.get("options")
                    if isinstance(opts, list):
                        options_snapshot = opts

                # Determine token_role and card placement
                if cleaned_origin_ct == "group" and tier in ("L1", "L2"):
                    if cleaned_plat == "discord":
                        token_role = "gate"
                        eff_card_ct = "group"
                        eff_card_addr = str(card_delivery_address or origin_chat_address).strip()
                    else:
                        # Feishu group L1/L2 -> direct confirm card goes to initiator's Feishu DM
                        token_role = "direct"
                        eff_card_ct = "dm"
                        eff_card_addr = str(
                            card_delivery_address or f"feishu:user:{initiator_platform_user_id}"
                        ).strip()
                else:
                    token_role = "direct"
                    eff_card_ct = str(card_chat_type or cleaned_origin_ct).strip().lower()
                    eff_card_addr = str(
                        card_delivery_address
                        or (
                            f"feishu:user:{initiator_platform_user_id}"
                            if cleaned_plat == "feishu" and eff_card_ct == "dm"
                            else origin_chat_address
                        )
                    ).strip()

                # Lookup known Feishu DM p2p_chat_id for reconciliation hint
                p2p_chat_id: str | None = None
                if cleaned_plat == "feishu" and eff_card_ct == "dm":
                    await cur.execute(
                        """
                        SELECT raw_payload->>'chat_id' AS p2p_chat_id
                        FROM dalton_channel_inbound_events
                        WHERE platform = 'feishu'
                          AND dalton_user_id = %s
                          AND chat_type = 'dm'
                          AND raw_payload->>'chat_id' IS NOT NULL
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (dalton_user_id,),
                    )
                    p2p_row = await cur.fetchone()
                    if p2p_row and p2p_row.get("p2p_chat_id"):
                        p2p_chat_id = str(p2p_row["p2p_chat_id"]).strip() or None

                # Level 7b-1 -> 7b-2 -> 7b-3
                await _acquire_hitl_resume_lock_in_tx(
                    cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                )
                nonce = generate_approval_nonce()
                token_hash = hash_approval_nonce(nonce)
                ttl_seconds = RISK_TIER_TTL_SECONDS[tier]

                await cur.execute(
                    """
                    INSERT INTO dalton_channel_action_tokens (
                        token_hash, token_role, parent_token_hash, platform,
                        dalton_user_id, initiator_platform_user_id,
                        origin_chat_type, origin_chat_address, origin_scope_key,
                        session_generation, card_chat_type, card_delivery_address,
                        bind_status, thread_id, run_id, interrupt_id,
                        risk_tier, action_type, args_sha256,
                        server_confirm_token, options_snapshot, prompt_summary,
                        expires_at
                    )
                    VALUES (
                        %s, %s, NULL, %s,
                        %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        'pending_bind', %s::uuid, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        NOW() + (%s || ' seconds')::interval
                    )
                    RETURNING expires_at
                    """,
                    (
                        token_hash,
                        token_role,
                        cleaned_plat,
                        dalton_user_id,
                        initiator_platform_user_id,
                        cleaned_origin_ct,
                        origin_chat_address,
                        origin_scope_key,
                        int(session_generation),
                        eff_card_ct,
                        eff_card_addr,
                        norm_tid,
                        str(run_id),
                        str(interrupt_id),
                        tier,
                        action_type,
                        args_hash,
                        server_confirm_token,
                        Jsonb(options_snapshot) if options_snapshot is not None else None,
                        Jsonb(dict(prompt_summary or {})),
                        str(ttl_seconds),
                    ),
                )
                ins = await cur.fetchone()
                return {
                    "ok": True,
                    "interactive_enabled": True,
                    "effective_tier": tier,
                    "risk_tier": tier,
                    "action_type": action_type,
                    "token_role": token_role,
                    "token_hash": token_hash,
                    "approval_nonce": nonce,
                    "token": nonce,
                    "card_chat_type": eff_card_ct,
                    "card_delivery_address": eff_card_addr,
                    "p2p_chat_id": p2p_chat_id,
                    "client_msg_uuid": f"dlt-card-{token_hash[:32]}",
                    "expires_at": _iso((ins or {}).get("expires_at")),
                    "ttl_seconds": ttl_seconds,
                }


# ---------------------------------------------------------------------------
# 2. Bind Card (POST /internal/action-tokens/bind-card)
# ---------------------------------------------------------------------------


async def bind_channel_action_card(
    *,
    approval_nonce: str | None = None,
    token_hash: str | None = None,
    phase: str = "confirm_bound",
    platform: str | None = None,
    card_channel_id: str | None = None,
    card_message_id: str | None = None,
    send_reconcile_meta: Mapping[str, Any] | None = None,
    gateway_epoch: int | str | None = None,
    require_gateway_epoch: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Manage card send lifecycle: ``prepare_send`` / ``confirm_bound`` / ``send_failed`` / ``send_unknown``."""
    cleaned_phase = str(phase or "confirm_bound").strip().lower()
    if cleaned_phase not in ("prepare_send", "confirm_bound", "send_failed", "send_unknown"):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_phase", "message": f"Unsupported bind-card phase: {phase!r}"},
        )
    eff_hash = (
        str(token_hash).strip().lower()
        if token_hash and str(token_hash).strip()
        else (hash_approval_nonce(approval_nonce) if approval_nonce and str(approval_nonce).strip() else "")
    )
    if not eff_hash:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_token", "message": "approval_nonce or token_hash is required."},
        )

    # Read-only lookup of authoritative row.platform BEFORE parsing epoch
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT token_hash, platform, thread_id::text AS thread_id, run_id, interrupt_id
                FROM dalton_channel_action_tokens
                WHERE token_hash = %s
                """,
                (eff_hash,),
            )
            pre = await cur.fetchone()
    if not pre:
        raise HTTPException(
            status_code=404,
            detail={"code": "token_not_found", "message": "Action token not found."},
        )
    auth_platform = require_known_channel_platform(
        pre["platform"], allowed=CARD_PLATFORMS, invalid_status=409
    )
    if platform is not None and str(platform).strip().lower() != auth_platform:
        raise HTTPException(
            status_code=400,
            detail={"code": "platform_mismatch", "message": "Request platform does not match token platform."},
        )

    parsed_epoch = _parse_epoch_strict(gateway_epoch, required=require_gateway_epoch)
    enabled_platforms = set(get_enabled_channel_platforms())

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SET LOCAL lock_timeout = '2s'")
                await cur.execute("SET LOCAL statement_timeout = '2s'")

                # Level 0b-2: gateway_lease FOR SHARE
                active_epoch = await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    parsed_epoch,
                    required=require_gateway_epoch,
                    platform=auth_platform,
                    enforce_post_lease_for_non_weixin=False,
                )

                # Level 7b-0: runtime config FOR SHARE
                if _before_7b0_lock_hook is not None:
                    await _before_7b0_lock_hook(f"bind_card:{cleaned_phase}")
                rt_cfg = await read_effective_interrupt_runtime_config_in_tx(cur, for_update=False)

                # confirm_bound needs 7b-1 -> 7b-2 -> 7b-3; others only need 7b-3
                resume_row: dict[str, Any] | None = None
                if cleaned_phase == "confirm_bound":
                    await _acquire_hitl_resume_lock_in_tx(
                        cur,
                        thread_id=str(pre["thread_id"]),
                        run_id=str(pre["run_id"]),
                        interrupt_id=str(pre["interrupt_id"]),
                    )
                    resume_row = await _lock_interrupt_resume_in_tx(
                        cur,
                        thread_id=str(pre["thread_id"]),
                        run_id=str(pre["run_id"]),
                        interrupt_id=str(pre["interrupt_id"]),
                    )

                await cur.execute(
                    """
                    SELECT *, thread_id::text AS thread_id_text
                    FROM dalton_channel_action_tokens
                    WHERE token_hash = %s
                    FOR UPDATE
                    """,
                    (eff_hash,),
                )
                tok = await cur.fetchone()
                if not tok:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "token_not_found", "message": "Action token not found."},
                    )
                tok_dict = dict(tok)
                tok_dict["thread_id"] = tok_dict.pop("thread_id_text")
                cur_bs = str(tok_dict["bind_status"])

                if cleaned_phase == "prepare_send":
                    # Idempotent re-entry if already send_in_flight (even during drain!)
                    if cur_bs == "send_in_flight":
                        return {
                            "ok": True,
                            "idempotent": True,
                            "abort_card_send": False,
                            "bind_status": "send_in_flight",
                            "token_hash": eff_hash,
                            "send_attempt_started_at": _iso(tok_dict.get("send_attempt_started_at")),
                            "send_gateway_epoch": tok_dict.get("send_gateway_epoch"),
                            "send_reconcile_meta": tok_dict.get("send_reconcile_meta"),
                        }
                    if cur_bs == "bound":
                        return {
                            "ok": True,
                            "idempotent": True,
                            "abort_card_send": True,
                            "bind_status": "bound",
                            "token_hash": eff_hash,
                        }
                    if cur_bs != "pending_bind":
                        return {
                            "ok": False,
                            "abort_card_send": True,
                            "bind_status": cur_bs,
                            "token_hash": eff_hash,
                        }
                    # Check switches, expiry, and consumption
                    db_now = await cg_db._fetch_db_now(cur)
                    exp_dt = _coerce_utc(tok_dict.get("expires_at"))
                    is_expired = exp_dt is not None and exp_dt <= db_now
                    if (
                        not rt_cfg["interactive_actions_enabled"]
                        or not rt_cfg["interrupt_ledger_enabled"]
                        or tok_dict.get("consumed_at") is not None
                        or is_expired
                    ):
                        await cur.execute(
                            """
                            UPDATE dalton_channel_action_tokens
                            SET bind_status = 'send_failed',
                                consumed_via = COALESCE(consumed_via, 'expired'),
                                consumed_at = COALESCE(consumed_at, NOW())
                            WHERE token_hash = %s
                            """,
                            (eff_hash,),
                        )
                        return {
                            "ok": False,
                            "abort_card_send": True,
                            "bind_status": "send_failed",
                            "token_hash": eff_hash,
                            "reason": "interactive_or_ledger_disabled_or_expired",
                        }
                    meta_dict = dict(send_reconcile_meta or {})
                    meta_dict.setdefault("client_msg_uuid", f"dlt-card-{eff_hash[:32]}")
                    await cur.execute(
                        """
                        UPDATE dalton_channel_action_tokens
                        SET bind_status = 'send_in_flight',
                            send_attempt_started_at = NOW(),
                            send_gateway_epoch = %s,
                            send_reconcile_meta = %s
                        WHERE token_hash = %s
                        RETURNING send_attempt_started_at
                        """,
                        (active_epoch or parsed_epoch, Jsonb(meta_dict), eff_hash),
                    )
                    upd = await cur.fetchone()
                    return {
                        "ok": True,
                        "idempotent": False,
                        "abort_card_send": False,
                        "bind_status": "send_in_flight",
                        "token_hash": eff_hash,
                        "send_attempt_started_at": _iso((upd or {}).get("send_attempt_started_at")),
                        "send_gateway_epoch": active_epoch or parsed_epoch,
                        "send_reconcile_meta": meta_dict,
                    }

                if cleaned_phase == "send_failed":
                    if cur_bs == "bound":
                        return {
                            "ok": True,
                            "idempotent": True,
                            "bind_status": "bound",
                            "token_hash": eff_hash,
                        }
                    await cur.execute(
                        """
                        UPDATE dalton_channel_action_tokens
                        SET bind_status = 'send_failed',
                            consumed_via = COALESCE(consumed_via, 'superseded'),
                            consumed_at = COALESCE(consumed_at, NOW())
                        WHERE token_hash = %s
                        """,
                        (eff_hash,),
                    )
                    return {
                        "ok": True,
                        "bind_status": "send_failed",
                        "token_hash": eff_hash,
                    }

                if cleaned_phase == "send_unknown":
                    if cur_bs == "bound":
                        return {
                            "ok": True,
                            "idempotent": True,
                            "bind_status": "bound",
                            "token_hash": eff_hash,
                        }
                    await cur.execute(
                        """
                        UPDATE dalton_channel_action_tokens
                        SET bind_status = 'send_unknown',
                            consumed_via = COALESCE(consumed_via, 'expired'),
                            consumed_at = COALESCE(consumed_at, NOW())
                        WHERE token_hash = %s
                        """,
                        (eff_hash,),
                    )
                    return {
                        "ok": True,
                        "bind_status": "send_unknown",
                        "token_hash": eff_hash,
                    }

                # phase == "confirm_bound"
                clean_msg_id = str(card_message_id or "").strip()
                if not clean_msg_id:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "missing_card_message_id", "message": "card_message_id is required for confirm_bound."},
                    )
                clean_ch_id = str(card_channel_id or "").strip() or None
                if cur_bs == "bound":
                    if (
                        str(tok_dict.get("card_message_id") or "") == clean_msg_id
                        and (clean_ch_id is None or str(tok_dict.get("card_channel_id") or "") == clean_ch_id)
                    ):
                        return {
                            "ok": True,
                            "idempotent": True,
                            "bind_status": "bound",
                            "token_hash": eff_hash,
                            "reopen_refused": tok_dict.get("late_patch_status") is not None,
                            "should_patch_terminal_immediately": tok_dict.get("late_patch_status") == "pending",
                            "late_patch_queued": tok_dict.get("late_patch_status") == "pending",
                            "resume_status": str(resume_row["resume_status"]) if resume_row else None,
                            "settled_via": str(resume_row["settled_via"]) if resume_row else None,
                        }
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "card_already_bound", "message": "Token is already bound to a different card message."},
                    )

                res = await _apply_confirm_bound_in_tx(
                    cur,
                    token_row=tok_dict,
                    card_channel_id=clean_ch_id,
                    card_message_id=clean_msg_id,
                    resume_row=resume_row,
                    ledger_enabled=rt_cfg["interrupt_ledger_enabled"],
                    enabled_platforms=enabled_platforms,
                )
                return {"ok": True, "idempotent": False, "token_hash": eff_hash, **res}


# ---------------------------------------------------------------------------
# 3. Inspect Discord Gate Token (POST /internal/action-tokens/inspect-gate)
# ---------------------------------------------------------------------------


async def inspect_discord_gate_token(
    *,
    approval_nonce: str,
    clicker_platform_user_id: str,
    channel_id: str,
    message_id: str,
    gateway_epoch: int | str | None = None,
    require_gateway_epoch: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Verify Discord group L1/L2 gate click, self-heal unbound card via 3-branch tx,
    enforce 3-way patch gate, write ``gate_verified_at``, and mint short-lived
    ``ephemeral_confirm`` token."""
    eff_hash = hash_approval_nonce(approval_nonce)
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT token_hash, platform, thread_id::text AS thread_id, run_id, interrupt_id
                FROM dalton_channel_action_tokens
                WHERE token_hash = %s
                """,
                (eff_hash,),
            )
            pre = await cur.fetchone()
    if not pre:
        raise HTTPException(
            status_code=404,
            detail={"code": "token_not_found", "message": "Gate token not found."},
        )
    require_known_channel_platform(
        pre["platform"], allowed=DISCORD_ONLY_PLATFORMS, invalid_status=409
    )
    parsed_epoch = _parse_epoch_strict(gateway_epoch, required=require_gateway_epoch)
    enabled_platforms = set(get_enabled_channel_platforms())

    # Pre-read live checkpoint outside the write transaction
    live_pending_by_id, _ = await load_live_pending_interrupts(str(pre["thread_id"]))

    deferred_error: HTTPException | None = None
    result_payload: dict[str, Any] | None = None

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SET LOCAL lock_timeout = '2s'")
                await cur.execute("SET LOCAL statement_timeout = '2s'")

                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    parsed_epoch,
                    required=require_gateway_epoch,
                    platform="discord",
                    enforce_post_lease_for_non_weixin=False,
                )
                if _before_7b0_lock_hook is not None:
                    await _before_7b0_lock_hook("inspect_gate")
                rt_cfg = await read_effective_interrupt_runtime_config_in_tx(cur, for_update=False)

                await _acquire_hitl_resume_lock_in_tx(
                    cur,
                    thread_id=str(pre["thread_id"]),
                    run_id=str(pre["run_id"]),
                    interrupt_id=str(pre["interrupt_id"]),
                )
                resume_row = await _lock_interrupt_resume_in_tx(
                    cur,
                    thread_id=str(pre["thread_id"]),
                    run_id=str(pre["run_id"]),
                    interrupt_id=str(pre["interrupt_id"]),
                )
                await cur.execute(
                    """
                    SELECT *, thread_id::text AS thread_id_text
                    FROM dalton_channel_action_tokens
                    WHERE token_hash = %s
                    FOR UPDATE
                    """,
                    (eff_hash,),
                )
                tok = await cur.fetchone()
                if not tok:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "token_not_found", "message": "Gate token not found."},
                    )
                tok_dict = dict(tok)
                tok_dict["thread_id"] = tok_dict.pop("thread_id_text")

                if str(tok_dict.get("token_role") or "") != "gate":
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "not_a_gate_token", "message": "Token is not a Discord gate token."},
                    )
                if str(clicker_platform_user_id).strip() != str(tok_dict["initiator_platform_user_id"]).strip():
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "clicker_mismatch",
                            "can_patch_terminal": False,
                            "message": "此项操作由发起人本人确认",
                        },
                    )

                # Self-heal unbound card before channel/message check (shares _apply_confirm_bound_in_tx)
                clean_ch = str(channel_id).strip()
                clean_mid = str(message_id).strip()
                if (
                    str(tok_dict["bind_status"]) in ("send_in_flight", "send_unknown", "send_failed")
                    and not tok_dict.get("card_message_id")
                ):
                    meta = tok_dict.get("send_reconcile_meta") or {}
                    meta_ch = (
                        str(meta.get("target_channel_id") or "").strip()
                        if isinstance(meta, Mapping)
                        else ""
                    )
                    if meta_ch and meta_ch != clean_ch:
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": "card_channel_mismatch",
                                "can_patch_terminal": False,
                                "message": "Card channel does not match reconcile metadata.",
                            },
                        )
                    await _apply_confirm_bound_in_tx(
                        cur,
                        token_row=tok_dict,
                        card_channel_id=clean_ch,
                        card_message_id=clean_mid,
                        resume_row=resume_row,
                        ledger_enabled=rt_cfg["interrupt_ledger_enabled"],
                        enabled_platforms=enabled_platforms,
                    )

                if (
                    str(tok_dict.get("card_channel_id") or "") != clean_ch
                    or str(tok_dict.get("card_message_id") or "") != clean_mid
                ):
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "card_message_mismatch",
                            "can_patch_terminal": False,
                            "message": "Card channel or message ID does not match bound token.",
                        },
                    )

                db_now = await cg_db._fetch_db_now(cur)
                exp_dt = _coerce_utc(tok_dict.get("expires_at"))
                is_expired = exp_dt is not None and exp_dt <= db_now
                can_patch = compute_can_patch_terminal(resume_row)

                # If already consumed (including via self-heal Branch 2 or Branch 3) -> commit & return 409/410
                if tok_dict.get("consumed_at") is not None:
                    cv = str(tok_dict.get("consumed_via") or "")
                    lp = tok_dict.get("late_patch_status")
                    if cv == "expired" and (resume_row is None or lp is not None):
                        if tok_dict.get("bind_status") == "bound" and lp is None:
                            lp = "pending" if "discord" in enabled_platforms else "skipped_platform_disabled"
                            await cur.execute(
                                """
                                UPDATE dalton_channel_action_tokens
                                SET late_patch_status = %s,
                                    late_patch_attempts = 0,
                                    late_patch_next_retry_at = CASE WHEN %s = 'pending' THEN NOW() ELSE NULL END
                                WHERE token_hash = %s
                                """,
                                (lp, lp, eff_hash),
                            )
                        deferred_error = HTTPException(
                            status_code=410,
                            detail={
                                "code": "token_expired",
                                "can_patch_terminal": tok_dict.get("bind_status") == "bound" and lp == "pending",
                                "patch_ack_mode": "late_card_results" if lp == "pending" else None,
                                "token_hash": eff_hash,
                            },
                        )
                    else:
                        eff_can_patch = can_patch or (lp == "pending")
                        ack_mode = (
                            "late_card_results"
                            if lp == "pending"
                            else ("card_results" if can_patch else None)
                        )
                        deferred_error = HTTPException(
                            status_code=409,
                            detail={
                                "code": "already_settled",
                                "settled_via": str(resume_row["settled_via"]) if resume_row else cv,
                                "can_patch_terminal": eff_can_patch,
                                "patch_ack_mode": ack_mode,
                                "token_hash": eff_hash,
                            },
                        )
                elif not rt_cfg["interactive_actions_enabled"] or not rt_cfg["interrupt_ledger_enabled"]:
                    # Commit any self-heal binding above, then return 409 interactive_actions_disabled
                    deferred_error = HTTPException(
                        status_code=409,
                        detail={
                            "code": "interactive_actions_disabled",
                            "can_patch_terminal": False,
                            "token_hash": eff_hash,
                        },
                    )
                elif is_expired:
                    lp = "pending" if "discord" in enabled_platforms else "skipped_platform_disabled"
                    await cur.execute(
                        """
                        UPDATE dalton_channel_action_tokens
                        SET consumed_via = 'expired',
                            consumed_at = NOW(),
                            late_patch_status = COALESCE(late_patch_status, %s),
                            late_patch_next_retry_at = CASE WHEN %s = 'pending' THEN NOW() ELSE late_patch_next_retry_at END
                        WHERE token_hash = %s
                        """,
                        (lp, lp, eff_hash),
                    )
                    deferred_error = HTTPException(
                        status_code=410,
                        detail={
                            "code": "token_expired",
                            "can_patch_terminal": lp == "pending",
                            "patch_ack_mode": "late_card_results" if lp == "pending" else None,
                            "token_hash": eff_hash,
                        },
                    )
                else:
                    # Verify binding, tier, group deliverable, and live checkpoint
                    uid = str(tok_dict["dalton_user_id"])
                    await cur.execute(
                        """
                        SELECT platform_user_id
                        FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = 'discord'
                        """,
                        (uid,),
                    )
                    id_row = await cur.fetchone()
                    if not id_row or str(id_row.get("platform_user_id") or "") != str(clicker_platform_user_id).strip():
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "identity_unbound", "can_patch_terminal": False},
                        )
                    tier = await get_user_tier(uid, conn=owned_conn)
                    if tier not in ("admin", "internal"):
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "external_member_forbidden", "can_patch_terminal": False},
                        )
                    grp_ok, grp_err, _ = await cg_db._verify_group_target_deliverable(
                        cur,
                        user_id=uid,
                        user_tier=tier,
                        platform="discord",
                        chat_address=str(tok_dict["origin_chat_address"]),
                        conn=owned_conn,
                    )
                    if not grp_ok:
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "group_not_deliverable", "message": grp_err, "can_patch_terminal": False},
                        )
                    if str(tok_dict["interrupt_id"]) not in live_pending_by_id:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "interrupt_no_longer_pending", "can_patch_terminal": can_patch},
                        )

                    # Mark gate_verified_at = NOW() (keeping consumed_at IS NULL) and mint ephemeral_confirm
                    await cur.execute(
                        """
                        UPDATE dalton_channel_action_tokens
                        SET gate_verified_at = NOW()
                        WHERE token_hash = %s
                        """,
                        (eff_hash,),
                    )
                    remaining_s = max(
                        1,
                        int((exp_dt - db_now).total_seconds())
                        if exp_dt is not None
                        else EPHEMERAL_CONFIRM_MAX_TTL_SECONDS,
                    )
                    tier_ttl = RISK_TIER_TTL_SECONDS.get(str(tok_dict["risk_tier"]), 600)
                    eph_ttl = min(tier_ttl, remaining_s, EPHEMERAL_CONFIRM_MAX_TTL_SECONDS)
                    eph_nonce = generate_approval_nonce()
                    eph_hash = hash_approval_nonce(eph_nonce)
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_action_tokens (
                            token_hash, token_role, parent_token_hash, platform,
                            dalton_user_id, initiator_platform_user_id,
                            origin_chat_type, origin_chat_address, origin_scope_key,
                            session_generation, card_chat_type, card_delivery_address,
                            card_channel_id, bind_status,
                            thread_id, run_id, interrupt_id,
                            risk_tier, action_type, args_sha256,
                            server_confirm_token, options_snapshot, prompt_summary,
                            expires_at
                        )
                        VALUES (
                            %s, 'ephemeral_confirm', %s, 'discord',
                            %s, %s,
                            %s, %s, %s,
                            %s, 'group', %s,
                            %s, 'ephemeral',
                            %s::uuid, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            NOW() + (%s || ' seconds')::interval
                        )
                        RETURNING expires_at
                        """,
                        (
                            eph_hash,
                            eff_hash,
                            uid,
                            str(tok_dict["initiator_platform_user_id"]),
                            str(tok_dict["origin_chat_type"]),
                            str(tok_dict["origin_chat_address"]),
                            str(tok_dict["origin_scope_key"]),
                            int(tok_dict["session_generation"]),
                            str(tok_dict["card_delivery_address"]),
                            clean_ch,
                            str(tok_dict["thread_id"]),
                            str(tok_dict["run_id"]),
                            str(tok_dict["interrupt_id"]),
                            str(tok_dict["risk_tier"]),
                            str(tok_dict["action_type"]),
                            str(tok_dict["args_sha256"]),
                            tok_dict.get("server_confirm_token"),
                            Jsonb(tok_dict["options_snapshot"]) if tok_dict.get("options_snapshot") is not None else None,
                            Jsonb(dict(tok_dict.get("prompt_summary") or {})),
                            str(eph_ttl),
                        ),
                    )
                    eph_ins = await cur.fetchone()
                    result_payload = {
                        "ok": True,
                        "gate_token_hash": eff_hash,
                        "ephemeral_token_hash": eph_hash,
                        "ephemeral_nonce": eph_nonce,
                        "risk_tier": str(tok_dict["risk_tier"]),
                        "action_type": str(tok_dict["action_type"]),
                        "prompt_summary": dict(tok_dict.get("prompt_summary") or {}),
                        "expires_at": _iso((eph_ins or {}).get("expires_at")),
                        "ttl_seconds": eph_ttl,
                    }

    # Transaction has committed (preserving any self-heal/expiration updates)
    if deferred_error is not None:
        raise deferred_error
    assert result_payload is not None
    return result_payload


# ---------------------------------------------------------------------------
# 4. Consume Action Token (POST /internal/action-tokens/consume)
# ---------------------------------------------------------------------------


def _build_hitl_response_for_token(
    *,
    token_row: Mapping[str, Any],
    decision: str,
    reason: str | None = None,
    answer_text: str | None = None,
    option_index: int | None = None,
) -> dict[str, Any]:
    """Assemble authoritative server-side ``hitl_response`` payload."""
    interrupt_id = str(token_row["interrupt_id"])
    action_type = str(token_row["action_type"])
    risk_tier = str(token_row["risk_tier"])
    clean_dec = str(decision or "").strip().lower()

    if risk_tier == "L0" and action_type == "ask_user_question":
        opts = token_row.get("options_snapshot")
        if clean_dec == "skip":
            return {"hitl_response": {interrupt_id: {"decisions": [{"type": "reject", "message": "skip"}]}}}
        selected_text = answer_text
        if option_index is not None:
            if not isinstance(opts, list) or option_index < 0 or option_index >= len(opts):
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_option_index", "message": "Option index out of bounds."},
                )
            opt_val = opts[option_index]
            if isinstance(opt_val, Mapping):
                selected_text = str(opt_val.get("label") or opt_val.get("value") or opt_val)
            else:
                selected_text = str(opt_val)
        return {
            "hitl_response": {
                interrupt_id: {
                    "decisions": [{"type": "approve", "message": str(selected_text or "")}],
                    "answer": str(selected_text or ""),
                }
            }
        }

    if clean_dec == "approve":
        dec_obj: dict[str, Any] = {"type": "approve"}
        confirm_tok = token_row.get("server_confirm_token")
        if confirm_tok:
            dec_obj["confirm_token"] = str(confirm_tok)
        entry: dict[str, Any] = {"decisions": [dec_obj]}
        if confirm_tok:
            entry["order_decisions"] = {action_type: dict(dec_obj)}
        return {"hitl_response": {interrupt_id: entry}}

    if clean_dec in ("reject", "reason"):
        dec_obj = {"type": "reject"}
        if reason is not None and str(reason).strip():
            dec_obj["message"] = str(reason).strip()
        return {"hitl_response": {interrupt_id: {"decisions": [dec_obj]}}}

    raise HTTPException(
        status_code=400,
        detail={"code": "invalid_decision", "message": f"Unsupported decision: {decision!r}"},
    )


async def consume_channel_action_token(
    *,
    approval_nonce: str,
    decision: str,
    clicker_platform_user_id: str,
    platform: str | None = None,
    channel_id: str | None = None,
    message_id: str | None = None,
    reason: str | None = None,
    answer_text: str | None = None,
    option_index: int | None = None,
    gateway_epoch: int | str | None = None,
    require_gateway_epoch: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Six-step click verification, shared 3-branch self-heal, 3-way patch gate, and
    atomic resume contention (Lock Order ④)."""
    eff_hash = hash_approval_nonce(approval_nonce)

    # Read-only lookup of authoritative platform & immutable origin session/group keys BEFORE epoch parsing
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT token_hash,
                       platform,
                       dalton_user_id,
                       origin_chat_type,
                       origin_chat_address,
                       origin_scope_key,
                       session_generation,
                       thread_id::text AS thread_id,
                       run_id,
                       interrupt_id
                FROM dalton_channel_action_tokens
                WHERE token_hash = %s
                """,
                (eff_hash,),
            )
            pre = await cur.fetchone()
    if not pre:
        raise HTTPException(
            status_code=404,
            detail={"code": "token_not_found", "message": "Action token not found."},
        )
    auth_platform = require_known_channel_platform(
        pre["platform"], allowed=CARD_PLATFORMS, invalid_status=409
    )
    if platform is not None and str(platform).strip().lower() != auth_platform:
        raise HTTPException(
            status_code=400,
            detail={"code": "platform_mismatch", "message": "Request platform does not match token platform."},
        )
    parsed_epoch = _parse_epoch_strict(gateway_epoch, required=require_gateway_epoch)
    thread_id = str(pre["thread_id"])
    run_id = str(pre["run_id"])
    interrupt_id = str(pre["interrupt_id"])
    pre_uid = str(pre.get("dalton_user_id") or "").strip()
    pre_origin_ct = str(pre.get("origin_chat_type") or "").strip().lower()
    pre_origin_addr = str(pre.get("origin_chat_address") or "").strip()
    pre_origin_scope = str(pre.get("origin_scope_key") or "").strip()

    # Unlocked checkpoint read outside the write transaction
    live_pending_by_id, live_snapshots = await load_live_pending_interrupts(thread_id)

    async def _attempt(conn_arg: Any) -> dict[str, Any]:
        preview = await _preview_interrupt_context(
            thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id, conn=conn_arg
        )
        if _after_preview_hook is not None:
            await _after_preview_hook("consume")
        preview_claim_id = preview["preview_claim_id"]
        preview_inb_keys = preview["inbound_keys"]
        preview_wx_aids = list(preview["weixin_account_ids"])
        enabled_platforms = set(get_enabled_channel_platforms())

        deferred_error: HTTPException | None = None
        result_payload: dict[str, Any] | None = None

        async with get_db_connection(conn_arg) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    await cur.execute("SET LOCAL statement_timeout = '2s'")

                    # Level 0b-1: old inbound advisory locks
                    locked_inb_key_list = await _acquire_inbound_advisory_locks_in_tx(
                        cur, preview_inb_keys
                    )
                    locked_inb_key_set = set(locked_inb_key_list)

                    # Level 0b-2: gateway_lease FOR SHARE
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        parsed_epoch,
                        required=require_gateway_epoch,
                        platform=auth_platform,
                        enforce_post_lease_for_non_weixin=False,
                    )

                    # Level 2: group chat advisory lock when origin is a group chat (Lock Order ④)
                    if pre_origin_ct == "group" and pre_origin_addr:
                        norm_grp_addr = cg_db._normalize_topic_chat_address(
                            auth_platform, pre_origin_addr
                        )
                        if norm_grp_addr:
                            await cg_db._acquire_group_chat_lock(
                                cur, auth_platform, norm_grp_addr
                            )

                    # Level 3a: Weixin accounts FOR KEY SHARE
                    locked_wx_aids = await _acquire_weixin_account_key_share_in_tx(
                        cur, preview_wx_aids
                    )

                    # Level 6: lock session_generations FOR UPDATE BEFORE Level 7a (Lock Order ④)
                    locked_sg_row: dict[str, Any] | None = None
                    if pre_origin_scope and pre_uid:
                        await cur.execute(
                            """
                            SELECT generation
                            FROM dalton_channel_session_generations
                            WHERE platform = %s
                              AND scope_key = %s
                              AND dalton_user_id = %s
                            FOR UPDATE
                            """,
                            (auth_platform, pre_origin_scope, pre_uid),
                        )
                        fetched_sg = await cur.fetchone()
                        if fetched_sg is not None:
                            locked_sg_row = dict(fetched_sg)

                    # Level 7a: old inbound_events FOR UPDATE + immediate Weixin lock-set check
                    locked_inbounds = await _lock_inbound_events_and_verify_weixin_set_in_tx(
                        cur, locked_inb_key_list, locked_wx_aids, op_name="consume"
                    )

                    # Level 7b-0: runtime config FOR SHARE
                    if _before_7b0_lock_hook is not None:
                        await _before_7b0_lock_hook("consume")
                    rt_cfg = await read_effective_interrupt_runtime_config_in_tx(
                        cur, for_update=False
                    )
                    if _after_7b0_lock_hook is not None:
                        await _after_7b0_lock_hook("consume")

                    # Level 7b-1: interrupt advisory lock
                    await _acquire_hitl_resume_lock_in_tx(
                        cur, thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id
                    )

                    # Level 7b-2: interrupt_resumes FOR UPDATE + unconditional claim_id check
                    resume_row = await _lock_interrupt_resume_in_tx(
                        cur, thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id
                    )
                    actual_claim_id = str(resume_row["claim_id"]) if resume_row else None
                    if actual_claim_id != preview_claim_id:
                        raise _ResumeInboundLockSetChanged(
                            f"consume: claim_id drifted ({preview_claim_id} -> {actual_claim_id})"
                        )
                    if resume_row:
                        if (
                            resume_row.get("weixin_account_id")
                            and str(resume_row["weixin_account_id"]) not in locked_wx_aids
                        ):
                            raise _ResumeInboundLockSetChanged(
                                "consume: locked resume weixin_account_id missing from 3a set"
                            )
                        if resume_row.get("platform") and resume_row.get("inbound_message_id"):
                            rk_pair = (
                                str(resume_row["platform"]).strip().lower(),
                                str(resume_row["inbound_message_id"]).strip(),
                            )
                            if rk_pair not in locked_inb_key_set:
                                raise _ResumeInboundLockSetChanged(
                                    f"consume: locked resume inbound {rk_pair} missing from 0b-1/7a set"
                                )
                    if _after_7b2_lock_hook is not None:
                        await _after_7b2_lock_hook("consume")

                    # Level 7b-3: lock all tokens for this interrupt in token_hash ASC order
                    all_tokens = await _lock_interrupt_tokens_in_tx(
                        cur, thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id
                    )
                    tok_by_hash = {str(t["token_hash"]): t for t in all_tokens}
                    tok_dict = tok_by_hash.get(eff_hash)
                    if not tok_dict:
                        raise HTTPException(
                            status_code=404,
                            detail={"code": "token_not_found", "message": "Action token not found."},
                        )

                    # Step 1: token role, self-heal via _apply_confirm_bound_in_tx, card/message match
                    role = str(tok_dict["token_role"])
                    if role not in ("direct", "ephemeral_confirm"):
                        raise HTTPException(
                            status_code=400,
                            detail={"code": "invalid_token_role", "message": "Gate tokens must call inspect-gate."},
                        )
                    clean_clicker = str(clicker_platform_user_id).strip()
                    clean_ch = str(channel_id or "").strip() or None
                    clean_mid = str(message_id or "").strip() or None

                    # Step 2 (early clicker check so unauthorized users never trigger self-heal)
                    if clean_clicker != str(tok_dict["initiator_platform_user_id"]).strip():
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": "clicker_mismatch",
                                "can_patch_terminal": False,
                                "message": "此项操作由发起人本人确认",
                            },
                        )

                    if (
                        role == "direct"
                        and str(tok_dict["bind_status"]) in ("send_in_flight", "send_unknown", "send_failed")
                        and not tok_dict.get("card_message_id")
                        and clean_mid
                    ):
                        meta = tok_dict.get("send_reconcile_meta") or {}
                        meta_ch = (
                            str(meta.get("target_channel_id") or "").strip()
                            if isinstance(meta, Mapping)
                            else ""
                        )
                        if meta_ch and clean_ch and meta_ch != clean_ch:
                            raise HTTPException(
                                status_code=403,
                                detail={
                                    "code": "card_channel_mismatch",
                                    "can_patch_terminal": False,
                                    "message": "Card channel does not match reconcile metadata.",
                                },
                            )
                        await _apply_confirm_bound_in_tx(
                            cur,
                            token_row=tok_dict,
                            card_channel_id=clean_ch,
                            card_message_id=clean_mid,
                            resume_row=resume_row,
                            ledger_enabled=rt_cfg["interrupt_ledger_enabled"],
                            enabled_platforms=enabled_platforms,
                        )

                    if not tok_dict.get("card_message_id"):
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "card_not_bound", "can_patch_terminal": False},
                        )
                    if clean_mid and str(tok_dict["card_message_id"]) != clean_mid:
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "card_message_mismatch", "can_patch_terminal": False},
                        )
                    if auth_platform == "discord":
                        if not clean_ch or str(tok_dict.get("card_channel_id") or "") != clean_ch:
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "card_channel_mismatch", "can_patch_terminal": False},
                            )
                    elif auth_platform == "feishu":
                        if str(tok_dict["card_chat_type"]) == "group":
                            if clean_ch and tok_dict.get("card_channel_id") and str(tok_dict["card_channel_id"]) != clean_ch:
                                raise HTTPException(
                                    status_code=403,
                                    detail={"code": "card_channel_mismatch", "can_patch_terminal": False},
                                )
                        elif clean_ch and tok_dict.get("card_channel_id") and str(tok_dict["card_channel_id"]) != clean_ch:
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "card_channel_mismatch", "can_patch_terminal": False},
                            )

                    if role == "ephemeral_confirm":
                        parent_hash = str(tok_dict.get("parent_token_hash") or "")
                        parent_row = tok_by_hash.get(parent_hash)
                        if not parent_row or parent_row.get("gate_verified_at") is None:
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "gate_not_verified", "can_patch_terminal": False},
                            )

                    # Step 2 (continued): live identity binding check
                    uid = str(tok_dict["dalton_user_id"])
                    await cur.execute(
                        """
                        SELECT platform_user_id
                        FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        """,
                        (uid, auth_platform),
                    )
                    id_row = await cur.fetchone()
                    if not id_row or str(id_row.get("platform_user_id") or "") != clean_clicker:
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": "clicker_mismatch",
                                "can_patch_terminal": False,
                                "message": "此项操作由发起人本人确认",
                            },
                        )

                    # Step 3: re-verify group tier and group target deliverable
                    if str(tok_dict["origin_chat_type"]) == "group":
                        tier = await get_user_tier(uid, conn=owned_conn)
                        if tier not in ("admin", "internal"):
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "external_member_forbidden", "can_patch_terminal": False},
                            )
                        grp_ok, grp_err, _ = await cg_db._verify_group_target_deliverable(
                            cur,
                            user_id=uid,
                            user_tier=tier,
                            platform=auth_platform,
                            chat_address=str(tok_dict["origin_chat_address"]),
                            conn=owned_conn,
                        )
                        if not grp_ok:
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "group_not_deliverable", "message": grp_err, "can_patch_terminal": False},
                            )

                    # Step 4: idempotent replay & unified 3-way patch gate
                    can_patch = compute_can_patch_terminal(resume_row)
                    if tok_dict.get("consumed_at") is not None:
                        cv = str(tok_dict.get("consumed_via") or "")
                        lp = tok_dict.get("late_patch_status")
                        if (
                            cv == "channel"
                            and resume_row is not None
                            and str(resume_row.get("token_hash") or "") == eff_hash
                        ):
                            r_status = str(resume_row["resume_status"])
                            if can_patch and r_status == "admitted":
                                result_payload = {
                                    "ok": True,
                                    "idempotent_replay": True,
                                    "outcome": "completed",
                                    "can_patch_terminal": True,
                                    "patch_ack_mode": "card_results",
                                    "claim_id": str(resume_row["claim_id"]),
                                    "request_key": str(resume_row["request_key"]),
                                    "admitted_run_id": resume_row.get("admitted_run_id"),
                                    "resume_payload": resume_row.get("resume_payload"),
                                    "token_hash": eff_hash,
                                }
                            elif can_patch and r_status == "terminal_failed":
                                deferred_error = HTTPException(
                                    status_code=409,
                                    detail={
                                        "code": "resume_terminal_failed",
                                        "can_patch_terminal": True,
                                        "patch_ack_mode": "card_results",
                                        "claim_id": str(resume_row["claim_id"]),
                                        "token_hash": eff_hash,
                                    },
                                )
                            else:
                                result_payload = {
                                    "ok": True,
                                    "idempotent_replay": True,
                                    "outcome": "in_progress",
                                    "can_patch_terminal": False,
                                    "patch_ack_mode": None,
                                    "claim_id": str(resume_row["claim_id"]),
                                    "request_key": str(resume_row["request_key"]),
                                    "resume_status": r_status,
                                    "admitted_run_id": resume_row.get("admitted_run_id"),
                                    "resume_payload": resume_row.get("resume_payload"),
                                    "prompt_summary": dict(tok_dict.get("prompt_summary") or {}),
                                    "token_hash": eff_hash,
                                }
                        elif cv == "expired" and (resume_row is None or lp is not None):
                            if tok_dict.get("bind_status") == "bound" and lp is None:
                                lp = "pending" if auth_platform in enabled_platforms else "skipped_platform_disabled"
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_action_tokens
                                    SET late_patch_status = %s,
                                        late_patch_attempts = 0,
                                        late_patch_next_retry_at = CASE WHEN %s = 'pending' THEN NOW() ELSE NULL END
                                    WHERE token_hash = %s
                                    """,
                                    (lp, lp, eff_hash),
                                )
                            deferred_error = HTTPException(
                                status_code=410,
                                detail={
                                    "code": "token_expired",
                                    "can_patch_terminal": tok_dict.get("bind_status") == "bound" and lp == "pending",
                                    "patch_ack_mode": "late_card_results" if lp == "pending" else None,
                                    "token_hash": eff_hash,
                                },
                            )
                        else:
                            eff_can_patch = can_patch or (lp == "pending")
                            ack_mode = (
                                "late_card_results"
                                if lp == "pending"
                                else ("card_results" if can_patch else None)
                            )
                            deferred_error = HTTPException(
                                status_code=409,
                                detail={
                                    "code": "already_settled",
                                    "settled_via": str(resume_row["settled_via"]) if resume_row else cv,
                                    "can_patch_terminal": eff_can_patch,
                                    "patch_ack_mode": ack_mode,
                                    "token_hash": eff_hash,
                                },
                            )
                    elif not rt_cfg["interactive_actions_enabled"] or not rt_cfg["interrupt_ledger_enabled"]:
                        # Preserve Step 1 self-heal binding commit and return 409 interactive_actions_disabled
                        deferred_error = HTTPException(
                            status_code=409,
                            detail={
                                "code": "interactive_actions_disabled",
                                "can_patch_terminal": False,
                                "token_hash": eff_hash,
                            },
                        )
                    else:
                        # Step 5: DB clock expiry, session generation, live checkpoint & args_sha256
                        db_now = await cg_db._fetch_db_now(cur)
                        exp_dt = _coerce_utc(tok_dict.get("expires_at"))
                        if exp_dt is not None and exp_dt <= db_now:
                            lp = (
                                ("pending" if auth_platform in enabled_platforms else "skipped_platform_disabled")
                                if tok_dict.get("bind_status") == "bound"
                                else None
                            )
                            await cur.execute(
                                """
                                UPDATE dalton_channel_action_tokens
                                SET consumed_via = 'expired',
                                    consumed_at = NOW(),
                                    late_patch_status = COALESCE(late_patch_status, %s),
                                    late_patch_next_retry_at = CASE WHEN %s = 'pending' THEN NOW() ELSE late_patch_next_retry_at END
                                WHERE token_hash = %s
                                """,
                                (lp, lp, eff_hash),
                            )
                            deferred_error = HTTPException(
                                status_code=410,
                                detail={
                                    "code": "token_expired",
                                    "can_patch_terminal": lp == "pending",
                                    "patch_ack_mode": "late_card_results" if lp == "pending" else None,
                                    "token_hash": eff_hash,
                                },
                            )
                        else:
                            # Use Level 6 FOR UPDATE locked session_generations row (acquired before Level 7a)
                            if locked_sg_row is not None and int(locked_sg_row["generation"]) != int(tok_dict["session_generation"]):
                                raise HTTPException(
                                    status_code=409,
                                    detail={"code": "session_generation_superseded", "can_patch_terminal": False},
                                )

                            pending_rec = live_pending_by_id.get(interrupt_id)
                            if not pending_rec:
                                raise HTTPException(
                                    status_code=409,
                                    detail={"code": "interrupt_no_longer_pending", "can_patch_terminal": can_patch},
                                )
                            live_val = pending_rec.get("value") if isinstance(pending_rec.get("value"), Mapping) else {}
                            live_reqs = live_val.get("action_requests")
                            if not isinstance(live_reqs, list) or len(live_reqs) != 1 or not isinstance(live_reqs[0], Mapping):
                                raise HTTPException(
                                    status_code=409,
                                    detail={"code": "scope_changed", "can_patch_terminal": False},
                                )
                            live_action = live_reqs[0]
                            if compute_args_sha256(live_action) != str(tok_dict["args_sha256"]):
                                raise HTTPException(
                                    status_code=409,
                                    detail={"code": "scope_changed", "can_patch_terminal": False},
                                )
                            if str(tok_dict["action_type"]) == "create_workspace" and str(decision).strip().lower() == "approve":
                                tcid = live_action.get("tool_call_id")
                                snap = live_snapshots.get(tcid) if isinstance(tcid, str) else None
                                if not await is_verified_managed_cloud_create_workspace(
                                    live_action, snap, uid, cur=cur, conn=owned_conn
                                ):
                                    raise HTTPException(
                                        status_code=409,
                                        detail={"code": "scope_changed", "can_patch_terminal": False},
                                    )
                            if str(tok_dict["action_type"]) == "ptc_agent":
                                cur_tier = await _classify_ptc_agent_workspace_tier(
                                    cur, dalton_user_id=uid, action_req=live_action, conn=owned_conn
                                )
                                if cur_tier != str(tok_dict["risk_tier"]):
                                    raise HTTPException(
                                        status_code=409,
                                        detail={"code": "scope_changed", "can_patch_terminal": False},
                                    )

                            # Step 6: assemble resume_payload & atomic insert/takeover with rowcount == 1
                            resume_payload = _build_hitl_response_for_token(
                                token_row=tok_dict,
                                decision=decision,
                                reason=reason,
                                answer_text=answer_text,
                                option_index=option_index,
                            )
                            new_claim_id = str(uuid4())
                            new_request_key = derive_hitl_resume_request_key(
                                thread_id, run_id, interrupt_id, new_claim_id
                            )
                            card_targets = await _collect_bound_card_targets_in_tx(
                                all_tokens,
                                existing_targets=(resume_row.get("card_targets") if resume_row else None),
                                enabled_platforms=enabled_platforms,
                            )
                            superseded_hashes = [
                                str(t["token_hash"])
                                for t in all_tokens
                                if t.get("consumed_at") is None
                            ]
                            old_channel_text_key: tuple[str, str] | None = None

                            if resume_row is None:
                                await cur.execute(
                                    """
                                    INSERT INTO dalton_channel_interrupt_resumes (
                                        thread_id, run_id, interrupt_id, claim_id,
                                        settled_via, settled_by_user_id, platform, token_hash,
                                        inbound_message_id, legacy_direct,
                                        origin_scope_key, session_generation, origin_chat_address,
                                        request_key, resume_payload,
                                        card_delivery_address, card_channel_id, card_message_id,
                                        superseded_token_hashes, card_targets,
                                        resume_status, requires_gateway_ack, delivery_settled,
                                        lease_expires_at, run_consumed_at, channel_ack_at,
                                        attempt_count, next_retry_at, last_error
                                    )
                                    VALUES (
                                        %s::uuid, %s, %s, %s::uuid,
                                        'channel_button', %s, %s, %s,
                                        NULL, FALSE,
                                        %s, %s, %s,
                                        %s::uuid, %s,
                                        %s, %s, %s,
                                        %s, %s,
                                        'pending_dispatch', TRUE, FALSE,
                                        NOW() + (%s || ' seconds')::interval, NULL, NULL,
                                        0, NULL, NULL
                                    )
                                    ON CONFLICT (thread_id, run_id, interrupt_id) DO NOTHING
                                    """,
                                    (
                                        thread_id,
                                        run_id,
                                        interrupt_id,
                                        new_claim_id,
                                        uid,
                                        auth_platform,
                                        eff_hash,
                                        str(tok_dict["origin_scope_key"]),
                                        int(tok_dict["session_generation"]),
                                        str(tok_dict["origin_chat_address"]),
                                        new_request_key,
                                        Jsonb(resume_payload),
                                        str(tok_dict["card_delivery_address"]),
                                        tok_dict.get("card_channel_id"),
                                        tok_dict.get("card_message_id"),
                                        Jsonb(superseded_hashes),
                                        Jsonb(card_targets),
                                        str(RESUME_LEASE_SECONDS),
                                    ),
                                )
                                if cur.rowcount != 1:
                                    raise _ResumeInboundLockSetChanged(
                                        f"consume: insert rowcount={cur.rowcount}"
                                    )
                            else:
                                # Check if takeover is allowed (expired pending_dispatch or terminal_failed)
                                r_st = str(resume_row["resume_status"])
                                lease_dt = _coerce_utc(resume_row.get("lease_expires_at"))
                                can_takeover = False
                                if r_st == "pending_dispatch" and lease_dt is not None and lease_dt < db_now:
                                    ex_run = await _has_active_or_advanced_run_for_key(
                                        cur, thread_id=thread_id, request_key=str(resume_row["request_key"])
                                    )
                                    if not ex_run or str(ex_run.get("status") or "") not in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                                        can_takeover = True
                                elif r_st == "terminal_failed":
                                    if not await _has_in_progress_run_on_thread(cur, thread_id):
                                        ex_run = await _has_active_or_advanced_run_for_key(
                                            cur,
                                            thread_id=thread_id,
                                            request_key=str(resume_row["request_key"]),
                                            admitted_run_id=resume_row.get("admitted_run_id"),
                                        )
                                        if not ex_run or str(ex_run.get("status") or "") not in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                                            can_takeover = True

                                if not can_takeover:
                                    raise HTTPException(
                                        status_code=409,
                                        detail={
                                            "code": "already_settled",
                                            "settled_via": str(resume_row["settled_via"]),
                                            "can_patch_terminal": can_patch,
                                            "patch_ack_mode": "card_results" if can_patch else None,
                                        },
                                    )

                                if (
                                    str(resume_row.get("settled_via") or "") == "channel_text"
                                    and resume_row.get("platform")
                                    and resume_row.get("inbound_message_id")
                                ):
                                    old_channel_text_key = (
                                        str(resume_row["platform"]).strip().lower(),
                                        str(resume_row["inbound_message_id"]).strip(),
                                    )

                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_interrupt_resumes
                                    SET claim_id = %s::uuid,
                                        settled_via = 'channel_button',
                                        settled_by_user_id = %s,
                                        platform = %s,
                                        token_hash = %s,
                                        inbound_message_id = NULL,
                                        legacy_direct = FALSE,
                                        origin_scope_key = %s,
                                        session_generation = %s,
                                        origin_chat_address = %s,
                                        weixin_account_id = NULL,
                                        weixin_bot_id = NULL,
                                        weixin_bot_generation = NULL,
                                        weixin_recipient_id = NULL,
                                        request_key = %s::uuid,
                                        resume_payload = %s,
                                        admitted_run_id = NULL,
                                        card_delivery_address = %s,
                                        card_channel_id = %s,
                                        card_message_id = %s,
                                        superseded_token_hashes = %s,
                                        card_targets = %s,
                                        resume_status = 'pending_dispatch',
                                        requires_gateway_ack = TRUE,
                                        delivery_settled = FALSE,
                                        lease_expires_at = NOW() + (%s || ' seconds')::interval,
                                        run_consumed_at = NULL,
                                        channel_ack_at = NULL,
                                        attempt_count = 0,
                                        next_retry_at = NULL,
                                        last_error = NULL,
                                        updated_at = NOW()
                                    WHERE thread_id = %s::uuid
                                      AND run_id = %s
                                      AND interrupt_id = %s
                                      AND claim_id = %s::uuid
                                    """,
                                    (
                                        new_claim_id,
                                        uid,
                                        auth_platform,
                                        eff_hash,
                                        str(tok_dict["origin_scope_key"]),
                                        int(tok_dict["session_generation"]),
                                        str(tok_dict["origin_chat_address"]),
                                        new_request_key,
                                        Jsonb(resume_payload),
                                        str(tok_dict["card_delivery_address"]),
                                        tok_dict.get("card_channel_id"),
                                        tok_dict.get("card_message_id"),
                                        Jsonb(superseded_hashes),
                                        Jsonb(card_targets),
                                        str(RESUME_LEASE_SECONDS),
                                        thread_id,
                                        run_id,
                                        interrupt_id,
                                        preview_claim_id,
                                    ),
                                )
                                if cur.rowcount != 1:
                                    raise _ResumeInboundLockSetChanged(
                                        f"consume: takeover update rowcount={cur.rowcount}"
                                    )

                            # After cur.rowcount == 1: settle superseded old channel_text inbound row & consume tokens
                            if old_channel_text_key and old_channel_text_key in locked_inbounds:
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_inbound_events
                                    SET status = 'completed',
                                        fallback_pending = FALSE,
                                        raw_payload = COALESCE(raw_payload, '{}'::jsonb)
                                            || jsonb_build_object('hitl_superseded_by', 'web_or_button_or_newer_text'),
                                        updated_at = NOW()
                                    WHERE platform = %s AND message_id = %s
                                    """,
                                    old_channel_text_key,
                                )

                            for t in all_tokens:
                                if t.get("consumed_at") is not None:
                                    continue
                                th = str(t["token_hash"])
                                cv_val = "channel" if th == eff_hash else "superseded"
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_action_tokens
                                    SET consumed_via = %s,
                                        consumed_at = NOW()
                                    WHERE token_hash = %s
                                    """,
                                    (cv_val, th),
                                )

                            result_payload = {
                                "ok": True,
                                "idempotent_replay": False,
                                "outcome": "claimed",
                                "can_patch_terminal": False,
                                "patch_ack_mode": None,
                                "claim_id": new_claim_id,
                                "request_key": new_request_key,
                                "thread_id": thread_id,
                                "run_id": run_id,
                                "interrupt_id": interrupt_id,
                                "resume_payload": resume_payload,
                                "token_hash": eff_hash,
                                "card_targets": card_targets,
                            }

        if deferred_error is not None:
            raise deferred_error
        assert result_payload is not None
        return result_payload

    return await _run_interrupt_tx_with_retry("consume_channel_action_token", conn, _attempt)


# ---------------------------------------------------------------------------
# 5. Claim Text Interrupt Resume (POST /internal/interrupt-resumes/claim-text)
# ---------------------------------------------------------------------------


async def claim_channel_text_interrupt_resume(
    *,
    platform: str,
    message_id: str,
    dalton_user_id: str,
    scope_key: str,
    generation: int,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    resume_payload: Mapping[str, Any],
    gateway_epoch: int | str | None = None,
    require_gateway_epoch: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Claim an ``ask_user_question`` or ``credit_pause`` interrupt via channel text reply (Lock Order ③)."""
    cleaned_plat = require_known_channel_platform(platform, allowed=ALL_CHANNEL_PLATFORMS)
    reject_company_system_auth(dalton_user_id)
    cleaned_mid = str(message_id or "").strip()
    cleaned_scope = str(scope_key or "").strip()
    norm_tid = normalize_uuid(thread_id)
    if not cleaned_mid or not cleaned_scope or not norm_tid or not str(run_id or "").strip() or not str(interrupt_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_claim_text_request", "message": "Missing required claim-text identifiers."},
        )
    parsed_epoch = _parse_epoch_strict(gateway_epoch, required=require_gateway_epoch)

    # Unlocked checkpoint read outside the write transaction
    live_pending_by_id, _ = await load_live_pending_interrupts(norm_tid)

    async def _attempt(conn_arg: Any) -> dict[str, Any]:
        preview = await _preview_interrupt_context(
            thread_id=norm_tid,
            run_id=str(run_id),
            interrupt_id=str(interrupt_id),
            extra_inbound_key=(cleaned_plat, cleaned_mid),
            conn=conn_arg,
        )
        if _after_preview_hook is not None:
            await _after_preview_hook("claim_text")
        preview_claim_id = preview["preview_claim_id"]
        preview_inb_keys = preview["inbound_keys"]
        preview_wx_aids = list(preview["weixin_account_ids"])
        enabled_platforms = set(get_enabled_channel_platforms())

        async with get_db_connection(conn_arg) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    await cur.execute("SET LOCAL statement_timeout = '2s'")

                    # Level 0b-1: sorted (platform, message_id) ASC advisory locks
                    locked_inb_key_list = await _acquire_inbound_advisory_locks_in_tx(
                        cur, preview_inb_keys
                    )
                    locked_inb_key_set = set(locked_inb_key_list)

                    # Level 0b-2: gateway_lease FOR SHARE
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        parsed_epoch,
                        required=require_gateway_epoch,
                        platform=cleaned_plat,
                        enforce_post_lease_for_non_weixin=True,
                    )

                    # Level 3a: Weixin accounts FOR KEY SHARE (before Level 6 & 7a)
                    locked_wx_aids = await _acquire_weixin_account_key_share_in_tx(
                        cur, preview_wx_aids
                    )

                    # Level 6: session_generations FOR UPDATE
                    await cur.execute(
                        """
                        SELECT generation, pending_interrupt
                        FROM dalton_channel_session_generations
                        WHERE platform = %s
                          AND scope_key = %s
                          AND dalton_user_id = %s
                        FOR UPDATE
                        """,
                        (cleaned_plat, cleaned_scope, dalton_user_id),
                    )
                    sg_row = await cur.fetchone()
                    if not sg_row or int(sg_row["generation"]) != int(generation):
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "session_generation_mismatch", "message": "Session generation mismatch."},
                        )

                    # Level 7a step 1: lock inbound_events + IMMEDIATE weixin_account_id check BEFORE thread_watches
                    locked_inbounds = await _lock_inbound_events_and_verify_weixin_set_in_tx(
                        cur, locked_inb_key_list, locked_wx_aids, op_name="claim_text"
                    )
                    in_row = locked_inbounds.get((cleaned_plat, cleaned_mid))
                    if not in_row:
                        raise HTTPException(
                            status_code=404,
                            detail={"code": "inbound_not_found", "message": "Inbound event not found."},
                        )
                    if str(in_row.get("platform") or "").strip().lower() != cleaned_plat:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "invalid_platform", "message": "Inbound row platform mismatch."},
                        )
                    if (
                        str(in_row.get("dalton_user_id") or "") != str(dalton_user_id)
                        or str(in_row.get("thread_scope_key") or "") != cleaned_scope
                    ):
                        raise HTTPException(
                            status_code=403,
                            detail={"code": "ownership_mismatch", "message": "Inbound ownership or scope mismatch."},
                        )

                    # Level 7a step 2: lock thread_watches + immediate weixin_account_id check
                    watch_row = await _lock_thread_watch_and_verify_weixin_set_in_tx(
                        cur, norm_tid, locked_wx_aids, op_name="claim_text"
                    )

                    # Level 7b-0: runtime config FOR SHARE
                    if _before_7b0_lock_hook is not None:
                        await _before_7b0_lock_hook("claim_text")
                    rt_cfg = await read_effective_interrupt_runtime_config_in_tx(
                        cur, for_update=False
                    )
                    if _after_7b0_lock_hook is not None:
                        await _after_7b0_lock_hook("claim_text")
                    if not rt_cfg["interrupt_ledger_enabled"]:
                        # Rollback implicitly (no writes done) and fall back to legacy_direct
                        return {
                            "ok": True,
                            "ledger_enabled": False,
                            "fallback_direct_dispatch": True,
                            "config_revision": rt_cfg["config_revision"],
                        }

                    # Level 7b-1: interrupt advisory lock
                    await _acquire_hitl_resume_lock_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )

                    # Level 7b-2: interrupt_resumes FOR UPDATE + unconditional claim_id check
                    resume_row = await _lock_interrupt_resume_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    actual_claim_id = str(resume_row["claim_id"]) if resume_row else None
                    if actual_claim_id != preview_claim_id:
                        raise _ResumeInboundLockSetChanged(
                            f"claim_text: claim_id drifted ({preview_claim_id} -> {actual_claim_id})"
                        )
                    if resume_row:
                        if (
                            resume_row.get("weixin_account_id")
                            and str(resume_row["weixin_account_id"]) not in locked_wx_aids
                        ):
                            raise _ResumeInboundLockSetChanged(
                                "claim_text: resume weixin_account_id missing from 3a set"
                            )
                        if resume_row.get("platform") and resume_row.get("inbound_message_id"):
                            rk_pair = (
                                str(resume_row["platform"]).strip().lower(),
                                str(resume_row["inbound_message_id"]).strip(),
                            )
                            if rk_pair not in locked_inb_key_set:
                                raise _ResumeInboundLockSetChanged(
                                    f"claim_text: resume inbound {rk_pair} missing from 0b-1/7a set"
                                )
                    if _after_7b2_lock_hook is not None:
                        await _after_7b2_lock_hook("claim_text")

                    # Level 7b-3: lock tokens for interrupt
                    all_tokens = await _lock_interrupt_tokens_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )

                    # Rule (c): idempotent replay of same inbound message
                    if (
                        resume_row is not None
                        and str(resume_row.get("platform") or "") == cleaned_plat
                        and str(resume_row.get("inbound_message_id") or "") == cleaned_mid
                    ):
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET status = 'hitl_claimed',
                                fallback_pending = FALSE,
                                raw_payload = COALESCE(raw_payload, '{}'::jsonb)
                                    || jsonb_build_object(
                                        'managed_by_interrupt_resume', TRUE,
                                        'claim_id', %s::text
                                    ),
                                updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                              AND status <> 'pending_resubmit'
                            """,
                            (str(resume_row["claim_id"]), cleaned_plat, cleaned_mid),
                        )
                        return {
                            "ok": True,
                            "ledger_enabled": True,
                            "idempotent_replay": True,
                            "claim_id": str(resume_row["claim_id"]),
                            "request_key": str(resume_row["request_key"]),
                            "resume_status": str(resume_row["resume_status"]),
                            "admitted_run_id": resume_row.get("admitted_run_id"),
                            "resume_payload": resume_row.get("resume_payload"),
                        }

                    # Verify pending_interrupt matches session/watch AND live checkpoint
                    sess_pi = sg_row.get("pending_interrupt") if isinstance(sg_row.get("pending_interrupt"), Mapping) else None
                    watch_pi = (
                        watch_row.get("pending_interrupt")
                        if watch_row and isinstance(watch_row.get("pending_interrupt"), Mapping)
                        else None
                    )
                    pi_ref = sess_pi or watch_pi
                    if pi_ref is not None:
                        if (
                            str(pi_ref.get("interrupt_id") or "") != str(interrupt_id)
                            or str(pi_ref.get("run_id") or "") != str(run_id)
                        ):
                            raise HTTPException(
                                status_code=409,
                                detail={"code": "pending_interrupt_mismatch", "message": "Pending interrupt does not match."},
                            )

                    live_rec = live_pending_by_id.get(str(interrupt_id))
                    if not live_rec:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "interrupt_no_longer_pending", "message": "Interrupt is no longer pending."},
                        )
                    live_val = live_rec.get("value") if isinstance(live_rec.get("value"), Mapping) else {}
                    outer_kind = str(live_val.get("kind") or "").strip().lower()
                    live_reqs = live_val.get("action_requests")
                    first_type = (
                        str(live_reqs[0].get("type") or "").strip().lower()
                        if isinstance(live_reqs, list) and len(live_reqs) == 1 and isinstance(live_reqs[0], Mapping)
                        else ""
                    )
                    if (
                        outer_kind not in ("", "ask_user_question", INTERRUPT_REASON_CREDIT_PAUSE)
                        or (first_type and first_type not in TEXT_CLAIM_ALLOWED_ACTION_TYPES)
                        or (not first_type and outer_kind not in TEXT_CLAIM_ALLOWED_ACTION_TYPES)
                    ):
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": "text_reply_not_allowed",
                                "message": "Tool approval interrupts cannot be settled via text reply.",
                            },
                        )

                    # Weixin live access, account status, and bot_generation check
                    wx_account_id: str | None = None
                    wx_bot_id: str | None = None
                    wx_bot_gen: int | None = None
                    wx_recipient_id: str | None = None
                    if cleaned_plat == "weixin":
                        roster_st = await check_team_member_status_live(
                            dalton_user_id, conn=owned_conn
                        )
                        if roster_st != "active":
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "member_not_in_roster", "message": "Member is not active."},
                            )
                        wx_tier = await get_user_tier(dalton_user_id, conn=owned_conn)
                        wx_cfg = await wx_db.ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=False
                        )
                        wx_ok, wx_deny = evaluate_weixin_member_access(
                            wx_tier,
                            channel_enabled=bool(wx_cfg["enabled"]),
                            db_allow_external=wx_cfg.get("allow_external"),
                        )
                        if not wx_ok:
                            raise HTTPException(
                                status_code=403,
                                detail={"code": wx_deny or "weixin_channel_disabled"},
                            )
                        in_wx_aid = in_row.get("weixin_account_id")
                        if not in_wx_aid:
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "weixin_account_missing"},
                            )
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id,
                                   ilink_bot_id,
                                   ilink_user_id,
                                   bot_generation,
                                   status
                            FROM dalton_channel_weixin_accounts
                            WHERE account_id = %s::uuid
                              AND user_id = %s
                            """,
                            (str(in_wx_aid), dalton_user_id),
                        )
                        wx_acc = await cur.fetchone()
                        if not wx_acc or str(wx_acc.get("status") or "") != "active":
                            raise HTTPException(
                                status_code=403,
                                detail={"code": "weixin_account_inactive"},
                            )
                        in_bot_gen = in_row.get("weixin_context_bot_generation")
                        if in_bot_gen is not None and int(in_bot_gen) != int(wx_acc["bot_generation"]):
                            raise HTTPException(
                                status_code=409,
                                detail={"code": "weixin_bot_generation_superseded"},
                            )
                        wx_account_id = str(wx_acc["account_id"])
                        wx_bot_id = str(wx_acc.get("ilink_bot_id") or "") or None
                        wx_bot_gen = int(wx_acc["bot_generation"])
                        wx_recipient_id = str(wx_acc.get("ilink_user_id") or "") or None

                    db_now = await cg_db._fetch_db_now(cur)
                    new_claim_id = str(uuid4())
                    new_request_key = derive_hitl_resume_request_key(
                        norm_tid, str(run_id), str(interrupt_id), new_claim_id
                    )
                    card_targets = await _collect_bound_card_targets_in_tx(
                        all_tokens,
                        existing_targets=(resume_row.get("card_targets") if resume_row else None),
                        enabled_platforms=enabled_platforms,
                    )
                    superseded_hashes = [
                        str(t["token_hash"])
                        for t in all_tokens
                        if t.get("consumed_at") is None
                    ]
                    old_inbound_to_complete: tuple[str, str] | None = None

                    if resume_row is None:
                        # Rule (a): first insert
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_interrupt_resumes (
                                thread_id, run_id, interrupt_id, claim_id,
                                settled_via, settled_by_user_id, platform, token_hash,
                                inbound_message_id, legacy_direct,
                                origin_scope_key, session_generation, origin_chat_address,
                                weixin_account_id, weixin_bot_id, weixin_bot_generation, weixin_recipient_id,
                                request_key, resume_payload,
                                superseded_token_hashes, card_targets,
                                resume_status, requires_gateway_ack, delivery_settled,
                                lease_expires_at, run_consumed_at, channel_ack_at,
                                attempt_count, next_retry_at, last_error
                            )
                            VALUES (
                                %s::uuid, %s, %s, %s::uuid,
                                'channel_text', %s, %s, NULL,
                                %s, FALSE,
                                %s, %s, %s,
                                %s::uuid, %s, %s, %s,
                                %s::uuid, %s,
                                %s, %s,
                                'pending_dispatch', TRUE, FALSE,
                                NOW() + (%s || ' seconds')::interval, NULL, NULL,
                                0, NULL, NULL
                            )
                            ON CONFLICT (thread_id, run_id, interrupt_id) DO NOTHING
                            """,
                            (
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                new_claim_id,
                                dalton_user_id,
                                cleaned_plat,
                                cleaned_mid,
                                cleaned_scope,
                                int(generation),
                                str(in_row.get("chat_address") or ""),
                                wx_account_id,
                                wx_bot_id,
                                wx_bot_gen,
                                wx_recipient_id,
                                new_request_key,
                                Jsonb(dict(resume_payload)),
                                Jsonb(superseded_hashes),
                                Jsonb(card_targets),
                                str(RESUME_LEASE_SECONDS),
                            ),
                        )
                        if cur.rowcount != 1:
                            raise _ResumeInboundLockSetChanged(
                                f"claim_text: insert rowcount={cur.rowcount}"
                            )
                    else:
                        r_st = str(resume_row["resume_status"])
                        lease_dt = _coerce_utc(resume_row.get("lease_expires_at"))
                        rule_b = False
                        rule_d = False
                        if r_st == "pending_dispatch" and lease_dt is not None and lease_dt < db_now:
                            ex_run = await _has_active_or_advanced_run_for_key(
                                cur, thread_id=norm_tid, request_key=str(resume_row["request_key"])
                            )
                            if not ex_run or str(ex_run.get("status") or "") not in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                                rule_b = True
                        elif (
                            r_st == "terminal_failed"
                            and str(resume_row.get("inbound_message_id") or "") != cleaned_mid
                        ):
                            if not await _has_in_progress_run_on_thread(cur, norm_tid):
                                ex_run = await _has_active_or_advanced_run_for_key(
                                    cur,
                                    thread_id=norm_tid,
                                    request_key=str(resume_row["request_key"]),
                                    admitted_run_id=resume_row.get("admitted_run_id"),
                                )
                                if not ex_run or str(ex_run.get("status") or "") not in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                                    rule_d = True

                        if not (rule_b or rule_d):
                            # Rollback leaving new inbound untouched ('pending') -> 409 already_settled
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": "already_settled",
                                    "settled_via": str(resume_row["settled_via"]),
                                    "resume_status": r_st,
                                },
                            )

                        if (
                            rule_b
                            and str(resume_row.get("settled_via") or "") == "channel_text"
                            and resume_row.get("platform")
                            and resume_row.get("inbound_message_id")
                            and (
                                str(resume_row["platform"]).strip().lower(),
                                str(resume_row["inbound_message_id"]).strip(),
                            )
                            != (cleaned_plat, cleaned_mid)
                        ):
                            old_inbound_to_complete = (
                                str(resume_row["platform"]).strip().lower(),
                                str(resume_row["inbound_message_id"]).strip(),
                            )

                        await cur.execute(
                            """
                            UPDATE dalton_channel_interrupt_resumes
                            SET claim_id = %s::uuid,
                                settled_via = 'channel_text',
                                settled_by_user_id = %s,
                                platform = %s,
                                token_hash = NULL,
                                inbound_message_id = %s,
                                legacy_direct = FALSE,
                                origin_scope_key = %s,
                                session_generation = %s,
                                origin_chat_address = %s,
                                weixin_account_id = %s::uuid,
                                weixin_bot_id = %s,
                                weixin_bot_generation = %s,
                                weixin_recipient_id = %s,
                                request_key = %s::uuid,
                                resume_payload = %s,
                                admitted_run_id = NULL,
                                resume_status = 'pending_dispatch',
                                lease_expires_at = NOW() + (%s || ' seconds')::interval,
                                run_consumed_at = NULL,
                                requires_gateway_ack = TRUE,
                                delivery_settled = FALSE,
                                channel_ack_at = NULL,
                                superseded_token_hashes = %s,
                                card_targets = %s,
                                attempt_count = 0,
                                next_retry_at = NULL,
                                last_error = NULL,
                                updated_at = NOW()
                            WHERE thread_id = %s::uuid
                              AND run_id = %s
                              AND interrupt_id = %s
                              AND claim_id = %s::uuid
                            """,
                            (
                                new_claim_id,
                                dalton_user_id,
                                cleaned_plat,
                                cleaned_mid,
                                cleaned_scope,
                                int(generation),
                                str(in_row.get("chat_address") or ""),
                                wx_account_id,
                                wx_bot_id,
                                wx_bot_gen,
                                wx_recipient_id,
                                new_request_key,
                                Jsonb(dict(resume_payload)),
                                str(RESUME_LEASE_SECONDS),
                                Jsonb(superseded_hashes),
                                Jsonb(card_targets),
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                preview_claim_id,
                            ),
                        )
                        if cur.rowcount != 1:
                            raise _ResumeInboundLockSetChanged(
                                f"claim_text: takeover update rowcount={cur.rowcount}"
                            )

                    # ONLY after cur.rowcount == 1: settle old inbound, mark tokens text_reply, set new inbound hitl_claimed
                    if old_inbound_to_complete and old_inbound_to_complete in locked_inbounds:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET status = 'completed',
                                fallback_pending = FALSE,
                                raw_payload = COALESCE(raw_payload, '{}'::jsonb)
                                    || jsonb_build_object('hitl_superseded_by', 'web_or_button_or_newer_text'),
                                updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            """,
                            old_inbound_to_complete,
                        )
                    for t in all_tokens:
                        if t.get("consumed_at") is not None:
                            continue
                        await cur.execute(
                            """
                            UPDATE dalton_channel_action_tokens
                            SET consumed_via = 'text_reply',
                                consumed_at = NOW()
                            WHERE token_hash = %s
                            """,
                            (str(t["token_hash"]),),
                        )
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = 'hitl_claimed',
                            fallback_pending = FALSE,
                            raw_payload = COALESCE(raw_payload, '{}'::jsonb)
                                || jsonb_build_object(
                                    'managed_by_interrupt_resume', TRUE,
                                    'claim_id', %s::text
                                ),
                            updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        """,
                        (new_claim_id, cleaned_plat, cleaned_mid),
                    )
                    return {
                        "ok": True,
                        "ledger_enabled": True,
                        "idempotent_replay": False,
                        "claim_id": new_claim_id,
                        "request_key": new_request_key,
                        "resume_status": "pending_dispatch",
                        "resume_payload": dict(resume_payload),
                        "card_targets": card_targets,
                    }

    return await _run_interrupt_tx_with_retry("claim_channel_text_interrupt_resume", conn, _attempt)


# ---------------------------------------------------------------------------
# 6. Ack Interrupt Resume (POST /internal/interrupt-resumes/ack)
# ---------------------------------------------------------------------------


def _apply_card_results_to_targets(
    existing_targets: Any,
    card_results: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Apply per-card patch outcomes to ``card_targets``; return ``(updated, any_pending_retry)``."""
    targets = [dict(e) for e in (existing_targets or []) if isinstance(e, Mapping)]
    by_hash = {str(e.get("token_hash") or ""): e for e in targets}
    any_retry = False
    for res in card_results:
        th = str(res.get("token_hash") or "").strip()
        if not th or th not in by_hash:
            continue
        entry = by_hash[th]
        if str(entry.get("patch_status") or "") in CARD_TERMINAL_PATCH_STATUSES:
            continue
        st = str(res.get("patch_status") or "").strip().lower()
        if st in CARD_TERMINAL_PATCH_STATUSES:
            entry["patch_status"] = st
            if res.get("error"):
                entry["last_error"] = str(res["error"])
        else:
            attempts = int(entry.get("attempt_count") or 0) + 1
            entry["attempt_count"] = attempts
            if res.get("error"):
                entry["last_error"] = str(res["error"])
            if attempts >= MAX_CARD_PATCH_ATTEMPTS:
                entry["patch_status"] = "failed_exhausted"
            else:
                entry["patch_status"] = "pending"
                any_retry = True
    return targets, any_retry


def _has_pending_card_targets(targets: Sequence[Mapping[str, Any]]) -> bool:
    return any(str(e.get("patch_status") or "") == "pending" for e in targets)


async def ack_channel_interrupt_resume(
    *,
    thread_id: str | None = None,
    run_id: str | None = None,
    interrupt_id: str | None = None,
    claim_id: str | None = None,
    outcome: str | None = None,
    settle_delivery: bool = False,
    error: str | None = None,
    card_results: Sequence[Mapping[str, Any]] | None = None,
    late_card_results: Sequence[Mapping[str, Any]] | None = None,
    gateway_epoch: int | str | None = None,
    require_gateway_epoch: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """Separate origin-channel dispatch/delivery settlement from per-card & late-card ack (Lock Order ⑤)."""
    cleaned_outcome = str(outcome or "").strip().lower() or None
    if cleaned_outcome is not None and cleaned_outcome not in (
        "retry_scheduled",
        "rekey_cancelled_before_consume",
        "terminal_failed",
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_ack_outcome", "message": f"Unsupported ack outcome: {outcome!r}"},
        )
    has_origin_branch = cleaned_outcome is not None or bool(settle_delivery)
    card_res_list = list(card_results or [])
    late_res_list = list(late_card_results or [])
    if not has_origin_branch and not card_res_list and not late_res_list:
        raise HTTPException(
            status_code=400,
            detail={"code": "empty_ack", "message": "Ack must specify outcome, settle_delivery, card_results, or late_card_results."},
        )

    norm_tid = normalize_uuid(thread_id) if thread_id else None
    has_resume_ref = bool(norm_tid and run_id and interrupt_id)
    if (has_origin_branch or card_res_list) and not has_resume_ref:
        raise HTTPException(
            status_code=400,
            detail={"code": "missing_resume_ref", "message": "thread_id, run_id, and interrupt_id are required."},
        )

    # Read-only authoritative platform checks BEFORE parsing gateway_epoch
    auth_platforms_checked: set[str] = set()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            if has_resume_ref:
                await cur.execute(
                    """
                    SELECT platform, card_targets
                    FROM dalton_channel_interrupt_resumes
                    WHERE thread_id = %s::uuid
                      AND run_id = %s
                      AND interrupt_id = %s
                    """,
                    (norm_tid, str(run_id), str(interrupt_id)),
                )
                pre_res = await cur.fetchone()
                if not pre_res:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "interrupt_resume_not_found", "message": "Interrupt resume not found."},
                    )
                if has_origin_branch:
                    p = require_known_channel_platform(
                        pre_res.get("platform"),
                        allowed=ALL_CHANNEL_PLATFORMS,
                        invalid_status=409,
                    )
                    auth_platforms_checked.add(p)
                if card_res_list:
                    ct_by_hash = {
                        str(e.get("token_hash") or ""): e
                        for e in (pre_res.get("card_targets") or [])
                        if isinstance(e, Mapping)
                    }
                    for cr in card_res_list:
                        th = str(cr.get("token_hash") or "").strip()
                        c_entry = ct_by_hash.get(th)
                        if not c_entry:
                            raise HTTPException(
                                status_code=404,
                                detail={"code": "card_target_not_found", "token_hash": th},
                            )
                        cp = require_known_channel_platform(
                            c_entry.get("platform"),
                            allowed=CARD_PLATFORMS,
                            invalid_status=409,
                        )
                        auth_platforms_checked.add(cp)

            if late_res_list:
                late_hashes = sorted({str(r.get("token_hash") or "").strip() for r in late_res_list if r.get("token_hash")})
                if not late_hashes:
                    raise HTTPException(
                        status_code=400,
                        detail={"code": "missing_token_hash", "message": "late_card_results entries require token_hash."},
                    )
                await cur.execute(
                    """
                    SELECT token_hash, platform
                    FROM dalton_channel_action_tokens
                    WHERE token_hash = ANY(%s::varchar[])
                    """,
                    (late_hashes,),
                )
                found_late = {str(r["token_hash"]): r for r in await cur.fetchall()}
                for lh in late_hashes:
                    if lh not in found_late:
                        raise HTTPException(
                            status_code=404,
                            detail={"code": "token_not_found", "token_hash": lh},
                        )
                    lp = require_known_channel_platform(
                        found_late[lh].get("platform"),
                        allowed=CARD_PLATFORMS,
                        invalid_status=409,
                    )
                    auth_platforms_checked.add(lp)

    parsed_epoch = _parse_epoch_strict(gateway_epoch, required=require_gateway_epoch)
    epoch_plat = "weixin" if "weixin" in auth_platforms_checked else (next(iter(auth_platforms_checked), None))

    # Pure card-only / late-card ack path: 0b-2 -> [7b-1 -> 7b-2] -> 7b-3
    if not has_origin_branch:
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    await cur.execute("SET LOCAL statement_timeout = '2s'")
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        parsed_epoch,
                        required=require_gateway_epoch,
                        platform=epoch_plat,
                        enforce_post_lease_for_non_weixin=False,
                    )
                    acked_resume = False
                    channel_ack_at_val: Any = None
                    if card_res_list and has_resume_ref:
                        assert norm_tid is not None
                        await _acquire_hitl_resume_lock_in_tx(
                            cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                        )
                        resume_row = await _lock_interrupt_resume_in_tx(
                            cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                        )
                        if not resume_row:
                            raise HTTPException(status_code=404, detail={"code": "interrupt_resume_not_found"})
                        # Shared 3-way gate check!
                        if not compute_can_patch_terminal(resume_row):
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": "resume_not_ready_for_card_ack",
                                    "message": "Resume has not passed the terminal card patch gate.",
                                },
                            )
                        updated_targets, any_retry = _apply_card_results_to_targets(
                            resume_row.get("card_targets"), card_res_list
                        )
                        all_done = not _has_pending_card_targets(updated_targets)
                        if all_done:
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET card_targets = %s,
                                    channel_ack_at = COALESCE(channel_ack_at, NOW()),
                                    next_retry_at = NULL,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid
                                  AND run_id = %s
                                  AND interrupt_id = %s
                                RETURNING channel_ack_at
                                """,
                                (Jsonb(updated_targets), norm_tid, str(run_id), str(interrupt_id)),
                            )
                            u_r = await cur.fetchone()
                            channel_ack_at_val = (u_r or {}).get("channel_ack_at")
                            acked_resume = True
                        else:
                            max_att = max(
                                (int(e.get("attempt_count") or 0) for e in updated_targets),
                                default=1,
                            )
                            backoff_s = int(_backoff_seconds(max_att)) if any_retry else 5
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET card_targets = %s,
                                    next_retry_at = NOW() + (%s || ' seconds')::interval,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid
                                  AND run_id = %s
                                  AND interrupt_id = %s
                                """,
                                (Jsonb(updated_targets), str(backoff_s), norm_tid, str(run_id), str(interrupt_id)),
                            )

                    updated_late: list[dict[str, Any]] = []
                    if late_res_list:
                        late_by_hash = {
                            str(r["token_hash"]).strip(): r
                            for r in late_res_list
                            if r.get("token_hash")
                        }
                        sorted_hashes = sorted(late_by_hash.keys())
                        await cur.execute(
                            """
                            SELECT token_hash, late_patch_status, late_patch_attempts
                            FROM dalton_channel_action_tokens
                            WHERE token_hash = ANY(%s::varchar[])
                            ORDER BY token_hash ASC
                            FOR UPDATE
                            """,
                            (sorted_hashes,),
                        )
                        for trow in await cur.fetchall():
                            th = str(trow["token_hash"])
                            req_item = late_by_hash[th]
                            st = str(req_item.get("patch_status") or "").strip().lower()
                            err_txt = str(req_item.get("error") or "") or None
                            if st in CARD_TERMINAL_PATCH_STATUSES:
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_action_tokens
                                    SET late_patch_status = %s,
                                        late_patch_next_retry_at = NULL,
                                        late_patch_last_error = COALESCE(%s, late_patch_last_error)
                                    WHERE token_hash = %s
                                    """,
                                    (st, err_txt, th),
                                )
                                updated_late.append({"token_hash": th, "late_patch_status": st})
                            else:
                                att = int(trow.get("late_patch_attempts") or 0) + 1
                                if att >= MAX_CARD_PATCH_ATTEMPTS:
                                    await cur.execute(
                                        """
                                        UPDATE dalton_channel_action_tokens
                                        SET late_patch_status = 'failed_exhausted',
                                            late_patch_attempts = %s,
                                            late_patch_next_retry_at = NULL,
                                            late_patch_last_error = %s
                                        WHERE token_hash = %s
                                        """,
                                        (att, err_txt, th),
                                    )
                                    updated_late.append({"token_hash": th, "late_patch_status": "failed_exhausted"})
                                else:
                                    b_s = int(_backoff_seconds(att))
                                    await cur.execute(
                                        """
                                        UPDATE dalton_channel_action_tokens
                                        SET late_patch_status = 'pending',
                                            late_patch_attempts = %s,
                                            late_patch_next_retry_at = NOW() + (%s || ' seconds')::interval,
                                            late_patch_last_error = %s
                                        WHERE token_hash = %s
                                        """,
                                        (att, str(b_s), err_txt, th),
                                    )
                                    updated_late.append({"token_hash": th, "late_patch_status": "pending"})

                    return {
                        "ok": True,
                        "acked": acked_resume,
                        "channel_ack_at": _iso(channel_ack_at_val),
                        "late_card_results": updated_late,
                    }

    # Origin-channel branch (Lock Order ⑤ origin path with full retry on lock-set/claim_id drift)
    assert norm_tid is not None and run_id is not None and interrupt_id is not None
    live_pending_by_id: dict[str, dict[str, Any]] = {}
    if cleaned_outcome in ("rekey_cancelled_before_consume", "terminal_failed"):
        try:
            live_pending_by_id, _ = await load_live_pending_interrupts(norm_tid)
        except Exception:
            live_pending_by_id = {}

    async def _attempt_origin(conn_arg: Any) -> dict[str, Any]:
        preview = await _preview_interrupt_context(
            thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id), conn=conn_arg
        )
        if _after_preview_hook is not None:
            await _after_preview_hook("ack_origin")
        preview_claim_id = preview["preview_claim_id"]
        preview_inb_keys = preview["inbound_keys"]
        preview_wx_aids = list(preview["weixin_account_ids"])
        prev_res = preview["preview_resume"]
        enabled_platforms = set(get_enabled_channel_platforms())

        async with get_db_connection(conn_arg) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    await cur.execute("SET LOCAL statement_timeout = '2s'")

                    # Level 0b-1
                    locked_inb_key_list = await _acquire_inbound_advisory_locks_in_tx(
                        cur, preview_inb_keys
                    )
                    locked_inb_key_set = set(locked_inb_key_list)

                    # Level 0b-2
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        parsed_epoch,
                        required=require_gateway_epoch,
                        platform=epoch_plat,
                        enforce_post_lease_for_non_weixin=False,
                    )

                    # Level 3a
                    locked_wx_aids = await _acquire_weixin_account_key_share_in_tx(
                        cur, preview_wx_aids
                    )

                    # Level 6: session_generations FOR UPDATE (needed when re-arming pending_interrupt)
                    if (
                        prev_res
                        and prev_res.get("platform")
                        and prev_res.get("origin_scope_key")
                        and prev_res.get("settled_by_user_id")
                    ):
                        await cur.execute(
                            """
                            SELECT generation
                            FROM dalton_channel_session_generations
                            WHERE platform = %s
                              AND scope_key = %s
                              AND dalton_user_id = %s
                            FOR UPDATE
                            """,
                            (
                                str(prev_res["platform"]),
                                str(prev_res["origin_scope_key"]),
                                str(prev_res["settled_by_user_id"]),
                            ),
                        )

                    # Level 7a step 1: inbound_events FOR UPDATE + IMMEDIATE Weixin check
                    locked_inbounds = await _lock_inbound_events_and_verify_weixin_set_in_tx(
                        cur, locked_inb_key_list, locked_wx_aids, op_name="ack_origin"
                    )

                    # Level 7a step 2: thread_watches FOR UPDATE + immediate Weixin check
                    await _lock_thread_watch_and_verify_weixin_set_in_tx(
                        cur, norm_tid, locked_wx_aids, op_name="ack_origin"
                    )

                    # Level 7b-0: runtime config FOR SHARE
                    rt_cfg = await read_effective_interrupt_runtime_config_in_tx(
                        cur, for_update=False
                    )

                    # Level 7b-1 -> 7b-2
                    await _acquire_hitl_resume_lock_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    resume_row = await _lock_interrupt_resume_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    actual_claim_id = str(resume_row["claim_id"]) if resume_row else None
                    if actual_claim_id != preview_claim_id:
                        raise _ResumeInboundLockSetChanged(
                            f"ack_origin: claim_id drifted ({preview_claim_id} -> {actual_claim_id})"
                        )
                    if not resume_row:
                        raise HTTPException(
                            status_code=404, detail={"code": "interrupt_resume_not_found"}
                        )
                    if (
                        resume_row.get("weixin_account_id")
                        and str(resume_row["weixin_account_id"]) not in locked_wx_aids
                    ):
                        raise _ResumeInboundLockSetChanged(
                            "ack_origin: resume weixin_account_id missing from 3a set"
                        )
                    if resume_row.get("platform") and resume_row.get("inbound_message_id"):
                        rk_pair = (
                            str(resume_row["platform"]).strip().lower(),
                            str(resume_row["inbound_message_id"]).strip(),
                        )
                        if rk_pair not in locked_inb_key_set:
                            raise _ResumeInboundLockSetChanged(
                                f"ack_origin: resume inbound {rk_pair} missing from 0b-1/7a set"
                            )
                    if claim_id and str(resume_row["claim_id"]) != str(claim_id).strip():
                        raise HTTPException(
                            status_code=409,
                            detail={"code": "interrupt_claim_mismatch", "message": "claim_id mismatch."},
                        )

                    cur_claim_id = str(resume_row["claim_id"])
                    r_plat = str(resume_row.get("platform") or "").strip().lower()
                    inb_pair = (
                        (r_plat, str(resume_row["inbound_message_id"]).strip())
                        if r_plat and resume_row.get("inbound_message_id")
                        else None
                    )
                    inb_row = locked_inbounds.get(inb_pair) if inb_pair else None

                    if cleaned_outcome == "retry_scheduled":
                        next_att = int(resume_row.get("attempt_count") or 0) + 1
                        if next_att >= MAX_RESUME_RETRIES:
                            cleaned_err = str(error or "max_attempts_exceeded")
                            deliv_settled = str(resume_row["settled_via"]) == "channel_button"
                            targets = [
                                dict(e)
                                for e in (resume_row.get("card_targets") or [])
                                if isinstance(e, Mapping)
                            ]
                            ack_now = deliv_settled and not _has_pending_card_targets(targets)
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET resume_status = 'terminal_failed',
                                    delivery_settled = %s,
                                    channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                                    attempt_count = %s,
                                    last_error = %s,
                                    lease_expires_at = NULL,
                                    next_retry_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s AND claim_id = %s::uuid
                                """,
                                (
                                    deliv_settled,
                                    ack_now,
                                    next_att,
                                    cleaned_err,
                                    ack_now,
                                    norm_tid,
                                    str(run_id),
                                    str(interrupt_id),
                                    cur_claim_id,
                                ),
                            )
                            if inb_pair and inb_row and not _is_credit_supplement_pending_resubmit(inb_row):
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_inbound_events
                                    SET status = 'failed', fallback_pending = FALSE, updated_at = NOW()
                                    WHERE platform = %s AND message_id = %s
                                    """,
                                    inb_pair,
                                )
                            await _rearm_pending_interrupt_in_tx(
                                cur,
                                thread_id=norm_tid,
                                run_id=str(run_id),
                                interrupt_id=str(interrupt_id),
                                platform=r_plat or None,
                                origin_scope_key=resume_row.get("origin_scope_key"),
                                dalton_user_id=resume_row.get("settled_by_user_id"),
                                session_generation=resume_row.get("session_generation"),
                                pending_record=live_pending_by_id.get(str(interrupt_id)),
                            )
                            return {
                                "ok": True,
                                "resume_status": "terminal_failed",
                                "attempt_count": next_att,
                                "delivery_settled": deliv_settled,
                            }

                        b_s = int(_backoff_seconds(next_att))
                        await cur.execute(
                            """
                            UPDATE dalton_channel_interrupt_resumes
                            SET attempt_count = %s,
                                lease_expires_at = NOW() + (%s || ' seconds')::interval,
                                next_retry_at = NOW() + (%s || ' seconds')::interval,
                                last_error = %s,
                                updated_at = NOW()
                            WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s AND claim_id = %s::uuid
                            """,
                            (
                                next_att,
                                str(DISPATCH_LEASE_SECONDS),
                                str(b_s),
                                str(error or "retry_scheduled"),
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                cur_claim_id,
                            ),
                        )
                        return {
                            "ok": True,
                            "resume_status": str(resume_row["resume_status"]),
                            "attempt_count": next_att,
                            "backoff_seconds": b_s,
                        }

                    if cleaned_outcome == "rekey_cancelled_before_consume":
                        admitted_rid = resume_row.get("admitted_run_id")
                        run_row = await _has_active_or_advanced_run_for_key(
                            cur,
                            thread_id=norm_tid,
                            request_key=str(resume_row["request_key"]),
                            admitted_run_id=admitted_rid,
                        )
                        att = int(resume_row.get("attempt_count") or 0)
                        has_other_active = await _has_in_progress_run_on_thread(cur, norm_tid)
                        if (
                            resume_row.get("run_consumed_at") is None
                            and not bool(resume_row.get("legacy_direct"))
                            and str(resume_row["settled_via"]) in ("channel_button", "channel_text")
                            and is_retryable_disconnect_cancel(run_row)
                            and not has_other_active
                            and rt_cfg["interrupt_ledger_enabled"]
                            and r_plat in enabled_platforms
                            and att < MAX_RESUME_RETRIES
                        ):
                            new_cid = str(uuid4())
                            new_rk = derive_hitl_resume_request_key(
                                norm_tid, str(run_id), str(interrupt_id), new_cid
                            )
                            b_s = int(_backoff_seconds(att + 1))
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET claim_id = %s::uuid,
                                    request_key = %s::uuid,
                                    admitted_run_id = NULL,
                                    resume_status = 'pending_dispatch',
                                    lease_expires_at = NOW() + (%s || ' seconds')::interval,
                                    attempt_count = attempt_count + 1,
                                    next_retry_at = NOW() + (%s || ' seconds')::interval,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid
                                  AND run_id = %s
                                  AND interrupt_id = %s
                                  AND claim_id = %s::uuid
                                """,
                                (
                                    new_cid,
                                    new_rk,
                                    str(DISPATCH_LEASE_SECONDS),
                                    str(b_s),
                                    norm_tid,
                                    str(run_id),
                                    str(interrupt_id),
                                    cur_claim_id,
                                ),
                            )
                            if cur.rowcount != 1:
                                raise _ResumeInboundLockSetChanged("ack_origin: rekey rowcount != 1")
                            return {
                                "ok": True,
                                "rekeyed": True,
                                "resume_status": "pending_dispatch",
                                "claim_id": new_cid,
                                "request_key": new_rk,
                                "attempt_count": att + 1,
                            }

                        # Branch B: complete terminal convergence
                        reason_code = _classify_non_retryable_cancel_reason(run_row)
                        if att >= MAX_RESUME_RETRIES and is_retryable_disconnect_cancel(run_row):
                            reason_code = "max_attempts_exceeded"
                        is_explicit_stop = asked_for(run_row)
                        deliv_settled = (
                            str(resume_row["settled_via"]) == "channel_button"
                            or is_explicit_stop
                            or r_plat not in enabled_platforms
                        )
                        targets = [
                            dict(e)
                            for e in (resume_row.get("card_targets") or [])
                            if isinstance(e, Mapping)
                        ]
                        ack_now = deliv_settled and not _has_pending_card_targets(targets)
                        await cur.execute(
                            """
                            UPDATE dalton_channel_interrupt_resumes
                            SET resume_status = 'terminal_failed',
                                delivery_settled = %s,
                                channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                                lease_expires_at = NULL,
                                last_error = %s,
                                next_retry_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                                updated_at = NOW()
                            WHERE thread_id = %s::uuid
                              AND run_id = %s
                              AND interrupt_id = %s
                              AND claim_id = %s::uuid
                            """,
                            (
                                deliv_settled,
                                ack_now,
                                reason_code,
                                ack_now,
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                cur_claim_id,
                            ),
                        )
                        if inb_pair and inb_row and not _is_credit_supplement_pending_resubmit(inb_row):
                            inb_status = "completed" if is_explicit_stop else "failed"
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET status = %s, fallback_pending = FALSE, updated_at = NOW()
                                WHERE platform = %s AND message_id = %s
                                """,
                                (inb_status, inb_pair[0], inb_pair[1]),
                            )
                        await _rearm_pending_interrupt_in_tx(
                            cur,
                            thread_id=norm_tid,
                            run_id=str(run_id),
                            interrupt_id=str(interrupt_id),
                            platform=r_plat or None,
                            origin_scope_key=resume_row.get("origin_scope_key"),
                            dalton_user_id=resume_row.get("settled_by_user_id"),
                            session_generation=resume_row.get("session_generation"),
                            pending_record=live_pending_by_id.get(str(interrupt_id)),
                        )
                        return {
                            "ok": True,
                            "rekeyed": False,
                            "resume_status": "terminal_failed",
                            "last_error": reason_code,
                            "delivery_settled": deliv_settled,
                        }

                    if cleaned_outcome == "terminal_failed":
                        deliv_settled = (
                            bool(settle_delivery)
                            or str(resume_row["settled_via"]) == "channel_button"
                            or r_plat not in enabled_platforms
                        )
                        targets = [
                            dict(e)
                            for e in (resume_row.get("card_targets") or [])
                            if isinstance(e, Mapping)
                        ]
                        if card_res_list and deliv_settled:
                            targets, _ = _apply_card_results_to_targets(targets, card_res_list)
                        ack_now = deliv_settled and not _has_pending_card_targets(targets)
                        await cur.execute(
                            """
                            UPDATE dalton_channel_interrupt_resumes
                            SET resume_status = 'terminal_failed',
                                delivery_settled = %s,
                                card_targets = %s,
                                channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                                lease_expires_at = NULL,
                                last_error = COALESCE(%s, last_error),
                                next_retry_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                                updated_at = NOW()
                            WHERE thread_id = %s::uuid
                              AND run_id = %s
                              AND interrupt_id = %s
                              AND claim_id = %s::uuid
                            """,
                            (
                                deliv_settled,
                                Jsonb(targets),
                                ack_now,
                                str(error) if error else None,
                                ack_now,
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                cur_claim_id,
                            ),
                        )
                        if inb_pair and inb_row and not _is_credit_supplement_pending_resubmit(inb_row):
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET status = 'failed', fallback_pending = FALSE, updated_at = NOW()
                                WHERE platform = %s AND message_id = %s
                                """,
                                inb_pair,
                            )
                        await _rearm_pending_interrupt_in_tx(
                            cur,
                            thread_id=norm_tid,
                            run_id=str(run_id),
                            interrupt_id=str(interrupt_id),
                            platform=r_plat or None,
                            origin_scope_key=resume_row.get("origin_scope_key"),
                            dalton_user_id=resume_row.get("settled_by_user_id"),
                            session_generation=resume_row.get("session_generation"),
                            pending_record=live_pending_by_id.get(str(interrupt_id)),
                        )
                        return {
                            "ok": True,
                            "resume_status": "terminal_failed",
                            "delivery_settled": deliv_settled,
                            "acked": ack_now,
                        }

                    # settle_delivery == True
                    r_st = str(resume_row["resume_status"])
                    ready_for_settle = r_st == "terminal_failed" or (
                        r_st == "admitted" and resume_row.get("run_consumed_at") is not None
                    )
                    if not ready_for_settle:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "resume_not_ready_for_delivery_settle",
                                "message": "Resume must be admitted+consumed or terminal_failed before settling delivery.",
                            },
                        )

                    targets = [
                        dict(e)
                        for e in (resume_row.get("card_targets") or [])
                        if isinstance(e, Mapping)
                    ]
                    if card_res_list:
                        targets, _ = _apply_card_results_to_targets(targets, card_res_list)
                    ack_now = not _has_pending_card_targets(targets)
                    await cur.execute(
                        """
                        UPDATE dalton_channel_interrupt_resumes
                        SET delivery_settled = TRUE,
                            card_targets = %s,
                            channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                            next_retry_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                            updated_at = NOW()
                        WHERE thread_id = %s::uuid
                          AND run_id = %s
                          AND interrupt_id = %s
                          AND claim_id = %s::uuid
                        RETURNING channel_ack_at
                        """,
                        (
                            Jsonb(targets),
                            ack_now,
                            ack_now,
                            norm_tid,
                            str(run_id),
                            str(interrupt_id),
                            cur_claim_id,
                        ),
                    )
                    upd_res = await cur.fetchone()

                    # Settle associated inbound_events — PRESERVE pending_resubmit for credit_supplement!
                    preserved_pending_resubmit = False
                    if inb_pair and inb_row:
                        if _is_credit_supplement_pending_resubmit(inb_row):
                            preserved_pending_resubmit = True
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET raw_payload = COALESCE(raw_payload, '{}'::jsonb)
                                        || jsonb_build_object('hitl_resume_settled', TRUE),
                                    updated_at = NOW()
                                WHERE platform = %s AND message_id = %s
                                """,
                                inb_pair,
                            )
                        else:
                            final_inb_st = "completed" if r_st == "admitted" else "failed"
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET status = %s,
                                    fallback_pending = FALSE,
                                    updated_at = NOW()
                                WHERE platform = %s AND message_id = %s
                                """,
                                (final_inb_st, inb_pair[0], inb_pair[1]),
                            )

                    return {
                        "ok": True,
                        "resume_status": r_st,
                        "delivery_settled": True,
                        "acked": ack_now,
                        "channel_ack_at": _iso((upd_res or {}).get("channel_ack_at")),
                        "preserved_pending_resubmit": preserved_pending_resubmit,
                    }

    return await _run_interrupt_tx_with_retry("ack_channel_interrupt_resume", conn, _attempt_origin)


# ---------------------------------------------------------------------------
# 7. Web / Legacy-Direct Claim, START Admission, Release, and RecoveryScanner
#    (Used by Step 2 real-PG concurrency tests ⑦/⑨/⑩/⑪ and Step 3 hooks)
# ---------------------------------------------------------------------------


async def claim_web_interrupt_resume(
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    user_id: str,
    resume_payload: Mapping[str, Any],
    request_key: str | None = None,
    legacy_direct: bool = False,
    legacy_platform: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Web interactive session (Lock Order ⑦) or legacy_direct (Lock Order ⑧) claim/takeover."""
    norm_tid = normalize_uuid(thread_id)
    if not norm_tid:
        raise HTTPException(status_code=400, detail={"code": "invalid_thread_id"})
    reject_company_system_auth(user_id)
    settled_via = "channel_text" if legacy_direct else "web"
    eff_plat = str(legacy_platform).strip().lower() if legacy_direct and legacy_platform else None

    async def _attempt(conn_arg: Any) -> dict[str, Any]:
        preview = await _preview_interrupt_context(
            thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id), conn=conn_arg
        )
        if _after_preview_hook is not None:
            await _after_preview_hook("claim_web")
        preview_claim_id = preview["preview_claim_id"]
        preview_inb_keys = preview["inbound_keys"]
        preview_wx_aids = list(preview["weixin_account_ids"])
        enabled_platforms = set(get_enabled_channel_platforms())

        async with get_db_connection(conn_arg) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    await cur.execute("SET LOCAL statement_timeout = '2s'")

                    # Level 0b-1
                    locked_inb_key_list = await _acquire_inbound_advisory_locks_in_tx(
                        cur, preview_inb_keys
                    )
                    locked_inb_key_set = set(locked_inb_key_list)
                    # Level 3a
                    locked_wx_aids = await _acquire_weixin_account_key_share_in_tx(
                        cur, preview_wx_aids
                    )
                    # Level 7a (inbound_events FOR UPDATE + immediate Weixin lock-set check)
                    locked_inbounds = await _lock_inbound_events_and_verify_weixin_set_in_tx(
                        cur, locked_inb_key_list, locked_wx_aids, op_name="claim_web"
                    )
                    # Level 7b-0
                    if _before_7b0_lock_hook is not None:
                        await _before_7b0_lock_hook("claim_web")
                    rt_cfg = await read_effective_interrupt_runtime_config_in_tx(
                        cur, for_update=False
                    )
                    # Level 7b-1 -> 7b-2
                    await _acquire_hitl_resume_lock_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    resume_row = await _lock_interrupt_resume_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    actual_claim_id = str(resume_row["claim_id"]) if resume_row else None
                    if actual_claim_id != preview_claim_id:
                        raise _ResumeInboundLockSetChanged(
                            f"claim_web: claim_id drifted ({preview_claim_id} -> {actual_claim_id})"
                        )
                    if resume_row:
                        if (
                            resume_row.get("weixin_account_id")
                            and str(resume_row["weixin_account_id"]) not in locked_wx_aids
                        ):
                            raise _ResumeInboundLockSetChanged("claim_web: resume weixin_account_id unlocked")
                        if resume_row.get("platform") and resume_row.get("inbound_message_id"):
                            rk_pair = (
                                str(resume_row["platform"]).strip().lower(),
                                str(resume_row["inbound_message_id"]).strip(),
                            )
                            if rk_pair not in locked_inb_key_set:
                                raise _ResumeInboundLockSetChanged("claim_web: resume inbound unlocked")
                    if _after_7b2_lock_hook is not None:
                        await _after_7b2_lock_hook("claim_web")

                    # Level 7b-3
                    all_tokens = await _lock_interrupt_tokens_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )

                    db_now = await cg_db._fetch_db_now(cur)
                    new_claim_id = str(uuid4())
                    eff_request_key = (
                        str(normalize_uuid(request_key))
                        if request_key and normalize_uuid(request_key)
                        else derive_hitl_resume_request_key(
                            norm_tid, str(run_id), str(interrupt_id), new_claim_id
                        )
                    )

                    if rt_cfg["interrupt_ledger_enabled"]:
                        card_targets = await _collect_bound_card_targets_in_tx(
                            all_tokens,
                            existing_targets=(resume_row.get("card_targets") if resume_row else None),
                            enabled_platforms=enabled_platforms,
                        )
                    else:
                        existing_ct = [
                            dict(e)
                            for e in ((resume_row or {}).get("card_targets") or [])
                            if isinstance(e, Mapping)
                        ]
                        card_targets = existing_ct if _has_pending_card_targets(existing_ct) else []

                    has_pending_cards = _has_pending_card_targets(card_targets)
                    req_gw_ack = has_pending_cards
                    deliv_settled = not has_pending_cards
                    superseded_hashes = [
                        str(t["token_hash"]) for t in all_tokens if t.get("consumed_at") is None
                    ]
                    old_inb_to_complete: tuple[str, str] | None = None

                    if resume_row is None:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_interrupt_resumes (
                                thread_id, run_id, interrupt_id, claim_id,
                                settled_via, settled_by_user_id, platform, token_hash,
                                inbound_message_id, legacy_direct,
                                request_key, resume_payload,
                                superseded_token_hashes, card_targets,
                                resume_status, requires_gateway_ack, delivery_settled,
                                lease_expires_at, run_consumed_at, channel_ack_at,
                                attempt_count, next_retry_at, last_error
                            )
                            VALUES (
                                %s::uuid, %s, %s, %s::uuid,
                                %s, %s, %s, NULL,
                                NULL, %s,
                                %s::uuid, %s,
                                %s, %s,
                                'pending_dispatch', %s, %s,
                                NOW() + (%s || ' seconds')::interval, NULL,
                                CASE WHEN %s THEN NULL ELSE NOW() END,
                                0, NULL, NULL
                            )
                            ON CONFLICT (thread_id, run_id, interrupt_id) DO NOTHING
                            """,
                            (
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                new_claim_id,
                                settled_via,
                                user_id,
                                eff_plat,
                                bool(legacy_direct),
                                eff_request_key,
                                Jsonb(dict(resume_payload)),
                                Jsonb(superseded_hashes),
                                Jsonb(card_targets),
                                req_gw_ack,
                                deliv_settled,
                                str(RESUME_LEASE_SECONDS),
                                has_pending_cards,
                            ),
                        )
                        if cur.rowcount != 1:
                            raise _ResumeInboundLockSetChanged("claim_web: insert rowcount != 1")
                    else:
                        r_st = str(resume_row["resume_status"])
                        lease_dt = _coerce_utc(resume_row.get("lease_expires_at"))
                        can_takeover = False
                        if r_st == "pending_dispatch" and lease_dt is not None and lease_dt < db_now:
                            ex_run = await _has_active_or_advanced_run_for_key(
                                cur, thread_id=norm_tid, request_key=str(resume_row["request_key"])
                            )
                            if not ex_run or str(ex_run.get("status") or "") not in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                                can_takeover = True
                                if (
                                    str(resume_row.get("settled_via") or "") == "channel_text"
                                    and resume_row.get("platform")
                                    and resume_row.get("inbound_message_id")
                                ):
                                    old_inb_to_complete = (
                                        str(resume_row["platform"]).strip().lower(),
                                        str(resume_row["inbound_message_id"]).strip(),
                                    )
                        elif r_st == "terminal_failed":
                            if not await _has_in_progress_run_on_thread(cur, norm_tid):
                                ex_run = await _has_active_or_advanced_run_for_key(
                                    cur,
                                    thread_id=norm_tid,
                                    request_key=str(resume_row["request_key"]),
                                    admitted_run_id=resume_row.get("admitted_run_id"),
                                )
                                if not ex_run or str(ex_run.get("status") or "") not in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                                    can_takeover = True
                        elif (
                            r_st == "admitted"
                            and str(resume_row.get("settled_via") or "") == "web"
                            and resume_row.get("run_consumed_at") is None
                        ):
                            ex_run = await _has_active_or_advanced_run_for_key(
                                cur,
                                thread_id=norm_tid,
                                request_key=str(resume_row["request_key"]),
                                admitted_run_id=resume_row.get("admitted_run_id"),
                            )
                            if ex_run and str(ex_run.get("status") or "") in UNADVANCED_RUN_STATUSES:
                                can_takeover = True

                        if not can_takeover:
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": "already_settled_in_channel",
                                    "settled_via": str(resume_row["settled_via"]),
                                },
                            )

                        await cur.execute(
                            """
                            UPDATE dalton_channel_interrupt_resumes
                            SET claim_id = %s::uuid,
                                settled_via = %s,
                                settled_by_user_id = %s,
                                platform = %s,
                                token_hash = NULL,
                                inbound_message_id = NULL,
                                legacy_direct = %s,
                                weixin_account_id = NULL,
                                weixin_bot_id = NULL,
                                weixin_bot_generation = NULL,
                                weixin_recipient_id = NULL,
                                request_key = %s::uuid,
                                resume_payload = %s,
                                admitted_run_id = NULL,
                                resume_status = 'pending_dispatch',
                                lease_expires_at = NOW() + (%s || ' seconds')::interval,
                                run_consumed_at = NULL,
                                requires_gateway_ack = %s,
                                delivery_settled = %s,
                                channel_ack_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                                superseded_token_hashes = %s,
                                card_targets = %s,
                                attempt_count = 0,
                                next_retry_at = NULL,
                                last_error = NULL,
                                updated_at = NOW()
                            WHERE thread_id = %s::uuid
                              AND run_id = %s
                              AND interrupt_id = %s
                              AND claim_id = %s::uuid
                            """,
                            (
                                new_claim_id,
                                settled_via,
                                user_id,
                                eff_plat,
                                bool(legacy_direct),
                                eff_request_key,
                                Jsonb(dict(resume_payload)),
                                str(RESUME_LEASE_SECONDS),
                                req_gw_ack,
                                deliv_settled,
                                has_pending_cards,
                                Jsonb(superseded_hashes),
                                Jsonb(card_targets),
                                norm_tid,
                                str(run_id),
                                str(interrupt_id),
                                preview_claim_id,
                            ),
                        )
                        if cur.rowcount != 1:
                            raise _ResumeInboundLockSetChanged("claim_web: takeover rowcount != 1")

                    if old_inb_to_complete and old_inb_to_complete in locked_inbounds:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET status = 'completed',
                                fallback_pending = FALSE,
                                raw_payload = COALESCE(raw_payload, '{}'::jsonb)
                                    || jsonb_build_object('hitl_superseded_by', 'web_or_button_or_newer_text'),
                                updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            """,
                            old_inb_to_complete,
                        )
                    cv_val = "text_reply" if legacy_direct else "web"
                    for t in all_tokens:
                        if t.get("consumed_at") is not None:
                            continue
                        await cur.execute(
                            """
                            UPDATE dalton_channel_action_tokens
                            SET consumed_via = %s,
                                consumed_at = NOW()
                            WHERE token_hash = %s
                            """,
                            (cv_val, str(t["token_hash"])),
                        )
                    return {
                        "ok": True,
                        "claim_id": new_claim_id,
                        "request_key": eff_request_key,
                        "requires_gateway_ack": req_gw_ack,
                        "delivery_settled": deliv_settled,
                        "card_targets": card_targets,
                    }

    return await _run_interrupt_tx_with_retry("claim_web_interrupt_resume", conn, _attempt)


async def admit_interrupt_resume_in_start_tx(
    cur: Any,
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    claim_id: str,
    request_key: str,
    admitted_run_id: str,
) -> None:
    """Lock Order ⑨ ``_on_start(conn)``: clear pending_interrupt on both tables and CAS
    ``pending_dispatch -> admitted`` asserting ``cur.rowcount == 1``."""
    norm_tid = normalize_uuid(thread_id)
    if not norm_tid:
        raise HTTPException(status_code=400, detail={"code": "invalid_thread_id"})

    # Read resume metadata to lock Weixin account (3a) -> session_generations (6) -> thread_watches (7a) -> 7b-1 -> 7b-2
    await cur.execute(
        """
        SELECT platform, origin_scope_key, settled_by_user_id,
               weixin_account_id::text AS weixin_account_id
        FROM dalton_channel_interrupt_resumes
        WHERE thread_id = %s::uuid
          AND run_id = %s
          AND interrupt_id = %s
        """,
        (norm_tid, str(run_id), str(interrupt_id)),
    )
    pre = await cur.fetchone()
    wx_aids: list[str] = []
    if pre and pre.get("weixin_account_id"):
        wx_aids.append(str(pre["weixin_account_id"]))
    await cur.execute(
        """
        SELECT weixin_account_id::text AS weixin_account_id
        FROM dalton_channel_thread_watches
        WHERE thread_id = %s::uuid
        """,
        (norm_tid,),
    )
    w_pre = await cur.fetchone()
    if w_pre and w_pre.get("weixin_account_id"):
        wx_aids.append(str(w_pre["weixin_account_id"]))

    locked_wx = await _acquire_weixin_account_key_share_in_tx(cur, wx_aids)

    if pre and pre.get("platform") and pre.get("origin_scope_key") and pre.get("settled_by_user_id"):
        await cur.execute(
            """
            UPDATE dalton_channel_session_generations
            SET pending_interrupt = NULL,
                updated_at = NOW()
            WHERE platform = %s
              AND scope_key = %s
              AND dalton_user_id = %s
              AND pending_interrupt IS NOT NULL
              AND pending_interrupt->>'interrupt_id' = %s
              AND pending_interrupt->>'run_id' = %s
            """,
            (
                str(pre["platform"]),
                str(pre["origin_scope_key"]),
                str(pre["settled_by_user_id"]),
                str(interrupt_id),
                str(run_id),
            ),
        )

    watch_row = await _lock_thread_watch_and_verify_weixin_set_in_tx(
        cur, norm_tid, locked_wx, op_name="admit_on_start"
    )
    if watch_row and isinstance(watch_row.get("pending_interrupt"), Mapping):
        wpi = watch_row["pending_interrupt"]
        if str(wpi.get("interrupt_id") or "") == str(interrupt_id) and str(wpi.get("run_id") or "") == str(run_id):
            await cur.execute(
                """
                UPDATE dalton_channel_thread_watches
                SET pending_interrupt = NULL,
                    updated_at = NOW()
                WHERE thread_id = %s::uuid
                """,
                (norm_tid,),
            )

    await _acquire_hitl_resume_lock_in_tx(
        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
    )
    await cur.execute(
        """
        UPDATE dalton_channel_interrupt_resumes
        SET resume_status = 'admitted',
            admitted_run_id = %s,
            lease_expires_at = NULL,
            requires_gateway_ack = CASE
                WHEN (settled_via = 'web' OR legacy_direct = TRUE)
                     AND NOT (card_targets @> '[{"patch_status": "pending"}]'::jsonb)
                THEN FALSE
                ELSE TRUE
            END,
            delivery_settled = CASE
                WHEN (settled_via = 'web' OR legacy_direct = TRUE)
                     AND NOT (card_targets @> '[{"patch_status": "pending"}]'::jsonb)
                THEN TRUE
                ELSE FALSE
            END,
            channel_ack_at = CASE
                WHEN (settled_via = 'web' OR legacy_direct = TRUE)
                     AND NOT (card_targets @> '[{"patch_status": "pending"}]'::jsonb)
                THEN COALESCE(channel_ack_at, NOW())
                ELSE NULL
            END,
            updated_at = NOW()
        WHERE thread_id = %s::uuid
          AND run_id = %s
          AND interrupt_id = %s
          AND claim_id = %s::uuid
          AND request_key = %s::uuid
          AND resume_status = 'pending_dispatch'
        """,
        (
            str(admitted_run_id),
            norm_tid,
            str(run_id),
            str(interrupt_id),
            str(claim_id),
            str(request_key),
        ),
    )
    if cur.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "interrupt_claim_lost",
                "message": "该确认请求已被其他操作接管或已过期",
            },
        )


async def release_unadmitted_interrupt_claim(
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    claim_id: str,
    conn: Any = None,
) -> dict[str, Any]:
    """Release an unadmitted web/legacy_direct claim on preflight or pre-START failure (Lock Order ⑨)."""
    norm_tid = normalize_uuid(thread_id)
    if not norm_tid:
        return {"released": False}

    async def _attempt(conn_arg: Any) -> dict[str, Any]:
        preview = await _preview_interrupt_context(
            thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id), conn=conn_arg
        )
        preview_claim_id = preview["preview_claim_id"]
        if preview_claim_id != str(claim_id):
            return {"released": False, "reason": "claim_id_changed"}
        enabled_platforms = set(get_enabled_channel_platforms())

        async with get_db_connection(conn_arg) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    locked_inb_keys = await _acquire_inbound_advisory_locks_in_tx(
                        cur, preview["inbound_keys"]
                    )
                    locked_wx = await _acquire_weixin_account_key_share_in_tx(
                        cur, list(preview["weixin_account_ids"])
                    )
                    await _lock_inbound_events_and_verify_weixin_set_in_tx(
                        cur, locked_inb_keys, locked_wx, op_name="release_unadmitted"
                    )
                    await _lock_thread_watch_and_verify_weixin_set_in_tx(
                        cur, norm_tid, locked_wx, op_name="release_unadmitted"
                    )
                    rt_cfg = await read_effective_interrupt_runtime_config_in_tx(
                        cur, for_update=False
                    )
                    await _acquire_hitl_resume_lock_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    resume_row = await _lock_interrupt_resume_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    if not resume_row or str(resume_row["claim_id"]) != str(claim_id):
                        return {"released": False, "reason": "claim_id_changed"}
                    if str(resume_row["resume_status"]) != "pending_dispatch":
                        return {"released": False, "reason": "already_admitted_or_terminal"}
                    ex_run = await _has_active_or_advanced_run_for_key(
                        cur, thread_id=norm_tid, request_key=str(resume_row["request_key"])
                    )
                    if ex_run is not None:
                        return {"released": False, "reason": "run_exists"}

                    all_tokens = await _lock_interrupt_tokens_in_tx(
                        cur, thread_id=norm_tid, run_id=str(run_id), interrupt_id=str(interrupt_id)
                    )
                    superseded = set(
                        str(h) for h in (resume_row.get("superseded_token_hashes") or [])
                    )
                    is_web_or_legacy = (
                        str(resume_row["settled_via"]) == "web"
                        or bool(resume_row.get("legacy_direct"))
                    )
                    if is_web_or_legacy and rt_cfg["interrupt_ledger_enabled"]:
                        await cur.execute(
                            """
                            DELETE FROM dalton_channel_interrupt_resumes
                            WHERE thread_id = %s::uuid
                              AND run_id = %s
                              AND interrupt_id = %s
                              AND claim_id = %s::uuid
                            """,
                            (norm_tid, str(run_id), str(interrupt_id), str(claim_id)),
                        )
                        if cur.rowcount != 1:
                            raise _ResumeInboundLockSetChanged("release_unadmitted: delete rowcount != 1")
                        db_now = await cg_db._fetch_db_now(cur)
                        restored = 0
                        for t in all_tokens:
                            th = str(t["token_hash"])
                            if th not in superseded:
                                continue
                            if str(t["platform"]).strip().lower() not in enabled_platforms:
                                continue
                            exp_dt = _coerce_utc(t.get("expires_at"))
                            if exp_dt is not None and exp_dt <= db_now:
                                continue
                            await cur.execute(
                                """
                                UPDATE dalton_channel_action_tokens
                                SET consumed_at = NULL,
                                    consumed_via = NULL
                                WHERE token_hash = %s
                                """,
                                (th,),
                            )
                            restored += 1
                        return {"released": True, "action": "deleted", "restored_tokens": restored}

                    # Drain mode or carried-over pending cards -> terminal_failed + delivery_settled = TRUE
                    targets = [
                        dict(e)
                        for e in (resume_row.get("card_targets") or [])
                        if isinstance(e, Mapping)
                    ]
                    ack_now = not _has_pending_card_targets(targets)
                    await cur.execute(
                        """
                        UPDATE dalton_channel_interrupt_resumes
                        SET resume_status = 'terminal_failed',
                            delivery_settled = TRUE,
                            channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                            lease_expires_at = NULL,
                            next_retry_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                            updated_at = NOW()
                        WHERE thread_id = %s::uuid
                          AND run_id = %s
                          AND interrupt_id = %s
                          AND claim_id = %s::uuid
                        """,
                        (ack_now, ack_now, norm_tid, str(run_id), str(interrupt_id), str(claim_id)),
                    )
                    return {"released": True, "action": "terminal_failed", "acked": ack_now}

    return await _run_interrupt_tx_with_retry("release_unadmitted_interrupt_claim", conn, _attempt)


async def mark_interrupt_resume_consumed_by_run(
    *,
    admitted_run_id: str,
    conn: Any = None,
) -> int:
    """Idempotently stamp ``run_consumed_at = NOW()`` (and ``delivery_settled = TRUE`` for
    web/legacy_direct rows) when ``admitted_run_id`` advances the thread checkpoint."""
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    UPDATE dalton_channel_interrupt_resumes
                    SET run_consumed_at = NOW(),
                        delivery_settled = CASE
                            WHEN settled_via = 'web' OR legacy_direct = TRUE THEN TRUE
                            ELSE delivery_settled
                        END,
                        next_retry_at = CASE
                            WHEN requires_gateway_ack = TRUE AND channel_ack_at IS NULL THEN NOW()
                            ELSE next_retry_at
                        END,
                        updated_at = NOW()
                    WHERE admitted_run_id = %s
                      AND run_consumed_at IS NULL
                    """,
                    (str(admitted_run_id),),
                )
                return int(cur.rowcount or 0)


async def reconcile_stale_interrupt_resumes(
    *,
    conn: Any = None,
) -> dict[str, int]:
    """Backend RecoveryScanner main-path reconciliation (Lock Order ⑩).

    Covers:
    1. Expired ``pending_dispatch`` rows.
    2. ``admitted`` rows with ``run_consumed_at IS NULL`` (retryable rekey vs terminal_failed).
    3. Tokens: ONLY never-sent ``pending_bind`` > 90s -> ``bind_expired``; disabled-platform tokens ->
       ``skipped_platform_disabled``; keeps ``send_in_flight`` and ``send_unknown`` untouched for
       enabled platforms.
    4. BT2 deliveries: per-attempt lease expiry (``status = 'in_flight' AND claim_lease_expires_at < NOW()``)
       -> ``unknown / attempt_lease_expired``; disabled-platform & exhausted (>= 8 attempts, non-active)
       -> explicit terminal ``failed``.
    5. Unacked ``interrupt_resumes`` re-entrant settlement for disabled platforms / all-terminal cards.
    """
    enabled_platforms = get_enabled_channel_platforms()
    enabled_set = set(enabled_platforms)
    stats = {
        "expired_pending_bind_tokens": 0,
        "disabled_platform_tokens": 0,
        "bt2_lease_reclaimed": 0,
        "bt2_terminal_failed": 0,
        "resumes_reconciled": 0,
        "resumes_acked": 0,
    }

    # Step 3a: Token cleanup (pending_bind > 90s ONLY, plus disabled platform tokens)
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH target AS (
                        SELECT token_hash
                        FROM dalton_channel_action_tokens
                        WHERE token_role IN ('direct', 'gate')
                          AND bind_status = 'pending_bind'
                          AND created_at < NOW() - (%s || ' seconds')::interval
                        ORDER BY token_hash ASC
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE dalton_channel_action_tokens t
                    SET bind_status = 'bind_expired',
                        consumed_via = COALESCE(t.consumed_via, 'expired'),
                        consumed_at = COALESCE(t.consumed_at, NOW())
                    FROM target
                    WHERE t.token_hash = target.token_hash
                    """,
                    (str(PENDING_BIND_EXPIRE_SECONDS),),
                )
                stats["expired_pending_bind_tokens"] = int(cur.rowcount or 0)

                disabled_card_plats = [p for p in CARD_PLATFORMS if p not in enabled_set]
                if disabled_card_plats:
                    await cur.execute(
                        """
                        WITH target AS (
                            SELECT token_hash
                            FROM dalton_channel_action_tokens
                            WHERE token_role IN ('direct', 'gate')
                              AND platform = ANY(%s::varchar[])
                              AND bind_status IN ('pending_bind', 'send_in_flight', 'send_unknown')
                            ORDER BY token_hash ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_action_tokens t
                        SET bind_status = 'skipped_platform_disabled',
                            consumed_via = COALESCE(t.consumed_via, 'expired'),
                            consumed_at = COALESCE(t.consumed_at, NOW())
                        FROM target
                        WHERE t.token_hash = target.token_hash
                        """,
                        (disabled_card_plats,),
                    )
                    stats["disabled_platform_tokens"] += int(cur.rowcount or 0)

                    await cur.execute(
                        """
                        WITH target AS (
                            SELECT token_hash
                            FROM dalton_channel_action_tokens
                            WHERE late_patch_status = 'pending'
                              AND platform = ANY(%s::varchar[])
                            ORDER BY token_hash ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_action_tokens t
                        SET late_patch_status = 'skipped_platform_disabled',
                            late_patch_next_retry_at = NULL
                        FROM target
                        WHERE t.token_hash = target.token_hash
                        """,
                        (disabled_card_plats,),
                    )
                    stats["disabled_platform_tokens"] += int(cur.rowcount or 0)

                # Step 3b: BT2 per-attempt lease expiry + disabled platform / exhausted terminal failure
                await cur.execute(
                    """
                    WITH target AS (
                        SELECT delivery_key, claim_attempt_id
                        FROM dalton_channel_deliveries
                        WHERE status = 'in_flight'
                          AND claim_lease_expires_at IS NOT NULL
                          AND claim_lease_expires_at < NOW()
                          AND (delivery_key LIKE 'deliverables-links:%%' OR bt2_enrolled = TRUE)
                        ORDER BY delivery_key ASC
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE dalton_channel_deliveries d
                    SET status = 'unknown',
                        error_code = 'attempt_lease_expired',
                        updated_at = NOW()
                    FROM target
                    WHERE d.delivery_key = target.delivery_key
                      AND d.status = 'in_flight'
                      AND (
                          (target.claim_attempt_id IS NULL AND d.claim_attempt_id IS NULL)
                          OR d.claim_attempt_id = target.claim_attempt_id
                      )
                    """
                )
                stats["bt2_lease_reclaimed"] = int(cur.rowcount or 0)

                disabled_all_plats = [p for p in ALL_CHANNEL_PLATFORMS if p not in enabled_set]
                if disabled_all_plats:
                    await cur.execute(
                        """
                        WITH target AS (
                            SELECT delivery_key
                            FROM dalton_channel_deliveries
                            WHERE delivery_key LIKE 'deliverables-links:%%'
                              AND platform = ANY(%s::varchar[])
                              AND (
                                  status IN ('in_flight', 'partial', 'unknown')
                                  OR (
                                      status = 'failed'
                                      AND COALESCE(error_code, '') NOT IN (
                                          'not_allowed', 'max_attempts_exceeded', 'skipped_platform_disabled'
                                      )
                                  )
                              )
                            ORDER BY delivery_key ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_deliveries d
                        SET status = 'failed',
                            error_code = 'skipped_platform_disabled',
                            updated_at = NOW()
                        FROM target
                        WHERE d.delivery_key = target.delivery_key
                        """,
                        (disabled_all_plats,),
                    )
                    stats["bt2_terminal_failed"] += int(cur.rowcount or 0)

                # Exhausted (>= 8 attempts) and NOT actively in_flight under valid lease
                await cur.execute(
                    """
                    WITH target AS (
                        SELECT delivery_key, run_id
                        FROM dalton_channel_deliveries
                        WHERE delivery_key LIKE 'deliverables-links:%%'
                          AND delivery_attempt >= %s
                          AND (
                              status IN ('partial', 'unknown')
                              OR (
                                  status = 'in_flight'
                                  AND claim_lease_expires_at IS NOT NULL
                                  AND claim_lease_expires_at < NOW()
                              )
                              OR (
                                  status = 'failed'
                                  AND COALESCE(error_code, '') NOT IN (
                                      'not_allowed', 'max_attempts_exceeded', 'skipped_platform_disabled'
                                  )
                              )
                          )
                        ORDER BY delivery_key ASC
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE dalton_channel_deliveries d
                    SET status = 'failed',
                        error_code = 'max_attempts_exceeded',
                        updated_at = NOW()
                    FROM target
                    WHERE d.delivery_key = target.delivery_key
                    RETURNING d.delivery_key, d.run_id
                    """,
                    (MAX_BT2_DELIVERY_ATTEMPTS,),
                )
                exhausted_rows = await cur.fetchall()
                stats["bt2_terminal_failed"] += len(exhausted_rows)

    # Step 4: Per-row reconciliation of unacked / stale interrupt_resumes (Lock Order ⑩)
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT thread_id::text AS thread_id,
                       run_id,
                       interrupt_id,
                       claim_id::text AS claim_id
                FROM dalton_channel_interrupt_resumes
                WHERE (resume_status = 'pending_dispatch' AND lease_expires_at < NOW())
                   OR (resume_status = 'admitted' AND run_consumed_at IS NULL)
                   OR (requires_gateway_ack = TRUE AND channel_ack_at IS NULL)
                ORDER BY updated_at ASC
                LIMIT 100
                """
            )
            candidates = [dict(r) for r in await cur.fetchall()]

    for cand in candidates:
        res_info = await _reconcile_single_interrupt_resume(
            thread_id=str(cand["thread_id"]),
            run_id=str(cand["run_id"]),
            interrupt_id=str(cand["interrupt_id"]),
            enabled_set=enabled_set,
            conn=conn,
        )
        if res_info.get("changed"):
            stats["resumes_reconciled"] += 1
        if res_info.get("acked"):
            stats["resumes_acked"] += 1

    return stats


async def _reconcile_single_interrupt_resume(
    *,
    thread_id: str,
    run_id: str,
    interrupt_id: str,
    enabled_set: set[str],
    conn: Any = None,
) -> dict[str, Any]:
    """Single-row Lock Order ⑩ reconciliation with immediate Weixin 3a check and claim_id verification."""
    async def _attempt(conn_arg: Any) -> dict[str, Any]:
        preview = await _preview_interrupt_context(
            thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id, conn=conn_arg
        )
        preview_claim_id = preview["preview_claim_id"]
        if preview_claim_id is None:
            return {"changed": False, "acked": False}
        if _after_preview_hook is not None:
            await _after_preview_hook("recovery_scanner")

        async with get_db_connection(conn_arg) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SET LOCAL lock_timeout = '2s'")
                    locked_inb_keys = await _acquire_inbound_advisory_locks_in_tx(
                        cur, preview["inbound_keys"]
                    )
                    locked_wx = await _acquire_weixin_account_key_share_in_tx(
                        cur, list(preview["weixin_account_ids"])
                    )
                    locked_inbounds = await _lock_inbound_events_and_verify_weixin_set_in_tx(
                        cur, locked_inb_keys, locked_wx, op_name="recovery_scanner"
                    )
                    await _lock_thread_watch_and_verify_weixin_set_in_tx(
                        cur, thread_id, locked_wx, op_name="recovery_scanner"
                    )
                    rt_cfg = await read_effective_interrupt_runtime_config_in_tx(
                        cur, for_update=False
                    )
                    await _acquire_hitl_resume_lock_in_tx(
                        cur, thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id
                    )
                    row = await _lock_interrupt_resume_in_tx(
                        cur, thread_id=thread_id, run_id=run_id, interrupt_id=interrupt_id
                    )
                    actual_cid = str(row["claim_id"]) if row else None
                    if actual_cid != preview_claim_id:
                        raise _ResumeInboundLockSetChanged("recovery_scanner: claim_id changed")
                    if not row:
                        return {"changed": False, "acked": False}
                    if row.get("weixin_account_id") and str(row["weixin_account_id"]) not in locked_wx:
                        raise _ResumeInboundLockSetChanged("recovery_scanner: resume wx unlocked")

                    r_st = str(row["resume_status"])
                    r_plat = str(row.get("platform") or "").strip().lower()
                    is_web_or_legacy = (
                        str(row["settled_via"]) == "web" or bool(row.get("legacy_direct"))
                    )
                    targets = [
                        dict(e) for e in (row.get("card_targets") or []) if isinstance(e, Mapping)
                    ]
                    changed = False
                    acked = False

                    # Normalize disabled-platform cards idempotently
                    for c_elem in targets:
                        cp = str(c_elem.get("platform") or "").strip().lower()
                        if cp not in enabled_set and str(c_elem.get("patch_status") or "") == "pending":
                            c_elem["patch_status"] = "skipped_platform_disabled"
                            changed = True

                    inb_pair = (
                        (r_plat, str(row["inbound_message_id"]).strip())
                        if r_plat and row.get("inbound_message_id")
                        else None
                    )
                    inb_row = locked_inbounds.get(inb_pair) if inb_pair else None

                    db_now = await cg_db._fetch_db_now(cur)
                    lease_dt = _coerce_utc(row.get("lease_expires_at"))

                    # (1) Expired pending_dispatch
                    if r_st == "pending_dispatch" and lease_dt is not None and lease_dt < db_now:
                        ex_run = await _has_active_or_advanced_run_for_key(
                            cur, thread_id=thread_id, request_key=str(row["request_key"])
                        )
                        if ex_run and str(ex_run.get("status") or "") in ACTIVE_OR_ADVANCED_RUN_STATUSES:
                            r_st = "admitted"
                            row["resume_status"] = "admitted"
                            row["admitted_run_id"] = str(ex_run["conversation_response_id"])
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET resume_status = 'admitted',
                                    admitted_run_id = %s,
                                    lease_expires_at = NULL,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s AND claim_id = %s::uuid
                                """,
                                (row["admitted_run_id"], thread_id, run_id, interrupt_id, actual_cid),
                            )
                            changed = True
                        elif not rt_cfg["interrupt_ledger_enabled"] or (r_plat and r_plat not in enabled_set):
                            r_st = "terminal_failed"
                            deliv_settled = (
                                is_web_or_legacy
                                or str(row["settled_via"]) == "channel_button"
                                or (bool(r_plat) and r_plat not in enabled_set)
                            )
                            ack_now = deliv_settled and not _has_pending_card_targets(targets)
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET resume_status = 'terminal_failed',
                                    delivery_settled = %s,
                                    card_targets = %s,
                                    channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                                    lease_expires_at = NULL,
                                    next_retry_at = CASE WHEN %s THEN NULL ELSE NOW() END,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid AND run_id = %s AND interrupt_id = %s AND claim_id = %s::uuid
                                """,
                                (deliv_settled, Jsonb(targets), ack_now, ack_now, thread_id, run_id, interrupt_id, actual_cid),
                            )
                            if (
                                inb_pair
                                and inb_row
                                and r_plat not in enabled_set
                                and not _is_credit_supplement_pending_resubmit(inb_row)
                            ):
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_inbound_events
                                    SET status = 'failed', fallback_pending = FALSE, updated_at = NOW()
                                    WHERE platform = %s AND message_id = %s
                                    """,
                                    inb_pair,
                                )
                            return {"changed": True, "acked": ack_now}

                    # (2) Re-entrant unacked settlement (CHECK-compliant)
                    if bool(row.get("requires_gateway_ack")) and row.get("channel_ack_at") is None:
                        deliv_settled = bool(row.get("delivery_settled"))
                        consumed_or_failed = (
                            r_st == "terminal_failed"
                            or (r_st == "admitted" and row.get("run_consumed_at") is not None)
                        )
                        if not deliv_settled and consumed_or_failed:
                            if (
                                is_web_or_legacy
                                or (str(row["settled_via"]) == "channel_button" and r_st == "terminal_failed")
                                or (bool(r_plat) and r_plat not in enabled_set)
                            ):
                                deliv_settled = True
                                changed = True
                                if (
                                    inb_pair
                                    and inb_row
                                    and r_plat not in enabled_set
                                    and not _is_credit_supplement_pending_resubmit(inb_row)
                                ):
                                    inb_st = "completed" if r_st == "admitted" else "failed"
                                    await cur.execute(
                                        """
                                        UPDATE dalton_channel_inbound_events
                                        SET status = %s, fallback_pending = FALSE, updated_at = NOW()
                                        WHERE platform = %s AND message_id = %s
                                        """,
                                        (inb_st, inb_pair[0], inb_pair[1]),
                                    )
                        ack_now = (
                            r_st in ("admitted", "terminal_failed")
                            and consumed_or_failed
                            and deliv_settled
                            and not _has_pending_card_targets(targets)
                        )
                        if changed or ack_now:
                            await cur.execute(
                                """
                                UPDATE dalton_channel_interrupt_resumes
                                SET card_targets = %s,
                                    delivery_settled = %s,
                                    channel_ack_at = CASE WHEN %s THEN COALESCE(channel_ack_at, NOW()) ELSE NULL END,
                                    next_retry_at = CASE WHEN %s THEN NULL ELSE next_retry_at END,
                                    updated_at = NOW()
                                WHERE thread_id = %s::uuid
                                  AND run_id = %s
                                  AND interrupt_id = %s
                                  AND claim_id = %s::uuid
                                """,
                                (
                                    Jsonb(targets),
                                    deliv_settled,
                                    ack_now,
                                    ack_now,
                                    thread_id,
                                    run_id,
                                    interrupt_id,
                                    actual_cid,
                                ),
                            )
                            acked = ack_now
                            changed = True
                    return {"changed": changed, "acked": acked}

    return await _run_interrupt_tx_with_retry("_reconcile_single_interrupt_resume", conn, _attempt)
