"""Offline unit tests for dalton-option-put-skew skill."""

from __future__ import annotations

import datetime as dt
import importlib.util
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import numpy as np
import pandas as pd
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-option-put-skew"
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
SCRIPTS_DIR = SKILL_DIR / "scripts"


def _load_option_skew_module():
    mod_name = "dalton_option_skew_under_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    target = SCRIPTS_DIR / "option_skew.py"
    spec = importlib.util.spec_from_file_location(mod_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _yf_download_sig(
    tickers: list[str] | str,
    start: str | None = None,
    progress: bool = False,
    auto_adjust: bool = False,
) -> pd.DataFrame:
    return pd.DataFrame()


def _yf_ticker_sig(ticker: str) -> object:
    return None


def _ticker_history_sig(period: str = "5d") -> pd.DataFrame:
    return pd.DataFrame()


def _ticker_option_chain_sig(date: str | None = None) -> object:
    return None


# ---------------------------------------------------------------------------
# Registry, frontmatter, references, and skill-ref scanner
# ---------------------------------------------------------------------------


def test_registry_frontmatter_references_and_skill_refs() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )
    from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY

    sys.path.insert(0, str(REPO_ROOT / "scripts" / "dalton"))
    try:
        import check_skill_refs
    finally:
        sys.path.pop(0)

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

    # All referenced scripts and markdown references exist under SKILL_DIR
    rel_scripts = set(re.findall(r"\.agents/skills/dalton-option-put-skew/([^\s`'\")]+\.py)", skill_md))
    assert rel_scripts == {"scripts/option_skew.py"}
    for rel in rel_scripts:
        assert (SKILL_DIR / rel).is_file()

    for rel_md in ("references/opening-execution.md", "references/etf-holdings.md"):
        assert rel_md in skill_md
        assert (SKILL_DIR / rel_md).is_file()

    # OpenClaw-only tools/skills removed; Dalton feishu_create_doc & out-of-scope note present
    assert "feishu_create_doc" in skill_md
    assert "不在本 skill 范围内" in skill_md
    for md_file in SKILL_DIR.rglob("*.md"):
        txt = md_file.read_text(encoding="utf-8")
        assert "feishu-doc-delivery" not in txt
        assert "session_status" not in txt
        assert "hedging-analysis" not in txt
        assert "researchdata__" not in txt
        assert "Discord" not in txt

    findings = check_skill_refs.scan_skills()
    for hit in findings["ghost_hits"]:
        assert SKILL_NAME not in hit["file"], f"Unexpected ghost tool ref: {hit}"
    for br in findings["broken_refs"]:
        assert SKILL_NAME not in br["file"], f"Unexpected broken ref: {br}"


# ---------------------------------------------------------------------------
# Option math & fake market data calculations (no network)
# ---------------------------------------------------------------------------


def test_bs_price_and_implied_vol_roundtrip() -> None:
    mod = _load_option_skew_module()
    S, K, T, v, r = 100.0, 90.0, 35 / 365, 0.28, 0.04
    put_px = mod.bs_price(S, K, T, v, r, "p")
    call_px = mod.bs_price(S, 110.0, T, 0.17, r, "c")
    assert put_px > 0
    assert call_px > 0
    assert mod.implied_vol(put_px, S, K, T, r, "p") == pytest.approx(0.28, abs=1e-4)
    assert mod.implied_vol(call_px, S, 110.0, T, r, "c") == pytest.approx(0.17, abs=1e-4)
    assert math.isnan(mod.implied_vol(-1.0, S, K, T, r, "p"))


