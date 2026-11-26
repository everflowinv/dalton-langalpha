"""Batch 3 unit tests for Team Model Catalog:
- Billing regression guard: unmodified ``find_model_pricing`` stops scan on priceless hit
- Cash price-source badge resolved by object identity from ``find_model_pricing`` result
  (parent-provider fallback, base-model fallback, empty ``{}`` base pricing omits badge)
- Subscription price-source badge resolved from ``resolve_notional_card`` (including
  ``notional_ref`` lookup order parity and ``z-ai-cn-coding`` subscription billing via
  ``resolve_model_billing``)
- FastAPI TestClient anonymous HTTP assertions, full-response parity when Dalton is disabled,
  and graceful 200 degradation on annotation errors
- Migration ``d055_drop_model_price_alerts`` offline SQL & single Dalton Alembic head
"""

from __future__ import annotations

import importlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.dalton.llm.notional_pricing import (
    _canonical_card_signature,
    build_notional_price_index_for_catalog,
    resolve_notional_card,
)
from src.dalton.services.team_model_catalog import compiled as compiled_mod
from src.dalton.services.team_model_catalog.compiled import (
    CompiledCatalog,
    compile_catalog_from_payload,
)
from src.dalton.services.team_model_catalog.constants import empty_catalog_payload
from src.dalton.services.team_model_catalog.service import (
    annotate_model_metadata_price_source,
    clear_price_source_cache,
)
from src.llms.llm import LLM, ModelConfig
from src.llms.pricing_utils import find_model_pricing

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_autospec_model_config(
    *,
    llm_config: dict,
    flat_providers: dict,
    manifest_models: dict,
) -> ModelConfig:
    mc = create_autospec(ModelConfig, instance=True)
    mc.llm_config = llm_config
    mc.manifest = {"models": manifest_models}
    mc.get_provider_info.side_effect = lambda prov: flat_providers.get(prov, {})
    mc.get_parent_provider.side_effect = lambda prov: flat_providers.get(prov, {}).get(
        "parent_provider", prov
    )
    return mc


def test_find_model_pricing_priceless_card_stops_same_provider_scan_regression() -> None:
    """When a provider lists a priceless matching card ahead of a priced alias card,
    ``find_model_pricing`` must stop the pass on the priceless hit and fall back to
    parent / base / None (matching f0e5497ba behavior)."""
    mc_no_parent = _make_autospec_model_config(
        llm_config={"foo": {"provider": "acme", "model_id": "foo"}},
        flat_providers={"acme": {"billing": "cash", "parent_provider": "acme"}},
        manifest_models={
            "acme": [
                {"id": "foo"},  # priceless match ends the pass
                {"id": "bar", "alias": ["foo"], "pricing": {"input": 1.0, "output": 2.0}},
            ],
        },
    )
    assert find_model_pricing("foo", "acme", _warn_on_miss=False, model_config=mc_no_parent) is None

    mc_with_parent = _make_autospec_model_config(
        llm_config={"foo": {"provider": "acme-cn", "model_id": "foo"}},
        flat_providers={
            "acme": {"billing": "cash", "parent_provider": "acme"},
            "acme-cn": {"billing": "cash", "parent_provider": "acme"},
        },
        manifest_models={
            "acme-cn": [
                {"id": "foo", "pricing": None},
                {"id": "bar", "alias": ["foo"], "pricing": {"input": 1.0, "output": 1.0}},
            ],
            "acme": [
                {"id": "foo", "pricing": {"input": 2.0, "output": 2.0}},
            ],
        },
    )
    assert find_model_pricing("foo", "acme-cn", _warn_on_miss=False, model_config=mc_with_parent) == {
        "input": 2.0,
        "output": 2.0,
    }


