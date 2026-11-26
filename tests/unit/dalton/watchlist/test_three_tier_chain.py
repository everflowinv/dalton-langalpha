"""Three-tier (`core`/`watch`/`radar`) end-to-end consistency unit tests."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton.autonomy import schedule as schedule_mod
from src.dalton.autonomy import tracking as tracking_mod
from src.dalton.coverage import models as cov_models
from src.dalton.coverage import service as cov_svc
from src.dalton.database import coverage as coverage_db

REPO_ROOT = Path(__file__).resolve().parents[4]
CALENDAR_STATE_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-earnings-calendar"
    / "scripts"
    / "calendar_state.py"
)
INIT_COMPANY_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-init-company"
    / "scripts"
    / "init_company.py"
)
SCREEN_MOVERS_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-weekly-review"
    / "scripts"
    / "screen_movers.py"
)
TRACKER_13F_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-13f-tracker"
    / "scripts"
    / "13f_skill.py"
)


def _load_script_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_models_three_tier_roundtrip_and_dedicated_watchlist_fields() -> None:
    assert cov_models.VALID_TIERS == frozenset({"core", "watch", "radar"})
    assert cov_models.normalize_tier("RADAR") == "radar"
    with pytest.raises(ValueError, match="Invalid tier"):
        cov_models.normalize_tier("unknown_tier")

    radar_comp = cov_models.make_new_company_dict(
        "NET",
        name="Cloudflare",
        tier="radar",
        extra_fields={
            "tags": ["infra", "ai"],
            "is_pinned": True,
            "sort_order": 5,
            "task_overrides": {"watch_enabled": False, "earnings_stages": ["t0"]},
        },
    )
    assert radar_comp["tier"] == "radar"
    assert radar_comp["sheet_section"] == "Radar"
    assert radar_comp["earnings"]["group"] == "C"

    row = cov_models.company_to_row(radar_comp, user_id="u1", source="ui")
    assert row["tier"] == "radar"
    assert row["tags"] == ["infra", "ai"]
    assert row["is_pinned"] is True
    assert row["sort_order"] == 5
    assert row["task_overrides"] == {"watch_enabled": False, "earnings_stages": ["t0"]}
    # Dedicated watchlist columns must not leak into extra JSONB
    for k in cov_models.WATCHLIST_DEDICATED_KEYS:
        assert k not in row["extra"]

    restored = cov_models.row_to_company(row)
    assert restored["tier"] == "radar"
    assert restored["tags"] == ["infra", "ai"]
    assert restored["is_pinned"] is True
    assert restored["sort_order"] == 5
    assert restored["task_overrides"] == {"watch_enabled": False, "earnings_stages": ["t0"]}

    doc = cov_models.build_coverage_doc([restored])
    assert doc["tiers"]["radar"]["count"] == 1
    assert doc["tiers"]["core"]["count"] == 0
    assert doc["tiers"]["watch"]["count"] == 0


def test_schedule_radar_and_unknown_tiers_build_zero_tasks() -> None:
    core_specs = schedule_mod.build_company_watch_specs(
        ticker="NVDA", workspace_id="ws-1", tier="core"
    )
    watch_specs = schedule_mod.build_company_watch_specs(
        ticker="AVGO", workspace_id="ws-2", tier="watch"
    )
    radar_specs = schedule_mod.build_company_watch_specs(
        ticker="NET", workspace_id="ws-3", tier="radar"
    )
    unknown_specs = schedule_mod.build_company_watch_specs(
        ticker="XYZ", workspace_id="ws-4", tier="mystery_tier"
    )
    private_specs = schedule_mod.build_company_watch_specs(
        ticker="SPCX", workspace_id="ws-5", tier="core", is_private=True
    )

    assert len(core_specs) == 4
    assert len(watch_specs) == 4
    assert radar_specs == []
    assert unknown_specs == []
    assert private_specs == []


@pytest.mark.asyncio
async def test_service_get_user_tier_and_summary_include_radar_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NVDA", name="NVIDIA", tier="core"),
            user_id="u1",
            source="chat",
        ),
        cov_models.company_to_row(
            cov_models.make_new_company_dict("AVGO", name="Broadcom", tier="watch"),
            user_id="u1",
            source="chat",
        ),
        cov_models.company_to_row(
            cov_models.make_new_company_dict("NET", name="Cloudflare", tier="radar"),
            user_id="u1",
            source="chat",
        ),
    ]
    by_ticker = {r["ticker"]: r for r in rows}

    monkeypatch.setattr(
        coverage_db,
        "get_coverage_row",
        create_autospec(
            coverage_db.get_coverage_row,
            side_effect=lambda uid, ticker, *, conn=None: by_ticker.get(ticker.upper()),
        ),
    )
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(coverage_db.list_coverage_rows, return_value=rows),
    )
    monkeypatch.setattr(
        coverage_db,
        "list_coverage_groups",
        create_autospec(coverage_db.list_coverage_groups, return_value=[]),
    )
    monkeypatch.setattr(
        coverage_db,
        "get_earnings_calendar_mirror",
        create_autospec(coverage_db.get_earnings_calendar_mirror, return_value=None),
    )
    monkeypatch.setattr(
        cov_svc,
        "_overlay_canonical_unit_workspaces",
        create_autospec(cov_svc._overlay_canonical_unit_workspaces, return_value=False),
    )

    assert await cov_svc.get_user_tier("u1", "NET") == "radar"
    assert await tracking_mod.resolve_default_tier("NET", user_id="u1") == "radar"

    summary = await cov_svc.list_user_coverage_summary("u1")
    assert summary["counts"]["core"] == 1
    assert summary["counts"]["watch"] == 1
    assert summary["counts"]["radar"] == 1
    assert summary["counts"]["total"] == 3
    assert [c["ticker"] for c in summary["radar"]] == ["NET"]

    # Company union rank order: core > watch > radar
    assert cov_svc._roster_rank_beats(("core", "2026-01-01", "b"), ("watch", "2026-12-01", "a"))
    assert cov_svc._roster_rank_beats(("watch", "2026-01-01", "b"), ("radar", "2026-12-01", "a"))
    assert not cov_svc._roster_rank_beats(("radar", "2026-12-01", "a"), ("watch", "2026-01-01", "b"))


def test_tracking_derive_status_radar_with_zero_tasks_is_disabled() -> None:
    derived = tracking_mod._derive_status_and_details(
        workspace_id="ws-net",
        ticker="NET",
        tier="radar",
        company_rows=[],
    )
    assert derived["status"] == "disabled"
    assert derived["tasks"] == []


def test_calendar_state_radar_unknown_tier_and_no_workspace_entry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cal = _load_script_module("test_calendar_state_watchlist", CALENDAR_STATE_PATH)

    assert cal.CHAIN_BY_TIER["core"] == ["tm3", "t0", "tp1"]
    assert cal.CHAIN_BY_TIER["watch"] == ["t0"]
    assert cal.CHAIN_BY_TIER["radar"] == []
    # Unknown tier must default to empty chain []
    assert cal.resolve_earnings_stages("unknown_tier") == []
    assert cal.default_automations_for("NET", "FY26Q4", tier="radar") == {}
    assert cal.default_automations_for("XYZ", "FY26Q4", tier="unknown_tier") == {}
    # No-workspace listed stock creates zero automation files even when Core/Watch
    assert cal.default_automations_for("CRWD", "FY26Q4", tier="core", workspace_id=None) == {}

    cov_file = tmp_path / "coverage.yaml"
    state_file = tmp_path / "earnings-calendar.json"
    scan_file = tmp_path / "scan.jsonl"
    cov_file.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "tier_policies": {
                    "core": {"earnings_stages": ["tm3", "t0", "tp1"]},
                    "watch": {"earnings_stages": ["t0"]},
                    "radar": {"earnings_stages": []},
                },
                "companies": [
                    {
                        "ticker": "NET",
                        "name": "Cloudflare",
                        "tier": "radar",
                        "market": "US",
                        "workspace": {"name": "NET", "status": "none"},
                        "earnings": {"group": "C"},
                    },
                    {
                        "ticker": "CRWD",
                        "name": "CrowdStrike",
                        "tier": "watch",
                        "market": "US",
                        "workspace": {"name": "CRWD", "status": "none"},
                        "earnings": {"group": "B"},
                    },
                    {
                        "ticker": "SPCX",
                        "name": "SpaceX",
                        "tier": "core",
                        "market": "PRIVATE",
                        "is_private": True,
                        "workspace": {"name": "SPCX", "status": "active"},
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # list includes no-workspace radar & watch stocks, skips unlisted private company
    rc_list = cal.main(
        ["--coverage", str(cov_file), "--state", str(state_file), "--json", "list"]
    )
    assert rc_list == 0
    list_payload = json.loads(capsys.readouterr().out)
    tickers = [c["ticker"] for c in list_payload["companies"]]
    assert "NET" in tickers
    assert "CRWD" in tickers
    assert "SPCX" not in tickers
    net_row = next(c for c in list_payload["companies"] if c["ticker"] == "NET")
    assert net_row["tier"] == "Radar"
    assert net_row["has_workspace"] is False

    # mark confirmed for no-workspace NET (radar) and CRWD (watch, no workspace)
    rc_m1 = cal.main(
        [
            "--coverage",
            str(cov_file),
            "--state",
            str(state_file),
            "mark",
            "--ticker",
            "NET",
            "--status",
            "confirmed",
            "--date",
            "2026-11-05",
            "--quarter",
            "FY26Q3",
        ]
    )
    assert rc_m1 == 0
    m1 = json.loads(capsys.readouterr().out)
    assert m1["entry"]["tier"] == "Radar"
    assert m1["entry"]["automations"] == {}

    rc_m2 = cal.main(
        [
            "--coverage",
            str(cov_file),
            "--state",
            str(state_file),
            "mark",
            "--ticker",
            "CRWD",
            "--status",
            "confirmed",
            "--date",
            "2026-12-01",
            "--quarter",
            "FY27Q3",
        ]
    )
    assert rc_m2 == 0
    m2 = json.loads(capsys.readouterr().out)
    assert m2["entry"]["automations"] == {}

    # check recognizes confirmed no-workspace / radar entries as already recorded (action=skip)
    rc_chk = cal.main(
        [
            "--coverage",
            str(cov_file),
            "--state",
            str(state_file),
            "check",
            "--ticker",
            "NET",
            "--date",
            "2026-11-05",
        ]
    )
    assert rc_chk == 0
    chk = json.loads(capsys.readouterr().out)
    assert chk["recorded"] is True
    assert chk["action"] == "skip"

    # diff reconciles radar with workspace_id AND no-workspace stocks with zero automation errors
    ledger = json.loads(state_file.read_text(encoding="utf-8"))
    ledger["entries"]["NET"]["workspace_id"] = "ws-net-1"
    state_file.write_text(json.dumps(ledger), encoding="utf-8")
    scan_file.write_text(
        "\n".join(
            [
                json.dumps({"ticker": "NET", "status": "confirmed", "earnings_date": "2026-11-05"}),
                json.dumps({"ticker": "CRWD", "status": "confirmed", "earnings_date": "2026-12-01"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rc_diff = cal.main(
        [
            "--coverage",
            str(cov_file),
            "--state",
            str(state_file),
            "diff",
            "--scan",
            str(scan_file),
        ]
    )
    assert rc_diff == 0
    diff_out = json.loads(capsys.readouterr().out)
    assert diff_out["reconciled"] is True
    assert diff_out["missing"] == []


def test_init_company_and_other_skills_recognize_radar(tmp_path: Path) -> None:
    # 1. init_company renders radar cleanly and validates against bundled company.schema.json
    init_mod = _load_script_module("test_init_company_watchlist", INIT_COMPANY_PATH)
    files = init_mod.render(
        "NET",
        {"ticker": "NET", "name": "Cloudflare", "tier": "radar", "market": "US"},
    )
    company_doc = json.loads(files["data/company.json"])
    assert company_doc["coverage_tier"] == "radar"
    assert "Radar" in company_doc["one_liner"]
    assert "Radar" in files["agent.md"]

    # 2. screen_movers removed BUILTIN_TICKERS, recognizes Radar, skips is_private
    movers_mod = _load_script_module("test_screen_movers_watchlist", SCREEN_MOVERS_PATH)
    assert not hasattr(movers_mod, "BUILTIN_TICKERS")
    cov_yaml = tmp_path / "cov.yaml"
    cov_yaml.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {"ticker": "NVDA", "name": "NVIDIA", "tier": "core", "peer_group": "semi"},
                    {"ticker": "NET", "name": "Cloudflare", "tier": "radar", "peer_group": "infra"},
                    {"ticker": "PRIV1", "name": "PrivCo", "tier": "core", "is_private": True},
                ]
            }
        ),
        encoding="utf-8",
    )
    entries, _, skipped = movers_mod.load_universe_from_coverage(str(cov_yaml))
    by_t = {e["ticker"]: e for e in entries}
    assert by_t["NET"]["tier"] == "Radar"
    assert "PRIV1" not in by_t
    assert any("PRIV1" in s for s in skipped)

    # 3. 13f_skill recognizes is_radar
    skill_13f = _load_script_module("test_13f_skill_watchlist", TRACKER_13F_PATH)
    roster, _, err = skill_13f._load_coverage_roster(None, coverage_path=str(cov_yaml))
    assert err is None
    net_13f = next(r for r in roster if r["ticker"] == "NET")
    assert net_13f["tier"] == "radar"
    assert net_13f["is_radar"] is True
    assert net_13f["is_core_coverage"] is False
    assert net_13f["is_watch"] is False
