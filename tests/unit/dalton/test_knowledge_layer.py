"""Unit tests for the Company Knowledge Layer (PK.1–PK.7) and P1.4 Legacy Migration."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.dalton.knowledge.ops import OpError, apply_ops, diff_documents
from src.dalton.knowledge.paths import (
    AGENT_SUMMARY_END,
    AGENT_SUMMARY_START,
    DOC_KEYS,
    RENDERED,
    data_path,
    history_edited_md_path,
    history_path,
    rendered_path,
)
from src.dalton.knowledge.render import (
    MAX_AGENT_BLOCK_CHARS,
    render_agent_block,
    render_document_md,
    update_agent_md_block,
)
from src.dalton.knowledge.validate import (
    SCHEMA_NAMES,
    check_all_schemas,
    validate_update,
)


def _sample_thesis_doc() -> dict[str, Any]:
    now = "2026-10-18T12:00:00Z"
    hv = {
        "stance": "long",
        "conviction": "medium",
        "horizon_months": 6,
        "summary": "Core advertising ROAS improvements offset elevated AI capex.",
        "variant_perception": "Street underestimates Q4 ad pricing power.",
        "position_suggestion": "core",
        "updated_at": now,
    }
    return {
        "schema_version": 1,
        "as_of": now,
        "one_liner": "Ad pricing momentum offsets 2027 AI infrastructure capex step-up.",
        "aggregate_status": "intact",
        "house_view": hv,
        "house_view_history": [
            {"at": now, "to": copy.deepcopy(hv), "reason": "Initial coverage"}
        ],
        "market_view": {
            "summary": "Consensus expects FY2027 EPS of $34.05.",
            "as_of": now,
            "figures": [
                {
                    "metric": "Consensus EPS",
                    "value": 34.05,
                    "unit": "per_share",
                    "period": "FY2027",
                    "label": "consensus",
                }
            ],
            "evidence_refs": [{"kind": "file", "path": "consensus.json"}],
        },
        "pillars": [
            {
                "id": "P1",
                "claim": "FoA ad revenue sustains >20% YoY growth in 2H26.",
                "priority": "core",
                "baseline": {"text": "2Q26 ad revenue +27% YoY", "as_of": "2026-10-15"},
                "expected_path": "3Q26 ad revenue beats upper end of guidance.",
                "thresholds": {
                    "confirm": [
                        {
                            "id": "T1",
                            "metric": "FoA Ad Growth",
                            "operator": ">=",
                            "value": 25.0,
                            "unit": "pct",
                            "period": "3Q26",
                            "label": "research_judgement",
                        }
                    ],
                    "warn": [],
                    "break": [
                        {
                            "id": "T2",
                            "metric": "FoA Ad Growth",
                            "operator": "<",
                            "value": 18.0,
                            "unit": "pct",
                            "period": "3Q26",
                            "label": "research_judgement",
                        }
                    ],
                },
                "status": "intact",
                "status_history": [
                    {"at": now, "to": "intact", "reason": "Initial baseline"}
                ],
                "next_proof_point": {"what": "3Q26 earnings release", "due": "2026-10-28"},
                "provenance": "inherited",
                "created_at": now,
            }
        ],
        "evidence": [
            {
                "id": "E-20261015-01",
                "at": now,
                "period": "2Q26",
                "source": {
                    "type": "filing",
                    "identity": "META 2Q26 10-Q",
                    "tier": 1,
                    "ref": {"kind": "file", "path": "sources/2Q26_10Q.md"},
                },
                "fact": "2Q26 Family of Apps ad revenue reached $59.4B (+27% YoY).",
                "figures": [
                    {
                        "metric": "Ad Revenue",
                        "value": 59.4,
                        "unit": "USD_bn",
                        "period": "2Q26",
                        "label": "reported",
                    }
                ],
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "high",
            }
        ],
        "official": None,
    }


def test_all_10_schemas_valid_draft_2020_12() -> None:
    schema_ids = check_all_schemas()
    assert set(schema_ids.keys()) == set(SCHEMA_NAMES)
    assert len(schema_ids) == 10
    for name, uri in schema_ids.items():
        assert uri == f"https://dalton.local/schemas/{name}"


def test_paths_and_headers() -> None:
    assert len(DOC_KEYS) == 8
    assert data_path("thesis") == "data/thesis.json"
    assert rendered_path("thesis") == "thesis.md"
    assert rendered_path("company") is None
    dt = datetime(2026, 10, 18, 9, 30, 0, tzinfo=UTC)
    assert history_path("thesis", 3, dt) == "history/thesis/20261018T093000Z-v0003.json"
    assert (
        history_edited_md_path("debate", 12, dt)
        == "history/debate/20261018T093000Z-v0012-edited.md"
    )


def test_hard_invariants_i1_i2_i3() -> None:
    old_doc = _sample_thesis_doc()

    # Valid update appending a new evidence item
    new_doc = copy.deepcopy(old_doc)
    new_doc["evidence"].append(
        {
            "id": "E-20261018-01",
            "at": "2026-10-18T13:00:00Z",
            "period": "3Q26",
            "source": {
                "type": "broker_report",
                "identity": "Piper Sandler Ad Tracker",
                "tier": 3,
                "ref": {"kind": "file", "path": "sources/piper.md"},
            },
            "fact": "September CPM rose +47% YoY.",
            "pillar_ids": ["P1"],
            "signal": "confirming",
            "quality": "medium",
        }
    )
    res_ok = validate_update("thesis", old_doc, new_doc, reason="Added Piper CPM evidence")
    assert res_ok.ok, res_ok.format_problems()

    # I1 violation: deleting or mutating existing evidence entry
    bad_ev_doc = copy.deepcopy(old_doc)
    bad_ev_doc["evidence"][0]["fact"] = "Mutated fact!"
    res_i1 = validate_update("thesis", old_doc, bad_ev_doc, reason="Mutate old evidence")
    assert not res_i1.ok
    assert any(e["code"] == "append_only_violation" for e in res_i1.errors)

    # I2 violation: writing `official` when source != "publish"
    bad_off_doc = copy.deepcopy(old_doc)
    bad_off_doc["official"] = {
        "version_no": 2,
        "publication_id": "12345678-1234-5678-1234-567812345678",
        "approved_by": "pm_user",
        "approved_at": "2026-10-18T14:00:00Z",
        "house_view": copy.deepcopy(old_doc["house_view"]),
        "aggregate_status": "intact",
    }
    res_i2_fail = validate_update(
        "thesis", old_doc, bad_off_doc, source="tool", reason="Try setting official"
    )
    assert not res_i2_fail.ok
    assert any(e["code"] == "official_is_publish_only" for e in res_i2_fail.errors)

    # I2 allowed when source == "publish"
    res_i2_pass = validate_update(
        "thesis", old_doc, bad_off_doc, source="publish", reason="Approved publication"
    )
    assert res_i2_pass.ok, res_i2_pass.format_problems()

    # I3 violation: target price without model_cell reference
    bad_target_doc = copy.deepcopy(old_doc)
    bad_target_doc["house_view"]["target"] = {
        "value": 850.0,
        "currency": "USD",
        "method": "25x FY27E EPS",
        "model_ref": {"kind": "file", "path": "notes.md"},
    }
    res_i3_fail = validate_update(
        "thesis", old_doc, bad_target_doc, reason="Set target without model_cell"
    )
    assert not res_i3_fail.ok
    assert any(e["code"] == "target_needs_model_cell" for e in res_i3_fail.errors)

    # I3 allowed when pointing to a registered model_cell
    good_target_doc = copy.deepcopy(old_doc)
    good_target_doc["house_view"]["target"] = {
        "value": 850.0,
        "currency": "USD",
        "method": "25x FY27E EPS",
        "model_ref": {
            "kind": "model_cell",
            "path": "model/meta_v1.xlsx",
            "cell": "Valuation!F12",
        },
    }
    models_doc = {
        "schema_version": 1,
        "current": "v1",
        "versions": [
            {
                "id": "v1",
                "path": "model/meta_v1.xlsx",
                "created_at": "2026-10-18T12:00:00Z",
                "reason": "Initial model",
                "recalculated": True,
            }
        ],
    }
    res_i3_pass = validate_update(
        "thesis",
        old_doc,
        good_target_doc,
        models_doc=models_doc,
        reason="Set model-backed target price",
    )
    assert res_i3_pass.ok, res_i3_pass.format_problems()


def test_ops_apply_and_server_maintained_fields() -> None:
    now = datetime(2026, 10, 18, 15, 0, 0, tzinfo=UTC)
    base_thesis = _sample_thesis_doc()

    # 1. Test `set` on house_view.stance -> automatically appends house_view_history
    # and `set` on P1.status -> automatically appends status_history
    # and `append` on evidence without id -> assigns E-20261018-01
    updated_thesis, summaries = apply_ops(
        "thesis",
        base_thesis,
        [
            {"op": "set", "path": "/house_view/conviction", "value": "high"},
            {"op": "set", "path": "/pillars/P1/status", "value": "strengthening"},
            {
                "op": "append",
                "path": "/evidence",
                "value": {
                    "period": "3Q26",
                    "source": {
                        "type": "expert",
                        "identity": "IGG UA Director",
                        "tier": 5,
                        "ref": {"kind": "file", "path": "sources/wiki/igg.md"},
                    },
                    "fact": "Meta share of gaming ad spend rose from 35% in Q2 to >40% in Q3.",
                    "pillar_ids": ["P1"],
                    "signal": "confirming",
                    "quality": "medium",
                },
            },
        ],
        reason="Upgrade conviction after expert check",
        now=now,
        evidence_ids=["E-20261018-01"],
    )
    assert len(updated_thesis["house_view_history"]) == 2
    assert updated_thesis["house_view_history"][-1]["to"]["conviction"] == "high"
    assert len(updated_thesis["pillars"][0]["status_history"]) == 2
    assert updated_thesis["pillars"][0]["status_history"][-1]["to"] == "strengthening"
    assert updated_thesis["evidence"][-1]["id"] == "E-20261018-01"

    # 2. Test `retire` on pillar P1
    retired_thesis, _ = apply_ops(
        "thesis",
        updated_thesis,
        [{"op": "retire", "path": "/pillars/P1", "value": {"reason": "Superseded by FY27 pillar"}}],
        reason="Retire P1",
        now=now,
    )
    assert retired_thesis["pillars"][0]["status"] == "retired"
    assert "retired_at" in retired_thesis["pillars"][0]

    # 3. Test `remove` on append-only evidence is rejected
    with pytest.raises(OpError) as exc_info:
        apply_ops(
            "thesis",
            updated_thesis,
            [{"op": "remove", "path": "/evidence/E-20261015-01"}],
            reason="Attempt illegal delete",
            now=now,
        )
    assert exc_info.value.code == "append_only_violation"

    # 4. Test `management` beat_rate and median_surprise_pct calculation
    mgmt_doc, _ = apply_ops(
        "management",
        None,
        [
            {
                "op": "create",
                "value": {
                    "guidance_style": {"bias": "conservative", "summary": "Beats upper end"},
                    "guidance_history": [
                        {
                            "metric": "Revenue",
                            "period": "2Q26",
                            "issued_at": "2026-04-29",
                            "guide": {"low": 56.0, "high": 59.0, "unit": "USD_bn"},
                            "guide_ref": {"kind": "file", "path": "sources/1Q26.md"},
                            "actual": {
                                "value": 60.8,
                                "unit": "USD_bn",
                                "reported_at": "2026-07-29",
                                "ref": {"kind": "file", "path": "sources/2Q26.md"},
                            },
                            "outcome": "above_high",
                        }
                    ],
                },
            }
        ],
        reason="Init management",
        now=now,
    )
    assert mgmt_doc["guidance_history"][0]["id"] == "G-001"
    assert mgmt_doc["guidance_history"][0]["delta_vs_mid_pct"] == pytest.approx(5.74, rel=1e-2)
    assert mgmt_doc["guidance_style"]["sample_n"] == 1
    assert mgmt_doc["guidance_style"]["beat_rate"] == 1.0

    # 5. Test `lessons` retract op
    lessons_doc, _ = apply_ops(
        "lessons",
        None,
        [
            {
                "op": "create",
                "value": {
                    "lessons": [
                        {
                            "statement": "Test lesson",
                            "applies_when": "Always",
                            "origin": {"event": "Test", "at": "2026-10-15"},
                            "scope": "company",
                            "confidence": "medium",
                        }
                    ]
                },
            }
        ],
        now=now,
    )
    assert lessons_doc["lessons"][0]["id"] == "L-001"
    retracted_lessons, _ = apply_ops(
        "lessons",
        lessons_doc,
        [{"op": "retract", "path": "/lessons/L-001", "value": "No longer holds"}],
        now=now,
    )
    assert retracted_lessons["lessons"][0]["status"] == "retracted"
    assert retracted_lessons["lessons"][0]["retracted"]["reason"] == "No longer holds"

    # 6. Test `agenda` question backoff maintenance
    agenda_doc, _ = apply_ops(
        "agenda",
        None,
        [
            {
                "op": "create",
                "value": {
                    "open_questions": [
                        {
                            "question": "What is Microsoft's exact stake in Anthropic?",
                            "why": "Impacts FQ2 non-operating gain",
                            "value": "medium",
                            "urgency": "this_week",
                            "origin": "analyst",
                            "owner": "analyst",
                            "status": "open",
                            "attempts": [
                                {
                                    "at": "2026-10-15T12:00:00Z",
                                    "method": "alphaengine_search",
                                    "progress": "none",
                                    "note": "Checked 8 IPO articles, none disclosed stake",
                                }
                            ],
                        }
                    ],
                    "next_validations": [],
                    "watch": {},
                    "wake_conditions": [],
                },
            }
        ],
        now=now,
    )
    q0 = agenda_doc["open_questions"][0]
    assert q0["id"] == "Q-001"
    assert q0["stalled_since"] == "2026-10-15T12:00:00Z"
    assert "next_attempt_after" in q0
    assert len(q0["attempts"][0]["evidence_fp"]) == 16

    # 7. Test diff_documents
    diffs = diff_documents(base_thesis, updated_thesis)
    assert any(d["path"] == "/house_view/conviction" for d in diffs)


def test_rendering_and_agent_block_cap() -> None:
    thesis_doc = _sample_thesis_doc()
    md = render_document_md("thesis", thesis_doc, version_no=1, ticker="META")
    assert md is not None
    assert md.startswith("<!-- 由 data/thesis.json 渲染")
    assert "META" in md
    assert "P1" in md

    block = render_agent_block(
        ticker="META",
        company_doc={"ticker": "META", "name": "Meta Platforms, Inc.", "coverage_tier": "core", "coverage_stage": "full"},
        thesis_doc=thesis_doc,
        thesis_version_no=2,
        alerts=["⚠ thesis.md 被直接修改"],
    )
    assert len(block) <= MAX_AGENT_BLOCK_CHARS
    assert block.startswith(AGENT_SUMMARY_START)
    assert block.endswith(AGENT_SUMMARY_END)

    existing = "# Custom Agent Notes\nKeep this top paragraph.\n\n" + block + "\n\nKeep this bottom paragraph.\n"
    updated_block = block.replace("v2", "v3")
    merged = update_agent_md_block(existing, updated_block)
    assert "Keep this top paragraph." in merged
    assert "Keep this bottom paragraph." in merged
    assert "v3" in merged


def test_migrated_4_companies_pass_all_schemas_and_debate_coverage() -> None:
    migration_root = Path(__file__).resolve().parents[3] / "var" / "dalton" / "migration"
    report_path = migration_root / "migration_report.json"
    assert report_path.exists(), "Run scripts/dalton/migrate_legacy_knowledge.py first"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["all_schemas_passed"] is True
    assert report["source_integrity"]["zero_source_mutations"] is True
    assert report["openclaw_debates_expected"] == 27
    assert report["openclaw_debates_migrated"] == 27

    expected_counts = {"META": 6, "AMZN": 7, "GOOGL": 7, "MSFT": 7}
    for ticker, exp_d in expected_counts.items():
        c_dir = migration_root / "Home" / "dalton" / "companies" / ticker
        for doc_key in DOC_KEYS:
            doc_file = c_dir / data_path(doc_key)
            assert doc_file.exists(), f"Missing {doc_file}"
            payload = json.loads(doc_file.read_text(encoding="utf-8"))
            val = validate_update(doc_key, None, payload, reason="Verify migrated doc")
            assert val.ok, f"{ticker} {doc_key} failed validation: {val.format_problems()}"

        debate_payload = json.loads((c_dir / "data" / "debate.json").read_text(encoding="utf-8"))
        d_ids = [d["id"] for d in debate_payload["debates"]]
        assert d_ids == [f"D{i}" for i in range(1, exp_d + 1)]

        for doc_key, md_rel in RENDERED.items():
            assert (c_dir / md_rel).exists(), f"Missing rendered {ticker}/{md_rel}"
        assert (c_dir / "agent.md").exists()
        assert (c_dir / "verdicts.md").exists()
        assert (c_dir / "published" / "INDEX.md").exists()
        assert (c_dir / "sources" / "legacy" / "provenance.json").exists()


@pytest.mark.asyncio
async def test_service_apply_update_and_append_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts
    from src.dalton.knowledge import service
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws-1", "user_id": "user-1"},
        ),
    )

    store: dict[str, list[dict[str, Any]]] = {}
    verdict_store: list[dict[str, Any]] = []

    async def fake_latest_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = store.get(doc_key, [])
        return rows[-1] if rows else None

    async def fake_latest_valid_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = [r for r in store.get(doc_key, []) if r.get("schema_valid", True)]
        return rows[-1] if rows else None

    async def fake_list_all_latest(workspace_id: str, valid_only: bool = False) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k, rows in store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[k] = filtered[-1]
        return out

    mock_insert_version = create_autospec(db_knowledge.insert_version)

    async def _insert_version_impl(
        *,
        user_id: str,
        workspace_id: str,
        doc_key: str,
        base_version_no: int | None,
        content: dict[str, Any],
        content_sha256: str,
        ops: list[dict[str, Any]] | None,
        reason: str,
        evidence_refs: list[dict[str, Any]] | None = None,
        source: str = "tool",
        warnings: list[dict[str, Any]] | None = None,
        schema_valid: bool = True,
        edited_path: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        rows = store.setdefault(doc_key, [])
        v_no = len(rows) + 1
        row = {
            "knowledge_version_id": f"kv-{doc_key}-{v_no}",
            "workspace_id": workspace_id,
            "doc_key": doc_key,
            "version_no": v_no,
            "base_version_no": base_version_no,
            "content": copy.deepcopy(content),
            "content_sha256": content_sha256,
            "ops": ops,
            "reason": reason,
            "source": source,
            "warnings": warnings or [],
            "schema_valid": schema_valid,
            "edited_path": edited_path,
            "files_written_at": None,
            "created_at": datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),
        }
        rows.append(row)
        return row

    mock_insert_version.side_effect = _insert_version_impl

    async def fake_mark_files_written(kv_id: str) -> None:
        for rows in store.values():
            for r in rows:
                if r["knowledge_version_id"] == kv_id:
                    r["files_written_at"] = datetime.now(UTC)

    mock_insert_verdict = create_autospec(db_verdicts.insert_verdict)

    async def _insert_verdict_impl(
        *,
        user_id: str,
        workspace_id: str,
        verdict: dict[str, Any],
        thread_id: str | None = None,
        run_id: str | None = None,
        automation_execution_id: str | None = None,
        origin_type: str | None = None,
    ) -> dict[str, Any]:
        v = copy.deepcopy(verdict)
        vid = f"VD-20261018-{len(verdict_store) + 1:02d}"
        v["id"] = vid
        row = {
            "verdict_id": vid,
            "workspace_id": workspace_id,
            "verdict_at": datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),
            "word": v["verdict"],
            "payload": v,
        }
        verdict_store.append(row)
        return row

    mock_insert_verdict.side_effect = _insert_verdict_impl

    async def fake_list_verdicts(workspace_id: str, limit: int = 60) -> list[dict[str, Any]]:
        return list(reversed(verdict_store))[:limit]

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest_version)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest_valid_version)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all_latest)
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_version)
    monkeypatch.setattr(db_knowledge, "mark_files_written", fake_mark_files_written)
    monkeypatch.setattr(db_verdicts, "insert_verdict", mock_insert_verdict)
    monkeypatch.setattr(db_verdicts, "list_verdicts", fake_list_verdicts)

    class MemoryBackend:
        def __init__(self) -> None:
            self.files: dict[str, str] = {}

        async def aread_text(self, path: str) -> str | None:
            return self.files.get(path)

        async def awrite_text(self, path: str, content: str) -> None:
            self.files[path] = content

    backend = MemoryBackend()
    thesis_doc = _sample_thesis_doc()

    # Import initial thesis
    res_imp = await service.import_knowledge_doc(
        user_id="user-1",
        workspace_id="ws-1",
        doc_key="thesis",
        content=thesis_doc,
        reason="Initial P1.4 import",
        backend=backend,
    )
    assert res_imp["ok"] is True
    assert res_imp["version_no"] == 1
    assert "data/thesis.json" in backend.files
    assert "thesis.md" in backend.files
    assert "agent.md" in backend.files

    # Version conflict check when passing wrong base_version
    res_conflict = await service.apply_update(
        user_id="user-1",
        workspace_id="ws-1",
        doc_key="thesis",
        ops=[{"op": "set", "path": "/one_liner", "value": "Updated one-liner"}],
        reason="Try stale base_version",
        base_version=0,
        backend=backend,
    )
    assert res_conflict["ok"] is False
    assert res_conflict["error"] == "version_conflict"

    # Append verdict and verify verdicts.md is rendered
    verdict_payload = {
        "event": "META 3Q26 ad revenue beat consensus by 2%",
        "trigger": {"kind": "earnings", "ref": "3Q26"},
        "verdict": "THESIS_STRENGTHENED",
        "affected": {"pillars": ["P1"], "debates": ["D1"], "drivers": ["K1"]},
        "rationale": "Ad impressions and CPM both accelerated above street bogey.",
        "evidence_ids": ["E-20261015-01"],
        "house_view_changed": False,
        "next_validation": {"what": "4Q26 ad revenue guidance", "due": "2027-01-29"},
        "question_ids": [],
        "progress": "some",
    }
    res_v = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict=verdict_payload,
        backend=backend,
    )
    assert res_v["ok"] is True, res_v.get("message")
    assert res_v["verdict_id"] == "VD-20261018-01"
    assert "verdicts.md" in backend.files
    assert "VD-20261018-01" in backend.files["verdicts.md"]


def test_question_backoff_stalled_duration_and_new_evidence_reset() -> None:
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
    # 1. Stalled for 10 days (2026-10-01 to 2026-10-11 = 240h) -> 0.5 * 240h = 120h (5 days) -> 2026-10-16
    agenda_doc, _ = apply_ops(
        "agenda",
        None,
        [
            {
                "op": "create",
                "value": {
                    "open_questions": [
                        {
                            "id": "Q-001",
                            "question": "What is Anthropic's exact gross margin?",
                            "why": "Impacts AWS AI margin debate",
                            "value": "high",
                            "urgency": "this_quarter",
                            "origin": "analyst",
                            "owner": "analyst",
                            "status": "open",
                            "attempts": [
                                {
                                    "at": "2026-10-01T12:00:00Z",
                                    "method": "search_1",
                                    "progress": "none",
                                    "note": "No disclosure",
                                },
                                {
                                    "at": "2026-10-11T12:00:00Z",
                                    "method": "search_2",
                                    "progress": "none",
                                    "note": "Still no disclosure",
                                },
                            ],
                        }
                    ],
                    "next_validations": [],
                    "watch": {},
                    "wake_conditions": [],
                },
            }
        ],
        now=now,
    )
    q = agenda_doc["open_questions"][0]
    assert q["stalled_since"] == "2026-10-01T12:00:00Z"
    assert q["next_attempt_after"] == "2026-10-16T12:00:00Z"

    # 2. Stalled for 80 days (2026-10-01 to 2026-12-20 = 1920h) -> 0.5 * 1920h = 960h -> clamped to 30d (720h = 2027-01-19)
    agenda_80d, _ = apply_ops(
        "agenda",
        agenda_doc,
        [
            {
                "op": "append",
                "path": "/open_questions/Q-001/attempts",
                "value": {
                    "at": "2026-12-20T12:00:00Z",
                    "method": "search_3",
                    "progress": "none",
                    "note": "Still stalled",
                },
            }
        ],
        now=now,
    )
    q80 = agenda_80d["open_questions"][0]
    assert q80["stalled_since"] == "2026-10-01T12:00:00Z"
    assert q80["next_attempt_after"] == "2027-01-19T12:00:00Z"

    # 3. Linking new evidence to the question immediately clears stalled_since and next_attempt_after!
    agenda_cleared, _ = apply_ops(
        "agenda",
        agenda_80d,
        [
            {
                "op": "set",
                "path": "/open_questions/Q-001/linked/evidence_ids",
                "value": ["E-20261221-01"],
            }
        ],
        now=now,
    )
    q_cleared = agenda_cleared["open_questions"][0]
    assert "stalled_since" not in q_cleared
    assert "next_attempt_after" not in q_cleared


@pytest.mark.asyncio
async def test_field_and_path_repair_with_warnings_and_assist_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge import service
    from src.dalton.knowledge.ops import repair_knowledge_ops
    from src.dalton.llm.assist import AssistResult, assist_json
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws1", "user_id": "u1"},
        ),
    )

    store: dict[str, list[dict[str, Any]]] = {}

    async def fake_latest_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = store.get(doc_key, [])
        return rows[-1] if rows else None

    async def fake_latest_valid_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = store.get(doc_key, [])
        return rows[-1] if rows else None

    mock_insert = create_autospec(db_knowledge.insert_version)

    async def _insert_impl(
        *,
        user_id: str,
        workspace_id: str,
        doc_key: str,
        base_version_no: int | None,
        content: dict[str, Any],
        content_sha256: str,
        ops: list[dict[str, Any]] | None,
        reason: str,
        evidence_refs: list[dict[str, Any]] | None = None,
        source: str = "tool",
        warnings: list[dict[str, Any]] | None = None,
        schema_valid: bool = True,
        edited_path: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        rows = store.setdefault(doc_key, [])
        v_no = len(rows) + 1
        row = {
            "knowledge_version_id": f"kv-{doc_key}-{v_no}",
            "workspace_id": workspace_id,
            "doc_key": doc_key,
            "version_no": v_no,
            "base_version_no": base_version_no,
            "content": copy.deepcopy(content),
            "content_sha256": content_sha256,
            "ops": ops,
            "reason": reason,
            "source": source,
            "warnings": warnings or [],
            "schema_valid": True,
            "created_at": datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC),
        }
        rows.append(row)
        return row

    mock_insert.side_effect = _insert_impl

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest_version)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest_valid_version)
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)

    # Create initial agenda
    await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="agenda",
        ops=[
            {
                "op": "create",
                "value": {
                    "open_questions": [],
                    "next_validations": [],
                    "watch": {},
                    "wake_conditions": [],
                },
            }
        ],
        reason="Initialize agenda",
        base_version=None,
    )

    # P0H §3.3: Verify repair_knowledge_ops does NOT auto-mutate fuzzy paths (like /open_questons) without LLM confirmation
    unconfirmed_ops, unconfirmed_warns = repair_knowledge_ops(
        "agenda",
        store["agenda"][-1]["content"],
        [{"op": "append", "path": "/open_questons", "value": {"question": "Test"}}],
    )
    assert unconfirmed_ops[0]["path"] == "/open_questons"
    assert unconfirmed_warns == []

    # Append using frozen alias path `/questions` and frozen alias field `statement` instead of `question`
    res_repaired = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="agenda",
        ops=[
            {
                "op": "append",
                "path": "/questions",
                "value": {
                    "statement": "Will AWS capacity double by 2027?",
                    "why": "Key driver of AWS acceleration",
                    "value": "high",
                    "urgency": "this_quarter",
                    "origin": "analyst",
                    "owner": "analyst",
                    "status": "open",
                    "attempts": [],
                },
            }
        ],
        reason="Add question with alias path and field",
        base_version=1,
    )
    assert res_repaired["ok"] is True, res_repaired
    assert any(w["code"] == "field_repaired" for w in res_repaired["warnings"])
    # R2-N3N4 #12: original_ops preserved in warnings
    assert "original_ops" in res_repaired["warnings"][0]
    saved_q = store["agenda"][-1]["content"]["open_questions"][0]
    assert saved_q["question"] == "Will AWS capacity double by 2027?"

    # Test assist_json(task="knowledge.repair_field") confirming difflib suggestion on fuzzy path `/open_questons`
    mock_assist_repair = create_autospec(assist_json)

    async def _assist_repair_impl(
        task: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
        thread_id: str | None = None,
        role: str = "assist",
        scope_ref: str | None = None,
        timeout_s: float = 60.0,
    ) -> AssistResult:
        assert task == "knowledge.repair_field"
        assert "difflib_suggestions" in payload
        assert payload["difflib_suggestions"].get("open_questons") == ["open_questions"]
        return AssistResult(
            ok=True,
            data={"mappings": {"open_questons": "open_questions"}},
            error=None,
        )

    mock_assist_repair.side_effect = _assist_repair_impl
    monkeypatch.setattr(service, "assist_json", mock_assist_repair)

    res_assist = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="agenda",
        ops=[
            {
                "op": "append",
                "path": "/open_questons",
                "value": {
                    "question": "How fast does Trainium ramp?",
                    "why": "Margin impact",
                    "value": "medium",
                    "urgency": "this_week",
                    "origin": "analyst",
                    "owner": "analyst",
                    "status": "open",
                    "attempts": [],
                },
            }
        ],
        reason="Add question with fuzzy path confirmed by assist_json",
        base_version=2,
    )
    assert res_assist["ok"] is True, res_assist
    assert any("knowledge.repair_field" in w["message"] for w in res_assist["warnings"])
    assert len(store["agenda"][-1]["content"]["open_questions"]) == 2



def test_g34_migrated_docs_have_no_fabricated_numbers_and_remediation() -> None:
    from scripts.dalton.migrate_legacy_knowledge import remediate_doc_payload

    migration_root = Path(__file__).resolve().parents[3] / "var" / "dalton" / "migration"
    for ticker in ("META", "AMZN", "GOOGL", "MSFT"):
        c_dir = migration_root / "Home" / "dalton" / "companies" / ticker / "data"
        drivers = json.loads((c_dir / "drivers.json").read_text(encoding="utf-8"))
        hvd = drivers["historical_valuation_driver"]
        assert hvd["eps_share_pct"] is None
        assert hvd["multiple_share_pct"] is None
        assert hvd.get("note") == "迁移时未计算，待首次研究补充"

        mgmt = json.loads((c_dir / "management.json").read_text(encoding="utf-8"))
        for g in mgmt["guidance_history"]:
            assert g["issued_at"] is None
            assert g.get("note") == "迁移时未计算，待首次研究补充"

        comp = json.loads((c_dir / "company.json").read_text(encoding="utf-8"))
        assert comp["autonomy"]["max_usd_per_day"] is None
        assert comp["autonomy"].get("note") == "迁移时未计算，待首次研究补充"

        agenda = json.loads((c_dir / "agenda.json").read_text(encoding="utf-8"))
        for ev in agenda["watch"]["events"]:
            assert ev["date"] != "2026-11-15"
        for val in agenda["next_validations"]:
            assert val["due"] != "2026-11-15"

    # Also verify remediate_doc_payload cleans up legacy fabricated payloads
    legacy_drivers = {
        "schema_version": 1,
        "as_of": "2026-10-18T12:00:00Z",
        "historical_valuation_driver": {
            "summary": "Test",
            "window": "2025-2026",
            "eps_share_pct": 65.0,
            "multiple_share_pct": 35.0,
        },
        "drivers": [],
    }
    cleaned_drv, changed = remediate_doc_payload("drivers", legacy_drivers)
    assert changed is True
    assert cleaned_drv["historical_valuation_driver"]["eps_share_pct"] is None
    assert cleaned_drv["historical_valuation_driver"]["multiple_share_pct"] is None


@pytest.mark.asyncio
async def test_validation_failure_includes_expected_schema_structure_and_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts
    from src.dalton.knowledge import service
    from src.dalton.llm.assist import AssistResult, assist_json
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws1", "user_id": "u1"},
        ),
    )
    mock_latest = create_autospec(db_knowledge.latest_version, return_value=None)
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        mock_latest,
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )
    monkeypatch.setattr(
        service,
        "assist_json",
        create_autospec(
            assist_json,
            return_value=AssistResult(ok=False, data=None, error="disabled"),
        ),
    )

    # 1. Construct a drivers update with a reading missing unit/period/as_of and label not in enum (plus a second reading missing unit/period/label/as_of)
    bad_drivers = {
        "schema_version": 1,
        "as_of": "2026-10-22T00:00:00Z",
        "historical_valuation_driver": {
            "summary": "EPS driven",
            "window": "2023-2026",
            "eps_share_pct": 80.0,
            "multiple_share_pct": 20.0,
        },
        "drivers": [
            {
                "id": "K1",
                "name": "Search & YouTube Ad Revenue",
                "kind": "volume",
                "description": "Monetization of commercial queries",
                "updated_at": "2026-10-22T00:00:00Z",
                "leading_indicators": [
                    {
                        "id": "K1.1",
                        "name": "Paid clicks YoY",
                        "source": "10-Q",
                        "frequency": "quarterly",
                        "good_direction": "up",
                        "readings": [
                            {
                                "value": 5.0,
                                "label": "unverified_guess",
                                "ref": {"kind": "filing", "accession": "0001652044-26-000042"},
                            },
                            {
                                "value": 6.0,
                                "ref": {"kind": "filing", "accession": "0001652044-26-000043"},
                            },
                        ],
                    }
                ],
            }
        ],
    }

    res = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="drivers",
        ops=[{"op": "create", "value": bad_drivers}],
        reason="Create drivers with invalid reading",
        base_version=None,
    )
    assert res["ok"] is False
    msg = res["message"]
    assert "Expected structure for /drivers/0/leading_indicators/0/readings/0" in msg
    assert 'required=["value", "unit", "period", "as_of", "ref", "label"]' in msg
    assert "additionalProperties=false" in msg
    assert (
        'enum=["reported", "company_guidance", "consensus", "estimate", "assumption", "research_judgement"]'
        in msg
    )
    # Same path (/drivers/0/leading_indicators/0/readings/0) must only be attached once even though it had multiple errors (missing required + invalid enum)
    assert msg.count("Expected structure for /drivers/0/leading_indicators/0/readings/0 ") == 1

    # 2. Also verify append_verdict includes expected structure on schema error
    monkeypatch.setattr(
        db_verdicts,
        "list_verdicts",
        create_autospec(db_verdicts.list_verdicts, return_value=[]),
    )
    res_v = await service.append_verdict(
        user_id="u1",
        workspace_id="ws1",
        verdict={
            "event": {"type": "earnings", "summary": "Q2"},
            "verdict": "INVALID_VERDICT_ENUM",
        },
    )
    assert res_v["ok"] is False
    v_msg = res_v["message"]
    assert "Expected structure for / (" in v_msg
    assert 'required=["at", "trigger", "event", "verdict", "rationale", "next_validation", "progress"]' in v_msg
    assert 'enum=["NO_CHANGE", "THESIS_STRENGTHENED", "THESIS_WEAKENED", "THESIS_BROKEN", "NEW_THESIS"]' in v_msg

    # 3. Verify agenda next_validations[].due drills into oneOf/$ref and shows date|null instead of any
    agenda_struct = service._format_expected_structures(
        "agenda",
        [{"path": "/next_validations/0", "code": "schema_error", "message": "missing required"}],
    )
    assert "Expected structure for /next_validations/0" in agenda_struct
    assert "due: date|null" in agenda_struct
    assert "due: any" not in agenda_struct

    # 4. Verify line-based truncation drops incomplete last line and appends '…（其余字段省略）' within max_chars
    multi_errors = [
        {"path": "/", "code": "schema_error", "message": "root err"},
        {"path": "/open_questions/0", "code": "schema_error", "message": "q err"},
        {"path": "/next_validations/0", "code": "schema_error", "message": "v err"},
        {"path": "/watch/events/0", "code": "schema_error", "message": "w err"},
        {"path": "/pm_handoffs/0", "code": "schema_error", "message": "h err"},
    ]
    truncated_struct = service._format_expected_structures(
        "agenda", multi_errors, max_chars=450
    )
    assert len(truncated_struct) <= 450
    assert truncated_struct.splitlines()[-1] == "…（其余字段省略）"


def test_thesis_source_types_and_model_cell_tier_constraints() -> None:
    """Verify expanded source.type enum (consensus, transcript, internal_note) and model cell/tier constraints."""
    from src.dalton.knowledge.validate import _load_schemas_and_registry

    schemas, _ = _load_schemas_and_registry()
    common_enum = schemas["common"]["$defs"]["sourceType"]["enum"]
    thesis_enum = schemas["thesis"]["$defs"]["evidence"]["properties"]["source"]["properties"]["type"]["enum"]
    assert common_enum == thesis_enum
    for required_type in ("consensus", "transcript", "internal_note", "legacy_thesis"):
        assert required_type in thesis_enum

    doc = _sample_thesis_doc()
    doc["evidence"].extend(
        [
            {
                "id": "E-20261023-02",
                "at": "2026-10-23T10:00:00Z",
                "period": "FY2026",
                "source": {
                    "type": "consensus",
                    "identity": "Visible Alpha consensus",
                    "tier": 3,
                    "ref": {"kind": "url", "url": "https://example.com/va"},
                },
                "fact": "FY26 consensus revenue $215bn",
                "pillar_ids": ["P1"],
                "signal": "neutral",
                "quality": "high",
            },
            {
                "id": "E-20261023-03",
                "at": "2026-10-23T11:00:00Z",
                "period": "3Q26",
                "source": {
                    "type": "transcript",
                    "identity": "META 3Q26 Earnings Call AlphaEngine ASR",
                    "tier": 2,
                    "ref": {"kind": "file", "path": "research/3q26-transcript.md"},
                },
                "fact": "Management noted AI ad ranking drove +4% conversions",
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "high",
            },
            {
                "id": "E-20261023-04",
                "at": "2026-10-23T12:00:00Z",
                "period": "3Q26",
                "source": {
                    "type": "internal_note",
                    "identity": "TMT group internal discussion",
                    "tier": 5,
                    "ref": {"kind": "file", "path": "research/internal-note.md"},
                },
                "fact": "Internal channel check notes steady CPMs",
                "pillar_ids": ["P1"],
                "signal": "mildly_confirming",
                "quality": "medium",
            },
            {
                "id": "E-20261023-05",
                "at": "2026-10-23T13:00:00Z",
                "period": "FY2026",
                "source": {
                    "type": "model",
                    "identity": "META-model-v01",
                    "tier": 2,
                    "ref": {
                        "kind": "model_cell",
                        "path": "model/META-model-v01.xlsx",
                        "cell": "Model!F42",
                    },
                },
                "fact": "Implied FY26 FCF yield 4.8%",
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "high",
            },
        ]
    )
    res_ok = validate_update("thesis", None, doc, reason="Valid expanded source types")
    assert res_ok.ok, res_ok.format_problems()
    assert not any(w["code"] == "source_tier_mismatch" for w in res_ok.warnings)

    # model with tier 1 must be rejected
    bad_tier_doc = copy.deepcopy(doc)
    bad_tier_doc["evidence"][-1]["source"]["tier"] = 1
    res_bad_tier = validate_update("thesis", None, bad_tier_doc, reason="Model tier 1 invalid")
    assert not res_bad_tier.ok
    assert any(e["code"] == "model_tier_1_forbidden" for e in res_bad_tier.errors)

    # model without model_cell ref must be rejected when newly added
    bad_ref_doc = copy.deepcopy(doc)
    bad_ref_doc["evidence"][-1]["source"]["ref"] = {"kind": "file", "path": "research/note.md"}
    res_bad_ref = validate_update("thesis", None, bad_ref_doc, reason="Model without model_cell")
    assert not res_bad_ref.ok
    assert any(e["code"] == "model_needs_model_cell" for e in res_bad_ref.errors)


@pytest.mark.asyncio
async def test_legacy_model_evidence_preserved_while_new_invalid_model_evidence_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing legacy model evidence (tier 1, no model_cell) is allowed on update; new invalid model evidence is rejected."""
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge import service

    legacy_doc = _sample_thesis_doc()
    legacy_doc["evidence"].append(
        {
            "id": "E-20261001-01",
            "at": "2026-10-01T00:00:00Z",
            "period": "FY2026",
            "source": {
                "type": "model",
                "identity": "Legacy DCF summary",
                "tier": 1,
                "ref": {"kind": "file", "path": "research/legacy-model-note.md"},
            },
            "fact": "Legacy model implied $600",
            "pillar_ids": ["P1"],
            "signal": "confirming",
            "quality": "medium",
        }
    )

    current_state: dict[str, Any] = {
        "version_no": 1,
        "content": copy.deepcopy(legacy_doc),
        "schema_valid": True,
    }

    monkeypatch.setattr(
        service,
        "verify_workspace_access",
        create_autospec(service.verify_workspace_access, return_value={"workspace_id": "ws1"}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(db_knowledge.latest_version, side_effect=lambda ws, dk: copy.deepcopy(current_state)),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )

    async def _fake_insert(**kwargs: Any) -> dict[str, Any]:
        current_state["version_no"] = int(current_state["version_no"]) + 1
        current_state["content"] = copy.deepcopy(kwargs["content"])
        return {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000010",
            "version_no": current_state["version_no"],
            "doc_key": kwargs["doc_key"],
            "content": kwargs["content"],
            "created_at": datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC),
        }

    mock_insert = create_autospec(db_knowledge.insert_version)
    mock_insert.side_effect = _fake_insert
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)

    # 1. Appending a normal filing evidence entry to a doc with legacy tier-1 non-model_cell model evidence succeeds
    res_normal = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="thesis",
        ops=[
            {
                "op": "append",
                "path": "/evidence",
                "value": {
                    "id": "E-20261023-09",
                    "at": "2026-10-23T10:00:00Z",
                    "period": "3Q26",
                    "source": {
                        "type": "filing",
                        "identity": "META 3Q26 10-Q",
                        "tier": 1,
                        "ref": {"kind": "filing", "accession": "0001326801-26-000055"},
                    },
                    "fact": "Ad impressions +10% YoY",
                    "pillar_ids": ["P1"],
                    "signal": "confirming",
                    "quality": "high",
                },
            }
        ],
        reason="Append normal filing evidence alongside legacy model evidence",
        base_version=1,
    )
    assert res_normal["ok"] is True, res_normal.get("message")

    # 2. Appending a new non-compliant model evidence entry is rejected
    res_bad_new = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="thesis",
        ops=[
            {
                "op": "append",
                "path": "/evidence",
                "value": {
                    "id": "E-20261023-10",
                    "at": "2026-10-23T11:00:00Z",
                    "period": "FY2026",
                    "source": {
                        "type": "model",
                        "identity": "New model note without cell",
                        "tier": 1,
                        "ref": {"kind": "file", "path": "research/new-model.md"},
                    },
                    "fact": "New unanchored model output",
                    "pillar_ids": ["P1"],
                    "signal": "confirming",
                    "quality": "high",
                },
            }
        ],
        reason="Attempt to append non-compliant model evidence",
        base_version=2,
    )
    assert res_bad_new["ok"] is False
    err_codes = {e["code"] for e in res_bad_new["problems"]}
    assert "model_tier_1_forbidden" in err_codes
    assert "model_needs_model_cell" in err_codes