def test_annotate_model_metadata_price_source_only_exposes_source_fields() -> None:
    mc = _make_autospec_model_config(
        llm_config={
            "gpt-official": {"provider": "openai", "model_id": "gpt-official"},
            "router-tp": {"provider": "openrouter", "model_id": "vendor/model-a"},
            "glm-plan": {"provider": "z-ai-cn-coding", "model_id": "glm-5.4"},
            "zai-no-meta": {"provider": "z-ai-cn-coding", "model_id": "glm-unlisted"},
            "plain-model": {"provider": "openai", "model_id": "plain-model"},
        },
        flat_providers={
            "openai": {"billing": "cash", "sdk": "openai"},
            "openrouter": {"billing": "cash", "sdk": "openai"},
            # Deliberately omit billing="subscription" to verify resolve_model_billing handles z-ai-cn-coding
            "z-ai-cn-coding": {"sdk": "openai"},
        },
        manifest_models={
            "openai": [
                {
                    "id": "gpt-official",
                    "pricing": {"input": 2.0, "output": 10.0},
                    "pricing_meta": {
                        "source_kind": "official",
                        "estimated": False,
                        "source_url": "https://example.com/internal",
                        "note": "private admin note",
                    },
                },
                {
                    "id": "plain-model",
                    "pricing": {"input": 1.0, "output": 2.0},
                },
            ],
            "openrouter": [
                {
                    "id": "vendor/model-a",
                    "pricing": {"input": 3.0, "output": 12.0},
                    "pricing_meta": {
                        "source_kind": "third_party",
                        "estimated": True,
                        "verified_at": "2026-11-26",
                    },
                },
            ],
            "z-ai-cn-coding": [
                {
                    "id": "glm-5.4",
                    "pricing": {"input": 0.0, "output": 0.0},
                    # Cash card metadata must NOT leak onto subscription models
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
                {
                    "id": "glm-unlisted",
                    "pricing": {"input": 0.0, "output": 0.0},
                    # Even without dalton_meta, z-ai-cn-coding is subscription-billed via resolve_model_billing
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
            ],
        },
    )

    dalton_meta = {
        "glm-plan": {
            "billing": "subscription",
            "notional_pricing": {
                "input": 1.4,
                "output": 4.4,
                "cached_input": 0.29,
                "estimated": True,
                "source_kind": "estimated",
                "source": "internal estimate",
                "note": "do not expose note",
            },
        },
    }

    base_metadata = {
        "gpt-official": {"provider": "openai", "sdk": "openai", "pricing": {"input": 2.0, "output": 10.0}},
        "router-tp": {
            "provider": "openrouter",
            "model_id": "vendor/model-a",
            "sdk": "openai",
            "pricing": {"input": 3.0, "output": 12.0},
        },
        "glm-plan": {"provider": "z-ai-cn-coding", "model_id": "glm-5.4", "sdk": "openai"},
        "zai-no-meta": {"provider": "z-ai-cn-coding", "model_id": "glm-unlisted", "sdk": "openai"},
        "plain-model": {"provider": "openai", "sdk": "openai", "pricing": {"input": 1.0, "output": 2.0}},
    }

    annotated = annotate_model_metadata_price_source(
        base_metadata,
        model_config=mc,
        dalton_meta=dalton_meta,
    )

    assert annotated["gpt-official"]["pricing_meta"] == {
        "source_kind": "official",
        "estimated": False,
    }
    assert "notional_pricing" not in annotated["gpt-official"]

    assert annotated["router-tp"]["pricing_meta"] == {
        "source_kind": "third_party",
        "estimated": True,
    }

    # Subscription notional_pricing exposes ONLY source_kind / estimated — zero numeric prices
    assert annotated["glm-plan"]["notional_pricing"] == {
        "source_kind": "estimated",
        "estimated": True,
    }
    assert set(annotated["glm-plan"]["notional_pricing"].keys()) <= {"source_kind", "estimated"}
    assert "pricing_meta" not in annotated["glm-plan"]

    # Hardcoded z-ai-cn-coding subscription without dalton_meta MUST NOT show cash pricing_meta badge
    assert "pricing_meta" not in annotated["zai-no-meta"]
    assert "notional_pricing" not in annotated["zai-no-meta"]

    assert "pricing_meta" not in annotated["plain-model"]
    assert "notional_pricing" not in annotated["plain-model"]


def test_annotate_model_metadata_price_source_matches_same_card_across_fallbacks_and_notional_ref() -> None:
    """Verify badge comes from the exact card providing the rate across:
    - parent provider fallback
    - base model version fallback
    - base model with empty ``pricing: {}`` (omits badge, matching ``find_model_pricing`` -> None)
    - ``notional_ref`` matching ``resolve_notional_card`` lookup order (including dot-vs-hyphen provider order)
    """
    mc = _make_autospec_model_config(
        llm_config={
            "regional-variant": {"provider": "acme-cn", "model_id": "acme-pro"},
            "snapshot-version": {"provider": "acme", "model_id": "acme-pro-0905"},
            "snapshot-empty-base": {"provider": "acme", "model_id": "acme-empty-0905"},
            "alias-cash": {"provider": "acme", "model_id": "acme-1.0"},
            "glm-5.4": {"provider": "z-ai", "model_id": "glm-5.4"},
            "glm-5.4-plan": {"provider": "z-ai-cn-coding", "model_id": "glm-5.4"},
            "chained-sub": {"provider": "z-ai-cn-coding", "model_id": "glm-5.4-chain"},
            "ref-order-sub": {"provider": "z-ai-cn-coding", "model_id": "ref-order-model"},
        },
        flat_providers={
            "acme": {"billing": "cash", "parent_provider": "acme"},
            "acme-cn": {"billing": "cash", "parent_provider": "acme"},
            "z-ai": {"billing": "cash", "parent_provider": "z-ai"},
            "z-ai-cn-coding": {"billing": "subscription", "parent_provider": "z-ai"},
        },
        manifest_models={
            # Provider listed first with dot-normalized match for notional_ref "ref-1.0"
            "first-prov": [
                {
                    "id": "ref-1-0",
                    "pricing": {"input": 1.1, "output": 3.3},
                    "pricing_meta": {"source_kind": "estimated", "estimated": True},
                },
            ],
            "second-prov": [
                {
                    "id": "ref-1.0",
                    "pricing": {"input": 9.9, "output": 19.9},
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
            ],
            "acme": [
                {
                    "id": "acme-pro",
                    "pricing": {"input": 4.0, "output": 16.0},
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
                {
                    "id": "acme-empty",
                    "pricing": {},
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
                {
                    "id": "acme-canonical",
                    "alias": ["acme-1-0"],
                    "pricing": {"input": 1.0, "output": 4.0},
                    "pricing_meta": {"source_kind": "third_party"},
                },
            ],
            "z-ai": [
                {
                    "id": "glm-5.4",
                    "pricing": {"input": 0.9333, "output": 2.9333},
                    "pricing_meta": {"source_kind": "estimated", "estimated": True},
                },
            ],
        },
    )

    dalton_meta = {
        "glm-5.4-plan": {
            "billing": "subscription",
            "notional_ref": "glm-5.4",
        },
        "chained-sub": {
            "billing": "subscription",
            "notional_ref": "glm-5.4-plan",
        },
        "ref-order-sub": {
            "billing": "subscription",
            "notional_ref": "ref-1.0",
        },
    }

    base_metadata = {
        "regional-variant": {
            "provider": "acme-cn",
            "model_id": "acme-pro",
            "pricing": {"input": 4.0, "output": 16.0},
        },
        "snapshot-version": {
            "provider": "acme",
            "model_id": "acme-pro-0905",
            "pricing": {"input": 4.0, "output": 16.0},
        },
        "snapshot-empty-base": {"provider": "acme", "model_id": "acme-empty-0905"},
        "alias-cash": {
            "provider": "acme",
            "model_id": "acme-1.0",
            "pricing": {"input": 1.0, "output": 4.0},
        },
        "glm-5.4-plan": {"provider": "z-ai-cn-coding", "model_id": "glm-5.4"},
        "chained-sub": {"provider": "z-ai-cn-coding", "model_id": "glm-5.4-chain"},
        "ref-order-sub": {"provider": "z-ai-cn-coding", "model_id": "ref-order-model"},
    }

    # Base model with empty pricing `{}` returns None from find_model_pricing -> no badge
    assert find_model_pricing("acme-empty-0905", "acme", _warn_on_miss=False, model_config=mc) is None

    annotated = annotate_model_metadata_price_source(
        base_metadata,
        model_config=mc,
        dalton_meta=dalton_meta,
    )

    assert annotated["regional-variant"]["pricing_meta"] == {"source_kind": "official", "estimated": False}
    assert annotated["snapshot-version"]["pricing_meta"] == {"source_kind": "official", "estimated": False}
    assert "pricing_meta" not in annotated["snapshot-empty-base"]
    assert annotated["alias-cash"]["pricing_meta"] == {"source_kind": "third_party"}
    assert annotated["glm-5.4-plan"]["notional_pricing"] == {"source_kind": "estimated", "estimated": True}
    assert annotated["chained-sub"]["notional_pricing"] == {"source_kind": "estimated", "estimated": True}
    # Must match first-prov's card (same card chosen by resolve_notional_card), NOT second-prov's official card
    assert annotated["ref-order-sub"]["notional_pricing"] == {"source_kind": "estimated", "estimated": True}


def test_oauth_sync_enabled_parity_between_badge_and_notional_billing() -> None:
    """Badge index must honor the exact ``oauth_sync_enabled`` flag of ``CompiledCatalog``
    so badge selection and notional billing pick the identical card in both True and False states."""
    mc = _make_autospec_model_config(
        llm_config={
            # Has a fallback card in _FALLBACK_OAUTH_CARDS when oauth_sync_enabled=True,
            # and no direct dalton_meta entry under its own model_key.
            "gemini-4-neon-high-oauth": {
                "provider": "antigravity-oauth",
                "model_id": "gemini-4-neon-2",
            },
        },
        flat_providers={
            "antigravity-oauth": {"billing": "subscription", "sdk": "google"},
        },
        manifest_models={},
    )
    # Bare model_id entry in dalton_meta (reachable via resolve_notional_card candidate scan
    # ONLY when _FALLBACK_OAUTH_CARDS does not populate the pair/model key).
    dalton_meta = {
        "gemini-4-neon-2": {
            "billing": "subscription",
            "notional_pricing": {
                "input": 9.0,
                "output": 18.0,
                "cached_input": 0.9,
                "source_kind": "third_party",
                "estimated": False,
            },
        },
    }
    metadata = {
        "gemini-4-neon-high-oauth": {
            "provider": "antigravity-oauth",
            "model_id": "gemini-4-neon-2",
        },
    }

    for oauth_enabled in (True, False):
        billing_idx = build_notional_price_index_for_catalog(
            model_config=mc,
            dalton_meta=dalton_meta,
            oauth_sync_enabled=oauth_enabled,
        )
        source_idx = build_notional_price_index_for_catalog(
            model_config=mc,
            dalton_meta=dalton_meta,
            oauth_sync_enabled=oauth_enabled,
            include_source_meta=True,
        )
        billing_card = resolve_notional_card(
            "gemini-4-neon-high-oauth",
            pricing_model_id="gemini-4-neon-2",
            pricing_provider="antigravity-oauth",
            index=billing_idx,
            model_config=mc,
        )
        source_card = resolve_notional_card(
            "gemini-4-neon-high-oauth",
            pricing_model_id="gemini-4-neon-2",
            pricing_provider="antigravity-oauth",
            index=source_idx,
            model_config=mc,
        )
        assert _canonical_card_signature(source_card) == _canonical_card_signature(billing_card)

        annotated = annotate_model_metadata_price_source(
            metadata,
            model_config=mc,
            dalton_meta=dalton_meta,
            oauth_sync_enabled=oauth_enabled,
        )
        if oauth_enabled:
            # With oauth_sync_enabled=True, _FALLBACK_OAUTH_CARDS wins (3.0/15.0, no source_kind)
            # -> badge MUST NOT steal third_party from the bare dalton_meta candidate.
            assert billing_card["input"] == 3.0
            assert "notional_pricing" not in annotated["gemini-4-neon-high-oauth"]
        else:
            # With oauth_sync_enabled=False, _FALLBACK_OAUTH_CARDS is excluded, so both billing
            # and badge resolve to the bare dalton_meta card (9.0/18.0, third_party).
            assert billing_card["input"] == 9.0
            assert annotated["gemini-4-neon-high-oauth"]["notional_pricing"] == {
                "source_kind": "third_party",
                "estimated": False,
            }

    # Verify full compiled catalog parity across both oauth_sync_enabled=True and False
    for oauth_enabled in (True, False):
        compiled = compile_catalog_from_payload(
            empty_catalog_payload(),
            revision=21 if oauth_enabled else 22,
            oauth_sync_enabled=oauth_enabled,
        )
        source_idx = build_notional_price_index_for_catalog(
            model_config=compiled.model_config,
            dalton_meta=compiled.dalton_meta,
            oauth_sync_enabled=compiled.oauth_sync_enabled,
            include_source_meta=True,
        )
        for key, cfg in compiled.model_config.llm_config.items():
            if not isinstance(cfg, dict):
                continue
            prov = str(cfg.get("provider") or "")
            mid = str(cfg.get("model_id") or key)
            billing_card = resolve_notional_card(
                key,
                pricing_model_id=mid,
                pricing_provider=prov or None,
                index=compiled.notional_price_index,
                model_config=compiled.model_config,
            )
            source_card = resolve_notional_card(
                key,
                pricing_model_id=mid,
                pricing_provider=prov or None,
                index=source_idx,
                model_config=compiled.model_config,
            )
            assert _canonical_card_signature(source_card) == _canonical_card_signature(billing_card)


def test_cash_badge_requires_displayed_pricing_and_matches_get_list_rates_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cash ``pricing_meta`` is attached ONLY when ``entry['pricing']`` is present (a cash rate
    is displayed), and calls ``find_model_pricing`` with the same parameters as ``get_list_rates``."""
    from src.llms import pricing_utils as pricing_utils_mod

    payload = empty_catalog_payload()
    payload["models"] = {
        "b3-input-only": {
            "model_id": "b3-input-only-id",
            "provider": "openai",
            "display_name": "B3 Input Only",
            "visible": True,
        },
        "b3-full-cash": {
            "model_id": "b3-full-cash-id",
            "provider": "openai",
            "display_name": "B3 Full Cash",
            "visible": True,
        },
    }
    payload["providers"] = {
        "models": {
            "openai": [
                {
                    "id": "b3-input-only-id",
                    "pricing": {"input": 2.0},  # missing output -> get_list_rates returns None
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
                {
                    "id": "b3-full-cash-id",
                    "pricing": {"input": 2.0, "output": 8.0},
                    "pricing_meta": {"source_kind": "official", "estimated": False},
                },
            ],
        },
    }
    compiled = compile_catalog_from_payload(payload, revision=31)
    raw_meta = compiled.model_config.get_model_metadata()
    assert "pricing" not in raw_meta["b3-input-only"]
    assert raw_meta["b3-full-cash"]["pricing"] == {"input": 2.0, "output": 8.0}

    clear_price_source_cache()
    annotated_cached = annotate_model_metadata_price_source(
        raw_meta,
        model_config=compiled.model_config,
        dalton_meta=compiled.dalton_meta,
        compiled_catalog=compiled,
    )
    assert "pricing_meta" not in annotated_cached["b3-input-only"]
    assert annotated_cached["b3-full-cash"]["pricing_meta"] == {
        "source_kind": "official",
        "estimated": False,
    }

    # Direct call without compiled_catalog: verify find_model_pricing args match get_list_rates
    # (no model_key kwarg) and skipped entirely when entry lacks pricing.
    real_find = pricing_utils_mod.find_model_pricing
    recorded_calls: list[tuple[tuple, dict]] = []

    def _spy_find(*args, **kwargs):
        recorded_calls.append((args, kwargs))
        return real_find(*args, **kwargs)

    spy_find = create_autospec(real_find, spec_set=True, side_effect=_spy_find)
    monkeypatch.setattr(pricing_utils_mod, "find_model_pricing", spy_find)

    subset_meta = {
        "b3-input-only": dict(raw_meta["b3-input-only"]),
        "b3-full-cash": dict(raw_meta["b3-full-cash"]),
    }
    annotated_direct = annotate_model_metadata_price_source(
        subset_meta,
        model_config=compiled.model_config,
        dalton_meta=compiled.dalton_meta,
    )
    assert "pricing_meta" not in annotated_direct["b3-input-only"]
    assert annotated_direct["b3-full-cash"]["pricing_meta"] == {
        "source_kind": "official",
        "estimated": False,
    }
    assert len(recorded_calls) == 1
    args, kwargs = recorded_calls[0]
    assert args == ("b3-full-cash-id", "openai")
    assert "model_key" not in kwargs
    assert kwargs.get("model_config") is compiled.model_config


def _build_b3_compiled_catalog(
    *,
    revision: int = 7,
    cash_source_kind: str = "third_party",
) -> CompiledCatalog:
    payload = empty_catalog_payload()
    payload["models"] = {
        "b3-cash-model": {
            "model_id": "b3-cash-id",
            "provider": "openai",
            "display_name": "B3 Cash",
            "visible": True,
        },
        "b3-snapshot-model": {
            "model_id": "b3-cash-id-0905",
            "provider": "openai",
            "display_name": "B3 Cash Snapshot",
            "visible": True,
        },
        "b3-sub-inline": {
            "model_id": "b3-sub-inline-id",
            "provider": "z-ai-cn-coding",
            "display_name": "B3 Sub Inline",
            "visible": True,
        },
        "b3-sub-ref": {
            "model_id": "b3-sub-ref-id",
            "provider": "z-ai-cn-coding",
            "display_name": "B3 Sub Ref",
            "visible": True,
        },
    }
    payload["providers"] = {
        "models": {
            "openai": [
                {
                    "id": "b3-cash-id",
                    "pricing": {"input": 2.0, "output": 8.0, "unit": "per_1m_tokens"},
                    "pricing_meta": {
                        "source_kind": cash_source_kind,
                        "estimated": False,
                        "note": "private-note-should-not-leak",
                    },
                },
            ],
        },
    }
    payload["dalton_meta"] = {
        "b3-sub-inline": {
            "billing": "subscription",
            "notional_pricing": {
                "input": 1.4,
                "output": 4.4,
                "cached_input": 0.29,
                "estimated": True,
                "source_kind": "estimated",
                "note": "private-sub-note",
            },
        },
        "b3-sub-ref": {
            "billing": "subscription",
            "notional_ref": "b3-cash-model",
        },
    }
    return compile_catalog_from_payload(payload, revision=revision)


def _mount_models_test_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compiled: CompiledCatalog,
    dalton_enabled: bool,
) -> TestClient:
    from src.dalton import settings as dalton_settings_mod
    from src.server.app import api_keys as api_keys_mod

    monkeypatch.setattr(LLM, "_model_config", LLM._model_config)
    monkeypatch.setattr(
        compiled_mod,
        "get_compiled_catalog_async",
        create_autospec(
            compiled_mod.get_compiled_catalog_async,
            spec_set=True,
            side_effect=AsyncMock(return_value=compiled),
        ),
    )
    monkeypatch.setattr(
        dalton_settings_mod,
        "is_dalton_enabled",
        create_autospec(
            dalton_settings_mod.is_dalton_enabled,
            spec_set=True,
            return_value=dalton_enabled,
        ),
    )

    app = FastAPI()
    app.include_router(api_keys_mod.router)
    return TestClient(app)


def test_http_get_models_anonymous_only_exposes_source_fields_and_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled = _build_b3_compiled_catalog()
    client = _mount_models_test_client(monkeypatch, compiled=compiled, dalton_enabled=True)

    res = client.get("/api/v1/models")
    assert res.status_code == 200
    meta = res.json()["model_metadata"]

    assert meta["b3-cash-model"]["pricing_meta"] == {
        "source_kind": "third_party",
        "estimated": False,
    }
    assert meta["b3-snapshot-model"]["pricing_meta"] == {
        "source_kind": "third_party",
        "estimated": False,
    }

    # Anonymous response MUST NOT leak numeric notional rates for subscription models
    sub_inline_np = meta["b3-sub-inline"]["notional_pricing"]
    assert sub_inline_np == {"source_kind": "estimated", "estimated": True}
    assert set(sub_inline_np.keys()) <= {"source_kind", "estimated"}
    assert not any(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in sub_inline_np.values()
    )

    sub_ref_np = meta["b3-sub-ref"]["notional_pricing"]
    assert sub_ref_np == {"source_kind": "third_party", "estimated": False}
    assert set(sub_ref_np.keys()) <= {"source_kind", "estimated"}


def test_http_get_models_zero_db_queries_and_caches_by_catalog_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /api/v1/models`` must perform zero DB queries during metadata & price-source
    annotation, cache computed badges per catalog revision, and recompute when revision changes."""
    import psycopg

    from src.dalton.database import team_model_catalog as db_catalog_mod
    from src.dalton.llm.role_policy import clear_tool_capability_cache
    from src.dalton.services.team_model_catalog import service as catalog_service_mod
    from src.server.database import pool as pool_mod

    clear_price_source_cache()
    clear_tool_capability_cache()
    compiled_v7 = _build_b3_compiled_catalog(revision=7, cash_source_kind="third_party")
    compiled_v8 = _build_b3_compiled_catalog(revision=8, cash_source_kind="official")
    current_compiled = {"value": compiled_v7}

    real_get_compiled_async = compiled_mod.get_compiled_catalog_async
    client = _mount_models_test_client(monkeypatch, compiled=compiled_v7, dalton_enabled=True)
    monkeypatch.setattr(
        compiled_mod,
        "get_compiled_catalog_async",
        create_autospec(
            real_get_compiled_async,
            spec_set=True,
            side_effect=AsyncMock(side_effect=lambda **kwargs: current_compiled["value"]),
        ),
    )

    # Wrap all DB connection / sync catalog entry points with create_autospec and assert 0 calls.
    psycopg_connect_spy = create_autospec(
        psycopg.connect,
        spec_set=True,
        side_effect=AssertionError("unexpected sync DB connect during GET /api/v1/models"),
    )
    head_sync_spy = create_autospec(
        db_catalog_mod.get_catalog_head_sync,
        spec_set=True,
        side_effect=AssertionError("unexpected get_catalog_head_sync during GET /api/v1/models"),
    )
    row_sync_spy = create_autospec(
        db_catalog_mod.get_catalog_row_sync,
        spec_set=True,
        side_effect=AssertionError("unexpected get_catalog_row_sync during GET /api/v1/models"),
    )
    pool_conn_spy = create_autospec(
        pool_mod.get_db_connection,
        spec_set=True,
        side_effect=AssertionError("unexpected get_db_connection during GET /api/v1/models"),
    )
    monkeypatch.setattr(psycopg, "connect", psycopg_connect_spy)
    monkeypatch.setattr(db_catalog_mod, "get_catalog_head_sync", head_sync_spy)
    monkeypatch.setattr(db_catalog_mod, "get_catalog_row_sync", row_sync_spy)
    monkeypatch.setattr(pool_mod, "get_db_connection", pool_conn_spy)

    real_compute = catalog_service_mod._compute_price_source_for_entries
    compute_spy = create_autospec(
        real_compute,
        spec_set=True,
        side_effect=real_compute,
    )
    monkeypatch.setattr(catalog_service_mod, "_compute_price_source_for_entries", compute_spy)

    # 1st request on revision 7: computes once, 0 DB calls
    res1 = client.get("/api/v1/models")
    assert res1.status_code == 200
    assert res1.json()["model_metadata"]["b3-cash-model"]["pricing_meta"] == {
        "source_kind": "third_party",
        "estimated": False,
    }
    assert compute_spy.call_count == 1
    assert psycopg_connect_spy.call_count == 0
    assert head_sync_spy.call_count == 0
    assert row_sync_spy.call_count == 0
    assert pool_conn_spy.call_count == 0

    # 2nd request on same revision 7: hits in-process revision cache (no recomputation, 0 DB calls)
    res2 = client.get("/api/v1/models")
    assert res2.status_code == 200
    assert res2.json()["model_metadata"]["b3-cash-model"]["pricing_meta"] == {
        "source_kind": "third_party",
        "estimated": False,
    }
    assert compute_spy.call_count == 1
    assert psycopg_connect_spy.call_count == 0
    assert head_sync_spy.call_count == 0
    assert row_sync_spy.call_count == 0
    assert pool_conn_spy.call_count == 0

    # 3rd request on revision 8: cache miss -> recomputes once with new source_kind, 0 DB calls
    current_compiled["value"] = compiled_v8
    res3 = client.get("/api/v1/models")
    assert res3.status_code == 200
    assert res3.json()["model_metadata"]["b3-cash-model"]["pricing_meta"] == {
        "source_kind": "official",
        "estimated": False,
    }
    assert compute_spy.call_count == 2
    assert psycopg_connect_spy.call_count == 0
    assert head_sync_spy.call_count == 0
    assert row_sync_spy.call_count == 0
    assert pool_conn_spy.call_count == 0


def test_resolve_model_billing_outside_scope_keeps_load_dalton_meta_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside ``bind_billing_dalton_meta``, calling ``resolve_model_billing`` without an
    explicit ``dalton_meta`` (even with a compiled ``ModelConfig``) must still fall back to
    ``load_dalton_meta()`` identically to pre-Batch-3 behavior."""
    from src.dalton.llm import overlay as overlay_mod
    from src.dalton.services.team_model_catalog.billing import (
        bind_billing_dalton_meta,
        resolve_model_billing,
    )

    compiled = _build_b3_compiled_catalog(revision=9)
    real_load = overlay_mod.load_dalton_meta
    load_spy = create_autospec(
        real_load,
        spec_set=True,
        return_value={"b3-cash-model": {"billing": "subscription"}},
    )
    monkeypatch.setattr(overlay_mod, "load_dalton_meta", load_spy)

    # Outside scope: must call load_dalton_meta() and honor its result
    outside_before = resolve_model_billing(
        model_key="b3-cash-model",
        provider="openai",
        model_id="b3-cash-id",
        model_config=compiled.model_config,
    )
    assert outside_before == "subscription"
    assert load_spy.call_count == 1

    # Inside scope: uses bound dalton_meta without calling load_dalton_meta()
    with bind_billing_dalton_meta({"b3-cash-model": {"billing": "cash"}}):
        inside = resolve_model_billing(
            model_key="b3-cash-model",
            provider="openai",
            model_id="b3-cash-id",
            model_config=compiled.model_config,
        )
    assert inside == "cash"
    assert load_spy.call_count == 1

    # After exiting scope: resets cleanly and calls load_dalton_meta() again
    outside_after = resolve_model_billing(
        model_key="b3-cash-model",
        provider="openai",
        model_id="b3-cash-id",
        model_config=compiled.model_config,
    )
    assert outside_after == "subscription"
    assert load_spy.call_count == 2


def test_http_get_models_full_response_identical_when_dalton_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Dalton is disabled, the full `/api/v1/models` response must match the exact
    pre-Batch-3 (`f0e5497ba`) response shape without calling `annotate_model_metadata_price_source`."""
    from ptc_agent.config.agent import COMPACTION_PROFILES
    from src.config.settings import get_prompt_guidance_default
    from src.dalton.llm.model_prefs import get_dalton_system_defaults
    from src.dalton.llm.role_policy import annotate_model_metadata_tool_calling
    from src.dalton.profile import resolve_profile
    from src.dalton.services.team_model_catalog import service as catalog_service_mod
    from src.dalton.services.team_model_catalog.service import derive_curated_setup_for_catalog
    from src.llms.gateway_names import gateway_index
    from src.llms.llm import get_configured_llm_models
    from src.llms.task_costs import task_cost_baseline
    from src.server.app import setup as setup_mod
    from src.server.services.llm.config import system_default_models
    from src.server.services.llm.provider_catalog import (
        build_key_candidates,
        build_provider_catalog,
    )
    from src.tools.web.manifest import (
        CAPABILITY_SEARCH,
        providers_with_capability,
        resolve_min_tier,
    )

    compiled = _build_b3_compiled_catalog()
    client = _mount_models_test_client(monkeypatch, compiled=compiled, dalton_enabled=False)
    real_annotate = catalog_service_mod.annotate_model_metadata_price_source

    spy = create_autospec(
        real_annotate,
        spec_set=True,
        side_effect=AssertionError("must not be called when Dalton is disabled"),
    )
    monkeypatch.setattr(catalog_service_mod, "annotate_model_metadata_price_source", spy)

    # Single HTTP call to the endpoint under test
    res_disabled = client.get("/api/v1/models")
    assert res_disabled.status_code == 200
    assert spy.call_count == 0
    body_disabled = res_disabled.json()

    # Independently construct the exact pre-change (`f0e5497ba`) expected payload
    cfg = compiled.model_config
    models_by_provider = get_configured_llm_models(model_config=cfg)
    llm_cfg = (
        setup_mod.agent_config.llm
        if setup_mod.agent_config and setup_mod.agent_config.llm
        else None
    )
    search_providers = {
        name: {
            "display_name": spec.display_name,
            "min_tier": resolve_min_tier(cap),
            "default_depth": cap.default_level,
            "depths": [
                {
                    "name": lv.name,
                    "display_name": lv.display_name,
                    "min_tier": resolve_min_tier(lv),
                }
                for lv in cap.levels
            ],
        }
        for name, spec in providers_with_capability(CAPABILITY_SEARCH).items()
        if (cap := spec.capability(CAPABILITY_SEARCH)) is not None
    }
    expected_f0e5497ba_payload = json.loads(
        json.dumps(
            {
                "models": {
                    provider: {
                        "display_name": cfg.get_display_name(provider),
                        "models": model_list,
                    }
                    for provider, model_list in models_by_provider.items()
                },
                "model_metadata": annotate_model_metadata_tool_calling(
                    cfg.get_model_metadata(), dalton_meta=compiled.dalton_meta
                ),
                "system_defaults": {
                    **system_default_models(),
                    "compaction_model": (llm_cfg.compaction or "") if llm_cfg else "",
                    "summarization_model": (llm_cfg.compaction or "") if llm_cfg else "",
                    "fetch_model": (llm_cfg.fetch_name or "") if llm_cfg else "",
                    "fallback_models": (llm_cfg.fallback or []) if llm_cfg else [],
                    "prompt_guidance": get_prompt_guidance_default(),
                    **get_dalton_system_defaults(),
                },
                "compaction_profiles": COMPACTION_PROFILES,
                "task_cost_baseline": task_cost_baseline(costs=compiled.task_costs),
                "search_providers": search_providers,
                "provider_catalog": build_provider_catalog(cfg),
                "key_candidates": build_key_candidates(cfg),
                "gateway_index": gateway_index(cfg),
                "antigravity_series": compiled.antigravity_series,
                "team_catalog_enabled": False,
                "catalog_revision": compiled.revision,
                **derive_curated_setup_for_catalog(compiled),
                "dalton_profile": resolve_profile().value,
            }
        )
    )

    assert body_disabled == expected_f0e5497ba_payload
    assert body_disabled["team_catalog_enabled"] is False
    assert body_disabled["model_metadata"]["b3-cash-model"]["pricing"] == {"input": 2.0, "output": 8.0}
    assert "pricing_meta" not in body_disabled["model_metadata"]["b3-cash-model"]
    assert "notional_pricing" not in body_disabled["model_metadata"]["b3-sub-inline"]


def test_http_get_models_degrades_gracefully_if_price_source_annotation_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.dalton.services.team_model_catalog import service as catalog_service_mod

    compiled = _build_b3_compiled_catalog()
    client = _mount_models_test_client(monkeypatch, compiled=compiled, dalton_enabled=True)
    monkeypatch.setattr(
        catalog_service_mod,
        "annotate_model_metadata_price_source",
        create_autospec(
            catalog_service_mod.annotate_model_metadata_price_source,
            spec_set=True,
            side_effect=RuntimeError("boom"),
        ),
    )

    with caplog.at_level(logging.WARNING, logger="src.server.app.api_keys"):
        res = client.get("/api/v1/models")

    assert res.status_code == 200
    meta = res.json()["model_metadata"]
    assert "b3-cash-model" in meta
    assert "pricing_meta" not in meta["b3-cash-model"]
    assert any("Failed to annotate model price source metadata" in rec.message for rec in caplog.records)


def test_d054_single_alembic_head_and_upgrade_downgrade_sql() -> None:
    cfg = Config(str(REPO_ROOT / "src/dalton/alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "src/dalton/migrations"))
    script_dir = ScriptDirectory.from_config(cfg)
    heads = script_dir.get_heads()
    assert heads == ["d055"]

    mod = importlib.import_module("src.dalton.migrations.versions.d055_drop_model_price_alerts")
    assert mod.revision == "d055"
    assert mod.down_revision == "d054"

    up_proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "src/dalton/alembic.ini", "upgrade", "d054:d055", "--sql"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "DROP INDEX IF EXISTS ix_dalton_price_alerts_status" in up_proc.stdout
    assert "DROP TABLE IF EXISTS dalton_model_price_alerts" in up_proc.stdout

    down_proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "src/dalton/alembic.ini", "downgrade", "d055:d054", "--sql"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "CREATE TABLE IF NOT EXISTS dalton_model_price_alerts" in down_proc.stdout
    assert "CREATE INDEX IF NOT EXISTS ix_dalton_price_alerts_status" in down_proc.stdout
    assert "REFERENCES dalton_team_model_catalog_revisions(revision)" in down_proc.stdout
