"""Unit tests for External Member Permissions Batch 1 (backend guards, monitors, sharing, threads)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import create_autospec

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg import AsyncConnection, AsyncCursor

from src.config import settings as app_settings
from src.dalton import access_tiers, auth_guards, invites
from src.dalton import settings as dalton_settings
from src.dalton import sharing as dalton_sharing
from src.dalton.auth import roles as auth_roles
from src.dalton.app import channels_router as dalton_channels_router
from src.dalton.app import router as dalton_router
from src.dalton.capabilities import (
    CORE_CAPABILITY,
    DALTON_ROUTE_CAPABILITY_REGISTRY,
    CapabilityId,
)
from src.dalton.monitors import db as monitor_db
from src.dalton.monitors import permissions as monitor_perms
from src.dalton.monitors import service as monitor_service
from src.dalton.monitors.grants import MonitorGrant
from src.dalton.monitors.models import (
    MonitorRecord,
    PublishMonitorRequest,
    RepublishMonitorRequest,
    UpdateMonitorRequest,
)
from src.server.app import share_links as share_links_app
from src.server.app.artifacts import comments as artifact_comments_app
from src.server.app.artifacts import records as artifact_records_app
from src.server.app.artifacts.models import (
    AddCommentMessageRequest,
    AudienceRequest,
    BindRequest,
    CommentAnchor,
    CreateCommentRequest,
    KeptRequest,
    MarkCommentReadRequest,
    UpdateCommentRequest,
)
from src.server.app.threads import crud as threads_crud
from src.server.app.threads import messaging as threads_messaging
from src.server.app.threads import sharing as threads_sharing
from src.server.database import artifacts as artifacts_db
from src.server.database import conversation as conv_db
from src.server.database import share_links as share_links_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.artifacts import versions as artifacts_versions_db
from src.server.database.artifacts import Artifact
from src.server.database.artifacts import records as artifacts_records_db
from src.server.database.artifacts.comments import Comment, CommentCounts, CommentMessage
from src.server.database.home_workspace import HOME_STAND_IN_KEY
from src.server.database.shared_workspaces import HOLDER_PREFIX
from src.server.models.conversation import SharePermissions, ThreadShareRequest
from src.server.services import policy as policy_mod
from src.server.services import share_audience
from src.server.services.shared_workspaces import access as shared_ws_access
from src.server.services.shared_workspaces.access import Access

NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)
ARTIFACT_ID = "11111111-1111-1111-1111-111111111111"
MONITOR_ID = "22222222-2222-2222-2222-222222222222"
WS_ID = "33333333-3333-3333-3333-333333333333"
THREAD_ID = "44444444-4444-4444-4444-444444444444"
COMMENT_ID = "55555555-5555-5555-5555-555555555555"


def _make_artifact(**overrides: Any) -> Artifact:
    base: dict[str, Any] = {
        "artifact_id": ARTIFACT_ID,
        "code": "abcdef123456",
        "workspace_id": WS_ID,
        "path": "report.html",
        "shared_at": None,
        "keep_updated": False,
        "kept_at": None,
        "latest_seq": 1,
        "shared_seq": 0,
        "files": None,
        "left_out_files": (),
        "bound_thread_id": None,
        "created_thread_id": None,
        "created_at": NOW,
        "legacy_refused": None,
        "revision": 1,
        "audience": None,
    }
    base.update(overrides)
    return Artifact(**base)


def _make_monitor(**overrides: Any) -> MonitorRecord:
    base: dict[str, Any] = {
        "monitor_id": MONITOR_ID,
        "slug": "macro-board",
        "artifact_id": ARTIFACT_ID,
        "artifact_code": "abcdef123456",
        "workspace_id": WS_ID,
        "entry_path": "report.html",
        "owner_user_id": "user-1",
        "owner_name": "User One",
        "owner_email": "user1@example.com",
        "title": "Macro Board",
        "category": "overview",
        "sort_order": 0,
        "visibility": "own",
        "member_user_ids": (),
        "published_seq": 1,
        "keep_updated": False,
        "source_kind": "workspace",
        "source_ref": f"{WS_ID}:report.html",
        "acl_revision": 1,
        "last_publish_error": None,
        "last_publish_failed_at": None,
        "data_as_of": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return MonitorRecord(**base)


def _make_comment(**overrides: Any) -> Comment:
    msg = CommentMessage(
        message_id="66666666-6666-6666-6666-666666666666",
        seq=1,
        author="user",
        body="Initial note",
        sent_at=None,
        request_key=None,
        thread_id=None,
        created_at=NOW,
        written_by="user-1",
    )
    base: dict[str, Any] = {
        "comment_id": COMMENT_ID,
        "artifact_id": ARTIFACT_ID,
        "number": 1,
        "path": "report.html",
        "file_sha256": None,
        "title": "Report",
        "anchor": {"kind": "element", "path": "#summary", "label": "Summary"},
        "status": "open",
        "delivery": "note",
        "thread_id": None,
        "thread_title": None,
        "thread_archived": False,
        "created_at": NOW,
        "updated_at": NOW,
        "resolved_at": None,
        "read_through": 0,
        "unread": 0,
        "messages": (msg,),
    }
    base.update(overrides)
    return Comment(**base)


# ---------------------------------------------------------------------------
# 1. Unified auth_guards & Structured 403 Payload Serialization
# ---------------------------------------------------------------------------


def test_structured_forbidden_detail_json_and_string_compat() -> None:
    app = FastAPI()

    @app.get("/test-ext")
    async def _ext_route() -> None:
        auth_guards.raise_external_member_forbidden("外部成员无权执行此操作")

    @app.get("/test-adm")
    async def _adm_route() -> None:
        auth_guards.raise_admin_required("仅管理员可执行此操作")

    client = TestClient(app)
    r_ext = client.get("/test-ext")
    assert r_ext.status_code == 403
    assert r_ext.json() == {
        "detail": {
            "code": "external_member_forbidden",
            "message": "外部成员无权执行此操作",
        }
    }

    r_adm = client.get("/test-adm")
    assert r_adm.status_code == 403
    assert r_adm.json() == {
        "detail": {
            "code": "admin_required",
            "message": "仅管理员可执行此操作",
        }
    }

    detail = auth_guards.forbidden_detail("external_member_forbidden", "外部成员不能创建公开分享链接")
    assert isinstance(detail, dict)
    assert detail["code"] == "external_member_forbidden"
    assert detail["message"] == "外部成员不能创建公开分享链接"
    assert detail == "外部成员不能创建公开分享链接"
    assert detail == {
        "code": "external_member_forbidden",
        "message": "外部成员不能创建公开分享链接",
    }


# ---------------------------------------------------------------------------
# 2. Single-User OSS Mode, Auth Mode Semantics & Capability Registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_user_oss_mode_short_circuit_and_credentials_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")
    monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    monkeypatch.delenv("DALTON_ALLOWED_EMAILS", raising=False)

    assert dalton_settings.get_dalton_auth_mode() == "oss"
    assert dalton_settings.is_dalton_single_user_oss_mode() is True
    assert dalton_settings.is_dalton_multiuser_auth_enabled() is False

    # Short-circuit to 'admin' without hitting DB
    get_user_spy = create_autospec(user_db.get_user)
    monkeypatch.setattr(user_db, "get_user", get_user_spy)
    assert await access_tiers.get_user_tier("local-dev-user") == "admin"
    assert await access_tiers.classify_owner_tier("local-dev-user") == "admin"
    assert access_tiers.classify_owner_tier_sync("local-dev-user") == "admin"
    assert await access_tiers.classify_owner_tiers_batch(["local-dev-user", "u2"]) == {
        "local-dev-user": "admin",
        "u2": "admin",
    }
    get_user_spy.assert_not_called()

    # /api/v1/dalton/credentials/status in single-user oss mode
    status_resp = await dalton_router.get_team_credentials_status_endpoint(
        user_id="local-dev-user"
    )
    assert status_resp["auth_mode"] == "oss"
    assert status_resp["caller_tier"] == "admin"
    assert status_resp["can_invite"] is False

    # Invites disabled in single-user oss mode
    with pytest.raises(HTTPException) as exc_inv:
        await invites.require_pm_for_invites("local-dev-user")
    assert exc_inv.value.status_code == 403
    assert exc_inv.value.detail["code"] == "invites_disabled_in_oss"


@pytest.mark.asyncio
async def test_local_mode_admin_check_ignores_pm_user_ids_and_structured_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.auth import roles as auth_roles

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_PM_USER_IDS", "user-in-env-pm")

    role_spy = create_autospec(auth_roles.get_user_role, return_value="external")
    monkeypatch.setattr(auth_roles, "get_user_role", role_spy)
    # Even with include_config_defaults=True, local mode only trusts dalton_user_roles
    assert (
        await access_tiers.is_admin_user("user-in-env-pm", include_config_defaults=True)
        is False
    )
    assert await access_tiers.get_user_tier("user-in-env-pm") == "external"

    with pytest.raises(HTTPException) as exc_inv:
        await invites.require_pm_for_invites("user-in-env-pm")
    assert exc_inv.value.status_code == 403
    assert exc_inv.value.detail["code"] == "external_member_forbidden"

    # Internal member cannot create internal/admin invites in local mode
    role_spy.return_value = "internal"
    with pytest.raises(HTTPException) as exc_tier:
        await invites.create_invite_for_pm("int-1", tier="internal")
    assert exc_tier.value.status_code == 403
    assert exc_tier.value.detail["code"] == "admin_required"

    # Non-admin blocked on Discord and Weixin admin endpoints with structured admin_required
    monkeypatch.setattr(
        dalton_channels_router,
        "is_admin_user",
        create_autospec(dalton_channels_router.is_admin_user, return_value=False),
    )
    with pytest.raises(HTTPException) as exc_wx:
        await dalton_channels_router._require_weixin_admin("int-1")
    assert exc_wx.value.status_code == 403
    assert exc_wx.value.detail["code"] == "admin_required"


def test_capability_registry_decision_11() -> None:
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("router.py", "get_workspace_settings_endpoint")]
        is CORE_CAPABILITY
    )
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("router.py", "update_workspace_settings_endpoint")]
        is CORE_CAPABILITY
    )
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("router.py", "get_user_sharing_settings_endpoint")]
        is CORE_CAPABILITY
    )
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("router.py", "update_user_sharing_settings_endpoint")]
        == CapabilityId.COLLEAGUE_SHARING
    )
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("router.py", "get_user_settings_endpoint")]
        == CapabilityId.CONVERSATION_ARCHIVE
    )
    assert (
        DALTON_ROUTE_CAPABILITY_REGISTRY[("router.py", "update_user_settings_endpoint")]
        == CapabilityId.CONVERSATION_ARCHIVE
    )


# ---------------------------------------------------------------------------
# 3. Upstream Platform Mode & DALTON_ENABLED=false Regressions (§4.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_mode_artifact_audience_kept_comments_binding_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "platform")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    assert dalton_settings.is_dalton_multiuser_auth_enabled() is False

    art = _make_artifact(shared_at=NOW, shared_seq=1, latest_seq=1)
    reached_spy = create_autospec(
        artifact_records_app.reached,
        return_value=(art, Access("owner-1")),
    )
    monkeypatch.setattr(artifact_records_app, "reached", reached_spy)

    async def _fake_changed(owner_id: str, a: Artifact, viewer: str) -> Any:
        return await artifact_records_app._response(a, owner_id, viewer)

    monkeypatch.setattr(
        artifact_records_app,
        "_changed",
        create_autospec(
            artifact_records_app._changed,
            side_effect=_fake_changed,
        ),
    )
    monkeypatch.setattr(
        artifact_records_app.activity,
        "record",
        create_autospec(artifact_records_app.activity.record),
    )
    monkeypatch.setattr(
        artifacts_db,
        "comment_counts",
        create_autospec(
            artifacts_db.comment_counts,
            return_value=CommentCounts(open=0, unread=0),
        ),
    )
    monkeypatch.setattr(
        artifact_records_app,
        "offered",
        create_autospec(artifact_records_app.offered, return_value=["public", "organization"]),
    )
    monkeypatch.setattr(
        artifacts_db,
        "set_audience",
        create_autospec(
            artifacts_db.set_audience,
            side_effect=lambda **kw: _make_artifact(
                shared_at=NOW,
                shared_seq=1,
                audience=kw.get("audience"),
            ),
        ),
    )

    # 1) Platform mode: org_member() satisfied -> audience='organization' returns 200
    monkeypatch.setattr(
        share_audience,
        "org_member",
        create_autospec(share_audience.org_member, return_value="org-1"),
    )
    res_org = await artifact_records_app.set_audience(
        ARTIFACT_ID, AudienceRequest(audience="organization"), x_user_id="owner-1"
    )
    assert res_org.audience == "organization"

    # 2) Platform mode: personal workspace owner with PUBLIC_LINKS -> audience='public' returns 200
    org_art = _make_artifact(shared_at=NOW, shared_seq=1, audience="organization")
    reached_spy.return_value = (org_art, Access("owner-1"))
    monkeypatch.setattr(
        share_audience,
        "require_policy",
        create_autospec(share_audience.require_policy, return_value=policy_mod.ALLOW_ALL),
    )
    res_pub = await artifact_records_app.set_audience(
        ARTIFACT_ID, AudienceRequest(audience="public"), x_user_id="owner-1"
    )
    assert res_pub.audience == "public"

    # 3) Platform mode: shared workspace (is_holder(owner_id) is True) switching organization -> public
    # hits refuse_public_link_of and returns 403 with detail.type == 'shared_members_only'
    shared_holder = f"{HOLDER_PREFIX}99999999-9999-9999-9999-999999999999"
    reached_spy.return_value = (org_art, Access(shared_holder))
    with pytest.raises(HTTPException) as exc_shared_pub:
        await artifact_records_app.set_audience(
            ARTIFACT_ID, AudienceRequest(audience="public"), x_user_id="member-1"
        )
    assert exc_shared_pub.value.status_code == 403
    assert exc_shared_pub.value.detail["type"] == "shared_members_only"

    # 4) Platform mode: comments & binding succeed without hitting Dalton role guards
    monkeypatch.setattr(
        artifact_comments_app,
        "reached",
        create_autospec(
            artifact_comments_app.reached,
            return_value=(art, Access("owner-1")),
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "create_comment",
        create_autospec(artifacts_db.create_comment, return_value=_make_comment()),
    )
    monkeypatch.setattr(
        artifact_comments_app,
        "publish_comment_change",
        create_autospec(artifact_comments_app.publish_comment_change),
    )
    created_c = await artifact_comments_app.create_comment(
        ARTIFACT_ID,
        CreateCommentRequest(
            anchor=CommentAnchor(kind="element", path="#summary", label="Summary"),
            body="hello",
        ),
        x_user_id="owner-1",
    )
    assert created_c.comment_id == COMMENT_ID


@pytest.mark.asyncio
async def test_dalton_disabled_list_shared_and_unshare_use_upstream_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_enabled",
        create_autospec(dalton_settings.is_dalton_enabled, return_value=False),
    )
    executed_sqls: list[str] = []

    cur = create_autospec(AsyncCursor, instance=True)

    async def _exec(sql: str, params: Any = None) -> None:
        executed_sqls.append(sql)

    cur.execute.side_effect = _exec
    cur.fetchall.return_value = []

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    monkeypatch.setattr(share_links_db, "get_db_connection", _conn_cm)
    await share_links_db.list_shared(WS_ID)
    assert len(executed_sqls) == 1
    assert "dalton_monitors" not in executed_sqls[0]

    # POST /artifacts/{id}/unshare when is_dalton_enabled() is False calls artifacts_db.unshare
    art = _make_artifact(shared_at=None)
    monkeypatch.setattr(
        artifact_records_app,
        "reached",
        create_autospec(
            artifact_records_app.reached,
            return_value=(art, Access("owner-1")),
        ),
    )
    unshare_spy = create_autospec(artifacts_db.unshare, return_value=art)
    dalton_unshare_spy = create_autospec(
        artifacts_db.unshare_artifact_and_downgrade_public_monitor
    )
    monkeypatch.setattr(artifacts_db, "unshare", unshare_spy)
    monkeypatch.setattr(
        artifacts_db,
        "unshare_artifact_and_downgrade_public_monitor",
        dalton_unshare_spy,
    )
    monkeypatch.setattr(
        artifact_records_app,
        "_changed",
        create_autospec(artifact_records_app._changed, return_value=None),
    )
    await artifact_records_app.unshare_artifact(ARTIFACT_ID, x_user_id="owner-1")
    unshare_spy.assert_awaited_once()
    dalton_unshare_spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. Audience, Kept (4 cases), Comments & Binding in Supabase / Local / OSS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["supabase", "local", "oss"])
async def test_audience_organization_unavailable_across_all_dalton_modes(
    auth_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)

    monkeypatch.setattr(
        share_links_app,
        "get_user_tier",
        create_autospec(share_links_app.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        artifact_records_app,
        "reached",
        create_autospec(
            artifact_records_app.reached,
            return_value=(_make_artifact(shared_at=NOW, shared_seq=1), Access("adm-1")),
        ),
    )

    # When publishing switch is off, PUT /audience returns 403 artifact_publishing_disabled
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    with pytest.raises(HTTPException) as exc_off:
        await artifact_records_app.set_audience(
            ARTIFACT_ID,
            AudienceRequest(audience="organization"),
            x_user_id="adm-1",
        )
    assert exc_off.value.status_code == 403
    assert exc_off.value.detail == {"code": "artifact_publishing_disabled"}

    # When publishing switch is on, choosing 'organization' returns 403 audience_unavailable
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    with pytest.raises(HTTPException) as exc_org:
        await artifact_records_app.set_audience(
            ARTIFACT_ID,
            AudienceRequest(audience="organization"),
            x_user_id="adm-1",
        )
    assert exc_org.value.status_code == 403
    assert exc_org.value.detail["type"] == "audience_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["supabase", "local"])
async def test_set_artifact_kept_four_cases(
    auth_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)

    tiers = {"ext-1": "external", "int-1": "internal", "adm-1": "admin"}
    monkeypatch.setattr(
        share_links_app,
        "get_user_tier",
        create_autospec(share_links_app.get_user_tier, side_effect=lambda u: tiers[u]),
    )
    stop_spy = create_autospec(
        artifacts_db.stop_keeping,
        return_value=_make_artifact(kept_at=None),
    )
    monkeypatch.setattr(artifacts_db, "stop_keeping", stop_spy)
    monkeypatch.setattr(
        artifact_records_app,
        "_changed",
        create_autospec(artifact_records_app._changed, return_value=None),
    )

    # Case 1: external + private artifact + kept=True -> 403 external_member_forbidden
    priv_art = _make_artifact(shared_at=None)
    reached_spy = create_autospec(
        artifact_records_app.reached,
        return_value=(priv_art, Access("ext-1")),
    )
    monkeypatch.setattr(artifact_records_app, "reached", reached_spy)
    with pytest.raises(HTTPException) as exc1:
        await artifact_records_app.set_artifact_kept(
            ARTIFACT_ID, KeptRequest(kept=True), x_user_id="ext-1"
        )
    assert exc1.value.status_code == 403
    assert exc1.value.detail["code"] == "external_member_forbidden"

    # Case 2: external (or demoted external) + shared artifact + kept=True -> 403 external_member_forbidden
    shared_art = _make_artifact(shared_at=NOW, shared_seq=1)
    reached_spy.return_value = (shared_art, Access("ext-1"))
    with pytest.raises(HTTPException) as exc2:
        await artifact_records_app.set_artifact_kept(
            ARTIFACT_ID, KeptRequest(kept=True), x_user_id="ext-1"
        )
    assert exc2.value.status_code == 403
    assert exc2.value.detail["code"] == "external_member_forbidden"

    # Case 3: internal + shared artifact + kept=True -> 403 admin_required
    reached_spy.return_value = (shared_art, Access("int-1"))
    with pytest.raises(HTTPException) as exc3:
        await artifact_records_app.set_artifact_kept(
            ARTIFACT_ID, KeptRequest(kept=True), x_user_id="int-1"
        )
    assert exc3.value.status_code == 403
    assert exc3.value.detail["code"] == "admin_required"

    # Case 4: external + kept=False -> 200 (skips member tier guards)
    reached_spy.return_value = (shared_art, Access("ext-1"))
    await artifact_records_app.set_artifact_kept(
        ARTIFACT_ID, KeptRequest(kept=False), x_user_id="ext-1"
    )
    stop_spy.assert_awaited_once_with(user_id="ext-1", artifact_id=ARTIFACT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["supabase", "local"])
async def test_comments_and_binding_external_guards(
    auth_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    monkeypatch.setattr(
        share_links_app,
        "get_user_tier",
        create_autospec(share_links_app.get_user_tier, return_value="external"),
    )

    # Write routes rejected with 403 external_member_forbidden
    with pytest.raises(HTTPException) as exc_create:
        await artifact_comments_app.create_comment(
            ARTIFACT_ID,
            CreateCommentRequest(
                anchor=CommentAnchor(kind="element", path="#summary", label="Summary"),
                body="test",
            ),
            x_user_id="ext-1",
        )
    assert exc_create.value.status_code == 403
    assert exc_create.value.detail["code"] == "external_member_forbidden"

    with pytest.raises(HTTPException) as exc_msg:
        await artifact_comments_app.add_comment_message(
            ARTIFACT_ID,
            COMMENT_ID,
            AddCommentMessageRequest(body="reply"),
            x_user_id="ext-1",
        )
    assert exc_msg.value.status_code == 403
    assert exc_msg.value.detail["code"] == "external_member_forbidden"

    with pytest.raises(HTTPException) as exc_upd:
        await artifact_comments_app.update_comment(
            ARTIFACT_ID,
            COMMENT_ID,
            UpdateCommentRequest(status="resolved"),
            x_user_id="ext-1",
        )
    assert exc_upd.value.status_code == 403
    assert exc_upd.value.detail["code"] == "external_member_forbidden"

    with pytest.raises(HTTPException) as exc_bind:
        await artifact_records_app.bind_artifact(
            ARTIFACT_ID,
            BindRequest(thread_id=THREAD_ID),
            x_user_id="ext-1",
        )
    assert exc_bind.value.status_code == 403
    assert exc_bind.value.detail["code"] == "external_member_forbidden"

    with pytest.raises(HTTPException) as exc_unbind:
        await artifact_records_app.unbind_artifact(ARTIFACT_ID, x_user_id="ext-1")
    assert exc_unbind.value.status_code == 403
    assert exc_unbind.value.detail["code"] == "external_member_forbidden"

    # Read, mark read, and delete own comment remain allowed for external owner
    art = _make_artifact()
    comment = _make_comment()
    monkeypatch.setattr(
        artifact_comments_app,
        "reached",
        create_autospec(
            artifact_comments_app.reached,
            return_value=(art, Access("ext-1")),
        ),
    )
    monkeypatch.setattr(
        artifacts_db,
        "list_comments",
        create_autospec(artifacts_db.list_comments, return_value=[comment]),
    )
    monkeypatch.setattr(
        artifacts_db,
        "mark_comment_read",
        create_autospec(artifacts_db.mark_comment_read, return_value=(comment, True)),
    )
    monkeypatch.setattr(
        artifacts_db,
        "delete_comment",
        create_autospec(artifacts_db.delete_comment, return_value=(True, None)),
    )
    monkeypatch.setattr(
        artifact_comments_app,
        "publish_comment_change",
        create_autospec(artifact_comments_app.publish_comment_change),
    )

    listed = await artifact_comments_app.list_comments(ARTIFACT_ID, x_user_id="ext-1")
    assert len(listed.comments) == 1

    marked = await artifact_comments_app.mark_comment_read(
        ARTIFACT_ID,
        COMMENT_ID,
        MarkCommentReadRequest(through=1),
        x_user_id="ext-1",
    )
    assert marked.comment_id == COMMENT_ID

    del_resp = await artifact_comments_app.delete_comment(
        ARTIFACT_ID, COMMENT_ID, x_user_id="ext-1"
    )
    assert del_resp.status_code == 204


# ---------------------------------------------------------------------------
# 5. Monitors: External 'own' + keep_updated (R8 #2) & Demoted External Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_external_member_own_monitor_keep_updated_and_demoted_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    tiers = {"ext-owner": "external", "adm-1": "admin"}
    for mod in (monitor_service, monitor_perms, access_tiers):
        monkeypatch.setattr(
            mod,
            "get_user_tier",
            create_autospec(mod.get_user_tier, side_effect=lambda u: tiers[u]),
        )

    # 1) External member can validate visibility='own', rejected on 'company'/'members'/'public'
    assert (
        await monitor_perms.validate_publish_permissions(
            caller_id="ext-owner", visibility="own", member_user_ids=()
        )
        == []
    )
    for bad_vis in ("members", "company", "public"):
        with pytest.raises(HTTPException) as exc_vis:
            await monitor_perms.validate_publish_permissions(
                caller_id="ext-owner",
                visibility=bad_vis,  # type: ignore[arg-type]
                member_user_ids=("adm-1",),
            )
        assert exc_vis.value.status_code == 403
        assert exc_vis.value.detail["code"] == "external_member_forbidden"

    # 2) R8 #2: External owner CAN enable keep_updated=True on their own 'own' monitor
    own_mon = _make_monitor(
        owner_user_id="ext-owner", visibility="own", keep_updated=False
    )
    mon_by_slug_spy = create_autospec(monitor_db.get_monitor_by_slug, return_value=own_mon)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        mon_by_slug_spy,
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "ext-owner", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "update_monitor_settings_tx",
        create_autospec(
            monitor_db.update_monitor_settings_tx,
            return_value=_make_monitor(
                owner_user_id="ext-owner", visibility="own", keep_updated=True
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    monkeypatch.setattr(
        monitor_service,
        "mint_monitor_grant",
        create_autospec(
            monitor_service.mint_monitor_grant,
            return_value=MonitorGrant(
                token="tok-own",
                monitor_id=MONITOR_ID,
                user_id="ext-owner",
                acl_revision=1,
                expires_at=9999999999,
            ),
        ),
    )
    upd_own = await monitor_service.update_monitor_settings(
        own_mon.slug,
        UpdateMonitorRequest(keep_updated=True),
        caller_id="ext-owner",
    )
    assert upd_own["monitor"]["visibility"] == "own"
    assert upd_own["monitor"]["keep_updated"] is True

    # 3) Demoted external owner with existing 'company' monitor:
    company_mon = _make_monitor(
        owner_user_id="ext-owner",
        visibility="company",
        keep_updated=True,
        published_seq=2,
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=company_mon),
    )
    mon_by_slug_spy.return_value = company_mon

    # 3a) get_monitor_by_artifact allows demoted external owner to read metadata with can_manage=True
    by_art = await monitor_service.get_monitor_by_artifact(
        ARTIFACT_ID, viewer_id="ext-owner"
    )
    assert by_art is not None
    assert by_art["can_manage"] is True
    assert by_art["visibility"] == "company"

    # 3b) POST /monitors on existing 'company' monitor rejected with 403 external_member_forbidden
    # even when payload passes visibility='own' (must downgrade via PATCH first)
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "ext-owner",
                "status": "running",
                "name": "ws",
                "config": {},
            },
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_normalize_workspace_entry_path",
        create_autospec(monitor_service._normalize_workspace_entry_path, return_value="report.html"),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    with pytest.raises(HTTPException) as exc_pub_before_downgrade:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(
                workspace_id=WS_ID,
                path="report.html",
                visibility="own",
                keep_updated=True,
            ),
            caller_id="ext-owner",
        )
    assert exc_pub_before_downgrade.value.status_code == 403
    assert exc_pub_before_downgrade.value.detail["code"] == "external_member_forbidden"

    # 3c) Manual POST /monitors/{slug}/republish rejected with 403 and records last_publish_error
    recorded_errors: list[str] = []

    async def _rec_err(mid: str, *, error_message: str, run_id: str | None = None) -> MonitorRecord:
        recorded_errors.append(error_message)
        return _make_monitor(**{**company_mon.__dict__, "last_publish_error": error_message})

    monkeypatch.setattr(
        monitor_db,
        "record_monitor_publish_failure",
        create_autospec(monitor_db.record_monitor_publish_failure, side_effect=_rec_err),
    )
    with pytest.raises(HTTPException) as exc_repub:
        await monitor_service.republish_workspace_monitor(
            company_mon.slug,
            RepublishMonitorRequest(),
            caller_id="ext-owner",
        )
    assert exc_repub.value.status_code == 403
    assert exc_repub.value.detail["code"] == "external_member_forbidden"
    assert recorded_errors[-1] == monitor_perms.EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE

    # 3d) Post-turn auto-update skips cutting version and records last_publish_error
    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(
            monitor_db.list_workspace_auto_update_monitors,
            return_value=[company_mon],
        ),
    )
    cut_spy = create_autospec(monitor_db.commit_monitor_version_tx)
    monkeypatch.setattr(monitor_db, "commit_monitor_version_tx", cut_spy)
    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    cut_spy.assert_not_awaited()
    assert recorded_errors[-1] == monitor_perms.EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE


# ---------------------------------------------------------------------------
# 6. Atomic Unshare & Admin Takeover Unshare
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_unshare_locks_share_links_then_monitors_and_downgrades_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_order: list[str] = []
    cur = create_autospec(AsyncCursor, instance=True)

    async def _exec(sql: str, params: Any = None) -> None:
        if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
            lock_order.append("lock_dalton_monitors")
            cur._row = {"monitor_id": MONITOR_ID, "visibility": "public"}
        elif "UPDATE dalton_monitors" in sql:
            lock_order.append("downgrade_monitor_to_own")
            cur._row = None

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: getattr(cur, "_row", None)

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    async def _lock_owned(_cur: Any, *, user_id: str, artifact_id: str) -> Artifact:
        lock_order.append(f"lock_share_links:{user_id}")
        return _make_artifact(shared_at=NOW, shared_seq=1, revision=1)

    async def _unshare_in_cur(_cur: Any, *, user_id: str, artifact_id: str) -> Artifact:
        lock_order.append(f"unshare_share_links:{user_id}")
        return _make_artifact(shared_at=None, shared_seq=0, revision=2)

    monkeypatch.setattr(artifacts_records_db, "get_db_connection", _conn_cm)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(artifacts_records_db.lock_owned, side_effect=_lock_owned),
    )
    monkeypatch.setattr(
        artifacts_records_db,
        "unshare_in_cur",
        create_autospec(artifacts_records_db.unshare_in_cur, side_effect=_unshare_in_cur),
    )

    # Original file owner user-a unshares after admin-b took over the public monitor
    res = await artifacts_records_db.unshare_artifact_and_downgrade_public_monitor(
        user_id="user-a", artifact_id=ARTIFACT_ID
    )
    assert res.shared is False
    assert res.revision == 2
    assert lock_order == [
        "lock_share_links:user-a",
        "lock_dalton_monitors",
        "downgrade_monitor_to_own",
        "unshare_share_links:user-a",
    ]


# ---------------------------------------------------------------------------
# 7. Thread Access (Table A & R8 #3) & Thread Sharing (Table B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["supabase", "local"])
async def test_thread_access_external_isolation_and_home_classification(
    auth_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)

    tiers = {
        "ext-1": "external",
        "int-1": "internal",
        "adm-1": "admin",
        "unk-1": "unknown",
    }
    monkeypatch.setattr(
        dalton_sharing,
        "get_user_tier",
        create_autospec(dalton_sharing.get_user_tier, side_effect=lambda u, **kw: tiers[u]),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "classify_owner_tier",
        create_autospec(
            dalton_sharing.classify_owner_tier, side_effect=lambda u, **kw: tiers[u]
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_team_reader",
        create_autospec(dalton_sharing.require_team_reader, return_value="ok"),
    )

    for sharing_on in (False, True):
        monkeypatch.setattr(dalton_sharing, "_sharing_auth_enabled", lambda _on=sharing_on: _on)
        monkeypatch.setattr(dalton_sharing, "is_colleague_dispatch_enabled", lambda: True)

        # 1) External member accessing own historical thread in another user's workspace -> 404
        with pytest.raises(HTTPException) as exc_ext_hist:
            await dalton_sharing.require_thread_access(
                THREAD_ID,
                "ext-1",
                channel="web",
                need="read",
                owner_id="ext-1",
                workspace_owner_id="int-1",
                workspace_id={"workspace_id": WS_ID, "user_id": "int-1", "config": {}},
            )
        assert exc_ext_hist.value.status_code == 404

        # 2) Internal/admin accessing foreign Home stand-in -> 404
        stand_in_ws = {
            "workspace_id": WS_ID,
            "user_id": "int-1",
            "is_home": False,
            "config": {HOME_STAND_IN_KEY: True},
        }
        with pytest.raises(HTTPException) as exc_stand_in:
            await dalton_sharing.require_thread_access(
                THREAD_ID,
                "adm-1",
                channel="web",
                need="read",
                owner_id="int-1",
                workspace_owner_id="int-1",
                workspace_id=stand_in_ws,
            )
        assert exc_stand_in.value.status_code == 404

        # 3) Internal/admin accessing external or unknown owner's personal Home -> 404
        for ext_or_unk in ("ext-1", "unk-1"):
            ext_home_ws = {
                "workspace_id": WS_ID,
                "user_id": ext_or_unk,
                "is_home": True,
                "config": {"kind": "home"},
            }
            with pytest.raises(HTTPException) as exc_ext_home:
                await dalton_sharing.require_thread_access(
                    THREAD_ID,
                    "adm-1",
                    channel="web",
                    need="read",
                    owner_id=ext_or_unk,
                    workspace_owner_id=ext_or_unk,
                    workspace_id=ext_home_ws,
                )
            assert exc_ext_home.value.status_code == 404

        # 4) Internal/admin accessing internal/admin owner's personal Home -> 403
        int_home_ws = {
            "workspace_id": WS_ID,
            "user_id": "int-1",
            "is_home": True,
            "config": {"kind": "home"},
        }
        with pytest.raises(HTTPException) as exc_int_home:
            await dalton_sharing.require_thread_access(
                THREAD_ID,
                "adm-1",
                channel="web",
                need="read",
                owner_id="int-1",
                workspace_owner_id="int-1",
                workspace_id=int_home_ws,
            )
        assert exc_int_home.value.status_code == 403

    # 5) colleague_sharing=off: internal member reading foreign regular workspace thread
    # or own historical thread in foreign regular workspace -> 403
    monkeypatch.setattr(dalton_sharing, "_sharing_auth_enabled", lambda: False)
    reg_ws = {
        "workspace_id": WS_ID,
        "user_id": "adm-1",
        "is_home": False,
        "config": {},
    }
    for t_owner in ("adm-1", "int-1"):
        with pytest.raises(HTTPException) as exc_off_reg:
            await dalton_sharing.require_thread_access(
                THREAD_ID,
                "int-1",
                channel="web",
                need="read",
                owner_id=t_owner,
                workspace_owner_id="adm-1",
                workspace_id=reg_ws,
            )
        assert exc_off_reg.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["supabase", "local"])
async def test_r8_3_explicit_distinction_of_hidden_workspace_write_callers(
    auth_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8 review item #3: explicitly contrast the two `need='write'` callers under `web_access='hidden'`.

    1) When `colleague_sharing=on` and workspace `web_access='hidden'`:
       - Non-owner colleague C (`caller == 'colleague-c'`) attempting `need='write'` on a thread
         enters `require_workspace_access` (`sharing.py:1549 -> 1342`) and receives HTTP 404 first.
       - When workspace `web_access='rw'`, colleague C passes `require_workspace_access` and hits
         `sharing.py:1565-1569`, receiving HTTP 403 (`该对话由同事发起，仅创建者可继续提问`).
    2) When `colleague_sharing=on` and workspace `web_access='hidden'`:
       - Workspace owner B (`caller == 'ws-owner-b'`) attempting `need='write'` on a thread
         initiated by colleague A (`thread_owner == 'colleague-a'`) inside B's workspace
         directly hits `sharing.py:1533-1545` BEFORE `require_workspace_access` and receives
         HTTP 403 (`该对话由同事发起，工作区所有者仅可查看`).
    """
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)
    monkeypatch.setattr(dalton_sharing, "_sharing_auth_enabled", lambda: True)
    monkeypatch.setattr(dalton_sharing, "is_colleague_dispatch_enabled", lambda: True)

    tiers = {
        "colleague-a": "internal",
        "ws-owner-b": "internal",
        "colleague-c": "internal",
    }
    monkeypatch.setattr(
        dalton_sharing,
        "get_user_tier",
        create_autospec(dalton_sharing.get_user_tier, side_effect=lambda u, **kw: tiers[u]),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "classify_owner_tier",
        create_autospec(
            dalton_sharing.classify_owner_tier, side_effect=lambda u, **kw: tiers[u]
        ),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_team_reader",
        create_autospec(dalton_sharing.require_team_reader, return_value="ok"),
    )

    ws_row = {
        "workspace_id": WS_ID,
        "user_id": "ws-owner-b",
        "status": "running",
        "is_home": False,
        "config": {},
    }

    # --- Under web_access='hidden' ---
    ws_sharing_spy = create_autospec(
        dalton_sharing.get_workspace_sharing,
        return_value={"web_access": "hidden", "cos_access": "hidden"},
    )
    monkeypatch.setattr(dalton_sharing, "get_workspace_sharing", ws_sharing_spy)

    # Caller 1: Non-owner colleague C writing to thread in hidden workspace -> 404
    with pytest.raises(HTTPException) as exc_colleague_hidden:
        await dalton_sharing.require_thread_access(
            THREAD_ID,
            "colleague-c",
            channel="web",
            need="write",
            owner_id="colleague-a",
            workspace_owner_id="ws-owner-b",
            workspace_id=ws_row,
        )
    assert exc_colleague_hidden.value.status_code == 404
    assert exc_colleague_hidden.value.detail == "Thread not found"

    # Caller 2: Workspace owner B writing to colleague A's thread in B's hidden workspace -> 403
    with pytest.raises(HTTPException) as exc_owner_hidden:
        await dalton_sharing.require_thread_access(
            THREAD_ID,
            "ws-owner-b",
            channel="web",
            need="write",
            owner_id="colleague-a",
            workspace_owner_id="ws-owner-b",
            workspace_id=ws_row,
        )
    assert exc_owner_hidden.value.status_code == 403
    assert exc_owner_hidden.value.detail == "该对话由同事发起，工作区所有者仅可查看"

    # --- Under web_access='rw' ---
    ws_sharing_spy.return_value = {"web_access": "rw", "cos_access": "rw"}

    # Caller 1 (under rw): Non-owner colleague C passes workspace check and hits :1565-1569 -> 403
    with pytest.raises(HTTPException) as exc_colleague_rw:
        await dalton_sharing.require_thread_access(
            THREAD_ID,
            "colleague-c",
            channel="web",
            need="write",
            owner_id="colleague-a",
            workspace_owner_id="ws-owner-b",
            workspace_id=ws_row,
        )
    assert exc_colleague_rw.value.status_code == 403
    assert exc_colleague_rw.value.detail == "该对话由同事发起，仅创建者可继续提问"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["supabase", "local"])
