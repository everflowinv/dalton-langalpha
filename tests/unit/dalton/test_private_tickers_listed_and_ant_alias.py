"""Tests for Item 6: SpaceX (SPCX) and Anthropic (ANT) are listed (no hardcoded private exclusion) + Anthropic <-> ANT alias."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from src.dalton.coverage.aliases import SUPPLEMENTAL_TICKER_ALIASES

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module(name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_anthropic_to_ant_alias_mapping_exists() -> None:
    """SUPPLEMENTAL_TICKER_ALIASES must map ANT <-> Anthropic (matching SPCX <-> SpaceX)."""
    assert "ANT" in SUPPLEMENTAL_TICKER_ALIASES
    assert "Anthropic" in SUPPLEMENTAL_TICKER_ALIASES["ANT"]
    assert "SPCX" in SUPPLEMENTAL_TICKER_ALIASES
    assert "SpaceX" in SUPPLEMENTAL_TICKER_ALIASES["SPCX"]


def test_move_quant_allows_spcx_and_ant_as_listed_us_tickers() -> None:
    """dalton-stock-move move_quant must not reject SPCX or ANT/Anthropic as unlisted."""
    mq = _load_module(
        "test_move_quant_item6",
        "plugins/dalton/skills/dalton-stock-move/scripts/move_quant.py",
    )
    for sym in ("SPCX", "ANT"):
        ok, reason = mq.supports_extended_session(sym)
        assert ok is True, f"{sym} should be supported as a listed US ticker, got {reason}"
        assert sym not in mq.PRIVATE_TICKERS
    assert "ANTHROPIC" not in mq.PRIVATE_TICKERS


def test_move_quant_blocks_openai_fallback_and_coverage_private_in_extended_session(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """OPENAI retains fallback private guard, coverage is_private=true blocks pre/post before yfinance,
    and SPCX/ANT continue to pass."""
    import sys
    import types

    mq = _load_module(
        "test_move_quant_private_extended",
        "plugins/dalton/skills/dalton-stock-move/scripts/move_quant.py",
    )

    # Fallback set keeps OPENAI only (SPCX and ANT stay allowed)
    assert mq.PRIVATE_TICKERS == {"OPENAI"}
    ok_openai, reason_openai = mq.supports_extended_session("OPENAI")
    assert ok_openai is False
    assert reason_openai and "OPENAI" in reason_openai
    for sym in ("SPCX", "ANT"):
        ok_sym, reason_sym = mq.supports_extended_session(sym)
        assert ok_sym is True, f"{sym} should stay allowed, got {reason_sym}"

    # Fake yfinance that fails loudly if download() is ever reached for private tickers
    download_calls: list[Any] = []

    def _forbidden_download(*args: Any, **kwargs: Any) -> Any:
        download_calls.append((args, kwargs))
        raise AssertionError("yf.download must not be called for private tickers")

    fake_yf = types.ModuleType("yfinance")
    fake_yf.download = _forbidden_download  # type: ignore[attr-defined]
    fake_yf.Ticker = lambda _sym: types.SimpleNamespace(info={})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    cov_path = tmp_path / "coverage.yaml"
    cov_path.write_text(
        """
companies:
  - ticker: STRIPE
    name: Stripe
    tier: Watch
    is_private: true
    peer_group: payments
  - ticker: SPCX
    name: SpaceX
    tier: Core
    peer_group: space
  - ticker: ANT
    name: Anthropic
    tier: Core
    peer_group: ai
""",
        encoding="utf-8",
    )

    # 1) Coverage-marked private ticker is blocked in run_analyze pre/post before yf.download
    res_cov = mq.run_analyze("STRIPE", session="pre", coverage_path=str(cov_path))
    assert res_cov["ok"] is False
    assert res_cov["unsupported_session"] is True
    assert "STRIPE" in (res_cov.get("error") or "")

    # 2) OPENAI fallback is blocked in run_analyze pre/post even without coverage entry
    res_openai = mq.run_analyze("OPENAI", session="post", coverage_path=str(cov_path))
    assert res_openai["ok"] is False
    assert res_openai["unsupported_session"] is True
    assert "OPENAI" in (res_openai.get("error") or "")

    assert download_calls == []


def test_weekly_review_screen_movers_does_not_skip_spcx_or_anthropic_by_hardcode(
    tmp_path: Path,
) -> None:
    """dalton-weekly-review screen_movers must determine listed vs unlisted from data,
    not by hardcoding SPCX/SpaceX/Anthropic/ANT as private."""
    sm = _load_module(
        "test_weekly_screen_movers_item6",
        "plugins/dalton/skills/dalton-weekly-review/scripts/screen_movers.py",
    )
    cov_path = tmp_path / "coverage.yaml"
    cov_path.write_text(
        """
