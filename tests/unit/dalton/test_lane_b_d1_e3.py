"""Lane-B batch tests: FMP quota surfacing on sector performance (B2), the
sales-notes ingest endpoint's validation (D1), the ingest router's receipt
and auto-ingest flag (D1), and `report_issue` (E3)."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

_MKT_MOD = "src.tools.market_data.market_overview"


# ---------------------------------------------------------------------- B2

class TestSectorPerformanceQuota:
    @pytest.mark.asyncio
    async def test_dated_call_reports_quota_exhausted_explicitly(self):
        """A dated request must name the exhausted quota, not return an empty table."""
        from src.data_client import FinancialDataProvider
        from src.data_client.base import FinancialDataSource
        from src.data_client.fmp.fmp_client import FmpQuotaExhausted
        from src.tools.market_data import market_overview
        from src.tools.market_data.market_overview import fetch_sector_performance

        financial = create_autospec(FinancialDataSource, instance=True)
        financial.get_sector_performance.side_effect = FmpQuotaExhausted(
            datetime(2026, 10, 28, 3, 0)
        )
        provider = create_autospec(FinancialDataProvider, instance=True)
        provider.financial = financial
        with patch(
            f"{_MKT_MOD}.get_financial_data_provider",
            new=create_autospec(
                market_overview.get_financial_data_provider, return_value=provider
            ),
        ):
            content, artifact = await fetch_sector_performance(date="2026-10-27")

        assert "额度已用完" in content
        assert artifact.get("error") == "fmp_quota_exhausted"
        assert artifact["sectors"] == []

    @pytest.mark.asyncio
    async def test_dateless_call_still_falls_back_to_yfinance_snapshot(self):
        from src.data_client import FinancialDataProvider
        from src.data_client.base import FinancialDataSource
        from src.data_client.fmp.fmp_client import FmpQuotaExhausted
        from src.tools.market_data import market_overview
        from src.tools.market_data.market_overview import fetch_sector_performance

        financial = create_autospec(FinancialDataSource, instance=True)
        financial.get_sector_performance.side_effect = FmpQuotaExhausted(
            datetime(2026, 10, 28, 3, 0)
        )
        provider = create_autospec(FinancialDataProvider, instance=True)
        provider.financial = financial
        snapshot_rows = [{"sector": "Technology", "changePctStr": "+1.50%"}]
        with (
            patch(
                f"{_MKT_MOD}.get_financial_data_provider",
                new=create_autospec(
                    market_overview.get_financial_data_provider, return_value=provider
                ),
            ),
            patch(
                f"{_MKT_MOD}._yfinance_sector_snapshot",
                new=create_autospec(
                    market_overview._yfinance_sector_snapshot,
                    return_value=snapshot_rows,
                ),
            ),
        ):
            content, artifact = await fetch_sector_performance()

        assert "Technology" in content
        assert artifact["sectors"][0]["sector"] == "Technology"


# ---------------------------------------------------------------------- D1

def _sn_bridge() -> rb.ResearchDataBridge:
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
    bridge.cfg.wiki_ingest_timeout_s = 10.0
    bridge.cfg.wiki_ingest_temp_dir = Path("/tmp/unused-sn")
    bridge.cfg.env_path = Path("/nonexistent-bridge.env")
    bridge.cfg.ocw_root = Path("/tmp/unused-ocw")
    bridge.cfg.subprocess_proxy = ""
    return bridge


@pytest.mark.asyncio
async def test_sales_notes_ingest_requires_institution_and_title():
    bridge = _sn_bridge()
    code, payload = await bridge.handle_sales_notes_ingest(
        {"title": "T", "content": "x", "ingested_by": {"user_id": "u1"}}
    )
    assert code == 400 and payload["error_code"] == "missing_institution"
    code, payload = await bridge.handle_sales_notes_ingest(
        {"institution": "MS", "content": "x", "ingested_by": {"user_id": "u1"}}
    )
    assert code == 400 and payload["error_code"] == "missing_title"


@pytest.mark.asyncio
async def test_sales_notes_ingest_validates_kind_and_content_xor_file():
    bridge = _sn_bridge()
    code, payload = await bridge.handle_sales_notes_ingest(
        {
            "institution": "MS",
            "title": "T",
            "kind": "随便",
            "content": "x",
            "ingested_by": {"user_id": "u1"},
        }
    )
    assert code == 400 and payload["error_code"] == "invalid_kind"

    code, payload = await bridge.handle_sales_notes_ingest(
        {"institution": "MS", "title": "T", "ingested_by": {"user_id": "u1"}}
    )
    assert code == 400 and payload["error_code"] == "content_or_file_required"

    code, payload = await bridge.handle_sales_notes_ingest(
        {
            "institution": "MS",
            "title": "T",
            "content": "x",
            "file": {"filename": "a.md", "content_b64": "eA=="},
            "ingested_by": {"user_id": "u1"},
        }
    )
    assert code == 400 and payload["error_code"] == "content_or_file_required"


@pytest.mark.asyncio
async def test_sales_notes_ingest_rejects_secret_payload():
    bridge = _sn_bridge()
    code, payload = await bridge.handle_sales_notes_ingest(
        {
            "institution": "MS",
            "title": "T",
            "content": "sk-proj-0123456789abcdef0123 notes here",
            "ingested_by": {"user_id": "u1"},
        }
    )
    assert code == 400 and payload["error_code"] == "secret_detected"


@pytest.mark.asyncio
async def test_sales_notes_ingest_dry_run_returns_preview():
    bridge = _sn_bridge()
    captured: dict = {}
    proc = create_autospec(asyncio.subprocess.Process, instance=True)
    proc.returncode = 0
    proc.communicate.return_value = (
        b'{"status": "ok", "doc_key": "abc123", "chunks": 3}\n',
        b"",
    )

    async def fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
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
                "institution": "MS",
                "title": "T",
                "content": "正文",
                "ingested_by": {"user_id": "u1"},
                "dry_run": True,
            }
        )
    assert code == 200 and payload["status"] == "dry_run"
    assert payload["dry_run"] is True
    # The argv rides the add-note subcommand with --dry-run, institution and
    # a temp-file --content, never the text on the command line.
    cmd = captured["cmd"]
    assert cmd[1:4] == ["--json", "add-note", "--institution"]
    assert "--dry-run" in cmd
    assert any(a == "--content" and b.startswith("@") for a, b in zip(cmd, cmd[1:]))


class TestIngestRouter:
    @pytest.mark.asyncio
    async def test_missing_speaker_asks_the_user_instead_of_guessing(self):
        from src.dalton import access_tiers
        from src.dalton.tools.ingest_tools import build_ingest_research_tool

        tool = build_ingest_research_tool(filesystem_backend=None)
        with patch(
            "src.dalton.tools.ingest_tools.get_user_tier",
            new=create_autospec(access_tiers.get_user_tier, return_value="internal"),
        ):
            result = json.loads(
                await tool.ainvoke({"content": "正文"}, config=_run_config())
            )
        assert result["status"] == "speaker_unknown"
        assert "谁在说" in result["question"]

    @pytest.mark.asyncio
    async def test_receipt_for_auto_ingest_write(self):
        from src.dalton.tools.ingest_tools import _render_ingest_receipt

        receipt = json.loads(
            _render_ingest_receipt(
                "sales-notes",
                {"ok": True, "status": "ingested", "document_id": "manual:abc", "title": "ORCL 3Q26"},
                auto_ingest=True,
                effective_confirm=True,
            )
        )
        assert receipt["auto_ingest"] is True
        assert "sales-notes" in receipt["receipt"]
        assert "manual:abc" in receipt["receipt"]
        assert "ORCL 3Q26" in receipt["receipt"]

    @pytest.mark.asyncio
    async def test_auto_ingest_flag_reads_workspaces_config(self):
        from src.dalton.tools.ingest_tools import get_workspace_auto_ingest
        from src.server.database import workspace as ws_db

        async def fake_get_workspace(ws_id):
            return {"config": {"dalton_auto_ingest": True}} if ws_id == "on" else {"config": {}}

        with patch(
            "src.server.database.workspace.get_workspace",
            new=create_autospec(ws_db.get_workspace, side_effect=fake_get_workspace),
        ):
            assert await get_workspace_auto_ingest("on") is True
            assert await get_workspace_auto_ingest("off") is False


def _run_config(**overrides):
    configurable = {
        "user_id": "u-1",
        "workspace_id": "ws-1",
        "thread_id": "th-1",
        "run_id": "r-1",
    }
    configurable.update(overrides)
    return {"configurable": configurable, "metadata": {}, "tags": [], "callbacks": None}


# ---------------------------------------------------------------------- E3

class TestReportIssue:
    @pytest.mark.asyncio
    async def test_files_the_report_and_returns_a_trackable_ref(self):
        from src.dalton.database import issues as issues_db
        from src.dalton.tools.issue_tools import create_issue_tools

        tool = create_issue_tools()[0]
        with patch(
            "src.dalton.tools.issue_tools.create_report_issue",
            new=create_autospec(
                issues_db.create_report_issue,
                return_value={"ref": "DALTON-20261027-a1b2c3", "issue_id": 7},
            ),
        ):
            payload = json.loads(
                await tool.ainvoke(
                    {"title": "图表标注丢失", "body": "发布后 chart 标注不见了", "severity": "normal"},
                    config=_run_config(),
                )
            )
        assert payload["ok"] is True
        assert payload["ref"] == "DALTON-20261027-a1b2c3"
        assert payload["issue_id"] == 7

    @pytest.mark.asyncio
    async def test_requires_an_authenticated_user(self):
        from src.dalton.tools.issue_tools import create_issue_tools

        tool = create_issue_tools()[0]
        payload = json.loads(
            await tool.ainvoke(
                {"title": "t", "body": "b"}, config=_run_config(user_id="")
            )
        )
        assert payload["ok"] is False

    @pytest.mark.asyncio
    async def test_empty_title_or_body_is_refused(self):
        from src.dalton.tools.issue_tools import create_issue_tools

        tool = create_issue_tools()[0]
        payload = json.loads(
            await tool.ainvoke({"title": "  ", "body": "b"}, config=_run_config())
        )
        assert payload["ok"] is False