async def test_five_thread_routes_and_external_stop_sharing_legacy_payload(
    auth_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)

    tiers = {"ext-1": "external", "int-1": "internal", "adm-1": "admin"}
    for mod in (dalton_sharing, threads_sharing, access_tiers):
        monkeypatch.setattr(
            mod,
            "get_user_tier",
            create_autospec(mod.get_user_tier, side_effect=lambda u, **kw: tiers[u]),
        )

    foreign_meta = {
        "user_id": "int-1",
        "thread_user_id": "int-1",
        "workspace_owner_id": "int-1",
        "workspace_id": WS_ID,
    }
    auth_meta_spy = create_autospec(conv_db.get_thread_auth_meta, return_value=foreign_meta)
    owner_id_spy = create_autospec(conv_db.get_thread_owner_id, return_value="int-1")
    monkeypatch.setattr(conv_db, "get_thread_auth_meta", auth_meta_spy)
    monkeypatch.setattr(conv_db, "get_thread_owner_id", owner_id_spy)

    # 1) All 5 thread routes return 404 for external member on foreign thread in both modes
    with pytest.raises(HTTPException) as e_get:
        await threads_crud.get_thread(THREAD_ID, x_user_id="ext-1")
    assert e_get.value.status_code == 404

    fake_replay_rows: Any = type(
        "ReplayRows",
        (),
        {
            "owner_id": "int-1",
            "workspace_owner_id": "int-1",
            "thread": {"workspace_id": WS_ID, "latest_run_id": None},
        },
    )()
    monkeypatch.setattr(
        threads_messaging,
        "get_replay_thread_data",
        create_autospec(
            threads_messaging.get_replay_thread_data, return_value=fake_replay_rows
        ),
    )
    from starlette.requests import Request as StarletteRequest

    ext_replay_req = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/v1/threads/{THREAD_ID}/messages/replay",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
        }
    )
    with pytest.raises(HTTPException) as e_replay:
        await threads_messaging.replay_thread_messages(
            THREAD_ID,
            x_user_id="ext-1",
            request=ext_replay_req,
            source="auto",
            limit=None,
            before_turn=None,
            max_bytes=None,
            min_turns=1,
        )
    assert e_replay.value.status_code == 404

    with pytest.raises(HTTPException) as e_status:
        await threads_messaging.get_thread_status(THREAD_ID, x_user_id="ext-1", fields=None)
    assert e_status.value.status_code == 404

    with pytest.raises(HTTPException) as e_get_share:
        await threads_sharing.get_thread_share(THREAD_ID, x_user_id="ext-1")
    assert e_get_share.value.status_code == 404

    with pytest.raises(HTTPException) as e_post_share:
        await threads_sharing.update_thread_share(
            THREAD_ID,
            ThreadShareRequest(is_shared=False),
            x_user_id="ext-1",
        )
    assert e_post_share.value.status_code == 404

    assert await shared_ws_access._dalton_external_outside(foreign_meta, "ext-1") is True

    # 2) External member stopping sharing on their OWN thread in their OWN workspace
    # with legacy permissions={"audience": "organization", "allow_files": True} -> 200
    own_meta = {
        "user_id": "ext-1",
        "thread_user_id": "ext-1",
        "workspace_owner_id": "ext-1",
        "workspace_id": WS_ID,
    }
    auth_meta_spy.return_value = own_meta
    owner_id_spy.return_value = "ext-1"
    existing_thread = {
        "conversation_thread_id": THREAD_ID,
        "user_id": "ext-1",
        "workspace_owner_id": "ext-1",
        "workspace_id": WS_ID,
        "is_shared": True,
        "share_token": "tok-123",
        "share_permissions": {"allow_files": False, "allow_download": False},
    }
    monkeypatch.setattr(
        threads_sharing,
        "get_thread_by_id",
        create_autospec(threads_sharing.get_thread_by_id, return_value=existing_thread),
    )
    saved_kwargs: dict[str, Any] = {}

    async def _upd_share(tid: str, **kwargs: Any) -> dict[str, Any]:
        saved_kwargs.update(kwargs)
        return {**existing_thread, **kwargs}

    monkeypatch.setattr(
        threads_sharing,
        "update_thread_sharing",
        create_autospec(threads_sharing.update_thread_sharing, side_effect=_upd_share),
    )
    monkeypatch.setattr(
        threads_sharing,
        "offered",
        create_autospec(threads_sharing.offered, return_value=["public"]),
    )

    resp_stop = await threads_sharing.update_thread_share(
        THREAD_ID,
        ThreadShareRequest(
            is_shared=False,
            permissions=SharePermissions(
                audience="organization",
                allow_files=True,
                allow_download=True,
            ),
        ),
        x_user_id="ext-1",
    )
    assert resp_stop.is_shared is False
    assert "share_permissions" not in saved_kwargs
    assert resp_stop.permissions.allow_files is False

    # Enabling sharing (is_shared=True) as external member returns 403 external_member_forbidden
    # for both default public and organization audience payloads (never audience_unavailable)
    for req_payload in (
        ThreadShareRequest(is_shared=True),
        ThreadShareRequest(
            is_shared=True,
            permissions=SharePermissions(audience="organization"),
        ),
    ):
        with pytest.raises(HTTPException) as exc_start_share:
            await threads_sharing.update_thread_share(
                THREAD_ID,
                req_payload,
                x_user_id="ext-1",
            )
        assert exc_start_share.value.status_code == 403
        assert exc_start_share.value.detail["code"] == "external_member_forbidden"


