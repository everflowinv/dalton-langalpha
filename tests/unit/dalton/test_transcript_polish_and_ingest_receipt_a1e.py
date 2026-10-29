"""Unit tests for Backlog A1e:
1. `dalton-transcript-polish` skill content, references, and `audit_numeric_units.py`
   (`self-test` + `"78 billion"` magnitude check).
2. Ingest receipt ID extraction (`_render_ingest_receipt`, `ingest_research`, and
   bridge `handle_sales_notes_ingest`) for realistic `sales-notes` and `company-wiki`
   responses, plus `"编号未返回"` fallback without internal field names.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-transcript-polish"
AUDIT_SCRIPT = SKILL_DIR / "scripts" / "audit_numeric_units.py"
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
from src.dalton.tools import ingest_tools  # noqa: E402


def _load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_numeric_units", AUDIT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_numeric_units"] = module
    spec.loader.exec_module(module)
    return module


class TestAuditNumericUnits:
    def test_self_test_passes_in_process_and_cli(self) -> None:
        mod = _load_audit_module()
        ok, checks = mod.self_test()
        assert ok is True
        assert all(item["ok"] for item in checks)

        proc = subprocess.run(
            [sys.executable, str(AUDIT_SCRIPT), "self-test", "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        assert payload["ok"] is True
        assert all(item["ok"] for item in payload["checks"])

    def test_78_billion_sample_catches_10x_magnitude_error_and_accepts_780_yi(
        self, tmp_path: Path
    ) -> None:
        mod = _load_audit_module()
        source_text = (
            "[S1-Speaker 1] Our ARR reached 78 billion US dollars this quarter, "
            "and capex guidance is 12 to 14 billion USD.\n"
        )
        good_target = (
            "本季度我们 ARR 达到 780 亿美元，资本开支指引为 120 亿至 140 亿美元。\n"
        )
        bad_target = (
            "本季度我们 ARR 达到 78 亿美元，资本开支指引为 120 亿至 140 亿美元。\n"
        )

        good_res = mod.audit(source_text, good_target)
        assert good_res.warnings == []

        bad_res = mod.audit(source_text, bad_target)
        assert len(bad_res.warnings) == 1
        warning = bad_res.warnings[0]
        assert warning.kind == "magnitude"
        assert warning.currency == "USD"
        assert "×10" in warning.hint
        assert "billion" in warning.hint

        # Also verify CLI invocation with --fail-on-warn on files
        src_file = tmp_path / "transcript.txt"
        bad_file = tmp_path / "polished_bad.md"
        good_file = tmp_path / "polished_good.md"
        src_file.write_text(source_text, encoding="utf-8")
        bad_file.write_text(bad_target, encoding="utf-8")
        good_file.write_text(good_target, encoding="utf-8")

        bad_proc = subprocess.run(
            [
                sys.executable,
                str(AUDIT_SCRIPT),
                str(src_file),
                str(bad_file),
                "--json",
                "--fail-on-warn",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad_proc.returncode == 1
        bad_json = json.loads(bad_proc.stdout)
        assert bad_json["warning_count"] == 1
        assert bad_json["warnings"][0]["kind"] == "magnitude"

        good_proc = subprocess.run(
            [
                sys.executable,
                str(AUDIT_SCRIPT),
                str(src_file),
                str(good_file),
                "--json",
                "--fail-on-warn",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert good_proc.returncode == 0
        good_json = json.loads(good_proc.stdout)
        assert good_json["warning_count"] == 0


class TestTranscriptPolishSkillPort:
    def test_skill_and_references_structure_and_dalton_conventions(self) -> None:
        skill_md = SKILL_DIR / "SKILL.md"
        assert skill_md.is_file()
        content = skill_md.read_text(encoding="utf-8")

        # Required references exist; colab-asr.md must NOT be ported
        refs_dir = SKILL_DIR / "references"
        for ref_name in (
            "transcript-editing.md",
            "numeric-integrity.md",
            "verification-protocol.md",
            "pipeline-lessons.md",
        ):
            assert (refs_dir / ref_name).is_file(), f"Missing reference {ref_name}"
        assert not (refs_dir / "colab-asr.md").exists()

        # Key Dalton conventions in SKILL.md
        assert ".agents/skills/dalton-transcript-polish/scripts/audit_numeric_units.py" in content
        assert "feishu_create_doc" in content
        assert "feishu_append" in content
        assert "ingest_research" in content
        assert "sell_side" in content
        assert "expert_or_company" in content
        assert "get_workspace_auto_ingest" in content
        assert "英文校订稿" in content
        assert "S1-Speaker 1" in content

        # Forbidden OpenClaw leftovers across SKILL.md and all references
        forbidden = (
            "colab-asr.md",
            "run_asr.sh",
            "seed_asr.py",
            "build_colab_notebook.py",
            "feishu_grant_perm.sh",
            "company-wiki/run.sh",
            "market-digest/run.sh",
            "openclaw-weixin",
            "2026-08-22",
        )
        for md_path in [skill_md, *refs_dir.glob("*.md")]:
            text = md_path.read_text(encoding="utf-8")
            for bad in forbidden:
                assert bad not in text, f"Forbidden OpenClaw leftover {bad!r} in {md_path.name}"


class TestIngestReceiptIdentifiers:
    def test_sales_notes_realistic_bridge_response_extracts_id_from_cli_result(self) -> None:
        # Realistic bridge response where the CLI note identifier is nested in cli_result
        sales_notes_resp: dict[str, Any] = {
            "ok": True,
            "status": "ingested",
            "dry_run": False,
            "request_id": "req-sn-001",
            "duplicate": False,
            "institution": "Morgan Stanley",
            "title": "ORCL｜3Q26 卖方电话会纪要",
            "kind": "卖方电话会",
            "sha256": "a" * 64,
            "cli_result": {
                "status": "ok",
                "doc_key": "8f3a1c2b9e0d",
                "note_path": "state/sales-notes/notes/2026/10/2026-10-29_8f3a1c2b9e0d.md",
                "chunks": 5,
            },
            "audit_path": "var/dalton/sales_notes_ingest_audit.jsonl",
        }
        receipt = json.loads(
            ingest_tools._render_ingest_receipt(
                "sales-notes", sales_notes_resp, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt["ok"] is True
        assert receipt["auto_ingest"] is True
        assert receipt["document_id"] == "manual:8f3a1c2b9e0d"
        assert "编号 manual:8f3a1c2b9e0d" in receipt["receipt"]
        assert "ORCL｜3Q26 卖方电话会纪要" in receipt["receipt"]
        assert "cli_result" not in receipt["receipt"]
        assert "audit_path" not in receipt["receipt"]

    def test_sales_notes_cli_result_with_note_id_is_extracted(self) -> None:
        sales_notes_resp: dict[str, Any] = {
            "ok": True,
            "status": "ingested",
            "dry_run": False,
            "duplicate": False,
            "institution": "BofA Securities",
            "title": "AI Supply Chain Call",
            "kind": "卖方电话会",
            "cli_result": {
                "status": "ok",
                "note_id": "manual:bofa-20261029-01",
                "chunks": 3,
            },
            "audit_path": "var/dalton/sales_notes_ingest_audit.jsonl",
        }
        receipt = json.loads(
            ingest_tools._render_ingest_receipt(
                "sales-notes", sales_notes_resp, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt["document_id"] == "manual:bofa-20261029-01"
        assert "编号 manual:bofa-20261029-01" in receipt["receipt"]
        assert "cli_result" not in receipt["receipt"]

    def test_company_wiki_realistic_bridge_response_extracts_wiki_document_id(self) -> None:
        wiki_resp: dict[str, Any] = {
            "aliases_added": [],
            "aliases_not_added": [],
            "ok": True,
            "status": "ingested",
            "dry_run": False,
            "request_id": "req-wiki-001",
            "doc_id": 1632,
            "document_id": "wiki:1632",
            "target_type": "company",
            "target": "FUTU",
            "content_type": "管理层会议纪要",
            "date": "2026-10-29",
            "filename": "2026-10-29-管理层会议纪要.md",
            "filepath": "wiki/companies/FUTU/2026-10-29-管理层会议纪要.md",
            "size_bytes": 4096,
            "char_count": 1800,
            "sha256": "b" * 64,
            "duplicate": False,
            "audit_path": "var/dalton/wiki_ingest_audit.jsonl",
        }
        receipt = json.loads(
            ingest_tools._render_ingest_receipt(
                "company-wiki", wiki_resp, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt["ok"] is True
        assert receipt["document_id"] == "wiki:1632"
        assert receipt["receipt"] == "已入库 company-wiki；编号 wiki:1632；FUTU"
        assert "cli_result" not in receipt["receipt"]
        assert "audit_path" not in receipt["receipt"]

        # Also when document_id is None/missing and only numeric doc_id is present
        wiki_resp_numeric_only = {**wiki_resp, "document_id": None}
        receipt_num = json.loads(
            ingest_tools._render_ingest_receipt(
                "company-wiki", wiki_resp_numeric_only, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt_num["document_id"] == "wiki:1632"
        assert receipt_num["receipt"] == "已入库 company-wiki；编号 wiki:1632；FUTU"

    def test_missing_identifier_displays_bianhao_wei_fanhui_never_internal_fields(self) -> None:
        resp_without_id: dict[str, Any] = {
            "ok": True,
            "status": "ingested",
            "dry_run": False,
            "duplicate": False,
            "institution": "Goldman Sachs",
            "title": "Semiconductor Call",
            "cli_result": {"status": "ok", "chunks": 2},
            "audit_path": "var/dalton/sales_notes_ingest_audit.jsonl",
        }
        receipt = json.loads(
            ingest_tools._render_ingest_receipt(
                "sales-notes", resp_without_id, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt["document_id"] is None
        assert receipt["receipt"] == "已入库 sales-notes；编号 编号未返回；Semiconductor Call"
        assert "cli_result" not in receipt["receipt"]
        assert "audit_path" not in receipt["receipt"]

    @pytest.mark.asyncio
    async def test_bridge_sales_notes_ingest_multiline_cli_json_and_note_id_extraction(
        self, tmp_path: Path
    ) -> None:
        bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
        bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
        bridge.cfg.wiki_ingest_timeout_s = 10.0
        bridge.cfg.wiki_ingest_temp_dir = tmp_path / "tmp"
        bridge.cfg.env_path = tmp_path / ".nonexistent.env"
        bridge.cfg.ocw_root = tmp_path / "ocw"
        bridge.cfg.subprocess_proxy = ""

        multiline_cli_stdout = json.dumps(
            {
                "ok": True,
                "status": "ok",
                "note_id": "9a8b7c6d5e4f",
                "note_path": "state/sales-notes/notes/2026/10/2026-10-29_9a8b7c6d5e4f.md",
                "chunks": 4,
            },
            ensure_ascii=False,
            indent=2,
        )
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.communicate.return_value = (multiline_cli_stdout.encode("utf-8"), b"")

        async def fake_exec(*cmd: Any, **kwargs: Any) -> Any:
            return proc

        with (
            patch.object(
                rb.ResearchDataBridge,
                "_resolve_market_digest_run_sh",
                new=create_autospec(
                    rb.ResearchDataBridge._resolve_market_digest_run_sh,
                    return_value=Path("/nonexistent/run.sh"),
                ),
            ),
            patch.object(
                rb.asyncio,
                "create_subprocess_exec",
                new=create_autospec(rb.asyncio.create_subprocess_exec, side_effect=fake_exec),
            ),
        ):
            code, payload = await bridge.handle_sales_notes_ingest(
                {
                    "institution": "Morgan Stanley",
                    "title": "ORCL 3Q26 Call",
                    "kind": "卖方电话会",
                    "content": "卖方电话会整理正文",
                    "ingested_by": {"user_id": "u-1"},
                    "dry_run": False,
                    "request_id": "req-multiline-1",
                }
            )

        assert code == 200
        assert payload["ok"] is True
        assert payload["doc_key"] == "9a8b7c6d5e4f"
        assert payload["document_id"] == "manual:9a8b7c6d5e4f"

        # Verify end-to-end receipt rendering with the bridge response
        receipt = json.loads(
            ingest_tools._render_ingest_receipt(
                "sales-notes", payload, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt["document_id"] == "manual:9a8b7c6d5e4f"
        assert "编号 manual:9a8b7c6d5e4f" in receipt["receipt"]
        assert "cli_result" not in receipt["receipt"]

    @pytest.mark.asyncio
    async def test_bridge_sales_notes_ingest_reads_the_real_add_note_output(
        self, tmp_path: Path
    ) -> None:
        bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
        bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
        bridge.cfg.wiki_ingest_timeout_s = 10.0
        bridge.cfg.wiki_ingest_temp_dir = tmp_path / "tmp"
        bridge.cfg.env_path = tmp_path / ".nonexistent.env"
        bridge.cfg.ocw_root = tmp_path / "ocw"
        bridge.cfg.subprocess_proxy = ""

        multiline_cli_stdout = json.dumps(
            {
                # What `market-digest/run.sh --json add-note` prints on success
                "ok": True,
                "status": "added",
                "document_id": "mail:48213",
                "doc_key": "manual-3f9c2a7be41d90c8",
                "sender": "Morgan Stanley",
                "subject": "ORCL 3Q26 Call",
                "date": "2026-10-29",
                "doc_date": None,
                "kind": "卖方电话会",
                "tickers": ["ORCL"],
                "chunks": 4,
                "chars": 5210,
                "image_only": False,
                "path": "notes/2026-10-29-morgan-stanley-orcl-3q26-call-be41d90c8.md",
                "original": None,
                "embedding_model": "text-embedding-3-large",
                "image_ingestion": None,
                "read": "researchdata.research(route=\"sales-notes\", operation=\"read\", document=\"mail:48213\")",
            },
            ensure_ascii=False,
            indent=2,
        )
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.communicate.return_value = (multiline_cli_stdout.encode("utf-8"), b"")

        async def fake_exec(*cmd: Any, **kwargs: Any) -> Any:
            return proc

        with (
            patch.object(
                rb.ResearchDataBridge,
                "_resolve_market_digest_run_sh",
                new=create_autospec(
                    rb.ResearchDataBridge._resolve_market_digest_run_sh,
                    return_value=Path("/nonexistent/run.sh"),
                ),
            ),
            patch.object(
                rb.asyncio,
                "create_subprocess_exec",
                new=create_autospec(rb.asyncio.create_subprocess_exec, side_effect=fake_exec),
            ),
        ):
            code, payload = await bridge.handle_sales_notes_ingest(
                {
                    "institution": "Morgan Stanley",
                    "title": "ORCL 3Q26 Call",
                    "kind": "卖方电话会",
                    "content": "卖方电话会整理正文",
                    "ingested_by": {"user_id": "u-1"},
                    "dry_run": False,
                    "request_id": "req-real-shape-1",
                }
            )

        assert code == 200
        assert payload["ok"] is True
        assert payload["document_id"] == "mail:48213"

        # Verify end-to-end receipt rendering with the bridge response
        receipt = json.loads(
            ingest_tools._render_ingest_receipt(
                "sales-notes", payload, auto_ingest=True, effective_confirm=True
            )
        )
        assert receipt["document_id"] == "mail:48213"
        assert "编号 mail:48213" in receipt["receipt"]
        assert "cli_result" not in receipt["receipt"]
