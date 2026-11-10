"""Saved-report native plan, item claim, and gateway dispatch."""

from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from src.dalton.database import automation_reports as reports
from src.dalton.database import data_task_notifications as dtn
from src.dalton.database.data_task_notifications import AuthorizedDataTaskTarget
from src.dalton.database.verdicts import get_verdict_for_execution
from src.dalton.llm.model_prefs import (
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_targets,
)
from src.dalton.notify import dispatch as notify_dispatch
from src.dalton.notify.channel_transport import post_data_task_gateway_notify
from src.dalton.settings import get_dalton_frontend_url


def _card(content: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "Digest"}},
        "body": {"elements": [{"tag": "markdown", "content": content}]},
    }


def _report_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source": "automation_report",
        "content_sha256": "abc123",
        "expected_item_count": 2,
        "content_kind": "cards",
        "profile": "market-digest-am",
        "cards": [_card("First"), _card("Second")],
        "target_snapshot": ["feishu:chat:oc_alpha", "feishu:chat:oc_beta"],
    }
    payload.update(overrides)
    return payload


def _automation(report_targets: list[str]) -> dict[str, Any]:
    return {
        "metadata": {
            "report_profile": {
                "profile": "market-digest-am",
                "report_targets": report_targets,
                "failure_targets": ["discord:user:123456789012345678"],
            }
        }
    }


def test_native_items_keep_card_order_and_carry_no_images() -> None:
    original = _report_payload()
    items, err = reports.native_items_from_report_payload(original)
    assert err is None
    assert items is not None
    assert [item["item_index"] for item in items] == [0, 1]
    assert [item["kind"] for item in items] == ["feishu_card", "feishu_card"]
    assert [item["card"]["body"]["elements"][0]["content"] for item in items] == [
        "First",
        "Second",
    ]
    assert all(item["images"] == [] and "artifact" not in item for item in items)
    items[0]["card"]["body"]["elements"][0]["content"] = "changed"
    assert original["cards"][0]["body"]["elements"][0]["content"] == "First"

    broken = _report_payload(expected_item_count=1)
    none_items, code = reports.native_items_from_report_payload(broken)
    assert none_items is None
    assert code == "payload_invalid"

    text_items, text_err = reports.native_items_from_report_payload(
        {
            "source": "automation_report",
            "content_sha256": "abc123",
            "expected_item_count": 1,
            "content_kind": "failure_text",
            "failure_text": "The digest failed closed.",
        }
    )
    assert text_err is None
    assert text_items == [
        {"item_index": 0, "kind": "text", "text": "The digest failed closed."}
    ]


def test_saved_target_removal_does_not_rewrite_the_snapshot() -> None:
    payload = _report_payload()
    ok, code = reports.saved_report_target_still_configured(
        _automation(["feishu:chat:oc_alpha", "feishu:chat:oc_beta"]),
        payload,
        "feishu:chat:oc_alpha",
    )
    assert ok is True
    assert code == ""
    removed, removed_code = reports.saved_report_target_still_configured(
        _automation(["feishu:chat:oc_alpha"]),
        payload,
        "feishu:chat:oc_beta",
    )
    assert removed is False
    assert removed_code == "target_removed"
    assert payload["target_snapshot"] == [
        "feishu:chat:oc_alpha",
        "feishu:chat:oc_beta",
    ]
    invalid, invalid_code = reports.saved_report_target_still_configured(
        _automation(["feishu:chat:oc_alpha"]),
        _report_payload(profile="market-digest-pm"),
        "feishu:chat:oc_alpha",
    )
    assert invalid is False
    assert invalid_code == "profile_invalid"