# ---------------------------------------------------------------------------
# 8. Workspace Settings & Sharing Two-Phase Verification (Decision 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_settings_and_sharing_two_phase_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    ws_row = {
        "workspace_id": WS_ID,
        "user_id": "owner-1",
        "status": "running",
        "is_home": False,
        "config": {},
    }
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_row),
    )
    monkeypatch.setattr(
        access_tiers,
        "is_admin_user",
        create_autospec(access_tiers.is_admin_user, return_value=False),
    )

    # Phase 1: Caller fails require_team_reader (e.g. missing from dalton_user_roles in local mode) -> 403
    team_reader_spy = create_autospec(
        dalton_sharing.require_team_reader,
        side_effect=HTTPException(status_code=403, detail="Forbidden"),
    )
    monkeypatch.setattr(dalton_sharing, "require_team_reader", team_reader_spy)
    with pytest.raises(HTTPException) as exc_p1_get:
        await dalton_router.get_workspace_sharing_endpoint(WS_ID, user_id="owner-1")
    assert exc_p1_get.value.status_code == 403

    with pytest.raises(HTTPException) as exc_p1_set:
        await dalton_router.get_workspace_settings_endpoint(WS_ID, user_id="owner-1")
    assert exc_p1_set.value.status_code == 403

    # Phase 1 in supabase mode: missing token identity + user_db.get_user DB error -> 503
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    team_reader_spy.side_effect = HTTPException(
        status_code=503, detail="Team reader verification unavailable"
    )
    with pytest.raises(HTTPException) as exc_p1_503:
        await dalton_router.get_workspace_sharing_endpoint(WS_ID, user_id="owner-1")
    assert exc_p1_503.value.status_code == 503

    # Phase 2: Caller passes require_team_reader, but classify_owner_tier returns 'unknown'
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    team_reader_spy.side_effect = None
    team_reader_spy.return_value = "ok"
    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, return_value="internal"),
    )
    owner_tier_spy = create_autospec(access_tiers.classify_owner_tier, return_value="unknown")
    monkeypatch.setattr(access_tiers, "classify_owner_tier", owner_tier_spy)
    monkeypatch.setattr(
        dalton_sharing,
        "get_workspace_sharing",
        create_autospec(
            dalton_sharing.get_workspace_sharing,
            return_value={
                "workspace_id": WS_ID,
                "cos_access": "ro",
                "web_access": "ro",
                "updated_by": None,
                "updated_at": None,
            },
        ),
    )
    upsert_sharing_spy = create_autospec(dalton_sharing.upsert_workspace_sharing)
    upsert_settings_spy = create_autospec(dalton_sharing.upsert_workspace_settings)
    monkeypatch.setattr(dalton_sharing, "upsert_workspace_sharing", upsert_sharing_spy)
    monkeypatch.setattr(dalton_sharing, "upsert_workspace_settings", upsert_settings_spy)

    # Non-owner gets 404 when owner is 'unknown'
    with pytest.raises(HTTPException) as exc_non_owner_unk:
        await dalton_router.get_workspace_sharing_endpoint(WS_ID, user_id="other-int")
    assert exc_non_owner_unk.value.status_code == 404

    # Owner GET /sharing and GET /settings return 200 with all can_edit* flags False
    sh_unk = await dalton_router.get_workspace_sharing_endpoint(WS_ID, user_id="owner-1")
    assert sh_unk["can_edit"] is False
    assert sh_unk["can_edit_web_access"] is False
    assert sh_unk["can_edit_cos_access"] is False
    assert sh_unk["can_edit_team_memory"] is False

    st_unk = await dalton_router.get_workspace_settings_endpoint(WS_ID, user_id="owner-1")
    assert st_unk["can_edit"] is False
    assert st_unk["can_edit_web_access"] is False
    assert st_unk["can_edit_cos_access"] is False
    assert st_unk["can_edit_team_memory"] is False

    # Owner PUT /sharing (even with only web_access='ro') and PUT /settings return 403 without DB write
    with pytest.raises(HTTPException) as exc_put_sh_unk:
        await dalton_router.update_workspace_sharing_endpoint(
            WS_ID,
            dalton_router.WorkspaceSharingUpdateRequest(web_access="ro"),
            user_id="owner-1",
        )
    assert exc_put_sh_unk.value.status_code == 403
    assert exc_put_sh_unk.value.detail["code"] == "external_member_forbidden"
    upsert_sharing_spy.assert_not_awaited()

    with pytest.raises(HTTPException) as exc_put_st_unk:
        await dalton_router.update_workspace_settings_endpoint(
            WS_ID,
            dalton_router.WorkspaceSettingsUpdateRequest(team_memory_enabled=True),
            user_id="owner-1",
        )
    assert exc_put_st_unk.value.status_code == 403
    assert exc_put_st_unk.value.detail["code"] == "external_member_forbidden"
    upsert_settings_spy.assert_not_awaited()

    # External owner on own non-Home workspace: can_edit_web_access=True, cos/team_memory=False
    owner_tier_spy.return_value = "external"
    sh_ext = await dalton_router.get_workspace_sharing_endpoint(WS_ID, user_id="owner-1")
    assert sh_ext["can_edit"] is True
    assert sh_ext["can_edit_web_access"] is True
    assert sh_ext["can_edit_cos_access"] is False
    assert sh_ext["can_edit_team_memory"] is False
    assert sh_ext["owner_external"] is True

    with pytest.raises(HTTPException) as exc_put_cos_ext:
        await dalton_router.update_workspace_sharing_endpoint(
            WS_ID,
            dalton_router.WorkspaceSharingUpdateRequest(cos_access="ro"),
            user_id="owner-1",
        )
    assert exc_put_cos_ext.value.status_code == 403
    assert exc_put_cos_ext.value.detail["code"] == "external_member_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["local", "supabase"])
