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


def test_all_11_schemas_valid_draft_2020_12() -> None:
    # 11 = 原 10 份 + sector profile 的 sector.schema.json（sector layer P1）。
    schema_ids = check_all_schemas()
    assert set(schema_ids.keys()) == set(SCHEMA_NAMES)
    assert len(schema_ids) == 11
    assert "sector" in schema_ids
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

        # 公司工作区只渲染公司 profile 的文档（RENDERED 另含行业 profile 的
        # sector.md，公司工作区没有 sector 文档，自然不渲染它）。
        for doc_key in DOC_KEYS:
            md_rel = RENDERED.get(doc_key)
            if md_rel is None:
                continue
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
    # The verdict rejection names the delta and a minimal example (schema
    # spec rides the tool docstring now — the structure dump moved off).
    assert "缺少必填字段（补上后重试）：/trigger、/rationale、/next_validation、/progress" in v_msg
    assert "/verdict" in v_msg  # INVALID_VERDICT_ENUM surfaces as 其他问题
    assert "最小合规示例：" in v_msg
    assert '"verdict":"NO_CHANGE"' in v_msg

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


def test_thesis_pillar_cap_excludes_retired_pillars() -> None:
    """5 retired + 3 active pillars (8 total) must pass validation; 8 active pillars must fail with '请先 retire 旧支柱'."""
    from src.dalton.knowledge.validate import validate_schema

    thesis_doc = _sample_thesis_doc()
    base_pillar = copy.deepcopy(thesis_doc["pillars"][0])
    pillars: list[dict[str, Any]] = []
    for idx in range(1, 9):
        p = copy.deepcopy(base_pillar)
        p["id"] = f"P{idx}"
        if idx <= 5:
            p["status"] = "retired"
            p["retired_at"] = "2026-10-20T00:00:00Z"
            p["status_reason"] = "Migrated legacy pillar retired"
        else:
            p["status"] = "intact"
        pillars.append(p)
    thesis_doc["pillars"] = pillars

    # 5 retired + 3 active = 3 non-retired pillars <= 7 -> passes
    errors_ok = validate_schema("thesis", thesis_doc)
    assert errors_ok == []

    # 8 active pillars -> rejected with explicit message
    for p in thesis_doc["pillars"]:
        p["status"] = "intact"
        p.pop("retired_at", None)
    errors_exceeded = validate_schema("thesis", thesis_doc)
    assert len(errors_exceeded) == 1
    assert errors_exceeded[0]["code"] == "max_active_pillars_exceeded"
    assert "非 retired 的支柱不超过 7 条" in errors_exceeded[0]["message"]
    assert "请先 retire 旧支柱" in errors_exceeded[0]["message"]


