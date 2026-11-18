"""Offline unit tests for dalton-13f-tracker skill."""

from __future__ import annotations

import importlib.util
import json
import re
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
)

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

    # Every script path referenced in SKILL.md exists under SKILL_DIR
    rel_scripts = set(re.findall(r"\.agents/skills/dalton-13f-tracker/([^\s`'\")]+\.py)", skill_md))
    assert rel_scripts == {"scripts/13f_skill.py"}
    for rel in rel_scripts:
        assert (SKILL_DIR / rel).is_file()
    for cfg in JSON_CONFIG_FILES:
        assert (SCRIPTS_DIR / cfg).is_file()
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
