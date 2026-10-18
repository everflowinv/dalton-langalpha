"""Unit tests for the Company Knowledge Layer (PK.1–PK.7) and P1.4 Legacy Migration."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.dalton.knowledge.ops import OpError, apply_ops, diff_documents, next_item_id
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
    render_published_index_md,
    render_verdicts_md,
    update_agent_md_block,
)
from src.dalton.knowledge.validate import (
    SCHEMA_NAMES,
    check_all_schemas,
    validate_schema,
    validate_update,
    validate_verdict,
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
    dt = datetime(2026, 10, 18, 9, 30, 0, tzinfo=timezone.utc)
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
    now = datetime(2026, 10, 18, 15, 0, 0, tzinfo=timezone.utc)
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
            "created_at": datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc),
        }
        rows.append(row)
        return row

    mock_insert_version.side_effect = _insert_version_impl

    async def fake_mark_files_written(kv_id: str) -> None:
        for rows in store.values():
            for r in rows:
                if r["knowledge_version_id"] == kv_id:
                    r["files_written_at"] = datetime.now(timezone.utc)

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
            "verdict_at": datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc),
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
    now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
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
            "created_at": datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc),
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


