"""Unit tests for WP-F: Dalton Financial Lenses, Analyst Skills, Evidence Agent, Workflows, and Templates."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DALTON_PLUGIN_DIR = REPO_ROOT / "plugins" / "dalton"
LENSES_DIR = DALTON_PLUGIN_DIR / "skills" / "dalton-financial-lenses"
SCRIPTS_DIR = LENSES_DIR / "scripts"

WPF_SKILLS = [
    "dalton-financial-lenses",
    "dalton-company-background",
    "dalton-bullbear",
    "dalton-initial-coverage",
    "dalton-full-coverage",
    "dalton-earnings-calendar",
    "dalton-earnings-preview",
    "dalton-earnings-flash",
    "dalton-earnings-deep",
    "dalton-stock-move",
    "dalton-thesis-delta",
    "dalton-filing-review",
    "dalton-weekly-review",
    "dalton-topic-research",
    "dalton-call",
    "dalton-pm-qa",
    "dalton-agenda",
    "dalton-research-director",
]

WPF_WORKFLOWS = [
    "bullbear-doc",
    "initial-coverage",
    "full-coverage",
    "earnings-deep-update",
    "weekly-coverage-review",
    "topic-research",
]


def _load_script_module(script_path: Path, mod_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(mod_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


anchors_mod = _load_script_module(SCRIPTS_DIR / "anchors.py", "dalton_lenses_anchors")
return_score_mod = _load_script_module(SCRIPTS_DIR / "return_score.py", "dalton_lenses_return_score")
netcash_mod = _load_script_module(SCRIPTS_DIR / "netcash.py", "dalton_lenses_netcash")
earnings_quality_mod = _load_script_module(
    SCRIPTS_DIR / "earnings_quality.py", "dalton_lenses_earnings_quality"
)


def _parse_frontmatter_and_body(md_path: Path) -> tuple[dict[str, Any], list[str]]:
    text = md_path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{md_path} must start with YAML frontmatter"
    parts = text.split("---\n", 2)
    assert len(parts) == 3, f"{md_path} must have closing frontmatter delimiter"
    meta = yaml.safe_load(parts[1])
    body_lines = parts[2].splitlines()
    return meta, body_lines


class TestSkillAndAgentConventions:
    """Verify frontmatter, conventions line, and required sections across all 18 WP-F skills and agent."""

    @pytest.mark.parametrize("skill_name", WPF_SKILLS)
    def test_skill_frontmatter_and_conventions_line(self, skill_name: str) -> None:
        skill_path = DALTON_PLUGIN_DIR / "skills" / skill_name / "SKILL.md"
        assert skill_path.is_file(), f"Missing skill file: {skill_path}"
        meta, body_lines = _parse_frontmatter_and_body(skill_path)
        assert set(meta.keys()) == {"name", "description"}, (
            f"{skill_name} frontmatter must ONLY contain 'name' and 'description', got {set(meta.keys())}"
        )
        assert meta["name"] == skill_name
        assert isinstance(meta["description"], str) and len(meta["description"].strip()) >= 20

        first_12_lines = "\n".join(body_lines[:12])
        assert (
            "Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable."
            in first_12_lines
        ), f"{skill_name} must include the exact Fund methodology line within the first 12 lines of body"

    def test_dalton_evidence_agent_definition(self) -> None:
        agent_path = DALTON_PLUGIN_DIR / "agents" / "dalton-evidence.md"
        assert agent_path.is_file()
        assert agent_path.stem == "dalton-evidence"
        meta, body_lines = _parse_frontmatter_and_body(agent_path)
        assert "description" in meta and meta["description"]
        assert "tools" in meta and meta["tools"]
        assert "preferred_connectors" in meta
        assert "dalton-conventions" in meta.get("skills", [])
        first_12_lines = "\n".join(body_lines[:12])
        assert (
            "Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable."
            in first_12_lines
        )

    def test_dalton_agenda_five_verdicts_and_ops(self) -> None:
        text = (DALTON_PLUGIN_DIR / "skills" / "dalton-agenda" / "SKILL.md").read_text(encoding="utf-8")
        for verdict in (
            "NO_CHANGE",
            "THESIS_STRENGTHENED",
            "THESIS_WEAKENED",
            "THESIS_BROKEN",
            "NEW_THESIS",
        ):
            assert verdict in text
        assert "knowledge_append_verdict" in text
        assert "agenda_ops" in text
        assert "next_validation" in text
        assert "progress" in text
        assert ".agents/user/automations/dalton-" in text

    def test_dalton_research_director_scoring_and_routing(self) -> None:
        text = (DALTON_PLUGIN_DIR / "skills" / "dalton-research-director" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        assert "dalton_coverage_board" in text
        assert "delegate_to_analyst" in text
        assert "dalton_record_plan" in text
        assert "dalton_schedule_automation" in text
        assert "dalton_agenda_note" in text
        assert "render_coverage.py" in text
        assert "days_since_last_verdict" in text

    def test_company_background_nine_sections(self) -> None:
        text = (DALTON_PLUGIN_DIR / "skills" / "dalton-company-background" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        tmpl = (
            DALTON_PLUGIN_DIR / "skills" / "dalton-company-background" / "templates" / "background.md"
        ).read_text(encoding="utf-8")
        for heading in (
            "一句话定位",
            "核心业务构成",
            "年度营收与盈利趋势",
            "最近 4 个报告季度",
            "资产负债表关键数据",
            "市场数据",
            "商业模式与竞争",
            "核心看点",
            "核心风险",
        ):
            assert heading in text
            assert heading in tmpl


class TestAnchors:
    """Verify anchors.py fixes HK+CNY reporting FX bug, price_usd conversion, and strict FX/currency rules."""

    def test_hk_stock_with_cny_reporting_currency_fx_fix(self) -> None:
        res = anchors_mod.build_anchors(
            "700.HK",
            "2025-12-31",
            price_local=400.0,
            shares=9280.0,
            listing_currency="HKD",
            financial_currency="CNY",
            fx_rate=0.1380,
            listing_fx_rate=0.1282,
            as_of="2026-04-20",
        )
        assert res["ticker"] == "0700.HK"
        assert res["market"] == "HK"
        assert res["listing_currency"] == "HKD"
        assert res["financial_currency"] == "CNY"
        assert res["fx"]["pair"] == "CNYUSD=X"
        assert res["fx"]["from_currency"] == "CNY"
        assert res["fx"]["rate"] == pytest.approx(0.1380)
        assert res["listing_fx"]["pair"] == "HKDUSD=X"
        assert res["listing_fx"]["from_currency"] == "HKD"
        assert res["listing_fx"]["rate"] == pytest.approx(0.1282)
        assert res["market_cap"]["price_local"] == pytest.approx(400.0)
        assert res["market_cap"]["price_usd"] == pytest.approx(51.28)
        assert res["market_cap"]["market_cap_usd_mn"] == pytest.approx(400.0 * 9280.0 * 0.1282, rel=1e-4)
        assert res["market_cap"]["market_cap_usd"] == pytest.approx(400.0 * 9280.0 * 0.1282, rel=1e-4)

    def test_us_stock_identity_fx(self) -> None:
        res = anchors_mod.build_anchors(
            "AAPL",
            "2025-09-27",
            price_local=220.0,
            shares=15_000_000_000,
            listing_currency="USD",
            financial_currency="USD",
            as_of="2026-04-20",
        )
        assert res["market"] == "US"
        assert res["fx"]["rate"] == 1.0
        assert res["market_cap"]["price_usd"] == 220.0
        assert res["market_cap"]["shares_mn"] == pytest.approx(15000.0)
        assert res["market_cap"]["market_cap_usd_mn"] == pytest.approx(3_300_000.0)

    def test_adr_and_non_us_require_explicit_currency_and_fx_offline(self) -> None:
        with pytest.raises(ValueError, match="reporting-currency"):
            anchors_mod.build_anchors("BABA", "2025-09-30", allow_network=False)

        with pytest.raises(ValueError, match="reporting-currency"):
            anchors_mod.build_anchors("0700.HK", "2025-09-30", allow_network=False)

        with pytest.raises(ValueError, match="requires explicit --fx-rate"):
            anchors_mod.build_anchors(
                "BABA",
                "2025-09-30",
                reporting_currency="CNY",
                allow_network=False,
            )

        res_baba = anchors_mod.build_anchors(
            "BABA",
            "2025-09-30",
            price_local=100.0,
            shares=19_200_000_000,
            adr_ratio=8.0,
            listing_currency="USD",
            reporting_currency="CNY",
            fx_rate=0.1400,
            as_of="2026-10-18",
        )
        assert res_baba["financial_currency"] == "CNY"
        assert res_baba["fx"]["pair"] == "CNYUSD=X"
        assert res_baba["fx"]["rate"] == pytest.approx(0.1400)
        assert res_baba["market_cap"]["shares_mn"] == pytest.approx(2400.0)
        assert res_baba["market_cap"]["market_cap_usd_mn"] == pytest.approx(240_000.0)


class TestReturnScore:
    """Verify 20-point Shareholder Return Score R1/R2/R3 calculation, single final rounding, and rationale formatting."""

    def test_ps2_reference_sample(self) -> None:
        res = return_score_mod.compute_return_score(
            ttm_div=4.73,
            ttm_buyback=6.70,
            true_return_share=0.78,
            annual=[11.24, 11.88, 15.05],
            fwd_total=14.61,
            credibility=90,
            adjustments=[(1.0, "FCFF覆盖率1.8x"), (-1.0, "回购主要对冲SBC")],
        )
        assert res["R1"] == pytest.approx(9.96, abs=0.01)
        assert res["R2"] == pytest.approx(12.72, abs=0.01)
        assert res["R3"] == pytest.approx(13.15, abs=0.01)
        assert res["driver"] == "R3"
        assert res["base_score"] == pytest.approx(13.15, abs=0.01)
        assert res["adjustment"] == pytest.approx(0.0)
        assert res["final_score"] == pytest.approx(13.1)

    def test_single_final_step_rounding_and_decimal_adjustment(self) -> None:
        res = return_score_mod.compute_return_score(
            ttm_div=2.75,
            ttm_buyback=1.0,
            true_return_share=0.75,
            annual=[3.2, 3.8, 4.4],
            fwd_total=4.25,
            credibility=75,
            adjustments=[(0.5, "半年度特别分红"), (-1.0, "存在可转债潜在稀释")],
        )
        assert 0.0 <= res["final_score"] <= 20.0
        assert "+0.5（半年度特别分红）" in res["rationale_zh"]
        assert "-1（存在可转债潜在稀释）" in res["rationale_zh"]
        assert "可持续性调整 -0.5" in res["rationale_zh"]

    def test_cli_json_execution(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "return_score.py"),
                "--ttm-div",
                "4.73",
                "--ttm-buyback",
                "6.70",
                "--true-return-share",
                "0.78",
                "--annual",
                "11.24,11.88,15.05",
                "--fwd-total",
                "14.61",
                "--credibility",
                "90",
                "--adjust",
                "+1:FCFF覆盖率1.8x",
                "--adjust",
                "-1:回购主要对冲SBC",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(proc.stdout)
        assert payload["driver"] == "R3"
        assert payload["R3"] == pytest.approx(13.15, abs=0.01)
        assert payload["final_score"] == pytest.approx(13.1)


class TestNetCashAndImpliedAssets:
    """Verify Basic Net Cash (Def A), Strict Net Cash (Def B), and Implied Operating Assets."""

    def test_basic_and_strict_net_cash_and_implied_operating_assets(self) -> None:
        res = netcash_mod.compute_netcash(
            cash_and_equivalents=500.0,
            restricted_cash=50.0,
            time_deposits=150.0,
            short_term_investments=100.0,
            short_term_debt=80.0,
            current_long_term_debt=20.0,
            long_term_debt=100.0,
            convertible_debt=0.0,
            lease_liabilities=45.0,
            quasi_debt_items=[
                {"item": "customer_advances", "amount": 60.0, "include": True, "reason": "Unearned revenue"},
                {"item": "operating_accruals", "amount": 30.0, "include": False, "reason": "Normal payroll"},
            ],
            non_free_cash_items=[
                {"item": "trapped_cash", "amount": 30.0, "include": True, "reason": "FX controls"},
            ],
            current_assets=1200.0,
            current_liabilities=600.0,
            other_payables=150.0,
            other_receivables=60.0,
            market_cap_usd_mn=1000.0,
            hidden_assets_gross_usd=300.0,
            hidden_assets_after_haircut_usd=210.0,
            fx_rate_to_usd=1.0,
        )
        assert res["gross_cash_usd"] == pytest.approx(800.0)
        assert res["interest_bearing_debt_usd"] == pytest.approx(200.0)
        assert res["lease_liabilities_usd"] == pytest.approx(45.0)
        assert res["basic_net_cash_usd"] == pytest.approx(600.0)
        assert res["basic_net_cash_to_mcap_pct"] == pytest.approx(60.0)
        # Restricted cash (50.0) + trapped_cash (30.0) + customer_advances (60.0) = 140.0 deducted
        assert res["non_free_cash_deductions_usd"] == pytest.approx(80.0)
        assert res["strict_net_cash_usd"] == pytest.approx(460.0)
        assert res["strict_net_cash_to_mcap_pct"] == pytest.approx(46.0)
        assert res["implied_operating_assets"]["basic_usd"] == pytest.approx(190.0)
        assert res["implied_operating_assets"]["strict_usd"] == pytest.approx(330.0)
        assert res["working_capital"]["working_capital_usd"] == pytest.approx(600.0)
        assert any("Other payables / Current liabilities" in w for w in res["warnings"])
        # Verify value_local is preserved on deduction items
        assert res["quasi_debt_items"][0]["value_local"] == pytest.approx(60.0)
        assert res["non_free_cash_items"][0]["value_local"] == pytest.approx(50.0)

    def test_netcash_cli_input_json_and_restricted_cash_override(self, tmp_path: Path) -> None:
        input_file = tmp_path / "netcash_input.json"
        input_file.write_text(
            json.dumps(
                {
                    "cash": 1000.0,
                    "restricted_cash": 200.0,
                    "lt_debt": 300.0,
                    "quasi_debt_items": [
                        {"item": "deferred_revenue", "value_local": 100.0, "include": True, "reason": "Prepaid"}
                    ],
                    "non_free_cash_items": [
                        {"item": "escrow", "value_local": 50.0, "include": True, "reason": "Customer escrow"}
                    ],
                    "market_cap_usd_mn": 2000.0,
                    "fx_rate": 0.5,
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "netcash.py"),
                "--input",
                str(input_file),
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        # gross_cash_usd = (1000 + 200) * 0.5 = 600; debt = 300 * 0.5 = 150 -> basic = 450
        assert out["basic_net_cash_usd"] == pytest.approx(450.0)
        # strict deducts deferred_revenue (50 USD) + restricted_cash (100 USD) + escrow (25 USD) = 175 USD -> strict = 275
        assert out["strict_net_cash_usd"] == pytest.approx(275.0)
        assert out["quasi_debt_items"][0]["value_local"] == pytest.approx(100.0)
        assert out["quasi_debt_items"][0]["value_usd"] == pytest.approx(50.0)

        with pytest.raises(ValueError, match="restricted_cash_free_reason"):
            netcash_mod.compute_netcash(
                cash_and_equivalents=500.0,
                restricted_cash=100.0,
                include_restricted_cash_in_free=True,
            )


class TestEarningsQuality:
    """Verify Sloan accrual ratio, CFO/NI cash conversion, and Cash Conversion Cycle (CCC)."""

    def test_healthy_earnings_quality(self) -> None:
        res = earnings_quality_mod.compute_earnings_quality(
            net_income=200.0,
            cfo=240.0,
            beginning_assets=1800.0,
            ending_assets=2200.0,
            cfi=-80.0,
            inventory=100.0,
            accounts_receivable=150.0,
            accounts_payable=120.0,
            revenue=1500.0,
            cogs=900.0,
            days_in_period=365.0,
            prior_ccc_days=30.0,
        )
        assert res["average_total_assets"] == pytest.approx(2000.0)
        assert res["sloan"]["operating_sloan_ratio"] == pytest.approx(-0.02)
        assert res["sloan"]["flag_gt_10pct"] is False
        assert res["cfo_to_ni"]["cfo_to_ni_ratio"] == pytest.approx(1.20)
        assert res["cfo_to_ni"]["flag_weak_conversion"] is False
        assert res["ccc"]["ccc_days"] == pytest.approx(28.39, abs=0.05)
        assert res["overall_quality"] == "strong"
        assert res["warnings"] == []

    def test_ccc_returns_null_when_components_missing_and_cli_ni_flags(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_DIR / "earnings_quality.py"),
                "--ni",
                "100",
                "--cfo",
                "120",
                "--assets-begin",
                "900",
                "--assets-end",
                "1100",
                "--revenue",
                "500",
                "--ar",
                "100",
                "--json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = json.loads(proc.stdout)
        assert out["average_total_assets"] == pytest.approx(1000.0)
        assert out["ccc"]["dso_days"] == pytest.approx(73.0)
        assert out["ccc"]["dio_days"] is None
        assert out["ccc"]["dpo_days"] is None
        assert out["ccc"]["ccc_days"] is None
        assert out["ccc"]["applicable"] is False

    def test_accrual_and_ccc_warnings(self) -> None:
        res = earnings_quality_mod.compute_earnings_quality(
            net_income=300.0,
            cfo=50.0,
            avg_total_assets=1500.0,
            dio=65.0,
            dso=80.0,
            dpo=35.0,
            prior_ccc_days=85.0,
        )
        assert res["sloan"]["flag_gt_10pct"] is True
        assert res["cfo_to_ni"]["flag_weak_conversion"] is True
        assert res["ccc"]["ccc_days"] == pytest.approx(110.0)
        assert res["ccc"]["flag_working_capital_deterioration"] is True
        assert res["overall_quality"] == "weak"
        assert len(res["warnings"]) == 3


class TestWorkflowsAndHelperScripts:
    """Verify all 6 prebuilt workflows compile in QuickJS and key skill scripts pass regression checks."""

    @pytest.mark.parametrize("wf_name", WPF_WORKFLOWS)
    def test_workflow_quickjs_compile(self, wf_name: str) -> None:
        from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
            PrebuiltWorkflowRegistry,
        )

        reg = PrebuiltWorkflowRegistry(REPO_ROOT)
        meta = reg.meta(wf_name)
        script = reg.get(wf_name)
        assert meta is not None, f"Workflow {wf_name} not loaded by PrebuiltWorkflowRegistry"
        assert script is not None and len(script) > 100
        assert meta.name == wf_name
        assert meta.description

    def test_scenario_irr_ps15_sample(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(DALTON_PLUGIN_DIR / "skills" / "dalton-call" / "scripts" / "scenario_irr.py"),
                "--current",
                "100",
                "--years",
                "2",
                "--prices",
                "80/130/170",
                "--probs",
                "0.25/0.5/0.25",
                "--yield",
                "0",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "14.0%" in proc.stdout

    def test_trading_days_nyse(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(
                    DALTON_PLUGIN_DIR
                    / "skills"
                    / "dalton-earnings-calendar"
                    / "scripts"
                    / "trading_days.py"
                ),
                "--date",
                "2026-04-28",
                "--minus",
                "3",
                "--exchange",
                "XNYS",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "2026-04-23"


def test_p0i_i6_unified_anchors_and_conflict_rejection(tmp_path: Path) -> None:
    """Verify P0I §6: --anchors unifies market_cap/fx/period across all 3 lenses and rejects simultaneous manual overrides."""
    anchors_file = tmp_path / "baba_anchors.json"
    anchors_payload = anchors_mod.build_anchors(
        "BABA",
        "2025-12-31",
        market_cap_usd_mn=271437.86,
        listing_currency="USD",
        reporting_currency="CNY",
        fx_rate=0.149772,
        as_of="2026-10-18",
    )
    anchors_file.write_text(json.dumps(anchors_payload), encoding="utf-8")

    # 1. netcash.py with --anchors
    nc_proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "netcash.py"),
            "--anchors",
            str(anchors_file),
            "--cash",
            "195321",
            "--restricted-cash",
            "41200",
            "--time-deposits",
            "182450",
            "--st-investments",
            "185900",
            "--st-debt",
            "18500",
            "--cur-lt-debt",
            "14200",
            "--lt-debt",
            "148900",
            "--convertible-debt",
            "35600",
            "--lease-liabilities",
            "22400",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    nc_out = json.loads(nc_proc.stdout)
    assert nc_out["anchors_source"] == "anchors"
    assert nc_out["ticker"] == "BABA"
    assert nc_out["period_end"] == "2025-12-31"
    assert nc_out["market_cap_usd_mn"] == pytest.approx(271437.86)
    assert nc_out["fx_rate_to_usd"] == pytest.approx(0.149772)
    assert nc_out["strict_net_cash_to_mcap_pct"] == pytest.approx(19.12, abs=0.02)

    # 2. return_score.py with --anchors
    rs_proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "return_score.py"),
            "--anchors",
            str(anchors_file),
            "--ttm-div",
            "1.05",
            "--ttm-buyback",
            "6.45",
            "--true-return-share",
            "0.85",
            "--annual",
            "3.8,6.2,7.5",
            "--fwd-total",
            "6.8",
            "--credibility",
            "90",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    rs_out = json.loads(rs_proc.stdout)
    assert rs_out["anchors_source"] == "anchors"
    assert rs_out["ticker"] == "BABA"
    assert rs_out["period_end"] == "2025-12-31"
    assert rs_out["market_cap_usd_mn"] == pytest.approx(271437.86)
    assert rs_out["fx_rate_to_usd"] == pytest.approx(0.149772)

    # 3. earnings_quality.py with --anchors
    eq_proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "earnings_quality.py"),
            "--anchors",
            str(anchors_file),
            "--ni",
            "129470",
            "--cfo",
            "163509",
            "--avg-assets",
            "1794669.5",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    eq_out = json.loads(eq_proc.stdout)
    assert eq_out["anchors_source"] == "anchors"
    assert eq_out["ticker"] == "BABA"
    assert eq_out["period_end"] == "2025-12-31"
    assert eq_out["market_cap_usd_mn"] == pytest.approx(271437.86)
    assert eq_out["fx_rate_to_usd"] == pytest.approx(0.149772)

    # 4. Reject simultaneous --anchors and manual --market-cap-usd-mn / --fx-rate on all 3 scripts
    for script_name, base_args in [
        ("netcash.py", ["--cash", "100"]),
        (
            "return_score.py",
            [
                "--ttm-div",
                "1.0",
                "--ttm-buyback",
                "2.0",
                "--true-return-share",
                "0.8",
                "--annual",
                "2,3,4",
                "--fwd-total",
                "3.0",
                "--credibility",
                "90",
            ],
        ),
        ("earnings_quality.py", ["--ni", "100", "--cfo", "120", "--avg-assets", "1000"]),
    ]:
        for manual_flag in (["--market-cap-usd-mn", "245000"], ["--fx-rate", "0.14"]):
            err_proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / script_name),
                    "--anchors",
                    str(anchors_file),
                    *base_args,
                    *manual_flag,
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
            assert err_proc.returncode == 2
            assert "Cannot specify both --anchors and manual" in err_proc.stderr

    # 5. Manual mode without --anchors marks anchors_source == "manual"
    manual_nc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "netcash.py"),
            "--cash",
            "100",
            "--market-cap-usd-mn",
            "1000",
            "--fx-rate",
            "0.5",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(manual_nc.stdout)["anchors_source"] == "manual"