@pytest.mark.parametrize("caller_tier", ["internal", "admin"])
async def test_external_owner_workspace_settings_hidden_when_colleague_sharing_disabled(
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
    caller_tier: str,
) -> None:
    """When colleague_sharing is off, non-owner GET/PUT /workspaces/{id}/settings on an
    external-owned workspace with stored web_access='ro' must both return 404 (matching
    /sharing); when colleague_sharing is on, existing 200 GET / 403 PUT behavior is kept.
    """
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", auth_mode)

    ws_row = {
        "workspace_id": WS_ID,
        "user_id": "ext-owner",
        "status": "running",
        "is_home": False,
        "config": {},
    }
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_row),
    )
    monkeypatch.setattr(
        access_tiers,
        "is_admin_user",
        create_autospec(access_tiers.is_admin_user, return_value=(caller_tier == "admin")),
    )
    monkeypatch.setattr(
        access_tiers,
        "get_user_tier",
        create_autospec(access_tiers.get_user_tier, return_value=caller_tier),
    )
    monkeypatch.setattr(
        access_tiers,
        "classify_owner_tier",
        create_autospec(access_tiers.classify_owner_tier, return_value="external"),
    )
    monkeypatch.setattr(
        dalton_sharing,
        "require_team_reader",
        create_autospec(dalton_sharing.require_team_reader, return_value="ok"),
    )
    sharing_spy = create_autospec(
        dalton_sharing.get_workspace_sharing,
        return_value={
            "workspace_id": WS_ID,
            "cos_access": "hidden",
            "web_access": "ro",
            "updated_by": "ext-owner",
            "updated_at": None,
        },
    )
    monkeypatch.setattr(dalton_sharing, "get_workspace_sharing", sharing_spy)

    # 1. colleague_sharing disabled -> GET and PUT /settings both return 404 without reading sharing row
    monkeypatch.setenv("DALTON_CAP_COLLEAGUE_SHARING", "false")
    dalton_settings.get_dalton_config.cache_clear()

    with pytest.raises(HTTPException) as exc_get_off:
        await dalton_router.get_workspace_settings_endpoint(WS_ID, user_id="caller-1")
    assert exc_get_off.value.status_code == 404

    with pytest.raises(HTTPException) as exc_put_off:
        await dalton_router.update_workspace_settings_endpoint(
            WS_ID,
            dalton_router.WorkspaceSettingsUpdateRequest(team_memory_enabled=True),
            user_id="caller-1",
        )
    assert exc_put_off.value.status_code == 404
    sharing_spy.assert_not_awaited()

    # 2. colleague_sharing enabled -> stored web_access='ro' preserves 200 GET and 403 PUT
    monkeypatch.setenv("DALTON_CAP_COLLEAGUE_SHARING", "true")
    dalton_settings.get_dalton_config.cache_clear()

    res_on = await dalton_router.get_workspace_settings_endpoint(WS_ID, user_id="caller-1")
    assert res_on["owner_external"] is True
    assert res_on["can_edit"] is False
    assert res_on["can_edit_web_access"] is False

    with pytest.raises(HTTPException) as exc_put_on:
        await dalton_router.update_workspace_settings_endpoint(
            WS_ID,
            dalton_router.WorkspaceSettingsUpdateRequest(team_memory_enabled=True),
            user_id="caller-1",
        )
    assert exc_put_on.value.status_code == 403
    assert exc_put_on.value.detail["code"] == "external_member_forbidden"
    assert sharing_spy.await_count == 2