companies:
  - ticker: SPCX
    name: SpaceX
    tier: Core
    peer_group: space
  - ticker: ANT
    name: Anthropic
    tier: Core
    peer_group: ai
  - ticker: PRIVATE_CO
    name: Some Private Co
    tier: Watch
    is_private: true
    peer_group: ai
""",
        encoding="utf-8",
    )
    entries, _groups, skipped = sm.load_universe_from_coverage(str(cov_path))
    entry_tickers = {e["ticker"] for e in entries}
    assert "SPCX" in entry_tickers
    assert "ANT" in entry_tickers
    assert "PRIVATE_CO" not in entry_tickers
    assert any("PRIVATE_CO" in s for s in skipped)


def test_richard_coverage_weekly_screen_movers_does_not_mark_anthropic_private() -> None:
    """dalton-richard-coverage-weekly screen_movers must not hardcode Anthropic in PRIVATE_ROWS
    and should map Anthropic -> ANT."""
    rcw = _load_module(
        "test_richard_cov_screen_movers_item6",
        "plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/screen_movers.py",
    )
    assert "Anthropic" not in rcw.PRIVATE_ROWS
    assert "Anthropic" in rcw.TICKERS
    assert rcw.TICKERS["Anthropic"][0] == "ANT"


def test_ai_app_weekly_monitor_universe_moves_anthropic_from_private_watchlist_to_listed_ant() -> None:
    """dalton-ai-app-weekly-monitor universe.json must not exclude Anthropic as unlisted in private_watchlist."""
    import json

    uni_path = (
        REPO_ROOT
        / "plugins"
        / "dalton"
        / "skills"
        / "dalton-ai-app-weekly-monitor"
        / "references"
        / "universe.json"
    )
    data = json.loads(uni_path.read_text(encoding="utf-8"))
    private_companies = data.get("private_watchlist", {}).get("companies", [])
    assert "Anthropic" not in private_companies
    listed_tickers = {entry.get("ticker") for entry in data.get("universe", []) if isinstance(entry, dict)}
    assert "ANT" in listed_tickers


def test_core_release_packaging_excludes_richard_exclusive_weekly_skill(tmp_path: Path) -> None:
    """Separation principle: package_release.sh denylist excludes FUND_AUTOMATIONS-bound Everflow
    skills (dalton-richard-coverage-weekly & dalton-ai-app-weekly-monitor) and verify_release_tarball rejects them."""
    import io
    import tarfile

    pkg_sh = (REPO_ROOT / "scripts" / "standalone" / "package_release.sh").read_text(encoding="utf-8")
    assert '"plugins/dalton/skills/dalton-richard-coverage-weekly"' in pkg_sh
    assert '"plugins/dalton/skills/dalton-ai-app-weekly-monitor"' in pkg_sh

    verify_mod = _load_module(
        "verify_release_tarball_sep_check",
        "scripts/standalone/verify_release_tarball.py",
    )
    for skill_dir, rel_file in (
        ("dalton-richard-coverage-weekly", "scripts/screen_movers.py"),
        ("dalton-ai-app-weekly-monitor", "scripts/ai_application_weekly_monitor_cli.py"),
    ):
        tar_path = tmp_path / f"bad_core_{skill_dir}.tar.gz"
        top = "dalton-core-test"
        with tarfile.open(tar_path, "w:gz") as tf:
            d_info = tarfile.TarInfo(name=top)
            d_info.type = tarfile.DIRTYPE
            tf.addfile(d_info)
            f_data = b"# exclusive\n"
            f_info = tarfile.TarInfo(
                name=f"{top}/plugins/dalton/skills/{skill_dir}/{rel_file}"
            )
            f_info.size = len(f_data)
            tf.addfile(f_info, io.BytesIO(f_data))

        violations = verify_mod.inspect_tarball(tar_path, expected_top_dir=top)
        assert any(skill_dir in v for v in violations), skill_dir
