"""Offline unit tests for dalton-13f-tracker skill."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-13f-tracker"
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
SCRIPTS_DIR = SKILL_DIR / "scripts"
JSON_CONFIG_FILES = (
    "classification.json",
    "descriptors.json",
    "institution_map.json",
    "merge_rules.json",
    "name_to_ticker.json",
    "ticker_aliases.json",
    "verified_tickers.json",
)
SKILL_SCRIPT_FILES = (
    "scripts/13f_skill.py",
    "scripts/audit_13f_run.py",
)


def _load_audit_module():
    mod_name = "dalton_13f_audit_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    target = SCRIPTS_DIR / "audit_13f_run.py"
    spec = importlib.util.spec_from_file_location(mod_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module

GOLDEN_FORMATTED_REPORT = (
    "\n**China Book**\n"
    "- PDD仓位几乎不变，1.5亿美元\n"
    "- 继25Q3建仓后，继续加仓Alibaba 20%至1.2亿美元\n"
    "- 建仓H World CB，4500万美元\n"
    "- 清仓Baidu（之前持仓4900万美元）\n"
    "\n"
    "**AI/Semi/US SaaS**\n"
    "- 建仓NVIDIA，5.4亿美元\n"
    "- 大幅加仓Alphabet至2.9亿美元，之前持仓1.5亿美元\n"
    "\n"
    "**Other US Book**\n"
    "- 继25Q3减仓后，继续减仓Sea 21%至8000万美元"
)


def _load_13f_module():
    mod_name = "dalton_13f_skill_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    target = SCRIPTS_DIR / "13f_skill.py"
    spec = importlib.util.spec_from_file_location(mod_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _identity_sig(user_identity: str) -> None:
    return None


def _company_sig(cik_or_ticker: str | int) -> object:
    return None


def _get_filings_sig(form: str = "13F-HR") -> list[object]:
    return []


def _filing_obj_sig() -> object:
    return None


def _compare_holdings_sig() -> object:
    return None


def _make_filing(
    *,
    report_period: str,
    manager: str,
    comparison_rows: list[dict] | None = None,
    previous_period: str = "",
) -> object:
    df = pd.DataFrame(comparison_rows or [])
    comparison_obj = SimpleNamespace(data=df, previous_period=previous_period)

    compare_fn = create_autospec(_compare_holdings_sig, spec_set=True)
    compare_fn.return_value = comparison_obj

    thirteenf = SimpleNamespace(
        report_period=report_period,
        management_company_name=manager,
        compare_holdings=compare_fn,
    )
    obj_fn = create_autospec(_filing_obj_sig, spec_set=True)
    obj_fn.return_value = thirteenf
    return SimpleNamespace(obj=obj_fn)


def _make_company_stub(cik: int, filings: list[object]):
    get_filings_fn = create_autospec(_get_filings_sig, spec_set=True)
    get_filings_fn.return_value = filings
    company_obj = SimpleNamespace(cik=cik, get_filings=get_filings_fn)
    company_cls = create_autospec(_company_sig, spec_set=True)
    company_cls.return_value = company_obj
    return company_cls


def _reset_env_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)


# ---------------------------------------------------------------------------
# Registry, frontmatter, paths, and skill-ref scanner
# ---------------------------------------------------------------------------


def test_registry_frontmatter_and_skill_refs() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )
    from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY

    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert SKILL_NAME in DALTON_SKILL_DEFINITIONS
    assert SKILL_NAME in SKILL_REGISTRY

    skill_md_path = SKILL_DIR / "SKILL.md"
    skill_md = skill_md_path.read_text(encoding="utf-8")
    assert skill_md.startswith("---\n")
    end_idx = skill_md.find("\n---\n", 4)
    meta = yaml.safe_load(skill_md[4:end_idx])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    desc = meta["description"].strip()
    assert 0 < len(desc) <= 120
    assert "触发词" not in desc
    assert desc == read_skill_description(SKILL_NAME)

    # Every script path referenced in SKILL.md exists under SKILL_DIR and every script is registered
    rel_scripts = set(re.findall(r"\.agents/skills/dalton-13f-tracker/([^\s`'\")]+\.py)", skill_md))
    assert rel_scripts == set(SKILL_SCRIPT_FILES)
    on_disk_scripts = {f"scripts/{p.name}" for p in SCRIPTS_DIR.glob("*.py")}
    assert on_disk_scripts == set(SKILL_SCRIPT_FILES)
    for rel in rel_scripts:
        assert (SKILL_DIR / rel).is_file()
    on_disk_jsons = {p.name for p in SCRIPTS_DIR.glob("*.json")}
    assert on_disk_jsons == set(JSON_CONFIG_FILES)
    for cfg in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / cfg).is_file()
        assert f"scripts/{cfg}" in skill_md
    dalton_paths = (REPO_ROOT / "scripts" / "dalton" / "dalton_paths.txt").read_text(encoding="utf-8")
    assert f"plugins/dalton/skills/{SKILL_NAME}/" in dalton_paths
    assert not (SKILL_DIR / "run.sh").exists()


# ---------------------------------------------------------------------------
# SEC identity precedence
# ---------------------------------------------------------------------------


def test_sec_identity_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)

    set_id_stub = create_autospec(_identity_sig, spec_set=True)
    monkeypatch.setattr(mod, "set_identity", set_id_stub)

    # 1. Default fallback matches src/dalton/settings.py
    ident, source = mod.configure_identity()
    assert source == "default"
    assert ident == "DaltonResearch research@everflowinv.com"
    assert ident == mod.DEFAULT_EDGAR_IDENTITY

    # 2. EDGAR_IDENTITY overrides default
    monkeypatch.setenv("EDGAR_IDENTITY", "EdgarUser edgar@example.com")
    ident, source = mod.configure_identity()
    assert source == "EDGAR_IDENTITY"
    assert ident == "EdgarUser edgar@example.com"

    # 3. SEC_USER_AGENT takes highest precedence
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonBot research@example.com")
    ident, source = mod.configure_identity()
    assert source == "SEC_USER_AGENT"
    assert ident == "DaltonBot research@example.com"
    assert set_id_stub.call_count == 3


# ---------------------------------------------------------------------------
# self-test (offline via create_autospec)
# ---------------------------------------------------------------------------


def test_self_test_passes_offline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    filing = _make_filing(report_period="2025-12-31", manager="HHLR Advisors, Ltd.")
    monkeypatch.setattr(mod, "Company", _make_company_stub(1762304, [filing]))

    monkeypatch.setattr(sys, "argv", ["13f_skill.py", "self-test", "--json"])
    mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert all(c["ok"] for c in out["checks"])
    env_check = next(c for c in out["checks"] if c["name"] == "env.SEC_IDENTITY")
    assert env_check["source"] == "SEC_USER_AGENT"


# ---------------------------------------------------------------------------
# Read-only disk mode when --workspace-dir is omitted (in-memory learning active)
# ---------------------------------------------------------------------------


def test_readonly_without_workspace_dir_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    before_bytes = {name: (SCRIPTS_DIR / name).read_bytes() for name in JSON_CONFIG_FILES}

    # map-propose without --workspace-dir refuses to write
    monkeypatch.setattr(sys, "argv", ["13f_skill.py", "map-propose", "--instruction", "把 ZZZX 从 AI 改到 Other", "--json"])
    mod.main()
    captured = capsys.readouterr()
    propose_res = json.loads(captured.out)
    assert propose_res["ok"] is False
    assert "--workspace-dir" in captured.err

    # map-apply without --workspace-dir refuses to write
    monkeypatch.setattr(sys, "argv", ["13f_skill.py", "map-apply", "--patch-id", "p1", "--json"])
    mod.main()
    apply_res = json.loads(capsys.readouterr().out)
    assert apply_res["ok"] is False

    # compare with unmapped ticker still runs auto-learn in memory, writes 0 files
    unknown_row = [
        {
            "Cusip": "999999100",
            "Ticker": "ZZZUNKN",
            "Issuer": "ZZZ UNKNOWN CORP",
            "Shares": 100_000,
            "Value": 50_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        }
    ]
    filing = _make_filing(report_period="2025-12-31", manager="Test Manager", comparison_rows=unknown_row)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9990001, [filing]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9990001", "--format", "cn", "--json"],
    )
    mod.main()
    cmp_res = json.loads(capsys.readouterr().out)
    assert cmp_res["ok"] is True
    assert cmp_res["auto_learn"] is True

    assert not (SKILL_DIR / "temp").exists()
    for name in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / name).read_bytes() == before_bytes[name]


def test_in_memory_learning_matches_report_with_and_without_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """In-memory auto-learn (share-class merge, alias lookup, classification) yields identical report whether --workspace-dir is passed or omitted."""
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    # Row 1: unknown SEC ticker whose issuer maps in name_to_ticker.json -> BABA (China Book)
    # Rows 2-3: two unconfigured share classes sharing issuer base so _build_report learns merge rule
    rows = [
        {
            "Cusip": "111111101",
            "Ticker": "BABAX",
            "Issuer": "ALIBABA GROUP HLDG LTD",
            "Shares": 100_000,
            "Value": 120_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "222222101",
            "Ticker": "ACMEA",
            "Issuer": "ACME CLOUD INC CLASS A",
            "Shares": 200_000,
            "Value": 80_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 200_000,
            "Status": "NEW",
        },
        {
            "Cusip": "222222102",
            "Ticker": "ACMEB",
            "Issuer": "ACME CLOUD INC CLASS B",
            "Shares": 100_000,
            "Value": 40_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
    ]

    before_bytes = {name: (SCRIPTS_DIR / name).read_bytes() for name in JSON_CONFIG_FILES}

    # Run WITHOUT --workspace-dir
    filing_ro = _make_filing(report_period="2025-12-31", manager="Learning Check Fund", comparison_rows=rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9990002, [filing_ro]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9990002", "--format", "cn", "--json"],
    )
    mod.main()
    res_no_ws = json.loads(capsys.readouterr().out)

    for name in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / name).read_bytes() == before_bytes[name]

    # Reset in-memory state before running fresh WITH --workspace-dir
    mod._init_state(None)

    # Run WITH --workspace-dir
    ws = tmp_path / "ws_learn_parity"
    filing_ws = _make_filing(report_period="2025-12-31", manager="Learning Check Fund", comparison_rows=rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9990002, [filing_ws]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "compare",
            "--institution",
            "9990002",
            "--workspace-dir",
            str(ws),
            "--format",
            "cn",
            "--json",
        ],
    )
    mod.main()
    res_with_ws = json.loads(capsys.readouterr().out)

    assert res_no_ws["formatted_report"] == res_with_ws["formatted_report"]
    assert (ws / "state" / "dalton-13f-tracker" / "merge_rules.json").is_file()
    mod._init_state(None)


# ---------------------------------------------------------------------------
# Workspace state isolation (auto-learn + map-propose/map-apply)
# ---------------------------------------------------------------------------


def test_workspace_auto_learn_and_map_patch_write_to_workspace_state_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    ws = tmp_path / "ws"
    state_dir = ws / "state" / "dalton-13f-tracker"
    before_bytes = {name: (SCRIPTS_DIR / name).read_bytes() for name in JSON_CONFIG_FILES}

    # 1. compare triggers auto-learn for unknown ticker + unknown institution alias
    rows = [
        {
            "Cusip": "888888100",
            "Ticker": "ZZZTST",
            "Issuer": "ZZZ TEST TECH INC",
            "Shares": 500_000,
            "Value": 60_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 500_000,
            "Status": "NEW",
        }
    ]
    filing = _make_filing(report_period="2025-12-31", manager="Unit Fund LLC", comparison_rows=rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(8880001, [filing]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "unit_fund_alias", "--workspace-dir", str(ws), "--json"],
    )
    mod.main()
    cmp_res = json.loads(capsys.readouterr().out)
    assert cmp_res["ok"] is True
    assert cmp_res["auto_learn"] is True

    cls_overlay = json.loads((state_dir / "classification.json").read_text(encoding="utf-8"))
    assert "ZZZTST" in cls_overlay["other_us_book"]
    inst_overlay = json.loads((state_dir / "institution_map.json").read_text(encoding="utf-8"))
    assert inst_overlay["unit_fund_alias"] == "8880001"
    log_text = (state_dir / "auto_learn_log.jsonl").read_text(encoding="utf-8")
    assert "ZZZTST" in log_text

    # 2. map-propose -> map-apply writes patch and updates workspace overlay
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "map-propose",
            "--instruction",
            "把 ZZZTST 从 Other 改到 AI",
            "--workspace-dir",
            str(ws),
            "--json",
        ],
    )
    mod.main()
    prop_res = json.loads(capsys.readouterr().out)
    assert prop_res["ok"] is True
    patch_id = prop_res["patch_id"]
    assert (state_dir / "map_patches.json").is_file()

    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "map-apply", "--patch-id", patch_id, "--workspace-dir", str(ws), "--json"],
    )
    mod.main()
    app_res = json.loads(capsys.readouterr().out)
    assert app_res["ok"] is True
    assert app_res["status"] == "applied"

    cls_overlay_after = json.loads((state_dir / "classification.json").read_text(encoding="utf-8"))
    assert "ZZZTST" in cls_overlay_after["ai_semi_saas"]
    assert "ZZZTST" not in cls_overlay_after["other_us_book"]

    # Built-in JSONs in skill directory remain untouched
    for name in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / name).read_bytes() == before_bytes[name]


# ---------------------------------------------------------------------------
# Workspace overlay takes precedence over built-in data
# ---------------------------------------------------------------------------


def test_workspace_overlay_overrides_builtin_data(tmp_path: Path) -> None:
    mod = _load_13f_module()
    ws = tmp_path / "ws_override"
    state_dir = ws / "state" / "dalton-13f-tracker"
    state_dir.mkdir(parents=True)

    # Override AAPL (built-in other_us_book) -> china_book, plus institution/alias/merge overrides
    (state_dir / "classification.json").write_text(
        json.dumps({"china_book": ["AAPL"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "institution_map.json").write_text(
        json.dumps({"hhlr": "0009998888"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "ticker_aliases.json").write_text(
        json.dumps({"OLDTICK": "BABA"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (state_dir / "merge_rules.json").write_text(
        json.dumps({"CUSTOMA": "CustomGroup", "CUSTOMB": "CustomGroup"}, ensure_ascii=False),
        encoding="utf-8",
    )

    mod._init_state(ws)
    assert "AAPL" in mod._CHINA_TICKERS
    assert "AAPL" not in mod._OTHER_US_TICKERS
    assert mod._INSTITUTION_MAP["hhlr"] == "0009998888"
    assert mod._TICKER_ALIASES["OLDTICK"] == "BABA"
    assert mod._MERGE_RULES["CUSTOMA"] == "CustomGroup"
    assert mod._MERGE_RULES["CUSTOMB"] == "CustomGroup"

    # Reset back to built-in read-only state after test
    mod._init_state(None)


# ---------------------------------------------------------------------------
# Concurrency, symlink containment, and convertible-bond value fidelity
# ---------------------------------------------------------------------------


def test_concurrent_writes_preserve_updates_from_stale_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent processes starting from the same snapshot merge updates without lost writes."""
    target = SCRIPTS_DIR / "13f_skill.py"
    spec_a = importlib.util.spec_from_file_location("dalton_13f_proc_a", target)
    spec_b = importlib.util.spec_from_file_location("dalton_13f_proc_b", target)
    assert spec_a is not None and spec_a.loader is not None
    assert spec_b is not None and spec_b.loader is not None
    proc_a = importlib.util.module_from_spec(spec_a)
    proc_b = importlib.util.module_from_spec(spec_b)
    spec_a.loader.exec_module(proc_a)
    spec_b.loader.exec_module(proc_b)

    ws = tmp_path / "ws_concurrent"
    state_dir = ws / "state" / "dalton-13f-tracker"

    # Both processes initialize from the same pre-write workspace snapshot
    proc_a._init_state(ws)
    proc_b._init_state(ws)

    # Process A writes classification + dict + patch + log
    monkeypatch.setattr(proc_a.time, "time", lambda: 1700000001)
    proc_a._learn_classification("ZZZCA", "China Book", source="auto-fallback")
    proc_a._learn_institution_alias("conc_fund_a", "1110001", source="auto")
    store_a = proc_a._load_patch_store()
    store_a.setdefault("patches", []).append(
        {"id": "pa_1", "ts": "2026-01-01T00:00:00", "instruction": "op a", "operations": [], "status": "proposed"}
    )
    proc_a._save_patch_store(store_a)

    # Process B still has stale in-memory state (neither A's mappings nor A's patch)
    assert "ZZZCA" not in proc_b._CHINA_TICKERS
    assert "conc_fund_a" not in proc_b._INSTITUTION_MAP

    monkeypatch.setattr(proc_b.time, "time", lambda: 1700000002)
    proc_b._learn_classification("ZZZCB", "Other US Book", source="auto-fallback")
    proc_b._learn_institution_alias("conc_fund_b", "2220002", source="auto")
    store_b = proc_b._load_patch_store()
    # Simulate stale snapshot on B before A's patch was saved
    store_b["patches"] = [
        {"id": "pb_1", "ts": "2026-01-01T00:00:01", "instruction": "op b", "operations": [], "status": "proposed"}
    ]
    proc_b._save_patch_store(store_b)

    cls_on_disk = json.loads((state_dir / "classification.json").read_text(encoding="utf-8"))
    assert "ZZZCA" in cls_on_disk["china_book"]
    assert "ZZZCB" in cls_on_disk["other_us_book"]

    inst_on_disk = json.loads((state_dir / "institution_map.json").read_text(encoding="utf-8"))
    assert inst_on_disk["conc_fund_a"] == "1110001"
    assert inst_on_disk["conc_fund_b"] == "2220002"

    patches_on_disk = json.loads((state_dir / "map_patches.json").read_text(encoding="utf-8"))
    patch_ids = [p["id"] for p in patches_on_disk["patches"]]
    assert patch_ids == ["pa_1", "pb_1"]

    log_text = (state_dir / "auto_learn_log.jsonl").read_text(encoding="utf-8")
    assert "ZZZCA" in log_text
    assert "ZZZCB" in log_text


def test_state_dir_and_target_symlink_escape_disables_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()

    # 1. state dir symlink pointing outside workspace disables writes
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    ws_escaped = tmp_path / "ws_escaped"
    ws_escaped.mkdir()
    (ws_escaped / "state").symlink_to(outside_dir)

    mod._init_state(ws_escaped)
    err = capsys.readouterr().err
    assert "outside workspace" in err
    assert mod._WRITES_ENABLED is False
    assert mod._STATE_DIR is None
    mod._learn_classification("ZZZESC", "Other US Book", source="auto-fallback")
    assert list(outside_dir.iterdir()) == []

    # 2. valid state dir, but target file is a symlink escaping workspace
    ws_valid = tmp_path / "ws_target_symlink"
    state_dir = ws_valid / "state" / "dalton-13f-tracker"
    state_dir.mkdir(parents=True)
    outside_target = tmp_path / "outside_classification.json"
    (state_dir / "classification.json").symlink_to(outside_target)

    mod._init_state(ws_valid)
    assert mod._WRITES_ENABLED is True
    mod._learn_classification("ZZZSYM", "Other US Book", source="auto-fallback")
    err2 = capsys.readouterr().err
    assert "outside workspace" in err2
    assert not outside_target.exists()

    mod._init_state(None)


