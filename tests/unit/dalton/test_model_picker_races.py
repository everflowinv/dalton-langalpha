"""Tests for model-picker race small fixes (Items 2 & 3: switch_thread_model atomic effort clear + SSE events, threads_write CAS SQL, PATCH /threads/{id} 409 & thread_model broadcast)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.llms.llm import ModelConfig
from src.server.app.threads import crud as crud_mod
from src.server.database.conversation import threads_read, threads_write
from src.server.services import thread_lifecycle_feed
from src.server.services.llm import thread_model as thread_model_svc
from src.server.services.llm import user_models
from src.server.services.shared_workspaces.access import Access
from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app

SVC_MOD = "src.server.services.llm.thread_model"
USER_ID = "usr-picker-001"
THREAD_ID = "22222222-2222-2222-2222-222222222222"
WS_ID = "ws-picker-001"


def _make_catalog(entries: dict[str, dict[str, Any]]) -> ModelConfig:
    mc = create_autospec(ModelConfig, instance=True)
    mc.llm_config = entries
    mc.get_model_config.side_effect = lambda name: entries.get(name)
    mc.get_provider_info.side_effect = lambda _prov: {}
    mc.listed_models.return_value = [
        (name, info) for name, info in entries.items() if info.get("visible", False)
    ]
    return mc


def _thread_row(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    row: dict[str, Any] = {
        "conversation_thread_id": THREAD_ID,
        "workspace_id": WS_ID,
        "current_status": "completed",
        "msg_type": "ptc",
        "thread_index": 0,
        "title": "Thread",
        "llm_model": "gemini-4-neon-low-oauth",
        "subagents_allowed": None,
        "reasoning_effort": None,
        "fast_mode": None,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# Item 2: switch_thread_model atomic reasoning_effort clear + SSE broadcast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_handoff", [True, False], ids=["deferred", "immediate"])
async def test_switch_thread_model_to_antigravity_clears_reasoning_effort_and_publishes_events(
    defer_handoff: bool,
):
    """Report High #3: switching from a standard model with reasoning_effort='high'
    to a built-in Antigravity variant atomically clears reasoning_effort=None and
    publishes both thread_model and thread_tuning SSE events."""
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "llm_model": "gpt-6.2-sol",
            "reasoning_effort": "high",
            "workspace_id": WS_ID,
            "user_id": USER_ID,
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(
            llm_model="gemini-4-neon-low-oauth",
            reasoning_effort=None,
            fast_mode=False,
        ),
    )
    pub_model = create_autospec(thread_lifecycle_feed.publish_thread_model)
    pub_tuning = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    get_pref = create_autospec(user_models.get_model_preference, return_value={})

    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch(f"{SVC_MOD}.user_models.get_model_preference", new=get_pref),
        patch.object(user_models, "get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch.object(thread_lifecycle_feed, "publish_thread_model", new=pub_model),
        patch.object(thread_lifecycle_feed, "publish_thread_tuning", new=pub_tuning),
        patch(f"{SVC_MOD}._read_latest_run_identity", new=AsyncMock(return_value=(None, False, None, False))),
        patch.object(thread_model_svc, "_immediate_handoff_runner", new=AsyncMock(return_value=(None, None))),
    ):
        result = await thread_model_svc.switch_thread_model(
            USER_ID,
            THREAD_ID,
            "gemini-4-neon-low-oauth",
            defer_handoff=defer_handoff,
        )

    assert result.changed is True
    assert result.model == "gemini-4-neon-low-oauth"
    update_spy.assert_awaited_once()
    call_kwargs = update_spy.await_args.kwargs
    assert call_kwargs["llm_model"] == "gemini-4-neon-low-oauth"
    assert "reasoning_effort" in call_kwargs
    assert call_kwargs["reasoning_effort"] is None
    if defer_handoff:
        assert "metadata_merge" in call_kwargs
    pub_model.assert_awaited_once_with(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        workspace_id=WS_ID,
        llm_model="gemini-4-neon-low-oauth",
    )
    pub_tuning.assert_awaited_once_with(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        workspace_id=WS_ID,
        reasoning_effort=None,
        fast_mode=False,
    )


@pytest.mark.asyncio
async def test_switch_thread_model_to_standard_model_keeps_effort_and_publishes_model_event():
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "llm_model": "gemini-4-neon-low-oauth",
            "reasoning_effort": None,
            "workspace_id": WS_ID,
            "user_id": USER_ID,
        },
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="gpt-6.2-sol", reasoning_effort=None),
    )
    pub_model = create_autospec(thread_lifecycle_feed.publish_thread_model)
    pub_tuning = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    get_pref = create_autospec(user_models.get_model_preference, return_value={})

    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch(f"{SVC_MOD}.user_models.get_model_preference", new=get_pref),
        patch.object(user_models, "get_model_preference", new=get_pref),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch.object(thread_lifecycle_feed, "publish_thread_model", new=pub_model),
        patch.object(thread_lifecycle_feed, "publish_thread_tuning", new=pub_tuning),
        patch(f"{SVC_MOD}._read_latest_run_identity", new=AsyncMock(return_value=(None, False, None, False))),
        patch.object(thread_model_svc, "_immediate_handoff_runner", new=AsyncMock(return_value=(None, None))),
    ):
        result = await thread_model_svc.switch_thread_model(
            USER_ID, THREAD_ID, "gpt-6.2-sol"
        )

    assert result.changed is True
    assert "reasoning_effort" not in update_spy.await_args.kwargs
    pub_model.assert_awaited_once_with(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        workspace_id=WS_ID,
        llm_model="gpt-6.2-sol",
    )
    pub_tuning.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_thread_model_custom_model_shadowing_antigravity_does_not_clear_effort():
    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    custom_pref = {
        "custom_models": [
            {"name": "gemini-4-neon-low-oauth", "provider": "openai", "model_id": "custom"}
        ]
    }
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={"llm_model": "gpt-6.2-sol", "workspace_id": WS_ID, "user_id": USER_ID},
    )
    update_spy = create_autospec(
        threads_write.update_thread_fields,
        return_value=_thread_row(llm_model="gemini-4-neon-low-oauth", reasoning_effort="high"),
    )
    pub_model = create_autospec(thread_lifecycle_feed.publish_thread_model)
    pub_tuning = create_autospec(thread_lifecycle_feed.publish_thread_tuning)

    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch(f"{SVC_MOD}.user_models.get_model_preference", new=create_autospec(user_models.get_model_preference, return_value=custom_pref)),
        patch(f"{SVC_MOD}.threads_read.get_thread_auth_meta", new=meta_mock),
        patch(f"{SVC_MOD}.threads_write.update_thread_fields", new=update_spy),
        patch.object(thread_lifecycle_feed, "publish_thread_model", new=pub_model),
        patch.object(thread_lifecycle_feed, "publish_thread_tuning", new=pub_tuning),
        patch(f"{SVC_MOD}._read_latest_run_identity", new=AsyncMock(return_value=(None, False, None, False))),
        patch.object(thread_model_svc, "_immediate_handoff_runner", new=AsyncMock(return_value=(None, None))),
    ):
        await thread_model_svc.switch_thread_model(
            USER_ID, THREAD_ID, "gemini-4-neon-low-oauth", model_pref=custom_pref
        )

    assert "reasoning_effort" not in update_spy.await_args.kwargs
    pub_model.assert_awaited_once()
    pub_tuning.assert_not_awaited()


# ---------------------------------------------------------------------------
# Item 3: threads_write.update_thread_fields CAS SQL & PATCH /threads/{id} 409
# ---------------------------------------------------------------------------


class _CursorStub:
    async def execute(self, sql: str, params: tuple[Any, ...]) -> None: ...
    async def fetchone(self) -> dict[str, Any] | None: ...


class _ConnStub:
    def cursor(self, **_kwargs: Any) -> Any: ...


@pytest.mark.asyncio
async def test_update_thread_fields_expected_guards_emit_is_not_distinct_from_sql():
    recorded: list[tuple[str, tuple[Any, ...]]] = []

    def _make_pool(fetchone_result: dict[str, Any] | None):
        cur = create_autospec(_CursorStub, instance=True)

        async def _exec(sql: str, params: tuple[Any, ...]) -> None:
            recorded.append((sql, params))

        cur.execute.side_effect = _exec
        cur.fetchone.return_value = fetchone_result

        @asynccontextmanager
        async def _cursor_ctx(**_kwargs: Any):
            yield cur

        conn = create_autospec(_ConnStub, instance=True)
        conn.cursor.side_effect = _cursor_ctx

        @asynccontextmanager
        async def _conn_ctx():
            yield conn

        return _conn_ctx

    # 1) Unconditional direct click: no expected_* predicates in WHERE.
    with patch.object(threads_write.pool, "get_db_connection", new=_make_pool(_thread_row())):
        res = await threads_write.update_thread_fields(THREAD_ID, llm_model="gemini-4-neon-low-oauth")
    assert res is not None
    sql_direct, params_direct = recorded[-1]
    assert "IS NOT DISTINCT FROM" not in sql_direct
    assert params_direct == ("gemini-4-neon-low-oauth", THREAD_ID)

    # 2) Background repatch with expected_llm_model + expected_reasoning_effort=None (explicit NULL).
    with patch.object(threads_write.pool, "get_db_connection", new=_make_pool(None)):
        res_miss = await threads_write.update_thread_fields(
            THREAD_ID,
            llm_model="gemini-4-neon-low-oauth",
            reasoning_effort=None,
            expected_llm_model="gemini-4-neon-high-oauth",
            expected_reasoning_effort=None,
            expected_fast_mode=False,
        )
    assert res_miss is None
    sql_cas, params_cas = recorded[-1]
    assert "llm_model IS NOT DISTINCT FROM %s" in sql_cas
    assert "reasoning_effort IS NOT DISTINCT FROM %s" in sql_cas
    assert "fast_mode IS NOT DISTINCT FROM %s" in sql_cas
    assert params_cas == (
        "gemini-4-neon-low-oauth",
        None,
        THREAD_ID,
        "gemini-4-neon-high-oauth",
        None,
        False,
    )


@pytest.mark.asyncio
async def test_patch_thread_background_cas_null_expected_returns_409_and_missing_returns_404():
    from src.server.app.threads import router

    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    # 1) Explicit JSON null for expected_reasoning_effort -> forwarded to update_thread_fields -> 409 when row exists.
    update_miss = create_autospec(crud_mod.update_thread_fields, return_value=None)
    pub_tuning = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    with (
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "update_thread_fields", new=update_miss),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value={"reasoning_effort": "high"})),
        patch.object(crud_mod, "publish_thread_tuning", new=pub_tuning),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"reasoning_effort": "low", "expected_reasoning_effort": None},
            )
    assert resp.status_code == 409
    assert resp.json()["detail"]["type"] == "thread_setting_conflict"
    update_miss.assert_awaited_once_with(
        THREAD_ID,
        reasoning_effort="low",
        expected_reasoning_effort=None,
    )
    pub_tuning.assert_not_awaited()

    # 2) When thread itself disappeared concurrently -> 404 (not 409).
    update_gone = create_autospec(crud_mod.update_thread_fields, return_value=None)
    with (
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "update_thread_fields", new=update_gone),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value=None)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp_gone = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"reasoning_effort": "low", "expected_reasoning_effort": "medium"},
            )
    assert resp_gone.status_code == 404


# ---------------------------------------------------------------------------
# R2 Codex-H4: server-side atomic reasoning_effort clear on PATCH llm_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_thread_manual_antigravity_switch_atomically_clears_effort_without_client_field():
    """Codex-H4: even when browser cache was stale (reasoning_effort: null) so the
    client omits reasoning_effort on a manual PATCH to a built-in Antigravity
    variant, the server unconditionally writes llm_model AND reasoning_effort=None
    in one SQL UPDATE and publishes both thread_model and thread_tuning."""
    from src.server.app.threads import router

    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    updated_row = _thread_row(
        llm_model="gemini-4-neon-low-oauth",
        reasoning_effort=None,
        fast_mode=False,
    )
    update_spy = create_autospec(crud_mod.update_thread_fields, return_value=updated_row)
    pub_model = create_autospec(thread_lifecycle_feed.publish_thread_model)
    pub_tuning = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    get_pref = create_autospec(user_models.get_model_preference, return_value={})

    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch.object(user_models, "get_model_preference", new=get_pref),
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "require_selectable_model", new=create_autospec(crud_mod.require_selectable_model)),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value={"llm_model": "gpt-6.2-sol", "reasoning_effort": "high", "workspace_id": WS_ID})),
        patch.object(crud_mod, "update_thread_fields", new=update_spy),
        patch.object(crud_mod, "publish_thread_model", new=pub_model),
        patch.object(crud_mod, "publish_thread_tuning", new=pub_tuning),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=AsyncMock(return_value=False)),
        patch("src.server.services.llm.thread_model.next_turn_runs_claude_code", new=AsyncMock(return_value=False)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"llm_model": "gemini-4-neon-low-oauth"},
            )

    assert resp.status_code == 200
    # Unconditional manual write (no expected_*), clearing reasoning_effort in the same SQL UPDATE.
    update_spy.assert_awaited_once_with(
        THREAD_ID,
        llm_model="gemini-4-neon-low-oauth",
        reasoning_effort=None,
    )
    pub_model.assert_awaited_once_with(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        workspace_id=WS_ID,
        llm_model="gemini-4-neon-low-oauth",
    )
    pub_tuning.assert_awaited_once_with(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        workspace_id=WS_ID,
        reasoning_effort=None,
        fast_mode=False,
    )


@pytest.mark.asyncio
async def test_patch_thread_manual_standard_and_shadowed_custom_models_do_not_clear_effort():
    """Codex-H4 guard: standard models and user custom models shadowing an
    Antigravity key must NOT auto-clear reasoning_effort on PATCH."""
    from src.server.app.threads import router

    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    # 1) Standard model keeps reasoning_effort untouched.
    update_std = create_autospec(
        crud_mod.update_thread_fields,
        return_value=_thread_row(llm_model="gpt-6.2-sol", reasoning_effort="high"),
    )
    pub_tuning_std = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch.object(user_models, "get_model_preference", new=create_autospec(user_models.get_model_preference, return_value={})),
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "require_selectable_model", new=create_autospec(crud_mod.require_selectable_model)),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value={"llm_model": "gemini-4-neon-low-oauth", "workspace_id": WS_ID})),
        patch.object(crud_mod, "update_thread_fields", new=update_std),
        patch.object(crud_mod, "publish_thread_model", new=create_autospec(thread_lifecycle_feed.publish_thread_model)),
        patch.object(crud_mod, "publish_thread_tuning", new=pub_tuning_std),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=AsyncMock(return_value=False)),
        patch("src.server.services.llm.thread_model.next_turn_runs_claude_code", new=AsyncMock(return_value=False)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"llm_model": "gpt-6.2-sol"},
            )
    assert resp.status_code == 200
    update_std.assert_awaited_once_with(THREAD_ID, llm_model="gpt-6.2-sol")
    pub_tuning_std.assert_not_awaited()

    # 2) Custom model shadowing an Antigravity key keeps reasoning_effort untouched.
    custom_pref = {
        "custom_models": [
            {"name": "gemini-4-neon-low-oauth", "provider": "openai", "model_id": "custom"}
        ]
    }
    update_custom = create_autospec(
        crud_mod.update_thread_fields,
        return_value=_thread_row(llm_model="gemini-4-neon-low-oauth", reasoning_effort="high"),
    )
    pub_tuning_custom = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch.object(user_models, "get_model_preference", new=create_autospec(user_models.get_model_preference, return_value=custom_pref)),
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "require_selectable_model", new=create_autospec(crud_mod.require_selectable_model)),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value={"llm_model": "gpt-6.2-sol", "workspace_id": WS_ID})),
        patch.object(crud_mod, "update_thread_fields", new=update_custom),
        patch.object(crud_mod, "publish_thread_model", new=create_autospec(thread_lifecycle_feed.publish_thread_model)),
        patch.object(crud_mod, "publish_thread_tuning", new=pub_tuning_custom),
        patch("src.server.handlers.thread_maintenance.hand_off_on_switch", new=AsyncMock(return_value=False)),
        patch("src.server.services.llm.thread_model.next_turn_runs_claude_code", new=AsyncMock(return_value=False)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp2 = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={"llm_model": "gemini-4-neon-low-oauth"},
            )
    assert resp2.status_code == 200
    update_custom.assert_awaited_once_with(THREAD_ID, llm_model="gemini-4-neon-low-oauth")
    pub_tuning_custom.assert_not_awaited()


# ---------------------------------------------------------------------------
# R3 Codex #1: conditional Antigravity PATCH must guard reasoning_effort (even None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_thread_conditional_antigravity_guards_null_reasoning_effort_against_concurrent_high():
    """R3 Codex #1: when a background conditional PATCH switches to a built-in
    Antigravity variant (clearing reasoning_effort=None), it must guard
    expected_reasoning_effort (including explicit None) so a concurrent writer's
    reasoning_effort='high' returns 409 instead of being wiped."""
    from src.server.app.threads import router

    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )
    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    # Case 1: client sends explicit expected_reasoning_effort=None -> forwarded & 409 on conflict.
    update_explicit = create_autospec(crud_mod.update_thread_fields, return_value=None)
    pub_model_1 = create_autospec(thread_lifecycle_feed.publish_thread_model)
    pub_tuning_1 = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch.object(user_models, "get_model_preference", new=create_autospec(user_models.get_model_preference, return_value={})),
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "require_selectable_model", new=create_autospec(crud_mod.require_selectable_model)),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value={"llm_model": "gpt-6.2-sol", "reasoning_effort": "high", "workspace_id": WS_ID})),
        patch.object(crud_mod, "update_thread_fields", new=update_explicit),
        patch.object(crud_mod, "publish_thread_model", new=pub_model_1),
        patch.object(crud_mod, "publish_thread_tuning", new=pub_tuning_1),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={
                    "llm_model": "gemini-4-neon-low-oauth",
                    "expected_llm_model": "gpt-6.2-sol",
                    "expected_reasoning_effort": None,
                },
            )
    assert resp.status_code == 409
    update_explicit.assert_awaited_once_with(
        THREAD_ID,
        llm_model="gemini-4-neon-low-oauth",
        reasoning_effort=None,
        expected_llm_model="gpt-6.2-sol",
        expected_reasoning_effort=None,
    )
    pub_model_1.assert_not_awaited()
    pub_tuning_1.assert_not_awaited()

    # Case 2: defense-in-depth — even if a conditional caller only sends expected_llm_model,
    # server auto-guards expected_reasoning_effort=None when clearing effort for Antigravity.
    update_implicit = create_autospec(crud_mod.update_thread_fields, return_value=None)
    pub_tuning_2 = create_autospec(thread_lifecycle_feed.publish_thread_tuning)
    with (
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch.object(user_models, "get_model_preference", new=create_autospec(user_models.get_model_preference, return_value={})),
        patch.object(crud_mod, "_authorize_thread_write", new=create_autospec(crud_mod._authorize_thread_write, return_value=Access(USER_ID))),
        patch.object(crud_mod, "require_selectable_model", new=create_autospec(crud_mod.require_selectable_model)),
        patch.object(crud_mod, "get_thread_auth_meta", new=create_autospec(crud_mod.get_thread_auth_meta, return_value={"llm_model": "gpt-6.2-sol", "reasoning_effort": "high", "workspace_id": WS_ID})),
        patch.object(crud_mod, "update_thread_fields", new=update_implicit),
        patch.object(crud_mod, "publish_thread_tuning", new=pub_tuning_2),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp2 = await c.patch(
                f"/api/v1/threads/{THREAD_ID}",
                json={
                    "llm_model": "gemini-4-neon-low-oauth",
                    "expected_llm_model": "gpt-6.2-sol",
                },
            )
    assert resp2.status_code == 409
    update_implicit.assert_awaited_once_with(
        THREAD_ID,
        llm_model="gemini-4-neon-low-oauth",
        reasoning_effort=None,
        expected_llm_model="gpt-6.2-sol",
        expected_reasoning_effort=None,
    )
    pub_tuning_2.assert_not_awaited()


# ---------------------------------------------------------------------------
# R3 Codex #2: thread_resolver + resolve_llm_config preserve explicit variant & avoid row effort leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_resolver_and_resolve_llm_config_preserve_explicit_variant_and_prevent_row_effort_leak():
    """R3 Codex #2:
    1) Queued request naming gemini-4-neon-low-oauth (omitting reasoning_effort) when
       thread row holds gpt-6.2-sol + reasoning_effort='high' resolves to Neon Low
       (NOT remapped to Neon High).
    2) Web request naming gemini-4-neon-low-oauth with preserve_antigravity_variant=True
       keeps Neon Low even if raw reasoning_effort='high' is supplied.
    3) Service dispatch (preserve_antigravity_variant=False, e.g. delegate_to_analyst)
       with explicit reasoning_effort='high' still maps Neon Low -> Neon High.
    4) Queued request naming another standard model (claude-opus-4-6) without effort
       does NOT inherit gpt-6.2-sol's thread reasoning_effort='high'.
    """
    import copy
    from types import SimpleNamespace
    from ptc_agent.config.agent import LLMConfig
    from src.server.app import setup as setup_mod
    from src.server.services import thread_agent
    from src.server.services.llm import config as llm_cfg
    from src.server.services.shared_workspaces.turn import TurnIdentity
    from src.server.services.turn_runtime import TurnRoute

    catalog = _make_catalog(
        {
            "gpt-6.2-sol": {"visible": True, "provider": "openai"},
            "claude-opus-4-6": {"visible": True, "provider": "anthropic"},
            "gemini-4-neon-low-oauth": {"visible": True, "provider": "antigravity-oauth"},
            "gemini-4-neon-high-oauth": {"visible": True, "provider": "antigravity-oauth"},
        }
    )

    class _BaseCfg:
        def __init__(self):
            self.llm = LLMConfig(name="gpt-6.2-sol")
            self.llm_client = None
            self.compaction = SimpleNamespace(
                native=True, cold_compact_tokens=None, profile="expansive", token_threshold=100_000
            )
            self.subagents = SimpleNamespace(resolved=None, enabled=(), definitions=None)
            self.subsidiary_llm_clients = {}
            self.role_prompt_guidance = {}
            self.claude_code = False
            self.claude_code_admitted = None
            self.claude_code_pins = None
            self.search_api = "tavily"
            self.search_depth = "basic"

        def model_copy(self, *, deep: bool = False):
            return copy.deepcopy(self) if deep else copy.copy(self)

    resolved_clients_calls: list[tuple[str, str | None]] = []

    async def _record_resolve_clients(_cfg, _uid, effective_model, _pref, _roles, **kwargs):
        resolved_clients_calls.append((effective_model, kwargs.get("reasoning_effort")))

    from src.server.services.policy import Policy

    def _fake_policy():
        return Policy()

    route = TurnRoute(workspace_id=WS_ID, role="analyst", owner_id=USER_ID)
    identity = TurnIdentity(sender=USER_ID, owner=USER_ID, recorded_as=USER_ID)
    thread_row = {"llm_model": "gpt-6.2-sol", "reasoning_effort": "high", "fast_mode": False}

    get_pref_mock = create_autospec(user_models.get_model_preference, return_value={})
    with (
        patch.object(setup_mod, "agent_config", _BaseCfg()),
        patch("src.llms.llm.LLM.get_model_config", return_value=catalog),
        patch.object(llm_cfg, "require_policy", new=create_autospec(llm_cfg.require_policy, return_value=_fake_policy())),
        patch.object(llm_cfg, "policy_refuses", new=create_autospec(llm_cfg.policy_refuses, return_value=None)),
        patch.object(user_models, "get_model_preference", new=get_pref_mock),
        patch.object(llm_cfg, "gate_models_by_policy", new=create_autospec(llm_cfg.gate_models_by_policy, side_effect=lambda _u, pref, _rm, **_kw: pref)),
        patch.object(llm_cfg, "gate_models_by_plan", new=create_autospec(llm_cfg.gate_models_by_plan, side_effect=lambda _b, _u, pref, _rm, **_kw: pref)),
        patch.object(llm_cfg, "plan_covers", new=create_autospec(llm_cfg.plan_covers, return_value=True)),
        patch.object(llm_cfg, "load_user_context", new=create_autospec(llm_cfg.load_user_context, return_value=None)),
        patch.object(llm_cfg, "gate_capabilities", new=create_autospec(llm_cfg.gate_capabilities, return_value=None)),
        patch.object(llm_cfg, "check_model_available", new=create_autospec(llm_cfg.check_model_available, return_value=False)),
        patch.object(llm_cfg, "read_user_subagents", new=create_autospec(llm_cfg.read_user_subagents, return_value=([], {}, {}))),
        patch.object(llm_cfg, "resolve_clients", new=create_autospec(llm_cfg.resolve_clients, side_effect=_record_resolve_clients)),
    ):
        # 1) Queued Neon Low (reasoning_effort omitted) while thread row has gpt-6.2-sol + high -> stays Neon Low.
        resolver_queued_ag = thread_agent.thread_resolver(
            USER_ID,
            THREAD_ID,
            route,
            identity=identity,
            thread=thread_row,
            is_byok=False,
            reasoning_effort=None,
            preserve_antigravity_variant=True,
        )
        cfg_queued_ag = await resolver_queued_ag("gemini-4-neon-low-oauth")
        assert cfg_queued_ag.llm.name == "gemini-4-neon-low-oauth"
        assert resolved_clients_calls[-1] == ("gemini-4-neon-low-oauth", None)

        # 2) Explicit web Neon Low with preserve_antigravity_variant=True keeps Neon Low even if raw reasoning_effort='high' arrives.
        resolver_web_explicit = thread_agent.thread_resolver(
            USER_ID,
            THREAD_ID,
            route,
            identity=identity,
            thread=thread_row,
            is_byok=False,
            reasoning_effort="high",
            preserve_antigravity_variant=True,
        )
        cfg_web_explicit = await resolver_web_explicit("gemini-4-neon-low-oauth")
        assert cfg_web_explicit.llm.name == "gemini-4-neon-low-oauth"
        assert resolved_clients_calls[-1] == ("gemini-4-neon-low-oauth", None)

        # 3) Service dispatch (preserve_antigravity_variant=False) with explicit effort='high' still maps Neon Low -> Neon High.
        resolver_service = thread_agent.thread_resolver(
            USER_ID,
            THREAD_ID,
            route,
            identity=identity,
            thread=thread_row,
            is_byok=False,
            reasoning_effort="high",
            preserve_antigravity_variant=False,
        )
        cfg_service = await resolver_service("gemini-4-neon-low-oauth")
        assert cfg_service.llm.name == "gemini-4-neon-high-oauth"
        assert resolved_clients_calls[-1] == ("gemini-4-neon-high-oauth", None)

        # 4) Queued different standard model (claude-opus-4-6, effort omitted) does NOT inherit gpt-6.2-sol's 'high'.
        resolver_std_other = thread_agent.thread_resolver(
            USER_ID,
            THREAD_ID,
            route,
            identity=identity,
            thread=thread_row,
            is_byok=False,
            reasoning_effort=None,
        )
        cfg_std_other = await resolver_std_other("claude-opus-4-6")
        assert cfg_std_other.llm.name == "claude-opus-4-6"
        assert resolved_clients_calls[-1] == ("claude-opus-4-6", None)

        # 5) Following the thread's own standard model (gpt-6.2-sol, effort omitted) still inherits row's 'high'.
        cfg_std_same = await resolver_std_other("gpt-6.2-sol")
        assert cfg_std_same.llm.name == "gpt-6.2-sol"
        assert resolved_clients_calls[-1] == ("gpt-6.2-sol", "high")

        # 6) R4 Codex #1: Thread-following web request (preserve_antigravity_variant=True,
        #    request omitted llm_model -> resolved from thread row or default to Neon Low)
        #    with stale reasoning_effort='high' MUST preserve Neon Low instead of remapping to Neon High.
        ag_thread_row = {
            "llm_model": "gemini-4-neon-low-oauth",
            "reasoning_effort": None,
            "fast_mode": False,
        }
        resolver_web_follow_ag = thread_agent.thread_resolver(
            USER_ID,
            THREAD_ID,
            route,
            identity=identity,
            thread=ag_thread_row,
            is_byok=False,
            reasoning_effort="high",
            preserve_antigravity_variant=True,
        )
        cfg_web_follow_ag = await resolver_web_follow_ag("gemini-4-neon-low-oauth")
        assert cfg_web_follow_ag.llm.name == "gemini-4-neon-low-oauth"
        assert resolved_clients_calls[-1] == ("gemini-4-neon-low-oauth", None)

        # Also when request_model is None (account default is Neon Low) with preserve_antigravity_variant=True.
        get_pref_mock.return_value = {"preferred_model": "gemini-4-neon-low-oauth"}
        cfg_web_default_ag = await resolver_web_follow_ag(None)
        assert cfg_web_default_ag.llm.name == "gemini-4-neon-low-oauth"
        assert resolved_clients_calls[-1] == ("gemini-4-neon-low-oauth", None)


# ---------------------------------------------------------------------------
# R4 Codex #5: keep_named_model publishes thread_model when admitted turn updates llm_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_named_model_publishes_thread_model_only_when_conditional_write_changes_row():
    """R4 Codex #5: after an admitted turn persists a named model onto
    conversation_threads.llm_model, broadcast thread_model so other tabs that
    opened the thread before admission refresh their cached detail."""
    from src.server.handlers.chat import request_prep
    from src.server.services.llm.thread_model import NamedModel

    pub_model = create_autospec(thread_lifecycle_feed.publish_thread_model)
    meta_mock = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": USER_ID,
            "workspace_id": WS_ID,
            "workspace_owner_id": USER_ID,
        },
    )

    # 1) Conditional write changed llm_model (True) -> publishes thread_model.
    remember_changed = AsyncMock(return_value=True)
    with (
        patch.object(request_prep, "remember_thread_llm_model", new=remember_changed),
        patch.object(threads_read, "get_thread_auth_meta", new=meta_mock),
        patch.object(thread_lifecycle_feed, "publish_thread_model", new=pub_model),
    ):
        await request_prep.keep_named_model(
            THREAD_ID, NamedModel("gemini-4-neon-low-oauth", seen=None)
        )

    remember_changed.assert_awaited_once_with(
        THREAD_ID, "gemini-4-neon-low-oauth", seen=None
    )
    pub_model.assert_awaited_once_with(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        workspace_id=WS_ID,
        llm_model="gemini-4-neon-low-oauth",
    )

    # 2) Conditional write made no change (False: already same or another tab moved it) -> no broadcast.
    pub_model.reset_mock()
    remember_noop = AsyncMock(return_value=False)
    with (
        patch.object(request_prep, "remember_thread_llm_model", new=remember_noop),
        patch.object(threads_read, "get_thread_auth_meta", new=meta_mock),
        patch.object(thread_lifecycle_feed, "publish_thread_model", new=pub_model),
    ):
        await request_prep.keep_named_model(
            THREAD_ID, NamedModel("gemini-4-neon-low-oauth", seen=None)
        )
    pub_model.assert_not_awaited()

    # 3) Shared-workspace holder row -> no user-scoped thread_model broadcast.
    pub_model.reset_mock()
    holder_meta = create_autospec(
        threads_read.get_thread_auth_meta,
        return_value={
            "user_id": "shared_ws:org-1:ws-1",
            "workspace_id": WS_ID,
            "workspace_owner_id": "shared_ws:org-1:ws-1",
        },
    )
    with (
        patch.object(request_prep, "remember_thread_llm_model", new=AsyncMock(return_value=True)),
        patch.object(threads_read, "get_thread_auth_meta", new=holder_meta),
        patch.object(thread_lifecycle_feed, "publish_thread_model", new=pub_model),
        patch("src.server.database.shared_workspaces.is_holder", side_effect=lambda v: isinstance(v, str) and v.startswith("shared_ws:")),
    ):
        await request_prep.keep_named_model(
            THREAD_ID, NamedModel("gemini-4-neon-low-oauth", seen=None)
        )
    pub_model.assert_not_awaited()