@pytest.mark.asyncio
async def test_apply_update_truncation_suffix_for_ops_and_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify op_summaries and val.warnings > 5 append '…等共 N 条' in service.apply_update."""
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge import service

    base_thesis = _sample_thesis_doc()
    monkeypatch.setattr(
        service,
        "verify_workspace_access",
        create_autospec(service.verify_workspace_access, return_value={"workspace_id": "ws1"}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(
            db_knowledge.latest_version,
            return_value={"version_no": 1, "content": base_thesis, "schema_valid": True},
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )

    async def _fake_insert(**kwargs: Any) -> dict[str, Any]:
        return {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000002",
            "version_no": 2,
            "doc_key": kwargs["doc_key"],
            "content": kwargs["content"],
            "created_at": datetime.now(UTC),
        }

    mock_insert = create_autospec(db_knowledge.insert_version)
    mock_insert.side_effect = _fake_insert
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)

    ops: list[dict[str, Any]] = []
    for i in range(1, 8):
        ops.append(
            {
                "op": "append",
                "path": "/evidence",
                "value": {
                    "id": f"E-20261023-1{i}",
                    "at": "2026-10-23T10:00:00Z",
                    "period": "3Q26",
                    "source": {
                        "type": "filing",
                        "identity": f"META 10-Q note {i}",
                        "tier": 3,  # triggers source_tier_mismatch warning on each of the 7 items
                        "ref": {"kind": "file", "path": f"research/n{i}.md"},
                    },
                    "fact": f"Fact item {i}",
                    "pillar_ids": ["P1"],
                    "signal": "confirming",
                    "quality": "high",
                },
            }
        )

    res = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="thesis",
        ops=ops,
        reason="Batch append 7 evidence entries",
        base_version=1,
    )
    assert res["ok"] is True
    msg = res["message"]
    assert "…等共 7 条." in msg
    assert "7 warning(s):" in msg
    assert msg.endswith("…等共 7 条")


@pytest.mark.asyncio
async def test_server_overwrites_write_timestamps_preserving_business_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify server overwrites verdict.at, house_view.updated_at, and market_view.updated_at with UTC now."""
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts
    from src.dalton.knowledge import service

    fixed_now = datetime(2026, 10, 23, 18, 30, 0, tzinfo=UTC)
    fixed_iso = "2026-10-23T18:30:00Z"

    monkeypatch.setattr(
        service,
        "verify_workspace_access",
        create_autospec(service.verify_workspace_access, return_value={"workspace_id": "ws1"}),
    )
    mock_latest = create_autospec(db_knowledge.latest_version, return_value=None)
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        mock_latest,
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )

    saved_docs: list[dict[str, Any]] = []

    async def _capture_insert(**kwargs: Any) -> dict[str, Any]:
        saved_docs.append(copy.deepcopy(kwargs["content"]))
        return {
            "knowledge_version_id": "00000000-0000-0000-0000-000000000001",
            "version_no": 1,
            "doc_key": kwargs["doc_key"],
            "content": kwargs["content"],
            "created_at": fixed_now,
        }

    mock_insert = create_autospec(db_knowledge.insert_version)
    mock_insert.side_effect = _capture_insert
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)

    thesis_in = _sample_thesis_doc()
    thesis_in["house_view"]["updated_at"] = "1999-01-01T00:00:00Z"
    thesis_in["market_view"]["updated_at"] = "1999-01-01T00:00:00Z"
    thesis_in["market_view"]["as_of"] = "2026-10-18T00:00:00Z"
    thesis_in["evidence"][0]["at"] = "2026-10-15T20:00:00Z"
    thesis_in["evidence"][0]["period"] = "3Q26"
    thesis_in["pillars"][0]["baseline"]["as_of"] = "2026-09-30"

    res_create = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="thesis",
        ops=[{"op": "create", "value": thesis_in}],
        reason="Initial thesis with stale model timestamps",
        base_version=None,
        now=fixed_now,
    )
    assert res_create["ok"] is True
    created_doc = saved_docs[-1]
    assert created_doc["as_of"] == fixed_iso
    assert created_doc["house_view"]["updated_at"] == fixed_iso
    assert created_doc["market_view"]["updated_at"] == fixed_iso
    # Business dates (including market_view.as_of) must remain untouched
    assert created_doc["market_view"]["as_of"] == "2026-10-18T00:00:00Z"
    assert created_doc["evidence"][0]["at"] == "2026-10-15T20:00:00Z"
    assert created_doc["evidence"][0]["period"] == "3Q26"
    assert created_doc["pillars"][0]["baseline"]["as_of"] == "2026-09-30"

    # When an existing doc only appends an evidence item, market_view.as_of stays unchanged
    mock_latest.return_value = {
        "version_no": 1,
        "content": copy.deepcopy(created_doc),
        "schema_valid": True,
    }
    later_now = datetime(2026, 10, 24, 9, 15, 0, tzinfo=UTC)
    res_append_ev = await service.apply_update(
        user_id="u1",
        workspace_id="ws1",
        doc_key="thesis",
        ops=[
            {
                "op": "append",
                "path": "/evidence",
                "value": {
                    "id": "E-20261024-01",
                    "at": "2026-10-24T08:00:00Z",
                    "period": "3Q26",
                    "source": {
                        "type": "consensus",
                        "identity": "Visible Alpha",
                        "tier": 3,
                        "ref": {"kind": "url", "url": "https://example.com/va"},
                    },
                    "fact": "Consensus FY26 EPS $26.10",
                    "pillar_ids": ["P1"],
                    "signal": "neutral",
                    "quality": "high",
                },
            }
        ],
        reason="Append one evidence entry",
        base_version=1,
        now=later_now,
    )
    assert res_append_ev["ok"] is True
    updated_doc = saved_docs[-1]
    assert updated_doc["market_view"]["as_of"] == "2026-10-18T00:00:00Z"

    # Also verify append_verdict uses the exact verdict_at (with microsecond precision) returned by insert_verdict
    verdict_db_dt = datetime(2026, 10, 23, 18, 30, 0, 456789, tzinfo=UTC)
    verdict_db_iso = "2026-10-23T18:30:00.456789Z"
    agenda_base = {
        "schema_version": 1,
        "as_of": "2026-10-20T00:00:00Z",
        "open_questions": [
            {
                "id": "Q-001",
                "question": "What is 4Q26 ad impression growth?",
                "why": "Key driver for P1",
                "value": "high",
                "urgency": "now",
                "origin": "analyst",
                "owner": "analyst",
                "status": "open",
                "attempts": [],
                "created_at": "2026-10-20T00:00:00Z",
            }
        ],
        "next_validations": [],
        "watch": {},
        "wake_conditions": [],
    }
    mock_latest.return_value = {
        "version_no": 1,
        "content": copy.deepcopy(agenda_base),
        "schema_valid": True,
    }

    async def _capture_verdict(
        *,
        user_id: str,
        workspace_id: str,
        verdict: dict[str, Any],
        thread_id: str | None = None,
        run_id: str | None = None,
        automation_execution_id: str | None = None,
        origin_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "verdict_id": "VD-20261023-01",
            "workspace_id": workspace_id,
            "verdict_at": verdict_db_dt,
            "word": verdict["verdict"],
            "payload": {**verdict, "id": "VD-20261023-01", "at": verdict_db_iso},
        }

    mock_insert_v = create_autospec(db_verdicts.insert_verdict)
    mock_insert_v.side_effect = _capture_verdict
    monkeypatch.setattr(db_verdicts, "insert_verdict", mock_insert_v)

    res_v = await service.append_verdict(
        user_id="u1",
        workspace_id="ws1",
        verdict={
            "at": "1999-01-01T00:00:00Z",
            "trigger": {"kind": "manual"},
            "event": "3Q26 earnings review",
            "verdict": "NO_CHANGE",
            "rationale": "In line with expectations",
            "next_validation": {"what": "4Q26 ad revenue guidance", "due": "2027-01-28"},
            "progress": "some",
            "question_ids": ["Q-001"],
        },
    )
    assert res_v["ok"] is True
    saved_agenda = saved_docs[-1]
    assert saved_agenda["last_verdict"]["at"] == verdict_db_iso
    assert saved_agenda["open_questions"][0]["attempts"][-1]["at"] == verdict_db_iso