def test_convertible_bond_value_below_par_is_not_scaled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Convertible bonds with Value < principal Shares (ratio < 1.0) must not be 1000x scaled."""
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    cb_rows = [
        {
            "Cusip": "448579AB8",
            "Ticker": "HTHT",
            "Issuer": "H WORLD GROUP LTD",
            "Shares": 1_000_000,
            "Value": 900_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 1_000_000,
            "Status": "NEW",
        }
    ]
    filing = _make_filing(report_period="2025-12-31", manager="CB Bond Fund", comparison_rows=cb_rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9995555, [filing]))

    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9995555", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    assert res["rows"][0]["Value"] == pytest.approx(900_000.0)
    assert "100万美元" in res["formatted_report"]
    assert "9.0亿美元" not in res["formatted_report"]


# ---------------------------------------------------------------------------
# Golden formatted_report format regression
# ---------------------------------------------------------------------------


def test_compare_formatted_report_matches_golden_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    current_rows = [
        {"Cusip": "01609W102", "Ticker": "BABA", "Issuer": "ALIBABA GROUP HOLDING LTD", "Shares": 1_200_000, "Value": 120_000_000.0, "PrevShares": 1_000_000, "PrevValue": 95_000_000.0, "ShareChange": 200_000, "Status": "INCREASED"},
        {"Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC", "Shares": 1_010_000, "Value": 150_000_000.0, "PrevShares": 1_000_000, "PrevValue": 140_000_000.0, "ShareChange": 10_000, "Status": "INCREASED"},
        {"Cusip": "448579AB8", "Ticker": "HTHT", "Issuer": "H WORLD GROUP LTD", "Shares": 500_000, "Value": 45_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 500_000, "Status": "NEW"},
        {"Cusip": "67066G104", "Ticker": "NVDA", "Issuer": "NVIDIA CORP", "Shares": 4_000_000, "Value": 540_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 4_000_000, "Status": "NEW"},
        {"Cusip": "02079K305", "Ticker": "GOOGL", "Issuer": "ALPHABET INC", "Shares": 1_000_000, "Value": 180_000_000.0, "PrevShares": 500_000, "PrevValue": 85_000_000.0, "ShareChange": 500_000, "Status": "INCREASED"},
        {"Cusip": "02079K107", "Ticker": "GOOG", "Issuer": "ALPHABET INC", "Shares": 600_000, "Value": 110_000_000.0, "PrevShares": 400_000, "PrevValue": 68_000_000.0, "ShareChange": 200_000, "Status": "INCREASED"},
        {"Cusip": "81141R100", "Ticker": "SE", "Issuer": "SEA LTD", "Shares": 790_000, "Value": 80_000_000.0, "PrevShares": 1_000_000, "PrevValue": 95_000_000.0, "ShareChange": -210_000, "Status": "DECREASED"},
        {"Cusip": "056752108", "Ticker": "BIDU", "Issuer": "BAIDU INC", "Shares": 0, "Value": 0.0, "PrevShares": 500_000, "PrevValue": 49_000_000.0, "ShareChange": -500_000, "Status": "CLOSED"},
        {"Cusip": "60937P106", "Ticker": "MRNA", "Issuer": "MODERNA INC", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
    ]
    prior_rows = [
        {"Cusip": "01609W102", "Ticker": "BABA", "Issuer": "ALIBABA GROUP HOLDING LTD", "Shares": 1_000_000, "Value": 95_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 1_000_000, "Status": "NEW"},
        {"Cusip": "81141R100", "Ticker": "SE", "Issuer": "SEA LTD", "Shares": 1_000_000, "Value": 95_000_000.0, "PrevShares": 1_200_000, "PrevValue": 110_000_000.0, "ShareChange": -200_000, "Status": "DECREASED"},
    ]

    f0 = _make_filing(report_period="2025-12-31", manager="Test Advisor", comparison_rows=current_rows)
    f1 = _make_filing(report_period="2025-09-30", manager="Test Advisor", comparison_rows=prior_rows)
    # Use a CIK outside _DEFAULT_MERGES so cmd_compare doesn't query sibling funds
    monkeypatch.setattr(mod, "Company", _make_company_stub(9991234, [f0, f1]))

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "compare",
            "--institution",
            "0009991234",
            "--format",
            "cn",
            "--auto-learn",
            "false",
            "--json",
        ],
    )
    mod.main()
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    assert res["total_positions"] == 7
    assert res["filtered_biotech"] == 2
    assert res["formatted_report"] == GOLDEN_FORMATTED_REPORT
    mod._init_state(None)


# ---------------------------------------------------------------------------
# Ticker normalization, short-name expansion, issuer fallback, AI hints & gate
# ---------------------------------------------------------------------------


def test_clean_ticker_preserves_real_nan_ticker_and_rejects_missing() -> None:
    mod = _load_13f_module()
    mod._init_state(None)

    assert mod._clean_ticker(None) == ""
    assert mod._clean_ticker(float("nan")) == ""
    assert mod._clean_ticker(pd.NA) == ""
    assert mod._clean_ticker("<NA>") == ""
    assert mod._clean_ticker("   ") == ""
    # Real NYSE municipal bond fund ticker NAN must not be treated as missing
    assert mod._clean_ticker("NAN") == "NAN"
    assert mod._clean_ticker(" nan ") == "NAN"

    # Real NAN holding keeps its ticker and classifies via NAN bucket, not issuer fallback
    report = mod._build_report(
        [
            {
                "Cusip": "67097U105",
                "Ticker": "NAN",
                "Issuer": "NUVEEN NY QUALITY MUN INCOME",
                "Shares": 100_000,
                "Value": 1_200_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 100_000,
                "Status": "NEW",
            }
        ],
        auto_learn=False,
    )
    assert "Other US Book" in report["categories"]
    assert mod._classify("NAN", "ALIBABA GROUP HOLDING LTD", auto_learn=False) == "Other US Book"


def test_short_name_expands_abbreviations_and_avoids_two_word_truncation() -> None:
    mod = _load_13f_module()
    mod._init_state(None)

    # Prompt verification examples (with and without corporate suffix)
    assert mod._short_name("BANK OF AMER CORP", "") == "Bank of America"
    assert mod._short_name("BANK OF AMER", "") == "Bank of America"
    assert mod._short_name("AMER SPORTS INC", "") == "Amer Sports"
    assert mod._short_name("NEW YORK TIMES CO", "") == "New York Times"
    assert mod._short_name("NEW YORK TIMES", "") == "New York Times"
    assert mod._short_name("D R HORTON INC", "") == "D.R. Horton"
    assert mod._short_name("D R HORTON", "") == "D R Horton"
    assert mod._short_name("KULICKE & SOFFA INDS INC", "") == "Kulicke & Soffa"
    assert mod._short_name("KULICKE & SOFFA", "") == "Kulicke & Soffa"
    assert mod._short_name("KKR & CO INC", "") == "KKR"
    assert mod._short_name("KKR & CO", "") == "Kkr"
    assert mod._short_name("THE TRADE DESK INC", "") == "The Trade Desk"
    assert mod._short_name("THE TRADE DESK", "") == "The Trade Desk"
    assert mod._short_name("HEWLETT PACKARD ENTERPRISE CO", "") == "Hewlett Packard Enterprise"
    assert mod._short_name("HEWLETT PACKARD ENTERPRISE", "") == "Hewlett Packard Enterprise"
    assert mod._short_name("APPLIED MATLS INC", "") == "Applied Materials"
    assert mod._short_name("APPLIED MATLS", "") == "Applied Materials"
    assert mod._short_name("TEXAS INSTRS INC", "") == "Texas Instruments"
    assert mod._short_name("TEXAS INSTRS", "") == "Texas Instruments"
    assert mod._short_name("CHARLES SCHWAB CORP (THE)", "") == "Charles Schwab"
    assert mod._short_name("INTERACTIVE BROKERS GROUP IN", "") == "Interactive Brokers"
    assert mod._short_name("INTERCONTINENTAL EXCHANGE IN", "") == "Intercontinental Exchange"
    assert mod._short_name("JEFFERIES FINANCIAL GROUP IN", "") == "Jefferies Financial"
    assert mod._short_name("NEBIUS GROUP N.V.", "") == "Nebius Group"
    assert mod._short_name("ACME ROBOTICS N.V.", "") == "Acme Robotics"
    assert mod._short_name("SEAGATE TECHNOLOGY HLDNGS PL", "") == "Seagate Technology"
    assert mod._short_name("LANVIN GROUP HOLDINGS LIMITE", "") == "Lanvin Group"
    assert mod._short_name("LANVIN LIMITE", "") == "Lanvin"
    assert mod._short_name("XANADU QUANTUM TECHNOLO LTD", "") == "Xanadu Quantum"
    assert mod._short_name("ACME HLDNGS INTERNATIONAL", "") == "Acme Holdings International"
    assert mod._short_name("ACME TECH", "") == "Acme Tech"
    assert mod._short_name("BENDING SPOONS SPA", "") == "Bending Spoons"

    # Leading NEW / DEL must not be stripped; only trailing legal noise is stripped
    assert mod._short_name("NEW FRONTIER CORP", "") == "New Frontier"
    assert mod._short_name("DEL MONTE FOODS", "") == "Del Monte Foods"
    assert mod._short_name("ACME HOLDINGS DEL", "") == "Acme"


def test_missing_ticker_row_resolves_by_issuer_name_in_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    mod._init_state(None)
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    rows = [
        {
            "Cusip": "01609W102",
            "Ticker": None,
            "Issuer": "ALIBABA GROUP HOLDING LTD",
            "Shares": 500_000,
            "Value": 50_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 500_000,
            "Status": "NEW",
        },
        {
            "Cusip": "038222105",
            "Ticker": float("nan"),
            "Issuer": "APPLIED MATLS INC",
            "Shares": 200_000,
            "Value": 40_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 200_000,
            "Status": "NEW",
        },
        # Clarivate missing-ticker row must resolve to CLVT (Clarivate), never CCC Intelligent Solutions
        {
            "Cusip": "G21810109",
            "Ticker": None,
            "Issuer": "CLARIVATE PLC",
            "Shares": 100_000,
            "Value": 10_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "12487Q109",
            "Ticker": "CCC",
            "Issuer": "CCC INTELLIGENT SOLUTIONS HL",
            "Shares": 300_000,
            "Value": 30_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 300_000,
            "Status": "NEW",
        },
        # Umbrella trust missing-ticker rows: mapped CUSIPs resolve to exact fund ticker (SMH/SOXX),
        # while unmapped CUSIPs must not guess an unrelated ETF ticker (GDXJ/USHY)
        {
            "Cusip": "92189f676",
            "Ticker": None,
            "Issuer": "VANECK ETF TRUST",
            "Shares": 50_000,
            "Value": 12_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 50_000,
            "Status": "NEW",
        },
        {
            "Cusip": "92189F999",
            "Ticker": None,
            "Issuer": "VANECK ETF TRUST",
            "Shares": 50_000,
            "Value": 12_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 50_000,
            "Status": "NEW",
        },
        {
            "Cusip": "464287999",
            "Ticker": float("nan"),
            "Issuer": "ISHARES TR",
            "Shares": 40_000,
            "Value": 11_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 40_000,
            "Status": "NEW",
        },
    ]
    filing = _make_filing(report_period="2025-12-31", manager="NoTicker Test Fund", comparison_rows=rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9997777, [filing]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9997777", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    assert res["categories"]["China Book"] == ["建仓Alibaba，5000万美元"]
    assert res["categories"]["AI/Semi/US SaaS"] == [
        "建仓Applied Materials，4000万美元",
        "建仓CCC Intelligent Solutions，3000万美元",
        "建仓VanEck Semiconductor ETF (SMH)，1200万美元",
    ]
    assert "建仓Clarivate，1000万美元" in res["categories"]["Other US Book"]
    assert "建仓Vaneck Etf Trust（CUSIP 92189F999），1200万美元" in res["categories"]["Other US Book"]
    assert "建仓Ishares Tr（CUSIP 464287999），1100万美元" in res["categories"]["Other US Book"]
    # Neither unmapped umbrella trust resolved to GDXJ / USHY
    assert all("GDXJ" not in line and "USHY" not in line for line in res["categories"]["Other US Book"])
    # Exact SEC HLDG key in name_to_ticker.json resolves cleanly without (CUSIP ...) fallback
    assert mod._lookup_ticker_by_issuer("ALIBABA GROUP HLDG LTD") == "BABA"

    # Also verify runtime uniqueness guard + dynamic multi-fund umbrella rule + derivative suffix rules
    assert mod._lookup_ticker_by_issuer("JACKSON ACQUISITION CO II") == ""
    assert mod._lookup_ticker_by_issuer("DYNAMIX CORP") == ""
    assert mod._detect_security_kind("JACSRT", "", "") == "right"
    assert mod._detect_security_kind("SYNRTS", "", "") == "right"
    assert mod._detect_security_kind("SYNWS", "", "") == "warrant"

    ws_coll = tmp_path / "ws_coll"
    state_dir = ws_coll / "state" / "dalton-13f-tracker"
    state_dir.mkdir(parents=True)
    (state_dir / "name_to_ticker.json").write_text(
        json.dumps(
            {
                "ALPHA CORP": "ZZCOLL",
                "BETA INC": "ZZCOLL",
                "SYNTHETIC SPONSOR FDS TR": "SYNA",
                "SYNTHETIC SPONSOR FDS ALPHA": "SYNB",
                "SYNTHETIC SPONSOR FDS BETA": "SYNC",
                "SYNTH RT CORP": "SYNSRT",
                "SYNTH RTS CORP": "SYNSRTS",
                "SYNTH WS CORP": "SYNSWS",
                "SYNTH W CORP": "SYNSW",
                "SYNTH U CORP": "SYNSU",
            }
        ),
        encoding="utf-8",
    )
    mod._init_state(ws_coll)
    assert mod._lookup_ticker_by_issuer("ALPHA CORP") == ""
    assert mod._lookup_ticker_by_issuer("VANECK ETF TRUST") == ""
    assert mod._lookup_ticker_by_issuer("VANGUARD ADMIRAL FDS INC") == ""
    assert mod._lookup_ticker_by_issuer("SYNTHETIC SPONSOR FDS TR") == ""
    for k_syn in ("SYNTH RT CORP", "SYNTH RTS CORP", "SYNTH WS CORP", "SYNTH W CORP", "SYNTH U CORP"):
        assert mod._lookup_ticker_by_issuer(k_syn) == ""
    mod._init_state(None)


def test_equity_and_warrant_never_merge_in_compare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    # Even if a workspace overlay tries to share a canonical name between GRAB and GRABW, they must remain separate
    ws = tmp_path / "ws_warrant_guard"
    state_dir = ws / "state" / "dalton-13f-tracker"
    state_dir.mkdir(parents=True)
    (state_dir / "merge_rules.json").write_text(
        json.dumps({"GRAB": "Grab", "GRABW": "Grab"}),
        encoding="utf-8",
    )
    mod._init_state(ws)

    rows = [
        {
            "Cusip": "G4124C109",
            "Ticker": "GRAB",
            "Issuer": "GRAB HOLDINGS LIMITED",
            "Shares": 100,
            "Value": 5_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100,
            "Status": "NEW",
        },
        {
            "Cusip": "G4124C117",
            "Ticker": "GRABW",
            "Issuer": "GRAB HOLDINGS LIMITED",
            "Shares": 0,
            "Value": 0.0,
            "PrevShares": 100,
            "PrevValue": 1_000_000.0,
            "ShareChange": -100,
            "Status": "CLOSED",
        },
        {
            "Cusip": "42806J700",
            "Ticker": "HTZ",
            "Issuer": "HERTZ GLOBAL HLDGS INC",
            "Shares": 200,
            "Value": 4_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 200,
            "Status": "NEW",
        },
        {
            "Cusip": "42806J148",
            "Ticker": "HTZWW",
            "Issuer": "HERTZ GLOBAL HLDGS INC",
            "Shares": 500,
            "Value": 2_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 500,
            "Status": "NEW",
        },
    ]
    filing = _make_filing(report_period="2026-09-30", manager="Warrant Guard Fund", comparison_rows=rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9996666, [filing]))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "compare",
            "--institution",
            "9996666",
            "--workspace-dir",
            str(ws),
            "--format",
            "cn",
            "--auto-learn",
            "false",
            "--json",
        ],
    )
    mod.main()
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    other_lines = res["categories"]["Other US Book"]
    assert "建仓Grab，500万美元" in other_lines
    assert "清仓Grab Warrants（之前持仓100万美元）" in other_lines
    assert "建仓Hertz Global，400万美元" in other_lines
    assert "建仓Hertz Global Warrants，200万美元" in other_lines
    assert all("Grab仓位不变" not in line for line in other_lines)
    mod._init_state(None)


def test_ai_hints_do_not_misclassify_non_ai_quantum_or_micron_solutions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    mod._init_state(None)
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    assert mod._infer_category_fallback("QMCO", "QUANTUM CORP") == "Other US Book"
    assert mod._infer_category_fallback("QUMSU", "QUANTUMSPHERE ACQUISITION CO") == "Other US Book"
    assert mod._infer_category_fallback("QS", "QUANTUMSCAPE CORP") == "Other US Book"
    assert mod._infer_category_fallback("QCOMP", "QUANTUM COMPUTING INC") == "AI/Semi/US SaaS"
    assert mod._infer_category_fallback("MICR", "MICRON SOLUTIONS INC") == "Other US Book"
    assert mod._infer_category_fallback("", "MICRON SOLUTIONS INC") == "Other US Book"
    assert mod._infer_category_fallback("", "MICRON TECHNOLOGY INC") == "AI/Semi/US SaaS"
    assert mod._infer_category_fallback("", "ACME GAS TURBINE CORP") == "AI/Semi/US SaaS"
    assert mod._infer_category_fallback("", "ACME FUEL CELL CORP") == "AI/Semi/US SaaS"
    assert mod._infer_category_fallback("", "ACME POWER GRID INC") == "AI/Semi/US SaaS"
    assert mod._infer_category_fallback("NGG", "NATIONAL GRID PLC") == "Other US Book"
    assert mod._infer_category_fallback("APPS", "DIGITAL TURBINE INC") == "Other US Book"
    assert mod._infer_category_fallback("", "GENERIC POWER ENERGY ELECTRIC CO") == "Other US Book"

    # Also verify via real compare -> report path including DBMF & SPWH
    rows = [
        {
            "Cusip": "59511B106",
            "Ticker": "MICR",
            "Issuer": "MICRON SOLUTIONS INC",
            "Shares": 100_000,
            "Value": 10_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "53944Y552",
            "Ticker": "DBMF",
            "Issuer": "IMGP DBI MANAGED FUTURES STRAT",
            "Shares": 100_000,
            "Value": 20_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "84920Y106",
            "Ticker": "SPWH",
            "Issuer": "SPORTSMANS WHSE HLDGS INC",
            "Shares": 100_000,
            "Value": 15_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
    ]
    filing = _make_filing(report_period="2026-09-30", manager="Classification Check Fund", comparison_rows=rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9994444, [filing]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9994444", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True
    assert set(res["categories"].keys()) == {"Other US Book"}
    assert "建仓Micron Solutions，1000万美元" in res["categories"]["Other US Book"]
    assert "建仓iMGP DBi Managed Futures ETF (DBMF)，2000万美元" in res["categories"]["Other US Book"]
    assert "建仓Sportsman's Warehouse，1500万美元" in res["categories"]["Other US Book"]


def test_json_configs_valid_no_duplicate_keys_and_no_bucket_conflicts() -> None:
    def _strict_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        out: dict[str, object] = {}
        for k, v in pairs:
            assert k not in seen, f"Duplicate JSON key: {k}"
            seen.add(k)
            out[k] = v
        return out

    for name in JSON_CONFIG_FILES:
        json.loads((SCRIPTS_DIR / name).read_text(encoding="utf-8"), object_pairs_hook=_strict_pairs)

    cls = json.loads((SCRIPTS_DIR / "classification.json").read_text(encoding="utf-8"))
    s_china = set(cls["china_book"])
    s_ai = set(cls["ai_semi_saas"])
    s_other = set(cls["other_us_book"])
    assert not (s_china & s_ai)
    assert not (s_china & s_other)
    assert not (s_ai & s_other)

    for tk in ("EDU", "LANV", "NOAH", "RDGT", "TOUR", "VIPS", "ZEPP"):
        assert tk in s_china
    for tk in (
        "GEV", "MTZ", "PWR", "ETN", "VRT", "HUBB", "POWL", "NVT", "MYRG", "FPS",
        "CEG", "VST", "BE", "FCEL", "GNRC", "INIO", "PSIX", "AGX", "PRIM",
    ):
        assert tk in s_ai
    for tk in (
        "FLS", "GE", "GVA", "AYI", "DBMF", "SPWH", "AEP", "PRE", "PRENW",
        "PLUG", "NRG", "FIX", "EME", "STRL", "IESC", "OKLO", "SMR", "NNE", "BWXT",
    ):
        assert tk in s_other
    assert "PRENW" not in s_china
    assert "AEP" not in s_ai
    assert "DBMF" not in s_china
    assert "SPWH" not in s_ai

    mr = json.loads((SCRIPTS_DIR / "merge_rules.json").read_text(encoding="utf-8"))
    assert mr["GRAB"] != mr["GRABW"]
    assert mr["HTZ"] != mr["HTZWW"]

    ntt = json.loads((SCRIPTS_DIR / "name_to_ticker.json").read_text(encoding="utf-8"))
    assert ntt["CLARIVATE PLC"] == "CLVT"
    assert ntt["GRAB HOLDINGS LIMITED"] == "GRAB"
    assert ntt["LANVIN GROUP HOLDINGS LIMITE"] == "LANV"
    assert ntt["ECARX HOLDINGS INC"] == "ECX"
    assert ntt["VINFAST AUTO LTD"] == "VFS"
    for umbrella in (
        "VANECK ETF TRUST",
        "ISHARES TR",
        "SPDR SER TR",
        "SELECT SECTOR SPDR TR",
        "VANGUARD ADMIRAL FDS INC",
        "EXCHANGE LISTED FDS TR",
    ):
        assert umbrella not in ntt
    assert "DYNAMIX CORP" not in ntt
    assert ntt["DYNAMIX CORP III"] == "DNMX"
    assert "SILVERBOX CORP" not in ntt
    assert ntt["SILVERBOX CORP IV"] == "SBXD"
    assert ntt["GAMESTOP CORP NEW"] == "GME"
    ntt_vals = set(ntt.values())
    mod = _load_13f_module()
    assert all(not mod._derivative_ticker_kind(v) for v in ntt_vals)
    for deriv in (
        "GRABW", "LANVWS", "ECXWW", "VFSWW", "AISPW", "BZAIW", "FUFUW",
        "BAERW", "WALDW", "MRNOW", "IAUXWS", "FIGXU", "JACSRT", "CELGRT", "GMEWS", "VALWS",
    ):
        assert deriv not in ntt_vals


def test_verified_tickers_canonical_short_names() -> None:
    mod = _load_13f_module()
    mod._init_state(None)
    verified = json.loads((SCRIPTS_DIR / "verified_tickers.json").read_text(encoding="utf-8"))
    mismatches: list[tuple[str, str, str, str]] = []
    for tk, info in verified.items():
        sample = str(info.get("sample_issuer") or "").strip()
        canonical = str(info.get("canonical_name") or "").strip()
        if not sample or not canonical:
            continue
        actual = mod._short_name(sample, tk)
        if actual != canonical:
            mismatches.append((tk, sample, canonical, actual))
    assert not mismatches, f"Canonical short-name mismatches: {mismatches}"
    assert mod._short_name("LANVIN GROUP HOLDINGS LIMITE", "LANV") == "Lanvin Group"
    assert mod._short_name("NEBIUS GROUP N.V.", "NBIS") == "Nebius Group"


def _default_prev_period(report_period: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-\d{2}$", (report_period or "").strip())
    if not m:
        return ""
    y, mo = int(m.group(1)), int(m.group(2))
    return {3: f"{y - 1}-12-31", 6: f"{y}-03-31", 9: f"{y}-06-30", 12: f"{y}-09-30"}.get(mo, "")


def _run_real_compare_to_file(
    *,
    mod,
    out_path: Path,
    slug: str,
    report_period: str,
    rows: list[dict],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fmt: str = "cn",
    workspace_dir: Path | None = None,
    evergreen_rows: list[dict] | None = None,
    evergreen_period: str | None = None,
    previous_period: str | None = None,
) -> dict:
    """Execute real `13f_skill.py compare` path and persist its JSON output."""
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))
    eff_prev_p = _default_prev_period(report_period) if previous_period is None else previous_period

    if slug == "hillhouse":
        f_hhlr = _make_filing(
            report_period=report_period,
            manager="HHLR Advisors, Ltd.",
            comparison_rows=rows,
            previous_period=eff_prev_p,
        )
        eg_p = evergreen_period or report_period
        f_eg = _make_filing(
            report_period=eg_p,
            manager="Evergreen Quality Fund GP Ltd",
            comparison_rows=evergreen_rows if evergreen_rows is not None else [],
            previous_period=_default_prev_period(eg_p) if previous_period is None else previous_period,
        )
        company_fn = create_autospec(_company_sig, spec_set=True)

        def _company_dispatch(cik_or_ticker):
            cik_str = str(cik_or_ticker).lstrip("0")
            if cik_str == "2016939":
                return SimpleNamespace(
                    cik=2016939,
                    get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg]),
                )
            return SimpleNamespace(
                cik=1762304,
                get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_hhlr]),
            )

        company_fn.side_effect = _company_dispatch
        monkeypatch.setattr(mod, "Company", company_fn)
        inst_arg = "HHLR"
    else:
        group_cik_map = {str(g["slug"]): str(g["cik"]) for g in mod._batch_groups()}
        cik_str = group_cik_map.get(slug, "9990100")
        cik_int = int(cik_str.lstrip("0") or "0")
        f_single = _make_filing(
            report_period=report_period,
            manager=f"{slug} Capital",
            comparison_rows=rows,
            previous_period=eff_prev_p,
        )
        monkeypatch.setattr(mod, "Company", _make_company_stub(cik_int, [f_single]))
        inst_arg = cik_str

    argv = [
        "13f_skill.py",
        "compare",
        "--institution",
        inst_arg,
        "--format",
        fmt,
        "--auto-learn",
        "false",
        "--json",
    ]
    if workspace_dir is not None:
        argv.extend(["--workspace-dir", str(workspace_dir)])
    monkeypatch.setattr(sys, "argv", argv)
    mod.main()
    raw_out = capsys.readouterr().out
    out_path.write_text(raw_out, encoding="utf-8")
    return json.loads(raw_out)


def _write_real_18_group_run(
    *,
    data_dir: Path,
    mod,
    audit_mod,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report_period: str = "2026-09-30",
    quarter_tag: str = "26q3",
    group_overrides: dict[str, list[dict]] | None = None,
    evergreen_rows: list[dict] | None = None,
    evergreen_period: str | None = None,
    fmt_overrides: dict[str, str] | None = None,
    workspace_dir: Path | None = None,
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    default_row = [
        {
            "Cusip": "722304102",
            "Ticker": "PDD",
            "Issuer": "PDD HOLDINGS INC",
            "Shares": 1_000_000,
            "Value": 150_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 1_000_000,
            "Status": "NEW",
        }
    ]
    default_eg = [
        {
            "Cusip": "01609W102",
            "Ticker": "BABA",
            "Issuer": "ALIBABA GROUP HOLDING LTD",
            "Shares": 500_000,
            "Value": 50_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 500_000,
            "Status": "NEW",
        }
    ]
    for idx, slug in audit_mod.EXPECTED_GROUPS:
        rows = (group_overrides or {}).get(slug, default_row)
        fmt = (fmt_overrides or {}).get(slug, "cn")
        _run_real_compare_to_file(
            mod=mod,
            out_path=data_dir / f"{idx:02d}_{slug}_{quarter_tag}.json",
            slug=slug,
            report_period=report_period,
            rows=rows,
            monkeypatch=monkeypatch,
            capsys=capsys,
            fmt=fmt,
            workspace_dir=workspace_dir,
            evergreen_rows=evergreen_rows if evergreen_rows is not None else default_eg,
            evergreen_period=evergreen_period,
        )


def test_audit_13f_run_end_to_end_compare_to_gate_and_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    audit_mod = _load_audit_module()
    before_bytes = {name: (SCRIPTS_DIR / name).read_bytes() for name in JSON_CONFIG_FILES}
    mod._init_state(None)

    # 1. Clean 18-group run via REAL compare -> report -> audit_13f_run.py
    # Includes: Hillhouse+Evergreen merge, real 'NAN', aliases XRS->TAL & FB->META,
    # AS/SCHW/IBKR/DBMF/SPWH/GRAB+GRABW.
    ws = tmp_path / "ws"
    ws_state = ws / "state" / "dalton-13f-tracker"
    ws_state.mkdir(parents=True)
    (ws_state / "verified_tickers.json").write_text(
        json.dumps(
            {
                "NAN": {
                    "category": "Other US Book",
                    "canonical_name": "Nuveen NY Municipal",
                    "sample_issuer": "NUVEEN NY QUALITY MUN INCOME",
                    "verified_quarter": "26Q3",
                }
            }
        ),
        encoding="utf-8",
    )

    clean_rows = [
        {"Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC", "Shares": 1_000_000, "Value": 150_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 1_000_000, "Status": "NEW"},
        {"Cusip": "67097U105", "Ticker": "NAN", "Issuer": "NUVEEN NY QUALITY MUN INCOME", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "874080104", "Ticker": "XRS", "Issuer": "TAL EDUCATION GROUP", "Shares": 200_000, "Value": 20_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 200_000, "Status": "NEW"},
        {"Cusip": "30303M102", "Ticker": "FB", "Issuer": "META PLATFORMS INC", "Shares": 100_000, "Value": 55_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "G0260P102", "Ticker": "AS", "Issuer": "AMER SPORTS INC", "Shares": 300_000, "Value": 12_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 300_000, "Status": "NEW"},
        {"Cusip": "808513105", "Ticker": "SCHW", "Issuer": "CHARLES SCHWAB CORP (THE)", "Shares": 200_000, "Value": 18_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 200_000, "Status": "NEW"},
        {"Cusip": "45841N107", "Ticker": "IBKR", "Issuer": "INTERACTIVE BROKERS GROUP IN", "Shares": 150_000, "Value": 25_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 150_000, "Status": "NEW"},
        {"Cusip": "53944Y552", "Ticker": "DBMF", "Issuer": "IMGP DBI MANAGED FUTURES STRAT", "Shares": 100_000, "Value": 14_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "84920Y106", "Ticker": "SPWH", "Issuer": "SPORTSMANS WHSE HLDGS INC", "Shares": 100_000, "Value": 11_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "G4124C109", "Ticker": "GRAB", "Issuer": "GRAB HOLDINGS LIMITED", "Shares": 100, "Value": 5_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100, "Status": "NEW"},
        {"Cusip": "G4124C117", "Ticker": "GRABW", "Issuer": "GRAB HOLDINGS LIMITED", "Shares": 0, "Value": 0.0, "PrevShares": 100, "PrevValue": 1_000_000.0, "ShareChange": -100, "Status": "CLOSED"},
        {"Cusip": "36828A101", "Ticker": "GEV", "Issuer": "GE VERNOVA INC", "Shares": 100_000, "Value": 30_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "576323109", "Ticker": "MTZ", "Issuer": "MASTEC INC", "Shares": 100_000, "Value": 22_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "093712107", "Ticker": "BE", "Issuer": "BLOOM ENERGY CORP", "Shares": 200_000, "Value": 16_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 200_000, "Status": "NEW"},
        {"Cusip": "368736104", "Ticker": "GNRC", "Issuer": "GENERAC HLDGS INC", "Shares": 100_000, "Value": 17_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "N52A8C105", "Ticker": "INIO", "Issuer": "INNIO NV", "Shares": 100_000, "Value": 15_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "73933G205", "Ticker": "PSIX", "Issuer": "POWER SOLUTIONS INTL INC", "Shares": 100_000, "Value": 12_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "04010E109", "Ticker": "AGX", "Issuer": "ARGAN INC", "Shares": 100_000, "Value": 11_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "369604301", "Ticker": "GE", "Issuer": "GE AEROSPACE", "Shares": 100_000, "Value": 28_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "34354P105", "Ticker": "FLS", "Issuer": "FLOWSERVE CORP", "Shares": 100_000, "Value": 13_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
    ]

    clean_dir = tmp_path / "clean_real_run"
    _write_real_18_group_run(
        data_dir=clean_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={"berkshire": clean_rows},
        workspace_dir=ws,
    )

    berkshire_out = json.loads((clean_dir / "18_berkshire_26q3.json").read_text(encoding="utf-8"))
    ai_lines = berkshire_out["categories"]["AI/Semi/US SaaS"]
    other_lines = berkshire_out["categories"]["Other US Book"]
    assert "建仓GE Vernova，3000万美元" in ai_lines
    assert "建仓MasTec，2200万美元" in ai_lines
    assert "建仓Bloom Energy，1600万美元" in ai_lines
    assert "建仓Generac，1700万美元" in ai_lines
    assert "建仓INNIO，1500万美元" in ai_lines
    assert "建仓Power Solutions International，1200万美元" in ai_lines
    assert "建仓Argan，1100万美元" in ai_lines
    assert "建仓GE Aerospace，2800万美元" in other_lines
    assert "建仓Flowserve，1300万美元" in other_lines

    # Run audit_13f_run.py as a fresh subprocess with HOME=/dev/null to prove no edgar cache dir is created!
    env = dict(os.environ)
    env["HOME"] = "/dev/null"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "audit_13f_run.py"),
            "--data-dir",
            str(clean_dir),
            "--quarter",
            "26Q3",
            "--workspace-dir",
            str(ws),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "RESULT: PASS" in proc.stdout
    assert "[EMPTY TICKER ROW]" not in proc.stdout
    assert "[UNVERIFIED NEW TICKERS]" not in proc.stdout

    # 2. Wrong base_period vs --quarter (all 18 files are 2026-06-30 when --quarter is 26Q3) -> non-zero
    wrong_period_dir = tmp_path / "wrong_period_run"
    _write_real_18_group_run(
        data_dir=wrong_period_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        report_period="2026-06-30",
        quarter_tag="26q3",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(wrong_period_dir), "--quarter", "26Q3"],
    )
    assert audit_mod.main() != 0
    assert "[PERIOD MISMATCH]" in capsys.readouterr().out

    # 3. Hillhouse Evergreen missing matching quarter -> merged_with has rows: 0 -> non-zero
    incomplete_merge_dir = tmp_path / "incomplete_merge_run"
    _write_real_18_group_run(
        data_dir=incomplete_merge_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        evergreen_period="2026-06-30",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(incomplete_merge_dir), "--quarter", "26Q3"],
    )
    assert audit_mod.main() != 0
    assert "[INCOMPLETE MERGE]" in capsys.readouterr().out

    # 4. Missing categories / formatted_report (--format raw) -> non-zero
    raw_dir = tmp_path / "raw_format_run"
    _write_real_18_group_run(
        data_dir=raw_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        fmt_overrides={"berkshire": "raw"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(raw_dir), "--quarter", "26Q3"],
    )
    assert audit_mod.main() != 0
    assert "[MISSING REPORT]" in capsys.readouterr().out

    # 5. Ticker-based category error caught even if short name differs from verified_tickers canonical_name
    # Misclassify COF (rendered as 'Capital One Financial' vs verified 'Capital One') into China Book via overlay
    ws_drift = tmp_path / "ws_drift"
    ws_drift_state = ws_drift / "state" / "dalton-13f-tracker"
    ws_drift_state.mkdir(parents=True)
    (ws_drift_state / "classification.json").write_text(
        json.dumps({"china_book": ["COF"]}),
        encoding="utf-8",
    )
    mod._init_state(ws_drift)
    cof_rows = [
        {
            "Cusip": "14040H105",
            "Ticker": "COF",
            "Issuer": "CAPITAL ONE FINL CORP",
            "Shares": 100_000,
            "Value": 20_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        }
    ]
    drift_dir = tmp_path / "drift_run"
    _write_real_18_group_run(
        data_dir=drift_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={"berkshire": cof_rows},
        workspace_dir=ws_drift,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(drift_dir), "--quarter", "26Q3", "--workspace-dir", str(ws_drift)],
    )
    assert audit_mod.main() != 0
    out_drift = capsys.readouterr().out
    assert "[CATEGORY DRIFT BY TICKER]" in out_drift or "[CATEGORY MISMATCH BY TICKER]" in out_drift

    # 6. Unverified ticker + bad name + --lookup-new via real yfinance stub -> non-zero with YF metadata
    mod._init_state(None)
    bad_rows = [
        {
            "Cusip": "999999100",
            "Ticker": "ZZZUNV",
            "Issuer": "ZZZ UNVERIFIED CTZN TR",
            "Shares": 100_000,
            "Value": 20_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        }
    ]
    bad_dir = tmp_path / "bad_run"
    _write_real_18_group_run(
        data_dir=bad_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={"first_beijing": bad_rows},
    )

    def _yf_ticker_sig(symbol: str) -> object:
        return None

    yf_ticker_fn = create_autospec(_yf_ticker_sig, spec_set=True)
    yf_ticker_fn.return_value = SimpleNamespace(
        info={
            "shortName": "ZZZ Unverified Corp",
            "country": "United States",
            "sector": "Industrials",
            "industry": "Specialty Industrial",
        }
    )
    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=yf_ticker_fn))
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(bad_dir), "--quarter", "26Q3", "--lookup-new"],
    )
    rc_err = audit_mod.main()
    out_err = capsys.readouterr().out
    assert rc_err != 0
    assert "[BAD NAME SUFFIX]" in out_err
    assert "[ABBREVIATED NAME]" in out_err
    assert "[UNVERIFIED NEW TICKERS]" in out_err
    assert "ZZZ Unverified Corp" in out_err
    assert "United States" in out_err
    assert "RESULT: FAIL" in out_err
    yf_ticker_fn.assert_called_once_with("ZZZUNV")

    # 7. Audit gate catches SEC-truncated half-word residuals (STX Hldngs Pl, LANV Limite, XNDU Technolo)
    trunc_dir = tmp_path / "trunc_run"
    _write_real_18_group_run(
        data_dir=trunc_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    f03 = trunc_dir / "03_aspex_26q3.json"
    d03 = json.loads(f03.read_text(encoding="utf-8"))
    d03["categories"].setdefault("AI/Semi/US SaaS", []).extend(
        [
            "建仓STX Hldngs Pl，1000万美元",
            "建仓LANV Limite，1000万美元",
            "建仓XNDU Technolo，1000万美元",
        ]
    )
    f03.write_text(json.dumps(d03, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(trunc_dir), "--quarter", "26Q3"],
    )
    assert audit_mod.main() != 0
    out_trunc = capsys.readouterr().out
    assert out_trunc.count("[BAD NAME SUFFIX]") >= 3
    assert out_trunc.count("[ABBREVIATED NAME]") >= 3

    # 8. Empty-ticker rows merge strictly by normalized uppercase CUSIP (compare -> report -> gate):
    #    Uses real edgartools compare_holdings() column shape (NO TitleOfClass injected):
    #    - lowercase 92189f676 (missing ticker) resolves via uppercase CUSIP lookup to SMH in AI/Semi/US SaaS
    #    - two unmapped empty-ticker VANECK ETF TRUST CUSIPs stay distinct with (CUSIP ...) and pass gate
    #    - same empty-ticker CUSIP with different case merges into one row with (CUSIP ...)
    #    - GRAB common + GRAB warrant (G4124C117, or unmapped G4124C999) stay separate from GRAB common
    cusip_dir = tmp_path / "cusip_empty_run"
    cusip_rows = [
        {
            "Cusip": "92189f676",
            "Ticker": "",
            "Issuer": "VANECK ETF TRUST",
            "Shares": 840_000,
            "Value": 511_560_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 840_000,
            "Status": "NEW",
        },
        {
            "Cusip": "92189F998",
            "Ticker": "",
            "Issuer": "VANECK ETF TRUST",
            "Shares": 50_000,
            "Value": 12_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 50_000,
            "Status": "NEW",
        },
        {
            "Cusip": "92189F999",
            "Ticker": None,
            "Issuer": "VANECK ETF TRUST",
            "Shares": 0,
            "Value": 0.0,
            "PrevShares": 60_000,
            "PrevValue": 9_000_000.0,
            "ShareChange": -60_000,
            "Status": "CLOSED",
        },
        {
            "Cusip": "99900a102",
            "Ticker": "",
            "Issuer": "CUSTOM EMPTY ENTITY CORP",
            "Shares": 100_000,
            "Value": 5_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "99900A102",
            "Ticker": "",
            "Issuer": "CUSTOM EMPTY ENTITY CORP",
            "Shares": 200_000,
            "Value": 10_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 200_000,
            "Status": "NEW",
        },
        {
            "Cusip": "G4124C109",
            "Ticker": "GRAB",
            "Issuer": "GRAB HOLDINGS LIMITED",
            "Shares": 100_000,
            "Value": 5_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "G4124C117",
            "Ticker": "",
            "Issuer": "GRAB HOLDINGS LIMITED",
            "Shares": 0,
            "Value": 0.0,
            "PrevShares": 100_000,
            "PrevValue": 2_000_000.0,
            "ShareChange": -100_000,
            "Status": "CLOSED",
        },
    ]
    _write_real_18_group_run(
        data_dir=cusip_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={"coatue": cusip_rows},
    )
    coatue_out = json.loads((cusip_dir / "10_coatue_26q3.json").read_text(encoding="utf-8"))
    ai_coatue = coatue_out["categories"]["AI/Semi/US SaaS"]
    assert "建仓VanEck Semiconductor ETF (SMH)，5.1亿美元" in ai_coatue
    other_lines = coatue_out["categories"]["Other US Book"]
    assert "建仓Vaneck Etf Trust（CUSIP 92189F998），1200万美元" in other_lines
    assert "清仓Vaneck Etf Trust（CUSIP 92189F999）（之前持仓900万美元）" in other_lines
    assert sum("Vaneck Etf Trust" in ln for ln in other_lines) == 2
    assert sum("Custom Empty Entity" in ln for ln in other_lines) == 1
    assert "建仓Custom Empty Entity（CUSIP 99900A102），1500万美元" in other_lines
    assert "建仓Grab，500万美元" in other_lines
    assert "清仓Grab Warrants（之前持仓200万美元）" in other_lines
    assert sum("Grab" in ln for ln in other_lines) == 2

    # Even when an unmapped empty-ticker Grab warrant CUSIP appears alone without TitleOfClass, it never shows as "清仓Grab"
    alone_grabw = _run_real_compare_to_file(
        mod=mod,
        out_path=tmp_path / "alone_grabw.json",
        slug="coatue",
        report_period="2026-09-30",
        rows=[
            {
                "Cusip": "G4124C999",
                "Ticker": "",
                "Issuer": "GRAB HOLDINGS LIMITED",
                "Shares": 0,
                "Value": 0.0,
                "PrevShares": 100_000,
                "PrevValue": 2_000_000.0,
                "ShareChange": -100_000,
                "Status": "CLOSED",
            }
        ],
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert alone_grabw["categories"]["Other US Book"] == ["清仓Grab（CUSIP G4124C999）（之前持仓200万美元）"]

    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(cusip_dir), "--quarter", "26Q3"],
    )
    rc_cusip = audit_mod.main()
    out_cusip = capsys.readouterr().out
    assert rc_cusip == 0, out_cusip
    assert "RESULT: PASS" in out_cusip

    # 9. Hillhouse gate: wrong merge entity, extra merged entity (Evergreen + Berkshire),
    #    or wrong primary CIK (Berkshire + Evergreen) must all fail with non-zero exit
    wrong_hh_dir = tmp_path / "wrong_hh_run"
    shutil.copytree(cusip_dir, wrong_hh_dir)
    f_hhlr = _make_filing(
        report_period="2026-09-30",
        manager="HHLR Advisors, Ltd.",
        previous_period="2026-06-30",
        comparison_rows=[
            {
                "Cusip": "722304102",
                "Ticker": "PDD",
                "Issuer": "PDD HOLDINGS INC",
                "Shares": 1_000_000,
                "Value": 150_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 1_000_000,
                "Status": "NEW",
            }
        ],
    )
    f_eg = _make_filing(
        report_period="2026-09-30",
        manager="Evergreen Quality Fund GP Ltd",
        previous_period="2026-06-30",
        comparison_rows=[
            {
                "Cusip": "01609W102",
                "Ticker": "BABA",
                "Issuer": "ALIBABA GROUP HOLDING LTD",
                "Shares": 200_000,
                "Value": 20_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 200_000,
                "Status": "NEW",
            }
        ],
    )
    f_brk = _make_filing(
        report_period="2026-09-30",
        manager="Berkshire Hathaway Inc",
        previous_period="2026-06-30",
        comparison_rows=[
            {
                "Cusip": "037833100",
                "Ticker": "AAPL",
                "Issuer": "APPLE INC",
                "Shares": 500_000,
                "Value": 100_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 500_000,
                "Status": "NEW",
            }
        ],
    )
    wrong_company_fn = create_autospec(_company_sig, spec_set=True)

    def _wrong_hh_dispatch(cik_or_ticker):
        c_str = str(cik_or_ticker).strip().lower().lstrip("0")
        if c_str in ("1067983", "berkshire"):
            return SimpleNamespace(
                cik=1067983,
                get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_brk]),
            )
        if c_str in ("2016939",):
            return SimpleNamespace(
                cik=2016939,
                get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg]),
            )
        return SimpleNamespace(
            cik=1762304,
            get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_hhlr]),
        )

    wrong_company_fn.side_effect = _wrong_hh_dispatch
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))
    monkeypatch.setattr(mod, "Company", wrong_company_fn)

    for bad_hh_argv in (
        ["13f_skill.py", "compare", "--institution", "HHLR", "--merge-with", "0001067983", "--format", "cn", "--auto-learn", "false", "--json"],
        ["13f_skill.py", "compare", "--institution", "HHLR", "--merge-with", "0002016939,0001067983", "--format", "cn", "--auto-learn", "false", "--json"],
        ["13f_skill.py", "compare", "--institution", "0001067983", "--merge-with", "0002016939", "--format", "cn", "--auto-learn", "false", "--json"],
        ["13f_skill.py", "compare", "--institution", "berkshire", "--merge-with", "0002016939", "--format", "cn", "--auto-learn", "false", "--json"],
    ):
        monkeypatch.setattr(sys, "argv", bad_hh_argv)
        mod.main()
        (wrong_hh_dir / "01_hillhouse_26q3.json").write_text(capsys.readouterr().out, encoding="utf-8")
        monkeypatch.setattr(
            sys,
            "argv",
            ["audit_13f_run.py", "--data-dir", str(wrong_hh_dir), "--quarter", "26Q3"],
        )
        assert audit_mod.main() != 0, f"Expected audit failure for {bad_hh_argv}"
        assert "[HILLHOUSE MERGE]" in capsys.readouterr().out

    # 10. Unified CUSIP identity across both quarters (Alphabet Codex #1 scenarios, real compare_holdings shape):
    #     (a) Case-split CUSIP rows in Q (02079k305 missing ticker + 02079K305 GOOGL) coalesce to UNCHANGED
    #     (b) Q has 02079k305 empty ticker & 02079K107 empty ticker; Q-1 has GOOGL & GOOG -> A+C merge + trend prefix
    #     (c) Reverse: Q has GOOGL, Q-1 had empty ticker on 02079k305 -> continuous add prefix still preserved
    nan = float("nan")
    f_alpha_split = _make_filing(
        report_period="2026-09-30",
        manager="Alpha Split Fund",
        comparison_rows=[
            {
                "Cusip": "02079k305",
                "Ticker": None,
                "Issuer": "ALPHABET INC",
                "Shares": 1_000_000,
                "Value": 180_000_000.0,
                "PrevShares": nan,
                "PrevValue": nan,
                "ShareChange": nan,
                "Status": "NEW",
            },
            {
                "Cusip": "02079K305",
                "Ticker": "GOOGL",
                "Issuer": "ALPHABET INC",
                "Shares": nan,
                "Value": nan,
                "PrevShares": 1_000_000,
                "PrevValue": 180_000_000.0,
                "ShareChange": nan,
                "Status": "CLOSED",
            },
        ],
    )
    monkeypatch.setattr(mod, "Company", _make_company_stub(9995501, [f_alpha_split]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9995501", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    split_res = json.loads(capsys.readouterr().out)
    assert split_res["categories"]["AI/Semi/US SaaS"] == ["Alphabet仓位不变，1.8亿美元"]
    assert len(split_res["rows"]) == 1
    assert split_res["rows"][0]["Cusip"] == "02079K305"
    assert split_res["rows"][0]["Ticker"] == "GOOGL"
    assert split_res["rows"][0]["Status"] == "UNCHANGED"

    # (b) Cross-quarter CUSIP resolution + A/C merge + continuous-add prefix preserved when Q lacks tickers
    f_ac_cur = _make_filing(
        report_period="2026-09-30",
        manager="Alpha Trend Fund",
        comparison_rows=[
            {
                "Cusip": "02079k305",
                "Ticker": "",
                "Issuer": "ALPHABET INC",
                "Shares": 1_500_000,
                "Value": 270_000_000.0,
                "PrevShares": 1_000_000,
                "PrevValue": 180_000_000.0,
                "ShareChange": 500_000,
                "Status": "INCREASED",
            },
            {
                "Cusip": "02079K107",
                "Ticker": "",
                "Issuer": "ALPHABET INC",
                "Shares": 900_000,
                "Value": 162_000_000.0,
                "PrevShares": 200_000,
                "PrevValue": 36_000_000.0,
                "ShareChange": 700_000,
                "Status": "INCREASED",
            },
        ],
    )
    f_ac_prev = _make_filing(
        report_period="2026-06-30",
        manager="Alpha Trend Fund",
        comparison_rows=[
            {
                "Cusip": "02079K305",
                "Ticker": "GOOGL",
                "Issuer": "ALPHABET INC",
                "Shares": 1_000_000,
                "Value": 180_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 1_000_000,
                "Status": "NEW",
            },
            {
                "Cusip": "02079k107",
                "Ticker": "GOOG",
                "Issuer": "ALPHABET INC",
                "Shares": 200_000,
                "Value": 36_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 200_000,
                "Status": "NEW",
            },
        ],
    )
    monkeypatch.setattr(mod, "Company", _make_company_stub(9995502, [f_ac_cur, f_ac_prev]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9995502", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    ac_res = json.loads(capsys.readouterr().out)
    assert ac_res["categories"]["AI/Semi/US SaaS"] == ["继26Q2建仓后，继续大幅加仓Alphabet至4.3亿美元，之前持仓2.2亿美元"]

    # (c) Reverse: Q has GOOGL on 02079K305, Q-1 had empty ticker on 02079k305 -> continuous-add prefix still works
    f_rev_cur = _make_filing(
        report_period="2026-09-30",
        manager="Alpha Rev Fund",
        comparison_rows=[
            {
                "Cusip": "02079K305",
                "Ticker": "GOOGL",
                "Issuer": "ALPHABET INC",
                "Shares": 2_000_000,
                "Value": 360_000_000.0,
                "PrevShares": 1_000_000,
                "PrevValue": 180_000_000.0,
                "ShareChange": 1_000_000,
                "Status": "INCREASED",
            }
        ],
    )
    f_rev_prev = _make_filing(
        report_period="2026-06-30",
        manager="Alpha Rev Fund",
        comparison_rows=[
            {
                "Cusip": "02079k305",
                "Ticker": "",
                "Issuer": "ALPHABET INC",
                "Shares": 1_000_000,
                "Value": 180_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 1_000_000,
                "Status": "NEW",
            }
        ],
    )
    monkeypatch.setattr(mod, "Company", _make_company_stub(9995503, [f_rev_cur, f_rev_prev]))
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "9995503", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    rev_res = json.loads(capsys.readouterr().out)
    assert rev_res["categories"]["AI/Semi/US SaaS"] == ["继26Q2建仓后，继续大幅加仓Alphabet至3.6亿美元，之前持仓1.8亿美元"]

    # 11. Derivative ticker (OXYWS) never merges into common stock (OXY) across repeated --auto-learn runs,
    #     and any pre-existing OXYWS->OXY alias in ticker_aliases.json is ignored on load.
    oxy_ws = tmp_path / "oxy_ws"
    oxy_state = oxy_ws / "state" / "dalton-13f-tracker"
    oxy_state.mkdir(parents=True)
    oxy_rows = [
        {
            "Cusip": "674599105",
            "Ticker": "OXY",
            "Issuer": "OCCIDENTAL PETE CORP",
            "Shares": 1_000_000,
            "Value": 50_000_000.0,
            "PrevShares": 1_000_000,
            "PrevValue": 50_000_000.0,
            "ShareChange": 0,
            "Status": "UNCHANGED",
        },
        {
            "Cusip": "674599162",
            "Ticker": "OXYWS",
            "Issuer": "OCCIDENTAL PETE CORP",
            "Shares": 300_000,
            "Value": 6_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 300_000,
            "Status": "NEW",
        },
    ]
    f_oxy = _make_filing(report_period="2026-09-30", manager="OXY Fund", comparison_rows=oxy_rows)
    monkeypatch.setattr(mod, "Company", _make_company_stub(9995511, [f_oxy]))
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    # Run 1 (auto-learn true) + Run 2 (reading workspace overlay from Run 1)
    for _ in range(2):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "13f_skill.py",
                "compare",
                "--institution",
                "9995511",
                "--workspace-dir",
                str(oxy_ws),
                "--format",
                "cn",
                "--json",
            ],
        )
        mod.main()
        oxy_res = json.loads(capsys.readouterr().out)
        assert oxy_res["categories"]["Other US Book"] == [
            "Occidental Petroleum仓位不变，5000万美元",
            "建仓Occidental Petroleum Warrants，600万美元",
        ]
    ta_file = oxy_state / "ticker_aliases.json"
    if ta_file.exists():
        assert "OXYWS" not in json.loads(ta_file.read_text(encoding="utf-8"))

    # Even if a stale overlay explicitly maps OXYWS -> OXY, _init_state ignores it
    stale_ws = tmp_path / "oxy_stale_ws"
    stale_state = stale_ws / "state" / "dalton-13f-tracker"
    stale_state.mkdir(parents=True)
    (stale_state / "ticker_aliases.json").write_text(json.dumps({"OXYWS": "OXY"}), encoding="utf-8")
    mod._init_state(stale_ws)
    assert "OXYWS" not in mod._TICKER_ALIASES
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "compare",
            "--institution",
            "9995511",
            "--workspace-dir",
            str(stale_ws),
            "--format",
            "cn",
            "--auto-learn",
            "false",
            "--json",
        ],
    )
    mod.main()
    oxy_stale_res = json.loads(capsys.readouterr().out)
    assert oxy_stale_res["categories"]["Other US Book"] == [
        "Occidental Petroleum仓位不变，5000万美元",
        "建仓Occidental Petroleum Warrants，600万美元",
    ]
    mod._init_state(None)

    # 12. Convertible bonds without ticker (H WORLD GROUP LTD / 44332NAB2, BILIBILI INC / 090040AF3)
    #     classify via issuer into China Book, keep CUSIP merge key & CB label, and pass audit gate.
    cb_dir = tmp_path / "cb_china_run"
    cb_rows = [
        {
            "Cusip": "44332NAB2",
            "Ticker": None,
            "Issuer": "H WORLD GROUP LTD",
            "Shares": 30_000_000,
            "Value": 30_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 30_000_000,
            "Status": "NEW",
        },
        {
            "Cusip": "090040AF3",
            "Ticker": "",
            "Issuer": "BILIBILI INC",
            "Shares": 20_000_000,
            "Value": 20_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 20_000_000,
            "Status": "NEW",
        },
        {
            "Cusip": "44332N106",
            "Ticker": "HTHT",
            "Issuer": "H WORLD GROUP LTD",
            "Shares": 500_000,
            "Value": 18_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 500_000,
            "Status": "NEW",
        },
    ]
    _write_real_18_group_run(
        data_dir=cb_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={"aspex": cb_rows},
    )
    aspex_cb = json.loads((cb_dir / "03_aspex_26q3.json").read_text(encoding="utf-8"))
    china_cb = aspex_cb["categories"]["China Book"]
    assert "建仓H World CB（CUSIP 44332NAB2），3000万美元" in china_cb
    assert "建仓Bilibili CB（CUSIP 090040AF3），2000万美元" in china_cb
    assert "建仓H World，1800万美元" in china_cb
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(cb_dir), "--quarter", "26Q3"],
    )
    assert audit_mod.main() == 0, capsys.readouterr().out
    _ = capsys.readouterr()

    # 13. Merge key never uses issuer short name: two VanEck ETFs (GDX 92189f106 + GDXJ 92189f791)
    #     with identical issuer "VANECK ETF TRUST" stay separate and render distinct names;
    #     across any report, distinct tickers never share the same rendered display name.
    mod._init_state(None)
    vaneck_rows = [
        {
            "Cusip": "92189f106",
            "Ticker": None,
            "Issuer": "VANECK ETF TRUST",
            "Shares": 100_000,
            "Value": 7_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 100_000,
            "Status": "NEW",
        },
        {
            "Cusip": "92189f791",
            "Ticker": None,
            "Issuer": "VANECK ETF TRUST",
            "Shares": 0,
            "Value": 0.0,
            "PrevShares": 100_000,
            "PrevValue": 9_000_000.0,
            "ShareChange": -100_000,
            "Status": "CLOSED",
        },
    ]
    vaneck_unverified = _run_real_compare_to_file(
        mod=mod,
        out_path=tmp_path / "vaneck_unverified.json",
        slug="coatue",
        report_period="2026-09-30",
        rows=vaneck_rows,
        monkeypatch=monkeypatch,
        capsys=capsys,
    )
    assert vaneck_unverified["categories"]["Other US Book"] == [
        "建仓Vaneck Etf Trust (GDX)，700万美元",
        "清仓Vaneck Etf Trust (GDXJ)（之前持仓900万美元）",
    ]

    # Also verify general invariant: distinct non-merged tickers sharing raw short name get distinct display names
    multi_same_short = mod._build_report(
        vaneck_rows
        + [
            {
                "Cusip": "111111101",
                "Ticker": "ACMEA",
                "Issuer": "ACME ALPHA CORP",
                "Shares": 10_000,
                "Value": 2_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 10_000,
                "Status": "NEW",
            },
            {
                "Cusip": "222222102",
                "Ticker": "ACMEB",
                "Issuer": "ACME ALPHA INC",
                "Shares": 20_000,
                "Value": 3_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 20_000,
                "Status": "NEW",
            },
        ],
        auto_learn=False,
    )
    all_bullets = [b for blist in multi_same_short["categories"].values() for b in blist]
    extracted_names = [audit_mod.extract_name_from_line(b) for b in all_bullets]
    assert len(all_bullets) == 4
    assert len(set(extracted_names)) == len(all_bullets)

    # Full compare -> report -> audit gate with GDX & GDXJ registered in verified_tickers.json overlay
    gdx_ws = tmp_path / "gdx_ws"
    gdx_state = gdx_ws / "state" / "dalton-13f-tracker"
    gdx_state.mkdir(parents=True)
    (gdx_state / "verified_tickers.json").write_text(
        json.dumps(
            {
                "GDX": {
                    "category": "Other US Book",
                    "canonical_name": "VanEck Gold Miners ETF (GDX)",
                    "sample_issuer": "VANECK ETF TRUST",
                    "verified_quarter": "26Q3",
                },
                "GDXJ": {
                    "category": "Other US Book",
                    "canonical_name": "VanEck Junior Gold Miners ETF (GDXJ)",
                    "sample_issuer": "VANECK ETF TRUST",
                    "verified_quarter": "26Q3",
                },
            }
        ),
        encoding="utf-8",
    )
    gdx_dir = tmp_path / "gdx_verified_run"
    _write_real_18_group_run(
        data_dir=gdx_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={"coatue": vaneck_rows},
        workspace_dir=gdx_ws,
    )
    coatue_gdx = json.loads((gdx_dir / "10_coatue_26q3.json").read_text(encoding="utf-8"))
    gdx_lines = coatue_gdx["categories"]["Other US Book"]
    assert gdx_lines == [
        "建仓VanEck Gold Miners ETF (GDX)，700万美元",
        "清仓VanEck Junior Gold Miners ETF (GDXJ)（之前持仓900万美元）",
    ]
    gdx_names = [audit_mod.extract_name_from_line(b) for b in gdx_lines]
    assert len(set(gdx_names)) == len(gdx_lines)
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(gdx_dir), "--quarter", "26Q3", "--workspace-dir", str(gdx_ws)],
    )
    assert audit_mod.main() == 0, capsys.readouterr().out
    _ = capsys.readouterr()

    # Negative check: if GDX+GDXJ were wrongly collapsed into one "Vaneck Etf Trust仓位不变", gate rejects it
    bad_gdx_dir = tmp_path / "gdx_collapsed_run"
    shutil.copytree(gdx_dir, bad_gdx_dir)
    bad_f10 = bad_gdx_dir / "10_coatue_26q3.json"
    bad_d10 = json.loads(bad_f10.read_text(encoding="utf-8"))
    bad_d10["categories"]["Other US Book"] = ["Vaneck Etf Trust仓位不变，700万美元"]
    bad_d10["formatted_report"] = "**Other US Book**\n- Vaneck Etf Trust仓位不变，700万美元"
    bad_f10.write_text(json.dumps(bad_d10, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(bad_gdx_dir), "--quarter", "26Q3", "--workspace-dir", str(gdx_ws)],
    )
    assert audit_mod.main() != 0
    assert "[MISSING VERIFIED RENDER]" in capsys.readouterr().out

    # 14. Reused ticker guard (Codex #8 Simplify ETF vs Figma on ticker FIG):
    #     Simplify Macro Strategy ETF (82889n715 / 82889N715, SIMPLIFY EXCHANGE TRADED FUN)
    #     must NOT inherit Figma's verified canonical name or AI/Semi/US SaaS bucket;
    #     must render with its own issuer short name + (FIG) in Other US Book and pass audit gate.
    mod._init_state(None)
    simplify_dir = tmp_path / "simplify_fig_run"
    simplify_rows = [
        {
            "Cusip": "82889n715",
            "Ticker": None,
            "Issuer": "SIMPLIFY EXCHANGE TRADED FUN",
            "Shares": 103_358,
            "Value": 2_157_423.0,
            "PrevShares": 443_873,
            "PrevValue": 10_120_304.0,
            "ShareChange": -340_515,
            "Status": "DECREASED",
        },
        {
            "Cusip": "316841105",
            "Ticker": "FIG",
            "Issuer": "FIGMA INC",
            "Shares": 500_000,
            "Value": 30_000_000.0,
            "PrevShares": 0,
            "PrevValue": 0.0,
            "ShareChange": 500_000,
            "Status": "NEW",
        },
    ]
    _write_real_18_group_run(
        data_dir=simplify_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        report_period="2024-06-30",
        quarter_tag="24q2",
        group_overrides={"coatue": simplify_rows},
    )
    coatue_fig = json.loads((simplify_dir / "10_coatue_24q2.json").read_text(encoding="utf-8"))
    assert coatue_fig["categories"]["AI/Semi/US SaaS"] == ["建仓Figma，3000万美元"]
    assert coatue_fig["categories"]["Other US Book"] == ["减仓Simplify Exchange Traded Funds (FIG) 77%至200万美元"]
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(simplify_dir), "--quarter", "24Q2"],
    )
    assert audit_mod.main() == 0, capsys.readouterr().out
    _ = capsys.readouterr()

    # Verify recorded CUSIP comparison is uppercase-normalized and blocks same-issuer CUSIP mismatch
    assert mod._ledger_entry_matches("INIO", "n52a8c105", "INNIO NV") is True
    assert mod._ledger_entry_matches("INIO", "N99999105", "INNIO NV") is False
    inio_mismatch = mod._build_report(
        [
            {
                "Cusip": "n99999105",
                "Ticker": "INIO",
                "Issuer": "INNIO NV",
                "Shares": 100_000,
                "Value": 15_000_000.0,
                "PrevShares": 0,
                "PrevValue": 0.0,
                "ShareChange": 100_000,
                "Status": "NEW",
            }
        ],
        auto_learn=False,
    )
    assert inio_mismatch["categories"]["Other US Book"] == ["建仓Innio (INIO)，1500万美元"]

    # Built-in JSON files remain completely untouched
    mod._init_state(None)
    for name in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / name).read_bytes() == before_bytes[name]


def test_same_cusip_wins_over_a_renamed_issuer():
    """A CUSIP match is the same security even after the issuer renames
    (QIFU TECHNOLOGY -> QFIN HOLDINGS); a reused ticker on a different CUSIP
    still does not borrow the ledger entry."""
    mod = _load_13f_module()
    mod._init_state(None)
    assert mod._ledger_entry_matches("QFIN", "88557W101", "QIFU TECHNOLOGY INC")
    assert mod._classify("QFIN", "QIFU TECHNOLOGY INC", cusip="88557w101") == "China Book"
    assert not mod._ledger_entry_matches("QFIN", "99999X999", "QIFU TECHNOLOGY INC")


# ---------------------------------------------------------------------------
# V2 Acceptance Tests: descriptors, aggregate, render-batch, verify-numbers
# ---------------------------------------------------------------------------


def test_format_value_promotes_rounding_boundary_to_yi() -> None:
    """Single-rounding unit threshold: >=99.5M promotes to 1.0亿美元; <99.5M rounds once with ROUND_HALF_UP to 万美元."""
    mod = _load_13f_module()
    assert mod._format_value(98_499_999.0) == "9800万美元"
    assert mod._format_value(98_500_000.0) == "9900万美元"
    assert mod._format_value(99_497_000.0) == "9900万美元"
    assert mod._format_value(99_499_999.0) == "9900万美元"
    assert mod._format_value(99_500_000.0) == "1.0亿美元"
    assert mod._format_value(99_600_000.0) == "1.0亿美元"
    assert mod._format_value(99_400_000.0) == "9900万美元"
    assert mod._format_value(100_000_000.0) == "1.0亿美元"
    assert mod._format_value(1_234_000_000.0) == "12.3亿美元"
    assert mod._format_value(900_000.0) == "100万美元"


def test_descriptors_json_and_institution_groups_contract() -> None:
    desc_cfg = json.loads((SCRIPTS_DIR / "descriptors.json").read_text(encoding="utf-8"))
    major = set(desc_cfg["major_bluechips"])
    familiar = set(desc_cfg["familiar_names"])
    descriptors = desc_cfg["descriptors"]

    assert 15 <= len(major) <= 30
    assert len(familiar) >= 100
    for tk in ("WAB", "MAIR", "Q", "INIO", "VICR", "RVTY", "BSP", "LSCC", "SITM", "SMTC", "VIAV"):
        assert tk in descriptors, f"Missing descriptor for {tk}"
    for tk, text in descriptors.items():
        assert 4 <= len(text) <= 24, f"Descriptor length out of range for {tk}: '{text}' ({len(text)})"

    inst_cfg = json.loads((SCRIPTS_DIR / "institution_map.json").read_text(encoding="utf-8"))
    assert inst_cfg.get("yiheng capital") == "0001447228"
    groups = inst_cfg.get("_groups")
    assert isinstance(groups, list) and len(groups) == 18
    assert groups[0]["slug"] == "hillhouse"
    assert groups[7]["slug"] == "yiheng" and groups[7]["display_name"] == "Yiheng Capital"
    assert groups[8]["slug"] == "tiger_global"
    assert groups[17]["slug"] == "berkshire"


def test_v2_end_to_end_aggregate_render_descriptors_and_verify_numbers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_13f_module()
    audit_mod = _load_audit_module()
    before_bytes = {name: (SCRIPTS_DIR / name).read_bytes() for name in JSON_CONFIG_FILES}

    ws = tmp_path / "ws_v2"
    ws_state = ws / "state" / "dalton-13f-tracker"
    ws_state.mkdir(parents=True)
    # Register new unverified equities ZETAC + ZETAL so audit passes and descriptor workflow triggers
    (ws_state / "verified_tickers.json").write_text(
        json.dumps(
            {
                "ZETAC": {
                    "category": "Other US Book",
                    "canonical_name": "Zeta NewCorp",
                    "sample_issuer": "ZETA NEWCORP INC",
                    "verified_quarter": "26Q3",
                    "cusip": "999888101",
                },
                "ZETAL": {
                    "category": "Other US Book",
                    "canonical_name": "Zeta LowCorp",
                    "sample_issuer": "ZETA LOWCORP INC",
                    "verified_quarter": "26Q3",
                    "cusip": "999888102",
                },
            }
        ),
        encoding="utf-8",
    )
    # Coverage file in workspace dalton/coverage.yaml (live position + core coverage)
    cov_dir = ws / "dalton"
    cov_dir.mkdir(parents=True)
    (cov_dir / "coverage.yaml").write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {"ticker": "ATAT", "name": "亚朵", "tier": "core", "sheet_section": "Live Position"},
                    {"ticker": "DHI", "name": "D.R. Horton", "tier": "watch", "sheet_section": "Live Position"},
                    {"ticker": "IBKR", "name": "Interactive Brokers", "tier": "core", "sheet_section": "US Financial"},
                    {"ticker": "ECHO", "name": "EchoStar", "tier": "watch", "sheet_section": "Live Position"},
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    mod._init_state(ws)

    # Construct 18-group fixture exercising:
    # - 99.6M boundary rounding -> 1.0亿美元 (Tiger Global Seagate)
    # - Major bluechip exclusion from clue pool (META +50% 400M) vs non-bluechip inclusion (Q, LSCC, INIO)
    # - INNIO shares doubled (1M -> 2M) while market value stayed flat at 270M in Aspex (weight >2%, net_buy=135M)
    # - Biotech row included in SEC total denominator (MRNA)
    # - Live/core coverage with small $1M holding (DHI), net add (ATAT, IBKR), and 0-holding exclusion (ECHO)
    # - Same-fund large sells (Aspex closed GLW 380M; D1 trimmed SHOP 120M)
    # - Descriptor first-appearance (WAB in Hillhouse, Q & LSCC in D1, BSP skipped via annotated-tickers, ZETAC missing->added, ZETAL <5M skipped)
    hillhouse_rows = [
        {"Cusip": "929740108", "Ticker": "WAB", "Issuer": "WESTINGHOUSE AIR BRAKE TECH", "Shares": 1_200_000, "Value": 240_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 1_200_000, "Status": "NEW"},
        {"Cusip": "30303M102", "Ticker": "META", "Issuer": "META PLATFORMS INC", "Shares": 1_000_000, "Value": 600_000_000.0, "PrevShares": 500_000, "PrevValue": 300_000_000.0, "ShareChange": 500_000, "Status": "INCREASED"},
        {"Cusip": "60937P106", "Ticker": "MRNA", "Issuer": "MODERNA INC", "Shares": 2_000_000, "Value": 1_000_000_000.0, "PrevShares": 2_000_000, "PrevValue": 1_000_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
    ]
    evergreen_rows = [
        {"Cusip": "04962L101", "Ticker": "ATAT", "Issuer": "ATOUR LIFESTYLE HOLDINGS LTD", "Shares": 1_000_000, "Value": 40_000_000.0, "PrevShares": 500_000, "PrevValue": 20_000_000.0, "ShareChange": 500_000, "Status": "INCREASED"},
    ]
    aspex_rows = [
        # INNIO: shares doubled 1M -> 2M, market value stayed 270M -> net_buy = 1M * 135 = 135M, weight = 270M / 1.5B = 18% >= 2%
        {"Cusip": "N52A8C105", "Ticker": "INIO", "Issuer": "INNIO NV", "Shares": 2_000_000, "Value": 270_000_000.0, "PrevShares": 1_000_000, "PrevValue": 270_000_000.0, "ShareChange": 1_000_000, "Status": "INCREASED"},
        {"Cusip": "219350105", "Ticker": "GLW", "Issuer": "CORNING INC", "Shares": 0, "Value": 0.0, "PrevShares": 4_000_000, "PrevValue": 380_000_000.0, "ShareChange": -4_000_000, "Status": "CLOSED"},
        {"Cusip": "30303M102", "Ticker": "META", "Issuer": "META PLATFORMS INC", "Shares": 0, "Value": 0.0, "PrevShares": 400_000, "PrevValue": 200_000_000.0, "ShareChange": -400_000, "Status": "CLOSED"},
        {"Cusip": "929740108", "Ticker": "WAB", "Issuer": "WESTINGHOUSE AIR BRAKE TECH", "Shares": 500_000, "Value": 100_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 500_000, "Status": "NEW"},
        {"Cusip": "999888101", "Ticker": "ZETAC", "Issuer": "ZETA NEWCORP INC", "Shares": 100_000, "Value": 12_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
        {"Cusip": "999888102", "Ticker": "ZETAL", "Issuer": "ZETA LOWCORP INC", "Shares": 50_000, "Value": 3_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 50_000, "Status": "NEW"},
    ]
    tiger_rows = [
        # 99.6M -> must format as 1.0亿美元
        {"Cusip": "G7945M107", "Ticker": "STX", "Issuer": "SEAGATE TECHNOLOGY HLDNGS PL", "Shares": 380_000, "Value": 99_600_000.0, "PrevShares": 1_000_000, "PrevValue": 250_000_000.0, "ShareChange": -620_000, "Status": "DECREASED"},
        {"Cusip": "N13904108", "Ticker": "BSP", "Issuer": "BENDING SPOONS SPA", "Shares": 400_000, "Value": 20_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 400_000, "Status": "NEW"},
        {"Cusip": "30303M102", "Ticker": "META", "Issuer": "META PLATFORMS INC", "Shares": 500_000, "Value": 300_000_000.0, "PrevShares": 500_000, "PrevValue": 280_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
    ]
    d1_rows = [
        {"Cusip": "74736K101", "Ticker": "Q", "Issuer": "QNITY ELECTRONICS INC", "Shares": 6_000_000, "Value": 640_000_000.0, "PrevShares": 1_000_000, "PrevValue": 140_000_000.0, "ShareChange": 5_000_000, "Status": "INCREASED"},
        {"Cusip": "518415104", "Ticker": "LSCC", "Issuer": "LATTICE SEMICONDUCTOR CORP", "Shares": 5_600_000, "Value": 770_000_000.0, "PrevShares": 1_000_000, "PrevValue": 170_000_000.0, "ShareChange": 4_600_000, "Status": "INCREASED"},
        {"Cusip": "82509L107", "Ticker": "SHOP", "Issuer": "SHOPIFY INC", "Shares": 500_000, "Value": 50_000_000.0, "PrevShares": 1_900_000, "PrevValue": 190_000_000.0, "ShareChange": -1_400_000, "Status": "DECREASED"},
        {"Cusip": "999888101", "Ticker": "ZETAC", "Issuer": "ZETA NEWCORP INC", "Shares": 200_000, "Value": 24_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 200_000, "Status": "NEW"},
    ]
    lone_pine_rows = [
        {"Cusip": "45841N107", "Ticker": "IBKR", "Issuer": "INTERACTIVE BROKERS GROUP IN", "Shares": 3_700_000, "Value": 370_000_000.0, "PrevShares": 500_000, "PrevValue": 50_000_000.0, "ShareChange": 3_200_000, "Status": "INCREASED"},
    ]
    durable_rows = [
        {"Cusip": "74736K101", "Ticker": "Q", "Issuer": "QNITY ELECTRONICS INC", "Shares": 5_000_000, "Value": 550_000_000.0, "PrevShares": 5_260_000, "PrevValue": 580_000_000.0, "ShareChange": -260_000, "Status": "DECREASED"},
        {"Cusip": "N13904108", "Ticker": "BSP", "Issuer": "BENDING SPOONS SPA", "Shares": 1_340_000, "Value": 670_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 1_340_000, "Status": "NEW"},
    ]
    berkshire_rows = [
        {"Cusip": "23331A109", "Ticker": "DHI", "Issuer": "D R HORTON INC", "Shares": 10_000, "Value": 1_000_000.0, "PrevShares": 10_000, "PrevValue": 1_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
        {"Cusip": "N13904108", "Ticker": "BSP", "Issuer": "BENDING SPOONS SPA", "Shares": 1_380_000, "Value": 690_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 1_380_000, "Status": "NEW"},
    ]

    data_dir = tmp_path / "run_v2"
    _write_real_18_group_run(
        data_dir=data_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={
            "hillhouse": hillhouse_rows,
            "aspex": aspex_rows,
            "tiger_global": tiger_rows,
            "d1_capital": d1_rows,
            "lone_pine": lone_pine_rows,
            "durable": durable_rows,
            "berkshire": berkshire_rows,
        },
        evergreen_rows=evergreen_rows,
        workspace_dir=ws,
    )

    # 1) aggregate
    agg_path = tmp_path / "agg_26q3.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "aggregate",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--out",
            str(agg_path),
        ],
    )
    mod.main()
    agg = json.loads(capsys.readouterr().out)
    assert agg["ok"] is True
    assert agg["coverage_missing"] is False
    assert agg["portfolio_and_coverage_reconciliation"]["coverage_missing"] is False

    # Held vs closed counts separated cleanly for META: Hillhouse(+1), Tiger(unchanged) -> held_count=2; Aspex(closed) -> closed_count=1
    ai_matrix = {e["name"]: e for e in agg["sector_consensus_matrix"]["AI/Semi/US SaaS"]}
    meta_ent = ai_matrix["Meta"]
    assert meta_ent["held_count"] == 2
    assert meta_ent["closed_count"] == 1
    assert meta_ent["buy_count"] == 1
    assert meta_ent["sell_count"] == 1
    assert meta_ent["total_end_value_usd"] == pytest.approx(900_000_000.0)

    # Portfolio & coverage reconciliation: ATAT & IBKR net_add, DHI $1M net_trim_or_flat, ECHO excluded (0 holdings)
    cov_rec = agg["portfolio_and_coverage_reconciliation"]
    add_tickers = {x["coverage_ticker"] for x in cov_rec["net_add"]}
    flat_tickers = {x["coverage_ticker"] for x in cov_rec["net_trim_or_flat"]}
    assert {"ATAT", "IBKR"} <= add_tickers
    assert "DHI" in flat_tickers
    assert "ECHO" not in (add_tickers | flat_tickers)

    # New clue candidates: META excluded (major_bluechip); INNIO included (net_buy = 135M via share delta * end_price, weight >= 2%)
    cand_by_tk = {c["ticker"]: c for c in agg["new_clue_candidates"]}
    assert "META" not in cand_by_tk
    assert "INIO" in cand_by_tk
    inio_trig = cand_by_tk["INIO"]["triggers"][0]
    assert inio_trig["fund_slug"] == "aspex"
    assert inio_trig["net_buy_usd"] == pytest.approx(135_000_000.0)
    assert inio_trig["net_share_increase_multiple"] == pytest.approx(1.0)
    assert any(s["ticker"] == "GLW" for s in inio_trig["same_fund_large_sells"])

    # Hillhouse SEC total includes biotech MRNA ($1.0B + $0.6B + $0.24B + $0.04B = $1.88B) -> WAB weight = 240/1880 = 12.77%
    hh_summary = next(f for f in agg["funds"] if f["slug"] == "hillhouse")
    assert hh_summary["total_value_usd"] == pytest.approx(1_880_000_000.0)
    # Q candidate attaches ALL 18-group holdings (both D1 and Durable)
    assert len(cand_by_tk["Q"]["all_18_group_holdings"]) == 2
    d1_q_trig = cand_by_tk["Q"]["triggers"][0]
    assert any(s["ticker"] == "SHOP" for s in d1_q_trig["same_fund_large_sells"])

    # 2) Draft Chapters 1-2 with exact, approximate, and sum numbers + one intentional mismatch
    chapters_path = tmp_path / "chapters_1_2.md"
    chapters_path.write_text(
        "\n".join([
            "# 13F Tracker_2026Q3",
            "",
            "## 核心结论与跨机构动向",
            "",
            "1. **科技、半导体与企业软件（AI/Semi/US SaaS）**：",
            "   - **Meta**（2 家持有，1 家买入、1 家清仓，期末合计 9.0 亿美元）。",
            "",
            "2. **中概股（China Book）**：",
            "   - **PDD** 获多家机构持有。",
            "",
            "3. **Other US Book**：",
            "   - **意大利订阅应用软件并购整合平台 Bending Spoons** 获 Durable（6.7 亿美元）、Berkshire（6.9 亿美元）与 Tiger Global（2000 万美元）建仓。",
            "",
            "4. **实盘持仓与重点覆盖标的**：",
            "   - **净增持**：Interactive Brokers（Lone Pine 大幅加仓至 3.7 亿美元）。",
            "   - **净减持或持平**：D.R. Horton（Berkshire 维持 100 万美元不变）。",
            "",
            "## 持仓变化中的新线索",
            "",
            "- **Qnity Electronics**：D1 将 Qnity 股数猛加 5 倍至 6.4 亿美元（加上 Durable 的 5.5 亿，两家合计近 12 亿）。",
            "- **INNIO**：Aspex 股数翻倍（持仓 2.7 亿，净增 1.35 亿美元）。",
            "- **故意错误测试**：D1 建仓 Lattice 9.9 亿美元。",
        ]),
        encoding="utf-8",
    )

    # 3) First render-batch: detects ZETAC missing descriptor (ZETAL is <5M so ignored)
    report_path = tmp_path / "report_26q3.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "render-batch",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--annotated-tickers",
            "BSP",
            "--chapters-file",
            str(chapters_path),
            "--out",
            str(report_path),
        ],
    )
    mod.main()
    rb1 = json.loads(capsys.readouterr().out)
    assert rb1["status"] == "needs_descriptors"
    missing_tks = [m["ticker"] for m in rb1["missing_descriptors"]]
    assert missing_tks == ["ZETAC"]

    # 4) descriptors-add writes drafted descriptor to workspace overlay ONLY (builtin untouched)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "descriptors-add",
            "--quarter",
            "26Q3",
            "--items-json",
            json.dumps({"ZETAC": "测试新型工业软件商"}, ensure_ascii=False),
        ],
    )
    mod.main()
    add_res = json.loads(capsys.readouterr().out)
    assert add_res["ok"] is True
    assert add_res["added_count"] == 1
    assert add_res["summary_line"] == "本季新增定语 1 条"

    # 5) Re-run render-batch -> status ok, clean 5-section Markdown
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "render-batch",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--annotated-tickers",
            "BSP",
            "--chapters-file",
            str(chapters_path),
            "--out",
            str(report_path),
        ],
    )
    mod.main()
    rb2 = json.loads(capsys.readouterr().out)
    assert rb2["status"] == "ok"
    assert rb2["missing_count"] == 0
    assert rb2["new_descriptors_line"] == "本季新增定语 1 条"

    final_md = report_path.read_text(encoding="utf-8")
    # 5-section structure & pure markdown (no escapes, no 10000万美元)
    for h in (
        "# 13F Tracker_2026Q3",
        "## 核心结论与跨机构动向",
        "## 持仓变化中的新线索",
        "## China Book 机构（8 组 / 9 申报主体）",
        "## US Growth 机构（9 组）",
        "## Value 机构（1 组）",
        "### 1. Hillhouse 高瓴（HHLR + Evergreen 合并） 26Q3 vs 26Q2（13F 市值 18.8亿美元",
        "### 8. Yiheng Capital 26Q3 vs 26Q2（13F 市值 ",
        "### 18. Berkshire Hathaway 伯克希尔·哈撒韦 26Q3 vs 26Q2（13F 市值 ",
    ):
        assert h in final_md
    assert "\\_" not in final_md and "\\." not in final_md and "\\+" not in final_md
    assert "10000万美元" not in final_md
    assert "减仓Seagate Technology 62%至1.0亿美元" in final_md

    # Descriptor first-appearance & dedup checks:
    # - WAB gets descriptor in #1 Hillhouse, NOT repeated in #3 Aspex
    assert "建仓北美货运铁路机车与轨交制动设备龙头Wabtec，2.4亿美元" in final_md
    assert "建仓Wabtec，1.0亿美元" in final_md
    # - ZETAC gets drafted descriptor in #3 Aspex, NOT repeated in #11 D1
    assert "建仓测试新型工业软件商Zeta NewCorp，1200万美元" in final_md
    assert "建仓Zeta NewCorp，2400万美元" in final_md
    # - BSP was in --annotated-tickers -> NOT embedded in Tiger, Durable, or Berkshire
    assert "建仓Bending Spoons，2000万美元" in final_md
    assert "建仓Bending Spoons，6.7亿美元" in final_md
    assert "建仓Bending Spoons，6.9亿美元" in final_md
    assert final_md.count("意大利订阅应用软件并购整合平台") == 1  # only in Ch1-2

    # 6) verify-numbers: non-blocking, honors approx & sums, catches the 9.9亿 Lattice mismatch
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "verify-numbers",
            "--aggregate",
            str(agg_path),
            "--report-file",
            str(report_path),
        ],
    )
    mod.main()
    vn = json.loads(capsys.readouterr().out)
    assert vn["ok"] is True
    assert vn["blocking"] is False
    # 12亿 (11.9亿 actual) with "近" should actively match (neither mismatched nor unresolved);
    # 9.9亿 Lattice (7.7亿 actual) must be flagged
    assert any("9.9 亿美元" in m["claimed"] for m in vn["mismatches"])
    assert not any("12 亿" in m["claimed"] for m in vn["mismatches"])
    assert not any("12 亿" in u["claimed"] for u in vn["unresolved"])

    # 7) audit_13f_run.py with --report-file passes
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_13f_run.py",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--workspace-dir",
            str(ws),
            "--report-file",
            str(report_path),
        ],
    )
    assert audit_mod.main() == 0
    assert "RESULT: PASS" in capsys.readouterr().out

    # Built-in JSON files remain completely untouched
    mod._init_state(None)
    for name in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / name).read_bytes() == before_bytes[name]


def test_v2_codex_fixes_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression coverage for Codex review findings #1-#5, #7, and #8."""
    mod = _load_13f_module()
    audit_mod = _load_audit_module()

    # 1. Share-class merge preserves reported PrevValue regardless of row order
    mod._init_state(None)
    row_existing = {
        "Cusip": "02079K305",
        "Ticker": "GOOGL",
        "Issuer": "ALPHABET INC",
        "Shares": 100,
        "Value": 20_000.0,
        "PrevShares": 50,
        "PrevValue": 8_000.0,
        "ShareChange": 50,
        "Status": "INCREASED",
    }
    row_new_no_prev = {
        "Cusip": "02079K107",
        "Ticker": "GOOG",
        "Issuer": "ALPHABET INC",
        "Shares": 20,
        "Value": 3_920.0,
        "PrevShares": 0,
        "PrevValue": 0.0,
        "ShareChange": 20,
        "Status": "NEW",
    }
    prep_a = mod._prepare_fund_items([row_existing, row_new_no_prev], auto_learn=False)
    prep_b = mod._prepare_fund_items([row_new_no_prev, row_existing], auto_learn=False)
    item_a = prep_a["categories"]["AI/Semi/US SaaS"][0]
    item_b = prep_b["categories"]["AI/Semi/US SaaS"][0]
    assert item_a["prev_value"] == pytest.approx(8_000.0)
    assert item_b["prev_value"] == pytest.approx(8_000.0)
    assert item_a["text"] == item_b["text"]

    # Setup clean 18-group fixture for #2, #3, #4, #5, #7, #8
    ws = tmp_path / "ws_codex"
    ws_state = ws / "state" / "dalton-13f-tracker"
    ws_state.mkdir(parents=True)
    # #7: Workspace overlay removes SMTC from familiar_names / major_bluechips and removes WAB descriptor?
    # Keep WAB descriptor; remove NVDA from major_bluechips and remove SMTC from familiar_names
    (ws_state / "descriptors.json").write_text(
        json.dumps({"remove": ["NVDA", "SMTC"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (ws_state / "verified_tickers.json").write_text(
        json.dumps(
            {
                "UNIF": {
                    "category": "Other US Book",
                    "canonical_name": "Unified Co",
                    "sample_issuer": "UNIFIED CO",
                    "verified_quarter": "26Q3",
                    "cusip": "888777101",
                },
                "EDGEA": {
                    "category": "Other US Book",
                    "canonical_name": "EdgePct Co",
                    "sample_issuer": "EDGEPCT CO",
                    "verified_quarter": "26Q3",
                    "cusip": "888777102",
                },
                "EDGEB": {
                    "category": "Other US Book",
                    "canonical_name": "EdgeWt Co",
                    "sample_issuer": "EDGEWT CO",
                    "verified_quarter": "26Q3",
                    "cusip": "888777103",
                },
            }
        ),
        encoding="utf-8",
    )
    cov_file = tmp_path / "cov.yaml"
    cov_file.write_text(
        yaml.safe_dump({"companies": [{"ticker": "PDD", "name": "PDD", "tier": "core"}]}, allow_unicode=True),
        encoding="utf-8",
    )
    mod._init_state(ws)
    assert "NVDA" not in mod._MAJOR_BLUECHIPS
    assert "SMTC" not in mod._FAMILIAR_NAMES

    # #3: Raw precision thresholds:
    # - EDGEA: +19.96% shares (1000 -> 1199.6? use integer 10_000 -> 11_996), weight 3.0%, net_buy < 150M -> MUST NOT enter pool
    # - EDGEB: +30% shares, weight 1.996% (<2%), net_buy < 150M -> MUST NOT enter pool
    # - #4: UNIF has Ticker="UNIF" in Hillhouse and Ticker="" in First Beijing on same CUSIP 888777101 -> unified across funds!
    # - #7: WAB is UNCHANGED in Hillhouse (no descriptor), NEW >=5M in Aspex (gets descriptor on Aspex!)
    # - #8: Tiger Global DECREASED STX -62% (end=99.6M, prev=250M)
    hillhouse_rows = [
        {"Cusip": "888777101", "Ticker": "UNIF", "Issuer": "UNIFIED CO", "Shares": 1_000_000, "Value": 30_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 1_000_000, "Status": "NEW"},
        {"Cusip": "929740108", "Ticker": "WAB", "Issuer": "WESTINGHOUSE AIR BRAKE TECH", "Shares": 100_000, "Value": 20_000_000.0, "PrevShares": 100_000, "PrevValue": 20_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
        {"Cusip": "888777102", "Ticker": "EDGEA", "Issuer": "EDGEPCT CO", "Shares": 11_996, "Value": 30_000_000.0, "PrevShares": 10_000, "PrevValue": 25_000_000.0, "ShareChange": 1_996, "Status": "INCREASED"},
        # Total value of Hillhouse: 30M + 20M + 30M + 19.96M + 900.04M = 1000.0M -> EDGEB weight = 19.96 / 1000 = 1.996%
        {"Cusip": "888777103", "Ticker": "EDGEB", "Issuer": "EDGEWT CO", "Shares": 1_300_000, "Value": 19_960_000.0, "PrevShares": 1_000_000, "PrevValue": 15_000_000.0, "ShareChange": 300_000, "Status": "INCREASED"},
        {"Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC", "Shares": 9_000_400, "Value": 900_040_000.0, "PrevShares": 9_000_400, "PrevValue": 900_040_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
    ]
    first_beijing_rows = [
        # Missing ticker on same CUSIP 888777101
        {"Cusip": "888777101", "Ticker": "", "Issuer": "UNIFIED CO", "Shares": 500_000, "Value": 25_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 500_000, "Status": "NEW"},
    ]
    aspex_rows = [
        {"Cusip": "929740108", "Ticker": "WAB", "Issuer": "WESTINGHOUSE AIR BRAKE TECH", "Shares": 500_000, "Value": 100_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 500_000, "Status": "NEW"},
    ]
    tiger_rows = [
        {"Cusip": "G7945M107", "Ticker": "STX", "Issuer": "SEAGATE TECHNOLOGY HLDNGS PL", "Shares": 380_000, "Value": 99_600_000.0, "PrevShares": 1_000_000, "PrevValue": 250_000_000.0, "ShareChange": -620_000, "Status": "DECREASED"},
    ]

    data_dir = tmp_path / "run_codex"
    _write_real_18_group_run(
        data_dir=data_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={
            "hillhouse": hillhouse_rows,
            "first_beijing": first_beijing_rows,
            "aspex": aspex_rows,
            "tiger_global": tiger_rows,
        },
        evergreen_rows=[
            {"Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC", "Shares": 1_000, "Value": 100_000.0, "PrevShares": 1_000, "PrevValue": 100_000.0, "ShareChange": 0, "Status": "UNCHANGED"}
        ],
        workspace_dir=ws,
    )

    # Check #3 & #4 via aggregate with valid --coverage
    agg_path = tmp_path / "agg_codex.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "aggregate",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--coverage",
            str(cov_file),
            "--out",
            str(agg_path),
        ],
    )
    mod.main()
    agg = json.loads(capsys.readouterr().out)
    assert agg["coverage_missing"] is False
    cand_tks = {c["ticker"] for c in agg["new_clue_candidates"]}
    assert "EDGEA" not in cand_tks
    assert "EDGEB" not in cand_tks
    assert "UNIF" in cand_tks
    unif_cand = next(c for c in agg["new_clue_candidates"] if c["ticker"] == "UNIF")
    assert unif_cand["held_count"] == 2
    assert len(unif_cand["all_18_group_holdings"]) == 2
    assert {h["fund_slug"] for h in unif_cand["all_18_group_holdings"]} == {"hillhouse", "first_beijing"}

    # Check #5: Missing coverage file reports coverage_missing=True explicitly
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "aggregate",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--coverage",
            str(tmp_path / "does_not_exist.yaml"),
        ],
    )
    mod.main()
    agg_miss = json.loads(capsys.readouterr().out)
    assert agg_miss["coverage_missing"] is True
    assert agg_miss["coverage_missing_reason"]
    assert agg_miss["portfolio_and_coverage_reconciliation"]["coverage_missing"] is True

    # Check #7: WAB is UNCHANGED in Hillhouse -> gets descriptor in Aspex (first qualifying NEW/>=5M)
    ch_path = tmp_path / "ch_codex.md"
    ch_path.write_text(
        "\n".join([
            "# 13F Tracker_2026Q3",
            "",
            "## 核心结论与跨机构动向",
            "",
            "- Tiger Global 加仓 Seagate +62% 至 2.5 亿美元。",
            "",
            "## 持仓变化中的新线索",
            "",
            "- 本季无新线索。",
        ]),
        encoding="utf-8",
    )
    rpt_path = tmp_path / "rpt_codex.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "render-batch",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--chapters-file",
            str(ch_path),
            "--out",
            str(rpt_path),
        ],
    )
    mod.main()
    rb = json.loads(capsys.readouterr().out)
    assert rb["ok"] is True
    rpt_md = rpt_path.read_text(encoding="utf-8")
    assert "Wabtec仓位不变，2000万美元" in rpt_md
    assert "建仓北美货运铁路机车与轨交制动设备龙头Wabtec，1.0亿美元" in rpt_md

    # Check #8: verify-numbers catches BOTH wrong direction (+62% vs -62%) AND wrong period (2.5亿 prev vs 9960万 end)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "verify-numbers",
            "--aggregate",
            str(agg_path),
            "--report-file",
            str(rpt_path),
        ],
    )
    mod.main()
    vn = json.loads(capsys.readouterr().out)
    assert vn["mismatch_count"] >= 2
    claimed_list = [m["claimed"] for m in vn["mismatches"]]
    assert any("62%" in c for c in claimed_list)
    assert any("2.5 亿美元" in c for c in claimed_list)

    # Check #2a: Chapters quarter mismatch (2026Q2 vs 26Q3) fails both render-batch and audit_13f_run.py
    bad_ch = tmp_path / "ch_bad_q.md"
    bad_ch.write_text("# 13F Tracker_2026Q2\n\n## 核心结论与跨机构动向\n\n## 持仓变化中的新线索\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="does not match"):
        mod.cmd_render_batch(
            SimpleNamespace(
                data_dir=str(data_dir),
                quarter="26Q3",
                annotated_tickers="",
                chapters_file=str(bad_ch),
                include_biotech=False,
                out="",
                json=True,
            )
        )
    bad_rpt = tmp_path / "rpt_bad_q.md"
    bad_rpt.write_text(rpt_md.replace("# 13F Tracker_2026Q3", "# 13F Tracker_2026Q2"), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_13f_run.py",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--workspace-dir",
            str(ws),
            "--report-file",
            str(bad_rpt),
        ],
    )
    assert audit_mod.main() != 0
    assert "[REPORT QUARTER MISMATCH]" in capsys.readouterr().out

    # Check #2b: Wrong institution CIK in 02_first_beijing_26q3.json fails aggregate/render-batch AND audit_13f_run.py
    bad_cik_dir = tmp_path / "bad_cik_dir"
    shutil.copytree(data_dir, bad_cik_dir)
    f02 = bad_cik_dir / "02_first_beijing_26q3.json"
    d02 = json.loads(f02.read_text(encoding="utf-8"))
    d02["cik"] = "0001067983"
    d02["institution"] = "0001067983"
    f02.write_text(json.dumps(d02, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Institution CIK mismatch"):
        mod._load_batch_runs(bad_cik_dir, "26Q3")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(bad_cik_dir), "--quarter", "26Q3", "--workspace-dir", str(ws)],
    )
    assert audit_mod.main() != 0
    assert "[INSTITUTION CIK MISMATCH]" in capsys.readouterr().out

    mod._init_state(None)


