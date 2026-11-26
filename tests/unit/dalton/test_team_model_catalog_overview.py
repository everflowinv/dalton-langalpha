"""Unit tests for GET /api/v1/dalton/team-models overview edit-echo & metadata fields."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.services.team_model_catalog import compiled as compiled_mod
from src.dalton.services.team_model_catalog import service as service_mod
from src.dalton.services.team_model_catalog.compiled import (
    compile_catalog_from_payload,
    reset_catalog_caches_for_tests,
)
from src.dalton.services.team_model_catalog.constants import CATALOG_SCHEMA
from src.dalton.services.team_model_catalog.validation import _SUPPORTED_VARIANT_SDKS


@pytest.fixture(autouse=True)
def _reset_catalog_state():
    reset_catalog_caches_for_tests()
    try:
        yield
    finally:
        reset_catalog_caches_for_tests()


def _sample_upstream_models() -> dict[str, Any]:
    return {
        "gpt-5": {
            "model_id": "gpt-5",
            "provider": "openai",
            "display_name": "GPT-5",
            "visible": True,
            "context": 400000,
            "input_modalities": ["text", "image"],
            "prompt_guidance": "detailed",
            "parameters": {
                "max_tokens": 16384,
                "reasoning": {"effort": "medium"},
                "api_key": "sk-should-never-leak",
                "default_headers": {"Authorization": "Bearer secret-header"},
                "nested": {
                    "temperature": 0.2,
                    "client_secret": "top-secret",
                    "access_token": "tok-123",
                },
            },
        },
        "claude-test-oauth": {
            "model_id": "claude-test",
            "provider": "claude-oauth",
            "display_name": "Claude Test OAuth",
            "visible": True,
            "context": 200000,
            "input_modalities": ["text"],
        },
    }


def _sample_upstream_providers() -> dict[str, Any]:
    return {
        "provider_config": {
            "openai": {
                "display_name": "OpenAI",
                "sdk": "openai",
                "env_key": "OPENAI_API_KEY",
                "base_url": "https://api.openai.com/v1",
                "access_type": "api_key",
                "billing": "cash",
            },
            "anthropic": {
                "display_name": "Anthropic",
                "sdk": "anthropic",
                "env_key": "ANTHROPIC_API_KEY",
                "base_url": "https://api.anthropic.com",
                "access_type": "api_key",
                "billing": "cash",
            },
            "z-ai": {
                "display_name": "Z.AI",
                "sdk": "openai",
                "env_key": "ZAI_API_KEY",
                "base_url": "https://api.z.ai/v1",
                "access_type": "api_key",
                "billing": "cash",
            },
            "claude-oauth": {
                "display_name": "Claude OAuth",
                "sdk": "anthropic",
                "access_type": "oauth",
                "billing": "subscription",
            },
        },
        "models": {
            "openai": [
                {
                    "id": "gpt-5",
                    "name": "GPT-5",
                    "alias": ["gpt-5-latest", "gpt-5-2026"],
                    "pricing": {"input": 2.5, "cached_input": 0.25, "output": 10.0},
                    "pricing_meta": {"source_kind": "official", "verified_at": "2026-11-24"},
                }
            ]
        },
    }


def _patch_overview_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    compiled: Any,
    *,
    creds: dict[str, dict[str, Any]] | None = None,
) -> None:
    get_compiled_mock = create_autospec(compiled_mod.get_compiled_catalog_async, return_value=compiled)
    list_creds_mock = create_autospec(
        service_mod.list_catalog_provider_credentials,
        return_value=creds or {},
    )
    get_row_mock = create_autospec(
        service_mod.get_catalog_row,
        return_value={
            "revision": compiled.revision,
            "legacy_sync_import_status": compiled.legacy_sync_import_status,
            "legacy_sync_import_error": None,
        },
    )
    monkeypatch.setattr(service_mod, "get_compiled_catalog_async", get_compiled_mock)
    monkeypatch.setattr(service_mod, "list_catalog_provider_credentials", list_creds_mock)
    monkeypatch.setattr(service_mod, "get_catalog_row", get_row_mock)


def test_overview_echoes_edit_fields_strips_credentials_and_exposes_server_variant_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "models": {
            "acme-fast": {
                "model_id": "acme-fast-v1",
                "provider": "acme-gw",
                "display_name": "Acme Fast",
                "visible": True,
                "prompt_guidance": "lean",
                "context": 128000,
                "parameters": {"max_output_tokens": 8192, "top_p": 0.9},
            }
        },
        "providers": {
            "provider_variants": {
                "openai": {
                    "acme-gw": {
                        "display_name": "Acme Gateway",
                        "sdk": "openai",
                        "parent_provider": "openai",
                        "access_type": "api_key",
                        "billing": "cash",
                        "base_url": "https://gw.acme.example.com/v1",
                    }
                }
            },
            "models": {
                "acme-gw": [
                    {
                        "id": "acme-fast-v1",
                        "name": "Acme Fast",
                        "alias": ["acme-fast-alias"],
                        "pricing": {"input": 1.0, "output": 4.0},
                    }
                ]
            },
        },
        "task_costs": {
            "bounds": {
                "gpt-5": {"model": "claude-sonnet-4-5", "times": 0.75},
                "acme-fast": {"model": "unmeasured-ref", "times": 1.25},
            }
        },
    }
    compiled = compile_catalog_from_payload(
        payload,
        revision=9,
        oauth_sync_enabled=True,
        native_overlay_override={},
        upstream_models_override=_sample_upstream_models(),
        upstream_providers_override=_sample_upstream_providers(),
    )
    _patch_overview_dependencies(
        monkeypatch,
        compiled,
        creds={
            "acme-gw": {
                "provider_slug": "acme-gw",
                "key_fingerprint": "sk-...9a2f",
                "bound_base_url": "https://gw.acme.example.com/v1",
                "canonical_bound_url": "https://gw.acme.example.com/v1",
                "updated_at": "2026-11-25T10:00:00Z",
                "encrypted_api_key": "cipher-blob-must-not-appear",
            }
        },
    )

    overview = asyncio.run(service_mod.get_team_catalog_overview())

    assert overview["parent_provider_groups"] == ["anthropic", "claude-oauth", "openai", "z-ai"]
    assert overview["supported_variant_sdks"] == sorted(_SUPPORTED_VARIANT_SDKS)

    by_key = {m["model_key"]: m for m in overview["models"]}
    gpt5 = by_key["gpt-5"]
    assert gpt5["prompt_guidance"] == "detailed"
    assert gpt5["alias"] == ["gpt-5-latest", "gpt-5-2026"]
    assert gpt5["task_costs_bound"] == {"model": "claude-sonnet-4-5", "times": 0.75}
    assert gpt5["parameters"] == {
        "max_tokens": 16384,
        "reasoning": {"effort": "medium"},
        "nested": {"temperature": 0.2},
    }

    acme_fast = by_key["acme-fast"]
    assert acme_fast["prompt_guidance"] == "lean"
    assert acme_fast["alias"] == ["acme-fast-alias"]
    assert acme_fast["parameters"] == {"max_output_tokens": 8192, "top_p": 0.9}
    assert acme_fast["task_costs_bound"] == {"model": "unmeasured-ref", "times": 1.25}

    serialized = str(overview)
    for forbidden in (
        "sk-should-never-leak",
        "secret-header",
        "top-secret",
        "tok-123",
        "cipher-blob-must-not-appear",
    ):
        assert forbidden not in serialized


def test_overview_omits_oauth_parent_provider_groups_when_oauth_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = compile_catalog_from_payload(
        {"schema": CATALOG_SCHEMA},
        revision=2,
        oauth_sync_enabled=False,
        native_overlay_override={},
        upstream_models_override=_sample_upstream_models(),
        upstream_providers_override=_sample_upstream_providers(),
    )
    _patch_overview_dependencies(monkeypatch, compiled)

    overview = asyncio.run(service_mod.get_team_catalog_overview())

    assert overview["parent_provider_groups"] == ["anthropic", "openai", "z-ai"]
    assert all(not g.endswith("-oauth") for g in overview["parent_provider_groups"])
    assert overview["supported_variant_sdks"] == sorted(_SUPPORTED_VARIANT_SDKS)