@pytest.mark.asyncio
async def test_verdicts_same_timestamp_tiebreak_returns_latest_inserted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two verdicts written at the exact same timestamp return the later one (VD-...-02) first when querying latest."""
    from unittest.mock import create_autospec

    from src.dalton.database import verdicts as verdicts_mod

    same_ts = datetime(2026, 10, 23, 15, 4, 5, 123456, tzinfo=UTC)
    monkeypatch.setattr(
        verdicts_mod,
        "_now_utc",
        create_autospec(verdicts_mod._now_utc, return_value=same_ts),
    )

    inserted_rows: list[dict[str, Any]] = []
    executed_sqls: list[str] = []
    last_op: dict[str, Any] = {"kind": "", "rows": []}

    class _CurSpec:
        async def __aenter__(self) -> Any: ...
        async def __aexit__(self, *args: object) -> None: ...
        async def execute(self, sql: str, params: Any = None) -> None: ...
        async def fetchall(self) -> list[dict[str, Any]]: ...
        async def fetchone(self) -> dict[str, Any] | None: ...

    class _TxSpec:
        async def __aenter__(self) -> Any: ...
        async def __aexit__(self, *args: object) -> None: ...

    class _ConnSpec:
        async def __aenter__(self) -> Any: ...
        async def __aexit__(self, *args: object) -> None: ...
        def cursor(self, *args: Any, **kwargs: Any) -> Any: ...
        def transaction(self) -> Any: ...

    cur = create_autospec(_CurSpec, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        s = " ".join(str(sql).split())
        executed_sqls.append(s)
        if "SELECT verdict_id FROM dalton_verdicts" in s:
            last_op["kind"] = "select_ids"
            prefix = str(params[0]).rstrip("%")
            last_op["rows"] = [
                {"verdict_id": r["verdict_id"]}
                for r in inserted_rows
                if str(r["verdict_id"]).startswith(prefix)
            ]
        elif "INSERT INTO dalton_verdicts" in s:
            last_op["kind"] = "insert"
            row = {
                "verdict_id": str(params[0]),
                "user_id": str(params[1]),
                "workspace_id": str(params[2]),
                "verdict_at": params[3],
                "word": str(params[4]),
                "payload": params[9].obj,
            }
            inserted_rows.append(row)
            last_op["row"] = row
        elif "FROM dalton_verdicts WHERE workspace_id = %s" in s:
            last_op["kind"] = "list"
            assert "ORDER BY verdict_at DESC, length(verdict_id) DESC, verdict_id DESC" in s
            limit = int(params[-1])
            ordered = sorted(
                inserted_rows,
                key=lambda r: (r["verdict_at"], r["verdict_id"]),
                reverse=True,
            )
            last_op["rows"] = ordered[:limit]

    async def _fetchall() -> list[dict[str, Any]]:
        return list(last_op.get("rows", []))

    async def _fetchone() -> dict[str, Any] | None:
        return dict(last_op["row"]) if last_op.get("kind") == "insert" else None

    cur.execute.side_effect = _exec
    cur.fetchall.side_effect = _fetchall
    cur.fetchone.side_effect = _fetchone

    tx = create_autospec(_TxSpec, instance=True)
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = None

    conn = create_autospec(_ConnSpec, instance=True)
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    conn.cursor.return_value = cur
    conn.transaction.return_value = tx

    monkeypatch.setattr(
        verdicts_mod,
        "get_db_connection",
        create_autospec(verdicts_mod.get_db_connection, return_value=conn),
    )

    base_v = {
        "trigger": {"kind": "manual"},
        "event": "First verdict",
        "verdict": "NO_CHANGE",
        "rationale": "First",
        "next_validation": {"what": "Next check", "due": "2026-11-01"},
        "progress": "none",
    }
    r1 = await verdicts_mod.insert_verdict(user_id="u1", workspace_id="ws1", verdict=base_v)
    r2 = await verdicts_mod.insert_verdict(
        user_id="u1",
        workspace_id="ws1",
        verdict={**base_v, "event": "Second verdict", "verdict": "THESIS_STRENGTHENED"},
    )
    assert r1["verdict_id"] == "VD-20261023-01"
    assert r2["verdict_id"] == "VD-20261023-02"
    assert r1["verdict_at"] == same_ts
    assert r2["verdict_at"] == same_ts
    assert r2["payload"]["at"] == "2026-10-23T15:04:05.123456Z"

    latest_list = await verdicts_mod.list_verdicts("ws1", limit=1)
    assert len(latest_list) == 1
    assert latest_list[0]["verdict_id"] == "VD-20261023-02"


def test_render_agent_block_omits_none_fields() -> None:
    """Verify agent_block.md.j2 and render_agent_block omit None fields instead of rendering 'None'."""
    block = render_agent_block(
        ticker="META",
        company_doc={
            "ticker": "META",
            "name": "Meta Platforms, Inc.",
            "coverage_tier": None,
            "coverage_stage": None,
        },
        thesis_doc={
            "as_of": None,
            "one_liner": "AI 推荐拉动时长与转化",
            "aggregate_status": "intact",
            "house_view": {"stance": None, "conviction": None},
            "official": {
                "version_no": 1,
                "approved_at": None,
                "approved_by": None,
                "publication_id": None,
            },
            "pillars": [{"id": "P1", "status": None}],
        },
        debate_doc={
            "debates": [
                {
                    "id": "D1",
                    "variable": "ROIC on AI Capex",
                    "my_view": {"side": None, "confidence": None},
                }
            ]
        },
        agenda_doc={
            "next_validations": [
                {
                    "id": "V-002",
                    "due": None,
                    "what": "4Q26 Capex 指引",
                    "expected": None,
                    "status": "pending",
                }
            ],
            "pm_handoffs": [
                {"id": "H1", "what": None, "status": "waiting"}
            ],
        },
        last_verdict={"id": "VD-20261023-01", "verdict": "NO_CHANGE", "event": None},
    )
    assert "None" not in block
    assert "**下一项验证**：V-002 4Q26 Capex 指引\n" in block

    # Also test when both due and what/expected are None -> shows only '下一项验证：V-002'
    block_minimal = render_agent_block(
        ticker="META",
        agenda_doc={
            "next_validations": [
                {
                    "id": "V-002",
                    "due": None,
                    "what": None,
                    "expected": None,
                    "status": "pending",
                }
            ]
        },
    )
    assert "None" not in block_minimal
    assert "**下一项验证**：V-002\n" in block_minimal







def test_new_model_evidence_cannot_borrow_a_legacy_id() -> None:
    legacy_ev = {
        "id": "E-20261001-01",
        "at": "2026-10-01T00:00:00Z",
        "period": "FY2026",
        "source": {
            "type": "model",
            "identity": "Legacy DCF summary",
            "tier": 1,
            "ref": {"kind": "file", "path": "research/legacy-model-note.md"},
        },
        "fact": "Legacy model implied $600",
        "pillar_ids": ["P1"],
        "signal": "confirming",
        "quality": "medium",
    }
    old_doc = _sample_thesis_doc()
    old_doc["evidence"].append(copy.deepcopy(legacy_ev))
    new_doc = copy.deepcopy(old_doc)
    borrowed = copy.deepcopy(legacy_ev)
    borrowed["fact"] = "New model claim reusing an old id"
    new_doc["evidence"].append(borrowed)

    codes = [e.get("code") for e in validate_update("thesis", old_doc, new_doc).errors]

    assert "duplicate_evidence_id" in codes


def test_duplicate_ids_already_stored_stay_writable() -> None:
    old_doc = _sample_thesis_doc()
    dup = copy.deepcopy(old_doc["evidence"][0])
    old_doc["evidence"].append(dup)
    new_doc = copy.deepcopy(old_doc)
    new_doc["one_liner"] = "Edited one-liner only"

    codes = [e.get("code") for e in validate_update("thesis", old_doc, new_doc).errors]

    assert "duplicate_evidence_id" not in codes


def _setup_verdict_agenda_mocks(
    monkeypatch: pytest.MonkeyPatch,
    initial_agenda: dict[str, Any],
    *,
    verdict_id: str = "VD-20261024-01",
) -> list[dict[str, Any]]:
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws-1", "user_id": "user-1"},
        ),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )

    saved_agendas: list[dict[str, Any]] = [copy.deepcopy(initial_agenda)]

    async def _latest_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        if doc_key != "agenda":
            return None
        return {
            "version_no": len(saved_agendas),
            "content": copy.deepcopy(saved_agendas[-1]),
            "schema_valid": True,
        }

    async def _insert_version(
        *,
        user_id: str,
        workspace_id: str,
        doc_key: str,
        base_version_no: int | None,
        content: dict[str, Any],
        content_sha256: str,
        ops: list[dict[str, Any]] | None,
        reason: str,
        evidence_refs: list[dict[str, Any]] | None = None,
        source: str = "tool",
        warnings: list[dict[str, Any]] | None = None,
        schema_valid: bool = True,
        edited_path: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        saved_agendas.append(copy.deepcopy(content))
        v_no = len(saved_agendas)
        return {
            "knowledge_version_id": f"kv-agenda-{v_no}",
            "workspace_id": workspace_id,
            "doc_key": doc_key,
            "version_no": v_no,
            "base_version_no": base_version_no,
            "content": copy.deepcopy(content),
            "content_sha256": content_sha256,
            "ops": ops,
            "reason": reason,
            "source": source,
            "warnings": warnings or [],
            "schema_valid": schema_valid,
            "edited_path": edited_path,
            "files_written_at": None,
            "created_at": datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC),
        }

    async def _insert_verdict(
        *,
        user_id: str,
        workspace_id: str,
        verdict: dict[str, Any],
        thread_id: str | None = None,
        run_id: str | None = None,
        automation_execution_id: str | None = None,
        origin_type: str | None = None,
    ) -> dict[str, Any]:
        return {
            "verdict_id": verdict_id,
            "workspace_id": workspace_id,
            "verdict_at": datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC),
            "word": verdict["verdict"],
            "payload": {**verdict, "id": verdict_id, "at": "2026-10-24T12:00:00Z"},
        }

    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(db_knowledge.latest_version, side_effect=_latest_version),
    )
    monkeypatch.setattr(
        db_knowledge,
        "insert_version",
        create_autospec(db_knowledge.insert_version, side_effect=_insert_version),
    )
    monkeypatch.setattr(
        db_verdicts,
        "insert_verdict",
        create_autospec(db_verdicts.insert_verdict, side_effect=_insert_verdict),
    )
    return saved_agendas


def _sample_agenda_with_validations() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "as_of": "2026-10-23T00:00:00Z",
        "open_questions": [],
        "next_validations": [
            {
                "id": "V-001",
                "what": "10/28 盘后的 3Q26 财报",
                "due": "2026-10-28",
                "expected": "Search & YouTube 广告增速维持 >10%",
                "status": "pending",
            },
            {
                "id": "V-002",
                "what": "3Q26 预览重取一致预期",
                "due": "2026-10-24",
                "expected": "买方与卖方一致预期差距收敛",
                "status": "pending",
            },
        ],
        "watch": {},
        "wake_conditions": [],
    }


@pytest.mark.asyncio
async def test_append_verdict_next_validation_id_keeps_agenda_item_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.knowledge import service

    saved_agendas = _setup_verdict_agenda_mocks(
        monkeypatch, _sample_agenda_with_validations(), verdict_id="VD-20261024-01"
    )
    res = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict={
            "trigger": {"kind": "manual"},
            "event": "盘前常规检查",
            "verdict": "NO_CHANGE",
            "rationale": "基本面无新增变化，等待 3Q26 财报验证。",
            "next_validation": {
                "what": "10/28 盘后的 3Q26 财报",
                "due": "2026-10-28",
                "validation_id": "V-001",
            },
            "progress": "none",
        },
    )
    assert res["ok"] is True, res.get("message")
    latest_agenda = saved_agendas[-1]
    v001 = next(x for x in latest_agenda["next_validations"] if x["id"] == "V-001")
    assert v001["status"] == "pending"
    assert "result_verdict_id" not in v001


@pytest.mark.asyncio
async def test_append_verdict_trigger_ref_marks_matching_validation_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.knowledge import service

    saved_agendas = _setup_verdict_agenda_mocks(
        monkeypatch, _sample_agenda_with_validations(), verdict_id="VD-20261024-02"
    )
    res = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict={
            "trigger": {"kind": "due_validation", "ref": "V-002"},
            "event": "完成 V-002 一致预期核对",
            "verdict": "NO_CHANGE",
            "rationale": "一致预期无显著偏移，接下来看 V-001。",
            "next_validation": {
                "what": "10/28 盘后的 3Q26 财报",
                "due": "2026-10-28",
                "validation_id": "V-001",
            },
            "progress": "some",
        },
    )
    assert res["ok"] is True, res.get("message")
    latest_agenda = saved_agendas[-1]
    v001 = next(x for x in latest_agenda["next_validations"] if x["id"] == "V-001")
    v002 = next(x for x in latest_agenda["next_validations"] if x["id"] == "V-002")
    assert v002["status"] == "done"
    assert v002["result_verdict_id"] == "VD-20261024-02"
    assert v001["status"] == "pending"
    assert "result_verdict_id" not in v001


@pytest.mark.asyncio
async def test_append_verdict_trigger_signals_emits_signal_fields_consumed_and_resolves_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec

    from src.dalton.database import watch as watch_db
    from src.dalton.knowledge import service
    from src.dalton.tools.knowledge_tools import (
        knowledge_append_verdict,
        resolve_verdict_signals,
    )

    assert "next_validation 写的是下一项要看什么，不会关闭任何项" in (
        knowledge_append_verdict.description or ""
    )

    _setup_verdict_agenda_mocks(
        monkeypatch, _sample_agenda_with_validations(), verdict_id="VD-20261024-03"
    )
    mock_resolve = create_autospec(watch_db.resolve_signals)
    mock_resolve.side_effect = (
        lambda signal_ids=None, *, signal_refs=None, workspace_id=None, ticker=None, resolved_by=None, execution_id=None: len(
            signal_ids or []
        )
    )
    monkeypatch.setattr(watch_db, "resolve_signals", mock_resolve)

    verdict_payload = {
        "trigger": {"kind": "filing"},
        "trigger_signals": ["filing:0001045810-26-000012"],
        "event": "8-K 审阅完成",
        "verdict": "NO_CHANGE",
        "rationale": "常规定期披露，不影响核心假设。",
        "next_validation": {
            "what": "10/28 盘后的 3Q26 财报",
            "due": "2026-10-28",
            "validation_id": "V-001",
        },
        "progress": "some",
    }
    res = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict=verdict_payload,
    )
    assert res["ok"] is True, res.get("message")
    consumed_warnings = [
        w for w in res["warnings"] if w.get("code") == "signal_fields_consumed"
    ]
    assert len(consumed_warnings) == 1
    assert consumed_warnings[0]["dropped_fields"] == ["trigger_signals"]
    assert "这些字段已用于关闭信号，不存进结论" in consumed_warnings[0]["message"]

    count = await resolve_verdict_signals(
        workspace_id="ws-1",
        verdict=verdict_payload,
        verdict_id=res["verdict_id"],
    )
    assert count == 1
    assert mock_resolve.call_args.args[0] == ["filing:0001045810-26-000012"]
    assert mock_resolve.call_args.kwargs["resolved_by"] == "VD-20261024-03"


@pytest.mark.asyncio
async def test_append_verdict_reports_agenda_update_failed_with_expected_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When agenda_ops fail validation or op execution, append_verdict still saves the verdict and explicitly reports agenda_update: failed with error and expected structure."""
    from src.dalton.knowledge import service

    saved_agendas = _setup_verdict_agenda_mocks(
        monkeypatch, _sample_agenda_with_validations(), verdict_id="VD-20261024-04"
    )
    verdict_payload = {
        "trigger": {"kind": "earnings", "ref": "V-001"},
        "event": "META 3Q26 财报超预期",
        "verdict": "THESIS_STRENGTHENED",
        "rationale": "广告收入同比 +21% 超买方 bogey。",
        "evidence_ids": ["E-20261023-01"],
        "next_validation": {
            "what": "4Q26 财报指引兑现",
            "due": "2027-01-29",
        },
        "progress": "some",
    }

    # Case 1: Invalid op (e.g. non-existent 'close_question')
    res_bad_op = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict=verdict_payload,
        agenda_ops=[{"op": "close_question", "path": "/open_questions"}],
    )
    assert res_bad_op["ok"] is True
    assert res_bad_op["verdict_id"] == "VD-20261024-04"
    assert res_bad_op["agenda_version_no"] is None
    assert res_bad_op["agenda_update"] == "failed"
    assert "agenda_update: failed" in res_bad_op["message"]
    assert "unknown_op" in res_bad_op["message"]
    assert "Expected structure for " in res_bad_op["message"]
    assert "knowledge_update(doc=\"agenda\"" in res_bad_op["message"]
    assert len(saved_agendas) == 1

    # Case 2: Schema error in agenda_ops (e.g. invalid fields priority / due_date / verify_how)
    res_bad_schema = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict=verdict_payload,
        agenda_ops=[
            {
                "op": "append_item",
                "path": "/open_questions",
                "value": {
                    "question": "Bad schema question",
                    "priority": "P0",
                    "due_date": "2027-01-01",
                    "verify_how": "10-Q",
                },
            }
        ],
    )
    assert res_bad_schema["ok"] is True
    assert res_bad_schema["verdict_id"] == "VD-20261024-04"
    assert res_bad_schema["agenda_version_no"] is None
    assert res_bad_schema["agenda_update"] == "failed"
    assert "agenda_update: failed" in res_bad_schema["message"]
    assert "Expected structure for /open_questions/0" in res_bad_schema["message"]
    assert len(saved_agendas) == 1

    # Case 3: Missing leading '/' in agenda_ops path (previously raised OpError in repair_knowledge_ops before try/except)
    from unittest.mock import create_autospec

    from src.dalton.database import verdicts as db_verdicts

    monkeypatch.setattr(
        db_verdicts,
        "list_verdicts",
        create_autospec(
            db_verdicts.list_verdicts,
            return_value=[
                {
                    "verdict_id": "VD-20261024-04",
                    "workspace_id": "ws-1",
                    "verdict_at": datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC),
                    "word": "THESIS_STRENGTHENED",
                    "payload": {
                        **verdict_payload,
                        "id": "VD-20261024-04",
                        "at": "2026-10-24T12:00:00Z",
                    },
                }
            ],
        ),
    )

    class _BackendSpec:
        async def aread_text(self, path: str) -> str | None:
            return None

        async def awrite_text(self, path: str, content: str) -> None:
            return None

    written_files: dict[str, str] = {}
    mock_backend = create_autospec(_BackendSpec, instance=True)

    async def _awrite_text(path: str, content: str) -> None:
        written_files[path] = content

    mock_backend.awrite_text.side_effect = _awrite_text

    res_no_slash = await service.append_verdict(
        user_id="user-1",
        workspace_id="ws-1",
        verdict=verdict_payload,
        agenda_ops=[
            {
                "op": "set",
                "path": "open_questions/Q-001/status",
                "value": "resolved",
            }
        ],
        backend=mock_backend,
    )
    assert res_no_slash["ok"] is True
    assert res_no_slash["verdict_id"] == "VD-20261024-04"
    assert res_no_slash["agenda_version_no"] is None
    assert res_no_slash["agenda_update"] == "failed"
    assert "agenda_update: failed" in res_no_slash["message"]
    assert "invalid_path" in res_no_slash["message"]
    assert "path must start with '/'" in res_no_slash["message"]
    assert "verdicts.md" in written_files
    assert "VD-20261024-04" in written_files["verdicts.md"]
    assert len(saved_agendas) == 1


