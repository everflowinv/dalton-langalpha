"""Effective browser concurrency observability (backlog 三-2 precondition + P0
fix1, 2026-10-28): the source resolver names which channel set the crawl
browser concurrency (code default / literal config / env-substituted config /
legacy key) by reading the raw pre-substitution YAML rather than guessing from
isinstance(value, str), and the getter still returns the same number as before."""

from __future__ import annotations

from pathlib import Path

import pytest

import src.config.tool_settings as tool_settings
from src.config.core import clear_config_cache
from src.config.tool_settings import (
    get_crawler_browser_concurrency,
    resolve_crawler_browser_concurrency,
)


@pytest.fixture(autouse=True)
def _agent_config(monkeypatch: pytest.MonkeyPatch):
    holder = {"config": {}}

    def _fake_loader():
        return holder["config"]

    monkeypatch.setattr(tool_settings, "_get_agent_config_dict", _fake_loader)
    return holder


def test_no_config_keys_fall_back_to_code_default() -> None:
    value, source = resolve_crawler_browser_concurrency()
    assert (value, source) == (2, "default")
    assert get_crawler_browser_concurrency() == 2


def _set_config(monkeypatch: pytest.MonkeyPatch, config: dict) -> None:
    monkeypatch.setattr(
        tool_settings, "_get_agent_config_dict", lambda: config, raising=True
    )


def test_literal_config_value_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, {"crawler": {"browser_concurrency": 6}})
    assert resolve_crawler_browser_concurrency() == (6, "config_file")
    assert get_crawler_browser_concurrency() == 6


def test_literal_yaml_string_is_classified_as_config_file_not_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # P0 fix1 10: a quoted literal string "6" in YAML must be reported as
    # config_file, never misreported as env_var.
    _set_config(monkeypatch, {"crawler": {"browser_concurrency": "6"}})
    assert resolve_crawler_browser_concurrency() == (6, "config_file")

    cfg_path = tmp_path / "agent_config.yaml"
    cfg_path.write_text('crawler:\n  browser_concurrency: "6"\n', encoding="utf-8")
    monkeypatch.setenv("PTC_CONFIG_FILE", str(cfg_path))
    monkeypatch.setattr(
        tool_settings, "_get_agent_config_dict", tool_settings.load_agent_config
    )
    clear_config_cache()
    try:
        assert resolve_crawler_browser_concurrency() == (6, "config_file")
        assert get_crawler_browser_concurrency() == 6
    finally:
        clear_config_cache()


def test_env_substituted_value_source_from_raw_yaml_placeholder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / "agent_config.yaml"
    cfg_path.write_text(
        "crawler:\n  browser_concurrency: ${DALTON_TEST_BROWSER_CONCURRENCY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PTC_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("DALTON_TEST_BROWSER_CONCURRENCY", "4")
    monkeypatch.setattr(
        tool_settings, "_get_agent_config_dict", tool_settings.load_agent_config
    )
    clear_config_cache()
    try:
        assert resolve_crawler_browser_concurrency() == (4, "env_var")
        assert get_crawler_browser_concurrency() == 4
    finally:
        clear_config_cache()


def test_overlay_null_does_not_clobber_base_env_var_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P0 fix2 F: raw YAML overlay merging must match `load_yaml_config`'s
    `_deep_merge` semantics — a `null` in the overlay does not overwrite the
    base `${VAR}` entry."""
    cfg_path = tmp_path / "agent_config.yaml"
    cfg_path.write_text(
        "crawler:\n  browser_concurrency: ${DALTON_TEST_BROWSER_CONCURRENCY}\n",
        encoding="utf-8",
    )
    overlay_path = tmp_path / "agent_config.test.yaml"
    overlay_path.write_text(
        "crawler:\n  browser_concurrency: null\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PTC_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DALTON_TEST_BROWSER_CONCURRENCY", "4")
    monkeypatch.setattr(
        tool_settings, "_get_agent_config_dict", tool_settings.load_agent_config
    )
    clear_config_cache()
    try:
        assert resolve_crawler_browser_concurrency() == (4, "env_var")
        assert get_crawler_browser_concurrency() == 4
    finally:
        clear_config_cache()


def test_legacy_key_capped_at_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, {"crawler": {"max_concurrent_crawls": 3}})
    value, source = resolve_crawler_browser_concurrency()
    assert (value, source) == (2, "config_file_legacy")
    _set_config(monkeypatch, {"crawler": {"max_concurrent_crawls": 1}})
    assert resolve_crawler_browser_concurrency() == (1, "config_file_legacy")


def test_explicit_key_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(
        monkeypatch,
        {"crawler": {"browser_concurrency": 5, "max_concurrent_crawls": 1}},
    )
    assert resolve_crawler_browser_concurrency() == (5, "config_file")


def test_pdf_render_concurrency_is_a_named_constant() -> None:
    from src.server.services.pdf_render import PDF_RENDER_CONCURRENCY

    assert PDF_RENDER_CONCURRENCY == 2