# ---------------------------------------------------------------------------
# 9. R2 Convergence Regression Tests (Caller In-Tx Guard, Precedence, OSS Redeem)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r2_publish_off_external_downgrade_bypass_blocked_and_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    tiers = {"ext-owner": "external", "int-former-adm": "internal", "adm-1": "admin"}
    for mod in (monitor_service, monitor_perms, access_tiers):
        monkeypatch.setattr(
            mod,
            "get_user_tier",
            create_autospec(mod.get_user_tier, side_effect=lambda u: tiers[u]),
        )

    pub_ext_mon = _make_monitor(
        owner_user_id="ext-owner",
        visibility="public",
        keep_updated=True,
        published_seq=2,
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "ext-owner",
                "status": "running",
                "name": "ws",
                "config": {},
            },
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_normalize_workspace_entry_path",
        create_autospec(monitor_service._normalize_workspace_entry_path, return_value="report.html"),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=pub_ext_mon),
    )
    upd_tx_spy = create_autospec(monitor_db.update_monitor_settings_tx)
    monkeypatch.setattr(monitor_db, "update_monitor_settings_tx", upd_tx_spy)

    # 1) Publishing switch OFF + existing 'public' monitor: external owner CANNOT use
    # POST /monitors to downgrade to 'own' + keep_updated=True (must use PATCH)
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    with pytest.raises(HTTPException) as exc_bypass:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(
                workspace_id=WS_ID,
                path="report.html",
                visibility="own",
                keep_updated=True,
            ),
            caller_id="ext-owner",
        )
    assert exc_bypass.value.status_code == 403
    assert exc_bypass.value.detail["code"] == "external_member_forbidden"
    upd_tx_spy.assert_not_awaited()

    # 2) Publishing switch ON: external owner republishing existing 'public' monitor gets
    # 403 external_member_forbidden BEFORE admin_required; internal demoted admin still gets admin_required
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    slug_spy = create_autospec(monitor_db.get_monitor_by_slug, return_value=pub_ext_mon)
    monkeypatch.setattr(monitor_db, "get_monitor_by_slug", slug_spy)
    failures: list[str] = []

    async def _rec_fail(mid: str, *, error_message: str, run_id: str | None = None) -> MonitorRecord:
        failures.append(error_message)
        return _make_monitor(**{**pub_ext_mon.__dict__, "last_publish_error": error_message})

    monkeypatch.setattr(
        monitor_db,
        "record_monitor_publish_failure",
        create_autospec(monitor_db.record_monitor_publish_failure, side_effect=_rec_fail),
    )

    with pytest.raises(HTTPException) as exc_ext_pub:
        await monitor_service.republish_workspace_monitor(
            pub_ext_mon.slug, RepublishMonitorRequest(), caller_id="ext-owner"
        )
    assert exc_ext_pub.value.status_code == 403
    assert exc_ext_pub.value.detail["code"] == "external_member_forbidden"
    assert failures[-1] == monitor_perms.EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE

    pub_int_mon = _make_monitor(
        owner_user_id="int-former-adm",
        visibility="public",
        keep_updated=True,
        published_seq=2,
    )
    slug_spy.return_value = pub_int_mon
    with pytest.raises(HTTPException) as exc_int_pub:
        await monitor_service.republish_workspace_monitor(
            pub_int_mon.slug, RepublishMonitorRequest(), caller_id="int-former-adm"
        )
    assert exc_int_pub.value.status_code == 403
    assert exc_int_pub.value.detail["code"] == "admin_required"
    assert failures[-1] == monitor_service.PUBLIC_PERMISSION_LOST_MESSAGE

    # 3) Post-turn auto-update for external owner's 'public' monitor records external_member_forbidden message
    monkeypatch.setattr(
        monitor_db,
        "list_workspace_auto_update_monitors",
        create_autospec(
            monitor_db.list_workspace_auto_update_monitors, return_value=[pub_ext_mon]
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "db_get_workspace",
        create_autospec(
            monitor_service.db_get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": "ext-owner", "status": "running"},
        ),
    )
    await monitor_service.republish_workspace_monitors_after_turn(workspace_id=WS_ID)
    assert failures[-1] == monitor_perms.EXTERNAL_MONITOR_PUBLISH_FORBIDDEN_MESSAGE


@pytest.mark.asyncio
async def test_r2_commit_monitor_version_tx_verifies_live_caller_ownership_in_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    # Monitor row locked in DB is now owned by adm-2, while caller_user_id is non-admin former-owner
    cur = create_autospec(AsyncCursor, instance=True)
    cur._rows: list[dict[str, Any]] = []
    for_share_uids: list[str] = []

    async def _exec(sql: str, params: Any = None) -> None:
        if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
            cur._rows = [
                {
                    "monitor_id": MONITOR_ID,
                    "visibility": "company",
                    "owner_user_id": "adm-2",
                    "source_kind": "workspace",
                    "title": "Board",
                    "published_seq": 1,
                }
            ]
        elif "FROM dalton_user_roles" in sql and "FOR SHARE" in sql:
            uid = str(params[0]) if params else ""
            for_share_uids.append(uid)
            tier = "admin" if uid == "adm-2" else "internal"
            cur._rows = [{"tier": tier}]
        else:
            cur._rows = []

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_cm)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            return_value=_make_artifact(shared_at=None, latest_seq=1),
        ),
    )

    with pytest.raises(HTTPException) as exc_not_mgr:
        await monitor_db.commit_monitor_version_tx(
            monitor_id=MONITOR_ID,
            artifact_id=ARTIFACT_ID,
            workspace_owner_id="former-owner",
            files=[],
            title="Board",
            caller_user_id="former-owner",
        )
    assert exc_not_mgr.value.status_code == 403
    assert exc_not_mgr.value.detail["code"] == "external_member_forbidden"
    assert for_share_uids == ["adm-2", "former-owner"]