def test_dalton_agenda_skill_verdict_and_agenda_ops_examples_validate() -> None:
    """Extract the verdict and agenda_ops JSON examples from dalton-agenda/SKILL.md and verify they pass schema validation."""
    import re

    from src.dalton.knowledge.validate import validate_verdict

    skill_path = (
        Path(__file__).resolve().parents[3]
        / "plugins"
        / "dalton"
        / "skills"
        / "dalton-agenda"
        / "SKILL.md"
    )
    skill_text = skill_path.read_text(encoding="utf-8")
    json_blocks = [
        json.loads(match.group(1))
        for match in re.finditer(r"```json\s*\n(.*?)\n\s*```", skill_text, flags=re.DOTALL)
    ]
    verdict_examples = [
        b for b in json_blocks if isinstance(b, dict) and "verdict" in b and "next_validation" in b
    ]
    assert len(verdict_examples) == 1, "Expected a complete verdict JSON example in dalton-agenda/SKILL.md"
    verdict_example = verdict_examples[0]
    val = validate_verdict(verdict_example)
    assert val.ok, val.format_problems()
    assert val.warnings == []

    agenda_ops_examples = [b for b in json_blocks if isinstance(b, list)]
    assert len(agenda_ops_examples) == 1, "Expected a complete agenda_ops JSON example in dalton-agenda/SKILL.md"
    base_agenda = {
        "schema_version": 1,
        "as_of": "2026-10-23T00:00:00Z",
        "open_questions": [
            {
                "id": "Q-001",
                "question": "Reels 变现效率能否带动广告单价转正？",
                "why": "核心增长支柱 P1 验证",
                "value": "high",
                "urgency": "this_quarter",
                "origin": "analyst",
                "owner": "analyst",
                "status": "open",
                "attempts": [],
                "created_at": "2026-10-01T00:00:00Z",
            }
        ],
        "next_validations": [
            {
                "id": "V-001",
                "what": "3Q26 财报广告单价增速",
                "due": "2026-10-23",
                "expected": "同比增速 >= 8%",
                "status": "pending",
            }
        ],
        "watch": {},
        "wake_conditions": [],
    }
    now = datetime(2026, 10, 23, 21, 0, 0, tzinfo=UTC)
    updated_agenda, _ = apply_ops("agenda", base_agenda, agenda_ops_examples[0], now=now)
    ag_val = validate_update("agenda", base_agenda, updated_agenda, reason="Apply SKILL.md agenda_ops example")
    assert ag_val.ok, ag_val.format_problems()
    assert updated_agenda["open_questions"][0]["status"] == "resolved"
    assert updated_agenda["open_questions"][1]["id"] == "Q-002"
    assert updated_agenda["next_validations"][0]["status"] == "done"
    assert updated_agenda["next_validations"][1]["id"] == "V-002"


