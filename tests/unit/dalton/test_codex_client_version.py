"""Dalton pins the Codex client version: the codex backend lists GPT-6.2 only to clients >= 0.170.0."""

from src.dalton import settings
from src.dalton.llm import native_catalog
from src.llms.extension import codex


def test_request_path_and_model_list_share_the_dalton_version(monkeypatch) -> None:
    monkeypatch.delenv("DALTON_CODEX_CLIENT_VERSION", raising=False)
    assert settings.DEFAULT_CODEX_CLIENT_VERSION >= "0.170.0"
    assert codex.codex_client_version() == settings.DEFAULT_CODEX_CLIENT_VERSION
    assert codex.codex_identity_headers()["User-Agent"] == f"codex_cli_rs/{settings.DEFAULT_CODEX_CLIENT_VERSION}"
    assert native_catalog.get_codex_client_version() == codex.codex_client_version()


def test_env_override(monkeypatch) -> None:
    monkeypatch.setenv("DALTON_CODEX_CLIENT_VERSION", "0.171.2")
    assert codex.codex_client_version() == "0.171.2"
    assert native_catalog.get_codex_client_version() == "0.171.2"