@pytest.mark.asyncio
async def test_r2_single_user_oss_mode_rejects_invite_redeem_before_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "oss")

    rate_limit_spy = create_autospec(invites.enforce_invite_rate_limit)
    monkeypatch.setattr(invites, "enforce_invite_rate_limit", rate_limit_spy)

    with pytest.raises(HTTPException) as exc_ep:
        await dalton_router.redeem_invite_endpoint(
            body=dalton_router.InviteRedeemRequest(
                token="tok-oss",
                username="alice",
                password="validpassword123",
            )
        )
    assert exc_ep.value.status_code == 403
    assert exc_ep.value.detail["code"] == "invites_disabled_in_oss"
    rate_limit_spy.assert_not_awaited()

    with pytest.raises(HTTPException) as exc_fn:
        await invites.redeem_invite_token(
            token="tok-oss",
            username="alice",
            password="validpassword123",
        )
    assert exc_fn.value.status_code == 403
    assert exc_fn.value.detail["code"] == "invites_disabled_in_oss"


@pytest.mark.asyncio
async def test_r3_commit_monitor_version_tx_blocks_stale_public_takeover_and_sorts_for_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")

    # Monitor was already downgraded to 'own' under owner 'a-owner', while admin 'z-admin'
    # arrives with new_owner_user_id='z-admin' computed from a stale public snapshot.
    cur = create_autospec(AsyncCursor, instance=True)
    cur._rows: list[dict[str, Any]] = []
    for_share_uids: list[str] = []

    async def _exec(sql: str, params: Any = None) -> None:
        if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
            cur._rows = [
                {
                    "monitor_id": MONITOR_ID,
                    "visibility": "own",
                    "owner_user_id": "a-owner",
                    "source_kind": "workspace",
                    "title": "Private Board",
                    "published_seq": 1,
                }
            ]
        elif "FROM dalton_user_roles" in sql and "FOR SHARE" in sql:
            uid = str(params[0]) if params else ""
            for_share_uids.append(uid)
            tier = "admin" if uid == "z-admin" else "internal"
            cur._rows = [{"tier": tier}]
        else:
            cur._rows = []

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_cm)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            return_value=_make_artifact(shared_at=None, latest_seq=1),
        ),
    )
    cut_in_cur_spy = create_autospec(artifacts_versions_db.cut_monitor_in_cur)
    monkeypatch.setattr(artifacts_versions_db, "cut_monitor_in_cur", cut_in_cur_spy)

    with pytest.raises(HTTPException) as exc_takeover:
        await monitor_db.commit_monitor_version_tx(
            monitor_id=MONITOR_ID,
            artifact_id=ARTIFACT_ID,
            workspace_owner_id="a-owner",
            files=[],
            title="Private Board",
            new_owner_user_id="z-admin",
            caller_user_id="z-admin",
        )
    assert exc_takeover.value.status_code == 403
    assert exc_takeover.value.detail["code"] == "external_member_forbidden"
    cut_in_cur_spy.assert_not_awaited()
    assert for_share_uids == ["a-owner", "z-admin"]


