"""Unit tests for `plugins/dalton_data/dalton_sec_mcp_server.py` (Rules R1–R10).

Uses compact real SEC EDGAR companyfacts fixtures under
`tests/unit/mcp_servers/fixtures/dalton_sec/`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from plugins.dalton_data import dalton_sec_mcp_server as ds
from src.dalton.provenance.declared_sources import expand_declared_sources

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "dalton_sec"


@pytest.fixture(scope="module")
def meta_facts() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "meta_facts.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def msft_facts() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "msft_facts.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def amzn_facts() -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / "amzn_facts.json").read_text(encoding="utf-8"))


class TestDurationBucketingAndPeriods:
    """R1: 1q (75-105d) vs 2q (165-195d) vs 3q (255-285d) vs 4q (350-380d)."""

    def test_meta_2025q2_never_returns_h1_ytd(self, meta_facts: dict[str, Any]) -> None:
        q2 = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["Revenues", "PaymentsToAcquirePropertyPlantAndEquipment"],
            period="2025Q2",
        )
        h1 = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["Revenues", "PaymentsToAcquirePropertyPlantAndEquipment"],
            period="H1 2025",
        )

        q2_rev = q2["concepts"]["Revenues"]
        h1_rev = h1["concepts"]["Revenues"]
        assert q2_rev["status"] == "ok"
        assert h1_rev["status"] == "ok"

        # 2025Q2 single-quarter revenue ($47,516M, 90 days) != H1 2025 6M revenue ($89,830M, 180 days)
        assert q2_rev["val"] == 47_516_000_000
        assert 75 <= q2_rev["duration_days"] <= 105
        assert q2_rev["calendar_period"] == "2025Q2"
        assert q2_rev["fiscal_period"] == "FY2025Q2"
        assert q2_rev["derived"] is False

        assert h1_rev["val"] == 89_830_000_000
        assert 165 <= h1_rev["duration_days"] <= 195
        assert h1_rev["calendar_period"] == "H1 2025"
        assert h1_rev["fiscal_period"] == "FY2025H1"
        assert q2_rev["val"] != h1_rev["val"]

        # CapEx: H1 2025 is direct 6M ($29,479M); 2025Q2 is derived H1 - Q1 ($16,538M), never 6M YTD
        q2_capex = q2["concepts"]["PaymentsToAcquirePropertyPlantAndEquipment"]
        h1_capex = h1["concepts"]["PaymentsToAcquirePropertyPlantAndEquipment"]
        assert h1_capex["val"] == 29_479_000_000
        assert 165 <= h1_capex["duration_days"] <= 195
        assert h1_capex["derived"] is False

        assert q2_capex["val"] == 16_538_000_000
        assert 75 <= q2_capex["duration_days"] <= 105
        assert q2_capex["derived"] is True
        assert q2_capex["derivation_method"] == "H1_minus_Q1"
        assert q2_capex["val"] != h1_capex["val"]


class TestFiscalVsCalendarAlignment:
    """R3: Non-December FYE alignment (MSFT FYE month = 6)."""

    def test_msft_fy2026q1_matches_calendar_2025q3(self, msft_facts: dict[str, Any]) -> None:
        by_fy = ds.normalize_company_facts(
            msft_facts,
            ticker="MSFT",
            concepts=["Revenues", "PaymentsToAcquirePropertyPlantAndEquipment"],
            period="FY2026Q1",
        )
        by_cal = ds.normalize_company_facts(
            msft_facts,
            ticker="MSFT",
            concepts=["Revenues", "PaymentsToAcquirePropertyPlantAndEquipment"],
            period="2025Q3",
        )

        assert by_fy["fiscal_year_end_month"] == 6
        rev_fy = by_fy["concepts"]["Revenues"]
        rev_cal = by_cal["concepts"]["Revenues"]

        assert rev_fy["status"] == "ok"
        assert rev_fy["val"] == 77_673_000_000
        assert rev_fy["calendar_period"] == "2025Q3"
        assert rev_fy["fiscal_period"] == "FY2026Q1"
        assert rev_fy["accession_no"] == "0001193125-25-256321"
        assert rev_cal["val"] == rev_fy["val"]
        assert rev_cal["fiscal_period"] == "FY2026Q1"

        capex = by_fy["concepts"]["PaymentsToAcquirePropertyPlantAndEquipment"]
        assert capex["val"] == 19_394_000_000
        assert capex["calendar_period"] == "2025Q3"
        assert capex["fiscal_period"] == "FY2026Q1"


class TestDerivedQ4:
    """R4: Standalone Q4 derived via FY - 9M when 10-K only reports full-year flow."""

    def test_amzn_2025q4_derived_from_fy_minus_9m(self, amzn_facts: dict[str, Any]) -> None:
        res = ds.normalize_company_facts(
            amzn_facts,
            ticker="AMZN",
            concepts=["Revenues", "PaymentsToAcquirePropertyPlantAndEquipment"],
            period="2025Q4",
        )
        rev = res["concepts"]["Revenues"]
        assert rev["status"] == "ok"
        assert rev["derived"] is True
        assert rev["derivation_method"] == "FY_minus_9M"
        assert rev["val"] == 213_386_000_000
        assert rev["calendar_period"] == "2025Q4"
        assert rev["fiscal_period"] == "FY2025Q4"
        assert 75 <= rev["duration_days"] <= 105
        assert len(rev["components"]) == 2
        assert rev["components"][0]["period"] == "FY2025"
        assert rev["components"][1]["period"] == "FY20259M"
        assert rev["components"][0]["val"] - rev["components"][1]["val"] == rev["val"]

        # Ensure AMZN TTM 10-Q cash flow rows are not mistaken for FY in CapEx Q4 derivation
        capex = res["concepts"]["PaymentsToAcquirePropertyPlantAndEquipment"]
        assert capex["status"] == "ok"
        assert capex["derived"] is True
        assert capex["derivation_method"] == "FY_minus_9M"
        assert capex["val"] == 39_522_000_000
        assert 75 <= capex["duration_days"] <= 105


class TestRestatementAndPointInTime:
    """R5 & R6: `as_reported` vs `latest` restatement mode and `as_of` cutoff."""

    def test_as_reported_vs_latest_restatement(self, meta_facts: dict[str, Any]) -> None:
        patched = copy.deepcopy(meta_facts)
        rows = patched["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"]["USD"]
        # Inject a later restatement for 2025Q2 (end=2025-06-30, start=2025-04-01)
        rows.append(
            {
                "start": "2025-04-01",
                "end": "2025-06-30",
                "val": 47_999_000_000,
                "accn": "0001628280-26-999999",
                "fy": 2026,
                "fp": "Q2",
                "form": "10-Q",
                "filed": "2026-07-31",
                "frame": "CY2025Q2",
            }
        )

        as_rep = ds.normalize_company_facts(
            patched,
            ticker="META",
            concepts=["Revenues"],
            period="2025Q2",
            restatement="as_reported",
        )["concepts"]["Revenues"]
        latest = ds.normalize_company_facts(
            patched,
            ticker="META",
            concepts=["Revenues"],
            period="2025Q2",
            restatement="latest",
        )["concepts"]["Revenues"]

        assert as_rep["val"] == 47_516_000_000
        assert as_rep["restatement_mode"] == "as_reported"
        assert as_rep["restated_in_later_filing"] is True
        assert as_rep["latest_val"] == 47_999_000_000

        assert latest["val"] == 47_999_000_000
        assert latest["restatement_mode"] == "latest"
        assert latest["restated"] is True
        assert latest["as_reported_val"] == 47_516_000_000

    def test_as_of_cutoff_excludes_future_filings(self, meta_facts: dict[str, Any]) -> None:
        # META 2025Q2 10-Q was filed on 2025-07-31; cutoff before that date must exclude it
        before = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["Revenues"],
            period="2025Q2",
            as_of="2025-07-15",
        )["concepts"]["Revenues"]
        assert before["status"] == "empty_period"

        after = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["Revenues"],
            period="2025Q2",
            as_of="2025-08-01",
        )["concepts"]["Revenues"]
        assert after["status"] == "ok"
        assert after["val"] == 47_516_000_000


class TestTrailingAndConceptErrors:
    """R2 & trailing_q: Multi-quarter series and explicit not_found with fuzzy suggestions."""

    def test_trailing_8q_returns_chronological_quarters(self, meta_facts: dict[str, Any]) -> None:
        res = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["Revenues"],
            period="trailing:8q",
        )
        rev = res["concepts"]["Revenues"]
        assert rev["status"] == "ok"
        facts = rev["facts"]
        assert len(facts) == 8
        for item in facts:
            assert 75 <= item["duration_days"] <= 105
        # Verify strictly increasing period order
        periods = [(f["cal_year"], f["cal_q"]) for f in facts]
        assert periods == sorted(periods)
        assert len(set(periods)) == 8

    def test_nonexistent_concept_returns_suggestions(self, meta_facts: dict[str, Any]) -> None:
        res = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["NonExistentRevenueConceptXYZ"],
            period="2025Q2",
        )
        info = res["concepts"]["NonExistentRevenueConceptXYZ"]
        assert info["status"] == "not_found"
        assert isinstance(info["suggestions"], list)
        assert len(info["suggestions"]) >= 1

    def test_declared_sources_compatible_with_provenance_middleware(
        self, meta_facts: dict[str, Any]
    ) -> None:
        res = ds.normalize_company_facts(
            meta_facts,
            ticker="META",
            concepts=["Revenues"],
            period="2025Q2",
        )
        envelope = {
            "_declared_sources": res["_declared_sources"],
            "_sources": res["_sources"],
            "data": [res],
        }
        expanded = expand_declared_sources(
            "dalton_sec", "get_company_facts", json.dumps(envelope), "tc-sec-1"
        )
        assert len(expanded) == 1
        assert expanded[0].source_type == "sec_filing"
        assert expanded[0].detail == "filing"
        assert expanded[0].provider == "dalton_sec:SEC EDGAR"
        assert "0001628280-25-036791" in expanded[0].identifier
