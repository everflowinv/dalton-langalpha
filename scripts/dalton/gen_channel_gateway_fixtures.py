#!/usr/bin/env python3
"""Generate real backend payload fixtures for `integrations/dalton-channel-gateway/test/fixtures/`.

Calls real backend serializers, Pydantic models, and payload builders directly
wherever pure/in-memory functions exist, and constructs verbatim DB/route structures
from the exact backend lines where DB connections are required. Also writes
`integrations/dalton-channel-gateway/test/fixtures/SOURCES.md` documenting the
exact backend `file:line` and generation method for every fixture.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
from uuid import UUID, uuid5

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from langchain_core.messages import AIMessage  # noqa: E402

from ptc_agent.agent.middleware.credit_gate import (  # noqa: E402
    INTERRUPT_REASON_CREDIT_PAUSE,
)
from src.dalton.database.channel_gateway import (  # noqa: E402
    DALTON_CHANNEL_NAMESPACE,
    _json_safe_rows,
    _serialize_inbound_row,
    default_app_settings,
    format_external_thread_id,
)
from src.server.contracts.run_times import run_completed_at  # noqa: E402
from src.server.contracts.status import to_public  # noqa: E402
from src.server.database.conversation.errors import (  # noqa: E402
    external_id_conflict_payload,
)
from src.server.handlers.chat.admission_gate import (  # noqa: E402
    admission_conflict_detail,
)
from src.server.models.chat import (  # noqa: E402
    HITLDecision,
    HITLResponse,
    serialize_hitl_response_map,
)
from src.server.models.wakeup import (  # noqa: E402
    WakeupOut,
    WakeupPriceOut,
    WakeupsResponse,
)
from src.server.services.history import projector as history_projector  # noqa: E402
from src.server.services.history.replay import (  # noqa: E402
    items as replay_items,
    stopped as replay_stopped,
    stubs as replay_stubs,
)
from src.server.services.runs.pending_input import _resend_required  # noqa: E402

FIXTURES_DIR = (
    REPO_ROOT / "integrations" / "dalton-channel-gateway" / "test" / "fixtures"
)

USER_ID = "user_alice_123"
THREAD_ID = "11111111-2222-3333-4444-555555555555"
RUN_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
WORKSPACE_ID = "99999999-8888-7777-6666-555555555555"
HOME_WS_ID = "00000000-0000-0000-0000-000000000001"
FIXED_DT = datetime(2026, 11, 8, 0, 0, 0, tzinfo=timezone.utc)
FIXED_ISO = FIXED_DT.isoformat()


def make_uuid5(name: str) -> str:
    return str(uuid5(DALTON_CHANNEL_NAMESPACE, name))


def _write_json(filename: str, payload: Any) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    target = FIXTURES_DIR / filename
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_i1_fixtures() -> dict[str, Any]:
    """Generated via `_serialize_inbound_row` and `format_external_thread_id` (`src/dalton/database/channel_gateway.py:148-164, 374-417`)."""
    dm_ext_id = format_external_thread_id("feishu", "dm:ou_alice", USER_ID, 1, "dm")
    base_row = {
        "dalton_user_id": USER_ID,
        "request_key": UUID(make_uuid5("feishu:m_default_1")),
        "retry_request_key": None,
        "resubmitted_request_key": None,
        "steered_into_run_id": None,
        "thread_id": UUID(THREAD_ID),
        "run_id": None,
        "turn_index": None,
        "workspace_id": UUID(WORKSPACE_ID),
        "external_thread_id": dm_ext_id,
        "fallback_pending": False,
        "sent_to_current_count": 0,
        "uploaded_attachments": [],
        "raw_payload": {
            "platform_user_id": "ou_alice",
            "generation": 1,
            "text": "Hello",
        },
    }

    default_resolved = _serialize_inbound_row(
        base_row,
        existing_status=None,
        linked=True,
        allowed=True,
        deny_reason=None,
        user_tier="internal",
        workspace_status="running",
        workspace_owner_id=USER_ID,
        generation=1,
        pending_interrupt=None,
    )

    lazy_home = _serialize_inbound_row(
        {
            **base_row,
            "request_key": UUID(make_uuid5("feishu:m_lazy_home")),
            "workspace_id": UUID(HOME_WS_ID),
            "raw_payload": {
                "platform_user_id": "ou_alice",
                "generation": 1,
                "text": "Upload to lazy Home",
            },
        },
        existing_status=None,
        linked=True,
        allowed=True,
        deny_reason=None,
        user_tier="internal",
        workspace_status=None,
        workspace_owner_id=USER_ID,
        generation=1,
        pending_interrupt=None,
    )

    grp_ext_id = format_external_thread_id(
        "feishu", "chat:oc_group1", USER_ID, 1, "group"
    )
    colleague_stopped = _serialize_inbound_row(
        {
            **base_row,
            "request_key": UUID(make_uuid5("feishu:m_colleague_stopped")),
            "external_thread_id": grp_ext_id,
            "raw_payload": {
                "platform_user_id": "ou_alice",
                "generation": 1,
                "text": "Upload to colleague ws",
            },
        },
        existing_status=None,
        linked=True,
        allowed=True,
        deny_reason=None,
        user_tier="internal",
        workspace_status="stopped",
        workspace_owner_id="user_colleague_bob",
        generation=1,
        pending_interrupt=None,
    )

    new_thread_unresolved = _serialize_inbound_row(
        {
            **base_row,
            "request_key": UUID(make_uuid5("feishu:m_ext_conflict")),
            "thread_id": None,
            "external_thread_id": format_external_thread_id(
                "feishu", "dm:ou_alice", USER_ID, 2, "dm"
            ),
            "raw_payload": {
                "platform_user_id": "ou_alice",
                "generation": 2,
                "text": "First message in raced thread",
            },
        },
        existing_status=None,
        linked=True,
        allowed=True,
        deny_reason=None,
        user_tier="internal",
        workspace_status="running",
        workspace_owner_id=USER_ID,
        generation=2,
        pending_interrupt=None,
    )

    return {
        "default_resolved": default_resolved,
        "lazy_home": lazy_home,
        "colleague_stopped": colleague_stopped,
        "new_thread_unresolved": new_thread_unresolved,
    }


def build_i2_fixtures() -> dict[str, Any]:
    """Verbatim return structures from `consume_bind_code_or_unbind` (`src/dalton/database/channel_gateway.py:1063-1133, 1243-1248`)."""
    return {
        "bind_success": {
            "ok": True,
            "user_id": USER_ID,
            "platform": "feishu",
            "platform_user_id": "ou_alice",
            "generation": 1,
        },
        "bind_revoked_in_group": {
            "ok": False,
            "reason": "revoked_in_group",
        },
        "bind_invalid_or_expired": {
            "ok": False,
            "reason": "invalid_or_expired",
        },
        "unbind_success": {
            "ok": True,
            "user_id": USER_ID,
            "platform": "feishu",
            "generations": {"dm:ou_alice": 2},
        },
    }


def build_i3_fixtures() -> dict[str, Any]:
    """Return structures from `execute_session_command` (`src/dalton/database/channel_gateway.py`)."""
    ext_gen_1 = format_external_thread_id("feishu", "dm:ou_alice", USER_ID, 1, "dm")
    ext_gen_2 = format_external_thread_id("feishu", "dm:ou_alice", USER_ID, 2, "dm")
    return {
        "workspace_list": {
            "ok": True,
            "linked": True,
            "allowed": True,
            "dalton_user_id": USER_ID,
            "user_tier": "internal",
            "current_workspace_id": WORKSPACE_ID,
            "current_workspace_name": "Quant Workspace",
            "is_home": False,
            "home_workspace_id": HOME_WS_ID,
            "generation": 1,
            "external_thread_id": ext_gen_1,
            "duplicate": False,
            "command_effect_status": "done",
            "command_reply_pending": True,
            "workspaces": [
                {"workspace_id": HOME_WS_ID, "name": "Home"},
                {"workspace_id": WORKSPACE_ID, "name": "Quant Workspace"},
            ],
        },
        "workspace_switch_dm": {
            "ok": True,
            "linked": True,
            "allowed": True,
            "dalton_user_id": USER_ID,
            "user_tier": "internal",
            "workspace_id": WORKSPACE_ID,
            "workspace_name": "Quant Workspace",
            "is_home": False,
            "override_workspace_id": WORKSPACE_ID,
            "home_workspace_id": HOME_WS_ID,
            "generation": 2,
            "external_thread_id": ext_gen_2,
            "duplicate": False,
            "command_effect_status": "done",
            "command_reply_pending": True,
        },
        "new_session": {
            "ok": True,
            "linked": True,
            "allowed": True,
            "dalton_user_id": USER_ID,
            "user_tier": "internal",
            "workspace_id": HOME_WS_ID,
            "workspace_name": "Chief of Staff",
            "is_home": True,
            "override_workspace_id": None,
            "home_workspace_id": HOME_WS_ID,
            "generation": 2,
            "external_thread_id": ext_gen_2,
            "duplicate": False,
            "command_effect_status": "done",
            "command_reply_pending": True,
        },
        "stop_session": {
            "ok": True,
            "linked": True,
            "allowed": True,
            "dalton_user_id": USER_ID,
            "user_tier": "internal",
            "workspace_id": WORKSPACE_ID,
            "is_home": False,
            "thread_id": THREAD_ID,
            "run_id": RUN_ID,
            "generation": 1,
            "external_thread_id": ext_gen_1,
            "duplicate": False,
            "command_effect_status": "pending_cancel",
            "command_reply_pending": True,
        },
    }


def build_i4_fixtures() -> dict[str, Any]:
    """Generated using `default_app_settings` and exact return dictionary of `get_channel_internal_state` (`src/dalton/database/channel_gateway.py:124-136, 1882-1894`)."""
    feishu_cfg = default_app_settings("feishu")
    feishu_cfg["chats"] = {
        "feishu:oc_group1": {"workspace_id": WORKSPACE_ID},
    }
    feishu_cfg["automation_output"] = {
        WORKSPACE_ID: "feishu:oc_group1",
    }
    feishu_cfg["agent_messages"] = {
        "enabled": True,
        "allowed": ["feishu:@me", "feishu:oc_group1"],
    }

    discord_cfg = default_app_settings("discord")
    discord_cfg["preferred"] = None

    weixin_cfg = default_app_settings("weixin")

    ext_id = format_external_thread_id("feishu", "dm:ou_alice", USER_ID, 1, "dm")

    default_state = {
        "user_id": USER_ID,
        "user_tier": "internal",
        "home_workspace_id": HOME_WS_ID,
        "identities": {
            "feishu": {
                "platform": "feishu",
                "platform_user_id": "ou_alice",
                "platform_username": "Alice",
                "platform_tenant_key": "t_1",
                "weixin_context_token": None,
                "created_at": FIXED_ISO,
                "updated_at": FIXED_ISO,
            },
            "discord": {
                "platform": "discord",
                "platform_user_id": "dc_alice",
                "platform_username": "alice#0001",
                "platform_tenant_key": None,
                "weixin_context_token": None,
                "created_at": FIXED_ISO,
                "updated_at": FIXED_ISO,
            },
            "weixin": {
                "platform": "weixin",
                "platform_user_id": "wx_alice",
                "platform_username": "AliceWX",
                "platform_tenant_key": None,
                "weixin_context_token": "ctx_tok_123",
                "created_at": FIXED_ISO,
                "updated_at": FIXED_ISO,
            },
        },
        "settings": {
            "version": "v1",
            "apps": {
                "feishu": feishu_cfg,
                "discord": discord_cfg,
                "weixin": weixin_cfg,
            },
        },
        "chat_bindings": [
            {
                "platform": "feishu",
                "chat_address": "feishu:oc_group1",
                "chat_name": "Alpha Research Group",
                "workspace_id": WORKSPACE_ID,
                "bound_by_user_id": USER_ID,
                "last_used_at": 1700000000,
            },
        ],
        "current_conversation": {
            "address": "feishu:@me",
            "platform": "feishu",
            "thread_id": THREAD_ID,
            "run_id": RUN_ID,
            "external_thread_id": ext_id,
        },
        "resolved_thread_id": THREAD_ID,
    }
    return {"default_state": default_state}


def build_i5_fixtures() -> dict[str, Any]:
    """Outer return dicts copied from `update_user_channel_settings` (`src/dalton/database/channel_gateway.py:2024-2027, 2395-2408`), inner `settings` generated via `default_app_settings`."""
    feishu_cfg = default_app_settings("feishu")
    discord_cfg = default_app_settings("discord")
    weixin_cfg = default_app_settings("weixin")
    apps_dict = {
        "feishu": feishu_cfg,
        "discord": discord_cfg,
        "weixin": weixin_cfg,
    }
    return {
        "put_success": {
            "version": "v2",
            "settings": apps_dict,
            "changes": ["Updated feishu settings"],
        },
        "automation_output_success": {
            "version": "v2",
            "settings": apps_dict,
            "address": "feishu:oc_group1",
            "name": "feishu:oc_group1",
            "changes": [
                f"Set feishu automation output for workspace {WORKSPACE_ID} to feishu:oc_group1"
            ],
        },
        "version_conflict_409": {
            "code": "version_conflict",
            "message": "Channel settings version conflict: expected v2, got v1.",
        },
        "group_permission_403": {
            "detail": "You do not have permission to manage group chat feishu:oc_secret",
        },
    }


def build_i6_fixtures() -> dict[str, Any]:
    """Verbatim return structures from `authorize_and_claim_delivery` (`src/dalton/database/channel_gateway.py:2548-2596, 2988-2996`)."""
    return {
        "claimed_feishu_dm": {
            "claim_status": "claimed",
            "recipient_id": "ou_alice",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": True,
            "home_workspace_id": HOME_WS_ID,
            "weixin_context_token": None,
        },
        "already_completed_sent": {
            "claim_status": "already_completed",
            "status": "sent",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": True,
            "first_response_json": {
                "status": "sent",
                "code": None,
                "address": "feishu:@me",
                "current": True,
                "message": "Delivered to feishu:@me.",
                "files": [],
            },
        },
        "already_completed_partial": {
            "claim_status": "already_completed",
            "status": "partial",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": True,
            "first_response_json": {
                "status": "partial",
                "code": "unavailable",
                "address": "feishu:@me",
                "current": True,
                "message": "Delivered 1/2 text chunks before error: Feishu rate limit on chunk 2",
                "files": [],
            },
        },
        "in_flight_or_unknown": {
            "claim_status": "in_flight_or_unknown",
            "status": "unknown",
            "code": "in_flight",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": True,
        },
        "denied_not_allowed": {
            "claim_status": "denied",
            "status": "failed",
            "code": "not_allowed",
            "message": "Target 'feishu:oc_group2' is not in agent_messages.allowed.",
        },
    }


def build_i7_fixtures() -> dict[str, Any]:
    """Generated via `_json_safe_rows` and verbatim return structures from `update_delivery_progress`, `update_inbound_progress`, `reconcile_steer`, and `recover_channel_state` (`src/dalton/database/channel_gateway.py:3130-3788`)."""
    returned_key = make_uuid5(f"feishu:m_steer_1:returned:{RUN_ID}")
    missing_run_key = make_uuid5("feishu:m_rec_dispatched_no_run")
    hitl_received_key = make_uuid5("feishu:m_rec_received_hitl")
    completed_fb_key = make_uuid5("feishu:m_rec_completed_fallback")

    startup_inbound_rows = _json_safe_rows(
        [
            {
                "platform": "feishu",
                "message_id": "m_rec_completed_fallback",
                "dalton_user_id": USER_ID,
                "chat_type": "dm",
                "chat_address": "feishu:@me",
                "thread_scope_key": "dm:ou_alice",
                "request_key": UUID(completed_fb_key),
                "retry_request_key": None,
                "resubmitted_request_key": None,
                "steered_into_run_id": None,
                "thread_id": UUID(THREAD_ID),
                "run_id": "run_rec_1",
                "turn_index": 1,
                "workspace_id": UUID(WORKSPACE_ID),
                "external_thread_id": format_external_thread_id(
                    "feishu", "dm:ou_alice", USER_ID, 1, "dm"
                ),
                "status": "completed",
                "fallback_pending": True,
                "sent_to_current_count": 0,
                "uploaded_attachments": [],
                "raw_payload": {
                    "platform_user_id": "ou_alice",
                    "generation": 1,
                    "text": "Hello",
                },
                "created_at": FIXED_DT,
                "updated_at": FIXED_DT,
            },
            {
                "platform": "feishu",
                "message_id": "m_rec_dispatched_no_run",
                "dalton_user_id": USER_ID,
                "chat_type": "dm",
                "chat_address": "feishu:@me",
                "thread_scope_key": "dm:ou_alice",
                "request_key": UUID(missing_run_key),
                "retry_request_key": None,
                "resubmitted_request_key": None,
                "steered_into_run_id": None,
                "thread_id": UUID(THREAD_ID),
                "run_id": None,
                "turn_index": None,
                "workspace_id": UUID(WORKSPACE_ID),
                "external_thread_id": format_external_thread_id(
                    "feishu", "dm:ou_alice", USER_ID, 1, "dm"
                ),
                "status": "dispatched",
                "fallback_pending": True,
                "sent_to_current_count": 0,
                "uploaded_attachments": [],
                "raw_payload": {
                    "platform_user_id": "ou_alice",
                    "generation": 1,
                    "text": "Crashed before metadata",
                },
                "created_at": FIXED_DT,
                "updated_at": FIXED_DT,
            },
            {
                "platform": "feishu",
                "message_id": "m_rec_received_hitl",
                "dalton_user_id": USER_ID,
                "chat_type": "dm",
                "chat_address": "feishu:@me",
                "thread_scope_key": "dm:ou_alice",
                "request_key": UUID(hitl_received_key),
                "retry_request_key": None,
                "resubmitted_request_key": None,
                "steered_into_run_id": None,
                "thread_id": UUID(THREAD_ID),
                "run_id": RUN_ID,
                "turn_index": 1,
                "workspace_id": UUID(WORKSPACE_ID),
                "external_thread_id": format_external_thread_id(
                    "feishu", "dm:ou_alice", USER_ID, 1, "dm"
                ),
                "status": "pending",
                "fallback_pending": False,
                "sent_to_current_count": 0,
                "uploaded_attachments": [],
                "raw_payload": {
                    "platform_user_id": "ou_alice",
                    "generation": 1,
                    "text": "My answer to question",
                },
                "created_at": FIXED_DT,
                "updated_at": FIXED_DT,
            },
        ]
    )

    # Verify every startup inbound row matches the SQL WHERE clause in `channel_gateway.py:3733`:
    # `WHERE status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit') OR fallback_pending = TRUE`
    for _r in startup_inbound_rows:
        assert (
            _r["status"]
            in ("pending", "accepted", "streaming", "steered", "pending_resubmit")
            or _r["fallback_pending"] is True
        ), f"Row {_r['message_id']} would not be selected by recover_channel_state startup query"

    startup_bg_rows = _json_safe_rows(
        [
            {
                "run_id": "bg_rec_1",
                "thread_id": UUID(THREAD_ID),
                "dalton_user_id": USER_ID,
                "platform": "feishu",
                "target_address": "feishu:@me",
                "turn_index": 1,
                "status": "streaming",
                "fallback_pending": True,
                "sent_to_current_count": 0,
                "created_at": FIXED_DT,
                "updated_at": FIXED_DT,
            }
        ]
    )

    startup_watch_rows = _json_safe_rows(
        [
            {
                "thread_id": UUID(THREAD_ID),
                "dalton_user_id": USER_ID,
                "platform": "feishu",
                "target_address": "feishu:@me",
                "scope_key": "dm:ou_alice",
                "generation": 1,
                "last_seen_turn_index": 1,
                "watch_active": True,
                "created_at": FIXED_DT,
                "updated_at": FIXED_DT,
            }
        ]
    )

    startup_auto_rows = _json_safe_rows(
        [
            {
                "execution_id": UUID("33333333-4444-5555-6666-777777777777"),
                "user_id": USER_ID,
                "workspace_id": UUID(WORKSPACE_ID),
                "thread_id": UUID(THREAD_ID),
                "automation_name": "Recovered Automation",
                "entries": ["feishu"],
                "status": "finishing",
                "finish_status": "failed",
                "final_text": None,
                "targets": [
                    {
                        "entry": "feishu",
                        "address": "feishu:@me",
                        "name": "Alice (feishu DM)",
                        "ok": True,
                        "message": None,
                        "agent_sent": False,
                    }
                ],
                "created_at": FIXED_DT,
                "updated_at": FIXED_DT,
            }
        ]
    )

    return {
        "delivery_progress_sent": {
            "ok": True,
            "delivery_key": f"{RUN_ID}:tc_001",
            "status": "sent",
            "sent_to_current_count": 1,
        },
        "inbound_progress_default": {
            "ok": True,
            "run_id": RUN_ID,
            "has_in_flight_current_text": False,
            "has_unknown_current_text": False,
            "has_in_flight_or_unknown_current_text": False,
            "sent_to_current_count": 0,
        },
        "reconcile_consumed": {
            "ok": True,
            "reconcile_status": "consumed",
            "outcome": "delivered",
            "run_status": "completed",
            "inbound_status": "completed",
            "resubmitted_request_key": None,
        },
        "reconcile_pending_resubmit": {
            "ok": True,
            "reconcile_status": "pending_resubmit",
            "outcome": "returned",
            "run_status": "completed",
            "inbound_status": "pending_resubmit",
            "resubmitted_request_key": returned_key,
        },
        "reconcile_returned_unresubmitted": {
            "ok": True,
            "reconcile_status": "returned_unresubmitted",
            "outcome": "returned",
            "run_status": "error",
            "inbound_status": "returned_unresubmitted",
            "resubmitted_request_key": None,
        },
        "reconcile_still_pending_interrupted": {
            "ok": True,
            "reconcile_status": "still_pending",
            "outcome": None,
            "run_status": "interrupted",
            "inbound_status": "steered",
            "resubmitted_request_key": None,
        },
        "reconcile_still_pending_running": {
            "ok": True,
            "reconcile_status": "still_pending",
            "outcome": None,
            "run_status": "in_progress",
            "inbound_status": "steered",
            "resubmitted_request_key": None,
        },
        "recover_startup_empty": {
            "mode": "startup",
            "inbound_events": [],
            "background_runs": [],
            "thread_watches": [],
            "automation_runs": [],
        },
        "recover_startup_populated": {
            "mode": "startup",
            "inbound_events": startup_inbound_rows,
            "background_runs": startup_bg_rows,
            "thread_watches": startup_watch_rows,
            "automation_runs": startup_auto_rows,
        },
        "recover_watch_reconnect": {
            "mode": "watch_reconnect",
            "background_runs": startup_bg_rows,
            "thread_watches": startup_watch_rows,
        },
        "recover_watch_reconnect_settled_turn_2": {
            "mode": "watch_reconnect",
            "background_runs": _json_safe_rows(
                [
                    {
                        "run_id": "bg_backfill_2",
                        "thread_id": UUID(THREAD_ID),
                        "dalton_user_id": USER_ID,
                        "platform": "feishu",
                        "target_address": "feishu:@me",
                        "turn_index": 2,
                        "status": "completed",
                        "fallback_pending": True,
                        "sent_to_current_count": 0,
                        "created_at": FIXED_DT,
                        "updated_at": FIXED_DT,
                    }
                ]
            ),
            "thread_watches": _json_safe_rows(
                [
                    {
                        "thread_id": UUID(THREAD_ID),
                        "dalton_user_id": USER_ID,
                        "platform": "feishu",
                        "target_address": "feishu:@me",
                        "scope_key": "dm:ou_alice",
                        "generation": 1,
                        "last_seen_turn_index": 2,
                        "watch_active": True,
                        "created_at": FIXED_DT,
                        "updated_at": FIXED_DT,
                    }
                ]
            ),
        },
    }


def build_i8_fixtures() -> dict[str, Any]:
    """Verbatim return structures from `register_automation_run` and `finish_automation_run` (`src/dalton/database/channel_gateway.py:3893-3964, 4000-4123`)."""
    exec_id = "22222222-3333-4444-5555-666666666666"
    return {
        "register_dm": {
            "targets": [
                {
                    "entry": "feishu",
                    "address": "feishu:@me",
                    "name": "Alice (feishu DM)",
                    "ok": True,
                    "message": None,
                }
            ]
        },
        "finish_claim_finishing": {
            "execution_id": exec_id,
            "status": "finishing",
            "previous_status": "registered",
            "user_id": USER_ID,
            "workspace_id": WORKSPACE_ID,
            "thread_id": THREAD_ID,
            "automation_name": "Daily Morning Brief",
            "finish_status": "completed",
            "final_text": "Summary of market moves.",
            "targets": [
                {
                    "entry": "feishu",
                    "address": "feishu:@me",
                    "name": "Alice (feishu DM)",
                    "ok": True,
                    "message": None,
                    "agent_sent": False,
                }
            ],
        },
        "finish_mark_finished": {
            "execution_id": exec_id,
            "status": "finished",
            "previous_status": "finishing",
            "user_id": USER_ID,
            "workspace_id": WORKSPACE_ID,
            "thread_id": THREAD_ID,
            "automation_name": "Daily Morning Brief",
            "finish_status": "completed",
            "final_text": "Summary of market moves.",
            "targets": [
                {
                    "entry": "feishu",
                    "address": "feishu:@me",
                    "name": "Alice (feishu DM)",
                    "ok": True,
                    "message": None,
                    "agent_sent": False,
                    "reached": True,
                    "via": "fallback",
                    "error": None,
                }
            ],
        },
    }


def build_i9_fixtures() -> dict[str, Any]:
    """Verbatim return structures from `get_weixin_state` and `upsert_weixin_state` (`src/dalton/database/channel_gateway.py:4160-4189, 4313-4325`)."""
    return {
        "get_unconfigured": {
            "configured": False,
            "ilink_bot_id": None,
            "owner_user_id": None,
            "base_url": "https://ilinkai.weixin.qq.com",
            "get_updates_buf": "",
            "status": "unconfigured",
            "updated_at": None,
            "bot_token": None,
        },
        "get_configured": {
            "configured": True,
            "ilink_bot_id": "ilink_bot_88",
            "owner_user_id": USER_ID,
            "base_url": "https://ilinkai.weixin.qq.com",
            "get_updates_buf": "cursor_xyz",
            "status": "active",
            "updated_at": FIXED_ISO,
            "bot_token": "decrypted_secret_bot_token",
        },
        "put_saved": {
            "ok": True,
            "ilink_bot_id": "ilink_bot_88",
            "owner_user_id": USER_ID,
            "base_url": "https://ilinkai.weixin.qq.com",
            "get_updates_buf": "cursor_xyz",
            "status": "active",
            "updated_at": FIXED_ISO,
        },
    }


def build_hitl_fixtures() -> dict[str, Any]:
    """Generated via `replay_items._interrupt_item` (`src/server/services/history/replay/items.py:64-86`, identical to `RunSSEProducer._handle_interrupt` at `src/server/services/runs/sse_producer.py:999-1037`), `ask_user.py:56-66`, `credit_gate.py:610-623`, `order_governance.py:309-331`, and `HITLResponse` / `serialize_hitl_response_map` (`src/server/models/chat.py:104-208`)."""
    # 1. ask_user_question interrupt value from `src/ptc_agent/agent/middleware/ask_user.py:56-66`
    ask_user_interrupt_value = {
        "action_requests": [
            {
                "type": "ask_user_question",
                "question": "Which time horizon should I analyze?",
                "options": ["1 Year", "5 Years"],
                "allow_multiple": False,
            }
        ]
    }
    ask_user_sse = replay_items._interrupt_item(
        THREAD_ID,
        {"id": "int_ask_1", "value": ask_user_interrupt_value},
    )

    # 2. credit_pause interrupt value from `src/ptc_agent/agent/middleware/credit_gate.py:616-623`
    credit_pause_interrupt_value = {
        "action_requests": [
            {
                "type": INTERRUPT_REASON_CREDIT_PAUSE,
                "message": "Your credit limit was reached for this turn.",
            }
        ]
    }
    credit_pause_sse = replay_items._interrupt_item(
        THREAD_ID,
        {"id": "int_credit_1", "value": credit_pause_interrupt_value},
    )

    # 3. order_approval interrupt value from `src/ptc_agent/agent/middleware/order_governance.py:309-331`
    order_approval_interrupt_value = {
        "kind": "order_approval",
        "action_requests": [
            {
                "name": "place_equity_order",
                "args": {"symbol": "AAPL", "qty": 10, "side": "buy"},
                "description": "Buy 10 shares of AAPL at market",
                "tool_call_id": "call_order_1",
                "attempt_id": "att_order_1",
                "order": {"symbol": "AAPL", "qty": 10, "side": "buy"},
            }
        ],
        "review_configs": [
            {
                "action_name": "place_equity_order",
                "allowed_decisions": ["approve", "reject"],
            }
        ],
    }
    order_approval_sse = replay_items._interrupt_item(
        THREAD_ID,
        {"id": "int_order_1", "value": order_approval_interrupt_value},
    )

    # 4. publish_approval interrupt value from `src/dalton/publish/service.py:2219-2263`
    publish_approval_interrupt_value = {
        "kind": "publish_approval",
        "action_requests": [
            {
                "type": "publish_report",
                "name": "publish_report",
                "tool_call_id": "call_publish_1",
                "attempt_id": "req_pub_1",
                "description": "正式发布《AAPL Deep Dive》",
                "args": {
                    "request_id": "req_pub_1",
                    "ticker": "AAPL",
                    "title": "AAPL Deep Dive",
                    "kind": "company_in_depth",
                    "draft_path": "reports/aapl_draft.md",
                    "published_path": "published/aapl_deep_dive.md",
                    "updates_thesis": True,
                    "thesis_version": "v2",
                },
            }
        ],
        "review_configs": [
            {
                "action_name": "publish_report",
                "allowed_decisions": ["approve", "reject"],
            }
        ],
    }
    publish_approval_sse = replay_items._interrupt_item(
        THREAD_ID,
        {"id": "int_publish_1", "value": publish_approval_interrupt_value},
    )

    # 5. HITLResponse models validated via `HITLResponse` (`src/server/models/chat.py:124-142`)
    approve_response = HITLResponse(
        decisions=[HITLDecision(type="approve", message="5 Years")]
    ).model_dump(exclude_none=True)

    skip_reject_response = HITLResponse(
        decisions=[HITLDecision(type="reject", message="")]
    ).model_dump(exclude_none=True)

    serialized_skip = serialize_hitl_response_map(
        {"int_ask_1": HITLResponse(decisions=[HITLDecision(type="reject", message="")])}
    )

    return {
        "ask_user_question_sse": ask_user_sse,
        "credit_pause_sse": credit_pause_sse,
        "order_approval_sse": order_approval_sse,
        "publish_approval_sse": publish_approval_sse,
        "hitl_response_approve": {"int_ask_1": approve_response},
        "hitl_response_skip_reject": {"int_ask_1": skip_reject_response},
        "hitl_response_skip_serialized_by_backend": serialized_skip,
    }


def build_conflict_and_sse_fixtures() -> dict[str, Any]:
    """Generated via `admission_conflict_detail` (`src/server/handlers/chat/admission_gate.py:61-101`), `external_id_conflict_payload` (`src/server/database/conversation/errors.py:70-83`), `messaging.py:360-380`, `error_handling.py:415-680`, and `stream_writer.py:257-269`."""
    running_detail = admission_conflict_detail("running")
    not_running_detail = admission_conflict_detail("not_running")
    stopping_detail = admission_conflict_detail("stopping")
    compacting_detail = admission_conflict_detail("compacting")
    workspace_moving_detail = admission_conflict_detail("workspace_moving")
    turn_conflict_detail = admission_conflict_detail("turn_conflict")
    ext_conflict = external_id_conflict_payload(
        "feishu",
        format_external_thread_id("feishu", "dm:ou_alice", USER_ID, 2, "dm"),
    )

    return {
        "http_409_admission_running": {"detail": running_detail},
        "http_409_admission_not_running": {"detail": not_running_detail},
        "http_409_admission_stopping": {"detail": stopping_detail},
        "http_409_admission_compacting": {"detail": compacting_detail},
        "http_409_admission_workspace_moving": {"detail": workspace_moving_detail},
        "http_409_admission_turn_conflict": {"detail": turn_conflict_detail},
        "http_503_writer_capacity": {
            "detail": {
                "code": "writer_capacity",
                "message": (
                    "The server is at its concurrent-run capacity; please retry "
                    "in a moment."
                ),
            }
        },
        "sse_error_admission_running": {
            "event": "error",
            "data": {
                "thread_id": THREAD_ID,
                "error": running_detail["message"],
                "type": "workflow_error",
                "error_type": "admission_conflict",
                "error_class": "RunSlotBusyError",
                "code": running_detail["code"],
            },
        },
        "http_409_duplicate_in_progress": {
            "detail": {
                "code": "duplicate_request",
                "message": (
                    "This request was already accepted; reconnect to the existing "
                    "run instead of resending."
                ),
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "run_status": "in_progress",
                "retryable": False,
            }
        },
        "http_409_duplicate_cancelled_retryable": {
            "detail": {
                "code": "duplicate_request",
                "message": (
                    "This request was already accepted; reconnect to the existing "
                    "run instead of resending."
                ),
                "thread_id": THREAD_ID,
                "run_id": "ffffffff-1111-2222-3333-444444444444",
                "run_status": "cancelled",
                "retryable": True,
            }
        },
        "http_409_duplicate_error_retryable": {
            "detail": {
                "code": "duplicate_request",
                "message": (
                    "This request was already accepted; reconnect to the existing "
                    "run instead of resending."
                ),
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "run_status": "error",
                "retryable": True,
            }
        },
        "http_409_resend_required": {
            "detail": _resend_required().detail,
        },
        "sse_error_duplicate_in_progress": {
            "event": "error",
            "data": {
                "thread_id": THREAD_ID,
                "error": (
                    "This request was already accepted; reconnect to the "
                    "existing run instead of resending."
                ),
                "type": "workflow_error",
                "error_type": "duplicate_request",
                "error_class": "DuplicateRequestError",
                "code": "duplicate_request",
                "run_id": RUN_ID,
                "run_status": "in_progress",
                "run_thread_id": THREAD_ID,
                "retryable": False,
            },
        },
        "sse_error_writer_capacity": {
            "event": "error",
            "data": {
                "thread_id": THREAD_ID,
                "error": (
                    "The server is at its concurrent-run capacity; please retry "
                    "in a moment."
                ),
                "type": "workflow_error",
                "error_type": "capacity_limit",
                "error_class": "WriterGuardUnavailable",
                "code": "writer_capacity",
            },
        },
        "sse_error_external_id_conflict": {
            "event": "error",
            "data": {
                "thread_id": THREAD_ID,
                "error": ext_conflict["message"],
                "type": "workflow_error",
                "error_type": ext_conflict["error_type"],
                "error_class": "ExternalIdConflictError",
                "platform": ext_conflict["platform"],
                "external_id": ext_conflict["external_id"],
            },
        },
        "http_409_external_id_conflict": {
            "detail": ext_conflict,
        },
        "sse_retry_recoverable": {
            "event": "retry",
            "data": {
                "message": "Temporary error occurred, you can retry or resume the turn",
                "thread_id": THREAD_ID,
                "auto_retry": True,
                "error_type": "rate_limit",
                "error_class": "RateLimitError",
                "retry_count": 1,
                "max_retries": 3,
                "recovery": "retry",
                "run_id": RUN_ID,
            },
        },
        "sse_retry_resend": {
            "event": "retry",
            "data": {
                "message": "Temporary error occurred, you can retry or resume the turn",
                "thread_id": THREAD_ID,
                "auto_retry": True,
                "error_type": "rate_limit",
                "error_class": "RateLimitError",
                "retry_count": 1,
                "max_retries": 3,
                "recovery": "resend",
                "run_id": RUN_ID,
            },
        },
        "sse_steering_accepted": {
            "event": "steering_accepted",
            "data": {
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "status": "accepted",
                "delivery": "steering",
            },
        },
        "sse_run_end_completed": {
            "event": "run_end",
            "data": {
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "outcome": "completed",
            },
        },
        "sse_run_end_error": {
            "event": "run_end",
            "data": {
                "thread_id": THREAD_ID,
                "run_id": RUN_ID,
                "outcome": "error",
            },
        },
        "sse_stream_gap": {
            "event": "stream_gap",
            "data": {
                "expected_from": 3,
                "first_available": 10,
            },
        },
        "sse_stream_new_thread": [
            {
                "event": "metadata",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "turn_index": 0,
                },
            },
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": THREAD_ID,
                    "agent": "main",
                    "id": "msg_new_1",
                    "role": "assistant",
                    "content": "Hello from new thread!",
                    "content_type": "text",
                },
            },
            {
                "event": "run_end",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "outcome": "completed",
                },
            },
        ],
        "sse_stream_existing_thread": [
            {
                "event": "metadata",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "turn_index": 1,
                },
            },
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": THREAD_ID,
                    "agent": "main",
                    "id": "msg_ext_1",
                    "role": "assistant",
                    "content": "Assistant reply on existing thread.",
                    "content_type": "text",
                },
            },
            {
                "event": "run_end",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "outcome": "completed",
                },
            },
        ],
        "sse_stream_retry_turn": [
            {
                "event": "metadata",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "turn_index": 1,
                },
            },
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": THREAD_ID,
                    "agent": "main",
                    "id": "msg_retry_1",
                    "role": "assistant",
                    "content": "Retried failed turn!",
                    "content_type": "text",
                },
            },
            {
                "event": "run_end",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "outcome": "completed",
                },
            },
        ],
        "sse_stream_attached": [
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": THREAD_ID,
                    "agent": "main",
                    "id": "msg_att_1",
                    "role": "assistant",
                    "content": "Streamed output.",
                    "content_type": "text",
                },
            },
            {
                "event": "run_end",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "outcome": "completed",
                },
            },
        ],
        "sse_stream_after_cursor_tail": [
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": THREAD_ID,
                    "agent": "main",
                    "id": "msg_att_tail_1",
                    "role": "assistant",
                    "content": "Tail after cursor.",
                    "content_type": "text",
                },
            },
            {
                "event": "run_end",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "outcome": "completed",
                },
            },
        ],
        "sse_stream_after_cursor_run_end_only": [
            {
                "event": "run_end",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "outcome": "completed",
                },
            },
        ],
        "sse_stream_partial_eof": [
            {
                "event": "metadata",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": RUN_ID,
                    "turn_index": 1,
                },
            },
            {
                "event": "message_chunk",
                "data": {
                    "thread_id": THREAD_ID,
                    "agent": "main",
                    "id": "msg_partial_1",
                    "role": "assistant",
                    "content": "TRUNCATED_PARTIAL_TEXT_DO_NOT_SEND",
                    "content_type": "text",
                },
            },
        ],
        "sse_stream_retry_partial_eof": [
            {
                "event": "metadata",
                "data": {
                    "thread_id": THREAD_ID,
                    "run_id": "run_retry_2",
                    "turn_index": 1,
                },
            },
        ],
    }


def build_replay_fixtures() -> dict[str, Any]:
    """Generated via `replay_items._user_message_item`, `history_projector.messages_to_history_events`, `history_projector.history_events_to_sse`, `replay_items._interrupt_item`, `replay_items._enrich`, `replay_stubs.unavailable_item`, and `messaging.py:1270-1312`."""
    # Turn 5: user message + reasoning + text + tip interrupt
    t5_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 5,
            "content": "Turn 5 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": "run_tip_5",
            "status": "interrupted",
            "created_at": FIXED_DT,
        },
    )
    t5_ai_msg = AIMessage(
        id="ai_msg_t5",
        content=[
            {
                "type": "reasoning",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": "INTERNAL_THINKING_5_SHOULD_NOT_BE_SENT",
                    }
                ],
            },
            {"type": "text", "text": "Turn 5 text"},
        ],
    )
    t5_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events([t5_ai_msg]),
        thread_id=THREAD_ID,
    )
    t5_interrupt = replay_items._interrupt_item(
        THREAD_ID,
        {
            "id": "int_tip_5",
            "value": {
                "action_requests": [
                    {
                        "type": "ask_user_question",
                        "question": "Which ticker?",
                        "options": ["AAPL", "MSFT"],
                        "allow_multiple": False,
                    }
                ]
            },
        },
    )
    t5_items.append(t5_interrupt)
    for item in t5_items:
        replay_items._enrich(item, THREAD_ID, 5, "run_tip_5")

    # Turn 4: user message + text
    t4_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 4,
            "content": "Turn 4 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": "run_4",
            "status": "completed",
            "created_at": FIXED_DT,
        },
    )
    t4_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events(
            [AIMessage(id="ai_msg_t4", content="Turn 4 text")]
        ),
        thread_id=THREAD_ID,
    )
    for item in t4_items:
        replay_items._enrich(item, THREAD_ID, 4, "run_4")

    page_1_events = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 4, "has_more": True},
        },
        t4_user,
        *t4_items,
        t5_user,
        *t5_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": "run_tip_5"},
        },
    ]

    # Page 2 (`before_turn=4`): Turn 2 and Turn 3
    t2_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 2,
            "content": "Turn 2 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": "run_2",
            "status": "completed",
            "created_at": FIXED_DT,
        },
    )
    t2_ai_msg = AIMessage(
        id="ai_msg_t2",
        content=[
            {
                "type": "reasoning",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": "HIDDEN_CHAIN_OF_THOUGHT_DO_NOT_LEAK",
                    }
                ],
            },
            {"type": "text", "text": "Found turn 2 across pages!"},
        ],
    )
    t2_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events([t2_ai_msg]),
        thread_id=THREAD_ID,
    )
    for item in t2_items:
        replay_items._enrich(item, THREAD_ID, 2, "run_2")

    page_2_events = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 2, "has_more": False},
        },
        t2_user,
        *t2_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID},
        },
    ]

    # Single-turn default replay
    t1_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": RUN_ID,
            "status": "completed",
            "created_at": FIXED_DT,
        },
    )
    t1_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events(
            [AIMessage(id="ai_msg_t1", content="Replayed assistant output.")]
        ),
        thread_id=THREAD_ID,
    )
    for item in t1_items:
        replay_items._enrich(item, THREAD_ID, 1, RUN_ID)

    default_single_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_user,
        *t1_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    unavailable_err = replay_stubs.unavailable_item(THREAD_ID)
    replay_items._enrich(unavailable_err, THREAD_ID, 1, RUN_ID)

    # Replay page with subagent `task:<id>` lane events (`src/server/services/history/replay/lanes.py:11`)
    # and a turn where main text precedes `tool_calls` (`send_message`) with no post-tool text
    task_chunk_item = {
        "event": "message_chunk",
        "data": {
            "thread_id": THREAD_ID,
            "agent": "task:subagent_99",
            "id": "ai_msg_task_99",
            "role": "assistant",
            "content": "SUBAGENT_TASK_TEXT_MUST_NOT_LEAK",
            "content_type": "text",
            "finish_reason": "stop",
        },
    }
    replay_items._enrich(task_chunk_item, THREAD_ID, 1, RUN_ID)

    task_tool_call_item = {
        "event": "tool_calls",
        "data": {
            "thread_id": THREAD_ID,
            "agent": "task:subagent_99",
            "id": "ai_tc_task_99",
            "role": "assistant",
            "tool_calls": [{"name": "web_search", "args": {"q": "AAPL"}, "id": "tc_sub_1", "type": "tool_call"}],
            "finish_reason": "tool_calls",
        },
    }
    replay_items._enrich(task_tool_call_item, THREAD_ID, 1, RUN_ID)

    main_tool_call_item = {
        "event": "tool_calls",
        "data": {
            "thread_id": THREAD_ID,
            "agent": "main",
            "id": "ai_tc_main_1",
            "role": "assistant",
            "tool_calls": [{"name": "send_message", "args": {"text": "Final answer before send_message"}, "id": "tc_main_1", "type": "tool_call"}],
            "finish_reason": "tool_calls",
        },
    }
    replay_items._enrich(main_tool_call_item, THREAD_ID, 1, RUN_ID)

    single_page_with_task_and_pre_tool = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_user,
        *t1_items,
        task_chunk_item,
        task_tool_call_item,
        main_tool_call_item,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    # In-progress turn stub (`src/server/services/history/replay/stubs.py:85-97`, `items.py:49-60`)
    # plus `event: snapshot` (`src/server/services/history/snapshot.py:105-115`)
    t1_in_progress_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": RUN_ID,
            "status": "in_progress",
            "created_at": FIXED_DT,
        },
        open_run=True,
    )
    in_progress_turn_with_snapshot = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_in_progress_user,
        {
            "event": "snapshot",
            "data": {
                "thread_id": THREAD_ID,
                "active_runs": [
                    {
                        "run_id": RUN_ID,
                        "lane": "main",
                        "started_at": FIXED_ISO,
                        "cursor": {"last_event_id": 2},
                    }
                ],
            },
        },
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    in_progress_turn_stub_without_snapshot = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_in_progress_user,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    truncated_page_without_replay_done = default_single_page[:-1]

    # Directly settled report-back turn 3 (`settled_Directly: True, run_id: None`)
    t3_settled_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 3,
            "content": "Report back",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response=None,
    )
    t3_settled_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events(
            [
                AIMessage(
                    id="ai_msg_sd_3",
                    content="Shape 2 directly settled report-back!",
                )
            ]
        ),
        thread_id=THREAD_ID,
    )
    for item in t3_settled_items:
        replay_items._enrich(item, THREAD_ID, 3, None)

    settled_directly_turn_3_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 3, "has_more": False},
        },
        t3_settled_user,
        *t3_settled_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": None},
        },
    ]

    # Backfilled turn 2 replay (`bg_backfill_2`)
    t2_backfill_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 2,
            "content": "Wakeup report back",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": "bg_backfill_2",
            "status": "completed",
            "created_at": FIXED_DT,
        },
    )
    t2_backfill_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events(
            [
                AIMessage(
                    id="ai_msg_bf_2",
                    content="Backfilled wakeup report from disconnect window.",
                )
            ]
        ),
        thread_id=THREAD_ID,
    )
    for item in t2_backfill_items:
        replay_items._enrich(item, THREAD_ID, 2, "bg_backfill_2")

    backfilled_turn_2_single_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 2, "has_more": False},
        },
        t2_backfill_user,
        *t2_backfill_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": "bg_backfill_2"},
        },
    ]

    # In-progress system (`report_back`) turn with `report_run_id` (`src/server/services/history/replay/items.py:40-48`)
    t2_in_progress_system_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 2,
            "type": "system",
            "content": "Wakeup report back",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": "bg_backfill_2",
            "status": "in_progress",
            "created_at": FIXED_DT,
        },
        open_run="bg_backfill_2",
    )
    in_progress_system_turn_with_report_run_id = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 2, "has_more": False},
        },
        t2_in_progress_system_user,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": "bg_backfill_2"},
        },
    ]

    t1_cursor_settled_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events(
            [
                AIMessage(
                    id="ai_msg_t1_cursor",
                    content="Prefix before cursor. Tail after cursor.",
                )
            ]
        ),
        thread_id=THREAD_ID,
    )
    for item in t1_cursor_settled_items:
        replay_items._enrich(item, THREAD_ID, 1, RUN_ID)

    settled_turn_1_after_cursor_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_user,
        *t1_cursor_settled_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    # Retry in-progress turn 1 where previous attempt `run_failed_1` had terminal `error` status (`stubs.py:86-98`, `items.py:56-60`)
    t1_retry_in_progress_stub_items = replay_stubs._stub_items(
        THREAD_ID,
        1,
        [
            {
                "turn_index": 1,
                "content": "Turn 1 prompt",
                "created_at": FIXED_ISO,
                "metadata": {},
            }
        ],
        response={
            "conversation_response_id": "run_failed_1",
            "status": "error",
            "created_at": FIXED_DT,
            "completed_at": FIXED_DT,
            "usage_settled_at": FIXED_DT,
            "errors": ["Temporary error"],
        },
        open_run="run_retry_2",
    )
    retry_in_progress_with_prev_failed_attempt_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        *t1_retry_in_progress_stub_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": "run_retry_2"},
        },
    ]

    t1_retry_settled_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": "run_retry_2",
            "status": "completed",
            "created_at": FIXED_DT,
            "completed_at": FIXED_DT,
            "usage_settled_at": FIXED_DT,
        },
    )
    t1_retry_settled_items = history_projector.history_events_to_sse(
        history_projector.messages_to_history_events(
            [
                AIMessage(
                    id="ai_msg_retry_2",
                    content="Recovered answer from retry attempt 2!",
                )
            ]
        ),
        thread_id=THREAD_ID,
    )
    for item in t1_retry_settled_items:
        replay_items._enrich(item, THREAD_ID, 1, "run_retry_2")
    retry_settled_new_attempt_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_retry_settled_user,
        *t1_retry_settled_items,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": "run_retry_2"},
        },
    ]

    def _make_settled_turn_1_page(rid: str, text: str = "Replayed assistant output.") -> list[dict[str, Any]]:
        u = replay_items._user_message_item(
            THREAD_ID,
            {
                "turn_index": 1,
                "content": "Turn 1 prompt",
                "created_at": FIXED_ISO,
                "metadata": {},
            },
            response={
                "conversation_response_id": rid,
                "status": "completed",
                "created_at": FIXED_DT,
                "completed_at": FIXED_DT,
                "usage_settled_at": FIXED_DT,
            },
        )
        items_list = history_projector.history_events_to_sse(
            history_projector.messages_to_history_events(
                [AIMessage(id=f"ai_msg_{rid}", content=text)]
            ),
            thread_id=THREAD_ID,
        )
        for it in items_list:
            replay_items._enrich(it, THREAD_ID, 1, rid)
        return [
            {
                "event": "history_page",
                "data": {"first_turn_index": 1, "has_more": False},
            },
            u,
            *items_list,
            {
                "event": "replay_done",
                "data": {"thread_id": THREAD_ID, "latest_run_id": rid},
            },
        ]

    in_progress_attach_drop_with_snapshot = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_in_progress_user,
        {
            "event": "snapshot",
            "data": {
                "thread_id": THREAD_ID,
                "active_runs": [
                    {
                        "run_id": "run_attach_drop_recover",
                        "lane": "main",
                        "started_at": FIXED_ISO,
                        "cursor": {"last_event_id": 0},
                    }
                ],
            },
        },
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": "run_attach_drop_recover"},
        },
    ]

    t1_cancelled_resp = {
        "conversation_response_id": RUN_ID,
        "status": "cancelled",
        "cancel_requested_at": FIXED_DT,
        "metadata": {"cancelled_by_user": True},
        "created_at": FIXED_DT,
        "completed_at": FIXED_DT,
        "usage_settled_at": FIXED_DT,
    }
    t1_cancelled_empty_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response=t1_cancelled_resp,
    )
    t1_stop_close = replay_stopped.stop_close_item(
        THREAD_ID, t1_cancelled_resp, []
    )
    assert t1_stop_close is not None
    replay_items._enrich(t1_stop_close, THREAD_ID, 1, RUN_ID)
    cancelled_empty_turn_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_cancelled_empty_user,
        t1_stop_close,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    t1_error_resp = {
        "conversation_response_id": RUN_ID,
        "status": "error",
        "created_at": FIXED_DT,
        "completed_at": FIXED_DT,
        "usage_settled_at": FIXED_DT,
        "errors": ["Sandbox execution timed out"],
    }
    t1_error_empty_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response=t1_error_resp,
    )
    t1_err_item = replay_items._error_item(THREAD_ID, t1_error_resp)
    assert t1_err_item is not None
    replay_items._enrich(t1_err_item, THREAD_ID, 1, RUN_ID)
    error_empty_turn_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_error_empty_user,
        t1_err_item,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    t1_interrupted_empty_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": RUN_ID,
            "status": "interrupted",
            "created_at": FIXED_DT,
            "completed_at": FIXED_DT,
            "usage_settled_at": FIXED_DT,
        },
    )
    interrupted_empty_turn_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_interrupted_empty_user,
        {
            "event": "interrupt",
            "data": {
                "thread_id": THREAD_ID,
                "turn_index": 1,
                "interrupt_id": "int_empty_turn_1",
                "run_id": RUN_ID,
                "action_requests": [
                    {
                        "type": "ask_user_question",
                        "args": {
                            "questions": [
                                {
                                    "id": "q_empty_1",
                                    "question": "Which market should I check first?",
                                    "options": [{"label": "US"}, {"label": "HK"}],
                                }
                            ]
                        },
                    }
                ],
            },
        },
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    t1_completed_empty_user = replay_items._user_message_item(
        THREAD_ID,
        {
            "turn_index": 1,
            "content": "Turn 1 prompt",
            "created_at": FIXED_ISO,
            "metadata": {},
        },
        response={
            "conversation_response_id": RUN_ID,
            "status": "completed",
            "created_at": FIXED_DT,
            "completed_at": FIXED_DT,
            "usage_settled_at": FIXED_DT,
        },
    )
    completed_empty_turn_page = [
        {
            "event": "history_page",
            "data": {"first_turn_index": 1, "has_more": False},
        },
        t1_completed_empty_user,
        {
            "event": "replay_done",
            "data": {"thread_id": THREAD_ID, "latest_run_id": RUN_ID},
        },
    ]

    return {
        "default_single_page": default_single_page,
        "recover_run_rec_1_page": _make_settled_turn_1_page("run_rec_1"),
        "recover_bg_rec_1_page": _make_settled_turn_1_page("bg_rec_1"),
        "in_progress_attach_drop_with_snapshot": in_progress_attach_drop_with_snapshot,
        "settled_attach_drop_page": _make_settled_turn_1_page("run_attach_drop_recover"),
        "settled_turn_1_after_cursor_page": settled_turn_1_after_cursor_page,
        "retry_in_progress_with_prev_failed_attempt_page": retry_in_progress_with_prev_failed_attempt_page,
        "retry_settled_new_attempt_page": retry_settled_new_attempt_page,
        "cancelled_empty_turn_page": cancelled_empty_turn_page,
        "error_empty_turn_page": error_empty_turn_page,
        "interrupted_empty_turn_page": interrupted_empty_turn_page,
        "completed_empty_turn_page": completed_empty_turn_page,
        "page_1_with_more_and_tip_interrupt": page_1_events,
        "page_2_oldest": page_2_events,
        "stub_replay_unavailable_event": unavailable_err,
        "single_page_with_task_and_pre_tool": single_page_with_task_and_pre_tool,
        "in_progress_turn_with_snapshot": in_progress_turn_with_snapshot,
        "in_progress_turn_stub_without_snapshot": in_progress_turn_stub_without_snapshot,
        "in_progress_system_turn_with_report_run_id": in_progress_system_turn_with_report_run_id,
        "truncated_page_without_replay_done": truncated_page_without_replay_done,
        "settled_directly_turn_3_page": settled_directly_turn_3_page,
        "backfilled_turn_2_single_page": backfilled_turn_2_single_page,
    }


def build_status_liveness_wakeups_watch_fixtures() -> dict[str, Any]:
    """Generated via `to_public`, `run_completed_at`, `WakeupsResponse`/`WakeupOut` (`src/server/models/wakeup.py:60-88`), `thread_status.py:284-302`, `status.py:95-107`, `messaging.py:1546-1594`, and `wake.py:80-87, 141-184`."""
    # 1. `/dispatches/liveness` (`src/server/app/threads/messaging.py:1453-1594`)
    live_row = {
        "conversation_response_id": RUN_ID,
        "conversation_thread_id": THREAD_ID,
        "status": "in_progress",
        "cancel_requested_at": None,
        "started_at": FIXED_DT,
        "completed_at": None,
        "created_at": FIXED_DT,
        "usage_settled_at": None,
    }
    completed_row = {
        "conversation_response_id": RUN_ID,
        "conversation_thread_id": THREAD_ID,
        "status": "completed",
        "cancel_requested_at": None,
        "started_at": FIXED_DT,
        "completed_at": FIXED_DT,
        "created_at": FIXED_DT,
        "usage_settled_at": FIXED_DT,
    }

    def _make_run_item(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": str(row["conversation_response_id"]),
            "thread_id": str(row["conversation_thread_id"]),
            "status": to_public(
                row["status"], cancel_requested_at=row.get("cancel_requested_at")
            ),
            "run_started_at": row["created_at"].isoformat(),
            "run_finished_at": run_completed_at(row),
        }

    liveness_running_response = {
        "liveness": [
            {
                "thread_id": THREAD_ID,
                "status": "running",
                "run_started_at": FIXED_ISO,
                "run_finished_at": None,
                "run_id": RUN_ID,
                "can_reconnect": True,
                "last_activity_at": FIXED_ISO,
                "title": "AAPL Analysis",
                "workspace_name": "Quant Workspace",
            }
        ],
        "runs": [_make_run_item(live_row)],
        "steers": [],
    }

    liveness_completed_response = {
        "liveness": [
            {
                "thread_id": THREAD_ID,
                "status": "completed",
                "run_started_at": FIXED_ISO,
                "run_finished_at": FIXED_ISO,
                "run_id": None,
                "can_reconnect": False,
                "last_activity_at": FIXED_ISO,
                "title": "AAPL Analysis",
                "workspace_name": "Quant Workspace",
            }
        ],
        "runs": [_make_run_item(completed_row)],
        "steers": [
            {
                "steer_id": "steer_1",
                "thread_id": THREAD_ID,
                "state": "taken",
                "run_id": RUN_ID,
            }
        ],
    }

    # 2. `/status?fields=report_back` (`src/server/services/report_back/handoff/status.py:95-107`)
    report_back_slice_idle = {
        "thread_id": THREAD_ID,
        "pending_report_back": False,
        "report_back_run_id": None,
        "recent_report_back_run_ids": [],
        "owed_threads": [],
        "active_tasks": [],
        "active_commands": [],
    }

    report_back_slice_active = {
        "thread_id": THREAD_ID,
        "pending_report_back": True,
        "report_back_run_id": "bg_run_99",
        "recent_report_back_run_ids": ["bg_run_settled_3"],
        "owed_threads": [
            {
                "thread_id": "22222222-3333-4444-5555-666666666666",
                "workspace_id": WORKSPACE_ID,
                "workspace_name": "Quant Workspace",
                "title": "Analyst Deep Dive",
            }
        ],
        "active_tasks": ["task_1"],
        "active_commands": ["cmd_1"],
    }

    # 3. Full `GET /api/v1/threads/{id}/status` (`src/server/services/thread_status.py:284-302` + `messaging.py:1442`)
    full_status_idle = {
        "thread_id": THREAD_ID,
        "run_id": None,
        "status": "idle",
        "can_reconnect": False,
        "latest_turn_index": 1,
        "token_totals": None,
        "next_summary": None,
        "last_update": FIXED_ISO,
        "progress": {
            "has_plan": False,
            "has_final_report": False,
            "completed": True,
            "checkpoint_id": "cp_1",
        },
        "active_tasks": [],
        "active_commands": [],
        "is_shared": False,
        "pending_report_back": False,
        "report_back_run_id": None,
        "recent_report_back_run_ids": [],
        "owed_threads": [],
        "owed_report_kinds": [],
        "pending_steers": [],
    }

    full_status_interrupted = {
        **full_status_idle,
        "status": "interrupted",
    }

    full_status_running = {
        **full_status_idle,
        "run_id": RUN_ID,
        "status": "running",
        "can_reconnect": True,
    }

    full_status_stopping = {
        **full_status_idle,
        "run_id": RUN_ID,
        "status": "stopping",
        "can_reconnect": False,
    }

    full_status_compacting = {
        **full_status_idle,
        "run_id": RUN_ID,
        "status": "compacting",
        "can_reconnect": False,
    }

    # 4. `GET /api/v1/threads/{id}/wakeups` (`src/server/models/wakeup.py:60-88`)
    def _make_wakeup(wakeup_id: str, state: str) -> dict[str, Any]:
        w = WakeupOut(
            wakeup_id=wakeup_id,
            short_id=wakeup_id[:8],
            label="Check AAPL after market close",
            kind="at",
            state=state,  # type: ignore[arg-type]
            timezone="America/New_York",
            at=FIXED_DT,
            next_fire_at=FIXED_DT if state == "armed" else None,
            fires_done=0 if state in ("armed", "held", "held_usage") else 1,
            fires_total=1,
            price=WakeupPriceOut(
                symbol="AAPL",
                kind="above",
                value=250.0,
            ),
            cancellable=state in ("armed", "held", "held_usage"),
            legacy=False,
            created_at=FIXED_DT,
        )
        return json.loads(
            WakeupsResponse(
                thread_id=THREAD_ID,
                user_timezone="America/New_York",
                wakeups=[w],
            ).model_dump_json()
        )

    # 5. `/watch` SSE events (`src/server/services/report_back/handoff/wake.py:80-87, 141-184`)
    watch_events = {
        "watch_snapshot": {
            "event": "watch_snapshot",
            "data": report_back_slice_idle,
        },
        "watch_snapshot_with_owed_threads": {
            "event": "watch_snapshot",
            "data": {
                **report_back_slice_idle,
                "pending_report_back": True,
                "owed_threads": report_back_slice_active["owed_threads"],
            },
        },
        "watch_snapshot_with_recent_settled": {
            "event": "watch_snapshot",
            "data": {
                **report_back_slice_idle,
                "recent_report_back_run_ids": ["bg_run_settled_3"],
            },
        },
        "workflow_started_shape_1_run_id": {
            "event": "workflow_started",
            "data": {"thread_id": THREAD_ID, "run_id": "bg_run_99"},
        },
        "workflow_started_shape_2_settled_directly": {
            "event": "workflow_started",
            "data": {
                "thread_id": THREAD_ID,
                "run_id": None,
                "settled_Directly": True,
                "status": "completed",
                "turn_index": 3,
            },
        },
        "workflow_started_shape_2_error": {
            "event": "workflow_started",
            "data": {"error": "Background sandbox failed to boot."},
        },
        "workflow_started_shape_3_needs_input": {
            "event": "workflow_started",
            "data": {
                "needs_input": "22222222-3333-4444-5555-666666666666",
            },
        },
        "workflow_started_shape_4_cleared": {
            "event": "workflow_started",
            "data": {"thread_id": THREAD_ID, "cleared": True},
        },
        "timeout": {
            "event": "timeout",
            "data": {},
        },
    }

    status_full_with_owed_thread = {
        **full_status_idle,
        "pending_report_back": True,
        "owed_threads": report_back_slice_active["owed_threads"],
    }

    analyst_subthread_status_pause_1 = {
        **full_status_idle,
        "thread_id": "22222222-3333-4444-5555-666666666666",
        "run_id": "sub_run_1",
        "status": "interrupted",
        "latest_turn_index": 1,
    }

    analyst_subthread_status_pause_2 = {
        **full_status_idle,
        "thread_id": "22222222-3333-4444-5555-666666666666",
        "run_id": "sub_run_2",
        "status": "interrupted",
        "latest_turn_index": 2,
    }

    analyst_subthread_status_interrupted_no_progress = {
        **full_status_idle,
        "thread_id": "22222222-3333-4444-5555-666666666666",
        "run_id": None,
        "status": "interrupted",
        "latest_turn_index": 1,
        "progress": None,
    }

    return {
        "dispatches_liveness_running": liveness_running_response,
        "dispatches_liveness_completed": liveness_completed_response,
        "status_report_back_slice_idle": report_back_slice_idle,
        "status_report_back_slice_active": report_back_slice_active,
        "status_full_idle": full_status_idle,
        "status_full_interrupted": full_status_interrupted,
        "status_full_running": full_status_running,
        "status_full_stopping": full_status_stopping,
        "status_full_compacting": full_status_compacting,
        "status_full_with_owed_thread": status_full_with_owed_thread,
        "analyst_subthread_status_pause_1": analyst_subthread_status_pause_1,
        "analyst_subthread_status_pause_2": analyst_subthread_status_pause_2,
        "analyst_subthread_status_interrupted_no_progress": analyst_subthread_status_interrupted_no_progress,
        "http_404_thread_not_found": {"detail": "Thread not found"},
        "wakeups_armed": _make_wakeup("w_armed_1", "armed"),
        "wakeups_held": _make_wakeup("w_held_1", "held"),
        "wakeups_held_usage": _make_wakeup("w_held_usage_1", "held_usage"),
        "wakeups_fired": _make_wakeup("w_fired_1", "fired"),
        "wakeups_empty": json.loads(
            WakeupsResponse(
                thread_id=THREAD_ID,
                user_timezone="America/New_York",
                wakeups=[],
            ).model_dump_json()
        ),
        "watch_events": watch_events,
    }


SOURCES_MD = r"""# Channel Gateway Test Fixtures — Backend Source Provenance (`SOURCES.md`)

