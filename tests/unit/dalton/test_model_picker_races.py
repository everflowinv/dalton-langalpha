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
