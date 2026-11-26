"""Tests for Item 6: SpaceX (SPCX) and Anthropic (ANT) are listed (no hardcoded private exclusion) + Anthropic <-> ANT alias."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from src.dalton.conversations import extractor

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module(name: str, rel_path: str) -> Any:
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_extractor_does_not_treat_spacex_spcx_or_anthropic_ant_as_private() -> None:
    """SPCX / SpaceX and ANT / Anthropic must not be hardcoded in PRIVATE_COMPANIES."""
    for sym in ("SPCX", "SPACEX", "ANT", "ANTHROPIC"):
        assert sym not in extractor.PRIVATE_COMPANIES
        assert not extractor._is_private_company_ticker(sym, alias_map=extractor.SUPPLEMENTAL_TICKER_ALIASES)

    tickers, entities = extractor.normalize_tickers_and_entities(
        ["SPCX", "ANT", "OPENAI"],
        [],
        alias_map=extractor.SUPPLEMENTAL_TICKER_ALIASES,
    )
    assert "SPCX" in tickers
    assert "ANT" in tickers
    assert "OPENAI" not in tickers
    assert any(e.upper() == "OPENAI" for e in entities)


def test_anthropic_to_ant_alias_mapping_resolves_company_name_to_ticker() -> None:
    """SUPPLEMENTAL_TICKER_ALIASES must map ANT <-> Anthropic (matching SPCX <-> SpaceX)."""
    assert "ANT" in extractor.SUPPLEMENTAL_TICKER_ALIASES
    assert "Anthropic" in extractor.SUPPLEMENTAL_TICKER_ALIASES["ANT"]
    assert "SPCX" in extractor.SUPPLEMENTAL_TICKER_ALIASES
    assert "SpaceX" in extractor.SUPPLEMENTAL_TICKER_ALIASES["SPCX"]

    # Company name -> ticker backfill in extractor resolves Anthropic -> ANT
    backfilled, added = extractor.backfill_tickers_from_quote(
        [],
        "Anthropic 发布了新的企业级 Claude 合同，年化收入大幅增长。",
        claim="Anthropic 企业合同推动收入增长。",
        alias_map=extractor.SUPPLEMENTAL_TICKER_ALIASES,
    )
    assert added >= 1
    assert "ANT" in backfilled


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


def test_extractor_normalizes_anthropic_company_name_in_tickers_to_only_ant() -> None:
    """When the LLM puts 'Anthropic' directly into tickers, normalization + validation + backfill
    must collapse it to 'ANT' and never keep 'ANTHROPIC' alongside 'ANT'."""
    alias_map = extractor.SUPPLEMENTAL_TICKER_ALIASES
    norm_tickers, _entities = extractor.normalize_tickers_and_entities(
        ["Anthropic"],
        [],
        alias_map=alias_map,
    )
    assert norm_tickers == ["ANT"]

    norm_dedup, _ = extractor.normalize_tickers_and_entities(
        ["Anthropic", "ANT"],
        [],
        alias_map=alias_map,
    )
    assert norm_dedup == ["ANT"]

    quote = "Anthropic 发布了新一代企业级 Claude 模型，推动年化经常性收入高速增长。"
    kept, dropped = extractor.validate_tickers_against_quote(
        ["Anthropic"],
        quote,
        claim="Anthropic 新产品带动收入增长。",
        alias_map=alias_map,
    )
    assert dropped == 0
    assert kept == ["ANT"]

    final_tickers, added = extractor.backfill_tickers_from_quote(
        kept,
        quote,
        claim="Anthropic 新产品带动收入增长。",
        alias_map=alias_map,
    )
    assert added == 0
    assert final_tickers == ["ANT"]


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
    """Separation principle: package_release.sh denylist excludes dalton-richard-coverage-weekly
    and verify_release_tarball.inspect_tarball rejects any core tarball containing it."""
    import io
    import tarfile

    pkg_sh = (REPO_ROOT / "scripts" / "standalone" / "package_release.sh").read_text(encoding="utf-8")
    assert '"plugins/dalton/skills/dalton-richard-coverage-weekly"' in pkg_sh

    verify_mod = _load_module(
        "verify_release_tarball_sep_check",
        "scripts/standalone/verify_release_tarball.py",
    )
    tar_path = tmp_path / "bad_core.tar.gz"
    top = "dalton-core-test"
    with tarfile.open(tar_path, "w:gz") as tf:
        d_info = tarfile.TarInfo(name=top)
        d_info.type = tarfile.DIRTYPE
        tf.addfile(d_info)
        f_data = b"# exclusive\n"
        f_info = tarfile.TarInfo(
            name=f"{top}/plugins/dalton/skills/dalton-richard-coverage-weekly/scripts/screen_movers.py"
        )
        f_info.size = len(f_data)
        tf.addfile(f_info, io.BytesIO(f_data))

    violations = verify_mod.inspect_tarball(tar_path, expected_top_dir=top)
    assert any("dalton-richard-coverage-weekly" in v for v in violations)
