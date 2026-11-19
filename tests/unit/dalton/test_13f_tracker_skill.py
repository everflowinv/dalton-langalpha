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
) -> object:
    df = pd.DataFrame(comparison_rows or [])
    comparison_obj = SimpleNamespace(data=df)

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
) -> dict:
    """Execute real `13f_skill.py compare` path and persist its JSON output."""
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")
    monkeypatch.setattr(mod, "set_identity", create_autospec(_identity_sig, spec_set=True))

    if slug == "hillhouse":
        f_hhlr = _make_filing(report_period=report_period, manager="HHLR Advisors, Ltd.", comparison_rows=rows)
        eg_p = evergreen_period or report_period
        f_eg = _make_filing(
            report_period=eg_p,
            manager="Evergreen Quality Fund GP Ltd",
            comparison_rows=evergreen_rows if evergreen_rows is not None else [],
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
        f_single = _make_filing(report_period=report_period, manager=f"{slug} Capital", comparison_rows=rows)
        monkeypatch.setattr(mod, "Company", _make_company_stub(9990100, [f_single]))
        inst_arg = "9990100"

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
