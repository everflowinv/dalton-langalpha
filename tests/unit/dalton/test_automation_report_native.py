"""Saved-report native plan and item-claim branches.

Dispatch, retry, and reconciliation are a later stage. These tests cover the
saved payload, the in-memory plan, and the claim authorization branch.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from src.dalton.database import automation_reports as reports
from src.dalton.database import data_task_notifications as dtn
from src.dalton.database.data_task_notifications import AuthorizedDataTaskTarget


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