@pytest.mark.asyncio
async def test_knowledge_update_dry_run_previews_diff_without_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry_run=True validates and previews diff/expected structure without writing DB versions or sandbox files."""
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge import service
    from src.dalton.tools.knowledge_tools import create_knowledge_tools

    monkeypatch.setattr(
        service,
        "verify_workspace_access",
        create_autospec(service.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    import src.dalton.tools.knowledge_tools as kt_mod

    monkeypatch.setattr(
        kt_mod,
        "verify_workspace_access",
        create_autospec(kt_mod.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_version",
        create_autospec(db_knowledge.latest_version, return_value=None),
    )
    monkeypatch.setattr(
        db_knowledge,
        "latest_valid_version",
        create_autospec(db_knowledge.latest_valid_version, return_value=None),
    )
    mock_insert = create_autospec(db_knowledge.insert_version)
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert)

    tools = create_knowledge_tools(None)
    k_update = tools[0]
    cfg = {"configurable": {"user_id": "u-1", "workspace_id": "ws-1"}}

    # 1. Invalid draft in dry_run=True returns problems + expected structure without calling insert_version
    bad_res = await k_update.ainvoke(
        {
            "name": "knowledge_update",
            "id": "tc-dry-1",
            "type": "tool_call",
            "args": {
                "doc": "company",
                "ops": [{"op": "create", "value": {"ticker": "GOOGL"}}],
                "reason": "Dry-run invalid company draft",
                "base_version": None,
                "dry_run": True,
            },
        },
        config=cfg,
    )
    bad_msg = bad_res.content if hasattr(bad_res, "content") else str(bad_res)
    assert "Validation failed for company (NOT SAVED)" in bad_msg
    assert "one_liner: string [required]" in bad_msg
    assert mock_insert.call_count == 0

    # 2. Valid draft in dry_run=True returns preview_doc, diff, and expected_structures without calling insert_version
    valid_company = {
        "ticker": "GOOGL",
        "exchange": "NASDAQ",
        "name": "Alphabet Inc.",
        "coverage_tier": "core",
        "coverage_stage": "initial",
        "one_liner": "Global search, cloud, and AI leader.",
    }
    res_dry = await service.apply_update(
        user_id="u-1",
        workspace_id="ws-1",
        doc_key="company",
        ops=[{"op": "create", "value": valid_company}],
        reason="Dry-run valid company draft",
        base_version=None,
        dry_run=True,
    )
    assert res_dry["ok"] is True
    assert res_dry["dry_run"] is True
    assert res_dry["would_be_version_no"] == 1
    assert res_dry["preview_doc"]["ticker"] == "GOOGL"
    assert any(d["path"] == "/ticker" for d in res_dry["diff"])
    assert "one_liner: string [required]" in res_dry["expected_structures"]
    assert mock_insert.call_count == 0

    # 3. Real tool entry point (ainvoke) returns message + before/after diff + processed preview_doc JSON
    ok_tool_res = await k_update.ainvoke(
        {
            "name": "knowledge_update",
            "id": "tc-dry-2",
            "type": "tool_call",
            "args": {
                "doc": "company",
                "ops": [{"op": "create", "value": valid_company}],
                "reason": "Dry-run valid company draft via tool",
                "base_version": None,
                "dry_run": True,
            },
        },
        config=cfg,
    )
    ok_msg = ok_tool_res.content if hasattr(ok_tool_res, "content") else str(ok_tool_res)
    assert "Dry-run passed for company (would save v1)" in ok_msg
    assert "Diff (before/after):" in ok_msg
    assert "Preview document (preview_doc):" in ok_msg
    assert '"name": "Alphabet Inc."' in ok_msg
    assert '"schema_version": 1' in ok_msg
    assert mock_insert.call_count == 0

    # 4. Preview document exceeding 8000 chars is truncated at 8000 chars with total length noted
    large_company = {
        **valid_company,
        "one_liner": "A" * 8500,
    }
    large_tool_res = await k_update.ainvoke(
        {
            "name": "knowledge_update",
            "id": "tc-dry-3",
            "type": "tool_call",
            "args": {
                "doc": "company",
                "ops": [{"op": "create", "value": large_company}],
                "reason": "Dry-run large company draft via tool",
                "base_version": None,
                "dry_run": True,
            },
        },
        config=cfg,
    )
    large_msg = large_tool_res.content if hasattr(large_tool_res, "content") else str(large_tool_res)
    assert "... (truncated at 8000 chars, total " in large_msg
    assert mock_insert.call_count == 0


@pytest.mark.asyncio
async def test_insert_version_and_insert_verdict_populate_workspace_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """insert_version and insert_verdict resolve ticker from workspace and write it into the INSERT statement."""
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts

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

    captured_kv_params: list[Any] = []
    captured_verdict_params: list[Any] = []
    last_op: dict[str, Any] = {}

    cur = create_autospec(_CurSpec, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    async def _exec(sql: str, params: Any = None) -> None:
        s = " ".join(str(sql).split())
        if "FROM dalton_knowledge_versions WHERE workspace_id = %s" in s:
            last_op["row"] = None
        elif "INSERT INTO dalton_knowledge_versions" in s:
            captured_kv_params.append(params)
            last_op["row"] = {
                "knowledge_version_id": "00000000-0000-0000-0000-000000000010",
                "version_no": 1,
                "ticker": params[-1],
            }
        elif "SELECT verdict_id FROM dalton_verdicts" in s:
            last_op["rows"] = []
        elif "INSERT INTO dalton_verdicts" in s:
            captured_verdict_params.append(params)
            last_op["row"] = {
                "verdict_id": params[0],
                "ticker": params[-1],
            }

    cur.execute.side_effect = _exec
    cur.fetchall.side_effect = lambda: list(last_op.get("rows", []))
    cur.fetchone.side_effect = lambda: last_op.get("row")

    tx = create_autospec(_TxSpec, instance=True)
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = None

    conn = create_autospec(_ConnSpec, instance=True)
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    conn.cursor.return_value = cur
    conn.transaction.return_value = tx

    monkeypatch.setattr(
        db_knowledge,
        "get_db_connection",
        create_autospec(db_knowledge.get_db_connection, return_value=conn),
    )
    monkeypatch.setattr(
        db_verdicts,
        "get_db_connection",
        create_autospec(db_verdicts.get_db_connection, return_value=conn),
    )
    monkeypatch.setattr(
        db_knowledge,
        "_resolve_workspace_ticker",
        create_autospec(db_knowledge._resolve_workspace_ticker, return_value="GOOGL"),
    )
    monkeypatch.setattr(
        db_verdicts,
        "_resolve_workspace_ticker",
        create_autospec(db_verdicts._resolve_workspace_ticker, return_value="GOOGL"),
    )

    kv_row = await db_knowledge.insert_version(
        user_id="u-1",
        workspace_id="ws-googl",
        doc_key="debate",
        base_version_no=None,
        content={"schema_version": 1, "as_of": "2026-10-23T05:00:00Z", "debates": []},
        content_sha256="abc",
        ops=[],
        reason="init debate",
    )
    assert kv_row["ticker"] == "GOOGL"
    assert captured_kv_params[-1][-1] == "GOOGL"

    vd_row = await db_verdicts.insert_verdict(
        user_id="u-1",
        workspace_id="ws-googl",
        verdict={
            "trigger": {"kind": "manual"},
            "event": "Initial coverage",
            "verdict": "NEW_THESIS",
            "rationale": "Initial coverage completed",
            "next_validation": {"what": "3Q26 earnings", "due": "2026-10-29"},
            "progress": "some",
        },
    )
    assert vd_row["ticker"] == "GOOGL"
    assert captured_verdict_params[-1][-1] == "GOOGL"


def test_apply_update_enforces_server_timestamps_and_clamps_future_as_of() -> None:
    """Server fills updated_at (including drivers[].updated_at) with write time and clamps pre-filled future as_of to write time."""
    write_now = datetime(2026, 10, 23, 5, 0, 0, tzinfo=UTC)
    write_iso = "2026-10-23T05:00:00Z"

    drivers_draft = {
        "as_of": "2026-10-23T04:15:00Z",
        "drivers": [
            {
                "id": "K1",
                "name": "Search AI Overviews monetization",
                "kind": "price",
                "description": "Search RPM resilience under AI Overviews",
                "updated_at": "2026-10-23T04:15:00Z",
                "leading_indicators": [
                    {
                        "id": "K1.1",
                        "name": "Search YoY growth",
                        "source": "10-Q",
                        "frequency": "quarterly",
                        "good_direction": "up",
                        "latest": {
                            "value": 14.0,
                            "unit": "pct",
                            "period": "2Q26",
                            "as_of": "2026-10-23T06:30:00Z",
                            "ref": {"kind": "filing", "form": "10-Q", "period": "2Q26"},
                            "label": "reported",
                        },
                        "readings": [
                            {
                                "value": 14.0,
                                "unit": "pct",
                                "period": "2Q26",
                                "as_of": "2026-10-23T04:15:00Z",
                                "ref": {"kind": "filing", "form": "10-Q", "period": "2Q26"},
                                "label": "reported",
                            },
                            {
                                "value": 15.0,
                                "unit": "pct",
                                "period": "3Q26",
                                "as_of": "2026-10-23T08:00:00Z",
                                "ref": {"kind": "filing", "form": "10-Q", "period": "3Q26"},
                                "label": "reported",
                            },
                        ],
                    }
                ],
            }
        ],
    }
    created_drivers, _ = apply_ops(
        "drivers",
        None,
        [{"op": "create", "value": drivers_draft}],
        reason="Init drivers",
        now=write_now,
    )
    # Top-level as_of and drivers[].updated_at are filled with server write time (05:00, not 04:15)
    assert created_drivers["as_of"] == write_iso
    assert created_drivers["drivers"][0]["updated_at"] == write_iso
    # Pre-filled as_of later than write time (06:30, 08:00) is clamped to write time (05:00); earlier as_of (04:15) is preserved
    ind = created_drivers["drivers"][0]["leading_indicators"][0]
    assert ind["latest"]["as_of"] == write_iso
    assert ind["readings"][0]["as_of"] == "2026-10-23T04:15:00Z"
    assert ind["readings"][1]["as_of"] == write_iso


def _sample_thesis_v2_with_underwriting() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "one_liner": "AI 推荐模型持续提升广告单价与转化率，当前估值具备显著安全边际",
        "aggregate_status": "intact",
        "house_view_history": [],
        "pillars": [
            {
                "id": "P1",
                "priority": "core",
                "status": "intact",
                "provenance": "approved",
                "claim": "广告推荐模型驱动单价与转化率持续提升",
                "baseline": {"text": "3Q26 广告平均单价同比 +11%", "as_of": "2026-10-18"},
                "expected_path": "FY27 广告单价维持 8%–12% 同比正增长",
                "thresholds": {
                    "confirm": [
                        {
                            "id": "T1",
                            "metric": "广告平均单价同比增速",
                            "operator": ">=",
                            "value": 8.0,
                            "unit": "pct",
                            "period": "4Q26",
                            "label": "estimate",
                        }
                    ],
                    "warn": [
                        {
                            "id": "T2",
                            "metric": "广告平均单价同比增速",
                            "operator": "<",
                            "value": 3.0,
                            "unit": "pct",
                            "period": "4Q26",
                            "label": "estimate",
                        }
                    ],
                    "break": [
                        {
                            "id": "T3",
                            "metric": "广告平均单价同比增速",
                            "operator": "<",
                            "value": 0.0,
                            "unit": "pct",
                            "period": "FY2027",
                            "label": "estimate",
                        }
                    ],
                },
                "created_at": "2026-10-01T09:00:00Z",
            }
        ],
        "evidence": [
            {
                "id": "E-20261018-01",
                "at": "2026-10-18T10:00:00Z",
                "period": "3Q26",
                "source": {
                    "type": "filing",
                    "identity": "META 3Q26 10-Q",
                    "tier": 1,
                    "ref": {"kind": "filing", "form": "10-Q", "accession": "0001326801-26-000045"},
                },
                "fact": "3Q26 广告平均单价同比 +11%，展示量同比 +10%",
                "figures": [
                    {"metric": "广告平均单价同比", "value": 11.0, "unit": "pct", "period": "3Q26", "label": "reported"}
                ],
                "pillar_ids": ["P1"],
                "signal": "confirming",
                "quality": "high",
            },
            {
                "id": "E-20261018-02",
                "at": "2026-10-18T10:30:00Z",
                "period": "FY2027",
                "source": {
                    "type": "consensus",
                    "identity": "Yahoo Finance 一致预期快照",
                    "tier": 3,
                    "ref": {"kind": "url", "url": "https://finance.yahoo.com/quote/META/analysis"},
                },
                "fact": "卖方一致预期 FY2027 GAAP 稀释 EPS 为 28.0 美元",
                "figures": [
                    {"metric": "FY2027 GAAP 稀释 EPS", "value": 28.0, "unit": "per_share", "period": "FY2027", "label": "consensus"}
                ],
                "pillar_ids": ["P1"],
                "signal": "neutral",
                "quality": "medium",
            },
        ],
        "market_view": {
            "summary": "市场担忧 FY27 资本开支挤压自由现金流，给予 21x 远期市盈率",
            "positioning": "balanced",
            "positioning_sources": ["E-20261018-02"],
            "observations": [
                {
                    "claim": "卖方一致预期 FY2027 GAAP 稀释 EPS 为 28.0 美元",
                    "who": "Yahoo Finance 卖方一致预期",
                    "as_of": "2026-10-18",
                    "sources": ["E-20261018-02"],
                    "confidence": "high",
                }
            ],
            "figures": [
                {
                    "metric": "FY2027 GAAP 稀释 EPS",
                    "period": "FY2027",
                    "value": 28.0,
                    "unit": "per_share",
                    "label": "consensus",
                }
            ],
        },
        "house_view": {
            "stance": "long",
            "conviction": "medium",
            "summary": "核心广告变现与 AI 推荐效率超预期，风险收益偏多",
            "horizon_months": 12,
            "actionability": "act_now",
            "binding_constraint": "none",
            "flip_conditions": {
                "upgrade_if": "4Q26 广告收入同比增速 >= 18% 且 FY27 Capex 指引 <= 700 亿美元时加仓并上调至高确信",
                "downgrade_if": "FY27 资本开支指引上限超过 750 亿美元且广告单价同比 < 3% 时减仓并降为观望",
            },
            "updated_at": "2026-10-18T12:00:00Z",
        },
        "underwriting": {
            "scenario_version": 1,
            "anchor": {
                "price": 580.0,
                "currency": "USD",
                "price_at": "2026-10-18",
                "source": "closing_price",
            },
            "hurdles": {
                "long_hurdle_annual": 0.15,
                "short_hurdle_annual": 0.20,
                "min_total_return": 0.10,
                "max_adverse_move_act_now": 0.25,
                "horizon_months": 12,
            },
            "states": [
                {
                    "name": "bear",
                    "condition": "广告增速放缓且折旧压制利润率；若广告单价转负则本情景失效",
                    "debate_ids": ["D1"],
                    "calc": {
                        "method": "eps_pe",
                        "period": "FY2027",
                        "basis": "GAAP 稀释 EPS",
                        "eps": {"lo": 24.0, "hi": 25.0},
                        "multiple": {"lo": 19.0, "hi": 20.0},
                        "sources": ["E-20261018-01"],
                    },
                    "cash_return_per_share": 2.0,
                },
                {
                    "name": "base",
                    "condition": "广告维持稳健增长，AI 提升转化率；若核心应用时长下滑则下调",
                    "debate_ids": ["D1"],
                    "calc": {
                        "method": "eps_pe",
                        "period": "FY2027",
                        "basis": "GAAP 稀释 EPS",
                        "eps": {"lo": 29.0, "hi": 31.0},
                        "multiple": {"lo": 22.0, "hi": 23.0},
                        "sources": ["E-20261018-01"],
                    },
                    "cash_return_per_share": 2.5,
                },
                {
                    "name": "bull",
                    "condition": "AI 商业化加速驱动收入与利润率双升；若监管限制个性化广告则失效",
                    "debate_ids": ["D1"],
                    "calc": {
                        "method": "eps_pe",
                        "period": "FY2027",
                        "basis": "GAAP 稀释 EPS",
                        "eps": {"lo": 33.0, "hi": 35.0},
                        "multiple": {"lo": 24.0, "hi": 25.0},
                        "sources": ["E-20261018-01"],
                    },
                    "cash_return_per_share": 3.0,
                },
            ],
            "stress": {
                "condition": "北美广告支出萎缩 10% 且反垄断罚款落地",
                "calc": {
                    "method": "eps_pe",
                    "period": "FY2027",
                    "basis": "GAAP 稀释 EPS",
                    "eps": {"lo": 20.0, "hi": 21.0},
                    "multiple": {"lo": 16.0, "hi": 17.0},
                    "sources": ["E-20261018-01"],
                },
                "needs_financing": "no",
            },
            "weight_bounds": {
                "bear": {"lo": 0.10, "hi": 0.25},
                "base": {"lo": 0.45, "hi": 0.60},
                "bull": {"lo": 0.25, "hi": 0.40},
                "rationale": "过去 8 个季度广告单价与展示量超预期基率",
                "sources": ["E-20261018-01"],
            },
            "protection": {
                "survival": "净现金充裕，自由现金流为正，无再融资压力",
                "needs_financing": "no",
                "value": "核心广告业务 6% 自由现金流收益率与 19x–20x 底部市盈率支撑",
                "value_range": {"lo": 456.0, "hi": 500.0},
                "path": "季度财报与广告单价高频跟踪提供路径验证",
                "failure": "自由现金流转负或核心应用日活连续两季下滑",
            },
            "breakeven_verdict": [
                {
                    "side": "long",
                    "b_row": 0.25,
                    "verdict": "clearly_above",
                    "debate_ids": ["D1"],
                    "basis": "base 权重 25% 时需约 40% bull 概率，证据支持明显高于门槛",
                },
                {
                    "side": "long",
                    "b_row": 0.5,
                    "verdict": "clearly_above",
                    "debate_ids": ["D1"],
                    "basis": "base 权重 50% 时仅需约 32% bull 概率，明显高于门槛",
                },
            ],
        },
        "official": None,
    }


def _setup_in_memory_knowledge_db(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, list[dict[str, Any]]], Any]:
    from unittest.mock import create_autospec

    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws-meta", "user_id": "u-1"},
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

    async def fake_list_verdicts(workspace_id: str, limit: int = 60) -> list[dict[str, Any]]:
        return list(reversed(verdict_store))[:limit]

    monkeypatch.setattr(db_knowledge, "latest_version", create_autospec(db_knowledge.latest_version, side_effect=fake_latest_version))
    monkeypatch.setattr(db_knowledge, "latest_valid_version", create_autospec(db_knowledge.latest_valid_version, side_effect=fake_latest_valid_version))
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", create_autospec(db_knowledge.list_all_latest_for_workspace, side_effect=fake_list_all_latest))
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_version)
    monkeypatch.setattr(db_knowledge, "mark_files_written", create_autospec(db_knowledge.mark_files_written, side_effect=fake_mark_files_written))
    monkeypatch.setattr(db_verdicts, "list_verdicts", create_autospec(db_verdicts.list_verdicts, side_effect=fake_list_verdicts))

    class MemoryBackend:
        def __init__(self) -> None:
            self.files: dict[str, str] = {}

        async def aread_text(self, path: str) -> str | None:
            return self.files.get(path)

        async def awrite_text(self, path: str, content: str) -> None:
            self.files[path] = content

    return store, MemoryBackend()


@pytest.mark.asyncio
async def test_knowledge_update_thesis_underwriting_returns_summary_in_dry_run_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """knowledge_update(doc='thesis') and knowledge_read(doc='thesis'|'all') return the underwriting summary in all states."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge.validate import validate_update
    from src.dalton.tools.knowledge_tools import create_knowledge_tools

    _store, backend = _setup_in_memory_knowledge_db(monkeypatch)
    debate_doc = {
        "schema_version": 2,
        "as_of": "2026-10-18T12:00:00Z",
        "debates": [
            {
                "id": "D1",
                "variable": "广告单价同比增速",
                "question": "AI 推荐模型能否持续推高广告单价",
                "why_it_matters": "直接决定 FY27 EPS 能否站上 30 美元",
                "rank": 1,
                "transmission": "eps_only",
                "bull": {
                    "case": "转化率持续提升带动广告单价增长",
                    "who": "多头买方",
                    "evidence_ids": ["E-20261018-01"],
                },
                "bear": {
                    "case": "高基数下广告单价增速放缓",
                    "who": "谨慎派卖方",
                    "evidence_ids": ["E-20261018-01"],
                },
                "strongest_rebuttal": {
                    "against": "my_view",
                    "text": "宏观广告预算收缩可能抵消 AI 转化率提升",
                },
                "my_view": {
                    "side": "bull",
                    "confidence": "medium",
                    "text": "AI 推荐模型仍在提升广告转化率",
                },
                "market_lean": {
                    "side": "bull",
                    "evidence": "卖方普遍预期 6% 以上增长",
                },
                "flip_condition": {
                    "text": "连续两个季度广告单价同比增速 < 4%",
                    "driver_ids": ["K1"],
                },
                "linked_pillars": ["P1"],
                "status": "open",
                "opened_at": "2026-10-01T09:00:00Z",
                "last_reviewed_at": "2026-10-18T12:00:00Z",
                "history": [
                    {
                        "at": "2026-10-18T12:00:00Z",
                        "change": "建立 D1 分歧",
                        "reason": "初始化",
                        "evidence_ids": ["E-20261018-01"],
                    }
                ],
            }
        ],
    }
    val_deb = validate_update("debate", None, debate_doc)
    assert val_deb.ok is True, val_deb.errors

    await db_knowledge.insert_version(
        user_id="u-1",
        workspace_id="ws-meta",
        doc_key="debate",
        base_version_no=None,
        content=debate_doc,
        content_sha256="d1",
        ops=[],
        reason="init debate",
    )

    tools = {t.name: t for t in create_knowledge_tools(filesystem_backend=backend)}
    update_tool = tools["knowledge_update"]
    read_tool = tools["knowledge_read"]
    assert "请务必先传 `dry_run=True` 查看赔率计算摘要" in (update_tool.description or "")

    thesis_doc = _sample_thesis_v2_with_underwriting()
    val_direct = validate_update("thesis", None, thesis_doc, debate_doc=debate_doc)
    assert val_direct.ok is True, val_direct.errors
    assert val_direct.errors == []

    cfg = {
        "configurable": {
            "user_id": "u-1",
            "workspace_id": "ws-meta",
            "turn_admitted_workspace_id": "ws-meta",
            "thread_id": "t-1",
            "run_id": "r-1",
        }
    }

    dry_msg = await update_tool.ainvoke(
        {
            "name": "knowledge_update",
            "args": {
                "doc": "thesis",
                "ops": [{"op": "create", "value": thesis_doc}],
                "reason": "预览三情景赔率卡",
                "base_version": None,
                "dry_run": True,
            },
            "id": "tc-dry-1",
            "type": "tool_call",
        },
        config=cfg,
    )
    dry_out = str(dry_msg.content if hasattr(dry_msg, "content") else dry_msg)
    assert "赔率计算摘要（试写通过，尚未保存｜立场：看多，期限：12 个月，基准股价：580.00" in dry_out
    assert "三情景价格区间：bear [456.00, 500.00]" in dry_out
    assert "看多达标终值：667.00" in dry_out
    assert "盈亏平衡所需概率（测试权重，不是主观预测）：看多：base 权重占 25% 时需 bull" in dry_out
    assert "全部权重组合是否都过最低回报要求：看多通过：是 / 看空通过：否（共检查 6 组极端权重组合）" in dry_out
    assert "最不利情景（bear 区间下沿，含现金回报）相对基准股价的跌幅：" in dry_out
    assert "估值倍数变动占目标回报比例：" in dry_out

    short_thesis_doc = copy.deepcopy(thesis_doc)
    short_thesis_doc["house_view"]["stance"] = "short"
    short_thesis_doc["house_view"]["actionability"] = "wait_price"
    short_thesis_doc["house_view"]["binding_constraint"] = "price"
    short_thesis_doc["house_view"]["binding_constraint_text"] = "等待反弹至做空建仓区间"
    short_thesis_doc["house_view"]["entry_band"] = {"lo": 620.0, "hi": 650.0, "unit": "USD"}
    short_dry_msg = await update_tool.ainvoke(
        {
            "name": "knowledge_update",
            "args": {
                "doc": "thesis",
                "ops": [{"op": "create", "value": short_thesis_doc}],
                "reason": "预览空头三情景赔率卡",
                "base_version": None,
                "dry_run": True,
            },
            "id": "tc-dry-short",
            "type": "tool_call",
        },
        config=cfg,
    )
    short_dry_out = str(
        short_dry_msg.content if hasattr(short_dry_msg, "content") else short_dry_msg
    )
    assert "最不利情景（bull 区间上沿）相对基准股价的涨幅：" in short_dry_out

    neutral_thesis_doc = copy.deepcopy(thesis_doc)
    neutral_thesis_doc["house_view"]["stance"] = "neutral"
    neutral_thesis_doc["house_view"]["actionability"] = "wait_evidence"
    neutral_thesis_doc["house_view"]["binding_constraint"] = "evidence"
    neutral_thesis_doc["house_view"]["binding_constraint_text"] = "等待下一季验证"
    neutral_thesis_doc["house_view"]["trigger_refs"] = ["V-001"]
    neutral_dry_msg = await update_tool.ainvoke(
        {
            "name": "knowledge_update",
            "args": {
                "doc": "thesis",
                "ops": [{"op": "create", "value": neutral_thesis_doc}],
                "reason": "预览中性立场三情景赔率卡",
                "base_version": None,
                "dry_run": True,
            },
            "id": "tc-dry-neutral",
            "type": "tool_call",
        },
        config=cfg,
    )
    neutral_dry_out = str(
        neutral_dry_msg.content
        if hasattr(neutral_dry_msg, "content")
        else neutral_dry_msg
    )
    assert "最不利情景不利变动：中性或暂不表态时不计算" in neutral_dry_out

    commit_msg = await update_tool.ainvoke(
        {
            "name": "knowledge_update",
            "args": {
                "doc": "thesis",
                "ops": [{"op": "create", "value": thesis_doc}],
                "reason": "正式写入三情景赔率卡",
                "base_version": None,
                "dry_run": False,
            },
            "id": "tc-commit-1",
            "type": "tool_call",
        },
        config=cfg,
    )
    commit_out = str(commit_msg.content if hasattr(commit_msg, "content") else commit_msg)
    assert "赔率计算摘要（已保存，版本 1｜立场：看多，期限：12 个月，基准股价：580.00" in commit_out
    assert "三情景价格区间：bear [456.00, 500.00]" in commit_out
    assert "全部权重组合是否都过最低回报要求：看多通过：是" in commit_out

    # knowledge_read(doc="thesis") and knowledge_read(doc="all") attach underwriting_summary
    read_single = await read_tool.ainvoke(
        {
            "name": "knowledge_read",
            "args": {"doc": "thesis"},
            "id": "tc-read-1",
            "type": "tool_call",
        },
        config=cfg,
    )
    read_single_json = json.loads(str(read_single.content if hasattr(read_single, "content") else read_single))
    assert "underwriting_summary" in read_single_json
    assert "赔率计算摘要（已保存，版本 1｜立场：看多" in read_single_json["underwriting_summary"]
    assert "三情景价格区间：bear [456.00, 500.00] (中点 478.00, 含现金终值 480.00)｜base [638.00, 713.00]" in read_single_json["underwriting_summary"]

    read_all = await read_tool.ainvoke(
        {
            "name": "knowledge_read",
            "args": {"doc": "all"},
            "id": "tc-read-all",
            "type": "tool_call",
        },
        config=cfg,
    )
    read_all_json = json.loads(str(read_all.content if hasattr(read_all, "content") else read_all))
    assert "underwriting_summary" in read_all_json["documents"]["thesis"]
    assert "三情景价格区间：bear [456.00, 500.00]" in read_all_json["documents"]["thesis"]["underwriting_summary"]

    # When validation / scenario_math fails, first line clearly warns that it was not saved and lists errors first
    bad_thesis = copy.deepcopy(thesis_doc)
    bad_thesis["underwriting"]["states"][0]["calc"]["eps"] = {"lo": 36.0, "hi": 38.0}
    fail_msg = await update_tool.ainvoke(
        {
            "name": "knowledge_update",
            "args": {
                "doc": "thesis",
                "ops": [{"op": "create", "value": bad_thesis}],
                "reason": "测试错误状态摘要",
                "base_version": 1,
                "dry_run": True,
            },
            "id": "tc-fail-1",
            "type": "tool_call",
        },
        config=cfg,
    )
    fail_out = str(fail_msg.content if hasattr(fail_msg, "content") else fail_msg)
    assert "赔率计算摘要（这一版没有保存成功（或计算有错误），下面的数字不能写进正文｜" in fail_out
    assert "\n- 错误：" in fail_out