def test_report_items_become_an_in_memory_plan_without_artifacts() -> None:
    items, err = reports.native_items_from_report_payload(_report_payload())
    assert err is None
    run_id = str(uuid4())
    job_key = str(uuid4())
    plan = dtn._resolved_plan_from_report_items(
        run_id=run_id,
        job_key=job_key,
        items=items or [],
    )
    assert plan is not None
    assert plan.run_id == run_id
    assert plan.job_key == job_key
    assert plan.artifacts_by_id == {}
    assert [item.kind for item in plan.items] == ["feishu_card", "feishu_card"]
    assert all(item.images == () and item.artifact is None for item in plan.items)
    wired = [item.to_wire_dict(delivery_key=f"key-{item.item_index}") for item in plan.items]
    assert [item["images"] for item in wired] == [[], []]
    assert all("artifact" not in item for item in wired)

    decision = dtn._authorized_target_from_report(
        {
            "scope": "report",
            "allowed": True,
            "run_id": run_id,
            "job_key": job_key,
            "owner_user_id": "owner",
            "owner_platform_user_id": None,
            "platform": "feishu",
            "target_kind": "group",
            "requested_target": "feishu:chat:oc_alpha",
            "canonical_target_address": "feishu:oc_alpha",
            "recipient_id": "oc_alpha",
            "items": items,
        }
    )
    assert decision.allowed is True
    assert decision.create_outer_row is False
    assert decision.plan is not None
    assert decision.plan.artifacts_by_id == {}
    denied = dtn._authorized_target_from_report(
        {
            "scope": "report",
            "allowed": False,
            "deny_status": "skipped",
            "deny_code": "target_not_routed",
            "deny_message": "missing",
        }
    )
    assert denied.allowed is False
    assert denied.create_outer_row is False
    assert denied.deny_code == "target_not_routed"
    assert denied.plan is None


def test_report_candidate_matches_only_the_saved_target() -> None:
    assert (
        dtn._is_candidate_outer_delivery_row(
            {
                "target": "feishu:user:ou_saved",
                "payload_json": {"source": "automation_report"},
            },
            platform="feishu",
            target_kind="dm",
            raw_ident="ou_other",
            requested_target="feishu:user:ou_other",
            canonical_target_address="feishu:@me",
        )
        is False
    )
    assert (
        dtn._is_candidate_outer_delivery_row(
            {
                "target": "feishu:user:ou_saved",
                "payload_json": {"source": "automation_report"},
            },
            platform="feishu",
            target_kind="dm",
            raw_ident="ou_saved",
            requested_target="feishu:user:ou_saved",
            canonical_target_address="feishu:@me",
        )
        is True
    )
    assert (
        dtn._is_candidate_outer_delivery_row(
            {"target": "feishu:user:ou_saved", "payload_json": {}},
            platform="feishu",
            target_kind="dm",
            raw_ident="ou_other",
            requested_target="feishu:user:ou_other",
            canonical_target_address="feishu:@me",
        )
        is True
    )


class _Cursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, sql: str, _params: Any = None) -> None:
        self.statements.append(" ".join(str(sql).split()))

    async def fetchone(self) -> None:
        return None

    async def fetchall(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_report_rows_are_not_replaced_by_a_data_insert() -> None:
    cursor = _Cursor()
    result = await dtn._persist_reconcile_identity_on_outer_delivery(
        cursor,
        run_id=str(uuid4()),
        platform="feishu",
        requested_target="feishu:chat:oc_alpha",
        canonical_target_address="feishu:oc_alpha",
        recipient_id="oc_alpha",
        delivery_keys=["data_task:run:feishu:oc_alpha:item:0"],
        job_key=str(uuid4()),
        owner_user_id="owner",
        create_missing=False,
    )
    assert result.ok is False
    assert result.code == "outer_delivery_missing"
    assert not any("INSERT INTO dalton_notify_deliveries" in sql for sql in cursor.statements)


@pytest.mark.asyncio
async def test_report_scope_does_not_fall_through_to_data_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    authorize_report = reports.authorize_saved_report_target
    execution_id = str(uuid4())
    monkeypatch.setattr(
        dtn,
        "get_dalton_settings",
        create_autospec(
            dtn.get_dalton_settings,
            return_value=SimpleNamespace(enabled=True),
        ),
    )

    async def _report_deny(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "scope": "report",
            "allowed": False,
            "deny_status": "skipped",
            "deny_code": "target_not_routed",
            "deny_message": "Requested target is not in the saved report batch.",
        }

    monkeypatch.setattr(
        reports,
        "authorize_saved_report_target",
        create_autospec(authorize_report, side_effect=_report_deny),
    )
    denied = await dtn._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=execution_id,
        target="feishu:chat:oc_alpha",
    )
    assert denied.allowed is False
    assert denied.deny_code == "target_not_routed"
    assert denied.create_outer_row is False
    assert not any("dalton_data_task_runs" in sql for sql in cursor.statements)

    async def _no_report(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"scope": "none"}

    monkeypatch.setattr(
        reports,
        "authorize_saved_report_target",
        create_autospec(authorize_report, side_effect=_no_report),
    )
    missing = await dtn._authorize_run_and_target_in_tx(
        cursor,
        object(),
        run_id=execution_id,
        target="feishu:chat:oc_alpha",
    )
    assert missing.deny_code == "run_not_found"
    assert any("dalton_data_task_runs" in sql for sql in cursor.statements)