All JSON files in this directory are generated by running:

```bash
.venv/bin/python scripts/dalton/gen_channel_gateway_fixtures.py
```

| Fixture File | Key / Payload | Generation Method (Direct Function Call vs Verbatim Dict Copy) | Backend Source (`file:line`) |
|---|---|---|---|
| `i1_register_and_resolve.json` | `default_resolved`, `lazy_home`, `colleague_stopped`, `new_thread_unresolved` | **Direct call** to `_serialize_inbound_row()` and `format_external_thread_id()` on hand-constructed DB row dicts | `src/dalton/database/channel_gateway.py:148-164`, `src/dalton/database/channel_gateway.py:374-417` |
| `i2_bind_code_consume.json` | `bind_success`, `bind_revoked_in_group`, `bind_invalid_or_expired`, `unbind_success` | **Verbatim dict copy** of return structures from `consume_bind_code_or_unbind()` | `src/dalton/database/channel_gateway.py:1063-1133`, `src/dalton/database/channel_gateway.py:1243-1248` |
| `i3_session_command.json` | `workspace_list`, `workspace_switch_dm`, `new_session`, `stop_session` | **Direct call** to `format_external_thread_id()` + **verbatim dict copy** of return structures from `execute_session_command()` | `src/dalton/database/channel_gateway.py:1418-1426`, `:1499-1505`, `:1593-1597`, `:1669-1674` |
| `i4_state.json` | `default_state` | **Direct call** to `default_app_settings()` (`feishu`, `discord`, `weixin`) and `format_external_thread_id()` + **hand-constructed outer response dict** matching `get_channel_internal_state()` (`user_tier: "internal"`, no `writable_workspaces` or `known_group_chats`) | `src/dalton/database/channel_gateway.py:124-136`, `src/dalton/database/channel_gateway.py:1882-1894`, `src/dalton/access_tiers.py:25-32` |
| `i5_settings_put.json` | `put_success`, `automation_output_success`, `version_conflict_409`, `group_permission_403` | **Direct call** to `default_app_settings()` for `settings` + **verbatim dict copy** of return/error structures from `update_user_channel_settings()` | `src/dalton/database/channel_gateway.py:2024-2027`, `src/dalton/database/channel_gateway.py:2395-2408` |
| `i6_authorize_and_claim.json` | `claimed_feishu_dm`, `already_completed_sent`, `in_flight_or_unknown`, `denied_not_allowed` | **Verbatim dict copy** of return structures from `authorize_and_claim_delivery()` | `src/dalton/database/channel_gateway.py:2548-2596`, `src/dalton/database/channel_gateway.py:2988-2996` |
| `i7_progress_and_reconcile.json` | `delivery_progress_sent`, `inbound_progress_default`, `reconcile_*`, `recover_*` | **Direct call** to `_json_safe_rows()` and `uuid5(DALTON_CHANNEL_NAMESPACE, ...)` on rows verified against `channel_gateway.py:3733` WHERE predicate + **verbatim dict copy** of return structures from `update_delivery_progress()`, `update_inbound_progress()`, `reconcile_steer()`, `recover_channel_state()` | `src/dalton/database/channel_gateway.py:501`, `:3130-3135`, `:3365-3372`, `:3467-3572`, `:3730-3788` |
| `i8_automation_runs.json` | `register_dm`, `finish_claim_finishing`, `finish_mark_finished` | **Verbatim dict copy** of return structures from `register_automation_run()` and `finish_automation_run()` | `src/dalton/database/channel_gateway.py:3893-3964`, `src/dalton/database/channel_gateway.py:4000-4123` |
| `i9_weixin_state.json` | `get_unconfigured`, `get_configured`, `put_saved` | **Verbatim dict copy** of return structures from `get_weixin_state()` and `upsert_weixin_state()` | `src/dalton/database/channel_gateway.py:4160-4189`, `src/dalton/database/channel_gateway.py:4313-4325` |
| `hitl_interrupts_and_responses.json` | `ask_user_question_sse`, `credit_pause_sse`, `order_approval_sse`, `publish_approval_sse`, `hitl_response_*` | **Direct call** to `replay_items._interrupt_item()`, `HITLResponse`, `HITLDecision`, `serialize_hitl_response_map()`, `INTERRUPT_REASON_CREDIT_PAUSE` on interrupt value dicts copied from `ask_user.py`, `credit_gate.py`, `order_governance.py`, and `src/dalton/publish/service.py` | `src/ptc_agent/agent/middleware/ask_user.py:56-83`, `src/ptc_agent/agent/middleware/credit_gate.py:610-623`, `src/ptc_agent/agent/middleware/order_governance.py:309-331`, `src/dalton/publish/service.py:2219-2263`, `src/server/services/history/replay/items.py:64-86`, `src/server/models/chat.py:104-208` |
| `conflict_and_sse_events.json` | `http_409_*`, `sse_error_*`, `sse_retry_recoverable`, `sse_retry_resend`, `sse_steering_accepted`, `sse_run_end_*`, `sse_stream_gap`, `sse_stream_*` | **Direct call** to `admission_conflict_detail()` and `external_id_conflict_payload()` + **verbatim SSE frame dicts** from `error_handling.py` (`recovery: "retry" \| "resend"`), `stream_writer.py` (`run_end`), `run_stream_reader.py` (`stream_gap: {expected_from, first_available}`), and `sse_producer.py` | `src/server/handlers/chat/admission_gate.py:61-101`, `src/server/database/conversation/errors.py:70-83`, `src/server/app/threads/messaging.py:360-380`, `src/server/handlers/chat/error_handling.py:293-297, 415-680`, `src/server/services/runs/pending_input.py:63-70`, `src/server/services/runs/stream_writer.py:257-269`, `src/server/handlers/chat/run_stream_reader.py:199-206` |
| `replay_events.json` | `default_single_page`, `page_1_with_more_and_tip_interrupt`, `page_2_oldest`, `stub_replay_unavailable_event`, `single_page_with_task_and_pre_tool` | **Direct call** to `replay_items._user_message_item()`, `history_projector.messages_to_history_events()`, `history_projector.history_events_to_sse()`, `replay_items._interrupt_item()`, `replay_items._enrich()`, `replay_stubs.unavailable_item()` + **verbatim dict copy** of `history_page`, `replay_done`, and `task:<id>` lane items | `src/server/app/threads/messaging.py:1268-1312`, `src/server/services/history/replay/items.py:22-86, 359-370`, `src/server/services/history/replay/stubs.py:60-75`, `src/server/services/history/projector.py:80-224`, `src/server/services/history/replay/lanes.py:10-11` |
| `status_liveness_wakeups_watch.json` | `dispatches_liveness_*`, `status_*`, `wakeups_*`, `watch_events` | **Direct call** to `to_public()`, `run_completed_at()`, `WakeupsResponse`, `WakeupOut`, `WakeupPriceOut` + **verbatim dict copy** of `read_report_back_slice()`, `read_thread_runtime_status()`, and `publish_wake()` / `watch_wakes()` frames (`needs_input: "<ptc_thread_id>"` string) | `src/server/app/threads/messaging.py:1453-1594`, `src/server/services/report_back/handoff/status.py:95-107`, `src/server/services/thread_status.py:284-302`, `src/server/models/wakeup.py:60-88`, `src/server/services/report_back/handoff/wake.py:80-87, 141-184`, `src/server/services/report_back/handoff/core.py:81-83` |
"""


def main() -> None:
    _write_json("i1_register_and_resolve.json", build_i1_fixtures())
    _write_json("i2_bind_code_consume.json", build_i2_fixtures())
    _write_json("i3_session_command.json", build_i3_fixtures())
    _write_json("i4_state.json", build_i4_fixtures())
    _write_json("i5_settings_put.json", build_i5_fixtures())
    _write_json("i6_authorize_and_claim.json", build_i6_fixtures())
    _write_json("i7_progress_and_reconcile.json", build_i7_fixtures())
    _write_json("i8_automation_runs.json", build_i8_fixtures())
    _write_json("i9_weixin_state.json", build_i9_fixtures())
    _write_json("hitl_interrupts_and_responses.json", build_hitl_fixtures())
    _write_json("conflict_and_sse_events.json", build_conflict_and_sse_fixtures())
    _write_json("replay_events.json", build_replay_fixtures())
    _write_json(
        "status_liveness_wakeups_watch.json",
        build_status_liveness_wakeups_watch_fixtures(),
    )
    (FIXTURES_DIR / "SOURCES.md").write_text(SOURCES_MD, encoding="utf-8")
    print(f"Generated 13 fixture files + SOURCES.md in {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
