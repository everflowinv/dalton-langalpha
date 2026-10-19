"""Unit tests for P0J.J3 smuggled-code cleanup and user-ID branch removal."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import psycopg
import pytest

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.budget import gate as gate_mod
from src.dalton.database import llm_journal as journal_mod
from src.dalton.database import verdicts as verdicts_mod
from src.dalton.knowledge import service as knowledge_service
from src.dalton.knowledge.validate import (
    _load_schemas_and_registry,
    get_validator,
    validate_schema,
    validate_update,
)
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm.providers.antigravity import client as ag_client
from src.dalton.llm.role_policy import DaltonStartupConfigError, RolePolicyError
from src.dalton.publish import review as review_mod

REPO_ROOT = Path(__file__).resolve().parents[3]


class _CursorProto:
    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...
    async def execute(self, query: Any, params: Any = None) -> Any: ...
    async def fetchone(self) -> Any: ...
    async def fetchall(self) -> Any: ...


class _TxProto:
    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...


class _ConnProto:
    async def __aenter__(self) -> Any: ...
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...
    def cursor(self, *args: Any, **kwargs: Any) -> Any: ...
    def transaction(self) -> Any: ...


def _make_spec_conn(cur: Any) -> Any:
    conn = create_autospec(_ConnProto, instance=True)
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    tx = create_autospec(_TxProto, instance=True)
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = None
    conn.transaction.return_value = tx
    conn.cursor.return_value = cur
    return conn


@pytest.mark.asyncio
async def test_verdicts_two_workspaces_same_day_no_id_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    """J3 §2.1: Two workspaces writing a verdict on the same day get distinct VD-YYYYMMDD-NN IDs without PK collision."""
    inserted_ids: list[str] = []

    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    last_op = {"kind": ""}

    async def _exec(sql: str, params: Any = None) -> None:
        s = " ".join(str(sql).split())
        if "SELECT verdict_id FROM dalton_verdicts" in s:
            last_op["kind"] = "select_ids"
        elif "INSERT INTO dalton_verdicts" in s:
            last_op["kind"] = "insert"
            vid = str(params[0])
            assert vid not in inserted_ids, f"Duplicate verdict_id {vid}"
            inserted_ids.append(vid)
            last_op["row"] = {
                "verdict_id": vid,
                "user_id": params[1],
                "workspace_id": params[2],
                "verdict_at": params[3],
                "word": params[4],
            }
        else:
            last_op["kind"] = "lock"

    async def _fetchall() -> list[dict[str, Any]]:
        if last_op["kind"] == "select_ids":
            return [{"verdict_id": vid} for vid in inserted_ids]
        return []

    async def _fetchone() -> dict[str, Any] | None:
        if last_op["kind"] == "insert":
            return dict(last_op["row"])
        return None

    cur.execute.side_effect = _exec
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone

    conn = _make_spec_conn(cur)
    monkeypatch.setattr(verdicts_mod, "get_db_connection", lambda: conn)

    v_payload = {
        "at": "2026-10-19T12:00:00Z",
        "trigger": {"kind": "filing", "ref": "0001-26-000001"},
        "event": "Q3 10-Q filed",
        "verdict": "NO_CHANGE",
        "progress": "confirmed",
    }
    r1 = await verdicts_mod.insert_verdict(
        user_id="user-a",
        workspace_id="ws-alpha",
        verdict=v_payload,
    )
    r2 = await verdicts_mod.insert_verdict(
        user_id="user-b",
        workspace_id="ws-beta",
        verdict=v_payload,
    )
    assert r1["verdict_id"] == "VD-20261019-01"
    assert r2["verdict_id"] == "VD-20261019-02"
    assert r1["verdict_id"] != r2["verdict_id"]


def test_validate_malformed_thresholds_input_does_not_crash() -> None:
    """J3 §2.2: Malformed pillar thresholds (e.g. list or string instead of dict) emit soft warning without crashing."""
    malformed_thesis: dict[str, Any] = {
        "schema_version": 1,
        "as_of": "2026-10-19T00:00:00Z",
        "one_liner": "Test malformed thresholds",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "high",
            "summary": "Summary",
            "updated_at": "2026-10-19T00:00:00Z",
        },
        "house_view_history": [],
        "pillars": [
            {
                "id": "P1",
                "claim": "Cloud margin expansion",
                "priority": "core",
                "baseline": {"text": "25%", "as_of": "2026-06-30"},
                "expected_path": "30% by FY27",
                "thresholds": ["not-a-dict"],
                "status": "intact",
                "provenance": "draft",
                "created_at": "2026-10-19T00:00:00Z",
            }
        ],
        "evidence": [],
        "official": None,
    }
    res = validate_update("thesis", None, malformed_thesis, reason="test malformed threshold")
    assert any(w["code"] == "missing_break_threshold" for w in res.warnings)
    assert any(e["code"] == "schema_error" and "thresholds" in e["path"] for e in res.errors)


@pytest.mark.asyncio
async def test_record_journal_replay_and_complete_debug_logging(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """J3 §2.3: record_journal_replay updates usage.replay_count and complete.py logs replay errors at DEBUG."""
    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None
    executed: list[tuple[str, Any]] = []

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(str(sql).split()), params))

    cur.execute.side_effect = _exec
    conn = _make_spec_conn(cur)

    await journal_mod.record_journal_replay("inv-123", conn=conn)
    assert len(executed) == 1
    assert "replay_count" in executed[0][0]
    assert executed[0][1] == ("inv-123",)

    # Verify complete.py logs at debug level if record_journal_replay fails
    mock_inspect = create_autospec(complete_mod.inspect_journal_state)
    mock_inspect.return_value = (
        "done",
        {
            "invocation_id": "inv-123",
            "model_key": "deepseek-flash",
            "provider": "deepseek",
            "billing_type": "cash",
            "response": {"kind": "text", "text": "cached-ok"},
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "notional_usd": "0.001",
            "attempt": 1,
        },
    )
    mock_replay = create_autospec(complete_mod.record_journal_replay)
    mock_replay.side_effect = RuntimeError("db replay increment failed")
    monkeypatch.setattr(complete_mod, "inspect_journal_state", mock_inspect)
    monkeypatch.setattr(complete_mod, "record_journal_replay", mock_replay)

    with caplog.at_level(logging.DEBUG, logger=complete_mod.logger.name):
        res = await complete_mod.dalton_complete(
            None,
            user_id="user-1",
            user_prompt="ping",
            purpose="unit_test",
            request_model="deepseek-flash",
        )
    assert res.ok is True
    assert res.replayed is True
    assert any("record_journal_replay failed" in rec.message for rec in caplog.records)


def test_schemas_remove_guidance_keep_company_guidance_and_legacy_docs_pass() -> None:
    """J3 §2.4: common.schema.json label enum removes 'guidance', keeps 'company_guidance', and legacy docs pass."""
    _load_schemas_and_registry.cache_clear()
    get_validator.cache_clear()

    fig_valid = {
        "metric": "capex",
        "value": 220.0,
        "unit": "USD_bn",
        "period": "LTM",
        "label": "company_guidance",
    }
    fig_invalid = {
        **fig_valid,
        "label": "guidance",
    }
    base_thesis = {
        "schema_version": 1,
        "as_of": "2026-10-19T00:00:00Z",
        "one_liner": "Test schema",
        "aggregate_status": "intact",
        "house_view": {
            "stance": "long",
            "conviction": "high",
            "summary": "Summary",
            "updated_at": "2026-10-19T00:00:00Z",
        },
        "house_view_history": [],
        "pillars": [
            {
                "id": "P1",
                "claim": "Claim",
                "priority": "core",
                "baseline": {"text": "Base", "figure": fig_valid, "as_of": "2026-06-30"},
                "expected_path": "Path",
                "thresholds": {"confirm": [], "warn": [], "break": []},
                "status": "intact",
                "provenance": "draft",
                "created_at": "2026-10-19T00:00:00Z",
            }
        ],
        "evidence": [
            {
                "id": "EV-AMZN-001",
                "at": "2026-07-31T20:10:00Z",
                "period": "LTM",
                "source": {
                    "type": "filing",
                    "identity": "10-Q",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-Q", "accession": "0001-26-000001"},
                },
                "fact": "Fact",
                "figures": [fig_valid],
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "high",
            }
        ],
        "official": None,
    }
    assert validate_schema("thesis", base_thesis) == []

    bad_thesis = json.loads(json.dumps(base_thesis))
    bad_thesis["evidence"][0]["figures"] = [fig_invalid]
    errs = validate_schema("thesis", bad_thesis)
    assert errs, "Expected 'guidance' label to be rejected by common.schema.json"

    # Legacy migrated documents and G3 SEC evidence fixtures must still pass schema validation
    migration_root = REPO_ROOT / "var" / "dalton" / "migration" / "Home" / "dalton" / "companies"
    if migration_root.exists():
        for ticker_dir in sorted(migration_root.iterdir()):
            data_dir = ticker_dir / "data"
            if not data_dir.is_dir():
                continue
            for doc_file in sorted(data_dir.glob("*.json")):
                doc_key = doc_file.stem
                payload = json.loads(doc_file.read_text(encoding="utf-8"))
                doc_errs = validate_schema(doc_key, payload)
                assert doc_errs == [], f"{ticker_dir.name}/{doc_file.name} failed validation: {doc_errs}"

    for ev_fixture in ("googl_sec_evidence.json", "amzn_sec_evidence.json"):
        ev_path = REPO_ROOT / "tests" / "fixtures" / "dalton" / "g3" / ev_fixture
        ev_data = json.loads(ev_path.read_text(encoding="utf-8"))
        thesis_with_ev = {**base_thesis, "evidence": ev_data["evidence"]}
        assert validate_schema("thesis", thesis_with_ev) == []


@pytest.mark.asyncio
async def test_append_verdict_records_dropped_fields_in_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """J3 §2.5: append_verdict records dropped temporary signal fields in returned warnings[]."""
    mock_latest_valid = create_autospec(knowledge_service.db_knowledge.latest_valid_version)
    mock_latest_valid.return_value = None
    mock_latest = create_autospec(knowledge_service.db_knowledge.latest_version)
    mock_latest.return_value = None
    mock_insert = create_autospec(knowledge_service.db_verdicts.insert_verdict)
    mock_insert.return_value = {
        "verdict_id": "VD-20261019-01",
        "word": "NO_CHANGE",
    }
    monkeypatch.setattr(knowledge_service.db_knowledge, "latest_valid_version", mock_latest_valid)
    monkeypatch.setattr(knowledge_service.db_knowledge, "latest_version", mock_latest)
    monkeypatch.setattr(knowledge_service.db_verdicts, "insert_verdict", mock_insert)

    res = await knowledge_service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict={
            "at": "2026-10-19T12:00:00Z",
            "trigger": {"kind": "filing"},
            "trigger_signal": "sec:10-Q:0001",
            "signal_ids": ["sig-1"],
            "event": "10-Q filed",
            "verdict": "NO_CHANGE",
            "rationale": "Inline with baseline expectations",
            "next_validation": {"what": "Next 10-Q", "due": "2027-01-31"},
            "progress": "some",
        },
    )
    assert res["ok"] is True, res
    dropped_warnings = [w for w in res["warnings"] if w.get("code") == "dropped_extra_fields"]
    assert len(dropped_warnings) == 1
    assert set(dropped_warnings[0]["dropped_fields"]) == {"trigger_signal", "signal_ids"}


def test_antigravity_sanitize_gemini_schema_flattening_and_deref_debug_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """J3 §2.6: Lock _sanitize_gemini_schema flattening ($ref, allOf, anyOf/oneOf first-branch + nullable) and deref debug log."""
    raw_schema = {
        "$defs": {
            "SubItem": {
                "type": "object",
                "properties": {
                    "kind": {"const": "fixed_kind"},
                    "score": {"anyOf": [{"type": "null"}, {"type": "number"}, {"type": "string"}]},
                },
                "required": ["kind", "score", "nonexistent"],
            }
        },
        "type": "object",
        "properties": {
            "item": {
                "allOf": [{"$ref": "#/$defs/SubItem"}],
                "description": "Outer description preserved",
            }
        },
        "required": ["item"],
    }
    sanitized = ag_client._sanitize_gemini_schema(raw_schema)
    assert sanitized["type"] == "object"
    item_prop = sanitized["properties"]["item"]
    assert item_prop["description"] == "Outer description preserved"
    assert item_prop["type"] == "object"
    assert item_prop["properties"]["kind"] == {"enum": ["fixed_kind"], "type": "string"}
    # anyOf takes the first non-null branch (number) and sets nullable=True
    assert item_prop["properties"]["score"] == {"type": "number", "nullable": True}
    assert item_prop["required"] == ["kind", "score"]

    with (
        patch(
            "langchain_core.utils.json_schema.dereference_refs",
            side_effect=ValueError("cyclic ref"),
        ),
        caplog.at_level(logging.DEBUG, logger=ag_client.logger.name),
    ):
        decl = ag_client._openai_tool_to_declaration(
            {
                "name": "my_tool",
                "description": "desc",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            }
        )
    assert decl is not None
    assert decl["name"] == "my_tool"
    assert any("dereference_refs failed for tool my_tool" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_review_rejects_when_no_cross_family_reviewer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """J3 §2.7: Independent review rejects with status='unavailable' when no cross-family reviewer is available."""
    bundle = review_mod.ReviewBundle(
        title="Test Report",
        kind="event_note",
        draft_markdown="# Draft",
        audit_summary={"total": 1, "matched": 1, "unverified": 0},
    )
    res = await review_mod.run_review(
        user_id="user-1",
        author_model="claude-opus-5-5-oauth",
        bundle=bundle,
        candidates=["claude-sonnet-5-5-oauth", "gpt-6.1-sol-oauth"],
        available_models={"claude-sonnet-5-5-oauth", "claude-opus-5-5-oauth"},
    )
    assert res.status == "unavailable"
    assert "不同家族" in (res.reason or "")

    # Also test when explain_model_availability reports only same-family models available
    from src.dalton.services import provider_status as ps_mod

    mock_explain = create_autospec(ps_mod.explain_model_availability)
    mock_explain.return_value = {
        "claude-opus-5-5-oauth": {"available": True, "family": "claude"},
        "claude-sonnet-5-5-oauth": {"available": True, "family": "claude"},
        "gpt-6.1-sol-oauth": {"available": False, "family": "openai"},
    }
    monkeypatch.setattr(ps_mod, "explain_model_availability", mock_explain)
    res2 = await review_mod.run_review(
        user_id="user-1",
        author_model="claude-opus-5-5-oauth",
        bundle=bundle,
    )
    assert res2.status == "unavailable"
    assert "不同家族" in (res2.reason or "")


def test_researchdata_bridge_test_delay_try_except_and_test_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """J3 §2.8: researchdata_bridge wraps DALTON_BRIDGE_TEST_DELAY_S in try/except and checks self.test_mode."""
    from scripts.dalton import researchdata_bridge as bridge_mod

    monkeypatch.setenv("DALTON_BRIDGE_TEST_MODE", "1")
    monkeypatch.setenv("DALTON_BRIDGE_TEST_DELAY_S", "malformed-float")
    cfg = bridge_mod.BridgeConfig()
    assert cfg.test_mode is True
    assert cfg.test_delay_s == 0.0
    assert cfg.resolve_test_delay_s() == 0.0

    # When self.test_mode is False, runtime resolve_test_delay_s() returns 0.0 even if env vars change later
    cfg.test_mode = False
    monkeypatch.setenv("DALTON_BRIDGE_TEST_DELAY_S", "5.0")
    assert cfg.resolve_test_delay_s() == 0.0


@pytest.mark.asyncio
async def test_dalton_startup_config_error_vs_transient_db_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """J3 §3.1: RolePolicyError and missing migration raise DaltonStartupConfigError; transient DB error does not."""
    assert issubclass(RolePolicyError, DaltonStartupConfigError)

    # 1. Missing migration raises DaltonStartupConfigError
    cur_missing = create_autospec(_CursorProto, instance=True)
    cur_missing.__aenter__.return_value = cur_missing
    cur_missing.__aexit__.return_value = None
    cur_missing.execute.side_effect = RuntimeError("relation alembic_version_dalton does not exist")
    conn_missing = _make_spec_conn(cur_missing)

    monkeypatch.setattr("src.dalton.llm.role_policy.enforce_role_policy_on_startup", lambda config=None: [])
    monkeypatch.setattr(gate_mod, "is_dalton_budget_gate_enabled", lambda: True)
    monkeypatch.setattr(gate_mod, "get_db_connection", lambda: conn_missing)

    with pytest.raises(DaltonStartupConfigError, match="alembic_version_dalton"):
        await gate_mod.verify_local_wiring()

    # 2. Transient DB error raises non-DaltonStartupConfigError (so setup.py only warns)
    cur_transient = create_autospec(_CursorProto, instance=True)
    cur_transient.__aenter__.return_value = cur_transient
    cur_transient.__aexit__.return_value = None
    cur_transient.execute.side_effect = psycopg.OperationalError("connection to server refused")
    conn_transient = _make_spec_conn(cur_transient)
    monkeypatch.setattr(gate_mod, "get_db_connection", lambda: conn_transient)

    with pytest.raises(psycopg.OperationalError) as exc_info:
        await gate_mod.verify_local_wiring()
    assert not isinstance(exc_info.value, DaltonStartupConfigError)


@pytest.mark.asyncio
async def test_assist_scope_always_includes_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """J3 §1: assist_json always includes user_id (including LOCAL_DEV_USER_ID) in eff_scope."""
    captured_scopes: list[str] = []
    mock_complete = create_autospec(assist_mod.dalton_complete)

    async def _fake_complete(*args: Any, **kwargs: Any) -> complete_mod.DaltonCompleteResult:
        captured_scopes.append(str(kwargs.get("scope_ref") or ""))
        return complete_mod.DaltonCompleteResult(
            ok=True,
            status="done",
            data={"ok": True},
            text='{"ok": true}',
            model="gemini-3.8-flash-low-oauth",
            provider="antigravity-oauth",
            billing_type="oauth",
            invocation_id="inv-test",
        )

    mock_complete.side_effect = _fake_complete
    mock_resolve = create_autospec(assist_mod.resolve_assist_model)
    mock_resolve.return_value = "gemini-3.8-flash-low-oauth"
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete)
    monkeypatch.setattr(assist_mod, "resolve_assist_model", mock_resolve)

    await assist_mod.assist_json(
        task="unit.test",
        instructions="Return ok",
        payload={"a": 1},
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        user_id=LOCAL_DEV_USER_ID,
    )
    assert len(captured_scopes) == 1
    assert captured_scopes[0].startswith(f"assist:{LOCAL_DEV_USER_ID}:unit.test:")

