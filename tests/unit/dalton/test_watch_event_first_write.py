"""`dalton_watch_event` first-write behavior: agenda auto-create + one conflict retry."""

from __future__ import annotations

import json
from unittest.mock import create_autospec

import pytest

from src.dalton.knowledge import service as knowledge_service
from src.dalton.knowledge.ops import apply_ops
from src.dalton.knowledge.validate import validate_update
from src.dalton.auth import verify_workspace_access
from src.dalton.database import knowledge as db_knowledge
from src.dalton.tools.director_tools import build_director_tools

USER_ID = "user-1"
WS_ID = "ws-1"


def _cos_tools():
    return {t.name: t for t in build_director_tools(role="chief_of_staff")}


def _invoke(**kwargs):
    tool = _cos_tools()["dalton_watch_event"]
    return tool, tool.ainvoke(
        {
            "workspace_id": WS_ID,
            "type": "earnings",
            "date": "2026-11-18",
            "date_status": "estimated",
            "note": "FY27Q1 财报",
            "source": "https://ir.example.com",
            **kwargs,
        },
        config={"configurable": {"user_id": USER_ID, "thread_id": "thread-1"}},
    )


def _latest_row(version_no: int, events: list[dict]) -> dict:
    return {
        "version_no": version_no,
        "content": {"watch": {"events": events}},
    }


@pytest.fixture
def access_allowed(monkeypatch: pytest.MonkeyPatch):
    spec = create_autospec(verify_workspace_access, return_value=None)
    monkeypatch.setattr(
        "src.dalton.tools.director_tools.verify_workspace_access", spec
    )
    return spec


@pytest.mark.asyncio
async def test_first_write_creates_schema_valid_agenda(
    monkeypatch: pytest.MonkeyPatch, access_allowed
):
    """No agenda yet: one apply_update with a create op whose document passes the agenda schema."""
    captured: list[dict] = []

    async def fake_apply_update(*, ops, base_version, **kwargs):
        captured.append({"ops": ops, "base_version": base_version})
        # Run the real op pipeline so schema validity is proven, not assumed.
        doc, _ = apply_ops("agenda", None, ops, reason="test")
        val = validate_update("agenda", None, doc, source="tool", reason="test")
        assert val.ok, val.format_problems()
        return {"ok": True, "doc": "agenda", "version_no": 1}

    monkeypatch.setattr(db_knowledge, "latest_version", create_autospec(
        db_knowledge.latest_version, return_value=None
    ))
    monkeypatch.setattr(knowledge_service, "apply_update", fake_apply_update)

    tool, coro = _invoke()
    res = json.loads(await coro)

    assert res["ok"] is True
    assert res["event_id"] == "W1"
    assert res["action"] == "created"
    assert len(captured) == 1
    assert captured[0]["base_version"] is None
    create_ops = [op for op in captured[0]["ops"] if op["op"] == "create"]
    assert len(create_ops) == 1
    assert create_ops[0]["value"]["watch"]["events"][0]["id"] == "W1"


@pytest.mark.asyncio
async def test_concurrent_first_write_retries_once_as_upsert(
    monkeypatch: pytest.MonkeyPatch, access_allowed
):
    """A losing concurrent first write re-reads the new latest and retries as a plain upsert."""

    async def fake_latest(workspace_id, doc_key):
        if fake_latest.calls == 0:
            fake_latest.calls += 1
            return None
        fake_latest.calls += 1
        return _latest_row(
            1,
            [
                {
                    "id": "W1",
                    "type": "earnings",
                    "date": "2026-11-18",
                    "date_status": "estimated",
                    "note": "FY27Q1 财报",
                }
            ],
        )

    fake_latest.calls = 0

    calls: list[dict] = []

    async def fake_apply_update(*, ops, base_version, **kwargs):
        calls.append({"ops": ops, "base_version": base_version})
        if len(calls) == 1:
            return {"ok": False, "error": "version_conflict", "latest_version": 1}
        return {"ok": True, "doc": "agenda", "version_no": 2}

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(knowledge_service, "apply_update", fake_apply_update)

    tool, coro = _invoke()
    res = json.loads(await coro)

    assert res["ok"] is True
    assert res["action"] == "updated"
    assert len(calls) == 2
    assert calls[0]["ops"][0]["op"] == "create"
    assert calls[1]["ops"][0]["op"] == "upsert_item"
    assert calls[1]["base_version"] == 1


@pytest.mark.asyncio
async def test_persistent_conflict_is_returned_as_is(
    monkeypatch: pytest.MonkeyPatch, access_allowed
):
    """Both attempts conflict: the second conflict comes back untouched (no third try)."""

    async def fake_latest(workspace_id, doc_key):
        return _latest_row(
            1,
            [{"id": "W1", "type": "earnings", "date": None, "date_status": "unknown"}],
        )

    apply_spec = create_autospec(
        knowledge_service.apply_update,
        return_value={"ok": False, "error": "version_conflict", "latest_version": 2},
    )
    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest)
    monkeypatch.setattr(knowledge_service, "apply_update", apply_spec)

    tool, coro = _invoke(date="2026-12-02")
    res = json.loads(await coro)

    assert res["ok"] is False
    assert res["error"] == "version_conflict"
    assert apply_spec.await_count == 1


@pytest.mark.asyncio
async def test_existing_agenda_keeps_upsert_shape(
    monkeypatch: pytest.MonkeyPatch, access_allowed
):
    """Agenda present: one plain upsert against the read version, decorated as before."""

    existing = _latest_row(
        4,
        [
            {
                "id": "W1",
                "type": "earnings",
                "date": "2026-11-18",
                "date_status": "estimated",
                "note": "FY27Q1 财报",
            }
        ],
    )
    latest_spec = create_autospec(db_knowledge.latest_version, return_value=existing)
    apply_spec = create_autospec(
        knowledge_service.apply_update,
        return_value={"ok": True, "doc": "agenda", "version_no": 5},
    )
    monkeypatch.setattr(db_knowledge, "latest_version", latest_spec)
    monkeypatch.setattr(knowledge_service, "apply_update", apply_spec)

    tool, coro = _invoke(date="2026-11-19")
    res = json.loads(await coro)

    assert res["ok"] is True
    assert res["action"] == "updated"
    assert res["event_id"] == "W1"
    assert "原 11/18" in res["event"]["note"]
    assert apply_spec.await_count == 1
    _, call_kwargs = apply_spec.await_args
    assert call_kwargs["ops"][0]["op"] == "upsert_item"
    assert call_kwargs["base_version"] == 4
