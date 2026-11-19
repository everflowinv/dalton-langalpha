"""Unit tests for `dalton_report_delivery`, creation warnings, and first-rejection DM."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, create_autospec
from uuid import UUID, uuid4

import pytest
from starlette.requests import Request

from src.dalton.database import automation_reports as reports_db
from src.dalton.services import report_delivery as rd_svc
from src.dalton.tools import build_dalton_tools
from src.dalton.tools.report_delivery_tools import create_report_delivery_tools
from src.ptc_agent.agent.prompts import get_loader
from src.server.app import automations as automations_app
from src.server.models.automation import AutomationCreate

_AUTO_UUID = "11111111-1111-1111-1111-111111111111"
_EXEC_UUID = "22222222-2222-2222-2222-222222222222"
_WS_UUID = "33333333-3333-3333-3333-333333333333"
_OWNER = "user-chao"
_OTHER_USER = "user-other"
_ADMIN = "user-admin"
_EXT_USER = "user-external"

_FEISHU_CHAT_AUTH = "feishu:chat:oc_group_auth_1"
_FEISHU_CHAT_UNAUTH = "feishu:chat:oc_group_unauth_9"
_DISCORD_CHAN_AUTH = "discord:channel:998877665544332211"
_CALLER_FEISHU_DM = "feishu:user:ou_chao_open_1"
_CALLER_DISCORD_DM = "discord:user:424242424242424242"

_DAILY_FILING_INST = "按 skills/dalton-daily-filing-alert/SKILL.md 运行并提交报告。"


def _cfg(user_id: str | None) -> dict[str, Any]:
    if not user_id:
        return {"configurable": {}}
    return {"configurable": {"user_id": user_id, "thread_id": "t-1"}}


def _make_automation_row(
    *,
    automation_id: str = _AUTO_UUID,
    user_id: str = _OWNER,
    name: str = "Chao Daily Filing",
    instruction: str = _DAILY_FILING_INST,
    metadata: dict[str, Any] | None = None,
    config_version: int = 3,
) -> dict[str, Any]:
    return {
        "automation_id": automation_id,
        "user_id": user_id,
        "name": name,
        "instruction": instruction,
        "metadata": dict(metadata or {}),
        "config_version": config_version,
    }


def _authorized_bindings() -> list[dict[str, Any]]:
    return [
        {
            "platform": "feishu",
            "chat_address": "feishu:oc_group_auth_1",
            "chat_name": "Research Group 1",
            "workspace_id": None,
            "bound_by_user_id": _ADMIN,
            "target": _FEISHU_CHAT_AUTH,
        },
        {
            "platform": "discord",
            "chat_address": "discord:998877665544332211",
            "chat_name": "discord-reports",
            "workspace_id": None,
            "bound_by_user_id": _ADMIN,
            "target": _DISCORD_CHAN_AUTH,
        },
    ]


def _caller_identities() -> list[dict[str, Any]]:
    return [
        {
            "platform": "feishu",
            "platform_user_id": "ou_chao_open_1",
            "target": _CALLER_FEISHU_DM,
            "supports_report_cards": True,
            "supports_text_targets": True,
            "supports_failure_targets": False,
        },
        {
            "platform": "discord",
            "platform_user_id": "424242424242424242",
            "target": _CALLER_DISCORD_DM,
            "supports_report_cards": False,
            "supports_text_targets": True,
            "supports_failure_targets": True,
        },
    ]


def _patch_common_set_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tmp_path: Path,
    tier: str = "internal",
    auto_row: dict[str, Any] | None = None,
    is_feishu_member: bool = True,
    master_enabled: bool = True,
) -> dict[str, Any]:
    row = auto_row if auto_row is not None else _make_automation_row()

    get_tier_mock = create_autospec(rd_svc.get_user_tier, return_value=tier)
    monkeypatch.setattr(rd_svc, "get_user_tier", get_tier_mock)

    async def _fake_get_auto(aid: str, uid: str, *, conn: Any = None) -> dict[str, Any] | None:
        if aid == row["automation_id"] and uid == row["user_id"]:
            return dict(row)
        return None

    get_auto_mock = create_autospec(rd_svc.auto_db.get_automation, side_effect=_fake_get_auto)
    monkeypatch.setattr(rd_svc.auto_db, "get_automation", get_auto_mock)

    list_dm_mock = create_autospec(
        rd_svc.list_caller_dm_identities,
        return_value=_caller_identities(),
    )
    monkeypatch.setattr(rd_svc, "list_caller_dm_identities", list_dm_mock)

    list_groups_mock = create_autospec(
        rd_svc.list_authorized_group_bindings,
        return_value=_authorized_bindings(),
    )
    monkeypatch.setattr(rd_svc, "list_authorized_group_bindings", list_groups_mock)

    open_id_mock = create_autospec(
        rd_svc.get_feishu_open_id_for_user,
        return_value="ou_chao_open_1",
    )
    monkeypatch.setattr(rd_svc, "get_feishu_open_id_for_user", open_id_mock)

    member_reason = None if is_feishu_member else "你不是该飞书群成员，无法将其设为报告发送对象"
    verify_member_mock = create_autospec(
        rd_svc.verify_feishu_chat_member,
        return_value=(is_feishu_member, member_reason),
    )
    monkeypatch.setattr(rd_svc, "verify_feishu_chat_member", verify_member_mock)

    sync_chats_mock = create_autospec(rd_svc.sync_owner_group_chat_settings, return_value=None)
    monkeypatch.setattr(rd_svc, "sync_owner_group_chat_settings", sync_chats_mock)

    resolve_targets_mock = create_autospec(
        rd_svc.resolve_dalton_notify_targets,
        return_value=[_CALLER_DISCORD_DM],
    )
    monkeypatch.setattr(rd_svc, "resolve_dalton_notify_targets", resolve_targets_mock)

    patch_prefs_mock = create_autospec(
        rd_svc.apply_dalton_notify_preference_patch,
        return_value={},
    )
    monkeypatch.setattr(rd_svc, "apply_dalton_notify_preference_patch", patch_prefs_mock)

    inv_prefs_mock = create_autospec(rd_svc.invalidate_user_prefs_cache, return_value=None)
    monkeypatch.setattr(rd_svc, "invalidate_user_prefs_cache", inv_prefs_mock)
    inv_prof_mock = create_autospec(rd_svc.invalidate_user_profile_cache, return_value=None)
    monkeypatch.setattr(rd_svc, "invalidate_user_profile_cache", inv_prof_mock)

    class _FakeTx:
        async def __aenter__(self) -> "_FakeTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

    @asynccontextmanager
    async def _fake_get_conn():
        yield _FakeConn()

    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_get_conn)

    async def _fake_update_auto(
        aid: str,
        uid: str,
        *,
        conn: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        updated = dict(row)
        updated["metadata"] = kwargs.get("metadata", row.get("metadata"))
        return updated

    update_auto_mock = create_autospec(
        rd_svc.auto_db.update_automation,
        side_effect=_fake_update_auto,
    )
    monkeypatch.setattr(rd_svc.auto_db, "update_automation", update_auto_mock)

    notify_admins_mock = create_autospec(
        rd_svc.notify_admins_group_target_change,
        return_value={"notified": True, "targets": ["feishu:user:ou_admin"]},
    )
    monkeypatch.setattr(rd_svc, "notify_admins_group_target_change", notify_admins_mock)

    master_mock = create_autospec(rd_svc.resolve_dalton_notify_enabled, return_value=master_enabled)
    monkeypatch.setattr(rd_svc, "resolve_dalton_notify_enabled", master_mock)

    eval_mock = create_autospec(
        rd_svc.evaluate_user_notify_eligibility,
        return_value=(master_enabled, None if master_enabled else "master_switch_off", []),
    )
    monkeypatch.setattr(rd_svc, "evaluate_user_notify_eligibility", eval_mock)

    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(tmp_path / "var_dalton"))

    return {
        "get_tier": get_tier_mock,
        "get_auto": get_auto_mock,
        "verify_member": verify_member_mock,
        "sync_chats": sync_chats_mock,
        "patch_prefs": patch_prefs_mock,
        "update_auto": update_auto_mock,
        "notify_admins": notify_admins_mock,
        "master_enabled": master_mock,
        "eval_eligibility": eval_mock,
    }


def test_tool_mounted_only_for_internal_and_admin() -> None:
    for role in ("analyst", "chief_of_staff"):
        assert "dalton_report_delivery" not in {
            t.name for t in build_dalton_tools(role, None, user_tier="external")
        }
        for tier in ("internal", "admin", None):
            assert "dalton_report_delivery" in {
                t.name for t in build_dalton_tools(role, None, user_tier=tier)
            }


def test_prompt_renders_report_delivery_guidance_only_when_enabled() -> None:
    loader = get_loader()
    rendered_off = loader.render("components/dalton_deliverable.md.j2", dalton_report_delivery_enabled=False)
    rendered_on = loader.render("components/dalton_deliverable.md.j2", dalton_report_delivery_enabled=True)
    assert "dalton_report_delivery" not in rendered_off
    assert "dalton_report_delivery" in rendered_on
    assert "user_confirmed=true" in rendered_on


@pytest.mark.asyncio
async def test_internal_set_authorized_group_member_succeeds_archives_and_notifies_admin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal", is_feishu_member=True)
    tool = create_report_delivery_tools()[0]

    raw = await tool.ainvoke(
        {
            "action": "set",
            "automation_id": _AUTO_UUID,
            "report_targets": [_FEISHU_CHAT_AUTH],
            "user_confirmed": True,
        },
        config=_cfg(_OWNER),
    )
    res = json.loads(raw)

    assert res["ok"] is True
    assert res["report_profile"] == {
        "profile": "daily-filing-alert",
        "report_targets": [_FEISHU_CHAT_AUTH],
        "failure_targets": [_CALLER_DISCORD_DM],
    }
    mocks["verify_member"].assert_awaited_once()
    mocks["sync_chats"].assert_awaited_once()
    mocks["patch_prefs"].assert_awaited_once()
    patch_arg = mocks["patch_prefs"].await_args.args[1]
    assert patch_arg[rd_svc.DALTON_NOTIFY_OVERRIDES_KEY][f"automation:{_AUTO_UUID}"] is True
    assert set(patch_arg[rd_svc.DALTON_NOTIFY_ROUTES_KEY][f"automation:{_AUTO_UUID}"]) == {
        _FEISHU_CHAT_AUTH,
        _CALLER_DISCORD_DM,
    }
    mocks["update_auto"].assert_awaited_once()
    assert mocks["update_auto"].await_args.kwargs["actor_user_id"] == _OWNER
    mocks["notify_admins"].assert_awaited_once()
    assert "archive_path" not in res
    assert not (tmp_path / "var_dalton").exists()


@pytest.mark.asyncio
async def test_internal_rejects_unauthorized_group_with_exact_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal")
    tool = create_report_delivery_tools()[0]

    raw = await tool.ainvoke(
        {
            "action": "set",
            "automation_id": _AUTO_UUID,
            "report_targets": [_FEISHU_CHAT_UNAUTH],
            "user_confirmed": True,
        },
        config=_cfg(_OWNER),
    )
    res = json.loads(raw)

    assert res["ok"] is False
    assert res["message"] == rd_svc.UNAUTHORIZED_GROUP_MESSAGE
    mocks["update_auto"].assert_not_awaited()
    mocks["notify_admins"].assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_rejects_non_member_of_authorized_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mocks = _patch_common_set_deps(
        monkeypatch, tmp_path=tmp_path, tier="internal", is_feishu_member=False
    )
    tool = create_report_delivery_tools()[0]

    raw = await tool.ainvoke(
        {
            "action": "set",
            "automation_id": _AUTO_UUID,
            "report_targets": [_FEISHU_CHAT_AUTH],
            "user_confirmed": True,
        },
        config=_cfg(_OWNER),
    )
    res = json.loads(raw)

    assert res["ok"] is False
    assert res["error"] == "not_group_member"
    mocks["update_auto"].assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_rejects_discord_channel_admin_allows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal")
    tool = create_report_delivery_tools()[0]

    raw_internal = await tool.ainvoke(
        {
            "action": "set",
            "automation_id": _AUTO_UUID,
            "report_targets": [],
            "text_targets": [_DISCORD_CHAN_AUTH],
            "user_confirmed": True,
        },
        config=_cfg(_OWNER),
    )
    res_internal = json.loads(raw_internal)
    assert res_internal["ok"] is False
    assert res_internal["error"] == "admin_only_channel"

    mocks["get_tier"].return_value = "admin"
    raw_admin = await tool.ainvoke(
        {
            "action": "set",
            "automation_id": _AUTO_UUID,
            "report_targets": [],
            "text_targets": [_DISCORD_CHAN_AUTH],
            "user_confirmed": True,
        },
        config=_cfg(_OWNER),
    )
    res_admin = json.loads(raw_admin)
    assert res_admin["ok"] is True


@pytest.mark.asyncio
async def test_rejects_foreign_task_external_tier_and_unconfirmed_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal")
    tool = create_report_delivery_tools()[0]

    # Foreign task rejected for internal user
    res_foreign = json.loads(
        await tool.ainvoke(
            {
                "action": "set",
                "automation_id": _AUTO_UUID,
                "report_targets": [_CALLER_FEISHU_DM],
            },
            config=_cfg(_OTHER_USER),
        )
    )
    assert res_foreign["ok"] is False
    assert res_foreign["error"] == "forbidden_or_not_found"

    # Unconfirmed group rejected
    res_unconfirmed = json.loads(
        await tool.ainvoke(
            {
                "action": "set",
                "automation_id": _AUTO_UUID,
                "report_targets": [_FEISHU_CHAT_AUTH],
                "user_confirmed": False,
            },
            config=_cfg(_OWNER),
        )
    )
    assert res_unconfirmed["ok"] is False
    assert res_unconfirmed["error"] == "confirmation_required"

    # External member rejected at runtime even if invoked directly
    mocks["get_tier"].return_value = "external"
    res_ext = json.loads(
        await tool.ainvoke(
            {"action": "get", "automation_id": _AUTO_UUID},
            config=_cfg(_EXT_USER),
        )
    )
    assert res_ext["ok"] is False
    assert res_ext["error"] == "forbidden"
    mocks["update_auto"].assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_targets_require_no_confirmation_and_skip_admin_notify(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal")
    tool = create_report_delivery_tools()[0]

    res = json.loads(
        await tool.ainvoke(
            {
                "action": "set",
                "automation_id": _AUTO_UUID,
                "report_targets": [_CALLER_FEISHU_DM],
                "user_confirmed": False,
            },
            config=_cfg(_OWNER),
        )
    )
    assert res["ok"] is True
    assert res["report_profile"]["report_targets"] == [_CALLER_FEISHU_DM]
    assert not hasattr(rd_svc, "add_dalton_personal_notify_target")
    mocks["sync_chats"].assert_not_awaited()
    mocks["notify_admins"].assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_archives_clears_profile_and_notifies_admin_when_groups_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    existing_row = _make_automation_row(
        metadata={
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_FEISHU_CHAT_AUTH],
                "failure_targets": [_CALLER_DISCORD_DM],
            },
            "report_profile_missing_notified_at": "2026-11-16T00:00:00+00:00",
            "report_profile_missing_notice": {"status": "sent", "attempts": 1},
            "other_key": "keep_me",
        }
    )
    mocks = _patch_common_set_deps(
        monkeypatch, tmp_path=tmp_path, tier="internal", auto_row=existing_row
    )
    tool = create_report_delivery_tools()[0]

    res = json.loads(
        await tool.ainvoke(
            {"action": "clear", "automation_id": _AUTO_UUID},
            config=_cfg(_OWNER),
        )
    )
    assert res["ok"] is True
    assert res["cleared"] is True
    assert mocks["update_auto"].await_args.kwargs["metadata"] == {"other_key": "keep_me"}
    patch_arg = mocks["patch_prefs"].await_args.args[1]
    assert patch_arg[rd_svc.DALTON_NOTIFY_OVERRIDES_KEY][f"automation:{_AUTO_UUID}"] is None
    assert patch_arg[rd_svc.DALTON_NOTIFY_ROUTES_KEY][f"automation:{_AUTO_UUID}"] is None
    mocks["notify_admins"].assert_awaited_once()
    assert "archive_path" not in res
    assert not (tmp_path / "var_dalton").exists()


@pytest.mark.asyncio
async def test_schedule_helper_and_upstream_create_warn_on_missing_report_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.autonomy import tracking as tracking_mod

    @asynccontextmanager
    async def _fake_conn():
        yield object()

    @asynccontextmanager
    async def _fake_lock(*args: Any, **kwargs: Any):
        yield object()

    monkeypatch.setattr(sched_mod, "get_db_connection", _fake_conn)
    monkeypatch.setattr(sched_mod, "user_automations_lock", _fake_lock)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": _WS_UUID, "user_id": _OWNER, "is_home": False},
        ),
    )
    monkeypatch.setattr(
        tracking_mod,
        "_resolve_workspace_ticker",
        create_autospec(tracking_mod._resolve_workspace_ticker, return_value="MSFT"),
    )
    monkeypatch.setattr(
        sched_mod, "list_all_user_automations", create_autospec(sched_mod.list_all_user_automations, return_value=[])
    )
    from src.dalton.database import company_automations as company_auto_db

    monkeypatch.setattr(
        company_auto_db,
        "resolve_automation_scope_for_write",
        create_autospec(company_auto_db.resolve_automation_scope_for_write, return_value="personal"),
    )
    created_row = {
        "automation_id": uuid4(),
        "name": "dalton-msft-filing-daily",
        "instruction": _DAILY_FILING_INST,
        "metadata": {},
    }
    from src.server.database import automation as server_auto_db

    monkeypatch.setattr(
        server_auto_db,
        "create_automation",
        create_autospec(server_auto_db.create_automation, return_value=created_row),
    )

    out = await sched_mod.schedule_dalton_automation_helper(
        "dalton-msft-filing-daily",
        {
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "instruction": _DAILY_FILING_INST,
        },
        user_id=_OWNER,
        workspace_id=_WS_UUID,
        caller_workspace_id=_WS_UUID,
        role="analyst",
    )
    assert out["ok"] is True
    assert out["warning"] == rd_svc.MISSING_REPORT_PROFILE_WARNING

    # Upstream POST /api/v1/automations hook
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    api_row = {
        "automation_id": UUID(_AUTO_UUID),
        "user_id": _OWNER,
        "name": "API Filing Daily",
        "description": None,
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": None,
        "last_run_at": None,
        "instruction": _DAILY_FILING_INST,
        "workspace_id": UUID(_WS_UUID),
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
        "last_execution": None,
        "scope": "personal",
    }
    monkeypatch.setattr(
        automations_app.cli_approval,
        "automation_runs",
        create_autospec(automations_app.cli_approval.automation_runs, return_value=None),
    )
    monkeypatch.setattr(
        automations_app.handler,
        "create_automation",
        create_autospec(automations_app.handler.create_automation, return_value=api_row),
    )
    from src.dalton.notify import dispatch as notify_dispatch

    monkeypatch.setattr(
        notify_dispatch,
        "populate_automation_notify_state",
        create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None),
    )
    principal = MagicMock()
    principal.user_id = _OWNER
    req = AutomationCreate(
        name="API Filing Daily",
        trigger_type="cron",
        cron_expression="0 8 * * *",
        instruction=_DAILY_FILING_INST,
        workspace_id=UUID(_WS_UUID),
    )
    http_req = Request({"type": "http", "headers": [], "app": MagicMock()})
    resp = await automations_app.create_automation(req, principal, http_req)
    assert resp.report_delivery_warning == rd_svc.MISSING_REPORT_PROFILE_WARNING


@pytest.mark.asyncio
async def test_first_missing_profile_rejection_sends_owner_dm_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed_sqls: list[tuple[str, Any]] = []
    current_row = {
        "automation_execution_id": _EXEC_UUID,
        "automation_id": _AUTO_UUID,
        "status": "running",
        "conversation_thread_id": "t-1",
        "conversation_response_id": "r-1",
        "user_id": _OWNER,
        "workspace_id": _WS_UUID,
        "name": "Chao Filing Daily",
        "metadata": {},
        "delivery_config": None,
    }

    class _FakeCur:
        async def __aenter__(self) -> "_FakeCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            executed_sqls.append((sql, params))

    class _FakeTx:
        async def __aenter__(self) -> "_FakeTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        def cursor(self, **kwargs: Any) -> _FakeCur:
            return _FakeCur()

    @asynccontextmanager
    async def _fake_get_conn(conn: Any = None):
        yield _FakeConn()

    monkeypatch.setattr(reports_db, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(
        reports_db,
        "lock_report_execution",
        create_autospec(reports_db.lock_report_execution, side_effect=lambda cur, eid, **kw: dict(current_row)),
    )
    monkeypatch.setattr(
        rd_svc,
        "resolve_dalton_notify_targets",
        create_autospec(rd_svc.resolve_dalton_notify_targets, return_value=[_CALLER_FEISHU_DM]),
    )
    send_mock = create_autospec(
        rd_svc.send_dalton_gateway_notice,
        return_value={"ok": True, "sent_targets": [_CALLER_FEISHU_DM], "failed_targets": [], "errors": {}},
    )
    monkeypatch.setattr(rd_svc, "send_dalton_gateway_notice", send_mock)

    base_args = {
        "user_id": _OWNER,
        "workspace_id": _WS_UUID,
        "thread_id": "t-1",
        "run_id": "r-1",
        "automation_id": _AUTO_UUID,
        "execution_id": _EXEC_UUID,
        "report_run_id": "run-1",
        "material": "yes",
        "cards": [{"title": "Daily"}],
    }

    first = await reports_db.submit_automation_report(**base_args)
    assert first["code"] == "profile_invalid"
    assert any("report_profile_missing_notice" in sql for sql, _ in executed_sqls)
    pending_writes = [json.loads(p[0]) for sql, p in executed_sqls if "report_profile_missing_notice" in sql and p]
    assert pending_writes[0]["status"] == "pending"
    assert pending_writes[0]["attempts"] == 1
    assert pending_writes[-1]["status"] == "sent"
    assert pending_writes[-1]["attempts"] == 1
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs["targets"] == [_CALLER_FEISHU_DM]
    assert send_mock.await_args.kwargs["source"] == "report_delivery_missing_profile_notice"

    # Simulate persisted sent status on subsequent run -> must NOT notify a second time.
    executed_sqls.clear()
    send_mock.reset_mock()
    current_row["metadata"] = {
        "report_profile_missing_notice": {
            "status": "sent",
            "attempts": 1,
            "last_attempt_at": "2026-11-17T00:00:00+00:00",
            "sent_at": "2026-11-17T00:00:00+00:00",
        }
    }

    second = await reports_db.submit_automation_report(**{**base_args, "report_run_id": "run-2"})
    assert second["code"] == "profile_invalid"
    assert not any("UPDATE automations" in sql for sql, _ in executed_sqls)
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_diagnostics_profile_inference_and_feishu_member_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    row = _make_automation_row(
        instruction="按 skills/dalton-market-digest/SKILL.md 运行",
        metadata={
            "report_profile": {
                "profile": "market-digest-am",
                "report_targets": [_FEISHU_CHAT_AUTH],
                "failure_targets": [],
            }
        },
    )
    _patch_common_set_deps(
        monkeypatch,
        tmp_path=tmp_path,
        tier="internal",
        auto_row=row,
        is_feishu_member=True,
        master_enabled=False,
    )
    monkeypatch.setattr(
        rd_svc,
        "resolve_dalton_notify_overrides",
        create_autospec(rd_svc.resolve_dalton_notify_overrides, return_value={}),
    )
    monkeypatch.setattr(
        rd_svc,
        "resolve_dalton_notify_routes",
        create_autospec(rd_svc.resolve_dalton_notify_routes, return_value={}),
    )

    tool = create_report_delivery_tools()[0]
    got = json.loads(
        await tool.ainvoke({"action": "get", "automation_id": _AUTO_UUID}, config=_cfg(_OWNER))
    )
    assert got["ok"] is True
    assert got["automation"]["notify_state"]["master_switch_enabled"] is False
    assert got["automation"]["notify_state"]["blocked_by_switch_or_route"] is True
    assert "master_switch_off" in got["automation"]["notify_state"]["blocked_reasons"]
    groups = {g["target"]: g for g in got["available_group_targets"]}
    assert groups[_FEISHU_CHAT_AUTH]["selectable"] is True
    assert groups[_DISCORD_CHAN_AUTH]["selectable"] is False
    assert groups[_DISCORD_CHAN_AUTH]["membership_status"] == "admin_only_channel"

    # Ambiguous market-digest without explicit profile requires profile; mismatched profile rejected
    res_req = json.loads(
        await tool.ainvoke(
            {
                "action": "set",
                "automation_id": _AUTO_UUID,
                "report_targets": [_CALLER_FEISHU_DM],
            },
            config=_cfg(_OWNER),
        )
    )
    assert res_req["ok"] is False
    assert res_req["error"] == "profile_required"

    res_mismatch = json.loads(
        await tool.ainvoke(
            {
                "action": "set",
                "automation_id": _AUTO_UUID,
                "profile": "daily-filing-alert",
                "report_targets": [_CALLER_FEISHU_DM],
            },
            config=_cfg(_OWNER),
        )
    )
    assert res_mismatch["ok"] is False
    assert res_mismatch["error"] == "profile_skill_mismatch"

    # Real verify_feishu_chat_member paginated HTTP check using create_autospec
    import httpx

    fake_creds = create_autospec(
        rd_svc.load_dedicated_feishu_vault_credentials,
        return_value={"DALTON_FEISHU_APP_ID": "cli_1", "DALTON_FEISHU_APP_SECRET": "sec_1"},
    )
    client_mock = create_autospec(httpx.AsyncClient, instance=True)
    client_mock.__aenter__.return_value = client_mock
    client_mock.__aexit__.return_value = False

    tok_resp = create_autospec(httpx.Response, instance=True)
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t-tok"}

    p1 = create_autospec(httpx.Response, instance=True)
    p1.status_code = 200
    p1.json.return_value = {
        "code": 0,
        "data": {"has_more": True, "page_token": "tok2", "items": [{"member_id": "ou_other"}]},
    }
    p2 = create_autospec(httpx.Response, instance=True)
    p2.status_code = 200
    p2.json.return_value = {
        "code": 0,
        "data": {"has_more": False, "items": [{"member_id": "ou_chao_open_1"}]},
    }
    client_mock.post.return_value = tok_resp
    client_mock.get.side_effect = [p1, p2]

    ok, err = await rd_svc.verify_feishu_chat_member(
        "oc_group_auth_1",
        "ou_chao_open_1",
        get_credentials_fn=fake_creds,
        http_client_factory=lambda **kw: client_mock,
    )
    assert ok is True and err is None


@pytest.mark.asyncio
async def test_unified_validation_blocks_rest_groups_and_prefs_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from fastapi import HTTPException
    from pydantic import ValidationError
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.server.models.automation import AutomationUpdate

    # Pydantic rejects malformed report_profile in AutomationCreate / AutomationUpdate
    with pytest.raises(ValidationError):
        AutomationCreate(
            name="Bad",
            trigger_type="cron",
            cron_expression="0 8 * * *",
            instruction=_DAILY_FILING_INST,
            metadata={"report_profile": {"profile": "nonexistent-profile"}},
        )
    with pytest.raises(ValidationError):
        AutomationUpdate(metadata={"report_profile": []})

    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal", is_feishu_member=False)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    existing_row = {
        "automation_id": UUID(_AUTO_UUID),
        "user_id": _OWNER,
        "name": "Chao Filing Daily",
        "description": None,
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": None,
        "last_run_at": None,
        "instruction": _DAILY_FILING_INST,
        "workspace_id": UUID(_WS_UUID),
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
        "last_execution": None,
        "scope": "personal",
    }
    monkeypatch.setattr(
        automations_app.cli_approval,
        "automation_runs",
        create_autospec(automations_app.cli_approval.automation_runs, return_value=None),
    )
    mocks["get_auto"].side_effect = None
    mocks["get_auto"].return_value = dict(existing_row)
    handler_update = create_autospec(automations_app.handler.update_automation, return_value=dict(existing_row))
    monkeypatch.setattr(automations_app.handler, "update_automation", handler_update)
    from src.dalton.notify import dispatch as notify_dispatch

    monkeypatch.setattr(
        notify_dispatch,
        "populate_automation_notify_state",
        create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None),
    )

    principal = MagicMock()
    principal.user_id = _OWNER
    http_req = Request({"type": "http", "headers": [], "app": MagicMock()})

    # Non-admin REST PATCH with a group target is always rejected ("请在对话里让 Dalton 设置")
    with pytest.raises(HTTPException) as exc_rest_grp:
        await automations_app.update_automation(
            _AUTO_UUID,
            principal,
            http_req,
            body={
                "metadata": {
                    "report_profile": {
                        "profile": "daily-filing-alert",
                        "report_targets": [_FEISHU_CHAT_AUTH],
                        "failure_targets": [],
                    }
                }
            },
        )
    assert exc_rest_grp.value.status_code == 403
    assert "请在对话里让 Dalton 设置" in str(exc_rest_grp.value.detail)
    handler_update.assert_not_awaited()

    # Non-admin REST PATCH with unbound DM target is rejected
    with pytest.raises(HTTPException) as exc_rest_dm:
        await automations_app.update_automation(
            _AUTO_UUID,
            principal,
            http_req,
            body={
                "metadata": {
                    "report_profile": {
                        "profile": "daily-filing-alert",
                        "report_targets": ["feishu:user:ou_stranger"],
                        "failure_targets": [],
                    }
                }
            },
        )
    assert exc_rest_dm.value.status_code == 403
    handler_update.assert_not_awaited()

    # Non-admin REST PATCH with bound DM target succeeds and warns nothing
    updated_dm_row = {
        **existing_row,
        "metadata": {
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_CALLER_FEISHU_DM],
                "failure_targets": [],
            }
        },
    }
    handler_update.return_value = updated_dm_row
    ok_dm_resp = await automations_app.update_automation(
        _AUTO_UUID,
        principal,
        http_req,
        body={"metadata": updated_dm_row["metadata"]},
    )
    assert ok_dm_resp.report_delivery_warning is None
    handler_update.assert_awaited_once()

    # Admin REST PATCH with authorized group target succeeds, archives, and notifies admins
    mocks["get_tier"].return_value = "admin"
    handler_update.reset_mock()
    updated_grp_row = {
        **existing_row,
        "metadata": {
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_FEISHU_CHAT_AUTH],
                "failure_targets": [],
            }
        },
    }
    handler_update.return_value = updated_grp_row
    admin_resp = await automations_app.update_automation(
        _AUTO_UUID,
        principal,
        http_req,
        body={"metadata": updated_grp_row["metadata"]},
    )
    assert admin_resp.report_delivery_warning is None
    mocks["notify_admins"].assert_awaited_once()

    # Preferences route write for automation:<uuid> on a report automation via REST is blocked for non-admin
    mocks["get_tier"].return_value = "internal"
    with pytest.raises(HTTPException) as exc_pref_grp:
        await model_prefs_mod._validate_report_automation_route_entries(
            _OWNER,
            {f"automation:{_AUTO_UUID}": [_FEISHU_CHAT_AUTH]},
            {},
            via_rest=True,
            user_confirmed=False,
        )
    assert exc_pref_grp.value.status_code == 403
    assert "请在对话里让 Dalton 设置" in str(exc_pref_grp.value.detail)


@pytest.mark.asyncio
async def test_set_and_clear_atomicity_on_version_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.services.company_automations import AutomationVersionConflictError

    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal", is_feishu_member=True)
    # Track uncommitted vs committed preference & settings writes across transaction rollback
    committed_state: dict[str, Any] = {"prefs": None, "settings": None}
    staged_state: dict[str, Any] = {}

    class _TxRollback:
        async def __aenter__(self) -> "_TxRollback":
            staged_state["prefs"] = committed_state["prefs"]
            staged_state["settings"] = committed_state["settings"]
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            if exc_type is not None:
                staged_state.clear()
                return False
            committed_state["prefs"] = staged_state.get("prefs")
            committed_state["settings"] = staged_state.get("settings")
            return False

    class _TxConn:
        def transaction(self) -> _TxRollback:
            return _TxRollback()

    @asynccontextmanager
    async def _tx_conn():
        yield _TxConn()

    monkeypatch.setattr(rd_svc, "get_db_connection", _tx_conn)

    async def _stage_prefs(uid: str, patch: dict[str, Any], **kw: Any) -> dict[str, Any]:
        staged_state["prefs"] = patch
        return {}

    async def _stage_settings(uid: str, bindings: Any, **kw: Any) -> None:
        staged_state["settings"] = list(bindings)

    mocks["patch_prefs"].side_effect = _stage_prefs
    mocks["sync_chats"].side_effect = _stage_settings
    mocks["update_auto"].side_effect = AutomationVersionConflictError("stale version")

    with pytest.raises(rd_svc.ReportDeliveryError) as exc_set:
        await rd_svc.set_report_delivery_config(
            caller_user_id=_OWNER,
            automation_id=_AUTO_UUID,
            report_targets=[_FEISHU_CHAT_AUTH],
            user_confirmed=True,
        )
    assert exc_set.value.code == "conflict"
    assert committed_state == {"prefs": None, "settings": None}
    mocks["notify_admins"].assert_not_awaited()
    assert not (tmp_path / "var_dalton").exists()

    # Clear conflict also leaves zero partial changes or archives
    existing = _make_automation_row(
        metadata={
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_FEISHU_CHAT_AUTH],
                "failure_targets": [],
            }
        }
    )
    mocks["get_auto"].side_effect = None
    mocks["get_auto"].return_value = existing
    mocks["update_auto"].side_effect = None
    mocks["update_auto"].return_value = None

    with pytest.raises(rd_svc.ReportDeliveryError) as exc_clear:
        await rd_svc.clear_report_delivery_config(
            caller_user_id=_OWNER,
            automation_id=_AUTO_UUID,
        )
    assert exc_clear.value.code == "conflict"
    assert committed_state == {"prefs": None, "settings": None}
    mocks["notify_admins"].assert_not_awaited()
    assert not (tmp_path / "var_dalton").exists()


@pytest.mark.asyncio
async def test_gateway_notice_failure_truthful_and_no_global_dm_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.notify import dispatch as dispatch_mod

    # 1. Gateway notice checks actual per-target outcome and never reports ok=True on failure
    claim_mock = create_autospec(
        dispatch_mod.claim_target_delivery_with_attempt,
        return_value=dispatch_mod._as_claim_attempt(True, 1, "44444444-4444-4444-4444-444444444444"),
    )
    mark_mock = create_autospec(dispatch_mod.mark_target_delivery, return_value=None)
    post_fail_mock = create_autospec(
        dispatch_mod.post_automation_gateway_notify,
        return_value={
            "ok": False,
            "status": "failed",
            "error_code": "gateway_down",
            "results": [{"target": _CALLER_FEISHU_DM, "ok": False, "error_code": "gateway_down"}],
        },
    )

    fail_res = await dispatch_mod.send_dalton_gateway_notice(
        source="report_delivery_missing_profile_notice",
        user_id=_OWNER,
        targets=[_CALLER_FEISHU_DM],
        title="Test",
        text="Body",
        claim_fn=claim_mock,
        gateway_post_fn=post_fail_mock,
        mark_fn=mark_mock,
    )
    assert fail_res["ok"] is False
    assert fail_res["failed_targets"] == [_CALLER_FEISHU_DM]
    assert fail_res["errors"][_CALLER_FEISHU_DM] == "gateway_down"
    mark_mock.assert_awaited_once()
    assert mark_mock.await_args.kwargs["status"] == "failed"

    # 2. Task DM target stays isolated from global personal targets; classified name obeys automation:<id>
    weekly_auto = {"automation_id": "aaaa1111-1111-1111-1111-111111111111", "name": "dalton-director-weekly", "metadata": {}}
    report_auto = {
        "automation_id": _AUTO_UUID,
        "name": "dalton-msft-move-up",
        "metadata": {
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_CALLER_FEISHU_DM],
                "failure_targets": [],
            }
        },
    }
    routes = {f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM]}
    overrides = {f"automation:{_AUTO_UUID}": True}

    ok_w, _, targets_w = await dispatch_mod.evaluate_user_notify_eligibility(
        _OWNER,
        weekly_auto,
        resolve_enabled_fn=create_autospec(dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
        resolve_overrides_fn=create_autospec(dispatch_mod.resolve_dalton_notify_overrides, return_value=overrides),
        resolve_targets_fn=create_autospec(dispatch_mod.resolve_dalton_notify_targets, return_value=[_CALLER_DISCORD_DM]),
        resolve_routes_fn=create_autospec(dispatch_mod.resolve_dalton_notify_routes, return_value=routes),
        is_admin_fn=create_autospec(dispatch_mod._default_notify_admin, return_value=False),
    )
    assert ok_w is True and targets_w == [_CALLER_DISCORD_DM]

    ok_r, _, targets_r = await dispatch_mod.evaluate_user_notify_eligibility(
        _OWNER,
        report_auto,
        resolve_enabled_fn=create_autospec(dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
        resolve_overrides_fn=create_autospec(dispatch_mod.resolve_dalton_notify_overrides, return_value=overrides),
        resolve_targets_fn=create_autospec(dispatch_mod.resolve_dalton_notify_targets, return_value=[_CALLER_DISCORD_DM]),
        resolve_routes_fn=create_autospec(dispatch_mod.resolve_dalton_notify_routes, return_value=routes),
        is_admin_fn=create_autospec(dispatch_mod._default_notify_admin, return_value=False),
    )
    assert ok_r is True and targets_r == [_CALLER_FEISHU_DM]


@pytest.mark.asyncio
async def test_missing_profile_notice_retries_up_to_3_and_early_rejections_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.database import ai_finance as ai_finance_db
    from src.dalton.database import market_digest as market_digest_db

    # State machine: failed -> retries up to 3, stops at 3; concurrent pending blocks duplicate
    stored_row: dict[str, Any] = {
        "automation_id": _AUTO_UUID,
        "user_id": _OWNER,
        "name": "Filing",
        "metadata": {},
    }

    class _StateCur:
        async def __aenter__(self) -> "_StateCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "UPDATE automations" in sql and params:
                notice = json.loads(params[0])
                stored_row["metadata"] = {"report_profile_missing_notice": notice}

        async def fetchone(self) -> dict[str, Any]:
            return dict(stored_row)

    class _StateTx:
        async def __aenter__(self) -> "_StateTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _StateConn:
        def transaction(self) -> _StateTx:
            return _StateTx()

        def cursor(self, **kwargs: Any) -> _StateCur:
            return _StateCur()

    @asynccontextmanager
    async def _fake_state_conn(conn: Any = None):
        yield _StateConn()

    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_state_conn)
    monkeypatch.setattr(
        rd_svc,
        "resolve_dalton_notify_targets",
        create_autospec(rd_svc.resolve_dalton_notify_targets, return_value=[_CALLER_FEISHU_DM]),
    )
    send_mock = create_autospec(
        rd_svc.send_dalton_gateway_notice,
        side_effect=[
            {"ok": False, "sent_targets": [], "failed_targets": [_CALLER_FEISHU_DM], "errors": {_CALLER_FEISHU_DM: "timeout"}},
            {"ok": True, "sent_targets": [_CALLER_FEISHU_DM], "failed_targets": [], "errors": {}},
        ],
    )
    monkeypatch.setattr(rd_svc, "send_dalton_gateway_notice", send_mock)

    # Attempt 1 fails -> status="failed", attempts=1
    ok1 = await rd_svc.handle_missing_report_profile_rejection(
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Filing",
        execution_id=_EXEC_UUID,
    )
    assert ok1 is False
    assert stored_row["metadata"]["report_profile_missing_notice"]["status"] == "failed"
    assert stored_row["metadata"]["report_profile_missing_notice"]["attempts"] == 1

    # Attempt 2 retries and succeeds -> status="sent", attempts=2
    ok2 = await rd_svc.handle_missing_report_profile_rejection(
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Filing",
        execution_id=_EXEC_UUID,
    )
    assert ok2 is True
    assert stored_row["metadata"]["report_profile_missing_notice"]["status"] == "sent"
    assert stored_row["metadata"]["report_profile_missing_notice"]["attempts"] == 2

    # Attempt 3 skipped because status == "sent"
    ok3 = await rd_svc.handle_missing_report_profile_rejection(
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Filing",
        execution_id=_EXEC_UUID,
    )
    assert ok3 is False
    assert send_mock.await_count == 2

    # Concurrent pending claim blocks second worker; 3rd failure stops further retries
    from datetime import datetime, timezone

    assert rd_svc.evaluate_missing_notice_claim(
        {"report_profile_missing_notice": {"status": "pending", "attempts": 1, "last_attempt_at": datetime.now(timezone.utc).isoformat()}}
    ) == (False, 1)
    assert rd_svc.evaluate_missing_notice_claim(
        {"report_profile_missing_notice": {"status": "failed", "attempts": 3, "last_attempt_at": datetime.now(timezone.utc).isoformat()}}
    ) == (False, 3)

    # Early rejection in ai_finance and market_digest triggers handle_missing_report_profile_rejection
    missing_row = {
        "automation_execution_id": _EXEC_UUID,
        "automation_id": _AUTO_UUID,
        "status": "running",
        "conversation_thread_id": "t-1",
        "conversation_response_id": "r-1",
        "user_id": _OWNER,
        "workspace_id": _WS_UUID,
        "name": "Early Check",
        "metadata": {},
        "delivery_config": None,
    }
    monkeypatch.setattr(ai_finance_db, "get_db_connection", _fake_state_conn)
    monkeypatch.setattr(
        ai_finance_db,
        "lock_report_execution",
        create_autospec(ai_finance_db.lock_report_execution, return_value=dict(missing_row)),
    )
    monkeypatch.setattr(market_digest_db, "get_db_connection", _fake_state_conn)
    monkeypatch.setattr(
        market_digest_db,
        "lock_report_execution",
        create_autospec(market_digest_db.lock_report_execution, return_value=dict(missing_row)),
    )
    handle_mock = create_autospec(rd_svc.handle_missing_report_profile_rejection, return_value=True)
    monkeypatch.setattr(rd_svc, "handle_missing_report_profile_rejection", handle_mock)

    res_ai = await ai_finance_db.authorize_ai_finance_execution(
        user_id=_OWNER,
        workspace_id=_WS_UUID,
        thread_id="t-1",
        run_id="r-1",
        automation_id=_AUTO_UUID,
        execution_id=_EXEC_UUID,
    )
    assert res_ai["ok"] is False and res_ai["code"] == "profile_invalid"

    res_md = await market_digest_db.authorize_market_digest_execution(
        user_id=_OWNER,
        workspace_id=_WS_UUID,
        thread_id="t-1",
        run_id="r-1",
        automation_id=_AUTO_UUID,
        execution_id=_EXEC_UUID,
        period="AM",
    )
    assert res_md["ok"] is False and res_md["code"] == "profile_invalid"
    assert handle_mock.await_count == 2


@pytest.mark.asyncio
async def test_admin_notify_throttles_within_10_minutes_sends_per_target_and_dismiss_has_no_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    # 1. Simplified admin notification: once per admin target, 10-minute throttle, no coalescing/retries
    notice_row: dict[str, Any] | None = None

    class _NoticeCur:
        def __init__(self) -> None:
            self._next_row: Any = None

        async def __aenter__(self) -> "_NoticeCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            nonlocal notice_row
            if "INSERT INTO dalton_report_delivery_admin_notices" in sql:
                if notice_row is None:
                    notice_row = {
                        "automation_id": _AUTO_UUID,
                        "last_notified_at": None,
                    }
            elif "SELECT last_notified_at" in sql:
                self._next_row = dict(notice_row) if notice_row else None
            elif "UPDATE dalton_report_delivery_admin_notices" in sql and params:
                notice_row["last_notified_at"] = datetime.now(timezone.utc)

        async def fetchone(self) -> Any:
            return self._next_row

    class _NoticeTx:
        async def __aenter__(self) -> "_NoticeTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _NoticeConn:
        def transaction(self) -> _NoticeTx:
            return _NoticeTx()

        def cursor(self, **kwargs: Any) -> _NoticeCur:
            return _NoticeCur()

    @asynccontextmanager
    async def _fake_notice_conn(conn: Any = None):
        yield _NoticeConn()

    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_notice_conn)

    async def _fake_gw_send(**kwargs: Any) -> dict[str, Any]:
        tgt = kwargs["targets"][0]
        if tgt == "discord:user:999":
            return {
                "ok": False,
                "sent_targets": [],
                "failed_targets": [tgt],
                "errors": {tgt: "channel_down"},
                "results": [{"target": tgt, "ok": False, "status": "failed", "error_code": "channel_down"}],
            }
        return {
            "ok": True,
            "sent_targets": [tgt],
            "failed_targets": [],
            "errors": {},
            "results": [{"target": tgt, "ok": True, "status": "sent"}],
        }

    gw_send_mock = create_autospec(
        rd_svc.send_dalton_gateway_notice,
        side_effect=_fake_gw_send,
    )

    r1 = await rd_svc.notify_admins_group_target_change(
        actor_user_id=_OWNER,
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Daily",
        action="set",
        previous_groups=[],
        new_groups=[_FEISHU_CHAT_AUTH],
        get_pm_ids_fn=lambda: [_ADMIN],
        resolve_targets_fn=create_autospec(
            rd_svc.resolve_dalton_notify_targets,
            return_value=["feishu:user:ou_admin", "discord:user:999"],
        ),
        gateway_send_fn=gw_send_mock,
    )
    assert r1["notified"] is True
    assert r1["partial_failure"] is True
    assert gw_send_mock.await_count == 2
    assert len(r1["results"]) == 2
    assert r1["results"][0]["target"] == "feishu:user:ou_admin" and r1["results"][0]["ok"] is True
    assert r1["results"][1]["target"] == "discord:user:999" and r1["results"][1]["error_code"] == "channel_down"

    # 2nd change within 10 minutes is throttled (no gateway calls, no coalescing)
    r2 = await rd_svc.notify_admins_group_target_change(
        actor_user_id=_OWNER,
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Daily",
        action="clear",
        previous_groups=[_FEISHU_CHAT_AUTH],
        new_groups=[],
        get_pm_ids_fn=lambda: [_ADMIN],
        resolve_targets_fn=create_autospec(
            rd_svc.resolve_dalton_notify_targets,
            return_value=["feishu:user:ou_admin"],
        ),
        gateway_send_fn=gw_send_mock,
    )
    assert r2["notified"] is False and r2["throttled"] is True
    assert gw_send_mock.await_count == 2

    # 2. dismiss_execution does NOT carry report_delivery_warning
    now = datetime.now(timezone.utc)
    api_row = {
        "automation_id": UUID(_AUTO_UUID),
        "user_id": _OWNER,
        "name": "API Filing Daily",
        "description": None,
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": None,
        "last_run_at": None,
        "instruction": _DAILY_FILING_INST,
        "workspace_id": UUID(_WS_UUID),
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
        "last_execution": None,
        "scope": "personal",
    }
    monkeypatch.setattr(
        automations_app.handler,
        "dismiss_execution",
        create_autospec(automations_app.handler.dismiss_execution, return_value=api_row),
    )
    from src.dalton.notify import dispatch as notify_dispatch

    monkeypatch.setattr(
        notify_dispatch,
        "populate_automation_notify_state",
        create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None),
    )
    dismiss_resp = await automations_app.dismiss_execution(UUID(_AUTO_UUID), UUID(_EXEC_UUID), _OWNER)
    assert dismiss_resp.report_delivery_warning is None


@pytest.mark.asyncio
async def test_feishu_token_cache_timeout_distinction_and_admin_external_owner_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio
    import httpx

    rd_svc.clear_feishu_tenant_token_cache()
    fake_creds = create_autospec(
        rd_svc.load_dedicated_feishu_vault_credentials,
        return_value={"DALTON_FEISHU_APP_ID": "cli_cache", "DALTON_FEISHU_APP_SECRET": "sec_cache"},
    )
    client_mock = create_autospec(httpx.AsyncClient, instance=True)
    client_mock.__aenter__.return_value = client_mock
    client_mock.__aexit__.return_value = False

    tok_resp = create_autospec(httpx.Response, instance=True)
    tok_resp.status_code = 200
    tok_resp.json.return_value = {"code": 0, "tenant_access_token": "t-cached", "expire": 7200}

    mem_resp = create_autospec(httpx.Response, instance=True)
    mem_resp.status_code = 200
    mem_resp.json.return_value = {"code": 0, "data": {"has_more": False, "items": [{"member_id": "ou_chao_open_1"}]}}
    client_mock.post.return_value = tok_resp
    client_mock.get.return_value = mem_resp

    # Two calls with token cache enabled -> only 1 token POST!
    ok1, _ = await rd_svc.verify_feishu_chat_member(
        "oc_group_auth_1",
        "ou_chao_open_1",
        get_credentials_fn=fake_creds,
        http_client_factory=lambda **kw: client_mock,
        use_token_cache=True,
    )
    ok2, _ = await rd_svc.verify_feishu_chat_member(
        "oc_group_auth_2",
        "ou_chao_open_1",
        get_credentials_fn=fake_creds,
        http_client_factory=lambda **kw: client_mock,
        use_token_cache=True,
    )
    assert ok1 is True and ok2 is True
    assert client_mock.post.await_count == 1
    rd_svc.clear_feishu_tenant_token_cache()

    # get(automation_id=None) skips Feishu membership calls; total timeout marks check_timeout
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal")
    monkeypatch.setattr(rd_svc.auto_db, "list_all_automations", create_autospec(rd_svc.auto_db.list_all_automations, return_value=[]))
    list_res = await rd_svc.get_report_delivery_config(caller_user_id=_OWNER, automation_id=None)
    assert list_res["available_group_targets"][0]["membership_status"] == "unchecked_in_list"
    mocks["verify_member"].assert_not_awaited()

    async def _slow_verify(cid: str, oid: str) -> tuple[bool, str | None]:
        await asyncio.sleep(0.2)
        return True, None

    monkeypatch.setattr(rd_svc, "resolve_dalton_notify_overrides", create_autospec(rd_svc.resolve_dalton_notify_overrides, return_value={}))
    monkeypatch.setattr(rd_svc, "resolve_dalton_notify_routes", create_autospec(rd_svc.resolve_dalton_notify_routes, return_value={}))
    timeout_res = await rd_svc.get_report_delivery_config(
        caller_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        verify_member_fn=_slow_verify,
        feishu_total_timeout_s=0.01,
    )
    assert timeout_res["available_group_targets"][0]["membership_status"] == "check_timeout"
    assert timeout_res["available_group_targets"][0]["selectable"] is False

    # Admin editing external owner's task with a group target is rejected cleanly with owner_tier_forbidden
    async def _tier_by_uid(uid: str) -> str:
        return "admin" if uid == _ADMIN else "external"

    mocks["get_tier"].side_effect = _tier_by_uid
    ext_auto = _make_automation_row(user_id=_EXT_USER)
    with pytest.raises(rd_svc.ReportDeliveryError) as exc_ext_owner:
        await rd_svc.validate_report_profile_change(
            _ADMIN,
            ext_auto,
            {
                "profile": "daily-filing-alert",
                "report_targets": [_FEISHU_CHAT_AUTH],
                "failure_targets": [],
            },
            via_rest=False,
            user_confirmed=True,
        )
    assert exc_ext_owner.value.code == "owner_tier_forbidden"


@pytest.mark.asyncio
async def test_r3_1_rest_all_effective_route_keys_reject_groups_and_tool_uses_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    import src.dalton.access_tiers as access_tiers_mod
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.dalton.tools import notify_tools as notify_tools_mod

    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )

    async def _no_op_lock(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_in_lock must not execute when REST group target is rejected")

    async def _empty_unlocked() -> dict[str, Any]:
        return {}

    # Non-admin via REST writing name key, category key, default key, or UUID key with group/channel is rejected 403
    for route_key, tgt in (
        ("dalton-msft-move-up", _FEISHU_CHAT_AUTH),
        ("Chao Daily Filing", _FEISHU_CHAT_AUTH),
        ("category:price_move", _FEISHU_CHAT_AUTH),
        ("price_move", _DISCORD_CHAN_AUTH),
        ("default", _FEISHU_CHAT_AUTH),
        (f"automation:{_AUTO_UUID}", _DISCORD_CHAN_AUTH),
    ):
        with pytest.raises(HTTPException) as exc:
            await model_prefs_mod.run_with_feishu_group_prefetch(
                user_id=_OWNER,
                submitted_routes={route_key: [tgt]},
                read_unlocked_routes=create_autospec(_empty_unlocked, return_value={}),
                run_in_lock=_no_op_lock,
                via_rest=True,
            )
        assert exc.value.status_code == 403
        assert "请在对话里让 Dalton 设置" in str(exc.value.detail)

        with pytest.raises(HTTPException) as exc_lock:
            await model_prefs_mod.validate_dalton_notify_routes_for_user(
                _OWNER,
                {route_key: [tgt]},
                {},
                via_rest=True,
            )
        assert exc_lock.value.status_code == 403

    # Tool route key helper forces automation:<uuid> for classified report tasks
    classified_report = {
        "automation_id": _AUTO_UUID,
        "name": "dalton-msft-move-up",
        "instruction": _DAILY_FILING_INST,
        "metadata": {},
    }
    assert notify_tools_mod._automation_route_key(classified_report) == f"automation:{_AUTO_UUID}"


@pytest.mark.asyncio
async def test_r3_2_task_dm_targets_bypass_global_list_and_survive_global_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.llms.preferences import TuningError
    from src.server.database import pool as pool_mod
    from src.server.database import user as user_db_mod

    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_targets": [_CALLER_DISCORD_DM],
            "dalton_notify_routes": {
                f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM],
                "data:cn-leverage-daily": [_CALLER_FEISHU_DM, _FEISHU_CHAT_AUTH],
            },
        },
        "other_preference": {},
    }

    async def _fake_lock_prefs(uid: str, *, conn: Any = None) -> dict[str, Any]:
        return json.loads(json.dumps(stored_prefs))

    async def _fake_upsert_prefs(
        user_id: str,
        *,
        model_preference: dict[str, Any] | None = None,
        conn: Any = None,
        **kw: Any,
    ) -> dict[str, Any]:
        mp = dict(stored_prefs["model_preference"])
        for k, v in (model_preference or {}).items():
            if v is None:
                mp.pop(k, None)
            elif isinstance(v, dict) and isinstance(mp.get(k), dict):
                merged = dict(mp[k])
                for sk, sv in v.items():
                    if sv is None:
                        merged.pop(sk, None)
                    else:
                        merged[sk] = sv
                mp[k] = merged
            else:
                mp[k] = v
        stored_prefs["model_preference"] = mp
        return {"user_id": user_id, "model_preference": mp, "other_preference": {}}

    class _FakeCur:
        async def __aenter__(self) -> "_FakeCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            return None

        async def fetchall(self) -> list[dict[str, Any]]:
            return [
                {"platform": "feishu", "platform_user_id": "ou_chao_open_1"},
                {"platform": "discord", "platform_user_id": "424242424242424242"},
            ]

    class _FakeTx:
        async def __aenter__(self) -> "_FakeTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _FakeConn:
        def transaction(self) -> _FakeTx:
            return _FakeTx()

        def cursor(self, **kwargs: Any) -> _FakeCur:
            return _FakeCur()

    @asynccontextmanager
    async def _fake_conn():
        yield _FakeConn()

    monkeypatch.setattr(pool_mod, "get_db_connection", _fake_conn)
    monkeypatch.setattr(user_db_mod, "get_user_preferences", create_autospec(user_db_mod.get_user_preferences, side_effect=_fake_lock_prefs))
    monkeypatch.setattr(user_db_mod, "lock_user_preferences", create_autospec(user_db_mod.lock_user_preferences, side_effect=_fake_lock_prefs))
    monkeypatch.setattr(user_db_mod, "upsert_user_preferences", create_autospec(user_db_mod.upsert_user_preferences, side_effect=_fake_upsert_prefs))
    monkeypatch.setattr(user_db_mod, "invalidate_user_prefs_cache", create_autospec(user_db_mod.invalidate_user_prefs_cache, return_value=None))
    monkeypatch.setattr(user_db_mod, "invalidate_user_profile_cache", create_autospec(user_db_mod.invalidate_user_profile_cache, return_value=None))
    monkeypatch.setattr(model_prefs_mod, "get_user_preferences", create_autospec(model_prefs_mod.get_user_preferences, side_effect=_fake_lock_prefs))
    monkeypatch.setattr(model_prefs_mod, "_validate_report_automation_route_entries", create_autospec(model_prefs_mod._validate_report_automation_route_entries, return_value=[]))

    # 1. Task route using bound Feishu DM (not in global Discord-only list) succeeds via real apply_dalton_notify_preference_patch
    await model_prefs_mod.apply_dalton_notify_preference_patch(
        _OWNER,
        {
            model_prefs_mod.DALTON_NOTIFY_ROUTES_KEY: {
                f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM],
            }
        },
    )
    assert stored_prefs["model_preference"]["dalton_notify_routes"][f"automation:{_AUTO_UUID}"] == [_CALLER_FEISHU_DM]

    # 2. Unbound personal DM still raises TuningError
    with pytest.raises(TuningError):
        await model_prefs_mod.apply_dalton_notify_preference_patch(
            _OWNER,
            {
                model_prefs_mod.DALTON_NOTIFY_ROUTES_KEY: {
                    f"automation:{_AUTO_UUID}": ["feishu:user:ou_unbound_stranger"],
                }
            },
        )

    # 3. Saving global dalton_notify_targets (removing Feishu DM / emptying global list) preserves automation:<uuid> DM route
    #    while still pruning non-automation routes (like data:cn-leverage-daily)
    await model_prefs_mod.apply_dalton_notify_preference_patch(
        _OWNER,
        {model_prefs_mod.DALTON_NOTIFY_TARGETS_KEY: [_CALLER_DISCORD_DM]},
    )
    assert stored_prefs["model_preference"]["dalton_notify_routes"][f"automation:{_AUTO_UUID}"] == [_CALLER_FEISHU_DM]
    assert stored_prefs["model_preference"]["dalton_notify_routes"]["data:cn-leverage-daily"] == [_FEISHU_CHAT_AUTH]


@pytest.mark.asyncio
async def test_r3_3_5_9_db_audit_history_honest_route_update_and_schedule_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.autonomy import tracking as tracking_mod
    from src.dalton.database import company_automations as company_auto_db
    from src.dalton.services.company_automations import automation_history_context
    from src.server.database import automation as server_auto_db

    # 1. Honest route_update audit: metadata_unchanged=True, note="路由变更、metadata 未变", NO metadata_after
    auto_row = _make_automation_row(
        metadata={"existing": "keep", "report_profile": {"profile": "daily-filing-alert", "report_targets": [_CALLER_FEISHU_DM], "failure_targets": []}}
    )
    rd_tier_mock = create_autospec(rd_svc.get_user_tier, return_value="internal")
    monkeypatch.setattr(rd_svc, "get_user_tier", rd_tier_mock)
    monkeypatch.setattr(rd_svc, "list_caller_dm_identities", create_autospec(rd_svc.list_caller_dm_identities, return_value=_caller_identities()))
    monkeypatch.setattr(rd_svc, "list_authorized_group_bindings", create_autospec(rd_svc.list_authorized_group_bindings, return_value=_authorized_bindings()))
    monkeypatch.setattr(rd_svc, "get_feishu_open_id_for_user", create_autospec(rd_svc.get_feishu_open_id_for_user, return_value="ou_chao_open_1"))
    monkeypatch.setattr(rd_svc, "verify_feishu_chat_member", create_autospec(rd_svc.verify_feishu_chat_member, return_value=(True, None)))

    route_change = await rd_svc.validate_report_profile_change(
        _OWNER,
        auto_row,
        {"profile": "daily-filing-alert", "report_targets": [_FEISHU_CHAT_AUTH], "failure_targets": []},
        via_rest=False,
        user_confirmed=True,
        action="route_update",
        previous_groups_override=[],
        skip_skill_match=True,
    )
    assert route_change.metadata_unchanged is True
    audit_entry = route_change.build_audit_entry()
    assert audit_entry["metadata_unchanged"] is True
    assert audit_entry["note"] == "路由变更、metadata 未变"
    assert "metadata_after" not in audit_entry
    assert audit_entry["metadata_before"] == auto_row["metadata"]

    # 2. prepare_update_history_columns records report_delivery_audit even with skip_if_unchanged=True
    locked_row = {
        **auto_row,
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "config_version": 2,
        "active_version": 2,
        "audit_history": [
            {
                "version": 2,
                "prev_version": 1,
                "action": "update",
                "changes": {},
                "snapshot": {"name": auto_row["name"], "trigger_type": "cron", "desired_status": "active"},
            }
        ],
    }
    from src.dalton import access_tiers as access_tiers_mod
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )
    set_change = await rd_svc.validate_report_profile_change(
        _OWNER,
        auto_row,
        {"profile": "daily-filing-alert", "report_targets": [_FEISHU_CHAT_AUTH], "failure_targets": []},
        via_rest=False,
        user_confirmed=True,
        action="set",
    )
    with automation_history_context(
        actor_user_id=_OWNER,
        action="update",
        skip_if_unchanged=True,
        report_delivery_audit=set_change.build_audit_entry(),
    ):
        extra_cols, is_noop = await company_auto_db.prepare_update_history_columns(
            locked_row,
            {"metadata": set_change.after_metadata},
            actor_user_id=_OWNER,
            action="update",
            skip_if_unchanged=True,
            pref_row={},
            changed_at_iso="2026-11-17T00:00:00+00:00",
        )
    assert is_noop is False
    latest_hist = extra_cols["audit_history"][-1]
    assert latest_hist["report_delivery_audit"]["actor_user_id"] == _OWNER
    assert latest_hist["report_delivery_audit"]["metadata_before"] == auto_row["metadata"]
    assert latest_hist["report_delivery_audit"]["metadata_after"]["report_profile"]["report_targets"] == [_FEISHU_CHAT_AUTH]

    # 3. schedule.py transaction rollback leaves NO audit and NO admin notify
    @asynccontextmanager
    async def _failing_lock(*args: Any, **kwargs: Any):
        yield object()
        raise RuntimeError("tx commit failed")

    monkeypatch.setattr(sched_mod, "user_automations_lock", _failing_lock)
    monkeypatch.setattr(sched_mod, "verify_workspace_access", create_autospec(sched_mod.verify_workspace_access, return_value={"workspace_id": _WS_UUID, "user_id": _OWNER, "is_home": False}))
    monkeypatch.setattr(tracking_mod, "_resolve_workspace_ticker", create_autospec(tracking_mod._resolve_workspace_ticker, return_value="MSFT"))
    monkeypatch.setattr(sched_mod, "list_all_user_automations", create_autospec(sched_mod.list_all_user_automations, return_value=[]))
    monkeypatch.setattr(company_auto_db, "resolve_automation_scope_for_write", create_autospec(company_auto_db.resolve_automation_scope_for_write, return_value="personal"))
    monkeypatch.setattr(server_auto_db, "create_automation", create_autospec(server_auto_db.create_automation, return_value={"automation_id": _AUTO_UUID, "name": "dalton-msft-filing", "instruction": _DAILY_FILING_INST, "metadata": {}}))
    sync_routes_mock = create_autospec(rd_svc.sync_report_delivery_routes_in_tx, return_value=None)
    monkeypatch.setattr(rd_svc, "sync_report_delivery_routes_in_tx", sync_routes_mock)
    rd_tier_mock.return_value = "admin"
    notify_mock = create_autospec(rd_svc.notify_admins_group_target_change, return_value={"notified": True})
    monkeypatch.setattr(rd_svc, "notify_admins_group_target_change", notify_mock)

    with pytest.raises(RuntimeError, match="tx commit failed"):
        await sched_mod.schedule_dalton_automation_helper(
            "dalton-msft-filing",
            {
                "action": "upsert",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * *",
                "instruction": _DAILY_FILING_INST,
                "metadata": {
                    "report_profile": {
                        "profile": "daily-filing-alert",
                        "report_targets": [_FEISHU_CHAT_AUTH],
                        "failure_targets": [],
                    }
                },
            },
            user_id=_OWNER,
            workspace_id=_WS_UUID,
            caller_workspace_id=_WS_UUID,
            role="analyst",
            via_rest=True,
        )
    sync_routes_mock.assert_awaited_once()
    notify_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_r3_4_10_missing_profile_stable_notice_id_gateway_dedup_and_clear_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.database import automation_notifications as auto_notif_db

    # 1. check_existing_missing_profile_gateway_notice prevents duplicate send when gateway already has 'sent'
    check_gw_mock = create_autospec(
        rd_svc.check_existing_missing_profile_gateway_notice,
        return_value="sent",
    )
    monkeypatch.setattr(
        rd_svc,
        "check_existing_missing_profile_gateway_notice",
        check_gw_mock,
    )
    gw_mock = create_autospec(rd_svc.send_dalton_gateway_notice)
    ok_dedup, err_dedup = await rd_svc.notify_owner_missing_report_profile_once(
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        gateway_send_fn=gw_mock,
    )
    assert ok_dedup is True and err_dedup is None
    gw_mock.assert_not_awaited()

    # 2. When no prior gateway record, sends with stable notice_id="report-profile-missing:<id>"
    check_gw_mock.return_value = None
    with monkeypatch.context() as mp:
        mp.setattr(
            rd_svc,
            "resolve_dalton_notify_targets",
            create_autospec(rd_svc.resolve_dalton_notify_targets, return_value=[_CALLER_FEISHU_DM]),
        )
        gw_mock.return_value = {"ok": True, "sent_targets": [_CALLER_FEISHU_DM], "failed_targets": [], "errors": {}}
        ok_send, err_send = await rd_svc.notify_owner_missing_report_profile_once(
            owner_user_id=_OWNER,
            automation_id=_AUTO_UUID,
            gateway_send_fn=gw_mock,
        )
    assert ok_send is True and err_send is None
    assert gw_mock.await_args.kwargs["notice_id"] == f"report-profile-missing:{_AUTO_UUID}"

    # 3. reconcile_uncertain_automation_notify_deliveries includes report_delivery_missing_profile_notice
    captured_sqls: list[str] = []

    class _RecCur:
        async def __aenter__(self) -> "_RecCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            captured_sqls.append(sql)

        async def fetchall(self) -> list[Any]:
            return []

    class _RecConn:
        def cursor(self, **kwargs: Any) -> _RecCur:
            return _RecCur()

    @asynccontextmanager
    async def _fake_rec_conn(conn: Any = None):
        yield _RecConn()

    await auto_notif_db.reconcile_uncertain_automation_notify_deliveries(get_connection=_fake_rec_conn)
    assert any("report_delivery_missing_profile_notice" in s for s in captured_sqls)

    # 4. clear resets report_profile_missing_notified_at + report_profile_missing_notice + gateway deliveries
    row_with_notice = _make_automation_row(
        metadata={
            "report_profile": {"profile": "daily-filing-alert", "report_targets": [_CALLER_FEISHU_DM], "failure_targets": []},
            "report_profile_missing_notified_at": "2026-11-16T00:00:00+00:00",
            "report_profile_missing_notice": {"status": "sent", "attempts": 1},
        }
    )
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal", auto_row=row_with_notice)
    reset_mock = create_autospec(rd_svc.reset_missing_profile_notice_deliveries, return_value=None)
    monkeypatch.setattr(rd_svc, "reset_missing_profile_notice_deliveries", reset_mock)
    await rd_svc.clear_report_delivery_config(caller_user_id=_OWNER, automation_id=_AUTO_UUID)
    cleared_meta = mocks["update_auto"].await_args.kwargs["metadata"]
    assert "report_profile_missing_notified_at" not in cleared_meta
    assert "report_profile_missing_notice" not in cleared_meta
    reset_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_r3_6_dalton_disabled_preserves_upstream_metadata_and_rest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.server.models.automation import AutomationUpdate

    monkeypatch.setenv("DALTON_ENABLED", "0")

    # 1. Pydantic AutomationCreate / AutomationUpdate allow arbitrary metadata.report_profile when Dalton is disabled
    m_create = AutomationCreate(
        name="Upstream Task",
        trigger_type="cron",
        cron_expression="0 8 * * *",
        instruction=_DAILY_FILING_INST,
        metadata={"report_profile": {"custom_upstream_shape": True}},
    )
    assert m_create.metadata == {"report_profile": {"custom_upstream_shape": True}}
    m_update = AutomationUpdate(metadata={"report_profile": ["anything"]})
    assert m_update.metadata == {"report_profile": ["anything"]}

    # 2. REST create_automation / update_automation skip Dalton report_profile permissions & warnings when disabled
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    api_row = {
        "automation_id": UUID(_AUTO_UUID),
        "user_id": _OWNER,
        "name": "Upstream Task",
        "description": None,
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": None,
        "last_run_at": None,
        "instruction": _DAILY_FILING_INST,
        "workspace_id": UUID(_WS_UUID),
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": None,
        "metadata": {"report_profile": {"custom_upstream_shape": True}},
        "created_at": now,
        "updated_at": now,
        "last_execution": None,
        "scope": "personal",
    }
    monkeypatch.setattr(automations_app.cli_approval, "automation_runs", create_autospec(automations_app.cli_approval.automation_runs, return_value=None))
    monkeypatch.setattr(automations_app.handler, "create_automation", create_autospec(automations_app.handler.create_automation, return_value=api_row))
    monkeypatch.setattr(automations_app.handler, "update_automation", create_autospec(automations_app.handler.update_automation, return_value=api_row))
    from src.dalton.notify import dispatch as notify_dispatch

    monkeypatch.setattr(notify_dispatch, "populate_automation_notify_state", create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None))
    val_spy = create_autospec(rd_svc.validate_report_profile_change)
    monkeypatch.setattr(rd_svc, "validate_report_profile_change", val_spy)

    principal = MagicMock()
    principal.user_id = _OWNER
    http_req = Request({"type": "http", "headers": [], "app": MagicMock()})
    c_resp = await automations_app.create_automation(m_create, principal, http_req)
    assert c_resp.report_delivery_warning is None
    assert c_resp.route_warning is None

    u_resp = await automations_app.update_automation(
        _AUTO_UUID,
        principal,
        http_req,
        body={"metadata": {"report_profile": ["anything"]}},
    )
    assert u_resp.report_delivery_warning is None
    assert u_resp.route_warning is None
    val_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_r3_7_8_populate_notify_state_classified_report_task_rest_route_sync_and_docstring(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.dalton.notify import dispatch as dispatch_mod

    # 1. Classified task (dalton-msft-move-up -> price_move) with report_profile reads routes in populate_automation_notify_state
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(
            model_prefs_mod.get_user_preferences,
            return_value={
                "model_preference": {
                    "dalton_notify_overrides": {
                        "category:price_move": False,
                        f"automation:{_AUTO_UUID}": True,
                    },
                    "dalton_notify_routes": {
                        f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM],
                    },
                }
            },
        ),
    )

    class _RespObj:
        def __init__(self) -> None:
            self.automation_id = _AUTO_UUID
            self.user_id = _OWNER
            self.scope = "personal"
            self.name = "dalton-msft-move-up"
            self.metadata = {
                "dalton_kind": "price_move",
                "report_profile": {
                    "profile": "daily-filing-alert",
                    "report_targets": [_CALLER_FEISHU_DM],
                    "failure_targets": [],
                },
            }
            self.dalton_notify_category = None
            self.dalton_notify_effective = None
            self.dalton_notify_source = None
            self.dalton_notify_route_targets = None

    resp_obj = _RespObj()
    await dispatch_mod.populate_automation_notify_state(_OWNER, [resp_obj])
    assert resp_obj.dalton_notify_category == "price_move"
    assert resp_obj.dalton_notify_effective is True
    assert resp_obj.dalton_notify_source == "task"
    assert resp_obj.dalton_notify_route_targets == [_CALLER_FEISHU_DM]

    # 2. REST create_automation & update_automation sync automation:<id> route in same tx and expose route_warning
    mocks = _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="admin", master_enabled=True)
    mocks["eval_eligibility"].return_value = (False, "toggle_off", [])
    api_row = {
        "automation_id": UUID(_AUTO_UUID),
        "user_id": _OWNER,
        "name": "API Report Sync",
        "description": None,
        "trigger_type": "cron",
        "cron_expression": "0 8 * * *",
        "timezone": "UTC",
        "trigger_config": None,
        "next_run_at": None,
        "last_run_at": None,
        "instruction": _DAILY_FILING_INST,
        "workspace_id": UUID(_WS_UUID),
        "llm_model": None,
        "additional_context": None,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "status": "active",
        "max_failures": 3,
        "failure_count": 0,
        "disable_reason": None,
        "delivery_config": None,
        "metadata": {
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_FEISHU_CHAT_AUTH],
                "failure_targets": [],
            }
        },
        "created_at": now,
        "updated_at": now,
        "last_execution": None,
        "scope": "personal",
    }
    monkeypatch.setattr(automations_app.cli_approval, "automation_runs", create_autospec(automations_app.cli_approval.automation_runs, return_value=None))
    monkeypatch.setattr(automations_app.handler, "create_automation", create_autospec(automations_app.handler.create_automation, return_value=api_row))
    monkeypatch.setattr(dispatch_mod, "populate_automation_notify_state", create_autospec(dispatch_mod.populate_automation_notify_state, return_value=None))

    principal = MagicMock()
    principal.user_id = _OWNER
    http_req = Request({"type": "http", "headers": [], "app": MagicMock()})
    req_create = AutomationCreate(
        name="API Report Sync",
        trigger_type="cron",
        cron_expression="0 8 * * *",
        instruction=_DAILY_FILING_INST,
        workspace_id=UUID(_WS_UUID),
        metadata=api_row["metadata"],
    )
    c_resp = await automations_app.create_automation(req_create, principal, http_req)
    mocks["patch_prefs"].assert_awaited_once()
    sync_patch = mocks["patch_prefs"].await_args.args[1]
    assert sync_patch[rd_svc.DALTON_NOTIFY_OVERRIDES_KEY][f"automation:{_AUTO_UUID}"] is True
    assert sync_patch[rd_svc.DALTON_NOTIFY_ROUTES_KEY][f"automation:{_AUTO_UUID}"] == [_FEISHU_CHAT_AUTH]
    assert c_resp.route_warning is not None

    # 3. Tool docstring updated for owner's DM default failure_targets
    tool = create_report_delivery_tools()[0]
    assert "任务属主的私信" in (tool.description or "")


@pytest.mark.asyncio
async def test_r4_1_normal_preferences_save_without_unbound_local_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from src.dalton import access_tiers as access_tiers_mod
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.server.app import users as users_app
    from src.server.models.user import UserPreferencesUpdate

    now = datetime.now(timezone.utc)
    stored_prefs: dict[str, Any] = {
        "user_preference_id": str(uuid4()),
        "user_id": _OWNER,
        "risk_preference": {},
        "investment_preference": {},
        "agent_preference": {},
        "other_preference": {},
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [_CALLER_FEISHU_DM],
            "dalton_notify_routes": {},
        },
        "created_at": now,
        "updated_at": now,
    }

    fake_conn = SimpleNamespace(transaction=nullcontext)

    async def _fake_get_prefs(uid: str, conn: Any = None) -> dict[str, Any]:
        return dict(stored_prefs)

    async def _fake_upsert_prefs(
        user_id: str,
        risk_preference: Any = None,
        investment_preference: Any = None,
        agent_preference: Any = None,
        other_preference: Any = None,
        model_preference: Any = None,
        conn: Any = None,
        expect_other_preference: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if other_preference:
            stored_prefs["other_preference"] = {
                **stored_prefs["other_preference"],
                **other_preference,
            }
        if model_preference:
            stored_prefs["model_preference"] = {
                **stored_prefs["model_preference"],
                **model_preference,
            }
        return dict(stored_prefs)

    monkeypatch.setattr(users_app, "get_db_connection", lambda: nullcontext(fake_conn))
    monkeypatch.setattr(
        users_app,
        "db_get_user",
        create_autospec(users_app.db_get_user, return_value={"user_id": _OWNER}),
    )
    monkeypatch.setattr(
        users_app,
        "db_get_user_preferences",
        create_autospec(users_app.db_get_user_preferences, side_effect=_fake_get_prefs),
    )
    monkeypatch.setattr(
        users_app,
        "db_lock_user_preferences",
        create_autospec(users_app.db_lock_user_preferences, side_effect=_fake_get_prefs),
    )
    monkeypatch.setattr(
        users_app,
        "upsert_user_preferences",
        create_autospec(users_app.upsert_user_preferences, side_effect=_fake_upsert_prefs),
    )
    monkeypatch.setattr(
        users_app,
        "maybe_complete_onboarding",
        create_autospec(users_app.maybe_complete_onboarding, return_value=None),
    )
    monkeypatch.setattr(
        users_app,
        "carry_default_change",
        create_autospec(users_app.carry_default_change, return_value=0),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "load_bound_dm_targets",
        create_autospec(model_prefs_mod.load_bound_dm_targets, return_value={_CALLER_FEISHU_DM}),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "get_user_tier",
        create_autospec(rd_svc.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_caller_dm_identities",
        create_autospec(rd_svc.list_caller_dm_identities, return_value=_caller_identities()),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_authorized_group_bindings",
        create_autospec(rd_svc.list_authorized_group_bindings, return_value=_authorized_bindings()),
    )

    from src.server.database import automation as server_auto_db

    report_auto_row = _make_automation_row(
        metadata={
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_CALLER_FEISHU_DM],
                "failure_targets": [],
            }
        }
    )
    monkeypatch.setattr(
        server_auto_db,
        "get_automation",
        create_autospec(server_auto_db.get_automation, return_value=report_auto_row),
    )

    orig_record = model_prefs_mod.record_pending_report_route_changes_in_tx
    record_spy = create_autospec(orig_record, side_effect=orig_record)
    monkeypatch.setattr(
        model_prefs_mod,
        "record_pending_report_route_changes_in_tx",
        record_spy,
    )

    # 1. Saving only other_preference.subagents_default succeeds with Dalton enabled
    monkeypatch.setenv("DALTON_ENABLED", "1")
    res_sub = await users_app.update_preferences(
        UserPreferencesUpdate(other_preference={"subagents_default": True}),
        user_id=_OWNER,
    )
    assert res_sub.other_preference.subagents_default is True
    record_spy.assert_not_awaited()

    # 2. Saving model settings succeeds without calling record_pending_report_route_changes_in_tx
    monkeypatch.setattr(
        users_app,
        "_validate_default_model_choices",
        create_autospec(users_app._validate_default_model_choices, return_value=None),
    )
    res_model = await users_app.update_preferences(
        UserPreferencesUpdate(model_preference={"default_model": "gpt-5"}),
        user_id=_OWNER,
    )
    assert res_model.model_preference.default_model == "gpt-5"
    record_spy.assert_not_awaited()

    # 3. Saving search settings succeeds without calling record_pending_report_route_changes_in_tx
    res_search = await users_app.update_preferences(
        UserPreferencesUpdate(
            other_preference={"search_provider": "serper", "search_depth": "standard"}
        ),
        user_id=_OWNER,
    )
    assert res_search.other_preference.search_provider == "serper"
    record_spy.assert_not_awaited()

    # 4. Saving non-report dalton_notify_routes (category key) does not call record_pending_report_route_changes_in_tx
    await users_app.update_preferences(
        UserPreferencesUpdate(
            model_preference={"dalton_notify_routes": {"category:price_move": [_CALLER_FEISHU_DM]}}
        ),
        user_id=_OWNER,
    )
    record_spy.assert_not_awaited()

    # 5. When DALTON_ENABLED=0, even automation:<uuid> route write skips record_pending_report_route_changes_in_tx
    monkeypatch.setenv("DALTON_ENABLED", "0")
    await users_app.update_preferences(
        UserPreferencesUpdate(
            model_preference={"dalton_notify_routes": {f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM]}}
        ),
        user_id=_OWNER,
    )
    record_spy.assert_not_awaited()

    # 6. When DALTON_ENABLED=1 and a report automation route actually changed, it IS called in tx
    monkeypatch.setenv("DALTON_ENABLED", "1")
    stored_prefs["model_preference"]["dalton_notify_routes"] = {}
    await users_app.update_preferences(
        UserPreferencesUpdate(
            model_preference={"dalton_notify_routes": {f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM]}}
        ),
        user_id=_OWNER,
    )
    record_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_r4_2_dalton_rest_put_automation_syncs_routes_and_returns_route_warning_and_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    from src.dalton import access_tiers as access_tiers_mod
    from src.dalton.app import router as dalton_router
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import company_automations as company_auto_db
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.dalton.notify import dispatch as dispatch_mod
    from src.server.database import automation as server_auto_db
    from src.server.database import user as user_db

    monkeypatch.setenv("DALTON_ENABLED", "1")
    stored_prefs: dict[str, Any] = {
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [],
            "dalton_notify_routes": {},
            "dalton_notify_overrides": {},
        }
    }
    stored_automations: list[dict[str, Any]] = []
    lock_conn = SimpleNamespace(transaction=nullcontext)

    @asynccontextmanager
    async def _fake_user_lock(uid: str, *, conn: Any = None):
        yield lock_conn

    async def _fake_create_auto(**kwargs: Any) -> dict[str, Any]:
        row = {
            "automation_id": _AUTO_UUID,
            "user_id": kwargs["user_id"],
            "name": kwargs["name"],
            "instruction": kwargs["instruction"],
            "workspace_id": kwargs.get("workspace_id"),
            "metadata": dict(kwargs.get("metadata") or {}),
            "scope": kwargs.get("scope", "personal"),
            "config_version": 1,
        }
        stored_automations.append(row)
        return dict(row)

    async def _fake_update_auto(aid: str, uid: str, **kwargs: Any) -> dict[str, Any]:
        row = stored_automations[0]
        row.update({k: v for k, v in kwargs.items() if k != "conn"})
        row["config_version"] = int(row.get("config_version") or 1) + 1
        return dict(row)

    async def _fake_lock_prefs(uid: str, conn: Any = None) -> dict[str, Any]:
        return dict(stored_prefs)

    async def _fake_upsert_prefs(
        user_id: str,
        model_preference: dict[str, Any] | None = None,
        conn: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert conn is lock_conn
        if model_preference:
            mp = dict(stored_prefs["model_preference"])
            for k, v in model_preference.items():
                if isinstance(v, dict) and isinstance(mp.get(k), dict):
                    merged_sub = dict(mp[k])
                    for sk, sv in v.items():
                        if sv is None:
                            merged_sub.pop(sk, None)
                        else:
                            merged_sub[sk] = sv
                    mp[k] = merged_sub
                else:
                    mp[k] = v
            stored_prefs["model_preference"] = mp
        return dict(stored_prefs)

    monkeypatch.setattr(sched_mod, "user_automations_lock", _fake_user_lock)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": _WS_UUID, "user_id": _OWNER, "is_home": False},
        ),
    )
    monkeypatch.setattr(
        dalton_router,
        "_verify_workspace_access",
        create_autospec(dalton_router._verify_workspace_access, return_value=None),
    )
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(sched_mod.list_all_user_automations, side_effect=lambda *a, **kw: list(stored_automations)),
    )
    monkeypatch.setattr(
        company_auto_db,
        "resolve_automation_scope_for_write",
        create_autospec(company_auto_db.resolve_automation_scope_for_write, return_value="personal"),
    )
    monkeypatch.setattr(
        server_auto_db,
        "create_automation",
        create_autospec(server_auto_db.create_automation, side_effect=_fake_create_auto),
    )
    monkeypatch.setattr(
        server_auto_db,
        "update_automation",
        create_autospec(server_auto_db.update_automation, side_effect=_fake_update_auto),
    )
    monkeypatch.setattr(
        user_db,
        "lock_user_preferences",
        create_autospec(user_db.lock_user_preferences, side_effect=_fake_lock_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "upsert_user_preferences",
        create_autospec(user_db.upsert_user_preferences, side_effect=_fake_upsert_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_prefs_cache",
        create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_profile_cache",
        create_autospec(user_db.invalidate_user_profile_cache, return_value=None),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(model_prefs_mod.get_user_preferences, side_effect=lambda uid: dict(stored_prefs)),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "get_user_tier",
        create_autospec(rd_svc.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_caller_dm_identities",
        create_autospec(rd_svc.list_caller_dm_identities, return_value=_caller_identities()),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_authorized_group_bindings",
        create_autospec(rd_svc.list_authorized_group_bindings, return_value=_authorized_bindings()),
    )

    body_create = dalton_router.DaltonAutomationWriteRequest(
        workspace_id=_WS_UUID,
        spec={
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "instruction": _DAILY_FILING_INST,
            "metadata": {
                "report_profile": {
                    "profile": "daily-filing-alert",
                    "report_targets": [f"  {_CALLER_FEISHU_DM}  "],
                    "failure_targets": [_CALLER_DISCORD_DM],
                }
            },
        },
    )
    res_create = await dalton_router.put_dalton_automation_endpoint(
        name="dalton-custom-dm-report",
        body=body_create,
        user_id=_OWNER,
    )
    assert res_create["ok"] is True
    assert res_create["action"] == "created"
    assert res_create["route_warning"] is None
    assert stored_automations[0]["metadata"]["report_profile"]["report_targets"] == [_CALLER_FEISHU_DM]
    assert stored_prefs["model_preference"]["dalton_notify_overrides"][f"automation:{_AUTO_UUID}"] is True
    assert stored_prefs["model_preference"]["dalton_notify_routes"][f"automation:{_AUTO_UUID}"] == [
        _CALLER_FEISHU_DM,
        _CALLER_DISCORD_DM,
    ]

    # Deliverable via evaluate_user_notify_eligibility after REST create
    allowed, skip_reason, targets = await dispatch_mod.evaluate_user_notify_eligibility(
        _OWNER, stored_automations[0]
    )
    assert allowed is True
    assert skip_reason is None
    assert targets == [_CALLER_FEISHU_DM, _CALLER_DISCORD_DM]

    # Update path via the same endpoint also syncs routes and surfaces route_warning when blocked
    stored_prefs["model_preference"]["dalton_notify_overrides"][f"automation:{_AUTO_UUID}"] = False
    body_update = dalton_router.DaltonAutomationWriteRequest(
        workspace_id=_WS_UUID,
        spec={
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 9 * * *",
            "instruction": _DAILY_FILING_INST,
            "metadata": {
                "report_profile": {
                    "profile": "daily-filing-alert",
                    "report_targets": [_CALLER_FEISHU_DM],
                    "failure_targets": [],
                }
            },
        },
    )
    res_update = await dalton_router.put_dalton_automation_endpoint(
        name="dalton-custom-dm-report",
        body=body_update,
        user_id=_OWNER,
    )
    assert res_update["ok"] is True
    assert res_update["action"] == "updated"
    assert res_update["route_warning"] is None
    assert stored_prefs["model_preference"]["dalton_notify_routes"][f"automation:{_AUTO_UUID}"] == [
        _CALLER_FEISHU_DM
    ]


@pytest.mark.asyncio
async def test_r4_3_route_update_audit_snapshot_uses_real_tx_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager
    from src.dalton.database import company_automations as company_auto_db
    from src.dalton import access_tiers as access_tiers_mod

    auto_row = _make_automation_row(
        metadata={
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_CALLER_FEISHU_DM],
                "failure_targets": [],
            }
        }
    )
    auto_row.update(
        {
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "timezone": "UTC",
            "notify_storage_mode": "legacy",
            "config_version": 2,
            "active_version": 2,
            "audit_history": [
                {
                    "version": 2,
                    "prev_version": 1,
                    "action": "update",
                    "changes": {},
                    "snapshot": {
                        "name": auto_row["name"],
                        "trigger_type": "cron",
                        "desired_status": "active",
                        "notify_enabled": True,
                        "notify_routes": [],
                    },
                }
            ],
        }
    )

    real_tx_prefs = {
        "user_id": _OWNER,
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [_CALLER_FEISHU_DM],
            "dalton_notify_overrides": {f"automation:{_AUTO_UUID}": True},
            "dalton_notify_routes": {f"automation:{_AUTO_UUID}": [_CALLER_FEISHU_DM]},
        },
        "other_preference": {},
    }
    captured_history: list[Any] = []

    class _AuditCur:
        def __init__(self) -> None:
            self._row: Any = None

        async def __aenter__(self) -> "_AuditCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "FROM automations" in sql:
                self._row = dict(auto_row)
            elif "FROM user_preferences" in sql:
                self._row = dict(real_tx_prefs)
            elif "UPDATE automations" in sql and params:
                captured_history.append(json.loads(params[3]))

        async def fetchone(self) -> Any:
            return self._row

    class _AuditConn:
        def cursor(self, **kwargs: Any) -> _AuditCur:
            return _AuditCur()

    @asynccontextmanager
    async def _fake_conn(c: Any = None):
        yield c

    monkeypatch.setattr(company_auto_db, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "get_user_tier",
        create_autospec(rd_svc.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_caller_dm_identities",
        create_autospec(rd_svc.list_caller_dm_identities, return_value=_caller_identities()),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_authorized_group_bindings",
        create_autospec(rd_svc.list_authorized_group_bindings, return_value=_authorized_bindings()),
    )

    change = await rd_svc.validate_report_profile_change(
        _OWNER,
        auto_row,
        {"profile": "daily-filing-alert", "report_targets": [_CALLER_FEISHU_DM], "failure_targets": []},
        via_rest=False,
        user_confirmed=False,
        action="route_update",
        previous_groups_override=[],
        skip_skill_match=True,
    )
    new_ver = await rd_svc.append_report_delivery_route_audit_in_tx(
        _AuditConn(), change=change
    )
    assert new_ver == 3
    snap = captured_history[-1][-1]["snapshot"]
    assert snap["notify_enabled"] is True
    assert snap["notify_routes"] == [_CALLER_FEISHU_DM]


@pytest.mark.asyncio
async def test_r5_2_finalize_once_best_effort_records_per_target_results_without_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    # 1. build_audit_entry does NOT use pending/retrying states
    change_v2 = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_OWNER,
        caller_tier="internal",
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Daily Filing",
        action="set",
        before_metadata={},
        after_metadata={"report_profile": {"profile": "daily-filing-alert", "report_targets": [_FEISHU_CHAT_AUTH], "failure_targets": []}},
        profile={"profile": "daily-filing-alert", "report_targets": [_FEISHU_CHAT_AUTH], "failure_targets": []},
        previous_groups=[],
        new_groups=[_FEISHU_CHAT_AUTH],
        audit_version=2,
        admin_notify_claimed=True,
    )
    entry_v2 = change_v2.build_audit_entry()
    assert entry_v2["admin_notify"]["required"] is True
    assert entry_v2["admin_notify"]["status"] == "not_sent"
    assert entry_v2["admin_notify"]["status"] not in {"pending", "retrying"}

    stored_history: list[dict[str, Any]] = [
        {
            "version": 2,
            "report_delivery_audit": entry_v2,
        }
    ]

    class _AuditCur:
        def __init__(self) -> None:
            self._row: Any = None

        async def __aenter__(self) -> "_AuditCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "SELECT audit_history" in sql:
                self._row = {
                    "automation_id": _AUTO_UUID,
                    "user_id": _OWNER,
                    "name": "Daily Filing",
                    "audit_history": list(stored_history),
                }
            elif "UPDATE automations" in sql and params:
                stored_history[:] = json.loads(params[0])

        async def fetchone(self) -> Any:
            return self._row

    class _AuditTx:
        async def __aenter__(self) -> "_AuditTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _AuditConn:
        def transaction(self) -> _AuditTx:
            return _AuditTx()

        def cursor(self, **kwargs: Any) -> _AuditCur:
            return _AuditCur()

    @asynccontextmanager
    async def _fake_db_conn(conn: Any = None):
        yield _AuditConn()

    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_db_conn)

    # 2. Per-target partial failure writes each target's success/error into version audit
    partial_notify = create_autospec(
        rd_svc.notify_admins_group_target_change,
        return_value={
            "notified": True,
            "partial_failure": True,
            "targets": ["feishu:user:ou_admin1"],
            "failed_targets": ["feishu:user:ou_admin2"],
            "errors": {"feishu:user:ou_admin2": "gateway_timeout"},
            "results": [
                {"admin_user_id": "admin-1", "target": "feishu:user:ou_admin1", "ok": True, "status": "sent", "error_code": None},
                {"admin_user_id": "admin-2", "target": "feishu:user:ou_admin2", "ok": False, "status": "failed", "error_code": "gateway_timeout"},
            ],
        },
    )
    out_partial = await change_v2.finalize(notify_admins_fn=partial_notify)
    assert out_partial["admin_notify_result"]["notified"] is True
    assert "replayed_pending_admin_notify" not in out_partial
    an_audit = stored_history[0]["report_delivery_audit"]["admin_notify"]
    assert an_audit["status"] == "sent"
    assert an_audit["partial_failure"] is True
    assert an_audit["errors"] == {"feishu:user:ou_admin2": "gateway_timeout"}
    assert len(an_audit["targets"]) == 2
    assert an_audit["targets"][1]["error_code"] == "gateway_timeout"

    # 3. Exception during notify records failure in audit without raising, and subsequent throttled change does NOT auto-replay
    change_fail = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_OWNER,
        caller_tier="internal",
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Daily Filing",
        action="set",
        before_metadata={},
        after_metadata={},
        profile={"profile": "daily-filing-alert", "report_targets": [_FEISHU_CHAT_AUTH], "failure_targets": []},
        previous_groups=[],
        new_groups=[_FEISHU_CHAT_AUTH],
        audit_version=3,
        admin_notify_claimed=True,
    )
    stored_history[:] = [{"version": 3, "report_delivery_audit": change_fail.build_audit_entry()}]
    failing_notify = create_autospec(
        rd_svc.notify_admins_group_target_change,
        side_effect=RuntimeError("gateway down"),
    )
    out_err = await change_fail.finalize(notify_admins_fn=failing_notify)
    assert out_err["admin_notify_result"]["notified"] is False
    assert out_err["admin_notify_result"]["reason"] == "finalize_error:RuntimeError"
    assert stored_history[0]["report_delivery_audit"]["admin_notify"]["status"] == "failed"
    assert stored_history[0]["report_delivery_audit"]["admin_notify"]["reason"] == "finalize_error:RuntimeError"

    # Subsequent throttled change does NOT replay v3
    change_throttled = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_OWNER,
        caller_tier="internal",
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Daily Filing",
        action="set",
        before_metadata={},
        after_metadata={},
        profile=None,
        previous_groups=[_FEISHU_CHAT_AUTH],
        new_groups=[],
        audit_version=4,
        admin_notify_claimed=False,
    )
    stored_history.append({"version": 4, "report_delivery_audit": change_throttled.build_audit_entry(throttled=True)})
    out_throttled = await change_throttled.finalize(notify_admins_fn=failing_notify)
    assert out_throttled["admin_notify_result"]["throttled"] is True
    assert failing_notify.await_count == 1
    assert stored_history[0]["report_delivery_audit"]["admin_notify"]["status"] == "failed"
    assert stored_history[1]["report_delivery_audit"]["admin_notify"]["status"] == "throttled"

    # 4. Also never raises if audit writeback itself raises
    change_err2 = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_OWNER,
        caller_tier="internal",
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Daily Filing",
        action="set",
        before_metadata={},
        after_metadata={},
        profile=None,
        previous_groups=[_FEISHU_CHAT_AUTH],
        new_groups=[],
        audit_version=5,
        admin_notify_claimed=True,
    )
    monkeypatch.setattr(
        rd_svc,
        "record_admin_notify_results_in_audit",
        create_autospec(
            rd_svc.record_admin_notify_results_in_audit,
            side_effect=RuntimeError("db writeback failed"),
        ),
    )
    out_err2 = await change_err2.finalize(notify_admins_fn=failing_notify)
    assert out_err2["admin_notify_result"]["status"] == "failed"
    assert not hasattr(rd_svc, "replay_pending_admin_notify_for_automation")


@pytest.mark.asyncio
async def test_r4_5_rest_create_persists_normalized_after_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import datetime, timezone
    from src.dalton.notify import dispatch as notify_dispatch

    monkeypatch.setenv("DALTON_ENABLED", "1")
    _patch_common_set_deps(monkeypatch, tmp_path=tmp_path, tier="internal", master_enabled=True)
    now = datetime.now(timezone.utc)

    captured_create_data: list[Any] = []

    async def _capture_create(*, user_id: str, data: Any, conn: Any = None, **kwargs: Any) -> dict[str, Any]:
        captured_create_data.append(data)
        return {
            "automation_id": UUID(_AUTO_UUID),
            "user_id": user_id,
            "name": data.name,
            "description": None,
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "timezone": "UTC",
            "trigger_config": None,
            "next_run_at": None,
            "last_run_at": None,
            "instruction": data.instruction,
            "workspace_id": UUID(_WS_UUID),
            "llm_model": None,
            "additional_context": None,
            "thread_strategy": "new",
            "conversation_thread_id": None,
            "status": "active",
            "max_failures": 3,
            "failure_count": 0,
            "disable_reason": None,
            "delivery_config": None,
            "metadata": dict(data.metadata or {}),
            "created_at": now,
            "updated_at": now,
            "last_execution": None,
            "scope": "personal",
        }

    monkeypatch.setattr(
        automations_app.cli_approval,
        "automation_runs",
        create_autospec(automations_app.cli_approval.automation_runs, return_value=None),
    )
    monkeypatch.setattr(
        automations_app.handler,
        "create_automation",
        create_autospec(automations_app.handler.create_automation, side_effect=_capture_create),
    )
    monkeypatch.setattr(
        notify_dispatch,
        "populate_automation_notify_state",
        create_autospec(notify_dispatch.populate_automation_notify_state, return_value=None),
    )

    principal = MagicMock()
    principal.user_id = _OWNER
    http_req = Request({"type": "http", "headers": [], "app": MagicMock()})
    req_create = AutomationCreate(
        name="REST Create Normalized",
        trigger_type="cron",
        cron_expression="0 8 * * *",
        instruction=_DAILY_FILING_INST,
        workspace_id=UUID(_WS_UUID),
        metadata={
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [f"  {_CALLER_FEISHU_DM}  "],
                "failure_targets": [],
            },
            rd_svc.REPORT_PROFILE_MISSING_NOTIFIED_KEY: "2026-01-01T00:00:00Z",
        },
    )
    resp = await automations_app.create_automation(req_create, principal, http_req)
    assert len(captured_create_data) == 1
    persisted_meta = captured_create_data[0].metadata
    assert persisted_meta["report_profile"] == {
        "profile": "daily-filing-alert",
        "report_targets": [_CALLER_FEISHU_DM],
        "failure_targets": [],
    }
    assert rd_svc.REPORT_PROFILE_MISSING_NOTIFIED_KEY not in persisted_meta
    assert resp.metadata["report_profile"]["report_targets"] == [_CALLER_FEISHU_DM]


@pytest.mark.asyncio
async def test_r4_6_missing_profile_no_dm_targets_does_not_consume_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auto_state: dict[str, Any] = {
        "automation_id": _AUTO_UUID,
        "user_id": _OWNER,
        "name": "Missing Profile Task",
        "metadata": {},
    }

    class _NoDmCur:
        def __init__(self) -> None:
            self._row: Any = None

        async def __aenter__(self) -> "_NoDmCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "SELECT automation_id, user_id, name, metadata" in sql:
                self._row = dict(auto_state)
            elif "UPDATE automations" in sql and params:
                notice_payload = json.loads(params[0])
                if "WHERE automation_id = %s::uuid\n" in sql and "AND COALESCE" in sql:
                    cur_notice = auto_state["metadata"].get(rd_svc.REPORT_PROFILE_MISSING_NOTICE_KEY) or {}
                    if (
                        cur_notice.get("status") == "pending"
                        and int(cur_notice.get("attempts") or 0) == int(params[2])
                    ):
                        auto_state["metadata"][rd_svc.REPORT_PROFILE_MISSING_NOTICE_KEY] = notice_payload
                else:
                    auto_state["metadata"][rd_svc.REPORT_PROFILE_MISSING_NOTICE_KEY] = notice_payload

        async def fetchone(self) -> Any:
            return self._row

    class _NoDmTx:
        async def __aenter__(self) -> "_NoDmTx":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

    class _NoDmConn:
        def transaction(self) -> _NoDmTx:
            return _NoDmTx()

        def cursor(self, **kwargs: Any) -> _NoDmCur:
            return _NoDmCur()

    monkeypatch.setattr(
        rd_svc,
        "check_existing_missing_profile_gateway_notice",
        create_autospec(rd_svc.check_existing_missing_profile_gateway_notice, return_value=None),
    )

    empty_resolve = create_autospec(rd_svc.resolve_dalton_notify_targets, return_value=[])
    empty_identities = create_autospec(rd_svc.list_caller_dm_identities, return_value=[])

    # Run 4 times with no DM targets: none should succeed, and attempts must stay at 0
    for _ in range(4):
        sent = await rd_svc.handle_missing_report_profile_rejection(
            owner_user_id=_OWNER,
            automation_id=_AUTO_UUID,
            conn=_NoDmConn(),
            resolve_targets_fn=empty_resolve,
            list_dm_identities_fn=empty_identities,
        )
        assert sent is False
        notice = auto_state["metadata"][rd_svc.REPORT_PROFILE_MISSING_NOTICE_KEY]
        assert notice["status"] == "failed"
        assert notice["last_error"] == "no_dm_targets"
        assert notice["attempts"] == 0

    # Once owner binds a DM target, full 3-attempt budget is still available and attempt 1 sends
    gw_mock = create_autospec(
        rd_svc.send_dalton_gateway_notice,
        return_value={"ok": True, "sent_targets": [_CALLER_FEISHU_DM], "failed_targets": [], "errors": {}},
    )
    sent_now = await rd_svc.handle_missing_report_profile_rejection(
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        conn=_NoDmConn(),
        resolve_targets_fn=create_autospec(rd_svc.resolve_dalton_notify_targets, return_value=[_CALLER_FEISHU_DM]),
        list_dm_identities_fn=empty_identities,
        gateway_send_fn=gw_mock,
    )
    assert sent_now is True
    gw_mock.assert_awaited_once()
    final_notice = auto_state["metadata"][rd_svc.REPORT_PROFILE_MISSING_NOTICE_KEY]
    assert final_notice["status"] == "sent"
    assert final_notice["attempts"] == 1


@pytest.mark.asyncio
async def test_r5_1_audit_snapshot_reflects_post_sync_routes_on_create_and_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import nullcontext
    from datetime import datetime, timezone

    from src.dalton import access_tiers as access_tiers_mod
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import company_automations as company_auto_db
    from src.dalton.llm import model_prefs as model_prefs_mod
    from src.server.database import automation as server_auto_db
    from src.server.database import user as user_db
    from src.server.services.automations import lifecycle as auto_lifecycle

    monkeypatch.setenv("DALTON_ENABLED", "1")
    now = datetime.now(timezone.utc)
    stored_prefs: dict[str, Any] = {
        "user_id": _OWNER,
        "model_preference": {
            "dalton_notify_enabled": True,
            "dalton_notify_targets": [],
            "dalton_notify_routes": {},
            "dalton_notify_overrides": {},
        },
        "other_preference": {},
    }
    stored_automations: dict[str, dict[str, Any]] = {}

    def _unwrap(val: Any) -> Any:
        return val.obj if hasattr(val, "obj") else val

    class _InMemCur:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []

        async def __aenter__(self) -> "_InMemCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            self._one = None
            self._many = []
            norm = " ".join(sql.split())
            if norm.startswith("SELECT file_name FROM automations"):
                self._many = []
                return
            if "INSERT INTO automations" in norm and params:
                aid = str(params[0])
                row = {
                    "automation_id": aid,
                    "user_id": params[1],
                    "name": params[2],
                    "file_name": params[3],
                    "description": params[4],
                    "trigger_type": params[5],
                    "cron_expression": params[6],
                    "timezone": params[7],
                    "trigger_config": _unwrap(params[8]),
                    "next_run_at": params[9],
                    "last_run_at": None,
                    "instruction": params[10],
                    "workspace_id": params[11],
                    "llm_model": params[12],
                    "additional_context": _unwrap(params[13]),
                    "thread_strategy": params[14],
                    "conversation_thread_id": params[15],
                    "status": params[16],
                    "max_failures": params[17],
                    "failure_count": 0,
                    "disable_reason": None,
                    "delivery_config": _unwrap(params[18]),
                    "metadata": _unwrap(params[19]),
                    "scope": params[20],
                    "notify_enabled": True,
                    "notify_routes": [],
                    "notify_storage_mode": "legacy",
                    "created_by": params[21],
                    "updated_by": params[22],
                    "config_version": params[23],
                    "active_version": params[24],
                    "audit_history": _unwrap(params[25]),
                    "deleted_at": None,
                    "deleted_by": None,
                    "created_at": now,
                    "updated_at": now,
                    "last_execution": None,
                }
                stored_automations[aid] = row
                self._one = dict(row)
                return
            if "WITH updated AS (UPDATE automations SET " in norm and params:
                set_part = norm.split("WITH updated AS (UPDATE automations SET ", 1)[1].split(" WHERE ", 1)[0]
                assignments = [seg.strip() for seg in set_part.split(",")]
                aid = str(params[-2])
                row = stored_automations[aid]
                p_idx = 0
                for assign in assignments:
                    col = assign.split("=", 1)[0].strip()
                    if "%s" in assign:
                        row[col] = _unwrap(params[p_idx])
                        p_idx += 1
                self._one = dict(row)
                return
            if norm.startswith("UPDATE automations SET audit_history") and params:
                aid = str(params[1])
                stored_automations[aid]["audit_history"] = json.loads(params[0])
                return
            if "FROM automations" in norm and params:
                aid = str(params[0])
                row = stored_automations.get(aid)
                self._one = dict(row) if row else None
                return

        async def fetchone(self) -> Any:
            return self._one

        async def fetchall(self) -> list[Any]:
            return list(self._many)

    class _InMemConn:
        info = object()

        def transaction(self) -> Any:
            return nullcontext()

        def cursor(self, **kwargs: Any) -> _InMemCur:
            return _InMemCur()

    mem_conn = _InMemConn()

    @asynccontextmanager
    async def _fake_get_conn(conn: Any = None):
        yield mem_conn

    @asynccontextmanager
    async def _fake_user_lock(uid: str, *, conn: Any = None):
        yield mem_conn

    async def _fake_lock_prefs(uid: str, conn: Any = None) -> dict[str, Any]:
        return json.loads(json.dumps(stored_prefs))

    async def _fake_upsert_prefs(
        user_id: str,
        model_preference: dict[str, Any] | None = None,
        conn: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if model_preference:
            mp = dict(stored_prefs["model_preference"])
            for k, v in model_preference.items():
                if isinstance(v, dict) and isinstance(mp.get(k), dict):
                    merged_sub = dict(mp[k])
                    for sk, sv in v.items():
                        if sv is None:
                            merged_sub.pop(sk, None)
                        else:
                            merged_sub[sk] = sv
                    mp[k] = merged_sub
                else:
                    mp[k] = v
            stored_prefs["model_preference"] = mp
        return json.loads(json.dumps(stored_prefs))

    monkeypatch.setattr(server_auto_db, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(company_auto_db, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_get_conn)
    monkeypatch.setattr(sched_mod, "user_automations_lock", _fake_user_lock)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": _WS_UUID, "user_id": _OWNER, "is_home": False},
        ),
    )
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(
            sched_mod.list_all_user_automations,
            side_effect=lambda *a, **kw: [dict(r) for r in stored_automations.values()],
        ),
    )
    monkeypatch.setattr(
        company_auto_db,
        "resolve_automation_scope_for_write",
        create_autospec(company_auto_db.resolve_automation_scope_for_write, return_value="personal"),
    )
    monkeypatch.setattr(
        user_db,
        "lock_user_preferences",
        create_autospec(user_db.lock_user_preferences, side_effect=_fake_lock_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "upsert_user_preferences",
        create_autospec(user_db.upsert_user_preferences, side_effect=_fake_upsert_prefs),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_prefs_cache",
        create_autospec(user_db.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        user_db,
        "invalidate_user_profile_cache",
        create_autospec(user_db.invalidate_user_profile_cache, return_value=None),
    )
    monkeypatch.setattr(
        rd_svc,
        "invalidate_user_prefs_cache",
        create_autospec(rd_svc.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        rd_svc,
        "invalidate_user_profile_cache",
        create_autospec(rd_svc.invalidate_user_profile_cache, return_value=None),
    )
    monkeypatch.setattr(
        model_prefs_mod,
        "get_user_preferences",
        create_autospec(
            model_prefs_mod.get_user_preferences,
            side_effect=lambda uid: json.loads(json.dumps(stored_prefs)),
        ),
    )
    monkeypatch.setattr(
        access_tiers_mod,
        "get_user_tier",
        create_autospec(access_tiers_mod.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "get_user_tier",
        create_autospec(rd_svc.get_user_tier, return_value="internal"),
    )
    feishu_dm_b = "feishu:user:ou_chao_open_2"
    identities_ab = _caller_identities() + [
        {
            "platform": "feishu",
            "platform_user_id": "ou_chao_open_2",
            "target": feishu_dm_b,
            "supports_report_cards": True,
            "supports_text_targets": True,
            "supports_failure_targets": False,
        }
    ]
    monkeypatch.setattr(
        rd_svc,
        "list_caller_dm_identities",
        create_autospec(rd_svc.list_caller_dm_identities, return_value=identities_ab),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_authorized_group_bindings",
        create_autospec(rd_svc.list_authorized_group_bindings, return_value=_authorized_bindings()),
    )
    monkeypatch.setattr(
        rd_svc,
        "get_feishu_open_id_for_user",
        create_autospec(rd_svc.get_feishu_open_id_for_user, return_value="ou_chao_open_1"),
    )
    monkeypatch.setattr(
        rd_svc,
        "verify_feishu_chat_member",
        create_autospec(rd_svc.verify_feishu_chat_member, return_value=(True, None)),
    )
    monkeypatch.setattr(
        rd_svc,
        "sync_owner_group_chat_settings",
        create_autospec(rd_svc.sync_owner_group_chat_settings, return_value=None),
    )

    # 1. Schedule create with target A -> v1 snapshot.notify_routes must already contain A
    res_create = await sched_mod.schedule_dalton_automation_helper(
        user_id=_OWNER,
        workspace_id=_WS_UUID,
        name="dalton-r5-snapshot-test",
        spec={
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "instruction": _DAILY_FILING_INST,
            "metadata": {
                "report_profile": {
                    "profile": "daily-filing-alert",
                    "report_targets": [_CALLER_FEISHU_DM],
                    "failure_targets": [],
                }
            },
        },
        via_rest=True,
    )
    assert res_create["ok"] is True
    aid = res_create["automation_id"]
    hist_v1 = stored_automations[aid]["audit_history"]
    assert len(hist_v1) == 1
    assert hist_v1[0]["snapshot"]["notify_enabled"] is True
    assert hist_v1[0]["snapshot"]["notify_routes"] == [_CALLER_FEISHU_DM]

    # 2. Schedule update A -> B -> v2 snapshot.notify_routes must be B (not A!) and changes must show A -> B
    res_update = await sched_mod.schedule_dalton_automation_helper(
        user_id=_OWNER,
        workspace_id=_WS_UUID,
        name="dalton-r5-snapshot-test",
        spec={
            "action": "upsert",
            "trigger_type": "cron",
            "cron_expression": "0 8 * * *",
            "instruction": _DAILY_FILING_INST,
            "metadata": {
                "report_profile": {
                    "profile": "daily-filing-alert",
                    "report_targets": [feishu_dm_b],
                    "failure_targets": [],
                }
            },
        },
        via_rest=True,
    )
    assert res_update["ok"] is True
    hist_v2 = stored_automations[aid]["audit_history"]
    assert len(hist_v2) == 2
    assert hist_v2[-1]["snapshot"]["notify_routes"] == [feishu_dm_b]
    assert hist_v2[-1]["changes"]["notify_routes"] == {
        "old": [_CALLER_FEISHU_DM],
        "new": [feishu_dm_b],
    }

    # 3. REST common handler update B -> A -> v3 snapshot.notify_routes must be A (not B!)
    monkeypatch.setattr(
        auto_lifecycle,
        "require_target_ownership",
        create_autospec(auto_lifecycle.require_target_ownership, return_value=None),
    )
    change_rest = await rd_svc.validate_report_profile_change(
        _OWNER,
        stored_automations[aid],
        {
            "profile": "daily-filing-alert",
            "report_targets": [_CALLER_FEISHU_DM],
            "failure_targets": [],
        },
        via_rest=True,
        action="update",
        after_metadata={
            "report_profile": {
                "profile": "daily-filing-alert",
                "report_targets": [_CALLER_FEISHU_DM],
                "failure_targets": [],
            }
        },
        conn=mem_conn,
    )
    saved_rest, _ = await rd_svc.execute_report_profile_rest_mutation(
        change=change_rest,
        actor_user_id=_OWNER,
        mutate_fn=lambda db_conn: auto_lifecycle.update_automation(
            automation_id=aid,
            user_id=_OWNER,
            fields={"metadata": change_rest.after_metadata},
            conn=db_conn,
        ),
        conn=mem_conn,
    )
    assert saved_rest is not None
    hist_v3 = stored_automations[aid]["audit_history"]
    assert len(hist_v3) == 3
    assert hist_v3[-1]["snapshot"]["notify_routes"] == [_CALLER_FEISHU_DM]
    assert hist_v3[-1]["changes"]["notify_routes"] == {
        "old": [feishu_dm_b],
        "new": [_CALLER_FEISHU_DM],
    }


@pytest.mark.asyncio
async def test_r5_3_schedule_clear_company_and_non_uuid_resets_missing_profile_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID

    reset_mock = create_autospec(rd_svc.reset_missing_profile_notice_deliveries, return_value=None)
    monkeypatch.setattr(rd_svc, "reset_missing_profile_notice_deliveries", reset_mock)

    clear_change = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_ADMIN,
        caller_tier="admin",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        automation_id=_AUTO_UUID,
        automation_name="Company Digest",
        action="clear",
        before_metadata={},
        after_metadata={},
        profile=None,
        previous_groups=[],
        new_groups=[],
    )
    warn_company = await sched_mod._sync_report_profile_in_schedule_tx(
        object(),
        change=clear_change,
        saved_row={
            "automation_id": _AUTO_UUID,
            "user_id": COMPANY_SYSTEM_USER_ID,
            "scope": "company",
            "config_version": 2,
        },
        default_owner_user_id=COMPANY_SYSTEM_USER_ID,
    )
    assert warn_company is None
    reset_mock.assert_awaited_once_with(
        reset_mock.await_args.args[0],
        _AUTO_UUID,
    )

    reset_mock.reset_mock()
    non_uuid_change = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_OWNER,
        caller_tier="internal",
        owner_user_id=_OWNER,
        automation_id="non-uuid-id",
        automation_name="Legacy Task",
        action="clear",
        before_metadata={},
        after_metadata={},
        profile=None,
        previous_groups=[],
        new_groups=[],
    )
    warn_non_uuid = await sched_mod._sync_report_profile_in_schedule_tx(
        object(),
        change=non_uuid_change,
        saved_row={
            "automation_id": "non-uuid-id",
            "user_id": _OWNER,
            "scope": "personal",
            "config_version": 2,
        },
        default_owner_user_id=_OWNER,
    )
    assert warn_non_uuid is not None
    reset_mock.assert_awaited_once_with(
        reset_mock.await_args.args[0],
        "non-uuid-id",
    )


@pytest.mark.asyncio
async def test_r5_4_compute_route_warning_failure_logs_and_audits_without_500(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from contextlib import nullcontext
    from src.dalton.autonomy import schedule as sched_mod
    from src.dalton.database import company_automations as company_auto_db
    from src.server.database import automation as server_auto_db

    stored_history: list[dict[str, Any]] = [
        {
            "version": 2,
            "report_delivery_audit": {
                "action": "set",
                "actor_user_id": _OWNER,
                "owner_user_id": _OWNER,
                "admin_notify": {"required": False, "status": "not_required", "throttled": False, "targets": []},
            },
        }
    ]

    class _ErrAuditCur:
        def __init__(self) -> None:
            self._row: Any = None

        async def __aenter__(self) -> "_ErrAuditCur":
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "SELECT audit_history" in sql:
                self._row = {"audit_history": list(stored_history)}
            elif "UPDATE automations" in sql and params:
                stored_history[:] = json.loads(params[0])

        async def fetchone(self) -> Any:
            return self._row

    class _ErrAuditConn:
        def transaction(self) -> Any:
            return nullcontext()

        def cursor(self, **kwargs: Any) -> _ErrAuditCur:
            return _ErrAuditCur()

    @asynccontextmanager
    async def _fake_conn(conn: Any = None):
        yield _ErrAuditConn()

    @asynccontextmanager
    async def _fake_user_lock(uid: str, *, conn: Any = None):
        yield _ErrAuditConn()

    monkeypatch.setattr(rd_svc, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        rd_svc,
        "compute_route_warning_for_automation",
        create_autospec(
            rd_svc.compute_route_warning_for_automation,
            side_effect=RuntimeError("db blip"),
        ),
    )
    monkeypatch.setattr(
        rd_svc,
        "apply_dalton_notify_preference_patch",
        create_autospec(rd_svc.apply_dalton_notify_preference_patch, return_value={}),
    )
    monkeypatch.setattr(
        rd_svc,
        "invalidate_user_prefs_cache",
        create_autospec(rd_svc.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        rd_svc,
        "invalidate_user_profile_cache",
        create_autospec(rd_svc.invalidate_user_profile_cache, return_value=None),
    )

    change_rest = rd_svc.ValidatedReportProfileChange(
        actor_user_id=_OWNER,
        caller_tier="internal",
        owner_user_id=_OWNER,
        automation_id=_AUTO_UUID,
        automation_name="Route Err Task",
        action="update",
        before_metadata={},
        after_metadata={"report_profile": {"profile": "daily-filing-alert", "report_targets": [_CALLER_FEISHU_DM], "failure_targets": []}},
        profile={"profile": "daily-filing-alert", "report_targets": [_CALLER_FEISHU_DM], "failure_targets": []},
        previous_groups=[],
        new_groups=[],
        route_targets=[_CALLER_FEISHU_DM],
    )

    with caplog.at_level("WARNING"):
        saved_row, rest_warning = await rd_svc.execute_report_profile_rest_mutation(
            change=change_rest,
            actor_user_id=_OWNER,
            mutate_fn=lambda db_conn: _fake_async_return(
                {
                    "automation_id": _AUTO_UUID,
                    "user_id": _OWNER,
                    "name": "Route Err Task",
                    "scope": "personal",
                    "config_version": 2,
                    "metadata": dict(change_rest.after_metadata),
                }
            ),
        )
    assert saved_row is not None
    assert rest_warning is not None and "RuntimeError" in rest_warning
    assert "route_warning_error" in stored_history[0]["report_delivery_audit"]
    assert "RuntimeError: db blip" in stored_history[0]["report_delivery_audit"]["route_warning_error"]["error"]
    assert "route_warning evaluation failed" in caplog.text

    # Also verify schedule.py does not raise when compute_route_warning_for_automation fails
    stored_history[0]["report_delivery_audit"].pop("route_warning_error", None)
    monkeypatch.setattr(sched_mod, "user_automations_lock", _fake_user_lock)
    monkeypatch.setattr(
        sched_mod,
        "verify_workspace_access",
        create_autospec(
            sched_mod.verify_workspace_access,
            return_value={"workspace_id": _WS_UUID, "user_id": _OWNER, "is_home": False},
        ),
    )
    monkeypatch.setattr(
        sched_mod,
        "list_all_user_automations",
        create_autospec(sched_mod.list_all_user_automations, return_value=[]),
    )
    monkeypatch.setattr(
        company_auto_db,
        "resolve_automation_scope_for_write",
        create_autospec(company_auto_db.resolve_automation_scope_for_write, return_value="personal"),
    )
    monkeypatch.setattr(
        rd_svc,
        "validate_report_profile_change",
        create_autospec(rd_svc.validate_report_profile_change, return_value=change_rest),
    )
    monkeypatch.setattr(
        server_auto_db,
        "create_automation",
        create_autospec(
            server_auto_db.create_automation,
            return_value={
                "automation_id": _AUTO_UUID,
                "user_id": _OWNER,
                "name": "Route Err Task",
                "scope": "personal",
                "config_version": 2,
                "instruction": _DAILY_FILING_INST,
                "metadata": dict(change_rest.after_metadata),
            },
        ),
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        sched_res = await sched_mod.schedule_dalton_automation_helper(
            user_id=_OWNER,
            workspace_id=_WS_UUID,
            name="dalton-r5-route-err",
            spec={
                "action": "upsert",
                "trigger_type": "cron",
                "cron_expression": "0 8 * * *",
                "instruction": _DAILY_FILING_INST,
                "metadata": dict(change_rest.after_metadata),
            },
            via_rest=True,
        )
    assert sched_res["ok"] is True
    assert sched_res["route_warning"] is not None and "RuntimeError" in sched_res["route_warning"]
    assert "route_warning_error" in stored_history[0]["report_delivery_audit"]
    assert "route_warning evaluation failed" in caplog.text


@pytest.mark.asyncio
async def test_sync_owner_group_chat_settings_always_includes_workspace_id() -> None:
    """Group chat entries written without a workspace must include ``workspace_id: None`` for PUT schema validation."""
    import psycopg

    from src.server.services.channel_settings import _App

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.fetchone.return_value = None
    executed: list[tuple[str, Any]] = []

    async def _execute(sql: object, params: object = None) -> None:
        executed.append((" ".join(str(sql).split()), params))

    cursor.execute.side_effect = _execute

    @asynccontextmanager
    async def _cur_cm(*_args: Any, **_kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = MagicMock()
    conn.cursor = _cur_cm
    conn.transaction = _tx_cm

    await rd_svc.sync_owner_group_chat_settings(
        _OWNER,
        [
            {"platform": "discord", "chat_address": "discord:1481256083589697566", "chat_name": "Reports"},
            {"platform": "feishu", "chat_address": "feishu:oc_g1", "chat_name": "Team", "workspace_id": _WS_UUID},
        ],
        conn=conn,
    )

    inserts = [p for sql, p in executed if "INSERT INTO dalton_channel_user_settings" in sql]
    assert len(inserts) == 1
    apps = json.loads(inserts[0][2])
    assert apps["discord"]["chats"]["discord:1481256083589697566"] == {
        "name": "Reports",
        "workspace_id": None,
    }
    assert apps["feishu"]["chats"]["feishu:oc_g1"] == {
        "name": "Team",
        "workspace_id": _WS_UUID,
    }
    # Both entries validate cleanly against the PUT settings _App schema
    _App.model_validate(apps["discord"])
    _App.model_validate(apps["feishu"])


async def _fake_async_return(val: Any) -> Any:
    return val