@pytest.mark.asyncio
async def test_apply_update_initial_direct_edit_thesis_validates_models_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-time import of preset data/thesis.json loads models context ({} when absent) so unregistered model refs are marked schema_valid=False."""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.knowledge import service

    _store, backend = _setup_in_memory_knowledge_db(monkeypatch)

    preset_thesis = _sample_thesis_v2_with_underwriting()
    preset_thesis["underwriting"]["states"][1]["calc"] = {
        "method": "reference",
        "price_range": {"lo": 638.0, "hi": 713.0},
        "ref": "model/UNREGISTERED-v01.xlsx#DCF!G18",
    }
    backend.files["data/thesis.json"] = json.dumps(preset_thesis, ensure_ascii=False)

    res = await service.apply_update(
        user_id="u-1",
        workspace_id="ws-meta",
        doc_key="thesis",
        ops=[{"op": "set", "path": "/house_view/summary", "value": "尝试在预置文件基础上修改"}],
        reason="test initial direct_edit validation",
        base_version=None,
        source="tool",
        backend=backend,
        skip_access_check=True,
    )
    # Initial file was imported as v1 (direct_edit), so base_version=None triggers base_version_mismatch,
    # and the imported v1 row must be marked schema_valid=False with unresolvable_reference_ref in warnings.
    assert res["ok"] is False
    v1_row = await db_knowledge.latest_version("ws-meta", "thesis")
    assert v1_row is not None
    assert v1_row["version_no"] == 1
    assert v1_row["source"] == "direct_edit"
    assert v1_row["schema_valid"] is False
    assert any(w.get("code") == "unresolvable_reference_ref" for w in (v1_row.get("warnings") or []))


def test_structured_write_example_md_validates_without_repair() -> None:
    """Extract JSON blocks from structured-write-example.md and validate directly via validate_update (without server-side field rename repair)."""
    import re
    from pathlib import Path
    from src.dalton.knowledge.validate import validate_update

    repo_root = Path(__file__).resolve().parents[3]
    example_md = (
        repo_root
        / "plugins/dalton/skills/dalton-knowledge/references/structured-write-example.md"
    )
    text = example_md.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, flags=re.DOTALL)
    assert len(blocks) == 4, f"Expected 4 JSON blocks in structured-write-example.md, got {len(blocks)}"

    models_doc, agenda_doc, debate_doc, thesis_doc = [json.loads(b) for b in blocks]

    val_models = validate_update("models", None, models_doc)
    assert val_models.ok is True, val_models.errors
    assert val_models.errors == []

    val_agenda = validate_update("agenda", None, agenda_doc)
    assert val_agenda.ok is True, val_agenda.errors
    assert val_agenda.errors == []

    val_debate = validate_update("debate", None, debate_doc)
    assert val_debate.ok is True, val_debate.errors
    assert val_debate.errors == []

    val_thesis = validate_update(
        "thesis",
        None,
        thesis_doc,
        debate_doc=debate_doc,
        agenda_doc=agenda_doc,
        models_doc=models_doc,
    )
    assert val_thesis.ok is True, val_thesis.errors
    assert val_thesis.errors == []

    # Also verify _sample_thesis_v2_with_underwriting() passes validate_update directly with 0 errors
    sample_thesis = _sample_thesis_v2_with_underwriting()
    val_sample = validate_update(
        "thesis",
        None,
        sample_thesis,
        debate_doc=debate_doc,
        agenda_doc=agenda_doc,
        models_doc=models_doc,
    )
    assert val_sample.ok is True, val_sample.errors
    assert val_sample.errors == []


def test_b4b_skill_prompts_field_paths_and_enums_match_schemas() -> None:
    """Hierarchically resolve dotted field paths and per-field enum values in all 16 batch-4b skill files against src/dalton/knowledge/schemas/*.schema.json.

    Known scope: fenced code blocks (```...```) are excluded from this inline-backtick scanner;
    complete JSON examples inside fenced blocks are validated directly by
    ``test_structured_write_example_md_validates_without_repair`` via ``validate_update``.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    schemas_dir = repo_root / "src/dalton/knowledge/schemas"
    schemas: dict[str, dict[str, Any]] = {}
    for p in sorted(schemas_dir.glob("*.schema.json")):
        key = p.name.removesuffix(".schema.json")
        schemas[key] = json.loads(p.read_text(encoding="utf-8"))

    doc_roots = (
        "thesis",
        "debate",
        "agenda",
        "drivers",
        "models",
        "management",
        "company",
        "lessons",
        "sector",
        "verdict",
    )

    def _deref(doc_key: str, node: Any) -> list[tuple[str, dict[str, Any]]]:
        """Dereference $ref, combinators, and array items while preserving each node's owning schema file key."""
        if not isinstance(node, dict):
            return []
        out: list[tuple[str, dict[str, Any]]] = [(doc_key, node)]
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref.startswith("#/$defs/"):
                target_key = ref.removeprefix("#/$defs/")
                target = (schemas[doc_key].get("$defs") or {}).get(target_key)
                if target:
                    out.extend(_deref(doc_key, target))
            elif ref.startswith("common#/$defs/"):
                target_key = ref.removeprefix("common#/$defs/")
                target = (schemas["common"].get("$defs") or {}).get(target_key)
                if target:
                    out.extend(_deref("common", target))
        for comb in ("oneOf", "anyOf", "allOf"):
            for sub in node.get(comb) or []:
                out.extend(_deref(doc_key, sub))
        if "items" in node and isinstance(node["items"], dict):
            out.extend(_deref(doc_key, node["items"]))
        return out

    def _step_prop(
        doc_key: str, node: dict[str, Any], prop: str
    ) -> list[tuple[str, dict[str, Any]]]:
        res: list[tuple[str, dict[str, Any]]] = []
        for cur_dk, n in _deref(doc_key, node):
            props = n.get("properties")
            if isinstance(props, dict) and prop in props:
                res.append((cur_dk, props[prop]))
            elif (
                n.get("type") == "object"
                and "properties" not in n
                and n.get("additionalProperties") is not False
                and prop in ("date", "at", "due")
            ):
                # agenda.wake_conditions[].params is an open object where kind="date" stores date/at/due
                res.append((cur_dk, {"type": "string"}))
        return res

    all_named_nodes: list[tuple[str, str, dict[str, Any]]] = []
    prop_names: set[str] = set()
    enum_strings: set[str] = set()

    def _collect_all_props(
        doc_key: str, node: Any, seen: set[tuple[str, int]] | None = None
    ) -> None:
        if seen is None:
            seen = set()
        key_id = (doc_key, id(node))
        if key_id in seen or not isinstance(node, dict):
            return
        seen.add(key_id)
        props = node.get("properties")
        if isinstance(props, dict):
            for k, v in props.items():
                prop_names.add(k)
                if isinstance(v, dict):
                    all_named_nodes.append((doc_key, k, v))
                _collect_all_props(doc_key, v, seen)
        enums = node.get("enum")
        if isinstance(enums, list):
            for item in enums:
                if item is not None:
                    enum_strings.add(str(item))
        const_val = node.get("const")
        if const_val is not None:
            enum_strings.add(str(const_val))
        for v in node.values():
            if isinstance(v, dict):
                _collect_all_props(doc_key, v, seen)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _collect_all_props(doc_key, item, seen)

    for dk in doc_roots:
        if dk in schemas:
            _collect_all_props(dk, schemas[dk])
    _collect_all_props("common", schemas["common"])

    def _resolve_path(
        path_str: str, *, allow_doc_root_singleton: bool = True
    ) -> list[tuple[str, dict[str, Any]]]:
        segs = [s.removesuffix("[]") for s in path_str.split(".")]
        # Strip knowledge_read response wrapper key `content.` so `content.house_view.entry_band` still resolves hierarchically
        if segs and segs[0] == "content" and len(segs) > 1:
            segs = segs[1:]
        if not segs:
            return []
        if segs[0] in doc_roots and (len(segs) > 1 or allow_doc_root_singleton):
            dk = segs[0]
            if len(segs) == 1:
                return [(dk, schemas[dk])]
            candidates: list[tuple[str, dict[str, Any]]] = [(dk, schemas[dk])]
            rem = segs[1:]
        else:
            candidates = [(dk, v) for dk, k, v in all_named_nodes if k == segs[0]]
            rem = segs[1:]
        for seg in rem:
            nxt: list[tuple[str, dict[str, Any]]] = []
            for dk, node in candidates:
                nxt.extend(_step_prop(dk, node, seg))
            candidates = nxt
            if not candidates:
                break
        return candidates

    def _get_field_enums(candidates: list[tuple[str, dict[str, Any]]]) -> set[str]:
        enums: set[str] = set()
        for dk, node in candidates:
            for _, n in _deref(dk, node):
                if "enum" in n and isinstance(n["enum"], list):
                    for e in n["enum"]:
                        if e is not None:
                            enums.add(str(e))
                if "const" in n and n["const"] is not None:
                    enums.add(str(n["const"]))
                if n.get("type") == "boolean" or (
                    isinstance(n.get("type"), list) and "boolean" in n["type"]
                ):
                    enums.update({"true", "false", "True", "False"})
        return enums

    # Explicit mapping for bare field names used in assignments (`field="val"` / `field: val`) or prose enum lists
    # that appear in multiple schema locations (or shadow a doc root name like `verdict`):
    default_bare_field_to_schema_path: dict[str, str] = {
        "verdict": "verdict.verdict",  # knowledge_append_verdict 的五词结论枚举（verdict.schema.json）
        "kind": "agenda.wake_conditions[].kind",  # agenda 唤醒条件 wake_conditions[].kind 枚举（agenda.schema.json）
        "method": "thesis.underwriting.states[].calc.method",  # 三情景估值计算方法枚举（thesis.schema.json）
        "side": "thesis.underwriting.breakeven_verdict[].side",  # 胜率裁决方向枚举（thesis.schema.json）
        "schema_version": "thesis.schema_version",  # 知识文档 schema_version 枚举（1 | 2）
        "progress": "verdict.progress",  # knowledge_append_verdict 的议程进展枚举（verdict.schema.json）
        "unit": "thesis.evidence[].figures[].unit",  # 证据数值条目单位枚举（common.schema.json#/$defs/unit）
        "label": "thesis.evidence[].figures[].label",  # 证据数值条目标签枚举（common.schema.json#/$defs/label）
    }

    # Explicit allowed value sets for bare tool/script parameters used in `param=val` / `param: val` expressions
    # (none of these names exist in any knowledge schema):
    bare_tool_arg_allowed_values: dict[str, set[str]] = {
        "doc": {"thesis", "debate", "agenda", "drivers", "models", "management", "company", "lessons", "sector", "all"},  # knowledge_read / knowledge_update 的 doc 参数
        "dry_run": {"true", "false", "True", "False"},  # knowledge_update 的 dry_run 布尔参数
        "done": {"true", "false", "True", "False"},  # dalton_check_done 返回的 done 布尔字段
        "passed": {"true", "false", "True", "False"},  # research/<as_of>-call.gates.json 的 passed 布尔字段
        "ok": {"true", "false", "True", "False"},  # 采集脚本与桥接响应的 ok 布尔状态字段
        "complete": {"true", "false", "True", "False"},  # AlphaEngine 分页响应的 complete 布尔字段
        "completed": {"true", "false", "True", "False"},  # collect.py 来源状态的 completed 布尔字段
        "incomplete": {"true", "false", "True", "False"},  # collect.py 来源状态的 incomplete 布尔字段
        "has_more": {"true", "false", "True", "False"},  # 分页响应与 collect.py 的 has_more 布尔字段
        "all_sources_ok": {"true", "false", "True", "False"},  # collect.py 汇总 JSON 的 all_sources_ok 布尔字段
        "short_circuit_eligible": {"true", "false", "True", "False"},  # collect.py 汇总 JSON 的 short_circuit_eligible 布尔字段
        "material": {"yes", "no"},  # collect.py commit --material 与增量重要度判定枚举
        "stale": {"true", "false", "True", "False"},  # 周度复盘与研究库陈旧标记布尔字段
        "call_candidate": {"true", "false", "True", "False"},  # 周度复盘候选标记布尔字段
        "description_verified": {"true", "false", "True", "False"},  # dalton-earnings-preview 校验脚本布尔字段
        "operation": {"ask", "filings", "get_document", "get_recording_status", "get_recording_task", "list_recording_tasks", "read", "search_library"},  # researchdata 桥接调用的 operation 参数
        "route": {"cn-hk", "funda-ai", "tmtb", "alphaengine", "guidepoint", "sales-notes", "company-wiki"},  # researchdata 桥接调用的 route 参数
        "sort": {"newest", "relevance"},  # researchdata 检索排序参数
        "search_in": {"title", "content", "all"},  # researchdata 检索范围参数
        "meeting_scope": {"personal", "institution", "all"},  # AlphaEngine 会议检索范围参数
        "document_code": {"summary"},  # researchdata 摘要文档类型参数
        "as_of_basis": {"unknown"},  # researchdata 日期基准回退标记
        "layer": {"digest"},  # researchdata 内容层级标记
        "independent": {"true", "false", "True", "False"},  # 证据独立性核验布尔字段
        "body_locked": {"true", "false", "True", "False"},  # 研报正文权限锁定布尔字段
        "listing_currency": {"HKD", "USD", "CNY"},  # 跨币种换算示例中的挂牌币种参数
        "financial_currency": {"HKD", "USD", "CNY"},  # 跨币种换算示例中的财报币种参数
        "publisher": {"OpenClaw"},  # 派生来源示例中的发布方标识
        "round": {"2"},  # 多轮取证轮次示例参数
    }

    # Non-schema identifiers that legitimately appear in backticks in the 12 schema-focused skill files,
    # each with its explicit reason (no stale or unused keys):
    non_schema_allowlist: dict[str, str] = {
        # LangChain / Dalton tools & parameters
        "knowledge_read": "LangChain tool name",
        "knowledge_update": "LangChain tool name",
        "knowledge_append_verdict": "LangChain tool name",
        "dalton_check_done": "LangChain tool name",
        "check_draft": "LangChain tool name",
        "check_language": "LangChain tool name",
        "publish_report": "LangChain tool name",
        "execute_code": "LangChain tool name",
        "web_search": "LangChain tool name",
        "base_version": "knowledge_update tool parameter",
        "dry_run": "knowledge_update tool parameter",
        "agenda_ops": "knowledge_append_verdict tool parameter",
        "latest_version_no": "knowledge_read response field",
        "underwriting_summary": "knowledge_update / knowledge_read response field",
        # Error codes & database table names documented in dalton-knowledge/SKILL.md
        "append_only_violation": "validate.py error code",
        "official_is_publish_only": "validate.py error code",
        "target_needs_model_cell": "validate.py error code",
        "model_needs_model_cell": "validate.py error code",
        "model_tier_1_forbidden": "validate.py error code",
        "dalton_knowledge_versions": "Postgres table name",
        "dalton_sector_members": "Postgres table name",
        "dalton_verdicts": "Postgres table name",
        # Mandate setting in scenario_math.py
        "max_entry_distance": "DaltonMandateSettings parameter in scenario_math.py",
        # Call gate result file (.gates.json) & review.json / .progress.json fields
        "failed_gates": "research/<as_of>-call.gates.json field",
        "actionability_after": "research/<as_of>-call.gates.json v1 downgrade record field",
        "run_id": ".progress.json / tracker run identifier",
        "resume_hint": ".progress.json field",
        "must_fix": "review.json field",
        "nice_to_have": "review.json field",
        "draft_sha256": "review.json field",
        "bb_check": "dalton-bullbear mechanical check script name",
        # External MCP / researchdata bridge & collect.py fields
        "dalton_sec": "SEC filing MCP server prefix",
        "get_earnings_history": "yfinance MCP tool operation",
        "get_options_chain": "yfinance MCP tool operation",
        "get_options_snapshot": "yfinance MCP tool operation",
        "street_estimate": "consensus snapshot field",
        "end_date": "MCP query date parameter",
        "to_date": "MCP query date parameter",
        "doc_id": "AlphaEngine document parameter",
        "type_ids": "AlphaEngine filter parameter",
        "foreign_report": "AlphaEngine document type",
        "message_id": "tmtb document parameter",
        "next_offset": "AlphaEngine pagination field",
        "content_field": "AlphaEngine get_document parameter",
        "content_sha256": "AlphaEngine get_document response field",
        "available_content_fields": "AlphaEngine get_document response field",
        "original_text": "AlphaEngine content_field value",
        "digest_cn": "AlphaEngine content_field value",
        "main_point": "AlphaEngine content_field value",
        "question_answer": "AlphaEngine content_field value",
        "get_document": "AlphaEngine researchdata operation",
        "search_library": "Guidepoint researchdata operation",
        "eps_per_1pct_revenue": "bullbear sensitivity table key",
        "eps_per_10bn_expense": "bullbear sensitivity table key",
        "implied_total_expenses_bn": "bullbear sensitivity table key",
        "all_sources_ok": "collect.py summary JSON field",
        "failed_sources": "collect.py summary JSON field",
        "internal_status": "collect.py summary JSON field",
        "short_circuit_eligible": "collect.py summary JSON field",
        "auto_flags": "collect.py summary JSON field",
        "packet_json": "collect.py summary JSON field",
        "period_alignment": "collect.py consensus delta field",
        "period_end": "collect.py consensus delta field",
        "no_period_info": "collect.py period_alignment value",
        "prev_key": "collect.py consensus delta field",
        "delta_note": "dalton_check_done parameter for dalton-thesis-delta",
        "researchdata_transport_unconfigured": "collect.py transport error status token",
    }

    # Dotted prefixes that belong to config.yaml, MCP tool calls, or external script JSON (not knowledge schemas)
    non_schema_dotted_prefixes = (
        "coverage_units.enabled",
        "workflow.enabled",
        "fundamentals.get_historical_valuation",
        "dalton_sec.list_filings",
        "x_api.search_posts",
        "_sources.identity",
        "plan.source",
        "metadata.doc_id",
        "params.query",
        "params.industry_names",
        "params.cursor",
        "params.content_field",
        "tools.researchdata",
        "counts.internal",
        "counts.alphaengine",
        "counts.sec",
        "consensus.price",
        "coverage.complete",
        "coverage.continuation_params",
        "hashlib.sha256",
    )

    banned_tokens = {
        "eps_band",
        "multiple_band",
        "price_band",
        "derivation",
        "cash_yield",
        "mandate_snapshot",
        "upgrade_or_add",
        "downgrade_or_trim",
        "multiple_regime_realized_in_cashflow",
        "multiple_regime_evidence_ids",
        "squeeze_scenario",
        "indeterminate",
        "scenario_irr",
        "src.dalton.knowledge.scenario_math",
    }

    dotted_field_re = re.compile(
        r"^[a-z_][a-z0-9_]*(\[\])?(\.[a-z_][a-z0-9_]*(\[\])?)+$"
    )
    # Require `==`, `=`, or `:` followed by whitespace or quotes so URI schemes like `evidence:E-...` or `site:hkexnews.hk` are not treated as assignments
    assign_re = re.compile(
        r"^([a-z_][a-z0-9_.\[\]]*)\s*(?:==|=|:\s+|:\s*(?=[\"']))\s*[\"']?([A-Za-z0-9_.-]+)[\"']?$"
    )
    prose_enum_re = re.compile(
        r"`([a-z_][a-z0-9_.\[\]]*)`(?:\s*[（(〔][^`）)〕]{0,12}?|\s*(?:为|取|限|只能是|必须是|不是|设为|填)\s*)(`[^`\n]+`(?:\s*(?:/|、|或|\|)\s*`[^`\n]+`)+)"
    )
    snake_token_re = re.compile(r"^[a-z][a-z0-9_]*_[a-z0-9_]+$")

    v2_schema_focused_files = {
        "plugins/dalton/skills/dalton-knowledge/SKILL.md",
        "plugins/dalton/skills/dalton-knowledge/references/structured-write-example.md",
        "plugins/dalton/skills/dalton-conventions/references/risk-reward.md",
        "plugins/dalton/skills/dalton-conventions/references/nonlinear.md",
        "plugins/dalton/skills/dalton-bullbear/SKILL.md",
        "plugins/dalton/skills/dalton-call/SKILL.md",
        "plugins/dalton/skills/dalton-call/references/evidence-gate.md",
        "plugins/dalton/skills/dalton-call/references/sizing.md",
        "plugins/dalton/skills/dalton-call/templates/call.md",
        "plugins/dalton/skills/dalton-earnings-preview/SKILL.md",
        "plugins/dalton/skills/dalton-earnings-flash/templates/flash.md",
        "plugins/dalton/skills/dalton-thesis-delta/SKILL.md",
    }

    def _resolve_field_for_enum_check(
        lhs: str,
        bare_map: dict[str, str],
    ) -> tuple[list[tuple[str, dict[str, Any]]], set[str] | None, str | None]:
        """Resolve `lhs` (dotted or bare) to `(schema_candidates, tool_arg_allowed, error_reason)`."""
        clean_lhs = lhs.removesuffix("[]")
        if dotted_field_re.match(lhs):
            if any(
                lhs == p or lhs.startswith(p + ".")
                for p in non_schema_dotted_prefixes
            ):
                return [], set(), None
            cands = _resolve_path(lhs, allow_doc_root_singleton=False)
            if not cands:
                return [], None, f"unresolved field path {lhs!r}"
            return cands, None, None

        # Bare field name: doc root names with assignments are treated as fields, never doc roots
        if clean_lhs in bare_map:
            mapped_path = bare_map[clean_lhs]
            cands = _resolve_path(mapped_path, allow_doc_root_singleton=False)
            if not cands:
                return [], None, f"mapped path {mapped_path!r} for bare field {lhs!r} did not resolve"
            return cands, None, None

        if clean_lhs in bare_tool_arg_allowed_values:
            return [], bare_tool_arg_allowed_values[clean_lhs], None

        matches = [(dk, v) for dk, k, v in all_named_nodes if k == clean_lhs]
        if len(matches) == 0:
            return [], None, f"unknown bare field {lhs!r} (not in schemas or explicit bare-field table)"
        if len(matches) > 1:
            return (
                [],
                None,
                f"ambiguous bare field {lhs!r} appears in {len(matches)} schema locations; use full path or add to bare_field_to_schema_path",
            )
        return matches, None, None

    def _scan_text(
        rel_path: str,
        content: str,
        *,
        check_single_snake: bool = True,
        bare_map: dict[str, str] | None = None,
    ) -> list[str]:
        eff_bare_map = bare_map if bare_map is not None else default_bare_field_to_schema_path
        problems: list[str] = []
        for banned in banned_tokens:
            if banned in content:
                problems.append(f"{rel_path}: banned token {banned!r}")

        no_fences = re.sub(r"```.*?```", "", content, flags=re.DOTALL)

        # 1) Check prose enum lists like `b_row`（`0.25` / `0.5` / `both`）
        for pm in prose_enum_re.finditer(no_fences):
            field, raw_list = pm.group(1), pm.group(2)
            cands, tool_allowed, _err = _resolve_field_for_enum_check(field, eff_bare_map)
            if tool_allowed is not None:
                allowed = tool_allowed
            elif cands:
                allowed = _get_field_enums(cands)
            else:
                continue
            if not allowed:
                continue
            for val in [v.strip("\"'") for v in re.findall(r"`([^`\n]+)`", raw_list)]:
                if val not in allowed:
                    problems.append(
                        f"{rel_path}: field {field!r} enum value {val!r} not in {sorted(allowed)}"
                    )

        # 2) Check backticked paths, `field="val"` / `field: val` assignments, and single snake_case tokens
        for raw_token in re.findall(r"`([^`\n]+)`", no_fences):
            token = raw_token.strip()
            if token.endswith((".md", ".json", ".log", ".py", ".xlsx", ".yaml", ".sh")):
                continue

            am = assign_re.match(token)
            if am:
                lhs, rhs = am.group(1), am.group(2)
                cands, tool_allowed, err = _resolve_field_for_enum_check(lhs, eff_bare_map)
                if err:
                    problems.append(f"{rel_path}: {err} in {token!r}")
                    continue
                if tool_allowed is not None:
                    if tool_allowed and rhs not in tool_allowed:
                        problems.append(
                            f"{rel_path}: invalid value in {token!r} (allowed for {lhs!r}: {sorted(tool_allowed)})"
                        )
                    continue
                allowed = _get_field_enums(cands)
                if allowed and rhs not in allowed:
                    problems.append(
                        f"{rel_path}: invalid enum assignment {token!r} (allowed for {lhs!r}: {sorted(allowed)})"
                    )
                continue

            if dotted_field_re.match(token):
                if any(
                    token == p or token.startswith(p + ".")
                    for p in non_schema_dotted_prefixes
                ):
                    continue
                if not _resolve_path(token):
                    problems.append(f"{rel_path}: unresolved dotted field path {token!r}")
            elif check_single_snake and snake_token_re.match(token.removesuffix("[]")):
                clean = token.removesuffix("[]")
                if (
                    clean not in prop_names
                    and clean not in enum_strings
                    and clean not in doc_roots
                    and clean not in non_schema_allowlist
                ):
                    problems.append(f"{rel_path}: unknown snake_case identifier {token!r}")
        return problems

    # Negative self-checks (Items 1 & 2): all must be flagged as failures
    neg_checks: list[tuple[str, list[str]]] = [
        (
            "underwriting.anchor.as_of",
            _scan_text("negative-self-check", "锚点时间写在 `underwriting.anchor.as_of`"),
        ),
        (
            'house_view.stance="clearly_above"',
            _scan_text("negative-self-check", '方向填 `house_view.stance="clearly_above"`'),
        ),
        (
            'house_view.stance: "clearly_above"',
            _scan_text("negative-self-check", '方向填 `house_view.stance: "clearly_above"`'),
        ),
        (
            'thesis.evidence[].figures[].label="market_color"',
            _scan_text(
                "negative-self-check",
                '数值标签填 `thesis.evidence[].figures[].label="market_color"`',
            ),
        ),
        (
            'thesis.evidence[].figures[].unit="made_up"',
            _scan_text(
                "negative-self-check",
                '单位填 `thesis.evidence[].figures[].unit="made_up"` 或 `unit="made_up"`',
            ),
        ),
        (
            'verdict="clearly_above"',
            _scan_text("negative-self-check", '五词结论填 `verdict="clearly_above"`'),
        ),
        (
            'verdict="THESIS_BROKEN" (against breakeven_verdict[].verdict)',
            _scan_text(
                "negative-self-check",
                '胜率裁决填 `verdict="THESIS_BROKEN"`',
                bare_map={
                    **default_bare_field_to_schema_path,
                    "verdict": "thesis.underwriting.breakeven_verdict[].verdict",
                },
            ),
        ),
        (
            'kind="volume" (in agenda wake_conditions context)',
            _scan_text("negative-self-check", '唤醒条件填 `kind="volume"`'),
        ),
        (
            'status="open" (ambiguous bare field not in table)',
            _scan_text("negative-self-check", '裸字段未列路径 `status="open"`'),
        ),
        (
            'invented_field="clearly_above"',
            _scan_text("negative-self-check", '自造字段 `invented_field="clearly_above"`'),
        ),
        (
            "b_row prose enum both",
            _scan_text("negative-self-check", "测试权重行 `b_row`（`0.25` / `0.5` / `both`）"),
        ),
    ]
    for label, probs in neg_checks:
        assert probs, f"Expected negative self-check {label!r} to fail, but got no problems"
        print(f"NEGATIVE_SELF_CHECK [{label}]: {probs}")

    # Positive self-checks: valid bare assignments in existing prompts must pass
    pos_checks = _scan_text(
        "positive-self-check",
        '合法写法：`verdict="NEW_THESIS"`、`kind = "date"`、`debates[].status = "open"`、`source.ref.kind="model_cell"`',
    )
    assert pos_checks == [], f"Expected positive self-check to pass, got {pos_checks}"

    b4b_skill_files = [
        "plugins/dalton/skills/dalton-knowledge/SKILL.md",
        "plugins/dalton/skills/dalton-knowledge/references/structured-write-example.md",
        "plugins/dalton/skills/dalton-conventions/SKILL.md",
        "plugins/dalton/skills/dalton-conventions/references/risk-reward.md",
        "plugins/dalton/skills/dalton-conventions/references/evidence-and-sources.md",
        "plugins/dalton/skills/dalton-conventions/references/nonlinear.md",
        "plugins/dalton/skills/dalton-bullbear/SKILL.md",
        "plugins/dalton/skills/dalton-call/SKILL.md",
        "plugins/dalton/skills/dalton-call/references/evidence-gate.md",
        "plugins/dalton/skills/dalton-call/references/sizing.md",
        "plugins/dalton/skills/dalton-call/templates/call.md",
        "plugins/dalton/skills/dalton-earnings-preview/SKILL.md",
        "plugins/dalton/skills/dalton-earnings-flash/SKILL.md",
        "plugins/dalton/skills/dalton-earnings-flash/templates/flash.md",
        "plugins/dalton/skills/dalton-thesis-delta/SKILL.md",
        "plugins/dalton/skills/dalton-weekly-review/SKILL.md",
    ]

    all_problems: list[str] = []
    for rel_path in b4b_skill_files:
        content = (repo_root / rel_path).read_text(encoding="utf-8")
        all_problems.extend(
            _scan_text(
                rel_path,
                content,
                check_single_snake=(rel_path in v2_schema_focused_files),
            )
        )

    assert all_problems == [], f"Schema field/enum mismatches in skill prompts: {all_problems}"