@pytest.mark.asyncio
async def test_r3_publish_off_post_monitors_rejects_concurrently_demoted_external_in_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")

    pub_mon = _make_monitor(
        owner_user_id="u-owner",
        visibility="public",
        keep_updated=False,
        published_seq=2,
    )
    # Pre-tx role read sees 'admin', while inside tx FOR SHARE sees demoted 'external'.
    monkeypatch.setattr(
        auth_roles,
        "get_user_role",
        create_autospec(auth_roles.get_user_role, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={
                "workspace_id": WS_ID,
                "user_id": "u-owner",
                "status": "running",
                "name": "ws",
                "config": {},
            },
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_normalize_workspace_entry_path",
        create_autospec(monitor_service._normalize_workspace_entry_path, return_value="report.html"),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=_make_artifact()),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=pub_mon),
    )

    cur = create_autospec(AsyncCursor, instance=True)
    cur._rows: list[Any] = []
    updated_sqls: list[str] = []

    async def _exec(sql: str, params: Any = None) -> None:
        if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
            cur._rows = [
                {
                    "title": "Board",
                    "category": "overview",
                    "sort_order": 0,
                    "visibility": "public",
                    "keep_updated": False,
                    "published_seq": 2,
                    "owner_user_id": "u-owner",
                    "source_kind": "workspace",
                }
            ]
        elif "FROM dalton_user_roles" in sql and "FOR SHARE" in sql:
            cur._rows = [{"tier": "external"}]
        elif "FROM dalton_monitor_members" in sql:
            cur._rows = []
        elif "UPDATE dalton_monitors" in sql:
            updated_sqls.append(sql)
            cur._rows = []
        else:
            cur._rows = []

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
    cur.fetchall.side_effect = lambda: list(cur._rows)

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_cm)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            return_value=_make_artifact(shared_at=NOW, latest_seq=2, shared_seq=2),
        ),
    )

    with pytest.raises(HTTPException) as exc_post:
        await monitor_service.publish_workspace_monitor(
            PublishMonitorRequest(
                workspace_id=WS_ID,
                path="report.html",
                visibility="own",
                keep_updated=True,
            ),
            caller_id="u-owner",
        )
    assert exc_post.value.status_code == 403
    assert exc_post.value.detail["code"] == "external_member_forbidden"
    assert updated_sqls == []

    # Contrast: independent PATCH (reject_external_non_own_existing=False) succeeds cleanly.
    unshare_spy = create_autospec(
        artifacts_records_db.unshare_in_cur,
        return_value=_make_artifact(shared_at=None, latest_seq=2, shared_seq=0, revision=2),
    )
    monkeypatch.setattr(artifacts_records_db, "unshare_in_cur", unshare_spy)
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            return_value=_make_monitor(
                owner_user_id="u-owner",
                visibility="own",
                keep_updated=False,
                published_seq=2,
            ),
        ),
    )
    patched = await monitor_db.update_monitor_settings_tx(
        MONITOR_ID,
        artifact_id=ARTIFACT_ID,
        owner_user_id="u-owner",
        caller_id="u-owner",
        title=None,
        category=None,
        sort_order=None,
        visibility="own",
        keep_updated=False,
        member_user_ids=[],
        resolve_caller_tier=monitor_service.get_user_tier,
        reject_external_non_own_existing=False,
    )
    assert patched.visibility == "own"
    assert len(updated_sqls) == 1
    unshare_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_r4_data_job_patch_local_requires_approver_and_reuses_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
    from src.dalton.monitors import data_job_registry
    from src.dalton.monitors.data_job_registry import MonitorDataJobBinding
    from src.server.utils import api as api_utils
    from src.dalton import sharing as sharing_mod

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    # 1. PATCH on data_job monitor in local mode must NOT skip registered approver check.
    job_mon = _make_monitor(
        slug="job-no-approver",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        visibility="company",
        source_kind="data_job",
        source_ref="job-no-approver-ref",
    )
    unapproved_binding = MonitorDataJobBinding(
        job_key="job-no-approver-ref",
        slug="job-no-approver",
        title="Unapproved Job",
        category="overview",
        visibility="company",
        rel_dir="research-outputs/job-no-approver",
        approved_by_admin_user_id=None,
    )
    monkeypatch.setitem(
        data_job_registry.MONITOR_DATA_JOB_REGISTRY,
        unapproved_binding.job_key,
        unapproved_binding,
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=job_mon),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="admin"),
    )
    monkeypatch.setattr(
        monitor_service,
        "_resolve_workspace_owner_id",
        create_autospec(
            monitor_service._resolve_workspace_owner_id,
            return_value=COMPANY_SYSTEM_USER_ID,
        ),
    )

    cur = create_autospec(AsyncCursor, instance=True)
    cur.connection = object()
    cur._rows: list[Any] = []
    locked_uids_in_order: list[str] = []

    async def _exec(sql: str, params: Any = None) -> None:
        if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
            cur._rows = [
                {
                    "title": "Job",
                    "category": "overview",
                    "sort_order": 0,
                    "visibility": "company",
                    "keep_updated": False,
                    "published_seq": 1,
                    "owner_user_id": COMPANY_SYSTEM_USER_ID,
                    "source_kind": "data_job",
                    "source_ref": "job-no-approver-ref",
                    "slug": "job-no-approver",
                }
            ]
        elif "FROM dalton_user_roles" in sql and "FOR SHARE" in sql:
            uid = str(params[0]) if params else ""
            locked_uids_in_order.append(uid)
            cur._rows = [{"tier": "admin"}]
        else:
            cur._rows = []

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
    cur.fetchall.side_effect = lambda: list(cur._rows)

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_cm)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            return_value=_make_artifact(shared_at=None, latest_seq=1),
        ),
    )

    with pytest.raises(HTTPException) as exc_unapproved:
        await monitor_service.update_monitor_settings(
            "job-no-approver",
            UpdateMonitorRequest(visibility="public"),
            caller_id="adm-1",
        )
    assert exc_unapproved.value.status_code == 403

    # 2. _resolve_live_tier_in_cur in non-local (supabase) mode forwards cur.connection to get_user_tier.
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    seen_conns: list[Any] = []

    async def _supabase_tier(uid: str, *, conn: Any = None) -> str:
        seen_conns.append((uid, conn))
        return "internal"

    monkeypatch.setattr(monitor_perms, "get_user_tier", _supabase_tier)
    resolved_tier = await monitor_db._resolve_live_tier_in_cur(cur, "u-supa")
    assert resolved_tier == "internal"
    assert seen_conns == [("u-supa", cur.connection)]

    # 3. require_thread_owner(..., conn=conn) forwards conn=conn to get_user_tier.
    monkeypatch.setattr(conv_db, "get_thread_owner_id", create_autospec(conv_db.get_thread_owner_id, return_value="u-ext"))
    monkeypatch.setattr(conv_db, "get_thread_auth_meta", create_autospec(conv_db.get_thread_auth_meta, return_value={"workspace_owner_id": "u-ext"}))
    seen_api_conns: list[Any] = []

    async def _api_tier(uid: str, *, conn: Any = None) -> str:
        seen_api_conns.append((uid, conn))
        return "external"

    monkeypatch.setattr(sharing_mod, "get_user_tier", _api_tier)
    tx_conn = object()
    await api_utils.require_thread_owner("th-1", "u-ext", conn=tx_conn)
    assert seen_api_conns == [("u-ext", tx_conn)]