def test_skew_index_outputs_metrics_and_quintile_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_option_skew_module()
    dates = pd.bdate_range("2025-01-02", periods=300)
    skew_vals = np.linspace(120.0, 155.0, len(dates))
    spx_vals = 5000.0 + np.cumsum(np.sin(np.arange(len(dates)) / 10.0) * 15.0)
    panel = pd.DataFrame(
        {
            ("Close", "^SKEW"): skew_vals,
            ("Close", "^GSPC"): spx_vals,
        },
        index=dates,
    )
    panel.columns = pd.MultiIndex.from_tuples(panel.columns)

    dl_stub = create_autospec(_yf_download_sig, spec_set=True)
    dl_stub.return_value = panel
    monkeypatch.setattr(mod.yf, "download", dl_stub)

    mod.skew_index()
    out = capsys.readouterr().out
    assert "SKEW " in out
    assert "155.0" in out
    assert "pct_since_2010" in out
    assert "pct_1y" in out
    for col in ("skew", "fwd21", "fwdmin", "p_dd5"):
        assert col in out
    assert out.strip().splitlines()[-1].startswith("4")


def test_chain_skew_outputs_expected_strikes_and_spreads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_option_skew_module()
    today = dt.date(2026, 5, 15)
    expiry = "2026-06-19"  # 35 DTE
    T = (dt.date.fromisoformat(expiry) - today).days / 365
    S = 100.0
    r = 0.04

    put_vols = {90.0: 0.28, 95.0: 0.24, 100.0: 0.20}
    call_vols = {100.0: 0.20, 105.0: 0.18, 110.0: 0.17}

    puts_df = pd.DataFrame(
        [
            {
                "strike": k,
                "bid": mod.bs_price(S, k, T, vol, r, "p") - 0.02,
                "ask": mod.bs_price(S, k, T, vol, r, "p") + 0.02,
                "lastPrice": mod.bs_price(S, k, T, vol, r, "p"),
            }
            for k, vol in put_vols.items()
        ]
    )
    calls_df = pd.DataFrame(
        [
            {
                "strike": k,
                "bid": mod.bs_price(S, k, T, vol, r, "c") - 0.02,
                "ask": mod.bs_price(S, k, T, vol, r, "c") + 0.02,
                "lastPrice": mod.bs_price(S, k, T, vol, r, "c"),
            }
            for k, vol in call_vols.items()
        ]
    )

    hist_fn = create_autospec(_ticker_history_sig, spec_set=True)
    hist_fn.return_value = pd.DataFrame({"Close": [99.0, 100.0]})

    chain_fn = create_autospec(_ticker_option_chain_sig, spec_set=True)
    chain_fn.return_value = SimpleNamespace(puts=puts_df, calls=calls_df)

    ticker_obj = SimpleNamespace(
        history=hist_fn,
        options=("2026-05-22", expiry, "2026-08-21"),
        option_chain=chain_fn,
    )
    ticker_cls = create_autospec(_yf_ticker_sig, spec_set=True)
    ticker_cls.return_value = ticker_obj
    monkeypatch.setattr(mod.yf, "Ticker", ticker_cls)

    mod.chain_skew("SPY", today, 25, 50, r)
    out = capsys.readouterr().out
    assert "SPY 2026-06-19 100.0" in out
    for key in ("'p90': 28.0", "'p95': 24.0", "'p100': 20.0", "'c100': 20.0", "'c105': 18.0", "'c110': 17.0"):
        assert key in out
    assert "90P-ATM 8.0" in out
    assert "90P-110C 11.0" in out


def test_chain_skew_handles_missing_expiry_in_window(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mod = _load_option_skew_module()
    hist_fn = create_autospec(_ticker_history_sig, spec_set=True)
    hist_fn.return_value = pd.DataFrame({"Close": [100.0]})
    chain_fn = create_autospec(_ticker_option_chain_sig, spec_set=True)

    ticker_obj = SimpleNamespace(history=hist_fn, options=("2026-09-18",), option_chain=chain_fn)
    ticker_cls = create_autospec(_yf_ticker_sig, spec_set=True)
    ticker_cls.return_value = ticker_obj
    monkeypatch.setattr(mod.yf, "Ticker", ticker_cls)

    mod.chain_skew("SMH", dt.date(2026, 5, 15), 25, 50, 0.04)
    out = capsys.readouterr().out
    assert "SMH no expiry in window" in out
    assert chain_fn.call_count == 0