@pytest.mark.asyncio
async def test_plan_and_claim_lock_the_saved_task_before_outer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class _Conn:
        def transaction(self) -> _Conn:
            return self

        async def __aenter__(self) -> _Conn:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def cursor(self, **_kwargs: Any) -> _CursorCm:
            return _CursorCm()

    class _CursorCm:
        async def __aenter__(self) -> _Cursor:
            return _Cursor()

        async def __aexit__(self, *_args: object) -> None:
            return None

    @asynccontextmanager
    async def _connection(_conn: Any = None) -> Any:
        yield _Conn()

    async def _lock(_cur: Any, _execution_id: str) -> bool:
        order.append("automation")
        return True

    async def _outer(_cur: Any, *, run_id: str, target: str) -> None:
        order.append("outer")

    async def _auth(_cur: Any, _conn: Any, *, run_id: str, target: str, **_kwargs: Any) -> AuthorizedDataTaskTarget:
        order.append("authorize")
        return AuthorizedDataTaskTarget(
            allowed=False,
            deny_code="target_not_routed",
            deny_message="missing",
        )

    monkeypatch.setattr(dtn, "get_db_connection", _connection)
    monkeypatch.setattr(
        reports,
        "lock_saved_report_automation",
        create_autospec(reports.lock_saved_report_automation, side_effect=_lock),
    )
    monkeypatch.setattr(
        dtn,
        "_acquire_outer_identity_xact_lock",
        create_autospec(dtn._acquire_outer_identity_xact_lock, side_effect=_outer),
    )
    monkeypatch.setattr(
        dtn,
        "_authorize_run_and_target_in_tx",
        create_autospec(dtn._authorize_run_and_target_in_tx, side_effect=_auth),
    )
    run_id = str(uuid4())
    plan = await dtn.authorize_data_task_notification_plan(
        run_id=run_id,
        target="feishu:chat:oc_alpha",
    )
    claim = await dtn.authorize_and_claim_data_task_delivery(
        delivery_key="data_task:key",
        run_id=run_id,
        target="feishu:chat:oc_alpha",
    )
    assert plan["code"] == "target_not_routed"
    assert claim["code"] == "target_not_routed"
    assert order == ["automation", "outer", "authorize", "automation", "outer", "authorize"]


def _saved_row(target: str, **overrides: Any) -> dict[str, Any]:
    payload = _report_payload(
        target_snapshot=[target],
        expected_item_count=1,
        cards=[_card("Only")],
    )
    row: dict[str, Any] = {
        "execution_id": "exec-saved",
        "automation_id": "auto-saved",
        "user_id": "owner",
        "channel": "feishu",
        "target": target,
        "status": "pending",
        "error_code": None,
        "attempt_count": 0,
        "payload_json": payload,
    }
    row.update(overrides)
    return row