def test_v2_codex_round4_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression coverage for Codex round-4 re-review items #1-#7."""
    mod = _load_13f_module()
    audit_mod = _load_audit_module()

    # Setup workspace with list removals, CUSIP-less FIG entry, and CUSIP-bearing INIO entry
    ws = tmp_path / "ws_r4"
    ws_state = ws / "state" / "dalton-13f-tracker"
    ws_state.mkdir(parents=True)
    (ws_state / "descriptors.json").write_text(
        json.dumps(
            {
                "remove": ["NVDA", "SMTC"],
                "remove_major_bluechips": ["TSLA"],
                "descriptors": {"EXST": "已有主营业务定语公司"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (ws_state / "verified_tickers.json").write_text(
        json.dumps(
            {
                "FIG": {
                    "category": "AI/Semi/US SaaS",
                    "canonical_name": "Figma",
                    "sample_issuer": "FIGMA INC",
                    "verified_quarter": "26Q3",
                },
                "EXST": {
                    "category": "Other US Book",
                    "canonical_name": "Existing Co",
                    "sample_issuer": "EXISTING CO",
                    "verified_quarter": "26Q3",
                    "cusip": "333444109",
                },
            }
        ),
        encoding="utf-8",
    )
    mod._init_state(ws)

    # #1 (CUSIP-only ledger match when vt has cusip; issuer match when vt lacks cusip)
    assert mod._ledger_entry_matches("EXST", "999999999", "EXISTING CO") is False
    assert mod._ledger_entry_matches("EXST", "333444109", "COMPLETELY DIFFERENT NAME") is True
    assert mod._ledger_entry_matches("FIG", "31680Q104", "FIGMA INC") is True

    # #5: descriptors-add preserves remove / remove_major_bluechips / existing descriptors
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "descriptors-add",
            "--quarter",
            "26Q3",
            "--items-json",
            json.dumps({"NEWR": "新增高精度工业传感器厂商"}, ensure_ascii=False),
        ],
    )
    mod.main()
    da = json.loads(capsys.readouterr().out)
    assert da["ok"] is True
    saved_overlay = json.loads((ws_state / "descriptors.json").read_text(encoding="utf-8"))
    assert saved_overlay["remove"] == ["NVDA", "SMTC"]
    assert saved_overlay["remove_major_bluechips"] == ["TSLA"]
    assert saved_overlay["descriptors"]["EXST"] == "已有主营业务定语公司"
    assert saved_overlay["descriptors"]["NEWR"] == "新增高精度工业传感器厂商"
    mod._init_state(ws)
    assert "NVDA" not in mod._MAJOR_BLUECHIPS
    assert "TSLA" not in mod._MAJOR_BLUECHIPS
    assert "SMTC" not in mod._FAMILIAR_NAMES

    # Build 18-group directory testing:
    # - #1a: FIGMA (CUSIP 31680Q104) has Ticker="FIG"+"FIGMA INC" in Hillhouse and Ticker=""+"RENAMED SHELL CORP" in Aspex
    #        -> MUST stay unified into 1 entity (held_count == 2), NOT split!
    # - #1b: Prior quarter in First Beijing had CUSIP 333444109 (EXST) with empty ticker ("cusip|333444109|equity": "NEW"),
    #        while Aspex supplies Ticker="EXST" in Q3 -> First Beijing MUST STILL keep "继26Q2建仓后，继续"!
    # - #3:  Hillhouse file has historical prev_quarter_label="26Q1", render-batch heading MUST use calendar "26Q3 vs 26Q2"
    # - #6:  Exact 1.5亿 float trap: value=350_000_000.0, shares=49, prev_shares=28, chg_shares=21
    #        (in IEEE-754 float: 350_000_000.0 / 49 * 21 == 149999999.99999997 < 1.5e8, Decimal == exact 150,000,000!)
    #        with weight < 2% (fund total 30B) -> MUST enter new_clue_candidates!
    # - #7:  Multi-fund prev sum check + explicit minus with buy verb conflict check
    data_dir = tmp_path / "batch_r4"
    data_dir.mkdir(parents=True)
    groups = mod._batch_groups()
    for g in groups:
        idx = int(g["index"])
        slug = g["slug"]
        rows = [
            {
                "Cusip": "037833100",
                "Ticker": "AAPL",
                "Issuer": "APPLE INC",
                "Shares": 1_000_000,
                "Value": 200_000_000.0,
                "PrevShares": 1_000_000,
                "PrevValue": 200_000_000.0,
                "ShareChange": 0,
                "Status": "UNCHANGED",
            }
        ]
        extra: dict[str, Any] = {}
        if slug == "hillhouse":
            rows = [
                # Cash anchor >=30B so EXST weight <2%
                {"Cusip": "037833100", "Ticker": "AAPL", "Issuer": "APPLE INC", "Shares": 100_000_000, "Value": 30_000_000_000.0, "PrevShares": 100_000_000, "PrevValue": 30_000_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
                {"Cusip": "31680Q104", "Ticker": "FIG", "Issuer": "FIGMA INC", "Shares": 1_000_000, "Value": 40_000_000.0, "PrevShares": 1_000_000, "PrevValue": 80_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
                {"Cusip": "333444109", "Ticker": "EXST", "Issuer": "EXISTING CO", "Shares": 49, "Value": 350_000_000.0, "PrevShares": 28, "PrevValue": 200_000_000.0, "ShareChange": 21, "Status": "INCREASED"},
            ]
            extra["merged_with"] = [
                {"institution": "0002016939", "base_period": "2026-09-30", "previous_period": "2026-06-30", "rows": 1}
            ]
            extra["prev_quarter_label"] = "26Q1"
            extra["prior_actions"] = {"ticker|AAPL|equity": "加仓"}
        elif slug == "first_beijing":
            rows = [
                {"Cusip": "333444109", "Ticker": "", "Issuer": "EXISTING CO", "Shares": 200_000, "Value": 20_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 100_000, "Status": "INCREASED"},
            ]
            extra["prior_actions"] = {"cusip|333444109|equity": "建仓"}
        elif slug == "aspex":
            rows = [
                {"Cusip": "31680Q104", "Ticker": "", "Issuer": "RENAMED SHELL CORP", "Shares": 500_000, "Value": 20_000_000.0, "PrevShares": 500_000, "PrevValue": 40_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
                {"Cusip": "333444109", "Ticker": "EXST", "Issuer": "EXISTING CO", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"},
            ]
        elif slug == "tiger_global":
            rows = [
                {"Cusip": "G7945M107", "Ticker": "STX", "Issuer": "SEAGATE TECHNOLOGY HLDGS", "Shares": 380_000, "Value": 99_600_000.0, "PrevShares": 1_000_000, "PrevValue": 250_000_000.0, "ShareChange": -620_000, "Status": "DECREASED"},
            ]

        prep = mod._prepare_fund_items(
            rows,
            exclude_biotech=True,
            auto_learn=False,
            prior_actions=mod._deserialize_prior_actions(extra.get("prior_actions")),
            prev_quarter_label=str(extra.get("prev_quarter_label") or "26Q2"),
        )
        cats_fmt = {
            k: [f"- {it['text']}" for it in v]
            for k, v in prep["categories"].items()
            if v
        }
        payload = {
            "ok": True,
            "institution": str(g["cik"]),
            "cik": str(g["cik"]),
            "base_period": "2026-09-30",
            "previous_period": "2026-06-30",
            "rows": rows,
            "categories": cats_fmt,
            "formatted_report": "stub",
            **extra,
        }
        (data_dir / f"{idx:02d}_{slug}_26q3.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # #4a: Without user coverage file, aggregate MUST return coverage_missing=True (no repo static fallback!)
    agg_nocov_path = tmp_path / "agg_r4_nocov.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "aggregate",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--out",
            str(agg_nocov_path),
        ],
    )
    mod.main()
    agg_nocov = json.loads(capsys.readouterr().out)
    assert agg_nocov["coverage_missing"] is True

    # #4b: Coverage file with ONLY tier=watch is NOT missing and reconciles EXST
    watch_cov = tmp_path / "cov_watch.yaml"
    watch_cov.write_text(
        yaml.safe_dump({"companies": [{"ticker": "EXST", "name": "Existing Co", "tier": "watch"}]}, allow_unicode=True),
        encoding="utf-8",
    )
    agg_path = tmp_path / "agg_r4.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "aggregate",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--coverage",
            str(watch_cov),
            "--out",
            str(agg_path),
        ],
    )
    mod.main()
    agg = json.loads(capsys.readouterr().out)
    assert agg["coverage_missing"] is False
    rec_all = agg["portfolio_and_coverage_reconciliation"]["net_add"] + agg["portfolio_and_coverage_reconciliation"]["net_trim_or_flat"]
    assert any(r["coverage_ticker"] == "EXST" and r["is_watch"] is True for r in rec_all)

    # #1a check: Figma stayed unified in 1 entry with held_count == 2
    ai_entries = [e for e in agg["sector_consensus_matrix"]["AI/Semi/US SaaS"] if e["ticker"] == "FIG"]
    assert len(ai_entries) == 1
    assert ai_entries[0]["held_count"] == 2
    assert ai_entries[0]["total_end_value_usd"] == pytest.approx(60_000_000.0)

    # #6 check: EXST entered new_clue_candidates at exact 1.5亿 despite float 149999999.99999997
    clue_exst = [c for c in agg["new_clue_candidates"] if c["ticker"] == "EXST"]
    assert len(clue_exst) == 1
    assert any("1.5亿" in r for t in clue_exst[0]["triggers"] for r in t["trigger_reasons"])

    # #3 & #1b check in render-batch:
    # - Hillhouse subheading is 26Q3 vs 26Q2 (NOT 26Q1!)
    # - First Beijing keeps 继26Q2建仓后，继续 prefix on EXST even after CUSIP unification!
    ch_path = tmp_path / "ch_r4.md"
    ch_path.write_text(
        (
            "# 13F Tracker_2026Q3\n\n"
            "## 核心结论与跨机构动向\n\n"
            "### AI/Semi/US SaaS\n"
            "- **Hillhouse + Aspex 合计持有 Figma**：上期合计 1.2 亿美元，期末合计 6000 万美元，错标上期合计 6000 万美元。\n"
            "- **Tiger Global 加仓 Seagate -62%**：方向矛盾测试。\n\n"
            "## 持仓变化中的新线索\n\n"
            "- 无。\n"
        ),
        encoding="utf-8",
    )
    rpt_path = tmp_path / "rpt_r4.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "render-batch",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--chapters-file",
            str(ch_path),
            "--out",
            str(rpt_path),
        ],
    )
    mod.main()
    rb = json.loads(capsys.readouterr().out)
    assert rb["ok"] is True
    rpt_md = rpt_path.read_text(encoding="utf-8")
    assert "### 1. Hillhouse 高瓴（HHLR + Evergreen 合并） 26Q3 vs 26Q2" in rpt_md
    # Hillhouse's stored prior actions are labelled 26Q1, not the 26Q2 this
    # comparison starts from, so its items carry no trend prefix at all.
    assert "继26Q1" not in rpt_md
    assert "大幅加仓已有主营业务定语公司Existing Co至3.5亿美元" in rpt_md
    assert "继26Q2建仓后，继续大幅加仓Existing Co至2000万美元" in rpt_md

    # #7 check: verify-numbers flags action+sign conflict AND wrong prev-sum (6000万美元), while accepting 1.2亿美元
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py",
            "--workspace-dir",
            str(ws),
            "--json",
            "verify-numbers",
            "--aggregate",
            str(agg_path),
            "--report-file",
            str(rpt_path),
        ],
    )
    mod.main()
    vn = json.loads(capsys.readouterr().out)
    reasons = [m["reason"] for m in vn["mismatches"]]
    assert "动作词与负号方向冲突" in reasons
    assert any(m["claimed"] == "6000 万美元" and "prev" in m["reason"] for m in vn["mismatches"])
    assert not any(m["claimed"] == "1.2 亿美元" for m in vn["mismatches"])

    # #3 audit_13f_run check: bad detail prev quarter "26Q3 vs 26Q1" triggers [REPORT QUARTER MISMATCH]
    bad_rpt = tmp_path / "rpt_bad_detail_q.md"
    bad_rpt.write_text(
        rpt_md.replace(
            "### 1. Hillhouse 高瓴（HHLR + Evergreen 合并） 26Q3 vs 26Q2",
            "### 1. Hillhouse 高瓴（HHLR + Evergreen 合并） 26Q3 vs 26Q1",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "audit_13f_run.py",
            "--data-dir",
            str(data_dir),
            "--quarter",
            "26Q3",
            "--workspace-dir",
            str(ws),
            "--report-file",
            str(bad_rpt),
        ],
    )
    assert audit_mod.main() != 0
    assert "[REPORT QUARTER MISMATCH]" in capsys.readouterr().out

    # #2a check: Non-Hillhouse non-empty merged_with fails both _load_batch_runs AND audit_13f_run.py
    bad_merge_dir = tmp_path / "bad_merge_dir"
    shutil.copytree(data_dir, bad_merge_dir)
    f02_m = bad_merge_dir / "02_first_beijing_26q3.json"
    d02_m = json.loads(f02_m.read_text(encoding="utf-8"))
    d02_m["merged_with"] = [{"institution": "0001067983", "rows": 1}]
    f02_m.write_text(json.dumps(d02_m, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unexpected merged_with"):
        mod._load_batch_runs(bad_merge_dir, "26Q3")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(bad_merge_dir), "--quarter", "26Q3", "--workspace-dir", str(ws)],
    )
    assert audit_mod.main() != 0
    assert "[UNEXPECTED MERGE]" in capsys.readouterr().out

    # #2b check: Matching cik with conflicting institution name fails _load_batch_runs directly
    bad_inst_dir = tmp_path / "bad_inst_dir"
    shutil.copytree(data_dir, bad_inst_dir)
    f02_i = bad_inst_dir / "02_first_beijing_26q3.json"
    d02_i = json.loads(f02_i.read_text(encoding="utf-8"))
    d02_i["institution"] = "berkshire"
    f02_i.write_text(json.dumps(d02_i, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Institution field mismatch"):
        mod._load_batch_runs(bad_inst_dir, "26Q3")

    mod._init_state(None)


def test_v2_codex_round5_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression coverage for Codex round-5 re-review items #1-#7."""
    mod = _load_13f_module()
    audit_mod = _load_audit_module()

    # #6: Raw share-ratio action thresholds (2.5% -> 加仓, 50.5% -> 大幅加仓 + descriptor eligibility)
    mod._init_state(None)
    assert mod._detect_action(1000, 25) == "加仓"
    assert "加仓Foo 3%至" in mod._change_template("Foo", 10_000_000.0, 1000, 25, 9_500_000.0)
    assert mod._detect_action(1000, 505) == "大幅加仓"
    assert "大幅加仓Foo至" in mod._change_template("Foo", 10_000_000.0, 1000, 505, 6_600_000.0)

    # #2: Prior actions lookup is strictly CUSIP-based (no ticker fallback on reused ticker FIG)
    simplify_rows = [
        {
            "Cusip": "82889N103",
            "Ticker": "FIG",
            "Issuer": "SIMPLIFY VOLATILITY ETF",
            "Shares": 200_000,
            "Value": 20_000_000.0,
            "PrevShares": 100_000,
            "PrevValue": 10_000_000.0,
            "ShareChange": 100_000,
            "Status": "INCREASED",
        }
    ]
    prep_reuse = mod._prepare_fund_items(
        simplify_rows,
        auto_learn=False,
        prior_actions={("ticker", "FIG", "equity"): "加仓", ("cusip", "31680Q104", "equity"): "加仓"},
        prev_quarter_label="26Q2",
    )
    reuse_items = prep_reuse["categories"]["Other US Book"]
    assert len(reuse_items) == 1
    assert reuse_items[0]["trend_prefix"] == ""

    # #3: Order-independent representative decides CUSIP category & display name
    row_alpha = {
        "Cusip": "999111222",
        "Ticker": "",
        "Issuer": "ALPHA INDUSTRIAL HOLDINGS CORP",
        "Shares": 100_000,
        "Value": 10_000_000.0,
        "PrevShares": 100_000,
        "PrevValue": 10_000_000.0,
        "ShareChange": 0,
        "Status": "UNCHANGED",
    }
    row_zebra_ai = {
        "Cusip": "999111222",
        "Ticker": "",
        "Issuer": "ZEBRA AI CLOUD SOFTWARE CORP",
        "Shares": 100_000,
        "Value": 10_000_000.0,
        "PrevShares": 100_000,
        "PrevValue": 10_000_000.0,
        "ShareChange": 0,
        "Status": "UNCHANGED",
    }
    p_fwd = mod._prepare_fund_items([row_alpha, row_zebra_ai], auto_learn=False)
    p_rev = mod._prepare_fund_items([row_zebra_ai, row_alpha], auto_learn=False)
    assert p_fwd["categories"] == p_rev["categories"]

    # #1 (cmd_compare): saves previous_period & refuses Hillhouse merge when Evergreen previous_period mismatches
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    df_main = pd.DataFrame([row_alpha])
    comp_hhlr = SimpleNamespace(data=df_main, previous_period="2026-06-30")
    cmp_fn_hhlr = create_autospec(_compare_holdings_sig, spec_set=True, return_value=comp_hhlr)
    tf_hhlr = SimpleNamespace(report_period="2026-09-30", management_company_name="HHLR Advisors, Ltd.", compare_holdings=cmp_fn_hhlr)
    f_hhlr = SimpleNamespace(obj=create_autospec(_filing_obj_sig, spec_set=True, return_value=tf_hhlr))

    df_eg = pd.DataFrame([row_zebra_ai])
    comp_eg_bad_prev = SimpleNamespace(data=df_eg, previous_period="2026-03-31")
    cmp_fn_eg = create_autospec(_compare_holdings_sig, spec_set=True, return_value=comp_eg_bad_prev)
    tf_eg = SimpleNamespace(report_period="2026-09-30", management_company_name="Evergreen Quality Fund GP Ltd", compare_holdings=cmp_fn_eg)
    f_eg = SimpleNamespace(obj=create_autospec(_filing_obj_sig, spec_set=True, return_value=tf_eg))

    comp_cls = create_autospec(_company_sig, spec_set=True)
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg if "2016939" in str(c) else f_hhlr]),
    )
    monkeypatch.setattr(mod, "Company", comp_cls)
    monkeypatch.setattr(
        sys,
        "argv",
        ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    cmp_out = json.loads(capsys.readouterr().out)
    assert cmp_out["ok"] is False
    assert cmp_out["previous_period"] == "2026-06-30"
    assert cmp_out["merged_with"][0]["previous_period"] == "2026-03-31"
    assert cmp_out["merged_with"][0]["rows"] == 0
    assert "上期报告期不一致" in (cmp_out["merged_with"][0]["note"] or "")
    assert "formatted_report" not in cmp_out

    # Setup 18-group fixture for #1 (batch), #3 (cross-fund), #4, #5, #6, #7
    ws = tmp_path / "ws_r5"
    ws_state = ws / "state" / "dalton-13f-tracker"
    ws_state.mkdir(parents=True)
    (ws_state / "descriptors.json").write_text(
        json.dumps({"descriptors": {"FOO5": "高增长功率电源芯片供应商"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    mod._init_state(ws)

    data_dir = tmp_path / "batch_r5"
    data_dir.mkdir(parents=True)
    groups = mod._batch_groups()
    for g in groups:
        idx = int(g["index"])
        slug = g["slug"]
        rows = [
            {"Cusip": "037833100", "Ticker": "AAPL", "Issuer": "APPLE INC", "Shares": 1_000_000, "Value": 200_000_000.0, "PrevShares": 1_000_000, "PrevValue": 200_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
        ]
        extra: dict[str, Any] = {}
        if slug == "hillhouse":
            rows.extend([
                {"Cusip": "31680Q104", "Ticker": "FIG", "Issuer": "FIGMA INC", "Shares": 1_000_000, "Value": 40_000_000.0, "PrevShares": 1_000_000, "PrevValue": 40_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
                {"Cusip": "555666701", "Ticker": "FOO5", "Issuer": "FOO FIVE CORP", "Shares": 1_505, "Value": 10_000_000.0, "PrevShares": 1_000, "PrevValue": 6_000_000.0, "ShareChange": 505, "Status": "INCREASED"},
                {"Cusip": "999111222", "Ticker": "", "Issuer": "ZEBRA AI CLOUD SOFTWARE CORP", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
            ])
            extra["merged_with"] = [{"institution": "0002016939", "base_period": "2026-09-30", "previous_period": "2026-06-30", "rows": 1}]
        elif slug == "aspex":
            rows.extend([
                {"Cusip": "31680Q104", "Ticker": "FIG", "Issuer": "FIGMA INC", "Shares": 1_000_000, "Value": 20_000_000.0, "PrevShares": 1_000_000, "PrevValue": 20_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
                {"Cusip": "999111222", "Ticker": "", "Issuer": "ALPHA INDUSTRIAL HOLDINGS CORP", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
            ])
        elif slug == "coatue":
            rows.append(
                {"Cusip": "31680Q104", "Ticker": "FIG", "Issuer": "FIGMA INC", "Shares": 1_500_000, "Value": 30_000_000.0, "PrevShares": 1_000_000, "PrevValue": 20_000_000.0, "ShareChange": 500_000, "Status": "INCREASED"},
            )
        elif slug == "tiger_global":
            rows.append(
                {"Cusip": "G7945M107", "Ticker": "STX", "Issuer": "SEAGATE TECHNOLOGY HLDGS", "Shares": 750_000, "Value": 100_000_000.0, "PrevShares": 1_000_000, "PrevValue": 150_000_000.0, "ShareChange": -250_000, "Status": "DECREASED"},
            )

        prep = mod._prepare_fund_items(rows, exclude_biotech=True, auto_learn=False)
        cats_fmt = {k: [f"- {it['text']}" for it in v] for k, v in prep["categories"].items() if v}
        payload = {
            "ok": True,
            "institution": str(g["cik"]),
            "cik": str(g["cik"]),
            "base_period": "2026-09-30",
            "previous_period": "2026-06-30",
            "rows": rows,
            "categories": cats_fmt,
            "formatted_report": "stub",
            **extra,
        }
        (data_dir / f"{idx:02d}_{slug}_26q3.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # #5: Unreadable/invalid YAML coverage returns coverage_missing=True; legal companies: [] is NOT missing
    bad_yaml = tmp_path / "bad_cov.yaml"
    bad_yaml.write_text("companies: [unclosed\n", encoding="utf-8")
    _, _, bad_reason = mod._load_coverage_roster(ws, str(bad_yaml))
    assert bad_reason is not None

    bad_utf8 = tmp_path / "bad_utf8.yaml"
    bad_utf8.write_bytes(b"\xff\xfe\xfd\x80")
    _, _, utf8_reason = mod._load_coverage_roster(ws, str(bad_utf8))
    assert utf8_reason is not None

    empty_cov = tmp_path / "empty_cov.yaml"
    empty_cov.write_text(yaml.safe_dump({"companies": []}), encoding="utf-8")
    agg_empty_path = tmp_path / "agg_empty.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py", "--workspace-dir", str(ws), "--json", "aggregate",
            "--data-dir", str(data_dir), "--quarter", "26Q3", "--coverage", str(empty_cov), "--out", str(agg_empty_path),
        ],
    )
    mod.main()
    agg = json.loads(capsys.readouterr().out)
    assert agg["coverage_missing"] is False
    # #3 check across funds: CUSIP 999111222 uses lexicographically earliest representative ("ALPHA...") -> Other US Book regardless
    cusip_rep_entries = [e for e in agg["sector_consensus_matrix"]["Other US Book"] if "Alpha Industrial" in e["name"]]
    assert len(cusip_rep_entries) == 1
    assert cusip_rep_entries[0]["held_count"] == 2

    swap_dir = tmp_path / "batch_r5_swap"
    shutil.copytree(data_dir, swap_dir)
    for fn, old_iss, new_iss in (
        ("01_hillhouse_26q3.json", "ZEBRA AI CLOUD SOFTWARE CORP", "ALPHA INDUSTRIAL HOLDINGS CORP"),
        ("03_aspex_26q3.json", "ALPHA INDUSTRIAL HOLDINGS CORP", "ZEBRA AI CLOUD SOFTWARE CORP"),
    ):
        fp = swap_dir / fn
        fp.write_text(fp.read_text(encoding="utf-8").replace(old_iss, new_iss), encoding="utf-8")
    agg_swap = mod._compute_aggregate_payload(data_dir=swap_dir, quarter="26Q3", workspace_dir=ws, coverage_path=str(empty_cov))
    swap_entries = [e for e in agg_swap["sector_consensus_matrix"]["Other US Book"] if "Alpha Industrial" in e["name"]]
    assert len(swap_entries) == 1
    assert swap_entries[0]["name"] == cusip_rep_entries[0]["name"]
    assert swap_entries[0]["category"] == cusip_rep_entries[0]["category"]
    assert swap_entries[0]["held_count"] == 2

    # #6 & #7: render-batch + verify-numbers range-sign conflict + breakdown 其中/包括 total scope
    ch_path = tmp_path / "ch_r5.md"
    ch_path.write_text(
        (
            "# 13F Tracker_2026Q3\n\n"
            "## 核心结论与跨机构动向\n\n"
            "### AI/Semi/US SaaS\n"
            "- **Figma 期末合计 9000 万美元（其中 Hillhouse 与 Aspex 持有）**：不应按 6000 万误报。\n"
            "- **Tiger Global 减仓 +20% 至 +30% Seagate**：区间方向矛盾测试。\n\n"
            "## 持仓变化中的新线索\n\n- 无。\n"
        ),
        encoding="utf-8",
    )
    rpt_path = tmp_path / "rpt_r5.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py", "--workspace-dir", str(ws), "--json", "render-batch",
            "--data-dir", str(data_dir), "--quarter", "26Q3", "--chapters-file", str(ch_path), "--out", str(rpt_path),
        ],
    )
    mod.main()
    rb = json.loads(capsys.readouterr().out)
    assert rb["ok"] is True
    rpt_md = rpt_path.read_text(encoding="utf-8")
    # #6: 50.5% (+505/1000) qualifies for 大幅加仓 + descriptor!
    assert "大幅加仓高增长功率电源芯片供应商Foo Five" in rpt_md

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py", "--workspace-dir", str(ws), "--json", "verify-numbers",
            "--aggregate", str(agg_empty_path), "--report-file", str(rpt_path),
        ],
    )
    mod.main()
    vn = json.loads(capsys.readouterr().out)
    assert "动作词与正号方向冲突" in [m["reason"] for m in vn["mismatches"]]
    assert not any(m["claimed"] == "9000 万美元" for m in vn["mismatches"])

    # #1 (missing 26Q2 filing -> previous_period=2026-03-31): render-batch renders 26Q3 vs 26Q1, audit_13f_run rejects!
    skip_q2_dir = tmp_path / "skip_q2"
    shutil.copytree(data_dir, skip_q2_dir)
    f02_skip = skip_q2_dir / "02_first_beijing_26q3.json"
    d02_skip = json.loads(f02_skip.read_text(encoding="utf-8"))
    d02_skip["previous_period"] = "2026-03-31"
    f02_skip.write_text(json.dumps(d02_skip, ensure_ascii=False), encoding="utf-8")
    skip_rpt = tmp_path / "rpt_skip_q2.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "13f_skill.py", "--workspace-dir", str(ws), "--json", "render-batch",
            "--data-dir", str(skip_q2_dir), "--quarter", "26Q3", "--chapters-file", str(ch_path), "--out", str(skip_rpt),
        ],
    )
    mod.main()
    _ = json.loads(capsys.readouterr().out)
    assert "### 2. First Beijing Investment 26Q3 vs 26Q1" in skip_rpt.read_text(encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(skip_q2_dir), "--quarter", "26Q3", "--workspace-dir", str(ws), "--report-file", str(skip_rpt)],
    )
    assert audit_mod.main() != 0
    out_skip = capsys.readouterr().out
    assert "[PREVIOUS PERIOD MISMATCH]" in out_skip
    assert "[REPORT QUARTER MISMATCH]" in out_skip

    # #4a: Duplicate Evergreen CIK entries with different padding fail BOTH _load_batch_runs AND audit_13f_run
    dup_dir = tmp_path / "dup_eg"
    shutil.copytree(data_dir, dup_dir)
    f01_dup = dup_dir / "01_hillhouse_26q3.json"
    d01_dup = json.loads(f01_dup.read_text(encoding="utf-8"))
    d01_dup["merged_with"] = [
        {"institution": "0002016939", "base_period": "2026-09-30", "previous_period": "2026-06-30", "rows": 1},
        {"institution": "2016939", "base_period": "2026-09-30", "previous_period": "2026-06-30", "rows": 1},
    ]
    f01_dup.write_text(json.dumps(d01_dup, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Duplicate merged_with"):
        mod._load_batch_runs(dup_dir, "26Q3")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(dup_dir), "--quarter", "26Q3", "--workspace-dir", str(ws)],
    )
    assert audit_mod.main() != 0
    assert "Duplicate merged_with" in capsys.readouterr().out

    # #4b: Combined alias "Evergreen" paired with HHLR CIK is rejected for primary institution validation
    eg_name_dir = tmp_path / "eg_name"
    shutil.copytree(data_dir, eg_name_dir)
    f01_eg = eg_name_dir / "01_hillhouse_26q3.json"
    d01_eg = json.loads(f01_eg.read_text(encoding="utf-8"))
    d01_eg["institution"] = "Evergreen"
    f01_eg.write_text(json.dumps(d01_eg, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unrecognized primary institution"):
        mod._load_batch_runs(eg_name_dir, "26Q3")
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit_13f_run.py", "--data-dir", str(eg_name_dir), "--quarter", "26Q3", "--workspace-dir", str(ws)],
    )
    assert audit_mod.main() != 0
    assert "[HILLHOUSE MERGE]" in capsys.readouterr().out

    mod._init_state(None)


def test_v2_codex_round6_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression coverage for Codex round-6 re-review items #1-#8."""
    mod = _load_13f_module()
    audit_mod = _load_audit_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    # #1: --merge-with Evergreen,0002016939 deduplicates by normalized CIK before summing;
    #     missing Evergreen filing in compare returns ok=False with error & no partial report
    row_hhlr = {
        "Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC",
        "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0,
        "ShareChange": 0, "Status": "UNCHANGED",
    }
    row_eg = {
        "Cusip": "01609W102", "Ticker": "BABA", "Issuer": "ALIBABA GROUP HOLDING LTD",
        "Shares": 50_000, "Value": 5_000_000.0, "PrevShares": 0, "PrevValue": 0.0,
        "ShareChange": 50_000, "Status": "NEW",
    }
    f_hhlr = _make_filing(report_period="2026-09-30", manager="HHLR", comparison_rows=[row_hhlr], previous_period="2026-06-30")
    f_eg = _make_filing(report_period="2026-09-30", manager="Evergreen", comparison_rows=[row_eg], previous_period="2026-06-30")

    def _dispatch_dup_merge(cik_or_ticker):
        s = str(cik_or_ticker).strip().lower()
        if "2016939" in s or "evergreen" in s:
            return SimpleNamespace(cik=2016939, get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg]))
        return SimpleNamespace(cik=1762304, get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_hhlr]))

    comp_cls = create_autospec(_company_sig, spec_set=True)
    comp_cls.side_effect = _dispatch_dup_merge
    monkeypatch.setattr(mod, "Company", comp_cls)
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "compare", "--institution", "HHLR", "--merge-with", "Evergreen,0002016939", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    dup_cmp = json.loads(capsys.readouterr().out)
    assert dup_cmp["ok"] is True
    assert len(dup_cmp["merged_with"]) == 1
    baba_rows = [r for r in dup_cmp["rows"] if r["Cusip"] == "01609W102"]
    assert len(baba_rows) == 1 and baba_rows[0]["Shares"] == 50_000

    # Missing Evergreen matching quarter -> compare returns ok=False without formatted_report
    f_eg_old = _make_filing(report_period="2026-06-30", manager="Evergreen", comparison_rows=[row_eg], previous_period="2026-03-31")
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if ("2016939" in str(c) or "evergreen" in str(c).lower()) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_old if ("2016939" in str(c) or "evergreen" in str(c).lower()) else f_hhlr]),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    miss_cmp = json.loads(capsys.readouterr().out)
    assert miss_cmp["ok"] is False
    assert "没有" in miss_cmp["error"]
    assert "formatted_report" not in miss_cmp

    # #3 (compare): representative row is selected BEFORE summing rows by CUSIP -> order swap invariant
    row_alpha_ind = {
        "Cusip": "888222333", "Ticker": "", "Issuer": "ALPHA INDUSTRIAL HOLDINGS CORP",
        "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW",
    }
    row_zebra_ai = {
        "Cusip": "888222333", "Ticker": "", "Issuer": "ZEBRA AI CLOUD SOFTWARE CORP",
        "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW",
    }
    res_swap = []
    for pair in ([row_alpha_ind, row_zebra_ai], [row_zebra_ai, row_alpha_ind]):
        f_pair = _make_filing(report_period="2026-09-30", manager="Swap Fund", comparison_rows=pair, previous_period="2026-06-30")
        monkeypatch.setattr(mod, "Company", _make_company_stub(9997701, [f_pair]))
        monkeypatch.setattr(
            sys, "argv",
            ["13f_skill.py", "compare", "--institution", "9997701", "--no-merge", "--format", "cn", "--auto-learn", "false", "--json"],
        )
        mod.main()
        res_swap.append(json.loads(capsys.readouterr().out))
    assert res_swap[0]["categories"] == res_swap[1]["categories"]
    assert "Other US Book" in res_swap[0]["categories"]
    assert res_swap[0]["rows"][0]["ReportPeriod"] == "2026-09-30"

    # #4 (compare passes cur_period to _resolve_cusip_tickers & stamps ReportPeriod): current ticker beats historical verified ticker
    ws_tk = tmp_path / "ws_r6_tk"
    ws_tk_state = ws_tk / "state" / "dalton-13f-tracker"
    ws_tk_state.mkdir(parents=True)
    (ws_tk_state / "verified_tickers.json").write_text(
        json.dumps({"OLDT": {"category": "Other US Book", "canonical_name": "Old Name", "sample_issuer": "RENAMED TECH CO", "verified_quarter": "26Q2"}}),
        encoding="utf-8",
    )
    mod._init_state(ws_tk)
    cur_tk_row = {"Cusip": "444555666", "Ticker": "NEWT", "Issuer": "RENAMED TECH CO", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}
    prev_tk_row = {"Cusip": "444555666", "Ticker": "OLDT", "Issuer": "RENAMED TECH CO", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 0, "PrevValue": 0.0, "ShareChange": 100_000, "Status": "NEW"}
    f_tk_cur = _make_filing(report_period="2026-09-30", manager="Ticker Fund", comparison_rows=[cur_tk_row], previous_period="2026-06-30")
    f_tk_prev = _make_filing(report_period="2026-06-30", manager="Ticker Fund", comparison_rows=[prev_tk_row], previous_period="2026-03-31")
    monkeypatch.setattr(mod, "Company", _make_company_stub(9997702, [f_tk_cur, f_tk_prev]))
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "--workspace-dir", str(ws_tk), "compare", "--institution", "9997702", "--no-merge", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    tk_cmp = json.loads(capsys.readouterr().out)
    assert tk_cmp["rows"][0]["ReportPeriod"] == "2026-09-30"
    assert "Renamed Tech" in tk_cmp["formatted_report"]

    # #5: Strict CUSIP-only trend lookup (prior GOOGL add must NOT leak prefix to current GOOG; no canon/category fallback)
    mod._init_state(None)
    goog_rows = [
        {"Cusip": "02079K107", "Ticker": "GOOG", "Issuer": "ALPHABET INC", "Shares": 200_000, "Value": 30_000_000.0, "PrevShares": 100_000, "PrevValue": 15_000_000.0, "ShareChange": 100_000, "Status": "INCREASED"},
    ]
    prep_goog = mod._prepare_fund_items(
        goog_rows,
        auto_learn=False,
        prior_actions={("cusip", "02079K305", "equity"): "加仓", ("canon", "Alphabet", "equity"): "加仓"},
        prev_quarter_label="26Q2",
    )
    assert prep_goog["categories"]["AI/Semi/US SaaS"][0]["trend_prefix"] == ""

    # #6: Coverage field type errors & null items return coverage_missing=True cleanly without crashing
    cov_bad_type = tmp_path / "cov_bad_type.yaml"
    cov_bad_type.write_text(
        yaml.safe_dump({"companies": [{"ticker": "PDD", "aliases": {"search_names": 123}}]}),
        encoding="utf-8",
    )
    _, _, reason_type = mod._load_coverage_roster(ws_tk, str(cov_bad_type))
    assert reason_type is not None

    cov_null_item = tmp_path / "cov_null_item.yaml"
    cov_null_item.write_text("companies:\n  - null\n", encoding="utf-8")
    _, _, reason_null = mod._load_coverage_roster(ws_tk, str(cov_null_item))
    assert reason_null is not None

    # Build 18-group batch fixture testing #2 (missing previous_period), #5 (no categories prefix fallback), & #7 (verify-numbers)
    ws = tmp_path / "ws_r6"
    ws.mkdir(parents=True)
    batch_dir = tmp_path / "batch_r6"
    _write_real_18_group_run(
        data_dir=batch_dir,
        mod=mod,
        audit_mod=audit_mod,
        monkeypatch=monkeypatch,
        capsys=capsys,
        group_overrides={
            "hillhouse": [{"Cusip": "30303M102", "Ticker": "META", "Issuer": "META PLATFORMS INC", "Shares": 1_000_000, "Value": 40_000_000.0, "PrevShares": 1_000_000, "PrevValue": 40_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}],
            "coatue": [{"Cusip": "458140100", "Ticker": "INTC", "Issuer": "INTEL CORP", "Shares": 1_400_000, "Value": 140_000_000.0, "PrevShares": 1_000_000, "PrevValue": 100_000_000.0, "ShareChange": 400_000, "Status": "INCREASED"},
                       {"Cusip": "30303M102", "Ticker": "META", "Issuer": "META PLATFORMS INC", "Shares": 1_000_000, "Value": 30_000_000.0, "PrevShares": 1_000_000, "PrevValue": 30_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}],
            "tiger_global": [{"Cusip": "458140100", "Ticker": "INTC", "Issuer": "INTEL CORP", "Shares": 750_000, "Value": 75_000_000.0, "PrevShares": 1_000_000, "PrevValue": 100_000_000.0, "ShareChange": -250_000, "Status": "DECREASED"}],
            "greenwoods": [{"Cusip": "458140100", "Ticker": "INTC", "Issuer": "INTEL CORP", "Shares": 1_350_000, "Value": 135_000_000.0, "PrevShares": 1_000_000, "PrevValue": 100_000_000.0, "ShareChange": 350_000, "Status": "INCREASED"},
                           {"Cusip": "30303M102", "Ticker": "META", "Issuer": "META PLATFORMS INC", "Shares": 1_000_000, "Value": 20_000_000.0, "PrevShares": 1_000_000, "PrevValue": 20_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}],
        },
        workspace_dir=ws,
    )

    # #5 batch check: remove prior_actions from 01_hillhouse_26q3.json & inject legacy prefix in categories -> render-batch must NOT restore prefix
    f01 = batch_dir / "01_hillhouse_26q3.json"
    d01 = json.loads(f01.read_text(encoding="utf-8"))
    d01.pop("prior_actions", None)
    d01["categories"]["AI/Semi/US SaaS"] = ["- 继26Q2加仓后，继续Meta仓位不变，4000万美元"]
    f01.write_text(json.dumps(d01, ensure_ascii=False), encoding="utf-8")

    # #7 check:
    # - Tiger Global Intel -25% with "-30% 至 -20%" -> matches cleanly!
    # - "Coatue、Tiger Global、景林均加仓 30% 至 48% Intel" -> MUST flag mismatch because Tiger Global is -25%!
    # - "Meta 期末合计 9000 万美元，Coatue 与景林..." -> matches all-holder sum (40+30+20=90M) cleanly without breakdown cue!
    cov_ok = tmp_path / "cov_ok.yaml"
    cov_ok.write_text(yaml.safe_dump({"companies": []}), encoding="utf-8")
    agg_path = tmp_path / "agg_r6.json"
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "--workspace-dir", str(ws), "--json", "aggregate", "--data-dir", str(batch_dir), "--quarter", "26Q3", "--coverage", str(cov_ok), "--out", str(agg_path)],
    )
    mod.main()
    _ = json.loads(capsys.readouterr().out)

    ch_path = tmp_path / "ch_r6.md"
    ch_path.write_text(
        (
            "# 13F Tracker_2026Q3\n\n"
            "## 核心结论与跨机构动向\n\n"
            "### AI/Semi/US SaaS\n"
            "- **Meta** 期末合计 9000 万美元，Coatue、景林长期持有。\n"
            "- **Intel**：Tiger Global 减仓 -30% 至 -20%，Coatue、Tiger Global、景林均加仓 30% 至 48%。\n\n"
            "## 持仓变化中的新线索\n\n- 无。\n"
        ),
        encoding="utf-8",
    )
    rpt_path = tmp_path / "rpt_r6.md"
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "--workspace-dir", str(ws), "--json", "render-batch", "--data-dir", str(batch_dir), "--quarter", "26Q3", "--chapters-file", str(ch_path), "--out", str(rpt_path)],
    )
    mod.main()
    _ = json.loads(capsys.readouterr().out)
    assert "继26Q2" not in rpt_path.read_text(encoding="utf-8").split("## China Book 机构", 1)[1]

    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "--workspace-dir", str(ws), "--json", "verify-numbers", "--aggregate", str(agg_path), "--report-file", str(rpt_path)],
    )
    mod.main()
    vn = json.loads(capsys.readouterr().out)
    assert not any(m["claimed"] == "9000 万美元" for m in vn["mismatches"])
    assert not any(m["claimed"] == "-30%--20%" for m in vn["mismatches"])
    assert any(m["claimed"] == "30%-48%" for m in vn["mismatches"])

    # #2 check: missing previous_period in 01_hillhouse_26q3.json (with Evergreen 2026-03-31) MUST fail BOTH _load_batch_runs AND audit_13f_run.py
    miss_prev_dir = tmp_path / "miss_prev_r6"
    shutil.copytree(batch_dir, miss_prev_dir)
    f01_mp = miss_prev_dir / "01_hillhouse_26q3.json"
    d01_mp = json.loads(f01_mp.read_text(encoding="utf-8"))
    d01_mp.pop("previous_period", None)
    d01_mp["merged_with"][0]["previous_period"] = "2026-03-31"
    f01_mp.write_text(json.dumps(d01_mp, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="Missing previous_period"):
        mod._load_batch_runs(miss_prev_dir, "26Q3")
    monkeypatch.setattr(
        sys, "argv",
        ["audit_13f_run.py", "--data-dir", str(miss_prev_dir), "--quarter", "26Q3", "--workspace-dir", str(ws)],
    )
    assert audit_mod.main() != 0
    assert "[PREVIOUS PERIOD MISMATCH]" in capsys.readouterr().out

    mod._init_state(None)


def test_v2_codex_round7_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression coverage for Codex round-7 re-review items #1-#6."""
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    row_pdd = {
        "Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC",
        "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0,
        "ShareChange": 0, "Status": "UNCHANGED",
    }
    row_baba = {
        "Cusip": "01609W102", "Ticker": "BABA", "Issuer": "ALIBABA GROUP HOLDING LTD",
        "Shares": 50_000, "Value": 5_000_000.0, "PrevShares": 0, "PrevValue": 0.0,
        "ShareChange": 50_000, "Status": "NEW",
    }

    # #1a: HHLR compare_holdings() returning None while prior filing exists in filings -> ok=False (never Evergreen-only!)
    tf_hhlr_none = SimpleNamespace(
        report_period="2026-09-30",
        management_company_name="HHLR Advisors, Ltd.",
        compare_holdings=create_autospec(_compare_holdings_sig, spec_set=True, return_value=None),
    )
    f_hhlr_none = SimpleNamespace(obj=create_autospec(_filing_obj_sig, spec_set=True, return_value=tf_hhlr_none))
    f_hhlr_old = _make_filing(report_period="2026-06-30", manager="HHLR Advisors, Ltd.", comparison_rows=[row_pdd], previous_period="2026-03-31")
    f_eg_ok = _make_filing(report_period="2026-09-30", manager="Evergreen Quality Fund GP Ltd", comparison_rows=[row_baba], previous_period="2026-06-30")

    comp_cls = create_autospec(_company_sig, spec_set=True)
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_ok] if "2016939" in str(c) else [f_hhlr_none, f_hhlr_old]),
    )
    monkeypatch.setattr(mod, "Company", comp_cls)
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    out_none = json.loads(capsys.readouterr().out)
    assert out_none["ok"] is False
    assert "formatted_report" not in out_none

    # #1b: Both HHLR and Evergreen having empty previous_period -> ok=False
    f_hhlr_nop = _make_filing(report_period="2026-09-30", manager="HHLR Advisors, Ltd.", comparison_rows=[row_pdd], previous_period="")
    f_eg_nop = _make_filing(report_period="2026-09-30", manager="Evergreen Quality Fund GP Ltd", comparison_rows=[row_baba], previous_period="")
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_nop] if "2016939" in str(c) else [f_hhlr_nop]),
    )
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    out_nop = json.loads(capsys.readouterr().out)
    assert out_nop["ok"] is False
    assert "上期报告期" in out_nop["error"]

    # #2: --merge-with resolves aliases to canonical CIK before querying Company; order-independent
    f_hhlr_ok = _make_filing(report_period="2026-09-30", manager="HHLR Advisors, Ltd.", comparison_rows=[row_pdd], previous_period="2026-06-30")
    queried_refs: list[str] = []

    def _dispatch_strict_cik(cik_or_ticker):
        s = str(cik_or_ticker).strip()
        queried_refs.append(s)
        if s == "0002016939" or s == "2016939":
            return SimpleNamespace(cik=2016939, get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_ok]))
        if s.lower() == "evergreen":
            raise AssertionError("Alias 'Evergreen' should have been resolved to CIK 0002016939 before Company()")
        return SimpleNamespace(cik=1762304, get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_hhlr_ok]))

    comp_cls.side_effect = _dispatch_strict_cik
    order_results = []
    for mw_arg in ("Evergreen,0002016939", "0002016939,Evergreen"):
        queried_refs.clear()
        monkeypatch.setattr(
            sys, "argv",
            ["13f_skill.py", "compare", "--institution", "HHLR", "--merge-with", mw_arg, "--format", "cn", "--auto-learn", "false", "--json"],
        )
        mod.main()
        res_o = json.loads(capsys.readouterr().out)
        assert res_o["ok"] is True
        assert all(q.lower() != "evergreen" for q in queried_refs)
        order_results.append(res_o)
    assert order_results[0]["rows"] == order_results[1]["rows"]
    assert order_results[0]["formatted_report"] == order_results[1]["formatted_report"]

    # #3: Stock + Option sharing same CUSIP in BOTH cur & prev holdings do NOT double-count (12M cur / 11M prev, NOT 24M/22M)
    df_cur_h = pd.DataFrame([
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Class": "COM", "PutCall": "", "SharesPrnAmount": 100_000, "Value": 10_000_000.0},
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Class": "COM", "PutCall": "Call", "SharesPrnAmount": 20_000, "Value": 2_000_000.0},
    ])
    df_prev_h = pd.DataFrame([
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Class": "COM", "PutCall": "", "SharesPrnAmount": 90_000, "Value": 9_000_000.0},
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Class": "COM", "PutCall": "Call", "SharesPrnAmount": 20_000, "Value": 2_000_000.0},
    ])
    # Poisoned cartesian 2x2 compare_holdings df that old code would have summed into 24M / 22M
    df_cart = pd.DataFrame([
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 90_000, "PrevValue": 9_000_000.0, "ShareChange": 10_000, "Status": "INCREASED"},
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 20_000, "PrevValue": 2_000_000.0, "ShareChange": 80_000, "Status": "INCREASED"},
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Shares": 20_000, "Value": 2_000_000.0, "PrevShares": 90_000, "PrevValue": 9_000_000.0, "ShareChange": -70_000, "Status": "DECREASED"},
        {"Cusip": "111222333", "Ticker": "OPTCO", "Issuer": "OPTION CO", "Shares": 20_000, "Value": 2_000_000.0, "PrevShares": 20_000, "PrevValue": 2_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
    ])

    def _prev_hr_sig() -> object:
        return None

    tf_prev_h = SimpleNamespace(report_period="2026-06-30", management_company_name="Opt Fund", holdings=df_prev_h, previous_holding_report=create_autospec(_prev_hr_sig, spec_set=True, return_value=None))
    tf_cur_h = SimpleNamespace(
        report_period="2026-09-30",
        management_company_name="Opt Fund",
        holdings=df_cur_h,
        previous_holding_report=create_autospec(_prev_hr_sig, spec_set=True, return_value=tf_prev_h),
        compare_holdings=create_autospec(_compare_holdings_sig, spec_set=True, return_value=SimpleNamespace(data=df_cart, previous_period="2026-06-30")),
    )
    f_opt_cur = SimpleNamespace(obj=create_autospec(_filing_obj_sig, spec_set=True, return_value=tf_cur_h))
    monkeypatch.setattr(mod, "Company", _make_company_stub(9998801, [f_opt_cur]))
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "compare", "--institution", "9998801", "--no-merge", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    opt_res = json.loads(capsys.readouterr().out)
    assert opt_res["rows"][0]["Value"] == pytest.approx(12_000_000.0)
    assert opt_res["rows"][0]["PrevValue"] == pytest.approx(11_000_000.0)
    assert "1200万美元" in opt_res["formatted_report"]

    # #4: Closed row inheriting verified OLDT must NOT override active holding NEWT
    ws_r7 = tmp_path / "ws_r7"
    ws_r7_state = ws_r7 / "state" / "dalton-13f-tracker"
    ws_r7_state.mkdir(parents=True)
    (ws_r7_state / "verified_tickers.json").write_text(
        json.dumps({"OLDT": {"category": "Other US Book", "canonical_name": "Old Legacy Co", "sample_issuer": "LEGACY OLD CORP", "verified_quarter": "26Q2"}}),
        encoding="utf-8",
    )
    mod._init_state(ws_r7)
    row_active_new = {"Cusip": "333444555", "Ticker": "NEWT", "Issuer": "MODERN NEW CO", "Shares": 100_000, "Value": 20_000_000.0, "PrevShares": 100_000, "PrevValue": 20_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}
    row_closed_old = {"Cusip": "333444555", "Ticker": "OLDT", "Issuer": "LEGACY OLD CORP", "Shares": 0, "Value": 0.0, "PrevShares": 50_000, "PrevValue": 10_000_000.0, "ShareChange": -50_000, "Status": "CLOSED"}
    f_hhlr_act = _make_filing(report_period="2026-09-30", manager="HHLR", comparison_rows=[row_active_new], previous_period="2026-06-30")
    f_eg_cls = _make_filing(report_period="2026-09-30", manager="Evergreen", comparison_rows=[row_closed_old], previous_period="2026-06-30")
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_cls] if "2016939" in str(c) else [f_hhlr_act]),
    )
    monkeypatch.setattr(mod, "Company", comp_cls)
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "--workspace-dir", str(ws_r7), "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    closed_guard = json.loads(capsys.readouterr().out)
    assert closed_guard["ok"] is True
    assert closed_guard["rows"][0]["Ticker"] == "NEWT"
    assert "Modern" in closed_guard["formatted_report"]
    assert "Legacy" not in closed_guard["formatted_report"]

    # #5: Merged fund (HHLR+Evergreen) Q3 valid, Q2 missing Evergreen comparison -> NO trend prefix (no error!)
    row_pdd_add_q3 = {"Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC", "Shares": 150_000, "Value": 15_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 50_000, "Status": "INCREASED"}
    row_pdd_add_q2 = {"Cusip": "722304102", "Ticker": "PDD", "Issuer": "PDD HOLDINGS INC", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 50_000, "PrevValue": 5_000_000.0, "ShareChange": 50_000, "Status": "INCREASED"}
    f_hhlr_q3 = _make_filing(report_period="2026-09-30", manager="HHLR", comparison_rows=[row_pdd_add_q3], previous_period="2026-06-30")
    f_hhlr_q2 = _make_filing(report_period="2026-06-30", manager="HHLR", comparison_rows=[row_pdd_add_q2], previous_period="2026-03-31")
    f_eg_q3 = _make_filing(report_period="2026-09-30", manager="Evergreen", comparison_rows=[row_baba], previous_period="2026-06-30")
    # Evergreen only has Q3 (no Q2 filing) -> prior merge incomplete -> PDD Q3 must have NO "继..." prefix!
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_q3] if "2016939" in str(c) else [f_hhlr_q3, f_hhlr_q2]),
    )
    monkeypatch.setattr(mod, "Company", comp_cls)
    monkeypatch.setattr(
        sys, "argv",
        ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"],
    )
    mod.main()
    inc_prev = json.loads(capsys.readouterr().out)
    assert inc_prev["ok"] is True
    assert "继" not in inc_prev["formatted_report"]
    assert "加仓PDD" in inc_prev["formatted_report"]

    # #6: _extract_actions computes per-CUSIP actions independently (Q2 GOOGL +50%, GOOG 0% -> Q3 GOOG gets NO prefix; missing CUSIP gets NO prefix)
    mod._init_state(None)
    q2_alpha_rows = [
        {"Cusip": "02079K305", "Ticker": "GOOGL", "Issuer": "ALPHABET INC", "Shares": 150_000, "Value": 20_000_000.0, "PrevShares": 100_000, "PrevValue": 14_000_000.0, "ShareChange": 50_000, "Status": "INCREASED"},
        {"Cusip": "02079K107", "Ticker": "GOOG", "Issuer": "ALPHABET INC", "Shares": 100_000, "Value": 14_000_000.0, "PrevShares": 100_000, "PrevValue": 14_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"},
    ]
    acts_sep = mod._extract_actions(q2_alpha_rows)
    assert acts_sep[("cusip", "02079K305", "equity")] == "加仓"
    assert acts_sep[("cusip", "02079K107", "equity")] == "不变"
    q3_goog_only = [
        {"Cusip": "02079K107", "Ticker": "GOOG", "Issuer": "ALPHABET INC", "Shares": 150_000, "Value": 22_000_000.0, "PrevShares": 100_000, "PrevValue": 14_000_000.0, "ShareChange": 50_000, "Status": "INCREASED"},
    ]
    prep_sep = mod._prepare_fund_items(q3_goog_only, auto_learn=False, prior_actions=acts_sep, prev_quarter_label="26Q2")
    assert prep_sep["categories"]["AI/Semi/US SaaS"][0]["trend_prefix"] == ""

    no_cusip_row = [
        {"Cusip": "", "Ticker": "NVDA", "Issuer": "NVIDIA CORP", "Shares": 150_000, "Value": 20_000_000.0, "PrevShares": 100_000, "PrevValue": 13_000_000.0, "ShareChange": 50_000, "Status": "INCREASED"},
    ]
    prep_nocusip = mod._prepare_fund_items(
        no_cusip_row,
        auto_learn=False,
        prior_actions={("ticker", "NVDA", "equity"): "加仓"},
        prev_quarter_label="26Q2",
    )
    assert prep_nocusip["categories"]["AI/Semi/US SaaS"][0]["trend_prefix"] == ""

    mod._init_state(None)


def test_v2_codex_round8_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression coverage for Codex round-8 re-review items #1-#4."""
    mod = _load_13f_module()
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    # #1: Multi-CUSIP merged item (Alphabet = GOOGL + GOOG) recomputes prior action from summed prior shares
    # Q2 total: (100+400)=500 -> (200+100)=300 (net trim!), even though GOOGL doubled (+100%)
    q2_rows = [
        {"Cusip": "02079K305", "Ticker": "GOOGL", "Issuer": "ALPHABET INC", "Shares": 200, "Value": 40_000.0, "PrevShares": 100, "PrevValue": 20_000.0, "ShareChange": 100, "Status": "INCREASED"},
        {"Cusip": "02079K107", "Ticker": "GOOG", "Issuer": "ALPHABET INC", "Shares": 100, "Value": 20_000.0, "PrevShares": 400, "PrevValue": 80_000.0, "ShareChange": -300, "Status": "DECREASED"},
    ]
    q3_rows = [
        {"Cusip": "02079K305", "Ticker": "GOOGL", "Issuer": "ALPHABET INC", "Shares": 240, "Value": 50_000.0, "PrevShares": 200, "PrevValue": 40_000.0, "ShareChange": 40, "Status": "INCREASED"},
        {"Cusip": "02079K107", "Ticker": "GOOG", "Issuer": "ALPHABET INC", "Shares": 120, "Value": 25_000.0, "PrevShares": 100, "PrevValue": 20_000.0, "ShareChange": 20, "Status": "INCREASED"},
    ]
    acts_multi = mod._extract_actions(q2_rows)
    prep_multi = mod._prepare_fund_items(q3_rows, auto_learn=False, prior_actions=acts_multi, prev_quarter_label="26Q2")
    alpha_item = prep_multi["categories"]["AI/Semi/US SaaS"][0]
    assert alpha_item["trend_prefix"] == ""
    assert "继" not in alpha_item["text"]

    # #2: Prior trend search ONLY accepts exact previous_period (never skips Q2 to use earlier 26Q1!)
    row_nvda_q3 = {"Cusip": "67066G104", "Ticker": "NVDA", "Issuer": "NVIDIA CORP", "Shares": 200_000, "Value": 20_000_000.0, "PrevShares": 100_000, "PrevValue": 10_000_000.0, "ShareChange": 100_000, "Status": "INCREASED"}
    row_nvda_q2_trim = {"Cusip": "67066G104", "Ticker": "NVDA", "Issuer": "NVIDIA CORP", "Shares": 100_000, "Value": 10_000_000.0, "PrevShares": 150_000, "PrevValue": 15_000_000.0, "ShareChange": -50_000, "Status": "DECREASED"}
    row_nvda_q1_add = {"Cusip": "67066G104", "Ticker": "NVDA", "Issuer": "NVIDIA CORP", "Shares": 150_000, "Value": 15_000_000.0, "PrevShares": 50_000, "PrevValue": 5_000_000.0, "ShareChange": 100_000, "Status": "INCREASED"}
    row_baba_q3 = {"Cusip": "01609W102", "Ticker": "BABA", "Issuer": "ALIBABA GROUP HOLDING LTD", "Shares": 50_000, "Value": 5_000_000.0, "PrevShares": 50_000, "PrevValue": 5_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}
    row_baba_q2 = {"Cusip": "01609W102", "Ticker": "BABA", "Issuer": "ALIBABA GROUP HOLDING LTD", "Shares": 50_000, "Value": 5_000_000.0, "PrevShares": 50_000, "PrevValue": 5_000_000.0, "ShareChange": 0, "Status": "UNCHANGED"}

    f_hh_q3 = _make_filing(report_period="2026-09-30", manager="HHLR", comparison_rows=[row_nvda_q3], previous_period="2026-06-30")
    f_hh_q2 = _make_filing(report_period="2026-06-30", manager="HHLR", comparison_rows=[row_nvda_q2_trim], previous_period="2026-03-31")
    f_hh_q1 = _make_filing(report_period="2026-03-31", manager="HHLR", comparison_rows=[row_nvda_q1_add], previous_period="2025-12-31")
    f_eg_q3 = _make_filing(report_period="2026-09-30", manager="Evergreen", comparison_rows=[row_baba_q3], previous_period="2026-06-30")
    f_eg_q2 = _make_filing(report_period="2026-06-30", manager="Evergreen", comparison_rows=[row_baba_q2], previous_period="2026-03-31")

    comp_cls = create_autospec(_company_sig, spec_set=True)
    # 2a: Even if filings lists Q1 before Q2, Q2 (main_prev_period) is used and NOT Q1 -> no "继26Q1加仓后，继续"
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_q3, f_eg_q2] if "2016939" in str(c) else [f_hh_q3, f_hh_q1, f_hh_q2]),
    )
    monkeypatch.setattr(mod, "Company", comp_cls)
    monkeypatch.setattr(sys, "argv", ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"])
    mod.main()
    r_order = json.loads(capsys.readouterr().out)
    assert r_order["ok"] is True and "继" not in r_order["formatted_report"]

    # 2b: When Q2 is missing from filings list, never falls back to Q1
    comp_cls.side_effect = lambda c: SimpleNamespace(
        cik=2016939 if "2016939" in str(c) else 1762304,
        get_filings=create_autospec(_get_filings_sig, spec_set=True, return_value=[f_eg_q3] if "2016939" in str(c) else [f_hh_q3, f_hh_q1]),
    )
    monkeypatch.setattr(sys, "argv", ["13f_skill.py", "compare", "--institution", "HHLR", "--format", "cn", "--auto-learn", "false", "--json"])
    mod.main()
    r_no_q2 = json.loads(capsys.readouterr().out)
    assert r_no_q2["ok"] is True and "继26Q1" not in r_no_q2["formatted_report"]

    # #3: Zero-share equity row + positive option row on same CUSIP -> option representative wins; all-zero falls back to equity-first
    agg_zero_eq = mod._aggregate_period_holdings_by_cusip([
        {"Cusip": "777888999", "Ticker": "OLDT", "Issuer": "OLD CORP", "Class": "COM", "PutCall": "", "SharesPrnAmount": 0, "Value": 0.0},
        {"Cusip": "777888999", "Ticker": "NEWT", "Issuer": "NEW CORP", "Class": "CALL", "PutCall": "Call", "SharesPrnAmount": 10_000, "Value": 5_000_000.0},
    ])
    assert agg_zero_eq["777888999"]["ticker"] == "NEWT"
    assert agg_zero_eq["777888999"]["issuer"] == "NEW CORP"
    agg_all_zero = mod._aggregate_period_holdings_by_cusip([
        {"Cusip": "777888999", "Ticker": "OLDT", "Issuer": "OLD CORP", "Class": "COM", "PutCall": "", "SharesPrnAmount": 0, "Value": 0.0},
        {"Cusip": "777888999", "Ticker": "NEWT", "Issuer": "NEW CORP", "Class": "CALL", "PutCall": "Call", "SharesPrnAmount": 0, "Value": 0.0},
    ])
    assert agg_all_zero["777888999"]["ticker"] == "OLDT"

    # #4: Reading .holdings retries via _retry on initial TimeoutError (no sleep delay in test)
    def _sleep_sig(secs: float) -> None:
        return None

    sleep_spy = create_autospec(_sleep_sig, spec_set=True)
    monkeypatch.setattr(mod.time, "sleep", sleep_spy)

    class _FlakyThirteenF:
        def __init__(self, report_period: str, df: pd.DataFrame, prev_obj=None) -> None:
            self.report_period = report_period
            self.management_company_name = "Retry Fund"
            self._df = df
            self._prev_obj = prev_obj
            self.calls = 0

        @property
        def holdings(self) -> pd.DataFrame:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("SEC infotable timeout")
            return self._df

        def previous_holding_report(self):
            return self._prev_obj

    df_prev_retry = pd.DataFrame([{"Cusip": "037833100", "Ticker": "AAPL", "Issuer": "APPLE INC", "SharesPrnAmount": 100_000, "Value": 10_000_000.0}])
    df_cur_retry = pd.DataFrame([{"Cusip": "037833100", "Ticker": "AAPL", "Issuer": "APPLE INC", "SharesPrnAmount": 200_000, "Value": 20_000_000.0}])
    prev_flaky = _FlakyThirteenF("2026-06-30", df_prev_retry, prev_obj=None)
    cur_flaky = _FlakyThirteenF("2026-09-30", df_cur_retry, prev_obj=prev_flaky)
    has_c, aligned_rows, prev_p, _ = mod._extract_period_comparison(cur_flaky)
    assert has_c is True
    assert prev_p == "2026-06-30"
    assert cur_flaky.calls == 2 and prev_flaky.calls == 2
    assert len(aligned_rows) == 1 and aligned_rows[0]["Value"] == pytest.approx(20_000_000.0)
    assert sleep_spy.call_count >= 2

    mod._init_state(None)