@pytest.mark.asyncio
async def test_r5_data_job_approver_for_share_order_cut_and_delete_conn_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
    from src.dalton.monitors import data_job_registry
    from src.dalton.monitors.data_job_registry import MonitorDataJobBinding
    from src.dalton.monitors.service import PublicAuthorityLostError
    from src.server.database.artifacts import versions as artifacts_versions_db
    from src.server.database.artifacts.versions import PublishResult

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "local")
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")

    approved_binding = MonitorDataJobBinding(
        job_key="job-approved-ref",
        slug="job-approved",
        title="Approved Job",
        category="overview",
        visibility="public",
        rel_dir="research-outputs/job-approved",
        approved_by_admin_user_id="adm-a-approver",
    )
    monkeypatch.setitem(
        data_job_registry.MONITOR_DATA_JOB_REGISTRY,
        approved_binding.job_key,
        approved_binding,
    )

    cur = create_autospec(AsyncCursor, instance=True)
    cur.connection = object()
    cur._rows: list[Any] = []
    cur.rowcount = 1
    locked_order: list[str] = []
    approver_live_tier = "external"

    async def _exec(sql: str, params: Any = None) -> None:
        if "FROM dalton_monitors" in sql and "FOR UPDATE" in sql:
            cur._rows = [
                {
                    "title": "Job",
                    "category": "overview",
                    "sort_order": 0,
                    "visibility": "public",
                    "keep_updated": True,
                    "published_seq": 1,
                    "owner_user_id": COMPANY_SYSTEM_USER_ID,
                    "source_kind": "data_job",
                    "source_ref": "job-approved-ref",
                    "slug": "job-approved",
                }
            ]
        elif "FROM dalton_user_roles" in sql and "FOR SHARE" in sql:
            uid = str(params[0])
            locked_order.append(uid)
            tier = approver_live_tier if uid == "adm-a-approver" else "admin"
            cur._rows = [{"tier": tier}]
        elif "SELECT user_id FROM dalton_monitor_members" in sql:
            cur._rows = []
        else:
            cur._rows = []

    cur.execute.side_effect = _exec
    cur.fetchone.side_effect = lambda: cur._rows[0] if cur._rows else None
    cur.fetchall.side_effect = lambda: list(cur._rows)

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm():
        yield

    conn = create_autospec(AsyncConnection, instance=True)
    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm

    @asynccontextmanager
    async def _conn_cm():
        yield conn

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_cm)
    monkeypatch.setattr(
        artifacts_records_db,
        "lock_owned",
        create_autospec(
            artifacts_records_db.lock_owned,
            return_value=_make_artifact(shared_at=None, latest_seq=1),
        ),
    )

    seen_verify_conns: list[tuple[str, Any]] = []

    async def _verify_authority(uid: str, *, conn: Any = None) -> None:
        seen_verify_conns.append((uid, conn))

    # 1) update_monitor_settings_tx in local mode locks approver ('adm-a-approver') BEFORE caller ('caller-z')
    #    via ascending FOR SHARE and rejects demoted approver even before external verify callback.
    with pytest.raises(PublicAuthorityLostError):
        await monitor_db.update_monitor_settings_tx(
            "20000000-0000-4000-8000-000000000001",
            artifact_id="art-1",
            owner_user_id=COMPANY_SYSTEM_USER_ID,
            caller_id="caller-z",
            visibility="public",
            verify_public_authority=_verify_authority,
            resolve_caller_tier=monitor_db.get_user_tier,
            public_approver_id="adm-a-approver",
        )
    assert locked_order == ["adm-a-approver", "caller-z"]

    # 2) _cut_and_finalize_monitor_in_cur forwards cur.connection to verify_public_authority
    #    on both data_job path and non-local workspace publish path.
    seen_verify_conns.clear()
    monkeypatch.setattr(
        artifacts_versions_db,
        "cut_monitor_in_cur",
        create_autospec(
            artifacts_versions_db.cut_monitor_in_cur,
            return_value=PublishResult(
                artifact=_make_artifact(shared_at=None, latest_seq=2),
                seq=2,
                cut=True,
                shared_now=False,
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            return_value=_make_monitor(
                slug="job-approved",
                owner_user_id=COMPANY_SYSTEM_USER_ID,
                visibility="public",
                source_kind="data_job",
            ),
        ),
    )
    await monitor_db._cut_and_finalize_monitor_in_cur(
        cur,
        monitor_id="20000000-0000-4000-8000-000000000001",
        locked=_make_artifact(shared_at=None, latest_seq=1),
        row={
            "visibility": "public",
            "owner_user_id": COMPANY_SYSTEM_USER_ID,
            "source_kind": "data_job",
        },
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        files=[],
        title="Job",
        verify_public_authority=_verify_authority,
    )
    assert seen_verify_conns == [(COMPANY_SYSTEM_USER_ID, cur.connection)]

    seen_verify_conns.clear()
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(
        monitor_db,
        "_resolve_live_tier_in_cur",
        create_autospec(monitor_db._resolve_live_tier_in_cur, return_value="admin"),
    )
    await monitor_db._cut_and_finalize_monitor_in_cur(
        cur,
        monitor_id="20000000-0000-4000-8000-000000000001",
        locked=_make_artifact(shared_at=None, latest_seq=1),
        row={
            "visibility": "public",
            "owner_user_id": "owner-w",
            "source_kind": "workspace",
        },
        workspace_owner_id="owner-w",
        files=[],
        title="WS Mon",
        caller_user_id="owner-w",
        verify_public_authority=_verify_authority,
    )
    assert seen_verify_conns == [("owner-w", cur.connection)]

    # 3) delete_monitor_tx forwards cur.connection when using custom tier resolver.
    del_conns: list[tuple[str, Any]] = []

    async def _del_tier(uid: str, *, conn: Any = None) -> str:
        del_conns.append((uid, conn))
        return "admin"

    monkeypatch.setattr(
        artifacts_records_db,
        "unshare_in_cur",
        create_autospec(
            artifacts_records_db.unshare_in_cur,
            return_value=_make_artifact(shared_at=None, latest_seq=1),
        ),
    )

    deleted = await monitor_db.delete_monitor_tx(
        "20000000-0000-4000-8000-000000000001",
        artifact_id="art-1",
        workspace_owner_id="owner-w",
        caller_id="caller-d",
        resolve_caller_tier=_del_tier,
    )
    assert deleted is True
    assert del_conns == [("caller-d", cur.connection)]