def test_dispatch_automation_report_accepts_keyword_injections() -> None:
    signature = inspect.signature(notify_dispatch.dispatch_automation_report)
    assert list(signature.parameters)[0] == "execution_id"
    for name in (
        "conn",
        "notify_fn",
        "reconcile_row_fn",
        "claim_fn",
        "mark_fn",
        "authorize_fn",
        "load_rows_fn",
        "max_attempts",
        "resolve_enabled_fn",
        "is_admin_fn",
    ):
        assert signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    claim_signature = inspect.signature(notify_dispatch.claim_saved_report_delivery)
    assert "payload" not in claim_signature.parameters
    assert claim_signature.parameters["conn"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_saved_report_claim_updates_without_inserting_or_replacing_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    class _Cursor:
        async def __aenter__(self) -> _Cursor:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, sql: str, _params: Any = None) -> None:
            executed.append(" ".join(sql.split()))

        async def fetchone(self) -> dict[str, int] | None:
            return {"delivery_id": 1, "attempt_count": 1}

    class _Connection:
        def cursor(self, **_kwargs: Any) -> _Cursor:
            return _Cursor()

    @asynccontextmanager
    async def _connection(_conn: Any = None) -> Any:
        yield _Connection()

    actual_get_connection = notify_dispatch.get_db_connection
    monkeypatch.setattr(
        notify_dispatch,
        "get_db_connection",
        create_autospec(actual_get_connection, side_effect=_connection),
    )
    claimed, attempt = await notify_dispatch.claim_saved_report_delivery(
        "exec-saved",
        "feishu",
        "feishu:chat:oc_alpha",
    )
    assert (claimed, attempt) == (True, 1)
    assert len(executed) == 1
    statement = executed[0]
    assert statement.upper().startswith("UPDATE ")
    assert "INSERT" not in statement.upper()
    assert "automation_report" in statement
    assert "payload_json =" not in statement
    assert "attempt_count = 0" in statement
    assert "RETURNING delivery_id, attempt_count" in statement

    class _Missing(_Cursor):
        async def fetchone(self) -> None:
            return None

    class _MissingConnection:
        def cursor(self, **_kwargs: Any) -> _Missing:
            return _Missing()

    @asynccontextmanager
    async def _missing_connection(_conn: Any = None) -> Any:
        yield _MissingConnection()

    monkeypatch.setattr(
        notify_dispatch,
        "get_db_connection",
        create_autospec(actual_get_connection, side_effect=_missing_connection),
    )
    assert await notify_dispatch.claim_saved_report_delivery(
        "exec-missing",
        "feishu",
        "feishu:chat:oc_missing",
    ) == (False, None)


def _injection_bundle(state: list[dict[str, Any]], *, ledger_ready: bool) -> dict[str, Any]:
    execution_id = str(state[0]["execution_id"])

    async def load(execution_id_arg: str, conn: Any = None) -> list[dict[str, Any]]:
        assert execution_id_arg == execution_id
        assert conn is None
        return [dict(row) for row in state]

    async def authorize(*, run_id: str, target: str, **_kwargs: Any) -> dict[str, Any]:
        assert run_id == execution_id
        return {"authorized": True, "run_id": run_id, "requested_target": target}

    async def claim(
        execution_id_arg: str,
        channel: str,
        target: str,
        *,
        max_attempts: int = 3,
        stale_pending_minutes: int = 10,
    ) -> tuple[bool, int | None]:
        assert execution_id_arg == execution_id
        row = state[0]
        if (
            row["status"] == "pending"
            and int(row["attempt_count"]) == 0
            and str(row["target"]) == target
            and str(row["channel"]) == channel
        ):
            row["attempt_count"] = 1
            return True, 1
        return False, None

    async def mark(
        execution_id_arg: str,
        channel: str,
        target: str,
        *,
        status: str,
        error_code: str | None = None,
        reason: str | None = None,
        expected_status: Any = None,
        expected_attempt_count: int | None = None,
        expected_updated_at: Any = None,
        exclude_uncertain_data_task: bool = False,
    ) -> bool:
        _ = (
            reason,
            expected_status,
            expected_attempt_count,
            expected_updated_at,
            exclude_uncertain_data_task,
        )
        assert execution_id_arg == execution_id
        row = state[0]
        assert row["channel"] == channel
        assert row["target"] == target
        row["status"] = status
        row["error_code"] = error_code
        return True

    async def reconcile(row: dict[str, Any], **kwargs: Any) -> bool:
        assert kwargs.get("allow_retry_candidate_statuses") is True
        if ledger_ready:
            state[0]["status"] = "sent"
            state[0]["error_code"] = None
            return True
        return False

    async def notify(
        targets: list[str],
        text: str = "",
        card: dict[str, Any] | None = None,
        *,
        images: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        gateway_url: str | None = None,
        service_token: str | None = None,
        timeout_s: float | None = None,
        http_client: Any = None,
    ) -> dict[str, Any]:
        assert text == ""
        assert card is None
        assert images is None
        assert gateway_url is None
        assert service_token is None
        assert http_client is None
        assert targets == [state[0]["target"]]
        assert run_id == execution_id
        assert timeout_s is not None and timeout_s > 0
        return {
            "ok": True,
            "status": "sent",
            "results": [{"target": targets[0], "ok": True, "status": "sent"}],
        }

    return {
        "load_rows_fn": create_autospec(reports.load_execution_report_rows, side_effect=load),
        "authorize_fn": create_autospec(
            dtn.authorize_data_task_notification_plan,
            side_effect=authorize,
        ),
        "claim_fn": create_autospec(
            notify_dispatch.claim_saved_report_delivery,
            side_effect=claim,
        ),
        "mark_fn": create_autospec(notify_dispatch.mark_target_delivery, side_effect=mark),
        "reconcile_row_fn": create_autospec(
            dtn.reconcile_single_data_task_delivery_row,
            side_effect=reconcile,
        ),
        "notify_fn": create_autospec(post_data_task_gateway_notify, side_effect=notify),
    }


@pytest.mark.asyncio
async def test_dispatch_confirms_a_saved_target_only_after_the_ledger_is_complete() -> None:
    target = "feishu:chat:oc_alpha"
    state = [_saved_row(target)]
    ready = {"done": False}
    bundle = _injection_bundle(state, ledger_ready=False)

    async def reconcile(row: dict[str, Any], **kwargs: Any) -> bool:
        assert kwargs.get("allow_retry_candidate_statuses") is True
        if ready["done"]:
            state[0]["status"] = "sent"
            state[0]["error_code"] = None
            return True
        return False

    bundle["reconcile_row_fn"] = create_autospec(
        dtn.reconcile_single_data_task_delivery_row,
        side_effect=reconcile,
    )

    async def notify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        ready["done"] = True
        assert kwargs.get("run_id") == state[0]["execution_id"]
        assert kwargs.get("timeout_s") is not None
        assert "text" not in kwargs
        assert "card" not in kwargs
        assert args[0] == [target]
        return {
            "ok": True,
            "status": "sent",
            "results": [{"target": target, "ok": True, "status": "sent"}],
        }

    bundle["notify_fn"] = create_autospec(post_data_task_gateway_notify, side_effect=notify)
    first = await notify_dispatch.dispatch_automation_report(
        state[0]["execution_id"],
        **bundle,
    )
    assert first["ok"] is True
    assert first["delivery_confirmed"] is True
    assert first["status"] == "sent"
    assert first["execution_id"] == state[0]["execution_id"]
    assert first["payload"]["cards"] == state[0]["payload_json"]["cards"]
    bundle["notify_fn"].assert_called_once()
    again = await notify_dispatch.dispatch_automation_report(
        state[0]["execution_id"],
        **bundle,
    )
    assert again["delivery_confirmed"] is True
    bundle["notify_fn"].assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_does_not_confirm_accepted_or_uncertain_transport() -> None:
    target = "feishu:chat:oc_alpha"
    state = [_saved_row(target)]
    bundle = _injection_bundle(state, ledger_ready=False)

    async def notify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "status": "accepted", "results": [{"target": target, "ok": True, "status": "accepted"}]}

    bundle["notify_fn"] = create_autospec(post_data_task_gateway_notify, side_effect=notify)
    accepted = await notify_dispatch.dispatch_automation_report(
        state[0]["execution_id"],
        **bundle,
    )
    assert accepted["delivery_confirmed"] is False
    assert state[0]["status"] == "failed"
    assert state[0]["error_code"] == "accepted"
    bundle["notify_fn"].assert_called_once()

    uncertain_state = [
        _saved_row(target, status="pending", error_code="unknown", attempt_count=1)
    ]
    uncertain_bundle = _injection_bundle(uncertain_state, ledger_ready=False)
    result = await notify_dispatch.dispatch_automation_report(
        uncertain_state[0]["execution_id"],
        **uncertain_bundle,
    )
    assert result["delivery_confirmed"] is False
    assert result["status"] == "unknown"
    uncertain_bundle["notify_fn"].assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_missing_batch_does_not_send() -> None:
    async def load(execution_id: str, conn: Any = None) -> list[dict[str, Any]]:
        _ = (execution_id, conn)
        return []

    notify = create_autospec(post_data_task_gateway_notify)
    view = await notify_dispatch.dispatch_automation_report(
        "exec-missing",
        load_rows_fn=create_autospec(reports.load_execution_report_rows, side_effect=load),
        notify_fn=notify,
    )
    assert view["ok"] is False
    assert view["code"] == "not_found"
    assert view["delivery_confirmed"] is False
    notify.assert_not_called()


@pytest.mark.asyncio
async def test_validated_report_profile_settles_without_generic_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def reader(execution_id: str, conn: Any = None) -> bool:
        _ = conn
        seen.append(execution_id)
        return execution_id == "exec-confirmed"

    monkeypatch.setattr(
        reports,
        "read_execution_report_confirmed",
        create_autospec(reports.read_execution_report_confirmed, side_effect=reader),
    )
    post = create_autospec(
        notify_dispatch.post_bridge_notify,
        side_effect=AssertionError("generic notification"),
    )
    automation = {
        "name": "AM digest",
        "user_id": "owner",
        "automation_id": str(uuid4()),
        "metadata": {
            "report_profile": {
                "profile": "market-digest-am",
                "report_targets": ["feishu:chat:oc_alpha"],
                "failure_targets": ["discord:user:123456789012345678"],
            }
        },
    }
    assert await notify_dispatch.on_automation_settled(
        automation=automation,
        execution_id="exec-open",
        final_text="This generic text must not be sent.",
        post_notify_fn=post,
    ) is False
    assert await notify_dispatch.on_automation_settled(
        automation=automation,
        execution_id="exec-confirmed",
        final_text="This generic text must not be sent.",
        post_notify_fn=post,
    ) is True
    assert seen == ["exec-open", "exec-confirmed"]
    post.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_settlement_still_sends_generic_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reader(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("report reader")

    monkeypatch.setattr(
        reports,
        "read_execution_report_confirmed",
        create_autospec(reports.read_execution_report_confirmed, side_effect=reader),
    )
    enabled = create_autospec(resolve_dalton_notify_enabled, return_value=True)
    overrides = create_autospec(resolve_dalton_notify_overrides, return_value={})
    targets = create_autospec(
        resolve_dalton_notify_targets,
        return_value=["feishu:user:ou_analyst_a"],
    )
    routes = create_autospec(notify_dispatch.resolve_dalton_notify_routes, return_value={})
    admin = create_autospec(notify_dispatch._default_notify_admin, return_value=False)
    verdict = create_autospec(
        get_verdict_for_execution,
        return_value={"word": "THESIS_WEAKENED"},
    )
    claim = create_autospec(notify_dispatch.claim_target_delivery, return_value=True)
    mark = create_autospec(notify_dispatch.mark_target_delivery, return_value=None)
    post = create_autospec(
        notify_dispatch.post_bridge_notify,
        return_value={
            "ok": True,
            "results": [{"target": "feishu:user:ou_analyst_a", "ok": True}],
        },
    )
    url = create_autospec(get_dalton_frontend_url, return_value="https://dalton.example.com")
    pushed = await notify_dispatch.on_automation_settled(
        automation={"name": "dalton-googl-move-down", "user_id": "u-1"},
        execution_id="exec-ordinary",
        thread_id="th-ordinary",
        final_text="核心客户订单下滑。短期营收面临下修风险。建议跟踪后续指引。",
        resolve_enabled_fn=enabled,
        resolve_overrides_fn=overrides,
        resolve_targets_fn=targets,
        resolve_routes_fn=routes,
        is_admin_fn=admin,
        get_verdict_fn=verdict,
        claim_target_fn=claim,
        mark_target_fn=mark,
        post_notify_fn=post,
        frontend_url_fn=url,
    )
    assert pushed is True
    post.assert_called_once()
